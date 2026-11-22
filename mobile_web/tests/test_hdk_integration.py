# -*- coding: utf-8 -*-
"""好单库 (Haodanku) 京东多供应商扩展、契约对齐与主备降级全方位验证测试。"""
from types import SimpleNamespace
from unittest.mock import Mock

from adapters.jd_adapter import JdAdapter
from adapters.tb_adapter import TaobaoAdapter
from infrastructure.hdk_client import HdkClient
from models.product import ProductRef, extract_canonical_goods_id, extract_jd_image_key


def _mock_response(status_code=200, json_data=None):
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = Mock()
    return resp


# ----------------------------------------------------------------------
# 1. HdkClient 独立单元测试：接口调用、参数组装、错误分类与多 Key 故障转移
# ----------------------------------------------------------------------

def test_hdk_search_goods_success_and_pagination():
    session = Mock()
    session.get.return_value = _mock_response(200, {
        "code": 200,
        "msg": "SUCCESS",
        "data": [
            {
                "skuid": "10001",
                "itemid": "IwQNjAAA_test1",
                "goodsname": "京东心相印抽纸",
                "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/test.jpg",
                "itemprice": "39.90",
                "itemendprice": "29.90",
                "couponmoney": "10.00",
                "couponurl": "https://coupon.jd.com/test",
                "commission": "15.00",
                "itemsale": "10000+",
                "shopname": "心相印京东自营旗舰店",
            }
        ]
    })
    client = HdkClient(api_key="test_hdk_key", union_id="2035679405", http_session=session)
    res = client.search_goods("抽纸", page=1, page_size=20)

    assert res["status"] == "success"
    assert len(res["data"]) == 1
    assert res["total_count"] == 1
    assert res["page"] == 1
    assert res["has_more"] is False
    call_args = session.get.call_args
    assert "jd_goods_search" in call_args[0][0]
    assert call_args[1]["params"]["keyword"] == "抽纸"
    assert call_args[1]["params"]["min_id"] == 1
    assert call_args[1]["params"]["back"] == 20
    assert call_args[1]["params"]["union_id"] == "2035679405"
    assert call_args[1]["params"]["apikey"] == "test_hdk_key"


def test_hdk_error_mapping_rate_limit_and_auth():
    session = Mock()
    # 模拟限流报错
    session.get.return_value = _mock_response(200, {"code": 0, "msg": "接口调用过于频繁，频次超限"})
    client = HdkClient(api_key="test_key", union_id="2035679405", http_session=session)
    res_rl = client.search_goods("洗衣液")
    assert res_rl["status"] == "error"
    assert res_rl["error_code"] == "RATE_LIMIT"

    # 模拟未授权报错
    session.get.return_value = _mock_response(200, {"code": 500, "msg": "京东联盟官方接口需授权京东账号后使用"})
    res_auth = client.search_goods("洗衣液")
    assert res_auth["status"] == "error"
    assert res_auth["error_code"] == "UPSTREAM_PROVIDER_UNAUTHORIZED"


def test_hdk_get_goods_detail_exact_id_matching():
    session = Mock()
    session.get.return_value = _mock_response(200, {
        "code": 200,
        "data": [
            {"skuid": "99999", "itemid": "other_item", "goodsname": "其他商品"},
            {"skuid": "10001", "itemid": "target_item", "goodsname": "目标商品", "itemprice": "19.90"},
        ]
    })
    client = HdkClient(api_key="test_key", union_id="2035679405", http_session=session)

    # 精确匹配 skuid
    res1 = client.get_goods_detail("10001")
    assert res1["status"] == "success"
    assert res1["data"]["goodsname"] == "目标商品"

    # 精确匹配 itemid
    res2 = client.get_goods_detail("target_item")
    assert res2["status"] == "success"
    assert res2["data"]["goodsname"] == "目标商品"

    # 匹配不符合返回 PRODUCT_IDENTITY_MISMATCH
    res3 = client.get_goods_detail("non_existent_id")
    assert res3["status"] == "error"
    assert res3["error_code"] == "PRODUCT_IDENTITY_MISMATCH"


def test_hdk_convert_promotion_link_success():
    session = Mock()
    session.post.return_value = _mock_response(200, {
        "code": 200,
        "msg": "SUCCESS",
        "data": {
            "short_url": "https://u.jd.com/ogpcRGH",
            "click_url": "https://union-click.jd.com/jdc?e=...",
        }
    })
    client = HdkClient(api_key="test_key", union_id="2035679405", http_session=session)
    res = client.convert_promotion_link(
        "https://jingfen.jd.com/detail/test_enc.html",
        sub_union_id="sub_123",
        coupon_url="https://coupon.jd.com/test",
    )
    assert res["status"] == "success"
    assert res["short_url"] == "https://u.jd.com/ogpcRGH"
    assert res["final_link"] == "https://u.jd.com/ogpcRGH"
    assert res["union_id"] == "2035679405"
    assert "promoter_id" not in res
    call_kwargs = session.post.call_args[1]["data"]
    assert call_kwargs["sceneId"] == 1
    assert call_kwargs["subUnionId"] == "sub_123"
    assert call_kwargs["coupon_url"] == "https://coupon.jd.com/test"
    assert "positionId" not in call_kwargs


def test_hdk_multi_key_failover():
    call_keys = []

    def _mock_get(url, params=None, **kw):
        key = params.get("apikey")
        call_keys.append(key)
        if key == "key_1":
            return _mock_response(200, {"code": 0, "msg": "频次超限"})
        return _mock_response(200, {"code": 200, "data": [{"skuid": "1", "goodsname": "抽纸"}]})

    session = Mock()
    session.get.side_effect = _mock_get
    client = HdkClient(api_key_list=["key_1", "key_2"], union_id="2035679405", http_session=session)
    res = client.search_goods("抽纸")
    assert res["status"] == "success"
    assert call_keys == ["key_1", "key_2"]
    assert client.api_key == "key_2"


def test_jd_adapter_hdk_multi_key_round_robin():
    """验证 JdAdapter 读取 hdk_api_key_list 并对健康推广转链通道进行多 Key 轮询负载均衡。"""
    settings = SimpleNamespace(
        jd_enabled=True,
        jd_union_id="2035679405",
        jd_position_id="88",
        ztk_appkey="ztk_test",
        hdk_api_key="hdk_k1",
        hdk_api_key_list=("hdk_k1", "hdk_k2", "hdk_k3"),
    )
    adapter = JdAdapter(settings=settings)
    assert adapter.hdk is not None
    assert adapter.hdk.appkey_list == ("hdk_k1", "hdk_k2", "hdk_k3")

    call_keys = []

    def _mock_post(url, params=None, data=None, **kw):
        call_keys.append((data or {}).get("apikey"))
        return _mock_response(200, {"code": 200, "data": {"short_url": "https://u.jd.com/ok"}})

    adapter.client.http.get = Mock(return_value=_mock_response(200, {"status": 301, "content": "ZTK频控"}))
    adapter.hdk.http.post = _mock_post

    enc_id = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    # 连续调用 3 次转链，多 key 均匀轮询分摊配额 (Round Robin)
    adapter.convert_promotion_url(enc_id, promoter_id="88")
    adapter.convert_promotion_url(enc_id, promoter_id="88")
    adapter.convert_promotion_url(enc_id, promoter_id="88")
    assert call_keys == ["hdk_k1", "hdk_k2", "hdk_k3"]


# ----------------------------------------------------------------------
# 2. 契约一致性验证：HDK 归一化输出与 ZTK 归一化输出具备相同核心字段
# ----------------------------------------------------------------------

def test_hdk_and_ztk_data_contract_equivalence():
    """验证 HDK 与 ZTK 产生的数据契约（ProductSummary 与 ProductDetail）结构完全对齐。"""
    settings = SimpleNamespace(
        jd_enabled=True,
        jd_union_id="2035679405",
        jd_position_id="88",
        ztk_appkey="ztk_test",
    )
    adapter = JdAdapter(settings=settings)

    # 1. 测试 ProductSummary 归一化（优先保留官方加密 itemid，规范生成 product_url）
    enc_itemid = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    raw_hdk_item = {
        "skuid": "10001",
        "itemid": enc_itemid,
        "goodsname": "心相印经典茶语丝柔抽纸3层120抽*24包",
        "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/123.jpg",
        "itemprice": "49.90",
        "itemendprice": "39.90",
        "couponmoney": "10.00",
        "couponurl": "https://coupon.jd.com/test_coupon",
        "commission": "10.00",
        "itemsale": "20000+",
        "shopname": "心相印京东自营旗舰店",
        "brand_name": "心相印",
    }
    summary = adapter._normalize_hdk_summary(raw_hdk_item)

    # 核心字段校验
    assert summary["platform"] == "jd"
    assert summary["platform_name"] == "京东"
    assert summary["goods_sign"] == enc_itemid
    assert summary["product_url"] == f"https://jingfen.jd.com/detail/{enc_itemid}.html"
    # goods_id 必须是由主图哈希或加密 ID 生成的确定性主键
    assert summary["goods_id"] == extract_canonical_goods_id("jd", enc_itemid, image_url=raw_hdk_item["itempic"])
    assert summary["price"] == 39.90
    assert summary["original_price"] == 49.90
    assert summary["coupon_amount"] == 10.0
    assert summary["has_coupon"] is True
    assert summary["coupon_url"] == "https://coupon.jd.com/test_coupon"
    assert summary["name"] == raw_hdk_item["goodsname"]
    assert summary["brand"] == "心相印"
    assert summary["mall_name"] == "心相印京东自营旗舰店"

    # 纯数字 SKU 绝不可拼成 jingfen.jd.com/detail/{数字}.html
    numeric_only_summary = adapter._normalize_hdk_summary({
        "skuid": "10001",
        "goodsname": "仅数字SKU商品",
        "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/num.jpg",
        "itemprice": "29.90",
    })
    assert numeric_only_summary["goods_sign"] == "10001"
    assert numeric_only_summary["product_url"] == "https://item.jd.com/10001.html"

    # 2. 测试 ProductDetail 归一化
    raw_hdk_detail_item = {
        **raw_hdk_item,
        "jd_image": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/img1.jpg,https://img14.360buyimg.com/n1/s800x800_jfs/t1/img2.jpg",
        "shop_info": {
            "userEvaluateScore": "9.8",
            "logisticsLvyueScore": "9.9",
            "afterServiceScore": "9.7",
        },
        "quota": "40.00",
        "couponstarttime": 1700000000,
        "couponendtime": 1700100000,
    }
    detail = adapter._normalize_hdk_detail(raw_hdk_detail_item)

    assert detail["platform"] == "jd"
    assert detail["goods_id"] == summary["goods_id"]
    assert detail["goods_sign"] == summary["goods_sign"]
    assert len(detail["carousel_images"]) >= 2
    assert detail["shop_ratings"]["desc_score"] == "9.8"
    assert detail["shop_ratings"]["lgst_score"] == "9.9"
    assert detail["shop_ratings"]["serv_score"] == "9.7"
    assert any("商品品牌" in spec["label"] and spec["value"] == "心相印" for spec in detail["specs"])
    assert detail["coupon_info"]["coupon_amount"] == 10.0
    assert detail["coupon_info"]["quota"] == 40.0


# ----------------------------------------------------------------------
# 3. JdAdapter 多供应商链执行与主备降级集成验证
# ----------------------------------------------------------------------

def test_jd_adapter_search_prefers_hdk_and_fails_over_to_ztk():
    """搜索按 ZTK 全网（含 goods.query）为主通道，ZTK 限流/不可用时无缝降级好单库 hdk_jd_search。"""
    hdk_mock = Mock()
    hdk_mock.search_goods.return_value = {
        "status": "success",
        "data": [
            {
                "itemid": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_hdkSearch123456789",
                "skuid": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_hdkSearch123456789",
                "goodsname": "好单库直连商品",
                "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/hdk_search.jpg",
                "itemprice": "39.90",
                "itemendprice": "29.90",
            }
        ],
        "total_count": 1,
        "has_more": False,
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.search_goods.return_value = {
        "status": "success",
        "data": [
            {
                "tao_id": "enc_2",
                "title": "折淘客直连商品",
                "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/ztk_search.jpg",
                "size": "29.90",
                "quanhou_jiage": "29.90",
            }
        ],
        "total_count": 1,
        "has_more": False,
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.search_goods("抽纸")

    assert res["status"] == "success"
    assert res["_provider"] == "ztk_jd_quanwang"
    assert res["data"][0]["name"] == "折淘客直连商品"
    ztk_client.search_goods.assert_called_once()
    hdk_mock.search_goods.assert_not_called()

    # ZTK 限流时无缝降级好单库搜索
    ztk_client.search_goods.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "ZTK限流",
    }
    res_fb = adapter.search_goods("抽纸")
    assert res_fb["status"] == "success"
    assert res_fb["_provider"] == "hdk_jd_search"
    assert res_fb["data"][0]["name"] == "好单库直连商品"
    hdk_mock.search_goods.assert_called_once()


def test_jd_adapter_detail_prefers_hdk_and_fails_over_to_ztk_without_blind_prefetch():
    """详情按 ZTK 详情为主、好单库 hdk_jd_detail 为备，互不误判下架，数字 SKU 跳过 HDK，并保留 can_prefetch_detail() == False 护栏。"""
    enc_target = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "success",
        "data": {
            "itemid": enc_target,
            "skuid": enc_target,
            "goodsname": "好单库详情商品",
            "itemendprice": "19.90",
            "itemprice": "29.90",
            "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/hdk.jpg",
        },
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [
            {
                "tao_id": enc_target,
                "title": "ZTK直连详情商品",
                "wlprice": "19.90",
                "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/ztk.jpg",
            }
        ],
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    assert adapter.capabilities["detail"] is True
    assert adapter.can_prefetch_detail(ProductRef("jd", "img_1", "ENC_1234567890123456789012345678901234567890")) is False

    # 1. ZTK 主通道命中时直出，不消耗 HDK 额度
    res_ztk = adapter.get_goods_detail(enc_target, goods_sign=enc_target)
    assert res_ztk["status"] == "success"
    assert res_ztk["_provider"] == "ztk_jd_detail"
    assert res_ztk["data"]["name"] == "ZTK直连详情商品"
    ztk_client.get_goods_detail.assert_called_once()
    hdk_mock.get_goods_detail.assert_not_called()

    # 2. ZTK 详情限流时无缝降级到好单库 hdk_jd_detail
    ztk_client.get_goods_detail.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "ZTK详情限流",
    }
    res_hdk = adapter.get_goods_detail(enc_target, goods_sign=enc_target)
    assert res_hdk["status"] == "success"
    assert res_hdk["_provider"] == "hdk_jd_detail"
    assert res_hdk["data"]["name"] == "好单库详情商品"
    hdk_mock.get_goods_detail.assert_called_once_with(enc_target)

    # 3. 纯数字 SKU 跳过 HDK（HDK 暂不支持纯数字查询），直走 ZTK
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [{"tao_id": "10001", "skuId": "10001", "title": "ZTK数字SKU商品", "wlprice": "9.90", "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/num.jpg"}],
    }
    res_num = adapter.get_goods_detail("10001", goods_id="10001")
    assert res_num["status"] == "success"
    assert res_num["_provider"] == "ztk_jd_detail"
    assert all(call.args[0] != "10001" for call in hdk_mock.get_goods_detail.call_args_list)


def test_jd_adapter_promotion_prefers_hdk_as_primary():
    """转链以 ZTK byunionid_get 为主力首选（含完整京口令与归因），失败时无缝降级 HDK。"""
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/ztk_primary",
        "click_url": "https://union-click.jd.com/ztk_primary",
        "j_command": "完整京口令",
        "j_short_command": "短京口令",
    }
    hdk_mock = Mock()
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/hdk_primary",
        "click_url": "https://union-click.jd.com/hdk_primary",
        "final_link": "https://u.jd.com/hdk_primary",
        "promoter_id": "88",
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.convert_promotion_url(
        "enc_item_id_12345678901234567890",
        promoter_id="88",
        trusted_product_url="https://jingfen.jd.com/detail/enc_item_id_12345678901234567890.html",
    )

    assert res["status"] == "success"
    assert res["_provider"] == "ztk_jd_byunionid"
    assert res["final_link"] == "https://u.jd.com/ztk_primary"
    assert res["promotion_token"] == "短京口令"
    ztk_client.convert_promotion_link.assert_called_once()
    hdk_mock.convert_promotion_link.assert_not_called()


def test_jd_adapter_promotion_fails_over_to_ztk_on_hdk_error():
    """ZTK 主力转链遇到限流时，公共请求自动无缝降级到 HDK 转链保底生链。"""
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.convert_promotion_link.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "ZTK转链限流",
    }
    hdk_mock = Mock()
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/hdk_backup",
        "click_url": "https://union-click.jd.com/hdk_backup",
        "final_link": "https://u.jd.com/hdk_backup",
        "promoter_id": "88",
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.convert_promotion_url(
        "enc_item_id_12345678901234567890",
        promoter_id="88",
        trusted_product_url="https://jingfen.jd.com/detail/enc_item_id_12345678901234567890.html",
    )

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_promotion"
    assert res["final_link"] == "https://u.jd.com/hdk_backup"
    ztk_client.convert_promotion_link.assert_called_once()
    hdk_mock.convert_promotion_link.assert_called_once()


def test_jd_adapter_universal_resolve_via_hdk():
    """验证单向组合解析：
    1. 前置 ZTK 未返回数据时，u.jd.com / union-click.jd.com 经 HDK 查卡与转链无缝兜底；
    2. HDK 未索引回退路径：HDK 查无结果后解包拿到加密 sku_id，再经 ZTK 详情反查并由 HDK 转链成功。
    """
    enc_id = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    img_url = "https://img14.360buyimg.com/pop/jfs/t1/556403/7/7738/198408/6ae9a7d7Fb34e56bd/008332032047f665.png"
    hdk_fast = Mock()
    hdk_fast.get_goods_detail.return_value = {
        "status": "success",
        "data": {
            "itemid": enc_id,
            "skuid": enc_id,
            "goodsname": "维达超韧抽纸3层100抽*24包",
            "itempic": img_url,
            "itemprice": "39.90",
            "itemendprice": "29.90",
            "couponmoney": "10.00",
            "couponurl": "https://coupon.jd.com/vinda_coupon",
        },
    }
    hdk_fast.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/vinda_promoted",
        "click_url": "https://union-click.jd.com/vinda_promoted",
        "final_link": "https://u.jd.com/vinda_promoted",
        "union_id": "2035679405",
    }
    ztk_fast = Mock()
    ztk_fast.appkey_list = ("ztk_key",)
    adapter_fast = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_fast,
        hdk_client=hdk_fast,
    )
    for fast_url in ("https://u.jd.com/oFast1", "https://union-click.jd.com/jdc?e=fast2"):
        r_fast = adapter_fast.resolve_and_convert_universal(fast_url)
        assert r_fast["status"] == "success"
        assert r_fast["_provider"] == "hdk_jd_promotion"
        assert r_fast["resolution_kind"] == "product"
        assert r_fast["final_link"] == "https://u.jd.com/vinda_promoted"
        assert r_fast["goods_sign"] == enc_id
        assert r_fast["product"]["name"] == "维达超韧抽纸3层100抽*24包"
        assert r_fast["product"]["price"] == 29.90
        assert r_fast["product"]["coupon_amount"] == 10.0
    ztk_fast.get_goods_detail.assert_not_called()
    ztk_fast.convert_universal_material.assert_not_called()

    # 回退场景：HDK 查卡返回 NOT_FOUND -> 解包 u.jd.com 拿加密 sku -> ZTK 详情兜底 -> HDK 原生转链
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {"status": "error", "error_code": "NOT_FOUND", "not_found": True}
    hdk_mock.resolve_short_url.return_value = {
        "status": "success",
        "data": {
            "dest_url": f"https://pro.m.jd.com/mall/active/abc/index.html?sku={enc_id}",
            "is_venue": False,
            "sku_id": enc_id,
        },
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/vinda_promoted",
        "click_url": "https://union-click.jd.com/vinda_promoted",
        "final_link": "https://u.jd.com/vinda_promoted",
        "union_id": "2035679405",
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [{
            "skuId": "10003",
            "tao_id": enc_id,
            "title": "维达超韧抽纸3层100抽*24包",
            "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/vinda.jpg",
            "size": "39.90",
            "quanhou_jiage": "29.90",
            "coupon_info_money": "10.00",
            "coupon_id": "https://coupon.jd.com/vinda_coupon",
        }],
    }
    ztk_client.convert_universal_material.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "ZTK万能转链限流",
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.resolve_and_convert_universal("https://u.jd.com/oGP72oi")

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_promotion"
    assert res["resolution_kind"] == "product"
    assert res["final_link"] == "https://u.jd.com/vinda_promoted"
    assert res["goods_sign"] == enc_id
    assert res["product"]["name"] == "维达超韧抽纸3层100抽*24包"
    assert res["product"]["price"] == 29.90
    assert res["product"]["coupon_amount"] == 10.0
    ztk_client.get_goods_detail.assert_called_once_with(enc_id)


def test_hdk_agents_md_rule3_compliance():
    """严守 AGENTS.md 第三条铁律：
    1. 严禁指鹿为马：详情或转链反查失败时，严禁截取标题调用搜索接口首条顶替；
    2. 主键解耦：product_id (goods_id) 必须使用静态主图哈希解耦动态 goods_sign，优先保留加密 itemid；
    3. 主图哈希绝不可传给上游详情接口，数字 SKU 绝不拼成 jingfen.jd.com/detail/{数字}.html。
    """
    ztk_mock = Mock()
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", ztk_appkey="ztk_test"),
        client=ztk_mock,
    )

    # 1. 验证静态主图哈希解耦与加密 itemid 优先
    enc_sign = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_3abcde123456789012"
    hdk_raw = {
        "skuid": "10099",
        "itemid": enc_sign,
        "goodsname": "合规测试商品",
        "itempic": "https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg",
        "itemprice": "50.00",
    }
    summary = adapter._normalize_hdk_summary(hdk_raw)
    img_key = extract_jd_image_key("https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg")
    assert img_key == "6ae3f40fF84b3cfef_0083320320005d73"
    assert summary["goods_id"] == img_key
    assert summary["goods_sign"] == enc_sign
    assert summary["product_url"] == f"https://jingfen.jd.com/detail/{enc_sign}.html"

    # 2. 验证主图哈希绝不能作为 target_id 直接调用上游详情接口
    hdk_mock = Mock()
    adapter.attach_hdk(hdk_mock)
    res = adapter.get_goods_detail(img_key, goods_id=img_key)
    assert res["status"] == "error"
    assert res["error_code"] == "INVALID_ARGUMENT"
    assert "不能直接使用主图哈希 ID" in res["error_msg"]
    hdk_mock.get_goods_detail.assert_not_called()
    ztk_mock.get_goods_detail.assert_not_called()


# ----------------------------------------------------------------------
# 4. JD 订单通道保真与 TB 平台好单库按需挂载测试
# ----------------------------------------------------------------------

def test_jd_adapter_orders_preserves_ztk_cas_and_does_not_prepend_hdk():
    """验证 JD 订单同步保留官方折淘客主通道（保障毫秒 modifyTime CAS 精度与完备状态码），attach_hdk 不抢占 orders。"""
    hdk_mock = Mock()
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.query_orders.return_value = {
        "status": "success",
        "order_list": [{"id": "ztk_row_1", "orderId": "p1", "skuId": "2", "skuName": "ZTK商品", "validCode": 18, "estimateFee": 5.0, "actualFee": 5.0, "orderTime": 1700000000, "modifyTime": 1700000100.5}],
        "has_more": False,
        "next_cursor": None,
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", jd_app_key="k", jd_app_secret="s"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.fetch_increment_orders(1700000000, 1700000200, page=1)

    assert res["status"] == "success"
    assert res["_provider"] == "ztk_jd_orders"
    assert res["order_list"][0]["id"] == "ztk_row_1"
    ztk_client.query_orders.assert_called_once()


def test_tb_adapter_promotion_prefers_hdk_and_fails_over_to_ztk():
    """验证淘宝转链：配置了 tb_name 且好单库成功时首选好单库；未配置或未授权时自动平滑降级至折淘客。"""
    hdk_mock = Mock()
    hdk_mock.appkey_list = ("hdk_key",)
    hdk_mock.convert_tb_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://s.click.taobao.com/hdk_tb",
        "click_url": "https://s.click.taobao.com/hdk_tb",
        "final_link": "https://s.click.taobao.com/hdk_tb",
        "tpwd": "￥淘口令HDK￥",
        "taoword": "￥淘口令HDK￥",
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.sid = "192646"

    settings = SimpleNamespace(
        taobao_enabled=True,
        tb_app_key="test_app_key",
        tb_app_secret="test_secret",
        tb_pid="mm_1_2_3",
        tb_channel_pid="mm_1_2_3",
        hdk_api_key="hdk_k",
        hdk_tb_name="已授权淘宝账号",
        ztk_appkey="ztk_k",
        ztk_sid="192646",
    )
    official_client = Mock()
    official_client.get_goods_details.return_value = {
        "status": "success",
        "data": [{"goods_id": "673231015690", "goods_sign": "sign_1", "goods_name": "淘宝精选商品"}],
    }
    tb_adapter = TaobaoAdapter(
        settings=settings,
        official_client=official_client,
        hdk_client=hdk_mock,
        ztk_client=ztk_client,
    )
    res = tb_adapter.convert_promotion_url("673231015690", identity={"relation_id": "12345"}, uid="user_1")

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_tb_promotion"
    assert res["promotion_token"] == "￥淘口令HDK￥"
    assert res["final_link"] == "https://s.click.taobao.com/hdk_tb"
    assert res["relation_id"] == "12345"

    # 模拟好单库提示未授权，验证平滑降级到折淘客
    hdk_mock.convert_tb_promotion_link.return_value = {
        "status": "error",
        "error_code": "AUTH_REQUIRED",
        "error_msg": "您淘宝未授权，请到好单库推广中心-授权管理-进行页面授权！",
    }
    ztk_client.convert_channel_url.return_value = {
        "status": "success",
        "url": "https://s.click.taobao.com/ztk_tb",
        "short_url": "https://s.click.taobao.com/ztk_tb",
        "tpwd": "￥淘口令ZTK￥",
    }
    res_degraded = tb_adapter.convert_promotion_url("673231015690", identity={"relation_id": "12345"}, uid="user_1")
    assert res_degraded["status"] == "success"
    assert res_degraded["_provider"] == "tb_official_promote"
    assert res_degraded["promotion_token"] == "￥淘口令ZTK￥"


def test_jd_adapter_universal_direct_short_link_native_hdk():
    """验证当输入为纯 u.jd.com 短链且无商品卡片时，HDK 原生直接转链成功而不提早降级。"""
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "error",
        "error_code": "NOT_FOUND",
        "not_found": True,
    }
    hdk_mock.resolve_short_url.return_value = {
        "status": "error",
        "error_code": "NOT_SUPPORTED",
        "error_msg": "好单库当前不支持短链反查",
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/direct_hdk_short",
        "click_url": "https://union-click.jd.com/direct_hdk_long",
        "final_link": "https://u.jd.com/direct_hdk_short",
        "promoter_id": "2035679405",
    }
    ztk_client = Mock()
    ztk_client.convert_universal_material.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "折淘客频控限流",
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.resolve_and_convert_universal("https://u.jd.com/o1W3Z7o")

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_universal"
    assert res["final_link"] == "https://u.jd.com/direct_hdk_short"
    assert res["short_url"] == "https://u.jd.com/direct_hdk_short"
    assert res["detail_lookup_failed"] is True
    assert res["resolution_kind"] == "degraded_product"
    assert ztk_client.convert_universal_material.call_count == 1


def test_jd_adapter_universal_venue_shortlink_resolves_cleanly():
    """验证当输入 u.jd.com 短链落地为官方活动/会场时，正向实证为 coupon_or_venue，无需单品卡片且绝不降级。"""
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "error",
        "error_code": "NOT_FOUND",
        "not_found": True,
    }
    hdk_mock.resolve_short_url.return_value = {
        "status": "success",
        "data": {
            "dest_url": "https://pro.m.jd.com/mall/active/4Gks5dMEpjonE5XYzAG1jG2mMM9R/index.html",
            "is_venue": True,
            "sku_id": "",
        },
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/my_venue_short",
        "click_url": "https://union-click.jd.com/my_venue_long",
        "final_link": "https://u.jd.com/my_venue_short",
        "promoter_id": "2035679405",
    }
    ztk_client = Mock()
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.resolve_and_convert_universal("https://u.jd.com/oOW5kMj")

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_promotion"
    assert res["resolution_kind"] == "coupon_or_venue"
    assert res["convert_status"] == "coupon_or_activity"
    assert res["detail_lookup_failed"] is False
    assert res["final_link"] == "https://u.jd.com/my_venue_short"
    # 会场直接正向实证成功，绝不调用折淘客去无谓反查单品详情！
    assert ztk_client.convert_universal_material.call_count == 0


def test_hdk_client_resolve_short_url_mocked():
    """验证 HdkClient.resolve_short_url 的轻量解包逻辑与会场/单品判断。"""
    from infrastructure.hdk_client import HdkClient

    # 1. 模拟落地为大促会场
    sess_mock = Mock()
    resp1 = Mock(status_code=200, text="<script>var hrl='https://u.jd.com/jda?e=123';</script>", url="https://u.jd.com/oOW5kMj")
    resp2 = Mock(status_code=200, url="https://pro.m.jd.com/mall/active/4Gks5dMEpjonE5XYzAG1jG2mMM9R/index.html?unionActId=31255")
    sess_mock.get.side_effect = [resp1, resp2]

    client = HdkClient(api_key="test_key", http_session=sess_mock)
    res = client.resolve_short_url("https://u.jd.com/oOW5kMj")
    assert res["status"] == "success"
    assert res["data"]["is_venue"] is True
    assert res["data"]["sku_id"] == ""

    # 2. 模拟落地为单品
    resp3 = Mock(status_code=200, text="<script>var hrl='https://u.jd.com/jda?e=456';</script>", url="https://u.jd.com/oDZwpf0")
    resp4 = Mock(status_code=200, url="https://item.m.jd.com/product/10190980164446.html?rid=19054")
    sess_mock.get.side_effect = [resp3, resp4]

    res_item = client.resolve_short_url("https://u.jd.com/oDZwpf0")
    assert res_item["status"] == "success"
    assert res_item["data"]["is_venue"] is False
    assert res_item["data"]["sku_id"] == "10190980164446"

    # 3. 模拟通天塔单品模板（携带加密 sku 参数，严禁判为会场）
    resp5 = Mock(status_code=200, text="<script>var hrl='https://u.jd.com/jda?e=789';</script>", url="https://u.jd.com/ogAwTxc")
    resp6 = Mock(status_code=200, url="https://pro.m.jd.com/mall/active/2eh7EHbxrCQQ/index.html?sku=eu8kjNqEBGUN1MbOYF&rid=19054")
    sess_mock.get.side_effect = [resp5, resp6]

    res_babel = client.resolve_short_url("https://u.jd.com/ogAwTxc")
    assert res_babel["status"] == "success"
    assert res_babel["data"]["is_venue"] is False
    assert res_babel["data"]["sku_id"] == "eu8kjNqEBGUN1MbOYF"

    # 4. 模拟防刷 returnurl 落地单品
    resp7 = Mock(status_code=200, text="<script>var hrl='https://u.jd.com/jda?e=999';</script>", url="https://u.jd.com/o6At3T3")
    resp8 = Mock(status_code=200, url="https://cfe.m.jd.com/privatedomain/risk_handler/?returnurl=https%3A%2F%2Fitem.m.jd.com%2Fproduct%2F100160514423.html")
    sess_mock.get.side_effect = [resp7, resp8]

    res_cfe = client.resolve_short_url("https://u.jd.com/o6At3T3")
    assert res_cfe["status"] == "success"
    assert res_cfe["data"]["is_venue"] is False
    assert res_cfe["data"]["sku_id"] == "100160514423"


def test_jd_adapter_babel_product_resolves_to_single_item():
    """验证通天塔单品落地页（携带加密 sku）在适配器中能够成功反查商品详情并标记为 single_item，绝不误杀为会场。"""
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "error",
        "error_code": "NOT_FOUND",
        "not_found": True,
    }
    hdk_mock.resolve_short_url.return_value = {
        "status": "success",
        "data": {
            "dest_url": "https://pro.m.jd.com/mall/active/2eh7EHbxrCQQ/index.html?sku=eu8kjNqEBGUN1MbOYF&rid=19054",
            "is_venue": False,
            "sku_id": "eu8kjNqEBGUN1MbOYF",
        },
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/my_promo_short",
        "click_url": "https://union-click.jd.com/my_promo_long",
        "final_link": "https://u.jd.com/my_promo_short",
        "promoter_id": "2035679405",
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [{
            "skuId": "100249470124",
            "tao_id": "eu8kjNqEBGUN1MbOYF",
            "title": "格力高百奇大礼包316g",
            "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/glico.jpg",
            "size": "29.90",
            "quanhou_jiage": "22.90",
            "coupon_info_money": "7.00",
        }],
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.resolve_and_convert_universal("https://u.jd.com/ogAwTxc")

    assert res["status"] == "success"
    assert res["resolution_kind"] == "product"
    assert res["goods_id"] == "eu8kjNqEBGUN1MbOYF"
    assert res["goods_sign"] == "eu8kjNqEBGUN1MbOYF"
    assert res["product"]["product_url"] == "https://item.jd.com/100249470124.html"
    assert res["title"] == "格力高百奇大礼包316g"
    assert res["price"] == 22.9
    assert res["final_link"] == "https://u.jd.com/my_promo_short"
    assert res.get("convert_status") is None  # 不是会场，单品正常流转！


def test_jd_adapter_numeric_sku_from_shortlink_preserves_shorturl_and_skips_detail_waste():
    """验证当 u.jd.com 解包落地为纯数字 SKU 时：
    1. 绝不拿纯数字 SKU 去冲击好单库/折淘客详情接口消耗配额（仅允许先用原 u.jd.com 短链查好单库）；
    2. 保留原始 u.jd.com 短链用于转链，不覆盖为必定报错的纯数字 SKU。
    """
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "error",
        "error_code": "NOT_FOUND",
        "not_found": True,
    }
    hdk_mock.resolve_short_url.return_value = {
        "status": "success",
        "data": {
            "dest_url": "https://item.m.jd.com/product/100293139120.html",
            "is_venue": False,
            "sku_id": "100293139120",
        },
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/hdk_short_ok",
        "click_url": "https://union-click.jd.com/hdk_long_ok",
        "final_link": "https://u.jd.com/hdk_short_ok",
        "promoter_id": "2035679405",
    }
    ztk_client = Mock()
    ztk_client.convert_universal_material.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "ztk limit",
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.resolve_and_convert_universal("https://u.jd.com/462xWY1")

    assert res["status"] == "success"
    # 严禁拿纯数字 SKU 100293139120 去白打详情接口（仅允许传入原短链查 HDK）
    assert [c.args[0] for c in hdk_mock.get_goods_detail.call_args_list] == ["https://u.jd.com/462xWY1"]
    ztk_client.get_goods_detail.assert_not_called()
    # 必须传原始短链给转链接口，而非纯数字 SKU
    hdk_mock.convert_promotion_link.assert_called_once()
    assert hdk_mock.convert_promotion_link.call_args.args[0] == "https://u.jd.com/462xWY1"
    assert res["resolution_kind"] == "degraded_product"
    assert res["detail_lookup_failed"] is True


def test_jd_adapter_coupon_url_with_to_item_resolves_as_venue_via_hdk():
    """验证官方领券链接 coupon.m.jd.com 即使尾部携带 to=item.jd.com/数字.html，也应直接由 HDK 正向转链并判为 coupon_or_venue，不被 ZTK 误杀。"""
    coupon_url = "https://coupon.m.jd.com/coupons/show.action?key=f0f3eaf97a6d46a78346e7edb0b1d6b4&roleId=3485005903&to=item.jd.com/10072190437649.html"
    hdk_mock = Mock()
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/oaEmfdS",
        "click_url": "https://union-click.jd.com/oaEmfdS",
        "final_link": "https://u.jd.com/oaEmfdS",
        "promoter_id": "2035679405",
    }
    ztk_client = Mock()
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.resolve_and_convert_universal(coupon_url)

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_promotion"
    assert res["resolution_kind"] == "coupon_or_venue"
    assert res["convert_status"] == "coupon_or_activity"
    assert res["final_link"] == "https://u.jd.com/oaEmfdS"
    hdk_mock.convert_promotion_link.assert_called_once()
    assert hdk_mock.convert_promotion_link.call_args.args[0] == coupon_url
    assert ztk_client.convert_universal_material.call_count == 0


# ----------------------------------------------------------------------
# 5. P0 安全、个人归因与容错回归测试
# ----------------------------------------------------------------------

def test_jd_personal_promoter_skips_hdk_without_health_errors_and_preserves_position_id():
    """JD 个人推手转链与万能转链必须跳过不传输 positionId 的 HDK，且不记入错误指标，由折淘客完整透传 positionId。"""
    from infrastructure.api_governance import GLOBAL_UPSTREAM_HEALTH

    GLOBAL_UPSTREAM_HEALTH.reset()
    hdk_mock = Mock()
    enc_id = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    hdk_mock.get_goods_detail.return_value = {
        "status": "success",
        "data": {
            "itemid": enc_id,
            "skuid": "10001",
            "goodsname": "个人归因测试商品",
            "itemendprice": "19.90",
            "itemprice": "29.90",
            "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/p.jpg",
        },
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)

    ztk_client.convert_promotion_link.return_value = {
        "status": "success",
        "promoter_id": "999999",
        "short_url": "https://u.jd.com/personal_ok",
        "click_url": "https://union-click.jd.com/personal_ok",
        "j_command": "！个人专属京口令！",
    }
    ztk_client.convert_universal_material.return_value = {
        "status": "success",
        "promoter_id": "999999",
        "short_url": "https://u.jd.com/personal_univ",
        "click_url": "https://union-click.jd.com/personal_univ",
        "j_command": "！个人万能京口令！",
        "tao_id": enc_id,
    }
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [{
            "tao_id": enc_id,
            "title": "个人归因测试商品",
            "quanhou_jiage": "19.90",
            "size": "29.90",
            "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/p.jpg",
            "item_url": f"https://jingfen.jd.com/detail/{enc_id}.html",
        }],
    }

    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )

    res_promo = adapter.convert_promotion_url(
        enc_id,
        uid="u_12345678",
        promoter_id="999999",
        trusted_product_url=f"https://jingfen.jd.com/detail/{enc_id}.html",
    )
    assert res_promo["status"] == "success"
    assert res_promo["_provider"] == "ztk_jd_byunionid"
    assert res_promo["promoter_id"] == "999999"
    assert res_promo["attribution_status"] == "verified"

    res_univ = adapter.resolve_and_convert_universal("https://u.jd.com/someone1", uid="u_12345678", promoter_id="999999")
    assert res_univ["status"] == "success"
    assert res_univ["_provider"] == "ztk_jd_universal"
    assert res_univ["promoter_id"] == "999999"

    hdk_mock.convert_promotion_link.assert_not_called()
    hdk_mock.resolve_short_url.assert_not_called()
    ztk_client.convert_promotion_link.assert_called_once()
    assert ztk_client.convert_promotion_link.call_args.args[1] == "999999"
    ztk_client.convert_universal_material.assert_called_once()
    assert ztk_client.convert_universal_material.call_args.args[1] == "999999"

    snap = GLOBAL_UPSTREAM_HEALTH.snapshot_local()
    promo_or_univ_hdk = [
        b for b in snap["endpoints"]
        if "hdk" in b["provider"] and b["operation"] in ("promotion", "universal")
    ]
    assert promo_or_univ_hdk == []
    assert all(b["error_calls"] == 0 for b in snap["endpoints"] if "hdk" in b["provider"])


def test_hdk_resolve_short_url_ssrf_guard():
    """HdkClient.resolve_short_url 必须严格校验入口域名及内嵌 var hrl 目标，阻断 SSRF。"""
    sess = Mock()
    client = HdkClient(api_key="k", http_session=sess)

    # 非 u.jd.com Host（查询串伪装）应直接拦截
    res_evil_input = client.resolve_short_url("https://evil.com/?redirect=u.jd.com/abc")
    assert res_evil_input["status"] == "error"
    assert res_evil_input["error_code"] == "NOT_SUPPORTED"
    sess.get.assert_not_called()

    # var hrl 指向内网或非官方域名时，绝不发起第二次 GET
    sess.get.return_value = Mock(status_code=200, text="<script>var hrl='http://127.0.0.1:8001/admin';</script>", url="https://u.jd.com/bad1")
    res_ssrf = client.resolve_short_url("https://u.jd.com/bad1")
    assert res_ssrf["status"] == "error"
    assert res_ssrf["error_code"] == "UNSAFE_REDIRECT"
    assert sess.get.call_count == 1


def test_tb_adapter_without_hdk_tb_name_skips_hdk_cleanly():
    """未配置 hdk_tb_name 时，TaobaoAdapter.attach_hdk 不注册 hdk_tb_promotion / hdk_tb_orders，无红灯告警。"""
    from infrastructure.api_governance import GLOBAL_UPSTREAM_HEALTH

    GLOBAL_UPSTREAM_HEALTH.reset()
    hdk_mock = Mock()
    hdk_mock.appkey_list = ("hdk_k",)
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_k",)
    ztk_client.sid = "192646"
    ztk_client.convert_channel_url.return_value = {
        "status": "success",
        "url": "https://s.click.taobao.com/ztk_direct",
        "short_url": "https://s.click.taobao.com/ztk_direct",
        "tpwd": "￥ZTK直连口令￥",
    }
    official_client = Mock()
    official_client.pid_prefix = "mm_1_2"
    official_client.get_goods_details.return_value = {
        "status": "success",
        "data": [{"goods_id": "673231015690", "goods_sign": "sign_1", "goods_name": "淘宝测试商品"}],
    }

    adapter = TaobaoAdapter(
        settings=SimpleNamespace(
            taobao_enabled=True,
            tb_app_key="k",
            tb_app_secret="s",
            tb_pid="mm_1_2_3",
            tb_channel_pid="mm_1_2_3",
            hdk_api_key="hdk_k",
            hdk_tb_name="",
            ztk_appkey="ztk_k",
            ztk_sid="192646",
        ),
        official_client=official_client,
        hdk_client=hdk_mock,
        ztk_client=ztk_client,
    )
    res = adapter.convert_promotion_url("673231015690", identity={"relation_id": "12345"}, uid="u_1")
    assert res["status"] == "success"
    assert res["_provider"] == "tb_official_promote"
    hdk_mock.convert_tb_promotion_link.assert_not_called()

    ztk_client.query_orders.return_value = {"status": "success", "order_list": [], "has_more": False, "next_cursor": None}
    ord_res = adapter.fetch_increment_orders(1700000000, 1700000600)
    assert ord_res["status"] == "success"
    assert ord_res["_provider"] == "ztk_tb_orders"

    snap = GLOBAL_UPSTREAM_HEALTH.snapshot_local()
    assert [b for b in snap["endpoints"] if "hdk" in b["provider"]] == []


def test_execute_with_key_failover_retries_on_timeout_and_quota_backlog():
    """多 Key 容灾执行器遇到 TIMEOUT 与 QUOTA_PACE_BACKLOG 时自动切换下一把健康 Key 并迁移活跃游标。"""
    from infrastructure.http_utils import execute_with_key_failover

    client = SimpleNamespace(appkey_list=("k1", "k2", "k3"), appkey="k1", _active_key_index=0)
    attempts = []

    def action(key):
        attempts.append(key)
        if key == "k1":
            return {"status": "error", "error_code": "TIMEOUT", "error_msg": "read timeout"}
        if key == "k2":
            return {"status": "error", "error_code": "QUOTA_PACE_BACKLOG", "error_msg": "pool busy"}
        return {"status": "success", "used_key": key}

    res = execute_with_key_failover(client, action)
    assert res == {"status": "success", "used_key": "k3"}
    assert attempts == ["k1", "k2", "k3"]
    assert client.appkey == "k3"
    assert client._active_key_index == 2


def test_removed_hardcoded_inviter_code_returns_config_error():
    """未配置 taobao_inviter_code 且上游未返回邀请码时，官方与折淘客客户端均返回明确 CONFIG_ERROR，不再使用个人硬编码。"""
    from infrastructure import tb_client
    from infrastructure.ztk_client import ZtkClient

    tb_res = tb_client.generate_tb_authority_url(
        "k", "s", "u_12345678", session_key="sess",
        _api_call=lambda **_kw: {"status": "error", "error_code": "27", "error_msg": "expired"},
    )
    assert tb_res["status"] == "error"
    assert tb_res["error_code"] == "CONFIG_ERROR"

    ztk_http = SimpleNamespace(get=Mock(return_value=_mock_response(200, {"error_response": {"code": 27}})))
    ztk = ZtkClient("ztk_k", "ztk_sid", http_session=ztk_http)
    ztk_res = ztk.generate_authority_url("u_12345678")
    assert ztk_res["status"] == "error"
    assert ztk_res["error_code"] == "CONFIG_ERROR"


def test_hdk_jd_universal_skips_unsupported_input_and_resolves_union_click_natively():
    """验证：
    1. union-click.jd.com 推广链直接由 hdk_jd_universal 原生查卡+转链完成，零 ZTK 配额消耗、零错误指标；
    2. HDK 不识别的京口令等文本返回 skipped（零错误指标），无缝顺延由 ztk_jd_universal 处理。
    """
    from infrastructure.api_governance import GLOBAL_UPSTREAM_HEALTH

    GLOBAL_UPSTREAM_HEALTH.reset()
    enc_id = "enc_id_12345678901234567890_123456"
    hdk_mock = Mock()
    hdk_mock.appkey_list = ("hdk_k",)
    hdk_mock.get_goods_detail.return_value = {
        "status": "success",
        "data": {
            "itemid": enc_id,
            "skuid": enc_id,
            "goodsname": "京东测试商品",
            "itemendprice": "19.9",
            "itemprice": "29.9",
            "itempic": "https://img14.360buyimg.com/p.jpg",
        },
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/hdk_uc_ok",
        "click_url": "https://union-click.jd.com/hdk_uc_ok",
        "final_link": "https://u.jd.com/hdk_uc_ok",
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_k",)
    ztk_client.convert_universal_material.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/ok",
        "click_url": "https://union-click.jd.com/ok",
        "j_command": "！OK！",
        "tao_id": enc_id,
    }
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [{
            "tao_id": enc_id,
            "title": "京东测试商品",
            "quanhou_jiage": "19.9",
            "size": "29.9",
            "pict_url": "https://img14.360buyimg.com/p.jpg",
            "item_url": f"https://jingfen.jd.com/detail/{enc_id}.html",
        }],
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res_uc = adapter.resolve_and_convert_universal("https://union-click.jd.com/jdc?e=&p=JF8B_TEST")
    assert res_uc["status"] == "success"
    assert res_uc["_provider"] == "hdk_jd_promotion"
    assert res_uc["final_link"] == "https://u.jd.com/hdk_uc_ok"
    ztk_client.convert_universal_material.assert_not_called()

    # 不识别的裸京口令应跳过 HDK 且不记录 error，转由 ZTK 处理
    res_tkl = adapter.resolve_and_convert_universal("！测试京口令abc！")
    assert res_tkl["status"] == "success"
    assert res_tkl["_provider"] == "ztk_jd_universal"
    snap = GLOBAL_UPSTREAM_HEALTH.snapshot_local()
    assert not any(
        b["provider"] == "hdk_jd_universal" and b["error_calls"] > 0
        for b in snap["endpoints"]
    )


def test_hdk_shop_homepage_venue_and_off_promotion_short_circuit():
    """验证 shop.m.jd.com/member/home 解包判为纯会场且零 ZTK 配额消耗，及 hdk_jd_universal 遇 PRODUCT_OFF_PROMOTION 立即短路。"""
    sess = Mock()
    sess.get.side_effect = [
        Mock(status_code=200, text="<script>var hrl='https://u.jd.com/jda?e=1';</script>", url="https://u.jd.com/orXLLP5"),
        Mock(status_code=200, url="https://shop.m.jd.com/member/home?shopId=10001"),
    ]
    hdk_cli = HdkClient(api_key="test_k", http_session=sess)
    shop_res = hdk_cli.resolve_short_url("https://u.jd.com/orXLLP5")
    assert shop_res["status"] == "success"
    assert shop_res["data"]["is_venue"] is True
    assert shop_res["data"]["sku_id"] == ""

    enc_id = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    ztk_mock = Mock()
    ztk_mock.appkey_list = ("ztk_k",)
    ztk_mock.get_goods_detail.return_value = {"status": "error", "error_code": "UPSTREAM_ERROR", "ztk_ambiguous_301": True}
    ztk_mock.convert_promotion_link.return_value = {"status": "error", "jd_off_promotion": True, "error_msg": "不在推广中"}
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {"status": "error", "error_code": "NOT_FOUND", "not_found": True}
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_mock,
        hdk_client=hdk_mock,
    )
    off_res = adapter.resolve_and_convert_universal(f"https://jingfen.jd.com/detail/{enc_id}.html")
    assert off_res["status"] == "error"
    assert off_res["error_code"] == "PRODUCT_OFF_PROMOTION"
    hdk_mock.convert_promotion_link.assert_not_called()
    ztk_mock.convert_universal_material.assert_not_called()


def test_hdk_discount_coupon_and_html_unescape_normalization():
    """验证 HDK 真实响应中的折扣小数 couponmoney(0.95/0.8) 纠偏及标题/店铺 HTML 实体还原。"""
    enc_id = "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g"
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=Mock(),
    )

    # 1. 折扣券实惠场景：itemprice=1207, itemendprice=1146.65, couponmoney=0.95 -> 实省 60.35 元 + HTML 实体还原
    s1 = adapter._normalize_hdk_summary({
        "itemid": enc_id,
        "goodsname": "Move Free &quot;益节&quot; 氨糖软骨素 &amp; 钙片",
        "shopname": "益节&amp;海外官方旗舰店",
        "brand_name": "Move&amp;Free",
        "itemprice": 1207,
        "itemendprice": 1146.65,
        "couponmoney": 0.95,
        "couponurl": "https://coupon.m.jd.com/coupons/show.action?linkKey=abc",
        "itempic": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/a.jpg",
    })
    assert s1["coupon_amount"] == 60.35
    assert s1["has_coupon"] is True
    assert s1["name"] == 'Move Free "益节" 氨糖软骨素 & 钙片'
    assert s1["mall_name"] == "益节&海外官方旗舰店"
    assert s1["brand"] == "Move&Free"

    # 2. 满减/门槛未抵扣时的 0.8 折标志：original == price -> 优惠额应归 0
    s2 = adapter._normalize_hdk_summary({
        "itemid": enc_id,
        "goodsname": "满件8折酸奶",
        "itemprice": 19.9,
        "itemendprice": 19.9,
        "couponmoney": 0.8,
        "couponurl": "https://coupon.m.jd.com/coupons/show.action?linkKey=xyz",
        "itempic": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/b.jpg",
    })
    assert s2["coupon_amount"] == 0.0
    assert s2["has_coupon"] is False

    # 3. 真实小额现金券（original=10.0, price=9.5, couponmoney=0.5）完整保留 0.5 元
    s3 = adapter._normalize_hdk_summary({
        "itemid": enc_id,
        "goodsname": "小额立减券商品",
        "itemprice": 10.0,
        "itemendprice": 9.5,
        "couponmoney": 0.5,
        "couponurl": "https://coupon.m.jd.com/coupons/show.action?linkKey=small",
        "itempic": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/c.jpg",
    })
    assert s3["coupon_amount"] == 0.5
    assert s3["has_coupon"] is True


def test_resolve_jd_short_url_standalone_and_without_hdk(monkeypatch):
    import requests
    from infrastructure.jd_client import resolve_jd_short_url

    # 1. Standalone resolve_jd_short_url enforces SSRF guards without HdkClient
    sess = Mock()
    assert resolve_jd_short_url("https://evil.com/x", http_session=sess)["error_code"] == "NOT_SUPPORTED"
    sess.get.assert_not_called()

    sess.get.return_value = Mock(
        status_code=200,
        text="<script>var hrl='http://169.254.169.254/latest/meta-data';</script>",
        url="https://u.jd.com/bad",
    )
    ssrf_res = resolve_jd_short_url("https://u.jd.com/bad", http_session=sess)
    assert ssrf_res["error_code"] == "UNSAFE_REDIRECT"
    assert sess.get.call_count == 1

    # 2. JdAdapter without HDK passes standalone short_url_resolver when HTTP session is real
    monkeypatch.setattr("adapters.jd_adapter.is_real_requests_session", lambda s: isinstance(s, requests.Session))
    real_sess = requests.Session()
    monkeypatch.setattr(real_sess, "get", Mock(return_value=Mock(status_code=200, url="https://u.jd.com/venue1", text="")))
    try:
        ztk_mock = Mock()
        ztk_mock.http = real_sess
        ztk_mock.appkey_list = ("ztk-k1",)
        ztk_mock.is_catalog_cooling.return_value = False
        ztk_mock.query_union_goods.return_value = {"status": "error", "error_code": "NOT_FOUND"}
        ztk_mock.convert_universal_material.return_value = {
            "status": "success",
            "short_url": "https://u.jd.com/out1",
            "click_url": "https://u.jd.com/out1",
            "resolved_venue": True,
        }
        adapter = JdAdapter(
            settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
            client=ztk_mock,
            hdk_client=None,
        )
        res = adapter.resolve_and_convert_universal("https://u.jd.com/venue1")
        assert res["status"] == "success"
        resolver_fn = ztk_mock.convert_universal_material.call_args.kwargs.get("short_url_resolver")
        assert callable(resolver_fn)
        assert resolver_fn("https://evil.com/x")["error_code"] == "NOT_SUPPORTED"
    finally:
        real_sess.close()


def test_hdk_jd_hot_rank_scale_533_cooldown_and_secret_redaction():
    """验证 HDK 京东热榜佣金 1% 比例不放大 100 倍、listId=533 跨 Key 冷却短路及异常隐藏 apikey。"""
    from infrastructure.api_governance import GLOBAL_PROVIDER_COOLDOWN
    from infrastructure.hdk_client import HDK_V2_API_BASE

    GLOBAL_PROVIDER_COOLDOWN.reset()
    rank_item = {
        "itemid": "IwQNjSHMMam3HPxmHnuv3HPxmHnuvQ_cIOf4YLZawohplFr3g",
        "skuid": "10099",
        "goodsname": "好单库热榜抽纸",
        "itempic": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/hot.jpg",
        "itemprice": "9.90",
        "itemendprice": "5.99",
        "couponmoney": "3.91",
        "commissionshare": "1.00",
        "commission": "0.06",
    }
    sess = Mock()
    sess.get.return_value = _mock_response(200, {"code": 200, "data": [rank_item], "min_id": 2})
    hdk_cli = HdkClient(api_key="hdk_secret_k1", api_key_list=("hdk_secret_k1", "hdk_secret_k2"),
                        union_id="2035679405", http_session=sess)
    hot = hdk_cli.get_jd_hot_rank(page=1, page_size=10)
    assert hot["status"] == "success"
    assert sess.get.call_args.args[0] == f"{HDK_V2_API_BASE}/jd_hot_rank"

    a = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
                  client=Mock(appkey_list=()), hdk_client=hdk_cli)
    norm = a._normalize_hdk_summary(hot["data"][0])
    assert norm["commission_rate"] == 1.0  # 严禁错放大成 100.0
    assert norm["commission_amount"] == 0.06

    # 测试 listId=533 无权限报错时 deterministic_all_keys 立即停机（只请求 1 把 key，不盲轮询后续 key）
    sess_blk = Mock()
    sess_blk.get.return_value = _mock_response(200, {"code": 0, "msg": "listId=533 您暂无权限访问京东超级搜索"})
    blk_cli = HdkClient(api_key="hdk_blk_1", api_key_list=("hdk_blk_1", "hdk_blk_2"),
                        union_id="2035679405", http_session=sess_blk)
    blk_res = blk_cli.search_goods("抽纸")
    assert blk_res["status"] == "error"
    assert blk_res.get("deterministic_all_keys") is True
    assert sess_blk.get.call_count == 1

    # 测试网络异常报错绝不泄露 apikey 与完整 URL
    sess_err = Mock()
    sess_err.get.side_effect = RuntimeError("GET https://v2.api.haodanku.com/jd_goods_search?apikey=hdk_secret_k1 failed")
    err_cli = HdkClient(api_key="hdk_secret_k1", union_id="2035679405", http_session=sess_err)
    err_res = err_cli.search_goods("抽纸")
    assert err_res["status"] == "error"
    assert "hdk_secret_k1" not in err_res["error_msg"]
    assert "apikey=" not in err_res["error_msg"]


def test_hdk_sku_mismatch_url_guard_tb_paging_transient_keyword_and_jd_sub_item_id():
    """验证 HDK expected_sku 防串货、转链 URL 协议校验、淘宝分页离散 back、请求异常不停机故障转移与好单库线报仅透传官方 JD 密文。"""
    from unittest.mock import patch
    from services.deal_sources.base import ChannelConfig, FetchContext
    from services.deal_sources.haodanku import HaodankuSource

    # 1. allow_single_url_match 下 expected_sku 不匹配拒绝放行 (PRODUCT_IDENTITY_MISMATCH)
    sess_sku = Mock()
    sess_sku.get.return_value = _mock_response(200, {
        "code": 200,
        "data": [{
            "itemid": "IwQNj_enc",
            "skuid": "1000111",
            "goodsname": "错配商品",
            "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/test.jpg",
            "itemprice": "29.90",
            "itemendprice": "19.90",
        }],
    })
    hdk_sku = HdkClient(api_key="hdk_k1", union_id="2035679405", http_session=sess_sku)
    sku_res = hdk_sku.get_goods_detail("https://u.jd.com/abc", allow_single_url_match=True, expected_sku="1000999")
    assert sku_res["status"] == "error" and sku_res["error_code"] == "PRODUCT_IDENTITY_MISMATCH"

    # 2. 京东/淘宝转链返回非法非 HTTP URL 时返回 PROTOCOL_ERROR
    sess_url = Mock()
    sess_url.post.side_effect = [
        _mock_response(200, {"code": 200, "data": {"short_url": "javascript:alert(1)", "click_url": ""}}),
        _mock_response(200, {"code": 200, "data": {"coupon_click_url": "ftp://evil.example/x", "item_url": ""}}),
    ]
    hdk_url = HdkClient(api_key="hdk_k1", union_id="2035679405", http_session=sess_url)
    bad_jd = hdk_url.convert_promotion_link("https://item.jd.com/10001.html")
    assert bad_jd["status"] == "error" and bad_jd["error_code"] == "PROTOCOL_ERROR"
    bad_tb = hdk_url.convert_tb_promotion_link("673231015690", pid="mm_1_2_3", tb_name="shop")
    assert bad_tb["status"] == "error" and bad_tb["error_code"] == "PROTOCOL_ERROR"

    # 3. search_tb_goods 发送 tb_p=cur_page 与离散 back (15->20)
    sess_tb = Mock()
    sess_tb.get.return_value = _mock_response(200, {"code": 200, "data": []})
    hdk_tb = HdkClient(api_key="hdk_k1", union_id="2035679405", http_session=sess_tb)
    tb_res = hdk_tb.search_tb_goods("抽纸", page=2, page_size=15)
    assert tb_res["status"] == "success"
    tb_params = sess_tb.get.call_args.kwargs["params"]
    assert tb_params["tb_p"] == 2 and tb_params["min_id"] == 2 and tb_params["back"] == 20

    # 4. “请求异常”不在 HDK_BLOCKED_KEYWORDS 中，多 Key 失败能故障转移到第二把 Key
    sess_fo = Mock()
    sess_fo.get.side_effect = [
        _mock_response(200, {"code": 0, "msg": "请求异常，请重试"}),
        _mock_response(200, {"code": 200, "data": [{"skuid": "10001", "itemid": "IwQNj_enc", "goodsname": "抽纸", "itempic": "https://img/a.jpg", "itemprice": "9.9"}]}),
    ]
    hdk_fo = HdkClient(api_key="hdk_k1", api_key_list=("hdk_k1", "hdk_k2"), union_id="2035679405", http_session=sess_fo)
    fo_res = hdk_fo.search_goods("抽纸")
    assert fo_res["status"] == "success" and sess_fo.get.call_count == 2

    # 5. HaodankuSource.fetch 对 JD sub_item_id 仅在满足 is_encrypted_jd_item_id 时生成 encrypted_id 链接
    official_sign = "G6QDzEKCKU5wE4ebWFK3wE4ebcI4vQ_3lzC2xxOIoDavPIXww"
    hdk_priv_sign = "VA02aBx1VAhDTydqHEVYFDkWYwJrd29XEl57X3x_FShUUD5OADxfbyFQA1RlZF5tC"
    src_sess = Mock()
    src_sess.get.return_value = _mock_response(200, {
        "code": 200,
        "data": [
            {
                "wire_id": "w_priv",
                "platform": "2",
                "starttime": 1700000000,
                "list": [{"content": "海天厨房调料组合12件套好价直降18.9元 https://u.jd.com/oklink", "item_id": hdk_priv_sign}],
            },
            {
                "wire_id": "w_off",
                "platform": "2",
                "starttime": 1700000001,
                "list": [{"content": "海天厨房调料组合12件套官方直降18.9元", "item_id": official_sign}],
            },
        ],
    })
    ctx = FetchContext(
        channel=ChannelConfig(name="好单库-全网", site_key="haodanku", default_url="https://v2.api.haodanku.com/wire_report_new"),
        session=src_sess,
        known_ids=set(),
    )
    with patch("services.deal_sources.haodanku._resolve_hdk_apikey", return_value="k1"):
        fetched = HaodankuSource().fetch(ctx)
    assert len(fetched.records) == 2
    by_id = {d["source_item_id"]: d for d in fetched.records}
    assert not any(lk.get("type") == "encrypted_id" for lk in by_id["w_priv"]["extracted_links"])
    assert any(lk.get("type") == "encrypted_id" and lk.get("url") == official_sign for lk in by_id["w_off"]["extracted_links"])











