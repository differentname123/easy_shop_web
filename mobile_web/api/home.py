# -*- coding: utf-8 -*-
"""
首页接口路由：拼多多进宝官方推荐与关键词检索
"""
from typing import Optional
from fastapi import APIRouter, Query, Depends, Request, HTTPException
from auth import get_optional_uid
from public_boundary import (
    FORBIDDEN_PLAINTEXT_QUERY_KEYS,
    require_item_code,
    resolve_internal_product_keys,
)
from repositories.favorite_repo import FavoriteRepository
from services.product_service import ProductService
from services.product_stats_service import ProductStatsBuffer, extract_actor_key
from adapters import get_all_platforms, require_adapter

router = APIRouter(prefix="/api/home", tags=["首页推荐与搜索"])
product_service = ProductService()
product_stats_buffer = ProductStatsBuffer(lambda: getattr(product_service, "repo", None))
favorite_repo = FavoriteRepository()


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
    cursor: Optional[str] = Query(None, max_length=2048, description="来源续页游标"),
    platform: str = Query("pdd", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台标识"),
    current_uid: Optional[str] = Depends(get_optional_uid),
):
    """
    获取首页官方推荐流商品
    """
    return product_service.get_home_recommendations(
        channel_type=channel_type,
        offset=offset,
        limit=limit,
        cursor=cursor,
        uid=current_uid,
        platform=platform
    )


@router.get("/search")
def search_goods(
    keyword: str = Query(..., min_length=1, max_length=500, description="搜索关键词或分享文案"),
    page: int = Query(1, ge=1, le=20, description="页码 (最大20页)"),
    page_size: int = Query(20, ge=1, le=50, description="每页数量 (1-50)"),
    platform: str = Query("pdd", min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台标识"),
    current_uid: Optional[str] = Depends(get_optional_uid),
):
    """
    首页关键词搜索联盟商品
    """
    return product_service.search_home_goods(
        keyword=keyword,
        page=page,
        page_size=page_size,
        uid=current_uid,
        platform=platform
    )


@router.get("/detail")
def get_goods_detail(
    request: Request,
    item_code: str = Query(..., min_length=8, max_length=256, description="加密商品编号"),
    current_uid: Optional[str] = Depends(get_optional_uid),
):
    """
    获取官方商品完整规范化详情 (含多张高清实拍轮播图、满减优惠、店铺与服务保障)
    """
    if any(k.lower() in FORBIDDEN_PLAINTEXT_QUERY_KEYS for k in request.query_params.keys()):
        raise HTTPException(status_code=400, detail="不支持明文商品参数，请使用加密商品编号")
    plat, cid = require_item_code(item_code)
    plat, gid, gsign = resolve_internal_product_keys(plat, cid)
    key = gsign or gid
    res = product_service.get_goods_detail(
        platform=plat,
        goods_id_or_sign=key,
        goods_id=gid or None,
        goods_sign=gsign or None,
        uid=current_uid,
    )
    if isinstance(res, dict) and res.get("status") == "success" and isinstance(res.get("data"), dict):
        data = res["data"]
        resolved_plat = data.get("platform") or plat
        pid = str(data.get("product_id") or data.get("goods_id") or data.get("goods_sign") or "").strip()
        if pid:
            product_stats_buffer.record(
                resolved_plat,
                pid,
                "view",
                extract_actor_key(request, current_uid),
            )
        if current_uid and pid and (
            favorite_repo._db is not None or type(getattr(product_service, "repo", None)).__name__ == "ProductRepository"
        ):
            try:
                is_fav = favorite_repo.is_favorited(current_uid, resolved_plat, pid)
                if not is_fav:
                    doc_sign = str(data.get("goods_sign") or "").strip()
                    if doc_sign and doc_sign != pid:
                        is_fav = favorite_repo.is_favorited(current_uid, resolved_plat, doc_sign)
                res = dict(res, data=dict(data, is_favorited=is_fav))
            except Exception:
                pass
    return res
