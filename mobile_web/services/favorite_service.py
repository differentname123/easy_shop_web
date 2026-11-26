# -*- coding: utf-8 -*-
"""单清单商品收藏夹业务编排：添加、移除、状态反查与批量实时联查。"""
from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from models.product import clean_product_id, normalize_platform, resolve_item_identity
from repositories.favorite_repo import FavoriteRepository, MAX_FAVORITES_PER_USER
from repositories.product_repo import ProductRepository


def _positive_price(raw_val: Any) -> Optional[float]:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    try:
        val = float(raw_val)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(val, 2) if math.isfinite(val) and val > 0 else None


def _iso_utc(val: Any) -> str:
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(val or "")


class FavoriteService:
    def __init__(
        self,
        favorite_repo: Optional[FavoriteRepository] = None,
        product_repo: Optional[ProductRepository] = None,
    ) -> None:
        self.repo = favorite_repo if favorite_repo is not None else FavoriteRepository()
        self.product_repo = product_repo if product_repo is not None else ProductRepository()

    @staticmethod
    def _resolve_key(platform: str, product_id: str, goods_sign: str = "") -> Tuple[str, str, str]:
        plat = normalize_platform(platform or "")
        raw_pid = str(product_id or "").strip()
        raw_sign = str(goods_sign or "").strip()
        if not plat or (not raw_pid and not raw_sign):
            raise HTTPException(status_code=400, detail="请提供有效的平台和商品标识")
        norm_plat, gid, gsign = resolve_item_identity(
            {
                "platform": plat,
                "product_id": raw_pid,
                "goods_id": raw_pid if raw_pid != raw_sign else "",
                "goods_sign": raw_sign,
            },
            default_platform=plat,
        )
        canonical_pid = gid or raw_pid or gsign
        return norm_plat, canonical_pid, gsign

    def _find_live_product(self, platform: str, product_id: str, goods_sign: str = "") -> Optional[Dict[str, Any]]:
        if self.product_repo is None:
            return None
        col = getattr(self.product_repo, "col", None)
        if col is None:
            return None
        or_clauses = [{"product_id": product_id}]
        if goods_sign and goods_sign != product_id:
            or_clauses.append({"goods_sign": goods_sign})
        query = {"platform": platform, "$or": or_clauses} if len(or_clauses) > 1 else {"platform": platform, **or_clauses[0]}
        doc = col.find_one(query)
        return doc if isinstance(doc, dict) else None

    def add_favorite(
        self,
        uid: str,
        platform: str,
        product_id: str,
        *,
        goods_sign: str = "",
        name: str = "",
        image_url: str = "",
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        plat, pid, gsign = self._resolve_key(platform, product_id, goods_sign)
        live_doc = self._find_live_product(plat, pid, gsign)
        if live_doc is not None:
            live_pid = clean_product_id(live_doc.get("product_id")) or str(live_doc.get("product_id") or "").strip()
            if live_pid:
                pid = live_pid
            gsign = str(live_doc.get("goods_sign") or gsign or "").strip()

        if self.repo.is_favorited(uid, plat, pid):
            return {
                "status": "success",
                "data": {"is_favorited": True, "already_exists": True, "platform": plat, "product_id": pid},
            }

        if self.repo.count_by_uid(uid) >= MAX_FAVORITES_PER_USER:
            raise HTTPException(status_code=400, detail=f"收藏夹已达上限（最多 {MAX_FAVORITES_PER_USER} 件）")

        collected_price: Optional[float] = None
        snap_name = str(name or "").strip()
        snap_img = str(image_url or "").strip()
        if live_doc is not None:
            deal_info = live_doc.get("deal_info") if isinstance(live_doc.get("deal_info"), dict) else {}
            if deal_info.get("is_deal_tip") is True:
                collected_price = _positive_price(deal_info.get("deal_price"))
            if collected_price is None:
                collected_price = _positive_price(live_doc.get("activity_price")) or _positive_price(live_doc.get("price"))
            snap_name = str(live_doc.get("name") or snap_name).strip()
            snap_img = str(live_doc.get("image_url") or snap_img).strip()
        if collected_price is None:
            collected_price = _positive_price(price)

        ok, already = self.repo.add_favorite(
            uid=uid,
            platform=plat,
            product_id=pid,
            collected_price=collected_price,
            snapshot={"name": snap_name, "image_url": snap_img, "goods_sign": gsign},
        )
        if not ok:
            raise HTTPException(status_code=400, detail="收藏商品失败，请检查参数")
        return {
            "status": "success",
            "data": {"is_favorited": True, "already_exists": already, "platform": plat, "product_id": pid},
        }

    def remove_favorite(self, uid: str, platform: str, product_id: str, goods_sign: str = "") -> Dict[str, Any]:
        plat, pid, gsign = self._resolve_key(platform, product_id, goods_sign)
        removed = self.repo.remove_favorite(uid, plat, pid)
        if not removed and gsign and gsign != pid:
            removed = self.repo.remove_favorite(uid, plat, gsign)
        return {"status": "success", "data": {"is_favorited": False, "removed": removed}}

    def _batch_fetch_products(self, fav_docs: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if not fav_docs or self.product_repo is None:
            return {}
        col = getattr(self.product_repo, "col", None)
        if col is None:
            return {}
        by_plat: Dict[str, List[str]] = {}
        for doc in fav_docs:
            plat = normalize_platform(doc.get("platform") or "")
            pid = str(doc.get("product_id") or "").strip()
            if plat and pid:
                by_plat.setdefault(plat, []).append(pid)
        if not by_plat:
            return {}
        or_filters = [
            {"platform": plat, "product_id": {"$in": list(dict.fromkeys(pids))}}
            for plat, pids in by_plat.items()
        ]
        query = or_filters[0] if len(or_filters) == 1 else {"$or": or_filters}
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for p_doc in col.find(query):
            if not isinstance(p_doc, dict):
                continue
            p_plat = normalize_platform(p_doc.get("platform") or "")
            p_pid = str(p_doc.get("product_id") or "").strip()
            if p_plat and p_pid:
                out[(p_plat, p_pid)] = p_doc
        return out

    def list_favorites(self, uid: str, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(500, int(page_size or 20)))
        fav_docs, total = self.repo.list_by_uid(uid, page=safe_page, page_size=safe_size)
        live_map = self._batch_fetch_products(fav_docs)

        items: List[Dict[str, Any]] = []
        for fav in fav_docs:
            plat = normalize_platform(fav.get("platform") or "")
            pid = str(fav.get("product_id") or "").strip()
            collected_price = _positive_price(fav.get("collected_price"))
            snap = fav.get("snapshot") if isinstance(fav.get("snapshot"), dict) else {}
            live_doc = live_map.get((plat, pid))

            current_price: Optional[float] = None
            is_off_promotion = False
            if live_doc is not None:
                summary = self.product_repo._normalize_doc_summary(live_doc)
                deal_info = live_doc.get("deal_info") if isinstance(live_doc.get("deal_info"), dict) else {}
                active_deal_price = _positive_price(deal_info.get("deal_price")) if deal_info.get("is_deal_tip") is True else None
                summary["deal_price"] = active_deal_price
                if live_doc.get("is_off_promotion") is True:
                    live_status = "off_promotion"
                    is_off_promotion = True
                else:
                    live_status = "online"
                    current_price = active_deal_price if active_deal_price is not None else _positive_price(summary.get("price"))
            else:
                live_status = "missing"
                summary = {
                    "platform": plat,
                    "platform_name": {"pdd": "拼多多", "taobao": "淘宝", "jd": "京东"}.get(plat, plat),
                    "product_id": pid,
                    "goods_id": pid if not (plat == "pdd" and pid == snap.get("goods_sign")) else "",
                    "goods_sign": str(snap.get("goods_sign") or (pid if plat != "pdd" else "")),
                    "name": str(snap.get("name") or "商品暂不可用"),
                    "image_url": str(snap.get("image_url") or ""),
                    "price": collected_price or 0.0,
                    "is_deal_tip": False,
                    "deal_price": None,
                }

            price_diff = (
                round(current_price - collected_price, 2)
                if current_price is not None and collected_price is not None
                else None
            )
            items.append({
                "platform": plat,
                "product_id": pid,
                "collected_price": collected_price,
                "current_price": current_price,
                "price_diff": price_diff,
                "live_status": live_status,
                "is_off_promotion": is_off_promotion,
                "created_at": _iso_utc(fav.get("created_at")),
                "product": summary,
            })

        return {
            "status": "success",
            "data": {
                "items": items,
                "total": total,
                "page": safe_page,
                "page_size": safe_size,
                "has_more": (safe_page * safe_size) < total,
            },
        }
