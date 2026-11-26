# -*- coding: utf-8 -*-
"""网站服务内置用户爆料单阶段清洗引擎：优惠券核验 + 全图文多模态审核对比 + 标准 AI-2 入库。"""
from __future__ import annotations

import datetime
import io
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import requests
from PIL import Image, ImageOps

from cache import BOOM_SORT_CACHE, clear_cached_deal_tip
from config import logger
from infrastructure.logging_utils import format_exc_summary
from infrastructure.runtime_io import load_prompt_cached
from models.deal_constants import (
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_USER_AUDIT,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    ERR_ADAPTER_CONFIG_ERROR,
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_USER_AUDIT_REJECTED,
    MAX_CLEAN_RETRIES,
    OK_EXISTING_DEAL_BETTER,
    PROMPT_USER_AUDIT_PATH,
    USER_SUBMIT_SITE,
)
from models.product import normalize_platform
from services.deal_cleaning.ai_client import DealAIClient
from services.deal_cleaning.url_venue import _is_safe_public_http_url
from services.deal_cleaning.workers import commit_batch_results_to_db
from services.deal_submit_service import (
    DealSubmitService,
    USER_SUBMIT_IMAGE_JPEG_QUALITY,
    USER_SUBMIT_MAX_IMAGE_DIM,
)

_MAX_REMOTE_IMAGE_BYTES = 5 * 1024 * 1024
_REMOTE_IMAGE_TIMEOUT_SECONDS = 5.0
_REMOTE_IMAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _fetch_safe_remote_image_bytes(
    url: str,
    *,
    max_bytes: int = _MAX_REMOTE_IMAGE_BYTES,
    timeout: float = _REMOTE_IMAGE_TIMEOUT_SECONDS,
) -> Optional[bytes]:
    """SSRF 安全地下载单张公开图片（每跳校验公网 IP，限大小与超时）。"""
    cur = str(url or "").strip()
    if not cur.lower().startswith(("http://", "https://")):
        return None
    sess = requests.Session()
    try:
        for _ in range(3):
            if not _is_safe_public_http_url(cur):
                return None
            resp = sess.get(
                cur,
                allow_redirects=False,
                timeout=(min(2.5, timeout), timeout),
                stream=True,
                headers={"User-Agent": _REMOTE_IMAGE_UA},
            )
            try:
                status = int(getattr(resp, "status_code", 200) or 200)
                if status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    cur = urljoin(cur, str(resp.headers["Location"]).strip())
                    continue
                if status != 200:
                    return None
                buf = bytearray()
                for chunk in resp.iter_content(chunk_size=16384):
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            return None
                return bytes(buf) if buf else None
            finally:
                resp.close()
        return None
    except Exception:
        return None
    finally:
        sess.close()


def _save_normalized_jpeg(raw_bytes: bytes, dest_path: Path) -> bool:
    """将远端获取的图片字节校验并标准化重编码为纯净 JPEG，供多模态模型安全读取。"""
    if not raw_bytes:
        return False
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
                return False
            if max(w, h) > USER_SUBMIT_MAX_IMAGE_DIM:
                scale = USER_SUBMIT_MAX_IMAGE_DIM / float(max(w, h))
                pil_img = pil_img.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            pil_img.save(dest_path, format="JPEG", quality=USER_SUBMIT_IMAGE_JPEG_QUALITY, optimize=True)
        return dest_path.is_file()
    except Exception:
        return False


def _extract_existing_deal_snapshot(formal_doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """提取正式商品当前在架线报完整摘要及众包反馈计数供多模态对比；无活跃线报时返回 None。"""
    if not isinstance(formal_doc, dict):
        return None
    di = formal_doc.get("deal_info")
    if not isinstance(di, dict) or di.get("is_deal_tip") is not True:
        return None
    stats = formal_doc.get("user_stats") if isinstance(formal_doc.get("user_stats"), dict) else {}
    coupon_labels = [
        str(c.get("label") or "").strip()
        for c in (di.get("coupon_links") or [])
        if isinstance(c, dict) and str(c.get("label") or "").strip()
    ]
    return {
        "source_item_id": str(di.get("source_item_id") or "").strip(),
        "deal_price": di.get("deal_price"),
        "price_logic": str(di.get("price_logic") or "").strip(),
        "card_highlight": str(di.get("card_highlight") or "").strip(),
        "tutorial_steps": [str(s or "").strip() for s in (di.get("tutorial_steps") or [])[:10] if str(s or "").strip()],
        "original_copywriting": str(di.get("original_copywriting") or "").strip()[:1000],
        "coupon_labels": coupon_labels,
        "has_coupon": bool(di.get("coupon_links")),
        "quality_score": int(di.get("quality_score") or 0),
        "source_site": str(di.get("source_site") or "").strip(),
        "source_is_reliable": bool(di.get("source_is_reliable")),
        "buyable_count": int(stats.get("buyable") or 0),
        "unbuyable_count": int(stats.get("unbuyable") or 0),
    }


def _build_audit_prompt_payload(
    doc: Dict[str, Any],
    existing_deal: Optional[Dict[str, Any]],
    *,
    official_img_indices: List[int],
    new_img_indices: List[int],
    existing_img_indices: List[int],
) -> Dict[str, Any]:
    conv_links = doc.get("converted_links") if isinstance(doc.get("converted_links"), list) else []
    main_link = conv_links[0] if conv_links and isinstance(conv_links[0], dict) else {}
    off_prod = main_link.get("official_product") if isinstance(main_link.get("official_product"), dict) else {}
    s1 = doc.get("ai_stage1_result") if isinstance(doc.get("ai_stage1_result"), dict) else {}
    cpn_targets = [
        t for t in (s1.get("extracted_targets") or [])
        if isinstance(t, dict) and t.get("link_id") != "L1" and t.get("text_role_hint") == "coupon_candidate"
    ]
    cpn_raw = str(cpn_targets[0].get("raw_span") or cpn_targets[0].get("clean_input") or "").strip() if cpn_targets else ""
    has_cpn = any(
        isinstance(c, dict) and c.get("link_id") != "L1" and c.get("convert_status") == "coupon_or_activity"
        for c in conv_links
    )
    if existing_deal is not None:
        existing_view: Optional[Dict[str, Any]] = {
            **existing_deal,
            "image_attachment_indices": existing_img_indices,
        }
    else:
        existing_view = None

    return {
        "official_product": {
            "platform": str(main_link.get("platform") or doc.get("platform") or ""),
            "title": str(off_prod.get("title") or doc.get("title") or ""),
            "price": off_prod.get("price"),
            "original_price": off_prod.get("original_price"),
            "activity_price": off_prod.get("activity_price"),
            "shop_name": str(off_prod.get("shop_name") or doc.get("mall_name") or ""),
            "brand": str(off_prod.get("brand") or ""),
            "category": str(off_prod.get("category") or ""),
            "image_attachment_indices": official_img_indices,
        },
        "new_submission": {
            "deal_price": s1.get("deal_price") if s1.get("deal_price") is not None else doc.get("price"),
            "tutorial_text": str(doc.get("tutorial_text") or "").strip(),
            "has_coupon": has_cpn,
            "coupon_input": cpn_raw,
            "image_attachment_indices": new_img_indices,
        },
        "existing_deal": existing_view,
    }


def _evaluate_audit_payload(
    ai_client: DealAIClient,
    payload: Dict[str, Any],
    *,
    existing_deal: Optional[Dict[str, Any]],
    image_paths: List[str],
    prompt_path: str = PROMPT_USER_AUDIT_PATH,
) -> Dict[str, Any]:
    """复用统一低档模型组客户端执行多模态图文审查与对比，异常或证据不一致一律 Fail-Closed。"""
    try:
        prompt_tpl = load_prompt_cached(prompt_path).strip()
        if not prompt_tpl:
            return {"ok": False, "error": "审核提示词模板为空"}
        parsed, err = ai_client.call_json_object(
            prompt_tpl,
            payload,
            file_paths=image_paths or None,
            stage_label="UserDealAudit",
        )
        if parsed is None:
            return {"ok": False, "error": err or "多模态审核调用未成功"}
        if not isinstance(parsed.get("is_content_valid"), bool):
            return {"ok": False, "error": "审核模型未返回合法布尔判定 is_content_valid"}
        if not isinstance(parsed.get("evidence_supports_claims"), bool):
            return {"ok": False, "error": "审核模型未返回合法布尔判定 evidence_supports_claims"}

        evidence_ok = bool(parsed["evidence_supports_claims"])
        is_valid = bool(parsed["is_content_valid"]) and evidence_ok
        reject_reason = str(parsed.get("content_reject_reason") or "").strip()[:120]
        decision_reason = str(parsed.get("decision_reason") or "").strip()[:120]
        if not is_valid:
            should_replace = False
            if not reject_reason:
                reject_reason = decision_reason or "图片证据未能充分支撑所声明的到手价或关键优惠条件"
        elif existing_deal is None:
            should_replace = True
            reject_reason = ""
        else:
            should_replace = parsed.get("should_replace_existing") is True
            reject_reason = ""

        return {
            "ok": True,
            "observed_image_evidence": str(parsed.get("observed_image_evidence") or "").strip()[:400],
            "evidence_supports_claims": evidence_ok,
            "is_content_valid": is_valid,
            "content_reject_reason": reject_reason,
            "should_replace_existing": bool(should_replace),
            "decision_reason": decision_reason,
        }
    except Exception as exc:
        return {"ok": False, "error": format_exc_summary(exc)}


class DealSubmitWorker:
    """网站服务内置的用户爆料单阶段流水线工作线程（单阶段认领 + 断点续跑 + 按需唤醒）。"""

    def __init__(
        self,
        submit_service: DealSubmitService,
        *,
        cleaner: Any = None,
        ai_generator: Optional[Callable[..., Dict[str, Any]]] = None,
        remote_image_fetcher: Optional[Callable[[str], Optional[bytes]]] = None,
        on_formal_updated: Optional[Callable[[], None]] = None,
        poll_interval: float = 15.0,
        maintenance_interval: float = 300.0,
        initial_delay_seconds: float = 5.0,
        batch_size: int = 4,
    ) -> None:
        self.submit_service = submit_service
        self._cleaner = cleaner
        self._owns_cleaner = cleaner is None
        self._ai_generator = ai_generator
        self._audit_ai_client: Optional[DealAIClient] = None
        self._remote_image_fetcher = remote_image_fetcher or _fetch_safe_remote_image_bytes
        self._on_formal_updated = on_formal_updated
        self.poll_interval = max(0.5, float(poll_interval))
        self.maintenance_interval = max(10.0, float(maintenance_interval))
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self.batch_size = max(1, int(batch_size))
        self._wake_event = submit_service.wake_event
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._worker_id = f"DealSubmitWorker_{os.getpid()}_{threading.get_ident()}"

    def _get_cleaner(self) -> Any:
        if self._cleaner is None:
            from services.deal_pool_cleaner import DealPoolCleaner

            self._cleaner = DealPoolCleaner(
                ai2_batch_size=self.batch_size,
                ai_max_workers=2,
                convert_max_workers=2,
                enable_live_unwrap=False,
                ai_caller=self._ai_generator,
            )
            self._owns_cleaner = True
        return self._cleaner

    def _get_audit_ai_client(self, cleaner: Any) -> DealAIClient:
        if self._audit_ai_client is None:
            if self._ai_generator is not None:
                self._audit_ai_client = DealAIClient(ai_caller=self._ai_generator)
            elif getattr(cleaner, "ai_client", None) is not None:
                self._audit_ai_client = cleaner.ai_client
            else:
                self._audit_ai_client = DealAIClient()
        return self._audit_ai_client

    @staticmethod
    def _doc_filter(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "source_site": str(doc.get("source_site") or USER_SUBMIT_SITE).strip(),
            "source_item_id": str(doc.get("source_item_id") or "").strip(),
        }

    def _mark_permanent_failed(
        self,
        repo: Any,
        doc: Dict[str, Any],
        *,
        error_code: str,
        error_msg: str,
        extra_set: Optional[Dict[str, Any]] = None,
    ) -> None:
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        payload: Dict[str, Any] = {
            "clean_status": "cleaned_failed",
            "clean_stage": CLEAN_STAGE_DONE,
            "clean_error_code": error_code,
            "clean_error_msg": str(error_msg or "")[:120],
            "cleaned_at": now_utc,
        }
        if extra_set:
            payload.update(extra_set)
        repo.col.update_many(
            self._doc_filter(doc),
            {
                "$set": payload,
                "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
            },
        )

    def _schedule_retry_or_fail(
        self,
        repo: Any,
        doc: Dict[str, Any],
        *,
        retry_err_msg: str,
        max_err_code: str,
        max_err_msg: str,
    ) -> None:
        new_retries = int(doc.get("clean_retry_count") or 0) + 1
        if new_retries >= MAX_CLEAN_RETRIES:
            self._mark_permanent_failed(
                repo,
                doc,
                error_code=max_err_code,
                error_msg=max_err_msg,
                extra_set={"clean_retry_count": new_retries},
            )
            return
        repo.col.update_many(
            self._doc_filter(doc),
            {
                "$set": {
                    "clean_stage": CLEAN_STAGE_NEED_USER_AUDIT,
                    "clean_retry_count": new_retries,
                    "next_retry_after_ts": round(time.time() + 60.0 * new_retries, 1),
                    "clean_error_msg": str(retry_err_msg or "")[:120],
                },
                "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
            },
        )

    def _ensure_coupon_converted(self, repo: Any, cleaner: Any, doc: Dict[str, Any]) -> str:
        """步骤 A：如有未转链的附加优惠券，完全复用 cleaner.convert_single_target 核验转链。"""
        s1 = doc.get("ai_stage1_result") if isinstance(doc.get("ai_stage1_result"), dict) else {}
        targets = s1.get("extracted_targets") if isinstance(s1.get("extracted_targets"), list) else []
        conv_links = list(doc.get("converted_links") or [])
        existing_lids = {str(c.get("link_id") or "").strip() for c in conv_links if isinstance(c, dict)}
        pending_coupons = [
            t for t in targets
            if isinstance(t, dict)
            and t.get("link_id") != "L1"
            and t.get("text_role_hint") == "coupon_candidate"
            and str(t.get("link_id") or "").strip() not in existing_lids
        ]
        if not pending_coupons:
            return "ready"

        main_link = conv_links[0] if conv_links and isinstance(conv_links[0], dict) else {}
        main_plat = normalize_platform(main_link.get("platform") or doc.get("platform") or "", default="")
        main_id_set = {
            str(x).strip()
            for x in (
                main_link.get("goods_id"),
                main_link.get("goods_sign"),
                doc.get("candidate_goods_id"),
            )
            if str(x or "").strip()
        }

        for cpn_tgt in pending_coupons:
            c_res = cleaner.convert_single_target(cpn_tgt, default_plat=main_plat or "unknown")
            c_status = str(c_res.get("convert_status") or "")
            c_plat = normalize_platform(c_res.get("platform") or "", default="")

            if c_status == "coupon_or_activity":
                if c_plat and main_plat and c_plat != main_plat:
                    self._mark_permanent_failed(
                        repo,
                        doc,
                        error_code=ERR_AI2_COUPON_FAILED_OR_DROPPED,
                        error_msg="附带的优惠券所属平台与当前商品不一致",
                    )
                    return "failed"
                conv_links.append({k: v for k, v in c_res.items() if k != "adapter_res"})
                continue

            if c_status == "product":
                c_ids = {
                    str(x).strip()
                    for x in (c_res.get("goods_id"), c_res.get("goods_sign"))
                    if str(x or "").strip()
                }
                same_item = bool(c_plat == main_plat and (c_ids & main_id_set))
                promo_cpn = str(
                    (c_res.get("promo_payload") or {}).get("coupon_url")
                    or (c_res.get("official_product") or {}).get("coupon_url")
                    or ""
                ).strip()
                if same_item and promo_cpn:
                    conv_links.append({
                        "link_id": c_res.get("link_id") or cpn_tgt.get("link_id") or "L2",
                        "clean_input": c_res.get("clean_input") or cpn_tgt.get("clean_input") or "",
                        "text_role_hint": "coupon_candidate",
                        "convert_status": "coupon_or_activity",
                        "platform": c_plat,
                        "goods_id": "",
                        "goods_sign": "",
                        "error_code": "OK_COUPON",
                        "error_msg": "",
                        "official_product": None,
                        "my_promo_link": promo_cpn,
                        "my_promo_token": str(c_res.get("my_promo_token") or ""),
                    })
                    continue
                self._mark_permanent_failed(
                    repo,
                    doc,
                    error_code=ERR_AI2_COUPON_FAILED_OR_DROPPED,
                    error_msg="附带的优惠券链接指向其他商品或不含领券信息",
                )
                return "failed"

            err_code = str(c_res.get("error_code") or "")
            if err_code in (ERR_ADAPTER_CONVERT_FAILED, ERR_ADAPTER_CONFIG_ERROR):
                self._schedule_retry_or_fail(
                    repo,
                    doc,
                    retry_err_msg=str(c_res.get("error_msg") or "优惠券转链服务暂忙"),
                    max_err_code=ERR_AI2_COUPON_FAILED_OR_DROPPED,
                    max_err_msg="附带的优惠券连续核验失败，请确认后重新提交",
                )
                return "retry"

            self._mark_permanent_failed(
                repo,
                doc,
                error_code=ERR_AI2_COUPON_FAILED_OR_DROPPED,
                error_msg="附带的优惠券链接已失效或无法领券",
            )
            return "failed"

        doc["converted_links"] = conv_links
        repo.col.update_many(self._doc_filter(doc), {"$set": {"converted_links": conv_links}})
        return "ready"

    @staticmethod
    def _extract_doc_product_key(doc: Dict[str, Any]) -> Tuple[str, str]:
        """提取用户爆料文档对应的规范化 `(platform, product_id)` 主键。"""
        conv_links = doc.get("converted_links") if isinstance(doc.get("converted_links"), list) else []
        main_link = conv_links[0] if conv_links and isinstance(conv_links[0], dict) else {}
        plat = normalize_platform(
            doc.get("resolved_platform") or main_link.get("platform") or doc.get("platform") or "",
            default="",
        )
        pid = str(
            doc.get("resolved_product_id") or main_link.get("goods_id") or doc.get("candidate_goods_id") or ""
        ).strip()
        return plat, pid

    def _fetch_formal_doc(self, repo: Any, doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        plat, pid = self._extract_doc_product_key(doc)
        if not plat or not pid:
            return None
        return repo.formal_col.find_one(
            {"platform": plat, "product_id": pid},
            {"_id": 0, "image_url": 1, "deal_info": 1, "user_stats": 1},
        )

    def _materialize_image_to_path(self, raw_url: str, dest_path: Path) -> bool:
        u = str(raw_url or "").strip()
        if not u:
            return False
        if u.startswith("/api/deals/image/"):
            img_id = u.rsplit("/", 1)[-1].strip()
            local_path = self.submit_service.get_image_file_path(img_id)
            if local_path is None:
                return False
            try:
                shutil.copyfile(local_path, dest_path)
                return dest_path.is_file()
            except OSError:
                return False
        if u.lower().startswith(("http://", "https://")):
            raw_bytes = self._remote_image_fetcher(u)
            if not raw_bytes:
                return False
            return _save_normalized_jpeg(raw_bytes, dest_path)
        return False

    def _collect_audit_media(
        self,
        doc: Dict[str, Any],
        formal_doc: Optional[Dict[str, Any]],
        tmp_dir: Path,
    ) -> Optional[Dict[str, Any]]:
        """收集官方商品图、新投稿图与现有在架好价图到临时目录并返回有序路径及索引映射。"""
        file_paths: List[str] = []
        official_indices: List[int] = []
        new_indices: List[int] = []
        existing_indices: List[int] = []

        conv_links = doc.get("converted_links") if isinstance(doc.get("converted_links"), list) else []
        main_link = conv_links[0] if conv_links and isinstance(conv_links[0], dict) else {}
        off_prod = main_link.get("official_product") if isinstance(main_link.get("official_product"), dict) else {}
        off_img_url = str(
            off_prod.get("image_url")
            or doc.get("image_url")
            or ((formal_doc or {}).get("image_url") if isinstance(formal_doc, dict) else "")
            or ""
        ).strip()
        if off_img_url:
            off_dest = tmp_dir / "official_1.jpg"
            if self._materialize_image_to_path(off_img_url, off_dest):
                file_paths.append(str(off_dest))
                official_indices.append(len(file_paths))

        for idx, raw_u in enumerate(doc.get("tutorial_images") or [], 1):
            u = str(raw_u or "").strip()
            if not u:
                continue
            if not u.startswith("/api/deals/image/"):
                return None
            new_dest = tmp_dir / f"new_{idx}.jpg"
            if not self._materialize_image_to_path(u, new_dest):
                return None
            file_paths.append(str(new_dest))
            new_indices.append(len(file_paths))

        if not new_indices:
            return None

        if isinstance(formal_doc, dict) and isinstance(formal_doc.get("deal_info"), dict):
            ex_di = formal_doc["deal_info"]
            if ex_di.get("is_deal_tip") is True:
                for idx, raw_u in enumerate((ex_di.get("user_share_images") or [])[:3], 1):
                    ex_dest = tmp_dir / f"existing_{idx}.jpg"
                    if self._materialize_image_to_path(str(raw_u or ""), ex_dest):
                        file_paths.append(str(ex_dest))
                        existing_indices.append(len(file_paths))

        return {
            "file_paths": file_paths,
            "official_indices": official_indices,
            "new_indices": new_indices,
            "existing_indices": existing_indices,
        }

    def _audit_single_doc(
        self,
        repo: Any,
        ai_client: DealAIClient,
        doc: Dict[str, Any],
    ) -> str:
        """步骤 B：图文全量审核与已有在架好价综合对比；已通过断点则直接复用。"""
        prior_audit = doc.get("user_audit_result")
        if isinstance(prior_audit, dict) and prior_audit.get("passed") is True:
            return "ready"

        formal_doc = self._fetch_formal_doc(repo, doc)
        existing_deal = _extract_existing_deal_snapshot(formal_doc)

        with tempfile.TemporaryDirectory(prefix="deal_audit_") as tmp_dir_str:
            media = self._collect_audit_media(doc, formal_doc, Path(tmp_dir_str))
            if media is None:
                missing_msg = (
                    "上传的截图文件已失效，请重新提交"
                    if any(str(u or "").strip() for u in (doc.get("tutorial_images") or []))
                    else "缺少商品实际下单或结算价格截图凭证，请重新提交"
                )
                self._mark_permanent_failed(
                    repo,
                    doc,
                    error_code=ERR_USER_AUDIT_REJECTED,
                    error_msg=missing_msg,
                )
                return "rejected"

            payload = _build_audit_prompt_payload(
                doc,
                existing_deal,
                official_img_indices=media["official_indices"],
                new_img_indices=media["new_indices"],
                existing_img_indices=media["existing_indices"],
            )
            audit_res = _evaluate_audit_payload(
                ai_client,
                payload,
                existing_deal=existing_deal,
                image_paths=media["file_paths"],
            )

        if not audit_res.get("ok"):
            self._schedule_retry_or_fail(
                repo,
                doc,
                retry_err_msg=str(audit_res.get("error") or "审查调用暂忙"),
                max_err_code=ERR_AI_MAX_RETRIES_EXCEEDED,
                max_err_msg="当前审查人数较多，请稍后重新提交",
            )
            return "retry"

        audit_record: Dict[str, Any] = {
            **audit_res,
            "compared_existing_site": str(existing_deal.get("source_site") or "") if existing_deal else "",
            "compared_existing_item_id": str(existing_deal.get("source_item_id") or "") if existing_deal else "",
            "compared_existing_price": existing_deal.get("deal_price") if existing_deal else None,
        }

        if not audit_res.get("is_content_valid"):
            audit_record["passed"] = False
            reject_msg = audit_res.get("content_reject_reason") or "图文内容与当前商品无关或未通过内容审查"
            self._mark_permanent_failed(
                repo,
                doc,
                error_code=ERR_USER_AUDIT_REJECTED,
                error_msg=reject_msg,
                extra_set={"user_audit_result": audit_record},
            )
            return "rejected"

        if existing_deal is not None and not audit_res.get("should_replace_existing"):
            audit_record["passed"] = True
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            decision_msg = audit_res.get("decision_reason") or "当前商品已有同价有效好价在架，本次暂未替换"
            repo.col.update_many(
                self._doc_filter(doc),
                {
                    "$set": {
                        "clean_status": "cleaned_success",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": OK_EXISTING_DEAL_BETTER,
                        "clean_error_msg": decision_msg,
                        "cleaned_at": now_utc,
                        "user_audit_result": audit_record,
                    },
                    "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                },
            )
            return "skipped_existing"

        audit_record["passed"] = True
        doc["user_audit_result"] = audit_record
        repo.col.update_many(
            self._doc_filter(doc),
            {"$set": {"user_audit_result": audit_record}},
        )
        return "ready"

    def _finalize_with_ai2(
        self,
        repo: Any,
        cleaner: Any,
        ready_docs: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """步骤 C：复用标准 AI-2 终审与正式表写库，确保重试留在本阶段并清理终审失败截图。"""
        stats = {"ai2_processed": 0, "published": 0, "rejected": 0}
        if not ready_docs:
            return stats

        docs_by_id = {str(d.get("source_item_id") or "").strip(): d for d in ready_docs}
        outcomes, _ = cleaner.process_ai2_unit(ready_docs, raw_col=repo.col, quiet_sub_logs=True)
        if not outcomes:
            return stats

        published_targets: List[Tuple[str, str]] = []
        for out in outcomes:
            mid = str(out.get("source_item_id") or "").strip()
            src_doc = docs_by_id.get(mid) or {}
            status = str(out.get("status") or "")
            if status == "retry_pending":
                out["clean_stage"] = CLEAN_STAGE_NEED_USER_AUDIT
            elif status == "success" and isinstance(out.get("product_doc"), dict):
                pdoc = out["product_doc"]
                stats["published"] += 1
                t_plat = normalize_platform(pdoc.get("platform") or "", default="")
                t_pid = str(pdoc.get("product_id") or "").strip()
                if t_plat and t_pid:
                    published_targets.append((t_plat, t_pid))
                audit_meta = src_doc.get("user_audit_result") if isinstance(src_doc.get("user_audit_result"), dict) else {}
                compared_ex_id = str(audit_meta.get("compared_existing_item_id") or "").strip()
                if compared_ex_id:
                    pdoc["_verified_replace_source_item_id"] = compared_ex_id
            elif status in ("failed", "low_score"):
                stats["rejected"] += 1

        commit_batch_results_to_db(outcomes, repo.col, repo.formal_col, cycle_num=0)
        stats["ai2_processed"] = len(outcomes)
        if published_targets:
            for t_plat, t_pid in dict.fromkeys(published_targets):
                clear_cached_deal_tip(t_plat, t_pid)
            BOOM_SORT_CACHE.clear()
            if callable(self._on_formal_updated):
                try:
                    self._on_formal_updated()
                except Exception as exc:
                    logger.warning("用户爆料上架后唤醒回调异常: %s", format_exc_summary(exc))
        return stats

    def run_once(self) -> Dict[str, int]:
        """执行单轮用户爆料单阶段清洗（优惠券转链 -> 全图文多模态审核对比 -> 标准 AI-2 入库）。"""
        repo = self.submit_service.raw_repo
        claimed = repo.claim_stage_docs(
            CLEAN_STAGE_NEED_USER_AUDIT,
            limit=self.batch_size,
            lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
            worker_id=self._worker_id,
            source_site=USER_SUBMIT_SITE,
        )
        summary = {
            "claimed": len(claimed),
            "deferred": 0,
            "audited": 0,
            "promoted": 0,
            "rejected": 0,
            "skipped_existing": 0,
            "coupon_failed": 0,
            "retried": 0,
            "ai2_processed": 0,
            "published": 0,
        }
        if not claimed:
            return summary

        # 同商品同一批次仅串行处理最早提交的一条，其余释放租约顺延至下一轮，防止同批并发互覆与失实状态回写
        claimed_sorted = sorted(
            claimed,
            key=lambda d: (float(d.get("publish_ts") or 0.0), str(d.get("source_item_id") or "")),
        )
        active_docs: List[Dict[str, Any]] = []
        deferred_docs: List[Dict[str, Any]] = []
        seen_product_keys: Set[Tuple[str, str]] = set()
        for doc in claimed_sorted:
            plat_k, pid_k = self._extract_doc_product_key(doc)
            if plat_k and pid_k:
                pkey = (plat_k, pid_k)
                if pkey in seen_product_keys:
                    deferred_docs.append(doc)
                    continue
                seen_product_keys.add(pkey)
            active_docs.append(doc)

        for d_doc in deferred_docs:
            repo.col.update_many(
                self._doc_filter(d_doc),
                {"$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""}},
            )
        summary["deferred"] = len(deferred_docs)

        cleaner = self._get_cleaner()
        ai_client = self._get_audit_ai_client(cleaner)
        ready_docs: List[Dict[str, Any]] = []

        for doc in active_docs:
            conv_state = self._ensure_coupon_converted(repo, cleaner, doc)
            if conv_state == "failed":
                summary["coupon_failed"] += 1
                summary["rejected"] += 1
                continue
            if conv_state == "retry":
                summary["retried"] += 1
                continue

            summary["audited"] += 1
            audit_state = self._audit_single_doc(repo, ai_client, doc)
            if audit_state == "ready":
                summary["promoted"] += 1
                ready_docs.append(doc)
            elif audit_state == "rejected":
                summary["rejected"] += 1
            elif audit_state == "skipped_existing":
                summary["skipped_existing"] += 1
            else:
                summary["retried"] += 1

        ai2_stats = self._finalize_with_ai2(repo, cleaner, ready_docs)
        summary["ai2_processed"] = ai2_stats["ai2_processed"]
        summary["published"] = ai2_stats["published"]
        summary["rejected"] += ai2_stats["rejected"]
        return summary

    def run_maintenance_once(self) -> None:
        """针对 user_submit 队列执行租约回收、重试封顶、30 天本地截图清理与统一 72 小时好价自动过期。"""
        repo = self.submit_service.raw_repo
        repo.reclaim_expired_leases(source_site=USER_SUBMIT_SITE)
        repo.sweep_expired_and_max_retry(source_site=USER_SUBMIT_SITE)
        self.submit_service.purge_expired_images()
        expired = repo.expire_stale_formal_deals(source_site=USER_SUBMIT_SITE)
        if expired > 0:
            clear_cached_deal_tip()
            BOOM_SORT_CACHE.clear()
            if callable(self._on_formal_updated):
                try:
                    self._on_formal_updated()
                except Exception as exc:
                    logger.warning("用户好价 72h 过期后唤醒回调异常: %s", format_exc_summary(exc))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="deal-submit-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("【用户爆料内置清洗】事件驱动工作线程已启动 (poll=%.1fs)", self.poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        if self._owns_cleaner and self._cleaner is not None:
            close_fn = getattr(self._cleaner, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
            self._cleaner = None

    def _run_loop(self) -> None:
        if self.initial_delay_seconds > 0 and not self._stop_event.is_set():
            self._wake_event.wait(timeout=self.initial_delay_seconds)
            self._wake_event.clear()
        last_maint = 0.0
        while not self._stop_event.is_set():
            try:
                res = self.run_once()
                now_mono = time.monotonic()
                if now_mono - last_maint >= self.maintenance_interval:
                    self.run_maintenance_once()
                    last_maint = now_mono
                if res["claimed"] >= self.batch_size or res.get("deferred", 0) > 0:
                    continue
            except Exception as exc:
                logger.warning("【用户爆料内置清洗】轮询处理异常: %s", format_exc_summary(exc))
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()
