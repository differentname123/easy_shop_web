# -*- coding: utf-8 -*-
"""详情页锚定用户爆料接入服务：官品核实、安全图片压缩、入池唤醒与个人记录查询。"""
from __future__ import annotations

import base64
import datetime
import io
import math
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 25_000_000

from adapters import PlatformUnavailableError
from auth import mask_phone
from cache_utils import ThreadSafeTTLCache
from config import logger, settings
from models.deal_constants import (
    CLEAN_STAGE_NEED_USER_AUDIT,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI2_MAIN_DEAD_ONLY_FILLER,
    ERR_AI2_PRODUCT_MISMATCH,
    ERR_AI2_VIRTUAL_OR_JUNK,
    ERR_LOW_QUALITY_SCORE,
    ERR_USER_AUDIT_REJECTED,
    OK_EXISTING_DEAL_BETTER,
    OK_SKIPPED_ALREADY_CLEANED,
    USER_SUBMIT_SITE,
    is_reliable_source_site,
)
from models.product import URL_RE, normalize_platform
from models.user import normalize_promoter_id
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.order_repo import OrderRepository
from repositories.user_repo import UserRepository
from services.deal_cleaning.text_sanitizer import normalize_inline_spaces
from services.deal_commission_service import compute_user_deals_commission_report

USER_SUBMIT_WINDOW_SECONDS = 600
USER_SUBMIT_MAX_PER_WINDOW = 5
MAX_TUTORIAL_LEN = 800
MAX_COUPON_LEN = 512

USER_SUBMIT_MAX_IMAGES = 3
USER_SUBMIT_MAX_RAW_IMAGE_BYTES = 8 * 1024 * 1024
USER_SUBMIT_MAX_IMAGE_DIM = 1280
USER_SUBMIT_IMAGE_JPEG_QUALITY = 82
USER_SUBMIT_IMAGE_RETENTION_DAYS = 30
USER_SUBMIT_IMAGE_MAX_AGE_SECONDS = USER_SUBMIT_IMAGE_RETENTION_DAYS * 86400
IMAGE_ID_PATTERN = re.compile(r"^dsi_[0-9a-f]{20}$")

_APP_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_IMAGE_DIR = _APP_DIR / "runtime" / "deal_submit_images"

_FRIENDLY_ERROR_LABELS: Dict[str, str] = {
    ERR_AI2_PRODUCT_MISMATCH: "玩法描述或优惠券与当前商品不匹配",
    ERR_AI2_VIRTUAL_OR_JUNK: "包含非正常商品或无效引流内容",
    ERR_AI2_COUPON_FAILED_OR_DROPPED: "附带的优惠券链接已失效或无法领券",
    ERR_AI2_MAIN_DEAD_ONLY_FILLER: "仅含凑单参考，未通过独立好价核验",
    ERR_LOW_QUALITY_SCORE: "综合评分未达到严选标准",
}


class DealSubmitError(Exception):
    """用户爆料请求参数或频控异常。"""

    def __init__(self, message: str, *, error_code: str = "invalid_deal_submit", status_code: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


def _build_official_product_snapshot(detail_data: Dict[str, Any], product_url: str) -> Dict[str, Any]:
    price = float(detail_data.get("price") or 0.0)
    original_price = float(detail_data.get("original_price") or price or 0.0)
    return {
        "title": str(detail_data.get("name") or "").strip(),
        "price": round(price, 2),
        "original_price": round(original_price, 2),
        "activity_price": round(price, 2),
        "coupon_amount": round(float(detail_data.get("coupon_amount") or 0.0), 2),
        "shop_name": str(detail_data.get("mall_name") or "").strip(),
        "brand": str(detail_data.get("brand") or "").strip(),
        "category": str(detail_data.get("category") or "").strip(),
        "image_url": str(detail_data.get("image_url") or "").strip(),
        "carousel_images": list(detail_data.get("carousel_images") or []),
        "sales_tip": str(detail_data.get("sales_tip") if detail_data.get("sales_tip") is not None else "0"),
        "platform_name": str(detail_data.get("platform_name") or "").strip(),
        "product_url": product_url,
        "desc": str(detail_data.get("desc") or ""),
        "detail_tags": list(detail_data.get("detail_tags") or []),
        "service_guarantees": list(detail_data.get("service_guarantees") or []),
        "shop_ratings": dict(detail_data.get("shop_ratings") or {}),
        "specs": list(detail_data.get("specs") or []),
        "coupon_info": dict(detail_data.get("coupon_info") or {}),
        "coupon_url": str(detail_data.get("coupon_url") or "").strip(),
        "commission_rate": float(detail_data.get("commission_rate") or 0.0),
        "commission_amount": float(detail_data.get("commission_amount") or 0.0),
    }


def _iso_or_str(val: Any) -> str:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=datetime.timezone.utc)
        return val.isoformat()
    return str(val or "").strip()


def normalize_and_compress_deal_image(raw_b64: str) -> bytes:
    """限制尺寸与体积，等比压缩至最长边 <= 1280px 并重编码为纯净 JPEG。"""
    s = str(raw_b64 or "").strip()
    if not s:
        raise DealSubmitError("上传图片内容为空", error_code="invalid_image", status_code=400)
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1].strip()
    max_b64_len = (USER_SUBMIT_MAX_RAW_IMAGE_BYTES * 4) // 3 + 256
    if len(s) > max_b64_len:
        raise DealSubmitError("单张图片过大，请上传 8MB 以内的图片", error_code="image_too_large", status_code=400)
    try:
        raw_bytes = base64.b64decode(s, validate=True)
    except Exception:
        try:
            raw_bytes = base64.b64decode(s)
        except Exception as exc:
            raise DealSubmitError("图片数据格式无效", error_code="invalid_image", status_code=400) from exc

    if not raw_bytes or len(raw_bytes) > USER_SUBMIT_MAX_RAW_IMAGE_BYTES:
        raise DealSubmitError("单张图片过大，请上传 8MB 以内的图片", error_code="image_too_large", status_code=400)

    try:
        with Image.open(io.BytesIO(raw_bytes)) as opened:
            pil_img = ImageOps.exif_transpose(opened)
            if pil_img.mode in ("RGBA", "LA", "PA") or (pil_img.mode == "P" and "transparency" in pil_img.info):
                rgba = pil_img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                pil_img = bg
            elif pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            w, h = pil_img.size
            if w <= 0 or h <= 0:
                raise ValueError("empty dimensions")
            if max(w, h) > USER_SUBMIT_MAX_IMAGE_DIM:
                scale = USER_SUBMIT_MAX_IMAGE_DIM / float(max(w, h))
                pil_img = pil_img.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            out_buf = io.BytesIO()
            pil_img.save(out_buf, format="JPEG", quality=USER_SUBMIT_IMAGE_JPEG_QUALITY, optimize=True)
            return out_buf.getvalue()
    except DealSubmitError:
        raise
    except Exception as exc:
        raise DealSubmitError("无法识别所选图片，请上传有效的 JPG/PNG/WEBP 图片", error_code="invalid_image", status_code=400) from exc


class DealSubmitService:
    """高内聚用户爆料接入服务：只做入参边界校验、官品核实、本地截图管理与进度查询。"""

    def __init__(
        self,
        *,
        product_service_getter: Optional[Callable[[], Any]] = None,
        raw_repo: Optional[DealRawPoolRepository] = None,
        user_repo: Optional[UserRepository] = None,
        order_repo: Optional[OrderRepository] = None,
        product_service: Any = None,
        image_dir: Optional[Path] = None,
    ):
        self._product_service = product_service
        self._product_service_getter = product_service_getter
        self._raw_repo = raw_repo
        self._user_repo = user_repo
        self._order_repo = order_repo
        self._wake_event = threading.Event()
        self.image_dir = Path(image_dir) if image_dir is not None else _DEFAULT_IMAGE_DIR
        self._rate_cache = ThreadSafeTTLCache(maxsize=4096, ttl=USER_SUBMIT_WINDOW_SECONDS)

    @property
    def wake_event(self):
        return self._wake_event

    def notify_worker(self) -> None:
        self._wake_event.set()

    @property
    def raw_repo(self) -> DealRawPoolRepository:
        if self._raw_repo is None:
            self._raw_repo = DealRawPoolRepository()
        return self._raw_repo

    @property
    def user_repo(self) -> UserRepository:
        if self._user_repo is None:
            self._user_repo = UserRepository()
        return self._user_repo

    @property
    def order_repo(self) -> OrderRepository:
        if self._order_repo is None:
            raw_db = getattr(self._raw_repo, "db", None)
            self._order_repo = OrderRepository(db_instance=raw_db) if raw_db is not None else OrderRepository()
        return self._order_repo

    @property
    def product_service(self) -> Any:
        if self._product_service is not None:
            return self._product_service
        if callable(self._product_service_getter):
            return self._product_service_getter()
        from api.home import product_service as shared_ps

        return shared_ps

    def _consume_rate_limit(self, uid: str) -> None:
        now = time.time()
        cutoff = now - USER_SUBMIT_WINDOW_SECONDS
        key = f"u:{uid}"
        recent = [t for t in (self._rate_cache.get(key) or []) if t >= cutoff]
        if len(recent) >= USER_SUBMIT_MAX_PER_WINDOW:
            self._rate_cache.set(key, recent)
            raise DealSubmitError(
                "10 分钟内提交次数已达上限，请稍后再试",
                error_code="submit_rate_limited",
                status_code=429,
            )
        self._rate_cache.set(key, recent + [now])

    def _persist_compressed_images(self, jpeg_blobs: List[bytes]) -> List[str]:
        if not jpeg_blobs:
            return []
        self.image_dir.mkdir(parents=True, exist_ok=True)
        urls: List[str] = []
        for blob in jpeg_blobs:
            img_id = f"dsi_{secrets.token_hex(10)}"
            (self.image_dir / f"{img_id}.jpg").write_bytes(blob)
            urls.append(f"/api/deals/image/{img_id}")
        return urls

    def get_image_file_path(self, image_id: str) -> Optional[Path]:
        clean_id = str(image_id or "").strip()
        if not IMAGE_ID_PATTERN.fullmatch(clean_id):
            return None
        file_path = self.image_dir / f"{clean_id}.jpg"
        return file_path if file_path.is_file() else None

    def delete_image_urls(self, image_urls: List[str]) -> int:
        """安全删除审核驳回投稿关联的本地压缩图。"""
        removed = 0
        for raw_u in image_urls or []:
            u = str(raw_u or "").strip()
            if not u.startswith("/api/deals/image/"):
                continue
            img_id = u.rsplit("/", 1)[-1].strip()
            path = self.get_image_file_path(img_id)
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    def purge_expired_images(
        self,
        *,
        max_age_days: float = USER_SUBMIT_IMAGE_RETENTION_DAYS,
        now_ts: Optional[float] = None,
    ) -> int:
        """定期清理超过保留期（默认 30 天）的本地用户爆料截图，防止磁盘无界增长。"""
        if not self.image_dir.is_dir():
            return 0
        ref_ts = time.time() if now_ts is None else float(now_ts)
        cutoff_ts = ref_ts - max(0.0, float(max_age_days)) * 86400.0
        removed = 0
        try:
            entries = list(self.image_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.suffix.lower() != ".jpg" or not IMAGE_ID_PATTERN.fullmatch(entry.stem):
                    continue
                if entry.stat().st_mtime < cutoff_ts:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        if removed > 0:
            logger.info("🧹 已清理超期用户爆料截图 %d 张（保留期 %.0f 天）", removed, float(max_age_days))
        return removed

    def submit_user_deal(
        self,
        *,
        uid: str,
        platform: str,
        product_id: str = "",
        goods_id: str = "",
        goods_sign: str = "",
        deal_price: float,
        tutorial_text: str,
        coupon_input: str = "",
        images_base64: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            raise DealSubmitError("请先登录后再提交好价爆料", error_code="unauthorized", status_code=401)

        plat = normalize_platform(platform, default="")
        if not plat:
            raise DealSubmitError("缺少商品平台标识", error_code="unsupported_platform", status_code=400)

        clean_pid_arg = str(product_id or "").strip()
        clean_gid_arg = str(goods_id or "").strip()
        clean_gsign_arg = str(goods_sign or "").strip()
        lookup_key = clean_gsign_arg or clean_gid_arg or clean_pid_arg
        if not lookup_key:
            raise DealSubmitError("缺少主商品标识，请从商品详情页进入", error_code="missing_product_identity", status_code=400)

        try:
            price_val = round(float(deal_price), 2)
        except (TypeError, ValueError):
            raise DealSubmitError("请输入有效的到手价", error_code="invalid_deal_price", status_code=400)
        if not math.isfinite(price_val) or price_val <= 0 or price_val >= 1000000:
            raise DealSubmitError("到手价需大于 0 且小于 1000000", error_code="invalid_deal_price", status_code=400)

        clean_tut = normalize_inline_spaces(str(tutorial_text or "")).strip()
        if not clean_tut:
            raise DealSubmitError("请填写购买步骤或优惠说明", error_code="empty_tutorial", status_code=400)
        if len(clean_tut) > MAX_TUTORIAL_LEN:
            raise DealSubmitError(f"玩法说明不能超过 {MAX_TUTORIAL_LEN} 字", error_code="tutorial_too_long", status_code=400)

        clean_cpn = normalize_inline_spaces(str(coupon_input or "")).strip()
        if len(clean_cpn) > MAX_COUPON_LEN:
            raise DealSubmitError("优惠券链接或口令过长", error_code="coupon_too_long", status_code=400)

        raw_imgs = list(images_base64 or [])
        if not raw_imgs:
            raise DealSubmitError(
                "请至少上传 1 张包含商品实际下单或结算价格的截图",
                error_code="missing_proof_image",
                status_code=400,
            )
        if len(raw_imgs) > USER_SUBMIT_MAX_IMAGES:
            raise DealSubmitError(f"最多上传 {USER_SUBMIT_MAX_IMAGES} 张截图", error_code="too_many_images", status_code=400)

        user_doc = self.user_repo.get_user_by_uid(clean_uid)
        if not user_doc:
            raise DealSubmitError("账号状态无效，请重新登录", error_code="user_not_found", status_code=401)

        self._consume_rate_limit(clean_uid)
        compressed_blobs = [normalize_and_compress_deal_image(b64) for b64 in raw_imgs]

        try:
            detail_res = self.product_service.get_goods_detail(
                platform=plat,
                goods_id_or_sign=lookup_key,
                goods_id=clean_gid_arg or clean_pid_arg or None,
                goods_sign=clean_gsign_arg or None,
                uid=clean_uid,
            )
        except PlatformUnavailableError as exc:
            raise DealSubmitError(str(exc), error_code="unsupported_platform", status_code=400) from exc

        if not isinstance(detail_res, dict) or detail_res.get("status") != "success" or not isinstance(detail_res.get("data"), dict):
            err_msg = (detail_res or {}).get("error_msg") if isinstance(detail_res, dict) else ""
            err_code = (detail_res or {}).get("error_code") if isinstance(detail_res, dict) else ""
            out_code = "unsupported_platform" if err_code in ("platform_unavailable", "capability_unavailable") else "product_not_verified"
            raise DealSubmitError(err_msg or "未能核实到该商品，请返回详情页重试", error_code=out_code, status_code=400)

        detail_data = detail_res["data"]
        resolved_plat = normalize_platform(detail_data.get("platform") or plat, default=plat)
        canonical_pid = str(detail_data.get("product_id") or detail_data.get("goods_id") or detail_data.get("goods_sign") or "").strip()
        canonical_gid = str(detail_data.get("goods_id") or canonical_pid).strip()
        canonical_gsign = str(detail_data.get("goods_sign") or "").strip()
        official_title = str(detail_data.get("name") or "").strip()
        if not canonical_pid or not official_title:
            raise DealSubmitError("商品官方基础信息不完整，暂不支持爆料", error_code="invalid_product_detail", status_code=400)

        product_url = str(detail_data.get("product_url") or "").strip()
        official_card = _build_official_product_snapshot(detail_data, product_url)
        main_anchor_ref = (
            canonical_gsign
            if (resolved_plat == "jd" and canonical_gsign and not canonical_gsign.startswith("jd_img_"))
            else (canonical_gid or canonical_pid)
        )
        main_clean_input = product_url or main_anchor_ref
        targets: List[Dict[str, Any]] = [{
            "link_id": "L1",
            "raw_span": "[#L1]",
            "clean_input": main_clean_input,
            "platform": resolved_plat,
            "text_role_hint": "main_candidate",
        }]
        extracted_links: List[Dict[str, Any]] = (
            [{"url": product_url, "label": "官方商品"}]
            if product_url
            else [{"item_id": main_anchor_ref, "label": "官方商品"}]
        )
        extracted_tokens: List[str] = []

        converted_links: List[Dict[str, Any]] = [{
            "link_id": "L1",
            "text_role_hint": "main_candidate",
            "convert_status": "product",
            "platform": resolved_plat,
            "goods_id": canonical_pid,
            "goods_sign": canonical_gsign,
            "my_promo_link": "",
            "my_promo_token": "",
            "clean_input": main_clean_input,
            "official_product": official_card,
        }]

        if clean_cpn:
            cpn_urls = URL_RE.findall(clean_cpn)
            cpn_clean_input = cpn_urls[0] if cpn_urls else clean_cpn
            targets.append({
                "link_id": "L2",
                "raw_span": clean_cpn,
                "clean_input": cpn_clean_input,
                "platform": resolved_plat,
                "text_role_hint": "coupon_candidate",
            })
            if cpn_urls:
                extracted_links.append({"url": cpn_clean_input, "label": "优惠券"})
            else:
                extracted_tokens.append(cpn_clean_input)
            combined_raw_text = f"主商品：[#L1]\n领券：[#L2]\n到手价：{price_val:.2f}元\n购买玩法：{clean_tut}"
            tutorial_text_db = f"{clean_tut}\n领券：{cpn_clean_input}"
        else:
            combined_raw_text = f"主商品：[#L1]\n到手价：{price_val:.2f}元\n购买玩法：{clean_tut}"
            tutorial_text_db = clean_tut

        image_urls = self._persist_compressed_images(compressed_blobs)
        tip_id = f"us_{secrets.token_hex(10)}"

        ai_stage1_result = {
            "main_tip_id": tip_id,
            "is_standalone_deal": True,
            "merged_tip_ids": [tip_id],
            "deal_price": price_val,
            "tutorial_score": 80,
            "combined_raw_text": combined_raw_text,
            "extracted_targets": targets,
            "extracted_images": image_urls,
            "strip_spans": [],
        }

        nick = str(user_doc.get("nickname") or "").strip() or mask_phone(user_doc.get("phone")) or "热心拼友"
        raw_record = {
            "source_site": USER_SUBMIT_SITE,
            "source_channel": "detail_anchor",
            "source_item_id": tip_id,
            "source_is_reliable": is_reliable_source_site(USER_SUBMIT_SITE),
            "is_group_stream": False,
            "has_full_detail": True,
            "platform": resolved_plat,
            "candidate_goods_id": canonical_pid,
            "resolved_platform": resolved_plat,
            "resolved_product_id": canonical_pid,
            "resolved_goods_sign": canonical_gsign,
            "resolved_official_title": official_title,
            "title": official_title,
            "price_text": f"¥{price_val:.2f}",
            "price": price_val,
            "image_url": str(detail_data.get("image_url") or "").strip(),
            "mall_name": str(detail_data.get("mall_name") or "").strip(),
            "tutorial_text": tutorial_text_db,
            "tutorial_images": image_urls,
            "extracted_links": extracted_links,
            "extracted_tokens": extracted_tokens,
            "publish_ts": time.time(),
            "submitted_by_uid": clean_uid,
            "clean_stage": CLEAN_STAGE_NEED_USER_AUDIT,
            "ai_stage1_result": ai_stage1_result,
            "converted_links": converted_links,
            "hot_metrics": {"provider_nickname": nick},
        }
        res = self.raw_repo.save_batch([raw_record], task_name=USER_SUBMIT_SITE)
        if res.get("error"):
            self.delete_image_urls(image_urls)
            raise DealSubmitError("服务暂时不可用，请稍后重试", error_code="db_write_error", status_code=503)
        self.notify_worker()

        return {
            "status": "success",
            "data": {
                "tip_id": tip_id,
                "state": "pending",
                "platform": resolved_plat,
                "product_id": canonical_pid,
                "deal_price": price_val,
                "image_count": len(image_urls),
                "message": "提交成功，已进入人工审查",
            },
        }

    def get_my_submissions(self, uid: str, limit: int = 30) -> Dict[str, Any]:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            raise DealSubmitError("请先登录", error_code="unauthorized", status_code=401)
        rows = self.raw_repo.list_user_submissions(clean_uid, limit=limit, source_site=USER_SUBMIT_SITE)
        try:
            default_jd_pos = normalize_promoter_id(getattr(settings, "jd_position_id", "") or "")
        except ValueError:
            default_jd_pos = ""
        attr_by_tid, report_summary = compute_user_deals_commission_report(
            rows,
            order_repo=self.order_repo,
            formal_col=getattr(self.raw_repo, "formal_col", None),
            default_jd_position_id=default_jd_pos,
        )

        items: List[Dict[str, Any]] = []
        for row in rows:
            c_status = str(row.get("clean_status") or "pending")
            err_code = str(row.get("clean_error_code") or "")
            raw_err_msg = str(row.get("clean_error_msg") or "").strip()
            if c_status == "pending":
                ui_state = "pending"
                state_label = "人工审查中"
                reason = "已提交，正在人工审查中"
            elif c_status == "cleaned_success":
                if err_code == OK_EXISTING_DEAL_BETTER:
                    ui_state = "duplicate"
                    state_label = "已有同价好价"
                    reason = (raw_err_msg or "当前商品已有同价在架好价凭证，本次暂未替换")[:80]
                elif err_code == OK_SKIPPED_ALREADY_CLEANED:
                    ui_state = "duplicate"
                    state_label = "已有同款在架"
                    reason = "该商品近期已有同款好价收录"
                else:
                    ui_state = "published"
                    state_label = "已收录上架"
                    reason = "已同步至严选好价与商品详情页"
            else:
                ui_state = "rejected"
                state_label = "未通过"
                if err_code == ERR_USER_AUDIT_REJECTED:
                    reason = raw_err_msg[:80] or "图文内容与当前商品无关或未通过内容审查"
                elif err_code == ERR_AI2_COUPON_FAILED_OR_DROPPED:
                    reason = (
                        raw_err_msg[:80]
                        if raw_err_msg and not raw_err_msg.startswith("AI-2 ")
                        else _FRIENDLY_ERROR_LABELS[ERR_AI2_COUPON_FAILED_OR_DROPPED]
                    )
                else:
                    reason = _FRIENDLY_ERROR_LABELS.get(err_code) or (raw_err_msg or "未通过核验")[:60]

            s1 = row.get("ai_stage1_result") if isinstance(row.get("ai_stage1_result"), dict) else {}
            res_plat = str(row.get("resolved_platform") or row.get("platform") or "").strip()
            res_pid = str(row.get("resolved_product_id") or row.get("candidate_goods_id") or "").strip()

            imgs = [str(u).strip() for u in (row.get("tutorial_images") or []) if str(u or "").strip()]
            tip_id = str(row.get("source_item_id") or "")
            attr = attr_by_tid.get(tip_id) or {}
            items.append({
                "tip_id": tip_id,
                "title": str(row.get("resolved_official_title") or row.get("title") or "").strip(),
                "deal_price": s1.get("deal_price"),
                "state": ui_state,
                "state_label": state_label,
                "reason": reason,
                "platform": res_plat,
                "product_id": res_pid,
                "goods_sign": str(row.get("resolved_goods_sign") or ""),
                "image_count": len(imgs),
                "created_at": _iso_or_str(row.get("first_seen_at")),
                "cleaned_at": _iso_or_str(row.get("cleaned_at")),
                "display_started_at": _iso_or_str(attr.get("display_started_at")),
                "display_ended_at": _iso_or_str(attr.get("display_ended_at")),
                "display_started_str": attr.get("display_started_str") or "",
                "display_ended_str": attr.get("display_ended_str") or "",
                "display_status": attr.get("display_status") or "none",
                "display_duration_seconds": int(attr.get("display_duration_seconds") or 0),
                "display_duration_text": attr.get("display_duration_text") or "",
                "commission_stats": attr.get("commission_stats"),
            })
        return {
            "status": "success",
            "data": {
                "items": items,
                "summary": report_summary,
                "can_submit": True,
                "strikes": 0,
            },
        }
