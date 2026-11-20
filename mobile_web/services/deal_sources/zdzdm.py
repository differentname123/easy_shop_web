# -*- coding: utf-8 -*-
"""
数据源 11：真的值得买国内优惠 (`https://www.zdzdm.com/youhui`)
================================================================================
- 策略：仅获取第一页（20 条）商品，每 30 分钟拉取一次；
- 防串货关键隔离：
  详情页右侧边栏存在全站推广淘宝链接，因此本插件【严禁】扫描整页 HTML，
  必须使用 `extract_balanced_tag(..., "article")` 仅截取 `<article>...</article>` 主体：
  - 从 `<span class="zd-article-origin">来源：...</span>` 提取精确商城并做非淘京拼过滤；
  - 从 `<time datetime="...">` 提取准确发布时间；
  - 从 `<div class="zd-article-actions">` 提取领券 (`优惠券>`) 与购买 (`前往购买>`) 按钮链接；
  - 从 `<div class="zd-rich">` 提取完整优惠描述文案（并剔除站内“是一群网购爱好者”固定页脚声明）。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any, Dict, List, Set
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


class ZdzdmSource(BaseDealSource):
    site_key = "zdzdm"
    source_key = "zdzdm"
    troubleshooting = {
        "endpoint": "https://www.zdzdm.com/youhui",
        "http_403_429": "真的值得买返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "真的值得买未解析到 .zd-deal 条目：请检查首页模板。",
        "timeout_or_conn": "真的值得买请求超时：下轮将自动重试。",
    }

    BASE_URL = "https://www.zdzdm.com"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        list_url = str(ch.default_url or f"{self.BASE_URL}/youhui")
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

        cards = re.findall(r'<article\s+class=["\']zd-deal["\'][^>]*>(.*?)</article>', list_html, re.S | re.I)

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

        for card_html in cards:
            m_href = re.search(r'href=["\'](/p/(\d+))["\']', card_html, re.I)
            if not m_href:
                continue
            rel_href = m_href.group(1).strip()
            item_id = m_href.group(2).strip()
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
            m_title = re.search(r'<h2[^>]*class=["\'][^"\']*zd-deal-title[^"\']*["\'][^>]*>(.*?)</h2>', card_html, re.S | re.I)
            price_text = ""
            title = ""
            if m_title:
                h2_inner = m_title.group(1)
                m_p = re.search(r'<span[^>]*class=["\'][^"\']*zd-content-price[^"\']*["\'][^>]*>(.*?)</span>', h2_inner, re.S | re.I)
                price_text = strip_html_tags(m_p.group(1)) if m_p else ""
                t_wo_p = re.sub(r'<span[^>]*class=["\'][^"\']*zd-content-price[^"\']*["\'][^>]*>.*?</span>', "", h2_inner, flags=re.S | re.I)
                title = strip_html_tags(t_wo_p)

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

            m_time = re.search(r"<time[^>]*>(.*?)</time>", card_html, re.S | re.I)
            pub_time = strip_html_tags(m_time.group(1)) if m_time else ""

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
                # 仅解析主 <article> 区域，彻底隔离边栏推广广告
                article_html = extract_balanced_tag(det_html, "article")
                if not article_html:
                    skipped_no_detail += 1
                    continue

                m_origin = re.search(
                    r'<span[^>]*class=["\'][^"\']*zd-article-origin[^"\']*["\'][^>]*>(.*?)</span>',
                    article_html,
                    re.S | re.I,
                )
                mall_raw = strip_html_tags(m_origin.group(1)) if m_origin else ""
                mall_name = re.sub(r"^来源[:：]\s*", "", mall_raw).strip()

                if is_confirmed_unsupported_deal(mall_name=mall_name):
                    skipped_unsupported += 1
                    continue

                m_det_time = re.search(r'<time[^>]*datetime=["\']([^"\']+)["\']', article_html, re.I)
                if m_det_time:
                    pub_time = html_lib.unescape(m_det_time.group(1)).strip()

                actions_html = extract_balanced_tag(article_html, "div", r'\bclass=["\'][^"\']*zd-article-actions[^"\']*["\']')
                rich_html = extract_balanced_tag(article_html, "div", r'\bclass=["\'][^"\']*zd-rich[^"\']*["\']')

                a_links, _, _ = extract_links_images_and_tokens(
                    actions_html, base_url=detail_url, exclude_hosts=SOURCE_SELF_HOSTS
                )
                r_links, r_imgs, r_tokens = extract_links_images_and_tokens(
                    rich_html, base_url=detail_url, exclude_hosts=SOURCE_SELF_HOSTS
                )

                merged_links: List[Dict[str, Any]] = []
                seen_u: Set[str] = set()
                for lk in [*a_links, *r_links]:
                    u = str(lk.get("url") or "").strip()
                    if u and u not in seen_u:
                        seen_u.add(u)
                        merged_links.append(lk)

                all_imgs: List[str] = [img_url] if img_url else []
                for im in r_imgs:
                    if im and im not in all_imgs:
                        all_imgs.append(im)

                if is_confirmed_unsupported_deal(mall_name=mall_name, links=merged_links, tokens=r_tokens):
                    skipped_unsupported += 1
                    continue

                rich_text = html_to_tutorial_text(rich_html, base_url=detail_url).strip()
                action_lines = [f"{lk.get('label') or '购买链接'}: {lk['url']}" for lk in a_links]
                tut_parts = [f"【{mall_name or '真的值得买'}】{title}".strip()]
                if rich_text:
                    tut_parts.append(rich_text)
                if action_lines:
                    tut_parts.extend(action_lines)
                tutorial_text = "\n".join(tut_parts).strip()

                rec = build_deal_record(
                    source_site="zdzdm",
                    source_channel=ch.name,
                    source_item_id=item_id,
                    source_url=detail_url,
                    source_list_page_url=list_url,
                    source_detail_url=detail_url,
                    title=title or f"真的值得买 #{item_id}",
                    price_text=price_text,
                    image_url=img_url,
                    mall_name=mall_name,
                    publish_time=pub_time,
                    tutorial_text=tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=merged_links,
                    extracted_tokens=r_tokens,
                    raw_payload=article_html,
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
