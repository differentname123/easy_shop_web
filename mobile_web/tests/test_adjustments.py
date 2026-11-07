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
            res = self.client.get("/api/home/recommend?channel_type=5&offset=240&limit=20")
            self.assertEqual(res.status_code, 200)
            mock_get.assert_called_once_with(channel_type=5, offset=240, limit=20, cursor=None, uid=None, platform="pdd")

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

    def test_unified_channel_names_across_pdd_and_taobao(self):
        """验证拼多多和淘宝两个适配器的榜单名称已统一为实时销量榜与今日销量榜"""
        from adapters.pdd_adapter import PddAdapter
        from adapters.tb_adapter import TaobaoAdapter

        pdd_channels = {ch["channel_type"]: ch["name"] for ch in PddAdapter.channels}
        tb_channels = {ch["channel_type"]: ch["name"] for ch in TaobaoAdapter.channels}

        self.assertEqual(pdd_channels.get(5), "实时销量榜")
        self.assertEqual(pdd_channels.get(1), "今日销量榜")
        self.assertEqual(tb_channels.get(5), "实时销量榜")
        self.assertEqual(tb_channels.get(1), "今日销量榜")
        self.assertEqual(pdd_channels, tb_channels)

    def test_detect_platform_from_text(self):
        """验证 PromoteService.detect_platform_from_text 能够准确识别淘宝/京东/拼多多链接与口令"""
        from services.promote_service import PromoteService

        self.assertEqual(PromoteService.detect_platform_from_text("67￥ HU293 2026abcd ￥ https://m.tb.cn/h.xxx 抽纸"), "taobao")
        self.assertEqual(PromoteService.detect_platform_from_text("https://uland.taobao.com/item/edetail?id=abc"), "taobao")
        self.assertEqual(PromoteService.detect_platform_from_text("3！CEDABDWqj8boqBMC！ CZ154"), "jd")
        self.assertEqual(PromoteService.detect_platform_from_text("抢购好物 https://u.jd.com/egUFTPy"), "jd")
        self.assertEqual(PromoteService.detect_platform_from_text("https://jingfen.jd.com/detail/2JQgZkZ2nGOIkKfwpvzdIkKfwpvzdQ_3SD7M7MGcugkXyuI4w.html"), "jd")
        self.assertEqual(PromoteService.detect_platform_from_text("https://p.pinduoduo.com/SqikflSS?sc=EFAC"), "pdd")
        self.assertEqual(PromoteService.detect_platform_from_text("https://mobile.yangkeduo.com/duo_coupon_landing.html?goods_sign=E932Ts1bVtZgMvVTwfvBVEJF7LgQI9Rz_JQwP5lJQN3"), "pdd")

    def test_universal_convert_endpoint_and_detail_cache_seeding(self):
        """测试 POST /api/promote/universal 万能转链及反查详情自动写入 DETAIL_CACHE"""
        from unittest.mock import patch
        from cache import DETAIL_CACHE
        from models.product import ProductRef
        from services.product_service import ProductService

        DETAIL_CACHE.clear()
        mock_adapter = MagicMock()
        mock_adapter.active = True
        mock_adapter.platform_code = "taobao"
        mock_adapter.platform_name = "淘宝"
        mock_adapter.capabilities = {"detail": True, "promotion": True}
        mock_adapter.requires_authorized_promotion = True
        mock_adapter.requires_numeric_promoter_id = False
        mock_adapter.resolve_product_ref.side_effect = (
            lambda gid_or_sign="", gid=None, gsign=None: ProductRef("taobao", gid or gid_or_sign, gsign or gid_or_sign)
        )
        mock_adapter.build_command_text.return_value = "【淘宝精选】好丽友派\n👉领券链接：https://s.click.taobao.com/abc"
        mock_adapter.resolve_and_convert_universal.return_value = {
            "status": "success",
            "url": "https://uland.taobao.com/coupon/edetail?e=xxx",
            "final_link": "https://s.click.taobao.com/abc",
            "short_url": "https://s.click.taobao.com/abc",
            "promotion_token": "￥JBnB61RqEqX￥",
            "token_type": "tpwd",
            "attribution_status": "anonymous",
            "need_auth": False,
            "is_fallback": False,
            "goods_id": "TB_ENC_ID_999",
            "goods_sign": "TB_ENC_ID_999",
            "title": "好丽友巧克力派",
            "price": 8.5,
            "coupon_amount": 3.0,
            "product": {
                "platform": "taobao",
                "platform_name": "淘宝",
                "goods_id": "TB_ENC_ID_999",
                "goods_sign": "TB_ENC_ID_999",
                "name": "好丽友巧克力派",
                "image_url": "https://img.alicdn.com/1.jpg",
                "carousel_images": ["https://img.alicdn.com/1.jpg"],
                "price": 8.5,
                "original_price": 11.5,
                "coupon_amount": 3.0,
                "has_coupon": True,
                "commission_rate": 10.0,
                "commission_amount": 0.85,
                "sales_tip": "1000+",
                "mall_name": "好丽友旗舰店",
                "product_url": "https://uland.taobao.com/item/edetail?id=TB_ENC_ID_999",
            },
        }

        with patch("services.promote_service.get_adapter", return_value=mock_adapter), \
             patch("services.product_service.get_adapter", return_value=mock_adapter):
            resp = self.client.post(
                "/api/promote/universal",
                json={"text": "67￥JBnB61RqEqX￥ https://m.tb.cn/h.xxx 好丽友巧克力派", "platform": "auto"},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body.get("status"), "success")
            self.assertEqual(body.get("platform"), "taobao")
            self.assertEqual(body.get("promotion_token"), "￥JBnB61RqEqX￥")
            self.assertIsNotNone(body.get("product"))
            self.assertEqual(body["product"].get("goods_id"), "TB_ENC_ID_999")
            self.assertIn("￥JBnB61RqEqX￥", body.get("command_text", ""))

            # 验证反查出的商品详情已自动种入 DETAIL_CACHE，后续访问详情页可直接秒开
            svc = ProductService(product_repo=MagicMock())
            cached_detail = svc.get_goods_detail(platform="taobao", goods_id="TB_ENC_ID_999", goods_sign="TB_ENC_ID_999")
            self.assertEqual(cached_detail.get("status"), "success")
            self.assertEqual(cached_detail.get("data", {}).get("name"), "好丽友巧克力派")


if __name__ == "__main__":
    unittest.main()

