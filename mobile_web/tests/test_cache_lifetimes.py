# -*- coding: utf-8 -*-
"""来源截止时间、推广热路径和可展示内容的跨层回归，不连接真实服务。"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config import PRODUCT_DETAIL_TTL, PROMOTION_LINK_TTL
from services import product_service as product_module
from services import promote_service as promote_module
from repositories.product_repo import ProductRepository
from tests.test_catalog_boundaries import catalog, detail
from tests.test_account_order_refactor import promotion
from tests.test_product_repository import MemoryCollection


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(product_module.time, "time", lambda: clock.now)
    return clock


def test_database_detail_refill_preserves_deadline_across_aliases(catalog, clock):
    service, repo, adapter = catalog
    item = detail(price=20)["data"]
    item["detail_updated_at"] = datetime.fromtimestamp(clock.now - PRODUCT_DETAIL_TTL + 1, timezone.utc)
    repo.get_product_detail.return_value = item
    adapter.get_goods_detail.return_value = detail(price=9)

    first = service.get_goods_detail(goods_id="123")
    first["data"]["carousel_images"].append("changed")
    clock.now += 0.5
    cached = service.get_goods_detail(goods_sign="SIGN_A")
    assert cached["source"] == "database"
    assert "changed" not in cached["data"]["carousel_images"]
    assert repo.get_product_detail.call_count == 1

    clock.now += 0.5
    refreshed = service.get_goods_detail(goods_sign="SIGN_A")
    assert refreshed["data"]["price"] == 9
    assert repo.get_product_detail.call_count == 2
    adapter.get_goods_detail.assert_called_once()
    assert "expires_at" not in refreshed
    assert "detail_updated_at" not in refreshed["data"]


@pytest.mark.parametrize("age", [0, PRODUCT_DETAIL_TTL + 1])
def test_database_detail_http_and_cache_do_not_expose_source_metadata(catalog, clock, monkeypatch, age):
    from fastapi.testclient import TestClient
    from api import home
    from main import create_app
    from public_boundary import encode_item_code

    service, repo, adapter = catalog
    item = detail()["data"]
    item["detail_updated_at"] = datetime.fromtimestamp(clock.now - age, timezone.utc)
    repo.get_product_detail.return_value = item
    adapter.get_goods_detail.return_value = {"status": "error"}
    monkeypatch.setattr(home, "product_service", service)
    client = TestClient(create_app())
    for code in (encode_item_code("pdd", "123"), encode_item_code("pdd", "SIGN_A")):
        response = client.get(f"/api/home/detail?item_code={code}")
        assert response.status_code == 200
        result = response.json()
        assert result["source"] == ("database" if age == 0 else "database_fallback")
        assert "detail_updated_at" not in result["data"]
        assert "expires_at" not in result
        assert "goods_id" not in result["data"] and "goods_sign" not in result["data"]
        assert result["data"]["item_code"] == encode_item_code("pdd", "123")
    assert "detail_updated_at" in item
    assert adapter.get_goods_detail.call_count == (0 if age == 0 else 2)


def test_database_write_delay_does_not_restart_detail_lifetime(catalog, clock):
    service, repo, adapter = catalog
    fetched_at = clock.now
    adapter.get_goods_detail.side_effect = [detail(price=20), detail(price=9)]

    def slow_write(item):
        clock.now += PRODUCT_DETAIL_TTL
        return True

    repo.upsert_product_detail.side_effect = slow_write
    first = service.get_goods_detail(goods_id="123")
    stored = repo.upsert_product_detail.call_args.args[0]
    assert stored["detail_updated_at"] == datetime.fromtimestamp(fetched_at, timezone.utc)
    assert "detail_updated_at" not in first["data"]
    assert service.get_goods_detail(goods_id="123")["data"]["price"] == 9
    assert adapter.get_goods_detail.call_count == 2


@pytest.mark.parametrize("timestamp", [True, float("nan"), float("inf"), "bad-date", 1800000001.0])
def test_invalid_or_future_detail_source_time_is_not_fresh(catalog, clock, timestamp):
    service, repo, adapter = catalog
    repo.get_product_detail.return_value = detail(detail_updated_at=timestamp)["data"]
    adapter.get_goods_detail.return_value = detail(price=9)
    assert service.get_goods_detail(goods_id="123")["data"]["price"] == 9
    adapter.get_goods_detail.assert_called_once()


def test_detail_cache_is_shared_across_users(catalog):
    service, _, adapter = catalog
    adapter.get_goods_detail.side_effect = [detail(price=20), detail(price=9)]
    # 用户 A 查询商品详情，触发首次拉取
    assert service.get_goods_detail(goods_id="123", uid="u_1234abcd")["data"]["price"] == 20
    # 用户 B 查询同款商品详情，直接命中跨用户公共热缓存，不重复请求上游 API
    assert service.get_goods_detail(goods_id="123", uid="u_abcd1234")["data"]["price"] == 20
    # 通过商品 Sign 访问同款，同样命中公共热缓存
    assert service.get_goods_detail(goods_sign="SIGN_A", uid="u_1234abcd")["data"]["price"] == 20
    # 验证上游接口仅调用 1 次，公共展示类数据跨用户完全共享
    assert adapter.get_goods_detail.call_count == 1


def test_sign_promotion_hot_hit_skips_identity_and_renders_current_copy(promotion):
    service, adapter, repo = promotion
    repo.get_product_identity.return_value = {"platform": "future_shop", "product_id": "trusted-id"}
    first = service.convert_link("future_shop", goods_sign="SIGN_A", goods_id="wrong-id",
                                 uid="u_1234abcd", title="旧标题", price=20, coupon_amount=2)
    repo.get_product_identity.side_effect = AssertionError("热缓存不能查询身份")
    second = service.convert_link("future_shop", goods_sign="SIGN_A", goods_id="another-wrong-id",
                                  uid="u_1234abcd", title="新标题", price=9, coupon_amount=0)
    assert first["final_link"] == second["final_link"]
    assert "旧标题" in first["command_text"] and "¥20" in first["command_text"]
    assert "新标题" in second["command_text"] and "¥9" in second["command_text"]
    assert "旧标题" not in second["command_text"] and "立减" not in second["command_text"]
    repo.get_product_identity.assert_called_once_with("SIGN_A", platform="future_shop")
    repo.get_product_detail.assert_not_called()
    repo.get_promotion_link_cache.assert_not_called()
    adapter.convert_promotion_url.assert_called_once()
    assert adapter.convert_promotion_url.call_args.kwargs["goods_id"] == "trusted-id"


def test_database_promotion_refill_preserves_expiry_and_rebuilds_copy(promotion, clock):
    service, adapter, repo = promotion
    repo.get_promotion_link_cache.return_value = {
        "status": "success", "final_link": "https://example.com/cached", "command_text": "过时文案",
        "from_cache": True, "_expires_at": clock.now + 1,
    }
    first = service.convert_link("future_shop", "sku-A", title="当前标题", price=7, coupon_amount=0)
    assert first["from_cache"] is True
    assert "当前标题" in first["command_text"] and "过时文案" not in first["command_text"]
    assert "_expires_at" not in first
    clock.now += 0.5
    second = service.convert_link("future_shop", "sku-A", title="另一标题", price=6)
    assert "另一标题" in second["command_text"]
    repo.get_promotion_link_cache.assert_called_once()
    adapter.convert_promotion_url.assert_not_called()

    clock.now += 0.5
    refreshed = service.convert_link("future_shop", "sku-A", title="到期刷新", price=5)
    assert refreshed["final_link"] == "https://example.com/promo"
    assert repo.get_promotion_link_cache.call_count == 2
    adapter.convert_promotion_url.assert_called_once()
    saved = repo.save_promotion_link_cache.call_args.kwargs
    assert "command_text" not in saved["data"]
    assert saved["expires_at"] == clock.now + PROMOTION_LINK_TTL


@pytest.mark.parametrize("expiry", [None, True, float("nan"), float("inf"), 1800000000.0, 1799999999.0,
                                    1800000000.0 + PROMOTION_LINK_TTL + 1])
def test_untrusted_promotion_deadlines_are_not_reused(promotion, clock, expiry):
    service, adapter, repo = promotion
    repo.get_promotion_link_cache.return_value = {
        "status": "success", "final_link": "https://example.com/old", "_expires_at": expiry,
    }
    assert service.convert_link("future_shop", "sku-A")["final_link"] == "https://example.com/promo"
    adapter.convert_promotion_url.assert_called_once()


def test_identity_only_product_is_not_detail_fallback_but_can_be_completed(catalog):
    service, _, adapter = catalog
    collection = MemoryCollection()
    service.repo = ProductRepository(SimpleNamespace(get_collection=lambda name: collection))
    assert service.repo.save_promotion_link_cache(
        "pdd", {"status": "success", "final_link": "https://example.com/promo"}, goods_sign="SIGN_A"
    )
    adapter.get_goods_detail.side_effect = [{"status": "error", "error_msg": "upstream failed"}, detail()]
    assert service.get_goods_detail(goods_sign="SIGN_A") == {"status": "error", "error_msg": "upstream failed"}
    assert service.repo.get_product_identity("SIGN_A")["product_id"] == "SIGN_A"
    assert service.get_goods_detail(goods_sign="SIGN_A")["data"]["price"] == 10
    assert len(collection.docs) == 1
    assert service.repo.get_product_detail("SIGN_A", key_type="sign")["name"] == "商品"


def test_detail_singleflight_collapses_concurrent_requests_across_different_uids(catalog):
    import threading
    import time
    service, _, adapter = catalog

    barrier = threading.Barrier(5)
    call_records = []

    def slow_detail(*args, **kwargs):
        call_records.append(kwargs.get("uid"))
        time.sleep(0.05)
        return detail(price=88.0)

    adapter.get_goods_detail.side_effect = slow_detail

    results = []
    def worker(user_id):
        barrier.wait()
        res = service.get_goods_detail(goods_id="sku_concurrent_99", uid=user_id)
        results.append(res)

    threads = [threading.Thread(target=worker, args=(f"user_{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 5 个不同用户的瞬间并发请求必须全部成功拿到详情
    assert len(results) == 5
    assert all(r["status"] == "success" and r["data"]["price"] == 88.0 for r in results)
    # 防并发击穿 Singleflight 必须将 5 个不同用户的请求聚合成单次上游请求
    assert adapter.get_goods_detail.call_count == 1


class _FormalProductsCollection(MemoryCollection):
    """补齐 bulk_write.matched_count，便于直接驱动 upsert_cleaned_products_to_formal_table。"""

    def bulk_write(self, operations, ordered=False):
        inserted = modified = matched = 0
        for operation in operations:
            result = self.update_one(operation._filter, operation._doc, upsert=operation._upsert)
            inserted += int(result.upserted_id is not None)
            modified += result.modified_count
            matched += int(result.matched_count)
        return SimpleNamespace(upserted_count=inserted, modified_count=modified, matched_count=matched)


def _make_preseed_platform_adapter(plat: str, spec: dict):
    from unittest.mock import MagicMock
    from adapters.base import build_command_text
    from models.product import ProductRef

    class _PreseedAdapter:
        active = True
        platform_code = plat
        platform_name = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多"}[plat]
        requires_numeric_promoter_id = plat == "jd"
        default_promoter_id = "10086" if plat == "jd" else ""
        supports_coupon_enrichment = plat == "jd"
        requires_authorized_promotion = plat in ("taobao", "pdd")

        def __init__(self):
            self.resolve_and_convert_universal = MagicMock(return_value=dict(spec["stage3_res"]))
            self.convert_promotion_url = MagicMock(return_value=dict(spec["refresh_res"]))

        @staticmethod
        def resolve_product_ref(raw="", goods_id=None, goods_sign=None):
            return ProductRef(plat, goods_id or "", goods_sign or "")

        @staticmethod
        def extract_trusted_coupon_url(doc):
            if not isinstance(doc, dict):
                return ""
            cinfo = doc.get("coupon_info") if isinstance(doc.get("coupon_info"), dict) else {}
            return str(cinfo.get("coupon_url") or doc.get("coupon_url") or "")

        @staticmethod
        def should_enrich_promotion_coupon(ref, doc=None):
            return False

        @staticmethod
        def build_command_text(title, final_link, price=None, coupon=None):
            return build_command_text(title, final_link, price, coupon, {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多"}[plat])

    return _PreseedAdapter()


@pytest.mark.parametrize(
    ("plat", "spec"),
    [
        (
            "jd",
            {
                "raw_input": "https://item.jd.com/1000123456.html",
                "goods_id": "1000123456",
                "goods_sign": "jd_sign_1000123456",
                "stage3_res": {
                    "status": "success",
                    "goods_id": "1000123456",
                    "goods_sign": "jd_sign_1000123456",
                    "final_link": "https://u.jd.com/preseed_jd",
                    "url": "https://u.jd.com/preseed_jd",
                    "short_url": "https://u.jd.com/preseed_jd",
                    "promotion_token": "21:/abcJDcmd",
                    "promoter_id": "10086",
                    "coupon_url": "https://coupon.m.jd.com/coupons/show.action?key=c1",
                    "link_type": "京东推广链接",
                    "cacheable": True,
                    "is_fallback": False,
                    "price": 99.0,
                    "coupon_amount": 10.0,
                    "title": "京东预置好物",
                    "product": {
                        "name": "京东预置好物",
                        "price": 99.0,
                        "original_price": 109.0,
                        "activity_price": 89.0,
                        "coupon_amount": 10.0,
                        "product_url": "https://item.jd.com/1000123456.html",
                        "image_url": "https://img.jd.com/1.jpg",
                        "carousel_images": ["https://img.jd.com/1.jpg"],
                        "coupon_info": {"coupon_url": "https://coupon.m.jd.com/coupons/show.action?key=c1"},
                        "raw_item": {"blob": "huge"},
                    },
                },
                "refresh_res": {
                    "status": "success",
                    "final_link": "https://u.jd.com/fresh_jd",
                    "url": "https://u.jd.com/fresh_jd",
                    "short_url": "https://u.jd.com/fresh_jd",
                    "promotion_token": "21:/freshJD",
                    "promoter_id": "10086",
                    "coupon_url": "https://coupon.m.jd.com/coupons/show.action?key=c1",
                    "cacheable": True,
                    "is_fallback": False,
                },
                "personal_res": {
                    "status": "success",
                    "final_link": "https://u.jd.com/personal_jd",
                    "url": "https://u.jd.com/personal_jd",
                    "promoter_id": "20086",
                    "cacheable": True,
                    "is_fallback": False,
                },
            },
        ),
        (
            "taobao",
            {
                "raw_input": "https://uland.taobao.com/item/edetail?id=tb_opaque_1",
                "goods_id": "tb_gid_1",
                "goods_sign": "tb_sign_1",
                "stage3_res": {
                    "status": "success",
                    "goods_id": "tb_gid_1",
                    "goods_sign": "tb_sign_1",
                    "final_link": "https://s.click.taobao.com/preseed_tb",
                    "url": "https://s.click.taobao.com/preseed_tb",
                    "short_url": "",
                    "promotion_token": "￥TB123￥",
                    "token_type": "tpwd",
                    "link_type": "淘宝官方淘口令",
                    "cacheable": True,
                    "is_fallback": False,
                    "price": 49.0,
                    "coupon_amount": 5.0,
                    "title": "淘宝预置好物",
                    "product": {
                        "name": "淘宝预置好物",
                        "price": 49.0,
                        "original_price": 59.0,
                        "activity_price": 44.0,
                        "coupon_amount": 5.0,
                        "product_url": "https://uland.taobao.com/item/edetail?id=tb_opaque_1",
                        "image_url": "https://img.alicdn.com/1.jpg",
                        "carousel_images": ["https://img.alicdn.com/1.jpg"],
                    },
                },
                "refresh_res": {
                    "status": "success",
                    "final_link": "https://s.click.taobao.com/fresh_tb",
                    "url": "https://s.click.taobao.com/fresh_tb",
                    "promotion_token": "￥FRESH￥",
                    "token_type": "tpwd",
                    "cacheable": True,
                    "is_fallback": False,
                },
                "personal_res": {
                    "status": "success",
                    "final_link": "https://s.click.taobao.com/personal_tb",
                    "url": "https://s.click.taobao.com/personal_tb",
                    "promotion_token": "￥MINE￥",
                    "token_type": "tpwd",
                    "relation_id": "999888",
                    "attribution_status": "verified",
                    "cacheable": True,
                    "is_fallback": False,
                },
            },
        ),
        (
            "pdd",
            {
                "raw_input": "https://p.pinduoduo.com/preseed123",
                "goods_id": "",
                "goods_sign": "E9_pdd_sign_1",
                "stage3_res": {
                    "status": "success",
                    "goods_id": "987654321",
                    "goods_sign": "E9_pdd_sign_1",
                    "final_link": "https://p.pinduoduo.com/preseed_pdd",
                    "url": "https://p.pinduoduo.com/preseed_pdd",
                    "mobile_url": "https://mobile.yangkeduo.com/preseed_pdd",
                    "short_url": "https://p.pinduoduo.com/preseed_pdd",
                    "we_app_info": {"page_path": "pages/index?a=1"},
                    "link_type": "拼多多小程序推广",
                    "cacheable": True,
                    "is_fallback": False,
                    "price": 19.9,
                    "coupon_amount": 3.0,
                    "title": "拼多多预置好物",
                    "product": {
                        "name": "拼多多预置好物",
                        "price": 19.9,
                        "original_price": 29.9,
                        "activity_price": 16.9,
                        "coupon_amount": 3.0,
                        "product_url": "https://p.pinduoduo.com/preseed123",
                        "image_url": "https://img.pddpic.com/1.jpg",
                        "carousel_images": ["https://img.pddpic.com/1.jpg"],
                    },
                },
                "refresh_res": {
                    "status": "success",
                    "final_link": "https://p.pinduoduo.com/fresh_pdd",
                    "url": "https://p.pinduoduo.com/fresh_pdd",
                    "short_url": "https://p.pinduoduo.com/fresh_pdd",
                    "we_app_info": {"page_path": "pages/fresh?a=2"},
                    "cacheable": True,
                    "is_fallback": False,
                },
                "personal_res": {
                    "status": "success",
                    "final_link": "https://p.pinduoduo.com/personal_pdd",
                    "url": "https://p.pinduoduo.com/personal_pdd",
                    "attribution_status": "verified",
                    "cacheable": True,
                    "is_fallback": False,
                },
            },
        ),
    ],
)
def test_stage_worker_handoff_preseeds_default_promotion_and_hits_db_on_first_click(monkeypatch, plat, spec):
    from copy import deepcopy
    from unittest.mock import MagicMock, patch
    from repositories.product_repo import upsert_cleaned_products_to_formal_table
    from services.deal_pool_cleaner import DealPoolCleaner

    promote_module.PROMOTION_CACHE.clear()
    adapter = _make_preseed_platform_adapter(plat, spec)
    cleaner = DealPoolCleaner(ai_batch_size=2, convert_max_workers=1)
    cleaner.adapters = {plat: adapter}

    raw_doc = {
        "_id": f"tip_{plat}_1",
        "source_site": "guangdiu",
        "source_channel": "hot",
        "source_item_id": f"tip_{plat}_1",
        "title": spec["stage3_res"]["title"],
        "platform": plat,
        "publish_ts": 1800000000.0,
        "ai_stage1_result": {
            "is_standalone_deal": True,
            "merged_tip_ids": [f"tip_{plat}_1"],
            "combined_raw_text": f"好物推荐 [#L1]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "clean_input": spec["raw_input"], "platform_hint": plat, "text_role_hint": "main_candidate"}
            ],
        },
    }

    conv_out, _ = cleaner.process_convert_unit([raw_doc], formal_col=None, shared_active_products={})
    assert len(conv_out) == 1
    assert conv_out[0]["stage_outcome"] == "advance_to_ai2"
    persisted_conv = deepcopy(conv_out[0]["converted_links"])
    assert "adapter_res" not in persisted_conv[0]
    assert "promo_payload" in persisted_conv[0]
    assert "raw_item" not in persisted_conv[0]["promo_payload"]
    assert "product" not in persisted_conv[0]["promo_payload"]

    # 模拟 StageWorker-AI2 跨阶段从数据库读取（不携带全内存 full_conv_list）
    stage4_doc = deepcopy(raw_doc)
    stage4_doc["converted_links"] = persisted_conv
    ai2_mock = {
        f"tip_{plat}_1": {
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": spec["stage3_res"]["price"] - spec["stage3_res"]["coupon_amount"],
            "quality_score": 88,
            "clean_title": spec["stage3_res"]["title"],
            "tutorial_steps": ["1. 直接下单"],
        }
    }
    with patch.object(cleaner, "run_ai_stage2_batch", return_value=ai2_mock):
        ai2_out, _ = cleaner.process_ai2_unit([stage4_doc])

    assert len(ai2_out) == 1 and ai2_out[0]["stage_outcome"] == "success"
    product_doc = ai2_out[0]["product_doc"]
    preseeded = product_doc.get("default_promotion")
    assert isinstance(preseeded, dict)
    assert preseeded["final_link"] == spec["stage3_res"]["final_link"]
    assert isinstance(product_doc.get("default_promotion_updated_at"), datetime)
    assert isinstance(product_doc.get("default_promotion_expires_at"), datetime)
    if plat == "jd":
        assert preseeded["token_type"] != "tpwd"
        assert preseeded["promoter_id"] == "10086"
        assert preseeded["coupon_url"] == spec["stage3_res"]["coupon_url"]
    elif plat == "taobao":
        assert preseeded["token_type"] == "tpwd"
        assert preseeded["promotion_token"] == "￥TB123￥"
    else:
        assert preseeded["we_app_info"] == {"page_path": "pages/index?a=1"}

    formal_col = _FormalProductsCollection()
    upsert_cleaned_products_to_formal_table(formal_col, [product_doc])
    repo = ProductRepository(SimpleNamespace(get_collection=lambda _n: formal_col))
    monkeypatch.setattr(promote_module, "get_adapter", lambda _p: adapter)
    user_service = MagicMock()
    user_service.get_numeric_promotion_context.return_value = {"status": "success", "promoter_id": "20086"}
    user_service.get_promotion_context.return_value = {
        "status": "success", "is_authorized": True, "identity": {"relation_id": "999888"},
    }
    service = promote_module.PromoteService(repo, user_service=user_service)

    # 第 1 次匿名点击（清空进程内存，仅走 L2 MongoDB default_promotion）-> 0 上游调用
    promote_module.PROMOTION_CACHE.clear()
    first = service.convert_link(
        plat,
        goods_id=spec["goods_id"] or None,
        goods_sign=spec["goods_sign"] or None,
        uid=None,
        title="首击文案",
    )
    assert first["status"] == "success"
    assert first["from_cache"] is True
    assert first["final_link"] == spec["stage3_res"]["final_link"]
    assert adapter.convert_promotion_url.call_count == 0
    if plat == "taobao":
        assert first["promotion_token"] == "￥TB123￥"
    elif plat == "pdd":
        assert first["we_app_info"] == {"page_path": "pages/index?a=1"}

    # 第 2 次匿名点击 -> 命中 L1 进程内存 PROMOTION_CACHE，连 DB 都不额外回查
    db_queries_after_first = len(formal_col.queries)
    second = service.convert_link(
        plat,
        goods_id=spec["goods_id"] or None,
        goods_sign=spec["goods_sign"] or None,
        uid=None,
        title="次击文案",
    )
    assert second["final_link"] == spec["stage3_res"]["final_link"]
    assert adapter.convert_promotion_url.call_count == 0
    assert len(formal_col.queries) == db_queries_after_first

    # 登录推手访问 -> 必须跳过站长公共缓存，独立调用上游生成个人归因，且不污染站长 L2 缓存
    adapter.convert_promotion_url.return_value = dict(spec["personal_res"])
    personal = service.convert_link(
        plat,
        goods_id=spec["goods_id"] or None,
        goods_sign=spec["goods_sign"] or None,
        uid="u_1234abcd",
        title="推手专属",
    )
    assert personal["status"] == "success"
    assert personal["final_link"] == spec["personal_res"]["final_link"]
    assert adapter.convert_promotion_url.call_count == 1
    assert formal_col.docs[0]["default_promotion"]["final_link"] == spec["stage3_res"]["final_link"]
    promote_module.PROMOTION_CACHE.clear()


def test_preseed_guardrails_and_three_day_expiry_refresh(monkeypatch):
    import time as py_time
    from copy import deepcopy
    from unittest.mock import patch
    from repositories.product_repo import upsert_cleaned_products_to_formal_table
    from services.deal_pool_cleaner import DealPoolCleaner

    promote_module.PROMOTION_CACHE.clear()

    def _run_ai2(plat: str, promo_payload: dict):
        adapter = SimpleNamespace(
            active=True,
            platform_code=plat,
            platform_name=plat,
            requires_numeric_promoter_id=plat == "jd",
        )
        cleaner = DealPoolCleaner(ai_batch_size=2, convert_max_workers=1)
        cleaner.adapters = {plat: adapter}
        doc = {
            "_id": f"guard_{plat}",
            "source_site": "guangdiu",
            "source_channel": "hot",
            "source_item_id": f"guard_{plat}",
            "title": "测试商品",
            "platform": plat,
            "publish_ts": py_time.time(),
            "ai_stage1_result": {"is_standalone_deal": True, "merged_tip_ids": [f"guard_{plat}"]},
            "converted_links": [
                {
                    "link_id": "L1",
                    "clean_input": "https://u.jd.com/x",
                    "text_role_hint": "main_candidate",
                    "convert_status": "product",
                    "platform": plat,
                    "goods_id": "10002",
                    "goods_sign": "sign_10002",
                    "my_promo_link": "https://u.jd.com/promo_x",
                    "my_promo_token": "",
                    "official_product": {
                        "title": "测试商品",
                        "price": 50.0,
                        "original_price": 60.0,
                        "activity_price": 50.0,
                        "coupon_amount": 0.0,
                        "image_url": "https://img.jd.com/2.jpg",
                        "carousel_images": ["https://img.jd.com/2.jpg"],
                    },
                    "promo_payload": promo_payload,
                }
            ],
        }
        ai2_mock = {
            f"guard_{plat}": {
                "is_valid_deal": True,
                "main_link_id": "L1",
                "deal_price": 50.0,
                "quality_score": 85,
                "clean_title": "测试商品",
                "tutorial_steps": ["1. 下单"],
            }
        }
        with patch.object(cleaner, "run_ai_stage2_batch", return_value=ai2_mock):
            out, _ = cleaner.process_ai2_unit([doc])
        return out[0]["product_doc"]

    # 1) 降级 / cacheable=False 绝不预置 default_promotion
    assert "default_promotion" not in _run_ai2("pdd", {"final_link": "https://p.pinduoduo.com/x", "is_fallback": True, "cacheable": True})
    assert "default_promotion" not in _run_ai2("taobao", {"final_link": "https://s.click.taobao.com/x", "is_fallback": False, "cacheable": False})

    # 2) 上游若返回更短 expires_at，default_promotion_expires_at 必须向下钳位而不强套 3 天
    now_t = py_time.time()
    shorter_doc = _run_ai2("taobao", {
        "final_link": "https://s.click.taobao.com/short",
        "is_fallback": False,
        "cacheable": True,
        "expires_at": now_t + 3600.0,
    })
    assert shorter_doc["default_promotion"]["expires_at"] == pytest.approx(now_t + 3600.0)
    assert shorter_doc["default_promotion_expires_at"].timestamp() <= now_t + 3601.0

    # 3) 入库后若超过 3 天过期（回拨 default_promotion_expires_at），下次匿名点击必须重拉上游并刷新 DB
    seed_doc = _run_ai2("taobao", {
        "final_link": "https://s.click.taobao.com/initial",
        "promotion_token": "￥INIT￥",
        "token_type": "tpwd",
        "is_fallback": False,
        "cacheable": True,
    })
    formal_col = _FormalProductsCollection()
    upsert_cleaned_products_to_formal_table(formal_col, [deepcopy(seed_doc)])
    formal_col.docs[0]["default_promotion_expires_at"] = datetime.fromtimestamp(py_time.time() - 60.0, timezone.utc)

    tb_spec = {
        "stage3_res": {"status": "success"},
        "refresh_res": {
            "status": "success",
            "final_link": "https://s.click.taobao.com/refreshed_3d",
            "url": "https://s.click.taobao.com/refreshed_3d",
            "promotion_token": "￥REFRESHED￥",
            "token_type": "tpwd",
            "cacheable": True,
            "is_fallback": False,
        },
    }
    adapter = _make_preseed_platform_adapter("taobao", tb_spec)
    adapter.requires_authorized_promotion = True
    monkeypatch.setattr(promote_module, "get_adapter", lambda _p: adapter)
    repo = ProductRepository(SimpleNamespace(get_collection=lambda _n: formal_col))
    service = promote_module.PromoteService(repo)

    res = service.convert_link("taobao", goods_id="10002", goods_sign="sign_10002", uid=None)
    assert res["status"] == "success"
    assert res["final_link"] == "https://s.click.taobao.com/refreshed_3d"
    assert adapter.convert_promotion_url.call_count == 1
    assert formal_col.docs[0]["default_promotion"]["final_link"] == "https://s.click.taobao.com/refreshed_3d"
    assert formal_col.docs[0]["default_promotion_expires_at"].timestamp() > py_time.time() + PROMOTION_LINK_TTL - 30
    promote_module.PROMOTION_CACHE.clear()


def test_jd_l2_promotion_cache_ignores_coupon_mismatch_and_uses_ttl_only(monkeypatch):
    import time as py_time
    from repositories.product_repo import upsert_cleaned_products_to_formal_table
    from services.deal_pool_cleaner import DealPoolCleaner

    promote_module.PROMOTION_CACHE.clear()
    jd_spec = {
        "stage3_res": {
            "status": "success",
            "goods_id": "1000888",
            "goods_sign": "jd_sign_888",
            "final_link": "https://u.jd.com/cached_plain",
            "url": "https://u.jd.com/cached_plain",
            "short_url": "https://u.jd.com/cached_plain",
            "promoter_id": "10086",
            "coupon_url": "https://coupon.m.jd.com/old_coupon",
            "cacheable": True,
            "is_fallback": False,
            "price": 80.0,
            "coupon_amount": 10.0,
            "title": "京东纯超时测试",
            "product": {
                "name": "京东纯超时测试",
                "price": 80.0,
                "original_price": 90.0,
                "activity_price": 70.0,
                "coupon_amount": 10.0,
                "product_url": "https://item.jd.com/1000888.html",
                "coupon_info": {"coupon_url": "https://coupon.m.jd.com/new_coupon"},
            },
        },
        "refresh_res": {
            "status": "success",
            "final_link": "https://u.jd.com/after_ttl_fresh",
            "url": "https://u.jd.com/after_ttl_fresh",
            "short_url": "https://u.jd.com/after_ttl_fresh",
            "promoter_id": "10086",
            "cacheable": True,
            "is_fallback": False,
        },
    }
    adapter = _make_preseed_platform_adapter("jd", jd_spec)
    cleaner = DealPoolCleaner(ai_batch_size=2, convert_max_workers=1)
    cleaner.adapters = {"jd": adapter}
    from unittest.mock import patch
    stage4_doc = {
        "_id": "jd_ttl_1",
        "source_site": "guangdiu",
        "source_channel": "hot",
        "source_item_id": "jd_ttl_1",
        "title": "京东纯超时测试",
        "platform": "jd",
        "publish_ts": py_time.time(),
        "ai_stage1_result": {"is_standalone_deal": True, "merged_tip_ids": ["jd_ttl_1"]},
        "converted_links": [
            {
                "link_id": "L1",
                "clean_input": "https://item.jd.com/1000888.html",
                "text_role_hint": "main_candidate",
                "convert_status": "product",
                "platform": "jd",
                "goods_id": "1000888",
                "goods_sign": "jd_sign_888",
                "my_promo_link": "https://u.jd.com/cached_plain",
                "my_promo_token": "",
                "official_product": jd_spec["stage3_res"]["product"],
                "promo_payload": {
                    "final_link": "https://u.jd.com/cached_plain",
                    "url": "https://u.jd.com/cached_plain",
                    "short_url": "https://u.jd.com/cached_plain",
                    "promoter_id": "10086",
                    "coupon_url": "https://coupon.m.jd.com/old_coupon",
                    "cacheable": True,
                    "is_fallback": False,
                },
            }
        ],
    }
    ai2_mock = {
        "jd_ttl_1": {
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": 70.0,
            "quality_score": 88,
            "clean_title": "京东纯超时测试",
            "tutorial_steps": ["1. 下单"],
        }
    }
    with patch.object(cleaner, "run_ai_stage2_batch", return_value=ai2_mock):
        ai2_out, _ = cleaner.process_ai2_unit([stage4_doc])

    formal_col = _FormalProductsCollection()
    upsert_cleaned_products_to_formal_table(formal_col, [ai2_out[0]["product_doc"]])
    repo = ProductRepository(SimpleNamespace(get_collection=lambda _n: formal_col))
    monkeypatch.setattr(promote_module, "get_adapter", lambda _p: adapter)
    service = promote_module.PromoteService(repo)

    # 未过期前，即便商品文档中的 coupon_url 与 default_promotion.coupon_url 不同，仍直接命中 L2 缓存（0 上游、不额外补查券）
    promote_module.PROMOTION_CACHE.clear()
    hit = service.convert_link("jd", goods_id="1000888", goods_sign="jd_sign_888", uid=None)
    assert hit["status"] == "success"
    assert hit["from_cache"] is True
    assert hit["final_link"] == "https://u.jd.com/cached_plain"
    assert adapter.convert_promotion_url.call_count == 0

    # 超时后才重新向上游生链并刷新数据库缓存
    promote_module.PROMOTION_CACHE.clear()
    formal_col.docs[0]["default_promotion_expires_at"] = datetime.fromtimestamp(py_time.time() - 60.0, timezone.utc)
    refreshed = service.convert_link("jd", goods_id="1000888", goods_sign="jd_sign_888", uid=None)
    assert refreshed["final_link"] == "https://u.jd.com/after_ttl_fresh"
    assert adapter.convert_promotion_url.call_count == 1
    promote_module.PROMOTION_CACHE.clear()

