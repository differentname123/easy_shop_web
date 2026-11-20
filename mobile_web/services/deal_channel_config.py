# -*- coding: utf-8 -*-
"""
好价线报采集频道配置模型与调度判定工具（第一性原理极简版）。
================================================================================
仅保留实际控制频道运行的核心参数：
- `enabled`: 启停开关
- `interval_seconds`: 单频道独立拉取间隔（秒）
- `max_details`: 单轮最多新抓详情条数
- `detail_delay_seconds`: 详情页请求间隔（秒，防触发目标站限流/滑块）
- `rate_limit_cooldown_seconds`: 触发限流或验证码后的站点冷却秒数
================================================================================
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DealChannelConfig:
    """单个线报采集频道的配置模型。"""

    name: str                                  # 频道显示名称
    site_key: str                              # 站点唯一标识（用于同源去重与站点级风控冷却）
    default_url: str                           # 源站监控地址（展示与排障参考）
    fetcher_name: str                          # 绑定的抓取函数名
    fetcher_kwargs: Dict[str, Any] = field(default_factory=dict)  # 频道专有抓取参数
    enabled: bool = True                       # True 开启监控 / False 停用
    interval_seconds: int = 180                # 拉取间隔（秒）
    max_details: int = 15                      # 单轮最多新抓详情条数
    detail_delay_seconds: float = 0.2          # 详情页请求间隔（秒）
    sleep_after: float = 0.3                   # 抓完本频道后安全休眠（秒）
    rate_limit_cooldown_seconds: int = 300     # 触发 429/403/验证码后的同站冷却秒数
    max_age_days: float = 2.0                  # 爆料有效保鲜期（天）：超出则丢弃，按频道灵活控制
    description: str = ""                      # 频道说明


# 默认监控频道（支持特惠采采全量严选、逛丢实时榜单与好单库全量线报）
DEFAULT_CHANNEL_CONFIGS: List[DealChannelConfig] = [
    # --------------------------------------------------------------------------
    # 1. 特惠采采（https://xhhcds.cn/）：人工严选 JSON 流，20分钟全量巡视
    # --------------------------------------------------------------------------
    DealChannelConfig(
        name="特惠采采_首页精选",
        site_key="tehuicaicai",
        default_url="https://xhhcds.cn/api/v1/public/home-products/?feed=home&page=1&page_size=50",
        fetcher_name="fetch_tehuicaicai_deals",
        fetcher_kwargs={
            "channel": "home",
            "page": 1,
            "page_size": 50,
            "max_pages": 10,
            "auto_paginate": True,
            "fetch_detail": True,
            "full_sweep": True,
        },
        enabled=True,
        interval_seconds=1200,
        max_details=50,
        detail_delay_seconds=0.05,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=7.0,
        description="特惠采采专员人工严选 JSON 流，20分钟全量巡视，已抓商品刷新确认时间，未在列表中自动判定失效",
    ),

    # --------------------------------------------------------------------------
    # 2. 好单库线报（https://www.haodanku.com/activity/tip_off）：淘宝/京东/百亿补贴 3 大频道
    # --------------------------------------------------------------------------
    DealChannelConfig(
        name="好单库_淘宝实时线报",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        fetcher_name="fetch_haodanku_wire_deals",
        fetcher_kwargs={
            "label_id": 10003,
            "channel_name": "好单库_淘宝实时线报",
            "full_sweep": True,
        },
        enabled=True,
        interval_seconds=1200,
        max_details=100,
        detail_delay_seconds=0.0,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=2.0,
        description="好单库官方淘宝实时线报，20分钟全量巡视，纯JSON直出无风控",
    ),
    DealChannelConfig(
        name="好单库_京东实时线报",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        fetcher_name="fetch_haodanku_wire_deals",
        fetcher_kwargs={
            "label_id": 10004,
            "channel_name": "好单库_京东实时线报",
            "full_sweep": True,
        },
        enabled=True,
        interval_seconds=1200,
        max_details=100,
        detail_delay_seconds=0.0,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=2.0,
        description="好单库官方京东实时线报，20分钟全量巡视，纯JSON直出无风控",
    ),
    DealChannelConfig(
        name="好单库_淘宝百亿补贴",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        fetcher_name="fetch_haodanku_wire_deals",
        fetcher_kwargs={
            "label_id": 10005,
            "channel_name": "好单库_淘宝百亿补贴",
            "full_sweep": True,
        },
        enabled=True,
        interval_seconds=1200,
        max_details=100,
        detail_delay_seconds=0.0,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=2.0,
        description="好单库官方淘宝百亿补贴线报，20分钟全量巡视，纯JSON直出无风控",
    ),

    # --------------------------------------------------------------------------
    # 3. 逛丢（https://guangdiu.com/）：首页、热门榜、小时风云榜
    # --------------------------------------------------------------------------
    DealChannelConfig(
        name="逛丢_首页实时流",
        site_key="guangdiu",
        default_url="https://guangdiu.com/",
        fetcher_name="fetch_guangdiu_deals",
        fetcher_kwargs={"channel": "latest", "fetch_detail": True},
        enabled=True,
        interval_seconds=180,
        max_details=35,
        detail_delay_seconds=0.3,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="逛丢首页实时爆料流（含详情购买方案解析与 go.php 直链还原）",
    ),
    DealChannelConfig(
        name="逛丢_热门榜",
        site_key="guangdiu",
        default_url="https://guangdiu.com/hots.php",
        fetcher_name="fetch_guangdiu_deals",
        fetcher_kwargs={"channel": "hots", "fetch_detail": True},
        enabled=True,
        interval_seconds=600,
        max_details=35,
        detail_delay_seconds=0.3,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="逛丢综合热门上榜好价流",
    ),
    DealChannelConfig(
        name="逛丢_小时风云榜",
        site_key="guangdiu",
        default_url="https://guangdiu.com/rank.php",
        fetcher_name="fetch_guangdiu_deals",
        fetcher_kwargs={"channel": "rank", "fetch_detail": True},
        enabled=True,
        interval_seconds=600,
        max_details=30,
        detail_delay_seconds=0.3,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="逛丢每小时热门风云榜",
    ),
]


def get_active_channels(
    channels: Optional[List[DealChannelConfig]] = None,
    run_all_tiers: bool = False,
    cycle_num: int = 1,
) -> List[DealChannelConfig]:
    """返回所有 `enabled=True` 的已启用频道（移除冗余梯队与奇偶轮限制，调度完全由各自 `interval_seconds` 控制）。"""
    _ = (run_all_tiers, cycle_num)
    pool = channels if channels is not None else DEFAULT_CHANNEL_CONFIGS
    return [ch for ch in pool if ch.enabled]


def is_channel_due(
    channel: DealChannelConfig,
    last_run_at: float,
    current_time: float,
) -> bool:
    """检查该频道是否已到达设定的拉取间隔 (`interval_seconds`)。"""
    if last_run_at <= 0:
        return True
    return (current_time - last_run_at) >= max(10, int(channel.interval_seconds))


def get_channel_max_age_days(
    channel_name: str = "",
    site_key: str = "",
    default_days: float = 2.0,
    channels: Optional[List[DealChannelConfig]] = None,
) -> float:
    """
    根据频道名称或站点标识获取配置的有效保鲜期（天）。
    - 优先精确匹配频道名（如 '特惠采采_首页精选' -> 7.0天）；
    - 其次匹配站点标识（如 'tehuicaicai' -> 7.0天）；
    - 未明确配置的兜底返回 default_days（默认2.0天）。
    """
    pool = channels if channels is not None else DEFAULT_CHANNEL_CONFIGS
    if channel_name:
        for c in pool:
            if c.name == channel_name:
                return float(c.max_age_days)
    if site_key:
        for c in pool:
            if c.site_key == site_key:
                return float(c.max_age_days)
    return float(default_days)


def get_site_max_age_days_map(
    channels: Optional[List[DealChannelConfig]] = None,
) -> Dict[str, float]:
    """获取站点级保鲜期映射表（多频道同站点取其最大保鲜期）。"""
    pool = channels if channels is not None else DEFAULT_CHANNEL_CONFIGS
    m: Dict[str, float] = {}
    for c in pool:
        cur = m.get(c.site_key, 0.0)
        m[c.site_key] = max(cur, float(c.max_age_days))
    return m

