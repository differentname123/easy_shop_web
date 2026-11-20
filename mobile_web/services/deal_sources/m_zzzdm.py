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
from typing import Any, Dict, List, Set

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


class MZzzdmSource(BaseDealSource):
    site_key = "zzzdm"
    source_key = "m_zzzdm"
    troubleshooting = {
        "endpoint": "https://m.zzzdm.com/posts/ranking.html",
        "http_403_429": "值值得买触发限流：请调低 max_details 或增大 detail_delay_seconds。",
        "empty_or_parse": "值值得买未解析到 .rank-card 条目：请核对列表模板是否改动。",
        "timeout_or_conn": "值值得买请求超时：下轮将自动重试。",
    }

    BASE_URL = "https://m.zzzdm.com"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        list_url = str(ch.default_url or f"{self.BASE_URL}/posts/ranking.html")
        full_sweep = ch.fetch_mode == "full_sweep"
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

        cards = re.findall(
            r'<a\b[^>]*href=["\'](/posts/view\?sid=([^"\'&]+))["\'][^>]*class=["\'][^"\']*rank-card[^"\']*["\'][^>]*>(.*?)</a>',
            list_html,
            re.S | re.I,
        )

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

        for rel_href, sid, inner in cards:
            sid = sid.strip()
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            raw_list_count += 1

            m_mall = re.search(r"🏬\s*([^<\n]+)", inner)
            mall_name = m_mall.group(1).strip() if m_mall else ""

            if is_confirmed_unsupported_deal(mall_name=mall_name):
                skipped_unsupported += 1
                continue

            if full_sweep:
                touch_ids.add(sid)

            if sid in skip_set:
                skipped_known += 1
                continue

            if detail_circuit_broken or detail_attempted >= max_details:
                continue

            if detail_attempted > 0:
                self.sleep_detail_delay(ctx)
            detail_attempted += 1

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

            detail_url = f"{self.BASE_URL}{rel_href}"

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
                        "item_id": sid,
                        "error_url": detail_url,
                        "http_status": rd.status_code,
                        "error_type": f"HTTP_{rd.status_code}",
                        "error": f"详情页返回非200状态码: {rd.status_code}",
                    })
                    continue

                det_html = rd.text

                # 详情页精确商城兜底（例如列表未识别时）
                if not mall_name:
                    m_det_mall = re.search(r"🏬\s*(?:</span>)?\s*([^<\n]+)", det_html)
                    if m_det_mall:
                        mall_name = m_det_mall.group(1).strip()

                # 1. 提取「立即购买」主购买链（严禁扫描全站顶部红包区）
                buy_links: List[Dict[str, Any]] = []
                for m_buy in re.finditer(
                    r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
                    det_html,
                    re.S | re.I,
                ):
                    a_txt = strip_html_tags(m_buy.group(2))
                    if "立即购买" in a_txt or "去购买" in a_txt or "直达链接" in a_txt:
                        buy_u = html_lib.unescape(m_buy.group(1)).strip()
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
                    after_coupon_marker = det_html.split("<!-- 优惠券 -->", 1)[1]
                    coupon_sec = extract_balanced_tag(after_coupon_marker, "section")
                if not coupon_sec:
                    m_coupon_sec = re.search(
                        r"<section\b[^>]*>(?:(?!</section>).)*?(?:🏷️|优惠券).*?</section>",
                        det_html,
                        re.S | re.I,
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
                    after_marker = det_html.split("<!-- 优惠详情 -->", 1)[1]
                    detail_sec = extract_balanced_tag(after_marker, "section")
                elif "优惠详情" in det_html:
                    after_marker = det_html.split("优惠详情", 1)[1]
                    detail_sec = extract_balanced_tag(after_marker, "div")

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
                    skipped_unsupported += 1
                    continue

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

                rec = build_deal_record(
                    source_site="zzzdm",
                    source_channel=ch.name,
                    source_item_id=sid,
                    source_url=detail_url,
                    source_list_page_url=list_url,
                    source_detail_url=detail_url,
                    title=title or f"值值得买热榜 #{sid}",
                    price_text=price_text,
                    image_url=img_url,
                    mall_name=mall_name,
                    publish_time=utc_now_iso(),
                    hot_metrics={"upstream_time": upstream_time},
                    tutorial_text=tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=merged_links,
                    extracted_tokens=d_tokens,
                    raw_payload=clean_detail_sec or det_html[:4000],
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
                    "item_id": sid,
                    "error_url": detail_url,
                    "http_status": None,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                })

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = raw_list_count > 0 and not (rate_limited and len(records) == 0 and len(touch_ids) == 0)
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
            touch_ids=sorted(touch_ids),
            sweep_completed=bool(full_sweep and raw_list_count > 0 and not rate_limited),
            rate_limited=rate_limited,
            detail_errors=detail_errors,
        )
