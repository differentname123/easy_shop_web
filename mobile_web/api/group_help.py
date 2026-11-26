# -*- coding: utf-8 -*-
"""拼多多多人团互助独立路由模块：上传二维码图片、搜索列表、详情查询、跳转计数与用户共创状态反馈。"""
import asyncio
import base64
from typing import Optional, Tuple
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from auth import extract_viewer_auth_soft
from models.group_help import (
    GROUP_ID_PATH_PATTERN,
    GroupFeedbackRequest,
    GroupHelpAuthRequiredError,
    GroupHelpPermissionError,
    GroupTextSubmitRequest,
)
from services.group_help_service import GroupHelpService, GroupHelpWorker

router = APIRouter(prefix="/api/group_help", tags=["拼多多多人团互助"])
group_help_service = GroupHelpService()
group_help_worker = GroupHelpWorker(group_help_service, interval_seconds=8.0)

_UPLOAD_SEMAPHORE = asyncio.Semaphore(4)
_MAX_REQUEST_BODY_BYTES = 15 * 1024 * 1024  # 15MB (兼容 10MB 原图的 Base64/FormData 封装开销)


class _PayloadTooLargeError(ValueError):
    pass


async def _extract_upload_payload(request: Request) -> Tuple[bytes, str, str]:
    """兼容 JSON(image_base64 + 可选 owner_token/share_text)、multipart/form-data(file) 与原始二进制流三种上传方式。"""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_REQUEST_BODY_BYTES:
                raise _PayloadTooLargeError("上传请求体积过大，请上传 10MB 以内的图片")
        except ValueError as exc:
            if isinstance(exc, _PayloadTooLargeError):
                raise

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        raw_body = await request.body()
        if len(raw_body) > _MAX_REQUEST_BODY_BYTES:
            raise _PayloadTooLargeError("上传请求体积过大，请上传 10MB 以内的图片")
        try:
            import json
            body = json.loads(raw_body)
        except Exception as exc:
            raise ValueError("JSON 请求体格式不正确") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON 请求体格式不正确")
        b64_str = str(body.get("image_base64") or body.get("image") or "").strip()
        if not b64_str:
            raise ValueError("请提供二维码图片数据 (image_base64)")
        if "," in b64_str and b64_str.startswith("data:"):
            b64_str = b64_str.split(",", 1)[1]
        owner_token = str(body.get("owner_token") or "").strip()[:64]
        share_text = str(body.get("share_text") or "").strip()[:500]
        try:
            return base64.b64decode(b64_str), owner_token, share_text
        except Exception as exc:
            raise ValueError("图片 Base64 编码格式无效") from exc

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file") or form.get("image")
        if upload is None or not hasattr(upload, "read"):
            raise ValueError("请在表单 file 字段中上传二维码分享图片")
        owner_token = str(form.get("owner_token") or "").strip()[:64]
        share_text = str(form.get("share_text") or "").strip()[:500]
        data = await upload.read(_MAX_REQUEST_BODY_BYTES + 1)
        if len(data) > _MAX_REQUEST_BODY_BYTES:
            raise _PayloadTooLargeError("上传请求体积过大，请上传 10MB 以内的图片")
        return data, owner_token, share_text

    raw = await request.body()
    if not raw:
        raise ValueError("未检测到上传的图片内容")
    if len(raw) > _MAX_REQUEST_BODY_BYTES:
        raise _PayloadTooLargeError("上传请求体积过大，请上传 10MB 以内的图片")
    return raw, "", ""


@router.post("/upload")
async def upload_group_poster(request: Request):
    """
    上传拼多多多人团二维码分享图片：
    立即在受控工作线程解码二维码 -> 免登录核验 307 重定向提取 group_order_id -> 签发团长口令并写入待解析队列。
    """
    try:
        uid, _ = extract_viewer_auth_soft(request)
        image_bytes, owner_token, share_text = await _extract_upload_payload(request)
        async with _UPLOAD_SEMAPHORE:
            return await asyncio.to_thread(
                group_help_service.submit_poster_image,
                image_bytes,
                owner_token=owner_token,
                viewer_uid=uid,
                share_text=share_text,
            )
    except GroupHelpPermissionError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "error_code": exc.error_code,
                "error_msg": str(exc),
            },
        )
    except _PayloadTooLargeError as exc:
        return JSONResponse(
            status_code=413,
            content={
                "status": "error",
                "error_code": "payload_too_large",
                "error_msg": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "error_code": "invalid_poster",
                "error_msg": str(exc),
            },
        )


@router.post("/submit_text")
async def submit_group_text(request: Request, payload: GroupTextSubmitRequest):
    """无图发团：直接提交拼多多 ⇥...⇤ 口令或官方拼团链接，结构化核验后立即写入大厅展示。"""
    uid, _ = extract_viewer_auth_soft(request)
    client_ip = (
        (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    try:
        return await asyncio.to_thread(
            group_help_service.submit_text_group,
            share_text=payload.share_text,
            goods_name=payload.goods_name,
            group_price=payload.group_price,
            original_price=payload.original_price,
            total_people=payload.total_people,
            remain_people=payload.remain_people,
            publisher_nick=str(payload.publisher_nick or ""),
            owner_token=str(payload.owner_token or "").strip(),
            viewer_uid=uid,
            client_ip=client_ip,
        )
    except GroupHelpPermissionError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "error_code": exc.error_code,
                "error_msg": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "error_code": "invalid_group_text",
                "error_msg": str(exc),
            },
        )


@router.get("/list")
def list_groups(
    request: Request,
    keyword: str = Query("", max_length=120, description="搜索商品名称、关键词或团单号"),
    filter_mode: str = Query("active", pattern="^(active|almost|all)$", description="筛选模式: active|almost|all"),
    offset: int = Query(0, ge=0, le=2000, description="分页偏移量"),
    limit: int = Query(20, ge=1, le=50, description="每页条数"),
):
    """获取多人团列表（仅展示已经过大模型解析出商品信息的团，同时返回当前用户下架权限状态）。"""
    uid, token_expired = extract_viewer_auth_soft(request)
    data = group_help_service.list_groups(
        keyword=keyword,
        filter_mode=filter_mode,
        offset=offset,
        limit=limit,
        viewer_uid=uid,
        token_expired=token_expired,
    )
    return {"status": "success", "data": data}


@router.get("/detail/{group_order_id}")
def get_group_detail(
    group_order_id: str = Path(..., min_length=10, max_length=36, pattern=GROUP_ID_PATH_PATTERN),
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
    group_order_id: str = Path(..., min_length=10, max_length=36, pattern=GROUP_ID_PATH_PATTERN),
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
    request: Request,
    payload: GroupFeedbackRequest,
    group_order_id: str = Path(..., min_length=10, max_length=36, pattern=GROUP_ID_PATH_PATTERN),
):
    """
    用户共创维护多人团状态（极简防恶意下架）：
    - mark_full / mark_expired / 差1人时 joined_one: 非团长需登录且受下架权限管控
    - 差>=2人时 joined_one: 免登录可用，被禁下架权限用户仍可使用
    - 团长本人恢复上架 set_remain>=1: 自动对错误下架该团的非团长账号计数 +1（满 2 次停用下架权限）
    """
    uid, token_expired = extract_viewer_auth_soft(request)
    try:
        res = group_help_service.submit_feedback_detailed(
            group_order_id=group_order_id,
            action=payload.action,
            remain_people=payload.remain_people,
            viewer_uid=uid,
            owner_token=str(payload.owner_token or "").strip(),
        )
    except GroupHelpAuthRequiredError as exc:
        return JSONResponse(
            status_code=401,
            content={
                "status": "error",
                "error_code": exc.error_code,
                "token_expired": token_expired,
                "error_msg": str(exc),
            },
        )
    except GroupHelpPermissionError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "error_code": exc.error_code,
                "error_msg": str(exc),
                "viewer_guard": group_help_service.get_viewer_guard(
                    viewer_uid=uid, token_expired=token_expired
                ),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error_code": "invalid_feedback", "error_msg": str(exc)},
        )

    if not res:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "not_found", "error_msg": "未找到该拼团记录"},
        )
    msg = (
        "已恢复上架大厅展示！已自动对错误下架该团的账号执行违规限制"
        if res.get("penalized_saboteur")
        else "感谢您的共创反馈，拼团状态已实时更新！"
    )
    return {
        "status": "success",
        "message": msg,
        "penalized_saboteur": bool(res.get("penalized_saboteur")),
        "viewer_guard": res.get("viewer_guard"),
        "data": res["data"],
    }


@router.get("/poster/{group_order_id}")
def get_group_poster(
    group_order_id: str = Path(..., min_length=10, max_length=36, pattern=GROUP_ID_PATH_PATTERN),
):
    """获取多人团分享海报图片。"""
    path = group_help_service.get_poster_file_path(group_order_id)
    if not path:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error_code": "poster_not_found", "error_msg": "未找到海报图片"},
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
