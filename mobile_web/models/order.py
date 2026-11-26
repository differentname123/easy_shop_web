# -*- coding: utf-8 -*-
"""
订单领域统一口径归一化函数
"""


def normalize_order_fields(order: dict, platform: str) -> dict:
    """公共边界补齐旧协议的中性口径；绝不以创建时间伪造付款时间。

    真实 0 值保留。新平台应声明自己的口径；没有来源证据时标为 unknown。
    同时供历史明细展示使用，不要求先运行数据迁移才能查询。
    """
    doc = dict(order)
    paid_time = doc.get("order_pay_time")
    if doc.get("order_time") is None:
        doc["order_time"] = paid_time
        if paid_time is not None and doc.get("order_time_basis") in (None, "", "unknown"):
            doc["order_time_basis"] = "paid"
    if not doc.get("order_time_basis"):
        doc["order_time_basis"] = (
            "paid" if paid_time is not None and doc["order_time"] == paid_time else "unknown"
        )
    if not doc.get("order_amount_basis"):
        doc["order_amount_basis"] = "paid" if platform in ("pdd", "taobao") else "unknown"
    return doc

