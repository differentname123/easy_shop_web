from unittest.mock import Mock
from types import SimpleNamespace
from adapters.jd_adapter import JdAdapter
from models.product import ProductRef
from services.product_service import ProductService
from cache import clear_all_caches


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


def test_superseded_unsets_goods_sign_and_prevents_inplace_overwrite_or_poisoned_cache(monkeypatch):
    import datetime
    from copy import deepcopy
    from repositories.product_repo import ProductRepository
    from tests.test_product_repository import MemoryCollection

    class _SupersededMemoryCol(MemoryCollection):
        def update_many(self, query, update):
            matched = modified = 0
            for d in self.docs:
                if self.matches(d, query):
                    matched += 1
                    d.update(deepcopy(update.get("$set", {})))
                    for k in update.get("$unset", {}):
                        d.pop(k, None)
                    modified += 1
            return SimpleNamespace(matched_count=matched, modified_count=modified)

    clear_all_caches()
    col = _SupersededMemoryCol()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _n: col))
    old_id = "6aca4d10F381a5657_0944320320d0cf17"
    new_id = "6afa7967F50cc2d4e_0083320320eaa21c"
    shared_sign = "VA02aBx1VAhDTydqHEVYFDkWYwJrd29XEl57X3x_FShUUD5OADxfbyFQA1RlZF5tC"

    col.docs.append({
        "platform": "jd",
        "product_id": old_id,
        "goods_id": old_id,
        "goods_sign": shared_sign,
        "name": "海天厨房调料组合套装",
        "activity_price": 18.9,
        "price": 18.9,
        "original_price": 18.9,
        "default_promotion": {"final_link": "https://u.jd.com/old_seasoning"},
        "default_promotion_expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2),
    })

    # 1. mark_product_superseded 必须同时注销 goods_sign 与历史推广链接缓存
    assert repo.mark_product_superseded("jd", old_id, new_id, shared_sign) is True
    retired_doc = next(d for d in col.docs if d["product_id"] == old_id)
    assert retired_doc["superseded_by"] == new_id
    assert retired_doc["is_off_promotion"] is True
    assert "goods_sign" not in retired_doc
    assert "default_promotion" not in retired_doc

    # 2. 用新主键 + 相同 goods_sign 写入详情时，必须新建文档，绝不原地覆写旧商品文档
    assert repo.upsert_product_detail({
        "platform": "jd",
        "goods_id": new_id,
        "goods_sign": shared_sign,
        "name": "联想ThinkPad笔记本",
        "price": 10775.7,
        "original_price": 11175.7,
        "image_url": f"https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/{new_id.replace('_', '/')}.jpg",
    }) is True
    assert len(col.docs) == 2
    assert retired_doc["name"] == "海天厨房调料组合套装"
    assert retired_doc["superseded_by"] == new_id
    active_new = next(d for d in col.docs if d["product_id"] == new_id)
    assert active_new["name"] == "联想ThinkPad笔记本"

    # 3. 即使模拟历史脏库中旧/退场文档仍残留 goods_sign，save_promotion_link_cache(only_if_exists=True)
    # 与 get_promotion_link_cache 也绝不覆写、绝不误读失效或 superseded 文档的毒化缓存
    col.docs.append({
        "platform": "jd",
        "product_id": "legacy_poison_id",
        "goods_id": "legacy_poison_id",
        "goods_sign": "LEGACY_SIGN",
        "name": "历史损坏商品",
        "activity_price": 9.9,
        "superseded_by": "nonexistent_target",
        "is_off_promotion": False,
        "default_promotion": {"final_link": "https://u.jd.com/poisoned"},
        "default_promotion_expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2),
    })
    assert repo.get_promotion_link_cache("jd", goods_id="legacy_poison_id", goods_sign="LEGACY_SIGN") is None
    repo.save_promotion_link_cache(
        "jd",
        {"final_link": "https://u.jd.com/should_not_attach"},
        goods_id="fresh_other_id",
        goods_sign="LEGACY_SIGN",
        only_if_exists=True,
    )
    poison_doc = next(d for d in col.docs if d.get("goods_sign") == "LEGACY_SIGN")
    assert poison_doc["product_id"] == "legacy_poison_id"
    assert poison_doc["default_promotion"]["final_link"] == "https://u.jd.com/poisoned"

    # 4. ProductService.get_goods_detail 遇到悬空或自指 superseded_by 时，若传入有效 goods_sign，自动回源重建真实商品
    col.docs.append({
        "platform": "jd",
        "product_id": old_id + "_legacy",
        "goods_id": old_id + "_legacy",
        "goods_sign": "G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww",
        "name": "错误覆盖商品",
        "activity_price": 10775.7,
        "superseded_by": "missing_new_target",
        "is_off_promotion": False,
        "detail_updated_at": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5),
    })
    fake_adapter = SimpleNamespace(
        active=True,
        platform_code="jd",
        platform_name="京东",
        capabilities={"detail": True},
        resolve_product_ref=lambda raw="", goods_id=None, goods_sign=None: ProductRef("jd", goods_id or "", goods_sign or raw),
        get_goods_detail=lambda *a, **kw: {
            "status": "success",
            "data": {
                "platform": "jd",
                "goods_id": old_id + "_legacy",
                "goods_sign": "G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww",
                "name": "海天厨房调料组合套装12件套",
                "price": 18.9,
                "original_price": 18.9,
                "image_url": "https://img14.360buyimg.com/pop/jfs/t20271010/538421/18/9854/219374/6aca4d10F381a5657/0944320320d0cf17.jpg",
                "carousel_images": ["https://img14.360buyimg.com/pop/jfs/t20271010/538421/18/9854/219374/6aca4d10F381a5657/0944320320d0cf17.jpg"],
            },
        },
    )
    monkeypatch.setattr("services.product_service.get_adapter", lambda plat: fake_adapter)
    svc = ProductService(product_repo=repo)
    recovered = svc.get_goods_detail(
        platform="jd",
        goods_id=old_id + "_legacy",
        goods_sign="G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww",
    )
    assert recovered["status"] == "success"
    assert recovered["data"]["name"] == "海天厨房调料组合套装12件套"
    clear_all_caches()


def test_commit_snapshot_order_superseded_cycle_mark_empty_and_resolve_ref_isolation(monkeypatch):
    """验证迁移先写后标、superseded_by 环/自指断路、空新ID拦截与 PromoteService 防缓存投毒。"""
    import datetime
    from cache import DETAIL_CACHE, SIGN_BY_ID_CACHE, make_detail_snapshot
    from repositories.product_repo import ProductRepository
    from services.promote_service import PromoteService
    from tests.test_product_repository import MemoryCollection

    clear_all_caches()
    col = MemoryCollection()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _n: col))

    # 1. mark_product_superseded 空 new_goods_id 绝不落库标记 superseded_by=""
    col.docs.append({"platform": "jd", "product_id": "OLD_1", "goods_sign": "OLD_SIGN"})
    assert repo.mark_product_superseded("jd", "OLD_1", "", "OLD_SIGN") is False
    assert "superseded_by" not in col.docs[0]

    # 2. _commit_snapshot: 当 upsert_product_detail 返回 False 或无有效图片时，绝不提前软下架 requested_ref
    svc = ProductService(product_repo=repo)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    snap_ok = make_detail_snapshot({
        "platform": "jd", "goods_id": "NEW_ID", "goods_sign": "NEW_SIGN", "name": "新品",
        "price": 19.9, "image_url": "https://img.jd.com/1.jpg", "carousel_images": ["https://img.jd.com/1.jpg"],
    }, now_dt, "jd")
    mark_calls = []
    monkeypatch.setattr(repo, "mark_product_superseded", lambda *a, **kw: mark_calls.append(a) or True)
    monkeypatch.setattr(repo, "upsert_product_detail", lambda *a, **kw: False)
    svc._commit_snapshot(snap_ok, requested_ref=ProductRef("jd", "OLD_1", "OLD_SIGN"))
    assert mark_calls == []

    snap_no_img = make_detail_snapshot({
        "platform": "jd", "goods_id": "NEW_ID", "goods_sign": "NEW_SIGN", "name": "无图新品", "price": 19.9,
    }, now_dt, "jd")
    monkeypatch.setattr(repo, "upsert_product_detail", lambda *a, **kw: True)
    svc._commit_snapshot(snap_no_img, requested_ref=ProductRef("jd", "OLD_1", "OLD_SIGN"))
    assert mark_calls == []

    # 3. get_goods_detail 遇到 A->B->A 循环与 A->A 自指绝不 RecursionError，安全退化为下架错误
    col.docs = [
        {"platform": "jd", "product_id": "CYC_A", "name": "环A", "price": 9.9, "superseded_by": "CYC_B"},
        {"platform": "jd", "product_id": "CYC_B", "name": "环B", "price": 9.9, "superseded_by": "CYC_A"},
        {"platform": "jd", "product_id": "SELF_A", "name": "自指", "price": 9.9, "superseded_by": "SELF_A"},
    ]
    monkeypatch.setattr("services.product_service.get_adapter", lambda _p: SimpleNamespace(
        active=True, platform_code="jd", platform_name="京东", capabilities={"detail": True},
        resolve_product_ref=lambda raw="", goods_id=None, goods_sign=None: ProductRef("jd", goods_id or raw, goods_sign or ""),
    ))
    cyc_res = svc.get_goods_detail("jd", "CYC_A", goods_id="CYC_A")
    assert cyc_res["status"] == "error" and cyc_res["error_code"] == "PRODUCT_OFF_PROMOTION"
    self_res = svc.get_goods_detail("jd", "SELF_A", goods_id="SELF_A")
    assert self_res["status"] == "error" and self_res["error_code"] == "PRODUCT_OFF_PROMOTION"

    # 4. PromoteService._resolve_product_ref: 传入 goods_sign 时，绝不让未验证客户端 goods_id 写入 SIGN_BY_ID_CACHE，也不跨读 id DETAIL_CACHE
    clear_all_caches()
    DETAIL_CACHE.set(
        ("detail", "pdd", "", "id", "WRONG_CLIENT_ID"),
        (time_future := datetime.datetime.now(datetime.timezone.utc).timestamp() + 3600,
         {"status": "success", "data": {"platform": "pdd", "goods_id": "WRONG_CLIENT_ID", "goods_sign": "OTHER_SIGN", "name": "错品"}}),
    )
    assert time_future > 0
    col.docs = [{"platform": "pdd", "product_id": "SIGN_REAL_1", "goods_sign": "SIGN_REAL_1"}]
    promo_svc = PromoteService(product_repo=repo, user_service=SimpleNamespace())
    resolved = promo_svc._resolve_product_ref(ProductRef("pdd", "WRONG_CLIENT_ID", "SIGN_REAL_1"))
    assert resolved.goods_sign == "SIGN_REAL_1"
    assert resolved.goods_id == ""
    assert SIGN_BY_ID_CACHE.get(("pdd", "WRONG_CLIENT_ID")) is None
    clear_all_caches()



