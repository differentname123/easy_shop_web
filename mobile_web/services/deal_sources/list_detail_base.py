# -*- coding: utf-8 -*-
"""
HTML 列表->详情型线报数据源公共模板基类 (`ListDetailDealSource`)。
================================================================================
统一收束 7 个列表->详情型站点的公共状态机，消除重复样板代码：
1. 列表拉取 (`fetch_page_html`) 与统一错误/限流 `FetchResult` 构建；
2. ID 去重、`raw_list_count` 统计、`cutoff_ts` 时间窗口与非淘京拼预过滤；
3. `full_sweep` 模式 `touch_ids` 采集与 `known_ids` 跳过计数；
4. `max_details` 配额截断、详情页间隔休眠、HTTP 403/429 熔断与异常记账；
5. 统一 `has_valid_detail_and_tutorial` 门禁校验与 `FetchResult` 汇总输出。
================================================================================
"""
from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import BaseDealSource, FetchContext, FetchResult
from .filters import has_valid_detail_and_tutorial, is_confirmed_unsupported_deal
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry


class SkipItem(Exception):
    """在单条详情解析阶段主动跳过当前条目的轻量控制流异常。"""

    def __init__(self, reason: str = "no_detail") -> None:
        super().__init__(reason)
        self.reason = reason


class DetailCircuitBreak(Exception):
    """触发详情页安全验证/频控熔断时抛出的控制流异常。"""

    def __init__(
        self,
        *,
        error_entry: Optional[Dict[str, Any]] = None,
        rate_limited: bool = True,
        stop_loop: bool = True,
        count_as_no_detail: bool = True,
    ) -> None:
        super().__init__((error_entry or {}).get("error") or "detail circuit break")
        self.error_entry = error_entry
        self.rate_limited = rate_limited
        self.stop_loop = stop_loop
        self.count_as_no_detail = count_as_no_detail


@dataclass
class ListPhaseResult:
    """列表阶段输出结果，承载规范化卡片字典与分页/风控状态。"""

    cards: List[Dict[str, Any]] = field(default_factory=list)
    request_url: str = ""
    http_status: Optional[int] = None
    pages_crawled: int = 1
    reached_end: bool = True
    rate_limited: bool = False
    allow_empty_ok: bool = False
    error_result: Optional[FetchResult] = None
    extra_state: Dict[str, Any] = field(default_factory=dict)


def fetch_page_html(
    ctx: FetchContext,
    url: str,
    *,
    t0: float,
    referer: str = "",
    timeout: Optional[float] = None,
    error_prefix: str = "",
) -> Tuple[Optional[Any], str, Optional[FetchResult]]:
    """拉取单个 HTML 页面；失败时返回可直接 return 的标准化错误 `FetchResult`。"""
    headers = {**DEFAULT_HEADERS, "Referer": referer} if referer else DEFAULT_HEADERS
    eff_timeout = timeout if timeout is not None else ctx.timeout
    resp = None
    try:
        resp = request_with_retry(ctx.session, "get", url, headers=headers, timeout=eff_timeout)
        resp.encoding = "utf-8"
        body = resp.text
        if resp.status_code != 200:
            err_msg = f"{error_prefix}HTTP {resp.status_code}" if not error_prefix else f"{error_prefix}: HTTP {resp.status_code}"
            return (
                resp,
                body,
                FetchResult(
                    status="error",
                    http_status=resp.status_code,
                    request_url=url,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                    rate_limited=is_rate_limit_status(resp.status_code),
                    detail_errors=[{
                        "error_url": url,
                        "http_status": resp.status_code,
                        "error_type": f"HTTP_{resp.status_code}",
                        "error": err_msg,
                    }],
                ),
            )
        return resp, body, None
    except Exception as exc:
        err_text = f"{error_prefix}: {str(exc)[:160]}" if error_prefix else str(exc)[:160]
        return (
            resp,
            "",
            FetchResult(
                status="error",
                http_status=getattr(resp, "status_code", None),
                request_url=url,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                rate_limited=False,
                detail_errors=[{
                    "error_url": url,
                    "http_status": getattr(resp, "status_code", None),
                    "error_type": type(exc).__name__,
                    "error": err_text,
                }],
            ),
        )


class ListDetailDealSource(BaseDealSource):
    """列表->详情两段式 HTML 数据源公共模板基类。"""

    def before_fetch(self, ctx: FetchContext) -> None:
        return None

    def after_fetch(self, ctx: FetchContext, result: FetchResult) -> None:
        return None

    @abstractmethod
    def fetch_list_cards(self, ctx: FetchContext, t0: float) -> ListPhaseResult:
        """抓取并解析列表页，返回 `ListPhaseResult`。"""

    def pre_filter_card(self, card: Dict[str, Any], ctx: FetchContext) -> Optional[str]:
        """列表卡片前置过滤，返回跳过原因 ('out_of_window' | 'unsupported' | 'no_detail') 或 None。"""
        cutoff_ts = ctx.cutoff_ts
        pub_ts = card.get("publish_ts")
        if cutoff_ts is not None and pub_ts is not None and pub_ts < cutoff_ts:
            return "out_of_window"

        mall_name = str(card.get("mall_name") or "").strip()
        list_buy_url = str(card.get("list_buy_url") or "").strip()
        list_links = [{"url": list_buy_url}] if list_buy_url else None
        if (mall_name or list_links) and is_confirmed_unsupported_deal(mall_name=mall_name, links=list_links):
            return "unsupported"
        return None

    def get_detail_timeout(self, ctx: FetchContext) -> float:
        return float(ctx.timeout)

    def get_detail_referer(self, card: Dict[str, Any], list_res: ListPhaseResult, ctx: FetchContext) -> str:
        return list_res.request_url

    def sleep_before_detail(self, ctx: FetchContext) -> None:
        self.sleep_detail_delay(ctx)

    @abstractmethod
    def parse_detail_record(
        self,
        card: Dict[str, Any],
        det_resp: Any,
        det_html: str,
        list_res: ListPhaseResult,
        ctx: FetchContext,
    ) -> Optional[Dict[str, Any]]:
        """解析详情 HTML 为标准化线报字典；返回 None 或抛出 `SkipItem`/`DetailCircuitBreak`。"""

    def fetch(self, ctx: FetchContext) -> FetchResult:
        self.before_fetch(ctx)
        t0 = time.perf_counter()
        list_res = self.fetch_list_cards(ctx, t0)
        if list_res.error_result is not None:
            self.after_fetch(ctx, list_res.error_result)
            return list_res.error_result

        full_sweep = ctx.channel.fetch_mode == "full_sweep"
        max_details = ctx.effective_max_details
        skip_set = ctx.known_ids

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
        skipped_out_of_window = 0
        detail_circuit_broken = False
        rate_limited = bool(list_res.rate_limited)

        def _consume_extra_state() -> None:
            nonlocal detail_circuit_broken, rate_limited
            extra = list_res.extra_state
            if not extra:
                return
            extra_errs = extra.pop("extra_errors", None)
            if isinstance(extra_errs, list):
                detail_errors.extend(extra_errs)
            if extra.pop("circuit_broken", False):
                detail_circuit_broken = True
            if extra.pop("rate_limited", False):
                rate_limited = True

        for card in list_res.cards:
            item_id = str(card.get("item_id") or "").strip()
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            raw_list_count += 1

            skip_reason = self.pre_filter_card(card, ctx)
            if skip_reason == "out_of_window":
                skipped_out_of_window += 1
                continue
            if skip_reason == "unsupported":
                skipped_unsupported += 1
                continue
            if skip_reason == "no_detail":
                skipped_no_detail += 1
                continue

            if full_sweep:
                touch_ids.add(item_id)

            if item_id in skip_set:
                skipped_known += 1
                continue

            if detail_circuit_broken or detail_attempted >= max_details:
                if full_sweep:
                    continue
                break

            if detail_attempted > 0:
                self.sleep_before_detail(ctx)
            detail_attempted += 1

            detail_url = str(card.get("detail_url") or "").strip()
            try:
                rd = request_with_retry(
                    ctx.session,
                    "get",
                    detail_url,
                    headers={**DEFAULT_HEADERS, "Referer": self.get_detail_referer(card, list_res, ctx)},
                    timeout=self.get_detail_timeout(ctx),
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

                rec = self.parse_detail_record(card, rd, rd.text, list_res, ctx)
                _consume_extra_state()
                if rec is None or not has_valid_detail_and_tutorial(rec):
                    skipped_no_detail += 1
                    continue

                detail_fetched += 1
                records.append(rec)
            except DetailCircuitBreak as cb:
                _consume_extra_state()
                detail_circuit_broken = True
                if cb.rate_limited:
                    rate_limited = True
                if cb.count_as_no_detail:
                    skipped_no_detail += 1
                if cb.error_entry:
                    detail_errors.append(cb.error_entry)
                if cb.stop_loop and not full_sweep:
                    break
                continue
            except SkipItem as sk:
                _consume_extra_state()
                if sk.reason == "unsupported":
                    skipped_unsupported += 1
                elif sk.reason == "out_of_window":
                    skipped_out_of_window += 1
                else:
                    skipped_no_detail += 1
                continue
            except Exception as exc:
                _consume_extra_state()
                skipped_no_detail += 1
                detail_errors.append({
                    "item_id": item_id,
                    "error_url": detail_url,
                    "http_status": None,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                })

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        is_ok = (
            list_res.pages_crawled > 0
            and (raw_list_count > 0 or list_res.allow_empty_ok)
            and not (rate_limited and len(records) == 0 and len(touch_ids) == 0)
        )
        result = FetchResult(
            status="ok" if is_ok else "error",
            http_status=list_res.http_status or 200,
            request_url=list_res.request_url,
            elapsed_ms=elapsed_ms,
            records=records,
            raw_list_count=raw_list_count,
            detail_fetched=detail_fetched,
            skipped_known=skipped_known,
            skipped_unsupported=skipped_unsupported,
            skipped_no_detail=skipped_no_detail,
            skipped_out_of_window=skipped_out_of_window,
            pages_crawled=list_res.pages_crawled,
            touch_ids=sorted(touch_ids),
            sweep_completed=bool(full_sweep and list_res.reached_end and raw_list_count > 0 and not rate_limited),
            rate_limited=rate_limited,
            detail_errors=detail_errors,
        )
        self.after_fetch(ctx, result)
        return result
