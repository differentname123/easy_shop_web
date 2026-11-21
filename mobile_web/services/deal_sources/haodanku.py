# -*- coding: utf-8 -*-
"""
数据源 3：好单库官方实时线报 API (`https://v2.api.haodanku.com/wire_report_new`)
================================================================================
- 支持三大榜单：`label_id=10003` 淘宝实时线报、`10004` 京东实时线报、`10005` 淘宝百亿补贴；
- 纯 JSON 游标分页（`min_id`）全量拉取，一次直出完整图/文/链，零二次详情请求；
- 飞猪 (`platform="3"`) 100% 确定性过滤；已抓 ID 纳入 `touch_ids` 刷新在架确认时间；
- 时间戳以本次拉取时间为准（`utc_now_iso()`），原始 `starttime` 仅保留在 `hot_metrics`。
================================================================================
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Set

from models.product import extract_all_tokens

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import (
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
    normalize_explicit_platform_code,
)
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .parsing import (
    build_deal_record,
    extract_first_price,
    utc_now_iso,
)


def _resolve_hdk_apikey(explicit_key: Optional[str] = None) -> str:
    key = str(explicit_key or "").strip()
    if key:
        return key
    try:
        from infrastructure.settings import get_settings

        key = str(get_settings().hdk_api_key or "").strip()
    except Exception:
        pass
    return key


class HaodankuSource(BaseDealSource):
    site_key = "haodanku"
    source_key = "haodanku"
    troubleshooting = {
        "endpoint": "https://v2.api.haodanku.com/wire_report_new",
        "http_403_429": "好单库 API 触发频控：请调大 interval_seconds 或检查 haodanku_api_key。",
        "empty_or_parse": "好单库返回空列表：请核查接口字段或 apikey 权限。",
        "timeout_or_conn": "好单库请求超时：下轮自动重试。",
    }

    BASE_URL = "https://v2.api.haodanku.com/wire_report_new"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        params = ch.params or {}
        label_id = int(params.get("label_id") or 10003)
        page_size = int(params.get("page_size") or 100)
        max_pages = max(1, int(ch.max_pages or params.get("max_pages") or 30))
        full_sweep = ch.fetch_mode == "full_sweep"
        eff_key = _resolve_hdk_apikey(params.get("apikey"))
        skip_set = ctx.known_ids

        records: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        touch_ids: Set[str] = set()
        raw_list_count = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        rate_limited = False
        primary_req_url = ""
        pages_crawled = 0
        last_http_status: Optional[int] = None
        hit_cursor_end = False
        min_id: Any = 1
        t0 = time.perf_counter()

        while pages_crawled < max_pages:
            req_url = f"{self.BASE_URL}?apikey={eff_key}&label_id={label_id}&min_size={page_size}&min_id={min_id}"
            if not primary_req_url:
                primary_req_url = req_url

            resp = None
            try:
                resp = request_with_retry(ctx.session, "get", req_url, headers=DEFAULT_HEADERS, timeout=ctx.timeout)
                last_http_status = getattr(resp, "status_code", None)
                if resp.status_code != 200:
                    if is_rate_limit_status(resp.status_code):
                        rate_limited = True
                    if pages_crawled == 0:
                        return FetchResult(
                            status="error",
                            http_status=resp.status_code,
                            request_url=req_url,
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
                        request_url=req_url,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                        detail_errors=[{"error": str(exc), "error_type": type(exc).__name__}],
                    )
                break

            code = body.get("code")
            if code != 200:
                sub_msg = str(body.get("sub_msg") or body.get("msg") or "")
                if any(kw in sub_msg for kw in ("频次", "频率", "超限", "上限", "频繁", "请稍后")):
                    rate_limited = True
                if pages_crawled == 0 and not isinstance(body.get("data"), list) and rate_limited:
                    return FetchResult(
                        status="error",
                        http_status=last_http_status,
                        request_url=req_url,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                        rate_limited=True,
                        detail_errors=[{"error": sub_msg or f"code={code}", "http_status": last_http_status}],
                    )
                hit_cursor_end = pages_crawled > 0
                break

            items = body.get("data")
            if not isinstance(items, list) or not items:
                hit_cursor_end = pages_crawled > 0
                break

            pages_crawled += 1
            for item in items:
                if not isinstance(item, dict):
                    continue
                wire_id = str(item.get("wire_id") or "").strip()
                if not wire_id or wire_id in seen_ids:
                    continue
                seen_ids.add(wire_id)
                raw_list_count += 1

                plat_raw = str(item.get("platform") or "").strip()
                if plat_raw == "1":
                    platform_code, platform_name = "taobao", "淘宝"
                elif plat_raw == "2":
                    platform_code, platform_name = "jd", "京东"
                elif plat_raw == "3":
                    skipped_unsupported += 1
                    continue
                else:
                    platform_code = normalize_explicit_platform_code(plat_raw)
                    platform_name = (
                        "京东"
                        if platform_code == "jd"
                        else ("淘宝" if platform_code == "taobao" else ("拼多多" if platform_code == "pdd" else ""))
                    )

                if is_confirmed_unsupported_deal(platform_code=platform_code, mall_name=platform_name):
                    skipped_unsupported += 1
                    continue

                if full_sweep:
                    touch_ids.add(wire_id)

                if wire_id in skip_set:
                    skipped_known += 1
                    continue

                img_list: List[str] = []
                for raw_img in item.get("images") or []:
                    if isinstance(raw_img, str):
                        for sub_img in raw_img.split(","):
                            c_img = sub_img.strip()
                            if c_img.startswith("http") and c_img not in img_list:
                                img_list.append(c_img)

                content_lines: List[str] = []
                extracted_links: List[Dict[str, Any]] = []
                extracted_tokens: List[str] = []

                for sub in item.get("list") or []:
                    if not isinstance(sub, dict):
                        continue
                    c_text = str(sub.get("content") or "").strip()
                    if c_text:
                        content_lines.append(c_text)

                    sub_item_id = str(sub.get("item_id") or "").strip()
                    if sub_item_id:
                        if sub_item_id.startswith(("http://", "https://")):
                            extracted_links.append({"label": "商品/活动链接", "url": sub_item_id, "type": "item_url", "platform": platform_code or "unknown"})
                        elif sub_item_id.isdigit():
                            if platform_code == "taobao":
                                extracted_links.append({"label": "淘宝商品", "url": f"https://item.taobao.com/item.htm?id={sub_item_id}", "type": "tb_numeric", "platform": "taobao"})
                            elif platform_code == "jd":
                                extracted_links.append({"label": "京东商品", "url": f"https://item.jd.com/{sub_item_id}.html", "type": "jd_numeric", "platform": "jd"})
                            else:
                                extracted_links.append({"label": "商品ID", "url": sub_item_id, "type": "item_id", "platform": platform_code or "unknown"})
                        elif re.match(r"^[A-Za-z0-9_-]{8,}$", sub_item_id):
                            if platform_code == "taobao":
                                extracted_links.append({"label": "淘宝二合一", "url": f"https://uland.taobao.com/item/edetail?id={sub_item_id}", "item_id": sub_item_id, "type": "tb_encrypted_id", "platform": "taobao"})
                            else:
                                extracted_links.append({"label": "加密ID", "url": sub_item_id, "type": "encrypted_id", "platform": platform_code or "unknown"})
                        elif extract_all_tokens(sub_item_id):
                            extracted_tokens.append(sub_item_id)

                    coupon_url = str(sub.get("coupon_url") or "").strip()
                    if coupon_url.startswith(("http://", "https://")):
                        extracted_links.append({"label": "优惠券", "url": coupon_url, "type": "coupon_url", "platform": platform_code or "unknown"})

                if not extracted_links and not extracted_tokens:
                    skipped_no_detail += 1
                    continue

                tutorial_text = "\n".join(content_lines).strip()
                title = ""
                for line in content_lines:
                    clean_line = re.sub(
                        r"^(?:豪车❗|速[‼️!]|先领券|拍下|\d+亓[‼️!]?|特价|折\d+.*?[‼️!]|Bug[‼️!]|🎀速[‼️!]|🎀|⚠️速[‼️!])\s*",
                        "",
                        line,
                    ).strip()
                    if len(clean_line) >= 4 and not clean_line.startswith(("http", "①", "②", "③", "1、", "2、", "3、", "---")):
                        title = clean_line
                        break
                if not title and content_lines:
                    title = content_lines[0]
                if not title:
                    title = f"{platform_name}精选好价商品"

                price, price_text = extract_first_price("", title, tutorial_text)

                hot_metrics: Dict[str, Any] = {
                    "wire_id": wire_id,
                    "cate_id": item.get("cate_id"),
                    "label_id": label_id,
                    "platform_raw": plat_raw,
                }
                if item.get("starttime"):
                    hot_metrics["upstream_starttime"] = item.get("starttime")

                rec = build_deal_record(
                    source_site="haodanku",
                    source_channel=ch.name,
                    source_item_id=wire_id,
                    source_url="https://www.haodanku.com/activity/tip_off",
                    source_list_page_url=primary_req_url,
                    source_detail_url=primary_req_url,
                    title=title,
                    price_text=price_text or "",
                    price=price,
                    image_url=img_list[0] if img_list else "",
                    mall_name=platform_name,
                    publish_time=utc_now_iso(),
                    explicit_platform=platform_code,
                    hot_metrics=hot_metrics,
                    tutorial_text=tutorial_text,
                    tutorial_images=img_list,
                    extracted_links=extracted_links,
                    extracted_tokens=extracted_tokens,
                    raw_payload=item,
                    has_full_detail=True,
                )
                if not has_valid_detail_and_tutorial(rec):
                    skipped_no_detail += 1
                    continue
                records.append(rec)

            next_min_id = body.get("min_id")
            if not next_min_id or next_min_id == min_id:
                hit_cursor_end = True
                break
            min_id = next_min_id

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = pages_crawled > 0 and not (rate_limited and len(records) == 0 and len(touch_ids) == 0)
        return FetchResult(
            status="ok" if is_ok else "error",
            http_status=last_http_status or 200,
            request_url=primary_req_url or self.BASE_URL,
            elapsed_ms=elapsed_ms,
            records=records,
            raw_list_count=raw_list_count,
            detail_fetched=len(records),
            skipped_known=skipped_known,
            skipped_unsupported=skipped_unsupported,
            skipped_no_detail=skipped_no_detail,
            pages_crawled=pages_crawled,
            touch_ids=sorted(touch_ids),
            sweep_completed=bool(full_sweep and hit_cursor_end and not rate_limited),
            rate_limited=rate_limited,
        )
