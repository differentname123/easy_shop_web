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
from services.deal_cleaning.workers import MaintenanceWorker
from services.deal_pipeline import run_fetch_stage
from services.deal_pool_cleaner import DealPoolCleaner
from services.deal_sources.base import BaseDealSource, ChannelConfig, FetchContext, FetchResult


def test_convert_unit_batch_db_dedup_and_no_same_batch_early_kill(tmp_path, monkeypatch):
    cleaner = DealPoolCleaner(ai_batch_size=4, ai_max_workers=1, convert_max_workers=1)
    cleaner.adapters["jd"] = SimpleNamespace(
        resolve_and_convert_universal=lambda inp: {
            "status": "success",
            "goods_id": "NEW_PROD_1" if "new" in inp else "EXISTING_PROD_1",
            "goods_sign": "SIG_NEW" if "new" in inp else "SIG_EX",
            "final_link": "https://u.jd.com/out1" if "new" in inp else "https://u.jd.com/out_ex",
            "promotion_token": "",
            "product": {
                "platform": "jd",
                "product_id": "NEW_PROD_1" if "new" in inp else "EXISTING_PROD_1",
                "name": "测试商品",
                "price": 50.0,
            },
        }
    )

    find_calls = []

    class _FormalCol:
        def find(self, query, projection=None):
            find_calls.append((query, projection))
            return [
                {
                    "platform": "jd",
                    "product_id": "EXISTING_PROD_1",
                    "goods_sign": "SIG_EX",
                    "name": "测试商品",
                    "price": 60.0,
                    "deal_source": "guangdiu",
                    "deal_info": {
                        "source_site": "guangdiu",
                        "quality_score": 80,
                        "deal_price": 50.0,
                        "tutorial_score": 80,
                    },
                }
            ]

    def _make_convert_doc(mid: str, url: str, deal_price: float):
        return {
            "source_site": "guangdiu",
            "source_channel": "逛丢_首页实时流",
            "source_item_id": mid,
            "platform": "jd",
            "title": f"线报 {mid}",
            "ai_stage1_result": {
                "main_tip_id": mid,
                "is_standalone_deal": True,
                "merged_tip_ids": [mid],
                "deal_price": deal_price,
                "tutorial_score": 70,
                "combined_raw_text": "下单 [#L1]",
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": url, "platform": "jd", "text_role_hint": "main_candidate"}
                ],
            },
        }

    # 1. 同批两条命中未入库新商品 NEW_PROD_1 -> 绝不提前批内误杀，两条都推进 AI-2；
    # 2. 已在库商品 EXISTING_PROD_1：同价(50.0)触发 post_stage3_db 早停，显著更低价(40.0)触发升级推进 AI-2；
    # 3. 整批仅发起 1 次按 pid_keys 的精确批量查库。
    docs = [
        _make_convert_doc("tip_new_a", "https://u.jd.com/new_a", 30.0),
        _make_convert_doc("tip_new_b", "https://u.jd.com/new_b", 29.0),
        _make_convert_doc("tip_ex_dup", "https://u.jd.com/ex_1", 50.0),
        _make_convert_doc("tip_ex_upgrade", "https://u.jd.com/ex_2", 40.0),
    ]
    outcomes, _ = cleaner.process_convert_unit(docs, formal_col=_FormalCol(), quiet_sub_logs=True)
    out_by_id = {o["source_item_id"]: o for o in outcomes}
    assert out_by_id["tip_new_a"]["stage_outcome"] == "advance_to_ai2"
    assert out_by_id["tip_new_b"]["stage_outcome"] == "advance_to_ai2"
    assert out_by_id["tip_ex_dup"]["status"] == "early_terminated"
    assert out_by_id["tip_ex_upgrade"]["stage_outcome"] == "advance_to_ai2"
    assert len(find_calls) == 1

    # run_fetch_stage 全量巡视与 MaintenanceWorker 到期清扫正常执行
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
    )
    repo.expire_missing_from_full_sweep.assert_called_once()

    repo_m = MagicMock()
    repo_m.reclaim_expired_leases.return_value = 0
    repo_m.sweep_expired_and_max_retry.return_value = {"expired_swept": 0, "max_retry_swept": 0}
    repo_m.expire_stale_formal_deals.return_value = 1
    repo_m.generate_pool_summary.return_value = {}
    stop_ev = threading.Event()
    mw = MaintenanceWorker(repo=repo_m, poll_interval=5.0, stop_event=stop_ev)
    mw.run_once(tick=30)
    repo_m.expire_stale_formal_deals.assert_called_once()


def test_daemon_mode_preserves_rich_product_fields_while_keeping_ai2_prompt_lean():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)

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


def test_generate_pool_summary_single_facet_aggregation_and_empty_edge():
    from models.deal_constants import CLEAN_STAGE_NEED_AI1, CLEAN_STAGE_NEED_AI2, CLEAN_STAGE_NEED_CONVERT

    _MISSING = object()

    def _get_field(doc, path):
        cur = doc
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return _MISSING
            cur = cur[part]
        return cur

    def _eval_expr(expr, doc):
        if isinstance(expr, str) and expr.startswith("$"):
            return _get_field(doc, expr[1:])
        if isinstance(expr, dict):
            if "$eq" in expr:
                lhs, rhs = expr["$eq"]
                return _eval_expr(lhs, doc) == _eval_expr(rhs, doc)
            if "$or" in expr:
                return any(bool(_eval_expr(sub, doc)) for sub in expr["$or"])
            if "$type" in expr:
                val = _eval_expr(expr["$type"], doc)
                return "missing" if val is _MISSING else ("null" if val is None else type(val).__name__)
            if "$cond" in expr:
                cond, t_val, f_val = expr["$cond"]
                return _eval_expr(t_val, doc) if _eval_expr(cond, doc) else _eval_expr(f_val, doc)
        return expr

    def _match_query(doc, flt):
        for k, cond in flt.items():
            val = _get_field(doc, k)
            if isinstance(cond, dict) and "$in" in cond:
                if val is _MISSING or val not in cond["$in"]:
                    return False
            elif val is _MISSING or val != cond:
                return False
        return True

    def _run_sub_pipeline(docs, stages):
        rows = list(docs)
        for st in stages:
            if "$match" in st:
                rows = [d for d in rows if _match_query(d, st["$match"])]
            elif "$group" in st:
                spec = st["$group"]
                grouped = {}
                for d in rows:
                    gval = _eval_expr(spec["_id"], d)
                    gk = None if gval is _MISSING else gval
                    acc = grouped.setdefault(gk, {"_id": gk, **{f: 0 for f in spec if f != "_id"}})
                    for fname, agg in spec.items():
                        if fname == "_id":
                            continue
                        if "$sum" in agg:
                            acc[fname] += int(_eval_expr(agg["$sum"], d) or 0)
                rows = list(grouped.values())
            elif "$sort" in st:
                for field, direction in reversed(list(st["$sort"].items())):
                    rows.sort(key=lambda r: (r.get(field) is None, r.get(field)), reverse=(direction < 0))
        return rows

    class _RawFacetCol:
        def __init__(self, docs):
            self.docs = list(docs)
            self.count_calls = 0
            self.agg_calls = 0

        def count_documents(self, flt):
            self.count_calls += 1
            raise AssertionError("generate_pool_summary must use single $facet aggregate instead of col.count_documents")

        def aggregate(self, pipeline):
            self.agg_calls += 1
            assert len(pipeline) == 1 and "$facet" in pipeline[0]
            return [{name: _run_sub_pipeline(self.docs, sub) for name, sub in pipeline[0]["$facet"].items()}]

    class _FormalCountCol:
        def __init__(self, docs):
            self.docs = list(docs)

        def count_documents(self, flt):
            return sum(1 for d in self.docs if _match_query(d, flt))

    sample_raw_docs = [
        {"source_site": "guangdiu", "platform": "jd", "has_full_detail": True, "quality_signals": {"has_unique_product_anchor": True}, "clean_status": "cleaned_success"},
        {"source_site": "guangdiu", "platform": "tb", "has_full_detail": True, "quality_signals": {"has_unique_product_anchor": True}, "clean_status": "cleaned_low_score", "clean_error_code": "LOW_SCORE"},
        {"source_site": "xianbao_fun", "platform": "jd", "has_full_detail": False, "clean_status": "merged_into_parent"},
        {"source_site": "xianbao_fun", "clean_status": "context_only"},
        {"source_site": "tehuicaicai", "platform": "pdd", "clean_status": "cleaned_failed", "clean_error_code": "OFF_PROMO"},
        {"source_site": "tehuicaicai", "clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI1},
        {"source_site": "tehuicaicai", "clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_CONVERT},
        {"source_site": "guangdiu", "clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI2},
        {"source_site": "guangdiu", "clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI2},
        {"source_site": "haodanku"},  # 缺失 clean_status，应计入 pending_clean_records
    ]
    raw_col = _RawFacetCol(sample_raw_docs)
    formal_col = _FormalCountCol([{"deal_info": {"is_deal_tip": True}}, {}])
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda n: raw_col if n == "deal_raw_tips" else formal_col))
    repo._indexes_ready = True

    summary = repo.generate_pool_summary()
    assert raw_col.count_calls == 0
    assert raw_col.agg_calls == 1
    assert summary["total_records"] == 10
    assert summary["full_detail_records"] == 2
    assert summary["unique_product_anchor_records"] == 2
    assert summary["unique_product_anchor_ratio"] == 20.0
    assert summary["cleaned_success_records"] == 1
    assert summary["cleaned_low_score_records"] == 1
    assert summary["merged_into_parent_records"] == 1
    assert summary["context_only_records"] == 1
    assert summary["cleaned_failed_records"] == 1
    assert summary["pending_clean_records"] == 5
    assert summary["pending_by_stage"] == {
        CLEAN_STAGE_NEED_AI1: 1,
        CLEAN_STAGE_NEED_CONVERT: 1,
        CLEAN_STAGE_NEED_AI2: 2,
    }
    assert summary["by_clean_error_code"] == {"LOW_SCORE": 1, "OFF_PROMO": 1}
    assert summary["by_platform"] == {"None": 6, "jd": 2, "tb": 1, "pdd": 1}
    assert summary["formal_products_total"] == 2
    assert summary["formal_deal_products_count"] == 1
    assert summary["by_source_site"][0]["_id"] == "guangdiu"

    # 空集合边界：零除保护与全零字段
    empty_raw = _RawFacetCol([])
    empty_formal = _FormalCountCol([])
    empty_repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda n: empty_raw if n == "deal_raw_tips" else empty_formal))
    empty_repo._indexes_ready = True
    empty_summary = empty_repo.generate_pool_summary()
    assert empty_raw.agg_calls == 1 and empty_raw.count_calls == 0
    assert empty_summary["total_records"] == 0
    assert empty_summary["unique_product_anchor_ratio"] == 0.0
    assert empty_summary["pending_clean_records"] == 0
    assert empty_summary["pending_by_stage"] == {
        CLEAN_STAGE_NEED_AI1: 0,
        CLEAN_STAGE_NEED_CONVERT: 0,
        CLEAN_STAGE_NEED_AI2: 0,
    }
    assert empty_summary["by_source_site"] == []
    assert empty_summary["by_clean_error_code"] == {}
    assert empty_summary["by_platform"] == {}
