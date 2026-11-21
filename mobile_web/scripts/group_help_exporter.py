# -*- coding: utf-8 -*-
"""
拼多多多人团互助——本平台海报外发桥接脚本 (`group_help_exporter.py`)。
================================================================================
- 常用运行参数集中在下方 `EXPORTER_CONFIG`，核心打包与远端同步逻辑位于 `services.group_help_sync`；
- 常用示例：
    python mobile_web/scripts/group_help_exporter.py --once
    python mobile_web/scripts/group_help_exporter.py --interval 15
    python mobile_web/scripts/group_help_exporter.py --list
    python mobile_web/scripts/group_help_exporter.py --mark-done <group_order_id>
================================================================================
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
from typing import Any, Dict, List, Optional

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

from infrastructure.runtime_io import bootstrap_script, install_shutdown_signals, interruptible_sleep
from services.group_help_sync import DEFAULT_BRIDGE_DIR, GroupHelpExporter, MiaozhuliPublisher

# ==============================================================================
# 【运维直改配置区】：海报外发桥接常用参数
# ==============================================================================
EXPORTER_CONFIG: Dict[str, Any] = {
    "interval_seconds": 15,                 # 后台轮询间隔秒数
    "limit": 30,                            # 单轮最多导出的在架海报数量
    "enable_remote_sync": True,             # 是否自动向秒助力同步口令拼团
    "bridge_dir": str(DEFAULT_BRIDGE_DIR),  # 外发工作目录
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="拼多多多人团本平台海报外发桥接脚本")
    parser.add_argument("--once", action="store_true", help="仅执行一轮队列打包同步后退出")
    parser.add_argument("--interval", type=int, default=int(EXPORTER_CONFIG["interval_seconds"]), help="后台轮询间隔秒数")
    parser.add_argument("--limit", type=int, default=int(EXPORTER_CONFIG["limit"]), help="单轮最多导出的在架海报数量")
    parser.add_argument("--bridge-dir", type=str, default="", help="自定义外发桥接目录")
    parser.add_argument("--list", action="store_true", help="查看当前外发队列统计并退出")
    parser.add_argument("--mark-done", type=str, default="", help="将指定团单号标记为已外发完成")
    parser.add_argument("--mark-failed", type=str, default="", help="将指定团单号标记为外发失败")
    parser.add_argument("--reason", type=str, default="", help="配合 --mark-failed 记录失败原因")
    parser.add_argument("--no-remote", action="store_true", help="禁用向秒助力等外部小站自动网络同步，仅保留本地海报文件队列")
    args = parser.parse_args(argv)

    bridge_path = Path(args.bridge_dir) if args.bridge_dir else Path(EXPORTER_CONFIG["bridge_dir"])
    log = bootstrap_script(
        "group_help_exporter",
        "group_help_exporter",
        log_path=bridge_path / "exporter.log",
        max_bytes=2 * 1024 * 1024,
        backup_count=3,
        configure_network=False,
    )
    exporter = GroupHelpExporter(
        bridge_dir=bridge_path,
        limit=args.limit,
        enable_remote_sync=bool(EXPORTER_CONFIG["enable_remote_sync"] and not args.no_remote),
        logger=log,
    )
    try:
        if args.mark_done:
            res = exporter.mark_done(args.mark_done, reason=args.reason or "cli mark-done")
            print(json.dumps({"group_order_id": args.mark_done, **res}, ensure_ascii=False, indent=2))
            return 0
        if args.mark_failed:
            res = exporter.mark_failed(args.mark_failed, reason=args.reason or "cli mark-failed")
            print(json.dumps({"group_order_id": args.mark_failed, **res}, ensure_ascii=False, indent=2))
            return 0
        if args.list:
            print(json.dumps(exporter.get_summary(), ensure_ascii=False, indent=2))
            return 0
        if args.once:
            exporter.run_once()
            return 0

        stop_event = threading.Event()
        install_shutdown_signals(stop_event, logger=log)
        interval = max(5, int(args.interval))
        log.info("海报外发桥接脚本已启动，bridge_dir=%s, interval=%ss", bridge_path, interval)
        while not stop_event.is_set():
            exporter.run_once()
            if interruptible_sleep(stop_event, interval):
                break
        return 0
    finally:
        exporter.close()


if __name__ == "__main__":
    raise SystemExit(main())
