# -*- coding: utf-8 -*-
"""
好价线报上游站点拉取层（频道专有处理 + 统一输出契约）。
================================================================================
设计原则（第一性原理）：
1. 上游各自处理频道/站点专有逻辑（分页水位线、详情抓取、跳板 URL 解包、风控熔断）；
2. 统一确定性过滤：仅当【100% 确认】商品属于非拼多多/京东/淘宝平台时才过滤（宁可放行存疑项，绝不误杀）；
3. 统一完整性门禁：获取不到完整详情或缺少实质教程/购买线索的“空壳单品”坚决不入库，避免浪费下游 AI 与转链算力；
4. 向上游调度层与下游仓储层输出完全一致的标准化 `DealRawRecord` 与 `FetchResult` 结构。
================================================================================
"""
import html as html_lib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests

from models.deal_constants import _UNSUPPORTED_EXTERNAL_DOMAINS
from models.product import detect_platform_signal, extract_jd_image_key, normalize_platform

SUPPORTED_PLATFORMS: Tuple[str, ...] = ("jd", "taobao", "pdd")

logger = logging.getLogger(__name__)

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_RATE_LIMIT_STATUSES: Tuple[int, ...] = (403, 429, 502, 503, 504)

_EXPLICIT_YUAN_RE = re.compile(
    r"(?:(?:￥|¥|💰)\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:元|r\b|R\b|块))",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    r"(?:[￥¥$€₴£¢()（）!！](?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{6,22}[￥¥$€₴£¢()（）!！]"
    r"|(?<![A-Za-z0-9:/._-])/(?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{8,16}/(?![A-Za-z0-9_.-])"
    r"|\b[A-Za-z0-9_-]{24,28}-[A-Za-z0-9_]{16,22}\b"
    r"|\b[A-Za-z0-9]{25,35}_[A-Za-z0-9]{15,22}\b)"
)
_RAW_URL_RE = re.compile(r"https?://[^\s<>\"'\]）】]+", re.IGNORECASE)

_SUPPORTED_MALL_KEYWORDS: Tuple[str, ...] = (
    "京东",
    "京喜",
    "淘宝",
    "天猫",
    "聚划算",
    "淘特",
    "拼多多",
)

# 仅包含 100% 确定不属于淘宝/京东/拼多多的独立平台或线下餐饮渠道（防止误杀品牌旗舰店）
_CONFIRMED_UNSUPPORTED_MALLS: frozenset = frozenset({
    "唯品会",
    "美团",
    "美团外卖",
    "饿了么",
    "抖音",
    "抖音电商",
    "快手",
    "小红书",
    "亚马逊",
    "亚马逊中国",
    "海外购",
    "考拉海购",
    "网易严选",
    "苏宁",
    "苏宁易购",
    "小米有品",
    "当当",
    "当当网",
    "1688",
    "阿里巴巴",
    "得物",
    "毒",
    "闲鱼",
    "转转",
    "叮咚买菜",
    "朴朴",
    "盒马",
    "山姆",
    "沃尔玛",
    "永辉",
    "肯德基",
    "麦当劳",
    "星巴克",
    "瑞幸",
    "霸王茶姬",
    "奈雪",
    "蜜雪冰城",
    "喜茶",
    "汉堡王",
    "必胜客",
    "滴滴",
    "携程",
    "飞猪",
    "去哪儿",
    "同程",
    "百度网盘",
    "阿里云盘",
    "夸克网盘",
    "苹果官网",
    "apple官网",
    "华为商城",
    "小米商城",
})

_SOURCE_SELF_HOSTS: frozenset = frozenset({
    "guangdiu.com",
    "iguangdiu.com",
    "xhhcds.cn",
    "new.xianbao.fun",
    "youhui.bacaoo.com",
    "0818tuan.com",
    "iehou.com",
})

_CONFIRMED_UNSUPPORTED_HOSTS: Tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            d
            for d in _UNSUPPORTED_EXTERNAL_DOMAINS
            if "/" not in d and d not in _SOURCE_SELF_HOSTS
        ]
        + [
            "amazon.cn",
            "amazon.com",
            "suning.com",
            "kaola.com",
            "you.163.com",
            "xiaomiyoupin.com",
            "dangdang.com",
            "1688.com",
            "dewu.com",
            "poizon.com",
            "ele.me",
            "ctrip.com",
            "fliggy.com",
            "qunar.com",
            "apple.com",
            "vmall.com",
            "mi.com",
            "alipan.com",
            "iesdouyin.com",
            "mcdonalds.com.cn",
            "kfc.com.cn",
            "starbucks.com.cn",
        ]
    )
)

_IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_explicit_platform_code(raw_code: Any) -> str:
    s = str(raw_code or "").strip().lower()
    if not s or s in ("unknown", "other", "all", "none"):
        return ""
    if s in ("tb", "tmall", "taobao"):
        return "taobao"
    if s in ("jd", "jingdong"):
        return "jd"
    if s in ("pdd", "pinduoduo", "yangkeduo"):
        return "pdd"
    return s


def _unwrap_known_redirect_url(url_str: str) -> str:
    """提取常见内部跳转链（如 guangdiu to.php?u=...）中的真实目标 URL。"""
    s = html_lib.unescape(str(url_str or "")).strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        if "guangdiu.com" in host and parts.path.endswith("to.php"):
            qs = parse_qs(parts.query)
            if qs.get("u"):
                inner = unquote(str(qs["u"][0] or "")).strip()
                if inner.startswith(("http://", "https://")):
                    return inner
    except Exception:
        pass
    return s


def _is_host_match(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_confirmed_unsupported_deal(
    *,
    platform_code: str = "",
    mall_name: str = "",
    links: Optional[List[Dict[str, Any]]] = None,
    tokens: Optional[List[str]] = None,
) -> bool:
    """
    【100% 确定非淘/京/拼平台过滤器 · 防误杀铁律】：
    仅当平台信号【百分百确定】属于拼多多/京东/淘宝之外的平台时返回 True；
    凡存在任何淘京拼正面信号，或平台归属尚不明确（如未知品牌名、未知第三方短链），一律返回 False 放行。
    """
    norm_code = _normalize_explicit_platform_code(platform_code)
    if norm_code and norm_code not in SUPPORTED_PLATFORMS:
        return True

    clean_mall = re.sub(r"\s+", "", str(mall_name or "")).strip()
    if clean_mall:
        if any(kw in clean_mall for kw in _SUPPORTED_MALL_KEYWORDS):
            return False
        mall_low = clean_mall.lower()
        if mall_low in _CONFIRMED_UNSUPPORTED_MALLS or clean_mall in _CONFIRMED_UNSUPPORTED_MALLS:
            return True
        # 兼容如 "唯品会 · 特卖" 形式
        first_seg = re.split(r"[·•\-|/]+", clean_mall)[0].strip()
        if first_seg and (first_seg in _CONFIRMED_UNSUPPORTED_MALLS or first_seg.lower() in _CONFIRMED_UNSUPPORTED_MALLS):
            return True

    if tokens and any(str(t or "").strip() for t in tokens):
        for tk in tokens:
            if detect_platform_signal(tk) in SUPPORTED_PLATFORMS:
                return False

    dest_urls: List[str] = []
    for lk in links or []:
        if not isinstance(lk, dict):
            continue
        raw_u = str(lk.get("url") or "").strip()
        if not raw_u.startswith(("http://", "https://")):
            continue
        u = _unwrap_known_redirect_url(raw_u)
        try:
            parts = urlsplit(u)
            host = (parts.hostname or "").strip().lower()
            path_low = (parts.path or "").lower()
        except Exception:
            continue
        if not host or path_low.endswith(_IMAGE_EXTENSIONS):
            continue
        if any(_is_host_match(host, self_h) for self_h in _SOURCE_SELF_HOSTS):
            continue
        if detect_platform_signal(u) in SUPPORTED_PLATFORMS:
            return False
        dest_urls.append(host)

    if norm_code in SUPPORTED_PLATFORMS:
        return False

    if dest_urls and all(
        any(_is_host_match(host, bad) for bad in _CONFIRMED_UNSUPPORTED_HOSTS)
        for host in dest_urls
    ):
        return True

    return False


def is_record_confirmed_unsupported(record: Dict[str, Any]) -> bool:
    """对标准化线报记录执行统一的非淘京拼平台确定性过滤检查。"""
    if not isinstance(record, dict):
        return True
    return is_confirmed_unsupported_deal(
        platform_code=str(record.get("platform") or ""),
        mall_name=str(record.get("mall_name") or ""),
        links=record.get("extracted_links") or [],
        tokens=record.get("extracted_tokens") or [],
    )


def has_valid_detail_and_tutorial(record: Dict[str, Any], min_text_chars: int = 4) -> bool:
    """
    【宁缺毋滥 · 拒绝空壳单品门禁】：
    验证线报记录既已拿到完整详情 (`has_full_detail=True`)，又包含实质性的爆料教程/方案文本与可操作购买锚点。
    """
    if not isinstance(record, dict) or not record.get("has_full_detail"):
        return False

    links = [lk for lk in (record.get("extracted_links") or []) if isinstance(lk, dict) and (lk.get("url") or lk.get("item_id"))]
    tokens = [str(tk).strip() for tk in (record.get("extracted_tokens") or []) if str(tk or "").strip()]
    cand_id = str(record.get("candidate_goods_id") or "").strip()
    if not (links or tokens or cand_id):
        return False

    tut = str(record.get("tutorial_text") or "").strip()
    if not tut or "[详情拉取异常" in tut:
        return False

    # 剥离图片占位、URL 与换行后检查是否还具备真实文案/方案
    stripped = re.sub(r"\[(?:群聊晒单/教程图片|教程图片):[^\]]*\]", " ", tut)
    stripped = re.sub(r"https?://\S+", " ", stripped)
    stripped = re.sub(r"\s+", "", stripped)
    if len(stripped) >= min_text_chars:
        return True

    hot = record.get("hot_metrics") if isinstance(record.get("hot_metrics"), dict) else {}
    return bool(hot.get("video_tutorial_url"))


def extract_first_price(*texts: Any) -> Tuple[Optional[float], str]:
    """依次从多个候选文本中提取数值参考价格与原始价格短语。"""
    for text in texts:
        raw = str(text or "").strip()
        if not raw:
            continue
        if re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
            try:
                return float(raw), f"{raw}元"
            except ValueError:
                pass
        m_bracket = re.search(r"【\s*(\d+(?:\.\d{1,2})?)\s*(?:元|r|R|块|包邮|起)?\s*】", raw)
        if m_bracket:
            try:
                return float(m_bracket.group(1)), m_bracket.group(0).strip()
            except ValueError:
                pass
        m = _EXPLICIT_YUAN_RE.search(raw)
        if m:
            num_str = m.group(1) or m.group(2)
            try:
                return float(num_str), m.group(0).strip()
            except ValueError:
                pass
    return None, ""


def extract_links_images_and_tokens(
    raw_html: str,
    base_url: str = "",
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """从 HTML 片段或文本中无损提取链接、图片 URL 与口令/加密 ID。"""
    html_str = str(raw_html or "")
    links: List[Dict[str, Any]] = []
    seen_urls: set = set()

    for m_a in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html_str, re.S | re.I):
        attrs, inner = m_a.group(1), m_a.group(2)
        anchor_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(inner))).strip()

        clip_m = re.search(r'data-clipboard-text=["\']([^"\']+)["\']', attrs, re.I)
        if clip_m:
            clip_val = html_lib.unescape(clip_m.group(1)).strip()
            if clip_val and clip_val not in seen_urls:
                seen_urls.add(clip_val)
                links.append({
                    "label": anchor_text or "剪贴板口令/活动词",
                    "url": clip_val,
                    "type": "clipboard",
                    "platform": detect_platform_signal(clip_val) or "unknown",
                })

        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not href_m:
            continue
        raw_href = html_lib.unescape(href_m.group(1)).strip()
        if not raw_href or raw_href.startswith(("javascript:", "#", "mailto:")):
            continue
        full_url = urljoin(base_url, raw_href) if base_url else raw_href
        if full_url not in seen_urls:
            seen_urls.add(full_url)
            links.append({
                "label": anchor_text or "链接",
                "url": full_url,
                "type": "anchor",
                "platform": detect_platform_signal(full_url) or detect_platform_signal(anchor_text) or "unknown",
            })

    stripped_tags = re.sub(r"<[^>]+>", " ", html_str)
    for m_url in _RAW_URL_RE.finditer(html_lib.unescape(stripped_tags)):
        u = m_url.group(0).rstrip(".,;，。；!！")
        if u not in seen_urls:
            seen_urls.add(u)
            links.append({
                "label": "正文裸链",
                "url": u,
                "type": "raw_url",
                "platform": detect_platform_signal(u) or "unknown",
            })

    images: List[str] = []
    for m_img in re.finditer(r"<img\b([^>]*)>", html_str, re.S | re.I):
        attrs = m_img.group(1)
        img_candidate = ""
        for attr_name in ("data-src", "data-original", "src"):
            m_attr = re.search(rf'{attr_name}=["\']([^"\']+)["\']', attrs, re.I)
            if m_attr and m_attr.group(1).strip() and not m_attr.group(1).strip().startswith("data:"):
                img_candidate = html_lib.unescape(m_attr.group(1).strip())
                break
        if img_candidate:
            if img_candidate.startswith("//"):
                img_candidate = "https:" + img_candidate
            elif base_url:
                img_candidate = urljoin(base_url, img_candidate)
            if img_candidate not in images:
                images.append(img_candidate)

    tokens: List[str] = []
    for m_tok in _TOKEN_RE.finditer(html_lib.unescape(html_str)):
        tok = m_tok.group(0).strip()
        if tok not in tokens:
            tokens.append(tok)

    return links, images, tokens


def html_to_tutorial_text(raw_html: str, base_url: str = "") -> str:
    """将包含购买/凑单教程的 HTML 转换为保留链接与换行结构的结构化文本。"""
    if not raw_html:
        return ""
    work = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", str(raw_html), flags=re.S | re.I)

    def _replace_anchor(match: re.Match) -> str:
        attrs, inner = match.group(1), match.group(2)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(inner))).strip()
        clip_m = re.search(r'data-clipboard-text=["\']([^"\']+)["\']', attrs, re.I)
        if clip_m:
            clip_val = html_lib.unescape(clip_m.group(1)).strip()
            if clip_val and clip_val not in text:
                return f"{text} [口令:{clip_val}]" if text else f"[口令:{clip_val}]"
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not href_m:
            return text
        raw_href = html_lib.unescape(href_m.group(1)).strip()
        if not raw_href or raw_href.startswith(("javascript:", "#", "mailto:")):
            return text
        full_url = urljoin(base_url, raw_href) if base_url else raw_href
        if not text:
            return full_url
        if full_url in text or raw_href in text:
            return text
        return f"{text} ({full_url})"

    def _replace_img(match: re.Match) -> str:
        attrs = match.group(1)
        for attr_name in ("data-src", "data-original", "src"):
            m_attr = re.search(rf'{attr_name}=["\']([^"\']+)["\']', attrs, re.I)
            if m_attr and m_attr.group(1).strip() and not m_attr.group(1).strip().startswith("data:"):
                u = html_lib.unescape(m_attr.group(1).strip())
                if u.startswith("//"):
                    u = "https:" + u
                elif base_url:
                    u = urljoin(base_url, u)
                return f"\n[教程图片: {u}]\n"
        return ""

    work = re.sub(r"<a\b([^>]*)>(.*?)</a>", _replace_anchor, work, flags=re.S | re.I)
    work = re.sub(r"<img\b([^>]*)>", _replace_img, work, flags=re.S | re.I)
    work = re.sub(r"<(?:br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", "\n", work, flags=re.I)
    work = re.sub(r"<[^>]+>", " ", work)
    work = html_lib.unescape(work)

    lines = []
    for line in work.splitlines():
        clean_line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if clean_line:
            lines.append(clean_line)
    return "\n".join(lines)


def _extract_div_by_id(html_text: str, div_id: str) -> str:
    """平衡匹配指定 id 的完整 <div>...</div> 子树，避免嵌套 <div> 导致提前截断。"""
    m_start = re.search(
        rf'<div\b[^>]*\bid=["\']{re.escape(div_id)}["\'][^>]*>',
        html_text,
        re.IGNORECASE,
    )
    if not m_start:
        return ""
    start_idx = m_start.start()
    depth = 0
    for m_tag in re.finditer(r"<(/?)div\b[^>]*>", html_text[start_idx:], re.IGNORECASE):
        if m_tag.group(1) == "/":
            depth -= 1
            if depth == 0:
                return html_text[start_idx : start_idx + m_tag.end()]
        else:
            depth += 1
    return html_text[start_idx:]


def build_deal_record(
    *,
    source_site: str,
    source_channel: str,
    source_item_id: str,
    source_url: str,
    title: str,
    price_text: str = "",
    price: Optional[float] = None,
    image_url: str = "",
    mall_name: str = "",
    publish_time: str = "",
    expire_time: Optional[str] = None,
    hot_metrics: Optional[Dict[str, Any]] = None,
    tutorial_text: str = "",
    tutorial_images: Optional[List[str]] = None,
    extracted_links: Optional[List[Dict[str, Any]]] = None,
    extracted_tokens: Optional[List[str]] = None,
    raw_payload: Any = None,
    explicit_platform: Optional[str] = None,
    source_list_page_url: str = "",
    source_detail_url: str = "",
    is_group_stream: bool = False,
) -> Dict[str, Any]:
    """构造向下游输出的统一标准化线报记录 (`DealRawRecord`)。"""
    clean_title = re.sub(r"\s+", " ", html_lib.unescape(str(title or ""))).strip()
    if price is None:
        price, inferred_price_text = extract_first_price(price_text, clean_title, tutorial_text)
        if not price_text and inferred_price_text:
            price_text = inferred_price_text

    links = extracted_links or []
    images = tutorial_images or []
    tokens = extracted_tokens or []
    primary_img = image_url or (images[0] if images else "")

    platform = normalize_platform(explicit_platform, default="") if explicit_platform else ""
    if not platform or platform == "unknown":
        probe_blob = " ".join([
            clean_title,
            mall_name,
            tutorial_text,
            " ".join(l.get("url", "") for l in links),
            " ".join(tokens),
        ])
        platform = detect_platform_signal(probe_blob) or "unknown"

    jd_image_key = extract_jd_image_key(primary_img)
    if not jd_image_key:
        for img in images:
            jd_image_key = extract_jd_image_key(img)
            if jd_image_key:
                break
    if jd_image_key and platform == "unknown":
        platform = "jd"

    primary_source_url = str(source_detail_url or source_url or source_list_page_url or "").strip()
    list_page_url = str(source_list_page_url or source_url or "").strip()
    detail_page_url = str(source_detail_url or source_url or list_page_url or "").strip()

    return {
        "source_site": source_site,
        "source_channel": source_channel,
        "source_item_id": str(source_item_id or "").strip(),
        "source_url": primary_source_url,
        "source_list_page_url": list_page_url,
        "source_detail_url": detail_page_url,
        "is_group_stream": bool(is_group_stream),
        "fetched_at": utc_now_iso(),
        "platform": platform,
        "candidate_goods_id": jd_image_key or "",
        "title": clean_title,
        "price_text": str(price_text or "").strip(),
        "price": price,
        "image_url": primary_img,
        "mall_name": str(mall_name or "").strip(),
        "publish_time": str(publish_time or "").strip(),
        "expire_time": str(expire_time).strip() if expire_time not in (None, "", "0", 0) else None,
        "hot_metrics": hot_metrics or {},
        "tutorial_text": tutorial_text,
        "tutorial_images": images,
        "extracted_links": links,
        "extracted_tokens": tokens,
        "raw_payload": raw_payload,
    }


def make_fetch_result(
    *,
    status: str,
    http_status: Optional[int],
    request_url: str,
    elapsed_ms: float,
    records: List[Dict[str, Any]],
    raw_list_count: int = 0,
    detail_fetched: int = 0,
    skipped_known: int = 0,
    skipped_unsupported: int = 0,
    skipped_no_detail: int = 0,
    rate_limited: bool = False,
    detail_errors: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """构造统一的渠道抓取返回结构，供调度管道统一消费。"""
    out: Dict[str, Any] = {
        "status": status,
        "http_status": http_status,
        "request_url": request_url,
        "elapsed_ms": elapsed_ms,
        "raw_list_count": raw_list_count,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "skipped_known": skipped_known,
        "skipped_unsupported": skipped_unsupported,
        "skipped_no_detail": skipped_no_detail,
        "rate_limited": rate_limited,
        "detail_errors": detail_errors or [],
        "records": records,
    }
    out.update(extra)
    return out


# ==============================================================================
# 站点 1：特惠采采 / 惠享购 (`https://xhhcds.cn/`)
# ==============================================================================
def fetch_tehuicaicai_deals(
    channel: str = "home",
    page: int = 1,
    page_size: int = 50,
    max_pages: int = 10,
    category_id: Optional[int] = None,
    fetch_detail: bool = True,
    max_details: int = 50,
    detail_delay: float = 0.05,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[Set[str]] = None,
    auto_paginate: bool = True,
    full_sweep: bool = False,
) -> Dict[str, Any]:
    """
    特惠采采专员精选集采流 (`https://xhhcds.cn/`)：
- 列表接口：`https://xhhcds.cn/api/v1/public/home-products/?feed=home&page={page}&page_size={page_size}`
- 详情接口：`https://xhhcds.cn/api/v1/products/{id}/`
- 策略：仅输出获取了完整详情、教程文案及有效下单线索的淘/京/拼商品。
- `full_sweep=True`（每轮全量巡视）：列表翻到最后一页、不因"整页已抓过"或详情配额用完而提前收工，
  已抓过的商品不重复抓详情，只把 ID 收进 `touch_ids` 交给上层刷新"本轮确认仍在架"的时间
  （`last_seen_at`）。这站是人工严选 JSON 接口、无风控，全量扫只是多几个轻请求。
    """
    http = session or requests.Session()
    base_api = "https://xhhcds.cn/api/v1"
    skip_set = skip_detail_ids or set()
    eff_delay = max(0.0, float(detail_delay))

    records: List[Dict[str, Any]] = []
    seen_ids: set = set()
    raw_list_count = 0
    detail_fetched = 0
    detail_attempted = 0
    skipped_known = 0
    skipped_unsupported = 0
    skipped_no_detail = 0
    detail_circuit_broken = False
    rate_limited = False
    detail_errors: List[Dict[str, Any]] = []
    total_items = 0
    pages_crawled = 0
    current_page = page
    primary_req_url = ""
    touch_ids: Set[str] = set()
    t0 = time.perf_counter()

    while current_page <= max_pages:
        if channel == "category" and category_id:
            list_url = f"{base_api}/public/categories/{category_id}/products/?page={current_page}&page_size={page_size}"
            web_page_url = f"https://xhhcds.cn/#/category?id={category_id}"
            channel_name = f"category_{category_id}"
        else:
            list_url = f"{base_api}/public/home-products/?feed=home&page={current_page}&page_size={page_size}"
            web_page_url = "https://xhhcds.cn/"
            channel_name = "home_products"

        if not primary_req_url:
            primary_req_url = list_url

        headers = {
            **DEFAULT_HEADERS,
            "Accept": "application/json",
            "Referer": web_page_url,
            "X-Request-ID": f"fetch-thcc-{int(time.time() * 1000)}-{current_page}",
        }

        resp = None
        try:
            resp = http.get(list_url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                if resp.status_code in _RATE_LIMIT_STATUSES:
                    rate_limited = True
                if pages_crawled == 0:
                    return make_fetch_result(
                        status="error",
                        http_status=resp.status_code,
                        request_url=list_url,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                        records=[],
                        rate_limited=rate_limited,
                        detail_errors=[{"error": f"HTTP {resp.status_code}", "http_status": resp.status_code}],
                        pages_crawled=0,
                    )
                break
            body = resp.json()
        except Exception as e:
            if pages_crawled == 0:
                return make_fetch_result(
                    status="error",
                    http_status=getattr(resp, "status_code", None),
                    request_url=list_url,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                    records=[],
                    detail_errors=[{"error": str(e), "error_type": type(e).__name__}],
                    pages_crawled=0,
                )
            break

        data_block = body.get("data") if isinstance(body, dict) else {}
        if not isinstance(data_block, dict):
            data_block = {}

        raw_items = data_block.get("items") or []
        if not isinstance(raw_items, list) or not raw_items:
            break

        pagination = data_block.get("pagination") or {}
        total_items = pagination.get("total", len(raw_items))
        pages_crawled += 1
        new_items_on_page = 0

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            raw_list_count += 1

            raw_plat_code = str(item.get("platform_code") or "").strip().lower()
            platform_code = _normalize_explicit_platform_code(raw_plat_code)
            platform_name = str(
                item.get("platform_name")
                or ("京东" if platform_code == "jd" else ("拼多多" if platform_code == "pdd" else ("淘宝" if platform_code == "taobao" else "")))
            ).strip()

            # 100% 确定非淘京拼平台直接跳过
            if is_confirmed_unsupported_deal(platform_code=raw_plat_code, mall_name=platform_name):
                skipped_unsupported += 1
                continue

            is_known = item_id in skip_set
            if is_known:
                skipped_known += 1
                if fetch_detail:
                    if full_sweep:
                        touch_ids.add(item_id)
                    continue
            else:
                new_items_on_page += 1

            title = str(item.get("title") or "").strip()
            display_price = str(item.get("display_price") or "").strip()
            cover_info = item.get("cover_image") or {}
            image_url = ""
            if isinstance(cover_info, dict):
                image_url = str(cover_info.get("public_url") or cover_info.get("direct_public_url") or "").strip()

            source_url = f"https://xhhcds.cn/#/product/{item_id}"
            source_detail_url = f"{base_api}/products/{item_id}/"
            hot_metrics: Dict[str, Any] = {
                "sold_count": item.get("sold_count"),
                "badge_text": item.get("badge_text"),
                "platform_code": platform_code or raw_plat_code,
            }

            if not fetch_detail:
                rec = build_deal_record(
                    source_site="tehuicaicai",
                    source_channel=f"{channel_name}:{platform_code}" if platform_code else channel_name,
                    source_item_id=item_id,
                    source_url=source_url,
                    source_list_page_url=list_url,
                    source_detail_url=source_detail_url,
                    title=title,
                    price_text=display_price,
                    image_url=image_url,
                    mall_name=platform_name,
                    explicit_platform=platform_code or None,
                    hot_metrics=hot_metrics,
                    tutorial_text=f"【{platform_name or '特惠采采'}】{title}\n到手价: {display_price}".strip(),
                    tutorial_images=[image_url] if image_url else [],
                    raw_payload=item,
                    is_group_stream=False,
                )
                rec["has_full_detail"] = False
                records.append(rec)
                continue

            if detail_circuit_broken or detail_attempted >= max_details:
                if not full_sweep:
                    break
                # 全量巡视：详情配额用完只停止抓详情，列表继续翻完（否则第一页就收工，全量毫无意义）
                continue

            if detail_attempted > 0 and eff_delay > 0:
                time.sleep(eff_delay)
            detail_attempted += 1

            try:
                det_headers = {
                    **DEFAULT_HEADERS,
                    "Accept": "application/json",
                    "Referer": source_url,
                    "X-Request-ID": f"fetch-thcc-det-{item_id}-{int(time.time() * 1000)}",
                }
                r_det = http.get(source_detail_url, headers=det_headers, timeout=timeout)
                if r_det.status_code != 200:
                    if r_det.status_code in _RATE_LIMIT_STATUSES:
                        detail_circuit_broken = True
                        rate_limited = True
                    skipped_no_detail += 1
                    detail_errors.append({
                        "item_id": item_id,
                        "error_url": source_detail_url,
                        "http_status": r_det.status_code,
                        "error_type": f"HTTP_{r_det.status_code}",
                        "error": f"详情页返回状态码: {r_det.status_code}",
                    })
                    continue

                det_body = r_det.json()
                det = det_body.get("data") if isinstance(det_body, dict) else {}
                if not isinstance(det, dict) or not det:
                    skipped_no_detail += 1
                    continue

                det_plat_raw = str(det.get("platform_code") or raw_plat_code).strip().lower()
                det_plat = _normalize_explicit_platform_code(det_plat_raw)
                det_plat_name = str(det.get("platform_name") or platform_name).strip()
                if is_confirmed_unsupported_deal(platform_code=det_plat_raw, mall_name=det_plat_name):
                    skipped_unsupported += 1
                    continue

                mi = det.get("material_info") if isinstance(det.get("material_info"), dict) else {}
                cmd_text = str(mi.get("command_text") or "").strip()
                second_cmd = str(mi.get("second_command_text") or "").strip()
                cmd_instr = str(mi.get("command_instruction_text") or "").strip()
                qr_instr = str(mi.get("qr_instruction_text") or "").strip()
                purchase_url = str(mi.get("purchase_url") or "").strip()
                coupon_urls = [str(u).strip() for u in (mi.get("coupon_urls") or []) if str(u or "").strip()]
                qr_img = str(mi.get("qr_image_url") or "").strip()
                second_qr_img = str(mi.get("second_qr_image_url") or "").strip()
                desc_html = str(det.get("description_html") or "").strip()
                desc_plain = str(det.get("description") or "").strip()
                review_html = str(det.get("review_detail_content") or "").strip()

                provider = str(det.get("provider_nickname") or "").strip()
                if provider:
                    hot_metrics["provider_nickname"] = provider
                publish_time = str(det.get("updated_at") or det.get("created_at") or "").strip()

                video_url = ""
                tutorials = det.get("tutorials") or []
                if isinstance(tutorials, list):
                    for tut in tutorials:
                        if isinstance(tut, dict) and tut.get("video_url"):
                            video_url = str(tut["video_url"]).strip()
                            hot_metrics["video_tutorial_url"] = video_url
                            break

                full_text_parts: List[str] = []
                if cmd_text:
                    full_text_parts.append(re.sub(r"(https?://u\.jd\.com)///+", r"\1/", cmd_text))
                if second_cmd and second_cmd != cmd_text:
                    full_text_parts.append(re.sub(r"(https?://u\.jd\.com)///+", r"\1/", second_cmd))
                if cmd_instr and cmd_instr not in "\n".join(full_text_parts):
                    full_text_parts.append(cmd_instr)
                if qr_instr and qr_instr not in "\n".join(full_text_parts):
                    full_text_parts.append(qr_instr)

                if not full_text_parts:
                    if desc_html:
                        desc_clean = html_to_tutorial_text(desc_html, base_url=source_url)
                        if desc_clean:
                            full_text_parts.append(desc_clean)
                    elif review_html:
                        rev_clean = html_to_tutorial_text(review_html, base_url=source_url)
                        if rev_clean:
                            full_text_parts.append(rev_clean)
                    elif desc_plain:
                        full_text_parts.append(desc_plain)

                # 没有任何详情文案或教程说明时，视为无教程空壳单品，直接丢弃
                has_tutorial_content = bool(full_text_parts or video_url or qr_img or second_qr_img)
                if not has_tutorial_content:
                    skipped_no_detail += 1
                    continue

                combined_tutorial_text = "\n".join(full_text_parts).strip()
                if not combined_tutorial_text and (video_url or qr_img):
                    combined_tutorial_text = f"【{det_plat_name or '特惠采采'}】{title}\n到手价: {display_price}".strip()

                links, imgs, tokens = extract_links_images_and_tokens(combined_tutorial_text, base_url=source_url)
                seen_link_urls = {str(lk.get("url") or "") for lk in links}
                for cu in coupon_urls:
                    if cu.startswith(("http://", "https://")) and cu not in seen_link_urls:
                        seen_link_urls.add(cu)
                        links.append({
                            "label": "特惠采采领券链接",
                            "url": cu,
                            "type": "coupon_url",
                            "platform": detect_platform_signal(cu) or det_plat or "unknown",
                        })
                if purchase_url.startswith(("http://", "https://")) and purchase_url not in seen_link_urls:
                    seen_link_urls.add(purchase_url)
                    links.append({
                        "label": "特惠采采下单短链",
                        "url": purchase_url,
                        "type": "purchase_url",
                        "platform": detect_platform_signal(purchase_url) or det_plat or "unknown",
                    })

                all_imgs: List[str] = [image_url] if image_url else []
                for extra_img in [*imgs, qr_img, second_qr_img]:
                    if extra_img and extra_img not in all_imgs:
                        all_imgs.append(extra_img)

                if is_confirmed_unsupported_deal(
                    platform_code=det_plat_raw,
                    mall_name=det_plat_name,
                    links=links,
                    tokens=tokens,
                ):
                    skipped_unsupported += 1
                    continue

                rec = build_deal_record(
                    source_site="tehuicaicai",
                    source_channel=f"{channel_name}:{det_plat or platform_code}" if (det_plat or platform_code) else channel_name,
                    source_item_id=item_id,
                    source_url=source_url,
                    source_list_page_url=list_url,
                    source_detail_url=source_detail_url,
                    title=str(det.get("title") or title).strip() or title,
                    price_text=str(det.get("display_price") or display_price).strip(),
                    image_url=image_url,
                    mall_name=det_plat_name,
                    publish_time=publish_time,
                    explicit_platform=det_plat or None,
                    hot_metrics=hot_metrics,
                    tutorial_text=combined_tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=links,
                    extracted_tokens=tokens,
                    raw_payload=det,
                    is_group_stream=False,
                )
                rec["has_full_detail"] = True

                if not has_valid_detail_and_tutorial(rec):
                    skipped_no_detail += 1
                    logger.warning(
                        "  ⚠️ [特惠采采-无购买锚点跳过] id=%s | title=%s | 详情无可用淘/京/拼链接或口令，暂不入库",
                        item_id,
                        title[:32],
                    )
                    continue

                detail_fetched += 1
                records.append(rec)
            except Exception as e_det:
                skipped_no_detail += 1
                detail_errors.append({
                    "item_id": item_id,
                    "error_url": source_detail_url,
                    "http_status": None,
                    "error_type": type(e_det).__name__,
                    "error": str(e_det)[:160],
                })

        if not auto_paginate:
            break
        # 全量巡视模式下"整页均为已抓商品""详情配额用完"都不构成收工理由，只有翻到末页才停
        if not full_sweep and new_items_on_page == 0:
            break
        if detail_circuit_broken and not full_sweep:
            break
        if not full_sweep and fetch_detail and detail_attempted >= max_details:
            break
        if not pagination.get("has_next", True):
            break

        current_page += 1
        if eff_delay > 0:
            time.sleep(eff_delay)

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    is_ok = pages_crawled > 0 and not (rate_limited and len(records) == 0)
    return make_fetch_result(
        status="success" if is_ok else "error",
        http_status=200 if pages_crawled > 0 else None,
        request_url=primary_req_url or f"{base_api}/public/home-products/?feed=home&page={page}&page_size={page_size}",
        elapsed_ms=elapsed_ms,
        records=records,
        raw_list_count=raw_list_count,
        detail_fetched=detail_fetched,
        skipped_known=skipped_known,
        skipped_unsupported=skipped_unsupported,
        skipped_no_detail=skipped_no_detail,
        rate_limited=rate_limited,
        detail_errors=detail_errors,
        total=total_items,
        pages_crawled=pages_crawled,
        touch_ids=sorted(touch_ids),
    )


# ==============================================================================
# 站点 2：逛丢 (`https://guangdiu.com/`)
# ==============================================================================
_GUANGDIU_CAPTCHA_MARKERS: Tuple[str, ...] = (
    "安全验证",
    "verify-wrap",
    "slidetrack",
    "请按住滑块",
    "拖动到最右边",
    "逛丢安全验证",
)

DEFAULT_GUANGDIU_COOKIE_FILE: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "guangdiu_cookies.json"
)


def _is_guangdiu_captcha_page(url: str, html_text: str) -> bool:
    """判断响应是否落入逛丢 `verify.php` 滑块安全验证页。"""
    if "verify.php" in str(url or "").lower():
        return True
    body = str(html_text or "")
    return any(marker in body for marker in _GUANGDIU_CAPTCHA_MARKERS)


def _load_guangdiu_cookies(
    session: requests.Session,
    cookie_file: Optional[str] = DEFAULT_GUANGDIU_COOKIE_FILE,
) -> bool:
    """从本地 JSON 文件加载已验证的逛丢 Cookie 到 requests.Session。"""
    if not cookie_file or not os.path.exists(cookie_file):
        return False
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return False
        now_ts = time.time()
        loaded = 0
        for c in items:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            value = str(c.get("value") or "")
            if not name:
                continue
            expires = c.get("expires")
            if isinstance(expires, (int, float)) and expires > 0 and expires < now_ts:
                continue
            domain = str(c.get("domain") or "guangdiu.com")
            path = str(c.get("path") or "/")
            session.cookies.set(name, value, domain=domain, path=path)
            loaded += 1
        return loaded > 0
    except Exception:
        return False


def _save_guangdiu_cookies(
    session: requests.Session,
    cookie_file: Optional[str] = DEFAULT_GUANGDIU_COOKIE_FILE,
) -> None:
    """将 requests.Session 中的逛丢相关 Cookie 持久化到本地 JSON 文件。"""
    if not cookie_file or not hasattr(session, "cookies"):
        return
    try:
        from requests.cookies import RequestsCookieJar
        if not isinstance(session.cookies, (RequestsCookieJar, list, tuple)):
            return
        parent = os.path.dirname(os.path.abspath(cookie_file))
        if parent:
            os.makedirs(parent, exist_ok=True)
        out: List[Dict[str, Any]] = []
        for c in session.cookies:
            c_name = getattr(c, "name", None)
            if not c_name or not isinstance(c_name, str):
                continue
            dom = str(getattr(c, "domain", "") or "")
            if dom and "guangdiu.com" not in dom.lower():
                continue
            out.append({
                "name": c_name,
                "value": getattr(c, "value", ""),
                "domain": dom or "guangdiu.com",
                "path": getattr(c, "path", "/") or "/",
                "expires": getattr(c, "expires", None),
            })
        if out:
            with open(cookie_file, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _launch_guangdiu_chromium(
    p: Any,
    *,
    headless: bool = True,
    proxy: Optional[str] = None,
    browser_channel: Optional[str] = "chrome",
    bypass_system_proxy: bool = True,
) -> Any:
    """
    启动浏览器用于逛丢滑块验证：
    优先复用本机系统 Chrome（与项目其余 Playwright 模块一致，无需额外下载 chromium_headless_shell），
    若未检测到则依次回退至 Edge 与 Playwright 内置 Chromium。
    """
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        # 禁用 QUIC：逛丢首包在走代理/丢 UDP 的网络下会因 HTTP/3 握手卡住（实测表现为
        # ERR_TIMED_OUT 或半开连接），强制走 TCP 后验证页能正常提交
        "--disable-quic",
    ]
    if not proxy and bypass_system_proxy:
        launch_args.append("--no-proxy-server")

    base_kwargs: Dict[str, Any] = {
        "headless": headless,
        "args": launch_args,
        "ignore_default_args": [
            "--enable-automation",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-component-extensions-with-background-pages",
        ],
    }
    if proxy:
        base_kwargs["proxy"] = {"server": proxy}

    candidates: List[Optional[str]] = []
    if browser_channel and str(browser_channel).strip().lower() not in ("", "chromium", "none"):
        candidates.append(str(browser_channel).strip())
    if "chrome" not in candidates:
        candidates.append("chrome")
    if os.name == "nt" and "msedge" not in candidates:
        candidates.append("msedge")
    candidates.append(None)

    last_err: Optional[Exception] = None
    for ch in candidates:
        kwargs = dict(base_kwargs)
        if ch:
            kwargs["channel"] = ch
        try:
            return p.chromium.launch(**kwargs)
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err is not None else RuntimeError("无法启动任何可用 Chromium/Chrome 浏览器")


_GUANGDIU_SLIDE_MAX_ATTEMPTS: int = 3


def _guangdiu_slide_geometry(page: Any) -> Optional[Dict[str, float]]:
    """
    读取逛丢滑块轨道与把手的真实几何尺寸（CSS 像素）。

    前端 `verify.php` 的实现是：
      `maxX = track.clientWidth - handle.offsetWidth`，
      mousemove 中位移达到 `maxX - 1` 才置 done 并 POST `verifyconfirm.php`（携带 token/dur/moves）。
    因此终点必须按真实尺寸算，且必须产生真实的 mousedown 与连续多帧 mousemove。
    """
    try:
        geo = page.evaluate(
            """() => {
                const track = document.getElementById('slidetrack');
                const handle = document.getElementById('slidehandle');
                if (!track || !handle) return null;
                const t = track.getBoundingClientRect();
                const h = handle.getBoundingClientRect();
                if (!t.width || !h.width) return null;
                return {
                    trackLeft: t.left, trackTop: t.top, trackWidth: t.width, trackHeight: t.height,
                    handleLeft: h.left, handleTop: h.top, handleWidth: h.width, handleHeight: h.height
                };
            }"""
        )
    except Exception:
        return None
    return geo if isinstance(geo, dict) else None


def _guangdiu_drag_slider(page: Any, geo: Dict[str, float], *, rng: random.Random) -> None:
    """
    完成一次完整的真人式滑动：落点对准把手 → 按住 → 变速分段拖动 → 精确到达最右端 → 松手。

    - 每帧位移带微小随机抖动、纵向 1~2 像素手部漂移，全程约 1.0~1.4 秒、数十次 mousemove，
      与服务端校验的 `dur` / `moves` 口径一致；
    - 终点取 `trackLeft + maxX + 3px`，前端会把位移 clamp 到 maxX，确保满足 `x >= maxX - 1`；
    - 必须「先到达终点、再松手」：前端 `onUp` 在未完成时会把把手瞬间弹回起点并清空填充，
      这正是此前观感上「拖了一下却没松开、页面一直停在原地」的根因。
    """
    max_x = float(geo["trackWidth"]) - float(geo["handleWidth"])
    if max_x <= 4:
        raise RuntimeError(
            f"滑块轨道尺寸异常: track_width={geo['trackWidth']:.1f}, handle_width={geo['handleWidth']:.1f}"
        )
    grip_x = float(geo["handleLeft"]) + float(geo["handleWidth"]) / 2.0
    grip_y = float(geo["handleTop"]) + float(geo["handleHeight"]) / 2.0
    end_x = float(geo["trackLeft"]) + max_x + 3.0

    page.mouse.move(grip_x, grip_y, steps=5)
    page.wait_for_timeout(rng.randint(120, 260))

    page.mouse.down()
    page.wait_for_timeout(rng.randint(70, 140))

    duration_s = rng.uniform(0.95, 1.35)
    frames = max(28, int(duration_s / 0.018))
    prev_x = 0.0
    for i in range(1, frames + 1):
        progress = i / float(frames)
        eased = 1.0 - (1.0 - progress) ** 3
        x = eased * max_x + rng.uniform(-0.8, 0.8)
        x = max(prev_x, min(x, max_x))
        prev_x = x
        page.mouse.move(grip_x + x, grip_y + rng.uniform(-1.5, 1.5))
        page.wait_for_timeout(rng.randint(14, 26))

    page.mouse.move(end_x, grip_y, steps=2)
    page.wait_for_timeout(rng.randint(90, 160))
    page.mouse.up()


def _open_guangdiu_page(page: Any, url: str, *, timeout_ms: int, attempts: int = 3, settle_ms: int = 600) -> None:
    """
    打开逛丢页面并等滑块控件就绪。

    两处必须如此：
    1. 用 `wait_until="commit"` 而不是 `domcontentloaded`：逛丢首屏实测 10~20 秒、偶发首包
       超时，等 DOMContentLoaded 会把浏览器卡死在半开状态（Playwright 超时也不会触发），
       卡住后整个验证流程既不报错也不推进；
    2. 真正需要的是滑块 DOM，因此提交后直接等 `#slidetrack` 挂载，拿到即可开始拖动。
    """
    last_err: Optional[Exception] = None
    for i in range(max(1, int(attempts))):
        try:
            page.goto(url, wait_until="commit", timeout=timeout_ms)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if i + 1 < attempts:
                page.wait_for_timeout(1200)
    if last_err is not None:
        raise last_err

    if "verify.php" in (page.url or "").lower():
        try:
            page.wait_for_selector(
                "#slidetrack",
                state="attached",
                timeout=min(15000, max(3000, int(timeout_ms // 3))),
            )
        except Exception:
            pass
    page.wait_for_timeout(max(200, int(settle_ms)))


def _guangdiu_human_cookie(context: Any) -> str:
    """读取服务端放行 Cookie `gdhuman` 的当前值（尚未下发返回空串）。"""
    for c in context.cookies():
        if str(c.get("name") or "") == "gdhuman":
            return str(c.get("value") or "")
    return ""


def _sync_guangdiu_cookies(
    session: requests.Session,
    context: Any,
    site_host: str,
    cookie_file: Optional[str] = DEFAULT_GUANGDIU_COOKIE_FILE,
) -> List[str]:
    """把浏览器里本站域名的 Cookie 写回 `requests.Session` 并持久化，返回 Cookie 名列表。"""
    names: List[str] = []
    for c in context.cookies():
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if dom and not (dom == site_host or dom.endswith("." + site_host)):
            continue
        names.append(str(c["name"]))
        session.cookies.set(
            str(c["name"]),
            str(c["value"]),
            domain=dom or site_host,
            path=str(c.get("path") or "/"),
        )
    _save_guangdiu_cookies(session, cookie_file)
    return names


def _solve_guangdiu_slider_playwright(
    session: requests.Session,
    target_url: str,
    *,
    verify_url: str = "",
    user_agent: str = DEFAULT_HEADERS["User-Agent"],
    timeout: float = 20.0,
    headless: bool = True,
    proxy: Optional[str] = None,
    cookie_file: Optional[str] = DEFAULT_GUANGDIU_COOKIE_FILE,
    max_drag_retries: int = _GUANGDIU_SLIDE_MAX_ATTEMPTS,
    browser_channel: Optional[str] = "chrome",
    bypass_system_proxy: bool = True,
) -> Tuple[bool, str, str]:
    """
    使用 Playwright 自动通过逛丢 `verify.php` 滑块验证，并把放行 Cookie 同步回 `requests.Session`。
    返回 `(solved, "", error_msg)`——正文一律由上层用同步好的 Cookie 重拉，这里不代取。

    只认「服务端是否放行」这一件事：前端只有把把手推到 `maxX - 1` 才 POST `verifyconfirm.php`，
    回执 ok 时服务端下发/轮换 `gdhuman` Cookie，所以「拖动后 `gdhuman` 变成新值」即通过；
    不看界面上的 ✓ / 类名（跳转后元素消失，易误判），也不读该 XHR 的响应体
    （慢网下它会永久挂起，跳转后 Chrome 会丢弃它）。没拿到新 Cookie 就重开验证页换 token 重拖，
    宁可多滑几次，也不加更多取证与兜底分支。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return (
            False,
            "",
            "未安装 playwright，请先执行: pip install playwright",
        )

    open_url = verify_url or target_url
    # 逛丢直连实测往返常在 10~20 秒，首屏超时必须给足，否则浏览器会停在半开的验证页上
    goto_timeout_ms = int(max(35.0, float(timeout) * 1.6) * 1000)
    rng = random.Random()
    site_host = (urlsplit(target_url).hostname or "").lower() or "guangdiu.com"
    attempts = max(1, int(max_drag_retries))

    try:
        with sync_playwright() as p:
            browser = _launch_guangdiu_chromium(
                p,
                headless=headless,
                proxy=proxy,
                browser_channel=browser_channel,
                bypass_system_proxy=bypass_system_proxy,
            )
            try:
                context = browser.new_context(
                    user_agent=user_agent,
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 800},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                # 带着会话里已有的本站 Cookie 起跑：若它仍有效，站点会直接放行、根本不出验证页
                pw_cookies = [
                    {
                        "name": c.name,
                        "value": c.value,
                        "domain": str(c.domain or site_host).lstrip(".") or site_host,
                        "path": c.path or "/",
                    }
                    for c in session.cookies
                    if not c.domain
                    or str(c.domain).lstrip(".").lower() == site_host
                    or str(c.domain).lstrip(".").lower().endswith("." + site_host)
                ]
                if pw_cookies:
                    try:
                        context.add_cookies(pw_cookies)
                    except Exception:
                        pass

                page = context.new_page()
                page.set_default_timeout(goto_timeout_ms)

                # 仅用于诊断文案：拖动是否真的把把手推到了终点（前端只有那时才上报）
                reports: List[str] = []
                page.on(
                    "request",
                    lambda req: reports.append(req.post_data or "")
                    if "verifyconfirm.php" in (req.url or "")
                    else None,
                )

                _open_guangdiu_page(
                    page, open_url, timeout_ms=goto_timeout_ms, settle_ms=rng.randint(500, 900)
                )

                last_err = ""
                for attempt in range(1, attempts + 1):
                    geo = _guangdiu_slide_geometry(page)
                    if not geo:
                        last_err = "验证页未渲染出 #slidetrack / #slidehandle 滑块控件，无法自动拖动"
                        break

                    reports_before = len(reports)
                    before = _guangdiu_human_cookie(context)
                    _guangdiu_drag_slider(page, geo, rng=rng)

                    # 松手后服务端回执 + 下发 Cookie 要几百毫秒，最多等 5 秒
                    deadline = time.time() + 5.0
                    after = before
                    while True:
                        after = _guangdiu_human_cookie(context)
                        if after and after != before:
                            break
                        if time.time() >= deadline:
                            break
                        page.wait_for_timeout(200)

                    cookie_names = _sync_guangdiu_cookies(session, context, site_host, cookie_file)
                    if after and after != before:
                        return True, "", ""

                    last_err = (
                        f"第 {attempt}/{attempts} 次拖动未获服务端放行"
                        f"（上报={'是' if len(reports) > reports_before else '无'}，"
                        f"gdhuman={after[:16] or '未下发'}，Cookie={'、'.join(cookie_names) or '无'}）"
                    )

                    if attempt < attempts:
                        # 上报失败后 token 即作废，必须重开验证页拿新 token 再拖
                        try:
                            _open_guangdiu_page(
                                page,
                                open_url,
                                timeout_ms=goto_timeout_ms,
                                settle_ms=rng.randint(700, 1200),
                            )
                        except Exception as e_reload:
                            last_err = (
                                f"重新打开验证页失败: {type(e_reload).__name__}: {str(e_reload)[:120]}"
                            )
                            break

                return False, "", last_err or "滑块自动拖动未通过"
            finally:
                browser.close()
    except Exception as e:
        return False, "", f"Playwright 滑块验证异常: {type(e).__name__}: {str(e)[:160]}"


def _unwrap_guangdiu_to_php(html_fragment: str) -> str:
    """将逛丢内部跳板 `guangdiu.com/to.php?u=...` 与 `iguangdiu.com/to.php?u=...` 还原为明文电商 URL。"""
    def _repl(m: re.Match) -> str:
        return html_lib.unescape(unquote(m.group(1)))

    return re.sub(
        r"(?:https?://(?:[a-z0-9-]+\.)?i?guangdiu\.com/)?to\.php\?[^\s\"'<>]*\bu=(https?%3A%2F%2F[^&\"'\s<>]+)",
        _repl,
        html_fragment,
        flags=re.I,
    )


def _extract_guangdiu_dabstract_text(unwrapped_dabs_html: str, detail_url: str) -> str:
    """提取逛丢 `#dabstract` 中的纯教程文字（剥离封面对照容器 `simgahead` 与纯图片占位后再校验实质内容）。"""
    dabs_text = html_to_tutorial_text(unwrapped_dabs_html, base_url=detail_url)
    return dabs_text.strip()


def _has_substantive_guangdiu_tutorial(dabs_text: str, title: str) -> bool:
    """判断逛丢详情页 `#dabstract` 是否包含真正的爆料作业/优惠说明（而非纯图片或标题重复空壳）。"""
    if not dabs_text:
        return False
    no_imgs = re.sub(r"\[(?:群聊晒单/教程图片|教程图片):[^\]]*\]", " ", dabs_text)
    no_urls = re.sub(r"https?://\S+", " ", no_imgs)
    clean_body = re.sub(r"\s+", "", no_urls)
    if len(clean_body) < 6:
        return False
    clean_title = re.sub(r"\s+", "", str(title or ""))
    if clean_title and clean_body == clean_title:
        return False
    return True


def fetch_guangdiu_deals(
    channel: str = "latest",
    page: int = 1,
    fetch_detail: bool = True,
    max_details: int = 8,
    detail_delay: float = 1.0,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[Set[str]] = None,
    enable_slider_solver: bool = True,
    cookie_file: Optional[str] = DEFAULT_GUANGDIU_COOKIE_FILE,
    max_captcha_solves: int = 2,
    solver_headless: bool = True,
    proxy: Optional[str] = None,
    browser_channel: Optional[str] = "chrome",
    bypass_system_proxy: bool = True,
) -> Dict[str, Any]:
    """
    逛丢全网爆料与榜单流 (`https://guangdiu.com/`)：
- 支持频道：
  - `"latest"` -> `https://guangdiu.com/` (首页)
  - `"hots"`   -> `https://guangdiu.com/hots.php` (热门榜)
  - `"rank"`   -> `https://guangdiu.com/rank.php` (小时风云榜)
  - `"pdd"`    -> `https://guangdiu.com/cate.php?m=拼多多`
  - `"cheaps"` -> `https://guangdiu.com/cheaps.php`
- 策略：
1. 列表页先行 100% 确定性平台过滤 + 已抓 ID 去重（不发多余 `detail.php`/`go.php` 请求）；
  1. 优先拉取 `detail.php` 检查滑块验证码与 `#dabstract` 实质教程，触发滑块时自动通过验证并同步 Cookie；
  2. 详情通过后再拉取 `go.php` 解包真实直达购买链，并二次确认非支持平台过滤。
    """
    http = session or requests.Session()
    if proxy:
        http.proxies.update({"http": proxy, "https": proxy})
    elif bypass_system_proxy:
        # 逛丢为国内直连站点，避免被本机系统代理/VPN 劫持导致 SSL 握手超时
        http.trust_env = False
    if cookie_file:
        _load_guangdiu_cookies(http, cookie_file)

    base = "https://guangdiu.com"
    if channel == "rank":
        list_url = f"{base}/rank.php"
    elif channel == "hots":
        list_url = f"{base}/hots.php" if page <= 1 else f"{base}/hots.php?p={page}"
    elif channel == "pdd":
        pdd_m = quote("拼多多")
        list_url = f"{base}/cate.php?m={pdd_m}" if page <= 1 else f"{base}/cate.php?m={pdd_m}&p={page}"
    elif channel == "cheaps":
        list_url = f"{base}/cheaps.php" if page <= 1 else f"{base}/cheaps.php?p={page}"
    else:
        list_url = f"{base}/" if page <= 1 else f"{base}/index.php?p={page}"

    eff_delay = max(0.0, float(detail_delay))
    captcha_triggered = 0
    captcha_solved = 0
    captcha_solve_attempts = 0

    def _try_solve_captcha(target_url: str, verify_url: str) -> Tuple[bool, str, str]:
        nonlocal captcha_triggered, captcha_solved, captcha_solve_attempts
        captcha_triggered += 1
        if not enable_slider_solver:
            return False, "", "滑块自动验证已关闭 (enable_slider_solver=False)"
        if captcha_solve_attempts >= max(0, int(max_captcha_solves)):
            return False, "", f"本轮滑块自动验证已达上限 ({max_captcha_solves} 次)"
        captcha_solve_attempts += 1
        ok, landed_html, err = _solve_guangdiu_slider_playwright(
            http,
            target_url,
            verify_url=verify_url,
            user_agent=DEFAULT_HEADERS["User-Agent"],
            timeout=max(18.0, float(timeout)),
            headless=solver_headless,
            proxy=proxy,
            cookie_file=cookie_file,
            browser_channel=browser_channel,
            bypass_system_proxy=bypass_system_proxy,
        )
        if ok:
            captcha_solved += 1
        return ok, landed_html, err

    t0 = time.perf_counter()
    eff_timeout = max(15.0, float(timeout))
    resp = None
    try:
        resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=eff_timeout)
        resp.encoding = "utf-8"
        list_html = resp.text
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return make_fetch_result(
            status="error",
            http_status=getattr(resp, "status_code", 0) if resp is not None else 0,
            request_url=list_url,
            elapsed_ms=elapsed_ms,
            records=[],
            rate_limited=True,
            detail_errors=[{
                "error_url": list_url,
                "http_status": 0,
                "error_type": type(e).__name__,
                "error": f"逛丢列表页网络请求异常 ({type(e).__name__}): {str(e)[:160]}",
            }],
            captcha_triggered=captcha_triggered,
            captcha_solved=captcha_solved,
        )

    resp_url = getattr(resp, "url", list_url) or list_url
    if resp.status_code == 200 and _is_guangdiu_captcha_page(resp_url, list_html):
        solved, landed_html, solve_err = _try_solve_captcha(list_url, resp_url)
        if solved:
            if landed_html and not _is_guangdiu_captcha_page(list_url, landed_html):
                list_html = landed_html
            else:
                try:
                    resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=eff_timeout)
                    resp.encoding = "utf-8"
                    list_html = resp.text
                except Exception as e_retry:
                    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                    return make_fetch_result(
                        status="error",
                        http_status=0,
                        request_url=list_url,
                        elapsed_ms=elapsed_ms,
                        records=[],
                        rate_limited=True,
                        detail_errors=[{
                            "error_url": list_url,
                            "http_status": 0,
                            "error_type": type(e_retry).__name__,
                            "error": f"滑块通过后重拉列表页网络异常: {str(e_retry)[:160]}",
                        }],
                        captcha_triggered=captcha_triggered,
                        captcha_solved=captcha_solved,
                    )
        else:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return make_fetch_result(
                status="error",
                http_status=resp.status_code,
                request_url=list_url,
                elapsed_ms=elapsed_ms,
                records=[],
                rate_limited=True,
                detail_errors=[{
                    "error_url": resp_url,
                    "http_status": resp.status_code,
                    "error_type": "CaptchaChallengeDetected",
                    "error": f"逛丢列表页触发滑块验证且自动通过失败: {solve_err}",
                }],
                captcha_triggered=captcha_triggered,
                captcha_solved=captcha_solved,
            )

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    if resp.status_code != 200:
        is_rl = resp.status_code in _RATE_LIMIT_STATUSES
        return make_fetch_result(
            status="error",
            http_status=resp.status_code,
            request_url=list_url,
            elapsed_ms=elapsed_ms,
            records=[],
            rate_limited=is_rl,
            detail_errors=[{
                "error_url": list_url,
                "http_status": resp.status_code,
                "error_type": f"HTTP_{resp.status_code}",
                "error": f"列表页返回非200状态码: {resp.status_code}",
            }],
            captcha_triggered=captcha_triggered,
            captcha_solved=captcha_solved,
        )

    is_rank = (channel == "rank")
    blocks: List[str] = []
    if is_rank:
        blocks = re.findall(
            r'(<div\s+class=["\'][^"\']*\bhourrankitem\b[^"\']*["\'][^>]*>.*?)(?=<div\s+class=["\'][^"\']*\bhourrankitem\b|<div\s+id=["\'][^"\']*(?:footer|sidebar)|<div\s+class=["\']headerholder|$)',
            list_html,
            re.S | re.I,
        )
    if not blocks:
        blocks = re.findall(
            r'(<div\s+class=["\'][^"\']*\bgooditem\b[^"\']*["\'][^>]*>.*?)(?=<div\s+class=["\'][^"\']*\bgooditem\b|<div\s+class=["\'][^"\']*\b(?:page|centerfix)\b|$)',
            list_html,
            re.S | re.I,
        )

    records: List[Dict[str, Any]] = []
    seen_ids: set = set()
    raw_list_count = 0
    detail_fetched = 0
    detail_attempted = 0
    skipped_known = 0
    skipped_unsupported = 0
    skipped_no_detail = 0
    detail_circuit_broken = False
    rate_limited = False
    detail_errors: List[Dict[str, Any]] = []
    skip_set = skip_detail_ids or set()

    def _fetch_go_target(go_url: str, item_id: str) -> str:
        nonlocal detail_circuit_broken, rate_limited
        try:
            r_go = http.get(go_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
            r_go.encoding = "utf-8"
            go_html = r_go.text
            r_go_url = getattr(r_go, "url", go_url) or go_url
            if r_go.status_code == 200 and _is_guangdiu_captcha_page(r_go_url, go_html):
                solved, landed_html, solve_err = _try_solve_captcha(go_url, r_go_url)
                if solved:
                    if landed_html and not _is_guangdiu_captcha_page(go_url, landed_html):
                        go_html = landed_html
                    else:
                        r_go = http.get(go_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
                        r_go.encoding = "utf-8"
                        go_html = r_go.text
                else:
                    detail_circuit_broken = True
                    rate_limited = True
                    detail_errors.append({
                        "item_id": item_id,
                        "error_url": go_url,
                        "http_status": r_go.status_code,
                        "error_type": "CaptchaChallengeDetected",
                        "error": f"go.php 触发滑块验证且自动通过失败: {solve_err}",
                    })
                    return ""

            if r_go.status_code == 200:
                m_var = re.search(
                    r"var\s+[A-Za-z0-9_]+\s*=\s*['\"](https?://[^'\"]+|/[^'\"]+)['\"]",
                    go_html,
                    re.I,
                )
                if m_var:
                    go_target = html_lib.unescape(m_var.group(1)).strip()
                    m_inner_u = re.search(r"[?&]u=(https?%3A%2F%2F[^&\"'\s]+)", go_target, re.I)
                    if m_inner_u:
                        go_target = unquote(m_inner_u.group(1))
                    elif go_target.startswith("/"):
                        go_target = urljoin(base, go_target)
                    return go_target
            else:
                if r_go.status_code in _RATE_LIMIT_STATUSES:
                    detail_circuit_broken = True
                    rate_limited = True
                detail_errors.append({
                    "item_id": item_id,
                    "error_url": go_url,
                    "http_status": r_go.status_code,
                    "error_type": f"GoRedirectHTTP_{r_go.status_code}",
                    "error": f"go.php 返回状态码: {r_go.status_code}",
                })
        except Exception as e_go:
            detail_errors.append({
                "item_id": item_id,
                "error_url": go_url,
                "http_status": None,
                "error_type": type(e_go).__name__,
                "error": str(e_go)[:160],
            })
        return ""

    for blk in blocks:
        list_pub_time = ""
        list_buy_url = ""
        if is_rank or "hourrankitem" in blk:
            m_id = re.search(r'detail\.php\?id=(\d+)', blk)
            if not m_id:
                continue
            item_id = m_id.group(1).strip()
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            raw_list_count += 1

            m_title_a = re.search(r'<a[^>]+class=["\'][^"\']*hourranktitle[^"\']*["\'][^>]*>(.*?)</a>', blk, re.S | re.I)
            raw_t = m_title_a.group(1) if m_title_a else ""
            m_emph = re.search(r'<span\s+class=["\']emphricepart["\'][^>]*>(.*?)</span>', raw_t, re.S | re.I)
            price_span = re.sub(r"<[^>]+>", "", html_lib.unescape(m_emph.group(1))).strip() if m_emph else ""
            t_only = re.sub(r'<span\s+class=["\']emphricepart["\'][^>]*>.*?</span>', "", raw_t, flags=re.S | re.I)
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(t_only))).strip()

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blk, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

            m_site = re.search(r'<div\s+class=["\'][^"\']*hourrankfromsite[^"\']*["\'][^>]*>(.*?)</div>', blk, re.S | re.I)
            site_raw = html_lib.unescape(m_site.group(1)).strip() if m_site else ""
            site_parts = [p.strip() for p in re.split(r'[\s\xa0·•-]+', site_raw) if p.strip()]
            mall_name = site_parts[0] if site_parts else ""
            preview_text = f"小时风云榜上榜商品 ({site_raw})" if site_raw else "小时风云榜上榜精选爆款"
        else:
            m_a = re.search(
                r'<a[^>]+class=["\'][^"\']*goodname[^"\']*["\'][^>]*href=["\'](?:https?://guangdiu\.com/)?detail\.php\?id=(\d+)["\'][^>]*>(.*?)</a>',
                blk,
                re.S | re.I,
            )
            if not m_a:
                m_a = re.search(
                    r'<a[^>]+href=["\'](?:https?://guangdiu\.com/)?detail\.php\?id=(\d+)["\'][^>]*class=["\'][^"\']*goodname[^"\']*["\'][^>]*>(.*?)</a>',
                    blk,
                    re.S | re.I,
                )
            if not m_a:
                m_a = re.search(
                    r'<a[^>]+href=["\'](?:https?://guangdiu\.com/)?detail\.php\?id=(\d+)["\'][^>]*>(.*?)</a>',
                    blk,
                    re.S | re.I,
                )
            if not m_a:
                continue
            item_id = m_a.group(1).strip()
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            raw_list_count += 1

            a_inner = m_a.group(2)
            m_emph = re.search(r'<span\s+class=["\']emphricepart["\'][^>]*>(.*?)</span>', a_inner, re.S | re.I)
            price_span = re.sub(r"<[^>]+>", "", html_lib.unescape(m_emph.group(1))).strip() if m_emph else ""
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_lib.unescape(a_inner))).strip()
            if not title:
                m_alt = re.search(r'<img[^>]+alt=["\']([^"\']+)["\']', blk, re.I)
                if m_alt:
                    title = html_lib.unescape(m_alt.group(1)).strip()

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blk, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

            m_mall = re.search(r'class=["\'][^"\']*rightmallname[^"\']*["\'][^>]*>(.*?)</a>', blk, re.S | re.I)
            mall_name = re.sub(r"<[^>]+>", "", html_lib.unescape(m_mall.group(1))).strip() if m_mall else ("拼多多" if channel == "pdd" else "")

            m_time = re.search(r'<span[^>]*class=["\'][^"\']*latesttime[^"\']*["\'][^>]*>(.*?)</span>', blk, re.S | re.I)
            if m_time:
                list_pub_time = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_lib.unescape(m_time.group(1)))).strip()

            m_buy_tag = re.search(r'<a\b[^>]*class=["\'][^"\']*innergototobuybtn[^"\']*["\'][^>]*>', blk, re.S | re.I)
            if m_buy_tag:
                m_href = re.search(r'href=["\']([^"\']+)["\']', m_buy_tag.group(0), re.I)
                if m_href:
                    list_buy_url = _unwrap_known_redirect_url(m_href.group(1))

            m_abs = re.search(r'<(?:div|a)\s+class=["\']abstractcontent["\'][^>]*>(.*?)</(?:div|a)>', blk, re.S | re.I)
            preview_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(m_abs.group(1)))).strip() if m_abs else ""
            preview_text = re.sub(r"\s*完整阅读>\s*$", "", preview_text).strip()

        # 1. 列表层 100% 确定非淘/京/拼平台前置过滤（绝不误杀未知商城名或未知短链）
        list_links_probe = [{"url": list_buy_url}] if list_buy_url else []
        if is_confirmed_unsupported_deal(mall_name=mall_name, links=list_links_probe):
            skipped_unsupported += 1
            continue

        # 2. 已存在详情库中直接跳过，不发送任何详情或 go.php 请求
        if fetch_detail and item_id in skip_set:
            skipped_known += 1
            continue

        detail_url = f"{base}/detail.php?id={item_id}"
        go_url = f"{base}/go.php?id={item_id}"
        channel_label = "逛丢风云榜" if is_rank else ("逛丢热门" if channel == "hots" else (mall_name or "逛丢好价"))

        tutorial_images: List[str] = [img_url] if img_url else []
        extracted_links: List[Dict[str, Any]] = []
        extracted_tokens: List[str] = []
        raw_payload: Any = blk
        pub_time = list_pub_time

        if not fetch_detail:
            go_target = list_buy_url or _fetch_go_target(go_url, item_id)
            if go_target:
                extracted_links.append({
                    "label": "逛丢直达购买链",
                    "url": go_target,
                    "type": "guangdiu_go_target",
                    "platform": detect_platform_signal(f"{go_target} {mall_name}") or "unknown",
                })
            if is_confirmed_unsupported_deal(mall_name=mall_name, links=extracted_links):
                skipped_unsupported += 1
                continue
            go_urls_str = "\n".join(f"直达链接: {lk['url']}" for lk in extracted_links)
            tutorial_text = f"【{channel_label}】{title}\n{preview_text}".strip()
            if go_urls_str:
                tutorial_text = f"{tutorial_text}\n{go_urls_str}".strip()
            rec = build_deal_record(
                source_site="guangdiu",
                source_channel=f"{channel}:{mall_name}" if mall_name else channel,
                source_item_id=item_id,
                source_url=detail_url,
                source_list_page_url=list_url,
                source_detail_url=detail_url,
                title=title,
                price_text=price_span,
                image_url=img_url,
                mall_name=mall_name,
                publish_time=pub_time,
                tutorial_text=tutorial_text,
                tutorial_images=tutorial_images,
                extracted_links=extracted_links,
                extracted_tokens=extracted_tokens,
                raw_payload=raw_payload,
            )
            rec["has_full_detail"] = False
            records.append(rec)
            continue

        # 3. 详情配额与熔断判断（不超额请求，也不硬撞验证码）
        if detail_circuit_broken or detail_attempted >= max_details:
            break

        if detail_attempted > 0 and eff_delay > 0:
            time.sleep(eff_delay * random.uniform(0.85, 1.35))
        detail_attempted += 1

        has_full_detail = False
        dabs_text = ""
        try:
            rd = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
            rd.encoding = "utf-8"
            if rd.status_code != 200:
                if rd.status_code in _RATE_LIMIT_STATUSES:
                    detail_circuit_broken = True
                    rate_limited = True
                skipped_no_detail += 1
                detail_errors.append({
                    "item_id": item_id,
                    "error_url": detail_url,
                    "http_status": rd.status_code,
                    "error_type": f"HTTP_{rd.status_code}",
                    "error": f"详情页返回非200状态码: {rd.status_code}",
                })
                continue

            det_html = rd.text
            raw_u = getattr(rd, "url", None)
            rd_url = str(raw_u).strip() if isinstance(raw_u, str) and str(raw_u).strip() else detail_url
            is_captcha = _is_guangdiu_captcha_page(rd_url, det_html)
            if is_captcha:
                solved, landed_html, solve_err = _try_solve_captcha(detail_url, rd_url)
                if solved:
                    if landed_html and not _is_guangdiu_captcha_page(detail_url, landed_html):
                        det_html = landed_html
                    else:
                        time.sleep(random.uniform(0.3, 0.7))
                        rd_retry = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
                        rd_retry.encoding = "utf-8"
                        retry_url = getattr(rd_retry, "url", detail_url) or detail_url
                        if rd_retry.status_code != 200 or _is_guangdiu_captcha_page(retry_url, rd_retry.text):
                            detail_circuit_broken = True
                            rate_limited = True
                            skipped_no_detail += 1
                            detail_errors.append({
                                "item_id": item_id,
                                "error_url": detail_url,
                                "http_status": rd_retry.status_code,
                                "error_type": "CaptchaRetryFailed",
                                "error": "滑块通过后重拉详情页仍被拦截，触发主动熔断保护",
                            })
                            break
                        det_html = rd_retry.text
                else:
                    detail_circuit_broken = True
                    rate_limited = True
                    skipped_no_detail += 1
                    detail_errors.append({
                        "item_id": item_id,
                        "error_url": rd_url,
                        "http_status": 200,
                        "error_type": "CaptchaChallengeDetected",
                        "error": f"逛丢详情页触发滑块安全验证，自动通过失败（{solve_err}），触发主动熔断保护",
                    })
                    break

            dabs_html = _extract_div_by_id(det_html, "dabstract")
            if not dabs_html:
                skipped_no_detail += 1
                continue

            unwrapped_dabs = _unwrap_guangdiu_to_php(dabs_html)
            raw_payload = unwrapped_dabs
            d_links, step_imgs, d_tokens = extract_links_images_and_tokens(unwrapped_dabs, base_url=detail_url)
            for lk in d_links:
                if not any(x.get("url") == lk.get("url") for x in extracted_links):
                    extracted_links.append(lk)
            for im in step_imgs:
                if im not in tutorial_images:
                    tutorial_images.append(im)
            for tk in d_tokens:
                if tk not in extracted_tokens:
                    extracted_tokens.append(tk)

            dabs_text = _extract_guangdiu_dabstract_text(unwrapped_dabs, detail_url)
            if not _has_substantive_guangdiu_tutorial(dabs_text, title):
                skipped_no_detail += 1
                continue

            m_det_time = re.search(
                r'class=["\'][^"\']*dinfoweibo[^"\']*["\'][^>]*>.*?<span[^>]*class=["\'][^"\']*latesttime[^"\']*["\'][^>]*>(.*?)</span>',
                det_html,
                re.S | re.I,
            )
            if not m_det_time:
                m_det_time = re.search(r'<span[^>]*class=["\'][^"\']*latesttime[^"\']*["\'][^>]*>(.*?)</span>', det_html, re.S | re.I)
            if m_det_time:
                parsed_t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_lib.unescape(m_det_time.group(1)))).strip()
                if parsed_t:
                    pub_time = parsed_t

            has_full_detail = True
        except Exception as e:
            skipped_no_detail += 1
            detail_errors.append({
                "item_id": item_id,
                "error_url": detail_url,
                "http_status": None,
                "error_type": type(e).__name__,
                "error": str(e)[:160],
            })
            continue

        if not has_full_detail:
            skipped_no_detail += 1
            continue

        # 4. 详情确认拿到实质教程后，再请求 go.php 提取最终买单直达链接
        go_target = list_buy_url or _fetch_go_target(go_url, item_id)
        if go_target and not any(lk.get("url") == go_target for lk in extracted_links):
            extracted_links.insert(0, {
                "label": "逛丢直达购买链",
                "url": go_target,
                "type": "guangdiu_go_target",
                "platform": detect_platform_signal(f"{go_target} {mall_name}") or "unknown",
            })

        # 5. 二次确定性平台过滤（防止商城名留空但 go.php 落地为美团/唯品会/亚马逊等非淘京拼外链）
        if is_confirmed_unsupported_deal(mall_name=mall_name, links=extracted_links, tokens=extracted_tokens):
            skipped_unsupported += 1
            continue

        go_urls_str = "\n".join(
            f"直达链接: {lk['url']}" for lk in extracted_links if lk.get("type") == "guangdiu_go_target"
        )
        tutorial_text = f"【{channel_label}】{title}\n{dabs_text}".strip()
        if go_urls_str:
            tutorial_text = f"{tutorial_text}\n{go_urls_str}".strip()

        rec = build_deal_record(
            source_site="guangdiu",
            source_channel=f"{channel}:{mall_name}" if mall_name else channel,
            source_item_id=item_id,
            source_url=detail_url,
            source_list_page_url=list_url,
            source_detail_url=detail_url,
            title=title,
            price_text=price_span,
            image_url=img_url,
            mall_name=mall_name,
            publish_time=pub_time,
            tutorial_text=tutorial_text,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=raw_payload,
        )
        rec["has_full_detail"] = True

        if not has_valid_detail_and_tutorial(rec):
            skipped_no_detail += 1
            logger.warning(
                "  ⚠️ [逛丢-无购买锚点跳过] id=%s | title=%s | 缺少实质教程或淘/京/拼购买链接",
                item_id,
                title[:32],
            )
            continue

        detail_fetched += 1
        records.append(rec)

    if cookie_file:
        _save_guangdiu_cookies(http, cookie_file)

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    is_ok = (raw_list_count > 0 or len(records) > 0) and not (rate_limited and len(records) == 0)
    return make_fetch_result(
        status="success" if is_ok else "error",
        http_status=resp.status_code,
        request_url=list_url,
        elapsed_ms=elapsed_ms,
        records=records,
        raw_list_count=raw_list_count,
        detail_fetched=detail_fetched,
        skipped_known=skipped_known,
        skipped_unsupported=skipped_unsupported,
        skipped_no_detail=skipped_no_detail,
        rate_limited=rate_limited,
        detail_errors=detail_errors,
        captcha_triggered=captcha_triggered,
        captcha_solved=captcha_solved,
    )


# ==============================================================================
# 逛丢滑块验证与详情拉取自测函数
# ==============================================================================
def test_guangdiu_slider_and_fetch(
    test_item_id: str = "30679695",
    channel: str = "latest",
    max_details: int = 3,
    cookie_file: str = DEFAULT_GUANGDIU_COOKIE_FILE,
    headless: bool = False,
    proxy: Optional[str] = None,
    browser_channel: Optional[str] = "chrome",
    bypass_system_proxy: bool = True,
) -> Dict[str, Any]:
    """
    一键验证两件事：
    1. 直接访问 `verify.php?url=%2Fdetail.php%3Fid%3D{test_item_id}` 实测滑块自动拖动是否能过，
       并校验同步回 `requests.Session` 的 Cookie 能否直接拉取 `detail.php` 中的 `#dabstract`；
    2. 调用 `fetch_guangdiu_deals(...)` 实测端到端列表+详情+直达链拉取是否正常产出爆料记录。

    使用建议：
    - 首次在本地调试建议 `headless=False`，可直接观察浏览器自动拖动滑块的完整过程；
    - 服务器/定时任务确认可用后改为 `headless=True`。
    """
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    elif bypass_system_proxy:
        session.trust_env = False

    target_detail_url = f"https://guangdiu.com/detail.php?id={test_item_id}"
    verify_url = f"https://guangdiu.com/verify.php?url={quote(f'/detail.php?id={test_item_id}', safe='')}"

    print("=" * 72)
    print(f"[测试 1/2] 直连逛丢滑块页自动验证: {verify_url}")
    t1 = time.perf_counter()
    solved, landed_html, err_msg = _solve_guangdiu_slider_playwright(
        session,
        target_detail_url,
        verify_url=verify_url,
        user_agent=DEFAULT_HEADERS["User-Agent"],
        timeout=25.0,
        headless=headless,
        proxy=proxy,
        cookie_file=cookie_file,
        browser_channel=browser_channel,
        bypass_system_proxy=bypass_system_proxy,
    )
    t1_ms = round((time.perf_counter() - t1) * 1000, 1)
    cookie_names = [c.name for c in session.cookies]

    requests_detail_ok = False
    requests_has_dabstract = False
    requests_http_status = None
    if solved:
        try:
            r_check = session.get(
                target_detail_url,
                headers={**DEFAULT_HEADERS, "Referer": "https://guangdiu.com/"},
                timeout=10.0,
            )
            r_check.encoding = "utf-8"
            requests_http_status = r_check.status_code
            r_check_url = getattr(r_check, "url", target_detail_url) or target_detail_url
            if r_check.status_code == 200 and not _is_guangdiu_captcha_page(r_check_url, r_check.text):
                requests_detail_ok = True
                requests_has_dabstract = bool(_extract_div_by_id(r_check.text, "dabstract"))
        except Exception as e_chk:
            err_msg = f"滑块通过但 requests 回查失败: {e_chk}"

    slider_report = {
        "solved": solved,
        "elapsed_ms": t1_ms,
        "synced_cookies": cookie_names,
        "landed_has_dabstract": bool(_extract_div_by_id(landed_html, "dabstract")) if landed_html else False,
        "requests_http_status": requests_http_status,
        "requests_detail_ok": requests_detail_ok,
        "requests_has_dabstract": requests_has_dabstract,
        "error": err_msg,
    }
    print(
        f"  -> solved={solved} | 耗时={t1_ms}ms | 同步Cookie={cookie_names} "
        f"| requests直连通过={requests_detail_ok} | 含#dabstract={requests_has_dabstract}"
    )
    if err_msg:
        print(f"  -> 错误信息: {err_msg}")

    if not solved and not requests_detail_ok:
        print("-" * 72)
        print("[提示] 前置安全验证未完成或超时，跳过端到端拉取测试以避免加重站点风控拦截。")
        print("请在浏览器中手动拖动滑块通过一次验证，或将已验证会话 Cookie 导出至 guangdiu_cookies.json 后再试。")
        print("=" * 72)
        print("[最终结论] CHECK - 安全验证未通过，请按提示完成验证后重试")
        return {
            "all_passed": False,
            "slider_test": slider_report,
            "fetch_test": {"status": "skipped", "reason": "captcha_not_solved"},
        }

    print("-" * 72)
    print(f"[测试 2/2] 端到端调用 fetch_guangdiu_deals(channel={channel!r}, max_details={max_details})")
    res = fetch_guangdiu_deals(
        channel=channel,
        page=1,
        fetch_detail=True,
        max_details=max_details,
        detail_delay=1.0,
        session=session,
        cookie_file=cookie_file,
        enable_slider_solver=True,
        solver_headless=headless,
        proxy=proxy,
        browser_channel=browser_channel,
        bypass_system_proxy=bypass_system_proxy,
    )
    print(
        f"  -> status={res['status']} | raw_list={res['raw_list_count']} | detail_fetched={res['detail_fetched']} "
        f"| 有效产出={res['count']} | captcha_triggered={res.get('captcha_triggered')} "
        f"| captcha_solved={res.get('captcha_solved')} | rate_limited={res['rate_limited']}"
    )
    if res.get("detail_errors"):
        print(f"  -> detail_errors: {res['detail_errors']}")
    for idx, rec in enumerate(res.get("records", [])[:3], start=1):
        preview = str(rec.get("tutorial_text") or "").replace("\n", " ")[:100]
        print(
            f"  [样本 {idx}] id={rec['source_item_id']} | platform={rec['platform']} "
            f"| price={rec['price_text']} | links={len(rec.get('extracted_links') or [])} "
            f"| title={rec['title'][:36]}"
        )
        print(f"           tutorial={preview}")

    all_passed = bool(solved and requests_detail_ok and res.get("status") == "success" and res.get("count", 0) > 0)
    print("=" * 72)
    print(f"[最终结论] {'PASS - 滑块验证与详情拉取全部通过' if all_passed else 'CHECK - 请查看上方具体输出排查'}")
    return {
        "all_passed": all_passed,
        "slider_test": slider_report,
        "fetch_test": {
            "status": res.get("status"),
            "raw_list_count": res.get("raw_list_count"),
            "detail_fetched": res.get("detail_fetched"),
            "count": res.get("count"),
            "captcha_triggered": res.get("captcha_triggered"),
            "captcha_solved": res.get("captcha_solved"),
            "rate_limited": res.get("rate_limited"),
            "detail_errors": res.get("detail_errors"),
        },
    }


if __name__ == "__main__":
    test_guangdiu_slider_and_fetch(headless=False)