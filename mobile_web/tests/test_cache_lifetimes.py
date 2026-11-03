# -*- coding: utf-8 -*-
"""来源截止时间、推广热路径和可展示内容的跨层回归，不连接真实服务。"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config import PRODUCT_DETAIL_TTL, PROMOTION_LINK_TTL
from services import product_service as product_module
from services import promote_service as promote_module
from repositories.product_repo import ProductRepository
from tests.test_catalog_boundaries import catalog, detail
from tests.test_account_order_refactor import promotion
from tests.test_product_repository import MemoryCollection


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(product_module.time, "time", lambda: clock.now)
    return clock


def test_database_detail_refill_preserves_deadline_across_aliases(catalog, clock):
    service, repo, adapter = catalog
    item = detail(price=20)["data"]
    item["detail_updated_at"] = datetime.fromtimestamp(clock.now - PRODUCT_DETAIL_TTL + 1, timezone.utc)
    repo.get_product_detail.return_value = item
    adapter.get_goods_detail.return_value = detail(price=9)

    first = service.get_goods_detail(goods_id="123")
    first["data"]["carousel_images"].append("changed")
    clock.now += 0.5
    cached = service.get_goods_detail(goods_sign="SIGN_A")
    assert cached["source"] == "database"
    assert "changed" not in cached["data"]["carousel_images"]
    assert repo.get_product_detail.call_count == 1

    clock.now += 0.5
    refreshed = service.get_goods_detail(goods_sign="SIGN_A")
    assert refreshed["data"]["price"] == 9
    assert repo.get_product_detail.call_count == 2
    adapter.get_goods_detail.assert_called_once()
    assert "expires_at" not in refreshed
    assert "detail_updated_at" not in refreshed["data"]


@pytest.mark.parametrize("age", [0, PRODUCT_DETAIL_TTL + 1])
def test_database_detail_http_and_cache_do_not_expose_source_metadata(catalog, clock, monkeypatch, age):
    from fastapi.testclient import TestClient
    from api import home
    from main import create_app

    service, repo, adapter = catalog
    item = detail()["data"]
    item["detail_updated_at"] = datetime.fromtimestamp(clock.now - age, timezone.utc)
    repo.get_product_detail.return_value = item
    adapter.get_goods_detail.return_value = {"status": "error"}
    monkeypatch.setattr(home, "product_service", service)
    client = TestClient(create_app())
    for key in ("goods_id=123", "goods_sign=SIGN_A"):
        response = client.get(f"/api/home/detail?{key}")
        assert response.status_code == 200
        result = response.json()
        assert result["source"] == ("database" if age == 0 else "database_fallback")
        assert "detail_updated_at" not in result["data"]
        assert "expires_at" not in result
    assert "detail_updated_at" in item
    assert adapter.get_goods_detail.call_count == (0 if age == 0 else 2)


def test_database_write_delay_does_not_restart_detail_lifetime(catalog, clock):
    service, repo, adapter = catalog
    fetched_at = clock.now
    adapter.get_goods_detail.side_effect = [detail(price=20), detail(price=9)]

    def slow_write(item):
        clock.now += PRODUCT_DETAIL_TTL
        return True

    repo.upsert_product_detail.side_effect = slow_write
    first = service.get_goods_detail(goods_id="123")
    stored = repo.upsert_product_detail.call_args.args[0]
    assert stored["detail_updated_at"] == datetime.fromtimestamp(fetched_at, timezone.utc)
    assert "detail_updated_at" not in first["data"]
    assert service.get_goods_detail(goods_id="123")["data"]["price"] == 9
    assert adapter.get_goods_detail.call_count == 2


@pytest.mark.parametrize("timestamp", [True, float("nan"), float("inf"), "bad-date", 1800000001.0])
def test_invalid_or_future_detail_source_time_is_not_fresh(catalog, clock, timestamp):
    service, repo, adapter = catalog
    repo.get_product_detail.return_value = detail(detail_updated_at=timestamp)["data"]
    adapter.get_goods_detail.return_value = detail(price=9)
    assert service.get_goods_detail(goods_id="123")["data"]["price"] == 9
    adapter.get_goods_detail.assert_called_once()


def test_detail_cache_remains_uid_scoped(catalog):
    service, _, adapter = catalog
    adapter.get_goods_detail.side_effect = [detail(price=20), detail(price=9)]
    assert service.get_goods_detail(goods_id="123", uid="u_1234abcd")["data"]["price"] == 20
    assert service.get_goods_detail(goods_id="123", uid="u_abcd1234")["data"]["price"] == 9
    assert service.get_goods_detail(goods_sign="SIGN_A", uid="u_1234abcd")["data"]["price"] == 20
    assert adapter.get_goods_detail.call_count == 2


def test_sign_promotion_hot_hit_skips_identity_and_renders_current_copy(promotion):
    service, adapter, repo = promotion
    repo.get_product_identity.return_value = {"platform": "future_shop", "product_id": "trusted-id"}
    first = service.convert_link("future_shop", goods_sign="SIGN_A", goods_id="wrong-id",
                                 uid="u_1234abcd", title="旧标题", price=20, coupon_amount=2)
    repo.get_product_identity.side_effect = AssertionError("热缓存不能查询身份")
    second = service.convert_link("future_shop", goods_sign="SIGN_A", goods_id="another-wrong-id",
                                  uid="u_1234abcd", title="新标题", price=9, coupon_amount=0)
    assert first["final_link"] == second["final_link"]
    assert "旧标题" in first["command_text"] and "¥20" in first["command_text"]
    assert "新标题" in second["command_text"] and "¥9" in second["command_text"]
    assert "旧标题" not in second["command_text"] and "立减" not in second["command_text"]
    repo.get_product_identity.assert_called_once_with("SIGN_A", platform="future_shop")
    repo.get_product_detail.assert_not_called()
    repo.get_promotion_link_cache.assert_not_called()
    adapter.convert_promotion_url.assert_called_once()
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "trusted-id"


def test_database_promotion_refill_preserves_expiry_and_rebuilds_copy(promotion, clock):
    service, adapter, repo = promotion
    repo.get_promotion_link_cache.return_value = {
        "status": "success", "final_link": "https://example.com/cached", "command_text": "过时文案",
        "from_cache": True, "_expires_at": clock.now + 1,
    }
    first = service.convert_link("future_shop", "sku-A", title="当前标题", price=7, coupon_amount=0)
    assert first["from_cache"] is True
    assert "当前标题" in first["command_text"] and "过时文案" not in first["command_text"]
    assert "_expires_at" not in first
    clock.now += 0.5
    second = service.convert_link("future_shop", "sku-A", title="另一标题", price=6)
    assert "另一标题" in second["command_text"]
    repo.get_promotion_link_cache.assert_called_once()
    adapter.convert_promotion_url.assert_not_called()

    clock.now += 0.5
    refreshed = service.convert_link("future_shop", "sku-A", title="到期刷新", price=5)
    assert refreshed["final_link"] == "https://example.com/promo"
    assert repo.get_promotion_link_cache.call_count == 2
    adapter.convert_promotion_url.assert_called_once()
    saved = repo.save_promotion_link_cache.call_args.kwargs
    assert "command_text" not in saved["data"]
    assert saved["expires_at"] == clock.now + PROMOTION_LINK_TTL


@pytest.mark.parametrize("expiry", [None, True, float("nan"), float("inf"), 1800000000.0, 1799999999.0,
                                    1800000000.0 + PROMOTION_LINK_TTL + 1])
def test_untrusted_promotion_deadlines_are_not_reused(promotion, clock, expiry):
    service, adapter, repo = promotion
    repo.get_promotion_link_cache.return_value = {
        "status": "success", "final_link": "https://example.com/old", "_expires_at": expiry,
    }
    assert service.convert_link("future_shop", "sku-A")["final_link"] == "https://example.com/promo"
    adapter.convert_promotion_url.assert_called_once()


def test_identity_only_product_is_not_detail_fallback_but_can_be_completed(catalog):
    service, _, adapter = catalog
    collection = MemoryCollection()
    service.repo = ProductRepository(SimpleNamespace(get_collection=lambda name: collection))
    assert service.repo.save_promotion_link_cache(
        "pdd", {"status": "success", "final_link": "https://example.com/promo"}, goods_sign="SIGN_A"
    )
    adapter.get_goods_detail.side_effect = [{"status": "error", "error_msg": "upstream failed"}, detail()]
    assert service.get_goods_detail(goods_sign="SIGN_A") == {"status": "error", "error_msg": "upstream failed"}
    assert service.repo.get_product_identity("SIGN_A")["product_id"] == "sign:SIGN_A"
    assert service.get_goods_detail(goods_sign="SIGN_A")["data"]["price"] == 10
    assert len(collection.docs) == 1
    assert service.repo.get_product_detail("SIGN_A", key_type="sign")["name"] == "商品"


def test_identity_only_record_can_still_supply_verified_promotion_id(promotion):
    service, adapter, _ = promotion
    collection = MemoryCollection()
    service.repo = ProductRepository(SimpleNamespace(get_collection=lambda name: collection))
    collection.docs.append({"platform": "future_shop", "product_id": "trusted-id", "goods_sign": "SIGN_A"})
    service.convert_link("future_shop", goods_sign="SIGN_A", goods_id="wrong-id", uid="u_1234abcd")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "trusted-id"
    assert service.repo.get_product_detail("SIGN_A", platform="future_shop", key_type="sign") is None
