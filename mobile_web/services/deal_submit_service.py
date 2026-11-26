# -*- coding: utf-8 -*-
"""详情页锚定用户爆料接入服务：官品核实、安全图片压缩、入池唤醒与个人记录查询。"""
from __future__ import annotations

import base64
import datetime
import hashlib
import io
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from PIL import Image, ImageChops, ImageOps

Image.MAX_IMAGE_PIXELS = 25_000_000

from adapters import PlatformUnavailableError
from auth import mask_phone
from config import logger, settings
from models.deal_constants import (
    CLEAN_STAGE_NEED_USER_AUDIT,
    DEFAULT_RELIABLE_SCORE,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI2_MAIN_DEAD_ONLY_FILLER,
    ERR_AI2_PRODUCT_MISMATCH,
    ERR_AI2_VIRTUAL_OR_JUNK,
    ERR_USER_AUDIT_REJECTED,
    OK_EXISTING_DEAL_BETTER,
    OK_SKIPPED_ALREADY_CLEANED,
    USER_SUBMIT_SITE,
    resolve_reliability_score,
)
from models.product import URL_RE, normalize_platform
from models.user import normalize_promoter_id
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.order_repo import OrderRepository
from repositories.user_repo import UserRepository
from services.deal_cleaning.text_sanitizer import normalize_inline_spaces
from services.deal_commission_service import compute_user_deals_commission_report

USER_SUBMIT_DAILY_LIMIT_AUTHORIZED = 20
USER_SUBMIT_DAILY_LIMIT_UNAUTHORIZED = 2
MAX_TUTORIAL_LEN = 800
MAX_COUPON_LEN = 512

USER_SUBMIT_MAX_IMAGES = 3
USER_SUBMIT_MAX_RAW_IMAGE_BYTES = 8 * 1024 * 1024
USER_SUBMIT_MAX_IMAGE_DIM = 1280
USER_SUBMIT_IMAGE_JPEG_QUALITY = 82
USER_SUBMIT_IMAGE_RETENTION_DAYS = 30
IMAGE_ID_PATTERN = re.compile(r"^dsi_[0-9a-f]{20}$")

_APP_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_IMAGE_DIR = _APP_DIR / "runtime" / "deal_submit_images"

_FRIENDLY_ERROR_LABELS: Dict[str, str] = {
    ERR_AI2_PRODUCT_MISMATCH: "玩法描述或优惠券与当前商品不匹配",
    ERR_AI2_VIRTUAL_OR_JUNK: "包含非正常商品或无效引流内容",
    ERR_AI2_COUPON_FAILED_OR_DROPPED: "附带的优惠券链接已失效或无法领券",
    ERR_AI2_MAIN_DEAD_ONLY_FILLER: "仅含凑单参考，未通过独立好价核验",
}


def _is_user_channel_authorized(user_doc: Any) -> bool:
    """校验用户是否已完成淘宝或拼多多渠道授权（解锁每日 20 条爆料额度）。"""
    if not isinstance(user_doc, dict):
        return False
    auth_status = user_doc.get("auth_status")
    if not isinstance(auth_status, dict):
        return False
    for plat_key in ("taobao", "pdd"):
        ch = auth_status.get(plat_key)
        if isinstance(ch, dict) and ch.get("is_authorized") is True:
            return True
    return False


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


def is_near_identical_deal_image(candidate_jpeg_bytes: bytes, existing_img_path: Path) -> bool:
    """识别经二次 JPEG/PNG 重编码导致字节哈希漂移的原图直接复用（同尺寸下均差<=2.5且显著差异像素<=25，不误杀同模板改价图）。"""
    if not candidate_jpeg_bytes or not existing_img_path.is_file():
        return False
    try:
        with Image.open(io.BytesIO(candidate_jpeg_bytes)) as cand_im, Image.open(existing_img_path) as ex_im:
            cand_g = ImageOps.exif_transpose(cand_im).convert("L")
            ex_g = ImageOps.exif_transpose(ex_im).convert("L")
            if cand_g.size != ex_g.size:
                return False
            hist = ImageChops.difference(cand_g, ex_g).histogram()
            total = sum(hist)
            if total <= 0:
                return False
            mean_diff = sum(i * c for i, c in enumerate(hist)) / float(total)
            gt40_pixels = sum(hist[41:])
            return mean_diff <= 2.5 and gt40_pixels <= 25
    except Exception:
        return False


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

    def _check_daily_quota(self, clean_uid: str, user_doc: Dict[str, Any]) -> None:
        daily_used = self.raw_repo.count_user_daily_submissions(clean_uid)
        if _is_user_channel_authorized(user_doc):
            if daily_used >= USER_SUBMIT_DAILY_LIMIT_AUTHORIZED:
                raise DealSubmitError(
                    f"今日爆料次数已达上限（{USER_SUBMIT_DAILY_LIMIT_AUTHORIZED}条/天），请明天再来",
                    error_code="submit_daily_limit_reached",
                    status_code=429,
                )
        elif daily_used >= USER_SUBMIT_DAILY_LIMIT_UNAUTHORIZED:
            raise DealSubmitError(
                f"未授权账号每日最多提交 {USER_SUBMIT_DAILY_LIMIT_UNAUTHORIZED} 条爆料，完成淘宝或拼多多授权后可提升至每日 {USER_SUBMIT_DAILY_LIMIT_AUTHORIZED} 条，或明天再来",
                error_code="submit_daily_limit_unauth",
                status_code=429,
            )

    def _persist_compressed_images(self, jpeg_blobs: List[bytes]) -> List[str]:
        if not jpeg_blobs:
            return []
        self.image_dir.mkdir(parents=True, exist_ok=True)
        urls: List[str] = []
        for blob in jpeg_blobs:
            digest = hashlib.sha256(blob).hexdigest()[:20]
            img_id = f"dsi_{digest}"
            target_path = self.image_dir / f"{img_id}.jpg"
            if target_path.is_file():
                # 同一内容复用唯一文件并刷新修改时间，防止活跃图被 30 天定时清理误收
                try:
                    os.utime(target_path, None)
                except OSError:
                    pass
            else:
                tmp_path = self.image_dir / f".tmp_{img_id}_{os.getpid()}_{threading.get_ident()}.jpg"
                try:
                    tmp_path.write_bytes(blob)
                    os.replace(tmp_path, target_path)
                finally:
                    tmp_path.unlink(missing_ok=True)
            url = f"/api/deals/image/{img_id}"
            if url not in urls:
                urls.append(url)
        return urls

    def get_image_file_path(self, image_id: str) -> Optional[Path]:
        clean_id = str(image_id or "").strip()
        if not IMAGE_ID_PATTERN.fullmatch(clean_id):
            return None
        file_path = self.image_dir / f"{clean_id}.jpg"
        return file_path if file_path.is_file() else None

    @staticmethod
    def _extract_local_image_ids(raw_urls: Any) -> List[str]:
        ids: List[str] = []
        if not isinstance(raw_urls, list):
            return ids
        for raw_u in raw_urls:
            u = str(raw_u or "").strip()
            if not u.startswith("/api/deals/image/"):
                continue
            img_id = u.rsplit("/", 1)[-1].strip()
            if IMAGE_ID_PATTERN.fullmatch(img_id):
                ids.append(img_id)
        return ids

    def _collect_referenced_image_ids(self) -> Optional[Set[str]]:
        """收集正式表与待审投稿中仍在引用的图片编号；任一库查询失败返回 None (Fail-Closed 放弃清理)。"""
        repo = self.raw_repo
        if repo is None:
            return None
        protected: Set[str] = set()
        try:
            formal_col = getattr(repo, "formal_col", None)
            if formal_col is not None:
                for doc in formal_col.find(
                    {"deal_info.user_share_images": {"$exists": True}},
                    {"_id": 0, "deal_info.user_share_images": 1},
                ):
                    di = doc.get("deal_info") if isinstance(doc.get("deal_info"), dict) else {}
                    protected.update(self._extract_local_image_ids(di.get("user_share_images")))

            raw_col = getattr(repo, "col", None)
            if raw_col is not None:
                for doc in raw_col.find(
                    {"source_site": USER_SUBMIT_SITE, "clean_status": "pending"},
                    {"_id": 0, "tutorial_images": 1},
                ):
                    protected.update(self._extract_local_image_ids(doc.get("tutorial_images")))
        except Exception:
            return None
        return protected

    def purge_expired_images(
        self,
        *,
        max_age_days: float = USER_SUBMIT_IMAGE_RETENTION_DAYS,
        now_ts: Optional[float] = None,
    ) -> int:
        """全站唯一图片清理出口：仅清理超保留期（默认 30 天）且无任何活跃引用的孤儿截图。"""
        if not self.image_dir.is_dir():
            return 0
        protected_ids = self._collect_referenced_image_ids()
        if protected_ids is None:
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
                if entry.stem in protected_ids:
                    continue
                if entry.stat().st_mtime < cutoff_ts:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        if removed > 0:
            logger.info("🧹 已清理超期无引用用户爆料截图 %d 张（保留期 %.0f 天）", removed, float(max_age_days))
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

        self._check_daily_quota(clean_uid, user_doc)
        compressed_blobs = [normalize_and_compress_deal_image(b64) for b64 in raw_imgs]
        candidate_img_ids = {f"dsi_{hashlib.sha256(blob).hexdigest()[:20]}" for blob in compressed_blobs}

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

        if self.raw_repo.has_pending_user_submission(clean_uid, resolved_plat, canonical_pid):
            raise DealSubmitError(
                "该商品已有你提交的爆料正在审查中，请等待审查结果后再提交",
                error_code="duplicate_pending_submission",
                status_code=409,
            )

        existing_img_ids: Set[str] = set()
        deal_tip = detail_data.get("deal_tip") if isinstance(detail_data.get("deal_tip"), dict) else {}
        existing_img_ids.update(self._extract_local_image_ids(deal_tip.get("share_images")))
        formal_col = getattr(self.raw_repo, "formal_col", None)
        if formal_col is not None:
            try:
                formal_doc = formal_col.find_one(
                    {"platform": resolved_plat, "product_id": canonical_pid},
                    {"_id": 0, "deal_info.user_share_images": 1},
                )
                if isinstance(formal_doc, dict) and isinstance(formal_doc.get("deal_info"), dict):
                    existing_img_ids.update(
                        self._extract_local_image_ids(formal_doc["deal_info"].get("user_share_images"))
                    )
            except Exception:
                pass
        is_reused_existing_img = bool(candidate_img_ids & existing_img_ids)
        if not is_reused_existing_img and existing_img_ids:
            for ex_id in existing_img_ids:
                ex_path = self.get_image_file_path(ex_id)
                if ex_path is None:
                    continue
                if any(is_near_identical_deal_image(blob, ex_path) for blob in compressed_blobs):
                    is_reused_existing_img = True
                    break
        if is_reused_existing_img:
            raise DealSubmitError(
                "请上传新的实拍/最新截图，不可直接复用该商品已有爆料图片",
                error_code="duplicate_deal_image",
                status_code=400,
            )

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
            "merged_tip_ids": [tip_id],
            "deal_price": price_val,
            "combined_raw_text": combined_raw_text,
            "extracted_targets": targets,
            "extracted_images": image_urls,
            "strip_spans": [],
        }

        nick = str(user_doc.get("nickname") or "").strip() or mask_phone(user_doc.get("phone")) or "热心拼友"
        user_reliability = resolve_reliability_score(
            user_doc.get("reliability_score"),
            default=DEFAULT_RELIABLE_SCORE,
        )
        raw_record = {
            "source_site": USER_SUBMIT_SITE,
            "source_channel": "detail_anchor",
            "source_item_id": tip_id,
            "reliability_score": user_reliability,
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
                    if "无明显优惠" in raw_err_msg or "官方在售价" in raw_err_msg:
                        state_label = "未达优惠门槛"
                    elif "6小时" in raw_err_msg or "保护期" in raw_err_msg:
                        state_label = "未替换在架好价"
                    else:
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
            },
        }
