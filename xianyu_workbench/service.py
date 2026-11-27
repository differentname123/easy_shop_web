# -*- coding: utf-8 -*-
"""闲鱼选品工作台核心业务编排：双源选品过滤、默认预览装配、AI 话术生成、图片管理与标准库输出。"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import uuid

from fastapi import HTTPException
from PIL import Image, ImageOps

from .auth import create_token, verify_password
from .bridge import generate_ai_copy, parse_ai_json
from .config import WORKBENCH_DIR, WorkbenchConfig, load_config
from .repo import WorkbenchRepository, clean_id, normalize_platform

Image.MAX_IMAGE_PIXELS = 25_000_000

ALLOWED_STATUSES = frozenset({"draft", "ready", "online", "offline", "rejected"})
ALLOWED_ORIGINS = frozenset({"official", "deal", "upload", "custom"})
ALLOWED_COPY_STYLES = frozenset({"casual", "value"})
PROMPT_PATH = WORKBENCH_DIR / "prompts" / "xianyu_copywriting.txt"
_MAX_UPLOAD_DIMENSION = 1600
_SCREENSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}\.jpg$")
_UPLOAD_NAME_RE = re.compile(r"^upl_[0-9a-f]{16}\.jpg$")


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

    # =========================================================================
    # 账号登录与主站 UID 绑定
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
            "status_counts": self.repo.get_status_counts(user["username"]),
        }

    def update_binding(self, username: str, *, shop_uid: str = "", shop_phone: str = "") -> Dict[str, Any]:
        target_uid = str(shop_uid or "").strip()
        clean_phone = str(shop_phone or "").strip()
        if not target_uid and clean_phone:
            resolved = self.repo.find_shop_uid_by_phone(clean_phone)
            if not resolved:
                raise HTTPException(status_code=404, detail="未找到该手机号对应的主站账号")
            target_uid = resolved
        elif target_uid and not self.repo.shop_uid_exists(target_uid):
            # 允许在完全独立库场景下直接填写 UID，但给出明确提示字段
            pass

        if not self.repo.update_bound_shop_uid(username, target_uid):
            raise HTTPException(status_code=404, detail="当前工作台账号不存在")
        return {
            "username": username,
            "bound_shop_uid": target_uid,
        }

    # =========================================================================
    # 模块一：双源选品候选池（主站收藏 & 闲鱼比价）
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
        include_handled: bool = False,
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
            if not plat or not pid:
                continue
            listing = listing_map.get((plat, pid))
            if not include_handled and listing is not None:
                continue

            prod = prod_map.get((plat, pid))
            live_status, live_price, is_active_deal, di = _resolve_live_product_state(prod)
            if not include_off and live_status == "off_promotion":
                continue

            snap = fav.get("snapshot") if isinstance(fav.get("snapshot"), dict) else {}
            collected_price = _positive_float(fav.get("collected_price"))
            official_imgs, deal_imgs = _collect_product_media(prod, fallback_image=str(snap.get("image_url") or ""))
            name = str((prod or {}).get("name") or snap.get("name") or "未命名商品").strip()
            price_diff = (
                round(live_price - collected_price, 2)
                if live_price is not None and collected_price is not None
                else None
            )

            filtered.append({
                "source_origin": "favorite",
                "platform": plat,
                "product_id": pid,
                "goods_sign": str((prod or {}).get("goods_sign") or snap.get("goods_sign") or "").strip(),
                "name": name,
                "image_url": official_imgs[0] if official_imgs else "",
                "collected_price": collected_price,
                "live_price": live_price,
                "price_diff": price_diff,
                "live_status": live_status,
                "is_deal_tip": is_active_deal,
                "deal_highlight": str(di.get("card_highlight") or "").strip(),
                "official_image_count": len(official_imgs),
                "deal_image_count": len(deal_imgs),
                "listing_status": listing.get("status") if listing else None,
                "listing_id": listing.get("listing_id") if listing else None,
                "created_at": _iso_utc(fav.get("created_at")),
            })

        total = len(filtered)
        start = (safe_page - 1) * safe_size
        page_items = filtered[start : start + safe_size]
        return {
            "items": page_items,
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
        winner: str = "my_product",
        source_type: str = "all",
        min_ratio: float = 1.0,
        sort: str = "ratio",
        include_handled: bool = False,
        include_off: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 20)))

        query: Dict[str, Any] = {}
        if winner == "my_product":
            query["is_my_product_better"] = True
        else:
            query["final_winner"] = {"$in": ["my_product", "tie", "xianyu_product"]}
        if source_type in ("official", "deal_tip"):
            query["source_type"] = source_type
        if min_ratio and float(min_ratio) > 0:
            query["cost_performance_ratio"] = {"$gte": float(min_ratio)}

        sort_spec = (
            [("evaluated_at", -1)]
            if sort == "latest"
            else [("cost_performance_ratio", -1), ("evaluated_at", -1)]
        )
        raw_evals = self.repo.list_evaluations(query, sort_spec, limit=500)

        # 若同一 (platform, product_id) 同时存在 official 与 deal_tip 记录，只保留排名靠前的那条进入待审流
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
            listing = listing_map.get((plat, pid))
            if not include_handled and listing is not None:
                continue

            prod = prod_map.get((plat, pid))
            live_status, live_price, is_active_deal, di = _resolve_live_product_state(prod)
            if not include_off and live_status == "off_promotion":
                continue

            cand = ev.get("candidate_product") if isinstance(ev.get("candidate_product"), dict) else {}
            bxy = ev.get("best_xianyu_product") if isinstance(ev.get("best_xianyu_product"), dict) else {}
            cr = ev.get("comparison_result") if isinstance(ev.get("comparison_result"), dict) else {}

            eval_price = _positive_float(ev.get("activity_price"))
            price_diff_from_eval = (
                round(live_price - eval_price, 2)
                if live_price is not None and eval_price is not None
                else None
            )

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

            filtered.append({
                "source_origin": "xianyu_eval",
                "platform": plat,
                "product_id": pid,
                "goods_sign": str((prod or {}).get("goods_sign") or cand.get("goods_sign") or "").strip(),
                "source_type": str(ev.get("source_type") or "official"),
                "category_name": str(ev.get("category_name") or "").strip(),
                "name": str((prod or {}).get("name") or ev.get("name") or "未命名商品").strip(),
                "image_url": official_imgs[0] if official_imgs else "",
                "eval_price": eval_price,
                "live_price": live_price,
                "price_diff_from_eval": price_diff_from_eval,
                "live_status": live_status,
                "is_deal_tip": is_active_deal,
                "deal_highlight": str(di.get("card_highlight") or cand.get("deal_highlight_text") or "").strip(),
                "official_image_count": len(official_imgs),
                "deal_image_count": len(deal_imgs),
                "cost_performance_ratio": ev.get("cost_performance_ratio"),
                "matched_unit": cr.get("matched_unit"),
                "final_winner": ev.get("final_winner"),
                "llm_reason": str(cr.get("llm_reason") or "").strip(),
                "evaluated_at": str(ev.get("evaluated_at") or ""),
                "listing_status": listing.get("status") if listing else None,
                "listing_id": listing.get("listing_id") if listing else None,
                "xianyu_ref": {
                    "item_id": xy_id,
                    "item_url": str(bxy.get("item_url") or (f"https://www.goofish.com/item?id={xy_id}" if xy_id else "")),
                    "title": str(bxy.get("title") or "").strip(),
                    "comparable_price": _positive_float(bxy.get("comparable_price")),
                    "spec_description": str(bxy.get("spec_description") or "").strip(),
                    "want_cnt": bxy.get("want_cnt"),
                    "is_free_shipping": bxy.get("is_free_shipping"),
                    "screenshot_url": shot_url,
                    "fallback_images": fallback_xy_imgs[:5],
                },
            })

        total = len(filtered)
        start = (safe_page - 1) * safe_size
        page_items = filtered[start : start + safe_size]
        return {
            "items": page_items,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "has_more": (start + safe_size) < total,
        }

    def reject_candidate(
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
                {"status": "rejected", "updated_at": now},
            )
            return updated or existing

        prod = self.repo.get_product_one(plat, pid)
        title = str((prod or {}).get("name") or "").strip()[:30]
        doc = self.repo.upsert_listing(
            owner,
            plat,
            pid,
            set_fields={
                "status": "rejected",
                "updated_at": now,
            },
            set_on_insert={
                "listing_id": f"xyl_{uuid.uuid4().hex[:8]}",
                "owner": owner,
                "platform": plat,
                "product_id": pid,
                "goods_sign": str((prod or {}).get("goods_sign") or "").strip(),
                "source_origin": source_origin or "xianyu_eval",
                "title": title,
                "description": "",
                "images": [],
                "sell_price": 0.0,
                "shipping_fee": 0.0,
                "cost_price": 0.0,
                "max_cost_price": 0.0,
                "eval_ref": None,
                "external_item_id": "",
                "offline_reason": "",
                "created_at": now,
                "confirmed_at": None,
            },
        )
        return doc or {}

    # =========================================================================
    # 模块二：预览草稿初始化、图片增删、AI 话术生成与标准格式定稿
    # =========================================================================
    def _build_eval_ref(self, eval_doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(eval_doc, dict):
            return None
        bxy = eval_doc.get("best_xianyu_product") if isinstance(eval_doc.get("best_xianyu_product"), dict) else {}
        cr = eval_doc.get("comparison_result") if isinstance(eval_doc.get("comparison_result"), dict) else {}
        xy_id = str(bxy.get("item_id") or "").strip()
        return {
            "xianyu_item_id": xy_id,
            "xianyu_item_url": str(bxy.get("item_url") or (f"https://www.goofish.com/item?id={xy_id}" if xy_id else "")),
            "xianyu_title": str(bxy.get("title") or "").strip(),
            "xianyu_price": _positive_float(bxy.get("comparable_price")),
            "xianyu_spec": str(bxy.get("spec_description") or "").strip(),
            "want_cnt": bxy.get("want_cnt"),
            "cp_ratio": eval_doc.get("cost_performance_ratio"),
            "matched_unit": cr.get("matched_unit"),
            "llm_reason": str(cr.get("llm_reason") or "").strip(),
            "screenshot_file": str(bxy.get("screenshot_file") or "").strip(),
            "fallback_images": [
                u for u in (_normalize_image_url(x) for x in (bxy.get("images") or [])) if u
            ][:5],
        }

    def _hydrate_listing(
        self,
        listing: Dict[str, Any],
        product_doc: Optional[Dict[str, Any]],
        *,
        xy_item_doc: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        out = dict(listing)
        live_status, live_price, is_active_deal, di = _resolve_live_product_state(product_doc)
        official_imgs, deal_imgs = _collect_product_media(product_doc)

        cost_price = _nonneg_float(out.get("cost_price"), 0.0)
        max_cost_price = _nonneg_float(out.get("max_cost_price"), 0.0)
        risk_alert = ""
        if live_status == "off_promotion":
            risk_alert = "off_promotion"
        elif live_price is not None and max_cost_price > 0 and live_price > max_cost_price:
            risk_alert = "price_exceeded"

        eval_ref = out.get("eval_ref")
        if isinstance(eval_ref, dict):
            eval_ref = dict(eval_ref)
            shot_file = str(eval_ref.get("screenshot_file") or "").strip()
            if not shot_file and isinstance(xy_item_doc, dict):
                shot_file = str(xy_item_doc.get("screenshot_file") or "").strip()
            eval_ref["screenshot_url"] = self._screenshot_url_if_exists(shot_file)
            if not eval_ref.get("fallback_images") and isinstance(xy_item_doc, dict):
                det_imgs = (xy_item_doc.get("detail") or {}).get("images") if isinstance(xy_item_doc.get("detail"), dict) else []
                eval_ref["fallback_images"] = [
                    u for u in (_normalize_image_url(x) for x in (det_imgs or [])) if u
                ][:5]
            out["eval_ref"] = eval_ref

        out["live_status"] = live_status
        out["live_price"] = live_price
        out["risk_alert"] = risk_alert
        out["is_deal_tip"] = is_active_deal
        out["deal_highlight"] = str(di.get("card_highlight") or "").strip()
        out["deal_price_logic"] = str(di.get("price_logic") or "").strip()
        out["product_name"] = str((product_doc or {}).get("name") or out.get("title") or "").strip()
        out["available_official_images"] = official_imgs
        out["available_deal_images"] = deal_imgs
        out["created_at"] = _iso_utc(out.get("created_at"))
        out["updated_at"] = _iso_utc(out.get("updated_at"))
        out["confirmed_at"] = _iso_utc(out.get("confirmed_at")) if out.get("confirmed_at") else ""
        return out

    def create_or_open_draft(
        self,
        owner: str,
        *,
        platform: str,
        product_id: str,
        source_origin: str = "favorite",
    ) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        pid = clean_id(product_id)
        if not plat or not pid:
            raise HTTPException(status_code=400, detail="请提供有效的 platform 和 product_id")

        now = datetime.now(timezone.utc)
        prod = self.repo.get_product_one(plat, pid)
        existing = self.repo.get_listing_by_product(owner, plat, pid)
        if existing is not None:
            if existing.get("status") == "rejected":
                existing = self.repo.update_listing_by_id(
                    owner,
                    existing["listing_id"],
                    {"status": "draft", "updated_at": now},
                ) or existing
            xy_id = str(((existing.get("eval_ref") or {}).get("xianyu_item_id")) or "").strip()
            xy_map = self.repo.batch_get_xianyu_items([xy_id]) if xy_id else {}
            return self._hydrate_listing(existing, prod, xy_item_doc=xy_map.get(xy_id))

        user = self.repo.get_user(owner) or {}
        bound_uid = str(user.get("bound_shop_uid") or "").strip()
        fav = self.repo.find_favorite_one(bound_uid, plat, pid) if bound_uid else None
        ev = self.repo.find_best_evaluation_for_product(plat, pid)
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
        cost_price = (
            live_price
            or _positive_float((fav or {}).get("collected_price"))
            or _positive_float((ev or {}).get("activity_price"))
            or 0.0
        )
        max_cost_price = round(cost_price * 1.05, 2) if cost_price > 0 else 0.0

        raw_title = (
            str(di.get("clean_title") or "").strip()
            or str((prod or {}).get("name") or "").strip()
            or str(snap.get("name") or "").strip()
            or str((ev or {}).get("name") or "").strip()
        )
        eval_ref = self._build_eval_ref(ev)
        listing_id = f"xyl_{uuid.uuid4().hex[:8]}"

        created = self.repo.upsert_listing(
            owner,
            plat,
            pid,
            set_fields={"updated_at": now},
            set_on_insert={
                "listing_id": listing_id,
                "owner": owner,
                "platform": plat,
                "product_id": pid,
                "goods_sign": str((prod or {}).get("goods_sign") or snap.get("goods_sign") or cand.get("goods_sign") or "").strip(),
                "source_origin": source_origin or ("xianyu_eval" if ev else "favorite"),
                "status": "draft",
                "title": raw_title[:30],
                "description": "",
                "images": default_images,
                "sell_price": cost_price,
                "shipping_fee": 0.0,
                "cost_price": cost_price,
                "max_cost_price": max_cost_price,
                "eval_ref": eval_ref,
                "external_item_id": "",
                "offline_reason": "",
                "created_at": now,
                "confirmed_at": None,
            },
        )
        if not created:
            raise HTTPException(status_code=500, detail="创建上架草稿失败")
        xy_id = str((eval_ref or {}).get("xianyu_item_id") or "").strip()
        xy_map = self.repo.batch_get_xianyu_items([xy_id]) if xy_id else {}
        return self._hydrate_listing(created, prod, xy_item_doc=xy_map.get(xy_id))

    def get_listing_detail(self, owner: str, listing_id: str) -> Dict[str, Any]:
        listing = self.repo.get_listing_by_id(owner, listing_id)
        if not listing:
            raise HTTPException(status_code=404, detail="上架单不存在或无权查看")
        prod = self.repo.get_product_one(listing["platform"], listing["product_id"])
        xy_id = str(((listing.get("eval_ref") or {}).get("xianyu_item_id")) or "").strip()
        xy_map = self.repo.batch_get_xianyu_items([xy_id]) if xy_id else {}
        return self._hydrate_listing(listing, prod, xy_item_doc=xy_map.get(xy_id))

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

    def update_listing(self, owner: str, listing_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.repo.get_listing_by_id(owner, listing_id)
        if not existing:
            raise HTTPException(status_code=404, detail="上架单不存在或无权编辑")

        set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

        if "title" in updates and updates["title"] is not None:
            set_fields["title"] = str(updates["title"]).strip()[:60]
        if "description" in updates and updates["description"] is not None:
            set_fields["description"] = str(updates["description"]).strip()[:2000]
        if "images" in updates and updates["images"] is not None:
            set_fields["images"] = self._sanitize_images(updates["images"])
        if "sell_price" in updates and updates["sell_price"] is not None:
            sp = _nonneg_float(updates["sell_price"], -1.0)
            if sp < 0:
                raise HTTPException(status_code=400, detail="售价不能为负数")
            set_fields["sell_price"] = sp
        if "shipping_fee" in updates and updates["shipping_fee"] is not None:
            sf = _nonneg_float(updates["shipping_fee"], -1.0)
            if sf < 0:
                raise HTTPException(status_code=400, detail="运费不能为负数")
            set_fields["shipping_fee"] = sf
        if "max_cost_price" in updates and updates["max_cost_price"] is not None:
            mc = _nonneg_float(updates["max_cost_price"], -1.0)
            if mc < 0:
                raise HTTPException(status_code=400, detail="止损成本上限不能为负数")
            set_fields["max_cost_price"] = mc
        if "external_item_id" in updates and updates["external_item_id"] is not None:
            set_fields["external_item_id"] = str(updates["external_item_id"]).strip()[:64]
        if "offline_reason" in updates and updates["offline_reason"] is not None:
            set_fields["offline_reason"] = str(updates["offline_reason"]).strip()[:100]

        target_status = str(updates.get("status") or existing.get("status") or "draft").strip().lower()
        if target_status not in ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail=f"不支持的状态: {target_status}")
        if "status" in updates and updates["status"] is not None:
            set_fields["status"] = target_status

        merged = {**existing, **set_fields}
        if target_status == "ready":
            if not str(merged.get("title") or "").strip():
                raise HTTPException(status_code=400, detail="确认就绪前请先填写闲鱼标题")
            if not str(merged.get("description") or "").strip():
                raise HTTPException(status_code=400, detail="确认就绪前请先生成或填写商品描述")
            if _nonneg_float(merged.get("sell_price"), 0.0) <= 0:
                raise HTTPException(status_code=400, detail="确认就绪前售价必须大于 0")
            if not (isinstance(merged.get("images"), list) and len(merged["images"]) >= 1):
                raise HTTPException(status_code=400, detail="确认就绪前请至少保留 1 张商品图片")
            set_fields["confirmed_at"] = datetime.now(timezone.utc)

        updated = self.repo.update_listing_by_id(owner, listing_id, set_fields)
        if not updated:
            raise HTTPException(status_code=404, detail="上架单不存在")
        prod = self.repo.get_product_one(updated["platform"], updated["product_id"])
        xy_id = str(((updated.get("eval_ref") or {}).get("xianyu_item_id")) or "").strip()
        xy_map = self.repo.batch_get_xianyu_items([xy_id]) if xy_id else {}
        return self._hydrate_listing(updated, prod, xy_item_doc=xy_map.get(xy_id))

    def generate_copy(self, owner: str, listing_id: str, *, style: str = "casual") -> Dict[str, Any]:
        listing = self.repo.get_listing_by_id(owner, listing_id)
        if not listing:
            raise HTTPException(status_code=404, detail="上架单不存在或无权操作")
        eff_style = style if style in ALLOWED_COPY_STYLES else "casual"
        prod = self.repo.get_product_one(listing["platform"], listing["product_id"]) or {}
        di = prod.get("deal_info") if isinstance(prod.get("deal_info"), dict) else {}
        eval_ref = listing.get("eval_ref") if isinstance(listing.get("eval_ref"), dict) else {}

        prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
        context_payload = {
            "style": eff_style,
            "product_name": str(prod.get("name") or listing.get("title") or "").strip(),
            "category": str(prod.get("category") or "").strip(),
            "brand": str(prod.get("brand") or "").strip(),
            "specs": prod.get("specs") or [],
            "sell_price": _nonneg_float(listing.get("sell_price"), 0.0),
            "shipping_fee": _nonneg_float(listing.get("shipping_fee"), 0.0),
            "deal_highlight": str(di.get("card_highlight") or "").strip(),
            "deal_price_logic": str(di.get("price_logic") or "").strip(),
            "xianyu_ref_spec": str(eval_ref.get("xianyu_spec") or "").strip(),
        }
        full_prompt = (
            f"{prompt_template}\n\n"
            f"<product_input>\n{json.dumps(context_payload, ensure_ascii=False, indent=2)}\n</product_input>"
        )

        res = self._ai_generator(full_prompt)
        raw_content = str((res or {}).get("content") or "").strip()
        status_str = str((res or {}).get("status") or "")
        model_used = str(((res or {}).get("metrics") or {}).get("model_used") or "")
        if ("失败" in status_str and not raw_content) or not raw_content:
            raise HTTPException(status_code=502, detail=f"AI 话术生成失败: {status_str or '空响应'}")

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

        if not title:
            title = str(listing.get("title") or context_payload["product_name"])[:30]
        if not description:
            description = raw_content
        return {
            "title": title[:30],
            "description": description[:2000],
            "style": eff_style,
            "model_used": model_used,
        }

    # =========================================================================
    # 模块三：标准上架库查询 & 资产上传/读取
    # =========================================================================
    def list_listings(
        self,
        owner: str,
        *,
        status: str = "ready",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        eff_status = str(status or "ready").strip().lower()
        if eff_status != "all" and eff_status not in ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail=f"不支持的筛选状态: {eff_status}")
        rows, total = self.repo.list_listings(owner, status=eff_status, page=page, page_size=page_size)
        keys = [(r.get("platform"), r.get("product_id")) for r in rows]
        prod_map = self.repo.batch_get_products(keys)
        xy_ids = [
            str(((r.get("eval_ref") or {}).get("xianyu_item_id")) or "").strip()
            for r in rows
        ]
        xy_map = self.repo.batch_get_xianyu_items(xy_ids)

        items = [
            self._hydrate_listing(
                r,
                prod_map.get((normalize_platform(r.get("platform")), clean_id(r.get("product_id")))),
                xy_item_doc=xy_map.get(str(((r.get("eval_ref") or {}).get("xianyu_item_id")) or "").strip()),
            )
            for r in rows
        ]
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 20)))
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "has_more": (safe_page * safe_size) < total,
            "status_counts": self.repo.get_status_counts(owner),
        }

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
