# -*- coding: utf-8 -*-
"""
拼多多多人团互助——本平台海报外发桥接脚本（完全解耦外挂）。

定位与边界说明：
- 独立于主站 FastAPI 进程运行，持续监测 `pdd_multi_groups` 中由用户上传且已通过审核的
  `source="user_upload"` 在架拼团海报；
- 将可外发任务统一打包到隔离工作目录 `mobile_web/runtime/group_help_bridge/`：
  * `outbound_pending/<group_order_id>/poster.jpg`  （待发布高清海报）
  * `outbound_pending/<group_order_id>/task.json`   （商品标题、拼团价、差额人数、H5直达链接与完成标记路径）
  * `outbound_queue.json`                           （按紧迫度排好序的待发布总队列清单）
  * `export_state.json`                             （外部脚本私有状态账本，绝不污染主数据库）
- 闭环联动机制：
  1) 当本平台某团被标记满员、过期或下架时，本脚本自动将其状态置为 `withdrawn` 并清理 `outbound_pending/<gid>/`，
     防止外部脚本发出已满失效海报；
  2) 当您的电脑微信桌面端自动化脚本将海报成功发到目标小程序后，只需在对应任务目录创建一个
     `done.flag` 文件（或调用 `python ... --mark-done <gid>`），下一轮轮询即自动归档为 `done` 并清理队列。

用法：
  python mobile_web/scripts/group_help_exporter.py --once
  python mobile_web/scripts/group_help_exporter.py --interval 15
  python mobile_web/scripts/group_help_exporter.py --list
  python mobile_web/scripts/group_help_exporter.py --mark-done 3501385058150301111
  python mobile_web/scripts/group_help_exporter.py --mark-failed 3501385058150301111 --reason "小程序审核拒绝"
"""
import argparse
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

CURRENT_DIR = Path(__file__).resolve().parent
MOBILE_WEB_DIR = CURRENT_DIR.parent
for _p in (str(MOBILE_WEB_DIR), str(CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models.group_help import (
    GROUP_STATUS_ACTIVE,
    PARSE_STATUS_PARSED,
    SOURCE_USER_UPLOAD,
    format_group_doc,
)
from repositories.group_help_repo import GroupHelpRepository

_DEFAULT_BRIDGE_DIR = MOBILE_WEB_DIR / "runtime" / "group_help_bridge"
_DEFAULT_POSTER_DIR = MOBILE_WEB_DIR / "runtime" / "group_posters"
_TERMINAL_STATES = ("done", "failed", "withdrawn")
_STATE_RETENTION_SECONDS = 7 * 86400


def _setup_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("group_help_exporter")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        file_handler = RotatingFileHandler(
            log_dir / "exporter.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        log.addHandler(file_handler)
        log.addHandler(stream_handler)
    log.propagate = False
    return log


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=dst.suffix or ".bin", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copy2(src, tmp_name)
        os.replace(tmp_name, dst)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class GroupHelpExporter:
    """本平台真实海报拼团外发任务打包与生命周期同步器。"""

    def __init__(
        self,
        repo: Optional[GroupHelpRepository] = None,
        *,
        bridge_dir: Optional[Path] = None,
        poster_dir: Optional[Path] = None,
        limit: int = 30,
        logger: Optional[logging.Logger] = None,
    ):
        self.repo = repo if repo is not None else GroupHelpRepository()
        self.bridge_dir = Path(bridge_dir) if bridge_dir is not None else _DEFAULT_BRIDGE_DIR
        self.poster_dir = Path(poster_dir) if poster_dir is not None else _DEFAULT_POSTER_DIR
        self.pending_dir = self.bridge_dir / "outbound_pending"
        self.state_file = self.bridge_dir / "export_state.json"
        self.queue_file = self.bridge_dir / "outbound_queue.json"
        self.limit = max(1, min(int(limit), 200))
        self.log = logger or _setup_logger(self.bridge_dir)

    def _load_state(self, now: float) -> Dict[str, Dict[str, Any]]:
        if not self.state_file.is_file():
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        items = raw.get("items") if isinstance(raw.get("items"), dict) else raw
        if not isinstance(items, dict):
            return {}
        cleaned: Dict[str, Dict[str, Any]] = {}
        cutoff = now - _STATE_RETENTION_SECONDS
        for gid, entry in items.items():
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("state") or "")
            ts = float(entry.get("finished_at") or entry.get("updated_at") or entry.get("exported_at") or now)
            if state in _TERMINAL_STATES and ts < cutoff:
                continue
            cleaned[str(gid)] = dict(entry)
        return cleaned

    def _save_state(self, state: Dict[str, Dict[str, Any]], now: float) -> None:
        _atomic_write_json(
            self.state_file,
            {
                "updated_at": int(now),
                "count": len(state),
                "items": state,
            },
        )

    def _resolve_poster_source(self, doc: Dict[str, Any]) -> Optional[Path]:
        raw_path = str(doc.get("poster_path") or "").strip()
        if raw_path:
            p = Path(raw_path)
            if p.is_file():
                return p
        gid = str(doc.get("group_order_id") or "").strip()
        if gid:
            fallback = self.poster_dir / f"{gid}.jpg"
            if fallback.is_file():
                return fallback
        return None

    def _cleanup_task_dir(self, gid: str) -> None:
        task_dir = self.pending_dir / gid
        if task_dir.is_dir():
            shutil.rmtree(task_dir, ignore_errors=True)

    @staticmethod
    def _build_task_payload(formatted: Dict[str, Any], task_dir: Path, exported_at: float) -> Dict[str, Any]:
        gid = formatted["group_order_id"]
        return {
            "group_order_id": gid,
            "entry_type": formatted.get("entry_type", "poster"),
            "source": formatted.get("source", SOURCE_USER_UPLOAD),
            "goods_name": formatted.get("goods_name", ""),
            "group_price": formatted.get("group_price"),
            "original_price": formatted.get("original_price"),
            "total_people": int(formatted.get("total_people") or 0),
            "remain_people": int(formatted.get("remain_people") or 1),
            "keywords": list(formatted.get("keywords") or []),
            "share_text": formatted.get("share_text", ""),
            "share_code": formatted.get("share_code", ""),
            "h5_join_url": formatted.get("h5_join_url", ""),
            "app_wake_url": formatted.get("app_wake_url", ""),
            "poster_path": str((task_dir / "poster.jpg").resolve()),
            "task_json_path": str((task_dir / "task.json").resolve()),
            "done_flag_path": str((task_dir / "done.flag").resolve()),
            "expires_at": int(formatted.get("expires_at") or 0),
            "remaining_seconds": int(formatted.get("remaining_seconds") or 0),
            "exported_at": int(exported_at),
        }

    def run_once(self, now: Optional[float] = None) -> Dict[str, Any]:
        current_ts = float(now if now is not None else time.time())
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        state = self._load_state(current_ts)

        exported_new = 0
        refreshed = 0
        completed_via_flag = 0
        withdrawn = 0

        # 1. 先检查所有既有 pending 任务是否已完成 (done.flag) 或站内已满员/过期撤回
        for gid, entry in list(state.items()):
            if entry.get("state") != "pending":
                continue
            task_dir = self.pending_dir / gid
            if (task_dir / "done.flag").exists():
                entry["state"] = "done"
                entry["finished_at"] = int(current_ts)
                entry["updated_at"] = int(current_ts)
                entry["reason"] = "detected done.flag"
                self._cleanup_task_dir(gid)
                completed_via_flag += 1
                continue

            doc = self.repo.get_by_group_order_id(gid)
            is_still_active = bool(
                doc
                and doc.get("parse_status") == PARSE_STATUS_PARSED
                and doc.get("group_status") == GROUP_STATUS_ACTIVE
                and int(doc.get("remain_people") or 0) > 0
                and float(doc.get("expires_at") or 0) > current_ts
            )
            if not is_still_active:
                entry["state"] = "withdrawn"
                entry["finished_at"] = int(current_ts)
                entry["updated_at"] = int(current_ts)
                entry["reason"] = "local group full, expired, or removed"
                self._cleanup_task_dir(gid)
                withdrawn += 1

        # 2. 扫描当前可外发的真实用户海报拼团
        active_docs = self.repo.list_active_for_export(
            source=SOURCE_USER_UPLOAD,
            limit=self.limit,
            now=current_ts,
        )
        active_by_gid: Dict[str, Dict[str, Any]] = {
            str(d.get("group_order_id") or "").strip(): d for d in active_docs if d.get("group_order_id")
        }

        queue_items: List[Dict[str, Any]] = []
        for gid, doc in active_by_gid.items():
            entry = state.get(gid)
            if entry and entry.get("state") in _TERMINAL_STATES:
                continue

            src_poster = self._resolve_poster_source(doc)
            if src_poster is None:
                continue

            task_dir = self.pending_dir / gid
            if (task_dir / "done.flag").exists():
                state[gid] = {
                    **(entry or {}),
                    "state": "done",
                    "exported_at": int((entry or {}).get("exported_at") or current_ts),
                    "finished_at": int(current_ts),
                    "updated_at": int(current_ts),
                    "reason": "detected done.flag",
                }
                self._cleanup_task_dir(gid)
                completed_via_flag += 1
                continue

            formatted = format_group_doc(doc, now=current_ts)
            dst_poster = task_dir / "poster.jpg"
            dst_task = task_dir / "task.json"
            is_first_export = entry is None or not dst_poster.is_file() or not dst_task.is_file()

            if is_first_export:
                _atomic_copy_file(src_poster, dst_poster)
            exported_at = float((entry or {}).get("exported_at") or current_ts)
            payload = self._build_task_payload(formatted, task_dir, exported_at=exported_at)
            _atomic_write_json(dst_task, payload)

            state[gid] = {
                "state": "pending",
                "exported_at": int(exported_at),
                "updated_at": int(current_ts),
                "goods_name": payload["goods_name"],
                "group_price": payload["group_price"],
                "remain_people": payload["remain_people"],
                "poster_path": payload["poster_path"],
            }
            queue_items.append(payload)
            if entry is None:
                exported_new += 1
            else:
                refreshed += 1

        queue_items.sort(key=lambda it: (int(it.get("remain_people") or 1), -int(it.get("exported_at") or 0)))
        self._save_state(state, now=current_ts)
        _atomic_write_json(
            self.queue_file,
            {
                "updated_at": int(current_ts),
                "pending_count": len(queue_items),
                "tasks": queue_items,
            },
        )

        stats = {
            "timestamp": int(current_ts),
            "pending_total": len(queue_items),
            "exported_new": exported_new,
            "refreshed": refreshed,
            "completed_via_flag": completed_via_flag,
            "withdrawn": withdrawn,
        }
        self.log.info("外发队列同步完成: %s", stats)
        return stats

    def mark_done(self, gid: str, reason: str = "cli mark-done", now: Optional[float] = None) -> Dict[str, Any]:
        return self._mark_terminal(gid, state_val="done", reason=reason, now=now)

    def mark_failed(self, gid: str, reason: str = "cli mark-failed", now: Optional[float] = None) -> Dict[str, Any]:
        return self._mark_terminal(gid, state_val="failed", reason=reason, now=now)

    def _mark_terminal(
        self, gid: str, *, state_val: str, reason: str, now: Optional[float] = None
    ) -> Dict[str, Any]:
        current_ts = float(now if now is not None else time.time())
        clean_gid = str(gid or "").strip()
        state = self._load_state(current_ts)
        prev = state.get(clean_gid) or {}
        entry = {
            **prev,
            "state": state_val,
            "exported_at": int(prev.get("exported_at") or current_ts),
            "finished_at": int(current_ts),
            "updated_at": int(current_ts),
            "reason": str(reason or ""),
        }
        state[clean_gid] = entry
        self._cleanup_task_dir(clean_gid)
        self._save_state(state, now=current_ts)

        # 同步从 outbound_queue.json 中移除该条目
        if self.queue_file.is_file():
            try:
                qdata = json.loads(self.queue_file.read_text(encoding="utf-8"))
                tasks = [t for t in (qdata.get("tasks") or []) if str(t.get("group_order_id")) != clean_gid]
                _atomic_write_json(
                    self.queue_file,
                    {"updated_at": int(current_ts), "pending_count": len(tasks), "tasks": tasks},
                )
            except Exception:
                pass
        return entry

    def get_summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        current_ts = float(now if now is not None else time.time())
        state = self._load_state(current_ts)
        counts: Dict[str, int] = {"pending": 0, "done": 0, "failed": 0, "withdrawn": 0}
        for entry in state.values():
            st = str(entry.get("state") or "")
            counts[st] = counts.get(st, 0) + 1
        pending_list = [
            {"group_order_id": gid, **entry}
            for gid, entry in state.items()
            if entry.get("state") == "pending"
        ]
        return {"bridge_dir": str(self.bridge_dir), "counts": counts, "pending": pending_list}


def main() -> None:
    parser = argparse.ArgumentParser(description="拼多多多人团本平台海报外发桥接脚本")
    parser.add_argument("--once", action="store_true", help="仅执行一轮队列打包同步后退出")
    parser.add_argument("--interval", type=int, default=15, help="后台轮询间隔秒数（默认 15）")
    parser.add_argument("--limit", type=int, default=30, help="单轮最多导出的在架海报数量（默认 30）")
    parser.add_argument("--bridge-dir", type=str, default="", help="自定义外发桥接目录")
    parser.add_argument("--list", action="store_true", help="查看当前外发队列统计并退出")
    parser.add_argument("--mark-done", type=str, default="", help="将指定团单号标记为已外发完成")
    parser.add_argument("--mark-failed", type=str, default="", help="将指定团单号标记为外发失败")
    parser.add_argument("--reason", type=str, default="", help="配合 --mark-failed 记录失败原因")
    args = parser.parse_args()

    bridge_path = Path(args.bridge_dir) if args.bridge_dir else _DEFAULT_BRIDGE_DIR
    log = _setup_logger(bridge_path)
    exporter = GroupHelpExporter(bridge_dir=bridge_path, limit=args.limit, logger=log)

    if args.mark_done:
        res = exporter.mark_done(args.mark_done, reason=args.reason or "cli mark-done")
        print(json.dumps({"group_order_id": args.mark_done, **res}, ensure_ascii=False, indent=2))
        return

    if args.mark_failed:
        res = exporter.mark_failed(args.mark_failed, reason=args.reason or "cli mark-failed")
        print(json.dumps({"group_order_id": args.mark_failed, **res}, ensure_ascii=False, indent=2))
        return

    if args.list:
        print(json.dumps(exporter.get_summary(), ensure_ascii=False, indent=2))
        return

    if args.once:
        exporter.run_once()
        return

    stop_flag = {"stop": False}

    def _handle_stop(signum, _frame):
        log.info("收到退出信号 (%s)，将在当前轮结束后安全退出...", signum)
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

    interval = max(5, int(args.interval))
    log.info("海报外发桥接脚本已启动，bridge_dir=%s, interval=%ss", bridge_path, interval)
    while not stop_flag["stop"]:
        exporter.run_once()
        slept = 0
        while slept < interval and not stop_flag["stop"]:
            time.sleep(min(2, interval - slept))
            slept += 2


if __name__ == "__main__":
    main()
