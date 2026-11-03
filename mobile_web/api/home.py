# -*- coding: utf-8 -*-
"""
首页接口路由：拼多多进宝官方推荐与关键词检索
"""
from typing import Optional
from fastapi import APIRouter, Query
from services.product_service import ProductService
from adapters import get_all_platforms, require_adapter

router = APIRouter(prefix="/api/home", tags=["首页推荐与搜索"])
product_service = ProductService()


@router.get("/platforms")
def get_platforms():
    return {"status": "success", "data": get_all_platforms()}


@router.get("/channels")
def get_channels(platform: str = Query("pdd", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$")):
    """频道来自适配器，不在路由中维护某个平台的业务配置。"""
    return {"status": "success", "data": list(require_adapter(platform).channels)}


@router.get("/recommend")
def get_recommend_goods(
    channel_type: int = Query(5, ge=1, description="平台频道代码，见 channels 接口"),
    offset: int = Query(0, ge=0, le=1000, description="偏移量"),
    limit: int = Query(20, ge=1, le=50, description="条数 (1-50)"),
    uid: Optional[str] = Query(None, max_length=64, description="推手用户UID"),
    platform: str = Query("pdd", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台标识"),
):
    """
    获取首页官方推荐流商品
    """
    return product_service.get_home_recommendations(
        channel_type=channel_type,
        offset=offset,
        limit=limit,
        uid=uid,
        platform=platform
    )


@router.get("/search")
def search_goods(
    keyword: str = Query(..., min_length=1, max_length=30, description="搜索关键词 (1-30字)"),
    page: int = Query(1, ge=1, le=20, description="页码 (最大20页)"),
    page_size: int = Query(20, ge=1, le=50, description="每页数量 (1-50)"),
    uid: Optional[str] = Query(None, max_length=64, description="推手用户UID"),
    platform: str = Query("pdd", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台标识"),
):
    """
    首页关键词搜索联盟商品
    """
    return product_service.search_home_goods(
        keyword=keyword,
        page=page,
        page_size=page_size,
        uid=uid,
        platform=platform
    )


@router.get("/detail")
def get_goods_detail(
    goods_sign: Optional[str] = Query(None, max_length=128, description="商品Sign"),
    goods_id: Optional[str] = Query(None, max_length=64, description="商品ID"),
    uid: Optional[str] = Query(None, max_length=64, description="推手用户UID"),
    platform: str = Query("pdd", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台标识"),
):
    """
    获取官方商品完整规范化详情 (含多张高清实拍轮播图、满减优惠、店铺与服务保障)
    """
    key = goods_sign or goods_id or ""
    return product_service.get_goods_detail(
        platform=platform,
        goods_id_or_sign=key,
        goods_id=goods_id,
        goods_sign=goods_sign,
        uid=uid
    )
