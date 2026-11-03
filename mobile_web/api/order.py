# -*- coding: utf-8 -*-
"""
订单接口路由：支持增量订单异步同步与推手返利收益明细查询
"""
from typing import Optional
from fastapi import APIRouter, Query, Depends, HTTPException, status
from services.order_service import OrderService
from auth import get_current_uid

router = APIRouter(prefix="/api/order", tags=["订单与收益"])
order_service = OrderService()


@router.post("/sync_and_query")
def sync_and_query_orders(
    current_uid: str = Depends(get_current_uid),
    uid: Optional[str] = Query(None, description="前端传入的推手UID (兼容参数，服务端严格校验)"),
    platform: str = Query("pdd", max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台代码"),
    days: int = Query(7, ge=0, le=90, description="查询时间跨度(天)，0为全部，默认7天，最大90天"),
    force: bool = Query(False, description="是否强制立即同步 (服务端统一控频，前端无法穿透)"),
):
    """
    异步触发增量订单同步入库，并查询当前认证推手在指定时间范围内的订单列表及返利总计。
    - 强制基于服务端会话令牌确认身份，杜绝传参横向越权；
    - 纯本地数据库极速返回，不阻塞当前请求。
    """
    if uid and uid.strip() != current_uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权查询非本人名下的订单数据"
        )

    return order_service.sync_and_query_orders(
        platform=platform,
        uid=current_uid,
        days=days,
        force_sync=False
    )
