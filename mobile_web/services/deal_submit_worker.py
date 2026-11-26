# -*- coding: utf-8 -*-
"""网站服务内置用户爆料单阶段清洗引擎：优惠券核验 + 全图文单阶段多模态审核对比与直接上架。"""
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
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageOps

from adapters import platform_name
from cache import BOOM_SORT_CACHE, clear_cached_deal_tip
from config import logger
from infrastructure.jd_client import jd_coupon_url
from infrastructure.logging_utils import format_exc_summary
from infrastructure.runtime_io import load_prompt_cached
from models.deal_constants import (
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_USER_AUDIT,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEFAULT_NORMAL_RELIABILITY_SCORE,
    DEFAULT_RELIABLE_SCORE,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    ERR_ADAPTER_CONFIG_ERROR,
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_USER_AUDIT_REJECTED,
    MAX_CLEAN_RETRIES,
    OK_EXISTING_DEAL_BETTER,
    PROMPT_USER_AUDIT_PATH,
    RELIABILITY_TIER_GAP,
    USER_SUBMIT_SITE,
    coerce_unix_timestamp,
    evaluate_deal_replacement,
    parse_publish_time_epoch,
    resolve_reliability_score,
)
from models.product import get_source_site_name, normalize_platform
from services.deal_cleaning.ai_client import DealAIClient
from services.deal_cleaning.stages import _attach_preseeded_default_promotion
from services.deal_cleaning.text_sanitizer import (
    clean_coupon_label,
    normalize_inline_spaces,
    sanitize_card_highlight,
    sanitize_clean_tutorial_step,
    sanitize_polished_copywriting,
    strip_markdown_link_wrapper,
)
from services.deal_cleaning.url_venue import (
    _is_safe_public_http_url,
    is_shop_homepage_url,
    rebuild_clean_official_venue_url,
)
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


def _to_naive_utc(dt_val: Any) -> Optional[datetime.datetime]:
    if not isinstance(dt_val, datetime.datetime):
        return None
    if dt_val.tzinfo is not None:
        return dt_val.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt_val


def _extract_existing_deal_snapshot(formal_doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """提取正式商品当前有效在架线报摘要供单阶段多模态对比与可靠分仲裁；超过 72h 视为已失效。"""
    if not isinstance(formal_doc, dict):
        return None
    di = formal_doc.get("deal_info")
    if not isinstance(di, dict) or di.get("is_deal_tip") is not True:
        return None
    upd_naive = _to_naive_utc(di.get("updated_at"))
    if upd_naive is not None:
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        if (now_utc - upd_naive).total_seconds() / 3600.0 >= DEAL_FORMAL_EXPIRY_HOURS:
            return None
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
        "source_site": str(di.get("source_site") or "").strip(),
        "reliability_score": resolve_reliability_score(
            di.get("reliability_score"),
            default=DEFAULT_NORMAL_RELIABILITY_SCORE,
        ),
        "updated_at": upd_naive,
    }


def _build_official_baseline_snapshot(
    doc: Dict[str, Any],
    formal_doc: Optional[Dict[str, Any]],
    *,
    new_score: int,
) -> Optional[Dict[str, Any]]:
    """当商品暂无有效在架好价时，将官方当前在售价抽象为与投稿同级、永远0小时保护的虚拟对比基线。"""
    conv_links = doc.get("converted_links") if isinstance(doc.get("converted_links"), list) else []
    main_link = conv_links[0] if conv_links and isinstance(conv_links[0], dict) else {}
    off_prod = main_link.get("official_product") if isinstance(main_link.get("official_product"), dict) else {}

    off_price: Optional[float] = None
    for cand in (
        off_prod.get("activity_price"),
        off_prod.get("price"),
        (formal_doc or {}).get("price") if isinstance(formal_doc, dict) else None,
    ):
        if isinstance(cand, (int, float)) and not isinstance(cand, bool) and float(cand) > 0:
            off_price = round(float(cand), 2)
            break
    if off_price is None:
        return None

    return {
        "is_official_baseline": True,
        "source_item_id": "",
        "deal_price": off_price,
        "price_logic": "商品官方当前在售价",
        "card_highlight": f"到手{off_price:g}元",
        "tutorial_steps": [],
        "original_copywriting": "",
        "coupon_labels": [],
        "has_coupon": False,
        "source_site": "official_baseline",
        "reliability_score": int(new_score),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    }


def _build_audit_prompt_payload(
    doc: Dict[str, Any],
    existing_deal: Optional[Dict[str, Any]],
    *,
    official_img_indices: List[int],
    new_img_indices: List[int],
    existing_img_indices: List[int],
    need_price_comparison: bool,
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
            k: v
            for k, v in existing_deal.items()
            if k not in ("updated_at", "is_official_baseline")
        }
        existing_view["image_attachment_indices"] = existing_img_indices
    else:
        existing_view = None

    return {
        "need_price_comparison": bool(need_price_comparison and existing_deal is not None),
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
    need_price_comparison: bool = False,
    prompt_path: str = PROMPT_USER_AUDIT_PATH,
) -> Dict[str, Any]:
    """复用统一低档模型组客户端执行单阶段多模态审核、比价与展示规整，异常或契约缺失一律 Fail-Closed。"""
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
        raw_comp = str(parsed.get("price_comparison") or "").strip().lower()
        comp_reason = str(parsed.get("comparison_reason") or parsed.get("decision_reason") or "").strip()[:120]

        if not is_valid:
            price_comparison = "none"
            if not reject_reason:
                reject_reason = comp_reason or "图片证据未能充分支撑所声明的到手价或关键优惠条件"
        else:
            reject_reason = ""
            if existing_deal is not None and need_price_comparison:
                if raw_comp not in ("cheaper", "same", "higher"):
                    return {"ok": False, "error": f"审核模型未返回合法同级价格对比结果: {raw_comp!r}"}
                price_comparison = raw_comp
            else:
                price_comparison = "none"

        raw_steps_val = parsed.get("tutorial_steps")
        if isinstance(raw_steps_val, list):
            raw_step_lines = [str(st or "") for st in raw_steps_val]
        elif isinstance(raw_steps_val, str) and raw_steps_val.strip():
            raw_step_lines = raw_steps_val.splitlines()
        else:
            raw_step_lines = []
        tutorial_steps = [s for s in (sanitize_clean_tutorial_step(st) for st in raw_step_lines) if s]

        clean_title = normalize_inline_spaces(str(parsed.get("clean_title") or "")).strip()
        price_logic = normalize_inline_spaces(str(parsed.get("price_logic") or "")).strip()
        card_highlight = sanitize_card_highlight(parsed.get("card_highlight")) or ""
        raw_cpn_lbl = str(parsed.get("coupon_label") or "").strip()
        coupon_label = clean_coupon_label(raw_cpn_lbl) if raw_cpn_lbl else ""
        polished_copy = sanitize_polished_copywriting(str(parsed.get("polished_copywriting") or ""))
        observed_evidence = str(parsed.get("observed_image_evidence") or "").strip()[:400]

        # 防注入与结构完整性门禁：宣称审核通过时，必须同时具备非空视觉证据描述及有效步骤/推荐文案，严禁被注入空壳回落泄漏原串
        if is_valid and (len(observed_evidence) < 8 or (not tutorial_steps and not polished_copy)):
            return {"ok": False, "error": "审核模型返回通过但缺少完整的视觉证据与规范文案"}

        return {
            "ok": True,
            "observed_image_evidence": observed_evidence,
            "evidence_supports_claims": evidence_ok,
            "is_content_valid": is_valid,
            "content_reject_reason": reject_reason,
            "price_comparison": price_comparison,
            "comparison_reason": comp_reason,
            "decision_reason": comp_reason,
            "clean_title": clean_title,
            "price_logic": price_logic,
            "card_highlight": card_highlight,
            "coupon_label": coupon_label,
            "tutorial_steps": tutorial_steps,
            "polished_copywriting": polished_copy,
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

    def _get_audit_ai_client(self, cleaner: Optional[Any] = None) -> DealAIClient:
        if self._audit_ai_client is None:
            if self._ai_generator is not None:
                self._audit_ai_client = DealAIClient(ai_caller=self._ai_generator)
            elif cleaner is not None and getattr(cleaner, "ai_client", None) is not None:
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

    def _ensure_coupon_converted(self, repo: Any, doc: Dict[str, Any]) -> str:
        """步骤 A：如有未转链的附加优惠券，复用 cleaner.convert_single_target 核验转链；无券时不初始化 cleaner。"""
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
        cleaner = self._get_cleaner()

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
            {"_id": 0, "image_url": 1, "price": 1, "deal_info": 1, "user_stats": 1},
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
        """步骤 B：零成本跨级前置门禁 + 单阶段多模态图文审核/对比/规整；已通过断点则直接复用。"""
        prior_audit = doc.get("user_audit_result")
        if isinstance(prior_audit, dict) and prior_audit.get("passed") is True:
            return "ready"

        new_score = resolve_reliability_score(
            doc.get("reliability_score"),
            default=DEFAULT_RELIABLE_SCORE,
        )
        formal_doc = self._fetch_formal_doc(repo, doc)
        existing_deal = _extract_existing_deal_snapshot(formal_doc)

        if existing_deal is not None and (new_score - existing_deal["reliability_score"]) <= -RELIABILITY_TIER_GAP:
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            msg = f"现有好价可靠分({existing_deal['reliability_score']})高于当前投稿({new_score})，有效期内受保护"
            repo.col.update_many(
                self._doc_filter(doc),
                {
                    "$set": {
                        "clean_status": "cleaned_success",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": OK_EXISTING_DEAL_BETTER,
                        "clean_error_msg": msg,
                        "cleaned_at": now_utc,
                    },
                    "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                },
            )
            return "skipped_existing"

        if existing_deal is None:
            existing_deal = _build_official_baseline_snapshot(doc, formal_doc, new_score=new_score)

        need_price_comparison = bool(
            existing_deal is not None
            and abs(new_score - existing_deal["reliability_score"]) < RELIABILITY_TIER_GAP
        )

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
                need_price_comparison=need_price_comparison,
            )
            audit_res = _evaluate_audit_payload(
                ai_client,
                payload,
                existing_deal=existing_deal,
                image_paths=media["file_paths"],
                need_price_comparison=need_price_comparison,
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

        s1 = doc.get("ai_stage1_result") if isinstance(doc.get("ai_stage1_result"), dict) else {}
        new_dp = s1.get("deal_price") if s1.get("deal_price") is not None else doc.get("price")

        ex_age_hours: Optional[float] = None
        if existing_deal is not None and isinstance(existing_deal.get("updated_at"), datetime.datetime):
            now_naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            ex_age_hours = max(0.0, (now_naive - existing_deal["updated_at"]).total_seconds() / 3600.0)

        should_replace, replace_reason = evaluate_deal_replacement(
            existing_active=existing_deal is not None,
            existing_reliability_score=(
                existing_deal["reliability_score"]
                if existing_deal is not None
                else DEFAULT_NORMAL_RELIABILITY_SCORE
            ),
            new_reliability_score=new_score,
            existing_price=existing_deal.get("deal_price") if existing_deal else None,
            new_price=new_dp,
            existing_age_hours=ex_age_hours,
            allow_higher_price_replace=True,
            price_relation=audit_res.get("price_comparison") if need_price_comparison else None,
        )

        audit_record: Dict[str, Any] = {
            **audit_res,
            "should_replace_existing": bool(audit_res.get("is_content_valid") and should_replace),
            "decision_reason": (
                audit_res.get("comparison_reason")
                or replace_reason
            )[:120],
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

        if existing_deal is not None and not should_replace:
            audit_record["passed"] = True
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if existing_deal.get("is_official_baseline"):
                decision_msg = "当前爆料价与商品官方在售价无明显优惠（需同口径便宜至少5%且不少于1元），暂未收录"
            else:
                decision_msg = replace_reason or audit_record.get("comparison_reason") or "当前商品已有同级有效好价在架，本次暂未替换"
            repo.col.update_many(
                self._doc_filter(doc),
                {
                    "$set": {
                        "clean_status": "cleaned_success",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": OK_EXISTING_DEAL_BETTER,
                        "clean_error_msg": decision_msg[:120],
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

    @staticmethod
    def _build_user_submit_outcome(doc: Dict[str, Any]) -> Dict[str, Any]:
        """直接将单阶段多模态审核产物规整为正式表 `product_doc` 与写库 outcome（跳过冗余 AI-2）。"""
        site = str(doc.get("source_site") or USER_SUBMIT_SITE).strip()
        site_name = get_source_site_name(site)
        channel = str(doc.get("source_channel") or "detail_anchor").strip()
        mid = str(doc.get("source_item_id") or "").strip()
        raw_title = str(doc.get("title") or "").strip()
        s1 = doc.get("ai_stage1_result") if isinstance(doc.get("ai_stage1_result"), dict) else {}
        conv_list = [c for c in (doc.get("converted_links") or []) if isinstance(c, dict)]
        main_conv = conv_list[0] if conv_list else {}
        product_card = main_conv.get("official_product") if isinstance(main_conv.get("official_product"), dict) else {}
        audit_res = doc.get("user_audit_result") if isinstance(doc.get("user_audit_result"), dict) else {}

        plat = normalize_platform(
            doc.get("resolved_platform") or main_conv.get("platform") or doc.get("platform") or "",
            default="",
        )
        goods_id = str(
            doc.get("resolved_product_id") or main_conv.get("goods_id") or doc.get("candidate_goods_id") or ""
        ).strip()
        goods_sign = str(doc.get("resolved_goods_sign") or main_conv.get("goods_sign") or "").strip()
        my_promo_link = str(main_conv.get("my_promo_link") or "")
        my_promo_token = str(main_conv.get("my_promo_token") or "")

        raw_dp = s1.get("deal_price") if s1.get("deal_price") is not None else doc.get("price")
        deal_price = round(float(raw_dp), 2) if isinstance(raw_dp, (int, float)) and not isinstance(raw_dp, bool) and float(raw_dp) > 0 else None

        coupon_links: List[Dict[str, Any]] = []
        seen_cpn_targets: Set[Tuple[str, str]] = set()
        seen_cpn_venues: Set[str] = set()
        cpn_label_override = str(audit_res.get("coupon_label") or "").strip()
        for c_obj in conv_list:
            lid = str(c_obj.get("link_id") or "").strip()
            if not lid or lid == "L1" or c_obj.get("convert_status") != "coupon_or_activity":
                continue
            c_url = str(c_obj.get("my_promo_link") or "").strip()
            c_tok = str(c_obj.get("my_promo_token") or "").strip()
            c_inp = str(c_obj.get("clean_input") or "").strip()
            if is_shop_homepage_url(c_url) or is_shop_homepage_url(c_inp):
                continue
            if not c_url and not c_tok:
                continue
            dedup_sig = (c_url, c_tok)
            if dedup_sig in seen_cpn_targets:
                continue
            venue_sigs = {
                v
                for v in (
                    rebuild_clean_official_venue_url(c_inp),
                    rebuild_clean_official_venue_url(c_url) if c_url else None,
                )
                if v
            }
            if venue_sigs & seen_cpn_venues:
                continue
            seen_cpn_targets.add(dedup_sig)
            seen_cpn_venues.update(venue_sigs)
            coupon_links.append({
                "link_id": lid,
                "label": clean_coupon_label(cpn_label_override or "优惠券"),
                "url": c_url,
                "token": c_tok,
                "platform": str(c_obj.get("platform") or plat).strip().lower() or plat,
            })

        raw_steps = audit_res.get("tutorial_steps") if isinstance(audit_res.get("tutorial_steps"), list) else []
        tutorial_steps = [s for s in (sanitize_clean_tutorial_step(st) for st in raw_steps) if s]
        raw_copy = str(audit_res.get("polished_copywriting") or "").strip()
        sanitized_copy = sanitize_polished_copywriting(raw_copy) if raw_copy else ""
        if not tutorial_steps:
            fallback_src = sanitized_copy or str(doc.get("tutorial_text") or "")
            fallback_tut = sanitize_clean_tutorial_step(fallback_src)
            if fallback_tut:
                tutorial_steps = [fallback_tut]
        if not coupon_links:
            empty_cpn_ui_rewrites = (
                ("先在上方【领券】处", "通过下方购买链接"),
                ("在上方【领券】处", "通过下方购买链接"),
                ("上方【领券】处", "下方购买链接"),
                ("上方【领券】", "下方购买链接"),
                ("先在上方领券处", "通过下方购买链接"),
                ("在上方领券处", "通过下方购买链接"),
                ("在上方领券", "通过下方购买链接"),
                ("上方领券处", "下方购买链接"),
                ("上方领券", "下方购买链接"),
            )
            rewritten_steps: List[str] = []
            for step in tutorial_steps:
                for old_ph, new_ph in empty_cpn_ui_rewrites:
                    if old_ph in step:
                        step = step.replace(old_ph, new_ph)
                rewritten_steps.append(step)
            tutorial_steps = rewritten_steps
        tutorial_text = "\n".join(tutorial_steps)

        clean_title = (
            normalize_inline_spaces(str(audit_res.get("clean_title") or "")).strip()
            or normalize_inline_spaces(str(product_card.get("title") or doc.get("resolved_official_title") or raw_title)).strip()
        )
        price_logic = normalize_inline_spaces(str(audit_res.get("price_logic") or "")).strip() or None
        card_highlight = sanitize_card_highlight(audit_res.get("card_highlight"))
        if not card_highlight and deal_price is not None:
            card_highlight = f"到手{deal_price:g}元"
        original_copywriting = (
            sanitized_copy
            or normalize_inline_spaces(str(doc.get("tutorial_text") or "")).strip()
        )

        official_img = str(product_card.get("image_url") or doc.get("image_url") or "").strip()
        user_share_images: List[str] = []
        for raw_u in doc.get("tutorial_images") or []:
            u = strip_markdown_link_wrapper(str(raw_u or "").strip())
            if u and u != official_img and u not in user_share_images:
                user_share_images.append(u)

        official_price = float(product_card.get("price") or 0.0)
        original_price = float(product_card.get("original_price") or official_price)
        official_activity_price = float(product_card.get("activity_price") or official_price)
        coupon_amount = float(product_card.get("coupon_amount") or 0.0)
        saved_price = round(max(coupon_amount, max(0.0, original_price - official_activity_price)), 2)

        pub_ts = (
            coerce_unix_timestamp(doc.get("publish_ts"))
            or parse_publish_time_epoch(doc.get("publish_time"))
            or coerce_unix_timestamp(doc.get("fetched_at"))
            or time.time()
        )
        reliability_score = resolve_reliability_score(
            doc.get("reliability_score"),
            default=DEFAULT_RELIABLE_SCORE,
        )
        deal_info: Dict[str, Any] = {
            "is_deal_tip": True,
            "clean_title": clean_title,
            "card_highlight": card_highlight,
            "publish_ts": float(pub_ts),
            "expire_time": doc.get("expire_time"),
            "deal_price": deal_price,
            "price_logic": price_logic,
            "price_is_reliable": True,
            "price_reliability_reason": "用户已提供实证截图并通过多模态核验",
            "reliability_score": reliability_score,
            "official_price": round(official_price, 2),
            "raw_tip_title": raw_title or str(product_card.get("title") or ""),
            "original_copywriting": original_copywriting,
            "coupon_links": coupon_links,
            "tutorial_steps": tutorial_steps,
            "tutorial_text": tutorial_text,
            "user_share_images": user_share_images,
            "my_promotion_link": my_promo_link,
            "my_promotion_token": my_promo_token,
            "source_site": site,
            "source_site_name": site_name,
            "source_channel": channel,
            "source_item_id": mid,
            "provider_nickname": str((doc.get("hot_metrics") or {}).get("provider_nickname") or "").strip(),
            "source_url": str(doc.get("source_detail_url") or doc.get("source_url") or "").strip(),
            "source_list_page_url": str(doc.get("source_list_page_url") or doc.get("source_url") or "").strip(),
            "source_detail_url": str(doc.get("source_detail_url") or doc.get("source_url") or "").strip(),
            "merged_tip_ids": [mid] if mid else [],
            "merged_source_urls": [],
            "seen_count": int(doc.get("seen_count") or 1),
            "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
            "hot_metrics": doc.get("hot_metrics") or {},
        }

        product_doc: Dict[str, Any] = {
            **product_card,
            "platform": plat,
            "platform_name": product_card.get("platform_name") or platform_name(plat),
            "product_id": goods_id,
            "goods_id": goods_id,
            "goods_sign": goods_sign,
            "deal_source": site,
            "deal_source_name": site_name,
            "name": str(product_card.get("title") or product_card.get("name") or clean_title).strip(),
            "brand": str(product_card.get("brand") or ""),
            "category": str(product_card.get("category") or ""),
            "price": round(official_price, 2),
            "original_price": round(original_price, 2),
            "activity_price": round(official_activity_price, 2),
            "saved_price": round(saved_price, 2),
            "coupon_amount": round(coupon_amount, 2),
            "has_coupon": coupon_amount > 0,
            "commission_rate": float(product_card.get("commission_rate") or 0.0),
            "commission_amount": float(product_card.get("commission_amount") or 0.0),
            "sales_tip": str(product_card.get("sales_tip") if product_card.get("sales_tip") is not None else "0"),
            "image_url": official_img,
            "product_url": str(product_card.get("product_url") or my_promo_link or ""),
            "mall_name": str(product_card.get("shop_name") or doc.get("mall_name") or ""),
            "desc": str(product_card.get("desc") or ""),
            "carousel_images": product_card.get("carousel_images") or ([official_img] if official_img else []),
            "detail_tags": product_card.get("detail_tags") or [],
            "service_guarantees": product_card.get("service_guarantees") or [],
            "shop_ratings": product_card.get("shop_ratings") or {},
            "specs": product_card.get("specs") or [],
            "coupon_info": product_card.get("coupon_info") or {},
            "deal_info": deal_info,
        }
        clean_cpn_url = jd_coupon_url(product_card.get("coupon_url")) if plat == "jd" else ""
        if clean_cpn_url:
            product_doc["coupon_url"] = clean_cpn_url
        else:
            product_doc.pop("coupon_url", None)

        _attach_preseeded_default_promotion(
            plat=plat,
            goods_id=goods_id,
            goods_sign=goods_sign,
            main_conv=main_conv,
            adapter_res={},
            my_promo_link=my_promo_link,
            my_promo_token=my_promo_token,
            product_doc=product_doc,
        )

        compared_ex_id = str(audit_res.get("compared_existing_item_id") or "").strip()
        if compared_ex_id:
            product_doc["_verified_replace_source_item_id"] = compared_ex_id

        conv_summary = [
            {
                "link_id": c.get("link_id"),
                "text_role_hint": c.get("text_role_hint"),
                "convert_status": c.get("convert_status"),
                "platform": c.get("platform"),
                "goods_id": c.get("goods_id"),
                "goods_sign": c.get("goods_sign"),
                "my_promo_link": c.get("my_promo_link"),
                "my_promo_token": c.get("my_promo_token"),
            }
            for c in conv_list
        ]

        return {
            "status": "success",
            "stage_outcome": "success",
            "error_code": "OK",
            "error_msg": "",
            "source_site": site,
            "source_channel": channel,
            "source_item_id": mid,
            "platform": plat,
            "product_id": goods_id,
            "goods_sign": goods_sign,
            "official_title": product_doc["name"],
            "raw_title": raw_title,
            "deal_price": deal_price,
            "official_price": official_price,
            "merged_tip_ids": [mid] if mid else [],
            "original_copywriting": original_copywriting,
            "ai_stage1_result": s1,
            "converted_links": conv_summary,
            "ai_stage2_result": audit_res,
            "winning_anchor": str(main_conv.get("clean_input") or "")[:100],
            "winning_anchor_type": main_conv.get("text_role_hint") or "main_candidate",
            "candidate_count": len(conv_list),
            "tutorial_img_count": len(user_share_images),
            "coupon_link_count": len(coupon_links),
            "product_doc": product_doc,
        }

    def _publish_ready_docs(
        self,
        repo: Any,
        ready_docs: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """步骤 C：直接将单阶段审核通过的用户爆料晋升正式表并清理缓存（无二次 AI-2 调用）。"""
        stats = {"published": 0, "rejected": 0}
        if not ready_docs:
            return stats

        outcomes: List[Dict[str, Any]] = []
        published_targets: List[Tuple[str, str]] = []
        for doc in ready_docs:
            out = self._build_user_submit_outcome(doc)
            outcomes.append(out)
            pdoc = out.get("product_doc")
            if out.get("status") == "success" and isinstance(pdoc, dict):
                stats["published"] += 1
                t_plat = normalize_platform(pdoc.get("platform") or "", default="")
                t_pid = str(pdoc.get("product_id") or "").strip()
                if t_plat and t_pid:
                    published_targets.append((t_plat, t_pid))

        commit_batch_results_to_db(outcomes, repo.col, repo.formal_col, cycle_num=0)
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
        """执行单轮用户爆料单阶段清洗（优惠券转链 -> 全图文单阶段多模态审核对比与直接上架）。"""
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

        ai_client = self._get_audit_ai_client(self._cleaner)
        ready_docs: List[Dict[str, Any]] = []

        for doc in active_docs:
            conv_state = self._ensure_coupon_converted(repo, doc)
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

        pub_stats = self._publish_ready_docs(repo, ready_docs)
        summary["published"] = pub_stats["published"]
        summary["rejected"] += pub_stats["rejected"]
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
