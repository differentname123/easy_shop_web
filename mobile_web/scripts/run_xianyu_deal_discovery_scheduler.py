# -*- coding: utf-8 -*-
r"""
闲鱼高性价比商品探测与比价定时任务主控脚本。
================================================================================
- 所有核心选品、缓存复用与三段式 LLM 比价逻辑均位于 `services.xianyu_discovery`；
- 所有日常运维参数集中在下方 `XIANYU_SCHEDULER_CONFIG`，修改保存即可生效；
- 常用示例：
    python mobile_web/scripts/run_xianyu_deal_discovery_scheduler.py
    python mobile_web/scripts/run_xianyu_deal_discovery_scheduler.py --once --only-official
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

from infrastructure.runtime_io import (
    bootstrap_script,
    install_shutdown_signals,
    interruptible_sleep,
)
from services import xianyu_discovery as xd

# ==============================================================================
# 【运维直改配置区】：闲鱼比价探测常用开关集中在此，一目了然、随时调整
# ==============================================================================
XIANYU_SCHEDULER_CONFIG: Dict[str, Any] = {
    "enable_loop": xd.ENABLE_LOOP,                                       # True=持续定时循环，False=单轮退出
    "loop_interval_hours": xd.LOOP_INTERVAL_HOURS,                       # 正常每轮结束后的等待间隔（小时）
    "risk_cooldown_hours": xd.RISK_COOLDOWN_HOURS,                       # 触发闲鱼风控/掉线熔断后的静默冷却（小时）
    "max_consecutive_browser_fails": xd.MAX_CONSECUTIVE_BROWSER_FAILS,   # 单轮连续多少次浏览器失败触发整轮熔断
    "enable_official": xd.ENABLE_OFFICIAL,                               # 是否开启【正式官方商品】比价
    "enable_deal_tip": xd.ENABLE_DEAL_TIP,                               # 是否开启【线报爆料商品】比价
    "scan_hours_window": xd.SCAN_HOURS_WINDOW,                           # 正式官方商品时效性窗口（小时）
    "deal_scan_hours_window": xd.DEAL_SCAN_HOURS_WINDOW,                 # 线报爆料商品时效性窗口（小时）
    "max_categories_per_round": xd.MAX_CATEGORIES_PER_ROUND,             # 单源每轮选取的类别上限
    "max_xianyu_search_items": xd.MAX_XIANYU_SEARCH_ITEMS,               # 每次闲鱼搜索抓取的概览商品数
    "max_detail_checks_per_category": xd.MAX_DETAIL_CHECKS_PER_CATEGORY, # 每个商品最多补充详情的可比闲鱼款数
    "max_live_h5_details_per_round": xd.MAX_LIVE_H5_DETAILS_PER_ROUND,   # 单轮实时 H5 新拉详情软上限
    "xianyu_detail_cache_days": xd.XIANYU_DETAIL_CACHE_DAYS,             # `xianyu_items` 详情缓存有效天数
    "debug_browser": xd.DEBUG_BROWSER,                                   # False=后台无头静默，True=显示浏览器窗口
    "search_user_data_dir": xd.SEARCH_USER_DATA_DIR,                     # 搜索小号独立目录
    "guest_user_data_dir": xd.GUEST_USER_DATA_DIR,                       # H5 访客详情串行锁标识目录
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="闲鱼高性价比商品探测与比价定时任务主控脚本"
    )
    parser.add_argument("--once", action="store_true", help="仅执行一轮探测比价后立即退出")
    parser.add_argument("--only-official", action="store_true", help="仅运行【正式官方商品】比价")
    parser.add_argument("--only-deal", action="store_true", help="仅运行【线报爆料商品】比价")
    parser.add_argument(
        "--max-categories",
        type=int,
        default=int(XIANYU_SCHEDULER_CONFIG["max_categories_per_round"]),
        help=f"单源每轮选取类别上限（默认 {XIANYU_SCHEDULER_CONFIG['max_categories_per_round']}）",
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=float(XIANYU_SCHEDULER_CONFIG["loop_interval_hours"]),
        help=f"正常轮次间隔小时数（默认 {XIANYU_SCHEDULER_CONFIG['loop_interval_hours']}h）",
    )
    parser.add_argument("--debug-browser", action="store_true", help="打开有头浏览器窗口调试")
    args = parser.parse_args(argv)

    logger = bootstrap_script(
        "xianyu_scheduler",
        "xianyu_deal_discovery",
        log_path=xd.LOG_FILE,
        backup_count=5,
        fmt="%(asctime)s - [%(levelname)s] %(message)s",
        configure_network=True,
    )
    xd.attach_xianyu_warning_forwarder(logger)

    if args.only_official and not args.only_deal:
        eff_official, eff_deal = True, False
    elif args.only_deal and not args.only_official:
        eff_official, eff_deal = False, True
    elif args.only_official and args.only_deal:
        eff_official, eff_deal = True, True
    else:
        eff_official = bool(XIANYU_SCHEDULER_CONFIG["enable_official"])
        eff_deal = bool(XIANYU_SCHEDULER_CONFIG["enable_deal_tip"])

    eff_loop = bool(XIANYU_SCHEDULER_CONFIG["enable_loop"]) and not args.once
    eff_interval_hours = float(args.interval_hours)
    eff_risk_cooldown_hours = float(XIANYU_SCHEDULER_CONFIG["risk_cooldown_hours"])
    eff_debug_browser = bool(args.debug_browser or XIANYU_SCHEDULER_CONFIG["debug_browser"])

    stop_event = threading.Event()
    install_shutdown_signals(stop_event, logger=logger)

    while not stop_event.is_set():
        try:
            xd.run_discovery_cycle(
                hours_window=int(XIANYU_SCHEDULER_CONFIG["scan_hours_window"]),
                deal_hours_window=int(XIANYU_SCHEDULER_CONFIG["deal_scan_hours_window"]),
                enable_official=eff_official,
                enable_deal_tip=eff_deal,
                max_categories=int(args.max_categories),
                max_search_items=int(XIANYU_SCHEDULER_CONFIG["max_xianyu_search_items"]),
                max_detail_checks=int(XIANYU_SCHEDULER_CONFIG["max_detail_checks_per_category"]),
                detail_cache_days=int(XIANYU_SCHEDULER_CONFIG["xianyu_detail_cache_days"]),
                search_user_data_dir=str(XIANYU_SCHEDULER_CONFIG["search_user_data_dir"]),
                guest_user_data_dir=str(XIANYU_SCHEDULER_CONFIG["guest_user_data_dir"]),
                debug_browser=eff_debug_browser,
                max_consecutive_browser_fails=int(XIANYU_SCHEDULER_CONFIG["max_consecutive_browser_fails"]),
                max_live_h5_details=int(XIANYU_SCHEDULER_CONFIG["max_live_h5_details_per_round"]),
            )
        except KeyboardInterrupt:
            logger.info("👋 收到中断信号，任务退出。")
            break
        except Exception as exc:
            logger.exception(f"💥 [探测轮次异常] {exc}")

        if not eff_loop or stop_event.is_set():
            break

        if xd.was_last_cycle_aborted_by_risk():
            sleep_hours = max(eff_interval_hours or 1.0, eff_risk_cooldown_hours or 2.0)
            logger.warning(
                f"🛑 本轮触发闲鱼限流/掉线保护，进入静默冷却：等待 {sleep_hours} 小时后再执行下一轮..."
            )
        else:
            sleep_hours = eff_interval_hours or 1.0
            logger.info(f"⏳ 本轮执行完毕，等待 {sleep_hours} 小时后执行下一轮...")

        if interruptible_sleep(stop_event, max(60.0, sleep_hours * 3600.0)):
            logger.info("👋 收到停止信号，闲鱼探测调度器退出。")
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
