"""真实TOP响应形状回归；所有数据脱敏且测试仍禁止网络和Mongo。"""
from unittest.mock import Mock, MagicMock

import pytest

from adapters.tb_adapter import TaobaoAdapter
from infrastructure.tb_client import clean_tb_goods_item
from services.product_service import ProductService


def item(sign, echoed=None, official_url=None):
    raw = {
        "item_id": sign,
        "item_basic_info": {
            "title": "测试商品", "seller_id": "seller-verified", "pict_url": "https://example.com/product.jpg",
            "item_url": official_url or "",
        },
        "price_promotion_info": {"zk_final_price": "12.50"},
        "publish_info": {"click_url": "https://s.click.taobao.com/promotion"},
    }
    if echoed is not None:
        raw["input_item_iid"] = echoed
    return clean_tb_goods_item(raw)


def adapter():
    official = Mock()
    official.get_goods_details.return_value = {
        "status": "success", "data": [item("new-detail-id", "search-input-id", "https://uland.taobao.com/item/edetail?id=new-detail-id")],
    }
    official.search_goods.return_value = {"status": "success", "data": [item("new-promotion-search-id")]}
    official.create_tpwd.return_value = {"status": "success", "password_simple": "￥OFFLINE_TOKEN￥"}
    return TaobaoAdapter(taobao_enabled=True, official_client=official, hdk_client=Mock(), ztk_client=Mock()), official


def test_detail_accepts_official_input_echo_and_preserves_request_identity():
    service, official = adapter()
    result = service.get_goods_detail(goods_sign="search-input-id")
    assert result["status"] == "success"
    assert result["data"]["goods_sign"] == "search-input-id"
    assert result["data"]["goods_id"] == "search-input-id"
    assert result["data"]["product_url"] == "https://uland.taobao.com/item/edetail?id=new-detail-id"
    official.get_goods_details.assert_called_once_with(["search-input-id"])


def test_search_detail_and_precise_promotion_keep_sign_primary_across_real_encryption_shape():
    platform, official = adapter()
    official.search_goods.return_value = {"status": "success", "data": [item("search-input-id")]}
    search = platform.search_goods("测试商品")
    card = search["data"][0]
    detail = platform.get_goods_detail(goods_sign=card["goods_sign"])
    assert card["goods_id"] == detail["data"]["goods_id"] == "search-input-id"
    assert card["goods_sign"] == detail["data"]["goods_sign"] == "search-input-id"
    official.search_goods.return_value = {"status": "success", "data": [item("new-promotion-search-id")]}
    promotion = platform.convert_promotion_url(card["goods_sign"])
    assert promotion["status"] == "success"
    assert promotion["goods_sign"] == card["goods_sign"] and promotion["goods_id"] == "search-input-id"
    assert official.search_goods.call_args.args[0] == detail["data"]["product_url"]


def test_reencrypted_detail_still_hits_shared_detail_cache(monkeypatch):
    platform, official = adapter()
    repo = MagicMock()
    repo.get_product_detail.return_value = None
    monkeypatch.setattr("services.product_service.get_adapter", lambda code: platform)
    products = ProductService(repo)
    first = products.get_goods_detail(platform="taobao", goods_sign="search-input-id")
    second = products.get_goods_detail(platform="taobao", goods_sign="search-input-id")
    assert first == second and first["status"] == "success"
    official.get_goods_details.assert_called_once()


def test_verified_sign_does_not_turn_unverified_auxiliary_id_into_an_alias():
    platform, official = adapter()
    result = platform.get_goods_detail(goods_sign="search-input-id", goods_id="client-unverified-id")
    assert result["status"] == "success"
    assert result["data"]["goods_sign"] == "search-input-id" and result["data"]["goods_id"] == "search-input-id"
    official.get_goods_details.assert_called_once_with(["search-input-id"])


def test_precise_official_url_query_can_return_a_freshly_encrypted_id():
    platform, official = adapter()
    result = platform.convert_promotion_url("search-input-id", goods_sign="search-input-id")
    assert result["status"] == "success" and result["promotion_token"] == "￥OFFLINE_TOKEN￥"
    assert result["goods_sign"] == "search-input-id"
    assert official.search_goods.call_args.args[0] == "https://uland.taobao.com/item/edetail?id=new-detail-id"
    assert not result["cacheable"]


@pytest.mark.parametrize("field,replacement", [("seller_id", "other-seller"), ("goods_name", "不同商品"),
                                               ("goods_image_url", "https://example.com/other.jpg")])
def test_fresh_encryption_does_not_allow_an_unrelated_product(field, replacement):
    platform, official = adapter()
    official.search_goods.return_value["data"][0][field] = replacement
    assert platform.convert_promotion_url("search-input-id")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"
    official.create_tpwd.assert_not_called()


def test_ambiguous_exact_query_does_not_select_the_first_result():
    platform, official = adapter()
    official.search_goods.return_value["data"].append(item("another-fresh-id"))
    assert platform.convert_promotion_url("search-input-id")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"
    official.create_tpwd.assert_not_called()


@pytest.mark.parametrize("source_url", [None, "https://taobao.com.attacker.test/item?id=x", "https://attacker.test/item?id=x"])
def test_only_an_official_detail_url_can_bridge_reencrypted_ids(source_url):
    platform, official = adapter()
    official.get_goods_details.return_value["data"][0]["raw_item"]["item_basic_info"]["item_url"] = source_url
    assert platform.convert_promotion_url("search-input-id")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"
    official.create_tpwd.assert_not_called()
