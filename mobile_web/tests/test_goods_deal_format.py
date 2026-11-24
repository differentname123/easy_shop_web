# -*- coding: utf-8 -*-
"""
回归测试：正式表好价线报（deal_info）结构化清洗隔离性、提示词补充约束、价格口径强校验与状态机幂等性。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock

_MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _MOBILE_WEB_DIR.parent
for _p in (str(_REPO_ROOT), str(_MOBILE_WEB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models.cost_performance import extract_sku_unit_entries
from services import goods_formatter as gif
from tests.test_product_repository import MemoryCollection as ProductMemoryCollection


def _set_dot_path(doc: dict, dot_path: str, value):
    parts = dot_path.split(".")
    cur = doc
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = copy.deepcopy(value)


class FakeBulkResult:
    def __init__(self, matched_count: int, modified_count: int, upserted_count: int = 0):
        self.acknowledged = True
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_count = upserted_count


class FakeCollection:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def bulk_write(self, operations, ordered=False):
        matched = 0
        modified = 0
        for op in operations:
            filt = op._filter
            upd = op._doc
            for doc in self.docs:
                if ProductMemoryCollection.matches(doc, filt):
                    matched += 1
                    before = copy.deepcopy(doc)
                    for k, v in upd.get("$set", {}).items():
                        _set_dot_path(doc, k, v)
                    for k, v in upd.get("$inc", {}).items():
                        _, cur_val = ProductMemoryCollection.get_path(doc, k)
                        _set_dot_path(doc, k, (cur_val or 0) + v)
                    if doc != before:
                        modified += 1
                    break
        return FakeBulkResult(matched_count=matched, modified_count=modified)


class FakeMongoDB:
    def __init__(self, docs: list[dict]):
        self.collection = FakeCollection(docs)
        self.indexes = []
        self.last_query = None
        self.last_projection = None
        self.last_limit = None

    def create_index(self, collection_name, keys, unique=False):
        self.indexes.append((collection_name, tuple(keys), unique))

    def find_many(self, collection_name, query=None, projection=None, sort=None, limit=0):
        self.last_query = query
        self.last_projection = projection
        self.last_limit = limit
        q = query or {}
        items = [doc for doc in self.collection.docs if ProductMemoryCollection.matches(doc, q)]
        if sort:
            for key, direction in reversed(sort):
                items.sort(
                    key=lambda d: (ProductMemoryCollection.get_path(d, key)[1] is not None, ProductMemoryCollection.get_path(d, key)[1]),
                    reverse=direction < 0,
                )
        items = [ProductMemoryCollection.project(doc, projection) for doc in items]
        if limit and limit > 0:
            items = items[:limit]
        return items

    def get_collection(self, collection_name):
        return self.collection


def _sample_valid_llm_item(
    sku_id: str,
    price: float,
    total_value: float = 8000,
    unit: str = "ml",
    card_highlight: str | None = "7.6元/瓶",
) -> dict:
    return {
        "sku_id": sku_id,
        "price": price,
        "core_entities": [
            {"name": "洗衣液", "score": 10},
            {"name": "蓝月亮", "score": 8},
        ],
        "decision_keywords": [
            {"attribute_name": "香型", "attribute_value": "薰衣草", "score": 7},
        ],
        "pricing_basis": {
            "is_inferred": False,
            "structure": [
                {"value": 2, "unit": "套"},
                {"value": 4000, "unit": unit},
            ],
            "total_value": total_value,
            "base_unit": unit,
            "equivalent_description": None,
        },
        "card_highlight": card_highlight,
    }


def test_build_deal_product_desc_strips_urls_and_tokens():
    product = {
        "name": "【官方旗舰店】蓝月亮深层洁净洗衣液4kg套装",
        "activity_price": 49.9,
        "deal_info": {
            "clean_title": "蓝月亮薰衣草洗衣液8斤套装",
            "price_logic": "领满99减30券 + 拍2件立减",
            "card_highlight": "30.4元/套",
            "tutorial_text": (
                "1. 先领券 https://u.jd.com/abc123 （口令：￥88XYZ99￥） "
                "2. 主商品拍 2 件 https://s.click.taobao.com/xyz (口令: $AAA111$) 实付60.8元起"
            ),
            "original_copywriting": (
                "群友爆料 蓝月亮8斤套装\n"
                "https://m.tb.cn/h.xyz （口令：￥TK999￥） 拍2件实付60.8元"
            ),
        },
    }
    desc = gif.build_deal_product_desc(product)
    assert "线报主商品：蓝月亮薰衣草洗衣液8斤套装" in desc
    assert "官方标题参考：【官方旗舰店】蓝月亮深层洁净洗衣液4kg套装" in desc
    assert "官方日常价：49.9元" in desc
    assert "优惠方式：领满99减30券 + 拍2件立减" in desc
    assert "购买步骤：1. 先领券 2. 主商品拍 2 件 实付60.8元起" in desc
    assert "原始爆料文案：群友爆料 蓝月亮8斤套装 拍2件实付60.8元" in desc
    assert "https://" not in desc
    assert "口令" not in desc


def test_check_format_info_validates_and_sanitizes_card_highlight():
    ok_none, res_none, _, _ = gif.check_format_info([_sample_valid_llm_item("s1", 96.0, card_highlight="到手96元")], ["s1"])
    assert ok_none and res_none["s1"]["card_highlight"] == "到手96元"

    ok_null, res_null, _, _ = gif.check_format_info([_sample_valid_llm_item("s2", 96.0, card_highlight=None)], ["s2"])
    assert ok_null and res_null["s2"]["card_highlight"] is None

    ok_fluff, res_fluff, _, _ = gif.check_format_info([_sample_valid_llm_item("s3", 29.9, card_highlight=" 好价推荐 ")], ["s3"])
    assert ok_fluff and res_fluff["s3"]["card_highlight"] is None

    bad_type = _sample_valid_llm_item("s4", 29.9)
    bad_type["card_highlight"] = 123
    ok_bad, _, err_bad, _ = gif.check_format_info([bad_type], ["s4"])
    assert not ok_bad and "card_highlight" in err_bad["s4"]

    missing_key = _sample_valid_llm_item("s5", 29.9)
    del missing_key["card_highlight"]
    ok_miss, _, err_miss, _ = gif.check_format_info([missing_key], ["s5"])
    assert not ok_miss and "card_highlight" in err_miss["s5"]


def test_check_format_info_enforces_expected_deal_price():
    bad_item = _sample_valid_llm_item("1001_deal", price=0.0)
    ok, results, errors, _ = gif.check_format_info(
        [bad_item], ["1001_deal"], expected_prices={"1001_deal": 97.35}
    )
    assert not ok
    assert "1001_deal" in errors

    mismatch_item = _sample_valid_llm_item("1001_deal", price=30.4)
    ok2, results2, errors2, _ = gif.check_format_info(
        [mismatch_item], ["1001_deal"], expected_prices={"1001_deal": 60.8}
    )
    assert not ok2
    assert "不一致" in errors2["1001_deal"]

    good_item = _sample_valid_llm_item("1001_deal", price=60.8000001)
    ok3, results3, errors3, _ = gif.check_format_info(
        [good_item], ["1001_deal"], expected_prices={"1001_deal": 60.8}
    )
    assert ok3
    assert errors3 == {}
    assert results3["1001_deal"]["price"] == 60.8


def test_run_deal_format_round_preserves_official_format_info_and_handles_lifecycle(monkeypatch):
    now_utc = datetime.now(timezone.utc)
    official_format_snapshot = {
        "p100_0": _sample_valid_llm_item("p100_0", price=49.9, total_value=4000, unit="ml")
    }
    doc_normal = {
        "_id": "oid-1",
        "platform": "jd",
        "product_id": "p100",
        "name": "蓝月亮深层洁净洗衣液4kg",
        "activity_price": 49.9,
        "updated_at": now_utc,
        "format_status": "success",
        "format_info": copy.deepcopy(official_format_snapshot),
        "deal_info": {
            "is_deal_tip": True,
            "clean_title": "蓝月亮洗衣液4kg*2套 共16斤",
            "deal_price": 60.8,
            "price_logic": "拍2件叠券实付60.8元",
            "tutorial_text": "主商品拍2件共到手16斤，实付60.8元起",
            "original_copywriting": "京东蓝月亮4kg套装 拍2件60.8元",
        },
    }
    doc_reformat_fail = {
        "_id": "oid-2",
        "platform": "taobao",
        "product_id": "p200",
        "name": "维达抽纸24包整箱",
        "activity_price": 39.9,
        "updated_at": now_utc,
        "format_status": "success",
        "format_info": {"p200_0": _sample_valid_llm_item("p200_0", price=39.9, total_value=2400, unit="抽")},
        "deal_info": {
            "is_deal_tip": True,
            "clean_title": "维达抽纸买一送一发2箱",
            "deal_price": 52.0,  # 新线报覆盖后价格从 29.9 变为 52.0
            "format_deal_price": 29.9,
            "format_clean_title": "维达抽纸1箱",
            "format_status": "success",
            "format_retry_count": 3,
            "format_info": _sample_valid_llm_item("p200_deal", price=29.9, total_value=2400, unit="抽"),
        },
    }
    doc_empty_title = {
        "_id": "oid-3",
        "platform": "pdd",
        "product_id": "p300",
        "name": "",
        "updated_at": now_utc,
        "deal_info": {
            "is_deal_tip": True,
            "clean_title": "",
            "deal_price": 9.9,
        },
    }
    doc_stale_old = {
        "_id": "oid-old",
        "platform": "pdd",
        "product_id": "p400",
        "name": "超窗口旧线报",
        "updated_at": now_utc - timedelta(days=gif.RECENT_DAYS + 3),
        "deal_info": {"is_deal_tip": True, "clean_title": "超窗旧商品", "deal_price": 9.9},
    }

    fake_db = FakeMongoDB([doc_normal, doc_reformat_fail, doc_empty_title, doc_stale_old])
    manager = gif.ProductManager(fake_db)

    def _fake_generate_content(prompt, **kwargs):
        assert "线报场景专属补充约束" in prompt
        assert "官方日常价：49.9元" in prompt
        assert "原始爆料文案：京东蓝月亮4kg套装 拍2件60.8元" in prompt
        # 只返回 p100_deal 成功，模拟 p200_deal 缺失失败
        payload = [_sample_valid_llm_item("p100_deal", price=60.8, total_value=8000, unit="ml")]
        return {
            "status": "✅ 成功",
            "content": json.dumps(payload, ensure_ascii=False),
            "metrics": {"model_used": "mock-deal-model"},
        }

    monkeypatch.setattr(gif, "generate_content", _fake_generate_content)
    counts = gif.run_deal_format_round(manager)

    assert fake_db.last_limit == 0
    assert fake_db.last_projection["activity_price"] == 1
    assert fake_db.last_projection["deal_info.original_copywriting"] == 1
    assert gif.DEAL_FORMAT_WORKERS == 10
    assert counts["success"] == 1
    assert counts["normal_success"] == 1
    assert counts["failed"] == 1
    assert counts["reformat_failed"] == 1
    assert counts["skipped"] == 1

    # 1. 验证常规成功：写入 deal_info.format_info，且顶层官方 format_info 完全未变
    assert doc_normal["format_info"] == official_format_snapshot
    assert doc_normal["format_status"] == "success"
    assert doc_normal["activity_price"] == 49.9
    assert doc_normal["deal_info"]["format_status"] == "success"
    assert doc_normal["deal_info"]["format_deal_price"] == 60.8
    assert doc_normal["deal_info"]["format_clean_title"] == "蓝月亮洗衣液4kg*2套 共16斤"
    assert doc_normal["deal_info"]["format_retry_count"] == 0
    assert doc_normal["deal_info"]["format_info"]["pricing_basis"]["total_value"] == 8000
    assert doc_normal["deal_info"]["format_info"]["card_highlight"] == "7.6元/瓶"

    # 2. 验证新线报覆盖后重洗失败：清空旧陈腐 deal_info.format_info 为 None，重试计数重置为 1
    assert doc_reformat_fail["deal_info"]["format_status"] == "failed"
    assert doc_reformat_fail["deal_info"]["format_info"] is None
    assert doc_reformat_fail["deal_info"]["format_deal_price"] == 52.0
    assert doc_reformat_fail["deal_info"]["format_retry_count"] == 1

    # 3. 验证无标题异常线报：直接标记 retry_count = FORMAT_MAX_RETRIES 避免空转
    assert doc_empty_title["deal_info"]["format_status"] == "failed"
    assert doc_empty_title["deal_info"]["format_retry_count"] == gif.FORMAT_MAX_RETRIES

    # 4. 验证下游性价比模型仅读取顶层官方 format_info，不受 deal_info.format_info 干扰
    entries = extract_sku_unit_entries(doc_normal)
    assert len(entries) == 1
    sku_price, base_unit, unit_qty_map = entries[0]
    assert sku_price == 49.9
    assert base_unit == "ml"
    assert unit_qty_map["ml"] == 4000.0

    # 同时验证 deal_info.format_info 单对象结构与 extract_sku_unit_entries 解析协议天然兼容
    deal_entries = extract_sku_unit_entries({
        "format_status": doc_normal["deal_info"]["format_status"],
        "format_info": doc_normal["deal_info"]["format_info"],
    })
    assert len(deal_entries) == 1
    assert deal_entries[0][0] == 60.8
    assert deal_entries[0][2]["ml"] == 8000.0


def test_run_format_round_recleans_when_activity_price_changes_and_skips_in_query(monkeypatch):
    """
    当单规格商品 activity_price 从 41.4 降为 35.4 时，pending_format_query 下推筛选并触发重洗，
    写回规范键 {pid}_0 与 format_activity_price=35.4；未改价且已有快照的商品直接在 Mongo 查询层排除，
    模型输出的前后空白也会在校验前自动 strip 而不会误杀整条记录。
    """
    now_utc = datetime.now(timezone.utc)
    cola_18 = {
        "_id": "oid-cola-18",
        "platform": "pdd",
        "product_id": "663508084573",
        "name": "可口可乐500ml*6瓶+雪碧500ml*6瓶+芬达500ml*6瓶多口味装包邮",
        "activity_price": 35.4,
        "price": 43.4,
        "updated_at": now_utc,
        "format_status": "success",
        "format_activity_price": 41.4,
        "format_info": {
            "663508084573_1": {
                "sku_id": "663508084573_1",
                "price": 41.4,
                "core_entities": [{"name": "碳酸饮料", "score": 10}],
                "decision_keywords": [{"attribute_name": "单瓶净含量", "attribute_value": "500ml", "score": 9}],
                "pricing_basis": {
                    "is_inferred": False,
                    "structure": [{"value": 18, "unit": "瓶"}, {"value": 500, "unit": "ml"}],
                    "total_value": 9000,
                    "base_unit": "ml",
                    "equivalent_description": None,
                },
            }
        },
    }
    cola_24 = {
        "_id": "oid-cola-24",
        "platform": "pdd",
        "product_id": "743329703469",
        "name": "可口可乐/雪碧/芬达/零度可乐500ml*24瓶汽水碳酸饮料整箱装包邮",
        "activity_price": 55.2,
        "price": 65.2,
        "updated_at": now_utc,
        "format_status": "success",
        "format_activity_price": 55.2,
        "format_info": {
            "743329703469_1": {
                "sku_id": "743329703469_1",
                "price": 55.2,
                "core_entities": [{"name": "碳酸饮料", "score": 10}],
                "decision_keywords": [{"attribute_name": "单瓶净含量", "attribute_value": "500ml", "score": 9}],
                "pricing_basis": {
                    "is_inferred": False,
                    "structure": [{"value": 24, "unit": "瓶"}, {"value": 500, "unit": "ml"}],
                    "total_value": 12000,
                    "base_unit": "ml",
                    "equivalent_description": None,
                },
            }
        },
    }

    query = gif.pending_format_query()
    assert ProductMemoryCollection.matches(cola_18, query) is True
    assert ProductMemoryCollection.matches(cola_24, query) is False
    assert gif._detect_product_price_changes(cola_18) == (True, {"663508084573_1"})
    assert gif._detect_product_price_changes(cola_24) == (False, set())

    captured_prompts: list[str] = []

    assert gif._clean_stripped_str({"v": " ok "}, "v") is True
    assert gif._clean_stripped_str({"v": "   "}, "v") is False

    def _fake_generate_content(prompt, **kwargs):
        captured_prompts.append(prompt)
        assert '"价格": "35.4"' in prompt
        assert "41.4" not in prompt
        payload = [{
            "sku_id": "663508084573_0",
            "price": 35.4,
            "core_entities": [{"name": " 碳酸饮料 ", "score": 10}],
            "decision_keywords": [{"attribute_name": " 单瓶净含量 ", "attribute_value": " 500ml ", "score": 9}],
            "pricing_basis": {
                "is_inferred": False,
                "structure": [{"value": 18, "unit": " 瓶 "}, {"value": 500, "unit": " ml "}],
                "total_value": 9000,
                "base_unit": " ml ",
                "equivalent_description": None,
            },
            "card_highlight": " 1.97元/瓶 ",
        }]
        return {
            "status": "✅ 成功",
            "content": json.dumps(payload, ensure_ascii=False),
            "metrics": {"model_used": "mock-goods-model"},
        }

    monkeypatch.setattr(gif, "generate_content", _fake_generate_content)

    fake_db = FakeMongoDB([cola_18, cola_24])
    manager = gif.ProductManager(fake_db)
    counts = gif.run_format_round(manager)

    assert len(captured_prompts) == 1
    assert counts["success"] == 1
    assert counts["reformat_success"] == 1
    assert cola_18["format_status"] == "success"
    assert cola_18["format_activity_price"] == 35.4
    assert cola_18["format_info"]["663508084573_0"]["price"] == 35.4
    assert cola_18["format_info"]["663508084573_0"]["core_entities"][0]["name"] == "碳酸饮料"
    assert cola_18["format_info"]["663508084573_0"]["card_highlight"] == "1.97元/瓶"
    assert cola_18["format_retry_count"] == 0

    # 第二轮：Mongo 查询条件直接过滤已成功且未改价的商品，零 DB 结果也零模型请求
    assert ProductMemoryCollection.matches(cola_18, gif.pending_format_query()) is False
    assert gif._detect_product_price_changes(cola_18) == (False, set())
    counts_round2 = gif.run_format_round(manager)
    assert len(captured_prompts) == 1
    assert counts_round2["success"] == 0

    e18 = extract_sku_unit_entries(cola_18)
    e24 = extract_sku_unit_entries(cola_24)
    assert round(e18[0][2]["ml"] / e18[0][0], 6) > round(e24[0][2]["ml"] / e24[0][0], 6)


def test_run_format_round_retry_convergence_and_pending_query_filters(monkeypatch):
    """
    验证：
    1. pending_format_query 下推保护 format_retry_count < 999 人工锁定记录、放行改价/重洗记录并过滤已完成未改价记录；
    2. 单规格商品价格变动重洗若遭遇模型校验失败，快照 format_activity_price 已推进，最多失败 3 轮即收敛停止。
    """
    now_utc = datetime.now(timezone.utc)
    query = gif.pending_format_query()
    assert ProductMemoryCollection.matches({"updated_at": now_utc}, query) is True
    assert ProductMemoryCollection.matches({"updated_at": now_utc, "format_retry_count": gif.LOCKED_RETRY_SENTINEL}, query) is False
    assert ProductMemoryCollection.matches({
        "updated_at": now_utc,
        "format_status": "success",
        "activity_price": 20.0,
        "format_activity_price": 20.0,
    }, query) is False

    # 价格从 52.9 降为 49.9，历史旧价曾满 3 次重试，改价后仍应被 pending_format_query 放行重洗一次
    doc_empty_sku_fail = {
        "_id": "oid-empty-fail",
        "platform": "jd",
        "product_id": "jd1001",
        "name": "零度无糖可乐1.25L*12瓶",
        "activity_price": 49.9,
        "format_activity_price": 52.9,
        "updated_at": now_utc,
        "format_status": "success",
        "format_retry_count": 3,
        "format_info": {
            "jd1001_0": _sample_valid_llm_item("jd1001_0", price=52.9, total_value=8000, unit="ml"),
        },
    }
    assert ProductMemoryCollection.matches(doc_empty_sku_fail, query) is True
    assert gif._detect_product_price_changes(doc_empty_sku_fail) == (True, {"jd1001_0"})

    monkeypatch.setattr(gif, "LLM_MAX_RETRIES", 1)

    def _fake_generate(prompt, **kwargs):
        payload = [_sample_valid_llm_item("jd1001_0", price=52.9, total_value=8000, unit="ml")]
        return {
            "status": "✅ 成功",
            "content": json.dumps(payload, ensure_ascii=False),
            "metrics": {"model_used": "mock-goods-model"},
        }

    monkeypatch.setattr(gif, "generate_content", _fake_generate)

    fake_db = FakeMongoDB([doc_empty_sku_fail])
    manager = gif.ProductManager(fake_db)
    counts = gif.run_format_round(manager)

    assert counts["reformat_failed"] == 1
    assert doc_empty_sku_fail["format_status"] == "failed"
    assert doc_empty_sku_fail["format_info"] == {}
    assert doc_empty_sku_fail["format_activity_price"] == 49.9
    assert doc_empty_sku_fail["format_retry_count"] == 1
    assert gif._detect_product_price_changes(doc_empty_sku_fail) == (False, set())


def test_run_format_round_skips_unchanged_success_with_zero_writes():
    unchanged_doc = {
        "_id": "oid_done",
        "platform": "pdd",
        "product_id": "100",
        "name": "维达抽纸24包",
        "activity_price": 19.9,
        "format_status": "success",
        "format_activity_price": 19.9,
        "format_info": {"100_0": _sample_valid_llm_item("100_0", price=19.9, total_value=24, unit="包")},
    }
    mgr = MagicMock()
    mgr.query.return_value = [unchanged_doc]
    counts = gif.run_format_round(mgr)
    assert counts["success"] == 0 and counts["skipped"] == 0
    mgr.update.assert_not_called()


def test_goods_formatter_prompts_align_card_highlight_without_weakening_pricing_basis():
    """验证 GoodsFormatter 线报组合提示词同时包含高光防微观单位与 pricing_basis 物理量隔离规则。"""
    combined = gif.build_deal_prompt_text()
    assert "元/斤" in combined
    assert "元/g" in combined and "严禁" in combined
    assert "仅作用于 `card_highlight`" in combined
    assert "pricing_basis" in combined and "base_unit" in combined

