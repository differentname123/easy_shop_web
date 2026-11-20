# -*- coding: utf-8 -*-
"""拼多多多人团互助仓储层：独立操作 pdd_multi_groups 集合，与主站业务完全解耦。"""
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from repositories.base_repo import BaseRepository
from models.group_help import (
    DEFAULT_GROUP_TTL_SECONDS,
    ENTRY_TYPE_POSTER,
    ENTRY_TYPE_TOKEN,
    EXPORT_STATUS_FAILED,
    EXPORT_STATUS_SUCCESS,
    EXPORT_TARGET_MIAOZHULI,
    GROUP_STATUS_ACTIVE,
    GROUP_STATUS_EXPIRED,
    GROUP_STATUS_FULL,
    GROUP_STATUS_PENDING_CONFIRM,
    MAX_PARSE_ATTEMPTS,
    OFFLINE_REASON_UPSTREAM_MISSING,
    PARSE_STATUS_FAILED,
    PARSE_STATUS_PARSED,
    PARSE_STATUS_PENDING,
    SOURCE_USER_TEXT,
    SOURCE_USER_UPLOAD,
    is_highly_similar_goods,
    validate_and_normalize_parsed_poster,
    extract_pdd_share_token,
)


class GroupHelpRepository(BaseRepository):
    COLLECTION_NAME = "pdd_multi_groups"

    def _init_indexes(self) -> bool:
        col = self._db.get_collection(self.COLLECTION_NAME)
        col.create_index([("group_order_id", ASCENDING)], unique=True, name="uk_group_order_id")
        col.create_index(
            [
                ("parse_status", ASCENDING),
                ("group_status", ASCENDING),
                ("click_count", ASCENDING),
                ("remain_people", ASCENDING),
                ("created_at", DESCENDING),
            ],
            name="idx_status_click_remain_created",
        )
        col.create_index([("goods_name", ASCENDING)], name="idx_goods_name")
        col.create_index([("keywords", ASCENDING)], name="idx_keywords")
        col.create_index([("share_code", ASCENDING)], name="idx_share_code")
        col.create_index([("last_exported_at", ASCENDING)], name="idx_last_exported")
        col.create_index(
            [("group_status", ASCENDING), ("expires_at", ASCENDING)],
            name="idx_status_expires",
        )
        col.create_index(
            [
                ("source", ASCENDING),
                ("parse_status", ASCENDING),
                ("group_status", ASCENDING),
                ("created_at", DESCENDING),
            ],
            name="idx_source_status_created",
        )
        return True

    def get_by_group_order_id(self, group_order_id: str) -> Optional[Dict[str, Any]]:
        gid = str(group_order_id or "").strip()
        if not gid:
            return None
        return self.col.find_one({"group_order_id": gid}, {"_id": 0})

    def create_or_refresh_initial_group(self, data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """写入初步核验通过的多人团记录。返回 (文档字典, 是否为新插入或重新激活)。"""
        now = float(data.get("created_at") or time.time())
        gid = str(data["group_order_id"]).strip()
        existing = self.get_by_group_order_id(gid)

        if existing is not None:
            orig_created = float(existing.get("created_at") or now)
            orig_expires = float(existing.get("expires_at") or (orig_created + DEFAULT_GROUP_TTL_SECONDS))
            if orig_expires <= now:
                raise ValueError(
                    "该拼团已超过有效期失效（拼多多拼团仅24小时有效），请在拼多多重新发起新团后再上传最新海报"
                )

            set_fields: Dict[str, Any] = {
                "short_url": data.get("short_url", existing.get("short_url", "")),
                "redirect_url": data.get("redirect_url", existing.get("redirect_url", "")),
                "refer_share_id": data.get("refer_share_id") or existing.get("refer_share_id", ""),
                "poster_path": data.get("poster_path", existing.get("poster_path", "")),
                "entry_type": str(data.get("entry_type") or existing.get("entry_type") or ENTRY_TYPE_POSTER),
                "source": str(data.get("source") or existing.get("source") or SOURCE_USER_UPLOAD),
                "parse_status": PARSE_STATUS_PENDING,
                "parse_attempts": 0,
                "parse_error": "",
                "group_status": GROUP_STATUS_ACTIVE,
                "user_takedown": False,
                "offline_reason": "",
                "updated_at": now,
            }
            if str(data.get("share_text") or "").strip():
                set_fields["share_text"] = str(data.get("share_text") or "").strip()
                set_fields["share_code"] = str(data.get("share_code") or "")

            # 每次上传新图片均重置为 pending 重新执行大模型合规与关键信息审核，严禁跳过审核直接复用旧状态
            updated = self.col.find_one_and_update(
                {"group_order_id": gid},
                {"$set": set_fields},
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
            )
            return updated or existing, True

        doc = {
            "group_order_id": gid,
            "entry_type": str(data.get("entry_type") or ENTRY_TYPE_POSTER),
            "source": str(data.get("source") or SOURCE_USER_UPLOAD),
            "short_url": str(data.get("short_url") or ""),
            "redirect_url": str(data.get("redirect_url") or ""),
            "refer_share_uin": str(data.get("refer_share_uin") or ""),
            "refer_share_channel": str(data.get("refer_share_channel") or ""),
            "refer_share_id": str(data.get("refer_share_id") or ""),
            "app_wake_url": str(data.get("app_wake_url") or ""),
            "h5_join_url": str(data.get("h5_join_url") or ""),
            "brand_card_url": str(data.get("brand_card_url") or ""),
            "poster_path": str(data.get("poster_path") or ""),
            "share_text": str(data.get("share_text") or ""),
            "share_code": str(data.get("share_code") or ""),
            "publisher_nick": str(data.get("publisher_nick") or ""),
            "owner_token": str(data.get("owner_token") or ""),
            "owner_uid": str(data.get("owner_uid") or ""),
            "last_takedown_uid": "",
            "user_takedown": False,
            "offline_reason": "",
            "parse_status": PARSE_STATUS_PENDING,
            "parse_attempts": 0,
            "parse_error": "",
            "goods_name": "",
            "keywords": [],
            "group_price": None,
            "original_price": None,
            "total_people": 0,
            "remain_people": 1,
            "group_status": GROUP_STATUS_ACTIVE,
            "click_count": 0,
            "feedback_count": 0,
            "exported_targets": dict(data.get("exported_targets") or {}),
            "last_exported_at": data.get("last_exported_at"),
            "created_at": now,
            "updated_at": now,
            "expires_at": now + DEFAULT_GROUP_TTL_SECONDS,
        }
        self.col.insert_one(dict(doc))
        return doc, True

    def fetch_pending_for_parse(self, limit: int = 2, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """拉取等待大模型解析的记录队列。"""
        current_ts = float(now if now is not None else time.time())
        cursor = (
            self.col.find(
                {
                    "parse_status": PARSE_STATUS_PENDING,
                    "parse_attempts": {"$lt": MAX_PARSE_ATTEMPTS},
                    "expires_at": {"$gt": current_ts},
                },
                {"_id": 0},
            )
            .sort("created_at", ASCENDING)
            .limit(max(1, int(limit)))
        )
        return list(cursor)

    def find_matching_active_group(
        self,
        goods_name: str,
        group_price: Optional[float],
        exclude_group_order_id: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """查找大厅中正在凑人的同款同价商品（价格完全一致且商品名称高度相似）。"""
        if not goods_name or group_price is None or group_price <= 0:
            return None
        current_ts = float(now if now is not None else time.time())
        exclude_gid = str(exclude_group_order_id or "").strip()

        cursor = (
            self.col.find(
                {
                    "parse_status": PARSE_STATUS_PARSED,
                    "group_status": GROUP_STATUS_ACTIVE,
                    "expires_at": {"$gt": current_ts},
                    "remain_people": {"$gt": 0},
                },
                {"_id": 0},
            )
            .sort(
                [
                    ("remain_people", ASCENDING),
                    ("click_count", ASCENDING),
                    ("created_at", DESCENDING),
                ]
            )
            .limit(100)
        )
        target_price = float(group_price)
        for cand in cursor:
            cand_gid = str(cand.get("group_order_id") or "").strip()
            if not cand_gid or cand_gid == exclude_gid:
                continue
            cand_price = cand.get("group_price")
            if not isinstance(cand_price, (int, float)) or isinstance(cand_price, bool):
                continue
            if abs(float(cand_price) - target_price) >= 0.01:
                continue
            if is_highly_similar_goods(goods_name, str(cand.get("goods_name") or "")):
                return cand
        return None

    def mark_parse_succeeded(
        self,
        group_order_id: str,
        parsed_data: Dict[str, Any],
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """校验大模型识别结果并将商品信息、还差人数、海报剩余时间回写至数据库。若已有同款同价在拼团则标记为待确认。"""
        validated = validate_and_normalize_parsed_poster(parsed_data)
        current_ts = float(now if now is not None else time.time())
        gid = str(group_order_id or "").strip()
        existing = self.get_by_group_order_id(gid)
        if not existing:
            return None

        created_at = float(existing.get("created_at") or current_ts)
        default_expires = created_at + DEFAULT_GROUP_TTL_SECONDS
        ai_rem_sec = validated.get("remaining_seconds")
        if ai_rem_sec is not None:
            expires_at = min(default_expires, created_at + float(ai_rem_sec))
        else:
            expires_at = min(default_expires, float(existing.get("expires_at") or default_expires))

        remain_people = validated["remain_people"]
        total_people = validated["total_people"]

        matched_group_order_id = ""
        if remain_people <= 0:
            group_status = GROUP_STATUS_FULL
        else:
            matched_doc = self.find_matching_active_group(
                goods_name=validated["goods_name"],
                group_price=validated["group_price"],
                exclude_group_order_id=gid,
                now=current_ts,
            )
            if matched_doc is not None:
                group_status = GROUP_STATUS_PENDING_CONFIRM
                matched_group_order_id = str(matched_doc.get("group_order_id") or "")
            else:
                group_status = GROUP_STATUS_ACTIVE

        return self.col.find_one_and_update(
            {"group_order_id": gid},
            {
                "$set": {
                    "parse_status": PARSE_STATUS_PARSED,
                    "parse_error": "",
                    "goods_name": validated["goods_name"],
                    "keywords": validated["keywords"],
                    "group_price": validated["group_price"],
                    "original_price": validated["original_price"],
                    "total_people": total_people,
                    "remain_people": remain_people,
                    "group_status": group_status,
                    "matched_group_order_id": matched_group_order_id,
                    "expires_at": expires_at,
                    "updated_at": current_ts,
                },
                "$inc": {"parse_attempts": 1},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

    def mark_parse_failed(
        self,
        group_order_id: str,
        error_msg: str,
        permanent: bool = False,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        current_ts = float(now if now is not None else time.time())
        gid = str(group_order_id or "").strip()
        existing = self.get_by_group_order_id(gid)
        if not existing:
            return None
        attempts = int(existing.get("parse_attempts") or 0) + 1
        new_status = PARSE_STATUS_FAILED if (permanent or attempts >= MAX_PARSE_ATTEMPTS) else PARSE_STATUS_PENDING
        return self.col.find_one_and_update(
            {"group_order_id": gid},
            {
                "$set": {
                    "parse_status": new_status,
                    "parse_attempts": attempts,
                    "parse_error": str(error_msg or "")[:300],
                    "updated_at": current_ts,
                }
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

    def expire_outdated_groups(self, now: Optional[float] = None) -> int:
        """将超过 24 小时（或超过 expires_at）的活跃团自动下线。"""
        current_ts = float(now if now is not None else time.time())
        res = self.col.update_many(
            {
                "group_status": GROUP_STATUS_ACTIVE,
                "expires_at": {"$lte": current_ts},
            },
            {
                "$set": {
                    "group_status": GROUP_STATUS_EXPIRED,
                    "updated_at": current_ts,
                }
            },
        )
        return int(res.modified_count or 0)

    def search_groups(
        self,
        keyword: str = "",
        filter_mode: str = "active",
        offset: int = 0,
        limit: int = 20,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """搜索并分页返回已完成大模型识别的多人团列表。"""
        current_ts = float(now if now is not None else time.time())
        self.expire_outdated_groups(now=current_ts)

        query: Dict[str, Any] = {"parse_status": PARSE_STATUS_PARSED}
        if filter_mode == "almost":
            query["group_status"] = GROUP_STATUS_ACTIVE
            query["expires_at"] = {"$gt": current_ts}
            query["remain_people"] = 1
        elif filter_mode == "all":
            # 展示最近 24 小时内的所有团（含刚满员/已失效供用户核对）
            query["created_at"] = {"$gt": current_ts - DEFAULT_GROUP_TTL_SECONDS}
        else:
            # 默认只展示正在进行中、未过期且还差人的团
            query["group_status"] = GROUP_STATUS_ACTIVE
            query["expires_at"] = {"$gt": current_ts}
            query["remain_people"] = {"$gt": 0}

        kw_clean = str(keyword or "").strip()
        if kw_clean:
            extracted_token = extract_pdd_share_token(kw_clean)
            if extracted_token:
                # 用户直接粘贴了拼多多口令文案或带口令符号的内容，优先进行口令直搜
                query["$or"] = [
                    {"group_order_id": extracted_token},
                    {"group_order_id": f"tok_{extracted_token}"},
                    {"share_code": extracted_token},
                ]
            else:
                tokens = [t.strip() for t in re.split(r"\s+", kw_clean) if t.strip()][:5]
                and_conditions = []
                for tok in tokens:
                    pattern = re.escape(tok)
                    clean_tok = tok.strip(" \t\r\n⇥⇤")
                    or_branch = [
                        {"goods_name": {"$regex": pattern, "$options": "i"}},
                        {"keywords": {"$regex": pattern, "$options": "i"}},
                        {"group_order_id": tok},
                        {"group_order_id": f"tok_{tok}"},
                        {"share_code": tok},
                    ]
                    if clean_tok and clean_tok != tok:
                        or_branch.extend(
                            [
                                {"group_order_id": clean_tok},
                                {"group_order_id": f"tok_{clean_tok}"},
                                {"share_code": clean_tok},
                            ]
                        )
                    and_conditions.append({"$or": or_branch})
                if and_conditions:
                    query["$and"] = and_conditions

        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(50, int(limit)))

        total = self.col.count_documents(query)
        cursor = (
            self.col.find(query, {"_id": 0})
            .sort(
                [
                    ("group_status", ASCENDING),
                    ("click_count", ASCENDING),
                    ("remain_people", ASCENDING),
                    ("created_at", DESCENDING),
                ]
            )
            .skip(safe_offset)
            .limit(safe_limit)
        )
        items = list(cursor)

        pending_count = self.col.count_documents(
            {
                "parse_status": PARSE_STATUS_PENDING,
                "expires_at": {"$gt": current_ts},
            }
        )
        return {
            "items": items,
            "total": total,
            "has_more": (safe_offset + len(items)) < total,
            "pending_count": pending_count,
        }

    def increment_click(self, group_order_id: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        current_ts = float(now if now is not None else time.time())
        gid = str(group_order_id or "").strip()
        return self.col.find_one_and_update(
            {"group_order_id": gid},
            {"$inc": {"click_count": 1}, "$set": {"updated_at": current_ts}},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

    def apply_user_feedback(
        self,
        group_order_id: str,
        action: str,
        remain_people: Optional[int] = None,
        now: Optional[float] = None,
        operator_uid: str = "",
        is_owner: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """用户共创维护多人团状态：支持标记已满、标记已失效、我已参团(-1)、修改还差几人、确认继续上架自己的团。"""
        current_ts = float(now if now is not None else time.time())
        gid = str(group_order_id or "").strip()
        existing = self.get_by_group_order_id(gid)
        if not existing:
            return None

        act = str(action or "").strip()
        expires_at = float(existing.get("expires_at") or 0)
        is_time_expired = expires_at <= current_ts

        if is_time_expired:
            if existing.get("group_status") != GROUP_STATUS_EXPIRED:
                existing = (
                    self.col.find_one_and_update(
                        {"group_order_id": gid},
                        {"$set": {"group_status": GROUP_STATUS_EXPIRED, "updated_at": current_ts}},
                        projection={"_id": 0},
                        return_document=ReturnDocument.AFTER,
                    )
                    or existing
                )
            if act == "mark_expired":
                return existing
            raise ValueError("该拼团已超过有效期失效（拼多多拼团仅24小时有效），无法重新上架或修改状态")

        prev_rem = int(existing.get("remain_people") if existing.get("remain_people") is not None else 1)
        was_active = existing.get("group_status") == GROUP_STATUS_ACTIVE and prev_rem > 0
        update_fields: Dict[str, Any] = {"updated_at": current_ts}

        clean_op_uid = str(operator_uid or "").strip()

        if act == "joined_one" and prev_rem >= 2:
            updated = self.col.find_one_and_update(
                {"group_order_id": gid, "remain_people": {"$gte": 2}},
                {
                    "$set": {"updated_at": current_ts},
                    "$inc": {"remain_people": -1, "feedback_count": 1},
                },
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                return updated
            existing = self.get_by_group_order_id(gid)
            if not existing:
                return None
            prev_rem = int(existing.get("remain_people") if existing.get("remain_people") is not None else 1)
            if prev_rem < 2 and not is_owner and not clean_op_uid:
                return existing
            was_active = existing.get("group_status") == GROUP_STATUS_ACTIVE and prev_rem > 0

        if act == "mark_full":
            update_fields["group_status"] = GROUP_STATUS_FULL
            update_fields["remain_people"] = 0
            update_fields["matched_group_order_id"] = ""
        elif act == "mark_expired":
            update_fields["group_status"] = GROUP_STATUS_EXPIRED
            update_fields["matched_group_order_id"] = ""
        elif act == "confirm_publish":
            cur_rem = max(1, int(existing.get("remain_people") or 1))
            update_fields["remain_people"] = cur_rem
            update_fields["group_status"] = GROUP_STATUS_ACTIVE
            update_fields["matched_group_order_id"] = ""
        elif act == "joined_one":
            new_rem = max(0, prev_rem - 1)
            update_fields["remain_people"] = new_rem
            if new_rem == 0:
                update_fields["group_status"] = GROUP_STATUS_FULL
        elif act == "set_remain":
            if remain_people is None or isinstance(remain_people, bool):
                raise ValueError("请指定有效的剩余人数")
            val = int(remain_people)
            if val < 0 or val > 20:
                raise ValueError("剩余人数需在 0 到 20 之间")
            update_fields["remain_people"] = val
            update_fields["matched_group_order_id"] = ""
            update_fields["group_status"] = GROUP_STATUS_FULL if val == 0 else GROUP_STATUS_ACTIVE
        else:
            raise ValueError(f"不支持的反馈操作类型: {act}")

        target_status = update_fields.get("group_status", existing.get("group_status"))
        now_offline = target_status in (GROUP_STATUS_FULL, GROUP_STATUS_EXPIRED)

        if was_active and now_offline:
            update_fields["last_takedown_uid"] = "" if is_owner else clean_op_uid
            update_fields["user_takedown"] = True
            update_fields["user_takedown_at"] = current_ts

        if target_status == GROUP_STATUS_ACTIVE and is_owner:
            update_fields["user_takedown"] = False
            update_fields["offline_reason"] = ""

        return self.col.find_one_and_update(
            {"group_order_id": gid},
            {
                "$set": update_fields,
                "$inc": {"feedback_count": 1},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

    def pop_last_takedown_uid(self, group_order_id: str, exclude_uid: str = "") -> str:
        """原子取出并清空最近一次下架该团的非团长用户 UID（单次下架最多追责一次，且不罚团长本人）。"""
        gid = str(group_order_id or "").strip()
        existing = self.get_by_group_order_id(gid)
        if not existing:
            return ""
        by_uid = str(existing.get("last_takedown_uid") or "").strip()
        if not by_uid or (exclude_uid and by_uid == str(exclude_uid).strip()):
            return ""
        updated = self.col.find_one_and_update(
            {"group_order_id": gid, "last_takedown_uid": by_uid},
            {"$set": {"last_takedown_uid": ""}},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        return by_uid if updated else ""

    def upsert_parsed_group(
        self,
        data: Dict[str, Any],
        *,
        ttl_seconds: Optional[int] = None,
        now: Optional[float] = None,
        preserve_local_takedown: bool = True,
        check_similar: bool = True,
    ) -> Tuple[Dict[str, Any], bool, Optional[Dict[str, Any]]]:
        """写入或刷新已结构化校验的无图拼团（口令/链接/外部采集同步）。

        返回 (文档, 是否实际新增或刷新, 匹配到的站内同款已开团文档)。
        """
        current_ts = float(now if now is not None else time.time())
        gid = str(data.get("group_order_id") or "").strip()
        if not gid:
            raise ValueError("group_order_id 不能为空")

        ttl = max(60, min(int(ttl_seconds or DEFAULT_GROUP_TTL_SECONDS), DEFAULT_GROUP_TTL_SECONDS))
        created_at = float(data.get("created_at") or current_ts)
        raw_expires = float(data.get("expires_at") or (created_at + ttl))
        expires_at = min(raw_expires, created_at + ttl)

        existing = self.get_by_group_order_id(gid)
        if existing is not None:
            is_user_takedown = bool(existing.get("user_takedown"))
            orig_status = str(existing.get("group_status") or GROUP_STATUS_ACTIVE)
            orig_created = float(existing.get("created_at") or created_at)
            orig_expires = float(existing.get("expires_at") or (orig_created + DEFAULT_GROUP_TTL_SECONDS))

            # 核心铁律：网页用户反馈下架了的团，就算外部两个网站还在，也绝不要恢复
            if is_user_takedown:
                return existing, False, None

            if orig_expires <= current_ts:
                if preserve_local_takedown:
                    return existing, False, None
                raise ValueError("该拼团已超过有效期失效，请重新发起后再发布")

            incoming_source = str(data.get("source") or SOURCE_USER_TEXT)
            existing_source = str(
                existing.get("source") or (SOURCE_USER_UPLOAD if existing.get("poster_path") else SOURCE_USER_TEXT)
            )
            # 外部采集不得覆盖真实用户上传的海报与归属信息
            if preserve_local_takedown and existing_source == SOURCE_USER_UPLOAD and incoming_source != SOURCE_USER_UPLOAD:
                return existing, False, None

            was_upstream_missing = existing.get("offline_reason") == OFFLINE_REASON_UPSTREAM_MISSING
            should_relist_from_missing = (
                orig_status in (GROUP_STATUS_FULL, GROUP_STATUS_EXPIRED)
                and was_upstream_missing
                and not is_user_takedown
                and orig_expires > current_ts
            )

            if preserve_local_takedown and not should_relist_from_missing and (
                orig_status in (GROUP_STATUS_FULL, GROUP_STATUS_EXPIRED)
            ):
                return existing, False, None

            update_fields: Dict[str, Any] = {
                "goods_name": str(data.get("goods_name") or existing.get("goods_name") or ""),
                "keywords": list(data.get("keywords") or existing.get("keywords") or []),
                "group_price": data.get("group_price", existing.get("group_price")),
                "original_price": data.get("original_price", existing.get("original_price")),
                "total_people": int(data.get("total_people") if data.get("total_people") is not None else (existing.get("total_people") or 0)),
                "remain_people": max(1, int(data.get("remain_people") if data.get("remain_people") is not None else (existing.get("remain_people") or 1))),
                "updated_at": current_ts,
            }
            if should_relist_from_missing:
                update_fields["group_status"] = GROUP_STATUS_ACTIVE
                update_fields["offline_reason"] = ""
            if str(data.get("share_text") or "").strip():
                update_fields["share_text"] = str(data.get("share_text") or "").strip()
            if str(data.get("share_code") or "").strip():
                update_fields["share_code"] = str(data.get("share_code") or "").strip()
            if str(data.get("short_url") or "").strip() and not existing.get("short_url"):
                update_fields["short_url"] = str(data.get("short_url") or "").strip()
            if str(data.get("publisher_nick") or "").strip() and not existing.get("publisher_nick"):
                update_fields["publisher_nick"] = str(data.get("publisher_nick") or "").strip()[:40]

            updated = self.col.find_one_and_update(
                {"group_order_id": gid},
                {"$set": update_fields},
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
            )
            return updated or existing, False, None

        if expires_at <= current_ts:
            raise ValueError("该拼团已超过有效期，未录入")

        matched_doc: Optional[Dict[str, Any]] = None
        if check_similar:
            matched_doc = self.find_matching_active_group(
                goods_name=str(data.get("goods_name") or ""),
                group_price=data.get("group_price"),
                exclude_group_order_id=gid,
                now=current_ts,
            )

        target_status = GROUP_STATUS_PENDING_CONFIRM if matched_doc else GROUP_STATUS_ACTIVE
        matched_gid = str(matched_doc.get("group_order_id") or "") if matched_doc else ""

        doc = {
            "group_order_id": gid,
            "entry_type": str(data.get("entry_type") or ENTRY_TYPE_TOKEN),
            "source": str(data.get("source") or SOURCE_USER_TEXT),
            "short_url": str(data.get("short_url") or ""),
            "redirect_url": str(data.get("redirect_url") or ""),
            "refer_share_uin": str(data.get("refer_share_uin") or ""),
            "refer_share_channel": str(data.get("refer_share_channel") or ""),
            "refer_share_id": str(data.get("refer_share_id") or ""),
            "app_wake_url": str(data.get("app_wake_url") or ""),
            "h5_join_url": str(data.get("h5_join_url") or ""),
            "brand_card_url": str(data.get("brand_card_url") or ""),
            "poster_path": str(data.get("poster_path") or ""),
            "share_text": str(data.get("share_text") or ""),
            "share_code": str(data.get("share_code") or ""),
            "publisher_nick": str(data.get("publisher_nick") or "")[:40],
            "owner_token": str(data.get("owner_token") or ""),
            "owner_uid": str(data.get("owner_uid") or ""),
            "last_takedown_uid": "",
            "user_takedown": False,
            "offline_reason": "",
            "parse_status": PARSE_STATUS_PARSED,
            "parse_attempts": 1,
            "parse_error": "",
            "matched_group_order_id": matched_gid,
            "goods_name": str(data.get("goods_name") or ""),
            "keywords": list(data.get("keywords") or []),
            "group_price": data.get("group_price"),
            "original_price": data.get("original_price"),
            "total_people": int(data.get("total_people") or 0),
            "remain_people": max(1, int(data.get("remain_people") or 1)),
            "group_status": target_status,
            "click_count": 0,
            "feedback_count": 0,
            "exported_targets": dict(data.get("exported_targets") or existing.get("exported_targets") if existing else {}),
            "last_exported_at": data.get("last_exported_at", existing.get("last_exported_at") if existing else None),
            "created_at": created_at,
            "updated_at": current_ts,
            "expires_at": expires_at,
        }
        self.col.insert_one(dict(doc))
        return doc, True, matched_doc

    def sync_external_status(
        self,
        group_order_id: str,
        *,
        source: str,
        new_status: str,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """同步外部来源已满员/失效状态，仅作用于相同外部 source 的在架记录。"""
        gid = str(group_order_id or "").strip()
        if not gid:
            return None
        existing = self.get_by_group_order_id(gid)
        if not existing or str(existing.get("source") or "") != str(source or ""):
            return None
        if str(existing.get("group_status") or "") != GROUP_STATUS_ACTIVE:
            return existing
        current_ts = float(now if now is not None else time.time())
        update_fields: Dict[str, Any] = {"updated_at": current_ts}
        if new_status == GROUP_STATUS_FULL:
            update_fields["group_status"] = GROUP_STATUS_FULL
            update_fields["remain_people"] = 0
        elif new_status == GROUP_STATUS_EXPIRED:
            update_fields["group_status"] = GROUP_STATUS_EXPIRED
        else:
            return existing
        return self.col.find_one_and_update(
            {"group_order_id": gid, "source": str(source)},
            {"$set": update_fields},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

    def mark_missing_external_groups_offline(
        self,
        active_external_keys: Set[str],
        *,
        now: Optional[float] = None,
        sources: Tuple[str, ...] = ("acdog", "miaozhuli"),
    ) -> int:
        """两站皆无时自动下架：如果外部采集的活跃团在所有外部源中均已消失，则自动下架（从大厅隐藏）。

        仅作用于来源为外部采集（sources）且当前处于活跃（active）的团。
        下架时标记 offline_reason="upstream_missing"，保留其原本的 user_takedown 状态。
        """
        current_ts = float(now if now is not None else time.time())
        query = {
            "source": {"$in": list(sources)},
            "group_status": GROUP_STATUS_ACTIVE,
            "expires_at": {"$gt": current_ts},
        }
        cursor = self.col.find(query, {"_id": 0, "group_order_id": 1, "share_code": 1})
        missing_gids = []
        for doc in cursor:
            gid = str(doc.get("group_order_id") or "").strip()
            share_code = str(doc.get("share_code") or "").strip()
            # 只要 gid、去 tok_ 前缀、或 share_code 任一在本次外部拉取集合中，就代表外部还在
            if gid in active_external_keys:
                continue
            if gid.startswith("tok_") and gid[4:] in active_external_keys:
                continue
            if f"tok_{gid}" in active_external_keys:
                continue
            if share_code and share_code in active_external_keys:
                continue
            missing_gids.append(gid)

        if not missing_gids:
            return 0

        res = self.col.update_many(
            {"group_order_id": {"$in": missing_gids}},
            {
                "$set": {
                    "group_status": GROUP_STATUS_EXPIRED,
                    "offline_reason": OFFLINE_REASON_UPSTREAM_MISSING,
                    "offline_at": current_ts,
                    "updated_at": current_ts,
                }
            },
        )
        return int(res.modified_count or 0)

    def list_active_for_export(
        self,
        *,
        source: str = SOURCE_USER_UPLOAD,
        limit: int = 50,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """获取处于在架且可导出的真实海报拼团列表（供外挂导出脚本轮询）。"""
        current_ts = float(now if now is not None else time.time())
        self.expire_outdated_groups(now=current_ts)
        safe_limit = max(1, min(int(limit), 200))
        target_source = str(source or SOURCE_USER_UPLOAD).strip()
        cursor = (
            self.col.find(
                {
                    "parse_status": PARSE_STATUS_PARSED,
                    "group_status": GROUP_STATUS_ACTIVE,
                    "expires_at": {"$gt": current_ts},
                    "remain_people": {"$gt": 0},
                },
                {"_id": 0},
            )
            .sort([("remain_people", ASCENDING), ("created_at", DESCENDING)])
            .limit(max(safe_limit * 4, 120))
        )
        results: List[Dict[str, Any]] = []
        for doc in cursor:
            poster_path = str(doc.get("poster_path") or "").strip()
            if not poster_path:
                continue
            doc_source = str(doc.get("source") or SOURCE_USER_UPLOAD)
            if target_source and doc_source != target_source:
                continue
            results.append(doc)
            if len(results) >= safe_limit:
                break
        return results

    def list_pending_for_remote_export(
        self,
        target: str = EXPORT_TARGET_MIAOZHULI,
        limit: int = 30,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """获取符合向外部平台同步条件的站内在架记录（仅限真实用户上传/提交，避免循环反刍）。"""
        current_ts = float(now if now is not None else time.time())
        self.expire_outdated_groups(now=current_ts)
        safe_limit = max(1, min(int(limit), 100))
        target_name = str(target or EXPORT_TARGET_MIAOZHULI).strip()

        query: Dict[str, Any] = {
            "source": {"$in": [SOURCE_USER_UPLOAD, SOURCE_USER_TEXT]},
            "parse_status": PARSE_STATUS_PARSED,
            "group_status": GROUP_STATUS_ACTIVE,
            "expires_at": {"$gt": current_ts},
            "remain_people": {"$gt": 0},
            f"exported_targets.{target_name}.status": {"$ne": EXPORT_STATUS_SUCCESS},
            f"exported_targets.{target_name}.attempts": {"$not": {"$gte": 3}},
        }
        if target_name == EXPORT_TARGET_MIAOZHULI:
            # 秒助力平台必须具备拼多多分享口令文本
            query["share_text"] = {"$regex": r"\S+"}

        cursor = (
            self.col.find(query, {"_id": 0})
            .sort([("remain_people", ASCENDING), ("created_at", DESCENDING)])
            .limit(safe_limit)
        )
        return list(cursor)

    def record_export_result(
        self,
        group_order_id: str,
        target: str,
        *,
        success: bool,
        remote_id: str = "",
        error_msg: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """原子记录向外部目标平台的发布审计结果与状态。"""
        current_ts = float(now if now is not None else time.time())
        gid = str(group_order_id or "").strip()
        target_name = str(target or "").strip()
        if not gid or not target_name:
            return None

        update_set: Dict[str, Any] = {
            f"exported_targets.{target_name}.last_attempt_at": current_ts,
            "updated_at": current_ts,
        }
        if success:
            update_set[f"exported_targets.{target_name}.status"] = EXPORT_STATUS_SUCCESS
            update_set[f"exported_targets.{target_name}.remote_id"] = str(remote_id or "")
            update_set[f"exported_targets.{target_name}.exported_at"] = current_ts
            update_set[f"exported_targets.{target_name}.error_msg"] = ""
            update_set["last_exported_at"] = current_ts
        else:
            update_set[f"exported_targets.{target_name}.status"] = EXPORT_STATUS_FAILED
            update_set[f"exported_targets.{target_name}.error_msg"] = str(error_msg or "")[:300]

        return self.col.find_one_and_update(
            {"group_order_id": gid},
            {
                "$set": update_set,
                "$inc": {f"exported_targets.{target_name}.attempts": 1},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )


