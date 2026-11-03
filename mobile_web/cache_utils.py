# -*- coding: utf-8 -*-
"""
线程安全、有界容量 LRU-TTL 内存缓存包装模块
基于 cachetools.TTLCache 与 threading.RLock，彻底消除进程 OOM 与并发竞态
"""
import threading
from typing import Any, Hashable
from cachetools import TTLCache


class ThreadSafeTTLCache:
    """
    线程安全的有界 LRU+TTL 缓存容器。
    - maxsize: 设定最大容纳条目数，达到上限时自动按 LRU 算法剔除最久未访问条目；
    - ttl: 存活时间（秒），过期条目在读取或写入时自动失效。
    """
    def __init__(self, maxsize: int, ttl: float):
        self._cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.RLock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            try:
                return self._cache[key]
            except KeyError:
                return default

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._cache[key] = value

    def __contains__(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._cache

    def pop(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            return self._cache.pop(key, default)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def __len__(self) -> int:
        return self.size()
