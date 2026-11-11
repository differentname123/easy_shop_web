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
    checks.add_argument("--check-ai", action="store_true", help="校验当前系统环境对应的 AI 接口连通性")
    parser.add_argument("--cloud", action="store_true", help="配合 --check-ai 使用：在 Windows 本地提前测试 cloud_ai 云端配置")
    args = parser.parse_args(argv)

    try:
        if args.config:
            import os
            from infrastructure.settings import app_path
            os.environ["WEB_CONFIG_FILE"] = str(app_path(args.config))
            get_settings.cache_clear()
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

    if args.check_ai:
        from infrastructure.ai_api.model_api import generate_content
        res = generate_content(
            prompt='请仅回复 JSON: {"ok": true}',
            max_retries_per_model=2,
            timeout=20,
            stop_on_timeout=False,
            force_cloud=bool(args.cloud),
        )
        metrics = res.get("metrics") or {}
        mode_label = "Linux云端(cloud_ai)" if metrics.get("runtime_mode") == "linux_cloud" else "Windows本地(local_api_url)"
        if res.get("status") == "✅ 成功":
            slot_info = f" | 密钥槽: {metrics['key_slot']}" if metrics.get("key_slot") else ""
            print(
                f"AI 连通性自检成功 | 环境: {mode_label} | 命中模型: {metrics.get('model_used')}{slot_info} "
                f"| 耗时: {metrics.get('total_time_seconds')}s"
            )
            return 0
        print(
            f"AI 连通性自检失败 | 环境: {mode_label} | {res.get('content') or '请检查 config.local.json 中的 cloud_ai 配置'}",
            file=sys.stderr,
        )
        return 1

    if args.check:
        print("配置校验通过；未连接 MongoDB 或外部平台。")
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
