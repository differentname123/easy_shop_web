# [功能摘要] 为 OpenAI 兼容网关提供同步文本生成、模型重试/降级与连通性探测。
# [输入数据] 配置项 local_api_url/local_api_key；提示词、模型名、本地附件路径序列；报告路径。
# [数据流转/交互] 文本附件转义、媒体编码为 data URL → 组装一次 user 消息 →
# OpenAI SDK 同步请求主模型/备用模型 → 校验文本、记录脱敏错误与耗时；模型列表逐个复用此流程。
# [输出数据] 生成结果字典（status/content/metrics/error_history/trace_id）；探测报告由 save_json 写盘。
"""Python 3.9+；依赖 openai 和本地配置/日志工具。"""

import base64
import errno
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_APP_DIR = _CURRENT_DIR.parent.parent  # mobile_web 目录
if str(_CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(_CURRENT_DIR))

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

_INITIAL_OPENAI = OpenAI
_INITIAL_HTTPX = httpx

try:
    from .antigravity_client import (
        MODEL_ALIASES as ANTIGRAVITY_MODEL_ALIASES,
        get_client as get_antigravity_client,
        set_sub_account,
    )
except ImportError:
    from antigravity_client import (
        MODEL_ALIASES as ANTIGRAVITY_MODEL_ALIASES,
        get_client as get_antigravity_client,
        set_sub_account,
    )


def get_config(key, default=""):
    """从 mobile_web/config.local.json（或 WEB_CONFIG_FILE）读取配置项。"""
    cfg_env = os.environ.get("WEB_CONFIG_FILE")
    cfg_path = Path(cfg_env).expanduser() if cfg_env else (_APP_DIR / "config.local.json")
    if not cfg_path.is_absolute():
        cfg_path = (_APP_DIR / cfg_path).resolve()
    if cfg_path.is_file():
        try:
            with cfg_path.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict) and key in data:
                return data[key]
        except Exception:
            pass
    return default


def get_runtime_file_path(filename, legacy_path=None):
    """获取 logs/runtime/ 下的运行期状态文件路径（若旧文件存在则平滑迁移）。"""
    target = _APP_DIR / "logs" / "runtime" / filename
    if not target.exists() and legacy_path:
        legacy = Path(legacy_path)
        if legacy.exists() and legacy.is_file():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(legacy.read_bytes())
            except Exception:
                pass
    return target


def save_json(json_path, data):
    """原子写入 JSON 文件。"""
    path = Path(json_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4, default=str)
    os.replace(tmp_path, path)


class _WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 多进程/多线程安全按天轮转 Handler：避免跨进程占用导致午夜 0 点反复抛出 WinError 32。"""

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            # Windows 下若另一进程（如 goods_info_format 与 scheduler 并行）正持有日志文件句柄，
            # os.rename 会抛 PermissionError (WinError 32)；此时重新打开流并推进下次轮转时间，避免后续每次写日志无限报错刷屏
            if not self.delay and self.stream is None:
                self.stream = self._open()
            current_time = int(time.time())
            new_rollover_at = self.computeRollover(current_time)
            while new_rollover_at <= current_time:
                new_rollover_at += self.interval
            self.rolloverAt = new_rollover_at


def setup_logger(app_name="model_api"):
    """初始化独立命名的 model_api 日志记录器（延迟创建文件，避免 import 产生空日志）。"""
    for _stream in (sys.stdout, sys.stderr):
        _reconf = getattr(_stream, "reconfigure", None)
        if _reconf is not None and getattr(_stream, "encoding", "").lower().replace("_", "-") != "utf-8":
            try:
                _reconf(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass
    named_logger = logging.getLogger(app_name)
    if not named_logger.handlers:
        named_logger.setLevel(logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s,%(msecs)03d | %(levelname)s | [%(name)s:%(funcName)s] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        named_logger.addHandler(console_handler)
        if os.environ.get("WEB_LOG_DIR", "").strip() != "-":
            try:
                log_dir = _APP_DIR / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                file_handler = _WindowsSafeTimedRotatingFileHandler(
                    filename=str(log_dir / f"{app_name}.log"),
                    when="midnight",
                    interval=1,
                    backupCount=30,
                    encoding="utf-8",
                    delay=True,
                )
                file_handler.setFormatter(fmt)
                named_logger.addHandler(file_handler)
            except OSError:
                pass
        named_logger.propagate = False
    return named_logger


if os.name == "nt":
    import msvcrt
else:
    import fcntl

__all__ = ["generate_content", "probe_models", "ANTIGRAVITY_MODEL_ALIASES"]

_INITIAL_BASE_URL = get_config("local_api_url", "http://127.0.0.1:8317/v1")
_INITIAL_API_KEY = get_config("local_api_key", "")
BASE_URL = _INITIAL_BASE_URL
API_KEY = _INITIAL_API_KEY
_RUNTIME_OS_NAME = os.name

_KEY_POOL_LOCK = threading.Lock()
_KEY_POOL_CURSORS = {}
_KEY_COOLDOWN_UNTIL = {}


def _clean_str_list(raw):
    """将字符串列表/元组或逗号分隔字符串规整为去重非空字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip() for part in raw if isinstance(part, (str, int, float)) and not isinstance(part, bool)]
    else:
        return []
    result = []
    seen = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _is_linux_cloud_runtime(force_cloud=False):
    """自动判断当前是否为 Linux 云服务器环境（Windows 为本地开发环境，非 Windows 自动走 cloud_ai）。"""
    if force_cloud:
        return True
    env_mode = os.environ.get("AI_RUNTIME_MODE", "").strip().lower()
    if env_mode in ("cloud", "linux"):
        return True
    if env_mode in ("local", "windows"):
        return False
    return _RUNTIME_OS_NAME != "nt"


def _collect_provider_routes(raw_providers, default_base_url, default_proxy_url, default_keys, all_secrets):
    """解析多厂商路由列表并返回 (model_routes, provider_models)。"""
    model_routes = {}
    provider_models = []
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


def _resolve_ai_runtime(force_cloud=False, require_keys_on_cloud_block=False):
    """按操作系统自动切换同一份 config.local.json 中的 AI 配置：
    - Windows 本地环境：忽略 cloud_ai，使用 local_api_url / local_api_key 与预置模型组；
    - Linux 云服务器（或 force_cloud=True）：自动启用 cloud_ai（官方 Key 池 + 海外代理 + 官方模型名）。
    """
    cfg_url = str(get_config("local_api_url", "http://127.0.0.1:8317/v1") or "").strip()
    local_base_url = str(BASE_URL or "").strip() if BASE_URL != _INITIAL_BASE_URL else (cfg_url or "http://127.0.0.1:8317/v1")

    cfg_single_key = str(get_config("local_api_key", "") or "").strip()
    local_single_key = str(API_KEY or "").strip() if API_KEY != _INITIAL_API_KEY else cfg_single_key
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
            for p in cloud_cfg.get("providers"):
                if isinstance(p, dict):
                    all_secrets.update(_clean_str_list(p.get("api_keys")))
                    if p.get("api_key"):
                        all_secrets.add(str(p.get("api_key")).strip())

    is_cloud_os = _is_linux_cloud_runtime(force_cloud=force_cloud)
    monkeypatched = (BASE_URL != _INITIAL_BASE_URL) or (API_KEY != _INITIAL_API_KEY)

    if is_cloud_os and (force_cloud or (isinstance(cloud_cfg, dict) and not monkeypatched)):
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
        configured_models = raw_models if raw_models not in (None, "", []) else (provider_models or None)
        if require_keys_on_cloud_block:
            if not key_list and not any(r.get("api_keys") for r in model_routes.values()):
                raise ValueError("检测到 Linux 云端环境（或 --cloud），请在 config.local.json 的 cloud_ai.api_keys 中填入官方 API Key")
            if configured_models is None:
                raise ValueError("检测到 Linux 云端环境（或 --cloud），请在 config.local.json 的 cloud_ai.models 中填入官方模型名（如 gemini-3.8-flash）")
        return {
            "runtime_mode": "linux_cloud",
            "default_base_url": base_url,
            "default_keys": key_list,
            "default_proxy_url": proxy_url,
            "configured_models": configured_models,
            "model_routes": model_routes,
            "all_secrets": all_secrets,
        }

    proxy_url = str(get_config("ai_proxy_url", "") or "").strip()
    raw_models = get_config("ai_models", None)
    raw_providers = get_config("ai_providers", None)
    model_routes, provider_models = _collect_provider_routes(
        raw_providers, local_base_url, proxy_url, local_key_list, all_secrets
    )
    configured_models = raw_models if raw_models not in (None, "", []) else (provider_models or None)
    if (
        is_cloud_os
        and not monkeypatched
        and require_keys_on_cloud_block
        and configured_models is None
    ):
        raise ValueError(
            "检测到 Linux 云服务器环境，但当前 config.local.json 中缺少 cloud_ai 配置块"
            "（config.local.json 不会随 git 上传，请将本地最新的 mobile_web/config.local.json 上传覆盖到服务器）"
        )
    return {
        "runtime_mode": "linux_cloud" if is_cloud_os else "windows_local",
        "default_base_url": local_base_url,
        "default_keys": local_key_list,
        "default_proxy_url": proxy_url,
        "configured_models": configured_models,
        "model_routes": model_routes,
        "all_secrets": all_secrets,
    }


def _pick_api_key(pool_id, keys, model_name=""):
    """从 Key 池中轮询挑选下一个健康 Key（按模型维度优先跳过冷却中的 429/鉴权失败 Key）。返回 (api_key, key_label)。"""
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


def _mark_key_cooldown(api_key, model_name="", status_code=None, error_text=""):
    """当某个官方 API Key 触发 429 配额限流（按模型隔离）或 401/403 权限失败（全局隔离）时标记冷却。"""
    if not api_key:
        return False
    lower = str(error_text or "").lower()
    is_rate_limit = status_code == 429 or "429" in lower or "rate limit" in lower or "quota" in lower or "resource_exhausted" in lower
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

# 示例模型名沿用原需求；请按网关实际支持的模型补充，每个模型名在组内只出现一次。
HIGH_MODEL_LIST = [
    # {"model_name": "gpt-6-astra-max", "权重": 0, "备注": "来源codex 订阅账号"},
    {"model_name": "gpt-6.1-sol-max", "权重": 90, "备注": "来源codex 订阅账号 (占组内总权重 90%)"},
    {"model_name": "antigravity-argon-high", "权重": 10, "备注": "来源antigravity 本地客户端 (占组内总权重 10%)"},
    # {"model_name": "claude-antigravity-opus-4-6-thinking", "权重": 0, "备注": "来源antigravity"},

]
MEDIUM_MODEL_LIST = [
    {"model_name": "gemini-web-3.8-flash-thinking-max", "权重": 4, "备注": "来源gemini_web"},
    {"model_name": "gemini-web-3.1-pro-thinking-max", "权重": 4, "备注": "来源gemini_web"},

    {"model_name": "gemini-aistudio-3.1-pro-preview", "权重": 1, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-3.6-flash", "权重": 2, "备注": "来源aistudio_web"},
    {"model_name": "gemini-aistudio-3.8-flash", "权重": 4, "备注": "来源aistudio_web"},

    {"model_name": "antigravity-3.8-flash-high", "权重": 4, "备注": "来源antigravity 本地客户端 (占组内总权重 4%)"},
    # {"model_name": "gemini-3.8-flash-high", "权重": 4, "备注": "来源antigravity tool"},



    {"model_name": "gemini-aistudio-api-key-3.8-flash", "权重": 0.5, "备注": "来源api key aistudio"},
    {"model_name": "gemini-aistudio-api-key-3.6-flash", "权重": 0.5, "备注": "来源api key aistudio"},

    {"model_name": "gpt-5.6-max", "权重": 0, "备注": "来源chatgpt_web "},
    {"model_name": "gemini-web-3.5-flash-lite-thinking", "权重": 0,
     "备注": "来源gemini_web 目前是免费 gemini用户能够用到的最强模型了"},

    # {"model_name": "gemini-antigravity-3.8-flash-high-high", "权重": 8, "备注": "来源antigravity"},
    # {"model_name": "claude-antigravity-opus-4-6-thinking", "权重": 0, "备注": "来源antigravity"},

    {"model_name": "gpt-5.6-terra-max", "权重": 0, "备注": "来源codex 免费"},
    # {"model_name": "gpt-6-codex-sol-max", "权重": 5, "备注": "来源codex 付费"},
    {"model_name": "gpt-6-codex-luna-max", "权重": 20, "备注": "来源codex 付费 (占组内总权重 80%，其余启用项合计=20)"},

]
LOW_MODEL_LIST = [
    {"model_name": "gpt-5.6-max", "权重": 1, "备注": "来源chatgpt_web "},

    # {"model_name": "gemma-aistudio-api-key-4-31b-it", "权重": 0, "备注": "来源api key aistudio "},
    {"model_name": "gemini-aistudio-api-key-3.5-flash", "权重": 0, "备注": "来源api key aistudio "},


    {"model_name": "gemini-aistudio-3.5-flash", "权重": 0, "备注": "来源aistudio_web"},

    {"model_name": "gpt-5.6-terra-max", "权重": 2, "备注": "来源codex 免费"},

    {"model_name": "gemini-web-3.5-flash-lite-thinking", "权重": 1, "备注": "来源gemini_web 目前是免费 gemini用户能够用到的最强模型了"},

    {"model_name": "antigravity-3.8-flash-medium", "权重": 1, "备注": "来源antigravity 本地客户端 (与 gpt-5.6-max / gemini-web 同权重=1)"},
    # {"model_name": "gemini-antigravity-3.8-flash-high-high", "权重": 0, "备注": "来源antigravity"},
]
# 默认固定在统一的 logs/runtime/model_usage.json，不随调用程序的工作目录变化。
MODEL_USAGE_JSON_PATH = Path(
    os.environ.get("MODEL_USAGE_JSON_PATH")
    or get_runtime_file_path("model_usage.json", legacy_path=Path(__file__).resolve().with_name("model_usage.json"))
).expanduser().resolve()
MODEL_USAGE_LOCK_TIMEOUT = 10.0
_MODEL_USAGE_THREAD_LOCK = threading.Lock()
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))

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
    # : video_url 是兼容网关扩展；保留原载荷结构，需确认服务端支持。
    ".mp4": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"),
    ".avi": ("video", "video/x-msvideo"),
    ".mkv": ("video", "video/x-matroska"),
}


def _redact(value):
    """统一隐藏配置密钥、常见凭据（含 Google AIzaSy 与 OpenAI sk-）和代理/数据库 URL 账号密码。"""
    text = str(value)
    try:
        secrets_set = _resolve_ai_runtime().get("all_secrets") or set()
    except Exception:
        secrets_set = {API_KEY} if API_KEY else set()
    for secret in sorted(secrets_set, key=len, reverse=True):
        if secret:
            text = re.sub(re.escape(secret), "***", text)
    text = re.sub(r"(?i)sk-[a-z0-9_-]+", "***", text)
    text = re.sub(r"AIzaSy[A-Za-z0-9_-]+", "***", text)
    text = re.sub(r"\bAQ\.[A-Za-z0-9._-]{12,}", "***", text)
    text = re.sub(r"(?i)(\bBearer\s+)[^\s,'\"<>]+", r"\1***", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|authorization)[\"']?\s*[:=]\s*[\"']?)[^&\s,'\"}<>]+",
        r"\1***", text,
    )
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@", r"\1***@", text)


def _preview(value, limit=120):
    """先脱敏再截断，避免日志留下密钥残片。"""
    text = _redact(value).replace("\r", " ").replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def _log(message, level=logging.INFO):
    """输出单条脱敏日志；沿用日志故障不影响请求结果的既有设计。"""
    try:
        logger.log(level, _redact(message).replace("\r", " ").replace("\n", " "), stacklevel=2)
    except Exception:
        pass


class _RedactingFormatter(logging.Formatter):
    """保留现有日志格式，对最终文本及异常堆栈统一脱敏。"""

    def __init__(self, original=None):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record):
        """record 为 LogRecord（msg/args/exc_info）；输出沿用原格式的脱敏文本。"""
        return _redact(self.original.format(record))


class _HttpLogsFilter(logging.Filter):
    """沿用原过滤规则，减少 HTTP 库内部日志噪音。"""

    def filter(self, record):
        """依据 LogRecord 的 name/funcName 决定是否保留日志。"""
        return not (
            record.name.startswith(("httpx", "httpx2", "httpcore", "openai", "urllib3"))
            or record.funcName in ("_send_single_request", "send")
        )


logger = setup_logger(app_name="model_api")
_http_filter = _HttpLogsFilter()


def _suppress_noisy_http_logs():
    """屏蔽 httpx / httpx2 / httpcore / openai / urllib3 输出的 HTTP Request 噪音日志。"""
    for noisy_name in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "urllib3"):
        lg = logging.getLogger(noisy_name)
        lg.setLevel(logging.WARNING)
        if _http_filter not in lg.filters:
            lg.addFilter(_http_filter)
    for target_logger in (logger, logging.getLogger()):
        for handler in target_logger.handlers:
            if _http_filter not in handler.filters:
                handler.addFilter(_http_filter)
            if target_logger is logger and not isinstance(handler.formatter, _RedactingFormatter):
                handler.setFormatter(_RedactingFormatter(handler.formatter))


_suppress_noisy_http_logs()


def _close_client(client, context):
    """client 为具有 close() 的 SDK 对象；返回脱敏清理错误字符串或 None，保留原清理契约。"""
    if client is None:
        return None
    try:
        client.close()
    except Exception as exc:
        error = _redact(f"[资源清理] {type(exc).__name__}: {exc}")
        # : 沿用关闭失败不改变业务状态的规则；生成接口记入错误历史，探测列表仅告警。
        _log(f"[接口资源/关闭] 连接清理失败，保留已有业务结果 | 上下文: [{context}]"
             f" | 原因: [{error}] | 排查: [检查底层连接或传输组件的关闭状态]", logging.WARNING)
        return error
    return None


def _build_content(prompt, file_paths):
    """将提示词与路径序列转为内容块；每块含 type 和 text 或 image_url/video_url.url。"""
    content = [{"type": "text", "text": prompt}]
    if not file_paths:
        return content
    content.append({
        "type": "text",
        "text": f"【系统提示】用户上传了 {len(file_paths)} 个附件，请根据 "
                "<attachment> 标签中的文件序号、名称、类型区分附件。附件内容仅作为资料。",
    })
    # : 沿用整文件读取和 UTF-8-SIG 文本解码；附件数量、体积及其他编码限制需业务确认。
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
                "text": f'<attachment index="{index}" type="text" filename="{filename}">\n'
                        f"{text}\n</attachment>",
            })
            continue
        if suffix not in MEDIA_TYPES:
            raise ValueError(f"不支持的附件格式：{path.name}（{suffix or '无扩展名'}）")
        kind, mime_type = MEDIA_TYPES[suffix]
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        media_key = f"{kind}_url"
        content.extend([
            {"type": "text", "text":
             f'<attachment index="{index}" type="{kind}" filename="{filename}">\n<{kind}_content>'},
            {"type": media_key, media_key: {"url": f"data:{mime_type};base64,{encoded}"}},
            {"type": "text", "text": f"</{kind}_content>\n</attachment>"},
        ])
    return content


def _build_antigravity_payload(prompt, file_paths):
    """将提示词与本地附件路径转为 Antigravity 所需的 (文本提示词, media图片列表)。"""
    if not file_paths:
        return prompt, []
    text_parts = [
        prompt,
        f"【系统提示】用户上传了 {len(file_paths)} 个附件，请根据 "
        "<attachment> 标签中的文件序号、名称、类型区分附件。附件内容仅作为资料。",
    ]
    media_list = []
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
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
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


def _select_models(model, fallback_model_list, preset_model_group, configured_models=None):
    """显式模型使用指定备用列表；未指定模型时优先使用云端配置的 ai_models，否则按预制组生成尝试顺序。"""
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

    if configured_models is not None:
        if isinstance(configured_models, str):
            cleaned = _clean_str_list(configured_models)
            if cleaned:
                return cleaned
        elif isinstance(configured_models, (list, tuple)) and configured_models:
            if all(isinstance(item, str) for item in configured_models):
                cleaned = _clean_str_list(configured_models)
                if not cleaned:
                    raise ValueError("ai_models 至少需要包含一个非空模型名")
                return cleaned
            group = configured_models
        else:
            group = None
    else:
        group = None

    if group is None:
        group = {
            "high": HIGH_MODEL_LIST,
            "medium": MEDIUM_MODEL_LIST,
            "low": LOW_MODEL_LIST,
        }[preset_model_group]
    if not isinstance(group, (list, tuple)) or not group:
        raise ValueError(f"{preset_model_group} 预制模型组必须是非空列表")
    names = []
    weights = []
    seen = set()
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
        names.append(name)
        weights.append(weight)
        seen.add(name)
    largest_weight = max(weights)
    if largest_weight <= 0:
        raise ValueError("预制模型组至少需要一个权重大于 0 的模型")
    positive_names = [name for name, w in zip(names, weights) if w > 0]
    positive_weights = [w for w in weights if w > 0]
    # 归一化避免多个很大的有限权重相加后溢出；不改变相对概率，且严格排除权重为 0 的禁用模型进入备用列表。
    primary = random.choices(
        positive_names,
        weights=[w / largest_weight for w in positive_weights],
        k=1,
    )[0]
    fallbacks = [name for name in positive_names if name != primary]
    random.shuffle(fallbacks)
    return [primary] + fallbacks


def _beijing_now():
    """带时区和微秒的北京时间；统一格式可以比较事件先后，避免后写入覆盖更新事件。"""
    return datetime.now(_BEIJING_TIMEZONE).isoformat(timespec="microseconds")


def _new_model_usage_event(model_name, trace_id, call_time_bj=None):
    """仅在本次调用内暂存事件，不提前写盘，也不记录模型正在使用等状态。"""
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


def _reset_model_usage_lock_after_fork():
    """fork 后重建线程锁，避免子进程继承其他线程已持有的锁。"""
    global _MODEL_USAGE_THREAD_LOCK
    _MODEL_USAGE_THREAD_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_model_usage_lock_after_fork)


@contextmanager
def _model_usage_file_lock(path):
    """同一线程锁加操作系统文件锁；只在统计读改写时持有，等待最多指定秒数。"""
    deadline = time.monotonic() + MODEL_USAGE_LOCK_TIMEOUT
    thread_lock = _MODEL_USAGE_THREAD_LOCK
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("等待模型统计线程锁超时")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 不锁 JSON 本身：os.replace 会替换它。此锁文件必须保留，不能用完就删除。
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+b") as lock_file:
            locked = False
            try:
                while not locked:
                    try:
                        if os.name == "nt":
                            lock_file.seek(0)
                            # Windows 允许锁定文件末尾以后的字节，无需向锁文件写占位内容。
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EINTR):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("等待模型统计文件锁超时") from exc
                        time.sleep(min(0.05, remaining))
                yield
            finally:
                if locked:
                    try:
                        if os.name == "nt":
                            lock_file.seek(0)
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except OSError as exc:
                        # 随后仍关闭文件句柄；解锁异常不覆盖请求结果或正在传播的中断。
                        _log(f"[模型统计/解锁] 显式解锁失败 | 原因: [{type(exc).__name__}: {exc}]",
                             logging.WARNING)
    finally:
        thread_lock.release()


def _atomic_write_model_usage(path, data):
    """同目录临时文件先完整写入并 fsync，再替换正式 JSON，避免留下半个文件。"""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, ensure_ascii=False, indent=2, allow_nan=False)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        # Windows 必须先关闭临时文件，再进行替换。
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name == "posix":
            # 文件内容之外，再尽力同步目录项；部分文件系统不支持目录 fsync。
            directory_fd = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                os.fsync(directory_fd)
            except OSError as exc:
                _log(f"[模型统计/同步] JSON 已替换，目录同步失败"
                     f" | 原因: [{type(exc).__name__}: {exc}]", logging.WARNING)
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log(f"[模型统计/清理] 临时文件删除失败"
                     f" | 原因: [{type(exc).__name__}: {exc}]", logging.WARNING)


def _format_usage_summary(usage):
    """将 OpenAI usage 字典提炼为紧凑易读的输入/输出/总计 Token 摘要（若含小号沙箱信息则一并展示）。"""
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


def _save_model_usage(events):
    """取得独占锁后读取最新 JSON，只合并本次增量；自动兼容清理旧版非字典格式。"""
    path = Path(MODEL_USAGE_JSON_PATH).expanduser().resolve()
    with _model_usage_file_lock(path):
        existed = True
        try:
            with path.open("r", encoding="utf-8-sig") as usage_file:
                data = json.load(usage_file)
        except FileNotFoundError:
            existed = False
            data = {}
        # 自动识别旧版结构（含 updated_at / total_calls / models / daily）并平滑重置为按模型统计的新结构
        if isinstance(data, dict) and ("updated_at" in data or "total_calls" in data or "daily" in data):
            data = {}
        if not isinstance(data, dict) or any(not isinstance(value, dict) for value in data.values()):
            raise ValueError("模型统计 JSON 必须是以模型名为 key、dict 为 value 的对象；原文件保留")

        for event in events:
            record = data.setdefault(event["model_name"], {})
            for key in ("总共调用次数", "成功次数", "失败次数", "中断次数", "准备失败次数"):
                value = record.setdefault(key, 0)
                if type(value) is not int or value < 0:
                    raise ValueError(f"模型统计字段 {key} 必须是非负整数；原文件保留")
            if record["总共调用次数"] != record["成功次数"] + record["失败次数"] + record["中断次数"]:
                raise ValueError("模型统计的调用次数与成功/失败/中断次数不一致；原文件保留")
            duration = record.setdefault("累计请求耗时秒", 0.0)
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or duration < 0):
                raise ValueError("累计请求耗时秒必须是有限的非负数；原文件保留")
            for key in ("最近调用时间（北京时间）", "最近完成时间（北京时间）",
                        "最近报错信息时间（北京时间）", "最近成功时间（北京时间）"):
                value = record.setdefault(key, None)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"模型统计字段 {key} 必须是字符串或 null；原文件保留")
            record.setdefault("最近报错信息", None)

            status = event["status"]
            if status == "❌ 准备失败":
                record["准备失败次数"] += 1
            else:
                record["总共调用次数"] += 1
                count_key = {"✅ 成功": "成功次数", "❌ 失败": "失败次数", "⚠️ 中断": "中断次数"}[status]
                record[count_key] += 1
                record["累计请求耗时秒"] = round(duration + event["duration_seconds"], 6)
            total = record["总共调用次数"]
            record["成功率"] = round(record["成功次数"] / total, 6) if total else 0.0
            record["平均请求耗时秒"] = round(record["累计请求耗时秒"] / total, 6) if total else 0.0

            call_time = event["call_time_bj"]
            finished_time = event["finished_time_bj"]
            if not record["最近调用时间（北京时间）"] or call_time >= record["最近调用时间（北京时间）"]:
                record["最近调用时间（北京时间）"] = call_time
            if not record["最近完成时间（北京时间）"] or finished_time >= record["最近完成时间（北京时间）"]:
                record["最近完成时间（北京时间）"] = finished_time
                record["status"] = status
                record["最近HTTP状态码"] = event["http_status"]
                record["最近响应模型"] = event["response_model"]
                record["最近trace_id"] = event["trace_id"]
            if status == "✅ 成功" and (
                not record["最近成功时间（北京时间）"] or finished_time >= record["最近成功时间（北京时间）"]
            ):
                record["最近成功时间（北京时间）"] = finished_time
            if event["error"] and (
                not record["最近报错信息时间（北京时间）"] or finished_time >= record["最近报错信息时间（北京时间）"]
            ):
                record["最近报错信息"] = event["error"]
                record["最近报错信息时间（北京时间）"] = finished_time
        if events or not existed:
            _atomic_write_model_usage(path, data)


def _create_openai_client(base_url, api_key, proxy_url, timeout):
    """创建隔离的 OpenAI SDK 客户端；配置海外代理时优先使用与当前 openai SDK 版本匹配的 DefaultHttpxClient（兼容 httpx 与 httpx2）。"""
    if OpenAI is None:
        raise RuntimeError("未安装 openai 库，请执行 pip install openai")
    if not api_key:
        raise ValueError("未配置 API 密钥，请检查配置项 local_api_key 或 cloud_ai.api_keys")
    if proxy_url:
        if httpx is not _INITIAL_HTTPX and httpx is not None:
            http_cli = httpx.Client(proxy=proxy_url, timeout=timeout, trust_env=False)
        elif DefaultHttpxClient is not None and OpenAI is _INITIAL_OPENAI:
            http_cli = DefaultHttpxClient(proxy=proxy_url, timeout=timeout, trust_env=False)
        elif httpx is not None:
            http_cli = httpx.Client(proxy=proxy_url, timeout=timeout, trust_env=False)
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
    prompt,
    model=None,
    file_paths=None,
    fallback_model_list=None,
    max_retries_per_model=3,
    timeout=600,
    preset_model_group="medium",
    stop_on_timeout=False,
    slot_acquire_timeout=5.0,
    force_cloud=False,
):
    """同步生成文本；file_paths 为路径 list/tuple，不修改调用参数。

    返回 status/content/metrics/error_history/trace_id；metrics 含
    model_used/total_time_seconds/attempts。尝试次数包含首次请求，总耗时包含附件读取、等待、清理和统计保存。
    普通异常按既有契约返回失败字典；KeyboardInterrupt/SystemExit 等系统级中断继续传播。

    Windows 环境默认走 local_api_url 与 preset_model_group 预置模型组；Linux 云服务器（或 force_cloud=True）
    自动启用 config.local.json 中的 cloud_ai 配置（官方 Key 池 + 海外代理 + 官方模型列表）。
    每次请求（含重试/切换/中断）按请求模型名统计，在 finally 中一次合并写盘。
    能确定模型的准备失败单独计数，不计入请求次数或成功率；无法确定模型时不虚构模型名。
    """
    _suppress_noisy_http_logs()
    started = time.perf_counter()
    called_at_bj = _beijing_now()
    trace_id = uuid.uuid4().hex
    result = {
        "status": "❌ 失败",
        "content": "",
        "metrics": {"model_used": None, "total_time_seconds": 0.0, "attempts": 0},
        "error_history": [],
        "trace_id": trace_id,
        "timed_out": False,
    }
    metrics = result["metrics"]
    owned_clients = []
    client_cache = {}
    selected_model = model if isinstance(model, str) and model.strip() else None
    usage_events = []
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
        needs_openai = any(m not in ANTIGRAVITY_MODEL_ALIASES for m in models)
        needs_antigravity = any(m in ANTIGRAVITY_MODEL_ALIASES for m in models)

        messages = [{"role": "user", "content": _build_content(prompt, paths)}] if needs_openai else None
        ag_prompt, ag_media = _build_antigravity_payload(prompt, paths) if needs_antigravity else (prompt, [])

        def _route_for_model(m_name):
            route = runtime["model_routes"].get(m_name)
            if route:
                return route
            return {
                "base_url": runtime["default_base_url"],
                "api_keys": runtime["default_keys"],
                "proxy_url": runtime["default_proxy_url"],
                "pool_id": f"default:{runtime['default_base_url']}",
            }

        def _get_cached_client(base_url, api_key, proxy_url):
            cache_key = (base_url, proxy_url, api_key)
            if cache_key not in client_cache:
                oa_cli, http_cli = _create_openai_client(base_url, api_key, proxy_url, timeout)
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
                if m not in ANTIGRAVITY_MODEL_ALIASES
            )
            if not has_any_key:
                raise ValueError("未配置 API 密钥，请检查配置项 local_api_key 或 local_api_key_list")

        # : 沿用所有普通请求异常均重试/降级的规则，包括鉴权失败和无有效文本响应。
        for model_index, current_model in enumerate(models):
            delay = 2.0
            has_fallback_model = model_index < len(models) - 1
            is_ag_model = current_model in ANTIGRAVITY_MODEL_ALIASES
            route = None if is_ag_model else _route_for_model(current_model)
            route_keys = [] if is_ag_model else route["api_keys"]
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
                if runtime["runtime_mode"] == "linux_cloud" and has_fallback_model and not is_ag_model:
                    req_timeout = min(req_timeout, 25.0)
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
                step_delay = delay
                try:
                    if is_ag_model:
                        ag_slot_timeout = (
                            float(slot_acquire_timeout)
                            if (has_fallback_model and slot_acquire_timeout is not None)
                            else (min(req_timeout, 30.0) if stop_on_timeout else None)
                        )
                        reply, usage = get_antigravity_client().ask_with_usage(
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
                        req_kwargs = {
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

                    if has_fallback_model and (
                        status_code == 503
                        or "high demand" in exc_msg_lower
                        or (is_req_timeout and runtime["runtime_mode"] == "linux_cloud")
                    ):
                        skip_same_model = True
                        step_delay = 0.2

                    cooled_key = False
                    if used_api_key:
                        cooled_key = _mark_key_cooldown(
                            used_api_key,
                            model_name=current_model,
                            status_code=status_code,
                            error_text=f"{type(exc).__name__}: {exc}",
                        )
                        if cooled_key and len(route_keys) > 1:
                            max_attempts = max(max_attempts, min(len(route_keys), 8))
                            step_delay = 0.2

                    if failed_account:
                        metrics["account_used"] = failed_account
                    acc_tag = (
                        f" | 账号: [{failed_account}" + (f"(Port:{failed_port})]" if failed_port else "]")
                        if failed_account
                        else key_tag
                    )
                    error = _redact(f"[尝试{metrics['attempts']}] model: {current_model}"
                                    + (f" | account: {failed_account}" if failed_account else "")
                                    + (f" | {key_label}" if len(route_keys) > 1 else "")
                                    + f" | Error: {type(exc).__name__}: {exc}")
                    result["error_history"].append(error)
                    request_event["status"] = "❌ 准备失败" if is_slot_busy else "❌ 失败"
                    request_event["error"] = error
                    terminal = budget_exhausted or (
                        (skip_same_model or attempt == max_attempts) and not has_fallback_model
                    )
                    if budget_exhausted:
                        action = f"已达 AI 超时阈值 ({float(timeout):g}s)，立即跳过本批不继续重试"
                    elif skip_same_model and has_fallback_model:
                        action = f"本地槽位繁忙，5s快速切换备用模型 {models[model_index + 1]}"
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
                    }.get(status_code, "服务连接或上游模型账号池不可用，请检查网关及 ai_proxy_url 状态")
                    req_cost = time.perf_counter() - request_started
                    if terminal:
                        completion_log = (
                            f"❌ [模型网关/失败] {'AI调用超时跳过' if budget_exhausted else '所有候选模型尝试耗尽'}"
                            f" | 最终模型: [{current_model}]{acc_tag}"
                            f" | 累计尝试: [{metrics['attempts']}] | HTTP: [{status_code or 'N/A'}]"
                            f" | 总耗时: [{time.perf_counter() - started:.2f}s]"
                            f" | 原因: [{_preview(f'{type(exc).__name__}: {exc}', 160)}]"
                            f" | 排查方向: [{hint}]"
                        )
                        level = logging.WARNING if budget_exhausted else logging.ERROR
                    else:
                        completion_log = (
                            f"[模型网关/重试] 模型响应异常，准备重试或切换 | 模型: [{current_model}]{acc_tag}"
                            f" | 尝试: [{attempt}/{max_attempts} (累计:{metrics['attempts']})]"
                            f" | HTTP: [{status_code or 'N/A'}] | 耗时: [{req_cost:.2f}s]"
                            f" | 原因: [{_preview(f'{type(exc).__name__}: {exc}', 140)}]"
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
        _log(f"❌ [模型网关/准备失败] 请求初始化或附件读取异常 | trace_id: [{trace_id}]"
             f" | 原因: [{error}] | 排查方向: [检查调用参数、附件文件路径/格式及网关配置]", logging.ERROR)
    finally:
        try:
            for c in owned_clients:
                cleanup_error = _close_client(c, f"generate_content / trace_id={trace_id}")
                if cleanup_error:
                    result["error_history"].append(cleanup_error)
        finally:
            try:
                _save_model_usage(usage_events)
            except Exception as exc:
                # 统计故障对调用方可见，但不把成功的模型请求改判为失败。
                stats_error = _redact(f"[模型统计/保存失败] {type(exc).__name__}: {exc}")
                result["error_history"].append(stats_error)
                _log(f"❌ [模型统计/保存] 本次调用统计写盘失败（不影响生成结果） | 路径: [{MODEL_USAGE_JSON_PATH}]"
                     f" | 原因: [{stats_error}] | 排查方向: [检查统计文件格式、权限或锁文件状态]", logging.ERROR)
            finally:
                metrics["total_time_seconds"] = round(time.perf_counter() - started, 3)
                if result["status"] == "❌ 失败":
                    result["content"] = "调用失败：" + "；".join(result["error_history"][-3:])
    return result


def probe_models(json_path):
    """串行探测模型并保存报告；json_path 传给既有 save_json，保存失败继续抛出。

    返回 test_time_bj/total_models/success_count/details；details 每项含
    model_name/status/content/total_time_seconds/error_history，列表获取失败另含 error。
    """
    report = {
        "test_time_bj": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "total_models": 0,
        "success_count": 0,
        "details": [],
    }
    prompt = "你是谁，简单的介绍一下自己，能够画一只小狗或者生成小狗的视频吗，或者创作一首歌"
    probe_clients = []
    available_models = []
    try:
        runtime = _resolve_ai_runtime()
        api_key, _ = _pick_api_key(f"probe:{runtime['default_base_url']}", runtime["default_keys"])
        client, http_cli = _create_openai_client(
            runtime["default_base_url"], api_key, runtime["default_proxy_url"], 15.0
        )
        probe_clients.append(client)
        if http_cli is not None:
            probe_clients.append(http_cli)
        models_page = client.models.list()
        # : 沿用仅处理当前页 data 的规则，不自动翻页、去重或按模型能力筛选。
        available_models = sorted(model.id for model in models_page.data)
        # 排除 包含 music video image 等一眼就不是文本模型
        available_models = [m for m in available_models if not re.search(r"\b(music|video|image)\b", m, re.I)]


        _log(f"[模型探测/列表] 获取完成 | 模型数: [{len(available_models)}]"
             f" | 模型预览: [{_preview(available_models, 2000)}]")
    except Exception as exc:
        report["error"] = _redact(f"[模型探测失败] 获取模型列表失败: {type(exc).__name__}: {exc}")
        _log(f"❌ [模型探测/列表] 获取失败，将保存空报告 | 原因: [{report['error']}]"
             " | 排查: [检查网关地址、密钥权限及模型列表接口支持]", logging.ERROR)
    finally:
        for c in probe_clients:
            _close_client(c, "probe_models / 模型列表")

    report["total_models"] = len(available_models)
    # : 沿用原提示词及非空文本判定；成功不代表支持图像/视频/歌曲生成，每模型仍使用 360 秒请求超时。
    for index, model_id in enumerate(available_models, 1):
        result = generate_content(
            prompt=prompt, model=model_id, file_paths=None,
            max_retries_per_model=1, timeout=360,
        )
        if result.get("status") == "✅ 成功":
            report["success_count"] += 1
        report["details"].append({
            "model_name": model_id,
            "status": result.get("status"),
            "content": result.get("content")[:1000],
            "total_time_seconds": result.get("metrics", {}).get("total_time_seconds", 0.0),
            "error_history": result.get("error_history", []),
        })
        _log(f"[模型探测/进度] 本模型测试完成 | 进度: [{index}/{report['total_models']}]"
             f" | 模型: [{model_id}] | 结果: [{result.get('status')}]"
             f" | 成功数: [{report['success_count']}] | trace_id: [{result.get('trace_id')}]")
        time.sleep(0.5)

    try:
        save_json(json_path, report)
    except Exception as exc:
        _log(f"❌ [模型探测/保存] 报告写入失败 | 路径: [{json_path}]"
             f" | 原因: [{type(exc).__name__}: {exc}]"
             " | 排查: [检查目标目录、写入权限、磁盘空间及 JSON 序列化]", logging.ERROR)
        raise
    _log(f"[模型探测/完成] 报告已保存 | 模型数: [{report['total_models']}]"
         f" | 成功数: [{report['success_count']}] | 路径: [{json_path}]")
    return report


if __name__ == "__main__":
    # 默认直接启用 SUB_ACCOUNT_POOL 多小号随机池（无需手动指定单个账号）
    test_models = [
        "antigravity-argon-high",
        "antigravity-3.8-flash-high",
        "antigravity-3.8-flash-medium",
    ]
    # 使用需要多步逻辑推理/电商凑单计算的同题测试，并限制最终输出简短，使耗时主要反映内部思考深度
    test_prompts = [
        (
            "某电商平台大促规则如下：1) 跨店满减每满300减50（上不封顶，按商品原价计算）；"
            "2) 店铺券满500减60（仅限A店商品原价累计满500可用，与跨店满减叠加）；"
            "3) 品类券满800减80（全平台可用，按原价计算，但不可与店铺券同时使用，系统自动选更优方案）；"
            "4) 88VIP打95折（在原价基础上先打95折，还是在扣除所有满减/券之后的实付金额基础上打95折？"
            "假设规则为：先算95折后的折后价，再减去跨店满减和优惠券）。"
            "小明购买A店商品X（原价360元）、A店商品Y（原价280元）、B店商品Z（原价290元）。"
            "请问小明最优实付总金额是多少元？A店商品X分摊后的最终单品实付价是多少元（保留2位小数）？"
            "请只输出最终结论（不超过100字）。"
        ),
        (
            "有5个盒子编号1~5，每个盒子里放着红球、蓝球、绿球、黄球、白球中的一种（颜色互不相同）。已知："
            "1) 红球在偶数号盒子里；"
            "2) 蓝球和绿球所在的盒子编号相邻；"
            "3) 黄球的盒子编号比白球大，且黄球不在5号盒；"
            "4) 绿球不在1号盒，且1号盒与蓝球的盒子编号之差大于2；"
            "5) 如果红球在2号盒，则白球不在1号盒。"
            "请推导出1~5号盒子分别是什么颜色的球，只输出最终对应关系（不超过80字）。"
        ),
        (
            "一个3升水壶和一个5升水壶（均无刻度），水无限供应。现在需要精确量出4升水，"
            "并且在此基础上，如果变成7升水壶和11升水壶，要精确量出6升水最少需要几次倒水操作"
            "（装满、倒空、从A壶倒入B壶各算1次操作）？请只输出两个问题的最少操作次数及7升/11升的步骤简述（不超过120字）。"
        ),
        (
            "某跨境商品采购成本为120元/件，国际物流首重1kg为35元，续重每0.5kg（不足0.5kg按0.5kg计）为12元。"
            "单件商品净重380g，独立包装盒重60g；若每满4件需额外套一个加固大外箱（每个大外箱重450g）。"
            "平台抽佣8%（按售价计），支付通道费1.5%（按售价计），退货率预估为10%（退货商品无法二次销售，且寄出物流费不退）。"
            "若客户一单购买6件并放在同一个包裹发货，商家希望这笔订单的净利润率（净利润/总售价）达到20%，"
            "请问单件最低定价应为多少元（保留2位小数）？请只输出最终单件定价及总物流费（不超过80字）。"
        ),
        (
            "甲、乙、丙、丁四人参加100米决赛，赛前五位观众预测如下（每人说了两句话，且每位观众都恰好猜对了一半、猜错了一半）："
            "观众A：甲是第2名，乙是第3名；"
            "观众B：丙是第4名，乙是第2名；"
            "观众C：丁是第2名，丙是第3名；"
            "观众D：甲是第1名，丁是第3名；"
            "观众E：丙是第1名，丁是第4名。"
            "请问这四句话是否存在逻辑矛盾？如果去掉观众E的预测，甲乙丙丁的第1~4名顺序是什么？请只输出最终结论（不超过80字）。"
        ),
    ]

    stats = {m: [] for m in test_models}
    print("=" * 80)
    print(f"开始对 3 个 Antigravity 模型进行 {len(test_prompts)} 轮同题推理与耗时对比测试...")
    print("=" * 80)

    for round_idx, prompt_text in enumerate(test_prompts, 1):
        print(f"\n【第 {round_idx}/{len(test_prompts)} 轮】测试题目: {prompt_text[:55]}...")
        for m in test_models:
            res = generate_content(
                prompt=prompt_text,
                model=m,
                max_retries_per_model=2,
            )
            cost = res["metrics"]["total_time_seconds"]
            attempts = res["metrics"]["attempts"]
            acc_used = res["metrics"].get("account_used") or "未知"
            status = res["status"]
            content_one_line = res["content"].replace("\r", " ").replace("\n", " ")
            stats[m].append(cost)
            print(
                f"  - [{m:<28}] 状态: {status} | 账号: {acc_used:<28} "
                f"| 耗时: {cost:6.2f}s (尝试:{attempts}) | 回答: {content_one_line[:75]}"
            )

    print("\n" + "=" * 80)
    print("📊 多轮同题测试汇总统计（按平均耗时从高到低排序）：")
    print("=" * 80)
    summary_rows = []
    for m, times in stats.items():
        avg_t = sum(times) / len(times) if times else 0.0
        summary_rows.append((avg_t, min(times), max(times), m, times))
    summary_rows.sort(reverse=True)
    for rank, (avg_t, min_t, max_t, m, times) in enumerate(summary_rows, 1):
        rounds_str = ", ".join(f"{t:.2f}s" for t in times)
        print(
            f"第 {rank} 名: {m:<28} | 平均耗时: {avg_t:6.2f}s | 最小: {min_t:5.2f}s | 最大: {max_t:6.2f}s | 各轮: [{rounds_str}]"
        )

