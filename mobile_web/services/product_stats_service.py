# -*- coding: utf-8 -*-
"""通用商品互动计数缓冲服务：内存去重、有界聚合、后台批量刷盘。"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, Tuple

from cache_utils import ThreadSafeTTLCache
from config import logger
from infrastructure.logging_utils import format_exc_summary
from models.product import clean_product_id, normalize_platform

SERVER_PRODUCT_ACTIONS = frozenset({"view", "intent"})
CLIENT_PRODUCT_ACTIONS = frozenset({"share", "buyable", "unbuyable"})
ALLOWED_PRODUCT_ACTIONS = SERVER_PRODUCT_ACTIONS | CLIENT_PRODUCT_ACTIONS
_PRICE_FEEDBACK_ACTIONS = frozenset({"buyable", "unbuyable"})

MAX_PENDING_PRODUCTS = 4096
FLUSH_INTERVAL_SECONDS = 15.0
FLUSH_BATCH_TRIGGER = 200
DEDUP_MAX_SIZE = 20000
DEDUP_TTL_SECONDS = 300


def extract_actor_key(request: Any = None, uid: Optional[str] = None) -> str:
    """提取轻量去重主体标识（已登录用 uid，游客用真实客户端 IP）。"""
    clean_uid = str(uid or "").strip()
    if clean_uid:
        return f"u:{clean_uid}"
    if request is None:
        return ""
    headers = getattr(request, "headers", None)
    forwarded = headers.get("x-forwarded-for") if headers is not None else None
    if isinstance(forwarded, str) and forwarded.strip():
        ip = forwarded.split(",")[0].strip()
    else:
        client = getattr(request, "client", None)
        ip = str(getattr(client, "host", "") or "").strip() or "127.0.0.1"
    return f"ip:{ip}" if ip else ""


class ProductStatsBuffer:
    """进程内有界商品动作计数缓冲器，避免高频点击直接阻塞数据库写入。"""

    def __init__(
        self,
        repo_provider: Callable[[], Any],
        *,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self._repo_provider = repo_provider
        self._flush_interval = max(0.5, float(flush_interval))
        self._lock = threading.Lock()
        self._pending: Dict[Tuple[str, str], Dict[str, int]] = {}
        self._dedup = ThreadSafeTTLCache(maxsize=DEDUP_MAX_SIZE, ttl=DEDUP_TTL_SECONDS)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def record(self, platform: str, product_id: str, action: str, actor_key: str = "") -> bool:
        """记录一次合法商品动作；满足去重或容量上限时静默跳过并返回 False。"""
        act = str(action or "").strip().lower()
        if act not in ALLOWED_PRODUCT_ACTIONS:
            return False
        plat = normalize_platform(platform or "")
        pid = clean_product_id(product_id) or str(product_id or "").strip()
        if not plat or not pid or len(pid) > 128:
            return False

        clean_actor = str(actor_key or "").strip()
        if clean_actor:
            dedup_act = "price_feedback" if act in _PRICE_FEEDBACK_ACTIONS else act
            dedup_key = (clean_actor, plat, pid, dedup_act)
            if self._dedup.get(dedup_key) is not None:
                return False
            self._dedup.set(dedup_key, 1)

        should_wake = False
        with self._lock:
            key = (plat, pid)
            if key not in self._pending and len(self._pending) >= MAX_PENDING_PRODUCTS:
                return False
            bucket = self._pending.setdefault(key, {})
            bucket[act] = bucket.get(act, 0) + 1
            should_wake = len(self._pending) >= FLUSH_BATCH_TRIGGER

        if should_wake:
            self._wake_event.set()
        return True

    def flush(self) -> int:
        """原子交换待写入桶并批量写库；空桶不触发任何数据库访问。"""
        with self._lock:
            if not self._pending:
                return 0
            batch = self._pending
            self._pending = {}

        try:
            repo = self._repo_provider() if callable(self._repo_provider) else None
            bulk_fn = getattr(repo, "bulk_inc_user_stats", None) if repo is not None else None
            if not callable(bulk_fn):
                return 0
            bulk_fn(batch)
            return len(batch)
        except Exception as exc:
            logger.warning(
                "⚠️ 【商品互动计数刷盘异常】批量写入 user_stats 失败，已忽略本批统计以保护主链路 | 批大小[%d] 异常<%s>",
                len(batch),
                format_exc_summary(exc),
            )
            return 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="product-stats-flusher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self.flush()

    def reset_state(self) -> None:
        """仅用于测试隔离重置内存桶与去重缓存。"""
        with self._lock:
            self._pending.clear()
        self._dedup.clear()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self._flush_interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self.flush()
