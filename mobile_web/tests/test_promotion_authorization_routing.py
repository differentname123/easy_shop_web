"""Local platform qualification, public-cache reuse and explicit authorization checks; no network."""
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from adapters.base import UnavailablePlatformAdapter
from auth import create_access_token
from cache import PROMOTION_CACHE
from config import AUTH_STATUS_TTL, AUTH_NEGATIVE_STATUS_TTL
from infrastructure.tb_client import TaobaoClient
from services.promote_service import PromoteService
from services.user_service import UserService

UID = "u_12345678"
OTHER_UID = "u_87654321"


def authorized(uid=UID, relation="relation-a", age=0):
    return {"is_authorized": True, "updated_at": time.time() - age,
            "identity": {"external_id": uid, "relation_id": relation}}


def public_link(**extra):
    return {"status": "success", "final_link": "https://example.com/public", "promotion_token": "￥PUBLIC￥",
            "attribution_status": "anonymous", "is_fallback": False, "need_auth": False,
            "expires_at": time.time() + 3600, **extra}


class RoutedAdapter(UnavailablePlatformAdapter):
    active = True
    requires_authorized_promotion = True
    required_identity_fields = ("relation_id",)

    def __init__(self, platform):
        self.platform_code = platform
        self.platform_name = "淘宝" if platform == "taobao" else "拼多多"
        if platform == "pdd":
            self.required_identity_fields = ()
        self.convert_promotion_url = Mock(side_effect=self.promote)
        self.check_authority = Mock(return_value={"status": "success", "is_authorized": False, "bind": 0})
        self.generate_authority_url = Mock(return_value={"status": "success", "auth_url": "https://example.com/auth"})

    @staticmethod
    def promote(*, uid=None, identity=None, **kwargs):
        if not uid:
            return public_link()
        return {"status": "success", "final_link": f"https://example.com/{uid}/{identity.get('relation_id', '')}",
                "attribution_status": "verified", "need_auth": False, "is_fallback": False}


@pytest.fixture
def routing(monkeypatch):
    adapters = {platform: RoutedAdapter(platform) for platform in ("taobao", "pdd")}
    monkeypatch.setattr("services.user_service.get_adapter", adapters.__getitem__)
    monkeypatch.setattr("services.promote_service.get_adapter", adapters.__getitem__)
    profiles = {uid: {"uid": uid, "auth_status": {}} for uid in (UID, OTHER_UID)}
    users = Mock()
    users.get_user_by_uid.side_effect = lambda uid: deepcopy(profiles.get(uid))

    def update(uid, platform, is_authorized, bind, extra=None):
        profiles[uid]["auth_status"][platform] = {
            "is_authorized": is_authorized, "bind": bind, "updated_at": time.time(), **(extra or {})}
        return True

    users.update_auth_status.side_effect = update
    products, saved = Mock(), {}
    products.get_product_identity.return_value = None
    products.get_promotion_link_cache.side_effect = lambda platform, **kwargs: deepcopy(saved.get(platform))

    def save(platform, data, expires_at, **kwargs):
        saved[platform] = deepcopy(data) | {"_expires_at": expires_at}
        return True

    products.save_promotion_link_cache.side_effect = save
    user_service = UserService(users, order_repo=Mock())
    service = PromoteService(products, user_service)
    return SimpleNamespace(service=service, users=user_service, user_repo=users, products=products,
                           profiles=profiles, adapters=adapters, saved=saved)


@pytest.mark.parametrize("platform", ["taobao", "pdd"])
@pytest.mark.parametrize("state", ["missing", "negative", "expired_negative", "other_platform", "incomplete"])
def test_nonqualified_conversion_uses_public_db_without_authorization_or_promotion_network(routing, platform, state):
    statuses = routing.profiles[UID]["auth_status"]
    if state == "negative":
        statuses[platform] = {"is_authorized": False, "updated_at": time.time()}
    elif state == "expired_negative":
        statuses[platform] = {"is_authorized": False, "updated_at": time.time() - AUTH_NEGATIVE_STATUS_TTL - 1}
    elif state == "other_platform":
        statuses["pdd" if platform == "taobao" else "taobao"] = authorized()
    elif state == "incomplete":
        statuses[platform] = {"is_authorized": True}
    before = deepcopy(statuses)
    routing.saved[platform] = public_link(_expires_at=time.time() + 300)
    result = routing.service.convert_link(platform, "123", uid=UID)
    assert result["status"] == "success" and result["need_auth"] is True
    assert result["auth_platform"] == platform and result["attribution_status"] == "anonymous"
    assert result["final_link"] == "https://example.com/public"
    routing.adapters[platform].check_authority.assert_not_called()
    routing.adapters[platform].convert_promotion_url.assert_not_called()
    routing.user_repo.update_auth_status.assert_not_called()
    assert statuses == before


def test_positive_authorization_persists_indefinitely_without_artificial_expiration(routing):
    # 正向授权不设人工定时过期（如 30 天前的授权依然有效），离线直接转链无需查网
    routing.profiles[UID]["auth_status"]["taobao"] = authorized(age=30 * 86400)
    result = routing.service.convert_link("taobao", "123", uid=UID)
    assert result["status"] == "success"
    assert result["need_auth"] is False
    assert result["attribution_status"] == "verified"
    assert f"https://example.com/{UID}/relation-a" in result["final_link"]
    routing.adapters["taobao"].check_authority.assert_not_called()
    routing.adapters["taobao"].convert_promotion_url.assert_called_once()
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["uid"] == UID
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["identity"]["relation_id"] == "relation-a"


@pytest.mark.parametrize("platform", ["taobao", "pdd"])
def test_public_generation_shared_by_two_accounts_and_guest_without_persisting_user_hints(routing, platform):
    first = routing.service.convert_link(platform, "123", uid=UID, title="第一份文案")
    second = routing.service.convert_link(platform, "123", uid=OTHER_UID, title="第二份文案")
    guest = routing.service.convert_link(platform, "123", title="游客文案")
    assert first["need_auth"] is True and second["need_auth"] is True
    assert not guest["need_auth"] and "auth_platform" not in guest and "auth_msg" not in guest
    assert "第二份文案" in second["command_text"] and "第一份文案" not in second["command_text"]
    assert "￥PUBLIC￥" in guest["command_text"]
    adapter = routing.adapters[platform]
    adapter.convert_promotion_url.assert_called_once()
    assert adapter.convert_promotion_url.call_args.kwargs["uid"] is None
    assert adapter.convert_promotion_url.call_args.kwargs["identity"] == {}
    assert not routing.saved[platform]["is_fallback"] and not routing.saved[platform]["need_auth"]
    assert "auth_msg" not in routing.saved[platform] and "command_text" not in routing.saved[platform]
    deadline = routing.saved[platform]["_expires_at"]
    PROMOTION_CACHE.clear()
    result = routing.service.convert_link(platform, "123", uid=OTHER_UID)
    assert result["need_auth"] and result["final_link"] == first["final_link"]
    assert routing.saved[platform]["_expires_at"] == deadline
    adapter.convert_promotion_url.assert_called_once()
    routing.products.save_promotion_link_cache.assert_called_once()
    assert routing.products.get_promotion_link_cache.call_count == 2


def test_personal_caches_follow_platform_identity_and_never_write_public_database(routing):
    for uid in (UID, OTHER_UID):
        routing.profiles[uid]["auth_status"]["taobao"] = authorized(uid)
    first = routing.service.convert_link("taobao", "123", uid=UID)
    other = routing.service.convert_link("taobao", "123", uid=OTHER_UID)
    assert first["final_link"] != other["final_link"]
    assert routing.service.convert_link("taobao", "123", uid=UID)["final_link"] == first["final_link"]
    routing.profiles[UID]["auth_status"]["taobao"] = authorized(relation="relation-b")
    changed = routing.service.convert_link("taobao", "123", uid=UID, identity={"relation_id": "forged"}, relation_id="forged")
    assert changed["final_link"] != first["final_link"]
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["identity"]["relation_id"] == "relation-b"
    assert "relation_id" not in routing.adapters["taobao"].convert_promotion_url.call_args.kwargs
    assert routing.adapters["taobao"].convert_promotion_url.call_count == 3
    routing.products.get_promotion_link_cache.assert_not_called()
    routing.products.save_promotion_link_cache.assert_not_called()
    routing.adapters["taobao"].check_authority.assert_not_called()


def test_revoked_or_expired_authorization_does_not_reuse_warm_personal_link(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized()
    personal = routing.service.convert_link("taobao", "123", uid=UID)
    routing.profiles[UID]["auth_status"]["taobao"] = {"is_authorized": False, "updated_at": time.time()}
    generic = routing.service.convert_link("taobao", "123", uid=UID)
    assert generic["final_link"] != personal["final_link"] and generic["need_auth"]
    assert generic["authorization_status"] == "unauthorized"
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["uid"] is None
    routing.adapters["taobao"].check_authority.assert_not_called()
    routing.user_repo.update_auth_status.assert_not_called()


@pytest.mark.parametrize("identity", [{"external_id": OTHER_UID, "relation_id": "x"}, "invalid"])
def test_corrupt_or_mismatched_saved_identity_is_error_not_generic(routing, identity):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized() | {"identity": identity}
    assert routing.service.convert_link("taobao", "123", uid=UID)["status"] == "error"
    routing.adapters["taobao"].convert_promotion_url.assert_not_called()
    routing.products.get_promotion_link_cache.assert_not_called()


def test_negative_cache_force_and_check_only_have_distinct_responsibilities(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = {"is_authorized": False, "updated_at": time.time()}
    adapter = routing.adapters["taobao"]
    ordinary = routing.users.check_and_get_auth(UID, "taobao")
    assert ordinary["auth_url"] == "https://example.com/auth"
    adapter.check_authority.assert_not_called()
    adapter.generate_authority_url.reset_mock()
    checked = routing.users.check_and_get_auth(UID, "taobao", force=True, check_only=True)
    assert checked["is_authorized"] is False and "auth_url" not in checked
    assert "完成渠道授权备案" in checked["msg"]
    adapter.check_authority.assert_called_once_with(uid=UID)
    adapter.generate_authority_url.assert_not_called()
    routing.profiles[UID]["auth_status"]["taobao"]["updated_at"] = time.time() - AUTH_NEGATIVE_STATUS_TTL - 1
    routing.users.get_promotion_context(UID, "taobao")
    assert adapter.check_authority.call_count == 2
    adapter.check_authority.return_value = {"status": "success", "is_authorized": True, "bind": 1,
                                           "identity": {"external_id": UID, "relation_id": "new"}}
    assert routing.users.check_and_get_auth(UID, "taobao", force=True, check_only=True)["is_authorized"]
    routing.service.convert_link("taobao", "123", uid=UID)
    assert adapter.convert_promotion_url.call_args.kwargs["identity"]["relation_id"] == "new"


@pytest.mark.parametrize("response", [{"status": "error", "error_code": "SESSION_EXPIRED"},
                                     {"status": "success", "is_authorized": "false"}])
def test_failed_explicit_check_never_overwrites_authorization(routing, response):
    saved = authorized()
    routing.profiles[UID]["auth_status"]["taobao"] = saved
    routing.adapters["taobao"].check_authority.return_value = response
    result = routing.users.check_and_get_auth(UID, "taobao", force=True, check_only=True)
    assert result["status"] == "error"
    routing.user_repo.update_auth_status.assert_not_called()
    assert routing.profiles[UID]["auth_status"]["taobao"] == saved


@pytest.mark.parametrize("code,need_auth", [("AUTH_REQUIRED", True), ("PERSONAL_PROMOTION_UNAVAILABLE", False)])
def test_runtime_personal_failure_reuses_public_db_and_only_explicit_denial_revokes(routing, code, need_auth):
    routing.profiles[UID]["auth_status"]["pdd"] = authorized()
    routing.saved["pdd"] = public_link(_expires_at=time.time() + 300)
    adapter = routing.adapters["pdd"]
    adapter.convert_promotion_url.return_value = {"status": "error", "error_code": code, "need_auth": need_auth}
    adapter.convert_promotion_url.side_effect = None
    result = routing.service.convert_link("pdd", "123", uid=UID)
    assert result["status"] == "success" and result["final_link"] == "https://example.com/public"
    assert result["attribution_status"] == "anonymous" and result["is_fallback"]
    assert bool(result.get("need_auth")) is need_auth
    adapter.convert_promotion_url.assert_called_once()
    # 策略 B: 转链失败不抹除数据库授权信息，防止网络抖动误伤推手
    routing.user_repo.update_auth_status.assert_not_called()
    assert routing.profiles[UID]["auth_status"]["pdd"]["is_authorized"] is True
    if need_auth:
        # 下次转链依然乐观重试渠道生链
        routing.service.convert_link("pdd", "123", uid=UID)
        assert adapter.convert_promotion_url.call_count == 2


def test_identity_mismatch_failure_does_not_fall_back(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized()
    adapter = routing.adapters["taobao"]
    adapter.convert_promotion_url.side_effect = None
    adapter.convert_promotion_url.return_value = {"status": "error", "error_code": "IDENTITY_MISMATCH", "need_auth": True}
    assert routing.service.convert_link("taobao", "123", uid=UID)["error_code"] == "IDENTITY_MISMATCH"
    adapter.convert_promotion_url.assert_called_once()
    routing.products.get_promotion_link_cache.assert_not_called()
    routing.user_repo.update_auth_status.assert_not_called()


@pytest.mark.parametrize("extra", [{"cacheable": False}, {"is_fallback": True}])
def test_uncacheable_generic_data_stays_uncacheable_after_adding_auth_prompt(routing, extra):
    adapter = routing.adapters["taobao"]
    adapter.convert_promotion_url.side_effect = lambda **kwargs: public_link(**extra)
    routing.service.convert_link("taobao", "123", uid=UID)
    routing.service.convert_link("taobao", "123", uid=OTHER_UID)
    assert adapter.convert_promotion_url.call_count == 2
    routing.products.save_promotion_link_cache.assert_not_called()


def test_personal_data_in_public_cache_is_rejected(routing):
    routing.saved["taobao"] = public_link(attribution_status="verified", relation_id="private", _expires_at=time.time() + 300)
    result = routing.service.convert_link("taobao", "123", uid=UID)
    assert result["final_link"] == "https://example.com/public" and "relation_id" not in result
    routing.adapters["taobao"].convert_promotion_url.assert_called_once()
    PROMOTION_CACHE.clear()
    routing.saved.clear()
    routing.adapters["taobao"].convert_promotion_url.side_effect = lambda **kwargs: public_link(relation_id="private")
    assert routing.service.convert_link("taobao", "456", uid=UID)["error_code"] == "invalid_public_promotion"


def test_authorization_endpoint_passes_explicit_check_flags_and_keeps_uid_guard(monkeypatch):
    from api import user
    from main import app
    check = Mock(return_value={"status": "success", "is_authorized": False})
    monkeypatch.setattr(user, "user_service", SimpleNamespace(check_and_get_auth=check))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_access_token(UID)}"}
    assert client.get("/api/user/auth?platform=taobao&force=true&check_only=true", headers=headers).status_code == 200
    check.assert_called_once_with(uid=UID, platform="taobao", force=True, check_only=True)
    assert client.get(f"/api/user/auth?uid={OTHER_UID}&force=true&check_only=true", headers=headers).status_code == 403
    assert client.get("/api/user/auth?force=true&check_only=true").status_code == 401
    check.assert_called_once()


def test_zero_channel_is_not_a_valid_local_personal_identity(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized(relation="0")
    result = routing.service.convert_link("taobao", "123", uid=UID)
    assert result["need_auth"] is True and result["authorization_status"] == "unknown"
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["uid"] is None
    routing.adapters["taobao"].check_authority.assert_not_called()


def test_explicit_authority_check_can_repair_a_corrupt_saved_identity(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized(OTHER_UID)
    routing.adapters["taobao"].check_authority.return_value = {
        "status": "success", "is_authorized": True, "bind": 1,
        "identity": {"external_id": UID, "relation_id": "repaired"},
    }
    result = routing.users.check_and_get_auth(UID, "taobao", force=True, check_only=True)
    assert result["is_authorized"] is True
    assert routing.profiles[UID]["auth_status"]["taobao"]["identity"]["external_id"] == UID
    routing.adapters["taobao"].generate_authority_url.assert_not_called()


def test_runtime_failure_always_yields_station_master_fallback_without_touching_db(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized()
    routing.saved["taobao"] = public_link(_expires_at=time.time() + 300)
    adapter = routing.adapters["taobao"]
    adapter.convert_promotion_url.side_effect = None
    adapter.convert_promotion_url.return_value = {"status": "error", "error_code": "AUTH_REQUIRED", "need_auth": True}
    result = routing.service.convert_link("taobao", "123", uid=UID)
    assert result["status"] == "success"
    assert result["is_fallback"] is True
    assert result["need_auth"] is True
    assert "渠道授权未生效或已失效" in result["auth_msg"]
    adapter.convert_promotion_url.assert_called_once()
    routing.user_repo.update_auth_status.assert_not_called()


def test_injected_taobao_client_honors_authorization_specific_timeout():
    response = SimpleNamespace(status_code=200, raise_for_status=Mock(), json=lambda: {
        "tbk_sc_publisher_info_get_response": {"data": {"total_count": 0, "inviter_list": []}}})
    http = SimpleNamespace(post=Mock(return_value=response))
    client = TaobaoClient("key", "secret", session_key="session", http_session=http)
    assert client.check_authority(UID)["is_authorized"] is False
    assert http.post.call_args.kwargs["timeout"] == (2.0, 3.0)
