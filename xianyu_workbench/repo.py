# -*- coding: utf-8 -*-
"""工作台数据仓储：通用选品库、多渠道账户与同步快照、及上游只读数据源。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
import threading
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


def _parse_utc_dt(value: Any) -> Optional[datetime]:
    """将 datetime 或 ISO 时间字符串规范化为 UTC datetime，无效时返回 None。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


class WorkbenchRepository:
    # 工作台自有 5 张核心表
    USERS_COL = "xy_users"
    LISTINGS_COL = "xy_listings"
    CHANNELS_COL = "xy_channels"
    CHANNEL_ITEMS_COL = "xy_channel_items"
    JOBS_COL = "xy_channel_jobs"

    FAVORITES_COL = "favorites"
    PRODUCTS_COL = "products"
    EVALS_COL = "xianyu_deal_evaluations"
    XIANYU_ITEMS_COL = "xianyu_items"
    SHOP_USERS_COL = "users"

    _LEGACY_CHANNEL_BLOB_KEYS: Tuple[str, ...] = (
        "online_items",
        "abnormal_items",
        "manual_links",
        "publications",
    )

    def __init__(self, db: Any) -> None:
        self.db = db
        self._indexes_ready = False
        self._conv_lock = threading.RLock()

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
    def channel_items_col(self):
        return self.db.get_collection(self.CHANNEL_ITEMS_COL)

    @property
    def jobs_col(self):
        return self.db.get_collection(self.JOBS_COL)

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
            self.channel_items_col.create_index(
                [("owner", 1), ("channel", 1), ("external_item_id", 1)],
                unique=True,
            )
            self.channel_items_col.create_index(
                [("owner", 1), ("channel", 1), ("status", 1), ("updated_at", -1)],
            )
            self.channel_items_col.create_index(
                [("owner", 1), ("channel", 1), ("listing_id", 1)],
            )
            self.jobs_col.create_index([("job_id", 1)], unique=True)
            self.jobs_col.create_index([("owner", 1), ("channel", 1), ("created_at", -1)])
            self._indexes_ready = True
            self.migrate_legacy_channel_docs()
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

    def batch_get_listings_by_ids(
        self,
        owner: str,
        listing_ids: Sequence[str],
    ) -> Dict[str, Dict[str, Any]]:
        """按 listing_id 批量读取选品文档（不限状态，包含 dismissed，用于下架溯源）。"""
        self.ensure_indexes()
        clean_owner = str(owner or "").strip()
        clean_ids = list(dict.fromkeys(str(x or "").strip() for x in listing_ids if str(x or "").strip()))
        if not clean_owner or not clean_ids:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for doc in self.listings_col.find(
            {"owner": clean_owner, "listing_id": {"$in": clean_ids}},
            {"_id": 0},
        ):
            if isinstance(doc, dict) and doc.get("listing_id"):
                out[str(doc["listing_id"]).strip()] = dict(doc)
        return out

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
    # 可插拔销售渠道账号状态 (xy_channels: 仅保留账号绑定与同步状态)
    # =========================================================================
    def _migrate_channel_doc_if_legacy(self, doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """将旧版 xy_channels 中的内嵌商品数组/字典平滑迁移至 xy_channel_items 并清理冗余字段。"""
        if not isinstance(doc, dict):
            return None
        if not any(k in doc for k in self._LEGACY_CHANNEL_BLOB_KEYS):
            return dict(doc)

        clean_owner = str(doc.get("owner") or "").strip()
        clean_ch = str(doc.get("channel") or "").strip().lower()
        if not clean_owner or not clean_ch:
            return dict(doc)

        manual_links = doc.get("manual_links") if isinstance(doc.get("manual_links"), dict) else {}
        publications = doc.get("publications") if isinstance(doc.get("publications"), dict) else {}
        pub_by_ext: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        for lid, pub in publications.items():
            if not isinstance(pub, dict):
                continue
            ext = str(pub.get("external_item_id") or "").strip()
            if ext:
                pub_by_ext[ext] = (str(lid or "").strip(), pub)

        synced_dt = _parse_utc_dt(doc.get("synced_at"))
        handled_ext_ids = set()

        for target_status, is_online, entries in (
            ("online", True, doc.get("online_items") or []),
            ("abnormal", False, doc.get("abnormal_items") or []),
        ):
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                ext_id = str(entry.get("item_id") or entry.get("external_item_id") or "").strip()
                if not ext_id or ext_id in handled_ext_ids:
                    continue
                handled_ext_ids.add(ext_id)
                pub_lid, pub_rec = pub_by_ext.get(ext_id, ("", {}))
                manual_lid = str(manual_links.get(ext_id) or "").strip()
                resolved_lid = manual_lid or pub_lid
                link_source = "published" if (pub_lid and resolved_lid == pub_lid) else ("manual" if resolved_lid else "")
                pub_at = _parse_utc_dt(pub_rec.get("published_at"))
                if self.get_channel_item(clean_owner, clean_ch, ext_id, ensure_idx=False) is None:
                    self._upsert_channel_item(
                        clean_owner,
                        clean_ch,
                        ext_id,
                        set_fields={
                            "listing_id": resolved_lid,
                            "link_source": link_source,
                            "status": target_status,
                            "is_online": is_online,
                            "item_status": entry.get("item_status"),
                            "status_label": str(entry.get("status_label") or ""),
                            "title": str(entry.get("title") or ""),
                            "price": str(entry.get("price") or ""),
                            "post_info": str(entry.get("post_info") or ""),
                            "category_id": entry.get("category_id"),
                            "want_info": str(entry.get("want_info") or ""),
                            "pic_url": str(entry.get("pic_url") or ""),
                            "item_url": str(entry.get("item_url") or ""),
                            "published_at": pub_at,
                            "downshelved_at": None,
                            "last_synced_at": synced_dt,
                            "sync_order": idx + 1,
                        },
                        ensure_idx=False,
                    )

        for lid, pub in publications.items():
            if not isinstance(pub, dict):
                continue
            ext_id = str(pub.get("external_item_id") or "").strip()
            if not ext_id or ext_id in handled_ext_ids:
                continue
            handled_ext_ids.add(ext_id)
            clean_lid = str(lid or pub.get("listing_id") or "").strip()
            pub_at = _parse_utc_dt(pub.get("published_at"))
            dw_at = _parse_utc_dt(pub.get("downshelved_at"))
            if self.get_channel_item(clean_owner, clean_ch, ext_id, ensure_idx=False) is None:
                self._upsert_channel_item(
                    clean_owner,
                    clean_ch,
                    ext_id,
                    set_fields={
                        "listing_id": clean_lid,
                        "link_source": "published" if clean_lid else "",
                        "status": "downshelf",
                        "is_online": False,
                        "item_status": None,
                        "status_label": "已下架",
                        "title": str(pub.get("title") or ""),
                        "price": str(pub.get("price") or ""),
                        "item_url": str(pub.get("item_url") or ""),
                        "published_at": pub_at,
                        "downshelved_at": dw_at,
                        "last_synced_at": synced_dt,
                        "sync_order": 0,
                    },
                    ensure_idx=False,
                )

        for ext_id, lid in manual_links.items():
            clean_ext = str(ext_id or "").strip()
            clean_lid = str(lid or "").strip()
            if not clean_ext or not clean_lid or clean_ext in handled_ext_ids:
                continue
            handled_ext_ids.add(clean_ext)
            if self.get_channel_item(clean_owner, clean_ch, clean_ext, ensure_idx=False) is None:
                self._upsert_channel_item(
                    clean_owner,
                    clean_ch,
                    clean_ext,
                    set_fields={
                        "listing_id": clean_lid,
                        "link_source": "manual",
                        "status": "pending",
                        "is_online": False,
                    },
                    ensure_idx=False,
                )

        self.channels_col.update_one(
            {"owner": clean_owner, "channel": clean_ch},
            {"$unset": {k: "" for k in self._LEGACY_CHANNEL_BLOB_KEYS}},
        )
        cleaned = dict(doc)
        for k in self._LEGACY_CHANNEL_BLOB_KEYS:
            cleaned.pop(k, None)
        return cleaned

    def migrate_legacy_channel_docs(self) -> int:
        """扫描并幂等迁移全部遗留 xy_channels 文档。"""
        migrated = 0
        for doc in list(self.channels_col.find({}, {"_id": 0})):
            if isinstance(doc, dict) and any(k in doc for k in self._LEGACY_CHANNEL_BLOB_KEYS):
                self._migrate_channel_doc_if_legacy(doc)
                migrated += 1
        return migrated

    def get_channel_account(self, owner: str, channel: str) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.channels_col.find_one(
            {"owner": str(owner).strip(), "channel": str(channel).strip().lower()},
            {"_id": 0},
        )
        return self._migrate_channel_doc_if_legacy(dict(doc) if isinstance(doc, dict) else None)

    def list_channel_accounts(self, owner: str) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        cursor = self.channels_col.find({"owner": str(owner).strip()}, {"_id": 0})
        out: List[Dict[str, Any]] = []
        for doc in list(cursor):
            cleaned = self._migrate_channel_doc_if_legacy(dict(doc) if isinstance(doc, dict) else None)
            if cleaned is not None:
                out.append(cleaned)
        return out

    def upsert_channel_account(
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
            "im_state": "stopped",
            "im_message": "",
            "im_error_code": "",
            "im_last_sync_at": None,
            "group_counts": {},
            "synced_at": None,
            "created_at": now,
        }
        set_on_insert = {k: v for k, v in defaults.items() if k not in full_set}
        self.channels_col.update_one(
            flt,
            {"$set": full_set, "$setOnInsert": set_on_insert},
            upsert=True,
        )
        return self.get_channel_account(flt["owner"], flt["channel"]) or {}

    # =========================================================================
    # 渠道商品实体仓储 (xy_channel_items: 每件渠道商品独立文档，下架改状态不删记录)
    # =========================================================================
    def _upsert_channel_item(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        *,
        set_fields: Dict[str, Any],
        set_on_insert: Optional[Dict[str, Any]] = None,
        ensure_idx: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if ensure_idx:
            self.ensure_indexes()
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        ext_id = str(external_item_id or "").strip()
        if not clean_owner or not clean_ch or not ext_id:
            return None
        now = datetime.now(timezone.utc)
        flt = {
            "owner": clean_owner,
            "channel": clean_ch,
            "external_item_id": ext_id,
        }
        clean_set = {**set_fields, "updated_at": set_fields.get("updated_at") or now}
        defaults: Dict[str, Any] = {
            "owner": clean_owner,
            "channel": clean_ch,
            "external_item_id": ext_id,
            "listing_id": "",
            "link_source": "",
            "status": "pending",
            "is_online": False,
            "item_status": None,
            "status_label": "",
            "title": "",
            "price": "",
            "post_info": "",
            "category_id": None,
            "want_info": "",
            "pic_url": "",
            "item_url": "",
            "published_at": None,
            "downshelved_at": None,
            "last_synced_at": None,
            "sync_order": 0,
            "conversations": [],
            "conversation_count": 0,
            "unread_message_count": 0,
            "last_message_at": None,
            "created_at": now,
        }
        if set_on_insert:
            defaults.update(set_on_insert)
        clean_on_insert = {k: v for k, v in defaults.items() if k not in clean_set}
        update_doc: Dict[str, Any] = {"$set": clean_set}
        if clean_on_insert:
            update_doc["$setOnInsert"] = clean_on_insert
        self.channel_items_col.update_one(flt, update_doc, upsert=True)
        return self.get_channel_item(clean_owner, clean_ch, ext_id, ensure_idx=False)

    def get_channel_item(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        *,
        ensure_idx: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if ensure_idx:
            self.ensure_indexes()
        doc = self.channel_items_col.find_one(
            {
                "owner": str(owner or "").strip(),
                "channel": str(channel or "").strip().lower(),
                "external_item_id": str(external_item_id or "").strip(),
            },
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def list_channel_items(
        self,
        owner: str,
        channel: str,
        *,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        query: Dict[str, Any] = {
            "owner": str(owner or "").strip(),
            "channel": str(channel or "").strip().lower(),
        }
        eff = str(status or "").strip().lower()
        if eff and eff != "all":
            query["status"] = eff
        else:
            query["status"] = {"$in": ["online", "abnormal", "downshelf"]}

        sort_spec = (
            [("downshelved_at", -1), ("updated_at", -1)]
            if eff == "downshelf"
            else [("is_online", -1), ("sync_order", 1), ("updated_at", -1)]
        )
        cursor = self.channel_items_col.find(query, {"_id": 0}).sort(sort_spec)
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def list_channel_items_paged(
        self,
        owner: str,
        channel: str,
        *,
        status: str = "all",
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        self.ensure_indexes()
        query: Dict[str, Any] = {
            "owner": str(owner or "").strip(),
            "channel": str(channel or "").strip().lower(),
        }
        eff = str(status or "all").strip().lower()
        if eff and eff != "all":
            query["status"] = eff
        else:
            query["status"] = {"$in": ["online", "abnormal", "downshelf"]}

        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(100, int(page_size or 50)))
        total = int(self.channel_items_col.count_documents(query))
        if total == 0:
            return [], 0
        skip = (safe_page - 1) * safe_size
        if skip >= total:
            return [], total
        sort_spec = (
            [("downshelved_at", -1), ("updated_at", -1)]
            if eff == "downshelf"
            else [("is_online", -1), ("sync_order", 1), ("updated_at", -1)]
        )
        cursor = (
            self.channel_items_col.find(query, {"_id": 0})
            .sort(sort_spec)
            .skip(skip)
            .limit(safe_size)
        )
        return [dict(d) for d in cursor if isinstance(d, dict)], total

    def get_channel_item_counts(self, owner: str, channel: str) -> Dict[str, int]:
        self.ensure_indexes()
        counts = {"online": 0, "abnormal": 0, "downshelf": 0}
        cursor = self.channel_items_col.find(
            {
                "owner": str(owner or "").strip(),
                "channel": str(channel or "").strip().lower(),
                "status": {"$in": ["online", "abnormal", "downshelf"]},
            },
            {"_id": 0, "status": 1},
        )
        for doc in cursor:
            if not isinstance(doc, dict):
                continue
            st = str(doc.get("status") or "").strip().lower()
            if st in counts:
                counts[st] += 1
        counts["total"] = counts["online"] + counts["abnormal"] + counts["downshelf"]
        return counts

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
        now = datetime.now(timezone.utc)
        account = self.upsert_channel_account(
            clean_owner,
            clean_ch,
            {
                "group_counts": dict(group_counts),
                "synced_at": synced_at,
                "updated_at": now,
            },
        )

        seen_ids: List[str] = []
        seen_set = set()
        for target_status, is_online, entries in (
            ("online", True, online_items or []),
            ("abnormal", False, abnormal_items or []),
        ):
            for idx, raw_entry in enumerate(entries):
                if not isinstance(raw_entry, dict):
                    continue
                ext_id = str(raw_entry.get("item_id") or raw_entry.get("external_item_id") or "").strip()
                if not ext_id or ext_id in seen_set:
                    continue
                seen_set.add(ext_id)
                seen_ids.append(ext_id)
                set_fields: Dict[str, Any] = {
                    "status": target_status,
                    "is_online": is_online,
                    "item_status": raw_entry.get("item_status"),
                    "status_label": str(
                        raw_entry.get("status_label") or ("在售(正常)" if is_online else "异常状态")
                    ),
                    "title": str(raw_entry.get("title") or ""),
                    "price": str(raw_entry.get("price") or ""),
                    "post_info": str(raw_entry.get("post_info") or ""),
                    "category_id": raw_entry.get("category_id"),
                    "want_info": str(raw_entry.get("want_info") or ""),
                    "pic_url": str(raw_entry.get("pic_url") or ""),
                    "item_url": str(raw_entry.get("item_url") or ""),
                    "last_synced_at": synced_at,
                    "sync_order": idx + 1,
                    "updated_at": now,
                }
                if is_online:
                    set_fields["downshelved_at"] = None
                self._upsert_channel_item(
                    clean_owner,
                    clean_ch,
                    ext_id,
                    set_fields=set_fields,
                )

        # 线上已看不到的旧记录仅转标为 downshelf，保留选品关联与未来每商品挂载数据，绝不删记录
        missing_flt: Dict[str, Any] = {
            "owner": clean_owner,
            "channel": clean_ch,
            "status": {"$in": ["online", "abnormal"]},
        }
        if seen_ids:
            missing_flt["external_item_id"] = {"$nin": seen_ids}
        self.channel_items_col.update_many(
            missing_flt,
            {
                "$set": {
                    "status": "downshelf",
                    "is_online": False,
                    "status_label": "已下架",
                    "downshelved_at": synced_at,
                    "last_synced_at": synced_at,
                    "updated_at": now,
                }
            },
        )
        return account

    def update_channel_manual_link(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        listing_id: str,
    ) -> Optional[Dict[str, Any]]:
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        ext_id = str(external_item_id or "").strip()
        clean_lid = str(listing_id or "").strip()
        now = datetime.now(timezone.utc)
        self.upsert_channel_account(clean_owner, clean_ch, {"updated_at": now})
        return self._upsert_channel_item(
            clean_owner,
            clean_ch,
            ext_id,
            set_fields={
                "listing_id": clean_lid,
                "link_source": "manual" if clean_lid else "",
                "updated_at": now,
            },
        )

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
    ) -> Optional[Dict[str, Any]]:
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        clean_lid = str(listing_id or "").strip()
        now = datetime.now(timezone.utc)
        self.upsert_channel_account(clean_owner, clean_ch, {"updated_at": now})
        return self._upsert_channel_item(
            clean_owner,
            clean_ch,
            external_item_id,
            set_fields={
                "listing_id": clean_lid,
                "link_source": "published" if clean_lid else "",
                "status": "online",
                "is_online": True,
                "item_status": 0,
                "status_label": "在售(正常)",
                "title": str(title or ""),
                "price": str(price),
                "post_info": "",
                "want_info": "",
                "pic_url": str(pic_url or ""),
                "item_url": str(item_url or ""),
                "published_at": now,
                "downshelved_at": None,
                "last_synced_at": now,
                "sync_order": 0,
                "updated_at": now,
            },
        )

    def mark_channel_downshelf(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
    ) -> Optional[Dict[str, Any]]:
        clean_owner = str(owner).strip()
        clean_ch = str(channel).strip().lower()
        ext_id = str(external_item_id or "").strip()
        now = datetime.now(timezone.utc)
        self.upsert_channel_account(clean_owner, clean_ch, {"updated_at": now})
        return self._upsert_channel_item(
            clean_owner,
            clean_ch,
            ext_id,
            set_fields={
                "status": "downshelf",
                "is_online": False,
                "status_label": "已下架",
                "downshelved_at": now,
                "updated_at": now,
            },
        )

    def list_all_bound_channel_accounts(self, channel: str) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        clean_ch = str(channel or "").strip().lower()
        cursor = self.channels_col.find(
            {"channel": clean_ch, "bound": True, "online": True},
            {"_id": 0},
        )
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def update_channel_im_state(
        self,
        owner: str,
        channel: str,
        *,
        im_state: str,
        im_message: str = "",
        im_error_code: str = "",
        im_last_sync_at: Any = None,
        account_id: str = "",
        nickname: str = "",
    ) -> Dict[str, Any]:
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        now = datetime.now(timezone.utc)
        set_fields: Dict[str, Any] = {
            "im_state": str(im_state or "stopped").strip(),
            "im_message": str(im_message or "").strip(),
            "im_error_code": str(im_error_code or "").strip(),
            "updated_at": now,
        }
        sync_dt = _parse_utc_dt(im_last_sync_at)
        if sync_dt is not None:
            set_fields["im_last_sync_at"] = sync_dt
        if account_id:
            set_fields["account_id"] = str(account_id).strip()
        if nickname:
            set_fields["nickname"] = str(nickname).strip()
        if im_state in ("offline", "paused_risk"):
            set_fields["online"] = False
            set_fields["error_code"] = str(im_error_code or (
                "RISK_CONTROL_BLOCKED" if im_state == "paused_risk" else "ACCOUNT_OFFLINE"
            ))
            if im_message:
                set_fields["message"] = str(im_message).strip()
            set_fields["last_checked_at"] = now
        elif im_state == "online":
            set_fields["bound"] = True
            set_fields["online"] = True
            set_fields["error_code"] = ""
            set_fields["last_checked_at"] = now
        return self.upsert_channel_account(clean_owner, clean_ch, set_fields)

    @staticmethod
    def _merge_conv_messages(
        existing_msgs: Sequence[Dict[str, Any]],
        incoming_msgs: Sequence[Dict[str, Any]],
        cap: int = 80,
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        by_msg_id: Dict[str, int] = {}
        by_client_uuid: Dict[str, int] = {}

        def _norm_msg(raw: Dict[str, Any]) -> Dict[str, Any]:
            m = deepcopy(raw)
            dt = _parse_utc_dt(m.get("created_at")) or datetime.now(timezone.utc)
            m["created_at"] = dt
            m["created_at_ms"] = int(m.get("created_at_ms") or int(dt.timestamp() * 1000))
            m["message_id"] = str(m.get("message_id") or "").strip()
            m["client_uuid"] = str(m.get("client_uuid") or "").strip()
            m["sender"] = "me" if m.get("sender") == "me" else "peer"
            m["msg_type"] = str(m.get("msg_type") or "text")
            m["text"] = str(m.get("text") or "")
            m["image_url"] = str(m.get("image_url") or "")
            m["delivery_status"] = str(m.get("delivery_status") or "sent")
            m["error"] = str(m.get("error") or "")
            return m

        for raw in list(existing_msgs or []) + list(incoming_msgs or []):
            if not isinstance(raw, dict):
                continue
            item = _norm_msg(raw)
            mid = item["message_id"]
            cuuid = item["client_uuid"]
            hit_idx: Optional[int] = None
            if mid and mid in by_msg_id:
                hit_idx = by_msg_id[mid]
            elif cuuid and cuuid in by_client_uuid:
                hit_idx = by_client_uuid[cuuid]

            if hit_idx is not None:
                prev = merged[hit_idx]
                if prev.get("delivery_status") == "sending" or item.get("delivery_status") == "sent":
                    merged[hit_idx] = {**prev, **item}
            else:
                idx = len(merged)
                merged.append(item)
                if mid:
                    by_msg_id[mid] = idx
                if cuuid:
                    by_client_uuid[cuuid] = idx

        merged.sort(key=lambda x: (int(x.get("created_at_ms") or 0), str(x.get("message_id") or x.get("client_uuid") or "")))
        return merged[-max(1, int(cap)):]

    def _recompute_conv_summary(self, conversations: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
        convs_sorted = sorted(
            conversations,
            key=lambda c: (
                (_parse_utc_dt(c.get("last_message_at")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp()
            ),
            reverse=True,
        )[:50]
        unread_total = sum(int(c.get("unread_count") or 0) for c in convs_sorted)
        last_dt: Optional[datetime] = None
        for c in convs_sorted:
            dt = _parse_utc_dt(c.get("last_message_at"))
            if dt is not None and (last_dt is None or dt > last_dt):
                last_dt = dt
        return {
            "conversations": convs_sorted,
            "conversation_count": len(convs_sorted),
            "unread_message_count": unread_total,
            "last_message_at": last_dt,
            "updated_at": now,
        }

    def upsert_channel_item_conversations(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        *,
        conversations: Sequence[Dict[str, Any]],
        item_meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        ext_id = str(external_item_id or "").strip()
        if not clean_owner or not clean_ch or not ext_id:
            return None

        with self._conv_lock:
            existing_doc = self.get_channel_item(clean_owner, clean_ch, ext_id)
            if existing_doc is None:
                meta = item_meta or {}
                on_insert = {
                    "status": "online",
                    "is_online": True,
                    "status_label": "在售(正常)",
                    "title": str(meta.get("title") or ""),
                    "pic_url": str(meta.get("pic_url") or ""),
                    "item_url": str(meta.get("item_url") or f"https://www.goofish.com/item?id={ext_id}"),
                }
                existing_doc = self._upsert_channel_item(
                    clean_owner,
                    clean_ch,
                    ext_id,
                    set_fields={},
                    set_on_insert=on_insert,
                ) or {}

            now = datetime.now(timezone.utc)
            by_cid: Dict[str, Dict[str, Any]] = {}
            for c in existing_doc.get("conversations") or []:
                if isinstance(c, dict) and c.get("cid"):
                    by_cid[str(c["cid"])] = deepcopy(c)

            for inc in conversations or []:
                if not isinstance(inc, dict) or not inc.get("cid"):
                    continue
                cid = str(inc["cid"]).strip()
                prev = by_cid.get(cid) or {}
                merged_msgs = self._merge_conv_messages(
                    prev.get("messages") or [],
                    inc.get("messages") or [],
                )
                inc_dt = _parse_utc_dt(inc.get("last_message_at"))
                msg_dt = _parse_utc_dt(merged_msgs[-1].get("created_at")) if merged_msgs else None
                prev_dt = _parse_utc_dt(prev.get("last_message_at"))
                cand_dts = [d for d in (inc_dt, msg_dt, prev_dt) if d is not None]
                eff_dt = max(cand_dts) if cand_dts else now
                preview = (
                    str(merged_msgs[-1].get("text") or "")
                    if merged_msgs
                    else str(inc.get("last_message_preview") or prev.get("last_message_preview") or "")
                )
                by_cid[cid] = {
                    "cid": cid,
                    "peer_user_id": str(inc.get("peer_user_id") or prev.get("peer_user_id") or "").strip(),
                    "peer_nick": str(inc.get("peer_nick") or prev.get("peer_nick") or "").strip(),
                    "unread_count": int(inc.get("unread_count", prev.get("unread_count", 0)) or 0),
                    "has_gap": bool(inc.get("has_gap", False)),
                    "last_message_preview": preview[:120],
                    "last_message_at": eff_dt,
                    "messages": merged_msgs,
                    "updated_at": now,
                }

            summary_fields = self._recompute_conv_summary(list(by_cid.values()), now)
            return self._upsert_channel_item(
                clean_owner,
                clean_ch,
                ext_id,
                set_fields=summary_fields,
            )

    def append_channel_item_message(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        cid: str,
        message_doc: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        ext_id = str(external_item_id or "").strip()
        clean_cid = str(cid or "").strip()
        with self._conv_lock:
            existing_doc = self.get_channel_item(clean_owner, clean_ch, ext_id)
            if not existing_doc:
                return None
            convs = [deepcopy(c) for c in (existing_doc.get("conversations") or []) if isinstance(c, dict)]
            target = next((c for c in convs if str(c.get("cid") or "") == clean_cid), None)
            if target is None:
                return None
            now = datetime.now(timezone.utc)
            target["messages"] = self._merge_conv_messages(target.get("messages") or [], [message_doc])
            if message_doc.get("delivery_status") in ("sending", "sent"):
                target["last_message_preview"] = str(message_doc.get("text") or "")[:120]
                target["last_message_at"] = _parse_utc_dt(message_doc.get("created_at")) or now
            target["updated_at"] = now
            summary_fields = self._recompute_conv_summary(convs, now)
            return self._upsert_channel_item(
                clean_owner,
                clean_ch,
                ext_id,
                set_fields=summary_fields,
            )

    def update_channel_item_message_status(
        self,
        owner: str,
        channel: str,
        external_item_id: str,
        cid: str,
        client_uuid: str,
        *,
        delivery_status: str,
        message_id: str = "",
        created_at: Any = None,
        created_at_ms: int = 0,
        error: str = "",
    ) -> Optional[Dict[str, Any]]:
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        ext_id = str(external_item_id or "").strip()
        clean_cid = str(cid or "").strip()
        clean_uuid = str(client_uuid or "").strip()
        with self._conv_lock:
            existing_doc = self.get_channel_item(clean_owner, clean_ch, ext_id)
            if not existing_doc:
                return None
            convs = [deepcopy(c) for c in (existing_doc.get("conversations") or []) if isinstance(c, dict)]
            target = next((c for c in convs if str(c.get("cid") or "") == clean_cid), None)
            if target is None:
                return None
            now = datetime.now(timezone.utc)
            msgs = list(target.get("messages") or [])
            for m in reversed(msgs):
                if isinstance(m, dict) and str(m.get("client_uuid") or "") == clean_uuid:
                    m["delivery_status"] = str(delivery_status)
                    m["error"] = str(error or "")
                    if message_id:
                        m["message_id"] = str(message_id)
                    dt = _parse_utc_dt(created_at)
                    if dt is not None:
                        m["created_at"] = dt
                    if created_at_ms:
                        m["created_at_ms"] = int(created_at_ms)
                    break
            target["messages"] = msgs
            if delivery_status == "sent":
                target["unread_count"] = 0
                if msgs:
                    target["last_message_preview"] = str(msgs[-1].get("text") or "")[:120]
                    target["last_message_at"] = _parse_utc_dt(msgs[-1].get("created_at")) or now
            target["updated_at"] = now
            summary_fields = self._recompute_conv_summary(convs, now)
            return self._upsert_channel_item(
                clean_owner,
                clean_ch,
                ext_id,
                set_fields=summary_fields,
            )

    # =========================================================================
    # 渠道后台异步任务仓储 (xy_channel_jobs)
    # =========================================================================
    def create_channel_job(self, job_doc: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_indexes()
        stored = dict(job_doc)
        self.jobs_col.insert_one(stored)
        stored.pop("_id", None)
        return stored

    def get_channel_job(self, owner: str, channel: str, job_id: str) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        doc = self.jobs_col.find_one(
            {
                "owner": str(owner or "").strip(),
                "channel": str(channel or "").strip().lower(),
                "job_id": str(job_id or "").strip(),
            },
            {"_id": 0},
        )
        return dict(doc) if isinstance(doc, dict) else None

    def update_channel_job(self, job_id: str, set_fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.ensure_indexes()
        clean_id_val = str(job_id or "").strip()
        if not clean_id_val:
            return None
        res = self.jobs_col.update_one({"job_id": clean_id_val}, {"$set": dict(set_fields or {})})
        if int(getattr(res, "matched_count", 0) or 0) == 0:
            return None
        doc = self.jobs_col.find_one({"job_id": clean_id_val}, {"_id": 0})
        return dict(doc) if isinstance(doc, dict) else None

    def list_channel_jobs(self, owner: str, channel: str, limit: int = 15) -> List[Dict[str, Any]]:
        self.ensure_indexes()
        cursor = (
            self.jobs_col.find(
                {
                    "owner": str(owner or "").strip(),
                    "channel": str(channel or "").strip().lower(),
                },
                {"_id": 0},
            )
            .sort([("created_at", -1)])
            .limit(max(1, int(limit)))
        )
        return [dict(d) for d in cursor if isinstance(d, dict)]

    def prune_channel_jobs(self, owner: str, channel: str, keep: int = 20) -> int:
        self.ensure_indexes()
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        cursor = (
            self.jobs_col.find(
                {
                    "owner": clean_owner,
                    "channel": clean_ch,
                    "status": {"$in": ["completed", "partial", "failed", "cancelled"]},
                },
                {"_id": 0, "job_id": 1},
            )
            .sort([("created_at", -1)])
        )
        stale_ids = [
            str(d.get("job_id") or "")
            for idx, d in enumerate(cursor)
            if idx >= max(1, int(keep)) and isinstance(d, dict) and d.get("job_id")
        ]
        if not stale_ids:
            return 0
        res = self.jobs_col.delete_many({"owner": clean_owner, "channel": clean_ch, "job_id": {"$in": stale_ids}})
        return int(getattr(res, "deleted_count", 0) or 0)

    def mark_stale_channel_jobs_failed(self, active_job_ids: Sequence[str] = ()) -> int:
        self.ensure_indexes()
        now = datetime.now(timezone.utc)
        flt: Dict[str, Any] = {"status": {"$in": ["queued", "running"]}}
        keep_ids = [str(x or "").strip() for x in active_job_ids if str(x or "").strip()]
        if keep_ids:
            flt["job_id"] = {"$nin": keep_ids}
        updated = 0
        for doc in list(self.jobs_col.find(flt, {"_id": 0, "job_id": 1})):
            jid = str((doc or {}).get("job_id") or "").strip()
            if not jid:
                continue
            res = self.jobs_col.update_one(
                {"job_id": jid, "status": {"$in": ["queued", "running"]}},
                {
                    "$set": {
                        "status": "failed",
                        "error_code": "SERVER_RESTARTED",
                        "error": "服务已重启，后台任务中断",
                        "aborted_reason": "服务已重启，后台任务中断",
                        "finished_at": now,
                        "updated_at": now,
                    }
                },
            )
            updated += int(getattr(res, "matched_count", 0) or 0)
        return updated

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
