# -*- coding: utf-8 -*-
"""
数据源 8：IT之家辣品 (`https://digi.ithome.com/lapin`)
================================================================================
- 策略：获取 2 天内（`lookback_seconds=172800`）的辣品爆料，每 60 分钟拉取一次；
- 翻页机制：
  1. 首屏 `GET https://digi.ithome.com/lapin` 获取 `<ul class="bl">` 内最新 30 条与精确 `data-ot`；
  2. 若最旧条目仍在 2 天时间窗内，按页面原生的 `POST /category/domainpage?domain=digi&subdomain=lapin&ot={ot_ms}`
     向后加载更多页，直至超出 2 天窗口、无更多数据或到达 `max_pages`；
- 详情解析与防串货：
  1. 严格抽取文章正文 `<div id="paragraph">`；
  2. 剔除 IT之家 App 引流/广告链接 (`go.ruanmei.com`, `ithome.com`) 及尾部 `"广告声明：..."` 免责段落；
  3. 完整保留领券与购买步骤文案、优惠券/二合一/短链 (`uland.taobao.com`, `u.jd.com`) 与商品图；
  4. 严格执行 100% 确定非淘京拼过滤与非空壳门禁。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import (
    SOURCE_SELF_HOSTS,
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
)
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)

_CN_TZ = timezone(timedelta(hours=8))


def _parse_ithome_ot_epoch(ot_str: str) -> Optional[float]:
    s = str(ot_str or "").strip()
    if not s:
        return None
    try:
        # 例: 2026-11-20T22:53:52.4500000+08:00 (Python 3.11 仅支持 6 位微秒，先规整)
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.\d+)?([Zz]|[+-]\d{2}:\d{2})?$", s)
        if m:
            base_part = m.group(1).replace(" ", "T")
            tz_part = m.group(2) or "+08:00"
            if tz_part.upper() == "Z":
                tz_part = "+00:00"
            return datetime.fromisoformat(f"{base_part}{tz_part}").timestamp()
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CN_TZ)
        return dt.timestamp()
    except Exception:
        return None


def _parse_ithome_list_items(html_fragment: str) -> List[Dict[str, Any]]:
    """解析 IT之家列表 `<li>` 卡片条目。"""
    items: List[Dict[str, Any]] = []
    for li in re.findall(r"<li\b[^>]*>.*?</li>", html_fragment, re.S | re.I):
        m_a = re.search(
            r'<a\b[^>]*href=["\']([^"\']*/(\d+)\.htm)["\'][^>]*>(.*?)</a>',
            li,
            re.S | re.I,
        )
        if not m_a:
            continue
        detail_url = html_lib.unescape(m_a.group(1)).strip()
        if detail_url.startswith("//"):
            detail_url = "https:" + detail_url
        elif detail_url.startswith("/"):
            detail_url = urljoin("https://digi.ithome.com", detail_url)
        item_id = m_a.group(2).strip()

        m_title = re.search(r'<a\b[^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)</a>', li, re.S | re.I)
        title = strip_html_tags(m_title.group(1)) if m_title else strip_html_tags(m_a.group(3))

        m_ot = re.search(r'data-ot=["\']([^"\']+)["\']', li, re.I)
        ot_str = m_ot.group(1).strip() if m_ot else ""
        ot_ts = _parse_ithome_ot_epoch(ot_str)

        m_img = re.search(r'<img\b[^>]*\b(?:data-original|src)=["\']([^"\']+)["\']', li, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
        if img_url.startswith("//"):
            img_url = "https:" + img_url

        items.append({
            "item_id": item_id,
            "detail_url": detail_url,
            "title": title,
            "ot_str": ot_str,
            "ot_ts": ot_ts,
            "image_url": img_url,
        })
    return items


def _clean_ithome_paragraph_html(para_html: str) -> str:
    """剔除正文内的广告声明与站内辣品APP下载引导（严格限定单个 <p> 内不跨段吞噬）。"""
    cleaned = re.sub(
        r'<p[^>]*class=["\'][^"\']*ad-tips[^"\']*["\'][^>]*>(?:(?!</p>).)*?</p>',
        "",
        para_html,
        flags=re.S | re.I,
    )
    cleaned = re.sub(
        r"<p[^>]*>\s*广告声明：(?:(?!</p>).)*?</p>",
        "",
        cleaned,
        flags=re.S | re.I,
    )
    cleaned = re.sub(
        r"<p[^>]*>(?:(?!</p>).)*?辣品\s*App(?:(?!</p>).)*?</p>",
        "",
        cleaned,
        flags=re.S | re.I,
    )
    cleaned = re.sub(
        r"<p[^>]*>(?:(?!</p>).)*?(?:京东|淘宝|天猫)[^<]{0,20}无门槛红包(?:(?!</p>).)*?</p>",
        "",
        cleaned,
        flags=re.S | re.I,
    )
    return cleaned


class IthomeLapinSource(BaseDealSource):
    site_key = "ithome_lapin"
    source_key = "ithome_lapin"
    troubleshooting = {
        "endpoint": "https://digi.ithome.com/lapin | POST /category/domainpage?domain=digi&subdomain=lapin",
        "http_403_429": "IT之家返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "IT之家辣品列表解析为 0：请核对 <ul class='bl'> 结构。",
        "timeout_or_conn": "IT之家请求超时：下轮将自动重试。",
    }

    LIST_URL = "https://digi.ithome.com/lapin"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        list_url = str(ch.default_url or self.LIST_URL)
        max_pages = max(1, int(ch.max_pages or 10))
        max_details = ctx.effective_max_details
        cutoff_ts = ctx.cutoff_ts
        skip_set = ctx.known_ids

        t0 = time.perf_counter()
        resp = None
        try:
            resp = request_with_retry(ctx.session, "get", list_url, headers=DEFAULT_HEADERS, timeout=ctx.timeout)
            resp.encoding = "utf-8"
            if resp.status_code != 200:
                return FetchResult(
                    status="error",
                    http_status=resp.status_code,
                    request_url=list_url,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                    rate_limited=is_rate_limit_status(resp.status_code),
                    detail_errors=[{"error": f"HTTP {resp.status_code}", "http_status": resp.status_code}],
                )
            first_html = resp.text
        except Exception as exc:
            return FetchResult(
                status="error",
                http_status=getattr(resp, "status_code", None),
                request_url=list_url,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                detail_errors=[{"error": str(exc), "error_type": type(exc).__name__}],
            )

        pages_crawled = 1
        bl_fragment = extract_balanced_tag(first_html, "ul", r'\bclass=["\'][^"\']*\bbl\b') or first_html
        collected: List[Dict[str, Any]] = _parse_ithome_list_items(bl_fragment)
        seen_list_ids: Set[str] = {it["item_id"] for it in collected}

        # 时间窗口向后加载更多页
        while pages_crawled < max_pages and collected:
            last_ot_ts = collected[-1].get("ot_ts")
            if last_ot_ts is None:
                break
            if cutoff_ts is not None and last_ot_ts < cutoff_ts:
                break
            ot_ms = int(last_ot_ts * 1000)
            more_url = f"https://digi.ithome.com/category/domainpage?domain=digi&subdomain=lapin&ot={ot_ms}"
            try:
                r_more = request_with_retry(
                    ctx.session,
                    "post",
                    more_url,
                    headers={
                        **DEFAULT_HEADERS,
                        "Referer": list_url,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=ctx.timeout,
                )
                if r_more.status_code != 200:
                    break
                payload = r_more.json()
                if not isinstance(payload, dict) or not payload.get("success"):
                    break
                content = payload.get("content") or {}
                cnt = int(content.get("count") or 0)
                more_html = str(content.get("html") or "")
                if cnt <= 0 or not more_html:
                    break
                pages_crawled += 1
                more_items = _parse_ithome_list_items(more_html)
                added = 0
                for mi in more_items:
                    if mi["item_id"] not in seen_list_ids:
                        seen_list_ids.add(mi["item_id"])
                        collected.append(mi)
                        added += 1
                if added == 0:
                    break
            except Exception:
                break

        records: List[Dict[str, Any]] = []
        detail_errors: List[Dict[str, Any]] = []
        raw_list_count = len(collected)
        detail_fetched = 0
        detail_attempted = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        skipped_out_of_window = 0
        detail_circuit_broken = False
        rate_limited = False

        for entry in collected:
            item_id = entry["item_id"]
            ot_ts = entry.get("ot_ts")
            if cutoff_ts is not None and ot_ts is not None and ot_ts < cutoff_ts:
                skipped_out_of_window += 1
                continue

            if item_id in skip_set:
                skipped_known += 1
                continue

            if detail_circuit_broken or detail_attempted >= max_details:
                break

            if detail_attempted > 0:
                self.sleep_detail_delay(ctx)
            detail_attempted += 1

            detail_url = entry["detail_url"]
            title = entry["title"]
            img_url = entry["image_url"]
            pub_time = entry["ot_str"]

            try:
                rd = request_with_retry(
                    ctx.session,
                    "get",
                    detail_url,
                    headers={**DEFAULT_HEADERS, "Referer": list_url},
                    timeout=ctx.timeout,
                )
                rd.encoding = "utf-8"
                if rd.status_code != 200:
                    if is_rate_limit_status(rd.status_code):
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
                para_html = extract_balanced_tag(det_html, "div", r'\bid=["\']paragraph["\']')
                if not para_html:
                    skipped_no_detail += 1
                    continue

                clean_para = _clean_ithome_paragraph_html(para_html)
                links, det_imgs, tokens = extract_links_images_and_tokens(
                    clean_para,
                    base_url=detail_url,
                    exclude_hosts=SOURCE_SELF_HOSTS,
                )
                tutorial_text = html_to_tutorial_text(clean_para, base_url=detail_url).strip()
                # 兜底剥除行内广告声明尾巴
                tutorial_text = re.sub(r"\n?广告声明：.*$", "", tutorial_text, flags=re.S).strip()

                all_imgs: List[str] = [img_url] if img_url else []
                for im in det_imgs:
                    if im and im not in all_imgs:
                        all_imgs.append(im)

                if is_confirmed_unsupported_deal(links=links, tokens=tokens):
                    skipped_unsupported += 1
                    continue

                rec = build_deal_record(
                    source_site="ithome_lapin",
                    source_channel=ch.name,
                    source_item_id=item_id,
                    source_url=detail_url,
                    source_list_page_url=list_url,
                    source_detail_url=detail_url,
                    title=title or f"IT之家辣品 #{item_id}",
                    image_url=img_url,
                    publish_time=pub_time,
                    tutorial_text=tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=links,
                    extracted_tokens=tokens,
                    raw_payload=clean_para,
                    has_full_detail=True,
                )
                if not has_valid_detail_and_tutorial(rec):
                    skipped_no_detail += 1
                    continue

                detail_fetched += 1
                records.append(rec)
            except Exception as exc:
                skipped_no_detail += 1
                detail_errors.append({
                    "item_id": item_id,
                    "error_url": detail_url,
                    "http_status": None,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                })

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = pages_crawled > 0 and raw_list_count > 0 and not (rate_limited and len(records) == 0)
        return FetchResult(
            status="ok" if is_ok else "error",
            http_status=resp.status_code,
            request_url=list_url,
            elapsed_ms=elapsed_ms,
            records=records,
            raw_list_count=raw_list_count,
            detail_fetched=detail_fetched,
            skipped_known=skipped_known,
            skipped_unsupported=skipped_unsupported,
            skipped_no_detail=skipped_no_detail,
            skipped_out_of_window=skipped_out_of_window,
            pages_crawled=pages_crawled,
            rate_limited=rate_limited,
            detail_errors=detail_errors,
        )
