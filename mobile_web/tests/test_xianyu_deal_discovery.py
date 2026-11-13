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

# 单元测试期间移除生产日志文件 Handler，避免把 Mock 测试日志写入真实业务日志
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


def _make_sample_official_doc(
    *,
    product_id: str,
    category_name: str,
    name: str,
    brand: str = "维达",
    activity_price: float = 19.9,
):
    return {
        "platform": "pdd",
        "product_id": product_id,
        "goods_id": product_id,
        "goods_sign": f"sign_{product_id}",
        "name": name,
        "brand": brand,
        "activity_price": activity_price,
        "price": activity_price,
        "format_status": "success",
        "updated_at": datetime.datetime.now(datetime.timezone.utc),
        "format_info": {
            "default": {
                "price": activity_price,
                "core_entities": [{"name": category_name, "score": 10}],
                "decision_keywords": [{"attribute_name": "品牌", "attribute_value": brand}],
                "pricing_basis": {
                    "is_inferred": False,
                    "structure": [{"value": 24, "unit": "包"}],
                    "total_value": 24.0,
                    "base_unit": "包",
                },
            }
        },
    }


def _make_sample_candidate(idx: int = 1, *, with_keyword: bool = True, source_type: str = sched.SOURCE_TYPE_OFFICIAL) -> dict:
    return {
        "source_type": source_type,
        "platform": "pdd",
        "product_id": f"10000{idx}",
        "goods_sign": f"sign_{idx}",
        "channel_type": 1,
        "category_name": f"测试分类{idx}",
        "category_item_count": 10,
        "name": f"维达抽纸整箱24包_{idx}",
        "outer_name": "",
        "brand": "维达",
        "db_activity_price": 19.9,
        "activity_price": 19.9,
        "original_price": 29.9,
        "primary_unit": "包",
        "unit_quantities": {"包": 24.0},
        "unit_cost_performance": {"包": round(24.0 / 19.9, 6)},
        "decision_keywords_summary": "品牌:维达",
        "pricing_basis": {"base_unit": "包", "total_value": 24.0},
        "search_keyword": f"维达 抽纸 24包 {idx}" if with_keyword else "",
        "backup_search_keyword": f"维达 抽纸 {idx}" if with_keyword else "",
        "selection_reason": "测试标品" if with_keyword else "",
        "llm_keyword_output": {},
    }


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


def test_prompts_contain_no_concrete_examples():
    """验证三个大模型提示词均采用纯抽象原则表达，不含具体举例词。"""
    prompt_paths = [
        sched.KEYWORD_PROMPT_PATH,
        sched.COARSE_FILTER_PROMPT_PATH,
        sched.COMPARISON_PROMPT_PATH,
    ]
    forbidden_markers = ("例如", "比如", "举例", "如：", "如“", '如"', "譬如")
    for p in prompt_paths:
        assert os.path.isfile(p), f"提示词文件不存在: {p}"
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        assert len(txt) > 200
        for marker in forbidden_markers:
            assert marker not in txt, f"提示词 {os.path.basename(p)} 中不应出现具体举例标记 '{marker}'"


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


def test_category_eval_count_rotation_official_price_filter_and_1to1_interleave():
    """验证按类别已比价次数升序轮转排序、正式商品实时价格暴涨淘汰（含单商品分类）、线报免覆盖，以及 1:1 交替编排。"""
    # 模拟评估库历史记录：「洗衣液」已比价 3 次，「抽纸」已比价 1 次，「纯牛奶」已比价 0 次，「洗发水」已比价 0 次但实时价格暴涨失效
    eval_history = [
        {"platform": "pdd", "product_id": "old_1", "category_name": "洗衣液", "source_type": "official"},
        {"platform": "pdd", "product_id": "old_2", "category_name": "洗衣液", "source_type": "official"},
        {"platform": "pdd", "product_id": "old_3", "category_name": "洗衣液", "source_type": "official"},
        {"platform": "pdd", "product_id": "old_4", "category_name": "抽纸", "source_type": "official"},
        {"platform": "pdd", "product_id": "already_done", "category_name": "纯牛奶", "source_type": "official"},
    ]
    fake_eval_col = MagicMock()
    fake_eval_col.find.return_value = eval_history
    fake_db = MagicMock()
    fake_db.get_collection.return_value = fake_eval_col

    raw_official_docs = [
        _make_sample_official_doc(product_id="already_done", category_name="纯牛奶", name="蒙牛纯牛奶250ml*24盒", brand="蒙牛", activity_price=39.9),
        _make_sample_official_doc(product_id="milk_new", category_name="纯牛奶", name="伊利纯牛奶250ml*24盒", brand="伊利", activity_price=41.9),
        _make_sample_official_doc(product_id="shampoo_spiked", category_name="洗发水", name="海飞丝洗发水500g", brand="海飞丝", activity_price=19.9),
        _make_sample_official_doc(product_id="tissue_new", category_name="抽纸", name="维达抽纸24包", brand="维达", activity_price=19.9),
        _make_sample_official_doc(product_id="detergent_new", category_name="洗衣液", name="蓝月亮洗衣液4kg", brand="蓝月亮", activity_price=29.9),
    ]

    fake_repo = MagicMock()
    fake_repo._COST_PERFORMANCE_PROJECTION = {"name": 1}
    fake_repo._boom_content_filter.return_value = {}
    fake_cursor = MagicMock()
    fake_cursor.sort.return_value = raw_official_docs
    fake_repo.col.find.return_value = fake_cursor

    def _fake_verify_live_price(platform, product_id, goods_sign, fallback_price):
        if product_id == "shampoo_spiked":
            return 59.9  # 较入库价 19.9 暴涨 >30%，即使该类别仅 1 款商品也必须因优惠失效淘汰
        return fallback_price

    with patch.object(sched, "ProductRepository", return_value=fake_repo), patch.object(
        sched, "_verify_live_activity_price", side_effect=_fake_verify_live_price
    ):
        off_cands = sched.select_round_candidates_for_source(
            db=fake_db,
            hours_window=72,
            max_categories=20,
            source_type=sched.SOURCE_TYPE_OFFICIAL,
        )

    # 预期顺序：0次比价的「纯牛奶」(过滤掉 already_done 选出 milk_new) -> 1次比价的「抽纸」 -> 3次比价的「洗衣液」；「洗发水」因涨价淘汰
    assert [c["category_name"] for c in off_cands] == ["纯牛奶", "抽纸", "洗衣液"]
    assert off_cands[0]["product_id"] == "milk_new"

    # 验证 1:1 交替合并
    deal_cands = [
        _make_sample_candidate(1, source_type=sched.SOURCE_TYPE_DEAL_TIP),
        _make_sample_candidate(2, source_type=sched.SOURCE_TYPE_DEAL_TIP),
    ]
    merged = sched.interleave_candidates_one_to_one(off_cands[:2], deal_cands)
    assert [c["source_type"] for c in merged] == [
        sched.SOURCE_TYPE_OFFICIAL,
        sched.SOURCE_TYPE_DEAL_TIP,
        sched.SOURCE_TYPE_OFFICIAL,
        sched.SOURCE_TYPE_DEAL_TIP,
    ]


def test_step1_single_item_eligibility_llm_and_ineligible_skip():
    """验证单商品第 1 次大模型调用：判定无资格时 0 次闲鱼搜索、0 次详情请求，直接返回 ineligible 并写入评估库防重复扫描。"""
    cand = _make_sample_candidate(1, with_keyword=False)
    cand["name"] = "散装称重无牌杂粮5斤装"
    cand["brand"] = ""

    ineligible_llm_resp = {
        "status": "成功",
        "content": json.dumps(
            {
                "is_eligible": False,
                "search_keyword": "",
                "backup_search_keyword": "",
                "reason": "无明确品牌且属散装非标品，无法在闲鱼唯一锁定同款",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_step1"},
    }

    fake_db = MagicMock()
    fake_eval_col = MagicMock()
    fake_items_col = _InMemoryXianyuItemsCol()
    fake_db.get_collection.side_effect = lambda name: fake_eval_col if name == sched.EVAL_COLLECTION_NAME else fake_items_col

    with patch.object(sched, "gen_db_object", return_value=fake_db), patch.object(
        sched, "select_round_candidates_for_source", return_value=[cand]
    ), patch.object(sched, "generate_content", return_value=ineligible_llm_resp) as mock_llm, patch.object(
        sched, "search_xianyu_items"
    ) as mock_search, patch.object(sched, "get_xianyu_item_details_batch") as mock_det:
        recs = sched.run_discovery_cycle(
            enable_official=True,
            enable_deal_tip=False,
            max_categories=20,
        )

    assert len(recs) == 1
    assert mock_llm.call_count == 1
    assert mock_search.call_count == 0
    assert mock_det.call_count == 0
    assert recs[0]["final_winner"] == "ineligible"
    assert recs[0]["xianyu_audit_trail"]["ineligible"] is True
    assert fake_eval_col.update_one.call_count == 1


def test_top2_detail_cap_and_7day_xianyu_items_cache_reuse():
    """验证默认仅保留前 2 个可比商品拉取详情，且 xianyu_items 表 7 天内详情直接复用、搜索概览入库绝不覆盖旧详情。"""
    assert sched.MAX_DETAIL_CHECKS_PER_CATEGORY == 2

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    two_days_ago = now_utc - datetime.timedelta(days=2)
    nine_days_ago = now_utc - datetime.timedelta(days=9)

    fake_items_col = _InMemoryXianyuItemsCol(
        [
            {
                "item_id": "9001",
                "has_detail": True,
                "detail_updated_at": two_days_ago,
                "detail_updated_at_str": "2026-11-11 10:00:00",
                "detail": {
                    "ok": True,
                    "item_id": "9001",
                    "title": "7天内有效缓存详情9001",
                    "desc": "维达超韧抽纸24包多规格包邮",
                    "price": 9.9,
                    "transport_fee": 0.0,
                    "is_free_shipping": True,
                    "has_multi_sku": True,
                    "min_price": 9.9,
                    "max_price": 18.5,
                    "sku_list": [
                        {"spec_text": "8包", "price": 9.9, "quantity": 5},
                        {"spec_text": "24包整箱", "price": 18.5, "quantity": 12},
                    ],
                },
            },
            {
                "item_id": "9002",
                "has_detail": True,
                "detail_updated_at": nine_days_ago,
                "detail": {
                    "ok": True,
                    "item_id": "9002",
                    "title": "超7天过期详情9002",
                    "desc": "旧详情",
                    "price": 25.0,
                    "transport_fee": 0.0,
                    "is_free_shipping": True,
                    "has_multi_sku": False,
                    "min_price": 25.0,
                    "max_price": 25.0,
                    "sku_list": [],
                },
            },
        ]
    )

    cand = _make_sample_candidate(1, with_keyword=True)
    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [
            {"item_id": f"900{i}", "title": f"维达抽纸24包_{i}", "price": 18.0 + i, "want_cnt": 5, "is_free_shipping": True}
            for i in range(1, 7)
        ],
    }
    fake_live_det = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_guest",
        "details_by_id": {
            "9002": {
                "ok": True,
                "item_id": "9002",
                "title": "刷新后的新详情9002",
                "desc": "维达抽纸24包整箱包邮",
                "price": 18.0,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "has_multi_sku": False,
                "min_price": 18.0,
                "max_price": 18.0,
                "sku_list": [],
            }
        },
    }
    fake_step3_llm = {
        "status": "成功",
        "content": json.dumps(
            {
                "has_comparable_item": True,
                "comparable_item_ids": ["9001", "9002"],
                "best_xianyu_item_id": "9002",
                "best_xianyu_price": 18.0,
                "best_xianyu_spec": "24包整箱包邮18元",
                "matched_unit": "包",
                "my_unit_quantity": 24.0,
                "my_unit_cost_performance": round(24.0 / 19.9, 6),
                "xianyu_unit_quantity": 24.0,
                "xianyu_unit_cost_performance": round(24.0 / 18.0, 6),
                "cost_performance_ratio": round((24.0 / 19.9) / (24.0 / 18.0), 4),
                "llm_winner": "xianyu_product",
                "llm_reason": "闲鱼含邮18元买24包更划算",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_step3"},
    }

    # 初筛返回 4 个 ID，但系统应仅保留前 2 个 (["9001", "9002"])；其中 9001 命中 7 天缓存免拉取，仅补拉 ["9002"]
    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res), patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(["9001", "9002", "9003", "9004"], {}, "ok")
    ), patch.object(sched, "get_xianyu_item_details_batch", return_value=fake_live_det) as mock_batch_det, patch.object(
        sched, "generate_content", return_value=fake_step3_llm
    ):
        rec = sched.compare_candidate_with_xianyu(
            candidate=cand,
            search_user_data_dir=r"W:\temp\xianyu_xiaohao",
            guest_user_data_dir=r"W:\temp\xianyu_guest",
            items_col=fake_items_col,
            detail_cache_days=7,
        )

    assert mock_batch_det.call_count == 1
    assert mock_batch_det.call_args.kwargs["item_ids"] == ["9002"]
    assert fake_items_col.docs["9001"]["has_detail"] is True
    assert len(fake_items_col.docs["9001"]["detail"]["sku_list"]) == 2
    assert fake_items_col.docs["9002"]["detail_updated_at"] > two_days_ago
    assert fake_items_col.docs["9003"]["has_detail"] is False

    audit = rec["xianyu_audit_trail"]
    assert audit["step3_detail_fetched_count"] == 2
    assert audit["step3_detail_cache_hit_count"] == 1
    assert audit["step3_detail_live_fetched_count"] == 1
    assert rec["final_winner"] == "xianyu_product"


def test_step3_llm_final_ratio_and_winner_authoritative_over_dirty_preclean():
    """验证当预清洗数据存在历史错算误差时，最终入库比例与胜负直接以第 3 次大模型独立校准结果为准。"""
    cand = _make_sample_candidate(1, with_keyword=True, source_type=sched.SOURCE_TYPE_DEAL_TIP)
    cand["name"] = "水牛奶200ml*8盒*4箱共32盒"
    cand["activity_price"] = 69.9
    # 模拟预清洗曾将 32 盒错误翻倍算成 128 盒（如果按旧本地代码覆盖会误判我方 3.69x 大胜）
    cand["unit_quantities"] = {"盒": 128.0, "ml": 25600.0}
    cand["unit_cost_performance"] = {"盒": round(128.0 / 69.9, 6), "ml": round(25600.0 / 69.9, 6)}

    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [
            {"item_id": f"8800{i}", "title": "同款水牛奶200ml*32盒整箱包邮", "price": 64.5, "want_cnt": 8, "is_free_shipping": True}
            for i in range(1, 7)
        ],
    }
    fake_det_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_guest",
        "details_by_id": {
            "88001": {
                "ok": True,
                "item_id": "88001",
                "title": "同款水牛奶200ml*32盒整箱包邮",
                "desc": "全新日期200ml*8盒*4提共32盒包邮",
                "price": 64.5,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "has_multi_sku": False,
                "min_price": 64.5,
                "max_price": 64.5,
                "sku_list": [],
            }
        },
    }
    # 第 3 次大模型根据原始标题独立校准我方真实数量为 32 盒，给出真实比例 0.9227 与 xianyu_product 胜出
    my_real_cp = round(32.0 / 69.9, 6)
    xy_real_cp = round(32.0 / 64.5, 6)
    real_ratio = round(my_real_cp / xy_real_cp, 4)
    fake_step3_llm = {
        "status": "成功",
        "content": json.dumps(
            {
                "has_comparable_item": True,
                "comparable_item_ids": ["88001"],
                "best_xianyu_item_id": "88001",
                "best_xianyu_price": 64.5,
                "best_xianyu_spec": "200ml*32盒包邮64.5元",
                "matched_unit": "盒",
                "my_unit_quantity": 32.0,
                "my_unit_cost_performance": my_real_cp,
                "xianyu_unit_quantity": 32.0,
                "xianyu_unit_cost_performance": xy_real_cp,
                "cost_performance_ratio": real_ratio,
                "llm_winner": "xianyu_product",
                "llm_reason": "标题核实我方实为32盒而非预清洗的128盒，同为32盒闲鱼64.5元优于我方69.9元",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_step3"},
    }

    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res), patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(["88001"], {}, "ok")
    ), patch.object(sched, "get_xianyu_item_details_batch", return_value=fake_det_res), patch.object(
        sched, "generate_content", return_value=fake_step3_llm
    ):
        rec = sched.compare_candidate_with_xianyu(candidate=cand)

    assert rec["comparison_basis"] == "llm_final_judgment"
    assert rec["final_winner"] == "xianyu_product"
    assert rec["is_my_product_better"] is False
    assert rec["cost_performance_ratio"] == real_ratio
    assert rec["comparison_result"]["my_unit_quantities"] == {"盒": 32.0}


def test_mid_round_circuit_breaker_stops_immediately_without_db_write():
    """验证逐商品串行闭环下，第 1 个商品搜索触发风控/掉线立即熔断，后续候选 0 次 AI/搜索请求且 0 脏数据入库。"""
    cands = [
        _make_sample_candidate(1, with_keyword=False),
        _make_sample_candidate(2, with_keyword=False),
        _make_sample_candidate(3, with_keyword=False),
    ]
    step1_ok = {
        "status": "成功",
        "content": json.dumps(
            {
                "is_eligible": True,
                "search_keyword": "维达 抽纸 24包",
                "backup_search_keyword": "维达 抽纸",
                "reason": "标准零售标品",
            },
            ensure_ascii=False,
        ),
        "metrics": {"model_used": "unit_test_step1"},
    }
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
        sched, "select_round_candidates_for_source", return_value=cands
    ), patch.object(sched, "generate_content", return_value=step1_ok) as mock_llm, patch.object(
        sched, "search_xianyu_items", return_value=blocked_res
    ) as mock_search:
        records = sched.run_discovery_cycle(
            enable_official=True,
            enable_deal_tip=False,
        )

    assert len(records) == 1
    assert mock_llm.call_count == 1
    assert mock_search.call_count == 1
    assert fake_col.update_one.call_count == 0
    assert sched._LAST_CYCLE_ABORTED_BY_RISK is True


def test_indexes_and_report_formatters():
    fake_eval_col = MagicMock()
    sched._ensure_eval_indexes(fake_eval_col)
    eval_keys = [call.args[0] for call in fake_eval_col.create_index.call_args_list]
    assert [("platform", 1), ("product_id", 1), ("source_type", 1)] in eval_keys

    fake_items_col = _InMemoryXianyuItemsCol()
    sched._ensure_xianyu_items_indexes(fake_items_col)
    item_idx_keys = [keys for keys, _ in fake_items_col.indexes]
    assert [("item_id", 1)] in item_idx_keys
    assert all("last_search_keyword" not in str(k) for k in item_idx_keys)

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
            "comparison_basis": "llm_final_judgment",
            "matched_unit": "ml",
            "cost_performance_ratio": 1.12,
            "my_unit_quantities": {"ml": 60.0},
            "my_unit_cost_performance": {"ml": 0.9091},
            "xianyu_unit_quantities": {"ml": 30.0},
            "xianyu_unit_cost_performance": {"ml": 0.8108},
            "llm_reason": "线报单价更优",
        },
        "xianyu_audit_trail": {"search_keyword": "瑷尔博士 精华油", "search_ok": True, "batch_detail_ok": True},
    }
    card = sched._format_single_comparison_card(1, 2, base_rec)
    assert "[线报爆料]" in card
    assert "💡 线报优惠方案 : 领券+多人团" in card

    inel_rec = {
        **base_rec,
        "source_type": sched.SOURCE_TYPE_OFFICIAL,
        "final_winner": "ineligible",
        "comparison_result": {"has_comparable_item": False, "llm_reason": "[不具备比价资格] 散装非标品"},
        "xianyu_audit_trail": {"ineligible": True, "search_ok": True, "batch_detail_ok": True},
    }
    summary = sched._format_batch_summary_report("disc_test", 12.3, [inel_rec, base_rec])
    assert "⏭️ 无资格跳过: 1" in summary
    assert "🏆 我方胜出: 1" in summary


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

    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?jsv=2.7.2",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    assert any("pc.search" in r for r in tracker["expired_rets"])

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

    _emit(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    _emit(
        "https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/",
        ["FAIL_SYS_SESSION_EXPIRED::Session过期"],
    )
    assert len(tracker["expired_rets"]) == 2

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


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_funcs = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in test_funcs:
        fn()
        print(f"PASSED: {fn.__name__}")
    print(f"\nALL {len(test_funcs)} TESTS PASSED!")
