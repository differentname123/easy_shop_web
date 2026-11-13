# -*- coding: utf-8 -*-
"""
回归测试：正式表好价线报（deal_info）结构化清洗隔离性、提示词补充约束、价格口径强校验与状态机幂等性。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

_MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _MOBILE_WEB_DIR.parent
for _p in (str(_REPO_ROOT), str(_MOBILE_WEB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from repositories.product_repo import ProductRepository
from mobile_web.scripts import goods_info_format as gif


def _set_dot_path(doc: dict, dot_path: str, value):
    parts = dot_path.split(".")
    cur = doc
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = copy.deepcopy(value)


def _get_dot_path(doc: dict, dot_path: str, default=None):
    cur = doc
    for part in dot_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class FakeBulkResult:
    def __init__(self, matched_count: int, modified_count: int, upserted_count: int = 0):
        self.acknowledged = True
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_count = upserted_count


class FakeCollection:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def _matches(self, doc: dict, query: dict) -> bool:
        if "$and" in query:
            return all(self._matches(doc, sub) for sub in query["$and"])
        for k, v in query.items():
            if _get_dot_path(doc, k) != v:
                return False
        return True

    def bulk_write(self, operations, ordered=False):
        matched = 0
        modified = 0
        for op in operations:
            filt = op._filter
            upd = op._doc
            for doc in self.docs:
                if self._matches(doc, filt):
                    matched += 1
                    before = copy.deepcopy(doc)
                    for k, v in upd.get("$set", {}).items():
                        _set_dot_path(doc, k, v)
                    for k, v in upd.get("$inc", {}).items():
                        cur_val = _get_dot_path(doc, k, 0) or 0
                        _set_dot_path(doc, k, cur_val + v)
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
        items = copy.deepcopy(self.collection.docs)
        if limit and limit > 0:
            items = items[:limit]
        return items

    def get_collection(self, collection_name):
        return self.collection


def _sample_valid_llm_item(sku_id: str, price: float, total_value: float = 8000, unit: str = "ml") -> dict:
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
    }


def test_build_deal_prompt_text_reuses_base_prompt_without_mutating_file():
    prompt_text = gif.build_deal_prompt_text()
    base_text = gif.read_file_to_str(gif.PROMPT_FILE_PATH)
    assert prompt_text.startswith(base_text.rstrip())
    assert "线报场景专属补充约束" in prompt_text
    assert "DEAL_SUPPLEMENT_RULES" not in base_text


def test_build_deal_product_desc_strips_urls_and_tokens():
    product = {
        "name": "【官方旗舰店】蓝月亮深层洁净洗衣液4kg套装",
        "activity_price": 49.9,
        "deal_info": {
            "clean_title": "蓝月亮薰衣草洗衣液8斤套装",
            "price_logic": "领满99减30券 + 拍2件立减",
            "unit_deal_price": "折30.4元/套",
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
    assert "折算参考：折30.4元/套" in desc
    assert "购买步骤：1. 先领券 2. 主商品拍 2 件 实付60.8元起" in desc
    assert "原始爆料文案：群友爆料 蓝月亮8斤套装 拍2件实付60.8元" in desc
    assert "https://" not in desc
    assert "口令" not in desc


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
    official_format_snapshot = {
        "p100_0": _sample_valid_llm_item("p100_0", price=49.9, total_value=4000, unit="ml")
    }
    doc_normal = {
        "_id": "oid-1",
        "platform": "jd",
        "product_id": "p100",
        "name": "蓝月亮深层洁净洗衣液4kg",
        "activity_price": 49.9,
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
        "deal_info": {
            "is_deal_tip": True,
            "clean_title": "",
            "deal_price": 9.9,
        },
    }

    fake_db = FakeMongoDB([doc_normal, doc_reformat_fail, doc_empty_title])
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

    # 2. 验证新线报覆盖后重洗失败：清空旧陈腐 deal_info.format_info 为 None，重试计数重置为 1
    assert doc_reformat_fail["deal_info"]["format_status"] == "failed"
    assert doc_reformat_fail["deal_info"]["format_info"] is None
    assert doc_reformat_fail["deal_info"]["format_deal_price"] == 52.0
    assert doc_reformat_fail["deal_info"]["format_retry_count"] == 1

    # 3. 验证无标题异常线报：直接标记 retry_count = FORMAT_MAX_RETRIES 避免空转
    assert doc_empty_title["deal_info"]["format_status"] == "failed"
    assert doc_empty_title["deal_info"]["format_retry_count"] == gif.FORMAT_MAX_RETRIES

    # 4. 验证下游 ProductRepository 仅读取顶层官方 format_info，不受 deal_info.format_info 干扰
    entries = ProductRepository._extract_sku_unit_entries(doc_normal)
    assert len(entries) == 1
    sku_price, base_unit, unit_qty_map = entries[0]
    assert sku_price == 49.9
    assert base_unit == "ml"
    assert unit_qty_map["ml"] == 4000.0

    # 同时验证 deal_info.format_info 单对象结构与 _extract_sku_unit_entries 解析协议天然兼容
    deal_entries = ProductRepository._extract_sku_unit_entries({
        "format_status": doc_normal["deal_info"]["format_status"],
        "format_info": doc_normal["deal_info"]["format_info"],
    })
    assert len(deal_entries) == 1
    assert deal_entries[0][0] == 60.8
    assert deal_entries[0][2]["ml"] == 8000.0


def test_run_format_round_recleans_specless_placeholder_sku_when_activity_price_changes(monkeypatch):
    """
    真实回归场景（可乐 663508084573 vs 743329703469）：
    当商品含无规格属性的 1 元占位 sku_info ([{'价格': '￥41.4', '截图地址': ''}])，且 activity_price
    后来降为 35.4 时，清洗逻辑必须识别价格变动、以最新 activity_price (35.4) 送洗并同步占位价格，
    避免继续沿用陈腐价格 41.4 导致性价比排序错误，也避免因重喂旧 sku_info['价格'] 陷入死循环。
    """
    cola_18 = {
        "_id": "oid-cola-18",
        "platform": "pdd",
        "product_id": "663508084573",
        "name": "可口可乐500ml*6瓶+雪碧500ml*6瓶+芬达500ml*6瓶多口味装包邮",
        "activity_price": 35.4,
        "price": 43.4,
        "sku_info": [{"价格": "￥41.4", "截图地址": ""}],
        "format_status": "success",
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
        "sku_info": [{"价格": "￥55.2", "截图地址": ""}],
        "format_status": "success",
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

    # 重洗前：18瓶装因陈腐价格 41.4 被触发重洗检测，而 24瓶装价格一致不触发
    assert gif._detect_product_price_changes(cola_18) == (True, {"663508084573_1"})
    assert gif._detect_product_price_changes(cola_24) == (False, set())

    captured_prompts: list[str] = []

    def _fake_generate_content(prompt, **kwargs):
        captured_prompts.append(prompt)
        # 断言送给模型的价格必须是最新 activity_price (35.4)，绝不能是旧的 ￥41.4
        assert '"价格": "35.4"' in prompt
        assert "41.4" not in prompt
        payload = [{
            "sku_id": "663508084573_1",
            "price": 35.4,
            "core_entities": [{"name": "碳酸饮料", "score": 10}],
            "decision_keywords": [{"attribute_name": "单瓶净含量", "attribute_value": "500ml", "score": 9}],
            "pricing_basis": {
                "is_inferred": False,
                "structure": [{"value": 18, "unit": "瓶"}, {"value": 500, "unit": "ml"}],
                "total_value": 9000,
                "base_unit": "ml",
                "equivalent_description": None,
            },
        }]
        return {
            "status": "✅ 成功",
            "content": json.dumps(payload, ensure_ascii=False),
            "metrics": {"model_used": "mock-goods-model"},
        }

    monkeypatch.setattr(gif, "generate_content", _fake_generate_content)

    # 第一轮：执行重洗
    fake_db = FakeMongoDB([cola_18])
    manager = gif.ProductManager(fake_db)
    counts = gif.run_format_round(manager)

    assert len(captured_prompts) == 1
    assert counts["success"] == 1
    assert counts["reformat_success"] == 1
    assert cola_18["format_status"] == "success"
    assert cola_18["format_activity_price"] == 35.4
    assert cola_18["sku_info"] == [{"价格": "￥35.4", "截图地址": ""}]
    assert cola_18["format_info"]["663508084573_1"]["price"] == 35.4
    assert cola_18["format_retry_count"] == 0

    # 第二轮：价格未再变动时，检测结果为 False，即使误查入本轮也不会再次请求模型（防死循环）
    assert gif._detect_product_price_changes(cola_18) == (False, set())
    counts_round2 = gif.run_format_round(manager)
    assert len(captured_prompts) == 1
    assert counts_round2["success"] == 0

    # 验证下游性价比提取：18瓶装每元 9000/35.4 = 254.237ml，严格高于 24瓶装每元 12000/55.2 = 217.391ml
    e18 = ProductRepository._extract_sku_unit_entries(cola_18)
    e24 = ProductRepository._extract_sku_unit_entries(cola_24)
    assert round(e18[0][2]["ml"] / e18[0][0], 6) > round(e24[0][2]["ml"] / e24[0][0], 6)


def test_run_format_round_multi_sku_partial_price_change_and_retry_convergence(monkeypatch):
    """
    验证：
    1. 带真实规格属性的多规格商品：仅价格变动的 SKU 被局部剔除重洗，未变动规格直接保留不重复调模型；
       带复杂文案（如 '￥19.46多人团价￥21.46'）但未变动的规格不会误触发重洗。
    2. 单规格商品价格变动重洗若遭遇模型连续失败，快照 format_activity_price 已推进，最多失败 3 轮即收敛停止。
    3. pending_format_query 顶层保护 format_retry_count < 999 人工锁定记录。
    """
    query = gif.pending_format_query()
    assert {"format_retry_count": {"$lt": gif.LOCKED_RETRY_SENTINEL}} in query["$and"][1]["$or"]

    # 多规格商品：SKU1 原为旧占位残留 3.01，现更新为 ￥47.3；SKU2 保持 ￥23.8 未变
    doc_multi = {
        "_id": "oid-multi-1",
        "platform": "pdd",
        "product_id": "978550785391",
        "name": "原生木浆抽纸整箱家用商用餐巾纸",
        "activity_price": 3.01,
        "sku_info": [
            {"款式": "80包【整箱19200张】", "价格": "￥47.3", "截图地址": ""},
            {"款式": "40包【整箱9600张】", "价格": "￥23.8", "截图地址": ""},
        ],
        "format_status": "success",
        "format_info": {
            "978550785391_1": _sample_valid_llm_item("978550785391_1", price=3.01, total_value=8000, unit="张"),
            "978550785391_2": _sample_valid_llm_item("978550785391_2", price=23.8, total_value=8000, unit="张"),
        },
    }
    # 复杂价格文案但已记录快照未变的带规格商品：不应误判价格变动
    doc_complex_unchanged = {
        "_id": "oid-complex-1",
        "platform": "pdd",
        "product_id": "631582502680",
        "name": "漫花10提悬挂式抽纸家用12000张",
        "activity_price": 21.46,
        "sku_info": [{"款式": "10提12000张送挂钩", "价格": "￥19.46多人团价￥21.46", "截图地址": ""}],
        "format_status": "success",
        "format_sku_prices": {"631582502680_1": "￥19.46多人团价￥21.46"},
        "format_info": {
            "631582502680_1": _sample_valid_llm_item("631582502680_1", price=19.46, total_value=8000, unit="张"),
        },
    }
    # 单规格空 sku_info 商品：价格从 52.9 降为 49.9，但模拟模型偏离价格导致校验失败
    doc_empty_sku_fail = {
        "_id": "oid-empty-fail",
        "platform": "jd",
        "product_id": "jd1001",
        "name": "零度无糖可乐1.25L*12瓶",
        "activity_price": 49.9,
        "sku_info": [],
        "format_status": "success",
        "format_retry_count": 3,  # 历史旧价格曾满 3 次，新价格仍应获得一次全新重洗周期
        "format_info": {
            "jd1001_0": _sample_valid_llm_item("jd1001_0", price=52.9, total_value=8000, unit="ml"),
        },
    }

    assert gif._detect_product_price_changes(doc_multi) == (True, {"978550785391_1"})
    assert gif._detect_product_price_changes(doc_complex_unchanged) == (False, set())
    assert gif._detect_product_price_changes(doc_empty_sku_fail) == (True, {"jd1001_0"})

    monkeypatch.setattr(gif, "LLM_MAX_RETRIES", 1)

    def _fake_generate(prompt, **kwargs):
        # 仅 SKU1 被送洗，SKU2 未重复送洗
        assert "978550785391_1" in prompt
        assert "978550785391_2" not in prompt
        # 故意给 jd1001_0 返回错误的旧价格 52.9，触发 expected_price (49.9) 强校验拦截
        payload = [
            _sample_valid_llm_item("978550785391_1", price=47.3, total_value=8000, unit="张"),
            _sample_valid_llm_item("jd1001_0", price=52.9, total_value=8000, unit="ml"),
        ]
        return {
            "status": "✅ 成功",
            "content": json.dumps(payload, ensure_ascii=False),
            "metrics": {"model_used": "mock-goods-model"},
        }

    monkeypatch.setattr(gif, "generate_content", _fake_generate)

    fake_db = FakeMongoDB([doc_multi, doc_empty_sku_fail])
    manager = gif.ProductManager(fake_db)
    counts = gif.run_format_round(manager)

    assert counts["reformat_success"] == 1
    assert counts["reformat_failed"] == 1

    # 多规格商品：SKU1 已更新为 47.3 并记录快照，SKU2 完好保留，下一轮不再触发重洗
    assert doc_multi["format_info"]["978550785391_1"]["price"] == 47.3
    assert doc_multi["format_info"]["978550785391_2"]["price"] == 23.8
    assert doc_multi["format_sku_prices"]["978550785391_1"] == "￥47.3"
    assert gif._detect_product_price_changes(doc_multi) == (False, set())

    # 单规格失败商品：旧陈腐 52.9 已清空，快照已推进到 49.9，retry_count 置为 1 且不会在后续轮次再次误判为新价格重置为 0
    assert doc_empty_sku_fail["format_status"] == "failed"
    assert doc_empty_sku_fail["format_info"] == {}
    assert doc_empty_sku_fail["format_activity_price"] == 49.9
    assert doc_empty_sku_fail["format_retry_count"] == 1
    assert gif._detect_product_price_changes(doc_empty_sku_fail) == (False, set())

