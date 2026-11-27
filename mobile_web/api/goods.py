# -*- coding: utf-8 -*-
"""
爆单推广接口路由：数据库商品的多平台筛选、分类与分页
"""
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from auth import get_optional_uid
from api.home import product_service, product_stats_buffer
from public_boundary import require_item_code
from services.product_stats_service import extract_actor_key

router = APIRouter(prefix="/api/goods", tags=["爆单推广库"])


class ProductActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_code: str = Field(..., min_length=8, max_length=256)
    action: str = Field(..., pattern="^(share|buyable|unbuyable)$")


@router.post("/action")
def record_product_action(
    request: Request,
    req: ProductActionRequest,
    current_uid: Optional[str] = Depends(get_optional_uid),
):
    """通用商品动作统计上报（分享、能买到/买不到），仅内存聚合异步刷盘。"""
    plat, cid = require_item_code(req.item_code)
    product_stats_buffer.record(
        plat,
        cid,
        req.action,
        extract_actor_key(request, current_uid),
    )
    return {"status": "success"}


@router.get("/list")
def list_boom_goods(
    platform: str = Query("all", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台代码，all 查询全部本地商品"),
    category: Optional[str] = Query(None, max_length=20, description="商品分类"),
    keyword: Optional[str] = Query(None, max_length=500, description="关键词或分享文案模糊查询"),
    sort_by: Optional[str] = Query(None, pattern="^(cost_performance|score|latest|save_desc|price_asc|price_desc)$", description="排序（无搜索默认 score，有搜索默认 cost_performance）"),
    page: int = Query(1, ge=1, le=50, description="页码 (最大50页)"),
    page_size: int = Query(20, ge=1, le=50, description="每页数量 (最大50条)"),
):
    """
    分页查询爆单库商品
    """
    return product_service.get_boom_products(
        platform=platform,
        category=category,
        keyword=keyword,
        sort_by=sort_by,
        page=page,
        page_size=page_size,
    )
