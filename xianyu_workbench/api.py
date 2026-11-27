# -*- coding: utf-8 -*-
"""好价帮拍工作台 FastAPI 应用：平台无关核心接口 + 可插拔销售渠道接口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, require_current_username
from .bridge import open_mongo_connection
from .channel_service import ChannelService
from .channels import ChannelRegistry, build_default_channel_registry
from .config import WORKBENCH_DIR, WorkbenchConfig, load_config
from .repo import WorkbenchRepository
from .service import WorkbenchService

STATIC_DIR = WORKBENCH_DIR / "static"
_SCREENSHOT_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}\.jpg$")
_UPLOAD_FILE_RE = re.compile(r"^upl_[0-9a-f]{16}\.jpg$")
_PUBFAIL_FILE_RE = re.compile(r"^pubfail_\d+_[0-9a-f]{8}\.jpg$")
_DEAL_IMAGE_ID_RE = re.compile(r"^dsi_[0-9a-f]{20}$")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class BindingUpdateRequest(BaseModel):
    shop_uid: str = Field("", max_length=64)
    shop_phone: str = Field("", max_length=32)


class DismissCandidateRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32)
    product_id: str = Field(..., min_length=1, max_length=128)
    source_origin: str = Field("xianyu_eval", max_length=32)


class DraftCreateRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32)
    product_id: str = Field(..., min_length=1, max_length=128)
    source_origin: str = Field("favorite", max_length=32)


class ItemUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=4000)
    images: Optional[List[Any]] = None
    price: Optional[float] = None
    status: Optional[str] = Field(None, max_length=32)


class LinkSellingRequest(BaseModel):
    listing_id: str = Field("", max_length=64)


class PublishRequest(BaseModel):
    listing_id: str = Field(..., min_length=1, max_length=64)


class DownshelfRequest(BaseModel):
    external_item_id: str = Field(..., min_length=1, max_length=64)


class ReconcileApplyRequest(BaseModel):
    publish_listing_ids: List[str] = Field(default_factory=list)
    downshelf_item_ids: List[str] = Field(default_factory=list)


class CancelQrRequest(BaseModel):
    session_id: Optional[str] = Field(None, max_length=64)


class UploadImageRequest(BaseModel):
    image_base64: str = Field(..., min_length=8)


def create_app(
    config: Optional[WorkbenchConfig] = None,
    *,
    db: Any = None,
    service: Optional[WorkbenchService] = None,
    channel_registry: Optional[ChannelRegistry] = None,
    channel_service: Optional[ChannelService] = None,
) -> FastAPI:
    cfg = config or load_config()
    registry = channel_registry or build_default_channel_registry(cfg)
    state: Dict[str, Any] = {
        "service": service,
        "channel_service": channel_service,
        "close_db": None,
    }

    if db is not None:
        repo = WorkbenchRepository(db)
        repo.ensure_indexes()
        if state["service"] is None:
            state["service"] = WorkbenchService(repo, cfg)
        if state["channel_service"] is None:
            state["channel_service"] = ChannelService(repo, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["service"] is None or state["channel_service"] is None:
            mongo_db, close_fn = open_mongo_connection(cfg)
            state["close_db"] = close_fn
            repo = WorkbenchRepository(mongo_db)
            repo.ensure_indexes()
            if state["service"] is None:
                state["service"] = WorkbenchService(repo, cfg)
            if state["channel_service"] is None:
                state["channel_service"] = ChannelService(repo, registry)
        try:
            yield
        finally:
            try:
                registry.shutdown()
            except Exception:
                pass
            close_fn = state.get("close_db")
            state["close_db"] = None
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    app = FastAPI(title="帮拍选品工作台", lifespan=lifespan)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="workbench_static")

    def _svc() -> WorkbenchService:
        svc = state.get("service")
        if svc is None:
            raise HTTPException(status_code=503, detail="工作台服务尚未就绪")
        return svc

    def _ch_svc() -> ChannelService:
        csvc = state.get("channel_service")
        if csvc is None:
            raise HTTPException(status_code=503, detail="渠道服务尚未就绪")
        return csvc

    def _uid(request: Request) -> str:
        return require_current_username(request, cfg.auth_secret)

    # =========================================================================
    # 一、账号登录与主站收藏绑定
    # =========================================================================
    @app.post("/api/auth/login")
    def login(req: LoginRequest, response: Response):
        data = _svc().authenticate(req.username, req.password)
        response.set_cookie(
            key=COOKIE_NAME,
            value=data["token"],
            max_age=cfg.token_ttl_seconds,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return {"status": "success", "data": data}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(key=COOKIE_NAME, path="/")
        return {"status": "success", "data": {"logged_out": True}}

    @app.get("/api/auth/me")
    def get_me(request: Request):
        username = _uid(request)
        return {"status": "success", "data": _svc().get_me(username)}

    @app.patch("/api/auth/binding")
    def update_binding(req: BindingUpdateRequest, request: Request):
        username = _uid(request)
        data = _svc().update_binding(username, shop_uid=req.shop_uid, shop_phone=req.shop_phone)
        return {"status": "success", "data": data}

    # =========================================================================
    # 二、挑品候选池（比价好货 & 我的收藏）
    # =========================================================================
    @app.get("/api/candidates/favorites")
    def get_favorite_candidates(
        request: Request,
        page: int = Query(1, ge=1, le=500),
        page_size: int = Query(20, ge=1, le=100),
        include_off: bool = Query(False),
    ):
        username = _uid(request)
        data = _svc().list_favorite_candidates(
            username,
            page=page,
            page_size=page_size,
            include_off=include_off,
        )
        return {"status": "success", "data": data}

    @app.get("/api/candidates/evaluations")
    def get_evaluation_candidates(
        request: Request,
        source_type: str = Query("all", pattern="^(all|deal_tip|official)$"),
        sort_by: str = Query("want", pattern="^(want|ratio)$"),
        include_all_winners: bool = Query(False),
        include_off: bool = Query(False),
        page: int = Query(1, ge=1, le=500),
        page_size: int = Query(20, ge=1, le=100),
    ):
        username = _uid(request)
        data = _svc().list_evaluation_candidates(
            username,
            source_type=source_type,
            sort_by=sort_by,
            include_all_winners=include_all_winners,
            include_off=include_off,
            page=page,
            page_size=page_size,
        )
        return {"status": "success", "data": data}

    @app.post("/api/candidates/dismiss")
    def dismiss_candidate(req: DismissCandidateRequest, request: Request):
        username = _uid(request)
        data = _svc().dismiss_candidate(
            username,
            platform=req.platform,
            product_id=req.product_id,
            source_origin=req.source_origin,
        )
        return {"status": "success", "data": data}

    # =========================================================================
    # 三、通用商品素材库（平台无关）
    # =========================================================================
    @app.post("/api/items/draft")
    def create_or_open_draft(req: DraftCreateRequest, request: Request):
        username = _uid(request)
        data = _svc().create_or_open_draft(
            username,
            platform=req.platform,
            product_id=req.product_id,
            source_origin=req.source_origin,
        )
        return {"status": "success", "data": data}

    @app.get("/api/items")
    def list_items(
        request: Request,
        status: str = Query("active", pattern="^(active|ready|draft|dismissed|all)$"),
        page: int = Query(1, ge=1, le=500),
        page_size: int = Query(30, ge=1, le=100),
    ):
        username = _uid(request)
        data = _svc().list_items(username, status=status, page=page, page_size=page_size)
        return {"status": "success", "data": data}

    @app.get("/api/items/{listing_id}")
    def get_item_detail(listing_id: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _svc().get_item_detail(username, listing_id)}

    @app.patch("/api/items/{listing_id}")
    def update_item(listing_id: str, req: ItemUpdateRequest, request: Request):
        username = _uid(request)
        data = _svc().update_item(username, listing_id, req.model_dump(exclude_unset=True))
        return {"status": "success", "data": data}

    @app.post("/api/items/{listing_id}/generate-copy")
    def generate_copy(listing_id: str, request: Request):
        username = _uid(request)
        data = _svc().generate_copy(username, listing_id)
        return {"status": "success", "data": data}

    @app.post("/api/items/{listing_id}/failure/clear")
    def clear_item_failure(listing_id: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _svc().clear_item_failure(username, listing_id)}

    # =========================================================================
    # 四、可插拔销售渠道（扫码绑定、状态检测、真实在售同步与对账）
    # =========================================================================
    @app.get("/api/channels")
    def list_channels(request: Request):
        username = _uid(request)
        return {"status": "success", "data": {"channels": _ch_svc().list_channels(username)}}

    @app.post("/api/channels/{channel}/login/start")
    def start_channel_login(channel: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().start_qr_login(username, channel)}

    @app.get("/api/channels/{channel}/login/status/{session_id}")
    def get_channel_login_status(channel: str, session_id: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().get_qr_login_status(username, channel, session_id)}

    @app.get("/api/channels/{channel}/login/qr/{session_id}")
    def get_channel_login_qr(channel: str, session_id: str, request: Request):
        username = _uid(request)
        png = _ch_svc().get_qr_image_bytes(username, channel, session_id)
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.post("/api/channels/{channel}/login/cancel")
    def cancel_channel_login(channel: str, request: Request, req: Optional[CancelQrRequest] = None):
        username = _uid(request)
        sid = req.session_id if req else None
        return {"status": "success", "data": _ch_svc().cancel_qr_login(username, channel, sid)}

    @app.post("/api/channels/{channel}/status")
    def check_channel_status(channel: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().check_account_status(username, channel)}

    @app.post("/api/channels/{channel}/sync")
    def sync_channel_selling(channel: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().sync_selling(username, channel)}

    @app.get("/api/channels/{channel}/selling")
    def get_channel_selling(channel: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().get_selling_snapshot(username, channel)}

    @app.post("/api/channels/{channel}/selling/{external_item_id}/link")
    def link_selling_item(channel: str, external_item_id: str, req: LinkSellingRequest, request: Request):
        username = _uid(request)
        data = _ch_svc().link_selling_item(username, channel, external_item_id, req.listing_id)
        return {"status": "success", "data": data}

    @app.post("/api/channels/{channel}/publish")
    def publish_channel_item(channel: str, req: PublishRequest, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().publish_item(username, channel, req.listing_id)}

    @app.post("/api/channels/{channel}/downshelf")
    def downshelf_channel_item(channel: str, req: DownshelfRequest, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().downshelf_item(username, channel, req.external_item_id)}

    @app.post("/api/channels/{channel}/reconcile/preview")
    def preview_channel_reconcile(channel: str, request: Request):
        username = _uid(request)
        return {"status": "success", "data": _ch_svc().preview_reconcile(username, channel)}

    @app.post("/api/channels/{channel}/reconcile/apply")
    def apply_channel_reconcile(channel: str, req: ReconcileApplyRequest, request: Request):
        username = _uid(request)
        data = _ch_svc().apply_reconcile(
            username,
            channel,
            publish_listing_ids=req.publish_listing_ids,
            downshelf_item_ids=req.downshelf_item_ids,
        )
        return {"status": "success", "data": data}

    # =========================================================================
    # 五、图片上传与受保护媒体读取
    # =========================================================================
    @app.post("/api/assets/upload")
    def upload_image(req: UploadImageRequest, request: Request):
        _uid(request)
        url = _svc().save_uploaded_image_base64(req.image_base64)
        return {"status": "success", "data": {"url": url}}

    @app.get("/api/assets/screenshots/{filename}")
    def get_screenshot(filename: str, request: Request):
        _uid(request)
        path = WorkbenchService.resolve_safe_file(cfg.screenshot_dir, filename, _SCREENSHOT_FILE_RE)
        if path is None:
            raise HTTPException(status_code=404, detail="截图不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/assets/uploads/{filename}")
    def get_uploaded_image(filename: str, request: Request):
        _uid(request)
        path = WorkbenchService.resolve_safe_file(cfg.upload_dir, filename, _UPLOAD_FILE_RE)
        if path is None:
            raise HTTPException(status_code=404, detail="图片不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/assets/publish-failures/{filename}")
    def get_publish_failure_screenshot(filename: str, request: Request):
        _uid(request)
        path = WorkbenchService.resolve_safe_file(cfg.publish_fail_dir, filename, _PUBFAIL_FILE_RE)
        if path is None:
            raise HTTPException(status_code=404, detail="失败截图不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/deals/image/{image_id}")
    def get_deal_image(image_id: str, request: Request):
        _uid(request)
        clean_id = str(image_id or "").strip()
        if not _DEAL_IMAGE_ID_RE.fullmatch(clean_id):
            raise HTTPException(status_code=404, detail="爆料图不存在")
        base_dir = cfg.deal_image_dir.resolve()
        candidate = (base_dir / f"{clean_id}.jpg").resolve()
        if candidate.parent != base_dir or not candidate.is_file():
            raise HTTPException(status_code=404, detail="爆料图不存在")
        return FileResponse(candidate, media_type="image/jpeg")

    @app.get("/", response_class=HTMLResponse)
    def workbench_page():
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h3>选品工作台页面未就绪</h3>", status_code=200)

    return app
