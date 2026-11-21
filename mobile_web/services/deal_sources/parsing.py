# -*- coding: utf-8 -*-
"""
好价线报通用 HTML 解析、链接/口令抽取与统一标准化记录构造（单一真源）。
================================================================================
- 平台信号、淘口令与京东主图哈希统一复用 `models.product`；
- 通用平衡标签抽取器 (`extract_balanced_tag`) 精确隔离单品正文与全站公共广告栏，
  彻底杜绝“把边栏广告或全局红包横幅当作商品链接”的串货隐患。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from typing import Any, Collection, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from models.product import (
    detect_platform_signal,
    extract_all_tokens,
    extract_jd_image_key,
    normalize_platform,
)

from .filters import is_host_match

_EXPLICIT_YUAN_RE = re.compile(
    r"(?:(?:￥|¥|💰)\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:元|r\b|R\b|块|亓))",
    re.IGNORECASE,
)

_RAW_URL_RE = re.compile(r"https?://[^\s<>\"'\]）】]+", re.IGNORECASE)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_html_tags(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", html_lib.unescape(str(text)))
    return re.sub(r"\s+", " ", cleaned).strip()


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
        m_bracket = re.search(r"【\s*(?:券后|到手|实付|仅|约)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|r|R|块|亓|包邮|起)?\s*】", raw)
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


def _host_matches_any(url_str: str, excluded_hosts: Collection[str]) -> bool:
    if not excluded_hosts:
        return False
    try:
        host = (urlsplit(url_str).hostname or "").strip().lower()
    except Exception:
        return False
    if not host:
        return False
    return any(is_host_match(host, dom) for dom in excluded_hosts)


def _is_truncated_url_display(text: str, full_url: str) -> bool:
    """判断 <a> 锚点展示文本是否为同一 href 完整 URL 的省略/截断前缀。"""
    cleaned = re.sub(r"\s+", "", html_lib.unescape(str(text or ""))).rstrip("….")
    target = str(full_url or "").strip()
    return bool(
        cleaned.lower().startswith(("http://", "https://"))
        and len(cleaned) >= 12
        and len(target) > len(cleaned)
        and target.startswith(cleaned)
    )


def extract_links_images_and_tokens(
    raw_html: str,
    base_url: str = "",
    *,
    exclude_hosts: Optional[Collection[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """从 HTML 片段或纯文本中无损提取链接、图片 URL 与口令/加密 ID。"""
    html_str = str(raw_html or "")
    links: List[Dict[str, Any]] = []
    seen_urls: set = set()
    excl = tuple(h.lower() for h in (exclude_hosts or ()))

    for m_a in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html_str, re.S | re.I):
        attrs, inner = m_a.group(1), m_a.group(2)
        anchor_text = strip_html_tags(inner)

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
        if excl and _host_matches_any(full_url, excl):
            continue
        if full_url not in seen_urls:
            seen_urls.add(full_url)
            links.append({
                "label": anchor_text or "链接",
                "url": full_url,
                "type": "anchor",
                "platform": detect_platform_signal(full_url) or detect_platform_signal(anchor_text) or "unknown",
            })

    def _drop_truncated_anchor_text(match: re.Match) -> str:
        attrs, inner = match.group(1), match.group(2)
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if href_m:
            raw_href = html_lib.unescape(href_m.group(1)).strip()
            if raw_href and not raw_href.startswith(("javascript:", "#", "mailto:")):
                full_url = urljoin(base_url, raw_href) if base_url else raw_href
                a_text = strip_html_tags(inner)
                if _is_truncated_url_display(a_text, full_url) or _is_truncated_url_display(a_text, raw_href):
                    return " "
        return inner

    raw_url_html = re.sub(r"<a\b([^>]*)>(.*?)</a>", _drop_truncated_anchor_text, html_str, flags=re.S | re.I)
    stripped_tags = re.sub(r"<[^>]+>", " ", raw_url_html)
    for m_url in _RAW_URL_RE.finditer(html_lib.unescape(stripped_tags)):
        u = m_url.group(0).rstrip(".,;，。；!！…")
        if excl and _host_matches_any(u, excl):
            continue
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
        for attr_name in ("data-src", "data-original", "data-url", "src"):
            m_attr = re.search(rf'{attr_name}=["\']([^"\']+)["\']', attrs, re.I)
            if m_attr and m_attr.group(1).strip() and not m_attr.group(1).strip().startswith("data:"):
                val = html_lib.unescape(m_attr.group(1).strip())
                if "GIF16.gif" in val or "loading.png" in val or "loading_" in val:
                    continue
                img_candidate = val
                break
        if img_candidate:
            if img_candidate.startswith("//"):
                img_candidate = "https:" + img_candidate
            elif base_url:
                img_candidate = urljoin(base_url, img_candidate)
            if img_candidate not in images:
                images.append(img_candidate)

    tokens = extract_all_tokens(html_lib.unescape(stripped_tags))

    return links, images, tokens


def html_to_tutorial_text(raw_html: str, base_url: str = "", *, keep_images: bool = True) -> str:
    """将包含购买/凑单教程的 HTML 转换为保留链接与换行结构的结构化文本。"""
    if not raw_html:
        return ""
    work = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", str(raw_html), flags=re.S | re.I)

    def _replace_anchor(match: re.Match) -> str:
        attrs, inner = match.group(1), match.group(2)
        text = strip_html_tags(inner)
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
        if _is_truncated_url_display(text, full_url) or _is_truncated_url_display(text, raw_href):
            return full_url
        return f"{text} ({full_url})"

    def _replace_img(match: re.Match) -> str:
        if not keep_images:
            return ""
        attrs = match.group(1)
        for attr_name in ("data-src", "data-original", "data-url", "src"):
            m_attr = re.search(rf'{attr_name}=["\']([^"\']+)["\']', attrs, re.I)
            if m_attr and m_attr.group(1).strip() and not m_attr.group(1).strip().startswith("data:"):
                u = html_lib.unescape(m_attr.group(1).strip())
                if "GIF16.gif" in u or "loading.png" in u or "loading_" in u:
                    continue
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


def extract_balanced_tag(html_text: str, tag_name: str, attr_pattern: str = "") -> str:
    """平衡匹配指定标签及属性条件的完整 `<tag>...</tag>` 子树，防止嵌套标签提前截断。"""
    if not html_text:
        return ""
    if attr_pattern:
        start_re = re.compile(rf"<{tag_name}\b[^>]*{attr_pattern}[^>]*>", re.IGNORECASE | re.S)
    else:
        start_re = re.compile(rf"<{tag_name}\b[^>]*>", re.IGNORECASE | re.S)
    m_start = start_re.search(html_text)
    if not m_start:
        return ""
    start_idx = m_start.start()
    depth = 0
    tag_re = re.compile(rf"<(/?){tag_name}\b[^>]*>", re.IGNORECASE)
    for m_tag in tag_re.finditer(html_text[start_idx:]):
        if m_tag.group(1) == "/":
            depth -= 1
            if depth == 0:
                return html_text[start_idx : start_idx + m_tag.end()]
        else:
            depth += 1
    return html_text[start_idx:]


def extract_div_by_id(html_text: str, div_id: str) -> str:
    """平衡匹配指定 `id` 的完整 `<div>...</div>` 子树。"""
    return extract_balanced_tag(html_text, "div", rf'\bid=["\']{re.escape(div_id)}["\']')


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
    has_full_detail: bool = True,
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
            " ".join(str(l.get("url") or "") for l in links),
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
        "has_full_detail": bool(has_full_detail),
    }
