# -*- coding: utf-8 -*-
"""Windows / Debian 共用入口；可在任意工作目录执行本文件的绝对路径。"""
import argparse
import os
import sys

from infrastructure.logging_utils import configure_console_encoding
from infrastructure.settings import APP_DIR, SettingsError, get_settings


def main(argv=None):
    configure_console_encoding()
    parser = argparse.ArgumentParser(description="启动独立 mobile_web 服务（默认单进程，无热重载）")
    parser.add_argument("--host", help="监听地址；覆盖 WEB_HOST")
    parser.add_argument("--port", type=int, help="监听端口；覆盖 WEB_PORT")
    parser.add_argument("--config", help="私有 JSON 配置路径；相对路径以应用目录为基准")
    parser.add_argument("--reload", action="store_true", help="仅开发使用：启用热重载")
    parser.add_argument("--no-warmup", action="store_true", help="禁用启动时的联盟请求及推荐写库预热")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="只校验配置，不连接外部服务")
    checks.add_argument("--check-mongo", action="store_true", help="校验配置并只读 ping Mongo，不创建索引")
    args = parser.parse_args(argv)
    for name, value in (("WEB_HOST", args.host), ("WEB_PORT", args.port), ("WEB_CONFIG_FILE", args.config)):
        if value is not None:
            os.environ[name] = str(value)
    if args.no_warmup:
        os.environ["WEB_WARMUP_ENABLED"] = "0"
    get_settings.cache_clear()
    try:
        settings = get_settings()
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
