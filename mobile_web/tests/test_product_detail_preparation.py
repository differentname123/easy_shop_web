"""前后台共用详情流程；只使用内存仓储、假平台和可控并发。"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from cache import DETAIL_CACHE, RECOMMEND_CACHE
from config import PRODUCT_DETAIL_TTL
from models.product import DetailSnapshot, ProductRef
from services import product_service as module
from services.product_service import ProductService


NOW = 1800000000.0


def data(sign="SIGN", goods_id="123", price=10):
    return {"platform": "fixture", "goods_id": goods_id, "goods_sign": sign,
            "name": "商品", "image_url": "https://image.test/pic", "price": price,
            "original_price": price + 1, "desc": "完整描述", "carousel_images": ["https://image.test/gallery"]}


def snapshot(sign="SIGN", goods_id="123", price=10, fetched=NOW):
    return DetailSnapshot(ProductRef("fixture", goods_id, sign), data(sign, goods_id, price),
                          datetime.fromtimestamp(fetched, timezone.utc))


class MemoryDetails:
    def __init__(self):
        self.docs = {}
        self.writes = 0
        self.reads = 0
        self.write_failure = False
        self.read_failure = False
        self.read_twice = threading.Event()
        self.lock = threading.RLock()

    def add(self, seed):
        with self.lock:
            self.docs[(seed.ref.platform, seed.ref.goods_id or "sign:" + seed.ref.goods_sign)] = dict(
                deepcopy(seed.data), detail_updated_at=seed.fetched_at)

    def get_product_detail(self, key, platform="fixture", key_type=None, strict=False):
        with self.lock:
            self.reads += 1
            if self.reads >= 2:
                self.read_twice.set()
            if self.read_failure:
                if strict:
                    raise RuntimeError("database unavailable")
                return None
            field = "goods_sign" if key_type == "sign" else "goods_id"
            return next((deepcopy(doc) for doc in self.docs.values()
                         if doc["platform"] == platform and doc.get(field) == key), None)

    def upsert_product_detail(self, document):
        with self.lock:
            self.writes += 1
            if self.write_failure:
                return False
            key = (document["platform"], document.get("goods_id") or "sign:" + document["goods_sign"])
            existing = self.docs.get(key)
            if existing and existing["detail_updated_at"] >= document["detail_updated_at"]:
                return False
            self.docs[key] = deepcopy(document)
            return True


@pytest.fixture
def prepared(monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(module.time, "time", lambda: clock.now)
    repo = MemoryDetails()
    adapter = SimpleNamespace(
        active=True, platform_name="测试平台", channels=({"channel_type": 5, "name": "实时"},), capabilities=None,
        resolve_product_ref=lambda raw="", goods_id=None, goods_sign=None:
            ProductRef("fixture", goods_id or (raw if not goods_sign else ""), goods_sign or ""),
        can_prefetch_detail=lambda ref: bool(ref.key),
        get_goods_detail=Mock(return_value={"status": "success", "data": data()}),
        get_recommend_goods=Mock(),
    )
    monkeypatch.setattr(module, "get_adapter", lambda platform: adapter)
    return ProductService(repo), repo, adapter, clock


def test_snapshot_only_contains_public_whitelisted_data():
    payload = dict(data(), raw_item={"secret": "never"}, click_url="private", relation_id="private",
                   promotion_token="private", uid="private")
    seed = DetailSnapshot(ProductRef("fixture", "123", "SIGN"), payload,
                          datetime.fromtimestamp(NOW + 0.123456, timezone.utc))
    assert seed.fetched_at.microsecond == 123000
    assert not {"raw_item", "click_url", "relation_id", "promotion_token", "uid"} & seed.data.keys()
    payload["carousel_images"].append("mutated")
    assert "mutated" not in seed.data["carousel_images"]
    with pytest.raises(ValueError):
        DetailSnapshot(ProductRef("fixture", "123", "OTHER"), data(), seed.fetched_at)


def test_list_seed_warms_memory_before_database_without_polluting_list(prepared):
    service, repo, adapter, _ = prepared
    received = []
    service.prefetcher = SimpleNamespace(offer_products=lambda *args, **kwargs: received.append((args, kwargs)))
    summary = {key: value for key, value in data().items() if key not in ("desc", "carousel_images")}
    def recommend(**kwargs):
        kwargs["detail_sink"](snapshot())
        return {"status": "success", "data": [summary], "has_more": False}
    adapter.get_recommend_goods.side_effect = recommend
    result = service.get_home_recommendations(platform="fixture")
    assert "desc" not in result["data"][0] and "detail_sink" not in result
    assert all("fetched_at" not in repr(entry) for entry in [RECOMMEND_CACHE.get(("fixture", "", 5, 0, 20, ""))])
    assert received[0][1]["snapshots"][0].data["desc"] == "完整描述"
    assert service.get_goods_detail(platform="fixture", goods_sign="SIGN")["data"]["desc"] == "完整描述"
    adapter.get_goods_detail.assert_not_called()
    assert repo.writes == 0 and repo.reads == 0


def test_seed_is_persisted_without_another_official_request(prepared):
    service, repo, adapter, _ = prepared
    seed = snapshot()
    result = service.prefetch_product_detail(seed.ref, snapshot=seed)
    assert result.status == "ready"
    assert next(iter(repo.docs.values()))["detail_updated_at"] == seed.fetched_at
    adapter.get_goods_detail.assert_not_called()


def test_expired_seed_is_not_retimestamped_or_written(prepared):
    service, repo, adapter, _ = prepared
    seed = snapshot(fetched=NOW - PRODUCT_DETAIL_TTL)
    result = service.prefetch_product_detail(seed.ref, snapshot=seed)
    assert result.status != "ready" and not result.retryable
    assert repo.writes == 0
    adapter.get_goods_detail.assert_not_called()


def test_failed_persistence_retries_original_snapshot_not_official(prepared):
    service, repo, adapter, clock = prepared
    repo.write_failure = True
    first = service.prefetch_product_detail(ProductRef("fixture", "123", "SIGN"))
    assert first.status == "persist_failed" and first.retryable
    clock.now += 5
    repo.write_failure = False
    retried = service.prefetch_product_detail(first.snapshot.ref, snapshot=first.snapshot)
    assert retried.status == "ready" and retried.snapshot.fetched_at == first.snapshot.fetched_at
    assert next(iter(repo.docs.values()))["detail_updated_at"].timestamp() == NOW
    adapter.get_goods_detail.assert_called_once()


def test_database_failure_is_not_a_missing_product_and_does_not_consume_api(prepared):
    service, repo, adapter, _ = prepared
    repo.read_failure = True
    result = service.prefetch_product_detail(ProductRef("fixture", "123", "SIGN"))
    assert result.status == "error" and result.retryable
    adapter.get_goods_detail.assert_not_called()


def test_fallback_is_not_counted_as_newly_prepared(prepared):
    service, repo, adapter, _ = prepared
    repo.add(snapshot(fetched=NOW - PRODUCT_DETAIL_TTL - 1))
    adapter.get_goods_detail.return_value = {"status": "error", "error_code": "NETWORK_ERROR"}
    result = service.prefetch_product_detail(ProductRef("fixture", "123", "SIGN"))
    assert result.status == "error" and result.retryable
    assert service.get_goods_detail(platform="fixture", goods_sign="SIGN")["source"] == "database_fallback"
    assert repo.writes == 0


def test_fresh_database_skips_platform_but_near_expiry_refreshes_ahead(prepared):
    service, repo, adapter, _ = prepared
    ref = ProductRef("fixture", "123", "SIGN")
    repo.add(snapshot(fetched=NOW - 100))
    assert service.prefetch_product_detail(ref).status == "ready"
    adapter.get_goods_detail.assert_not_called()
    repo.add(snapshot(fetched=NOW - 300))
    started, release = threading.Event(), threading.Event()
    def slow_detail(**kwargs):
        started.set()
        assert release.wait(2)
        return {"status": "success", "data": data(price=7)}
    adapter.get_goods_detail.side_effect = slow_detail
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(service.prefetch_product_detail, ref)
        try:
            assert started.wait(1)
            # A still-valid foreground snapshot never waits for background refresh.
            assert service.get_goods_detail(platform="fixture", goods_sign="SIGN")["data"]["price"] == 10
        finally:
            release.set()
        assert task.result(timeout=2).status == "ready"
    assert service.get_goods_detail(platform="fixture", goods_sign="SIGN")["data"]["price"] == 7
    adapter.get_goods_detail.assert_called_once()


@pytest.mark.parametrize("background", [False, True])
def test_equivalent_cold_requests_share_one_running_fetch(prepared, background):
    service, repo, adapter, _ = prepared
    started, release = threading.Event(), threading.Event()
    def slow_detail(**kwargs):
        started.set()
        assert release.wait(2)
        return {"status": "success", "data": data()}
    adapter.get_goods_detail.side_effect = slow_detail
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.get_goods_detail, platform="fixture", goods_id="123", goods_sign="SIGN")
        try:
            assert started.wait(1)
            second = (pool.submit(service.prefetch_product_detail, ProductRef("fixture", "123", "SIGN")) if background else
                      pool.submit(service.get_goods_detail, platform="fixture", goods_id="123", goods_sign="SIGN"))
            assert repo.read_twice.wait(1)
        finally:
            release.set()
        assert first.result(timeout=2)["status"] == "success"
        result = second.result(timeout=2)
        assert result.status == "ready" if background else result["status"] == "success"
    adapter.get_goods_detail.assert_called_once()
    assert service._flights == {}


def test_newer_database_winner_also_replaces_old_ready_cache_alias(prepared):
    service, repo, adapter, _ = prepared
    old = snapshot(sign="old-sign", fetched=NOW - 20, price=30)
    newer = snapshot(sign="new-sign", fetched=NOW - 5, price=5)
    repo.add(newer)
    service._cache_detail("fixture", {"status": "success", "data": old.data}, NOW + PRODUCT_DETAIL_TTL - 20)
    result = service.prefetch_product_detail(old.ref, snapshot=old)
    assert result.status == "ready" and result.snapshot.ref.goods_sign == "new-sign"
    assert service.get_goods_detail(platform="fixture", goods_sign="old-sign")["data"]["price"] == 5
    assert next(iter(repo.docs.values()))["goods_sign"] == "new-sign"
    adapter.get_goods_detail.assert_not_called()


def test_older_cache_publish_cannot_rollback_a_newer_trusted_alias(prepared):
    service, _, _, _ = prepared
    newer = snapshot(sign="new-sign", fetched=NOW - 5, price=5)
    old = snapshot(sign="old-sign", fetched=NOW - 20, price=30)
    service._cache_detail("fixture", {"status": "success", "data": newer.data}, NOW + PRODUCT_DETAIL_TTL - 5)
    service._cache_detail("fixture", {"status": "success", "data": old.data}, NOW + PRODUCT_DETAIL_TTL - 20)
    assert service.get_goods_detail(platform="fixture", goods_sign="old-sign")["data"]["price"] == 5


def test_cancelled_prefetch_does_not_start_database_or_official_calls(prepared):
    service, repo, adapter, _ = prepared
    cancelled = threading.Event()
    cancelled.set()
    assert service.prefetch_product_detail(ProductRef("fixture", "123", "SIGN"), stop_event=cancelled).status == "skipped"
    assert repo.reads == 0 and repo.writes == 0
    adapter.get_goods_detail.assert_not_called()


def test_provider_exception_releases_singleflight_for_next_attempt(prepared):
    service, _, adapter, _ = prepared
    adapter.get_goods_detail.side_effect = [RuntimeError("temporary"), {"status": "success", "data": data()}]
    with pytest.raises(RuntimeError):
        service.get_goods_detail(platform="fixture", goods_sign="SIGN")
    assert service._flights == {}
    assert service.get_goods_detail(platform="fixture", goods_sign="SIGN")["status"] == "success"
