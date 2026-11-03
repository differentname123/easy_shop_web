# -*- coding: utf-8 -*-
"""
爆单推广接口路由：数据库商品的多平台筛选、分类与分页
"""
from typing import Optional
from fastapi import APIRouter, Query
from services.product_service import ProductService

router = APIRouter(prefix="/api/goods", tags=["爆单推广库"])
product_service = ProductService()


@router.get("/list")
def list_boom_goods(
    platform: str = Query("all", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台代码，all 查询全部本地商品"),
    category: Optional[str] = Query(None, max_length=20, description="商品分类"),
    keyword: Optional[str] = Query(None, max_length=30, description="关键词模糊查询 (最长30字)"),
    sort_by: str = Query("latest", pattern="^(latest|save_desc|price_asc|price_desc)$", description="排序"),
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


@router.get("/categories")
def get_categories(
    platform: str = Query("all", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台代码"),
):
    """
    获取爆单库现存分类列表
    """
    cats = product_service.get_boom_categories(platform=platform)
    return {"status": "success", "data": cats}
