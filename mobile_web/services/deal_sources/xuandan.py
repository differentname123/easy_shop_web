# -*- coding: utf-8 -*-
"""
数据源 4：选单网全站排行榜 (`https://www.xuandan.com/allList.html`)
================================================================================
- 支持三大监控频道：
  - `type=8`: 淘客精选社群素材文案榜（约 202 条 / 3 页）
  - `type=7&is_hidden=1`: 隐藏券实时销量榜
  - `type=11`: 低价实时销量榜
- 极简高效原理：
  列表页 HTML 内的 `<div class="liLayer hide" id="go_{id}">...</div>` 已直接包含完整发单文案、
  原价/券后价、领券链 (`uland.taobao.com/quan/detail?...`)、商品二合一加密链
  (`uland.taobao.com/item/edetail?id=...`) 与主图，无需发起任何二次详情请求！
- 翻页保护：遇到空列表或本页 0 新 ID（防末页回环重复）立即停止；全量模式且采用拉取时间。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any, Dict, List, Set

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import SOURCE_SELF_HOSTS, has_valid_detail_and_tutorial
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .parsing import (
    build_deal_record,
    extract_balanced_tag,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
)


def _extract_xuandan_title(tutorial_text: str, item_id: str) -> str:
    for raw_line in tutorial_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("原价", "领券:", "领券：", "抢购:", "抢购：", "下单:", "下单：", "http", "[教程图片")):
            continue
        cleaned = re.sub(r"https?://\S+", "", line).strip()
        if len(cleaned) >= 3:
            return cleaned[:120]
    return f"选单网精选商品 #{item_id}"


class XuandanSource(BaseDealSource):
    site_key = "xuandan"
    source_key = "xuandan"
    troubleshooting = {
        "endpoint": "https://www.xuandan.com/allList.html?type=8|7|11",
        "http_403_429": "选单网返回限流：请调大 interval_seconds。",
        "empty_or_parse": "选单网未匹配到 #go_{id} 文案块：请检查页面模板是否改版。",
        "timeout_or_conn": "选单网请求超时：页面较大（~800KB），下轮将自动重试。",
    }

    BASE_URL = "https://www.xuandan.com"

    def _build_page_url(self, type_id: int, is_hidden: Any, page: int) -> str:
        parts = [f"type={type_id}"]
        if is_hidden not in (None, "", 0, False):
            parts.append(f"is_hidden={int(is_hidden)}")
        if page > 1:
            parts.append(f"pageid={page}")
        return f"{self.BASE_URL}/allList.html?{'&'.join(parts)}"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        params = ch.params or {}
        type_id = int(params.get("type_id") or 8)
        is_hidden = params.get("is_hidden")
        max_pages = max(1, int(ch.max_pages or params.get("max_pages") or 10))
        full_sweep = ch.fetch_mode == "full_sweep"
        skip_set = ctx.known_ids

        records: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        touch_ids: Set[str] = set()
        raw_list_count = 0
        skipped_known = 0
        skipped_no_detail = 0
        pages_crawled = 0
        rate_limited = False
        reached_end = False
        last_http_status = None
        primary_url = self._build_page_url(type_id, is_hidden, 1)
        eff_timeout = max(20.0, float(ctx.timeout))
        t0 = time.perf_counter()

        for page in range(1, max_pages + 1):
            page_url = self._build_page_url(type_id, is_hidden, page)
            resp = None
            try:
                resp = request_with_retry(
                    ctx.session,
                    "get",
                    page_url,
                    headers={**DEFAULT_HEADERS, "Referer": primary_url},
                    timeout=eff_timeout,
                )
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
            go_ids = list(dict.fromkeys(re.findall(r'<div\b[^>]*\bid=["\']go_(\d+)["\']', html_text, re.I)))
            if not go_ids:
                reached_end = pages_crawled > 1
                break

            unseen_on_page = 0
            for gid in go_ids:
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                unseen_on_page += 1
                raw_list_count += 1

                if full_sweep:
                    touch_ids.add(gid)

                if gid in skip_set:
                    skipped_known += 1
                    continue

                div_html = extract_balanced_tag(html_text, "div", rf'\bid=["\']go_{re.escape(gid)}["\']')
                if not div_html:
                    skipped_no_detail += 1
                    continue

                # 从列表卡片外部有限切片提取主图，优先 data-original 避开 GIF16.gif 占位图
                img_url = ""
                marker = f"/Detail.html?id={gid}"
                marker_pos = html_text.find(marker)
                if marker_pos != -1:
                    card_window = html_text[marker_pos : marker_pos + 700]
                    next_pos = card_window.find("/Detail.html?id=", len(marker))
                    if next_pos != -1:
                        card_window = card_window[:next_pos]
                    m_orig = re.search(r'data-original=["\']([^"\']+)["\']', card_window, re.I)
                    if not m_orig:
                        m_orig = re.search(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', card_window, re.I)
                    if m_orig:
                        cand_img = html_lib.unescape(m_orig.group(1)).strip()
                        if cand_img and "GIF16.gif" not in cand_img and not cand_img.startswith("data:"):
                            img_url = ("https:" + cand_img) if cand_img.startswith("//") else cand_img

                # 去掉内部的 <img ...> 占位，保留清晰纯文案与领券/下单链接
                div_no_img = re.sub(r"<img\b[^>]*>", "", div_html, flags=re.I)
                links, inner_imgs, tokens = extract_links_images_and_tokens(
                    div_html, base_url=self.BASE_URL, exclude_hosts=SOURCE_SELF_HOSTS
                )
                all_imgs: List[str] = [img_url] if img_url else []
                for im in inner_imgs:
                    if im and im not in all_imgs:
                        all_imgs.append(im)
                if not img_url and all_imgs:
                    img_url = all_imgs[0]

                tutorial_text = html_to_tutorial_text(div_no_img, base_url=self.BASE_URL, keep_images=False).strip()
                title = _extract_xuandan_title(tutorial_text, gid)
                detail_url = f"{self.BASE_URL}/Detail.html?id={gid}"

                rec = build_deal_record(
                    source_site="xuandan",
                    source_channel=ch.name,
                    source_item_id=gid,
                    source_url=detail_url,
                    source_list_page_url=page_url,
                    source_detail_url=detail_url,
                    title=title,
                    image_url=img_url,
                    mall_name="淘宝/天猫",
                    publish_ts=time.time(),
                    explicit_platform="taobao",
                    hot_metrics={"type_id": type_id, "is_hidden": bool(is_hidden)},
                    tutorial_text=tutorial_text,
                    tutorial_images=all_imgs,
                    extracted_links=links,
                    extracted_tokens=tokens,
                    raw_payload=div_html,
                    has_full_detail=True,
                )
                if not has_valid_detail_and_tutorial(rec):
                    skipped_no_detail += 1
                    continue
                records.append(rec)

            if unseen_on_page == 0:
                reached_end = True
                break
            if page < max_pages:
                self.sleep_detail_delay(ctx)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = pages_crawled > 0 and not (rate_limited and len(records) == 0 and len(touch_ids) == 0)
        return FetchResult(
            status="ok" if is_ok else "error",
            http_status=last_http_status or 200,
            request_url=primary_url,
            elapsed_ms=elapsed_ms,
            records=records,
            raw_list_count=raw_list_count,
            detail_fetched=len(records),
            skipped_known=skipped_known,
            skipped_no_detail=skipped_no_detail,
            pages_crawled=pages_crawled,
            touch_ids=sorted(touch_ids),
            sweep_completed=bool(full_sweep and reached_end and not rate_limited),
            rate_limited=rate_limited,
        )
