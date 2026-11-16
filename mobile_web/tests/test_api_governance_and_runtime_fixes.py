# -*- coding: utf-8 -*-
"""
系统级运行隐患修复与上游商品/转链 API 统一治理层回归测试套件。
覆盖 6 大线上真实运行隐患与 `infrastructure/api_governance.py` 统一治理契约：
  1. 统一 API 治理层（前台/后台自适应配额节流、多通道故障转移链、四态结果契约、健康监控端点）；
  2. 京东 u.jd.com 单品限流防误判 OK_COUPON 与预热缓存防毒化、消除冗余 piliang 二次消耗；
  3. 淘宝全变体口令（括号/斜杠/书名号/AC01/CZ00）跨模块平台识别对齐；
  4. 万能转链 -> 详情页转链防幽灵空商品（DETAIL_CACHE 身份补齐 + only_if_exists=True 禁硬插空壳）；
  5. 内存/句柄/磁盘防泄漏（有界 LRU anchor_cache、有界 JSONL 轮转、Windows model_api.log 跨午夜安全滚动、闲鱼 db.close 与惰性连接池）；
  6. 后台订单主动轮询守护（OrderSyncWorker）、超72h过期好价摘标及云端墓碑删除/未清洗流商品回拉闭环。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading
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
from services.order_service import OrderSyncWorker
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
        single_item_evidence=True,
    )
    assert r_prod["resolution_kind"] == ResolutionKind.PRODUCT
    assert "detail_lookup_failed" not in r_prod

    # 2) 转链成功但缺商品卡片，且具备单品证据（如解包出加密 itemId）-> 强制标记 degraded_product + detail_lookup_failed=True
    r_deg = stamp_universal_resolution(
        {"status": "success", "final_link": "https://u.jd.com/new2", "product": None},
        single_item_evidence=True,
    )
    assert r_deg["resolution_kind"] == ResolutionKind.DEGRADED_PRODUCT
    assert r_deg["detail_lookup_failed"] is True

    # 3) 真正的纯领券/活动会场链接（无单品证据、无限流失败）-> coupon_or_venue
    r_cpn = stamp_universal_resolution(
        {"status": "success", "final_link": "https://u.jd.com/venue1", "product": None},
        single_item_evidence=False,
    )
    assert r_cpn["resolution_kind"] == ResolutionKind.COUPON_OR_VENUE
    assert not r_cpn.get("detail_lookup_failed")


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
                # 模拟首个轮询命中的 ztk_k2 频控返回空 tao_id，自动故障切换至 ztk_k1 返回真实加密 tao_id 与商品详情
                if params.get("appkey") == "ztk_k2":
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
    assert piliang_keys == ["ztk_k2", "ztk_k1"]

    # 2) 纯京口令（无 URL）：byunionid_get 转出 shortURL 后，向 piliang 透传该 shortURL 而非裸口令（防 301 转链失败）
    calls.clear()
    res_jcmd = adapter.resolve_and_convert_universal("11:/越峰花茉莉飘雪花茶！AC8yV1DP031LLfr6！ CA1393", promoter_id="10001")
    assert res_jcmd["status"] == "success"
    assert res_jcmd["resolution_kind"] == "product"
    piliang_tkls = [mat for u, _, mat in calls if "piliang" in u]
    assert piliang_tkls and all(t == "https://u.jd.com/officialShort1" for t in piliang_tkls)

    # 3) 纯领券短链 (jCommand 为空)：不白耗 piliang 与 quanwang，精准归为 coupon_or_venue
    calls.clear()
    res_cpn = adapter.resolve_and_convert_universal("https://u.jd.com/oa0INNe", promoter_id="10001")
    assert res_cpn["status"] == "success"
    assert res_cpn["resolution_kind"] == "coupon_or_venue"
    assert all("piliang" not in u and "quanwang" not in u for u, _, _ in calls)

    # 4) 若全部 Key 同时限流导致无商品卡：即使 jCommand 不含“抢购”，也精准标记 degraded_product，且不再重复浪费 quanwang 配额
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
    assert "越峰花茉莉飘雪花茶" in res_deg["title"]
    assert all("quanwang" not in u for u, _, _ in calls)


def test_deal_cleaner_never_poisons_degraded_jd_shortlink_as_ok_coupon():
    from mobile_web.scripts.run_deal_pool_scheduler import (
        ERR_ADAPTER_CONVERT_FAILED,
        DealPoolCleaner,
    )

    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    degraded_res = stamp_universal_resolution(
        {
            "status": "success",
            "url": "https://u.jd.com/myStationNewLink",
            "final_link": "https://u.jd.com/myStationNewLink",
            "short_url": "https://u.jd.com/myStationNewLink",
            "product": None,
        },
        verified_coupon_or_venue=False,
    )
    cleaner.adapters["jd"] = SimpleNamespace(resolve_and_convert_universal=lambda _inp: degraded_res)

    out = cleaner.convert_single_target(
        {"link_id": "L1", "clean_input": "https://u.jd.com/o6P5O9L", "platform": "jd", "text_role_hint": "main_candidate"},
        default_plat="jd",
    )
    assert out["convert_status"] == "failed"
    assert out["error_code"] == ERR_ADAPTER_CONVERT_FAILED
    assert out["resolution_kind"] == "degraded_product"

    # 轮末驱逐暂态失败缓存后，anchor_cache 不残留中毒记录，保证下一轮重试能真正重调接口恢复
    cleaner._evict_transient_failed_cache()
    assert "jd:https://u.jd.com/o6P5O9L" not in cleaner.anchor_cache

    # 验证预热门禁：旧库中未标记 resolution_kind 的 u.jd.com 与 union-click.jd.com OK_COUPON 均拒绝预热
    class _WarmupCursor:
        def sort(self, *_a):
            return self

        def limit(self, *_a):
            return [
                {
                    "converted_links": [
                        {
                            "link_id": "L_OLD_POISON_U",
                            "clean_input": "https://u.jd.com/poisonedOld",
                            "convert_status": "coupon_or_activity",
                            "error_code": "OK_COUPON",
                            "platform": "jd",
                            "my_promo_link": "https://u.jd.com/myPoisonedOut",
                        },
                        {
                            "link_id": "L_OLD_POISON_UC",
                            "clean_input": "https://union-click.jd.com/jdc?e=&p=JF8B_OLD",
                            "convert_status": "coupon_or_activity",
                            "error_code": "OK_COUPON",
                            "platform": "jd",
                            "my_promo_link": "https://u.jd.com/myPoisonedOut2",
                        },
                        {
                            "link_id": "L_NEW_VENUE",
                            "clean_input": "https://u.jd.com/realVenue",
                            "convert_status": "coupon_or_activity",
                            "resolution_kind": "coupon_or_venue",
                            "error_code": "OK_COUPON",
                            "platform": "jd",
                            "my_promo_link": "https://u.jd.com/myRealVenueOut",
                        },
                    ]
                }
            ]

    raw_col = MagicMock()
    raw_col.find.return_value = _WarmupCursor()
    cleaner.warmup_anchor_cache_from_db(raw_col)
    assert "jd:https://u.jd.com/poisonedOld" not in cleaner.anchor_cache
    assert "jd:https://union-click.jd.com/jdc?e=&p=JF8B_OLD" not in cleaner.anchor_cache
    assert "jd:https://u.jd.com/realVenue" in cleaner.anchor_cache


# ==============================================================================
# 3. 淘宝全变体口令单一真源识别（含 Emoji 🗝/💲，且不误伤搜索框括号型号词与非11位缓存键）
# ==============================================================================
def test_taobao_token_variants_detected_even_when_default_plat_is_jd():
    from models.product import classify_user_input, extract_taobao_token_core
    from mobile_web.scripts.run_deal_pool_scheduler import DealPoolCleaner, detect_candidate_platform

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
    fake_adapter.promotion_scope = ""
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
    from mobile_web.scripts.run_deal_pool_scheduler import (
        DealPoolCleaner,
        _append_bounded_jsonl_lines,
    )

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


def test_windows_safe_timed_rotating_file_handler_and_per_daemon_log_isolation():
    from infrastructure.ai_api.model_api import (
        _WindowsSafeTimedRotatingFileHandler,
        _resolve_process_log_stem,
    )

    with patch("sys.argv", ["scripts/run_deal_pool_scheduler.py"]):
        assert _resolve_process_log_stem("model_api") == "model_api_deal_pool"
    with patch("sys.argv", ["scripts/run_xianyu_deal_discovery_scheduler.py"]):
        assert _resolve_process_log_stem("model_api") == "model_api_xianyu"
    with patch("sys.argv", ["run.py"]):
        assert _resolve_process_log_stem("model_api") == "model_api"

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "model_api_deal_pool.log")
        handler = _WindowsSafeTimedRotatingFileHandler(log_path, when="midnight", backupCount=2, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(message)s"))

        rec = logging.LogRecord("test", logging.INFO, __file__, 1, "hello log line", (), None)
        handler.emit(rec)
        handler.rolloverAt = int(time.time()) - 10
        handler.doRollover()
        rotated_files = [f for f in os.listdir(tmpdir) if f.startswith("model_api_deal_pool.log.")]
        assert len(rotated_files) == 1

        # 极端情况下若 rename 仍抛 PermissionError，静默推进 rolloverAt 绝不抛未捕获异常
        handler.emit(rec)
        handler.rolloverAt = int(time.time()) - 10
        with patch.object(handler, "rotate", side_effect=PermissionError(32, "被占用")):
            handler.doRollover()
        assert handler.rolloverAt > int(time.time())
        handler.close()


def test_xianyu_round_pool_lazy_init_and_discovery_cycle_closes_db():
    import sys
    import types

    try:
        import playwright.sync_api  # noqa: F401
    except ModuleNotFoundError:
        _pw_mod = types.ModuleType("playwright")
        _pw_sync = types.ModuleType("playwright.sync_api")
        _pw_sync.sync_playwright = MagicMock()
        sys.modules.setdefault("playwright", _pw_mod)
        sys.modules.setdefault("playwright.sync_api", _pw_sync)

    from mobile_web.scripts import publish_to_xianyu as pxy
    from mobile_web.scripts import run_xianyu_deal_discovery_scheduler as xy_sched

    # 惰性验证：未实际调用 acquire_* 时，__enter__/__exit__ 绝不启动 Playwright
    with patch.object(pxy, "sync_playwright", side_effect=AssertionError("should not start eagerly")):
        with pxy.XianyuRoundBrowserPool() as pool:
            assert pool._pw is None

    # 验证 run_discovery_cycle 无论候选为空还是正常结束，均在 finally 中调用 db.close()
    fake_db = MagicMock()
    fake_db.get_collection.return_value = MagicMock()
    with patch.object(xy_sched, "gen_db_object", return_value=fake_db), patch.object(
        xy_sched, "select_round_candidates_for_source", return_value=[]
    ):
        xy_sched.run_discovery_cycle(enable_official=True, enable_deal_tip=False)

    assert fake_db.close.call_count == 1


# ==============================================================================
# 6. 后台订单定时同步守护 + 墓碑跨云端删除/好价过期摘标/云端未清洗流回拉
# ==============================================================================
def test_order_sync_worker_runs_periodically_and_stops_cleanly():
    order_svc = MagicMock()
    called_evt = threading.Event()

    def _fake_round(stop_event=None):
        called_evt.set()
        return {"pdd": "done"}

    order_svc.run_scheduled_sync_round.side_effect = _fake_round
    worker = OrderSyncWorker(order_svc, interval_seconds=60.0, initial_delay_seconds=0.01)
    worker.start()
    try:
        assert called_evt.wait(timeout=2.0) is True
    finally:
        worker.stop()
    assert worker._thread is None
    assert order_svc.run_scheduled_sync_round.call_count >= 1


def test_cloud_sync_tombstones_deal_expiry_and_cloud_only_pullback():
    from mobile_web.scripts import run_product_cloud_sync_scheduler as sync_mod
    from tests.test_product_cloud_sync import _FakeSyncCollection

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    t_old = now - datetime.timedelta(hours=80)

    # 1) 本地已超 72h 摘除好价标 (is_deal_tip=False + expired_at)，云端仍为 is_deal_tip=True -> 自动同步摘标
    local_expired_deal = {
        "platform": "jd",
        "product_id": "jd_stale_deal_1",
        "name": "过期好价纸巾",
        "price": 29.9,
        "activity_price": 29.9,
        "updated_at": t_old,
        "deal_info": {
            "is_deal_tip": False,
            "expired_at": now,
            "updated_at": now,
            "deal_price": 9.9,
        },
    }
    cloud_active_deal = {
        "platform": "jd",
        "product_id": "jd_stale_deal_1",
        "name": "过期好价纸巾",
        "price": 29.9,
        "activity_price": 29.9,
        "updated_at": t_old,
        "deal_info": {
            "is_deal_tip": True,
            "updated_at": t_old,
            "deal_price": 9.9,
        },
    }
    l_fp = sync_mod.extract_sync_fingerprint(local_expired_deal)
    c_fp = sync_mod.extract_sync_fingerprint(cloud_active_deal)
    should_sync, reason = sync_mod.needs_cloud_merge(l_fp, c_fp)
    assert should_sync is True
    assert reason == "deal_info_expired"
    op = sync_mod.build_product_merge_operation(local_expired_deal, c_fp)
    assert op._doc["$set"]["deal_info"]["is_deal_tip"] is False

    # 2) 墓碑跨云端删除 + 云端独有未清洗推荐流商品回拉到本地（自动排除已墓碑删除商品）
    class _TombstoneAwareCloudCol(_FakeSyncCollection):
        def find(self, query: dict | None = None, projection: dict | None = None):
            q = query or {}
            if "$and" in q and isinstance(q["$and"][0], dict) and "updated_at" in q["$and"][0]:
                cutoff_ms = sync_mod.to_utc_millis(q["$and"][0]["updated_at"]["$gte"])
                out = []
                for doc in self.docs:
                    if not sync_mod.is_valid_syncable_product(doc):
                        continue
                    if doc.get("format_status") is not None:
                        continue
                    u_ms = sync_mod.to_utc_millis(doc.get("updated_at"))
                    if u_ms is None or u_ms < cutoff_ms:
                        continue
                    out.append(self._project(doc, projection))
                return out
            return super().find(query, projection)

        def delete_many(self, filt: dict):
            plat = filt["platform"]
            pids = set(filt["product_id"]["$in"])
            before = len(self.docs)
            self.docs = [d for d in self.docs if not (d.get("platform") == plat and d.get("product_id") in pids)]
            return SimpleNamespace(deleted_count=before - len(self.docs))

    tombstone_col = MagicMock()
    tombstone_col.find.return_value = [{"platform": "pdd", "product_id": "dead_off_promo_1", "deleted_at": now}]

    local_col = _FakeSyncCollection([])
    cloud_col = _TombstoneAwareCloudCol(
        [
            {
                "_id": "c_dead",
                "platform": "pdd",
                "product_id": "dead_off_promo_1",
                "name": "已下架死链商品",
                "price": 19.9,
                "activity_price": 19.9,
                "updated_at": now,
            },
            {
                "_id": "c_stream_unfmt",
                "platform": "pdd",
                "product_id": "cloud_stream_new_1",
                "goods_sign": "E9_STREAM_1",
                "name": "云端推荐流新商品待规格清洗",
                "price": 15.8,
                "activity_price": 15.8,
                "image_url": "https://img.pddpic.com/s1.jpg",
                "updated_at": now,
                "default_promotion": {"final_link": "https://p.pinduoduo.com/cloud_only"},
            },
        ]
    )

    rep = sync_mod.run_sync_round(
        local_col,
        cloud_col,
        window_days=3.0,
        tombstone_col=tombstone_col,
        pull_cloud_unformatted=True,
    )
    assert rep["cloud_deleted_tombstones"] == 1
    assert rep["local_pulled_cloud_unformatted"] == 1
    assert all(d["product_id"] != "dead_off_promo_1" for d in cloud_col.docs)
    assert len(local_col.docs) == 1
    assert local_col.docs[0]["product_id"] == "cloud_stream_new_1"
    assert "default_promotion" not in local_col.docs[0]


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
    assert res_backlog["error_code"] == "UPSTREAM_ERROR"


def test_deal_pool_startup_recovery_idempotently_rescues_recent_misclassified_tips():
    from mobile_web.scripts.run_deal_pool_scheduler import (
        ERR_JD_NUMERIC_SKU,
        DealPoolCleaner,
        DealRawPoolRepository,
    )

    fake_db = MagicMock()
    fake_col = MagicMock()
    fake_col.update_many.side_effect = [
        SimpleNamespace(modified_count=7),
        SimpleNamespace(modified_count=3),
    ]
    fake_db.get_collection.return_value = fake_col

    repo = DealRawPoolRepository(db_instance=fake_db)
    fake_col.update_many.reset_mock()
    fake_col.update_many.side_effect = [
        SimpleNamespace(modified_count=7),
        SimpleNamespace(modified_count=3),
    ]
    out = repo.recover_recent_misclassified_tips(lookback_hours=48.0)
    assert out == {"jd_recovered": 7, "tb_recovered": 3}
    assert fake_col.update_many.call_count == 2
    jd_flt, jd_upd = fake_col.update_many.call_args_list[0].args
    assert ERR_JD_NUMERIC_SKU in jd_flt["clean_error_code"]["$in"]
    assert any("union-click" in str(cond) for cond in jd_flt["$or"])
    for call in fake_col.update_many.call_args_list:
        flt, upd = call.args
        assert flt["recovery_version"] == {"$ne": DealRawPoolRepository.RECOVERY_VERSION_TAG}
        assert upd["$set"]["clean_status"] == "pending"
        assert upd["$set"]["recovery_version"] == DealRawPoolRepository.RECOVERY_VERSION_TAG
        assert "ai_stage1_result" in upd["$unset"]

    # 验证同帖同时含 item.jd.com/{数字} 与 extracted_links(union-click.jd.com) 时，若 AI-1 漏提 union-click，由代码确定性补齐
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    doc = {
        "source_site": "guangdiu",
        "source_item_id": "gd_1",
        "platform": "jd",
        "title": "米多奇香米饼 9.9元",
        "tutorial_text": "https://item.jd.com/10171863267421.html",
        "extracted_links": [
            {"label": "直达购买", "url": "https://union-click.jd.com/jdc?e=&p=JF8B_VALID", "platform": "jd"}
        ],
    }
    s1_inp, tip_map = cleaner.build_stage1_task_input(doc, None)
    with patch.object(
        cleaner,
        "_call_ai_json_array",
        return_value=[
            {
                "main_tip_id": "gd_1",
                "is_standalone_deal": True,
                "merged_tip_ids": ["gd_1"],
                "extracted_targets": [
                    {
                        "link_id": "L1",
                        "raw_span": "https://item.jd.com/10171863267421.html",
                        "clean_input": "https://item.jd.com/10171863267421.html",
                        "platform": "jd",
                        "text_role_hint": "main_candidate",
                    }
                ],
                "strip_spans": [],
            }
        ],
    ):
        ai1_map = cleaner.run_ai_stage1_batch([s1_inp], {"gd_1": doc}, {"gd_1": tip_map}, quiet_sub_logs=True)
    extracted_urls = [t["clean_input"] for t in ai1_map["gd_1"]["extracted_targets"]]
    assert "https://union-click.jd.com/jdc?e=&p=JF8B_VALID" in extracted_urls


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


