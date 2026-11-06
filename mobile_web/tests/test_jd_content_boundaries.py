"""JD 商品内容和可信物料摘要的边界回归。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from cache import SEARCH_CACHE
from services.product_service import ProductService


@pytest.mark.parametrize("price", [None, "", "bad", float("nan"), float("inf"), -1, False])
def test_missing_or_invalid_price_is_not_published_as_free(price):
    client = Mock()
    client.search_goods.return_value = {"status": "success", "data": [
        {"tao_id": "opaque", "title": "商品", "quanhou_jiage": price}]}
    adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=client)
    assert adapter.search_goods("测试商品")["status"] == "error"


@pytest.mark.parametrize("price", [0, "0.00", 12.5])
def test_real_known_prices_including_zero_are_preserved(price):
    client = Mock()
    client.search_goods.return_value = {"status": "success", "data": [
        {"tao_id": "opaque", "title": "商品", "quanhou_jiage": price}]}
    adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=client)
    result = adapter.search_goods("测试商品")
    assert result["status"] == "success"
    assert result["data"][0]["price"] == float(price)


def test_disabled_jd_has_no_authorization_entry():
    adapter = JdAdapter(SimpleNamespace(jd_enabled=False))
    assert adapter.check_authority("u_1234abcd")["error_code"] == "platform_unavailable"
    assert adapter.generate_authority_url("u_1234abcd")["status"] == "error"


@pytest.mark.parametrize("prefetch", [False, True])
def test_search_material_urls_saved_even_without_detail_prefetch(monkeypatch, prefetch):
    SEARCH_CACHE.clear()
    item = {"platform": "jd", "goods_id": "opaque", "name": "商品", "price": 12,
            "product_url": "https://jingfen.jd.com/detail/material.html"}
    adapter = SimpleNamespace(active=True, requires_material_url=True,
                              search_goods=Mock(return_value={"status": "success", "data": [item]}))
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: adapter)
    repo = Mock()
    service = ProductService(repo)
    if prefetch:
        service.prefetcher = Mock()
    try:
        assert service.search_home_goods("测试商品", platform="jd")["status"] == "success"
    finally:
        service.wait_for_background_writes()
        SEARCH_CACHE.clear()
    repo.upsert_home_products.assert_called_once_with([item], channel_type=None)
    repo.upsert_product_detail.assert_not_called()
