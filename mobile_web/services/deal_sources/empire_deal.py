# -*- coding: utf-8 -*-
"""
数据源 9 & 10：发现值得买 (`http://www.kiees.com`) 与 牛杂网 (`https://www.niuza.com`)
================================================================================
两站采用完全一致的 EmpireCMS + WordPress 主题模板，因此抽象为共享基类 `EmpireDealSource`：
- `mode="index"` -> `{base_url}/index.html`（抓取首页列表，`<div class="postbox">`）
- `mode="hot_1"` -> `{base_url}/hot_1` -> `/hot_1/p2` .. 直至无 `<li class="list">` 为止
- 实证关键特性：
  1. 两站均会对 2019~2025 年老爆款再次顶置发车，因此按要求以本次拉取时间作为 `publish_time`；
  2. 两站老旧服务器 Keep-Alive 易主动断连（`RemoteDisconnected`），默认启用 `close_connection=True`
     并在单次瞬断时优雅重试 1 次；
  3. 列表页卡片内已含 `imlink_gobuy` 直达推广链，先行按此做非淘京拼过滤与 `known_ids` 去重；
  4. 详情页仅解析 `<body>` 内 `<div class="postsingle">`（含 `#shop_url` / `.single_gobuy` 买链与
     `<div class="entry-content">` 完整优惠步骤与领券文案），严禁扫描边栏或页面头部导航。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

from models.product import detect_platform_signal

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
    utc_now_iso,
)


def _parse_empire_index_cards(html_text: str, base_url: str) -> List[Dict[str, Any]]:
    wrap = html_text.split('id="home-left"', 1)[-1] if 'id="home-left"' in html_text else html_text
    wrap = wrap.split('id="sidebar"', 1)[0]
    boxes = re.findall(
        r'<div\s+class=["\']postbox["\'][^>]*>(.*?)(?=<div\s+class=["\']postbox["\']|<div\s+class=["\']wp-pagenavi|$)',
        wrap,
        re.S | re.I,
    )
    items: List[Dict[str, Any]] = []
    for blk in boxes:
        m_a = re.search(r'<a\b[^>]*href=["\']((?:https?://[^"\']+)?/\d{4}/\d{2}/\d{2}/(\d+)\.html)["\']', blk, re.I)
        if not m_a:
            continue
        detail_url = urljoin(base_url, html_lib.unescape(m_a.group(1)).strip())
        item_id = m_a.group(2).strip()

        m_h2 = re.search(r"<h2[^>]*>(.*?)</h2>", blk, re.S | re.I)
        price_text = ""
        title = ""
        if m_h2:
            h2_inner = m_h2.group(1)
            m_span = re.search(r"<span[^>]*>(.*?)</span>", h2_inner, re.S | re.I)
            price_text = strip_html_tags(m_span.group(1)) if m_span else ""
            title = strip_html_tags(h2_inner)

        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blk, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

        m_buy = re.search(
            r'<a\b[^>]*class=["\'][^"\']*imlink_gobuy[^"\']*["\'][^>]*href=["\']([^"\']+)["\']|<a\b[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*imlink_gobuy',
            blk,
            re.S | re.I,
        )
        list_buy_url = ""
        if m_buy:
            list_buy_url = html_lib.unescape(m_buy.group(1) or m_buy.group(2) or "").strip()

        items.append({
            "item_id": item_id,
            "detail_url": detail_url,
            "title": title,
            "price_text": price_text,
            "image_url": img_url,
            "list_buy_url": list_buy_url,
        })
    return items


def _parse_empire_hot_cards(html_text: str, base_url: str) -> List[Dict[str, Any]]:
    lis = re.findall(r'<li\s+class=["\']list["\'][^>]*>(.*?)</li>', html_text, re.S | re.I)
    items: List[Dict[str, Any]] = []
    for li in lis:
        m_a = re.search(r'<a\b[^>]*href=["\']((?:https?://[^"\']+)?/\d{4}/\d{2}/\d{2}/(\d+)\.html)["\']', li, re.I)
        if not m_a:
            continue
        detail_url = urljoin(base_url, html_lib.unescape(m_a.group(1)).strip())
        item_id = m_a.group(2).strip()

        m_h2 = re.search(r"<h2[^>]*>(.*?)</h2>", li, re.S | re.I)
        price_text = ""
        title = ""
        if m_h2:
            h2_inner = m_h2.group(1)
            m_span = re.search(r"<span[^>]*>(.*?)</span>", h2_inner, re.S | re.I)
            price_text = strip_html_tags(m_span.group(1)) if m_span else ""
            title = strip_html_tags(h2_inner)

        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', li, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

        m_buy = re.search(
            r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*imlink_gobuy[^"\']*["\']|<a\b[^>]*class=["\'][^"\']*imlink_gobuy[^"\']*["\'][^>]*href=["\']([^"\']+)["\']',
            li,
            re.S | re.I,
        )
        list_buy_url = ""
        if m_buy:
            list_buy_url = html_lib.unescape(m_buy.group(1) or m_buy.group(2) or "").strip()

        items.append({
            "item_id": item_id,
            "detail_url": detail_url,
            "title": title,
            "price_text": price_text,
            "image_url": img_url,
            "list_buy_url": list_buy_url,
        })
    return items


class EmpireDealSource(BaseDealSource):
    """发现值得买 (`kiees`) 与 牛杂网 (`niuza`) 共享抓取实现。"""

    base_url: str = ""
    close_connection: bool = True

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        params = ch.params or {}
        mode = str(params.get("mode") or "index").strip().lower()
        base = str(params.get("base_url") or self.base_url).rstrip("/")
        max_pages = 1 if mode == "index" else max(1, int(ch.max_pages or 8))
        full_sweep = ch.fetch_mode == "full_sweep"
        max_details = ctx.effective_max_details
        skip_set = ctx.known_ids

        all_cards: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        pages_crawled = 0
        rate_limited = False
        reached_end = False
        last_http_status: Optional[int] = None
        primary_url = f"{base}/index.html" if mode == "index" else f"{base}/hot_1"
        t0 = time.perf_counter()

        for page in range(1, max_pages + 1):
            if mode == "index":
                page_url = f"{base}/index.html" if page == 1 else f"{base}/index{page}.html"
            else:
                page_url = f"{base}/hot_1" if page == 1 else f"{base}/hot_1/p{page}"

            resp = None
            try:
                resp = request_with_retry(ctx.session, "get", page_url, headers=DEFAULT_HEADERS, timeout=ctx.timeout)
                last_http_status = resp.status_code
                resp.encoding = "utf-8"
                if resp.status_code != 200:
                    if is_rate_limit_status(resp.status_code):
                        rate_limited = True
                    if pages_crawled == 0:
                        return FetchResult(
                            status="error",
                            http_status=resp.status_code,
                            request_url=page_url,
                            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                            rate_limited=rate_limited,
                            detail_errors=[{"error": f"HTTP {resp.status_code}", "http_status": resp.status_code}],
                        )
                    break
                html_text = resp.text
            except Exception as exc:
                if pages_crawled == 0:
                    return FetchResult(
                        status="error",
                        http_status=getattr(resp, "status_code", None),
                        request_url=page_url,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                        detail_errors=[{"error": str(exc), "error_type": type(exc).__name__}],
                    )
                break

            pages_crawled += 1
            parsed = _parse_empire_index_cards(html_text, base) if mode == "index" else _parse_empire_hot_cards(html_text, base)
            if not parsed:
                reached_end = True
                break

            unseen_on_page = 0
            for card in parsed:
                cid = card["item_id"]
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                unseen_on_page += 1
                all_cards.append(card)
            if unseen_on_page == 0:
                reached_end = True
                break
            if mode == "index" and page >= max_pages:
                reached_end = True
                break

        records: List[Dict[str, Any]] = []
        touch_ids: Set[str] = set()
        detail_errors: List[Dict[str, Any]] = []
        raw_list_count = len(all_cards)
        detail_fetched = 0
        detail_attempted = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        detail_circuit_broken = False

        for card in all_cards:
            item_id = card["item_id"]
            list_buy_url = card.get("list_buy_url") or ""

            if list_buy_url and is_confirmed_unsupported_deal(links=[{"url": list_buy_url}]):
                skipped_unsupported += 1
                continue

            if full_sweep:
                touch_ids.add(item_id)

            if item_id in skip_set:
                skipped_known += 1
                continue

            if detail_circuit_broken or detail_attempted >= max_details:
                continue

            if detail_attempted > 0:
                self.sleep_detail_delay(ctx)
            detail_attempted += 1

            detail_url = card["detail_url"]
            title = card["title"]
            price_text = card["price_text"]
            img_url = card["image_url"]

            try:
                rd = request_with_retry(
                    ctx.session,
                    "get",
                    detail_url,
                    headers={**DEFAULT_HEADERS, "Referer": primary_url},
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
                body_part = det_html.split("<body", 1)[-1] if "<body" in det_html else det_html
                post_html = extract_balanced_tag(body_part, "div", r'\bclass=["\'][^"\']*postsingle[^"\']*["\']')
                if not post_html:
                    post_html = extract_balanced_tag(body_part, "div", r'\bclass=["\'][^"\']*entry\b')
                if not post_html:
                    skipped_no_detail += 1
                    continue

                # 提取购买按钮 (#shop_url 或 .single_gobuy)
                det_buy_links: List[Dict[str, Any]] = []
                for m_gobuy in re.finditer(
                    r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
                    post_html,
                    re.S | re.I,
                ):
                    full_a = m_gobuy.group(0)
                    href = html_lib.unescape(m_gobuy.group(1)).strip()
                    a_txt = strip_html_tags(m_gobuy.group(2))
                    if any(k in full_a for k in ("tracklink", "imlink_gobuy")) or any(
                        k in a_txt for k in ("购买链接", "去购买", "领券", "直达")
                    ):
                        if not any(h in href.lower() for h in (self.site_key,)):
                            det_buy_links.append({
                                "label": a_txt or "购买链接",
                                "url": href,
                                "type": "buy_url",
                                "platform": detect_platform_signal(href) or "unknown",
                            })

                # 提取正文内容区域 .entry-content
                ec_html = extract_balanced_tag(post_html, "div", r'\bclass=["\'][^"\']*entry-content[^"\']*["\']')
                if not ec_html:
                    ec_html = post_html
                ec_links, ec_imgs, ec_tokens = extract_links_images_and_tokens(
                    ec_html,
                    base_url=detail_url,
                    exclude_hosts=SOURCE_SELF_HOSTS,
                )
                ec_text = html_to_tutorial_text(ec_html, base_url=detail_url).strip()

                merged_links: List[Dict[str, Any]] = []
                seen_urls: Set[str] = set()
                for lk in [*det_buy_links, *ec_links]:
                    u = str(lk.get("url") or "").strip()
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        merged_links.append(lk)
                if list_buy_url and list_buy_url not in seen_urls:
                    merged_links.append({
                        "label": "列表购买链接",
                        "url": list_buy_url,
                        "type": "list_buy_url",
                        "platform": detect_platform_signal(list_buy_url) or "unknown",
                    })

                all_imgs: List[str] = [img_url] if img_url else []
                for im in ec_imgs:
                    if im and im not in all_imgs:
                        all_imgs.append(im)

                if is_confirmed_unsupported_deal(links=merged_links, tokens=ec_tokens):
                    skipped_unsupported += 1
                    continue

                buy_summary = "\n".join(f"购买链接: {lk['url']}" for lk in det_buy_links[:2])
                tutorial_text = f"{ec_text}\n{buy_summary}".strip() if buy_summary else ec_text

                rec = build_deal_record(
                    source_site=self.site_key,
                    source_channel=ch.name,
                    source_item_id=item_id,
                    source_url=detail_url,
                    source_list_page_url=primary_url,
                    source_detail_url=detail_url,
                    title=title or f"{self.site_key} #{item_id}",
                    price_text=price_text,
                    image_url=img_url,
                    publish_time=utc_now_iso(),
                    hot_metrics={"mode": mode},
                    tutorial_text=tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=merged_links,
                    extracted_tokens=ec_tokens,
                    raw_payload=ec_html,
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
        is_ok = pages_crawled > 0 and raw_list_count > 0 and not (rate_limited and len(records) == 0 and len(touch_ids) == 0)
        return FetchResult(
            status="ok" if is_ok else "error",
            http_status=last_http_status or 200,
            request_url=primary_url,
            elapsed_ms=elapsed_ms,
            records=records,
            raw_list_count=raw_list_count,
            detail_fetched=detail_fetched,
            skipped_known=skipped_known,
            skipped_unsupported=skipped_unsupported,
            skipped_no_detail=skipped_no_detail,
            pages_crawled=pages_crawled,
            touch_ids=sorted(touch_ids),
            sweep_completed=bool(full_sweep and reached_end and not rate_limited),
            rate_limited=rate_limited,
            detail_errors=detail_errors,
        )


class KieesSource(EmpireDealSource):
    site_key = "kiees"
    source_key = "kiees"
    base_url = "http://www.kiees.com"
    troubleshooting = {
        "endpoint": "http://www.kiees.com/index.html | http://www.kiees.com/hot_1",
        "http_403_429": "发现值得买返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "发现值得买未解析到商品卡片：请检查站点模板结构。",
        "timeout_or_conn": "发现值得买连接重置/超时：已启用 Connection: close 与自动重试。",
    }


class NiuzaSource(EmpireDealSource):
    site_key = "niuza"
    source_key = "niuza"
    base_url = "https://www.niuza.com"
    troubleshooting = {
        "endpoint": "https://www.niuza.com/index.html | https://www.niuza.com/hot_1",
        "http_403_429": "牛杂网返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "牛杂网未解析到商品卡片：请检查站点模板结构。",
        "timeout_or_conn": "牛杂网连接重置/超时：已启用 Connection: close 与自动重试。",
    }
