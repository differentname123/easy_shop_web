# -*- coding: utf-8 -*-
"""
拼多多多人团互助——外部高质量源定时采集守护脚本 (`group_help_importer.py`)。
================================================================================
- 运行参数集中在下方 `IMPORTER_CONFIG`，核心采集与同步逻辑位于 `services.group_help_sync`；
- 常用示例：
    python mobile_web/scripts/group_help_importer.py --once
    python mobile_web/scripts/group_help_importer.py --interval 600 --pages 0
================================================================================
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
from typing import Any, Dict, List, Optional

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

from infrastructure.logging_utils import format_exc_summary
from infrastructure.mongo import close_shared_db
from infrastructure.runtime_io import bootstrap_script, install_shutdown_signals, interruptible_sleep
from services.group_help_sync import (
    ACDOG_API_URL,
    DEFAULT_BRIDGE_DIR,
    MIAOZHULI_API_URL,
    GroupHelpImporter,
    _DEFAULT_SYNC_INTERVAL_SECONDS,
)

# ==============================================================================
# 【运维直改配置区】：外部拼团定时采集常用参数
# ==============================================================================
IMPORTER_CONFIG: Dict[str, Any] = {
    "interval_seconds": _DEFAULT_SYNC_INTERVAL_SECONDS,  # 默认轮询间隔 600s（10分钟）
    "pages": 0,                                          # 0=全量获取，>0 限制每次最多抓取页数
    "ttl_hours": 24.0,                                   # 外部团最多在站内保留小时数
    "dry_run": False,                                    # True 时仅校验不写库
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="拼多多多人团外部高质量源定时采集守护脚本")
    parser.add_argument("--once", action="store_true", help="仅执行一轮采集后退出")
    parser.add_argument("--interval", type=int, default=int(IMPORTER_CONFIG["interval_seconds"]), help="后台轮询间隔秒数（默认 600）")
    parser.add_argument("--pages", type=int, default=int(IMPORTER_CONFIG["pages"]), help="每次每个源抓取前 N 页（0 表示全量）")
    parser.add_argument("--ttl-hours", type=float, default=float(IMPORTER_CONFIG["ttl_hours"]), help="外部团最多在站内保留小时数")
    parser.add_argument("--dry-run", action="store_true", help="仅抓取校验并打印统计，不写入数据库")
    args = parser.parse_args(argv)

    log = bootstrap_script(
        "group_help_importer",
        "group_help_importer",
        log_path=DEFAULT_BRIDGE_DIR / "importer.log",
        max_bytes=2 * 1024 * 1024,
        backup_count=3,
        configure_network=False,
    )
    importer = GroupHelpImporter(
        external_ttl_seconds=int(max(0.5, args.ttl_hours) * 3600),
        pages=args.pages,
        dry_run=bool(args.dry_run or IMPORTER_CONFIG["dry_run"]),
        logger=log,
    )

    try:
        if args.once:
            importer.run_once()
            return 0

        stop_event = threading.Event()
        install_shutdown_signals(stop_event, logger=log)
        interval = max(15, int(args.interval))
        log.info("外部多人团采集脚本已启动，轮询间隔=%ss，pages=%s (0=全量获取)", interval, importer.pages)
        while not stop_event.is_set():
            try:
                importer.run_once()
            except Exception as exc:
                log.warning("[PDD-IMPORTER] 本轮采集异常，将在下个周期重试: %s", format_exc_summary(exc))
            if interruptible_sleep(stop_event, interval):
                break
        return 0
    finally:
        close_shared_db()


if __name__ == "__main__":
    raise SystemExit(main())
