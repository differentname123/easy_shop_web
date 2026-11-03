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
    legacy_order_statuses = {"refund": ["returned"], "settled": ["paid_out"]}

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
    service.convert_link("future_shop", goods_sign="opaque-sign")
    repo.get_product_identity.assert_called_once_with("opaque-sign", platform="future_shop")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "sku-A"
    assert adapter.convert_promotion_url.call_args.kwargs["goods_sign"] == "opaque-sign"


def test_unverified_client_id_is_not_used_as_sign_alias(promotion):
    service, adapter, repo = promotion
    service.convert_link("future_shop", goods_id="wrong-id", goods_sign="opaque-sign")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] is None
    assert repo.get_promotion_link_cache.call_args.kwargs["goods_id"] is None
    assert repo.save_promotion_link_cache.call_args.kwargs["goods_id"] is None


def test_sign_pair_uses_only_platform_verified_id(promotion):
    service, adapter, repo = promotion
    repo.get_product_identity.return_value = {"platform": "future_shop", "product_id": "verified-id"}
    service.convert_link("future_shop", goods_id="wrong-id", goods_sign="opaque-sign")
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "verified-id"
    assert repo.save_promotion_link_cache.call_args.kwargs["goods_id"] == "verified-id"


def test_url_only_promotion_does_not_create_a_fake_product_identity(promotion):
    service, adapter, repo = promotion
    service.convert_link("future_shop", product_url="https://example.com/item")
    assert adapter.convert_promotion_url.call_args.kwargs["product_url"] == "https://example.com/item"
    repo.get_promotion_link_cache.assert_not_called()
    repo.save_promotion_link_cache.assert_not_called()


def test_sign_only_never_promotes_storage_placeholder_to_real_id(promotion):
    service, adapter, repo = promotion
    repo.get_product_identity.return_value = {"product_id": "sign:opaque-sign"}
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


def test_command_helper_is_neutral_but_platform_adapter_controls_copy(promotion):
    text = promote_module.PromoteService.build_command_text("标题", "https://example.com", 10, 2)
    assert "标题" in text and "10" in text and "https://example.com" in text
    assert "拼多多" not in text
    service, _, _ = promotion
    assert "测试平台" in service.convert_link("future_shop", "sku-A")["command_text"]


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


@pytest.mark.parametrize("updated_at", [datetime.now(timezone.utc), datetime.now(timezone.utc).replace(tzinfo=None)])
def test_fresh_authorization_reuses_confirmation(user, updated_at):
    service, adapter, repo = user
    repo.get_user_by_uid.return_value["auth_status"] = {"future_shop": {"is_authorized": True, "updated_at": updated_at}}
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["is_authorized"]
    adapter.check_authority.assert_not_called()
    repo.update_auth_status.assert_not_called()


@pytest.mark.parametrize("status", [
    {"is_authorized": True, "authorized_at": 1700000000},
    {"is_authorized": True, "updated_at": 1700000000},
    {"is_authorized": True, "updated_at": "invalid"},
])
def test_expired_or_legacy_authorization_is_rechecked(user, status):
    service, adapter, repo = user
    repo.get_user_by_uid.return_value["auth_status"] = {"future_shop": status}
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["status"] == "success"
    adapter.check_authority.assert_called_once()
    repo.update_auth_status.assert_called_once()


@pytest.mark.parametrize("response", [
    {"status": "error", "is_authorized": False, "error_msg": "upstream unavailable"},
    {"status": "success"},
    {"status": "success", "is_authorized": "false"},
])
def test_authorization_errors_do_not_overwrite_state(user, response):
    service, adapter, repo = user
    repo.get_user_by_uid.return_value["auth_status"] = {"future_shop": {"is_authorized": True}}
    adapter.check_authority.return_value = response
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["status"] == "error"
    repo.update_auth_status.assert_not_called()
    adapter.generate_authority_url.assert_not_called()


@pytest.mark.parametrize("response", [{"status": "error", "error_msg": "quota"}, {"status": "success"}])
def test_authorization_link_generation_failure_remains_error(user, response):
    service, adapter, repo = user
    adapter.check_authority.return_value = {"status": "success", "is_authorized": False, "bind": 0}
    adapter.generate_authority_url.return_value = response
    assert service.check_and_get_auth("u_1234abcd", "future_shop")["status"] == "error"
    repo.update_auth_status.assert_called_once_with(uid="u_1234abcd", platform="future_shop", is_authorized=False, bind=0)


def test_confirmed_revocation_generates_real_authorization_url(user):
    service, adapter, repo = user
    adapter.check_authority.return_value = {"status": "success", "is_authorized": False, "bind": 0}
    result = service.check_and_get_auth("u_1234abcd", "future_shop")
    assert result["status"] == "success" and result["is_authorized"] is False
    assert result["auth_url"] == "https://example.com/auth"


def test_auth_repository_canonicalizes_alias_and_stamps_utc():
    db = MagicMock()
    db.get_collection.return_value.update_one.return_value = SimpleNamespace(modified_count=1, matched_count=1)
    repo = UserRepository(db_instance=db)
    repo.update_auth_status("u_1234abcd", "tb", True)
    fields = db.get_collection.return_value.update_one.call_args.args[1]["$set"]
    assert fields["auth_status.taobao.is_authorized"] is True
    assert fields["auth_status.taobao.updated_at"].tzinfo == timezone.utc


# 小型 Mongo 表达式解释器，仅用于检验本仓储发出的聚合管线，绝不连接数据库。
_MISSING = object()


def evaluate(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        return doc.get(expr[1:])
    if isinstance(expr, list):
        return [evaluate(value, doc) for value in expr]
    if not isinstance(expr, dict):
        return expr
    operator, value = next(iter(expr.items()))
    if operator == "$type":
        item = doc.get(value[1:], _MISSING)
        return "missing" if item is _MISSING else "bool" if isinstance(item, bool) else "other"
    if operator == "$convert":
        item = evaluate(value["input"], doc)
        return float(item) if item is not None else value["onNull"]
    if operator == "$cond":
        test, yes, no = value
        return evaluate(yes if evaluate(test, doc) else no, doc)
    args = evaluate(value, doc)
    if operator == "$eq":
        return args[0] == args[1]
    if operator == "$in":
        return args[0] in args[1]
    if operator == "$and":
        return all(args)
    if operator == "$not":
        return not args[0]
    raise AssertionError(f"unsupported expression {expr}")


class MemoryOrderCollection:
    def __init__(self, orders):
        self.orders = orders
        self.pipeline = None

    def create_index(self, *args, **kwargs):
        return "test_index"

    def aggregate(self, pipeline):
        self.pipeline = deepcopy(pipeline)
        match = pipeline[0]["$match"]
        docs = [doc for doc in self.orders if all(
            (doc.get(key, 0) >= value["$gte"]) if isinstance(value, dict) else doc.get(key) == value
            for key, value in match.items()
        )]
        output = {}
        for name, stages in pipeline[1]["$facet"].items():
            rows = deepcopy(docs)
            for stage in stages:
                operator, value = next(iter(stage.items()))
                if operator == "$project":
                    if value == {"_id": 0}:
                        rows = [{key: item for key, item in row.items() if key != "_id"} for row in rows]
                    else:
                        rows = [{key: evaluate(expr, row) for key, expr in value.items()} for row in rows]
                elif operator == "$group":
                    rows = [{key: sum(evaluate(expr["$sum"], row) for row in rows)
                             for key, expr in value.items() if key != "_id"}] if rows else []
                elif operator == "$sort":
                    for key, direction in reversed(list(value.items())):
                        rows.sort(key=lambda row: row.get(key, 0), reverse=direction < 0)
                elif operator == "$limit":
                    rows = rows[:value]
                else:
                    raise AssertionError(f"unsupported stage {stage}")
            output[name] = rows
        return [output]


def order_repo_for(collection):
    db = MagicMock()
    db.get_collection.return_value = collection
    return OrderRepository(db_instance=db)


def test_aggregation_covers_all_matches_not_only_100_display_rows():
    docs = [{"platform": "future_shop", "promoter_uid": "u_a", "order_sn": f"o{i:03}",
             "promotion_amount_yuan": "1.25", "order_pay_time": 1000 + i,
             "is_refund": False, "is_settled": True} for i in range(135)]
    docs.extend([
        dict(docs[0], platform="other", promotion_amount_yuan=999),
        dict(docs[0], promoter_uid="other", promotion_amount_yuan=999),
        dict(docs[0], order_pay_time=1, promotion_amount_yuan=999),
    ])
    col = MemoryOrderCollection(docs)
    result = order_repo_for(col).query_user_orders("future_shop", "u_a", start_timestamp=1000)
    assert result["total_count"] == 135
    assert result["total_commission"] == result["settled_commission"] == 168.75
    assert len(result["order_list"]) == 100
    assert result["order_list"][0]["order_sn"] == "o134"
    assert not any("$limit" in stage for stage in col.pipeline[1]["$facet"]["stats"])


def test_canonical_false_wins_and_legacy_rules_are_platform_supplied():
    docs = [
        {"order_sn": "false", "order_status": "returned", "is_refund": False, "is_settled": True},
        {"order_sn": "legacy-refund", "order_status": "returned"},
        {"order_sn": "legacy-settled", "order_status": "paid_out"},
        {"order_sn": "not-pdd", "order_status": 4},
        {"order_sn": "settled-false", "order_status": "paid_out", "is_settled": False},
    ]
    for doc in docs:
        doc.update(platform="future_shop", promoter_uid="u_a", promotion_amount_yuan=2)
    repo = order_repo_for(MemoryOrderCollection(docs))
    result = repo.query_user_orders("future_shop", "u_a", legacy_order_statuses=Adapter.legacy_order_statuses)
    assert result["total_count"] == 5 and result["total_commission"] == 8
    assert result["refund_count"] == 1 and result["refund_commission"] == 2
    assert result["settled_commission"] == 4
    by_id = {order["order_sn"]: order for order in result["order_list"]}
    assert not by_id["false"]["is_refund"] and not by_id["not-pdd"]["is_refund"]
    assert not by_id["settled-false"]["is_settled"]


def test_repository_errors_are_not_legal_empty_stats():
    col = MagicMock()
    col.aggregate.side_effect = RuntimeError("offline")
    col.bulk_write.side_effect = RuntimeError("offline")
    repo = order_repo_for(col)
    with pytest.raises(RuntimeError):
        repo.query_user_orders("future_shop", "u_a")
    with pytest.raises(RuntimeError):
        repo.upsert_orders([{"order_sn": "a"}], "future_shop")
    with pytest.raises(ValueError):
        repo.upsert_orders([{}], "future_shop")


@pytest.fixture
def orders(monkeypatch):
    adapter = Adapter()
    repo = MagicMock()
    repo.query_user_orders.return_value = {"total_count": 0, "order_list": []}
    repo.upsert_orders.side_effect = lambda orders, platform: len(orders)
    monkeypatch.setattr(order_module, "require_adapter", lambda platform: adapter)
    monkeypatch.setattr(order_module, "get_adapter", MagicMock(return_value=adapter))
    return order_module.OrderService(repo), adapter, repo


def run_sync(service, end_time=200000):
    assert order_module._SYNC_LOCK.acquire(blocking=False)
    service._sync_platform_orders_bg("future_shop", end_time)
    assert not order_module._SYNC_LOCK.locked()


def test_busy_lock_returns_local_data_without_start_or_sdk(orders):
    service, adapter, repo = orders
    assert order_module._SYNC_LOCK.acquire(blocking=False)
    try:
        with patch.object(order_module.threading, "Thread") as thread:
            result = service.sync_and_query_orders("future_shop", "u_a", days=0, force_sync=True)
        assert result["status"] == "success"
        thread.assert_not_called()
        order_module.get_adapter.assert_not_called()
        adapter.fetch_increment_orders.assert_not_called()
        assert repo.query_user_orders.call_args.kwargs["start_timestamp"] is None
        assert repo.query_user_orders.call_args.kwargs["legacy_order_statuses"] == adapter.legacy_order_statuses
    finally:
        order_module._SYNC_LOCK.release()


def test_thread_start_failure_releases_lock_and_preserves_success_watermark(orders):
    service, _, _ = orders
    order_module._LAST_SYNC_TIMESTAMP["future_shop"] = 123
    with patch.object(order_module.threading, "Thread") as thread:
        thread.return_value.start.side_effect = RuntimeError("thread unavailable")
        result = service.sync_and_query_orders("future_shop", "u_a")
    assert not order_module._SYNC_LOCK.locked()
    assert result["data"]["last_sync_time"] == 123
    assert result["data"]["sync_status"] == "error"
    assert result["data"]["last_sync_attempt_time"] > 123


def test_force_cannot_bypass_attempt_cooldown(orders):
    service, _, _ = orders
    order_module._LAST_SYNC_ATTEMPT["future_shop"] = 1000
    with patch.object(order_module.time, "time", return_value=1001), patch.object(order_module.threading, "Thread") as thread:
        assert service.sync_and_query_orders("future_shop", "u_a", force_sync=True)["status"] == "success"
    thread.assert_not_called()


@pytest.mark.parametrize("kind", ["upstream", "database", "short_write"])
def test_failed_sync_does_not_advance_watermark(orders, kind):
    service, adapter, repo = orders
    order_module._LAST_SYNC_TIMESTAMP["future_shop"] = 123
    if kind == "upstream":
        adapter.fetch_increment_orders.return_value = {"status": "error", "error_msg": "quota"}
    else:
        adapter.fetch_increment_orders.return_value = {"status": "success", "order_list": [{"order_sn": "a"}]}
        repo.upsert_orders.side_effect = RuntimeError("offline") if kind == "database" else None
        repo.upsert_orders.return_value = 0
    run_sync(service)
    assert order_module._SYNC_STATE["future_shop"]["status"] == "error"
    assert order_module._LAST_SYNC_TIMESTAMP["future_shop"] == 123


def test_page_budget_exhaustion_is_partial_not_success(orders, monkeypatch):
    service, adapter, repo = orders
    monkeypatch.setattr(order_module, "ORDER_SYNC_MAX_PAGES", 2)
    adapter.fetch_increment_orders.return_value = {"status": "success", "order_list": [{"order_sn": str(i)} for i in range(50)], "has_more": True}
    run_sync(service)
    assert order_module._SYNC_STATE["future_shop"]["status"] == "partial"
    assert order_module._SYNC_STATE["future_shop"]["synced_count"] == 100
    assert "future_shop" not in order_module._LAST_SYNC_TIMESTAMP
    assert adapter.normalize_order.call_count == 100
    assert repo.upsert_orders.call_args.args[0][0]["is_settled"] is True


def test_failure_after_first_page_is_partial_and_keeps_previous_success(orders):
    service, adapter, _ = orders
    order_module._LAST_SYNC_TIMESTAMP["future_shop"] = 123
    adapter.fetch_increment_orders.side_effect = [
        {"status": "success", "order_list": [{"order_sn": "a"}], "has_more": True},
        {"status": "error", "error_msg": "quota"},
    ]
    run_sync(service)
    assert order_module._SYNC_STATE["future_shop"]["status"] == "partial"
    assert order_module._LAST_SYNC_TIMESTAMP["future_shop"] == 123


def test_complete_empty_window_advances_only_success_watermark(orders):
    service, _, _ = orders
    run_sync(service)
    assert order_module._LAST_SYNC_TIMESTAMP["future_shop"] == 200000
    assert order_module._SYNC_STATE["future_shop"]["status"] == "success"


def test_query_database_failure_returns_error_envelope(orders):
    service, _, repo = orders
    repo.query_user_orders.side_effect = RuntimeError("offline")
    assert order_module._SYNC_LOCK.acquire(blocking=False)
    try:
        result = service.sync_and_query_orders("future_shop", "u_a")
    finally:
        order_module._SYNC_LOCK.release()
    assert result["status"] == "error" and "data" not in result


def test_disabled_platforms_are_rejected_before_cached_results(promotion, user, orders, monkeypatch):
    promotion_service, promotion_adapter, _ = promotion
    user_service, user_adapter, _ = user
    promotion_adapter.active = user_adapter.active = False
    for service_call in (
        lambda: promotion_service.convert_link("future_shop", "sku-A"),
        lambda: user_service.check_and_get_auth("u_a", "future_shop"),
    ):
        with pytest.raises(PlatformUnavailableError):
            service_call()
    monkeypatch.setattr(order_module, "require_adapter", MagicMock(side_effect=PlatformUnavailableError("暂未接入")))
    with pytest.raises(PlatformUnavailableError):
        orders[0].sync_and_query_orders("future_shop", "u_a")


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
