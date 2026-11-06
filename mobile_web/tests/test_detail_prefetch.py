# -*- coding: utf-8 -*-
"""预取编排的离线并发边界；不创建真实适配器、HTTP、Mongo 或监听端口。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from models.product import DetailPrefetchResult, DetailSnapshot, ProductRef
from services.detail_prefetch import (
    COOLDOWN_CAPACITY, DEFAULT_INTERVAL_SECONDS, PAGE_LIMIT, QUEUE_CAPACITY, WORKER_COUNT,
    DetailPrefetcher,
)


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class MemoryService:
    def __init__(self, handler=None):
        self.calls = []
        self.recommendations = []
        self.writes = []
        self.handler = handler
        self.prefetcher = None
        self.repo = SimpleNamespace(upsert_home_products=self.write)
        self.condition = threading.Condition()

    def write(self, items, *, channel_type):
        self.writes.append((items, channel_type, threading.current_thread().name))
        return len(items)

    def prefetch_product_detail(self, ref, **kwargs):
        with self.condition:
            self.calls.append((ref, kwargs))
            self.condition.notify_all()
        if self.handler:
            return self.handler(ref, **kwargs)
        return DetailPrefetchResult("ready", next_check_at=2000)

    def get_home_recommendations(self, **kwargs):
        with self.condition:
            self.recommendations.append(kwargs)
            self.condition.notify_all()
        items = [product(str(kwargs["channel_type"]), platform=kwargs["platform"])]
        self.prefetcher.offer_products(items, channel_type=kwargs["channel_type"])
        return {"status": "success", "data": items, "has_more": True, "next_cursor": "never-follow"}

    def wait_calls(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.calls) >= count, timeout=2)


def product(goods_id="1", *, sign="", platform="pdd"):
    return {"platform": platform, "goods_id": goods_id, "goods_sign": sign, "name": "offline"}


def snapshot(goods_id="1", sign="SIGN_A", fetched_at=900, *, platform="taobao"):
    ref = ProductRef(platform, goods_id, sign)
    return DetailSnapshot(ref, dict(product(goods_id, sign=sign, platform=platform), desc="complete"),
                          datetime.fromtimestamp(fetched_at, timezone.utc))


def idle(worker):
    with worker._condition:
        assert worker._condition.wait_for(lambda: not worker._pending and worker._inflight is None, timeout=2)


def blocked_service():
    entered, release = threading.Event(), threading.Event()

    def handler(ref, **kwargs):
        if ref.goods_id == "block":
            entered.set()
            assert release.wait(3), "test failed to release in-flight task"
        return DetailPrefetchResult("ready", next_check_at=2000)

    return MemoryService(handler), entered, release


def test_constants_and_explicit_lifecycle_never_start_threads_in_constructor():
    assert (WORKER_COUNT, QUEUE_CAPACITY, PAGE_LIMIT, DEFAULT_INTERVAL_SECONDS) == (1, 128, 20, 300)
    service = MemoryService()
    before = set(threading.enumerate())
    worker = DetailPrefetcher(service)
    assert set(threading.enumerate()) == before
    assert worker.offer_products([product()]) == 0
    worker.start()
    thread = worker._thread
    worker.start()
    assert worker._thread is thread
    worker.stop()
    worker.stop()
    assert not thread.is_alive()
    assert worker.offer_products([product()], channel_type=5, snapshots=[snapshot()]) == 0
    worker.start()
    assert worker._thread is thread and not thread.is_alive()
    assert service.calls == service.writes == service.recommendations == []


def test_full_queue_drops_immediately_and_stop_discards_unstarted_tasks():
    service, entered, release = blocked_service()
    worker = DetailPrefetcher(service, queue_capacity=3)
    stopper = None
    worker.start()
    try:
        worker.offer_products([product("block")])
        assert entered.wait(2)
        started = time.monotonic()
        assert worker.offer_products([product(str(i)) for i in range(10)]) == 2
        assert worker.offer_products([product("overflow")], channel_type=5) == 0
        assert time.monotonic() - started < 0.25
        with worker._condition:
            assert len(worker._pending) + (worker._inflight is not None) == 3
        assert len(service.calls) == 1, "list admission must not execute queued network/DB work"
        stopper = threading.Thread(target=worker.stop)
        stopper.start()
        assert worker._stop_event.wait(2)
        assert stopper.is_alive(), "stop must wait for in-flight I/O, not just time out join"
        assert worker.offer_products([product("after-stop")]) == 0
    finally:
        release.set()
        if stopper:
            stopper.join(2)
            assert not stopper.is_alive()
        worker.stop()
    assert not worker._thread.is_alive()
    assert [ref.goods_id for ref, _ in service.calls] == ["block"]
    assert service.writes == []


def test_concurrent_admission_is_atomic_and_primary_identity_is_typed():
    service, entered, release = blocked_service()
    worker = DetailPrefetcher(service)
    worker.start()
    try:
        worker.offer_products([product("block")])
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=16) as producers:
            counts = list(producers.map(lambda _: worker.offer_products([product("same")]), range(32)))
        assert sum(counts) == 1
        assert worker.offer_products([product("", sign="same")]) == 1  # ID 与 Sign 不合并。
        assert worker.offer_products([product("same", platform="taobao")]) == 1
        assert worker.offer_products([product("raw-1", sign="SIGNED")]) == 1
        assert worker.offer_products([product("raw-2", sign="SIGNED")]) == 0
        with worker._condition:
            refs = [task.ref for task in worker._pending.values()]
        assert ProductRef("pdd", "raw-1", "SIGNED") in refs
        assert ProductRef("pdd", "raw-2", "SIGNED") not in refs
    finally:
        release.set()
        worker.stop()


def test_summary_bulk_uses_same_bounded_worker_and_cache_candidates_do_not_rewrite():
    service, entered, release = blocked_service()
    worker = DetailPrefetcher(service)
    worker.start()
    try:
        worker.offer_products([product("block")])
        assert entered.wait(2)
        items = [product("2"), product("3")]
        worker.offer_products(items, channel_type=5)
        worker.offer_products(items)  # cache/search/local 重复候选不写摘要。
        items[0]["name"] = "mutated after response"
        assert service.writes == []
        release.set()
        idle(worker)
        assert len(service.writes) == 1
        written, channel, thread_name = service.writes[0]
        assert channel == 5 and written[0]["name"] == "offline"
        assert thread_name == worker._thread.name
        assert [ref.goods_id for ref, _ in service.calls] == ["block", "2", "3"]
    finally:
        release.set()
        worker.stop()


def test_detail_candidates_are_capped_at_twenty_but_original_summary_batch_is_preserved():
    service, entered, release = blocked_service()
    worker = DetailPrefetcher(service)
    worker.start()
    try:
        worker.offer_products([product("block")])
        assert entered.wait(2)
        assert worker.offer_products([product(str(i)) for i in range(1, 46)], channel_type=0) == 23
        with worker._condition:
            assert len(worker._pending) == 23
        release.set()
        idle(worker)
        assert [len(batch) for batch, _, _ in service.writes] == [20, 20, 5]
        assert [item["goods_id"] for batch, _, _ in service.writes for item in batch] == [str(i) for i in range(1, 46)]
        assert len(service.calls) == 21
    finally:
        release.set()
        worker.stop()


def test_pending_ready_snapshots_merge_by_verified_raw_id_keep_latest_full_ref():
    service, entered, release = blocked_service()
    worker = DetailPrefetcher(service)
    old = snapshot("123", "OLD_SIGN", 900)
    latest = snapshot("123", "NEW_SIGN", 901)
    worker.start()
    try:
        worker.offer_products([product("block")])
        assert entered.wait(2)
        assert worker.offer_products([], snapshots=[old]) == 1
        assert worker.offer_products([], snapshots=[latest]) == 1
        assert worker.offer_products([], snapshots=[old, latest]) == 0
        with worker._condition:
            assert len(worker._pending) == 1
            queued = next(iter(worker._pending.values()))
            assert queued.ref == latest.ref and queued.snapshot == latest
        release.set()
        idle(worker)
        assert service.calls[1][0] == latest.ref
        assert service.calls[1][1]["snapshot"] == latest
        assert len(service.calls) == 2
    finally:
        release.set()
        worker.stop()


def test_new_ready_version_arriving_during_execution_survives_old_completion():
    entered, release = threading.Event(), threading.Event()
    old, middle, newest = snapshot("123", "OLD", 900), snapshot("123", "MIDDLE", 901), snapshot("123", "NEW", 902)

    def handler(ref, **kwargs):
        if ref == old.ref:
            entered.set()
            assert release.wait(3)
        return DetailPrefetchResult("ready", snapshot=kwargs["snapshot"], next_check_at=2000)

    service = MemoryService(handler)
    worker = DetailPrefetcher(service, wall_clock=Clock())
    worker.start()
    try:
        worker.offer_products([], snapshots=[old])
        assert entered.wait(2)
        worker.offer_products([], snapshots=[middle])
        worker.offer_products([], snapshots=[newest])
        release.set()
        idle(worker)
        assert [ref for ref, _ in service.calls] == [old.ref, newest.ref]
        assert service.calls[-1][1]["snapshot"].fetched_at == newest.fetched_at
        assert worker.offer_products([], snapshots=[newest]) == 0
    finally:
        release.set()
        worker.stop()


def test_new_ready_snapshot_bypasses_ordinary_failure_and_older_ready_cooldowns():
    clock = Clock()
    fresh = snapshot("123", "SIGN_A", 999)
    service = MemoryService(lambda ref, **kw: DetailPrefetchResult(
        "ready" if kw["snapshot"] else "error", snapshot=kw["snapshot"], next_check_at=1300))
    worker = DetailPrefetcher(service, wall_clock=clock)
    worker.start()
    try:
        worker.offer_products([product("123", sign="SIGN_A", platform="taobao")])
        idle(worker)
        assert worker.offer_products([product("123", sign="SIGN_A", platform="taobao")]) == 0
        assert worker.offer_products([], snapshots=[fresh]) == 1
        idle(worker)
        assert worker.offer_products([], snapshots=[fresh]) == 0
        newer = snapshot("123", "SIGN_B", 1000)
        assert worker.offer_products([], snapshots=[newer]) == 1
        idle(worker)
        assert service.calls[-1][0] == newer.ref
    finally:
        worker.stop()


def test_persist_failure_retries_only_twice_and_reuses_fetched_snapshot():
    captured = snapshot("123", "READY", 995, platform="pdd")
    attempts = []

    def handler(ref, **kwargs):
        attempts.append(kwargs["snapshot"])
        return DetailPrefetchResult("persist_failed", snapshot=captured, retryable=True)

    service = MemoryService(handler)
    worker = DetailPrefetcher(service, wall_clock=Clock(), retry_backoff_seconds=0)
    worker.start()
    try:
        worker.offer_products([product("123", sign="READY")])
        idle(worker)
        assert attempts == [None, captured]
        assert attempts[1] is captured
        assert worker.offer_products([product("123", sign="READY")]) == 0
        assert len(service.calls) == 2
    finally:
        worker.stop()


@pytest.mark.parametrize("seeded", [False, True])
def test_persist_retry_uses_verified_snapshot_ref_when_identity_changes(seeded):
    old = snapshot("123", "OLD_SIGN", 994, platform="pdd")
    winner = snapshot("123", "NEW_SIGN", 995, platform="pdd")
    upstream_fetches = []

    def handler(ref, **kwargs):
        seed = kwargs["snapshot"]
        if seed is None:
            upstream_fetches.append(ref)
        if seed is None or seed.ref == old.ref:
            return DetailPrefetchResult("persist_failed", snapshot=winner, retryable=True)
        # 与真实共享入口一致：DB-only seed 必须与本次传入的可信 ref 严格相等。
        if seed.ref != ref:
            return DetailPrefetchResult("skipped")
        return DetailPrefetchResult("ready", snapshot=winner, next_check_at=1300)

    service = MemoryService(handler)
    worker = DetailPrefetcher(service, wall_clock=Clock(), retry_backoff_seconds=0)
    worker.start()
    try:
        if seeded:
            worker.offer_products([], snapshots=[old])
        else:
            worker.offer_products([product("123", sign="OLD_SIGN")])
        idle(worker)
        assert [ref for ref, _ in service.calls] == [old.ref, winner.ref]
        assert service.calls[1][1]["snapshot"] is winner
        assert len(upstream_fetches) == int(not seeded)
        assert worker.stats()["completed"]["ready"] == 1
        assert worker.stats()["completed"]["skipped"] == 0
    finally:
        worker.stop()


def test_stats_are_detached_and_info_summaries_never_contain_product_context(monkeypatch):
    from services import detail_prefetch as module

    messages = []
    monkeypatch.setattr(module, "logger", SimpleNamespace(
        info=lambda message, *args: messages.append(message % args),
        warning=lambda *args: None, debug=lambda *args: None,
    ))
    service = MemoryService()
    worker = DetailPrefetcher(service)
    private_values = ("PRIVATE-ID-CANARY", "PRIVATE-SIGN-CANARY", "PRIVATE-UID-CANARY", "https://private.invalid/link")
    item = dict(product(private_values[0], sign=private_values[1]), uid=private_values[2], product_url=private_values[3])
    worker.start()
    try:
        assert worker.offer_products([item], channel_type=5) == 2
        idle(worker)
        stats = worker.stats()
        assert stats["admitted"] == 2 and stats["seed_cycles"] == 0
        assert stats["queued"] == stats["inflight"] == 0 and stats["accepting"] is True
        assert stats["completed"]["ready"] == stats["completed"]["summary_ready"] == 1
        stats["completed"]["ready"] = -99
        stats["admitted"] = -99
        assert worker.stats()["completed"]["ready"] == 1 and worker.stats()["admitted"] == 2
    finally:
        worker.stop()
    assert worker.stats()["accepting"] is False
    assert any(" idle " in message for message in messages)
    assert any(" stopped " in message for message in messages)
    assert all(value not in "\n".join(messages) for value in private_values)


@pytest.mark.parametrize("result", [DetailPrefetchResult("error"), DetailPrefetchResult("skipped"),
                                  DetailPrefetchResult("superseded")])
def test_permanent_errors_and_nonready_results_do_not_retry(result):
    service = MemoryService(lambda ref, **kw: result)
    worker = DetailPrefetcher(service, wall_clock=Clock(), retry_backoff_seconds=0)
    worker.start()
    try:
        worker.offer_products([product()])
        idle(worker)
        assert len(service.calls) == 1
        assert worker.stats()["completed"][result.status] == 1
    finally:
        worker.stop()


@pytest.mark.parametrize("status,ttl", [("ready", 10), ("error", 60)])
def test_cooldowns_respect_service_next_check_and_bounded_failure_duration(status, ttl):
    clock = Clock()
    service = MemoryService(lambda ref, **kw: DetailPrefetchResult(status, next_check_at=clock() + 10))
    worker = DetailPrefetcher(service, wall_clock=clock)
    worker.start()
    try:
        assert worker.offer_products([product()]) == 1
        idle(worker)
        clock.value += ttl - 1
        assert worker.offer_products([product()]) == 0
        clock.value += 1
        assert worker.offer_products([product()]) == 1
        idle(worker)
        assert len(service.calls) == 2
    finally:
        worker.stop()


def test_cooldown_memory_has_a_fixed_bound():
    clock = Clock()
    service = MemoryService()
    worker = DetailPrefetcher(service, wall_clock=clock)
    worker.start()
    try:
        for offset in range(0, COOLDOWN_CAPACITY + 40, 20):
            worker.offer_products([product(str(i)) for i in range(offset, offset + 20)])
            idle(worker)
        assert len(worker._cooldowns) == COOLDOWN_CAPACITY
    finally:
        worker.stop()


def test_stop_interrupts_long_retry_backoff_and_leaves_no_live_worker():
    service = MemoryService(lambda ref, **kw: DetailPrefetchResult("error", retryable=True))
    worker = DetailPrefetcher(service, retry_backoff_seconds=3600)
    worker.start()
    worker.offer_products([product()])
    service.wait_calls(1)
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 1
    assert len(service.calls) == 1
    assert not worker._thread.is_alive()


def test_active_seed_warms_connections_once_and_only_two_first_pages_per_eligible_platform():
    clock = Clock(0)
    service = MemoryService()
    warms = []
    descriptors = [
        {"code": "pdd", "active": True, "capabilities": {"recommend": True, "detail": True}},
        {"code": "taobao", "active": True, "capabilities": {"recommend": True, "detail": True}},
        {"code": "disabled", "active": False},
        {"code": "no-detail", "active": True, "capabilities": {"recommend": True, "detail": False}},
        {"code": "no-recommend", "active": True, "capabilities": {"recommend": False, "detail": True}},
    ]
    adapters = {entry["code"]: SimpleNamespace(
        channels=({"channel_type": 5}, {"channel_type": 6}, {"channel_type": 7}),
        warmup=lambda code=entry["code"]: warms.append(code),
    ) for entry in descriptors}
    worker = DetailPrefetcher(service, seed_enabled=True, clock=clock, wall_clock=Clock(),
                              platform_provider=lambda: descriptors, adapter_provider=adapters.__getitem__)
    service.prefetcher = worker
    worker.start()
    try:
        with service.condition:
            assert service.condition.wait_for(lambda: len(service.recommendations) >= 4, timeout=2)
        idle(worker)
        assert warms == ["pdd", "taobao", "no-detail", "no-recommend"]
        assert [(call["platform"], call["channel_type"]) for call in service.recommendations] == [
            ("pdd", 5), ("pdd", 6), ("taobao", 5), ("taobao", 6)]
        assert all(call == dict(platform=call["platform"], channel_type=call["channel_type"],
                                offset=0, limit=20, uid=None, cursor=None)
                   for call in service.recommendations)
        clock.value = 1000  # 错过多个周期也只补一轮，不积压追赶。
        with worker._condition:
            worker._condition.notify_all()
        with service.condition:
            assert service.condition.wait_for(lambda: len(service.recommendations) >= 8, timeout=2)
        idle(worker)
        assert len(service.recommendations) == 8
        stats = worker.stats()
        assert stats["seed_cycles"] == 2
        assert stats["admitted"] >= 8 and stats["completed"]["ready"] == 4
        assert warms == ["pdd", "taobao", "no-detail", "no-recommend"]
    finally:
        worker.stop()


def test_passive_mode_does_not_touch_platforms_and_still_handles_real_candidates():
    def forbidden():
        raise AssertionError("warmup=false forbids active seed requests")

    service = MemoryService()
    worker = DetailPrefetcher(service, seed_enabled=False, platform_provider=forbidden)
    worker.start()
    try:
        worker.offer_products([product()])
        idle(worker)
        assert len(service.calls) == 1 and service.recommendations == []
        assert service.calls[0][1]["refresh_ahead_seconds"] == 330
    finally:
        worker.stop()
