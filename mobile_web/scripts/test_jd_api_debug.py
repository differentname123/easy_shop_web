# -*- coding: utf-8 -*-
"""
京东联盟开放平台 (https://api.jd.com/routerjson) 官方直连与官方协议接口权限全量自检脚本
"""
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SHANGHAI = timezone(timedelta(hours=8))

JD_APP_KEY = "45cb55a17ecbad6e91b004c0c34babe2"
JD_APP_SECRET = "8d501939a5084e62aeed3377f9d6e7ac"
JD_UNION_ID = "2035679405"
DEFAULT_POSITION_ID = "3110104129"
ZTK_APPKEY = "41ce110973f744118c1227d5f7f72b65"

JD_ROUTER_URL = "https://api.jd.com/routerjson"
ZTK_JD_BASE = "http://api.zhetaoke.com:20000/api"


def get_jd_server_time() -> str:
    try:
        resp = requests.head(JD_ROUTER_URL, timeout=5)
        date_hdr = resp.headers.get("Date")
        if date_hdr:
            dt = parsedate_to_datetime(date_hdr).astimezone(SHANGHAI)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def sign_jd_request(params: dict, app_secret: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if v is not None and k != "sign")
    raw = app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def call_jd_official(method: str, param_json_obj: dict, timestamp: str = None) -> dict:
    ts = timestamp or get_jd_server_time()
    param_str = json.dumps(param_json_obj, ensure_ascii=False, separators=(",", ":"))
    sys_params = {
        "method": method,
        "app_key": JD_APP_KEY,
        "timestamp": ts,
        "format": "json",
        "v": "1.0",
        "sign_method": "md5",
        "360buy_param_json": param_str,
    }
    sys_params["sign"] = sign_jd_request(sys_params, JD_APP_SECRET)
    resp = requests.get(JD_ROUTER_URL, params=sys_params, timeout=10)
    return resp.json()


def call_ztk_jd(endpoint: str, params: dict) -> dict:
    resp = requests.get(f"{ZTK_JD_BASE}/{endpoint}", params={"appkey": ZTK_APPKEY, **params}, timeout=10)
    return resp.json()


if __name__ == "__main__":
    server_ts = get_jd_server_time()
    print("=" * 95)
    print(f"🚀 京东联盟官方 API 权限与备选通道完整验证报告 (服务器时间: {server_ts})")
    print("=" * 95)

    # 1. 验证纯本地签名直连 https://api.jd.com/routerjson 可用的官方接口
    print("\n【一、纯本地签名直连 https://api.jd.com/routerjson（完全脱离折淘客）】")
    direct_tests = [
        ("1. 实时榜单查询 (rank.query)", "jd.union.open.goods.rank.query", {"RankGoodsReq": {"rankId": 200000, "sortType": 3, "pageIndex": 1, "pageSize": 2}}),
        ("2. 订单行查询 (order.row.query)", "jd.union.open.order.row.query", {"orderReq": {"pageIndex": 1, "pageSize": 5, "type": 1, "startTime": "2026-11-06 05:30:00", "endTime": "2026-11-06 06:30:00"}}),
        ("3. 商品类目查询 (category.goods.get)", "jd.union.open.category.goods.get", {"req": {"parentId": 0, "grade": 0}}),
        ("4. 官方活动查询 (activity.query)", "jd.union.open.activity.query", {"activityReq": {"pageIndex": 1, "pageSize": 2}}),
    ]
    sample_item_id = "JBQ6AYc9MMMqMi16XCDoqMi16XCDoQ_364ty2iuAR6MQXdbNU"
    for label, method, payload in direct_tests:
        res = call_jd_official(method, payload, timestamp=server_ts)
        root = next((k for k in res if k.endswith("_responce") or k.endswith("_response")), None)
        raw = res.get(root, {}).get("queryResult") or res.get(root, {}).get("getResult") or "{}"
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        data = parsed.get("data") or []
        if method == "jd.union.open.goods.rank.query" and data:
            sample_item_id = data[0].get("itemId") or sample_item_id
        print(f"  ✅ {label:36s} -> code={parsed.get('code')}, 返回条数={len(data)}")

    # 2. 验证京东官方 OpenAPI 透传接口（替代脆弱的 api_quanwang.ashx / api_detail.ashx）
    print("\n【二、京东官方 OpenAPI 协议透传接口（借用工具商高阶配额，替代脆弱的 api_quanwang / api_detail）】")

    # 2.1 官方关键词/单品搜索接口 jd.union.open.goods.query
    res_q = call_ztk_jd("open_jing_union_open_goods_query.ashx", {"unionId": JD_UNION_ID, "keyword": "可乐", "pageIndex": 1, "pageSize": 3})
    q_raw = res_q.get("jd_union_open_goods_query_response", {}).get("result") or res_q
    q_parsed = json.loads(q_raw) if isinstance(q_raw, str) else q_raw
    if isinstance(q_parsed, dict) and q_parsed.get("code") == 200:
        q_data = q_parsed.get("data") or []
        first_q = q_data[0] if q_data else {}
        print(f"  ✅ 1. 官方搜索 (open_jing_union_open_goods_query.ashx, keyword='可乐') -> code=200, 条数={len(q_data)}")
        print(f"        首条商品: {first_q.get('skuName', '')[:30]} | priceInfo={first_q.get('priceInfo')} | 轮播图数={len((first_q.get('imageInfo') or {}).get('imageList') or [])}")
    else:
        print(f"  ⚠️ 1. 官方搜索响应: {str(res_q)[:200]}")

    # 2.2 官方搜索接口按 keyword=itemId 精确查单品完整详情（含价格、佣金、优惠券、轮播图、店铺）
    res_id_q = call_ztk_jd("open_jing_union_open_goods_query.ashx", {"unionId": JD_UNION_ID, "keyword": sample_item_id, "pageIndex": 1, "pageSize": 1})
    id_raw = res_id_q.get("jd_union_open_goods_query_response", {}).get("result") or res_id_q
    id_parsed = json.loads(id_raw) if isinstance(id_raw, str) else id_raw
    if isinstance(id_parsed, dict) and id_parsed.get("code") == 200 and id_parsed.get("data"):
        d_item = id_parsed["data"][0]
        print(f"  ✅ 2. 官方单品详情 (goods.query 传 keyword=itemId) -> code=200")
        print(f"        标题: {d_item.get('skuName')[:35]} | 价格: {d_item.get('priceInfo')} | 佣金: {d_item.get('commissionInfo', {}).get('commission')} | 店铺: {d_item.get('shopInfo', {}).get('shopName')}")
    else:
        print(f"  ⚠️ 2. 官方单品详情 (goods.query 传 keyword=itemId) 响应: {str(res_id_q)[:200]}")

    # 2.3 官方大字段详情接口 jd.union.open.goods.bigfield.query
    res_bf = call_ztk_jd("open_jd_union_open_goods_bigfield_query.ashx", {"unionId": JD_UNION_ID, "content": sample_item_id})
    bf_raw = res_bf.get("jd_union_open_goods_bigfield_query_response", {}).get("result") or res_bf
    bf_parsed = json.loads(bf_raw) if isinstance(bf_raw, str) else bf_raw
    if isinstance(bf_parsed, dict) and bf_parsed.get("code") == 200 and bf_parsed.get("data"):
        bf_item = bf_parsed["data"][0]
        imgs = (bf_item.get("imageInfo") or {}).get("imageList") or []
        print(f"  ✅ 3. 官方大字段详情 (open_jd_union_open_goods_bigfield_query.ashx) -> code=200")
        print(f"        反解出真实数字 mainSkuId={bf_item.get('mainSkuId')} | 轮播图数={len(imgs)} | 类目={bf_item.get('categoryInfo', {}).get('cid3Name')}")
    else:
        print(f"  ⚠️ 3. 官方大字段详情响应: {str(res_bf)[:200]}")

    # 2.4 官方京粉精选商品池接口 jd.union.open.goods.jingfen.query
    res_jf = call_ztk_jd("open_jing_union_open_goods_jingfen_query.ashx", {"unionId": JD_UNION_ID, "eliteId": "22", "pageIndex": 1, "pageSize": 3})
    jf_raw = res_jf.get("jd_union_open_goods_jingfen_query_response", {}).get("result") or res_jf
    jf_parsed = json.loads(jf_raw) if isinstance(jf_raw, str) else jf_raw
    if isinstance(jf_parsed, dict) and jf_parsed.get("code") == 200:
        jf_data = jf_parsed.get("data") or []
        print(f"  ✅ 4. 官方京粉精选池 (open_jing_union_open_goods_jingfen_query.ashx) -> code=200, 条数={len(jf_data)}")
    else:
        print(f"  ⚠️ 4. 官方京粉精选池响应: {str(res_jf)[:200]}")

    # 2.5 官方转链接口直接传加密 itemId（无需先调详情接口拿 URL）
    res_promo = call_ztk_jd("open_jing_union_open_promotion_byunionid_get.ashx", {
        "unionId": JD_UNION_ID, "materialId": sample_item_id, "positionId": DEFAULT_POSITION_ID, "chainType": 3
    })
    p_raw = res_promo.get("jd_union_open_promotion_byunionid_get_response", {}).get("result") or res_promo
    p_parsed = json.loads(p_raw) if isinstance(p_raw, str) else p_raw
    if isinstance(p_parsed, dict) and p_parsed.get("code") == 200:
        pdata = p_parsed.get("data") or {}
        print(f"  ✅ 5. 官方转链直接传加密 itemId (无需查详情) -> code=200, shortURL={pdata.get('shortURL')}, jShortCommand={pdata.get('jShortCommand')}")
    else:
        print(f"  ⚠️ 5. 官方转链响应: {str(res_promo)[:200]}")