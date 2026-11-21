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


def test_personal_failure_falls_back_to_station_master_floor_unless_station_master_fails(routing):
    service, adapter, repo, users = routing
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "error", "error_msg": "推手用户不存在"}
    res_missing = service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    assert res_missing["status"] == "success"
    assert res_missing["promoter_id"] == "3110104129"
    assert res_missing["is_fallback"] is True and res_missing["attribution_status"] == "anonymous"
    assert res_missing["cacheable"] is False
    assert len(adapter.calls) == 1 and adapter.calls[0]["promoter_id"] == "3110104129" and adapter.calls[0]["uid"] is None

    # 个人推广位上游失败、但站长推广位可用时，自动以站长推广位保底（绝不白白丢单）
    module.PROMOTION_CACHE.clear()
    repo.get_promotion_link_cache.return_value = None
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "success", "promoter_id": "1234"}
    orig_convert = adapter.convert_promotion_url
    adapter.convert_promotion_url = lambda **kw: (
        {"status": "error", "error_msg": "personal position rejected"}
        if kw.get("promoter_id") == "1234"
        else orig_convert(**kw)
    )
    res_fb = service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    assert res_fb["status"] == "success" and res_fb["promoter_id"] == "3110104129"
    assert res_fb["is_fallback"] is True and res_fb["attribution_status"] == "anonymous"
    adapter.convert_promotion_url = orig_convert

    # 当上游连站长推广位也无法转链时，才真正返回错误
    module.PROMOTION_CACHE.clear()
    repo.get_promotion_link_cache.return_value = None
    adapter.failure = True
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["status"] == "error"


def test_invalid_personal_identity_falls_back_while_broken_default_or_wrong_result_fails_closed(routing):
    service, adapter, _, users = routing
    bad_uid_res = service.convert_link("jd", "opaque-material", uid="bad-uid")
    assert bad_uid_res["status"] == "success" and bad_uid_res["promoter_id"] == "3110104129"
    assert bad_uid_res["is_fallback"] is True and bad_uid_res["attribution_status"] == "anonymous"

    module.PROMOTION_CACHE.clear()
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "success", "promoter_id": "u_bad"}
    bad_pid_res = service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    assert bad_pid_res["status"] == "success" and bad_pid_res["promoter_id"] == "3110104129"
    assert bad_pid_res["is_fallback"] is True

    # 站长自身默认推广位未配置/非法，或平台返回伪造身份时仍严格拦截
    module.PROMOTION_CACHE.clear()
    adapter.wrong_identity = True
    assert service.convert_link("jd", "opaque-material")["error_code"] == "invalid_promotion_identity"
    adapter.wrong_identity = False
    adapter.default_promoter_id = "bad_default"
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["error_code"] == "invalid_promoter_id"


def test_cache_changes_with_numeric_identity_and_default_configuration(routing):
    service, adapter, repo, users = routing
    service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    service.convert_link("jd", "opaque-material", uid="u_1234abcd")
    assert len(adapter.calls) == 1
    users.get_numeric_promotion_context.side_effect = lambda *args: {"status": "success", "promoter_id": "2468"}
    assert service.convert_link("jd", "opaque-material", uid="u_1234abcd")["promoter_id"] == "2468"
    assert len(adapter.calls) == 2
    public = service.convert_link("jd", "opaque-material")
    stored = dict(repo.save_promotion_link_cache.call_args.kwargs["data"], _expires_at=__import__("time").time() + 60)
    # 匿名公共链接仅按有效期 TTL 复用：未过期时直接命中数据库缓存（0 上游），到期后按当前默认位重刷
    module.PROMOTION_CACHE.clear()
    repo.get_promotion_link_cache.return_value = stored
    adapter.default_promoter_id = "3333"
    cached_pub = service.convert_link("jd", "opaque-material")
    assert cached_pub["final_link"] == public["final_link"]
    assert len(adapter.calls) == 3

    module.PROMOTION_CACHE.clear()
    repo.get_promotion_link_cache.return_value = dict(stored, _expires_at=__import__("time").time() - 1)
    refreshed_pub = service.convert_link("jd", "opaque-material")
    assert refreshed_pub["promoter_id"] == "3333"
    assert len(adapter.calls) == 4


def test_universal_convert_deleted_user_falls_back_to_station_master_floor(monkeypatch):
    class FakeJdUniversalAdapter:
        active = True
        platform_code = "jd"
        platform_name = "京东"
        requires_numeric_promoter_id = True
        requires_authorized_promotion = False
        default_promoter_id = "3110104129"

        def __init__(self):
            self.calls = []

        def build_command_text(self, title, final_link, price=None, coupon=None):
            return f"{title} {final_link}"

        def resolve_and_convert_universal(self, text, uid=None, **kwargs):
            self.calls.append({"uid": uid, **kwargs})
            if kwargs.get("promoter_id") == "9999":
                return {"status": "error", "error_msg": "personal position failed"}
            return {
                "status": "success",
                "short_url": "https://u.jd.com/station_floor",
                "final_link": "https://u.jd.com/station_floor",
                "promotion_token": "！站长保底京口令！",
                "promoter_id": kwargs["promoter_id"],
                "attribution_status": "verified" if uid else "anonymous",
                "title": "京东测试商品",
                "price": 19.9,
            }

    adapter = FakeJdUniversalAdapter()
    users = MagicMock()
    users.get_numeric_promotion_context.return_value = {
        "status": "error",
        "error_code": "promoter_identity_unavailable",
        "error_msg": "推手用户不存在",
    }
    monkeypatch.setattr(module, "get_adapter", lambda plat: adapter)
    svc = module.PromoteService(product_repo=MagicMock(), user_service=users)

    res1 = svc.universal_convert("https://u.jd.com/abcDEF", platform="jd", uid="u_57f51d4c")
    assert res1["status"] == "success"
    assert res1["final_link"] == "https://u.jd.com/station_floor"
    assert res1["is_fallback"] is True and res1["attribution_status"] == "anonymous" and res1["cacheable"] is False
    assert adapter.calls[-1] == {"uid": None, "promoter_id": "3110104129"}

    users.get_numeric_promotion_context.return_value = {"status": "success", "promoter_id": "9999"}
    res2 = svc.universal_convert("https://u.jd.com/abcDEF", platform="jd", uid="u_57f51d4c")
    assert res2["status"] == "success"
    assert res2["is_fallback"] is True and res2["attribution_status"] == "anonymous"
    assert [c["promoter_id"] for c in adapter.calls[-2:]] == ["9999", "3110104129"]


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
