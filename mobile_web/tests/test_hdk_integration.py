# -*- coding: utf-8 -*-
"""好单库 (Haodanku) 京东多供应商扩展、契约对齐与主备降级全方位验证测试。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.jd_adapter import JdAdapter
from adapters.tb_adapter import TaobaoAdapter
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


def test_jd_adapter_promotion_prefers_hdk_as_primary():
    """转链以 HDK 为主力首选，成功时直接返回，不消耗 ZTK 额度。"""
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
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
    assert res["_provider"] == "hdk_jd_promotion"
    assert res["final_link"] == "https://u.jd.com/hdk_primary"
    assert res["short_url"] == "https://u.jd.com/hdk_primary"
    # ZTK 未被调用
    ztk_client.convert_promotion_link.assert_not_called()


def test_jd_adapter_promotion_fails_over_to_ztk_on_hdk_error():
    """HDK 主力转链遇到限流时，自动无缝降级到 ZTK 转链保底生链。"""
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    ztk_client.convert_promotion_link.return_value = {
        "status": "success",
        "short_url": "https://u.jd.com/ztk_backup",
        "click_url": "https://union-click.jd.com/ztk_backup",
        "j_command": "京口令abc",
    }
    hdk_mock = Mock()
    hdk_mock.convert_promotion_link.return_value = {
        "status": "error",
        "error_code": "RATE_LIMIT",
        "error_msg": "好单库转链限流",
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
    assert res["final_link"] == "https://u.jd.com/ztk_backup"
    assert res["promotion_token"] == "京口令abc"
    hdk_mock.convert_promotion_link.assert_called_once()
    ztk_client.convert_promotion_link.assert_called_once()


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


# ----------------------------------------------------------------------
# 4. JD 订单同步与 TB 平台好单库主力扩展测试
# ----------------------------------------------------------------------

def test_jd_adapter_orders_prefers_hdk_and_fails_over_to_ztk():
    """验证 JD 订单同步首选好单库，且能够无缝归一化；好单库失败时顺延至折淘客。"""
    hdk_mock = Mock()
    hdk_mock.query_jd_orders.return_value = {
        "status": "success",
        "order_list": [
            {
                "id": "trade_hdk_123",
                "orderId": "parent_hdk_123",
                "skuId": "10001",
                "skuName": "好单库京东商品",
                "skuNum": 2,
                "estimateCosPrice": 99.0,
                "estimateFee": 9.9,
                "actualFee": 9.9,
                "validCode": 18,
                "orderTime": 1700000000,
                "modifyTime": 1700000100,
                "finishTime": 1700000200,
                "positionId": "88",
                "subUnionId": "sub_1",
                "commissionRate": 10.0,
            }
        ],
        "total_count": 1,
        "page": 1,
        "has_more": False,
        "next_cursor": None,
    }
    ztk_client = Mock()
    ztk_client.appkey_list = ("ztk_key",)
    adapter = JdAdapter(
        settings=SimpleNamespace(jd_enabled=True, jd_union_id="2035679405", jd_position_id="88", jd_app_key="k", jd_app_secret="s"),
        client=ztk_client,
        hdk_client=hdk_mock,
    )
    res = adapter.fetch_increment_orders(1700000000, 1700000200, page=1)

    assert res["status"] == "success"
    assert res["_provider"] == "hdk_jd_orders"
    assert len(res["order_list"]) == 1
    # 验证字段被 normalize_order 正确映射
    norm = adapter.normalize_order(res["order_list"][0])
    assert norm["order_sn"] == "trade_hdk_123"
    assert norm["goods_name"] == "好单库京东商品"
    assert norm["is_settled"] is True
    assert norm["promotion_amount_yuan"] == 9.9
    ztk_client.query_orders.assert_not_called()

    # 模拟好单库报错，验证顺延至折淘客
    hdk_mock.query_jd_orders.return_value = {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "限流"}
    ztk_client.query_orders.return_value = {
        "status": "success",
        "order_list": [{"id": "ztk_row_1", "orderId": "p1", "skuId": "2", "skuName": "ZTK商品", "validCode": 16, "estimateFee": 5.0, "orderTime": 1700000000, "modifyTime": 1700000000}],
        "has_more": False,
        "next_cursor": None,
    }
    res_fallback = adapter.fetch_increment_orders(1700000000, 1700000200, page=1)
    assert res_fallback["status"] == "success"
    assert res_fallback["_provider"] == "ztk_jd_orders"
    assert res_fallback["order_list"][0]["id"] == "ztk_row_1"


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
    assert res["_provider"] == "hdk_jd_universal"
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



