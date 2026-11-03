# -*- coding: utf-8 -*-
"""订单持久化及全量聚合；平台状态解释由服务提供，不在仓储猜测。"""
from datetime import datetime, timezone
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
        """只持久化规范化订单；失败抛出，不能让同步任务误报成功。"""
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
            doc.update(platform=platform, order_sn=order_sn, synced_at=now)
            if doc.get("promoter_uid"):
                doc["promoter_uid"] = str(doc["promoter_uid"]).strip()
            operations.append(UpdateOne(
                {"platform": platform, "order_sn": order_sn}, {"$set": doc}, upsert=True
            ))
        try:
            self.col.bulk_write(operations, ordered=False)
        except Exception:
            logger.exception("批量写入订单失败")
            raise
        # 幂等重复写入 modified_count 可以为 0，但本页仍已全部处理。
        return len(operations)

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
            "order_sn": doc.get("order_sn"),
            "goods_id": doc.get("goods_id") or "",
            "goods_name": doc.get("goods_name", "精选商品"),
            "goods_thumbnail_url": doc.get("goods_thumbnail_url", ""),
            "goods_quantity": doc.get("goods_quantity", 1),
            "order_amount_yuan": doc.get("order_amount_yuan", 0.0),
            "promotion_amount_yuan": float(doc.get("promotion_amount_yuan") or 0.0),
            "promotion_rate": doc.get("promotion_rate", 0),
            "order_status": doc.get("order_status"),
            "order_status_desc": doc.get("order_status_desc", "未知状态"),
            "is_refund": self._status_flag(doc, "is_refund", rules.get("refund", [])),
            "is_settled": self._status_flag(doc, "is_settled", rules.get("settled", [])),
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
        pipeline = [
            {"$match": query},
            {"$facet": {
                "stats": [
                    {"$project": {
                        "commission": {"$convert": {"input": "$promotion_amount_yuan", "to": "double", "onNull": 0}},
                        "refund": refund, "settled": settled,
                    }},
                    {"$group": {
                        "_id": None,
                        "total_count": {"$sum": 1},
                        "total_commission": {"$sum": {"$cond": ["$refund", 0, "$commission"]}},
                        "settled_commission": {"$sum": {"$cond": [
                            {"$and": [{"$not": ["$refund"]}, "$settled"]}, "$commission", 0
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
