# -*- coding: utf-8 -*-
"""多供应商可插拔扩展、主备降级、轮询分流与服务层声明式契约回归测试。"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.base import BasePlatformAdapter
from adapters.jd_adapter import JdAdapter
from adapters.pdd_adapter import PddAdapter
from adapters.tb_adapter import TaobaoAdapter
from cache import DETAIL_CACHE, MATERIAL_CACHE, clear_all_caches
from infrastructure.api_governance import GLOBAL_UPSTREAM_HEALTH
from infrastructure.http_utils import register_sensitive_provider_keywords, sanitize_user_error_message
from infrastructure.pdd_client import PddClient, register_gateway_fallback_ips
from infrastructure.settings import SettingsError, load_settings
from models.product import ProductRef, extract_canonical_goods_id, extract_jd_image_key
from services.product_service import ProductService
from services.promote_service import PromoteService
from tests.offline_support import _CONFIG


def test_register_provider_failover_and_prepend_across_all_platforms():
    # 1. JdAdapter：内置触发 RATE_LIMIT 时自动降级到备用第三方详情 API；prepend 可设为首选并在异常时退回内置
    jd_client = Mock()
    jd_client.appkey_list = ("ztk-1",)
    jd_client.get_goods_detail.return_value = {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "限流"}
    jd = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"), client=jd_client)

    jd_backup = Mock(return_value={
        "status": "success",
        "data": {"platform": "jd", "goods_id": "g1", "goods_sign": "enc_123", "name": "第三方详情商品", "price": 19.9},
    })
    jd.register_provider("detail", "jd_third_party", jd_backup)
    res_jd = jd.get_goods_detail("enc_123", goods_sign="enc_123")
    assert res_jd["status"] == "success"
    assert res_jd["_provider"] == "jd_third_party"
    assert res_jd["data"]["name"] == "第三方详情商品"

    # prepend=True 设为首选后若抛出未捕获异常，自动记入错误指标并顺延回备用通道，绝不崩溃
    jd.register_provider("detail", "jd_crash_primary", Mock(side_effect=RuntimeError("socket reset")), prepend=True)
    res_jd2 = jd.get_goods_detail("enc_123", goods_sign="enc_123")
    assert res_jd2["status"] == "success"
    assert res_jd2["_provider"] == "jd_third_party"
    assert any(
        item["provider"] == "jd_crash_primary" and item["error_calls"] >= 1
        for item in GLOBAL_UPSTREAM_HEALTH.snapshot_local().get("endpoints", [])
    )

    # 2. TaobaoAdapter：未配置好单库导致 recommend 能力关闭，注册第三方榜单后立即激活该能力
    tb = TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True, tb_app_key="k", tb_app_secret="s", tb_adzone_id="123"))
    assert tb.capabilities["recommend"] is False
    assert tb.get_recommend_goods()["error_code"] == "CONFIG_ERROR"

    tb.register_provider("recommend", "tb_third_rank", lambda **kw: {
        "status": "success",
        "data": [{"platform": "taobao", "goods_id": "t1", "goods_sign": "t1", "name": "第三方榜单品", "price": 9.9}],
        "has_more": False,
    })
    assert tb.capabilities["recommend"] is True
    res_tb = tb.get_recommend_goods(channel_type=5, limit=10)
    assert res_tb["status"] == "success" and res_tb["data"][0]["goods_id"] == "t1"

    # 3. PddAdapter：缺少官方密钥时订单能力关闭，挂载备用订单源后可正常同步
    pdd = PddAdapter(client_id="", client_secret="", pid="", enabled=True)
    assert pdd.capabilities["orders"] is False
    pdd.register_provider("orders", "pdd_relay_orders", lambda **kw: {
        "status": "success", "order_list": [{"order_sn": "p1"}], "total_count": 1, "has_more": False,
    })
    assert pdd.capabilities["orders"] is True
    res_pdd = pdd.fetch_increment_orders(100, 200)
    assert res_pdd["status"] == "success" and res_pdd["order_list"][0]["order_sn"] == "p1"


def test_round_robin_strategy_rotates_and_fails_over_within_single_call():
    jd_client = Mock()
    jd_client.appkey_list = ("ztk-1",)
    jd = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405"), client=jd_client)
    calls = []

    def p1(**kw):
        calls.append("p1")
        if len(calls) == 3:
            return {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "busy"}
        return {"status": "success", "data": {"platform": "jd", "goods_id": "1", "name": "A", "price": 10.0}}

    def p2(**kw):
        calls.append("p2")
        return {"status": "success", "data": {"platform": "jd", "goods_id": "1", "name": "B", "price": 10.0}}

    # 只启用注册的两个通道进行轮询（built-in client 通过置空 _has_ztk_catalog 退出默认通道）
    jd._has_ztk_catalog = False
    jd.register_provider("detail", "p1", p1)
    jd.register_provider("detail", "p2", p2, strategy="round_robin")

    assert jd.get_goods_detail("12345")["data"]["name"] == "A"
    assert jd.get_goods_detail("12345")["data"]["name"] == "B"
    # 第 3 次轮到 p1，返回 RATE_LIMIT 时单次请求内无缝顺延到 p2 成功
    assert jd.get_goods_detail("12345")["data"]["name"] == "B"
    assert calls == ["p1", "p2", "p1", "p2"]

    with pytest.raises(ValueError):
        jd.set_provider_strategy("detail", "invalid_mode")


@pytest.mark.parametrize("err_code", [
    "PRODUCT_OFF_PROMOTION",
    "AUTH_REQUIRED",
    "IDENTITY_MISMATCH",
    "AMBIGUOUS_IDENTITY",
    "INCOMPLETE_IDENTITY",
])
def test_deterministic_errors_short_circuit_without_calling_backup_providers(err_code):
    jd_client = Mock()
    jd_client.appkey_list = ("ztk-1",)
    jd = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405"), client=jd_client)
    backup = Mock(return_value={"status": "success", "data": {"name": "不应被调用"}})
    jd.register_provider("detail", "primary_det", lambda **kw: {"status": "error", "error_code": err_code, "error_msg": "确定错误"}, prepend=True)
    jd.register_provider("detail", "backup", backup)

    res = jd.get_goods_detail("12345")
    assert res["status"] == "error" and res["error_code"] == err_code
    backup.assert_not_called()


def test_pdd_anonymous_promotion_tries_extra_provider_before_direct_link_fallback(monkeypatch):
    pdd = PddAdapter(client_id="c", client_secret="s", pid="pid", enabled=True)
    monkeypatch.setattr(pdd, "_attempt_promotions", lambda *a, **kw: (None, None, None, False))

    pdd.register_provider("promotion", "pdd_third_promo", lambda **kw: {
        "status": "success", "final_link": "https://u.third.test/p", "is_fallback": False,
    })
    res = pdd.convert_promotion_url("E_SIGN_1234567890", uid=None)
    assert res["status"] == "success"
    assert res["is_fallback"] is False
    assert res["final_link"] == "https://u.third.test/p"


def test_pdd_client_wrapper_and_register_gateway_fallback_ips(monkeypatch):
    from infrastructure import pdd_client

    fake_ips = dict(pdd_client._GATEWAY_FALLBACK_IPS)
    fake_cache = dict(pdd_client._PDD_DNS_CACHE)
    monkeypatch.setattr(pdd_client, "_GATEWAY_FALLBACK_IPS", fake_ips)
    monkeypatch.setattr(pdd_client, "_PDD_DNS_CACHE", fake_cache)
    monkeypatch.setattr(pdd_client, "_PDD_DNS_ENABLED", True)

    register_gateway_fallback_ips("API.CustomGateway.Test", ["10.0.0.1", " 10.0.0.2 "])
    assert fake_ips["api.customgateway.test"] == ("10.0.0.1", "10.0.0.2")
    assert len(fake_cache["api.customgateway.test"][1]) == 2

    sess = SimpleNamespace(head=Mock(), post=Mock(return_value=SimpleNamespace(raise_for_status=Mock(), json=lambda: {"ok": True})))
    cli = PddClient("cid", "csec", "pid", http_session=sess)
    cli.warmup()
    sess.head.assert_called_once()
    assert cli.call_api("pdd.test", {"a": 1}) == {"ok": True}
    assert sess.post.call_count == 1


def test_settings_allows_ztk_only_taobao_profile(tmp_path):
    cfg = dict(
        _CONFIG,
        pdd_enabled=False,
        taobao_enabled=True,
        myself_pdd_client_id="",
        myself_pdd_client_secret="",
        myself_pdd_pid="",
        myself_taobao_app_key="",
        myself_taobao_app_secret="",
        zhetaoke_appkey="ztk-key",
        zhetaoke_sid="ztk-sid",
        myself_taobao_pid="mm_11_22_33",
    )
    path = tmp_path / "cfg_ztk_tb.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.taobao_enabled and loaded.ztk_appkey == "ztk-key" and loaded.tb_adzone_id == "33"

    cfg_partial = dict(cfg, zhetaoke_sid="")
    bad_path = tmp_path / "cfg_ztk_bad.json"
    bad_path.write_text(json.dumps(cfg_partial), encoding="utf-8")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(bad_path)
    assert "zhetaoke_sid" in str(exc_info.value)


def test_cross_provider_jd_image_key_and_keyword_redaction(monkeypatch):
    u1 = "https://img14.360buyimg.com/pop/jfs/t1/100/20/300/400/6af74ca5F32ceb84f/0083320320631ee4.jpg"
    u2 = "//img10.360buyimg.com/n1/s450x450_jfs/t1/100/20/300/400/6af74ca5F32ceb84f/0083320320631ee4.jpg!q70.dpg"
    u3 = "https://img11.360buyimg.com/pop/jfs/t1/100/20/300/400/6af74ca5F32ceb84f/0083320320631ee4.jpg.avif"
    assert extract_jd_image_key(u1) == extract_jd_image_key(u2) == extract_jd_image_key(u3) == "6af74ca5F32ceb84f_0083320320631ee4"
    assert extract_canonical_goods_id("jd", "signA", image_url=u2) == "6af74ca5F32ceb84f_0083320320631ee4"

    from infrastructure import http_utils
    monkeypatch.setattr(http_utils, "_PROVIDER_LEAK_KEYWORDS", list(http_utils._PROVIDER_LEAK_KEYWORDS))
    register_sensitive_provider_keywords("NewCustomVendorX")
    sanitized = sanitize_user_error_message("UPSTREAM_ERROR", "NewCustomVendorX appkey expired", platform_name="京东")
    assert "NewCustomVendorX" not in sanitized


def test_services_use_declarative_adapter_hooks_for_arbitrary_platform(monkeypatch):
    clear_all_caches()

    class CustomPlatformAdapter(BasePlatformAdapter):
        platform_code = "customplat"
        platform_name = "自定义联盟"
        requires_material_url = True
        supports_coupon_enrichment = True
        preserves_known_coupons_across_rank = True

        def __init__(self):
            self._ensure_provider_state()
            self.capabilities = {
                "recommend": True, "search": True, "detail": True,
                "promotion": True, "authorization": False, "orders": False,
            }
            self.detail_calls = []
            self.promote_calls = []

        def is_authoritative_material_url(self, url: str) -> bool:
            return "auth.custom.test" in str(url or "")

        def extract_trusted_coupon_url(self, doc=None) -> str:
            url = str((doc or {}).get("coupon_url") or "")
            return url if url.startswith("https://coupon.custom.test/") else ""

        def should_enrich_promotion_coupon(self, ref: ProductRef, local_doc=None) -> bool:
            return bool(ref.goods_sign) and (local_doc is None or local_doc.get("detail_updated_at") is None)

        def needs_coupon_upgrade(self, universal_result: dict, product=None) -> bool:
            return bool(product and product.get("coupon_url") and product.get("product_url") and not universal_result.get("coupon_url"))

        def skip_prefetch_if_detail_checked(self, ref: ProductRef, item=None):
            if isinstance(item, dict) and item.get("detail_updated_at") is not None:
                return 3600.0
            return None

        def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
            return ProductRef(self.platform_code, goods_id or "", goods_sign or goods_id_or_sign)

        def get_recommend_goods(self, **kwargs):
            return {"status": "success", "data": [
                {"platform": "customplat", "goods_id": "c1", "goods_sign": "SIG_C1",
                 "name": "自定义商品", "price": 20.0, "coupon_amount": 0.0,
                 "product_url": "https://weak.custom.test/item/c1"}
            ], "has_more": False}

        def search_goods(self, **kwargs):
            return {"status": "success", "data": []}

        def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs):
            self.detail_calls.append(goods_sign or goods_id_or_sign)
            return {"status": "success", "data": {
                "platform": "customplat", "goods_id": "c1", "goods_sign": "SIG_C1",
                "name": "自定义商品", "price": 15.0, "image_url": "https://img.custom.test/c1.jpg",
                "coupon_amount": 5.0, "coupon_url": "https://coupon.custom.test/5off",
                "product_url": "https://auth.custom.test/detail/c1",
            }}

        def convert_promotion_url(self, goods_id_or_sign, uid=None, **kwargs):
            self.promote_calls.append(dict(kwargs))
            return {
                "status": "success", "url": "https://u.custom.test/up", "final_link": "https://u.custom.test/up",
                "short_url": "https://u.custom.test/up", "coupon_url": kwargs.get("trusted_coupon_url", ""),
                "attribution_status": "anonymous",
            }

        def resolve_and_convert_universal(self, raw_text: str, uid=None, **kwargs):
            return {
                "status": "success", "url": "https://u.custom.test/raw", "final_link": "https://u.custom.test/raw",
                "short_url": "https://u.custom.test/raw", "goods_id": "c1", "goods_sign": "SIG_C1",
                "title": "自定义商品", "price": 15.0, "coupon_amount": 5.0, "attribution_status": "anonymous",
                "product": {
                    "platform": "customplat", "goods_id": "c1", "goods_sign": "SIG_C1",
                    "name": "自定义商品", "price": 15.0, "image_url": "https://img.custom.test/c1.jpg",
                    "coupon_url": "https://coupon.custom.test/5off",
                    "product_url": "https://auth.custom.test/detail/c1",
                },
            }

        def check_authority(self, uid): return {"status": "not_required", "is_authorized": False}
        def generate_authority_url(self, uid, **kwargs): return {"status": "not_required", "is_authorized": False}
        def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs):
            return {"status": "success", "order_list": [], "total_count": 0}

    custom = CustomPlatformAdapter()
    import adapters as adapters_pkg
    monkeypatch.setitem(adapters_pkg._ADAPTER_REGISTRY, "customplat", custom)

    repo = Mock()
    repo.get_product_detail.return_value = None
    repo.get_promotion_link_cache.return_value = None
    repo.upsert_product_detail.return_value = True
    repo.get_known_coupon_overlays.return_value = {}

    prod_svc = ProductService(product_repo=repo)
    promo_svc = PromoteService(product_repo=repo)

    # 1. 权威物料URL保护：已缓存权威 auth.custom.test 不被榜单 weak.custom.test 覆盖
    MATERIAL_CACHE.set(("customplat", "sign", "SIG_C1"), "https://auth.custom.test/detail/c1")
    rec = prod_svc.get_home_recommendations(platform="customplat", channel_type=5)
    assert rec["status"] == "success"
    repo.get_known_coupon_overlays.assert_called_once()
    assert MATERIAL_CACHE.get(("customplat", "sign", "SIG_C1")) == "https://auth.custom.test/detail/c1"

    # 2. 点击转链触发单次补券 + 二合一参数透传，全程零硬编码平台判断
    conv = promo_svc.convert_link("customplat", "SIG_C1", goods_sign="SIG_C1")
    assert conv["status"] == "success"
    assert custom.detail_calls == ["SIG_C1"]
    assert custom.promote_calls[-1]["trusted_coupon_url"] == "https://coupon.custom.test/5off"

    # 3. 万能转链后自动执行优惠券二合一升级转链
    univ = promo_svc.universal_convert("https://u.jd.com/custom_c1", platform="customplat")
    assert univ["status"] == "success"
    assert univ["final_link"] == "https://u.custom.test/up"
    assert univ["coupon_url"] == "https://coupon.custom.test/5off"

    # 4. 后台预取遵从 skip_prefetch_if_detail_checked 返回的 3600s 冷却时间，不重复打详情接口
    repo.get_product_detail.return_value = {
        "platform": "customplat", "goods_id": "c1", "goods_sign": "SIG_C1",
        "image_url": "https://img.custom.test/c1.jpg",
        "detail_updated_at": datetime.now(timezone.utc),
    }
    DETAIL_CACHE.clear()
    pre = prod_svc.prefetch_product_detail(ProductRef("customplat", "c1", "SIG_C1"))
    assert pre.status == "ready"
    assert len(custom.detail_calls) == 1
    clear_all_caches()


def test_all_capability_operation_slots_and_runtime_strategy_switch():
    # 1. TaobaoAdapter 支持从零凭据启动后通过 register_provider 激活全量能力（recommend/search/detail/promotion/universal/auth/orders）
    tb = TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True))
    assert not any(tb.capabilities.values())
    tb.register_provider("recommend", "ext_tb_rec", lambda **kw: {"status": "success", "data": [{"goods_id": "r1"}]})
    tb.register_provider("search", "ext_tb_search", lambda **kw: {"status": "success", "data": [{"goods_id": "s1"}]})
    tb.register_provider("detail", "ext_tb_detail", lambda **kw: {"status": "success", "data": {"goods_id": "d1"}})
    tb.register_provider("promotion", "ext_tb_promo", lambda **kw: {"status": "success", "final_link": "https://s.click/x"})
    tb.register_provider("universal", "ext_tb_univ", lambda **kw: {"status": "success", "final_link": "https://s.click/u"})
    tb.register_provider("check_authority", "ext_tb_auth", lambda **kw: {"status": "success", "is_authorized": True, "bind": 1})
    tb.register_provider("generate_authority_url", "ext_tb_auth_url", lambda **kw: {"status": "success", "auth_url": "https://auth.tb/1"})
    tb.register_provider("orders", "ext_tb_orders", lambda **kw: {"status": "success", "order_list": [], "total_count": 0})
    assert all(tb.capabilities.values())
    assert tb.get_recommend_goods(channel_type=5, limit=10)["data"][0]["goods_id"] == "r1"
    assert tb.search_goods("paper")["data"][0]["goods_id"] == "s1"
    assert tb.get_goods_detail("d1")["data"]["goods_id"] == "d1"
    assert tb.convert_promotion_url("d1")["final_link"] == "https://s.click/x"
    assert tb.resolve_and_convert_universal("https://item.taobao.com/item.htm?id=123")["final_link"] == "https://s.click/u"
    assert tb.check_authority("u_12345678")["is_authorized"] is True
    assert tb.generate_authority_url("u_12345678")["auth_url"] == "https://auth.tb/1"
    assert tb.fetch_increment_orders(1, 2)["status"] == "success"

    # 2. JdAdapter universal / orders 在无折淘客配置时也能通过 register_provider 接入，并可随时切换 failover / round_robin
    jd = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_position_id="8899"))
    assert jd.capabilities["orders"] is False and jd.capabilities["promotion"] is False
    jd.register_provider("universal", "ext_jd_u1", lambda **kw: {"status": "success", "final_link": "https://u.jd.com/1", "_tag": "u1"})
    jd.register_provider("universal", "ext_jd_u2", lambda **kw: {"status": "success", "final_link": "https://u.jd.com/2", "_tag": "u2"})
    assert jd.resolve_and_convert_universal("https://u.jd.com/abc")["_tag"] == "u1"
    jd.set_provider_strategy("universal", "round_robin")
    assert jd.resolve_and_convert_universal("https://u.jd.com/abc")["_tag"] == "u1"
    assert jd.resolve_and_convert_universal("https://u.jd.com/abc")["_tag"] == "u2"
    jd.set_provider_strategy("universal", "failover")
    assert jd.resolve_and_convert_universal("https://u.jd.com/abc")["_tag"] == "u1"

    # 3. PddAdapter universal 支持前置新接口并在失败时无缝退回官方路由
    pdd = PddAdapter(client_id="c", client_secret="s", pid="pid", enabled=True)
    pdd.register_provider("universal", "ext_pdd_univ", lambda **kw: {"status": "success", "final_link": "https://p.pdd/ext"}, prepend=True)
    assert pdd.resolve_and_convert_universal("https://mobile.yangkeduo.com/goods.html?goods_sign=E_1234567890123456789012345")["final_link"] == "https://p.pdd/ext"

