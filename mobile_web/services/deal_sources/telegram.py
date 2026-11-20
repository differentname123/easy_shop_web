# -*- coding: utf-8 -*-
"""
数据源 7/13/14：Telegram 公开频道网页流 (`https://t.me/s/{tg_channel}`)
================================================================================
- 支持频道：
  - `zzzdm`       -> 获取最近 1 小时数据 (`lookback_seconds=3600`)，30 分钟一次
  - `BaccanoSoul` -> 获取最近 15 分钟数据 (`lookback_seconds=900`)，10 分钟一次
  - `Pojieapp`    -> 获取最近 15 分钟数据 (`lookback_seconds=900`)，10 分钟一次
- 核心工程要点：
  1. `default_network_mode = "proxy"`：自动走系统/环境变量代理访问 `t.me/s/...`；
  2. 基于 `?before={id}` 向上回溯翻页，直至页内最旧消息早于 `cutoff_ts` 或触及 `max_pages`；
  3. 仅提取消息正文图片 (`tgme_widget_message_photo_wrap`)，绝不把频道头像误作商品图；
  4. 严格执行 100% 确定非淘京拼过滤（自动剔除网盘/软件/限免 APP 情报）与非空壳门禁
     （剔除纯文字提醒、无购买链接/口令的降级空壳消息）。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import (
    SOURCE_SELF_HOSTS,
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
    is_host_match,
)
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .parsing import (
    TOKEN_RE,
    build_deal_record,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
)


def _parse_iso_epoch(dt_str: str) -> Optional[float]:
    s = str(dt_str or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _extract_tg_title(clean_text: str, tg_channel: str, msg_id: str) -> str:
    for raw_line in clean_text.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        if ln.startswith(("http://", "https://", "价格：", "商家：", "值率：", "领券：", "---", "【🛒")):
            continue
        if TOKEN_RE.fullmatch(ln):
            continue
        cleaned = re.sub(r"https?://\S+", "", ln).strip(" -|")
        if len(cleaned) >= 3:
            return cleaned[:120]
    return f"TG_{tg_channel} #{msg_id}"


class TelegramChannelSource(BaseDealSource):
    site_key = "telegram"
    source_key = "telegram"
    default_network_mode = "proxy"
    troubleshooting = {
        "endpoint": "https://t.me/s/{tg_channel}",
        "http_403_429": "Telegram 网页预览限流：请调大 interval_seconds。",
        "empty_or_parse": "Telegram 未解析到消息或时间窗内无新消息：请核对频道名或代理连通性。",
        "timeout_or_conn": "访问 t.me 超时或代理异常：请检查本地代理服务（如 127.0.0.1:7890）是否开启。",
    }

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ch = ctx.channel
        params = ch.params or {}
        tg_channel = str(params.get("tg_channel") or "").strip()
        if not tg_channel:
            return FetchResult(
                status="error",
                http_status=None,
                request_url=ch.default_url,
                elapsed_ms=0.0,
                detail_errors=[{"error": "缺少 params.tg_channel 配置"}],
            )

        max_pages = max(1, int(ch.max_pages or 5))
        cutoff_ts = ctx.cutoff_ts
        skip_set = ctx.known_ids

        records: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        raw_list_count = 0
        skipped_known = 0
        skipped_unsupported = 0
        skipped_no_detail = 0
        skipped_out_of_window = 0
        pages_crawled = 0
        rate_limited = False
        last_http_status: Optional[int] = None
        next_url: Optional[str] = f"https://t.me/s/{tg_channel}"
        primary_url = next_url
        t0 = time.perf_counter()

        while next_url and pages_crawled < max_pages:
            resp = None
            try:
                resp = request_with_retry(
                    ctx.session,
                    "get",
                    next_url,
                    headers=DEFAULT_HEADERS,
                    timeout=max(15.0, float(ctx.timeout)),
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
                            request_url=next_url,
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
                        request_url=next_url,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                        detail_errors=[{"error": str(exc), "error_type": type(exc).__name__}],
                    )
                break

            pages_crawled += 1
            msg_blocks = re.findall(
                r'(<div\s+class="tgme_widget_message_wrap[^"]*"[^>]*>.*?)(?=<div\s+class="tgme_widget_message_wrap|</section>)',
                html_text,
                re.S,
            )
            if not msg_blocks:
                break

            page_oldest_ts: Optional[float] = None
            current_page_url = next_url

            for blk in msg_blocks:
                m_post = re.search(r'data-post="([^"]+)"', blk)
                if not m_post:
                    continue
                post_ref = m_post.group(1).strip()
                msg_id = post_ref.split("/", 1)[-1].strip()
                global_item_id = f"{tg_channel}_{msg_id}"
                if global_item_id in seen_ids:
                    continue
                seen_ids.add(global_item_id)
                raw_list_count += 1

                m_dt = re.search(r'<time[^>]+datetime="([^"]+)"', blk)
                dt_iso = m_dt.group(1).strip() if m_dt else ""
                msg_ts = _parse_iso_epoch(dt_iso)
                if msg_ts is not None:
                    if page_oldest_ts is None or msg_ts < page_oldest_ts:
                        page_oldest_ts = msg_ts
                    if cutoff_ts is not None and msg_ts < cutoff_ts:
                        skipped_out_of_window += 1
                        continue

                if global_item_id in skip_set:
                    skipped_known += 1
                    continue

                m_txt = re.search(
                    r'<div\s+class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                    blk,
                    re.S,
                )
                txt_html = m_txt.group(1) if m_txt else ""
                if not txt_html:
                    skipped_no_detail += 1
                    continue

                # 提取消息内外部链接（剔除 t.me 与源站自身链接）与口令
                links, _, tokens = extract_links_images_and_tokens(
                    txt_html,
                    base_url="https://t.me",
                    exclude_hosts=SOURCE_SELF_HOSTS,
                )

                # 同时检查是否存在指向 m.zzzdm.com 的评论/详情参考地址
                source_detail_url = f"https://t.me/{post_ref}"
                m_zzz_ref = re.search(r'href=["\'](https?://m\.zzzdm\.com/[^"\']+)["\']', txt_html, re.I)
                if m_zzz_ref:
                    source_detail_url = html_lib.unescape(m_zzz_ref.group(1)).strip()

                # 仅提取正文配图（忽略频道头像 tgme_widget_message_user_photo）
                images: List[str] = []
                for m_photo in re.finditer(
                    r'class="[^"]*tgme_widget_message_photo_wrap[^"]*"[^>]*style="[^"]*background-image:url\(\'([^\']+)\'\)',
                    blk,
                    re.S,
                ):
                    im_u = html_lib.unescape(m_photo.group(1)).strip()
                    if im_u and im_u not in images:
                        images.append(im_u)

                tutorial_text = html_to_tutorial_text(txt_html, base_url="https://t.me", keep_images=False)
                # 清理 TG 机器人常见的降级前缀占位，保留所有有效商品描述、步骤与链接
                tutorial_text = re.sub(r"^(?:\[消息降级\]\s*|\[img\]\s*)+", "", tutorial_text, flags=re.M).strip()
                # 剥离 m.zzzdm.com 的 【🛒 立即购买】【💬 查看评论】 无效装饰尾行
                tutorial_text = re.sub(r"\n?-{10,}\n【🛒\s*立即购买】.*$", "", tutorial_text, flags=re.S).strip()

                mall_name = ""
                m_mall = re.search(r"商家：\s*\[([^\]]+)\]", tutorial_text)
                if m_mall:
                    mall_name = m_mall.group(1).strip()

                if is_confirmed_unsupported_deal(mall_name=mall_name, links=links, tokens=tokens):
                    skipped_unsupported += 1
                    continue

                # 也过滤正文中虽被 exclude_hosts 过滤掉、但外链指向已知软件/网盘域的纯资源帖
                raw_ext_urls = re.findall(r'href="(https?://[^"]+)"', txt_html)
                if raw_ext_urls and not links and not tokens:
                    # 仅包含 t.me 或内部参考链接而无电商购买链 -> 空壳跳过
                    non_self_urls = [
                        u for u in raw_ext_urls
                        if not any(is_host_match((urlsplit(u).hostname or "").lower(), sh) for sh in SOURCE_SELF_HOSTS)
                    ]
                    if not non_self_urls:
                        skipped_no_detail += 1
                        continue

                title = _extract_tg_title(tutorial_text, tg_channel, msg_id)

                rec = build_deal_record(
                    source_site="telegram",
                    source_channel=ch.name,
                    source_item_id=global_item_id,
                    source_url=f"https://t.me/{post_ref}",
                    source_list_page_url=current_page_url,
                    source_detail_url=source_detail_url,
                    title=title,
                    image_url=images[0] if images else "",
                    mall_name=mall_name,
                    publish_time=dt_iso,
                    hot_metrics={"tg_channel": tg_channel, "msg_id": msg_id},
                    tutorial_text=tutorial_text,
                    tutorial_images=images,
                    extracted_links=links,
                    extracted_tokens=tokens,
                    raw_payload=txt_html,
                    has_full_detail=True,
                )
                if not has_valid_detail_and_tutorial(rec):
                    skipped_no_detail += 1
                    continue

                records.append(rec)

            # 若本页最旧消息已经早于时间窗口，则停止继续向前翻页
            if cutoff_ts is not None and page_oldest_ts is not None and page_oldest_ts < cutoff_ts:
                break

            m_before = re.search(rf'href="(/s/{re.escape(tg_channel)}\?before=\d+)"', html_text)
            if not m_before:
                break
            candidate_next = "https://t.me" + m_before.group(1)
            if candidate_next == current_page_url:
                break
            next_url = candidate_next
            if pages_crawled < max_pages:
                self.sleep_detail_delay(ctx)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = pages_crawled > 0 and not (rate_limited and len(records) == 0)
        return FetchResult(
            status="ok" if is_ok else "error",
            http_status=last_http_status or 200,
            request_url=primary_url,
            elapsed_ms=elapsed_ms,
            records=records,
            raw_list_count=raw_list_count,
            detail_fetched=len(records),
            skipped_known=skipped_known,
            skipped_unsupported=skipped_unsupported,
            skipped_no_detail=skipped_no_detail,
            skipped_out_of_window=skipped_out_of_window,
            pages_crawled=pages_crawled,
            rate_limited=rate_limited,
        )
