# -*- coding: utf-8 -*-
"""商品边界回归：测试替身模拟平台和 Mongo，不访问真实联盟或数据库。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

import adapters
from adapters.base import BasePlatformAdapter
from adapters.pdd_adapter import PddAdapter
from cache import DETAIL_CACHE
from models.product import ProductRef
from services.product_service import ProductService


@pytest.fixture
def catalog():
    repo = MagicMock()
    repo.get_product_detail.return_value = None
    repo.upsert_product_detail.return_value = True
    adapter = MagicMock(spec=PddAdapter)
    adapter.active = True
    adapter.resolve_product_ref.side_effect = PddAdapter().resolve_product_ref
    service = ProductService(product_repo=repo)
    with patch("services.product_service.get_adapter", return_value=adapter):
        yield service, repo, adapter


def detail(goods_id="123", sign="SIGN_A", **kwargs):
    return {"status": "success", "data": {
        "platform": "pdd", "goods_id": goods_id, "goods_sign": sign, "name": "商品",
        "price": 10.0, "carousel_images": ["https://img.test/a", "https://img.test/b"], **kwargs,
    }}


def test_product_ref_namespaces_platform_identity_type_and_user():
    assert ProductRef("tb", "0", "SIGN").platform == "taobao"
    assert ProductRef("pdd", "0", "SIGN").goods_id == ""
    assert ProductRef("pdd", "None", "SIGN").goods_id == ""
    assert ProductRef("pdd", "123").cache_keys() != ProductRef("pdd", "", "123").cache_keys()
    assert ProductRef("pdd", "123").cache_keys() != ProductRef("other", "123").cache_keys()
    assert ProductRef("pdd", "123").cache_keys("u_a") != ProductRef("pdd", "123").cache_keys("u_b")


def test_detail_ignores_legacy_bare_cache_keys(catalog):
    service, repo, adapter = catalog
    DETAIL_CACHE.set("123", detail(name="错误平台"))
    DETAIL_CACHE.set("pdd_123", detail(name="旧裸键"))
    adapter.get_goods_detail.return_value = detail()
    result = service.get_goods_detail(goods_id="123")
    assert result["data"]["name"] == "商品"
    adapter.get_goods_detail.assert_called_once()
    repo.get_product_detail.assert_called_once_with("123", platform="pdd", key_type="id")


def test_unverified_request_alias_cannot_poison_another_id(catalog):
    service, repo, adapter = catalog
    adapter.get_goods_detail.side_effect = [detail("111", "SIGN_A"), detail("222", "SIGN_B")]
    first = service.get_goods_detail(goods_sign="SIGN_A", goods_id="222")
    second = service.get_goods_detail(goods_id="222")
    assert first["data"]["goods_id"] == "111"
    assert second["data"]["goods_id"] == "222"
    assert adapter.get_goods_detail.call_count == 2
    repo.get_product_detail.assert_any_call("SIGN_A", platform="pdd", key_type="sign")


def test_verified_response_aliases_share_cache(catalog):
    service, _, adapter = catalog
    adapter.get_goods_detail.return_value = detail()
    service.get_goods_detail(goods_sign="SIGN_A")
    assert service.get_goods_detail(goods_id="123")["data"]["goods_sign"] == "SIGN_A"
    assert adapter.get_goods_detail.call_count == 1
    assert DETAIL_CACHE.get("123") is None


def test_detail_cache_isolated_per_platform(catalog):
    service, _, adapter = catalog
    adapter.get_goods_detail.side_effect = [detail(), detail(platform="other", name="另一平台")]
    service.get_goods_detail(platform="pdd", goods_id="123")
    adapter.resolve_product_ref.side_effect = lambda raw, gid, sign: ProductRef("other", gid or raw, sign)
    result = service.get_goods_detail(platform="other", goods_id="123")
    assert result["data"]["name"] == "另一平台"
    assert adapter.get_goods_detail.call_count == 2


@pytest.mark.parametrize("updated", [datetime.now(timezone.utc), datetime.now(timezone.utc).replace(tzinfo=None)])
def test_fresh_database_detail_uses_independent_timestamp(catalog, updated):
    service, repo, adapter = catalog
    item = detail()["data"]
    item["detail_updated_at"] = updated
    repo.get_product_detail.return_value = item
    assert service.get_goods_detail(goods_id="123")["source"] == "database"
    adapter.get_goods_detail.assert_not_called()


@pytest.mark.parametrize("timestamp", [None, datetime.now() - timedelta(days=2), "bad-date"])
def test_multiphoto_or_recent_summary_does_not_make_old_detail_fresh(catalog, timestamp):
    service, repo, adapter = catalog
    item = detail(price=90.0)["data"]
    item.update(detail_updated_at=timestamp, updated_at=datetime.now())
    repo.get_product_detail.return_value = item
    adapter.get_goods_detail.return_value = detail(price=12.0)
    assert service.get_goods_detail(goods_id="123")["data"]["price"] == 12.0
    repo.upsert_product_detail.assert_called_once()


def test_database_fallback_does_not_hide_upstream_recovery(catalog):
    service, repo, adapter = catalog
    repo.get_product_detail.return_value = detail()["data"]
    adapter.get_goods_detail.side_effect = [{"status": "error"}, detail(price=9.0)]
    assert service.get_goods_detail(goods_id="123")["source"] == "database_fallback"
    assert service.get_goods_detail(goods_id="123")["data"]["price"] == 9.0
    assert adapter.get_goods_detail.call_count == 2


def test_empty_detail_identity_never_reaches_db_or_sdk(catalog):
    service, repo, adapter = catalog
    assert service.get_goods_detail(goods_id="0")["status"] == "error"
    repo.get_product_detail.assert_not_called()
    adapter.get_goods_detail.assert_not_called()


def test_invalid_provider_platform_is_not_persisted(catalog):
    service, repo, adapter = catalog
    adapter.get_goods_detail.return_value = detail(platform="other")
    assert service.get_goods_detail(goods_id="123")["error_code"] == "product_identity_mismatch"
    repo.upsert_product_detail.assert_not_called()


def test_catalog_cache_does_not_share_uid_specific_results(catalog):
    service, _, adapter = catalog
    adapter.get_recommend_goods.return_value = {"status": "success", "data": []}
    adapter.search_goods.return_value = {"status": "success", "data": []}
    for uid in ("u_a", "u_b", "u_a"):
        service.get_home_recommendations(uid=uid)
        service.search_home_goods("ABC", uid=uid)
    assert adapter.get_recommend_goods.call_count == 2
    assert adapter.search_goods.call_count == 2
    assert adapter.search_goods.call_args.kwargs["keyword"] == "ABC"


def test_pdd_missing_id_and_unknown_commercial_fields_are_not_fabricated():
    adapter = PddAdapter()
    result = adapter._normalize_goods({
        "goods_id": 0, "goods_sign": "SIGN_A", "goods_name": "测试",
        "min_group_price_yuan": 10.0, "promotion_rate": 0, "_raw_data": {"goods_name": "测试"},
    })
    assert result["goods_id"] == ""
    assert result["commission_amount"] == 0
    assert result["coupon_info"] == {}
    assert result["service_guarantees"] == []
    assert result["shop_ratings"] == {}
    assert "brand" not in result
    assert "category" not in result


def test_pdd_keeps_long_numeric_ids_and_explicit_numeric_signs():
    adapter = PddAdapter()
    assert adapter.resolve_product_ref("12345678901234567890").goods_id == "12345678901234567890"
    assert adapter.resolve_product_ref(goods_sign="123").goods_sign == "123"


def test_pdd_search_page_size_contract():
    with patch("adapters.pdd_adapter.call_pdd_api", return_value={
        "goods_search_response": {"goods_list": [{"goods_id": 1, "goods_name": "商品"}], "total_count": 3}
    }) as call:
        result = PddAdapter().search_goods("商品", page=1, page_size=1)
    assert call.call_args.args[-1]["page_size"] == 1
    assert result["page_size"] == 1
    assert len(result["data"]) == 1
    assert result["has_more"] is True


def test_pdd_recommendation_cap_lives_in_adapter():
    with patch("adapters.pdd_adapter.call_pdd_api") as call:
        result = PddAdapter().get_recommend_goods(offset=400)
    call.assert_not_called()
    assert result["has_more"] is False


def test_pdd_recommend_pagination_uses_total_when_page_filters_one_item():
    raw_19 = [{"goods_id": i, "goods_sign": f"SIGN_{i}", "goods_name": f"商品{i}"} for i in range(1, 20)]
    with patch("adapters.pdd_adapter.call_pdd_api", return_value={
        "goods_basic_detail_response": {"list": raw_19, "total": 422}
    }):
        res = PddAdapter().get_recommend_goods(channel_type=1, offset=0, limit=20)
    assert res["status"] == "success"
    assert len(res["data"]) == 19
    assert res["has_more"] is True
    assert res["next_offset"] == 20



def test_pdd_order_failure_stays_failure():
    with patch("adapters.pdd_adapter.query_increment_orders", return_value={"status": "error", "error_msg": "API失败"}):
        result = PddAdapter().fetch_increment_orders(1, 2)
    assert result["status"] == "error"
    assert result["error_msg"] == "API失败"


def test_pdd_order_explicit_flags_override_legacy_status():
    item = PddAdapter().normalize_order({"order_status": 4, "is_refund": False})
    assert item["is_refund"] is False
    assert PddAdapter().normalize_order({"order_status": 4})["is_refund"] is True


def test_disabled_platforms_cannot_generate_fake_links_or_authorization():
    assert adapters.get_adapter("tb") is adapters.get_adapter("taobao")
    for code in ("taobao", "jd"):
        adapter = adapters.get_adapter(code)
        assert adapter.convert_promotion_url("123")["status"] == "error"
        assert adapter.generate_authority_url("u_12345678")["status"] == "error"
    metadata = {item["code"]: item for item in adapters.get_all_platforms()}
    assert metadata["pdd"]["active"] is True
    assert metadata["taobao"]["active"] is False
    assert metadata["jd"]["active"] is False


def test_public_authorization_required_comes_from_adapter_declaration():
    by_code = {item["code"]: item for item in adapters.get_all_platforms()}
    assert set(by_code) >= {"pdd", "taobao", "jd"}
    assert by_code["pdd"]["authorization_required"] is True
    assert by_code["taobao"]["authorization_required"] is True
    assert by_code["jd"]["authorization_required"] is False
    for code, item in by_code.items():
        real_adapter = adapters.get_adapter(code)
        assert item["authorization_required"] is bool(real_adapter.requires_authorized_promotion)


def test_registered_test_platform_uses_existing_core_flow_and_routes(monkeypatch):
    class FixtureAdapter(BasePlatformAdapter):
        platform_code = "fixture"
        platform_name = "测试替身"
        channels = ({"channel_type": 42, "name": "测试频道"},)
        def get_recommend_goods(self, **kwargs):
            return {"status": "success", "data": [{"platform": "fixture", "goods_id": "x-1"}]}
        def search_goods(self, **kwargs): return {"status": "success", "data": []}
        def get_goods_detail(self, **kwargs): return {"status": "success", "data": {}}
        def convert_promotion_url(self, **kwargs): return {"status": "error"}
        def check_authority(self, **kwargs): return {"status": "error"}
        def generate_authority_url(self, **kwargs): return {"status": "error"}
        def fetch_increment_orders(self, **kwargs): return {"status": "error"}
    monkeypatch.setitem(adapters._ADAPTER_REGISTRY, "fixture", FixtureAdapter())
    from api import home
    monkeypatch.setattr(home.product_service, "repo", MagicMock())
    from main import create_app
    client = TestClient(create_app())
    try:
        assert client.get("/api/home/channels?platform=fixture").json()["data"][0]["channel_type"] == 42
        result = client.get("/api/home/recommend?platform=fixture&channel_type=42&offset=450").json()
        assert result["data"][0]["goods_id"] == "x-1"
        assert client.get("/api/home/channels?platform=unknown").status_code == 400
        assert client.get("/api/home/channels?platform=jd").status_code == 400
    finally:
        home.product_service.wait_for_background_writes()


def test_category_cache_and_long_keyword_search_routes():
    from cache import CATEGORY_CACHE
    from main import app
    from models.product import extract_search_keyword

    CATEGORY_CACHE.clear()
    mock_repo = MagicMock()
    mock_repo.get_categories.return_value = ["食品", "数码"]
    service = ProductService(product_repo=mock_repo)
    assert service.get_boom_categories(platform="pdd") == ["食品", "数码"]
    assert service.get_boom_categories(platform="pdd") == ["食品", "数码"]
    assert mock_repo.get_categories.call_count == 1

    share_text = "67￥ HU293 2026abcd ￥ https://m.tb.cn/h.xxx CZ0001 【淘宝】农夫山泉饮用天然水550ml*24瓶整箱装家庭囤货实惠装大容量"
    cleaned = extract_search_keyword(share_text)
    assert "https://" not in cleaned and "HU293" not in cleaned and "CZ0001" not in cleaned
    assert "农夫山泉饮用天然水" in cleaned and len(cleaned) <= 60

    with patch("services.product_service.ProductService.search_home_goods", return_value={"status": "success", "data": [], "has_more": False}):
        assert TestClient(app).get("/api/home/search", params={"keyword": share_text, "platform": "taobao"}).status_code == 200


def test_query_token_is_redacted_from_request_log():
    from starlette.requests import Request
    from main import _request_path
    request = Request({"type": "http", "method": "GET", "scheme": "http", "path": "/api/user/info",
                       "query_string": b"token=private-token&keyword=test", "headers": []})
    path = _request_path(request)
    assert "private-token" not in path
    assert "[REDACTED]" in path
    assert "keyword=test" in path



