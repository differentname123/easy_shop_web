# -*- coding: utf-8 -*-
"""
好价线报采集插件体系 (`services.deal_sources`) 公开接口导出。
"""
from .baicaio import BaicaioSource
from .base import BaseDealSource, ChannelConfig, DealChannelConfig, FetchContext, FetchResult
from .catalog import (
    DEFAULT_CHANNEL_CONFIGS,
    get_active_channels,
    get_site_max_age_days_map,
    is_channel_due,
)
from .empire_deal import EmpireDealSource, KieesSource, NiuzaSource
from .filters import (
    CONFIRMED_UNSUPPORTED_HOSTS,
    CONFIRMED_UNSUPPORTED_MALLS,
    SOURCE_SELF_HOSTS,
    SUPPORTED_PLATFORMS,
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
    is_host_match,
    is_record_confirmed_unsupported,
    normalize_explicit_platform_code,
    unwrap_known_redirect_url,
)
from .guangdiu import (
    DEFAULT_GUANGDIU_COOKIE_FILE,
    GuangdiuSource,
    has_substantive_guangdiu_tutorial,
    is_guangdiu_captcha_page,
    load_guangdiu_cookies,
    save_guangdiu_cookies,
    unwrap_guangdiu_to_php,
)
from .haodanku import HaodankuSource
from .http_client import (
    DEFAULT_HEADERS,
    RATE_LIMIT_STATUSES,
    create_site_session,
    is_rate_limit_status,
    request_with_retry,
)
from .ithome_lapin import IthomeLapinSource
from .list_detail_base import DetailCircuitBreak, ListDetailDealSource, ListPhaseResult, SkipItem
from .m_zzzdm import MZzzdmSource
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_div_by_id,
    extract_first_price,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
    utc_now_iso,
)
from .registry import get_source, get_source_troubleshooting, list_sources, register_source
from .tehuicaicai import TehuicaicaiSource
from .telegram import TelegramChannelSource
from .tuan0818 import Tuan0818Source
from .xuandan import XuandanSource
from .zdzdm import ZdzdmSource

__all__ = [
    "BaseDealSource",
    "ChannelConfig",
    "DealChannelConfig",
    "FetchContext",
    "FetchResult",
    "DEFAULT_CHANNEL_CONFIGS",
    "get_active_channels",
    "is_channel_due",
    "get_site_max_age_days_map",
    "get_source",
    "list_sources",
    "register_source",
    "get_source_troubleshooting",
    "DEFAULT_HEADERS",
    "RATE_LIMIT_STATUSES",
    "create_site_session",
    "is_rate_limit_status",
    "request_with_retry",
    "utc_now_iso",
    "strip_html_tags",
    "extract_first_price",
    "extract_links_images_and_tokens",
    "html_to_tutorial_text",
    "extract_balanced_tag",
    "extract_div_by_id",
    "build_deal_record",
    "ListDetailDealSource",
    "ListPhaseResult",
    "DetailCircuitBreak",
    "SkipItem",
    "SUPPORTED_PLATFORMS",
    "SOURCE_SELF_HOSTS",
    "CONFIRMED_UNSUPPORTED_MALLS",
    "CONFIRMED_UNSUPPORTED_HOSTS",
    "normalize_explicit_platform_code",
    "unwrap_known_redirect_url",
    "is_host_match",
    "is_confirmed_unsupported_deal",
    "is_record_confirmed_unsupported",
    "has_valid_detail_and_tutorial",
    "TehuicaicaiSource",
    "GuangdiuSource",
    "DEFAULT_GUANGDIU_COOKIE_FILE",
    "is_guangdiu_captcha_page",
    "load_guangdiu_cookies",
    "save_guangdiu_cookies",
    "unwrap_guangdiu_to_php",
    "has_substantive_guangdiu_tutorial",
    "HaodankuSource",
    "XuandanSource",
    "Tuan0818Source",
    "MZzzdmSource",
    "TelegramChannelSource",
    "IthomeLapinSource",
    "EmpireDealSource",
    "KieesSource",
    "NiuzaSource",
    "ZdzdmSource",
    "BaicaioSource",
]
