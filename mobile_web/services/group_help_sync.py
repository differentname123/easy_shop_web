# -*- coding: utf-8 -*-
"""
拼多多多人团互助外部桥接与采集同步服务 (`services.group_help_sync`)。
================================================================================
- `GroupHelpExporter` / `MiaozhuliPublisher`: 本平台 `source="user_upload"` 海报外发队列打包、done.flag 归档与秒助力口令同步；
- `GroupHelpImporter`: 闪助力 (`acdog.cn`) 与秒助力 (`5138zhuan.com`) 定时采集入库与两站皆无自动下架同步；
- 统一复用 `infrastructure.runtime_io.atomic_write_json` 与 `infrastructure.logging_utils.configure_named_logger`
  （严格遵守 `WEB_LOG_DIR="-"` 禁磁盘日志策略）。
================================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from infrastructure.logging_utils import configure_named_logger
from infrastructure.runtime_io import atomic_write_json
from models.group_help import (
    DEFAULT_GROUP_TTL_SECONDS,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    EXPORT_TARGET_MIAOZHULI,
    GROUP_STATUS_ACTIVE,
    GROUP_STATUS_EXPIRED,
    GROUP_STATUS_FULL,
    PARSE_STATUS_PARSED,
    SOURCE_USER_UPLOAD,
    extract_pdd_share_token,
    extract_smart_fields_from_pdd_share_text,
    format_group_doc,
    parse_pdd_group_share_fields,
)
from repositories.group_help_repo import GroupHelpRepository

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE_DIR = MOBILE_WEB_DIR / "runtime" / "group_help_bridge"
DEFAULT_POSTER_DIR = MOBILE_WEB_DIR / "runtime" / "group_posters"

ACDOG_API_URL = "https://acdog.cn/api/pin/deals"
MIAOZHULI_API_URL = "http://bz.5138zhuan.com/zd/api/list.php"

_DEFAULT_SYNC_INTERVAL_SECONDS = DEFAULT_SYNC_INTERVAL_SECONDS
_DEFAULT_EXTERNAL_TTL_SECONDS = DEFAULT_GROUP_TTL_SECONDS
_TERMINAL_STATES = ("done", "failed", "withdrawn")
_STATE_RETENTION_SECONDS = 7 * 86400
_CST_TZ = timezone(timedelta(hours=8))
_CORE_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,28}$")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _setup_bridge_logger(name: str, log_dir: Path, filename: str) -> logging.Logger:
    return configure_named_logger(
        name,
        log_path=log_dir / filename,
        max_bytes=2 * 1024 * 1024,
        backup_count=3,
        fmt="%(asctime)s [%(levelname)s] %(message)s",
    )


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


def _parse_epoch_seconds(val: Any, default_ts: float) -> float:
    if val is None or val == "" or isinstance(val, bool):
        return default_ts
    if isinstance(val, (int, float)):
        num = float(val)
        return (num / 1000.0) if num > 1e12 else num
    text = str(val).strip()
    if not text:
        return default_ts
    try:
        num = float(text)
        return (num / 1000.0) if num > 1e12 else num
    except ValueError:
        pass
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST_TZ)
        return dt.timestamp()
    except ValueError:
        return default_ts


def _extract_list_items(payload: Any) -> List[Dict[str, Any]]:
    candidates: Any = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            candidates = payload.get("items")
        elif isinstance(payload.get("list"), list):
            candidates = payload.get("list")
        elif isinstance(payload.get("data"), list):
            candidates = payload.get("data")
        elif isinstance(payload.get("data"), dict):
            nested = payload["data"]
            for key in ("items", "list", "deals", "rows"):
                if isinstance(nested.get(key), list):
                    candidates = nested[key]
                    break
    return [it for it in candidates if isinstance(it, dict)]


def _clean_external_goods_name(title_candidate: str, raw_text: str) -> str:
    title = str(title_candidate or "").strip()
    raw = str(raw_text or "").strip()
    smart = extract_smart_fields_from_pdd_share_text(raw)
    smart_title = str(smart.get("goods_name") or "").strip()
    if not title:
        return smart_title
    if smart_title and ("..." in title or "…" in title) and smart_title != title:
        return smart_title
    if "（" in smart_title and "）" in smart_title and "（" not in title:
        suffix_part = smart_title[smart_title.rfind("（") :]
        return f"{title}{suffix_part}"
    return title


class MiaozhuliPublisher:
    """秒助力 (`bz.5138zhuan.com`) 匿名设备身份外发客户端。"""

    INIT_URL = "http://bz.5138zhuan.com/zd/api/h5_init.php"
    PUBLISH_URL = "http://bz.5138zhuan.com/zd/api/h5_publish.php"

    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 10):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.session.headers.update({
            "User-Agent": _USER_AGENT,
            "Referer": "http://bz.5138zhuan.com/zd/",
            "Origin": "http://bz.5138zhuan.com",
            "Accept": "application/json",
        })
        self._initialized = False

    def close(self) -> None:
        if callable(getattr(self.session, "close", None)):
            try:
                self.session.close()
            except Exception:
                pass

    def ensure_session(self) -> bool:
        if self._initialized and "zd_device_uid" in self.session.cookies:
            return True
        try:
            resp = self.session.get(self.INIT_URL, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    self._initialized = True
                    return True
        except Exception:
            pass
        return False

    def publish_deal(self, doc: Dict[str, Any]) -> Tuple[bool, str, str]:
        if not self.ensure_session():
            return False, "", "秒助力设备身份初始化失败"

        share_text = str(doc.get("share_text") or "").strip()
        if not share_text:
            return False, "", "缺少有效的拼团口令文本"

        title = str(doc.get("goods_name") or "")[:80].strip()
        price = doc.get("group_price")
        team_size = doc.get("total_people") or 2

        payload = {
            "title": title,
            "url": "",
            "share_text": share_text,
            "price": float(price) if price is not None else 0.0,
            "team_size": int(team_size) if team_size else 2,
            "hp": "",
        }
        try:
            resp = self.session.post(self.PUBLISH_URL, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                return False, "", f"HTTP {resp.status_code}"
            res_json = resp.json()
            if res_json.get("code") == 0:
                remote_id = str(res_json.get("id") or "")
                return True, remote_id, ""
            return False, "", str(res_json.get("msg") or "对方拒绝发布")
        except Exception as exc:
            return False, "", str(exc)


class GroupHelpExporter:
    """本平台真实海报拼团外发任务打包与生命周期同步器。"""

    def __init__(
        self,
        repo: Optional[GroupHelpRepository] = None,
        *,
        bridge_dir: Optional[Path] = None,
        poster_dir: Optional[Path] = None,
        limit: int = 30,
        publisher: Optional[Any] = None,
        enable_remote_sync: bool = True,
        logger: Optional[logging.Logger] = None,
    ):
        self.repo = repo if repo is not None else GroupHelpRepository()
        self.bridge_dir = Path(bridge_dir) if bridge_dir is not None else DEFAULT_BRIDGE_DIR
        self.poster_dir = Path(poster_dir) if poster_dir is not None else DEFAULT_POSTER_DIR
        self.pending_dir = self.bridge_dir / "outbound_pending"
        self.state_file = self.bridge_dir / "export_state.json"
        self.queue_file = self.bridge_dir / "outbound_queue.json"
        self.limit = max(1, min(int(limit), 200))
        self.publisher = publisher or MiaozhuliPublisher()
        self.enable_remote_sync = bool(enable_remote_sync)
        self.log = logger or _setup_bridge_logger("group_help_exporter", self.bridge_dir, "exporter.log")

    def close(self) -> None:
        if callable(getattr(self.publisher, "close", None)):
            self.publisher.close()

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
        atomic_write_json(
            self.state_file,
            {"updated_at": int(now), "count": len(state), "items": state},
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

    def _sync_remote_miaozhuli(self, now: float) -> Dict[str, int]:
        stats = {"pending": 0, "success": 0, "failed": 0}
        if not self.enable_remote_sync:
            return stats

        pending_items = self.repo.list_pending_for_remote_export(
            target=EXPORT_TARGET_MIAOZHULI,
            limit=self.limit,
            now=now,
        )
        stats["pending"] = len(pending_items)
        if not pending_items:
            return stats

        for doc in pending_items:
            gid = str(doc.get("group_order_id") or "")
            name = str(doc.get("goods_name") or "")
            price = doc.get("group_price")
            remain = doc.get("remain_people")
            t0 = time.perf_counter()
            try:
                ok, remote_id, err_msg = self.publisher.publish_deal(doc)
                duration_ms = int((time.perf_counter() - t0) * 1000)
                if ok:
                    self.repo.record_export_result(
                        gid, target=EXPORT_TARGET_MIAOZHULI, success=True, remote_id=remote_id, now=now,
                    )
                    stats["success"] += 1
                    self.log.info(
                        "[EXPORTER-SUCCESS] [秒助力] 发布成功 | 团单: %s | 商品: %s (¥%s, 差%s人) | 远端ID: %s | 耗时: %sms",
                        gid, name[:28], price, remain, remote_id, duration_ms,
                    )
                else:
                    self.repo.record_export_result(
                        gid, target=EXPORT_TARGET_MIAOZHULI, success=False, error_msg=err_msg, now=now,
                    )
                    stats["failed"] += 1
                    self.log.warning(
                        "[EXPORTER-FAIL] [秒助力] 发布失败 | 团单: %s | 商品: %s | 原因: %s | 耗时: %sms",
                        gid, name[:28], err_msg, duration_ms,
                    )
            except Exception as exc:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                self.repo.record_export_result(
                    gid, target=EXPORT_TARGET_MIAOZHULI, success=False, error_msg=str(exc), now=now,
                )
                stats["failed"] += 1
                self.log.error(
                    "[EXPORTER-ERROR] [秒助力] 异常中断 | 团单: %s | 异常: %s | 耗时: %sms",
                    gid, exc, duration_ms,
                )
        return stats

    def run_once(self, now: Optional[float] = None) -> Dict[str, Any]:
        loop_t0 = time.perf_counter()
        current_ts = float(now if now is not None else time.time())
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        state = self._load_state(current_ts)

        exported_new = refreshed = completed_via_flag = withdrawn = 0

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

        active_docs = self.repo.list_active_for_export(
            source=SOURCE_USER_UPLOAD, limit=self.limit, now=current_ts,
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
            if entry is None or not dst_poster.is_file() or not dst_task.is_file():
                _atomic_copy_file(src_poster, dst_poster)
            exported_at = float((entry or {}).get("exported_at") or current_ts)
            payload = self._build_task_payload(formatted, task_dir, exported_at=exported_at)
            atomic_write_json(dst_task, payload)

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
        atomic_write_json(
            self.queue_file,
            {"updated_at": int(current_ts), "pending_count": len(queue_items), "tasks": queue_items},
        )

        remote_stats = self._sync_remote_miaozhuli(now=current_ts)
        stats = {
            "timestamp": int(current_ts),
            "pending_total": len(queue_items),
            "exported_new": exported_new,
            "refreshed": refreshed,
            "completed_via_flag": completed_via_flag,
            "withdrawn": withdrawn,
            "remote_miaozhuli": remote_stats,
        }
        total_time_ms = int((time.perf_counter() - loop_t0) * 1000)
        has_activity = bool(
            exported_new or completed_via_flag or withdrawn or remote_stats["success"] or remote_stats["failed"]
        )
        log_fn = self.log.info if has_activity else self.log.debug
        log_fn(
            "[PDD-EXPORTER] %s | 耗时=%sms | 本地海报: 待办=%s 新增=%s 归档=%s 撤回=%s | 秒助力: 待办=%s 成功=%s 失败=%s",
            "轮询完成" if has_activity else "空闲轮询",
            total_time_ms,
            stats["pending_total"],
            exported_new,
            completed_via_flag,
            withdrawn,
            remote_stats["pending"],
            remote_stats["success"],
            remote_stats["failed"],
        )
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

        if self.queue_file.is_file():
            try:
                qdata = json.loads(self.queue_file.read_text(encoding="utf-8"))
                tasks = [t for t in (qdata.get("tasks") or []) if str(t.get("group_order_id")) != clean_gid]
                atomic_write_json(
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


class GroupHelpImporter:
    """外部多人团轮询与入库同步器。"""

    def __init__(
        self,
        repo: Optional[GroupHelpRepository] = None,
        *,
        http_get: Optional[Callable[..., Any]] = None,
        external_ttl_seconds: int = _DEFAULT_EXTERNAL_TTL_SECONDS,
        pages: int = 0,
        dry_run: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        self.repo = repo if repo is not None else GroupHelpRepository()
        self._http_get = http_get
        self.external_ttl_seconds = max(1800, min(int(external_ttl_seconds), DEFAULT_GROUP_TTL_SECONDS))
        self.pages = max(0, int(pages))
        self.dry_run = bool(dry_run)
        self.log = logger or _setup_bridge_logger("group_help_importer", DEFAULT_BRIDGE_DIR, "importer.log")

    def _fetch_json(self, url: str, params: Dict[str, Any]) -> Any:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if self._http_get is not None:
            resp = self._http_get(url, params=params, headers=headers, timeout=12)
        else:
            resp = requests.get(url, params=params, headers=headers, timeout=12)
        if getattr(resp, "status_code", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status_code', '?')} from {url}")
        return resp.json()

    def _upsert_external_candidate(
        self,
        *,
        source: str,
        raw_share_text: str,
        goods_name: str,
        group_price: Any,
        total_people: Any,
        remain_people: Any,
        publisher_nick: str,
        created_at: float,
        upstream_expires_at: Optional[float],
        now: float,
        stats: Dict[str, int],
    ) -> Optional[str]:
        parsed = parse_pdd_group_share_fields(
            share_text=raw_share_text,
            goods_name=goods_name,
            group_price=group_price,
            total_people=total_people,
            remain_people=remain_people,
            publisher_nick=publisher_nick,
        )
        gid = str(parsed.get("group_order_id") or "").strip()
        if parsed["needs_url_resolve"] and not gid:
            stats["skipped_unresolved"] += 1
            return None

        if gid:
            existing = self.repo.get_by_group_order_id(gid)
            if existing is not None and bool(existing.get("user_takedown")):
                stats["skipped_user_takedown"] = stats.get("skipped_user_takedown", 0) + 1
                return gid

        local_cap = now + self.external_ttl_seconds
        pdd_cap = created_at + DEFAULT_GROUP_TTL_SECONDS
        expires_at = min(local_cap, pdd_cap)
        if upstream_expires_at is not None and upstream_expires_at > 0:
            expires_at = min(expires_at, upstream_expires_at)

        if expires_at <= now:
            stats["skipped_expired"] += 1
            return None

        if self.dry_run:
            stats["dry_run_ok"] += 1
            return gid

        doc_input: Dict[str, Any] = {
            **parsed,
            "source": source,
            "poster_path": "",
            "created_at": min(created_at, now),
            "expires_at": expires_at,
        }
        _, is_new, _ = self.repo.upsert_parsed_group(
            doc_input,
            ttl_seconds=DEFAULT_GROUP_TTL_SECONDS,
            now=now,
            preserve_local_takedown=True,
            check_similar=False,
        )
        if is_new:
            stats["upserted"] += 1
        else:
            stats["unchanged"] += 1
        return gid

    def _sync_acdog(self, now: float) -> Tuple[Dict[str, int], Set[str]]:
        stats = {
            "fetched": 0, "upserted": 0, "unchanged": 0, "synced_inactive": 0,
            "skipped_invalid": 0, "skipped_expired": 0, "skipped_unresolved": 0,
            "skipped_user_takedown": 0, "dry_run_ok": 0,
        }
        active_keys: Set[str] = set()
        page = 1
        max_pages = self.pages if self.pages > 0 else 50
        while page <= max_pages:
            payload = self._fetch_json(ACDOG_API_URL, {"page": page, "size": 50})
            items = _extract_list_items(payload)
            if not items:
                break
            stats["fetched"] += len(items)

            page_all_expired = True
            for item in items:
                platform = str(item.get("platform") or "").strip().lower()
                if platform and platform not in ("pdd", "pinduoduo"):
                    stats["skipped_invalid"] += 1
                    continue

                raw_text = str(item.get("rawText") or "").strip()
                core_code = str(item.get("coreCode") or "").strip()
                if core_code and _CORE_CODE_RE.match(core_code) and not extract_pdd_share_token(raw_text):
                    raw_text = f"{raw_text} ⇥{core_code}⇤".strip()

                token_code = extract_pdd_share_token(raw_text) or (
                    core_code if _CORE_CODE_RE.match(core_code) else ""
                )
                gid_guess = f"tok_{token_code}" if token_code else ""

                status_str = str(item.get("status") or "").strip().lower()
                spots_left = item.get("spotsLeft")
                marked_full = int(item.get("markedFullCount") or 0)
                marked_invalid = int(item.get("markedInvalidCount") or 0)
                created_at = _parse_epoch_seconds(item.get("createdAt"), now)
                upstream_expires = _parse_epoch_seconds(item.get("expiresAt"), 0.0) or None

                if (now - created_at) < DEFAULT_GROUP_TTL_SECONDS:
                    page_all_expired = False

                is_full = (
                    status_str in ("full", "completed", "finished")
                    or (spots_left is not None and not isinstance(spots_left, bool) and int(spots_left) <= 0)
                    or marked_full >= 2
                )
                is_expired = (
                    status_str in ("expired", "invalid", "deleted", "closed")
                    or marked_invalid >= 2
                    or (upstream_expires is not None and upstream_expires <= now)
                )
                if is_full or is_expired:
                    if not self.dry_run and gid_guess:
                        target_status = GROUP_STATUS_FULL if is_full else GROUP_STATUS_EXPIRED
                        if self.repo.sync_external_status(
                            gid_guess, source="acdog", new_status=target_status, now=now
                        ):
                            stats["synced_inactive"] += 1
                    stats["skipped_expired"] += 1
                    continue

                cleaned_title = _clean_external_goods_name(str(item.get("titleDisplay") or ""), raw_text)
                try:
                    upserted_gid = self._upsert_external_candidate(
                        source="acdog",
                        raw_share_text=raw_text,
                        goods_name=cleaned_title,
                        group_price=item.get("price"),
                        total_people=item.get("groupSize") or 0,
                        remain_people=spots_left if spots_left is not None else 1,
                        publisher_nick=str(item.get("publisherDisplayName") or ""),
                        created_at=created_at,
                        upstream_expires_at=upstream_expires,
                        now=now,
                        stats=stats,
                    )
                    if upserted_gid:
                        active_keys.add(upserted_gid)
                    if gid_guess:
                        active_keys.add(gid_guess)
                    if token_code:
                        active_keys.add(token_code)
                except ValueError:
                    stats["skipped_invalid"] += 1

            has_more = bool(payload.get("data", {}).get("hasMore", True)) if isinstance(payload, dict) else True
            if not has_more or page_all_expired:
                break
            page += 1

        return stats, active_keys

    def _sync_miaozhuli(self, now: float) -> Tuple[Dict[str, int], Set[str]]:
        stats = {
            "fetched": 0, "upserted": 0, "unchanged": 0, "synced_inactive": 0,
            "skipped_invalid": 0, "skipped_expired": 0, "skipped_unresolved": 0,
            "skipped_user_takedown": 0, "dry_run_ok": 0,
        }
        active_keys: Set[str] = set()
        page = 1
        max_pages = self.pages if self.pages > 0 else 50
        while page <= max_pages:
            payload = self._fetch_json(MIAOZHULI_API_URL, {"tab": "all", "page": page})
            items = _extract_list_items(payload)
            if not items:
                break
            stats["fetched"] += len(items)

            for item in items:
                share_text = str(item.get("share_text") or "").strip()
                url_val = str(item.get("url") or "").strip()
                if url_val and url_val not in share_text:
                    raw_text = f"{share_text} {url_val}".strip()
                else:
                    raw_text = share_text or url_val

                token_code = extract_pdd_share_token(raw_text)
                gid_guess = f"tok_{token_code}" if token_code else ""

                status_str = str(item.get("status") or "").strip().lower()
                marks = item.get("marks") if isinstance(item.get("marks"), dict) else {}
                full_marks = int(marks.get("full") or 0)
                exp_marks = int(marks.get("expire") or 0)
                time_left_raw = item.get("time_left")
                time_left_sec: Optional[float] = None
                if time_left_raw is not None and not isinstance(time_left_raw, bool):
                    try:
                        time_left_sec = float(time_left_raw)
                    except (TypeError, ValueError):
                        time_left_sec = None

                created_at = _parse_epoch_seconds(item.get("created_at"), now)
                upstream_expires = (
                    (now + time_left_sec)
                    if time_left_sec is not None
                    else (created_at + self.external_ttl_seconds)
                )

                is_full = status_str in ("full", "completed", "finished") or full_marks >= 1
                is_expired = (
                    status_str in ("expired", "invalid", "deleted", "closed")
                    or exp_marks >= 1
                    or (time_left_sec is not None and time_left_sec <= 0)
                )
                if is_full or is_expired:
                    if not self.dry_run and gid_guess:
                        target_status = GROUP_STATUS_FULL if is_full else GROUP_STATUS_EXPIRED
                        if self.repo.sync_external_status(
                            gid_guess, source="miaozhuli", new_status=target_status, now=now
                        ):
                            stats["synced_inactive"] += 1
                    stats["skipped_expired"] += 1
                    continue

                cleaned_title = _clean_external_goods_name(str(item.get("title") or ""), raw_text)
                try:
                    upserted_gid = self._upsert_external_candidate(
                        source="miaozhuli",
                        raw_share_text=raw_text,
                        goods_name=cleaned_title,
                        group_price=item.get("price"),
                        total_people=item.get("team_size") or 0,
                        remain_people=1,
                        publisher_nick=str(item.get("nick") or ""),
                        created_at=created_at,
                        upstream_expires_at=upstream_expires,
                        now=now,
                        stats=stats,
                    )
                    if upserted_gid:
                        active_keys.add(upserted_gid)
                    if gid_guess:
                        active_keys.add(gid_guess)
                    if token_code:
                        active_keys.add(token_code)
                except ValueError:
                    stats["skipped_invalid"] += 1

            has_more = bool(payload.get("has_more", True)) if isinstance(payload, dict) else True
            if not has_more:
                break
            page += 1

        return stats, active_keys

    def run_once(self, now: Optional[float] = None) -> Dict[str, Any]:
        loop_t0 = time.perf_counter()
        current_ts = float(now if now is not None else time.time())
        summary: Dict[str, Any] = {
            "timestamp": int(current_ts),
            "sources": {},
            "offline_synced": 0,
        }

        if not self.dry_run:
            try:
                self.repo.expire_outdated_groups(now=current_ts)
            except Exception as exc:
                self.log.warning("[IMPORTER-WARN] 清理站内过期拼团异常: %s", exc)

        seen_active_keys: Set[str] = set()
        has_source_error = False

        for source_name, handler in (("acdog", self._sync_acdog), ("miaozhuli", self._sync_miaozhuli)):
            try:
                st, source_keys = handler(current_ts)
                summary["sources"][source_name] = st
                seen_active_keys.update(source_keys)
            except Exception as exc:
                has_source_error = True
                self.log.warning("[IMPORTER-WARN] 采集源 [%s] 本轮失败 (跳过不中断): %s", source_name, exc)
                summary["sources"][source_name] = {"error": str(exc)}

        if not self.dry_run and not has_source_error:
            try:
                summary["offline_synced"] = self.repo.mark_missing_external_groups_offline(
                    seen_active_keys, now=current_ts,
                )
            except Exception as exc:
                self.log.warning("[IMPORTER-WARN] 两站皆无自动下架同步异常: %s", exc)

        total_time_ms = int((time.perf_counter() - loop_t0) * 1000)
        acdog_st = summary["sources"].get("acdog", {})
        miao_st = summary["sources"].get("miaozhuli", {})

        def _fmt_src(st: Any) -> str:
            if not isinstance(st, dict) or "error" in st:
                return f"异常 ({st.get('error', '未知错误') if isinstance(st, dict) else st})"
            return (
                f"抓取 {st.get('fetched', 0):3d} | 新增 {st.get('upserted', 0):2d} | "
                f"刷新 {st.get('unchanged', 0):2d} | 满员/下架同步 {st.get('synced_inactive', 0):2d} | "
                f"用户下架跳过 {st.get('skipped_user_takedown', 0):2d} | "
                f"过滤垃圾/过期 {st.get('skipped_invalid', 0) + st.get('skipped_expired', 0):2d}"
            )

        def _sum_key(k: str) -> int:
            return (
                (acdog_st.get(k, 0) if isinstance(acdog_st, dict) else 0)
                + (miao_st.get(k, 0) if isinstance(miao_st, dict) else 0)
            )

        total_fetched = _sum_key("fetched")
        offline_count = int(summary.get("offline_synced") or 0)
        if total_fetched == 0 and offline_count == 0 and not has_source_error:
            self.log.debug("[PDD-IMPORTER] 空闲轮询 | 耗时=%sms | 无新增或变更", total_time_ms)
            return summary

        try:
            active_in_hall = self.repo.col.count_documents({
                "parse_status": "parsed",
                "group_status": "active",
                "expires_at": {"$gt": current_ts},
                "remain_people": {"$gt": 0},
            })
        except Exception:
            active_in_hall = -1
        hall_str = f"{active_in_hall} 款" if active_in_hall >= 0 else "未知"
        page_mode_str = f"{self.pages}页" if self.pages > 0 else "全量(直至无更多/超时)"

        self.log.info(
            "\n========================================================================================\n"
            "[PDD-IMPORTER] 采集轮询完成 | 总耗时: %sms | 翻页模式: %s\n"
            "  ├─ 闪助力 [acdog]   : %s\n"
            "  ├─ 秒助力 [miaozhuli]: %s\n"
            "  └─ 全局汇总: 扫描 %d 款 | 实际新增 %d 款 | 状态刷新 %d 款 | 同步下架 %d 款 | 两站皆无自动下架 %d 款 | 用户下架保持拦截 %d 款 | 大厅在售: %s\n"
            "========================================================================================",
            total_time_ms,
            page_mode_str,
            _fmt_src(acdog_st),
            _fmt_src(miao_st),
            total_fetched,
            _sum_key("upserted"),
            _sum_key("unchanged"),
            _sum_key("synced_inactive"),
            offline_count,
            _sum_key("skipped_user_takedown"),
            hall_str,
        )
        return summary
