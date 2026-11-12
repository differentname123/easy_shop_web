# -*- coding: utf-8 -*-
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MOBILE_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_MOBILE_WEB_DIR)
for _p in (_MOBILE_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import playwright.sync_api  # noqa: F401
except ModuleNotFoundError:
    import types

    _pw_mod = types.ModuleType("playwright")
    _pw_sync = types.ModuleType("playwright.sync_api")
    _pw_sync.sync_playwright = MagicMock()
    sys.modules.setdefault("playwright", _pw_mod)
    sys.modules.setdefault("playwright.sync_api", _pw_sync)

import logging
from mobile_web.scripts import run_xianyu_deal_discovery_scheduler as sched

# 单元测试期间移除生产日志文件 Handler，避免把 Mock 熔断测试日志写入真实业务日志
sched.logger.handlers = [
    h for h in sched.logger.handlers if not isinstance(h, logging.FileHandler)
]
logging.getLogger("publish_to_xianyu").handlers = [
    h
    for h in logging.getLogger("publish_to_xianyu").handlers
    if not isinstance(h, (logging.FileHandler, sched._ForwardWarningToFileHandler))
]


def _make_sample_deal_doc(
    *,
    product_id: str = "848349275560",
    outer_price: float = 76.0,
    deal_price: float = 66.0,
    format_deal_price: float = 66.0,
    publish_time: str = "2026-11-12 12:00:00",
    need_reformat: bool = False,
    format_status: str = "success",
):
    return {
        "platform": "pdd",
        "product_id": product_id,
        "goods_id": product_id,
        "goods_sign": f"sign_{product_id}",
        "name": "官方外层标题 瑷尔博士精华油",
        "brand": "瑷尔博士",
        "activity_price": outer_price,
        "price": outer_price,
        "updated_at": datetime.datetime(2026, 11, 12, 5, 0, 0, tzinfo=datetime.timezone.utc),
        "deal_info": {
            "is_deal_tip": True,
            "clean_title": "瑷尔博士2.0摇醒精华油30ml*2瓶",
            "deal_price": deal_price,
            "format_deal_price": format_deal_price,
            "format_status": format_status,
            "need_reformat": need_reformat,
            "publish_time": publish_time,
            "price_logic": "领券+百亿补贴多人团",
            "quality_score": 85,
            "format_info": {
                "sku_id": f"{product_id}_deal",
                "price": format_deal_price,
                "core_entities": [{"name": "面部精华油", "score": 10}],
                "decision_keywords": [
                    {"attribute_name": "品牌", "attribute_value": "瑷尔博士", "score": 10},
                    {"attribute_name": "版本", "attribute_value": "2.0摇醒", "score": 9},
                ],
                "pricing_basis": {
                    "is_inferred": False,
                    "structure": [
                        {"value": 2, "unit": "瓶"},
                        {"value": 30, "unit": "ml"},
                    ],
                    "total_value": 60.0,
                    "base_unit": "ml",
                },
            },
        },
    }


def test_build_deal_tip_virtual_doc_shelf_life_and_snapshot():
    now_utc = datetime.datetime(2026, 11, 12, 6, 0, 0, tzinfo=datetime.timezone.utc)
    cutoff_48h = now_utc - datetime.timedelta(hours=48)

    # 1. 48h 内北京时间字符串正常通过并映射为独立商品结构
    doc_ok = _make_sample_deal_doc(publish_time="2026-11-12 12:00:00")
    vdoc = sched._build_deal_tip_virtual_doc(doc_ok, cutoff_utc=cutoff_48h)
    assert vdoc is not None
    assert vdoc["source_type"] == sched.SOURCE_TYPE_DEAL_TIP
    assert vdoc["name"] == "瑷尔博士2.0摇醒精华油30ml*2瓶"
    assert vdoc["activity_price"] == 66.0
    cat_sku = sched._extract_primary_sku_and_category(vdoc, vdoc["activity_price"])
    assert cat_sku is not None
    cat_name, primary_sku = cat_sku
    assert cat_name == "面部精华油"
    base_u, units = sched._extract_raw_units(primary_sku)
    assert base_u == "ml"
    assert units["ml"] == 60.0
    assert units["瓶"] == 2.0

    # 2. ISO UTC 字符串在 48h 内正常通过
    doc_iso = _make_sample_deal_doc(publish_time="2026-11-11T08:30:00Z")
    assert sched._build_deal_tip_virtual_doc(doc_iso, cutoff_utc=cutoff_48h) is not None

    # 3. 超过 48h 的陈旧线报（哪怕外层 updated_at 是今天）严格拦截
    doc_expired = _make_sample_deal_doc(publish_time="2026-11-09 10:00:00")
    assert sched._build_deal_tip_virtual_doc(doc_expired, cutoff_utc=cutoff_48h) is None

    # 4. 价格刚变动但尚未重洗（deal_price != format_deal_price）严格拦截
    doc_mismatch = _make_sample_deal_doc(deal_price=55.0, format_deal_price=66.0)
    assert sched._build_deal_tip_virtual_doc(doc_mismatch, cutoff_utc=cutoff_48h) is None


def test_step1_deal_tip_skips_official_price_overwrite_while_official_verifies():
    now_utc = datetime.datetime(2026, 11, 12, 6, 0, 0, tzinfo=datetime.timezone.utc)
    cutoff_48h = now_utc - datetime.timedelta(hours=48)
    vdoc = sched._build_deal_tip_virtual_doc(_make_sample_deal_doc(), cutoff_utc=cutoff_48h)
    cat_name, sku_fmt = sched._extract_primary_sku_and_category(vdoc, vdoc["activity_price"])

    fake_llm_ret = (
        True,
        None,  # replaced dynamically below
        "瑷尔博士 摇醒精华油 30ml",
        "瑷尔博士 精华油",
        {"model_used": "test-model", "parsed_json": {"selection_reason": "测试入选"}},
    )

    def _fake_select_llm(category_name, candidates):
        return True, candidates[0], fake_llm_ret[2], fake_llm_ret[3], fake_llm_ret[4]

    with patch.object(sched, "_select_standard_product_and_keywords_by_llm", side_effect=_fake_select_llm), patch.object(
        sched, "_verify_live_activity_price", return_value=76.0
    ) as mock_verify:
        # 线报商品：绝不调用官方接口覆盖 66.0 元线报价
        champ_deal = sched._evaluate_single_category_step1(
            cat_name,
            [(vdoc, sku_fmt, vdoc["activity_price"])],
            source_type=sched.SOURCE_TYPE_DEAL_TIP,
        )
        assert champ_deal is not None
        assert champ_deal["source_type"] == sched.SOURCE_TYPE_DEAL_TIP
        assert champ_deal["activity_price"] == 66.0
        assert mock_verify.call_count == 0

        # 正式商品：调用官方接口验价并刷新为实时价格 76.0 元
        off_doc = {**vdoc, "source_type": sched.SOURCE_TYPE_OFFICIAL, "activity_price": 72.0}
        champ_off = sched._evaluate_single_category_step1(
            cat_name,
            [(off_doc, sku_fmt, 72.0)],
            source_type=sched.SOURCE_TYPE_OFFICIAL,
        )
        assert champ_off is not None
        assert champ_off["source_type"] == sched.SOURCE_TYPE_OFFICIAL
        assert champ_off["activity_price"] == 76.0
        assert mock_verify.call_count == 1


def test_ensure_eval_indexes_upgrades_legacy_unique_index():
    fake_col = MagicMock()
    fake_col.list_indexes.return_value = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "platform_1_product_id_1", "key": {"platform": 1, "product_id": 1}, "unique": True},
    ]
    sched._ensure_eval_indexes(fake_col)
    fake_col.drop_index.assert_called_once_with("platform_1_product_id_1")
    created_keys = [call.args[0] for call in fake_col.create_index.call_args_list]
    assert [("platform", 1), ("product_id", 1), ("source_type", 1)] in created_keys


def test_formatters_distinguish_official_and_deal_tip():
    base_rec = {
        "source_type": sched.SOURCE_TYPE_DEAL_TIP,
        "platform": "pdd",
        "product_id": "848349275560",
        "category_name": "面部精华油",
        "name": "瑷尔博士2.0摇醒精华油30ml*2瓶",
        "activity_price": 66.0,
        "final_winner": "my_product",
        "candidate_product": {
            "source_type": sched.SOURCE_TYPE_DEAL_TIP,
            "category_item_count": 5,
            "primary_unit": "ml",
            "unit_quantities": {"ml": 60.0},
            "decision_keywords_summary": "品牌:瑷尔博士",
            "deal_price_logic": "领券+多人团",
        },
        "best_xianyu_product": {
            "item_id": "999888",
            "title": "闲鱼同款精华油30ml",
            "comparable_price": 37.0,
            "is_free_shipping": True,
            "unit_quantities": {"ml": 30.0},
        },
        "comparison_result": {
            "has_comparable_item": True,
            "comparison_basis": "unit_ratio",
            "matched_unit": "ml",
            "cost_performance_ratio": 1.12,
            "my_unit_cost_performance": {"ml": 0.9091},
            "xianyu_unit_cost_performance": {"ml": 0.8108},
            "llm_reason": "线报单价更优",
        },
        "xianyu_audit_trail": {"search_keyword": "瑷尔博士 精华油", "search_ok": True, "batch_detail_ok": True},
    }
    card = sched._format_single_comparison_card(1, 2, base_rec)
    assert "[线报爆料]" in card
    assert "💡 线报优惠方案 : 领券+多人团" in card

    off_rec = {**base_rec, "source_type": sched.SOURCE_TYPE_OFFICIAL}
    summary = sched._format_batch_summary_report("disc_test", 12.3, [off_rec, base_rec])
    assert "【正式官价】参评 1" in summary
    assert "【线报爆料】参评 1" in summary


def test_guest_auth_tracker_detects_core_search_detail_session_expired_only():
    from mobile_web.scripts import publish_to_xianyu as pxy

    fake_page = MagicMock()
    fake_page.frames = []
    fake_page.url = "https://www.goofish.com/search?q=%E6%8A%BD%E7%BA%B8"
    tracker = pxy._attach_auth_tracker(fake_page)
    on_resp = tracker["_on_response_cb"]

    def _emit(url: str, ret_list):
        resp = MagicMock()
        resp.url = url
        resp.json.return_value = {"ret": ret_list, "data": {}}
        on_resp(resp)

    # 访客首页常规未登录返回（loginuser.get）与热词底纹（pc.search.shade）绝不误判为限流
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/?jsv=2.7.2",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search.shade/1.0/?jsv=2.7.2",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    assert tracker["guest_login_blocked_rets"] == []
    pxy._verify_guest_no_risk_block(fake_page, tracker, r"W:\temp\xianyu_guest", task_name="unit_test")

    # 一旦核心搜索接口 pc.search/ 返回 Session过期，立即识别为免登录限流拦截并标记 is_risk_blocked=True
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?jsv=2.7.2",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    assert len(tracker["guest_login_blocked_rets"]) == 1
    caught_err = None
    try:
        pxy._verify_guest_no_risk_block(fake_page, tracker, r"W:\temp\xianyu_guest", task_name="unit_test")
    except pxy.XianyuOfflineError as err:
        caught_err = err
    assert caught_err is not None
    assert caught_err.error_code == "RISK_CONTROL_BLOCKED"
    built = pxy._build_offline_result(
        r"W:\temp\xianyu_guest",
        caught_err.reason,
        error_code=caught_err.error_code,
        action="search_xianyu_items",
    )
    assert built["ok"] is False
    assert built["is_risk_blocked"] is True


def _make_sample_champion(idx: int = 1) -> dict:
    return {
        "source_type": sched.SOURCE_TYPE_OFFICIAL,
        "platform": "pdd",
        "product_id": f"10000{idx}",
        "goods_sign": f"sign_{idx}",
        "channel_type": 1,
        "category_name": f"测试分类{idx}",
        "category_item_count": 10,
        "category_top20_count": 10,
        "name": f"维达抽纸整箱24包_{idx}",
        "brand": "维达",
        "db_activity_price": 19.9,
        "activity_price": 19.9,
        "primary_unit": "包",
        "unit_quantities": {"包": 24.0},
        "unit_cost_performance": {"包": round(24.0 / 19.9, 6)},
        "decision_keywords_summary": "品牌:维达",
        "pricing_basis": {},
        "search_keyword": f"维达 抽纸 24包 {idx}",
        "backup_search_keyword": f"维达 抽纸 {idx}",
        "selection_reason": "测试标品",
        "llm_keyword_output": {},
    }


def test_compare_candidate_never_retries_backup_keyword_when_primary_search_fails():
    cand = _make_sample_champion(1)
    breaker = sched._RoundCircuitBreaker(max_consecutive_fails=2)
    blocked_res = {
        "ok": False,
        "is_offline": True,
        "is_risk_blocked": True,
        "error_code": "RISK_CONTROL_BLOCKED",
        "user_data_dir": r"W:\temp\xianyu_guest",
        "items": [],
        "message": "免登录检索被强制要求登录/IP匿名配额限流",
    }
    with patch.object(sched, "search_xianyu_items", return_value=blocked_res) as mock_search:
        rec = sched.compare_candidate_with_xianyu(
            candidate=cand,
            guest_user_data_dir=r"W:\temp\xianyu_guest",
            circuit_breaker=breaker,
        )
    # 主搜索报错时绝不换备用词二次重试，且单次风控限流立即触发整轮熔断
    assert mock_search.call_count == 1
    assert breaker.is_tripped() is True
    assert rec["final_winner"] == "browser_error"
    assert rec["xianyu_audit_trail"]["browser_aborted"] is True
    assert rec["xianyu_audit_trail"]["is_risk_blocked"] is True


def test_pre_round_probe_aborts_before_stage1_ai_and_marks_risk_cooldown():
    probe_fail = (
        False,
        {
            "ok": False,
            "is_risk_blocked": True,
            "error_code": "RISK_CONTROL_BLOCKED",
            "message": "免登录检索被强制要求登录",
        },
    )
    with patch.object(sched, "probe_xianyu_guest_ready", return_value=probe_fail) as mock_probe, patch.object(
        sched, "gen_db_object"
    ) as mock_db, patch.object(sched, "select_verified_category_champions") as mock_stage1:
        records = sched.run_discovery_cycle(
            enable_official=True,
            enable_deal_tip=True,
            enable_pre_round_probe=True,
            probe_keyword="抽纸",
        )
    assert records == []
    assert mock_probe.call_count == 1
    assert mock_db.call_count == 0
    assert mock_stage1.call_count == 0
    assert sched._LAST_CYCLE_ABORTED_BY_RISK is True


def test_mid_round_circuit_breaker_stops_remaining_champions_without_retry_or_db_write():
    champs = [_make_sample_champion(1), _make_sample_champion(2), _make_sample_champion(3)]
    blocked_res = {
        "ok": False,
        "is_offline": True,
        "is_risk_blocked": True,
        "error_code": "RISK_CONTROL_BLOCKED",
        "user_data_dir": r"W:\temp\xianyu_guest",
        "items": [],
        "message": "免登录检索被强制要求登录",
    }
    fake_db = MagicMock()
    fake_col = MagicMock()
    fake_col.list_indexes.return_value = []
    fake_db.get_collection.return_value = fake_col

    with patch.object(sched, "gen_db_object", return_value=fake_db), patch.object(
        sched, "select_verified_category_champions", return_value=champs
    ), patch.object(sched, "search_xianyu_items", return_value=blocked_res) as mock_search:
        records = sched.run_discovery_cycle(
            enable_official=True,
            enable_deal_tip=False,
            enable_pre_round_probe=False,
            compare_workers=2,
        )

    # 第 1 个分类在单槽令牌池中触发限流熔断后，排队中的第 2、3 个分类直接跳过，总计仅发起 1 次浏览器搜索，且 0 条脏数据入库
    assert mock_search.call_count == 1
    assert len(records) == 1
    assert fake_col.update_one.call_count == 0
    assert sched._LAST_CYCLE_ABORTED_BY_RISK is True


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_funcs = [
        v for k, v in list(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for fn in test_funcs:
        fn()
        print(f"PASSED: {fn.__name__}")
    print(f"\nALL {len(test_funcs)} TESTS PASSED!")


