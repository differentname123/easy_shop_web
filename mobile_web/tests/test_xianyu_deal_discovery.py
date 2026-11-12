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

from mobile_web.scripts import run_xianyu_deal_discovery_scheduler as sched


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
