# -*- coding: utf-8 -*-
"""
转链接口路由：根据用户 UID 动态生成带返利归因的推广链接
"""
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field
from services.promote_service import PromoteService

router = APIRouter(prefix="/api/promote", tags=["推广生链与归因"])
promote_service = PromoteService()


class ConvertRequest(BaseModel):
    platform: str = Field("pdd", max_length=32, pattern="^[a-z][a-z0-9_]*$")
    goods_sign: Optional[str] = Field(None, max_length=128)
    goods_id: Optional[str] = Field(None, max_length=64)
    product_url: Optional[str] = Field(None, max_length=512)
    uid: Optional[str] = Field(None, max_length=64)
    title: Optional[str] = Field(None, max_length=200)
    price: Optional[float] = None
    coupon_amount: Optional[float] = None


@router.post("/convert")
def convert_link(req: ConvertRequest):
    """
    点击商品时生成推手专属转链及平台口令（未登录使用官方推广位缓存）
    """
    goods_key = req.goods_sign or req.goods_id or ""
    return promote_service.convert_link(
        platform=req.platform,
        goods_id_or_sign=goods_key,
        goods_id=req.goods_id,
        goods_sign=req.goods_sign,
        product_url=req.product_url,
        uid=req.uid,
        title=req.title,
        price=req.price,
        coupon_amount=req.coupon_amount,
    )

