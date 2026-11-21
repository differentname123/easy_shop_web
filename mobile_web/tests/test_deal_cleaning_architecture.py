# -*- coding: utf-8 -*-
"""Wave 3A / Phase 5.1-5.2 核心架构与正确性回归测试（全离线确定性验证）。"""
from __future__ import annotations

import datetime
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from models.deal_constants import (
    CLEAN_STAGE_DONE,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_UNSUPPORTED_PLATFORM,
    UPSTREAM_DISAPPEARED,
)
from repositories.deal_raw_pool_repo import DealRawPoolRepository, parse_publish_time_epoch
from services.deal_cleaning.cache_state import ActiveProductIndex
from services.deal_cleaning.workers import MaintenanceWorker
from services.deal_pipeline import run_fetch_stage
from services.deal_pool_cleaner import DealPoolCleaner
from services.deal_sources.base import BaseDealSource, ChannelConfig, FetchContext, FetchResult


def test_active_product_index_ttl_lru_and_invalidation_hooks(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    idx = ActiveProductIndex(ttl_seconds=12 * 3600.0, max_size=2, clock=lambda: clock["now"])

    idx.upsert(("jd", "p1"), {"official_title": "A", "quality_score": 80}, source_site="guangdiu")
    idx.upsert(("tb", "p2"), {"official_title": "B", "quality_score": 85}, source_site="tehuicaicai")
    idx.upsert(("pdd", "p3"), {"official_title": "C", "quality_score": 90}, source_site="tehuicaicai")

    # LRU 上限淘汰最早写入的 p1
    assert idx.get(("jd", "p1")) is None
    assert idx.get(("tb", "p2")) is not None
    assert idx.get(("pdd", "p3")) is not None

    # TTL 超时自动失效
    clock["now"] += 12 * 3600.0 + 1.0
    assert idx.get(("tb", "p2")) is None
    assert ("pdd", "p3") not in idx

    # 单站点精准失效与单项失效
    idx.upsert(("jd", "a1"), {"official_title": "X"}, source_site="haodanku")
    idx.upsert(("jd", "a2"), {"official_title": "Y"}, source_site="tehuicaicai")
    assert idx.invalidate_site("haodanku") == 1
    assert idx.get(("jd", "a1")) is None
    assert idx.get(("jd", "a2")) is not None

    # run_fetch_stage 全量巡视失效后联动 invalidate_site
    class _SweepSource(BaseDealSource):
        site_key = "tehuicaicai"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                status="ok",
                http_status=200,
                request_url=ctx.channel.default_url,
                elapsed_ms=1.0,
                raw_list_count=1,
                sweep_completed=True,
                records=[{
                    "source_site": "tehuicaicai",
                    "source_item_id": "t1",
                    "has_full_detail": True,
                    "tutorial_text": "领券立减20元包邮",
                    "extracted_links": [{"url": "https://u.jd.com/keep1"}],
                }],
            )

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 1, "updated": 0}
    repo.expire_missing_from_full_sweep.return_value = {"raw_expired": 0, "formal_expired": 2}
    repo.generate_pool_summary.return_value = {"total_records": 1}

    monkeypatch.setattr("services.deal_pipeline.STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"), raising=False)
    monkeypatch.setattr("services.deal_pipeline.LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"), raising=False)
    monkeypatch.setattr("services.deal_pipeline.OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"), raising=False)

    ch = ChannelConfig(
        name="特惠采采_测试",
        site_key="tehuicaicai",
        default_url="https://xhhcds.cn/",
        expire_missing_on_sweep=True,
        sleep_after=0.0,
    )
    run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        channels=[ch],
        channel_last_run={},
        source_registry={"tehuicaicai": _SweepSource()},
        active_index=idx,
    )
    assert idx.get(("jd", "a2")) is None

    # MaintenanceWorker 在第 30 次心跳到期摘标时联动 invalidate_all
    idx.upsert(("jd", "m1"), {"official_title": "M"}, source_site="guangdiu")
    repo_m = MagicMock()
    repo_m.reclaim_expired_leases.return_value = 0
    repo_m.sweep_expired_and_max_retry.return_value = {"expired_swept": 0, "max_retry_swept": 0}
    repo_m.expire_stale_formal_deals.return_value = 1
    repo_m.generate_pool_summary.return_value = {}
    stop_ev = threading.Event()
    mw = MaintenanceWorker(repo=repo_m, poll_interval=5.0, stop_event=stop_ev, active_index=idx)
    mw.run_once(tick=30)
    assert len(idx) == 0


def test_daemon_mode_preserves_rich_product_fields_while_keeping_ai2_prompt_lean():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    cleaner.adapters["jd"] = SimpleNamespace(
        resolve_and_convert_universal=lambda _inp: {
            "status": "success",
            "goods_id": "JD_RICH_1",
            "goods_sign": "SIGN_RICH_1",
            "final_link": "https://u.jd.com/rich1",
            "promotion_token": "！RICH1！",
            "product": {
                "platform": "jd",
                "platform_name": "京东",
                "product_id": "JD_RICH_1",
                "name": "维达超韧抽纸 3层130抽*24包",
                "price": 39.9,
                "original_price": 59.9,
                "activity_price": 39.9,
                "coupon_amount": 10.0,
                "mall_name": "维达京东自营旗舰店",
                "image_url": "https://img14.360buyimg.com/main.jpg",
                "carousel_images": ["https://img14.360buyimg.com/c1.jpg", "https://img14.360buyimg.com/c2.jpg"],
                "product_url": "https://item.jd.com/10001.html",
                "desc": "加厚亲肤无香精超韧系列长描述",
                "detail_tags": ["京东自营", "破损包赔"],
                "service_guarantees": ["7天无理由退货", "闪电发货"],
                "shop_ratings": {"dsr": "4.98"},
                "specs": [{"name": "规格", "value": "24包"}],
                "coupon_info": {"discount": 10},
                "raw_item": {"giant_raw": "should_not_persist"},
            },
        }
    )

    conv_res = cleaner.convert_single_target({
        "link_id": "L1",
        "clean_input": "https://u.jd.com/orig1",
        "platform": "jd",
        "text_role_hint": "main_candidate",
    })
    assert conv_res["convert_status"] == "product"

    # 模拟 StageWorker-Convert 落盘 Mongo 前剥离 adapter_res（跨阶段持久化形态）
    persisted_conv = {k: v for k, v in conv_res.items() if k != "adapter_res"}
    assert "raw_item" not in persisted_conv["official_product"]
    assert persisted_conv["official_product"]["desc"] == "加厚亲肤无香精超韧系列长描述"

    captured_ai2_inputs = []

    def _mock_ai2(stage2_inputs, quiet_sub_logs=False):
        captured_ai2_inputs.extend(stage2_inputs)
        return {
            "tip_rich_1": {
                "main_tip_id": "tip_rich_1",
                "is_valid_deal": True,
                "main_link_id": "L1",
                "deal_price": 29.9,
                "coupon_links": [],
                "tutorial_steps": ["1. 进入商品详情页领券拍下实付29.9元"],
                "quality_score": 88,
            }
        }

    cleaner.run_ai_stage2_batch = _mock_ai2
    doc_ai2 = {
        "source_site": "guangdiu",
        "source_channel": "逛丢_首页实时流",
        "source_item_id": "tip_rich_1",
        "platform": "jd",
        "title": "维达抽纸 29.9元",
        "tutorial_text": "领券下单 https://u.jd.com/orig1",
        "ai_stage1_result": {
            "main_tip_id": "tip_rich_1",
            "merged_tip_ids": ["tip_rich_1"],
            "combined_raw_text": "领券下单 [#L1]",
            "tutorial_score": 84,
            "extracted_targets": [],
        },
        "converted_links": [persisted_conv],
    }

    ai2_out, _ = cleaner.process_ai2_unit([doc_ai2], raw_col=None, quiet_sub_logs=True)
    assert len(ai2_out) == 1 and ai2_out[0]["status"] == "success"

    # LLM 输入负载精简（不膨胀）
    sent_off = captured_ai2_inputs[0]["converted_links"][0]["official_product"]
    assert "desc" not in sent_off
    assert "specs" not in sent_off
    assert "carousel_images" not in sent_off

    # 正式表落盘产物完整保留全部富字段与 tutorial_score
    pdoc = ai2_out[0]["product_doc"]
    assert pdoc["desc"] == "加厚亲肤无香精超韧系列长描述"
    assert pdoc["detail_tags"] == ["京东自营", "破损包赔"]
    assert pdoc["service_guarantees"] == ["7天无理由退货", "闪电发货"]
    assert pdoc["shop_ratings"] == {"dsr": "4.98"}
    assert pdoc["specs"] == [{"name": "规格", "value": "24包"}]
    assert pdoc["coupon_info"] == {"discount": 10}
    assert pdoc["product_url"] == "https://item.jd.com/10001.html"
    assert pdoc["carousel_images"] == ["https://img14.360buyimg.com/c1.jpg", "https://img14.360buyimg.com/c2.jpg"]
    assert pdoc["deal_info"]["tutorial_score"] == 84

    active_idx = ActiveProductIndex()
    active_idx.upsert_from_product_doc(pdoc)
    assert active_idx.get(("jd", "JD_RICH_1"))["tutorial_score"] == 84


def test_ai_semaphore_caps_concurrent_llm_requests_across_stages(tmp_path):
    p1 = tmp_path / "p1.txt"
    p2 = tmp_path / "p2.txt"
    p1.write_text("S1 PROMPT", encoding="utf-8")
    p2.write_text("S2 PROMPT", encoding="utf-8")

    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    def _slow_ai_caller(**_kwargs):
        with state_lock:
            state["active"] += 1
            state["calls"] += 1
            if state["active"] > state["max_active"]:
                state["max_active"] = state["active"]
        try:
            time.sleep(0.05)
            return {"status": "✅ 成功", "content": json.dumps([{"main_tip_id": "t1"}])}
        finally:
            with state_lock:
                state["active"] -= 1

    sem = threading.BoundedSemaphore(2)
    cleaner = DealPoolCleaner(
        ai_batch_size=1,
        ai2_batch_size=1,
        ai_max_workers=4,
        convert_max_workers=1,
        stage1_prompt_path=str(p1),
        stage2_prompt_path=str(p2),
        ai_semaphore=sem,
        ai_caller=_slow_ai_caller,
    )

    chunks_s1 = [{"tip_id": f"a{i}"} for i in range(4)]
    chunks_s2 = [{"main_tip_id": f"b{i}"} for i in range(4)]

    t1 = threading.Thread(
        target=cleaner.run_ai_stage1_batch,
        args=(chunks_s1, {f"a{i}": {} for i in range(4)}),
        kwargs={"quiet_sub_logs": True},
    )
    t2 = threading.Thread(
        target=cleaner.run_ai_stage2_batch,
        args=(chunks_s2,),
        kwargs={"quiet_sub_logs": True},
    )
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert state["calls"] == 8
    assert state["max_active"] <= 2


def test_taobao_absorbed_coupon_fail_closed_when_goods_and_link_mismatch():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True
    cleaner.adapters["taobao"] = SimpleNamespace(
        resolve_and_convert_universal=lambda inp: (
            {
                "status": "success",
                "goods_id": "TB_OTHER_ITEM",
                "goods_sign": "TB_OTHER_ITEM",
                "final_link": "https://s.click.taobao.com/other_item",
                "promotion_token": "￥OTHER￥",
                "product": {"platform": "taobao", "product_id": "TB_OTHER_ITEM", "name": "另一款商品", "price": 19.0},
            }
            if "L_DIFF" in inp
            else {
                "status": "success",
                "goods_id": "TB_MAIN_ITEM",
                "goods_sign": "TB_MAIN_ITEM",
                "final_link": "https://s.click.taobao.com/main_item",
                "promotion_token": "￥MAIN￥",
                "product": {"platform": "taobao", "product_id": "TB_MAIN_ITEM", "name": "主商品", "price": 29.0},
            }
        )
    )
    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        "tip_tb_mismatch": {
            "main_tip_id": "tip_tb_mismatch",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_tb_mismatch"],
            "combined_raw_text": "领券 [#L1] 拍主商品 [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "L_DIFF", "clean_input": "https://s.click.taobao.com/L_DIFF", "platform": "taobao", "text_role_hint": "coupon_candidate"},
                {"link_id": "L2", "raw_span": "L_MAIN", "clean_input": "https://s.click.taobao.com/L_MAIN", "platform": "taobao", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        }
    }
    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        "tip_tb_mismatch": {
            "main_tip_id": "tip_tb_mismatch",
            "is_valid_deal": True,
            "main_link_id": "L2",
            "deal_price": 19.0,
            "coupon_links": [],
            "tutorial_steps": ["1. 下单"],
            "quality_score": 85,
        }
    }
    raw_col = MagicMock()
    raw_col.find.return_value = []
    res = cleaner.clean_batch_records(
        [{"source_site": "guangdiu", "source_channel": "latest", "source_item_id": "tip_tb_mismatch", "platform": "taobao", "title": "测试"}],
        raw_col,
        shared_active_products={},
        quiet_sub_logs=True,
    )
    assert res[0]["status"] == "failed"
    assert res[0]["error_code"] == ERR_AI2_COUPON_FAILED_OR_DROPPED


def test_channel1_ok_coupon_requires_positive_resolution_kind_and_external_shortlink_permanent():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1, enable_live_unwrap=False)
    cleaner.adapters["jd"] = SimpleNamespace(
        resolve_and_convert_universal=lambda _inp: {
            "status": "success",
            "final_link": "https://u.jd.com/newPromoNoKind",
            "promotion_token": "！NEW！",
            "product": None,
        }
    )

    # 1. 无 resolution_kind=='coupon_or_venue' 正向声明时，严禁放行 OK_COUPON
    out_unverified = cleaner.convert_single_target({
        "link_id": "L1",
        "clean_input": "https://u.jd.com/origLink",
        "platform": "jd",
        "text_role_hint": "coupon_candidate",
    })
    assert out_unverified["convert_status"] == "failed"

    # 2. 第三方短链解包未命中公共领券白名单 -> 永久 ERR_UNSUPPORTED_PLATFORM，驱逐暂态缓存后仍驻留
    out_ext = cleaner.convert_single_target({
        "link_id": "L2",
        "clean_input": "https://unknown.short.test/abc123",
        "platform": "unknown",
        "text_role_hint": "main_candidate",
    })
    assert out_ext["convert_status"] == "failed"
    assert out_ext["error_code"] == ERR_UNSUPPORTED_PLATFORM

    s3_out, _ = cleaner.process_convert_unit(
        [{
            "source_site": "guangdiu",
            "source_channel": "latest",
            "source_item_id": "tip_ext_perm",
            "platform": "unknown",
            "title": "外链测试",
            "clean_retry_count": 0,
            "ai_stage1_result": {
                "is_standalone_deal": True,
                "extracted_targets": [
                    {"link_id": "L2", "clean_input": "https://unknown.short.test/abc123", "platform": "unknown", "text_role_hint": "main_candidate"}
                ],
            },
        }],
        shared_active_products={},
        quiet_sub_logs=True,
    )
    assert s3_out[0]["status"] == "failed"
    assert s3_out[0]["error_code"] == ERR_UNSUPPORTED_PLATFORM
    cleaner._evict_transient_failed_cache()
    assert "jd:https://unknown.short.test/abc123" in cleaner.anchor_cache


def test_save_batch_mixed_site_disappeared_reset_and_sweeps_set_clean_stage_done():
    upd_filters = []
    docs = [
        {"_id": "1", "source_site": "tehuicaicai", "source_item_id": "t1", "clean_status": "pending", "clean_stage": "need_convert", "stage_lease_owner": "w1", "stage_lease_until_ts": time.time() + 100, "last_seen_at": datetime.datetime(2020, 1, 1), "clean_cutoff_ts": time.time() - 10},
        {"_id": "2", "source_site": "haodanku", "source_item_id": "h1", "clean_status": "pending", "clean_stage": "need_ai1", "stage_lease_owner": "w2", "stage_lease_until_ts": time.time() + 100, "last_seen_at": datetime.datetime(2020, 1, 1)},
    ]

    class _Col:
        def update_many(self, flt, upd):
            upd_filters.append((flt, upd))
            return SimpleNamespace(modified_count=1)

        def bulk_write(self, ops, ordered=False):
            return SimpleNamespace(upserted_count=len(ops), modified_count=0)

    col = _Col()
    formal_col = _Col()
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda n: col if n == "deal_raw_tips" else formal_col))
    repo._indexes_ready = True

    # 1. save_batch 对混合站点各自触发 UPSTREAM_DISAPPEARED 重置（不再只看 records[0]）
    upd_filters.clear()
    repo.save_batch(
        [
            {"source_site": "tehuicaicai", "source_item_id": "t1", "title": "A", "extracted_links": [{"url": "https://u.jd.com/a"}], "has_full_detail": True},
            {"source_site": "guangdiu", "source_item_id": "g1", "title": "B", "extracted_links": [{"url": "https://u.jd.com/b"}], "has_full_detail": True},
        ],
        task_name="mixed_test",
    )
    disappeared_sites = {
        flt.get("source_site"): flt.get("source_item_id", {}).get("$in")
        for flt, _upd in upd_filters
        if flt.get("clean_error_code") == UPSTREAM_DISAPPEARED
    }
    assert disappeared_sites == {"tehuicaicai": ["t1"], "guangdiu": ["g1"]}

    # 2. expire_missing_from_full_sweep 与 sweep_expired_and_max_retry 均写 clean_stage=done 与 $unset 租约
    upd_filters.clear()
    repo.expire_missing_from_full_sweep("tehuicaicai", "特惠采采_首页精选", datetime.datetime.now())
    raw_exp_flt, raw_exp_upd = upd_filters[0]
    assert raw_exp_flt["source_site"] == "tehuicaicai"
    assert raw_exp_upd["$set"]["clean_stage"] == CLEAN_STAGE_DONE
    assert raw_exp_upd["$set"]["clean_error_code"] == UPSTREAM_DISAPPEARED
    assert "stage_lease_owner" in raw_exp_upd["$unset"]

    upd_filters.clear()
    res_sw = repo.sweep_expired_and_max_retry()
    assert res_sw["expired_swept"] >= 1
    cutoff_flt, cutoff_upd = upd_filters[0]
    assert {"clean_cutoff_ts": {"$gt": 0, "$lt": cutoff_flt["$and"][1]["clean_cutoff_ts"]["$lt"]}}
    for _flt, upd in upd_filters:
        assert upd["$set"]["clean_stage"] == CLEAN_STAGE_DONE
        assert "stage_lease_owner" in upd["$unset"]
        assert "stage_lease_until_ts" in upd["$unset"]


def test_parse_publish_time_chinese_relative_and_cleaner_close_releases_session():
    # 单一真源解析中文相对时间
    now = time.time()
    t_3min = parse_publish_time_epoch("3分钟前")
    t_today = parse_publish_time_epoch("今天 12:30")
    assert t_3min is not None and abs((now - 180.0) - t_3min) < 5.0
    assert t_today is not None

    # cleaner.close() 确定性关闭并清理共享解包 session
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    mock_sess = MagicMock()
    cleaner._shared_unwrap_session = mock_sess
    cleaner.close()
    mock_sess.close.assert_called_once()
    assert cleaner._shared_unwrap_session is None
