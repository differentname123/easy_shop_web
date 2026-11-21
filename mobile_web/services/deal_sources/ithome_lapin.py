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
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

from models.deal_constants import parse_publish_time_epoch

from .base import FetchContext
from .filters import SOURCE_SELF_HOSTS, is_confirmed_unsupported_deal
from .http_client import DEFAULT_HEADERS, request_with_retry
from .list_detail_base import ListDetailDealSource, ListPhaseResult, SkipItem, fetch_page_html
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)


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
        ot_ts = parse_publish_time_epoch(ot_str)

        m_img = re.search(r'<img\b[^>]*\b(?:data-original|src)=["\']([^"\']+)["\']', li, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
        if img_url.startswith("//"):
            img_url = "https:" + img_url

        items.append({
            "item_id": item_id,
            "detail_url": detail_url,
            "title": title,
            "publish_time": ot_str,
            "publish_ts": ot_ts,
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
    cleaned = re.sub(r"<p[^>]*>\s*广告声明：(?:(?!</p>).)*?</p>", "", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"<p[^>]*>(?:(?!</p>).)*?辣品\s*App(?:(?!</p>).)*?</p>", "", cleaned, flags=re.S | re.I)
    cleaned = re.sub(
        r"<p[^>]*>(?:(?!</p>).)*?(?:京东|淘宝|天猫)[^<]{0,20}无门槛红包(?:(?!</p>).)*?</p>",
        "",
        cleaned,
        flags=re.S | re.I,
    )
    return cleaned


class IthomeLapinSource(ListDetailDealSource):
    site_key = "ithome_lapin"
    source_key = "ithome_lapin"
    troubleshooting = {
        "endpoint": "https://digi.ithome.com/lapin | POST /category/domainpage?domain=digi&subdomain=lapin",
        "http_403_429": "IT之家返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "IT之家辣品列表解析为 0：请核对 <ul class='bl'> 结构。",
        "timeout_or_conn": "IT之家请求超时：下轮将自动重试。",
    }

    LIST_URL = "https://digi.ithome.com/lapin"

    def fetch_list_cards(self, ctx: FetchContext, t0: float) -> ListPhaseResult:
        ch = ctx.channel
        list_url = str(ch.default_url or self.LIST_URL)
        max_pages = max(1, int(ch.max_pages or 10))
        cutoff_ts = ctx.cutoff_ts

        resp, first_html, err = fetch_page_html(ctx, list_url, t0=t0)
        if err is not None:
            return ListPhaseResult(request_url=list_url, http_status=err.http_status, error_result=err)

        pages_crawled = 1
        bl_fragment = extract_balanced_tag(first_html, "ul", r'\bclass=["\'][^"\']*\bbl\b') or first_html
        collected: List[Dict[str, Any]] = _parse_ithome_list_items(bl_fragment)
        seen_list_ids: Set[str] = {it["item_id"] for it in collected}

        while pages_crawled < max_pages and collected:
            last_ot_ts = collected[-1].get("publish_ts")
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
                added = 0
                for mi in _parse_ithome_list_items(more_html):
                    if mi["item_id"] not in seen_list_ids:
                        seen_list_ids.add(mi["item_id"])
                        collected.append(mi)
                        added += 1
                if added == 0:
                    break
            except Exception:
                break

        return ListPhaseResult(
            cards=collected,
            request_url=list_url,
            http_status=resp.status_code,
            pages_crawled=pages_crawled,
            reached_end=False,
        )

    def parse_detail_record(
        self,
        card: Dict[str, Any],
        det_resp: Any,
        det_html: str,
        list_res: ListPhaseResult,
        ctx: FetchContext,
    ) -> Optional[Dict[str, Any]]:
        item_id = card["item_id"]
        detail_url = card["detail_url"]
        img_url = card["image_url"]

        para_html = extract_balanced_tag(det_html, "div", r'\bid=["\']paragraph["\']')
        if not para_html:
            return None

        clean_para = _clean_ithome_paragraph_html(para_html)
        links, det_imgs, tokens = extract_links_images_and_tokens(
            clean_para,
            base_url=detail_url,
            exclude_hosts=SOURCE_SELF_HOSTS,
        )
        tutorial_text = html_to_tutorial_text(clean_para, base_url=detail_url).strip()
        tutorial_text = re.sub(r"\n?广告声明：.*$", "", tutorial_text, flags=re.S).strip()

        all_imgs: List[str] = [img_url] if img_url else []
        for im in det_imgs:
            if im and im not in all_imgs:
                all_imgs.append(im)

        if is_confirmed_unsupported_deal(links=links, tokens=tokens):
            raise SkipItem("unsupported")

        return build_deal_record(
            source_site="ithome_lapin",
            source_channel=ctx.channel.name,
            source_item_id=item_id,
            source_url=detail_url,
            source_list_page_url=list_res.request_url,
            source_detail_url=detail_url,
            title=card["title"] or f"IT之家辣品 #{item_id}",
            image_url=img_url,
            publish_time=card["publish_time"],
            publish_ts=card.get("publish_ts"),
            tutorial_text=tutorial_text,
            tutorial_images=all_imgs,
            extracted_links=links,
            extracted_tokens=tokens,
            raw_payload=clean_para,
            has_full_detail=True,
        )
