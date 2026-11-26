# -*- coding: utf-8 -*-
"""进程内商品预取协调器；所有 HTTP/详情持久化均交给已装配的商品服务。"""
import math
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
from itertools import islice
from typing import Optional

from adapters import get_adapter, get_all_platforms
from cache import is_product_off_promotion
from config import logger
from infrastructure.api_governance import api_caller_context
from infrastructure.logging_utils import format_exc_summary
from models.product import DetailPrefetchResult, DetailSnapshot, ProductRef


QUEUE_CAPACITY = 128
PAGE_LIMIT = 20
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MAX_RANK_LIMIT = 400
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.25
FAILURE_COOLDOWN_SECONDS = 60
RATE_LIMIT_COOLDOWN_SECONDS = 600
COOLDOWN_CAPACITY = QUEUE_CAPACITY * 2
_COMPLETION_STATUSES = ("ready", "persist_failed", "error", "skipped", "superseded", "stopped",
                        "summary_ready", "summary_error")


def _primary_key(ref):
    return ("detail", ref.platform, "sign" if ref.goods_sign else "id", ref.key)


def _snapshot_key(snapshot):
    ref = snapshot.ref
    # 只有服务交付的、已核验的 snapshot 才允许以原 ID 合并重新加密的 Sign。
    return ("ready", ref.platform, "id", ref.goods_id) if ref.goods_id else _primary_key(ref)


def _source_version(snapshot):
    if snapshot is None:
        return None
    fetched = snapshot.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (fetched.timestamp(), snapshot.ref)


def _newer_snapshot(incoming, previous):
    if previous is None:
        return True
    new, old = _source_version(incoming), _source_version(previous)
    return new[0] > old[0] or (new[0] == old[0] and new[1] != old[1])


@dataclass
class _DetailTask:
    key: tuple
    ref: ProductRef
    snapshot: Optional[DetailSnapshot] = None


@dataclass
class _SummaryTask:
    items: list
    channel_type: object


class DetailPrefetcher:
    """单 worker、有界队列；offer 不等待网络、数据库或空闲队列槽。

    start/stop 属于 lifespan；停止后的实例永久拒收，不能回退到散落线程。
    queue_capacity/clock 等注入点仅供离线并发测试，不是部署配置项。
    """

    def __init__(self, service, *, seed_enabled=False,
                 interval_seconds=DEFAULT_INTERVAL_SECONDS, queue_capacity=QUEUE_CAPACITY,
                 clock=None, wall_clock=None, retry_backoff_seconds=RETRY_BACKOFF_SECONDS,
                 platform_provider=None, adapter_provider=None,
                 deep_seed_enabled=True, max_rank_limit=DEFAULT_MAX_RANK_LIMIT):
        self.service = service
        self.seed_enabled = seed_enabled
        self.interval_seconds = interval_seconds
        self.deep_seed_enabled = deep_seed_enabled
        self.max_rank_limit = max_rank_limit
        self._capacity = queue_capacity
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._retry_backoff = retry_backoff_seconds
        self._platforms = platform_provider or get_all_platforms
        self._adapter = adapter_provider or get_adapter
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._pending = OrderedDict()
        self._cooldowns = OrderedDict()
        self._inflight = None
        self._summary_sequence = 0
        self._accepting = False
        self._thread = None
        self._admitted = 0
        self._seed_cycles = 0
        self._completed = dict.fromkeys(_COMPLETION_STATUSES, 0)
        self._batch_detail_total = 0
        self._batch_detail_errors = {}
        self._batch_detail_reasons = {}
        self._batch_persist_errors = {}
        self._batch_off_promotion = 0

    def _inflight_tasks(self):
        if self._inflight is None:
            return ()
        if isinstance(self._inflight, tuple):
            return self._inflight
        return (self._inflight,)

    def stats(self):
        """返回脱离内部状态的只读统计快照；不暴露任何商品或用户身份。"""
        with self._condition:
            return {"admitted": self._admitted, "seed_cycles": self._seed_cycles,
                    "queued": len(self._pending), "inflight": len(self._inflight_tasks()),
                    "accepting": self._accepting, "completed": dict(self._completed)}

    def _log_stats(self, phase, *, admitted=None):
        stats = self.stats()
        comp = stats["completed"]
        logger.info(
            "【详情预取】 %s 轮次[%s] 准入[%s] 排队[%s] 执行中[%s] 统计[就绪:%s/失败:%s/入库失败:%s/跳过:%s/摘要入库:%s]",
            phase,
            stats["seed_cycles"],
            stats["admitted"] if admitted is None else admitted,
            stats["queued"],
            stats["inflight"],
            comp.get("ready", 0),
            comp.get("error", 0),
            comp.get("persist_failed", 0),
            comp.get("skipped", 0),
            comp.get("summary_ready", 0),
        )

    def _has_capacity(self):
        # 在途任务也计入总容量，整个协调器至多保有128个任务。
        return len(self._pending) + len(self._inflight_tasks()) < self._capacity

    def start(self):
        with self._condition:
            if self._thread is not None or self._stop_event.is_set():
                return
            self._accepting = True
            self._thread = threading.Thread(target=self._run, name="product-detail-prefetch", daemon=False)
            self._thread.start()

    def offer_products(self, items, *, channel_type=None, snapshots=()):
        """详情只取首屏20条；新推荐摘要按20条切批，托管到同一个有界队列。"""
        # 关闭后连复制都不做；锁内还会再次检查以覆盖与 stop 的竞争。
        if self._stop_event.is_set():
            return 0
        source = iter(items or ())
        products = [deepcopy(item) for item in islice(source, PAGE_LIMIT) if isinstance(item, dict)]
        ready = [deepcopy(snapshot) for snapshot in islice(snapshots or (), PAGE_LIMIT)
                 if isinstance(snapshot, DetailSnapshot) and snapshot.ref.key]
        ready_primary = {_primary_key(snapshot.ref) for snapshot in ready}
        accepted = 0
        with self._condition:
            if not self._accepting:
                return 0
            if channel_type is not None:
                batch = products
                while batch and self._has_capacity():
                    self._summary_sequence += 1
                    self._pending[("summary", self._summary_sequence)] = _SummaryTask(batch, channel_type)
                    accepted += 1
                    if not self._has_capacity():
                        break
                    batch = [deepcopy(item) for item in islice(source, PAGE_LIMIT) if isinstance(item, dict)]
            for snapshot in ready:
                accepted += self._offer_detail(snapshot.ref, snapshot)
            for item in products:
                ref = ProductRef(item.get("platform"), item.get("goods_id"), item.get("goods_sign"))
                if ref.key and _primary_key(ref) not in ready_primary:
                    accepted += self._offer_detail(ref)
            if accepted:
                self._admitted += accepted
                self._condition.notify()
        return accepted

    def _offer_detail(self, ref, snapshot=None):
        """调用者持有 condition；不做任何外部 I/O。"""
        key = _snapshot_key(snapshot) if snapshot is not None else _primary_key(ref)
        queued = self._pending.get(key)
        running = next(
            (t for t in self._inflight_tasks() if isinstance(t, _DetailTask) and t.key == key),
            None,
        )
        if snapshot is None:
            if queued is not None or running is not None:
                return 0
            # 只匹配主标识，不拿普通候选里的 ID/Sign 建立未经核验的别名。
            for task in (*self._pending.values(), *self._inflight_tasks()):
                if isinstance(task, _DetailTask) and task.snapshot is not None and _primary_key(task.ref) == key:
                    return 0
        else:
            previous = queued or running
            if previous is not None and not _newer_snapshot(snapshot, previous.snapshot):
                return 0
        cooldown = self._cooldowns.get(key)
        if cooldown and self._wall_clock() < cooldown[0]:
            version = _source_version(snapshot)
            # 已经抓好的新版本不能被旧候选/旧版本失败冷却吞掉。
            if snapshot is None or (cooldown[1] is not None and
                                    (version[0] < cooldown[1][0] or version == cooldown[1])):
                return 0
        if queued is not None:
            self._pending[key] = _DetailTask(key, ref, snapshot)
            return 1
        if snapshot is not None:
            # 将同一个主 Sign 的尚未开始普通候选升级为 ready，不额外占槽。
            self._pending.pop(_primary_key(ref), None)
        if not self._has_capacity():
            return 0
        self._pending[key] = _DetailTask(key, ref, snapshot)
        return 1

    def stop(self):
        """停止接收、丢未开始任务并等待在途超时 I/O 自然返回；绝不假装强杀线程。"""
        with self._condition:
            self._accepting = False
            self._stop_event.set()
            self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is threading.current_thread():
            raise RuntimeError("预取 worker 不能 join 自己")
        if thread is not None:
            thread.join()

    def _warm_connections(self):
        for platform in self._platforms():
            if self._stop_event.is_set():
                return
            if platform.get("active"):
                try:
                    self._adapter(platform["code"]).warmup()
                except Exception as exc:
                    logger.debug(
                        "【详情预取】平台[%s] 连接池与索引预热未就绪 | 异常<%s>",
                        platform["code"], format_exc_summary(exc),
                    )

    def _seed(self):
        with api_caller_context("background"):
            self._seed_inner()

    def _seed_inner(self):
        for platform in self._platforms():
            capabilities = platform.get("capabilities") or {}
            if not platform.get("active") or not all(capabilities.get(name, True) for name in ("recommend", "detail")):
                continue
            if self._stop_event.is_set():
                return
            adapter = None
            try:
                adapter = self._adapter(platform["code"])
                channels = adapter.channels[:2]
            except Exception as exc:
                logger.warning(
                    "❌ 【详情预取告警】获取平台预取频道配置失败，已跳过该平台本轮预取 | 平台[%s] 异常<%s> 排查方向<请检查平台适配器注册或配置状态>",
                    platform["code"], format_exc_summary(exc),
                )
                continue
            for channel in channels:
                if self._stop_event.is_set():
                    return
                ch_type = channel.get("channel_type")
                first_res = None
                try:
                    # 不 force 缓存、不翻页、不携带用户作用域；service 自己交付候选和 ready 快照。
                    first_res = self.service.get_home_recommendations(
                        platform=platform["code"], channel_type=ch_type,
                        offset=0, limit=PAGE_LIMIT, uid=None, cursor=None,
                    )
                except Exception as exc:
                    logger.warning(
                        "❌ 【详情预取告警】执行平台首屏推荐预取时发生异常，下轮将自动重试 | 平台[%s] 频道[%s] 异常<%s>",
                        platform["code"], ch_type, format_exc_summary(exc),
                    )
                    continue

                if not self.deep_seed_enabled or adapter is None:
                    continue
                get_recommend = getattr(adapter, "get_recommend_goods", None)
                if not callable(get_recommend) or not isinstance(first_res, dict) or first_res.get("status") != "success":
                    continue

                has_more = bool(first_res.get("has_more"))
                next_offset = first_res.get("next_offset") or PAGE_LIMIT
                cursor = first_res.get("next_cursor")
                max_limit = getattr(adapter, "recommendation_limit", None) or self.max_rank_limit

                # 后续页：拉取榜单摘要直接入库；淘宝若自带随行详情快照 (detail_sink) 则一并复用落库，不额外消耗详情接口配额
                while has_more and next_offset < max_limit:
                    if self._stop_event.is_set():
                        return
                    paging = {"cursor": cursor} if cursor else {}
                    page_limit = min(50, max_limit - next_offset) if platform["code"] == "pdd" else PAGE_LIMIT
                    page_snapshots = []
                    collect_snapshots = page_snapshots.append if platform["code"] == "taobao" else None
                    try:
                        page_res = get_recommend(
                            channel_type=ch_type, offset=next_offset, limit=page_limit,
                            uid=None, detail_sink=collect_snapshots, **paging,
                        )
                    except Exception as exc:
                        logger.warning(
                            "❌ 【榜单摘要续拉异常】平台[%s] 频道[%s] 偏移[%s] 拉取摘要失败 | 异常<%s>",
                            platform["code"], ch_type, next_offset, format_exc_summary(exc),
                        )
                        break

                    if not isinstance(page_res, dict) or page_res.get("status") != "success":
                        break
                    raw_items = page_res.get("data") or []
                    if not isinstance(raw_items, list) or not raw_items:
                        break

                    valid_items = [
                        it for it in raw_items
                        if isinstance(it, dict) and not is_product_off_promotion(
                            platform["code"], it.get("goods_id"), it.get("goods_sign")
                        )
                    ]
                    if valid_items:
                        try:
                            self.service.repo.upsert_home_products(valid_items, channel_type=ch_type)
                        except Exception as exc:
                            logger.warning(
                                "❌ 【榜单摘要入库异常】平台[%s] 频道[%s] 批量入库失败 | 数量[%d] 异常<%s>",
                                platform["code"], ch_type, len(valid_items), format_exc_summary(exc),
                            )
                    if page_snapshots and callable(getattr(self.service, "prefetch_product_detail", None)):
                        for snap in page_snapshots:
                            if not isinstance(snap, DetailSnapshot) or not snap.ref.key:
                                continue
                            if is_product_off_promotion(platform["code"], snap.ref.goods_id, snap.ref.goods_sign):
                                continue
                            try:
                                self.service.prefetch_product_detail(
                                    snap.ref,
                                    snapshot=snap,
                                    refresh_ahead_seconds=self.interval_seconds + 30,
                                    stop_event=self._stop_event,
                                )
                            except Exception:
                                pass

                    has_more = bool(page_res.get("has_more"))
                    new_offset = page_res.get("next_offset")
                    if not isinstance(new_offset, int) or new_offset <= next_offset:
                        new_offset = next_offset + len(raw_items)
                    if new_offset <= next_offset:
                        break
                    next_offset = new_offset
                    cursor = page_res.get("next_cursor")

    def _record_detail_outcome(self, task, result, snapshot=None, last_exc_summary=""):
        now = self._wall_clock()
        next_check = getattr(result, "next_check_at", 0)
        err_code = str(getattr(result, "error_code", "") or "").strip()
        err_msg = str(getattr(result, "error_msg", "") or "").strip()
        err_upper = err_code.upper()
        status = getattr(result, "status", "error")
        if (
            (status == "ready" or err_upper == "PRODUCT_OFF_PROMOTION")
            and isinstance(next_check, (int, float))
            and not isinstance(next_check, bool)
            and math.isfinite(next_check)
        ):
            until = max(now, next_check)
        elif err_upper in ("RATE_LIMIT", "QUOTA_EXCEEDED", "429"):
            until = now + RATE_LIMIT_COOLDOWN_SECONDS
        else:
            until = now + FAILURE_COOLDOWN_SECONDS
        with self._condition:
            # 执行期间收到的新版本留在 pending；完成旧任务绝不 pop 新任务。
            for key in {task.key, _primary_key(task.ref)}:
                self._cooldowns[key] = (until, _source_version(snapshot))
                self._cooldowns.move_to_end(key)
            while len(self._cooldowns) > COOLDOWN_CAPACITY:
                self._cooldowns.popitem(last=False)
        status = status if status in _COMPLETION_STATUSES else "error"
        if last_exc_summary:
            err_code = err_code or "INTERNAL_ERROR"
            err_msg = err_msg or last_exc_summary
        return status, err_code, err_msg

    def _execute_detail_batch(self, tasks):
        with api_caller_context("background"):
            try:
                outcomes = self.service.prefetch_product_details_batch(
                    [t.ref for t in tasks],
                    refresh_ahead_seconds=self.interval_seconds + 30,
                    stop_event=self._stop_event,
                ) or {}
            except Exception as exc:
                exc_summary = format_exc_summary(exc)
                logger.warning(
                    "❌ 【详情预取批量异常】执行同平台批量预取任务时发生异常，不影响在线服务 | 平台[%s] 异常<%s>",
                    tasks[0].ref.platform if tasks else "", exc_summary,
                )
                return [
                    self._record_detail_outcome(
                        t,
                        DetailPrefetchResult("error", error_code="INTERNAL_ERROR", error_msg=exc_summary),
                        last_exc_summary=exc_summary,
                    )
                    for t in tasks
                ]
            recorded = []
            for t in tasks:
                if self._stop_event.is_set():
                    recorded.append(("stopped", "", ""))
                    continue
                res = outcomes.get(t.ref.key)
                if res is None:
                    res = DetailPrefetchResult("error", error_code="UPSTREAM_ERROR", error_msg="批量预取未返回结果")
                snap = getattr(res, "snapshot", None)
                if getattr(res, "status", "") == "persist_failed" and snap is not None and not self._stop_event.is_set():
                    try:
                        res = self.service.prefetch_product_detail(
                            snap.ref,
                            snapshot=snap,
                            refresh_ahead_seconds=self.interval_seconds + 30,
                            stop_event=self._stop_event,
                        )
                        snap = getattr(res, "snapshot", None) or snap
                    except Exception:
                        pass
                recorded.append(self._record_detail_outcome(t, res, snap))
            return recorded

    def _execute_detail(self, task):
        with api_caller_context("background"):
            return self._execute_detail_inner(task)

    def _execute_detail_inner(self, task):
        snapshot = task.snapshot
        result = None
        last_exc_summary = ""
        for attempt in range(MAX_ATTEMPTS):
            if self._stop_event.is_set():
                return "stopped", "", ""
            try:
                result = self.service.prefetch_product_detail(
                    snapshot.ref if snapshot is not None else task.ref,
                    snapshot=snapshot, refresh_ahead_seconds=self.interval_seconds + 30,
                    stop_event=self._stop_event,
                )
            except Exception as exc:
                last_exc_summary = format_exc_summary(exc)
                # 只有共享入口明确标识的暂时错误才重试。
                logger.warning(
                    "❌ 【详情预取单品异常】执行后台商品详情预取任务时发生异常，不影响在线服务 | 平台[%s] 异常<%s>",
                    task.ref.platform, last_exc_summary,
                )
                break
            if result.snapshot is not None:
                snapshot = result.snapshot  # persist_failed 的第二次尝试只落库，不重复 HTTP。
            if not result.retryable or attempt + 1 == MAX_ATTEMPTS:
                break
            with self._condition:
                replacement = self._pending.get(task.key)
                if isinstance(replacement, _DetailTask) and replacement.snapshot is not None:
                    if snapshot is None or _newer_snapshot(replacement.snapshot, snapshot):
                        break
            if self._stop_event.wait(self._retry_backoff):
                return "stopped", "", ""
        return self._record_detail_outcome(task, result, snapshot, last_exc_summary)

    def _execute(self, task):
        if isinstance(task, _SummaryTask):
            try:
                self.service.repo.upsert_home_products(task.items, channel_type=task.channel_type)
                return "summary_ready", "", ""
            except Exception as exc:
                logger.warning(
                    "❌ 【详情预取告警】预取队列批量写入推荐商品摘要失败，不影响在线结果 | 频道[%s] 异常<%s> 排查方向<请检查MongoDB写入连接状态>",
                    task.channel_type, format_exc_summary(exc),
                )
                return "summary_error", "PERSIST_FAILED", format_exc_summary(exc)
        return self._execute_detail(task)

    def _pop_same_platform_batch_locked(self, first_task, max_batch: int = PAGE_LIMIT):
        batch = [first_task]
        if (
            not isinstance(first_task, _DetailTask)
            or first_task.snapshot is not None
            or not callable(getattr(self.service, "prefetch_product_details_batch", None))
        ):
            return batch
        plat = first_task.ref.platform
        extra_keys = [
            k for k, v in self._pending.items()
            if isinstance(v, _DetailTask) and v.snapshot is None and v.ref.platform == plat
        ][:max(0, max_batch - 1)]
        for k in extra_keys:
            popped = self._pending.pop(k, None)
            if popped is not None:
                batch.append(popped)
        return batch

    def _run(self):
        try:
            if self.seed_enabled:
                self._warm_connections()
            next_seed = self._clock() if self.seed_enabled else None
            while not self._stop_event.is_set():
                if next_seed is not None and self._clock() >= next_seed:
                    admitted_before = self.stats()["admitted"]
                    self._seed()
                    with self._condition:
                        self._seed_cycles += 1
                        admitted = self._admitted - admitted_before
                    self._log_stats("seed", admitted=admitted)
                    # 周期从本轮完成后计算，慢请求不会积压补跑。
                    next_seed = self._clock() + self.interval_seconds
                    continue
                with self._condition:
                    if self._stop_event.is_set():
                        return
                    if not self._pending:
                        timeout = max(0, next_seed - self._clock()) if next_seed is not None else None
                        self._condition.wait(timeout)
                        continue
                    _, task = self._pending.popitem(last=False)
                    exec_tasks = self._pop_same_platform_batch_locked(task)
                    self._inflight = tuple(exec_tasks) if len(exec_tasks) > 1 else exec_tasks[0]
                task_outcomes = [("stopped", "", "")] * len(exec_tasks)
                try:
                    if not self._stop_event.is_set():
                        if len(exec_tasks) > 1:
                            task_outcomes = self._execute_detail_batch(exec_tasks)
                        else:
                            task_outcomes = [self._execute(exec_tasks[0])]
                finally:
                    batch_detail_total = 0
                    batch_detail_errors = None
                    batch_detail_reasons = None
                    batch_persist_errors = None
                    batch_off_promotion = 0
                    with self._condition:
                        for done_task, (status, err_code, err_msg) in zip(exec_tasks, task_outcomes):
                            self._completed[status] += 1
                            if isinstance(done_task, _DetailTask):
                                self._batch_detail_total += 1
                                plat = done_task.ref.platform or "unknown"
                                if status == "persist_failed":
                                    self._batch_persist_errors[plat] = self._batch_persist_errors.get(plat, 0) + 1
                                elif status == "skipped" and err_code.upper() == "PRODUCT_OFF_PROMOTION":
                                    self._batch_off_promotion += 1
                                elif status == "error":
                                    tag = f"{plat}[{err_code or 'UNKNOWN'}]"
                                    self._batch_detail_errors[tag] = self._batch_detail_errors.get(tag, 0) + 1
                                    if err_msg and tag not in self._batch_detail_reasons:
                                        self._batch_detail_reasons[tag] = err_msg[:120]
                        self._inflight = None
                        batch_finished = not self._pending
                        if batch_finished:
                            batch_detail_total = self._batch_detail_total
                            self._batch_detail_total = 0
                            batch_off_promotion = self._batch_off_promotion
                            self._batch_off_promotion = 0
                            if self._batch_persist_errors:
                                batch_persist_errors = dict(self._batch_persist_errors)
                                self._batch_persist_errors.clear()
                            if self._batch_detail_errors:
                                batch_detail_errors = dict(self._batch_detail_errors)
                                self._batch_detail_errors.clear()
                            if self._batch_detail_reasons:
                                batch_detail_reasons = dict(self._batch_detail_reasons)
                                self._batch_detail_reasons.clear()
                        self._condition.notify_all()
                if batch_finished:
                    if batch_persist_errors:
                        dist = ", ".join(f"{plat}:{cnt}条" for plat, cnt in sorted(batch_persist_errors.items()))
                        logger.warning(
                            "❌ 【详情预取告警】本批次后台预取存在数据库落盘失败任务 | 入库失败分布[%s] 排查方向<请检查MongoDB写入连接状态或磁盘空间>",
                            dist,
                        )
                    if batch_detail_errors:
                        err_total = sum(batch_detail_errors.values())
                        dist = ", ".join(f"{tag}:{cnt}条" for tag, cnt in sorted(batch_detail_errors.items()))
                        detail_hint = "; ".join(f"{tag}={reason}" for tag, reason in sorted((batch_detail_reasons or {}).items()))
                        logger.warning(
                            "❌ 【详情预取告警】本批次后台预取存在失败任务 | 失败[%d/%d条] 分布[%s]%s 排查方向<请检查上游接口错误码、入参ID/Sign或网络状态>",
                            err_total,
                            batch_detail_total,
                            dist,
                            f" | 原因<{detail_hint}>" if detail_hint else "",
                        )
                    elif batch_off_promotion > 0:
                        logger.info(
                            "【详情预取】本批次已自动清理已确认下架/退推商品 [%d/%d条]",
                            batch_off_promotion,
                            batch_detail_total,
                        )
                    self._log_stats("idle")
        finally:
            with self._condition:
                self._accepting = False
                self._pending.clear()
                self._inflight = None
                self._batch_detail_total = 0
                self._batch_detail_errors.clear()
                self._batch_detail_reasons.clear()
                self._batch_persist_errors.clear()
                self._batch_off_promotion = 0
                self._condition.notify_all()
            self._log_stats("stopped")
