# -*- coding: utf-8 -*-
"""
好价线报采集频道配置中心（单一真源 · 23 频道 / 12 站点）。
================================================================================
严格依据 `scripts/线报监控来源.txt` 声明式配置全部 14 个监控来源（共 23 个子频道）：
- `fetch_mode`:
  - `"full_sweep"` : 全量巡视；可配合 `use_fetch_time_as_publish_time=True` 与 `expire_missing_on_sweep=True`
  - `"single_page"`: 仅拉取第一页最新条目（增量去重跳过已知 ID）
  - `"time_window"`: 按 `lookback_seconds` 回溯时间窗口向后翻页拉取
- `network_mode`: `"direct"` (国内直连不走系统代理) | `"proxy"` (Telegram 等海外通道自动走代理)
================================================================================
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import ChannelConfig

DEFAULT_CHANNEL_CONFIGS: List[ChannelConfig] = [
    # 1. 特惠采采 (https://xhhcds.cn/)
    ChannelConfig(
        name="特惠采采_首页精选",
        site_key="tehuicaicai",
        default_url="https://xhhcds.cn/api/v1/public/home-products/?feed=home&page=1&page_size=50",
        params={"channel": "home", "page_size": 50, "auto_paginate": True},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1200,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=True,
        max_pages=15,
        max_details=150,
        detail_delay_seconds=0.05,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=7.0,
        description="特惠采采人工严选 JSON 流，20分钟全量巡视，以拉取时间作为发布时间并自动下架缺失商品",
    ),

    # 2. 逛丢 (https://guangdiu.com/)
    ChannelConfig(
        name="逛丢_首页实时流",
        site_key="guangdiu",
        default_url="https://guangdiu.com/",
        params={"channel": "latest"},
        fetch_mode="single_page",
        enabled=True,
        interval_seconds=180,
        max_pages=1,
        max_details=35,
        detail_delay_seconds=0.3,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=600,
        max_age_days=2.0,
        description="逛丢首页实时流，3分钟拉取一页",
    ),
    ChannelConfig(
        name="逛丢_热门榜",
        site_key="guangdiu",
        default_url="https://guangdiu.com/hots.php",
        params={"channel": "hots"},
        fetch_mode="single_page",
        enabled=True,
        interval_seconds=1200,
        max_pages=1,
        max_details=35,
        detail_delay_seconds=0.3,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=600,
        max_age_days=2.0,
        description="逛丢热门榜，20分钟拉取一页",
    ),
    ChannelConfig(
        name="逛丢_小时风云榜",
        site_key="guangdiu",
        default_url="https://guangdiu.com/rank.php",
        params={"channel": "rank"},
        fetch_mode="single_page",
        enabled=True,
        interval_seconds=1200,
        max_pages=1,
        max_details=30,
        detail_delay_seconds=0.3,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=600,
        max_age_days=2.0,
        description="逛丢小时风云榜，20分钟拉取一页",
    ),

    # 3. 好单库 API (3 大榜单)
    ChannelConfig(
        name="好单库_淘宝实时线报",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        params={"label_id": 10003},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=True,
        max_pages=30,
        max_details=0,
        detail_delay_seconds=0.0,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=2.0,
        description="好单库淘宝实时线报 API，30分钟全量巡视，以拉取时间作为发布时间",
    ),
    ChannelConfig(
        name="好单库_京东实时线报",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        params={"label_id": 10004},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=True,
        max_pages=30,
        max_details=0,
        detail_delay_seconds=0.0,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=2.0,
        description="好单库京东实时线报 API，30分钟全量巡视，以拉取时间作为发布时间",
    ),
    ChannelConfig(
        name="好单库_淘宝百亿补贴",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        params={"label_id": 10005},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=True,
        max_pages=30,
        max_details=0,
        detail_delay_seconds=0.0,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        max_age_days=2.0,
        description="好单库淘宝百亿补贴 API，30分钟全量巡视，以拉取时间作为发布时间",
    ),

    # 4. 选单网 (https://www.xuandan.com/)
    ChannelConfig(
        name="选单网_社群文案榜",
        site_key="xuandan",
        default_url="https://www.xuandan.com/allList.html?type=8",
        params={"type_id": 8},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=10,
        max_details=0,
        detail_delay_seconds=0.1,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="选单网社群素材文案榜 (type=8)，30分钟全量翻页，列表内嵌完整文案与二合一链接",
    ),
    ChannelConfig(
        name="选单网_隐藏券销量榜",
        site_key="xuandan",
        default_url="https://www.xuandan.com/allList.html?type=7&is_hidden=1",
        params={"type_id": 7, "is_hidden": 1},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=35,
        max_details=0,
        detail_delay_seconds=0.1,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="选单网隐藏券销量榜 (type=7&is_hidden=1)，30分钟全量翻页",
    ),
    ChannelConfig(
        name="选单网_低价销量榜",
        site_key="xuandan",
        default_url="https://www.xuandan.com/allList.html?type=11",
        params={"type_id": 11},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=15,
        max_details=0,
        detail_delay_seconds=0.1,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="选单网低价销量榜 (type=11)，30分钟全量翻页",
    ),

    # 5. 0818团 (http://www.0818tuan.com/list-1-0.html)
    ChannelConfig(
        name="0818团_最新线报",
        site_key="0818tuan",
        default_url="http://www.0818tuan.com/list-1-0.html",
        fetch_mode="single_page",
        enabled=True,
        interval_seconds=3600,
        max_pages=1,
        max_details=50,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="0818团最新线报单页，1小时拉取一次",
    ),

    # 6. 值值得买热榜 (https://m.zzzdm.com/posts/ranking.html)
    ChannelConfig(
        name="值值得买_24小时热榜",
        site_key="zzzdm",
        source_key="m_zzzdm",
        default_url="https://m.zzzdm.com/posts/ranking.html",
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=True,
        max_pages=1,
        max_details=90,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="值值得买 24 小时热榜，30分钟全量巡视，以拉取时间作为发布时间",
    ),

    # 7. Telegram: 真正值得买 (https://t.me/s/zzzdm)
    ChannelConfig(
        name="TG_真正值得买",
        site_key="telegram",
        default_url="https://t.me/s/zzzdm",
        params={"tg_channel": "zzzdm"},
        fetch_mode="time_window",
        enabled=True,
        interval_seconds=1800,
        lookback_seconds=3600,
        max_pages=8,
        max_details=0,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        network_mode="proxy",
        description="Telegram 真正值得买频道，30分钟执行一次，回溯最近 1 小时消息",
    ),

    # 8. IT之家辣品 (https://digi.ithome.com/lapin)
    ChannelConfig(
        name="IT之家辣品_好物",
        site_key="ithome_lapin",
        default_url="https://digi.ithome.com/lapin",
        fetch_mode="time_window",
        enabled=True,
        interval_seconds=3600,
        lookback_seconds=172800,
        max_pages=10,
        max_details=80,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.5,
        description="IT之家辣品好物，60分钟执行一次，获取 2 天内爆料",
    ),

    # 9. 发现值得买 (http://www.kiees.com)
    ChannelConfig(
        name="发现值得买_首页",
        site_key="kiees",
        default_url="http://www.kiees.com/index.html",
        params={"mode": "index"},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=1,
        max_details=35,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        close_connection=True,
        description="发现值得买首页，30分钟全量巡视，以拉取时间作为发布时间",
    ),
    ChannelConfig(
        name="发现值得买_24H热榜",
        site_key="kiees",
        default_url="http://www.kiees.com/hot_1",
        params={"mode": "hot_1"},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=8,
        max_details=80,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        close_connection=True,
        description="发现值得买 24H 热榜，30分钟全量翻页，以拉取时间作为发布时间",
    ),

    # 10. 牛杂网 (https://www.niuza.com/)
    ChannelConfig(
        name="牛杂网_首页",
        site_key="niuza",
        default_url="https://www.niuza.com/index.html",
        params={"mode": "index"},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=1,
        max_details=40,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        close_connection=True,
        description="牛杂网首页，30分钟全量巡视，以拉取时间作为发布时间",
    ),
    ChannelConfig(
        name="牛杂网_24H热榜",
        site_key="niuza",
        default_url="https://www.niuza.com/hot_1",
        params={"mode": "hot_1"},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=8,
        max_details=80,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        close_connection=True,
        description="牛杂网 24H 热榜，30分钟全量翻页，以拉取时间作为发布时间",
    ),

    # 11. 真的值得买 (https://www.zdzdm.com/youhui)
    ChannelConfig(
        name="真的值得买_国内优惠",
        site_key="zdzdm",
        default_url="https://www.zdzdm.com/youhui",
        fetch_mode="single_page",
        enabled=True,
        interval_seconds=1800,
        max_pages=1,
        max_details=25,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="真的值得买国内优惠第一页，30分钟拉取一次",
    ),

    # 12. 白菜哦 (https://www.baicaio.com/)
    ChannelConfig(
        name="白菜哦_编辑推荐",
        site_key="baicaio",
        default_url="https://www.baicaio.com/nice/",
        params={"mode": "nice"},
        fetch_mode="single_page",
        enabled=True,
        interval_seconds=1800,
        max_pages=1,
        max_details=25,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="白菜哦编辑推荐第一页，30分钟拉取一次",
    ),
    ChannelConfig(
        name="白菜哦_小时榜",
        site_key="baicaio",
        default_url="https://www.baicaio.com/hour/",
        params={"mode": "hour"},
        fetch_mode="full_sweep",
        enabled=True,
        interval_seconds=1800,
        use_fetch_time_as_publish_time=True,
        expire_missing_on_sweep=False,
        max_pages=1,
        max_details=30,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        description="白菜哦小时排行榜，30分钟全量巡视，以拉取时间作为发布时间",
    ),

    # 13 & 14. Telegram 高频情报频道
    ChannelConfig(
        name="TG_彼岸情报",
        site_key="telegram",
        default_url="https://t.me/s/BaccanoSoul",
        params={"tg_channel": "BaccanoSoul"},
        fetch_mode="time_window",
        enabled=True,
        interval_seconds=600,
        lookback_seconds=900,
        max_pages=5,
        max_details=0,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        network_mode="proxy",
        description="Telegram BaccanoSoul 频道，10分钟执行一次，获取最近 15 分钟电商情报",
    ),
    ChannelConfig(
        name="TG_福利资源",
        site_key="telegram",
        default_url="https://t.me/s/Pojieapp",
        params={"tg_channel": "Pojieapp"},
        fetch_mode="time_window",
        enabled=True,
        interval_seconds=600,
        lookback_seconds=900,
        max_pages=5,
        max_details=0,
        detail_delay_seconds=0.15,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=300,
        max_age_days=2.0,
        network_mode="proxy",
        description="Telegram Pojieapp 频道，10分钟执行一次，获取最近 15 分钟电商线报",
    ),
]


def get_active_channels(
    channels: Optional[List[ChannelConfig]] = None,
) -> List[ChannelConfig]:
    """返回所有 `enabled=True` 的已启用频道。"""
    pool = channels if channels is not None else DEFAULT_CHANNEL_CONFIGS
    return [ch for ch in pool if ch.enabled]


def is_channel_due(
    channel: ChannelConfig,
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
    channels: Optional[List[ChannelConfig]] = None,
) -> float:
    """根据频道名称或站点标识获取配置的有效保鲜期（天）。"""
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
    channels: Optional[List[ChannelConfig]] = None,
) -> Dict[str, float]:
    """获取站点级保鲜期映射表（多频道同站点取其最大保鲜期）。"""
    pool = channels if channels is not None else DEFAULT_CHANNEL_CONFIGS
    m: Dict[str, float] = {}
    for c in pool:
        cur = m.get(c.site_key, 0.0)
        m[c.site_key] = max(cur, float(c.max_age_days))
    return m
