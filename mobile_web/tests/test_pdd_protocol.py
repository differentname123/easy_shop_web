"""PDD runtime protocol regression checks with exclusively fake requests."""

import hashlib
import inspect
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from infrastructure import pdd_client


@pytest.fixture
def fake_gateway(monkeypatch):
    replies = []

    def post(*args, **kwargs):
        assert replies, "unexpected gateway request"
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: reply)

    session = SimpleNamespace(post=Mock(side_effect=post))
    monkeypatch.setattr(pdd_client, "_PDD_SESSION", session)
    return session.post, replies


def test_only_runtime_closure_and_transport_entry_points_are_exported():
    signatures = {
        "make_custom_parameters": "(uid=None, extra_custom=None)",
        "extract_short_url": "(response_dict)",
        "call_pdd_api": "(client_id, client_secret, api_type, business_params=None, timeout=(2.5, 4.0))",
        "clean_goods_item": "(raw_data, source_type='')",
        "clean_order_item": "(order)",
        "check_member_authority": "(client_id, client_secret, pid, uid)",
        "generate_authority_url": "(client_id, client_secret, pid, uid=None, **kwargs)",
        "convert_pdd_url": "(client_id, client_secret, pid, source_url, uid=None, **kwargs)",
        "generate_goods_promotion_url": "(client_id, client_secret, pid, goods_sign, uid=None, search_id=None, **kwargs)",
        "generate_smart_promotion_url": "(client_id, client_secret, pid, goods_sign, cash_gift_id=None, uid=None, **kwargs)",
        "get_goods_detail": "(client_id, client_secret, pid=None, goods_sign=None, goods_id=None, uid=None, **kwargs)",
        "query_increment_orders": "(client_id, client_secret, start_time, end_time, page=1, page_size=50, **kwargs)",
        "configure_transport": "(dns_mode='auto', trust_env=None)",
        "warmup": "()",
    }
    exported = {
        name for name, obj in vars(pdd_client).items()
        if not name.startswith("_") and inspect.isfunction(obj) and obj.__module__ == pdd_client.__name__
    }
    assert exported == set(signatures)
    for name, signature in signatures.items():
        assert str(inspect.signature(getattr(pdd_client, name))) == signature
    assert not hasattr(pdd_client, "get_config")
    assert not hasattr(pdd_client, "setup_logger")
    assert not hasattr(pdd_client, "traceback")


@pytest.mark.parametrize("uid,expected", [(None, None), ("   ", None), (0, '{"uid":"0"}'), ("  用户01  ", '{"uid":"用户01"}')])
def test_custom_parameters_preserve_uid_string_and_unicode(uid, expected):
    assert pdd_client.make_custom_parameters(uid) == expected


def test_extra_custom_parameters_preserve_original_override_semantics():
    assert pdd_client.make_custom_parameters("old", {"uid": "new", "渠道": 2}) == '{"uid":"new","渠道":2}'
    assert pdd_client.make_custom_parameters(None, {"x": [1, 2]}) == '{"x":[1,2]}'
    assert pdd_client.make_custom_parameters(None, "not-a-dict") is None


def test_gateway_signs_exact_serialization_and_keeps_default_post_timeout(monkeypatch, fake_gateway):
    post, replies = fake_gateway
    monkeypatch.setattr(pdd_client.time, "time", lambda: 1234567890.9)
    replies.append({"ok": True})
    business = {"enabled": True, "disabled": False, "values": ["中文", 1], "object": {"uid": "01"}, "omit": None, "zero": 0}
    assert pdd_client.call_pdd_api("client", "secret", "pdd.test", business) == {"ok": True}
    args, kwargs = post.call_args
    assert args == ("https://gw-api.pinduoduo.com/api/router",)
    assert kwargs["timeout"] == (2.5, 4.0)
    assert kwargs["headers"] == {"Content-Type": "application/json;charset=utf-8"}
    params = kwargs["json"]
    expected = {
        "type": "pdd.test", "client_id": "client", "timestamp": "1234567890",
        "data_type": "JSON", "version": "V1", "enabled": "true", "disabled": "false",
        "values": '["中文",1]', "object": '{"uid":"01"}', "zero": "0",
    }
    signed_text = "secret" + "".join(key + expected[key] for key in sorted(expected)) + "secret"
    expected["sign"] = hashlib.md5(signed_text.encode("utf-8")).hexdigest().upper()
    assert params == expected
    assert "omit" in business and business["omit"] is None
    assert "verify" not in kwargs


def test_gateway_keeps_explicit_timeout_and_business_metadata_overrides(fake_gateway):
    post, replies = fake_gateway
    replies.append({})
    pdd_client.call_pdd_api("client", "secret", "pdd.test", {"timestamp": 5, "data_type": "JSON"}, timeout=(1, 9))
    assert post.call_args.kwargs["timeout"] == (1, 9)
    assert post.call_args.kwargs["json"]["timestamp"] == "5"


@pytest.mark.parametrize("error", [requests.Timeout("timed out"), requests.HTTPError("bad status"), ValueError("bad JSON")])
def test_gateway_wraps_network_status_and_json_errors(fake_gateway, monkeypatch, error):
    post, replies = fake_gateway
    if isinstance(error, requests.Timeout):
        replies.append(error)
    else:
        response = Mock()
        if isinstance(error, requests.HTTPError):
            response.raise_for_status.side_effect = error
        else:
            response.json.side_effect = error
        monkeypatch.setattr(pdd_client._PDD_SESSION, "post", Mock(return_value=response))
    with pytest.raises(RuntimeError, match=f"拼多多网络请求层崩溃: {error}"):
        pdd_client.call_pdd_api("client", "secret", "pdd.test")


def test_gateway_preserves_business_error_details(fake_gateway):
    _, replies = fake_gateway
    replies.append({"error_response": {"error_code": 60001, "sub_code": "AUTH", "error_msg": "未备案", "sub_msg": "请授权"}})
    with pytest.raises(RuntimeError, match=r"PDD网关拒绝: \[60001:AUTH\] 未备案 - 请授权"):
        pdd_client.call_pdd_api("client", "secret", "pdd.test")


@pytest.mark.parametrize("payload,expected", [
    (None, None), ([], None), ({}, None),
    ({"mobile_short_url": "mobile", "short_url": "short", "url": "long"}, "mobile"),
    ({"mobile_short_url": "", "short_url": "short", "url": "long"}, "short"),
    ({"url": "long"}, "long"),
])
def test_short_link_priority(payload, expected):
    assert pdd_client.extract_short_url(payload) == expected


def test_goods_cleaning_keeps_fen_amounts_and_commission_truncation():
    raw = {"goods_id": 42, "goods_sign": "SIGN", "min_group_price": 1234, "coupon_discount": 125, "promotion_rate": 175}
    cleaned = pdd_client.clean_goods_item(raw, "detail_api")
    assert cleaned["min_group_price"] == 1234
    assert cleaned["min_group_price_yuan"] == 12.34
    assert cleaned["min_normal_price"] == 1234
    assert cleaned["coupon_discount"] == 125
    assert cleaned["coupon_discount_yuan"] == 1.25
    assert cleaned["estimated_commission"] == 194
    assert cleaned["estimated_commission_yuan"] == 1.94
    assert cleaned["promotion_rate"] == 175
    assert cleaned["_raw_data"] is raw
    assert cleaned["_source_api"] == "detail_api"
    assert pdd_client.clean_goods_item({"min_group_price": 100})["estimated_commission"] == 0
    assert pdd_client.clean_goods_item({}) is None
    assert pdd_client.clean_goods_item([]) is None


@pytest.mark.parametrize("custom,uid", [
    ('{"uid":"0001"}', "0001"), ('{"uid":0}', "0"), ("raw-uid", "raw-uid"),
    ('{"x":1}', '{"x":1}'), ('["uid"]', '["uid"]'), ("", None),
])
@pytest.mark.parametrize("status", [2, 3, 4, 5])
def test_order_cleaning_preserves_attribution_money_and_status(custom, uid, status):
    raw = {"custom_parameters": custom, "order_status": status, "goods_price": 1234,
           "order_amount": 2468, "promotion_amount": 135, "promotion_rate": 175, "p_id": "pid"}
    order = pdd_client.clean_order_item(raw)
    assert order["promoter_uid"] == uid
    assert order["is_self_operated"] is (not bool(uid))
    assert order["goods_price_yuan"] == 12.34
    assert order["order_amount_yuan"] == 24.68
    assert order["promotion_amount_yuan"] == 1.35
    assert order["promotion_rate"] == 17.5
    assert order["is_refund"] is (status == 4)
    assert order["is_settled"] is (status in [3, 5])
    assert order["pid"] == "pid"


def test_authority_helpers_use_local_gateway_and_forward_uid(fake_gateway):
    post, replies = fake_gateway
    replies.extend([{"authority_query_response": {"bind": 1, "request_id": "request"}},
                    {"rp_promotion_url_generate_response": {"url_list": [{"short_url": "auth"}]}}])
    assert pdd_client.check_member_authority("c", "s", "pid", " 007 ") == {
        "status": "success", "bind": 1, "is_authorized": True, "request_id": "request"}
    first_params = post.call_args.kwargs["json"]
    assert first_params["type"] == "pdd.ddk.member.authority.query"
    assert first_params["custom_parameters"] == '{"uid":"007"}'
    assert pdd_client.generate_authority_url("c", "s", "pid", "007", generate_we_app=False) == {
        "status": "success", "url_info": {"short_url": "auth"}}
    second_params = post.call_args.kwargs["json"]
    assert second_params["p_id_list"] == '["pid"]'
    assert second_params["channel_type"] == "10"
    assert second_params["generate_we_app"] == "false"


def test_authority_failures_preserve_error_shapes(fake_gateway):
    _, replies = fake_gateway
    replies.extend([requests.Timeout("offline"), {}, requests.Timeout("offline")])
    assert pdd_client.check_member_authority("c", "s", "pid", "uid")["is_authorized"] is False
    assert pdd_client.generate_authority_url("c", "s", "pid")["error_msg"] == "接口调用成功但未返回可用链接"
    assert pdd_client.generate_authority_url("c", "s", "pid")["error_msg"].startswith("生成备案链接失败:")


def test_convert_and_promotion_use_expected_response_envelopes(fake_gateway):
    post, replies = fake_gateway
    raw = {"mobile_short_url": "mobile", "short_url": "short"}
    replies.append({"goods_zs_unit_generate_response": raw})
    assert pdd_client.convert_pdd_url("c", "s", "pid", "source", uid=7) == {
        "status": "success", "jump_url": "mobile", "raw_data": raw}
    assert post.call_args.kwargs["json"]["source_url"] == "source"
    replies.append({"goods_promotion_url_generate_response": {"goods_promotion_url_list": [{"short_url": "promotion"}]}})
    assert pdd_client.generate_goods_promotion_url("c", "s", "pid", "SIGN", uid=7, search_id="SEARCH") == {
        "status": "success", "data": {"short_url": "promotion"}}
    params = post.call_args.kwargs["json"]
    assert params["goods_sign_list"] == '["SIGN"]'
    assert params["custom_parameters"] == '{"uid":"7"}'
    assert params["search_id"] == "SEARCH"
    assert params["generate_we_app"] == "true"


@pytest.mark.parametrize("helper,args,prefix", [
    ("convert_pdd_url", ("c", "s", "pid", "source"), "转链失败:"),
    ("generate_goods_promotion_url", ("c", "s", "pid", "SIGN"), "生成商品推广链接异常:"),
])
def test_link_errors_return_error_objects(fake_gateway, helper, args, prefix):
    _, replies = fake_gateway
    replies.append(requests.Timeout("offline"))
    result = getattr(pdd_client, helper)(*args)
    assert result["status"] == "error"
    assert result["error_msg"].startswith(prefix)


def test_smart_promotion_degrades_cash_gift_then_red_packet_then_standard(fake_gateway):
    post, replies = fake_gateway
    replies.extend([requests.Timeout("cash gift unavailable"), {"error_response": {"error_code": 99999}},
                    {"goods_promotion_url_generate_response": {"goods_promotion_url_list": [{"short_url": "standard"}]}}])
    result = pdd_client.generate_smart_promotion_url("c", "s", "pid", "SIGN", cash_gift_id="123", uid="007", search_id="SEARCH")
    assert result == {"status": "success", "link_type": "常规推广", "data": {"short_url": "standard"}}
    params = [call.kwargs["json"] for call in post.call_args_list]
    assert [item["type"] for item in params] == [
        "pdd.ddk.goods.promotion.url.generate", "pdd.ddk.rp.prom.url.generate", "pdd.ddk.goods.promotion.url.generate"]
    assert params[0]["cash_gift_id"] == "123"
    assert params[1]["channel_type"] == "23"
    assert json.loads(params[1]["diy_sp_red_packet_param"]) == {"goods_sign": "SIGN"}
    assert "search_id" not in params[1]
    assert all(item["custom_parameters"] == '{"uid":"007"}' for item in params)


@pytest.mark.parametrize("cash_gift,reply,expected", [
    (5, {"goods_promotion_url_generate_response": {"goods_promotion_url_list": [{"short_url": "gift"}]}}, "多多礼金秒杀"),
    (None, {"rp_promotion_url_generate_response": {"url_list": [{"short_url": "red"}]}}, "官方超级红包"),
])
def test_smart_promotion_stops_at_first_available_strategy(fake_gateway, cash_gift, reply, expected):
    post, replies = fake_gateway
    replies.append(reply)
    result = pdd_client.generate_smart_promotion_url("c", "s", "pid", "SIGN", cash_gift_id=cash_gift)
    assert result["link_type"] == expected
    assert post.call_count == 1


def test_goods_sign_detail_uses_cleaner_and_forwards_kwargs(fake_gateway):
    post, replies = fake_gateway
    raw = {"goods_sign": "SIGN", "min_group_price": 1000}
    replies.append({"goods_detail_response": {"goods_details": [raw]}})
    result = pdd_client.get_goods_detail("c", "s", "pid", goods_sign="SIGN", uid="007", test_flag=True)
    assert result["status"] == "success"
    assert result["data"]["_source_api"] == "detail_api"
    assert result["data"]["min_group_price"] == 1000
    assert post.call_args.kwargs["json"]["test_flag"] == "true"


def test_goods_id_detail_keeps_short_link_probe_strategy(fake_gateway):
    post, replies = fake_gateway
    replies.extend([{"goods_zs_unit_generate_response": {"short_url": "probe"}},
                    {"goods_search_response": {"goods_list": [{"goods_id": 42, "goods_sign": "SIGN", "min_group_price": 1000}]}}])
    result = pdd_client.get_goods_detail("c", "s", "pid", goods_id=42, uid="007")
    assert result["status"] == "success"
    assert result["data"]["_source_api"] == "search_api_probe"
    params = [call.kwargs["json"] for call in post.call_args_list]
    assert params[0]["source_url"] == "https://mobile.pinduoduo.com/goods.html?goods_id=42"
    assert params[1]["type"] == "pdd.ddk.goods.search"
    assert params[1]["keyword"] == "probe"
    assert params[1]["page_size"] == "10"
    assert params[1]["custom_parameters"] == '{"uid":"007"}'


def test_goods_detail_failure_paths_remain_non_throwing(fake_gateway):
    _, replies = fake_gateway
    assert pdd_client.get_goods_detail("c", "s")["error_msg"] == "必须提供 goods_sign 或 goods_id 其中之一"
    replies.extend([{}, requests.Timeout("offline"), {}])
    assert "Detail 接口未找到商品" in pdd_client.get_goods_detail("c", "s", goods_sign="SIGN")["error_msg"]
    assert "Detail 查询失败:" in pdd_client.get_goods_detail("c", "s", goods_sign="SIGN")["error_msg"]
    assert pdd_client.get_goods_detail("c", "s", goods_id=42)["error_msg"] == "通过 goods_id 生成探针短链失败"


def test_increment_orders_uses_local_cleaner_and_original_gateway_parameters(fake_gateway):
    post, replies = fake_gateway
    replies.append({"order_list_get_response": {"total_count": 51, "order_list": [{
        "order_sn": "ORDER", "custom_parameters": '{"uid":"007"}', "order_status": 3,
        "order_amount": 1234, "promotion_amount": 105,
    }]}})
    result = pdd_client.query_increment_orders("c", "s", "100", "200", page="2", page_size="50", custom_key=False)
    assert result["status"] == "success"
    assert result["total_count"] == 51
    assert result["order_list"][0]["promoter_uid"] == "007"
    assert result["order_list"][0]["is_settled"] is True
    assert result["order_list"][0]["order_amount_yuan"] == 12.34
    assert result["order_list"][0]["promotion_amount_yuan"] == 1.05
    params = post.call_args.kwargs["json"]
    assert params["type"] == "pdd.ddk.order.list.increment.get"
    assert params["start_update_time"] == "100"
    assert params["end_update_time"] == "200"
    assert params["page"] == "2"
    assert params["page_size"] == "50"
    assert params["return_count"] == "true"
    assert params["custom_key"] == "false"


def test_increment_orders_failure_preserves_empty_list(fake_gateway):
    _, replies = fake_gateway
    replies.append(requests.Timeout("offline"))
    result = pdd_client.query_increment_orders("c", "s", 100, 200)
    assert result["status"] == "error"
    assert result["order_list"] == []
    assert result["error_msg"].startswith("增量订单查询失败:")
