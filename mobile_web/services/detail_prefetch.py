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
from config import logger
from models.product import DetailSnapshot, ProductRef


WORKER_COUNT = 1
QUEUE_CAPACITY = 128
PAGE_LIMIT = 20
DEFAULT_INTERVAL_SECONDS = 300
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.25
FAILURE_COOLDOWN_SECONDS = 60
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
                 platform_provider=None, adapter_provider=None):
        self.service = service
        self.seed_enabled = seed_enabled
        self.interval_seconds = interval_seconds
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

    def stats(self):
        """返回脱离内部状态的只读统计快照；不暴露任何商品或用户身份。"""
        with self._condition:
            return {"admitted": self._admitted, "seed_cycles": self._seed_cycles,
                    "queued": len(self._pending), "inflight": int(self._inflight is not None),
                    "accepting": self._accepting, "completed": dict(self._completed)}

    def _log_stats(self, phase, *, admitted=None):
        stats = self.stats()
        logger.info("[detail-prefetch] %s seed_cycles=%s admitted=%s queued=%s inflight=%s completed=%s",
                    phase, stats["seed_cycles"], stats["admitted"] if admitted is None else admitted,
                    stats["queued"], stats["inflight"], stats["completed"])

    def _has_capacity(self):
        # 在途任务也计入总容量，整个协调器至多保有128个任务。
        return len(self._pending) + (self._inflight is not None) < self._capacity

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
        running = self._inflight if isinstance(self._inflight, _DetailTask) and self._inflight.key == key else None
        if snapshot is None:
            if queued is not None or running is not None:
                return 0
            # 只匹配主标识，不拿普通候选里的 ID/Sign 建立未经核验的别名。
            for task in (*self._pending.values(), self._inflight):
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
                    logger.debug("[%s] 连接预热失败: %s", platform["code"], type(exc).__name__)

    def _seed(self):
        for platform in self._platforms():
            capabilities = platform.get("capabilities") or {}
            if not platform.get("active") or not all(capabilities.get(name, True) for name in ("recommend", "detail")):
                continue
            if self._stop_event.is_set():
                return
            try:
                channels = self._adapter(platform["code"]).channels[:2]
            except Exception as exc:
                logger.warning("[%s] 商品预取平台不可用: %s", platform["code"], type(exc).__name__)
                continue
            for channel in channels:
                if self._stop_event.is_set():
                    return
                try:
                    # 不 force 缓存、不翻页、不携带用户作用域；service 自己交付候选和 ready 快照。
                    self.service.get_home_recommendations(
                        platform=platform["code"], channel_type=channel["channel_type"],
                        offset=0, limit=PAGE_LIMIT, uid=None, cursor=None,
                    )
                except Exception as exc:
                    logger.warning("[%s] 商品首屏预取失败: %s", platform["code"], type(exc).__name__)

    def _execute_detail(self, task):
        snapshot = task.snapshot
        result = None
        for attempt in range(MAX_ATTEMPTS):
            if self._stop_event.is_set():
                return "stopped"
            try:
                result = self.service.prefetch_product_detail(
                    snapshot.ref if snapshot is not None else task.ref,
                    snapshot=snapshot, refresh_ahead_seconds=self.interval_seconds + 30,
                    stop_event=self._stop_event,
                )
            except Exception as exc:
                # 只有共享入口明确标识的暂时错误才重试。
                logger.warning("商品详情预取失败: %s", type(exc).__name__)
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
                return "stopped"
        now = self._wall_clock()
        next_check = getattr(result, "next_check_at", 0)
        if getattr(result, "status", None) == "ready" and isinstance(next_check, (int, float)) and math.isfinite(next_check):
            until = max(now, next_check)
        else:
            until = now + FAILURE_COOLDOWN_SECONDS
        with self._condition:
            # 执行期间收到的新版本留在 pending；完成旧任务绝不 pop 新任务。
            for key in {task.key, _primary_key(task.ref)}:
                self._cooldowns[key] = (until, _source_version(snapshot))
                self._cooldowns.move_to_end(key)
            while len(self._cooldowns) > COOLDOWN_CAPACITY:
                self._cooldowns.popitem(last=False)
        status = getattr(result, "status", "error")
        return status if status in _COMPLETION_STATUSES else "error"

    def _execute(self, task):
        if isinstance(task, _SummaryTask):
            try:
                self.service.repo.upsert_home_products(task.items, channel_type=task.channel_type)
                return "summary_ready"
            except Exception as exc:
                logger.warning("推荐商品入库失败，不影响在线结果: %s", type(exc).__name__)
                return "summary_error"
        return self._execute_detail(task)

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
                    self._inflight = task
                status = "stopped"
                try:
                    if not self._stop_event.is_set():
                        status = self._execute(task)
                finally:
                    with self._condition:
                        self._completed[status] += 1
                        self._inflight = None
                        batch_finished = not self._pending
                        self._condition.notify_all()
                if batch_finished:
                    self._log_stats("idle")
        finally:
            with self._condition:
                self._accepting = False
                self._pending.clear()
                self._inflight = None
                self._condition.notify_all()
            self._log_stats("stopped")
