# -*- coding: utf-8 -*-
"""详情线报覆盖回归：保证“库里有线报，详情就一定展示”，且不误删、不猜测。"""
from contextlib import contextmanager
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from adapters.pdd_adapter import PddAdapter
from cache import clear_all_caches, get_cached_deal_tip
from services.product_service import ProductService

TIP = {
    "publish_time": "2026-11-20 14:27",
    "deal_price": 74.5,
    "unit_deal_price": "2.48元/杯",
    "price_logic": "买5件合计74.5元",
    "coupon_links": [],
    "tutorial_steps": ["1. 下单5件"],
    "original_copywriting": "爆料原文",
    "share_images": [],
    "tips_timeline": [],
}


@pytest.fixture(autouse=True)
def _clean_caches():
    clear_all_caches()
    yield
    clear_all_caches()


@pytest.fixture
def service():
    repo = MagicMock()
    repo.get_product_detail.return_value = None
    repo.upsert_product_detail.return_value = True
    repo.get_deal_tip_view.return_value = ("ok", TIP)
    adapter = MagicMock(spec=PddAdapter)
    adapter.active = True
    adapter.resolve_product_ref.side_effect = PddAdapter().resolve_product_ref
    svc = ProductService(product_repo=repo)
    with patch("services.product_service.get_adapter", return_value=adapter):
        yield svc, repo, adapter


def payload(goods_id="123", sign="SIGN_A", **kwargs):
    return {"status": "success", "data": {
        "platform": "pdd", "goods_id": goods_id, "goods_sign": sign, "name": "商品",
        "price": 10.0, "carousel_images": ["https://img.test/a"], **kwargs,
    }}


def test_upstream_payload_without_deal_tip_is_overlaid(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload()
    result = svc.get_goods_detail(goods_id="123")
    assert result["data"]["deal_tip"] == TIP
    repo.get_deal_tip_view.assert_called_once_with("123", platform="pdd")


def test_cache_hit_still_carries_fresh_deal_tip(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload()
    svc.get_goods_detail(goods_id="123")          # 首次：上游浅数据 + 覆盖
    assert adapter.get_goods_detail.call_count == 1
    repo.get_deal_tip_view.reset_mock()
    second = svc.get_goods_detail(goods_sign="SIGN_A")   # 第二次：命中详情缓存
    assert second["data"]["deal_tip"] == TIP
    assert repo.get_deal_tip_view.call_count == 0          # 60 秒短线报缓存生效
    assert get_cached_deal_tip("pdd", "123") is not None


def test_database_says_no_deal_hides_stale_tip(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload(deal_tip=TIP)
    repo.get_deal_tip_view.return_value = ("ok", None)
    result = svc.get_goods_detail(goods_id="123")
    assert result["data"]["deal_tip"] is None


def test_view_error_keeps_existing_tip(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload(deal_tip=TIP)
    repo.get_deal_tip_view.side_effect = RuntimeError("mongo down")
    result = svc.get_goods_detail(goods_id="123")
    assert result["data"]["deal_tip"] == TIP
    assert get_cached_deal_tip("pdd", "123") is None   # 失败不缓存，下次重试


def test_view_missing_does_not_rewrite_payload(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload(deal_tip=TIP)
    repo.get_deal_tip_view.return_value = ("missing", None)
    result = svc.get_goods_detail(goods_id="123")
    assert result["data"]["deal_tip"] == TIP


def test_overlay_skips_when_product_identity_unknown(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload(goods_id="")
    result = svc.get_goods_detail(goods_sign="SIGN_ONLY")
    assert repo.get_deal_tip_view.call_count == 0
    assert "deal_tip" not in result["data"]


def test_error_state_is_not_cached_and_retries(service):
    svc, repo, adapter = service
    adapter.get_goods_detail.return_value = payload()
    repo.get_deal_tip_view.return_value = ("error", None)
    first = svc.get_goods_detail(goods_id="123")
    assert first["data"].get("deal_tip") is None
    repo.get_deal_tip_view.return_value = ("ok", TIP)
    second = svc.get_goods_detail(goods_id="123")
    assert second["data"]["deal_tip"] == TIP
    assert repo.get_deal_tip_view.call_count == 2


# —— 仓储投影本身的三种状态 ——

@contextmanager
def _repo_with(find_one):
    from repositories.product_repo import ProductRepository

    coll = MagicMock()
    coll.find_one.side_effect = find_one
    with patch.object(ProductRepository, "col", new_callable=PropertyMock, return_value=coll):
        yield ProductRepository(), coll


def test_view_reports_ok_for_product_without_deal_info():
    """商品存在但整条没有 deal_info 时必须是 ok+None，否则旧线报永远清不掉。"""
    with _repo_with(lambda *a, **k: {"_id": object()}) as (repo, coll):
        assert repo.get_deal_tip_view("123", platform="pdd") == ("ok", None)
        projection = coll.find_one.call_args[0][1]
    assert projection == {"deal_info": 1}, "投影必须保留 _id，否则空文档会被误判为不存在"


def test_view_reports_missing_only_when_document_absent():
    with _repo_with(lambda *a, **k: None) as (repo, _):
        assert repo.get_deal_tip_view("123", platform="pdd") == ("missing", None)


def test_view_reports_error_when_query_raises():
    def boom(*a, **k):
        raise RuntimeError("mongo down")

    with _repo_with(boom) as (repo, _):
        assert repo.get_deal_tip_view("123", platform="pdd") == ("error", None)


def test_expired_deal_remains_visible_with_is_deal_tip_false():
    """验证用户产品原则：超期摘标(is_deal_tip=False)后，只要包含教程/文案/价格等实质内容，详情页依然保留展示。"""
    from repositories.product_repo import ProductRepository

    doc = {
        "_id": "dummy_id",
        "deal_info": {
            "is_deal_tip": False,
            "publish_time": "2026-11-16 19:56:31",
            "updated_at": "2026-11-18 06:49:06",
            "deal_price": 12.9,
            "tutorial_steps": ["1. 领券下单", "2. 实付12.9元"],
            "original_copywriting": "海天调料12件套组合",
        },
    }
    tip = ProductRepository._extract_deal_tip(doc)
    assert tip is not None
    assert tip["is_deal_tip"] is False
    assert tip["publish_time"] == "2026-11-16 19:56"
    assert "updated_at" not in tip
    assert tip["deal_price"] == 12.9
    assert len(tip["tutorial_steps"]) == 2

