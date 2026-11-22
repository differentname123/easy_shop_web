# -*- coding: utf-8 -*-
"""
系统级运行隐患修复与上游商品/转链 API 统一治理层回归测试套件。
覆盖核心治理契约与跨模块系统隐患：
  1. 统一 API 治理层（前台/后台自适应配额节流、多通道故障转移链、四态结果契约、健康监控端点）；
  2. 京东 u.jd.com 单品保留 tkl_piliang 补全详情与多 Key 轮询防空 tao_id；
  3. 淘宝全变体口令跨模块平台识别单一真源对齐；
  4. 万能转链 -> 详情页转链防幽灵空商品（DETAIL_CACHE 身份补齐 + only_if_exists=True 禁硬插空壳）；
  5. 内存/句柄/磁盘防泄漏（有界 LRU anchor_cache、有界 JSONL 轮转、闲鱼浏览器串行锁、AI 超时日志安全）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cache import DETAIL_CACHE, MATERIAL_CACHE, PROMOTION_CACHE, SIGN_BY_ID_CACHE
from infrastructure.api_governance import (
    GLOBAL_UPSTREAM_HEALTH,
    QuotaPaceBacklogError,
    ResolutionKind,
    UpstreamQuotaPacer,
    api_caller_context,
    execute_provider_chain,
    get_caller_context,
    is_valid_product_card,
    stamp_universal_resolution,
)
from models.product import detect_platform_signal, extract_search_keyword
from repositories.product_repo import ProductRepository
from services.promote_service import PromoteService


# ==============================================================================
# 1. 统一 API 治理层契约验证
# ==============================================================================
def test_upstream_quota_pacer_foreground_vs_background_budget(monkeypatch):
    clock = {"now": 100.0, "slept": 0.0}
    monkeypatch.setattr("infrastructure.api_governance.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "infrastructure.api_governance.time.sleep",
        lambda s: (clock.__setitem__("slept", clock["slept"] + s), clock.__setitem__("now", clock["now"] + s)),
    )

    pacer = UpstreamQuotaPacer()
    pacer._next_allowed_ts[("test_ztk_catalog", "key_a")] = 103.0

    # 前台请求（默认 foreground）：等待 3.0s > foreground_max_wait(1.5s)，立即抛出 QuotaPaceBacklogError 快速降级
    assert get_caller_context() == "foreground"
    with pytest.raises(QuotaPaceBacklogError):
        pacer.pace("test_ztk_catalog", "key_a", 1.0, foreground_max_wait=1.5, background_max_wait=6.0)

    # 后台任务（background 上下文）：允许平滑排队等待 3.0s <= 6.0s，绝不误杀有效商品
    with api_caller_context("background"):
        assert get_caller_context() == "background"
        waited_bg = pacer.pace("test_ztk_catalog", "key_a", 1.0, foreground_max_wait=1.5, background_max_wait=6.0)
        assert waited_bg == pytest.approx(3.0)
    assert get_caller_context() == "foreground"


def test_execute_provider_chain_failover_and_deterministic_short_circuit():
    GLOBAL_UPSTREAM_HEALTH.reset()
    calls = []

    def step_primary_rate_limited():
        calls.append("primary")
        return {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "触发频控"}

    def step_backup_ok():
        calls.append("backup")
        return {"status": "success", "data": {"goods_id": "1001", "name": "维达抽纸", "price": 19.9}}

    res = execute_provider_chain(
        platform="jd",
        operation="detail",
        providers=[
            ("ztk_detail", step_primary_rate_limited),
            ("official_backup", step_backup_ok),
        ],
    )
    assert calls == ["primary", "backup"]
    assert res["status"] == "success"
    assert res["_provider"] == "official_backup"

    # 若首通道返回确定性业务拦截（如纯数字 SKU 不支持），严禁继续盲调备用通道浪费配额
    calls.clear()

    def step_deterministic_reject():
        calls.append("reject")
        return {"status": "error", "error_code": "UNSUPPORTED_JD_NUMERIC_SKU", "error_msg": "不支持纯数字SKU"}

    res_det = execute_provider_chain(
        platform="jd",
        operation="universal_convert",
        providers=[
            ("official_primary", step_deterministic_reject),
            ("backup_should_not_run", step_backup_ok),
        ],
    )
    assert calls == ["reject"]
    assert res_det["error_code"] == "UNSUPPORTED_JD_NUMERIC_SKU"

    snap = GLOBAL_UPSTREAM_HEALTH.get_combined_health_report()
    assert len(snap["aggregated_endpoints"]) >= 2


def test_stamp_universal_resolution_strict_four_state_contract():
    # 1) 完整商品卡片 -> product
    valid_prod = {
        "goods_id": "jfs/abcdef0123456789",
        "goods_sign": "enc_1",
        "name": "农夫山泉550ml*24",
        "image_url": "https://img14.360buyimg.com/n1/jfs/t1/1.jpg",
        "price": 24.9,
    }
    assert is_valid_product_card(valid_prod) is True
    r_prod = stamp_universal_resolution(
        {"status": "success", "final_link": "https://u.jd.com/new1", "product": valid_prod},
    )
    assert r_prod["resolution_kind"] == ResolutionKind.PRODUCT
    assert "detail_lookup_failed" not in r_prod

    # 2) 转链成功但缺商品卡片，且具备单品证据（如解包出加密 itemId）-> 强制标记 degraded_product + detail_lookup_failed=True
    r_deg = stamp_universal_resolution(
        {"status": "success", "final_link": "https://u.jd.com/new2", "product": None},
    )
    assert r_deg["resolution_kind"] == ResolutionKind.DEGRADED_PRODUCT
    assert r_deg["detail_lookup_failed"] is True

    # 3) 真正的纯领券/活动会场链接（必须具备正向 verified_coupon_or_venue=True 实证）-> coupon_or_venue
    r_cpn = stamp_universal_resolution(
        {"status": "success", "final_link": "https://u.jd.com/venue1", "product": None},
        verified_coupon_or_venue=True,
    )
    assert r_cpn["resolution_kind"] == ResolutionKind.COUPON_OR_VENUE
    assert not r_cpn.get("detail_lookup_failed")

    # 4) Fail-Closed：无商品卡且缺少正向会场实证 (verified_coupon_or_venue=None) 时绝不能误标为纯券
    r_unverified = stamp_universal_resolution(
        {"status": "success", "final_link": "https://u.jd.com/unknown", "product": None},
    )
    assert r_unverified["resolution_kind"] == ResolutionKind.DEGRADED_PRODUCT
    assert r_unverified["detail_lookup_failed"] is True


def test_upstream_health_monitoring_endpoint():
    from main import app

    client = TestClient(app)
    resp = client.get("/api/system/upstream-health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "aggregated_endpoints" in body
    assert "processes" in body


# ==============================================================================
# 2. 京东 u.jd.com / union-click 单品保留 tkl_piliang 补全详情 + 多 Key 轮询防空 tao_id + 无“抢购”单品防误判 OK_COUPON
# ==============================================================================
def test_jd_convert_enriches_u_jd_via_piliang_with_key_failover_and_skips_only_jingfen_or_venue():
    from adapters.jd_adapter import JdAdapter
    from infrastructure.jd_client import JdClient, PROMOTION_ROOT

    calls = []

    class _FakeHttp:
        def get(self, url, **kw):
            params = kw.get("params") or {}
            calls.append((url, params.get("appkey"), params.get("materialId") or params.get("tkl") or params.get("q")))
            if "open_jing_union_open_promotion_byunionid_get.ashx" in url:
                mat = str(params.get("materialId") or "")
                j_cmd = (
                    ""
                    if "oa0INNe" in mat
                    else "11:/越峰花茉莉飘雪花茶浓香型袋装50克！AC8yV1DP031LLfr6！ CA1393"
                )
                payload = {
                    PROMOTION_ROOT: {
                        "result": json.dumps(
                            {
                                "code": 200,
                                "data": {
                                    "shortURL": "https://u.jd.com/officialShort1",
                                    "clickURL": "https://union-click.jd.com/jdc?e=1",
                                    "jCommand": j_cmd,
                                    "jShortCommand": "！AC8yV1DP031LLfr6！ CA1393" if j_cmd else "",
                                },
                            }
                        )
                    }
                }
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)

            if "open_gaoyongzhuanlian_tkl_piliang.ashx" in url:
                # 模拟首个轮询命中的 ztk_k1 频控返回空 tao_id，自动故障切换至 ztk_k2 返回真实加密 tao_id 与商品详情
                if params.get("appkey") == "ztk_k1":
                    payload = {"status": 200, "result_url": "https://u.jd.com/s1", "tao_id": "", "title": ""}
                else:
                    payload = {
                        "status": 200,
                        "result_url": "https://u.jd.com/s2",
                        "tao_id": "BFQsJ9uP57Tw9XUt2BLLw9XwJs1PWQ_3hj3gPHIC2A97hgSPd",
                        "title": "越峰花茉莉飘雪花茶浓香型袋装50克",
                        "pic": "https://img14.360buyimg.com/pop/jfs/t1/568869/28/6699/278367/6aedce51F01fd44fe/00834e64e62f0766.jpg",
                        "size": "15.8",
                        "quanhou_jiage": "15.8",
                        "nick": "越峰花茶叶京东自营旗舰店",
                    }
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)

            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"status": 301, "content": "无符合条件的数据"},
            )

    client = JdClient(
        appkey="ztk_k1",
        union_id="10001",
        http_session=_FakeHttp(),
        appkey_list=("ztk_k1", "ztk_k2"),
    )
    adapter = JdAdapter(client=client, jd_enabled=True, jd_position_id="10001")

    # 1) 非选品库、无“抢购”字样的普通京东单品：通过 tkl_piliang 多 Key 轮询直接拿到完整商品卡
    res_prod = adapter.resolve_and_convert_universal("https://u.jd.com/oGP72oi", promoter_id="10001")
    assert res_prod["status"] == "success"
    assert res_prod["resolution_kind"] == "product"
    assert res_prod["product"]["name"] == "越峰花茉莉飘雪花茶浓香型袋装50克"
    assert res_prod["product"]["price"] == 15.8
    piliang_keys = [k for u, k, _ in calls if "piliang" in u]
    assert piliang_keys == ["ztk_k1", "ztk_k2"]

    # 2) 纯京口令（无 URL）：byunionid_get 转出 shortURL 后，向 piliang 透传该 shortURL 而非裸口令（防 301 转链失败）
    calls.clear()
    res_jcmd = adapter.resolve_and_convert_universal("11:/越峰花茉莉飘雪花茶！AC8yV1DP031LLfr6！ CA1393", promoter_id="10001")
    assert res_jcmd["status"] == "success"
    assert res_jcmd["resolution_kind"] == "product"
    piliang_tkls = [mat for u, _, mat in calls if "piliang" in u]
    assert piliang_tkls and all(t == "https://u.jd.com/officialShort1" for t in piliang_tkls)

    # 2b) 原始长 union-click.jd.com 链接：同样优先透传 byunionid_get 转出的 shortURL (u.jd.com) 给 piliang，避免 ZTK 拒识长链报 301
    calls.clear()
    res_uc = adapter.resolve_and_convert_universal("https://union-click.jd.com/jdc?e=618%7Cpc%7C&p=abcLong", promoter_id="10001")
    assert res_uc["status"] == "success"
    assert res_uc["resolution_kind"] == "product"
    uc_piliang_tkls = [mat for u, _, mat in calls if "piliang" in u]
    assert uc_piliang_tkls and all(t == "https://u.jd.com/officialShort1" for t in uc_piliang_tkls)

    # 3) 纯领券短链（jCommand 为空）：它到底是什么，以实网结果为准，不能靠 mock 想象。
    #    2026-11-20 实网验证（真实 ztk Key + 真实站点）：
    #      · u.jd.com/oa0INNe（本用例的券短链）连跑 3 次稳定 = degraded_product（无 name/price，detail_lookup_failed=True）
    #      · 真实数据里的券链接 u.jd.com/5reWTig（取自特惠采采 material_info.coupon_urls）同样 = degraded_product
    #      · 真实下单短链 u.jd.com/oGpSC4R / ogpfg4F = product，带真实商品名与价格（如 49.9）
    #    也就是说：券短链本身拿不到商品卡，"拿不到卡就不可能被标成 product"；旧断言 coupon_or_venue 也是错的。
    #    这里锁住真正的不变量：无商品卡 → 绝不标 product，且不凭空编造商品卡。
    #    （新语义下券短链也会去 piliang 试解析，不再用"口令为空"短路，所以 piliang 会被调用，这是有意的。）
    class _NoCardHttp(_FakeHttp):
        def get(self, url, **kw):
            if "open_gaoyongzhuanlian_tkl_piliang.ashx" in url:
                calls.append((url, kw.get("params", {}).get("appkey"), kw.get("params", {}).get("tkl")))
                return SimpleNamespace(
                    status_code=200,
                    raise_for_status=lambda: None,
                    json=lambda: {"status": 301, "content": "无符合条件的数据"},
                )
            return super().get(url, **kw)

    calls.clear()
    adapter.client = JdClient(
        appkey="ztk_k1", union_id="10001", http_session=_NoCardHttp(), appkey_list=("ztk_k1", "ztk_k2")
    )
    res_cpn = adapter.resolve_and_convert_universal("https://u.jd.com/oa0INNe", promoter_id="10001")
    assert res_cpn["status"] == "success"
    assert res_cpn["product"] in (None, {})
    assert res_cpn["resolution_kind"] != "product", "没有商品卡时绝不能标成 product"
    assert res_cpn["resolution_kind"] in ("coupon_or_venue", "degraded_product")
    assert any("piliang" in u for u, _, _ in calls), "券短链也应尝试 piliang 解析（不再按口令为空短路）"

    # 4) 若 tkl_piliang 空 tao_id，自动回退一次精确短链 api_quanwang(q=short_u, youquan=0)；若仍无数据则稳健标记 degraded_product
    class _AllThrottledHttp(_FakeHttp):
        def get(self, url, **kw):
            if "open_gaoyongzhuanlian_tkl_piliang.ashx" in url:
                calls.append((url, kw.get("params", {}).get("appkey"), kw.get("params", {}).get("tkl")))
                return SimpleNamespace(
                    status_code=200,
                    raise_for_status=lambda: None,
                    json=lambda: {"status": 200, "result_url": "https://u.jd.com/s1", "tao_id": ""},
                )
            return super().get(url, **kw)

    calls.clear()
    adapter.client = JdClient(appkey="ztk_k1", union_id="10001", http_session=_AllThrottledHttp(), appkey_list=("ztk_k1", "ztk_k2"))
    res_deg = adapter.resolve_and_convert_universal("https://u.jd.com/oGP72oi", promoter_id="10001")
    assert res_deg["status"] == "success"
    assert res_deg["resolution_kind"] == "degraded_product"
    assert res_deg.get("detail_lookup_failed") is True
    assert res_deg["title"] == "京东精选推广好物"
    assert any("api_quanwang" in u and q == "https://u.jd.com/officialShort1" for u, _, q in calls)


def test_jd_universal_piliang_empty_tao_id_early_stop_and_hdk_or_quanwang_recovery():
    """验证真实退化恢复链路：
    1. tkl_piliang 全局返回空 tao_id 时，3-key 池最多尝试 2 次即早停，不浪费第 3 把 Key；
    2. HDK 解包官方短链返回加密 sku_id -> api_detail 拿全量商品卡（以及 is_venue=True -> coupon_or_venue）；
    3. 落地仅为数字 SKU 时，唯一匹配 api_quanwang(q=short_u)（剥离残缺 |h 后缀）救回商品卡，且 len != 1 时严禁误用。
    """
    from adapters.jd_adapter import JdAdapter
    from infrastructure.jd_client import JdClient, PROMOTION_ROOT

    enc_sku = "QIOFn31J70YqJ2LZfIp3i8gOzXpo4_3fgGzi3ummOJ1tnRAy"
    calls = []

    class _EarlyStopHttp:
        def get(self, url, **kw):
            params = kw.get("params") or {}
            calls.append((url, params.get("appkey"), params))
            if "byunionid_get" in url:
                return SimpleNamespace(
                    status_code=200,
                    raise_for_status=lambda: None,
                    json=lambda: {
                        PROMOTION_ROOT: {
                            "result": json.dumps({
                                "code": 200,
                                "data": {"shortURL": "https://u.jd.com/shortOk", "clickURL": "https://union-click.jd.com/longOk"},
                            })
                        }
                    },
                )
            if "tkl_piliang" in url:
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"status": 200, "tao_id": "", "content": "https://u.jd.com/p"})
            if "api_detail" in url:
                return SimpleNamespace(
                    status_code=200,
                    raise_for_status=lambda: None,
                    json=lambda: {
                        "status": 200,
                        "content": [{
                            "tao_id": enc_sku,
                            "title": "美的燃气灶A3",
                            "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/6ab9c932F86c506ae/0083320320b195ad.jpg",
                            "quanhou_jiage": "488.81",
                            "size": "714",
                            "item_url": f"https://jingfen.jd.com/detail/{enc_sku}.html",
                        }],
                    },
                )
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"status": 301, "content": "无符合条件的数据"})

    # 1 & 2a: union-click + HDK 零配额先行解包返回加密 sku -> 跳过 tkl_piliang，直接 api_detail 拿全卡
    hdk_enc = SimpleNamespace(
        resolve_short_url=lambda _u: {"status": "success", "data": {"sku_id": enc_sku, "is_venue": False}},
        convert_promotion_link=lambda *_a, **_kw: {"status": "error", "error_code": "NOT_SUPPORTED"},
    )
    cli1 = JdClient(union_id="2035679405", appkey_list=("k1", "k2", "k3"), http_session=_EarlyStopHttp())
    ad1 = JdAdapter(client=cli1, jd_enabled=True, jd_position_id="88", hdk_client=hdk_enc)
    res_enc = ad1.resolve_and_convert_universal("https://union-click.jd.com/jdc?e=&p=JF8BAQ", promoter_id="88")
    assert res_enc["status"] == "success"
    assert res_enc["resolution_kind"] == "product"
    assert res_enc["product"]["name"] == "美的燃气灶A3"
    assert res_enc["product"]["price"] == 488.81
    piliang_calls = [c for c in calls if "tkl_piliang" in c[0]]
    assert len(piliang_calls) == 0

    # 2b: union-click 会场 -> HDK 先行解包 is_venue=True 识别为 coupon_or_venue，同样 0 次 tkl_piliang
    calls.clear()
    hdk_venue = SimpleNamespace(
        resolve_short_url=lambda _u: {"status": "success", "data": {"sku_id": "", "is_venue": True}},
        convert_promotion_link=lambda *_a, **_kw: {"status": "error", "error_code": "NOT_SUPPORTED"},
    )
    cli2 = JdClient(union_id="2035679405", appkey_list=("k1", "k2", "k3"), http_session=_EarlyStopHttp())
    ad2 = JdAdapter(client=cli2, jd_enabled=True, jd_position_id="88", hdk_client=hdk_venue)
    res_venue = ad2.resolve_and_convert_universal("https://union-click.jd.com/jdc?e=&p=JF8B_VENUE", promoter_id="88")
    assert res_venue["status"] == "success"
    assert res_venue["resolution_kind"] == "coupon_or_venue"
    assert [c for c in calls if "tkl_piliang" in c[0]] == []

    # 3: 数字 SKU 解包后：3-key tkl_piliang 空 tao_id 第 2 次早停，随后走 Fallback B api_quanwang(q=short_u) 仅 len==1 时采信并清洗 |h；len==2 严格拒绝
    class _QuanwangHttp(_EarlyStopHttp):
        def __init__(self, row_count):
            self.row_count = row_count

        def get(self, url, **kw):
            if "api_quanwang" in url:
                row = {
                    "tao_id": "xPQNgratvgsxvGGtlMZ7xvG7SFJilQ_3LZQS3mHtarEqceyab",
                    "title": "欧贝拉亚麻籽油500ml",
                    "pict_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/6af414c4Fef8155cc/0083320320b2df3d.jpg",
                    "small_images": "https://img14.360buyimg.com/a.jpg|h",
                    "quanhou_jiage": "45.4",
                    "size": "45.4",
                    "item_url": "https://jingfen.jd.com/detail/xPQNgratvgsxvGGtlMZ7xvG7SFJilQ_3LZQS3mHtarEqceyab.html",
                }
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"status": 200, "content": [dict(row) for _ in range(self.row_count)]})
            return super().get(url, **kw)

    hdk_num = SimpleNamespace(
        resolve_short_url=lambda _u: {"status": "success", "data": {"sku_id": "10239086058847", "is_venue": False}},
        convert_promotion_link=lambda *_a, **_kw: {"status": "error", "error_code": "RATE_LIMIT"},
    )
    calls.clear()
    ad_num1 = JdAdapter(
        client=JdClient(union_id="2035679405", appkey_list=("k1", "k2", "k3"), http_session=_QuanwangHttp(1)),
        jd_enabled=True,
        jd_position_id="88",
        hdk_client=hdk_num,
    )
    res_num1 = ad_num1.resolve_and_convert_universal("https://u.jd.com/ogVRibX", promoter_id="88")
    assert res_num1["status"] == "success"
    assert res_num1["resolution_kind"] == "product"
    assert res_num1["product"]["name"] == "欧贝拉亚麻籽油500ml"
    assert res_num1["product"]["carousel_images"] == ["https://img14.360buyimg.com/a.jpg"]
    assert len([c for c in calls if "tkl_piliang" in c[0]]) == 2

    ad_num2 = JdAdapter(
        client=JdClient(union_id="2035679405", appkey_list=("k1", "k2"), http_session=_QuanwangHttp(2)),
        jd_enabled=True,
        jd_position_id="88",
        hdk_client=hdk_num,
    )
    res_num2 = ad_num2.resolve_and_convert_universal("https://u.jd.com/ogVRibX", promoter_id="88")
    assert res_num2["status"] == "success"
    assert res_num2["resolution_kind"] == "degraded_product"
    assert res_num2["product"] is None


# ==============================================================================
# 3. 淘宝全变体口令单一真源识别（含 Emoji 🗝/💲，且不误伤搜索框括号型号词与非11位缓存键）
# ==============================================================================
def test_taobao_token_variants_detected_even_when_default_plat_is_jd():
    from models.product import classify_user_input, extract_taobao_token_core
    from services.deal_pool_cleaner import DealPoolCleaner, detect_candidate_platform

    samples = [
        "(kP89fb8d16p)/",
        "/hU89fb8d16p/",
        "8(abc12345678) AC01 速度抢",
        "/CZ00/ 4$abc12345678$://",
        "【HU293 abc12345678】",
        "「MF168 zyx98765432」",
        "77 CZ057 2:/🗝pir46VYeOmG💲",
        "(1q466V6pbHz) SY66",
        "/YD168/ (b8Go6VTywRq)",
    ]
    for s in samples:
        assert detect_platform_signal(s) == "taobao", f"failed signal for {s}"
        assert detect_candidate_platform(s, default_plat="jd") == "taobao", f"failed candidate plat for {s}"

    assert extract_taobao_token_core("77 CZ057 2:/🗝pir46VYeOmG💲") == "pir46VYeOmG"

    # 搜索框输入带括号的常见数码型号词不应被误判为淘口令
    model_kw = classify_user_input("(iPhone16Pro) 手机壳")
    assert model_kw["kind"] == "keyword"
    assert model_kw["platform"] is None

    # 二级缓存键严格限制 11 位核心口令，防止 /88VIP1234/ 等短串发生键碰撞
    assert DealPoolCleaner._build_anchor_cache_keys("taobao", "/88VIP1234/") == ["taobao:/88VIP1234/"]
    assert "taobao:tk:pir46VYeOmG" in DealPoolCleaner._build_anchor_cache_keys("taobao", "77 CZ057 2:/🗝pir46VYeOmG💲")

    # 清洗关键词时能干净剥离 AC01 / CZ00 与斜杠口令
    kw = extract_search_keyword("8(abc12345678) AC01 CZ00 维达超韧抽纸24包整箱")
    assert "abc12345678" not in kw
    assert "AC01" not in kw
    assert "CZ00" not in kw
    assert "维达超韧抽纸" in kw


# ==============================================================================
# 4. 万能转链 -> 详情页转链彻底杜绝幽灵 null 商品落库
# ==============================================================================
def test_universal_convert_then_detail_convert_never_inserts_null_ghost_product():
    DETAIL_CACHE.clear()
    MATERIAL_CACHE.clear()
    PROMOTION_CACHE.clear()
    SIGN_BY_ID_CACHE.clear()

    mock_col = MagicMock()
    # 模拟该随机商品尚未入正式 products 库（find_one 返回 None）
    mock_col.find_one.return_value = None
    mock_col.update_one.return_value = SimpleNamespace(acknowledged=True, matched_count=0, modified_count=0, upserted_id=None)
    mock_db = MagicMock()
    mock_db.get_collection.return_value = mock_col

    repo = ProductRepository(db_instance=mock_db)
    repo._indexes_ready = True
    svc = PromoteService(product_repo=repo)

    pdd_sign = "E9j2bxjdkWJgMvVRwf_Bar9wkdqmMC2aqg_JcTqWynsx"
    fake_adapter = MagicMock()
    fake_adapter.active = True
    fake_adapter.platform_code = "pdd"
    fake_adapter.platform_name = "拼多多"
    fake_adapter.capabilities = {"detail": True, "promotion": True}
    fake_adapter.requires_authorized_promotion = False
    fake_adapter.requires_numeric_promoter_id = False
    fake_adapter.requires_material_url = False
    from adapters.pdd_adapter import PddAdapter

    real_pdd = PddAdapter()
    fake_adapter.resolve_product_ref.side_effect = real_pdd.resolve_product_ref
    fake_adapter.build_command_text.side_effect = real_pdd.build_command_text
    fake_adapter.resolve_and_convert_universal.return_value = {
        "status": "success",
        "url": "https://p.pinduoduo.com/u_123",
        "final_link": "https://p.pinduoduo.com/u_123",
        "short_url": "https://p.pinduoduo.com/u_123",
        "goods_id": "6655443322",
        "goods_sign": pdd_sign,
        "title": "农夫山泉茶π果味茶饮料",
        "price": 29.9,
        "product": {
            "platform": "pdd",
            "goods_id": "6655443322",
            "goods_sign": pdd_sign,
            "name": "农夫山泉茶π果味茶饮料",
            "image_url": "https://img.pddpic.com/1.jpg",
            "price": 29.9,
            "original_price": 39.9,
        },
    }
    fake_adapter.convert_promotion_url.return_value = {
        "status": "success",
        "url": "https://p.pinduoduo.com/c_456",
        "final_link": "https://p.pinduoduo.com/c_456",
        "short_url": "https://p.pinduoduo.com/c_456",
        "attribution_status": "anonymous",
    }

    with patch("services.promote_service.get_adapter", return_value=fake_adapter):
        u_res = svc.universal_convert(f"https://mobile.yangkeduo.com/goods.html?goods_sign={pdd_sign}", platform="pdd")
        assert u_res["status"] == "success"

        # 模拟详情页仅携带 goods_sign 发起点击转链：_resolve_product_ref 从 DETAIL_CACHE 补回真实数字 goods_id，且 save_promotion_link_cache 使用 upsert=False
        c_res = svc.convert_link(platform="pdd", goods_id_or_sign=pdd_sign, goods_sign=pdd_sign)
        assert c_res["status"] == "success"

    assert fake_adapter.convert_promotion_url.call_args.kwargs.get("goods_id") == "6655443322"
    assert mock_col.update_one.called
    for call in mock_col.update_one.call_args_list:
        assert call.kwargs.get("upsert") is False
        flt = call.args[0]
        assert flt.get("product_id") == "6655443322" or {"product_id": "6655443322"} in (flt.get("$or") or [])
        assert call.args[1]["$set"]["product_id"] == "6655443322"


# ==============================================================================
# 5. 有界缓存、有界 JSONL 轮转、Windows 日志跨午夜滚动与闲鱼连接释放
# ==============================================================================
def test_bounded_anchor_cache_lru_and_bounded_jsonl_rotation():
    from repositories.deal_raw_pool_repo import _append_bounded_jsonl_lines
    from services.deal_pool_cleaner import DealPoolCleaner

    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner.max_anchor_cache_size = 5
    for i in range(12):
        cleaner._set_anchor_cache_locked(f"jd:key_{i}", {"convert_status": "product", "idx": i})
    assert len(cleaner.anchor_cache) == 5
    assert "jd:key_0" not in cleaner.anchor_cache
    assert "jd:key_11" in cleaner.anchor_cache

    slim = cleaner._slim_adapter_res_for_cache(
        {
            "status": "success",
            "price": 9.9,
            "product": {"goods_id": "1", "name": "抽纸", "price": 9.9, "raw_item": {"huge": "x" * 5000}},
            "extra_huge_blob": "y" * 5000,
        },
        keep_product=True,
    )
    assert "extra_huge_blob" not in slim
    assert "raw_item" not in slim["product"]

    with tempfile.TemporaryDirectory() as tmpdir:
        jpath = os.path.join(tmpdir, "history.jsonl")
        for i in range(30):
            _append_bounded_jsonl_lines(jpath, [json.dumps({"seq": i, "pad": "a" * 80})], max_bytes=600, backup_count=2)
        assert os.path.exists(jpath)
        assert os.path.getsize(jpath) <= 750
        assert os.path.exists(f"{jpath}.1")


def test_ztk_order_query_recognizes_relay_status_throttle_and_pace_backlog():
    from infrastructure.api_governance import QuotaPaceBacklogError
    from infrastructure.ztk_client import ZtkClient

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"status": 429, "content": "请求太频繁"}
    fake_http = SimpleNamespace(get=MagicMock(return_value=fake_resp))

    client = ZtkClient("test_key", "test_sid", http_session=fake_http)
    res = client.query_orders(0, 600)
    assert res["status"] == "error"
    assert res["error_code"] == "UPSTREAM_ERROR"
    assert "限流" in res["error_msg"] or "繁忙" in res["error_msg"]

    with patch("infrastructure.ztk_client.GLOBAL_QUOTA_PACER.pace", side_effect=QuotaPaceBacklogError("backlog")):
        res_backlog = client.query_orders(0, 600)
    assert res_backlog["status"] == "error"
    assert res_backlog["error_code"] == "QUOTA_PACE_BACKLOG"


def test_upstream_health_report_skips_stale_snapshots_from_aggregation():
    from pathlib import Path
    from infrastructure import api_governance as gov

    reg = gov.UpstreamHealthRegistry()
    reg.record_call("jd", "ztk_jd", "api_detail", outcome="success", latency_ms=50.0, allow_persist=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        stale_snap = {
            "role": "old_dead_worker",
            "pid": 99999,
            "updated_ts": time.time() - 900.0,
            "endpoints": [
                {
                    "platform": "jd",
                    "provider": "ztk_jd",
                    "operation": "api_detail",
                    "total_calls": 99,
                    "success_calls": 0,
                    "rate_limit_calls": 99,
                    "degraded_calls": 0,
                    "error_calls": 0,
                    "avg_latency_ms": 500.0,
                    "p95_latency_ms": 500.0,
                    "max_latency_ms": 500.0,
                }
            ],
        }
        (tmp_path / "upstream_health_old_dead_worker.json").write_text(json.dumps(stale_snap), encoding="utf-8")
        with patch.object(gov, "_HEALTH_DIR", tmp_path):
            report = reg.get_combined_health_report()
    ep = report["aggregated_endpoints"][0]
    assert ep["total_calls"] == 1
    assert ep["rate_limit_calls"] == 0
    assert report["alert_summary"] == []


def test_xianyu_round_pool_search_then_h5_never_self_deadlocks_and_ai_timeout_clean():
    import logging
    from infrastructure.xianyu import session as pxy
    from services.deal_pool_cleaner import DealPoolCleaner

    # 1) 真实调用 _acquire_serial_browser_lock（仅桩掉 Playwright），验证同轮先 search_session 再 h5_detail_session 绝不自死锁
    class _FakePage:
        url = "https://www.goofish.com"
        frames = []

        def on(self, *_a, **_k):
            pass

        def remove_listener(self, *_a, **_k):
            pass

        def goto(self, *_a, **_k):
            pass

        def wait_for_timeout(self, *_a, **_k):
            pass

        def close(self):
            pass

    class _FakeCtx:
        def __init__(self):
            self.pages = [_FakePage()]

        def set_default_timeout(self, *_a):
            pass

        def set_default_navigation_timeout(self, *_a):
            pass

        def on(self, *_a, **_k):
            pass

        def remove_listener(self, *_a, **_k):
            pass

        def new_page(self):
            p = _FakePage()
            self.pages.append(p)
            return p

        def cookies(self):
            return [{"name": "_m_h5_tk", "value": "tok_1"}]

        def close(self):
            pass

    class _FakeBrowser:
        def is_connected(self):
            return True

        def new_context(self, **_k):
            return _FakeCtx()

        def close(self):
            pass

    class _FakePw:
        chromium = SimpleNamespace(
            launch_persistent_context=lambda **_k: _FakeCtx(),
            launch=lambda **_k: _FakeBrowser(),
        )

    class _FakePwCm:
        def __enter__(self):
            return _FakePw()

        def __exit__(self, *_a):
            return False

    with tempfile.TemporaryDirectory() as tmpdir:
        lock_file = os.path.join(tmpdir, "xianyu_test.lock")
        dir_search = os.path.join(tmpdir, "search_acc")
        dir_guest = os.path.join(tmpdir, "guest_acc")
        with patch.object(pxy, "_GLOBAL_LOCK_FILE_PATH", lock_file), patch.object(
            pxy, "sync_playwright", return_value=_FakePwCm()
        ):
            with pxy.XianyuRoundBrowserPool() as pool:
                with pool.acquire_search_session(user_data_dir=dir_search, lock_timeout_sec=2.0) as (_c1, p1, _t1):
                    assert p1 is not None
                with pool.acquire_h5_detail_session(lock_key_dir=dir_guest, lock_timeout_sec=2.0) as (_c2, p2, _t2):
                    assert p2 is not None
            # 退出后全局线程锁必须已完全释放
            assert pxy._PROCESS_THREAD_LOCK.acquire(timeout=0.5)
            pxy._PROCESS_THREAD_LOCK.release()

    # 2) _call_ai_json_array 遇到 timed_out=True 时不抛 UnboundLocalError
    mock_ai = MagicMock(return_value={"status": "❌ 失败", "timed_out": True, "error_history": ["timeout"]})
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1, ai_caller=mock_ai)
    logged_warnings = []
    with patch.object(logging.getLogger("deal_pool_scheduler"), "warning", side_effect=lambda msg, *args: logged_warnings.append(msg % args if args else msg)):
        out = cleaner._call_ai_json_array("prompt", [{"id": 1}], "AI-Stage2")
    assert out == []
    assert mock_ai.call_count == 1
    assert all("resp_text" not in w for w in logged_warnings)


def test_simplified_robustness_fail_closed_taobao_share_code_prefetch_and_order_cooldown():
    from adapters.jd_adapter import JdAdapter
    from adapters.tb_adapter import TaobaoAdapter
    from infrastructure.jd_client import JdClient
    from infrastructure.ztk_client import ZtkClient
    from models.product import ProductRef, classify_user_input
    from services.detail_prefetch import DetailPrefetcher, RATE_LIMIT_COOLDOWN_SECONDS
    from services.order_service import OrderService
    from tests.test_detail_prefetch import Clock, MemoryService, idle, product

    # 1. Standalone share-code / hardware model keywords are NOT classified as links/tokens
    for kw in ("CZ1234", "(iPhone15Pro)", "!RTX4060!"):
        c = classify_user_input(kw)
        assert c["kind"] == "keyword"
        assert c["has_link_or_token"] is False
        assert c["keyword"] == kw

    # 2. TaobaoAdapter Fail-Closed: empty tao_id on m.tb.cn shortlink -> degraded_product (NEVER coupon_or_venue);
    #    empty tao_id on explicit official pages.tmall.com/wow/ venue -> coupon_or_venue
    tb_http = SimpleNamespace(get=lambda url, **kw: SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {
            "status": 200,
            "content": [{
                "tao_id": "",
                "title": "",
                "shorturl": "https://s.click.taobao.com/tbShort1",
                "coupon_click_url": "https://uland.taobao.com/coupon/edetail?e=test",
                "tkl": "￥tbTok123456￥",
            }],
        },
    ))
    ztk = ZtkClient("tk1", "sid1", http_session=tb_http, appkey_list=("tk1",))
    tb = TaobaoAdapter(official_client=MagicMock(), hdk_client=MagicMock(), ztk_client=ztk, tb_channel_pid="mm_1_2_3", taobao_enabled=True)
    deg = tb.resolve_and_convert_universal("https://m.tb.cn/h.throttleCase")
    assert deg["status"] == "success" and deg["resolution_kind"] == "degraded_product"
    assert deg.get("detail_lookup_failed") is True
    venue = tb.resolve_and_convert_universal("https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479")
    assert venue["status"] == "success" and venue["resolution_kind"] == "coupon_or_venue"

    # 3. JD ZTK items opt out of blind background prefetch, and DetailPrefetcher backs off 600s on RATE_LIMIT
    jd = JdAdapter(client=JdClient(appkey_list=("jk1",), union_id="10001", http_session=MagicMock()), jd_enabled=True, jd_position_id="10001")
    assert jd.can_prefetch_detail(ProductRef("jd", "img_1", "ENC_1234567890123456789012345678901234567890")) is False

    clock = Clock()
    svc_rl = MemoryService(lambda ref, **kw: __import__("models.product", fromlist=["DetailPrefetchResult"]).DetailPrefetchResult("error", error_code="RATE_LIMIT"))
    worker = DetailPrefetcher(svc_rl, wall_clock=clock, retry_backoff_seconds=0)
    worker.start()
    try:
        assert worker.offer_products([product("rl_1")]) == 1
        idle(worker)
        clock.value += RATE_LIMIT_COOLDOWN_SECONDS - 1
        assert worker.offer_products([product("rl_1")]) == 0
        clock.value += 1
        assert worker.offer_products([product("rl_1")]) == 1
        idle(worker)
    finally:
        worker.stop()

    # 4. OrderService surfaces real cooldown seconds remaining while keeping 180s interval locked
    o_repo = MagicMock()
    o_repo.query_user_orders.return_value = {"status": "success", "orders": [], "summary": {}}
    osvc = OrderService(order_repo=o_repo, sync_repo=MagicMock(get_state=MagicMock(return_value=None)))
    osvc._last_sync_attempt["pdd"] = time.time() - 40
    osvc._sync_state["pdd"] = {"status": "success", "error": "", "synced_count": 1}
    with patch("services.order_service.require_adapter", return_value=SimpleNamespace(active=True, platform_name="拼多多", capabilities={"orders": True})), \
         patch("services.order_service.get_adapter", return_value=SimpleNamespace(active=True, platform_name="拼多多", capabilities={"orders": True})):
        ores = osvc.sync_and_query_orders("pdd", "u_cool_test")
    assert ores["status"] == "success"
    assert 130 <= ores["data"]["sync_cooldown_remaining"] <= 145


def test_stage3_jd_lane_count_scales_with_ztk_keys():
    """验证 Stage 3 京东车道并发上限受 ZTK appkey 数量约束，防止 20 线程自伤踩踏，同时不挤占淘/拼并发。"""
    import threading
    from services.deal_pool_cleaner import DealPoolCleaner

    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=20)
    cleaner.adapters["jd"] = SimpleNamespace(client=SimpleNamespace(appkey_list=("k1", "k2", "k3")))

    lock = threading.Lock()
    active_jd, max_jd = 0, 0
    active_other, max_other = 0, 0

    def _fake_convert(tgt, default_plat="unknown"):
        nonlocal active_jd, max_jd, active_other, max_other
        plat = tgt.get("platform") or default_plat
        if plat == "jd":
            with lock:
                active_jd += 1
                max_jd = max(max_jd, active_jd)
            time.sleep(0.03)
            with lock:
                active_jd -= 1
        else:
            with lock:
                active_other += 1
                max_other = max(max_other, active_other)
            time.sleep(0.03)
            with lock:
                active_other -= 1
        return {"convert_status": "product", "platform": plat, "error_code": "OK", "error_msg": ""}

    cleaner.convert_single_target = _fake_convert
    mixed_targets = [
        ({"link_id": f"J{i}", "clean_input": f"https://u.jd.com/k{i}", "platform": "jd"}, "jd")
        for i in range(12)
    ] + [
        ({"link_id": f"T{i}", "clean_input": f"https://m.tb.cn/h.{i}", "platform": "taobao"}, "taobao")
        for i in range(6)
    ]
    cleaner.run_stage3_concurrent_conversions(mixed_targets, quiet_sub_logs=True)
    assert 1 <= max_jd <= 3
    assert max_other >= 4

    max_jd = 0
    pure_jd = [
        ({"link_id": f"P{i}", "clean_input": f"https://u.jd.com/p{i}", "platform": "jd"}, "jd")
        for i in range(9)
    ]
    cleaner.run_stage3_concurrent_conversions(pure_jd, quiet_sub_logs=True)
    assert 1 <= max_jd <= 3


def test_universal_convert_with_logged_in_uid_seeds_public_detail_cache(monkeypatch):
    from cache import clear_all_caches, get_cached_product_detail
    from models.product import ProductRef
    from services.promote_service import PromoteService

    clear_all_caches()
    product_card = {
        "platform": "taobao",
        "goods_id": "66778899",
        "goods_sign": "sign_tb_66778899",
        "name": "维达超韧抽纸",
        "price": 29.9,
        "original_price": 39.9,
        "image_url": "https://img.alicdn.com/test.jpg",
        "carousel_images": ["https://img.alicdn.com/test.jpg"],
    }
    fake_adapter = SimpleNamespace(
        active=True,
        platform_name="淘宝",
        requires_authorized_promotion=False,
        requires_numeric_promoter_id=False,
        resolve_and_convert_universal=lambda *a, **k: {
            "status": "success",
            "final_link": "https://s.click.taobao.com/demo",
            "goods_id": "66778899",
            "goods_sign": "sign_tb_66778899",
            "title": "维达超韧抽纸",
            "price": 29.9,
            "coupon_amount": 0.0,
            "product": product_card,
        },
        build_command_text=lambda *a, **kw: "口令",
    )
    monkeypatch.setattr("services.promote_service.get_adapter", lambda p: fake_adapter)
    svc = PromoteService(product_repo=MagicMock(), user_service=MagicMock())
    res = svc.universal_convert("https://m.tb.cn/h.demo123", platform="taobao", uid="u_1234abcd")
    assert res["status"] == "success"
    cached_public = get_cached_product_detail(
        ProductRef("taobao", "66778899", "sign_tb_66778899"),
        uid=None,
    )
    assert cached_public is not None
    assert cached_public[1]["data"]["name"] == "维达超韧抽纸"


# ==============================================================================
# 6. Wave A：防抖动误杀下架、有界熔断冷却、PDD Sign 缓存升级与解包会话复用
# ==============================================================================
def test_retry_on_off_promotion_does_not_confirm_off_on_network_retry():
    from adapters.base import BasePlatformAdapter

    class _DummyAdapter(BasePlatformAdapter):
        platform_code = "demo"
        platform_name = "演示"
        def get_recommend_goods(self, *a, **k): return {}
        def search_goods(self, *a, **k): return {}
        def get_goods_detail(self, *a, **k): return {}
        def convert_promotion_url(self, *a, **k): return {}
        def check_authority(self, *a, **k): return {}
        def generate_authority_url(self, *a, **k): return {}
        def fetch_increment_orders(self, *a, **k): return {}

    adp = _DummyAdapter()
    is_off = lambda r: r.get("error_code") == "PRODUCT_OFF_PROMOTION"

    # 第一次疑似下架，第二次变成网络抖动 -> 绝不可 confirmed_off=True
    calls = [
        {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION"},
        {"status": "error", "error_code": "NETWORK_ERROR"},
    ]
    res1, off1 = adp._retry_on_off_promotion(lambda: calls.pop(0), is_off, delay=0)
    assert off1 is False
    assert res1["error_code"] == "NETWORK_ERROR"

    # 两次均确认下架 -> confirmed_off=True
    calls2 = [
        {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION"},
        {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION"},
    ]
    res2, off2 = adp._retry_on_off_promotion(lambda: calls2.pop(0), is_off, delay=0)
    assert off2 is True
    assert res2["error_code"] == "PRODUCT_OFF_PROMOTION"


def test_upstream_cooldown_registry_bounded_and_mock_safe(monkeypatch):
    from infrastructure.api_governance import UpstreamCooldownRegistry

    clock = {"now": 100.0}
    monkeypatch.setattr("infrastructure.api_governance.time.monotonic", lambda: clock["now"])
    reg = UpstreamCooldownRegistry(max_entries=16)

    mock_sess = MagicMock()
    reg.mark_cooling("jd", "hdk", "jd_goods_search", 300.0, http_session=mock_sess)
    assert reg.is_cooling("jd", "hdk", "jd_goods_search", http_session=mock_sess) is False

    # 强制或无 mock_sess 标记后，冷却期间跳过，到期自动恢复
    reg.mark_cooling("jd", "hdk", "jd_goods_search", 60.0, force=True)
    assert reg.is_cooling("jd", "hdk", "jd_goods_search") is True
    assert reg.remaining_seconds("jd", "hdk", "jd_goods_search") == pytest.approx(60.0)
    clock["now"] += 61.0
    assert reg.is_cooling("jd", "hdk", "jd_goods_search") is False

    for i in range(25):
        reg.mark_cooling("jd", "hdk", f"op_{i}", 60.0, force=True)
    assert len(reg._entries) <= 16


def test_pdd_sign_cache_upgrade_and_non_fallback_errors_and_unwrap_session():
    from models.product import ProductRef
    from services.deal_pool_cleaner import DealPoolCleaner
    from services.product_service import ProductService

    SIGN_BY_ID_CACHE.clear()
    SIGN_BY_ID_CACHE.set(("pdd", "833182837312"), "E9X2Y5ff1NRgMvVRwfDBZIMCUINV0l6ycw_JJxlgUX5h")

    upgraded = ProductService._upgrade_ref_sign(ProductRef("pdd", "833182837312", ""))
    assert upgraded.goods_sign == "E9X2Y5ff1NRgMvVRwfDBZIMCUINV0l6ycw_JJxlgUX5h"

    mock_repo = MagicMock()
    mock_repo.get_product_identity.return_value = None
    svc = PromoteService(product_repo=mock_repo, user_service=MagicMock())
    resolved = svc._resolve_product_ref(ProductRef("pdd", "833182837312", ""))
    assert resolved.goods_sign == "E9X2Y5ff1NRgMvVRwfDBZIMCUINV0l6ycw_JJxlgUX5h"
    assert "UNSUPPORTED_TB_NUMERIC_ID" in PromoteService._NON_FALLBACK_PRODUCT_ERRORS
    assert "INVALID_PDD_URL" in PromoteService._NON_FALLBACK_PRODUCT_ERRORS
    assert "INVALID_TKL" in PromoteService._NON_FALLBACK_PRODUCT_ERRORS

    from infrastructure.transport import _TRACKED_SESSIONS

    cleaner = DealPoolCleaner(ai_batch_size=1, ai_max_workers=1, convert_max_workers=1)
    try:
        sess = cleaner._get_unwrap_session()
        assert sess in _TRACKED_SESSIONS
    finally:
        cleaner.close()






