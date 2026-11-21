# -*- coding: utf-8 -*-
"""
数据源 1：特惠采采 / 惠享购 (`https://xhhcds.cn/`)
================================================================================
- 列表接口：`https://xhhcds.cn/api/v1/public/home-products/?feed=home&page={page}&page_size={page_size}`
- 详情接口：`https://xhhcds.cn/api/v1/products/{id}/`
- 特性：
  1. 人工严选 JSON 流，无风控，带完整发单文案、领券短链、下单短链、视频教程；
  2. 全量巡视 (`fetch_mode="full_sweep"`)：翻完所有页，已抓商品仅收进 `touch_ids` 不重抓详情；
  3. 无论在架更新或首次抓取，以本次拉取时间作为 `publish_time` 参与保鲜；
  4. 严格丢弃无购买锚点或无实质说明的空壳单品。
================================================================================
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Set

from infrastructure.logging_utils import get_app_logger
from models.product import detect_platform_signal

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import (
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
    normalize_explicit_platform_code,
)
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .parsing import (
    build_deal_record,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
)

logger = get_app_logger(__name__)


class TehuicaicaiSource(BaseDealSource):
    site_key = "tehuicaicai"
    source_key = "tehuicaicai"
    troubleshooting = {
        "endpoint": "https://xhhcds.cn/api/v1/public/home-products/?feed=home | https://xhhcds.cn/api/v1/products/{id}/",
        "http_403_429": "特惠采采返回限流：请调大 detail_delay_seconds 或降低 max_details。",
        "empty_or_parse": "特惠采采列表返回 0 条：专员非工作时段发单较少，或请检查 JSON 接口字段。",
        "timeout_or_conn": "特惠采采连接超时：接口抖动，下轮将自动重试。",
    }

    BASE_API = "https://xhhcds.cn/api/v1"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        params = ch.params or {}
        channel_mode = str(params.get("channel") or "home").strip()
        category_id = params.get("category_id")
        page_size = int(params.get("page_size") or 50)
        max_pages = max(1, int(ch.max_pages or params.get("max_pages") or 15))
        auto_paginate = bool(params.get("auto_paginate", True))
        full_sweep = ch.fetch_mode == "full_sweep"
        max_details = ctx.effective_max_details
        skip_set = ctx.known_ids

        records: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        touch_ids: Set[str] = set()
        detail_errors: List[Dict[str, Any]] = []
        raw_list_count = 0
        detail_fetched = 0
        detail_attempted = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        pages_crawled = 0
        detail_circuit_broken = False
        rate_limited = False
        reached_end = False
        current_page = int(params.get("page") or 1)
        primary_req_url = ""
        t0 = time.perf_counter()

        while pages_crawled < max_pages:
            if channel_mode == "category" and category_id:
                list_url = f"{self.BASE_API}/public/categories/{category_id}/products/?page={current_page}&page_size={page_size}"
                web_page_url = f"https://xhhcds.cn/#/category?id={category_id}"
                sub_channel = f"category_{category_id}"
            else:
                list_url = f"{self.BASE_API}/public/home-products/?feed=home&page={current_page}&page_size={page_size}"
                web_page_url = "https://xhhcds.cn/"
                sub_channel = "home_products"

            if not primary_req_url:
                primary_req_url = list_url

            headers = {
                **DEFAULT_HEADERS,
                "Accept": "application/json",
                "Referer": web_page_url,
                "X-Request-ID": f"fetch-thcc-{int(time.time() * 1000)}-{current_page}",
            }

            resp = None
            try:
                resp = request_with_retry(ctx.session, "get", list_url, headers=headers, timeout=ctx.timeout)
                if resp.status_code != 200:
                    if is_rate_limit_status(resp.status_code):
                        rate_limited = True
                    if pages_crawled == 0:
                        return FetchResult(
                            status="error",
                            http_status=resp.status_code,
                            request_url=list_url,
                            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                            rate_limited=rate_limited,
                            detail_errors=[{"error": f"HTTP {resp.status_code}", "http_status": resp.status_code}],
                        )
                    break
                body = resp.json()
            except Exception as exc:
                if pages_crawled == 0:
                    return FetchResult(
                        status="error",
                        http_status=getattr(resp, "status_code", None),
                        request_url=list_url,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                        detail_errors=[{"error": str(exc), "error_type": type(exc).__name__}],
                    )
                break

            data_block = body.get("data") if isinstance(body, dict) else {}
            if not isinstance(data_block, dict):
                data_block = {}

            raw_items = data_block.get("items") or []
            if not isinstance(raw_items, list) or not raw_items:
                reached_end = pages_crawled > 0
                break

            pagination = data_block.get("pagination") or {}
            pages_crawled += 1
            new_items_on_page = 0

            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "").strip()
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                raw_list_count += 1

                raw_plat_code = str(item.get("platform_code") or "").strip().lower()
                platform_code = normalize_explicit_platform_code(raw_plat_code)
                platform_name = str(
                    item.get("platform_name")
                    or ("京东" if platform_code == "jd" else ("拼多多" if platform_code == "pdd" else ("淘宝" if platform_code == "taobao" else "")))
                ).strip()

                if is_confirmed_unsupported_deal(platform_code=raw_plat_code, mall_name=platform_name):
                    skipped_unsupported += 1
                    continue

                if item_id in skip_set:
                    skipped_known += 1
                    if full_sweep:
                        touch_ids.add(item_id)
                    continue

                new_items_on_page += 1
                if detail_circuit_broken or detail_attempted >= max_details:
                    if not full_sweep:
                        break
                    continue

                if detail_attempted > 0:
                    self.sleep_detail_delay(ctx)
                detail_attempted += 1

                title = str(item.get("title") or "").strip()
                display_price = str(item.get("display_price") or "").strip()
                cover_info = item.get("cover_image") or {}
                image_url = ""
                if isinstance(cover_info, dict):
                    image_url = str(cover_info.get("public_url") or cover_info.get("direct_public_url") or "").strip()

                source_url = f"https://xhhcds.cn/#/product/{item_id}"
                source_detail_url = f"{self.BASE_API}/products/{item_id}/"
                hot_metrics: Dict[str, Any] = {
                    "sold_count": item.get("sold_count"),
                    "badge_text": item.get("badge_text"),
                    "platform_code": platform_code or raw_plat_code,
                    "sub_channel": sub_channel,
                }

                try:
                    det_headers = {
                        **DEFAULT_HEADERS,
                        "Accept": "application/json",
                        "Referer": source_url,
                        "X-Request-ID": f"fetch-thcc-det-{item_id}-{int(time.time() * 1000)}",
                    }
                    r_det = request_with_retry(ctx.session, "get", source_detail_url, headers=det_headers, timeout=ctx.timeout)
                    if r_det.status_code != 200:
                        if is_rate_limit_status(r_det.status_code):
                            detail_circuit_broken = True
                            rate_limited = True
                        skipped_no_detail += 1
                        detail_errors.append({
                            "item_id": item_id,
                            "error_url": source_detail_url,
                            "http_status": r_det.status_code,
                            "error_type": f"HTTP_{r_det.status_code}",
                            "error": f"详情页返回状态码: {r_det.status_code}",
                        })
                        continue

                    det_body = r_det.json()
                    det = det_body.get("data") if isinstance(det_body, dict) else {}
                    if not isinstance(det, dict) or not det:
                        skipped_no_detail += 1
                        continue

                    det_plat_raw = str(det.get("platform_code") or raw_plat_code).strip().lower()
                    det_plat = normalize_explicit_platform_code(det_plat_raw)
                    det_plat_name = str(det.get("platform_name") or platform_name).strip()
                    if is_confirmed_unsupported_deal(platform_code=det_plat_raw, mall_name=det_plat_name):
                        skipped_unsupported += 1
                        continue

                    mi = det.get("material_info") if isinstance(det.get("material_info"), dict) else {}
                    cmd_text = str(mi.get("command_text") or "").strip()
                    second_cmd = str(mi.get("second_command_text") or "").strip()
                    cmd_instr = str(mi.get("command_instruction_text") or "").strip()
                    qr_instr = str(mi.get("qr_instruction_text") or "").strip()
                    purchase_url = str(mi.get("purchase_url") or "").strip()
                    coupon_urls = [str(u).strip() for u in (mi.get("coupon_urls") or []) if str(u or "").strip()]
                    qr_img = str(mi.get("qr_image_url") or "").strip()
                    second_qr_img = str(mi.get("second_qr_image_url") or "").strip()
                    desc_html = str(det.get("description_html") or "").strip()
                    desc_plain = str(det.get("description") or "").strip()
                    review_html = str(det.get("review_detail_content") or "").strip()

                    provider = str(det.get("provider_nickname") or "").strip()
                    if provider:
                        hot_metrics["provider_nickname"] = provider
                    upstream_time = str(det.get("updated_at") or det.get("created_at") or "").strip()
                    if upstream_time:
                        hot_metrics["upstream_time"] = upstream_time

                    video_url = ""
                    tutorials = det.get("tutorials") or []
                    if isinstance(tutorials, list):
                        for tut in tutorials:
                            if isinstance(tut, dict) and tut.get("video_url"):
                                video_url = str(tut["video_url"]).strip()
                                hot_metrics["video_tutorial_url"] = video_url
                                break

                    full_text_parts: List[str] = []
                    if cmd_text:
                        full_text_parts.append(re.sub(r"(https?://u\.jd\.com)///+", r"\1/", cmd_text))
                    if second_cmd and second_cmd != cmd_text:
                        full_text_parts.append(re.sub(r"(https?://u\.jd\.com)///+", r"\1/", second_cmd))
                    if cmd_instr and cmd_instr not in "\n".join(full_text_parts):
                        full_text_parts.append(cmd_instr)
                    if qr_instr and qr_instr not in "\n".join(full_text_parts):
                        full_text_parts.append(qr_instr)

                    if not full_text_parts:
                        if desc_html:
                            desc_clean = html_to_tutorial_text(desc_html, base_url=source_url)
                            if desc_clean:
                                full_text_parts.append(desc_clean)
                        elif review_html:
                            rev_clean = html_to_tutorial_text(review_html, base_url=source_url)
                            if rev_clean:
                                full_text_parts.append(rev_clean)
                        elif desc_plain:
                            full_text_parts.append(desc_plain)

                    if not (full_text_parts or video_url or qr_img or second_qr_img):
                        skipped_no_detail += 1
                        continue

                    combined_tutorial_text = "\n".join(full_text_parts).strip()
                    if not combined_tutorial_text and (video_url or qr_img):
                        combined_tutorial_text = f"【{det_plat_name or '特惠采采'}】{title}\n到手价: {display_price}".strip()
                    extra_raw_refs = [
                        raw_ref
                        for raw_ref in (*coupon_urls, purchase_url)
                        if raw_ref and raw_ref not in combined_tutorial_text
                    ]
                    if extra_raw_refs:
                        combined_tutorial_text = (
                            f"{combined_tutorial_text}\n" + "\n".join(extra_raw_refs)
                            if combined_tutorial_text
                            else "\n".join(extra_raw_refs)
                        ).strip()

                    links, imgs, tokens = extract_links_images_and_tokens(combined_tutorial_text, base_url=source_url)
                    seen_link_urls = {str(lk.get("url") or "") for lk in links}

                    def _append_extra_url(raw_val: str, *, label: str, link_type: str) -> None:
                        sub_links, _, sub_tokens = extract_links_images_and_tokens(raw_val, base_url=source_url)
                        for clk in sub_links:
                            u_ext = str(clk.get("url") or "").strip()
                            if u_ext and u_ext not in seen_link_urls:
                                seen_link_urls.add(u_ext)
                                links.append({
                                    "label": label,
                                    "url": u_ext,
                                    "type": link_type,
                                    "platform": detect_platform_signal(u_ext) or det_plat or "unknown",
                                })
                        for tk in sub_tokens:
                            if tk and tk not in tokens:
                                tokens.append(tk)
                        if raw_val.startswith(("http://", "https://")) and raw_val not in seen_link_urls:
                            seen_link_urls.add(raw_val)
                            links.append({
                                "label": label,
                                "url": raw_val,
                                "type": link_type,
                                "platform": detect_platform_signal(raw_val) or det_plat or "unknown",
                            })

                    for cu in coupon_urls:
                        _append_extra_url(cu, label="特惠采采领券链接", link_type="coupon_url")
                    if purchase_url:
                        _append_extra_url(purchase_url, label="特惠采采下单短链", link_type="purchase_url")

                    all_imgs: List[str] = [image_url] if image_url else []
                    for extra_img in [*imgs, qr_img, second_qr_img]:
                        if extra_img and extra_img not in all_imgs:
                            all_imgs.append(extra_img)

                    if is_confirmed_unsupported_deal(
                        platform_code=det_plat_raw,
                        mall_name=det_plat_name,
                        links=links,
                        tokens=tokens,
                    ):
                        skipped_unsupported += 1
                        continue

                    rec = build_deal_record(
                        source_site="tehuicaicai",
                        source_channel=ch.name,
                        source_item_id=item_id,
                        source_url=source_url,
                        source_list_page_url=list_url,
                        source_detail_url=source_detail_url,
                        title=str(det.get("title") or title).strip() or title,
                        price_text=str(det.get("display_price") or display_price).strip(),
                        image_url=image_url,
                        mall_name=det_plat_name,
                        publish_ts=time.time(),
                        explicit_platform=det_plat or None,
                        hot_metrics=hot_metrics,
                        tutorial_text=combined_tutorial_text,
                        tutorial_images=all_imgs,
                        extracted_links=links,
                        extracted_tokens=tokens,
                        raw_payload=det,
                        has_full_detail=True,
                    )
                    if not has_valid_detail_and_tutorial(rec):
                        skipped_no_detail += 1
                        logger.warning("  ⚠️ [特惠采采-无购买锚点跳过] id=%s | title=%s", item_id, title[:32])
                        continue

                    detail_fetched += 1
                    records.append(rec)
                except Exception as exc_det:
                    skipped_no_detail += 1
                    detail_errors.append({
                        "item_id": item_id,
                        "error_url": source_detail_url,
                        "http_status": None,
                        "error_type": type(exc_det).__name__,
                        "error": str(exc_det)[:160],
                    })

            if not auto_paginate:
                break
            if not full_sweep and new_items_on_page == 0:
                break
            if detail_circuit_broken and not full_sweep:
                break
            if not full_sweep and detail_attempted >= max_details:
                break
            if not pagination.get("has_next", True):
                reached_end = True
                break
            current_page += 1
            self.sleep_detail_delay(ctx)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = pages_crawled > 0 and not (rate_limited and len(records) == 0 and len(touch_ids) == 0)
        return FetchResult(
            status="ok" if is_ok else "error",
            http_status=200 if pages_crawled > 0 else None,
            request_url=primary_req_url or f"{self.BASE_API}/public/home-products/?feed=home&page=1&page_size={page_size}",
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
