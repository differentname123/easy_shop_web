# -*- coding: utf-8 -*-
"""
自动化测试：验证历史数据库兼容代码已彻底取消，且规范化输出正确
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from repositories.product_repo import ProductRepository
from repositories.user_repo import UserRepository, hash_password


class TestProductRepoAdjustments(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_col = MagicMock()
        self.mock_db.get_collection.return_value = self.mock_col
        self.repo = ProductRepository(db_instance=self.mock_db)
        self.repo._db = self.mock_db

    def test_normalize_doc_summary_pure(self):
        """验证商品摘要直接以 commission_amount 计算，不依赖历史字段"""
        doc = {
            "platform": "pdd",
            "product_id": "123456",
            "name": "测试商品",
            "activity_price": 50.0,
            "original_price": 60.0,
            "saved_price": 10.0,
            "commission_amount": 5.0,
            "sales_tip": "热销1000件",
            "image_url": "https://img.example.com/1.jpg"
        }
        res = self.repo._normalize_doc_summary(doc)
        self.assertEqual(res["commission_amount"], 5.0)
        self.assertEqual(res["commission_rate"], 10.0) # 5 / 50 * 100
        self.assertEqual(res["product_id"], "123456")
        self.assertEqual(res["price"], 50.0)

    def test_normalize_doc_no_promotion_url(self):
        """验证商品详情对象中无历史 promotion_url 冗余属性"""
        doc = {
            "platform": "pdd",
            "product_id": "123456",
            "name": "详情测试商品",
            "activity_price": 100.0,
            "original_price": 120.0,
            "carousel_images": ["https://img.example.com/c1.jpg", "https://img.example.com/c2.jpg"],
            "image_url": "https://img.example.com/main.jpg",
            "commission_amount": 10.0,
            "product_url": "https://mobile.pinduoduo.com/goods.html?goods_id=123456"
        }
        res = self.repo._normalize_doc(doc)
        self.assertNotIn("promotion_url", res)
        self.assertEqual(res["carousel_images"], ["https://img.example.com/c1.jpg", "https://img.example.com/c2.jpg"])
        self.assertEqual(res["commission_amount"], 10.0)

    def test_get_promotion_link_cache_rejects_legacy_promotion_url(self):
        """验证 get_promotion_link_cache 不再兼容底层的 promotion_url 伪缓存"""
        # 模拟库内仅存在历史 promotion_url，但没有 default_promotion
        self.mock_col.find_one.return_value = {
            "product_id": "88888",
            "promotion_url": "https://p.pinduoduo.com/old_link_123",
            "name": "历史商品"
        }
        cache = self.repo.get_promotion_link_cache(platform="pdd", goods_id="88888")
        # 绝不兼容：应返回 None 以便重新生链
        self.assertIsNone(cache)

    def test_get_promotion_link_cache_accepts_valid_default_promotion(self):
        """验证规范生成的 default_promotion 能够正常命中缓存"""
        self.mock_col.find_one.return_value = {
            "product_id": "88888",
            "default_promotion": {
                "status": "success",
                "final_link": "https://p.pinduoduo.com/valid_link",
                "command_text": "口令文本"
            },
            "default_promotion_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        cache = self.repo.get_promotion_link_cache(platform="pdd", goods_id="88888")
        self.assertIsNotNone(cache)
        self.assertEqual(cache["final_link"], "https://p.pinduoduo.com/valid_link")
        self.assertTrue(cache["from_cache"])

    def test_get_product_detail_condition_is_pure_string(self):
        """验证 get_product_detail 仅以字符串查找，无 int(key) 历史分支"""
        self.mock_col.find_one.return_value = None
        self.repo.get_product_detail("12345")
        call_args = self.mock_col.find_one.call_args[0][0]
        self.assertEqual(call_args["$and"][0], {"platform": "pdd", "$or": [{"product_id": "12345"}, {"goods_sign": "12345"}]})
        self.assertEqual(call_args["$and"][1], self.repo._displayable_content_filter())


class TestUserRepoAdjustments(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_col = MagicMock()
        self.mock_db.get_collection.return_value = self.mock_col
        self.repo = UserRepository(db_instance=self.mock_db)
        self.repo._db = self.mock_db

    def test_existing_user_wrong_password_raises(self):
        """已有用户密码错误直接抛错"""
        self.mock_col.find_one.return_value = {
            "phone": "13800138000",
            "password_hash": hash_password("correct_password")
        }
        with self.assertRaises(ValueError) as ctx:
            self.repo.register_or_login(phone="13800138000", password="wrong_password")
        self.assertIn("输入的密码不正确", str(ctx.exception))


class TestProductServiceCache(unittest.TestCase):
    def test_search_cache_and_category_cache(self):
        from unittest.mock import patch
        from services.product_service import ProductService, _SEARCH_CACHE, _CATEGORY_CACHE

        mock_repo = MagicMock()
        mock_repo.get_categories.return_value = ["食品", "数码"]
        service = ProductService(product_repo=mock_repo)

        # 1. 验证类目缓存命中
        _CATEGORY_CACHE.clear()
        cats1 = service.get_boom_categories(platform="pdd")
        self.assertEqual(cats1, ["食品", "数码"])
        self.assertEqual(mock_repo.get_categories.call_count, 1)

        # 第二次调用命中缓存，不应再次查询 repo
        cats2 = service.get_boom_categories(platform="pdd")
        self.assertEqual(cats2, ["食品", "数码"])
        self.assertEqual(mock_repo.get_categories.call_count, 1)

        # 2. 验证搜索缓存命中
        _SEARCH_CACHE.clear()
        with patch("services.product_service.get_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            mock_adapter.search_goods.return_value = {
                "status": "success",
                "data": [{"goods_id": "999", "name": "缓存纸巾"}]
            }
            mock_get_adapter.return_value = mock_adapter

            res1 = service.search_home_goods(keyword="纸巾", page=1)
            self.assertEqual(res1["status"], "success")
            self.assertEqual(mock_adapter.search_goods.call_count, 1)

            # 第二次相同关键词搜索应命中内存缓存
            res2 = service.search_home_goods(keyword="纸巾", page=1)
            self.assertEqual(res2["status"], "success")
            self.assertEqual(mock_adapter.search_goods.call_count, 1)


class TestOffsetAndDetailEnhancements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from main import app
        cls.client = TestClient(app)

    def test_home_recommend_large_offset_not_422(self):
        """测试 offset=240 等深分页不再触发 422 校验失败"""
        from unittest.mock import patch
        with patch("services.product_service.ProductService.get_home_recommendations") as mock_get:
            mock_get.return_value = {
                "status": "success",
                "data": [{"goods_id": "111", "name": "深页商品"}],
                "total": 300,
                "has_more": True
            }
            res = self.client.get("/api/home/recommend?channel_type=3&offset=240&limit=20")
            self.assertEqual(res.status_code, 200)
            mock_get.assert_called_once_with(channel_type=3, offset=240, limit=20, uid=None, platform="pdd")

    def test_detail_dual_key_caching(self):
        """测试 get_goods_detail 双键缓存 (goods_id 与 goods_sign)"""
        from unittest.mock import patch
        from services.product_service import ProductService, DETAIL_CACHE

        mock_repo = MagicMock()
        mock_repo.get_product_detail.return_value = None
        service = ProductService(product_repo=mock_repo)

        DETAIL_CACHE.clear()
        with patch("services.product_service.get_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            from adapters.pdd_adapter import PddAdapter
            mock_adapter.resolve_product_ref.side_effect = PddAdapter().resolve_product_ref
            mock_adapter.get_goods_detail.return_value = {
                "status": "success",
                "data": {
                    "goods_id": "888123",
                    "goods_sign": "SIGN_XYZ_999",
                    "name": "多图商品",
                    "carousel_images": ["https://img.com/1.jpg", "https://img.com/2.jpg"],
                    "price": 29.9
                }
            }
            mock_get_adapter.return_value = mock_adapter

            # 首次通过 goods_sign 查询
            res1 = service.get_goods_detail(platform="pdd", goods_sign="SIGN_XYZ_999", goods_id="888123")
            self.assertIsNotNone(res1)
            self.assertEqual(res1.get("status"), "success")
            self.assertEqual(res1.get("data", {}).get("goods_id"), "888123")
            self.assertEqual(mock_adapter.get_goods_detail.call_count, 1)

            # 再次通过 goods_id 查询，应直接命中双键缓存，不调用 adapter
            res2 = service.get_goods_detail(platform="pdd", goods_id="888123")
            self.assertIsNotNone(res2)
            self.assertEqual(res2.get("data", {}).get("goods_id"), "888123")
            self.assertEqual(mock_adapter.get_goods_detail.call_count, 1)

            # 再次通过 goods_sign 查询，应直接命中双键缓存，不调用 adapter
            res3 = service.get_goods_detail(platform="pdd", goods_sign="SIGN_XYZ_999")
            self.assertIsNotNone(res3)
            self.assertEqual(res3.get("data", {}).get("goods_id"), "888123")
            self.assertEqual(mock_adapter.get_goods_detail.call_count, 1)

    def test_detail_page_route_renders_html(self):
        """测试 /detail 页面正常返回包含双键参数的 html"""
        res = self.client.get("/detail?goods_id=888123&goods_sign=SIGN_XYZ_999&platform=pdd")
        self.assertEqual(res.status_code, 200)
        self.assertIn("<!DOCTYPE html>", res.text)
        self.assertIn("商品详情", res.text)


if __name__ == "__main__":
    unittest.main()
