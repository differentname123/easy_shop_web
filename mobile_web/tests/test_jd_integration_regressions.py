"""复核发现的真实边界：写入背压、来源精度和显示时区。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from cache import MATERIAL_CACHE, SEARCH_CACHE
from models.product import ProductRef
from repositories.order_repo import OrderRepository
from services.product_service import ProductService
from services.promote_service import PromoteService
from tests.test_order_sync_protocol import MemoryCollection, repository


def test_material_mapping_available_immediately_and_on_search_hit_despite_write_backpressure(monkeypatch):
    item = {"platform": "jd", "goods_id": "opaque-no-detail-url", "name": "商品", "price": 10,
            "product_url": "https://jingfen.jd.com/detail/verified.html"}
    adapter = SimpleNamespace(active=True, requires_material_url=True,
                              search_goods=Mock(return_value={"status": "success", "data": [item]}))
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: adapter)
    repo = Mock()
    repo.get_product_detail.return_value = None
    product = ProductService(repo)
    promotion = PromoteService(repo)
    ref = ProductRef("jd", item["goods_id"])
    assert product._summary_write_slots.acquire(False)
    assert product._summary_write_slots.acquire(False)
    try:
        assert product.search_home_goods("测试商品", platform="jd")["status"] == "success"
        repo.upsert_home_products.assert_not_called()
        assert promotion._trusted_material_url(ref) == item["product_url"]
        repo.get_product_detail.assert_not_called()
    finally:
        product._summary_write_slots.release()
        product._summary_write_slots.release()
        product.wait_for_background_writes()
    # 搜索缓存命中也恢复可信映射，不必依赖可关闭的预取和再次请求供应商。
    MATERIAL_CACHE.clear()
    assert SEARCH_CACHE.size() == 1
    product.search_home_goods("测试商品", platform="jd")
    assert promotion._trusted_material_url(ref) == item["product_url"]
    adapter.search_goods.assert_called_once()


@pytest.mark.parametrize("newer,older", [
    (1793926800900, 1793926800100),
    (1793926800.9, 1793926800.1),
    ("2026-11-06 09:00:00.900", "2026-11-06 09:00:00.100"),
])
def test_fractional_modify_clock_cannot_allow_old_state_to_roll_back(newer, older):
    adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=Mock())
    base = {"id": "line", "orderId": "parent", "positionId": "123",
            "orderTime": "2026-11-06 08:00:00", "skuId": "1234", "skuName": "商品",
            "estimateFee": 2, "actualFee": 1.5}
    latest = adapter.normalize_order(dict(base, validCode=18, modifyTime=newer))
    prior = adapter.normalize_order(dict(base, validCode=16, modifyTime=older))
    assert latest["order_modify_at"] > prior["order_modify_at"]
    assert latest["source_updated_at"] == latest["order_modify_at"]
    col = MemoryCollection()
    repo = repository(col)
    repo.upsert_orders([latest], "jd")
    repo.upsert_orders([prior], "jd")
    assert col.docs[0]["is_settled"] is True
    assert col.docs[0]["promotion_amount_yuan"] == 1.5
    assert col.docs[0]["order_modify_at"] == latest["order_modify_at"]


@pytest.mark.parametrize("source", ["2026-11-06 08:00:00", "2026-11-06 00:05:00"])
def test_jd_display_time_roundtrips_shanghai_not_timezone_less_utc(source):
    adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=Mock())
    doc = adapter.normalize_order({"id": "row", "validCode": 16, "orderTime": source})
    row = OrderRepository()._format_order(doc)
    assert row["order_time_str"] == source[:16]
    assert row["order_time_basis"] == "created" and row["order_pay_time_str"] == ""
