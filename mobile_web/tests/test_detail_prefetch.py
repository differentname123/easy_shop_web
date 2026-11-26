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
    COOLDOWN_CAPACITY, DEFAULT_INTERVAL_SECONDS, PAGE_LIMIT, QUEUE_CAPACITY,
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


def test_batch_logging_distinguishes_proven_off_promotion_from_real_errors(monkeypatch):
    from services import detail_prefetch as detail_prefetch_mod

    records = []
    rec_lock = threading.Lock()

    def _rec(level, msg, *args):
        text = msg % args if args else str(msg)
        with rec_lock:
            records.append((level, text))

    monkeypatch.setattr(
        detail_prefetch_mod,
        "logger",
        SimpleNamespace(
            debug=lambda msg, *a: _rec("DEBUG", msg, *a),
            info=lambda msg, *a: _rec("INFO", msg, *a),
            warning=lambda msg, *a: _rec("WARNING", msg, *a),
            error=lambda msg, *a: _rec("ERROR", msg, *a),
            exception=lambda msg, *a: _rec("ERROR", msg, *a),
            isEnabledFor=lambda lvl: True,
        ),
    )

    # (a) 确定性商品下架（skipped + PRODUCT_OFF_PROMOTION）：零 ❌，只记普通下架清理 INFO
    records.clear()
    svc_off = MemoryService(
        lambda ref, **k: DetailPrefetchResult(
            "skipped" if ref.goods_id == "1" else "ready",
            next_check_at=2000,
            error_code="PRODUCT_OFF_PROMOTION" if ref.goods_id == "1" else "",
        )
    )
    worker = DetailPrefetcher(svc_off, wall_clock=Clock())
    worker.start()
    try:
        assert worker.offer_products([product(str(i)) for i in range(1, 7)]) == 6
        idle(worker)
    finally:
        worker.stop()
    msgs_a = [msg for _, msg in records]
    assert all("❌" not in m for m in msgs_a)
    assert any("【详情预取】本批次已自动清理已确认下架/退推商品 [1/6条]" in m for m in msgs_a)

    # (b) 哪怕只有 1 条接口/调用失败（如 40001）：Fail-Closed 必须立刻打 ❌ 并携带具体错误码与原因
    records.clear()
    svc_single_err = MemoryService(
        lambda ref, **k: DetailPrefetchResult(
            "error" if ref.goods_id == "1" else "ready",
            next_check_at=2000,
            error_code="40001" if ref.goods_id == "1" else "",
            error_msg="[15:40001] 内部调用失败" if ref.goods_id == "1" else "",
        )
    )
    worker = DetailPrefetcher(svc_single_err, wall_clock=Clock())
    worker.start()
    try:
        assert worker.offer_products([product(str(i), platform="taobao") for i in range(1, 7)]) == 6
        idle(worker)
    finally:
        worker.stop()
    msgs_b = [msg for _, msg in records]
    assert any(
        "❌ 【详情预取告警】本批次后台预取存在失败任务" in m
        and "taobao[40001]:1条" in m
        and "[15:40001] 内部调用失败" in m
        for m in msgs_b
    )

    # (c) 出现数据库落盘失败：必须打 ❌ 告警
    records.clear()
    captured = snapshot("123", "READY", 995, platform="pdd")
    svc_persist = MemoryService(lambda ref, **k: DetailPrefetchResult("persist_failed", snapshot=captured, retryable=True))
    worker = DetailPrefetcher(svc_persist, wall_clock=Clock(), retry_backoff_seconds=0)
    worker.start()
    try:
        assert worker.offer_products([product("123", sign="READY")]) == 1
        idle(worker)
    finally:
        worker.stop()
    msgs_c = [msg for _, msg in records]
    assert any("❌ 【详情预取告警】本批次后台预取存在数据库落盘失败任务" in m for m in msgs_c)


def test_constants_and_explicit_lifecycle_never_start_threads_in_constructor():
    assert (QUEUE_CAPACITY, PAGE_LIMIT, DEFAULT_INTERVAL_SECONDS) == (128, 20, 300)
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


def test_seed_pulls_full_rank_summaries_without_fetching_extra_details():
    """验证榜单续页仅批量入库摘要，首屏详情预热正常，后续页绝不增加详情 API 调用。"""
    written_batches = []
    detail_calls = []

    class MockAdapter:
        platform_code = "pdd"
        channels = ({"channel_type": 5, "name": "实时榜"},)
        recommendation_limit = 400

        def __init__(self):
            self.recommend_calls = []

        def warmup(self):
            pass

        def get_recommend_goods(self, channel_type=5, offset=0, limit=20, uid=None, detail_sink=None, **kwargs):
            self.recommend_calls.append({
                "channel_type": channel_type, "offset": offset, "limit": limit,
                "detail_sink": detail_sink, "kwargs": kwargs,
            })
            if offset == 0:
                return {
                    "status": "success",
                    "data": [{"platform": "pdd", "goods_id": f"page1_item_{i}", "goods_sign": f"s1_{i}", "name": f"首屏品{i}"} for i in range(20)],
                    "has_more": True,
                    "next_offset": 20,
                    "next_cursor": "cursor_page2",
                }
            elif offset == 20:
                return {
                    "status": "success",
                    "data": [{"platform": "pdd", "goods_id": f"page2_item_{i}", "goods_sign": f"s2_{i}", "name": f"续页品{i}"} for i in range(20)],
                    "has_more": False,
                    "next_offset": 40,
                    "next_cursor": None,
                }
            return {"status": "success", "data": [], "has_more": False}

    adapter = MockAdapter()

    class MockService:
        def __init__(self):
            self.prefetcher = None
            self.repo = SimpleNamespace(
                upsert_home_products=lambda items, channel_type: written_batches.append((channel_type, [it["goods_id"] for it in items]))
            )

        def get_home_recommendations(self, platform="pdd", channel_type=5, offset=0, limit=20, uid=None, cursor=None):
            # 模拟首屏行为：调用 adapter 并将首屏交付给 prefetcher
            res = adapter.get_recommend_goods(channel_type=channel_type, offset=offset, limit=limit, uid=uid)
            items = res.get("data") or []
            if self.prefetcher:
                self.prefetcher.offer_products(items, channel_type=channel_type)
            return res

        def prefetch_product_detail(self, ref, **kwargs):
            detail_calls.append(ref.goods_id)
            return DetailPrefetchResult("ready", next_check_at=2000)

    service = MockService()
    descriptors = [{"code": "pdd", "active": True, "capabilities": {"recommend": True, "detail": True}}]
    worker = DetailPrefetcher(
        service, seed_enabled=False,
        platform_provider=lambda: descriptors,
        adapter_provider=lambda code: adapter,
        deep_seed_enabled=True,
    )
    service.prefetcher = worker
    worker.start()

    try:
        # 手动触发一次 seed
        worker._seed()
        idle(worker)

        # 1. 验证适配器被调用的参数
        assert len(adapter.recommend_calls) == 2
        # 首屏由 get_home_recommendations 触发
        assert adapter.recommend_calls[0]["offset"] == 0
        # 续页由 _seed 后续页循环触发，且 detail_sink 必须为 None
        assert adapter.recommend_calls[1]["offset"] == 20
        assert adapter.recommend_calls[1]["detail_sink"] is None
        assert adapter.recommend_calls[1]["kwargs"].get("cursor") == "cursor_page2"

        # 2. 验证两页商品均成功批量写入 MongoDB
        written_gids = [gid for _, gids in written_batches for gid in gids]
        assert len(written_gids) == 40
        assert "page1_item_0" in written_gids
        assert "page2_item_19" in written_gids

        # 3. 核心约束验证：只有首屏商品（page1）被调用了单品详情预取，续页商品（page2）绝对不增加详情请求！
        assert len(detail_calls) == 20
        assert all(gid.startswith("page1_item_") for gid in detail_calls)
        assert not any(gid.startswith("page2_item_") for gid in detail_calls)
    finally:
        worker.stop()


def test_worker_batches_same_platform_snapshotless_tasks_and_product_service_batch_prefetch(monkeypatch):
    """验证 DetailPrefetcher 同平台 <=20 批量聚合与 ProductService.prefetch_product_details_batch 单条失败隔离。"""
    from cache import clear_all_caches
    from repositories.product_repo import ProductRepository
    from services.product_service import ProductService

    # 1. DetailPrefetcher 将同平台无快照任务聚合成批、跨平台与带快照任务不混批
    batch_sizes = []

    class BatchService:
        def __init__(self):
            self.prefetcher = None
            self.single_calls = []

        def prefetch_product_detail(self, ref, **kwargs):
            self.single_calls.append((ref.platform, ref.goods_id, kwargs.get("snapshot") is not None))
            return DetailPrefetchResult("ready", next_check_at=2000)

        def prefetch_product_details_batch(self, refs, **kwargs):
            batch_sizes.append((refs[0].platform, [r.goods_id for r in refs]))
            return {r.key: DetailPrefetchResult("ready", next_check_at=2000) for r in refs}

    svc = BatchService()
    worker = DetailPrefetcher(svc, seed_enabled=False, clock=Clock(1000))
    # 未启动 worker 前不能入队，先直接构造运行并在停止状态前先挂起？用 start + 条件批量入队
    with worker._condition:
        worker._accepting = True
        for i in range(4):
            worker._offer_detail(ProductRef("jd", f"jd_{i}", f"jd_sign_{i}"))
        worker._offer_detail(ProductRef("pdd", "pdd_0", "pdd_sign_0"))
    worker.start()
    idle(worker)
    worker.stop()
    assert ("jd", [f"jd_{i}" for i in range(4)]) in batch_sizes
    assert any(c[:2] == ("pdd", "pdd_0") for c in svc.single_calls)

    # 2. ProductService.prefetch_product_details_batch 隔离坏项/下架并写入正常项
    clear_all_caches()
    docs = {}

    class MemRepo:
        def __init__(self):
            self.upserted = []
            self.deleted = []

        def get_product_detail(self, key, platform="jd", **_kw):
            return docs.get((platform, key))

        def get_known_goods_sign(self, platform, goods_id):
            return None

        def upsert_product_detail(self, item, only_if_newer=False):
            self.upserted.append(item)
            return True

        def delete_product(self, platform, goods_id=None, goods_sign=None):
            self.deleted.append((platform, goods_id, goods_sign))
            return 1

    repo = MemRepo()
    psvc = ProductService(product_repo=repo)
    fake_adapter = SimpleNamespace(
        active=True,
        platform_name="京东",
        capabilities={"detail": True},
        batch_get_goods_details=lambda refs, **_kw: {
            refs[0].key: {
                "status": "success",
                "data": {
                    "platform": "jd",
                    "platform_name": "京东",
                    "goods_id": "ok_img_1",
                    "goods_sign": "sign_ok_1",
                    "name": "批量正常商品",
                    "image_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/ok.jpg",
                    "price": 19.9,
                },
            },
            refs[1].key: {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "下架"},
        },
    )
    monkeypatch.setattr("services.product_service.get_adapter", lambda _plat: fake_adapter)
    res_map = psvc.prefetch_product_details_batch([
        ProductRef("jd", "ok_img_1", "sign_ok_1"),
        ProductRef("jd", "off_img_2", "sign_off_2"),
    ])
    assert res_map["sign_ok_1"].status == "ready"
    assert res_map["sign_off_2"].status == "skipped"
    assert res_map["sign_off_2"].error_code == "PRODUCT_OFF_PROMOTION"
    assert len(repo.upserted) == 1 and repo.upserted[0]["goods_id"] == "ok_img_1"
    assert ("jd", "off_img_2", "sign_off_2") in repo.deleted
    clear_all_caches()


def test_prefetch_skips_batch_when_batch_detail_lacks_gallery_and_skips_cacheable_false(monkeypatch):
    """验证 batch_detail_includes_gallery=False 时预取改走单品详情，且 cacheable=False 不写入详情缓存/DB。"""
    from cache import clear_all_caches
    from services.product_service import ProductService

    clear_all_caches()

    class MemRepo:
        def __init__(self):
            self.docs = {}
            self.upserted = []

        def get_product_detail(self, key, platform="pdd", **_kw):
            return self.docs.get((platform, key))

        def get_known_goods_sign(self, platform, goods_id):
            return None

        def upsert_product_detail(self, item, only_if_newer=False):
            self.upserted.append(item)
            self.docs[(item.get("platform", "pdd"), item.get("goods_id"))] = item
            return True

        def upsert_home_products(self, items, channel_type=None):
            return 0

        def delete_product(self, platform, goods_id=None, goods_sign=None):
            return 0

        def get_deal_tip_view(self, pid, platform="pdd"):
            return ("not_found", None)

    repo = MemRepo()
    psvc = ProductService(product_repo=repo)
    single_calls = []

    def _forbid_batch(*_args, **_kwargs):
        raise AssertionError("batch_get_goods_details must not run when batch_detail_includes_gallery is False")

    def _single_detail(goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **_kw):
        single_calls.append((goods_id, goods_sign))
        return {
            "status": "success",
            "data": {
                "platform": "pdd",
                "platform_name": "拼多多",
                "goods_id": goods_id or "101",
                "goods_sign": goods_sign or "SIGN_101",
                "name": "多图拼多多商品",
                "image_url": "https://img.pddpic.com/a.jpg",
                "carousel_images": [
                    "https://img.pddpic.com/a.jpg",
                    "https://img.pddpic.com/b.jpg",
                    "https://img.pddpic.com/c.jpg",
                ],
                "price": 19.9,
            },
        }

    pdd_adapter = SimpleNamespace(
        active=True,
        platform_name="拼多多",
        capabilities={"detail": True},
        batch_detail_includes_gallery=False,
        resolve_product_ref=lambda goods_id_or_sign="", goods_id=None, goods_sign=None: ProductRef("pdd", goods_id or "", goods_sign or ""),
        batch_get_goods_details=_forbid_batch,
        get_goods_detail=_single_detail,
    )
    monkeypatch.setattr("services.product_service.get_adapter", lambda _plat: pdd_adapter)

    res_map = psvc.prefetch_product_details_batch([
        ProductRef("pdd", "101", "SIGN_101"),
        ProductRef("pdd", "102", "SIGN_102"),
    ])
    assert res_map["SIGN_101"].status == "ready"
    assert res_map["SIGN_102"].status == "ready"
    assert len(single_calls) == 2
    assert len(repo.upserted) == 2
    assert len(repo.upserted[0]["carousel_images"]) == 3

    # 2. cacheable=False（如限流降级 goods.search 无图库结果）可直接返回展示，但不得持久化或写入内存详情缓存
    clear_all_caches()
    repo.upserted.clear()
    fallback_calls = 0

    def _non_cacheable_detail(**_kw):
        nonlocal fallback_calls
        fallback_calls += 1
        return {
            "status": "success",
            "cacheable": False,
            "_provider": "pdd_goods_search_sign_detail",
            "data": {
                "platform": "pdd",
                "platform_name": "拼多多",
                "goods_id": "202",
                "goods_sign": "SIGN_202",
                "name": "限流降级单图商品",
                "image_url": "https://img.pddpic.com/only1.jpg",
                "carousel_images": ["https://img.pddpic.com/only1.jpg"],
                "price": 9.9,
            },
        }

    pdd_adapter.get_goods_detail = _non_cacheable_detail
    first = psvc.get_goods_detail(platform="pdd", goods_id="202", goods_sign="SIGN_202")
    second = psvc.get_goods_detail(platform="pdd", goods_id="202", goods_sign="SIGN_202")
    assert first["status"] == "success" and first["data"]["name"] == "限流降级单图商品"
    assert second["status"] == "success"
    assert fallback_calls == 2
    assert len(repo.upserted) == 0
    clear_all_caches()



