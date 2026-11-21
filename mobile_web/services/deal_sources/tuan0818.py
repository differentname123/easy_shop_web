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
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from .base import FetchContext
from .filters import SOURCE_SELF_HOSTS, is_confirmed_unsupported_deal
from .list_detail_base import ListDetailDealSource, ListPhaseResult, SkipItem, fetch_page_html
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_div_by_id,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)


class Tuan0818Source(ListDetailDealSource):
    site_key = "0818tuan"
    source_key = "0818tuan"
    troubleshooting = {
        "endpoint": "http://www.0818tuan.com/list-1-0.html",
        "http_403_429": "0818团返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "0818团未解析到 /xbhd/ 条目：请核对列表页面结构。",
        "timeout_or_conn": "0818团请求超时：下轮将自动重试。",
    }

    BASE_URL = "http://www.0818tuan.com"

    def fetch_list_cards(self, ctx: FetchContext, t0: float) -> ListPhaseResult:
        list_url = str(ctx.channel.default_url or f"{self.BASE_URL}/list-1-0.html")
        resp, list_html, err = fetch_page_html(ctx, list_url, t0=t0)
        if err is not None:
            return ListPhaseResult(request_url=list_url, http_status=err.http_status, error_result=err)

        cards: List[Dict[str, Any]] = []
        for full_a, rel_href, item_id in re.findall(
            r'(<a\b[^>]*href=["\'](/xbhd/(\d+)\.html)["\'][^>]*>.*?</a>)',
            list_html,
            re.S | re.I,
        ):
            time_m = re.search(r'<span[^>]*class=["\'][^"\']*badge[^"\']*["\'][^>]*>(.*?)</span>', full_a, re.S | re.I)
            pub_time = strip_html_tags(time_m.group(1)) if time_m else ""
            title_m = re.search(r'title=["\']([^"\']+)["\']', full_a, re.I)
            if title_m:
                title = html_lib.unescape(title_m.group(1)).strip()
            else:
                title = strip_html_tags(re.sub(r"<span[^>]*>.*?</span>", "", full_a, flags=re.S | re.I))
            cards.append({
                "item_id": item_id,
                "detail_url": urljoin(self.BASE_URL, rel_href),
                "title": title,
                "publish_time": pub_time,
            })

        return ListPhaseResult(cards=cards, request_url=list_url, http_status=resp.status_code, pages_crawled=1)

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
        pub_time = card["publish_time"]

        m_full_time = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)", det_html)
        if m_full_time:
            pub_time = m_full_time.group(1)

        xb_fragment = extract_div_by_id(det_html, "xbcontent")
        if not xb_fragment:
            xb_fragment = extract_balanced_tag(det_html, "div", r'\bclass=["\'][^"\']*post-content[^"\']*["\']')
        if not xb_fragment:
            return None

        clean_xb = re.sub(
            r"<p[^>]*>\s*(?:<a[^>]*href=[\"'][^\"']*member/fava[^\"']*[\"'][^>]*>.*?</a>|上一篇：.*?|下一篇：.*?)\s*</p>",
            "",
            xb_fragment,
            flags=re.S | re.I,
        )
        links, images, tokens = extract_links_images_and_tokens(
            clean_xb, base_url=detail_url, exclude_hosts=SOURCE_SELF_HOSTS
        )
        if is_confirmed_unsupported_deal(links=links, tokens=tokens):
            raise SkipItem("unsupported")

        tutorial_text = html_to_tutorial_text(clean_xb, base_url=detail_url)
        return build_deal_record(
            source_site="0818tuan",
            source_channel=ctx.channel.name,
            source_item_id=item_id,
            source_url=detail_url,
            source_list_page_url=list_res.request_url,
            source_detail_url=detail_url,
            title=card["title"] or f"0818团线报 #{item_id}",
            publish_time=pub_time,
            tutorial_text=tutorial_text,
            tutorial_images=images,
            extracted_links=links,
            extracted_tokens=tokens,
            raw_payload=xb_fragment,
            has_full_detail=True,
        )
