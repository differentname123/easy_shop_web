# -*- coding: utf-8 -*-
"""渠道后台异步任务调度器：按 (owner, channel) 串行执行同步/上架/下架/对齐并持久化进度。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple
import uuid

from fastapi import HTTPException

from .channel_service import ChannelService
from .repo import WorkbenchRepository

LOGGER = logging.getLogger("xianyu_workbench.jobs")

ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
MAX_PENDING_JOBS_PER_CHANNEL = 20


def _iso_utc(val: Any) -> str:
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(val or "")


def _serialize_job(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    for key in ("created_at", "started_at", "finished_at", "updated_at"):
        out[key] = _iso_utc(out.get(key)) if out.get(key) else ""
    out["target_listing_ids"] = list(out.get("target_listing_ids") or [])
    out["target_item_ids"] = list(out.get("target_item_ids") or [])
    out["published"] = list(out.get("published") or [])
    out["downshelved"] = list(out.get("downshelved") or [])
    out["failed"] = list(out.get("failed") or [])
    out["total"] = int(out.get("total") or 0)
    out["completed"] = int(out.get("completed") or 0)
    out["cancel_requested"] = bool(out.get("cancel_requested"))
    return out


@dataclass
class _QueuedTask:
    job_id: str
    owner: str
    channel: str
    kind: str
    payload: Dict[str, Any]
    cancel_event: threading.Event = field(default_factory=threading.Event)


class ChannelJobRunner:
    """按账号与渠道隔离的串行后台任务执行器（不阻塞 HTTP 请求，支持断点恢复与安全取消）。"""

    def __init__(self, repo: WorkbenchRepository, channel_service: ChannelService) -> None:
        self.repo = repo
        self.channel_service = channel_service
        self._lock = threading.Lock()
        self._queues: Dict[Tuple[str, str], Deque[_QueuedTask]] = {}
        self._workers: Dict[Tuple[str, str], threading.Thread] = {}
        self._tasks_by_id: Dict[str, _QueuedTask] = {}
        self._active_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._shutting_down = False

    def recover_stale_jobs(self) -> int:
        with self._lock:
            active_ids = list(self._tasks_by_id.keys())
        return self.repo.mark_stale_channel_jobs_failed(active_ids)

    def _enqueue(
        self,
        *,
        owner: str,
        channel: str,
        kind: str,
        label: str,
        total: int,
        target_listing_ids: Sequence[str] = (),
        target_item_ids: Sequence[str] = (),
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        clean_owner = str(owner or "").strip()
        clean_ch = str(channel or "").strip().lower()
        key = (clean_owner, clean_ch)
        pub_targets = list(target_listing_ids)
        dw_targets = list(target_item_ids)

        with self._lock:
            if self._shutting_down:
                raise HTTPException(status_code=503, detail="服务正在停机，暂不接收新任务")

            active_list = self._active_by_key.get(key) or []
            # 幂等去重与互斥校验
            if kind == "sync":
                for entry in active_list:
                    if entry["kind"] == "sync":
                        existing = self.repo.get_channel_job(clean_owner, clean_ch, entry["job_id"])
                        if existing:
                            return _serialize_job(existing)
            elif kind == "publish" and pub_targets:
                lid = pub_targets[0]
                for entry in active_list:
                    if lid in entry["target_listing_ids"]:
                        existing = self.repo.get_channel_job(clean_owner, clean_ch, entry["job_id"])
                        if existing:
                            return _serialize_job(existing)
            elif kind == "downshelf" and dw_targets:
                ext_id = dw_targets[0]
                for entry in active_list:
                    if ext_id in entry["target_item_ids"]:
                        existing = self.repo.get_channel_job(clean_owner, clean_ch, entry["job_id"])
                        if existing:
                            return _serialize_job(existing)
            elif kind == "reconcile":
                for entry in active_list:
                    if entry["kind"] == "reconcile":
                        raise HTTPException(
                            status_code=409,
                            detail="当前已有同步任务正在执行，请等待完成或先停止后再试",
                        )

            if len(active_list) >= MAX_PENDING_JOBS_PER_CHANNEL:
                raise HTTPException(status_code=429, detail="后台待处理任务过多，请稍候")

            now = datetime.now(timezone.utc)
            job_id = f"job_{uuid.uuid4().hex[:12]}"
            job_doc = {
                "job_id": job_id,
                "owner": clean_owner,
                "channel": clean_ch,
                "kind": kind,
                "label": label,
                "status": "queued",
                "total": max(1, int(total)),
                "completed": 0,
                "current_label": "排队等待执行...",
                "target_listing_ids": pub_targets,
                "target_item_ids": dw_targets,
                "published": [],
                "downshelved": [],
                "failed": [],
                "aborted_reason": "",
                "error_code": "",
                "error": "",
                "cancel_requested": False,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            }
            created = self.repo.create_channel_job(job_doc)

            task = _QueuedTask(
                job_id=job_id,
                owner=clean_owner,
                channel=clean_ch,
                kind=kind,
                payload=dict(payload or {}),
            )
            self._tasks_by_id[job_id] = task
            self._active_by_key.setdefault(key, []).append({
                "job_id": job_id,
                "kind": kind,
                "target_listing_ids": set(pub_targets),
                "target_item_ids": set(dw_targets),
            })
            self._queues.setdefault(key, deque()).append(task)

            worker = self._workers.get(key)
            if worker is None or not worker.is_alive():
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(key,),
                    name=f"xy-job-{clean_ch}-{clean_owner[:12]}",
                    daemon=True,
                )
                self._workers[key] = t
                t.start()

            return _serialize_job(created)

    def submit_sync(self, owner: str, channel: str) -> Dict[str, Any]:
        adapter = self.channel_service.validate_sync_request(owner, channel)
        return self._enqueue(
            owner=owner,
            channel=adapter.channel_code,
            kind="sync",
            label=f"刷新{adapter.channel_name}在售",
            total=1,
        )

    def submit_publish(self, owner: str, channel: str, listing_id: str) -> Dict[str, Any]:
        lst = self.channel_service.validate_publish_request(owner, channel, listing_id)
        title = str(lst.get("title") or listing_id).strip()[:18]
        return self._enqueue(
            owner=owner,
            channel=channel,
            kind="publish",
            label=f"上架：{title}",
            total=1,
            target_listing_ids=[str(lst["listing_id"])],
            payload={"listing_id": str(lst["listing_id"]), "title": title},
        )

    def submit_downshelf(self, owner: str, channel: str, external_item_id: str) -> Dict[str, Any]:
        ext_id = self.channel_service.validate_downshelf_request(owner, channel, external_item_id)
        return self._enqueue(
            owner=owner,
            channel=channel,
            kind="downshelf",
            label=f"下架商品 {ext_id}",
            total=1,
            target_item_ids=[ext_id],
            payload={"external_item_id": ext_id},
        )

    def submit_reconcile(
        self,
        owner: str,
        channel: str,
        *,
        publish_listing_ids: Sequence[str] = (),
        downshelf_item_ids: Sequence[str] = (),
    ) -> Dict[str, Any]:
        pub_ids, dw_ids = self.channel_service.validate_reconcile_request(
            owner,
            channel,
            publish_listing_ids,
            downshelf_item_ids,
        )
        return self._enqueue(
            owner=owner,
            channel=channel,
            kind="reconcile",
            label="同步选品库",
            total=len(pub_ids) + len(dw_ids),
            target_listing_ids=pub_ids,
            target_item_ids=dw_ids,
            payload={"publish_listing_ids": pub_ids, "downshelf_item_ids": dw_ids},
        )

    def _finish_task_bookkeeping(self, key: Tuple[str, str], job_id: str) -> None:
        with self._lock:
            self._tasks_by_id.pop(job_id, None)
            active_list = self._active_by_key.get(key)
            if active_list is not None:
                self._active_by_key[key] = [x for x in active_list if x["job_id"] != job_id]
                if not self._active_by_key[key]:
                    self._active_by_key.pop(key, None)

    def _worker_loop(self, key: Tuple[str, str]) -> None:
        owner, channel = key
        while True:
            with self._lock:
                q = self._queues.get(key)
                if not q:
                    self._queues.pop(key, None)
                    self._workers.pop(key, None)
                    return
                task = q.popleft()
                if self._shutting_down or task.cancel_event.is_set():
                    should_skip = True
                else:
                    should_skip = False

            if should_skip:
                now = datetime.now(timezone.utc)
                self.repo.update_channel_job(
                    task.job_id,
                    {
                        "status": "cancelled",
                        "current_label": "",
                        "aborted_reason": "任务在排队期间已取消",
                        "finished_at": now,
                        "updated_at": now,
                    },
                )
                self._finish_task_bookkeeping(key, task.job_id)
                continue

            try:
                self._execute_task(task)
            except Exception as exc:
                LOGGER.exception("[渠道后台任务异常] job=%s owner=%s ch=%s: %s", task.job_id, owner, channel, exc)
                now = datetime.now(timezone.utc)
                self.repo.update_channel_job(
                    task.job_id,
                    {
                        "status": "failed",
                        "current_label": "",
                        "error_code": "JOB_ERROR",
                        "error": str(exc)[:200],
                        "aborted_reason": str(exc)[:200],
                        "finished_at": now,
                        "updated_at": now,
                    },
                )
            finally:
                self._finish_task_bookkeeping(key, task.job_id)
                try:
                    self.repo.prune_channel_jobs(owner, channel, keep=20)
                except Exception:
                    pass

    def _execute_task(self, task: _QueuedTask) -> None:
        now = datetime.now(timezone.utc)
        self.repo.update_channel_job(
            task.job_id,
            {
                "status": "running",
                "started_at": now,
                "updated_at": now,
            },
        )

        if task.kind == "sync":
            self._run_sync(task)
        elif task.kind == "publish":
            self._run_publish(task)
        elif task.kind == "downshelf":
            self._run_downshelf(task)
        elif task.kind == "reconcile":
            self._run_reconcile(task)

    def _run_sync(self, task: _QueuedTask) -> None:
        self.repo.update_channel_job(
            task.job_id,
            {"current_label": "正在同步线上在售列表...", "updated_at": datetime.now(timezone.utc)},
        )
        try:
            self.channel_service.sync_selling(task.owner, task.channel)
            fin = datetime.now(timezone.utc)
            self.repo.update_channel_job(
                task.job_id,
                {
                    "status": "completed",
                    "completed": 1,
                    "current_label": "",
                    "finished_at": fin,
                    "updated_at": fin,
                },
            )
        except HTTPException as exc:
            fin = datetime.now(timezone.utc)
            acct = self.repo.get_channel_account(task.owner, task.channel) or {}
            err_code = str(acct.get("error_code") or ("ACCOUNT_OFFLINE" if exc.status_code == 409 else "SYNC_FAILED"))
            self.repo.update_channel_job(
                task.job_id,
                {
                    "status": "failed",
                    "completed": 1,
                    "current_label": "",
                    "error_code": err_code,
                    "error": str(exc.detail or "同步失败"),
                    "aborted_reason": str(exc.detail or "同步失败"),
                    "finished_at": fin,
                    "updated_at": fin,
                },
            )

    def _run_publish(self, task: _QueuedTask) -> None:
        lid = str(task.payload.get("listing_id") or "")
        title = str(task.payload.get("title") or lid)
        self.repo.update_channel_job(
            task.job_id,
            {"current_label": f"正在上架：{title}", "updated_at": datetime.now(timezone.utc)},
        )
        try:
            res = self.channel_service.publish_item(task.owner, task.channel, lid)
            fin = datetime.now(timezone.utc)
            self.repo.update_channel_job(
                task.job_id,
                {
                    "status": "completed",
                    "completed": 1,
                    "current_label": "",
                    "published": [
                        {
                            "listing_id": res["listing_id"],
                            "item_id": res["item_id"],
                            "item_url": res["item_url"],
                            "title": res["title"],
                            "price": res["price"],
                        }
                    ],
                    "finished_at": fin,
                    "updated_at": fin,
                },
            )
        except HTTPException as exc:
            fin = datetime.now(timezone.utc)
            lst = self.repo.get_listing_by_id(task.owner, lid) or {}
            pf = lst.get("publish_failure") if isinstance(lst.get("publish_failure"), dict) else {}
            err_code = str(pf.get("error_code") or ("ACCOUNT_OFFLINE" if exc.status_code == 409 else "PUBLISH_FAILED"))
            msg = str(exc.detail or pf.get("message") or "上架失败")
            self.repo.update_channel_job(
                task.job_id,
                {
                    "status": "failed",
                    "completed": 1,
                    "current_label": "",
                    "error_code": err_code,
                    "error": msg,
                    "aborted_reason": msg if exc.status_code == 409 else "",
                    "failed": [
                        {
                            "action": "publish",
                            "listing_id": lid,
                            "error_code": err_code,
                            "message": msg,
                            "screenshot_url": str(pf.get("screenshot_url") or ""),
                        }
                    ],
                    "finished_at": fin,
                    "updated_at": fin,
                },
            )

    def _run_downshelf(self, task: _QueuedTask) -> None:
        ext_id = str(task.payload.get("external_item_id") or "")
        self.repo.update_channel_job(
            task.job_id,
            {"current_label": f"正在下架 {ext_id}", "updated_at": datetime.now(timezone.utc)},
        )
        try:
            res = self.channel_service.downshelf_item(task.owner, task.channel, ext_id)
            fin = datetime.now(timezone.utc)
            self.repo.update_channel_job(
                task.job_id,
                {
                    "status": "completed",
                    "completed": 1,
                    "current_label": "",
                    "downshelved": [{"item_id": res["item_id"]}],
                    "finished_at": fin,
                    "updated_at": fin,
                },
            )
        except HTTPException as exc:
            fin = datetime.now(timezone.utc)
            acct = self.repo.get_channel_account(task.owner, task.channel) or {}
            err_code = str(acct.get("error_code") or ("ACCOUNT_OFFLINE" if exc.status_code == 409 else "DOWNSHELF_FAILED"))
            msg = str(exc.detail or "下架失败")
            self.repo.update_channel_job(
                task.job_id,
                {
                    "status": "failed",
                    "completed": 1,
                    "current_label": "",
                    "error_code": err_code,
                    "error": msg,
                    "aborted_reason": msg if exc.status_code == 409 else "",
                    "failed": [{"action": "downshelf", "item_id": ext_id, "message": msg}],
                    "finished_at": fin,
                    "updated_at": fin,
                },
            )

    def _run_reconcile(self, task: _QueuedTask) -> None:
        pub_ids = list(task.payload.get("publish_listing_ids") or [])
        dw_ids = list(task.payload.get("downshelf_item_ids") or [])

        def _on_progress(snap: Dict[str, Any]) -> None:
            self.repo.update_channel_job(
                task.job_id,
                {
                    "total": int(snap.get("total") or task.payload.get("total") or 1),
                    "completed": int(snap.get("completed") or 0),
                    "current_label": str(snap.get("current_label") or ""),
                    "published": list(snap.get("published") or []),
                    "downshelved": list(snap.get("downshelved") or []),
                    "failed": list(snap.get("failed") or []),
                    "aborted_reason": str(snap.get("aborted_reason") or ""),
                    "updated_at": datetime.now(timezone.utc),
                },
            )

        res = self.channel_service.apply_reconcile(
            task.owner,
            task.channel,
            publish_listing_ids=pub_ids,
            downshelf_item_ids=dw_ids,
            on_progress=_on_progress,
            should_cancel=lambda: task.cancel_event.is_set() or self._shutting_down,
        )

        published = list(res.get("published") or [])
        downshelved = list(res.get("downshelved") or [])
        failed = list(res.get("failed") or [])
        aborted_reason = str(res.get("aborted_reason") or "")
        cancelled = bool(res.get("cancelled"))

        if cancelled:
            final_status = "cancelled"
        elif failed and (published or downshelved):
            final_status = "partial"
        elif failed and not (published or downshelved):
            final_status = "failed"
        else:
            final_status = "completed"

        acct = self.repo.get_channel_account(task.owner, task.channel) or {}
        err_code = ""
        if final_status in ("failed", "partial") and not acct.get("online", True):
            err_code = str(acct.get("error_code") or "ACCOUNT_OFFLINE")
        elif failed:
            err_code = "PARTIAL_FAILED" if final_status == "partial" else "RECONCILE_FAILED"

        fin = datetime.now(timezone.utc)
        self.repo.update_channel_job(
            task.job_id,
            {
                "status": final_status,
                "completed": len(published) + len(downshelved) + len(failed),
                "current_label": "",
                "published": published,
                "downshelved": downshelved,
                "failed": failed,
                "aborted_reason": aborted_reason,
                "error_code": err_code,
                "error": aborted_reason or (failed[0]["message"] if failed else ""),
                "finished_at": fin,
                "updated_at": fin,
            },
        )

    def list_jobs(self, owner: str, channel: str, *, limit: int = 15) -> Dict[str, Any]:
        adapter = self.channel_service._adapter(channel)
        docs = self.repo.list_channel_jobs(owner, adapter.channel_code, limit=limit)
        serialized = [_serialize_job(d) for d in docs]
        active = [j for j in serialized if j["status"] in ACTIVE_JOB_STATUSES]
        recent = [j for j in serialized if j["status"] in TERMINAL_JOB_STATUSES]
        return {
            "active_jobs": active,
            "recent_jobs": recent,
            "latest_job": serialized[0] if serialized else None,
        }

    def get_job(self, owner: str, channel: str, job_id: str) -> Dict[str, Any]:
        adapter = self.channel_service._adapter(channel)
        doc = self.repo.get_channel_job(owner, adapter.channel_code, job_id)
        if not doc:
            raise HTTPException(status_code=404, detail="后台任务不存在")
        return _serialize_job(doc)

    def cancel_job(self, owner: str, channel: str, job_id: str) -> Dict[str, Any]:
        adapter = self.channel_service._adapter(channel)
        clean_owner = str(owner or "").strip()
        clean_ch = adapter.channel_code
        doc = self.repo.get_channel_job(clean_owner, clean_ch, job_id)
        if not doc:
            raise HTTPException(status_code=404, detail="后台任务不存在")
        if doc.get("status") in TERMINAL_JOB_STATUSES:
            return _serialize_job(doc)

        with self._lock:
            task = self._tasks_by_id.get(job_id)
            if task is not None:
                task.cancel_event.set()
            key = (clean_owner, clean_ch)
            q = self._queues.get(key)
            was_queued = False
            if q is not None and task is not None:
                new_q: Deque[_QueuedTask] = deque()
                for item in q:
                    if item.job_id == job_id:
                        was_queued = True
                    else:
                        new_q.append(item)
                self._queues[key] = new_q

        now = datetime.now(timezone.utc)
        if was_queued:
            updated = self.repo.update_channel_job(
                job_id,
                {
                    "status": "cancelled",
                    "cancel_requested": True,
                    "current_label": "",
                    "aborted_reason": "已取消排队任务",
                    "finished_at": now,
                    "updated_at": now,
                },
            )
            self._finish_task_bookkeeping(key, job_id)
            return _serialize_job(updated or doc)

        updated = self.repo.update_channel_job(
            job_id,
            {"cancel_requested": True, "updated_at": now},
        )
        return _serialize_job(updated or doc)

    def shutdown(self, wait: bool = True, timeout_sec: float = 5.0) -> None:
        with self._lock:
            self._shutting_down = True
            for task in self._tasks_by_id.values():
                task.cancel_event.set()
            workers = list(self._workers.values())
        if wait:
            deadline = datetime.now(timezone.utc).timestamp() + max(0.5, float(timeout_sec))
            for w in workers:
                rem = max(0.05, deadline - datetime.now(timezone.utc).timestamp())
                if w.is_alive():
                    w.join(timeout=rem)
