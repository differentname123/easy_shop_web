# -*- coding: utf-8 -*-
"""
新增架构重构单元测试套件：
验证 models 领域模型、cache 分域治理、adapters 动态注册及 BaseRepository 契约规范。
"""
import unittest
from unittest.mock import MagicMock
from models.product import ProductSummary, ProductDetail
from models.order import OrderItem, UserOrderStats
from models.user import UserProfile, RegisterOrLoginDTO
from cache import (
    RECOMMEND_CACHE,
    DETAIL_CACHE,
    SEARCH_CACHE,
    CATEGORY_CACHE,
    PROMOTION_CACHE,
    clear_all_caches
)
from adapters import get_adapter, register_adapter, get_all_platforms
from adapters.base import BasePlatformAdapter
from repositories.base_repo import BaseRepository
from services.promote_service import PromoteService


class TestDomainModels(unittest.TestCase):
    def test_product_summary_and_detail(self):
        """测试商品卡片与详情领域模型构造与属性映射"""
        summary = ProductSummary(
            goods_id="10001",
            goods_sign="sign_abc",
            name="测试优质抽纸",
            image_url="https://img.example.com/item.jpg",
            price=19.9,
            original_price=29.9,
            coupon_amount=10.0,
            has_coupon=True,
            commission_rate=15.0,
            commission_amount=2.98
        )
        self.assertEqual(summary.goods_id, "10001")
        self.assertEqual(summary.platform, "pdd")
        self.assertTrue(summary.has_coupon)
        self.assertEqual(summary.price, 19.9)

        dump_func = getattr(summary, "model_dump", None) or summary.dict
        detail = ProductDetail(
            **dump_func(),
            desc="商品详细描述",
            carousel_images=["https://img.example.com/1.jpg", "https://img.example.com/2.jpg"],
            detail_tags=["官方补贴", "退货包运费"]
        )
        self.assertEqual(len(detail.carousel_images), 2)
        self.assertIn("官方补贴", detail.detail_tags)

    def test_order_models(self):
        """测试订单与统计模型"""
        item = OrderItem(
            order_sn="SN_9999",
            goods_id="G_123",
            goods_name="爆款洗衣液",
            promotion_amount_yuan=5.5,
            is_settled=True
        )
        self.assertEqual(item.order_sn, "SN_9999")
        self.assertTrue(item.is_settled)

        stats = UserOrderStats(
            total_count=1,
            total_commission=5.5,
            settled_commission=5.5,
            order_list=[item]
        )
        self.assertEqual(stats.total_commission, 5.5)
        self.assertEqual(len(stats.order_list), 1)

    def test_user_models(self):
        """测试用户模型与 DTO"""
        dto = RegisterOrLoginDTO(
            phone="13800000000",
            password="securePassword123"
        )
        self.assertEqual(dto.phone, "13800000000")

        profile = UserProfile(
            uid="u_test01",
            phone="138****0000",
            nickname="超级推手",
            invite_code="INV_ABC123"
        )
        self.assertEqual(profile.uid, "u_test01")
        self.assertIn("****", profile.phone)


class TestCacheGovernance(unittest.TestCase):
    def test_clear_all_caches(self):
        """测试集中缓存治理清空方法"""
        RECOMMEND_CACHE.set("k1", "v1")
        DETAIL_CACHE.set("k2", "v2")
        SEARCH_CACHE.set("k3", "v3")
        CATEGORY_CACHE.set("k4", "v4")
        PROMOTION_CACHE.set("k5", "v5")

        self.assertEqual(RECOMMEND_CACHE.get("k1"), "v1")
        self.assertEqual(DETAIL_CACHE.get("k2"), "v2")

        clear_all_caches()

        self.assertIsNone(RECOMMEND_CACHE.get("k1"))
        self.assertIsNone(DETAIL_CACHE.get("k2"))
        self.assertIsNone(SEARCH_CACHE.get("k3"))
        self.assertIsNone(CATEGORY_CACHE.get("k4"))
        self.assertIsNone(PROMOTION_CACHE.get("k5"))


class TestAdapterRegistry(unittest.TestCase):
    def test_dynamic_register_and_get(self):
        """测试适配器动态扩展与获取"""
        class MockCustomAdapter(BasePlatformAdapter):
            platform_code = "custom"
            platform_name = "自营平台"
            def get_recommend_goods(self, **kwargs): return {"status": "success", "data": []}
            def search_goods(self, **kwargs): return {"status": "success", "data": []}
            def get_goods_detail(self, **kwargs): return {"status": "success", "data": {}}
            def convert_promotion_url(self, **kwargs): return {"status": "success", "url": ""}
            def check_authority(self, **kwargs): return {"status": "success", "is_authorized": True}
            def generate_authority_url(self, **kwargs): return {"status": "success", "auth_url": ""}
            def fetch_increment_orders(self, **kwargs): return {"status": "success", "order_list": []}

        custom_inst = MockCustomAdapter()
        register_adapter("custom", custom_inst)
        self.assertEqual(get_adapter("custom"), custom_inst)

    def test_get_all_platforms_info(self):
        """测试获取平台元信息列表"""
        platforms = get_all_platforms()
        codes = [p["code"] for p in platforms]
        self.assertIn("pdd", codes)
        self.assertIn("taobao", codes)
        self.assertIn("jd", codes)


class TestPromoteServiceCommandBuilder(unittest.TestCase):
    def test_build_command_text(self):
        """测试统一口令生成逻辑"""
        text = PromoteService.build_command_text(
            title="优质水果",
            final_link="https://p.pinduoduo.com/abc",
            price=9.9,
            coupon=5.0
        )
        self.assertIn("【好物精选】优质水果", text)
        self.assertIn("【券后特惠】¥9.9", text)
        self.assertIn("已立减 ¥5.0", text)
        self.assertIn("https://p.pinduoduo.com/abc", text)


class TestBaseRepositoryContract(unittest.TestCase):
    def test_missing_collection_name_raises(self):
        """验证未定义 COLLECTION_NAME 时访问 col 抛出 NotImplementedError"""
        class IncompleteRepo(BaseRepository):
            pass

        repo = IncompleteRepo(db_instance=MagicMock())
        with self.assertRaises(NotImplementedError):
            _ = repo.col

    def test_seed_resolved_product_seeds_detail_cache_without_repo_io(self):
        """验证万能转链反查商品后写入 DETAIL_CACHE 且不触发冗余无效的仓储调用"""
        from cache import DETAIL_CACHE
        DETAIL_CACHE.clear()
        repo = MagicMock()
        service = PromoteService(product_repo=repo)
        service._seed_resolved_product(
            "pdd",
            {
                "platform": "pdd",
                "goods_id": "10086",
                "goods_sign": "E9_SIGN_10086",
                "name": "维达抽纸整箱",
                "image_url": "https://img.example.com/1.jpg",
                "price": 19.9,
                "original_price": 29.9,
            },
        )
        self.assertGreater(len(DETAIL_CACHE), 0)
        repo.save_product_detail.assert_not_called()

    def test_unified_identity_url_and_ztk_parsers(self):
        """验证跨平台统一抽取的 ZTK 字段解析、京东物料 URL 规范化、商品三元组身份解析与用户索引幂等去重"""
        from infrastructure.http_utils import extract_ztk_item_fields, parse_carousel_images
        from models.product import normalize_jd_material_url, resolve_item_identity
        from repositories.user_repo import UserRepository
        from types import SimpleNamespace

        # 1. 轮播图去重与主图前置
        imgs = parse_carousel_images("//img.example.com/a.jpg|https://img.example.com/b.jpg|//img.example.com/a.jpg", "//img.example.com/main.jpg")
        self.assertEqual(imgs, [
            "https://img.example.com/main.jpg",
            "https://img.example.com/a.jpg",
            "https://img.example.com/b.jpg",
        ])

        # 2. 折淘客通用字段提取
        fields = extract_ztk_item_fields(
            {
                "tao_id": "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd",
                "title": "维达抽纸24包",
                "pict_url": "//img.alicdn.com/1.jpg",
                "small_images": "//img.alicdn.com/2.jpg",
                "size": "39.9",
                "quanhou_jiage": "29.9",
                "coupon_info_money": "10",
                "tkrate3": "10",
                "tkfee3": "0",
            },
            zero_fallback_to_size=True,
            include_primary_in_carousel=True,
        )
        self.assertEqual(fields["price"], 29.9)
        self.assertEqual(fields["original_price"], 39.9)
        self.assertEqual(fields["coupon_amount"], 10.0)
        self.assertEqual(fields["commission_amount"], 2.99)
        self.assertEqual(len(fields["carousel_images"]), 2)

        # 3. 京东加密 itemId 物料链接纠偏
        encrypted_id = "BWQ9383WurxZ8XgYyttv5GNgYyttv5_3rtcLEOl4l98a1A8AG"
        self.assertEqual(
            normalize_jd_material_url(f"https://item.jd.com/{encrypted_id}.html"),
            f"https://jingfen.jd.com/detail/{encrypted_id}.html",
        )
        self.assertEqual(
            normalize_jd_material_url("", "100012043978"),
            "https://item.jd.com/100012043978.html",
        )

        # 4. 统一商品三元组身份解析
        plat, gid, gsign = resolve_item_identity({
            "platform": "tb",
            "goods_sign": "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd",
        })
        self.assertEqual(plat, "taobao")
        self.assertEqual(gid, "0v7DKOgI5JqnZd4Nhxd")
        self.assertEqual(gsign, "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd")

        # 5. UserRepository 渠道唯一索引在同集合实例上幂等去重，避免每次写操作重复执行 DDL
        db = MagicMock()
        col = db.get_collection.return_value
        col.update_one.return_value = SimpleNamespace(modified_count=1, matched_count=1)
        user_repo = UserRepository(db_instance=db)
        col.create_index.reset_mock()
        user_repo.update_auth_status("u_12345678", "taobao", True, extra={"identity": {"relation_id": "rel_1"}})
        user_repo.update_auth_status("u_87654321", "taobao", True, extra={"identity": {"relation_id": "rel_2"}})
        self.assertEqual(col.create_index.call_count, 1)


if __name__ == "__main__":
    unittest.main()

