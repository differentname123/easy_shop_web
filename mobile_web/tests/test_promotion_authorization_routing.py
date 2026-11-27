"""Local platform qualification, public-cache reuse and explicit authorization checks; no network."""
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from adapters.base import BasePlatformAdapter
from auth import create_access_token
from cache import PROMOTION_CACHE
from config import AUTH_NEGATIVE_STATUS_TTL
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


class RoutedAdapter(BasePlatformAdapter):
    active = True
    channels = ()
    requires_authorized_promotion = True
    required_identity_fields = ("relation_id",)

    def _unimplemented(self, *args, **kwargs):
        return {"status": "error", "error_code": "platform_unavailable"}

    get_recommend_goods = _unimplemented
    search_goods = _unimplemented
    get_goods_detail = _unimplemented
    convert_promotion_url = _unimplemented
    check_authority = _unimplemented
    generate_authority_url = _unimplemented
    fetch_increment_orders = _unimplemented

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
def test_corrupt_or_mismatched_saved_identity_falls_back_to_station_master_without_leaking_identity(routing, identity):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized() | {"identity": identity}
    res = routing.service.convert_link("taobao", "123", uid=UID)
    assert res["status"] == "success" and res["final_link"] == "https://example.com/public"
    assert res["is_fallback"] is True and res["attribution_status"] == "anonymous" and res["cacheable"] is False
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["uid"] is None
    assert routing.adapters["taobao"].convert_promotion_url.call_args.kwargs["identity"] == {}


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
def test_runtime_personal_failure_reuses_public_db_and_never_revokes_authorization(routing, code, need_auth):
    routing.profiles[UID]["auth_status"]["pdd"] = authorized()
    routing.saved["pdd"] = public_link(_expires_at=time.time() + 300)
    adapter = routing.adapters["pdd"]
    adapter.convert_promotion_url.return_value = {"status": "error", "error_code": code, "need_auth": need_auth}
    adapter.convert_promotion_url.side_effect = None
    result = routing.service.convert_link("pdd", "123", uid=UID)
    assert result["status"] == "success" and result["final_link"] == "https://example.com/public"
    assert result["attribution_status"] == "anonymous" and result["is_fallback"]
    assert bool(result.get("need_auth")) is need_auth
    if need_auth:
        assert "渠道授权未生效或已失效" in result["auth_msg"]
    adapter.convert_promotion_url.assert_called_once()
    # 策略 B: 转链失败不抹除数据库授权信息，防止网络抖动误伤推手
    routing.user_repo.update_auth_status.assert_not_called()
    assert routing.profiles[UID]["auth_status"]["pdd"]["is_authorized"] is True
    if need_auth:
        routing.service.convert_link("pdd", "123", uid=UID)
        assert adapter.convert_promotion_url.call_count == 2


def test_identity_mismatch_or_missing_user_falls_back_to_station_master_link(routing):
    routing.profiles[UID]["auth_status"]["taobao"] = authorized()
    routing.saved["taobao"] = public_link(_expires_at=time.time() + 300)
    adapter = routing.adapters["taobao"]
    adapter.convert_promotion_url.side_effect = None
    adapter.convert_promotion_url.return_value = {"status": "error", "error_code": "IDENTITY_MISMATCH", "need_auth": True}
    res = routing.service.convert_link("taobao", "123", uid=UID)
    assert res["status"] == "success" and res["final_link"] == "https://example.com/public"
    assert res["is_fallback"] is True and res["attribution_status"] == "anonymous"
    adapter.convert_promotion_url.assert_called_once()
    routing.user_repo.update_auth_status.assert_not_called()

    # 用户被删库（推手用户不存在）时同样由站长公共推广链保底，绝不丢单
    res_deleted = routing.service.convert_link("taobao", "123", uid="u_deadbeef")
    assert res_deleted["status"] == "success" and res_deleted["final_link"] == "https://example.com/public"
    assert res_deleted["is_fallback"] is True and res_deleted["attribution_status"] == "anonymous"


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


def test_universal_convert_http_route_and_keyword_or_ambiguous_interceptions(monkeypatch):
    from unittest.mock import MagicMock
    from infrastructure.http_utils import sanitize_user_error_message
    from models.product import ProductRef
    from main import app

    sanitized = sanitize_user_error_message("UPSTREAM_ERROR", "折淘客ZTK请求失败: errorCode=40009 relationId sid invalid", "淘宝")
    for leak in ("折淘客", "ZTK", "40009", "relationId"):
        assert leak not in sanitized

    client = TestClient(app)
    ambiguous = client.post("/api/promote/universal", json={"text": "农夫山泉饮用天然水550ml*24瓶整箱装", "platform": "auto"})
    assert ambiguous.status_code == 200
    assert ambiguous.json()["error_code"] == "AMBIGUOUS_PLATFORM_TEXT"

    svc = PromoteService(product_repo=MagicMock(), user_service=MagicMock())
    intercepted = svc.universal_convert(raw_text="维达抽纸整箱", platform="taobao")
    assert intercepted["status"] == "error"
    assert intercepted["error_code"] == "KEYWORD_NOT_LINK"
    assert intercepted["suggested_keyword"] == "维达抽纸整箱"
    assert intercepted["suggested_platform"] == "taobao"

    mock_adapter = MagicMock()
    mock_adapter.active = True
    mock_adapter.platform_code = "taobao"
    mock_adapter.platform_name = "淘宝"
    mock_adapter.capabilities = {"detail": True, "promotion": True}
    mock_adapter.requires_authorized_promotion = True
    mock_adapter.requires_numeric_promoter_id = False
    mock_adapter.resolve_product_ref.side_effect = lambda raw="", gid=None, gsign=None: ProductRef("taobao", gid or raw, gsign or raw)
    mock_adapter.build_command_text.return_value = "【淘宝精选】好丽友派\n￥JBnB61RqEqX￥ https://s.click.taobao.com/abc"
    mock_adapter.resolve_and_convert_universal.return_value = {
        "status": "success",
        "url": "https://uland.taobao.com/coupon/edetail?e=xxx",
        "final_link": "https://s.click.taobao.com/abc",
        "short_url": "https://s.click.taobao.com/abc",
        "promotion_token": "￥JBnB61RqEqX￥",
        "token_type": "tpwd",
        "attribution_status": "anonymous",
        "need_auth": False,
        "is_fallback": False,
        "goods_id": "TB_ENC_ID_999",
        "goods_sign": "TB_ENC_ID_999",
        "title": "好丽友巧克力派",
        "price": 8.5,
        "coupon_amount": 3.0,
        "product": {
            "platform": "taobao", "platform_name": "淘宝",
            "goods_id": "TB_ENC_ID_999", "goods_sign": "TB_ENC_ID_999",
            "name": "好丽友巧克力派", "image_url": "https://img.alicdn.com/1.jpg",
            "price": 8.5, "original_price": 11.5, "coupon_amount": 3.0, "has_coupon": True,
        },
    }
    monkeypatch.setattr("services.promote_service.get_adapter", lambda _p: mock_adapter)
    monkeypatch.setattr("services.product_service.get_adapter", lambda _p: mock_adapter)
    resp = client.post(
        "/api/promote/universal",
        json={"text": "67￥JBnB61RqEqX￥ https://m.tb.cn/h.xxx 好丽友巧克力派", "platform": "auto"},
    )
    from public_boundary import encode_item_code
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success" and body["platform"] == "taobao"
    assert body["promotion_token"] == "￥JBnB61RqEqX￥"
    assert body["product"]["item_code"] == encode_item_code("taobao", "TB_ENC_ID_999")
    assert "goods_id" not in body["product"] and "goods_sign" not in body["product"]
    assert "goods_id" not in body and "goods_sign" not in body

    # 验证错误清洗单一真源对 UPSTREAM_ERROR / RATE_LIMIT / 数字网关码的收口（detail 与 convert 出口）
    from unittest.mock import MagicMock as _MM
    from services.product_service import ProductService

    msg_upstream = sanitize_user_error_message("UPSTREAM_ERROR", "京东返回业务错误 (2001703)", "京东")
    assert "2001703" not in msg_upstream and "业务错误" not in msg_upstream
    msg_rl = sanitize_user_error_message("RATE_LIMIT", "京东选品库处于短时频控冷却中，暂跳过避免空转", "京东")
    assert "空转" not in msg_rl and "冷却" not in msg_rl and "繁忙" in msg_rl
    msg_num = sanitize_user_error_message("50001", "PDD网关拒绝: [50001:400011]", "拼多多", scene="detail")
    assert "50001" not in msg_num and "网关" not in msg_num and "转链" not in msg_num
    assert sanitize_user_error_message("INVALID_TAOBAO_URL", "该链接不属于淘宝/天猫平台", "淘宝") == "该链接不属于淘宝/天猫平台"

    err_repo = _MM()
    err_repo.get_product_detail.return_value = None
    err_repo.get_promotion_link_cache.return_value = None
    err_adapter = _MM()
    err_adapter.active = True
    err_adapter.platform_code = "pdd"
    err_adapter.platform_name = "拼多多"
    err_adapter.capabilities = {"detail": True, "promotion": True}
    err_adapter.requires_authorized_promotion = False
    err_adapter.requires_numeric_promoter_id = False
    err_adapter.resolve_product_ref.return_value = ProductRef("pdd", "999", "SIGN_ERR")
    err_adapter.get_goods_detail.return_value = {"status": "error", "error_code": "50001", "error_msg": "PDD网关拒绝: [50001:400011]"}
    err_adapter.convert_promotion_url.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR", "error_msg": "京东返回业务错误 (2001703)"}

    monkeypatch.setattr("services.product_service.get_adapter", lambda _p: err_adapter)
    monkeypatch.setattr("services.promote_service.get_adapter", lambda _p: err_adapter)
    det_err = ProductService(err_repo).get_goods_detail(platform="pdd", goods_sign="SIGN_ERR")
    assert "50001" not in det_err["error_msg"] and "网关" not in det_err["error_msg"]
    conv_err = PromoteService(product_repo=err_repo, user_service=_MM()).convert_link("pdd", "SIGN_ERR")
    assert "2001703" not in conv_err["error_msg"] and "业务错误" not in conv_err["error_msg"]

