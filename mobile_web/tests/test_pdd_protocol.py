"""PDD runtime protocol regression checks with exclusively fake requests."""

import hashlib
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


@pytest.mark.parametrize("uid,extra,expected", [
    (None, None, None),
    ("   ", None, None),
    (0, None, '{"uid":"0"}'),
    ("  用户01  ", None, '{"uid":"用户01"}'),
    ("old", {"uid": "new", "渠道": 2}, '{"uid":"new","渠道":2}'),
    (None, {"x": [1, 2]}, '{"x":[1,2]}'),
    (None, "not-a-dict", None),
])
def test_custom_parameters_preserve_uid_unicode_and_overrides(uid, extra, expected):
    assert pdd_client.make_custom_parameters(uid, extra) == expected


def test_gateway_signs_exact_serialization_and_timeout_overrides(monkeypatch, fake_gateway):
    post, replies = fake_gateway
    monkeypatch.setattr(pdd_client.time, "time", lambda: 1234567890.9)
    replies.extend([{"ok": True}, {}])
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


@pytest.mark.parametrize("custom,uid,status", [
    ('{"uid":"0001"}', "0001", 2),
    ('{"uid":0}', "0", 3),
    ("raw-uid", "raw-uid", 4),
    ('{"x":1}', '{"x":1}', 5),
    ('["uid"]', '["uid"]', 2),
    ("", None, 4),
])
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

    post.reset_mock()
    replies.append({"goods_promotion_url_generate_response": {"goods_promotion_url_list": [{"short_url": "gift"}]}})
    gift_res = pdd_client.generate_smart_promotion_url("c", "s", "pid", "SIGN", cash_gift_id=5)
    assert gift_res["link_type"] == "多多礼金秒杀" and post.call_count == 1


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
    err_missing = pdd_client.get_goods_detail("c", "s")
    assert err_missing["error_code"] == "INVALID_ARGUMENT"
    assert err_missing["error_msg"] == "必须提供 goods_sign 或 goods_id 其中之一"
    replies.extend([
        {},
        {"goods_detail_response": {"goods_details": []}},
        requests.Timeout("offline"),
        {},
    ])
    assert pdd_client.get_goods_detail("c", "s", goods_sign="SIGN")["error_code"] == "PROTOCOL_ERROR"
    off_res = pdd_client.get_goods_detail("c", "s", goods_sign="SIGN")
    assert off_res["error_code"] == "PRODUCT_OFF_PROMOTION" and "下架" in off_res["error_msg"]
    net_res = pdd_client.get_goods_detail("c", "s", goods_sign="SIGN")
    assert net_res["error_code"] == "NETWORK_ERROR" and "Detail 查询失败:" in net_res["error_msg"]
    probe_res = pdd_client.get_goods_detail("c", "s", goods_id=42)
    assert probe_res["status"] == "error" and probe_res["error_code"] == "PROMOTION_FAILED"


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


def test_pdd_detail_separates_proven_off_promotion_from_api_and_protocol_errors(fake_gateway):
    from adapters.pdd_adapter import PddAdapter

    _, replies = fake_gateway
    adapter = PddAdapter(client_id="c", client_secret="s", pid="pid")

    # 1. 网关正常返回 200 且 goods_details 为空 -> 100% 确定商品下架/退出推广
    replies.append({"goods_detail_response": {"goods_details": []}})
    off_res = adapter.get_goods_detail(goods_sign="SIGN_OFF")
    assert off_res["status"] == "error"
    assert off_res["error_code"] == "PRODUCT_OFF_PROMOTION"

    # 2. 网关抛出业务/调用错 -> 必须保留原始网关错误码，不能混成下架
    replies.append({"error_response": {"error_code": 50001, "sub_code": "BAD_SIGN", "error_msg": "参数非法"}})
    gw_res = adapter.get_goods_detail(goods_sign="SIGN_BAD")
    assert gw_res["status"] == "error"
    assert gw_res["error_code"] == "50001"

    # 3. 网络超时 -> 保留 NETWORK_ERROR
    replies.append(requests.Timeout("pdd timeout"))
    net_res = adapter.get_goods_detail(goods_sign="SIGN_NET")
    assert net_res["status"] == "error"
    assert net_res["error_code"] == "NETWORK_ERROR"

    # 4. 响应结构损坏 -> 返回 PROTOCOL_ERROR
    replies.append({"goods_detail_response": "unexpected"})
    proto_res = adapter.get_goods_detail(goods_sign="SIGN_PROTO")
    assert proto_res["status"] == "error"
    assert proto_res["error_code"] == "PROTOCOL_ERROR"


def test_pdd_detail_strips_allow_fallback_forwards_test_flag_and_fails_over_on_rate_limit(fake_gateway):
    from adapters.pdd_adapter import PddAdapter

    post, replies = fake_gateway
    adapter = PddAdapter(client_id="c", client_secret="s", pid="pid")

    # 1. allow_fallback 等内部适配器标志不得透传给官方网关，但测试/透传参数如 test_flag 保留
    replies.append({"goods_detail_response": {"goods_details": [{"goods_id": 101, "goods_sign": "SIGN_1", "goods_name": "拼多多抽纸", "min_group_price": 1990}]}})
    ok_res = adapter.get_goods_detail(goods_sign="SIGN_1", allow_fallback=False, test_flag=True)
    assert ok_res["status"] == "success"
    sent_json = post.call_args.kwargs["json"]
    assert "allow_fallback" not in sent_json
    assert sent_json["test_flag"] == "true"

    # 2. pdd.ddk.goods.detail 触发 50002 限流 -> 自动降级 pdd_goods_search_sign_detail (goods_sign_list)
    replies.extend([
        {"error_response": {"error_code": 50002, "error_msg": "请求频次超限"}},
        {"goods_search_response": {"goods_list": [{"goods_id": 102, "goods_sign": "SIGN_RL", "goods_name": "限流降级抽纸", "min_group_price": 2500}]}},
    ])
    rl_res = adapter.get_goods_detail(goods_sign="SIGN_RL")
    assert rl_res["status"] == "success"
    assert rl_res["_provider"] == "pdd_goods_search_sign_detail"
    assert rl_res["data"]["name"] == "限流降级抽纸"
    assert post.call_args.kwargs["json"]["type"] == "pdd.ddk.goods.search"
    assert json.loads(post.call_args.kwargs["json"]["goods_sign_list"]) == ["SIGN_RL"]


def test_pdd_search_failover_hdk_and_recommend_60001_and_service_tags(fake_gateway):
    from adapters.pdd_adapter import PddAdapter
    from infrastructure.hdk_client import HdkClient

    post, replies = fake_gateway
    hdk_get = Mock(return_value=SimpleNamespace(
        status_code=200,
        raise_for_status=Mock(),
        json=lambda: {"code": 200, "data": [{
            "goods_id": "998877",
            "goods_sign": "E9_hdk_sign_1234567890",
            "goods_name": "HDK拼多多抽纸",
            "goods_thumbnail_url": "https://img.pddpic.com/hdk.jpg",
            "min_group_price": "20.00",
            "coupon_discount": "5.00",
            "promotion_rate": "2.88",
        }]},
    ))
    hdk_cli = HdkClient(api_key="hdk_k", http_session=SimpleNamespace(get=hdk_get))
    adapter = PddAdapter(client_id="c", client_secret="s", pid="pid", enabled=True, hdk_client=hdk_cli)

    # 1. search_goods 遇 pageSize < 10 报错时自动重试 page_size=10；触发 50002 限流后降级 HDK 且保留 2.88% 佣金比例
    replies.extend([
        {"error_response": {"error_code": 3000000, "error_msg": "pageSize不能小于10"}},
        {"error_response": {"error_code": 50002, "error_msg": "QPS超限"}},
    ])
    sr = adapter.search_goods("抽纸", page=1, page_size=3)
    assert sr["status"] == "success"
    assert sr["_provider"] == "hdk_pdd_search"
    assert post.call_args_list[-2].kwargs["json"]["page_size"] == "3"
    assert post.call_args.kwargs["json"]["page_size"] == "10"
    assert sr["data"][0]["commission_rate"] == 2.88
    assert sr["data"][0]["price"] == 15.0

    # 2. get_recommend_goods limit 上限 50，并在未备案 uid 触发 60001 时自动无 uid 重试
    replies.extend([
        {"error_response": {"error_code": 60001, "error_msg": "未传入已经授权备案过的相关参数"}},
        {"goods_basic_detail_response": {"list": [{"goods_id": 201, "goods_sign": "SIGN_REC", "goods_name": "推荐好物", "min_group_price": 1990}], "total": 50}},
    ])
    rec = adapter.get_recommend_goods(channel_type=5, offset=0, limit=100, uid="unfiled_u")
    assert rec["status"] == "success" and len(rec["data"]) == 1
    assert post.call_args_list[-2].kwargs["json"]["limit"] == "50"
    assert "custom_parameters" not in post.call_args_list[-1].kwargs["json"]

    # 3. service_tags 映射中文服务保障标签
    det = adapter._normalize_goods({
        "goods_id": 201, "goods_sign": "SIGN_REC", "goods_name": "保障测试",
        "min_group_price_yuan": 19.9,
        "_raw_data": {"goods_id": 201, "goods_sign": "SIGN_REC", "goods_name": "保障测试", "service_tags": [1, 12, 24]},
    })
    assert det["service_guarantees"] == ["包邮", "24小时发货", "极速退款"]


def test_pdd_smart_promotion_we_app_off_promo_and_universal_zs_unit_fallback(fake_gateway):
    from adapters.pdd_adapter import PddAdapter

    post, replies = fake_gateway
    adapter = PddAdapter(client_id="c", client_secret="s", pid="pid", enabled=True)

    # 1. rp.prom[23] 携带 generate_we_app 与 generate_schema_url
    replies.append({"rp_promotion_url_generate_response": {"url_list": [{"short_url": "https://p.pinduoduo.com/hb"}]}})
    ok_conv = adapter.convert_promotion_url("SIGN_HB", title="红包测试")
    assert ok_conv["status"] == "success"
    rp_json = post.call_args.kwargs["json"]
    assert rp_json["generate_we_app"] == "true"
    assert rp_json["generate_schema_url"] == "true"

    # 2. 步 1/2 返回下架错误码 (40001 / 商品已下架) 时立即短路 PRODUCT_OFF_PROMOTION，不降级直连保底
    replies.append({"error_response": {"error_code": 50001, "sub_code": "40001", "error_msg": "商品非多多客商品"}})
    off_conv = adapter.convert_promotion_url("SIGN_OFF_1")
    assert off_conv["status"] == "error" and off_conv["error_code"] == "PRODUCT_OFF_PROMOTION"

    # 3. 严格主机名校验证伪域名被拦截，合法未收录 p.pinduoduo.com 分享短链（非 40009/非多结果）允许 zs.unit 洗链
    bad_host = adapter.resolve_and_convert_universal("https://evilpinduoduo.com/goods/123")
    assert bad_host["status"] == "error" and bad_host["error_code"] == "INVALID_PDD_URL"

    replies.extend([
        {"goods_search_response": {"goods_list": [], "total_count": 0}},
        {"goods_zs_unit_generate_response": {"mobile_short_url": "https://p.pinduoduo.com/washed"}},
    ])
    washed = adapter.resolve_and_convert_universal("https://p.pinduoduo.com/act123")
    assert washed["status"] == "success"
    assert washed["final_link"] == "https://p.pinduoduo.com/washed"
    assert washed["resolution_kind"] == "degraded_product"
    assert washed["detail_lookup_failed"] is True


def test_pdd_40009_bypasses_hdk_search_detail_60001_mismatch_numeric_sign_and_commission_clamp(fake_gateway):
    """验证 PDD 直查保留 40009、sign 搜重试 60001 并拦截串货、纯数字拒入 goods_sign、50001:60001 识别与负佣金钳位。"""
    from adapters.pdd_adapter import PddAdapter

    post, replies = fake_gateway
    hdk_mock = Mock()
    hdk_mock.search_pdd_goods.side_effect = AssertionError("resolve_product_from_link must not hit HDK")
    adapter = PddAdapter(client_id="c", client_secret="s", pid="pid", enabled=True, hdk_client=hdk_mock)

    # 1. resolve_product_from_link 即使配置了 hdk_client 也直连官方保留 40009（绝不被 HDK 吞掉）
    replies.append({"error_response": {"error_code": 50001, "sub_code": "40009", "error_msg": "请传入原始链接(40009)"}})
    res_40009 = adapter.resolve_product_from_link("https://mobile.yangkeduo.com/goods.html?goods_id=833182837312")
    assert res_40009["status"] == "error" and res_40009["error_code"] == "UNSUPPORTED_PDD_ID_ONLY_URL"
    assert hdk_mock.search_pdd_goods.call_count == 0

    # 2. _detail_via_sign_search 遇到 60001 自动脱掉 custom_parameters 重试，且返回行不匹配绝不取 rows[0]；直查返回串货报 product_identity_mismatch
    replies.extend([
        {"error_response": {"error_code": 50001, "sub_code": "429", "error_msg": "限流"}},
        {"error_response": {"error_code": 50001, "sub_code": "60001", "error_msg": "custom_parameters invalid"}},
        {"goods_search_response": {"goods_list": [{"goods_id": 999, "goods_sign": "OTHER_SIGN_ZZZ", "goods_name": "串货商品", "min_group_price": 990}], "total_count": 1}},
    ])
    det = adapter.get_goods_detail("REQ_PDD_SIGN_111", uid="unfiled_u", goods_sign="REQ_PDD_SIGN_111")
    assert det["status"] == "error" and det["error_code"] == "NOT_FOUND"
    assert "custom_parameters" not in post.call_args.kwargs["json"]

    replies.append({
        "goods_detail_response": {"goods_details": [{"goods_id": 999, "goods_sign": "OTHER_SIGN_ZZZ", "goods_name": "串货商品", "min_group_price": 990}]}
    })
    det_mismatch = adapter.get_goods_detail("10001", goods_id="10001", goods_sign="REQ_PDD_SIGN_111")
    assert det_mismatch["status"] == "error" and det_mismatch["error_code"] == "product_identity_mismatch"

    # 3. convert_promotion_url 绝不将纯数字当作 goods_sign 喂给 rp.prom/promotion.url.generate
    before_count = post.call_count
    adapter.convert_promotion_url("", goods_id=None, goods_sign="833182837312")
    assert post.call_count == before_count  # 未发出新的 rp.prom/url.generate 请求

    # 4. _is_unauthorized_error 识别 [50001:60001] 复合错误串；clean_goods_item 对负优惠与 has_coupon=False 钳位
    assert adapter._is_unauthorized_error({"error_msg": "[50001:60001] 推手未备案"}) is True
    assert adapter._is_unauthorized_error({"error_code": "50001", "sub_code": "60001"}) is True
    cleaned = pdd_client.clean_goods_item({
        "goods_id": 1001,
        "goods_sign": "Y_REAL_SIGN_ABC",
        "goods_name": "测试商品",
        "min_group_price": 1000,
        "has_coupon": False,
        "coupon_discount": 500,
        "promotion_rate": 200,
    })
    assert cleaned["estimated_commission_yuan"] == 2.0
    clamped = pdd_client.clean_goods_item({
        "goods_id": 1002,
        "goods_sign": "Y_REAL_SIGN_DEF",
        "goods_name": "大券测试",
        "min_group_price": 1000,
        "has_coupon": True,
        "coupon_discount": 2000,
        "promotion_rate": 200,
    })
    assert clamped["estimated_commission_yuan"] == 0.0



