"""
回归测试：好价线报清洗引擎抗抖动、折淘客双 Key 轮询与熔断、暂态转链失败自动重排与活人话术无损脱敏
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mobile_web.infrastructure.http_utils import error_dict, execute_with_key_failover
from mobile_web.scripts.run_deal_pool_scheduler import (
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_JD_NUMERIC_SKU,
    DealPoolCleaner,
)


def test_jd_and_ztk_client_real_round_robin_vs_sticky():
    from infrastructure.jd_client import JdClient
    from infrastructure.ztk_client import ZtkClient

    def _reply(body):
        return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)

    jd_keys = []
    jd_http = SimpleNamespace(
        get=lambda url, params=None, **_kw: jd_keys.append(params["appkey"]) or _reply({"status": 200, "content": [{"tao_id": "1"}]})
    )
    jd_cli = JdClient(appkey_list=["jd-k1", "jd-k2"], union_id="10001", http_session=jd_http)
    for _ in range(4):
        assert jd_cli.search_goods("维达抽纸")["status"] == "success"
    assert jd_keys == ["jd-k1", "jd-k2", "jd-k1", "jd-k2"]

    # ZtkClient sharing a single sid stays sticky (round_robin=False)
    tb_sticky = []
    sticky_http = SimpleNamespace(
        get=lambda url, params=None, **_kw: tb_sticky.append((params["appkey"], params["sid"])) or _reply(
            {"status": 200, "content": [{"tao_id": "101", "coupon_click_url": "https://uland.taobao.com/e=1", "tkl": "￥T1￥"}]}
        )
    )
    ztk_sticky = ZtkClient(appkey_list=["tk-1", "tk-2"], sid="sid-shared", http_session=sticky_http)
    for _ in range(3):
        assert ztk_sticky.convert_channel_url("https://item.taobao.com/item.htm?id=101", pid="mm_1_2_3", allow_public=True)["status"] == "success"
    assert tb_sticky == [("tk-1", "sid-shared"), ("tk-1", "sid-shared"), ("tk-1", "sid-shared")]

    # ZtkClient with per-key paired sid_list rotates round-robin with matching (appkey, sid) pairs
    tb_rr = []
    rr_http = SimpleNamespace(
        get=lambda url, params=None, **_kw: tb_rr.append((params["appkey"], params["sid"])) or _reply(
            {"status": 200, "content": [{"tao_id": "101", "coupon_click_url": "https://uland.taobao.com/e=1", "tkl": "￥T2￥"}]}
        )
    )
    ztk_rr = ZtkClient(appkey_list=["tk-1", "tk-2"], sid_list=["sid-1", "sid-2"], http_session=rr_http)
    for _ in range(4):
        assert ztk_rr.convert_channel_url("https://item.taobao.com/item.htm?id=101", pid="mm_1_2_3", allow_public=True)["status"] == "success"
    assert tb_rr == [("tk-1", "sid-1"), ("tk-2", "sid-2"), ("tk-1", "sid-1"), ("tk-2", "sid-2")]


def test_stage3_transient_failure_schedules_retry_reuses_ai1_and_evicts_transient_cache():
    cleaner = DealPoolCleaner(ai_batch_size=10, ai_max_workers=2, convert_max_workers=2)
    cleaner._cache_warmed_up = True

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
    assert ai1_call_count["n"] == 1
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
    assert ai1_call_count["n"] == 1


def test_deterministic_error_remains_cached_and_does_not_retry():
    cleaner = DealPoolCleaner(ai_batch_size=10, ai_max_workers=2, convert_max_workers=2)
    cleaner._cache_warmed_up = True
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
    assert "jd:https://item.jd.com/100012345678.html" in cleaner.anchor_cache
    assert fake_jd_adapter.resolve_and_convert_universal.call_count == 1

    # Even after transient failure eviction and a second batch run, deterministic failure stays cached without re-hitting upstream
    cleaner._evict_transient_failed_cache()
    assert "jd:https://item.jd.com/100012345678.html" in cleaner.anchor_cache
    doc2 = dict(doc, source_item_id="tip_sku_2")
    res2 = cleaner.clean_batch_records([doc2], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert res2[0]["status"] == "failed" and res2[0]["error_code"] == ERR_JD_NUMERIC_SKU
    assert fake_jd_adapter.resolve_and_convert_universal.call_count == 1


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


def test_run_clean_stage_sweeps_stale_pending_by_either_publish_ts_or_first_seen_at():
    import datetime
    import time
    from mobile_web.scripts.run_deal_pool_scheduler import (
        DEAL_MAX_CLEAN_AGE_DAYS,
        ERR_AI_MAX_RETRIES_EXCEEDED,
        ERR_EXPIRED_OVER_7_DAYS,
        MAX_CLEAN_RETRIES,
        DealRawPoolRepository,
        run_clean_stage,
    )

    def _matches(doc, flt):
        for key, cond in flt.items():
            if key == "$and":
                if not all(_matches(doc, sub) for sub in cond):
                    return False
            elif key == "$or":
                if not any(_matches(doc, sub) for sub in cond):
                    return False
            elif key == "$nor":
                if any(_matches(doc, sub) for sub in cond):
                    return False
            elif isinstance(cond, dict):
                exists = key in doc
                val = doc.get(key)
                if "$exists" in cond and exists != bool(cond["$exists"]):
                    return False
                if "$gt" in cond and (val is None or not (val > cond["$gt"])):
                    return False
                if "$gte" in cond and (val is None or not (val >= cond["$gte"])):
                    return False
                if "$lt" in cond and (val is None or not (val < cond["$lt"])):
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    now_ts = time.time()
    stale_ts = now_ts - (DEAL_MAX_CLEAN_AGE_DAYS + 1) * 86400.0
    stale_dt = now_utc - datetime.timedelta(days=DEAL_MAX_CLEAN_AGE_DAYS + 1)
    fresh_dt = now_utc - datetime.timedelta(hours=1)

    docs = [
        {"_id": "exp_pub", "clean_status": "pending", "publish_ts": stale_ts, "first_seen_at": fresh_dt},
        {"_id": "exp_seen", "publish_ts": 0, "first_seen_at": stale_dt},
        {"_id": "retry_cap", "clean_status": "pending", "publish_ts": now_ts - 3600, "first_seen_at": fresh_dt, "clean_retry_count": MAX_CLEAN_RETRIES},
        {"_id": "cooling", "clean_status": "pending", "publish_ts": now_ts - 3600, "first_seen_at": fresh_dt, "next_retry_after_ts": now_ts + 300},
    ]

    class _MemRawCol:
        def update_many(self, flt, upd):
            modified = 0
            for doc in docs:
                if _matches(doc, flt):
                    doc.update(upd.get("$set", {}))
                    modified += 1
            return SimpleNamespace(modified_count=modified)

        def count_documents(self, flt):
            return sum(1 for doc in docs if _matches(doc, flt))

    raw_col = _MemRawCol()
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: raw_col))
    repo._indexes_ready = True

    out = run_clean_stage(repo, cleaner=SimpleNamespace(), cycle_num=1, clean_limit=10)
    assert out["expired_over_7d_swept"] == 2
    assert out["total_pending_found"] == 0
    by_id = {d["_id"]: d for d in docs}
    assert by_id["exp_pub"]["clean_status"] == "cleaned_failed"
    assert by_id["exp_pub"]["clean_error_code"] == ERR_EXPIRED_OVER_7_DAYS
    assert by_id["exp_seen"]["clean_status"] == "cleaned_failed"
    assert by_id["exp_seen"]["clean_error_code"] == ERR_EXPIRED_OVER_7_DAYS
    assert by_id["retry_cap"]["clean_status"] == "cleaned_failed"
    assert by_id["retry_cap"]["clean_error_code"] == ERR_AI_MAX_RETRIES_EXCEEDED
    assert by_id["cooling"]["clean_status"] == "pending"


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


def test_convert_single_target_admits_own_promo_and_rejects_failed_or_echo():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner.adapters = {
        "taobao": SimpleNamespace(resolve_and_convert_universal=lambda _x: {"status": "error", "error_msg": "不支持转链"}),
        "jd": SimpleNamespace(
            resolve_and_convert_universal=lambda x: {
                "status": "success",
                "final_link": "https://u.jd.com/echoSame" if "echoSame" in x else "https://u.jd.com/ogDTQlg",
                "promotion_token": "" if "echoSame" in x else "！FBxJIEwx！ CA1393",
            }
        ),
    }
    bad_tb = cleaner.convert_single_target(
        {"link_id": "L1", "clean_input": "https://s.click.taobao.com/XNl8wji", "platform": "taobao", "text_role_hint": "coupon_candidate"}
    )
    assert bad_tb["convert_status"] == "failed"
    assert bad_tb["my_promo_link"] is None

    echo_jd = cleaner.convert_single_target(
        {"link_id": "L_ECHO", "clean_input": "https://u.jd.com/echoSame", "platform": "jd", "text_role_hint": "coupon_candidate"}
    )
    assert echo_jd["convert_status"] == "failed"
    assert echo_jd["my_promo_link"] is None

    own_jd = cleaner.convert_single_target(
        {"link_id": "L2", "clean_input": "https://u.jd.com/oaDAVcO", "platform": "jd", "text_role_hint": "coupon_candidate"}
    )
    assert own_jd["convert_status"] == "coupon_or_activity"
    assert own_jd["error_code"] == "OK_COUPON"
    assert own_jd["my_promo_link"] == "https://u.jd.com/ogDTQlg"


def test_copywriting_space_normalization_and_deal_12h_clock_isolation():
    import datetime
    from mobile_web.scripts.run_deal_pool_scheduler import upsert_cleaned_products_to_formal_table

    # 1. \u00a0 不换行空格、mp://、/:/ 口令壳与 strip_spans 100% 精准剥离，正常型号与中文描述完好无损
    doc = {
        "source_item_id": "tip_nbsp",
        "title": "好价牛奶",
        "tutorial_text": (
            "不到一块钱一盒的金典纯牛奶，型号 Wacom DTK168 同场好价\n"
            "38￥\u00a0\u00a0pFTS6WMzTIi￥\n"
            "7Fu🌹zhiHU7177,打開/\n"
            "https://u.jd.com/o1s7UHc\n"
            "微信小程序 mp://kX9aBcDeFgHiJkL\n"
            "复制口令 /:/ 速度锁单手慢无！\n"
            "guangdiu.com/go.php?id=30504742"
        ),
        "extracted_links": [{"url": "https://u.jd.com/o1s7UHc"}],
        "extracted_tokens": [],
    }
    cleaned = DealPoolCleaner._build_clean_original_copywriting(
        ["tip_nbsp"],
        {"tip_nbsp": doc},
        [
            {"raw_span": "38￥  pFTS6WMzTIi￥", "clean_input": "38￥  pFTS6WMzTIi￥"},
            {"raw_span": "7Fu🌹zhiHU7177,打開/", "clean_input": "HU7177"},
        ],
        doc=doc,
        strip_spans=["guangdiu.com/go.php?id=30504742"],
    )
    assert "pFTS6WMzTIi" not in cleaned
    assert "HU7177" not in cleaned
    assert "guangdiu.com" not in cleaned
    assert "mp://" not in cleaned
    assert "u.jd.com" not in cleaned
    assert "/:/" not in cleaned
    assert "不到一块钱一盒的金典纯牛奶，型号 Wacom DTK168 同场好价" in cleaned
    assert "速度锁单手慢无！" in cleaned

    # 2. 顶层 updated_at 刚被刷新但 deal_info.updated_at 已超 12h 时，允许新线报覆盖并重置 deal_info.format_*
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    stored_doc = {
        "platform": "jd",
        "product_id": "10001",
        "updated_at": now - datetime.timedelta(minutes=1),
        "deal_info": {
            "is_deal_tip": True,
            "quality_score": 92,
            "deal_price": 32.9,
            "updated_at": now - datetime.timedelta(hours=18),
            "format_status": "success",
            "format_info": {"sku_id": "10001_deal", "price": 32.9},
        },
    }
    captured_ops = []
    formal_col = MagicMock()
    formal_col.find.return_value = [stored_doc]
    formal_col.bulk_write.side_effect = lambda ops, ordered=False: (
        captured_ops.extend(ops) or SimpleNamespace(upserted_count=0, modified_count=1, matched_count=1)
    )

    with patch("infrastructure.search_meili.is_meili_available", return_value=False):
        res = upsert_cleaned_products_to_formal_table(
            formal_col,
            [
                {
                    "platform": "jd",
                    "product_id": "10001",
                    "name": "可口可乐24罐",
                    "price": 45.9,
                    "deal_info": {"is_deal_tip": True, "quality_score": 80, "deal_price": 26.8},
                }
            ],
        )
    assert res["protected_high_score"] == 0
    set_doc = captured_ops[0]._doc["$set"]
    assert set_doc["deal_info.deal_price"] == 26.8
    assert set_doc["deal_info.need_reformat"] is True
    assert set_doc["deal_info.format_status"] == "pending"
    assert set_doc["deal_info.format_info"] is None
    assert isinstance(set_doc["deal_info.updated_at"], datetime.datetime)


def test_minimalist_four_element_pipeline_and_zero_loss_copywriting():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    # 1. 数学斜体/粗体 Unicode 还原为标准 ASCII，同时完整保留中文全角标点与 Emoji；剥离拔草哦合集拼接头
    bacaoo_doc = {
        "source_site": "bacaoo",
        "source_item_id": "bc_sub_1",
        "title": "【奶嘴2个装】贝亲宽口径奶嘴 ￥41.5",
        "tutorial_text": (
            "【PDD热销产品汇总14点档更新2026/11/13-拔草哦国内优惠频道】"
            "【奶嘴2个装】𝟖𝟖𝒗𝒊𝒑 𝑩𝒖𝒈好价！贝亲宽口径奶嘴 ￥41.5 🚗\n"
            "直达链接: https://p.pinduoduo.com/abc123"
        ),
        "extracted_links": [{"url": "https://p.pinduoduo.com/abc123", "platform": "pdd"}],
        "extracted_tokens": [],
    }
    bc_copy = DealPoolCleaner._build_clean_original_copywriting(["bc_sub_1"], {"bc_sub_1": bacaoo_doc}, [], doc=bacaoo_doc)
    assert "拔草哦" not in bc_copy
    assert "PDD热销产品汇总" not in bc_copy
    assert "88vip Bug好价！" in bc_copy
    assert "【奶嘴2个装】" in bc_copy
    assert "🚗" in bc_copy
    assert "pinduoduo.com" not in bc_copy

    # 2. 凑单品 (filler_candidate) 在 Stage 3 完全跳过 API 转链，但原始凑单口令照常剔除，且 AI-2 微整形不丢凑单动作、教程纯文字无链
    converted_inputs = []
    fake_tb_adapter = MagicMock()

    def _mock_tb_convert(raw_input):
        converted_inputs.append(raw_input)
        if "CPN_OK" in raw_input:
            return {
                "status": "success",
                "goods_id": "",
                "goods_sign": "",
                "final_link": "https://uland.taobao.com/quan/detail?sellerId=1&activityId=2",
                "url": "https://uland.taobao.com/quan/detail?sellerId=1&activityId=2",
                "promotion_token": "1$MYCPN123$://",
                "product": None,
            }
        if "MAIN_OK" in raw_input:
            return {
                "status": "success",
                "goods_id": "TB888999",
                "goods_sign": "TB888999",
                "final_link": "https://s.click.taobao.com/my_main",
                "url": "https://s.click.taobao.com/my_main",
                "promotion_token": "2$MYMAIN456$://",
                "product": {
                    "platform": "taobao",
                    "product_id": "TB888999",
                    "name": "名创优品一次性纯棉洗脸巾3包",
                    "price": 29.9,
                    "original_price": 39.9,
                    "activity_price": 29.9,
                    "coupon_amount": 0.0,
                    "mall_name": "名创优品旗舰店",
                },
            }
        raise AssertionError(f"Unexpected conversion call for {raw_input}")

    fake_tb_adapter.resolve_and_convert_universal.side_effect = _mock_tb_convert
    cleaner.adapters["taobao"] = fake_tb_adapter

    raw_doc = {
        "source_site": "zhetaoke",
        "source_channel": "zhetaoke_group_1",
        "source_item_id": "ztk_999",
        "platform": "taobao",
        "title": "名创优品洗脸巾3包9.9亓",
        "tutorial_text": (
            "𝟖𝟖𝒗𝒊𝒑礼金+淘金碧到手9.9亓\n"
            "名创优品 一次性洗脸巾3包\n"
            "领黑五200-20卷：7$CPN_OK_11$://\n"
            "下单：8$MAIN_OK_22$://\n"
            "凑单：9Fu🌹zhiFIL9999,打開/\n"
            "（可退）"
        ),
        "extracted_links": [],
        "extracted_tokens": [],
    }

    cleaner.run_ai_stage1_batch = lambda *args, **kwargs: {
        "ztk_999": {
            "main_tip_id": "ztk_999",
            "is_standalone_deal": True,
            "merged_tip_ids": ["ztk_999"],
            "combined_raw_text": (
                "88vip礼金+淘金碧到手9.9亓\n"
                "名创优品 一次性洗脸巾3包\n"
                "领黑五200-20卷： [#L1]\n"
                "下单： [#L2]\n"
                "凑单： [#L3]\n"
                "（可退）"
            ),
            "extracted_images": [],
            "extracted_targets": [
                {
                    "link_id": "L1",
                    "raw_span": "7$CPN_OK_11$://",
                    "clean_input": "7$CPN_OK_11$://",
                    "platform": "taobao",
                    "text_role_hint": "coupon_candidate",
                },
                {
                    "link_id": "L2",
                    "raw_span": "8$MAIN_OK_22$://",
                    "clean_input": "8$MAIN_OK_22$://",
                    "platform": "taobao",
                    "text_role_hint": "main_candidate",
                },
                {
                    "link_id": "L3",
                    "raw_span": "9Fu🌹zhiFIL9999,打開/",
                    "clean_input": "9Fu🌹zhiFIL9999,打開/",
                    "platform": "taobao",
                    "text_role_hint": "filler_candidate",
                },
            ],
            "_from_fallback": False,
        }
    }

    captured_s2_inputs = []

    def _mock_s2(stage2_inputs, total_pending_count=0, quiet_sub_logs=False):
        captured_s2_inputs.extend(stage2_inputs)
        return {
            "ztk_999": {
                "main_tip_id": "ztk_999",
                "is_valid_deal": True,
                "main_link_id": "L2",
                "clean_title": "名创优品一次性纯棉洗脸巾3包",
                "deal_price": 9.9,
                "unit_deal_price": "3.3元/包",
                "price_logic": "88VIP + 首单礼金 + 淘金币 + 领满200减20券凑单（可退）",
                "coupon_links": [{"link_id": "L1", "label": "先领：黑五满200减20券"}],
                "tutorial_steps": [
                    "1. 先在上方【领券】处领取黑五满200减20券 [#L1]",
                    "2. 从淘宝金币频道加购本品 1 件 https://s.click.taobao.com/accidental (口令: 2$MYMAIN456$://)",
                    "3. 按满200减20门槛自行搭配凑单品合并付款（凑单品付款后可按需退掉），本品到手约 9.9 元",
                ],
                "polished_copywriting": (
                    "88vip礼金+淘金碧到手9.9亓\n"
                    "名创优品 一次性洗脸巾3包\n"
                    "领黑五200-20卷，凑单拍下（可退）"
                ),
                "quality_score": 90,
                "score_breakdown": {"match_score": 28, "completeness_score": 28, "price_clarity_score": 18, "tutorial_and_image_score": 16},
            }
        }

    cleaner.run_ai_stage2_batch = _mock_s2
    raw_col = MagicMock()
    raw_col.find.return_value = []

    results = cleaner.clean_batch_records([raw_doc], raw_col, shared_active_products={}, quiet_sub_logs=True)
    assert len(results) == 1
    assert results[0]["status"] == "success"

    # 凑单口令 L3 绝不调用官方转链 API，但在 draft_copywriting 中已被完整剥离
    assert converted_inputs == ["7$CPN_OK_11$://", "8$MAIN_OK_22$://"]
    assert len(captured_s2_inputs) == 1
    assert "FIL9999" not in captured_s2_inputs[0]["draft_copywriting"]
    l3_meta = next(c for c in captured_s2_inputs[0]["converted_links"] if c["link_id"] == "L3")
    assert l3_meta["convert_status"] == "skipped_filler"

    deal_info = results[0]["product_doc"]["deal_info"]
    assert "filler_links" not in deal_info
    assert "auxiliary_links" not in deal_info
    assert len(deal_info["coupon_links"]) == 1
    assert deal_info["coupon_links"][0]["label"] == "黑五满200减20券"
    assert deal_info["original_copywriting"] == (
        "88vip礼金+淘金碧到手9.9亓\n"
        "名创优品 一次性洗脸巾3包\n"
        "领黑五200-20卷，凑单拍下（可退）"
    )
    # 教程为 100% 纯文字，无 [#L1]、无网址、无口令，且完整保留凑单动作
    for step in deal_info["tutorial_steps"]:
        assert "[#L" not in step
        assert "http" not in step
        assert "口令" not in step
    assert any("凑单" in step for step in deal_info["tutorial_steps"])


def test_deal_cleaner_never_poisons_degraded_jd_shortlink_as_ok_coupon():
    from infrastructure.api_governance import stamp_universal_resolution

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


def test_deal_pool_startup_recovery_idempotently_rescues_recent_misclassified_tips():
    from mobile_web.scripts.run_deal_pool_scheduler import DealRawPoolRepository

    fake_db = MagicMock()
    fake_col = MagicMock()
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


def test_jd_empty_jcommand_never_skips_piliang_or_misclassified_as_venue():
    """验证 AGENTS.md 准则三：京东 byunionid_get 即使返回空 jCommand，也严禁跳过 tkl_piliang，严禁误判为会场/纯券。"""
    from infrastructure.jd_client import JdClient
    from adapters.jd_adapter import JdAdapter

    client = JdClient(appkey_list=["fake_key"], union_id="123456")

    # 模拟 byunionid_get 成功但 jCommand 为空串
    called_endpoints = []

    def _fake_exec(endpoint, params, evaluator, **kwargs):
        called_endpoints.append(endpoint)
        if endpoint == "open_jing_union_open_promotion_byunionid_get.ashx":
            return evaluator({
                "status": 200,
                "data": {
                    "code": 200,
                    "data": {
                        "shortURL": "https://u.jd.com/myPromo",
                        "clickURL": "https://union-click.jd.com/jdc?e=1",
                        "jCommand": "",
                        "jShortCommand": "",
                    },
                },
            })
        elif endpoint == "open_gaoyongzhuanlian_tkl_piliang.ashx":
            return evaluator({
                "status": 200,
                "tao_id": "cipher_product_123",
                "result_url": "https://u.jd.com/myPromo",
                "result_tkl": "",
                "pic": "https://img.jd.com/p.jpg",
                "title": "京东测试牛奶",
            })
        return {"status": "error"}

    client._execute_with_failover = _fake_exec
    res = client.convert_universal_material("https://u.jd.com/oaTdF8d", "3110104129")

    # 必须调用了 piliang，并且成功提取了 tao_id
    assert "open_gaoyongzhuanlian_tkl_piliang.ashx" in called_endpoints
    assert res.get("tao_id") == "cipher_product_123"

    # 验证 adapter 绝不将普通短链判为 coupon_or_venue
    adapter = JdAdapter(
        settings=SimpleNamespace(
            jd_enabled=True,
            ztk_appkey="fake_key",
            ztk_appkey_list=["fake_key"],
            jd_union_id="123456",
            jd_position_id="3110104129",
            jd_app_key="",
            jd_app_secret="",
            jd_api_base="http://api.zhetaoke.com:20000/api",
        ),
        client=client,
    )
    adapter.get_goods_detail = MagicMock(return_value={
        "status": "success",
        "data": {
            "name": "京东测试牛奶",
            "price": 65.8,
            "goods_id": "cipher_product_123",
            "goods_sign": "sign_123",
        },
    })
    conv_out = adapter.resolve_and_convert_universal("https://u.jd.com/oaTdF8d")
    assert conv_out["status"] == "success"
    assert conv_out["resolution_kind"] == "product"
    assert conv_out["product"]["name"] == "京东测试牛奶"



def test_deal_pool_parse_publish_time_epoch_china_tz_consistency():
    """验证东八区无时区字符串与显式 UTC Z 字符串解析结果严格一致，消除容器 UTC 8 小时漂移。"""
    from mobile_web.scripts.run_deal_pool_scheduler import parse_publish_time_epoch

    ts_cn = parse_publish_time_epoch("2026-11-12 14:30:00")
    ts_utc = parse_publish_time_epoch("2026-11-12T06:30:00Z")
    ts_iso_offset = parse_publish_time_epoch("2026-11-12T14:30:00+08:00")
    assert ts_cn is not None
    assert ts_cn == ts_utc == ts_iso_offset


def test_xianbao_detail_loop_circuit_breaks_on_429_and_caps_attempts(monkeypatch):
    """验证详情抓取遇到 429/502 立即熔断不再硬撞整页，且遇到 404 也严格按尝试次数封顶在 max_details。"""
    from mobile_web.scripts import test_deal_sites_scraper as scraper

    monkeypatch.setattr(scraper.time, "sleep", lambda _: None)

    li_items = "".join(
        f'<li class="article-list"><a href="/haodan/{1000 + i}.html" data-catename="京东" data-price="9.9">'
        f"测试线报商品 #{i}</a></li>"
        for i in range(25)
    )
    list_html = f"<ul>{li_items}</ul>"

    calls_429 = []

    def fake_get_429(url, headers=None, timeout=10.0):
        calls_429.append(url)
        resp = MagicMock()
        if url.endswith("haodan-hot.html"):
            resp.status_code = 200
            resp.text = list_html
        elif len(calls_429) == 2:
            resp.status_code = 200
            resp.text = '<div class="article-content"><a href="https://u.jd.com/abc123">直达</a></div><div id="foot"></div>'
        else:
            resp.status_code = 429
            resp.text = "Too Many Requests"
        return resp

    sess_429 = MagicMock()
    sess_429.get.side_effect = fake_get_429

    res_429 = scraper.fetch_xianbao_fun_deals(
        channel="haodan_hot",
        fetch_detail=True,
        max_details=15,
        session=sess_429,
    )
    assert len(calls_429) == 3
    assert res_429["count"] == 25
    assert res_429["detail_fetched"] == 1
    assert len(res_429["detail_errors"]) == 1
    assert res_429["detail_errors"][0]["http_status"] == 429

    calls_404 = []

    def fake_get_404(url, headers=None, timeout=10.0):
        calls_404.append(url)
        resp = MagicMock()
        if url.endswith("haodan-hot.html"):
            resp.status_code = 200
            resp.text = list_html
        else:
            resp.status_code = 404
            resp.text = "Not Found"
        return resp

    sess_404 = MagicMock()
    sess_404.get.side_effect = fake_get_404

    res_404 = scraper.fetch_xianbao_fun_deals(
        channel="haodan_hot",
        fetch_detail=True,
        max_details=4,
        session=sess_404,
    )
    assert len(calls_404) == 1 + 4
    assert res_404["detail_fetched"] == 0
    assert len(res_404["detail_errors"]) == 4


def test_run_fetch_stage_short_circuits_same_site_after_rate_limit(monkeypatch, tmp_path):
    """验证同源站点在单轮内触发 429 后，后续同站点频道直接 cooldown_skipped 冷却跳过，且错误路径按频道聚合。"""
    from mobile_web.scripts import run_deal_pool_scheduler as dps
    from mobile_web.services import deal_pipeline as dp_mod
    from mobile_web.services.deal_channel_config import DealChannelConfig

    monkeypatch.setattr(dp_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(dp_mod, "STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(dp_mod, "LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setattr(dp_mod, "OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"))
    monkeypatch.setattr(dp_mod, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"))

    xianbao_calls = []

    def fake_xianbao(**kwargs):
        xianbao_calls.append(kwargs.get("channel"))
        return {
            "status": "success",
            "http_status": 200,
            "request_url": "https://new.xianbao.fun/test",
            "raw_list_count": 2,
            "count": 1,
            "detail_fetched": 0,
            "detail_errors": [
                {"item_id": "1", "error_url": "https://new.xianbao.fun/1.html", "http_status": 429, "error_type": "HTTP_429", "error": "429"},
                {"item_id": "2", "error_url": "https://new.xianbao.fun/2.html", "http_status": 429, "error_type": "HTTP_429", "error": "429"},
            ],
            "records": [{"source_site": "xianbao_fun", "source_item_id": "1", "has_full_detail": False}],
        }

    ok_stub = lambda **kwargs: {
        "status": "success",
        "http_status": 200,
        "request_url": "https://example.com/ok",
        "raw_list_count": 1,
        "count": 1,
        "detail_fetched": 1,
        "detail_errors": [],
        "records": [{"source_site": "tehuicaicai", "source_item_id": "h1", "has_full_detail": True, "extracted_links": [{"url": "https://u.jd.com/x"}]}],
    }

    monkeypatch.setattr(dps, "fetch_xianbao_fun_deals", fake_xianbao)
    monkeypatch.setattr(dps, "fetch_tehuicaicai_deals", ok_stub)

    test_channels = [
        DealChannelConfig(
            name="线报酷_频道A",
            site_key="xianbao_fun",
            default_url="https://new.xianbao.fun/a/",
            fetcher_name="fetch_xianbao_fun_deals",
            fetcher_kwargs={"channel": "a", "fetch_detail": True},
        ),
        DealChannelConfig(
            name="线报酷_频道B",
            site_key="xianbao_fun",
            default_url="https://new.xianbao.fun/b/",
            fetcher_name="fetch_xianbao_fun_deals",
            fetcher_kwargs={"channel": "b", "fetch_detail": True},
        ),
        DealChannelConfig(
            name="特惠采采_测试",
            site_key="tehuicaicai",
            default_url="https://xhhcds.cn/",
            fetcher_name="fetch_tehuicaicai_deals",
            fetcher_kwargs={"channel": "home", "fetch_detail": True},
        ),
    ]

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 1, "updated": 0}
    repo.generate_pool_summary.return_value = {"total_records": 1}

    out = dps.run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=1,
        channels=test_channels,
        channel_last_run={},
    )
    assert len(xianbao_calls) == 1
    xb_tasks = [t for t in out["tasks"] if t["site_key"] == "xianbao_fun"]
    assert len(xb_tasks) == 2
    assert sum(1 for t in xb_tasks if t["status"] == "cooldown_skipped") == 1
    assert len(out["error_paths"]) == 1
    assert "共 2 条详情异常" in out["error_paths"][0]["error_message"]


def test_deal_ai1_scoring_and_timeline_upgrade_conditions():
    from mobile_web.scripts.run_deal_pool_scheduler import DealPoolCleaner

    # 1. 结构化时间线条目生成
    raw_doc = {
        "source_item_id": "tip_998",
        "source_site": "zhetaoke",
        "source_channel": "群3",
        "publish_time": "2026-11-18 01:20:00",
        "publish_ts": 1794936000.0,
        "title": "爆料抽纸 9.9包邮",
        "tutorial_text": "拍1件领券立减到手9.9元",
    }
    ai1_res = {
        "deal_price": 9.9,
        "tutorial_score": 85,
        "combined_raw_text": "拍1件领券立减到手9.9元",
    }
    entry = DealPoolCleaner._make_timeline_entry(raw_doc, ai1_res=ai1_res)
    assert entry["tip_id"] == "tip_998"
    assert entry["source_site"] == "zhetaoke"
    assert entry["publish_ts"] == 1794936000.0
    assert entry["deal_price"] == 9.9
    assert entry["tutorial_score"] == 85
    assert "9.9元" in entry["copywriting"]

    # 2. 教程更优升级（库内商品教程分仅 60，新线报 AI-1 评出 85 分高质量教程）
    ex_poor = {
        "platform": "tb",
        "product_id": "111",
        "tutorial_score": 60,
        "quality_score": 60,
        "deal_price": 9.9,
    }
    should_up, reason = DealPoolCleaner._should_upgrade_existing_deal(ex_poor, ai1_res, raw_doc)
    assert should_up is True
    assert "教程补全升级" in reason

    # 3. 价格突破升级（库内商品虽有 90 分教程，但新线报价格从 50.0 破低到 38.0）
    ex_high_score = {
        "platform": "jd",
        "product_id": "222",
        "tutorial_score": 90,
        "quality_score": 90,
        "deal_price": 50.0,
    }
    ai1_cheap = {
        "deal_price": 38.0,
        "tutorial_score": 80,
    }
    should_up2, reason2 = DealPoolCleaner._should_upgrade_existing_deal(ex_high_score, ai1_cheap, raw_doc)
    assert should_up2 is True
    assert "价格择优升级" in reason2

    # 4. 普通重复（库内教程完整，且价格相同、新分未显著超越 -> 安全早停，仅记录时间线）
    ai1_dup = {
        "deal_price": 50.0,
        "tutorial_score": 75,
    }
    should_up3, _ = DealPoolCleaner._should_upgrade_existing_deal(ex_high_score, ai1_dup, raw_doc)
    assert should_up3 is False


def test_upsert_cleaned_products_honors_lower_price_improvement():
    import datetime
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from mobile_web.scripts.run_deal_pool_scheduler import upsert_cleaned_products_to_formal_table

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    stored_doc = {
        "platform": "jd",
        "product_id": "10099",
        "name": "某品牌耳机",
        "price": 299.0,
        "updated_at": now - datetime.timedelta(hours=1),
        "deal_info": {
            "is_deal_tip": True,
            "quality_score": 95,
            "deal_price": 199.0,
            "updated_at": now - datetime.timedelta(hours=1),
        },
    }
    captured_ops = []
    formal_col = MagicMock()
    formal_col.find.return_value = [stored_doc]
    formal_col.bulk_write.side_effect = lambda ops, ordered=False: (
        captured_ops.extend(ops) or SimpleNamespace(upserted_count=0, modified_count=1, matched_count=1)
    )

    # 新线报质量分 88 (低于库内 95)，但价格从 199 破低到 149 (降低超 2%) -> 必须放行更新 deal_price 而不被高分拦截！
    with patch("infrastructure.search_meili.is_meili_available", return_value=False):
        res = upsert_cleaned_products_to_formal_table(
            formal_col,
            [
                {
                    "platform": "jd",
                    "product_id": "10099",
                    "name": "某品牌耳机",
                    "price": 299.0,
                    "deal_info": {
                        "is_deal_tip": True,
                        "quality_score": 88,
                        "deal_price": 149.0,
                        "tutorial_steps": ["1. 领券", "2. 下单"],
                    },
                }
            ],
        )
    assert res["protected_high_score"] == 0
    assert len(captured_ops) == 1
    set_fields = captured_ops[0]._doc["$set"]
    assert set_fields["deal_info.deal_price"] == 149.0
    assert set_fields["deal_info.tutorial_steps"] == ["1. 领券", "2. 下单"]
    assert "$push" in captured_ops[0]._doc
    assert "deal_info.tips_timeline" in captured_ops[0]._doc["$push"]
    tl_pushed = captured_ops[0]._doc["$push"]["deal_info.tips_timeline"]["$each"]
    assert len(tl_pushed) == 1
    assert tl_pushed[0]["deal_price"] == 149.0


def test_commit_batch_results_records_supplementary_copies():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from mobile_web.scripts.run_deal_pool_scheduler import _commit_batch_results_to_db

    raw_col = MagicMock()
    raw_col.bulk_write.return_value = SimpleNamespace(modified_count=1)
    formal_col = MagicMock()
    captured_formal_ops = []
    formal_col.bulk_write.side_effect = lambda ops, ordered=False: (
        captured_formal_ops.extend(ops) or SimpleNamespace(upserted_count=0, modified_count=1)
    )

    early_results = [
        {
            "status": "early_terminated",
            "source_site": "xianbao_fun",
            "source_item_id": "xb_dup_1",
            "platform": "tb",
            "product_id": "67890",
            "error_msg": "已入库早停",
            "resonance_update": {
                "platform": "tb",
                "product_id": "67890",
                "seen_count": 1,
                "source_site": "xianbao_fun",
                "seen_channels": ["ch1"],
                "copywriting": "群友最新实操作业：拍2件叠加88vip再减5元，凑单某某可退！",
                "timeline_entry": {
                    "tip_id": "xb_dup_1",
                    "source_site": "xianbao_fun",
                    "source_channel": "ch1",
                    "publish_time": "2026-11-18 01:25:00",
                    "publish_ts": 1794936300.0,
                    "title": "群友爆料作业",
                    "copywriting": "群友最新实操作业：拍2件叠加88vip再减5元",
                    "deal_price": 45.0,
                    "tutorial_score": 80,
                },
            },
        }
    ]

    commit_res = _commit_batch_results_to_db(
        early_results,
        raw_col=raw_col,
        formal_col=formal_col,
        cycle_num=1,
    )
    assert len(captured_formal_ops) == 1
    upd_op = captured_formal_ops[0]._doc
    assert "$addToSet" in upd_op
    assert "deal_info.supplementary_copies" in upd_op["$addToSet"]
    copies = upd_op["$addToSet"]["deal_info.supplementary_copies"]["$each"]
    assert len(copies) == 1
    assert "群友最新实操作业" in copies[0]
    assert "$push" in upd_op
    assert "deal_info.tips_timeline" in upd_op["$push"]
    tl_ops = upd_op["$push"]["deal_info.tips_timeline"]["$each"]
    assert len(tl_ops) == 1
    assert tl_ops[0]["tip_id"] == "xb_dup_1"
    assert tl_ops[0]["deal_price"] == 45.0


def test_fetch_guangdiu_filters_vipshop(monkeypatch):
    from mobile_web.scripts.test_deal_sites_scraper import fetch_guangdiu_deals
    import requests

    html_content = """
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=101">天猫高质商品</a>
        <a class="rightmallname">天猫</a>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=102">唯品会特卖包包</a>
        <a class="rightmallname">唯品会</a>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=103">京东自营好物</a>
        <a class="rightmallname">京东</a>
    </div>
    """

    class FakeResponse:
        status_code = 200
        text = html_content

    fake_session = MagicMock()
    fake_session.get.return_value = FakeResponse()

    res = fetch_guangdiu_deals(
        channel="latest",
        fetch_detail=False,
        session=fake_session,
    )
    records = res["records"]
    assert len(records) == 2
    item_ids = [r["source_item_id"] for r in records]
    assert "101" in item_ids
    assert "103" in item_ids
    assert "102" not in item_ids
    malls = [r["mall_name"] for r in records]
    assert "唯品会" not in malls


def test_fetch_guangdiu_rank_and_hots_parser():
    from mobile_web.scripts.test_deal_sites_scraper import fetch_guangdiu_deals
    from unittest.mock import MagicMock

    rank_html = """
    <div class="hourrankitem">
        <a class="clickmask" href="./detail.php?id=801" target="_blank"></a>
        <div class="hourranknum"><div class="hourranknumtext">No.1</div></div>
        <a class="hourrankimgdiv" href="./detail.php?id=801" target="_blank">
            <img class="hourrankimg" src="https://img.guangdiu.com/801.jpg" alt="京东生鲜 柿饼 16.6元" />
        </a>
        <div class="hourrankinfo">
            <div class="hourrankmallandtitle">
                <a class="hourranktitle" href="./detail.php?id=801" target="_blank">京鲜生 广西流心柿饼 2斤 <br/><span class="emphricepart">16.6元</span></a>
            </div>
            <div class="clear"></div>
            <div class="hourrankfromsite">京东&nbsp;·&nbsp;水果源头直发京东自营专区</div>
        </div>
    </div>
    <div class="clear"></div>
    <div class="hourrankitem">
        <a class="clickmask" href="./detail.php?id=802" target="_blank"></a>
        <div class="hourranknum"><div class="hourranknumtext">No.2</div></div>
        <div class="hourrankinfo">
            <div class="hourrankmallandtitle">
                <a class="hourranktitle" href="./detail.php?id=802" target="_blank">唯品会 特卖女装 <span class="emphricepart">99元</span></a>
            </div>
            <div class="clear"></div>
            <div class="hourrankfromsite">唯品会&nbsp;·&nbsp;自营特卖</div>
        </div>
    </div>
    """

    go_js = """var _xdk9 = 'https%3A%2F%2Fitem.jd.com%2F10001.html';"""

    def fake_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "go.php" in url:
            resp.text = "var target = 'https://union-click.jd.com/jdc?e=mock_rank';"
        elif "rank.php" in url:
            resp.text = rank_html
        elif "hots.php" in url:
            resp.text = """
            <div class="gooditem">
                <a class="goodname" href="detail.php?id=901">热水壶 <span class="emphricepart">39元</span></a>
                <a class="rightmallname">天猫</a>
                <div class="abstractcontent">满100减60优惠券</div>
            </div>
            """
        else:
            resp.text = ""
        return resp

    session = MagicMock()
    session.get.side_effect = fake_get

    # 1. 验证 rank.php
    res_rank = fetch_guangdiu_deals(channel="rank", fetch_detail=False, session=session)
    assert res_rank["status"] == "success"
    records_rank = res_rank["records"]
    assert len(records_rank) == 1  # 唯品会被过滤，保留京东
    rec1 = records_rank[0]
    assert rec1["source_item_id"] == "801"
    assert rec1["mall_name"] == "京东"
    assert "京鲜生 广西流心柿饼 2斤" in rec1["title"]
    assert rec1["price_text"] == "16.6元"
    assert "https://union-click.jd.com/jdc?e=mock_rank" in [lk["url"] for lk in rec1["extracted_links"]]

    # 2. 验证 hots.php
    res_hots = fetch_guangdiu_deals(channel="hots", fetch_detail=False, session=session)
    assert res_hots["status"] == "success"
    rec_hot = res_hots["records"][0]
    assert rec_hot["source_item_id"] == "901"
    assert rec_hot["mall_name"] == "天猫"
    assert "【逛丢热门】" in rec_hot["tutorial_text"]
    assert "满100减60优惠券" in rec_hot["tutorial_text"]


def test_fetch_guangdiu_captcha_circuit_breaker(monkeypatch):
    from mobile_web.scripts.test_deal_sites_scraper import fetch_guangdiu_deals
    from unittest.mock import MagicMock

    # 滑块求解器由用例显式注入失败结果：它内部会真起浏览器访问真实站点，
    # 既不确定也不该出现在离线单测里（不能靠"真实站点当下是否放行"来决定断言）。
    # 同一模块在 `mobile_web.services.*` 与 `services.*` 两种导入路径下各有一份实例，都要打。
    for module_name in ("mobile_web.services.deal_fetchers", "services.deal_fetchers"):
        monkeypatch.setattr(
            f"{module_name}._solve_guangdiu_slider_playwright",
            lambda *a, **k: (False, "", "用例注入：自动通过失败"),
        )

    list_html = """
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=701">好价抽纸 9.9元</a>
        <a class="rightmallname">京东</a>
        <div class="abstractcontent">领券立减20元</div>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=702">纯牛奶 49元</a>
        <a class="rightmallname">天猫</a>
        <div class="abstractcontent">买一送一</div>
    </div>
    """

    captcha_html = """
    <!DOCTYPE html>
    <html>
    <head><title>安全验证 - 逛丢</title></head>
    <body>
        <div class="verify-wrap">
            <p class="verify-title">请完成安全验证</p>
            <div class="slide-track" id="slidetrack"></div>
        </div>
    </body>
    </html>
    """

    detail_called = []

    def fake_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "go.php" in url:
            resp.text = "var u = 'https://union-click.jd.com/jdc?e=ok';"
        elif "detail.php" in url:
            detail_called.append(url)
            resp.text = captcha_html  # 模拟详情页触发滑块验证码
        else:
            resp.text = list_html
        return resp

    session = MagicMock()
    session.get.side_effect = fake_get

    # 验证“宁缺毋滥”铁律：当详情页遇到验证码未能拿到爆料教程时，系统主动断路保护 IP，且绝不入库空壳商品
    res = fetch_guangdiu_deals(channel="latest", fetch_detail=True, max_details=5, session=session)
    # 首次触发验证码后，detail_circuit_broken 触发，后续 702 不再发起 detail 请求
    assert len(detail_called) == 1
    assert "701" in detail_called[0]
    # 两个商品均未获得实质性详情教程，宁缺毋滥，全部跳过等待下一轮冷却后重试，records 为空
    assert len(res["records"]) == 0
    assert any(err.get("error_type") == "CaptchaChallengeDetected" for err in res["detail_errors"])

    # 验证正常成功获取详情教程时的入库逻辑
    normal_detail_html = """
    <html><body>
        <div id="dabstract">
            购买方案：1 店铺 京东自营，面价39元；2 领券 <a href="to.php?u=https%3A%2F%2Fitem.jd.com%2F701.html">直达商品</a>
        </div>
        <a class="dgotobutton"></a>
    </body></html>
    """
    def fake_get_ok(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "go.php" in url:
            resp.text = "var u = 'https://union-click.jd.com/jdc?e=ok';"
        elif "detail.php" in url:
            resp.text = normal_detail_html
        else:
            resp.text = list_html
        return resp

    session_ok = MagicMock()
    session_ok.get.side_effect = fake_get_ok
    res_ok = fetch_guangdiu_deals(channel="latest", fetch_detail=True, max_details=5, session=session_ok)
    assert res_ok["status"] == "success"
    assert len(res_ok["records"]) == 2
    rec = res_ok["records"][0]
    assert rec["has_full_detail"] is True
    assert "购买方案" in rec["tutorial_text"]
    assert any("union-click.jd.com" in lk["url"] for lk in rec["extracted_links"])


def test_guangdiu_default_cookie_file_wiring_and_persistence(tmp_path):
    """验证逛丢 Cookie 持久化链路闭环：默认路径规范、持久化序列化及 Mock 防御。"""
    import inspect
    import json
    import os
    import requests
    from mobile_web.services.deal_fetchers import (
        DEFAULT_GUANGDIU_COOKIE_FILE,
        _load_guangdiu_cookies,
        _save_guangdiu_cookies,
        fetch_guangdiu_deals,
    )

    # 1. 验证默认 Cookie 路径存在且为规范绝对路径
    assert DEFAULT_GUANGDIU_COOKIE_FILE.endswith("guangdiu_cookies.json")
    assert os.path.isabs(DEFAULT_GUANGDIU_COOKIE_FILE)

    # 2. 验证 fetch_guangdiu_deals 默认参数已接入 DEFAULT_GUANGDIU_COOKIE_FILE
    sig = inspect.signature(fetch_guangdiu_deals)
    assert sig.parameters["cookie_file"].default == DEFAULT_GUANGDIU_COOKIE_FILE

    # 3. 验证 _save_guangdiu_cookies 与 _load_guangdiu_cookies 存取闭环
    test_cookie_file = str(tmp_path / "test_gd_cookies.json")
    sess = requests.Session()
    sess.cookies.set("gdhuman", "test_val_12345", domain="guangdiu.com", path="/")
    sess.cookies.set("PHPSESSID", "test_sess_67890", domain="guangdiu.com", path="/")

    _save_guangdiu_cookies(sess, test_cookie_file)
    assert os.path.exists(test_cookie_file)

    with open(test_cookie_file, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    cookie_dict = {c["name"]: c["value"] for c in saved_data}
    assert cookie_dict.get("gdhuman") == "test_val_12345"
    assert cookie_dict.get("PHPSESSID") == "test_sess_67890"

    # 新 Session 加载验证
    sess2 = requests.Session()
    assert "gdhuman" not in sess2.cookies
    loaded = _load_guangdiu_cookies(sess2, test_cookie_file)
    assert loaded is True
    assert sess2.cookies.get("gdhuman") == "test_val_12345"
    assert sess2.cookies.get("PHPSESSID") == "test_sess_67890"

    # 4. 验证 Mock session 防御：传入 MagicMock 不会写坏文件
    mock_sess = MagicMock()
    _save_guangdiu_cookies(mock_sess, test_cookie_file)
    # 文件内容保持原样
    with open(test_cookie_file, "r", encoding="utf-8") as f:
        after_mock_data = json.load(f)
    assert len(after_mock_data) == 2



