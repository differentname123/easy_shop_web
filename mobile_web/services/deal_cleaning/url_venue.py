# -*- coding: utf-8 -*-
"""线报链接平台识别、官方无佣金公共会场白名单重组、通道 1 伪转链门禁与通道 2 SSRF 安全解包。"""
from __future__ import annotations

import html as html_lib
import ipaddress
import re
import socket
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests

from models.deal_constants import _UNSUPPORTED_EXTERNAL_DOMAINS
from models.product import detect_platform_signal

ROLE_HINT_NORMALIZE_MAP: Dict[str, str] = {
    "main_candidate": "main_candidate",
    "main_product": "main_candidate",
    "main": "main_candidate",
    "coupon_candidate": "coupon_candidate",
    "coupon": "coupon_candidate",
    "filler_candidate": "filler_candidate",
    "filler_product": "filler_candidate",
    "filler": "filler_candidate",
}

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

_UNWRAP_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

_TUN_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


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


def rebuild_clean_official_venue_url(raw_input: str, _allow_login_unwrap: bool = True) -> Optional[str]:
    """
    官方原生公共领券/会场/店铺页路径+参数白名单重组（其余外部追踪参数与分佣参数一律剥离）。
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
    _ = role_hint
    return rebuild_clean_official_venue_url(raw_input) is not None


def _precheck_direct_non_cps_venue_url(raw_input: str) -> Optional[str]:
    """纯无佣金公共领券/活动页前置直出（排除可转佣的 pro*.m.jd.com 会场）。"""
    rebuilt = rebuild_clean_official_venue_url(raw_input)
    if not rebuilt:
        return None
    if (urlsplit(rebuilt).hostname or "").strip().lower() in ("pro.m.jd.com", "prodev.m.jd.com"):
        return None
    return rebuilt


def _is_verified_station_promo_link(clean_input: str, promo_link: str) -> bool:
    """通道 1 门禁：强校验转链产出必须属于官方联盟推广域，且严禁原样回显输入。"""
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


def _is_official_ecommerce_http_url(url_str: str) -> bool:
    """判断 http(s) 链接域名是否归属淘/京/拼官方域名体系。"""
    try:
        host = (urlsplit(str(url_str or "").strip()).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    return any(host == suf or host.endswith("." + suf) for suf in _OFFICIAL_ECOMMERCE_HOST_SUFFIXES)


def _is_safe_public_http_url(url_str: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> bool:
    """SSRF 安全校验：每跳强制校验 http/https 标准端口与公网 IP。"""
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
            ips = [
                ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
                for info in (infos or [])
                if info and len(info) >= 5 and info[4]
            ]
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
    """通道 2 通用安全解包：跟随最多 max_hops 跳并在命中官方公共领券/会场白名单时重组返回纯净 URL。"""
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


def is_shop_homepage_url(raw_input: Any) -> bool:
    """判断 URL 是否为京东/淘宝店铺首页（绝不当作独立优惠券按钮或引发 Fail-Closed 拦截）。"""
    s = str(raw_input or "").strip().lower()
    if not s.startswith(("http://", "https://")):
        return False
    try:
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        path = parts.path or ""
    except Exception:
        return False
    return bool(
        (host == "shop.m.jd.com" and path == "/mshop/gethomepage")
        or (host == "store.taobao.com" and path == "/shop/view_shop.htm")
    )


def is_explicit_coupon_target(c: Dict[str, Any]) -> bool:
    """判定目标是否为原始线报中的优惠券/活动候选链接。"""
    if not isinstance(c, dict):
        return False
    clean_input = str(c.get("clean_input") or "").strip().lower()
    promo_link = str(c.get("my_promo_link") or "").strip().lower()
    if is_shop_homepage_url(clean_input) or is_shop_homepage_url(promo_link):
        return False
    host, path = "", ""
    if clean_input.startswith(("http://", "https://")):
        try:
            parts = urlsplit(clean_input)
            host = (parts.hostname or "").lower()
            path = parts.path or ""
        except Exception:
            host, path = "", ""
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
