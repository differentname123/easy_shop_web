# -*- coding: utf-8 -*-
import datetime
import json
import logging
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

from mobile_web.scripts import publish_to_xianyu as pxy
from mobile_web.scripts import run_xianyu_deal_discovery_scheduler as sched

# 单元测试期间移除生产日志文件 Handler，避免把 Mock 熔断测试日志写入真实业务日志
sched.logger.handlers = [h for h in sched.logger.handlers if not isinstance(h, logging.FileHandler)]
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
                    "structure": [{"value": 2, "unit": "瓶"}, {"value": 30, "unit": "ml"}],
                    "total_value": 60.0,
                    "base_unit": "ml",
                },
            },
        },
    }


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


def test_build_deal_tip_virtual_doc_shelf_life_and_snapshot():
    now_utc = datetime.datetime(2026, 11, 12, 6, 0, 0, tzinfo=datetime.timezone.utc)
    cutoff_48h = now_utc - datetime.timedelta(hours=48)

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

    doc_iso = _make_sample_deal_doc(publish_time="2026-11-11T08:30:00Z")
    assert sched._build_deal_tip_virtual_doc(doc_iso, cutoff_utc=cutoff_48h) is not None

    doc_expired = _make_sample_deal_doc(publish_time="2026-11-09 10:00:00")
    assert sched._build_deal_tip_virtual_doc(doc_expired, cutoff_utc=cutoff_48h) is None

    doc_mismatch = _make_sample_deal_doc(deal_price=55.0, format_deal_price=66.0)
    assert sched._build_deal_tip_virtual_doc(doc_mismatch, cutoff_utc=cutoff_48h) is None


def test_step1_deal_tip_skips_official_price_overwrite_while_official_verifies():
    now_utc = datetime.datetime(2026, 11, 12, 6, 0, 0, tzinfo=datetime.timezone.utc)
    cutoff_48h = now_utc - datetime.timedelta(hours=48)
    vdoc = sched._build_deal_tip_virtual_doc(_make_sample_deal_doc(), cutoff_utc=cutoff_48h)
    cat_name, sku_fmt = sched._extract_primary_sku_and_category(vdoc, vdoc["activity_price"])

    def _fake_select_llm(category_name, candidates):
        return (
            True,
            candidates[0],
            "瑷尔博士 摇醒精华油 30ml",
            "瑷尔博士 精华油",
            {"model_used": "test-model", "parsed_json": {"selection_reason": "测试入选"}},
        )

    with patch.object(sched, "_select_standard_product_and_keywords_by_llm", side_effect=_fake_select_llm), patch.object(
        sched, "_verify_live_activity_price", return_value=76.0
    ) as mock_verify:
        champ_deal = sched._evaluate_single_category_step1(
            cat_name, [(vdoc, sku_fmt, vdoc["activity_price"])], source_type=sched.SOURCE_TYPE_DEAL_TIP
        )
        assert champ_deal is not None
        assert champ_deal["source_type"] == sched.SOURCE_TYPE_DEAL_TIP
        assert champ_deal["activity_price"] == 66.0
        assert mock_verify.call_count == 0

        off_doc = {**vdoc, "source_type": sched.SOURCE_TYPE_OFFICIAL, "activity_price": 72.0}
        champ_off = sched._evaluate_single_category_step1(
            cat_name, [(off_doc, sku_fmt, 72.0)], source_type=sched.SOURCE_TYPE_OFFICIAL
        )
        assert champ_off is not None
        assert champ_off["source_type"] == sched.SOURCE_TYPE_OFFICIAL
        assert champ_off["activity_price"] == 76.0
        assert mock_verify.call_count == 1


def test_ensure_eval_indexes_creates_composite_unique_index():
    fake_col = MagicMock()
    sched._ensure_eval_indexes(fake_col)
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
    assert "【二、逐商品" not in summary


def test_auth_tracker_search_vs_h5_guest_detail_detection():
    fake_page = MagicMock()
    fake_page.frames = []
    fake_page.url = "https://h5.m.goofish.com/item?id=780427279990"
    tracker = pxy._attach_auth_tracker(fake_page)
    on_resp = tracker["_on_response_cb"]

    def _emit(url: str, ret_list):
        resp = MagicMock()
        resp.url = url
        resp.json.return_value = {"ret": ret_list, "data": {}}
        on_resp(resp)

    # 非核心接口（loginuser.get / pc.search.shade）Session过期绝不误判为 H5 详情风控拦截
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

    # 登录搜索核心接口 pc.search/ 返回 Session过期，记入 expired_rets 供 _verify_account_online 拦截掉线
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?jsv=2.7.2",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    assert any("pc.search" in r for r in tracker["expired_rets"])

    # H5 详情接口 awesome.detail/ 返回 Session过期，立即识别为免登录限流拦截 (RISK_CONTROL_BLOCKED)
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idle.awesome.detail/1.0/?jsv=2.7.2",
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


def test_compare_candidate_never_retries_backup_keyword_when_primary_search_fails():
    cand = _make_sample_champion(1)
    blocked_res = {
        "ok": False,
        "is_offline": True,
        "is_risk_blocked": True,
        "error_code": "RISK_CONTROL_BLOCKED",
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [],
        "message": "检测到闲鱼滑块验证",
    }
    with patch.object(sched, "search_xianyu_items", return_value=blocked_res) as mock_search:
        rec = sched.compare_candidate_with_xianyu(
            candidate=cand,
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
        )
    assert mock_search.call_count == 1
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
            "message": "检测到滑块拦截",
        },
    )
    with patch.object(sched, "probe_xianyu_search_ready", return_value=probe_fail) as mock_probe, patch.object(
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
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [],
        "message": "搜索触发滑块限流",
    }
    fake_db = MagicMock()
    fake_col = MagicMock()
    fake_db.get_collection.return_value = fake_col

    with patch.object(sched, "gen_db_object", return_value=fake_db), patch.object(
        sched, "select_verified_category_champions", return_value=champs
    ), patch.object(sched, "search_xianyu_items", return_value=blocked_res) as mock_search:
        records = sched.run_discovery_cycle(
            enable_official=True,
            enable_deal_tip=False,
            enable_pre_round_probe=False,
        )

    # 顺序串行执行下，第 1 个分类触发风控立即 break，后续第 2、3 个分类 0 次请求，且 0 条脏数据写入数据库
    assert mock_search.call_count == 1
    assert len(records) == 1
    assert fake_col.update_one.call_count == 0
    assert sched._LAST_CYCLE_ABORTED_BY_RISK is True


def test_option_b_dual_directory_isolation_in_compare_candidate():
    cand = _make_sample_champion(1)
    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [
            {"item_id": "880001", "title": "维达抽纸整箱24包包邮", "price": 18.5, "want_cnt": 5, "is_free_shipping": True}
            for _ in range(6)
        ],
    }
    fake_batch_det_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_guest",
        "details_by_id": {
            "880001": {
                "ok": True,
                "item_id": "880001",
                "title": "维达抽纸整箱24包包邮",
                "desc": "维达超韧抽纸3层120抽24包整箱包邮",
                "price": 18.5,
                "original_price": 29.9,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "has_multi_sku": False,
                "min_price": 18.5,
                "max_price": 18.5,
                "sku_list": [],
                "cpv_labels": [],
                "want_cnt": 5,
            }
        },
    }
    fake_step3_llm = {
        "status": "成功",
        "content": json.dumps(
            {
                "has_comparable_item": True,
                "comparable_item_ids": ["880001"],
                "best_xianyu_item_id": "880001",
                "best_xianyu_price": 18.5,
                "xianyu_unit_quantities": {"包": 24.0},
                "xianyu_unit_cost_performance": {"包": round(24.0 / 18.5, 6)},
                "llm_winner": "xianyu_product",
                "llm_reason": "同款24包闲鱼含邮更便宜",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_model"},
    }

    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res) as mock_search, patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(["880001"], {}, "ok")
    ), patch.object(
        sched, "get_xianyu_item_details_batch", return_value=fake_batch_det_res
    ) as mock_batch_det, patch.object(sched, "generate_content", return_value=fake_step3_llm):
        rec = sched.compare_candidate_with_xianyu(
            candidate=cand,
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
        )

    assert mock_search.call_count == 1
    assert mock_search.call_args.kwargs["user_data_dir"] == r"W:\temp\xianyu_xiaohao"
    assert mock_batch_det.call_count == 1
    assert mock_batch_det.call_args.kwargs["user_data_dir"] == r"W:\temp\xianyu_guest"
    assert rec["final_winner"] == "xianyu_product"
    assert rec["best_xianyu_product"]["title"] == "维达抽纸整箱24包包邮"
    assert "keyword_llm" not in rec["llm_raw_output"]
    assert "step3_comparison_llm" in rec["llm_raw_output"]


def test_pre_round_probe_uses_xiaohao_login_and_aborts_when_account_offline():
    offline_res = {
        "ok": False,
        "is_offline": True,
        "is_risk_blocked": False,
        "error_code": "ACCOUNT_OFFLINE",
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [],
        "message": "闲鱼账号处于未登录或掉线状态 (缺少 unb 登录标识 Cookie)",
    }
    with patch.object(sched, "search_xianyu_items", return_value=offline_res) as mock_search, patch.object(
        sched, "gen_db_object"
    ) as mock_db, patch.object(sched, "select_verified_category_champions") as mock_stage1:
        records = sched.run_discovery_cycle(
            enable_official=True,
            enable_deal_tip=True,
            enable_pre_round_probe=True,
            probe_keyword="抽纸",
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
        )

    assert records == []
    assert mock_search.call_count == 1
    assert mock_search.call_args.kwargs["user_data_dir"] == r"W:\temp\xianyu_xiaohao"
    assert mock_db.call_count == 0
    assert mock_stage1.call_count == 0
    assert sched._LAST_CYCLE_ABORTED_BY_RISK is True


def test_parse_h5_awesome_detail_packet_and_single_detail_delegation():
    sample_h5_pkt = {
        "api": "mtop.taobao.idle.awesome.detail",
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "itemDO": {
                "title": "维达抽纸超韧系列多规格包邮",
                "desc": "维达抽纸超韧系列多规格包邮\n正品直发全国包邮",
                "soldPrice": "9.90",
                "originalPrice": "29.90",
                "transportFee": "0.00",
                "minPrice": "9.90",
                "maxPrice": "26.90",
                "wantCnt": 18,
                "browseCnt": 320,
                "quantity": 50,
                "priceRelativeTags": [{"text": "包邮"}],
                "skuList": [
                    {
                        "price": 990,
                        "priceInCent": "990",
                        "quantity": 20,
                        "propertyList": [{"propertyText": "规格", "valueText": "8包体验装"}],
                    },
                    {
                        "price": 2690,
                        "priceInCent": "2690",
                        "quantity": 30,
                        "propertyList": [{"propertyText": "规格", "valueText": "24包整箱装"}],
                    },
                ],
            },
            "sellerDO": {"nick": "测试卖家", "city": "广州"},
        },
    }
    parsed = pxy._parse_detail_packet_to_dict(
        success_packet=sample_h5_pkt,
        item_id="780427279990",
        detail_url="https://www.goofish.com/item?id=780427279990",
        user_data_dir=r"W:\temp\xianyu_guest",
    )
    assert parsed["ok"] is True
    assert parsed["is_free_shipping"] is True
    assert parsed["has_multi_sku"] is True
    assert len(parsed["sku_list"]) == 2
    assert parsed["sku_list"][0]["price"] == 9.9
    assert parsed["sku_list"][1]["price"] == 26.9

    with patch.object(
        pxy,
        "get_xianyu_item_details_batch",
        return_value={"ok": True, "details_by_id": {"780427279990": parsed}},
    ) as mock_batch:
        single_res = pxy.get_xianyu_item_detail("https://www.goofish.com/item?id=780427279990")
    assert mock_batch.call_count == 1
    assert mock_batch.call_args.kwargs["item_ids"] == ["780427279990"]
    assert single_res["item_id"] == "780427279990"


def test_havana_silent_sso_cold_start_clears_transient_session_expired():
    """验证浏览器冷启动瞬间未生成 cookie2 触发的瞬态 Session过期，在 havana_lgc2_77 静默续期成功后能自动清除并通过在线校验。"""
    fake_page = MagicMock()
    fake_page.frames = []
    fake_page.url = "https://www.goofish.com/"
    fake_ctx = MagicMock()

    tracker = pxy._attach_auth_tracker(fake_page)
    on_resp = tracker["_on_response_cb"]

    def _emit(url: str, ret_list, data=None):
        resp = MagicMock()
        resp.url = url
        resp.json.return_value = {"ret": ret_list, "data": data or {}}
        on_resp(resp)

    # 1. 刚打开页面时 cookie2 尚未生成，loginuser.get 与 user.page.nav 短暂返回 Session过期
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    assert len(tracker["expired_rets"]) == 2

    # 2. 模拟等待 200ms 后 sdkSilent 静默换取 cookie2 成功并重发 loginuser.get 返回 userId
    cookie_state = [
        [{"name": "unb", "value": "4229872747"}, {"name": "havana_lgc2_77", "value": "token"}],
        [{"name": "unb", "value": "4229872747"}, {"name": "havana_lgc2_77", "value": "token"}, {"name": "cookie2", "value": "fresh_c2"}],
    ]
    fake_ctx.cookies.side_effect = lambda: cookie_state[0] if tracker["expired_rets"] else cookie_state[1]

    def _on_wait(_ms):
        _emit(
            "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/",
            ["SUCCESS::调用成功"],
            {"userId": 4229872747},
        )

    fake_page.wait_for_timeout.side_effect = _on_wait
    unb = pxy._verify_account_online(fake_ctx, fake_page, tracker, r"W:\temp\xianyu_xiaohao", task_name="cold_start_test")
    assert unb == "4229872747"
    assert tracker["expired_rets"] == []


class _InMemoryXianyuItemsCol:
    """模拟 MongoDB xianyu_items 集合的 bulk_write ($set / $setOnInsert) 与 find 查询语义。"""

    def __init__(self, initial_docs=None):
        self.docs = {}
        self.indexes = []
        for d in initial_docs or []:
            self.docs[str(d["item_id"])] = dict(d)

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def bulk_write(self, ops, ordered=False):
        for op in ops:
            iid = str(op._filter["item_id"])
            is_insert = iid not in self.docs
            target = self.docs.setdefault(iid, {})
            if is_insert and "$setOnInsert" in op._doc:
                target.update(dict(op._doc["$setOnInsert"]))
            if "$set" in op._doc:
                target.update(dict(op._doc["$set"]))

    def find(self, query):
        id_filter = set((query.get("item_id") or {}).get("$in") or [])
        req_has_detail = query.get("has_detail")
        matched = []
        for iid, doc in self.docs.items():
            if id_filter and iid not in id_filter:
                continue
            if req_has_detail is not None and bool(doc.get("has_detail")) != bool(req_has_detail):
                continue
            matched.append(dict(doc))
        return matched


def test_ensure_xianyu_items_indexes():
    fake_items_col = _InMemoryXianyuItemsCol()
    sched._ensure_xianyu_items_indexes(fake_items_col)
    unique_item_idx = [
        (keys, kw) for keys, kw in fake_items_col.indexes if keys == [("item_id", 1)]
    ]
    assert len(unique_item_idx) == 1
    assert unique_item_idx[0][1].get("unique") is True


def test_xianyu_items_7day_detail_cache_full_hit_skips_browser_detail_fetch():
    """验证 7 天内已有完整详情时：搜索更新概览绝不冲掉旧详情，且步骤 3 完全跳过 H5 浏览器拉取直接复用多规格详情。"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    two_days_ago = now_utc - datetime.timedelta(days=2)
    cached_detail = {
        "ok": True,
        "item_id": "880001",
        "item_url": "https://www.goofish.com/item?id=880001",
        "title": "维达抽纸整箱24包包邮",
        "desc": "维达超韧抽纸3层120抽多规格整箱包邮",
        "price": 9.9,
        "original_price": 29.9,
        "transport_fee": 0.0,
        "is_free_shipping": True,
        "has_multi_sku": True,
        "min_price": 9.9,
        "max_price": 18.5,
        "sku_list": [
            {"spec_text": "规格: 8包体验装", "price": 9.9, "quantity": 10},
            {"spec_text": "规格: 24包整箱装", "price": 18.5, "quantity": 25},
        ],
        "cpv_labels": [],
        "want_cnt": 12,
    }
    fake_items_col = _InMemoryXianyuItemsCol(
        [
            {
                "item_id": "880001",
                "has_detail": True,
                "detail": cached_detail,
                "detail_updated_at": two_days_ago,
                "detail_updated_at_str": "2026-11-11 10:00:00",
            }
        ]
    )
    cand = _make_sample_champion(1)
    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [
            {"item_id": f"88000{i}", "title": "维达抽纸整箱24包包邮", "price": 9.9, "want_cnt": 12, "is_free_shipping": True}
            for i in range(1, 7)
        ],
    }
    fake_step3_llm = {
        "status": "成功",
        "content": json.dumps(
            {
                "has_comparable_item": True,
                "comparable_item_ids": ["880001"],
                "best_xianyu_item_id": "880001",
                "best_xianyu_spec": "24包整箱装",
                "best_xianyu_price": 18.5,
                "xianyu_unit_quantities": {"包": 24.0},
                "xianyu_unit_cost_performance": {"包": round(24.0 / 18.5, 6)},
                "llm_winner": "xianyu_product",
                "llm_reason": "复用7天内详情的多规格24包真实价18.5元比价",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_model"},
    }

    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res), patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(["880001"], {}, "ok")
    ), patch.object(sched, "get_xianyu_item_details_batch") as mock_batch_det, patch.object(
        sched, "generate_content", return_value=fake_step3_llm
    ):
        rec = sched.compare_candidate_with_xianyu(
            candidate=cand,
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
            items_col=fake_items_col,
            detail_cache_days=7,
        )

    # 7天内详情全命中 -> 0 次 H5 详情浏览器调用，且搜索入库未覆盖原有详情与多规格 SKU
    assert mock_batch_det.call_count == 0
    assert fake_items_col.docs["880001"]["has_detail"] is True
    assert fake_items_col.docs["880001"]["detail_updated_at"] == two_days_ago
    assert "search_updated_at" in fake_items_col.docs["880001"]
    assert fake_items_col.docs["880002"]["has_detail"] is False
    assert rec["xianyu_audit_trail"]["step3_detail_cache_hit_count"] == 1
    assert rec["xianyu_audit_trail"]["step3_detail_live_fetched_count"] == 0
    assert len(rec["best_xianyu_product"]["sku_list"]) == 2
    assert rec["final_winner"] == "xianyu_product"


def test_xianyu_items_partial_cache_hit_and_expired_detail_refresh():
    """验证混合场景：<7天详情直接复用、>7天过期详情与仅搜索无详情记录走H5补拉并刷新入库、拉取失败记录不污染缓存。"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    three_days_ago = now_utc - datetime.timedelta(days=3)
    eight_days_ago = now_utc - datetime.timedelta(days=8)

    fake_items_col = _InMemoryXianyuItemsCol(
        [
            {
                "item_id": "9001",
                "has_detail": True,
                "detail_updated_at": three_days_ago,
                "detail": {
                    "ok": True,
                    "item_id": "9001",
                    "title": "缓存有效商品9001",
                    "desc": "3天前已拉取详情",
                    "price": 21.0,
                    "transport_fee": 0.0,
                    "is_free_shipping": True,
                    "has_multi_sku": False,
                    "min_price": 21.0,
                    "max_price": 21.0,
                    "sku_list": [],
                },
            },
            {
                "item_id": "9002",
                "has_detail": True,
                "detail_updated_at": eight_days_ago,
                "detail": {
                    "ok": True,
                    "item_id": "9002",
                    "title": "已超7天过期商品9002",
                    "desc": "8天前旧详情",
                    "price": 25.0,
                    "transport_fee": 0.0,
                    "is_free_shipping": True,
                    "has_multi_sku": False,
                    "min_price": 25.0,
                    "max_price": 25.0,
                    "sku_list": [],
                },
            },
            {
                "item_id": "9003",
                "has_detail": False,
                "search_card": {"item_id": "9003", "title": "仅有搜索概览无详情9003", "price": 19.0},
            },
        ]
    )

    cand = _make_sample_champion(2)
    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [
            {"item_id": "9001", "title": "缓存有效商品9001", "price": 21.0, "want_cnt": 3, "is_free_shipping": True},
            {"item_id": "9002", "title": "已超7天过期商品9002", "price": 20.5, "want_cnt": 4, "is_free_shipping": True},
            {"item_id": "9003", "title": "仅有搜索概览无详情9003", "price": 19.0, "want_cnt": 6, "is_free_shipping": True},
            {"item_id": "9004", "title": "拉取详情失败商品9004", "price": 15.0, "want_cnt": 1, "is_free_shipping": True},
            {"item_id": "9005", "title": "凑够6条商品9005", "price": 29.0, "want_cnt": 1, "is_free_shipping": True},
            {"item_id": "9006", "title": "凑够6条商品9006", "price": 30.0, "want_cnt": 1, "is_free_shipping": True},
        ],
    }
    fake_live_batch_det = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_guest",
        "details_by_id": {
            "9002": {
                "ok": True,
                "item_id": "9002",
                "title": "已超7天过期商品9002_新详情",
                "desc": "重新拉取的新详情24包",
                "price": 20.5,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "has_multi_sku": False,
                "min_price": 20.5,
                "max_price": 20.5,
                "sku_list": [],
            },
            "9003": {
                "ok": True,
                "item_id": "9003",
                "title": "新补拉详情商品9003",
                "desc": "24包整箱装包邮",
                "price": 19.0,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "has_multi_sku": False,
                "min_price": 19.0,
                "max_price": 19.0,
                "sku_list": [],
            },
            "9004": {
                "ok": False,
                "item_id": "9004",
                "error_code": "DETAIL_PACKET_EMPTY",
                "message": "商品已下架",
            },
        },
    }
    fake_step3_llm = {
        "status": "成功",
        "content": json.dumps(
            {
                "has_comparable_item": True,
                "comparable_item_ids": ["9001", "9002", "9003"],
                "best_xianyu_item_id": "9003",
                "best_xianyu_price": 19.0,
                "xianyu_unit_quantities": {"包": 24.0},
                "xianyu_unit_cost_performance": {"包": round(24.0 / 19.0, 6)},
                "llm_winner": "xianyu_product",
                "llm_reason": "9003含邮19元最优",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_model"},
    }

    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res), patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(["9001", "9002", "9003", "9004"], {}, "ok")
    ), patch.object(
        sched, "get_xianyu_item_details_batch", return_value=fake_live_batch_det
    ) as mock_batch_det, patch.object(sched, "generate_content", return_value=fake_step3_llm):
        rec = sched.compare_candidate_with_xianyu(
            candidate=cand,
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
            items_col=fake_items_col,
            detail_cache_days=7,
        )

    # 9001 命中7天缓存免拉取；仅向 H5 请求缺失/过期的 ["9002", "9003", "9004"]
    assert mock_batch_det.call_count == 1
    assert mock_batch_det.call_args.kwargs["item_ids"] == ["9002", "9003", "9004"]

    # 9002 详情与时间戳已刷新；9003 变为 has_detail=True；9004 失败绝不标为 has_detail=True
    assert fake_items_col.docs["9002"]["detail_updated_at"] > three_days_ago
    assert fake_items_col.docs["9002"]["price"] == 20.5
    assert fake_items_col.docs["9003"]["has_detail"] is True
    assert fake_items_col.docs["9004"]["has_detail"] is False

    audit = rec["xianyu_audit_trail"]
    assert audit["step3_detail_cache_hit_count"] == 1
    assert audit["step3_detail_live_fetched_count"] == 2
    assert audit["step3_detail_fetched_count"] == 3
    assert audit["step3_detail_failed_count"] == 1


def test_save_json_concurrent_unique_tmp_and_permission_retry():
    """验证 save_json 使用进程+随机唯一临时文件并在 Windows PermissionError 时自动退避重试，不残留 .tmp 文件。"""
    import concurrent.futures
    import tempfile
    from infrastructure.ai_api import model_api

    with tempfile.TemporaryDirectory() as tmpdir:
        target = os.path.join(tmpdir, "api_keys_state.json")
        real_replace = os.replace
        calls = {"n": 0}

        def _flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(5, "拒绝访问")
            return real_replace(src, dst)

        with patch.object(model_api.os, "replace", side_effect=_flaky_replace):
            model_api.save_json(target, {"status": "ok", "count": 3})

        assert calls["n"] == 3
        with open(target, "r", encoding="utf-8") as f:
            assert json.load(f) == {"status": "ok", "count": 3}
        assert [x for x in os.listdir(tmpdir) if x.endswith(".tmp")] == []

        # 多线程高频并发写同一路径不抛 FileNotFoundError / PermissionError
        def _worker(tid: int):
            for seq in range(15):
                model_api.save_json(target, {"tid": tid, "seq": seq})

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(_worker, i) for i in range(6)]
            for fut in futs:
                fut.result()

        with open(target, "r", encoding="utf-8") as f:
            final_data = json.load(f)
        assert "tid" in final_data and "seq" in final_data
        assert [x for x in os.listdir(tmpdir) if x.endswith(".tmp")] == []


def test_layer1_source_purification_and_cumulative_unit_dedup():
    """验证第1层源头净化（幽灵品牌/商用大桶/试用小样/官价低销量剔除）与【共X盒】累计总量防翻倍修复。"""
    ghost_doc = {
        "name": "洗衣液大桶40斤酒店洗衣房专用持久留香实惠装",
        "brand": "宝渍",
        "sales_tip": "已拼5000+件",
    }
    assert sched._is_disqualified_source_candidate(ghost_doc, {}, sched.SOURCE_TYPE_OFFICIAL) is True

    sample_doc = {
        "name": "蓝月亮洗衣液旅行装试用装小样50g*2袋",
        "brand": "蓝月亮",
        "sales_tip": "已拼1万+件",
    }
    assert sched._is_disqualified_source_candidate(sample_doc, {}, sched.SOURCE_TYPE_DEAL_TIP) is True

    low_sales_off_doc = {
        "name": "维达超韧抽纸3层120抽24包整箱",
        "brand": "维达",
        "sales_tip": "已拼38件",
    }
    assert sched._is_disqualified_source_candidate(low_sales_off_doc, {}, sched.SOURCE_TYPE_OFFICIAL) is True

    good_off_doc = {
        "name": "维达超韧抽纸3层120抽24包整箱",
        "brand": "维达",
        "sales_tip": "已拼1.2万+件",
    }
    assert sched._is_disqualified_source_candidate(good_off_doc, {}, sched.SOURCE_TYPE_OFFICIAL) is False

    # 验证「200ml*8盒*4箱共32盒」累计结构不会被二次连乘放大 4 倍（同时覆盖 total_value 25600 错算 + 标题 '共32盒' 双重纠偏）
    liunainai_sku = {
        "pricing_basis": {
            "total_value": 25600,
            "base_unit": "ml",
            "structure": [
                {"value": 4, "unit": "箱"},
                {"value": 32, "unit": "盒"},
                {"value": 200, "unit": "ml"},
            ],
        },
        "decision_keywords": [],
    }
    base_u, units = sched._extract_raw_units(
        liunainai_sku, context_text="隔壁刘奶奶水牛奶200ml*8盒*4箱共32盒学生早餐奶"
    )
    assert base_u == "ml"
    assert units["箱"] == 4.0
    assert units["盒"] == 32.0
    assert units["ml"] == 6400.0

    # 验证常规非累计每箱单数结构「4箱 * 8盒 * 200ml」依然正常连乘得 32 盒 / 6400ml
    normal_sku = {
        "pricing_basis": {
            "total_value": 6400,
            "base_unit": "ml",
            "structure": [
                {"value": 4, "unit": "箱"},
                {"value": 8, "unit": "盒"},
                {"value": 200, "unit": "ml"},
            ],
        },
        "decision_keywords": [],
    }
    _, normal_units = sched._extract_raw_units(normal_sku, context_text="纯牛奶200ml*8盒*4箱整箱")
    assert normal_units["箱"] == 4.0
    assert normal_units["盒"] == 32.0
    assert normal_units["ml"] == 6400.0


def test_zero_brand_hit_short_circuit_skips_step2_llm_and_backup_search():
    """验证主搜索返回 >=10 条泛化结果但 0 条包含候选品牌时：立即短路判定无可比，省去备用词搜索与 2 次初筛 AI，并记入零命中品牌集。"""
    saved_brands = set(sched._SESSION_XIANYU_ZERO_BRANDS)
    try:
        sched._SESSION_XIANYU_ZERO_BRANDS.clear()
        cand = _make_sample_champion(1)
        cand["brand"] = "五缘湾"
        cand["name"] = "五缘湾手抓饼原味家庭装20片1800g"
        cand["search_keyword"] = "五缘湾 手抓饼 20片"
        cand["backup_search_keyword"] = "五缘湾 手抓饼"

        fuzzy_search_res = {
            "ok": True,
            "is_offline": False,
            "is_risk_blocked": False,
            "user_data_dir": r"W:\temp\xianyu_xiaohao",
            "items": [
                {
                    "item_id": f"7700{i:02d}",
                    "title": f"思念安井手抓饼原味家庭装20片_{i}",
                    "price": 19.9,
                    "want_cnt": 2,
                    "is_free_shipping": True,
                }
                for i in range(12)
            ],
        }

        with patch.object(sched, "search_xianyu_items", return_value=fuzzy_search_res) as mock_search, patch.object(
            sched, "_filter_comparable_xianyu_items_by_llm"
        ) as mock_step2_llm, patch.object(sched, "get_xianyu_item_details_batch") as mock_batch_det:
            rec = sched.compare_candidate_with_xianyu(
                candidate=cand,
                search_user_data_dir=r"W:\temp\xianyu_xiaohao",
                guest_user_data_dir=r"W:\temp\xianyu_guest",
            )

        assert mock_search.call_count == 1
        assert mock_step2_llm.call_count == 0
        assert mock_batch_det.call_count == 0
        assert rec["final_winner"] == "no_comparable"
        assert rec["xianyu_audit_trail"]["brand_zero_short_circuit"] is True
        assert rec["xianyu_audit_trail"]["search_keyword"] == "五缘湾 手抓饼 20片"
        assert rec["llm_raw_output"]["step3_comparison_llm"]["parsed_json"] is None
        assert "五缘湾" in sched._SESSION_XIANYU_ZERO_BRANDS
        assert (
            sched._is_disqualified_source_candidate(
                {"name": "五缘湾手抓饼葱香20片", "brand": "五缘湾", "sales_tip": "已拼2000+件"},
                {},
                sched.SOURCE_TYPE_OFFICIAL,
            )
            is True
        )
    finally:
        sched._SESSION_XIANYU_ZERO_BRANDS.clear()
        sched._SESSION_XIANYU_ZERO_BRANDS.update(saved_brands)


def test_12h_search_keyword_cache_reuses_xianyu_items_without_browser_search():
    """验证 12h 内同搜索词命中 xianyu_items 概览缓存时：0 次登录小号浏览器搜索请求，直接复用概览列表。"""
    fake_items_col = _InMemoryXianyuItemsCol()
    sched._ensure_xianyu_items_indexes(fake_items_col)
    kw_idx = [
        (keys, kw)
        for keys, kw in fake_items_col.indexes
        if keys == [("last_search_keyword", 1), ("search_updated_at", -1)]
    ]
    assert len(kw_idx) == 1

    cand = _make_sample_champion(1)
    seed_cards = [
        {
            "item_id": f"66000{i}",
            "title": f"维达抽纸整箱24包缓存概览_{i}",
            "price": 22.0 + i,
            "want_cnt": 5,
            "is_free_shipping": True,
        }
        for i in range(1, 8)
    ]
    saved_cnt = sched._save_xianyu_search_items(fake_items_col, seed_cards, keyword=cand["search_keyword"])
    assert saved_cnt == 7

    with patch.object(sched, "search_xianyu_items") as mock_search, patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=([], {"model_used": "unit_test"}, "无同规格")
    ) as mock_step2:
        rec = sched.compare_candidate_with_xianyu(
            candidate={**cand, "backup_search_keyword": ""},
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
            items_col=fake_items_col,
            search_cache_hours=12,
        )

    assert mock_search.call_count == 0
    assert mock_step2.call_count == 1
    assert rec["xianyu_audit_trail"]["search_cache_hit"] is True
    assert rec["xianyu_audit_trail"]["raw_search_count"] == 7
    assert rec["llm_raw_output"]["step3_comparison_llm"]["parsed_json"] is None


def test_jit_interleaved_2_to_1_stops_remaining_stage1_ai_on_circuit_breaker():
    """验证阶段 1 按需流式选品按 2:1（线报:官价）交替产出，且中途熔断时后续分类 0 次阶段 1 AI 调用。"""
    deal_cats = [("线报分类A", [("d1", {}, 9.9)]), ("线报分类B", [("d2", {}, 9.9)]), ("线报分类C", [("d3", {}, 9.9)])]
    off_cats = [("官价分类X", [("o1", {}, 19.9)]), ("官价分类Y", [("o2", {}, 19.9)])]

    def _fake_scan(db, hours_window=72, max_categories=20, source_type=sched.SOURCE_TYPE_OFFICIAL):
        return list(deal_cats) if source_type == sched.SOURCE_TYPE_DEAL_TIP else list(off_cats)

    step1_calls = []

    def _fake_step1(cat_name, raw_items, src_type):
        step1_calls.append((src_type, cat_name))
        champ = _make_sample_champion(len(step1_calls))
        champ["source_type"] = src_type
        champ["category_name"] = cat_name
        return champ

    # 子场景 1：正常完成时严格按 2:1 (线报A -> 线报B -> 官价X -> 线报C -> 官价Y) 顺序流式产出
    with patch.object(sched, "_scan_sorted_categories_for_source", side_effect=_fake_scan), patch.object(
        sched, "_evaluate_single_category_step1", side_effect=_fake_step1
    ):
        est_total, gen = sched._iter_jit_interleaved_champions(
            db=MagicMock(),
            hours_window=72,
            deal_hours_window=48,
            enable_official=True,
            enable_deal_tip=True,
            max_categories=10,
            ratio=(2, 1),
        )
        assert est_total == 5
        assert step1_calls == []  # 构造生成器时 0 次 AI 预跑
        yielded = [c["category_name"] for c in gen]
        assert yielded == ["线报分类A", "线报分类B", "官价分类X", "线报分类C", "官价分类Y"]

    # 子场景 2：run_discovery_cycle 第 1 个分类触发风控熔断时，后续 4 个分类 0 次阶段 1 AI 调用、0 条脏数据入库
    step1_calls.clear()
    fake_db = MagicMock()
    fake_col = MagicMock()
    fake_db.get_collection.return_value = fake_col
    blocked_res = {
        "ok": False,
        "is_offline": True,
        "is_risk_blocked": True,
        "error_code": "RISK_CONTROL_BLOCKED",
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [],
        "message": "搜索触发滑块限流",
    }

    with patch.object(sched, "gen_db_object", return_value=fake_db), patch.object(
        sched, "_scan_sorted_categories_for_source", side_effect=_fake_scan
    ), patch.object(sched, "_evaluate_single_category_step1", side_effect=_fake_step1), patch.object(
        sched, "search_xianyu_items", return_value=blocked_res
    ) as mock_search:
        recs = sched.run_discovery_cycle(
            enable_deal_tip=True,
            enable_official=True,
            max_categories=10,
            deal_to_official_ratio=(2, 1),
            enable_pre_round_probe=False,
        )

    assert len(recs) == 1
    assert mock_search.call_count == 1
    assert fake_col.update_one.call_count == 0
    assert step1_calls == [(sched.SOURCE_TYPE_DEAL_TIP, "线报分类A")]
    assert sched._LAST_CYCLE_ABORTED_BY_RISK is True


def test_ephemeral_h5_session_and_guest_risk_message():
    """验证 get_xianyu_item_details_batch 默认走纯内存无痕会话，且访客限流提示不误导用户打开人工登录目录。"""
    from contextlib import contextmanager

    entered = {"ephemeral": 0, "persistent": 0}

    @contextmanager
    def _fake_ephemeral_guard(**kwargs):
        entered["ephemeral"] += 1
        raise pxy.XianyuOfflineError(
            r"W:\temp\xianyu_guest",
            "H5详情接口触发风控滑块验证: RGV587_ERROR::SM",
            error_code="RISK_CONTROL_BLOCKED",
        )
        yield  # pragma: no cover

    @contextmanager
    def _fake_persistent_guard(**kwargs):
        entered["persistent"] += 1
        yield MagicMock(), MagicMock(), {"failed_apis": []}  # pragma: no cover

    with patch.object(pxy, "_ephemeral_h5_session_guard", side_effect=_fake_ephemeral_guard), patch.object(
        pxy, "_browser_session_guard", side_effect=_fake_persistent_guard
    ):
        res = pxy.get_xianyu_item_details_batch(["880001", "880002"])

    assert entered["ephemeral"] == 1
    assert entered["persistent"] == 0
    assert res["ok"] is False
    assert res["is_risk_blocked"] is True
    assert res["is_offline"] is False
    assert "open_browser_for_manual_use" not in res["message"]
    assert "纯内存访客会话无需人工打开浏览器目录" in res["message"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_funcs = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in test_funcs:
        fn()
        print(f"PASSED: {fn.__name__}")
    print(f"\nALL {len(test_funcs)} TESTS PASSED!")
