# -*- coding: utf-8 -*-
r"""
闲鱼选品与上架工作台独立启动入口与账号管理 CLI。
================================================================================
常用示例：
  python xianyu_workbench/run.py create-user admin 123456 --bind-uid u_xxxxxxxx
  python xianyu_workbench/run.py create-user admin 123456 --bind-phone 13800138000
  python xianyu_workbench/run.py list-users
  python xianyu_workbench/run.py reset-password admin newpass123
  python xianyu_workbench/run.py              # 默认启动 Web 工作台 (127.0.0.1:8002)
================================================================================
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, List, Optional

WORKBENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = WORKBENCH_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from xianyu_workbench.bridge import ensure_mobile_web_on_path, open_mongo_connection

ensure_mobile_web_on_path()

from xianyu_workbench.auth import hash_password
from xianyu_workbench.config import WorkbenchConfig, load_config
from xianyu_workbench.repo import WorkbenchRepository


def cmd_create_user(
    repo: WorkbenchRepository,
    *,
    username: str,
    password: str,
    bind_uid: str = "",
    bind_phone: str = "",
) -> dict:
    repo.ensure_indexes()
    target_uid = str(bind_uid or "").strip()
    if not target_uid and str(bind_phone or "").strip():
        resolved = repo.find_shop_uid_by_phone(bind_phone)
        if not resolved:
            raise ValueError(f"未在主站 users 表找到手机号 {bind_phone} 对应的用户")
        target_uid = resolved
    return repo.create_user(username, hash_password(password), bound_shop_uid=target_uid)


def cmd_reset_password(repo: WorkbenchRepository, *, username: str, password: str) -> bool:
    repo.ensure_indexes()
    ok = repo.update_password(username, hash_password(password))
    if not ok:
        raise ValueError(f"账号 {username} 不存在")
    return True


def cmd_list_users(repo: WorkbenchRepository) -> List[dict]:
    repo.ensure_indexes()
    return repo.list_users()


def main(argv: Optional[List[str]] = None) -> int:
    cfg: WorkbenchConfig = load_config()
    parser = argparse.ArgumentParser(description="闲鱼选品与上架工作台 CLI")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动闲鱼工作台 Web 服务")
    p_serve.add_argument("--host", default=cfg.host, help=f"监听地址（默认 {cfg.host}）")
    p_serve.add_argument("--port", type=int, default=cfg.port, help=f"监听端口（默认 {cfg.port}）")

    p_create = sub.add_parser("create-user", help="创建工作台登录账号")
    p_create.add_argument("username", help="用户名（3~32位字母数字或 _.-）")
    p_create.add_argument("password", help="登录密码（至少6位）")
    p_create.add_argument("--bind-uid", default="", help="绑定的主站收藏夹 UID")
    p_create.add_argument("--bind-phone", default="", help="通过主站手机号自动反查绑定 UID")

    p_reset = sub.add_parser("reset-password", help="重置工作台账号密码")
    p_reset.add_argument("username", help="用户名")
    p_reset.add_argument("password", help="新密码（至少6位）")

    sub.add_parser("list-users", help="列出所有工作台账号")

    args = parser.parse_args(argv)
    cmd = args.command or "serve"

    if cmd == "serve":
        import uvicorn
        from xianyu_workbench.api import create_app

        host = getattr(args, "host", cfg.host)
        port = int(getattr(args, "port", cfg.port))
        print(f"🚀 闲鱼选品与上架工作台已启动: http://{host}:{port}")
        uvicorn.run(create_app(cfg), host=host, port=port)
        return 0

    db, close_fn = open_mongo_connection(cfg)
    try:
        repo = WorkbenchRepository(db)
        if cmd == "create-user":
            created = cmd_create_user(
                repo,
                username=args.username,
                password=args.password,
                bind_uid=args.bind_uid,
                bind_phone=args.bind_phone,
            )
            print(
                f"✅ 账号创建成功: username={created['username']} | "
                f"bound_shop_uid={created.get('bound_shop_uid') or '(未绑定)'}"
            )
            return 0
        if cmd == "reset-password":
            cmd_reset_password(repo, username=args.username, password=args.password)
            print(f"✅ 密码已重置: username={args.username}")
            return 0
        if cmd == "list-users":
            users = cmd_list_users(repo)
            if not users:
                print("(暂无工作台账号，请运行 create-user 创建)")
            for u in users:
                print(
                    f"- {u['username']} | bound_shop_uid={u.get('bound_shop_uid') or '(未绑定)'}"
                )
            return 0
    except Exception as exc:
        print(f"❌ 执行失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if callable(close_fn):
            close_fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
