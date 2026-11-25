# -*- coding: utf-8 -*-
"""
转链接口路由：令牌校验本人身份，服务解析数字归因或平台渠道资格。
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_optional_uid
from api.home import product_stats_buffer
from api.user import user_service
from models.product import resolve_item_identity
from pydantic import BaseModel, Field
from services.promote_service import PromoteService
from services.product_stats_service import extract_actor_key

router = APIRouter(prefix="/api/promote", tags=["推广生链与归因"])
promote_service = PromoteService(user_service=user_service)


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
def convert_link(request: Request, req: ConvertRequest, current_uid: Optional[str] = Depends(get_optional_uid)):
    """
    个人身份由服务端解析；仅要求授权的平台才做渠道分流，京东不走备案。
    """
    if req.uid and req.uid.strip() != current_uid:
        raise HTTPException(status_code=403, detail="推广归因必须使用本人已验证身份")
    goods_key = req.goods_sign or req.goods_id or ""
    res = promote_service.convert_link(
        platform=req.platform,
        goods_id_or_sign=goods_key,
        goods_id=req.goods_id,
        goods_sign=req.goods_sign,
        product_url=req.product_url,
        uid=current_uid,
        title=req.title,
        price=req.price,
        coupon_amount=req.coupon_amount,
    )
    if isinstance(res, dict) and res.get("status") == "success":
        plat, gid, gsign = resolve_item_identity(
            {"platform": req.platform, "goods_id": req.goods_id, "goods_sign": req.goods_sign},
            default_platform=req.platform,
        )
        pid = gid or gsign
        if pid:
            product_stats_buffer.record(plat, pid, "intent", extract_actor_key(request, current_uid))
    return res


class UniversalConvertRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000, description="用户粘贴的商品链接、推广链接、口令或分享文案")
    platform: str = Field("auto", max_length=32, pattern="^[a-z][a-z0-9_]*$")
    uid: Optional[str] = Field(None, max_length=64)


@router.post("/universal")
def universal_convert_link(
    request: Request,
    req: UniversalConvertRequest,
    current_uid: Optional[str] = Depends(get_optional_uid),
):
    """
    万能转链与商品详情反查接口：
    支持智能识别（platform='auto'）或指定平台（'pdd' / 'jd' / 'taobao'），
    将用户自带链接、已有推广短链或淘口令/京口令转换为本人推广链接，并返回反查的商品详情卡片。
    """
    if req.uid and req.uid.strip() != current_uid:
        raise HTTPException(status_code=403, detail="推广归因必须使用本人已验证身份")
    res = promote_service.universal_convert(
        raw_text=req.text,
        platform=req.platform,
        uid=current_uid,
    )
    if isinstance(res, dict) and res.get("status") == "success" and isinstance(res.get("product"), dict):
        prod = res["product"]
        plat = res.get("platform") or prod.get("platform") or ""
        pid = str(prod.get("product_id") or prod.get("goods_id") or prod.get("goods_sign") or "").strip()
        if plat and pid:
            product_stats_buffer.record(plat, pid, "intent", extract_actor_key(request, current_uid))
    return res


