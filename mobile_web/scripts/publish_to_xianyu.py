# -*- coding: utf-8 -*-
r"""
闲鱼 (Goofish) 手动浏览器接管与工具命令行入口。
================================================================================
- 底层能力统一位于 `infrastructure.xianyu`（会话/锁、卖家操作、市场搜索与 H5 详情）；
- 常用默认配置集中于下方 `XIANYU_CLI_CONFIG`，不带子命令直接运行等同于 `login`；
- 常用示例：
    python mobile_web/scripts/publish_to_xianyu.py
    python mobile_web/scripts/publish_to_xianyu.py --account seller status
    python mobile_web/scripts/publish_to_xianyu.py search "可口可乐 330ml 24罐" --max-items 5
    python mobile_web/scripts/publish_to_xianyu.py detail 780427279990
================================================================================
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

from infrastructure.runtime_io import bootstrap_script
from infrastructure.xianyu import (
    DEFAULT_GUEST_USER_DATA_DIR,
    DEFAULT_SEARCH_USER_DATA_DIR,
    DEFAULT_USER_DATA_DIR,
    GOOFISH_PERSONAL_URL,
    check_login_status,
    clean_browser_cache,
    get_messages,
    get_selling_items,
    get_xianyu_item_detail,
    open_browser_for_manual_use,
    search_xianyu_items,
)

# ==============================================================================
# 【运维直改配置区】：直接运行本脚本无子命令时，默认打开 `default_account` 登录窗
# ==============================================================================
XIANYU_CLI_CONFIG: Dict[str, Any] = {
    "default_account": "search",             # 可选: "search"(比价搜索小号) | "seller"(卖货主号) | "guest"(免登录访客)
    "home_url": GOOFISH_PERSONAL_URL,      # `login` 手动接管打开的初始页面
    "debug": False,                        # 是否开启有头调试窗口
}

_ACCOUNT_DIRS: Dict[str, str] = {
    "seller": DEFAULT_USER_DATA_DIR,
    "search": DEFAULT_SEARCH_USER_DATA_DIR,
    "guest": DEFAULT_GUEST_USER_DATA_DIR,
}


def _resolve_user_data_dir(account: Optional[str], explicit_dir: str, *, default_account: str) -> str:
    if str(explicit_dir or "").strip():
        return str(explicit_dir).strip()
    key = account or default_account
    return _ACCOUNT_DIRS.get(key, DEFAULT_SEARCH_USER_DATA_DIR)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="闲鱼 (Goofish) 手动浏览器接管与诊断命令行工具"
    )
    parser.add_argument(
        "--account",
        choices=("seller", "search", "guest"),
        default=None,
        help=(
            f"目标账号目录预设 (seller={DEFAULT_USER_DATA_DIR}, search={DEFAULT_SEARCH_USER_DATA_DIR}, "
            f"guest={DEFAULT_GUEST_USER_DATA_DIR}；默认按 XIANYU_CLI_CONFIG['default_account']，detail 默认 guest)"
        ),
    )
    parser.add_argument(
        "--user-data-dir",
        type=str,
        default="",
        help="可选直接指定浏览器目录路径（优先级高于 --account）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="显示浏览器窗口用于现场调试",
    )

    sub = parser.add_subparsers(dest="command", help="可执行子命令（省略时默认执行 login）")

    p_login = sub.add_parser("login", help="打开持久化浏览器窗口供人工扫码登录/维护后关闭保存")
    p_login.add_argument("--home-url", type=str, default=str(XIANYU_CLI_CONFIG["home_url"]))

    sub.add_parser("status", help="快速检测账号在线状态与风控提示")

    p_selling = sub.add_parser("selling", help="查询在售商品并核对指定 item_id 状态")
    p_selling.add_argument("--expected-ids", type=str, default="", help="逗号分隔的待核对商品 ID 列表")

    p_msg = sub.add_parser("messages", help="拉取左侧买家会话与消息详情")
    p_msg.add_argument("--only-unread", action="store_true", help="仅读取未读会话")
    p_msg.add_argument("--only-my-items", action="store_true", help="仅过滤绑定我方商品的会话")
    p_msg.add_argument("--max-sessions", type=int, default=10, help="最多拉取多少个会话（默认 10）")

    p_search = sub.add_parser("search", help="使用搜索小号按关键字搜索闲鱼公开概览商品")
    p_search.add_argument("keyword", type=str, help="搜索关键字")
    p_search.add_argument("--max-items", type=int, default=10, help="最多返回条数（默认 10）")

    p_detail = sub.add_parser("detail", help="使用纯内存无痕 H5 会话抓取单个闲鱼商品完整详情")
    p_detail.add_argument("item_id_or_url", type=str, help="闲鱼 item_id 或商品链接")

    sub.add_parser("clean-cache", help="清理指定浏览器目录的缓存垃圾目录以释放空间")

    args = parser.parse_args(argv)
    bootstrap_script("xianyu_cli", "publish_to_xianyu", log_path=None, configure_network=False)

    cmd = args.command or "login"
    debug = bool(args.debug or XIANYU_CLI_CONFIG["debug"])
    fallback_acc = "guest" if cmd == "detail" else str(XIANYU_CLI_CONFIG["default_account"])
    user_dir = _resolve_user_data_dir(args.account, args.user_data_dir, default_account=fallback_acc)

    if cmd == "login":
        home_url = getattr(args, "home_url", str(XIANYU_CLI_CONFIG["home_url"]))
        open_browser_for_manual_use(user_data_dir=user_dir, home_url=home_url)
        return 0

    if cmd == "clean-cache":
        clean_browser_cache(user_data_dir=user_dir)
        return 0

    if cmd == "status":
        res = check_login_status(user_data_dir=user_dir, debug=debug)
    elif cmd == "selling":
        expected = [x.strip() for x in str(args.expected_ids or "").split(",") if x.strip()]
        res = get_selling_items(
            user_data_dir=user_dir,
            expected_item_ids=expected or None,
            debug=debug,
        )
    elif cmd == "messages":
        res = get_messages(
            user_data_dir=user_dir,
            only_unread=bool(args.only_unread),
            only_my_items=bool(args.only_my_items),
            max_sessions=max(1, int(args.max_sessions)),
            debug=debug,
        )
    elif cmd == "search":
        res = search_xianyu_items(
            keyword=args.keyword,
            max_items=max(1, int(args.max_items)),
            user_data_dir=user_dir,
            debug=debug,
        )
    elif cmd == "detail":
        res = get_xianyu_item_detail(
            item_id_or_url=args.item_id_or_url,
            user_data_dir=user_dir,
            debug=debug,
        )
    else:
        parser.print_help()
        return 1

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
