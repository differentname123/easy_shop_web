# -*- coding: utf-8 -*-
"""商品仓储确定性回归测试：无外部 API、无真实 MongoDB。"""
import datetime
import re
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from bson.decimal128 import Decimal128

from config import PROMOTION_LINK_TTL
from repositories.product_repo import ProductRepository


class MemoryCursor:
    """仅实现仓储实际使用的排序、分页和超时链式接口。"""

    def __init__(self, docs, calls):
        self.docs = docs
        self.calls = calls
        self.offset = 0
        self.size = None

    def sort(self, spec):
        self.calls.append(("sort", spec))
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda doc: (doc.get(key) is not None, doc.get(key)), reverse=direction < 0)
        return self

    def skip(self, count):
        self.calls.append(("skip", count))
        self.offset = count
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        self.size = count
        return self

    def max_time_ms(self, milliseconds):
        self.calls.append(("max_time_ms", milliseconds))
        return self

    def __iter__(self):
        end = None if self.size is None else self.offset + self.size
        return iter(self.docs[self.offset:end])


class MemoryCollection:
    """覆盖本仓储使用的 Mongo 更新子集，并实际检查旧复合唯一索引。"""

    def __init__(self):
        self.docs = []
        self.indexes = []
        self.queries = []
        self.projections = []
        self.cursor_calls = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    @staticmethod
    def expression(doc, value, variables=None):
        """仅支持可展示价格条件使用的表达式；未知操作立即失败。"""
        variables = variables or {}
        if isinstance(value, str) and value.startswith("$$"):
            return variables[value[2:]]
        if isinstance(value, str) and value.startswith("$"):
            return doc.get(value[1:])
        if not isinstance(value, dict):
            return value
        op, args = next(iter(value.items()))
        evaluate = lambda item: MemoryCollection.expression(doc, item, variables)
        if op == "$let":
            bindings = {key: evaluate(item) for key, item in args["vars"].items()}
            return MemoryCollection.expression(doc, args["in"], {**variables, **bindings})
        if op == "$ifNull":
            first = evaluate(args[0])
            return first if first is not None else evaluate(args[1])
        if op == "$convert":
            assert args["to"] == "double"
            number = evaluate(args["input"])
            if number is None:
                return args["onNull"]
            try:
                return float(number.to_decimal() if isinstance(number, Decimal128) else number)
            except (TypeError, ValueError, OverflowError):
                return args["onError"]
        if op == "$type":
            number = evaluate(args)
            for python_type, mongo_type in ((bool, "bool"), (str, "string"), (int, "int"),
                                            (float, "double"), (Decimal128, "decimal")):
                if isinstance(number, python_type):
                    return mongo_type
            return "null" if number is None else "unsupported"
        if op == "$and":
            return all(evaluate(item) for item in args)
        left, right = (evaluate(item) for item in args)
        if op == "$eq":
            return left == right
        if op == "$in":
            return left in right
        if op == "$gte":
            return left is not None and left >= right
        if op == "$lt":
            return left is not None and left < right
        raise AssertionError(f"unsupported expression: {op}")

    @staticmethod
    def matches(doc, query):
        for key, value in query.items():
            if key == "$and":
                matched = all(MemoryCollection.matches(doc, branch) for branch in value)
            elif key == "$or":
                matched = any(MemoryCollection.matches(doc, branch) for branch in value)
            elif key == "$expr":
                matched = MemoryCollection.expression(doc, value)
            elif isinstance(value, dict):
                matched = True
                for op, operand in value.items():
                    if op == "$regex":
                        flags = re.I if value.get("$options") == "i" else 0
                        values = doc.get(key) if isinstance(doc.get(key), list) else [doc.get(key)]
                        matched = matched and any(isinstance(item, str) and re.search(operand, item, flags) for item in values)
                    elif op != "$options":
                        raise AssertionError(f"unsupported field operator: {op}")
            else:
                matched = doc.get(key) == value
            if not matched:
                return False
        return True

    @staticmethod
    def project(doc, projection):
        if projection is None:
            return deepcopy(doc)
        return {key: deepcopy(value) for key, value in doc.items() if projection.get(key)}

    def find_one(self, query, projection=None):
        self.queries.append(deepcopy(query))
        self.projections.append(deepcopy(projection))
        return next((self.project(doc, projection) for doc in self.docs if self.matches(doc, query)), None)

    def find(self, query, projection=None):
        self.queries.append(deepcopy(query))
        self.projections.append(deepcopy(projection))
        docs = [self.project(doc, projection) for doc in self.docs if self.matches(doc, query)]
        return MemoryCursor(docs, self.cursor_calls)

    def distinct(self, field, query):
        self.queries.append(deepcopy(query))
        return list(dict.fromkeys(doc.get(field) for doc in self.docs if self.matches(doc, query)))

    def update_one(self, query, update, upsert=False):
        if set(update.get("$set", {})) & set(update.get("$setOnInsert", {})):
            raise ValueError("MongoDB rejects conflicting update paths")
        index = next((i for i, doc in enumerate(self.docs) if self.matches(doc, query)), None)
        inserted = index is None
        if inserted and not upsert:
            return SimpleNamespace(upserted_count=0, modified_count=0)
        if inserted:
            doc = {key: value for key, value in query.items() if not key.startswith("$")}
            doc.update(deepcopy(update.get("$setOnInsert", {})))
        else:
            doc = deepcopy(self.docs[index])
        doc.update(deepcopy(update.get("$set", {})))
        identity = (doc.get("platform"), doc.get("product_id"))
        for i, other in enumerate(self.docs):
            if i != index and (other.get("platform"), other.get("product_id")) == identity:
                raise ValueError("duplicate unique(platform, product_id)")
        if inserted:
            self.docs.append(doc)
        else:
            self.docs[index] = doc
        return SimpleNamespace(upserted_count=int(inserted), modified_count=int(not inserted))

    def bulk_write(self, operations, ordered=False):
        inserted = modified = 0
        for operation in operations:
            result = self.update_one(operation._filter, operation._doc, upsert=operation._upsert)
            inserted += result.upserted_count
            modified += result.modified_count
        return SimpleNamespace(upserted_count=inserted, modified_count=modified)


class TestProductRepository(unittest.TestCase):
    def setUp(self):
        self.col = MemoryCollection()
        db = SimpleNamespace(get_collection=lambda name: self.col)
        self.repo = ProductRepository(db_instance=db)

    def test_known_zero_values_do_not_fall_back_to_positive_fields(self):
        doc = {
            "platform": "pdd", "product_id": "100", "activity_price": 0,
            "price": 99, "original_price": 0, "saved_price": 0,
            "coupon_amount": 0, "has_coupon": False, "commission_amount": 0,
        }
        for normalize in (self.repo._normalize_doc_summary, self.repo._normalize_doc):
            result = normalize(doc)
            for key in ("price", "original_price", "saved_price", "coupon_amount", "commission_amount", "commission_rate"):
                self.assertEqual(result[key], 0, key)
            self.assertFalse(result["has_coupon"])

    def test_zero_saved_price_and_false_coupon_are_not_inferred_from_discount(self):
        result = self.repo._normalize_doc_summary({
            "product_id": "1", "activity_price": 80, "original_price": 100,
            "saved_price": 0, "coupon_amount": 0, "has_coupon": False,
        })
        self.assertEqual(result["saved_price"], 0)
        self.assertEqual(result["coupon_amount"], 0)
        self.assertFalse(result["has_coupon"])

    def test_zero_or_missing_commission_does_not_create_a_payout(self):
        for commission in (0, None):
            self.assertEqual(self.repo._calc_commission(100, commission), (0.0, 0.0))
        self.assertEqual(self.repo._calc_commission(50, 5), (10.0, 5.0))
        self.assertEqual(self.repo._calc_commission(0, 5), (0.0, 5.0))

    def test_explicit_commission_rate_is_preserved(self):
        result = self.repo._normalize_doc_summary({
            "product_id": "1", "activity_price": 10, "commission_amount": 0,
            "commission_rate": 0,
        })
        self.assertEqual(result["commission_rate"], 0)

    def test_summary_and_detail_share_money_and_identity_mapping(self):
        payload = {
            "platform": "tb", "goods_id": "AB12", "goods_sign": "SIGN",
            "name": "真实商品", "price": 0, "min_group_price_yuan": 99,
            "original_price": 0, "coupon_amount": 0, "coupon_discount_yuan": 10,
            "has_coupon": False, "commission_amount": 0, "estimated_commission_yuan": 10,
            "brand_name": "真实品牌", "category_name": "食品", "sales_tip": 0,
        }
        summary_filter, summary = self.repo._build_product_update(payload, detail=False)
        detail_filter, detail = self.repo._build_product_update(payload, detail=True)
        self.assertEqual(summary_filter, detail_filter)
        for key in ("platform", "product_id", "goods_sign", "name", "activity_price", "price",
                    "original_price", "saved_price", "coupon_amount", "has_coupon",
                    "commission_amount", "brand", "category", "sales_tip"):
            self.assertEqual(summary["$set"][key], detail["$set"][key], key)
        self.assertEqual(summary["$set"]["activity_price"], 0)
        self.assertEqual(summary["$set"]["commission_amount"], 0)
        self.assertEqual(summary["$set"]["platform"], "taobao")
        self.assertEqual(summary["$set"]["source"], "taobao_recommend_stream")

    def test_detail_roundtrip_preserves_real_provider_fields(self):
        detail = {
            "platform": "pdd", "goods_id": "100", "goods_sign": "SIGN100",
            "name": "商品", "price": 20, "original_price": 30,
            "coupon_amount": 10, "has_coupon": True, "commission_amount": 0,
            "commission_rate": 0, "brand_name": "真实品牌", "category_name": "食品",
            "mall_name": "真实店铺", "desc": "来源描述", "product_url": "https://example.com/100",
            "image_url": "https://img.example.com/1", "carousel_images": ["https://img.example.com/1", "https://img.example.com/2"],
            "detail_tags": ["来源标签"], "unified_tags": ["平台标签"],
            "service_guarantees": ["仅来源明确承诺"], "shop_ratings": {"desc_score": "3.2"},
            "specs": [{"label": "净重", "value": "500g"}],
            "coupon_info": {"coupon_remain_quantity": 0, "coupon_total_quantity": 7, "valid_tip": "已过期"},
            "search_id": "SEARCH-1",
        }
        self.assertTrue(self.repo.upsert_product_detail(detail))
        result = self.repo.get_product_detail("SIGN100", platform="pdd", key_type="sign")
        for key in ("desc", "product_url", "carousel_images", "detail_tags", "unified_tags",
                    "service_guarantees", "shop_ratings", "specs", "coupon_info", "search_id", "mall_name"):
            self.assertEqual(result[key], detail[key], key)
        self.assertEqual(result["goods_id"], "100")
        self.assertEqual(result["product_id"], "100")
        self.assertEqual(result["brand_name"], "真实品牌")
        self.assertEqual(result["category_name"], "食品")
        self.assertIsInstance(result["detail_updated_at"], datetime.datetime)
        self.assertNotIn("promotion_url", result)
        self.assertNotIn("_id", result)
        result["coupon_info"]["coupon_remain_quantity"] = 999
        self.assertEqual(self.col.docs[0]["coupon_info"]["coupon_remain_quantity"], 0)

    def test_unknown_detail_fields_are_empty_not_fixed_commercial_claims(self):
        result = self.repo._normalize_doc({"product_id": "1", "activity_price": 100})
        for key in ("detail_tags", "service_guarantees", "specs"):
            self.assertEqual(result[key], [])
        for key in ("shop_ratings", "coupon_info"):
            self.assertEqual(result[key], {})
        self.assertEqual(result["desc"], "")
        self.assertIsNone(result["detail_updated_at"])
        self.assertEqual(result["commission_amount"], 0)

    def test_summary_does_not_overwrite_missing_curated_or_rich_fields(self):
        self.repo.upsert_product_detail({
            "goods_id": "100", "goods_sign": "SIGN100", "name": "商品", "price": 10,
            "brand": "真实品牌", "category": "食品", "mall_name": "真实店铺",
            "desc": "来源详情", "product_url": "https://example.com/100",
            "carousel_images": ["https://img.example.com/a"],
            "detail_tags": ["真实标签"], "coupon_info": {"coupon_remain_quantity": 0},
        })
        before = deepcopy(self.col.docs[0])
        self.repo.upsert_home_products([{
            "goods_id": "100", "name": "新标题", "price": 9,
            "brand_name": "", "category": None,
            "desc": "摘要不得写入", "coupon_info": {"coupon_remain_quantity": 8900},
        }], channel_type=5)
        stored = self.col.docs[0]
        for key in ("brand", "category", "mall_name", "desc", "product_url", "goods_sign",
                    "carousel_images", "detail_tags", "coupon_info", "detail_updated_at"):
            self.assertEqual(stored[key], before[key], key)
        self.assertEqual(stored["name"], "新标题")
        self.assertEqual(stored["activity_price"], 9)
        self.assertEqual(stored["channel_type"], 5)
        self.assertFalse(stored["need_reformat"])
        self.assertEqual(stored["format_status"], "success")
        self.assertEqual(stored["format_retry_count"], 999)

    def test_summary_can_update_explicit_brand_and_category(self):
        self.repo.upsert_home_products([{"goods_id": "100", "brand": "旧", "category": "旧类"}])
        self.repo.upsert_home_products([{"goods_id": "100", "brand_name": "新", "category_name": "新类"}])
        self.assertEqual(self.col.docs[0]["brand"], "新")
        self.assertEqual(self.col.docs[0]["category"], "新类")

    def test_partial_price_update_preserves_known_original_price(self):
        self.repo.upsert_home_products([{"goods_id": "100", "price": 80, "original_price": 100}])
        self.repo.upsert_home_products([{"goods_id": "100", "price": 0}])
        self.assertEqual(self.col.docs[0]["activity_price"], 0)
        self.assertEqual(self.col.docs[0]["price"], 100)
        self.assertEqual(self.col.docs[0]["original_price"], 100)

    def test_only_details_refresh_detail_updated_at(self):
        self.repo.upsert_home_products([{"goods_id": "100", "name": "商品", "price": 10}])
        self.assertNotIn("detail_updated_at", self.col.docs[0])
        self.repo.upsert_product_detail({"goods_id": "100", "desc": "来源"})
        old = datetime.datetime(2020, 1, 1)
        self.col.docs[0]["detail_updated_at"] = old
        self.repo.upsert_home_products([{"goods_id": "100", "price": 9}])
        self.assertEqual(self.col.docs[0]["detail_updated_at"], old)
        self.assertGreater(self.col.docs[0]["updated_at"], old)
        self.assertEqual(self.repo.get_product_detail("100")["detail_updated_at"], old)
        self.repo.upsert_product_detail({"goods_id": "100", "desc": "新来源"})
        self.assertGreater(self.col.docs[0]["detail_updated_at"], old)

    def test_sign_only_products_do_not_collide_under_existing_unique_index(self):
        count = self.repo.upsert_home_products([
            {"goods_id": 0, "goods_sign": "SIGN_A", "name": "A", "price": 10},
            {"goods_id": "0", "goods_sign": "SIGN_B", "name": "B", "price": 20},
            {"goods_id": None, "goods_sign": "SIGN_C", "name": "C", "price": 30},
        ])
        self.assertEqual(count, 3)
        self.assertEqual(len(self.col.docs), 3)
        self.assertEqual(len({doc["product_id"] for doc in self.col.docs}), 3)
        self.assertIn(([('platform', 1), ('product_id', 1)], {'unique': True, 'background': True}), self.col.indexes)
        for sign in ("SIGN_A", "SIGN_B", "SIGN_C"):
            result = self.repo.get_product_detail(sign, key_type="sign")
            self.assertEqual(result["goods_id"], "")
            self.assertEqual(result["product_id"], "")
            self.assertEqual(result["goods_sign"], sign)

    def test_real_id_upgrades_sign_only_record_without_new_document(self):
        self.repo.upsert_home_products([{"goods_id": "0", "goods_sign": "SIGN_A", "name": "A"}])
        self.repo.upsert_product_detail({"goods_id": "123", "goods_sign": "SIGN_A", "price": 10})
        self.assertEqual(len(self.col.docs), 1)
        self.assertEqual(self.col.docs[0]["product_id"], "123")
        self.assertEqual(self.repo.get_product_detail("SIGN_A", key_type="sign")["goods_id"], "123")

    def test_sign_only_summary_never_replaces_existing_real_id(self):
        self.repo.upsert_product_detail({"goods_id": "123", "goods_sign": "SIGN_A", "price": 10})
        self.repo.upsert_home_products([{"goods_id": 0, "goods_sign": "SIGN_A", "price": 9}])
        self.assertEqual(len(self.col.docs), 1)
        self.assertEqual(self.col.docs[0]["product_id"], "123")

    def test_detail_lookup_is_always_platform_scoped(self):
        for platform in ("pdd", "jd", "taobao"):
            self.repo.upsert_product_detail({"platform": platform, "goods_id": "123", "goods_sign": "SAME_SIGN", "name": platform, "price": 10})
        self.assertEqual(self.repo.get_product_detail("123")["name"], "pdd")
        self.assertEqual(self.repo.get_product_detail("123", platform="jd", key_type="id")["name"], "jd")
        self.assertEqual(self.repo.get_product_detail("SAME_SIGN", platform="tb", key_type="sign")["name"], "taobao")
        self.assertEqual(self.col.queries[-1]["$and"][0], {"platform": "taobao", "goods_sign": "SAME_SIGN"})
        self.assertEqual(self.col.queries[-1]["$and"][1], self.repo._displayable_content_filter())
        self.repo.get_product_detail("123", platform="jd")
        self.assertEqual(self.col.queries[-1]["$and"][0], {"platform": "jd", "$or": [{"product_id": "123"}, {"goods_sign": "123"}]})

    def test_explicit_key_type_prevents_id_and_sign_ambiguity(self):
        self.repo.upsert_product_detail({"goods_id": "A", "goods_sign": "B", "name": "by-sign", "price": 10})
        self.repo.upsert_product_detail({"goods_id": "B", "goods_sign": "C", "name": "by-id", "price": 20})
        self.assertEqual(self.repo.get_product_detail("B", key_type="sign")["name"], "by-sign")
        self.assertEqual(self.repo.get_product_detail("B", key_type="id")["name"], "by-id")

    def test_invalid_identity_does_not_write_or_query_zero_as_id(self):
        self.assertEqual(self.repo.upsert_home_products([{"goods_id": 0}, {"goods_id": "0"}, {}, None]), 0)
        self.assertFalse(self.repo.upsert_product_detail({"goods_id": "0"}))
        self.assertIsNone(self.repo.get_product_detail("0", key_type="id"))
        self.assertIsNone(self.repo.get_product_detail("sign:internal", key_type="id"))
        self.assertEqual(self.col.docs, [])
        self.assertEqual(self.col.queries, [])

    def test_only_two_requested_sort_indexes_are_added_to_existing_indexes(self):
        self.assertEqual(self.col.indexes, [
            ([("platform", 1), ("product_id", 1)], {"unique": True, "background": True}),
            ("goods_sign", {"background": True}),
            ([("updated_at", -1)], {"background": True}),
            ([("platform", 1), ("updated_at", -1)], {"background": True}),
        ])

    def test_displayable_content_requires_name_and_known_valid_price(self):
        cases = [
            ({"activity_price": 0, "price": 99}, 0),
            ({"price": 0}, 0),
            ({"activity_price": None, "price": "12.50"}, 12.5),
            ({"activity_price": "0", "price": 99}, 0),
            ({"activity_price": 2}, 2),
            ({"activity_price": Decimal128("2.50")}, 2.5),
            ({"activity_price": None}, None),
            ({}, None),
            ({"activity_price": "", "price": 99}, None),
            ({"activity_price": "not-money", "price": 99}, None),
            ({"activity_price": -1, "price": 99}, None),
            ({"activity_price": True}, None),
            ({"activity_price": False}, None),
            ({"activity_price": float("nan")}, None),
            ({"activity_price": float("inf")}, None),
            ({"activity_price": float("-inf")}, None),
            ({"activity_price": []}, None),
            ({"activity_price": datetime.datetime(2026, 1, 1)}, None),
        ]
        for money, expected in cases:
            with self.subTest(money=money):
                self.col.docs = [{"platform": "pdd", "product_id": "001", "name": "商品", **money}]
                listing = self.repo.query_boom_products()
                detail = self.repo.get_product_detail("001", key_type="id")
                self.assertEqual(listing["status"], "success")
                if expected is None:
                    self.assertEqual(listing["data"], [])
                    self.assertIsNone(detail)
                else:
                    self.assertEqual(listing["data"][0]["price"], expected)
                    self.assertEqual(detail["price"], expected)
        for name in (None, "", " \t\n", 0, ["商品"]):
            with self.subTest(name=name):
                self.col.docs = [{"platform": "pdd", "product_id": "001", "name": name, "price": 0}]
                self.assertEqual(self.repo.query_boom_products()["data"], [])
                self.assertIsNone(self.repo.get_product_detail("001", key_type="id"))

    def test_bare_promotion_record_stays_hidden_until_real_content_arrives(self):
        self.assertTrue(self.repo.save_promotion_link_cache(
            "pdd", {"final_link": "https://example.com/123"}, goods_id="00123", goods_sign="SIGN",
        ))
        self.assertEqual(self.repo.query_boom_products()["data"], [])
        self.assertIsNone(self.repo.get_product_detail("00123", key_type="id"))
        self.assertIsNone(self.repo.get_product_detail("SIGN", key_type="sign"))
        self.assertIsNone(self.repo.get_product_detail("SIGN"))
        identity = self.repo.get_product_identity("SIGN")
        self.assertEqual(identity, {"platform": "pdd", "product_id": "00123", "goods_sign": "SIGN"})
        self.assertEqual(self.repo.get_categories(), [])
        self.repo.upsert_home_products([{"goods_id": "00123", "name": "后补商品", "price": 0, "category": "食品"}])
        self.assertEqual(len(self.col.docs), 1)
        self.assertEqual(self.repo.query_boom_products()["data"][0]["price"], 0)
        self.assertEqual(self.repo.get_product_detail("SIGN", key_type="sign")["goods_id"], "00123")
        self.assertEqual(self.repo.get_categories(), ["食品"])
        self.assertIsNotNone(self.repo.get_promotion_link_cache("pdd", goods_id="00123"))

    def test_content_filter_runs_before_pagination_and_keeps_count_semantics(self):
        now = datetime.datetime(2026, 11, 3)
        for number in range(8):
            doc = {"platform": "pdd", "product_id": str(number + 1), "updated_at": now - datetime.timedelta(minutes=number)}
            if number % 2:
                doc.update(name=f"商品{number}", activity_price=number)
            self.col.docs.append(doc)
        page1 = self.repo.query_boom_products(page=1, page_size=2)
        self.assertEqual([row["product_id"] for row in page1["data"]], ["2", "4"])
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["total_count"], 3)
        page2 = self.repo.query_boom_products(page=2, page_size=2)
        self.assertEqual([row["product_id"] for row in page2["data"]], ["6", "8"])
        self.assertFalse(page2["has_more"])
        self.assertEqual(page2["total_count"], 4)
        keyword = self.repo.query_boom_products(keyword="商品", page=2, page_size=2)
        self.assertEqual(keyword["total_count"], 2)
        self.assertFalse(keyword["has_more"])
        self.assertEqual(self.col.cursor_calls[-4:], [
            ("sort", [("updated_at", -1)]), ("skip", 2), ("limit", 3), ("max_time_ms", 2000),
        ])

    def test_categories_use_same_content_filter_and_platform_scope(self):
        self.col.docs = [
            {"platform": "pdd", "name": "商品", "activity_price": 0, "category": "食品"},
            {"platform": "pdd", "name": "缺价", "category": "推广裸类"},
            {"platform": "pdd", "price": 0, "category": "无名类"},
            {"platform": "pdd", "name": "无类", "price": 1, "category": " "},
            {"platform": "taobao", "name": "另一平台", "price": "3", "category": "美妆"},
        ]
        self.assertEqual(set(self.repo.get_categories()), {"食品", "美妆"})
        self.assertEqual(self.repo.get_categories("pdd"), ["食品"])
        self.assertEqual(self.repo.get_categories("tb"), ["美妆"])
        self.assertEqual(self.col.queries[-1]["$and"][0], {"platform": "taobao"})
        self.assertEqual(self.col.queries[-1]["$and"][1], self.repo._displayable_content_filter())

    def test_list_projection_preserves_complete_summary_but_not_rich_or_cache_fields(self):
        doc = {
            "_id": "mongo-id", "platform": "tb", "product_id": "AB", "goods_id": "AB",
            "goods_sign": "SIGN", "name": "商品", "brand": "品牌", "category": "类目",
            "activity_price": 0, "price": 90, "original_price": 100, "saved_price": 0,
            "coupon_amount": 0, "has_coupon": False, "commission_rate": 0, "commission_amount": 0,
            "sales_tip": "100人付款", "image_url": "https://example.com/a.png", "mall_name": "店铺",
            "updated_at": datetime.datetime(2026, 11, 3), "desc": "大详情",
            "carousel_images": ["https://example.com/b.png"], "coupon_info": {"large": "detail"},
            "default_promotion": {"final_link": "https://example.com/private"},
            "default_promotion_expires_at": datetime.datetime(2026, 11, 4),
        }
        self.col.docs = [doc]
        result = self.repo.query_boom_products()["data"]
        self.assertEqual(result, [self.repo._normalize_doc_summary(doc)])
        self.assertEqual(self.col.projections[-1], {
            "_id": 0, **dict.fromkeys((
                "platform", "product_id", "goods_id", "goods_sign", "name", "brand", "category",
                "activity_price", "price", "original_price", "saved_price", "coupon_amount", "has_coupon",
                "commission_rate", "commission_amount", "sales_tip", "image_url", "mall_name", "updated_at",
            ), 1),
        })
        for field in ("_id", "desc", "carousel_images", "default_promotion", "default_promotion_expires_at"):
            self.assertNotIn(field, result[0])

    def test_identity_lookup_is_platform_scoped_and_does_not_fetch_content(self):
        self.col.docs = [
            {"platform": "pdd", "product_id": "00123", "goods_sign": "SAME", "desc": "不要读取"},
            {"platform": "taobao", "product_id": "AB", "goods_sign": "SAME", "name": "也不读取"},
            {"platform": "jd", "goods_id": "LEGACY", "goods_sign": "OLD"},
        ]
        self.assertEqual(self.repo.get_product_identity("SAME")["product_id"], "00123")
        self.assertEqual(self.repo.get_product_identity(" SAME ", "tb"), {
            "platform": "taobao", "product_id": "AB", "goods_sign": "SAME",
        })
        self.assertEqual(self.col.queries[-1], {"platform": "taobao", "goods_sign": "SAME"})
        self.assertEqual(self.col.projections[-1], {"_id": 0, "platform": 1, "product_id": 1, "goods_id": 1, "goods_sign": 1})
        self.assertEqual(self.repo.get_product_identity("OLD", "jd")["goods_id"], "LEGACY")
        self.assertIsNone(self.repo.get_product_identity("SAME", "jd"))
        before = len(self.col.queries)
        self.assertIsNone(self.repo.get_product_identity("  "))
        self.assertEqual(len(self.col.queries), before)

    def test_detail_query_keeps_string_id_type_under_and(self):
        self.col.docs = [
            {"platform": "pdd", "product_id": 123, "name": "数字ID不能误配", "price": 1},
            {"platform": "pdd", "product_id": "00123", "name": "字符串ID", "price": 2},
        ]
        self.assertIsNone(self.repo.get_product_detail("123", key_type="id"))
        self.assertEqual(self.repo.get_product_detail("00123", key_type="id")["name"], "字符串ID")
        self.assertEqual(self.col.queries[-1]["$and"][0], {"platform": "pdd", "product_id": "00123"})

    def test_detail_write_preserves_service_fetch_time_and_summary_never_refreshes_it(self):
        utc = datetime.timezone.utc
        fetched = datetime.datetime(2025, 1, 2, 3, 4, 5, tzinfo=utc)
        for supplied in (fetched, fetched.replace(tzinfo=None), fetched.astimezone(datetime.timezone(datetime.timedelta(hours=8)))):
            with self.subTest(supplied=supplied):
                _, update = self.repo._build_product_update({"goods_id": "1", "detail_updated_at": supplied}, detail=True)
                self.assertEqual(update["$set"]["detail_updated_at"], fetched.replace(tzinfo=None))
                _, summary = self.repo._build_product_update({"goods_id": "1", "detail_updated_at": supplied}, detail=False)
                self.assertNotIn("detail_updated_at", summary["$set"])
        for invalid in (None, "2025-01-02", 123):
            before = datetime.datetime.now(utc).replace(tzinfo=None)
            _, update = self.repo._build_product_update({"goods_id": "1", "detail_updated_at": invalid}, detail=True)
            after = datetime.datetime.now(utc).replace(tzinfo=None)
            self.assertLessEqual(before, update["$set"]["detail_updated_at"])
            self.assertLessEqual(update["$set"]["detail_updated_at"], after)

    def test_sign_only_promotion_cache_uses_same_unique_identity_strategy(self):
        for sign in ("S1", "S2"):
            self.assertTrue(self.repo.save_promotion_link_cache(
                "pdd", {"final_link": f"https://example.com/{sign}", "from_cache": True}, goods_sign=sign,
            ))
            result = self.repo.get_promotion_link_cache("pdd", goods_sign=sign)
            self.assertEqual(result["final_link"], f"https://example.com/{sign}")
            self.assertTrue(result["from_cache"])
        self.assertEqual(len(self.col.docs), 2)
        self.assertNotIn("from_cache", self.col.docs[0]["default_promotion"])
        self.assertNotIn("detail_updated_at", self.col.docs[0])
        self.repo.upsert_product_detail({"goods_id": "123", "goods_sign": "S1", "price": 10})
        self.assertEqual(len(self.col.docs), 2)
        self.assertEqual(self.repo.get_promotion_link_cache("pdd", goods_id="123")["final_link"], "https://example.com/S1")

    def test_non_numeric_platform_ids_work_for_explicit_promotion_identity(self):
        self.assertTrue(self.repo.save_promotion_link_cache("tb", {"final_link": "https://example.com/AB"}, goods_id="AB"))
        self.assertEqual(self.repo.get_promotion_link_cache("taobao", goods_id="AB")["final_link"], "https://example.com/AB")
        self.assertEqual(self.col.docs[0]["platform"], "taobao")

    def test_explicit_numeric_sign_does_not_read_or_overwrite_same_numeric_id(self):
        self.assertTrue(self.repo.save_promotion_link_cache(
            "pdd", {"final_link": "https://example.com/by-id"}, goods_id="123", goods_sign="ID_SIGN",
        ))
        self.assertTrue(self.repo.save_promotion_link_cache(
            "pdd", {"final_link": "https://example.com/by-sign"}, goods_id="456", goods_sign="123",
        ))
        original_id_doc = deepcopy(self.col.docs[0])
        result = self.repo.get_promotion_link_cache("pdd", goods_sign="123", goods_key="123")
        self.assertEqual(result["final_link"], "https://example.com/by-sign")
        self.assertEqual(self.col.queries[-1], {"platform": "pdd", "goods_sign": "123"})
        self.assertTrue(self.repo.save_promotion_link_cache(
            "pdd", {"final_link": "https://example.com/updated-sign"}, goods_sign="123", goods_key="123",
        ))
        self.assertEqual(self.col.docs[0], original_id_doc)
        self.assertEqual(self.col.docs[1]["product_id"], "456")
        self.assertEqual(self.col.docs[1]["default_promotion"]["final_link"], "https://example.com/updated-sign")
        self.assertEqual(len(self.col.docs), 2)

    def test_explicit_non_numeric_id_does_not_invent_or_overwrite_sign(self):
        self.assertTrue(self.repo.save_promotion_link_cache(
            "taobao", {"final_link": "https://example.com/by-id"}, goods_id="sku-A", goods_sign="REAL_SIGN",
        ))
        self.assertTrue(self.repo.save_promotion_link_cache(
            "taobao", {"final_link": "https://example.com/by-sign"}, goods_id="sku-B", goods_sign="sku-A",
        ))
        original_sign_doc = deepcopy(self.col.docs[1])
        result = self.repo.get_promotion_link_cache("tb", goods_id="sku-A", goods_key="sku-A")
        self.assertEqual(result["final_link"], "https://example.com/by-id")
        self.assertEqual(self.col.queries[-1], {"platform": "taobao", "product_id": "sku-A"})
        self.assertTrue(self.repo.save_promotion_link_cache(
            "tb", {"final_link": "https://example.com/updated-id"}, goods_id="sku-A", goods_key="sku-A",
        ))
        self.assertEqual(self.col.docs[0]["goods_sign"], "REAL_SIGN")
        self.assertEqual(self.col.docs[0]["default_promotion"]["final_link"], "https://example.com/updated-id")
        self.assertEqual(self.col.docs[1], original_sign_doc)
        self.assertEqual(self.repo.get_promotion_link_cache("tb", goods_sign="sku-A")["final_link"], "https://example.com/by-sign")

    def test_explicit_invalid_id_is_not_reinterpreted_using_legacy_key(self):
        self.assertTrue(self.repo.save_promotion_link_cache(
            "pdd", {"final_link": "https://example.com/123"}, goods_id="123",
        ))
        before = deepcopy(self.col.docs)
        for invalid_id in ("0", "sign:internal"):
            with self.subTest(invalid_id=invalid_id):
                self.assertIsNone(self.repo.get_promotion_link_cache("pdd", goods_id=invalid_id, goods_key="123"))
                self.assertFalse(self.repo.save_promotion_link_cache(
                    "pdd", {"final_link": "https://example.com/wrong"}, goods_id=invalid_id, goods_key="123",
                ))
        self.assertEqual(self.col.docs, before)

    def test_promotion_cache_uses_utc_expiry_and_minimal_projection(self):
        now = 1_800_000_000.0
        with patch("repositories.product_repo.time.time", return_value=now):
            for timezone in (None, datetime.timezone.utc, datetime.timezone(datetime.timedelta(hours=8))):
                with self.subTest(timezone=timezone):
                    expiry = datetime.datetime.fromtimestamp(now + 60, datetime.timezone.utc)
                    expiry = expiry.replace(tzinfo=None) if timezone is None else expiry.astimezone(timezone)
                    self.col.docs = [{
                        "platform": "pdd", "product_id": "1", "default_promotion_expires_at": expiry,
                        "default_promotion": {"final_link": "https://example.com/1", "nested": {"value": 1}},
                        "default_promotion_updated_at": datetime.datetime(2000, 1, 1), "desc": "不要读取详情",
                    }]
                    result = self.repo.get_promotion_link_cache("pdd", goods_id="1")
                    self.assertEqual(result["final_link"], "https://example.com/1")
                    self.assertTrue(result["from_cache"])
                    self.assertIsInstance(result["_expires_at"], float)
                    self.assertEqual(result["_expires_at"], now + 60)
                    self.assertEqual(self.col.projections[-1], {
                        "_id": 0, "default_promotion": 1, "default_promotion_expires_at": 1,
                    })
                    result["nested"]["value"] = 2
                    self.assertEqual(self.col.docs[0]["default_promotion"]["nested"]["value"], 1)

    def test_promotion_cache_rejects_missing_invalid_expired_or_distant_expiry(self):
        now = 1_800_000_000.0
        utc_dt = lambda seconds: datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
        expiries = (None, "2099-01-01", now + 60, {}, utc_dt(now - 1), utc_dt(now), utc_dt(now + PROMOTION_LINK_TTL + 1))
        with patch("repositories.product_repo.time.time", return_value=now):
            for expiry in expiries:
                with self.subTest(expiry=expiry):
                    self.col.docs = [{
                        "platform": "pdd", "product_id": "1",
                        "default_promotion": {"final_link": "https://example.com/1"},
                        "default_promotion_updated_at": utc_dt(now),
                    }]
                    if expiry is not None:
                        self.col.docs[0]["default_promotion_expires_at"] = expiry
                    self.assertIsNone(self.repo.get_promotion_link_cache("pdd", goods_id="1"))
            for seconds in (now + 0.001, now + PROMOTION_LINK_TTL):
                self.col.docs[0]["default_promotion_expires_at"] = utc_dt(seconds)
                self.assertIsNotNone(self.repo.get_promotion_link_cache("pdd", goods_id="1"))
            for payload in (None, "link", {}, {"final_link": ""}):
                self.col.docs[0]["default_promotion"] = payload
                self.assertIsNone(self.repo.get_promotion_link_cache("pdd", goods_id="1"))

    def test_promotion_save_keeps_supplied_expiry_without_renewing_and_strips_transient_fields(self):
        now = 1_800_000_000.0
        data = {
            "final_link": "https://example.com/1", "command_text": "请求级口令",
            "from_cache": True, "_expires_at": now + 5, "_debug": "internal",
        }
        with patch("repositories.product_repo.time.time", return_value=now):
            self.assertTrue(self.repo.save_promotion_link_cache("pdd", data, goods_id="1", expires_at=now + 10))
            stored = self.col.docs[0]
            self.assertEqual(stored["default_promotion"], {"final_link": "https://example.com/1"})
            self.assertEqual(stored["default_promotion_updated_at"], datetime.datetime.fromtimestamp(now, datetime.timezone.utc))
            self.assertEqual(stored["default_promotion_expires_at"].timestamp(), now + 10)
            self.assertEqual(stored["default_promotion_expires_at"].utcoffset(), datetime.timedelta(0))
            self.assertIn("command_text", data)
        with patch("repositories.product_repo.time.time", return_value=now + 9):
            self.assertEqual(self.repo.get_promotion_link_cache("pdd", goods_id="1")["_expires_at"], now + 10)
        with patch("repositories.product_repo.time.time", return_value=now + 10):
            self.assertIsNone(self.repo.get_promotion_link_cache("pdd", goods_id="1"))

    def test_promotion_save_defaults_to_shared_ttl_and_preserves_legacy_goods_key_api(self):
        now = 1_800_000_000.123456
        expected_expiry = 1_800_000_000.123 + PROMOTION_LINK_TTL
        with patch("repositories.product_repo.time.time", return_value=now):
            self.assertTrue(self.repo.save_promotion_link_cache("pdd", {"final_link": "https://example.com/1"}, goods_key="001"))
            self.assertEqual(self.col.docs[0]["product_id"], "001")
            self.assertEqual(self.col.docs[0]["default_promotion_expires_at"].timestamp(), expected_expiry)
            self.assertEqual(self.repo.get_promotion_link_cache("pdd", goods_key="001")["_expires_at"], expected_expiry)
            self.assertTrue(self.repo.save_promotion_link_cache("pdd", {"final_link": "https://example.com/S"}, goods_key="SIGN"))
            self.assertEqual(self.col.docs[1]["goods_sign"], "SIGN")
            self.assertIsNotNone(self.repo.get_promotion_link_cache("pdd", goods_key="SIGN"))
            self.assertIsNone(self.repo.get_promotion_link_cache("jd", goods_key="SIGN"))

    def test_promotion_save_rejects_invalid_expiry_without_writing(self):
        now = 1_800_000_000.0
        with patch("repositories.product_repo.time.time", return_value=now):
            for expiry in (True, "1800000001", float("nan"), float("inf"), now - 1, now, now + PROMOTION_LINK_TTL + 1):
                with self.subTest(expiry=expiry):
                    self.assertFalse(self.repo.save_promotion_link_cache(
                        "pdd", {"final_link": "https://example.com/1"}, goods_id="1", expires_at=expiry,
                    ))
            self.assertEqual(self.col.docs, [])


if __name__ == "__main__":
    unittest.main()
