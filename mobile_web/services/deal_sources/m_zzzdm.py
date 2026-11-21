# -*- coding: utf-8 -*-
"""
数据源 6：值值得买 24 小时热榜 (`https://m.zzzdm.com/posts/ranking.html`)
================================================================================
- 策略：每 30 分钟全量巡视单页榜单（约 90 条），并以本次拉取时间作为 `publish_time`；
- 关键防串货隔离（严格限定详情 DOM 域）：
  详情页顶部常驻全站双11红包横幅（如 `u.jd.com/FGpTvZF` 与 `m.tb.cn/h.jbIMh5M`），
  因此本插件【严禁】全文扫描详情 HTML，而是精确提取以下三处并剥离导购宣传语：
  1. `🏷️ 优惠券` `<section>` 内的优惠券链接；
  2. `优惠详情` `<section>` 内的正文与步骤链接/图片；
  3. `🛒 立即购买` 按钮的 `href`。
- 列表卡片先行读取商城名 (`🏬 京东` / `🏬 天猫精选` / `🏬 抖音` 等)，在请求详情前即完成
  100% 非淘京拼过滤与 `known_ids` 去重（已抓商品进入 `touch_ids`）。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

from models.product import detect_platform_signal

from .base import FetchContext
from .filters import SOURCE_SELF_HOSTS, is_confirmed_unsupported_deal, is_host_match
from .list_detail_base import ListDetailDealSource, ListPhaseResult, SkipItem, fetch_page_html
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)


def _strip_zzzdm_promo_noise(html_fragment: str) -> str:
    """剔除优惠详情尾部的站内双11/活动抽奖引导语，保留真实商品优惠描述与小编补充。"""
    cleaned = re.sub(
        r"<p[^>]*>\s*(?:<span[^>]*>\s*)?(?:<strong>)?(?:双11惊喜大礼盒|点击下图开启好运).*?</p>",
        "",
        html_fragment,
        flags=re.S | re.I,
    )
    cleaned = re.sub(r"<p[^>]*>\s*(?:<span>\s*</span>\s*|<a>\s*<img>\s*</a>\s*)+</p>", "", cleaned, flags=re.S | re.I)
    return cleaned


class MZzzdmSource(ListDetailDealSource):
    site_key = "zzzdm"
    source_key = "m_zzzdm"
    troubleshooting = {
        "endpoint": "https://m.zzzdm.com/posts/ranking.html",
        "http_403_429": "值值得买触发限流：请调低 max_details 或增大 detail_delay_seconds。",
        "empty_or_parse": "值值得买未解析到 .rank-card 条目：请核对列表模板是否改动。",
        "timeout_or_conn": "值值得买请求超时：下轮将自动重试。",
    }

    BASE_URL = "https://m.zzzdm.com"

    def fetch_list_cards(self, ctx: FetchContext, t0: float) -> ListPhaseResult:
        list_url = str(ctx.channel.default_url or f"{self.BASE_URL}/posts/ranking.html")
        resp, list_html, err = fetch_page_html(ctx, list_url, t0=t0)
        if err is not None:
            return ListPhaseResult(request_url=list_url, http_status=err.http_status, error_result=err)

        cards: List[Dict[str, Any]] = []
        for rel_href, sid, inner in re.findall(
            r'<a\b[^>]*href=["\'](/posts/view\?sid=([^"\'&]+))["\'][^>]*class=["\'][^"\']*rank-card[^"\']*["\'][^>]*>(.*?)</a>',
            list_html,
            re.S | re.I,
        ):
            sid = sid.strip()
            if not sid:
                continue
            m_mall = re.search(r"🏬\s*([^<\n]+)", inner)
            mall_name = m_mall.group(1).strip() if m_mall else ""

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', inner, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
            m_alt = re.search(r'<img[^>]+alt=["\']([^"\']+)["\']', inner, re.I)
            title = html_lib.unescape(m_alt.group(1)).strip() if m_alt else ""
            if not title:
                m_h = re.search(r"<h[23][^>]*>(.*?)</h[23]>", inner, re.S | re.I)
                title = strip_html_tags(m_h.group(1)) if m_h else strip_html_tags(inner)[:80]

            m_price = re.search(r"￥\s*([0-9]+(?:\.[0-9]+)?)", inner)
            price_text = f"{m_price.group(1)}元" if m_price else ""
            m_up_time = re.search(r"(\d{2}-\d{2}\s+\d{2}:\d{2})", inner)
            upstream_time = m_up_time.group(1) if m_up_time else ""

            cards.append({
                "item_id": sid,
                "detail_url": f"{self.BASE_URL}{rel_href}",
                "mall_name": mall_name,
                "image_url": img_url,
                "title": title,
                "price_text": price_text,
                "upstream_time": upstream_time,
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
        sid = card["item_id"]
        detail_url = card["detail_url"]
        title = card["title"]
        img_url = card["image_url"]
        mall_name = card["mall_name"]

        if not mall_name:
            m_det_mall = re.search(r"🏬\s*(?:</span>)?\s*([^<\n]+)", det_html)
            if m_det_mall:
                mall_name = m_det_mall.group(1).strip()

        # 1. 提取「立即购买」主购买链（严禁扫描全站顶部红包区）
        buy_links: List[Dict[str, Any]] = []
        for m_buy in re.finditer(r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', det_html, re.S | re.I):
            a_txt = strip_html_tags(m_buy.group(2))
            if "立即购买" in a_txt or "去购买" in a_txt or "直达链接" in a_txt:
                buy_u = html_lib.unescape(m_buy.group(1)).strip()
                buy_host = (urlsplit(buy_u).hostname or "").strip().lower()
                if any(is_host_match(buy_host, sh) for sh in SOURCE_SELF_HOSTS):
                    continue
                if buy_u and not any(x["url"] == buy_u for x in buy_links):
                    buy_links.append({
                        "label": "立即购买",
                        "url": buy_u,
                        "type": "buy_url",
                        "platform": detect_platform_signal(f"{buy_u} {mall_name}") or "unknown",
                    })

        # 2. 仅提取「优惠券」section 内的领券链（严禁误抓全站红包横幅）
        coupon_sec = ""
        if "<!-- 优惠券 -->" in det_html:
            coupon_sec = extract_balanced_tag(det_html.split("<!-- 优惠券 -->", 1)[1], "section")
        if not coupon_sec:
            m_coupon_sec = re.search(
                r"<section\b[^>]*>(?:(?!</section>).)*?(?:🏷️|优惠券).*?</section>", det_html, re.S | re.I
            )
            if m_coupon_sec:
                coupon_sec = m_coupon_sec.group(0)
        if coupon_sec and any(ad_kw in coupon_sec for ad_kw in ("双11红包", "京东红包", "淘宝红包")):
            coupon_sec = ""
        coupon_links: List[Dict[str, Any]] = []
        if coupon_sec:
            c_links, _, _ = extract_links_images_and_tokens(
                coupon_sec, base_url=detail_url, exclude_hosts=SOURCE_SELF_HOSTS
            )
            for lk in c_links:
                lk["type"] = "coupon_url"
                coupon_links.append(lk)

        # 3. 仅提取「优惠详情」section 正文（含小编补充），彻底隔绝全站红包横幅与底部评论区
        detail_sec = ""
        if "<!-- 优惠详情 -->" in det_html:
            detail_sec = extract_balanced_tag(det_html.split("<!-- 优惠详情 -->", 1)[1], "section")
        elif "优惠详情" in det_html:
            detail_sec = extract_balanced_tag(det_html.split("优惠详情", 1)[1], "div")

        clean_detail_sec = _strip_zzzdm_promo_noise(detail_sec)
        d_links, d_imgs, d_tokens = extract_links_images_and_tokens(
            clean_detail_sec, base_url=detail_url, exclude_hosts=SOURCE_SELF_HOSTS
        )
        detail_text = html_to_tutorial_text(clean_detail_sec, base_url=detail_url).strip()

        merged_links: List[Dict[str, Any]] = []
        seen_u: Set[str] = set()
        for lk in [*buy_links, *coupon_links, *d_links]:
            u = str(lk.get("url") or "").strip()
            if u and u not in seen_u:
                seen_u.add(u)
                merged_links.append(lk)

        all_imgs: List[str] = [img_url] if img_url else []
        for im in d_imgs:
            if im and im not in all_imgs:
                all_imgs.append(im)

        if is_confirmed_unsupported_deal(mall_name=mall_name, links=merged_links, tokens=d_tokens):
            raise SkipItem("unsupported")

        tut_parts: List[str] = []
        if mall_name or title:
            tut_parts.append(f"【{mall_name or '值值得买'}】{title}".strip())
        if detail_text:
            tut_parts.append(detail_text)
        if coupon_links:
            tut_parts.append("优惠券: " + " | ".join(lk["url"] for lk in coupon_links))
        if buy_links:
            tut_parts.append("购买链接: " + " | ".join(lk["url"] for lk in buy_links))
        tutorial_text = "\n".join(tut_parts).strip()

        return build_deal_record(
            source_site="zzzdm",
            source_channel=ctx.channel.name,
            source_item_id=sid,
            source_url=detail_url,
            source_list_page_url=list_res.request_url,
            source_detail_url=detail_url,
            title=title or f"值值得买热榜 #{sid}",
            price_text=card["price_text"],
            image_url=img_url,
            mall_name=mall_name,
            publish_ts=time.time(),
            hot_metrics={"upstream_time": card["upstream_time"]},
            tutorial_text=tutorial_text,
            tutorial_images=all_imgs,
            extracted_links=merged_links,
            extracted_tokens=d_tokens,
            raw_payload=clean_detail_sec or det_html[:4000],
            has_full_detail=True,
        )
