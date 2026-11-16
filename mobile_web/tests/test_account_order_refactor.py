# -*- coding: utf-8 -*-
"""账户/推广/订单重构回归：所有仓储、线程与平台调用均为本地测试替身。"""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapters import PlatformUnavailableError
from adapters.base import build_command_text
from models.product import ProductRef
from repositories.order_repo import OrderRepository
from repositories.user_repo import UserRepository
from services import order_service as order_module
from services import promote_service as promote_module
from services import user_service as user_module


class Adapter:
    active = True
    platform_code = "future_shop"
    platform_name = "测试平台"

    def __init__(self):
        self.convert_promotion_url = MagicMock(return_value={"status": "success", "url": "https://example.com/promo"})
        self.check_authority = MagicMock(return_value={"status": "success", "is_authorized": True, "bind": 1})
        self.generate_authority_url = MagicMock(return_value={"status": "success", "auth_url": "https://example.com/auth"})
        self.fetch_increment_orders = MagicMock(return_value={"status": "success", "order_list": []})
        self.normalize_order = MagicMock(side_effect=lambda order: dict(order, is_refund=False, is_settled=True))

    def resolve_product_ref(self, raw="", goods_id=None, goods_sign=None):
        return ProductRef(self.platform_code, goods_id or (raw if not goods_sign else ""), goods_sign or "")

    def build_command_text(self, title, final_link, price=None, coupon=None):
        return build_command_text(title, final_link, price, coupon, self.platform_name)


@pytest.fixture(autouse=True)
def isolated_state():
    promote_module.PROMOTION_CACHE.clear()
    for state in (order_module._LAST_SYNC_TIMESTAMP, order_module._LAST_SYNC_ATTEMPT, order_module._SYNC_STATE):
        state.clear()
    yield
    promote_module.PROMOTION_CACHE.clear()
    for state in (order_module._LAST_SYNC_TIMESTAMP, order_module._LAST_SYNC_ATTEMPT, order_module._SYNC_STATE):
        state.clear()


@pytest.fixture
def promotion(monkeypatch):
    adapter = Adapter()
    repo = MagicMock()
    repo.get_product_identity.return_value = None
    repo.get_promotion_link_cache.return_value = None
    monkeypatch.setattr(promote_module, "get_adapter", lambda platform: adapter)
    return promote_module.PromoteService(repo), adapter, repo


def test_fallback_is_not_cached_and_next_request_retries(promotion):
    service, adapter, repo = promotion
    adapter.convert_promotion_url.side_effect = [
        {"status": "success", "final_link": "https://example.com/raw", "is_fallback": True},
        {"status": "success", "final_link": "https://example.com/attributed"},
    ]
    first = service.convert_link("future_shop", "sku-A")
    assert first["is_fallback"]
    assert promote_module.PROMOTION_CACHE.size() == 0
    repo.save_promotion_link_cache.assert_not_called()
    assert service.convert_link("future_shop", "sku-A")["final_link"].endswith("attributed")
    assert adapter.convert_promotion_url.call_count == 2
    repo.save_promotion_link_cache.assert_called_once()


def test_invalid_success_and_legacy_fallback_cache_are_rejected(promotion):
    service, adapter, repo = promotion
    repo.get_promotion_link_cache.return_value = {"status": "success", "final_link": "https://example.com/raw", "is_fallback": True}
    adapter.convert_promotion_url.return_value = {"status": "success", "command_text": "not a link"}
    assert service.convert_link("future_shop", "sku-A")["status"] == "error"
    assert promote_module.PROMOTION_CACHE.size() == 0
    repo.save_promotion_link_cache.assert_not_called()


def test_sign_resolution_is_platform_scoped_and_accepts_nonnumeric_real_ids(promotion):
    service, adapter, repo = promotion
    repo.get_product_identity.return_value = {"platform": "future_shop", "product_id": "sku-A"}
    service.convert_link("future_shop", goods_id="wrong-id", goods_sign="opaque-sign")
    repo.get_product_identity.assert_called_once_with("opaque-sign", platform="future_shop")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "sku-A"
    assert adapter.convert_promotion_url.call_args.kwargs["goods_sign"] == "opaque-sign"
    assert repo.save_promotion_link_cache.call_args.kwargs["goods_id"] == "sku-A"


def test_identity_only_record_supplies_verified_id_without_becoming_detail(promotion):
    from repositories.product_repo import ProductRepository
    from tests.test_product_repository import MemoryCollection

    service, adapter, _ = promotion
    collection = MemoryCollection()
    service.repo = ProductRepository(SimpleNamespace(get_collection=lambda name: collection))
    collection.docs.append({"platform": "future_shop", "product_id": "trusted-id", "goods_sign": "SIGN_A"})
    service.convert_link("future_shop", goods_sign="SIGN_A", goods_id="wrong-id", uid="u_1234abcd")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "trusted-id"
    assert service.repo.get_product_detail("SIGN_A", platform="future_shop", key_type="sign") is None


def test_unverified_client_id_is_not_used_as_sign_alias(promotion):
    service, adapter, repo = promotion
    service.convert_link("future_shop", goods_id="wrong-id", goods_sign="opaque-sign")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] is None
    assert repo.get_promotion_link_cache.call_args.kwargs["goods_id"] is None
    assert repo.save_promotion_link_cache.call_args.kwargs["goods_id"] is None


def test_url_only_promotion_does_not_create_a_fake_product_identity(promotion):
    service, adapter, repo = promotion
    service.convert_link("future_shop", product_url="https://example.com/item")
    assert adapter.convert_promotion_url.call_args.kwargs["product_url"] == "https://example.com/item"
    repo.get_promotion_link_cache.assert_not_called()
    repo.save_promotion_link_cache.assert_not_called()


def test_sign_only_never_promotes_storage_placeholder_to_real_id(promotion):
    service, adapter, repo = promotion
    repo.get_product_identity.return_value = {"product_id": "opaque-sign", "goods_sign": "opaque-sign"}
    service.convert_link("future_shop", goods_sign="opaque-sign")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] is None


def test_promotion_cache_is_uid_scoped_and_defensively_copied(promotion):
    service, adapter, repo = promotion
    result = service.convert_link("future_shop", "sku-A", uid="u_1234abcd")
    result["final_link"] = "changed"
    assert service.convert_link("future_shop", "sku-A", uid="u_1234abcd")["final_link"] != "changed"
    service.convert_link("future_shop", "sku-A", uid="u_abcd1234")
    assert adapter.convert_promotion_url.call_count == 2
    repo.get_promotion_link_cache.assert_not_called()
    repo.save_promotion_link_cache.assert_not_called()


def test_command_helper_coupon_adaptive_and_long_link_suppression(promotion):
    from adapters.pdd_adapter import PddAdapter

    text = promote_module.PromoteService.build_command_text("标题", "https://example.com", 10, 2)
    assert "标题" in text and "10" in text and "https://example.com" in text
    assert "拼多多" not in text
    service, _, _ = promotion
    assert "测试平台" in service.convert_link("future_shop", "sku-A")["command_text"]

    pdd_adapter = PddAdapter()
    text_no_coupon = pdd_adapter.build_command_text("无券好物", "https://p.pinduoduo.com/short1", 19.9, 0.0)
    assert "【特惠参考价】¥19.9" in text_no_coupon and "已立减" not in text_no_coupon
    text_with_coupon = pdd_adapter.build_command_text("有券好物", "https://p.pinduoduo.com/short2", 19.9, 5.0)
    assert "【券后特惠】¥19.9（已立减 ¥5.0）" in text_with_coupon

    long_uland = "https://uland.taobao.com/coupon/edetail?e=" + ("x" * 180)
    assert promote_module.PromoteService._final_link({
        "status": "success",
        "final_link": long_uland,
        "short_url": "https://s.click.taobao.com/short_abc",
        "promotion_token": "￥ABCD1234￥",
    }) == "https://s.click.taobao.com/short_abc"

    decorated = service._with_command(
        {"status": "success", "final_link": long_uland, "promotion_token": "￥ABCD1234￥"},
        pdd_adapter,
        {"title": "超长链接测试商品", "price": 29.9, "coupon_amount": 0.0},
    )
    assert "￥ABCD1234￥" in decorated["command_text"] and long_uland not in decorated["command_text"]


def test_promotion_platform_and_id_sign_cache_types_are_distinct(promotion):
    service, adapter, _ = promotion
    service.convert_link("future_shop", goods_id="same")
    service.convert_link("future_shop", goods_sign="same")
    service.convert_link("another_shop", goods_id="same")
    assert adapter.convert_promotion_url.call_count == 3


@pytest.fixture
def user(monkeypatch):
    adapter = Adapter()
    repo = MagicMock()
    repo.get_user_by_uid.return_value = {"uid": "u_1234abcd", "auth_status": {}}
    repo.update_auth_status.return_value = True
    monkeypatch.setattr(user_module, "get_adapter", lambda platform: adapter)
    return user_module.UserService(repo), adapter, repo


@pytest.mark.parametrize("updated_at", [
    datetime.now(timezone.utc),
    datetime.now(timezone.utc).replace(tzinfo=None),
    1700000000,  # 正授权长期有效 (ttl=None)，兼容历史秒级时间戳
])
def test_saved_positive_authorization_reuses_confirmation_across_timestamp_formats(user, updated_at):
    service, adapter, repo = user
    repo.get_user_by_uid.return_value["auth_status"] = {"future_shop": {"is_authorized": True, "updated_at": updated_at}}
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["is_authorized"]
    adapter.check_authority.assert_not_called()
    repo.update_auth_status.assert_not_called()


@pytest.mark.parametrize("status", [
    {"is_authorized": True, "authorized_at": 1700000000},
    {"is_authorized": True, "updated_at": None},
    {"is_authorized": True, "updated_at": "invalid"},
])
def test_legacy_or_unparseable_updated_at_authorization_is_rechecked(user, status):
    service, adapter, repo = user
    repo.get_user_by_uid.return_value["auth_status"] = {"future_shop": status}
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["status"] == "success"
    adapter.check_authority.assert_called_once()
    repo.update_auth_status.assert_called_once()


@pytest.mark.parametrize("response", [{"status": "error", "error_msg": "quota"}, {"status": "success"}])
def test_authorization_link_generation_failure_remains_error(user, response):
    service, adapter, repo = user
    adapter.check_authority.return_value = {"status": "success", "is_authorized": False, "bind": 0}
    adapter.generate_authority_url.return_value = response
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["status"] == "error"
    repo.update_auth_status.assert_called_once_with(
        uid="u_1234abcd", platform="future_shop", is_authorized=False, bind=0, extra={"identity": {}},
    )


def test_auth_repository_canonicalizes_alias_and_stamps_utc():
    db = MagicMock()
    db.get_collection.return_value.update_one.return_value = SimpleNamespace(modified_count=1, matched_count=1)
    repo = UserRepository(db_instance=db)
    repo.update_auth_status("u_1234abcd", "tb", True)
    fields = db.get_collection.return_value.update_one.call_args.args[1]["$set"]
    assert fields["auth_status.taobao.is_authorized"] is True
    assert fields["auth_status.taobao.updated_at"].tzinfo == timezone.utc


def test_user_repo_login_errors_and_concurrent_duplicate_key_recovery():
    from pymongo.errors import DuplicateKeyError
    from auth import hash_password

    pwd_hash = hash_password("secret123")
    existing_doc = {
        "uid": "u_existing",
        "phone": "13900001111",
        "password_hash": pwd_hash,
        "nickname": "老用户",
        "invite_code": "INV_888888",
    }

    col = MagicMock()
    db = MagicMock()
    db.get_collection.return_value = col
    db.__getitem__.return_value = col
    repo = UserRepository(db_instance=db)

    col.find_one.return_value = None
    with pytest.raises(ValueError, match="该手机号尚未注册"):
        repo.register_or_login(phone="13900001111", password="password123", mode="login")

    col.find_one.return_value = dict(existing_doc)
    with pytest.raises(ValueError, match="输入的密码不正确"):
        repo.register_or_login(phone="13900001111", password="wrong_password")

    col.find_one.side_effect = [None, None, dict(existing_doc)]
    col.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error phone_1")
    user_ok = repo.register_or_login("13900001111", "secret123")
    assert user_ok["uid"] == "u_existing" and "password_hash" not in user_ok

    col.find_one.side_effect = [None, None, dict(existing_doc)]
    with pytest.raises(ValueError, match="密码"):
        repo.register_or_login("13900001111", "wrong_pass")


@pytest.fixture
def orders(monkeypatch):
    adapter = Adapter()
    repo = MagicMock()
    repo.query_user_orders.return_value = {"total_count": 0, "order_list": []}
    repo.upsert_orders.side_effect = lambda orders, platform: len(orders)
    monkeypatch.setattr(order_module, "require_adapter", lambda platform: adapter)
    monkeypatch.setattr(order_module, "get_adapter", MagicMock(return_value=adapter))
    sync_repo = MagicMock()
    sync_repo.get_state.return_value = None
    user_repo = MagicMock()
    user_repo.get_uid_by_platform_identity.return_value = None
    return order_module.OrderService(repo, sync_repo=sync_repo, user_repo=user_repo), adapter, repo


def test_busy_lock_returns_local_data_without_start_or_sdk(orders):
    service, adapter, repo = orders
    assert order_module._platform_lock("future_shop").acquire(blocking=False)
    try:
        with patch.object(order_module.threading, "Thread") as thread:
            result = service.sync_and_query_orders("future_shop", "u_a", days=0, force_sync=True)
        assert result["status"] == "success"
        thread.assert_not_called()
        order_module.get_adapter.assert_not_called()
        adapter.fetch_increment_orders.assert_not_called()
        assert repo.query_user_orders.call_args.kwargs["start_timestamp"] is None
    finally:
        order_module._platform_lock("future_shop").release()


def test_thread_start_failure_releases_lock_and_preserves_success_watermark(orders):
    service, _, _ = orders
    order_module._LAST_SYNC_TIMESTAMP["future_shop"] = 123
    with patch.object(order_module.threading, "Thread") as thread:
        thread.return_value.start.side_effect = RuntimeError("thread unavailable")
        result = service.sync_and_query_orders("future_shop", "u_a")
    assert not order_module._platform_lock("future_shop").locked()
    assert result["data"]["last_sync_time"] == 123
    assert result["data"]["sync_status"] == "error"
    assert result["data"]["last_sync_attempt_time"] > 123


def test_query_database_failure_returns_error_envelope(orders):
    service, _, repo = orders
    repo.query_user_orders.side_effect = RuntimeError("offline")
    assert order_module._platform_lock("future_shop").acquire(blocking=False)
    try:
        result = service.sync_and_query_orders("future_shop", "u_a")
    finally:
        order_module._platform_lock("future_shop").release()
    assert result["status"] == "error" and "data" not in result


def test_disabled_platforms_are_rejected_before_cached_results(promotion, user, orders, monkeypatch):
    import adapters

    promotion_service, promotion_adapter, _ = promotion
    user_service, user_adapter, _ = user
    order_service, order_adapter, _ = orders
    promotion_adapter.active = user_adapter.active = order_adapter.active = False

    monkeypatch.setattr(order_module, "require_adapter", adapters.require_adapter)
    monkeypatch.setitem(adapters._ADAPTER_REGISTRY, "future_shop", order_adapter)

    for service_call in (
        lambda: promotion_service.convert_link("future_shop", "sku-A"),
        lambda: user_service.check_and_get_auth("u_a", "future_shop"),
        lambda: order_service.sync_and_query_orders("future_shop", "u_a"),
    ):
        with pytest.raises(PlatformUnavailableError):
            service_call()


def test_api_days_zero_future_platform_syntax_and_force_protection(monkeypatch):
    # 包括路由首次导入的仓储构造也注入 mock，避免测试访问真实 Mongo。
    with patch("repositories.base_repo.gen_db_object", return_value=MagicMock()):
        from api import order, user, promote
    from auth import get_current_uid
    app = FastAPI()
    for router in (order.router, user.router, promote.router):
        app.include_router(router)
    app.dependency_overrides[get_current_uid] = lambda: "u_1234abcd"
    order_mock = MagicMock(return_value={"status": "success", "data": {}})
    auth_mock = MagicMock(return_value={"status": "success"})
    promote_mock = MagicMock(return_value={"status": "success"})
    monkeypatch.setattr(order.order_service, "sync_and_query_orders", order_mock)
    monkeypatch.setattr(user.user_service, "check_and_get_auth", auth_mock)
    monkeypatch.setattr(promote.promote_service, "convert_link", promote_mock)
    with TestClient(app) as client:
        assert client.post("/api/order/sync_and_query", params={"days": 0, "platform": "future_shop", "force": True}).status_code == 200
        assert order_mock.call_args.kwargs["days"] == 0
        assert order_mock.call_args.kwargs["force_sync"] is False
        assert client.get("/api/user/auth", params={"platform": "future_shop"}).status_code == 200
        assert client.post("/api/promote/convert", json={"platform": "future_shop", "goods_id": "sku-A"}).status_code == 200
        for bad in ("pdd.dot", "$bad", "Upper", "a" * 33):
            assert client.post("/api/order/sync_and_query", params={"platform": bad}).status_code == 422
            assert client.get("/api/user/auth", params={"platform": bad}).status_code == 422
            assert client.post("/api/promote/convert", json={"platform": bad}).status_code == 422
        assert client.post("/api/order/sync_and_query", params={"days": -1}).status_code == 422
