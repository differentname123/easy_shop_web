# -*- coding: utf-8 -*-
"""订单持久化及全量聚合；平台状态解释由服务提供，不在仓储猜测。"""
from datetime import datetime, timezone
import math
from typing import Dict, Any, List, Optional
from pymongo import UpdateOne
from repositories.base_repo import BaseRepository
from models.product import normalize_platform
from config import logger


class OrderRepository(BaseRepository):
    COLLECTION_NAME = "orders"

    def _init_indexes(self):
        try:
            self.col.create_index([("platform", 1), ("order_sn", 1)], unique=True)
            self.col.create_index([("platform", 1), ("promoter_uid", 1), ("order_pay_time", -1)])
            self.col.create_index([("order_modify_at", -1)])
            return True
        except Exception as exc:
            logger.warning(f"订单集合索引创建提示: {exc}")
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
            doc = dict(order)
            doc.pop("_id", None)
            doc.update(platform=platform, order_sn=order_sn)
            if "promoter_uid" in doc:
                doc["promoter_uid"] = str(doc["promoter_uid"] or "").strip()
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
                if key in ("promoter_uid", "attribution_status", "attribution_identity"):
                    # 已知归属不可被新页的缺字段、未知身份或另一个UID改写。
                    previous = ({"$ifNull": ["$attribution_status", "matched"]}
                                if key == "attribution_status" else f"${key}")
                    replacement = {"$cond": [owned, previous, replacement]}
                fields[key] = replacement
            operations.append(UpdateOne(
                {"platform": platform, "order_sn": order_sn}, [{"$set": fields}], upsert=True
            ))
        try:
            result = self.col.bulk_write(operations, ordered=False)
            if not result.acknowledged:
                raise RuntimeError("订单批量写入未确认")
        except Exception:
            logger.exception("批量写入订单失败")
            raise
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
    def _status_flag(doc: dict, field: str, statuses: list) -> bool:
        return doc[field] is True if field in doc else doc.get("order_status") in statuses

    @staticmethod
    def _status_expression(field: str, statuses: list) -> dict:
        # 显式 False 优先；只有旧文档缺失规范字段时才回退到适配器规则。
        return {"$cond": [
            {"$eq": [{"$type": f"${field}"}, "missing"]},
            {"$in": ["$order_status", statuses]},
            {"$eq": [f"${field}", True]},
        ]}

    def _format_order(self, doc: dict, rules: dict) -> dict:
        pay_time = doc.get("order_pay_time")
        return {
            "platform": doc.get("platform", ""),
            "order_sn": doc.get("order_sn"),
            "goods_id": doc.get("goods_id") or "",
            "goods_name": doc.get("goods_name", "精选商品"),
            "goods_thumbnail_url": doc.get("goods_thumbnail_url", ""),
            "goods_quantity": doc.get("goods_quantity", 1),
            "order_amount_yuan": doc.get("order_amount_yuan", 0.0),
            # 当前报告口径由适配器选择；付款/结算预估均不表示净到账。
            "promotion_amount_yuan": float(doc.get("promotion_amount_yuan") or 0.0),
            "estimated_commission_yuan": doc.get("estimated_commission_yuan"),
            "settled_commission_yuan": doc.get("settled_commission_yuan"),
            "commission_basis": doc.get("commission_basis"),
            "promotion_rate": doc.get("promotion_rate", 0),
            "order_status": doc.get("order_status"),
            "order_status_desc": doc.get("order_status_desc", "未知状态"),
            "is_refund": self._status_flag(doc, "is_refund", rules.get("refund", [])),
            "is_settled": self._status_flag(doc, "is_settled", rules.get("settled", [])),
            "is_invalid": doc.get("is_invalid") is True,
            "order_pay_time": pay_time,
            "order_pay_time_str": datetime.fromtimestamp(pay_time, timezone.utc).strftime("%Y-%m-%d %H:%M") if pay_time else "",
        }

    def query_user_orders(
        self,
        platform: str = "pdd",
        promoter_uid: str = "",
        start_timestamp: Optional[int] = None,
        limit: int = 100,
        legacy_order_statuses: Optional[dict] = None,
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
            query["order_pay_time"] = {"$gte": int(start_timestamp)}
        rules = legacy_order_statuses or {}
        refund = self._status_expression("is_refund", list(rules.get("refund", [])))
        settled = self._status_expression("is_settled", list(rules.get("settled", [])))
        invalid = self._status_expression("is_invalid", [])
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
                    {"$sort": {"order_pay_time": -1, "order_sn": -1}},
                    {"$limit": max(1, min(int(limit), 100))},
                    {"$project": {"_id": 0}},
                ],
            }},
        ]
        try:
            rows = list(self.col.aggregate(pipeline))
            facet = rows[0] if rows else {}
            stats = (facet.get("stats") or [{}])[0]
            return {
                "total_count": int(stats.get("total_count", 0)),
                "total_commission": round(float(stats.get("total_commission", 0)), 2),
                "settled_commission": round(float(stats.get("settled_commission", 0)), 2),
                "refund_commission": round(float(stats.get("refund_commission", 0)), 2),
                "refund_count": int(stats.get("refund_count", 0)),
                "order_list": [self._format_order(doc, rules) for doc in facet.get("order_list", [])],
            }
        except Exception:
            logger.exception("查询或聚合用户订单失败")
            raise
