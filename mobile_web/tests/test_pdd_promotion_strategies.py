"""PDD 策略与个人/通用边界回归；真实策略函数只连接 mock 网关。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from adapters import pdd_adapter
from infrastructure import pdd_client
from services import promote_service


UID = "u_1234abcd"
SIGN = "SIGNED_PRODUCT"
SOURCE = "https://mobile.pinduoduo.com/goods.html?goods_id=123"
PROMOTION = "https://mobile.yangkeduo.com/promotion/mock"
RED_API = "pdd.ddk.rp.prom.url.generate"
STANDARD_API = "pdd.ddk.goods.promotion.url.generate"
WASH_API = "pdd.ddk.goods.zs.unit.url.gen"


@pytest.fixture
def strategy(monkeypatch):
    replies = []

    def gateway(client_id, client_secret, api_type, business_params=None, **kwargs):
        assert replies, f"unexpected extra strategy call: {api_type}"
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    api = Mock(side_effect=gateway)
    smart = Mock(wraps=pdd_client.generate_smart_promotion_url)
    standard = Mock(wraps=pdd_client.generate_goods_promotion_url)
    wash = Mock(wraps=pdd_client.convert_pdd_url)
    monkeypatch.setattr(pdd_client, "call_pdd_api", api)
    monkeypatch.setattr(pdd_client, "generate_goods_promotion_url", standard)
    monkeypatch.setattr(pdd_adapter, "generate_smart_promotion_url", smart)
    monkeypatch.setattr(pdd_adapter, "convert_pdd_url", wash)
    adapter = pdd_adapter.PddAdapter(client_id="offline", client_secret="offline", pid="mock_pid", enabled=True)
    yield SimpleNamespace(adapter=adapter, api=api, smart=smart, standard=standard, wash=wash, replies=replies)
    assert replies == [], "some expected gateway calls did not run"


def assert_calls(strategy, api_types, uid):
    assert [call.args[2] for call in strategy.api.call_args_list] == api_types
    for call in strategy.api.call_args_list:
        custom = call.args[3].get("custom_parameters")
        assert (json.loads(custom) if custom else None) == ({"uid": uid} if uid else None)


def test_pdd_declares_authorization_without_new_identity_fields(strategy):
    assert strategy.adapter.requires_authorized_promotion is True
    assert strategy.adapter.required_identity_fields == ()


@pytest.mark.parametrize("uid", [None, UID])
def test_real_smart_red_packet_success_stops_without_standard_or_wash(strategy, uid):
    strategy.replies.append({"rp_promotion_url_generate_response": {"url_list": [{"short_url": PROMOTION}]}})
    result = strategy.adapter.convert_promotion_url("123", uid=uid)
    assert result["final_link"] == PROMOTION
    assert result["is_fallback"] is False
    assert result["need_auth"] is False
    assert promote_service.PromoteService._cacheable(result)
    strategy.smart.assert_called_once()
    strategy.standard.assert_not_called()
    strategy.wash.assert_not_called()
    assert_calls(strategy, [RED_API], uid)


@pytest.mark.parametrize("uid", [None, UID])
def test_real_smart_uses_standard_once_and_strips_internal_identity(strategy, uid):
    strategy.replies.extend([
        requests.Timeout("red packet timeout"),
        {"goods_promotion_url_generate_response": {"goods_promotion_url_list": [{"short_url": PROMOTION}]}},
    ])
    result = strategy.adapter.convert_promotion_url("123", uid=uid, identity={}, search_id="SEARCH")
    assert result["final_link"] == PROMOTION
    assert result["link_type"] == "常规推广"
    strategy.smart.assert_called_once()
    strategy.standard.assert_called_once()
    strategy.wash.assert_not_called()
    assert "identity" not in strategy.smart.call_args.kwargs
    assert strategy.api.call_args_list[1].args[3]["search_id"] == "SEARCH"
    assert_calls(strategy, [RED_API, STANDARD_API], uid)


def test_personal_all_fail_runs_one_smart_one_standard_one_numeric_wash_and_no_anonymous_retry(strategy):
    strategy.replies.extend([{}, {}, {}])
    result = strategy.adapter.convert_promotion_url("123", uid=UID)
    assert result["status"] == "error"
    assert result["error_code"] == "PERSONAL_PROMOTION_UNAVAILABLE"
    assert result["need_auth"] is False
    assert "auth_platform" not in result
    assert "final_link" not in result
    strategy.smart.assert_called_once()
    strategy.standard.assert_called_once()
    strategy.wash.assert_called_once()
    assert strategy.wash.call_args.kwargs["source_url"] == SOURCE
    assert_calls(strategy, [RED_API, STANDARD_API, WASH_API], UID)


def test_personal_sign_failure_never_synthesizes_anonymous_success_or_direct_link(strategy):
    strategy.replies.extend([{}, {}])
    result = strategy.adapter.convert_promotion_url(SIGN, uid=UID)
    assert result["status"] == "error"
    assert result["error_code"] == "PERSONAL_PROMOTION_UNAVAILABLE"
    assert result["need_auth"] is False
    assert not promote_service.PromoteService._cacheable(result)
    strategy.smart.assert_called_once()
    strategy.standard.assert_called_once()
    strategy.wash.assert_not_called()
    assert_calls(strategy, [RED_API, STANDARD_API], UID)


def test_smart_preserves_intermediate_authorization_evidence_when_last_failure_is_generic(strategy):
    strategy.replies.extend([RuntimeError("PDD网关拒绝: [60001:] 未传入已经授权备案过的相关参数"), {}, {}])
    result = strategy.adapter.convert_promotion_url("123", uid=UID)
    assert result["status"] == "error"
    assert result["need_auth"] is True
    assert result["error_code"] == "AUTH_REQUIRED"
    assert result["auth_platform"] == "pdd"
    assert "auth_msg" in result
    assert_calls(strategy, [RED_API, STANDARD_API, WASH_API], UID)
    strategy.standard.assert_called_once()
    strategy.wash.assert_called_once()


def test_smart_returns_intermediate_error_details_only_when_all_strategies_fail(strategy):
    strategy.replies.extend([RuntimeError("PDD网关拒绝: [60001:] 未备案"), {}])
    result = pdd_client.generate_smart_promotion_url("offline", "offline", "mock_pid", SIGN, uid=UID)
    assert result["status"] == "error"
    assert result["strategy_errors"] == ["PDD网关拒绝: [60001:] 未备案"]
    assert_calls(strategy, [RED_API, STANDARD_API], UID)


def test_success_after_intermediate_authorization_error_remains_personal_success(strategy):
    strategy.replies.extend([
        RuntimeError("PDD网关拒绝: [60001:] 未备案"),
        {"goods_promotion_url_generate_response": {"goods_promotion_url_list": [{"short_url": PROMOTION}]}},
    ])
    result = strategy.adapter.convert_promotion_url(SIGN, uid=UID)
    assert result["status"] == "success"
    assert result["need_auth"] is False
    assert result["is_fallback"] is False
    assert "strategy_errors" not in result
    assert_calls(strategy, [RED_API, STANDARD_API], UID)


@pytest.mark.parametrize("failure", [
    {"status": "error", "error_code": 60001, "error_msg": "business error"},
    {"status": "error", "error_code": "AUTH_REQUIRED"},
    {"status": "error", "error_msg": "通用业务失败", "sub_msg": "未传入已经授权备案过的相关参数"},
    {"status": "error", "error_msg": "用户未授权"},
    {"status": "error", "error_msg": "未授权"},
    RuntimeError("PDD网关拒绝: [60001:AUTH] 请授权"),
])
def test_confirmed_authorization_errors_are_structured_without_anonymous_retry(strategy, failure):
    if isinstance(failure, Exception):
        strategy.smart.side_effect = failure
    else:
        strategy.smart.side_effect = None
        strategy.smart.return_value = failure
    result = strategy.adapter.convert_promotion_url(SIGN, uid=UID)
    assert result["status"] == "error"
    assert result["need_auth"] is True
    assert result["error_code"] == "AUTH_REQUIRED"
    assert result["auth_platform"] == "pdd"
    assert not result.get("final_link")
    strategy.smart.assert_called_once()
    strategy.api.assert_not_called()
    strategy.standard.assert_not_called()
    strategy.wash.assert_not_called()


@pytest.mark.parametrize("failure", [
    {"status": "error", "error_msg": "未能生成推广链接"},
    {"status": "error", "error_msg": "AUTH invalid client_id or client_secret"},
    {"status": "error", "error_msg": "应用未授权访问此接口"},
    {"status": "error", "error_msg": "应用未备案"},
    {"status": "error", "error_msg": "商品 ID 60001 无效"},
    {"status": "error", "error_code": 160001, "error_msg": "generic business failure 160001"},
    {"status": "error", "error_msg": "网络连接超时"},
    requests.Timeout("AUTH host unavailable"),
])
def test_generic_network_and_config_errors_do_not_claim_user_is_unauthorized(strategy, failure):
    if isinstance(failure, Exception):
        strategy.smart.side_effect = failure
    else:
        strategy.smart.side_effect = None
        strategy.smart.return_value = failure
    result = strategy.adapter.convert_promotion_url(SIGN, uid=UID)
    assert result["status"] == "error"
    assert result["need_auth"] is False
    assert result["error_code"] == "PERSONAL_PROMOTION_UNAVAILABLE"
    assert "auth_platform" not in result
    assert "auth_msg" not in result
    strategy.smart.assert_called_once()
    strategy.api.assert_not_called()
    strategy.wash.assert_not_called()


def test_real_smart_network_errors_are_not_mistaken_for_missing_authorization(strategy):
    strategy.replies.extend([requests.Timeout("AUTH proxy unavailable"), requests.Timeout("connection timed out")])
    result = strategy.adapter.convert_promotion_url(SIGN, uid=UID)
    assert result["error_code"] == "PERSONAL_PROMOTION_UNAVAILABLE"
    assert result["need_auth"] is False
    assert_calls(strategy, [RED_API, STANDARD_API], UID)


@pytest.mark.parametrize("uid", [None, UID])
def test_sign_only_ignores_unverified_product_url_after_smart_is_exhausted(strategy, uid):
    strategy.replies.extend([{}, {}])
    result = strategy.adapter.convert_promotion_url(SIGN, uid=uid, product_url=SOURCE)
    if uid:
        assert result["status"] == "error"
        assert result["error_code"] == "PERSONAL_PROMOTION_UNAVAILABLE"
        assert not result.get("final_link")
    else:
        assert result["final_link"] == "https://mobile.yangkeduo.com/goods.html?goods_sign=SIGNED_PRODUCT"
        assert result["is_fallback"] is True
    assert not promote_service.PromoteService._cacheable(result)
    assert result["need_auth"] is False
    strategy.smart.assert_called_once()
    strategy.standard.assert_called_once()
    strategy.wash.assert_not_called()
    assert_calls(strategy, [RED_API, STANDARD_API], uid)


@pytest.mark.parametrize("uid", [None, UID])
@pytest.mark.parametrize("goods_sign", [None, SIGN])
def test_numeric_identity_cannot_be_overridden_by_conflicting_product_url(strategy, uid, goods_sign):
    strategy.replies.extend([{}, {}, {"goods_zs_unit_generate_response": {"short_url": PROMOTION}}])
    result = strategy.adapter.convert_promotion_url(
        "123", uid=uid, goods_id="123", goods_sign=goods_sign,
        product_url="https://mobile.pinduoduo.com/goods.html?goods_id=999",
    )
    assert result["final_link"] == PROMOTION
    assert result["is_fallback"] is False
    strategy.smart.assert_called_once()
    strategy.standard.assert_called_once()
    strategy.wash.assert_called_once()
    assert strategy.wash.call_args.kwargs["source_url"] == SOURCE
    assert strategy.api.call_args_list[2].args[3]["source_url"] == SOURCE
    assert_calls(strategy, [RED_API, STANDARD_API, WASH_API], uid)


def test_url_only_conversion_does_not_try_smart_or_standard(strategy):
    strategy.replies.append({"goods_zs_unit_generate_response": {"short_url": PROMOTION}})
    result = strategy.adapter.convert_promotion_url("", product_url=SOURCE, uid=UID)
    assert result["final_link"] == PROMOTION
    strategy.smart.assert_not_called()
    strategy.standard.assert_not_called()
    strategy.wash.assert_called_once()
    assert_calls(strategy, [WASH_API], UID)


def test_wash_authorization_error_is_reported_without_anonymous_retry(strategy):
    strategy.replies.append(RuntimeError("PDD网关拒绝: [60001:] 未备案"))
    result = strategy.adapter.convert_promotion_url("", product_url=SOURCE, uid=UID)
    assert result["error_code"] == "AUTH_REQUIRED"
    assert result["need_auth"] is True
    assert result["auth_platform"] == "pdd"
    assert_calls(strategy, [WASH_API], UID)


@pytest.mark.parametrize("uid", [None, UID])
@pytest.mark.parametrize("source", [
    "https://evilpinduoduo.com/goods/123",
    "https://pinduoduo.com.evil.example/goods/123",
    "file://pinduoduo.com/goods/123",
    "javascript:alert(1)",
    "https://[pinduoduo.com/goods/123",
])
def test_unsupported_url_only_input_is_error_not_direct_link_success(strategy, source, uid):
    result = strategy.adapter.convert_promotion_url("", product_url=source, uid=uid)
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_product_url"
    assert result["need_auth"] is False
    assert not result.get("final_link")
    assert not promote_service.PromoteService._cacheable(result)
    strategy.api.assert_not_called()
    strategy.smart.assert_not_called()
    strategy.standard.assert_not_called()
    strategy.wash.assert_not_called()


@pytest.mark.parametrize("target,source,direct,expected_calls", [
    ("123", None, SOURCE, [RED_API, STANDARD_API, WASH_API]),
    (SIGN, None, "https://mobile.yangkeduo.com/goods.html?goods_sign=SIGNED_PRODUCT", [RED_API, STANDARD_API]),
    ("", SOURCE, SOURCE, [WASH_API]),
])
def test_anonymous_all_fail_preserves_uncacheable_direct_link(strategy, target, source, direct, expected_calls):
    strategy.replies.extend({} for _ in expected_calls)
    result = strategy.adapter.convert_promotion_url(target, product_url=source)
    assert result["status"] == "success"
    assert result["final_link"] == direct
    assert result["is_fallback"] is True
    assert result["cacheable"] is False
    assert result["need_auth"] is False
    assert "auth_platform" not in result
    assert result["link_type"] == "商品直连保底"
    assert not promote_service.PromoteService._cacheable(result)
    assert_calls(strategy, expected_calls, None)


def test_anonymous_direct_fallback_is_not_written_or_reused_by_service(strategy, monkeypatch):
    repo = SimpleNamespace(
        get_product_identity=Mock(return_value=None),
        get_promotion_link_cache=Mock(return_value=None),
        save_promotion_link_cache=Mock(),
    )
    monkeypatch.setattr(promote_service, "get_adapter", lambda platform: strategy.adapter)
    service = promote_service.PromoteService(product_repo=repo)
    strategy.replies.extend({} for _ in range(6))
    for _ in range(2):
        result = service.convert_link("pdd", "123")
        assert result["final_link"] == SOURCE
        assert result["is_fallback"] is True
        assert result["cacheable"] is False
    repo.save_promotion_link_cache.assert_not_called()
    assert strategy.smart.call_count == 2
    assert strategy.standard.call_count == 2
    assert strategy.wash.call_count == 2
    assert_calls(strategy, [RED_API, STANDARD_API, WASH_API] * 2, None)


def test_missing_product_input_does_not_attempt_any_gateway_strategy(strategy):
    result = strategy.adapter.convert_promotion_url("", uid=UID)
    assert result["status"] == "error"
    assert "error_code" not in result
    strategy.smart.assert_not_called()
    strategy.standard.assert_not_called()
    strategy.wash.assert_not_called()
    strategy.api.assert_not_called()
