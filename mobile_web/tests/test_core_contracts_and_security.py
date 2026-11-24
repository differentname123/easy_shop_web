# -*- coding: utf-8 -*-
"""核心基础契约与安全防线测试（领域模型、ZTK通用解析、BaseRepository、Auth/JWT、ThreadSafeTTLCache、API 边界鉴权）。"""
import asyncio
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import (
    create_access_token,
    get_current_uid,
    hash_password,
    mask_phone,
    verify_access_token,
    verify_password,
)
from cache_utils import ThreadSafeTTLCache
from infrastructure.http_utils import extract_ztk_item_fields, parse_carousel_images
from models.order import OrderItem, UserOrderStats
from models.product import ProductDetail, ProductSummary, normalize_jd_material_url, resolve_item_identity
from models.user import RegisterOrLoginDTO, UserProfile
from repositories.base_repo import BaseRepository


class TestDomainModelsAndHelpers(unittest.TestCase):
    def test_product_summary_and_detail_models_and_commercial_invariants(self):
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
            commission_amount=2.98,
        )
        self.assertEqual(summary.goods_id, "10001")
        self.assertEqual(summary.platform, "pdd")
        self.assertTrue(summary.has_coupon)

        inferred_summary = ProductSummary(
            platform="jd",
            goods_id="10002",
            name="京东测试商品",
            image_url="https://img.example.com/1.jpg",
            price=29.9,
            original_price=29.9,
            coupon_amount=10.0,
            has_coupon=False,
        )
        self.assertTrue(inferred_summary.has_coupon)
        self.assertEqual(inferred_summary.original_price, 39.9)

        dump_func = getattr(summary, "model_dump", None) or summary.dict
        detail = ProductDetail(
            **dump_func(),
            desc="商品详细描述",
            carousel_images=["https://img.example.com/1.jpg", "https://img.example.com/2.jpg"],
            detail_tags=["5元", "官方补贴", "5元优惠券", "退货包运费"],
        )
        self.assertEqual(len(detail.carousel_images), 2)
        self.assertEqual(detail.detail_tags, ["5元优惠券", "官方补贴", "退货包运费"])

    def test_order_and_user_models(self):
        item = OrderItem(
            order_sn="SN_9999",
            goods_id="G_123",
            goods_name="爆款洗衣液",
            promotion_amount_yuan=5.5,
            is_settled=True,
        )
        stats = UserOrderStats(total_count=1, total_commission=5.5, settled_commission=5.5, order_list=[item])
        self.assertEqual(stats.total_commission, 5.5)
        self.assertEqual(len(stats.order_list), 1)

        dto = RegisterOrLoginDTO(phone="13800000000", password="securePassword123")
        self.assertEqual(dto.phone, "13800000000")
        profile = UserProfile(uid="u_test01", phone="138****0000", nickname="超级推手", invite_code="INV_ABC123")
        self.assertEqual(profile.uid, "u_test01")
        self.assertIn("****", profile.phone)

    def test_missing_collection_name_raises(self):
        class IncompleteRepo(BaseRepository):
            pass

        repo = IncompleteRepo(db_instance=MagicMock())
        with self.assertRaises(NotImplementedError):
            _ = repo.col

    def test_ztk_carousel_identity_and_encrypted_url_helpers(self):
        imgs = parse_carousel_images(
            "//img.example.com/a.jpg|https://img.example.com/b.jpg|//img.example.com/a.jpg",
            "//img.example.com/main.jpg",
        )
        self.assertEqual(
            imgs,
            ["https://img.example.com/main.jpg", "https://img.example.com/a.jpg", "https://img.example.com/b.jpg"],
        )

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

        encrypted_id = "BWQ9383WurxZ8XgYyttv5GNgYyttv5_3rtcLEOl4l98a1A8AG"
        self.assertEqual(
            normalize_jd_material_url(f"https://item.jd.com/{encrypted_id}.html"),
            f"https://jingfen.jd.com/detail/{encrypted_id}.html",
        )
        self.assertEqual(
            resolve_item_identity({"platform": "tb", "goods_sign": "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd"}),
            ("taobao", "0v7DKOgI5JqnZd4Nhxd", "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd"),
        )


class TestAuthSecurity(unittest.TestCase):
    def test_verify_tampered_and_expired_tokens_fail(self):
        token = create_access_token("alice", ttl=3600)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        self.assertIsNone(verify_access_token(tampered))
        self.assertIsNone(verify_access_token("invalid_token_format"))
        self.assertIsNone(verify_access_token(""))
        self.assertIsNone(verify_access_token(None))

        expired = create_access_token("bob", ttl=-10)
        self.assertIsNone(verify_access_token(expired))

    def test_password_hashing_pbkdf2(self):
        raw_pwd = "MySecurePassword#2026"
        pwd_hash = hash_password(raw_pwd)
        self.assertTrue(pwd_hash.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password(raw_pwd, pwd_hash))
        self.assertFalse(verify_password("WrongPassword", pwd_hash))
        self.assertFalse(verify_password(raw_pwd, "legacy_plain_or_sha256_hash"))

    def test_mask_phone(self):
        self.assertEqual(mask_phone("13812345678"), "138****5678")
        self.assertEqual(mask_phone("123"), "123")
        self.assertEqual(mask_phone(""), "")
        self.assertEqual(mask_phone(None), "")

    def test_get_current_uid_dependency(self):
        token = create_access_token("uid_007", ttl=60)
        self.assertEqual(asyncio.run(get_current_uid(authorization=f"Bearer {token}")), "uid_007")
        self.assertEqual(asyncio.run(get_current_uid(authorization=token)), "uid_007")
        self.assertEqual(asyncio.run(get_current_uid(token=token)), "uid_007")

        for auth_header in (None, "Bearer bad_token"):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(get_current_uid(authorization=auth_header, token=None))
            self.assertEqual(ctx.exception.status_code, 401)


class TestThreadSafeTTLCache(unittest.TestCase):
    def test_basic_ttl_and_lru_eviction(self):
        cache = ThreadSafeTTLCache(maxsize=3, ttl=0.1)
        cache.set("key1", "val1")
        self.assertEqual(cache.get("key1"), "val1")
        self.assertIn("key1", cache)
        time.sleep(0.15)
        self.assertIsNone(cache.get("key1"))
        self.assertNotIn("key1", cache)

        lru = ThreadSafeTTLCache(maxsize=3, ttl=60)
        for k, v in (("a", 1), ("b", 2), ("c", 3), ("d", 4)):
            lru.set(k, v)
        self.assertEqual(len(lru), 3)
        self.assertIsNone(lru.get("a"))
        self.assertEqual(lru.get("d"), 4)

    def test_thread_safety(self):
        cache = ThreadSafeTTLCache(maxsize=100, ttl=10)
        errors = []

        def worker(w_id):
            try:
                for i in range(50):
                    cache.set(f"k_{w_id}_{i}", i)
                    v = cache.get(f"k_{w_id}_{i}")
                    if v != i and v is not None:
                        errors.append("Mismatch")
            except Exception as exc:
                errors.append(str(exc))

        with ThreadPoolExecutor(max_workers=8) as executor:
            for wid in range(8):
                executor.submit(worker, wid)
        self.assertEqual(errors, [])


class TestApiSecurityRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from main import app
        cls.client = TestClient(app)

    def test_goods_list_validation_bounds(self):
        # page_size 限制 <= 50, page >= 1, keyword 长度 <= 500
        self.assertEqual(self.client.get("/api/goods/list?page_size=200").status_code, 422)
        self.assertEqual(self.client.get("/api/goods/list?page=0").status_code, 422)
        self.assertEqual(self.client.get(f"/api/goods/list?keyword={'a' * 501}").status_code, 422)

    def test_home_recommend_validation_bounds(self):
        # limit 限制 <= 50, channel_type >= 0
        self.assertEqual(self.client.get("/api/home/recommend?limit=150").status_code, 422)
        self.assertEqual(self.client.get("/api/home/recommend?channel_type=-1").status_code, 422)

    def test_protected_routes_require_valid_token(self):
        self.assertEqual(self.client.post("/api/order/sync_and_query").status_code, 401)
        self.assertEqual(
            self.client.post("/api/order/sync_and_query", headers={"Authorization": "Bearer fake_token"}).status_code,
            401,
        )
        self.assertEqual(self.client.get("/api/user/info").status_code, 401)
        self.assertEqual(self.client.get("/api/user/auth").status_code, 401)

    def test_promote_convert_validation(self):
        # goods_id max_length = 64
        huge_payload = {"platform": "pdd", "goods_id": "x" * 150, "title": "title", "price": 10.0}
        self.assertEqual(self.client.post("/api/promote/convert", json=huge_payload).status_code, 422)

    def test_search_keyword_preserves_natural_english_model_and_strips_real_tokens(self):
        from models.product import extract_search_keyword
        from services.deal_cleaning.url_venue import is_explicit_coupon_target

        self.assertEqual(extract_search_keyword("(iPhone16Pro) 手机壳"), "(iPhone16Pro) 手机壳")
        self.assertEqual(extract_search_keyword("￥Ab12Cd34Ef5￥ 苹果手机壳"), "苹果手机壳")
        self.assertTrue(
            is_explicit_coupon_target({"clean_input": "https://mobile.yangkeduo.com/duo_coupon_landing.html?goods_id=1"})
        )

    def test_production_docs_disabled_and_cors_allowlist_enforced(self):
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/redoc").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

        allowed_resp = self.client.get(
            "/health",
            headers={"Origin": "https://www.shop.easyeverything.cn"},
        )
        self.assertEqual(allowed_resp.status_code, 200)
        self.assertEqual(
            allowed_resp.headers.get("access-control-allow-origin"),
            "https://www.shop.easyeverything.cn",
        )

        denied_resp = self.client.get(
            "/health",
            headers={"Origin": "https://evil.example.com"},
        )
        self.assertEqual(denied_resp.status_code, 200)
        self.assertIsNone(denied_resp.headers.get("access-control-allow-origin"))

    def test_deploy_nginx_configs_icp_footers_and_manual_hygiene(self):
        from pathlib import Path

        mobile_web_dir = Path(__file__).resolve().parent.parent
        deploy_dir = mobile_web_dir / "deploy"
        nginx_conf = deploy_dir / "mobile_web.nginx.conf"

        self.assertTrue(nginx_conf.is_file(), f"missing nginx config: {nginx_conf}")
        for removed in ("fail2ban", "nginx", "portal", "portal.html", "mobile_web.prod.service"):
            self.assertFalse((deploy_dir / removed).exists(), f"redundant path should be removed: {removed}")

        nginx_text = nginx_conf.read_text(encoding="utf-8")
        self.assertEqual(nginx_text.count("{"), nginx_text.count("}"), "unbalanced braces in mobile_web.nginx.conf")

        first_server_idx = nginx_text.index("\nserver {")
        top_directives = "\n".join(
            line for line in nginx_text[:first_server_idx].splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("server_tokens", top_directives)

        active_nginx_directives = "\n".join(
            line for line in nginx_text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("server_name www.easyeverything.cn", active_nginx_directives)

        for expected in (
            "www.shop.easyeverything.cn",
            "shop.easyeverything.cn",
            "limit_req_zone",
            "limit_conn_zone",
            "/api/system/upstream-health",
            "docs|redoc|openapi",
            "location /api/",
        ):
            self.assertIn(expected, nginx_text)

        for html_path in (
            mobile_web_dir / "static" / "index.html",
            mobile_web_dir / "static" / "detail.html",
            mobile_web_dir / "static" / "group_help.html",
        ):
            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("蜀ICP备2026073570号-1", html_text, str(html_path))
            self.assertIn("https://beian.miit.gov.cn/", html_text, str(html_path))

        deploy_sh = deploy_dir / "deploy.sh"
        self.assertTrue(deploy_sh.is_file(), f"missing deploy script: {deploy_sh}")
        raw_bytes = deploy_sh.read_bytes()
        self.assertNotIn(b"\r\n", raw_bytes, "deploy.sh must use LF line endings")
        deploy_text = raw_bytes.decode("utf-8")
        self.assertTrue(deploy_text.startswith("#!/usr/bin/env bash"))
        for marker in (
            "--init",
            "sync_meilisearch.py",
            "ssl_certificate",
            "--db-path /data/meili_data/data.ms",
            "--max-indexing-memory 268435456",
            "journalctl -u meilisearch.service",
            "--resolve",
            "systemctl restart mobile_web.service",
        ):
            self.assertIn(marker, deploy_text)


if __name__ == "__main__":
    unittest.main()
