import pytest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from adapters.jd_adapter import JdAdapter
from models.product import ProductRef, DetailSnapshot
from services.product_service import ProductService
from cache import clear_all_caches, is_product_off_promotion


def test_hdk_commission_normalization():
    settings = SimpleNamespace(
        jd_enabled=True,
        jd_union_id="2035679405",
        jd_position_id="88",
        ztk_appkey="ztk_test",
    )
    adapter = JdAdapter(settings=settings)

    # 场景1：HDK 正常返回 commissionshare 45% 与 commission 4.09 元
    item1 = {
        "skuid": "10001",
        "goodsname": "暖宝宝",
        "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/123.jpg",
        "itemprice": "9.08",
        "itemendprice": "9.08",
        "commissionshare": 45,
        "commission": 4.09,
    }
    s1 = adapter._normalize_hdk_summary(item1)
    assert s1["price"] == 9.08
    assert s1["commission_rate"] == 45.0
    assert s1["commission_amount"] == 4.09

    # 场景2：HDK commissionshare 为 0，但有 commission 4.09 元（反算比例）
    item2 = {
        "skuid": "10001",
        "goodsname": "暖宝宝",
        "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/123.jpg",
        "itemprice": "9.08",
        "itemendprice": "9.08",
        "commissionshare": 0,
        "commission": 4.09,
    }
    s2 = adapter._normalize_hdk_summary(item2)
    assert s2["commission_amount"] == 4.09
    assert round(s2["commission_rate"], 1) == 45.0

    # 场景3：HDK commission 为 0 或缺失，但有 commissionshare 45%（正算金额）
    item3 = {
        "skuid": "10001",
        "goodsname": "暖宝宝",
        "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/123.jpg",
        "itemprice": "9.08",
        "itemendprice": "9.08",
        "commissionshare": 45,
        "commission": 0,
    }
    s3 = adapter._normalize_hdk_summary(item3)
    assert s3["commission_rate"] == 45.0
    assert s3["commission_amount"] == 4.09


def test_mark_product_superseded_in_repo():
    from repositories.product_repo import ProductRepository

    # Mock collection
    mock_res = Mock()
    mock_res.modified_count = 1
    mock_col = Mock()
    mock_col.update_many.return_value = mock_res
    db = SimpleNamespace(get_collection=lambda name: mock_col)
    repo = ProductRepository(db_instance=db)

    success = repo.mark_product_superseded(
        platform="jd",
        old_goods_id="old_id_123",
        new_goods_id="new_id_456",
        old_goods_sign="old_sign_abc",
    )
    assert success is True
    assert mock_col.update_many.called
    filter_arg, update_arg = mock_col.update_many.call_args[0]

    # 验证不过滤新商品，且软下架旧商品并记录替代关系
    assert filter_arg["platform"] == "jd"
    assert filter_arg["product_id"] == {"$ne": "new_id_456"}
    assert update_arg["$set"]["is_off_promotion"] is True
    assert update_arg["$set"]["off_promotion_reason"] == "superseded"
    assert update_arg["$set"]["superseded_by"] == "new_id_456"


def test_product_detail_superseded_transparent_forwarding(monkeypatch):
    clear_all_caches()
    old_id = "old_gid_111"
    new_id = "new_gid_222"

    class FakeAdapter:
        active = True
        platform_code = "jd"
        platform_name = "京东"
        capabilities = {"detail": True}

        def resolve_product_ref(self, raw="", goods_id=None, goods_sign=None):
            gid = goods_id or raw
            return ProductRef("jd", gid, gid)

        def get_goods_detail(self, goods_id_or_sign="", goods_id=None, goods_sign=None, uid=None, **kwargs):
            return {
                "status": "success",
                "data": {
                    "platform": "jd",
                    "goods_id": new_id,
                    "name": "新版暖宝宝",
                    "price": 9.08,
                    "original_price": 9.08,
                    "image_url": "https://img14.360buyimg.com/pop/jfs/t1/new.png",
                }
            }

    repo = Mock()

    # 模拟读取老商品：记录已被 new_id 替代
    def fake_get_detail(key, platform="jd", **kwargs):
        if key == old_id:
            return {
                "platform": "jd",
                "goods_id": old_id,
                "is_off_promotion": True,
                "off_promotion_reason": "superseded",
                "superseded_by": new_id,
            }
        elif key == new_id:
            return {
                "platform": "jd",
                "goods_id": new_id,
                "name": "新版暖宝宝",
                "price": 9.08,
                "original_price": 9.08,
                "image_url": "https://img14.360buyimg.com/pop/jfs/t1/new.png",
                "detail_updated_at": None,
            }
        return None

    repo.get_product_detail.side_effect = fake_get_detail
    repo.get_deal_tip_view.return_value = ("not_found", None)

    monkeypatch.setattr("services.product_service.get_adapter", lambda plat: FakeAdapter())
    svc = ProductService(product_repo=repo)

    # 用户传入旧 ID 请求详情
    result = svc.get_goods_detail(platform="jd", goods_id=old_id)

    # 验证透明转发：成功返回新商品数据，且未返回 PRODUCT_OFF_PROMOTION 报错
    assert result["status"] == "success"
    assert result["data"]["goods_id"] == new_id
    assert result["data"]["name"] == "新版暖宝宝"
    clear_all_caches()


def test_boom_content_filter_superseded_closed():
    from repositories.product_repo import ProductRepository
    f = ProductRepository._boom_content_filter()
    assert f.get("superseded_by") == {"$exists": False}
    assert f.get("is_off_promotion") == {"$ne": True}


def test_meili_format_doc_superseded_flag():
    from infrastructure.search_meili import format_meili_doc
    # 未被替代的正常商品
    doc1 = {"platform": "jd", "product_id": "1001", "name": "商品A", "activity_price": 10.0}
    m1 = format_meili_doc(doc1)
    assert m1["is_off_promotion"] is False

    # 被替代的商品
    doc2 = {
        "platform": "jd",
        "product_id": "1002",
        "name": "商品B",
        "activity_price": 10.0,
        "superseded_by": "1003",
    }
    m2 = format_meili_doc(doc2)
    assert m2["is_off_promotion"] is True


def test_product_repo_build_product_update_symmetric_commission():
    from repositories.product_repo import ProductRepository
    repo = ProductRepository(db_instance=Mock())

    # 1. 只有 commission_amount (2.0)，activity_price 为 10.0 -> 自动补齐 commission_rate = 20.0
    item1 = {
        "platform": "jd",
        "goods_id": "111",
        "activity_price": 10.0,
        "commission_amount": 2.0,
    }
    _, update1 = repo._build_product_update(item1, detail=False)
    assert update1["$set"]["commission_amount"] == 2.0
    assert update1["$set"]["commission_rate"] == 20.0

    # 2. 只有 commission_rate (15.0)，activity_price 为 20.0 -> 自动补齐 commission_amount = 3.0
    item2 = {
        "platform": "jd",
        "goods_id": "222",
        "activity_price": 20.0,
        "commission_rate": 15.0,
    }
    _, update2 = repo._build_product_update(item2, detail=False)
    assert update2["$set"]["commission_rate"] == 15.0
    assert update2["$set"]["commission_amount"] == 3.0


def test_extract_ztk_item_fields_symmetric_commission():
    from infrastructure.http_utils import extract_ztk_item_fields

    # zero_fallback_to_size=False 时，佣金仍可根据 tkrate3 和 price 正确计算
    f1 = extract_ztk_item_fields(
        {
            "tao_id": "123",
            "title": "测试",
            "size": "20.0",
            "quanhou_jiage": "20.0",
            "tkrate3": "25.0",
            "tkfee3": "0",
        },
        zero_fallback_to_size=False,
    )
    assert f1["price"] == 20.0
    assert f1["commission_rate"] == 25.0
    assert f1["commission_amount"] == 5.0

    # 当没有 tkrate3 但有 tkfee3 时，自动反算比例
    f2 = extract_ztk_item_fields(
        {
            "tao_id": "123",
            "title": "测试",
            "size": "50.0",
            "quanhou_jiage": "50.0",
            "tkrate3": "0",
            "tkfee3": "10.0",
        },
        zero_fallback_to_size=False,
    )
    assert f2["commission_amount"] == 10.0
    assert f2["commission_rate"] == 20.0


def test_cache_normalize_platform_sync():
    from cache import SEARCH_CACHE, sync_product_summary_in_caches, clear_all_caches
    clear_all_caches()

    # 预置 "taobao" 的搜索结果列表
    SEARCH_CACHE.set(
        ("taobao", "纸巾", "all", None, 0, 20),
        {
            "status": "success",
            "data": [
                {"platform": "taobao", "goods_id": "tb_999", "price": 19.9, "name": "旧标题"}
            ],
        },
    )

    # 详情页使用缩写 "tb" 回流同步摘要
    sync_product_summary_in_caches(
        platform="tb",
        detail_data={"goods_id": "tb_999", "price": 12.9, "name": "新标题"},
    )

    cached = SEARCH_CACHE.get(("taobao", "纸巾", "all", None, 0, 20))
    assert cached is not None
    updated_item = cached["data"][0]
    assert updated_item["price"] == 12.9
    assert updated_item["name"] == "新标题"
    clear_all_caches()

