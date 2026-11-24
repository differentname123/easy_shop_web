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

_OFFICIAL_CPS_HOSTS = frozenset({
    "u.jd.com",
    "union-click.jd.com",
    "jingfen.jd.com",
    "pro.m.jd.com",
    "prodev.m.jd.com",
    "item.jd.com",
    "item.m.jd.com",
    "item.yiyaojd.com",
    "npcitem.jd.hk",
    "mitem.jd.hk",
    "uland.taobao.com",
    "s.click.taobao.com",
    "s.click.tmall.com",
    "m.tb.cn",
    "s.tb.cn",
    "e.tb.cn",
    "pages-fast.m.taobao.com",
    "mo.m.taobao.com",
    "pages.tmall.com",
    "item.taobao.com",
    "detail.tmall.com",
    "chaoshi.detail.tmall.com",
    "detail.tmall.hk",
    "h5.m.taobao.com",
    "p.pinduoduo.com",
    "mobile.yangkeduo.com",
    "mobile.pinduoduo.com",
})

_PLATFORM_HOMEPAGE_HOSTS = frozenset({
    "www.jd.com",
    "jd.com",
    "m.jd.com",
    "www.taobao.com",
    "m.taobao.com",
    "www.tmall.com",
    "www.pinduoduo.com",
})

_JD_NUMERIC_PATH_RE = re.compile(r"^/(?:product/)?\d+\.html$", re.IGNORECASE)

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

    if _allow_login_unwrap and host in ("plogin.m.jd.com", "passport.jd.com"):
        for k, v in parse_qsl(parts.query, keep_blank_values=False):
            if k.strip().lower() == "returnurl" and v.strip():
                rebuilt = rebuild_clean_official_venue_url(unquote(v.strip()), _allow_login_unwrap=False)
                if rebuilt:
                    return rebuilt
        # 部分线报源未对内嵌 returnurl 做 URL 转义，直接保留后缀整串做白名单二次重组
        raw_ret_m = re.search(r"(?:^|&)returnurl=(https?://.+)$", parts.query, re.IGNORECASE)
        if raw_ret_m:
            return rebuild_clean_official_venue_url(unquote(raw_ret_m.group(1).strip()), _allow_login_unwrap=False)
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

    if (host == "coupon.m.jd.com" and path == "/coupons/show.action") or (
        host == "coupon.jd.com" and ("coupon" in path.lower() or path == "/coupons/show.action")
    ):
        pairs, found = _pick_allowed_params({"key": "key", "roleid": "roleId", "linkkey": "linkKey"})
        if not (found.get("linkKey") or (found.get("key") and found.get("roleId"))):
            return None
        return urlunsplit(("https", "coupon.m.jd.com", "/coupons/show.action", urlencode(pairs), ""))

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
        # 通天塔单品模板携带非纯数字加密 sku/mainSku/skuId，绝不能剥参数冒充纯会场
        for qk, qv in parse_qsl(parts.query, keep_blank_values=False):
            if qk.strip().lower() in ("sku", "mainsku", "skuid") and qv.strip() and not qv.strip().isdigit():
                return None
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


def is_official_cps_http_url(url_str: str) -> bool:
    """判断 http(s) 链接是否为三平台官方联盟转链/直查入口域（不走免费前置 HTTP 解包）。"""
    try:
        host = (urlsplit(str(url_str or "").strip()).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return False
    return bool(host and host in _OFFICIAL_CPS_HOSTS)


def _unwrap_jd_login_returnurl(url_str: str) -> str:
    """将 plogin.m.jd.com / passport.jd.com 登录跳转包装里的 returnurl 还原为内层官方链接。"""
    s = str(url_str or "").strip()
    try:
        parts = urlsplit(s)
    except Exception:
        return s
    if (parts.hostname or "").strip().lower() not in ("plogin.m.jd.com", "passport.jd.com"):
        return s
    for k, v in parse_qsl(parts.query or "", keep_blank_values=False):
        if k.strip().lower() == "returnurl":
            inner = unquote(v.strip()).strip()
            if inner.lower().startswith(("http://", "https://")):
                return inner
    return s


def _is_terminal_numeric_item_url(url_str: str) -> bool:
    """识别纯数字直跳单品页（不含可转佣 goods_sign/短链），避免后续无意义转链调用。"""
    try:
        parts = urlsplit(str(url_str or "").strip())
    except Exception:
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    path = parts.path or ""
    qs = parse_qs(parts.query or "")
    if host in ("item.jd.com", "item.m.jd.com", "item.yiyaojd.com", "npcitem.jd.hk", "mitem.jd.hk"):
        return bool(_JD_NUMERIC_PATH_RE.match(path))
    if host in ("item.taobao.com", "detail.tmall.com", "chaoshi.detail.tmall.com", "detail.tmall.hk", "h5.m.taobao.com"):
        return any(v.strip().isdigit() for v in qs.get("id", []))
    if host in ("mobile.yangkeduo.com", "p.pinduoduo.com", "mobile.pinduoduo.com"):
        has_goods_id = any(v.strip().isdigit() for v in qs.get("goods_id", []))
        has_goods_sign = any(v.strip() for v in qs.get("goods_sign", []))
        return has_goods_id and not has_goods_sign
    return False


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


def _walk_http_redirects(
    start_url: str,
    *,
    max_hops: int = 4,
    timeout: float = 3.5,
    http_session: Any = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    on_hop: Optional[Callable[[str], Optional[Tuple[str, str, str]]]] = None,
) -> Tuple[str, str, str, Optional[str]]:
    """共享 SSRF 安全 HTTP 重定向追踪器，供会场解包与非联盟入口统一前置预检共用。"""
    cur = str(start_url or "").strip()
    own_session = http_session is None
    sess = requests.Session() if own_session else http_session
    req_timeout = (min(2.5, float(timeout)), float(timeout))
    prev_url: Optional[str] = None
    try:
        for _ in range(max(1, int(max_hops))):
            if not _is_safe_public_http_url(cur, resolver=resolver):
                return ("unsafe", cur, "ssrf_blocked", None)
            headers = {"User-Agent": _UNWRAP_MOBILE_UA}
            if prev_url:
                headers["Referer"] = prev_url
            resp = None
            try:
                resp = sess.get(cur, allow_redirects=False, timeout=req_timeout, stream=True, headers=headers)
                status_code = int(getattr(resp, "status_code", 200) or 200)
                if status_code >= 400:
                    return ("http_error", cur, f"http_{status_code}", None)
                nxt = ""
                if status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
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
            except Exception as exc:
                return ("transient", cur, f"network_error:{type(exc).__name__}", None)
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

            if on_hop is not None:
                hit = on_hop(cur)
                if hit is not None:
                    tag, resolved_val, reason = hit
                    return (tag, cur, reason, resolved_val)
    finally:
        if own_session and hasattr(sess, "close"):
            try:
                sess.close()
            except Exception:
                pass
    return ("end", cur, "max_hops_or_end", None)


def _classify_non_cps_hop(cand_url: str) -> Optional[Tuple[str, str, str]]:
    """判定一跳 URL 是否已落入五类终态之一（venue / numeric_item / cps_redirect / dead）；未确定返回 None。"""
    v = _precheck_direct_non_cps_venue_url(cand_url)
    if v:
        return ("venue", v, "unwrapped_venue")
    unwrapped = _unwrap_jd_login_returnurl(cand_url)
    if unwrapped != cand_url:
        v2 = _precheck_direct_non_cps_venue_url(unwrapped)
        if v2:
            return ("venue", v2, "unwrapped_venue")
    if _is_terminal_numeric_item_url(unwrapped):
        return ("numeric_item", unwrapped, "terminal_numeric_item")
    if is_official_cps_http_url(unwrapped):
        return ("cps_redirect", unwrapped, "cps_redirect")
    try:
        parts = urlsplit(unwrapped)
        host = (parts.hostname or "").strip().lower().rstrip(".")
        path = parts.path or "/"
        if host in _PLATFORM_HOMEPAGE_HOSTS and path in ("", "/"):
            return ("dead", unwrapped, "platform_homepage")
    except Exception:
        pass
    return None


def resolve_non_cps_http_url(
    raw_input: str,
    max_hops: int = 4,
    timeout: float = 3.5,
    http_session: Any = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> Dict[str, Any]:
    """
    统一前置安全 HTTP 预检器（处理 3.cn / llipin.cn / kurl07.cn / t.cn / 店铺页等所有非官方联盟入口）：
    返回 {"kind": "venue"|"cps_redirect"|"numeric_item"|"dead"|"transient", "url": str, "reason": str}。
    """
    cur = str(raw_input or "").strip()
    if not cur.lower().startswith(("http://", "https://")):
        return {"kind": "dead", "url": cur, "reason": "not_http"}

    direct_v = _precheck_direct_non_cps_venue_url(cur)
    if direct_v:
        return {"kind": "venue", "url": direct_v, "reason": "direct_venue"}
    init_cls = _classify_non_cps_hop(cur)
    if init_cls is not None:
        tag, resolved_val, reason = init_cls
        return {"kind": tag, "url": resolved_val, "reason": reason}

    final_u = cur
    for attempt in range(2):
        tag, final_u, reason, val = _walk_http_redirects(
            cur,
            max_hops=max_hops,
            timeout=timeout,
            http_session=http_session,
            resolver=resolver,
            on_hop=_classify_non_cps_hop,
        )
        if tag in ("venue", "cps_redirect", "numeric_item", "dead"):
            return {"kind": tag, "url": val or final_u, "reason": reason}
        if tag == "transient":
            return {"kind": "transient", "url": final_u, "reason": reason}
        if tag in ("unsafe", "http_error"):
            return {"kind": "dead", "url": final_u, "reason": reason}
        term = _classify_non_cps_hop(final_u)
        if term is not None:
            return {"kind": term[0], "url": term[1], "reason": term[2]}
        # 第三方短链偶发插播随机广告跳转至陌生非电商域时，仅免费再复核 1 次
        if (
            attempt == 0
            and tag == "end"
            and not _is_official_ecommerce_http_url(cur)
            and final_u != cur
            and not _is_official_ecommerce_http_url(final_u)
        ):
            continue
        break
    return {"kind": "dead", "url": final_u, "reason": "non_ecommerce_or_expired"}


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

    def _on_venue_hop(cand_u: str) -> Optional[Tuple[str, str, str]]:
        rebuilt = rebuild_clean_official_venue_url(cand_u)
        return ("venue", rebuilt, "unwrapped_venue") if rebuilt else None

    tag, final_u, _reason, val = _walk_http_redirects(
        cur,
        max_hops=max_hops,
        timeout=timeout,
        http_session=http_session,
        resolver=resolver,
        on_hop=_on_venue_hop,
    )
    if tag == "venue" and val:
        return val
    if tag == "end" and final_u != cur:
        return rebuild_clean_official_venue_url(final_u)
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
        if host == "coupon.m.jd.com" or "duo_coupon_landing.html" in path.lower():
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
