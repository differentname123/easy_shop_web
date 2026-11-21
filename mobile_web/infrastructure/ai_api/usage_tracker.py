# -*- coding: utf-8 -*-
"""AI 模型调用统计跟踪器：Windows 无锁按进程角色物理分片落盘 + 节流批量刷新。"""
from __future__ import annotations

import atexit
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.api_governance import get_process_role
from infrastructure.logging_utils import is_file_logging_disabled
from infrastructure.runtime_io import atomic_write_json

_APP_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_RUNTIME_DIR = _APP_DIR / "logs" / "runtime"

FLUSH_INTERVAL_SEC = 10.0
FLUSH_EVENT_THRESHOLD = 20

_LOCK = threading.Lock()
# resolved_path_key (or "__memory__:<role>") -> state dict
_SHARD_STATES: Dict[str, Dict[str, Any]] = {}


def _load_existing_stats(path: Path) -> Tuple[Dict[str, Any], bool]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, False
    except Exception:
        return {}, True
    if not isinstance(raw, dict):
        return {}, True
    if "updated_at" in raw or "total_calls" in raw or "daily" in raw:
        return {}, True
    if any(not isinstance(v, dict) for v in raw.values()):
        return {}, True
    return raw, True


def _ensure_model_record(data: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    record = data.get(model_name)
    if not isinstance(record, dict):
        record = {}
        data[model_name] = record
    for key in ("总共调用次数", "成功次数", "失败次数", "中断次数", "准备失败次数"):
        val = record.get(key)
        if type(val) is not int or val < 0:
            record[key] = 0
    expected_total = record["成功次数"] + record["失败次数"] + record["中断次数"]
    if record["总共调用次数"] != expected_total:
        record["总共调用次数"] = expected_total
    duration = record.setdefault("累计请求耗时秒", 0.0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        record["累计请求耗时秒"] = 0.0
    for key in (
        "最近调用时间（北京时间）",
        "最近完成时间（北京时间）",
        "最近报错信息时间（北京时间）",
        "最近成功时间（北京时间）",
    ):
        val = record.get(key)
        if not isinstance(val, str):
            record[key] = None
    record.setdefault("最近报错信息", None)
    record.setdefault("成功率", 0.0)
    record.setdefault("平均请求耗时秒", 0.0)
    return record


def _apply_event(record: Dict[str, Any], event: Dict[str, Any]) -> None:
    status = event["status"]
    if status == "❌ 准备失败":
        record["准备失败次数"] += 1
    else:
        record["总共调用次数"] += 1
        count_key = {"✅ 成功": "成功次数", "❌ 失败": "失败次数", "⚠️ 中断": "中断次数"}[status]
        record[count_key] += 1
        duration = float(record.get("累计请求耗时秒") or 0.0)
        record["累计请求耗时秒"] = round(duration + float(event.get("duration_seconds") or 0.0), 6)

    total = record["总共调用次数"]
    record["成功率"] = round(record["成功次数"] / total, 6) if total else 0.0
    record["平均请求耗时秒"] = round(record["累计请求耗时秒"] / total, 6) if total else 0.0

    call_time = event.get("call_time_bj")
    finished_time = event.get("finished_time_bj")
    if call_time and (not record["最近调用时间（北京时间）"] or call_time >= record["最近调用时间（北京时间）"]):
        record["最近调用时间（北京时间）"] = call_time
    if finished_time and (not record["最近完成时间（北京时间）"] or finished_time >= record["最近完成时间（北京时间）"]):
        record["最近完成时间（北京时间）"] = finished_time
        record["status"] = status
        record["最近HTTP状态码"] = event.get("http_status")
        record["最近响应模型"] = event.get("response_model")
        record["最近trace_id"] = event.get("trace_id")
    if status == "✅ 成功" and finished_time and (
        not record["最近成功时间（北京时间）"] or finished_time >= record["最近成功时间（北京时间）"]
    ):
        record["最近成功时间（北京时间）"] = finished_time
    if event.get("error") and finished_time and (
        not record["最近报错信息时间（北京时间）"] or finished_time >= record["最近报错信息时间（北京时间）"]
    ):
        record["最近报错信息"] = event["error"]
        record["最近报错信息时间（北京时间）"] = finished_time


def _resolve_target(path_override: Optional[Path | str] = None) -> Tuple[Optional[Path], bool, str]:
    """返回 (target_path, is_explicit_override, state_key)。"""
    raw = path_override if path_override is not None else (os.environ.get("MODEL_USAGE_JSON_PATH") or "").strip() or None
    if raw is not None:
        p = Path(raw).expanduser().resolve()
        return p, True, str(p)
    role = get_process_role()
    if is_file_logging_disabled():
        return None, False, f"__memory__:{role}"
    p = (_DEFAULT_RUNTIME_DIR / f"model_usage_{role}.json").resolve()
    return p, False, str(p)


def _get_or_init_state(target: Optional[Path], state_key: str) -> Dict[str, Any]:
    state = _SHARD_STATES.get(state_key)
    if state is None:
        if target is not None:
            data, existed = _load_existing_stats(target)
        else:
            data, existed = {}, False
        state = {
            "path": target,
            "data": data,
            "existed": existed,
            "dirty_count": 0,
            "last_flush_mono": time.monotonic(),
        }
        _SHARD_STATES[state_key] = state
    return state


def record_usage_events(
    events: List[Dict[str, Any]],
    *,
    path_override: Optional[Path | str] = None,
) -> None:
    """记录一次请求的模型事件；显式覆盖路径立即落盘，默认分片按节流批量原子写盘。"""
    target, is_explicit, state_key = _resolve_target(path_override)
    now_mono = time.monotonic()
    snapshot_to_write: Optional[Tuple[Path, Dict[str, Any]]] = None

    with _LOCK:
        state = _get_or_init_state(target, state_key)
        data = state["data"]
        for event in events:
            record = _ensure_model_record(data, event["model_name"])
            _apply_event(record, event)
        state["dirty_count"] += len(events)

        if target is not None:
            if is_explicit:
                if events or not state["existed"]:
                    snapshot_to_write = (target, json.loads(json.dumps(data, ensure_ascii=False)))
                    state["dirty_count"] = 0
                    state["existed"] = True
                    state["last_flush_mono"] = now_mono
            elif state["dirty_count"] > 0 and (
                state["dirty_count"] >= FLUSH_EVENT_THRESHOLD
                or (now_mono - state["last_flush_mono"]) >= FLUSH_INTERVAL_SEC
            ):
                snapshot_to_write = (target, json.loads(json.dumps(data, ensure_ascii=False)))
                state["dirty_count"] = 0
                state["existed"] = True
                state["last_flush_mono"] = now_mono

    if snapshot_to_write is not None:
        out_path, payload = snapshot_to_write
        atomic_write_json(out_path, payload, fsync=False, indent=2)


def flush_usage(path_override: Optional[Path | str] = None) -> None:
    """将尚未落盘的模型统计分片立即原子写入磁盘。"""
    pending: List[Tuple[Path, Dict[str, Any]]] = []
    target_key = str(Path(path_override).expanduser().resolve()) if path_override is not None else None
    now_mono = time.monotonic()

    with _LOCK:
        for state_key, state in _SHARD_STATES.items():
            if target_key is not None and state_key != target_key:
                continue
            path = state.get("path")
            if path is not None and state.get("dirty_count", 0) > 0:
                pending.append((path, json.loads(json.dumps(state["data"], ensure_ascii=False))))
                state["dirty_count"] = 0
                state["existed"] = True
                state["last_flush_mono"] = now_mono

    for out_path, payload in pending:
        try:
            atomic_write_json(out_path, payload, fsync=False, indent=2)
        except Exception:
            pass


def _merge_model_records(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    if not isinstance(source, dict):
        return
    for model_name, rec in source.items():
        if not isinstance(rec, dict):
            continue
        dst = target.get(model_name)
        if dst is None:
            dst = _ensure_model_record({}, model_name)
            target[model_name] = dst
        dst["成功次数"] += max(0, int(rec.get("成功次数") or 0))
        dst["失败次数"] += max(0, int(rec.get("失败次数") or 0))
        dst["中断次数"] += max(0, int(rec.get("中断次数") or 0))
        dst["准备失败次数"] += max(0, int(rec.get("准备失败次数") or 0))
        dst["总共调用次数"] = dst["成功次数"] + dst["失败次数"] + dst["中断次数"]
        dur = float(rec.get("累计请求耗时秒") or 0.0)
        if math.isfinite(dur) and dur >= 0:
            dst["累计请求耗时秒"] = round(float(dst["累计请求耗时秒"]) + dur, 6)

        tot = dst["总共调用次数"]
        dst["成功率"] = round(dst["成功次数"] / tot, 6) if tot else 0.0
        dst["平均请求耗时秒"] = round(dst["累计请求耗时秒"] / tot, 6) if tot else 0.0

        c_at = rec.get("最近调用时间（北京时间）")
        if isinstance(c_at, str) and (not dst["最近调用时间（北京时间）"] or c_at >= dst["最近调用时间（北京时间）"]):
            dst["最近调用时间（北京时间）"] = c_at

        f_at = rec.get("最近完成时间（北京时间）")
        if isinstance(f_at, str) and (not dst["最近完成时间（北京时间）"] or f_at >= dst["最近完成时间（北京时间）"]):
            dst["最近完成时间（北京时间）"] = f_at
            if "status" in rec:
                dst["status"] = rec.get("status")
            if "最近HTTP状态码" in rec:
                dst["最近HTTP状态码"] = rec.get("最近HTTP状态码")
            if "最近响应模型" in rec:
                dst["最近响应模型"] = rec.get("最近响应模型")
            if "最近trace_id" in rec:
                dst["最近trace_id"] = rec.get("最近trace_id")

        s_at = rec.get("最近成功时间（北京时间）")
        if isinstance(s_at, str) and (not dst["最近成功时间（北京时间）"] or s_at >= dst["最近成功时间（北京时间）"]):
            dst["最近成功时间（北京时间）"] = s_at

        e_at = rec.get("最近报错信息时间（北京时间）")
        if isinstance(e_at, str) and (not dst["最近报错信息时间（北京时间）"] or e_at >= dst["最近报错信息时间（北京时间）"]):
            dst["最近报错信息时间（北京时间）"] = e_at
            dst["最近报错信息"] = rec.get("最近报错信息")


def get_combined_model_usage(runtime_dir: Optional[Path | str] = None) -> Dict[str, Any]:
    """合并读取各进程分片 (`model_usage_*.json`) 与历史遗留 `model_usage.json` 统计。"""
    base_dir = Path(runtime_dir).expanduser().resolve() if runtime_dir is not None else _DEFAULT_RUNTIME_DIR
    if runtime_dir is None:
        flush_usage()

    merged: Dict[str, Any] = {}
    candidate_files: List[Path] = []
    if base_dir.is_dir():
        legacy = base_dir / "model_usage.json"
        if legacy.is_file():
            candidate_files.append(legacy)
        candidate_files.extend(sorted(base_dir.glob("model_usage_*.json")))

    seen_paths = set()
    for fp in candidate_files:
        resolved = str(fp.resolve())
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        data, _ = _load_existing_stats(fp)
        _merge_model_records(merged, data)

    if runtime_dir is None:
        # 补充内存模式 (WEB_LOG_DIR="-") 下尚未落盘的当前进程分片
        role = get_process_role()
        mem_key = f"__memory__:{role}"
        with _LOCK:
            mem_state = _SHARD_STATES.get(mem_key)
            if mem_state is not None:
                _merge_model_records(merged, json.loads(json.dumps(mem_state["data"], ensure_ascii=False)))

    return merged


def reset_usage_tracker() -> None:
    """清空进程内缓存的模型调用统计状态（主要用于测试隔离）。"""
    with _LOCK:
        _SHARD_STATES.clear()


atexit.register(flush_usage)

__all__ = [
    "record_usage_events",
    "flush_usage",
    "get_combined_model_usage",
    "reset_usage_tracker",
]
