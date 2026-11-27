# -*- coding: utf-8 -*-
"""闲鱼选品工作台独立 FastAPI 应用装配与 HTTP 接口。"""
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
from .config import WORKBENCH_DIR, WorkbenchConfig, load_config
from .repo import WorkbenchRepository
from .service import WorkbenchService

STATIC_DIR = WORKBENCH_DIR / "static"
_SCREENSHOT_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}\.jpg$")
_UPLOAD_FILE_RE = re.compile(r"^upl_[0-9a-f]{16}\.jpg$")
_DEAL_IMAGE_ID_RE = re.compile(r"^dsi_[0-9a-f]{20}$")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class BindingUpdateRequest(BaseModel):
    shop_uid: str = Field("", max_length=64)
    shop_phone: str = Field("", max_length=32)


class RejectCandidateRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32)
    product_id: str = Field(..., min_length=1, max_length=128)
    source_origin: str = Field("xianyu_eval", max_length=32)


class DraftCreateRequest(BaseModel):
    platform: str = Field(..., min_length=1, max_length=32)
    product_id: str = Field(..., min_length=1, max_length=128)
    source_origin: str = Field("favorite", max_length=32)


class ListingUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=4000)
    images: Optional[List[Any]] = None
    sell_price: Optional[float] = None
    shipping_fee: Optional[float] = None
    max_cost_price: Optional[float] = None
    status: Optional[str] = Field(None, max_length=32)
    external_item_id: Optional[str] = Field(None, max_length=64)
    offline_reason: Optional[str] = Field(None, max_length=128)


class GenerateCopyRequest(BaseModel):
    style: str = Field("casual", max_length=32)


class UploadImageRequest(BaseModel):
    image_base64: str = Field(..., min_length=8)


def create_app(
    config: Optional[WorkbenchConfig] = None,
    *,
    db: Any = None,
    service: Optional[WorkbenchService] = None,
) -> FastAPI:
    cfg = config or load_config()
    state: Dict[str, Any] = {"service": service, "close_db": None}

    if state["service"] is None and db is not None:
        repo = WorkbenchRepository(db)
        repo.ensure_indexes()
        state["service"] = WorkbenchService(repo, cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["service"] is None:
            mongo_db, close_fn = open_mongo_connection(cfg)
            state["close_db"] = close_fn
            repo = WorkbenchRepository(mongo_db)
            repo.ensure_indexes()
            state["service"] = WorkbenchService(repo, cfg)
        try:
            yield
        finally:
            close_fn = state.get("close_db")
            state["close_db"] = None
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    app = FastAPI(title="闲鱼选品与上架工作台", lifespan=lifespan)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="workbench_static")

    def _svc() -> WorkbenchService:
        svc = state.get("service")
        if svc is None:
            raise HTTPException(status_code=503, detail="工作台服务尚未就绪")
        return svc

    def _uid(request: Request) -> str:
        return require_current_username(request, cfg.auth_secret)

    # =========================================================================
    # 独立鉴权与主站 UID 绑定
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
    # 模块一：候选选品池
    # =========================================================================
    @app.get("/api/candidates/favorites")
    def get_favorite_candidates(
        request: Request,
        page: int = Query(1, ge=1, le=500),
        page_size: int = Query(20, ge=1, le=100),
        include_handled: bool = Query(False),
        include_off: bool = Query(False),
    ):
        username = _uid(request)
        data = _svc().list_favorite_candidates(
            username,
            page=page,
            page_size=page_size,
            include_handled=include_handled,
            include_off=include_off,
        )
        return {"status": "success", "data": data}

    @app.get("/api/candidates/evaluations")
    def get_evaluation_candidates(
        request: Request,
        winner: str = Query("my_product", pattern="^(my_product|all)$"),
        source_type: str = Query("all", pattern="^(all|official|deal_tip)$"),
        min_ratio: float = Query(1.0, ge=0.0, le=100.0),
        sort: str = Query("ratio", pattern="^(ratio|latest)$"),
        include_handled: bool = Query(False),
        include_off: bool = Query(False),
        page: int = Query(1, ge=1, le=500),
        page_size: int = Query(20, ge=1, le=100),
    ):
        username = _uid(request)
        data = _svc().list_evaluation_candidates(
            username,
            winner=winner,
            source_type=source_type,
            min_ratio=min_ratio,
            sort=sort,
            include_handled=include_handled,
            include_off=include_off,
            page=page,
            page_size=page_size,
        )
        return {"status": "success", "data": data}

    @app.post("/api/candidates/reject")
    def reject_candidate(req: RejectCandidateRequest, request: Request):
        username = _uid(request)
        data = _svc().reject_candidate(
            username,
            platform=req.platform,
            product_id=req.product_id,
            source_origin=req.source_origin,
        )
        return {"status": "success", "data": {"listing_id": data.get("listing_id"), "status": data.get("status")}}

    # =========================================================================
    # 模块二 & 三：预览编辑、AI 文案与标准上架库
    # =========================================================================
    @app.post("/api/listings/draft")
    def create_or_open_draft(req: DraftCreateRequest, request: Request):
        username = _uid(request)
        data = _svc().create_or_open_draft(
            username,
            platform=req.platform,
            product_id=req.product_id,
            source_origin=req.source_origin,
        )
        return {"status": "success", "data": data}

    @app.get("/api/listings")
    def list_listings(
        request: Request,
        status: str = Query("ready", pattern="^(draft|ready|online|offline|rejected|all)$"),
        page: int = Query(1, ge=1, le=500),
        page_size: int = Query(20, ge=1, le=100),
    ):
        username = _uid(request)
        data = _svc().list_listings(username, status=status, page=page, page_size=page_size)
        return {"status": "success", "data": data}

    @app.get("/api/listings/{listing_id}")
    def get_listing_detail(listing_id: str, request: Request):
        username = _uid(request)
        data = _svc().get_listing_detail(username, listing_id)
        return {"status": "success", "data": data}

    @app.patch("/api/listings/{listing_id}")
    def update_listing(listing_id: str, req: ListingUpdateRequest, request: Request):
        username = _uid(request)
        data = _svc().update_listing(username, listing_id, req.model_dump(exclude_unset=True))
        return {"status": "success", "data": data}

    @app.post("/api/listings/{listing_id}/generate-copy")
    def generate_copy(listing_id: str, req: GenerateCopyRequest, request: Request):
        username = _uid(request)
        data = _svc().generate_copy(username, listing_id, style=req.style)
        return {"status": "success", "data": data}

    # =========================================================================
    # 图片上传与受保护截图/图片读取
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
        return HTMLResponse("<h3>闲鱼工作台前端页面未就绪</h3>", status_code=200)

    return app
