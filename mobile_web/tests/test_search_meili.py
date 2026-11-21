# -*- coding: utf-8 -*-
"""
Meilisearch 全文搜索引擎客户端与仓储集成单元测试
涵盖主键清洗、文档转换、健康状态缓存、搜索过滤、排序降级与写穿透同步。
"""
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import datetime

from infrastructure.search_meili import (
    make_meili_doc_id,
    format_meili_doc,
    is_meili_available,
    search_products,
    index_documents,
    delete_documents,
)
from repositories.product_repo import ProductRepository


class TestSearchMeili(unittest.TestCase):

    def test_make_meili_doc_id_sanitization(self):
        """测试 Meilisearch 主键清洗：仅保留 a-zA-Z0-9-_，非法字符安全转为下划线"""
        self.assertEqual(make_meili_doc_id("pdd", "123456"), "pdd_123456")
        self.assertEqual(make_meili_doc_id("jd", "100.200:300"), "jd_100_200_300")
        self.assertEqual(make_meili_doc_id("taobao", "a/b@c+d"), "taobao_a_b_c_d")
        self.assertEqual(make_meili_doc_id("pdd", "商品123"), "pdd___123")

    def test_format_meili_doc_valid(self):
        """测试正常商品文档转换为 Meilisearch 索引结构"""
        doc = {
            "platform": "pdd",
            "product_id": "P1001",
            "name": "可口可乐 1.25L 大瓶装",
            "brand": "可口可乐",
            "mall_name": "可口可乐自营旗舰店",
            "category": "饮料",
            "price": 45.0,
            "activity_price": 38.5,
            "saved_price": 6.5,
            "is_off_promotion": False,
            "channel_type": 1,
            "deal_info": {"is_deal_tip": True},
            "image_url": "https://example.com/coke.jpg",
            "updated_at": datetime.datetime(2026, 11, 18, 12, 0, 0),
        }
        res = format_meili_doc(doc)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "pdd_P1001")
        self.assertEqual(res["product_id"], "P1001")
        self.assertEqual(res["platform"], "pdd")
        self.assertEqual(res["name"], "可口可乐 1.25L 大瓶装")
        self.assertEqual(res["price"], 45.0)
        self.assertEqual(res["activity_price"], 38.5)
        self.assertEqual(res["saved_price"], 6.5)
        self.assertFalse(res["is_off_promotion"])
        self.assertTrue(res["is_deal_tip"])
        self.assertEqual(res["channel_type"], 1)

    def test_format_meili_doc_invalid(self):
        """测试不合法或缺失关键字段的文档安全返回 None"""
        self.assertIsNone(format_meili_doc(None))
        self.assertIsNone(format_meili_doc({}))
        self.assertIsNone(format_meili_doc({"platform": "pdd"}))  # 无 product_id 与 name
        self.assertIsNone(format_meili_doc({"product_id": "123", "name": ""}))  # 空标题

    @patch("infrastructure.search_meili.get_meili_client")
    def test_is_meili_available_caching(self, mock_get_client):
        """测试健康检测状态缓存，不会对不可用实例产生持续阻塞"""
        mock_client = MagicMock()
        mock_client.health.return_value = {"status": "available"}
        mock_get_client.return_value = mock_client

        # 首次强制检查返回 True
        self.assertTrue(is_meili_available(force_check=True))

        # 客户端异常时
        mock_client.health.side_effect = Exception("Connection refused")
        self.assertFalse(is_meili_available(force_check=True))

    def test_search_products_empty_keyword(self):
        """空关键词直接返回 None，不执行检索"""
        self.assertIsNone(search_products(""))
        self.assertIsNone(search_products("   "))

    @patch("infrastructure.search_meili.is_meili_available", return_value=False)
    def test_search_products_fallback_when_unavailable(self, mock_avail):
        """当 Meilisearch 处于不可用状态时，优雅返回 None 促使上游回退数据库"""
        self.assertIsNone(search_products("康师傅"))

    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_search_products_query_building(self, mock_get_client, mock_avail):
        """测试 search_products 组装 filter 与 sort 参数"""
        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_index.search.return_value = {"hits": [{"platform": "pdd", "product_id": "101"}]}
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        hits = search_products(
            keyword="可乐",
            platform="pdd",
            category="饮料",
            sort_by="price_asc",
            limit=10,
            offset=0,
        )
        self.assertEqual(len(hits), 1)
        mock_index.search.assert_called_once()
        args, kwargs = mock_index.search.call_args
        self.assertEqual(args[0], "可乐")
        params = args[1]
        self.assertIn('platform = "pdd"', params["filter"])
        self.assertIn('category = "饮料"', params["filter"])
        self.assertIn("NOT is_off_promotion = true", params["filter"])
        self.assertEqual(params["sort"], ["activity_price:asc"])
        self.assertEqual(params["limit"], 10)


class TestProductRepoMeiliIntegration(unittest.TestCase):

    def setUp(self):
        self.col = MagicMock()
        self.col.__class__.__name__ = "Collection"  # 模拟持久化集合类型
        self.db = SimpleNamespace(get_collection=lambda name: self.col)
        self.repo = ProductRepository(db_instance=self.db)
        self.repo._force_meili_enabled = True

    @patch("repositories.product_repo.search_products")
    def test_query_boom_products_uses_meili_hits(self, mock_search):
        """测试 query_boom_products 优先采用 Meilisearch 命中并保持相关性顺序"""
        mock_search.return_value = [
            {"platform": "pdd", "product_id": "P1"},
            {"platform": "pdd", "product_id": "P2"},
        ]
        self.col.find.return_value = [
            {"platform": "pdd", "product_id": "P2", "name": "可口可乐 P2", "price": 20.0},
            {"platform": "pdd", "product_id": "P1", "name": "可口可乐 P1", "price": 10.0},
        ]

        res = self.repo.query_boom_products(platform="pdd", keyword="可乐", sort_by="latest")
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["data"]), 2)
        # 结果必须按照 Meilisearch hits 的相关性顺序 [P1, P2] 排布，而非 MongoDB 游标乱序
        self.assertEqual([d["product_id"] for d in res["data"]], ["P1", "P2"])

    @patch("repositories.product_repo.search_products")
    def test_query_boom_products_falls_back_when_meili_returns_none(self, mock_search):
        """当 Meilisearch 返回 None (不可用) 时，优雅回退到 Mongo 游标查询"""
        mock_search.return_value = None
        # Mongo cursor mock
        cursor_mock = MagicMock()
        cursor_mock.sort.return_value = cursor_mock
        cursor_mock.skip.return_value = cursor_mock
        cursor_mock.limit.return_value = cursor_mock
        cursor_mock.max_time_ms.return_value = [
            {"platform": "pdd", "product_id": "FALLBACK_1", "name": "降级商品", "price": 15.0}
        ]
        self.col.find.return_value = cursor_mock

        res = self.repo.query_boom_products(platform="pdd", keyword="可乐", sort_by="latest")
        self.assertEqual(res["status"], "success")
        self.assertEqual([d["product_id"] for d in res["data"]], ["FALLBACK_1"])

    @patch("repositories.product_repo.search_products")
    def test_rank_boom_products_by_cost_performance_empty_hits(self, mock_search):
        """当 Meilisearch 返回 0 命中时，坚守防误杀准则，返回空列表，不模糊兜底错乱商品"""
        mock_search.return_value = []
        res = self.repo.rank_boom_products_by_cost_performance(keyword="完全不存在的商品999")
        self.assertEqual(res, [])

    def test_format_meili_doc_with_deal_info_and_entities(self):
        """测试 format_meili_doc 自动将原始爆料标题与 AI 提取实体打包入 search_keywords"""
        doc = {
            "platform": "pdd",
            "product_id": "P6635",
            "name": "可口可乐多口味装包邮",
            "price": 33.4,
            "deal_info": {
                "is_deal_tip": True,
                "raw_tip_title": "【多多】可口可乐500ml*18瓶混合装",
                "format_info": {
                    "core_entities": [{"name": "碳酸饮料", "score": 10}],
                    "decision_keywords": [{"attribute_name": "品类", "attribute_value": "可乐"}],
                },
            },
        }
        res = format_meili_doc(doc)
        self.assertIsNotNone(res)
        self.assertIn("search_keywords", res)
        # 验证 search_keywords 中包含原始爆料标题与实体词
        self.assertIn("【多多】可口可乐500ml*18瓶混合装", res["search_keywords"])
        self.assertIn("碳酸饮料", res["search_keywords"])
        self.assertIn("可乐", res["search_keywords"])

    @patch("repositories.product_repo.upsert_cleaned_products_to_formal_table")
    def test_repo_upsert_cleaned_deal_products_ssot(self, mock_upsert_deal):
        """测试 ProductRepository 作为单一真源入口统一调度好价商品入库"""
        mock_upsert_deal.return_value = {"upserted": 1, "modified": 0, "matched": 0}
        product_docs = [{"platform": "pdd", "product_id": "P999", "name": "测试商品"}]
        res = self.repo.upsert_cleaned_deal_products(product_docs)
        mock_upsert_deal.assert_called_once_with(self.col, product_docs)
        self.assertEqual(res["upserted"], 1)


if __name__ == "__main__":
    unittest.main()
