# -*- coding: utf-8 -*-
"""拼多多多人团互助仓储层：独立操作 pdd_multi_groups 集合，与主站业务完全解耦。"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from repositories.base_repo import BaseRepository
from models.group_help import (
    DEFAULT_GROUP_TTL_SECONDS,
    GROUP_STATUS_ACTIVE,
    GROUP_STATUS_EXPIRED,
    GROUP_STATUS_FULL,
    GROUP_STATUS_PENDING_CONFIRM,
    MAX_PARSE_ATTEMPTS,
    PARSE_STATUS_FAILED,
    PARSE_STATUS_PARSED,
    PARSE_STATUS_PENDING,
    is_highly_similar_goods,
    validate_and_normalize_parsed_poster,
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
                "parse_status": PARSE_STATUS_PENDING,
                "parse_attempts": 0,
                "parse_error": "",
                "group_status": GROUP_STATUS_ACTIVE,
                "updated_at": now,
            }

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
            "short_url": str(data.get("short_url") or ""),
            "redirect_url": str(data.get("redirect_url") or ""),
            "refer_share_uin": str(data.get("refer_share_uin") or ""),
            "refer_share_channel": str(data.get("refer_share_channel") or ""),
            "refer_share_id": str(data.get("refer_share_id") or ""),
            "app_wake_url": str(data.get("app_wake_url") or ""),
            "h5_join_url": str(data.get("h5_join_url") or ""),
            "brand_card_url": str(data.get("brand_card_url") or ""),
            "poster_path": str(data.get("poster_path") or ""),
            "owner_token": str(data.get("owner_token") or ""),
            "owner_uid": str(data.get("owner_uid") or ""),
            "last_takedown_uid": "",
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
            tokens = [t.strip() for t in re.split(r"\s+", kw_clean) if t.strip()][:5]
            and_conditions = []
            for tok in tokens:
                pattern = re.escape(tok)
                and_conditions.append(
                    {
                        "$or": [
                            {"goods_name": {"$regex": pattern, "$options": "i"}},
                            {"keywords": {"$regex": pattern, "$options": "i"}},
                            {"group_order_id": tok},
                        ]
                    }
                )
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
        clean_op_uid = str(operator_uid or "").strip()

        if was_active and now_offline:
            update_fields["last_takedown_uid"] = "" if is_owner else clean_op_uid

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

