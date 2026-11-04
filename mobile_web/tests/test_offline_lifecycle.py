# -*- coding: utf-8 -*-
"""覆盖真实 lifespan 的冷启动，不能用路由替身掩盖预热旁路。"""
import asyncio
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import adapters
import config
from adapters.pdd_adapter import PddAdapter
from api import goods, home
import main as main_module
from models.product import DetailPrefetchResult
from cache import clear_all_caches
from main import create_app
from tests.offline_support import BLOCKED_CALLS, assert_no_blocked_calls


def test_application_import_uses_only_offline_configuration_and_logger():
    assert config.PDD_CLIENT_ID == "offline-client"
    assert config.MONGO_DB_NAME == "mobile_web_offline"
    assert config.AUTH_SECRET == "mobile-web-offline-test-secret-not-for-deployment"
    assert not any(isinstance(handler, logging.FileHandler) for handler in config.logger.handlers)
    assert config.settings.detail_prefetch_enabled is False


def test_caught_import_attempt_is_not_lost_before_function_fixtures():
    start = len(BLOCKED_CALLS)
    try:
        BLOCKED_CALLS.append("simulated import attempt")
        with pytest.raises(AssertionError, match="simulated import attempt"):
            assert_no_blocked_calls()
    finally:
        del BLOCKED_CALLS[start:]
    assert_no_blocked_calls()


def test_cold_lifespan_preheat_uses_the_assembled_repository(monkeypatch):
    clear_all_caches()
    adapter = MagicMock(spec=PddAdapter)
    adapter.active = True
    adapter.platform_name = "离线拼多多"
    adapter.channels = PddAdapter.channels
    items = [{"platform": "pdd", "goods_id": "123", "goods_sign": "SIGN_A", "name": "仅测试", "price": 10}]
    adapter.get_recommend_goods.return_value = {"status": "success", "data": items}
    monkeypatch.setitem(adapters._ADAPTER_REGISTRY, "pdd", adapter)
    upsert_finished = threading.Event()
    writes = []

    class MemoryProducts:
        def upsert_home_products(self, products, **kwargs):
            writes.append((products, kwargs))
            upsert_finished.set()
            return len(products)

    monkeypatch.setattr(home.product_service, "repo", MemoryProducts())
    blocked_start = len(BLOCKED_CALLS)
    started_before = set(threading.enumerate())
    try:
        with TestClient(create_app()) as client:
            assert upsert_finished.wait(timeout=2), "预热没有使用已装配的内存仓储"
            assert client.get("/health").json()["status"] == "ok"
            assert client.get("/api/home/recommend").json()["data"] == items
    finally:
        assert not any(thread not in started_before and thread.name in
                       ("platform-warmup", "bg-upsert-home-products", "product-detail-prefetch")
                       for thread in threading.enumerate()), "lifespan 必须自行回收后台线程"
    assert writes == [(items, {"channel_type": 5})]
    adapter.warmup.assert_called_once()
    adapter.get_recommend_goods.assert_called_once()
    assert BLOCKED_CALLS[blocked_start:] == []


class LifecycleProducts:
    def __init__(self):
        self.prefetcher = None
        self.calls = []
        self.details = []
        self.writes = []
        self.wait_threads = []
        self.write_starts = 0
        self.condition = threading.Condition()
        self.detail_entered = threading.Event()
        self.detail_release = None
        self.repo = SimpleNamespace(upsert_home_products=self.upsert)

    def upsert(self, items, **kwargs):
        self.writes.append((items, kwargs))
        return len(items)

    def get_home_recommendations(self, **kwargs):
        items = [{"platform": "pdd", "goods_id": str(kwargs["channel_type"]), "goods_sign": ""}]
        if self.prefetcher is not None:
            self.prefetcher.offer_products(items, channel_type=kwargs["channel_type"])
        with self.condition:
            self.calls.append(kwargs)
            self.condition.notify_all()
        return {"status": "success", "data": items}

    def prefetch_product_detail(self, ref, **kwargs):
        self.details.append(ref)
        self.detail_entered.set()
        if self.detail_release is not None:
            assert self.detail_release.wait(3)
        return DetailPrefetchResult("ready", next_check_at=2_000_000_000)

    def start_background_writes(self):
        self.write_starts += 1

    def wait_for_background_writes(self):
        self.wait_threads.append(threading.get_ident())


def install_lifecycle_fakes(monkeypatch, *, warmup, prefetch):
    service, other = LifecycleProducts(), LifecycleProducts()
    adapter = SimpleNamespace(channels=({"channel_type": 5}, {"channel_type": 6}, {"channel_type": 7}),
                              warmup=MagicMock())
    monkeypatch.setattr(home, "product_service", service)
    monkeypatch.setattr(goods, "product_service", other)
    monkeypatch.setattr(main_module, "settings", replace(config.settings, warmup_enabled=warmup,
                                                       detail_prefetch_enabled=prefetch))
    monkeypatch.setattr(main_module, "get_all_platforms", lambda: [
        {"code": "pdd", "active": True, "capabilities": {"recommend": True, "detail": True}}])
    monkeypatch.setattr(main_module, "get_adapter", lambda platform: adapter)
    return service, other, adapter


@pytest.mark.parametrize("warmup,prefetch,seed_calls", [
    (False, False, 0), (False, True, 0), (True, False, 1), (True, True, 2),
])
def test_lifespan_four_switch_combinations_and_bindings(monkeypatch, warmup, prefetch, seed_calls):
    service, other, adapter = install_lifecycle_fakes(monkeypatch, warmup=warmup, prefetch=prefetch)
    app = create_app()
    original_repo = service.repo
    worker = None
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        if warmup:
            with service.condition:
                assert service.condition.wait_for(lambda: len(service.calls) >= seed_calls, timeout=2)
        assert len(service.calls) == seed_calls
        assert service.repo is original_repo, "lifespan 不能另造仓储"
        if prefetch:
            worker = app.state.detail_prefetcher
            assert service.prefetcher is other.prefetcher is worker
            assert worker.seed_enabled is warmup
            if not warmup:
                assert service.details == [] and service.calls == []
                other.prefetcher.offer_products([{"platform": "pdd", "goods_id": "passive"}])
                assert service.detail_entered.wait(2)
                assert service.details[0].goods_id == "passive"
                assert other.details == [], "goods 列表候选复用已装配 home 服务"
        else:
            assert service.prefetcher is other.prefetcher is None
            assert app.state.detail_prefetcher is None
    assert service.prefetcher is other.prefetcher is None
    assert app.state.detail_prefetcher is None
    assert len(service.calls) == seed_calls
    assert len(service.wait_threads) == len(other.wait_threads) == 1
    assert adapter.warmup.call_count == int(warmup)
    if worker:
        assert not worker._thread.is_alive()


def test_repeated_lifespan_creates_fresh_worker_and_reclaims_it(monkeypatch):
    service, other, _ = install_lifecycle_fakes(monkeypatch, warmup=False, prefetch=True)
    app = create_app()
    workers = []
    for _ in range(2):
        with TestClient(app):
            worker = service.prefetcher
            assert worker is other.prefetcher
            assert worker._thread.is_alive()
            workers.append(worker)
        assert not worker._thread.is_alive()
        assert service.prefetcher is other.prefetcher is None
        assert worker.offer_products([{"goods_id": "after-shutdown"}], channel_type=5) == 0
    assert workers[0] is not workers[1]
    assert len(service.wait_threads) == len(other.wait_threads) == 2
    assert service.write_starts == other.write_starts == 2


def test_shutdown_waits_for_io_without_blocking_event_loop_and_unbinds_only_after_exit(monkeypatch):
    service, other, _ = install_lifecycle_fakes(monkeypatch, warmup=False, prefetch=True)
    release = threading.Event()
    service.detail_release = release
    app = create_app()

    async def exercise():
        context = main_module.lifespan(app)
        await context.__aenter__()
        worker = service.prefetcher
        worker.offer_products([{"platform": "pdd", "goods_id": "blocked"}])
        assert await asyncio.to_thread(service.detail_entered.wait, 2)
        shutdown = asyncio.create_task(context.__aexit__(None, None, None))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(worker._stop_event.wait, 2), timeout=2)
            # 若 join 在事件循环同步执行，这里的协程根本无法继续。
            await asyncio.sleep(0)
            assert not shutdown.done()
            assert service.prefetcher is other.prefetcher is worker
            assert worker.offer_products([{"goods_id": "rejected"}]) == 0
        finally:
            release.set()
            await asyncio.wait_for(shutdown, timeout=2)
        assert not worker._thread.is_alive()
        assert service.prefetcher is other.prefetcher is None
        assert all(ident != threading.get_ident() for ident in service.wait_threads + other.wait_threads)

    asyncio.run(exercise())
