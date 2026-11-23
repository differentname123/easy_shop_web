"""Taobao / HDK / ZTK protocol boundaries. All HTTP responses are offline substitutes."""
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.tb_adapter import TaobaoAdapter
from infrastructure import tb_client
from infrastructure.hdk_client import HdkClient, HDK_SALES_URL
from infrastructure.ztk_client import ZtkClient, ZTK_ORDER_URL


def reply(body, status=200, text=""):
    return SimpleNamespace(status_code=status, text=text, json=Mock(return_value=body), raise_for_status=Mock())


def official_item(sign="opaque-A", input_id="101", **extra):
    raw = {
        "item_id": sign, "item_basic_info": {"title": "官方标题", "pict_url": "//official/image.jpg", "shop_title": "官方店"},
        "price_promotion_info": {"reserve_price": "120", "zk_final_price": "100", "final_promotion_price": "80"},
        "publish_info": {"income_rate": "15.5", "income_info": {"commission_amount": "12.40"}},
    }
    if input_id is not None:
        raw["input_item_iid"] = input_id
    raw.update(extra)
    return tb_client.clean_tb_goods_item(raw)


def make_adapter(official=None, hdk=None, ztk=None, **settings):
    return TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True), official_client=official or Mock(),
                         hdk_client=hdk or Mock(), ztk_client=ztk or Mock(), **settings)


def test_top_signature_preserves_existing_serialization_and_transport():
    http = SimpleNamespace(post=Mock(return_value=reply({"response": {}})))
    result = tb_client.call_tb_api("key", "secret", "taobao.test", {"flag": True, "x": {"中文": 1}, "skip": None}, http_session=http)
    assert result["status"] == "success"
    args, kwargs = http.post.call_args
    assert args == (tb_client.TOP_GATEWAY_URL,)
    assert kwargs["allow_redirects"] is False
    params = kwargs["data"]
    assert params["flag"] == "true" and params["x"] == '{"中文":1}' and "skip" not in params
    unsigned = {k: v for k, v in params.items() if k != "sign"}
    text = "secret" + "".join(k + str(unsigned[k]) for k in sorted(unsigned)) + "secret"
    assert params["sign"] == hashlib.md5(text.encode()).hexdigest().upper()


@pytest.mark.parametrize("body", [[], None, "not-json"])
def test_top_rejects_nonobject_response(body):
    http = SimpleNamespace(post=Mock(return_value=reply(body)))
    assert tb_client.call_tb_api("k", "s", "m", http_session=http)["error_code"] == "PROTOCOL_ERROR"


def test_top_transport_error_does_not_disclose_signed_url(caplog):
    secret_url = "https://gateway/?session=private-session&app_secret=private-key"
    http = SimpleNamespace(post=Mock(side_effect=RuntimeError(secret_url)))
    response = tb_client.call_tb_api("k", "s", "m", http_session=http)
    assert response["error_code"] == "NETWORK_ERROR"
    assert "private" not in str(response) + caplog.text


def test_clean_goods_does_not_guess_coupon_id_or_missing_commercial_fields():
    item = official_item(input_id=None)
    assert item["goods_sign"] == "opaque-A" and item["goods_id"] == ""
    assert item["price"] == 80 and item["original_price"] == 120
    assert item["coupon_amount"] == 0 and item["has_coupon"] is False
    assert item["commission_rate"] == 15.5 and item["commission_amount"] == 12.4
    raw = item["raw_item"]
    raw["publish_info"] = {"income_info": {"commission_rate": "5500"}}
    raw["item_basic_info"].pop("shop_title")
    raw["price_promotion_info"]["final_promotion_path_list"] = {"final_promotion_path_map_data": [
        {"promotion_title": "店铺优惠券", "promotion_fee": "10"},
        {"promotion_title": "跨店满减", "promotion_fee": "30"},
    ]}
    cleaned = tb_client.clean_tb_goods_item(raw)
    assert cleaned["coupon_amount"] == 10 and cleaned["commission_rate"] == 0
    assert cleaned["mall_name"] == ""


@pytest.mark.parametrize("echoed,expected_id", [("opaque-request-sign", ""), ("101", "101"), ("opaque-A", "")])
def test_clean_goods_opaque_echo_is_not_an_original_goods_id(echoed, expected_id):
    item = official_item(input_id=echoed)
    assert item["goods_id"] == expected_id
    assert item["input_item_iid"] == echoed and item["goods_sign"] == "opaque-A"


def test_official_detail_batch_parameter_and_strict_shape():
    api = Mock(return_value={"status": "success", "data": {"tbk_item_info_upgrade_get_response": {"results": {"tbk_item_detail": []}}}})
    assert tb_client.get_tb_goods_details("key", "secret", ["001", "opaque"], _api_call=api)["data"] == []
    assert api.call_args.kwargs["biz_params"]["item_id"] == "001,opaque"
    assert api.call_args.kwargs["method"] == "taobao.tbk.item.info.upgrade.get"
    assert tb_client.get_tb_goods_details("k", "s", list(range(21)), _api_call=api)["status"] == "error"
    api.return_value = {"status": "success", "data": {}}
    assert tb_client.get_tb_goods_details("k", "s", ["1"], _api_call=api)["error_code"] == "PROTOCOL_ERROR"


def test_settings_capabilities_are_independent_and_disabled_is_instance_state():
    a = TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123"))
    assert a.active and a.capabilities["detail"] and a.capabilities["search"] and a.capabilities["recommend"]
    assert not a.capabilities["orders"]
    a_no_az = TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s"))
    assert a_no_az.active and a_no_az.capabilities["detail"]
    assert not a_no_az.capabilities["search"] and not a_no_az.capabilities["recommend"] and not a_no_az.capabilities["orders"]
    assert a_no_az.get_recommend_goods()["error_code"] == "CONFIG_ERROR"
    assert a.fetch_increment_orders(1, 2)["order_list"] == []
    assert not TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=False)).active


@pytest.mark.parametrize("channel,sale_type", [(5, 1), (1, 2)])
def test_rank_mapping_official_order_missing_items_and_cursor(channel, sale_type):
    hdk = Mock()
    hdk.get_ranked_ids.return_value = {"status": "success", "item_ids": ["101", "102", "103"], "source_count": 3,
                                      "has_more": True, "next_cursor": {"min_id": "next"}, "total_count": 100}
    official = Mock()
    official.get_goods_details.return_value = {"status": "success", "data": [official_item("C", "103"), official_item("A", "101")]}
    adapter = make_adapter(official, hdk)
    result = adapter.get_recommend_goods(channel_type=channel, limit=3)
    assert hdk.get_ranked_ids.call_args.args == (sale_type,)
    assert [i["goods_sign"] for i in result["data"]] == ["A", "C"]
    assert result["next_offset"] == 3 and result["has_more"]
    cursor = result["next_cursor"]
    hdk.get_ranked_ids.return_value = {**hdk.get_ranked_ids.return_value, "next_cursor": {"min_id": "next-2"}}
    second = adapter.get_recommend_goods(channel_type=channel, offset=3, limit=3, cursor=json.dumps(cursor))
    assert second["next_offset"] == 6
    assert hdk.get_ranked_ids.call_args.kwargs["cursor"] == {"min_id": "next"}
    assert adapter.get_recommend_goods(channel_type=6)["status"] == "error"
    assert adapter.get_recommend_goods(channel_type=channel, offset=0, cursor=cursor)["status"] == "error"
    assert adapter.get_recommend_goods(channel_type=channel, cursor="{bad")["status"] == "error"


def test_rank_batches_20_and_all_missing_page_still_advances():
    hdk, official = Mock(), Mock()
    ids = [str(i) for i in range(101, 146)]
    hdk.get_ranked_ids.return_value = {"status": "success", "item_ids": ids, "has_more": True, "next_cursor": {"min_id": "46"}}
    official.get_goods_details.return_value = {"status": "success", "data": []}
    result = make_adapter(official, hdk).get_recommend_goods(limit=45)
    assert result["data"] == [] and result["has_more"] and result["next_offset"] == 45
    assert [len(call.args[0]) for call in official.get_goods_details.call_args_list] == [20, 20, 5]
    official.get_goods_details.return_value = {"status": "error", "error_code": "NETWORK_ERROR"}
    failed = make_adapter(official, hdk).get_recommend_goods(limit=45)
    assert failed["status"] == "error" and "next_offset" not in failed


def test_public_dtos_strip_personal_links_and_keep_sign_only_identity():
    item = official_item(input_id=None)
    item.update(coupon_share_url="https://private/?relation_id=personal", click_url="https://private/", goods_id="")
    official = Mock()
    official.search_goods.return_value = {"status": "success", "data": [item]}
    official.get_goods_details.return_value = {"status": "success", "data": [item]}
    adapter = make_adapter(official)
    search = adapter.search_goods("词", uid="uid")
    detail = adapter.get_goods_detail(goods_sign="opaque-A", uid="uid")
    for dto in (search["data"][0], detail["data"]):
        assert dto["goods_id"] == "opaque-A" and dto["goods_sign"] == "opaque-A"
        assert "private" not in json.dumps(dto)
        assert "raw_item" not in dto and "coupon_share_url" not in dto
    assert adapter.get_goods_detail("999")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"


def test_authority_missing_session_is_error_and_exact_external_id_is_required():
    api = Mock()
    assert tb_client.check_publisher_relation("k", "s", "uid", _api_call=api)["error_code"] == "CONFIG_ERROR"
    api.assert_not_called()
    def response(data):
        return {"status": "success", "data": {"tbk_sc_publisher_info_get_response": {"data": data}}}
    api.return_value = response({"total_count": 0})
    assert tb_client.check_publisher_relation("k", "s", "uid", "session", api)["is_authorized"] is False
    api.return_value = response({"total_count": 2, "inviter_list": {"map_data": [{"relation_id": "9007199254740993", "external_id": "uid"}]}})
    assert tb_client.check_publisher_relation("k", "s", "uid", "session", api)["error_code"] == "INCOMPLETE_IDENTITY"
    api.return_value = response({"inviter_list": {"map_data": [{"external_id": "another-user", "relation_id": "9007199254740993"}]}})
    assert tb_client.check_publisher_relation("k", "s", "uid", "session", api)["error_code"] == "IDENTITY_MISMATCH"
    api.return_value = response({"inviter_list": {"map_data": [{"external_id": "uid", "relation_id": "9007199254740993"}]}})
    result = tb_client.check_publisher_relation("k", "s", "uid", "session", api)
    assert result["identity"] == {"external_id": "uid", "relation_id": "9007199254740993"}
    assert result["bind"] == 1
    api.return_value = {"status": "error", "error_code": "invalid-session", "error_msg": "session invalid"}
    assert tb_client.check_publisher_relation("k", "s", "uid", "session", api)["status"] == "error"


def test_invitation_encodes_each_uid_and_official_invitecode():
    result = tb_client.generate_tb_authority_url("k", "s", "user &中文", inviter_code="code")
    assert result["auth_url"].startswith("https://mos.m.taobao.com/inviter/register?")
    assert "externalId=user+%26%E4%B8%AD%E6%96%87" in result["auth_url"]
    api = Mock(return_value={"status": "success", "data": {"tbk_sc_invitecode_get_response": {"data": {"inviter_code": "official-code"}}}})
    assert "official-code" in tb_client.generate_tb_authority_url("k", "s", "u", "session", _api_call=api)["auth_url"]
    assert api.call_args.kwargs["session_key"] == "session"


def promotion_official():
    official = Mock()
    target = official_item()
    target["coupon_share_url"] = "https://uland.taobao.com/coupon?official=1"
    official.get_goods_details.return_value = {"status": "success", "data": [target]}
    official.search_goods.return_value = {"status": "success", "data": [official_item("unrelated", "999"), target]}
    official.create_tpwd.return_value = {"status": "success", "password_simple": "￥真实口令￥"}
    return official


def test_precision_promotion_two_users_are_separated_and_real_token_preserved():
    official = promotion_official()
    ztk = Mock()
    ztk.convert_channel_url.side_effect = lambda target, rel, pid, **kwargs: {
        "status": "success", "final_link": f"https://uland.taobao.com/channel_{rel}",
        "tpwd": f"￥口令_{rel}￥", "url": f"https://uland.taobao.com/channel_{rel}",
    }
    adapter = make_adapter(official, ztk=ztk)
    for uid, relation in (("u1", "r1"), ("u2", "r2")):
        result = adapter.convert_promotion_url("101", uid=uid, identity={"external_id": uid, "relation_id": relation})
        assert result["status"] == "success" and result["promotion_token"] == f"￥口令_{relation}￥"
        assert result["token_type"] == "tpwd" and result["attribution_status"] == "verified"
        assert result["relation_id"] == relation
        assert result["cacheable"] is True and "expires_at" not in result
        assert ztk.convert_channel_url.call_args.args[1] == relation
    official.search_goods.assert_not_called()
    result = adapter.convert_promotion_url("101")
    assert result["attribution_status"] == "anonymous"
    assert result["promotion_token"] == "￥真实口令￥"
    assert official.search_goods.called
    assert "relation_id" not in official.search_goods.call_args.kwargs


def test_missing_identity_never_uses_shared_relation_and_wrong_product_rejected():
    official = promotion_official()
    adapter = make_adapter(official)
    result = adapter.convert_promotion_url("101", uid="u", relation_id="shared-test-relation")
    assert result["status"] == "error" and result["error_code"] == "AUTH_REQUIRED" and result["need_auth"]
    official.get_goods_details.assert_not_called()
    official.search_goods.assert_not_called()
    assert adapter.convert_promotion_url("101", uid="u", identity={"external_id": "wrong", "relation_id": "r"})["status"] == "error"
    official.create_tpwd.reset_mock()
    official.search_goods.return_value = {"status": "success", "data": [official_item("wrong", "999")]}
    assert adapter.convert_promotion_url("101")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"
    official.create_tpwd.assert_not_called()


def test_precision_accepts_verified_canonical_sign_without_search_alias():
    official = promotion_official()
    candidate = official_item(input_id=None)
    candidate["coupon_share_url"] = "https://uland.taobao.com/official"
    official.search_goods.return_value = {"status": "success", "data": [candidate]}
    result = make_adapter(official).convert_promotion_url("101")
    assert result["status"] == "success" and result["goods_sign"] == "opaque-A"
    candidate["goods_id"] = "999"
    assert make_adapter(official).convert_promotion_url("101")["error_code"] == "PRODUCT_IDENTITY_MISMATCH"


def test_source_expiry_is_not_invented_and_empty_token_is_error():
    official = promotion_official()
    official.create_tpwd.return_value = {"status": "success", "model": "￥token￥"}
    result = make_adapter(official).convert_promotion_url("101")
    assert result["status"] == "success" and result["promotion_token"] == "￥token￥"
    assert "expires_at" not in result and result["cacheable"] is True
    official.create_tpwd.return_value = {"status": "success"}
    assert make_adapter(official).convert_promotion_url("101")["status"] == "error"


def test_hdk_transmits_only_rank_parameters_and_discards_third_party_content():
    http = SimpleNamespace(get=Mock(return_value=reply({"code": 1, "data": [{"itemid": "001", "title": "不可信标题", "price": 0.01}, {}], "min_id": 3})))
    result = HdkClient("key", http).get_ranked_ids(1, limit=2)
    assert result["item_ids"] == ["001", ""] and result["source_count"] == 2
    assert "不可信" not in str(result)
    assert result["next_cursor"] == {"min_id": "3"}
    assert http.get.call_args.args == (HDK_SALES_URL,)
    assert http.get.call_args.kwargs["allow_redirects"] is False
    http.get.return_value = reply({"code": 1, "data": [{"itemid": "1"}]})
    assert HdkClient("key", http).get_ranked_ids(1, limit=1)["error_code"] == "PAGINATION_ERROR"
    assert HdkClient("", http).get_ranked_ids(1)["error_code"] == "CONFIG_ERROR"


def test_hdk_discrete_page_size_does_not_send_unsupported_ui_limit():
    http = SimpleNamespace(get=Mock(return_value=reply({"code": 1, "data": [], "min_id": 0})))
    client = HdkClient("key", http)
    assert client.get_ranked_ids(1, limit=45)["status"] == "success"
    assert http.get.call_args.kwargs["params"]["back"] == 20
    assert client.get_ranked_ids(1, limit=3)["status"] == "success"
    assert http.get.call_args.kwargs["params"]["back"] == 2
    assert http.get.call_args.kwargs["params"]["cid"] == 0


def test_hdk_five_page_cap_and_exhausted_page_return_has_more_false():
    # 第 5 页 (min_id=5, back=20) 返回 20 条且 min_id=6 时，直接标记 has_more=False 终止翻页
    rows_20 = [{"itemid": str(i)} for i in range(1, 21)]
    http = SimpleNamespace(get=Mock(return_value=reply({"code": 1, "data": rows_20, "min_id": 6})))
    client = HdkClient("key", http)
    page5 = client.get_ranked_ids(1, limit=20, cursor={"min_id": "5"})
    assert page5["status"] == "success"
    assert len(page5["item_ids"]) == 20
    assert page5["has_more"] is False
    assert page5["next_cursor"] is None

    # 若调用方仍请求 min_id=6，好单库返回 code=0, min_id=0, data=[] ("数据获取完毕！") 时视为正常空尾页而非业务报错
    http.get.return_value = reply({"code": 0, "msg": "数据获取完毕！", "data": [], "min_id": 0})
    page6 = client.get_ranked_ids(1, limit=20, cursor={"min_id": "6"})
    assert page6["status"] == "success"
    assert page6["item_ids"] == []
    assert page6["has_more"] is False
    assert page6["next_cursor"] is None

    # 真正的上游错误（如 key 错误或超时，min_id 为 None）仍返回 UPSTREAM_ERROR
    http.get.return_value = reply({"code": 0, "msg": "网络超时，页面加载失败。稍后再试~", "data": [], "min_id": None})
    err = client.get_ranked_ids(1, limit=20, cursor={"min_id": "1"})
    assert err["status"] == "error"
    assert err["error_code"] == "UPSTREAM_ERROR"


def order_response(orders=None, has_next=False, position="", root="tbk_sc_order_details_get_response"):
    return {root: {"data": {"results": {"publisher_order_dto": orders or []},
            "has_next": has_next, "position_index": position}}}


@pytest.mark.parametrize("root", ["tbk_sc_order_details_get_response", "tbk_order_details_get_response"])
def test_ztk_direct_json_cursor_shanghai_window_and_query_contract(root):
    http = SimpleNamespace(get=Mock(return_value=reply(order_response([{"trade_id": "001"}], True, "opaque-cursor", root))))
    client = ZtkClient("appkey", "sid", http)
    result = client.query_orders(0, 1200, page_size=100)
    assert result["order_list"] == [{"trade_id": "001"}]
    assert result["next_cursor"] == {"page_no": 2, "position_index": "opaque-cursor"}
    args, kwargs = http.get.call_args
    assert args == (ZTK_ORDER_URL,)
    params = kwargs["params"]
    assert params["query_type"] == 4 and params["order_scene"] == 2
    assert params["start_time"] == "1970-01-01 08:00:00" and params["jump_type"] == 1
    assert params["signurl"] == 2  # Signed-URL mode must request HTTPS, not the legacy HTTP variant.
    client.query_orders(0, 1200, cursor=result["next_cursor"])
    assert http.get.call_args.kwargs["params"]["page_no"] == 2
    assert http.get.call_args.kwargs["params"]["position_index"] == "opaque-cursor"
    before = http.get.call_count
    for kwargs in ({"page": 2}, {"page_size": 101}, {"page": 101}):
        assert client.query_orders(0, 1200, **kwargs)["status"] == "error"
    assert client.query_orders(0, 1201)["status"] == "error"
    assert http.get.call_count == before


@pytest.mark.parametrize("envelope", [lambda url: url, lambda url: {"url": url}, lambda url: {"code": 200, "data": url}])
@pytest.mark.parametrize("method,root", [
    ("taobao.tbk.sc.order.details.get", "tbk_sc_order_details_get_response"),
    ("taobao.tbk.order.details.get", "tbk_order_details_get_response"),
])
def test_ztk_supported_signed_url_modes(envelope, method, root):
    url = f"https://eco.taobao.com/router/rest?method={method}&sign=private"
    http = SimpleNamespace(get=Mock(side_effect=[reply(envelope(url)), reply(order_response(root=root))]))
    result = ZtkClient("key", "sid", http).query_orders(0, 100)
    assert result["status"] == "success" and result["order_list"] == []
    assert http.get.call_args_list[1].args == (url,)
    assert all(call.kwargs["allow_redirects"] is False for call in http.get.call_args_list)


@pytest.mark.parametrize("gateway", ["http://eco.taobao.com/router/rest", "https://attacker.test/router/rest", "https://eco.taobao.com.attacker.test/router/rest", "https://u:p@eco.taobao.com/router/rest", "https://eco.taobao.com:10001/router/rest", "https://eco.taobao.com/redirect"])
def test_ztk_rejects_untrusted_or_nonhttps_second_hop(gateway):
    url = gateway + "?method=taobao.tbk.sc.order.details.get&sign=private"
    http = SimpleNamespace(get=Mock(return_value=reply({"url": url})))
    assert ZtkClient("k", "s", http).query_orders(0, 100)["error_code"] == "UNTRUSTED_GATEWAY"
    assert http.get.call_count == 1


@pytest.mark.parametrize("body", [{}, {"code": -1, "msg": "error"}, {"error_response": {"code": 1}}, {"tbk_sc_order_details_get_response": {"data": {}}}])
def test_ztk_error_or_unknown_shapes_never_become_empty_success(body):
    http = SimpleNamespace(get=Mock(return_value=reply(body)))
    assert ZtkClient("k", "s", http).query_orders(0, 100)["status"] == "error"


@pytest.mark.parametrize("query", [
    "sign=private",
    "method=&sign=private",
    "method=taobao.tbk.tpwd.create&sign=private",
    "method=taobao.tbk.sc.order.details.get&method=taobao.tbk.order.details.get&sign=private",
    "method=taobao.tbk.sc.order.details.get&method=&sign=private",
    "method=taobao.tbk.sc.order.details.get&%6dethod=taobao.tbk.tpwd.create&sign=private",
])
def test_ztk_signed_gateway_requires_exactly_one_readonly_order_method(query):
    http = SimpleNamespace(get=Mock(return_value=reply({"url": "https://eco.taobao.com/router/rest?" + query})))
    assert ZtkClient("k", "s", http).query_orders(0, 100)["error_code"] == "UNTRUSTED_GATEWAY"
    assert http.get.call_count == 1


@pytest.mark.parametrize("body", [
    order_response(root="unknown_order_get_response"),
    {**order_response(), **order_response(root="tbk_order_details_get_response")},
])
def test_ztk_rejects_unknown_or_ambiguous_order_roots_even_with_valid_data(body):
    http = SimpleNamespace(get=Mock(return_value=reply(body)))
    assert ZtkClient("k", "s", http).query_orders(0, 100)["error_code"] == "PROTOCOL_ERROR"
    assert http.get.call_count == 1


def test_ztk_missing_next_cursor_redirect_or_network_are_explicit_errors():
    http = SimpleNamespace(get=Mock(return_value=reply(order_response(has_next=True))))
    assert ZtkClient("k", "s", http).query_orders(0, 100)["error_code"] == "PAGINATION_ERROR"
    http.get.return_value = reply({}, status=302)
    assert ZtkClient("k", "s", http).query_orders(0, 100)["error_code"] == "UNSAFE_REDIRECT"
    http.get.side_effect = RuntimeError("https://host/?appkey=secret")
    result = ZtkClient("k", "s", http).query_orders(0, 100)
    assert result["error_code"] == "NETWORK_ERROR" and "secret" not in str(result)
    assert ZtkClient().query_orders(0, 100)["error_code"] == "CONFIG_ERROR"


@pytest.mark.parametrize("status,settled,invalid", [(12, False, False), (14, False, False), (3, True, False), (13, False, True), (99, False, False)])
def test_order_suborder_identity_commission_and_invalid_not_refund(status, settled, invalid):
    order = {"trade_id": "0001", "trade_parent_id": "parent", "tk_status": status,
             "relation_id": "9007199254740993", "pub_share_pre_fee": "12.34", "pub_share_fee": "8.76",
             "alipay_total_price": "100.00", "refund_tag": 1, "item_num": 2,
             "modified_time": "2026-11-04 12:00:00", "tk_paid_time": "2026-11-04 11:00:00"}
    result = make_adapter().normalize_order(order)
    assert result["order_sn"] == "0001" and result["trade_parent_id"] == "parent"
    assert result["is_settled"] is settled and result["is_invalid"] is invalid and not result["is_refund"]
    assert result["attribution_identity"] == {"relation_id": "9007199254740993"}
    assert result["estimated_commission_yuan"] == 12.34 and result["settled_commission_yuan"] == 8.76
    assert result["promotion_amount_yuan"] == (8.76 if settled else 12.34)
    assert result["order_modify_at"] == result["source_updated_at"] > result["order_pay_time"]
    assert "uid" not in result


def test_settled_missing_fee_never_uses_payment_estimate_and_unknown_identity_not_guessed():
    result = make_adapter().normalize_order({"trade_id": "1", "tk_status": 3, "pub_share_pre_fee": "50"})
    assert result["promotion_amount_yuan"] == 0 and result["settled_commission_yuan"] is None
    assert result["commission_basis"] == "missing_settlement" and result["attribution_identity"] == {}


def test_ztk_convert_channel_url_success_and_error_contract():
    http = SimpleNamespace(get=Mock(return_value=reply({
        "status": 200,
        "content": [{
            "coupon_click_url": "//uland.taobao.com/coupon/edetail?e=coupon123",
            "title": "测试高佣商品",
            "quanhou_jiage": "29.90",
            "tkrate3": "20.5",
            "tkfee3": "6.12",
            "tkl": "￥ZTKTWOINONE￥"
        }]
    })))
    client = ZtkClient("my_appkey", "my_sid", http)
    res = client.convert_channel_url("https://uland.taobao.com/item/edetail?id=123", "999888", "mm_1_2_3")
    assert res["status"] == "success"
    assert res["final_link"] == "https://uland.taobao.com/coupon/edetail?e=coupon123"
    assert res["tpwd"] == "￥ZTKTWOINONE￥"
    assert res["final_price"] == "29.90"
    assert res["commission_rate"] == "20.5"
    assert res["is_channel"] is True
    assert res["relation_id"] == "999888"

    # Missing credentials
    assert ZtkClient("", "").convert_channel_url("url", "rid", "pid")["error_code"] == "CONFIG_ERROR"
    # Invalid argument
    assert client.convert_channel_url("", "rid", "pid")["error_code"] == "INVALID_ARGUMENT"
    # Upstream invalid relationId (status 301 and content string)
    http.get.return_value = reply({'content': '很抱歉！无效的relationId！', 'status': 301})
    auth_err = client.convert_channel_url("url", "bad_rid", "pid")
    assert auth_err["error_code"] == "AUTH_REQUIRED"
    assert auth_err["need_auth"] is True
    assert "很抱歉！无效的relationId！" in auth_err["error_msg"]

    # Upstream non-200 or empty content
    http.get.return_value = reply({"status": 500, "msg": "转链异常"})
    assert client.convert_channel_url("url", "rid", "pid")["error_code"] == "UPSTREAM_ERROR"
    # Network exception
    http.get.side_effect = RuntimeError("network failed")
    assert client.convert_channel_url("url", "rid", "pid")["error_code"] == "NETWORK_ERROR"


def test_ztk_invalid_relation_id_triggers_fallback_without_revoking_saved_auth(monkeypatch):
    from services.promote_service import PromoteService
    from services.user_service import UserService

    official = promotion_official()
    ztk = ZtkClient("ztk_key", "ztk_sid")
    ztk.convert_channel_url = Mock(return_value={
        "status": "error",
        "error_code": "AUTH_REQUIRED",
        "error_msg": "折淘客渠道授权失效: 很抱歉！无效的relationId！",
        "need_auth": True,
    })
    adapter = make_adapter(official, ztk=ztk)
    repo, user_repo = Mock(), Mock()
    repo.get_promotion_link_cache.return_value = None
    user_repo.get_user_by_uid.return_value = {
        "uid": "u_12345678",
        "auth_status": {
            "taobao": {
                "is_authorized": True,
                "updated_at": time.time(),
                "identity": {"external_id": "u_12345678", "relation_id": "bad_rid"},
            }
        },
    }
    import adapters as adapters_pkg
    monkeypatch.setitem(adapters_pkg._ADAPTER_REGISTRY, "taobao", adapter)

    result = PromoteService(repo, UserService(user_repo)).convert_link("taobao", "101", uid="u_12345678")

    # 策略 B: 转链失败不抹除数据库授权信息，防止网络抖动误伤推手
    user_repo.update_auth_status.assert_not_called()
    assert result["status"] == "success"
    assert result["need_auth"] is True and result["is_fallback"] is True
    assert result["final_link"].startswith("https://")
    assert result["promotion_token"] == "￥真实口令￥"
    assert "渠道授权未生效或已失效" in result["auth_msg"]
    assert official.search_goods.called


def test_taobao_detail_extracts_verified_specs_from_official_item():
    cleaned = tb_client.clean_tb_goods_item({
        "item_id": "889900",
        "input_item_iid": "889900",
        "item_basic_info": {
            "title": "维达抽纸整箱",
            "pict_url": "//official/vinda.jpg",
            "brand_name": "Vinda/维达",
            "category_name": "纸品/湿巾",
            "shop_title": "维达官方旗舰店",
            "provcity": "广东 江门",
        },
        "price_promotion_info": {
            "reserve_price": "39.9",
            "zk_final_price": "39.9",
            "final_promotion_price": "29.9",
            "final_promotion_path_list": {"final_promotion_path_map_data": [
                {"promotion_title": "店铺优惠券", "promotion_fee": "10.0"},
            ]},
        },
        "publish_info": {"income_rate": "10", "income_info": {"commission_amount": "2.99"}},
    })
    official = Mock()
    official.get_goods_details.return_value = {"status": "success", "data": [cleaned]}
    adapter = make_adapter(official)
    res = adapter.get_goods_detail("889900", goods_id="889900")
    assert res["status"] == "success"
    assert res["data"]["price"] == 29.9
    assert res["data"]["original_price"] == 39.9
    assert res["data"]["coupon_amount"] == 10.0
    assert res["data"]["specs"] == [
        {"label": "商品品牌", "value": "Vinda/维达"},
        {"label": "商品类目", "value": "纸品/湿巾"},
        {"label": "店铺名称", "value": "维达官方旗舰店"},
        {"label": "发货地", "value": "广东 江门"},
    ]


def test_ztk_client_multi_key_and_sid_pairing_failover():
    http = SimpleNamespace(get=Mock(side_effect=[
        reply({"code": 301, "msg": "appkey日调用次数超限"}),
        reply(order_response([{"trade_id": "t1"}])),
    ]))
    client = ZtkClient("key1", "sid1", http, appkey_list=("key1", "key2"))
    res = client.query_orders(0, 100)
    assert res["status"] == "success" and len(res["order_list"]) == 1
    assert client.appkey == "key2" and http.get.call_count == 2

    seen_pairs = []

    def _get(url, params=None, **_kwargs):
        seen_pairs.append((params["appkey"], params["sid"]))
        if params["appkey"] == "ak-1":
            return reply({"status": 400, "content": "appkey未授权或已到期"})
        return reply({
            "status": 200,
            "content": [{"tao_id": "888777666", "coupon_click_url": "https://uland.taobao.com/coupon/edetail?e=test", "tkl": "￥AbCdEf123￥"}],
        })

    paired = ZtkClient(appkey_list=["ak-1", "ak-2", "ak-3"], sid_list=["sid-1", "sid-2", "sid-3"], http_session=SimpleNamespace(get=_get))
    conv = paired.convert_channel_url("https://item.taobao.com/item.htm?id=888777666", relation_id="2999888777", pid="mm_1_2_3")
    assert conv["status"] == "success" and conv["raw"]["tao_id"] == "888777666" and conv["tpwd"] == "￥AbCdEf123￥"
    assert seen_pairs == [("ak-1", "sid-1"), ("ak-2", "sid-2")]


def test_hdk_client_multi_key_pool_failover():
    seen_keys = []

    def _get(url, params=None, **_kwargs):
        key = params["apikey"]
        seen_keys.append(key)
        if key == "hdk-1":
            return reply({"code": 0, "msg": "apikey请求超限"})
        if key == "hdk-2":
            raise RuntimeError("transient socket timeout")
        return reply({"code": 1, "data": [{"itemid": "555666"}], "min_id": 0})

    client = HdkClient(api_key_list=["hdk-1", "hdk-2", "hdk-3"], http_session=SimpleNamespace(get=_get))
    res = client.get_ranked_ids(1, limit=20)
    assert res["status"] == "success" and res["item_ids"] == ["555666"]
    assert seen_keys == ["hdk-1", "hdk-2", "hdk-3"] and client.api_key == "hdk-3"


def test_injected_taobao_client_honors_authorization_specific_timeout():
    response = SimpleNamespace(status_code=200, raise_for_status=Mock(), json=lambda: {
        "tbk_sc_publisher_info_get_response": {"data": {"total_count": 0, "inviter_list": []}}})
    http = SimpleNamespace(post=Mock(return_value=response))
    client = tb_client.TaobaoClient("key", "secret", session_key="session", http_session=http)
    assert client.check_authority("u_12345678")["is_authorized"] is False
    assert http.post.call_args.kwargs["timeout"] == (2.0, 3.0)


def test_tb_client_never_clobbers_config_when_local_cfg_corrupt(tmp_path, monkeypatch):
    cfg_path = tmp_path / "custom_cfg.json"
    cfg_path.write_text("{corrupt json", encoding="utf-8")
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("TB_REFRESH_TOKEN", "env_ref_tok")

    res = tb_client.reload_or_refresh_tb_credentials(
        app_key="k",
        app_secret="s",
        current_session="old_sess",
        _api_call=lambda **_kw: {
            "status": "success",
            "data": {"top_auth_token_refresh_response": {"token_result": json.dumps({"access_token": "new_sess_tok"})}},
        },
    )
    assert res["session_key"] == "new_sess_tok"
    assert cfg_path.read_text(encoding="utf-8") == "{corrupt json"


def test_unified_channel_names_and_tb_50001_empty_search():
    from adapters.pdd_adapter import PddAdapter

    pdd_channels = {ch["channel_type"]: ch["name"] for ch in PddAdapter.channels}
    tb_channels = {ch["channel_type"]: ch["name"] for ch in TaobaoAdapter.channels}
    assert pdd_channels == tb_channels == {5: "实时销量榜", 1: "今日销量榜"}

    tb_empty = tb_client.search_tb_material_upgrade(
        app_key="test_key",
        app_secret="test_secret",
        adzone_id="123456",
        keyword="zzzznotfound999999xyz",
        _api_call=lambda **_kw: {"status": "error", "error_code": "50001", "error_msg": "[15:50001] 无结果"},
    )
    assert tb_empty["status"] == "success" and tb_empty["data"] == [] and tb_empty["has_more"] is False


def test_taobao_adapter_falls_back_to_ztk_sid_channel_when_top_session_expired():
    # 当官方 TOP taobao_session 过期报错 27:invalid-sessionkey 时，自动回退到折淘客 sid 授权核验与邀请码接口
    http = SimpleNamespace(get=Mock(side_effect=[
        reply({"tbk_sc_publisher_info_get_response": {"data": {
            "total_count": 1,
            "inviter_list": {"map_data": [{"external_id": "u_8bb0595a", "relation_id": "3400251845", "account_name": "测试推手"}]},
        }}}),
        reply({"tbk_sc_invitecode_get_response": {"data": {"inviter_code": "ZTKCODE88"}}}),
    ]))
    ztk = ZtkClient("ztk_key", "ztk_sid", http, appkey_list=("ztk_key",))
    official = Mock()
    official.inviter_code = ""
    official.try_refresh_session.return_value = ""
    official.check_authority.return_value = {
        "status": "error", "error_code": "27",
        "error_msg": "淘宝开放平台业务错误 [27:invalid-sessionkey]: SessionKey非法",
    }
    official.generate_authority_url.return_value = {
        "status": "error", "error_code": "27",
        "error_msg": "淘宝开放平台业务错误 [27:invalid-sessionkey]: SessionKey非法",
    }
    adapter = make_adapter(official, ztk=ztk)

    auth_res = adapter.check_authority("u_8bb0595a")
    assert auth_res["status"] == "success"
    assert auth_res["is_authorized"] is True
    assert auth_res["identity"]["relation_id"] == "3400251845"

    invite_res = adapter.generate_authority_url("u_8bb0595a")
    assert invite_res["status"] == "success"
    assert "inviterCode=ZTKCODE88" in invite_res["auth_url"]
    assert "externalId=u_8bb0595a" in invite_res["auth_url"]


def test_universal_convert_promotional_copy_uses_piliang_and_never_falls_back_to_keyword_search():
    from infrastructure.ztk_client import ZTK_CHANNEL_URL, ZTK_CHANNEL_PILIANG_URL

    promo_copy = (
        "★天猫双11狂欢节 官方立减85折起★两大瓶值31.8亓！伊利金典鲜牛奶组合售价：34元"
        "[下单链接：https://uland.TaoBao.com/item/edetail?id=ZBv39R9SnhPDy0vQa9hN8sVtz-PpYd2QbhO7ZkXxOnIn]"
        "返2亓红包[红包] 下单赠送张凌赫周边！ 伊利鲜奶旗舰店•金典鲜牛奶组合10瓶 有值钱货！"
    )
    http = SimpleNamespace(get=Mock(side_effect=[
        reply({"status": 301, "content": "很抱歉！商品ID解析错误！！！"}),
        reply({
            "status": 200,
            "tao_id": "y3g8ZZZqiRt2KgB4B9YU5OyskUY-NOokyrnFzby6RAz2upW",
            "title": "【直播间下单更优惠】伊利金典鲜牛奶208ml*8+金典450ml*2组合装",
            "pic": "https://img.alicdn.com/bao/uploaded/i4/yili.jpg",
            "quanhou_jiage": "40.3",
            "size": "100.0",
            "coupon_info_money": "36",
            "tkrate3": "2.0",
            "tkfee3": "0.81",
            "result_tkl": "(dAM561pUz8d)",
            "result_url": "https://uland.taobao.com/coupon/edetail?e=yili",
            "shorturl": "https://s.click.taobao.com/yili123",
            "shorturl2": "https://s.tb.cn/h.yili123",
        }),
    ]))
    ztk = ZtkClient("ztk_key", "ztk_sid", http, appkey_list=("ztk_key",))
    official = Mock()
    adapter = make_adapter(official, ztk=ztk, tb_channel_pid="mm_1_2_3")

    res = adapter.resolve_and_convert_universal(promo_copy, uid=None)
    assert res["status"] == "success"
    assert res["title"] == "【直播间下单更优惠】伊利金典鲜牛奶208ml*8+金典450ml*2组合装"
    assert res["promotion_token"] == "(dAM561pUz8d)"
    assert res["short_url"] == "https://s.click.taobao.com/yili123"
    assert res["product"]["goods_id"] == "NOokyrnFzby6RAz2upW"
    assert res["product"]["goods_sign"] == "y3g8ZZZqiRt2KgB4B9YU5OyskUY-NOokyrnFzby6RAz2upW"
    assert http.get.call_args_list[0].args == (ZTK_CHANNEL_URL,)
    assert http.get.call_args_list[1].args == (ZTK_CHANNEL_PILIANG_URL,)
    official.search_goods.assert_not_called()

    # 当折淘客转链失败时，绝不回退到官方关键词模糊搜索顶替商品
    http_fail = SimpleNamespace(get=Mock(side_effect=[
        reply({"status": 301, "content": "很抱歉！商品ID解析错误！！！"}),
        reply({"status": 301, "content": "很抱歉！转链失败！"}),
    ]))
    ztk_fail = ZtkClient("ztk_key", "ztk_sid", http_fail, appkey_list=("ztk_key",))
    adapter_fail = make_adapter(official, ztk=ztk_fail, tb_channel_pid="mm_1_2_3")
    fail_res = adapter_fail.resolve_and_convert_universal("「伊利金典鲜牛奶组合」无效口令文本", uid=None)
    assert fail_res["status"] == "error"
    official.search_goods.assert_not_called()


    # 当输入为手淘 App 原生普通分享文案 (e.tb.cn 纯数字 ID) 时，明确返回 UNSUPPORTED_TB_NUMERIC_ID 指引，绝不爬虫或搜索兜底
    http_etb = SimpleNamespace(get=Mock(side_effect=[
        reply({"status": 301, "content": "很抱歉！商品ID解析错误！！！"}),
        reply({"status": 301, "content": "很抱歉！商品ID解析错误！！！"}),
    ]))
    ztk_etb = ZtkClient("ztk_key", "ztk_sid", http_etb, appkey_list=("ztk_key",))
    adapter_etb = make_adapter(official, ztk=ztk_etb, tb_channel_pid="mm_1_2_3")
    etb_res = adapter_etb.resolve_and_convert_universal(
        "【淘宝】大促价保 https://e.tb.cn/h.jRvbZZRGv5sOyfM?tk=k4VE6XYnYTX CZ057 「可口可乐碳酸饮料mini经典270ml*24罐整箱汽水原味」",
        uid=None,
    )
    assert etb_res["status"] == "error"
    assert etb_res["error_code"] == "UNSUPPORTED_TB_NUMERIC_ID"
    assert "e.tb.cn" in etb_res["error_msg"]
    official.search_goods.assert_not_called()


def test_tb_top_errors_enforce_fail_closed_red_cross_policy(monkeypatch):
    records = []

    def _rec(level, msg, *args):
        text = msg % args if args else str(msg)
        records.append((level, text))

    monkeypatch.setattr(
        tb_client,
        "logger",
        SimpleNamespace(
            debug=lambda msg, *a: _rec("DEBUG", msg, *a),
            info=lambda msg, *a: _rec("INFO", msg, *a),
            warning=lambda msg, *a: _rec("WARNING", msg, *a),
            error=lambda msg, *a: _rec("ERROR", msg, *a),
            exception=lambda msg, *a: _rec("ERROR", msg, *a),
            isEnabledFor=lambda lvl: True,
        ),
    )

    def _call(method, body):
        http = SimpleNamespace(post=Mock(return_value=reply(body)))
        return tb_client.call_tb_api(
            "app_key",
            "app_secret",
            method,
            {"item_id": "123456"},
            http_session=http,
        )

    # 详情接口返回 50001 / 40001 / 应用级错误 -> 可能存在传参截断或内部调用问题，一律打 ❌
    for err_body in (
        {"error_response": {"code": 15, "msg": "Remote service error", "sub_code": "50001", "sub_msg": "无结果"}},
        {"error_response": {"code": 15, "msg": "Remote service error", "sub_code": "40001", "sub_msg": "内部调用失败"}},
        {"error_response": {"code": 11, "msg": "Insufficient isv permissions", "sub_code": "invalid-appkey", "sub_msg": "无效AppKey"}},
    ):
        records.clear()
        res = _call("taobao.tbk.item.info.upgrade.get", err_body)
        assert res["status"] == "error"
        assert any("❌ 【淘宝网关异常】" in msg for _, msg in records)

    # 搜索接口 50001 与官方 24h SessionKey 自动切通道 -> 确定无需运维介入，绝不打 ❌
    records.clear()
    res_search_empty = _call(
        "taobao.tbk.dg.material.optional.upgrade",
        {"error_response": {"code": 15, "msg": "Remote service error", "sub_code": "50001", "sub_msg": "无结果"}},
    )
    assert res_search_empty["status"] == "error"
    assert any(lvl == "DEBUG" for lvl, _ in records)
    assert all("❌" not in msg for _, msg in records)

    records.clear()
    res_sess = _call(
        "taobao.tbk.sc.publisher.info.get",
        {"error_response": {"code": 27, "msg": "Invalid session", "sub_code": "invalid-sessionkey"}},
    )
    assert res_sess["status"] == "error"
    assert any(lvl == "INFO" and "自动切换至折淘客" in msg for lvl, msg in records)
    assert all("❌" not in msg for _, msg in records)


def test_tb_universal_edetail_falls_back_to_official_top_on_ztk_network_error():
    """验证 uland.taobao.com/item/edetail?id=... 在 ZTK 瞬断时无缝降级官方 TOP 通道，不回环到 ZTK。"""
    enc_id = "nV21R9Q5oT7xYzA8bC3dE4fG6hJ7kL9mN"
    ztk = ZtkClient(
        "ztk_key",
        "ztk_sid",
        http_session=SimpleNamespace(get=Mock(side_effect=RuntimeError("conn reset"))),
        appkey_list=("ztk_key",),
    )
    official = Mock()
    off_row = official_item(sign=enc_id, input_id=enc_id, item_url=f"https://uland.taobao.com/item/edetail?id={enc_id}")
    official.get_goods_details.return_value = {"status": "success", "data": [off_row]}
    official.search_goods.return_value = {
        "status": "success",
        "data": [{**off_row, "coupon_share_url": "https://uland.taobao.com/coupon/edetail?e=off2in1"}],
    }
    official.create_tpwd.return_value = {"status": "success", "password_simple": "￥OfficialTpwd8￥"}

    adapter = make_adapter(official, ztk=ztk, tb_channel_pid="mm_1_2_3", tb_adzone_id="3")
    res = adapter.resolve_and_convert_universal(f"https://uland.taobao.com/item/edetail?id={enc_id}")

    assert res["status"] == "success"
    assert res["_provider"] == "tb_official_universal"
    assert res["resolution_kind"] == "product"
    assert res["promotion_token"] == "￥OfficialTpwd8￥"
    assert res["product"]["name"] == "官方标题"
    ztk.http.get.assert_called_once()


def test_reload_or_refresh_tb_credentials_never_treats_user_nick_as_session(monkeypatch, tmp_path):
    cfg = tmp_path / "config.local.json"
    cfg.write_text(json.dumps({
        "taobao_session": "6100_valid_old_session",
        "taobao_refresh_token": "refresh_tok_1",
    }), encoding="utf-8")
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg))

    def fake_api(**kwargs):
        return {
            "status": "success",
            "data": {
                "top_auth_token_refresh_response": {
                    "token_result": json.dumps({"taobao_user_nick": "my_nick_not_a_token"}),
                }
            },
        }

    res = tb_client.reload_or_refresh_tb_credentials("appk", "apps", "6100_valid_old_session", _api_call=fake_api)
    assert res == {"session_key": "6100_valid_old_session"}
    persisted = json.loads(cfg.read_text(encoding="utf-8"))
    assert persisted["taobao_session"] == "6100_valid_old_session"


def test_tb_official_material_recommend_primary_and_hdk_fallback_with_detail_sink():
    """验证官方 taobao.tbk.dg.material.recommend 可独立作为榜单首选（无需 HDK），并在 HDK 失败时兜底及填充 detail_sink。"""
    raw_map = {
        "item_id": "opaque_rec_1",
        "item_basic_info": {"title": "官方精选抽纸", "pict_url": "https://img.alicdn.com/a.jpg", "shop_title": "官方旗舰店"},
        "price_promotion_info": {"zk_final_price": "29.90", "final_promotion_price": "19.90", "coupon_amount": "10.00"},
        "publish_info": {"income_rate": "8.5"},
    }
    post = Mock(return_value=reply({"tbk_dg_material_recommend_response": {"result_list": {"map_data": [raw_map]}, "total_count": 30}}))
    off = tb_client.TaobaoClient("k", "s", adzone_id="123", http_session=SimpleNamespace(post=post))
    adapter_no_hdk = TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123"), official_client=off)
    sinks = []
    rec = adapter_no_hdk.get_recommend_goods(5, offset=0, limit=10, detail_sink=sinks.append)
    assert rec["status"] == "success" and rec["_provider"] == "tb_official_recommend"
    assert rec["data"][0]["name"] == "官方精选抽纸"
    assert rec["data"][0]["price"] == 19.9
    assert len(sinks) == 1 and sinks[0].data["name"] == "官方精选抽纸"
    assert post.call_args.kwargs["data"]["material_id"] == "3756"

    # 当配置了 HDK 但 HDK 失败时，自动平滑降级到 tb_official_recommend
    hdk = Mock()
    hdk.get_ranked_ids.return_value = {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "HDK限流"}
    adapter_with_hdk = make_adapter(off, hdk=hdk)
    rec_fb = adapter_with_hdk.get_recommend_goods(1, offset=0, limit=10)
    assert rec_fb["status"] == "success" and rec_fb["_provider"] == "tb_official_recommend"
    assert post.call_args.kwargs["data"]["material_id"] == "13366"


def test_tb_multi_source_search_detail_promotion_failover_and_truncated_suffix_guard():
    """验证淘宝多源有序降级：搜索 TOP->ZTK->HDK，详情 TOP->ZTK->HDK + 19位残缺后缀拦截，转链 TOP->ZTK渠道->ZTK高佣。"""
    # 1. 截断 19 位淘口令/加密后缀在查详情前即返回 INVALID_IDENTITY，不打 TOP 50001
    off_mock = Mock()
    adapter_guard = make_adapter(off_mock)
    bad_suffix = adapter_guard.get_goods_detail("NOokyrnFzby6RAz2upW")
    assert bad_suffix["status"] == "error" and bad_suffix["error_code"] == "INVALID_IDENTITY"
    off_mock.get_goods_details.assert_not_called()

    # 2. 搜索降级：TOP 报错 -> ZTK api_quanwang -> HDK supersearch
    off_err = Mock()
    off_err.search_goods.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR", "error_msg": "TOP搜索失败"}
    ztk_get = Mock(side_effect=[
        reply({"status": 200, "content": [{"tao_id": "z1-ab", "title": "ZTK抽纸", "pict_url": "https://img.test/z.jpg", "size": "29.9", "quanhou_jiage": "19.9", "coupon_info_money": "10", "tkrate3": "5", "tkfee3": "1.0"}]}),
        reply({"status": 301, "content": "限流"}),
    ])
    ztk = ZtkClient("ztk_k", "ztk_sid", http_session=SimpleNamespace(get=ztk_get), appkey_list=("ztk_k",))
    hdk_get = Mock(return_value=reply({"code": 1, "data": [{"itemid": "hdk_1", "itemtitle": "HDK抽纸", "itempic": "https://img.test/h.jpg", "itemprice": "30", "itemendprice": "20", "couponmoney": "10", "tkrates": "6", "tkmoney": "1.2"}]}))
    hdk = HdkClient("hdk_k", http_session=SimpleNamespace(get=hdk_get))
    a_multi = TaobaoAdapter(
        settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123", tb_channel_pid="mm_1_2_3", ztk_appkey="ztk_k", ztk_sid="ztk_sid", hdk_api_key="hdk_k"),
        official_client=off_err, ztk_client=ztk, hdk_client=hdk,
    )
    sr_ztk = a_multi.search_goods("抽纸")
    assert sr_ztk["status"] == "success" and sr_ztk["_provider"] == "ztk_tb_search"
    assert sr_ztk["data"][0]["name"] == "ZTK抽纸"
    sr_hdk = a_multi.search_goods("抽纸")
    assert sr_hdk["status"] == "success" and sr_hdk["_provider"] == "hdk_tb_search"
    assert sr_hdk["data"][0]["name"] == "HDK抽纸"

    # 3. 详情降级：TOP 网络异常 -> ZTK api_detail -> HDK item_detail
    off_err.get_goods_details.return_value = {"status": "error", "error_code": "NETWORK_ERROR", "error_msg": "timeout"}
    ztk_det_http = Mock(side_effect=[
        reply({"status": 200, "content": [{"tao_id": "673231015690", "title": "ZTK详情抽纸", "pict_url": "https://img.test/zd.jpg", "size": "29.9", "quanhou_jiage": "19.9", "coupon_info_money": "10"}]}),
        reply({"status": 301, "content": "频次上限"}),
    ])
    ztk_det = ZtkClient("ztk_k", "ztk_sid", http_session=SimpleNamespace(get=ztk_det_http), appkey_list=("ztk_k",))
    hdk_det_http = Mock(return_value=reply({"code": 1, "data": {"itemid": "673231015690", "itemtitle": "HDK详情抽纸", "itempic": "https://img.test/hd.jpg", "itemprice": "29.9", "itemendprice": "19.9", "couponmoney": "10"}}))
    a_det = TaobaoAdapter(
        settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123", tb_channel_pid="mm_1_2_3", ztk_appkey="ztk_k", ztk_sid="ztk_sid", hdk_api_key="hdk_k"),
        official_client=off_err, ztk_client=ztk_det, hdk_client=HdkClient("hdk_k", http_session=SimpleNamespace(get=hdk_det_http)),
    )
    d1 = a_det.get_goods_detail("673231015690", goods_id="673231015690")
    assert d1["status"] == "success" and d1["_provider"] == "ztk_tb_detail"
    assert d1["data"]["name"] == "ZTK详情抽纸"
    d2 = a_det.get_goods_detail("673231015690", goods_id="673231015690")
    assert d2["status"] == "success" and d2["_provider"] == "hdk_tb_detail"
    assert d2["data"]["name"] == "HDK详情抽纸"

    # 4. 转链 elif 修复：TOP 失败后依次降级到 ztk_tb_promote -> ztk_tb_gaoyong
    ztk_conv_get = Mock(side_effect=[
        reply({"status": 301, "content": "渠道口令转链限流"}),
        reply({"status": 200, "content": [{"coupon_click_url": "https://uland.taobao.com/gy?e=1", "shorturl": "https://s.click.taobao.com/gy", "tkl": "￥高佣保底￥"}]}),
    ])
    ztk_conv = ZtkClient("ztk_k", "ztk_sid", http_session=SimpleNamespace(get=ztk_conv_get), appkey_list=("ztk_k",))
    a_conv = TaobaoAdapter(
        settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123", tb_channel_pid="mm_1_2_3", ztk_appkey="ztk_k", ztk_sid="ztk_sid"),
        official_client=off_err, ztk_client=ztk_conv,
    )
    conv_res = a_conv.convert_promotion_url("673231015690", goods_id="673231015690")
    assert conv_res["status"] == "success" and conv_res["_provider"] == "ztk_tb_gaoyong"
    assert conv_res["promotion_token"] == "￥高佣保底￥"


def test_ztk_publisher_pagination_session_cooldown_and_resolve_item_id():
    """验证 ZTK 渠道备案自动翻页、24h TOP Session 冷却退避及 open_shangpin_id 独立解链。"""
    from infrastructure.api_governance import GLOBAL_PROVIDER_COOLDOWN

    GLOBAL_PROVIDER_COOLDOWN.reset()
    # 1. check_publisher_relation 第1页未命中但 total_count=150，自动翻第2页命中
    p1 = {"tbk_sc_publisher_info_get_response": {"data": {"total_count": 150, "inviter_list": {"map_data": [{"external_id": "other", "relation_id": "111"}]}}}}
    p2 = {"tbk_sc_publisher_info_get_response": {"data": {"total_count": 150, "inviter_list": {"map_data": [{"external_id": "u_target", "relation_id": "999888"}]}}}}
    ztk_get = Mock(side_effect=[reply(p1), reply(p2), reply({"item_id": "673231015690"})])
    ztk = ZtkClient("ztk_k", "ztk_sid", http_session=SimpleNamespace(get=ztk_get), appkey_list=("ztk_k",))
    auth = ztk.check_publisher_relation("u_target")
    assert auth["status"] == "success" and auth["is_authorized"] is True
    assert auth["identity"]["relation_id"] == "999888"

    # 2. open_shangpin_id.ashx + resolve_product_from_link
    off_ok = Mock()
    off_ok.get_goods_details.return_value = {"status": "success", "data": [official_item(sign="enc_resolved", input_id="673231015690")]}
    a_link = TaobaoAdapter(
        settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123", tb_channel_pid="mm_1_2_3", ztk_appkey="ztk_k", ztk_sid="ztk_sid"),
        official_client=off_ok, ztk_client=ztk,
    )
    resolved = a_link.resolve_product_from_link("89￥ABCD1234EFGH￥:// CZ0001")
    assert resolved["status"] == "success" and resolved["item_id"] == "673231015690"
    assert resolved["product"]["name"] == "官方标题"

    # 3. 真实 HTTP 会话下 TOP Session 27 错触发 300s 冷却，二次核验跳过官方直走 ZTK
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("infrastructure.api_governance.is_real_requests_session", lambda _s: True)
        off_sess = Mock()
        off_sess.http_session = SimpleNamespace()
        off_sess.session_key = "expired_24h"
        off_sess.try_refresh_session.return_value = ""
        off_sess.check_authority.return_value = {"status": "error", "error_code": "27", "error_msg": "invalid-sessionkey"}
        ztk2 = Mock()
        ztk2.appkey_list = ("ztk_k",)
        ztk2.sid = "ztk_sid"
        ztk2.check_publisher_relation.return_value = {"status": "success", "is_authorized": True, "bind": 1, "identity": {"external_id": "u_target", "relation_id": "999888"}}
        a_cool = TaobaoAdapter(
            settings=SimpleNamespace(
                taobao_enabled=True, tb_app_key="k", tb_app_secret="s",
                tb_session="expired_24h", tb_refresh_token="refresh_tok_1",
                tb_channel_pid="mm_1_2_3", ztk_appkey="ztk_k", ztk_sid="ztk_sid",
            ),
            official_client=off_sess, ztk_client=ztk2,
        )
        r1 = a_cool.check_authority("u_target")
        r2 = a_cool.check_authority("u_target")
        assert r1["status"] == "success" and r2["status"] == "success"
        assert off_sess.check_authority.call_count == 1
        assert ztk2.check_publisher_relation.call_count == 2


def test_tb_detail_mismatch_hdk_status_retry_promo_id_and_carousel():
    """验证淘宝详情防串货/空壳过滤、HDK 报错映射、空 data 重试、加密选品优先及逗号轮播图解析。"""
    from infrastructure.http_utils import parse_carousel_images
    from models.product import ProductRef

    # 1. ZTK 详情返回其他商品（含空壳与错配）绝不伪造请求方的 goods_sign
    row_payload = {"status": 200, "content": [{"tao_id": "", "title": "", "pict_url": ""}, {"tao_id": "OTHER_ENC_SIGN", "num_iid": "999888", "title": "串货商品", "pict_url": "https://img/x.jpg"}]}
    ztk_get = Mock(side_effect=[reply(row_payload), reply(row_payload)])
    ztk_cli = ZtkClient("ztk_k", "ztk_sid", http_session=SimpleNamespace(get=ztk_get), appkey_list=("ztk_k",))
    ztk_raw = ztk_cli.get_tb_goods_detail("REQ_ENC_A")
    assert ztk_raw["status"] == "success" and ztk_raw["data"][0]["tao_id"] == "OTHER_ENC_SIGN"

    off_err = Mock()
    off_err.get_goods_details.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR", "error_msg": "top fail"}
    a_mismatch = make_adapter(official=off_err, ztk=ztk_cli, hdk=Mock(appkey_list=()))
    det_res = a_mismatch.get_goods_detail("REQ_ENC_A", goods_sign="REQ_ENC_A")
    assert det_res["status"] == "error" and det_res["error_code"] == "PRODUCT_IDENTITY_MISMATCH"

    # 2. HDK 精选“下架”映射 NOT_FOUND（非 PRODUCT_OFF_PROMOTION），“未授权”映射 UPSTREAM_PROVIDER_UNAUTHORIZED（非 AUTH_REQUIRED）
    hdk_get = Mock(side_effect=[
        reply({"code": 0, "msg": "该商品已下架"}),
        reply({"code": 0, "msg": "淘宝账号未授权"}),
    ])
    hdk_cli = HdkClient(api_key="hdk_k", http_session=SimpleNamespace(get=hdk_get))
    r_not_found = hdk_cli.get_tb_goods_detail("673231015690")
    assert r_not_found["status"] == "error" and r_not_found["error_code"] == "NOT_FOUND" and r_not_found.get("hdk_not_found") is True
    r_unauth = hdk_cli.get_tb_goods_detail("673231015690")
    assert r_unauth["status"] == "error" and r_unauth["error_code"] == "UPSTREAM_PROVIDER_UNAUTHORIZED"

    # 3. _retry_on_off_promotion 遇到 TOP status=success 但 data=[] 时也会触发一次重试
    calls = {"n": 0}

    def _top_call():
        calls["n"] += 1
        return {"status": "success", "data": []} if calls["n"] == 1 else {"status": "success", "data": [{"name": "ok"}]}

    retried, confirmed_off = a_mismatch._retry_on_off_promotion(_top_call, is_off_fn=lambda r: r.get("status") != "success" or not r.get("data"))
    assert calls["n"] == 2 and retried["data"][0]["name"] == "ok" and confirmed_off is False

    # 4. _preferred_tb_promo_id 优先非纯数字、非截断 goods_sign
    assert TaobaoAdapter._preferred_tb_promo_id(ProductRef("taobao", "673231015690", "enc_real_sign_XYZ")) == "enc_real_sign_XYZ"
    assert TaobaoAdapter._preferred_tb_promo_id(ProductRef("taobao", "673231015690", "")) == "673231015690"

    # 5. HDK "0.00" 售价回退 itemprice，及 parse_carousel_images 支持逗号分隔多图且保留 OSS 参数内逗号
    hdk_norm = a_mismatch._normalize_hdk_tb_detail({
        "itemid": "673231015690",
        "itemtitle": "维达抽纸",
        "itempic": "https://img/p.jpg",
        "taobao_image": "https://img/1.jpg?x-oss-process=image/resize,w_800,https://img/2.jpg|//img/3.jpg",
        "itemprice": "29.90",
        "itemendprice": "0.00",
        "couponmoney": "0",
        "tkrates": "10.0",
    })
    assert hdk_norm["price"] == 29.9
    assert hdk_norm["carousel_images"] == [
        "https://img/p.jpg",
        "https://img/1.jpg?x-oss-process=image/resize,w_800",
        "https://img/2.jpg",
        "https://img/3.jpg",
    ]
    assert parse_carousel_images("https://a.com/1.jpg?x=1,2,https://a.com/2.jpg") == [
        "https://a.com/1.jpg?x=1,2",
        "https://a.com/2.jpg",
    ]


def test_taobao_universal_rejects_cross_platform_and_spoofed_domains():
    ztk = Mock()
    official = Mock()
    adapter = make_adapter(official, ztk=ztk, tb_channel_pid="mm_1_2_3")

    for bad_url in ("https://u.jd.com/oOUaUwq", "https://p.pinduoduo.com/act123", "https://eviltaobao.com/item.htm?id=1"):
        res = adapter.resolve_and_convert_universal(bad_url)
        assert res["status"] == "error" and res["error_code"] == "INVALID_TAOBAO_URL"
    ztk.convert_channel_url.assert_not_called()
    official.convert_promotion_url.assert_not_called()


def test_ztk_dual_sid_pools_tb_batch_details_venue_wh_alimama_and_auth_priority():
    """验证 ZTK 双 SID 池隔离路由、TB 20 批量查卡（不连坐）、pages-fast/wh_alimama 已转链会场识别及 90d ZTK 授权优先。"""
    from models.product import ProductRef

    # 1. 双 SID 池隔离路由：高并发走 sid_list，渠道备案/邀请走 publisher_sid_list
    seen_calls = []

    def _dual_get(url, params=None, timeout=None, allow_redirects=False):
        seen_calls.append((url, dict(params or {})))
        if "open_gaoyongzhuanlian.ashx" in url:
            return reply({
                "status": 200,
                "content": [{
                    "item_url": "https://s.click.taobao.com/gy_ok",
                    "shorturl": "https://s.click.taobao.com/gy_s",
                    "tkl": "8￥GYTOK1234￥",
                    "quanhou_jiage": "19.9",
                    "tkrate3": "10",
                }],
            })
        if "open_sc_publisher_get.ashx" in url:
            return reply({
                "tbk_sc_publisher_info_get_response": {
                    "data": {
                        "total_count": 1,
                        "inviter_list": {
                            "map_data": [{"external_id": "u_dual", "relation_id": "2999888"}],
                        },
                    },
                },
            })
        if "open_sc_invitecode_get.ashx" in url:
            return reply({"tbk_sc_invitecode_get_response": {"data": {"inviter_code": "PUBINV1"}}})
        raise AssertionError(f"Unexpected ZTK call: {url}")

    ztk_dual = ZtkClient(
        "k0", "orig_sid",
        appkey_list=("k0", "k1", "k2"),
        sid_list=("orig_sid", "193336", "193338"),
        publisher_sid_list=("orig_sid", "193334", "193333"),
        http_session=SimpleNamespace(get=_dual_get),
    )
    assert ztk_dual._sid_for_key("k1") == "193336"
    assert ztk_dual._sid_for_key("k2") == "193338"
    assert ztk_dual._publisher_sid_for_key("k1") == "193334"
    assert ztk_dual._publisher_sid_for_key("k2") == "193333"
    gy_res = ztk_dual.convert_gaoyong_url("673231015690", "mm_1_2_3")
    auth_res = ztk_dual.check_publisher_relation("u_dual")
    url_res = ztk_dual.generate_authority_url("u_dual")
    assert gy_res["status"] == "success"
    assert auth_res["status"] == "success" and auth_res["identity"]["relation_id"] == "2999888"
    assert url_res["status"] == "success" and "PUBINV1" in url_res["auth_url"]
    # 验证 gaoyong 用 sid_list，publisher_info / invitecode 用 publisher_sid_list
    gy_params = [p for u, p in seen_calls if "open_gaoyongzhuanlian.ashx" in u][0]
    pub_params = [p for u, p in seen_calls if "open_sc_publisher_get.ashx" in u][0]
    inv_params = [p for u, p in seen_calls if "open_sc_invitecode_get.ashx" in u][0]
    assert gy_params["sid"] == {"k0": "orig_sid", "k1": "193336", "k2": "193338"}[gy_params["appkey"]]
    assert pub_params["sid"] == {"k0": "orig_sid", "k1": "193334", "k2": "193333"}[pub_params["appkey"]]
    assert inv_params["sid"] == {"k0": "orig_sid", "k1": "193334", "k2": "193333"}[inv_params["appkey"]]

    # 2. TaobaoAdapter.batch_get_goods_details: 20批量靠 input_item_iid 精准对齐，坏/缺失项单查兜底不连坐
    off_batch = Mock()
    off_batch.get_goods_details.side_effect = lambda ids: (
        {"status": "success", "data": [official_item(sign="enc_batch_1", input_id="enc_batch_1")]}
        if len(ids) > 1
        else {"status": "error", "error_code": "isv.item-not-exist", "error_msg": "不存在"}
    )
    ztk_batch = Mock()
    ztk_batch.appkey_list = ("k0",)
    ztk_batch.sid = "orig_sid"
    ztk_batch.sid_list = ("orig_sid",)
    ztk_batch.get_tb_goods_detail.return_value = {"status": "error", "error_code": "NOT_FOUND"}
    hdk_batch = Mock()
    hdk_batch.appkey_list = ()
    ad_batch = make_adapter(official=off_batch, ztk=ztk_batch, hdk=hdk_batch)
    batch_res = ad_batch.batch_get_goods_details([
        ProductRef("taobao", "c1", goods_sign="enc_batch_1"),
        ProductRef("taobao", "bad_off", goods_sign="bad_off"),
    ])
    assert batch_res["enc_batch_1"]["status"] == "success"
    assert batch_res["enc_batch_1"]["data"]["name"] == "官方标题"
    assert batch_res["bad_off"]["error_code"] == "PRODUCT_OFF_PROMOTION"

    # 3. pages-fast / wh_alimama=true 官方会场成功转为 s.click 推广链后，判为 verified coupon_or_venue
    ztk_venue = Mock()
    ztk_venue.appkey_list = ("k0",)
    ztk_venue.sid = "orig_sid"
    ztk_venue.sid_list = ("orig_sid",)
    ztk_venue.convert_channel_url.return_value = {
        "status": "success",
        "title": "淘宝精选活动会场",
        "final_price": None,
        "coupon_amount": 0.0,
        "url": "https://s.click.taobao.com/converted_venue_long",
        "final_link": "https://s.click.taobao.com/converted_venue_short",
        "short_url": "https://s.click.taobao.com/converted_venue_short",
        "tpwd": "8￥VENUEOK1234￥",
        "tao_id": "",
        "raw": {},
    }
    ad_venue = make_adapter(Mock(), ztk=ztk_venue, hdk=Mock(appkey_list=()), tb_channel_pid="mm_1_2_3")
    raw_wh_url = "https://pages-fast.m.taobao.com/wow/z/uniapp/1323/page?wh_alimama=true"
    v_res = ad_venue.resolve_and_convert_universal(raw_wh_url)
    assert v_res["status"] == "success"
    assert v_res["resolution_kind"] == "coupon_or_venue"
    assert not v_res.get("detail_lookup_failed")

    # 4. 无 tb_refresh_token 时，即使配了 tb_session，check_authority 也优先走 90d ztk_tb_auth 避免 24h invalid-sessionkey
    off_no_rt = Mock()
    off_no_rt.check_authority.side_effect = AssertionError("must prefer ztk_tb_auth when tb_refresh_token is absent")
    ztk_auth_pref = Mock()
    ztk_auth_pref.appkey_list = ("k0",)
    ztk_auth_pref.sid = "orig_sid"
    ztk_auth_pref.sid_list = ("orig_sid",)
    ztk_auth_pref.check_publisher_relation.return_value = {
        "status": "success", "is_authorized": True, "bind": 1, "identity": {"external_id": "u1", "relation_id": "888777"},
    }
    ad_auth_pref = TaobaoAdapter(
        settings=SimpleNamespace(
            taobao_enabled=True, tb_app_key="k", tb_app_secret="s",
            tb_session="stale_24h", tb_channel_pid="mm_1_2_3", ztk_appkey="k0", ztk_sid="orig_sid",
        ),
        official_client=off_no_rt, ztk_client=ztk_auth_pref,
    )
    ar = ad_auth_pref.check_authority("u1")
    assert ar["status"] == "success" and ar["identity"]["relation_id"] == "888777"
    ztk_auth_pref.check_publisher_relation.assert_called_once_with("u1")

    # 5. ZtkClient.convert_channel_url 保留 tkl_piliang 空 tao_id 但含 ali_trackid 的官方会场，并拒绝 err.tmall.com/error 假 200
    def _venue_piliang_get(url, params=None, timeout=None, allow_redirects=False):
        tkl_in = (params or {}).get("tkl") or ""
        if "open_gaoyongzhuanlian_tkl.ashx" in url:
            return reply({"status": 301, "content": "很抱歉！商品ID解析错误！！！"})
        if "open_gaoyongzhuanlian_tkl_piliang.ashx" in url:
            if "404page" in tkl_in:
                return reply({"status": 200, "tao_id": "", "result_url": "https://err.tmall.com/error1.html?c=404&ali_trackid=2:1"})
            return reply({"status": 200, "tao_id": "", "result_url": f"{tkl_in}&ali_trackid=2:116341500041"})
        raise AssertionError(f"Unexpected ZTK call: {url}")

    ztk_vcli = ZtkClient("k0", "orig_sid", http_session=SimpleNamespace(get=_venue_piliang_get))
    v_ok = ztk_vcli.convert_channel_url(
        "https://pages.tmall.com/wow/z/lianmeng/default/route?budgetTag=2026012001&wh_alimama=true",
        "", "mm_1_2_3", allow_public=True,
    )
    assert v_ok["status"] == "success" and "ali_trackid=2:116341500041" in v_ok["final_link"]
    v_404 = ztk_vcli.convert_channel_url(
        "https://pages-fast.m.taobao.com/wow/z/uniapp/404page?wh_alimama=true",
        "", "mm_1_2_3", allow_public=True,
    )
    assert v_404["status"] == "error"


def test_tb_channel_convert_never_fabricates_placeholder_and_auto_rescues_detail():
    """
    验证：
    1. ZtkClient.convert_channel_url 在上游缺标题时严禁捏造 "渠道推广商品"；
    2. TaobaoAdapter._do_ztk_resolve 遇到缺失标题/价格/图片的残卡时自动调用 get_goods_detail(tao_id) 补全，
       下架短链透出 PRODUCT_OFF_PROMOTION，拉卡失败则 Fail-Closed 标为 degraded_product。
    """
    missing_raw_item = {
        "status": 200,
        "content": [{
            "tao_id": "nV21R9Q5oT7xYzA8bC3dE4fG6hJ7kL9mN",
            "title": "",
            "size": "0",
            "quanhou_jiage": "0",
            "pic": "",
            "coupon_click_url": "https://uland.taobao.com/coupon/edetail?e=ok1",
            "shorturl": "https://s.click.taobao.com/tbOk1",
            "tkl": "￥okToken123￥",
        }],
    }
    ztk = ZtkClient("ztk_key", "ztk_sid", SimpleNamespace(get=Mock(return_value=reply(missing_raw_item))), appkey_list=("ztk_key",))
    conv_raw = ztk.convert_channel_url("https://uland.taobao.com/coupon/edetail?e=raw1", "", "mm_1_2_3", allow_public=True)
    assert conv_raw["status"] == "success"
    assert conv_raw["title"] == "" and conv_raw["title"] != "渠道推广商品"

    full_detail_card = {
        "status": "success",
        "data": {
            "platform": "taobao",
            "platform_name": "淘宝/天猫",
            "goods_id": "6hJ7kL9mN",
            "goods_sign": "nV21R9Q5oT7xYzA8bC3dE4fG6hJ7kL9mN",
            "name": "维达超韧抽纸4层24包",
            "price": 32.9,
            "original_price": 49.9,
            "coupon_amount": 5.0,
            "has_coupon": True,
            "image_url": "https://img.alicdn.com/vinda.jpg",
            "carousel_images": ["https://img.alicdn.com/vinda1.jpg", "https://img.alicdn.com/vinda2.jpg"],
            "mall_name": "维达官方旗舰店",
            "product_url": "https://uland.taobao.com/item/edetail?id=nV21R9Q5oT7xYzA8bC3dE4fG6hJ7kL9mN",
        },
    }
    ad_rescue = make_adapter(Mock(), ztk=ztk, tb_channel_pid="mm_1_2_3")
    ad_rescue.get_goods_detail = Mock(return_value=full_detail_card)
    r_ok = ad_rescue.resolve_and_convert_universal("https://uland.taobao.com/coupon/edetail?e=raw1")
    assert r_ok["status"] == "success"
    assert r_ok["resolution_kind"] == "product"
    assert r_ok["product"]["name"] == "维达超韧抽纸4层24包"
    assert r_ok["product"]["price"] == 32.9
    ad_rescue.get_goods_detail.assert_called_once()

    ad_off = make_adapter(Mock(), ztk=ztk, tb_channel_pid="mm_1_2_3")
    ad_off.get_goods_detail = Mock(return_value={"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "已下架"})
    r_off = ad_off.resolve_and_convert_universal("https://uland.taobao.com/coupon/edetail?e=raw1")
    assert r_off["status"] == "error" and r_off["error_code"] == "PRODUCT_OFF_PROMOTION"

    ad_deg = make_adapter(Mock(), ztk=ztk, tb_channel_pid="mm_1_2_3")
    ad_deg.get_goods_detail = Mock(return_value={"status": "error", "error_code": "RATE_LIMIT", "error_msg": "rate limited"})
    r_deg = ad_deg.resolve_and_convert_universal("https://uland.taobao.com/coupon/edetail?e=raw1")
    assert r_deg["status"] == "success"
    assert r_deg["resolution_kind"] == "degraded_product"
    assert r_deg["detail_lookup_failed"] is True
    assert r_deg["product"] is None






