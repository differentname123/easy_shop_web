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
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

from .base import FetchContext
from .filters import SOURCE_SELF_HOSTS, is_confirmed_unsupported_deal
from .list_detail_base import ListDetailDealSource, ListPhaseResult, SkipItem, fetch_page_html
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)


class ZdzdmSource(ListDetailDealSource):
    site_key = "zdzdm"
    source_key = "zdzdm"
    troubleshooting = {
        "endpoint": "https://www.zdzdm.com/youhui",
        "http_403_429": "真的值得买返回限流：请调低 max_details 或调大 detail_delay_seconds。",
        "empty_or_parse": "真的值得买未解析到 .zd-deal 条目：请检查首页模板。",
        "timeout_or_conn": "真的值得买请求超时：下轮将自动重试。",
    }

    BASE_URL = "https://www.zdzdm.com"

    def fetch_list_cards(self, ctx: FetchContext, t0: float) -> ListPhaseResult:
        list_url = str(ctx.channel.default_url or f"{self.BASE_URL}/youhui")
        resp, list_html, err = fetch_page_html(ctx, list_url, t0=t0)
        if err is not None:
            return ListPhaseResult(request_url=list_url, http_status=err.http_status, error_result=err)

        cards: List[Dict[str, Any]] = []
        for card_html in re.findall(r'<article\s+class=["\']zd-deal["\'][^>]*>(.*?)</article>', list_html, re.S | re.I):
            m_href = re.search(r'href=["\'](/p/(\d+))["\']', card_html, re.I)
            if not m_href:
                continue
            rel_href, item_id = m_href.group(1).strip(), m_href.group(2).strip()
            m_title = re.search(
                r'<h2[^>]*class=["\'][^"\']*zd-deal-title[^"\']*["\'][^>]*>(.*?)</h2>', card_html, re.S | re.I
            )
            price_text, title = "", ""
            if m_title:
                h2_inner = m_title.group(1)
                m_p = re.search(
                    r'<span[^>]*class=["\'][^"\']*zd-content-price[^"\']*["\'][^>]*>(.*?)</span>', h2_inner, re.S | re.I
                )
                price_text = strip_html_tags(m_p.group(1)) if m_p else ""
                t_wo_p = re.sub(
                    r'<span[^>]*class=["\'][^"\']*zd-content-price[^"\']*["\'][^>]*>.*?</span>',
                    "",
                    h2_inner,
                    flags=re.S | re.I,
                )
                title = strip_html_tags(t_wo_p)

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
            m_time = re.search(r"<time[^>]*>(.*?)</time>", card_html, re.S | re.I)
            pub_time = strip_html_tags(m_time.group(1)) if m_time else ""

            cards.append({
                "item_id": item_id,
                "detail_url": urljoin(self.BASE_URL, rel_href),
                "title": title,
                "price_text": price_text,
                "image_url": img_url,
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
        # 仅解析主 <article> 区域，彻底隔离边栏推广广告
        article_html = extract_balanced_tag(det_html, "article")
        if not article_html:
            return None

        m_origin = re.search(
            r'<span[^>]*class=["\'][^"\']*zd-article-origin[^"\']*["\'][^>]*>(.*?)</span>',
            article_html,
            re.S | re.I,
        )
        mall_raw = strip_html_tags(m_origin.group(1)) if m_origin else ""
        mall_name = re.sub(r"^来源[:：]\s*", "", mall_raw).strip()
        if is_confirmed_unsupported_deal(mall_name=mall_name):
            raise SkipItem("unsupported")

        item_id = card["item_id"]
        detail_url = card["detail_url"]
        title = card["title"]
        img_url = card["image_url"]
        pub_time = card["publish_time"]

        m_det_time = re.search(r'<time[^>]*datetime=["\']([^"\']+)["\']', article_html, re.I)
        if m_det_time:
            pub_time = html_lib.unescape(m_det_time.group(1)).strip()

        actions_html = extract_balanced_tag(
            article_html, "div", r'\bclass=["\'][^"\']*zd-article-actions[^"\']*["\']'
        )
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
            raise SkipItem("unsupported")

        rich_text = html_to_tutorial_text(rich_html, base_url=detail_url).strip()
        action_lines = [f"{lk.get('label') or '购买链接'}: {lk['url']}" for lk in a_links]
        tut_parts = [f"【{mall_name or '真的值得买'}】{title}".strip()]
        if rich_text:
            tut_parts.append(rich_text)
        if action_lines:
            tut_parts.extend(action_lines)
        tutorial_text = "\n".join(tut_parts).strip()

        return build_deal_record(
            source_site="zdzdm",
            source_channel=ctx.channel.name,
            source_item_id=item_id,
            source_url=detail_url,
            source_list_page_url=list_res.request_url,
            source_detail_url=detail_url,
            title=title or f"真的值得买 #{item_id}",
            price_text=card["price_text"],
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
