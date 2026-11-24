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
    CLEAN_STAGE_NEED_AI2,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI2_PRICE_INTEGRITY,
    ERR_UNSUPPORTED_PLATFORM,
    MAX_CLEAN_RETRIES,
    PROMPT_STAGE2_PATH,
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

    # LLM 输入负载精简（不膨胀、且屏蔽诱发二次扣券的 price / coupon_amount 等字段）
    sent_off = captured_ai2_inputs[0]["converted_links"][0]["official_product"]
    assert set(sent_off.keys()) == {"title", "original_price", "activity_price", "shop_name", "category", "brand"}
    assert "price" not in sent_off
    assert "coupon_amount" not in sent_off
    assert "commission_rate" not in sent_off
    assert "commission_amount" not in sent_off
    assert "sales_tip" not in sent_off
    assert "desc" not in sent_off
    assert "specs" not in sent_off
    assert "carousel_images" not in sent_off

    # 正式表落盘产物完整保留全部富字段与 tutorial_score
    pdoc = ai2_out[0]["product_doc"]
    assert pdoc["price"] == 39.9
    assert pdoc["coupon_amount"] == 10.0
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


def _make_stage2_price_test_doc(
    *,
    tip_id: str,
    raw_text: str,
    official_price: float,
    original_price: float,
    coupon_amount: float = 0.0,
    stage1_deal_price: float | None = None,
    retry_count: int = 0,
):
    return {
        "source_site": "haodanku",
        "source_channel": "好单库_测试",
        "source_item_id": tip_id,
        "platform": "jd",
        "title": raw_text.splitlines()[0],
        "tutorial_text": raw_text,
        "clean_retry_count": retry_count,
        "ai_stage1_result": {
            "main_tip_id": tip_id,
            "merged_tip_ids": [tip_id],
            "deal_price": stage1_deal_price,
            "combined_raw_text": f"{raw_text}\n商品/活动链接: [#L1]",
            "tutorial_score": 85,
            "extracted_targets": [
                {"link_id": "L1", "clean_input": "https://u.jd.com/item1", "platform": "jd", "text_role_hint": "main_candidate"}
            ],
        },
        "converted_links": [
            {
                "link_id": "L1",
                "clean_input": "https://u.jd.com/item1",
                "text_role_hint": "main_candidate",
                "convert_status": "product",
                "platform": "jd",
                "goods_id": f"GID_{tip_id}",
                "goods_sign": f"GS_{tip_id}",
                "my_promo_link": "https://u.jd.com/promo1",
                "my_promo_token": "",
                "official_product": {
                    "title": "测试商品标准品名",
                    "price": official_price,
                    "original_price": original_price,
                    "activity_price": official_price,
                    "coupon_amount": coupon_amount,
                    "shop_name": "官方旗舰店",
                    "category": "食品饮料",
                    "brand": "伊利",
                },
            }
        ],
    }


def test_ai2_price_integrity_guard_blocks_double_coupon_and_official_overwrite():
    cleaner = DealPoolCleaner(ai_batch_size=4, ai_max_workers=1, convert_max_workers=1)

    # 1. 还原 1656363：原文 到手【59.9元】，official activity_price=69.9, coupon=30.0，AI-2 错扣成 39.9 -> 拦截重试
    doc_double_sub = _make_stage2_price_test_doc(
        tip_id="1656363",
        raw_text="伊利 纯牛奶200ml*24盒*2箱\n折29.9/箱！！到手【59.9元】",
        official_price=69.9,
        original_price=99.9,
        coupon_amount=30.0,
        stage1_deal_price=59.9,
    )
    # 2. 还原 1656363 达重试上限：二次扣官方券属 100% 确定性错算，达上限后 Fail-Closed 为 failed
    doc_double_sub_max = _make_stage2_price_test_doc(
        tip_id="1656363_max",
        raw_text="伊利 纯牛奶200ml*24盒*2箱\n折29.9/箱！！到手【59.9元】",
        official_price=69.9,
        original_price=99.9,
        coupon_amount=30.0,
        stage1_deal_price=59.9,
        retry_count=MAX_CLEAN_RETRIES - 1,
    )
    # 3. 还原 14319776 / 2779234：原文 券后30.4元，official activity_price=25.39，AI-2 错抄官价 25.39 -> 拦截重试
    doc_overwrite = _make_stage2_price_test_doc(
        tip_id="14319776",
        raw_text="2026秋上册 初中必刷题\n原价33.4元 【券后30.4元】包邮",
        official_price=25.39,
        original_price=33.4,
        coupon_amount=3.0,
        stage1_deal_price=30.4,
    )
    # 4. 还原无数字价线报（stage1_deal_price=None）且已达重试上限：即使官价穿透也不误杀整条有效商品
    doc_no_price_max = _make_stage2_price_test_doc(
        tip_id="1660044_max",
        raw_text="蔬果园 修复发膜500g\n部分地区特价叠加限免优惠",
        official_price=39.9,
        original_price=39.9,
        coupon_amount=0.0,
        stage1_deal_price=None,
        retry_count=MAX_CLEAN_RETRIES - 1,
    )

    mock_returns = {
        "1656363": {"main_tip_id": "1656363", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 39.9, "quality_score": 90},
        "1656363_max": {"main_tip_id": "1656363_max", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 39.9, "quality_score": 90},
        "14319776": {"main_tip_id": "14319776", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 25.39, "quality_score": 90},
        "1660044_max": {"main_tip_id": "1660044_max", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 39.9, "quality_score": 90},
    }
    cleaner.run_ai_stage2_batch = lambda inputs, quiet_sub_logs=False: {
        it["main_tip_id"]: mock_returns[it["main_tip_id"]] for it in inputs
    }

    outcomes, _ = cleaner.process_ai2_unit(
        [doc_double_sub, doc_double_sub_max, doc_overwrite, doc_no_price_max],
        raw_col=None,
        quiet_sub_logs=True,
    )
    by_id = {o["source_item_id"]: o for o in outcomes}

    assert by_id["1656363"]["status"] == "retry_pending"
    assert by_id["1656363"]["clean_stage"] == CLEAN_STAGE_NEED_AI2
    assert by_id["1656363"]["error_code"] == ERR_AI2_PRICE_INTEGRITY
    assert by_id["1656363_max"]["status"] == "failed"
    assert by_id["1656363_max"]["error_code"] == ERR_AI2_PRICE_INTEGRITY
    assert by_id["14319776"]["status"] == "retry_pending"
    assert by_id["14319776"]["clean_stage"] == CLEAN_STAGE_NEED_AI2
    assert by_id["14319776"]["error_code"] == ERR_AI2_PRICE_INTEGRITY
    assert by_id["1660044_max"]["status"] == "success"

    # 无数字到手价若 AI-2 合规返回 deal_price=None，应正常放行入库
    doc_no_price_ok = _make_stage2_price_test_doc(
        tip_id="1660044_ok",
        raw_text="蔬果园 修复发膜500g\n部分地区特价叠加限免优惠",
        official_price=39.9,
        original_price=39.9,
    )
    cleaner.run_ai_stage2_batch = lambda inputs, quiet_sub_logs=False: {
        "1660044_ok": {"main_tip_id": "1660044_ok", "is_valid_deal": True, "main_link_id": "L1", "deal_price": None, "quality_score": 82}
    }
    ok_out, _ = cleaner.process_ai2_unit([doc_no_price_ok], raw_col=None, quiet_sub_logs=True)
    assert ok_out[0]["status"] == "success"
    assert ok_out[0]["deal_price"] is None


def test_ai2_price_integrity_guard_has_no_false_positives_on_valid_deals():
    cleaner = DealPoolCleaner(ai_batch_size=4, ai_max_workers=1, convert_max_workers=1)

    # (a) 原文价格确实等于官方在售价 69.9 -> 允许放行
    doc_same_as_official = _make_stage2_price_test_doc(
        tip_id="ok_same",
        raw_text="伊利 纯牛奶 200ml*24盒*2箱\n到手【69.9元】",
        official_price=69.9,
        original_price=99.9,
        coupon_amount=30.0,
    )
    # (b) 多件乘积 拍4件 10.8/件 -> 43.2 即使恰好等于 official activity_price 43.2 -> 允许放行
    doc_multi_product = _make_stage2_price_test_doc(
        tip_id="ok_mult",
        raw_text="颐莲 面膜洁面精华\n拍4件，10.8/件",
        official_price=43.2,
        original_price=69.0,
        coupon_amount=20.0,
    )
    # (c) 券后6.25 淘金币抵扣1.55 -> 4.70 与 act-cpn=4.80 差 0.10 > 0.02 -> 允许放行
    doc_coin_calc = _make_stage2_price_test_doc(
        tip_id="ok_coin",
        raw_text="饭巢火锅底料180g\n【券后6.25元】右上角淘金币抵扣1.55",
        official_price=9.8,
        original_price=14.8,
        coupon_amount=5.0,
    )

    mock_returns = {
        "ok_same": {"main_tip_id": "ok_same", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 69.9, "quality_score": 88},
        "ok_mult": {"main_tip_id": "ok_mult", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 43.2, "quality_score": 88},
        "ok_coin": {"main_tip_id": "ok_coin", "is_valid_deal": True, "main_link_id": "L1", "deal_price": 4.7, "quality_score": 88},
    }
    cleaner.run_ai_stage2_batch = lambda inputs, quiet_sub_logs=False: {
        it["main_tip_id"]: mock_returns[it["main_tip_id"]] for it in inputs
    }

    outcomes, _ = cleaner.process_ai2_unit([doc_same_as_official, doc_multi_product, doc_coin_calc], raw_col=None, quiet_sub_logs=True)
    assert [o["status"] for o in outcomes] == ["success", "success", "success"]
    assert [o["deal_price"] for o in outcomes] == [69.9, 43.2, 4.7]


def test_stage2_prompt_contract_removes_misleading_fields_and_example_artifacts():
    with open(PROMPT_STAGE2_PATH, "r", encoding="utf-8") as fh:
        prompt_text = fh.read()
    assert '"coupon_amount"' not in prompt_text
    assert "coupon_amount 为 0" not in prompt_text
    assert "附实付图" not in prompt_text
    assert "unit_deal_price" not in prompt_text
    assert '"card_highlight"' in prompt_text
    assert "价格唯一真源" in prompt_text
    assert "严禁二次扣减" in prompt_text


def test_multi_product_collection_guard_post_ai1_and_post_convert_no_false_positive():
    """验证合集帖双保险简洁拦截（AI-1主推>2直接拦截 / 转链后同平台>=2或跨平台>=3主商品拦截）及单品零误杀。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from models.deal_constants import ERR_MULTI_PRODUCT_COLLECTION

    cleaner = DealPoolCleaner(ai_batch_size=10, ai_max_workers=1, convert_max_workers=1)
    ai1_called_ids = []
    converted_urls = []
    ai2_called_ids = []

    def _fake_ai1(stage1_inputs, *_a, **_kw):
        for inp in stage1_inputs:
            ai1_called_ids.append(inp["main_tip_id"])
        return {
            # 多链接单品（6条原始链接全都是 券/凑单 + 1主品）：不再前置快筛误拦，正常走完 AI-1 并入库
            "ok_many_raw_links": {
                "main_tip_id": "ok_many_raw_links",
                "is_standalone_deal": True,
                "merged_tip_ids": ["ok_many_raw_links"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "https://u.jd.com/cpn1", "clean_input": "https://u.jd.com/cpn1", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L2", "raw_span": "https://u.jd.com/cpn2", "clean_input": "https://u.jd.com/cpn2", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L3", "raw_span": "https://u.jd.com/cpn3", "clean_input": "https://u.jd.com/cpn3", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L4", "raw_span": "https://u.jd.com/cpn4", "clean_input": "https://u.jd.com/cpn4", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L5", "raw_span": "https://u.jd.com/rich_main", "clean_input": "https://u.jd.com/rich_main", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L6", "raw_span": "https://u.jd.com/rich_filler", "clean_input": "https://u.jd.com/rich_filler", "platform": "jd", "text_role_hint": "filler_candidate"},
                ],
                "_from_fallback": False,
            },
            # AI-1 后拦截：识别出 3 个独立 main_candidate，转链零调用直接拦截且不吸收 neighbor
            "col_post_ai1": {
                "main_tip_id": "col_post_ai1",
                "is_standalone_deal": True,
                "merged_tip_ids": ["col_post_ai1", "single_neighbor"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "https://u.jd.com/c1", "clean_input": "https://u.jd.com/c1", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L2", "raw_span": "https://u.jd.com/c2", "clean_input": "https://u.jd.com/c2", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L3", "raw_span": "https://u.jd.com/c3", "clean_input": "https://u.jd.com/c3", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
                "_from_fallback": False,
            },
            "single_neighbor": {
                "main_tip_id": "single_neighbor",
                "is_standalone_deal": True,
                "merged_tip_ids": ["single_neighbor"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "https://u.jd.com/nb1", "clean_input": "https://u.jd.com/nb1", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
                "_from_fallback": False,
            },
            # 2 个同平台不同商品 -> 转链后凭真实 goods_id >= 2 拦截，0 AI-2 调用
            "col_same_plat_2prods": {
                "main_tip_id": "col_same_plat_2prods",
                "is_standalone_deal": True,
                "merged_tip_ids": ["col_same_plat_2prods"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "https://u.jd.com/sp1", "clean_input": "https://u.jd.com/sp1", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L2", "raw_span": "https://u.jd.com/sp2", "clean_input": "https://u.jd.com/sp2", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
                "_from_fallback": False,
            },
            # 边界单品1：AI-1 标出 2 个 main_candidate，转链证实其中 1 个是券会场、1 个是商品 -> 仅 1 个商品，正常放行
            "ok_2mains_1coupon_1prod": {
                "main_tip_id": "ok_2mains_1coupon_1prod",
                "is_standalone_deal": True,
                "merged_tip_ids": ["ok_2mains_1coupon_1prod"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "https://u.jd.com/cpn_disguise", "clean_input": "https://u.jd.com/cpn_disguise", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L2", "raw_span": "https://u.jd.com/nb1", "clean_input": "https://u.jd.com/nb1", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
                "_from_fallback": False,
            },
            # 边界单品2：1个数字SKU + 1个主推 + 1个凑单 + 2个优惠券
            "ok_single_rich": {
                "main_tip_id": "ok_single_rich",
                "is_standalone_deal": True,
                "merged_tip_ids": ["ok_single_rich"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=1", "clean_input": "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=1", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L2", "raw_span": "https://coupon.m.jd.com/coupons/show.action?key=k2&roleId=2", "clean_input": "https://coupon.m.jd.com/coupons/show.action?key=k2&roleId=2", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L3", "raw_span": "https://item.jd.com/10001.html", "clean_input": "https://item.jd.com/10001.html", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L4", "raw_span": "https://u.jd.com/rich_main", "clean_input": "https://u.jd.com/rich_main", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L5", "raw_span": "https://u.jd.com/rich_filler", "clean_input": "https://u.jd.com/rich_filler", "platform": "jd", "text_role_hint": "filler_candidate"},
                ],
                "_from_fallback": False,
            },
            # 边界单品3：同商品双链（口令 + 短链 -> 同一 goods_id）
            "ok_dual_same_gid": {
                "main_tip_id": "ok_dual_same_gid",
                "is_standalone_deal": True,
                "merged_tip_ids": ["ok_dual_same_gid"],
                "extracted_targets": [
                    {"link_id": "L1", "raw_span": "￥TOKEN_SAME￥", "clean_input": "￥TOKEN_SAME￥", "platform": "taobao", "text_role_hint": "main_candidate"},
                    {"link_id": "L2", "raw_span": "https://s.click.taobao.com/same_gid", "clean_input": "https://s.click.taobao.com/same_gid", "platform": "taobao", "text_role_hint": "main_candidate"},
                ],
                "_from_fallback": False,
            },
        }

    cleaner.run_ai_stage1_batch = _fake_ai1

    def _mock_jd_conv(inp: str):
        converted_urls.append(inp)
        if "item.jd.com/" in inp:
            return {"status": "error", "error_code": "UNSUPPORTED_JD_NUMERIC_SKU", "error_msg": "数字ID"}
        if "cpn" in inp:
            return {
                "status": "success",
                "resolution_kind": "coupon_or_venue",
                "final_link": f"https://u.jd.com/my_{inp.rsplit('/', 1)[-1]}",
                "promotion_token": "",
            }
        gid_map = {
            "https://u.jd.com/sp1": ("JD_SP_1", "毛巾"),
            "https://u.jd.com/sp2": ("JD_SP_2", "干发帽"),
            "https://u.jd.com/rich_main": ("JD_RICH_1", "维达抽纸24包"),
            "https://u.jd.com/nb1": ("JD_NB_1", "独立邻居商品"),
        }
        gid, title = gid_map.get(inp, ("JD_OTHER", "其他"))
        return {
            "status": "success",
            "goods_id": gid,
            "goods_sign": gid,
            "final_link": inp,
            "promotion_token": "",
            "product": {"platform": "jd", "product_id": gid, "name": title, "price": 29.9, "original_price": 39.9, "activity_price": 29.9, "coupon_amount": 0.0, "mall_name": "自营店"},
        }

    def _mock_tb_conv(inp: str):
        converted_urls.append(inp)
        return {
            "status": "success",
            "goods_id": "TB_SAME_999",
            "goods_sign": "TB_SAME_999",
            "final_link": "https://s.click.taobao.com/same_gid",
            "promotion_token": "￥SAME￥",
            "product": {"platform": "taobao", "product_id": "TB_SAME_999", "name": "可口可乐330ml*12瓶", "price": 15.6, "original_price": 25.0, "activity_price": 15.6, "coupon_amount": 0.0, "mall_name": "可口可乐旗舰店"},
        }

    cleaner.adapters = {
        "jd": SimpleNamespace(resolve_and_convert_universal=_mock_jd_conv),
        "taobao": SimpleNamespace(resolve_and_convert_universal=_mock_tb_conv),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    def _fake_ai2(stage2_inputs, *_a, **_kw):
        out = {}
        for item in stage2_inputs:
            mid = item["main_tip_id"]
            ai2_called_ids.append(mid)
            main_lid = next(lk["link_id"] for lk in item["converted_links"] if lk["convert_status"] == "product")
            cpns = [{"link_id": lk["link_id"], "label": "优惠券"} for lk in item["converted_links"] if lk["convert_status"] == "coupon_or_activity"]
            out[mid] = {
                "main_tip_id": mid,
                "is_valid_deal": True,
                "main_link_id": main_lid,
                "deal_price": 15.6,
                "card_highlight": "【1.3元/瓶】",
                "coupon_links": cpns,
                "tutorial_steps": ["1. 下单购买"],
                "quality_score": 88,
            }
        return out

    cleaner.run_ai_stage2_batch = _fake_ai2

    docs = [
        # 原文含 6 个裸链但仅 1 个是主品（不再做前置快筛误杀，全部交由 AI-1 拆解识别）
        {
            "source_site": "haodanku", "source_channel": "好单库", "source_item_id": "ok_many_raw_links",
            "platform": "jd", "title": "多券多活动单品", "tutorial_text": "15.6",
            "extracted_links": [{"url": f"https://u.jd.com/cpn{i}"} for i in range(1, 5)] + [{"url": "https://u.jd.com/rich_main"}, {"url": "https://u.jd.com/rich_filler"}],
            "extracted_tokens": [],
        },
        # AI-1 识别出 3 个 main_candidate -> 转链零调用拦截，且不吸收 single_neighbor
        {
            "source_site": "haodanku", "source_channel": "好单库", "source_item_id": "col_post_ai1",
            "platform": "jd", "title": "3品合集", "tutorial_text": "3品",
            "extracted_links": [{"url": f"https://u.jd.com/c{i}"} for i in (1, 2, 3)],
            "extracted_tokens": [],
        },
        {
            "source_site": "haodanku", "source_channel": "好单库", "source_item_id": "single_neighbor",
            "platform": "jd", "title": "无辜单品", "tutorial_text": "单品",
            "extracted_links": [{"url": "https://u.jd.com/nb1"}],
            "extracted_tokens": [],
        },
        # 同平台 2 个不同商品 -> 转链后拦截，0 AI-2 调用
        {
            "source_site": "haodanku", "source_channel": "好单库", "source_item_id": "col_same_plat_2prods",
            "platform": "jd", "title": "毛巾干发帽2件合集", "tutorial_text": "2品",
            "extracted_links": [{"url": "https://u.jd.com/sp1"}, {"url": "https://u.jd.com/sp2"}],
            "extracted_tokens": [],
        },
        # 2 个 main_candidate 中实为 1券 + 1品
        {
            "source_site": "haodanku", "source_channel": "好单库", "source_item_id": "ok_2mains_1coupon_1prod",
            "platform": "jd", "title": "先领券再买单品", "tutorial_text": "15.6",
            "extracted_links": [{"url": "https://u.jd.com/cpn_disguise"}, {"url": "https://u.jd.com/nb1"}],
            "extracted_tokens": [],
        },
        # 合法多券+数字SKU+凑单 单品
        {
            "source_site": "guangdiu", "source_channel": "逛丢", "source_item_id": "ok_single_rich",
            "platform": "jd", "title": "维达抽纸", "tutorial_text": "维达抽纸15.6",
            "extracted_links": [
                {"url": "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=1", "type": "coupon_url"},
                {"url": "https://item.jd.com/10001.html"},
                {"url": "https://u.jd.com/rich_main"},
            ],
            "extracted_tokens": [],
        },
        # 同品双链
        {
            "source_site": "telegram", "source_channel": "tg", "source_item_id": "ok_dual_same_gid",
            "platform": "taobao", "title": "可乐", "tutorial_text": "15.6",
            "extracted_links": [{"url": "https://s.click.taobao.com/same_gid"}],
            "extracted_tokens": ["￥TOKEN_SAME￥"],
        },
    ]

    mock_col = MagicMock()
    mock_col.find.return_value = []
    res = cleaner.clean_batch_records(docs, mock_col, shared_active_products={}, quiet_sub_logs=True)
    res_map = {r["source_item_id"]: r for r in res}

    # 1. 多裸链不再前置快筛：必须进入 AI-1 且成功入库
    assert "ok_many_raw_links" in ai1_called_ids
    assert res_map["ok_many_raw_links"]["status"] == "success"

    # 2. AI-1 产出 >=3 个 main_candidate：拦截且不浪费转链调用、不误吸邻居
    assert res_map["col_post_ai1"]["status"] == "failed"
    assert res_map["col_post_ai1"]["error_code"] == ERR_MULTI_PRODUCT_COLLECTION
    assert not any(u in converted_urls for u in ("https://u.jd.com/c1", "https://u.jd.com/c2", "https://u.jd.com/c3"))
    assert res_map["single_neighbor"]["status"] == "success"

    # 3. 转链后同平台 >=2 不同主商品：拦截且不调用 AI-2
    assert res_map["col_same_plat_2prods"]["status"] == "failed"
    assert res_map["col_same_plat_2prods"]["error_code"] == ERR_MULTI_PRODUCT_COLLECTION
    assert "col_same_plat_2prods" not in ai2_called_ids

    # 4. 各种边界单品均零误杀，且无 unit_deal_price
    assert res_map["ok_2mains_1coupon_1prod"]["status"] == "success"
    assert res_map["ok_single_rich"]["status"] == "success"
    assert res_map["ok_dual_same_gid"]["status"] == "success"
    deal_info = res_map["ok_dual_same_gid"]["product_doc"]["deal_info"]
    assert "unit_deal_price" not in deal_info
    assert deal_info["card_highlight"] == "1.3元/瓶"

    # 5. 跨平台比价边界：1 TB + 1 JD (total=2, same_plat=1) 放行；1 TB + 2 JD (total=3) 拦截
    s3_cross_docs = [
        {
            "source_site": "guangdiu", "source_channel": "逛丢", "source_item_id": "cross_2_ok",
            "platform": "taobao", "title": "比价单品", "tutorial_text": "",
            "ai_stage1_result": {
                "main_tip_id": "cross_2_ok", "is_standalone_deal": True, "merged_tip_ids": ["cross_2_ok"],
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": "https://s.click.taobao.com/same_gid", "platform": "taobao", "text_role_hint": "main_candidate"},
                    {"link_id": "L2", "clean_input": "https://u.jd.com/nb1", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
            },
        },
        {
            "source_site": "haodanku", "source_channel": "好单库", "source_item_id": "cross_3_block",
            "platform": "taobao", "title": "3平台合集", "tutorial_text": "",
            "ai_stage1_result": {
                "main_tip_id": "cross_3_block", "is_standalone_deal": True, "merged_tip_ids": ["cross_3_block"],
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": "https://s.click.taobao.com/same_gid", "platform": "taobao", "text_role_hint": "main_candidate"},
                    {"link_id": "L2", "clean_input": "https://u.jd.com/nb1", "platform": "jd", "text_role_hint": "main_candidate"},
                    {"link_id": "L3", "clean_input": "https://u.jd.com/rich_main", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
            },
        },
    ]
    s3_out, _ = cleaner.process_convert_unit(s3_cross_docs, shared_active_products={}, quiet_sub_logs=True)
    s3_map = {o["source_item_id"]: o for o in s3_out}
    assert s3_map["cross_2_ok"]["stage_outcome"] == "advance_to_ai2"
    assert s3_map["cross_3_block"]["status"] == "failed"
    assert s3_map["cross_3_block"]["error_code"] == ERR_MULTI_PRODUCT_COLLECTION


def test_competitor_noise_stripping_preserves_legitimate_platform_benefits():
    """验证竞品积分/水印清洗：精准剥离值得买积分、TG 值率与逛丢前缀，零误删正规积分/返卡/教程与壳行。"""
    from services.deal_cleaning.text_sanitizer import (
        assemble_combined_text_and_images,
        build_clean_original_copywriting,
        sanitize_polished_copywriting,
    )

    # 1) zzzdm 含值得买积分 + 有用名额补充 -> 只去积分部分，保留名额与真实实付价
    doc_zzz = {
        "source_site": "zzzdm",
        "source_item_id": "e76qb3qk",
        "title": "【京东】某品牌口罩 50只",
        "tutorial_text": (
            "京东此款目前活动售价19.9元，领5元券，实付低至12.39元。\n"
            "小编补充：返 5 值得买积分，补贴后低至 11.89 元，折 2.38 元/件\n"
            "新增400名额！速冲！\n"
            "购买链接: https://u.jd.com/oOkQmWD"
        ),
        "extracted_links": [{"url": "https://u.jd.com/oOkQmWD"}],
        "extracted_tokens": [],
    }
    copy_zzz = build_clean_original_copywriting(["e76qb3qk"], {"e76qb3qk": doc_zzz}, [], doc=doc_zzz)
    assert "值得买积分" not in copy_zzz
    assert "11.89" not in copy_zzz
    assert "实付低至12.39元" in copy_zzz
    assert "新增400名额！速冲！" in copy_zzz

    comb_zzz, _ = assemble_combined_text_and_images(
        ["e76qb3qk"],
        [{"link_id": "L1", "raw_span": "https://u.jd.com/oOkQmWD", "clean_input": "https://u.jd.com/oOkQmWD"}],
        {"e76qb3qk": doc_zzz},
        doc_zzz,
    )
    assert "值得买积分" not in comb_zzz
    assert "[#L1]" in comb_zzz
    assert "新增400名额！速冲！" in comb_zzz

    # 2) TG_真正值得买 含值得买积分 + 被污染 bot 价格行 + 值率 + 领券:[无需领券] -> 仅当含积分污染才剥 bot 价格行，保留真实正文价格
    doc_tg_polluted = {
        "source_site": "telegram",
        "source_item_id": "zzzdm_399932",
        "title": "徽六 潜香600 六安瓜片绿茶",
        "tutorial_text": (
            "徽六 潜香600 六安瓜片绿茶\n"
            "下单2件，实付低至130元。\n"
            "小编补充：返180值得买积分，补贴后低至112.00元，补贴后折56.00元/件\n"
            "价格：56.0元 (比上次发布低35%)\n"
            "商家：[京东]\n"
            "值率：值: 6 不值: 0\n"
            "领券：[满360减187 (https://u.jd.com/og4nZP7)]\n"
            "------------------------------"
        ),
        "extracted_links": [{"url": "https://u.jd.com/og4nZP7"}],
        "extracted_tokens": [],
    }
    copy_tg_pol = build_clean_original_copywriting(
        ["zzzdm_399932"], {"zzzdm_399932": doc_tg_polluted}, [], doc=doc_tg_polluted
    )
    assert "值得买积分" not in copy_tg_pol
    assert "112.00" not in copy_tg_pol
    assert "价格：56.0元" not in copy_tg_pol
    assert "值率：" not in copy_tg_pol
    assert "实付低至130元" in copy_tg_pol
    assert "满360减187" in copy_tg_pol

    # 3) TG_真正值得买 不含值得买积分 -> 保留 价格： 行，仅剥值率 / 无需领券 / 分割线
    doc_tg_clean = {
        "source_site": "telegram",
        "source_item_id": "zzzdm_399924",
        "title": "360 车载充气泵",
        "tutorial_text": (
            "360 车载有线充气泵，实付低至27.45元。\n"
            "价格：27.45元 (低于常卖价)\n"
            "商家：[拼多多]\n"
            "值率：值: 8 不值: 0\n"
            "领券：[无需领券]\n"
            "------------------------------"
        ),
        "extracted_links": [],
        "extracted_tokens": [],
    }
    copy_tg_clean = build_clean_original_copywriting(
        ["zzzdm_399924"], {"zzzdm_399924": doc_tg_clean}, [], doc=doc_tg_clean
    )
    assert "价格：27.45元" in copy_tg_clean
    assert "值率：" not in copy_tg_clean
    assert "无需领券" not in copy_tg_clean

    # 4) 逛丢标签水印剥离，但保留其余全部正文与 shell 行（结构问题2不误动）
    polished = sanitize_polished_copywriting(
        "【逛丢热门】维达抽纸24包\n领券\n抢购\nPLUS会员1积分兑111-11券\n入会领100积分，确认收货兑换积分返30猫卡\n返45元京东E卡\n好评返现5元\n小编补充：点击右下角逛一逛领随机券"
    )
    assert "【逛丢热门】" not in polished
    assert "PLUS会员1积分兑111-11券" in polished
    assert "入会领100积分" in polished
    assert "兑换积分返30猫卡" in polished
    assert "返45元京东E卡" in polished
    assert "好评返现5元" in polished
    assert "小编补充：点击右下角逛一逛领随机券" in polished
    assert "领券\n抢购" in polished


def test_ai2_batch_swap_guard_high_price_and_score_breakdown_sum():
    """验证 AI-2 批内串位护栏、10万-100万高价保留、score_breakdown 严格求和与默认 ai2_batch_size=6。"""
    from models.deal_constants import CLEAN_STAGE_NEED_AI2, ERR_AI_TIMEOUT_RETRY
    from services.deal_cleaning.stages import _is_ai2_suspected_batch_swap

    cleaner_default = DealPoolCleaner(ai_max_workers=1, convert_max_workers=1)
    assert cleaner_default.ai2_batch_size == 6

    # 1) 批内串位拦截正反验证
    swap_payload = {"is_valid_deal": True, "clean_title": "欧巴兰美瞳半年抛2片大直径彩色隐形眼镜"}
    assert _is_ai2_suspected_batch_swap(
        swap_payload,
        "【百补专属】KIMTRUE且初无花果卸妆油",
        "且初无花果卸妆油",
        [{"official_product": {"title": "KIMTRUE且初无花果卸妆油150ml", "shop_name": "KIMTRUE旗舰店", "brand": "且初"}}],
    ) is True
    assert _is_ai2_suspected_batch_swap(
        {"is_valid_deal": True, "clean_title": "且初无花果卸妆油 150ml"},
        "【百补专属】KIMTRUE且初无花果卸妆油",
        "且初无花果卸妆油",
        [{"official_product": {"title": "KIMTRUE且初无花果卸妆油150ml", "shop_name": "KIMTRUE旗舰店", "brand": "且初"}}],
    ) is False

    # 2) process_ai2_unit 端到端验证：串位走 retry_pending；正常高价(301010)与分项和重算生效
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    docs = [
        {
            "source_site": "xuandan",
            "source_channel": "选单网",
            "source_item_id": "swap_1",
            "title": "KIMTRUE且初无花果卸妆油",
            "ai_stage1_result": {"combined_raw_text": "KIMTRUE且初无花果卸妆油 [#L1]", "merged_tip_ids": ["swap_1"], "extracted_targets": []},
            "full_conv_list": [{"link_id": "L1", "text_role_hint": "main_candidate", "convert_status": "product", "platform": "taobao", "goods_id": "G1", "official_product": {"title": "且初无花果卸妆油", "shop_name": "且初旗舰店", "brand": "且初", "price": 59.0, "activity_price": 59.0}, "my_promo_link": "https://s.click.taobao.com/g1"}],
        },
        {
            "source_site": "zzzdm",
            "source_channel": "值值得买",
            "source_item_id": "lux_1",
            "title": "VERTU Signature 手机 301010元",
            "ai_stage1_result": {"combined_raw_text": "VERTU Signature 手机 实付301010元 [#L1]", "merged_tip_ids": ["lux_1"], "extracted_targets": []},
            "full_conv_list": [{"link_id": "L1", "text_role_hint": "main_candidate", "convert_status": "product", "platform": "taobao", "goods_id": "G2", "official_product": {"title": "VERTU Signature 手机", "shop_name": "VERTU旗舰店", "brand": "VERTU", "price": 301010.0, "activity_price": 301010.0}, "my_promo_link": "https://s.click.taobao.com/g2"}],
        },
    ]
    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        "swap_1": {
            "main_tip_id": "swap_1",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "clean_title": "欧巴兰美瞳半年抛2片大直径彩色隐形眼镜",
            "deal_price": 7.09,
            "quality_score": 88,
        },
        "lux_1": {
            "main_tip_id": "lux_1",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "clean_title": "VERTU Signature 手机",
            "deal_price": 301010.0,
            "card_highlight": "到手301010元",
            "tutorial_steps": ["1. 下单1件，实付301010元"],
            "quality_score": 94,  # 故意偏离分项和 91，验证确定性重算
            "score_breakdown": {"match_score": 27, "completeness_score": 25, "price_clarity_score": 19, "tutorial_and_image_score": 20},
        },
    }
    raw_col = MagicMock()
    raw_col.find.return_value = []
    outcomes, _ = cleaner.process_ai2_unit(docs, raw_col=raw_col, quiet_sub_logs=True)
    out_map = {o["source_item_id"]: o for o in outcomes}

    assert out_map["swap_1"]["status"] == "retry_pending"
    assert out_map["swap_1"]["clean_stage"] == CLEAN_STAGE_NEED_AI2
    assert out_map["swap_1"]["error_code"] == ERR_AI_TIMEOUT_RETRY

    assert out_map["lux_1"]["status"] == "success"
    assert out_map["lux_1"]["deal_price"] == 301010.0
    assert out_map["lux_1"]["quality_score"] == 91
    assert out_map["lux_1"]["product_doc"]["deal_info"]["quality_score"] == 91
