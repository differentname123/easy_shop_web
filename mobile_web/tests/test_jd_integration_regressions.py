"""复核发现的真实边界：写入背压、来源精度和显示时区。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from cache import MATERIAL_CACHE, SEARCH_CACHE
from models.product import ProductRef
from repositories.order_repo import OrderRepository
from services.product_service import ProductService
from services.promote_service import PromoteService
from tests.test_order_sync_protocol import MemoryCollection, repository


def test_material_mapping_available_immediately_and_on_search_hit_despite_write_backpressure(monkeypatch):
    item = {"platform": "jd", "goods_id": "opaque-no-detail-url", "name": "商品", "price": 10,
            "product_url": "https://jingfen.jd.com/detail/verified.html"}
    adapter = SimpleNamespace(active=True, requires_material_url=True,
                              search_goods=Mock(return_value={"status": "success", "data": [item]}))
    monkeypatch.setattr("services.product_service.get_adapter", lambda platform: adapter)
    repo = Mock()
    repo.get_product_detail.return_value = None
    product = ProductService(repo)
    promotion = PromoteService(repo)
    ref = ProductRef("jd", item["goods_id"])
    assert product._summary_write_slots.acquire(False)
    assert product._summary_write_slots.acquire(False)
    try:
        assert product.search_home_goods("测试商品", platform="jd")["status"] == "success"
        repo.upsert_home_products.assert_not_called()
        assert promotion._trusted_material_url(ref) == item["product_url"]
        repo.get_product_detail.assert_not_called()
    finally:
        product._summary_write_slots.release()
        product._summary_write_slots.release()
        product.wait_for_background_writes()
    # 搜索缓存命中也恢复可信映射，不必依赖可关闭的预取和再次请求供应商。
    MATERIAL_CACHE.clear()
    assert SEARCH_CACHE.size() == 1
    product.search_home_goods("测试商品", platform="jd")
    assert promotion._trusted_material_url(ref) == item["product_url"]
    adapter.search_goods.assert_called_once()


@pytest.mark.parametrize("newer,older", [
    (1793926800900, 1793926800100),
    (1793926800.9, 1793926800.1),
    ("2026-11-06 09:00:00.900", "2026-11-06 09:00:00.100"),
])
def test_fractional_modify_clock_cannot_allow_old_state_to_roll_back(newer, older):
    adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=Mock())
    base = {"id": "line", "orderId": "parent", "positionId": "123",
            "orderTime": "2026-11-06 08:00:00", "skuId": "1234", "skuName": "商品",
            "estimateFee": 2, "actualFee": 1.5}
    latest = adapter.normalize_order(dict(base, validCode=18, modifyTime=newer))
    prior = adapter.normalize_order(dict(base, validCode=16, modifyTime=older))
    assert latest["order_modify_at"] > prior["order_modify_at"]
    assert latest["source_updated_at"] == latest["order_modify_at"]
    col = MemoryCollection()
    repo = repository(col)
    repo.upsert_orders([latest], "jd")
    repo.upsert_orders([prior], "jd")
    assert col.docs[0]["is_settled"] is True
    assert col.docs[0]["promotion_amount_yuan"] == 1.5
    assert col.docs[0]["order_modify_at"] == latest["order_modify_at"]


@pytest.mark.parametrize("source", ["2026-11-06 08:00:00", "2026-11-06 00:05:00"])
def test_jd_display_time_roundtrips_shanghai_not_timezone_less_utc(source):
    adapter = JdAdapter(SimpleNamespace(jd_enabled=True), client=Mock())
    doc = adapter.normalize_order({"id": "row", "validCode": 16, "orderTime": source})
    row = OrderRepository()._format_order(doc)
    assert row["order_time_str"] == source[:16]
    assert row["order_time_basis"] == "created" and row["order_pay_time_str"] == ""


def test_unified_jd_coupon_extraction_rank_preservation_and_depleted_fallback():
    from cache import clear_all_caches
    from infrastructure.jd_client import DEFAULT_API_BASE, JdClient, PROMOTION_ROOT
    from repositories.product_repo import ProductRepository
    from tests.test_product_repository import MemoryCollection as ProductMemCol

    clear_all_caches()
    enc_sign = "Wl4yEImIlQsfIguVL32ZfIguVL32Z4_3QWdizpmh42cklzdJw"
    jd_img = "https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg"
    coupon_link = "http://coupon.m.jd.com/coupons/show.action?key=c123&roleId=999"

    # 1. _summary 统一提取 coupon_id -> coupon_url，入库后即使后续详情券额为0也不抹除已有 coupon_url，且无券榜单轮询不覆盖优惠券与券后价
    col = ProductMemCol()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda name: col))

    jd_adapter = JdAdapter(SimpleNamespace(jd_enabled=True, jd_position_id="88"), client=Mock())
    detail_card = jd_adapter._detail({
        "tao_id": enc_sign,
        "title": "可口可乐500ml*24瓶",
        "quanhou_jiage": "44.90",
        "size": "52.90",
        "coupon_info_money": "8",
        "coupon_id": coupon_link,
        "pict_url": jd_img,
        "item_url": f"https://jingfen.jd.com/detail/{enc_sign}.html",
    })
    assert detail_card["coupon_url"] == coupon_link
    assert detail_card["price"] == 44.9 and detail_card["coupon_amount"] == 8.0
    assert repo.upsert_product_detail(detail_card) is True

    # 京东实时销量榜 5 分钟轮询返回无券摘要 -> 自动保留已知券与券后价
    rank_card = jd_adapter._summary({
        "itemId": enc_sign,
        "skuName": "可口可乐500ml*24瓶",
        "wlprice": "52.90",
        "imageUrl": jd_img,
        "inOrderCount30Days": "5000",
    })
    assert "coupon_url" not in rank_card and rank_card["coupon_amount"] == 0.0
    repo.upsert_home_products([rank_card], channel_type=5)

    saved = repo.get_product_detail(enc_sign, platform="jd", key_type="sign")
    assert saved["coupon_url"] == coupon_link
    assert saved["coupon_amount"] == 8.0 and saved["has_coupon"] is True
    assert saved["price"] == 44.9 and saved["original_price"] == 52.9
    assert saved["sales_tip"] == "5000"

    # 2. 二合一领券转链：优先带 couponUrl；若券已领完自动无损回退普通推广链
    replies = [
        SimpleNamespace(status_code=200, raise_for_status=Mock(), json=Mock(return_value={
            PROMOTION_ROOT: {"result": {"code": 400, "message": "优惠券已领完或无效"}}
        })),
        SimpleNamespace(status_code=200, raise_for_status=Mock(), json=Mock(return_value={
            PROMOTION_ROOT: {"result": {"code": 200, "data": {
                "shortURL": "https://u.jd.com/fallback_ok",
                "clickURL": "https://union-click.jd.com/fallback_ok",
                "jCommand": "兜底口令",
                "jShortCommand": "兜底短口令",
            }}}
        })),
    ]
    http = SimpleNamespace(get=Mock(side_effect=replies))
    real_client = JdClient("ztk-key", "10001", http_session=http)
    live_adapter = JdAdapter(SimpleNamespace(jd_enabled=True, jd_position_id="88"), client=real_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("services.promote_service.get_adapter", lambda plat: live_adapter)
        promo_svc = PromoteService(product_repo=repo)
        conv = promo_svc.convert_link("jd", enc_sign)
        assert conv["status"] == "success"
        assert conv["final_link"] == "https://u.jd.com/fallback_ok"
        assert http.get.call_count == 2
        assert http.get.call_args_list[0].args == (DEFAULT_API_BASE + "/open_jing_union_open_promotion_byunionid_get.ashx",)
        assert http.get.call_args_list[0].kwargs["params"]["couponUrl"] == coupon_link
        assert "couponUrl" not in http.get.call_args_list[1].kwargs["params"]
    clear_all_caches()


def test_unchecked_jd_rank_item_enriches_once_on_convert_and_prefetch_short_circuits():
    from cache import clear_all_caches
    from repositories.product_repo import ProductRepository
    from tests.test_product_repository import MemoryCollection as ProductMemCol

    clear_all_caches()
    enc_sign = "H942Tfk1n9iTlRHE8K75IkKfe6mXtQ_3Mqe07A69i2lWGNpGd"
    jd_img = "https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg"
    coupon_link = "https://coupon.m.jd.com/coupons/show.action?key=k888&roleId=777"

    col = ProductMemCol()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda name: col))

    mock_client = Mock()
    # 模拟 api_detail.ashx 遇 301 限流 -> 交叉探针转链存活 -> 自动走 api_quanwang.ashx 短链兜底拿到隐藏券
    mock_client.get_goods_detail.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR", "ztk_ambiguous_301": True}
    mock_client.convert_promotion_link.side_effect = [
        {"status": "success", "short_url": "https://u.jd.com/probe1"},
        {"status": "success", "promoter_id": "88", "short_url": "https://u.jd.com/two_in_one", "click_url": "", "j_command": "领券口令", "j_short_command": "领券口令"},
    ]
    mock_client.search_goods.return_value = {
        "status": "success",
        "total_count": 1,
        "data": [{
            "tao_id": enc_sign,
            "title": "芬达橙味500ml*24瓶",
            "quanhou_jiage": "39.90",
            "size": "49.90",
            "coupon_info_money": "10",
            "coupon_id": coupon_link,
            "pict_url": jd_img,
            "item_url": f"https://jingfen.jd.com/detail/{enc_sign}.html",
        }],
    }

    jd_adapter = JdAdapter(SimpleNamespace(jd_enabled=True, jd_position_id="88"), client=mock_client)
    repo.upsert_home_products([jd_adapter._summary({
        "itemId": enc_sign, "skuName": "芬达橙味500ml*24瓶", "wlprice": "49.90", "imageUrl": jd_img,
    })], channel_type=5)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("services.promote_service.get_adapter", lambda plat: jd_adapter)
        mp.setattr("services.product_service.get_adapter", lambda plat: jd_adapter)
        promo_svc = PromoteService(product_repo=repo)
        res = promo_svc.convert_link("jd", enc_sign)
        assert res["status"] == "success"
        assert res["final_link"] == "https://u.jd.com/two_in_one"
        assert mock_client.convert_promotion_link.call_args_list[-1].kwargs == {"coupon_url": coupon_link}

        # 落库后已具备 detail_updated_at 与 coupon_url，后台预取直接短路不消耗上游配额
        prod_svc = ProductService(product_repo=repo)
        mock_client.get_goods_detail.reset_mock()
        pf = prod_svc.prefetch_product_detail(ProductRef("jd", "6ae3f40fF84b3cfef_0083320320005d73", enc_sign))
        assert pf.status == "ready"
        mock_client.get_goods_detail.assert_not_called()
    clear_all_caches()

