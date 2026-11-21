# -*- coding: utf-8 -*-
"""统一 HTTP Session 连接池、系统代理策略 (trust_env) 与 Windows DNS 防卡死缓存基础设施。"""
from __future__ import annotations

import socket
import sys
import threading
import time
import weakref
from typing import Dict, Iterable, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

from infrastructure.logging_utils import get_app_logger

logger = get_app_logger("transport")

_REAL_STDLIB_GETADDRINFO = socket.getaddrinfo
_ORIG_GETADDRINFO = socket.getaddrinfo

_PDD_FALLBACK_IPS = ("101.226.73.254", "58.217.134.20", "101.226.73.255", "58.217.134.19")
_HDK_FALLBACK_IPS = ("139.200.130.76", "113.125.206.117", "139.200.130.199")
_GATEWAY_FALLBACK_IPS: Dict[str, Tuple[str, ...]] = {
    "gw-api.pinduoduo.com": _PDD_FALLBACK_IPS,
    "eco.taobao.com": ("59.82.121.73",),
    "gw.api.taobao.com": ("59.82.23.84",),
    "api.zhetaoke.com": ("111.198.53.170",),
    "api.jd.com": ("106.39.169.86",),
    "v2.api.haodanku.com": _HDK_FALLBACK_IPS,
    "v3.api.haodanku.com": _HDK_FALLBACK_IPS,
}

_DNS_CACHE: Dict[str, Tuple[float, list]] = {}
_DNS_REFRESHING: set[str] = set()
_DNS_LOCK = threading.Lock()
_TRANSPORT_LOCK = threading.Lock()
_DNS_ENABLED = False
_DNS_INSTALLED = False
_DNS_REAL_RESOLVE_TIMEOUT_SEC = 1.5
_DNS_PROACTIVE_REFRESH_WINDOW_SEC = 90.0

_TRACKED_SESSIONS: "weakref.WeakSet[requests.Session]" = weakref.WeakSet()


def _is_windows() -> bool:
    return sys.platform == "win32"


_TRUST_ENV_OVERRIDE: Optional[bool] = None


def default_trust_env() -> bool:
    """Windows 下默认不读取系统代理避免国内联盟接口被本地代理劫持；Linux 默认遵循环境变量。"""
    if _TRUST_ENV_OVERRIDE is not None:
        return _TRUST_ENV_OVERRIDE
    return not _is_windows()


def create_http_session(
    *,
    pool_connections: int = 16,
    pool_maxsize: int = 32,
    max_retries: int = 0,
    trust_env: Optional[bool] = None,
    headers: Optional[Dict[str, str]] = None,
) -> requests.Session:
    """构造带连接池与统一 trust_env 策略的 requests.Session，并纳入传输层统一托管。"""
    session = requests.Session()
    session.trust_env = default_trust_env() if trust_env is None else bool(trust_env)
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=max_retries,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if headers:
        session.headers.update(headers)
    with _TRANSPORT_LOCK:
        _TRACKED_SESSIONS.add(session)
        if trust_env is None:
            session.trust_env = default_trust_env()
    return session


def _resolve_with_bounded_timeout(resolver, host, port, family, type_, proto, flags):
    """对真实 C 级系统 DNS 解析施加 1.5s 超时保护防 12s 卡死；测试替身直接同步调用。"""
    if resolver is not _REAL_STDLIB_GETADDRINFO:
        return resolver(host, port, family, type_, proto, flags)
    box = [None, None]
    done = threading.Event()

    def _worker():
        try:
            box[0] = resolver(host, port, family, type_, proto, flags)
        except Exception as exc:
            box[1] = exc
        finally:
            done.set()

    t = threading.Thread(target=_worker, name=f"dns-{host}", daemon=True)
    t.start()
    if not done.wait(timeout=_DNS_REAL_RESOLVE_TIMEOUT_SEC):
        raise socket.gaierror(f"DNS resolve timed out (>{_DNS_REAL_RESOLVE_TIMEOUT_SEC}s) for {host}")
    if box[1] is not None:
        raise box[1]
    return box[0]


def _schedule_proactive_dns_refresh(host, port, type_, proto, flags):
    resolver = _ORIG_GETADDRINFO
    if resolver is not _REAL_STDLIB_GETADDRINFO:
        return
    with _DNS_LOCK:
        if host in _DNS_REFRESHING:
            return
        _DNS_REFRESHING.add(host)

    def _bg_refresh():
        try:
            res = _resolve_with_bounded_timeout(resolver, host, port, socket.AF_INET, type_, proto, flags)
            if res:
                with _DNS_LOCK:
                    _DNS_CACHE[host] = (time.time() + 600, res)
        except Exception:
            pass
        finally:
            with _DNS_LOCK:
                _DNS_REFRESHING.discard(host)

    threading.Thread(target=_bg_refresh, name=f"dns-pre-{host}", daemon=True).start()


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if _DNS_ENABLED and host in _GATEWAY_FALLBACK_IPS:
        now = time.time()
        entry = _DNS_CACHE.get(host)
        if entry and now < entry[0]:
            if (entry[0] - now) <= _DNS_PROACTIVE_REFRESH_WINDOW_SEC:
                _schedule_proactive_dns_refresh(host, port, type, proto, flags)
            return [(f, t, p, c, (addr[0], port or addr[1])) for f, t, p, c, addr in entry[1]]
        with _DNS_LOCK:
            entry = _DNS_CACHE.get(host)
            if entry and now < entry[0]:
                return [(f, t, p, c, (addr[0], port or addr[1])) for f, t, p, c, addr in entry[1]]
            try:
                res = _resolve_with_bounded_timeout(_ORIG_GETADDRINFO, host, port, socket.AF_INET, type, proto, flags)
                _DNS_CACHE[host] = (now + 600, res)
                return res
            except Exception as exc:
                logger.debug(
                    "【DNS缓存兜底】解析联盟网关域名[%s]失败，已切换至缓存或内置备用IPv4地址 | 异常<%s>",
                    host, exc.__class__.__name__,
                )
                if entry:
                    return [(f, t, p, c, (addr[0], port or addr[1])) for f, t, p, c, addr in entry[1]]
                fallback_ips = _GATEWAY_FALLBACK_IPS.get(host, _PDD_FALLBACK_IPS)
                fallback_res = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))
                    for ip in fallback_ips
                ]
                _DNS_CACHE[host] = (now + 300, fallback_res)
                return fallback_res
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


def configure_transport(dns_mode: str = "auto", trust_env: Optional[bool] = None) -> None:
    """显式配置平台传输 defaults；不在此执行真实解析或预置过期静态 IP。"""
    global _ORIG_GETADDRINFO, _DNS_ENABLED, _DNS_INSTALLED, _TRUST_ENV_OVERRIDE

    if dns_mode not in ("auto", "on", "off"):
        raise ValueError("dns_mode must be 'auto', 'on', or 'off'")
    if trust_env is not None and not isinstance(trust_env, bool):
        raise TypeError("trust_env must be a bool or None")

    enabled = dns_mode == "on" or (dns_mode == "auto" and _is_windows())
    effective_trust = not _is_windows() if trust_env is None else trust_env
    with _TRANSPORT_LOCK:
        _TRUST_ENV_OVERRIDE = trust_env
        for sess in list(_TRACKED_SESSIONS):
            try:
                sess.trust_env = effective_trust
            except Exception:
                pass
        if enabled:
            if not _DNS_INSTALLED:
                _ORIG_GETADDRINFO = socket.getaddrinfo
                _DNS_ENABLED = True
                socket.getaddrinfo = _cached_getaddrinfo
                _DNS_INSTALLED = True
            else:
                _DNS_ENABLED = True
        else:
            _DNS_ENABLED = False
            if _DNS_INSTALLED and socket.getaddrinfo is _cached_getaddrinfo:
                socket.getaddrinfo = _ORIG_GETADDRINFO
                _DNS_INSTALLED = False


def register_gateway_fallback_ips(host: str, ips: Iterable[str]) -> None:
    """为新增第三方联盟网关域名登记备用 IPv4，仅当真实 DNS 失败/超时时兜底。"""
    clean_host = str(host or "").strip().lower()
    clean_ips = tuple(str(ip).strip() for ip in (ips or ()) if str(ip).strip())
    if not clean_host or not clean_ips:
        return
    with _TRANSPORT_LOCK:
        _GATEWAY_FALLBACK_IPS[clean_host] = clean_ips
        with _DNS_LOCK:
            _DNS_CACHE.pop(clean_host, None)
