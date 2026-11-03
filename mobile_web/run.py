# -*- coding: utf-8 -*-
"""服务启动与自检入口，全部配置直接读取 config.local.json，无需额外传参。"""
import argparse
import sys

from infrastructure.logging_utils import configure_console_encoding
from infrastructure.settings import APP_DIR, SettingsError, get_settings


def main(argv=None):
    configure_console_encoding()
    parser = argparse.ArgumentParser(description="启动 mobile_web 服务（全部配置来自 config.local.json）")
    parser.add_argument("--config", help="可选：指定私有 JSON 配置路径（默认 config.local.json）")
    parser.add_argument("--reload", action="store_true", help="仅开发调试：启用热重载")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="校验 config.local.json 配置有效性")
    checks.add_argument("--check-mongo", action="store_true", help="只读 ping Mongo 连通性")
    args = parser.parse_args(argv)

    try:
        from infrastructure.settings import load_settings
        settings = load_settings(args.config) if args.config else get_settings()
    except SettingsError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.check_mongo:
        from infrastructure.mongo import gen_db_object
        db = None
        try:
            db = gen_db_object(settings)
            db.ping()
        except Exception as exc:
            # 驱动异常可能包含连接信息，不回显异常正文或凭据。
            print(f"MongoDB ping 失败（{type(exc).__name__}）；请检查连接、认证与网络。", file=sys.stderr)
            return 1
        finally:
            if db is not None:
                db.close()
        print("MongoDB ping 成功；未创建索引、未修改业务数据。")
        return 0

    if args.check:
        print("配置校验通过；未连接 MongoDB 或拼多多。")
        return 0

    import uvicorn
    uvicorn.run(
        "main:app", host=settings.web_host, port=settings.web_port,
        reload=args.reload, reload_dirs=[str(APP_DIR)] if args.reload else None,
        workers=1, access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
