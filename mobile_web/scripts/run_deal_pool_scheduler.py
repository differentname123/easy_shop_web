# -*- coding: utf-8 -*-
"""
好价线报拉取 (`deal_raw_tips`) + 流水线清洗入正式表 (`products`) 主控入口。
================================================================================
【运维说明】：
1. 运行调度参数集中在下方 `SCHEDULER_CONFIG`；
2. 监控数据源与频道策略统一声明在 `services/deal_sources/catalog.py` (`DEFAULT_CHANNEL_CONFIGS`)，
   插件注册表位于 `services/deal_sources/registry.py`；
3. 日常直接运行：`python run_deal_pool_scheduler.py`（或 `--once` / `--stats` / `--ops-report` / `--reset-db`）。
================================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any, Dict, List

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
for _path in (PROJECT_ROOT, WORKSPACE_ROOT, CURRENT_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from infrastructure.runtime_io import bootstrap_script, install_shutdown_signals, interruptible_sleep
from models.deal_constants import (
    DEAL_FORMAL_EXPIRY_HOURS,
    DEAL_POOL_LOG_PREFIXES,
    DEAL_POOL_RUNTIME_HEALTH_PATH,
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_CLEAN_WINDOW,
    DEFAULT_FORMAL_PRODUCTS_COLLECTION,
    DEFAULT_RAW_COLLECTION_NAME,
    LOG_DIR,
    LOG_FILE_PATH,
)
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from services.deal_pipeline import (
    DealPipelineRunner,
    print_latest_ops_report,
    print_stats_report,
    run_clean_stage,
    run_fetch_stage,
    run_single_cycle,
    run_stage_pipeline_drain,
)
from services.deal_pool_cleaner import DealPoolCleaner
from services.deal_sources import (
    DEFAULT_CHANNEL_CONFIGS,
    DealChannelConfig,
    get_active_channels,
    is_channel_due,
)

# ==============================================================================
# 【运维直改配置区 1】：全局运行参数（直接改这里保存并运行 python run_deal_pool_scheduler.py）
# ==============================================================================
SCHEDULER_CONFIG: Dict[str, Any] = {
    # 运行模式："daemon" (默认常驻并行守护) | "once" (拉取+清洗单轮退出) | "clean_only" (仅清洗) | "stats" | "ops_report"
    "run_mode": "daemon",
    "reset_db_on_start": False,              # True 时启动前清空初步表与相关日志（谨慎开启）

    # 调度节拍与清洗吞吐控制
    "fetch_tick_seconds": 15,                # 调度心跳检测周期（秒），支持各频道独立配置任意秒数间隔
    "clean_limit": DEFAULT_CLEAN_WINDOW,     # 单批清洗最大抽取条数（默认 200）
    "ai_batch_size": 10,                     # AI-1 拆解打包条数（默认 10 条/批）
    "ai2_batch_size": 10,                    # AI-2 终审打包条数（默认 10 条/批）
    "ai_workers": 10,                        # AI 并发线程数
    "convert_workers": 20,                   # 官方转链并发线程数
    "ai_timeout": DEFAULT_AI_TIMEOUT_SECONDS,

    # 运行时限与集合
    "duration_hours": 0.0,                   # 限定运行时长（0.0 表示持续运行）
    "max_cycles": 0,                         # 最大拉取轮次（0 表示不限轮次）
    "serial": False,                         # False: 并行流水线常驻; True: 串行单轮交替
    "collection": DEFAULT_RAW_COLLECTION_NAME,
    "formal_collection": DEFAULT_FORMAL_PRODUCTS_COLLECTION,
}

# ==============================================================================
# 【监控频道表】：唯一真源已收敛至 services/deal_sources/catalog.py (DEFAULT_CHANNEL_CONFIGS)
# ==============================================================================
MONITORED_CHANNELS: List[DealChannelConfig] = list(DEFAULT_CHANNEL_CONFIGS)


def _reset_raw_pool_and_logs(repo: DealRawPoolRepository) -> None:
    try:
        for fname in os.listdir(LOG_DIR):
            fpath = os.path.join(LOG_DIR, fname)
            if not os.path.isfile(fpath) or not any(fname.startswith(p) for p in DEAL_POOL_LOG_PREFIXES):
                continue
            if fpath == LOG_FILE_PATH:
                with open(fpath, "w", encoding="utf-8") as lf:
                    lf.write("")
            else:
                try:
                    os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass
    if os.path.isfile(DEAL_POOL_RUNTIME_HEALTH_PATH):
        try:
            os.remove(DEAL_POOL_RUNTIME_HEALTH_PATH)
        except Exception:
            pass
    repo.reset_raw_collection()


def _compute_next_wake_seconds(
    channels: List[DealChannelConfig],
    last_run_map: Dict[str, float],
    site_cooldown_until: Dict[str, float],
    now_ts: float,
    max_tick: int,
) -> int:
    """计算距离下一个最早到期频道的等待秒数，保证精确按各频道间隔唤醒。"""
    if not channels:
        return max(5, max_tick)
    min_wait = float(max(5, max_tick))
    for ch in channels:
        last_t = last_run_map.get(ch.name, 0.0)
        due_at = (last_t + max(10, int(ch.interval_seconds))) if last_t > 0 else now_ts
        cool_until = max(
            site_cooldown_until.get(ch.site_key, 0.0),
            site_cooldown_until.get(ch.name, 0.0),
        )
        wait_s = max(0.0, max(due_at, cool_until) - now_ts)
        if wait_s < min_wait:
            min_wait = wait_s
    return max(1, int(round(min_wait)))


def _run_scheduler_body(
    args: argparse.Namespace,
    cfg: Dict[str, Any],
    mode: str,
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    logger: Any,
) -> None:
    if args.reset_db or bool(cfg.get("reset_db_on_start")):
        _reset_raw_pool_and_logs(repo)

    if mode == "stats":
        print_stats_report(repo)
        return

    if mode == "ops_report":
        print_latest_ops_report()
        return

    repo.migrate_pending_to_stage_machine()

    init_expired = repo.expire_stale_formal_deals()
    if init_expired > 0:
        logger.info("🧹 [启动好价保鲜清扫] 自动摘除超 %.0f 小时未刷新好价标 %d 个", DEAL_FORMAL_EXPIRY_HOURS, init_expired)

    recovered = repo.recover_recent_misclassified_tips(lookback_hours=48.0)
    if recovered["jd_recovered"] or recovered["tb_recovered"]:
        logger.info(
            "♻️ [启动误杀线报幂等救援] 已释放近 48h 误杀线报回 pending: 京东短链/联盟链=%d 条, 淘口令误路由=%d 条",
            recovered["jd_recovered"],
            recovered["tb_recovered"],
        )

    clean_limit = int(cfg["clean_limit"])
    if mode == "clean_only":
        if cfg["serial"]:
            run_clean_stage(repo=repo, cleaner=cleaner, cycle_num=1, clean_limit=clean_limit)
        else:
            run_stage_pipeline_drain(repo=repo, cleaner=cleaner, clean_limit=clean_limit)
        print_stats_report(repo)
        return

    stop_event = threading.Event()
    new_fetch_event = threading.Event()
    install_shutdown_signals(stop_event, logger=logger, on_signal=lambda _: new_fetch_event.set())

    channel_health_tracker: Dict[str, Dict[str, Any]] = {}
    channel_last_run: Dict[str, float] = {}
    site_cooldown_until: Dict[str, float] = {}

    effective_channels = list(MONITORED_CHANNELS)
    enabled_channels = get_active_channels(effective_channels)
    active_cnt = len(enabled_channels)
    disabled_cnt = len(effective_channels) - active_cnt

    if mode == "once":
        run_fetch_stage(
            repo=repo,
            session=None,
            cycle_num=1,
            channel_health_tracker=channel_health_tracker,
            channels=effective_channels,
            channel_last_run=channel_last_run,
            site_cooldown_until=site_cooldown_until,
        )
        if cfg["serial"]:
            run_clean_stage(repo=repo, cleaner=cleaner, cycle_num=1, clean_limit=clean_limit)
        else:
            run_stage_pipeline_drain(repo=repo, cleaner=cleaner, clean_limit=clean_limit)
        print_stats_report(repo)
        return

    start_wall = time.time()
    duration_hours = float(cfg.get("duration_hours") or 0.0)
    max_cycles = int(cfg.get("max_cycles") or 0)
    fetch_tick_seconds = max(5, int(cfg.get("fetch_tick_seconds") or 15))
    deadline_wall = (start_wall + duration_hours * 3600.0) if duration_hours > 0 else None

    if cfg["serial"]:
        logger.info(
            "🚀 好价线报【串行模式】已启动 | 初步表=[%s] -> 正式表=[%s] | 监控频道: %d启用/%d停用 | 心跳=%ds",
            cfg["collection"],
            cfg["formal_collection"],
            active_cnt,
            disabled_cnt,
            fetch_tick_seconds,
        )
        cycle_num = 0
        while not stop_event.is_set():
            now_wall = time.time()
            has_due = any(
                is_channel_due(ch, channel_last_run.get(ch.name, 0.0), now_wall)
                and max(site_cooldown_until.get(ch.site_key, 0.0), site_cooldown_until.get(ch.name, 0.0)) <= now_wall
                for ch in enabled_channels
            )
            if has_due:
                cycle_num += 1
                try:
                    run_single_cycle(
                        repo=repo,
                        cleaner=cleaner,
                        session=None,
                        cycle_num=cycle_num,
                        clean_limit=clean_limit,
                        channel_health_tracker=channel_health_tracker,
                        channels=effective_channels,
                        channel_last_run=channel_last_run,
                        site_cooldown_until=site_cooldown_until,
                    )
                except Exception as cycle_exc:
                    logger.exception("🚨 第 #%d 轮执行异常: %s", cycle_num, cycle_exc)

            if (max_cycles > 0 and cycle_num >= max_cycles) or (
                deadline_wall is not None and time.time() >= deadline_wall
            ):
                break

            sleep_sec = _compute_next_wake_seconds(
                enabled_channels, channel_last_run, site_cooldown_until, time.time(), fetch_tick_seconds
            )
            wait_cap = min(float(sleep_sec), max(0.0, deadline_wall - time.time())) if deadline_wall else float(sleep_sec)
            if interruptible_sleep(stop_event, wait_cap):
                break
        print_stats_report(repo)
        return

    ch_summary = ", ".join(f"{c.name}({c.interval_seconds}s)" for c in enabled_channels) or "无"
    logger.info(
        "🚀 好价线报【流水线并行守护】已启动 | 初步表=[%s] -> 正式表=[%s] | 启用频道(%d): %s | AI批次=%d条/批",
        cfg["collection"],
        cfg["formal_collection"],
        active_cnt,
        ch_summary,
        cleaner.ai_batch_size,
    )

    runner = DealPipelineRunner(
        repo=repo,
        cleaner=cleaner,
        clean_limit=clean_limit,
        stop_event=stop_event,
        ai1_upstream_event=new_fetch_event,
        ai1_poll_interval=2.0,
        convert_poll_interval=1.5,
        ai2_poll_interval=2.0,
        maint_poll_interval=30.0,
    )

    def _producer_fetch_worker() -> None:
        fetch_cycle = 0
        while not stop_event.is_set():
            now_wall = time.time()
            due_now = [
                ch
                for ch in enabled_channels
                if is_channel_due(ch, channel_last_run.get(ch.name, 0.0), now_wall)
                and max(site_cooldown_until.get(ch.site_key, 0.0), site_cooldown_until.get(ch.name, 0.0)) <= now_wall
            ]
            if due_now:
                fetch_cycle += 1
                try:
                    fetch_res = run_fetch_stage(
                        repo=repo,
                        session=None,
                        cycle_num=fetch_cycle,
                        channel_health_tracker=channel_health_tracker,
                        channels=effective_channels,
                        channel_last_run=channel_last_run,
                        site_cooldown_until=site_cooldown_until,
                        active_index=runner.active_index,
                    )
                    if fetch_res.get("total_inserted", 0) > 0:
                        new_fetch_event.set()
                except Exception as fetch_exc:
                    logger.exception("🚨 [拉取线程/Producer] 第 #%d 轮拉取异常: %s", fetch_cycle, fetch_exc)

            if (max_cycles > 0 and fetch_cycle >= max_cycles) or (
                deadline_wall is not None and time.time() >= deadline_wall
            ):
                stop_event.set()
                new_fetch_event.set()
                break

            sleep_sec = _compute_next_wake_seconds(
                enabled_channels, channel_last_run, site_cooldown_until, time.time(), fetch_tick_seconds
            )
            wait_cap = min(float(sleep_sec), max(0.0, deadline_wall - time.time())) if deadline_wall else float(sleep_sec)
            if interruptible_sleep(stop_event, wait_cap):
                new_fetch_event.set()
                break

    producer_thread = threading.Thread(target=_producer_fetch_worker, name="DealProducerFetch", daemon=True)
    producer_thread.start()
    runner.start()

    try:
        while (
            producer_thread.is_alive()
            or runner.worker_ai1.is_alive()
            or runner.worker_convert.is_alive()
            or runner.worker_ai2.is_alive()
        ):
            producer_thread.join(timeout=1.0)
            runner.join(timeout_per_worker=0.5)
            if stop_event.is_set():
                runner.stop()
                new_fetch_event.set()
                break
    except KeyboardInterrupt:
        stop_event.set()
        runner.stop()
        new_fetch_event.set()

    print_stats_report(repo)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="好价线报拉取与清洗主控守护脚本（参数集中配置在文件顶部 SCHEDULER_CONFIG 与 MONITORED_CHANNELS）"
    )
    parser.add_argument("--once", action="store_true", help="临时执行单轮拉取+清洗后退出")
    parser.add_argument("--clean-only", action="store_true", help="临时仅执行待清洗队列排空")
    parser.add_argument("--stats", action="store_true", help="仅打印初步表与正式表统计")
    parser.add_argument("--ops-report", action="store_true", help="仅打印最近一轮运维报告")
    parser.add_argument("--reset-db", action="store_true", help="启动前清空初步表与统计日志")
    args = parser.parse_args()

    logger = bootstrap_script(
        "deal_scheduler",
        "deal_pool_scheduler",
        log_path=LOG_FILE_PATH,
        max_bytes=15 * 1024 * 1024,
        backup_count=3,
    )

    cfg = dict(SCHEDULER_CONFIG)
    mode = str(cfg.get("run_mode") or "daemon").strip().lower()
    if args.stats:
        mode = "stats"
    elif args.ops_report:
        mode = "ops_report"
    elif args.clean_only:
        mode = "clean_only"
    elif args.once or int(cfg.get("max_cycles") or 0) == 1:
        mode = "once"

    repo = DealRawPoolRepository(
        collection_name=str(cfg["collection"]),
        formal_collection_name=str(cfg["formal_collection"]),
    )
    cleaner = DealPoolCleaner(
        ai_batch_size=int(cfg["ai_batch_size"]),
        ai2_batch_size=int(cfg.get("ai2_batch_size", 10)),
        ai_max_workers=int(cfg["ai_workers"]),
        convert_max_workers=int(cfg["convert_workers"]),
        ai_timeout=float(cfg["ai_timeout"]),
    )
    try:
        _run_scheduler_body(args, cfg, mode, repo, cleaner, logger)
    finally:
        cleaner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
