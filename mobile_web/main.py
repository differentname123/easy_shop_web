# -*- coding: utf-8 -*-
"""移动端服务装配入口：HTTP 基础设施、路由与静态页面。"""
if __name__ == "__main__":
    from run import main
    raise SystemExit(main())

import asyncio
import os
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from infrastructure.logging_utils import configure_logging, format_exc_summary
from infrastructure.pdd_client import configure_transport
from services.detail_prefetch import DetailPrefetcher

from config import logger, settings
from adapters import get_adapter, get_all_platforms, PlatformUnavailableError
from api.home import router as home_router
from api.goods import router as goods_router
from api.user import router as user_router
from api.promote import router as promote_router
from api.order import router as order_router
from api.group_help import router as group_help_router, group_help_worker

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
GROUP_HELP_ENABLED = True  # 多人团互助独立模块总开关，置为 False 即可一键下线


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings)
    configure_transport(dns_mode=settings.pdd_dns_mode, trust_env=settings.pdd_trust_env)

    from api.home import product_service
    from api.goods import product_service as goods_service

    services = tuple(dict.fromkeys((product_service, goods_service)))
    for service in services:
        start_writes = getattr(service, "start_background_writes", None)
        if callable(start_writes):
            start_writes()
    prefetcher = None
    warmup_thread = None
    warmup_stop = threading.Event()
    app.state.detail_prefetcher = None

    def warm_connections():
        # 预取关闭时保留旧的首榜预热，但线程及其摘要写入必须随 lifespan 回收。
        for platform in get_all_platforms():
            if warmup_stop.is_set():
                return
            if not platform["active"]:
                continue
            adapter = None
            try:
                adapter = get_adapter(platform["code"])
                adapter.warmup()
            except Exception as exc:
                logger.debug(
                    "【系统预热】平台[%s] 预热连接与索引未就绪 | 异常<%s> 排查方向<请检查MongoDB或上游网关连通性>",
                    platform["code"], format_exc_summary(exc),
                )
            if adapter is None:
                continue
            if warmup_stop.is_set():
                return
            if platform.get("capabilities", {}).get("recommend", True):
                try:
                    if adapter.channels:
                        product_service.get_home_recommendations(
                            platform=platform["code"], channel_type=adapter.channels[0]["channel_type"],
                            offset=0, limit=20,
                        )
                except Exception as exc:
                    logger.warning(
                        "❌ 【系统预热异常】正在预热平台首页推荐流时发生异常 | 平台[%s] 异常<%s> 排查方向<请检查平台配置、上游接口连通性或数据库状态>",
                        platform["code"], format_exc_summary(exc),
                    )

    try:
        if settings.detail_prefetch_enabled:
            prefetcher = DetailPrefetcher(
                product_service, seed_enabled=settings.warmup_enabled,
                interval_seconds=settings.detail_prefetch_interval_seconds,
                platform_provider=get_all_platforms, adapter_provider=get_adapter,
            )
            for service in services:
                service.prefetcher = prefetcher
            app.state.detail_prefetcher = prefetcher
            prefetcher.start()
        elif settings.warmup_enabled:
            warmup_thread = threading.Thread(target=warm_connections, name="platform-warmup", daemon=False)
            warmup_thread.start()
        if GROUP_HELP_ENABLED and (settings.detail_prefetch_enabled or settings.warmup_enabled):
            group_help_worker.start()
        yield
    finally:
        warmup_stop.set()
        if GROUP_HELP_ENABLED:
            await asyncio.to_thread(group_help_worker.stop)
        if prefetcher is not None:
            # stop 先封 admission/set Event/丢排队项，再完整 join；同步 I/O 由自身超时返回。
            await asyncio.to_thread(prefetcher.stop)
        if warmup_thread is not None:
            await asyncio.to_thread(warmup_thread.join)
        for service in services:
            await asyncio.to_thread(service.wait_for_background_writes)
        # 在 worker 和兼容摘要线程真正退出之前，不得解除绑定导致请求回退到旧线程路径。
        if prefetcher is not None:
            for service in services:
                if service.prefetcher is prefetcher:
                    service.prefetcher = None
        app.state.detail_prefetcher = None


def _request_path(request) -> str:
    """保留中文查询日志可读性，但不记录兼容查询参数中的认证凭据。"""
    sensitive = {"token", "auth_token", "access_token", "password", "authorization"}
    pairs = urllib.parse.parse_qsl(request.url.query, keep_blank_values=True)
    query = "&".join(f"{key}={'[REDACTED]' if key.lower() in sensitive else value}" for key, value in pairs)
    return (request.url.path + ("?" + query if query else "")).replace("\n", "\\n").replace("\r", "\\r")


def _html_page(filename: str, fallback: str):
    path = os.path.join(STATIC_DIR, filename)
    if os.path.isfile(path):
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse(f"<h2>{fallback}</h2>")


def create_app() -> FastAPI:
    application = FastAPI(
        title="移动端电商导购与返利服务",
        description="官方推荐、爆单库、推手授权与带 UID 佣金转链",
        version="2.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                               allow_methods=["*"], allow_headers=["*"])
    application.add_middleware(GZipMiddleware, minimum_size=500)

    @application.exception_handler(PlatformUnavailableError)
    async def unavailable_platform(request, exc):
        return JSONResponse(status_code=400, content={
            "status": "error", "error_code": "platform_unavailable", "error_msg": str(exc),
        })

    @application.middleware("http")
    async def request_log_middleware(request, call_next):
        started = time.perf_counter()
        forwarded = request.headers.get("x-forwarded-for")
        client = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "127.0.0.1")
        req_path = _request_path(request)
        path_only = request.url.path
        is_noisy_asset = path_only.startswith("/static/") or path_only in (
            "/favicon.ico", "/apple-touch-icon.png", "/manifest.json", "/health",
        )
        try:
            response = await call_next(request)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            logger.error(
                "❌ 【HTTP请求异常】处理客户端请求时发生未捕获异常 | 客户端[%s] 接口[%s %s] 耗时[%.1fms] 异常<%s> 排查方向<请结合上方业务日志排查对应接口或下游依赖>",
                client, request.method, req_path, duration, format_exc_summary(exc),
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        status_code = response.status_code
        if status_code >= 500:
            logger.error(
                "❌ 【HTTP请求异常】接口返回服务端错误 | 客户端[%s] 接口[%s %s] 状态[%d] 耗时[%.1fms] 排查方向<请检查服务端异常日志或下游依赖状态>",
                client, request.method, req_path, status_code, duration,
            )
        elif status_code >= 400:
            logger.warning(
                "【HTTP请求告警】接口返回客户端或鉴权错误 | 客户端[%s] 接口[%s %s] 状态[%d] 耗时[%.1fms] 排查方向<请检查请求参数、登录Token或访问路径>",
                client, request.method, req_path, status_code, duration,
            )
        elif is_noisy_asset and duration < 1000:
            logger.debug(
                "【静态资源】客户端[%s] 接口[%s %s] 状态[%d] 耗时[%.1fms]",
                client, request.method, req_path, status_code, duration,
            )
        elif duration >= 3000:
            logger.warning(
                "【HTTP慢请求】接口响应耗时过长 | 客户端[%s] 接口[%s %s] 状态[%d] 耗时[%.1fms] 排查方向<可能发生上游网关慢响应或重试，请结合业务日志排查>",
                client, request.method, req_path, status_code, duration,
            )
        else:
            logger.info(
                "【HTTP请求】客户端[%s] 接口[%s %s] 状态[%d] 耗时[%.1fms]",
                client, request.method, req_path, status_code, duration,
            )
        return response

    for router in (home_router, goods_router, user_router, promote_router, order_router):
        application.include_router(router)
    if GROUP_HELP_ENABLED:
        application.include_router(group_help_router)
    if os.path.isdir(STATIC_DIR):
        application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/favicon.ico")
    def favicon():
        icon_path = os.path.join(STATIC_DIR, "icons", "icon-192.png")
        if os.path.isfile(icon_path):
            return FileResponse(icon_path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        return Response(content=b"", media_type="image/x-icon")

    @application.get("/apple-touch-icon.png")
    def apple_touch_icon():
        icon_path = os.path.join(STATIC_DIR, "icons", "apple-touch-icon.png")
        if os.path.isfile(icon_path):
            return FileResponse(icon_path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        return Response(content=b"", media_type="image/png")

    @application.get("/manifest.json")
    def web_manifest():
        manifest_path = os.path.join(STATIC_DIR, "manifest.json")
        if os.path.isfile(manifest_path):
            return FileResponse(manifest_path, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})
        return JSONResponse(content={})

    @application.get("/", response_class=HTMLResponse)
    def index_page():
        return _html_page("index.html", "移动端页面正在构建中...")

    @application.get("/detail", response_class=HTMLResponse)
    def detail_page():
        return _html_page("detail.html", "商品详情页构建中...")

    if GROUP_HELP_ENABLED:
        @application.get("/group_help", response_class=HTMLResponse)
        def group_help_page():
            return _html_page("group_help.html", "多人团互助页面正在构建中...")

    @application.get("/health")
    def health_check():
        return {"status": "ok", "service": "mobile_web_service"}

    return application


app = create_app()
