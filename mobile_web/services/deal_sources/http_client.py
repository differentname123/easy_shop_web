# -*- coding: utf-8 -*-
"""
线报采集专属轻量 HTTP 客户端工厂与瞬断单次重试工具。
================================================================================
- 按站点线程创建独立 `requests.Session`，彻底消除多线程共享 Session 竞争；
- 国内直连站 (`network_mode="direct"`) 强制 `trust_env=False`，避免 Windows 系统代理劫持；
- 海外源如 Telegram (`network_mode="proxy"`) 启用 `trust_env=True` 自动走本地代理；
- 老旧站点（如发现值得买/牛杂网）支持 `close_connection=True` (`Connection: close`)，
  并对瞬时 `ConnectionError` / `ChunkedEncodingError` 做 1 次退避重试（绝不重试 4xx/5xx/验证码）。
================================================================================
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import requests

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

RATE_LIMIT_STATUSES: Tuple[int, ...] = (403, 429, 502, 503, 504)


def is_rate_limit_status(status_code: Optional[int]) -> bool:
    return status_code in RATE_LIMIT_STATUSES


def create_site_session(
    *,
    network_mode: str = "direct",
    close_connection: bool = False,
    proxy: Optional[str] = None,
) -> requests.Session:
    """为单个站点车道创建线程隔离的 HTTP Session。"""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    if close_connection:
        session.headers["Connection"] = "close"

    if proxy:
        session.trust_env = False
        session.proxies.update({"http": proxy, "https": proxy})
    else:
        session.trust_env = (str(network_mode or "direct").strip().lower() == "proxy")
    return session


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 1,
    backoff: float = 0.4,
    **kwargs: Any,
) -> requests.Response:
    """仅对网络瞬断（ConnectionError / ChunkedEncodingError）重试最多 `retries` 次。"""
    attempts = max(1, 1 + int(retries))
    last_exc: Optional[Exception] = None
    req_fn = getattr(session, method.lower())
    for attempt in range(attempts):
        try:
            return req_fn(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                if backoff > 0:
                    time.sleep(backoff)
                continue
            raise
    assert last_exc is not None
    raise last_exc
