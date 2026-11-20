# -*- coding: utf-8 -*-
"""
数据源 12：白菜哦 (`https://www.baicaio.com/`)
================================================================================
- 支持两个监控频道：
  1. `"nice"` (`https://www.baicaio.com/nice/`)：编辑推荐第一页（20 条），每 30 分钟拉取一次；
  2. `"hour"` (`https://www.baicaio.com/hour/`)：小时排行榜全量巡视，每 30 分钟拉取一次，
     并以本次拉取时间作为发布时间（不以源站更新时间为准）。
- 关键防错与跳板解包机制：
  1. 详情页顶部导航栏硬编码了 `orig-show-id-3`（天猫商城），因此真实商城名必须从商品标签区
     `<div class="mt-15 biaoqian">...<a title="商城：...">` 提取；
  2. 详情页内的购买按钮 (`去购买`) 与正文超链接有时是内部跳板 `/?m=item&a=tgo&to=...`
     （请求后返回 HTTP 302 `Location: https://u.jd.com/...` 或调试正文 `["url"]=>...`），
     本插件会自动解包为真实明文电商短链再入库并参与非淘京拼过滤；
  3. 仅提取 `<!--正文start-->...<!--正文end-->` 正文与主购买按钮，避免边栏推荐串货。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any, Dict, List, Set
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


def _parse_nice_list(list_html: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    ul_html = extract_balanced_tag(list_html, "ul", r'\bclass=["\'][^"\']*J_item[^"\']*["\']') or list_html
    # 实测 /nice/ 列表中 <li class="item"> 之间经常省略闭合 </li>，必须前瞻到下一个 <li 边界切分
    for li in re.findall(
        r'<li\s+class=["\']item["\'][^>]*>.*?(?=<li\b|<!--\s*列表end\s*-->|</ul>|$)',
        ul_html,
        re.S | re.I,
    ):
        m_href = re.search(r'href=["\'](https?://www\.baicaio\.com/item/(\d+)\.html)["\']', li, re.I)
        if not m_href:
            continue
        detail_url, item_id = m_href.group(1), m_href.group(2)
        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', li, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
        m_t = re.search(r'<div[^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>.*?<a[^>]*>(.*?)</a>', li, re.S | re.I)
        title = strip_html_tags(m_t.group(1)) if m_t else ""
        m_time = re.search(r'<div\s+class=["\']user["\'][^>]*>.*?<span[^>]*>(.*?)</span>', li, re.S | re.I)
        pub_time = strip_html_tags(m_time.group(1)) if m_time else ""
        items.append({
            "item_id": item_id,
            "detail_url": detail_url,
            "title": title,
            "image_url": img_url,
            "mall_name": "",
            "price_text": "",
            "publish_time": pub_time,
        })
    return items


def _parse_hour_list(list_html: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    ul_html = extract_balanced_tag(list_html, "ul", r'\bclass=["\'][^"\']*list\s+mt-20[^"\']*["\']') or list_html
    for li in re.findall(
        r'<li\s+class=["\']clearfix["\'][^>]*>.*?(?=<li\b|<!--\s*列表循环end\s*-->|</ul>|$)',
        ul_html,
        re.S | re.I,
    ):
        m_href = re.search(r'href=["\'](https?://www\.baicaio\.com/item/(\d+)\.html)["\']', li, re.I)
        if not m_href:
            continue
        detail_url, item_id = m_href.group(1), m_href.group(2)
        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', li, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
        m_t = re.search(r'<a[^>]+href=["\']https?://www\.baicaio\.com/item/\d+\.html["\'][^>]*title=["\']([^"\']+)["\']', li, re.I)
        title = strip_html_tags(m_t.group(1)) if m_t else ""
        m_mall = re.search(r'href=["\']https?://www\.baicaio\.com/orig-show-id-\d+["\'][^>]*>(.*?)</a>', li, re.S | re.I)
        mall_name = strip_html_tags(m_mall.group(1)) if m_mall else ""
        m_price = re.search(r'<p[^>]*class=["\'][^"\']*fc-red[^"\']*["\'][^>]*>(.*?)</p>', li, re.S | re.I)
        price_text = strip_html_tags(m_price.group(1)) if m_price else ""
        items.append({
            "item_id": item_id,
            "detail_url": detail_url,
            "title": title,
            "image_url": img_url,
            "mall_name": mall_name,
            "price_text": price_text,
            "publish_time": "",
        })
    return items


class BaicaioSource(BaseDealSource):
    site_key = "baicaio"
    source_key = "baicaio"
    troubleshooting = {
        "endpoint": "https://www.baicaio.com/nice/ | https://www.baicaio.com/hour/",
        "http_403_429": "白菜哦返回限流：请调低 max_details 或增大 detail_delay_seconds。",
        "empty_or_parse": "白菜哦列表解析为 0：请核对 nice/hour 页面列表选择器。",
        "timeout_or_conn": "白菜哦请求超时：下轮将自动重试。",
    }

    BASE_URL = "https://www.baicaio.com"

    def _unwrap_tgo_url(self, ctx: FetchContext, raw_url: str, referer_url: str) -> str:
        """解析白菜哦站内 `/?m=item&a=tgo&to=...` 跳板，获取真实的电商直达链接。"""
        u = html_lib.unescape(str(raw_url or "")).strip()
        if not u:
            return ""
        if "a=tgo" not in u and "m=item" not in u:
            return u
        full_u = urljoin(self.BASE_URL, u)
        try:
            rg = request_with_retry(
                ctx.session,
                "get",
                full_u,
                headers={**DEFAULT_HEADERS, "Referer": referer_url},
                timeout=ctx.timeout,
                allow_redirects=False,
            )
            loc = rg.headers.get("Location") or rg.headers.get("location") or ""
            if loc.startswith(("http://", "https://")):
                return loc.strip()
            m_dump = re.search(r'\["url"\]\s*=>\s*string\(\d+\)\s*"(https?://[^"]+)"', rg.text)
            if m_dump:
                return html_lib.unescape(m_dump.group(1)).strip()
        except Exception:
            pass
        return ""

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        params = ch.params or {}
        mode = str(params.get("mode") or "nice").strip().lower()
        list_url = str(ch.default_url or (f"{self.BASE_URL}/hour/" if mode == "hour" else f"{self.BASE_URL}/nice/"))
        full_sweep = ch.fetch_mode == "full_sweep"
        use_fetch_time = bool(ch.use_fetch_time_as_publish_time or mode == "hour")
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

        cards = _parse_hour_list(list_html) if mode == "hour" else _parse_nice_list(list_html)
        pages_crawled = 1
        if mode == "hour" and not cards:
            m_prev = re.search(
                r'<div[^>]+class=["\'][^"\']*hour-page[^"\']*["\'][^>]*>.*?<a[^>]+href=["\']([^"\']*hour-index-hour-\d+[^"\']*)["\']',
                list_html,
                re.S | re.I,
            )
            if m_prev:
                prev_url = urljoin(self.BASE_URL, html_lib.unescape(m_prev.group(1)).strip())
                try:
                    rp = request_with_retry(
                        ctx.session,
                        "get",
                        prev_url,
                        headers={**DEFAULT_HEADERS, "Referer": list_url},
                        timeout=ctx.timeout,
                    )
                    rp.encoding = "utf-8"
                    if rp.status_code == 200:
                        pages_crawled += 1
                        cards = _parse_hour_list(rp.text)
                except Exception:
                    pass

        records: List[Dict[str, Any]] = []
        detail_errors: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        touch_ids: Set[str] = set()
        raw_list_count = 0
        detail_fetched = 0
        detail_attempted = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        detail_circuit_broken = False
        rate_limited = False

        for card in cards:
            item_id = card["item_id"]
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            raw_list_count += 1

            mall_name = card.get("mall_name") or ""
            if mall_name and is_confirmed_unsupported_deal(mall_name=mall_name):
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
            img_url = card["image_url"]
            price_text = card["price_text"]
            pub_time = card["publish_time"]

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

                # 精确提取详情页标题、真实商城与发表时间
                m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>", det_html, re.S | re.I)
                if m_h1:
                    h1_inner = m_h1.group(1)
                    m_red = re.search(r'<span[^>]*class=["\'][^"\']*fc-red[^"\']*["\'][^>]*>(.*?)</span>', h1_inner, re.S | re.I)
                    if m_red and not price_text:
                        price_text = strip_html_tags(m_red.group(1))
                    title = strip_html_tags(h1_inner) or title

                tag_box = extract_balanced_tag(det_html, "div", r'\bclass=["\'][^"\']*biaoqian[^"\']*["\']')
                if tag_box:
                    m_mall_tag = re.search(r'title=["\']商城[:：]([^"\']+)["\']', tag_box, re.I)
                    if m_mall_tag:
                        mall_name = m_mall_tag.group(1).strip()

                if is_confirmed_unsupported_deal(mall_name=mall_name):
                    skipped_unsupported += 1
                    continue

                m_det_time = re.search(r"发表时间[:：]\s*([^<\n]+)", det_html)
                if m_det_time:
                    pub_time = strip_html_tags(m_det_time.group(1))

                # 提取主购买按钮链接（仅限于正文前的商品主操作区，避免导航栏混淆）
                buy_url_raw = ""
                m_buy = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>[^<]*去购买[^<]*</a>', det_html, re.S | re.I)
                if not m_buy:
                    m_buy = re.search(r'<a\b[^>]*class=["\'][^"\']*btn-3[^"\']*["\'][^>]*href=["\']([^"\']+)["\']', det_html, re.S | re.I)
                if m_buy:
                    buy_url_raw = html_lib.unescape(m_buy.group(1)).strip()

                # 提取 <!--正文start-->...<!--正文end--> 正文片段
                con_html = ""
                if "<!--正文start-->" in det_html and "<!--正文end-->" in det_html:
                    con_html = det_html.split("<!--正文start-->", 1)[1].split("<!--正文end-->", 1)[0]
                else:
                    con_html = extract_balanced_tag(det_html, "div", r'\bclass=["\'][^"\']*content\s+mt-20[^"\']*["\']')

                if not con_html:
                    skipped_no_detail += 1
                    continue

                # 解包所有内部 tgo 跳板链接（先替换正文中的 tgo，确保 tutorial_text 也拿到真实短链）
                tgo_cache: Dict[str, str] = {}

                def _resolve_url(candidate: str) -> str:
                    c = html_lib.unescape(str(candidate or "")).strip()
                    if not c:
                        return ""
                    if "a=tgo" in c:
                        if c not in tgo_cache:
                            tgo_cache[c] = self._unwrap_tgo_url(ctx, c, detail_url)
                        return tgo_cache[c]
                    return urljoin(self.BASE_URL, c) if c.startswith("/") else c

                con_unwrapped = re.sub(
                    r'href=["\']([^"\']*a=tgo[^"\']*)["\']',
                    lambda m: f'href="{_resolve_url(m.group(1)) or m.group(1)}"',
                    con_html,
                    flags=re.I,
                )

                c_links, c_imgs, c_tokens = extract_links_images_and_tokens(
                    con_unwrapped,
                    base_url=detail_url,
                    exclude_hosts=SOURCE_SELF_HOSTS,
                )

                merged_links: List[Dict[str, Any]] = []
                seen_urls: Set[str] = set()
                if buy_url_raw:
                    resolved_buy = _resolve_url(buy_url_raw)
                    if resolved_buy.startswith(("http://", "https://")) and "baicaio.com" not in resolved_buy.lower():
                        seen_urls.add(resolved_buy)
                        merged_links.append({
                            "label": "去购买",
                            "url": resolved_buy,
                            "type": "buy_url",
                            "platform": detect_platform_signal(f"{resolved_buy} {mall_name}") or "unknown",
                        })

                for lk in c_links:
                    u = str(lk.get("url") or "").strip()
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        merged_links.append(lk)

                all_imgs: List[str] = [img_url] if img_url else []
                for im in c_imgs:
                    if im and im not in all_imgs:
                        all_imgs.append(im)

                if is_confirmed_unsupported_deal(mall_name=mall_name, links=merged_links, tokens=c_tokens):
                    skipped_unsupported += 1
                    continue

                con_text = html_to_tutorial_text(con_unwrapped, base_url=detail_url).strip()
                buy_line = f"\n直达购买: {merged_links[0]['url']}" if merged_links and merged_links[0]["url"] not in con_text else ""
                tutorial_text = f"【{mall_name or '白菜哦'}】{title}\n{con_text}{buy_line}".strip()

                effective_pub_time = utc_now_iso() if use_fetch_time else pub_time
                hot_metrics: Dict[str, Any] = {"mode": mode}
                if pub_time:
                    hot_metrics["upstream_time"] = pub_time

                rec = build_deal_record(
                    source_site="baicaio",
                    source_channel=ch.name,
                    source_item_id=item_id,
                    source_url=detail_url,
                    source_list_page_url=list_url,
                    source_detail_url=detail_url,
                    title=title or f"白菜哦 #{item_id}",
                    price_text=price_text,
                    image_url=img_url,
                    mall_name=mall_name,
                    publish_time=effective_pub_time,
                    hot_metrics=hot_metrics,
                    tutorial_text=tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=merged_links,
                    extracted_tokens=c_tokens,
                    raw_payload=con_unwrapped,
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
        is_ok = (raw_list_count > 0 or (mode == "hour" and "list mt-20" in list_html)) and not (
            rate_limited and len(records) == 0 and len(touch_ids) == 0
        )
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
            pages_crawled=pages_crawled,
            touch_ids=sorted(touch_ids),
            sweep_completed=bool(full_sweep and raw_list_count > 0 and not rate_limited),
            rate_limited=rate_limited,
            detail_errors=detail_errors,
        )
