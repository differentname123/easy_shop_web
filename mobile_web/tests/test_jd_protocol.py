"""Authoritative non-debug JD protocol, HTTP-only offline substitutes."""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from infrastructure.jd_client import (
    DEFAULT_API_BASE, JdClient, ORDER_METHOD, ORDER_ROOT, PROMOTION_ROOT,
    RANK_ROOT, SHANGHAI, canonical_promoter_id, parse_order_response, product_url,
)


SIGNED = "https://api.jd.com/routerjson?method=" + ORDER_METHOD + "&sign=private-signature"


def reply(body, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value=body), raise_for_status=Mock())


def http_for(*bodies):
    return SimpleNamespace(get=Mock(side_effect=[reply(body) for body in bodies]))


def route_http(routes, fallback=None):
    def _get(url, params=None, timeout=None, allow_redirects=False):
        for suffix, handler in routes.items():
            if suffix in url:
                body = handler(url, params) if callable(handler) else handler
                return reply(body)
        if callable(fallback):
            return reply(fallback(url, params))
        if fallback is not None:
            return reply(fallback)
        raise AssertionError(f"unexpected GET url in route_http: {url}")

    return SimpleNamespace(get=Mock(side_effect=_get))


def client(http):
    return JdClient("test-ztk-key", "10001", "test-jd-key", "test-jd-secret", http_session=http)


def adapter(client_mock=None, **settings):
    return JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_position_id="77", **settings),
                     client=client_mock if client_mock is not None else Mock())


def item(item_id="opaque-material", **overrides):
    return {"tao_id": item_id, "title": "京东测试商品", "quanhou_jiage": "0", "size": "20.50",
            "tkrate3": "0", "tkfee3": "0", "coupon_info_money": "0",
            "pict_url": "https://img.test/product.jpg", "shop_title": "测试自营店",
            "item_url": "https://jingfen.jd.com/detail/verified.html", **overrides}


def wrapped(root, value, field="queryResult", string=False):
    return {root: {field: json.dumps(value) if string else value}}


def order_body(rows=None, has_more=False, string=False):
    return wrapped(ORDER_ROOT, {"code": 200, "data": [] if rows is None else rows, "hasMore": has_more}, string=string)


def test_disabled_and_empty_optional_configuration_is_safe_and_never_calls_http():
    a = JdAdapter(settings=SimpleNamespace())
    assert a.active is False and a.default_promoter_id == ""
    assert not any(a.capabilities.values())
    for result in (a.get_recommend_goods(), a.search_goods("商品"), a.get_goods_detail("sku"),
                   a.convert_promotion_url("sku"), a.check_authority("u_1234abcd"),
                   a.generate_authority_url("u_1234abcd"), a.fetch_increment_orders(0, 100)):
        assert result["error_code"] == "platform_unavailable"
    a = JdAdapter(settings=SimpleNamespace(jd_enabled=True, ztk_appkey="key", jd_union_id="100"))
    assert a.active and a.capabilities["search"] and a.capabilities["detail"]
    assert not a.capabilities["promotion"] and not a.capabilities["orders"]
    a = JdAdapter(settings=SimpleNamespace(jd_enabled=True, ztk_appkey="key", jd_union_id="100", jd_position_id="77"))
    assert a.capabilities["promotion"] and not a.capabilities["orders"]
    assert a.fetch_increment_orders(0, 100)["error_code"] == "CONFIG_ERROR"


@pytest.mark.parametrize("string", [False, True])
@pytest.mark.parametrize("channel,sort_type", [(5, 1), (1, 3)])
def test_rank_script_roots_fields_zero_values_and_channel_mapping(string, channel, sort_type):
    source = {"itemId": "opaque-rank", "skuName": "榜单商品", "wlprice": 0,
              "purchasePriceInfo": {"price": "29.90"}, "commissionShare": 0,
              "commission": "0", "imageUrl": "https://img.test/rank.jpg"}
    http = http_for(wrapped(RANK_ROOT, {"code": 200, "data": [source]}, string=string))
    result = adapter(client(http)).get_recommend_goods(channel, limit=3, detail_sink=Mock())
    assert result["status"] == "success" and not result["has_more"]
    card = result["data"][0]
    assert card["goods_id"] == "opaque-rank" and card["goods_sign"] == "opaque-rank"
    assert card["price"] == 0 and card["original_price"] == 29.9
    assert card["commission_rate"] == card["commission_amount"] == 0
    assert "product_url" not in card and "carousel_images" not in card
    call = http.get.call_args
    assert call.args == (DEFAULT_API_BASE + "/open_jd_union_open_goods_rank_query.ashx",)
    assert call.kwargs["params"] == {"appkey": "test-ztk-key", "unionId": "10001",
                                   "sortType": sort_type, "rankId": 200000, "pageIndex": 1, "pageSize": 3}
    assert call.kwargs["allow_redirects"] is False and call.kwargs["timeout"] == (3.0, 12.0)


def _rank_row(item_id):
    return {
        "itemId": item_id,
        "skuName": f"榜单商品-{item_id}",
        "wlprice": "20.50",
        "imageUrl": "https://img.test/rank.jpg",
    }


def test_rank_non_aligned_offset_spans_pages_without_losing_order_or_publishing_details():
    c = Mock()
    c.get_rank_goods.side_effect = [
        {"status": "success", "data": [_rank_row(f"rank_{i}_encrypted") for i in range(4, 7)], "has_more": True},
        {"status": "success", "data": [_rank_row(f"rank_{i}_encrypted") for i in range(7, 10)], "has_more": True},
    ]
    sink = Mock()
    result = adapter(c).get_recommend_goods(offset=5, limit=3, detail_sink=sink)
    assert [g["goods_id"] for g in result["data"]] == ["rank_6_encrypted", "rank_7_encrypted", "rank_8_encrypted"]
    assert result["next_offset"] == 8 and result["has_more"]
    assert [call.kwargs["page"] for call in c.get_rank_goods.call_args_list] == [2, 3]
    sink.assert_not_called()
    assert adapter(c).get_recommend_goods(channel_type=6)["status"] == "error"


def test_failed_second_rank_page_does_not_return_partial_success():
    c = Mock()
    c.get_rank_goods.side_effect = [
        {"status": "success", "data": [_rank_row(f"rank_{i}_encrypted") for i in range(4, 7)], "has_more": True},
        {"status": "error", "error_code": "NETWORK_ERROR"},
    ]
    result = adapter(c).get_recommend_goods(offset=5, limit=3)
    assert result["status"] == "error" and result["data"] == [] and "next_offset" not in result


def test_search_exact_script_parameters_public_url_and_missing_only_price_fallback():
    limit_union = wrapped("jd_union_open_goods_query_response", {"code": 2000, "message": "limit"}, field="result", string=True)
    quanwang_replies = iter([{"status": 200, "content": [item()]}, {"status": 200, "content": [item("second", quanhou_jiage=None)]}])
    http = route_http({
        "/open_jing_union_open_goods_query.ashx": limit_union,
        "/api_quanwang.ashx": lambda _u, _p: next(quanwang_replies),
    })
    a = adapter(client(http))
    first = a.search_goods("纸", page=2, page_size=3)
    assert first["data"][0]["price"] == 0
    assert first["data"][0]["product_url"] == "https://jingfen.jd.com/detail/verified.html"
    assert "raw" not in str(first) and "jCommand" not in str(first)
    call = http.get.call_args
    assert call.args == (DEFAULT_API_BASE + "/api_quanwang.ashx",)
    assert call.kwargs["params"] == {"appkey": "test-ztk-key", "unionId": "10001", "q": "纸纸",
                                     "page": 2, "page_size": 3, "sort": "new", "youquan": 0, "type": 2}
    assert a.search_goods("足够长关键词")["data"][0]["price"] == 20.5
    assert http.get.call_args.kwargs["params"]["q"] == "足够长关键词"
    assert http.get.call_args.kwargs["params"]["youquan"] == 0
    http_cpn = http_for({"status": 200, "content": [item()]})
    assert client(http_cpn).search_goods("足够长关键词", has_coupon=True)["status"] == "success"
    assert http_cpn.get.call_args.kwargs["params"]["youquan"] == 1


@pytest.mark.parametrize("images,expected", [("a|b||a", ["a", "b"]), (["a", "b", None, "a"], ["a", "b"]), (None, [])])
def test_detail_identified_opaque_id_carousels_and_script_query(images, expected):
    http = http_for({"status": 200, "content": [item(small_images=images, coupon_info_money="5")]})
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


@pytest.mark.parametrize("content", [[item("other", item_url="")], [item(), item()], []])
def test_detail_requires_unique_matching_supplier_identity(content):
    http = http_for({"status": 200, "content": content})
    assert adapter(client(http)).get_goods_detail("opaque-material")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"


def test_detail_allows_rank_item_opaque_id_mapped_to_supplier_material_url():
    # 模拟真实折淘客对于榜单商品的返回：请求参数为榜单混淆ID，返回单品详情的tao_id为不同密钥池物料长ID：
    # 1. 携带同主图 CDN 指纹时 api_detail 可直接跨密钥池放行；或无主图时由 goods_query 凭 oriItemId 确证；
    # 2. 若无主图/同后缀且 goods_query 亦未命中，严禁放行无关物料长ID（防 api_detail 子串误搜串货）。
    from models.product import extract_jd_image_key

    rank_item_id = "Wl4yEImIlQsfIguVL32ZfIguVL32Z4_3QWdizpmh42cklzdJw"
    material_tao_id = "ejQ4e41VT9804VHJskns04VH3HCuxQ_3vbTPlrXEzwgtTJU5x"
    supplier_item_url = f"https://jingfen.jd.com/detail/{material_tao_id}.html"
    detail_img = "https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg"
    detail_row = item(material_tao_id, item_url=supplier_item_url, pict_url=detail_img)
    img_key = extract_jd_image_key(detail_img)
    assert img_key == "6ae3f40fF84b3cfef_0083320320005d73"
    empty_union = wrapped("jd_union_open_goods_query_response", {"code": 200, "data": []}, field="result", string=True)

    http = route_http({
        "/open_jing_union_open_goods_query.ashx": empty_union,
        "/api_detail.ashx": {"status": 200, "content": [detail_row]},
    })
    a = adapter(client(http))
    # 无主图且不同后缀+goods_query空：严格拒收防幻觉
    assert a.get_goods_detail(rank_item_id)["error_code"] == "NOT_FOUND"
    # 同主图 CDN 指纹：跨密钥池安全放行并刷新官方物料链
    result = a.get_goods_detail(rank_item_id, goods_id=img_key, goods_sign=rank_item_id)
    assert result["status"] == "success"
    assert result["data"]["goods_id"] == img_key
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
    assert result["cacheable"] is True and "expires_at" not in result
    assert result["need_auth"] is False and result["is_fallback"] is False
    assert result["attribution_status"] == "verified"
    assert http.get.call_count == 1
    params = http.get.call_args.kwargs["params"]
    assert params == {"appkey": "test-ztk-key", "unionId": "10001", "chainType": "3",
                      "positionId": "88", "materialId": "https://jingfen.jd.com/detail/verified.html"}


def test_material_url_bridge_uses_supplier_detail_not_request_url_and_rejects_numeric_sku():
    encrypted_tao_id = "2JQkZkZ2nGOIkKHE8K75IkKfe6mXtQ_3SD7M7MGcugkXyuI4w"
    c = Mock()
    c.get_goods_detail.return_value = {"status": "success", "data": [item(item_url="")]}
    a = adapter(c)
    result = a.convert_promotion_url("opaque-material", promoter_id="88", product_url="https://item.jd.com/999.html",
                                     url="https://item.jd.com/999.html")
    assert result["error_code"] == "MATERIAL_URL_REQUIRED"
    c.convert_promotion_link.assert_not_called()
    # 纯数字 itemId 不足以构成可信商品链接
    c.get_goods_detail.return_value = {"status": "success", "data": [item("123", item_url="")]}
    assert a.convert_promotion_url("123", promoter_id="88")["error_code"] == "MATERIAL_URL_REQUIRED"
    # 加密 tao_id / itemId 可兜底拼出标准京粉物料链接
    c.get_goods_detail.return_value = {"status": "success", "data": [item(encrypted_tao_id, item_url="")]}
    c.convert_promotion_link.return_value = {"status": "success", "short_url": "https://u.jd.com/1"}
    assert a.convert_promotion_url(encrypted_tao_id, promoter_id="88")["status"] == "success"
    assert c.convert_promotion_link.call_args.args == (f"https://jingfen.jd.com/detail/{encrypted_tao_id}.html", "88")
    # 真实上游对纯数字 item.jd.com/123.html 返回 301 不支持数字 id 转链时精确映射 UNSUPPORTED_JD_NUMERIC_SKU
    numeric_http = http_for({"status": 301, "content": "很抱歉！不支持数字id转链！"})
    assert client(numeric_http).convert_universal_material("https://item.jd.com/123.html", "88")["error_code"] == "UNSUPPORTED_JD_NUMERIC_SKU"
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
def test_orders_script_two_hops_shanghai_type3_pagination_and_shapes(string, rows):
    http = http_for({"status": 200, "content": SIGNED}, order_body(rows, "true", string),
                    {"status": 200, "content": SIGNED}, order_body())
    c = client(http)
    result = c.query_orders(0, 3600, page=1, page_size=50)
    assert result["order_list"] == [{"id": "row1"}] and result["has_more"]
    assert result["next_cursor"] == {"page": 2}
    first = http.get.call_args_list[0]
    assert first.args == (DEFAULT_API_BASE + "/open_jing_union_openz_order_row_query.ashx",)
    assert first.kwargs["params"] == {"appkey": "test-ztk-key", "jd_app_key": "test-jd-key", "jd_app_secret": "test-jd-secret",
                                      "startTime": "1970-01-01 08:00:00", "endTime": "1970-01-01 09:00:00",
                                      "type": "3", "pageIndex": "1", "pageSize": "50", "fields": "goodsInfo,categoryInfo"}
    assert http.get.call_args_list[1].args == (SIGNED,)
    assert all(call.kwargs["allow_redirects"] is False for call in http.get.call_args_list)
    result2 = c.query_orders(0, 3600, page=2, cursor=result["next_cursor"])
    assert result2["order_list"] == [] and result2["has_more"] is False and result2["next_cursor"] is None
    assert http.get.call_args_list[2].kwargs["params"]["pageIndex"] == "2"


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
    http = http_for({"status": 200, "content": url})
    result = client(http).query_orders(0, 100)
    assert result["error_code"] == "UNTRUSTED_GATEWAY" and http.get.call_count == 1
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


@pytest.mark.parametrize("hop", [1, 2])
def test_redirects_denied_and_errors_sanitized_at_both_hops(hop, caplog):
    http = SimpleNamespace(get=Mock(side_effect=([reply({"status": 200, "content": SIGNED})] if hop == 2 else []) + [reply({}, 302)]))
    assert client(http).query_orders(0, 100)["error_code"] == "UNSAFE_REDIRECT"
    http.get.side_effect = ([reply({"status": 200, "content": SIGNED})] if hop == 2 else []) + [RuntimeError(SIGNED + "&secret=private-secret")]
    result = client(http).query_orders(0, 100)
    assert result["error_code"] == "NETWORK_ERROR" and "private" not in str(result) + caplog.text


@pytest.mark.parametrize("body", [None, [], {}, {"status": 500, "content": SIGNED},
                                         {"status": "error", "error_msg": SIGNED}])
def test_unknown_catalog_and_relay_envelopes_do_not_disclose_messages(body):
    for method in ("get_rank_goods", "get_goods_detail", "search_goods", "query_orders"):
        http = http_for(body)
        c = client(http)
        args = {"get_rank_goods": (), "get_goods_detail": ("opaque",), "search_goods": ("京东商品",), "query_orders": (0, 100)}[method]
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


def test_order_normalizer_extracts_goods_info_thumbnail_and_canonical_image_key():
    a = adapter()
    https_row = a.normalize_order({
        "id": "row_https",
        "skuId": "100122053502",
        "validCode": 16,
        "goodsInfo": {"imageUrl": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/66bde2a7Fb47aa096/69ff34903c83a560.jpg"},
    })
    assert https_row["goods_id"] == "100122053502"
    assert https_row["goods_thumbnail_url"] == "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/66bde2a7Fb47aa096/69ff34903c83a560.jpg"
    assert https_row["canonical_goods_id"] == "66bde2a7Fb47aa096_69ff34903c83a560"

    proto_row = a.normalize_order({
        "id": "row_proto",
        "skuId": "10235356702978",
        "validCode": 17,
        "goodsInfo": {"imageUrl": "//img10.360buyimg.com/n1/jfs/t1/9/8/7/6aca4d10F381a5657/0944320320d0cf17.jpg"},
    })
    assert proto_row["goods_thumbnail_url"] == "https://img10.360buyimg.com/n1/jfs/t1/9/8/7/6aca4d10F381a5657/0944320320d0cf17.jpg"
    assert proto_row["canonical_goods_id"] == "6aca4d10F381a5657_0944320320d0cf17"

    missing_or_external = [
        a.normalize_order({"id": "row_no_info", "skuId": "1001", "validCode": 16}),
        a.normalize_order({"id": "row_bad_cdn", "skuId": "1002", "validCode": 16, "goodsInfo": {"imageUrl": "https://evil.example.com/a/b.jpg"}}),
    ]
    assert missing_or_external[0]["goods_thumbnail_url"] == ""
    assert missing_or_external[0]["canonical_goods_id"] == ""
    assert missing_or_external[1]["goods_thumbnail_url"] == "https://evil.example.com/a/b.jpg"
    assert missing_or_external[1]["canonical_goods_id"] == ""


@pytest.mark.parametrize("value", ["https://evil.test/p", "https://jd.com.evil.test/p", "https://@item.jd.com/1.html",
                                    "https://item.jd.com:8443/p", "javascript:alert(1)", "https://item.jd.com/1.html#x"])
def test_supplier_material_url_requires_official_host_and_safe_components(value):
    assert product_url(value) == ""


def test_client_config_base_and_pagination_errors_are_offline():
    http = http_for()
    for base in ("https://user:pass@relay.test/api", "https://relay.test/api?secret=private", "https://relay.test/unknown", ""):
        assert JdClient("k", "100", api_base=base, http_session=http).search_goods("京东商品")["error_code"] == "CONFIG_ERROR"
    for args in ((1.5, 20), (True, 20), (1, 2.5), (1, 0)):
        assert client(http).get_rank_goods(page=args[0], page_size=args[1])["error_code"] == "INVALID_ARGUMENT"
    http.get.assert_not_called()
    http = http_for({"status": 200, "content": [], "hasMore": True})
    assert client(http).search_goods("京东商品")["error_code"] == "PAGINATION_ERROR"


def test_numeric_scope_config_fingerprint_and_failure_has_no_owner_fallback():
    a = adapter(jd_union_id="100")
    assert a.default_promoter_id == "77"
    assert canonical_promoter_id("00088") == "88" and canonical_promoter_id("１２") == ""
    c = Mock()
    c.convert_promotion_link.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR"}
    result = adapter(c).convert_promotion_url("opaque", uid="u_test", promoter_id="88", trusted_product_url="https://item.jd.com/123.html")
    assert result["status"] == "error" and c.convert_promotion_link.call_count == 1
    assert c.convert_promotion_link.call_args.args[1] == "88"


def test_appkey_list_failover_rescues_rate_limited_key_and_updates_active_cursor():
    # api_detail 遇 301 对应宝贝已下架时第 1 把 Key 立即早停不连打；遇显式频控(1000/429)才自动切 Key 2 并冷却 Key 1
    short_301_http = http_for(
        {"status": 301, "content": "对应宝贝已下架"},
        {"status": 200, "content": [item("should-not-call")]},
    )
    c_early = JdClient(["key-1", "key-2"], "10001", "test-jd-key", "test-jd-secret", http_session=short_301_http)
    early_res = c_early.get_goods_detail("opaque-material")
    assert early_res["status"] == "error" and early_res.get("ztk_ambiguous_301") is True
    assert short_301_http.get.call_count == 1
    assert c_early._key_cooldown_until == {}

    http = http_for(
        {"status": 301, "content": "今日调用次数已达1000上限"},
        {"status": 200, "content": [item("opaque-material")]},
        {"status": 200, "content": [item("second-item")]},
        {"status": 200, "content": [item("third-item")]},
    )
    c = JdClient(["key-1", "key-2"], "10001", "test-jd-key", "test-jd-secret", http_session=http)
    assert c.appkey_list == ("key-1", "key-2")

    # 第一次请求（游标 0）：Key 1 返回显式频控 -> 自动切换 Key 2 成功，且 Key 1（索引 0）进入冷却隔离
    res1 = c.get_goods_detail("opaque-material")
    assert res1["status"] == "success"
    assert http.get.call_count == 2
    assert http.get.call_args_list[0].kwargs["params"]["appkey"] == "key-1"
    assert http.get.call_args_list[1].kwargs["params"]["appkey"] == "key-2"
    assert c.appkey == "key-2"
    assert 0 in c._key_cooldown_until

    # 第二次请求（游标 1）：直接走 Key 2 成功
    res2 = c.search_goods("蓝牙耳机")
    assert res2["status"] == "success"
    assert http.get.call_count == 3
    assert http.get.call_args_list[2].kwargs["params"]["appkey"] == "key-2"

    # 第三次请求（轮询游标已绕回索引 0）：因 Key 1 仍在 60s 冷却期内，仍跳过 Key 1 直接使用健康的 Key 2
    res3 = c.search_goods("无线耳机")
    assert res3["status"] == "success"
    assert http.get.call_count == 4
    assert http.get.call_args_list[3].kwargs["params"]["appkey"] == "key-2"


def test_appkey_list_never_retries_on_unsafe_redirect_or_untrusted_gateway():
    http = SimpleNamespace(get=Mock(side_effect=[reply({}, 302), reply({"status": 200, "content": [item()]})]))
    c = JdClient(["key-1", "key-2"], "10001", "test-jd-key", "test-jd-secret", http_session=http)
    assert c.search_goods("蓝牙耳机")["error_code"] == "UNSAFE_REDIRECT"
    assert http.get.call_count == 1

    evil_signed = "https://evil.test/routerjson?method=" + ORDER_METHOD
    order_http = SimpleNamespace(get=Mock(side_effect=[reply({"status": 200, "content": evil_signed}), reply({"status": 200, "content": SIGNED})]))
    c2 = JdClient(["key-1", "key-2"], "10001", "test-jd-key", "test-jd-secret", http_session=order_http)
    assert c2.query_orders(0, 100)["error_code"] == "UNTRUSTED_GATEWAY"
    assert order_http.get.call_count == 1


def test_rank_encrypted_item_id_converts_directly_with_jingfen_url_without_detail_call():
    rank_item_id = "ph4N7kTDsgSNgYyttv5GNgYyttv5G4_373ZMX5zcfkxXRxU0H"
    http = http_for(wrapped(PROMOTION_ROOT, {"code": 200, "data": {
        "shortURL": "https://u.jd.com/rankshort", "clickURL": "https://union-click.jd.com/ranklong",
        "jCommand": "榜单京口令", "jShortCommand": "榜单短口令"}}, field="result", string=True))
    a = adapter(client(http))
    result = a.convert_promotion_url(rank_item_id, uid="u_8bb0595a", promoter_id="3110104129")
    assert result["status"] == "success"
    assert result["short_url"] == "https://u.jd.com/rankshort"
    assert http.get.call_count == 1
    assert http.get.call_args.kwargs["params"]["materialId"] == f"https://jingfen.jd.com/detail/{rank_item_id}.html"


def test_encrypted_id_rewrites_stale_item_jd_url_to_jingfen_and_falls_back_to_detail_when_needed():
    # 1. 若传入了旧缓存的 https://item.jd.com/{encrypted_id}.html，JdAdapter 直接纠正为 https://jingfen.jd.com/detail/{encrypted_id}.html；
    # 2. 若京东对榜单混淆 ID 返回 2001104，JdClient 识别为京东业务错误不轮询其余 appkey，并由 JdAdapter 调 api_detail.ashx 换取重签名 tao_id 重试。
    encrypted_id = "2JQgZkZ2nGOIkKfwpvzdIkKfwpvzdQ_3SD7M7MGcugkXyuI4w"
    resigned_id = "2JQkZkZ2nGOIkKHE8K75IkKfe6mXtQ_3SD7M7MGcugkXyuI4w"
    resigned_jingfen_url = f"https://jingfen.jd.com/detail/{resigned_id}.html"
    http = http_for(
        wrapped(PROMOTION_ROOT, {"code": 2001104, "message": "2001104_您的账号无权限访问，原因：商品不在推广中", "data": None}, field="result", string=True),
        {"status": 200, "content": [item(resigned_id, item_url=resigned_jingfen_url)]},
        wrapped(PROMOTION_ROOT, {"code": 200, "data": {
            "shortURL": "https://u.jd.com/2riP3lL", "clickURL": "https://union-click.jd.com/jdc?e=test",
            "jCommand": "长京口令", "jShortCommand": "https://u.jd.com/2riP3lL CA1507"}}, field="result", string=True),
    )
    c = JdClient(["key-1", "key-2", "key-3"], "10001", "test-jd-key", "test-jd-secret", http_session=http)
    a = adapter(c)
    result = a.convert_promotion_url(
        encrypted_id, uid="u_8bb0595a", promoter_id="3110104129",
        trusted_product_url=f"https://item.jd.com/{encrypted_id}.html",
    )
    assert result["status"] == "success"
    assert result["short_url"] == "https://u.jd.com/2riP3lL"
    assert result["promotion_token"] == "https://u.jd.com/2riP3lL CA1507"
    assert http.get.call_count == 3
    assert http.get.call_args_list[0].kwargs["params"]["materialId"] == f"https://jingfen.jd.com/detail/{encrypted_id}.html"
    assert http.get.call_args_list[1].args == (DEFAULT_API_BASE + "/api_detail.ashx",)
    assert http.get.call_args_list[2].kwargs["params"]["materialId"] == resigned_jingfen_url


def test_live_empty_order_response_without_data_key_is_accepted_as_empty_page():
    # 实测京东联盟 jd.union.open.order.row.query 在时间窗无订单时省略 data 字段，仅返回 code=200, message='success', requestId
    body = wrapped(ORDER_ROOT, {"code": 200, "message": "success", "requestId": "o_064e3d2a_mw5i63kh_17803994"}, string=True)
    res = parse_order_response(body)
    assert res == {"status": "success", "order_list": [], "has_more": False, "total_count": 0}


def test_jd_detail_301_rate_limit_vs_off_promotion_arbitration_and_eviction():
    from cache import clear_all_caches, is_product_off_promotion, RECOMMEND_CACHE
    from services.product_service import ProductService
    clear_all_caches()
    off_promo_id = "oxXpYeb9FgS3eszB2h2f3eszB2h2fX_3TYrAutPKopeY0fpzO"
    empty_union = wrapped("jd_union_open_goods_query_response", {"code": 200, "data": []}, field="result", string=True)
    # 场景1：api_detail.ashx 返回 301 对应宝贝已下架，且 goods_query 返回 1000 限流 -> 判定为 RATE_LIMIT（绝不调 byunionid 猜限流）
    http_rate_limited = route_http({
        "/open_jing_union_open_goods_query.ashx": {"status": 301, "content": "请求频率超限(1000)"},
        "/api_detail.ashx": {"status": 301, "content": "对应宝贝已下架"},
        "/open_jing_union_open_promotion_byunionid_get.ashx": AssertionError("get_goods_detail must never probe byunionid_get"),
    })
    a_alive = adapter(client(http_rate_limited))
    res_alive = a_alive.get_goods_detail(off_promo_id, allow_fallback=False)
    assert res_alive["status"] == "error"
    assert res_alive["error_code"] == "RATE_LIMIT"

    # 场景2：api_detail.ashx 返回 301 对应宝贝已下架，且 goods_query 返回 code=200 data=[] -> 交叉实锤 PRODUCT_OFF_PROMOTION
    http_off_promo = route_http({
        "/open_jing_union_open_goods_query.ashx": empty_union,
        "/api_detail.ashx": {"status": 301, "content": "对应宝贝已下架"},
        "/open_jing_union_open_promotion_byunionid_get.ashx": AssertionError("get_goods_detail must never probe byunionid_get"),
    })
    a_dead = adapter(client(http_off_promo))
    res_dead = a_dead.get_goods_detail(off_promo_id)
    assert res_dead["status"] == "error"
    assert res_dead["error_code"] == "PRODUCT_OFF_PROMOTION"

    # 场景3：ProductService 收到 PRODUCT_OFF_PROMOTION 时，不降级返回陈旧本地缓存，而是立即从全缓存与本地库物理驱逐并记入黑名单
    repo = Mock()
    repo.get_product_detail.return_value = {"platform": "jd", "goods_id": off_promo_id, "goods_sign": off_promo_id, "name": "旧缓存商品", "price": 99.0}
    repo.delete_product.return_value = True
    RECOMMEND_CACHE.set(("jd", "", 5, 0, 20, ""), {"status": "success", "data": [{"goods_id": off_promo_id, "goods_sign": off_promo_id}]})
    svc = ProductService(product_repo=repo)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("services.product_service.get_adapter", lambda plat: a_dead)
        http_off_promo2 = route_http({
            "/open_jing_union_open_goods_query.ashx": empty_union,
            "/api_detail.ashx": {"status": 301, "content": "对应宝贝已下架"},
        })
        a_dead.client = client(http_off_promo2)
        svc_res = svc.get_goods_detail(platform="jd", goods_id=off_promo_id, goods_sign=off_promo_id)
    assert svc_res["status"] == "error"
    assert svc_res["error_code"] == "PRODUCT_OFF_PROMOTION"
    repo.delete_product.assert_called_once_with("jd", goods_id=off_promo_id, goods_sign=off_promo_id)
    assert is_product_off_promotion("jd", off_promo_id, off_promo_id) is True
    assert RECOMMEND_CACHE.get(("jd", "", 5, 0, 20, ""))["data"] == []
    clear_all_caches()


def test_jd_image_key_never_calls_upstream_detail_and_service_upgrades_sign():
    from cache import clear_all_caches
    from services.product_service import ProductService

    clear_all_caches()
    img_key = "6ab9c932F86c506ae_0083320320b195ad"
    real_sign = "wAQEbTBpySSwDZcuvK0xwDZcuvK0xQ_3ioMtBxPdPEEdaHrjC"
    cola_img = "https://img14.360buyimg.com/pop/jfs/t1/523108/39/14102/81368/6ab9c932F86c506ae/0083320320b195ad.jpg"
    wrong_img = "https://img14.360buyimg.com/pop/jfs/t1/506834/27/8256/61208/6a901dbdFd5a8f134/00833203200bd826.png"

    # 1. 直接向 JdAdapter 传入主图哈希 ID 时，必须立刻拦截，绝不请求折淘客 api_detail.ashx
    c = Mock()
    a = adapter(c)
    direct = a.get_goods_detail(goods_id=img_key, goods_sign=img_key)
    assert direct["status"] == "error" and direct["error_code"] == "INVALID_ARGUMENT"
    c.get_goods_detail.assert_not_called()

    # 2. 若上游返回的唯一商品主图哈希与请求的 ref.goods_id 冲突，必须拒绝防止串货
    c.get_goods_detail.return_value = {"status": "success", "data": [
        item("other_sign_12345678901234567890_123456789012345678", pict_url=wrong_img,
             item_url="https://jingfen.jd.com/detail/other_sign_12345678901234567890_123456789012345678.html")
    ]}
    conflict = a.get_goods_detail(goods_id=img_key, goods_sign=real_sign)
    assert conflict["status"] == "error" and conflict["error_code"] == "PRODUCT_IDENTITY_MISMATCH"

    # 3. ProductService 收到 goods_sign == goods_id 时，先按 key_type='id' 查库并自动升级为真实 50 位 goods_sign 请求上游
    c.get_goods_detail.return_value = {"status": "success", "data": [
        item("wAQDbTBpySSwDZccMINYwDZcuvK0xQ_3ioMtBxPdPEEdaHrjC", title="可口可乐330ml*24", pict_url=cola_img,
             item_url="https://jingfen.jd.com/detail/wAQDbTBpySSwDZccMINYwDZcuvK0xQ_3ioMtBxPdPEEdaHrjC.html")
    ]}
    repo = Mock()
    repo.get_product_detail.return_value = {
        "platform": "jd", "product_id": img_key, "goods_id": img_key, "goods_sign": real_sign,
        "name": "可口可乐330ml*24", "price": 46.9, "image_url": cola_img, "detail_updated_at": None,
    }
    repo.upsert_product_detail.return_value = True
    svc = ProductService(product_repo=repo)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("services.product_service.get_adapter", lambda plat: a)
        res = svc.get_goods_detail(platform="jd", goods_id=img_key, goods_sign=img_key)
    assert res["status"] == "success"
    assert res["data"]["goods_id"] == img_key
    assert res["data"]["goods_sign"] == real_sign
    c.get_goods_detail.assert_called_with(real_sign)
    clear_all_caches()


def test_jd_universal_convert_passes_raw_text_directly_and_never_falls_back_to_keyword_search():
    # 1. 混排文案先从文本精准提取内嵌 u.jd.com URL，4步直线转链：前置未出卡 -> byunionid_get 直转 -> 补 1 次 api_detail(newshort) 拿全卡（0 次 tkl_piliang / api_quanwang）
    empty_union = wrapped("jd_union_open_goods_query_response", {"code": 200, "data": []}, field="result", string=True)
    promo_ok = {
        "jd_union_open_promotion_byunionid_get_response": {
            "result": json.dumps({
                "code": 200,
                "data": {
                    "shortURL": "https://u.jd.com/newshort",
                    "clickURL": "https://union-click.jd.com/jdc?e=new",
                    "jCommand": "12:/领5元优惠券！抢购可口可乐330ml*24！ ！ABCD1234EFGH！ CA1234",
                    "jShortCommand": "！ABCD1234EFGH！ CA1234",
                },
            }),
        },
    }
    coke_detail = {
        "status": 200,
        "content": [{
            "tao_id": "wAQEbTBpySSwDZcuvK0xwDZcuvK0xQ_3ioMtBxPdPEEdaHrjC",
            "title": "可口可乐330ml*24",
            "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/523108/39/14102/81368/6ab9c932F86c506ae/0083320320b195ad.jpg",
            "size": "49.2",
            "quanhou_jiage": "44.28",
            "coupon_info_money": "4.92",
            "tkrate3": "4",
            "tkfee3": "1.77",
            "nick": "可口可乐京东自营旗舰店",
            "volume": "20000",
            "item_url": "https://jingfen.jd.com/detail/wAQEbTBpySSwDZcuvK0xwDZcuvK0xQ_3ioMtBxPdPEEdaHrjC.html",
        }],
    }
    detail_replies = iter([
        {"status": 301, "content": "对应宝贝已下架"},
        coke_detail,
    ])
    s = route_http({
        "/api_detail.ashx": lambda _u, _p: next(detail_replies),
        "/open_jing_union_open_goods_query.ashx": empty_union,
        "/open_jing_union_open_promotion_byunionid_get.ashx": promo_ok,
    })
    a = adapter(client(s))
    mixed_text = "★京东超级补贴★ 可口可乐330ml*24 售价：49.2元[下单链接：https://u.jd.com/oldshort]返2元红包！"
    res = a.resolve_and_convert_universal(mixed_text, promoter_id="8899")
    assert res["status"] == "success"
    assert res["short_url"] == "https://u.jd.com/newshort"
    assert res["promotion_token"] == "！ABCD1234EFGH！ CA1234"
    assert res["product"]["name"] == "可口可乐330ml*24"
    assert s.get.call_count == 4
    assert s.get.call_args_list[0].args[0].endswith("/api_detail.ashx")
    assert s.get.call_args_list[1].args[0].endswith("/open_jing_union_open_goods_query.ashx")
    assert s.get.call_args_list[2].args[0].endswith("/open_jing_union_open_promotion_byunionid_get.ashx")
    assert s.get.call_args_list[2].kwargs["params"]["materialId"] == "https://u.jd.com/oldshort"
    assert s.get.call_args_list[3].args[0].endswith("/api_detail.ashx")
    assert s.get.call_args_list[3].kwargs["params"]["tao_id"] == "https://u.jd.com/newshort"
    assert not any("tkl_piliang" in c.args[0] or "api_quanwang" in c.args[0] for c in s.get.call_args_list)

    # 2. 当转链失败（如仅含商品标题无链接/口令）时，直接返回错误，绝不调用 search_goods 模糊搜索顶替商品
    c_fail = Mock()
    c_fail.convert_universal_material.return_value = {
        "status": "error",
        "error_code": "UPSTREAM_ERROR",
        "error_msg": "未能转链",
    }
    a_fail = adapter(c_fail)
    fail_res = a_fail.resolve_and_convert_universal("【京东】「可口可乐330ml*24」抢购好物", promoter_id="8899")
    assert fail_res["status"] == "error"
    c_fail.search_goods.assert_not_called()


def test_rank_coupon_list_extracts_single_item_usable_coupon_without_side_effects():
    from cache import clear_all_caches
    from models.product import ProductRef
    from repositories.product_repo import ProductRepository
    from services.promote_service import PromoteService
    from tests.test_product_repository import MemoryCollection as ProductMemCol

    clear_all_caches()
    cola_sign = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    cola_img = "https://img14.360buyimg.com/pop/jfs/t1/550001/12/3456/78901/6af74ca5F32ceb84f/0083320320631ee4.jpg"
    cola_coupon = "https://coupon.m.jd.com/coupons/show.action?linkKey=AAROH_xIpeffAs_-naABEFoeyXTPnH86"

    rows = [
        # 1. 单件可直接使用的官方券（满28减4，原价64 -> 券后60）
        {
            "itemId": cola_sign,
            "skuName": "可口可乐500ml*24瓶整箱",
            "wlprice": 64.0,
            "imageUrl": cola_img,
            "commissionShare": 7.0,
            "commission": 4.2,
            "purchasePriceInfo": {
                "price": 64.0,
                "purchasePrice": 60.0,
                "couponList": [{
                    "discount": 4.0, "quota": 28.0, "link": cola_coupon,
                    "couponStatus": 0, "remainCnt": 50000,
                }],
            },
        },
        # 2. 多件凑单 / PLUS 礼金导致 purchasePrice < wlprice 但无可用单品券 -> 严禁虚构低价
        {
            "itemId": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_noCouponPromo12345",
            "skuName": "凑单促销商品",
            "wlprice": 29.7,
            "imageUrl": "https://img.test/no_coupon.jpg",
            "purchasePriceInfo": {"price": 29.7, "purchasePrice": 19.9, "couponList": []},
        },
        # 3. 高门槛满减券（单件20元，券门槛满99减15）+ 已领完券 -> 单件不可用，保持原价
        {
            "itemId": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_highQuotaOnly12345",
            "skuName": "高门槛券商品",
            "wlprice": 20.0,
            "imageUrl": "https://img.test/high_quota.jpg",
            "purchasePriceInfo": {
                "price": 20.0,
                "purchasePrice": 20.0,
                "couponList": [
                    {"discount": 15.0, "quota": 99.0, "link": cola_coupon, "couponStatus": 0, "remainCnt": 100},
                    {"discount": 5.0, "quota": 10.0, "link": cola_coupon, "couponStatus": 0, "remainCnt": 0},
                ],
            },
        },
        # 4. 多张同品非叠加券（满19减5 + 满20减10，上游虚算7.9）-> 仅取单张最优券 22.9 - 10 = 12.9
        {
            "itemId": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_multiCoupons123456",
            "skuName": "多券择优商品",
            "wlprice": 22.9,
            "imageUrl": "https://img.test/multi.jpg",
            "purchasePriceInfo": {
                "price": 22.9,
                "purchasePrice": 7.9,
                "couponList": [
                    {"discount": 5.0, "quota": 19.0, "link": cola_coupon + "1", "couponStatus": 0, "remainCnt": 100},
                    {"discount": 10.0, "quota": 20.0, "link": cola_coupon + "2", "couponStatus": 0, "remainCnt": 100},
                ],
            },
        },
    ]

    http = http_for(wrapped(RANK_ROOT, {"code": 200, "data": rows}))
    jd_adapter = adapter(client(http))
    res = jd_adapter.get_recommend_goods(channel_type=5, limit=10)
    assert res["status"] == "success"
    c0, c1, c2, c3 = res["data"]

    assert c0["price"] == 60.0 and c0["original_price"] == 64.0
    assert c0["coupon_amount"] == 4.0 and c0["has_coupon"] is True and c0["coupon_url"] == cola_coupon

    assert c1["price"] == 29.7 and c1["coupon_amount"] == 0.0 and c1["has_coupon"] is False and "coupon_url" not in c1
    assert c2["price"] == 20.0 and c2["coupon_amount"] == 0.0 and c2["has_coupon"] is False and "coupon_url" not in c2
    assert c3["price"] == 12.9 and c3["original_price"] == 22.9 and c3["coupon_amount"] == 10.0 and c3["coupon_url"] == cola_coupon + "2"

    # 榜单入库已自带官方 coupon_url，后续点击转链直接命中本地券链接，0 次额外详情 API 请求
    col = ProductMemCol()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda name: col))
    repo.upsert_home_products([c0], channel_type=5)
    mock_client = Mock()
    no_extra_adapter = adapter(mock_client)
    promo_svc = PromoteService(product_repo=repo)
    resolved_coupon = promo_svc._trusted_coupon_url(
        ProductRef("jd", c0["goods_id"], cola_sign), adapter=no_extra_adapter,
    )
    assert resolved_coupon == cola_coupon
    mock_client.get_goods_detail.assert_not_called()
    clear_all_caches()


def test_merchant_main_image_update_preserves_detail_and_coupon_via_verified_cipher_pair():
    from cache import clear_all_caches
    from services.product_service import ProductService

    clear_all_caches()
    old_img_key = "6af2db48Fa28b7d27_0083320320b55f09"
    old_img_url = f"https://img14.360buyimg.com/pop/jfs/t1/540000/1/100/200/{old_img_key.replace('_', '/')}.jpg"
    new_img_key = "6af74ca5F32ceb84f_0083320320631ee4"
    new_img_url = f"https://img14.360buyimg.com/pop/jfs/t1/550000/2/300/400/{new_img_key.replace('_', '/')}.jpg"

    req_sign = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    same_pool_resalt = "IwQNjSHMMam3HPzzzzzz3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    resalted_cipher = "Ip4ZpZqBj5GQZVLMtk0dQZVturRAQ4_3NM1doJsxx4B57H48G"
    coupon_link = "https://coupon.m.jd.com/coupons/show.action?linkKey=AAROH_xIpeffAs_-naABEFoeyXTPnH86"

    # 1a. 同池重加盐且商家更换主图（prefix[:14]+suffix 一致）：api_detail 直接放行并更新 4 元优惠券
    c = Mock()
    c.get_goods_detail.return_value = {
        "status": "success",
        "data": [item(
            same_pool_resalt,
            title="可口可乐500ml*24瓶整箱",
            size="64",
            quanhou_jiage="60",
            coupon_info_money="4",
            coupon_info="满28元减4元",
            coupon_id=coupon_link,
            pict_url=new_img_url,
            item_url=f"https://jingfen.jd.com/detail/{same_pool_resalt}.html",
        )],
    }
    a = adapter(c)
    repo = Mock()
    repo.get_product_detail.return_value = {
        "platform": "jd", "product_id": old_img_key, "goods_id": old_img_key, "goods_sign": req_sign,
        "name": "可口可乐500ml*24瓶整箱", "price": 64.0, "original_price": 64.0,
        "coupon_amount": 0.0, "has_coupon": False, "image_url": old_img_url, "detail_updated_at": None,
    }
    repo.upsert_product_detail.return_value = True
    svc = ProductService(product_repo=repo)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("services.product_service.get_adapter", lambda plat: a)
        detail_res = svc.get_goods_detail(platform="jd", goods_id=old_img_key, goods_sign=req_sign)

    assert detail_res["status"] == "success"
    assert detail_res.get("source") != "database_fallback"
    assert detail_res["data"]["price"] == 60.0
    assert detail_res["data"]["original_price"] == 64.0
    assert detail_res["data"]["coupon_amount"] == 4.0
    assert detail_res["data"]["has_coupon"] is True
    assert detail_res["data"]["coupon_url"] == coupon_link
    assert detail_res["data"]["goods_id"] == new_img_key
    assert detail_res["data"]["goods_sign"] == req_sign

    # 1b. 跨密钥池重加密 + 主图同时变更：api_detail 拒猜，由 goods_query 凭 oriItemId==req_sign 权威确证；伪造假 ID 严格拒收
    c_cross = Mock()
    c_cross.get_goods_detail.return_value = {
        "status": "success",
        "data": [item("WaTNAju9LsLL7rmn1wRVL7rmn1wRVT_sweEl8DJRszhniFt6P", title="误搜遥控器", pict_url=new_img_url)],
    }
    c_cross.query_union_goods.return_value = {
        "status": "success",
        "data": [{
            "oriItemId": req_sign,
            "itemId": resalted_cipher,
            "skuId": 100088,
            "skuName": "可口可乐500ml*24瓶整箱",
            "materialUrl": f"item.jd.com/{resalted_cipher}.html",
            "imageInfo": {"imageList": [{"url": new_img_url}]},
            "priceInfo": {"price": 64.0, "lowestCouponPrice": 60.0},
            "couponInfo": {"couponList": [{"discount": 4.0, "quota": 28.0, "link": coupon_link, "useEndTime": 4102415999000, "isBest": 1}]},
            "commissionInfo": {"commissionShare": 5.0, "couponCommission": 3.0},
            "shopInfo": {"shopName": "可口可乐京东自营"},
            "comments": 10000,
        }],
    }
    cross_res = adapter(c_cross).get_goods_detail(req_sign, goods_id=old_img_key, goods_sign=req_sign)
    assert cross_res["status"] == "success" and cross_res["_provider"] == "ztk_jd_goods_query_detail"
    assert cross_res["data"]["name"] == "可口可乐500ml*24瓶整箱"

    c_fake = Mock()
    c_fake.get_goods_detail.return_value = {
        "status": "success",
        "data": [item("WaTNAju9LsLL7rmn1wRVL7rmn1wRVT_sweEl8DJRszhniFt6P", title="误搜遥控器", pict_url=new_img_url)],
    }
    c_fake.query_union_goods.return_value = {"status": "success", "data": [], "total_count": 0}
    fake_det = adapter(c_fake).get_goods_detail(req_sign, goods_id=old_img_key, goods_sign=req_sign)
    assert fake_det["status"] == "error" and fake_det["error_code"] == "NOT_FOUND"

    # 2. get_goods_detail 遇 301 下架且 goods_query 返回 1000 限流时裁决为 RATE_LIMIT（不再调 byunionid 猜限流或撞 api_quanwang）
    c_rl = Mock()
    c_rl.get_goods_detail.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR", "ztk_ambiguous_301": True}
    c_rl.query_union_goods.return_value = {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "1000 limit"}
    resilient = adapter(c_rl).get_goods_detail(req_sign, goods_id=old_img_key, goods_sign=req_sign)
    assert resilient["status"] == "error" and resilient["error_code"] == "RATE_LIMIT"
    c_rl.convert_promotion_link.assert_not_called()
    c_rl.search_goods.assert_not_called()

    # 3. 防串货红线不变：若上游退化返回非标准加密 ID（如误解析成数字 SKU '6'）且主图冲突，严格拒绝
    c.get_goods_detail.return_value = {
        "status": "success",
        "data": [item("6", skuId="6", pict_url=new_img_url, item_url="https://item.jd.com/6.html")],
    }
    mismatch = a.get_goods_detail(req_sign, goods_id=old_img_key, goods_sign=req_sign)
    assert mismatch["status"] == "error" and mismatch["error_code"] == "PRODUCT_IDENTITY_MISMATCH"
    clear_all_caches()


def test_jd_search_goods_rescues_single_key_301_rate_limit_and_converges_on_genuine_empty():
    # Case 1: Key 1 returns 301 "无符合条件的数据" due to 1000-call throttle, Key 2 returns real data
    called_keys = []

    def _get_rate_limited(url, params=None, **_kwargs):
        k = params["appkey"]
        called_keys.append(k)
        if k == "jd-k1":
            return reply({"status": 301, "content": "无符合条件的数据"})
        return reply({"status": 200, "content": [{"tao_id": "100999", "tao_title": "京东好价抽纸", "quanhou_jiage": "19.9"}]})

    jd1 = JdClient(union_id="10001", appkey_list=["jd-k1", "jd-k2", "jd-k3", "jd-k4"], http_session=SimpleNamespace(get=_get_rate_limited))
    res1 = jd1.search_goods("维达抽纸", page=1, page_size=20)
    assert res1["status"] == "success" and len(res1["data"]) == 1
    assert called_keys == ["jd-k1", "jd-k2"]
    assert 0 in jd1._key_cooldown_until

    # Case 2: Genuine empty search across 4 keys stops after 2 keys confirm 301 (does not waste k3/k4)
    empty_keys = []

    def _get_truly_empty(url, params=None, **_kwargs):
        empty_keys.append(params["appkey"])
        return reply({"status": 301, "content": "无符合条件的数据"})

    jd2 = JdClient(union_id="10001", appkey_list=["jd-k1", "jd-k2", "jd-k3", "jd-k4"], http_session=SimpleNamespace(get=_get_truly_empty))
    res2 = jd2.search_goods("不存在的冷门词xyz", page=1, page_size=20)
    assert res2 == {"status": "success", "data": [], "has_more": False, "total_count": 0}
    assert empty_keys == ["jd-k1", "jd-k2"]


def test_jd_client_per_key_pacing_reserves_slots_per_appkey_and_skips_mocks(monkeypatch):
    import requests
    from infrastructure.api_governance import GLOBAL_QUOTA_PACER

    sleeps = []
    monkeypatch.setattr("infrastructure.api_governance.time.monotonic", lambda: 1000.0)
    monkeypatch.setattr("infrastructure.api_governance.time.sleep", lambda s: sleeps.append(round(s, 3)))
    monkeypatch.setattr("infrastructure.api_governance.is_real_requests_session", lambda s: isinstance(s, requests.Session))
    monkeypatch.setattr(GLOBAL_QUOTA_PACER, "_next_allowed_ts", {})

    # 1. Mocked http_session incurs zero pacing sleep
    mock_cli = JdClient(appkey_list=["k-mock"], union_id="2035679405", http_session=SimpleNamespace(get=lambda *a, **kw: None))
    mock_cli._pace_real_http_by_key({"params": {"appkey": "k-mock"}})
    assert sleeps == [] and GLOBAL_QUOTA_PACER._next_allowed_ts == {}

    # 2. Real requests.Session spaces same-key calls by 0.22s while allowing distinct keys immediately
    real_cli = JdClient(appkey_list=["k-real-1", "k-real-2"], union_id="2035679405", http_session=requests.Session())
    real_cli._pace_real_http_by_key({"params": {"appkey": "k-real-1"}})
    real_cli._pace_real_http_by_key({"params": {"appkey": "k-real-2"}})
    assert sleeps == []
    real_cli._pace_real_http_by_key({"params": {"appkey": "k-real-1"}})
    assert sleeps == [0.22]


def test_jd_client_pace_backlog_fast_fails_instead_of_blocking(monkeypatch):
    import requests
    from infrastructure.api_governance import GLOBAL_QUOTA_PACER

    cli = JdClient(appkey_list=["busy_key"], union_id="100001")
    monkeypatch.setattr("infrastructure.api_governance.is_real_requests_session", lambda s: isinstance(s, requests.Session))
    monkeypatch.setattr(GLOBAL_QUOTA_PACER, "_next_allowed_ts", {("ztk_jd_general", "busy_key"): 1000.0})
    monkeypatch.setattr("infrastructure.api_governance.time.monotonic", lambda: 100.0)

    res = cli._get("https://api.zhetaoke.com:10001/api/open_jing_union_open_goods_jingfen_query.ashx", params={"appkey": "busy_key"})
    assert res["status"] == "error" and res["error_code"] == "QUOTA_PACE_BACKLOG"


def test_jd_detail_rate_limit_fallback_uses_unified_detail_and_never_logs_red_cross(monkeypatch, caplog):
    import logging
    from cache import clear_all_caches
    from models.product import ProductRef
    from services import product_service as prod_mod

    clear_all_caches()
    enc_sign = "MHQuhO536DtWOykdGM94WOykdGM94Q_3RTWqX1JrEJSAPxknW"
    gid = "6ad1ed9fFa252fa1e_093532032071e8dc"

    class FakeJdDetailAdapter:
        active = True
        platform_code = "jd"
        platform_name = "京东"
        capabilities = {"detail": True}

        def resolve_product_ref(self, raw="", goods_id=None, goods_sign=None):
            return ProductRef("jd", goods_id or gid, goods_sign or enc_sign)

        def get_goods_detail(self, goods_id_or_sign="", goods_id=None, goods_sign=None, uid=None, **kwargs):
            assert kwargs.get("allow_fallback", True) is True
            return {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "京东详情接口触发临时频控"}

    repo = Mock()
    repo.get_product_detail.return_value = {
        "platform": "jd", "goods_id": gid, "goods_sign": enc_sign, "name": "小草先生纯黑芝麻油500ml",
        "price": 9.99, "original_price": 9.99,
        "image_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/6ad1ed9fFa252fa1e/093532032071e8dc.png",
    }
    monkeypatch.setattr(prod_mod, "get_adapter", lambda plat: FakeJdDetailAdapter())
    svc = prod_mod.ProductService(product_repo=repo)

    with caplog.at_level(logging.INFO):
        res = svc.get_goods_detail(platform="jd", goods_id=gid, goods_sign=enc_sign)
    assert res["status"] == "success" and res["source"] == "database_fallback"
    assert all("❌" not in rec.getMessage() for rec in caplog.records)
    clear_all_caches()


@pytest.mark.parametrize("price", [None, "", "bad", float("nan"), float("inf"), -1, False])
def test_missing_or_invalid_price_is_not_published_as_free(price):
    mock_client = Mock()
    mock_client.search_goods.return_value = {"status": "success", "data": [
        {"tao_id": "opaque", "title": "商品", "quanhou_jiage": price}]}
    jd_adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=mock_client)
    assert jd_adapter.search_goods("测试商品")["status"] == "error"


@pytest.mark.parametrize("price", [0, "0.00", 12.5])
def test_real_known_prices_including_zero_are_preserved(price):
    mock_client = Mock()
    mock_client.search_goods.return_value = {"status": "success", "data": [
        {"tao_id": "opaque", "title": "商品", "quanhou_jiage": price}]}
    jd_adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=mock_client)
    result = jd_adapter.search_goods("测试商品")
    assert result["status"] == "success"
    assert result["data"][0]["price"] == float(price)


@pytest.mark.parametrize("prefetch", [False, True])
def test_search_material_urls_saved_even_without_detail_prefetch(monkeypatch, prefetch):
    from cache import SEARCH_CACHE
    from services.product_service import ProductService

    SEARCH_CACHE.clear()
    material_item = {"platform": "jd", "goods_id": "opaque", "name": "商品", "price": 12,
                     "product_url": "https://jingfen.jd.com/detail/material.html"}
    fake_adapter = SimpleNamespace(active=True, requires_material_url=True,
                                   search_goods=Mock(return_value={"status": "success", "data": [material_item]}))
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: fake_adapter)
    repo = Mock()
    service = ProductService(repo)
    if prefetch:
        service.prefetcher = Mock()
    try:
        assert service.search_home_goods("测试商品", platform="jd")["status"] == "success"
    finally:
        service.wait_for_background_writes()
        SEARCH_CACHE.clear()
    repo.upsert_home_products.assert_called_once_with([material_item], channel_type=None)
    repo.upsert_product_detail.assert_not_called()


def test_jd_client_catalog_cooldown_short_circuits_catalog_only():
    """选品库突发冷却开启后，api_quanwang / tkl_piliang 零 HTTP 立即熔断；独立的 api_detail (50.9 QPS) 与 byunionid_get 不受连坐。"""
    http = http_for(
        {"status": 200, "content": [{"tao_id": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g", "title": "商品", "wlprice": "19.9", "pict_url": "https://img14.360buyimg.com/a.jpg"}]},
        wrapped(PROMOTION_ROOT, {"code": 200, "data": {"shortURL": "https://u.jd.com/ok"}}, field="result", string=True),
    )
    cli = client(http)
    cli.mark_catalog_rate_limited(60.0, force=True)
    assert cli.is_catalog_cooling() is True

    srch = cli.search_goods("抽纸")
    assert srch["error_code"] == "RATE_LIMIT" and srch.get("catalog_cooling") is True
    assert http.get.call_count == 0

    det = cli.get_goods_detail("IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g")
    assert det["status"] == "success"
    assert http.get.call_count == 1

    promo = cli.convert_promotion_link("https://jingfen.jd.com/detail/IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g.html", "88")
    assert promo["status"] == "success"
    assert promo["short_url"] == "https://u.jd.com/ok"
    assert http.get.call_count == 2


def test_jd_client_catalog_cooldown_stepped_backoff_and_reset_on_success():
    """连续限流冷却按 65s -> 120s -> 240s -> 300s 阶梯退避；同一冷却窗内不重复递增；api_quanwang / tkl_piliang 成功即归零复位。"""
    http = http_for(
        {"status": 200, "content": [{"tao_id": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g", "title": "商品", "wlprice": "19.9", "pict_url": "https://img14.360buyimg.com/a.jpg"}]},
    )
    cli = client(http)
    expected_steps = [65.0, 120.0, 240.0, 300.0, 300.0]
    for idx, expected_sec in enumerate(expected_steps):
        cli.mark_catalog_rate_limited(force=True)
        rem = cli.catalog_cooldown_remaining()
        assert expected_sec - 2.0 <= rem <= expected_sec + 0.5
        assert cli._catalog_cooldown_streak == idx + 1
        # 同一冷却窗内并发再次调用不得推高 streak
        cli.mark_catalog_rate_limited(force=True)
        assert cli._catalog_cooldown_streak == idx + 1
        cli._catalog_cooldown_until = 0.0

    res = cli.search_goods("抽纸")
    assert res["status"] == "success"
    assert cli._catalog_cooldown_streak == 0


def test_jd_union_goods_query_jingfen_and_resolve_product_from_link():
    """验证 ZTK :20000 联盟全网查询 (goods.query)、京粉精选 (jingfen) 及独立短链解析链 (resolve_product_from_link)。"""
    enc_id = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    union_row = {
        "itemId": enc_id,
        "skuId": 100088,
        "skuName": "维达超韧抽纸3层100抽*24包",
        "materialUrl": f"item.jd.com/{enc_id}.html",
        "imageInfo": {"imageList": [{"url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/vinda.jpg"}]},
        "priceInfo": {"price": 39.9, "lowestCouponPrice": 29.9},
        "couponInfo": {"couponList": [{"discount": 10.0, "quota": 20.0, "link": "https://coupon.m.jd.com/coupons/show.action?key=k1", "useEndTime": 2999999999000, "isBest": 1}]},
        "commissionInfo": {"commissionShare": 6.5, "couponCommission": 1.94},
        "shopInfo": {"shopName": "维达官方旗舰店"},
        "brandName": "维达",
        "comments": 50000,
    }
    jf_row = {**union_row, "itemId": "JfEncItemId1234567890_JfSuffix123456", "skuName": "京粉精选商品"}
    http = route_http({
        "/open_jing_union_open_goods_query.ashx": wrapped("jd_union_open_goods_query_response", {"code": 200, "data": [union_row], "totalCount": 1}, field="result", string=True),
        "/open_jing_union_open_goods_jingfen_query.ashx": wrapped("jd_union_open_goods_jingfen_query_response", {"code": 200, "data": [jf_row], "totalCount": 1}, field="result", string=True),
    })
    cli = JdClient("test-ztk-key", "10001", http_session=http)
    a = adapter(cli)

    # 1. 空关键词 search_goods 防死循环
    assert cli.search_goods("")["error_code"] == "INVALID_ARGUMENT"

    # 2. goods.query 支持按 u.jd.com 短链直接解析出官方卡片
    link_res = a.resolve_product_from_link("https://u.jd.com/or9ETP0")
    assert link_res["status"] == "success"
    assert link_res["is_venue"] is False
    assert link_res["product"]["name"] == "维达超韧抽纸3层100抽*24包"
    assert link_res["product"]["price"] == 29.9
    assert link_res["product"]["coupon_amount"] == 10.0
    assert link_res["product"]["product_url"] == f"https://jingfen.jd.com/detail/{enc_id}.html"

    # 3. goods.query 作为关键词搜索主通道；jingfen 作为榜单兜底
    sr = a.search_goods("维达抽纸", page=1, page_size=5)
    assert sr["status"] == "success" and sr["_provider"] == "ztk_jd_goods_query"
    assert sr["data"][0]["goods_sign"] == enc_id

    jf = cli.query_jingfen_goods(elite_id=22, page=1, page_size=5)
    assert jf["status"] == "success" and jf["data"][0]["skuName"] == "京粉精选商品"


def test_jd_official_direct_orders_md5_signing_and_ztk_fallback():
    """验证直连 api.jd.com 订单查询 (MD5 签名+code=2000冷却熔断) 与失败自动回退 ZTK，及 ZTK hop-2 重试。"""
    from infrastructure.api_governance import GLOBAL_PROVIDER_COOLDOWN

    GLOBAL_PROVIDER_COOLDOWN.reset()
    row = {"id": "ord_1", "orderId": "p_1", "skuId": "1001", "skuName": "抽纸", "validCode": 16,
           "estimateFee": "1.20", "actualFee": None, "orderTime": "2026-11-22 10:00:00",
           "modifyTime": "2026-11-22 10:05:00", "positionId": "77", "estimateCosPrice": "20.00", "commissionRate": "6.0"}
    post_ok = Mock(return_value=reply(wrapped(ORDER_ROOT, {"code": 200, "data": [row], "hasMore": False}, string=True)))
    get_unused = Mock(side_effect=AssertionError("ztk orders should not run when direct succeeds"))
    cli_ok = JdClient("ztk-key", "10001", "jd-app-key", "jd-app-secret",
                      http_session=SimpleNamespace(post=post_ok, get=get_unused))
    a_ok = adapter(cli_ok)
    res_ok = a_ok.fetch_increment_orders(1700000000, 1700000600, page=1, page_size=50)
    assert res_ok["status"] == "success"
    assert res_ok["_provider"] == "jd_official_orders"
    assert len(res_ok["order_list"]) == 1
    post_data = post_ok.call_args.kwargs["data"]
    assert post_data["method"] == "jd.union.open.order.row.query"
    assert post_data["app_key"] == "jd-app-key"
    assert len(post_data["sign"]) == 32 and post_data["sign"].isupper()

    # 直连返回 code=2000 限流 -> 自动回退 ztk_jd_orders；hop-2 瞬断自动重试 1 次成功
    post_2000 = Mock(return_value=reply(wrapped(ORDER_ROOT, {"code": 2000, "message": "limit"}, string=True)))
    ztk_get = Mock(side_effect=[
        reply({"status": 200, "content": SIGNED}),
        RuntimeError("hop2 glitch"),
        reply(wrapped(ORDER_ROOT, {"code": 200, "data": [row], "hasMore": False}, string=True)),
    ])
    cli_fb = JdClient("ztk-key", "10001", "jd-app-key", "jd-app-secret",
                      http_session=SimpleNamespace(post=post_2000, get=ztk_get))
    a_fb = adapter(cli_fb)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("infrastructure.jd_client.is_real_requests_session", lambda _s: True)
        mp.setattr("infrastructure.jd_client.time.sleep", lambda _sec: None)
        res_fb = a_fb.fetch_increment_orders(1700000000, 1700000600, page=1, page_size=50)
    assert res_fb["status"] == "success"
    assert res_fb["_provider"] == "ztk_jd_orders"
    assert ztk_get.call_count == 3


def test_non_official_hdk_cipher_skips_ztk_and_blocks_cross_product_hallucination():
    """回归防串联串货：非联盟标准 (<24..36>_<18>) 的好单库私有长密文必须跳过折淘客 api_detail 幻觉，且 _verify_detail_match 严格正向实证。"""
    from infrastructure.jd_client import is_encrypted_jd_item_id

    hdk_priv_sign = "VA02aBx1VAhDTydqHEVYFDkWYwJrd29XEl57X3x_FShUUD5OADxfbyFQA1RlZF5tC"
    assert is_encrypted_jd_item_id(hdk_priv_sign) is False
    assert is_encrypted_jd_item_id("cxBlCARSHRkObjR_SzZgezUXIG1iMwoNDRJAUCh") is False
    assert is_encrypted_jd_item_id("G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww") is True
    assert is_encrypted_jd_item_id("0R8MF8Nvx9ar3zRsREvH7tVo_3UIh70CfCxhC3uAwvJ") is True
    assert is_encrypted_jd_item_id("Za7HW7YfQ7ym7v81r9ZQm7vG20ZC6p7uS2ir_3NM15bsAVF41u3cVtA") is True

    fake_http = route_http({
        "/open_jing_union_open_goods_query.ashx": AssertionError("query_union_goods should be skipped for non-official sign"),
        "/api_detail.ashx": AssertionError("api_detail should be skipped for non-official sign"),
    })
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "success",
        "data": {
            "itemid": hdk_priv_sign,
            "skuid": hdk_priv_sign,
            "goodsname": "好单库海天调料套装",
            "itempic": "https://img14.360buyimg.com/pop/jfs/t20271010/538421/18/9854/219374/6aca4d10F381a5657/0944320320d0cf17.jpg",
            "itemprice": "18.90",
            "itemendprice": "11.90",
            "commissionshare": "9.00",
        },
    }
    cli = client(fake_http)
    a = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"),
        client=cli,
        hdk_client=hdk_mock,
    )
    res = a.get_goods_detail(hdk_priv_sign, goods_id="6aca4d10F381a5657_0944320320d0cf17", goods_sign=hdk_priv_sign)
    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_detail"
    assert res["data"]["name"] == "好单库海天调料套装"
    assert fake_http.get.call_count == 0

    # 即便调用 _verify_detail_match 且传入的非图片键也不在 rows 里，也绝不能放行随机商品（如笔记本电脑）
    hallucinated_row = item(
        "Ip47p46tKKbQZuFz7MhZQZuFz7MhZ4_3NM1doJsxx4NrEHtJG",
        title="联想ThinkPad笔记本电脑",
        pict_url="https://img14.360buyimg.com/pop/jfs/t1/583642/33/5494/68856/6afa7967F50cc2d4e/0083320320eaa21c.jpg",
        item_url="https://jingfen.jd.com/detail/Ip47p46tKKbQZuFz7MhZQZuFz7MhZ4_3NM1doJsxx4NrEHtJG.html",
    )
    mismatch = a._verify_detail_match([hallucinated_row], a.resolve_product_ref(hdk_priv_sign), hdk_priv_sign)
    assert mismatch["status"] == "error" and mismatch["error_code"] == "PRODUCT_IDENTITY_MISMATCH"


def test_jd_short_url_coupon_timestamps_cooling_preservation_and_uid_fail_closed():
    """验证 JD 短链 returnurl/去查询串判定、券时间戳、冷却保底保留、HDK 密文跳过 ZTK 与个人 uid 宁缺毋滥。"""
    from infrastructure.jd_client import resolve_jd_short_url

    # 1. resolve_jd_short_url: returnurl 提取单品 SKU，会场判断剥离 query 避免假阳性，非京东域名触发 UNSAFE_REDIRECT
    def _short_sess(loc):
        s = Mock()
        s.get.side_effect = [
            SimpleNamespace(status_code=302, headers={"Location": loc}),
            SimpleNamespace(status_code=200, text=""),
        ]
        return s

    r_ret = resolve_jd_short_url(
        "https://u.jd.com/test1",
        http_session=_short_sess("https://passport.jd.com/new/login.aspx?ReturnUrl=https%3A%2F%2Fitem.jd.com%2F1000123456.html"),
    )
    assert r_ret["status"] == "success" and r_ret["data"]["sku_id"] == "1000123456" and r_ret["data"]["is_venue"] is False
    r_q = resolve_jd_short_url(
        "https://u.jd.com/test2",
        http_session=_short_sess("https://m.jd.com/jump?ref=https://pro.m.jd.com/mall/active/abc/index.html"),
    )
    assert r_q["status"] == "success" and r_q["data"]["is_venue"] is False
    r_v = resolve_jd_short_url(
        "https://u.jd.com/test3",
        http_session=_short_sess("https://pro.m.jd.com/mall/active/abc/index.html?utm=1"),
    )
    assert r_v["status"] == "success" and r_v["data"]["is_venue"] is True
    r_evil = resolve_jd_short_url(
        "https://u.jd.com/test4",
        http_session=_short_sess("https://other.example.com/phishing"),
    )
    assert r_evil["status"] == "error" and r_evil["error_code"] == "UNSAFE_REDIRECT"

    # 2. _extract_union_coupon: 过滤过期/未开始券，正确识别毫秒级有效券
    import time
    now_ms = int(time.time() * 1000)
    disc, clink, _ = JdAdapter._extract_union_coupon({
        "couponInfo": {
            "couponList": [
                {"discount": 30, "quota": 40, "link": "https://coupon.m.jd.com/expired", "useEndTime": now_ms - 60_000},
                {"discount": 25, "quota": 40, "link": "https://coupon.m.jd.com/future", "useStartTime": now_ms + 3600_000, "useEndTime": now_ms + 7200_000},
                {"discount": 10, "quota": 20, "link": "https://coupon.m.jd.com/valid", "useStartTime": now_ms - 60_000, "useEndTime": now_ms + 3600_000},
            ]
        }
    }, 50.0)
    assert disc == 10.0 and clink == "https://coupon.m.jd.com/valid"

    # 3. get_goods_detail: ZTK 触发 RATE_LIMIT 时若 HDK 仅返回 NOT_FOUND，保留 RATE_LIMIT + catalog_cooling
    official_sign = "G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww"
    cooling_state = {"on": False}

    def _ztk_detail_rl(*a, **kw):
        cooling_state["on"] = True
        return {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "选品库频控"}

    rl_cli = Mock(
        appkey_list=("k1",),
        is_catalog_cooling=lambda: cooling_state["on"],
        convert_promotion_link=lambda *a, **kw: {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "限流"},
        get_goods_detail=_ztk_detail_rl,
    )
    hdk_nf = Mock(
        appkey_list=("hk1",),
        get_goods_detail=lambda *a, **kw: {"status": "error", "error_code": "NOT_FOUND", "error_msg": "未收录", "not_found": True},
    )
    a_rl = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"),
        client=rl_cli, hdk_client=hdk_nf,
    )
    rl_res = a_rl.get_goods_detail(official_sign, goods_sign=official_sign)
    assert rl_res["status"] == "error" and rl_res["error_code"] == "RATE_LIMIT" and rl_res.get("catalog_cooling") is True

    # 4. _promote_via_ztk 跳过非官方 HDK 1-underscore 密文，改走 HDK 转链成功
    hdk_priv_sign = "VA02aBx1VAhDTydqHEVYFDkWYwJrd29XEl57X3x_FShUUD5OADxfbyFQA1RlZF5tC"
    ztk_promo_cli = Mock(
        appkey_list=("k1",),
        is_catalog_cooling=lambda: False,
        convert_promotion_link=Mock(side_effect=AssertionError("ZTK should skip HDK private cipher")),
    )
    hdk_promo_cli = Mock(
        appkey_list=("hk1",),
        convert_promotion_link=lambda *a, **kw: {"status": "success", "short_url": "https://u.jd.com/hdkok", "click_url": "https://u.jd.com/hdkok"},
    )
    a_hdk_skip = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"),
        client=ztk_promo_cli, hdk_client=hdk_promo_cli,
    )
    conv_hdk = a_hdk_skip.convert_promotion_url(
        hdk_priv_sign,
        goods_id="6aca4d10F381a5657_0944320320d0cf17",
        goods_sign=hdk_priv_sign,
        promoter_id="88",
    )
    assert conv_hdk["status"] == "success" and conv_hdk["final_link"] == "https://u.jd.com/hdkok"
    assert ztk_promo_cli.convert_promotion_link.call_count == 0

    # 5. resolve_and_convert_universal: 传入 uid 且无有效推广位时宁缺毋滥；冷却期仍优先 ZTK byunionid_get（独立池不连坐），失败才降级 HDK
    a_no_prom = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="", ztk_appkey="k1"),
        client=rl_cli, hdk_client=hdk_promo_cli,
    )
    fail_uid = a_no_prom.resolve_and_convert_universal("https://item.jd.com/1000123456.html", uid="u_person")
    assert fail_uid["status"] == "error" and fail_uid["error_code"] in ("INVALID_PROMOTER_ID", "CONFIG_ERROR")

    cool_ztk_ok = Mock(
        appkey_list=("k1",),
        is_catalog_cooling=lambda: True,
        convert_promotion_link=Mock(return_value={"status": "success", "short_url": "https://u.jd.com/ztkfb"}),
    )
    hdk_unused = Mock(
        appkey_list=("hk1",),
        convert_promotion_link=Mock(return_value={"status": "success", "short_url": "https://u.jd.com/hdkfb"}),
    )
    a_cool_fb = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"),
        client=cool_ztk_ok, hdk_client=hdk_unused,
    )
    fb_res = a_cool_fb._convert_material_url_with_fallback(
        f"https://jingfen.jd.com/detail/{official_sign}.html",
        "88",
        is_personal=False,
        goods_sign=official_sign,
    )
    assert fb_res["status"] == "success" and fb_res["final_link"] == "https://u.jd.com/ztkfb"
    assert cool_ztk_ok.convert_promotion_link.call_count == 1
    assert hdk_unused.convert_promotion_link.call_count == 0

    cool_ztk_err = Mock(
        appkey_list=("k1",),
        is_catalog_cooling=lambda: True,
        convert_promotion_link=Mock(return_value={"status": "error", "error_code": "UPSTREAM_ERROR"}),
    )
    a_hdk_fb = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"),
        client=cool_ztk_err, hdk_client=hdk_unused,
    )
    hdk_res = a_hdk_fb._convert_material_url_with_fallback(
        f"https://jingfen.jd.com/detail/{official_sign}.html",
        "88",
        is_personal=False,
        goods_sign=official_sign,
    )
    assert hdk_res["status"] == "success" and hdk_res["final_link"] == "https://u.jd.com/hdkfb"
    assert hdk_unused.convert_promotion_link.call_count == 1


def test_jd_universal_domain_guard_and_foreground_vs_background_cooling():
    from infrastructure.api_governance import api_caller_context
    from infrastructure.jd_client import _is_trusted_jd_redirect_url
    from models.product import JD_DOMAINS, url_matches_domains

    # 1. 官方域名匹配严谨且拒绝伪域名、淘宝/拼多多跨平台域名，可信重定向含 jd.hk / yiyaojd.com
    assert url_matches_domains("https://u.jd.com/oOUaUwq", JD_DOMAINS) is True
    assert url_matches_domains("https://coupon.m.jd.com/coupons/show.action?key=1", JD_DOMAINS) is True
    assert url_matches_domains("https://eviljd.com/123.html", JD_DOMAINS) is False
    assert _is_trusted_jd_redirect_url("https://npcitem.jd.hk/100234.html") is True
    assert _is_trusted_jd_redirect_url("https://item.yiyaojd.com/100234.html") is True

    ztk_guard = Mock()
    ad_guard = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"), client=ztk_guard)
    for bad_url in ("https://m.tb.cn/h.demo123", "https://mobile.yangkeduo.com/goods.html?goods_id=1", "https://eviljd.com/x"):
        err = ad_guard.resolve_and_convert_universal(bad_url)
        assert err["status"] == "error" and err["error_code"] == "INVALID_JD_URL"
    ztk_guard.convert_universal_material.assert_not_called()

    # 2. 选品池 cooling 期间：background 与 foreground 均不再全局连坐拦截独立池 api_detail + byunionid_get，同时跳过 goods_query 共享池
    enc_id = "G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww"
    api_row = {
        "tao_id": enc_id,
        "title": "维达抽纸整箱24包",
        "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/vinda.jpg",
        "size": "39.9",
        "quanhou_jiage": "29.9",
        "coupon_info_money": "10",
        "tkrate3": "6.5",
        "tkfee3": "1.94",
        "nick": "维达旗舰店",
        "volume": "10000",
        "item_url": f"https://jingfen.jd.com/detail/{enc_id}.html",
    }
    calls = []

    def _cooling_get(url, params=None, **_kw):
        calls.append(url)
        if "api_detail.ashx" in url:
            return reply({"status": 200, "content": [api_row]})
        if "byunionid_get" in url:
            return reply({PROMOTION_ROOT: {"result": json.dumps({"code": 200, "data": {"shortURL": "https://u.jd.com/fgOk1", "clickURL": "https://union-click.jd.com/fgOk1"}})}})
        raise AssertionError(f"Catalog pool must stay skipped while cooling: {url}")

    cli_fg = JdClient(appkey_list=("k1",), union_id="2035679405", http_session=SimpleNamespace(get=_cooling_get))
    cli_fg.mark_catalog_rate_limited(60.0, force=True)
    ad_fg = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"), client=cli_fg)

    with api_caller_context("background"):
        bg_res = ad_fg.resolve_and_convert_universal("https://u.jd.com/oGP72oi")
    assert bg_res["status"] == "success" and bg_res["resolution_kind"] == "product"
    assert bg_res["final_link"] == "https://u.jd.com/fgOk1"

    fg_res = ad_fg.resolve_and_convert_universal("https://u.jd.com/oGP72oi")
    assert fg_res["status"] == "success"
    assert fg_res["resolution_kind"] == "product"
    assert fg_res["final_link"] == "https://u.jd.com/fgOk1"
    assert fg_res["product"]["name"] == "维达抽纸整箱24包"
    assert any("api_detail.ashx" in u for u in calls)
    assert not any("goods_query.ashx" in u or "api_quanwang.ashx" in u or "tkl_piliang.ashx" in u for u in calls)

    # 3. 冷却期且 api_detail 返回 301 + 跳过 goods_query 时，仍经 byunionid_get 保底返回 degraded_product（带 catalog_cooling=True，不打 tkl_piliang/goods_query）
    calls.clear()

    def _cooling_deg_get(url, params=None, **_kw):
        calls.append(url)
        if "api_detail.ashx" in url:
            return reply({"status": 301, "content": "对应宝贝已下架"})
        if "byunionid_get" in url:
            return reply({PROMOTION_ROOT: {"result": json.dumps({"code": 200, "data": {"shortURL": "https://u.jd.com/fgDeg1", "clickURL": "https://union-click.jd.com/fgDeg1", "jCommand": "！CMD1234567890！"}})}})
        raise AssertionError(f"Unexpected catalog call while cooling: {url}")

    cli_deg = JdClient(appkey_list=("k1",), union_id="2035679405", http_session=SimpleNamespace(get=_cooling_deg_get))
    cli_deg.mark_catalog_rate_limited(60.0, force=True)
    ad_deg = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"), client=cli_deg)
    deg_res = ad_deg.resolve_and_convert_universal("https://u.jd.com/oDeg777", uid="u_12345678", promoter_id="999999")
    assert deg_res["status"] == "success"
    assert deg_res["resolution_kind"] == "degraded_product"
    assert deg_res["detail_lookup_failed"] is True
    assert not any("goods_query.ashx" in u or "tkl_piliang" in u or "api_quanwang" in u for u in calls)
    assert deg_res["final_link"] == "https://u.jd.com/fgDeg1"
    assert not any("tkl_piliang" in u or "api_quanwang" in u for u in calls)


def test_jd_batch_get_goods_details_aligns_via_ori_item_id_and_isolates_bad_ids():
    """验证京东 20 批量查卡靠 oriItemId 精准对齐（无视重加密与乱序）、坏 ID 不连坐，并拦截主图指纹 ID。"""
    from models.product import ProductRef

    id_a = "2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w"
    id_b = "YRQafyCEaFDJag78gSKrJagzasiirQ_3bnREOlDJSyoYeHEIH"
    bad_id = "FAKEID00000000000000000000000Q_300000000000000000"
    img_id = "6ae3f40fF84b3cfef_0083320320005d73"

    re_salted_b = "NEWPREFIX_re_salted_BBBBBBBBBBQ_3newSaltB000000000"
    re_salted_a = "NEWPREFIX_re_salted_AAAAAAAAAQ_3newSaltA000000000"

    def _batch_get(url, params=None, **_kw):
        if "open_jing_union_open_goods_query" in url:
            assert params.get("itemIds") == f"{id_a},{id_b},{bad_id}"
            return reply({
                "jd_union_open_goods_query_response": {
                    "result": json.dumps({
                        "code": 200,
                        "totalCount": 2,
                        # 刻意倒序 + 重写 itemId 密文，仅靠 oriItemId 对齐
                        "data": [
                            {
                                "oriItemId": id_b,
                                "itemId": re_salted_b,
                                "skuId": 2002,
                                "skuName": "商品B真品",
                                "materialUrl": f"item.jd.com/{re_salted_b}.html",
                                "imageInfo": {"imageList": [{"url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/b.jpg"}]},
                                "priceInfo": {"price": 59.9, "lowestCouponPrice": 49.9},
                                "couponInfo": {"couponList": []},
                                "commissionInfo": {"commissionShare": 10.0, "couponCommission": 4.99},
                                "shopInfo": {"shopName": "店B"},
                                "comments": 500,
                            },
                            {
                                "oriItemId": id_a,
                                "itemId": re_salted_a,
                                "skuId": 1001,
                                "skuName": "商品A真品",
                                "materialUrl": f"item.jd.com/{re_salted_a}.html",
                                "imageInfo": {"imageList": [{"url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/a.jpg"}]},
                                "priceInfo": {"price": 29.9, "lowestCouponPrice": 19.9},
                                "couponInfo": {"couponList": []},
                                "commissionInfo": {"commissionShare": 8.0, "couponCommission": 1.59},
                                "shopInfo": {"shopName": "店A"},
                                "comments": 800,
                            },
                        ],
                    }),
                },
            })
        if "api_detail.ashx" in url:
            return reply({"status": 301, "content": "对应宝贝已下架"})
        raise AssertionError(f"Unexpected call: {url}")

    hdk_stub = SimpleNamespace(
        get_goods_detail=Mock(return_value={"status": "error", "error_code": "NOT_FOUND"}),
        batch_get_goods_details=Mock(return_value={"status": "success", "data": []}),
    )
    cli = JdClient(appkey_list=("k1",), union_id="2035679405", http_session=SimpleNamespace(get=_batch_get))
    ad = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="k1"),
        client=cli,
        hdk_client=hdk_stub,
    )

    refs = [
        ProductRef("jd", id_a, goods_sign=id_a),
        ProductRef("jd", id_b, goods_sign=id_b),
        ProductRef("jd", bad_id, goods_sign=bad_id),
        ProductRef("jd", img_id, goods_sign=""),
    ]
    res_map = ad.batch_get_goods_details(refs)
    assert res_map[id_a]["status"] == "success"
    assert res_map[id_a]["data"]["name"] == "商品A真品"
    assert res_map[id_b]["status"] == "success"
    assert res_map[id_b]["data"]["name"] == "商品B真品"
    # 混入的坏 ID 被确认下架，不影响 A/B；主图指纹 ID 被就地拒绝
    assert res_map[bad_id]["error_code"] == "PRODUCT_OFF_PROMOTION"
    assert res_map[img_id]["error_code"] == "INVALID_ARGUMENT"


def test_jd_api_detail_independent_from_catalog_cooldown_o19hsre_venue_and_hdk_back_enum():
    """验证 api_detail 独立故障域不被 catalog_cooling 连坐、pro.m.jd.com?sku=数字 判会场、HDK back 对齐合法离散枚举。"""
    from infrastructure.jd_client import resolve_jd_short_url
    from infrastructure.hdk_client import HdkClient

    # 1. api_detail 不受 mark_catalog_rate_limited 影响
    detail_row = {
        "tao_id": "2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w",
        "title": "独立详情商品",
        "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/a.jpg",
        "size": "39.9",
        "quanhou_jiage": "29.9",
        "coupon_info_money": "10",
        "tkrate3": "5",
        "tkfee3": "1.5",
        "item_url": "https://jingfen.jd.com/detail/2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w.html",
    }
    s_det = route_http({"/api_detail.ashx": {"status": 200, "content": [detail_row]}})
    cli_det = JdClient(appkey_list=("k1",), union_id="2035679405", http_session=s_det)
    cli_det.mark_catalog_rate_limited(60.0, force=True)
    assert cli_det.search_goods("可乐")["error_code"] == "RATE_LIMIT"
    assert cli_det.get_goods_detail("https://u.jd.com/oO99YvK")["status"] == "success"

    # 2. o19hsre 落地 pro.m.jd.com?sku=纯数字 时，官方会场 Host 优先级高于参数 ?sku=，判为 is_venue=True
    def _unwrap_get(url, allow_redirects=False, timeout=5.0, **_kw):
        if "u.jd.com/o19hsre" in url:
            return SimpleNamespace(
                status_code=302,
                headers={"Location": "https://pro.m.jd.com/mall/active/2ABC/index.html?sku=100115132399"},
                close=Mock(),
            )
        return SimpleNamespace(status_code=200, headers={}, url=url, close=Mock())

    u_res = resolve_jd_short_url("https://u.jd.com/o19hsre", http_session=SimpleNamespace(get=_unwrap_get))
    assert u_res["status"] == "success"
    assert u_res["data"]["is_venue"] is True
    assert u_res["data"]["sku_id"] == "100115132399"

    # 3. HDK v3/jd_goods_search back 参数严格对齐 (1, 2, 5, 10, 20, 30, 50) 并按原始 page_size 截断
    observed_back = []

    def _hdk_get(url, params=None, timeout=5.0, allow_redirects=False, **_kw):
        observed_back.append(params.get("back"))
        rows = [
            {
                "itemid": f"enc_id_{i}",
                "skuid": f"enc_id_{i}",
                "goodsname": f"商品{i}",
                "itempic": f"https://img14.360buyimg.com/n1/s800x800_jfs/t1/1/2/3/p{i}.jpg",
                "itemprice": "20",
                "itemendprice": "15",
            }
            for i in range(int(params.get("back") or 1))
        ]
        return SimpleNamespace(status_code=200, json=Mock(return_value={"code": 200, "data": rows}), raise_for_status=Mock())

    hdk_cli = HdkClient(api_key="hdk_test_key", http_session=SimpleNamespace(get=_hdk_get))
    sr = hdk_cli.search_goods("抽纸", page=1, page_size=7)
    assert sr["status"] == "success"
    assert observed_back == [10]
    assert len(sr["data"]) == 7


def test_jd_jcommand_direct_lookup_vs_plain_copywriting_skip(monkeypatch):
    """验证完整/带前缀京口令直走独立 api_detail，纯文案无链接无口令绝不乱搜；且 1001 频控识别生效。"""
    enc = "2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w"
    det_row = {
        "tao_id": enc,
        "title": "京口令测试商品",
        "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/jcmd.jpg",
        "size": "59.9",
        "quanhou_jiage": "49.9",
        "coupon_info_money": "10",
        "item_url": f"https://jingfen.jd.com/detail/{enc}.html",
    }
    calls = []

    def _get(url, params=None, **_kw):
        calls.append((url, dict(params or {})))
        if "api_detail.ashx" in url:
            return reply({"status": 200, "content": [det_row]})
        return reply({"status": 301, "content": "无数据"})

    cli = JdClient(appkey_list=("k1",), union_id="2035679405", http_session=SimpleNamespace(get=_get))
    ad = adapter(cli)
    r_jcmd = ad.resolve_product_from_link("17！Z6cyKDGdIYPpSnpI！ CA1234")
    assert r_jcmd["status"] == "success"
    assert r_jcmd["product"]["name"] == "京口令测试商品"
    assert len(calls) == 1 and calls[0][1]["tao_id"] == "！Z6cyKDGdIYPpSnpI！"

    calls.clear()
    r_plain = ad.resolve_product_from_link("【京东】纯推广文案无链接无口令")
    assert r_plain["status"] == "success" and r_plain["product"] is None
    assert calls == []

    # 验证 1001 显式频控关键字触发 RATE_LIMIT 与冷却
    cli_rl = JdClient(
        appkey_list=("k1",),
        union_id="2035679405",
        http_session=SimpleNamespace(get=Mock(return_value=reply({"status": 301, "content": "错误码1001调用受限"}))),
    )
    monkeypatch.setattr("infrastructure.jd_client.is_real_requests_session", lambda _s: True)
    rl_res = cli_rl.query_union_goods(keyword="可乐")
    assert rl_res["error_code"] == "RATE_LIMIT"
    assert rl_res.get("ztk_explicit_rate_limit") is True
    assert cli_rl.is_catalog_cooling() is True


def test_jd_shortlink_2001917_fallback_to_clean_jingfen_and_venue_markers_and_health(monkeypatch):
    """验证：(1) u.jd.com 报 2001917 且已拿纯净 jingfen 卡时自动回退重转；(2) hour/mcard/lives/plus 登录跳转判会场；(3) byunionid_get 内层报错不虚记 success。"""
    from infrastructure.api_governance import GLOBAL_UPSTREAM_HEALTH
    from infrastructure.jd_client import resolve_jd_short_url

    enc = "2JQRZRHbk3IIkCiQD6m6IkSVn4BiaQ_3SD7sm6WTHRz0i9pLt"
    clean_jf = f"https://jingfen.jd.com/detail/{enc}.html"
    by_calls = []

    def _get(url, params=None, **_kw):
        p = dict(params or {})
        if "api_detail.ashx" in url:
            return reply({
                "status": 200,
                "content": [{
                    "tao_id": enc,
                    "title": "麻六记酸辣粉178g*6",
                    "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/254098/2/4826/191226/676e23c3F2d243c6d/6d28386d3896746b.jpg",
                    "size": "36.9",
                    "quanhou_jiage": "36.9",
                    "tkrate3": "2",
                    "tkfee3": "0.74",
                    "item_url": clean_jf,
                }],
            })
        if "byunionid_get.ashx" in url:
            by_calls.append(p.get("materialId"))
            if p.get("materialId") == "https://u.jd.com/o61KxfW":
                return reply(wrapped(PROMOTION_ROOT, {"code": 2001917, "message": "优惠券不可购买该商品"}, field="result", string=True))
            if p.get("materialId") == "https://u.jd.com/bad400":
                return reply(wrapped(PROMOTION_ROOT, {"code": 400, "message": "参数错误"}, field="result", string=True))
            if p.get("materialId") == clean_jf:
                return reply(wrapped(
                    PROMOTION_ROOT,
                    {"code": 200, "data": {"shortURL": "https://u.jd.com/cleanOk", "clickURL": "https://union-click.jd.com/ok", "jCommand": "！CLEAN！"}},
                    field="result",
                    string=True,
                ))
        raise AssertionError(f"unexpected call: {url} {p}")

    GLOBAL_UPSTREAM_HEALTH.reset()
    monkeypatch.setattr("infrastructure.jd_client.is_real_requests_session", lambda _s: True)
    cli = JdClient(appkey_list=("k1",), union_id="2035679405", http_session=SimpleNamespace(get=_get))
    ad = adapter(cli)
    res = ad.resolve_and_convert_universal("https://u.jd.com/o61KxfW")
    assert res["status"] == "success"
    assert res["resolution_kind"] == "product"
    assert res["final_link"] == "https://u.jd.com/cleanOk"
    assert res["product"]["name"] == "麻六记酸辣粉178g*6"
    assert by_calls == ["https://u.jd.com/o61KxfW", clean_jf]
    bad400_res = cli.convert_universal_material("https://u.jd.com/bad400", "77")
    assert bad400_res["status"] == "error"

    snap = GLOBAL_UPSTREAM_HEALTH.snapshot_local()
    by_stat = next(
        (ep for ep in snap["endpoints"] if ep["provider"] == "ztk_jd" and "byunionid_get" in ep["operation"]),
        None,
    )
    assert by_stat is not None
    assert by_stat["total_calls"] == 3
    assert by_stat["success_calls"] == 1
    assert by_stat["degraded_calls"] == 1
    assert by_stat["error_calls"] == 1

    # 官方非单品活动域名与 plogin+plus.m.jd.com 正向判会场
    for dest in (
        "https://hour.jd.com/aggregationChannelPub/channel?channelId=13376",
        "https://mcard.jd.com/?cardIdEnc=abc",
        "https://lives.jd.com/?rid=14633",
        "https://plogin.m.jd.com/login/nopassword?appid=300&returnurl=https%3A%2F%2Fplus.m.jd.com%2Findex",
    ):
        sess = SimpleNamespace(
            get=lambda url, _dest=dest, **_kw: SimpleNamespace(
                status_code=302,
                headers={"Location": _dest},
                close=Mock(),
            ) if "u.jd.com" in url else SimpleNamespace(status_code=200, headers={}, url=url, close=Mock())
        )
        u_res = resolve_jd_short_url("https://u.jd.com/vtest", http_session=sess)
        assert u_res["status"] == "success"
        assert u_res["data"]["is_venue"] is True, dest









