# -*- coding: utf-8 -*-
"""
全面自动化测试套件：验证安全加固、性能优化与架构重构成果
涵盖：
1. 认证鉴权与密码安全 (HMAC-SHA256 Token, PBKDF2, 向上平滑兼容, 手机号脱敏, get_current_uid 依赖)
2. 线程安全缓存 (ThreadSafeTTLCache, 并发安全, TTL 过期, LRU 淘汰)
3. 多平台适配器 (PDD/TB/JD 接口契约, 增量订单拉取, 兜底转链标记, 去除假数据)
4. 仓储层安全 (正则转义防护 ReDoS, 游标探测分页, 隔绝大模型开销标记)
5. 用户服务与授权备案 (旧密码登录无缝升级 PBKDF2, 避免重复调用官方备案 API)
6. 订单服务并发与性能 (非阻塞互斥锁, 本地快速响应, 多平台聚合)
7. API 参数边界防御与接口鉴权 (Pydantic / Query 边界校验, 401/422 状态码)
"""
import asyncio
import hashlib
import re
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import (
    create_access_token,
    verify_access_token,
    hash_password,
    verify_password,
    mask_phone,
    get_current_uid,
    LEGACY_SALT,
)
from cache_utils import ThreadSafeTTLCache
from adapters import get_adapter
from adapters.pdd_adapter import PddAdapter
from adapters.tb_adapter import TaobaoAdapter
from adapters.jd_adapter import JdAdapter
from repositories.product_repo import ProductRepository
from repositories.user_repo import UserRepository
from services.user_service import UserService
from services.order_service import OrderService, _SYNC_LOCK


# ==================== 1. 认证鉴权与密码安全测试 ====================
class TestAuthSecurity(unittest.TestCase):
    def test_create_and_verify_access_token_success(self):
        """测试正常生成与校验 Token"""
        uid = "test_user_12345"
        token = create_access_token(uid, ttl=3600)
        self.assertIsInstance(token, str)
        self.assertIn(".", token)

        parsed_uid = verify_access_token(token)
        self.assertEqual(parsed_uid, uid)

    def test_verify_tampered_token_fails(self):
        """测试篡改 Token 签名或 Payload 必定失败"""
        token = create_access_token("alice", ttl=3600)
        # 篡改最后一位字符
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        self.assertIsNone(verify_access_token(tampered))

        # 伪造空/错误格式
        self.assertIsNone(verify_access_token("invalid_token_format"))
        self.assertIsNone(verify_access_token(""))
        self.assertIsNone(verify_access_token(None))

    def test_verify_expired_token_fails(self):
        """测试已过期的 Token 必定被拒绝"""
        token = create_access_token("bob", ttl=-10)
        self.assertIsNone(verify_access_token(token))

    def test_password_hashing_pbkdf2(self):
        """测试 PBKDF2 密码哈希生成与校验"""
        raw_pwd = "MySecurePassword#2026"
        pwd_hash = hash_password(raw_pwd)
        self.assertTrue(pwd_hash.startswith("pbkdf2_sha256$"))

        # 正确密码校验通过
        self.assertTrue(verify_password(raw_pwd, pwd_hash))
        # 错误密码校验失败
        self.assertFalse(verify_password("WrongPassword", pwd_hash))

    def test_legacy_password_compatibility(self):
        """测试向上平滑兼容历史 sha256 盐值哈希"""
        pwd = "mypassword"
        legacy_hash = hashlib.sha256(f"{LEGACY_SALT}_{pwd}_{LEGACY_SALT}".encode("utf-8")).hexdigest()

        # 能够正确验证历史旧哈希
        self.assertTrue(verify_password(pwd, legacy_hash))
        self.assertFalse(verify_password("wrongpassword", legacy_hash))

    def test_mask_phone(self):
        """测试手机号脱敏"""
        self.assertEqual(mask_phone("13812345678"), "138****5678")
        self.assertEqual(mask_phone("19988889999"), "199****9999")
        # 异常/短号码兜底安全
        self.assertEqual(mask_phone("123"), "123")
        self.assertEqual(mask_phone(""), "")
        self.assertEqual(mask_phone(None), "")

    def test_get_current_uid_dependency(self):
        """测试 FastAPI get_current_uid 异步依赖提取"""
        token = create_access_token("uid_007", ttl=60)

        # 1. Bearer header
        self.assertEqual(asyncio.run(get_current_uid(authorization=f"Bearer {token}")), "uid_007")
        # 2. Raw header
        self.assertEqual(asyncio.run(get_current_uid(authorization=token)), "uid_007")
        # 3. Query param token
        self.assertEqual(asyncio.run(get_current_uid(token=token)), "uid_007")

        # 4. 缺少凭据抛出 401
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_current_uid(authorization=None, token=None))
        self.assertEqual(ctx.exception.status_code, 401)

        # 5. 无效凭据抛出 401
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_current_uid(authorization="Bearer bad_token"))
        self.assertEqual(ctx.exception.status_code, 401)


# ==================== 2. 线程安全缓存测试 ====================
class TestThreadSafeTTLCache(unittest.TestCase):
    def test_basic_and_ttl(self):
        """测试缓存基本存取与 TTL 过期"""
        cache = ThreadSafeTTLCache(maxsize=10, ttl=0.1)
        cache.set("key1", "val1")
        self.assertEqual(cache.get("key1"), "val1")
        self.assertIn("key1", cache)

        time.sleep(0.15)
        # 过期后返回 None
        self.assertIsNone(cache.get("key1"))
        self.assertNotIn("key1", cache)

    def test_maxsize_eviction(self):
        """测试 LRU 超容淘汰与 __len__"""
        cache = ThreadSafeTTLCache(maxsize=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("d", 4)  # 超出容量，最老数据淘汰
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("d"), 4)

    def test_thread_safety(self):
        """测试高并发读写线程安全"""
        cache = ThreadSafeTTLCache(maxsize=100, ttl=10)
        errors = []

        def worker(w_id):
            try:
                for i in range(50):
                    cache.set(f"k_{w_id}_{i}", i)
                    v = cache.get(f"k_{w_id}_{i}")
                    if v != i and v is not None:
                        errors.append("Mismatch")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=8) as executor:
            for wid in range(8):
                executor.submit(worker, wid)

        self.assertEqual(errors, [])


# ==================== 3. 多平台适配器契约测试 ====================
class TestMultiPlatformAdapters(unittest.TestCase):
    def test_adapter_factory(self):
        """测试适配器工厂方法"""
        self.assertIsInstance(get_adapter("pdd"), PddAdapter)
        self.assertIsInstance(get_adapter("tb"), TaobaoAdapter)
        self.assertIsInstance(get_adapter("jd"), JdAdapter)

    def test_tb_jd_adapters_contract(self):
        """测试淘宝与京东适配器契约：返回标准错误/空列表，绝不产生假 mock 数据"""
        tb = get_adapter("tb")
        detail = tb.get_goods_detail("123")
        self.assertEqual(detail["status"], "error")
        orders = tb.fetch_increment_orders(0, 100)
        self.assertEqual(orders.get("order_list"), [])

        jd = get_adapter("jd")
        jd_detail = jd.get_goods_detail("456")
        self.assertEqual(jd_detail["status"], "error")
        jd_orders = jd.fetch_increment_orders(0, 100)
        self.assertEqual(jd_orders.get("order_list"), [])

    def test_pdd_adapter_fallback_marking(self):
        """测试当拼多多上游生链失败时，兜底链接显式打上 is_fallback 标记且不重试"""
        adapter = PddAdapter()
        with patch("adapters.pdd_adapter.generate_smart_promotion_url", side_effect=Exception("API Limit")), \
             patch("adapters.pdd_adapter.convert_pdd_url", return_value={"status": "error", "error_msg": "API Limit"}), \
             patch("adapters.pdd_adapter.generate_goods_promotion_url", return_value={"status": "error"}):
            res = adapter.convert_promotion_url(
                goods_id_or_sign="111",
                uid="uid_test"
            )
            self.assertEqual(res["status"], "success")
            self.assertTrue(res.get("is_fallback"))
            self.assertEqual(res["final_link"], "https://mobile.pinduoduo.com/goods.html?goods_id=111")


# ==================== 4. 仓储层安全加固测试 ====================
class TestProductRepoSecurity(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_col = MagicMock()
        self.mock_db.get_collection.return_value = self.mock_col
        self.repo = ProductRepository(db_instance=self.mock_db)
        self.repo._db = self.mock_db

    def test_query_boom_products_regex_escaped(self):
        """测试搜索关键词被自动转义 (re.escape)，防御正则注入与回溯攻击"""
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_cursor.max_time_ms.return_value = []
        self.mock_col.find.return_value = mock_cursor

        # 输入包含正则特殊字符的恶意关键词
        nasty_input = ".*+?^${}()|[]\\"
        self.repo.query_boom_products(keyword=nasty_input, page=1, page_size=20)

        # 检查 find 参数中的正则表达式是否经过 re.escape
        call_args = self.mock_col.find.call_args[0][0]
        escaped_pattern = call_args["$and"][0]["name"]["$regex"]
        self.assertEqual(escaped_pattern, re.escape(nasty_input.strip()[:30]))

    def test_query_boom_products_cursor_probe_pagination(self):
        """测试游标探测分页 (limit=page_size+1)，不进行慢查询 count_documents"""
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_cursor.max_time_ms.return_value = [
            {"product_id": f"p_{i}", "price": 10.0, "commission_amount": 1.0}
            for i in range(21)  # 返回 21 条（page_size + 1）
        ]
        self.mock_col.find.return_value = mock_cursor

        result = self.repo.query_boom_products(keyword="手机", page=1, page_size=20)

        # 验证 limit 调用了 21 (page_size + 1)
        mock_cursor.limit.assert_called_with(21)
        # 验证没有调用 count_documents
        self.mock_col.count_documents.assert_not_called()
        # 验证 has_more 为 True，返回结果裁剪为 20 条
        self.assertTrue(result["has_more"])
        self.assertEqual(len(result["data"]), 20)

    def test_upsert_isolates_commercial_llm_cost(self):
        """测试写入商品时设置 need_reformat: False 和 format_status: success，隔绝大模型调用费用"""
        self.mock_col.bulk_write.return_value = MagicMock()
        products = [{
            "goods_id": "1001",
            "name": "测试商品",
            "price": 10.0,
            "original_price": 20.0,
        }]
        self.repo.upsert_home_products(products, channel_type=1)

        # 提取 bulk_write 的 UpdateOne 操作
        operations = self.mock_col.bulk_write.call_args[0][0]
        update_doc = operations[0]._doc["$set"]
        self.assertFalse(update_doc["need_reformat"])
        self.assertEqual(update_doc["format_status"], "success")
        self.assertEqual(update_doc["format_retry_count"], 999)


# ==================== 5. 用户服务与密码平滑升级测试 ====================
class TestUserServiceSecurity(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_users_col = MagicMock()
        self.mock_db.get_collection.return_value = self.mock_users_col
        self.user_repo = UserRepository(db_instance=self.mock_db)
        self.user_repo._db = self.mock_db
        self.user_service = UserService(user_repo=self.user_repo)

    def test_legacy_password_auto_upgrade_on_login(self):
        """测试使用旧 sha256 密码登录成功后，数据库自动无感升级为 PBKDF2 哈希"""
        pwd = "OldPassword123"
        legacy_hash = hashlib.sha256(f"{LEGACY_SALT}_{pwd}_{LEGACY_SALT}".encode("utf-8")).hexdigest()

        user_doc = {
            "uid": "u_999",
            "phone": "13800138000",
            "password_hash": legacy_hash,
            "balance": 0.0,
            "auth_status": {}
        }
        self.mock_users_col.find_one.return_value = user_doc

        # 用户登录
        res = self.user_service.register_or_login("13800138000", pwd)
        self.assertEqual(res["status"], "success")
        self.assertIn("auth_token", res["user"])

        # 验证 update_one 被调用，更新密码为 PBKDF2
        self.mock_users_col.update_one.assert_called_once()
        update_args = self.mock_users_col.update_one.call_args[0]
        new_hash = update_args[1]["$set"]["password_hash"]
        self.assertTrue(new_hash.startswith("pbkdf2_sha256$"))

    def test_check_and_get_auth_skips_external_api_if_authorized(self):
        """测试用户已备案时直接返回，绝不重复调用外部电商平台接口"""
        self.mock_users_col.find_one.return_value = {
            "uid": "u_888",
            "phone": "13900139000",
            "auth_status": {
                "pdd": {"is_authorized": True, "updated_at": time.time()}
            }
        }

        with patch("services.user_service.get_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            mock_adapter.platform_name = "拼多多"
            mock_get_adapter.return_value = mock_adapter

            res = self.user_service.check_and_get_auth(uid="u_888", platform="pdd")
            self.assertEqual(res["status"], "success")
            self.assertTrue(res["is_authorized"])
            # 外部备案查询接口未被调用
            mock_adapter.check_authority.assert_not_called()


# ==================== 6. 订单服务并发非阻塞锁测试 ====================
class TestOrderServiceConcurrency(unittest.TestCase):
    def test_non_blocking_sync_lock(self):
        """测试多用户并发调用订单同步时，非阻塞互斥锁确保只有一个线程执行拉取，其他线程秒级返回"""
        mock_order_repo = MagicMock()
        mock_order_repo.query_user_orders.return_value = {
            "order_list": [],
            "total_count": 0,
            "total_commission": 0.0,
            "settled_commission": 0.0,
            "refund_commission": 0.0,
            "refund_count": 0
        }
        service = OrderService(order_repo=mock_order_repo)

        # 人工占用同步锁，模拟一个耗时很长的后台同步正在运行
        acquired = _SYNC_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)

        try:
            with patch("services.order_service.get_adapter") as mock_get_adapter:
                # 另一个用户发起强制同步
                start_t = time.time()
                res = service.sync_and_query_orders(uid="user_a", days=7, force_sync=True)
                elapsed = time.time() - start_t

                # 验证：绝不阻塞等待锁，0.1 秒内直接完成并返回本地数据
                self.assertLess(elapsed, 0.2)
                self.assertEqual(res["status"], "success")
                # 外部适配器未被调用（跳过了排队执行）
                mock_get_adapter.assert_not_called()
        finally:
            # 释放锁
            _SYNC_LOCK.release()


# ==================== 7. API 参数边界防御与鉴权拦截测试 ====================
class TestApiSecurityRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from main import app
        cls.client = TestClient(app)

    def test_goods_list_validation_bounds(self):
        """测试商品列表参数越界拦截"""
        # 超大 page_size ( > 100)
        res = self.client.get("/api/goods/list?page_size=200")
        self.assertEqual(res.status_code, 422)

        # 负数 page ( < 1)
        res = self.client.get("/api/goods/list?page=0")
        self.assertEqual(res.status_code, 422)

        # 超长 keyword ( > 50)
        long_kw = "a" * 51
        res = self.client.get(f"/api/goods/list?keyword={long_kw}")
        self.assertEqual(res.status_code, 422)

    def test_home_recommend_validation_bounds(self):
        """测试首页推荐参数越界拦截"""
        # 超大 limit ( > 100)
        res = self.client.get("/api/home/recommend?limit=150")
        self.assertEqual(res.status_code, 422)

        # 非法 channel_type ( < 0)
        res = self.client.get("/api/home/recommend?channel_type=-1")
        self.assertEqual(res.status_code, 422)

    def test_protected_routes_require_valid_token(self):
        """测试受保护接口无 Token 或假 Token 必须返回 401"""
        # 1. /api/order/sync_and_query
        res1 = self.client.post("/api/order/sync_and_query")
        self.assertEqual(res1.status_code, 401)

        res1_fake = self.client.post("/api/order/sync_and_query", headers={"Authorization": "Bearer fake_token"})
        self.assertEqual(res1_fake.status_code, 401)

        # 2. /api/user/info
        res2 = self.client.get("/api/user/info")
        self.assertEqual(res2.status_code, 401)

        # 3. /api/user/auth
        res3 = self.client.get("/api/user/auth")
        self.assertEqual(res3.status_code, 401)

    def test_promote_convert_validation(self):
        """测试转链接口参数边界防御"""
        # 超长字段请求
        huge_payload = {
            "platform": "pdd",
            "goods_id": "x" * 150,  # > 128
            "title": "title",
            "price": 10.0
        }
        res = self.client.post("/api/promote/convert", json=huge_payload)
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main()
