"""平台共用边界回归：配置、可信渠道身份、推广凭证与HTTP鉴权。"""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from auth import create_access_token
from infrastructure.settings import load_settings, SettingsError
from infrastructure.tb_client import clean_tb_goods_item, call_tb_api, check_publisher_relation
from models.product import ProductRef
from repositories.user_repo import UserRepository
from services.user_service import UserService
from services.promote_service import PromoteService
from services.product_service import ProductService
from services.order_service import OrderService
from tests.offline_support import _CONFIG


UID = "u_1234abcd"


class ChannelAdapter:
    active = True
    platform_code = "fixture"
    platform_name = "测试平台"
    required_identity_fields = ("relation_id",)
    requires_authorized_promotion = True

    def __init__(self):
        self.check_authority = MagicMock(return_value={
            "status": "success", "is_authorized": True, "bind": 1,
            "identity": {"relation_id": "relation-a", "external_id": UID},
        })
        self.generate_authority_url = MagicMock(return_value={"status": "success", "auth_url": "https://example.com/auth"})
        self.convert_promotion_url = MagicMock(return_value={
            "status": "success", "final_link": "https://example.com/promotion",
            "promotion_token": "￥真实凭证￥", "token_type": "tpwd", "expires_at": time.time() + 60,
        })

    def resolve_product_ref(self, raw="", goods_id=None, goods_sign=None):
        return ProductRef("fixture", goods_id or (raw if not goods_sign else ""), goods_sign or "")

    def build_command_text(self, title, link, price=None, coupon=None):
        return f"{title}\n{link}"


def config_file(tmp_path, **values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(dict(_CONFIG, **values)), encoding="utf-8")
    return path


def test_taobao_only_config_preserves_json_credentials_and_derives_adzone(tmp_path):
    path = config_file(tmp_path, pdd_enabled=False, taobao_enabled=True,
                       myself_pdd_client_id="", myself_pdd_client_secret="", myself_pdd_pid="",
                       myself_taobao_app_key="key", myself_taobao_app_secret="tb-private-secret",
                       myself_taobao_pid="mm_123_456_789", taobao_session="private-session",
                       haodanku_api_key="private-hdk", zhetaoke_appkey="private-ztk", zhetaoke_sid="private-sid")
    settings = load_settings(path)
    assert not settings.pdd_enabled and settings.taobao_enabled
    assert settings.tb_adzone_id == "789"
    for secret in ("tb-private-secret", "private-session", "private-hdk", "private-ztk", "private-sid"):
        assert secret not in repr(settings)


def test_explicitly_enabled_incomplete_platform_fails_without_echoing_secrets(tmp_path):
    path = config_file(tmp_path, taobao_enabled=True, myself_taobao_app_secret="must-not-echo")
    with pytest.raises(SettingsError) as error:
        load_settings(path)
    assert "myself_taobao_app_key" in str(error.value)
    assert "must-not-echo" not in str(error.value)


def test_optional_third_party_credentials_do_not_disable_official_catalog(tmp_path):
    settings = load_settings(config_file(tmp_path, taobao_enabled=True, myself_taobao_app_key="key",
                                        myself_taobao_app_secret="secret", taobao_adzone_id="123"))
    assert settings.taobao_enabled and not settings.hdk_api_key and not settings.ztk_appkey


def user_service(monkeypatch, saved=None):
    adapter = ChannelAdapter()
    repo = MagicMock()
    repo.get_user_by_uid.return_value = {"uid": UID, "auth_status": {"fixture": saved or {}}}
    repo.update_auth_status.return_value = True
    order_repo = MagicMock()
    monkeypatch.setattr("services.user_service.get_adapter", lambda platform: adapter)
    return UserService(repo, order_repo), adapter, repo, order_repo


def test_old_authorized_record_without_identity_is_revalidated_and_backfilled(monkeypatch):
    service, adapter, repo, orders = user_service(monkeypatch, {
        "is_authorized": True, "bind": 1, "updated_at": datetime.now(timezone.utc),
    })
    result = service.check_and_get_auth(UID, "fixture")
    assert result["is_authorized"] is True and "identity" not in result
    adapter.check_authority.assert_called_once_with(uid=UID)
    assert repo.update_auth_status.call_args.kwargs["extra"]["identity"]["relation_id"] == "relation-a"
    orders.assign_unmatched_orders.assert_called_once_with("fixture", {"relation_id": "relation-a"}, UID)


def test_fresh_complete_identity_avoids_upstream_and_invite(monkeypatch):
    service, adapter, repo, _ = user_service(monkeypatch, {
        "is_authorized": True, "updated_at": datetime.now(timezone.utc),
        "identity": {"relation_id": "relation-a", "external_id": UID},
    })
    assert service.get_promotion_context(UID, "fixture")["identity"]["relation_id"] == "relation-a"
    adapter.check_authority.assert_not_called()
    adapter.generate_authority_url.assert_not_called()
    repo.update_auth_status.assert_not_called()


@pytest.mark.parametrize("response", [
    {"status": "success", "is_authorized": True, "identity": {}},
    {"status": "success", "is_authorized": True, "identity": {"relation_id": "x", "external_id": "someone-else"}},
    {"status": "error", "error_code": "SESSION_EXPIRED", "error_msg": "平台凭据已失效"},
])
def test_invalid_or_unverified_channel_is_not_saved(monkeypatch, response):
    service, adapter, repo, orders = user_service(monkeypatch)
    adapter.check_authority.return_value = response
    assert service.get_promotion_context(UID, "fixture")["status"] == "error"
    repo.update_auth_status.assert_not_called()
    orders.assign_unmatched_orders.assert_not_called()


def test_confirmed_revocation_clears_identity_without_generating_invite_for_conversion(monkeypatch):
    service, adapter, repo, _ = user_service(monkeypatch)
    adapter.check_authority.return_value = {"status": "success", "is_authorized": False, "bind": 0}
    assert service.get_promotion_context(UID, "fixture")["identity"] == {}
    assert repo.update_auth_status.call_args.kwargs["extra"] == {"identity": {}}
    adapter.generate_authority_url.assert_not_called()


def test_identity_repository_has_unique_partial_index_and_rejects_field_injection():
    db = MagicMock()
    col = db.get_collection.return_value
    col.update_one.return_value = SimpleNamespace(modified_count=1, matched_count=1)
    repo = UserRepository(db_instance=db)
    assert repo.update_auth_status(UID, "tb", True, extra={"identity": {"relation_id": "123"}})
    assert any(call.kwargs.get("partialFilterExpression") == {
        "auth_status.taobao.identity.relation_id": {"$type": "string"}
    } and call.kwargs.get("unique") is True for call in col.create_index.call_args_list)
    with pytest.raises(ValueError):
        repo.update_auth_status(UID, "tb", True, extra={"identity.relation_id": "123"})
    with pytest.raises(ValueError):
        repo.update_auth_status(UID, "tb", True, extra={"identity": {"$where": "bad"}})


def test_identity_reverse_lookup_does_not_choose_an_ambiguous_user():
    db = MagicMock()
    col = db.get_collection.return_value
    repo = UserRepository(db_instance=db)
    col.find.return_value.limit.return_value = [{"uid": UID}, {"uid": "u_deadbeef"}]
    assert repo.get_uid_by_platform_identity("tb", {"relation_id": "123"}) is None
    col.find.return_value.limit.return_value = [{"uid": UID}]
    assert repo.get_uid_by_platform_identity("tb", {"relation_id": "123"}) == UID


def promotion_service(monkeypatch):
    adapter = ChannelAdapter()
    repo = MagicMock()
    repo.get_product_identity.return_value = None
    repo.get_promotion_link_cache.return_value = None
    users = MagicMock()
    users.get_promotion_context.return_value = {"status": "success", "is_authorized": True,
                                                "identity": {"relation_id": "a"}}
    monkeypatch.setattr("services.promote_service.get_adapter", lambda platform: adapter)
    return PromoteService(repo, users), adapter, repo, users


def test_token_survives_cache_but_text_uses_current_request_and_identity(monkeypatch):
    service, adapter, _, users = promotion_service(monkeypatch)
    first = service.convert_link("fixture", goods_id="123", uid=UID, title="第一次")
    second = service.convert_link("fixture", goods_id="123", uid=UID, title="第二次")
    assert "￥真实凭证￥" in first["command_text"] and "￥真实凭证￥" in second["command_text"]
    assert "第二次" in second["command_text"] and "第一次" not in second["command_text"]
    adapter.convert_promotion_url.assert_called_once()
    assert adapter.convert_promotion_url.call_args.kwargs["identity"] == {"relation_id": "a"}
    users.get_promotion_context.return_value["identity"] = {"relation_id": "b"}
    service.convert_link("fixture", goods_id="123", uid=UID)
    assert adapter.convert_promotion_url.call_count == 2


def test_uncacheable_tokens_and_expired_results_do_not_enter_cache(monkeypatch):
    service, adapter, repo, _ = promotion_service(monkeypatch)
    adapter.convert_promotion_url.return_value["cacheable"] = False
    service.convert_link("fixture", goods_id="123")
    service.convert_link("fixture", goods_id="123")
    assert adapter.convert_promotion_url.call_count == 2
    repo.save_promotion_link_cache.assert_not_called()
    adapter.convert_promotion_url.return_value["expires_at"] = time.time() - 1
    assert service.convert_link("fixture", goods_id="123")["status"] == "error"


def test_upstream_authorization_error_cannot_use_previous_promotion_cache(monkeypatch):
    service, adapter, _, users = promotion_service(monkeypatch)
    service.convert_link("fixture", goods_id="123", uid=UID)
    users.get_promotion_context.return_value = {"status": "error", "error_code": "SESSION_EXPIRED"}
    assert service.convert_link("fixture", goods_id="123", uid=UID)["error_code"] == "SESSION_EXPIRED"
    adapter.convert_promotion_url.assert_called_once()


def test_personal_conversion_requires_token_and_rejects_forged_uid(monkeypatch):
    import api.promote as promote_api
    from main import app
    convert = MagicMock(return_value={"status": "success", "final_link": "https://example.com"})
    monkeypatch.setattr(promote_api, "promote_service", SimpleNamespace(convert_link=convert))
    client = TestClient(app)
    payload = {"platform": "pdd", "goods_id": "123", "uid": UID}
    assert client.post("/api/promote/convert", json=payload).status_code == 403
    assert client.post("/api/promote/convert", json=payload, headers={"Authorization": "Bearer invalid"}).status_code == 401
    token = create_access_token(UID)
    assert client.post("/api/promote/convert", json=payload, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert convert.call_args.kwargs["uid"] == UID
    payload["uid"] = "u_deadbeef"
    assert client.post("/api/promote/convert", json=payload, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    payload["uid"] = None
    assert client.post("/api/promote/convert", json=payload).status_code == 200
    assert convert.call_args.kwargs["uid"] is None


@pytest.mark.parametrize("disabled", [True, False])
def test_unavailable_catalog_cannot_bypass_flags_with_direct_api_calls(monkeypatch, disabled):
    adapter = MagicMock()
    adapter.active = not disabled
    adapter.platform_name = "测试平台"
    adapter.capabilities = {"recommend": False, "search": False, "detail": False}
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: adapter)
    service = ProductService(MagicMock())
    assert service.get_home_recommendations(platform="fixture")["status"] == "error"
    assert service.search_home_goods("商品", platform="fixture")["status"] == "error"
    assert service.get_goods_detail(platform="fixture", goods_id="123")["status"] == "error"
    adapter.get_recommend_goods.assert_not_called()
    adapter.search_goods.assert_not_called()
    adapter.get_goods_detail.assert_not_called()


def test_unconfigured_orders_do_not_start_sync_or_query_repository(monkeypatch):
    adapter = SimpleNamespace(capabilities={"orders": False}, platform_name="测试平台")
    monkeypatch.setattr("services.order_service.require_adapter", lambda platform: adapter)
    orders, sync, users = MagicMock(), MagicMock(), MagicMock()
    result = OrderService(orders, sync, users).sync_and_query_orders("fixture", UID)
    assert result["error_code"] == "capability_unavailable"
    sync.get_state.assert_not_called()
    orders.query_user_orders.assert_not_called()


def test_catalog_cursor_is_opaque_but_part_of_cache_scope(monkeypatch):
    adapter = SimpleNamespace(active=True, platform_name="测试平台", channels=(), capabilities=None,
                              get_recommend_goods=MagicMock(return_value={"status": "success", "data": [], "has_more": False}))
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: adapter)
    service = ProductService(MagicMock())
    service.get_home_recommendations(platform="fixture", cursor={"page": 2})
    service.get_home_recommendations(platform="fixture", cursor={"page": 2})
    service.get_home_recommendations(platform="fixture", cursor={"page": 3})
    assert adapter.get_recommend_goods.call_count == 2
    assert adapter.get_recommend_goods.call_args.kwargs["cursor"] == {"page": 3}


@pytest.mark.parametrize("price", [{}, {"final_promotion_price": "NaN"}, {"final_promotion_price": "bad"},
                                   {"final_promotion_price": -1}, {"final_promotion_price": True}])
def test_unknown_or_invalid_official_prices_do_not_become_zero_yuan_products(price):
    assert clean_tb_goods_item({"item_id": "opaque", "item_basic_info": {"title": "商品"},
                                "price_promotion_info": price}) is None


def test_explicit_zero_and_opaque_input_proof_are_preserved_without_fake_raw_id():
    item = clean_tb_goods_item({"item_id": "opaque", "input_item_iid": "original-A",
                               "item_basic_info": {"title": "商品"},
                               "price_promotion_info": {"final_promotion_price": "0"}})
    assert item["price"] == 0
    assert item["goods_id"] == "" and item["goods_sign"] == "opaque"
    assert item["input_item_iid"] == "original-A"
    assert clean_tb_goods_item({"item_id": "opaque", "item_basic_info": ["invalid"]}) is None


def test_empty_channel_count_is_authoritative_but_partial_or_unverified_records_are_not():
    api = MagicMock()
    def respond(data):
        api.return_value = {"status": "success", "data": {"tbk_sc_publisher_info_get_response": {"data": data}}}
        return check_publisher_relation("key", "secret", UID, "session", api)
    assert respond({"total_count": 0})["is_authorized"] is False
    assert respond({"total_count": 2, "inviter_list": {"map_data": [
        {"relation_id": "a", "external_id": UID}
    ]}})["error_code"] == "INCOMPLETE_IDENTITY"
    assert respond({"inviter_list": {"map_data": [{"relation_id": "a"}]}})["error_code"] == "IDENTITY_MISMATCH"


def test_top_business_errors_cannot_echo_credentials():
    http = MagicMock()
    http.post.return_value.status_code = 200
    http.post.return_value.json.return_value = {"error_response": {
        "code": 15, "msg": "bad session secret-session / secret-app-key / secret-value",
    }}
    response = call_tb_api("secret-app-key", "secret-value", "test.read", session_key="secret-session", http_session=http)
    for secret in ("secret-session", "secret-app-key", "secret-value"):
        assert secret not in json.dumps(response)
    assert response["status"] == "error"
    http.post.return_value.json.return_value = {"error_response": []}
    assert call_tb_api("key", "secret", "test.read", http_session=http)["error_code"] == "PROTOCOL_ERROR"


def test_html_revalidates_and_versioned_assets_resolve_without_external_calls():
    from main import app
    client = TestClient(app)
    for path in ("/", "/detail"):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-cache"
        assert "session.js?v=" in response.text
    assert client.get("/static/js/session.js?v=offline-check").status_code == 200


def test_catalog_query_uid_is_not_a_personal_identity(monkeypatch):
    import api.home as home_api
    from main import app
    products = SimpleNamespace(get_home_recommendations=MagicMock(return_value={"status": "success", "data": []}))
    monkeypatch.setattr(home_api, "product_service", products)
    response = TestClient(app).get(f"/api/home/recommend?uid={UID}&cursor=%7B%7D")
    assert response.status_code == 200
    assert products.get_home_recommendations.call_args.kwargs["uid"] is None
    assert products.get_home_recommendations.call_args.kwargs["cursor"] == "{}"
