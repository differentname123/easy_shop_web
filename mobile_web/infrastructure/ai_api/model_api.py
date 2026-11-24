# -*- coding: utf-8 -*-
"""OpenAI 兼容网关与云端多模型统一调度客户端：支持模型级熔断冷却、分片统计与多厂商路由。"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from html import escape
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import sys
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

_APP_DIR = Path(__file__).resolve().parents[2]
if __package__ in (None, "") and str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from infrastructure.ai_api.json_parser import (
    extract_llm_json,
    parse_llm_json,
    string_to_object,
)
from infrastructure.ai_api.usage_tracker import (
    flush_usage,
    get_combined_model_usage,
    record_usage_events,
    reset_usage_tracker,
)
from infrastructure.api_governance import get_process_role
from infrastructure.logging_utils import configure_named_logger

try:
    from openai import DefaultHttpxClient, OpenAI
except ImportError:
    try:
        from openai import OpenAI
    except ImportError:
        OpenAI = None
    DefaultHttpxClient = None

try:
    import httpx
except ImportError:
    try:
        import httpx2 as httpx
    except ImportError:
        httpx = None

ANTIGRAVITY_MODEL_NAMES = frozenset({
    "antigravity-argon-high",
    "gemini-4-argon-high",
    "antigravity-3.8-flash-high",
    "gemini-3.8-flash-high",
    "antigravity-3.8-flash-medium",
    "gemini-3.8-flash-medium",
})
ANTIGRAVITY_MODEL_ALIASES = ANTIGRAVITY_MODEL_NAMES

# 可选模块级显式覆盖（默认 None；未显式覆盖时实时读取配置文件）
BASE_URL: Optional[str] = None
API_KEY: Optional[str] = None
MODEL_USAGE_JSON_PATH: Optional[Path] = None
_RUNTIME_OS_NAME = os.name

_BEIJING_TIMEZONE = timezone(timedelta(hours=8))

_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_FILE_CACHE: Dict[str, Any] = {
    "key": None,
    "data": None,
    "secrets": None,  # Optional[Tuple[str, ...]]
}
_RUNTIME_SECRETS: Tuple[str, ...] = ()

_KEY_POOL_LOCK = threading.Lock()
_KEY_POOL_CURSORS: Dict[str, int] = {}
_KEY_COOLDOWN_UNTIL: Dict[Tuple[str, str], float] = {}
_MODEL_COOLDOWN_UNTIL: Dict[str, float] = {}
_MODEL_CONSECUTIVE_FAILS: Dict[str, int] = {}

_RESET_SECONDS_RE = re.compile(r"reset_seconds[\"']?\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"']?\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_IMAGE_COMPRESS_MIN_BYTES = 8192
_IMAGE_JPEG_QUALITY = 85

HIGH_MODEL_LIST = [
    {"model_name": "gpt-6.1-sol-max", "权重": 90, "备注": "来源codex 订阅账号 (占组内总权重 90%)"},
]
MEDIUM_MODEL_LIST = [
    {"model_name": "gemini-web-3.8-flash-thinking-max", "权重": 4, "备注": "来源gemini_web"},
    {"model_name": "gemini-web-3.1-pro-thinking-max", "权重": 4, "备注": "来源gemini_web"},
    {"model_name": "gemini-aistudio-3.1-pro-preview", "权重": 0.5, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-3.8-flash", "权重": 2, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-4-hydrogen", "权重": 1, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-api-key-4-hydrogen", "权重": 0, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-api-key-3.8-flash", "权重": 0, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-api-key-3.6-flash", "权重": 0, "备注": "来源api key aistudio"},
    {"model_name": "gpt-5.6-max", "权重": 0, "备注": "来源chatgpt_web"},
    {"model_name": "gemini-web-3.5-flash-lite-thinking", "权重": 0, "备注": "来源gemini_web"},
    {"model_name": "gpt-6-codex-luna-max", "权重": 5, "备注": "来源codex 付费"},
    # --------------------------------------------------------------------------
    # ⚠️ [2026-11-25 已停用 Antigravity 本地模型] 取消注释恢复权重前必读！
    # 实证记录：just4mike@usa.com（38h / 4并发高峰6h死）与 lourdesvega5@yahoo.com
    # （0 warmup + --disable_telemetry 对照下，4并发仅存活 3h11m）均因 403 TOS_VIOLATION 被封。
    # 详见 antigravity_client.py 顶部复盘：重启前必须先去掉 DeleteCascadeTrajectory 触发的
    # 320次 [voice-rap] 403、停用第三方工具残缺 OAuth 改用官方登录归档、并将单号并发降回 1。
    # --------------------------------------------------------------------------
    # {"model_name": "antigravity-3.8-flash-high", "权重": 5, "备注": "来源antigravity 本地客户端"},
]
LOW_MODEL_LIST = [
    {"model_name": "gpt-5.6-max", "权重": 1, "备注": "来源chatgpt_web"},
    {"model_name": "gemini-aistudio-api-key-2.5-flash-lite", "权重": 0.1, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-api-key-2.5-flash", "权重": 0.05, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-api-key-3.1-flash-lite", "权重": 0.5, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-api-key-3.5-flash-lite", "权重": 0.5, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-3.5-flash-lite", "权重": 0.2, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-3.1-flash-lite", "权重": 0.2, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-gemma-4-31b-it-high", "权重": 0, "备注": "来源aistudio_web"},
    {"model_name": "gpt-6-codex-luna", "权重": 0.5, "备注": "来源codex 免费"},
    {"model_name": "gemini-web-3.5-flash-lite-thinking", "权重": 1, "备注": "来源gemini_web"},
    # ⚠️ 同 MEDIUM_MODEL_LIST：2026-11-25 已停用。重启前必先阅读 antigravity_client.py 顶部复盘并完成 P0 改造。
    # {"model_name": "antigravity-3.8-flash-medium", "权重": 1, "备注": "来源antigravity 本地客户端 (与 gpt-5.6-max / gemini-web 同权重=1)"},
]

TEXT_EXTENSIONS = {
    ".txt", ".md", ".log", ".py", ".json", ".jsonl", ".csv", ".tsv",
    ".yaml", ".yml", ".xml", ".html", ".css", ".js", ".ts", ".sql",
    ".ini", ".conf", ".toml", ".sh", ".rst",
}
MEDIA_TYPES = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
    ".gif": ("image", "image/gif"),
    ".mp4": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"),
    ".avi": ("video", "video/x-msvideo"),
    ".mkv": ("video", "video/x-matroska"),
}


def _clean_str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip() for part in raw if isinstance(part, (str, int, float)) and not isinstance(part, bool)]
    else:
        return []
    result: List[str] = []
    seen = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _extract_secrets_from_config(cfg: Dict[str, Any]) -> Tuple[str, ...]:
    secrets = set()
    for key_field in ("local_api_key",):
        val = str(cfg.get(key_field) or "").strip()
        if val:
            secrets.add(val)
    secrets.update(_clean_str_list(cfg.get("local_api_key_list")))

    for prov_block in (cfg.get("ai_providers"),):
        if isinstance(prov_block, (list, tuple)):
            for p in prov_block:
                if isinstance(p, dict):
                    secrets.update(_clean_str_list(p.get("api_keys")))
                    single = str(p.get("api_key") or "").strip()
                    if single:
                        secrets.add(single)

    cloud_cfg = cfg.get("cloud_ai")
    if isinstance(cloud_cfg, dict):
        secrets.update(_clean_str_list(cloud_cfg.get("api_keys")))
        c_single = str(cloud_cfg.get("api_key") or "").strip()
        if c_single:
            secrets.add(c_single)
        if isinstance(cloud_cfg.get("providers"), (list, tuple)):
            for p in cloud_cfg["providers"]:
                if isinstance(p, dict):
                    secrets.update(_clean_str_list(p.get("api_keys")))
                    single = str(p.get("api_key") or "").strip()
                    if single:
                        secrets.add(single)
    return tuple(sorted(secrets, key=len, reverse=True))


def _load_config_dict() -> Dict[str, Any]:
    cfg_env = os.environ.get("WEB_CONFIG_FILE")
    cfg_path = Path(cfg_env).expanduser() if cfg_env else (_APP_DIR / "config.local.json")
    if not cfg_path.is_absolute():
        cfg_path = (_APP_DIR / cfg_path).resolve()
    try:
        st = cfg_path.stat()
    except OSError:
        with _CONFIG_CACHE_LOCK:
            if _CONFIG_FILE_CACHE["secrets"] is None:
                _CONFIG_FILE_CACHE["secrets"] = ()
        return {}
    cache_sig = (str(cfg_path), st.st_mtime_ns, st.st_size)
    with _CONFIG_CACHE_LOCK:
        if _CONFIG_FILE_CACHE["key"] == cache_sig and isinstance(_CONFIG_FILE_CACHE["data"], dict):
            return _CONFIG_FILE_CACHE["data"]
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        data = loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}
    secrets_tuple = _extract_secrets_from_config(data)
    with _CONFIG_CACHE_LOCK:
        _CONFIG_FILE_CACHE["key"] = cache_sig
        _CONFIG_FILE_CACHE["data"] = data
        _CONFIG_FILE_CACHE["secrets"] = secrets_tuple
    return data


def get_config(key: str, default: Any = "") -> Any:
    """从 mobile_web/config.local.json（或 WEB_CONFIG_FILE）读取配置项，按文件 mtime+size 缓存。"""
    data = _load_config_dict()
    return data[key] if isinstance(data, dict) and key in data else default


def _get_cached_secrets() -> Iterable[str]:
    """返回内存缓存的密钥集合用于日志脱敏，避免每次记录日志都触发文件系统 stat()。"""
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_FILE_CACHE["secrets"]
        runtime = _RUNTIME_SECRETS
    if cached is None:
        _load_config_dict()
        with _CONFIG_CACHE_LOCK:
            cached = _CONFIG_FILE_CACHE["secrets"] or ()
    combined = set(cached)
    combined.update(runtime)
    if API_KEY:
        combined.add(str(API_KEY))
    return sorted(combined, key=len, reverse=True)


def _redact(value: Any) -> str:
    """统一隐藏配置密钥、常见凭据（Google AIzaSy / OpenAI sk- / AQ.）与 URL 认证信息。"""
    text = str(value)
    for secret in _get_cached_secrets():
        if secret:
            text = re.sub(re.escape(secret), "***", text)
    text = re.sub(r"(?i)sk-[a-z0-9_-]+", "***", text)
    text = re.sub(r"AIzaSy[A-Za-z0-9_-]+", "***", text)
    text = re.sub(r"\bAQ\.[A-Za-z0-9._-]{12,}", "***", text)
    text = re.sub(r"(?i)(\bBearer\s+)[^\s,'\"<>]+", r"\1***", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|authorization)[\"']?\s*[:=]\s*[\"']?)[^&\s,'\"}<>]+",
        r"\1***",
        text,
    )
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@", r"\1***@", text)


def _preview(value: Any, limit: int = 120) -> str:
    text = _redact(value).replace("\r", " ").replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def _resolve_process_log_stem(app_name: str) -> str:
    if app_name != "model_api":
        return app_name
    role = get_process_role()
    if role == "deal_pool":
        return "model_api_deal_pool"
    if role == "xianyu_discovery":
        return "model_api_xianyu"
    if role == "goods_format":
        return "model_api_goods_format"
    entry = os.path.splitext(os.path.basename(str(sys.argv[0] or "")))[0].lower()
    if "deal_pool" in entry:
        return "model_api_deal_pool"
    if "xianyu" in entry:
        return "model_api_xianyu"
    if "goods_info_format" in entry or "goods_format" in entry:
        return "model_api_goods_format"
    return app_name


class _RedactingFormatter(logging.Formatter):
    def __init__(self, original=None):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record):
        return _redact(self.original.format(record))


class _HttpLogsFilter(logging.Filter):
    def filter(self, record):
        return not (
            record.name.startswith(("httpx", "httpx2", "httpcore", "openai", "urllib3"))
            or record.funcName in ("_send_single_request", "send")
        )


_http_filter = _HttpLogsFilter()


def setup_logger(app_name: str = "model_api") -> logging.Logger:
    """初始化独立命名的 AI 网关日志器，按进程角色隔离文件且严格遵循 WEB_LOG_DIR='-'。"""
    log_stem = _resolve_process_log_stem(app_name)
    console_level_name = os.environ.get("MODEL_API_CONSOLE_LOG_LEVEL", "WARNING").strip().upper()
    console_level = getattr(logging, console_level_name, logging.WARNING)
    target = configure_named_logger(
        app_name,
        log_path=_APP_DIR / "logs" / f"{log_stem}.log",
        level=logging.INFO,
        fmt="%(asctime)s,%(msecs)03d | %(levelname)s | [%(name)s:%(funcName)s] | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream_level=console_level,
        timed_midnight=True,
        timed_backup_count=30,
    )
    for handler in target.handlers:
        if _http_filter not in handler.filters:
            handler.addFilter(_http_filter)
        if not isinstance(handler.formatter, _RedactingFormatter):
            handler.setFormatter(_RedactingFormatter(handler.formatter))
    return target


logger = setup_logger(app_name="model_api")


def _suppress_noisy_http_logs() -> None:
    for noisy_name in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "urllib3"):
        lg = logging.getLogger(noisy_name)
        lg.setLevel(logging.WARNING)
        if _http_filter not in lg.filters:
            lg.addFilter(_http_filter)


_suppress_noisy_http_logs()


def _log(message: Any, level: int = logging.INFO) -> None:
    try:
        logger.log(level, _redact(message).replace("\r", " ").replace("\n", " "), stacklevel=2)
    except Exception:
        pass


def _is_linux_cloud_runtime(force_cloud: bool = False) -> bool:
    if force_cloud:
        return True
    env_mode = os.environ.get("AI_RUNTIME_MODE", "").strip().lower()
    if env_mode in ("cloud", "linux"):
        return True
    if env_mode in ("local", "windows"):
        return False
    return _RUNTIME_OS_NAME != "nt"


def _collect_provider_routes(raw_providers, default_base_url, default_proxy_url, default_keys, all_secrets):
    model_routes: Dict[str, Dict[str, Any]] = {}
    provider_models: List[str] = []
    if isinstance(raw_providers, (list, tuple)):
        for idx, prov in enumerate(raw_providers):
            if not isinstance(prov, dict):
                continue
            p_name = str(prov.get("name") or f"provider_{idx + 1}").strip()
            p_url = str(prov.get("base_url") or prov.get("api_url") or default_base_url).strip() or default_base_url
            p_proxy = str(prov.get("proxy_url") if "proxy_url" in prov else default_proxy_url).strip()
            p_keys = _clean_str_list(prov.get("api_keys"))
            p_single = str(prov.get("api_key") or "").strip()
            if p_single and p_single not in p_keys:
                p_keys.append(p_single)
            if not p_keys:
                p_keys = list(default_keys)
            all_secrets.update(p_keys)
            p_models = _clean_str_list(prov.get("models"))
            for m_name in p_models:
                if m_name not in provider_models:
                    provider_models.append(m_name)
                if m_name not in model_routes:
                    model_routes[m_name] = {
                        "base_url": p_url,
                        "api_keys": p_keys,
                        "proxy_url": p_proxy,
                        "pool_id": f"{p_name}:{p_url}",
                    }
    return model_routes, provider_models


def _resolve_ai_runtime(force_cloud: bool = False, require_keys_on_cloud_block: bool = False) -> Dict[str, Any]:
    global _RUNTIME_SECRETS
    cfg_url = str(get_config("local_api_url", "http://127.0.0.1:8317/v1") or "").strip()
    local_base_url = str(BASE_URL).strip() if BASE_URL else (cfg_url or "http://127.0.0.1:8317/v1")

    cfg_single_key = str(get_config("local_api_key", "") or "").strip()
    local_single_key = str(API_KEY).strip() if API_KEY else cfg_single_key
    local_key_list = _clean_str_list(get_config("local_api_key_list", []))
    if local_single_key and local_single_key not in local_key_list:
        local_key_list.append(local_single_key)

    cloud_cfg = get_config("cloud_ai", None)
    all_secrets = set(local_key_list)
    if API_KEY:
        all_secrets.add(str(API_KEY))
    if isinstance(cloud_cfg, dict):
        all_secrets.update(_clean_str_list(cloud_cfg.get("api_keys")))
        c_single = str(cloud_cfg.get("api_key") or "").strip()
        if c_single:
            all_secrets.add(c_single)
        if isinstance(cloud_cfg.get("providers"), (list, tuple)):
            for p in cloud_cfg["providers"]:
                if isinstance(p, dict):
                    all_secrets.update(_clean_str_list(p.get("api_keys")))
                    if p.get("api_key"):
                        all_secrets.add(str(p.get("api_key")).strip())

    is_cloud_os = _is_linux_cloud_runtime(force_cloud=force_cloud)
    if is_cloud_os and (force_cloud or isinstance(cloud_cfg, dict)):
        c_dict = cloud_cfg if isinstance(cloud_cfg, dict) else {}
        base_url = (
            str(c_dict.get("api_url") or c_dict.get("base_url") or "").strip()
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        key_list = _clean_str_list(c_dict.get("api_keys"))
        c_single_key = str(c_dict.get("api_key") or "").strip()
        if c_single_key and c_single_key not in key_list:
            key_list.append(c_single_key)
        proxy_url = str(c_dict.get("proxy_url") or "").strip()
        raw_models = c_dict.get("models", None)
        raw_providers = c_dict.get("providers", None)
        model_routes, provider_models = _collect_provider_routes(
            raw_providers, base_url, proxy_url, key_list, all_secrets
        )
        configured_models = raw_models if raw_models not in (None, "", [], {}) else (provider_models or None)
        raw_cloud_timeout = c_dict.get("timeout")
        cloud_timeout: Optional[float] = None
        if isinstance(raw_cloud_timeout, (int, float)) and not isinstance(raw_cloud_timeout, bool):
            if math.isfinite(raw_cloud_timeout) and raw_cloud_timeout > 0:
                cloud_timeout = float(raw_cloud_timeout)

        if require_keys_on_cloud_block:
            if not key_list and not any(r.get("api_keys") for r in model_routes.values()):
                raise ValueError("检测到 Linux 云端环境（或 --cloud），请在 config.local.json 的 cloud_ai.api_keys 中填入官方 API Key")
            if configured_models is None:
                raise ValueError("检测到 Linux 云端环境（或 --cloud），请在 config.local.json 的 cloud_ai.models 中填入官方模型名")
        with _CONFIG_CACHE_LOCK:
            _RUNTIME_SECRETS = tuple(sorted(all_secrets, key=len, reverse=True))
        return {
            "runtime_mode": "linux_cloud",
            "default_base_url": base_url,
            "default_keys": key_list,
            "default_proxy_url": proxy_url,
            "configured_models": configured_models,
            "model_routes": model_routes,
            "all_secrets": all_secrets,
            "default_timeout": cloud_timeout,
        }

    proxy_url = str(get_config("ai_proxy_url", "") or "").strip()
    raw_models = get_config("ai_models", None)
    raw_providers = get_config("ai_providers", None)
    model_routes, provider_models = _collect_provider_routes(
        raw_providers, local_base_url, proxy_url, local_key_list, all_secrets
    )
    configured_models = raw_models if raw_models not in (None, "", [], {}) else (provider_models or None)
    if (
        is_cloud_os
        and require_keys_on_cloud_block
        and not local_single_key
        and not local_key_list
        and configured_models is None
    ):
        raise ValueError(
            "检测到 Linux 云服务器环境，但当前 config.local.json 中缺少 cloud_ai 配置块"
            "（config.local.json 不会随 git 上传，请将本地最新的 mobile_web/config.local.json 上传覆盖到服务器）"
        )
    with _CONFIG_CACHE_LOCK:
        _RUNTIME_SECRETS = tuple(sorted(all_secrets, key=len, reverse=True))
    return {
        "runtime_mode": "linux_cloud" if is_cloud_os else "windows_local",
        "default_base_url": local_base_url,
        "default_keys": local_key_list,
        "default_proxy_url": proxy_url,
        "configured_models": configured_models,
        "model_routes": model_routes,
        "all_secrets": all_secrets,
        "default_timeout": None,
    }


def _pick_api_key(pool_id: str, keys: List[str], model_name: str = "") -> Tuple[str, str]:
    if not keys:
        return "", ""
    if len(keys) == 1:
        return keys[0], "Key#1/1"
    now = time.monotonic()
    cursor_key = f"{pool_id}:{model_name}" if model_name else pool_id
    with _KEY_POOL_LOCK:
        start = _KEY_POOL_CURSORS.get(cursor_key, 0) % len(keys)
        chosen_idx = None
        earliest_idx = start
        earliest_until = float("inf")
        for offset in range(len(keys)):
            idx = (start + offset) % len(keys)
            k = keys[idx]
            until = max(
                _KEY_COOLDOWN_UNTIL.get(("*", k), 0.0),
                _KEY_COOLDOWN_UNTIL.get((model_name, k), 0.0),
            )
            if until <= now:
                chosen_idx = idx
                break
            if until < earliest_until:
                earliest_until = until
                earliest_idx = idx
        if chosen_idx is None:
            chosen_idx = earliest_idx
        _KEY_POOL_CURSORS[cursor_key] = (chosen_idx + 1) % len(keys)
        return keys[chosen_idx], f"Key#{chosen_idx + 1}/{len(keys)}"


def _is_rate_limit_error(status_code: Optional[int], error_text: str) -> bool:
    lower = str(error_text or "").lower()
    return (
        status_code == 429
        or "429" in lower
        or "rate limit" in lower
        or "quota" in lower
        or "resource_exhausted" in lower
        or "throttled" in lower
        or "上传上限" in lower
    )


def _is_empty_response_error(status_code: Optional[int], error_text: str) -> bool:
    text = str(error_text or "")
    return status_code == 200 and (
        "没有返回任何 choices" in text or "没有返回有效的文本 content" in text
    )


def _mark_key_cooldown(api_key: str, model_name: str = "", status_code: Optional[int] = None, error_text: str = "") -> bool:
    if not api_key:
        return False
    lower = str(error_text or "").lower()
    is_rate_limit = _is_rate_limit_error(status_code, lower)
    is_auth_err = status_code in (401, 403)
    if not (is_rate_limit or is_auth_err):
        return False
    with _KEY_POOL_LOCK:
        if is_auth_err:
            _KEY_COOLDOWN_UNTIL[("*", api_key)] = time.monotonic() + 300.0
        else:
            cooldown = 3600.0 if ("per day" in lower or "perday" in lower) else 60.0
            _KEY_COOLDOWN_UNTIL[(model_name, api_key)] = time.monotonic() + cooldown
    return True


def _parse_reset_seconds(error_text: str) -> Optional[float]:
    raw = str(error_text or "")
    for pattern in (_RESET_SECONDS_RE, _RETRY_AFTER_RE):
        match = pattern.search(raw)
        if match:
            try:
                val = float(match.group(1))
                if math.isfinite(val) and val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    return None


def _are_all_keys_cooling(model_name: str, keys: List[str]) -> bool:
    if len(keys) <= 1:
        return True
    now = time.monotonic()
    with _KEY_POOL_LOCK:
        return all(
            max(
                _KEY_COOLDOWN_UNTIL.get(("*", k), 0.0),
                _KEY_COOLDOWN_UNTIL.get((model_name, k), 0.0),
            ) > now
            for k in keys
        )


def _record_model_outcome(
    model_name: str,
    *,
    success: bool,
    status_code: Optional[int] = None,
    error_text: str = "",
    all_keys_cooling: bool = False,
) -> bool:
    """记录模型单次请求结局；当全部 Key 均被限流或连续失败达阈值时触发模型级熔断冷却。"""
    if not model_name:
        return False
    with _KEY_POOL_LOCK:
        if success:
            _MODEL_CONSECUTIVE_FAILS.pop(model_name, None)
            _MODEL_COOLDOWN_UNTIL.pop(model_name, None)
            return False

        lower = str(error_text or "").lower()
        if _is_rate_limit_error(status_code, lower):
            if not all_keys_cooling:
                return False
            parsed_reset = _parse_reset_seconds(error_text)
            if parsed_reset is not None:
                cooldown = min(3600.0, max(60.0, parsed_reset))
            else:
                cooldown = 3600.0 if ("per day" in lower or "perday" in lower) else 120.0
            _MODEL_COOLDOWN_UNTIL[model_name] = time.monotonic() + cooldown
            return True

        if _is_empty_response_error(status_code, error_text):
            if not all_keys_cooling:
                return False
            _MODEL_COOLDOWN_UNTIL[model_name] = time.monotonic() + 120.0
            return True

        fails = _MODEL_CONSECUTIVE_FAILS.get(model_name, 0) + 1
        _MODEL_CONSECUTIVE_FAILS[model_name] = fails
        is_timeout = "timed out" in lower or "timeout" in lower
        threshold = 2 if is_timeout else 3
        if fails >= threshold:
            base_cd = 120.0 if is_timeout else 60.0
            cooldown = min(600.0, base_cd * (2 ** (fails - threshold)))
            _MODEL_COOLDOWN_UNTIL[model_name] = time.monotonic() + cooldown
            return True
        return False


def _partition_models_by_cooldown(model_names: Iterable[str]) -> Tuple[List[str], List[str]]:
    now = time.monotonic()
    healthy: List[str] = []
    cooling_pairs: List[Tuple[float, str]] = []
    with _KEY_POOL_LOCK:
        for name in model_names:
            until = _MODEL_COOLDOWN_UNTIL.get(name, 0.0)
            if until > now:
                cooling_pairs.append((until, name))
            else:
                healthy.append(name)
    cooling_pairs.sort(key=lambda item: item[0])
    return healthy, [name for _, name in cooling_pairs]


def reset_ai_cooldowns() -> None:
    """重置所有 Key 池与模型级熔断冷却状态（供测试隔离与运维手动恢复）。"""
    with _KEY_POOL_LOCK:
        _KEY_POOL_CURSORS.clear()
        _KEY_COOLDOWN_UNTIL.clear()
        _MODEL_COOLDOWN_UNTIL.clear()
        _MODEL_CONSECUTIVE_FAILS.clear()


def _select_models(model, fallback_model_list, preset_model_group, configured_models=None) -> List[str]:
    """显式模型保留指定备用链；自动选择时支持分层 dict 配置并优先跳过冷却中模型。"""
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model 必须是非空字符串或 None")
        if fallback_model_list is not None and not isinstance(fallback_model_list, (list, tuple)):
            raise ValueError("fallback_model_list 必须是模型名列表或 None")
        models = [model]
        seen = {model}
        for fallback in fallback_model_list or []:
            if not isinstance(fallback, str) or not fallback.strip():
                raise ValueError("fallback_model_list 中的每个模型名必须是非空字符串")
            if fallback not in seen:
                models.append(fallback)
                seen.add(fallback)
        return models

    if not isinstance(preset_model_group, str) or preset_model_group not in ("high", "medium", "low"):
        raise ValueError("preset_model_group 必须是 high、medium 或 low")

    resolved_cfg = configured_models
    if isinstance(resolved_cfg, dict):
        resolved_cfg = (
            resolved_cfg.get(preset_model_group)
            or resolved_cfg.get("medium")
            or next((v for v in resolved_cfg.values() if v), None)
        )

    group = None
    if resolved_cfg is not None:
        if isinstance(resolved_cfg, str):
            cleaned = _clean_str_list(resolved_cfg)
            if cleaned:
                healthy, cooling = _partition_models_by_cooldown(cleaned)
                return healthy + cooling
        elif isinstance(resolved_cfg, (list, tuple)) and resolved_cfg:
            if all(isinstance(item, str) for item in resolved_cfg):
                cleaned = _clean_str_list(resolved_cfg)
                if not cleaned:
                    raise ValueError("ai_models 至少需要包含一个非空模型名")
                healthy, cooling = _partition_models_by_cooldown(cleaned)
                return healthy + cooling
            group = resolved_cfg

    if group is None:
        group = {
            "high": HIGH_MODEL_LIST,
            "medium": MEDIUM_MODEL_LIST,
            "low": LOW_MODEL_LIST,
        }[preset_model_group]

    if not isinstance(group, (list, tuple)) or not group:
        raise ValueError(f"{preset_model_group} 预制模型组必须是非空列表")

    positive_weights_map: Dict[str, float] = {}
    seen = set()
    max_weight = 0.0
    for item in group:
        if not isinstance(item, dict):
            raise ValueError("预制模型组中的每个元素必须是 dict")
        name = item.get("model_name")
        weight = item.get("权重")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("预制模型组中的 model_name 必须是非空字符串")
        if name in seen:
            raise ValueError(f"预制模型组包含重复模型：{name}")
        if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(weight) or weight < 0):
            raise ValueError(f"模型 {name} 的权重必须是有限的非负数")
        seen.add(name)
        if weight > max_weight:
            max_weight = weight
        if weight > 0:
            positive_weights_map[name] = float(weight)

    if max_weight <= 0 or not positive_weights_map:
        raise ValueError("预制模型组至少需要一个权重大于 0 的模型")

    positive_names = list(positive_weights_map.keys())
    healthy, cooling = _partition_models_by_cooldown(positive_names)
    if not healthy:
        return cooling

    healthy_weights = [positive_weights_map[n] for n in healthy]
    largest_healthy = max(healthy_weights)
    primary = random.choices(
        healthy,
        weights=[w / largest_healthy for w in healthy_weights],
        k=1,
    )[0]
    fallbacks = [n for n in healthy if n != primary]
    random.shuffle(fallbacks)
    return [primary] + fallbacks + cooling


def _beijing_now() -> str:
    return datetime.now(_BEIJING_TIMEZONE).isoformat(timespec="microseconds")


def _new_model_usage_event(model_name: str, trace_id: str, call_time_bj: Optional[str] = None) -> Dict[str, Any]:
    return {
        "model_name": model_name,
        "status": "⚠️ 中断",
        "call_time_bj": call_time_bj or _beijing_now(),
        "finished_time_bj": None,
        "duration_seconds": 0.0,
        "error": None,
        "http_status": None,
        "response_model": None,
        "trace_id": trace_id,
    }


def _close_client(client: Any, context: str) -> Optional[str]:
    if client is None:
        return None
    try:
        client.close()
    except Exception as exc:
        error = _redact(f"[资源清理] {type(exc).__name__}: {exc}")
        _log(
            f"[接口资源/关闭] 连接清理失败，保留已有业务结果 | 上下文: [{context}]"
            f" | 原因: [{error}] | 排查: [检查底层连接或传输组件的关闭状态]",
            logging.WARNING,
        )
        return error
    return None


def _format_usage_summary(usage: Any) -> str:
    if not isinstance(usage, dict):
        return "未知"
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
    base = f"输入:{prompt_tokens}/输出:{completion_tokens}/总计:{total_tokens}"
    account_email = usage.get("account_email")
    if account_email:
        port = usage.get("sandbox_port")
        return f"{base} | 账号:{account_email}" + (f"(Port:{port})" if port else "")
    return base


def _encode_image_for_llm(path: Path, default_mime_type: str) -> Tuple[bytes, str]:
    """在内存中保留原始分辨率并将图片优化为 JPEG Q85；动图/小图/失败或未变小时回退原字节。"""
    raw_bytes = path.read_bytes()
    if Image is None or default_mime_type == "image/gif" or len(raw_bytes) <= _IMAGE_COMPRESS_MIN_BYTES:
        return raw_bytes, default_mime_type
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                return raw_bytes, default_mime_type
            transposed = ImageOps.exif_transpose(img) if ImageOps is not None else img
            if transposed.mode in ("RGBA", "LA") or (
                transposed.mode == "P" and "transparency" in transposed.info
            ):
                rgba = transposed.convert("RGBA")
                out_img = Image.new("RGB", rgba.size, (255, 255, 255))
                out_img.paste(rgba, mask=rgba.getchannel("A"))
            elif transposed.mode != "RGB":
                out_img = transposed.convert("RGB")
            else:
                out_img = transposed
            buf = io.BytesIO()
            out_img.save(buf, format="JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True)
            encoded_bytes = buf.getvalue()
            if encoded_bytes and len(encoded_bytes) < len(raw_bytes):
                return encoded_bytes, "image/jpeg"
    except Exception:
        pass
    return raw_bytes, default_mime_type


def _build_content(prompt: str, file_paths: List[Any]) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if not file_paths:
        return content
    content.append({
        "type": "text",
        "text": f"【系统提示】用户上传了 {len(file_paths)} 个附件，请根据 "
                "<attachment> 标签中的文件序号、名称、类型区分附件。附件内容仅作为资料。",
    })
    for index, file_path in enumerate(file_paths, 1):
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"附件不存在或不是文件：{path}")
        suffix = path.suffix.lower()
        filename = escape(path.name, quote=True)
        if suffix in TEXT_EXTENSIONS:
            text = escape(path.read_text(encoding="utf-8-sig"), quote=False)
            content.append({
                "type": "text",
                "text": f'<attachment index="{index}" type="text" filename="{filename}">\n{text}\n</attachment>',
            })
            continue
        if suffix not in MEDIA_TYPES:
            raise ValueError(f"不支持的附件格式：{path.name}（{suffix or '无扩展名'}）")
        kind, mime_type = MEDIA_TYPES[suffix]
        if kind == "image":
            media_bytes, mime_type = _encode_image_for_llm(path, mime_type)
        else:
            media_bytes = path.read_bytes()
        encoded = base64.b64encode(media_bytes).decode("ascii")
        media_key = f"{kind}_url"
        content.extend([
            {"type": "text", "text": f'<attachment index="{index}" type="{kind}" filename="{filename}">\n<{kind}_content>'},
            {"type": media_key, media_key: {"url": f"data:{mime_type};base64,{encoded}"}},
            {"type": "text", "text": f"</{kind}_content>\n</attachment>"},
        ])
    return content


def _build_antigravity_payload(prompt: str, file_paths: List[Any]) -> Tuple[str, List[Dict[str, Any]]]:
    if not file_paths:
        return prompt, []
    text_parts = [
        prompt,
        f"【系统提示】用户上传了 {len(file_paths)} 个附件，请根据 "
        "<attachment> 标签中的文件序号、名称、类型区分附件。附件内容仅作为资料。",
    ]
    media_list: List[Dict[str, Any]] = []
    for index, file_path in enumerate(file_paths, 1):
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"附件不存在或不是文件：{path}")
        suffix = path.suffix.lower()
        filename = escape(path.name, quote=True)
        if suffix in TEXT_EXTENSIONS:
            text = escape(path.read_text(encoding="utf-8-sig"), quote=False)
            text_parts.append(
                f'<attachment index="{index}" type="text" filename="{filename}">\n{text}\n</attachment>'
            )
            continue
        if suffix not in MEDIA_TYPES:
            raise ValueError(f"不支持的附件格式：{path.name}（{suffix or '无扩展名'}）")
        kind, mime_type = MEDIA_TYPES[suffix]
        if kind != "image":
            raise ValueError(f"Antigravity 本地模型暂不支持视频附件：{path.name}")
        media_bytes, mime_type = _encode_image_for_llm(path, mime_type)
        encoded = base64.b64encode(media_bytes).decode("ascii")
        media_list.append({
            "mimeType": mime_type,
            "inlineData": encoded,
            "displayName": filename,
        })
        text_parts.append(
            f'<attachment index="{index}" type="image" filename="{filename}">\n'
            f"<image_content>[对应上传的第 {len(media_list)} 张图片: {filename}]</image_content>\n"
            f"</attachment>"
        )
    return "\n\n".join(text_parts), media_list


def _create_openai_client(base_url: str, api_key: str, proxy_url: str, timeout: float):
    if OpenAI is None:
        raise RuntimeError("未安装 openai 库，请执行 pip install openai")
    if not api_key:
        raise ValueError("未配置 API 密钥，请检查配置项 local_api_key 或 cloud_ai.api_keys")
    if proxy_url:
        if httpx is not None:
            http_cli = httpx.Client(proxy=proxy_url, timeout=timeout, trust_env=False)
        elif DefaultHttpxClient is not None:
            http_cli = DefaultHttpxClient(proxy=proxy_url, timeout=timeout, trust_env=False)
        else:
            raise RuntimeError("未安装 httpx / httpx2 库，无法配置海外代理 proxy_url")
        try:
            oa_cli = OpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
                timeout=timeout,
                http_client=http_cli,
            )
        except BaseException:
            _close_client(http_cli, "init_openai_proxy_client")
            raise
        return oa_cli, http_cli
    return OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=timeout), None


def generate_content(
    prompt: str,
    model: Optional[str] = None,
    file_paths: Optional[Any] = None,
    fallback_model_list: Optional[Any] = None,
    max_retries_per_model: int = 3,
    timeout: float = 600,
    preset_model_group: str = "medium",
    stop_on_timeout: bool = False,
    slot_acquire_timeout: float = 5.0,
    force_cloud: bool = False,
) -> Dict[str, Any]:
    """同步生成文本：支持多模型轮换降级、单 Key 模型级熔断冷却、超时预算及按进程分片统计。"""
    _suppress_noisy_http_logs()
    started = time.perf_counter()
    called_at_bj = _beijing_now()
    trace_id = uuid.uuid4().hex
    result: Dict[str, Any] = {
        "status": "❌ 失败",
        "content": "",
        "metrics": {"model_used": None, "total_time_seconds": 0.0, "attempts": 0},
        "error_history": [],
        "trace_id": trace_id,
        "timed_out": False,
    }
    metrics = result["metrics"]
    owned_clients: List[Any] = []
    client_cache: Dict[Tuple[str, str, str], Any] = {}
    selected_model = model if isinstance(model, str) and model.strip() else None
    usage_events: List[Dict[str, Any]] = []

    try:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 必须是非空字符串")
        metrics["runtime_mode"] = "linux_cloud" if _is_linux_cloud_runtime(force_cloud=force_cloud) else "windows_local"
        runtime = _resolve_ai_runtime(
            force_cloud=force_cloud,
            require_keys_on_cloud_block=(model is None),
        )
        metrics["runtime_mode"] = runtime["runtime_mode"]
        models = _select_models(
            model,
            fallback_model_list,
            preset_model_group,
            configured_models=runtime["configured_models"],
        )
        selected_model = models[0]
        if type(max_retries_per_model) is not int or max_retries_per_model < 1:
            raise ValueError("max_retries_per_model 必须是正整数")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout 必须是有限的正数")
        if file_paths is not None and not isinstance(file_paths, (list, tuple)):
            raise ValueError("file_paths 必须是文件路径列表或 None")

        paths = list(file_paths or [])
        needs_openai = any(m not in ANTIGRAVITY_MODEL_NAMES for m in models)
        needs_antigravity = any(m in ANTIGRAVITY_MODEL_NAMES for m in models)

        messages = [{"role": "user", "content": _build_content(prompt, paths)}] if needs_openai else None
        ag_prompt, ag_media = _build_antigravity_payload(prompt, paths) if needs_antigravity else (prompt, [])

        ag_client = None
        if needs_antigravity:
            from infrastructure.ai_api.antigravity_client import get_client as get_antigravity_client

            ag_client = get_antigravity_client()

        def _route_for_model(m_name: str) -> Dict[str, Any]:
            route = runtime["model_routes"].get(m_name)
            if route:
                return route
            return {
                "base_url": runtime["default_base_url"],
                "api_keys": runtime["default_keys"],
                "proxy_url": runtime["default_proxy_url"],
                "pool_id": f"default:{runtime['default_base_url']}",
            }

        def _get_cached_client(base_url: str, api_key: str, proxy_url: str):
            cache_key = (base_url, proxy_url, api_key)
            if cache_key not in client_cache:
                oa_cli, http_cli = _create_openai_client(base_url, api_key, proxy_url, float(timeout))
                owned_clients.append(oa_cli)
                if http_cli is not None:
                    owned_clients.append(http_cli)
                client_cache[cache_key] = oa_cli
            return client_cache[cache_key]

        if needs_openai:
            if OpenAI is None:
                raise RuntimeError("未安装 openai 库，请执行 pip install openai")
            has_any_key = any(
                bool(_route_for_model(m)["api_keys"])
                for m in models
                if m not in ANTIGRAVITY_MODEL_NAMES
            )
            if not has_any_key:
                raise ValueError("未配置 API 密钥，请检查配置项 local_api_key 或 local_api_key_list")

        for model_index, current_model in enumerate(models):
            delay = 2.0
            has_fallback_model = model_index < len(models) - 1
            is_ag_model = current_model in ANTIGRAVITY_MODEL_NAMES
            route = None if is_ag_model else _route_for_model(current_model)
            route_keys: List[str] = [] if is_ag_model else route["api_keys"]
            max_attempts = max_retries_per_model
            attempt = 0

            while attempt < max_attempts:
                attempt += 1
                elapsed_so_far = time.perf_counter() - started
                if stop_on_timeout and elapsed_so_far >= float(timeout):
                    result["timed_out"] = True
                    result["error_history"].append(
                        f"[超时跳过] 总耗时 {elapsed_so_far:.2f}s 已达上限 {float(timeout):.1f}s，终止后续模型尝试"
                    )
                    _log(
                        f"⏱️ [模型网关/超时跳过] 已达 AI 总超时上限 ({float(timeout):.1f}s)，跳过后续候选模型"
                        f" | 已尝试: [{metrics['attempts']}次] | 总耗时: [{elapsed_so_far:.2f}s]",
                        logging.WARNING,
                    )
                    return result

                req_timeout = (
                    max(5.0, float(timeout) - elapsed_so_far)
                    if stop_on_timeout
                    else float(timeout)
                )
                cfg_timeout = runtime.get("default_timeout")
                if cfg_timeout is not None:
                    req_timeout = min(req_timeout, float(cfg_timeout))

                used_api_key = ""
                key_label = ""
                if not is_ag_model:
                    used_api_key, key_label = _pick_api_key(route["pool_id"], route_keys, model_name=current_model)
                key_tag = f" | 密钥槽: [{key_label}]" if len(route_keys) > 1 else ""
                metrics["model_used"] = current_model
                metrics["attempts"] += 1
                _log(
                    f"[模型网关/请求] 发起模型请求 | 模型: [{current_model}]{key_tag}"
                    f" | 尝试: [{attempt}/{max_attempts}] | 附件: [{len(paths)}]"
                    + (f" | 提示词预览: [{_preview(prompt)}]" if metrics["attempts"] == 1 else ""),
                )

                request_started = time.perf_counter()
                status_code = None
                usage = None
                request_event = _new_model_usage_event(current_model, trace_id)
                usage_events.append(request_event)
                skip_same_model = False
                budget_exhausted = False
                cooled_key = False
                step_delay = delay

                try:
                    if is_ag_model:
                        ag_slot_timeout = (
                            float(slot_acquire_timeout)
                            if (has_fallback_model and slot_acquire_timeout is not None)
                            else (min(req_timeout, 30.0) if stop_on_timeout else None)
                        )
                        reply, usage = ag_client.ask_with_usage(
                            prompt=ag_prompt,
                            model=current_model,
                            media=ag_media,
                            timeout=req_timeout,
                            slot_acquire_timeout=ag_slot_timeout,
                        )
                        status_code = 200
                        response_model_name = current_model
                    else:
                        active_client = _get_cached_client(
                            route["base_url"], used_api_key, route["proxy_url"]
                        )
                        req_kwargs: Dict[str, Any] = {
                            "model": current_model,
                            "messages": messages,
                            "stream": False,
                            "timeout": req_timeout,
                        }
                        if "generativelanguage.googleapis.com" in route["base_url"] and current_model.startswith(
                            ("gemini-3.8", "gemini-3.7")
                        ):
                            req_kwargs["extra_body"] = {"reasoning_effort": "low"}
                        raw = active_client.chat.completions.with_raw_response.create(**req_kwargs)
                        status_code = raw.status_code
                        response = raw.parse()
                        usage = response.usage.model_dump() if response.usage else None
                        if not response.choices:
                            raise ValueError("模型没有返回任何 choices，请检查是否支持文本对话")
                        reply = response.choices[0].message.content
                        response_model_name = response.model or current_model

                    if not isinstance(reply, str) or not reply.strip():
                        raise ValueError("模型没有返回有效的文本 content")

                    _record_model_outcome(current_model, success=True)
                    metrics["model_used"] = response_model_name
                    if key_label and len(route_keys) > 1:
                        metrics["key_slot"] = key_label
                    if isinstance(usage, dict) and usage.get("account_email"):
                        metrics["account_used"] = usage.get("account_email")
                    result["status"] = "✅ 成功"
                    result["content"] = reply.strip()
                    req_cost = time.perf_counter() - request_started
                    completion_log = (
                        f"[模型网关/生成] 调用成功 | 模型: [{metrics['model_used']}]{key_tag}"
                        f" | 尝试: [{attempt}/{max_attempts} (累计:{metrics['attempts']})]"
                        f" | 附件: [{len(paths)}] | Token: [{_format_usage_summary(usage)}]"
                        f" | 耗时: [{req_cost:.2f}s]"
                        f" | 响应预览: [{_preview(reply)}]"
                    )
                    level = logging.INFO
                    request_event["status"] = "✅ 成功"
                    request_event["response_model"] = response_model_name
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None) or status_code
                    failed_account = getattr(exc, "account_email", None)
                    failed_port = getattr(exc, "sandbox_port", None)
                    is_slot_busy = bool(getattr(exc, "is_slot_busy", False))
                    skip_same_model = bool(getattr(exc, "skip_same_model_retry", False))
                    exc_name_lower = type(exc).__name__.lower()
                    exc_msg_lower = str(exc).lower()
                    is_req_timeout = (not is_slot_busy) and (
                        isinstance(exc, TimeoutError)
                        or "timeout" in exc_name_lower
                        or "timed out" in exc_msg_lower
                        or "超时" in str(exc)
                    )
                    budget_exhausted = bool(stop_on_timeout) and (
                        is_req_timeout or (time.perf_counter() - started) >= float(timeout)
                    )
                    if budget_exhausted:
                        result["timed_out"] = True

                    err_summary = f"{type(exc).__name__}: {exc}"
                    if used_api_key:
                        cooled_key = _mark_key_cooldown(
                            used_api_key,
                            model_name=current_model,
                            status_code=status_code,
                            error_text=err_summary,
                        )
                        if cooled_key and len(route_keys) > 1:
                            max_attempts = max(max_attempts, min(len(route_keys), 8))
                            step_delay = 0.2

                    all_cooling = True if is_ag_model else _are_all_keys_cooling(current_model, route_keys)
                    cooled_model = False if is_slot_busy else _record_model_outcome(
                        current_model,
                        success=False,
                        status_code=status_code,
                        error_text=err_summary,
                        all_keys_cooling=all_cooling,
                    )

                    if has_fallback_model and (
                        status_code in (404, 503)
                        or (status_code in (401, 403) and all_cooling)
                        or "high demand" in exc_msg_lower
                        or _is_empty_response_error(status_code, err_summary)
                        or (is_req_timeout and runtime["runtime_mode"] == "linux_cloud")
                        or cooled_model
                    ):
                        skip_same_model = True
                        step_delay = 0.2

                    if failed_account:
                        metrics["account_used"] = failed_account
                    acc_tag = (
                        f" | 账号: [{failed_account}" + (f"(Port:{failed_port})]" if failed_port else "]")
                        if failed_account
                        else key_tag
                    )
                    error = _redact(
                        f"[尝试{metrics['attempts']}] model: {current_model}"
                        + (f" | account: {failed_account}" if failed_account else "")
                        + (f" | {key_label}" if len(route_keys) > 1 else "")
                        + f" | Error: {err_summary}"
                    )
                    result["error_history"].append(error)
                    request_event["status"] = "❌ 准备失败" if is_slot_busy else "❌ 失败"
                    request_event["error"] = error
                    terminal = budget_exhausted or (
                        (skip_same_model or attempt == max_attempts) and not has_fallback_model
                    )

                    if budget_exhausted:
                        action = f"已达 AI 超时阈值 ({float(timeout):g}s)，立即跳过本批不继续重试"
                    elif skip_same_model and has_fallback_model:
                        if is_slot_busy:
                            reason_label = "本地槽位繁忙"
                        elif status_code == 503 or "high demand" in exc_msg_lower:
                            reason_label = "上游模型 503/高负载"
                        elif cooled_model:
                            reason_label = "当前模型触发限流/熔断冷却"
                        else:
                            reason_label = "当前模型不可用"
                        action = f"{reason_label}，快速切换备用模型 {models[model_index + 1]}"
                    elif attempt < max_attempts:
                        if cooled_key and len(route_keys) > 1:
                            action = "当前密钥限流/不可用，立即自动切换池中下一个 API Key 重试"
                        else:
                            action = f"等待 {step_delay:g}s 后重试同模型" + (
                                "(自动切换池中其他小号)" if failed_account
                                else ("(轮换下一个 API Key)" if len(route_keys) > 1 else "")
                            )
                    elif has_fallback_model:
                        action = f"切换备用模型 {models[model_index + 1]}"
                    else:
                        action = "所有候选模型尝试耗尽，返回失败结果"

                    hint = {
                        400: "请求格式或附件类型不被模型接受，请检查网关协议和附件支持",
                        401: "密钥无效或过期，请检查 local_api_key / local_api_key_list",
                        403: "当前密钥无权访问此模型，请检查模型权限",
                        404: "模型名称或接口路由不存在，请检查模型名及 local_api_url",
                        429: "请求过于频繁或额度不足，请检查服务端限流与余额",
                        503: "上游服务高负载或临时不可用，已切换备用模型重试",
                    }.get(status_code, "服务连接或上游模型账号池不可用，请检查网关及 ai_proxy_url 状态")

                    req_cost = time.perf_counter() - request_started
                    if terminal:
                        completion_log = (
                            f"❌ [模型网关/失败] {'AI调用超时跳过' if budget_exhausted else '所有候选模型尝试耗尽'}"
                            f" | 最终模型: [{current_model}]{acc_tag}"
                            f" | 累计尝试: [{metrics['attempts']}] | HTTP: [{status_code or 'N/A'}]"
                            f" | 总耗时: [{time.perf_counter() - started:.2f}s]"
                            f" | 原因: [{_preview(err_summary, 160)}]"
                            f" | 排查方向: [{hint}]"
                        )
                        level = logging.WARNING if budget_exhausted else logging.ERROR
                    else:
                        completion_log = (
                            f"[模型网关/重试] 模型响应异常，准备重试或切换 | 模型: [{current_model}]{acc_tag}"
                            f" | 尝试: [{attempt}/{max_attempts} (累计:{metrics['attempts']})]"
                            f" | HTTP: [{status_code or 'N/A'}] | 耗时: [{req_cost:.2f}s]"
                            f" | 原因: [{_preview(err_summary, 140)}]"
                            f" | 后续动作: [{action}]"
                        )
                        level = logging.INFO if is_slot_busy else logging.WARNING
                except BaseException as exc:
                    request_event["status"] = "⚠️ 中断"
                    request_event["error"] = _redact(f"[请求中断] {type(exc).__name__}: {exc}")
                    raise
                finally:
                    request_event["http_status"] = status_code
                    request_event["duration_seconds"] = round(time.perf_counter() - request_started, 6)
                    request_event["finished_time_bj"] = _beijing_now()

                _log(completion_log, level)
                if result["status"] == "✅ 成功":
                    return result
                if budget_exhausted:
                    return result
                if skip_same_model:
                    break
                if attempt < max_attempts:
                    time.sleep(step_delay)
                    if not (cooled_key and len(route_keys) > 1):
                        delay = min(delay * 2, 600.0)
    except Exception as exc:
        error = _redact(f"[处理失败] {type(exc).__name__}: {exc}")
        result["error_history"].append(error)
        if not usage_events and selected_model is not None:
            preparation_event = _new_model_usage_event(selected_model, trace_id, called_at_bj)
            preparation_event["status"] = "❌ 准备失败"
            preparation_event["error"] = error
            preparation_event["finished_time_bj"] = _beijing_now()
            usage_events.append(preparation_event)
        _log(
            f"❌ [模型网关/准备失败] 请求初始化或附件读取异常 | trace_id: [{trace_id}]"
            f" | 原因: [{error}] | 排查方向: [检查调用参数、附件文件路径/格式及网关配置]",
            logging.ERROR,
        )
    finally:
        try:
            for c in owned_clients:
                cleanup_error = _close_client(c, f"generate_content / trace_id={trace_id}")
                if cleanup_error:
                    result["error_history"].append(cleanup_error)
        finally:
            try:
                record_usage_events(usage_events, path_override=MODEL_USAGE_JSON_PATH)
            except Exception as exc:
                stats_error = _redact(f"[模型统计/保存失败] {type(exc).__name__}: {exc}")
                result["error_history"].append(stats_error)
                _log(
                    f"❌ [模型统计/保存] 本次调用统计写盘失败（不影响生成结果）"
                    f" | 原因: [{stats_error}] | 排查方向: [检查统计目录权限或环境变量]",
                    logging.ERROR,
                )
            finally:
                metrics["total_time_seconds"] = round(time.perf_counter() - started, 3)
                if result["status"] == "❌ 失败":
                    result["content"] = "调用失败：" + "；".join(result["error_history"][-3:])
    return result


__all__ = [
    "generate_content",
    "parse_llm_json",
    "extract_llm_json",
    "string_to_object",
    "get_combined_model_usage",
    "flush_usage",
    "reset_usage_tracker",
    "reset_ai_cooldowns",
    "setup_logger",
    "ANTIGRAVITY_MODEL_ALIASES",
    "ANTIGRAVITY_MODEL_NAMES",
]


def _cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AI 模型网关自检与快速测试 CLI")
    parser.add_argument("--check", action="store_true", help="输出当前运行模式、可用模型列表、熔断状态与聚合统计")
    parser.add_argument("--prompt", default="", help="执行一次测试文本生成")
    parser.add_argument("--model", default=None, help="显式指定模型名")
    parser.add_argument("--group", default="medium", choices=("low", "medium", "high"), help="预制/云端分层模型组")
    parser.add_argument("--cloud", action="store_true", help="强制走 cloud_ai 云端路由")
    args = parser.parse_args(argv)

    if args.check or not args.prompt:
        rt = _resolve_ai_runtime(force_cloud=args.cloud, require_keys_on_cloud_block=False)
        candidates = _select_models(args.model, None, args.group, configured_models=rt["configured_models"])
        with _KEY_POOL_LOCK:
            now = time.monotonic()
            cooling = {m: round(u - now, 1) for m, u in _MODEL_COOLDOWN_UNTIL.items() if u > now}
        summary = {
            "runtime_mode": rt["runtime_mode"],
            "default_base_url": _redact(rt["default_base_url"]),
            "key_count": len(rt["default_keys"]),
            "candidate_models": candidates,
            "cooling_models_sec": cooling,
            "combined_usage_models": list(get_combined_model_usage().keys()),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not args.prompt:
            return 0

    res = generate_content(
        prompt=args.prompt,
        model=args.model,
        preset_model_group=args.group,
        force_cloud=args.cloud,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("status") == "✅ 成功" else 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
