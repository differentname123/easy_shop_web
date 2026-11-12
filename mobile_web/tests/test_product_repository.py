# -*- coding: utf-8 -*-
"""商品仓储确定性回归测试：无外部 API、无真实 MongoDB。"""
import datetime
import re
import threading
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo.errors import DuplicateKeyError
from pymongo.results import UpdateResult

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
        self.write_calls = []
        self.lock = threading.RLock()

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    @staticmethod
    def get_path(doc, path):
        current = doc
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current

    @staticmethod
    def expression(doc, value, variables=None):
        """仅支持可展示价格条件使用的表达式；未知操作立即失败。"""
        variables = variables or {}
        if isinstance(value, str) and value.startswith("$$"):
            return variables[value[2:]]
        if isinstance(value, str) and value.startswith("$"):
            return MemoryCollection.get_path(doc, value[1:])[1]
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
        if op == "$or":
            return any(evaluate(item) for item in args)
        left, right = (evaluate(item) for item in args)
        if op == "$eq":
            return MemoryCollection.equal(left, right)
        if op == "$in":
            return any(MemoryCollection.equal(left, candidate) for candidate in right)
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
                exists, field_val = MemoryCollection.get_path(doc, key)
                matched = True
                for op, operand in value.items():
                    if op == "$regex":
                        flags = re.I if value.get("$options") == "i" else 0
                        values = field_val if isinstance(field_val, list) else [field_val]
                        matched = matched and any(isinstance(item, str) and re.search(operand, item, flags) for item in values)
                    elif op == "$exists":
                        matched = matched and (exists == operand)
                    elif op == "$eq":
                        matched = matched and MemoryCollection.equal(field_val, operand)
                    elif op == "$ne":
                        matched = matched and not MemoryCollection.equal(field_val, operand)
                    elif op != "$options":
                        raise AssertionError(f"unsupported field operator: {op}")
            else:
                _, field_val = MemoryCollection.get_path(doc, key)
                matched = MemoryCollection.equal(field_val, value)
            if not matched:
                return False
        return True

    @staticmethod
    def equal(left, right):
        if isinstance(left, bool) != isinstance(right, bool):
            return False
        # Mongo 日期查询按 UTC 毫秒比较，不能依赖 Python aware/naive 相等。
        if isinstance(left, datetime.datetime) and isinstance(right, datetime.datetime):
            def bson_time(value):
                value = (value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None
                         else value.astimezone(datetime.timezone.utc))
                return value.replace(microsecond=value.microsecond // 1000 * 1000)
            return bson_time(left) == bson_time(right)
        return left == right

    @staticmethod
    def project(doc, projection):
        if projection is None:
            return deepcopy(doc)
        return {key: deepcopy(value) for key, value in doc.items() if projection.get(key)}

    def find_one(self, query, projection=None):
        with self.lock:
            self.queries.append(deepcopy(query))
            self.projections.append(deepcopy(projection))
            for doc in self.docs:
                if self.matches(doc, query):
                    # 直接注入的旧文档 fixture 也遵循 Mongo 文档总有 _id 的约定。
                    doc.setdefault("_id", ObjectId())
                    return self.project(doc, projection)
            return None

    def find(self, query, projection=None):
        self.queries.append(deepcopy(query))
        self.projections.append(deepcopy(projection))
        docs = [self.project(doc, projection) for doc in self.docs if self.matches(doc, query)]
        return MemoryCursor(docs, self.cursor_calls)

    def distinct(self, field, query):
        self.queries.append(deepcopy(query))
        return list(dict.fromkeys(doc.get(field) for doc in self.docs if self.matches(doc, query)))

    def update_one(self, query, update, upsert=False):
        with self.lock:
            self.write_calls.append((deepcopy(query), deepcopy(update), upsert))
            if set(update.get("$set", {})) & set(update.get("$setOnInsert", {})):
                raise ValueError("MongoDB rejects conflicting update paths")
            index = next((i for i, doc in enumerate(self.docs) if self.matches(doc, query)), None)
            inserted = index is None
            if inserted and not upsert:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            if inserted:
                doc = {key: deepcopy(value) for key, value in query.items()
                       if not key.startswith("$") and not isinstance(value, dict)}
                doc.update(deepcopy(update.get("$setOnInsert", {})))
            else:
                doc = deepcopy(self.docs[index])
            doc.update(deepcopy(update.get("$set", {})))
            doc.setdefault("_id", ObjectId())
            identity = (doc.get("platform"), doc.get("product_id"))
            for i, other in enumerate(self.docs):
                if i != index and (other.get("platform"), other.get("product_id")) == identity:
                    raise DuplicateKeyError("duplicate unique(platform, product_id)")
                if i != index and other.get("_id") == doc["_id"]:
                    raise DuplicateKeyError("duplicate unique(_id)")
            modified = not inserted and doc != self.docs[index]
            if inserted:
                self.docs.append(doc)
            else:
                self.docs[index] = doc
            raw_result = {"n": 1, "nModified": int(modified)}
            if inserted:
                raw_result["upserted"] = doc["_id"]
            return UpdateResult(raw_result, acknowledged=True)

    def bulk_write(self, operations, ordered=False):
        inserted = modified = 0
        for operation in operations:
            result = self.update_one(operation._filter, operation._doc, upsert=operation._upsert)
            inserted += int(result.upserted_id is not None)
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

    def test_summary_material_url_is_persisted_without_creating_a_detail_clock(self):
        url = "https://item.jd.com/123.html"
        self.assertEqual(self.repo.upsert_home_products([{
            "platform": "jd", "goods_id": "123", "name": "摘要商品", "price": 10,
            "product_url": f"  {url}  ", "desc": "摘要不得冒充详情",
            "detail_updated_at": datetime.datetime(2020, 1, 1),
        }], channel_type=5), 1)
        stored = self.col.docs[0]
        self.assertEqual(stored["product_url"], url)
        self.assertNotIn("detail_updated_at", stored)
        self.assertNotIn("desc", stored)
        self.assertEqual(self.repo.query_boom_products(platform="jd")["data"][0]["product_url"], url)
        self.assertIsNone(self.repo.get_product_detail("123", platform="jd")["detail_updated_at"])

        old_detail_time = datetime.datetime(2020, 1, 1)
        self.assertTrue(self.repo.upsert_product_detail({
            "platform": "jd", "goods_id": "123", "desc": "真实详情",
            "detail_updated_at": old_detail_time,
        }))
        self.repo.upsert_home_products([{
            "platform": "jd", "goods_id": "123", "product_url": url + "?source=summary",
        }])
        self.assertEqual(self.col.docs[0]["detail_updated_at"], old_detail_time)
        self.assertEqual(self.col.docs[0]["desc"], "真实详情")
        self.assertEqual(self.col.docs[0]["product_url"], url + "?source=summary")

    def test_empty_detail_or_summary_material_url_does_not_erase_known_link(self):
        url = "https://item.jd.com/123.html"
        self.repo.upsert_home_products([{
            "platform": "jd", "goods_id": "123", "name": "商品", "price": 10, "product_url": url,
        }])
        for index, empty_url in enumerate(("", None, " \t ")):
            with self.subTest(product_url=empty_url):
                self.assertTrue(self.repo.upsert_product_detail({
                    "platform": "jd", "goods_id": "123", "product_url": empty_url,
                    "desc": "真实详情", "detail_updated_at": datetime.datetime(2020, 1, 1 + index),
                }))
                self.assertEqual(self.col.docs[0]["product_url"], url)
                self.repo.upsert_home_products([{
                    "platform": "jd", "goods_id": "123", "product_url": empty_url,
                }])
                self.assertEqual(self.col.docs[0]["product_url"], url)

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
            self.assertEqual(result["product_id"], sign)
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
        self.assertIsNone(self.repo.get_product_detail("None", key_type="id"))
        self.assertEqual(self.col.docs, [])
        self.assertEqual(self.col.queries, [])

    def test_price_and_savings_sort_indexes_are_added_to_existing_indexes(self):
        self.assertEqual(self.col.indexes, [
            ([("platform", 1), ("product_id", 1)], {"unique": True, "background": True}),
            ("goods_sign", {"background": True}),
            ([("updated_at", -1)], {"background": True}),
            ([("platform", 1), ("updated_at", -1)], {"background": True}),
            ([("activity_price", 1)], {"background": True}),
            ([("platform", 1), ("activity_price", 1)], {"background": True}),
            ([("saved_price", -1), ("updated_at", -1)], {"background": True}),
            ([("platform", 1), ("saved_price", -1), ("updated_at", -1)], {"background": True}),
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
                self.col.docs = [{"platform": "pdd", "product_id": "001", "name": "商品", "channel_type": 5, **money}]
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
                self.col.docs = [{"platform": "pdd", "product_id": "001", "name": name, "price": 0, "channel_type": 5}]
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
        self.repo.upsert_home_products([{"goods_id": "00123", "name": "后补商品", "price": 0, "category": "食品"}], channel_type=5)
        self.assertEqual(len(self.col.docs), 1)
        self.assertEqual(self.repo.query_boom_products()["data"][0]["price"], 0)
        self.assertEqual(self.repo.get_product_detail("SIGN", key_type="sign")["goods_id"], "00123")
        self.assertEqual(self.repo.get_categories(), ["食品"])
        self.assertIsNotNone(self.repo.get_promotion_link_cache("pdd", goods_id="00123"))

    def test_content_filter_runs_before_pagination_and_keeps_count_semantics(self):
        now = datetime.datetime(2026, 11, 3)
        for number in range(8):
            doc = {"platform": "pdd", "product_id": str(number + 1), "updated_at": now - datetime.timedelta(minutes=number), "channel_type": 5}
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
            {"platform": "pdd", "name": "商品", "activity_price": 0, "category": "食品", "channel_type": 5},
            {"platform": "pdd", "name": "缺价", "category": "推广裸类", "channel_type": 5},
            {"platform": "pdd", "price": 0, "category": "无名类", "channel_type": 5},
            {"platform": "pdd", "name": "无类", "price": 1, "category": " ", "channel_type": 5},
            {"platform": "pdd", "name": "占位类", "price": 1, "category": "未知标签_1", "channel_type": 5},
            {"platform": "pdd", "name": "超长类", "price": 1, "category": "这是一个超过二十个字符长度的异常脏数据分类名", "channel_type": 5},
            {"platform": "taobao", "name": "另一平台", "price": "3", "category": "美妆", "channel_type": 1},
        ]
        self.assertEqual(set(self.repo.get_categories()), {"食品", "美妆"})
        self.assertEqual(self.repo.get_categories("pdd"), ["食品"])
        self.assertEqual(self.repo.get_categories("tb"), ["美妆"])
        self.assertEqual(self.col.queries[-1]["$and"][0], {"platform": "taobao"})
        self.assertEqual(self.col.queries[-1]["$and"][1], self.repo._boom_content_filter())

    def test_boom_products_and_categories_only_query_ranking_data_excluding_search_and_detail_only_records(self):
        # 1. 搜索摘要入库 (channel_type=None) 与单商品详情入库均不得进入爆单推广或爆单分类
        self.repo.upsert_home_products([{
            "platform": "jd", "goods_id": "search-jd-1", "name": "京东搜索商品",
            "price": 39.9, "category": "搜索专属类目", "product_url": "https://item.jd.com/search-jd-1.html",
        }], channel_type=None)
        self.repo.upsert_product_detail({
            "platform": "taobao", "goods_id": "detail-tb-1", "name": "淘宝详情预取或散查商品",
            "price": 59.9, "category": "详情专属类目",
        })
        self.assertEqual(self.repo.query_boom_products()["data"], [])
        self.assertEqual(self.repo.get_categories(), [])
        # 但详情缓存与物料链接反查仍可正常命中
        self.assertEqual(self.repo.get_product_detail("search-jd-1", platform="jd")["name"], "京东搜索商品")
        self.assertEqual(self.repo.get_product_detail("detail-tb-1", platform="taobao")["name"], "淘宝详情预取或散查商品")

        # 2. 非法 channel_type (None / bool / str / list) 同样被爆单查询排除
        for invalid_channel in (None, True, False, "5", [5]):
            with self.subTest(invalid_channel=invalid_channel):
                self.col.docs = [{
                    "platform": "pdd", "product_id": "bad-ch", "name": "脏频道商品",
                    "activity_price": 9.9, "category": "脏频道类目", "channel_type": invalid_channel,
                }]
                self.assertEqual(self.repo.query_boom_products()["data"], [])
                self.assertEqual(self.repo.get_categories(), [])

        # 3. 榜单商品 (channel_type=1 或 5) 可被爆单推广和分类正常查询，且后续详情入库或搜索更新不会抹除其榜单属性
        self.col.docs = []
        self.repo.upsert_home_products([{
            "platform": "pdd", "goods_id": "rank-pdd-1", "name": "拼多多榜单爆品",
            "price": 19.9, "category": "百货",
        }], channel_type=5)
        self.repo.upsert_product_detail({
            "platform": "pdd", "goods_id": "rank-pdd-1", "name": "拼多多榜单爆品",
            "price": 19.9, "desc": "完整详情",
        })
        self.repo.upsert_home_products([{
            "platform": "pdd", "goods_id": "rank-pdd-1", "name": "拼多多榜单爆品",
            "price": 18.9,
        }], channel_type=None)
        boom_items = self.repo.query_boom_products()["data"]
        self.assertEqual([item["product_id"] for item in boom_items], ["rank-pdd-1"])
        self.assertEqual(boom_items[0]["price"], 18.9)
        self.assertEqual(self.repo.get_categories(), ["百货"])

    def test_list_projection_preserves_complete_summary_but_not_rich_or_cache_fields(self):
        doc = {
            "_id": "mongo-id", "platform": "tb", "product_id": "AB", "goods_id": "AB",
            "goods_sign": "SIGN", "name": "商品", "brand": "品牌", "category": "类目",
            "activity_price": 0, "price": 90, "original_price": 100, "saved_price": 0,
            "coupon_amount": 0, "has_coupon": False, "commission_rate": 0, "commission_amount": 0,
            "sales_tip": "100人付款", "image_url": "https://example.com/a.png", "mall_name": "店铺",
            "updated_at": datetime.datetime(2026, 11, 3), "channel_type": 5, "desc": "大详情",
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
                "product_url",
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
        for invalid_id in ("0", "None"):
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

    def test_taobao_and_jd_canonical_product_id_deduplicates_rotating_goods_signs(self):
        # 1. 淘宝：动态前缀变化但 `-` 后缀恒定时，入库合并为唯一 product_id，并刷新最新 goods_sign
        tb_sign_1 = "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd"
        tb_sign_2 = "7b4f7m5p92H8b6B69R97Yf6t6X-0v7DKOgI5JqnZd4Nhxd"
        self.repo.upsert_home_products([{
            "platform": "taobao", "goods_id": tb_sign_1, "goods_sign": tb_sign_1,
            "name": "淘宝同款商品", "price": 29.9,
        }])
        self.repo.upsert_home_products([{
            "platform": "taobao", "goods_id": tb_sign_2, "goods_sign": tb_sign_2,
            "name": "淘宝同款商品", "price": 26.9,
        }])
        tb_docs = [d for d in self.col.docs if d.get("platform") == "taobao"]
        self.assertEqual(len(tb_docs), 1)
        self.assertEqual(tb_docs[0]["product_id"], "0v7DKOgI5JqnZd4Nhxd")
        self.assertEqual(tb_docs[0]["goods_sign"], tb_sign_2)
        self.assertEqual(tb_docs[0]["activity_price"], 26.9)

        # 2. 京东：跨接口（榜单 itemId vs 搜索/详情 tao_id）拥有相同 360buyimg.com 主图 URL 时，
        #    统一提取 <parent_hash>_<file_stem> 作为唯一 product_id，并刷新最新 goods_sign
        jd_img = "https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg"
        jd_rank_sign = "BWQ9383WurxZ8XgYyttv5GNgYyttv5_3rtcLEOl4l98a1A8AG"
        jd_search_sign = "H942Tfk1n9iTlRHE8K75IkKfe6mXtQ_3Mqe07A69i2lWGNpGd"
        self.repo.upsert_home_products([{
            "platform": "jd", "goods_id": jd_rank_sign, "goods_sign": jd_rank_sign,
            "name": "京东同款商品", "price": 99.0, "image_url": jd_img,
        }])
        self.repo.upsert_home_products([{
            "platform": "jd", "goods_id": jd_search_sign, "goods_sign": jd_search_sign,
            "name": "京东同款商品", "price": 89.0, "image_url": jd_img,
        }])
        jd_docs = [d for d in self.col.docs if d.get("platform") == "jd"]
        self.assertEqual(len(jd_docs), 1)
        self.assertEqual(jd_docs[0]["product_id"], "6ae3f40fF84b3cfef_0083320320005d73")
        self.assertEqual(jd_docs[0]["goods_sign"], jd_search_sign)
        self.assertEqual(jd_docs[0]["activity_price"], 89.0)

    def test_cost_performance_unit_normalization_and_frequency_grouping(self):
        now = datetime.datetime(2026, 11, 8, 12, 0, 0)
        # 质量类：2 个商品为“斤”，1 个商品为“kg” -> 同度量衡最多单位为“斤”（共 3 个商品归入“斤”组）
        # P1: 2 斤 @ 10 元 -> 0.2 斤/元
        # P2: 1 kg (= 2 斤) @ 5 元 -> 0.4 斤/元 (斤组第1)
        # P3: 1 斤 @ 10 元 -> 0.1 斤/元 (斤组第3)
        # 离散单位“包”：共 2 个商品（少于“斤”组的 3 个，排在“斤”组之后）
        # P4: 10 包 @ 20 元 -> 0.5 包/元 (包组第2)
        # P5: 30 包 @ 30 元 -> 1.0 包/元 (包组第1 -> P5 在 P4 前)
        self.col.docs = [
            {
                "platform": "pdd", "product_id": "P1", "name": "苹果2斤", "activity_price": 10.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=5),
                "format_status": "success",
                "format_info": {
                    "price": 10.0,
                    "pricing_basis": {
                        "base_unit": "斤", "total_value": 2,
                        "structure": [{"unit": "斤", "value": 2}],
                    },
                },
            },
            {
                "platform": "pdd", "product_id": "P2", "name": "苹果1kg", "activity_price": 5.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=4),
                "format_status": "success",
                "format_info": {
                    "price": 5.0,
                    "pricing_basis": {
                        "base_unit": "kg", "total_value": 1,
                        "structure": [{"unit": "kg", "value": 1}],
                    },
                },
            },
            {
                "platform": "pdd", "product_id": "P3", "name": "苹果1斤", "activity_price": 10.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=3),
                "format_status": "success",
                "format_info": {
                    "price": 10.0,
                    "pricing_basis": {
                        "base_unit": "斤", "total_value": 1,
                        "structure": [{"unit": "斤", "value": 1}],
                    },
                },
            },
            {
                "platform": "pdd", "product_id": "P4", "name": "抽纸10包", "activity_price": 20.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=2),
                "format_status": "success",
                "format_info": {
                    "price": 20.0,
                    "pricing_basis": {
                        "base_unit": "包", "total_value": 10,
                        "structure": [{"unit": "包", "value": 10}],
                    },
                },
            },
            {
                "platform": "pdd", "product_id": "P5", "name": "抽纸30包", "activity_price": 30.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=1),
                "format_status": "success",
                "format_info": {
                    "price": 30.0,
                    "pricing_basis": {
                        "base_unit": "包", "total_value": 30,
                        "structure": [{"unit": "箱", "value": 2}, {"unit": "包", "value": 15}],
                    },
                },
            },
        ]
        ranked = self.repo.rank_boom_products_by_cost_performance(platform="pdd")
        # “斤”组共 3 个商品 (P2 > P1 > P3)，“包”组共 2 个商品 (P5 > P4) -> 按 2 : 1 比例轮询穿插
        self.assertEqual([item["product_id"] for item in ranked], ["P2", "P1", "P5", "P3", "P4"])
        for item in ranked:
            self.assertNotIn("format_info", item)
            self.assertNotIn("format_status", item)
            self.assertNotIn("cost_performance_score", item)

        # 验证跨单位重复第 1 名自动顺延去重：
        # T1 同时具备“包”(1.0包/元, 第1) 和“抽”(100抽/元, 第1)
        # “包”组有 3 个商品 (T1 > T3 > T2)，“抽”组有 2 个商品 (T1 > T2)
        # 第一轮：“包”取 2 个 (T1, T3)，接着“抽”取 1 个时因 T1 已出列，自动顺延取 T2
        self.col.docs = [
            {
                "platform": "pdd", "product_id": "T1", "name": "维达20包*100抽", "activity_price": 20.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=3),
                "format_status": "success",
                "format_info": {
                    "price": 20.0,
                    "pricing_basis": {
                        "base_unit": "抽", "total_value": 2000,
                        "structure": [{"unit": "包", "value": 20}, {"unit": "抽", "value": 100}],
                    },
                },
            },
            {
                "platform": "pdd", "product_id": "T2", "name": "清风15包*100抽", "activity_price": 30.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=2),
                "format_status": "success",
                "format_info": {
                    "price": 30.0,
                    "pricing_basis": {
                        "base_unit": "抽", "total_value": 1500,
                        "structure": [{"unit": "包", "value": 15}, {"unit": "抽", "value": 100}],
                    },
                },
            },
            {
                "platform": "pdd", "product_id": "T3", "name": "心相印18包", "activity_price": 20.0,
                "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=1),
                "format_status": "success",
                "format_info": {
                    "price": 20.0,
                    "pricing_basis": {
                        "base_unit": "包", "total_value": 18,
                        "structure": [{"unit": "包", "value": 18}],
                    },
                },
            },
        ]
        dedup_ranked = self.repo.rank_boom_products_by_cost_performance(platform="pdd")
        self.assertEqual([item["product_id"] for item in dedup_ranked], ["T1", "T3", "T2"])

    def test_cost_performance_multi_sku_selects_highest_score_valid_sku_and_places_unformatted_last(self):
        now = datetime.datetime(2026, 11, 8, 12, 0, 0)
        self.col.docs = [
            # 未完成 format_info 清洗的商品（新）
            {
                "platform": "pdd", "product_id": "UNFORMATTED_NEW", "name": "未清洗新商品",
                "activity_price": 9.9, "channel_type": 5, "updated_at": now,
            },
            # 未完成 format_info 清洗的商品（旧）
            {
                "platform": "pdd", "product_id": "UNFORMATTED_OLD", "name": "未清洗旧商品",
                "activity_price": 8.8, "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=10),
                "format_status": "failed",
                "format_info": {
                    "price": 8.8,
                    "pricing_basis": {"base_unit": "g", "total_value": 500, "structure": [{"unit": "g", "value": 500}]},
                },
            },
            # 单 SKU 商品：500g @ 50元 -> 10 g/元
            {
                "platform": "pdd", "product_id": "SINGLE_SKU", "name": "单规格500g",
                "activity_price": 50.0, "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=2),
                "format_status": "success",
                "format_info": {
                    "price": 50.0,
                    "pricing_basis": {"base_unit": "g", "total_value": 500, "structure": [{"unit": "g", "value": 500}]},
                },
            },
            # 多 SKU 商品：
            # - 脏规格：price(10) < activity_price(20)，即使性价比极高也必须过滤
            # - 低性价比合法规格：100g @ 20元 -> 5 g/元
            # - 高性价比合法规格：500g * 2袋 @ 50元 -> 20 g/元（应作为该商品代表主规格）
            {
                "platform": "pdd", "product_id": "MULTI_SKU", "name": "多规格商品",
                "activity_price": 20.0, "channel_type": 5, "updated_at": now - datetime.timedelta(minutes=5),
                "format_status": "success",
                "sku_info": [
                    {"sku": "引流脏规格", "price": 10.0},
                    {"sku": "小包装", "price": 20.0},
                    {"sku": "家庭大包装", "price": 50.0},
                ],
                "format_info": {
                    "MULTI_SKU_1": {
                        "price": 10.0,
                        "pricing_basis": {"base_unit": "g", "total_value": 99999, "structure": [{"unit": "g", "value": 99999}]},
                    },
                    "MULTI_SKU_2": {
                        "price": 20.0,
                        "pricing_basis": {"base_unit": "g", "total_value": 100, "structure": [{"unit": "g", "value": 100}]},
                    },
                    "MULTI_SKU_3": {
                        "price": 50.0,
                        "pricing_basis": {
                            "base_unit": "g", "total_value": 1000,
                            "structure": [{"unit": "袋", "value": 2}, {"unit": "g", "value": 500}],
                        },
                    },
                },
            },
        ]
        ranked = self.repo.rank_boom_products_by_cost_performance(platform="pdd")
        # MULTI_SKU (20 g/元) > SINGLE_SKU (10 g/元) > UNFORMATTED_NEW > UNFORMATTED_OLD
        self.assertEqual(
            [item["product_id"] for item in ranked],
            ["MULTI_SKU", "SINGLE_SKU", "UNFORMATTED_NEW", "UNFORMATTED_OLD"],
        )

    def test_service_get_boom_products_uses_cache_and_paginates_cost_performance(self):
        from cache import BOOM_SORT_CACHE, clear_all_caches, evict_product_from_caches
        from services.product_service import ProductService

        clear_all_caches()
        now = datetime.datetime(2026, 11, 8, 12, 0, 0)
        self.col.docs = [
            {
                "platform": "pdd", "product_id": f"P{i}", "name": f"商品{i}",
                "activity_price": 10.0, "channel_type": 5,
                "updated_at": now - datetime.timedelta(minutes=i),
                "format_status": "success",
                "format_info": {
                    "price": 10.0,
                    "pricing_basis": {"base_unit": "g", "total_value": 100 * i, "structure": [{"unit": "g", "value": 100 * i}]},
                },
            }
            for i in range(1, 4)
        ]
        service = ProductService(product_repo=self.repo)
        # 1. 无搜索关键词时：跨品类商品无可比性，即使传入 cost_performance 也使用最新上架 (latest) 排序
        no_kw_res = service.get_boom_products(platform="pdd", sort_by="cost_performance", page=1, page_size=3)
        self.assertEqual([item["product_id"] for item in no_kw_res["data"]], ["P1", "P2", "P3"])

        # 2. 有搜索关键词时：默认使用性价比最高 (cost_performance) 排序并写入 BOOM_SORT_CACHE
        queries_before = len(self.col.queries)
        page1 = service.get_boom_products(platform="pdd", keyword="商品", page=1, page_size=2)
        self.assertEqual([item["product_id"] for item in page1["data"]], ["P3", "P2"])
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["total_count"], 2)
        self.assertEqual(len(self.col.queries), queries_before + 1)

        # 第二页直接命中 BOOM_SORT_CACHE，不再查询数据库
        page2 = service.get_boom_products(platform="pdd", keyword="商品", page=2, page_size=2)
        self.assertEqual([item["product_id"] for item in page2["data"]], ["P1"])
        self.assertFalse(page2["has_more"])
        self.assertEqual(page2["total_count"], 1)
        self.assertEqual(len(self.col.queries), queries_before + 1)

        # 淘汰单个商品时同步从 BOOM_SORT_CACHE 中剔除该商品且不触发重新查库
        evict_product_from_caches("pdd", goods_id="P1")
        after_evict = service.get_boom_products(platform="pdd", keyword="商品", page=1, page_size=10)
        self.assertEqual([item["product_id"] for item in after_evict["data"]], ["P3", "P2"])
        self.assertEqual(len(self.col.queries), queries_before + 1)

    def test_deal_tip_normalization_and_boom_visibility(self):
        now = datetime.datetime(2026, 11, 12, 10, 0, 0)
        deal_doc = {
            "platform": "taobao",
            "product_id": "TB-DEAL-1",
            "goods_id": "TB-DEAL-1",
            "goods_sign": "SIGN-DEAL-1",
            "name": "立白大师香氛洗衣液8斤",
            "activity_price": 49.9,
            "price": 59.9,
            "category": "家清",
            "image_url": "https://img.test/main.jpg",
            "updated_at": now,
            "deal_info": {
                "is_deal_tip": True,
                "deal_price": 24.9,
                "unit_deal_price": "3.11元/斤",
                "price_logic": "领15元商品券+拍凑单品满200减30，主商品实付约24.9元",
                "publish_time": "2026-11-12T09:04:25Z",
                "coupon_links": [
                    {"link_id": "L1", "label": "15元商品券", "url": "https://uland.taobao.com/coupon/1", "token": "1$AAA$://", "platform": "tb"},
                    {"link_id": "BAD", "label": "空链接", "url": "", "token": ""},
                ],
                "filler_links": [
                    {"link_id": "L2", "label": "凑单棉签", "url": "https://s.click.taobao.com/filler", "token": "2$BBB$://", "platform": "taobao", "official_price": 9.9},
                ],
                "tutorial_steps": ["1. 先领15元券 1$AAA$://", "2. 加购主商品与凑单品一起结算"],
                "original_copywriting": "立白香氛8斤好价！留香超久～\n赶紧锁单！",
                "user_share_images": [
                    "https://wx4.sinaimg.cn/mw600/a.jpg",
                    "https://wx4.sinaimg.cn/mw600/a.jpg",
                    "ftp://invalid.test/x.jpg",
                ],
                "quality_score": 88,
                "source_site": "smzdm",
                "source_url": "https://internal.source/1",
                "my_promotion_link": "https://internal.promo/1",
            },
        }
        plain_doc = {
            "platform": "pdd",
            "product_id": "PDD-PLAIN-1",
            "name": "普通无爆料榜单商品",
            "activity_price": 12.8,
            "category": "食品",
            "channel_type": 5,
            "updated_at": now - datetime.timedelta(minutes=5),
        }
        non_bool_deal_doc = {
            "platform": "jd",
            "product_id": "JD-DIRTY-1",
            "name": "非法布尔值爆料",
            "activity_price": 19.9,
            "category": "数码",
            "deal_info": {"is_deal_tip": 1, "deal_price": 9.9},
        }
        self.col.docs = [deal_doc, plain_doc, non_bool_deal_doc]

        # 1. 严选好价列表同时接纳榜单商品与 deal_info.is_deal_tip == True 的线报商品，排除非布尔脏记录，且列表摘要不夹带重字段 deal_tip
        boom_res = self.repo.query_boom_products()
        self.assertEqual([item["product_id"] for item in boom_res["data"]], ["TB-DEAL-1", "PDD-PLAIN-1"])
        self.assertNotIn("deal_tip", boom_res["data"][0])
        self.assertEqual(sorted(self.repo.get_categories()), ["家清", "食品"])

        # 2. 详情反查返回脱敏后的 deal_tip 白名单，UTC 时间转为北京时间展示，内部源字段绝不外泄
        detail = self.repo.get_product_detail("TB-DEAL-1", platform="taobao")
        self.assertIsNotNone(detail["deal_tip"])
        tip = detail["deal_tip"]
        self.assertEqual(tip["publish_time"], "2026-11-12 17:04")
        self.assertEqual(tip["deal_price"], 24.9)
        self.assertEqual(tip["unit_deal_price"], "3.11元/斤")
        self.assertEqual(len(tip["coupon_links"]), 1)
        self.assertEqual(tip["coupon_links"][0]["platform"], "taobao")
        self.assertEqual(len(tip["filler_links"]), 1)
        self.assertEqual(tip["filler_links"][0]["official_price"], 9.9)
        self.assertEqual(tip["share_images"], ["https://wx4.sinaimg.cn/mw600/a.jpg"])
        for secret_key in ("quality_score", "source_site", "source_url", "my_promotion_link"):
            self.assertNotIn(secret_key, tip)

        # 3. 普通无爆料商品详情 deal_tip 为 None，且官方详情刷新写入不会破坏库中已有的 deal_info
        plain_detail = self.repo.get_product_detail("PDD-PLAIN-1", platform="pdd")
        self.assertIsNone(plain_detail["deal_tip"])

        self.assertTrue(self.repo.upsert_product_detail({
            "platform": "taobao", "goods_id": "TB-DEAL-1", "goods_sign": "SIGN-DEAL-1",
            "name": "立白大师香氛洗衣液8斤", "price": 49.9, "desc": "官方刷新详情",
        }))
        after_refresh = self.repo.get_product_detail("TB-DEAL-1", platform="taobao")
        self.assertEqual(after_refresh["desc"], "官方刷新详情")
        self.assertEqual(after_refresh["deal_tip"]["deal_price"], 24.9)


if __name__ == "__main__":
    unittest.main()

