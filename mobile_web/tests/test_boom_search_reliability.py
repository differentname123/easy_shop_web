# -*- coding: utf-8 -*-
"""
单元测试：严选好价可靠优先排序与搜索重构 (`models.product` / `services.boom_search` / `ProductRepository`)。
================================================================================
覆盖范围（100% 离线）：
1. `is_product_reliable` 唯一真源与 Mongo 过滤器的等价完备性；
2. 无搜索词默认浏览的双段索引分页：纯可靠页、交界跨页去重拼接、纯不可靠尾页及多排序模式；
3. 有词搜索（Meilisearch 路径 + Mongo 降级路径，`cost_performance` 与 `latest`）：
   - 可靠商品全部优先于不可靠商品，且不隐藏任何不可靠商品；
   - 高性价比但未验证可靠的线报不会插队超越可靠商品；
   - 可靠池仅含 L3 噪音而不可靠池含 L1 正品时，L3 噪音被全局相关性拦截，不会误顶前排。
================================================================================
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import patch

from models.product import (
    is_product_reliable,
    reliable_product_mongo_filter,
    unreliable_product_mongo_filter,
)
from repositories.product_repo import ProductRepository
from tests.test_product_repository import MemoryCollection


def _mk_repo(docs):
    col = MemoryCollection()
    col.docs = list(docs)
    db = SimpleNamespace(get_collection=lambda _name: col)
    return ProductRepository(db_instance=db), col


def test_is_product_reliable_truth_table_and_mongo_filter_equivalence():
    """验证领域函数与 Mongo 正反过滤条件在全组合下完全等价。"""
    cases = [
        # (doc, expected_reliable)
        ({"platform": "jd", "product_id": "PLAIN", "channel_type": 5}, True),
        ({"platform": "jd", "product_id": "EXPIRED_TIP", "deal_info": {"is_deal_tip": False, "source_is_reliable": False, "price_is_reliable": False}}, True),
        ({"platform": "jd", "product_id": "TIP_SRC_T", "deal_info": {"is_deal_tip": True, "source_is_reliable": True, "price_is_reliable": False}}, True),
        ({"platform": "jd", "product_id": "TIP_PRC_T", "deal_info": {"is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": True}}, True),
        ({"platform": "jd", "product_id": "TIP_BOTH_T", "deal_info": {"is_deal_tip": True, "source_is_reliable": True, "price_is_reliable": True}}, True),
        ({"platform": "jd", "product_id": "TIP_BOTH_F", "deal_info": {"is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": False}}, False),
        ({"platform": "jd", "product_id": "TIP_NONE", "deal_info": {"is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": None}}, False),
        ({"platform": "jd", "product_id": "TIP_MISSING", "deal_info": {"is_deal_tip": True}}, False),
    ]
    rel_flt = reliable_product_mongo_filter()
    unrel_flt = unreliable_product_mongo_filter()

    assert is_product_reliable(None) is False
    for doc, expected in cases:
        assert is_product_reliable(doc) is expected, doc["product_id"]
        assert MemoryCollection.matches(doc, rel_flt) is expected, doc["product_id"]
        assert MemoryCollection.matches(doc, unrel_flt) is (not expected), doc["product_id"]


def test_browse_default_list_two_stage_pagination_reliable_first():
    """验证无词默认浏览严格按照「全部可靠（无线报/来源可靠/价格可靠）-> 全部不可靠」跨页无缝排序且绝不隐藏。"""
    t0 = datetime.datetime(2026, 11, 24, 10, 0, 0)
    # 故意让不可靠线报更新时间最新、价格最低，验证可靠池仍绝对优先，同时各段内部保持 sort_by 顺序
    docs = [
        # U1: 不可靠线报（更新时间最新 t0-1m，价格最低 5.0）
        {
            "platform": "jd", "product_id": "U1", "name": "不可靠线报最新", "activity_price": 5.0,
            "updated_at": t0 - datetime.timedelta(minutes=1),
            "deal_info": {"is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": False},
        },
        # U2: 不可靠线报（未判定 None，t0-2m，价格 6.0）
        {
            "platform": "jd", "product_id": "U2", "name": "不可靠线报未判定", "activity_price": 6.0,
            "updated_at": t0 - datetime.timedelta(minutes=2),
            "deal_info": {"is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": None},
        },
        # R1: 官方无线报商品（t0-10m，价格 19.9）
        {
            "platform": "pdd", "product_id": "R1", "name": "官方直供商品", "activity_price": 19.9,
            "channel_type": 5, "updated_at": t0 - datetime.timedelta(minutes=10),
        },
        # R2: 来源高可靠线报（即便 price_is_reliable=False 也属可靠，t0-20m，价格 29.9）
        {
            "platform": "jd", "product_id": "R2", "name": "特惠采采严选商品", "activity_price": 29.9,
            "updated_at": t0 - datetime.timedelta(minutes=20),
            "deal_info": {"is_deal_tip": True, "source_is_reliable": True, "price_is_reliable": False},
        },
        # R3: 价格判定可靠线报（t0-30m，价格 12.0）
        {
            "platform": "taobao", "product_id": "R3", "name": "价格判定可靠线报", "activity_price": 12.0,
            "updated_at": t0 - datetime.timedelta(minutes=30),
            "deal_info": {"is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": True},
        },
    ]
    repo, _col = _mk_repo(docs)

    # page_size=2 依次翻完 3 页：第 1 页 [R1, R2]，第 2 页交界 [R3, U1]，第 3 页尾段 [U2]
    p1 = repo.query_boom_products(sort_by="latest", page=1, page_size=2)
    p2 = repo.query_boom_products(sort_by="latest", page=2, page_size=2)
    p3 = repo.query_boom_products(sort_by="latest", page=3, page_size=2)

    assert [x["product_id"] for x in p1["data"]] == ["R1", "R2"]
    assert p1["has_more"] is True
    assert all(x["is_reliable"] is True for x in p1["data"])

    assert [x["product_id"] for x in p2["data"]] == ["R3", "U1"]
    assert [x["is_reliable"] for x in p2["data"]] == [True, False]
    assert p2["has_more"] is True

    assert [x["product_id"] for x in p3["data"]] == ["U2"]
    assert p3["data"][0]["is_reliable"] is False
    assert p3["has_more"] is False

    # price_asc 下同样先可靠按价升序 [R3(12), R1(19.9), R2(29.9)]，再不可靠按价升序 [U1(5), U2(6)]
    p_asc = repo.query_boom_products(sort_by="price_asc", page=1, page_size=10)
    assert [x["product_id"] for x in p_asc["data"]] == ["R3", "R1", "R2", "U1", "U2"]


def test_search_reliable_first_without_l3_noise_promotion():
    """验证搜索场景：可靠优先 + 不因过滤导致可靠池 L3 噪音压过不可靠池 L1 正品，同时覆盖 Meili 与 Mongo 降级。"""
    now = datetime.datetime(2026, 11, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
    docs = [
        # 可靠但仅 L3 蹭词噪音（苹果手机防摔保护壳）
        {
            "platform": "jd", "product_id": "REL_L3_CASE", "name": "苹果手机防摔保护壳",
            "activity_price": 9.9, "channel_type": 1, "updated_at": now,
            "format_status": "success", "format_activity_price": 9.9,
            "format_info": {"price": 9.9, "core_entities": [{"name": "手机壳", "score": 10}], "pricing_basis": {"base_unit": "个", "total_value": 1}},
        },
        # 不可靠但为 L1 真苹果手机，且价格极低（性价比更高但不可靠）
        {
            "platform": "jd", "product_id": "UNREL_L1_PHONE", "name": "Apple/苹果手机 iPhone 16 Pro 256GB",
            "brand": "Apple", "activity_price": 4999.0, "updated_at": now - datetime.timedelta(minutes=2),
            "format_status": "success", "format_activity_price": 4999.0,
            "format_info": {"price": 4999.0, "core_entities": [{"name": "手机", "score": 10}], "pricing_basis": {"base_unit": "GB", "total_value": 256}},
            "deal_info": {
                "is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": False,
                "publish_time": "2026-11-24 19:00:00", "deal_price": 1999.0, "format_deal_price": 1999.0,
                "format_status": "success", "need_reformat": False,
                "format_info": {"price": 1999.0, "core_entities": [{"name": "手机", "score": 10}], "pricing_basis": {"base_unit": "GB", "total_value": 256}},
            },
        },
        # 可靠 L1 真苹果手机（官方直降，价格略高于不可靠线报，必须稳居第一）
        {
            "platform": "jd", "product_id": "REL_L1_PHONE", "name": "Apple/苹果手机 iPhone 16 128GB",
            "brand": "Apple", "activity_price": 5299.0, "channel_type": 1, "updated_at": now - datetime.timedelta(minutes=5),
            "format_status": "success", "format_activity_price": 5299.0,
            "format_info": {"price": 5299.0, "core_entities": [{"name": "手机", "score": 10}], "pricing_basis": {"base_unit": "GB", "total_value": 128}},
        },
    ]

    repo, _col = _mk_repo(docs)

    # 1. Meilisearch 命中：即使故意先返回 REL_L3_CASE 与 UNREL_L1_PHONE，最终先过滤掉 L3 噪音，再按可靠优先 -> [REL_L1_PHONE, UNREL_L1_PHONE]
    repo._force_meili_enabled = True
    fake_hits = [
        {"platform": "jd", "product_id": "REL_L3_CASE"},
        {"platform": "jd", "product_id": "UNREL_L1_PHONE"},
        {"platform": "jd", "product_id": "REL_L1_PHONE"},
    ]
    with patch("repositories.product_repo.search_products", return_value=fake_hits):
        meili_cost_res = repo.query_boom_products(keyword="苹果手机", sort_by="cost_performance", page=1, page_size=10)
        meili_latest_res = repo.query_boom_products(keyword="苹果手机", sort_by="latest", page=1, page_size=10)

    assert [x["product_id"] for x in meili_cost_res["data"]] == ["REL_L1_PHONE", "UNREL_L1_PHONE"]
    assert [x["product_id"] for x in meili_latest_res["data"]] == ["REL_L1_PHONE", "UNREL_L1_PHONE"]
    assert [x["is_reliable"] for x in meili_cost_res["data"]] == [True, False]

    # 2. Meilisearch 不可用降级 Mongo regex 同样保持：过滤 L3 噪音，且可靠 L1 优先于不可靠 L1
    repo._force_meili_enabled = False
    mongo_res = repo.query_boom_products(keyword="苹果手机", sort_by="cost_performance", page=1, page_size=10)
    assert [x["product_id"] for x in mongo_res["data"]] == ["REL_L1_PHONE", "UNREL_L1_PHONE"]


def test_product_service_resolve_boom_sort_consistency():
    """验证 ProductService.get_boom_products 与 resolve_boom_sort 统一口径，无搜索词兜底 latest，有词默认 cost_performance。"""
    from services.boom_search import resolve_boom_sort
    from services.product_service import ProductService

    assert resolve_boom_sort("", None) == ("", "latest")
    assert resolve_boom_sort("   ", "cost_performance") == ("", "latest")
    assert resolve_boom_sort("抽纸", None) == ("抽纸", "cost_performance")
    assert resolve_boom_sort("抽纸", "price_asc") == ("抽纸", "price_asc")

    t0 = datetime.datetime(2026, 11, 24, 10, 0, 0)
    repo, _col = _mk_repo([
        {
            "platform": "jd", "product_id": "UNREL_CHEAP", "name": "维达抽纸24包",
            "activity_price": 9.9, "updated_at": t0,
            "format_status": "success", "format_activity_price": 9.9,
            "format_info": {"price": 9.9, "pricing_basis": {"base_unit": "包", "total_value": 24}},
            "deal_info": {
                "is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": False,
                "publish_time": "2026-11-24 17:00:00", "deal_price": 5.0, "format_deal_price": 5.0,
                "format_status": "success", "need_reformat": False,
                "format_info": {"price": 5.0, "pricing_basis": {"base_unit": "包", "total_value": 24}},
            },
        },
        {
            "platform": "pdd", "product_id": "REL_NORMAL", "name": "维达抽纸24包官方装",
            "activity_price": 29.9, "channel_type": 5, "updated_at": t0 - datetime.timedelta(minutes=5),
            "format_status": "success", "format_activity_price": 29.9,
            "format_info": {"price": 29.9, "pricing_basis": {"base_unit": "包", "total_value": 24}},
        },
    ])
    svc = ProductService(product_repo=repo)
    with patch("repositories.product_repo.time.time", return_value=t0.timestamp() + 3600):
        search_res = svc.get_boom_products(keyword="抽纸", page=1, page_size=10)
    assert [x["product_id"] for x in search_res["data"]] == ["REL_NORMAL", "UNREL_CHEAP"]
    assert [x["is_reliable"] for x in search_res["data"]] == [True, False]


def test_home_overlay_exposes_is_reliable_consistently():
    """验证首页列表叠加 (`_overlay_deal_card_info` / `get_deal_card_overlays`) 正确计算 is_reliable 且纯官方品默认 True。"""
    from services.product_service import ProductService

    repo, _col = _mk_repo([
        {
            "platform": "jd", "product_id": "JD-REL-TIP", "goods_id": "JD-REL-TIP",
            "name": "可靠线报", "activity_price": 19.9,
            "deal_info": {
                "is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": True,
                "card_highlight": "到手19.9元", "publish_time": "2026-11-24T10:00:00Z",
            },
        },
        {
            # 无 card_highlight 且无 publish_time 的活跃不可靠线报，仍必须覆盖为 is_reliable=False
            "platform": "jd", "product_id": "JD-UNREL-BARE", "goods_id": "JD-UNREL-BARE",
            "name": "不可靠无高光线报", "activity_price": 9.9,
            "deal_info": {
                "is_deal_tip": True, "source_is_reliable": False, "price_is_reliable": False,
            },
        },
    ])
    svc = ProductService(product_repo=repo)
    raw_items = [
        {"platform": "jd", "goods_id": "JD-REL-TIP", "name": "可靠线报"},
        {"platform": "jd", "goods_id": "JD-UNREL-BARE", "name": "不可靠无高光线报"},
        {"platform": "jd", "goods_id": "JD-PURE-OFFICIAL", "name": "纯官方商品"},
    ]
    overlaid = svc._overlay_deal_card_info("jd", raw_items)
    assert overlaid[0]["is_reliable"] is True
    assert overlaid[1]["is_deal_tip"] is True
    assert overlaid[1]["is_reliable"] is False
    assert overlaid[2]["is_reliable"] is True
    assert "is_reliable" not in raw_items[2]  # 不原地修改传入缓存字典


def test_frontend_positive_verified_badge_and_prompt_calibration():
    """验证缩略图仅展示正向核验标识且不出现负面字样，以及 AI-2 提示词防高于官价误杀条款到位。"""
    from pathlib import Path
    from models.deal_constants import PROMPT_STAGE2_PATH

    mobile_web_dir = Path(__file__).resolve().parents[1]
    index_html = (mobile_web_dir / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (mobile_web_dir / "static" / "css" / "style.css").read_text(encoding="utf-8")
    prompt_txt = Path(PROMPT_STAGE2_PATH).read_text(encoding="utf-8")

    # 首页与严选好价两个缩略图区域都渲染正向徽章，且无任何负面描述
    assert index_html.count('v-if="item.is_reliable === true" class="verified-badge"') == 2
    assert "不可靠" not in index_html
    assert ".verified-badge" in style_css
    assert "高于或接近官价" in prompt_txt
