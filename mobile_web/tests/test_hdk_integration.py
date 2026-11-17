# -*- coding: utf-8 -*-
"""好单库 (Haodanku) 京东多供应商扩展、契约对齐与主备降级全方位验证测试。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from infrastructure.hdk_client import HdkClient
from infrastructure.jd_client import JdClient
from models.product import ProductDetail, ProductRef, ProductSummary, extract_canonical_goods_id, extract_jd_image_key


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
    assert res_auth["error_code"] == "AUTH_REQUIRED"


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
        promoter_id="888",
        sub_union_id="sub_123",
        coupon_url="https://coupon.jd.com/test",
    )
    assert res["status"] == "success"
    assert res["short_url"] == "https://u.jd.com/ogpcRGH"
    assert res["final_link"] == "https://u.jd.com/ogpcRGH"
    assert res["promoter_id"] == "888"
    call_kwargs = session.post.call_args[1]["data"]
    assert call_kwargs["sceneId"] == 1
    assert call_kwargs["subUnionId"] == "sub_123"
    assert call_kwargs["coupon_url"] == "https://coupon.jd.com/test"


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
    """验证 JdAdapter 读取 hdk_api_key_list 并进行多 Key 轮询负载均衡。"""
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

    def _mock_get(url, params=None, **kw):
        k = params.get("apikey")
        call_keys.append(k)
        return _mock_response(200, {"code": 200, "data": [{"skuid": "1", "goodsname": "纸巾"}]})

    adapter.hdk.http.get = _mock_get

    # 连续调用 3 次 search，多 key 均匀轮询分摊配额 (Round Robin)
    adapter.search_goods("纸巾")
    adapter.search_goods("纸巾")
    adapter.search_goods("纸巾")
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

    # 1. 测试 ProductSummary 归一化
    raw_hdk_item = {
        "skuid": "10001",
        "itemid": "IwQNjAAA_test_enc_id",
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
    assert summary["goods_sign"] == "10001"
    # goods_id 必须是由主图哈希生成的确定性主键，解耦动态 goods_sign
    assert summary["goods_id"] == extract_canonical_goods_id("jd", "10001", image_url=raw_hdk_item["itempic"])
    assert summary["price"] == 39.90
    assert summary["original_price"] == 49.90
    assert summary["coupon_amount"] == 10.0
    assert summary["has_coupon"] is True
    assert summary["coupon_url"] == "https://coupon.jd.com/test_coupon"
    assert summary["name"] == raw_hdk_item["goodsname"]
    assert summary["brand"] == "心相印"
    assert summary["mall_name"] == "心相印京东自营旗舰店"

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

def test_jd_adapter_search_prefers_hdk():
    """HDK 挂载后，search 首选 HDK (高配额+全网搜索)；若 HDK 成功则直接返回。"""
    hdk_mock = Mock()
    hdk_mock.search_goods.return_value = {
        "status": "success",
        "data": [
            {
                "skuid": "10002",
                "itemid": "enc_2",
                "goodsname": "好单库首选商品",
                "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/hdk.jpg",
                "itemprice": "29.90",
            }
        ],
        "total_count": 1,
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }
    ztk_client = Mock()
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.search_goods("抽纸")

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_search"
    assert res["data"][0]["name"] == "好单库首选商品"
    # ZTK 未被调用（节省 ZTK 选品库额度）
    ztk_client.search_goods.assert_not_called()


def test_jd_adapter_detail_fails_over_to_ztk_on_hdk_rate_limit():
    """HDK 详情触发 RATE_LIMIT 时，自动无缝降级到 ZTK 详情。"""
    hdk_mock = Mock()
    hdk_mock.get_goods_detail.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "好单库频次超限",
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.get_goods_detail.return_value = {
        "status": "success",
        "data": [
            {
                "tao_id": "enc_target",
                "title": "ZTK兜底商品",
                "wlprice": "19.90",
                "pict_url": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/ztk.jpg",
            }
        ]
    }
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.get_goods_detail("enc_target", goods_sign="enc_target")

    assert res["status"] == "success"
    assert res["_provider"] == "ztk_jd_detail"
    assert res["data"]["name"] == "ZTK兜底商品"
    # 验证 HDK 被调用后失败，随后 ZTK 被调用
    hdk_mock.get_goods_detail.assert_called_once()
    ztk_client.get_goods_detail.assert_called_once()


def test_jd_adapter_promotion_fails_over_to_hdk_on_ztk_error():
    """转链以 ZTK 为主（产出京口令），若 ZTK 发生临时异常，无缝降级到 HDK 转链保底生链。"""
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
    assert res["short_url"] == "https://u.jd.com/hdk_backup"


def test_jd_adapter_universal_resolve_via_hdk():
    """验证当输入 u.jd.com 短链时，HDK 能够直接反查完整商品卡片并完成高佣转链。"""
    hdk_mock = Mock()
    hdk_mock.resolve_short_url.return_value = {
        "status": "success",
        "data": {
            "skuid": "10003",
            "itemid": "IwQNjAAA_test_short_card",
            "goodsname": "维达超韧抽纸3层100抽*24包",
            "itempic": "https://img14.360buyimg.com/n1/s800x800_jfs/t1/vinda.jpg",
            "itemprice": "39.90",
            "itemendprice": "29.90",
            "couponmoney": "10.00",
            "couponurl": "https://coupon.jd.com/vinda_coupon",
        }
    }
    hdk_mock.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/vinda_promoted",
        "click_url": "https://union-click.jd.com/vinda_promoted",
        "final_link": "https://u.jd.com/vinda_promoted",
        "promoter_id": "88",
    }
    ztk_client = Mock()
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
    assert res["_provider"] == "hdk_jd_universal"
    assert res["final_link"] == "https://u.jd.com/vinda_promoted"
    assert res["product"]["name"] == "维达超韧抽纸3层100抽*24包"
    assert res["product"]["price"] == 29.90
    assert res["product"]["coupon_amount"] == 10.0


def test_hdk_agents_md_rule3_compliance():
    """严守 AGENTS.md 第三条铁律：
    1. 严禁指鹿为马：详情或转链反查失败时，严禁截取标题调用搜索接口首条顶替；
    2. 主键解耦：product_id (goods_id) 必须使用静态主图哈希解耦动态 goods_sign；
    3. 主图哈希绝不可传给上游详情接口。
    """
    adapter = JdAdapter(settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88"))

    # 1. 验证静态主图哈希解耦
    hdk_raw = {
        "skuid": "10099",
        "itemid": "dynamic_goods_sign_salt_12345",
        "goodsname": "合规测试商品",
        "itempic": "https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg",
        "itemprice": "50.00",
    }
    summary = adapter._normalize_hdk_summary(hdk_raw)
    img_key = extract_jd_image_key("https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg")
    assert img_key == "6ae3f40fF84b3cfef_0083320320005d73"
    assert summary["goods_id"] == img_key
    assert summary["goods_sign"] == "10099"  # skuid 取首选

    # 2. 验证主图哈希绝不能作为 target_id 直接调用上游详情接口
    hdk_mock = Mock()
    adapter.attach_hdk(hdk_mock)
    res = adapter.get_goods_detail(img_key, goods_id=img_key)
    assert res["status"] == "error"
    assert res["error_code"] == "INVALID_ARGUMENT"
    assert "不能直接使用主图哈希 ID" in res["error_msg"]
    hdk_mock.get_goods_detail.assert_not_called()

