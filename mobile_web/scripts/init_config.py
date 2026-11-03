# -*- coding: utf-8 -*-
"""一次性创建私有配置；可显式从旧项目 JSON 按白名单迁入，绝不覆盖已有文件。"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
from infrastructure.logging_utils import configure_console_encoding
from infrastructure.settings import LEGACY_AUTH_SECRET, app_path

FIELDS = {
    "myself_pdd_client_id": "",
    "myself_pdd_client_secret": "",
    "myself_pdd_pid": "",
    "local_mongo_host": "localhost",
    "local_mongo_port": 27017,
    "local_mongo_user": "",
    "local_mongo_password": "",
    "local_mongo_db_name": "crawler_data",
}


def _safe_secret(value):
    return (isinstance(value, str) and len(value.strip()) >= 32
            and value != LEGACY_AUTH_SECRET and not value.lower().startswith("change-me"))


def main(argv=None):
    configure_console_encoding()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="旧配置 JSON；仅迁入本应用需要的字段")
    parser.add_argument("--output", default="config.local.json", help="输出路径，默认在应用目录内")
    args = parser.parse_args(argv)
    target = app_path(args.output)
    if target.exists():
        print(f"配置已存在，未覆盖：{target}")
        return 0
    source = {}
    if args.source:
        try:
            with Path(args.source).expanduser().open("r", encoding="utf-8-sig") as handle:
                source = json.load(handle)
        except (OSError, ValueError, UnicodeError):
            print("无法读取源配置，请检查路径、权限及 UTF-8 JSON 格式；未创建配置。", file=sys.stderr)
            return 2
        if not isinstance(source, dict):
            print("源配置顶层必须是 JSON 对象；未创建配置。", file=sys.stderr)
            return 2
    values = {key: source.get(key, default) for key, default in FIELDS.items()}
    # 仅迁入阶段将 null/空字符串视为未提供；运行配置解析器仍严格校验显式空值。
    for key in ("local_mongo_host", "local_mongo_port", "local_mongo_db_name"):
        if values[key] is None or values[key] == "":
            values[key] = FIELDS[key]
    # authSource 是可选的兼容扩展；空值不迁入，也不带入其他账号/服务配置。
    if source.get("mongo_auth_source") not in (None, ""):
        values["mongo_auth_source"] = source["mongo_auth_source"]
    auth_secret = os.environ.get("WEB_AUTH_SECRET", source.get("web_auth_secret", ""))
    if "WEB_AUTH_SECRET" in os.environ and not _safe_secret(auth_secret):
        print("当前 WEB_AUTH_SECRET 不安全；请先更正或取消该环境变量，未创建配置。", file=sys.stderr)
        return 2
    generated = not _safe_secret(auth_secret)
    values["web_auth_secret"] = secrets.token_urlsafe(48) if generated else auth_secret
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL 避免并发初始化覆盖；在 Debian 上一开始就是仅所有者可读写。
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"配置已存在，未覆盖：{target}")
        return 0
    except OSError:
        print("无法创建私有配置，请检查输出目录权限。", file=sys.stderr)
        return 2
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    print(f"私有配置已创建：{target}（未输出配置值）")
    if generated:
        print("已生成并持久化新的认证密钥；旧令牌将失效，需要重新登录。")
    else:
        print("已保留现有私有认证密钥。")
    print("请填写尚未配置的 PDD 字段，再运行 python run.py --check。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
