# -*- coding: utf-8 -*-
"""统一运行时文件 I/O、原子持久化、有界 JSONL 日志、提示词热加载缓存与脚本生命周期引导。"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

_PROMPT_CACHE_LOCK = threading.Lock()
_JSONL_LOCK = threading.Lock()
# path_str -> (mtime_ns, size, content)
_PROMPT_CACHE: Dict[str, Tuple[int, int, str]] = {}


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    fsync: bool = False,
    retries: int = 5,
    indent: Optional[int] = 2,
) -> None:
    """原子写入 JSON 文件：同目录临时文件 + 可选 fsync + Windows 并发替换退避重试。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt >= attempts - 1:
                    raise
                time.sleep(0.03 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def append_bounded_jsonl(
    path: Path | str,
    lines: Iterable[str],
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 0,
) -> None:
    """线程安全且受磁盘容量上限约束地追加 JSONL；支持 .1/.2 滚动备份或就地折半截断。"""
    items = [line if line.endswith("\n") else f"{line}\n" for line in lines if line]
    if not items:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(items)
    encoded_len = len(payload.encode("utf-8", errors="ignore"))
    with _JSONL_LOCK:
        if backup_count > 0 and max_bytes > 0 and target.exists():
            try:
                cur_size = target.stat().st_size
                if cur_size + encoded_len > max_bytes:
                    for idx in range(int(backup_count), 0, -1):
                        src = target if idx == 1 else Path(f"{target}.{idx - 1}")
                        dst = Path(f"{target}.{idx}")
                        if src.exists():
                            try:
                                if dst.exists():
                                    dst.unlink()
                                os.replace(src, dst)
                            except OSError:
                                pass
            except OSError:
                pass
        with target.open("a", encoding="utf-8") as fh:
            fh.write(payload)
        if backup_count > 0 or max_bytes <= 0:
            return
        try:
            size = target.stat().st_size
            if size <= max_bytes:
                return
            keep_bytes = max(1024, max_bytes // 2)
            with target.open("rb") as rf:
                rf.seek(max(0, size - keep_bytes))
                raw = rf.read()
            nl = raw.find(b"\n")
            trimmed = raw[nl + 1 :] if nl >= 0 else raw
            tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
            try:
                with tmp.open("wb") as wf:
                    wf.write(trimmed)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError:
            pass


def should_write_runtime_artifact(path: Path | str, protected_dir: Path | str) -> bool:
    """当 WEB_LOG_DIR='-'（测试/无盘模式）且目标位于默认受保护日志目录内时跳过落盘；显式覆写为 tmp 路径时仍允许写入。"""
    if os.environ.get("WEB_LOG_DIR", "").strip() != "-":
        return True
    try:
        target = Path(path).resolve()
        base = Path(protected_dir).resolve()
        return target != base and base not in target.parents
    except OSError:
        return False


def load_prompt_cached(path: Path | str, *, encoding: str = "utf-8") -> str:
    """按文件 mtime_ns + size 自动热更新的线程安全提示词加载器。"""
    p = Path(path)
    key = str(p)
    stat = p.stat()
    sig = (int(stat.st_mtime_ns), int(stat.st_size))
    with _PROMPT_CACHE_LOCK:
        cached = _PROMPT_CACHE.get(key)
        if cached is not None and cached[:2] == sig:
            return cached[2]
    text = p.read_text(encoding=encoding)
    with _PROMPT_CACHE_LOCK:
        _PROMPT_CACHE[key] = (sig[0], sig[1], text)
    return text


def install_shutdown_signals(
    stop_event: threading.Event,
    *,
    logger: Optional[logging.Logger] = None,
    on_signal: Optional[Any] = None,
) -> None:
    """统一注册 SIGINT / SIGTERM 优雅停机处理器。"""
    def _handler(signum, _frame):
        if not stop_event.is_set():
            if logger is not None:
                logger.info("收到停止信号 (%s)，正在通知工作线程优雅退出...", signum)
            stop_event.set()
            if callable(on_signal):
                on_signal(signum)

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def interruptible_sleep(stop_event: threading.Event, seconds: float) -> bool:
    """可被停机事件立即唤醒的等待；返回 True 表示收到停机信号。"""
    if seconds <= 0:
        return stop_event.is_set()
    return stop_event.wait(timeout=float(seconds))


def bootstrap_script(
    role: str,
    logger_name: str,
    *,
    log_path: Optional[Path | str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
    configure_network: bool = True,
) -> logging.Logger:
    """后台/工具脚本统一起步器：控制台 UTF-8、进程角色标记、传输层配置与滚动日志。"""
    from infrastructure.api_governance import set_process_role
    from infrastructure.logging_utils import configure_console_encoding, configure_named_logger

    configure_console_encoding()
    set_process_role(role)
    if configure_network:
        try:
            from infrastructure.settings import get_settings
            from infrastructure.transport import configure_transport
            st = get_settings()
            configure_transport(dns_mode=st.pdd_dns_mode, trust_env=st.pdd_trust_env)
        except Exception:
            pass
    target_logger = configure_named_logger(
        logger_name,
        log_path=log_path,
        max_bytes=max_bytes,
        backup_count=backup_count,
        level=level,
        fmt=fmt,
        datefmt=datefmt,
    )
    if logger_name != "mobile_web":
        app_logger = logging.getLogger("mobile_web")
        if not any(getattr(h, "_mobile_web_handler", False) for h in app_logger.handlers):
            for prev_h in tuple(getattr(app_logger, "_script_bridge_handlers", ()) or ()):
                app_logger.removeHandler(prev_h)
            bridged_handlers = list(target_logger.handlers)
            for h in bridged_handlers:
                if h not in app_logger.handlers:
                    app_logger.addHandler(h)
            app_logger._script_bridge_handlers = bridged_handlers
            app_logger.setLevel(max(level, logging.WARNING))
            app_logger.propagate = False
    return target_logger
