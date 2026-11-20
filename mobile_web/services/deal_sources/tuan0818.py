# -*- coding: utf-8 -*-
"""
数据源 5：0818团 (`http://www.0818tuan.com/list-1-0.html`)
================================================================================
- 策略：仅抓首页单页爆料列表，1 小时执行一次；
- 先按列表 `known_ids` 去重再抓 `/xbhd/{id}.html` 详情；
- 剥离站内收藏/上下篇导航，只保留正文教程、图片与外部电商链接；
- 严格执行平台确定性过滤与非空壳门禁。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any, Dict, List, Set
from urllib.parse import urljoin

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import has_valid_detail_and_tutorial, is_confirmed_unsupported_deal
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .parsing import (
    build_deal_record,
    extract_div_by_id,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)


class Tuan0818Source(BaseDealSource):
    site_key = "0818tuan"
    source_key = "0818tuan"
    troubleshooting = {
        "endpoint": "http://www.0818tuan.com/list-1-0.html",
        "http_403_429": "0818团返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "0818团未解析到 /xbhd/ 条目：请核对列表页面结构。",
        "timeout_or_conn": "0818团请求超时：下轮将自动重试。",
    }

    BASE_URL = "http://www.0818tuan.com"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        list_url = str(ch.default_url or f"{self.BASE_URL}/list-1-0.html")
        max_details = ctx.effective_max_details
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
            list_html = resp.text
        except Exception as exc:
            return FetchResult(
                status="error",
                http_status=getattr(resp, "status_code", None),
                request_url=list_url,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                detail_errors=[{"error": str(exc), "error_type": type(exc).__name__}],
            )

        anchors = re.findall(
            r'(<a\b[^>]*href=["\'](/xbhd/(\d+)\.html)["\'][^>]*>.*?</a>)',
            list_html,
            re.S | re.I,
        )

        records: List[Dict[str, Any]] = []
        detail_errors: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        raw_list_count = 0
        detail_fetched = 0
        detail_attempted = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        detail_circuit_broken = False
        rate_limited = False

        for full_a, rel_href, item_id in anchors:
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            raw_list_count += 1

            if item_id in skip_set:
                skipped_known += 1
                continue

            if detail_circuit_broken or detail_attempted >= max_details:
                break

            if detail_attempted > 0:
                self.sleep_detail_delay(ctx)
            detail_attempted += 1

            detail_url = urljoin(self.BASE_URL, rel_href)
            time_m = re.search(r'<span[^>]*class=["\'][^"\']*badge[^"\']*["\'][^>]*>(.*?)</span>', full_a, re.S | re.I)
            pub_time = strip_html_tags(time_m.group(1)) if time_m else ""
            title_m = re.search(r'title=["\']([^"\']+)["\']', full_a, re.I)
            if title_m:
                title = html_lib.unescape(title_m.group(1)).strip()
            else:
                no_span = re.sub(r"<span[^>]*>.*?</span>", "", full_a, flags=re.S | re.I)
                title = strip_html_tags(no_span)

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
                m_full_time = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)", det_html)
                if m_full_time:
                    pub_time = m_full_time.group(1)

                xb_fragment = extract_div_by_id(det_html, "xbcontent")
                if not xb_fragment:
                    m_post = re.search(
                        r'(<div[^>]*class=["\'][^"\']*post-content[^"\']*["\'][^>]*>.*?</div>)',
                        det_html,
                        re.S | re.I,
                    )
                    xb_fragment = m_post.group(1) if m_post else ""

                if not xb_fragment:
                    skipped_no_detail += 1
                    continue

                clean_xb = re.sub(
                    r"<p[^>]*>\s*(?:<a[^>]*href=[\"'][^\"']*member/fava[^\"']*[\"'][^>]*>.*?</a>|上一篇：.*?|下一篇：.*?)\s*</p>",
                    "",
                    xb_fragment,
                    flags=re.S | re.I,
                )
                links, images, tokens = extract_links_images_and_tokens(clean_xb, base_url=detail_url)
                tutorial_text = html_to_tutorial_text(clean_xb, base_url=detail_url)

                if is_confirmed_unsupported_deal(links=links, tokens=tokens):
                    skipped_unsupported += 1
                    continue

                rec = build_deal_record(
                    source_site="0818tuan",
                    source_channel=ch.name,
                    source_item_id=item_id,
                    source_url=detail_url,
                    source_list_page_url=list_url,
                    source_detail_url=detail_url,
                    title=title or f"0818团线报 #{item_id}",
                    publish_time=pub_time,
                    tutorial_text=tutorial_text,
                    tutorial_images=images,
                    extracted_links=links,
                    extracted_tokens=tokens,
                    raw_payload=xb_fragment,
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
        is_ok = raw_list_count > 0 and not (rate_limited and len(records) == 0)
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
            pages_crawled=1,
            rate_limited=rate_limited,
            detail_errors=detail_errors,
        )
