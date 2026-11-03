# -*- coding: utf-8 -*-
"""应用命名日志；仅显式启动时配置，不在 import 时创建文件。"""
import logging
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger("mobile_web")
_LOCK = threading.RLock()
_SIGNATURE = None


def configure_console_encoding():
    """Windows 的重定向输出也使用 UTF-8，避免云端/本地终端日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def configure_logging(settings):
    global _SIGNATURE
    signature = (settings.log_dir, settings.log_level)
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
        if settings.log_dir is not None:
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(TimedRotatingFileHandler(
                settings.log_dir / "mobile_web.log", when="midnight", backupCount=30,
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
