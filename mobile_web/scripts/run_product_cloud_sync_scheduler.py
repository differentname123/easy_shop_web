# -*- coding: utf-8 -*-
"""
本地商品库 -> 云端商品库 (`crawler_data.products`) 定时检查合并同步主控脚本。
================================================================================
- 运行参数集中在下方 `CLOUD_SYNC_CONFIG`，核心同步逻辑位于 `services.product_cloud_sync`；
- 常用示例：
    python mobile_web/scripts/run_product_cloud_sync_scheduler.py
    python mobile_web/scripts/run_product_cloud_sync_scheduler.py --once --dry-run
================================================================================
"""
from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import sys
import threading
from typing import Any, Dict, List, Optional

from pymongo import MongoClient

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

from infrastructure.mongo import gen_db_object
from infrastructure.runtime_io import bootstrap_script, install_shutdown_signals, interruptible_sleep
from services import product_cloud_sync as pcs

# ==============================================================================
# 【运维直改配置区】：所有云同步常用参数集中在此，改完保存即可生效
# ==============================================================================
CLOUD_SYNC_CONFIG: Dict[str, Any] = {
    "window_days": pcs.DEFAULT_SYNC_WINDOW_DAYS,               # 完整对账窗口（天）
    "interval_seconds": pcs.DEFAULT_SYNC_INTERVAL_SECONDS,     # 每轮检查间隔秒数（默认 180s）
    "batch_size": pcs.DEFAULT_BATCH_SIZE,                      # 云端 bulk_write 每批条数
    "full_sweep_every_n_cycles": pcs.FULL_SWEEP_EVERY_N_CYCLES,# 每 N 轮做一次全窗口对账
    "rolling_overlap_seconds": pcs.ROLLING_OVERLAP_SECONDS,    # 增量窗口重叠冗余秒数
    "dry_run": False,                                          # True 时仅统计差异不写云端
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地商品库 -> 云端商品库 (`crawler_data.products`) 定时检查合并同步守护脚本"
    )
    parser.add_argument(
        "--days",
        type=float,
        default=float(CLOUD_SYNC_CONFIG["window_days"]),
        help=f"检查本地最近多少天内有更新/清洗变动的商品（默认: {CLOUD_SYNC_CONFIG['window_days']} 天）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(CLOUD_SYNC_CONFIG["interval_seconds"]),
        help=f"持续定时模式下每轮检查合并间隔秒数（默认: {CLOUD_SYNC_CONFIG['interval_seconds']} 秒）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(CLOUD_SYNC_CONFIG["batch_size"]),
        help=f"云端 bulk_write 每批提交条数（默认: {CLOUD_SYNC_CONFIG['batch_size']}）",
    )
    parser.add_argument(
        "--cloud-uri",
        type=str,
        default="",
        help="可选覆盖云端 MongoDB URI（默认从 config.local.json 的 cloud_mongo_uri 读取）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行单轮完整窗口检查合并后立即退出",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：仅对比不写入云端",
    )
    args = parser.parse_args(argv)

    logger = bootstrap_script(
        "cloud_sync_scheduler",
        "product_cloud_sync",
        log_path=pcs.LOG_FILE_PATH,
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        configure_network=False,
    )

    try:
        cloud_uri, cloud_db_name, cloud_host, cloud_port = pcs.resolve_cloud_mongo_target(
            explicit_uri=args.cloud_uri or None,
        )
    except Exception as exc:
        logger.error("❌ 云端数据库配置解析失败: %s", exc)
        return 1

    eff_dry_run = bool(args.dry_run or CLOUD_SYNC_CONFIG["dry_run"])
    interval_sec = max(10, int(args.interval))
    full_every = max(1, int(CLOUD_SYNC_CONFIG["full_sweep_every_n_cycles"]))
    overlap_sec = max(0, int(CLOUD_SYNC_CONFIG["rolling_overlap_seconds"]))

    logger.info(
        "🚀 启动商品双库检查合并同步器 | 集合=[%s] | 基准窗口=[最近 %.1f 天] | 周期=[%s] | 云端目标=[%s (%s:%d/%s)]",
        pcs.COLLECTION_NAME,
        args.days,
        "单次运行(--once)" if args.once else f"每 {interval_sec} 秒 (每 {full_every} 轮全窗口对账)",
        pcs.mask_mongo_uri(cloud_uri),
        cloud_host,
        cloud_port,
        cloud_db_name,
    )

    stop_event = threading.Event()
    install_shutdown_signals(stop_event, logger=logger)

    local_db = None
    cloud_client: Optional[MongoClient] = None
    cloud_indexes_ready = False
    last_successful_round_start_utc: Optional[datetime.datetime] = None
    cycle_num = 0

    try:
        while not stop_event.is_set():
            cycle_num += 1
            round_start_utc = datetime.datetime.now(datetime.timezone.utc)
            is_full_sweep = (
                args.once
                or last_successful_round_start_utc is None
                or (cycle_num - 1) % full_every == 0
            )
            since_utc = (
                None
                if is_full_sweep
                else (last_successful_round_start_utc - datetime.timedelta(seconds=overlap_sec))
            )

            try:
                if local_db is None:
                    local_db = gen_db_object()
                    local_db.ping()
                if cloud_client is None:
                    cloud_client = MongoClient(
                        cloud_uri,
                        maxPoolSize=10,
                        connect=False,
                        tz_aware=True,
                        compressors="zlib",
                        serverSelectionTimeoutMS=pcs.DEFAULT_CONNECT_TIMEOUT_MS,
                        connectTimeoutMS=pcs.DEFAULT_CONNECT_TIMEOUT_MS,
                        socketTimeoutMS=pcs.DEFAULT_SOCKET_TIMEOUT_MS,
                    )
                    cloud_client.admin.command("ping")
                    cloud_indexes_ready = False

                local_col = local_db.get_collection(pcs.COLLECTION_NAME)
                tombstone_col = local_db.get_collection(pcs.DELETED_TOMBSTONES_COLLECTION)
                cloud_col = cloud_client[cloud_db_name][pcs.COLLECTION_NAME]

                if not cloud_indexes_ready and not eff_dry_run:
                    pcs.ensure_cloud_products_indexes(cloud_col)
                    cloud_indexes_ready = True

                report = pcs.run_sync_round(
                    local_col=local_col,
                    cloud_col=cloud_col,
                    window_days=args.days,
                    since_utc=since_utc,
                    batch_size=args.batch_size,
                    dry_run=eff_dry_run,
                    logger=logger,
                    tombstone_col=tombstone_col,
                    pull_cloud_unformatted=is_full_sweep,
                )
                report["cycle_num"] = cycle_num
                pcs.save_latest_sync_report(report)
                last_successful_round_start_utc = round_start_utc

            except Exception as exc:
                logger.warning(
                    "❌ [第 #%d 轮同步异常] 连接或读写发生异常，下轮将自动重连并回退全窗口对账 | 原因: %s",
                    cycle_num,
                    exc,
                )
                last_successful_round_start_utc = None
                if local_db is not None:
                    try:
                        local_db.close()
                    except Exception:
                        pass
                    local_db = None
                if cloud_client is not None:
                    try:
                        cloud_client.close()
                    except Exception:
                        pass
                    cloud_client = None
                    cloud_indexes_ready = False
                if args.once:
                    return 1

            if args.once or stop_event.is_set():
                break

            interruptible_sleep(stop_event, interval_sec)
    finally:
        if local_db is not None:
            try:
                local_db.close()
            except Exception:
                pass
        if cloud_client is not None:
            try:
                cloud_client.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
