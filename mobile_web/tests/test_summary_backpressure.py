"""Cache-only summary writes have nonblocking, bounded admission and joined shutdown."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import product_service as module
from services.product_service import ProductService


class BlockingSummaries:
    def __init__(self, expected):
        self.expected = expected
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = []
        self.active = 0
        self.maximum = 0

    def upsert_home_products(self, items, **kwargs):
        with self.lock:
            self.calls.append((items, kwargs))
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if len(self.calls) >= self.expected:
                self.entered.set()
        try:
            assert self.release.wait(5)
            return len(items)
        finally:
            with self.lock:
                self.active -= 1


def fill_capacity(service, repo):
    for index in range(service._MAX_BACKGROUND_WRITES):
        service._offer_products([{"goods_id": str(index)}], channel_type=5)
    assert repo.entered.wait(2)


def test_summary_admission_is_bounded_and_overload_is_nonblocking_and_logged(monkeypatch):
    repo = BlockingSummaries(ProductService._MAX_BACKGROUND_WRITES)
    service = ProductService(repo)
    warning = Mock()
    monkeypatch.setattr(module, "logger", SimpleNamespace(warning=warning))
    try:
        fill_capacity(service, repo)
        with ThreadPoolExecutor(max_workers=4) as callers:
            requests = [callers.submit(service._offer_products, [{"goods_id": f"drop-{index}"}], 5)
                        for index in range(24)]
            for request in requests:
                # A full writer must not wait for repository I/O or an available slot.
                request.result(timeout=2)
        assert len(repo.calls) == repo.maximum == service._MAX_BACKGROUND_WRITES
        with service._write_lock:
            assert len(service._background_writes) == service._MAX_BACKGROUND_WRITES
        assert warning.call_count == 24
        assert all("满载" in call.args[0] for call in warning.call_args_list)
    finally:
        repo.release.set()
        service.wait_for_background_writes()
    assert repo.active == 0 and service._background_writes == set()


def test_foreground_recommendation_returns_while_summary_writes_are_full(monkeypatch):
    repo = BlockingSummaries(ProductService._MAX_BACKGROUND_WRITES)
    service = ProductService(repo)
    result = {"status": "success", "data": [{"platform": "pdd", "goods_id": "foreground"}]}
    adapter = SimpleNamespace(active=True, platform_name="fixture", capabilities=None, channels=(),
                              get_recommend_goods=Mock(return_value=result))
    monkeypatch.setattr(module, "get_adapter", lambda platform: adapter)
    try:
        fill_capacity(service, repo)
        with ThreadPoolExecutor(max_workers=1) as caller:
            response = caller.submit(service.get_home_recommendations, force_refresh=True).result(timeout=2)
        assert response is result and response["status"] == "success"
        assert len(repo.calls) == service._MAX_BACKGROUND_WRITES
        assert not repo.release.is_set()
    finally:
        repo.release.set()
        service.wait_for_background_writes()


def test_shutdown_closes_admission_joins_writes_and_restart_reuses_capacity(monkeypatch):
    repo = BlockingSummaries(ProductService._MAX_BACKGROUND_WRITES)
    service = ProductService(repo)
    joining = threading.Event()
    workers = []
    try:
        fill_capacity(service, repo)
        with service._write_lock:
            workers = list(service._background_writes)
        for worker in workers:
            original_join = worker.join

            def observed_join(*args, _join=original_join, **kwargs):
                joining.set()
                return _join(*args, **kwargs)

            monkeypatch.setattr(worker, "join", observed_join)
        with ThreadPoolExecutor(max_workers=1) as pool:
            shutdown = pool.submit(service.wait_for_background_writes)
            try:
                assert joining.wait(2)
                assert not shutdown.done()
                assert not service._writes_open
                service._offer_products([{"goods_id": "closed"}], channel_type=5)
                assert len(repo.calls) == service._MAX_BACKGROUND_WRITES
            finally:
                repo.release.set()
            shutdown.result(timeout=2)
        assert service._background_writes == set()
        assert not any(worker.is_alive() for worker in workers)
        service.start_background_writes()
        service._offer_products([{"goods_id": "restart"}], channel_type=7)
        service.wait_for_background_writes()
        assert len(repo.calls) == service._MAX_BACKGROUND_WRITES + 1
        assert repo.calls[-1] == ([{"goods_id": "restart"}], {"channel_type": 7})
    finally:
        repo.release.set()
        service.wait_for_background_writes()


@pytest.mark.parametrize("failure", ["construct", "start"])
def test_thread_creation_or_start_failure_returns_admission_slot(monkeypatch, failure):
    repo = SimpleNamespace(upsert_home_products=Mock(return_value=1))
    service = ProductService(repo)
    real_thread = threading.Thread
    attempts = []
    warning = Mock()
    monkeypatch.setattr(module, "logger", SimpleNamespace(warning=warning))

    def thread_factory(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            if failure == "construct":
                raise RuntimeError("cannot construct thread")
            return Mock(start=Mock(side_effect=RuntimeError("cannot start thread")))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(module.threading, "Thread", thread_factory)
    try:
        service._offer_products([{"goods_id": "failed"}], channel_type=5)
        assert service._background_writes == set()
        # Exactly all slots are available again, not merely one spare slot.
        for _ in range(service._MAX_BACKGROUND_WRITES):
            assert service._summary_write_slots.acquire(blocking=False)
        assert not service._summary_write_slots.acquire(blocking=False)
        for _ in range(service._MAX_BACKGROUND_WRITES):
            service._summary_write_slots.release()
        service._offer_products([{"goods_id": "retry"}], channel_type=5)
    finally:
        service.wait_for_background_writes()
    repo.upsert_home_products.assert_called_once_with([{"goods_id": "retry"}], channel_type=5)
    assert "未能启动" in warning.call_args.args[0]


def test_repository_exception_releases_capacity_and_shutdown_still_drains(monkeypatch):
    repo = SimpleNamespace(upsert_home_products=Mock(side_effect=RuntimeError("database unavailable")))
    service = ProductService(repo)
    warning = Mock()
    monkeypatch.setattr(module, "logger", SimpleNamespace(warning=warning))
    service._offer_products([{"goods_id": "failed"}], channel_type=5)
    service.wait_for_background_writes()
    assert service._background_writes == set()
    assert "入库失败" in warning.call_args.args[0]
    for _ in range(service._MAX_BACKGROUND_WRITES):
        assert service._summary_write_slots.acquire(blocking=False)
    for _ in range(service._MAX_BACKGROUND_WRITES):
        service._summary_write_slots.release()
    service.start_background_writes()
    repo.upsert_home_products.side_effect = None
    service._offer_products([{"goods_id": "retry"}], channel_type=5)
    service.wait_for_background_writes()
    assert repo.upsert_home_products.call_count == 2


def test_enabled_prefetcher_keeps_its_existing_offer_path_even_when_writers_full():
    repo = BlockingSummaries(ProductService._MAX_BACKGROUND_WRITES)
    service = ProductService(repo)
    try:
        fill_capacity(service, repo)
        service.prefetcher = SimpleNamespace(offer_products=Mock())
        items, snapshots = [{"goods_id": "prefetch"}], (object(),)
        service._offer_products(items, channel_type=7, snapshots=snapshots)
        service.prefetcher.offer_products.assert_called_once_with(items, channel_type=7, snapshots=snapshots)
        assert len(repo.calls) == service._MAX_BACKGROUND_WRITES
    finally:
        repo.release.set()
        service.wait_for_background_writes()
