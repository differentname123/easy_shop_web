# -*- coding: utf-8 -*-
"""覆盖真实 lifespan 的冷启动，不能用路由替身掩盖预热旁路。"""
import logging
import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import adapters
import config
from adapters.pdd_adapter import PddAdapter
from api import home
from cache import clear_all_caches
from main import create_app
from tests.offline_support import BLOCKED_CALLS, assert_no_blocked_calls


def test_application_import_uses_only_offline_configuration_and_logger():
    assert config.PDD_CLIENT_ID == "offline-client"
    assert config.MONGO_DB_NAME == "mobile_web_offline"
    assert config.AUTH_SECRET == "mobile-web-offline-test-secret-not-for-deployment"
    assert not any(isinstance(handler, logging.FileHandler) for handler in config.logger.handlers)


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
        for thread in threading.enumerate():
            if thread not in started_before and thread.name in ("platform-warmup", "bg-upsert-home-products"):
                thread.join(timeout=2)
                assert not thread.is_alive(), "预热线程不能跨出测试替身生命周期"
    assert writes == [(items, {"channel_type": 5})]
    adapter.warmup.assert_called_once()
    adapter.get_recommend_goods.assert_called_once()
    assert BLOCKED_CALLS[blocked_start:] == []
