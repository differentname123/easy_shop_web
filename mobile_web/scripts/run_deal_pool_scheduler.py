# -*- coding: utf-8 -*-
"""
好价线报拉取 (`deal_raw_tips`) + 流水线清洗入正式表 (`products`) 主控入口。
================================================================================
【运维直改配置说明】：
1. 直接修改本文件下方的 `SCHEDULER_CONFIG` 与 `MONITORED_CHANNELS` 即可控制：
   - 监控哪些站点/频道 (`enabled = True / False`)
   - 每个频道的拉取间隔 (`interval_seconds`)、单轮最多抓多少新详情 (`max_details`)、
     请求节奏 (`detail_delay_seconds`) 与风控冷却时间 (`rate_limit_cooldown_seconds`)
   - 后续需要扩充新频道或新站点时，直接在 `MONITORED_CHANNELS` 追加一条 `DealChannelConfig` 即可
2. 日常直接运行：`python run_deal_pool_scheduler.py`（无需记忆复杂的命令行配置参数）。
================================================================================
"""
import argparse
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List

import requests

# 确保 Windows 终端下正常输出中文与 Emoji
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
for _path in (PROJECT_ROOT, WORKSPACE_ROOT, CURRENT_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from infrastructure.ai_api.model_api import generate_content
from infrastructure.api_governance import set_process_role

# 1. 导出全局常量与状态机标识（保持兼容）
from models.deal_constants import (
    CLEAN_FAILED_SAMPLES_JSONL,
    CLEAN_OPS_HISTORY_JSONL,
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    CLEAN_SUCCESS_PREVIEW_JSONL,
    CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEAL_INFO_PROTECTION_HOURS,
    DEAL_MAX_CLEAN_AGE_DAYS,
    DEAL_RAW_TTL_DAYS,
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_CLEAN_INTERVAL_SECONDS,
    DEFAULT_CLEAN_WINDOW,
    DEFAULT_FORMAL_PRODUCTS_COLLECTION,
    DEFAULT_RAW_COLLECTION_NAME,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
    DEFAULT_STAGE_POLL_INTERVAL_SECONDS,
    ENGINEERING_PHILOSOPHY_MANIFEST,
    ERROR_LOG_JSONL_PATH,
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_ADAPTER_OFF_PROMOTION,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI2_MAIN_DEAD_ONLY_FILLER,
    ERR_AI2_PRODUCT_MISMATCH,
    ERR_AI2_VIRTUAL_OR_JUNK,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_AI_TIMEOUT_RETRY,
    ERR_EMPTY_PRODUCT_IDENTITY,
    ERR_EXPIRED_OVER_7_DAYS,
    ERR_JD_NUMERIC_SKU,
    ERR_LOW_QUALITY_SCORE,
    ERR_MAIN_PRODUCT_DEAD,
    ERR_MERGED_INTO_NEIGHBOR,
    ERR_NO_ANCHOR,
    ERR_PDD_NUMERIC_ID,
    ERR_TB_NUMERIC_ID,
    ERR_UNSUPPORTED_PLATFORM,
    EXCLUDED_SITES_AND_CHANNELS_MANIFEST,
    LATEST_CLEAN_OPS_REPORT_PATH,
    LATEST_OPS_REPORT_PATH,
    LEGACY_SHADOW_TEST_COLLECTION,
    LOCAL_FALLBACK_JSONL,
    LOG_DIR,
    LOG_FILE_PATH,
    MAX_ANCHOR_CACHE_SIZE,
    MAX_CLEAN_RETRIES,
    MAX_JSONL_FILE_BYTES,
    OK_SKIPPED_ALREADY_CLEANED,
    OPS_REPORT_HISTORY_JSONL,
    PROMPT_DIR,
    PROMPT_STAGE1_PATH,
    PROMPT_STAGE2_PATH,
    QUALIFIED_DEAL_MIN_SCORE,
    SOURCE_REPUTATION_BONUS,
    STATS_SNAPSHOT_PATH,
    _UNSUPPORTED_EXTERNAL_DOMAINS,
)

# 2. 导出仓储层与信号检测核心
from repositories.deal_raw_pool_repo import (
    DealRawPoolRepository,
    _SIGNAL_SKU_KEYWORDS,
    _SIGNAL_TOKEN_SYMBOLS,
    _SIGNAL_URL_OR_DOMAIN_KEYWORDS,
    _append_bounded_jsonl_lines,
    _is_pure_jd_coupon_only_record,
    append_error_records_jsonl,
    evaluate_quality_signals,
    has_potential_product_signal,
    parse_publish_time_epoch,
)

# 3. 导出频道配置模型
from services.deal_channel_config import (
    DEFAULT_CHANNEL_CONFIGS,
    DealChannelConfig,
    get_active_channels,
    is_channel_due,
)

# 4. 导出统一线报抓取层
from services.deal_fetchers import (
    fetch_guangdiu_deals,
    fetch_tehuicaicai_deals,
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
    is_record_confirmed_unsupported,
)

# 5. 导出清洗引擎与阶段流水线
from services.deal_pool_cleaner import (
    DealPoolCleaner,
    MaintenanceWorker,
    StageWorker,
    _commit_batch_results_to_db,
    _is_safe_public_http_url,
    _is_verified_station_promo_link,
    detect_candidate_platform,
    is_direct_coupon_or_venue_url,
    rebuild_clean_official_venue_url,
    unwrap_to_official_public_venue_url,
    upsert_cleaned_products_to_formal_table,
)

# 6. 导出调度管道与运维报告
from services.deal_pipeline import (
    OPS_TROUBLESHOOTING_PLAYBOOK,
    SITE_DISPLAY_NAMES,
    build_and_log_cycle_ops_report,
    get_ops_troubleshooting_advice,
    print_latest_ops_report,
    print_stats_report,
    run_clean_stage,
    run_fetch_stage,
    run_single_cycle,
    run_stage_pipeline_drain,
)

# 7. 导出备用抓取器（供后续按需手动新增频道时直接配置 fetcher_name）
from mobile_web.scripts.test_deal_sites_scraper import (
    fetch_0818tuan_deals,
    fetch_bacaoo_deals,
    fetch_gwdang_discounts,
    fetch_haodanku_weal_list,
    fetch_haodanku_wire_report,
    fetch_iehou_deals,
    fetch_xianbao_fun_deals,
    fetch_xuandan_deals,
    fetch_zhetaoke_rank_list,
    fetch_zhetaoke_xianbao_groups,
)


def setup_scheduler_logger() -> logging.Logger:
    """配置控制台与文件滚动日志器。"""
    sched_logger = logging.getLogger("deal_pool_scheduler")
    if sched_logger.handlers:
        return sched_logger
    sched_logger.setLevel(logging.INFO)
    sched_logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    sched_logger.addHandler(console_handler)

    if os.environ.get("WEB_LOG_DIR", "").strip() != "-":
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=15 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        sched_logger.addHandler(file_handler)
    return sched_logger


logger = setup_scheduler_logger()


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
# 【运维直改配置区 2】：监控频道与单频道速度调控表（唯一真源）
# ------------------------------------------------------------------------------
# 每个频道独立掌控以下参数，按需直接修改或新增即可：
# - enabled                    : True 开启监控 / False 停用
# - interval_seconds           : 多久拉取一次（秒），如 120 / 180 / 360
# - max_details                : 单轮最多抓多少条新商品详情（防触发目标站验证码）
# - detail_delay_seconds       : 单条详情请求之间的休眠间隔（秒）
# - sleep_after                : 本频道抓完后休眠多久（秒）
# - rate_limit_cooldown_seconds: 遇到 429/滑块验证码后，该站点自动冷却休息多少秒
# ==============================================================================
MONITORED_CHANNELS: List[DealChannelConfig] = [
    # --------------------------------------------------------------------------
    # 1. 特惠采采（https://xhhcds.cn/）：人工精选 JSON 接口，逐页拉取直到整页均已抓过
    # --------------------------------------------------------------------------
    DealChannelConfig(
        name="特惠采采_首页精选",
        site_key="tehuicaicai",
        default_url="https://xhhcds.cn/api/v1/public/home-products/?feed=home&page=1&page_size=50",
        fetcher_name="fetch_tehuicaicai_deals",
        fetcher_kwargs={
            "channel": "home",
            "page": 1,
            "page_size": 50,
            "max_pages": 10,
            "auto_paginate": True,
            "fetch_detail": True,
        },
        enabled=True,
        interval_seconds=180,
        max_details=50,
        detail_delay_seconds=0.05,
        sleep_after=0.3,
        rate_limit_cooldown_seconds=180,
        description="特惠采采专员精选好价流，JSON 无风控，直达短链与文案完整",
    ),

    # --------------------------------------------------------------------------
    # 2. 逛丢（https://guangdiu.com/）：首页、热门榜、小时风云榜
    # --------------------------------------------------------------------------
    DealChannelConfig(
        name="逛丢_首页实时流",
        site_key="guangdiu",
        default_url="https://guangdiu.com/",
        fetcher_name="fetch_guangdiu_deals",
        fetcher_kwargs={"channel": "latest", "page": 1, "fetch_detail": True},
        enabled=True,
        interval_seconds=180,
        max_details=3,
        detail_delay_seconds=1.5,
        sleep_after=1.5,
        rate_limit_cooldown_seconds=300,
        description="逛丢首页实时好价（同站三榜每轮随机顺序轮转，单频道限额 3 条以均摊每 5 分钟 ~6 条 IP 配额）",
    ),
    DealChannelConfig(
        name="逛丢_热门榜",
        site_key="guangdiu",
        default_url="https://guangdiu.com/hots.php",
        fetcher_name="fetch_guangdiu_deals",
        fetcher_kwargs={"channel": "hots", "page": 1, "fetch_detail": True},
        enabled=True,
        interval_seconds=360,
        max_details=3,
        detail_delay_seconds=1.5,
        sleep_after=1.5,
        rate_limit_cooldown_seconds=300,
        description="逛丢热门上榜流",
    ),
    DealChannelConfig(
        name="逛丢_小时风云榜",
        site_key="guangdiu",
        default_url="https://guangdiu.com/rank.php",
        fetcher_name="fetch_guangdiu_deals",
        fetcher_kwargs={"channel": "rank", "page": 1, "fetch_detail": True},
        enabled=True,
        interval_seconds=360,
        max_details=3,
        detail_delay_seconds=1.5,
        sleep_after=1.5,
        rate_limit_cooldown_seconds=300,
        description="逛丢小时风云榜",
    ),

    # --------------------------------------------------------------------------
    # 【扩展模板】：后续如需新增频道，复制下方模板并按抓取器类型二选一配置：
    # - 调度说明：不同 site_key 自动并行拉取；同一 site_key 内部每轮随机排序轮转，防首个频道饿死同站其他榜
    # - 类型 A（含独立详情页抓取）：fetcher_kwargs 设 "fetch_detail": True，函数签名需接收 session / skip_detail_ids / max_details
    # - 类型 B（纯列表/API 一次出全量）：不要传 "fetch_detail": True，函数仅需接收 session 及业务参数，由管道自动增量去重
    # --------------------------------------------------------------------------
    # # 类型 A 示例（线报酷）：
    # DealChannelConfig(
    #     name="线报酷_好单拼多多专属流",
    #     site_key="xianbao_fun",
    #     default_url="https://new.xianbao.fun/category-haodan-pdd/",
    #     fetcher_name="fetch_xianbao_fun_deals",
    #     fetcher_kwargs={"channel": "category_haodan_pdd", "page": 1, "fetch_detail": True},
    #     enabled=False,
    #     interval_seconds=240,
    #     max_details=15,
    #     sleep_after=0.5,
    # ),
    # # 类型 B 示例（好单库/折淘客/选单等纯列表源）：
    # DealChannelConfig(
    #     name="好单库_实时线报",
    #     site_key="haodanku",
    #     default_url="http://v2.api.haodanku.com/get_wire_report",
    #     fetcher_name="fetch_haodanku_wire_report",
    #     fetcher_kwargs={"min_id": 1},
    #     enabled=False,
    #     interval_seconds=180,
    #     sleep_after=0.3,
    # ),
]


def _reset_raw_pool_and_logs(repo: DealRawPoolRepository) -> None:
    try:
        for fname in os.listdir(LOG_DIR):
            fpath = os.path.join(LOG_DIR, fname)
            if not os.path.isfile(fpath):
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
        cool_until = site_cooldown_until.get(ch.site_key, 0.0)
        target_at = max(due_at, cool_until)
        wait_s = max(0.0, target_at - now_ts)
        if wait_s < min_wait:
            min_wait = wait_s
    return max(1, int(round(min_wait)))


def main():
    parser = argparse.ArgumentParser(
        description="好价线报拉取与清洗主控守护脚本（参数集中配置在文件顶部 SCHEDULER_CONFIG 与 MONITORED_CHANNELS）"
    )
    parser.add_argument("--once", action="store_true", help="临时执行单轮拉取+清洗后退出")
    parser.add_argument("--clean-only", action="store_true", help="临时仅执行待清洗队列排空")
    parser.add_argument("--stats", action="store_true", help="仅打印初步表与正式表统计")
    parser.add_argument("--ops-report", action="store_true", help="仅打印最近一轮运维报告")
    parser.add_argument("--reset-db", action="store_true", help="启动前清空初步表与统计日志")
    args = parser.parse_args()

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

    set_process_role("deal_scheduler")
    try:
        from infrastructure.pdd_client import configure_transport
        from infrastructure.settings import get_settings as _get_app_settings
        _st = _get_app_settings()
        configure_transport(dns_mode=_st.pdd_dns_mode, trust_env=_st.pdd_trust_env)
    except Exception:
        pass

    repo = DealRawPoolRepository(
        collection_name=str(cfg["collection"]),
        formal_collection_name=str(cfg["formal_collection"]),
    )
    cleaner = DealPoolCleaner(
        use_ai_for_multi=True,
        ai_batch_size=int(cfg["ai_batch_size"]),
        ai2_batch_size=int(cfg.get("ai2_batch_size", 10)),
        ai_max_workers=int(cfg["ai_workers"]),
        convert_max_workers=int(cfg["convert_workers"]),
        ai_timeout=float(cfg["ai_timeout"]),
    )

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

    stop_requested = False
    new_fetch_event = threading.Event()

    def _handle_sig(signum, frame):
        nonlocal stop_requested
        logger.info("收到停止信号 (%s)，正在通知各工作线程优雅退出...", signum)
        stop_requested = True
        new_fetch_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    channel_health_tracker: Dict[str, Dict[str, Any]] = {}
    channel_last_run: Dict[str, float] = {}
    site_cooldown_until: Dict[str, float] = {}

    effective_channels = list(MONITORED_CHANNELS)
    enabled_channels = get_active_channels(effective_channels)
    active_cnt = len(enabled_channels)
    disabled_cnt = len(effective_channels) - active_cnt

    if mode == "once":
        with requests.Session() as session:
            run_fetch_stage(
                repo=repo,
                session=session,
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
        while not stop_requested:
            now_wall = time.time()
            has_due = any(
                is_channel_due(ch, channel_last_run.get(ch.name, 0.0), now_wall)
                and site_cooldown_until.get(ch.site_key, 0.0) <= now_wall
                for ch in enabled_channels
            )
            if has_due:
                cycle_num += 1
                try:
                    with requests.Session() as session:
                        run_single_cycle(
                            repo=repo,
                            cleaner=cleaner,
                            session=session,
                            cycle_num=cycle_num,
                            clean_limit=clean_limit,
                            channel_health_tracker=channel_health_tracker,
                            channels=effective_channels,
                            channel_last_run=channel_last_run,
                            site_cooldown_until=site_cooldown_until,
                        )
                except Exception as cycle_exc:
                    logger.exception("🚨 第 #%d 轮执行异常: %s", cycle_num, cycle_exc)

            if max_cycles > 0 and cycle_num >= max_cycles:
                break
            if deadline_wall is not None and time.time() >= deadline_wall:
                break

            sleep_sec = _compute_next_wake_seconds(
                enabled_channels, channel_last_run, site_cooldown_until, time.time(), fetch_tick_seconds
            )
            for _ in range(sleep_sec):
                if stop_requested or (deadline_wall is not None and time.time() >= deadline_wall):
                    break
                time.sleep(1)
        print_stats_report(repo)
        return

    # 默认并行流水线常驻守护模式
    ch_summary = ", ".join(f"{c.name}({c.interval_seconds}s)" for c in enabled_channels) or "无"
    logger.info(
        "🚀 好价线报【流水线并行守护】已启动 | 初步表=[%s] -> 正式表=[%s] | 启用频道(%d): %s | AI批次=%d条/批",
        cfg["collection"],
        cfg["formal_collection"],
        active_cnt,
        ch_summary,
        cleaner.ai_batch_size,
    )

    cleaner.warmup_anchor_cache_from_db(repo.col)
    shared_active_products = cleaner._load_active_formal_products(repo.formal_col)
    shared_lock = threading.Lock()
    ai_semaphore = threading.BoundedSemaphore(cleaner.ai_max_workers)

    ai1_done_event = threading.Event()
    convert_done_event = threading.Event()

    def _producer_fetch_worker() -> None:
        nonlocal stop_requested
        fetch_cycle = 0
        while not stop_requested:
            now_wall = time.time()
            due_now = [
                ch
                for ch in enabled_channels
                if is_channel_due(ch, channel_last_run.get(ch.name, 0.0), now_wall)
                and site_cooldown_until.get(ch.site_key, 0.0) <= now_wall
            ]
            if due_now:
                fetch_cycle += 1
                try:
                    with requests.Session() as session:
                        fetch_res = run_fetch_stage(
                            repo=repo,
                            session=session,
                            cycle_num=fetch_cycle,
                            channel_health_tracker=channel_health_tracker,
                            channels=effective_channels,
                            channel_last_run=channel_last_run,
                            site_cooldown_until=site_cooldown_until,
                        )
                        if fetch_res.get("total_inserted", 0) > 0:
                            new_fetch_event.set()
                except Exception as fetch_exc:
                    logger.exception("🚨 [拉取线程/Producer] 第 #%d 轮拉取异常: %s", fetch_cycle, fetch_exc)

            if max_cycles > 0 and fetch_cycle >= max_cycles:
                stop_requested = True
                new_fetch_event.set()
                break

            if deadline_wall is not None and time.time() >= deadline_wall:
                stop_requested = True
                new_fetch_event.set()
                break

            sleep_sec = _compute_next_wake_seconds(
                enabled_channels, channel_last_run, site_cooldown_until, time.time(), fetch_tick_seconds
            )
            for _ in range(sleep_sec):
                if stop_requested or (deadline_wall is not None and time.time() >= deadline_wall):
                    stop_requested = True
                    new_fetch_event.set()
                    break
                time.sleep(1)

    ai1_batch = max(64, cleaner.ai_batch_size * cleaner.ai_max_workers * 2)
    if clean_limit > 0:
        ai1_batch = max(cleaner.ai_batch_size, clean_limit)

    worker_ai1 = StageWorker(
        name="StageWorker-AI1",
        stage=CLEAN_STAGE_NEED_AI1,
        repo=repo,
        cleaner=cleaner,
        batch_size=ai1_batch,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
        poll_interval=2.0,
        stop_event=threading.Event(),
        ai_semaphore=ai_semaphore,
        upstream_event=new_fetch_event,
        downstream_event=ai1_done_event,
        shared_active_products=shared_active_products,
        shared_lock=shared_lock,
    )
    worker_convert = StageWorker(
        name="StageWorker-Convert",
        stage=CLEAN_STAGE_NEED_CONVERT,
        repo=repo,
        cleaner=cleaner,
        batch_size=200,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
        poll_interval=1.5,
        stop_event=threading.Event(),
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
        batch_size=100,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,
        poll_interval=2.0,
        stop_event=threading.Event(),
        ai_semaphore=ai_semaphore,
        upstream_event=convert_done_event,
        downstream_event=None,
        shared_active_products=shared_active_products,
        shared_lock=shared_lock,
    )
    worker_maint = MaintenanceWorker(repo=repo, poll_interval=30.0, stop_event=threading.Event())
    producer_thread = threading.Thread(target=_producer_fetch_worker, name="DealProducerFetch", daemon=True)

    producer_thread.start()
    worker_ai1.start()
    worker_convert.start()
    worker_ai2.start()
    worker_maint.start()

    try:
        while producer_thread.is_alive() or worker_ai1.is_alive() or worker_convert.is_alive() or worker_ai2.is_alive():
            producer_thread.join(timeout=1.0)
            worker_ai1.join(timeout=0.5)
            worker_convert.join(timeout=0.5)
            worker_ai2.join(timeout=0.5)
            if stop_requested:
                worker_ai1.stop_event.set()
                worker_convert.stop_event.set()
                worker_ai2.stop_event.set()
                worker_maint.stop_event.set()
                ai1_done_event.set()
                convert_done_event.set()
                new_fetch_event.set()
                break
    except KeyboardInterrupt:
        stop_requested = True
        worker_ai1.stop_event.set()
        worker_convert.stop_event.set()
        worker_ai2.stop_event.set()
        worker_maint.stop_event.set()
        ai1_done_event.set()
        convert_done_event.set()
        new_fetch_event.set()

    print_stats_report(repo)


if __name__ == "__main__":
    main()
