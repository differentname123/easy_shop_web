# -*- coding: utf-8 -*-
"""
正式商品 & 好价线报规格结构化清洗主控脚本 (`goods_info_format.py`)。
================================================================================
- 运行参数统一集中在下方 `FORMAT_CONFIG`，可直接修改保存或通过 CLI 标志临时覆盖；
- 核心清洗逻辑与模型调用已收束至 `services.goods_formatter`；
- 常用示例：
    python mobile_web/scripts/goods_info_format.py
    python mobile_web/scripts/goods_info_format.py --once
    python mobile_web/scripts/goods_info_format.py --only-deal --once
================================================================================
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

from infrastructure.mongo import gen_db_object
from infrastructure.runtime_io import bootstrap_script, install_shutdown_signals, interruptible_sleep
from services import goods_formatter as gf

# ==============================================================================
# 【运维直改配置区】：所有核心运行参数集中在此，改完保存即可生效
# ==============================================================================
FORMAT_CONFIG: Dict[str, Any] = {
    "enable_official": True,                                  # 是否清洗正式商品 (`format_info.{sku_id}`)
    "enable_deal": True,                                      # 是否清洗好价线报 (`deal_info.format_info`)
    "round_interval_seconds": gf.ROUND_INTERVAL_SECONDS,      # 每轮结束后的等待间隔秒数
    "recent_days": gf.RECENT_DAYS,                            # 回溯扫描最近多少天内更新的商品
    "format_workers": gf.FORMAT_WORKERS,                      # 官方商品 SKU 清洗并发线程数
    "deal_format_workers": gf.DEAL_FORMAT_WORKERS,            # 好价线报清洗并发线程数
    "batch_size": gf.FORMAT_BATCH_SIZE,                       # 单次请求模型的 SKU 打包大小
    "deal_round_limit": gf.DEAL_FORMAT_ROUND_LIMIT,           # 单轮线报清洗数量上限 (0=不限)
}


def _worker_loop(
    task_name: str,
    round_func: Callable[[gf.ProductManager], Any],
    interval_seconds: int,
    stop_event: threading.Event,
    logger: Any,
    *,
    run_once: bool = False,
) -> None:
    db_instance = product_manager = None
    try:
        while not stop_event.is_set():
            try:
                if product_manager is None:
                    if db_instance is not None:
                        db_instance.close()
                        db_instance = None
                    db_instance = gen_db_object()
                    db_instance.ping()
                    product_manager = gf.ProductManager(db_instance)

                round_func(product_manager)
            except Exception as exc:
                product_manager = None
                logger.exception(
                    "❌ [%s调度/异常] 本轮终止，下轮重新连接 | 间隔: [%d 秒] | 原因: [%s]",
                    task_name,
                    interval_seconds,
                    " ".join(str(exc).split())[:400],
                )
            if run_once or stop_event.is_set():
                break
            logger.info("[%s调度/等待] 本轮结束 | 等待: [%d 秒]", task_name, interval_seconds)
            interruptible_sleep(stop_event, interval_seconds)
    finally:
        if db_instance is not None:
            db_instance.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="商品与好价线报规格结构化清洗主控脚本")
    parser.add_argument("--once", action="store_true", help="仅执行一轮清洗后立即退出")
    parser.add_argument("--only-official", action="store_true", help="仅运行正式商品规格清洗")
    parser.add_argument("--only-deal", action="store_true", help="仅运行好价线报规格清洗")
    parser.add_argument("--interval", type=int, default=0, help="覆盖每轮等待间隔秒数")
    parser.add_argument("--recent-days", type=int, default=0, help="覆盖回溯最近更新天数")
    args = parser.parse_args(argv)

    logger = bootstrap_script(
        "goods_format",
        "goods_format",
        log_path=MOBILE_WEB_DIR / "logs" / "goods_format.log",
        configure_network=False,
    )

    cfg = dict(FORMAT_CONFIG)
    if args.only_official and not args.only_deal:
        cfg["enable_official"], cfg["enable_deal"] = True, False
    elif args.only_deal and not args.only_official:
        cfg["enable_official"], cfg["enable_deal"] = False, True
    interval = max(1, int(args.interval or cfg["round_interval_seconds"]))
    recent_days = max(1, int(args.recent_days or cfg["recent_days"]))

    stop_event = threading.Event()
    install_shutdown_signals(stop_event, logger=logger)

    tasks = []
    if cfg["enable_official"]:
        tasks.append((
            "格式化",
            "format_task",
            lambda pm: gf.run_format_round(
                pm,
                workers=int(cfg["format_workers"]),
                batch_size=int(cfg["batch_size"]),
                recent_days=recent_days,
            ),
        ))
    if cfg["enable_deal"]:
        tasks.append((
            "线报格式化",
            "deal_format_task",
            lambda pm: gf.run_deal_format_round(
                pm,
                limit=int(cfg["deal_round_limit"]),
                workers=int(cfg["deal_format_workers"]),
                batch_size=int(cfg["batch_size"]),
                recent_days=recent_days,
            ),
        ))

    threads = [
        threading.Thread(
            target=_worker_loop,
            args=(task_label, fn, interval, stop_event, logger),
            kwargs={"run_once": bool(args.once)},
            name=thread_name,
            daemon=True,
        )
        for task_label, thread_name, fn in tasks
    ]
    for t in threads:
        t.start()
        logger.info("[系统/启动] 已启动 %s 线程 (TID: %d)", t.name, t.ident)

    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
