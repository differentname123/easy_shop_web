# -*- coding: utf-8 -*-
"""
好价线报双阶段 AI 提纯 + 官方联盟转链核验 + 高分晋升清洗器 (`DealPoolCleaner`)。
负责 Stage 2 (AI-1 拆解) -> Stage 3 (官方转链) -> Stage 4 (AI-2 终审评级) -> Stage 5 (高分入库 `products`)。
"""
import copy
import datetime
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import zip_longest
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from pymongo import UpdateOne

from adapters import get_adapter, platform_name
from services.deal_sources.catalog import get_site_max_age_days_map
from infrastructure.ai_api.model_api import generate_content
from infrastructure.api_governance import api_caller_context
from infrastructure.mongo import gen_db_object
from models.deal_constants import (
    CLEAN_FAILED_SAMPLES_JSONL,
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    CLEAN_SUCCESS_PREVIEW_JSONL,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEAL_INFO_PROTECTION_HOURS,
    DEAL_MAX_CLEAN_AGE_DAYS,
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_CLEAN_WINDOW,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
    DEFAULT_STAGE_POLL_INTERVAL_SECONDS,
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_ADAPTER_OFF_PROMOTION,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI2_MAIN_DEAD_ONLY_FILLER,
    ERR_AI2_PRODUCT_MISMATCH,
    ERR_AI2_VIRTUAL_OR_JUNK,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_AI_TIMEOUT_RETRY,
    ERR_EMPTY_PRODUCT_IDENTITY,
    ERR_EXPIRED_OVER_7_DAYS,
    ERR_JD_NUMERIC_SKU,
    ERR_LOW_QUALITY_SCORE,
    ERR_MAIN_PRODUCT_DEAD,
    ERR_MERGED_INTO_NEIGHBOR,
    ERR_NO_ANCHOR,
    ERR_PDD_NUMERIC_ID,
    ERR_TB_NUMERIC_ID,
    ERR_UNSUPPORTED_PLATFORM,
    MAX_ANCHOR_CACHE_SIZE,
    MAX_CLEAN_RETRIES,
    OK_SKIPPED_ALREADY_CLEANED,
    PROMPT_STAGE1_PATH,
    PROMPT_STAGE2_PATH,
    QUALIFIED_DEAL_MIN_SCORE,
    SOURCE_REPUTATION_BONUS,
    STATS_SNAPSHOT_PATH,
    _UNSUPPORTED_EXTERNAL_DOMAINS,
)
from models.product import (
    detect_platform_signal,
    extract_canonical_goods_id,
    extract_jd_image_key,
    extract_taobao_token_core,
    get_source_site_name,
    is_jd_image_key,
)
from repositories.deal_raw_pool_repo import (
    DealRawPoolRepository,
    _append_bounded_jsonl_lines,
    evaluate_quality_signals,
    parse_publish_time_epoch,
)
from mobile_web.scripts.goods_info_format import string_to_object

logger = logging.getLogger("deal_pool_scheduler")

def _to_naive_utc(dt: Any) -> Optional[datetime.datetime]:
    """统一将任意 datetime 对象转换为无时区偏移 (offset-naive) 的 UTC 时间，彻底杜绝 MongoDB tz_aware 比较报错。"""
    if not isinstance(dt, datetime.datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


_CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


def parse_publish_time_epoch(pub_str: Any) -> Optional[float]:
    """将常见发布时间字符串解析为 Unix 时间戳（秒），统一按东八区或显式时区解析，防止 UTC 容器环境偏移 8 小时。"""
    s = str(pub_str or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        try:
            dt = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            pass
    try:
        iso_dt = datetime.datetime.fromisoformat(s)
        if iso_dt.tzinfo is None:
            iso_dt = iso_dt.replace(tzinfo=_CHINA_TZ)
        return iso_dt.timestamp()
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=_CHINA_TZ).timestamp()
        except Exception:
            continue
    return None


def detect_candidate_platform(raw_input: str, default_plat: str = "unknown") -> str:
    """判断单条链接或口令所属平台（jd / taobao / pdd）。"""
    s = (raw_input or "").strip().lower()
    if s.startswith(("http://", "https://")):
        path_only = s.split("?", 1)[0].split("#", 1)[0]
        if path_only.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")):
            return "unsupported_other"
    if any(d in s for d in _UNSUPPORTED_EXTERNAL_DOMAINS):
        return "unsupported_other"
    sig = detect_platform_signal(raw_input)
    if sig in ("jd", "taobao", "pdd"):
        return sig
    if default_plat in ("jd", "taobao", "pdd"):
        return default_plat
    return "unknown"


_MATH_ALPHANUM_RE = re.compile(r"[\U0001D400-\U0001D7FF]+")


def _normalize_inline_spaces(text: str) -> str:
    """统一特殊空格、剥离零宽字符，并将倾斜/花体数学字母数字(U+1D400..U+1D7FF，如 𝟖𝟖𝒗𝒊𝒑)还原为标准 ASCII 字符。"""
    if not text:
        return ""
    s = str(text).replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
    if _MATH_ALPHANUM_RE.search(s):
        s = _MATH_ALPHANUM_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), s)
    return s


def _clean_coupon_label(label: str) -> str:
    """清理优惠券按钮文案中的冗余『领券：/先领』前缀，避免与前端 [领券] 角标重复。"""
    s = _normalize_inline_spaces(str(label or "")).strip()
    s = re.sub(r"^(?:先领|点击领取|领取|领券|优惠券)\s*[:：]\s*", "", s).strip()
    s = re.sub(r"^(?:先领|点击领取)(?=.{2,})", "", s).strip()
    if not s or s in ("领券", "领", "券"):
        return "优惠券"
    return s


def rebuild_clean_official_venue_url(raw_input: str, _allow_login_unwrap: bool = True) -> Optional[str]:
    """
    【唯一例外辅通道：官方原生公共领券/会场页 · 路径白名单 + 参数白名单重组】
    仅允许以下 6 类平台原生公共领券/活动/店铺主页，且仅提取页面打开必需的官方参数重组为纯净 HTTPS URL（其余一切外部参数 100% 丢弃）：
      1. `coupon.m.jd.com/coupons/show.action` -> 仅保留 `key`, `roleId`, `linkKey`（须含 `linkKey` 或同时含 `key`+`roleId`）
      2. `uland.taobao.com/quan/detail` -> 仅保留 `sellerId`, `activityId`（两者须同时存在；绝不放行 `/coupon/edetail` 或 `pid`）
      3. `pages.tmall.com/wow/...` 及 `pages-fast.m.taobao.com/wow/...` -> 严禁阿里妈妈联盟红包（`/lianmeng` 或 `wh_alimama=true`），其余仅保留路径与 `wh_pid`
      4. `pro.m.jd.com/mall/active/...` 及 `prodev.m.jd.com/mall/active/...` -> 仅保留路径本身（清空全部 query 参数）
      5. `shop.m.jd.com/mshop/gethomepage` 及 `shop.m.jd.com/member/...` -> 仅保留 `venderId` / `shopId`（须至少含其一）
      6. `store.taobao.com/shop/view_shop.htm` -> 仅保留 `user_number_id` / `shop_id`（须至少含其一）
    其余任何链接（含一切第三方跳转短链、加密分佣页、未知域名）一律返回 None。
    """
    raw = str(raw_input or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(raw)
    except Exception:
        return None
    host = (parts.hostname or "").strip().lower()
    path = parts.path or ""
    if not host or not path:
        return None

    if _allow_login_unwrap and host == "plogin.m.jd.com":
        for k, v in parse_qsl(parts.query, keep_blank_values=False):
            if k.strip().lower() == "returnurl" and v.strip():
                return rebuild_clean_official_venue_url(unquote(v.strip()), _allow_login_unwrap=False)
        return None

    def _pick_allowed_params(canonical_map: Dict[str, str]) -> Tuple[List[Tuple[str, str]], Dict[str, str]]:
        kept_list: List[Tuple[str, str]] = []
        found: Dict[str, str] = {}
        for k, v in parse_qsl(parts.query, keep_blank_values=False):
            canon = canonical_map.get(k.strip().lower())
            val = v.strip()
            if canon and val and canon not in found:
                found[canon] = val
                kept_list.append((canon, val))
        return kept_list, found

    if host == "coupon.m.jd.com" and path == "/coupons/show.action":
        pairs, found = _pick_allowed_params({"key": "key", "roleid": "roleId", "linkkey": "linkKey"})
        if not (found.get("linkKey") or (found.get("key") and found.get("roleId"))):
            return None
        return urlunsplit(("https", host, path, urlencode(pairs), ""))

    if host == "uland.taobao.com" and path == "/quan/detail":
        pairs, found = _pick_allowed_params({"sellerid": "sellerId", "activityid": "activityId"})
        if not (found.get("sellerId") and found.get("activityId")):
            return None
        return urlunsplit(("https", host, path, urlencode(pairs), ""))

    if host in ("pages.tmall.com", "pages-fast.m.taobao.com") and path.startswith("/wow/"):
        path_segments = [seg for seg in path.lower().split("/") if seg]
        if "lianmeng" in path_segments:
            return None
        for qk, qv in parse_qsl(parts.query, keep_blank_values=True):
            if qk.strip().lower() in ("wh_alimama", "alimama") and qv.strip().lower() in ("true", "1"):
                return None
        pairs, _ = _pick_allowed_params({"wh_pid": "wh_pid"})
        return urlunsplit(("https", host, path, urlencode(pairs), ""))

    if host in ("pro.m.jd.com", "prodev.m.jd.com") and path.startswith("/mall/active/"):
        return urlunsplit(("https", host, path, "", ""))

    if host == "shop.m.jd.com" and (path == "/mshop/gethomepage" or path.startswith("/member/")):
        pairs, found = _pick_allowed_params({"venderid": "venderId", "shopid": "shopId"})
        if not (found.get("venderId") or found.get("shopId")):
            return None
        return urlunsplit(("https", host, path, urlencode(pairs), ""))

    if host == "store.taobao.com" and path == "/shop/view_shop.htm":
        pairs, found = _pick_allowed_params({"user_number_id": "user_number_id", "shop_id": "shop_id"})
        if not (found.get("user_number_id") or found.get("shop_id")):
            return None
        return urlunsplit(("https", host, path, urlencode(pairs), ""))

    return None


def is_direct_coupon_or_venue_url(raw_input: str, role_hint: str = "") -> bool:
    """判断一个 URL 是否满足官方公共领券/会场白名单准入。"""
    return rebuild_clean_official_venue_url(raw_input) is not None


def _precheck_direct_non_cps_venue_url(raw_input: str) -> Optional[str]:
    """
    前置免白耗判断：若输入字面已是 100% 无联盟佣金的官方公共领券/活动页（排除可转联盟佣金的京东 `pro*.m.jd.com` 会场），
    直接返回重组后的纯净官方 URL，省去必败的上游转链 API 调用。
    """
    rebuilt = rebuild_clean_official_venue_url(raw_input)
    if not rebuilt:
        return None
    if (urlsplit(rebuilt).hostname or "").strip().lower() in ("pro.m.jd.com", "prodev.m.jd.com"):
        return None
    return rebuilt


def _is_verified_station_promo_link(clean_input: str, promo_link: str) -> bool:
    """
    【通道 1 门禁】：强校验转链产出的 `promo_link` 必须属于官方联盟推广域名，
    且归一化 `(host, path, query)` 严禁等于原输入（彻底封堵上游接口原样回显或仅将 http 改 https 的伪转链）。
    """
    p_raw = str(promo_link or "").strip()
    if not p_raw.lower().startswith(("http://", "https://")):
        return False
    try:
        p = urlsplit(p_raw)
    except Exception:
        return False
    p_host = (p.hostname or "").strip().lower()
    p_path = (p.path or "").strip()
    is_official_cps_host = p_host in (
        "u.jd.com",
        "union-click.jd.com",
        "s.click.taobao.com",
        "s.click.tmall.com",
        "p.pinduoduo.com",
        "mobile.yangkeduo.com",
    ) or (p_host == "uland.taobao.com" and p_path.startswith(("/coupon/edetail", "/item/edetail")))
    if not is_official_cps_host:
        return False

    c_raw = str(clean_input or "").strip()
    if c_raw.lower().startswith(("http://", "https://")):
        try:
            c = urlsplit(c_raw)
            if (p_host, p_path.rstrip("/"), p.query or "") == (
                (c.hostname or "").strip().lower(),
                (c.path or "").rstrip("/"),
                c.query or "",
            ):
                return False
        except Exception:
            return False
    return True


_TERMINAL_SINGLE_ITEM_HOSTS = frozenset({
    "item.jd.com",
    "item.m.jd.com",
    "item.yiyaojd.com",
    "npcitem.jd.hk",
    "item.taobao.com",
    "h5.m.taobao.com",
    "detail.tmall.com",
    "chaoshi.detail.tmall.com",
    "detail.tmall.hk",
    "mobile.yangkeduo.com",
    "p.pinduoduo.com",
})

_UNWRAP_HTML_REDIRECT_RE = re.compile(
    r"(?:real_jump_address|hrl|var\s+url)\s*=\s*['\"]((?:https?:)?//[^'\"]+)['\"]"
    r"|location\.(?:href|replace|assign)\s*(?:=|\()\s*['\"]((?:https?:)?//[^'\"]+)['\"]"
    r"|<meta\b(?=[^>]*http-equiv=['\"]?refresh['\"]?)[^>]*content=['\"][^'\"]*?url=['\"]?((?:https?:)?//[^'\"\s>]+)",
    re.IGNORECASE,
)

_OFFICIAL_ECOMMERCE_HOST_SUFFIXES = (
    "jd.com",
    "jd.hk",
    "yiyaojd.com",
    "3.cn",
    "jingfen.com",
    "taobao.com",
    "tmall.com",
    "tmall.hk",
    "tb.cn",
    "tb3.cn",
    "pinduoduo.com",
    "yangkeduo.com",
)


def _is_official_ecommerce_http_url(url_str: str) -> bool:
    """判断 http(s) 链接域名是否归属淘/京/拼官方域名体系（非官方第三方短链无需白调联盟转链 API，直接走安全解包白名单）。"""
    try:
        host = (urlsplit(str(url_str or "").strip()).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    return any(host == suf or host.endswith("." + suf) for suf in _OFFICIAL_ECOMMERCE_HOST_SUFFIXES)


_UNWRAP_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)


_TUN_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _is_safe_public_http_url(url_str: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> bool:
    """SSRF 安全校验：每跳强制校验 http/https 标准端口与全局公网 IP，拒绝回环、内网、链路本地与云元数据地址。"""
    s = str(url_str or "").strip()
    if not s or any(ord(ch) < 33 for ch in s):
        return False
    try:
        parts = urlsplit(s)
        port = parts.port
    except Exception:
        return False
    if parts.scheme.lower() not in ("http", "https") or parts.username is not None or parts.password is not None:
        return False
    if port not in (None, 80, 443):
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ips = [ipaddress.ip_address(host.split("%", 1)[0])]
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
        try:
            infos = resolver(host, port or (443 if parts.scheme.lower() == "https" else 80), 0, socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in (infos or []) if info and len(info) >= 5 and info[4]]
        except Exception:
            return False
    if not ips:
        return False
    for ip in ips:
        if (
            ip.is_global
            and not (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )
        ):
            continue
        # 本机代理 TUN / Fake-IP 模式会将公网域名解析为 198.18.0.0/15；直接访问裸 198.18.x.x IP 仍严格拦截
        if (not is_ip_literal) and ip in _TUN_FAKE_IP_NETWORK:
            continue
        return False
    return True


def unwrap_to_official_public_venue_url(
    raw_input: str,
    max_hops: int = 4,
    timeout: float = 3.5,
    http_session: Any = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> Optional[str]:
    """
    【通道 2 零域名预设通用安全解包】：
    当上游官方联盟转链未成功时，不预设任何第三方短链域名规则，安全跟随最多 `max_hops` 跳（301/302、meta refresh、前端明文跳转及手淘 `tu=`），
    一旦落地页严格命中 `rebuild_clean_official_venue_url` 的 4 类官方公共白名单，立即剥离全部追踪参数重组返回纯净官方 HTTPS URL；否则一律返回 None。
    """
    cur = str(raw_input or "").strip()
    if not cur.lower().startswith(("http://", "https://")) or rebuild_clean_official_venue_url(cur) is not None:
        return None
    try:
        init_host = (urlsplit(cur).hostname or "").strip().lower()
    except Exception:
        return None
    if not init_host or init_host in _TERMINAL_SINGLE_ITEM_HOSTS:
        return None

    own_session = http_session is None
    sess = requests.Session() if own_session else http_session
    req_timeout = (min(2.5, float(timeout)), float(timeout))
    prev_url: Optional[str] = None
    try:
        for _ in range(max(1, int(max_hops))):
            if not _is_safe_public_http_url(cur, resolver=resolver):
                return None
            headers = {"User-Agent": _UNWRAP_MOBILE_UA}
            if prev_url:
                headers["Referer"] = prev_url
            resp = None
            try:
                resp = sess.get(cur, allow_redirects=False, timeout=req_timeout, stream=True, headers=headers)
                nxt = ""
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    nxt = str(resp.headers.get("Location") or "").strip()
                else:
                    if hasattr(resp, "iter_content"):
                        buf = bytearray()
                        for chunk in resp.iter_content(chunk_size=4096):
                            if chunk:
                                buf.extend(chunk[: max(0, 16384 - len(buf))])
                            if len(buf) >= 16384:
                                break
                        body = bytes(buf).decode(getattr(resp, "encoding", None) or "utf-8", errors="ignore")
                    else:
                        body = str(getattr(resp, "text", "") or "")[:16384]
                    m = _UNWRAP_HTML_REDIRECT_RE.search(body.replace(r"\/", "/"))
                    if m:
                        nxt = next((g for g in m.groups() if g), "")
            except Exception:
                return None
            finally:
                if resp is not None and hasattr(resp, "close"):
                    try:
                        resp.close()
                    except Exception:
                        pass

            if not nxt:
                break
            nxt = html_lib.unescape(nxt).strip()
            if nxt.startswith("//"):
                nxt = "https:" + nxt
            elif not nxt.lower().startswith(("http://", "https://")):
                nxt = urljoin(cur, nxt)

            qs = parse_qs(urlsplit(nxt).query)
            if qs.get("tu"):
                cand_tu = unquote(str(qs["tu"][0] or "")).strip()
                if cand_tu.lower().startswith(("http://", "https://")):
                    prev_url, cur = nxt, cand_tu
                else:
                    prev_url, cur = cur, nxt
            else:
                prev_url, cur = cur, nxt

            rebuilt = rebuild_clean_official_venue_url(cur)
            if rebuilt:
                return rebuilt
    finally:
        if own_session and hasattr(sess, "close"):
            try:
                sess.close()
            except Exception:
                pass

    if cur != str(raw_input or "").strip():
        return rebuild_clean_official_venue_url(cur)
    return None


_ROLE_HINT_NORMALIZE_MAP = {
    "main_candidate": "main_candidate",
    "main_product": "main_candidate",
    "main": "main_candidate",
    "coupon_candidate": "coupon_candidate",
    "coupon": "coupon_candidate",
    "filler_candidate": "filler_candidate",
    "filler_product": "filler_candidate",
    "filler": "filler_candidate",
}


class DealPoolCleaner:
    """
    五阶段双 AI 赋能 + 官方 API 转链清洗器：
    - Stage 2: 调用 AI-1 (`爆料阶段1_原始拆解与转链提纯.txt`) 合并同群 ±3 分钟相邻消息、无损拆解并编号全部链接/口令 (`L1`, `L2`...)；
    - Stage 3: 确定性调用 `JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal()`，区分单品 (`product`)、优惠券/会场 (`coupon_or_activity`) 与死链 (`failed`)；
    - Stage 4: 调用 AI-2 (`爆料阶段2_底牌终审与教程评分.txt`) 结合官方真实商品详情终审主商品/优惠券/凑单品角色、淘汰残次死链、提取爆料到手价、生成步骤化教程并给出 0~100 分 `quality_score`。
    """

    def __init__(
        self,
        use_ai_for_multi: bool = True,
        ai_batch_size: int = 10,
        ai2_batch_size: Optional[int] = None,
        ai_max_workers: int = 8,
        convert_max_workers: int = 10,
        ai_timeout: float = DEFAULT_AI_TIMEOUT_SECONDS,
        unwrap_http_session: Any = None,
        unwrap_resolver: Optional[Callable[..., Any]] = None,
    ):
        self.use_ai = True
        self.ai_batch_size = max(1, ai_batch_size)
        self.ai2_batch_size = max(1, ai2_batch_size if ai2_batch_size is not None else 10)
        self.ai_max_workers = max(1, ai_max_workers)
        self.convert_max_workers = max(1, convert_max_workers)
        self.ai_timeout = max(10.0, float(ai_timeout))
        self.unwrap_http_session = unwrap_http_session
        self.unwrap_resolver = unwrap_resolver or socket.getaddrinfo
        self.adapters = {
            "jd": get_adapter("jd"),
            "taobao": get_adapter("taobao"),
            "pdd": get_adapter("pdd"),
        }
        self.prompt_stage1_template = self._load_prompt_file(PROMPT_STAGE1_PATH)
        self.prompt_stage2_template = self._load_prompt_file(PROMPT_STAGE2_PATH)
        self._prompt_stage1_mtime = self._get_file_mtime_ns(PROMPT_STAGE1_PATH)
        self._prompt_stage2_mtime = self._get_file_mtime_ns(PROMPT_STAGE2_PATH)
        self._prompt_lock = threading.Lock()

        # 线程安全的有界 LRU 内存级转链结果缓存，避免不同采集群重复刷屏的同一链接/口令重复调用上游接口且杜绝内存无限膨胀
        self._cache_lock = threading.Lock()
        self._cache_warmed_up = False
        self.max_anchor_cache_size = MAX_ANCHOR_CACHE_SIZE
        self.anchor_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._shared_unwrap_session: Optional[requests.Session] = None
        self.stats_cache_hits = 0
        self.stats_ai1_calls = 0
        self.stats_ai1_model_hits = 0
        self.stats_ai1_fallbacks = 0
        self.stats_ai2_calls = 0
        self.stats_ai2_model_hits = 0
        self.stats_ai2_fallbacks = 0
        self.stats_ai_retries_scheduled = 0
        self.stats_early_terminated_pre_ai1 = 0
        self.stats_early_terminated_post_stage3 = 0
        self.stats_deal_upgrades_post_stage3 = 0
        self.last_batch_stage_timings: Dict[str, float] = {}

    def _set_anchor_cache_locked(self, key: str, value: Dict[str, Any]) -> None:
        if not key:
            return
        if key in self.anchor_cache:
            self.anchor_cache.move_to_end(key)
        self.anchor_cache[key] = value
        while len(self.anchor_cache) > self.max_anchor_cache_size:
            self.anchor_cache.popitem(last=False)

    def _get_anchor_cache_locked(self, key: str) -> Optional[Dict[str, Any]]:
        val = self.anchor_cache.get(key)
        if val is not None:
            self.anchor_cache.move_to_end(key)
        return val

    @staticmethod
    def _slim_adapter_res_for_cache(res: Any, *, keep_product: bool) -> Dict[str, Any]:
        if not keep_product or not isinstance(res, dict):
            return {}
        prod = res.get("product")
        if not isinstance(prod, dict) or not prod:
            return {}
        slim_prod = {k: v for k, v in prod.items() if k != "raw_item"}
        return {
            "status": "success",
            "product": slim_prod,
            "price": res.get("price"),
            "coupon_amount": res.get("coupon_amount"),
            "title": res.get("title"),
        }

    def _get_unwrap_session(self) -> Any:
        if self.unwrap_http_session is not None:
            return self.unwrap_http_session
        with self._cache_lock:
            if self._shared_unwrap_session is None:
                sess = requests.Session()
                adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32)
                sess.mount("http://", adapter)
                sess.mount("https://", adapter)
                self._shared_unwrap_session = sess
            return self._shared_unwrap_session

    def _can_run_live_unwrap(self) -> bool:
        """生产环境默认开启通用短链安全解包；离线单元测试未显式注入 `unwrap_http_session` 时自动静默跳过真实外网请求。"""
        if self.unwrap_http_session is not None:
            return True
        req_fn = getattr(requests.sessions.Session, "request", None)
        return getattr(req_fn, "__module__", "").startswith("requests")

    def _evict_transient_failed_cache(self) -> None:
        """
        每轮 Stage 3 结束后驱逐内存缓存中的暂态转链失败项 (`ERR_ADAPTER_CONVERT_FAILED`)，
        保证进入 `retry_pending` 的记录在下一轮扫描时能真正重新请求上游 API，
        同时保留确定性永久错误（如纯数字 SKU、商品已下架、非支持平台）避免无意义重复请求。
        """
        with self._cache_lock:
            stale_keys = [
                k
                for k, v in self.anchor_cache.items()
                if isinstance(v, dict)
                and v.get("convert_status") == "failed"
                and v.get("error_code") == ERR_ADAPTER_CONVERT_FAILED
            ]
            for k in stale_keys:
                self.anchor_cache.pop(k, None)

    @staticmethod
    def _build_anchor_cache_keys(plat: str, raw_anchor: str) -> List[str]:
        """生成锚点缓存的主键列表：除完整字符串外，若为淘口令则额外提取严格 11 位核心字母数字码作为二级索引键。"""
        s = str(raw_anchor or "").strip()
        if not s:
            return []
        keys = [f"{plat}:{s}"]
        if plat == "taobao" and not s.lower().startswith("http"):
            tk = extract_taobao_token_core(s)
            if len(tk) == 11:
                tk_key = f"taobao:tk:{tk}"
                if tk_key not in keys:
                    keys.append(tk_key)
        return keys

    def warmup_anchor_cache_from_db(self, raw_col) -> None:
        """
        启动时从初步表 `deal_raw_tips` 预热最近 12 小时内已成功转链的 `converted_links` 缓存，
        确保进程重启后遇到相同推广链接/口令也能 0ms 命中缓存并触发前置提前终止。
        """
        if self._cache_warmed_up or raw_col is None:
            return
        self._cache_warmed_up = True
        try:
            cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
                hours=DEAL_INFO_PROTECTION_HOURS
            )
            cursor = (
                raw_col.find(
                    {
                        "cleaned_at": {"$gte": cutoff},
                        "converted_links.0": {"$exists": True},
                    },
                    {"_id": 0, "converted_links": 1},
                )
                .sort("cleaned_at", -1)
                .limit(3000)
            )
            loaded = 0
            with self._cache_lock:
                for doc in cursor:
                    for cl in doc.get("converted_links") or []:
                        if not isinstance(cl, dict):
                            continue
                        c_status = cl.get("convert_status")
                        c_plat = str(cl.get("platform") or "").strip()
                        c_inp = self._strip_markdown_link_wrapper(str(cl.get("clean_input") or "").strip())
                        if c_plat not in ("jd", "taobao", "pdd") or not c_inp:
                            continue
                        p_link = str(cl.get("my_promo_link") or "").strip()
                        p_tok = str(cl.get("my_promo_token") or "").strip()
                        cl_copy = copy.deepcopy(cl)
                        cl_copy["clean_input"] = c_inp
                        cl_copy.pop("adapter_res", None)
                        if c_status == "product":
                            if not (p_link or p_tok):
                                continue
                        elif c_status == "coupon_or_activity":
                            # 防护历史限流期间未标记 resolution_kind 的京东短链/联盟跳转链误判 OK_COUPON 污染预热缓存
                            c_inp_low = c_inp.lower()
                            if (
                                c_plat == "jd"
                                and cl.get("error_code") == "OK_COUPON"
                                and cl.get("resolution_kind") != "coupon_or_venue"
                                and ("u.jd.com" in c_inp_low or "union-click.jd.com" in c_inp_low)
                            ):
                                continue
                            is_new_link = (
                                cl.get("error_code") != "OK_DIRECT_COUPON"
                                and _is_verified_station_promo_link(c_inp, p_link)
                            )
                            is_new_tok = (
                                cl.get("error_code") != "OK_DIRECT_COUPON"
                                and bool(p_tok)
                                and p_tok != c_inp
                            )
                            if is_new_link or is_new_tok:
                                rebuilt_promo = rebuild_clean_official_venue_url(p_link) if p_link else None
                                cl_copy["my_promo_link"] = p_link if is_new_link else rebuilt_promo
                                cl_copy["my_promo_token"] = p_tok if is_new_tok else None
                            else:
                                cand_venue = (
                                    p_link if cl.get("error_code") == "OK_DIRECT_COUPON" and p_link else c_inp
                                )
                                rebuilt = rebuild_clean_official_venue_url(cand_venue)
                                if not rebuilt:
                                    continue
                                cl_copy["error_code"] = "OK_DIRECT_COUPON"
                                cl_copy["my_promo_link"] = rebuilt
                                cl_copy["my_promo_token"] = None
                        else:
                            continue
                        for ckey in self._build_anchor_cache_keys(c_plat, c_inp):
                            if ckey not in self.anchor_cache:
                                self._set_anchor_cache_locked(ckey, cl_copy)
                                loaded += 1
            if loaded > 0:
                logger.info("🔥 [转链缓存预热] 已从 `deal_raw_tips` 恢复最近 12h 有效转链锚点 %d 个", loaded)
        except Exception as e:
            logger.warning("预热转链缓存异常: %s", e)

    @staticmethod
    def _get_file_mtime_ns(path: str) -> Optional[int]:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _load_prompt_file(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.error("读取 AI 提示词文件 [%s] 失败: %s", path, e)
            return ""

    def _resolve_stage1_path(self) -> str:
        sched = sys.modules.get("mobile_web.scripts.run_deal_pool_scheduler")
        return getattr(sched, "PROMPT_STAGE1_PATH", PROMPT_STAGE1_PATH)

    def _resolve_stage2_path(self) -> str:
        sched = sys.modules.get("mobile_web.scripts.run_deal_pool_scheduler")
        return getattr(sched, "PROMPT_STAGE2_PATH", PROMPT_STAGE2_PATH)

    def _get_stage1_prompt(self) -> str:
        p_path = self._resolve_stage1_path()
        cur_mtime = self._get_file_mtime_ns(p_path)
        if cur_mtime is not None and cur_mtime != getattr(self, "_prompt_stage1_mtime", None):
            with self._prompt_lock:
                if cur_mtime != getattr(self, "_prompt_stage1_mtime", None):
                    loaded = self._load_prompt_file(p_path)
                    if loaded:
                        self.prompt_stage1_template = loaded
                        self._prompt_stage1_mtime = cur_mtime
        return self.prompt_stage1_template

    def _get_stage2_prompt(self) -> str:
        p_path = self._resolve_stage2_path()
        cur_mtime = self._get_file_mtime_ns(p_path)
        if cur_mtime is not None and cur_mtime != getattr(self, "_prompt_stage2_mtime", None):
            with self._prompt_lock:
                if cur_mtime != getattr(self, "_prompt_stage2_mtime", None):
                    loaded = self._load_prompt_file(p_path)
                    if loaded:
                        self.prompt_stage2_template = loaded
                        self._prompt_stage2_mtime = cur_mtime
        return self.prompt_stage2_template

    @staticmethod
    def _extract_doc_images(msg_doc: Dict[str, Any]) -> List[str]:
        """确定性提取单条原始文档中的去重图片列表（代码直接提取，无需发给大模型复读）。"""
        imgs: List[str] = []
        primary_img = str(msg_doc.get("image_url") or "").strip()
        if primary_img:
            imgs.append(primary_img)
        for im in msg_doc.get("tutorial_images") or []:
            im_s = str(im or "").strip()
            if im_s and im_s not in imgs:
                imgs.append(im_s)
        return imgs

    @classmethod
    def _format_raw_message_for_ai1(cls, msg_doc: Dict[str, Any], is_target: bool = False) -> Dict[str, Any]:
        """
        将单条 `deal_raw_tips` 文档格式化为 Stage 1 AI 标准输入的 `raw_messages` 元素。
        确保 DOM 属性提取或源站中转解包出的链接/口令（如逛丢 `go.php`、白菜哦 `tgo`、好单库加密ID）
        100% 出现在 `content` 文本中，同时仅传 `image_count` 节省无意义的 URL Token 开销。
        """
        title = _normalize_inline_spaces(str(msg_doc.get("title") or "")).strip()
        tut = _normalize_inline_spaces(str(msg_doc.get("tutorial_text") or "")).strip()
        content = tut if tut else title
        if title and title not in content:
            content = f"【标题】{title}\n{content}".strip()

        extra_anchor_lines: List[str] = []
        for lk in msg_doc.get("extracted_links") or []:
            if not isinstance(lk, dict):
                continue
            u = _normalize_inline_spaces(html_lib.unescape(str(lk.get("url") or lk.get("item_id") or ""))).strip()
            lbl = str(lk.get("label") or "附带链接").strip()
            if u and u not in content:
                extra_anchor_lines.append(f"{lbl}: {u}")
        for tk in msg_doc.get("extracted_tokens") or []:
            tk_s = _normalize_inline_spaces(html_lib.unescape(str(tk or ""))).strip()
            if tk_s and tk_s not in content and not any(tk_s in ln for ln in extra_anchor_lines):
                extra_anchor_lines.append(f"附带口令/加密ID: {tk_s}")

        if extra_anchor_lines:
            content = f"{content}\n" + "\n".join(extra_anchor_lines)

        imgs = cls._extract_doc_images(msg_doc)
        return {
            "tip_id": str(msg_doc.get("source_item_id") or ""),
            "is_target": bool(is_target),
            "publish_time": str(msg_doc.get("publish_time") or msg_doc.get("fetched_at") or ""),
            "content": content.strip(),
            "image_count": len(imgs),
        }

    @classmethod
    def _assemble_combined_text_and_images(
        cls,
        merged_tip_ids: List[str],
        norm_targets: List[Dict[str, Any]],
        tip_map: Dict[str, Dict[str, Any]],
        orig_doc: Dict[str, Any],
    ) -> Tuple[str, List[str]]:
        """
        【确定性代码拼接与占位符替换】：
        不让 AI-1 复读长文本与图片 URL，直接由 Python 按 `merged_tip_ids` 顺序拼接文本与图片，
        并将 `raw_span`（回退 `clean_input`）按最长优先替换为 `[#L1]`、`[#L2]` 供 Stage 4 (AI-2) 阅读。
        """
        ordered_docs: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for tid in merged_tip_ids or []:
            tid_s = str(tid or "").strip()
            if tid_s and tid_s not in seen_ids and tid_s in tip_map:
                seen_ids.add(tid_s)
                ordered_docs.append(tip_map[tid_s])
        if not ordered_docs and orig_doc:
            ordered_docs.append(orig_doc)

        text_blocks: List[str] = []
        extracted_images: List[str] = []
        for nd in ordered_docs:
            msg_fmt = cls._format_raw_message_for_ai1(nd, is_target=False)
            c_txt = str(msg_fmt.get("content") or "").strip()
            if c_txt:
                text_blocks.append(c_txt)
            for im_u in cls._extract_doc_images(nd):
                if im_u not in extracted_images:
                    extracted_images.append(im_u)

        combined = "\n".join(text_blocks).strip()

        # 按匹配子串长度降序替换为唯一临时标记，避免短链接误伤长链接子串
        replace_jobs: List[Tuple[str, str, str]] = []
        for tgt in norm_targets or []:
            lid = str(tgt.get("link_id") or "").strip()
            if not lid:
                continue
            r_span = str(tgt.get("raw_span") or "").strip()
            c_inp = str(tgt.get("clean_input") or "").strip()
            cand = ""
            if r_span and len(r_span) >= 4 and r_span in combined:
                cand = r_span
            elif c_inp and len(c_inp) >= 4 and c_inp in combined:
                cand = c_inp
            replace_jobs.append((lid, cand, c_inp))

        unmatched_lids: List[str] = []
        for lid, cand, _c_inp in sorted(replace_jobs, key=lambda x: len(x[1]), reverse=True):
            tmp_tok = f"\x00_PLACEHOLDER_{lid}_\x00"
            if cand and cand in combined:
                combined = combined.replace(cand, f" {tmp_tok} ", 1)
            else:
                unmatched_lids.append(lid)

        for lid, _cand, _c_inp in replace_jobs:
            combined = combined.replace(f"\x00_PLACEHOLDER_{lid}_\x00", f"[#{lid}]")

        if unmatched_lids:
            combined = (combined + "\n" + " ".join(f"[#{lid}]" for lid in unmatched_lids)).strip()

        return combined, extracted_images

    def build_stage1_task_input(self, doc: Dict[str, Any], raw_col) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        """
        构造单条待清洗记录的 Stage 1 AI 输入：
        - 若 `is_group_stream=True`（折淘客采集群）：从 `deal_raw_tips` 查询同群 (`group_id`) 前后 ±3 分钟（`±180s`）内的相邻消息（含 `context_only` 纯文字/纯图片消息）；
        - 若 `is_group_stream=False`（网页/榜单独立帖）：仅包含自身单条消息（`raw_messages` 长度为 1）。
        返回 `(stage1_input_dict, tip_id_to_doc_map)`。
        """
        site = str(doc.get("source_site") or "")
        main_tip_id = str(doc.get("source_item_id") or "")
        is_group_stream = bool(
            doc.get("is_group_stream")
            or (site == "zhetaoke" and main_tip_id.startswith("xb_"))
        )
        group_id = str(doc.get("group_id") or (doc.get("hot_metrics") or {}).get("group_id") or "").strip()
        pub_ts = doc.get("publish_ts")
        if not isinstance(pub_ts, (int, float)):
            pub_ts = parse_publish_time_epoch(doc.get("publish_time"))

        neighbor_docs: List[Dict[str, Any]] = [doc]
        if is_group_stream and group_id and raw_col is not None and isinstance(pub_ts, (int, float)):
            try:
                cursor = (
                    raw_col.find(
                        {
                            "source_site": site,
                            "group_id": group_id,
                            "publish_ts": {"$gte": float(pub_ts) - 180.0, "$lte": float(pub_ts) + 180.0},
                        },
                        {"raw_payload": 0},
                    )
                    .sort([("publish_ts", 1), ("source_item_id", 1)])
                    .limit(7)
                )
                fetched_neighbors = list(cursor)
                if fetched_neighbors:
                    if not any(str(d.get("source_item_id")) == main_tip_id for d in fetched_neighbors):
                        fetched_neighbors.append(doc)
                    fetched_neighbors.sort(
                        key=lambda x: (
                            float(x.get("publish_ts") or pub_ts),
                            str(x.get("source_item_id") or ""),
                        )
                    )
                    neighbor_docs = fetched_neighbors
            except Exception as e:
                logger.warning("查询同群 [%s] 相邻 ±3 分钟消息异常: %s", group_id, e)

        tip_map: Dict[str, Dict[str, Any]] = {}
        raw_messages: List[Dict[str, Any]] = []
        for nd in neighbor_docs:
            tid = str(nd.get("source_item_id") or "")
            if not tid or tid in tip_map:
                continue
            tip_map[tid] = nd
            raw_messages.append(self._format_raw_message_for_ai1(nd, is_target=(tid == main_tip_id)))

        stage1_item = {
            "main_tip_id": main_tip_id,
            "source_site": site,
            "source_channel": str(doc.get("source_channel") or ""),
            "is_group_stream": is_group_stream,
            "raw_messages": raw_messages,
        }
        return stage1_item, tip_map

    @staticmethod
    def _strip_markdown_link_wrapper(raw_val: str) -> str:
        """若字符串被包裹在 `[...](https://...)` Markdown 语法中，提取圆括号内的真实 URL。"""
        s = html_lib.unescape(str(raw_val or "")).strip()
        if s.startswith("[") and "](" in s and s.endswith(")"):
            inner = s.split("](", 1)[-1][:-1].strip()
            if inner:
                return inner
        return s

    def _fallback_stage1_extract(self, doc: Dict[str, Any], fail_reason: str = "AI1_TIMEOUT_OR_EMPTY") -> Dict[str, Any]:
        """
        【遵循核心铁律：Fail-Closed + 严格重试计数】
        当 Stage 1 AI 超时或未返回某条记录的拆解结果时，不伪造拆解，标记 `_from_fallback=True`，
        交由外层跳过本轮处理并将 `clean_retry_count` + 1（累计满 3 次永久放弃）。
        """
        main_tip_id = str(doc.get("source_item_id") or "")
        msg = self._format_raw_message_for_ai1(doc, is_target=True)
        return {
            "main_tip_id": main_tip_id,
            "is_standalone_deal": True,
            "merged_tip_ids": [main_tip_id],
            "combined_raw_text": msg["content"].strip(),
            "extracted_images": self._extract_doc_images(doc),
            "extracted_targets": [],
            "deal_price": None,
            "tutorial_score": 50,
            "_from_fallback": True,
            "_fallback_reason": fail_reason,
        }

    def _call_ai_json_array(
        self,
        prompt_template: str,
        batch_payload: List[Dict[str, Any]],
        stage_label: str,
        *,
        _allow_bisect: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        调用项目统一 AI 接口 `generate_content`（严格限制超时 `self.ai_timeout` 且 `stop_on_timeout=True`、
        本地槽位忙 5s 快速切换备用模型）；若多条打包批次发生超时或截断空返回，自动对半切分重试一次防整批连坐。
        """
        if not batch_payload or not prompt_template:
            return []
        payload_json = json.dumps(batch_payload, ensure_ascii=False, indent=2)
        if stage_label == "AI-Stage1":
            full_prompt = f"{prompt_template}\n<deal_stage1_input>\n{payload_json}\n</deal_stage1_input>"
        else:
            full_prompt = (
                f"{prompt_template}\n\n"
                f"==================== 本批次待处理输入 JSON 数据 ====================\n"
                f"{payload_json}"
            )
        out_items: List[Dict[str, Any]] = []
        try:
            resp = generate_content(
                prompt=full_prompt,
                preset_model_group="low",
                max_retries_per_model=1,
                timeout=self.ai_timeout,
                stop_on_timeout=True,
                slot_acquire_timeout=5.0,
            )
            resp_text = ""
            if isinstance(resp, dict):
                if resp.get("status") != "✅ 成功":
                    detail = "；".join(str(err) for err in (resp.get("error_history") or []))
                    if resp.get("timed_out"):
                        logger.debug("[%s] 批次达到 AI 超时阈值 (%.0fs)，留待下一轮或切分重试", stage_label, self.ai_timeout)
                    else:
                        raise RuntimeError(detail or str(resp.get("content") or "模型调用失败"))
                else:
                    resp_text = str(resp.get("content") or "")
            elif resp:
                resp_text = str(resp)

            cleaned_text = resp_text.strip()
            if cleaned_text:
                # 剥离思考模型输出的 <thought>...</thought> 或 <thinking>...</thinking>，防止思维链中的 [#L1] 干扰 JSON 括号定位
                for close_tag in ("</thought>", "</thinking>"):
                    if close_tag in cleaned_text:
                        cleaned_text = cleaned_text.split(close_tag)[-1].strip()
                if "```" in cleaned_text:
                    parts = cleaned_text.split("```")
                    for seg in parts[1::2]:
                        body = seg.strip()
                        if body.lower().startswith("json"):
                            body = body[4:].strip()
                        if body.startswith("[") or body.startswith("{"):
                            cleaned_text = body
                            break
                parsed = string_to_object(cleaned_text)
                if isinstance(parsed, list):
                    out_items = [item for item in parsed if isinstance(item, dict)]
                elif isinstance(parsed, dict):
                    out_items = [parsed]
        except Exception as e:
            err_brief = str(e).splitlines()[0][:200]
            logger.debug("[%s] 调用模型组解析 JSON 偶发异常 (留待重试): %s", stage_label, err_brief)

        if (
            not out_items
            and _allow_bisect
            and len(batch_payload) >= 3
            and getattr(generate_content, "__module__", "").endswith("model_api")
        ):
            half = len(batch_payload) // 2
            left_out = self._call_ai_json_array(prompt_template, batch_payload[:half], stage_label, _allow_bisect=False)
            right_out = self._call_ai_json_array(prompt_template, batch_payload[half:], stage_label, _allow_bisect=False)
            return left_out + right_out
        return out_items

    def run_ai_stage1_batch(
        self,
        stage1_inputs: List[Dict[str, Any]],
        docs_by_tip_id: Dict[str, Dict[str, Any]],
        neighbor_maps_by_id: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        total_pending_count: int = 0,
        quiet_sub_logs: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        """并发执行 Stage 2（AI-1：原始拆解与转链提纯），并由 Python 确定性组装 `combined_raw_text` 与 `extracted_images`。"""
        results_by_id: Dict[str, Dict[str, Any]] = {}
        total_items = len(stage1_inputs)
        chunks = [
            stage1_inputs[i : i + self.ai_batch_size]
            for i in range(0, total_items, self.ai_batch_size)
        ]
        total_batches = len(chunks)

        prompt_tpl = self._get_stage1_prompt()

        def _worker(chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            self.stats_ai1_calls += 1
            return self._call_ai_json_array(prompt_tpl, chunk, "AI-Stage1")

        def _ingest_arr(arr: List[Dict[str, Any]]) -> None:
            for item in arr:
                mid = str(item.get("main_tip_id") or item.get("task_id") or "").strip()
                if mid and ":" in mid and mid not in docs_by_tip_id:
                    mid_suffix = mid.split(":", 1)[-1].strip()
                    if mid_suffix in docs_by_tip_id:
                        mid = mid_suffix
                if mid:
                    item["main_tip_id"] = mid
                    results_by_id[mid] = item

        if chunks:
            t_s1 = time.perf_counter()
            if total_batches == 1:
                try:
                    _ingest_arr(_worker(chunks[0]))
                except Exception as e:
                    logger.warning("[AI-Stage1] 单批次执行异常 (留待重试): %s", str(e).splitlines()[0][:200])
            else:
                done_batches = 0
                done_items = 0
                step_ai = max(2, total_batches // 4)
                ai_worker_count = min(self.ai_max_workers, total_batches)
                with ThreadPoolExecutor(max_workers=ai_worker_count) as pool:
                    futures = {pool.submit(_worker, ch): ch for ch in chunks}
                    for fut in as_completed(futures):
                        ch = futures[fut]
                        done_batches += 1
                        done_items += len(ch)
                        try:
                            _ingest_arr(fut.result())
                        except Exception as e:
                            err_msg = str(e).splitlines()[0][:250]
                            logger.warning(
                                "🚨 [AI-1 并发异常告警] 子批次(%d条)调用失败 (将留待重试) | 错误: %s | 当前AI并发=%d线程",
                                len(ch),
                                err_msg,
                                self.ai_max_workers,
                            )
                        if not quiet_sub_logs and (done_batches % step_ai == 0 or done_batches == total_batches):
                            elapsed_ai = time.perf_counter() - t_s1
                            spd_ai = done_items / max(0.1, elapsed_ai)
                            logger.info(
                                "  📊 [AI-1 拆解子进度 %02d/%02d组 (%3d%%)] 已完成=%3d/%3d条 | 耗时=%.1fs | 瞬时速度=%.2f条/s | 并发=%d线程",
                                done_batches,
                                total_batches,
                                int(done_batches * 100 / max(1, total_batches)),
                                done_items,
                                total_items,
                                elapsed_ai,
                                spd_ai,
                                ai_worker_count,
                            )

        # 若 AI-1 超时/未返回某条记录，标记 _from_fallback=True 走重试计数；若正常返回，由代码确定性组装文本与图片
        for inp in stage1_inputs:
            mid = str(inp.get("main_tip_id") or "")
            orig_doc = docs_by_tip_id.get(mid) or {}
            ai1_res = results_by_id.get(mid)
            if not isinstance(ai1_res, dict) or "extracted_targets" not in ai1_res:
                self.stats_ai1_fallbacks += 1
                ai1_res = self._fallback_stage1_extract(orig_doc)
                results_by_id[mid] = ai1_res
            else:
                self.stats_ai1_model_hits += 1
                ai1_res["_from_fallback"] = False

                raw_ts = ai1_res.get("tutorial_score")
                if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) and 0 <= raw_ts <= 100:
                    ai1_res["tutorial_score"] = int(raw_ts)
                else:
                    ai1_res["tutorial_score"] = 50

                raw_dp = ai1_res.get("deal_price")
                if isinstance(raw_dp, (int, float)) and not isinstance(raw_dp, bool) and 0.1 <= raw_dp <= 100000:
                    ai1_res["deal_price"] = round(float(raw_dp), 2)
                elif isinstance(raw_dp, str):
                    try:
                        val = float(re.sub(r"[^\d.]", "", raw_dp))
                        ai1_res["deal_price"] = round(val, 2) if 0.1 <= val <= 100000 else None
                    except Exception:
                        ai1_res["deal_price"] = None
                else:
                    ai1_res["deal_price"] = None

                norm_targets: List[Dict[str, Any]] = []
                for idx_t, tgt in enumerate(ai1_res.get("extracted_targets") or [], 1):
                    if not isinstance(tgt, dict):
                        continue
                    c_inp = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
                    if not c_inp:
                        continue
                    raw_role = str(tgt.get("text_role_hint") or "main_candidate").strip().lower()
                    tgt["link_id"] = str(tgt.get("link_id") or f"L{idx_t}").strip()
                    tgt["raw_span"] = _normalize_inline_spaces(str(tgt.get("raw_span") or "")).strip()
                    tgt["clean_input"] = c_inp
                    tgt["text_role_hint"] = _ROLE_HINT_NORMALIZE_MAP.get(raw_role, "main_candidate")
                    norm_targets.append(tgt)
                if not ai1_res.get("merged_tip_ids"):
                    ai1_res["merged_tip_ids"] = [mid]
                else:
                    ai1_res["merged_tip_ids"] = [
                        (str(x).split(":", 1)[-1].strip() if ":" in str(x) else str(x).strip())
                        for x in ai1_res["merged_tip_ids"]
                        if str(x or "").strip()
                    ]
                tip_map = (neighbor_maps_by_id or {}).get(mid) or ({mid: orig_doc} if orig_doc else {})
                # 确定性补齐：若源站结构化按钮 (extracted_links) 中存在 AI-1 漏提的联盟推广商品链或优惠券链接，自动补入候选
                seen_clean_inputs = {t["clean_input"] for t in norm_targets}
                for m_tid in ai1_res["merged_tip_ids"]:
                    src_d = tip_map.get(m_tid) or {}
                    s_site = str(src_d.get("source_site") or (orig_doc or {}).get("source_site") or "")
                    for lk in src_d.get("extracted_links") or []:
                        if not isinstance(lk, dict) or len(norm_targets) >= 8:
                            continue
                        u_cand = self._strip_markdown_link_wrapper(str(lk.get("url") or "").strip())
                        if not u_cand or u_cand in seen_clean_inputs:
                            continue
                        u_low = u_cand.lower()

                        # 1. 联盟推广商品链补齐（防止同帖仅提了纯数字 item.jd.com 而漏掉 union-click/u.jd.com）
                        is_venue = rebuild_clean_official_venue_url(u_cand) is not None
                        if not is_venue and any(
                            dom in u_low
                            for dom in (
                                "u.jd.com/",
                                "union-click.jd.com/",
                                "jingfen.jd.com/detail/",
                                "s.click.taobao.com/",
                                "m.tb.cn/",
                                "e.tb.cn/",
                                "p.pinduoduo.com/",
                                "yangkeduo.com/",
                            )
                        ):
                            seen_clean_inputs.add(u_cand)
                            next_lid = f"L{len(norm_targets) + 1}"
                            norm_targets.append({
                                "link_id": next_lid,
                                "raw_span": u_cand,
                                "clean_input": u_cand,
                                "platform": detect_candidate_platform(u_cand, str(lk.get("platform") or src_d.get("platform") or "unknown")),
                                "text_role_hint": "main_candidate",
                            })
                            continue

                        # 2. 方案二：仅针对具有确定性 HTML DOM 结构的“逛丢”平台，确定性补齐漏提的官方优惠券链接
                        if s_site == "guangdiu" and "duo_coupon_landing" not in u_low:
                            t_low = str(lk.get("type") or "").strip().lower()
                            lbl = str(lk.get("label") or "").strip()
                            is_explicit_cpn = (
                                is_venue
                                or t_low in ("coupon_url", "tb_coupon")
                                or ("uland.taobao.com" in u_low and "/coupon/" in u_low)
                                or (
                                    ("券" in lbl or "补贴" in lbl or "红包" in lbl)
                                    and any(
                                        p in u_low
                                        for p in (
                                            "jd.com",
                                            "taobao.com",
                                            "tmall.com",
                                            "yangkeduo.com",
                                            "pinduoduo.com",
                                        )
                                    )
                                )
                            )
                            if is_explicit_cpn:
                                c_plat = detect_candidate_platform(u_cand, str(lk.get("platform") or src_d.get("platform") or "unknown"))
                                if c_plat in ("jd", "taobao", "pdd"):
                                    seen_clean_inputs.add(u_cand)
                                    next_lid = f"L{len(norm_targets) + 1}"
                                    norm_targets.append({
                                        "link_id": next_lid,
                                        "raw_span": u_cand,
                                        "clean_input": u_cand,
                                        "platform": c_plat,
                                        "text_role_hint": "coupon_candidate",
                                    })
                ai1_res["extracted_targets"] = norm_targets
                raw_strips = ai1_res.get("strip_spans")
                ai1_res["strip_spans"] = (
                    [_normalize_inline_spaces(str(x)).strip() for x in raw_strips if str(x or "").strip()]
                    if isinstance(raw_strips, list)
                    else []
                )
                assembled_text, assembled_imgs = self._assemble_combined_text_and_images(
                    ai1_res["merged_tip_ids"],
                    norm_targets,
                    tip_map,
                    orig_doc,
                )
                ai1_res["combined_raw_text"] = assembled_text
                ai1_res["extracted_images"] = assembled_imgs

        return results_by_id

    def convert_single_target(self, target: Dict[str, Any], default_plat: str = "unknown") -> Dict[str, Any]:
        """
        【Stage 3：官方 API 转链与底牌分类（100% 确定性代码，支持多线程并发与线程安全缓存）】
        对 Stage 1 AI 拆出的单个 `target` (`link_id`, `clean_input`, `platform`, `text_role_hint`) 调用官方 Adapter 转链，
        精准分类为 `"product"`（具体单品）、`"coupon_or_activity"`（优惠券/活动会场）或 `"failed"`（死链/不支持）。
        """
        link_id = str(target.get("link_id") or "L1").strip()
        clean_input = self._strip_markdown_link_wrapper(str(target.get("clean_input") or ""))
        role_hint = str(target.get("text_role_hint") or "main_candidate").strip()
        hint_plat = str(target.get("platform") or default_plat or "unknown").strip().lower()
        plat = detect_candidate_platform(clean_input, hint_plat if hint_plat in ("jd", "taobao", "pdd") else default_plat)

        if not clean_input:
            return {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "platform": plat,
                "error_code": ERR_NO_ANCHOR,
                "error_msg": "空链接或口令",
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }

        if plat == "unsupported_other":
            return {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "platform": plat,
                "error_code": ERR_UNSUPPORTED_PLATFORM,
                "error_msg": f"非淘京拼支持平台外链: {clean_input[:80]}",
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }

        # 前置免白耗通道：纯公共无佣金领券/消费券页直接按白名单重组返回，省去必败的上游转链 API 调用
        pre_direct_url = _precheck_direct_non_cps_venue_url(clean_input)
        if pre_direct_url:
            direct_plat = detect_candidate_platform(pre_direct_url, plat if plat in ("jd", "taobao", "pdd") else "unknown")
            direct_keys = self._build_anchor_cache_keys(direct_plat, clean_input)
            with self._cache_lock:
                hit_direct = next((k for k in direct_keys if k in self.anchor_cache), None)
                if hit_direct is not None:
                    entry = self._get_anchor_cache_locked(hit_direct)
                    if entry is not None and entry.get("convert_status") == "coupon_or_activity":
                        cached = copy.deepcopy(entry)
                        cached["link_id"] = link_id
                        cached["text_role_hint"] = role_hint
                        self.stats_cache_hits += 1
                        return cached
            out = {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "coupon_or_activity",
                "resolution_kind": "coupon_or_venue",
                "platform": direct_plat,
                "goods_id": "",
                "goods_sign": "",
                "error_code": "OK_DIRECT_COUPON",
                "error_msg": "",
                "official_product": None,
                "my_promo_link": pre_direct_url,
                "my_promo_token": None,
                "adapter_res": {},
            }
            with self._cache_lock:
                for ck in direct_keys:
                    self._set_anchor_cache_locked(ck, out)
            return copy.deepcopy(out)

        platforms_to_try = [plat] if plat in ("jd", "taobao", "pdd") else ["taobao", "jd", "pdd"]
        last_failed: Optional[Dict[str, Any]] = None
        pending_failed_cache: List[Tuple[List[str], Dict[str, Any]]] = []
        had_cache_miss = False
        any_detail_lookup_failed = False

        is_external_http_shortlink = (
            clean_input.lower().startswith(("http://", "https://"))
            and not _is_official_ecommerce_http_url(clean_input)
        )

        for try_plat in platforms_to_try:
            cache_keys = self._build_anchor_cache_keys(try_plat, clean_input)
            with self._cache_lock:
                hit_key = next((k for k in cache_keys if k in self.anchor_cache), None)
                if hit_key is not None:
                    entry = self._get_anchor_cache_locked(hit_key) or {}
                    cached = copy.deepcopy(entry)
                    cached["link_id"] = link_id
                    cached["text_role_hint"] = role_hint
                    c_status = cached.get("convert_status")
                    if c_status == "coupon_or_activity":
                        self.stats_cache_hits += 1
                        return cached
                    if c_status == "product":
                        # 仅当缓存中包含完整的 adapter_res.product 字典时才直接复用；
                        # 若是从数据库预热的精简缓存（无 adapter_res）且未能触发前置早停（说明需要作为新商品入库），则重新请求官方接口补全完整字段
                        c_ares = cached.get("adapter_res")
                        if isinstance(c_ares, dict) and isinstance(c_ares.get("product"), dict) and c_ares.get("product"):
                            self.stats_cache_hits += 1
                            return cached
                    else:
                        self.stats_cache_hits += 1
                        last_failed = cached
                        continue

            if is_external_http_shortlink:
                had_cache_miss = True
                out = {
                    "link_id": link_id,
                    "clean_input": clean_input,
                    "text_role_hint": role_hint,
                    "convert_status": "failed",
                    "resolution_kind": "error",
                    "platform": try_plat,
                    "goods_id": "",
                    "goods_sign": "",
                    "error_code": ERR_ADAPTER_CONVERT_FAILED,
                    "error_msg": "第三方短链未命中官方公共领券白名单",
                    "official_product": None,
                    "my_promo_link": None,
                    "my_promo_token": None,
                    "adapter_res": {},
                }
                for p_tag in platforms_to_try:
                    pending_failed_cache.append((self._build_anchor_cache_keys(p_tag, clean_input), out))
                last_failed = out
                break

            adapter = self.adapters.get(try_plat)
            if not adapter:
                continue
            had_cache_miss = True

            try:
                with api_caller_context("background"):
                    res = adapter.resolve_and_convert_universal(clean_input)
            except Exception as exc:
                out = {
                    "link_id": link_id,
                    "clean_input": clean_input,
                    "text_role_hint": role_hint,
                    "convert_status": "failed",
                    "resolution_kind": "error",
                    "platform": try_plat,
                    "error_code": ERR_ADAPTER_CONVERT_FAILED,
                    "error_msg": f"Adapter({try_plat})调用异常: {exc}",
                    "official_product": None,
                    "my_promo_link": None,
                    "my_promo_token": None,
                    "adapter_res": {},
                }
                pending_failed_cache.append((cache_keys, out))
                last_failed = out
                continue

            is_degraded_product = bool(
                isinstance(res, dict)
                and (
                    res.get("detail_lookup_failed")
                    or res.get("resolution_kind") == "degraded_product"
                )
            )
            if is_degraded_product:
                any_detail_lookup_failed = True

            if isinstance(res, dict) and res.get("status") == "success":
                goods_id = str(res.get("goods_id") or "").strip()
                goods_sign = str(res.get("goods_sign") or "").strip()
                product_card = res.get("product")
                promo_link = str(res.get("final_link") or res.get("url") or "").strip()
                promo_token = str(res.get("promotion_token") or "").strip()

                if goods_id and goods_sign and isinstance(product_card, dict) and (promo_link or promo_token):
                    official_price = float(product_card.get("price") or res.get("price") or 0.0)
                    orig_price = float(product_card.get("original_price") or official_price)
                    act_price = float(product_card.get("activity_price") or official_price)
                    cpn_amt = float(product_card.get("coupon_amount") or res.get("coupon_amount") or 0.0)
                    slim_ares = self._slim_adapter_res_for_cache(res, keep_product=True)
                    out = {
                        "link_id": link_id,
                        "clean_input": clean_input,
                        "text_role_hint": role_hint,
                        "convert_status": "product",
                        "resolution_kind": "product",
                        "platform": try_plat,
                        "goods_id": goods_id,
                        "goods_sign": goods_sign,
                        "error_code": "OK",
                        "error_msg": "",
                        "official_product": {
                            "title": str(product_card.get("name") or res.get("title") or "").strip(),
                            "price": round(official_price, 2),
                            "original_price": round(orig_price, 2),
                            "activity_price": round(act_price, 2),
                            "coupon_amount": round(cpn_amt, 2),
                            "shop_name": str(product_card.get("mall_name") or "").strip(),
                            "commission_rate": float(product_card.get("commission_rate") or res.get("commission_rate") or 0.0),
                            "commission_amount": float(product_card.get("commission_amount") or res.get("commission_amount") or 0.0),
                            "image_url": str(product_card.get("image_url") or res.get("image_url") or res.get("pict_url") or "").strip(),
                            "carousel_images": product_card.get("carousel_images") or res.get("carousel_images") or [],
                            "category": str(product_card.get("category") or product_card.get("category_name") or ""),
                            "brand": str(product_card.get("brand") or product_card.get("brand_name") or ""),
                            "sales_tip": str(product_card.get("sales_tip") if product_card.get("sales_tip") is not None else "0"),
                        },
                        "my_promo_link": promo_link or None,
                        "my_promo_token": promo_token or None,
                        "adapter_res": slim_ares,
                    }
                    with self._cache_lock:
                        for ck in cache_keys:
                            self._set_anchor_cache_locked(ck, out)
                    return copy.deepcopy(out)

                # 【通道 1：我方专属联盟新链】：强校验官方联盟推广域且归一化路径/参数严禁等于原输入，或生成不同于原输入的专属口令
                # 严禁将详情反查限流降级的单品 (`degraded_product`) 误归类为纯优惠券 `OK_COUPON`！
                if not is_degraded_product:
                    is_new_station_link = _is_verified_station_promo_link(clean_input, promo_link)
                    is_new_token = bool(promo_token) and promo_token != clean_input
                    rebuilt_promo = rebuild_clean_official_venue_url(promo_link) if promo_link else None
                    if is_new_station_link or is_new_token:
                        out = {
                            "link_id": link_id,
                            "clean_input": clean_input,
                            "text_role_hint": role_hint,
                            "convert_status": "coupon_or_activity",
                            "resolution_kind": "coupon_or_venue",
                            "platform": try_plat,
                            "goods_id": "",
                            "goods_sign": "",
                            "error_code": "OK_COUPON",
                            "error_msg": "",
                            "official_product": None,
                            "my_promo_link": promo_link if is_new_station_link else rebuilt_promo,
                            "my_promo_token": promo_token if is_new_token else None,
                            "adapter_res": {},
                        }
                        with self._cache_lock:
                            for ck in cache_keys:
                                self._set_anchor_cache_locked(ck, out)
                        return copy.deepcopy(out)
                    if rebuilt_promo:
                        venue_plat = detect_candidate_platform(rebuilt_promo, try_plat)
                        out = {
                            "link_id": link_id,
                            "clean_input": clean_input,
                            "text_role_hint": role_hint,
                            "convert_status": "coupon_or_activity",
                            "resolution_kind": "coupon_or_venue",
                            "platform": venue_plat,
                            "goods_id": "",
                            "goods_sign": "",
                            "error_code": "OK_DIRECT_COUPON",
                            "error_msg": "",
                            "official_product": None,
                            "my_promo_link": rebuilt_promo,
                            "my_promo_token": None,
                            "adapter_res": {},
                        }
                        with self._cache_lock:
                            for p_tag in {try_plat, venue_plat}:
                                for ck in self._build_anchor_cache_keys(p_tag, clean_input):
                                    self._set_anchor_cache_locked(ck, out)
                        return copy.deepcopy(out)

            raw_code = str((res or {}).get("error_code") or "")
            raw_msg = (
                "商品详情反查触发临时限流或网络波动"
                if is_degraded_product
                else str((res or {}).get("error_msg") or "转链未返回有效结果")
            )
            if raw_code == "UNSUPPORTED_TB_NUMERIC_ID" or (try_plat == "taobao" and "不支持数字id" in raw_msg):
                err_code = ERR_TB_NUMERIC_ID
            elif raw_code == "UNSUPPORTED_PDD_ID_ONLY_URL" or (try_plat == "pdd" and "40009" in raw_msg):
                err_code = ERR_PDD_NUMERIC_ID
            elif raw_code == "UNSUPPORTED_JD_NUMERIC_SKU" or (try_plat == "jd" and "不支持数字id" in raw_msg):
                err_code = ERR_JD_NUMERIC_SKU
            elif not is_degraded_product and (
                raw_code == "PRODUCT_OFF_PROMOTION"
                or any(k in raw_msg for k in ("下架", "不在推广", "取消推广", "不支持该商品"))
            ):
                err_code = ERR_ADAPTER_OFF_PROMOTION
            else:
                err_code = ERR_ADAPTER_CONVERT_FAILED

            out = {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "resolution_kind": "degraded_product" if is_degraded_product else "error",
                "platform": try_plat,
                "goods_id": "",
                "goods_sign": "",
                "error_code": err_code,
                "error_msg": raw_msg,
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }
            pending_failed_cache.append((cache_keys, out))
            last_failed = out

        # 【通道 2：零域名预设通用安全解包 -> 4 类官方纯净无佣金页重组】（非详情限流场景下统一裁决）
        if had_cache_miss and not any_detail_lookup_failed:
            rebuilt_venue_url = rebuild_clean_official_venue_url(clean_input)
            if not rebuilt_venue_url and self._can_run_live_unwrap():
                rebuilt_venue_url = unwrap_to_official_public_venue_url(
                    clean_input,
                    http_session=self._get_unwrap_session(),
                    resolver=self.unwrap_resolver,
                )
            if rebuilt_venue_url:
                venue_plat = detect_candidate_platform(
                    rebuilt_venue_url,
                    plat if plat in ("jd", "taobao", "pdd") else "unknown",
                )
                out = {
                    "link_id": link_id,
                    "clean_input": clean_input,
                    "text_role_hint": role_hint,
                    "convert_status": "coupon_or_activity",
                    "resolution_kind": "coupon_or_venue",
                    "platform": venue_plat,
                    "goods_id": "",
                    "goods_sign": "",
                    "error_code": "OK_DIRECT_COUPON",
                    "error_msg": "",
                    "official_product": None,
                    "my_promo_link": rebuilt_venue_url,
                    "my_promo_token": None,
                    "adapter_res": {},
                }
                with self._cache_lock:
                    for p_tag in set(platforms_to_try) | {venue_plat}:
                        if p_tag in ("jd", "taobao", "pdd"):
                            for ck in self._build_anchor_cache_keys(p_tag, clean_input):
                                self._set_anchor_cache_locked(ck, out)
                return copy.deepcopy(out)

        if pending_failed_cache:
            with self._cache_lock:
                for cks, failed_out in pending_failed_cache:
                    for ck in cks:
                        self._set_anchor_cache_locked(ck, failed_out)

        return copy.deepcopy(last_failed) if last_failed else {
            "link_id": link_id,
            "clean_input": clean_input,
            "text_role_hint": role_hint,
            "convert_status": "failed",
            "platform": plat,
            "goods_id": "",
            "goods_sign": "",
            "error_code": ERR_ADAPTER_CONVERT_FAILED,
            "error_msg": "未能识别平台或转链失败",
            "official_product": None,
            "my_promo_link": None,
            "my_promo_token": None,
            "adapter_res": {},
        }

    def run_stage3_concurrent_conversions(
        self,
        unique_targets: List[Tuple[Dict[str, Any], str]],
        total_pending_count: int = 0,
        quiet_sub_logs: bool = False,
    ) -> None:
        """
        并发执行 Stage 3 官方 API 转链：
        - 淘宝/拼多多通过多线程池全速并发转链；
        - 京东按折淘客 `appkey` 池规模匹配受控并发车道（1 Key ≈ 1 通道，配合单 Key 限流器避免队列积压超时与上游突发限流）。
        """
        if not unique_targets:
            return

        total_targets = len(unique_targets)
        step = max(20, total_targets // 5)
        t_s3 = time.perf_counter()
        done_cnt = 0
        prog_lock = threading.Lock()

        jd_targets: List[Tuple[Dict[str, Any], str]] = []
        other_targets: List[Tuple[Dict[str, Any], str]] = []
        for it in unique_targets:
            tgt, def_plat = it
            c_inp = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
            hint_p = str(tgt.get("platform") or def_plat or "unknown").strip().lower()
            det_p = detect_candidate_platform(c_inp, hint_p if hint_p in ("jd", "taobao", "pdd") else def_plat)
            if det_p == "jd":
                jd_targets.append(it)
            else:
                other_targets.append(it)

        # 按实际 ZTK appkey 数量控制 JD 并发车道（1 Key 对应 1 通道，上限 6 线程），避免多线程打穿 0.85s 限流队列；
        # 其余并发槽位全量留给淘宝/拼多多。
        jd_adapter = self.adapters.get("jd")
        ztk_key_count = len(getattr(getattr(jd_adapter, "client", None), "appkey_list", ()) or ())
        jd_lane_cap = min(max(2, ztk_key_count), 6) if ztk_key_count > 0 else 2
        jd_lane_count = min(self.convert_max_workers, jd_lane_cap, len(jd_targets))
        if not other_targets:
            other_slots = 0
        else:
            other_slots = min(max(1, self.convert_max_workers - jd_lane_count), len(other_targets))
        worker_slots = max(1, other_slots + jd_lane_count)

        if not quiet_sub_logs:
            logger.info(
                "🔗 [Stage 3-官方转链启动] 本批共核验=%d个独立链接/口令 (JD=%d, 其它=%d) | 并发线程分配: JD车道=%d, 其它车道=%d, 总并发上限=%d线程",
                total_targets,
                len(jd_targets),
                len(other_targets),
                jd_lane_count,
                other_slots,
                worker_slots,
            )

        def _record_done() -> None:
            nonlocal done_cnt
            with prog_lock:
                done_cnt += 1
                cur_done = done_cnt
            if not quiet_sub_logs and (cur_done % step == 0 or cur_done == total_targets):
                elapsed_s3 = time.perf_counter() - t_s3
                spd_s3 = cur_done / max(0.1, elapsed_s3)
                logger.info(
                    "  📊 [Stage 3-官方转链 子进度 %3d/%3d个 (%3d%%)] 已核验=%3d个 | 耗时=%.1fs | 瞬时速度=%.1f个/s (约%.1f条线报/分) | 并发=%d线程",
                    cur_done,
                    total_targets,
                    int(cur_done * 100 / max(1, total_targets)),
                    cur_done,
                    elapsed_s3,
                    spd_s3,
                    spd_s3 * 60 / 2.6,
                    worker_slots,
                )

        def _conv_worker(item: Tuple[Dict[str, Any], str]) -> None:
            tgt, def_plat = item
            c_inp = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
            try:
                res = self.convert_single_target(tgt, default_plat=def_plat)
                # 详细埋点：实时感知上游频控、限流或风控异常（排除系统内部正常的无卡片降级重试占位文案）
                err_msg = str((res or {}).get("error_msg") or "")
                err_code = str((res or {}).get("error_code") or "")
                is_internal_card_degraded = "商品详情反查" in err_msg
                if not is_internal_card_degraded and any(k in err_msg for k in ("频繁", "频控", "429", "限流", "风控", "黑名单", "系统繁忙")):
                    logger.warning(
                        "🚨 [Stage 3 转链上游频控/风控预警] input=%s | code=%s | msg=%s | 当前转链并发=%d",
                        c_inp[:60],
                        err_code,
                        err_msg[:120],
                        self.convert_max_workers,
                    )
            except Exception as e:
                logger.warning(
                    "🚨 [Stage 3 转链执行异常] input=%s | 异常: %s | 当前转链并发=%d",
                    c_inp[:60],
                    str(e)[:150],
                    self.convert_max_workers,
                )
            finally:
                _record_done()

        if total_targets == 1:
            _conv_worker(unique_targets[0])
            return

        jd_iter = iter(jd_targets)
        jd_iter_lock = threading.Lock()

        def _jd_lane_runner() -> None:
            while True:
                with jd_iter_lock:
                    jd_it = next(jd_iter, None)
                if jd_it is None:
                    break
                _conv_worker(jd_it)

        with ThreadPoolExecutor(max_workers=worker_slots) as pool:
            futures = []
            for _ in range(jd_lane_count):
                futures.append(pool.submit(_jd_lane_runner))
            for o_it in other_targets:
                futures.append(pool.submit(_conv_worker, o_it))
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    pass

    def run_ai_stage2_batch(
        self,
        stage2_inputs: List[Dict[str, Any]],
        total_pending_count: int = 0,
        quiet_sub_logs: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        """并发执行 Stage 4（AI-2：底牌终审、教程重构与质量评分），返回 `main_tip_id -> ai2_result` 字典。"""
        results_by_id: Dict[str, Dict[str, Any]] = {}
        if not stage2_inputs:
            return results_by_id

        total_items = len(stage2_inputs)
        bs2 = max(1, getattr(self, "ai2_batch_size", 6))
        chunks = [
            stage2_inputs[i : i + bs2]
            for i in range(0, total_items, bs2)
        ]
        total_batches = len(chunks)
        t_s4 = time.perf_counter()

        prompt_tpl = self._get_stage2_prompt()

        def _worker(chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            self.stats_ai2_calls += 1
            return self._call_ai_json_array(prompt_tpl, chunk, "AI-Stage2")

        def _ingest_arr(arr: List[Dict[str, Any]]) -> None:
            for item in arr:
                mid = str(item.get("main_tip_id") or item.get("task_id") or "").strip()
                if mid and ":" in mid:
                    mid = mid.split(":", 1)[-1].strip()
                if mid:
                    item["main_tip_id"] = mid
                    results_by_id[mid] = item

        if total_batches == 1:
            try:
                _ingest_arr(_worker(chunks[0]))
            except Exception as e:
                logger.debug("[AI-Stage2] 批次偶发执行异常 (留待重试): %s", str(e).splitlines()[0][:200])
        else:
            done_batches = 0
            done_items = 0
            with ThreadPoolExecutor(max_workers=min(self.ai_max_workers, total_batches)) as pool:
                futures = {pool.submit(_worker, ch): ch for ch in chunks}
                for fut in as_completed(futures):
                    ch = futures[fut]
                    done_batches += 1
                    done_items += len(ch)
                    try:
                        _ingest_arr(fut.result())
                    except Exception as e:
                        logger.debug("[AI-Stage2] 并发批次偶发执行异常 (留待重试): %s", str(e).splitlines()[0][:200])
                    if not quiet_sub_logs:
                        logger.debug(
                            "  📊 [阶段4-AI终审 子进度 %02d/%02d批 (%3d%%)] 本批已终审=%3d/%3d条 | 已耗时=%.1fs",
                            done_batches,
                            total_batches,
                            int(done_batches * 100 / max(1, total_batches)),
                            done_items,
                            total_items,
                            time.perf_counter() - t_s4,
                        )

        return results_by_id

    @staticmethod
    def _fallback_stage2_evaluate(s2_input: Dict[str, Any]) -> Dict[str, Any]:
        """
        【遵循核心铁律：Fail-Closed】
        当 Stage 2 AI 未返回某条记录的终审结果时，严禁用本地规则伪造 72 分及格分蒙混入正式库 `products`，
        直接标记为 `is_valid_deal=False` 拦截，确保进入正式库的每一件商品 100% 经过 AI-2 铁面验真。
        """
        mid = str(s2_input.get("main_tip_id") or "")
        return {
            "main_tip_id": mid,
            "is_valid_deal": False,
            "reject_reason": "AI2_AUDIT_UNAVAILABLE",
            "main_link_id": None,
            "quality_score": 0,
            "_from_fallback": True,
        }

    @staticmethod
    def _sanitize_clean_tutorial_step(text: str) -> str:
        """确保购买教程步骤为 100% 纯文字规范指引：剥离偶发残留的 [#L1] 占位符、裸网址与口令壳。"""
        out = _normalize_inline_spaces(str(text or ""))
        out = re.sub(r"\s*\[#L\d+\]", "", out)
        out = re.sub(r"[（(]\s*口令\s*[:：][^）)]*[）)]", "", out)
        out = re.sub(r"(?:#小程序|mp)://\S+", "", out)
        out = re.sub(r"https?://[^\s\u4e00-\u9fa5\"'<>）)\]]+", "", out)
        out = re.sub(
            r"(?<![A-Za-z0-9])(?:u\.jd\.com|3\.cn|m\.tb\.cn|s\.click\.taobao\.com|p\.pinduoduo\.com)/[A-Za-z0-9_.\-?=&/%]+",
            "",
            out,
        )
        out = re.sub(
            r"(?i)\d{0,2}[ \t]*[￥¥$€£₴¢!！（(《「/]{1,2}[ \t]*[A-Za-z0-9]{8,16}[ \t]*[￥¥$€£₴¢!！）)》」/]{1,2}(?:[ \t]*:?/{1,2}(?:[ \t]*(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*)?|[ \t]+(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*)?",
            "",
            out,
        )
        out = re.sub(r"[ \t]{2,}", " ", out)
        return out.strip()

    _CARD_HIGHLIGHT_FLUFF = frozenset({
        "好价推荐", "超值优惠", "限时特惠", "精选好物", "点击查看", "无", "null", "none",
    })

    @classmethod
    def _sanitize_card_highlight(cls, raw_val: Any) -> str:
        """提纯 Stage 2 AI 生成的缩略图超短亮点（复用无链清理，剔除空洞套话并截断至 16 字）。"""
        if not isinstance(raw_val, str):
            return ""
        text = raw_val.replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return ""
        text = cls._sanitize_clean_tutorial_step(text)
        text = text.strip(" \t\r\n-·•:：|｜,，。!！;；\"'“”‘’[]【】()")
        if not text or text.lower() in cls._CARD_HIGHLIGHT_FLUFF:
            return ""
        return text[:16].rstrip(" \t-·•:：|｜,，;；")

    @staticmethod
    def _sanitize_polished_copywriting(text: str) -> str:
        """对 AI-2 微整形后的 `polished_copywriting` 执行确定性无链校验，杜绝偶发残留占位符或链接。"""
        out = _normalize_inline_spaces(str(text or "")).strip()
        if not out:
            return ""
        out = re.sub(r"【来源采集群:\s*(?:【[^】]*】)?[^】]*】\s*", "", out)
        out = re.sub(r"【折淘客[^】]*】", "", out)
        out = re.sub(r"【[^】]*拔草哦[^】]*】\s*", "", out)
        out = re.sub(r"\s*\[#L\d+\]", "", out)
        out = re.sub(r"(?:#小程序|mp)://\S+", "", out)
        out = re.sub(r"https?://[^\s\u4e00-\u9fa5\"'<>）)\]]+", "", out)
        out = re.sub(
            r"(?<![A-Za-z0-9])(?:u\.jd\.com|3\.cn|m\.tb\.cn|s\.click\.taobao\.com|p\.pinduoduo\.com)/[A-Za-z0-9_.\-?=&/%]+",
            "",
            out,
        )
        out = re.sub(
            r"(?i)\d{0,2}[ \t]*[￥¥$€£₴¢!！（(《「/]{1,2}[ \t]*[A-Za-z0-9]{8,16}[ \t]*[￥¥$€£₴¢!！）)》」/]{1,2}(?:[ \t]*:?/{1,2}(?:[ \t]*(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*)?|[ \t]+(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*)?",
            "",
            out,
        )
        kept_lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        return "\n".join(kept_lines).strip()

    @staticmethod
    def _is_explicit_coupon_target(c: Dict[str, Any]) -> bool:
        """判定目标是否为原始线报中的优惠券/活动候选链接。"""
        if not isinstance(c, dict):
            return False
        clean_input = str(c.get("clean_input") or "").strip().lower()
        host, path = "", ""
        if clean_input.startswith(("http://", "https://")):
            try:
                parts = urlsplit(clean_input)
                host = (parts.hostname or "").lower()
                path = parts.path or ""
            except Exception:
                host, path = "", ""
            if host == "shop.m.jd.com" and path == "/mshop/gethomepage":
                return False
            if host == "store.taobao.com" and path == "/shop/view_shop.htm":
                return False
        if str(c.get("text_role_hint") or "").strip().lower() == "coupon_candidate":
            return True
        if str(c.get("convert_status") or "").strip().lower() == "coupon_or_activity":
            return True
        if host:
            if host in ("coupon.m.jd.com", "duo_coupon_landing.html"):
                return True
            if host == "shop.m.jd.com" and path.startswith("/member/"):
                return True
            if host == "uland.taobao.com":
                if "/quan/" in path:
                    return True
                if "/edetail" in path:
                    return False
                if "/coupon/" in path:
                    return True
            if host in ("pro.m.jd.com", "prodev.m.jd.com") and "/mall/active" in path:
                return True
            if host in ("pages.tmall.com", "pages-fast.m.taobao.com") and path.startswith("/wow/"):
                return True
            if "coupon" in path.lower() or "quan" in path.lower():
                return True
        return False

    @classmethod
    def _build_clean_original_copywriting(
        cls,
        merged_tip_ids: List[str],
        tip_map: Dict[str, Dict[str, Any]],
        ai1_targets: List[Dict[str, Any]],
        doc: Optional[Dict[str, Any]] = None,
        strip_spans: Optional[List[str]] = None,
    ) -> str:
        """
        生成保留原博主“活人感话术”（原始用词、防屏蔽黑话、语气、Emoji、标点与换行排版）的纯净无链文案初稿：
        - 仅剔除他人的推广链接、淘口令/京口令、加密商品 ID，以及采集源站拼接头（如 `【来源采集群:...】`、`【...拔草哦...】`）；
        - 绝不注入我方推广链接/口令（我方推广链接单独存放在 `my_promotion_link` 与 `coupon_links`）。
        """
        ordered_docs: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for tid in merged_tip_ids or []:
            tid_s = str(tid or "").strip()
            if tid_s and tid_s not in seen_ids and tid_s in (tip_map or {}):
                seen_ids.add(tid_s)
                ordered_docs.append(tip_map[tid_s])
        if not ordered_docs and doc:
            ordered_docs.append(doc)
        if not ordered_docs:
            return ""

        raw_blocks: List[str] = []
        doc_foreign_spans: List[str] = []
        for nd in ordered_docs:
            tut = str(nd.get("tutorial_text") or "").strip()
            ttl = str(nd.get("title") or "").strip()
            block = tut if tut else ttl
            if block:
                raw_blocks.append(block)
            for lk in nd.get("extracted_links") or []:
                if isinstance(lk, dict):
                    u = html_lib.unescape(str(lk.get("url") or lk.get("item_id") or "")).strip()
                    if u:
                        doc_foreign_spans.append(u)
                elif lk:
                    doc_foreign_spans.append(str(lk).strip())
            for tk in nd.get("extracted_tokens") or []:
                tk_s = html_lib.unescape(str(tk or "")).strip()
                if tk_s:
                    doc_foreign_spans.append(tk_s)

        out = _normalize_inline_spaces("\n".join(raw_blocks)).strip()
        if not out:
            return ""

        # 1. 仅剥离爬虫/聚合源站内部拼接的伪标签（非原作者撰写内容），必须在剥离裸 URL 之前执行以免留下空括号壳子
        out = re.sub(r"【来源采集群:\s*(?:【[^】]*】)?[^】]*】\s*", "", out)
        out = re.sub(r"【折淘客[^】]*】", "", out)
        out = re.sub(r"【[^】]*拔草哦[^】]*】\s*", "", out)
        out = re.sub(r"\[[^\]]*(?:图片|附图|晒单图|外链)\s*[:：]\s*https?://[^\]]*\]", "", out)
        out = re.sub(r"\[(?:群聊晒单/)?教程图片(?:\s*[:：][^\]]*)?\]", "", out)
        out = re.sub(r"\[口令\s*[:：][^\]]*\]", "", out)
        out = re.sub(r"(?m)^[ \t]*群发凑单文案\s*[:：]\s*", "", out)
        out = re.sub(r"[ \t]*\|[ \t]*(?:淘宝|京东|拼多多)?商品ID\s*[:：]\s*[A-Za-z0-9_\-]+", "", out)
        out = re.sub(
            r"(?m)^[ \t]*(?:->\s*(?:领券/活动链接|链接|淘宝加密商品ID)|商品加密ID|加密ID|二合一链接|二合一落地页|落地页|直达链接|附带链接|附带口令/加密ID)\s*[:：].*$",
            "",
            out,
        )
        out = re.sub(r"(?m)^[ \t]*-->[ \t]*$", "", out)

        # 2. 剥离 Markdown 链接 `[文字](https://...)` 与 HTML 解析附加的 `(https://...)` 中的网址，完整保留文字本身
        def _unwrap_md_link(m: re.Match) -> str:
            lbl = m.group(1).strip()
            if not lbl or lbl.lower().startswith(("http://", "https://", "www.")):
                return ""
            return lbl

        out = re.sub(r"\[([^\]]*)\]\(\s*https?://[^)\s]+\s*\)", _unwrap_md_link, out)
        out = re.sub(r"[ \t]*\(\s*https?://[^)\s]+\s*\)", "", out)

        # 3. 先完整清除整段淘口令/京口令壳子、小程序跳转码与裸网址（防止后续内层口令替换后残留 `8:// CZ3456` 外壳）
        out = re.sub(r"(?:#小程序|mp)://\S+", "", out)
        out = re.sub(r"https?://[^\s\u4e00-\u9fa5\"'<>）)\]]+", "", out)
        out = re.sub(
            r"(?<![A-Za-z0-9])(?:u\.jd\.com|3\.cn|m\.tb\.cn|s\.click\.taobao\.com|p\.pinduoduo\.com)/[A-Za-z0-9_.\-?=&/%]+",
            "",
            out,
        )
        out = re.sub(
            r"(?i)\d{0,2}[ \t]*[￥¥$€£₴¢!！（(《「/]{1,2}[ \t]*[A-Za-z0-9]{8,16}[ \t]*[￥¥$€£₴¢!！）)》」/]{1,2}(?:[ \t]*:?/{1,2}(?:[ \t]*(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*)?|[ \t]+(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*)?",
            "",
            out,
        )
        out = re.sub(
            r"(?i)(?:(?<=\s)|^)\d{0,2}[ \t]*(?:[\(（\$￥¥!！/\-]{1,3}[ \t]*[\)）\$￥¥!！/\-]{0,3}[ \t]*)?(?::?/{0,2}[ \t]*)?(?:CZ|MF|HU|CA|ZH|MU|AC|CB|TK)[ \t]*\d{2,6}[ \t]*/*(?=\s|$)",
            "",
            out,
        )

        # 4. 精确剔除 AI-1 识别出的 raw_span / strip_spans 与预提取锚点（支持防屏蔽短口令外壳，同时保护正常中文描述）
        def _can_safely_remove_ai_span(s: str) -> bool:
            if not s or len(s) < 5:
                return False
            zh_count = len(re.findall(r"[\u4e00-\u9fff]", s))
            if zh_count == 0:
                return True
            return len(s) <= 60 and zh_count <= 8 and bool(re.search(r"[A-Za-z0-9]{4,}", s))

        spans_to_remove: Set[str] = set()
        for t in ai1_targets or []:
            if not isinstance(t, dict):
                continue
            raw_sp = _normalize_inline_spaces(str(t.get("raw_span") or "")).strip()
            if _can_safely_remove_ai_span(raw_sp):
                spans_to_remove.add(raw_sp)
            for field_name in ("raw_span", "clean_input"):
                val = _normalize_inline_spaces(cls._strip_markdown_link_wrapper(str(t.get(field_name) or "").strip()))
                if _can_safely_remove_ai_span(val):
                    spans_to_remove.add(val)
        for sp in strip_spans or []:
            val = _normalize_inline_spaces(cls._strip_markdown_link_wrapper(str(sp or "").strip()))
            if _can_safely_remove_ai_span(val):
                spans_to_remove.add(val)
        for sp in doc_foreign_spans:
            val = _normalize_inline_spaces(cls._strip_markdown_link_wrapper(sp))
            if val and len(val) >= 6 and not re.search(r"[\u4e00-\u9fff]", val):
                spans_to_remove.add(val)

        for sp in sorted(spans_to_remove, key=len, reverse=True):
            if sp in out:
                out = out.replace(sp, "")

        out = re.sub(r"\s*\[#L\d+\]", "", out)
        out = re.sub(r"(?:(?<=\s)|^)(?:/:/|/:|:/)(?=\s|$)", "", out)

        # 5. 保留原博主的原始行文与表情，仅剔除因整行只有链接被删空后留下的纯空白行
        kept_lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        return "\n".join(kept_lines).strip()

    @classmethod
    def _should_upgrade_existing_deal(
        cls,
        ex_info: Dict[str, Any],
        ai1_res: Dict[str, Any],
        doc: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """
        判断新线报是否值得对库内已存在的同款商品执行【择优升级】：
        纯基于 AI-1 产出的标准化数值比对，不含任何脆弱正则与手工关键词猜测：
        1. 价格突破升级：新线报提取到预估到手价且比库内现有 deal_price 明显更低 (>= 5% 折扣)；
        2. 教程更优升级：新线报 tutorial_score 明显超越库内现有水平 (新分 >= 75 且 新分 >= 旧分 + 15)；
        若均不满足，则判定无需重复清洗，返回 (False, "")。
        """
        new_dp = ai1_res.get("deal_price")
        ex_dp = ex_info.get("deal_price")
        new_score = int(ai1_res.get("tutorial_score") or 0)
        ex_score = int(ex_info.get("tutorial_score") or ex_info.get("quality_score") or 75)

        # 维度一：价格突破升级（新到手价明显更优）
        if (
            isinstance(new_dp, (int, float))
            and isinstance(ex_dp, (int, float))
            and float(ex_dp) > 0
            and float(new_dp) > 0
            and float(new_dp) < float(ex_dp) * 0.95
        ):
            return True, f"新线报预估价({float(new_dp):.2f}元)显著低于库内旧价格({float(ex_dp):.2f}元)，触发价格择优升级"

        # 维度二：教程质量更优升级
        if new_score >= 75 and new_score >= ex_score + 15:
            return True, f"新线报教程实操分({new_score})显著超越库内现有水平({ex_score})，触发教程补全升级"

        return False, ""

    @classmethod
    def _make_timeline_entry(
        cls,
        doc: Dict[str, Any],
        ai1_res: Optional[Dict[str, Any]] = None,
        clean_copy: str = "",
    ) -> Dict[str, Any]:
        """构造一条结构化、有界限的线报时间线记录（用于 products.deal_info.tips_timeline 时序展示）。"""
        mid = str(doc.get("source_item_id") or "")
        site = str(doc.get("source_site") or "")
        channel = str(doc.get("source_channel") or "")
        pub_time = str(doc.get("publish_time") or "")
        pub_ts = doc.get("publish_ts")
        if not isinstance(pub_ts, (int, float)):
            pub_ts = parse_publish_time_epoch(pub_time) or 0.0
        title = str(doc.get("title") or "").strip()
        dp = (ai1_res.get("deal_price") if isinstance(ai1_res, dict) else None)
        ts = (ai1_res.get("tutorial_score") if isinstance(ai1_res, dict) else 50)
        copywriting = clean_copy or (str(ai1_res.get("combined_raw_text") or "") if isinstance(ai1_res, dict) else "") or title
        return {
            "tip_id": mid,
            "source_site": site,
            "source_channel": channel,
            "publish_time": pub_time,
            "publish_ts": float(pub_ts),
            "title": title,
            "copywriting": cls._sanitize_polished_copywriting(copywriting)[:300],
            "deal_price": dp,
            "tutorial_score": int(ts) if ts is not None else 50,
        }

    @classmethod
    def _lookup_active_formal_products(
        cls,
        formal_col,
        pid_keys: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """按指定的 (platform, product_id) 精确查询 12h 内活跃已清洗商品；若 pid_keys 为 None 则全量查询。"""
        active_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if formal_col is None:
            return active_map
        try:
            cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
                hours=DEAL_INFO_PROTECTION_HOURS
            )
            base_q: Dict[str, Any] = {
                "deal_info.is_deal_tip": True,
                "deal_info.updated_at": {"$gte": cutoff},
            }
            if pid_keys is not None:
                keys_list = [(str(p or "").strip(), str(i or "").strip()) for p, i in pid_keys if p and i]
                if not keys_list:
                    return active_map
                if len(keys_list) == 1:
                    base_q["platform"] = keys_list[0][0]
                    base_q["product_id"] = keys_list[0][1]
                else:
                    base_q["$or"] = [{"platform": p, "product_id": pid} for p, pid in keys_list]

            cursor = formal_col.find(
                base_q,
                {
                    "_id": 0,
                    "platform": 1,
                    "product_id": 1,
                    "goods_sign": 1,
                    "name": 1,
                    "price": 1,
                    "deal_info.quality_score": 1,
                    "deal_info.deal_price": 1,
                    "deal_info.tutorial_steps": 1,
                    "deal_info.tutorial_score": 1,
                },
            )
            for d in cursor:
                plat = str(d.get("platform") or "").strip()
                pid = str(d.get("product_id") or "").strip()
                if plat and pid:
                    di = d.get("deal_info") if isinstance(d.get("deal_info"), dict) else {}
                    t_steps = di.get("tutorial_steps")
                    t_cnt = len(t_steps) if isinstance(t_steps, list) else (1 if t_steps else 0)
                    active_map[(plat, pid)] = {
                        "platform": plat,
                        "product_id": pid,
                        "goods_sign": str(d.get("goods_sign") or ""),
                        "official_title": str(d.get("name") or ""),
                        "official_price": d.get("price"),
                        "deal_price": di.get("deal_price"),
                        "quality_score": int(di.get("quality_score") or 75),
                        "tutorial_steps_count": t_cnt,
                        "tutorial_score": int(di.get("tutorial_score") or di.get("quality_score") or 75),
                    }
        except Exception as e:
            logger.warning("查询正式表已清洗活跃商品集合异常: %s", e)
        return active_map

    @classmethod
    def _load_active_formal_products(cls, formal_col) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """全量查询 12h 内活跃商品（兼容老调用）。"""
        return cls._lookup_active_formal_products(formal_col, pid_keys=None)

    @staticmethod
    def _load_neighbor_docs(
        raw_col, site: str, merged_tip_ids: List[str], current_doc: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """根据 merged_tip_ids 组装对应的完整文档映射，供文案去链清洗使用。"""
        tip_map: Dict[str, Dict[str, Any]] = {}
        if current_doc:
            mid = str(current_doc.get("source_item_id") or "")
            if mid:
                tip_map[mid] = current_doc
        needed = [tid for tid in merged_tip_ids if tid and tid not in tip_map]
        if needed and raw_col is not None:
            try:
                for nd in raw_col.find(
                    {"source_site": site, "source_item_id": {"$in": needed}},
                    {"raw_payload": 0},
                ):
                    ntid = str(nd.get("source_item_id") or "").strip()
                    if ntid:
                        tip_map[ntid] = nd
            except Exception as e:
                logger.warning("补查邻居消息文档异常 (%s: %s): %s", site, needed, e)
        return tip_map

    def process_ai1_unit(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col,
        formal_col=None,
        total_pending_count: int = 0,
        shared_active_products: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
        shared_lock: Optional[threading.Lock] = None,
        quiet_sub_logs: bool = False,
    ) -> Tuple[List[Dict[str, Any]], float, Dict[str, Dict[str, Dict[str, Any]]]]:
        """
        阶段一/AI-1拆解单元（Stage 2 AI-1）：
        - Tier 1 缓存早停；
        - 复用已成功 AI-1 结果或调用 run_ai_stage1_batch；
        - 邻居碎片吸收判定 (merged_into_parent)；
        - AI-1 超时重试 (retry_pending) 或 无锚点拦截 (failed)；
        - 胜出有效记录输出 stage_outcome='advance_to_convert'。
        返回: (outcomes, elapsed_seconds, neighbor_maps_by_id)
        """
        if not selected_docs:
            return [], 0.0, {}

        t_ai1_start = time.perf_counter()
        self.warmup_anchor_cache_from_db(raw_col)
        active_formal_products = (
            shared_active_products
            if shared_active_products is not None
            else self._load_active_formal_products(formal_col)
        )

        def _get_active_prod(pid_key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
            if shared_lock is not None:
                with shared_lock:
                    v = active_formal_products.get(pid_key)
                    return copy.deepcopy(v) if v is not None else None
            v = active_formal_products.get(pid_key)
            return copy.deepcopy(v) if v is not None else None

        docs_by_tip_id: Dict[str, Dict[str, Any]] = {}
        neighbor_maps_by_id: Dict[str, Dict[str, Dict[str, Any]]] = {}
        stage1_inputs: List[Dict[str, Any]] = []
        outcomes_by_id: Dict[str, Dict[str, Any]] = {}
        pre_ai1_early_count = 0

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if not mid:
                continue
            docs_by_tip_id[mid] = doc
            site = str(doc.get("source_site") or "")
            site_name = get_source_site_name(site)
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()

            # Tier 1 前置提前终止（Before AI-1）
            raw_anchors: List[str] = []
            for lk in doc.get("extracted_links") or []:
                if isinstance(lk, dict):
                    u = self._strip_markdown_link_wrapper(str(lk.get("url") or lk.get("item_id") or ""))
                    if u and u not in raw_anchors:
                        raw_anchors.append(u)
            for tk in doc.get("extracted_tokens") or []:
                tk_s = self._strip_markdown_link_wrapper(str(tk or ""))
                if tk_s and tk_s not in raw_anchors:
                    raw_anchors.append(tk_s)

            matched_cached_conv: Optional[Dict[str, Any]] = None
            matched_cached_list: List[Dict[str, Any]] = []
            matched_ex_info: Optional[Dict[str, Any]] = None
            if raw_anchors:
                def_plat = str(doc.get("platform") or "unknown").strip().lower()
                all_anchors_cached = True
                with self._cache_lock:
                    for anchor_item in raw_anchors:
                        guess_plat = detect_candidate_platform(anchor_item, def_plat)
                        plats_to_check = [guess_plat] if guess_plat in ("jd", "taobao", "pdd") else ["taobao", "jd", "pdd"]
                        found_entry = None
                        for cp in plats_to_check:
                            for ck in self._build_anchor_cache_keys(cp, anchor_item):
                                if ck in self.anchor_cache:
                                    found_entry = self.anchor_cache[ck]
                                    break
                            if found_entry is not None:
                                break
                        if found_entry is None:
                            all_anchors_cached = False
                            break
                        matched_cached_list.append(found_entry)

                    if all_anchors_cached and matched_cached_list:
                        prod_entries = [e for e in matched_cached_list if e.get("convert_status") == "product"]
                        unique_pids = {
                            (str(e.get("platform") or ""), str(e.get("goods_id") or ""))
                            for e in prod_entries
                            if e.get("platform") and e.get("goods_id")
                        }
                        if len(unique_pids) == 1:
                            only_pid = next(iter(unique_pids))
                            ex_cand = _get_active_prod(only_pid)
                            if ex_cand is not None:
                                matched_cached_conv = copy.deepcopy(prod_entries[0])
                                matched_ex_info = ex_cand
                                self.stats_cache_hits += len(matched_cached_list)

            if matched_cached_conv is not None and matched_ex_info is not None:
                c_plat = str(matched_cached_conv.get("platform") or "")
                c_gid = str(matched_cached_conv.get("goods_id") or "")
                c_gsign = str(matched_cached_conv.get("goods_sign") or "")
                ex_info = matched_ex_info
                self.stats_early_terminated_pre_ai1 += 1
                pre_ai1_early_count += 1
                outcomes_by_id[mid] = {
                    "status": "early_terminated",
                    "stage_outcome": "early_terminated",
                    "early_stage": "pre_ai1",
                    "error_code": OK_SKIPPED_ALREADY_CLEANED,
                    "error_msg": f"前置锚点缓存命中已入库唯一商品({c_plat}:{c_gid})，提前终止跳过AI-1/转链/AI-2并累加共振",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": c_plat,
                    "product_id": c_gid,
                    "goods_sign": c_gsign,
                    "official_title": ex_info.get("official_title") or raw_title,
                    "official_price": ex_info.get("official_price"),
                    "deal_price": ex_info.get("deal_price"),
                    "title": raw_title,
                    "quality_score": ex_info.get("quality_score", 75),
                    "merged_tip_ids": [mid],
                    "ai_stage1_result": {"early_terminated": "pre_ai1_cache_hit"},
                    "converted_links": [{k: v for k, v in e.items() if k != "adapter_res"} for e in matched_cached_list],
                    "ai_stage2_result": {"early_terminated": "pre_ai1_cache_hit"},
                    "candidate_count": len(raw_anchors),
                    "attempt_errors": [],
                    "resonance_update": {
                        "platform": c_plat,
                        "product_id": c_gid,
                        "seen_count": max(1, int(doc.get("seen_count") or 1)),
                        "source_site": site,
                        "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                        "copywriting": self._sanitize_polished_copywriting(str(doc.get("tutorial_text") or raw_title)),
                        "timeline_entry": self._make_timeline_entry(doc, clean_copy=self._sanitize_polished_copywriting(str(doc.get("tutorial_text") or raw_title))),
                    },
                }
                continue

            saved_ai1 = doc.get("ai_stage1_result")
            if (
                doc.get("clean_error_code") in (ERR_ADAPTER_CONVERT_FAILED, ERR_AI_TIMEOUT_RETRY)
                and isinstance(saved_ai1, dict)
                and not saved_ai1.get("_from_fallback")
                and saved_ai1.get("is_standalone_deal") is not False
                and saved_ai1.get("extracted_targets")
            ):
                reused_map: Dict[str, Dict[str, Any]] = {mid: doc}
                extra_mids = [
                    str(tid or "").strip()
                    for tid in (saved_ai1.get("merged_tip_ids") or [])
                    if str(tid or "").strip() and str(tid or "").strip() != mid
                ]
                if extra_mids and raw_col is not None:
                    try:
                        for nd in raw_col.find(
                            {"source_site": site, "source_item_id": {"$in": extra_mids}},
                            {"raw_payload": 0},
                        ):
                            ntid = str(nd.get("source_item_id") or "").strip()
                            if ntid:
                                reused_map[ntid] = nd
                    except Exception as e:
                        logger.warning("重试复用 AI-1 结果时补查邻居消息异常 (%s:%s): %s", site, mid, e)
                neighbor_maps_by_id[mid] = reused_map
                continue

            s1_inp, tip_map = self.build_stage1_task_input(doc, raw_col)
            neighbor_maps_by_id[mid] = tip_map
            stage1_inputs.append(s1_inp)

        if pre_ai1_early_count > 0 and not quiet_sub_logs:
            logger.info(
                "⚡ [Stage 2 前置缓存早停] 命中已清洗入库商品缓存提前终止=%d条 (直接跳过 AI-1 / 转链 / AI-2)",
                pre_ai1_early_count,
            )

        ai1_hits_before = self.stats_ai1_model_hits
        ai1_fb_before = self.stats_ai1_fallbacks
        if not quiet_sub_logs:
            n_chunks = (len(stage1_inputs) + self.ai_batch_size - 1) // self.ai_batch_size if stage1_inputs else 0
            logger.info(
                "🤖 [AI-1 拆解启动] 本批认领=%d条 (每组=%d条, 共%d组, 并发=%d线程) | 阶段: need_ai1 -> need_convert",
                len(stage1_inputs),
                self.ai_batch_size,
                n_chunks,
                min(self.ai_max_workers, n_chunks) if n_chunks else 1,
            )
        ai1_results = (
            self.run_ai_stage1_batch(
                stage1_inputs,
                docs_by_tip_id,
                neighbor_maps_by_id=neighbor_maps_by_id,
                total_pending_count=total_pending_count,
                quiet_sub_logs=quiet_sub_logs,
            )
            if stage1_inputs
            else {}
        )
        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if mid and mid not in ai1_results and mid not in outcomes_by_id:
                saved_ai1 = doc.get("ai_stage1_result")
                if isinstance(saved_ai1, dict) and not saved_ai1.get("_from_fallback") and saved_ai1.get("extracted_targets"):
                    ai1_results[mid] = copy.deepcopy(saved_ai1)
        t_ai1_elapsed = round(time.perf_counter() - t_ai1_start, 2)

        # 邻居碎片吸收判定
        absorbed_by_neighbor: Dict[str, str] = {}
        active_parent_ids: Set[str] = set()
        sorted_ai1_items = sorted(
            ai1_results.items(),
            key=lambda kv: (
                len((kv[1] or {}).get("extracted_targets") or []),
                len((kv[1] or {}).get("merged_tip_ids") or []),
            ),
            reverse=True,
        )
        for mid, ai1_res in sorted_ai1_items:
            if ai1_res.get("_from_fallback") or not ai1_res.get("is_standalone_deal", True):
                continue
            if mid in absorbed_by_neighbor:
                continue
            for m_tid in ai1_res.get("merged_tip_ids") or []:
                m_tid_str = str(m_tid or "").strip()
                if (
                    m_tid_str
                    and m_tid_str != mid
                    and m_tid_str not in absorbed_by_neighbor
                    and m_tid_str not in active_parent_ids
                ):
                    absorbed_by_neighbor[m_tid_str] = mid
                    active_parent_ids.add(mid)

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if not mid or mid in outcomes_by_id:
                continue
            site = str(doc.get("source_site") or "")
            site_name = get_source_site_name(site)
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()
            ai1_res = ai1_results.get(mid) or {}

            if mid in absorbed_by_neighbor or ai1_res.get("is_standalone_deal") is False:
                parent_id = absorbed_by_neighbor.get(mid) or ""
                outcomes_by_id[mid] = {
                    "status": "merged_into_parent",
                    "stage_outcome": "merged_into_parent",
                    "error_code": ERR_MERGED_INTO_NEIGHBOR,
                    "error_msg": f"群聊连续碎片已合并入相邻主线报 {parent_id}".strip(),
                    "merged_into_tip_id": parent_id,
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [],
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": 0,
                    "attempt_errors": [],
                }
                continue

            if ai1_res.get("_from_fallback"):
                new_retries = int(doc.get("clean_retry_count") or 0) + 1
                self.stats_ai_retries_scheduled += 1
                if new_retries < MAX_CLEAN_RETRIES:
                    outcomes_by_id[mid] = {
                        "status": "retry_pending",
                        "stage_outcome": "retry_pending",
                        "clean_stage": CLEAN_STAGE_NEED_AI1,
                        "error_code": ERR_AI_TIMEOUT_RETRY,
                        "error_msg": f"AI-1 超时或未返回有效拆解，跳过本轮并累加重试 ({new_retries}/{MAX_CLEAN_RETRIES})",
                        "retry_count": new_retries,
                        "source_site": site,
                        "source_channel": channel,
                        "source_item_id": mid,
                        "title": raw_title,
                        "ai_stage1_result": ai1_res,
                        "converted_links": [],
                        "ai_stage2_result": None,
                        "quality_score": 0,
                        "candidate_count": 0,
                        "attempt_errors": [],
                    }
                else:
                    outcomes_by_id[mid] = {
                        "status": "failed",
                        "stage_outcome": "failed",
                        "error_code": ERR_AI_MAX_RETRIES_EXCEEDED,
                        "error_msg": f"AI-1 连续 {new_retries} 次超时或未返回结果，达最大重试上限({MAX_CLEAN_RETRIES}次)，永久放弃",
                        "retry_count": new_retries,
                        "source_site": site,
                        "source_channel": channel,
                        "source_item_id": mid,
                        "title": raw_title,
                        "ai_stage1_result": ai1_res,
                        "converted_links": [],
                        "ai_stage2_result": None,
                        "quality_score": 0,
                        "candidate_count": 0,
                        "attempt_errors": [],
                    }
                continue

            targets = ai1_res.get("extracted_targets") or []
            if not targets:
                outcomes_by_id[mid] = {
                    "status": "failed",
                    "stage_outcome": "failed",
                    "error_code": ERR_NO_ANCHOR,
                    "error_msg": "Stage 1 AI 未在爆料文本中识别出任何淘/京/拼链接或口令",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [],
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": 0,
                    "attempt_errors": [],
                }
                continue

            # 成功完成 AI-1 拆解，推进至转链阶段
            outcomes_by_id[mid] = {
                "status": "in_progress",
                "stage_outcome": "advance_to_convert",
                "source_site": site,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "candidate_count": len(targets),
            }

        out_list = [outcomes_by_id[str(d.get("source_item_id") or "")] for d in selected_docs if str(d.get("source_item_id") or "") in outcomes_by_id]
        if not quiet_sub_logs:
            adv_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "advance_to_convert")
            retry_cnt = sum(1 for o in out_list if o.get("status") == "retry_pending")
            fail_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "failed")
            logger.info(
                "🤖 [AI-1 拆解完成] 耗时=%.1fs | 本批认领=%d条 (直出=%d条, 留待重试=%d条) -> 推进待转链=%d条 (淘汰纯券/无链=%d条)",
                t_ai1_elapsed,
                len(selected_docs),
                self.stats_ai1_model_hits - ai1_hits_before,
                retry_cnt,
                adv_cnt,
                fail_cnt,
            )
        return out_list, t_ai1_elapsed, neighbor_maps_by_id

    def process_convert_unit(
        self,
        selected_docs: List[Dict[str, Any]],
        formal_col=None,
        total_pending_count: int = 0,
        shared_active_products: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
        shared_lock: Optional[threading.Lock] = None,
        quiet_sub_logs: bool = False,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """
        阶段二/转链核验单元（Stage 3 官方 Adapter 并发转链）：
        - 收集 extracted_targets 并发执行官方转链；
        - 单品与凑单品存活校验；
        - Tier 2 转链后已入库商品缓存早停 (post_stage3_db)；
        - 同批次相同主商品去重早停 (post_stage3_batch)；
        - 暂态错误重试 (retry_pending) 或 转链失败 (failed)；
        - 胜出有效记录输出 stage_outcome='advance_to_ai2'。
        返回: (outcomes, elapsed_seconds)
        """
        if not selected_docs:
            return [], 0.0

        t_stage3_start = time.perf_counter()
        active_formal_products = (
            shared_active_products
            if shared_active_products is not None
            else self._load_active_formal_products(formal_col)
        )

        def _get_active_prod(pid_key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
            if shared_lock is not None:
                with shared_lock:
                    v = active_formal_products.get(pid_key)
                    return copy.deepcopy(v) if v is not None else None
            v = active_formal_products.get(pid_key)
            return copy.deepcopy(v) if v is not None else None

        # 收集去重目标
        total_target_mentions = 0
        unique_target_map: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]] = {}
        for doc in selected_docs:
            ai1_res = doc.get("ai_stage1_result") or {}
            if ai1_res.get("_from_fallback") or ai1_res.get("is_standalone_deal") is False:
                continue
            def_plat = str(doc.get("platform") or "unknown")
            for tgt in (ai1_res.get("extracted_targets") or [])[:8]:
                if not isinstance(tgt, dict):
                    continue
                if str(tgt.get("text_role_hint") or "").strip() == "filler_candidate":
                    continue
                c_inp = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
                if not c_inp:
                    continue
                total_target_mentions += 1
                hint_p = str(tgt.get("platform") or def_plat or "unknown").strip().lower()
                det_p = detect_candidate_platform(c_inp, hint_p if hint_p in ("jd", "taobao", "pdd") else def_plat)
                ukey = (det_p, c_inp)
                if ukey not in unique_target_map:
                    unique_target_map[ukey] = (tgt, def_plat)

        if not quiet_sub_logs:
            logger.info(
                "🔗 [官方转链启动] 本批全量认领=%d条 (并发核验=%d个独立链接/口令, 引用=%d次, 并发=%d线程) | 阶段: need_convert -> need_ai2",
                len(selected_docs),
                len(unique_target_map),
                total_target_mentions,
                self.convert_max_workers,
            )
        self.run_stage3_concurrent_conversions(
            list(unique_target_map.values()),
            total_pending_count=total_pending_count,
            quiet_sub_logs=quiet_sub_logs,
        )
        t_stage3_elapsed = round(time.perf_counter() - t_stage3_start, 2)

        outcomes_by_id: Dict[str, Dict[str, Any]] = {}
        post_stage3_candidates: List[Tuple[str, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], Optional[Tuple[str, str]], float]] = []
        post_stage3_db_early_count = 0

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            site = str(doc.get("source_site") or "")
            site_name = get_source_site_name(site)
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()
            ai1_res = doc.get("ai_stage1_result") or {}
            targets = ai1_res.get("extracted_targets") or []

            conv_list: List[Dict[str, Any]] = []
            attempt_errors: List[Dict[str, str]] = []
            def_doc_plat = str(doc.get("platform") or "unknown")
            for tgt in targets[:8]:
                if not isinstance(tgt, dict):
                    continue
                if str(tgt.get("text_role_hint") or "").strip() == "filler_candidate":
                    c_inp_f = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
                    hint_pf = str(tgt.get("platform") or def_doc_plat).strip().lower()
                    conv_list.append({
                        "link_id": str(tgt.get("link_id") or ""),
                        "raw_span": str(tgt.get("raw_span") or ""),
                        "clean_input": c_inp_f,
                        "text_role_hint": "filler_candidate",
                        "platform": detect_candidate_platform(c_inp_f, hint_pf if hint_pf in ("jd", "taobao", "pdd") else def_doc_plat),
                        "convert_status": "skipped_filler",
                        "error_code": "SKIPPED_FILLER_NO_CONVERT",
                        "error_msg": "凑单辅助品按极简规范跳过转链",
                        "goods_id": "",
                        "goods_sign": "",
                        "my_promo_link": "",
                        "my_promo_token": "",
                        "official_product": None,
                        "adapter_res": None,
                    })
                    continue
                c_res = self.convert_single_target(tgt, default_plat=def_doc_plat)
                conv_list.append(c_res)
                if c_res["convert_status"] == "failed":
                    attempt_errors.append({
                        "link_id": c_res["link_id"],
                        "anchor": str(c_res.get("clean_input") or "")[:100],
                        "error_code": c_res["error_code"],
                        "error_msg": c_res["error_msg"],
                    })

            prod_convs = [c for c in conv_list if c["convert_status"] == "product"]
            coupon_convs = [c for c in conv_list if c["convert_status"] == "coupon_or_activity"]
            has_skipped_filler = any(c.get("convert_status") == "skipped_filler" for c in conv_list)
            conv_summary_s3 = [
                {k: v for k, v in c.items() if k != "adapter_res"}
                for c in conv_list
            ]

            if not prod_convs:
                transient_err = next(
                    (e for e in attempt_errors if e["error_code"] == ERR_ADAPTER_CONVERT_FAILED),
                    None,
                )
                if transient_err is not None:
                    new_retries = int(doc.get("clean_retry_count") or 0) + 1
                    self.stats_ai_retries_scheduled += 1
                    if new_retries < MAX_CLEAN_RETRIES:
                        outcomes_by_id[mid] = {
                            "status": "retry_pending",
                            "stage_outcome": "retry_pending",
                            "clean_stage": CLEAN_STAGE_NEED_CONVERT,
                            "error_code": ERR_ADAPTER_CONVERT_FAILED,
                            "error_msg": f"官方转链或反查暂态失败，保留待下轮重试 ({new_retries}/{MAX_CLEAN_RETRIES}): {transient_err['error_msg']}",
                            "retry_count": new_retries,
                            "source_site": site,
                            "source_channel": channel,
                            "source_item_id": mid,
                            "title": raw_title,
                            "ai_stage1_result": ai1_res,
                            "converted_links": conv_summary_s3,
                            "ai_stage2_result": None,
                            "quality_score": 0,
                            "candidate_count": len(targets),
                            "attempt_errors": attempt_errors,
                        }
                    else:
                        outcomes_by_id[mid] = {
                            "status": "failed",
                            "stage_outcome": "failed",
                            "error_code": ERR_ADAPTER_CONVERT_FAILED,
                            "error_msg": f"官方转链连续 {new_retries} 轮失败，达最大重试上限({MAX_CLEAN_RETRIES}次)放弃: {transient_err['error_msg']}",
                            "retry_count": new_retries,
                            "source_site": site,
                            "source_channel": channel,
                            "source_item_id": mid,
                            "title": raw_title,
                            "ai_stage1_result": ai1_res,
                            "converted_links": conv_summary_s3,
                            "ai_stage2_result": None,
                            "quality_score": 0,
                            "candidate_count": len(targets),
                            "attempt_errors": attempt_errors,
                        }
                    continue

                if has_skipped_filler:
                    primary_err_code = ERR_AI2_MAIN_DEAD_ONLY_FILLER
                    primary_err_msg = "主商品未存活或仅含凑单辅助品，提前终止拦截"
                elif coupon_convs and not attempt_errors:
                    primary_err_code = ERR_EMPTY_PRODUCT_IDENTITY
                    primary_err_msg = "仅包含优惠券或活动会场链接，无具体主商品"
                else:
                    primary_err_code = attempt_errors[0]["error_code"] if attempt_errors else ERR_ADAPTER_CONVERT_FAILED
                    primary_err_msg = attempt_errors[0]["error_msg"] if attempt_errors else "所有候选链接均转链失败"
                outcomes_by_id[mid] = {
                    "status": "failed",
                    "stage_outcome": "failed",
                    "error_code": primary_err_code,
                    "error_msg": primary_err_msg,
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_s3,
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": len(targets),
                    "attempt_errors": attempt_errors,
                }
                continue

            main_prod_convs = [c for c in prod_convs if c.get("text_role_hint") != "filler_candidate"]
            if not main_prod_convs:
                transient_main_err = next(
                    (
                        c
                        for c in conv_list
                        if c["convert_status"] == "failed"
                        and c["error_code"] == ERR_ADAPTER_CONVERT_FAILED
                        and c.get("text_role_hint") != "filler_candidate"
                    ),
                    None,
                )
                if transient_main_err is not None:
                    new_retries = int(doc.get("clean_retry_count") or 0) + 1
                    self.stats_ai_retries_scheduled += 1
                    if new_retries < MAX_CLEAN_RETRIES:
                        outcomes_by_id[mid] = {
                            "status": "retry_pending",
                            "stage_outcome": "retry_pending",
                            "clean_stage": CLEAN_STAGE_NEED_CONVERT,
                            "error_code": ERR_ADAPTER_CONVERT_FAILED,
                            "error_msg": f"主商品官方转链暂态失败，保留待下轮重试 ({new_retries}/{MAX_CLEAN_RETRIES}): {transient_main_err['error_msg']}",
                            "retry_count": new_retries,
                            "source_site": site,
                            "source_channel": channel,
                            "source_item_id": mid,
                            "title": raw_title,
                            "ai_stage1_result": ai1_res,
                            "converted_links": conv_summary_s3,
                            "ai_stage2_result": None,
                            "quality_score": 0,
                            "candidate_count": len(targets),
                            "attempt_errors": attempt_errors,
                        }
                        continue

                outcomes_by_id[mid] = {
                    "status": "failed",
                    "stage_outcome": "failed",
                    "error_code": ERR_AI2_MAIN_DEAD_ONLY_FILLER,
                    "error_msg": "主商品链接转链失败，仅凑单品链接存活，提前终止拦截",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_s3,
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": len(targets),
                    "attempt_errors": attempt_errors,
                }
                continue

            transient_degraded = next(
                (
                    c
                    for c in conv_list
                    if c.get("convert_status") == "failed"
                    and c.get("resolution_kind") == "degraded_product"
                    and c.get("text_role_hint") != "filler_candidate"
                ),
                None,
            )
            if transient_degraded is not None:
                new_retries = int(doc.get("clean_retry_count") or 0) + 1
                if new_retries < MAX_CLEAN_RETRIES:
                    self.stats_ai_retries_scheduled += 1
                    conv_summary_retry = [
                        {k: v for k, v in c.items() if k != "adapter_res"}
                        for c in conv_list
                    ]
                    outcomes_by_id[mid] = {
                        "status": "retry_pending",
                        "stage_outcome": "retry_pending",
                        "clean_stage": CLEAN_STAGE_NEED_CONVERT,
                        "error_code": ERR_ADAPTER_CONVERT_FAILED,
                        "error_msg": (
                            f"存在非凑单链接触发详情反查暂态限流，保留待下轮重试 "
                            f"({new_retries}/{MAX_CLEAN_RETRIES}): {transient_degraded.get('error_msg') or ''}"
                        ),
                        "retry_count": new_retries,
                        "source_site": site,
                        "source_channel": channel,
                        "source_item_id": mid,
                        "title": raw_title,
                        "ai_stage1_result": ai1_res,
                        "converted_links": conv_summary_retry,
                        "ai_stage2_result": None,
                        "quality_score": 0,
                        "candidate_count": len(targets),
                        "attempt_errors": attempt_errors,
                    }
                    continue

            unique_main_pids = {
                (str(c.get("platform") or ""), str(c.get("goods_id") or ""))
                for c in main_prod_convs
                if c.get("platform") and c.get("goods_id")
            }
            single_main_pid: Optional[Tuple[str, str]] = (
                next(iter(unique_main_pids)) if len(unique_main_pids) == 1 else None
            )

            # Tier 2 提前终止或择优升级（已入库商品）
            ex_info_s3 = _get_active_prod(single_main_pid) if single_main_pid is not None else None
            should_upgrade = False
            upgrade_reason = ""
            if single_main_pid is not None and ex_info_s3 is not None:
                should_upgrade, upgrade_reason = self._should_upgrade_existing_deal(ex_info_s3, ai1_res, doc)
                if should_upgrade:
                    self.stats_deal_upgrades_post_stage3 += 1
                    if not quiet_sub_logs:
                        logger.info(
                            "✨ [Stage 3 择优升级] 命中已入库商品(%s:%s)但满足升级条件: %s -> 推进至 AI-2 终审更新！",
                            single_main_pid[0],
                            single_main_pid[1],
                            upgrade_reason,
                        )

            if single_main_pid is not None and ex_info_s3 is not None and not should_upgrade:
                c_plat, c_gid = single_main_pid
                primary_conv = main_prod_convs[0]
                c_gsign = str(primary_conv.get("goods_sign") or "")
                off_p = primary_conv.get("official_product") or {}
                ex_info = ex_info_s3
                merged_tids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
                if mid not in merged_tids:
                    merged_tids.insert(0, mid)
                self.stats_early_terminated_post_stage3 += 1
                post_stage3_db_early_count += 1
                clean_copy = self._sanitize_polished_copywriting(str(ai1_res.get("combined_raw_text") or raw_title))
                timeline_item = self._make_timeline_entry(doc, ai1_res=ai1_res, clean_copy=clean_copy)
                outcomes_by_id[mid] = {
                    "status": "early_terminated",
                    "stage_outcome": "early_terminated",
                    "early_stage": "post_stage3_db",
                    "error_code": OK_SKIPPED_ALREADY_CLEANED,
                    "error_msg": f"转链唯一商品({c_plat}:{c_gid})已在正式库清洗完毕(12h内)，提前终止跳过AI-2并累加共振",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": c_plat,
                    "product_id": c_gid,
                    "goods_sign": c_gsign,
                    "official_title": off_p.get("title") or ex_info.get("official_title") or raw_title,
                    "official_price": off_p.get("price") or ex_info.get("official_price"),
                    "deal_price": ex_info.get("deal_price"),
                    "title": raw_title,
                    "quality_score": ex_info.get("quality_score", 75),
                    "merged_tip_ids": merged_tids,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_s3,
                    "ai_stage2_result": {"early_terminated": "post_stage3_existing_product"},
                    "candidate_count": len(targets),
                    "attempt_errors": attempt_errors,
                    "resonance_update": {
                        "platform": c_plat,
                        "product_id": c_gid,
                        "seen_count": max(1, int(doc.get("seen_count") or 1)),
                        "source_site": site,
                        "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                        "copywriting": clean_copy,
                        "timeline_entry": timeline_item,
                    },
                }
                continue

            new_score = int(ai1_res.get("tutorial_score") or 50)
            richness_score = (
                len(conv_list) * 10.0
                + len(ai1_res.get("extracted_images") or []) * 5.0
                + new_score * 0.3
                + (25.0 if should_upgrade else 0.0)
            )
            post_stage3_candidates.append((mid, doc, ai1_res, conv_list, single_main_pid, richness_score))

        self._evict_transient_failed_cache()

        # 第二遍扫描 Stage 3 结果：同批次内相同唯一商品去重
        best_mid_by_pid: Dict[Tuple[str, str], Tuple[float, str]] = {}
        for mid, _doc, _ai1_res, _conv_list, single_pid, r_score in post_stage3_candidates:
            if single_pid is not None:
                prev = best_mid_by_pid.get(single_pid)
                if prev is None or r_score > prev[0]:
                    best_mid_by_pid[single_pid] = (r_score, mid)

        post_stage3_batch_early_count = 0
        for mid, doc, ai1_res, conv_list, single_pid, _r_score in post_stage3_candidates:
            site = str(doc.get("source_site") or "")
            site_name = get_source_site_name(site)
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()

            if single_pid is not None and best_mid_by_pid.get(single_pid, (0.0, mid))[1] != mid:
                winner_mid = best_mid_by_pid[single_pid][1]
                c_plat, c_gid = single_pid
                main_prod_convs = [c for c in conv_list if c["convert_status"] == "product" and c.get("text_role_hint") != "filler_candidate"]
                primary_conv = main_prod_convs[0]
                c_gsign = str(primary_conv.get("goods_sign") or "")
                off_p = primary_conv.get("official_product") or {}
                merged_tids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
                if mid not in merged_tids:
                    merged_tids.insert(0, mid)
                self.stats_early_terminated_post_stage3 += 1
                post_stage3_batch_early_count += 1
                clean_copy_batch = self._sanitize_polished_copywriting(str(ai1_res.get("combined_raw_text") or raw_title))
                timeline_item_batch = self._make_timeline_entry(doc, ai1_res=ai1_res, clean_copy=clean_copy_batch)
                outcomes_by_id[mid] = {
                    "status": "early_terminated",
                    "stage_outcome": "early_terminated",
                    "early_stage": "post_stage3_batch",
                    "error_code": OK_SKIPPED_ALREADY_CLEANED,
                    "error_msg": f"同批次已有更优线报({winner_mid})命中同一唯一商品({c_plat}:{c_gid})，提前终止跳过AI-2并累加共振",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": c_plat,
                    "product_id": c_gid,
                    "goods_sign": c_gsign,
                    "official_title": off_p.get("title") or raw_title,
                    "official_price": off_p.get("price"),
                    "deal_price": None,
                    "title": raw_title,
                    "quality_score": 75,
                    "merged_tip_ids": merged_tids,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [
                        {k: v for k, v in c.items() if k != "adapter_res"}
                        for c in conv_list
                    ],
                    "ai_stage2_result": {"early_terminated": "post_stage3_same_batch_duplicate", "winner_tip_id": winner_mid},
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                    "resonance_update": {
                        "platform": c_plat,
                        "product_id": c_gid,
                        "seen_count": max(1, int(doc.get("seen_count") or 1)),
                        "source_site": site,
                        "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                        "copywriting": clean_copy_batch,
                        "timeline_entry": timeline_item_batch,
                    },
                }
                continue

            conv_summary_s3 = [
                {k: v for k, v in c.items() if k != "adapter_res"}
                for c in conv_list
            ]
            outcomes_by_id[mid] = {
                "status": "in_progress",
                "stage_outcome": "advance_to_ai2",
                "source_site": site,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_s3,
                "full_conv_list": conv_list,
                "candidate_count": len(conv_list),
            }

        if (post_stage3_db_early_count > 0 or post_stage3_batch_early_count > 0) and not quiet_sub_logs:
            logger.info(
                "⚡ [Stage 3 唯一商品标识早停] 转链后命中唯一商品 `(platform, goods_id)` 提前终止=%d条 "
                "(已在正式库=%d条, 同批去重=%d条) -> 直接跳过 Stage 4 (AI-2)！",
                post_stage3_db_early_count + post_stage3_batch_early_count,
                post_stage3_db_early_count,
                post_stage3_batch_early_count,
            )

        out_list = [outcomes_by_id[str(d.get("source_item_id") or "")] for d in selected_docs if str(d.get("source_item_id") or "") in outcomes_by_id]
        if not quiet_sub_logs:
            adv_ai2_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "advance_to_ai2")
            early_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "early_terminated")
            retry_cnt = sum(1 for o in out_list if o.get("status") == "retry_pending")
            fail_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "failed")
            spd_doc = len(selected_docs) / max(0.1, t_stage3_elapsed) * 60
            logger.info(
                "🔗 [官方转链完成] 耗时=%.1fs | 认领=%d条 (推进待终审=%d条, 早停吸收=%d条, 留待重试=%d条, 失败淘汰=%d条) | 速度=%.1f条/分 | 并发=%d线程 | 累计缓存命中=%d次",
                t_stage3_elapsed,
                len(selected_docs),
                adv_ai2_cnt,
                early_cnt,
                retry_cnt,
                fail_cnt,
                spd_doc,
                self.convert_max_workers,
                self.stats_cache_hits,
            )
        return out_list, t_stage3_elapsed

    def process_ai2_unit(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col=None,
        formal_col=None,
        total_pending_count: int = 0,
        neighbor_maps_by_id: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        quiet_sub_logs: bool = False,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """
        阶段三/AI-2终审单元（Stage 4 AI-2）：
        - 组装文案与精简底牌数据；
        - 并发调用 run_ai_stage2_batch 进行终审与质量评分；
        - 组装最终 product_doc、deal_info 及 coupon_links；
        - 评分 >= 60 输出 status='success'，低分输出 status='low_score'。
        返回: (outcomes, elapsed_seconds)
        """
        if not selected_docs:
            return [], 0.0

        t_ai2_start = time.perf_counter()
        draft_copy_by_id: Dict[str, str] = {}
        stage2_inputs: List[Dict[str, Any]] = []

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            site = str(doc.get("source_site") or "")
            ai1_res = doc.get("ai_stage1_result") or {}
            conv_list = doc.get("full_conv_list") or doc.get("converted_links") or doc.get("stage3_converted_links") or []

            merged_tids_for_copy = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
            if mid not in merged_tids_for_copy:
                merged_tids_for_copy.insert(0, mid)
            tip_map_for_copy = (neighbor_maps_by_id.get(mid) if neighbor_maps_by_id else None) or self._load_neighbor_docs(
                raw_col, site, merged_tids_for_copy, current_doc=doc
            )
            draft_copywriting = self._build_clean_original_copywriting(
                merged_tids_for_copy,
                tip_map_for_copy,
                ai1_res.get("extracted_targets") or [],
                doc=doc,
                strip_spans=ai1_res.get("strip_spans") or [],
            )
            draft_copy_by_id[mid] = draft_copywriting

            has_valid_prod = any(
                c.get("convert_status") == "product"
                and c.get("text_role_hint") != "filler_candidate"
                for c in conv_list
            )
            ai2_conv_links = [
                {
                    "link_id": c["link_id"],
                    "text_role_hint": c.get("text_role_hint") or "main_candidate",
                    "convert_status": c["convert_status"],
                    "platform": c["platform"],
                    "official_product": c.get("official_product"),
                }
                for c in conv_list
                if not (
                    has_valid_prod
                    and c.get("convert_status") == "failed"
                    and c.get("error_code") in (ERR_JD_NUMERIC_SKU, ERR_TB_NUMERIC_ID, ERR_PDD_NUMERIC_ID)
                )
            ]
            stage2_inputs.append({
                "main_tip_id": mid,
                "source_site": site,
                "is_group_stream": bool(doc.get("is_group_stream")),
                "combined_raw_text": str(ai1_res.get("combined_raw_text") or ""),
                "draft_copywriting": draft_copywriting,
                "share_image_count": len(ai1_res.get("extracted_images") or []),
                "converted_links": ai2_conv_links,
            })

        ai2_hits_before = self.stats_ai2_model_hits
        ai2_fb_before = self.stats_ai2_fallbacks
        if not quiet_sub_logs:
            bs2 = max(1, getattr(self, "ai2_batch_size", 6))
            n_chunks = (len(stage2_inputs) + bs2 - 1) // bs2 if stage2_inputs else 0
            logger.info(
                "🧠 [AI-2 终审启动] 本批认领=%d款新唯一商品 (每组=%d条, 共%d组, 并发=%d线程) | 阶段: need_ai2 -> 正式库",
                len(stage2_inputs),
                bs2,
                n_chunks,
                min(self.ai_max_workers, n_chunks) if n_chunks else 1,
            )
        ai2_results = self.run_ai_stage2_batch(
            stage2_inputs,
            total_pending_count=total_pending_count,
            quiet_sub_logs=quiet_sub_logs,
        )
        t_ai2_elapsed = round(time.perf_counter() - t_ai2_start, 2)
        s2_input_map = {item["main_tip_id"]: item for item in stage2_inputs}

        outcomes_by_id: Dict[str, Dict[str, Any]] = {}
        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            site = str(doc.get("source_site") or "")
            site_name = get_source_site_name(site)
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()
            ai1_res = doc.get("ai_stage1_result") or {}
            conv_list = doc.get("full_conv_list") or doc.get("converted_links") or doc.get("stage3_converted_links") or []
            s2_inp = s2_input_map.get(mid) or {}
            ai2_res = ai2_results.get(mid)
            if not isinstance(ai2_res, dict) or "is_valid_deal" not in ai2_res:
                self.stats_ai2_fallbacks += 1
                ai2_res = self._fallback_stage2_evaluate(s2_inp)
            else:
                self.stats_ai2_model_hits += 1
                ai2_res["_from_fallback"] = False

            conv_summary_for_db = [
                {k: v for k, v in c.items() if k != "adapter_res"}
                for c in conv_list
            ]
            if ai2_res.get("_from_fallback"):
                new_retries = int(doc.get("clean_retry_count") or 0) + 1
                self.stats_ai_retries_scheduled += 1
                if new_retries < MAX_CLEAN_RETRIES:
                    outcomes_by_id[mid] = {
                        "status": "retry_pending",
                        "stage_outcome": "retry_pending",
                        "clean_stage": CLEAN_STAGE_NEED_AI2,
                        "error_code": ERR_AI_TIMEOUT_RETRY,
                        "error_msg": f"AI-2 超时或未返回有效终审，跳过本轮并累加重试 ({new_retries}/{MAX_CLEAN_RETRIES})",
                        "retry_count": new_retries,
                        "source_site": site,
                        "source_channel": channel,
                        "source_item_id": mid,
                        "title": raw_title,
                        "ai_stage1_result": ai1_res,
                        "converted_links": conv_summary_for_db,
                        "ai_stage2_result": ai2_res,
                        "quality_score": 0,
                        "candidate_count": len(conv_list),
                        "attempt_errors": [],
                    }
                else:
                    outcomes_by_id[mid] = {
                        "status": "failed",
                        "stage_outcome": "failed",
                        "error_code": ERR_AI_MAX_RETRIES_EXCEEDED,
                        "error_msg": f"AI-2 连续 {new_retries} 次超时或未返回结果，达最大重试上限({MAX_CLEAN_RETRIES}次)，永久放弃",
                        "retry_count": new_retries,
                        "source_site": site,
                        "source_channel": channel,
                        "source_item_id": mid,
                        "title": raw_title,
                        "ai_stage1_result": ai1_res,
                        "converted_links": conv_summary_for_db,
                        "ai_stage2_result": ai2_res,
                        "quality_score": 0,
                        "candidate_count": len(conv_list),
                        "attempt_errors": [],
                    }
                continue

            link_map = {c["link_id"]: c for c in conv_list}
            is_valid = bool(ai2_res.get("is_valid_deal"))
            main_lid = str(ai2_res.get("main_link_id") or "").strip()
            main_conv = link_map.get(main_lid)
            if is_valid and (
                not main_conv
                or main_conv.get("convert_status") != "product"
                or main_conv.get("text_role_hint") == "filler_candidate"
            ):
                is_valid = False
                if not ai2_res.get("reject_reason"):
                    ai2_res["reject_reason"] = "MAIN_PRODUCT_DEAD_ONLY_FILLER"

            if not is_valid or not main_conv:
                rej = str(ai2_res.get("reject_reason") or "ALL_PRODUCTS_DEAD").upper()
                if "COUPON" in rej:
                    err_code = ERR_AI2_COUPON_FAILED_OR_DROPPED
                elif "FILLER" in rej:
                    err_code = ERR_AI2_MAIN_DEAD_ONLY_FILLER
                elif "MISMATCH" in rej:
                    err_code = ERR_AI2_PRODUCT_MISMATCH
                elif "VIRTUAL" in rej or "JUNK" in rej:
                    err_code = ERR_AI2_VIRTUAL_OR_JUNK
                else:
                    err_code = ERR_ADAPTER_CONVERT_FAILED
                outcomes_by_id[mid] = {
                    "status": "failed",
                    "stage_outcome": "failed",
                    "error_code": err_code,
                    "error_msg": f"AI-2 终审拦截 ({rej})",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_for_db,
                    "ai_stage2_result": ai2_res,
                    "quality_score": 0,
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                }
                continue

            try:
                quality_score = int(float(ai2_res.get("quality_score") or 0))
            except Exception:
                quality_score = 0
            quality_score = max(0, min(100, quality_score))
            score_breakdown = ai2_res.get("score_breakdown") if isinstance(ai2_res.get("score_breakdown"), dict) else {}

            # 高质量信源信誉加分 (Source Reputation Bonus)
            # 严格门禁：1. is_valid 且主商品单品验真存活 (前序已校验通过)；2. 原始分达到及格线 (>=60)；3. 上限 100 封顶截断
            source_bonus = (
                SOURCE_REPUTATION_BONUS.get(f"{site}:{channel}")
                or SOURCE_REPUTATION_BONUS.get(site, 0)
            )
            if source_bonus > 0 and quality_score >= QUALIFIED_DEAL_MIN_SCORE:
                raw_base_score = quality_score
                quality_score = min(100, quality_score + source_bonus)
                score_breakdown["base_quality_score"] = raw_base_score
                score_breakdown["source_reputation_bonus"] = source_bonus
                logger.info(
                    "站点 [%s:%s] 命中高质量信源加分: 原始分 %d -> 加权分 %d (+%d)",
                    site, channel, raw_base_score, quality_score, source_bonus,
                )

            plat = str(main_conv.get("platform") or "")
            goods_id = str(main_conv.get("goods_id") or "")
            goods_sign = str(main_conv.get("goods_sign") or "")
            adapter_res = main_conv.get("adapter_res") if isinstance(main_conv.get("adapter_res"), dict) else {}
            product_card = copy.deepcopy(adapter_res.get("product") if isinstance(adapter_res.get("product"), dict) else {})
            off_prod_fallback = main_conv.get("official_product") if isinstance(main_conv.get("official_product"), dict) else {}
            if not product_card and off_prod_fallback:
                product_card = {
                    "name": off_prod_fallback.get("title") or "",
                    "price": off_prod_fallback.get("price") or 0.0,
                    "original_price": off_prod_fallback.get("original_price") or off_prod_fallback.get("price") or 0.0,
                    "activity_price": off_prod_fallback.get("activity_price") or off_prod_fallback.get("price") or 0.0,
                    "coupon_amount": off_prod_fallback.get("coupon_amount") or 0.0,
                    "mall_name": off_prod_fallback.get("shop_name") or "",
                    "commission_rate": float(off_prod_fallback.get("commission_rate") or 0.0),
                    "commission_amount": float(off_prod_fallback.get("commission_amount") or 0.0),
                    "image_url": str(off_prod_fallback.get("image_url") or "").strip(),
                    "carousel_images": off_prod_fallback.get("carousel_images") or [],
                    "category": str(off_prod_fallback.get("category") or ""),
                    "brand": str(off_prod_fallback.get("brand") or ""),
                    "sales_tip": str(off_prod_fallback.get("sales_tip") if off_prod_fallback.get("sales_tip") is not None else "0"),
                }

            my_promo_link = str(main_conv.get("my_promo_link") or "")
            my_promo_token = str(main_conv.get("my_promo_token") or "")

            raw_deal_p = ai2_res.get("deal_price")
            deal_price: Optional[float] = None
            if isinstance(raw_deal_p, (int, float)) and 0 < float(raw_deal_p) < 100000:
                deal_price = round(float(raw_deal_p), 2)
            elif isinstance(raw_deal_p, str):
                try:
                    val = float(re.sub(r"[^\d.]", "", raw_deal_p))
                    if 0 < val < 100000:
                        deal_price = round(val, 2)
                except Exception:
                    pass

            unit_deal_price = _normalize_inline_spaces(str(ai2_res.get("unit_deal_price") or "")).strip() or None
            price_logic = _normalize_inline_spaces(str(ai2_res.get("price_logic") or "")).strip() or None
            card_highlight = self._sanitize_card_highlight(ai2_res.get("card_highlight"))
            clean_title = _normalize_inline_spaces(str(ai2_res.get("clean_title") or product_card.get("name") or raw_title)).strip()

            def _is_other_known_platform(c: Dict[str, Any]) -> bool:
                c_p = str(c.get("platform") or "").strip().lower()
                return bool(plat in ("jd", "taobao", "pdd") and c_p in ("jd", "taobao", "pdd") and c_p != plat)

            coupon_links: List[Dict[str, Any]] = []
            dropped_cross_platform_coupon = False
            seen_cpn_ids: Set[str] = set()
            seen_cpn_targets: Set[Tuple[str, str]] = set()
            for item in ai2_res.get("coupon_links") or []:
                if not isinstance(item, dict):
                    continue
                lid = str(item.get("link_id") or "").strip()
                c_obj = link_map.get(lid)
                if (
                    not c_obj
                    or c_obj.get("convert_status") in ("failed", "skipped_filler")
                    or lid == main_lid
                    or lid in seen_cpn_ids
                ):
                    continue
                c_plat = str(c_obj.get("platform") or plat).strip().lower()
                if _is_other_known_platform(c_obj):
                    dropped_cross_platform_coupon = True
                    continue
                if goods_id and str(c_obj.get("goods_id") or "").strip() == goods_id:
                    continue
                c_url = str(c_obj.get("my_promo_link") or "").strip()
                c_tok = str(c_obj.get("my_promo_token") or "").strip()
                if not c_url and not c_tok:
                    continue
                if c_url and my_promo_link and c_url == my_promo_link:
                    continue
                if plat == "taobao":
                    c_host = (urlsplit(c_url).hostname or "").strip().lower() if c_url else ""
                    if c_obj.get("convert_status") == "product" or c_host in ("s.click.taobao.com", "s.click.tmall.com"):
                        continue
                dedup_sig = (c_url, c_tok)
                if dedup_sig in seen_cpn_targets:
                    continue
                seen_cpn_ids.add(lid)
                seen_cpn_targets.add(dedup_sig)
                coupon_links.append({
                    "link_id": lid,
                    "label": _clean_coupon_label(item.get("label") or item.get("coupon_desc") or "优惠券"),
                    "url": c_url,
                    "token": c_tok,
                    "platform": c_plat or plat,
                })

            # 【优惠券与断链一致性拦截（Fail-Closed 铁律）】
            # 若最终未产出任何有效优惠券（not coupon_links）：
            # 1. 且非“已被主商品二合一推广链吸收的同品券链”（has_absorbed_coupon）；
            # 2. 若原始线报仍包含未转出有效券且未被吸收的同平台显式优惠券链接，或包含非纯数字 SKU 拒识的同平台转链失败非凑单链接且到手价显著低于官方售价，
            # 3. 或 AI-2 将异平台链接误配作主商品优惠券导致被剔除（防跨平台串货），
            # 满足任一即表明好价前置动作已断裂或错配，全条线报予以淘汰，绝不脑补或串货放行。
            def _is_coupon_absorbed_by_main(c: Dict[str, Any]) -> bool:
                if (
                    c.get("link_id") == main_lid
                    or c.get("convert_status") != "product"
                    or _is_other_known_platform(c)
                ):
                    return False
                c_gid = str(c.get("goods_id") or "").strip()
                c_url = str(c.get("my_promo_link") or "").strip()
                return bool(
                    (goods_id and c_gid == goods_id)
                    or (my_promo_link and c_url == my_promo_link)
                    or plat == "taobao"
                )

            has_absorbed_coupon = any(
                self._is_explicit_coupon_target(c) and _is_coupon_absorbed_by_main(c)
                for c in conv_list
            )
            if is_valid and not coupon_links and (dropped_cross_platform_coupon or not has_absorbed_coupon):
                should_reject = False
                reject_detail = ""
                if dropped_cross_platform_coupon:
                    should_reject = True
                    reject_detail = "AI-2 错将异平台链接混作主商品优惠券，防跨平台串货拦截"
                else:
                    raw_coupon_candidates = [
                        c for c in conv_list
                        if c.get("link_id") != main_lid
                        and c.get("text_role_hint") != "filler_candidate"
                        and not _is_other_known_platform(c)
                        and not _is_coupon_absorbed_by_main(c)
                        and self._is_explicit_coupon_target(c)
                    ]
                    if raw_coupon_candidates:
                        should_reject = True
                        reject_detail = "原始优惠券链接转链失败或未被保留"
                    elif deal_price is not None:
                        has_failed_non_filler = any(
                            c.get("link_id") != main_lid
                            and c.get("text_role_hint") != "filler_candidate"
                            and not _is_other_known_platform(c)
                            and c.get("convert_status") == "failed"
                            and c.get("error_code") not in (ERR_JD_NUMERIC_SKU, ERR_TB_NUMERIC_ID, ERR_PDD_NUMERIC_ID)
                            for c in conv_list
                        )
                        if has_failed_non_filler:
                            off_act_p = float(product_card.get("activity_price") or product_card.get("price") or 0.0)
                            if off_act_p > 0 and deal_price < off_act_p * 0.95:
                                should_reject = True
                                reject_detail = "依赖的前置优惠链接转链失效且到手价低于官方售价"

                if should_reject:
                    is_valid = False
                    ai2_res["is_valid_deal"] = False
                    ai2_res["reject_reason"] = "COUPON_LINK_FAILED_OR_DROPPED"
                    outcomes_by_id[mid] = {
                        "status": "failed",
                        "stage_outcome": "failed",
                        "error_code": ERR_AI2_COUPON_FAILED_OR_DROPPED,
                        "error_msg": f"AI-2 终审拦截 (COUPON_LINK_FAILED_OR_DROPPED: {reject_detail})",
                        "source_site": site,
                        "source_channel": channel,
                        "source_item_id": mid,
                        "title": raw_title,
                        "ai_stage1_result": ai1_res,
                        "converted_links": conv_summary_for_db,
                        "ai_stage2_result": ai2_res,
                        "quality_score": 0,
                        "candidate_count": len(conv_list),
                        "attempt_errors": [],
                    }
                    continue

            raw_steps_val = ai2_res.get("tutorial_steps")
            if isinstance(raw_steps_val, list):
                raw_step_lines = [str(st or "") for st in raw_steps_val]
            elif isinstance(raw_steps_val, str) and raw_steps_val.strip():
                raw_step_lines = raw_steps_val.splitlines()
            else:
                raw_step_lines = str(ai2_res.get("tutorial_summary") or "").splitlines()
            tutorial_steps = [
                s for s in (self._sanitize_clean_tutorial_step(st) for st in raw_step_lines)
                if s
            ]
            tutorial_text = "\n".join(tutorial_steps)

            official_img = str(product_card.get("image_url") or "").strip()
            extracted_imgs = [
                self._strip_markdown_link_wrapper(str(u).strip())
                for u in (ai1_res.get("extracted_images") or [])
                if str(u or "").strip()
            ]
            user_share_images: List[str] = []
            for img_u in extracted_imgs:
                if img_u and img_u != official_img and img_u not in user_share_images:
                    user_share_images.append(img_u)

            merged_tip_ids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
            if mid not in merged_tip_ids:
                merged_tip_ids.insert(0, mid)
            tip_map = (neighbor_maps_by_id.get(mid) if neighbor_maps_by_id else None) or self._load_neighbor_docs(
                raw_col, site, merged_tip_ids, current_doc=doc
            )
            merged_source_urls: List[str] = []
            for m_tid in merged_tip_ids:
                nd = tip_map.get(m_tid) or {}
                u_det = str(nd.get("source_detail_url") or nd.get("source_url") or "").strip()
                if u_det and u_det not in merged_source_urls:
                    merged_source_urls.append(u_det)

            raw_polished_copy = str(ai2_res.get("polished_copywriting") or "").strip()
            original_copywriting = (
                self._sanitize_polished_copywriting(raw_polished_copy) if raw_polished_copy else ""
            ) or draft_copy_by_id.get(mid) or self._build_clean_original_copywriting(
                merged_tip_ids,
                tip_map,
                ai1_res.get("extracted_targets") or [],
                doc=doc,
                strip_spans=ai1_res.get("strip_spans") or [],
            )

            source_list_page_url = str(doc.get("source_list_page_url") or doc.get("source_url") or "").strip()
            source_detail_url = str(doc.get("source_detail_url") or doc.get("source_url") or source_list_page_url).strip()

            official_price = float(product_card.get("price") or adapter_res.get("price") or 0.0)
            original_price = float(product_card.get("original_price") or official_price)
            official_activity_price = float(product_card.get("activity_price") or official_price)
            coupon_amount = float(product_card.get("coupon_amount") or adapter_res.get("coupon_amount") or 0.0)
            saved_price = round(max(coupon_amount, max(0.0, original_price - official_activity_price)), 2)

            publish_time_str = str(doc.get("publish_time") or doc.get("fetched_at") or "").strip()

            site_name = get_source_site_name(site)
            provider_nickname = str((doc.get("hot_metrics") or {}).get("provider_nickname") or "").strip()

            deal_info = {
                "is_deal_tip": True,
                "quality_score": quality_score,
                "score_breakdown": score_breakdown,
                "clean_title": clean_title,
                "card_highlight": card_highlight,
                "publish_time": publish_time_str,
                "expire_time": doc.get("expire_time"),
                "deal_price": deal_price,
                "unit_deal_price": unit_deal_price,
                "price_logic": price_logic,
                "official_price": round(official_price, 2),
                "raw_tip_title": raw_title or str(product_card.get("name") or ""),
                "original_copywriting": original_copywriting,
                "coupon_links": coupon_links,
                "tutorial_steps": tutorial_steps,
                "tutorial_text": tutorial_text,
                "user_share_images": user_share_images,
                "my_promotion_link": my_promo_link,
                "my_promotion_token": my_promo_token,
                "source_site": site,
                "source_site_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "provider_nickname": provider_nickname,
                "source_url": source_detail_url,
                "source_list_page_url": source_list_page_url,
                "source_detail_url": source_detail_url,
                "merged_tip_ids": merged_tip_ids,
                "merged_source_urls": merged_source_urls,
                "seen_count": int(doc.get("seen_count") or 1),
                "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                "hot_metrics": doc.get("hot_metrics") or {},
            }

            product_doc = {
                **product_card,
                "platform": plat,
                "platform_name": product_card.get("platform_name") or platform_name(plat),
                "product_id": goods_id,
                "goods_id": goods_id,
                "goods_sign": goods_sign,
                "deal_source": site,
                "deal_source_name": site_name,
                "name": str(product_card.get("name") or adapter_res.get("title") or clean_title).strip(),
                "brand": str(product_card.get("brand") or product_card.get("brand_name") or ""),
                "category": str(product_card.get("category") or product_card.get("category_name") or ""),
                "price": round(official_price, 2),
                "original_price": round(original_price, 2),
                "activity_price": round(official_activity_price, 2),
                "saved_price": round(saved_price, 2),
                "coupon_amount": round(coupon_amount, 2),
                "has_coupon": coupon_amount > 0,
                "commission_rate": float(product_card.get("commission_rate") or 0.0),
                "commission_amount": float(product_card.get("commission_amount") or 0.0),
                "sales_tip": str(product_card.get("sales_tip") if product_card.get("sales_tip") is not None else "0"),
                "image_url": official_img or str(doc.get("image_url") or ""),
                "product_url": str(product_card.get("product_url") or my_promo_link or ""),
                "mall_name": str(product_card.get("mall_name") or doc.get("mall_name") or ""),
                "desc": str(product_card.get("desc") or ""),
                "carousel_images": product_card.get("carousel_images") or ([official_img] if official_img else []),
                "detail_tags": product_card.get("detail_tags") or [],
                "service_guarantees": product_card.get("service_guarantees") or [],
                "shop_ratings": product_card.get("shop_ratings") or {},
                "specs": product_card.get("specs") or [],
                "coupon_info": product_card.get("coupon_info") or {},
                "deal_info": deal_info,
            }

            if quality_score < QUALIFIED_DEAL_MIN_SCORE:
                outcomes_by_id[mid] = {
                    "status": "low_score",
                    "stage_outcome": "low_score",
                    "error_code": ERR_LOW_QUALITY_SCORE,
                    "error_msg": f"综合评分 {quality_score} < {QUALIFIED_DEAL_MIN_SCORE} 分，仅留存初步表不入正式表",
                    "source_site": site,
                    "source_site_name": site_name,
                    "deal_source": site,
                    "deal_source_name": site_name,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": plat,
                    "product_id": goods_id,
                    "goods_sign": goods_sign,
                    "official_title": product_doc["name"],
                    "title": raw_title,
                    "deal_price": deal_price,
                    "official_price": official_price,
                    "quality_score": quality_score,
                    "score_breakdown": score_breakdown,
                    "merged_tip_ids": merged_tip_ids,
                    "original_copywriting": original_copywriting,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_for_db,
                    "ai_stage2_result": ai2_res,
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                }
                continue

            outcomes_by_id[mid] = {
                "status": "success",
                "stage_outcome": "success",
                "error_code": "OK",
                "error_msg": "",
                "source_site": site,
                "source_channel": channel,
                "source_item_id": mid,
                "platform": plat,
                "product_id": goods_id,
                "goods_sign": goods_sign,
                "official_title": product_doc["name"],
                "raw_title": raw_title,
                "deal_price": deal_price,
                "official_price": official_price,
                "quality_score": quality_score,
                "score_breakdown": score_breakdown,
                "merged_tip_ids": merged_tip_ids,
                "original_copywriting": original_copywriting,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_for_db,
                "ai_stage2_result": ai2_res,
                "winning_anchor": str(main_conv.get("clean_input") or "")[:100],
                "winning_anchor_type": main_conv.get("text_role_hint") or "main_candidate",
                "candidate_count": len(conv_list),
                "tutorial_img_count": len(user_share_images),
                "coupon_link_count": len(coupon_links),
                "product_doc": product_doc,
            }

        out_list = [outcomes_by_id[str(d.get("source_item_id") or "")] for d in selected_docs if str(d.get("source_item_id") or "") in outcomes_by_id]
        if not quiet_sub_logs:
            succ_cnt = sum(1 for o in out_list if o.get("status") == "success")
            low_cnt = sum(1 for o in out_list if o.get("status") in ("low_score", "cleaned_low_score"))
            retry_cnt = sum(1 for o in out_list if o.get("status") == "retry_pending")
            fail_cnt = sum(1 for o in out_list if o.get("status") in ("failed", "cleaned_failed"))
            logger.info(
                "🧠 [AI-2 终审完成] 耗时=%.1fs | 本批认领=%d款新商品 (正式库好价入库=%d件, 低分存档=%d件, 留待重试=%d件, 淘汰=%d件)",
                t_ai2_elapsed,
                len(selected_docs),
                succ_cnt,
                low_cnt,
                retry_cnt,
                fail_cnt,
            )
        return out_list, t_ai2_elapsed

    def clean_batch_records(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col,
        formal_col=None,
        total_pending_count: int = 0,
        shared_active_products: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
        shared_lock: Optional[threading.Lock] = None,
        quiet_sub_logs: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        对一批记录完整执行：
        Stage 2 (AI-1) -> Stage 3 (官方转链) -> Stage 4 (AI-2) 顺序驱动（内部由各阶段单元函数纯净执行）。
        """
        if not selected_docs:
            return []

        t_batch_start = time.perf_counter()
        final_by_mid: Dict[str, Dict[str, Any]] = {}

        # 1. Stage 2 (AI-1 拆解)
        ai1_outcomes, t_ai1_elapsed, neighbor_maps = self.process_ai1_unit(
            selected_docs,
            raw_col,
            formal_col=formal_col,
            total_pending_count=total_pending_count,
            shared_active_products=shared_active_products,
            shared_lock=shared_lock,
            quiet_sub_logs=quiet_sub_logs,
        )
        ai1_map = {str(r.get("source_item_id") or ""): r for r in ai1_outcomes}

        adv_to_convert: List[Dict[str, Any]] = []
        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            out = ai1_map.get(mid)
            if not out:
                continue
            if out.get("stage_outcome") == "advance_to_convert":
                d_copy = copy.deepcopy(doc)
                d_copy["ai_stage1_result"] = out.get("ai_stage1_result")
                adv_to_convert.append(d_copy)
            else:
                final_by_mid[mid] = out

        # 2. Stage 3 (官方转链)
        t_stage3_elapsed = 0.0
        adv_to_ai2: List[Dict[str, Any]] = []
        if adv_to_convert:
            conv_outcomes, t_stage3_elapsed = self.process_convert_unit(
                adv_to_convert,
                formal_col=formal_col,
                total_pending_count=total_pending_count,
                shared_active_products=shared_active_products,
                shared_lock=shared_lock,
                quiet_sub_logs=quiet_sub_logs,
            )
            conv_map = {str(r.get("source_item_id") or ""): r for r in conv_outcomes}
            for doc in adv_to_convert:
                mid = str(doc.get("source_item_id") or "")
                out = conv_map.get(mid)
                if not out:
                    continue
                if out.get("stage_outcome") == "advance_to_ai2":
                    d_copy = copy.deepcopy(doc)
                    d_copy["converted_links"] = out.get("converted_links") or []
                    d_copy["full_conv_list"] = out.get("full_conv_list") or []
                    adv_to_ai2.append(d_copy)
                else:
                    final_by_mid[mid] = out

        # 3. Stage 4 (AI-2 终审)
        t_ai2_elapsed = 0.0
        if adv_to_ai2:
            ai2_outcomes, t_ai2_elapsed = self.process_ai2_unit(
                adv_to_ai2,
                raw_col=raw_col,
                formal_col=formal_col,
                total_pending_count=total_pending_count,
                neighbor_maps_by_id=neighbor_maps,
                quiet_sub_logs=quiet_sub_logs,
            )
            for out in ai2_outcomes:
                mid = str(out.get("source_item_id") or "")
                final_by_mid[mid] = out

        self.last_batch_stage_timings = {
            "stage2_ai1_seconds": t_ai1_elapsed,
            "stage3_convert_seconds": t_stage3_elapsed,
            "stage4_ai2_seconds": t_ai2_elapsed,
        }

        avg_ms = round((time.perf_counter() - t_batch_start) * 1000.0 / max(1, len(selected_docs)), 1)
        final_results: List[Dict[str, Any]] = []
        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if mid in final_by_mid:
                item = final_by_mid[mid]
                item["elapsed_ms"] = avg_ms
                final_results.append(item)

        if final_results:
            final_results[0]["_stage_timings"] = self.last_batch_stage_timings

        return final_results


def upsert_cleaned_products_to_formal_table(
    formal_col,
    product_docs: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    【Stage 5：高分好价商品晋升入正式表 `products`（含质量评分防覆盖保护）】
    - 严格以唯一索引 `{"platform": plat, "product_id": pid}` 作为过滤条件；
    - 顶层商品属性 (`name`, `price`, `original_price`, `activity_price` 等) 始终保持官方最新数据；
    - 高分保护法则：
      1) 同批次多条线报命中同一商品时，按 `deal_info.quality_score` 最高者胜出；
      2) 若 `products` 表中该商品已有 `deal_info.is_deal_tip == True` 且 `updated_at` 在 12 小时保鲜期内，
         仅当新线报 `quality_score >= 已有 deal_info.quality_score` 时才更新 `deal_info` 内容，
         防止低分简陋线报覆盖带优惠券、带凑单教程、带实付晒单图的高分精品线报！
    """
    if not product_docs:
        return {"upserted": 0, "modified": 0, "matched": 0, "protected_high_score": 0, "total_ops": 0}

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    protection_cutoff = now - datetime.timedelta(hours=DEAL_INFO_PROTECTION_HOURS)

    grouped_by_pid: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for doc in product_docs:
        grouped_by_pid[(doc["platform"], doc["product_id"])].append(doc)

    # 批量查询正式表中已存在的同款商品，检查其现有 deal_info.quality_score 与更新时间
    existing_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        or_queries = [{"platform": p, "product_id": pid} for (p, pid) in grouped_by_pid.keys()]
        if or_queries:
            for ex_doc in formal_col.find(
                {"$or": or_queries},
                {
                    "_id": 0,
                    "platform": 1,
                    "product_id": 1,
                    "deal_info.is_deal_tip": 1,
                    "deal_info.quality_score": 1,
                    "deal_info.deal_price": 1,
                    "deal_info.updated_at": 1,
                },
            ):
                k = (str(ex_doc.get("platform") or ""), str(ex_doc.get("product_id") or ""))
                existing_map[k] = ex_doc
    except Exception as e:
        logger.warning("查询正式表已有商品评分异常: %s", e)

    ops = []
    protected_high_score = 0

    for (plat, pid), docs_for_item in grouped_by_pid.items():
        # 按 quality_score 升序排列，取最高分的一条作为胜出者
        docs_sorted = sorted(
            docs_for_item,
            key=lambda x: int((x.get("deal_info") or {}).get("quality_score") or 0),
        )
        best_doc = copy.deepcopy(docs_sorted[-1])
        deal_info = copy.deepcopy(best_doc.pop("deal_info", {}) or {})
        new_score = int(deal_info.get("quality_score") or 0)

        total_seen_inc = 0
        all_sites: List[str] = []
        all_channels: List[str] = []
        for d in docs_for_item:
            di = d.get("deal_info") or {}
            total_seen_inc += max(1, int(di.get("seen_count") or 1))
            s_site = str(di.get("source_site") or "").strip()
            if s_site and s_site not in all_sites:
                all_sites.append(s_site)
            for ch in di.get("seen_channels") or []:
                ch_s = str(ch or "").strip()
                if ch_s and ch_s not in all_channels:
                    all_channels.append(ch_s)

        deal_info.pop("seen_count", None)
        deal_info.pop("seen_channels", None)

        set_fields: Dict[str, Any] = {
            **best_doc,
            "updated_at": now,
            "last_clean_source": "deal_pool",
        }
        # 仅当商品真实带有官方高清轮播图且已包含有效佣金时，才标记详情已更新；
        # 否则避免打上伪造的 detail_updated_at，以便后台 DetailPrefetcher 能自然发现并预取自愈
        if len(best_doc.get("carousel_images") or []) > 1 and float(best_doc.get("commission_amount") or 0.0) > 0:
            set_fields["detail_updated_at"] = now

        ex_record = existing_map.get((plat, pid))
        ex_di = (ex_record or {}).get("deal_info") if isinstance((ex_record or {}).get("deal_info"), dict) else {}
        ex_score = int(ex_di.get("quality_score") or 0)
        ex_deal_updated = _to_naive_utc(ex_di.get("updated_at"))
        is_fresh_existing = bool(
            ex_di.get("is_deal_tip")
            and ex_deal_updated is not None
            and ex_deal_updated >= protection_cutoff
        )

        new_dp = (
            float(deal_info["deal_price"])
            if isinstance(deal_info.get("deal_price"), (int, float))
            and not isinstance(deal_info.get("deal_price"), bool)
            and float(deal_info["deal_price"]) > 0
            else None
        )
        ex_dp = (
            float(ex_di["deal_price"])
            if isinstance(ex_di.get("deal_price"), (int, float))
            and not isinstance(ex_di.get("deal_price"), bool)
            and float(ex_di["deal_price"]) > 0
            else None
        )
        price_improved = bool(new_dp is not None and ex_dp is not None and new_dp < ex_dp * 0.98)

        if is_fresh_existing and ex_score > new_score and not price_improved:
            # 触发高分保护：保留已有的高分 deal_info 教程与价格，仅更新顶层官方商品详情并累加跨群共振热度
            protected_high_score += 1
        else:
            has_numeric_dp = (
                isinstance(deal_info.get("deal_price"), (int, float))
                and not isinstance(deal_info.get("deal_price"), bool)
                and float(deal_info["deal_price"]) > 0
            )
            deal_info["updated_at"] = now
            deal_info["expired_at"] = None
            deal_info["need_reformat"] = has_numeric_dp
            deal_info["format_status"] = "pending" if has_numeric_dp else "skipped"
            deal_info["format_info"] = None
            deal_info["format_deal_price"] = None
            deal_info["format_retry_count"] = 0
            deal_info["format_error"] = ""
            for dk, dv in deal_info.items():
                set_fields[f"deal_info.{dk}"] = dv

        # 收集本批次中关于该商品的所有有效线报时间线条目
        batch_timeline_entries: List[Dict[str, Any]] = []
        seen_tids: Set[str] = set()
        for d in docs_for_item:
            di = d.get("deal_info") or {}
            mid = str(di.get("source_item_id") or d.get("product_id") or "")
            if mid and mid not in seen_tids:
                seen_tids.add(mid)
                pub_t = str(di.get("publish_time") or "")
                s_site = str(di.get("source_site") or "")
                s_name = str(di.get("source_site_name") or get_source_site_name(s_site))
                p_nick = str(di.get("provider_nickname") or (di.get("hot_metrics") or {}).get("provider_nickname") or "")
                batch_timeline_entries.append({
                    "tip_id": mid,
                    "source_site": s_site,
                    "source_site_name": s_name,
                    "source_channel": str(di.get("source_channel") or ""),
                    "provider_nickname": p_nick,
                    "publish_time": pub_t,
                    "publish_ts": parse_publish_time_epoch(pub_t) or 0.0,
                    "title": str(di.get("raw_tip_title") or d.get("name") or ""),
                    "copywriting": str(di.get("original_copywriting") or "")[:300],
                    "deal_price": di.get("deal_price"),
                    "tutorial_score": int(di.get("tutorial_score") or di.get("quality_score") or 75),
                })

        update_spec: Dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {
                "created_at": now,
                "source": "deal_pool",
            },
            "$inc": {
                "deal_info.total_seen_count": total_seen_inc,
                "deal_info.tip_hit_count": len(docs_for_item),
            },
        }
        if batch_timeline_entries:
            update_spec["$push"] = {
                "deal_info.tips_timeline": {
                    "$each": batch_timeline_entries,
                    "$sort": {"publish_ts": -1},
                    "$slice": 20,
                }
            }
        add_to_set: Dict[str, Any] = {}
        if all_sites:
            add_to_set["deal_info.resonance_sites"] = {"$each": all_sites}
        if all_channels:
            add_to_set["deal_info.resonance_channels"] = {"$each": all_channels}
        if add_to_set:
            update_spec["$addToSet"] = add_to_set

        ops.append(UpdateOne({"platform": plat, "product_id": pid}, update_spec, upsert=True))

    res = formal_col.bulk_write(ops, ordered=False)
    try:
        from infrastructure.search_meili import index_documents, is_meili_available
        if is_meili_available():
            index_documents(product_docs)
    except Exception as meili_err:
        logger.warning("线报商品写入正式表后同步 Meilisearch 异常: %s", meili_err)

    return {
        "upserted": res.upserted_count,
        "modified": res.modified_count,
        "matched": res.matched_count,
        "protected_high_score": protected_high_score,
        "total_ops": len(ops),
    }


def _commit_batch_results_to_db(
    batch_results: List[Dict[str, Any]],
    raw_col,
    formal_col,
    cycle_num: int,
    shared_active_products: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    shared_lock: Optional[threading.Lock] = None,
) -> Dict[str, Any]:
    """
    将本轮清洗窗口或阶段工作单元的结果统一写回初步表 `deal_raw_tips` 与正式表 `products`：
    - 支持阶段流转：advance_to_convert (stage=need_convert) 与 advance_to_ai2 (stage=need_ai2)；
    - 终态记录统一标记 clean_stage=done 并剥离租约与临时中间字段；
    - 超时跳过重试 (retry_pending) 保持当前 clean_stage、解除租约并累加 clean_retry_count。
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    raw_status_ops = []
    batch_success_product_docs: List[Dict[str, Any]] = []
    batch_early_terminated_results: List[Dict[str, Any]] = []
    batch_failed_samples: List[Dict[str, Any]] = []
    retry_pending_count = 0

    for r in batch_results:
        s_site = r["source_site"]
        s_id = r["source_item_id"]
        common_ai_fields = {
            "cleaned_at": now_utc,
            "ai_stage1_result": r.get("ai_stage1_result"),
            "converted_links": r.get("converted_links") or [],
            "ai_stage2_result": r.get("ai_stage2_result"),
            "quality_score": r.get("quality_score", 0),
        }
        if r.get("original_copywriting"):
            common_ai_fields["original_copywriting"] = r["original_copywriting"]

        stage_outcome = r.get("stage_outcome")

        # 1. 阶段中间状态推进：由 AI-1 推进至 转链
        if stage_outcome == "advance_to_convert":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            "clean_stage": CLEAN_STAGE_NEED_CONVERT,
                            "ai_stage1_result": r.get("ai_stage1_result"),
                            "stage1_cleaned_at": now_utc,
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )
            )
        # 2. 阶段中间状态推进：由 转链 推进至 AI-2
        elif stage_outcome == "advance_to_ai2":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            "clean_stage": CLEAN_STAGE_NEED_AI2,
                            "converted_links": r.get("converted_links") or [],
                            "stage3_converted_links": r.get("converted_links") or [],
                            "stage2_converted_at": now_utc,
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )
            )
        elif r["status"] == "success":
            batch_success_product_docs.append(r["product_doc"])
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_success",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": "OK",
                            "clean_error_msg": "",
                            "resolved_platform": r["platform"],
                            "resolved_product_id": r["product_id"],
                            "resolved_goods_sign": r["goods_sign"],
                            "resolved_official_title": r["official_title"],
                            "merged_tip_ids": r.get("merged_tip_ids") or [s_id],
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                            "stage3_converted_links": "",
                        },
                    },
                )
            )
            for m_tid in r.get("merged_tip_ids") or []:
                if m_tid != s_id:
                    raw_status_ops.append(
                        UpdateOne(
                            {"source_site": s_site, "source_item_id": m_tid, "clean_status": {"$in": ["pending", "context_only"]}},
                            {
                                "$set": {
                                    "clean_status": "merged_into_parent",
                                    "clean_stage": CLEAN_STAGE_DONE,
                                    "merged_into_tip_id": s_id,
                                    "cleaned_at": now_utc,
                                },
                                "$unset": {
                                    "stage_lease_owner": "",
                                    "stage_lease_until_ts": "",
                                    "stage3_converted_links": "",
                                },
                            },
                        )
                    )
        elif r["status"] == "early_terminated":
            batch_early_terminated_results.append(r)
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_success",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": OK_SKIPPED_ALREADY_CLEANED,
                            "clean_error_msg": r["error_msg"],
                            "resolved_platform": r.get("platform"),
                            "resolved_product_id": r.get("product_id"),
                            "resolved_goods_sign": r.get("goods_sign") or "",
                            "resolved_official_title": r.get("official_title") or "",
                            "merged_tip_ids": r.get("merged_tip_ids") or [s_id],
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                            "stage3_converted_links": "",
                        },
                    },
                )
            )
            for m_tid in r.get("merged_tip_ids") or []:
                if m_tid != s_id:
                    raw_status_ops.append(
                        UpdateOne(
                            {"source_site": s_site, "source_item_id": m_tid, "clean_status": {"$in": ["pending", "context_only"]}},
                            {
                                "$set": {
                                    "clean_status": "merged_into_parent",
                                    "clean_stage": CLEAN_STAGE_DONE,
                                    "merged_into_tip_id": s_id,
                                    "cleaned_at": now_utc,
                                },
                                "$unset": {
                                    "stage_lease_owner": "",
                                    "stage_lease_until_ts": "",
                                    "stage3_converted_links": "",
                                },
                            },
                        )
                    )
        elif r["status"] == "merged_into_parent":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "merged_into_parent",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "merged_into_tip_id": r.get("merged_into_tip_id") or "",
                            "clean_error_code": r["error_code"],
                            "clean_error_msg": r["error_msg"],
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                            "stage3_converted_links": "",
                        },
                    },
                )
            )
        elif r["status"] == "retry_pending":
            retry_pending_count += 1
            r_cnt = int(r.get("retry_count") or 1)
            retry_set = {
                "last_clean_attempt_at": now_utc,
                "clean_status": "pending",
                "clean_retry_count": r_cnt,
                "next_retry_after_ts": round(time.time() + 180.0 * r_cnt, 1),
                "clean_error_code": r["error_code"],
                "clean_error_msg": r["error_msg"],
                "ai_stage1_result": r.get("ai_stage1_result"),
                "converted_links": r.get("converted_links") or [],
                "ai_stage2_result": r.get("ai_stage2_result"),
            }
            if r.get("clean_stage"):
                retry_set["clean_stage"] = r["clean_stage"]
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": retry_set,
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )
            )
        elif r["status"] == "low_score":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_low_score",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": r["error_code"],
                            "clean_error_msg": r["error_msg"],
                            "resolved_platform": r.get("platform"),
                            "resolved_product_id": r.get("product_id"),
                            "resolved_goods_sign": r.get("goods_sign"),
                            "resolved_official_title": r.get("official_title"),
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                            "stage3_converted_links": "",
                        },
                    },
                )
            )
        else:
            fail_set: Dict[str, Any] = {
                **common_ai_fields,
                "clean_status": "cleaned_failed",
                "clean_stage": CLEAN_STAGE_DONE,
                "clean_error_code": r["error_code"],
                "clean_error_msg": r["error_msg"],
                "clean_attempt_errors": r.get("attempt_errors") or [],
            }
            if "retry_count" in r:
                fail_set["clean_retry_count"] = int(r["retry_count"])
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": fail_set,
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                            "stage3_converted_links": "",
                        },
                    },
                )
            )
            batch_failed_samples.append({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "cycle_num": cycle_num,
                "source_site": s_site,
                "source_channel": r.get("source_channel"),
                "source_item_id": s_id,
                "error_code": r["error_code"],
                "error_msg": r["error_msg"],
                "title": r.get("title"),
                "attempt_errors": r.get("attempt_errors") or [],
            })

    # 1. 立即回写初步表 `deal_raw_tips`
    if raw_status_ops:
        try:
            raw_col.bulk_write(raw_status_ops, ordered=False)
        except Exception as e:
            logger.error("回写初步表 `deal_raw_tips` 清洗状态异常: %s", e)

    # 2. 立即晋升写入正式表 `products`，并实时更新内存 `shared_active_products`
    formal_write_res = {"upserted": 0, "modified": 0, "matched": 0, "protected_high_score": 0, "total_ops": 0}
    if batch_success_product_docs:
        try:
            formal_write_res = upsert_cleaned_products_to_formal_table(formal_col, batch_success_product_docs)
        except Exception as e:
            logger.exception("写入正式表 `products` 发生异常: %s", e)

        if shared_active_products is not None:
            def _update_mem_active() -> None:
                for pdoc in batch_success_product_docs:
                    plat = str(pdoc.get("platform") or "").strip()
                    pid = str(pdoc.get("product_id") or "").strip()
                    if plat and pid:
                        di = pdoc.get("deal_info") if isinstance(pdoc.get("deal_info"), dict) else {}
                        new_q = int(di.get("quality_score") or 75)
                        prev_entry = shared_active_products.get((plat, pid))
                        new_dp = float(di["deal_price"]) if isinstance(di.get("deal_price"), (int, float)) and not isinstance(di.get("deal_price"), bool) and float(di["deal_price"]) > 0 else None
                        ex_dp = float(prev_entry["deal_price"]) if prev_entry and isinstance(prev_entry.get("deal_price"), (int, float)) and not isinstance(prev_entry.get("deal_price"), bool) and float(prev_entry["deal_price"]) > 0 else None
                        improved = bool(new_dp is not None and ex_dp is not None and new_dp < ex_dp * 0.98)
                        if prev_entry is None or new_q >= int(prev_entry.get("quality_score") or 0) or improved:
                            shared_active_products[(plat, pid)] = {
                                "platform": plat,
                                "product_id": pid,
                                "goods_sign": str(pdoc.get("goods_sign") or ""),
                                "official_title": str(pdoc.get("name") or ""),
                                "official_price": pdoc.get("price"),
                                "deal_price": di.get("deal_price"),
                                "quality_score": new_q,
                                "tutorial_steps_count": len(di.get("tutorial_steps") or []) if isinstance(di.get("tutorial_steps"), list) else (1 if di.get("tutorial_steps") else 0),
                                "tutorial_score": int(di.get("tutorial_score") or di.get("quality_score") or new_q),
                            }

            if shared_lock is not None:
                with shared_lock:
                    _update_mem_active()
            else:
                _update_mem_active()

    # 3. 立即对提前终止 (early_terminated) 记录在正式表 `products` 中原子累加跨站/跨群共振热度
    early_resonance_modified = 0
    if batch_early_terminated_results:
        grouped_res: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for er_item in batch_early_terminated_results:
            ru = er_item.get("resonance_update") or {}
            plat = str(ru.get("platform") or er_item.get("platform") or "").strip()
            pid = str(ru.get("product_id") or er_item.get("product_id") or "").strip()
            if not plat or not pid:
                continue
            bucket = grouped_res.setdefault(
                (plat, pid),
                {"seen_inc": 0, "hit_inc": 0, "sites": set(), "channels": set(), "copies": [], "timeline": []},
            )
            bucket["seen_inc"] += max(1, int(ru.get("seen_count") or 1))
            bucket["hit_inc"] += 1
            s_site = str(ru.get("source_site") or "").strip()
            if s_site:
                bucket["sites"].add(s_site)
            for ch in ru.get("seen_channels") or []:
                ch_s = str(ch or "").strip()
                if ch_s:
                    bucket["channels"].add(ch_s)
            c_txt = str(ru.get("copywriting") or "").strip()
            if c_txt and len(c_txt) >= 15 and c_txt not in bucket["copies"] and len(bucket["copies"]) < 3:
                bucket["copies"].append(c_txt[:300])
            tl_entry = ru.get("timeline_entry")
            if isinstance(tl_entry, dict) and tl_entry.get("tip_id"):
                if not any(e.get("tip_id") == tl_entry["tip_id"] for e in bucket["timeline"]):
                    bucket["timeline"].append(tl_entry)

        early_ops = []
        for (plat, pid), b in grouped_res.items():
            upd_doc: Dict[str, Any] = {
                "$inc": {
                    "deal_info.total_seen_count": b["seen_inc"],
                    "deal_info.tip_hit_count": b["hit_inc"],
                }
            }
            add_set: Dict[str, Any] = {}
            if b["sites"]:
                add_set["deal_info.resonance_sites"] = {"$each": sorted(b["sites"])}
            if b["channels"]:
                add_set["deal_info.resonance_channels"] = {"$each": sorted(b["channels"])}
            if b.get("copies"):
                add_set["deal_info.supplementary_copies"] = {"$each": b["copies"]}
            if add_set:
                upd_doc["$addToSet"] = add_set
            if b.get("timeline"):
                upd_doc["$push"] = {
                    "deal_info.tips_timeline": {
                        "$each": b["timeline"],
                        "$sort": {"publish_ts": -1},
                        "$slice": 20,
                    }
                }
            early_ops.append(UpdateOne({"platform": plat, "product_id": pid}, upd_doc, upsert=False))
        if early_ops:
            try:
                er_res = formal_col.bulk_write(early_ops, ordered=False)
                early_resonance_modified = er_res.modified_count
            except Exception as e:
                logger.warning("累加提前终止商品共振热度异常: %s", e)

    return {
        "formal_write_res": formal_write_res,
        "early_resonance_modified": early_resonance_modified,
        "success_product_docs": batch_success_product_docs,
        "early_terminated_results": batch_early_terminated_results,
        "failed_samples": batch_failed_samples,
        "retry_pending_count": retry_pending_count,
    }



class StageWorker(threading.Thread):
    """阶段工作线程：各阶段独立认领符合自身状态的记录并即时推进至下一阶段。"""
    def __init__(
        self,
        name: str,
        stage: str,
        repo: DealRawPoolRepository,
        cleaner: DealPoolCleaner,
        batch_size: int,
        lease_seconds: int = 300,
        poll_interval: float = DEFAULT_STAGE_POLL_INTERVAL_SECONDS,
        stop_event: Optional[threading.Event] = None,
        ai_semaphore: Optional[threading.BoundedSemaphore] = None,
        upstream_event: Optional[threading.Event] = None,
        downstream_event: Optional[threading.Event] = None,
        shared_active_products: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
        shared_lock: Optional[threading.Lock] = None,
        quiet_sub_logs: bool = False,
    ):
        super().__init__(name=name, daemon=True)
        self.stage = stage
        self.repo = repo
        self.cleaner = cleaner
        self.batch_size = max(1, batch_size)
        self.lease_seconds = max(10, lease_seconds)
        self.poll_interval = max(0.5, float(poll_interval))
        self.stop_event = stop_event or threading.Event()
        self.ai_semaphore = ai_semaphore
        self.upstream_event = upstream_event
        self.downstream_event = downstream_event
        self.shared_active_products = shared_active_products
        self.shared_lock = shared_lock
        self.quiet_sub_logs = quiet_sub_logs
        self.worker_id = f"{name}_{os.getpid()}_{threading.get_ident()}"
        self.total_processed = 0
        self.total_advanced = 0

    def _compute_safe_lease(self, limit: int) -> int:
        """根据认领批量动态自适应计算安全租约，确保大批次处理期间不被维护线程误回收。"""
        if limit <= 0:
            return self.lease_seconds
        if self.stage in (CLEAN_STAGE_NEED_AI1, CLEAN_STAGE_NEED_AI2):
            ai_chunk_size = max(1, getattr(self.cleaner, "ai_batch_size", 4))
            if self.stage == CLEAN_STAGE_NEED_AI2:
                ai_chunk_size = max(1, getattr(self.cleaner, "ai2_batch_size", 6))
            ai_workers = max(1, getattr(self.cleaner, "ai_max_workers", 8))
            ai_timeout = float(getattr(self.cleaner, "ai_timeout", 90.0))
            rounds = (limit + (ai_chunk_size * ai_workers) - 1) // (ai_chunk_size * ai_workers)
            est_sec = int((rounds + 2) * ai_timeout * 1.5)
            return max(self.lease_seconds, est_sec)
        elif self.stage == CLEAN_STAGE_NEED_CONVERT:
            conv_workers = max(1, getattr(self.cleaner, "convert_max_workers", 10))
            est_sec = int((limit / conv_workers) * 2.0) + 120
            return max(self.lease_seconds, est_sec)
        return self.lease_seconds

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                safe_lease = self._compute_safe_lease(self.batch_size)
                claimed = self.repo.claim_stage_docs(
                    self.stage,
                    limit=self.batch_size,
                    lease_seconds=safe_lease,
                    worker_id=self.worker_id,
                )
                if not claimed:
                    if self.upstream_event is not None:
                        self.upstream_event.wait(timeout=self.poll_interval)
                        self.upstream_event.clear()
                    else:
                        time.sleep(self.poll_interval)
                    continue

                if self.stage == CLEAN_STAGE_NEED_AI1:
                    if self.ai_semaphore is not None:
                        with self.ai_semaphore:
                            outcomes, elapsed, _ = self.cleaner.process_ai1_unit(
                                claimed,
                                self.repo.col,
                                formal_col=self.repo.formal_col,
                                total_pending_count=len(claimed),
                                shared_active_products=self.shared_active_products,
                                shared_lock=self.shared_lock,
                                quiet_sub_logs=self.quiet_sub_logs,
                            )
                    else:
                        outcomes, elapsed, _ = self.cleaner.process_ai1_unit(
                            claimed,
                            self.repo.col,
                            formal_col=self.repo.formal_col,
                            total_pending_count=len(claimed),
                            shared_active_products=self.shared_active_products,
                            shared_lock=self.shared_lock,
                            quiet_sub_logs=self.quiet_sub_logs,
                        )
                elif self.stage == CLEAN_STAGE_NEED_CONVERT:
                    outcomes, elapsed = self.cleaner.process_convert_unit(
                        claimed,
                        formal_col=self.repo.formal_col,
                        total_pending_count=len(claimed),
                        shared_active_products=self.shared_active_products,
                        shared_lock=self.shared_lock,
                        quiet_sub_logs=self.quiet_sub_logs,
                    )
                elif self.stage == CLEAN_STAGE_NEED_AI2:
                    if self.ai_semaphore is not None:
                        with self.ai_semaphore:
                            outcomes, elapsed = self.cleaner.process_ai2_unit(
                                claimed,
                                raw_col=self.repo.col,
                                formal_col=self.repo.formal_col,
                                total_pending_count=len(claimed),
                                quiet_sub_logs=self.quiet_sub_logs,
                            )
                    else:
                        outcomes, elapsed = self.cleaner.process_ai2_unit(
                            claimed,
                            raw_col=self.repo.col,
                            formal_col=self.repo.formal_col,
                            total_pending_count=len(claimed),
                            quiet_sub_logs=self.quiet_sub_logs,
                        )
                else:
                    logger.warning("[%s] 未知阶段标记: %s", self.name, self.stage)
                    time.sleep(self.poll_interval)
                    continue

                if outcomes:
                    _commit_batch_results_to_db(
                        outcomes,
                        self.repo.col,
                        self.repo.formal_col,
                        cycle_num=0,
                        shared_active_products=self.shared_active_products,
                        shared_lock=self.shared_lock,
                    )
                    self.total_processed += len(outcomes)
                    adv_count = sum(1 for o in outcomes if o.get("stage_outcome") in ("advance_to_convert", "advance_to_ai2", "success"))
                    self.total_advanced += adv_count
                    if self.downstream_event is not None and adv_count > 0:
                        self.downstream_event.set()

            except Exception as e:
                logger.exception("🚨 [%s] 阶段工作线程处理异常，稍后继续: %s", self.name, e)
                time.sleep(self.poll_interval)


class MaintenanceWorker(threading.Thread):
    """后台清扫维护线程：租约防死锁清扫、陈旧数据出队、重试上限归档与好价保鲜。"""
    def __init__(
        self,
        repo: DealRawPoolRepository,
        poll_interval: float = 30.0,
        stop_event: Optional[threading.Event] = None,
    ):
        super().__init__(name="DealMaintenanceWorker", daemon=True)
        self.repo = repo
        self.poll_interval = max(5.0, poll_interval)
        self.stop_event = stop_event or threading.Event()

    def run(self) -> None:
        tick = 0
        while not self.stop_event.is_set():
            tick += 1
            try:
                # 1. 租约清扫：释放超时未完成的租约回队
                reclaimed = self.repo.reclaim_expired_leases()
                if reclaimed > 0:
                    logger.info("🧹 [维护清扫] 成功释放超时未完成租约 %d 条回队", reclaimed)

                # 2. 字段自包含与渠道级保鲜清扫：优先依据数据库自带的 clean_cutoff_ts 一键淘汰
                now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                now_ts = time.time()
                self.repo.col.update_many(
                    {
                        "clean_status": "pending",
                        "clean_cutoff_ts": {"$gt": 0, "$lt": now_ts},
                    },
                    {
                        "$set": {
                            "clean_status": "cleaned_failed",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                            "clean_error_msg": "已超过本爆料专属保鲜截止期，自动清扫",
                            "cleaned_at": now_utc,
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )
                site_age_map = get_site_max_age_days_map()
                for s_key, age_days in site_age_map.items():
                    s_ts = time.time() - age_days * 86400.0
                    s_dt = now_utc - datetime.timedelta(days=age_days)
                    self.repo.col.update_many(
                        {
                            "clean_status": "pending",
                            "clean_cutoff_ts": {"$exists": False},
                            "source_site": s_key,
                            "$or": [
                                {"publish_ts": {"$gt": 0, "$lt": s_ts}},
                                {"first_seen_at": {"$lt": s_dt}},
                            ],
                        },
                        {
                            "$set": {
                                "clean_status": "cleaned_failed",
                                "clean_stage": CLEAN_STAGE_DONE,
                                "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                                "clean_error_msg": f"超过渠道 {s_key} 保鲜期 ({age_days:.0f}天)，自动清扫",
                                "cleaned_at": now_utc,
                            },
                            "$unset": {
                                "stage_lease_owner": "",
                                "stage_lease_until_ts": "",
                            },
                        },
                    )
                configured_sites = list(site_age_map.keys())
                fallback_ts = time.time() - DEAL_MAX_CLEAN_AGE_DAYS * 86400.0
                fallback_dt = now_utc - datetime.timedelta(days=DEAL_MAX_CLEAN_AGE_DAYS)
                self.repo.col.update_many(
                    {
                        "clean_status": "pending",
                        "clean_cutoff_ts": {"$exists": False},
                        "source_site": {"$nin": configured_sites},
                        "$or": [
                            {"publish_ts": {"$gt": 0, "$lt": fallback_ts}},
                            {"first_seen_at": {"$lt": fallback_dt}},
                        ],
                    },
                    {
                        "$set": {
                            "clean_status": "cleaned_failed",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                            "clean_error_msg": f"超过默认保鲜期 ({DEAL_MAX_CLEAN_AGE_DAYS}天)，自动清扫",
                            "cleaned_at": now_utc,
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )

                # 3. 超限重试清扫：重试达到上限的标记为 cleaned_failed
                self.repo.col.update_many(
                    {
                        "clean_status": "pending",
                        "clean_retry_count": {"$gte": MAX_CLEAN_RETRIES},
                    },
                    {
                        "$set": {
                            "clean_status": "cleaned_failed",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": ERR_AI_MAX_RETRIES_EXCEEDED,
                            "clean_error_msg": f"已达最大重试上限({MAX_CLEAN_RETRIES}次)，自动放弃",
                            "cleaned_at": now_utc,
                        },
                        "$unset": {
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )

                # 4. 每 30 轮（约 15 分钟）执行一次正式库好价保鲜摘标
                if tick % 30 == 0:
                    exp_cnt = self.repo.expire_stale_formal_deals(now_utc=now_utc)
                    if exp_cnt > 0:
                        logger.info("🧹 [正式库好价保鲜] 自动摘除超 %.0f 小时未刷新好价标 %d 个", DEAL_FORMAL_EXPIRY_HOURS, exp_cnt)

                # 5. 更新状态快照与心跳看板
                summary = self.repo.generate_pool_summary()
                with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)

                if tick % 10 == 0:
                    p_stages = summary.get("pending_by_stage") or {}
                    logger.info(
                        "📈 [全库阶段心跳看板] 总线报=%d条 | 成功清洗=%d条, 失败淘汰=%d条 | ⏳ 待清洗总Pending=%d条 (AI-1待拆=%d, 待转链=%d, 待终审=%d) | 📦 正式库好价=%d件",
                        summary.get("total_records", 0),
                        summary.get("cleaned_success_records", 0),
                        summary.get("cleaned_failed_records", 0),
                        summary.get("pending_clean_records", 0),
                        p_stages.get("need_ai1", 0),
                        p_stages.get("need_convert", 0),
                        p_stages.get("need_ai2", 0),
                        summary.get("formal_deal_products_count", 0),
                    )

            except Exception as e:
                logger.warning("维护线程执行异常: %s", e)

            self.stop_event.wait(timeout=self.poll_interval)


