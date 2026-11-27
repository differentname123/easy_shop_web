# -*- coding: utf-8 -*-
"""统一对外边界安全与高性价比架构验收测试。"""
from __future__ import annotations

from dataclasses import replace
import datetime
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import adapters
from auth import create_access_token
from cache import SIGN_BY_ID_CACHE, clear_all_caches
import config
import main as main_module
from main import create_app
from public_boundary import (
    coupon_link_digest,
    decode_coupon_code,
    decode_item_code,
    encode_coupon_code,
    encode_item_code,
)
from repositories.favorite_repo import FavoriteRepository
from repositories.product_repo import ProductRepository
from services.deal_submit_service import DealSubmitService
from services.favorite_service import FavoriteService
from services.product_service import ProductService
from services.promote_service import PromoteService
from tests.test_favorites import _FavMemoryCollection
from tests.test_product_repository import MemoryCollection as ProductMemoryCollection
from tests.test_user_deal_submit import _make_harness, _make_png_b64


_FORBIDDEN_RESPONSE_KEYS = frozenset({
    "goods_id",
    "goods_sign",
    "product_id",
    "canonical_goods_id",
    "candidate_goods_id",
    "resolved_product_id",
    "resolved_goods_sign",
    "item_id",
    "sku_id",
    "tao_id",
    "num_iid",
    "superseded_by",
    "search_id",
    "product_url",
    "coupon_url",
    "item_url",
    "material_url",
    "coupon_share_url",
    "coupon_click_url",
    "raw",
    "raw_item",
})

_FORBIDDEN_REGULAR_PROMO_KEYS = _FORBIDDEN_RESPONSE_KEYS | frozenset({
    "click_url",
    "short_url",
    "mobile_url",
    "mobile_short_url",
    "final_link",
    "schema_url",
    "promotion_token",
    "j_command",
    "j_short_command",
    "command_text",
    "my_promo_link",
    "my_promo_token",
    "my_promotion_link",
    "my_promotion_token",
    "we_app_info",
})

_SECRET_SUBSTRINGS = (
    "https://item.jd.com/",
    "https://jingfen.jd.com/",
    "https://coupon.jd.com/",
    "https://uland.taobao.com/",
    "https://s.click.taobao.com/",
    "https://mobile.yangkeduo.com/",
    "SIGN_PDD_SECRET",
    "JD_ENC_SECRET",
    "TB_ENC_SECRET",
    "￥secretTkl88￥",
    "！secretJcmd99！",
    "6ae3f40fF84b3cfef_0083320320005d73",
)


def _assert_no_leaks(obj, *, promo_mode: bool = False, path: str = "$"):
    forbidden = _FORBIDDEN_RESPONSE_KEYS if promo_mode else _FORBIDDEN_REGULAR_PROMO_KEYS
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            _assert_no_leaks(item, promo_mode=False, path=f"{path}[{idx}]")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in forbidden, f"泄漏禁止字段 {path}.{k}"
            _assert_no_leaks(v, promo_mode=False, path=f"{path}.{k}")
        return
    if isinstance(obj, str) and not promo_mode:
        for secret in _SECRET_SUBSTRINGS:
            assert secret not in obj, f"文本字段 {path} 泄漏敏感片段 {secret}: {obj}"


def test_deterministic_codec_and_tamper_rejection():
    clear_all_caches()
    c1 = encode_item_code("jd", "6ae3f40fF84b3cfef_0083320320005d73")
    c2 = encode_item_code("jd", "6ae3f40fF84b3cfef_0083320320005d73")
    assert c1 == c2
    assert c1.startswith("p1_")
    assert "jd" not in c1.lower()
    assert "6ae3f40f" not in c1
    assert decode_item_code(c1) == ("jd", "6ae3f40fF84b3cfef_0083320320005d73")

    cpn = encode_coupon_code("jd", "6ae3f40fF84b3cfef_0083320320005d73", "https://coupon.jd.com/1", "！abc123！")
    assert cpn.startswith("c1_")
    assert "coupon.jd.com" not in cpn
    assert decode_coupon_code(cpn) == (
        "jd",
        "6ae3f40fF84b3cfef_0083320320005d73",
        coupon_link_digest("https://coupon.jd.com/1", "！abc123！"),
    )

    # 篡改、跨类型混用、明文全部拒绝
    tampered = c1[:-1] + ("A" if c1[-1] != "A" else "B")
    for bad in (tampered, cpn, "123456", "jd:123", "", None, "p1_invalid", "p1_" + "a" * 300):
        try:
            decode_item_code(bad)
            raise AssertionError(f"应拒绝非法 item_code: {bad!r}")
        except ValueError:
            pass
    for bad_cpn in (c1, cpn[:-1] + ("A" if cpn[-1] != "A" else "B"), "https://coupon.jd.com/1", ""):
        try:
            decode_coupon_code(bad_cpn)
            raise AssertionError(f"应拒绝非法 coupon_code: {bad_cpn!r}")
        except ValueError:
            pass


def test_all_endpoints_strip_plaintext_and_auto_protect_new_routes(monkeypatch):
    clear_all_caches()
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    jd_cid = "6ae3f40fF84b3cfef_0083320320005d73"
    jd_sign = "JD_ENC_SECRET_SIGN_001"
    pdd_gid = "987654321"
    pdd_sign = "SIGN_PDD_SECRET_001"
    cpn_url = "https://coupon.jd.com/ilove?key=secret_cpn"
    cpn_tok = "！secretJcmd99！"

    prod_col = ProductMemoryCollection()
    prod_col.docs.append({
        "platform": "jd",
        "product_id": jd_cid,
        "goods_id": jd_cid,
        "goods_sign": jd_sign,
        "name": "维达抽纸24包",
        "image_url": "https://img14.360buyimg.com/pop/jfs/t1/1/1/1/6ae3f40fF84b3cfef/0083320320005d73.jpg",
        "carousel_images": ["https://img14.360buyimg.com/pop/jfs/t1/1/1/1/6ae3f40fF84b3cfef/0083320320005d73.jpg"],
        "product_url": f"https://jingfen.jd.com/detail/{jd_sign}.html",
        "coupon_url": cpn_url,
        "price": 19.9,
        "original_price": 29.9,
        "activity_price": 19.9,
        "coupon_amount": 10.0,
        "has_coupon": True,
        "channel_type": 5,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        "detail_updated_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        "desc": "好价详情 https://item.jd.com/100012345.html 口令 ￥secretTkl88￥",
        "coupon_info": {"coupon_url": cpn_url, "coupon_remain_count": 50},
        "deal_info": {
            "is_deal_tip": True,
            "deal_price": 18.9,
            "price_logic": "领券 https://coupon.jd.com/ilove?key=secret_cpn 减10",
            "tutorial_steps": ["1. 先领券 https://coupon.jd.com/ilove?key=secret_cpn", "2. 下单到手18.9"],
            "original_copywriting": "维达抽纸\nhttps://u.jd.com/abc123\n￥secretTkl88￥\n到手18.9",
            "coupon_links": [{"label": "满20减10", "url": cpn_url, "token": cpn_tok, "platform": "jd"}],
        },
        "is_off_promotion": False,
    })

    fav_col = _FavMemoryCollection()

    class FakeDb:
        def get_collection(self, name):
            if name == "products":
                return prod_col
            return fav_col

    prod_repo = ProductRepository(db_instance=FakeDb())
    fav_repo = FavoriteRepository(db_instance=FakeDb())
    ps = ProductService(product_repo=prod_repo)
    promo_svc = PromoteService(product_repo=prod_repo)
    fav_svc = FavoriteService(favorite_repo=fav_repo, product_repo=prod_repo)

    pdd_adapter = MagicMock()
    pdd_adapter.active = True
    pdd_adapter.platform_code = "pdd"
    pdd_adapter.platform_name = "拼多多"
    pdd_adapter.channels = ({"channel_type": 5, "name": "热销"},)
    pdd_adapter.capabilities = {"recommend": True, "search": True, "detail": True, "promotion": True, "orders": True}
    pdd_adapter.requires_authorized_promotion = False
    pdd_adapter.requires_numeric_promoter_id = False
    pdd_adapter.requires_material_url = False
    pdd_adapter.supports_coupon_enrichment = False
    pdd_adapter.get_recommend_goods.return_value = {
        "status": "success",
        "data": [{
            "platform": "pdd",
            "goods_id": pdd_gid,
            "goods_sign": pdd_sign,
            "name": "测试拼多多商品",
            "image_url": "https://img.pdd.com/a.jpg",
            "product_url": "https://mobile.yangkeduo.com/goods.html?goods_id=987654321",
            "coupon_url": "https://mobile.yangkeduo.com/coupon.html?x=1",
            "price": 12.0,
        }],
    }
    pdd_adapter.search_goods.return_value = pdd_adapter.get_recommend_goods.return_value
    pdd_adapter.resolve_and_convert_universal.return_value = {
        "status": "success",
        "resolution_kind": "product",
        "final_link": "https://p.pinduoduo.com/s/my_promo",
        "short_url": "https://p.pinduoduo.com/s/my_promo",
        "url": "https://p.pinduoduo.com/s/my_promo",
        "promotion_token": "",
        "goods_id": pdd_gid,
        "goods_sign": pdd_sign,
        "product": {
            "platform": "pdd",
            "goods_id": pdd_gid,
            "goods_sign": pdd_sign,
            "name": "万能反查商品",
            "image_url": "https://img.pdd.com/a.jpg",
            "product_url": "https://mobile.yangkeduo.com/goods.html?goods_id=987654321",
            "coupon_url": "https://mobile.yangkeduo.com/coupon.html?x=1",
            "price": 12.0,
        },
    }
    pdd_adapter.build_command_text.return_value = "测试口令文案 https://p.pinduoduo.com/s/my_promo"
    monkeypatch.setitem(adapters._ADAPTER_REGISTRY, "pdd", pdd_adapter)

    jd_adapter = MagicMock()
    jd_adapter.active = True
    jd_adapter.platform_code = "jd"
    jd_adapter.platform_name = "京东"
    jd_adapter.channels = ({"channel_type": 5, "name": "热销"},)
    jd_adapter.capabilities = {"recommend": True, "search": True, "detail": True, "promotion": True, "orders": True}
    jd_adapter.requires_authorized_promotion = False
    jd_adapter.requires_numeric_promoter_id = True
    jd_adapter.default_promoter_id = "12345"
    jd_adapter.requires_material_url = True
    jd_adapter.supports_coupon_enrichment = True
    jd_adapter.extract_trusted_coupon_url.return_value = cpn_url
    jd_adapter.should_enrich_promotion_coupon.return_value = False
    jd_adapter.is_authoritative_material_url.return_value = True
    jd_adapter.resolve_product_ref.side_effect = lambda k, goods_id=None, goods_sign=None: SimpleNamespace(
        platform="jd", goods_id=goods_id or jd_cid, goods_sign=goods_sign or "", key=(goods_sign or goods_id or jd_cid),
    )
    jd_adapter.convert_promotion_url.return_value = {
        "status": "success",
        "promoter_id": "12345",
        "final_link": "https://u.jd.com/my_converted_link",
        "short_url": "https://u.jd.com/my_converted_link",
        "url": "https://u.jd.com/my_converted_link",
        "promotion_token": "！myJcmd01！",
        "goods_id": jd_cid,
        "goods_sign": jd_sign,
    }
    jd_adapter.build_command_text.return_value = "京东商品 https://u.jd.com/my_converted_link"
    monkeypatch.setitem(adapters._ADAPTER_REGISTRY, "jd", jd_adapter)

    import api.home as home_api
    import api.goods as goods_api
    import api.promote as promo_api
    import api.favorite as fav_api
    import api.order as order_api
    import api.group_help as gh_api

    monkeypatch.setattr(home_api, "product_service", ps)
    monkeypatch.setattr(goods_api, "product_service", ps)
    monkeypatch.setattr(home_api, "favorite_repo", fav_repo)
    monkeypatch.setattr(promo_api, "promote_service", promo_svc)
    monkeypatch.setattr(fav_api, "favorite_service", fav_svc)
    monkeypatch.setattr(
        order_api.order_service,
        "sync_and_query_orders",
        lambda **k: {
            "status": "success",
            "data": {
                "order_list": [{
                    "platform": "jd",
                    "order_sn": "ORD_1",
                    "goods_id": "100012345",
                    "goods_name": "维达抽纸",
                    "promotion_amount_yuan": 1.2,
                }],
            },
        },
    )
    monkeypatch.setattr(
        gh_api.group_help_service,
        "list_groups",
        lambda **k: {"items": [{"group_order_id": "3510310577796381111", "goods_id": "123", "share_text": "pdd_group"}], "total": 1, "has_more": False},
    )

    app = create_app()

    # 验收标准 3：新写一个返回商品数据的接口，不做任何额外配置也会自动隐藏明文并输出加密编号
    @app.get("/api/test_new_product_feed")
    def _new_product_feed():
        return {
            "status": "success",
            "items": [{
                "platform": "taobao",
                "goods_id": "TB_ENC_SECRET",
                "goods_sign": "SALT-TB_ENC_SECRET",
                "product_url": "https://uland.taobao.com/coupon/edetail?e=raw",
                "coupon_url": "https://uland.taobao.com/coupon/edetail?e=cpn",
                "name": "新增接口自动脱敏商品",
            }],
        }

    tok = create_access_token("u_sec12345", ttl=3600)
    headers = {"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"}
    jd_item_code = encode_item_code("jd", jd_cid)
    pdd_item_code = encode_item_code("pdd", pdd_gid)

    with TestClient(app) as client:
        # 1. 新接口零配置自动脱敏
        new_resp = client.get("/api/test_new_product_feed", headers=headers)
        assert new_resp.status_code == 200
        new_json = new_resp.json()
        _assert_no_leaks(new_json)
        assert new_json["items"][0]["item_code"] == encode_item_code("taobao", "TB_ENC_SECRET")

        # 2. 首页推荐、搜索、严选库全部脱敏（含 gzip 响应）
        rec_json = client.get("/api/home/recommend?platform=pdd&channel_type=5", headers=headers).json()
        _assert_no_leaks(rec_json)
        assert rec_json["data"][0]["item_code"] == pdd_item_code

        srch_json = client.get("/api/home/search?platform=pdd&keyword=抽纸", headers=headers).json()
        _assert_no_leaks(srch_json)
        assert srch_json["data"][0]["item_code"] == pdd_item_code

        boom_json = client.get("/api/goods/list?platform=jd", headers=headers).json()
        _assert_no_leaks(boom_json)
        assert boom_json["data"][0]["item_code"] == jd_item_code

        # 3. 详情页脱敏（含 coupon_links 转 coupon_code 与线报文案去链接去口令）
        det_json = client.get(f"/api/home/detail?item_code={jd_item_code}", headers=headers).json()
        _assert_no_leaks(det_json)
        assert det_json["data"]["item_code"] == jd_item_code
        cpn_entry = det_json["data"]["deal_tip"]["coupon_links"][0]
        assert cpn_entry["coupon_code"].startswith("c1_")
        assert "url" not in cpn_entry and "token" not in cpn_entry
        assert cpn_entry["has_url"] is True and cpn_entry["has_token"] is True
        cpn_code = cpn_entry["coupon_code"]

        # 4. 收藏 & 订单脱敏
        fav_add = client.post("/api/favorites/add", headers=headers, json={"item_code": jd_item_code}).json()
        _assert_no_leaks(fav_add)
        fav_list = client.get("/api/favorites/list", headers=headers).json()
        _assert_no_leaks(fav_list)
        assert fav_list["data"]["items"][0]["item_code"] == jd_item_code

        ord_json = client.post("/api/order/sync_and_query?platform=jd&days=7", headers=headers).json()
        _assert_no_leaks(ord_json)
        assert ord_json["data"]["order_list"][0]["item_code"] == encode_item_code("jd", "100012345")

        # 5. 万能转链：顶层保留推广结果，嵌套 product 脱敏
        uni_json = client.post(
            "/api/promote/universal",
            headers=headers,
            json={"text": "https://mobile.yangkeduo.com/goods.html?goods_id=987654321", "platform": "pdd"},
        ).json()
        assert uni_json["final_link"] == "https://p.pinduoduo.com/s/my_promo"
        _assert_no_leaks(uni_json, promo_mode=True)
        assert uni_json["product"]["item_code"] == pdd_item_code

        # 6. 拼多多互助接口保持豁免
        gh_json = client.get("/api/group_help/list").json()
        assert gh_json["data"]["items"][0]["goods_id"] == "123"

        # 7. 验收标准 2：明文入参 / 伪造编号全被拒绝
        for bad_q in ("goods_id=987654321", "goods_sign=SIGN_PDD_SECRET_001", f"item_code={jd_item_code}&platform=jd", "item_code=forged_123"):
            assert client.get(f"/api/home/detail?{bad_q}").status_code in (400, 422)
        for bad_promo in (
            {"platform": "jd", "goods_id": jd_cid},
            {"item_code": "p1_tampered123456"},
            {"coupon_code": jd_item_code},
            {"item_code": jd_item_code, "coupon_code": cpn_code},
            {},
        ):
            assert client.post("/api/promote/convert", headers=headers, json=bad_promo).status_code in (400, 422)
        assert client.post("/api/favorites/add", headers=headers, json={"platform": "jd", "product_id": jd_cid}).status_code == 422
        assert client.post("/api/favorites/remove", headers=headers, json={"item_code": "bad"}).status_code in (400, 422)
        assert client.post("/api/goods/action", headers=headers, json={"platform": "jd", "product_id": jd_cid, "action": "share"}).status_code == 422
        assert client.post("/api/deals/submit", headers=headers, json={"platform": "jd", "product_id": jd_cid, "deal_price": 10, "tutorial_text": "test"}).status_code == 422

        # 8. 验收标准 4：模拟服务重启清空全部内存缓存后，既有 item_code / coupon_code 仍可正常详情/收藏/转链/兑券
        clear_all_caches()
        assert SIGN_BY_ID_CACHE.get(("jd", jd_cid)) is None

        det_after_restart = client.get(f"/api/home/detail?item_code={jd_item_code}", headers=headers)
        assert det_after_restart.status_code == 200
        _assert_no_leaks(det_after_restart.json())

        clear_all_caches()
        conv_after_restart = client.post("/api/promote/convert", headers=headers, json={"item_code": jd_item_code})
        assert conv_after_restart.status_code == 200
        conv_json = conv_after_restart.json()
        assert conv_json["final_link"] == "https://u.jd.com/my_converted_link"
        _assert_no_leaks(conv_json, promo_mode=True)
        # 验证冷启动转链从 MongoDB 补回了真实 goods_sign 与 trusted_coupon_url，绝未把主图哈希传给上游
        assert jd_adapter.convert_promotion_url.call_args.kwargs["goods_sign"] == jd_sign
        assert jd_adapter.convert_promotion_url.call_args.kwargs["trusted_coupon_url"] == cpn_url

        cpn_after_restart = client.post("/api/promote/convert", headers=headers, json={"coupon_code": cpn_code})
        assert cpn_after_restart.status_code == 200
        cpn_json = cpn_after_restart.json()
        assert cpn_json["final_link"] == cpn_url
        assert cpn_json["promotion_token"] == cpn_tok


def test_user_deal_submit_and_submissions_sanitize_output(monkeypatch, tmp_path):
    clear_all_caches()
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )
    _, _, _, _, svc = _make_harness(image_dir=tmp_path / "imgs")
    import api.deal_submit as api_mod
    monkeypatch.setattr(api_mod, "deal_submit_service", svc)

    tok = create_access_token("u_alice", ttl=3600)
    headers = {"Authorization": f"Bearer {tok}"}
    code = encode_item_code("jd", "1001001")

    with TestClient(create_app()) as client:
        sub_resp = client.post(
            "/api/deals/submit",
            headers=headers,
            json={
                "item_code": code,
                "deal_price": 29.9,
                "tutorial_text": "领券下单29.9",
                "images_base64": [_make_png_b64(320, 240)],
            },
        )
        assert sub_resp.status_code == 200
        _assert_no_leaks(sub_resp.json())
        assert sub_resp.json()["data"]["item_code"] == code

        list_resp = client.get("/api/deals/my_submissions", headers=headers)
        assert list_resp.status_code == 200
        list_json = list_resp.json()
        _assert_no_leaks(list_json)
        assert list_json["data"]["items"][0]["item_code"] == code


def test_middleware_fail_closed_and_stale_sign_self_healing(monkeypatch):
    from models.product import ProductRef
    import public_boundary

    clear_all_caches()
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    # 1. Fail-Closed：脱敏过程异常时严禁静默吐出原始明文
    app = create_app()

    @app.get("/api/test-fail-closed")
    def _test_fail_closed_route():
        return {"status": "success", "goods_id": "999111222", "product_url": "https://item.jd.com/999111222.html"}

    monkeypatch.setattr(public_boundary, "sanitize_public_payload", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(app) as client:
        resp = client.get("/api/test-fail-closed")
        assert resp.status_code == 500
        assert resp.json()["status"] == "error"
        assert "999111222" not in resp.text
        assert "item.jd.com" not in resp.text

    monkeypatch.undo()
    clear_all_caches()
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    # 2. _sanitize_deal_tip 无 deepcopy 时仍正确递归脱敏嵌套结构，且密钥切换立即生效
    deal_sanitized = public_boundary.sanitize_public_payload({
        "platform": "pdd",
        "goods_id": "88776655",
        "deal_tip": {
            "is_deal_tip": True,
            "nested": {"goods_id": "88776655", "product_url": "https://mobile.yangkeduo.com/goods.html?goods_id=88776655"},
            "coupon_links": [{"label": "5元券", "url": "https://mobile.yangkeduo.com/cpn?1", "platform": "pdd"}],
        },
    })
    assert deal_sanitized["deal_tip"]["nested"]["item_code"] == encode_item_code("pdd", "88776655")
    assert "goods_id" not in deal_sanitized["deal_tip"]["nested"]
    assert "product_url" not in deal_sanitized["deal_tip"]["nested"]
    assert deal_sanitized["deal_tip"]["coupon_links"][0]["coupon_code"].startswith("c1_")

    old_code = encode_item_code("pdd", "88776655")
    monkeypatch.setattr(config, "AUTH_SECRET", "rotated_test_secret_key_for_hmac_cache_check_999")
    assert encode_item_code("pdd", "88776655") != old_code
    try:
        decode_item_code(old_code)
        raise AssertionError("更换 AUTH_SECRET 后旧 item_code 应校验失败")
    except ValueError:
        pass

    # 3. SIGN_BY_ID_CACHE 存旧 sign 时，PromoteService 与 ProductService 均能自动以数据库新 sign 自愈刷新
    prod_col = ProductMemoryCollection()
    prod_col.docs.append({
        "platform": "pdd",
        "product_id": "88776655",
        "goods_id": "88776655",
        "goods_sign": "E9_NEW_ROTATED_SIGN_002",
        "name": "自愈测试商品",
        "price": 9.9,
        "image_url": "https://img.pdd.com/1.jpg",
        "detail_updated_at": datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        "is_off_promotion": False,
    })
    prod_repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _n: prod_col))

    SIGN_BY_ID_CACHE.set(("pdd", "88776655"), "E9_OLD_STALE_SIGN_001")
    resolved = PromoteService(product_repo=prod_repo)._resolve_product_ref(ProductRef("pdd", "88776655", ""))
    assert resolved.goods_sign == "E9_NEW_ROTATED_SIGN_002"
    assert SIGN_BY_ID_CACHE.get(("pdd", "88776655")) == "E9_NEW_ROTATED_SIGN_002"

    pdd_adapter = MagicMock()
    pdd_adapter.active = True
    pdd_adapter.platform_code = "pdd"
    pdd_adapter.platform_name = "拼多多"
    pdd_adapter.capabilities = {"detail": True}
    pdd_adapter.resolve_product_ref.side_effect = lambda gid_or_sign, goods_id=None, goods_sign=None: ProductRef(
        "pdd", goods_id or (gid_or_sign if str(gid_or_sign or "").isdigit() else ""), goods_sign or ""
    )
    pdd_adapter.get_goods_detail.return_value = {
        "status": "success",
        "data": {
            "platform": "pdd",
            "goods_id": "88776655",
            "goods_sign": "E9_NEW_ROTATED_SIGN_002",
            "name": "自愈测试商品",
            "price": 9.9,
            "image_url": "https://img.pdd.com/1.jpg",
        },
    }
    monkeypatch.setitem(adapters._ADAPTER_REGISTRY, "pdd", pdd_adapter)

    SIGN_BY_ID_CACHE.set(("pdd", "88776655"), "E9_OLD_STALE_SIGN_001")
    ps = ProductService(product_repo=prod_repo)
    det_res = ps.get_goods_detail(platform="pdd", goods_id="88776655")
    assert det_res["status"] == "success"
    assert pdd_adapter.get_goods_detail.call_args.kwargs["goods_sign"] == "E9_NEW_ROTATED_SIGN_002"
    assert SIGN_BY_ID_CACHE.get(("pdd", "88776655")) == "E9_NEW_ROTATED_SIGN_002"
