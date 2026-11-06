# -*- coding: utf-8 -*-
"""业务配置统一从私有 JSON 读取；WEB_CONFIG_FILE 仅选择文件路径。"""
import json
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

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
    pdd_enabled: bool = True
    taobao_enabled: bool = False
    tb_app_key: str = ""
    tb_app_secret: str = field(default="", repr=False)
    tb_pid: str = ""
    tb_adzone_id: str = ""
    tb_session: str = field(default="", repr=False)
    tb_inviter_code: str = field(default="", repr=False)
    hdk_api_key: str = field(default="", repr=False)
    ztk_appkey: str = field(default="", repr=False)
    ztk_appkey_list: tuple = field(default=(), repr=False)
    ztk_sid: str = field(default="", repr=False)
    detail_prefetch_enabled: bool = True
    detail_prefetch_interval_seconds: int = 300
    jd_enabled: bool = False
    jd_union_id: str = ""
    jd_position_id: str = ""
    jd_app_key: str = ""
    jd_app_secret: str = field(default="", repr=False)
    jd_api_base: str = "http://api.zhetaoke.com:20000/api"


def app_path(value) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else APP_DIR / path).resolve()


def _read_config(config_file=None):
    import os
    selected = config_file or os.environ.get("WEB_CONFIG_FILE") or DEFAULT_CONFIG_FILE
    path = app_path(selected)
    if not path.exists():
        if config_file is not None or "WEB_CONFIG_FILE" in os.environ:
            raise SettingsError(f"无法读取配置文件：{path}")
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


def load_settings(config_file=None, environ=None, *, require_credentials=True) -> Settings:
    """纯粹从私有 JSON 读取所有配置，不读取任何环境变量。"""
    values = _read_config(config_file)

    def text(key, default=""):
        return _text(values.get(key, default), key)

    pdd_client_id = text("myself_pdd_client_id").strip()
    pdd_client_secret = text("myself_pdd_client_secret")
    pdd_pid = text("myself_pdd_pid").strip()
    tb_app_key = text("myself_taobao_app_key").strip()
    tb_app_secret = text("myself_taobao_app_secret")
    tb_pid = text("myself_taobao_pid").strip()
    tb_adzone_id = text("taobao_adzone_id").strip()
    if not tb_adzone_id and tb_pid.startswith("mm_"):
        parts = tb_pid.split("_")
        if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
            tb_adzone_id = parts[-1]
    pdd_enabled = _boolean(values.get("pdd_enabled", True), "pdd_enabled")
    taobao_enabled = _boolean(values.get("taobao_enabled", bool(tb_app_key and tb_app_secret and tb_adzone_id)),
                              "taobao_enabled")
    jd_enabled = _boolean(values.get("jd_enabled", False), "jd_enabled")
    jd_union_id = text("myself_jd_union_id").strip()
    jd_position_id = text("myself_jd_position_id").strip()
    jd_app_key = text("myself_jd_app_key").strip()
    jd_app_secret = text("myself_jd_app_secret")
    jd_api_base = text("jd_api_base", "http://api.zhetaoke.com:20000/api").strip()

    def _parse_key_entries(raw_val, field_name):
        if raw_val is None:
            return []
        if isinstance(raw_val, bool):
            raise SettingsError(f"{field_name} 必须是字符串或字符串列表")
        if isinstance(raw_val, (str, int)):
            return [part.strip() for part in str(raw_val).split(",") if part.strip()]
        if isinstance(raw_val, list):
            items = []
            for entry in raw_val:
                if isinstance(entry, bool) or not isinstance(entry, (str, int)):
                    raise SettingsError(f"{field_name} 列表元素必须是字符串")
                items.extend(part.strip() for part in str(entry).split(",") if part.strip())
            return items
        raise SettingsError(f"{field_name} 必须是字符串或字符串列表")

    primary_keys = _parse_key_entries(values.get("zhetaoke_appkey", ""), "zhetaoke_appkey")
    extra_keys = _parse_key_entries(values.get("zhetaoke_appkey_list", None), "zhetaoke_appkey_list")
    merged_keys = []
    for k in primary_keys + extra_keys:
        if k and k not in merged_keys:
            merged_keys.append(k)
    ztk_appkey_list = tuple(merged_keys)
    ztk_appkey = ztk_appkey_list[0] if ztk_appkey_list else ""

    # Identity values stay strings, including values larger than JavaScript's safe integers.
    for name, value in (("myself_jd_union_id", jd_union_id), ("myself_jd_position_id", jd_position_id)):
        if value and (not value.isascii() or not value.isdigit() or not value.lstrip("0")):
            raise SettingsError(f"{name} 必须是正整数数字字符串")
    auth_secret = text("web_auth_secret")
    mongo_user = text("local_mongo_user")
    mongo_password = text("local_mongo_password")
    mongo_host = text("local_mongo_host", "localhost").strip()
    mongo_db_name = text("local_mongo_db_name", "crawler_data").strip()
    mongo_auth_source = text("mongo_auth_source", "admin").strip()
    web_host = text("web_host", "0.0.0.0").strip()

    for name, value in (("local_mongo_host", mongo_host), ("local_mongo_db_name", mongo_db_name),
                        ("mongo_auth_source", mongo_auth_source), ("web_host", web_host)):
        if not value:
            raise SettingsError(f"{name} 不能为空")

    if bool(mongo_user) != bool(mongo_password):
        raise SettingsError("local_mongo_user 与 local_mongo_password 必须同时配置或同时留空")

    if require_credentials:
        required = []
        if pdd_enabled:
            required.extend((("myself_pdd_client_id", pdd_client_id),
                             ("myself_pdd_client_secret", pdd_client_secret), ("myself_pdd_pid", pdd_pid)))
        if taobao_enabled:
            required.extend((("myself_taobao_app_key", tb_app_key),
                             ("myself_taobao_app_secret", tb_app_secret),
                             ("taobao_adzone_id / myself_taobao_pid", tb_adzone_id)))
        if jd_enabled:
            # Catalog credentials do not imply promotion/order credentials are available.
            required.extend((("zhetaoke_appkey", ztk_appkey), ("myself_jd_union_id", jd_union_id),
                             ("jd_api_base", jd_api_base)))
        missing = [key for key, value in required if not value.strip()]
        if missing:
            raise SettingsError("缺少必需配置：" + ", ".join(missing))
        if not pdd_enabled and not taobao_enabled and not jd_enabled:
            raise SettingsError("至少需要启用一个联盟平台")
        if len(auth_secret.strip()) < 32 or auth_secret == LEGACY_AUTH_SECRET or auth_secret.lower().startswith("change-me"):
            raise SettingsError("web_auth_secret 必须是至少 32 字符的私有密钥；请在 config.local.json 中配置")

    log_dir = text("web_log_dir", "runtime/logs").strip()
    log_level = text("web_log_level", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise SettingsError("web_log_level 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL")

    dns_mode = text("pdd_dns_mode", "auto").strip().lower()
    if dns_mode not in {"auto", "on", "off"}:
        raise SettingsError("pdd_dns_mode 必须是 auto/on/off")

    web_port = _port(values.get("web_port", 8000), "web_port")
    mongo_port = _port(values.get("local_mongo_port", 27017), "local_mongo_port")
    warmup_enabled = _boolean(values.get("web_warmup_enabled", True), "web_warmup_enabled")
    trust_env = _boolean(values.get("pdd_trust_env", sys.platform != "win32"), "pdd_trust_env")
    detail_prefetch_enabled = _boolean(
        values.get("web_detail_prefetch_enabled", warmup_enabled), "web_detail_prefetch_enabled")
    interval = values.get("web_detail_prefetch_interval_seconds", 300)
    if isinstance(interval, bool) or not isinstance(interval, (str, int)):
        raise SettingsError("web_detail_prefetch_interval_seconds 必须是 60 到 300 的整数")
    try:
        detail_prefetch_interval_seconds = int(interval)
    except (TypeError, ValueError):
        raise SettingsError("web_detail_prefetch_interval_seconds 必须是 60 到 300 的整数") from None
    if not 60 <= detail_prefetch_interval_seconds <= 300:
        raise SettingsError("web_detail_prefetch_interval_seconds 必须是 60 到 300 的整数")

    return Settings(
        pdd_client_id=pdd_client_id, pdd_client_secret=pdd_client_secret, pdd_pid=pdd_pid,
        mongo_host=mongo_host, mongo_port=mongo_port,
        mongo_user=mongo_user, mongo_password=mongo_password, mongo_db_name=mongo_db_name,
        mongo_auth_source=mongo_auth_source, web_host=web_host,
        web_port=web_port,
        auth_secret=auth_secret,
        warmup_enabled=warmup_enabled,
        detail_prefetch_enabled=detail_prefetch_enabled,
        detail_prefetch_interval_seconds=detail_prefetch_interval_seconds,
        log_dir=None if log_dir in {"", "-"} else app_path(log_dir), log_level=log_level,
        pdd_dns_mode=dns_mode,
        pdd_trust_env=trust_env,
        pdd_enabled=pdd_enabled, taobao_enabled=taobao_enabled,
        tb_app_key=tb_app_key, tb_app_secret=tb_app_secret, tb_pid=tb_pid, tb_adzone_id=tb_adzone_id,
        tb_session=text("taobao_session").strip(), tb_inviter_code=text("taobao_inviter_code").strip(),
        hdk_api_key=text("haodanku_api_key").strip(),
        ztk_appkey=ztk_appkey, ztk_appkey_list=ztk_appkey_list, ztk_sid=text("zhetaoke_sid").strip(),
        jd_enabled=jd_enabled, jd_union_id=jd_union_id, jd_position_id=jd_position_id,
        jd_app_key=jd_app_key, jd_app_secret=jd_app_secret, jd_api_base=jd_api_base,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """每个进程使用同一份配置；修改文件后需要重启。"""
    return load_settings()
