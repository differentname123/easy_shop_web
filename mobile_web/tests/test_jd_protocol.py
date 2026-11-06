"""Authoritative non-debug JD protocol, HTTP-only offline substitutes (multi-tier official + fallback)."""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from cache import DETAIL_CACHE, clear_all_caches
from infrastructure.jd_client import (
    BIGFIELD_ROOT, DEFAULT_API_BASE, GOODS_QUERY_ROOT, JD_ROUTER_URL, JdClient,
    ORDER_METHOD, ORDER_ROOT, PROMOTION_ROOT, RANK_METHOD, RANK_ROOT, SHANGHAI,
    canonical_promoter_id, parse_order_response, product_url, sign_jd_request,
)
from models.product import DetailSnapshot, ProductRef
from services.product_service import ProductService


SIGNED = "https://api.jd.com/routerjson?method=" + ORDER_METHOD + "&sign=private-signature"


def reply(body, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value=body), raise_for_status=Mock())


def http_for(*bodies):
    return SimpleNamespace(get=Mock(side_effect=[reply(body) for body in bodies]))


def client(http, app_key="test-jd-key", app_secret="test-jd-secret"):
    return JdClient("test-ztk-key", "10001", app_key, app_secret, http_session=http)


def adapter(client_mock=None, **settings):
    return JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_position_id="77", **settings),
                     client=client_mock if client_mock is not None else Mock())


def item(item_id="opaque-material", **overrides):
    return {"tao_id": item_id, "title": "京东测试商品", "quanhou_jiage": "0", "size": "20.50",
            "tkrate3": "0", "tkfee3": "0", "coupon_info_money": "0",
            "pict_url": "https://img.test/product.jpg", "shop_title": "测试自营店",
            "item_url": "https://jingfen.jd.com/detail/verified.html", **overrides}


def official_goods_item(item_id="opaque-material", **overrides):
    base = {
        "itemId": item_id,
        "skuName": "京东官方测试商品",
        "priceInfo": {"price": 20.5, "lowestPrice": 20.5, "lowestCouponPrice": 15.5},
        "commissionInfo": {"commission": 1.55, "commissionShare": 10.0, "couponCommission": 1.55},
        "couponInfo": {"couponList": [{"discount": 5.0, "isBest": 1}]},
        "imageInfo": {
            "whiteImage": "https://img.test/white.jpg",
            "imageList": [{"url": "https://img.test/1.jpg"}, {"url": "https://img.test/2.jpg"}],
        },
        "shopInfo": {"shopName": "京东官方自营旗舰店"},
        "materialUrl": "jingfen.jd.com/detail/verified.html",
        "inOrderCount30Days": 120,
    }
    base.update(overrides)
    return base


def wrapped(root, value, field="queryResult", string=False):
    return {root: {field: json.dumps(value) if string else value}}


def order_body(rows=None, has_more=False, string=False):
    return wrapped(ORDER_ROOT, {"code": 200, "data": [] if rows is None else rows, "hasMore": has_more}, string=string)


def test_disabled_and_empty_optional_configuration_is_safe_and_never_calls_http():
    a = JdAdapter(settings=SimpleNamespace())
    assert a.active is False and a.default_promoter_id == ""
    assert not any(a.capabilities.values())
    for result in (a.get_recommend_goods(), a.search_goods("商品"), a.get_goods_detail("sku"),
                   a.convert_promotion_url("sku"), a.fetch_increment_orders(0, 100)):
        assert result["error_code"] == "platform_unavailable"
    a = JdAdapter(settings=SimpleNamespace(jd_enabled=True, ztk_appkey="key", jd_union_id="100"))
    assert a.active and a.capabilities["search"] and a.capabilities["detail"]
    assert not a.capabilities["promotion"] and not a.capabilities["orders"]
    a = JdAdapter(settings=SimpleNamespace(jd_enabled=True, ztk_appkey="key", jd_union_id="100", jd_position_id="77"))
    assert a.capabilities["promotion"] and not a.capabilities["orders"]
    assert a.fetch_increment_orders(0, 100)["error_code"] == "CONFIG_ERROR"


@pytest.mark.parametrize("string", [False, True])
@pytest.mark.parametrize("channel,sort_type", [(5, 1), (1, 3)])
def test_rank_primary_direct_router_and_relay_fallback(string, channel, sort_type):
    source = {"itemId": "opaque-rank", "skuName": "榜单商品", "wlprice": 0,
              "purchasePriceInfo": {"price": "29.90"}, "commissionShare": 0,
              "commission": "0", "imageUrl": "https://img.test/rank.jpg"}
    # 1. 一级主通道：配置了 app_key + app_secret 时本地签名直连 JD_ROUTER_URL
    http_direct = http_for(wrapped(RANK_ROOT, {"code": 200, "data": [source]}, string=string))
    result = adapter(client(http_direct)).get_recommend_goods(channel, limit=3, detail_sink=Mock())
    assert result["status"] == "success" and not result["has_more"]
    card = result["data"][0]
    assert card["goods_id"] == "opaque-rank" and card["goods_sign"] == "opaque-rank"
    assert card["price"] == 0 and card["original_price"] == 29.9
    assert card["commission_rate"] == card["commission_amount"] == 0
    assert "product_url" not in card and "carousel_images" not in card
    direct_call = http_direct.get.call_args
    assert direct_call.args == (JD_ROUTER_URL,)
    assert direct_call.kwargs["params"]["method"] == RANK_METHOD
    assert json.loads(direct_call.kwargs["params"]["360buy_param_json"]) == {
        "RankGoodsReq": {"rankId": 200000, "sortType": sort_type, "pageIndex": 1, "pageSize": 3}
    }
    assert direct_call.kwargs["params"]["sign"] == sign_jd_request(direct_call.kwargs["params"], "test-jd-secret")
    assert direct_call.kwargs["allow_redirects"] is False and direct_call.kwargs["timeout"] == (3.0, 12.0)

    # 2. 二级兜底通道：当直连网关返回错误时，自动回退至折淘客中继 open_jd_union_open_goods_rank_query.ashx
    http_fallback = http_for(
        {"error_response": {"code": 2000}},
        wrapped(RANK_ROOT, {"code": 200, "data": [source]}, string=string),
    )
    fallback_res = adapter(client(http_fallback)).get_recommend_goods(channel, limit=3)
    assert fallback_res["status"] == "success"
    relay_call = http_fallback.get.call_args_list[1]
    assert relay_call.args == (DEFAULT_API_BASE + "/open_jd_union_open_goods_rank_query.ashx",)
    assert relay_call.kwargs["params"] == {"appkey": "test-ztk-key", "unionId": "10001",
                                           "sortType": sort_type, "rankId": 200000, "pageIndex": 1, "pageSize": 3}


def test_rank_non_aligned_offset_spans_pages_without_losing_order_or_publishing_details():
    c = Mock()
    c.get_rank_goods.side_effect = [
        {"status": "success", "data": [item(str(i)) for i in range(4, 7)], "has_more": True},
        {"status": "success", "data": [item(str(i)) for i in range(7, 10)], "has_more": True},
    ]
    sink = Mock()
    result = adapter(c).get_recommend_goods(offset=5, limit=3, detail_sink=sink)
    assert [g["goods_id"] for g in result["data"]] == ["6", "7", "8"]
    assert result["next_offset"] == 8 and result["has_more"]
    assert [call.kwargs["page"] for call in c.get_rank_goods.call_args_list] == [2, 3]
    sink.assert_not_called()
    assert adapter(c).get_recommend_goods(channel_type=6)["status"] == "error"


def test_failed_second_rank_page_does_not_return_partial_success():
    c = Mock()
    c.get_rank_goods.side_effect = [
        {"status": "success", "data": [item(str(i)) for i in range(4, 7)], "has_more": True},
        {"status": "error", "error_code": "NETWORK_ERROR"},
    ]
    result = adapter(c).get_recommend_goods(offset=5, limit=3)
    assert result["status"] == "error" and result["data"] == [] and "next_offset" not in result


def test_search_primary_official_goods_query_and_quanwang_fallback():
    # 1. 一级主通道：优先走 open_jing_union_open_goods_query.ashx (无需短词补齐，自带轮播图并直接生成 DetailSnapshot)
    http_primary = http_for(
        wrapped(GOODS_QUERY_ROOT, {"code": 200, "data": [official_goods_item("official-1")]}, field="result", string=True)
    )
    a_primary = adapter(client(http_primary))
    snapshots = []
    primary_res = a_primary.search_goods("纸", page=2, page_size=3, detail_sink=snapshots.append)
    assert primary_res["status"] == "success"
    card = primary_res["data"][0]
    assert card["goods_id"] == "official-1"
    assert card["price"] == 15.5 and card["original_price"] == 20.5
    assert card["coupon_amount"] == 5.0 and card["has_coupon"] is True
    assert card["commission_rate"] == 10.0 and card["commission_amount"] == 1.55
    assert card["mall_name"] == "京东官方自营旗舰店"
    assert card["product_url"] == "https://jingfen.jd.com/detail/verified.html"
    assert len(snapshots) == 1 and isinstance(snapshots[0], DetailSnapshot)
    assert snapshots[0].data["carousel_images"] == ["https://img.test/1.jpg", "https://img.test/2.jpg"]
    call_primary = http_primary.get.call_args
    assert call_primary.args == (DEFAULT_API_BASE + "/open_jing_union_open_goods_query.ashx",)
    assert call_primary.kwargs["params"] == {
        "appkey": "test-ztk-key", "unionId": "10001", "keyword": "纸", "pageIndex": 2, "pageSize": 3
    }

    # 2. 二级兜底通道：当官方主通道失败或为空时，自动回退至 api_quanwang.ashx (短词自动补齐至 4 字符)
    http_fallback = http_for(
        {"status": 400},
        {"status": 200, "content": [item()]},
        {"status": 400},
        {"status": 200, "content": [item("second", quanhou_jiage=None)]},
    )
    a = adapter(client(http_fallback))
    first = a.search_goods("纸", page=2, page_size=3)
    assert first["data"][0]["price"] == 0
    assert first["data"][0]["product_url"] == "https://jingfen.jd.com/detail/verified.html"
    assert "raw" not in str(first) and "jCommand" not in str(first)
    call = http_fallback.get.call_args
    assert call.args == (DEFAULT_API_BASE + "/api_quanwang.ashx",)
    assert call.kwargs["params"] == {"appkey": "test-ztk-key", "unionId": "10001", "q": "纸纸纸纸",
                                     "page": 2, "page_size": 3, "sort": "new", "youquan": 1, "type": 2}
    assert a.search_goods("足够长关键词")["data"][0]["price"] == 20.5
    assert http_fallback.get.call_args.kwargs["params"]["q"] == "足够长关键词"


def test_list_as_detail_snapshots_populate_cache_and_disable_blind_prefetch(monkeypatch):
    clear_all_caches()
    a = adapter()
    assert a.can_prefetch_detail(ProductRef("jd", "opaque-1")) is False
    assert a.can_prefetch_detail() is False

    http = http_for(
        wrapped(GOODS_QUERY_ROOT, {"code": 200, "data": [official_goods_item("jd-snap-1")]}, field="result")
    )
    jd_adapt = adapter(client(http))
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: jd_adapt)
    repo = Mock()
    repo.get_product_detail.return_value = None
    repo.upsert_product_detail.return_value = True
    service = ProductService(product_repo=repo)
    try:
        search_res = service.search_home_goods("可乐", platform="jd")
        assert search_res["status"] == "success"
        assert http.get.call_count == 1
        # 搜索已直接沉淀详情快照到 DETAIL_CACHE，后续点击详情实现 0 外部请求
        detail_res = service.get_goods_detail(platform="jd", goods_id="jd-snap-1")
        assert detail_res["status"] == "success"
        assert detail_res["data"]["carousel_images"] == ["https://img.test/1.jpg", "https://img.test/2.jpg"]
        assert http.get.call_count == 1
    finally:
        service.wait_for_background_writes()
        clear_all_caches()


@pytest.mark.parametrize("images,expected", [("a|b||a", ["a", "b"]), (["a", "b", None, "a"], ["a", "b"]), (None, [])])
def test_detail_identified_opaque_id_carousels_and_script_query(images, expected):
    # 测试当官方详情未命中时，平滑回退至折淘客 api_detail.ashx 并解析轮播图
    http = http_for(
        {"status": 400},
        {"status": 200, "content": [item(small_images=images, coupon_info_money="5")]},
    )
    a = adapter(client(http))
    ref = a.resolve_product_ref("opaque-material")
    assert ref.goods_id == "opaque-material" and ref.goods_sign == "opaque-material"
    result = a.get_goods_detail("opaque-material")
    assert result["data"]["carousel_images"] == expected
    assert result["data"]["coupon_amount"] == 5 and result["data"]["has_coupon"]
    assert result["data"]["product_url"] == "https://jingfen.jd.com/detail/verified.html"
    call = http.get.call_args
    assert call.args == (DEFAULT_API_BASE + "/api_detail.ashx",)
    assert call.kwargs["params"]["tao_id"] == "opaque-material" and call.kwargs["params"]["type"] == 1


def test_detail_primary_official_goods_query_and_bigfield_enrichment():
    # 当官方 goods.query 单品详情只有 1 张图时，自动联动 bigfield.query 补充多张轮播图与 mainSkuId
    single_img_item = official_goods_item(
        "opaque-material",
        imageInfo={"imageList": [{"url": "https://img.test/only1.jpg"}]},
        materialUrl="",
    )
    bigfield_item = {
        "itemId": "opaque-material",
        "mainSkuId": 8476879,
        "imageInfo": {"imageList": [{"url": "https://img.test/bf1.jpg"}, {"url": "https://img.test/bf2.jpg"}]},
    }
    http = http_for(
        wrapped(GOODS_QUERY_ROOT, {"code": 200, "data": [single_img_item]}, field="result"),
        wrapped(BIGFIELD_ROOT, {"code": 200, "data": [bigfield_item]}, field="result"),
    )
    a = adapter(client(http))
    result = a.get_goods_detail("opaque-material")
    assert result["status"] == "success"
    assert result["data"]["carousel_images"] == ["https://img.test/bf1.jpg", "https://img.test/bf2.jpg"]
    assert result["data"]["product_url"] == "https://item.jd.com/8476879.html"
    assert http.get.call_count == 2


@pytest.mark.parametrize("content", [[item("other", item_url="")], [item(), item()], []])
def test_detail_requires_unique_matching_supplier_identity(content):
    http = http_for({"status": 400}, {"status": 200, "content": content})
    assert adapter(client(http)).get_goods_detail("opaque-material")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"


def test_detail_allows_rank_item_opaque_id_mapped_to_supplier_material_url():
    rank_item_id = "Wl4yEImIlQsfIguVL32ZfIguVL32Z4_3QWdizpmh42cklzdJw"
    material_tao_id = "ejQ4e41VT9804VHJskns04VH3HCuxQ_3vbTPlrXEzwgtTJU5x"
    supplier_item_url = f"https://jingfen.jd.com/detail/{material_tao_id}.html"
    http = http_for(
        wrapped(GOODS_QUERY_ROOT, {"code": 200, "data": [official_goods_item(material_tao_id, materialUrl=supplier_item_url)]}, field="result")
    )
    a = adapter(client(http))
    result = a.get_goods_detail(rank_item_id)
    assert result["data"]["goods_id"] == rank_item_id
    assert result["data"]["goods_sign"] == rank_item_id
    assert result["data"]["product_url"] == supplier_item_url


@pytest.mark.parametrize("string", [False, True])
def test_promotion_script_parameters_commands_and_server_identity_only(string):
    http = http_for(wrapped(PROMOTION_ROOT, {"code": 200, "data": {
        "shortURL": "https://u.jd.com/short", "clickURL": "https://union-click.jd.com/long",
        "jCommand": "真实完整京口令", "jShortCommand": "真实短京口令"}}, field="result", string=string))
    a = adapter(client(http))
    result = a.convert_promotion_url("opaque-material", uid="u_test", promoter_id="0088",
                                     trusted_product_url="https://jingfen.jd.com/detail/verified.html")
    assert result["status"] == "success" and result["promoter_id"] == "88"
    assert result["promotion_token"] == "真实短京口令" and result["j_command"] == "真实完整京口令"
    assert result["short_url"] == result["final_link"] == "https://u.jd.com/short"
    assert result["click_url"] == result["url"] == "https://union-click.jd.com/long"
    assert result["goods_id"] == "opaque-material" and result["goods_sign"] == "opaque-material"
    assert result["cacheable"] is False and "expires_at" not in result
    assert result["need_auth"] is False and result["is_fallback"] is False
    assert result["attribution_status"] == "verified"
    assert http.get.call_count == 1
    params = http.get.call_args.kwargs["params"]
    assert params == {"appkey": "test-ztk-key", "unionId": "10001", "chainType": "3",
                      "positionId": "88", "materialId": "https://jingfen.jd.com/detail/verified.html"}


def test_material_url_bridge_uses_supplier_detail_not_request_url_and_never_invents_opaque_sku():
    c = Mock()
    c.get_goods_detail.return_value = {"status": "success", "data": [item(item_url="")]}
    a = adapter(c)
    result = a.convert_promotion_url("opaque-material", promoter_id="88", product_url="https://item.jd.com/999.html",
                                     url="https://item.jd.com/999.html")
    assert result["error_code"] == "MATERIAL_URL_REQUIRED"
    c.convert_promotion_link.assert_not_called()
    # Even numeric itemId alone is not an explicit SKU assertion from the supplier.
    c.get_goods_detail.return_value = {"status": "success", "data": [item("123", item_url="")]}
    assert a.convert_promotion_url("123", promoter_id="88")["error_code"] == "MATERIAL_URL_REQUIRED"
    c.get_goods_detail.return_value = {"status": "success", "data": [item(item_url="", skuId="00123")]}
    c.convert_promotion_link.return_value = {"status": "success", "short_url": "https://u.jd.com/1"}
    assert a.convert_promotion_url("opaque-material", promoter_id="88")["status"] == "success"
    assert c.convert_promotion_link.call_args.args == ("https://item.jd.com/123.html", "88")
    # Real item_url is available in raw detail even though the example's DTO omits it.
    c.get_goods_detail.return_value = {"status": "success", "data": [item()]}
    assert a.convert_promotion_url("opaque-material", promoter_id="88")["status"] == "success"
    assert c.convert_promotion_link.call_args.args[0] == "https://jingfen.jd.com/detail/verified.html"


@pytest.mark.parametrize("promoter", [None, "", "u_12345678", "owner", "0", "１２", True, 1.2, "1" * 21])
def test_bad_or_missing_personal_promoter_never_falls_back(promoter):
    c = Mock()
    result = adapter(c).convert_promotion_url("opaque-material", uid="u_12345678", promoter_id=promoter)
    assert result["error_code"] == "INVALID_PROMOTER_ID" and "need_auth" not in result
    c.get_goods_detail.assert_not_called()
    c.convert_promotion_link.assert_not_called()


def test_url_is_not_identified_goods_and_authorization_is_not_faked():
    c = Mock()
    a = adapter(c)
    assert a.requires_numeric_promoter_id and not a.requires_authorized_promotion
    assert not a.capabilities["authorization"]
    assert a.check_authority("u_test") == a.generate_authority_url("u_test") == {
        "status": "not_required", "authorization_required": False, "is_authorized": False, "bind": 0}
    assert a.convert_promotion_url("https://item.jd.com/999.html", promoter_id="88")["status"] == "error"
    assert a.get_goods_detail("https://item.jd.com/999.html")["status"] == "error"
    c.get_goods_detail.assert_not_called()


@pytest.mark.parametrize("string", [False, True])
@pytest.mark.parametrize("rows", [[{"id": "row1"}], {"orderRowResp": [{"id": "row1"}]}, {"orderRowResp": {"id": "row1"}}])
def test_orders_primary_direct_router_and_two_hop_relay_fallback(string, rows):
    # 1. 一级主通道：本地 MD5 签名直连 JD_ROUTER_URL (不将 jd_app_secret 发送给第三方)
    http_direct = http_for(order_body(rows, "true", string), order_body())
    c_direct = client(http_direct)
    res_direct = c_direct.query_orders(0, 3600, page=1, page_size=50)
    assert res_direct["order_list"] == [{"id": "row1"}] and res_direct["has_more"]
    assert res_direct["next_cursor"] == {"page": 2}
    direct_call = http_direct.get.call_args_list[0]
    assert direct_call.args == (JD_ROUTER_URL,)
    assert direct_call.kwargs["params"]["method"] == ORDER_METHOD
    assert "test-jd-secret" not in str(direct_call.kwargs["params"])
    assert json.loads(direct_call.kwargs["params"]["360buy_param_json"]) == {
        "orderReq": {
            "pageIndex": 1, "pageSize": 50, "type": 3,
            "startTime": "1970-01-01 08:00:00", "endTime": "1970-01-01 09:00:00",
            "fields": "goodsInfo,categoryInfo",
        }
    }
    res_direct_2 = c_direct.query_orders(0, 3600, page=2, cursor=res_direct["next_cursor"])
    assert res_direct_2["order_list"] == [] and res_direct_2["has_more"] is False and res_direct_2["next_cursor"] is None

    # 2. 二级兜底通道：当直连网关失败时，回退至折淘客两跳签名中继
    http_relay = http_for(
        {"error_response": {"code": 500}}, {"status": 200, "content": SIGNED}, order_body(rows, "true", string),
        {"error_response": {"code": 500}}, {"status": 200, "content": SIGNED}, order_body(),
    )
    c = client(http_relay)
    result = c.query_orders(0, 3600, page=1, page_size=50)
    assert result["order_list"] == [{"id": "row1"}] and result["has_more"]
    assert result["next_cursor"] == {"page": 2}
    first_relay = http_relay.get.call_args_list[1]
    assert first_relay.args == (DEFAULT_API_BASE + "/open_jing_union_openz_order_row_query.ashx",)
    assert first_relay.kwargs["params"] == {"appkey": "test-ztk-key", "jd_app_key": "test-jd-key", "jd_app_secret": "test-jd-secret",
                                            "startTime": "1970-01-01 08:00:00", "endTime": "1970-01-01 09:00:00",
                                            "type": "3", "pageIndex": "1", "pageSize": "50", "fields": "goodsInfo,categoryInfo"}
    assert http_relay.get.call_args_list[2].args == (SIGNED,)
    assert all(call.kwargs["allow_redirects"] is False for call in http_relay.get.call_args_list)
    result2 = c.query_orders(0, 3600, page=2, cursor=result["next_cursor"])
    assert result2["order_list"] == [] and result2["has_more"] is False and result2["next_cursor"] is None
    assert http_relay.get.call_args_list[4].kwargs["params"]["pageIndex"] == "2"


@pytest.mark.parametrize("url", [
    "http://api.jd.com/routerjson?method=" + ORDER_METHOD,
    "https://evil.test/routerjson?method=" + ORDER_METHOD,
    "https://api.jd.com.evil.test/routerjson?method=" + ORDER_METHOD,
    "https://u:p@api.jd.com/routerjson?method=" + ORDER_METHOD,
    "https://@api.jd.com/routerjson?method=" + ORDER_METHOD,
    "https://api.jd.com:/routerjson?method=" + ORDER_METHOD,
    "https://api.jd.com:20000/routerjson?method=" + ORDER_METHOD,
    "https://api.jd.com/redirect?method=" + ORDER_METHOD,
    "https://api.jd.com/routerjson?sign=private",
    "https://api.jd.com/routerjson?method=",
    "https://api.jd.com/routerjson?method=jd.union.open.promotion.byunionid.get",
    SIGNED + "&method=" + ORDER_METHOD,
    SIGNED + "&%6dethod=",
    SIGNED + "#fragment",
    SIGNED + "\n",
    "https://api.jd.com\\@evil.test/routerjson?method=" + ORDER_METHOD,
])
def test_order_signed_gateway_exact_allowlist_and_duplicate_method_rejection(url):
    http = http_for({"error_response": {"code": 500}}, {"status": 200, "content": url})
    result = client(http).query_orders(0, 100)
    assert result["error_code"] == "UNTRUSTED_GATEWAY" and http.get.call_count == 2
    assert url not in str(result) and "private" not in str(result)


@pytest.mark.parametrize("body", [
    {}, {ORDER_ROOT: {}}, {ORDER_ROOT: {"queryResult": "not-json"}},
    {ORDER_ROOT: {"queryResult": []}},
    wrapped(ORDER_ROOT, {"code": 200}), wrapped(ORDER_ROOT, {"code": 200, "data": {}}),
    wrapped(ORDER_ROOT, {"code": 200, "data": "unknown"}),
    wrapped(ORDER_ROOT, {"code": 200, "data": ["not-a-row"]}),
    order_body([], "yes"), order_body([], True),
    {"jd_union_open_order_row_query_response": {"queryResult": {"code": 200, "data": []}}},
    {"error_response": {"code": 500, "zh_desc": SIGNED}},
    wrapped(ORDER_ROOT, {"code": 500, "message": SIGNED, "data": []}),
])
def test_unknown_order_shapes_errors_and_invalid_pagination_never_become_empty_success(body):
    result = parse_order_response(body)
    assert result["status"] == "error" and "private" not in str(result)


def test_order_missing_credentials_bad_window_and_cursor_do_not_call_http():
    http = http_for()
    c = client(http)
    for args in ((0, 3601), (100, 0)):
        assert c.query_orders(*args)["error_code"] == "INVALID_ARGUMENT"
    for kwargs in ({"page": 0}, {"page_size": 101}, {"page": 2, "cursor": {"page": 3}}, {"cursor": "bad"}):
        assert c.query_orders(0, 100, **kwargs)["error_code"] == "INVALID_ARGUMENT"
    assert JdClient("key", http_session=http).query_orders(0, 100)["error_code"] == "CONFIG_ERROR"
    http.get.assert_not_called()


@pytest.mark.parametrize("hop", [1, 2, 3])
def test_redirects_denied_and_errors_sanitized_at_all_hops(hop, caplog):
    prefix = []
    if hop == 2:
        prefix = [reply({"error_response": {"code": 500}})]
    elif hop == 3:
        prefix = [reply({"error_response": {"code": 500}}), reply({"status": 200, "content": SIGNED})]
    http = SimpleNamespace(get=Mock(side_effect=prefix + [reply({}, 302)]))
    assert client(http).query_orders(0, 100)["error_code"] == "UNSAFE_REDIRECT"
    http.get.side_effect = prefix + [RuntimeError(SIGNED + "&secret=private-secret")]
    if hop == 1:
        # When hop 1 (direct router) raises RuntimeError, hop 2 (relay) is tried; if relay also fails, error is sanitized.
        http.get.side_effect = [RuntimeError(SIGNED + "&secret=private-secret"), RuntimeError(SIGNED + "&secret=private-secret")]
    result = client(http).query_orders(0, 100)
    assert result["error_code"] == "NETWORK_ERROR" and "private" not in str(result) + caplog.text


@pytest.mark.parametrize("body", [None, [], {}, {"status": 500, "content": SIGNED},
                                         {"status": "error", "error_msg": SIGNED}])
def test_unknown_catalog_and_relay_envelopes_do_not_disclose_messages(body):
    for method in ("get_rank_goods", "get_goods_detail", "search_goods", "query_orders"):
        http = http_for(body, body, body)
        c = client(http)
        args = {"get_rank_goods": (), "get_goods_detail": ("opaque",), "search_goods": ("商品",), "query_orders": (0, 100)}[method]
        result = getattr(c, method)(*args)
        assert result["status"] == "error" and "private" not in str(result)


@pytest.mark.parametrize("status,settled,refund,invalid", [
    (3, False, False, False), (17, False, False, False), (18, True, False, False),
    (27, False, False, False), (28, False, True, False), (24, False, False, True), (999, False, False, False),
])
def test_order_normalizer_row_identity_amount_basis_time_and_flags(status, settled, refund, invalid):
    source = {"id": "000row1", "orderId": "000parent", "positionId": "00088", "subUnionId": "not-a-uid",
              "validCode": status, "skuId": "000123", "skuNum": 2, "skuName": "订单商品",
              "estimateCosPrice": "0", "estimateFee": "1.25", "actualFee": "0", "commissionRate": "10",
              "orderTime": "2026-11-06 08:00:00", "modifyTime": "2026-11-06 09:00:00", "finishTime": "2026-11-06 08:30:00"}
    result = adapter().normalize_order(source)
    assert result["order_sn"] == "000row1" and result["parent_order_sn"] == "000parent"
    assert result["goods_id"] == "000123" and result["goods_quantity"] == 2
    assert result["promoter_id"] == "88" and result["position_id"] == "00088" and result["attribution_identity"] == {}
    assert "promoter_uid" not in result and "uid" not in result
    assert result["order_time_basis"] == "created" and result["order_amount_basis"] == "commissionable"
    assert result["order_amount_yuan"] == 0 and result["order_pay_time"] is None and result["order_pay_time_str"] == ""
    assert result["order_time"] == int(datetime(2026, 11, 6, 8, tzinfo=SHANGHAI).timestamp())
    assert result["source_updated_at"] == result["order_modify_at"] > result["order_time"]
    assert result["estimated_commission_yuan"] == 1.25 and result["settled_commission_yuan"] == 0
    assert result["promotion_amount_yuan"] == (0 if settled else 1.25)
    assert result["is_settled"] is settled and result["is_refund"] is refund and result["is_invalid"] is invalid
    assert result["is_after_sale"] is (status == 27)


def test_order_missing_actual_never_uses_estimate_and_unknown_numeric_evidence_preserved():
    a = adapter()
    result = a.normalize_order({"id": "row", "orderId": "parent", "validCode": 18, "estimateFee": "19.5", "positionId": "opaque"})
    assert result["settled_commission_yuan"] is None and result["promotion_amount_yuan"] == 0
    assert result["commission_basis"] == "missing_settlement" and result["order_time"] is None
    assert result["promoter_id"] == "" and result["position_id"] == "opaque"
    assert a.normalize_order({"id": "row2", "orderId": "parent"})["order_sn"] != result["order_sn"]
    assert a.normalize_order({"id": "row3", "estimateFee": 0, "actualFee": 9})["promotion_amount_yuan"] == 0
    with pytest.raises(ValueError):
        a.normalize_order({"orderId": "parent"})


@pytest.mark.parametrize("value", ["https://evil.test/p", "https://jd.com.evil.test/p", "https://@item.jd.com/1.html",
                                    "https://item.jd.com:8443/p", "javascript:alert(1)", "https://item.jd.com/1.html#x"])
def test_supplier_material_url_requires_official_host_and_safe_components(value):
    assert product_url(value) == ""


def test_client_config_base_and_pagination_errors_are_offline():
    http = http_for()
    for base in ("https://user:pass@relay.test/api", "https://relay.test/api?secret=private", "https://relay.test/unknown", ""):
        assert JdClient("k", "100", api_base=base, http_session=http).search_goods("商品")["error_code"] == "CONFIG_ERROR"
    for args in ((1.5, 20), (True, 20), (1, 2.5), (1, 0)):
        assert client(http).get_rank_goods(page=args[0], page_size=args[1])["error_code"] == "INVALID_ARGUMENT"
    http.get.assert_not_called()
    http = http_for({"status": 400}, {"status": 200, "content": [], "hasMore": True})
    assert client(http).search_goods("商品")["error_code"] == "PAGINATION_ERROR"


def test_numeric_scope_config_fingerprint_and_failure_has_no_owner_fallback():
    a = adapter(jd_union_id="100")
    b = adapter(jd_union_id="101")
    assert a.default_promoter_id == "77" and a.promotion_scope != b.promotion_scope
    assert canonical_promoter_id("00088") == "88" and canonical_promoter_id("１２") == ""
    c = Mock()
    c.convert_promotion_link.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR"}
    result = adapter(c).convert_promotion_url("opaque", uid="u_test", promoter_id="88", trusted_product_url="https://item.jd.com/123.html")
    assert result["status"] == "error" and c.convert_promotion_link.call_count == 1
    assert c.convert_promotion_link.call_args.args[1] == "88"
