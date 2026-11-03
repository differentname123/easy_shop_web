# -*- coding: utf-8 -*-
"""
订单领域统一数据模型 (DTO)
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    """订单单品信息"""
    order_sn: str
    goods_id: str
    goods_name: str = "精选商品"
    goods_thumbnail_url: str = ""
    goods_quantity: int = 1
    order_amount_yuan: float = 0.0
    promotion_amount_yuan: float = 0.0
    promotion_rate: int = 0
    order_status: Optional[int | str] = None
    order_status_desc: str = "未知状态"
    is_refund: bool = False
    is_settled: bool = False
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
