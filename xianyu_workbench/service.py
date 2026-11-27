# -*- coding: utf-8 -*-
"""平台无关选品与商品素材核心服务：候选挑品、图片/标价编排、帮拍话术生成与素材管理。"""
from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

from fastapi import HTTPException
from PIL import Image, ImageOps

from .auth import create_token, verify_password
from .bridge import encode_main_site_item_code, generate_ai_copy, parse_ai_json
from .config import WORKBENCH_DIR, WorkbenchConfig, load_config
from .copy_templates import (
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEMPLATE_IDS,
    NONE_TEMPLATE_ID,
    compose_full_description,
    get_combined_template_content,
    get_combined_template_name,
    get_template_content,
    get_template_name,
    list_copy_templates,
    normalize_template_id,
    normalize_template_ids,
    resolve_listing_template_ids,
    strip_platform_names,
)
from .repo import WorkbenchRepository, clean_http_url, clean_id, normalize_platform

Image.MAX_IMAGE_PIXELS = 25_000_000

ALLOWED_STATUSES = frozenset({"draft", "ready", "dismissed"})
ALLOWED_ORIGINS = frozenset({"official", "deal", "upload", "custom"})
PROMPT_PATH = WORKBENCH_DIR / "prompts" / "copywriting.txt"
_MAX_UPLOAD_DIMENSION = 1600
_SCREENSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}\.jpg$")
_UPLOAD_NAME_RE = re.compile(r"^upl_[0-9a-f]{16}\.jpg$")
_PLATFORM_READABLE = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多"}
_COPY_STALE_SECONDS = 150
TITLE_MAX_LEN = 30
LOGGER = logging.getLogger("xianyu_workbench.copy")


def _positive_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val.to_decimal() if hasattr(val, "to_decimal") else val)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(num, 2) if math.isfinite(num) and num > 0 else None


def _nonneg_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        num = float(val.to_decimal() if hasattr(val, "to_decimal") else val)
    except (TypeError, ValueError, OverflowError):
        return default
    return round(num, 2) if math.isfinite(num) and num >= 0 else default


def _iso_utc(val: Any) -> str:
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(val or "")


def _normalize_image_url(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        s = "https:" + s
    if s.startswith(("http://", "https://", "/api/assets/uploads/", "/api/deals/image/", "/api/assets/screenshots/")):
        return s
    return ""


def _normalize_status(raw_status: Any) -> str:
    s = str(raw_status or "draft").strip().lower()
    if s in ("online", "offline"):
        return "ready"
    if s == "rejected":
        return "dismissed"
    return s if s in ALLOWED_STATUSES else "draft"


def _collect_product_media(
    product_doc: Optional[Dict[str, Any]],
    *,
    fallback_image: str = "",
) -> Tuple[List[str], List[str]]:
    """提取官方实拍图列表与爆料实拍图列表，保持去重有序。"""
    official: List[str] = []
    deal: List[str] = []
    seen_official = set()
    seen_deal = set()

    if isinstance(product_doc, dict):
        raw_carousel = product_doc.get("carousel_images")
        candidates = list(raw_carousel) if isinstance(raw_carousel, list) and raw_carousel else []
        main_img = _normalize_image_url(product_doc.get("image_url"))
        if main_img:
            candidates.insert(0, main_img)
        for item in candidates:
            u = _normalize_image_url(item)
            if u and u not in seen_official:
                seen_official.add(u)
                official.append(u)

        di = product_doc.get("deal_info") if isinstance(product_doc.get("deal_info"), dict) else {}
        for item in (di.get("user_share_images") or []):
            u = _normalize_image_url(item)
            if u and u not in seen_deal:
                seen_deal.add(u)
                deal.append(u)

    fb = _normalize_image_url(fallback_image)
    if fb and fb not in seen_official:
        official.append(fb)
    return official, deal


def _resolve_live_product_state(
    product_doc: Optional[Dict[str, Any]],
) -> Tuple[str, Optional[float], bool, Dict[str, Any]]:
    """返回 `(live_status, live_price, is_active_deal, deal_info)`。"""
    if not isinstance(product_doc, dict):
        return "missing", None, False, {}
    di = product_doc.get("deal_info") if isinstance(product_doc.get("deal_info"), dict) else {}
    is_active_deal = bool(di.get("is_deal_tip") is True)
    if product_doc.get("is_off_promotion") is True:
        return "off_promotion", None, is_active_deal, di

    deal_price = _positive_float(di.get("deal_price")) if is_active_deal else None
    official_price = _positive_float(product_doc.get("activity_price")) or _positive_float(product_doc.get("price"))
    live_price = deal_price if deal_price is not None else official_price
    return "online", live_price, is_active_deal, di


class WorkbenchService:
    def __init__(
        self,
        repo: WorkbenchRepository,
        config: Optional[WorkbenchConfig] = None,
        *,
        ai_generator: Optional[Callable[[str], Dict[str, Any]]] = None,
        json_parser: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.repo = repo
        self.config = config or load_config()
        self._ai_generator = ai_generator or generate_ai_copy
        self._json_parser = json_parser or parse_ai_json
        self._copy_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xy_copy")
        self._copy_lock = threading.Lock()
        self._copy_inflight: Set[Tuple[str, str]] = set()
        self._shutting_down = threading.Event()

    def shutdown(self, wait: bool = True) -> None:
        """停机时停止接收并等待或取消后台文案生成任务，避免悬挂线程访问已关闭库连接。"""
        self._shutting_down.set()
        try:
            self._copy_executor.shutdown(wait=wait, cancel_futures=True)
        except TypeError:
            self._copy_executor.shutdown(wait=wait)

    # =========================================================================
    # 账号登录与主站收藏 UID 绑定
    # =========================================================================
    def authenticate(self, username: str, password: str) -> Dict[str, Any]:
        clean_user = str(username or "").strip()
        user = self.repo.get_user(clean_user)
        if not user or not verify_password(password, user.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = create_token(clean_user, self.config.auth_secret, self.config.token_ttl_seconds)
        return {
            "token": token,
            "user": {
                "username": clean_user,
                "bound_shop_uid": str(user.get("bound_shop_uid") or "").strip(),
            },
        }

    def get_me(self, username: str) -> Dict[str, Any]:
        user = self.repo.get_user(username)
        if not user:
            raise HTTPException(status_code=401, detail="账号不存在或已失效")
        return {
            "username": user["username"],
            "bound_shop_uid": str(user.get("bound_shop_uid") or "").strip(),
            "item_counts": self.repo.get_item_counts(user["username"]),
            "main_site_port": int(getattr(self.config, "main_site_port", 8001) or 8001),
            "main_site_base_url": str(getattr(self.config, "main_site_base_url", "") or "").strip(),
            "default_template_id": DEFAULT_TEMPLATE_ID,
            "default_template_ids": list(DEFAULT_TEMPLATE_IDS),
            "copy_templates": list_copy_templates(),
        }

    def update_binding(self, username: str, *, shop_uid: str = "", shop_phone: str = "") -> Dict[str, Any]:
        target_uid = str(shop_uid or "").strip()
        clean_phone = str(shop_phone or "").strip()
        if not target_uid and clean_phone:
            resolved = self.repo.find_shop_uid_by_phone(clean_phone)
            if not resolved:
                raise HTTPException(status_code=404, detail="未找到该手机号对应的主站账号")
            target_uid = resolved
        if not self.repo.update_bound_shop_uid(username, target_uid):
            raise HTTPException(status_code=404, detail="当前工作台账号不存在")
        return {
            "username": username,
            "bound_shop_uid": target_uid,
        }

    # =========================================================================
    # 模块一：极简双源挑品池（比价好货 & 我的收藏）
    # =========================================================================
    def _screenshot_url_if_exists(self, filename: str) -> str:
        clean = str(filename or "").strip()
        if not clean or not _SCREENSHOT_NAME_RE.fullmatch(clean):
            return ""
        path = self.config.screenshot_dir / clean
        return f"/api/assets/screenshots/{clean}" if path.is_file() else ""

    def list_favorite_candidates(
        self,
        owner: str,
        *,
        page: int = 1,
        page_size: int = 20,
        include_off: bool = False,
    ) -> Dict[str, Any]:
        user = self.repo.get_user(owner)
        if not user:
            raise HTTPException(status_code=401, detail="账号不存在")
        bound_uid = str(user.get("bound_shop_uid") or "").strip()
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 20)))
        if not bound_uid:
            return {
                "items": [],
                "total": 0,
                "page": safe_page,
                "page_size": safe_size,
                "has_more": False,
                "needs_binding": True,
                "bound_shop_uid": "",
            }

        fav_docs = self.repo.list_favorites_by_uid(bound_uid)
        keys = [(d.get("platform"), d.get("product_id")) for d in fav_docs]
        prod_map = self.repo.batch_get_products(keys)
        listing_map = self.repo.get_listing_map_by_products(owner, keys)

        filtered: List[Dict[str, Any]] = []
        for fav in fav_docs:
            plat = normalize_platform(fav.get("platform"))
            pid = clean_id(fav.get("product_id"))
            if not plat or not pid or listing_map.get((plat, pid)) is not None:
                continue

            prod = prod_map.get((plat, pid))
            live_status, live_price, is_active_deal, di = _resolve_live_product_state(prod)
            if not include_off and live_status == "off_promotion":
                continue

            snap = fav.get("snapshot") if isinstance(fav.get("snapshot"), dict) else {}
            collected_price = _positive_float(fav.get("collected_price"))
            official_imgs, deal_imgs = _collect_product_media(prod, fallback_image=str(snap.get("image_url") or ""))
            name = str((prod or {}).get("name") or snap.get("name") or "未命名商品").strip()
            ref_price = live_price if live_price is not None else collected_price
            gsign = str((prod or {}).get("goods_sign") or snap.get("goods_sign") or "").strip()
            main_item_code = encode_main_site_item_code(plat, pid, gsign)

            filtered.append({
                "source_origin": "favorite",
                "platform": plat,
                "product_id": pid,
                "main_item_code": main_item_code,
                "name": name,
                "image_url": official_imgs[0] if official_imgs else "",
                "price": ref_price,
                "collected_price": collected_price,
                "live_status": live_status,
                "is_deal_tip": is_active_deal,
                "deal_highlight": str(di.get("card_highlight") or "").strip(),
                "official_image_count": len(official_imgs),
                "deal_image_count": len(deal_imgs),
                "created_at": _iso_utc(fav.get("created_at")),
            })

        total = len(filtered)
        start = (safe_page - 1) * safe_size
        return {
            "items": filtered[start : start + safe_size],
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "has_more": (start + safe_size) < total,
            "needs_binding": False,
            "bound_shop_uid": bound_uid,
        }

    def list_evaluation_candidates(
        self,
        owner: str,
        *,
        source_type: str = "all",
        sort_by: str = "want",
        include_all_winners: bool = False,
        include_off: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 20)))
        clean_source = str(source_type or "all").strip().lower()
        if clean_source not in ("all", "deal_tip", "official"):
            clean_source = "all"
        clean_sort = str(sort_by or "want").strip().lower()
        if clean_sort not in ("want", "ratio"):
            clean_sort = "want"

        query: Dict[str, Any] = {}
        if include_all_winners:
            query["final_winner"] = {"$in": ["my_product", "tie", "xianyu_product"]}
        else:
            query["is_my_product_better"] = True
        if clean_source in ("deal_tip", "official"):
            query["source_type"] = clean_source

        sort_spec = (
            [("best_xianyu_product.want_cnt", -1), ("cost_performance_ratio", -1), ("evaluated_at", -1)]
            if clean_sort == "want"
            else [("cost_performance_ratio", -1), ("evaluated_at", -1)]
        )
        raw_evals = self.repo.list_evaluations(
            query,
            sort_spec,
            limit=2000,
        )

        dedup_evals: List[Dict[str, Any]] = []
        seen_keys = set()
        for ev in raw_evals:
            plat = normalize_platform(ev.get("platform"))
            pid = clean_id(ev.get("product_id"))
            if not plat or not pid or (plat, pid) in seen_keys:
                continue
            seen_keys.add((plat, pid))
            dedup_evals.append(ev)

        keys = [(ev.get("platform"), ev.get("product_id")) for ev in dedup_evals]
        prod_map = self.repo.batch_get_products(keys)
        listing_map = self.repo.get_listing_map_by_products(owner, keys)

        xy_item_ids = [
            str((ev.get("best_xianyu_product") or {}).get("item_id") or "").strip()
            for ev in dedup_evals
        ]
        xy_items_map = self.repo.batch_get_xianyu_items(xy_item_ids)

        filtered: List[Dict[str, Any]] = []
        for ev in dedup_evals:
            plat = normalize_platform(ev.get("platform"))
            pid = clean_id(ev.get("product_id"))
            if listing_map.get((plat, pid)) is not None:
                continue

            prod = prod_map.get((plat, pid))
            live_status, live_price, is_active_deal, di = _resolve_live_product_state(prod)
            if not include_off and live_status == "off_promotion":
                continue

            cand = ev.get("candidate_product") if isinstance(ev.get("candidate_product"), dict) else {}
            bxy = ev.get("best_xianyu_product") if isinstance(ev.get("best_xianyu_product"), dict) else {}
            cr = ev.get("comparison_result") if isinstance(ev.get("comparison_result"), dict) else {}

            eval_price = _positive_float(ev.get("activity_price"))
            ref_price = live_price if live_price is not None else eval_price
            official_imgs, deal_imgs = _collect_product_media(
                prod,
                fallback_image=str(cand.get("image_url") or ""),
            )

            xy_id = str(bxy.get("item_id") or "").strip()
            xy_cached = xy_items_map.get(xy_id) or {}
            shot_file = str(bxy.get("screenshot_file") or xy_cached.get("screenshot_file") or "").strip()
            shot_url = self._screenshot_url_if_exists(shot_file)

            fallback_xy_imgs: List[str] = []
            for candidate_imgs in (
                bxy.get("images"),
                (xy_cached.get("detail") or {}).get("images") if isinstance(xy_cached.get("detail"), dict) else None,
            ):
                if isinstance(candidate_imgs, list):
                    for u in candidate_imgs:
                        cu = _normalize_image_url(u)
                        if cu and cu not in fallback_xy_imgs:
                            fallback_xy_imgs.append(cu)
            if not fallback_xy_imgs and xy_id:
                coarse_list = ((ev.get("xianyu_audit_trail") or {}).get("xianyu_coarse_items")) or []
                for c_item in coarse_list:
                    if isinstance(c_item, dict) and str(c_item.get("item_id") or "").strip() == xy_id:
                        cu = _normalize_image_url(c_item.get("image_url"))
                        if cu:
                            fallback_xy_imgs.append(cu)
                        break

            gsign = str((prod or {}).get("goods_sign") or cand.get("goods_sign") or "").strip()
            main_item_code = encode_main_site_item_code(plat, pid, gsign)
            xy_item_url = clean_http_url(bxy.get("item_url")) or (
                f"https://www.goofish.com/item?id={xy_id}" if xy_id else ""
            )

            filtered.append({
                "source_origin": "xianyu_eval",
                "platform": plat,
                "product_id": pid,
                "main_item_code": main_item_code,
                "source_type": str(ev.get("source_type") or "official"),
                "name": str((prod or {}).get("name") or ev.get("name") or "未命名商品").strip(),
                "image_url": official_imgs[0] if official_imgs else "",
                "price": ref_price,
                "live_status": live_status,
                "is_deal_tip": is_active_deal,
                "deal_highlight": str(di.get("card_highlight") or cand.get("deal_highlight_text") or "").strip(),
                "official_image_count": len(official_imgs),
                "deal_image_count": len(deal_imgs),
                "cost_performance_ratio": ev.get("cost_performance_ratio"),
                "final_winner": ev.get("final_winner"),
                "llm_reason": str(cr.get("llm_reason") or "").strip(),
                "evaluated_at": str(ev.get("evaluated_at") or ""),
                "xianyu_ref": {
                    "item_id": xy_id,
                    "item_url": xy_item_url,
                    "title": str(bxy.get("title") or "").strip(),
                    "comparable_price": _positive_float(bxy.get("comparable_price")),
                    "spec_description": str(bxy.get("spec_description") or "").strip(),
                    "want_cnt": bxy.get("want_cnt"),
                    "screenshot_url": shot_url,
                    "fallback_images": fallback_xy_imgs[:5],
                },
            })

        def _sort_num(val: Any) -> float:
            try:
                return float(val) if val is not None else -1.0
            except (TypeError, ValueError):
                return -1.0

        if clean_sort == "want":
            filtered.sort(
                key=lambda item: (
                    _sort_num((item.get("xianyu_ref") or {}).get("want_cnt")),
                    _sort_num(item.get("cost_performance_ratio")),
                    str(item.get("evaluated_at") or ""),
                ),
                reverse=True,
            )
        else:
            filtered.sort(
                key=lambda item: (
                    _sort_num(item.get("cost_performance_ratio")),
                    _sort_num((item.get("xianyu_ref") or {}).get("want_cnt")),
                    str(item.get("evaluated_at") or ""),
                ),
                reverse=True,
            )

        total = len(filtered)
        start = (safe_page - 1) * safe_size
        return {
            "items": filtered[start : start + safe_size],
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "has_more": (start + safe_size) < total,
            "source_type": clean_source,
            "sort_by": clean_sort,
        }

    def dismiss_candidate(
        self,
        owner: str,
        *,
        platform: str,
        product_id: str,
        source_origin: str = "xianyu_eval",
    ) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        pid = clean_id(product_id)
        if not plat or not pid:
            raise HTTPException(status_code=400, detail="无效的商品标识")

        now = datetime.now(timezone.utc)
        existing = self.repo.get_listing_by_product(owner, plat, pid)
        if existing is not None:
            updated = self.repo.update_listing_by_id(
                owner,
                existing["listing_id"],
                {
                    "status": "dismissed",
                    "copy_gen_token": "",
                    "copy_status": "" if existing.get("copy_status") == "generating" else str(existing.get("copy_status") or ""),
                    "updated_at": now,
                },
            )
            return {"listing_id": (updated or existing).get("listing_id"), "status": "dismissed"}

        prod = self.repo.get_product_one(plat, pid)
        title = str((prod or {}).get("name") or "").strip()[:TITLE_MAX_LEN]
        doc = self.repo.upsert_listing(
            owner,
            plat,
            pid,
            set_fields={"status": "dismissed", "copy_gen_token": "", "updated_at": now},
            set_on_insert={
                "listing_id": f"xyl_{uuid.uuid4().hex[:8]}",
                "owner": owner,
                "platform": plat,
                "product_id": pid,
                "source_origin": source_origin or "xianyu_eval",
                "title": title,
                "description": "",
                "copy_status": "",
                "copy_error": "",
                "images": [],
                "price": 0.0,
                "created_at": now,
                "confirmed_at": None,
            },
        )
        return {"listing_id": (doc or {}).get("listing_id"), "status": "dismissed"}

    # =========================================================================
    # 模块二：平台无关商品素材编排（图片/标价/帮拍话术）
    # =========================================================================
    def _hydrate_item(
        self,
        doc: Dict[str, Any],
        product_doc: Optional[Dict[str, Any]],
        eval_doc: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        live_status, live_price, is_active_deal, di = _resolve_live_product_state(product_doc)
        official_imgs, deal_imgs = _collect_product_media(product_doc)
        plat = normalize_platform(doc.get("platform"))
        pid = clean_id(doc.get("product_id"))
        cand = (
            (eval_doc or {}).get("candidate_product")
            if isinstance((eval_doc or {}).get("candidate_product"), dict)
            else {}
        )
        gsign = str((product_doc or {}).get("goods_sign") or cand.get("goods_sign") or "").strip()
        main_item_code = encode_main_site_item_code(plat, pid, gsign)
        bxy = (
            (eval_doc or {}).get("best_xianyu_product")
            if isinstance((eval_doc or {}).get("best_xianyu_product"), dict)
            else {}
        )
        xy_id = str(bxy.get("item_id") or "").strip()
        xianyu_ref_url = clean_http_url(bxy.get("item_url")) or (
            f"https://www.goofish.com/item?id={xy_id}" if xy_id else ""
        )
        raw_pf = doc.get("publish_failure")
        publish_failure = dict(raw_pf) if isinstance(raw_pf, dict) and raw_pf else None

        copy_status = str(doc.get("copy_status") or "").strip()
        copy_error = str(doc.get("copy_error") or "").strip()
        lid_clean = str(doc.get("listing_id") or "").strip()
        owner_clean = str(doc.get("owner") or "").strip()
        if copy_status == "generating":
            with self._copy_lock:
                inflight = (owner_clean, lid_clean) in self._copy_inflight
            if not inflight:
                upd_at = doc.get("updated_at")
                if isinstance(upd_at, datetime):
                    dt_utc = upd_at.replace(tzinfo=timezone.utc) if upd_at.tzinfo is None else upd_at.astimezone(timezone.utc)
                    if (datetime.now(timezone.utc) - dt_utc).total_seconds() > _COPY_STALE_SECONDS:
                        copy_status = "failed"
                        copy_error = "后台生成已中断，请点击重试"

        # 向后兼容读取老文档的 sell_price / rejected
        raw_price = doc.get("price") if doc.get("price") is not None else doc.get("sell_price")
        display_price = _nonneg_float(raw_price, 0.0)
        raw_images = doc.get("images") if isinstance(doc.get("images"), list) else []
        clean_images: List[Dict[str, str]] = []
        for entry in raw_images:
            if isinstance(entry, dict) and entry.get("url"):
                clean_images.append({
                    "url": str(entry["url"]),
                    "origin": str(entry.get("origin") or "custom"),
                })

        raw_title = str(doc.get("title") or "").strip()
        raw_desc = str(doc.get("description") or "").strip()
        template_ids = resolve_listing_template_ids(doc)
        template_id = template_ids[0] if template_ids else NONE_TEMPLATE_ID
        template_names = [get_template_name(tid) for tid in template_ids]
        template_name = get_combined_template_name(template_ids)
        template_text = get_combined_template_content(template_ids)
        full_description = compose_full_description(raw_desc, template_ids)

        return {
            "listing_id": lid_clean,
            "owner": owner_clean,
            "platform": plat,
            "product_id": pid,
            "main_item_code": main_item_code,
            "source_origin": str(doc.get("source_origin") or "favorite"),
            "status": _normalize_status(doc.get("status")),
            "title": raw_title,
            "description": raw_desc,
            "template_ids": template_ids,
            "template_names": template_names,
            "template_id": template_id,
            "template_name": template_name,
            "template_text": template_text,
            "full_description": full_description,
            "copy_status": copy_status,
            "copy_error": copy_error,
            "images": clean_images,
            "price": display_price,
            "xianyu_ref_url": xianyu_ref_url,
            "publish_failure": publish_failure,
            "live_status": live_status,
            "live_price": live_price,
            "off_promotion_alert": live_status == "off_promotion",
            "is_deal_tip": is_active_deal,
            "deal_highlight": str(di.get("card_highlight") or "").strip(),
            "product_name": str((product_doc or {}).get("name") or doc.get("title") or "").strip(),
            "available_official_images": official_imgs,
            "available_deal_images": deal_imgs,
            "created_at": _iso_utc(doc.get("created_at")),
            "updated_at": _iso_utc(doc.get("updated_at")),
            "confirmed_at": _iso_utc(doc.get("confirmed_at")) if doc.get("confirmed_at") else "",
        }

    def _claim_copy_slot(self, owner: str, listing_id: str) -> str:
        """为后台生成申请互斥槽位与令牌；若已在运行或服务停机中则返回空字符串。"""
        with self._copy_lock:
            if self._shutting_down.is_set():
                return ""
            key = (str(owner).strip(), str(listing_id or "").strip())
            if not key[0] or not key[1] or key in self._copy_inflight:
                return ""
            token = uuid.uuid4().hex[:12]
            self._copy_inflight.add(key)
            return token

    def _release_copy_slot(self, owner: str, listing_id: str) -> None:
        with self._copy_lock:
            self._copy_inflight.discard((str(owner).strip(), str(listing_id or "").strip()))

    def _submit_background_copy(self, owner: str, listing_id: str, token: str) -> None:
        """提交已写好令牌的后台文案任务；提交失败时立即释放槽位并回写失败态。"""
        if not token:
            return
        try:
            self._copy_executor.submit(self._run_background_copy_task, owner, listing_id, token)
        except Exception as exc:
            self._release_copy_slot(owner, listing_id)
            try:
                self.repo.finish_listing_copy_generation(
                    owner,
                    listing_id,
                    token=token,
                    set_fields={
                        "copy_status": "failed",
                        "copy_error": f"后台任务投递失败: {exc}"[:200],
                        "copy_gen_token": "",
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            except Exception:
                pass

    def create_or_open_draft(
        self,
        owner: str,
        *,
        platform: str,
        product_id: str,
        source_origin: str = "favorite",
        auto_generate_copy: bool = True,
    ) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        pid = clean_id(product_id)
        if not plat or not pid:
            raise HTTPException(status_code=400, detail="请提供有效的 platform 和 product_id")

        now = datetime.now(timezone.utc)
        prod = self.repo.get_product_one(plat, pid)
        ev = self.repo.find_best_evaluation_for_product(plat, pid)
        existing = self.repo.get_listing_by_product(owner, plat, pid)
        if existing is not None:
            lid = existing["listing_id"]
            updates: Dict[str, Any] = {}
            if _normalize_status(existing.get("status")) == "dismissed":
                updates["status"] = "draft"
            token = ""
            if auto_generate_copy and not str(existing.get("description") or "").strip():
                token = self._claim_copy_slot(owner, lid)
                if token:
                    updates.update({
                        "copy_status": "generating",
                        "copy_error": "",
                        "copy_gen_token": token,
                    })
            if updates:
                updates["updated_at"] = now
                try:
                    existing = self.repo.update_listing_by_id(owner, lid, updates) or existing
                except Exception:
                    if token:
                        self._release_copy_slot(owner, lid)
                    raise
                if token:
                    self._submit_background_copy(owner, lid, token)
            return self._hydrate_item(existing, prod, ev)

        user = self.repo.get_user(owner) or {}
        bound_uid = str(user.get("bound_shop_uid") or "").strip()
        fav = self.repo.find_favorite_one(bound_uid, plat, pid) if bound_uid else None
        if prod is None and fav is None and ev is None:
            raise HTTPException(status_code=404, detail="未在商品库、收藏夹或比价记录中找到该商品")

        snap = (fav or {}).get("snapshot") if isinstance((fav or {}).get("snapshot"), dict) else {}
        cand = (ev or {}).get("candidate_product") if isinstance((ev or {}).get("candidate_product"), dict) else {}
        fallback_img = str(snap.get("image_url") or cand.get("image_url") or "").strip()
        official_imgs, deal_imgs = _collect_product_media(prod, fallback_image=fallback_img)

        default_images: List[Dict[str, str]] = []
        seen_urls = set()
        for u in official_imgs[: self.config.default_official_image_count]:
            if u not in seen_urls:
                seen_urls.add(u)
                default_images.append({"url": u, "origin": "official"})
        for u in deal_imgs:
            if len(default_images) >= self.config.max_images_per_listing:
                break
            if u not in seen_urls:
                seen_urls.add(u)
                default_images.append({"url": u, "origin": "deal"})

        _live_status, live_price, _is_deal, di = _resolve_live_product_state(prod)
        default_price = (
            live_price
            or _positive_float((fav or {}).get("collected_price"))
            or _positive_float((ev or {}).get("activity_price"))
            or 0.0
        )

        raw_title = strip_platform_names(
            str(di.get("clean_title") or "").strip()
            or str((prod or {}).get("name") or "").strip()
            or str(snap.get("name") or "").strip()
            or str((ev or {}).get("name") or "").strip()
        )
        listing_id = f"xyl_{uuid.uuid4().hex[:8]}"
        token = self._claim_copy_slot(owner, listing_id) if auto_generate_copy else ""

        set_fields: Dict[str, Any] = {"updated_at": now}
        if token:
            set_fields.update({
                "copy_status": "generating",
                "copy_error": "",
                "copy_gen_token": token,
            })

        try:
            created = self.repo.upsert_listing(
                owner,
                plat,
                pid,
                set_fields=set_fields,
                set_on_insert={
                    "listing_id": listing_id,
                    "owner": owner,
                    "platform": plat,
                    "product_id": pid,
                    "source_origin": source_origin or ("xianyu_eval" if ev else "favorite"),
                    "status": "draft",
                    "title": raw_title[:TITLE_MAX_LEN],
                    "description": "",
                    "template_ids": list(DEFAULT_TEMPLATE_IDS),
                    "template_id": DEFAULT_TEMPLATE_ID,
                    "copy_status": "generating" if token else "",
                    "copy_error": "",
                    "copy_gen_token": token,
                    "images": default_images,
                    "price": default_price,
                    "created_at": now,
                    "confirmed_at": None,
                },
            )
        except Exception:
            if token:
                self._release_copy_slot(owner, listing_id)
            raise
        if not created:
            if token:
                self._release_copy_slot(owner, listing_id)
            raise HTTPException(status_code=500, detail="创建商品素材草稿失败")
        if token:
            self._submit_background_copy(owner, created["listing_id"], token)
        return self._hydrate_item(created, prod, ev)

    def get_item_detail(self, owner: str, listing_id: str) -> Dict[str, Any]:
        doc = self.repo.get_listing_by_id(owner, listing_id)
        if not doc:
            raise HTTPException(status_code=404, detail="商品素材不存在或无权查看")
        prod = self.repo.get_product_one(doc["platform"], doc["product_id"])
        ev = self.repo.find_best_evaluation_for_product(doc["platform"], doc["product_id"])
        return self._hydrate_item(doc, prod, ev)

    def clear_item_failure(self, owner: str, listing_id: str) -> Dict[str, Any]:
        updated = self.repo.clear_listing_publish_failure(owner, listing_id)
        if not updated:
            raise HTTPException(status_code=404, detail="商品素材不存在或无权操作")
        prod = self.repo.get_product_one(updated["platform"], updated["product_id"])
        ev = self.repo.find_best_evaluation_for_product(updated["platform"], updated["product_id"])
        return self._hydrate_item(updated, prod, ev)

    def dismiss_item(self, owner: str, listing_id: str) -> Dict[str, Any]:
        """从选品库移出（软删除为 dismissed），保留历史文档以便渠道同步识别并下架在售帖子。"""
        return self.update_item(owner, listing_id, {"status": "dismissed"})

    def _sanitize_images(self, raw_images: Any) -> List[Dict[str, str]]:
        if not isinstance(raw_images, list):
            raise HTTPException(status_code=400, detail="images 必须是数组")
        cleaned: List[Dict[str, str]] = []
        seen = set()
        for entry in raw_images:
            if isinstance(entry, str):
                url = _normalize_image_url(entry)
                origin = "custom"
            elif isinstance(entry, dict):
                url = _normalize_image_url(entry.get("url"))
                raw_origin = str(entry.get("origin") or "custom").strip().lower()
                origin = raw_origin if raw_origin in ALLOWED_ORIGINS else "custom"
            else:
                continue
            if not url:
                raise HTTPException(status_code=400, detail="包含不支持的图片地址格式")
            if url not in seen:
                seen.add(url)
                cleaned.append({"url": url, "origin": origin})
        if len(cleaned) > self.config.max_images_per_listing:
            raise HTTPException(
                status_code=400,
                detail=f"单件商品最多保留 {self.config.max_images_per_listing} 张图片",
            )
        return cleaned

    def update_item(self, owner: str, listing_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.repo.get_listing_by_id(owner, listing_id)
        if not existing:
            raise HTTPException(status_code=404, detail="商品素材不存在或无权编辑")

        now = datetime.now(timezone.utc)
        set_fields: Dict[str, Any] = {"updated_at": now}

        if "title" in updates and updates["title"] is not None:
            set_fields["title"] = str(updates["title"]).strip()[:TITLE_MAX_LEN]
        if "description" in updates and updates["description"] is not None:
            clean_desc = str(updates["description"]).strip()[:2000]
            set_fields["description"] = clean_desc
            if clean_desc:
                set_fields["copy_status"] = "ready"
                set_fields["copy_error"] = ""
                set_fields["copy_gen_token"] = ""
        if "template_ids" in updates and updates["template_ids"] is not None:
            try:
                norm_ids = normalize_template_ids(updates["template_ids"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            set_fields["template_ids"] = norm_ids
            set_fields["template_id"] = norm_ids[0] if norm_ids else NONE_TEMPLATE_ID
        elif "template_id" in updates and updates["template_id"] is not None:
            try:
                norm_id = normalize_template_id(updates["template_id"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            set_fields["template_id"] = norm_id
            set_fields["template_ids"] = [] if norm_id == NONE_TEMPLATE_ID else [norm_id]
        if "images" in updates and updates["images"] is not None:
            set_fields["images"] = self._sanitize_images(updates["images"])
        if "price" in updates and updates["price"] is not None:
            pr = _nonneg_float(updates["price"], -1.0)
            if pr < 0:
                raise HTTPException(status_code=400, detail="标价不能为负数")
            set_fields["price"] = pr

        cur_status = _normalize_status(existing.get("status"))
        target_status = _normalize_status(updates.get("status")) if updates.get("status") else cur_status
        if updates.get("status") is not None:
            raw_req_status = str(updates["status"]).strip().lower()
            if raw_req_status not in ALLOWED_STATUSES:
                raise HTTPException(status_code=400, detail=f"不支持的状态: {raw_req_status}")
            target_status = raw_req_status
            set_fields["status"] = target_status
            if target_status == "dismissed":
                set_fields["copy_gen_token"] = ""
                if existing.get("copy_status") == "generating":
                    set_fields["copy_status"] = ""

        merged = {**existing, **set_fields}
        eff_price = _nonneg_float(
            merged.get("price") if merged.get("price") is not None else merged.get("sell_price"),
            0.0,
        )
        if target_status == "ready":
            if not str(merged.get("title") or "").strip():
                raise HTTPException(status_code=400, detail="加入待上架前请先填写商品标题")
            if not str(merged.get("description") or "").strip():
                raise HTTPException(status_code=400, detail="加入待上架前请先生成或填写商品文案")
            if eff_price <= 0:
                raise HTTPException(status_code=400, detail="加入待上架前请填写大于 0 的标价")
            if not (isinstance(merged.get("images"), list) and len(merged["images"]) >= 1):
                raise HTTPException(status_code=400, detail="加入待上架前请至少保留 1 张商品图片")
            set_fields["confirmed_at"] = now

        updated = self.repo.update_listing_by_id(owner, listing_id, set_fields)
        if not updated:
            raise HTTPException(status_code=404, detail="商品素材不存在")
        prod = self.repo.get_product_one(updated["platform"], updated["product_id"])
        ev = self.repo.find_best_evaluation_for_product(updated["platform"], updated["product_id"])
        return self._hydrate_item(updated, prod, ev)

    def dismiss_item(self, owner: str, listing_id: str) -> Dict[str, Any]:
        """从选品库移出（软删除为 dismissed），保留历史文档以便渠道同步识别并下架在售帖子。"""
        return self.update_item(owner, listing_id, {"status": "dismissed"})

    def _build_copy_prompt(self, doc: Dict[str, Any], prod: Dict[str, Any]) -> Tuple[str, str]:
        di = prod.get("deal_info") if isinstance(prod.get("deal_info"), dict) else {}
        raw_price = doc.get("price") if doc.get("price") is not None else doc.get("sell_price")
        display_price = _nonneg_float(raw_price, 0.0)
        if display_price <= 0:
            _, live_p, _, _ = _resolve_live_product_state(prod)
            if live_p:
                display_price = live_p
        fallback_name = strip_platform_names(prod.get("name") or doc.get("title") or "")

        # 对传入大模型的上下文彻底剥离第三方电商平台名，且不传递发货时效/退货保障以免混入非商品话术
        shop_source = strip_platform_names(prod.get("mall_name") or prod.get("shop_name") or "")
        clean_specs: List[Dict[str, str]] = []
        for sp in (prod.get("specs") or []):
            if not isinstance(sp, dict):
                continue
            lbl = strip_platform_names(sp.get("label") or "")
            val = strip_platform_names(sp.get("value") or "")
            if not lbl or not val or lbl in ("店铺名称", "平台"):
                continue
            clean_specs.append({"label": lbl, "value": val})

        raw_payload: Dict[str, Any] = {
            "product_name": fallback_name,
            "shop_source": shop_source,
            "brand": strip_platform_names(prod.get("brand") or ""),
            "category": strip_platform_names(prod.get("category") or ""),
            "specs": clean_specs,
            "price": display_price if display_price > 0 else None,
            "deal_highlight": strip_platform_names(di.get("card_highlight") or ""),
        }
        context_payload = {k: v for k, v in raw_payload.items() if v}
        prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
        full_prompt = (
            f"{prompt_template}\n\n"
            f"<product_input>\n{json.dumps(context_payload, ensure_ascii=False, indent=2)}\n</product_input>"
        )
        return full_prompt, fallback_name

    def _invoke_copy_llm(self, doc: Dict[str, Any], prod: Dict[str, Any]) -> Dict[str, str]:
        """调用大模型并解析纯商品 `{title, description}`，剥离任何误带出的平台名称。"""
        full_prompt, fallback_name = self._build_copy_prompt(doc, prod)
        res = self._ai_generator(full_prompt)
        raw_content = str((res or {}).get("content") or "").strip()
        status_str = str((res or {}).get("status") or "")
        model_used = str(((res or {}).get("metrics") or {}).get("model_used") or "")
        if ("失败" in status_str and not raw_content) or not raw_content:
            raise ValueError(f"AI 文案生成失败: {status_str or '空响应'}")

        title = ""
        description = ""
        try:
            parsed = self._json_parser(raw_content)
            if isinstance(parsed, dict):
                title = str(parsed.get("title") or "").strip()
                description = str(parsed.get("description") or "").strip()
        except Exception:
            lines = [ln.strip() for ln in raw_content.splitlines() if ln.strip()]
            if lines:
                title = lines[0]
                description = "\n".join(lines[1:]).strip() or lines[0]

        clean_title = strip_platform_names(title) or strip_platform_names(doc.get("title") or fallback_name)
        clean_desc = strip_platform_names(description) or strip_platform_names(raw_content)
        return {
            "title": clean_title[:TITLE_MAX_LEN],
            "description": clean_desc[:2000],
            "model_used": model_used,
        }

    def _run_background_copy_task(self, owner: str, listing_id: str, token: str) -> None:
        try:
            if self._shutting_down.is_set():
                return
            doc = self.repo.get_listing_by_id(owner, listing_id)
            if not doc or _normalize_status(doc.get("status")) == "dismissed":
                return
            if str(doc.get("copy_gen_token") or "") != token:
                return
            prod = self.repo.get_product_one(doc["platform"], doc["product_id"]) or {}
            generated = self._invoke_copy_llm(doc, prod)
            saved = self.repo.finish_listing_copy_generation(
                owner,
                listing_id,
                token=token,
                set_fields={
                    "title": generated["title"],
                    "description": generated["description"],
                    "copy_status": "ready",
                    "copy_error": "",
                    "copy_gen_token": "",
                    "copy_model": generated.get("model_used") or "",
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            if not saved:
                LOGGER.info("跳过回写后台文案（可能已被手动修改或移出）: %s/%s", owner, listing_id)
        except Exception as exc:
            LOGGER.warning("后台文案生成失败 %s/%s: %s", owner, listing_id, exc)
            try:
                self.repo.finish_listing_copy_generation(
                    owner,
                    listing_id,
                    token=token,
                    set_fields={
                        "copy_status": "failed",
                        "copy_error": str(exc)[:200],
                        "copy_gen_token": "",
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            except Exception:
                pass
        finally:
            self._release_copy_slot(owner, listing_id)

    def generate_copy(self, owner: str, listing_id: str) -> Dict[str, Any]:
        doc = self.repo.get_listing_by_id(owner, listing_id)
        if not doc:
            raise HTTPException(status_code=404, detail="商品素材不存在或无权操作")
        prod = self.repo.get_product_one(doc["platform"], doc["product_id"]) or {}
        try:
            generated = self._invoke_copy_llm(doc, prod)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        now = datetime.now(timezone.utc)
        self.repo.update_listing_by_id(
            owner,
            listing_id,
            {
                "title": generated["title"],
                "description": generated["description"],
                "copy_status": "ready",
                "copy_error": "",
                "copy_gen_token": "",
                "copy_model": generated.get("model_used") or "",
                "updated_at": now,
            },
        )
        return {
            "title": generated["title"],
            "description": generated["description"],
            "model_used": generated.get("model_used") or "",
            "copy_status": "ready",
            "copy_error": "",
        }

    def list_items(
        self,
        owner: str,
        *,
        status: str = "active",
        page: int = 1,
        page_size: int = 30,
    ) -> Dict[str, Any]:
        eff = str(status or "active").strip().lower()
        if eff not in ("active", "ready", "draft", "dismissed", "all"):
            raise HTTPException(status_code=400, detail=f"不支持的筛选状态: {eff}")
        rows, total = self.repo.list_listings(owner, status=eff, page=page, page_size=page_size)
        keys = [(r.get("platform"), r.get("product_id")) for r in rows]
        prod_map = self.repo.batch_get_products(keys)
        eval_map = self.repo.batch_get_best_evaluations(keys)
        items = [
            self._hydrate_item(
                r,
                prod_map.get((normalize_platform(r.get("platform")), clean_id(r.get("product_id")))),
                eval_map.get((normalize_platform(r.get("platform")), clean_id(r.get("product_id")))),
            )
            for r in rows
        ]
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 30)))
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "has_more": (safe_page * safe_size) < total,
            "item_counts": self.repo.get_item_counts(owner),
        }

    # =========================================================================
    # 图片上传与受保护媒体读取
    # =========================================================================
    def save_uploaded_image_base64(self, image_base64: str) -> str:
        raw = str(image_base64 or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="请提供图片数据")
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1].strip()
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="图片 Base64 编码无效") from exc
        if not blob or len(blob) > self.config.max_upload_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"图片大小需在 1B ~ {self.config.max_upload_bytes // (1024 * 1024)}MB 之间",
            )

        try:
            with Image.open(io.BytesIO(blob)) as img:
                img.verify()
            with Image.open(io.BytesIO(blob)) as img:
                transposed = ImageOps.exif_transpose(img)
                rgb = transposed.convert("RGB")
                if max(rgb.size) > _MAX_UPLOAD_DIMENSION:
                    rgb.thumbnail((_MAX_UPLOAD_DIMENSION, _MAX_UPLOAD_DIMENSION), Image.Resampling.LANCZOS)
                out_buf = io.BytesIO()
                rgb.save(out_buf, format="JPEG", quality=85, optimize=True)
                jpeg_bytes = out_buf.getvalue()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="无法识别的图片文件，请上传 JPG/PNG/WebP 图片") from exc

        self.config.upload_dir.mkdir(parents=True, exist_ok=True)
        filename = f"upl_{uuid.uuid4().hex[:16]}.jpg"
        target_path = self.config.upload_dir / filename
        tmp_path = self.config.upload_dir / f".tmp_{filename}_{os.getpid()}_{threading.get_ident()}"
        try:
            tmp_path.write_bytes(jpeg_bytes)
            os.replace(tmp_path, target_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return f"/api/assets/uploads/{filename}"

    @staticmethod
    def resolve_safe_file(directory: Path, filename: str, pattern: re.Pattern[str]) -> Optional[Path]:
        clean = str(filename or "").strip()
        if not pattern.fullmatch(clean):
            return None
        base_dir = directory.resolve()
        candidate = (base_dir / clean).resolve()
        if candidate.parent != base_dir or not candidate.is_file():
            return None
        return candidate
