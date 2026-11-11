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

