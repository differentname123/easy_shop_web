# -*- coding: utf-8 -*-
"""工作台数据仓储：通用选品库、多渠道账户与同步快照、及上游只读数据源。"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pymongo.errors import DuplicateKeyError


def normalize_platform(platform: Optional[str], default: str = "pdd") -> str:
    code = str(platform or default).strip().lower()
    return "taobao" if code == "tb" else code


def clean_id(value: Any) -> str:
    if isinstance(value, bool) or (isinstance(value, (int, float)) and value == 0):
        return ""
    text = str(value or "").strip()
    return "" if text in ("", "0", "0.0", "None") else text


class WorkbenchRepository:
    USERS_COL = "xy_users"
    LISTINGS_COL = "xy_listings"
    CHANNEL_ACCOUNTS_COL = "xy_channel_accounts"
    CHANNEL_SELLING_COL = "xy_channel_selling"

    FAVORITES_COL = "favorites"
    PRODUCTS_COL = "products"
    EVALS_COL = "xianyu_deal_evaluations"
    XIANYU_ITEMS_COL = "xianyu_items"
    SHOP_USERS_COL = "users"

    def __init__(self, db: Any) -> None:
        self.db = db
        self._indexes_ready = False

    @property
    def users_col(self):
        return self.db.get_collection(self.USERS_COL)

    @property
    def listings_col(self):
        return self.db.get_collection(self.LISTINGS_COL)

    @property
    def channel_accounts_col(self):
        return self.db.get_collection(self.CHANNEL_ACCOUNTS_COL)

    @property
    def channel_selling_col(self):
        return self.db.get_collection(self.CHANNEL_SELLING_COL)

    @property
    def favorites_col(self):
        return self.db.get_collection(self.FAVORITES_COL)

    @property
    def products_col(self):
        return self.db.get_collection(self.PRODUCTS_COL)

    @property
    def evals_col(self):
        return self.db.get_collection(self.EVALS_COL)

    @property
    def xianyu_items_col(self):
        return self.db.get_collection(self.XIANYU_ITEMS_COL)

    @property
    def shop_users_col(self):
        return self.db.get_collection(self.SHOP_USERS_COL)

    def ensure_indexes(self) -> bool:
        if self._indexes_ready or self.db is None:
            return self._indexes_ready
        try:
            self.users_col.create_index([("username", 1)], unique=True)
            self.listings_col.create_index([("listing_id", 1)], unique=True)
            self.listings_col.create_index(
                [("owner", 1), ("platform", 1), ("product_id", 1)],
                unique=True,
            )
            self.listings_col.create_index([("owner", 1), ("status", 1), ("updated_at", -1)])
            self.channel_accounts_col.create_index([("owner", 1), ("channel", 1)], unique=True)
            self.channel_selling_col.create_index([("owner", 1), ("channel", 1)], unique=True)
            self._indexes_ready = True
            return True
        except Exception:
            return False

    # =========================================================================
    # 工作台账号 (xy_users)
    # =========================================================================
    def create_user(
        self,
        username: str,
        password_hash: str,
        bound_shop_uid: str = "",
    ) -> Dict[str, Any]:
        self.ensure_indexes()
        clean_user = str(username or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", clean_user):
            raise ValueError("用户名需为 3~32 位字母、数字或 _.-")
        now = datetime.now(timezone.utc)
        doc = {
            "username": clean_user,
            "password_hash": str(password_hash),
            "bound_shop_uid": str(bound_shop_uid or "").strip(),
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.users_col.insert_one(doc)
        except DuplicateKeyError as exc:
            raise ValueError(f"账号 {clean_user} 已存在") from exc
        return {k: v for k, v in doc.items() if k != "_id"}

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        clean_user = str(username or "").strip()
        if not clean_user:
            return None
        doc = self.users_col.find_one({"username": clean_user}, {"_id": 0})
        return dict(doc) if isinstance(doc, dict) else None

    def list_users(self) -> List[Dict[str, Any]]:
        cursor = self.users_col.find({}, {"_id": 0, "password_hash": 0}).sort([("created_at", 1)])
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def update_password(self, username: str, password_hash: str) -> bool:
        res = self.users_col.update_one(
            {"username": str(username or "").strip()},
            {"$set": {"password_hash": password_hash, "updated_at": datetime.now(timezone.utc)}},
        )
        return int(getattr(res, "matched_count", 0) or 0) > 0

    def update_bound_shop_uid(self, username: str, bound_shop_uid: str) -> bool:
        res = self.users_col.update_one(
            {"username": str(username or "").strip()},
            {"$set": {"bound_shop_uid": str(bound_shop_uid or "").strip(), "updated_at": datetime.now(timezone.utc)}},
        )
        return int(getattr(res, "matched_count", 0) or 0) > 0

    def find_shop_uid_by_phone(self, phone: str) -> Optional[str]:
        clean_phone = str(phone or "").strip()
        if not clean_phone:
            return None
        doc = self.shop_users_col.find_one({"phone": clean_phone}, {"_id": 0, "uid": 1})
        if isinstance(doc, dict) and doc.get("uid"):
            return str(doc["uid"]).strip()
        return None

    def shop_uid_exists(self, uid: str) -> bool:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return False
        return self.shop_users_col.find_one({"uid": clean_uid}, {"_id": 1}) is not None

    # =========================================================================
    # 通用商品素材库 (xy_listings，平台无关)
    # =========================================================================
    def get_listing_by_id(self, owner: str, listing_id: str) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.listings_col.find_one(
            {"owner": str(owner).strip(), "listing_id": str(listing_id or "").strip()},
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def get_listing_by_product(
        self,
        owner: str,
        platform: str,
        product_id: str,
    ) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.listings_col.find_one(
            {
                "owner": str(owner).strip(),
                "platform": normalize_platform(platform),
                "product_id": clean_id(product_id),
            },
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def get_listing_map_by_products(
        self,
        owner: str,
        keys: Sequence[Tuple[str, str]],
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        self.ensure_indexes()
        clean_owner = str(owner or "").strip()
        by_plat: Dict[str, List[str]] = {}
        for plat, pid in keys:
            np = normalize_platform(plat)
            cp = clean_id(pid)
            if np and cp:
                by_plat.setdefault(np, []).append(cp)
        if not clean_owner or not by_plat:
            return {}
        or_clauses = [
            {"platform": plat, "product_id": {"$in": list(dict.fromkeys(pids))}}
            for plat, pids in by_plat.items()
        ]
        query: Dict[str, Any] = {"owner": clean_owner}
        if len(or_clauses) == 1:
            query.update(or_clauses[0])
        else:
            query["$or"] = or_clauses
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for doc in self.listings_col.find(query, {"_id": 0}):
            if isinstance(doc, dict):
                k = (normalize_platform(doc.get("platform")), clean_id(doc.get("product_id")))
                out[k] = dict(doc)
        return out

    def upsert_listing(
        self,
        owner: str,
        platform: str,
        product_id: str,
        *,
        set_fields: Dict[str, Any],
        set_on_insert: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        flt = {
            "owner": str(owner).strip(),
            "platform": normalize_platform(platform),
            "product_id": clean_id(product_id),
        }
        self.listings_col.update_one(
            flt,
            {"$set": set_fields, "$setOnInsert": set_on_insert},
            upsert=True,
        )
        return self.get_listing_by_product(flt["owner"], flt["platform"], flt["product_id"])

    def update_listing_by_id(
        self,
        owner: str,
        listing_id: str,
        set_fields: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        flt = {"owner": str(owner).strip(), "listing_id": str(listing_id or "").strip()}
        res = self.listings_col.update_one(flt, {"$set": set_fields})
        if int(getattr(res, "matched_count", 0) or 0) == 0:
            return None
        return self.get_listing_by_id(flt["owner"], flt["listing_id"])

    def list_listings(
        self,
        owner: str,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        self.ensure_indexes()
        query: Dict[str, Any] = {"owner": str(owner or "").strip()}
        eff = str(status or "active").strip().lower()
        if eff == "active":
            query["status"] = {"$in": ["ready", "draft", "online", "offline"]}
        elif eff == "ready":
            query["status"] = {"$in": ["ready", "online", "offline"]}
        elif eff == "dismissed":
            query["status"] = {"$in": ["dismissed", "rejected"]}
        elif eff != "all":
            query["status"] = eff

        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 20)))
        total = int(self.listings_col.count_documents(query))
        if total == 0:
            return [], 0
        skip = (safe_page - 1) * safe_size
        if skip >= total:
            return [], total
        cursor = (
            self.listings_col.find(query, {"_id": 0})
            .sort([("updated_at", -1)])
            .skip(skip)
            .limit(safe_size)
        )
        return [dict(d) for d in cursor if isinstance(d, dict)], total

    def list_all_active_listings(self, owner: str, limit: int = 300) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        cursor = (
            self.listings_col.find(
                {
                    "owner": str(owner or "").strip(),
                    "status": {"$in": ["ready", "draft", "online", "offline"]},
                },
                {"_id": 0},
            )
            .sort([("updated_at", -1)])
            .limit(max(1, int(limit)))
        )
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def get_item_counts(self, owner: str) -> Dict[str, int]:
        self.ensure_indexes()
        clean_owner = str(owner or "").strip()
        counts = {"draft": 0, "ready": 0, "dismissed": 0}
        for doc in self.listings_col.find({"owner": clean_owner}, {"_id": 0, "status": 1}):
            if not isinstance(doc, dict):
                continue
            raw = str(doc.get("status") or "").strip().lower()
            if raw in ("ready", "online", "offline"):
                counts["ready"] += 1
            elif raw in ("dismissed", "rejected"):
                counts["dismissed"] += 1
            elif raw == "draft":
                counts["draft"] += 1
        counts["active"] = counts["draft"] + counts["ready"]
        return counts

    # =========================================================================
    # 可插拔渠道账户与同步在售快照 (xy_channel_accounts / xy_channel_selling)
    # =========================================================================
    def get_channel_account(self, owner: str, channel: str) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.channel_accounts_col.find_one(
            {"owner": str(owner).strip(), "channel": str(channel).strip().lower()},
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def list_channel_accounts(self, owner: str) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        cursor = self.channel_accounts_col.find({"owner": str(owner).strip()}, {"_id": 0})
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def upsert_channel_account(
        self,
        owner: str,
        channel: str,
        set_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.ensure_indexes()
        flt = {"owner": str(owner).strip(), "channel": str(channel).strip().lower()}
        now = datetime.now(timezone.utc)
        self.channel_accounts_col.update_one(
            flt,
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "owner": flt["owner"],
                    "channel": flt["channel"],
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return self.get_channel_account(flt["owner"], flt["channel"]) or {}

    def get_channel_selling(self, owner: str, channel: str) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.channel_selling_col.find_one(
            {"owner": str(owner).strip(), "channel": str(channel).strip().lower()},
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def upsert_channel_selling(
        self,
        owner: str,
        channel: str,
        *,
        online_items: List[Dict[str, Any]],
        abnormal_items: List[Dict[str, Any]],
        group_counts: Dict[str, int],
        synced_at: datetime,
    ) -> Dict[str, Any]:
        self.ensure_indexes()
        flt = {"owner": str(owner).strip(), "channel": str(channel).strip().lower()}
        self.channel_selling_col.update_one(
            flt,
            {
                "$set": {
                    "online_items": list(online_items),
                    "abnormal_items": list(abnormal_items),
                    "group_counts": dict(group_counts),
                    "synced_at": synced_at,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "owner": flt["owner"],
                    "channel": flt["channel"],
                    "manual_links": {},
                },
            },
            upsert=True,
        )
        return self.get_channel_selling(flt["owner"], flt["channel"]) or {}

    def update_channel_manual_link(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        listing_id: str,
    ) -> Dict[str, Any]:
        self.ensure_indexes()
        flt = {"owner": str(owner).strip(), "channel": str(channel).strip().lower()}
        existing = self.get_channel_selling(flt["owner"], flt["channel"]) or {}
        links = dict(existing.get("manual_links") or {})
        ext_id = str(external_item_id or "").strip()
        clean_lid = str(listing_id or "").strip()
        if clean_lid:
            links[ext_id] = clean_lid
        else:
            links.pop(ext_id, None)
        now = datetime.now(timezone.utc)
        self.channel_selling_col.update_one(
            flt,
            {
                "$set": {"manual_links": links, "updated_at": now},
                "$setOnInsert": {
                    "owner": flt["owner"],
                    "channel": flt["channel"],
                    "online_items": [],
                    "abnormal_items": [],
                    "group_counts": {},
                    "synced_at": None,
                },
            },
            upsert=True,
        )
        return self.get_channel_selling(flt["owner"], flt["channel"]) or {}

    # =========================================================================
    # 上游只读数据源 (favorites / products / xianyu_deal_evaluations / xianyu_items)
    # =========================================================================
    def list_favorites_by_uid(self, uid: str) -> List[Dict[str, Any]]:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return []
        cursor = (
            self.favorites_col.find({"uid": clean_uid}, {"_id": 0})
            .sort([("created_at", -1)])
            .limit(500)
        )
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def find_favorite_one(self, uid: str, platform: str, product_id: str) -> Optional[Dict[str, Any]]:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return None
        doc = self.favorites_col.find_one(
            {
                "uid": clean_uid,
                "platform": normalize_platform(platform),
                "product_id": clean_id(product_id),
            },
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def batch_get_products(self, keys: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        by_plat: Dict[str, List[str]] = {}
        for plat, pid in keys:
            np = normalize_platform(plat)
            cp = clean_id(pid)
            if np and cp:
                by_plat.setdefault(np, []).append(cp)
        if not by_plat:
            return {}
        or_clauses = [
            {"platform": plat, "product_id": {"$in": list(dict.fromkeys(pids))}}
            for plat, pids in by_plat.items()
        ]
        query = or_clauses[0] if len(or_clauses) == 1 else {"$or": or_clauses}
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for doc in self.products_col.find(query, {"_id": 0}):
            if isinstance(doc, dict):
                k = (normalize_platform(doc.get("platform")), clean_id(doc.get("product_id")))
                out[k] = dict(doc)
        return out

    def get_product_one(self, platform: str, product_id: str) -> Optional[Dict[str, Any]]:
        doc = self.products_col.find_one(
            {"platform": normalize_platform(platform), "product_id": clean_id(product_id)},
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def list_evaluations(
        self,
        query: Dict[str, Any],
        sort_spec: Sequence[Tuple[str, int]],
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        cursor = self.evals_col.find(query, {"_id": 0}).sort(list(sort_spec)).limit(max(1, int(limit)))
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def find_best_evaluation_for_product(
        self,
        platform: str,
        product_id: str,
    ) -> Optional[Dict[str, Any]]:
        cursor = (
            self.evals_col.find(
                {"platform": normalize_platform(platform), "product_id": clean_id(product_id)},
                {"_id": 0},
            )
            .sort([("is_my_product_better", -1), ("cost_performance_ratio", -1), ("evaluated_at", -1)])
            .limit(1)
        )
        docs = [dict(d) for d in cursor if isinstance(d, dict)]
        return docs[0] if docs else None

    def batch_get_xianyu_items(self, item_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        clean_ids = list(dict.fromkeys(str(i or "").strip() for i in item_ids if str(i or "").strip()))
        if not clean_ids:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for doc in self.xianyu_items_col.find({"item_id": {"$in": clean_ids}}, {"_id": 0}):
            if isinstance(doc, dict) and doc.get("item_id"):
                out[str(doc["item_id"]).strip()] = dict(doc)
        return out
