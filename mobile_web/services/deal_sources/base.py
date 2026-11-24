# -*- coding: utf-8 -*-
"""
好价线报采集插件体系核心抽象契约 (`ChannelConfig` / `FetchContext` / `FetchResult` / `BaseDealSource`)。
================================================================================
零耦合设计：
- 抓取源插件只依赖 `FetchContext` 发起网络请求并返回标准化 `FetchResult`；
- 抓取源不直接读写 MongoDB、不感知调度线程、不依赖其他抓取源；
- 频道策略（全量巡视 / 单页抓新 / 时间窗口回溯 / 拉取时间保鲜 / 缺失失效清除 / 代理模式）
  全部由 `ChannelConfig` 声明式控制，由上层调度管道统一执行。
================================================================================
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import requests


@dataclass
class ChannelConfig:
    """单个线报采集频道的声明式配置模型。"""

    name: str                                  # 频道显示名称（唯一标识，亦写入 source_channel）
    site_key: str                              # 站点唯一标识（用于同源并发隔离与风控冷却）
    default_url: str                           # 源站监控地址（展示与排障参考）
    source_key: str = ""                       # 绑定插件键名；留空则默认复用 site_key
    params: Dict[str, Any] = field(default_factory=dict)  # 频道专有参数（如 label_id/type_id/mode/tg_channel）
    fetch_mode: str = "single_page"            # "full_sweep" | "single_page" | "time_window"
    enabled: bool = True                       # True 开启监控 / False 停用
    interval_seconds: int = 1800               # 拉取间隔（秒）
    lookback_seconds: Optional[int] = None     # 时间窗口模式下的回溯秒数（如 3600 / 900 / 172800）
    use_fetch_time_as_publish_time: bool = False  # True 时以本次拉取时间作为发布/保鲜基准时间
    expire_missing_on_sweep: bool = False      # True 且全量巡视完整成功时，自动下架未在列表出现的旧品
    max_pages: int = 1                         # 单轮最大翻页数上限（防无限翻页）
    max_details: int = 50                      # 单轮最多新抓详情条数（0 表示纯列表直出源）
    detail_delay_seconds: float = 0.15         # 详情页请求间隔（秒）
    sleep_after: float = 0.3                   # 本频道抓完后安全休眠（秒）
    rate_limit_cooldown_seconds: int = 300     # 触发 429/403/验证码后的同站冷却秒数
    max_age_days: float = 2.0                  # 爆料有效保鲜期（天）
    network_mode: str = "direct"               # "direct" (直连不走系统代理) | "proxy" (走系统/环境变量代理)
    close_connection: bool = False             # True 时携带 Connection: close 规避老站点 Keep-Alive 断连
    description: str = ""                      # 频道说明

    @property
    def resolved_source_key(self) -> str:
        return (self.source_key or self.site_key).strip()


# 兼容名称别名
DealChannelConfig = ChannelConfig


@dataclass
class FetchContext:
    """传递给数据源插件的单次执行上下文。"""

    channel: ChannelConfig
    session: requests.Session
    known_ids: Set[str] = field(default_factory=set)
    now_ts: float = field(default_factory=time.time)
    max_details: Optional[int] = None
    timeout: float = 15.0

    @property
    def effective_max_details(self) -> int:
        if self.max_details is not None:
            return max(0, int(self.max_details))
        return max(0, int(self.channel.max_details))

    @property
    def cutoff_ts(self) -> Optional[float]:
        lb = self.channel.lookback_seconds
        if lb is None or lb <= 0:
            return None
        return float(self.now_ts) - float(lb)


@dataclass
class FetchResult:
    """统一渠道抓取输出契约（status 严格收敛为 'ok' 或 'error'）。"""

    status: str
    http_status: Optional[int]
    request_url: str
    elapsed_ms: float
    records: List[Dict[str, Any]] = field(default_factory=list)
    raw_list_count: int = 0
    detail_fetched: int = 0
    skipped_known: int = 0
    skipped_unsupported: int = 0
    skipped_no_detail: int = 0
    skipped_out_of_window: int = 0
    pages_crawled: int = 0
    touch_ids: List[str] = field(default_factory=list)
    sweep_completed: bool = False
    rate_limited: bool = False
    allow_empty_ok: bool = False
    detail_errors: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "status": self.status,
            "http_status": self.http_status,
            "request_url": self.request_url,
            "elapsed_ms": self.elapsed_ms,
            "raw_list_count": self.raw_list_count,
            "count": len(self.records),
            "detail_fetched": self.detail_fetched,
            "skipped_known": self.skipped_known,
            "skipped_unsupported": self.skipped_unsupported,
            "skipped_no_detail": self.skipped_no_detail,
            "skipped_out_of_window": self.skipped_out_of_window,
            "pages_crawled": self.pages_crawled,
            "touch_ids": list(self.touch_ids),
            "sweep_completed": self.sweep_completed,
            "rate_limited": self.rate_limited,
            "allow_empty_ok": self.allow_empty_ok,
            "detail_errors": list(self.detail_errors),
            "records": self.records,
        }
        if self.extra:
            out.update(self.extra)
        return out


class BaseDealSource(ABC):
    """线报数据源插件基类：高内聚实现单站点解析逻辑，与调度/数据库彻底解耦。"""

    site_key: str = ""
    source_key: str = ""
    default_network_mode: str = "direct"
    close_connection: bool = False
    troubleshooting: Dict[str, str] = {}

    @property
    def resolved_source_key(self) -> str:
        return (self.source_key or self.site_key).strip()

    @abstractmethod
    def fetch(self, ctx: FetchContext) -> FetchResult:
        """抓取并标准化本频道的线报数据。"""

    @staticmethod
    def sleep_detail_delay(ctx: FetchContext) -> None:
        delay = max(0.0, float(ctx.channel.detail_delay_seconds))
        if delay > 0:
            time.sleep(delay)
