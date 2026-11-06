# -*- coding: utf-8 -*-
"""
订单领域统一数据模型 (DTO)
"""
from typing import List, Optional
from pydantic import BaseModel, Field


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


class OrderItem(BaseModel):
    """订单单品信息"""
    order_sn: str
    parent_order_sn: Optional[str] = None
    platform: str = "pdd"
    goods_id: str
    goods_name: str = "精选商品"
    goods_thumbnail_url: str = ""
    goods_quantity: int = 1
    order_amount_yuan: float = 0.0
    order_amount_basis: str = "unknown"
    # 平台当前报告口径；两种佣金均可能是预估，不表示钱包净到账。
    promotion_amount_yuan: float = 0.0
    estimated_commission_yuan: Optional[float] = None
    settled_commission_yuan: Optional[float] = None
    commission_basis: Optional[str] = None
    promotion_rate: float = 0
    order_status: Optional[int | str] = None
    order_status_desc: str = "未知状态"
    is_refund: bool = False
    is_settled: bool = False
    is_invalid: bool = False
    order_time: Optional[int] = None
    order_time_basis: str = "unknown"
    order_time_str: str = ""
    # 历史付款时间兼容字段；JD 无真实付款时间时保持 None。
    order_pay_time: Optional[int] = None
    order_pay_time_str: str = ""


class UserOrderStats(BaseModel):
    """推手订单明细及返利汇总汇总模型"""
    total_count: int = 0
    total_commission: float = 0.0
    settled_commission: float = 0.0
    refund_commission: float = 0.0
    refund_count: int = 0
    order_list: List[OrderItem] = Field(default_factory=list)
    platform: str = "pdd"
    uid: str = ""
    query_days: int = 7
    last_sync_time: int = 0
    last_sync_attempt_time: int = 0
    sync_status: str = "idle"
    sync_error: str = ""
    synced_count: int = 0
