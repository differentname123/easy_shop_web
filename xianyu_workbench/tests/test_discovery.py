# -*- coding: utf-8 -*-
import datetime
import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKBENCH_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_WORKBENCH_DIR)
_MOBILE_WEB_DIR = os.path.join(_REPO_ROOT, "mobile_web")
for _p in (_REPO_ROOT, _MOBILE_WEB_DIR):
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

from xianyu_workbench.client import market as xianyu_market
from xianyu_workbench.client import session as xianyu_session
from xianyu_workbench import discovery as sched


def _make_sample_deal_doc(
    *,
    product_id: str = "848349275560",
    outer_price: float = 76.0,
    deal_price: float = 66.0,
    format_deal_price: float = 66.0,
    publish_time: str = "2026-11-12 12:00:00",
    need_reformat: bool = False,
    format_status: str = "success",
    reliability_score: int = 10,
    price_is_reliable=True,
    category_name: str = "面部精华油",
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
            "reliability_score": reliability_score,
            "price_is_reliable": price_is_reliable,
            "clean_title": "瑷尔博士2.0摇醒精华油30ml*2瓶",
            "deal_price": deal_price,
            "format_deal_price": format_deal_price,
            "format_status": format_status,
            "need_reformat": need_reformat,
            "publish_time": publish_time,
            "price_logic": "领券+百亿补贴多人团",
            "format_info": {
                "sku_id": f"{product_id}_deal",
                "price": format_deal_price,
                "core_entities": [{"name": category_name, "score": 10}],
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

    @staticmethod
    def _apply_set_dict(target, set_map):
        for k, v in set_map.items():
            if "." in k:
                cur = target
                parts = k.split(".")
                for part in parts[:-1]:
                    nxt = cur.get(part)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        cur[part] = nxt
                    cur = nxt
                cur[parts[-1]] = v
            else:
                target[k] = v

    @staticmethod
    def _assert_no_path_conflict(set_keys, insert_keys) -> None:
        for sk in set_keys:
            for ik in insert_keys:
                if sk == ik or sk.startswith(ik + ".") or ik.startswith(sk + "."):
                    raise ValueError(
                        f"MongoDB code=40 path conflict between $set ({sk}) and $setOnInsert ({ik})"
                    )

    def bulk_write(self, ops, ordered=False):
        for op in ops:
            set_map = dict(op._doc.get("$set") or {})
            insert_map = dict(op._doc.get("$setOnInsert") or {})
            if set_map and insert_map:
                self._assert_no_path_conflict(set_map.keys(), insert_map.keys())
            iid = str(op._filter["item_id"])
            is_insert = iid not in self.docs
            if not is_insert or op._upsert:
                target = self.docs.setdefault(iid, {})
                if is_insert and insert_map:
                    self._apply_set_dict(target, insert_map)
                if set_map:
                    self._apply_set_dict(target, set_map)
                if "$push" in op._doc:
                    for field, push_spec in op._doc["$push"].items():
                        arr = list(target.get(field) or [])
                        if isinstance(push_spec, dict) and "$each" in push_spec:
                            arr.extend(push_spec["$each"])
                            sl = push_spec.get("$slice")
                            if isinstance(sl, int) and sl < 0:
                                arr = arr[sl:]
                        else:
                            arr.append(push_spec)
                        target[field] = arr

    def find(self, query, projection=None):
        id_filter = set((query.get("item_id") or {}).get("$in") or [])
        req_has_detail = query.get("has_detail")
        matched = []
        for iid, doc in self.docs.items():
            if id_filter and iid not in id_filter:
                continue
            if req_has_detail is not None and bool(doc.get("has_detail")) != bool(req_has_detail):
                continue
            row = dict(doc)
            if isinstance(projection, dict) and projection:
                snap_spec = projection.get("snapshots")
                if isinstance(snap_spec, dict) and isinstance(snap_spec.get("$slice"), int) and "snapshots" in row:
                    sl = snap_spec["$slice"]
                    snaps = list(row.get("snapshots") or [])
                    row["snapshots"] = snaps[sl:] if sl < 0 else snaps[:sl]
            matched.append(row)
        return matched


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

    doc_epoch = _make_sample_deal_doc(publish_time="")
    doc_epoch["deal_info"]["publish_ts"] = datetime.datetime(2026, 11, 12, 4, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    vdoc_epoch = sched._build_deal_tip_virtual_doc(doc_epoch, cutoff_utc=cutoff_48h)
    assert vdoc_epoch is not None
    assert vdoc_epoch["deal_publish_time"] == "2026-11-12 12:00"

    doc_expired = _make_sample_deal_doc(publish_time="2026-11-09 10:00:00")
    assert sched._build_deal_tip_virtual_doc(doc_expired, cutoff_utc=cutoff_48h) is None

    doc_mismatch = _make_sample_deal_doc(deal_price=55.0, format_deal_price=66.0)
    assert sched._build_deal_tip_virtual_doc(doc_mismatch, cutoff_utc=cutoff_48h) is None

    # 可靠性拦截：仅当可靠分>=20或价格核验可靠之一满足才可通过
    doc_src_rel = _make_sample_deal_doc(reliability_score=20, price_is_reliable=False)
    assert sched._build_deal_tip_virtual_doc(doc_src_rel, cutoff_utc=cutoff_48h) is not None
    doc_unreliable_ff = _make_sample_deal_doc(reliability_score=10, price_is_reliable=False)
    assert sched._build_deal_tip_virtual_doc(doc_unreliable_ff, cutoff_utc=cutoff_48h) is None
    doc_unreliable_fn = _make_sample_deal_doc(reliability_score=10, price_is_reliable=None)
    assert sched._build_deal_tip_virtual_doc(doc_unreliable_fn, cutoff_utc=cutoff_48h) is None


def test_category_eval_count_rotation_official_price_filter_and_1to1_interleave():
    """验证按类别已比价次数升序轮转排序、正式商品实时价格暴涨淘汰（含单商品分类）、线报免覆盖，以及 1:1 交替编排。"""
    # 模拟评估库历史记录：「洗衣液」已比价 3 次，「抽纸」已比价 1 次，「纯牛奶」已比价 0 次，「洗发水」已比价 0 次但实时价格暴涨失效
    eval_cat_agg = [
        {"_id": {"category_name": "洗衣液", "source_type": "official"}, "count": 3},
        {"_id": {"category_name": "抽纸", "source_type": "official"}, "count": 1},
        {"_id": {"category_name": "纯牛奶", "source_type": "official"}, "count": 1},
    ]
    fake_eval_col = MagicMock()
    fake_eval_col.aggregate.return_value = eval_cat_agg
    fake_eval_col.find.return_value = [
        {"platform": "pdd", "product_id": "already_done", "source_type": "official"}
    ]
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

    def _fake_batch_verify(items):
        return {
            (sched.normalize_platform(plat), str(pid)): _fake_verify_live_price(plat, pid, gsign, fb_p)
            for plat, pid, gsign, fb_p in items
        }

    with patch.object(sched, "ProductRepository", return_value=fake_repo), patch.object(
        sched, "_verify_live_activity_price", side_effect=_fake_verify_live_price
    ), patch.object(sched, "_batch_verify_live_activity_prices", side_effect=_fake_batch_verify):
        off_cands = sched.select_round_candidates_for_source(
            db=fake_db,
            hours_window=72,
            max_categories=20,
            source_type=sched.SOURCE_TYPE_OFFICIAL,
        )

    # 预期顺序：0次比价的「纯牛奶」(过滤掉 already_done 选出 milk_new) -> 1次比价的「抽纸」 -> 3次比价的「洗衣液」；「洗发水」因涨价淘汰
    assert [c["category_name"] for c in off_cands] == ["纯牛奶", "抽纸", "洗衣液"]
    assert off_cands[0]["product_id"] == "milk_new"
    assert fake_eval_col.aggregate.call_count == 1
    assert fake_eval_col.find.call_count == 1
    eval_find_filter = fake_eval_col.find.call_args.args[0]
    assert "product_id" in eval_find_filter and "$in" in eval_find_filter["product_id"]
    assert set(eval_find_filter["product_id"]["$in"]) == {
        "already_done", "milk_new", "shampoo_spiked", "tissue_new", "detergent_new"
    }

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
    """验证按 MAX_DETAIL_CHECKS_PER_CATEGORY 截断可比商品详情拉取，且 xianyu_items 表 7 天内详情直接复用、搜索概览绝不覆盖旧详情。"""
    cap = sched.MAX_DETAIL_CHECKS_PER_CATEGORY
    assert cap >= 2

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    two_days_ago = now_utc - datetime.timedelta(days=2)
    nine_days_ago = now_utc - datetime.timedelta(days=9)

    fake_items_col = _InMemoryXianyuItemsCol(
        [
            {
                "item_id": "9001",
                "has_detail": True,
                "detail_updated_at": two_days_ago,
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
    candidate_ids = [f"900{i}" for i in range(1, cap + 3)]
    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "user_data_dir": r"W:\temp\xianyu_xiaohao",
        "items": [
            {"item_id": f"900{i}", "title": f"维达抽纸24包_{i}", "price": 18.0 + i, "want_cnt": 5, "is_free_shipping": True}
            for i in range(1, cap + 5)
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

    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res), patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(candidate_ids, {}, "ok")
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
    assert mock_batch_det.call_args.kwargs["item_ids"] == [i for i in candidate_ids[:cap] if i != "9001"]
    assert fake_items_col.docs["9001"]["has_detail"] is True
    assert len(fake_items_col.docs["9001"]["detail"]["sku_list"]) == 2
    assert fake_items_col.docs["9002"]["detail_updated_at"] > two_days_ago
    assert fake_items_col.docs[f"900{cap + 1}"]["has_detail"] is False

    audit = rec["xianyu_audit_trail"]
    assert audit["step3_detail_fetched_count"] == cap
    assert audit["step3_detail_cache_hit_count"] == 1
    assert audit["step3_detail_live_fetched_count"] == cap - 1
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

    try:
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
    finally:
        sched._LAST_CYCLE_ABORTED_BY_RISK = False


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
    assert "线报爆料" in card
    assert "领券+多人团" in card

    inel_rec = {
        **base_rec,
        "source_type": sched.SOURCE_TYPE_OFFICIAL,
        "final_winner": "ineligible",
        "comparison_result": {"has_comparable_item": False, "llm_reason": "[不具备比价资格] 散装非标品"},
        "xianyu_audit_trail": {"ineligible": True, "search_ok": True, "batch_detail_ok": True},
    }
    summary = sched._format_batch_summary_report("disc_test", 12.3, [inel_rec, base_rec])
    assert "无资格" in summary and "1" in summary
    assert "我方胜出" in summary


def test_auth_tracker_search_vs_h5_guest_detail_detection():
    fake_page = MagicMock()
    fake_page.frames = []
    fake_page.url = "https://h5.m.goofish.com/item?id=780427279990"
    tracker = xianyu_session._attach_auth_tracker(fake_page)
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
    xianyu_market._verify_guest_no_risk_block(fake_page, tracker, r"W:\temp\xianyu_guest", task_name="unit_test")

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
        xianyu_market._verify_guest_no_risk_block(fake_page, tracker, r"W:\temp\xianyu_guest", task_name="unit_test")
    except xianyu_session.XianyuOfflineError as err:
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
    parsed = xianyu_market._parse_detail_packet_to_dict(
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
        xianyu_market,
        "get_xianyu_item_details_batch",
        return_value={"ok": True, "details_by_id": {"780427279990": parsed}},
    ) as mock_batch:
        single_res = xianyu_market.get_xianyu_item_detail("https://www.goofish.com/item?id=780427279990")
    assert mock_batch.call_count == 1
    assert mock_batch.call_args.kwargs["item_ids"] == ["780427279990"]
    assert single_res["item_id"] == "780427279990"


def test_havana_silent_sso_cold_start_clears_transient_session_expired():
    fake_page = MagicMock()
    fake_page.frames = []
    fake_page.url = "https://www.goofish.com/"
    fake_ctx = MagicMock()

    tracker = xianyu_session._attach_auth_tracker(fake_page)
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
    unb = xianyu_session._verify_account_online(fake_ctx, fake_page, tracker, r"W:\temp\xianyu_xiaohao", task_name="cold_start_test")
    assert unb == "4229872747"
    assert tracker["expired_rets"] == []


def test_ephemeral_h5_session_and_guest_risk_message():
    """验证 get_xianyu_item_details_batch 默认走纯内存无痕会话，且访客限流提示不误导用户打开人工登录目录。"""
    from contextlib import contextmanager

    entered = {"ephemeral": 0, "persistent": 0}

    @contextmanager
    def _fake_ephemeral_guard(**kwargs):
        entered["ephemeral"] += 1
        raise xianyu_session.XianyuOfflineError(
            r"W:\temp\xianyu_guest",
            "H5详情接口触发风控滑块验证: RGV587_ERROR::SM",
            error_code="RISK_CONTROL_BLOCKED",
        )
        yield  # pragma: no cover

    @contextmanager
    def _fake_persistent_guard(**kwargs):
        entered["persistent"] += 1
        yield MagicMock(), MagicMock(), {"failed_apis": []}  # pragma: no cover

    with patch.object(xianyu_market, "_ephemeral_h5_session_guard", side_effect=_fake_ephemeral_guard), patch.object(
        xianyu_market, "_browser_session_guard", side_effect=_fake_persistent_guard
    ):
        res = xianyu_market.get_xianyu_item_details_batch(["880001", "880002"])

    assert entered["ephemeral"] == 1
    assert entered["persistent"] == 0
    assert res["ok"] is False
    assert res["is_risk_blocked"] is True
    assert res["is_offline"] is False
    assert "open_browser_for_manual_use" not in res["message"]
    assert "纯内存" in res["message"] and "无需" in res["message"]


def test_xianyu_round_pool_lazy_init_and_discovery_cycle_closes_db():
    assert not any(isinstance(h, logging.FileHandler) for h in sched.logger.handlers)
    with patch.object(xianyu_session, "sync_playwright", side_effect=AssertionError("should not start eagerly")):
        with xianyu_session.XianyuRoundBrowserPool() as pool:
            assert pool._pw is None

    fake_db = MagicMock()
    fake_db.get_collection.return_value = MagicMock()
    with patch.object(sched, "gen_db_object", return_value=fake_db), patch.object(
        sched, "select_round_candidates_for_source", return_value=[]
    ):
        sched.run_discovery_cycle(enable_official=True, enable_deal_tip=False)

    assert fake_db.close.call_count == 1


def test_xianyu_discovery_transient_llm_outage_never_blacklists_candidate(monkeypatch):
    cand = _make_sample_candidate(1, with_keyword=False)
    monkeypatch.setattr(
        sched,
        "_call_llm_json",
        lambda **kwargs: (False, {}, {"model_used": "mock", "attempt_used": 3}, "504 Gateway Timeout"),
    )

    rec = sched.compare_candidate_with_xianyu(
        candidate=cand,
        search_user_data_dir="dummy_search",
        guest_user_data_dir="dummy_guest",
    )
    assert rec["final_winner"] == "llm_error"
    assert rec["xianyu_audit_trail"]["llm_aborted"] is True
    assert rec["xianyu_audit_trail"]["ineligible"] is False

    fake_eval_col = MagicMock()
    fake_items_col = MagicMock()
    fake_db = MagicMock()
    fake_db.get_collection.side_effect = lambda name: (
        fake_eval_col if name == sched.EVAL_COLLECTION_NAME else fake_items_col
    )
    monkeypatch.setattr(sched, "gen_db_object", lambda: fake_db)
    monkeypatch.setattr(
        sched,
        "select_round_candidates_for_source",
        lambda **kwargs: [cand] if kwargs.get("source_type") == sched.SOURCE_TYPE_OFFICIAL else [],
    )

    records = sched.run_discovery_cycle(max_categories=1)
    assert len(records) == 1
    assert records[0]["xianyu_audit_trail"]["llm_aborted"] is True
    fake_eval_col.update_one.assert_not_called()


def test_xianyu_batch_verify_live_activity_prices_chunks_of_20(monkeypatch):
    """验证官方批量验价按每批 <=20 条聚合调用 adapter.batch_get_goods_details 并隔离下架商品。"""
    calls = []

    class FakeAdapter:
        def batch_get_goods_details(self, refs):
            calls.append(len(refs))
            out = {}
            for r in refs:
                if r.goods_id == "p_off":
                    out[r.key] = {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION"}
                else:
                    out[r.key] = {"status": "success", "data": {"price": 25.5}}
            return out

    monkeypatch.setattr(sched, "get_adapter", lambda _plat: FakeAdapter())
    items = [("jd", f"p_{i}", f"sign_{i}", 20.0) for i in range(24)]
    items.append(("jd", "p_off", "sign_off", 20.0))
    res = sched._batch_verify_live_activity_prices(items)
    assert calls == [20, 5]
    assert res[("jd", "p_0")] == 25.5
    assert res[("jd", "p_23")] == 25.5
    assert res[("jd", "p_off")] is None


def test_select_round_candidates_filters_unreliable_products():
    """验证闲鱼比价候选选拔（线报与官方双源）均在 Mongo 查询条件与内存筛选中强制仅入选可靠商品。"""
    expected_reliable_filter = sched.reliable_product_mongo_filter()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    recent_pub = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    # 1) 线报源：仅可靠分>=20或价格核验可靠通过，(10,F) 被过滤
    deal_docs = [
        _make_sample_deal_doc(
            product_id="deal_unreliable",
            category_name="面霜",
            publish_time=recent_pub,
            reliability_score=10,
            price_is_reliable=False,
        ),
        _make_sample_deal_doc(
            product_id="deal_src_ok",
            category_name="精华液",
            publish_time=recent_pub,
            reliability_score=20,
            price_is_reliable=False,
        ),
        _make_sample_deal_doc(
            product_id="deal_price_ok",
            category_name="防晒霜",
            publish_time=recent_pub,
            reliability_score=10,
            price_is_reliable=True,
        ),
    ]
    fake_eval_col = MagicMock()
    fake_eval_col.aggregate.return_value = []
    fake_eval_col.find.return_value = []
    fake_db = MagicMock()
    fake_db.get_collection.return_value = fake_eval_col

    fake_repo = MagicMock()
    fake_repo._SUMMARY_PROJECTION = {"name": 1}
    fake_repo._COST_PERFORMANCE_PROJECTION = {"name": 1}
    fake_repo._boom_content_filter.return_value = {}
    fake_deal_cursor = MagicMock()
    fake_deal_cursor.sort.return_value = deal_docs
    fake_repo.col.find.return_value = fake_deal_cursor

    with patch.object(sched, "ProductRepository", return_value=fake_repo):
        deal_cands = sched.select_round_candidates_for_source(
            db=fake_db,
            hours_window=48,
            max_categories=20,
            source_type=sched.SOURCE_TYPE_DEAL_TIP,
        )

    assert [c["product_id"] for c in deal_cands] == ["deal_src_ok", "deal_price_ok"]
    deal_query, deal_proj = fake_repo.col.find.call_args.args
    assert expected_reliable_filter in deal_query["$and"]
    assert deal_proj["deal_info.is_deal_tip"] == 1
    assert deal_proj["deal_info.reliability_score"] == 1
    assert deal_proj["deal_info.price_is_reliable"] == 1

    # 2) 官方源：纯官方商品与附带可靠线报的商品通过，附带不可靠活跃线报的商品被排除
    off_pure = _make_sample_official_doc(product_id="off_pure", category_name="抽纸", name="维达抽纸24包")
    off_with_reliable_deal = _make_sample_official_doc(
        product_id="off_rel_deal", category_name="洗衣液", name="蓝月亮洗衣液"
    )
    off_with_reliable_deal["deal_info"] = {
        "is_deal_tip": True,
        "reliability_score": 20,
        "price_is_reliable": False,
    }
    off_with_unreliable_deal = _make_sample_official_doc(
        product_id="off_unrel_deal", category_name="牙膏", name="云南白药牙膏"
    )
    off_with_unreliable_deal["deal_info"] = {
        "is_deal_tip": True,
        "reliability_score": 10,
        "price_is_reliable": False,
    }

    fake_off_cursor = MagicMock()
    fake_off_cursor.sort.return_value = [off_pure, off_with_reliable_deal, off_with_unreliable_deal]
    fake_repo.col.find.reset_mock()
    fake_repo.col.find.return_value = fake_off_cursor

    with patch.object(sched, "ProductRepository", return_value=fake_repo), patch.object(
        sched, "_batch_verify_live_activity_prices", side_effect=lambda items: {(sched.normalize_platform(p), str(pid)): fb for p, pid, _gs, fb in items}
    ), patch.object(
        sched, "_verify_live_activity_price", side_effect=lambda _p, _pid, _gs, fb: fb
    ):
        off_cands = sched.select_round_candidates_for_source(
            db=fake_db,
            hours_window=72,
            max_categories=20,
            source_type=sched.SOURCE_TYPE_OFFICIAL,
        )

    assert [c["product_id"] for c in off_cands] == ["off_pure", "off_rel_deal"]
    off_query, off_proj = fake_repo.col.find.call_args.args
    assert expected_reliable_filter in off_query["$and"]
    assert off_proj["deal_info.is_deal_tip"] == 1
    assert off_proj["deal_info.reliability_score"] == 1
    assert off_proj["deal_info.price_is_reliable"] == 1


def test_h5_batch_detail_captures_screenshot_non_fatally_and_forwards_to_eval(tmp_path):
    from contextlib import contextmanager
    from pathlib import Path

    # 1) 验证 get_xianyu_item_details_batch 在 screenshot_dir 非空时保存 {item_id}.jpg 且截屏失败不阻断详情
    fake_page = MagicMock()
    fake_ctx = MagicMock()
    tracker = {"failed_apis": [], "risk_rets": [], "guest_login_blocked_rets": []}

    def _fake_goto(url, **_kwargs):
        # 找到绑定的 response 监听器并喂入成功详情数据包
        iid = url.split("id=")[-1]
        pkt = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "itemDO": {
                    "title": f"闲鱼商品_{iid}",
                    "desc": "全新未拆包邮",
                    "soldPrice": "35.00",
                    "transportFee": "0.00",
                    "imageInfos": [{"url": f"https://img.goofish.com/{iid}.jpg"}],
                },
                "sellerDO": {"nick": "卖家A"},
            },
        }
        fake_resp = MagicMock()
        fake_resp.url = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.awesome.detail/1.0/"
        fake_resp.json.return_value = pkt
        for call in fake_page.on.call_args_list:
            if call.args[0] == "response":
                call.args[1](fake_resp)

    fake_page.goto.side_effect = _fake_goto

    shot_calls = []

    def _fake_screenshot(*, path, **_kwargs):
        shot_calls.append(path)
        if "880002" in path:
            raise RuntimeError("mock screenshot error")
        Path(path).write_bytes(b"\xff\xd8\xff\xe0mock-shot")

    fake_page.screenshot.side_effect = _fake_screenshot

    @contextmanager
    def _fake_ephemeral_guard(**_kwargs):
        yield fake_ctx, fake_page, tracker

    with patch.object(xianyu_market, "_ephemeral_h5_session_guard", side_effect=_fake_ephemeral_guard):
        batch_res = xianyu_market.get_xianyu_item_details_batch(
            ["880001", "880002"],
            screenshot_dir=str(tmp_path),
        )

    assert batch_res["ok"] is True
    assert batch_res["details_by_id"]["880001"]["ok"] is True
    assert batch_res["details_by_id"]["880001"]["screenshot_file"] == "880001.jpg"
    assert (tmp_path / "880001.jpg").is_file()
    assert batch_res["details_by_id"]["880002"]["ok"] is True
    assert "screenshot_file" not in batch_res["details_by_id"]["880002"]
    assert len(shot_calls) == 2

    # 2) 验证 compare_candidate_with_xianyu 将 screenshot_file 与 images 写入 best_xianyu_product 且不污染第 3 步 LLM 输入
    cand = _make_sample_candidate(1, with_keyword=True, source_type=sched.SOURCE_TYPE_DEAL_TIP)
    captured_prompts = []

    def _fake_step3_llm(prompt, **_kwargs):
        captured_prompts.append(prompt)
        return {
            "status": "成功",
            "content": json.dumps(
                {
                    "has_comparable_item": True,
                    "comparable_item_ids": ["880001"],
                    "best_xianyu_item_id": "880001",
                    "best_xianyu_price": 35.0,
                    "best_xianyu_spec": "24包包邮35元",
                    "matched_unit": "包",
                    "my_unit_quantity": 24.0,
                    "my_unit_cost_performance": round(24.0 / 19.9, 6),
                    "xianyu_unit_quantity": 24.0,
                    "xianyu_unit_cost_performance": round(24.0 / 35.0, 6),
                    "cost_performance_ratio": 1.7588,
                    "llm_winner": "my_product",
                    "llm_reason": "我方19.9元明显优于闲鱼35元",
                },
                ensure_ascii=False,
            ),
            "metrics": {"model_used": "unit_test_step3"},
        }

    fake_search_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "items": [{"item_id": "880001", "title": "维达抽纸24包", "price": 35.0, "want_cnt": 12, "is_free_shipping": True}],
    }
    fake_det_res = {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "details_by_id": {
            "880001": {
                "ok": True,
                "item_id": "880001",
                "title": "维达抽纸24包",
                "desc": "全新包邮",
                "price": 35.0,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "has_multi_sku": False,
                "min_price": 35.0,
                "max_price": 35.0,
                "sku_list": [],
                "want_cnt": 12,
                "images": ["https://img.goofish.com/880001.jpg"],
                "screenshot_file": "880001.jpg",
            }
        },
    }

    with patch.object(sched, "search_xianyu_items", return_value=fake_search_res), patch.object(
        sched, "_filter_comparable_xianyu_items_by_llm", return_value=(["880001"], {}, "ok")
    ), patch.object(sched, "get_xianyu_item_details_batch", return_value=fake_det_res), patch.object(
        sched, "generate_content", side_effect=_fake_step3_llm
    ):
        rec = sched.compare_candidate_with_xianyu(candidate=cand, screenshot_dir=str(tmp_path))

    assert rec["best_xianyu_product"]["screenshot_file"] == "880001.jpg"
    assert rec["best_xianyu_product"]["images"] == ["https://img.goofish.com/880001.jpg"]
    assert len(captured_prompts) == 1
    assert "screenshot_file" not in captured_prompts[0]


def test_workbench_entrypoints_resist_pycharm_pythonpath_shadowing():
    import subprocess

    repo_root = _REPO_ROOT
    workbench_dir = _WORKBENCH_DIR
    mobile_web_dir = _MOBILE_WEB_DIR

    for py_path in (
        f"{repo_root}{os.pathsep}{mobile_web_dir}",
        f"{mobile_web_dir}{os.pathsep}{repo_root}",
    ):
        env = dict(os.environ, PYTHONPATH=py_path)
        for script_name in ("run_discovery.py", "xianyu_cli.py", "run.py"):
            proc = subprocess.run(
                [sys.executable, os.path.join(workbench_dir, script_name), "--help"],
                cwd=workbench_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert proc.returncode == 0, (
                f"{script_name} failed under PYTHONPATH={py_path!r}: "
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )


def test_market_parsers_schema_v2_and_strict_nulls():
    from xianyu_workbench.client import market as xy_market

    # 1. 搜索卡片 Schema v2 解析（UTC datetime publish_at、seller_id_enc、类目、标签与排名，无冗余 *_str）
    raw_search_wrap = {
        "data": {
            "item": {
                "main": {
                    "clickParam": {
                        "args": {
                            "publishTime": "1795622954000",
                            "CoinPay_Morediscount": "true",
                            "userIsUseFishShopCard": "false",
                            "seller_id": "enc_seller_001",
                            "catId": "50025446",
                            "cCatId": "126858348",
                            "tbCatId": "50012478",
                            "tag": "freeship/credit-6",
                            "tagname": "包邮/极好",
                            "keyword": "维达抽纸",
                            "position": "0",
                        }
                    },
                    "exContent": {
                        "itemId": "1104149344866",
                        "area": "四川",
                        "picUrl": "//img.alicdn.com/test.jpg",
                        "isAliMaMaAD": False,
                        "showVideoIcon": False,
                        "userIsUseFishShopCard": True,
                        "userNickName": "测试卖家",
                        "detailParams": {
                            "itemId": "1104149344866",
                            "title": "维达抽纸24包",
                            "soldPrice": "24",
                            "picWidth": "887",
                            "picHeight": "1920",
                            "isVideo": "true",
                        },
                        "fishTags": {
                            "r3": {
                                "tagList": [
                                    {"data": {"labelId": "41", "isStrikeThrough": "true", "content": "¥50"}},
                                    {"data": {"labelId": "749", "content": "累计降价1.99元"}},
                                    {"data": {"content": "34人想要"}},
                                ]
                            },
                            "r5": {
                                "tagList": [
                                    {"data": {"labelId": "664", "content": "nfrIcon"}},
                                ]
                            },
                        },
                    },
                }
            }
        }
    }
    card = xy_market._parse_search_result_item(raw_search_wrap, keyword="维达抽纸", rank=3)
    assert card is not None
    assert isinstance(card["publish_at"], datetime.datetime)
    assert card["publish_at"].tzinfo == datetime.timezone.utc
    assert int(card["publish_at"].timestamp()) == 1795622954
    assert "publish_ts" not in card
    assert "publish_time_str" not in card
    assert card["is_free_shipping"] is True  # 由 official_tag_codes 中的 freeship 识别
    assert card["original_price"] == 50.0
    assert card["price_drop_amount"] == 1.99
    assert card["has_nfr_badge"] is True
    assert "¥50" not in card["seller_tags"]
    assert "累计降价1.99元" not in card["seller_tags"]
    assert "nfrIcon" not in card["seller_tags"]
    assert card["want_cnt"] == 34
    assert card["pic_width"] == 887
    assert card["pic_height"] == 1920
    assert card["is_video"] is True
    assert card["is_ad"] is False
    assert card["is_fish_shop"] is True
    assert card["coin_discount"] is True
    assert card["seller_id_enc"] == "enc_seller_001"
    assert card["cat_id"] == "50025446"
    assert card["channel_cat_id"] == "126858348"
    assert card["tb_cat_id"] == "50012478"
    assert card["search_keyword"] == "维达抽纸"
    assert card["search_rank"] == 3

    # 2. 详情包 Schema v2 解析（UTC datetime publish_at、嵌套 seller、严格 None 边界）
    raw_detail_pkt = {
        "data": {
            "itemDO": {
                "itemId": 1101996601108,
                "title": "闪迪内存卡",
                "desc": "公司采购整多了",
                "soldPrice": "18",
                "transportFee": "0.00",
                "priceRelativeTags": [{"text": "包邮", "trackParams": {"tagType": "baoyou"}}],
                "priceTextTags": [{"trackParams": {"tagType": "coin_pay"}}],
                "commonTags": [{"text": "包邮"}, {"text": "48小时发货"}],
                "wantCnt": 107,
                "browseCnt": 1247,
                "collectCnt": 33,
                "favorCnt": 0,
                "soldCnt": 0,
                "quantity": 98,
                "gmtCreate": 1795250507000,
                "GMT_CREATE_DATE_KEY": "2026-11-21 16:41:47",
                "itemStatus": 0,
                "itemStatusStr": "在线",
                "bargained": False,
                "titleIsUserInput": False,
                "categoryId": 50025387,
                "itemCatDTO": {"catId": 50025387, "tbCatId": 50012166, "channelCatId": 126854217},
                "itemLabelExtList": [{"valueText": "闪存卡"}, {"valueText": "Sandisk/闪迪"}],
                "imageInfos": [
                    {"url": "//img.alicdn.com/1.jpg", "major": True, "widthSize": 1080, "heightSize": 1440, "type": 0},
                    {"url": "//img.alicdn.com/2.jpg", "major": False, "widthSize": 1000, "heightSize": 1000, "type": 0},
                ],
                "skuList": [
                    {
                        "inventoryId": 1138603,
                        "priceInCent": 1800,
                        "quantity": 22,
                        "propertyList": [{"propertyText": "容量", "actualValueText": "16G"}],
                        "features": {"idleOriginalQuantity": "25", "sku_order_num": "0"},
                    }
                ],
            },
            "sellerDO": {
                "sellerId": 3670364797,
                "nick": "C***e",
                "city": "深圳",
                "registerTime": 1617431685000,
                "idleFishCreditTag": {"trackParams": {"sellerLevel": "1"}},
                "hasSoldNumInteger": 1050,
                "newGoodRatioRate": "75%",
                "itemCount": 30,
                "userRegDay": 2064,
                "lastVisitTime": "7小时前来过",
                "replyIn24hRatioDouble": 0.8585,
                "replyInterval": "2小时",
                # avgReply30dLong 故意缺省，验证返回 None 而非 0
                "businessType": 2,
                "yxpPro": False,
                "zhimaAuth": True,
                "zhimaLevelInfo": {"levelName": "信用极好"},
                "remarkDO": {"sellerGoodRemarkCnt": 42, "sellerBadRemarkCnt": 14, "sellerDefaultRemarkCnt": 0},
            },
        }
    }
    det = xy_market._parse_detail_packet_to_dict(
        raw_detail_pkt,
        item_id="1101996601108",
        detail_url="https://www.goofish.com/item?id=1101996601108",
        user_data_dir="guest",
    )
    assert det["collect_cnt"] == 33
    assert det["coin_discount"] is True
    assert "favor_cnt" not in det
    assert "sold_cnt" not in det
    assert isinstance(det["publish_at"], datetime.datetime)
    assert det["publish_at"].tzinfo == datetime.timezone.utc
    assert int(det["publish_at"].timestamp()) == 1795250507
    assert "gmt_create_ts" not in det
    assert "gmt_create_str" not in det
    assert det["item_status"] == 0
    assert det["item_status_str"] == "在线"
    assert det["first_pic_width"] == 1080
    assert det["first_pic_height"] == 1440
    assert det["has_video"] is False
    assert det["item_labels"] == ["闪存卡", "Sandisk/闪迪"]
    assert det["service_tags"] == ["包邮", "48小时发货"]
    assert det["sku_list"][0]["original_quantity"] == 25
    assert det["sku_list"][0]["sku_order_num"] == 0
    assert det["seller"]["seller_id"] == "3670364797"
    assert isinstance(det["seller"]["register_at"], datetime.datetime)
    assert det["seller"]["register_at"].tzinfo == datetime.timezone.utc
    assert int(det["seller"]["register_at"].timestamp()) == 1617431685
    assert det["seller"]["seller_level"] == 1
    assert det["seller"]["item_count"] == 30
    assert det["seller"]["reg_days"] == 2064
    assert det["seller"]["reply_ratio_24h"] == 0.8585
    assert det["seller"]["avg_reply_30d_min"] is None
    assert det["seller"]["zhima_level"] == "信用极好"
    assert det["seller"]["good_remark_cnt"] == 42
    assert det["seller"]["bad_remark_cnt"] == 14


def test_save_xianyu_items_schema_v2_detail_protection_and_smart_snapshots():
    fake_col = _InMemoryXianyuItemsCol()

    # 1. 连续写入 35 次搜索（价格与想要数递增），验证严格 30 条快照截断、UTC 时间与零 *_str 冗余
    pub_search_dt = datetime.datetime(2026, 11, 25, 10, 0, 0, tzinfo=datetime.timezone.utc)
    for i in range(35):
        sched._save_xianyu_search_items(
            fake_col,
            [
                {
                    "item_id": "777001",
                    "title": "搜索态标题",
                    "price": 20.0 + i * 0.1,
                    "want_cnt": 5 + i,
                    "publish_at": pub_search_dt,
                    "pic_width": 900,
                    "pic_height": 1200,
                    "is_video": False,
                    "is_ad": False,
                    "is_fish_shop": True,
                    "coin_discount": False,
                    "seller_id_enc": "enc_s777",
                    "search_rank": i + 1,
                }
            ],
            search_keyword="测试关键字",
        )

    doc_after_search = fake_col.docs["777001"]
    assert doc_after_search["schema_version"] == 2
    assert doc_after_search["has_detail"] is False
    assert doc_after_search["publish_at"] == pub_search_dt
    assert doc_after_search["publish_at_source"] == "search"
    assert len(doc_after_search["snapshots"]) == 30

    # 2. 完全相同价格与想要数 1 分钟内再次搜索，应被智能去重不重复追加快照
    sched._save_xianyu_search_items(
        fake_col,
        [
            {
                "item_id": "777001",
                "title": "搜索态标题",
                "price": 20.0 + 34 * 0.1,
                "want_cnt": 5 + 34,
                "publish_at": pub_search_dt,
            }
        ],
        search_keyword="测试关键字",
    )
    assert len(fake_col.docs["777001"]["snapshots"]) == 30
    assert fake_col.docs["777001"]["snapshots"][-1]["source"] == "search"

    # 3. 写入权威 H5 详情（search -> detail 升级应追加快照，且 publish_at_source 切为 "detail"）
    pub_detail_dt = datetime.datetime(2026, 11, 24, 8, 30, 0, tzinfo=datetime.timezone.utc)
    sched._save_xianyu_item_details(
        fake_col,
        {
            "777001": {
                "ok": True,
                "item_id": "777001",
                "title": "权威详情标题",
                "desc": "详细描述",
                "price": 22.0,
                "transport_fee": 0.0,
                "is_free_shipping": True,
                "want_cnt": 40,
                "browse_cnt": 400,
                "quantity": 10,
                "collect_cnt": 12,
                "publish_at": pub_detail_dt,
                "has_video": True,
                "screenshot_file": "item_777001.jpg",
                "seller": {
                    "seller_id": "998877",
                    "avg_reply_30d_min": None,
                    "reply_ratio_24h": 0.95,
                },
            }
        },
    )

    doc = fake_col.docs["777001"]
    assert doc["has_detail"] is True
    assert doc["title"] == "权威详情标题"
    assert doc["price"] == 22.0
    assert doc["publish_at"] == pub_detail_dt
    assert doc["publish_at_source"] == "detail"
    assert doc["metrics"]["want_cnt"] == 40
    assert doc["metrics"]["browse_cnt"] == 400
    assert doc["metrics"]["collect_cnt"] == 12
    assert doc["metrics"]["want_rate"] == 0.1
    assert "favor_cnt" not in doc["metrics"]
    assert "sold_cnt" not in doc["metrics"]
    assert doc["seller_id"] == "998877"
    assert doc["screenshot_file"] == "item_777001.jpg"
    assert doc["detail"]["seller"]["avg_reply_30d_min"] is None
    assert doc["detail"]["seller"]["reply_ratio_24h"] == 0.95
    assert len(doc["snapshots"]) == 30
    assert doc["snapshots"][-1]["source"] == "detail"
    assert doc["snapshots"][-1]["browse_cnt"] == 400
    for forbidden in ("publish_time_str", "gmt_create_ts", "gmt_create_str", "detail_updated_at_str", "updated_at_str", "created_at_str"):
        assert forbidden not in doc

    # 4. 详情优先保护：再次搜索写入脏标题/不同价格/旧时间，绝不覆盖详情态主字段、metrics 与 detail
    sched._save_xianyu_search_items(
        fake_col,
        [
            {
                "item_id": "777001",
                "title": "搜索覆盖尝试标题",
                "price": 999.0,
                "want_cnt": 1,
                "publish_at": pub_search_dt,
                "is_ad": True,
                "search_rank": 2,
            }
        ],
        search_keyword="新关键字",
    )
    doc_protected = fake_col.docs["777001"]
    assert doc_protected["title"] == "权威详情标题"
    assert doc_protected["price"] == 22.0
    assert doc_protected["publish_at"] == pub_detail_dt
    assert doc_protected["publish_at_source"] == "detail"
    assert doc_protected["metrics"]["want_cnt"] == 40
    assert doc_protected["is_ad"] is True  # 允许刷新广告曝光/搜索卡位
    assert doc_protected["search_card"]["search_keyword"] == "新关键字"

    # 5. 验证 7 天缓存读取直接使用原生 detail_updated_at 并正确返回 screenshot_file
    cached = sched._load_cached_xianyu_details(fake_col, ["777001"], cache_days=7)
    assert cached["777001"]["screenshot_file"] == "item_777001.jpg"
    assert cached["777001"]["_from_db_cache"] is True
    assert isinstance(cached["777001"]["_detail_updated_at"], datetime.datetime)






