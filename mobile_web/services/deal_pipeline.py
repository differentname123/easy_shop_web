# -*- coding: utf-8 -*-
"""
好价线报多阶段流水线与拉取调度引擎 (`DealPipeline`)。
负责 Producer 阶段（`run_fetch_stage`，支持多源独立调度间隔）与 Consumer 阶段（`run_stage_pipeline_drain`）。
"""
import copy
import datetime
import json
import logging
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from itertools import zip_longest
from models.deal_constants import (
    CLEAN_FAILED_SAMPLES_JSONL,
    CLEAN_SUCCESS_PREVIEW_JSONL,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    CLEAN_OPS_HISTORY_JSONL,
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEAL_MAX_CLEAN_AGE_DAYS,
    DEFAULT_CLEAN_WINDOW,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
    ENGINEERING_PHILOSOPHY_MANIFEST,
    ERROR_LOG_JSONL_PATH,
    ERR_AI_TIMEOUT_RETRY,
    ERR_EXPIRED_OVER_7_DAYS,
    EXCLUDED_SITES_AND_CHANNELS_MANIFEST,
    LATEST_CLEAN_OPS_REPORT_PATH,
    LATEST_OPS_REPORT_PATH,
    MAX_CLEAN_RETRIES,
    OPS_REPORT_HISTORY_JSONL,
    STATS_SNAPSHOT_PATH,
    CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
)
from repositories.deal_raw_pool_repo import (
    DealRawPoolRepository,
    _append_bounded_jsonl_lines,
    evaluate_quality_signals,
)
from services.deal_channel_config import (
    DealChannelConfig,
    DEFAULT_CHANNEL_CONFIGS,
    get_active_channels,
    is_channel_due,
)
from services.deal_fetchers import (
    is_record_confirmed_unsupported,
)
from services.deal_pool_cleaner import (
    DealPoolCleaner,
    MaintenanceWorker,
    StageWorker,
    _commit_batch_results_to_db,
    upsert_cleaned_products_to_formal_table,
)

logger = logging.getLogger("deal_pool_scheduler")

# ==============================================================================
# 站点显示名称与排障手册
# ==============================================================================
SITE_DISPLAY_NAMES: Dict[str, str] = {
    "tehuicaicai": "特惠采采 (专员人工严选集采)",
    "guangdiu": "逛丢 (首页/热门榜/小时风云榜)",
    "zhetaoke": "折淘客 (采集群/爆款榜)",
    "haodanku": "好单库 (实时线报群/福利单)",
    "xuandan": "选单网 (快抢群发素材)",
    "0818tuan": "0818团 (实战凑单线报)",
    "xianbao_fun": "线报酷 (聚合频道)",
    "bacaoo": "拔草哦 (优惠与拼多多专区)",
}

OPS_TROUBLESHOOTING_PLAYBOOK: Dict[str, Dict[str, str]] = {
    "tehuicaicai": {
        "endpoint": "https://xhhcds.cn/api/v1/public/home-products/?feed=home | https://xhhcds.cn/api/v1/products/{id}/",
        "http_403_429": "特惠采采返回限流：请调大 detail_delay_seconds 或降低 max_details。",
        "empty_or_parse": "特惠采采列表返回 0 条：专员非工作时段发单较少，或请检查 JSON 接口字段。",
        "timeout_or_conn": "特惠采采连接超时：接口抖动，下轮将自动重试。",
    },
    "guangdiu": {
        "endpoint": "https://guangdiu.com/ | https://guangdiu.com/hots.php | https://guangdiu.com/rank.php",
        "http_403_429": "逛丢触发限流或滑块安全验证：已触发自动熔断冷却，可适当调低 max_details 或增大 detail_delay_seconds。",
        "empty_or_parse": "逛丢列表解析为 0：请访问报错路径核对页面 DOM 是否变更。",
        "timeout_or_conn": "逛丢请求超时：单次超时不影响其余频道，下轮自动补抓。",
    },
    "zhetaoke": {
        "endpoint": "https://www.zhetaoke.com/xianbao.aspx | https://www.zhetaoke.com/list/love_1_0_1.html",
        "http_403_429": "折淘客触发频率限制：请调大 sleep_after。",
        "empty_or_parse": "折淘客返回 0 条记录：请检查 `.sc-product` DOM 类名是否改版。",
        "timeout_or_conn": "折淘客连接超时：偶发抖动将在下轮恢复。",
    },
    "haodanku": {
        "endpoint": "https://www.haodanku.com/HolidayActivity/wire_report | https://www.haodanku.com/HolidayActivity/weal_list",
        "http_403_429": "好单库接口返回 403/429：请检查请求头是否完整。",
        "empty_or_parse": "好单库返回空列表：请核查接口字段。",
        "timeout_or_conn": "好单库请求超时：下轮自动重试。",
    },
    "xuandan": {
        "endpoint": "https://www.xuandan.com/NewHome/msKqList",
        "http_403_429": "选单网接口限流：请确认请求头包含正确的 Referer。",
        "empty_or_parse": "选单网返回 0 条：请检查 JSON 响应。",
        "timeout_or_conn": "选单网连接超时：下轮自动重试。",
    },
    "0818tuan": {
        "endpoint": "http://www.0818tuan.com/list-1-{page}.html",
        "http_403_429": "0818团访问受限：请降低抓取上限。",
        "empty_or_parse": "0818团列表为空：请检查列表容器 DOM 是否改动。",
        "timeout_or_conn": "0818团详情页超时：下轮自动重试。",
    },
    "xianbao_fun": {
        "endpoint": "https://new.xianbao.fun/",
        "http_403_429": "线报酷触发频率限制：请调大请求间隔。",
        "empty_or_parse": "线报酷返回 0 条：请检查列表结构。",
        "timeout_or_conn": "线报酷详情页超时：下轮自动补抓。",
    },
    "bacaoo": {
        "endpoint": "https://youhui.bacaoo.com/",
        "http_403_429": "拔草哦返回 403/429：请调大请求间隔。",
        "empty_or_parse": "拔草哦解析为 0 条：请检查页面响应。",
        "timeout_or_conn": "拔草哦连接超时：下轮自动重试。",
    },
}


def get_ops_troubleshooting_advice(
    site_key: str,
    http_status: Optional[int],
    error_type: str,
    fetched_count: int,
    consecutive_failures: int,
) -> str:
    """根据站点标识、HTTP 状态码、错误类型及连续失败次数，生成明确的运维人员处理建议。"""
    playbook = OPS_TROUBLESHOOTING_PLAYBOOK.get(site_key, {})
    escalation = (
        f"【🚨 连续 {consecutive_failures} 轮异常，需人工介入】"
        if consecutive_failures >= 3
        else "【💡 偶发单轮波动或风控冷却，系统将在冷却后自动重试】"
    )
    if (
        http_status in (401, 403, 429, 503)
        or "429" in error_type
        or "403" in error_type
        or "Captcha" in error_type
    ):
        base_advice = playbook.get(
            "http_403_429",
            "目标站点返回风控/限流/验证码，请调大频道拉取间隔或降低单轮详情数。",
        )
    elif "Timeout" in error_type or "Connection" in error_type or "ChunkedEncoding" in error_type:
        base_advice = playbook.get(
            "timeout_or_conn",
            "网络请求超时或连接重置，系统将在下一轮自动重试。",
        )
    elif http_status == 200 and fetched_count == 0:
        base_advice = playbook.get(
            "empty_or_parse",
            "HTTP 200 但列表解析为 0 条记录，可能是页面结构变更，请点开错误路径 URL 核对。",
        )
    else:
        base_advice = playbook.get(
            "timeout_or_conn",
            "请核对报错路径 URL 响应状态。",
        )
    return f"{escalation} {base_advice}"


def append_error_records_jsonl(error_records: List[Dict[str, Any]]):
    """将本轮捕获的所有列表级与详情级错误路径持久化追加写入 logs/deal_pool_errors.jsonl。"""
    if not error_records:
        return
    try:
        _append_bounded_jsonl_lines(
            ERROR_LOG_JSONL_PATH,
            [json.dumps(item, ensure_ascii=False) for item in error_records],
        )
    except Exception as e:
        logger.error("写入错误路径日志 [%s] 失败: %s", ERROR_LOG_JSONL_PATH, e)


# 全局多源调度时间记录器：key=channel.name, value=上次运行的 epoch 时间戳
_GLOBAL_CHANNEL_LAST_RUN: Dict[str, float] = {}


def _resolve_fetcher_callable(fetcher_name: str) -> Optional[Callable[..., Dict[str, Any]]]:
    """查找顺序：1. run_deal_pool_scheduler（支持测试 monkeypatch）-> 2. deal_fetchers -> 3. test_deal_sites_scraper。"""
    sched_mod = sys.modules.get("mobile_web.scripts.run_deal_pool_scheduler")
    if sched_mod and hasattr(sched_mod, fetcher_name):
        return getattr(sched_mod, fetcher_name)
    try:
        import services.deal_fetchers as fetchers_mod
        if hasattr(fetchers_mod, fetcher_name):
            return getattr(fetchers_mod, fetcher_name)
    except Exception:
        pass
    try:
        import mobile_web.scripts.test_deal_sites_scraper as scraper_mod
        if hasattr(scraper_mod, fetcher_name):
            return getattr(scraper_mod, fetcher_name)
    except Exception:
        pass
    return None


def run_fetch_stage(
    repo: DealRawPoolRepository,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: Optional[int] = None,
    run_all_tiers: bool = False,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
    latest_clean_report: Optional[Dict[str, Any]] = None,
    channels: Optional[List[DealChannelConfig]] = None,
    channel_last_run: Optional[Dict[str, float]] = None,
    site_cooldown_until: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    执行单轮【阶段一（Producer）：多站点好价拉取入初步表 `deal_raw_tips`】：
    - 仅运行 `enabled=True` 且已到自身 `interval_seconds`（且所属站点未处于冷却期）的频道；
    - 统一按活跃站点加载已抓详情 ID 集合（零硬编码浪费）；
    - 统一门禁：仅保存具备完整详情且未被 100% 确认为非淘京拼平台的标准化记录。
    """
    _ = run_all_tiers
    fetch_start = time.perf_counter()
    now_wall = time.time()
    cycle_mode = "PER_CHANNEL_SCHEDULE"
    if channel_health_tracker is None:
        channel_health_tracker = {}
    last_run_map = channel_last_run if channel_last_run is not None else _GLOBAL_CHANNEL_LAST_RUN
    cooldown_map = site_cooldown_until if site_cooldown_until is not None else {}

    active_channels = get_active_channels(channels=channels)
    due_channels = [
        ch
        for ch in active_channels
        if is_channel_due(ch, last_run_map.get(ch.name, 0.0), now_wall)
    ]

    active_sites = sorted({ch.site_key for ch in due_channels if ch.site_key})
    dedup_map: Dict[str, Set[str]] = {
        site: repo.get_existing_detailed_ids(site, limit=5000)
        for site in active_sites
    }

    total_inserted = 0
    total_updated = 0
    total_fetched = 0
    task_reports: List[Dict[str, Any]] = []
    cycle_error_paths: List[Dict[str, Any]] = []
    total_channels = len(due_channels)

    try:
        cur_pending_cnt = repo.col.count_documents(
            {"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]}
        )
    except Exception:
        cur_pending_cnt = 0

    logger.info(
        "================ 开始第 #%d 轮 [阶段1-线报拉取] (本轮到期 %d/%d 个已启用频道 | 📦 库内Pending=%d条) ================",
        cycle_num,
        total_channels,
        len(active_channels),
        cur_pending_cnt,
    )

    rate_limited_sites: Set[str] = {
        site
        for site, until_ts in cooldown_map.items()
        if until_ts and until_ts > now_wall
    }
    _rate_limit_codes = {403, 429, 502, 503, 504}

    for task_idx, ch in enumerate(due_channels, 1):
        rem_tasks = total_channels - task_idx
        task_name = ch.name
        site_key = ch.site_key
        default_url = ch.default_url
        pause_sec = ch.sleep_after
        t0 = time.perf_counter()

        ch_state = channel_health_tracker.setdefault(
            task_name,
            {"consecutive_failures": 0, "total_runs": 0, "total_errors": 0, "last_error_url": ""},
        )

        if site_key in rate_limited_sites:
            logger.info(
                "  ⏸️ [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 站点 [%s] 处于限流/验证码冷却期，跳过本轮",
                task_idx,
                total_channels,
                rem_tasks,
                task_name,
                site_key,
            )
            task_reports.append({
                "task_name": task_name,
                "site_key": site_key,
                "request_url": default_url,
                "http_status": 429,
                "status": "cooldown_skipped",
                "fetched": 0,
                "detail_fetched": 0,
                "detail_error_count": 0,
                "inserted": 0,
                "updated": 0,
                "unique_anchor_count": 0,
                "consecutive_failures": ch_state["consecutive_failures"],
                "elapsed_ms": 0.0,
            })
            continue

        ch_state["total_runs"] += 1
        fn = _resolve_fetcher_callable(ch.fetcher_name)
        if fn is None:
            logger.error("未找到频道 [%s] 对应的抓取函数 [%s]，请检查配置！", task_name, ch.fetcher_name)
            continue

        call_kwargs = dict(ch.fetcher_kwargs)
        call_kwargs["session"] = session
        if site_key in dedup_map:
            call_kwargs["skip_detail_ids"] = dedup_map[site_key]
        if call_kwargs.get("fetch_detail"):
            eff_max_details = max_new_details_per_channel if max_new_details_per_channel is not None else ch.max_details
            call_kwargs["max_details"] = eff_max_details
            if getattr(fn, "__module__", "") == "services.deal_fetchers" and "detail_delay" not in call_kwargs:
                call_kwargs["detail_delay"] = ch.detail_delay_seconds

        try:
            res = fn(**call_kwargs)
            last_run_map[task_name] = time.time()
            req_url = str(res.get("request_url") or default_url)
            http_status = res.get("http_status")
            raw_records = res.get("records") or []
            detail_errors = res.get("detail_errors") or []

            has_captcha_err = any("Captcha" in str(d.get("error_type") or "") for d in detail_errors)
            is_rate_limited = (
                bool(res.get("rate_limited"))
                or http_status in _rate_limit_codes
                or any(d.get("http_status") in _rate_limit_codes for d in detail_errors)
                or has_captcha_err
            )
            if is_rate_limited:
                rate_limited_sites.add(site_key)
                cooldown_sec = max(30, int(getattr(ch, "rate_limit_cooldown_seconds", 300)))
                cooldown_map[site_key] = time.time() + cooldown_sec

            # 统一入池前门禁：绝不放行无完整详情或 100% 确定非淘京拼平台的记录
            records: List[Dict[str, Any]] = []
            pipeline_filtered = 0
            for r in raw_records:
                if not isinstance(r, dict):
                    continue
                if not r.get("has_full_detail", True):
                    pipeline_filtered += 1
                    continue
                if is_record_confirmed_unsupported(r):
                    pipeline_filtered += 1
                    continue
                records.append(r)

            for r in records:
                if r.get("has_full_detail") and r.get("source_item_id"):
                    sid = str(r["source_item_id"])
                    if site_key in dedup_map:
                        dedup_map[site_key].add(sid)

            db_res = repo.save_batch(records, task_name=task_name)
            cost_ms = round((time.perf_counter() - t0) * 1000, 1)

            ins = db_res.get("inserted", 0)
            upd = db_res.get("updated", 0)
            cnt = len(records)
            det_cnt = res.get("detail_fetched", cnt)
            raw_list_cnt = int(res.get("raw_list_count") if res.get("raw_list_count") is not None else cnt)
            skipped_known = int(res.get("skipped_known") or 0)
            skipped_unsup = int(res.get("skipped_unsupported") or 0) + pipeline_filtered
            skipped_nodet = int(res.get("skipped_no_detail") or 0)

            total_inserted += ins
            total_updated += upd
            total_fetched += cnt

            unique_cnt = sum(
                1 for r in records
                if evaluate_quality_signals(r)["has_unique_product_anchor"]
            )

            if detail_errors:
                first_err = detail_errors[0]
                first_url = str(first_err.get("error_url") or first_err.get("detail_url") or req_url)
                first_type = str(first_err.get("error_type") or "DetailFetchError")
                first_msg = str(first_err.get("error") or "")
                if len(detail_errors) == 1:
                    summary_msg = first_msg
                else:
                    type_counts = Counter(str(d.get("error_type") or "DetailFetchError") for d in detail_errors)
                    type_summary = ", ".join(f"{k}:{v}" for k, v in type_counts.items())
                    summary_msg = f"本频道共 {len(detail_errors)} 条详情异常 ({type_summary})，首条: {first_msg}"
                err_item = {
                    "occurred_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cycle_num": cycle_num,
                    "site_key": site_key,
                    "task_name": task_name,
                    "stage": "detail_fetch",
                    "severity": "warning",
                    "error_url": first_url,
                    "item_id": str(first_err.get("item_id") or ""),
                    "http_status": first_err.get("http_status"),
                    "error_type": first_type,
                    "error_message": summary_msg,
                    "consecutive_failures": 1,
                    "ops_advice": get_ops_troubleshooting_advice(
                        site_key=site_key,
                        http_status=first_err.get("http_status"),
                        error_type=first_type,
                        fetched_count=cnt,
                        consecutive_failures=1,
                    ),
                }
                cycle_error_paths.append(err_item)
                logger.warning(
                    "  ⚠️ [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 详情异常 (异常=%d, 新抓=%d, 耗时=%.1fms) | 首条=%s",
                    task_idx,
                    total_channels,
                    rem_tasks,
                    task_name,
                    len(detail_errors),
                    cnt,
                    cost_ms,
                    summary_msg,
                )

            if is_rate_limited and cnt == 0:
                ch_state["consecutive_failures"] += 1
                status_label = "rate_limited"
                logger.warning(
                    "  🟠 [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 触发风控/验证码熔断，已进入 %ds 冷却 (耗时=%.1fms)",
                    task_idx,
                    total_channels,
                    rem_tasks,
                    task_name,
                    max(30, int(getattr(ch, "rate_limit_cooldown_seconds", 300))),
                    cost_ms,
                )
            elif raw_list_cnt == 0 and http_status == 200 and cnt == 0:
                ch_state["consecutive_failures"] += 1
                status_label = "empty_warning"
                logger.warning(
                    "  🟡 [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 列表解析为 0 条 (HTTP 200, 耗时=%.1fms)",
                    task_idx,
                    total_channels,
                    rem_tasks,
                    task_name,
                    cost_ms,
                )
            else:
                ch_state["consecutive_failures"] = 0
                status_label = "success"
                logger.info(
                    "  🟢 [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 列表=%02d | 跳过(已抓=%02d, 非淘京拼=%02d, 无详情=%02d) | 新入=%02d (新增=%02d, 耗时=%.1fms)",
                    task_idx,
                    total_channels,
                    rem_tasks,
                    task_name,
                    raw_list_cnt,
                    skipped_known,
                    skipped_unsup,
                    skipped_nodet,
                    cnt,
                    ins,
                    cost_ms,
                )

            task_reports.append({
                "task_name": task_name,
                "site_key": site_key,
                "request_url": req_url,
                "http_status": http_status,
                "status": status_label,
                "raw_list_count": raw_list_cnt,
                "fetched": cnt,
                "detail_fetched": det_cnt,
                "skipped_known": skipped_known,
                "skipped_unsupported": skipped_unsup,
                "skipped_no_detail": skipped_nodet,
                "detail_error_count": len(detail_errors),
                "inserted": ins,
                "updated": upd,
                "unique_anchor_count": unique_cnt,
                "consecutive_failures": ch_state["consecutive_failures"],
                "elapsed_ms": cost_ms,
            })
        except Exception as e:
            last_run_map[task_name] = time.time()
            cost_ms = round((time.perf_counter() - t0) * 1000, 1)
            ch_state["consecutive_failures"] += 1
            ch_state["total_errors"] += 1
            ch_state["last_error_url"] = default_url
            err_type = type(e).__name__
            advice = get_ops_troubleshooting_advice(
                site_key=site_key,
                http_status=None,
                error_type=err_type,
                fetched_count=0,
                consecutive_failures=ch_state["consecutive_failures"],
            )
            err_item = {
                "occurred_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cycle_num": cycle_num,
                "site_key": site_key,
                "task_name": task_name,
                "stage": "list_fetch",
                "severity": "error",
                "error_url": default_url,
                "http_status": None,
                "error_type": err_type,
                "error_message": str(e)[:240],
                "consecutive_failures": ch_state["consecutive_failures"],
                "ops_advice": advice,
            }
            cycle_error_paths.append(err_item)
            logger.warning(
                "  ❌ [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 拉取异常 (连续=%d轮, 耗时=%.1fms) | 错误路径=%s | 异常=%s: %s | 运维建议=%s",
                task_idx,
                total_channels,
                rem_tasks,
                task_name,
                ch_state["consecutive_failures"],
                cost_ms,
                default_url,
                err_type,
                e,
                advice,
            )
            task_reports.append({
                "task_name": task_name,
                "site_key": site_key,
                "request_url": default_url,
                "http_status": None,
                "status": "error",
                "fetched": 0,
                "detail_fetched": 0,
                "detail_error_count": 0,
                "inserted": 0,
                "updated": 0,
                "unique_anchor_count": 0,
                "consecutive_failures": ch_state["consecutive_failures"],
                "error_type": err_type,
                "error": str(e)[:240],
                "elapsed_ms": cost_ms,
            })

        if pause_sec > 0:
            time.sleep(pause_sec)

    try:
        repo.col.delete_many({"has_full_detail": False})
    except Exception:
        pass

    append_error_records_jsonl(cycle_error_paths)
    fetch_elapsed_s = round(time.perf_counter() - fetch_start, 2)

    summary = repo.generate_pool_summary()
    try:
        with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    ops_report = build_and_log_cycle_ops_report(
        cycle_num=cycle_num,
        cycle_mode=cycle_mode,
        fetch_elapsed_s=fetch_elapsed_s,
        task_reports=task_reports,
        cycle_error_paths=cycle_error_paths,
        clean_report=latest_clean_report or {},
        pool_summary=summary,
    )

    return {
        "cycle_num": cycle_num,
        "cycle_mode": cycle_mode,
        "fetch_elapsed_s": fetch_elapsed_s,
        "total_fetched": total_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "tasks": task_reports,
        "error_paths": cycle_error_paths,
        "ops_report": ops_report,
        "pool_summary": summary,
    }


def build_and_log_cycle_ops_report(
    cycle_num: int,
    cycle_mode: str,
    fetch_elapsed_s: float,
    task_reports: List[Dict[str, Any]],
    cycle_error_paths: List[Dict[str, Any]],
    clean_report: Dict[str, Any],
    pool_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """生成每轮综合运维报告并写入文件。"""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok_tasks = [t for t in task_reports if t["status"] == "success"]
    warn_tasks = [t for t in task_reports if t["status"] in ("empty_warning", "cooldown_skipped", "rate_limited")]
    fail_tasks = [t for t in task_reports if t["status"] == "error"]

    total_fetched = sum(t.get("fetched", 0) for t in task_reports)
    total_detail_fetched = sum(t.get("detail_fetched", 0) for t in task_reports)
    total_inserted = sum(t.get("inserted", 0) for t in task_reports)
    total_updated = sum(t.get("updated", 0) for t in task_reports)
    total_unique_anchor = sum(t.get("unique_anchor_count", 0) for t in task_reports)

    site_stats: Dict[str, Dict[str, Any]] = {}
    for t in task_reports:
        s_key = t.get("site_key") or "unknown"
        if s_key not in site_stats:
            site_stats[s_key] = {
                "site_key": s_key,
                "site_name": SITE_DISPLAY_NAMES.get(s_key, s_key),
                "tasks_total": 0,
                "tasks_ok": 0,
                "tasks_warn": 0,
                "tasks_fail": 0,
                "fetched": 0,
                "detail_fetched": 0,
                "inserted": 0,
                "updated": 0,
                "unique_anchor_count": 0,
                "detail_error_count": 0,
                "total_elapsed_ms": 0.0,
                "max_consecutive_failures": 0,
            }
        st = site_stats[s_key]
        st["tasks_total"] += 1
        if t["status"] == "success":
            st["tasks_ok"] += 1
        elif t["status"] in ("empty_warning", "cooldown_skipped", "rate_limited"):
            st["tasks_warn"] += 1
        else:
            st["tasks_fail"] += 1
        st["fetched"] += t.get("fetched", 0)
        st["detail_fetched"] += t.get("detail_fetched", 0)
        st["inserted"] += t.get("inserted", 0)
        st["updated"] += t.get("updated", 0)
        st["unique_anchor_count"] += t.get("unique_anchor_count", 0)
        st["detail_error_count"] += t.get("detail_error_count", 0)
        st["total_elapsed_ms"] += float(t.get("elapsed_ms") or 0.0)
        st["max_consecutive_failures"] = max(st["max_consecutive_failures"], int(t.get("consecutive_failures") or 0))

    site_matrix: List[Dict[str, Any]] = []
    for s_key, st in sorted(site_stats.items()):
        total_runs = max(1, st["tasks_total"])
        avg_ms = round(st["total_elapsed_ms"] / total_runs, 1)
        u_ratio = round(st["unique_anchor_count"] * 100.0 / max(1, st["fetched"]), 1)
        badge = "🟢健康正常"
        if st["tasks_fail"] > 0 or st["max_consecutive_failures"] >= 3:
            badge = "🔴异常告警"
        elif st["tasks_warn"] > 0 or st["detail_error_count"] > 0:
            badge = "🟡偶发告警"
        site_matrix.append({
            **st,
            "health_badge": badge,
            "avg_elapsed_ms": avg_ms,
            "unique_anchor_ratio": u_ratio,
        })

    overall_health = "🟢优秀"
    if len(fail_tasks) > 0 or any(s["max_consecutive_failures"] >= 3 for s in site_stats.values()):
        overall_health = "🔴告警(有失败)"
    elif len(warn_tasks) > 0 or len(cycle_error_paths) > 0:
        overall_health = "🟡良好(有波动)"

    report = {
        "cycle_num": cycle_num,
        "generated_at": now_str,
        "cycle_mode": cycle_mode,
        "overall_health": overall_health,
        "fetch_elapsed_s": fetch_elapsed_s,
        "channels_scheduled": len(task_reports),
        "channels_ok": len(ok_tasks),
        "channels_warn": len(warn_tasks),
        "channels_fail": len(fail_tasks),
        "total_fetched": total_fetched,
        "total_detail_fetched": total_detail_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "cycle_unique_anchor_count": total_unique_anchor,
        "cycle_unique_anchor_ratio": round(total_unique_anchor * 100.0 / max(1, total_fetched), 1),
        "pool_total_records": pool_summary.get("total_records", 0),
        "pool_unique_anchor_records": pool_summary.get("unique_product_anchor_records", 0),
        "pool_unique_anchor_ratio": pool_summary.get("unique_product_anchor_ratio", 0.0),
        "pool_cleaned_success_records": pool_summary.get("cleaned_success_records", 0),
        "pool_cleaned_failed_records": pool_summary.get("cleaned_failed_records", 0),
        "pool_pending_clean_records": pool_summary.get("pending_clean_records", 0),
        "formal_products_total": pool_summary.get("formal_products_total", 0),
        "formal_deal_products_count": pool_summary.get("formal_deal_products_count", 0),
        "site_health_matrix": site_matrix,
        "clean_stage_summary": clean_report,
        "error_paths_count": len(cycle_error_paths),
        "error_paths": cycle_error_paths[:30],
    }

    try:
        with open(LATEST_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        _append_bounded_jsonl_lines(
            OPS_REPORT_HISTORY_JSONL,
            [json.dumps(report, ensure_ascii=False)],
        )
    except Exception as e:
        logger.error("写入运维报告失败: %s", e)

    return report


def run_clean_stage(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    cycle_num: int,
    clean_limit: int = DEFAULT_CLEAN_WINDOW,
) -> Dict[str, Any]:
    """
    执行 Stage 2 ~ Stage 5 扁平单窗口清洗闭环（彻底移除多层嵌套切分与双层线程池）：
    1. 前置确定性清扫：超过 `DEAL_MAX_CLEAN_AGE_DAYS`（7天）或重试满 `MAX_CLEAN_RETRIES`（3次）的陈旧/卡死记录直接归档失败；
    2. 各来源最新均匀抽样：按 `source_site` 各自倒序取最新记录，通过 `zip_longest` 严格均匀轮询抽取最多 `clean_limit`（默认200）条；
    3. 扁平四步流水线：`上下文组装与Tier-1早停 -> AI-1并发拆解(10条/批) -> 全局去重并发官方转链与Tier-2早停 -> AI-2满载打包终审(10条/批) -> 批量写库`。
    """
    t_start = time.perf_counter()
    raw_col = repo.col
    formal_col = repo.formal_col
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff_ts = time.time() - DEAL_MAX_CLEAN_AGE_DAYS * 86400.0
    cutoff_dt = now_utc - datetime.timedelta(days=DEAL_MAX_CLEAN_AGE_DAYS)

    # Step 0: 前置确定性清扫（超过 7 天保鲜期 & 重试次数已达上限的陈旧/异常记录直接出队，绝不浪费 AI 机会）
    expired_over_7d_cnt = 0
    max_retry_swept_cnt = 0
    try:
        exp_res = raw_col.update_many(
            {
                "$and": [
                    {"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]},
                    {
                        "$or": [
                            {"publish_ts": {"$gt": 0, "$lt": cutoff_ts}},
                            {"first_seen_at": {"$lt": cutoff_dt}},
                        ]
                    },
                ]
            },
            {
                "$set": {
                    "clean_status": "cleaned_failed",
                    "ai_clean_status": "cleaned_failed",
                    "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                    "clean_error_msg": f"爆料时间超过 {DEAL_MAX_CLEAN_AGE_DAYS} 天保鲜期，自动跳过清洗避免浪费资源",
                    "cleaned_at": now_utc,
                }
            },
        )
        expired_over_7d_cnt = int(exp_res.modified_count or 0)

        retry_sweep_res = raw_col.update_many(
            {
                "$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}],
                "clean_retry_count": {"$gte": MAX_CLEAN_RETRIES},
            },
            {
                "$set": {
                    "clean_status": "cleaned_failed",
                    "ai_clean_status": "cleaned_failed",
                    "clean_error_code": ERR_AI_MAX_RETRIES_EXCEEDED,
                    "clean_error_msg": f"已达最大重试上限({MAX_CLEAN_RETRIES}次)，自动放弃避免占用清洗队列",
                    "cleaned_at": now_utc,
                }
            },
        )
        max_retry_swept_cnt = int(retry_sweep_res.modified_count or 0)
        if expired_over_7d_cnt > 0 or max_retry_swept_cnt > 0:
            logger.info(
                "🧹 [前置保鲜清扫] 自动剔除超%d天陈旧Pending=%d条, 已满%d次重试上限=%d条",
                DEAL_MAX_CLEAN_AGE_DAYS,
                expired_over_7d_cnt,
                MAX_CLEAN_RETRIES,
                max_retry_swept_cnt,
            )
    except Exception as e:
        logger.warning("前置保鲜与重试上限清扫异常: %s", e)

    if cycle_num > 0 and cycle_num % 30 == 0:
        expired_deals = repo.expire_stale_formal_deals(now_utc=now_utc)
        if expired_deals > 0:
            logger.info("🧹 [正式库好价保鲜] 已自动摘除超 %.0f 小时未刷新好价标 %d 个", DEAL_FORMAL_EXPIRY_HOURS, expired_deals)

    fresh_guard_clause: Dict[str, Any] = {
        "$nor": [
            {"publish_ts": {"$gt": 0, "$lt": cutoff_ts}},
            {"first_seen_at": {"$lt": cutoff_dt}},
            {"next_retry_after_ts": {"$gt": round(time.time(), 1)}},
        ]
    }
    active_pending_filter: Dict[str, Any] = {
        "$and": [
            {"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]},
            fresh_guard_clause,
        ]
    }
    total_pending_initial = raw_col.count_documents(active_pending_filter)
    if total_pending_initial <= 0:
        logger.info(
            "✨ [阶段二~五：双AI+官方转链清洗] 当前初步表 `deal_raw_tips` 无%d天内待清洗 (`pending`) 记录，跳过本轮清洗。",
            DEAL_MAX_CLEAN_AGE_DAYS,
        )
        return {
            "cycle_num": cycle_num,
            "total_pending_found": 0,
            "expired_over_7d_swept": expired_over_7d_cnt,
            "total_processed": 0,
            "resolved_success": 0,
            "early_terminated": 0,
            "retry_pending": 0,
            "formal_upserted": 0,
            "formal_modified": 0,
            "elapsed_s": round(time.perf_counter() - t_start, 2),
        }

    # Step 1: 按每个来源 (`source_site`) 各自获取最新待清洗记录，再通过 zip_longest 严格均匀轮询抽取本轮窗口
    eff_limit = clean_limit if (clean_limit and clean_limit > 0) else DEFAULT_CLEAN_WINDOW
    raw_sites = [
        str(s or "").strip()
        for s in raw_col.distinct("source_site", active_pending_filter)
        if str(s or "").strip()
    ]
    sorted_sites = sorted(set(raw_sites))
    docs_by_site: Dict[str, List[Dict[str, Any]]] = {}
    for s_site in sorted_sites:
        site_query = {
            "$and": [
                {"source_site": s_site},
                {"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]},
                fresh_guard_clause,
            ]
        }
        docs_by_site[s_site] = list(
            raw_col.find(site_query, {"raw_payload": 0})
            .sort([("publish_ts", -1), ("last_seen_at", -1)])
            .limit(eff_limit)
        )
        docs_by_site[s_site].sort(key=lambda d: int(d.get("clean_retry_count") or 0))

    selected_docs: List[Dict[str, Any]] = []
    selected_keys: Set[Tuple[str, str]] = set()
    site_queues = [docs_by_site[s] for s in sorted_sites if docs_by_site.get(s)]
    for group in zip_longest(*site_queues):
        for item in group:
            if item is None:
                continue
            k = (str(item.get("source_site") or ""), str(item.get("source_item_id") or ""))
            if k not in selected_keys:
                selected_keys.add(k)
                selected_docs.append(item)
                if clean_limit and clean_limit > 0 and len(selected_docs) >= clean_limit:
                    break
        if clean_limit and clean_limit > 0 and len(selected_docs) >= clean_limit:
            break

    total_selected = len(selected_docs)
    per_site_selected = Counter(str(d.get("source_site") or "unknown") for d in selected_docs)
    site_dist_str = ", ".join(f"{k}:{v}" for k, v in sorted(per_site_selected.items()))

    logger.info(
        "🧼 ================ 开始第 #%d 轮 [扁平单窗口：各平台最新均匀清洗] "
        "(📦 库内%d天内Pending=%d条 | 本轮窗口=%d条 [AI批大小=%d, AI并发=%d, AI超时=%.0fs] | 来源均匀分布=[%s]) ================",
        cycle_num,
        DEAL_MAX_CLEAN_AGE_DAYS,
        total_pending_initial,
        total_selected,
        cleaner.ai_batch_size,
        cleaner.ai_max_workers,
        cleaner.ai_timeout,
        site_dist_str,
    )

    # Step 2: 扁平单窗口执行 Stage 1(上下文/缓存) -> Stage 2(AI-1) -> Stage 3(并发转链/去重) -> Stage 4(AI-2满载打包)
    cleaner.warmup_anchor_cache_from_db(raw_col)
    shared_active_products = cleaner._load_active_formal_products(formal_col)
    all_processed_results = cleaner.clean_batch_records(
        selected_docs,
        raw_col,
        formal_col=formal_col,
        total_pending_count=total_pending_initial,
        shared_active_products=shared_active_products,
        shared_lock=None,
        quiet_sub_logs=False,
    )

    # Step 3: 统一批量落盘更新 `deal_raw_tips` 与正式表 `products`
    commit_info = _commit_batch_results_to_db(
        all_processed_results,
        raw_col,
        formal_col,
        cycle_num,
        shared_active_products=shared_active_products,
        shared_lock=None,
    )
    formal_write_res = commit_info["formal_write_res"]
    early_resonance_modified = commit_info["early_resonance_modified"]
    success_product_docs = commit_info["success_product_docs"]
    early_terminated_results = commit_info["early_terminated_results"]
    failed_samples = commit_info["failed_samples"]
    retry_pending_cnt = commit_info["retry_pending_count"]

    batch_elapsed_s = round(time.perf_counter() - t_start, 2)
    remaining_pending_now = max(0, total_pending_initial - total_selected + retry_pending_cnt)
    logger.info(
        "💾 [窗口落盘完成] 本轮处理=%d条 (终审入库=%d, 早停共振=%d, 暂态待重试=%d, 过滤/合并=%d) | "
        "正式表新增=%d, 更新共振=%d | 总耗时=%.2fs | 📦 库内剩余Pending≈%d条",
        len(all_processed_results),
        len(success_product_docs),
        len(early_terminated_results),
        retry_pending_cnt,
        len(all_processed_results) - len(success_product_docs) - len(early_terminated_results) - retry_pending_cnt,
        formal_write_res["upserted"],
        formal_write_res["modified"] + early_resonance_modified,
        batch_elapsed_s,
        remaining_pending_now,
    )

    # ==========================================================================
    # 构建阶段二~五全维度清洗运维统计报告 (`latest_clean_ops_report.json`)
    # ==========================================================================
    total_scanned = len(all_processed_results)
    has_candidate_cnt = sum(1 for r in all_processed_results if r.get("candidate_count", 0) > 0)
    resolved_cnt = len(success_product_docs)
    early_terminated_cnt = len(early_terminated_results)
    effective_resolved_cnt = resolved_cnt + early_terminated_cnt
    merged_cnt = sum(1 for r in all_processed_results if r["status"] == "merged_into_parent")
    low_score_cnt = sum(1 for r in all_processed_results if r["status"] == "low_score")
    unique_pids_in_batch = len({
        (r["platform"], r["product_id"])
        for r in all_processed_results
        if r["status"] in ("success", "early_terminated") and r.get("platform") and r.get("product_id")
    })

    formal_total_now = formal_col.count_documents({})
    formal_deal_total_now = formal_col.count_documents({"deal_info.is_deal_tip": True})

    sites_in_batch = sorted({r["source_site"] for r in all_processed_results})
    site_stats: Dict[str, Dict[str, Any]] = {}
    for site in sites_in_batch:
        s_rows = [r for r in all_processed_results if r["source_site"] == site]
        s_total = len(s_rows)
        s_has_cand = sum(1 for r in s_rows if r.get("candidate_count", 0) > 0)
        s_ok = sum(1 for r in s_rows if r["status"] in ("success", "early_terminated"))
        s_early = sum(1 for r in s_rows if r["status"] == "early_terminated")
        s_retry = sum(1 for r in s_rows if r["status"] == "retry_pending")
        s_uniq = len({
            (r["platform"], r["product_id"])
            for r in s_rows
            if r["status"] in ("success", "early_terminated") and r.get("platform") and r.get("product_id")
        })
        err_counter = Counter(
            r["error_code"]
            for r in s_rows
            if r["status"] not in ("success", "early_terminated", "merged_into_parent")
        )
        site_stats[site] = {
            "scanned": s_total,
            "has_product_candidate": s_has_cand,
            "resolved_success": s_ok,
            "early_terminated_skipped_ai": s_early,
            "retry_pending": s_retry,
            "unique_products": s_uniq,
            "overall_resolution_rate_pct": round(s_ok / max(s_total, 1) * 100, 1),
            "candidate_resolution_rate_pct": round(s_ok / max(s_has_cand, 1) * 100, 1) if s_has_cand else 0.0,
            "failure_breakdown": dict(err_counter.most_common()),
        }

    global_err_counter = Counter(
        r["error_code"]
        for r in all_processed_results
        if r["status"] not in ("success", "early_terminated", "merged_into_parent")
    )
    plat_counter = Counter(
        r["platform"]
        for r in all_processed_results
        if r["status"] in ("success", "early_terminated") and r.get("platform")
    )

    with_publish_time = sum(1 for d in success_product_docs if d["deal_info"].get("publish_time"))
    with_tutorial_text = sum(1 for d in success_product_docs if len(d["deal_info"].get("tutorial_text") or "") >= 10)
    with_proof_images = sum(1 for d in success_product_docs if d["deal_info"].get("user_share_images"))
    with_coupon_links = sum(1 for d in success_product_docs if d["deal_info"].get("coupon_links"))
    with_deal_price = sum(1 for d in success_product_docs if d["deal_info"].get("deal_price") is not None)
    avg_quality_score = round(
        sum(int(d["deal_info"].get("quality_score") or 0) for d in success_product_docs) / max(1, resolved_cnt),
        1,
    ) if resolved_cnt else 0.0

    stage_timings = getattr(cleaner, "last_batch_stage_timings", {}) or {}
    clean_report = {
        "cycle_num": cycle_num,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_formal_collection": repo.formal_collection_name,
        "formal_collection_status": {
            "formal_products_total_count": formal_total_now,
            "formal_deal_tip_products_count": formal_deal_total_now,
            "batch_upserted_new_products": formal_write_res["upserted"],
            "batch_modified_existing_products": formal_write_res["modified"] + early_resonance_modified,
            "batch_protected_high_score_products": formal_write_res.get("protected_high_score", 0),
        },
        "global_funnel": {
            "0_expired_over_7d_swept_tips": expired_over_7d_cnt,
            "0b_max_retry_swept_tips": max_retry_swept_cnt,
            "1_total_scanned_tips": total_scanned,
            "2_has_product_candidate_tips": has_candidate_cnt,
            "3_unique_product_resolved_tips": effective_resolved_cnt,
            "3a_ai2_audited_new_tips": resolved_cnt,
            "3b_early_terminated_skipped_ai_tips": early_terminated_cnt,
            "3c_merged_neighbor_tips": merged_cnt,
            "3d_low_score_filtered_tips": low_score_cnt,
            "3e_timeout_retry_pending_tips": retry_pending_cnt,
            "4_deduplicated_unique_products_in_batch": unique_pids_in_batch,
            "avg_quality_score": avg_quality_score,
            "overall_resolution_rate_pct": round(effective_resolved_cnt / max(total_scanned, 1) * 100, 1),
            "candidate_resolution_rate_pct": round(effective_resolved_cnt / max(has_candidate_cnt, 1) * 100, 1) if has_candidate_cnt else 0.0,
            "cross_tip_duplication_rate_pct": round((1 - unique_pids_in_batch / max(effective_resolved_cnt, 1)) * 100, 1) if effective_resolved_cnt else 0.0,
            "remaining_pending_anchored_tips": remaining_pending_now,
        },
        "resolved_by_platform": dict(plat_counter),
        "enrichment_retention_metrics": {
            "resolved_total": resolved_cnt,
            "retained_publish_time_count": with_publish_time,
            "retained_publish_time_pct": round(with_publish_time / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_tutorial_text_count": with_tutorial_text,
            "retained_tutorial_text_pct": round(with_tutorial_text / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_proof_or_guide_images_count": with_proof_images,
            "retained_proof_or_guide_images_pct": round(with_proof_images / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_coupon_links_count": with_coupon_links,
            "retained_coupon_links_pct": round(with_coupon_links / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_deal_price_count": with_deal_price,
            "retained_deal_price_pct": round(with_deal_price / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
        },
        "failure_taxonomy_breakdown": {
            code: {
                "count": cnt,
                "share_of_failures_pct": round(cnt / max(total_scanned - effective_resolved_cnt - merged_cnt, 1) * 100, 1),
            }
            for code, cnt in global_err_counter.most_common()
        },
        "by_source_site": site_stats,
        "engine_efficiency_metrics": {
            "batch_elapsed_seconds": batch_elapsed_s,
            "stage2_ai1_seconds": stage_timings.get("stage2_ai1_seconds", 0.0),
            "stage3_convert_seconds": stage_timings.get("stage3_convert_seconds", 0.0),
            "stage4_ai2_seconds": stage_timings.get("stage4_ai2_seconds", 0.0),
            "avg_ms_per_anchored_record": round(batch_elapsed_s * 1000 / max(has_candidate_cnt, 1), 1) if has_candidate_cnt else 0.0,
            "anchor_cache_hits": cleaner.stats_cache_hits,
            "early_terminated_pre_ai1": cleaner.stats_early_terminated_pre_ai1,
            "early_terminated_post_stage3": cleaner.stats_early_terminated_post_stage3,
            "deal_upgrades_post_stage3": cleaner.stats_deal_upgrades_post_stage3,
            "ai_stage1_calls": cleaner.stats_ai1_calls,
            "ai_stage1_model_hits": cleaner.stats_ai1_model_hits,
            "ai_stage1_fallbacks": cleaner.stats_ai1_fallbacks,
            "ai_stage2_calls": cleaner.stats_ai2_calls,
            "ai_stage2_model_hits": cleaner.stats_ai2_model_hits,
            "ai_stage2_fallbacks": cleaner.stats_ai2_fallbacks,
            "ai_retries_scheduled": cleaner.stats_ai_retries_scheduled,
        },
        "engineering_philosophy_manifest": ENGINEERING_PHILOSOPHY_MANIFEST,
        "attempted_dead_ends_manifest": CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
    }

    try:
        with open(LATEST_CLEAN_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(clean_report, f, ensure_ascii=False, indent=2)
        _append_bounded_jsonl_lines(
            CLEAN_OPS_HISTORY_JSONL,
            [json.dumps(clean_report, ensure_ascii=False)],
        )
        if failed_samples:
            _append_bounded_jsonl_lines(
                CLEAN_FAILED_SAMPLES_JSONL,
                [json.dumps(item, ensure_ascii=False) for item in failed_samples[:200]],
            )
        with open(CLEAN_SUCCESS_PREVIEW_JSONL, "w", encoding="utf-8") as f:
            for doc in success_product_docs[:50]:
                f.write(json.dumps(doc, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("写入阶段二清洗报表文件异常: %s", e)

    gf = clean_report["global_funnel"]
    er = clean_report["enrichment_retention_metrics"]
    fc = clean_report["formal_collection_status"]
    em = clean_report["engine_efficiency_metrics"]
    plat_str = f"京东(jd)={plat_counter.get('jd', 0)} | 淘宝(taobao)={plat_counter.get('taobao', 0)} | 拼多多(pdd)={plat_counter.get('pdd', 0)}"
    top_global_errs = ", ".join(f"{k}:{v}" for k, v in global_err_counter.most_common(4)) or "无"
    lines = [
        "",
        "┌" + "─" * 98 + "┐",
        f"│ 🧼 【第 #{cycle_num} 轮 Stage 2~5：各平台最新均匀抽样 + 扁平双阶段 AI + 并发转链 (`{repo.formal_collection_name}`) 报告】│",
        "├" + "─" * 98 + "┤",
        (
            f"│ • 全局转化漏斗 : 扫描Pending={gf['1_total_scanned_tips']}条 -> "
            f"含候选={gf['2_has_product_candidate_tips']}条 -> "
            f"有效命中={gf['3_unique_product_resolved_tips']}条 "
            f"(AI-2新审={resolved_cnt}条[均分{gf['avg_quality_score']}] + ⚡早停跳过AI={early_terminated_cnt}条) | "
            f"群聊合并={merged_cnt}条 | 暂态待重试={retry_pending_cnt}条 | 超7天清扫={expired_over_7d_cnt}条"
        ),
        (
            f"│ • 终审转化率   : 候选有效率={gf['candidate_resolution_rate_pct']}% | "
            f"总有效率={gf['overall_resolution_rate_pct']}% | "
            f"本批去重独立商品={gf['4_deduplicated_unique_products_in_batch']}个 | 剩余Pending={gf['remaining_pending_anchored_tips']}条"
        ),
        f"│ • 平台分布明细 : {plat_str}",
        (
            f"│ • 正式表入库   : 正式表 `{repo.formal_collection_name}` 本轮新增={fc['batch_upserted_new_products']}个, "
            f"更新共振={fc['batch_modified_existing_products']}个 (高分保护防覆盖={fc['batch_protected_high_score_products']}次) | "
            f"累计好价商品={fc['formal_deal_tip_products_count']}个"
        ),
        (
            f"│ • 引擎与AI指标 : 总耗={batch_elapsed_s}s (AI1={em['stage2_ai1_seconds']}s / 并发转链={em['stage3_convert_seconds']}s / AI2={em['stage4_ai2_seconds']}s) | "
            f"AI-1(直出={cleaner.stats_ai1_model_hits}) | AI-2(直出={cleaner.stats_ai2_model_hits}) | "
            f"早停累计={cleaner.stats_early_terminated_pre_ai1 + cleaner.stats_early_terminated_post_stage3} | 择优升级={cleaner.stats_deal_upgrades_post_stage3} | 缓存命中={cleaner.stats_cache_hits}"
        ),
        (
            f"│ • 专有信息保留 : 爆料时间={er['retained_publish_time_pct']}% | "
            f"重构教程={er['retained_tutorial_text_pct']}% | "
            f"爆料价={er['retained_deal_price_pct']}% | "
            f"晒单/步骤图={er['retained_proof_or_guide_images_pct']}%({er['retained_proof_or_guide_images_count']}条) | "
            f"附带优惠券链={er['retained_coupon_links_pct']}%({er['retained_coupon_links_count']}条)"
        ),
        f"│ • 全局拦截分布 : {top_global_errs}",
        "├" + "─" * 98 + "┤",
        f"│ {'站点 (source_site)':<15} | {'扫描数':>6} | {'含候选数':>8} | {'有效命中':>8} | {'早停跳AI':>8} | {'有效率':>8} | {'独立商品':>6} | Top 拦截/重试原因",
        "├" + "─" * 98 + "┤",
    ]
    for site, st in sorted(site_stats.items(), key=lambda x: x[1]["resolved_success"], reverse=True):
        top_err = ", ".join(f"{k}:{v}" for k, v in list(st["failure_breakdown"].items())[:2]) or "-"
        lines.append(
            f"│ {site:<15} | {st['scanned']:>6} | {st['has_product_candidate']:>8} | "
            f"{st['resolved_success']:>8} | {st['early_terminated_skipped_ai']:>8} | "
            f"{st['candidate_resolution_rate_pct']:>7.1f}% | "
            f"{st['unique_products']:>8} | {top_err}"
        )
    lines.append("└" + "─" * 98 + "┘")
    logger.info("\n".join(lines))

    return clean_report


# ==============================================================================
# 每轮综合运维拉取与清洗排障诊断报告生成器
# ==============================================================================


def run_stage_pipeline_drain(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    clean_limit: int = 0,
    timeout_seconds: float = 3600.0,
) -> Dict[str, Any]:
    """排空模式：并发流水线作业，直至库内所有阶段待处理记录全部排空或达到目标上限。"""
    t_start = time.perf_counter()
    repo.migrate_pending_to_stage_machine()
    cleaner.warmup_anchor_cache_from_db(repo.col)
    shared_active_products = cleaner._load_active_formal_products(repo.formal_col)
    shared_lock = threading.Lock()
    ai_semaphore = threading.BoundedSemaphore(cleaner.ai_max_workers)

    stop_event = threading.Event()
    ai1_done_event = threading.Event()
    convert_done_event = threading.Event()

    ai1_batch = max(64, cleaner.ai_batch_size * cleaner.ai_max_workers * 2)
    if clean_limit > 0:
        ai1_batch = max(cleaner.ai_batch_size, clean_limit)

    convert_batch = 200
    ai2_batch = 100

    worker_ai1 = StageWorker(
        name="StageWorker-AI1",
        stage=CLEAN_STAGE_NEED_AI1,
        repo=repo,
        cleaner=cleaner,
        batch_size=ai1_batch,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
        poll_interval=1.0,
        stop_event=stop_event,
        ai_semaphore=ai_semaphore,
        upstream_event=None,
        downstream_event=ai1_done_event,
        shared_active_products=shared_active_products,
        shared_lock=shared_lock,
    )
    worker_convert = StageWorker(
        name="StageWorker-Convert",
        stage=CLEAN_STAGE_NEED_CONVERT,
        repo=repo,
        cleaner=cleaner,
        batch_size=convert_batch,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
        poll_interval=1.0,
        stop_event=stop_event,
        ai_semaphore=None,
        upstream_event=ai1_done_event,
        downstream_event=convert_done_event,
        shared_active_products=shared_active_products,
        shared_lock=shared_lock,
    )
    worker_ai2 = StageWorker(
        name="StageWorker-AI2",
        stage=CLEAN_STAGE_NEED_AI2,
        repo=repo,
        cleaner=cleaner,
        batch_size=ai2_batch,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
        poll_interval=1.0,
        stop_event=stop_event,
        ai_semaphore=ai_semaphore,
        upstream_event=convert_done_event,
        downstream_event=None,
        shared_active_products=shared_active_products,
        shared_lock=shared_lock,
    )
    worker_maint = MaintenanceWorker(repo=repo, poll_interval=15.0, stop_event=stop_event)

    logger.info(
        "🚀 [阶段化流水线启动] AI-1 (批=%d, 并发=%d) | 转链 (批=%d, 并发=%d) | AI-2 (批=%d, 并发=%d) | 排空模式运行中...",
        ai1_batch,
        cleaner.ai_max_workers,
        convert_batch,
        cleaner.convert_max_workers,
        ai2_batch,
        cleaner.ai_max_workers,
    )

    worker_ai1.start()
    worker_convert.start()
    worker_ai2.start()
    worker_maint.start()

    idle_count = 0
    try:
        while time.perf_counter() - t_start < timeout_seconds:
            p_ai1 = repo.col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI1})
            p_conv = repo.col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_CONVERT})
            p_ai2 = repo.col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI2})
            total_pending = p_ai1 + p_conv + p_ai2
            total_done = worker_ai2.total_processed

            if clean_limit > 0 and total_done >= clean_limit:
                logger.info("🏁 已达到目标处理上限 (--clean-limit=%d)，通知各阶段 Worker 结束。", clean_limit)
                break

            if total_pending == 0:
                idle_count += 1
                if idle_count >= 2:
                    logger.info("✨ 所有阶段队列均已排空 (pending=0)，流水线处理完毕。")
                    break
            else:
                idle_count = 0

            time.sleep(2.0)
    finally:
        stop_event.set()
        ai1_done_event.set()
        convert_done_event.set()
        worker_ai1.join(timeout=3.0)
        worker_convert.join(timeout=3.0)
        worker_ai2.join(timeout=3.0)
        worker_maint.join(timeout=2.0)

    elapsed_s = round(time.perf_counter() - t_start, 2)
    logger.info(
        "🏁 [阶段化流水线排空完成] 耗时=%.2fs | AI-1处理=%d条, 转链处理=%d条, AI-2终审=%d条",
        elapsed_s,
        worker_ai1.total_processed,
        worker_convert.total_processed,
        worker_ai2.total_processed,
    )
    return {
        "elapsed_s": elapsed_s,
        "ai1_processed": worker_ai1.total_processed,
        "convert_processed": worker_convert.total_processed,
        "ai2_processed": worker_ai2.total_processed,
    }


def run_single_cycle(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: Optional[int] = None,
    run_all_tiers: bool = False,
    clean_limit: int = DEFAULT_CLEAN_WINDOW,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
    channels: Optional[List[DealChannelConfig]] = None,
    channel_last_run: Optional[Dict[str, float]] = None,
    site_cooldown_until: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """执行单轮完整【阶段一：拉取入初步表】+【阶段二~五：清洗入正式表】闭环。"""
    fetch_res = run_fetch_stage(
        repo=repo,
        session=session,
        cycle_num=cycle_num,
        max_new_details_per_channel=max_new_details_per_channel,
        run_all_tiers=run_all_tiers,
        channel_health_tracker=channel_health_tracker,
        channels=channels,
        channel_last_run=channel_last_run,
        site_cooldown_until=site_cooldown_until,
    )
    clean_report = run_clean_stage(
        repo=repo,
        cleaner=cleaner,
        cycle_num=cycle_num,
        clean_limit=clean_limit,
    )
    summary = repo.generate_pool_summary()
    try:
        with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        if os.path.exists(LATEST_OPS_REPORT_PATH):
            with open(LATEST_OPS_REPORT_PATH, "r", encoding="utf-8") as f:
                ops_rep = json.load(f)
            ops_rep["clean_stage_summary"] = clean_report
            ops_rep["pool_cleaned_success_records"] = summary.get("cleaned_success_records", 0)
            ops_rep["pool_cleaned_failed_records"] = summary.get("cleaned_failed_records", 0)
            ops_rep["pool_pending_clean_records"] = summary.get("pending_clean_records", 0)
            ops_rep["formal_products_total"] = summary.get("formal_products_total", 0)
            ops_rep["formal_deal_products_count"] = summary.get("formal_deal_products_count", 0)
            with open(LATEST_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
                json.dump(ops_rep, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {
        **fetch_res,
        "clean_report": clean_report,
        "pool_summary": summary,
    }


def print_stats_report(repo: DealRawPoolRepository):
    """在终端打印直观的初步表 (`deal_raw_tips`) 与正式表 (`products`) 全景统计报告。"""
    summary = repo.generate_pool_summary()
    print("\n" + "=" * 98)
    print(f"📊 好价线报初步表 (`{summary['raw_collection']}`) -> 正式表 (`{summary['formal_collection']}`) 全景统计报告 ({summary['generated_at']})")
    print("=" * 98)
    print(
        f"  • 初步表累计去重记录数     : {summary['total_records']} 条 "
        f"(含完整详情: {summary['full_detail_records']} 条, 含唯一商品硬锚点: {summary['unique_product_anchor_records']} 条 / {summary['unique_product_anchor_ratio']}%)"
    )
    print(
        f"  • 初步表清洗状态分布       : 已解析入库(cleaned_success)={summary['cleaned_success_records']} 条 | "
        f"已识别拦截(cleaned_failed)={summary['cleaned_failed_records']} 条 | 待清洗(pending)={summary['pending_clean_records']} 条"
    )
    print(
        f"  • 正式表 `{summary['formal_collection']}` 商品统计 : 累计沉淀好价线报唯一商品={summary['formal_deal_products_count']} 个 "
        f"(正式表全库商品总数={summary['formal_products_total']} 个)"
    )
    print(f"  • 清洗拦截错误码分布       : {json.dumps(summary['by_clean_error_code'], ensure_ascii=False)}")
    print("-" * 98)
    print("  • 纳入监控的 8 大高可靠站点入池与清洗入库明细 :")
    for s in summary.get("by_source_site") or []:
        cnt = max(1, s["count"])
        u_cnt = s.get("unique_anchor_count", 0)
        ok_cnt = s.get("cleaned_success_count", 0)
        fail_cnt = s.get("cleaned_failed_count", 0)
        pend_cnt = s.get("pending_count", 0)
        print(
            f"    - {str(s['_id']):<14} : 初步表={s['count']:>4}条 | "
            f"含锚点={u_cnt:>4}条 ({u_cnt * 100.0 / cnt:>5.1f}%) | "
            f"已入正式表={ok_cnt:>4}条 | 已拦截={fail_cnt:>4}条 | 待清洗={pend_cnt:>4}条"
        )
    print("-" * 98)
    print(f"  • 核心工程准则备忘 ({len(ENGINEERING_PHILOSOPHY_MANIFEST)} 条铁律) :")
    for pk, pv in ENGINEERING_PHILOSOPHY_MANIFEST.items():
        print(f"    🧭 {pk}: {pv[:110]}...")
    print("-" * 98)
    print(f"  • 采集阶段已尝试并剔除的 {len(EXCLUDED_SITES_AND_CHANNELS_MANIFEST)} 个站点/频道备忘 (严禁重复踩坑) :")
    for ex_name, ex_reason in EXCLUDED_SITES_AND_CHANNELS_MANIFEST.items():
        print(f"    ❌ {ex_name}: {ex_reason[:110]}...")
    print("-" * 98)
    print(f"  • 清洗阶段历史探索与回归极简基线备忘 ({len(CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST)} 条) :")
    for de_key, de_desc in CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST.items():
        print(f"    🛑 {de_key}: {de_desc[:110]}...")
    print("=" * 98 + "\n")


def print_latest_ops_report():
    """读取并打印最近一轮的综合拉取与清洗运维报告。"""
    print("\n" + "=" * 98)
    print("🛠️ 最近一轮好价拉取与唯一商品清洗运维诊断报告 (Ops Fetch & Clean Report)")
    print("=" * 98)
    if os.path.exists(LATEST_OPS_REPORT_PATH):
        try:
            with open(LATEST_OPS_REPORT_PATH, "r", encoding="utf-8") as f:
                rep = json.load(f)
            print(f"  • 报告轮次/时间 : 第 #{rep.get('cycle_num')} 轮 ({rep.get('generated_at')}) | 模式={rep.get('cycle_mode')}")
            print(f"  • 整体健康度    : {rep.get('overall_health')}")
            print(
                f"  • 阶段一(拉取)  : 调度={rep.get('channels_scheduled')}个 "
                f"(🟢正常={rep.get('channels_ok')} | 🟡空列表={rep.get('channels_warn')} | 🔴失败={rep.get('channels_fail')}) "
                f"| 新增={rep.get('total_inserted')} 更新={rep.get('total_updated')} | 耗时={rep.get('fetch_elapsed_s')}s"
            )
        except Exception as ex:
            print(f"  读取 {LATEST_OPS_REPORT_PATH} 失败: {ex}")
    else:
        print(f"  尚未生成 `{LATEST_OPS_REPORT_PATH}`，请先运行至少一轮。")
