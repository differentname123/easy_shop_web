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


def clean_http_url(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        s = "https:" + s
    return s if s.startswith(("http://", "https://")) else ""


class WorkbenchRepository:
    # 工作台自有 3 张核心表
    USERS_COL = "xy_users"
    LISTINGS_COL = "xy_listings"
    CHANNELS_COL = "xy_channels"

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
    def channels_col(self):
        return self.db.get_collection(self.CHANNELS_COL)

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
            self.channels_col.create_index([("owner", 1), ("channel", 1)], unique=True)
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
        # MongoDB 严禁同一字段同时出现在 $set 与 $setOnInsert 中（code=40 path conflict）
        clean_set = dict(set_fields or {})
        clean_on_insert = {k: v for k, v in (set_on_insert or {}).items() if k not in clean_set}
        update_doc: Dict[str, Any] = {"$set": clean_set}
        if clean_on_insert:
            update_doc["$setOnInsert"] = clean_on_insert
        self.listings_col.update_one(flt, update_doc, upsert=True)
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

    def finish_listing_copy_generation(
        self,
        owner: str,
        listing_id: str,
        *,
        token: str,
        set_fields: Dict[str, Any],
    ) -> bool:
        """按生成令牌原子回写异步文案结果，若已被人工覆盖或移出则安全放弃。"""
        self.ensure_indexes()
        clean_tok = str(token or "").strip()
        if not clean_tok:
            return False
        flt = {
            "owner": str(owner).strip(),
            "listing_id": str(listing_id or "").strip(),
            "copy_gen_token": clean_tok,
        }
        res = self.listings_col.update_one(flt, {"$set": set_fields})
        return int(getattr(res, "matched_count", 0) or 0) > 0

    def record_listing_publish_failure(
        self,
        owner: str,
        listing_id: str,
        failure_info: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return self.update_listing_by_id(
            owner,
            listing_id,
            {
                "publish_failure": dict(failure_info or {}),
                "updated_at": now,
            },
        )

    def clear_listing_publish_failure(
        self,
        owner: str,
        listing_id: str,
    ) -> Optional[Dict[str, Any]]:
        return self.update_listing_by_id(
            owner,
            listing_id,
            {
                "publish_failure": None,
                "updated_at": datetime.now(timezone.utc),
            },
        )

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
    # 可插拔销售渠道统一文档 (xy_channels: 账号绑定 + 在售快照 + 发布追踪)
    # =========================================================================
    def get_channel_doc(self, owner: str, channel: str) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.channels_col.find_one(
            {"owner": str(owner).strip(), "channel": str(channel).strip().lower()},
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def list_channel_docs(self, owner: str) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        cursor = self.channels_col.find({"owner": str(owner).strip()}, {"_id": 0})
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def _upsert_channel(
        self,
        owner: str,
        channel: str,
        set_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.ensure_indexes()
        flt = {"owner": str(owner).strip(), "channel": str(channel).strip().lower()}
        now = datetime.now(timezone.utc)
        full_set = {**set_fields, "updated_at": set_fields.get("updated_at") or now}
        defaults = {
            "owner": flt["owner"],
            "channel": flt["channel"],
            "bound": False,
            "online": False,
            "account_id": "",
            "nickname": "",
            "error_code": "",
            "message": "",
            "last_checked_at": None,
            "online_items": [],
            "abnormal_items": [],
            "group_counts": {},
            "manual_links": {},
            "publications": {},
            "synced_at": None,
            "created_at": now,
        }
        set_on_insert = {k: v for k, v in defaults.items() if k not in full_set}
        self.channels_col.update_one(
            flt,
            {"$set": full_set, "$setOnInsert": set_on_insert},
            upsert=True,
        )
        return self.get_channel_doc(flt["owner"], flt["channel"]) or {}

    def get_channel_account(self, owner: str, channel: str) -> Optional[Dict[str, Any]]:
        return self.get_channel_doc(owner, channel)

    def list_channel_accounts(self, owner: str) -> List[Dict[str, Any]]:
        return self.list_channel_docs(owner)

    def upsert_channel_account(
        self,
        owner: str,
        channel: str,
        set_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._upsert_channel(owner, channel, set_fields)

    def get_channel_selling(self, owner: str, channel: str) -> Optional[Dict[str, Any]]:
        return self.get_channel_doc(owner, channel)

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
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        existing = self.get_channel_doc(clean_owner, clean_ch) or {}
        online_id_set = {
            str(x.get("item_id") or "").strip() for x in online_items if isinstance(x, dict)
        }
        publications = dict(existing.get("publications") or {})
        now_iso = synced_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        for lid, pub in list(publications.items()):
            if isinstance(pub, dict) and pub.get("status") == "online":
                if str(pub.get("external_item_id") or "").strip() not in online_id_set:
                    updated_pub = dict(pub)
                    updated_pub["status"] = "downshelf"
                    updated_pub["downshelved_at"] = now_iso
                    publications[lid] = updated_pub

        return self._upsert_channel(
            clean_owner,
            clean_ch,
            {
                "online_items": list(online_items),
                "abnormal_items": list(abnormal_items),
                "group_counts": dict(group_counts),
                "publications": publications,
                "synced_at": synced_at,
                "updated_at": datetime.now(timezone.utc),
            },
        )

    def update_channel_manual_link(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        listing_id: str,
    ) -> Dict[str, Any]:
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        existing = self.get_channel_doc(clean_owner, clean_ch) or {}
        links = dict(existing.get("manual_links") or {})
        ext_id = str(external_item_id or "").strip()
        clean_lid = str(listing_id or "").strip()
        if clean_lid:
            links[ext_id] = clean_lid
        else:
            links.pop(ext_id, None)
        return self._upsert_channel(clean_owner, clean_ch, {"manual_links": links})

    def record_channel_publication(
        self,
        owner: str,
        channel: str,
        *,
        listing_id: str,
        external_item_id: str,
        item_url: str,
        title: str,
        price: Any,
        pic_url: str = "",
    ) -> Dict[str, Any]:
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        existing = self.get_channel_doc(clean_owner, clean_ch) or {}
        links = dict(existing.get("manual_links") or {})
        publications = dict(existing.get("publications") or {})
        online_items = [
            dict(x)
            for x in (existing.get("online_items") or [])
            if isinstance(x, dict) and str(x.get("item_id") or "").strip() != external_item_id
        ]

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat().replace("+00:00", "Z")
        links[external_item_id] = listing_id
        publications[listing_id] = {
            "listing_id": listing_id,
            "external_item_id": external_item_id,
            "item_url": item_url,
            "title": title,
            "price": str(price),
            "status": "online",
            "published_at": now_iso,
        }
        online_items.insert(
            0,
            {
                "item_id": external_item_id,
                "title": title,
                "price": str(price),
                "post_info": "",
                "item_status": 0,
                "is_online": True,
                "status_label": "在售(正常)",
                "want_info": "",
                "pic_url": pic_url,
                "item_url": item_url,
            },
        )
        return self._upsert_channel(
            clean_owner,
            clean_ch,
            {
                "manual_links": links,
                "publications": publications,
                "online_items": online_items,
                "updated_at": now,
            },
        )

    def mark_channel_downshelf(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
    ) -> Dict[str, Any]:
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        ext_id = str(external_item_id or "").strip()
        existing = self.get_channel_doc(clean_owner, clean_ch) or {}
        links = dict(existing.get("manual_links") or {})
        links.pop(ext_id, None)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat().replace("+00:00", "Z")
        publications = dict(existing.get("publications") or {})
        for lid, pub in list(publications.items()):
            if isinstance(pub, dict) and str(pub.get("external_item_id") or "").strip() == ext_id:
                updated_pub = dict(pub)
                updated_pub["status"] = "downshelf"
                updated_pub["downshelved_at"] = now_iso
                publications[lid] = updated_pub

        online_items = [
            dict(x)
            for x in (existing.get("online_items") or [])
            if isinstance(x, dict) and str(x.get("item_id") or "").strip() != ext_id
        ]
        abnormal_items = [
            dict(x)
            for x in (existing.get("abnormal_items") or [])
            if isinstance(x, dict) and str(x.get("item_id") or "").strip() != ext_id
        ]
        return self._upsert_channel(
            clean_owner,
            clean_ch,
            {
                "manual_links": links,
                "publications": publications,
                "online_items": online_items,
                "abnormal_items": abnormal_items,
                "updated_at": now,
            },
        )

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

    def batch_get_best_evaluations(
        self, keys: Sequence[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
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
        cursor = (
            self.evals_col.find(
                query,
                {
                    "_id": 0,
                    "platform": 1,
                    "product_id": 1,
                    "is_my_product_better": 1,
                    "cost_performance_ratio": 1,
                    "evaluated_at": 1,
                    "best_xianyu_product": 1,
                    "candidate_product": 1,
                },
            )
            .sort([("is_my_product_better", -1), ("cost_performance_ratio", -1), ("evaluated_at", -1)])
        )
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for doc in cursor:
            if isinstance(doc, dict):
                k = (normalize_platform(doc.get("platform")), clean_id(doc.get("product_id")))
                out.setdefault(k, dict(doc))
        return out

    def batch_get_xianyu_items(self, item_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        clean_ids = list(dict.fromkeys(str(i or "").strip() for i in item_ids if str(i or "").strip()))
        if not clean_ids:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for doc in self.xianyu_items_col.find({"item_id": {"$in": clean_ids}}, {"_id": 0}):
            if isinstance(doc, dict) and doc.get("item_id"):
                out[str(doc["item_id"]).strip()] = dict(doc)
        return out
