# -*- coding: utf-8 -*-
"""详情页锚定用户爆料路由：登录鉴权、安全压缩图读取与个人提交记录查询。"""
import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from auth import get_current_uid
from services.deal_submit_service import DealSubmitError, DealSubmitService

router = APIRouter(prefix="/api/deals", tags=["用户爆料"])
deal_submit_service = DealSubmitService()
_IMAGE_SEMAPHORE = asyncio.Semaphore(4)


class DealSubmitRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32, pattern="^[a-z][a-z0-9_]*$")
    product_id: Optional[str] = Field("", max_length=128)
    goods_id: Optional[str] = Field("", max_length=128)
    goods_sign: Optional[str] = Field("", max_length=128)
    deal_price: float = Field(..., gt=0, lt=1000000)
    tutorial_text: str = Field(..., min_length=1, max_length=1000)
    coupon_input: Optional[str] = Field("", max_length=600)
    images_base64: Optional[List[str]] = Field(default_factory=list, max_length=3)


@router.post("/submit")
async def submit_user_deal(
    req: DealSubmitRequest,
    current_uid: str = Depends(get_current_uid),
):
    """从商品详情页发起好价补充，支持最多 3 张压缩截图，按原有清洗流程异步处理。"""
    try:
        async with _IMAGE_SEMAPHORE:
            return await asyncio.to_thread(
                deal_submit_service.submit_user_deal,
                uid=current_uid,
                platform=req.platform,
                product_id=req.product_id or "",
                goods_id=req.goods_id or "",
                goods_sign=req.goods_sign or "",
                deal_price=req.deal_price,
                tutorial_text=req.tutorial_text,
                coupon_input=req.coupon_input or "",
                images_base64=req.images_base64 or [],
            )
    except DealSubmitError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": "error", "error_code": exc.error_code, "error_msg": str(exc)},
        )


@router.get("/my_submissions")
def list_my_submissions(
    limit: int = Query(30, ge=1, le=50, description="返回条数"),
    current_uid: str = Depends(get_current_uid),
):
    """获取当前登录用户提交的好价爆料记录。"""
    try:
        return deal_submit_service.get_my_submissions(uid=current_uid, limit=limit)
    except DealSubmitError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": "error", "error_code": exc.error_code, "error_msg": str(exc)},
        )


@router.get("/image/{image_id}")
def get_deal_submit_image(
    image_id: str = Path(..., min_length=24, max_length=24, pattern=r"^dsi_[0-9a-f]{20}$"),
):
    """读取用户爆料压缩截图。"""
    path = deal_submit_service.get_image_file_path(image_id)
    if not path:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "image_not_found", "error_msg": "图片不存在或已失效"},
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
