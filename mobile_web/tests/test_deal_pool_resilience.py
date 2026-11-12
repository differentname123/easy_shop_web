"""
回归测试：好价线报清洗引擎抗抖动、折淘客双 Key 轮询与熔断、暂态转链失败自动重排与活人话术无损脱敏
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from mobile_web.infrastructure.ai_api.model_api import LOW_MODEL_LIST
from mobile_web.infrastructure.http_utils import error_dict, execute_with_key_failover
from mobile_web.scripts.run_deal_pool_scheduler import (
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_JD_NUMERIC_SKU,
    DealPoolCleaner,
)


def test_poison_model_gemini_3_5_flash_weight_is_zero():
    matched = [
        item for item in LOW_MODEL_LIST
        if item.get("model_name") == "gemini-aistudio-api-key-3.5-flash"
    ]
    assert matched, "LOW_MODEL_LIST should retain gemini-aistudio-api-key-3.5-flash entry"
    assert matched[0]["权重"] == 0


def test_jd_client_uses_healthy_round_robin_while_ztk_client_stays_sticky():
    seen_jd_keys = []
    jd_dummy = SimpleNamespace(appkey_list=("jd-key-1", "jd-key-2"), _active_key_index=0)
    for _ in range(4):
        res = execute_with_key_failover(
            jd_dummy,
            lambda k: seen_jd_keys.append(k) or {"status": "success", "key": k},
            round_robin=True,
        )
        assert res["status"] == "success"
    assert seen_jd_keys == ["jd-key-1", "jd-key-2", "jd-key-1", "jd-key-2"]

    seen_tb_keys = []
    tb_dummy = SimpleNamespace(appkey_list=("tb-key-1", "tb-key-2"), _active_key_index=0)
    for _ in range(3):
        res = execute_with_key_failover(
            tb_dummy,
            lambda k: seen_tb_keys.append(k) or {"status": "success", "key": k},
            round_robin=False,
        )
        assert res["status"] == "success"
    assert seen_tb_keys == ["tb-key-1", "tb-key-1", "tb-key-1"]


def test_round_robin_quarantines_failed_key_when_backup_key_rescues():
    calls = []
    client_dummy = SimpleNamespace(appkey_list=("key-bad", "key-good"), _active_key_index=0)

    def _call(key: str):
        calls.append(key)
        if key == "key-bad":
            return error_dict("UPSTREAM_ERROR", "rate_limit")
        return {"status": "success", "key": key}

    res1 = execute_with_key_failover(client_dummy, _call, round_robin=True)
    assert res1["status"] == "success"
    assert calls == ["key-bad", "key-good"]

    # Within 60s cooldown window, subsequent round-robin calls skip key-bad directly
    calls.clear()
    for _ in range(3):
        res = execute_with_key_failover(client_dummy, _call, round_robin=True)
        assert res["status"] == "success"
    assert calls == ["key-good", "key-good", "key-good"]


def test_stage3_transient_failure_schedules_retry_reuses_ai1_and_evicts_transient_cache():
    cleaner = DealPoolCleaner(ai_batch_size=10, ai_max_workers=2, convert_max_workers=2)
    cleaner.warmup_anchor_cache_from_db = lambda _col: None

    fake_jd_adapter = MagicMock()
    # Round 1: Zhetaoke JD transient 301 "无符合条件的数据"
    # Round 2: Promotion converted URL but detail lookup throttled (`detail_lookup_failed=True`)
    # Round 3: Full JD product resolved!
    fake_jd_adapter.resolve_and_convert_universal.side_effect = [
        {"status": "error", "error_code": "UPSTREAM_ERROR", "error_msg": "无符合条件的数据"},
        {
            "status": "success",
            "goods_id": "",
            "goods_sign": "",
            "product": None,
            "detail_lookup_failed": True,
            "final_link": "https://u.jd.com/our_promo",
            "url": "https://u.jd.com/our_promo",
        },
        {
            "status": "success",
            "goods_id": "100123456789",
            "goods_sign": "https://u.jd.com/converted_ok",
            "final_link": "https://u.jd.com/converted_ok",
            "url": "https://u.jd.com/converted_ok",
            "promotion_token": "",
            "product": {
                "platform": "jd",
                "product_id": "100123456789",
                "name": "维达抽纸整箱24包",
                "price": 24.9,
                "original_price": 39.9,
                "activity_price": 24.9,
                "coupon_amount": 15.0,
                "mall_name": "维达京东自营官方旗舰店",
                "image": "https://img14.360buyimg.com/n1/jfs/test.jpg",
            },
        },
    ]
    cleaner.adapters["jd"] = fake_jd_adapter

    ai1_call_count = {"n": 0}

    def _fake_stage1(stage1_inputs, docs_by_tip_id, neighbor_maps_by_id=None, total_pending_count=0, quiet_sub_logs=False):
        ai1_call_count["n"] += 1
        out = {}
        for inp in stage1_inputs:
            mid = inp["main_tip_id"]
            out[mid] = {
                "main_tip_id": mid,
                "is_standalone_deal": True,
                "merged_tip_ids": [mid],
                "combined_raw_text": "维达抽纸整箱24包速度冲 [#L1]",
                "extracted_images": [],
                "extracted_targets": [
                    {
                        "link_id": "L1",
                        "raw_span": "https://u.jd.com/o1s7UHc",
                        "clean_input": "https://u.jd.com/o1s7UHc",
                        "platform": "jd",
                        "text_role_hint": "main_candidate",
                    }
                ],
                "_from_fallback": False,
            }
        return out

    cleaner.run_ai_stage1_batch = _fake_stage1
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, total_pending_count=0, quiet_sub_logs=False: {
        inp["main_tip_id"]: {
            "main_tip_id": inp["main_tip_id"],
            "is_valid_deal": True,
            "main_link_id": "L1",
            "coupon_links": [],
            "filler_links": [],
            "deal_price": 24.9,
            "tutorial_steps": "1. 点击主商品下单实付24.9元",
            "quality_score": 86,
            "score_breakdown": {"price_advantage": 35, "brand_reputation": 25, "completeness": 26},
        }
        for inp in stage2_inputs
    }

    raw_col = MagicMock()
    raw_col.find.return_value = []

    doc1 = {
        "source_site": "coolapk",
        "source_channel": "看看买",
        "source_item_id": "tip_jd_1",
        "platform": "jd",
        "title": "维达抽纸整箱24包速度冲",
        "tutorial_text": "维达抽纸整箱24包速度冲 https://u.jd.com/o1s7UHc",
        "extracted_links": [{"url": "https://u.jd.com/o1s7UHc", "platform": "jd"}],
        "extracted_tokens": [],
        "clean_retry_count": 0,
    }

    # Attempt 1 -> transient failure -> retry_pending (retry_count=1), cache evicted
    res_round1 = cleaner.clean_batch_records([doc1], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert len(res_round1) == 1
    assert res_round1[0]["status"] == "retry_pending"
    assert res_round1[0]["error_code"] == ERR_ADAPTER_CONVERT_FAILED
    assert res_round1[0]["retry_count"] == 1
    assert ai1_call_count["n"] == 1
    assert cleaner.anchor_cache == {}

    # Attempt 2 -> reuse ai_stage1_result (no extra AI-1 call!), detail_lookup_failed -> retry_pending (retry_count=2)
    doc2 = dict(
        doc1,
        clean_retry_count=1,
        clean_error_code=ERR_ADAPTER_CONVERT_FAILED,
        ai_stage1_result=res_round1[0]["ai_stage1_result"],
    )
    res_round2 = cleaner.clean_batch_records([doc2], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert len(res_round2) == 1
    assert res_round2[0]["status"] == "retry_pending"
    assert res_round2[0]["error_code"] == ERR_ADAPTER_CONVERT_FAILED
    assert res_round2[0]["retry_count"] == 2
    assert ai1_call_count["n"] == 1  # AI-1 reused without re-calling LLM!
    assert cleaner.anchor_cache == {}

    # Attempt 3 -> upstream JD succeeds -> full product resolved!
    doc3 = dict(
        doc2,
        clean_retry_count=2,
        clean_error_code=ERR_ADAPTER_CONVERT_FAILED,
        ai_stage1_result=res_round2[0]["ai_stage1_result"],
    )
    res_round3 = cleaner.clean_batch_records([doc3], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert len(res_round3) == 1
    assert res_round3[0]["status"] == "success"
    assert res_round3[0]["product_id"] == "100123456789"
    assert ai1_call_count["n"] == 1  # Still only 1 AI-1 call total!


def test_deterministic_error_remains_cached_and_does_not_retry():
    cleaner = DealPoolCleaner(ai_batch_size=10, ai_max_workers=2, convert_max_workers=2)
    cleaner.warmup_anchor_cache_from_db = lambda _col: None
    fake_jd_adapter = MagicMock()
    fake_jd_adapter.resolve_and_convert_universal.return_value = {
        "status": "error",
        "error_code": "UNSUPPORTED_JD_NUMERIC_SKU",
        "error_msg": "京东升级不再支持纯数字SKU直接转链",
    }
    cleaner.adapters["jd"] = fake_jd_adapter
    cleaner.run_ai_stage1_batch = lambda stage1_inputs, *_a, **_kw: {
        inp["main_tip_id"]: {
            "main_tip_id": inp["main_tip_id"],
            "is_standalone_deal": True,
            "merged_tip_ids": [inp["main_tip_id"]],
            "combined_raw_text": "纯数字SKU [#L1]",
            "extracted_images": [],
            "extracted_targets": [
                {
                    "link_id": "L1",
                    "raw_span": "https://item.jd.com/100012345678.html",
                    "clean_input": "https://item.jd.com/100012345678.html",
                    "platform": "jd",
                    "text_role_hint": "main_candidate",
                }
            ],
            "_from_fallback": False,
        }
        for inp in stage1_inputs
    }
    raw_col = MagicMock()
    raw_col.find.return_value = []
    doc = {
        "source_site": "smzdm",
        "source_channel": "国内好价",
        "source_item_id": "tip_sku_1",
        "platform": "jd",
        "title": "纯数字SKU测试",
        "tutorial_text": "纯数字SKU https://item.jd.com/100012345678.html",
        "extracted_links": [{"url": "https://item.jd.com/100012345678.html", "platform": "jd"}],
        "extracted_tokens": [],
        "clean_retry_count": 0,
    }
    res = cleaner.clean_batch_records([doc], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert len(res) == 1
    assert res[0]["status"] == "failed"
    assert res[0]["error_code"] == ERR_JD_NUMERIC_SKU
    assert len(cleaner.anchor_cache) > 0


def test_clean_original_copywriting_strips_mp_urls_and_token_shells_without_touching_human_copy():
    doc = {
        "source_item_id": "tip_1",
        "title": "好价神车",
        "tutorial_text": (
            "兄弟们这波神车快冲！叠省省卡到手才9.9包邮！\n"
            "https://u.jd.com/o1s7UHc\n"
            "微信小程序 mp://kX9aBcDeFgHiJkL\n"
            "复制口令 /:/ 速度锁单手慢无！"
        ),
        "extracted_links": [{"url": "https://u.jd.com/o1s7UHc"}],
        "extracted_tokens": [],
    }
    cleaned = DealPoolCleaner._build_clean_original_copywriting(["tip_1"], {"tip_1": doc}, [], doc=doc)
    assert "mp://" not in cleaned
    assert "u.jd.com" not in cleaned
    assert "/:/" not in cleaned
    assert "兄弟们这波神车快冲！叠省省卡到手才9.9包邮！" in cleaned
    assert "速度锁单手慢无！" in cleaned


def test_four_key_pool_failover_and_cooldown_rotation():
    calls = []
    pool_dummy = SimpleNamespace(appkey_list=("k1", "k2", "k3", "k4"), _active_key_index=0)

    def _call(key: str):
        calls.append(key)
        if key in ("k1", "k2"):
            return error_dict("UPSTREAM_ERROR", "quota_exceeded")
        return {"status": "success", "key": key}

    res = execute_with_key_failover(pool_dummy, _call, round_robin=True)
    assert res == {"status": "success", "key": "k3"}
    assert calls == ["k1", "k2", "k3"]

    # k1 and k2 are quarantined for 60s; subsequent calls rotate cleanly across healthy k3/k4
    calls.clear()
    seen_healthy = [
        execute_with_key_failover(pool_dummy, _call, round_robin=True)["key"]
        for _ in range(4)
    ]
    assert set(seen_healthy) == {"k3", "k4"}
    assert "k1" not in calls and "k2" not in calls


def test_jd_search_goods_rescues_single_key_301_rate_limit_and_converges_on_genuine_empty():
    from infrastructure.jd_client import JdClient

    def _json_resp(payload):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        return resp

    # Case 1: Key 1 returns 301 "无符合条件的数据" due to 1000-call throttle, Key 2 returns real data
    called_keys = []

    def _get_rate_limited(url, params=None, **_kwargs):
        k = params["appkey"]
        called_keys.append(k)
        if k == "jd-k1":
            return _json_resp({"status": 301, "content": "无符合条件的数据"})
        return _json_resp({
            "status": 200,
            "content": [{"tao_id": "100999", "tao_title": "京东好价抽纸", "quanhou_jiage": "19.9"}],
        })

    http1 = SimpleNamespace(get=_get_rate_limited)
    jd1 = JdClient(union_id="10001", appkey_list=["jd-k1", "jd-k2", "jd-k3", "jd-k4"], http_session=http1)
    res1 = jd1.search_goods("维达抽纸", page=1, page_size=20)
    assert res1["status"] == "success"
    assert len(res1["data"]) == 1
    assert called_keys == ["jd-k1", "jd-k2"]
    assert 0 in jd1._key_cooldown_until  # jd-k1 quarantined

    # Case 2: Genuine empty search across 4 keys stops after 2 keys confirm 301 (does not waste k3/k4)
    empty_keys = []

    def _get_truly_empty(url, params=None, **_kwargs):
        empty_keys.append(params["appkey"])
        return _json_resp({"status": 301, "content": "无符合条件的数据"})

    http2 = SimpleNamespace(get=_get_truly_empty)
    jd2 = JdClient(union_id="10001", appkey_list=["jd-k1", "jd-k2", "jd-k3", "jd-k4"], http_session=http2)
    res2 = jd2.search_goods("不存在的冷门词xyz", page=1, page_size=20)
    assert res2 == {"status": "success", "data": [], "has_more": False, "total_count": 0}
    assert empty_keys == ["jd-k1", "jd-k2"]


def test_ztk_client_multi_account_sid_pairing_and_appkey_unauthorized_failover():
    from infrastructure.ztk_client import ZtkClient

    seen_pairs = []

    def _get(url, params=None, **_kwargs):
        seen_pairs.append((params["appkey"], params["sid"]))
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        if params["appkey"] == "ak-1":
            # Previously misclassified as user AUTH_REQUIRED when relation_id was present!
            resp.json.return_value = {"status": 400, "content": "appkey未授权或已到期"}
        else:
            resp.json.return_value = {
                "status": 200,
                "content": [{
                    "tao_id": "888777666",
                    "coupon_click_url": "https://uland.taobao.com/coupon/edetail?e=test",
                    "tkl": "￥AbCdEf123￥",
                }],
            }
        return resp

    client = ZtkClient(
        appkey_list=["ak-1", "ak-2", "ak-3"],
        sid_list=["sid-1", "sid-2", "sid-3"],
        http_session=SimpleNamespace(get=_get),
    )
    res = client.convert_channel_url(
        "https://item.taobao.com/item.htm?id=888777666",
        relation_id="2999888777",
        pid="mm_1_2_3",
    )
    assert res["status"] == "success"
    assert res["raw"]["tao_id"] == "888777666"
    assert res["tpwd"] == "￥AbCdEf123￥"
    assert seen_pairs == [("ak-1", "sid-1"), ("ak-2", "sid-2")]


def test_hdk_client_multi_key_pool_failover():
    from infrastructure.hdk_client import HdkClient

    seen_keys = []

    def _get(url, params=None, **_kwargs):
        key = params["apikey"]
        seen_keys.append(key)
        if key == "hdk-1":
            resp = MagicMock(status_code=200)
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"code": 0, "msg": "apikey请求超限"}
            return resp
        if key == "hdk-2":
            raise RuntimeError("transient socket timeout")
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"code": 1, "data": [{"itemid": "555666"}], "min_id": 0}
        return resp

    client = HdkClient(api_key_list=["hdk-1", "hdk-2", "hdk-3"], http_session=SimpleNamespace(get=_get))
    res = client.get_ranked_ids(1, limit=20)
    assert res["status"] == "success"
    assert res["item_ids"] == ["555666"]
    assert seen_keys == ["hdk-1", "hdk-2", "hdk-3"]
    assert client.api_key == "hdk-3"


def test_settings_parses_multi_key_pools_for_ztk_and_hdk(tmp_path):
    import json
    from infrastructure.settings import load_settings

    cfg_file = tmp_path / "config.test.json"
    cfg_file.write_text(
        json.dumps({
            "local_mongo_host": "127.0.0.1",
            "local_mongo_port": 27017,
            "local_mongo_db_name": "shop_test",
            "web_auth_secret": "x" * 48,
            "zhetaoke_appkey": "ztk-k1",
            "zhetaoke_appkey_list": ["ztk-k1", "ztk-k2", "ztk-k3", "ztk-k4"],
            "zhetaoke_sid": "sid-1",
            "zhetaoke_sid_list": ["sid-1", "sid-2", "sid-3", "sid-4"],
            "haodanku_api_key": "hdk-1",
            "haodanku_api_key_list": ["hdk-1", "hdk-2", "hdk-3"],
        }),
        encoding="utf-8",
    )
    loaded = load_settings(cfg_file, require_credentials=False)
    assert loaded.ztk_appkey_list == ("ztk-k1", "ztk-k2", "ztk-k3", "ztk-k4")
    assert loaded.ztk_sid_list == ("sid-1", "sid-2", "sid-3", "sid-4")
    assert loaded.hdk_api_key_list == ("hdk-1", "hdk-2", "hdk-3")


def test_select_models_excludes_zero_weight_models_from_primary_and_fallbacks():
    from mobile_web.infrastructure.ai_api.model_api import (
        LOW_MODEL_LIST,
        MEDIUM_MODEL_LIST,
        _select_models,
    )

    zero_low = {m["model_name"] for m in LOW_MODEL_LIST if m.get("权重", 0) <= 0}
    zero_med = {m["model_name"] for m in MEDIUM_MODEL_LIST if m.get("权重", 0) <= 0}
    assert "gemini-aistudio-api-key-3.5-flash" in zero_low

    for _ in range(100):
        chosen_low = _select_models(None, None, "low")
        assert chosen_low
        assert zero_low.isdisjoint(chosen_low)

        chosen_med = _select_models(None, None, "medium")
        assert chosen_med
        assert zero_med.isdisjoint(chosen_med)


def test_ai2_timeout_retry_reuses_valid_ai1_without_dropping_ai1_timeout_retries():
    from mobile_web.scripts.run_deal_pool_scheduler import ERR_AI_TIMEOUT_RETRY

    cleaner = DealPoolCleaner(ai_batch_size=6, ai_max_workers=2, convert_max_workers=2)
    cleaner.warmup_anchor_cache_from_db = lambda _col: None

    fake_jd_adapter = MagicMock()
    fake_jd_adapter.resolve_and_convert_universal.return_value = {
        "status": "success",
        "goods_id": "100999888777",
        "goods_sign": "https://u.jd.com/ok_prod",
        "final_link": "https://u.jd.com/ok_prod",
        "url": "https://u.jd.com/ok_prod",
        "promotion_token": "",
        "product": {
            "platform": "jd",
            "product_id": "100999888777",
            "name": "清风原木纯品抽纸24包",
            "price": 22.9,
            "original_price": 36.9,
            "activity_price": 22.9,
            "coupon_amount": 14.0,
            "mall_name": "清风京东自营旗舰店",
            "image": "https://img14.360buyimg.com/n1/jfs/qf.jpg",
        },
    }
    cleaner.adapters["jd"] = fake_jd_adapter

    ai1_calls = {"n": 0}
    ai2_attempt = {"n": 0}

    def _fake_stage1(stage1_inputs, docs_by_tip_id, neighbor_maps_by_id=None, total_pending_count=0, quiet_sub_logs=False):
        ai1_calls["n"] += 1
        return {
            inp["main_tip_id"]: {
                "main_tip_id": inp["main_tip_id"],
                "is_standalone_deal": True,
                "merged_tip_ids": [inp["main_tip_id"]],
                "combined_raw_text": "清风抽纸好价 [#L1]",
                "extracted_images": [],
                "extracted_targets": [
                    {
                        "link_id": "L1",
                        "raw_span": "https://u.jd.com/o1abcde",
                        "clean_input": "https://u.jd.com/o1abcde",
                        "platform": "jd",
                        "text_role_hint": "main_candidate",
                    }
                ],
                "_from_fallback": False,
            }
            for inp in stage1_inputs
        }

    def _fake_stage2(stage2_inputs, total_pending_count=0, quiet_sub_logs=False):
        ai2_attempt["n"] += 1
        if ai2_attempt["n"] == 1:
            return {}  # Simulate Stage 4 AI-2 timeout on Round 1
        return {
            inp["main_tip_id"]: {
                "main_tip_id": inp["main_tip_id"],
                "is_valid_deal": True,
                "main_link_id": "L1",
                "coupon_links": [],
                "filler_links": [],
                "deal_price": 22.9,
                "tutorial_steps": ["1. 点击 [#L1] 下单实付22.9元"],
                "quality_score": 85,
                "score_breakdown": {"price_advantage": 35, "brand_reputation": 25, "completeness": 25},
            }
            for inp in stage2_inputs
        }

    cleaner.run_ai_stage1_batch = _fake_stage1
    cleaner.run_ai_stage2_batch = _fake_stage2

    raw_col = MagicMock()
    raw_col.find.return_value = []
    base_doc = {
        "source_site": "guangdiu",
        "source_channel": "最新好价",
        "source_item_id": "gd_9001",
        "platform": "jd",
        "title": "清风抽纸24包22.9元",
        "tutorial_text": "清风抽纸好价 https://u.jd.com/o1abcde",
        "extracted_links": [{"url": "https://u.jd.com/o1abcde", "platform": "jd"}],
        "extracted_tokens": [],
        "clean_retry_count": 0,
    }

    # Round 1: AI-1 succeeds, AI-2 times out -> retry_pending with ERR_AI_TIMEOUT_RETRY
    r1 = cleaner.clean_batch_records([base_doc], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert r1[0]["status"] == "retry_pending"
    assert r1[0]["error_code"] == ERR_AI_TIMEOUT_RETRY
    assert ai1_calls["n"] == 1

    # Round 2: Retried after AI-2 timeout -> reuses saved_ai1 without re-calling AI-1!
    doc_r2 = dict(
        base_doc,
        clean_retry_count=1,
        clean_error_code=ERR_AI_TIMEOUT_RETRY,
        ai_stage1_result=r1[0]["ai_stage1_result"],
    )
    r2 = cleaner.clean_batch_records([doc_r2], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert r2[0]["status"] == "success"
    assert ai1_calls["n"] == 1

    # Contrast: If AI-1 itself timed out (_from_fallback=True), retry MUST still invoke AI-1!
    doc_ai1_fb = dict(
        base_doc,
        source_item_id="gd_9002",
        clean_retry_count=1,
        clean_error_code=ERR_AI_TIMEOUT_RETRY,
        ai_stage1_result={"main_tip_id": "gd_9002", "extracted_targets": [], "_from_fallback": True},
    )
    r3 = cleaner.clean_batch_records([doc_ai1_fb], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert r3[0]["status"] == "success"
    assert ai1_calls["n"] == 2


def test_jd_client_per_key_pacing_reserves_slots_per_appkey_and_skips_mocks(monkeypatch):
    import requests
    from infrastructure.jd_client import JdClient

    sleeps = []
    fake_clock = {"t": 1000.0}
    monkeypatch.setattr("infrastructure.jd_client.time.monotonic", lambda: fake_clock["t"])
    monkeypatch.setattr("infrastructure.jd_client.time.sleep", lambda s: sleeps.append(round(s, 3)))
    monkeypatch.setattr(JdClient, "_KEY_NEXT_ALLOWED_TS", {})

    # 1. Mocked http_session incurs zero pacing sleep
    mock_cli = JdClient(appkey_list=["k-mock"], union_id="2035679405", http_session=SimpleNamespace(get=lambda *a, **kw: None))
    mock_cli._pace_real_http_by_key({"params": {"appkey": "k-mock"}})
    assert sleeps == []
    assert JdClient._KEY_NEXT_ALLOWED_TS == {}

    # 2. Real requests.Session spaces same-key calls by 0.22s while allowing distinct keys immediately
    real_cli = JdClient(appkey_list=["k-real-1", "k-real-2"], union_id="2035679405", http_session=requests.Session())
    real_cli._pace_real_http_by_key({"params": {"appkey": "k-real-1"}})
    real_cli._pace_real_http_by_key({"params": {"appkey": "k-real-2"}})
    assert sleeps == []  # First call on each independent key does not wait on the other!

    real_cli._pace_real_http_by_key({"params": {"appkey": "k-real-1"}})
    assert sleeps == [0.22]  # Immediate second call on k-real-1 waits 0.22s


def test_antigravity_client_defaults_to_3_concurrency_per_account_6_across_2(tmp_path, monkeypatch):
    import os
    from infrastructure.ai_api import antigravity_client as ag

    monkeypatch.setattr(ag, "GLOBAL_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(
        ag,
        "sync_all_known_accounts_to_sandboxes",
        lambda overwrite_from_desktop=False: {
            "guardianriedel689@gmail.com": tmp_path / "sb1",
            "richard.nowak@usa.com": tmp_path / "sb2",
        },
    )

    assert ag.DEFAULT_MAX_CONCURRENCY_PER_ACCOUNT == 3
    pool = ag.AntigravityPoolClient(
        account_pool=["guardianriedel689@gmail.com", "richard.nowak@usa.com"]
    )
    assert pool.max_concurrency_per_account == 3

    workers = pool.get_eligible_workers()
    assert len(workers) == 2
    claimed = []
    try:
        for w in workers:
            assert w._max_concurrency == 3
            for _ in range(3):
                fd = w.try_claim_idle_slot()
                assert fd is not None
                claimed.append((w, fd))
            # 4th claim on same sub-account must return None immediately
            assert w.try_claim_idle_slot() is None
        assert len(claimed) == 6
    finally:
        for w, fd in claimed:
            w._unlock_fd(fd)
            os.close(fd)
            w._concurrency_sem.release()


def test_save_batch_anchors_fallback_publish_ts_in_set_on_insert_without_conflict():
    from mobile_web.scripts.run_deal_pool_scheduler import DealRawPoolRepository

    captured_ops = []

    class _DummyCol:
        def bulk_write(self, ops, ordered=False):
            captured_ops.extend(ops)
            return SimpleNamespace(upserted_count=len(ops), modified_count=0)

    dummy_col = _DummyCol()
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: dummy_col))
    repo._indexes_ready = True

    res = repo.save_batch(
        [
            {
                "source_site": "haodanku",
                "source_item_id": "hdk_1",
                "publish_time": "",
                "title": "无发布时间线报",
                "extracted_links": [{"url": "https://u.jd.com/2GR8aBc"}],
                "has_full_detail": True,
            },
            {
                "source_site": "zhetaoke",
                "source_item_id": "xb_2",
                "publish_time": "2026-11-12 06:30:00",
                "title": "有明确发布时间线报",
                "extracted_links": [{"url": "https://u.jd.com/3HS9dEf"}],
                "has_full_detail": True,
            },
        ],
        task_name="unit_test",
    )
    assert res["inserted"] == 2
    op_no_pub, op_with_pub = captured_ops[0]._doc, captured_ops[1]._doc

    # Empty publish_time: publish_ts is only in $setOnInsert (never overwritten on re-poll, no Mongo path conflict)
    assert "publish_ts" in op_no_pub["$setOnInsert"]
    assert "publish_ts" not in op_no_pub["$set"]

    # Explicit publish_time: publish_ts is in $set and absent from $setOnInsert
    assert "publish_ts" in op_with_pub["$set"]
    assert "publish_ts" not in op_with_pub["$setOnInsert"]


def test_run_clean_stage_sweeps_over_7d_by_either_publish_ts_or_first_seen_at():
    from mobile_web.scripts.run_deal_pool_scheduler import DealRawPoolRepository, run_clean_stage

    update_queries = []
    count_queries = []

    class _DummyRawCol:
        def update_many(self, flt, upd):
            update_queries.append((flt, upd))
            return SimpleNamespace(modified_count=2 if len(update_queries) == 1 else 0)

        def count_documents(self, flt):
            count_queries.append(flt)
            return 0

    raw_col = _DummyRawCol()
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: raw_col))
    repo._indexes_ready = True

    out = run_clean_stage(repo, cleaner=SimpleNamespace(), cycle_num=1, clean_limit=10)
    assert out["expired_over_7d_swept"] == 2
    sweep_or = update_queries[0][0]["$and"][1]["$or"]
    assert any("publish_ts" in cond for cond in sweep_or)
    assert any("first_seen_at" in cond and "$and" not in cond for cond in sweep_or)
    assert "$nor" in count_queries[0]["$and"][1]


def test_format_raw_message_skips_xianbao_synthetic_haodan_weibo_title_but_keeps_real_titles():
    # 1. xianbao_fun 好单线报 / 微博线报 synthetic title with broken regex price -> skipped when body exists
    haodan_doc = {
        "source_site": "xianbao_fun",
        "source_channel": "category_haodan:好单线报-服饰-淘宝",
        "source_item_id": "7465539",
        "title": "乔丹被窝防风防泼水羽绒服 拍1 15元",
        "tutorial_text": "速 乔丹被窝防风防泼水羽绒服\n弹 下拉叠金币任选拍115元\nhttps://m.tb.cn/h.jQvw5nR",
    }
    msg_haodan = DealPoolCleaner._format_raw_message_for_ai1(haodan_doc)
    assert "【标题】" not in msg_haodan["content"]
    assert "拍115元" in msg_haodan["content"]

    # 2. Fallback safety: if xianbao_fun detail fetch failed, title is still preserved
    haodan_err_doc = {
        "source_site": "xianbao_fun",
        "source_channel": "haodan_hot:好单线报-服饰-淘宝",
        "source_item_id": "7465540",
        "title": "波司登羽绒服 351.45元",
        "tutorial_text": "预览\n[详情拉取异常: Timeout]",
    }
    msg_err = DealPoolCleaner._format_raw_message_for_ai1(haodan_err_doc)
    assert "【标题】波司登羽绒服 351.45元" in msg_err["content"]

    # 3. xianbao_fun 豆瓣线报 (real user thread title with price only in title) -> MUST keep 【标题】
    douban_doc = {
        "source_site": "xianbao_fun",
        "source_channel": "category_douban:豆瓣线报-买组",
        "source_item_id": "7460001",
        "title": "欧莱雅紫熨斗眼霜60ml 145元",
        "tutorial_text": "[教程图片: https://img3.doubanio.com/view/group_topic/l/public/p1.webp]",
    }
    msg_douban = DealPoolCleaner._format_raw_message_for_ai1(douban_doc)
    assert msg_douban["content"].startswith("【标题】欧莱雅紫熨斗眼霜60ml 145元")

    # 4. haodanku / bacaoo editorial title with price only in title -> MUST keep 【标题】
    hdk_doc = {
        "source_site": "haodanku",
        "source_channel": "wire_report:淘宝实时线报",
        "source_item_id": "1650001",
        "title": "💰24.6 | 柏瑞美定妆喷雾100ml",
        "tutorial_text": "①柏瑞美定妆喷雾 https://uland.taobao.com/item/edetail?id=abc",
    }
    msg_hdk = DealPoolCleaner._format_raw_message_for_ai1(hdk_doc)
    assert msg_hdk["content"].startswith("【标题】💰24.6 | 柏瑞美定妆喷雾100ml")


def test_stage2_prompt_hot_reloads_on_mtime_change(monkeypatch, tmp_path):
    import os
    import mobile_web.scripts.run_deal_pool_scheduler as sched_mod

    p_file = tmp_path / "stage2_prompt.txt"
    p_file.write_text("初始提示词v1", encoding="utf-8")
    monkeypatch.setattr(sched_mod, "PROMPT_STAGE2_PATH", str(p_file))

    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    assert cleaner._get_stage2_prompt() == "初始提示词v1"

    p_file.write_text("热更新提示词v2", encoding="utf-8")
    st = os.stat(p_file)
    os.utime(p_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert cleaner._get_stage2_prompt() == "热更新提示词v2"


def test_deal_max_clean_age_days_is_2_days():
    import datetime
    from mobile_web.scripts.run_deal_pool_scheduler import (
        DEAL_MAX_CLEAN_AGE_DAYS,
        DealRawPoolRepository,
    )

    assert DEAL_MAX_CLEAN_AGE_DAYS == 2

    captured_ops = []

    class _DummyCol:
        def bulk_write(self, ops, ordered=False):
            captured_ops.extend(ops)
            return SimpleNamespace(upserted_count=len(ops), modified_count=0)

    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: _DummyCol()))
    repo._indexes_ready = True

    bj_tz = datetime.timezone(datetime.timedelta(hours=8))
    now_bj = datetime.datetime.now(bj_tz)
    fresh_20h = (now_bj - datetime.timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")
    stale_52h = (now_bj - datetime.timedelta(hours=52)).strftime("%Y-%m-%d %H:%M:%S")

    repo.save_batch(
        [
            {
                "source_site": "haodanku",
                "source_item_id": "fresh_1",
                "publish_time": fresh_20h,
                "title": "20小时内线报",
                "extracted_links": [{"url": "https://u.jd.com/2GR8aBc"}],
                "has_full_detail": True,
            },
            {
                "source_site": "haodanku",
                "source_item_id": "stale_1",
                "publish_time": stale_52h,
                "title": "52小时前过期线报",
                "extracted_links": [{"url": "https://u.jd.com/3HS9dEf"}],
                "has_full_detail": True,
            },
        ],
        task_name="age_test",
    )
    assert captured_ops[0]._doc["$setOnInsert"]["clean_status"] == "pending"
    assert captured_ops[1]._doc["$setOnInsert"]["clean_status"] == "cleaned_failed"


def test_pure_jd_coupon_only_routed_to_context_only_or_skipped_without_touching_real_deals():
    from mobile_web.scripts.run_deal_pool_scheduler import (
        DealRawPoolRepository,
        _is_pure_jd_coupon_only_record,
    )

    captured_ops = []

    class _DummyCol:
        def bulk_write(self, ops, ordered=False):
            captured_ops.extend(ops)
            return SimpleNamespace(upserted_count=len(ops), modified_count=0)

    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: _DummyCol()))
    repo._indexes_ready = True

    ztk_coupon_1 = {
        "source_site": "zhetaoke",
        "source_item_id": "xb_5001",
        "is_group_stream": True,
        "title": "速度防身，京东自营生鲜满199减100神券",
        "tutorial_text": "速度防身领券 https://coupon.m.jd.com/coupons/show.action?key=c3m6c1sbo2a740939106&roleId=123456\n[群聊晒单/教程图片: https://img.zhetaoke.com/pic1.jpg]",
        "extracted_links": [
            {"url": "https://coupon.m.jd.com/coupons/show.action?key=c3m6c1sbo2a740939106&roleId=123456", "platform": "jd"}
        ],
        "extracted_tokens": [],
        "has_full_detail": True,
    }
    ztk_coupon_login_wrap = {
        "source_site": "zhetaoke",
        "source_item_id": "xb_5002",
        "is_group_stream": True,
        "title": "PLUS补贴券防身",
        "tutorial_text": "https://plogin.m.jd.com/cgi-bin/m/tmauth?returnurl=https%3A%2F%2Fcoupon.m.jd.com%2Fcoupons%2Fshow.action%3FlinkKey%3DAAROH_xIpeffAs_-naABEFoe",
        "extracted_links": [
            {
                "url": "https://plogin.m.jd.com/cgi-bin/m/tmauth?returnurl=https://coupon.m.jd.com/coupons/show.action?linkKey=AAROH_xIpeffAs_-naABEFoe",
                "platform": "jd",
            }
        ],
        "extracted_tokens": [],
        "has_full_detail": True,
    }
    non_group_pure_coupon = {
        "source_site": "xianbao_fun",
        "source_item_id": "xbf_5003",
        "is_group_stream": False,
        "title": "京东领券防身",
        "tutorial_text": "先领 https://coupon.m.jd.com/coupons/show.action?key=abc&roleId=999",
        "extracted_links": [{"url": "https://coupon.m.jd.com/coupons/show.action?key=abc&roleId=999", "platform": "jd"}],
        "extracted_tokens": [],
        "has_full_detail": True,
    }
    coupon_plus_product = {
        "source_site": "zhetaoke",
        "source_item_id": "xb_5004",
        "is_group_stream": True,
        "title": "领券买维达抽纸24包19.9元",
        "tutorial_text": "先领券 https://coupon.m.jd.com/coupons/show.action?key=abc&roleId=999\n再拍主商品 https://u.jd.com/oGtS1YL",
        "extracted_links": [
            {"url": "https://coupon.m.jd.com/coupons/show.action?key=abc&roleId=999", "platform": "jd"},
            {"url": "https://u.jd.com/oGtS1YL", "platform": "jd"},
        ],
        "extracted_tokens": [],
        "has_full_detail": True,
    }
    coupon_plus_naked_tb_token = {
        "source_site": "zhetaoke",
        "source_item_id": "xb_5005",
        "is_group_stream": True,
        "title": "领券拍百补商品",
        "tutorial_text": "领券 https://coupon.m.jd.com/coupons/show.action?key=abc&roleId=999\n口令 ￥AbCdEfGh123￥",
        "extracted_links": [{"url": "https://coupon.m.jd.com/coupons/show.action?key=abc&roleId=999", "platform": "jd"}],
        "extracted_tokens": [],
        "has_full_detail": True,
    }

    assert _is_pure_jd_coupon_only_record(ztk_coupon_1) is True
    assert _is_pure_jd_coupon_only_record(ztk_coupon_login_wrap) is True
    assert _is_pure_jd_coupon_only_record(coupon_plus_product) is False
    assert _is_pure_jd_coupon_only_record(coupon_plus_naked_tb_token) is False

    res = repo.save_batch(
        [ztk_coupon_1, ztk_coupon_login_wrap, non_group_pure_coupon, coupon_plus_product, coupon_plus_naked_tb_token],
        task_name="coupon_filter_test",
    )
    assert res["context_only_saved"] == 2
    assert res["skipped_no_signal"] == 1
    assert len(captured_ops) == 4
    status_by_id = {
        op._doc["$setOnInsert"]["source_item_id"]: op._doc["$setOnInsert"]["clean_status"]
        for op in captured_ops
    }
    assert status_by_id == {
        "xb_5001": "context_only",
        "xb_5002": "context_only",
        "xb_5004": "pending",
        "xb_5005": "pending",
    }


def test_zhetaoke_group_retry_reuses_saved_ai1_and_hydrates_merged_neighbors():
    from mobile_web.scripts.run_deal_pool_scheduler import ERR_AI_TIMEOUT_RETRY

    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    fake_jd_adapter = MagicMock()

    def _mock_convert(clean_inp):
        if "coupon.m.jd.com" in clean_inp:
            return {"status": "failed", "error_code": "UPSTREAM_ERROR", "message": "native coupon"}
        return {
            "status": "success",
            "goods_id": "100888777666",
            "goods_sign": "https://u.jd.com/my_main_prod",
            "final_link": "https://u.jd.com/my_main_prod",
            "url": "https://u.jd.com/my_main_prod",
            "promotion_token": "￥MyJdToken￥",
            "product": {
                "platform": "jd",
                "product_id": "100888777666",
                "name": "维达超韧抽纸3层130抽24包",
                "price": 39.9,
                "original_price": 49.9,
                "activity_price": 39.9,
                "coupon_amount": 0.0,
                "mall_name": "维达京东自营官方旗舰店",
                "image": "https://img14.360buyimg.com/n1/jfs/vd.jpg",
            },
        }

    fake_jd_adapter.resolve_and_convert_universal.side_effect = _mock_convert
    cleaner.adapters["jd"] = fake_jd_adapter

    ai1_calls = {"n": 0}

    def _unexpected_stage1(*args, **kwargs):
        ai1_calls["n"] += 1
        return {}

    def _fake_stage2(stage2_inputs, total_pending_count=0, quiet_sub_logs=False):
        return {
            inp["main_tip_id"]: {
                "main_tip_id": inp["main_tip_id"],
                "is_valid_deal": True,
                "main_link_id": "L2",
                "coupon_links": [{"link_id": "L1", "coupon_desc": "满39减20券"}],
                "filler_links": [],
                "deal_price": 19.9,
                "tutorial_steps": ["1. 先领券 [#L1]", "2. 再拍维达抽纸 [#L2] 实付19.9元"],
                "quality_score": 88,
                "score_breakdown": {"price_advantage": 36, "brand_reputation": 26, "completeness": 26},
            }
            for inp in stage2_inputs
        }

    cleaner.run_ai_stage1_batch = _unexpected_stage1
    cleaner.run_ai_stage2_batch = _fake_stage2

    neighbor_coupon_doc = {
        "source_site": "zhetaoke",
        "source_channel": "zhetaoke_group_1",
        "source_item_id": "xb_100",
        "is_group_stream": True,
        "source_url": "https://zhetaoke.example/msg/xb_100",
        "title": "维达满39减20神券",
        "tutorial_text": "先领20元防身券 https://coupon.m.jd.com/coupons/show.action?key=vd20&roleId=111",
    }
    main_retry_doc = {
        "source_site": "zhetaoke",
        "source_channel": "zhetaoke_group_1",
        "source_item_id": "xb_101",
        "is_group_stream": True,
        "source_url": "https://zhetaoke.example/msg/xb_101",
        "platform": "jd",
        "title": "维达抽纸24包19.9元",
        "tutorial_text": "领楼上券拍维达抽纸19.9元 https://u.jd.com/other_prod",
        "extracted_links": [{"url": "https://u.jd.com/other_prod", "platform": "jd"}],
        "extracted_tokens": [],
        "clean_retry_count": 1,
        "clean_error_code": ERR_AI_TIMEOUT_RETRY,
        "ai_stage1_result": {
            "main_tip_id": "xb_101",
            "is_standalone_deal": True,
            "merged_tip_ids": ["xb_100", "xb_101"],
            "combined_raw_text": "先领20元防身券 [#L1]\n领楼上券拍维达抽纸19.9元 [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {
                    "link_id": "L1",
                    "raw_span": "https://coupon.m.jd.com/coupons/show.action?key=vd20&roleId=111",
                    "clean_input": "https://coupon.m.jd.com/coupons/show.action?key=vd20&roleId=111",
                    "platform": "jd",
                    "text_role_hint": "coupon",
                },
                {
                    "link_id": "L2",
                    "raw_span": "https://u.jd.com/other_prod",
                    "clean_input": "https://u.jd.com/other_prod",
                    "platform": "jd",
                    "text_role_hint": "main_candidate",
                },
            ],
            "_from_fallback": False,
        },
    }

    raw_col = MagicMock()
    raw_col.find.return_value = [neighbor_coupon_doc]

    results = cleaner.clean_batch_records([main_retry_doc], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert ai1_calls["n"] == 0  # Reused saved_ai1 without re-calling AI-1!
    assert results[0]["status"] == "success"
    raw_col.find.assert_called_once_with(
        {"source_site": "zhetaoke", "source_item_id": {"$in": ["xb_100"]}},
        {"raw_payload": 0},
    )
    deal_info = results[0]["product_doc"]["deal_info"]
    assert "先领20元防身券" in deal_info["original_copywriting"]
    assert "领楼上券拍维达抽纸19.9元" in deal_info["original_copywriting"]
    assert "https://zhetaoke.example/msg/xb_100" in deal_info["merged_source_urls"]
    assert "https://zhetaoke.example/msg/xb_101" in deal_info["merged_source_urls"]


def test_warmup_anchor_cache_from_db_sorts_by_cleaned_at_desc():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    sort_calls = []
    limit_calls = []

    class _FakeCursor:
        def sort(self, key, direction):
            sort_calls.append((key, direction))
            return self

        def limit(self, n):
            limit_calls.append(n)
            return [
                {
                    "converted_links": [
                        {
                            "link_id": "L1",
                            "clean_input": "https://u.jd.com/cached_anchor_1",
                            "convert_status": "product",
                            "platform": "jd",
                            "goods_id": "100012345678",
                            "goods_sign": "https://u.jd.com/my_cached_1",
                            "my_promo_link": "https://u.jd.com/my_cached_1",
                        }
                    ]
                }
            ]

    raw_col = MagicMock()
    raw_col.find.return_value = _FakeCursor()

    cleaner.warmup_anchor_cache_from_db(raw_col)
    assert sort_calls == [("cleaned_at", -1)]
    assert limit_calls == [3000]
    assert "jd:https://u.jd.com/cached_anchor_1" in cleaner.anchor_cache



