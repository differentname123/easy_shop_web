# -*- coding: utf-8 -*-
"""
真实线上数据源联调验证工具 (`verify_deal_sources.py`)。
================================================================================
严格按照 `services.deal_sources` 插件与 `ChannelConfig` 独立运行各站点/频道：
- 按频道各自的 `network_mode` (`direct` / `proxy`) 与 `close_connection` 创建隔离会话；
- 不依赖 MongoDB、不触碰线上数据库，仅输出抓取指标、过滤统计与样本预览；
- 遇到验证码/滑块严格 Fail-Closed 报告，绝不绕过验证。

常用示例：
  python mobile_web/scripts/verify_deal_sources.py --max-details 2 --max-pages 2
  python mobile_web/scripts/verify_deal_sources.py --site guangdiu,tehuicaicai
  python mobile_web/scripts/verify_deal_sources.py --channel 好单库_淘宝实时线报
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import traceback
from typing import Any, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
for _p in (PROJECT_ROOT, WORKSPACE_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.deal_sources import (
    DEFAULT_CHANNEL_CONFIGS,
    ChannelConfig,
    FetchContext,
    create_site_session,
    get_source,
    get_source_troubleshooting,
    has_valid_detail_and_tutorial,
)


def _select_channels(
    site_filter: str,
    channel_filter: str,
    include_disabled: bool,
) -> List[ChannelConfig]:
    wanted_sites = {s.strip().lower() for s in str(site_filter or "").split(",") if s.strip()}
    wanted_channels = {c.strip().lower() for c in str(channel_filter or "").split(",") if c.strip()}
    selected: List[ChannelConfig] = []
    for ch in DEFAULT_CHANNEL_CONFIGS:
        if not include_disabled and not ch.enabled:
            continue
        if wanted_sites and ch.site_key.lower() not in wanted_sites and ch.resolved_source_key.lower() not in wanted_sites:
            continue
        if wanted_channels and not any(w in ch.name.lower() for w in wanted_channels):
            continue
        selected.append(ch)
    return selected


def _truncate(text: Any, limit: int = 90) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _verify_one_channel(
    ch: ChannelConfig,
    *,
    max_details: int | None,
    max_pages: int | None,
    timeout: float,
    show_records: int,
) -> Dict[str, Any]:
    effective_ch = ch
    if max_pages is not None and max_pages > 0:
        effective_ch = dataclasses.replace(ch, max_pages=min(ch.max_pages, int(max_pages)))

    source = get_source(effective_ch.resolved_source_key)
    if source is None:
        return {
            "channel": effective_ch.name,
            "site_key": effective_ch.site_key,
            "status": "error",
            "rate_limited": False,
            "count": 0,
            "error": f"unregistered source_key={effective_ch.resolved_source_key}",
        }

    print(
        f"\n================================================================================\n"
        f"[{effective_ch.name}] site={effective_ch.site_key} | source={effective_ch.resolved_source_key} "
        f"| mode={effective_ch.fetch_mode} | net={effective_ch.network_mode} | url={effective_ch.default_url}"
    )

    try:
        with create_site_session(
            network_mode=effective_ch.network_mode,
            close_connection=effective_ch.close_connection,
        ) as session:
            ctx = FetchContext(
                channel=effective_ch,
                session=session,
                known_ids=set(),
                max_details=max_details,
                timeout=timeout,
            )
            result = source.fetch(ctx)
            res = result.to_dict()
    except Exception as exc:
        traceback.print_exc()
        return {
            "channel": effective_ch.name,
            "site_key": effective_ch.site_key,
            "status": "error",
            "rate_limited": False,
            "count": 0,
            "error": f"exception: {exc}",
        }

    records = res.get("records") or []
    print(
        f"  -> status={res.get('status')} | http={res.get('http_status')} | elapsed={res.get('elapsed_ms')}ms "
        f"| pages={res.get('pages_crawled')} | raw={res.get('raw_list_count')} | valid={len(records)} "
        f"| detail={res.get('detail_fetched')} | skip_unsupported={res.get('skipped_unsupported')} "
        f"| skip_no_detail={res.get('skipped_no_detail')} | skip_window={res.get('skipped_out_of_window')} "
        f"| sweep_completed={res.get('sweep_completed')} | rate_limited={res.get('rate_limited')}"
    )
    if res.get("error"):
        print(f"  !! error={res.get('error')}")
    if res.get("rate_limited"):
        advice = get_source_troubleshooting(effective_ch.site_key)
        if advice:
            print(f"  !! troubleshooting: {advice.get('hint', advice)}")
    if res.get("detail_errors"):
        print(f"  !! detail_errors ({len(res['detail_errors'])}): {res['detail_errors'][:2]}")

    detail_errors = res.get("detail_errors") or []
    summary_err = str(res.get("error") or "")
    if not summary_err and detail_errors:
        first_det = detail_errors[0] if isinstance(detail_errors[0], dict) else {}
        err_type = str(first_det.get("error_type") or "").strip()
        err_msg = str(first_det.get("error") or "").strip()
        summary_err = f"{err_type}: {err_msg}".strip(": ")

    for idx, rec in enumerate(records[: max(0, show_records)], start=1):
        valid_flag = has_valid_detail_and_tutorial(rec)
        ext_links = rec.get("extracted_links") or []
        tokens = rec.get("extracted_tokens") or []
        tut_txt = rec.get("tutorial_text") or ""
        src_url = rec.get("source_url") or rec.get("source_detail_url") or ""
        mall = rec.get("mall_name") or "-"
        price_txt = rec.get("price_text") or "-"
        print(
            f"  [样本 {idx}] id={rec.get('source_item_id')} | platform={rec.get('platform') or '?'} "
            f"| mall={mall} | price={price_txt} | valid_gate={valid_flag} "
            f"| pub={rec.get('publish_time') or '-'} | links={len(ext_links)} tokens={len(tokens)}"
        )
        print(f"    标题: {_truncate(rec.get('title'), 100)}")
        if src_url:
            print(f"    来源: {_truncate(src_url, 120)}")
        if ext_links and isinstance(ext_links[0], dict):
            print(f"    外链[0]: {_truncate(ext_links[0].get('url'), 120)}")
        if tokens:
            print(f"    口令[0]: {_truncate(tokens[0], 60)}")
        if rec.get("image_url"):
            print(f"    主图: {_truncate(rec.get('image_url'), 120)}")
        print(f"    攻略: {_truncate(tut_txt, 140)}")

    return {
        "channel": effective_ch.name,
        "site_key": effective_ch.site_key,
        "status": res.get("status"),
        "http_status": res.get("http_status"),
        "rate_limited": bool(res.get("rate_limited")),
        "count": len(records),
        "raw_list_count": int(res.get("raw_list_count") or 0),
        "sweep_completed": bool(res.get("sweep_completed")),
        "error": summary_err,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify real live deal sources via services.deal_sources.")
    parser.add_argument("--site", default="", help="Filter by site_key (comma-separated)")
    parser.add_argument("--channel", default="", help="Filter by channel name substring (comma-separated)")
    parser.add_argument("--max-details", type=int, default=2, help="Max new detail fetches per channel (default: 2)")
    parser.add_argument("--max-pages", type=int, default=2, help="Max pages override per channel (default: 2)")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP request timeout in seconds")
    parser.add_argument("--show-records", type=int, default=2, help="Sample records printed per channel")
    parser.add_argument("--include-disabled", action="store_true", help="Also verify disabled channels")
    parser.add_argument("--strict", action="store_true", help="Return non-zero on rate-limited channels too")
    args = parser.parse_args(argv)

    channels = _select_channels(args.site, args.channel, args.include_disabled)
    if not channels:
        print("No matching channels selected.")
        return 2

    print(f"Selected {len(channels)} channels | max_details={args.max_details} | max_pages={args.max_pages} | timeout={args.timeout}s")
    summaries: List[Dict[str, Any]] = []
    for ch in channels:
        summaries.append(
            _verify_one_channel(
                ch,
                max_details=args.max_details,
                max_pages=args.max_pages,
                timeout=args.timeout,
                show_records=args.show_records,
            )
        )

    print("\n================================================================================")
    print("联调汇总:")
    hard_errors = 0
    rate_limited_cnt = 0
    for item in summaries:
        flag = "OK" if item["status"] == "ok" else ("RATE_LIMIT" if item["rate_limited"] else "FAIL")
        if item["status"] != "ok":
            if item["rate_limited"]:
                rate_limited_cnt += 1
            else:
                hard_errors += 1
        print(
            f"  [{flag:10s}] {item['channel']:22s} | site={item['site_key']:13s} "
            f"| raw={item.get('raw_list_count', 0):3d} | valid={item.get('count', 0):3d} "
            f"| http={str(item.get('http_status')):>4s} | err={_truncate(item.get('error'), 60)}"
        )

    if hard_errors > 0:
        return 1
    if args.strict and rate_limited_cnt > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
