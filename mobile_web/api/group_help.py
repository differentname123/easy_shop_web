# -*- coding: utf-8 -*-
"""拼多多多人团互助独立路由模块：上传二维码图片、搜索列表、详情查询、跳转计数与用户共创状态反馈。"""
import base64
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from models.group_help import GroupFeedbackRequest
from services.group_help_service import GroupHelpService, GroupHelpWorker

router = APIRouter(prefix="/api/group_help", tags=["拼多多多人团互助"])
group_help_service = GroupHelpService()
group_help_worker = GroupHelpWorker(group_help_service, interval_seconds=8.0)


async def _extract_upload_bytes(request: Request) -> bytes:
    """兼容 JSON(image_base64)、multipart/form-data(file) 与原始二进制流三种上传方式。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON 请求体格式不正确")
        b64_str = str(body.get("image_base64") or body.get("image") or "").strip()
        if not b64_str:
            raise ValueError("请提供二维码图片数据 (image_base64)")
        if "," in b64_str and b64_str.startswith("data:"):
            b64_str = b64_str.split(",", 1)[1]
        try:
            return base64.b64decode(b64_str)
        except Exception as exc:
            raise ValueError("图片 Base64 编码格式无效") from exc

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file") or form.get("image")
        if upload is None or not hasattr(upload, "read"):
            raise ValueError("请在表单 file 字段中上传二维码分享图片")
        return await upload.read()

    raw = await request.body()
    if not raw:
        raise ValueError("未检测到上传的图片内容")
    return raw


@router.post("/upload")
async def upload_group_poster(request: Request):
    """
    上传拼多多多人团二维码分享图片：
    立即同步解码二维码 -> 免登录核验 307 重定向提取 group_order_id -> 写入待解析队列并反馈收录结果。
    """
    try:
        image_bytes = await _extract_upload_bytes(request)
        result = group_help_service.submit_poster_image(image_bytes)
        return result
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "error_code": "invalid_poster",
                "error_msg": str(exc),
            },
        )


@router.get("/list")
def list_groups(
    keyword: str = Query("", max_length=120, description="搜索商品名称、关键词或团单号"),
    filter_mode: str = Query("active", pattern="^(active|almost|all)$", description="筛选模式: active|almost|all"),
    offset: int = Query(0, ge=0, le=2000, description="分页偏移量"),
    limit: int = Query(20, ge=1, le=50, description="每页条数"),
):
    """获取多人团列表（仅展示已经过大模型解析出商品信息的团，支持关键词搜索）。"""
    data = group_help_service.list_groups(
        keyword=keyword,
        filter_mode=filter_mode,
        offset=offset,
        limit=limit,
    )
    return {"status": "success", "data": data}


@router.get("/detail/{group_order_id}")
def get_group_detail(
    group_order_id: str = Path(..., min_length=15, max_length=24, pattern=r"^\d+$"),
):
    """获取单个多人团完整详情。"""
    detail = group_help_service.get_group_detail(group_order_id)
    if not detail:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "not_found", "error_msg": "未找到该拼团记录"},
        )
    return {"status": "success", "data": detail}


@router.post("/click/{group_order_id}")
def record_group_click(
    group_order_id: str = Path(..., min_length=15, max_length=24, pattern=r"^\d+$"),
):
    """记录用户点击一键跳转去参团。"""
    detail = group_help_service.record_jump_click(group_order_id)
    if not detail:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "not_found", "error_msg": "未找到该拼团记录"},
        )
    return {"status": "success", "data": detail}


@router.post("/feedback/{group_order_id}")
def submit_group_feedback(
    payload: GroupFeedbackRequest,
    group_order_id: str = Path(..., min_length=15, max_length=24, pattern=r"^\d+$"),
):
    """
    用户共创维护多人团状态：
    - mark_full: 标记该团人已满（立即从进行中列表下线）
    - mark_expired: 标记该团已失效（立即下线）
    - joined_one: 我已参团（还差人数 -1，减至 0 自动标记已满）
    - set_remain: 直接指定还差几人
    """
    try:
        updated = group_help_service.submit_feedback(
            group_order_id=group_order_id,
            action=payload.action,
            remain_people=payload.remain_people,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error_code": "invalid_feedback", "error_msg": str(exc)},
        )

    if not updated:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "not_found", "error_msg": "未找到该拼团记录"},
        )
    return {"status": "success", "message": "感谢您的共创反馈，拼团状态已实时更新！", "data": updated}


@router.get("/poster/{group_order_id}")
def get_group_poster(
    group_order_id: str = Path(..., min_length=15, max_length=24, pattern=r"^\d+$"),
):
    """获取多人团分享海报图片。"""
    path = group_help_service.get_poster_file_path(group_order_id)
    if not path:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "poster_not_found", "error_msg": "未找到海报图片"},
        )
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
