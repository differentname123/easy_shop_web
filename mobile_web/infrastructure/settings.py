# -*- coding: utf-8 -*-
"""单一配置入口：环境变量 > 本目录私有 JSON > 非敏感默认值。"""
import json
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = APP_DIR / "config.local.json"
LEGACY_AUTH_SECRET = "easy_shop_secure_auth_secret_key_2026_v2"


class SettingsError(ValueError):
    """可直接向操作者展示、不会包含配置值的错误。"""


@dataclass(frozen=True)
class Settings:
    pdd_client_id: str
    pdd_client_secret: str = field(repr=False)
    pdd_pid: str
    mongo_host: str
    mongo_port: int
    mongo_user: str
    mongo_password: str = field(repr=False)
    mongo_db_name: str
    mongo_auth_source: str
    web_host: str
    web_port: int
    auth_secret: str = field(repr=False)
    warmup_enabled: bool
    log_dir: Optional[Path]
    log_level: str
    pdd_dns_mode: str
    pdd_trust_env: bool


def app_path(value) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else APP_DIR / path).resolve()


def _read_config(config_file, environ):
    explicit = config_file is not None or "WEB_CONFIG_FILE" in environ
    selected = config_file if config_file is not None else environ.get("WEB_CONFIG_FILE", DEFAULT_CONFIG_FILE)
    if not str(selected).strip():
        raise SettingsError("WEB_CONFIG_FILE 不能为空")
    path = app_path(selected)
    if not path.exists() and not explicit:
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            values = json.load(source)
    except OSError:
        raise SettingsError(f"无法读取配置文件：{path}") from None
    except (ValueError, UnicodeError):
        raise SettingsError(f"配置文件必须为有效 UTF-8 JSON：{path}") from None
    if not isinstance(values, dict):
        raise SettingsError("配置文件顶层必须是 JSON 对象")
    return values


def _text(value, name):
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SettingsError(f"{name} 必须是字符串")
    return str(value)


def _port(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SettingsError(f"{name} 必须是 1 到 65535 的整数")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} 必须是 1 到 65535 的整数") from None
    if not 1 <= port <= 65535:
        raise SettingsError(f"{name} 必须是 1 到 65535 的整数")
    return port


def _boolean(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise SettingsError(f"{name} 必须是 true/false 或 1/0")


def load_settings(config_file=None, environ: Optional[Mapping[str, str]] = None, *, require_credentials=True) -> Settings:
    env = os.environ if environ is None else environ
    values = _read_config(config_file, env)

    def get(name, key, default=""):
        # 环境中显式空值也覆盖 JSON，允许取消 Mongo 认证和关闭文件日志。
        return env[name] if name in env else values.get(key, default)

    def text(name, key, default=""):
        return _text(get(name, key, default), name)

    pdd_client_id = text("PDD_CLIENT_ID", "myself_pdd_client_id").strip()
    pdd_client_secret = text("PDD_CLIENT_SECRET", "myself_pdd_client_secret")
    pdd_pid = text("PDD_PID", "myself_pdd_pid").strip()
    auth_secret = text("WEB_AUTH_SECRET", "web_auth_secret")
    mongo_user = text("MONGO_USER", "local_mongo_user")
    mongo_password = text("MONGO_PASSWORD", "local_mongo_password")
    mongo_host = text("MONGO_HOST", "local_mongo_host", "localhost").strip()
    mongo_db_name = text("MONGO_DB_NAME", "local_mongo_db_name", "crawler_data").strip()
    mongo_auth_source = text("MONGO_AUTH_SOURCE", "mongo_auth_source", "admin").strip()
    web_host = text("WEB_HOST", "web_host", "127.0.0.1").strip()
    for name, value in (("MONGO_HOST", mongo_host), ("MONGO_DB_NAME", mongo_db_name),
                        ("MONGO_AUTH_SOURCE", mongo_auth_source), ("WEB_HOST", web_host)):
        if not value:
            raise SettingsError(f"{name} 不能为空")
    if bool(mongo_user) != bool(mongo_password):
        raise SettingsError("MONGO_USER 与 MONGO_PASSWORD 必须同时配置或同时留空")
    if require_credentials:
        missing = [name for name, value in (("PDD_CLIENT_ID", pdd_client_id),
                   ("PDD_CLIENT_SECRET", pdd_client_secret), ("PDD_PID", pdd_pid)) if not value.strip()]
        if missing:
            raise SettingsError("缺少必需配置：" + ", ".join(missing))
        if len(auth_secret.strip()) < 32 or auth_secret == LEGACY_AUTH_SECRET or auth_secret.lower().startswith("change-me"):
            raise SettingsError("WEB_AUTH_SECRET 必须是至少 32 字符的私有密钥；请先初始化 config.local.json")
    log_dir = text("WEB_LOG_DIR", "web_log_dir", "runtime/logs").strip()
    log_level = text("WEB_LOG_LEVEL", "web_log_level", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise SettingsError("WEB_LOG_LEVEL 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL")
    dns_mode = text("PDD_DNS_MODE", "pdd_dns_mode", "auto").strip().lower()
    if dns_mode not in {"auto", "on", "off"}:
        raise SettingsError("PDD_DNS_MODE 必须是 auto/on/off")
    return Settings(
        pdd_client_id=pdd_client_id, pdd_client_secret=pdd_client_secret, pdd_pid=pdd_pid,
        mongo_host=mongo_host, mongo_port=_port(get("MONGO_PORT", "local_mongo_port", 27017), "MONGO_PORT"),
        mongo_user=mongo_user, mongo_password=mongo_password, mongo_db_name=mongo_db_name,
        mongo_auth_source=mongo_auth_source, web_host=web_host,
        web_port=_port(env.get("WEB_PORT", env.get("PORT", values.get("web_port", 8000))), "WEB_PORT"),
        auth_secret=auth_secret,
        warmup_enabled=_boolean(get("WEB_WARMUP_ENABLED", "web_warmup_enabled", True), "WEB_WARMUP_ENABLED"),
        log_dir=None if log_dir in {"", "-"} else app_path(log_dir), log_level=log_level,
        pdd_dns_mode=dns_mode,
        pdd_trust_env=_boolean(get("PDD_TRUST_ENV", "pdd_trust_env", sys.platform != "win32"), "PDD_TRUST_ENV"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """每个进程使用同一份配置；修改文件或服务环境后需要重启。"""
    return load_settings()
