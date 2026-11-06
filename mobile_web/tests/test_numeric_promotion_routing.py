"""数字推广路由只信服务端上下文；无需真实平台或数据库。"""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from adapters.base import BasePlatformAdapter, build_command_text
from models.product import ProductRef
from services import promote_service as module
from services.user_service import UserService


class NumericAdapter:
    active = True
    platform_name = "数字商城"
    requires_numeric_promoter_id = True
    requires_material_url = True
    requires_authorized_promotion = False
    default_promoter_id = "3110104129"
    promotion_scope = "union-A:3110104129"
    capabilities = {"promotion": True}

    def __init__(self):
        self.calls = []
        self.cacheable = True
        self.failure = False
        self.wrong_identity = False

    def resolve_product_ref(self, raw="", goods_id=None, goods_sign=None):
        return ProductRef("jd", goods_id or raw)

    def build_command_text(self, title, final_link, price=None, coupon=None):
        return build_command_text(title, final_link, price, coupon, self.platform_name)

    def convert_promotion_url(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.failure:
            return {"status": "error", "error_msg": "upstream failed"}
        promoter_id = kwargs["promoter_id"]
        return {"status": "success", "short_url": "https://u.jd.com/" + promoter_id,
                "promoter_id": "999" if self.wrong_identity else promoter_id,
                "attribution_status": "verified" if kwargs.get("uid") else "anonymous",
                "promotion_token": "真实接口测试口令", "cacheable": self.cacheable}


@pytest.fixture
def routing(monkeypatch):
    module.PROMOTION_CACHE.clear()
    adapter = NumericAdapter()
    repo = MagicMock()
    repo.get_promotion_link_cache.return_value = None
    repo.get_product_detail.return_value = {
        "platform": "jd", "goods_id": "opaque-material", "product_url": "https://jingfen.jd.com/detail/verified.html"}
    users = MagicMock()
    users.get_numeric_promotion_context.side_effect = lambda uid, platform, default: {
        "status": "success", "promoter_id": {"u_1234abcd": "1234", "u_abcd1234": "5678"}[uid]}
    monkeypatch.setattr(module, "get_adapter", lambda platform: adapter)
    service = module.PromoteService(repo, users)
    yield service, adapter, repo, users
    module.PROMOTION_CACHE.clear()


def test_owner_and_two_users_use_same_conversion_without_authorization(routing):
    service, adapter, repo, users = routing
    for uid in (None, "u_1234abcd", "u_abcd1234"):
        result = service.convert_link("jd", "opaque-material", uid=uid)
        assert result["status"] == "success" and not result.get("need_auth")
        assert "真实接口测试口令" in result["command_text"]
    assert [call["promoter_id"] for call in adapter.calls] == ["3110104129", "1234", "5678"]
    assert all(call["trusted_product_url"].endswith("verified.html") for call in adapter.calls)
    users.get_promotion_context.assert_not_called()
    assert repo.save_promotion_link_cache.call_count == 1


def test_client_cannot_override_identity_or_verified_material(routing):
    service, adapter, _, _ = routing
    service.convert_link("jd", "opaque-material", uid="u_1234abcd", promoter_id="3110104129",
                         positionId="8888", identity={"promoter_id": "7777"},
                         trusted_product_url="https://item.jd.com/evil.html",
                         product_url="https://item.jd.com/unrelated.html", unionId="other")
    call = adapter.calls[-1]
    assert call["promoter_id"] == "1234"
    assert call["trusted_product_url"].endswith("verified.html")
    assert "positionId" not in call and "identity" not in call and "unionId" not in call


def test_foreign_or_mismatched_mapping_is_not_trusted(routing):
    service, adapter, repo, _ = routing
    repo.get_product_detail.return_value["platform"] = "pdd"
    service.convert_link("jd", "opaque-material")
    assert adapter.calls[-1]["trusted_product_url"] == ""
    module.PROMOTION_CACHE.clear()
    repo.get_product_detail.return_value.update(platform="jd", goods_id="other-item")
    repo.get_promotion_link_cache.return_value = None
    service.convert_link("jd", "opaque-material")
    assert adapter.calls[-1]["trusted_product_url"] == ""


def test_no_personal_failure_can_fall_back_to_default(routing):
    service, adapter, _, users = routing
    users.get_numeric_promotion_context.return_value = None
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "error", "error_msg": "db failed"}
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["status"] == "error"
    assert adapter.calls == []
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "success", "promoter_id": "1234"}
    adapter.failure = True
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["status"] == "error"
    assert len(adapter.calls) == 1 and adapter.calls[0]["uid"] == "u_1234abcd"


def test_invalid_identity_and_wrong_result_fail_closed(routing):
    service, adapter, _, users = routing
    assert service.convert_link("jd", "opaque-material", uid="bad-uid")["status"] == "error"
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "success", "promoter_id": "u_bad"}
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["status"] == "error"
    assert adapter.calls == []
    adapter.wrong_identity = True
    assert service.convert_link("jd", "opaque-material")["error_code"] == "invalid_promotion_identity"


def test_cache_changes_with_numeric_identity_and_default_configuration(routing):
    service, adapter, repo, users = routing
    service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    assert len(adapter.calls) == 1
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "success", "promoter_id": "2468"}
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["promoter_id"] == "2468"
    assert len(adapter.calls) == 2
    public = service.convert_link("jd", "opaque-material")
    stored = dict(repo.save_promotion_link_cache.call_args.kwargs["data"], _expires_at=10**10)
    # A changed deployment default must reject the old Mongo cache as well as memory cache.
    stored["_expires_at"] = __import__("time").time() + 60
    repo.get_promotion_link_cache.return_value = stored
    adapter.default_promoter_id = "3333"
    adapter.promotion_scope = "union-B:3333"
    assert service.convert_link("jd", "opaque-material")["promoter_id"] == "3333"
    assert public["promoter_id"] != "3333"


def test_unknown_lifetime_disables_all_writes(routing):
    service, adapter, repo, _ = routing
    adapter.cacheable = False
    for _ in range(2):
        assert service.convert_link("jd", "opaque-material")["status"] == "success"
    assert len(adapter.calls) == 2
    assert module.PROMOTION_CACHE.size() == 0
    repo.save_promotion_link_cache.assert_not_called()


def test_numeric_user_context_only_resolves_identity_without_order_mutations(monkeypatch):
    users, orders = MagicMock(), MagicMock()
    users.ensure_promoter_id.return_value = "1234"
    service = UserService(users, orders)
    for _ in range(2):
        result = service.get_numeric_promotion_context("u_1234abcd", "jd", "3110104129")
        assert result == {"status": "success", "uid": "u_1234abcd", "promoter_id": "1234"}
    users.ensure_promoter_id.assert_called_with("u_1234abcd", reserved_ids=("3110104129",))
    assert orders.mock_calls == []
    assert service.get_numeric_promotion_context("u_1234abcd", "jd", "3110104129")["status"] == "success"
    users.ensure_promoter_id.side_effect = RuntimeError("secret must not leak")
    failed = service.get_numeric_promotion_context("u_1234abcd", "jd", "3110104129")
    assert failed["status"] == "error" and "secret" not in str(failed)


def test_no_auth_api_path_reports_not_required_without_remote_calls(monkeypatch):
    users = MagicMock()
    adapter = NumericAdapter()
    adapter.check_authority = MagicMock(side_effect=AssertionError("no authority call"))
    adapter.generate_authority_url = MagicMock(side_effect=AssertionError("no authority URL"))
    monkeypatch.setattr("services.user_service.get_adapter", lambda platform: adapter)
    service = UserService(users)
    for check_only in (False, True):
        result = service.check_and_get_auth("u_1234abcd", "jd", check_only=check_only)
        assert result["status"] == "success"
        assert result["authorization_required"] is False and result["is_authorized"] is False
        assert "auth_url" not in result
    users.update_auth_status.assert_not_called()
