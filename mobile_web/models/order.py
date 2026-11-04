# -*- coding: utf-8 -*-
"""
订单领域统一数据模型 (DTO)
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    """订单单品信息"""
    order_sn: str
    platform: str = "pdd"
    goods_id: str
    goods_name: str = "精选商品"
    goods_thumbnail_url: str = ""
    goods_quantity: int = 1
    order_amount_yuan: float = 0.0
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
