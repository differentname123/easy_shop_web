# -*- coding: utf-8 -*-
"""
好价线报多阶段流水线与拉取调度引擎 (`DealPipeline`)。
负责 Producer 阶段（`run_fetch_stage`，支持多源独立调度间隔）与 Consumer 阶段（`run_stage_pipeline_drain`）。
"""
import datetime
import json
import logging
import os
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from infrastructure.runtime_io import (
    atomic_write_json,
    should_write_runtime_artifact,
)
from models.deal_constants import (
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
    ERROR_LOG_JSONL_PATH,
    LATEST_OPS_REPORT_PATH,
    LOG_DIR,
    OPS_REPORT_HISTORY_JSONL,
    STATS_SNAPSHOT_PATH,
)
from models.product import SOURCE_SITE_NAMES
from repositories.deal_raw_pool_repo import (
    DealRawPoolRepository,
    _append_bounded_jsonl_lines,
    append_error_records_jsonl,
    evaluate_quality_signals,
)
from services.deal_sources.base import (
    BaseDealSource,
    ChannelConfig,
    DealChannelConfig,
    FetchContext,
)
from services.deal_sources.catalog import (
    get_active_channels,
    is_channel_due,
)
from services.deal_sources.filters import (
    has_valid_detail_and_tutorial,
    is_record_confirmed_unsupported,
)
from services.deal_sources.http_client import (
    RATE_LIMIT_STATUSES,
    create_site_session,
)
from services.deal_sources.registry import (
    get_source_troubleshooting,
    list_sources,
)
from services.deal_pool_cleaner import (
    DealPoolCleaner,
    MaintenanceWorker,
    StageWorker,
)

logger = logging.getLogger("deal_pool_scheduler")

# ==============================================================================
# 站点显示名称与排障手册（单一真源：复用 models.product 与各数据源插件）
# ==============================================================================
SITE_DISPLAY_NAMES: Dict[str, str] = SOURCE_SITE_NAMES


def get_ops_troubleshooting_advice(
    site_key: str,
    http_status: Optional[int],
    error_type: str,
    fetched_count: int,
    consecutive_failures: int,
) -> str:
    """根据站点标识、HTTP 状态码、错误类型及连续失败次数，生成明确的运维人员处理建议。"""
    playbook = get_source_troubleshooting(site_key)
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


# 全局多源调度时间记录器：key=channel.name, value=上次运行的 epoch 时间戳
_GLOBAL_CHANNEL_LAST_RUN: Dict[str, float] = {}


def _write_json_artifact(path: str, payload: Any) -> None:
    if should_write_runtime_artifact(path, LOG_DIR):
        atomic_write_json(path, payload)


def run_fetch_stage(
    repo: DealRawPoolRepository,
    session: Optional[requests.Session] = None,
    cycle_num: int = 1,
    max_new_details_per_channel: Optional[int] = None,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
    latest_clean_report: Optional[Dict[str, Any]] = None,
    channels: Optional[List[ChannelConfig]] = None,
    channel_last_run: Optional[Dict[str, float]] = None,
    site_cooldown_until: Optional[Dict[str, float]] = None,
    source_registry: Optional[Dict[str, BaseDealSource]] = None,
    session_factory: Optional[Callable[[ChannelConfig], requests.Session]] = None,
) -> Dict[str, Any]:
    """
    执行单轮【阶段一（Producer）：多站点插件并行拉取入初步表 `deal_raw_tips`】：
    - 仅运行 `enabled=True` 且已到自身 `interval_seconds`（且所属站点未处于冷却期）的频道；
    - 生产环境每个站点车道独立创建线程隔离的 HTTP Session（按频道 `network_mode` / `close_connection`）；
    - 数据库写入与全量失效摘除在站点车道内异步直落，全局锁仅保护共享内存统计字典。
    """
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

    # 按站点分组：不同站点彼此并行拉取；同一站点内部随机打乱到期频道顺序，
    # 保证长列表/热门/实时等多个频道在单站限流窗口下公平轮转出单，避免首个频道长期饿死后续频道。
    channels_by_site: Dict[str, List[DealChannelConfig]] = defaultdict(list)
    for ch in due_channels:
        channels_by_site[ch.site_key].append(ch)
    site_items: List[Tuple[str, List[DealChannelConfig]]] = list(channels_by_site.items())
    random.shuffle(site_items)
    for _, site_channels in site_items:
        random.shuffle(site_channels)

    active_sites = sorted({ch.site_key for ch in due_channels if ch.site_key})
    dedup_map: Dict[str, Set[str]] = {
        site: repo.get_existing_detailed_ids(site, limit=50000)
        for site in active_sites
    }

    total_inserted = 0
    total_updated = 0
    total_fetched = 0
    task_reports: List[Dict[str, Any]] = []
    cycle_error_paths: List[Dict[str, Any]] = []
    total_channels = len(due_channels)
    task_counter = 0

    try:
        cur_pending_cnt = repo.col.count_documents(
            {"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]}
        )
    except Exception:
        cur_pending_cnt = 0

    logger.info(
        "================ 开始第 #%d 轮 [阶段1-线报拉取] (本轮到期 %d/%d 个已启用频道 | %d 站点并行·同站随机 | 📦 库内Pending=%d条) ================",
        cycle_num,
        total_channels,
        len(active_channels),
        len(site_items),
        cur_pending_cnt,
    )

    known_site_keys = {ch.site_key for ch in active_channels if ch.site_key}
    rate_limited_sites: Set[str] = {
        key
        for key, until_ts in cooldown_map.items()
        if until_ts and until_ts > now_wall and key in known_site_keys
    }
    rate_limited_channels: Set[str] = {
        key
        for key, until_ts in cooldown_map.items()
        if until_ts and until_ts > now_wall and key not in known_site_keys
    }
    _rate_limit_codes = set(RATE_LIMIT_STATUSES)
    active_registry = source_registry if source_registry is not None else list_sources()
    lock = threading.Lock()

    def _execute_site_lane(site_key: str, site_channels: List[ChannelConfig]) -> None:
        nonlocal total_inserted, total_updated, total_fetched, task_counter
        site_dedup = dedup_map.get(site_key)

        for ch in site_channels:
            task_name = ch.name
            default_url = ch.default_url
            pause_sec = ch.sleep_after
            t0 = time.perf_counter()

            with lock:
                task_counter += 1
                task_idx = task_counter
                rem_tasks = total_channels - task_idx
                ch_state = channel_health_tracker.setdefault(
                    task_name,
                    {"consecutive_failures": 0, "total_runs": 0, "total_errors": 0, "last_error_url": ""},
                )
                is_site_limited = (site_key in rate_limited_sites) or (task_name in rate_limited_channels)
                if not is_site_limited:
                    ch_state["total_runs"] += 1

            if is_site_limited:
                logger.info(
                    "  ⏸️ [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 站点 [%s] 处于限流/验证码冷却期，跳过本轮",
                    task_idx,
                    total_channels,
                    rem_tasks,
                    task_name,
                    site_key,
                )
                with lock:
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

            source = active_registry.get(ch.resolved_source_key)
            if source is None:
                logger.error("未找到频道 [%s] 对应的数据源插件 [%s]，请检查注册表！", task_name, ch.resolved_source_key)
                with lock:
                    last_run_map[task_name] = time.time()
                    ch_state["consecutive_failures"] += 1
                    ch_state["total_errors"] += 1
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
                        "error_type": "SourceNotRegistered",
                        "error": f"Unknown source_key: {ch.resolved_source_key}",
                        "elapsed_ms": 0.0,
                    })
                continue

            owns_session = False
            if session_factory is not None:
                lane_session = session_factory(ch)
                owns_session = hasattr(lane_session, "close")
            elif session is not None:
                lane_session = session
            else:
                lane_session = create_site_session(
                    network_mode=ch.network_mode,
                    close_connection=ch.close_connection,
                )
                owns_session = True

            sweep_start_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            try:
                ctx = FetchContext(
                    channel=ch,
                    session=lane_session,
                    known_ids=site_dedup if site_dedup is not None else set(),
                    now_ts=time.time(),
                    max_details=max_new_details_per_channel,
                )
                raw_res = source.fetch(ctx)
                res_dict = raw_res.to_dict() if hasattr(raw_res, "to_dict") else dict(raw_res)
                now_done = time.time()
                req_url = str(res_dict.get("request_url") or default_url)
                http_status = res_dict.get("http_status")
                raw_records = res_dict.get("records") or []
                detail_errors = res_dict.get("detail_errors") or []

                has_captcha_err = any("Captcha" in str(d.get("error_type") or "") for d in detail_errors)
                is_http_rate_limited = (
                    http_status in _rate_limit_codes
                    or any(d.get("http_status") in _rate_limit_codes for d in detail_errors)
                )
                is_rate_limited = (
                    bool(res_dict.get("rate_limited"))
                    or is_http_rate_limited
                    or has_captcha_err
                )

                fetch_ts = time.time()
                records: List[Dict[str, Any]] = []
                pipeline_filtered = 0
                pipeline_skipped_known = 0
                for r in raw_records:
                    if not isinstance(r, dict):
                        continue
                    if not r.get("has_full_detail", True):
                        pipeline_filtered += 1
                        continue
                    if is_record_confirmed_unsupported(r):
                        pipeline_filtered += 1
                        continue
                    if not has_valid_detail_and_tutorial(r):
                        pipeline_filtered += 1
                        continue
                    sid = str(r.get("source_item_id") or "")
                    if site_dedup is not None and sid and sid in site_dedup:
                        pipeline_skipped_known += 1
                        continue
                    orig_ch = str(r.get("source_channel") or "").strip()
                    if orig_ch and orig_ch != task_name:
                        hm = r.setdefault("hot_metrics", {})
                        if isinstance(hm, dict) and "sub_channel" not in hm:
                            hm["sub_channel"] = orig_ch
                    r["source_channel"] = task_name
                    r["source_is_reliable"] = bool(ch.source_is_reliable)
                    if ch.use_fetch_time_as_publish_time:
                        r["publish_ts"] = fetch_ts
                        r.pop("publish_time", None)
                    records.append(r)

                if site_dedup is not None:
                    for r in records:
                        if r.get("has_full_detail", True) and r.get("source_item_id"):
                            site_dedup.add(str(r["source_item_id"]))

                touch_ids = res_dict.get("touch_ids") or []
                if touch_ids:
                    repo.touch_seen(
                        site_key,
                        touch_ids,
                        channel=task_name,
                        refresh_publish_time=ch.use_fetch_time_as_publish_time,
                        max_age_days=ch.max_age_days,
                    )

                unique_cnt = sum(
                    1 for r in records
                    if evaluate_quality_signals(r)["has_unique_product_anchor"]
                )

                err_item_detail: Optional[Dict[str, Any]] = None
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
                    err_item_detail = {
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
                            fetched_count=len(records),
                            consecutive_failures=1,
                        ),
                    }

                db_res = repo.save_batch(
                    records,
                    task_name=task_name,
                    refresh_publish_time=ch.use_fetch_time_as_publish_time,
                    max_age_days=ch.max_age_days,
                )
                raw_list_cnt = int(
                    res_dict.get("raw_list_count")
                    if res_dict.get("raw_list_count") is not None
                    else len(raw_records)
                )
                if (
                    ch.expire_missing_on_sweep
                    and res_dict.get("status") == "ok"
                    and res_dict.get("sweep_completed")
                    and not is_rate_limited
                    and raw_list_cnt > 0
                ):
                    repo.expire_missing_from_full_sweep(
                        source_site=site_key,
                        channel=task_name,
                        sweep_start_time=sweep_start_time,
                    )

                cost_ms = round((time.perf_counter() - t0) * 1000, 1)
                ins = db_res.get("inserted", 0)
                upd = db_res.get("updated", 0)
                cnt = len(records)
                det_cnt = res_dict.get("detail_fetched", cnt)
                skipped_known = int(res_dict.get("skipped_known") or 0) + pipeline_skipped_known
                skipped_unsup = int(res_dict.get("skipped_unsupported") or 0) + pipeline_filtered
                skipped_nodet = int(res_dict.get("skipped_no_detail") or 0)

                with lock:
                    last_run_map[task_name] = now_done
                    if is_rate_limited:
                        cooldown_sec = max(30, int(getattr(ch, "rate_limit_cooldown_seconds", 300)))
                        if has_captcha_err and not is_http_rate_limited:
                            # 页面级验证码仅冷却触发验证的当前频道，避免连坐同站健康实时流频道
                            rate_limited_channels.add(task_name)
                            cooldown_map[task_name] = now_done + cooldown_sec
                        else:
                            rate_limited_sites.add(site_key)
                            cooldown_map[site_key] = now_done + cooldown_sec

                    total_inserted += ins
                    total_updated += upd
                    total_fetched += cnt
                    if err_item_detail is not None:
                        cycle_error_paths.append(err_item_detail)

                    if is_rate_limited and cnt == 0:
                        ch_state["consecutive_failures"] += 1
                        status_label = "rate_limited"
                    elif res_dict.get("status") != "ok" and cnt == 0:
                        ch_state["consecutive_failures"] += 1
                        ch_state["total_errors"] += 1
                        status_label = "error"
                    elif raw_list_cnt == 0 and http_status == 200 and cnt == 0 and not res_dict.get("allow_empty_ok"):
                        ch_state["consecutive_failures"] += 1
                        status_label = "empty_warning"
                    else:
                        ch_state["consecutive_failures"] = 0
                        status_label = "success"
                    consec_fail = ch_state["consecutive_failures"]

                    task_report = {
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
                        "consecutive_failures": consec_fail,
                        "elapsed_ms": cost_ms,
                    }
                    if status_label == "error":
                        task_report["error"] = str(
                            res_dict.get("error")
                            or (detail_errors[0].get("error") if detail_errors else f"HTTP {http_status}")
                        )[:240]
                    task_reports.append(task_report)

                if detail_errors:
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

                if status_label == "rate_limited":
                    logger.warning(
                        "  🟠 [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 触发风控/验证码熔断，已进入 %ds 冷却 (耗时=%.1fms)",
                        task_idx,
                        total_channels,
                        rem_tasks,
                        task_name,
                        max(30, int(getattr(ch, "rate_limit_cooldown_seconds", 300))),
                        cost_ms,
                    )
                elif status_label == "error":
                    err_hint = str(
                        res_dict.get("error")
                        or (summary_msg if detail_errors else f"status={res_dict.get('status')}, http={http_status}")
                    )
                    logger.warning(
                        "  ❌ [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 抓取失败 (http=%s, 连续=%d轮, 耗时=%.1fms) | %s",
                        task_idx,
                        total_channels,
                        rem_tasks,
                        task_name,
                        http_status,
                        consec_fail,
                        cost_ms,
                        err_hint,
                    )
                elif status_label == "empty_warning":
                    logger.warning(
                        "  🟡 [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 列表解析为 0 条 (HTTP 200, 耗时=%.1fms)",
                        task_idx,
                        total_channels,
                        rem_tasks,
                        task_name,
                        cost_ms,
                    )
                else:
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
            except Exception as e:
                now_done = time.time()
                cost_ms = round((time.perf_counter() - t0) * 1000, 1)
                err_type = type(e).__name__
                with lock:
                    last_run_map[task_name] = now_done
                    ch_state["consecutive_failures"] += 1
                    ch_state["total_errors"] += 1
                    ch_state["last_error_url"] = default_url
                    consec_fail = ch_state["consecutive_failures"]
                    advice = get_ops_troubleshooting_advice(
                        site_key=site_key,
                        http_status=None,
                        error_type=err_type,
                        fetched_count=0,
                        consecutive_failures=consec_fail,
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
                        "consecutive_failures": consec_fail,
                        "ops_advice": advice,
                    }
                    cycle_error_paths.append(err_item)
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
                        "consecutive_failures": consec_fail,
                        "error_type": err_type,
                        "error": str(e)[:240],
                        "elapsed_ms": cost_ms,
                    })
                logger.warning(
                    "  ❌ [阶段1-拉取 %02d/%02d(剩%02d)] [%-22s] 拉取异常 (连续=%d轮, 耗时=%.1fms) | 错误路径=%s | 异常=%s: %s | 运维建议=%s",
                    task_idx,
                    total_channels,
                    rem_tasks,
                    task_name,
                    consec_fail,
                    cost_ms,
                    default_url,
                    err_type,
                    e,
                    advice,
                )
            finally:
                if owns_session:
                    try:
                        lane_session.close()
                    except Exception:
                        pass

            if pause_sec > 0:
                time.sleep(pause_sec)

    if len(site_items) > 1:
        with ThreadPoolExecutor(max_workers=min(12, len(site_items)), thread_name_prefix="DealFetchSite") as pool:
            futures = [pool.submit(_execute_site_lane, sk, chs) for sk, chs in site_items]
            for fut in as_completed(futures):
                fut.result()
    else:
        for sk, chs in site_items:
            _execute_site_lane(sk, chs)

    append_error_records_jsonl(cycle_error_paths, path=ERROR_LOG_JSONL_PATH)
    fetch_elapsed_s = round(time.perf_counter() - fetch_start, 2)

    summary = repo.generate_pool_summary()
    try:
        _write_json_artifact(STATS_SNAPSHOT_PATH, summary)
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
        _write_json_artifact(LATEST_OPS_REPORT_PATH, report)
        _append_bounded_jsonl_lines(
            OPS_REPORT_HISTORY_JSONL,
            [json.dumps(report, ensure_ascii=False)],
        )
    except Exception as e:
        logger.error("写入运维报告失败: %s", e)

    return report


class DealPipelineRunner:
    """统一编排 StageWorker-AI1 / Convert / AI2 与 MaintenanceWorker 生命周期（供排空模式与常驻守护复用）。"""

    def __init__(
        self,
        repo: DealRawPoolRepository,
        cleaner: DealPoolCleaner,
        *,
        clean_limit: int = 0,
        stop_event: Optional[threading.Event] = None,
        ai1_upstream_event: Optional[threading.Event] = None,
        ai1_poll_interval: float = 1.0,
        convert_poll_interval: float = 1.0,
        ai2_poll_interval: float = 1.0,
        maint_poll_interval: float = 15.0,
    ):
        self.repo = repo
        self.cleaner = cleaner
        self.stop_event = stop_event or threading.Event()
        self.ai1_done_event = threading.Event()
        self.convert_done_event = threading.Event()
        self.ai_semaphore = threading.BoundedSemaphore(cleaner.ai_max_workers)
        cleaner.set_ai_semaphore(self.ai_semaphore)

        ai1_batch = max(64, cleaner.ai_batch_size * cleaner.ai_max_workers * 2)
        if clean_limit > 0:
            ai1_batch = max(cleaner.ai_batch_size, clean_limit)

        self.worker_ai1 = StageWorker(
            name="StageWorker-AI1",
            stage=CLEAN_STAGE_NEED_AI1,
            repo=repo,
            cleaner=cleaner,
            batch_size=ai1_batch,
            lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
            poll_interval=ai1_poll_interval,
            stop_event=self.stop_event,
            ai_semaphore=self.ai_semaphore,
            upstream_event=ai1_upstream_event,
            downstream_event=self.ai1_done_event,
        )
        self.worker_convert = StageWorker(
            name="StageWorker-Convert",
            stage=CLEAN_STAGE_NEED_CONVERT,
            repo=repo,
            cleaner=cleaner,
            batch_size=200,
            lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
            poll_interval=convert_poll_interval,
            stop_event=self.stop_event,
            ai_semaphore=None,
            upstream_event=self.ai1_done_event,
            downstream_event=self.convert_done_event,
        )
        self.worker_ai2 = StageWorker(
            name="StageWorker-AI2",
            stage=CLEAN_STAGE_NEED_AI2,
            repo=repo,
            cleaner=cleaner,
            batch_size=100,
            lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
            poll_interval=ai2_poll_interval,
            stop_event=self.stop_event,
            ai_semaphore=self.ai_semaphore,
            upstream_event=self.convert_done_event,
            downstream_event=None,
        )
        self.worker_maint = MaintenanceWorker(
            repo=repo,
            poll_interval=maint_poll_interval,
            stop_event=self.stop_event,
        )
        self.ai1_batch = ai1_batch

    def start(self) -> None:
        self.worker_ai1.start()
        self.worker_convert.start()
        self.worker_ai2.start()
        self.worker_maint.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.ai1_done_event.set()
        self.convert_done_event.set()

    def join(self, timeout_per_worker: float = 3.0) -> None:
        for w in (self.worker_ai1, self.worker_convert, self.worker_ai2):
            w.join(timeout=timeout_per_worker)
        self.worker_maint.join(timeout=min(2.0, timeout_per_worker))

    def processed_counts(self) -> Dict[str, int]:
        return {
            "ai1_processed": self.worker_ai1.total_processed,
            "convert_processed": self.worker_convert.total_processed,
            "ai2_processed": self.worker_ai2.total_processed,
        }


def run_stage_pipeline_drain(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    clean_limit: int = 0,
    timeout_seconds: float = 3600.0,
) -> Dict[str, Any]:
    """排空模式：并发流水线作业，直至库内所有阶段待处理记录全部排空或达到目标上限。"""
    t_start = time.perf_counter()
    repo.migrate_pending_to_stage_machine()
    runner = DealPipelineRunner(repo=repo, cleaner=cleaner, clean_limit=clean_limit)

    logger.info(
        "🚀 [阶段化流水线启动] AI-1(批=%d, 并发=%d) | 转链(批=200, 并发=%d) | AI-2(批=100, 并发=%d) | 排空模式",
        runner.ai1_batch,
        cleaner.ai_max_workers,
        cleaner.convert_max_workers,
        cleaner.ai_max_workers,
    )
    runner.start()

    idle_count = 0
    try:
        while time.perf_counter() - t_start < timeout_seconds:
            p_ai1 = repo.col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI1})
            p_conv = repo.col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_CONVERT})
            p_ai2 = repo.col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI2})
            total_pending = p_ai1 + p_conv + p_ai2
            total_done = runner.worker_ai2.total_processed

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
        runner.stop()
        runner.join()

    elapsed_s = round(time.perf_counter() - t_start, 2)
    counts = runner.processed_counts()
    logger.info(
        "🏁 [阶段化流水线排空完成] 耗时=%.2fs | AI-1=%d, 转链=%d, AI-2=%d",
        elapsed_s,
        counts["ai1_processed"],
        counts["convert_processed"],
        counts["ai2_processed"],
    )
    return {"elapsed_s": elapsed_s, **counts}


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
    print("  • 纳入监控的各线报数据源入池与清洗入库明细 :")
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
