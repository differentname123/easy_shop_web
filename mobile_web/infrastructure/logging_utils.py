# -*- coding: utf-8 -*-
"""应用与后台脚本统一日志设施；仅显式启动时配置，不在 import 时创建文件。"""
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mobile_web")
_LOCK = threading.RLock()
_SIGNATURE = None

_SENSITIVE_EXC_PATTERN = re.compile(
    r"(https?://\S+|(?:app_?key|secret|sign|token|auth_token|access_token|password)\s*[=:]\s*[^\s,&;]+)",
    re.IGNORECASE,
)


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 下多进程或文件被占用时安全跳过本次滚动，避免抛 PermissionError 中断业务。"""

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            if self.stream is None:
                self.stream = self._open()
            current_time = int(time.time())
            self.rolloverAt = self.computeRollover(current_time)


def get_app_logger(name: Optional[str] = None) -> logging.Logger:
    """获取归属于 `mobile_web` 层级的子日志器，确保适配器/客户端日志统一进入主日志通道。"""
    if not name:
        return logger
    clean = str(name).strip()
    if clean == "mobile_web" or clean.startswith("mobile_web."):
        return logging.getLogger(clean)
    return logging.getLogger(f"mobile_web.{clean}")


def is_file_logging_disabled() -> bool:
    """当环境显式设置 WEB_LOG_DIR='-' 时禁止创建任何日志文件（测试/容器 stdout 模式）。"""
    return os.environ.get("WEB_LOG_DIR", "").strip() == "-"


def format_exc_summary(exc: BaseException, *, include_message: bool = True, max_len: int = 160) -> str:
    """将异常浓缩为脱敏的单行摘要，防止签名 URL 或敏感凭据泄露到日志中。"""
    exc_type = type(exc).__name__
    if not include_message:
        return exc_type
    raw_msg = str(exc or "").strip().replace("\r", " ").replace("\n", " ")
    if not raw_msg:
        return exc_type
    clean_msg = _SENSITIVE_EXC_PATTERN.sub("[REDACTED]", raw_msg)
    if len(clean_msg) > max_len:
        clean_msg = clean_msg[:max_len] + "..."
    return f"{exc_type}: {clean_msg}"


PROVEN_NON_OPS_ERROR_CODES = frozenset({
    "PRODUCT_OFF_PROMOTION",
    "AUTH_REQUIRED",
    "UNSUPPORTED_TB_NUMERIC_ID",
    "UNSUPPORTED_JD_NUMERIC_SKU",
    "UNSUPPORTED_PDD_ID_ONLY_URL",
    "AMBIGUOUS_PLATFORM_TEXT",
    "KEYWORD_NOT_LINK",
    "UNSUPPORTED_URL_DOMAIN",
    "COUPON_NOT_FOUND",
    "INVALID_USER_INPUT",
})


def is_non_ops_error_code(error_code: object) -> bool:
    """Fail-Closed：仅已实证确定非运维/代码故障的业务拒单码返回 True。"""
    return str(error_code or "").strip().upper() in PROVEN_NON_OPS_ERROR_CODES


def severity_icon(*, error_code: object = None, non_ops_ok: bool = False) -> str:
    """统一日志红叉准入开关：默认一律打 ❌，仅确定完全不是问题的状态才降级为 ⚠️。"""
    return "⚠️" if (non_ops_ok or is_non_ops_error_code(error_code)) else "❌"


def configure_console_encoding():
    """Windows 的重定向输出也使用 UTF-8，避免云端/本地终端日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass


def configure_logging(settings):
    global _SIGNATURE
    log_dir = settings.log_dir
    signature = (log_dir, settings.log_level)
    with _LOCK:
        owned = [handler for handler in logger.handlers if getattr(handler, "_mobile_web_handler", False)]
        if _SIGNATURE == signature and owned:
            return logger
        for handler in owned:
            logger.removeHandler(handler)
            handler.close()
        formatter = logging.Formatter("%(asctime)sZ [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
        formatter.converter = time.gmtime
        handlers = [logging.StreamHandler(sys.stdout)]
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(WindowsSafeTimedRotatingFileHandler(
                log_dir / "mobile_web.log", when="midnight", backupCount=30,
                encoding="utf-8", delay=True, utc=True,
            ))
        for handler in handlers:
            handler._mobile_web_handler = True
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        logger.setLevel(settings.log_level)
        logger.propagate = False
        _SIGNATURE = signature
        return logger


def configure_named_logger(
    name: str,
    *,
    log_path: Optional[Path | str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
    stream_level: Optional[int] = None,
    timed_midnight: bool = False,
    timed_backup_count: int = 7,
) -> logging.Logger:
    """幂等配置独立命名日志器（后台脚本/AI 网关共用），严格尊重 WEB_LOG_DIR='-' 禁文件策略。"""
    target_logger = logging.getLogger(name)
    resolved_path = None if (log_path is None or is_file_logging_disabled()) else Path(log_path)
    sig = (resolved_path, level, fmt, datefmt, timed_midnight, max_bytes, backup_count, stream_level)
    with _LOCK:
        owned = [h for h in target_logger.handlers if getattr(h, "_named_logger_handler", False)]
        if getattr(target_logger, "_named_logger_sig", None) == sig and owned:
            return target_logger
        for h in owned:
            target_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        formatter = logging.Formatter(fmt, datefmt=datefmt)
        handlers = []
        sh = logging.StreamHandler(sys.stdout)
        if stream_level is not None:
            sh.setLevel(stream_level)
        handlers.append(sh)
        if resolved_path is not None:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            if timed_midnight:
                fh = WindowsSafeTimedRotatingFileHandler(
                    resolved_path, when="midnight", backupCount=timed_backup_count,
                    encoding="utf-8", delay=True,
                )
            else:
                fh = RotatingFileHandler(
                    resolved_path, maxBytes=max_bytes, backupCount=backup_count,
                    encoding="utf-8", delay=True,
                )
            handlers.append(fh)
        for h in handlers:
            h._named_logger_handler = True
            h.setFormatter(formatter)
            target_logger.addHandler(h)
        target_logger.setLevel(level)
        target_logger.propagate = False
        target_logger._named_logger_sig = sig
        return target_logger
