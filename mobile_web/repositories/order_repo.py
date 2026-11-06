# -*- coding: utf-8 -*-
"""订单持久化及全量聚合；平台状态解释由服务提供，不在仓储猜测。"""
from datetime import datetime, timedelta, timezone
import math
from typing import Dict, Any, List, Optional
from pymongo import UpdateOne
from repositories.base_repo import BaseRepository
from infrastructure.logging_utils import format_exc_summary
from models.product import normalize_platform
from models.order import normalize_order_fields
from models.user import normalize_promoter_id
from config import logger

# 联盟业务展示统一北京时间；存储及来源时钟仍是UTC时间戳。
_DISPLAY_TIMEZONE = timezone(timedelta(hours=8))


class OrderRepository(BaseRepository):
    COLLECTION_NAME = "orders"

    def _init_indexes(self):
        try:
            self.col.create_index([("platform", 1), ("order_sn", 1)], unique=True)
            # 保留历史付款时间索引，混合新旧数据的兼容分支仍会用到。
            self.col.create_index([("platform", 1), ("promoter_uid", 1), ("order_pay_time", -1)])
            self.col.create_index([("platform", 1), ("promoter_uid", 1), ("order_time", -1), ("order_sn", -1)])
            self.col.create_index([("order_modify_at", -1)])
            return True
        except Exception as exc:
            logger.warning(
                "❌ 【数据库索引告警】创建订单集合索引时发生异常 | 集合[orders] 异常<%s> 排查方向<请检查MongoDB连接状态或orders集合中是否存在重复(platform+order_sn)记录>",
                format_exc_summary(exc),
            )
            return False

    def upsert_orders(self, orders: List[Dict[str, Any]], platform: str = "pdd") -> int:
        """只写规范字段；原子防旧页回退，幂等重放仍算该页已处理。

        更新管线需要 MongoDB 4.2+。不做先读后写，避免同步与授权回填互相抹掉归属。
        order_modify_at 是适配器确认的秒级来源更新时间，不用本机 synced_at 猜新旧。
        """
        if not orders:
            return 0
        platform = normalize_platform(platform)
        now = datetime.now(timezone.utc)
        operations = []
        for order in orders:
            order_sn = str(order.get("order_sn") or "").strip()
            if not order_sn:
                raise ValueError("平台订单缺少 order_sn，不能确认该页已完整入库")
            doc = normalize_order_fields(order, platform)
            doc.pop("_id", None)
            doc.update(platform=platform, order_sn=order_sn)
            if "promoter_uid" in doc:
                doc["promoter_uid"] = str(doc["promoter_uid"] or "").strip()
            if doc.get("promoter_id") is not None and doc["promoter_id"] != "":
                doc["promoter_id"] = normalize_promoter_id(doc["promoter_id"])
            else:
                doc.pop("promoter_id", None)
            # 新页缺证据不清掉旧页保存的未知归因，后续绑定仍可安全回填。
            if not doc.get("attribution_identity"):
                doc.pop("attribution_identity", None)
            modified_at = doc.get("order_modify_at")
            if modified_at is None:
                source_time = 0.0
                doc.pop("order_modify_at", None)
            else:
                source_time = float(modified_at)
                if isinstance(modified_at, bool) or not math.isfinite(source_time) or source_time < 0:
                    raise ValueError("order_modify_at 必须是规范化来源时间戳")
                doc["order_modify_at"] = source_time
            # 没有可信来源时间的更新不能覆盖已具有来源时间的状态。
            fresh = {"$lte": [
                {"$convert": {"input": "$order_modify_at", "to": "double", "onError": 0, "onNull": 0}},
                source_time,
            ]}
            owned = {"$ne": [{"$ifNull": ["$promoter_uid", ""]}, ""]}
            fields = {"platform": {"$literal": platform}, "order_sn": {"$literal": order_sn},
                      "synced_at": {"$literal": now}}
            for key, value in doc.items():
                if key in ("platform", "order_sn"):
                    continue
                replacement = {"$cond": [fresh, {"$literal": value}, f"${key}"]}
                if key in ("promoter_uid", "attribution_status", "attribution_identity", "promoter_id"):
                    # 已知归属不可被新页的缺字段、未知身份或另一个UID改写。
                    previous = ({"$ifNull": ["$attribution_status", "matched"]}
                                if key == "attribution_status" else f"${key}")
                    replacement = {"$cond": [owned, previous, replacement]}
                fields[key] = replacement
            operations.append(UpdateOne(
                {"platform": platform, "order_sn": order_sn}, [{"$set": fields}], upsert=True
            ))
        result = self.col.bulk_write(operations, ordered=False)
        if not result.acknowledged:
            raise RuntimeError("订单批量写入未确认")
        return len(operations)

    def assign_unmatched_orders(self, platform: str, identity: dict, uid: str) -> int:
        """绑定身份后补归因；无 UID 才允许更新，绝不转移他人已归属订单。"""
        clean_uid = str(uid or "").strip()
        if not clean_uid or not isinstance(identity, dict) or not identity:
            return 0
        query: Dict[str, Any] = {"platform": normalize_platform(platform),
                                 "promoter_uid": {"$in": [None, ""]}}
        for key, value in identity.items():
            if (not isinstance(key, str) or not key or "." in key or "$" in key
                    or not isinstance(value, str) or not value.strip()):
                raise ValueError("无效的平台归因身份")
            query[f"attribution_identity.{key}"] = value
        result = self.col.update_many(query, {"$set": {"promoter_uid": clean_uid, "attribution_status": "matched"}})
        if not result.acknowledged:
            raise RuntimeError("订单归因回填未确认")
        return result.modified_count

    @staticmethod
    def _status_expression(field: str) -> dict:
        return {"$eq": [f"${field}", True]}

    def _format_order(self, doc: dict) -> dict:
        doc = normalize_order_fields(doc, doc.get("platform", ""))
        pay_time = doc.get("order_pay_time")
        order_time = doc["order_time"]
        return {
            "platform": doc.get("platform", ""),
            "order_sn": doc.get("order_sn"),
            "parent_order_sn": doc.get("parent_order_sn"),
            "goods_id": doc.get("goods_id") or "",
            "goods_name": doc.get("goods_name", "精选商品"),
            "goods_thumbnail_url": doc.get("goods_thumbnail_url", ""),
            "goods_quantity": doc.get("goods_quantity", 1),
            "order_amount_yuan": doc.get("order_amount_yuan", 0.0),
            "order_amount_basis": doc["order_amount_basis"],
            # 当前报告口径由适配器选择；付款/结算预估均不表示净到账。
            "promotion_amount_yuan": float(doc.get("promotion_amount_yuan") or 0.0),
            "estimated_commission_yuan": doc.get("estimated_commission_yuan"),
            "settled_commission_yuan": doc.get("settled_commission_yuan"),
            "commission_basis": doc.get("commission_basis"),
            "promotion_rate": doc.get("promotion_rate", 0),
            "order_status": doc.get("order_status"),
            "order_status_desc": doc.get("order_status_desc", "未知状态"),
            "is_refund": doc.get("is_refund") is True,
            "is_settled": doc.get("is_settled") is True,
            "is_invalid": doc.get("is_invalid") is True,
            "order_time": order_time,
            "order_time_basis": doc["order_time_basis"],
            "order_time_str": datetime.fromtimestamp(order_time, _DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M") if order_time is not None else "",
            "order_pay_time": pay_time,
            "order_pay_time_str": datetime.fromtimestamp(pay_time, _DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M") if pay_time else "",
        }

    def query_user_orders(
        self,
        platform: str = "pdd",
        promoter_uid: str = "",
        start_timestamp: Optional[int] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Mongo 单次 facet：统计全量匹配订单，明细单独排序/截取。失败交给服务返回 error。"""
        empty = {
            "total_count": 0, "total_commission": 0.0, "settled_commission": 0.0,
            "refund_commission": 0.0, "refund_count": 0, "order_list": [],
        }
        uid = str(promoter_uid or "").strip()
        if not uid:
            return empty
        query: Dict[str, Any] = {"platform": normalize_platform(platform), "promoter_uid": uid}
        if start_timestamp is not None and start_timestamp > 0:
            # 原生 OR 分支保留新/旧复合索引的可用性；缺失或 null 才回退付款时间。
            # 已声明的创建时间（以及真实 0）不能被另一个付款时间绕过筛选。
            query["$or"] = [
                {"order_time": {"$gte": int(start_timestamp)}},
                {"order_time": None, "order_pay_time": {"$gte": int(start_timestamp)}},
            ]
        refund = self._status_expression("is_refund")
        settled = self._status_expression("is_settled")
        invalid = self._status_expression("is_invalid")
        pipeline = [
            {"$match": query},
            {"$facet": {
                "stats": [
                    {"$project": {
                        "commission": {"$convert": {"input": "$promotion_amount_yuan", "to": "double", "onNull": 0}},
                        "refund": refund, "settled": settled, "invalid": invalid,
                    }},
                    {"$group": {
                        "_id": None,
                        "total_count": {"$sum": 1},
                        "total_commission": {"$sum": {"$cond": [
                            {"$and": [{"$not": ["$refund"]}, {"$not": ["$invalid"]}]}, "$commission", 0
                        ]}},
                        "settled_commission": {"$sum": {"$cond": [
                            {"$and": [{"$not": ["$refund"]}, {"$not": ["$invalid"]}, "$settled"]}, "$commission", 0
                        ]}},
                        "refund_commission": {"$sum": {"$cond": ["$refund", "$commission", 0]}},
                        "refund_count": {"$sum": {"$cond": ["$refund", 1, 0]}},
                    }},
                ],
                "order_list": [
                    # 兼容未回填的旧文档，但不让明细排序/截断影响全量佣金统计。
                    {"$set": {"_query_order_time": {"$ifNull": ["$order_time", "$order_pay_time"]}}},
                    {"$sort": {"_query_order_time": -1, "order_sn": -1}},
                    {"$limit": max(1, min(int(limit), 100))},
                    {"$project": {"_id": 0, "_query_order_time": 0}},
                ],
            }},
        ]
        rows = list(self.col.aggregate(pipeline))
        facet = rows[0] if rows else {}
        stats = (facet.get("stats") or [{}])[0]
        return {
            "total_count": int(stats.get("total_count", 0)),
            "total_commission": round(float(stats.get("total_commission", 0)), 2),
            "settled_commission": round(float(stats.get("settled_commission", 0)), 2),
            "refund_commission": round(float(stats.get("refund_commission", 0)), 2),
            "refund_count": int(stats.get("refund_count", 0)),
            "order_list": [self._format_order(doc) for doc in facet.get("order_list", [])],
        }
