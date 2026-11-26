# -*- coding: utf-8 -*-
"""单清单商品收藏夹路由：强制登录鉴权与个人隔离。"""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from auth import get_current_uid
from api.home import favorite_repo, product_service
from services.favorite_service import FavoriteService

router = APIRouter(prefix="/api/favorites", tags=["商品收藏夹"])
favorite_service = FavoriteService(favorite_repo=favorite_repo, product_repo=product_service.repo)


class FavoriteAddRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$")
    product_id: str = Field("", max_length=128)
    goods_sign: Optional[str] = Field(None, max_length=128)
    name: Optional[str] = Field(None, max_length=200)
    image_url: Optional[str] = Field(None, max_length=512)
    price: Optional[float] = None


class FavoriteRemoveRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$")
    product_id: str = Field("", max_length=128)
    goods_sign: Optional[str] = Field(None, max_length=128)


@router.get("/list")
def list_favorites(
    page: int = Query(1, ge=1, le=500, description="页码"),
    page_size: int = Query(20, ge=1, le=500, description="每页条数 (最大500)"),
    current_uid: str = Depends(get_current_uid),
):
    return favorite_service.list_favorites(uid=current_uid, page=page, page_size=page_size)


@router.post("/add")
def add_favorite(req: FavoriteAddRequest, current_uid: str = Depends(get_current_uid)):
    return favorite_service.add_favorite(
        uid=current_uid,
        platform=req.platform,
        product_id=req.product_id,
        goods_sign=req.goods_sign or "",
        name=req.name or "",
        image_url=req.image_url or "",
        price=req.price,
    )


@router.post("/remove")
def remove_favorite(req: FavoriteRemoveRequest, current_uid: str = Depends(get_current_uid)):
    return favorite_service.remove_favorite(
        uid=current_uid,
        platform=req.platform,
        product_id=req.product_id,
        goods_sign=req.goods_sign or "",
    )
