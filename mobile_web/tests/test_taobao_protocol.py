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
    assert a.active and a.capabilities["detail"] and a.capabilities["search"]
    assert not a.capabilities["orders"] and not a.capabilities["recommend"]
    assert a.get_recommend_goods()["error_code"] == "CONFIG_ERROR"
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
    def response(records):
        return {"status": "success", "data": {"tbk_sc_publisher_info_get_response": {"data": {"inviter_list": {"map_data": records}}}}}
    api.return_value = response([{"external_id": "another-user", "relation_id": "9007199254740993"}])
    assert tb_client.check_publisher_relation("k", "s", "uid", "session", api)["error_code"] == "IDENTITY_MISMATCH"
    api.return_value = response([{"external_id": "uid", "relation_id": "9007199254740993"}])
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
        assert result["cacheable"] is False and "expires_at" not in result
        assert ztk.convert_channel_url.call_args.args[1] == relation
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
    official.create_tpwd.return_value = {"status": "success", "model": "￥token￥", "expires_at": 1900000000}
    result = make_adapter(official).convert_promotion_url("101")
    assert result["expires_at"] == 1900000000 and result["cacheable"]
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
    official = promotion_official()
    ztk = ZtkClient("ztk_key", "ztk_sid")
    ztk.convert_channel_url = Mock(return_value={
        "status": "error",
        "error_code": "AUTH_REQUIRED",
        "error_msg": "折淘客渠道授权失效: 很抱歉！无效的relationId！",
        "need_auth": True,
    })
    adapter = make_adapter(official, ztk=ztk)
    repo, users = Mock(), Mock()
    repo.get_promotion_link_cache.return_value = None
    users.get_promotion_context.return_value = {
        "status": "success", "is_authorized": True, "identity": {"external_id": "u_12345678", "relation_id": "bad_rid"}
    }
    monkeypatch.setattr("services.promote_service.get_adapter", lambda platform: adapter)

    service = PromoteService(repo, users)
    result = service.convert_link("taobao", "101", uid="u_12345678")

    # 1. 策略 B: 转链失败不抹除数据库授权信息，防止网络抖动误伤推手
    users.invalidate_promotion_authorization.assert_not_called()
    # 2. 返回站长自营兜底链接供继续复制
    assert result["status"] == "success"
    assert result["need_auth"] is True
    assert result["is_fallback"] is True
    assert result["final_link"].startswith("https://")
    assert result["promotion_token"] == "￥真实口令￥"
    assert "渠道授权未生效或已失效" in result["auth_msg"]
    # 3. 站长官方接口被调用以生成兜底链接
    assert official.search_goods.called


def test_adapter_dual_track_channel_promotion_via_ztk():
    official = promotion_official()
    ztk = ZtkClient("ztk_key", "ztk_sid")
    ztk.convert_channel_url = Mock(return_value={
        "status": "success",
        "final_link": "https://uland.taobao.com/channel_landing",
        "tpwd": "￥CHANNEL_TPWD￥",
        "url": "https://uland.taobao.com/channel_landing",
    })
    adapter = make_adapter(official, ztk=ztk)
    result = adapter.convert_promotion_url("101", uid="u_12345678", identity={"external_id": "u_12345678", "relation_id": "3400251845"})
    assert result["status"] == "success"
    assert result["final_link"] == "https://uland.taobao.com/channel_landing"
    assert result["promotion_token"] == "￥CHANNEL_TPWD￥"
    assert result["attribution_status"] == "verified"
    assert result["relation_id"] == "3400251845"
    assert ztk.convert_channel_url.called
    official.search_goods.assert_not_called()


def test_unauthorized_user_promotion_falls_back_to_official_station_master_link(monkeypatch):
    from services.promote_service import PromoteService
    official = promotion_official()
    adapter = make_adapter(official)
    repo, users = Mock(), Mock()
    repo.get_promotion_link_cache.return_value = None
    users.get_promotion_context.return_value = {"status": "success", "is_authorized": False, "identity": {}}
    monkeypatch.setattr("services.promote_service.get_adapter", lambda platform: adapter)
    result = PromoteService(repo, users).convert_link("taobao", "101", uid="u_57f51d4c")
    users.get_promotion_context.assert_called_once_with("u_57f51d4c", "taobao", allow_remote=False)
    assert result["status"] == "success"
    assert result["need_auth"] is True
    assert result["is_fallback"] is True
    assert result["final_link"].startswith("https://")
    assert result["promotion_token"] == "￥真实口令￥"
    assert result["auth_platform"] == "taobao"
    assert "尚未在淘宝完成渠道授权备案" in result["auth_msg"]
    assert result["attribution_status"] == "anonymous"
    assert result["cacheable"] is False
    assert "relation_id" not in official.search_goods.call_args.kwargs


def test_user_service_caches_negative_auth_status_without_repeated_network_calls(monkeypatch):
    from services.user_service import UserService
    repo = Mock()
    repo.get_user_by_uid.return_value = {
        "uid": "u_testuser",
        "auth_status": {
            "taobao": {
                "is_authorized": False,
                "bind": 0,
                "updated_at": time.time(),
            }
        }
    }
    adapter = Mock()
    adapter.active = True
    adapter.platform_name = "淘宝"
    monkeypatch.setattr("services.user_service.get_adapter", lambda plat: adapter)

    service = UserService(user_repo=repo)
    res = service.get_promotion_context("u_testuser", "taobao")
    assert res["status"] == "success"
    assert res["is_authorized"] is False
    assert res["identity"] == {}
    adapter.check_authority.assert_not_called()

    # When force=True, it rechecks upstream
    adapter.check_authority.return_value = {"status": "success", "is_authorized": False, "bind": 0}
    repo.update_auth_status.return_value = True
    res_forced = service.get_promotion_context("u_testuser", "taobao", force=True)
    assert res_forced["status"] == "success"
    assert res_forced["is_authorized"] is False
    adapter.check_authority.assert_called_once_with(uid="u_testuser")


def test_promote_service_caches_generic_promotion_in_db_for_unauthorized_users(monkeypatch):
    from services.promote_service import PromoteService
    from models.product import ProductRef
    repo = Mock()
    user_svc = Mock()
    user_svc.get_promotion_context.return_value = {
        "status": "success", "is_authorized": False, "identity": {}
    }
    repo.get_product_identity.return_value = None
    repo.get_promotion_link_cache.return_value = None

    adapter = Mock()
    adapter.active = True
    adapter.platform_name = "淘宝"
    adapter.requires_authorized_promotion = True
    adapter.resolve_product_ref.return_value = ProductRef("taobao", "101", "")
    adapter.convert_promotion_url.return_value = {
        "status": "success", "final_link": "https://uland.taobao.com/coupon/test",
        "promotion_token": "￥通用口令￥", "need_auth": False, "is_fallback": False,
        "attribution_status": "anonymous", "expires_at": time.time() + 3600,
        "goods_id": "101", "link_type": "通用官方推广"
    }
    adapter.build_command_text.return_value = "测试文案"
    monkeypatch.setattr("services.promote_service.get_adapter", lambda plat: adapter)

    service = PromoteService(product_repo=repo, user_service=user_svc)
    result = service.convert_link(platform="taobao", goods_id_or_sign="101", uid="u_57f51d4c")
    assert result["status"] == "success"
    assert result["need_auth"] is True
    assert result["final_link"] == "https://uland.taobao.com/coupon/test"
    repo.save_promotion_link_cache.assert_called_once()
    saved_data = repo.save_promotion_link_cache.call_args.kwargs["data"]
    assert saved_data["final_link"] == "https://uland.taobao.com/coupon/test"
    assert saved_data["promotion_token"] == "￥通用口令￥"

    assert saved_data["need_auth"] is False and saved_data["is_fallback"] is False
    assert "auth_msg" not in saved_data
    assert adapter.convert_promotion_url.call_args.kwargs["uid"] is None
    # Clear memory to exercise persistent-cache refill, not another memory hit.
    from cache import PROMOTION_CACHE
    PROMOTION_CACHE.clear()
    repo.get_promotion_link_cache.return_value = {
        "status": "success", "final_link": "https://uland.taobao.com/coupon/test",
        "promotion_token": "￥通用口令￥", "link_type": "通用官方推广",
        "_expires_at": time.time() + 3600
    }
    adapter.convert_promotion_url.reset_mock()
    res2 = service.convert_link(platform="taobao", goods_id_or_sign="101", uid="u_57f51d4c")
    assert res2["status"] == "success"
    assert res2["need_auth"] is True
    assert res2["final_link"] == "https://uland.taobao.com/coupon/test"
    adapter.convert_promotion_url.assert_not_called()


def test_taobao_detail_extracts_verified_specs_from_official_item():
    official = Mock()
    official.configured = True
    official.get_goods_details.return_value = {
        "status": "success",
        "data": [{
            "goods_id": "889900",
            "goods_sign": "889900",
            "goods_name": "维达抽纸整箱",
            "brand_name": "Vinda/维达",
            "category_name": "纸品/湿巾",
            "mall_name": "维达官方旗舰店",
            "provcity": "广东 江门",
            "min_group_price_yuan": 29.9,
            "original_price_yuan": 39.9,
            "coupon_discount_yuan": 10.0,
            "has_coupon": True,
        }],
    }
    adapter = make_adapter(official)
    res = adapter.get_goods_detail("889900", goods_id="889900")
    assert res["status"] == "success"
    assert res["data"]["specs"] == [
        {"label": "商品品牌", "value": "Vinda/维达"},
        {"label": "商品类目", "value": "纸品/湿巾"},
        {"label": "店铺名称", "value": "维达官方旗舰店"},
        {"label": "发货地", "value": "广东 江门"},
    ]


def test_ztk_client_multi_key_failover():
    http = SimpleNamespace(get=Mock(side_effect=[
        reply({"code": 301, "msg": "appkey日调用次数超限"}),
        reply(order_response([{"trade_id": "t1"}])),
    ]))
    client = ZtkClient("key1", "sid1", http, appkey_list=("key1", "key2"))
    res = client.query_orders(0, 100)
    assert res["status"] == "success"
    assert len(res["order_list"]) == 1
    assert client.appkey == "key2"
    assert http.get.call_count == 2


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



