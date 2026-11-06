# -*- coding: utf-8 -*-
"""
京东联盟推手平台 API 自检与验证程序 (多级高可用架构基准版)
================================================================================
本文件是整个项目中京东联盟能力的【唯一基准实现 (Reference Implementation)】。
项目中的 `infrastructure/jd_client.py` 与 `adapters/jd_adapter.py` 均以此文件的协议与多级策略为准。

【背景与痛点】：
折淘客自定义的爬虫/券池接口 `api_quanwang.ashx` (全网搜索) 与 `api_detail.ashx` (商品详情) 极其脆弱：
1. 严禁多线程/高频调用，极易触发防采集风控限制；
2. 存在“只获取商品信息不产生订单”的自动封禁检测；
3. `api_quanwang.ashx` 仅返回折淘客券池内商品，且要求关键词长度 >= 4。

【全新多级高可用方案】：
1. 实时榜单 (`get_rank_goods`):
   - 一级主通道: 本地 MD5 签名直连京东宙斯官方网关 `https://api.jd.com/routerjson` (`jd.union.open.goods.rank.query`)，完全零第三方依赖；
   - 二级兜底通道: 折淘客官方协议中继 `open_jd_union_open_goods_rank_query.ashx`。
   - 列表自带 `imgList` 多张轮播图、`purchasePriceInfo` 券后价与优惠券列表，自动写入本地详情快照池。
2. 关键词搜索 (`search_goods`):
   - 一级主通道: 京东联盟官方搜索协议 `open_jing_union_open_goods_query.ashx` (`jd.union.open.goods.query`)，
     覆盖全网京东 CPS 商品，无需短词补齐，单次返回 8~10 张轮播图 (`imageInfo.imageList`)、完整价格、佣金、优惠券与落地页 URL；
   - 二级兜底通道: 折淘客 `api_quanwang.ashx` (仅在主通道异常或无结果时兜底)。
   - 搜索结果同样自动生成详情快照写入本地缓存，实现“列表即详情”。
3. 商品详情 (`get_goods_detail`):
   - 第 0 级 (零外部请求): 优先读取榜单/搜索已沉淀的本地详情快照池 (`_LOCAL_DETAIL_CACHE`)；
   - 第 1 级 (官方主通道): 调用 `open_jing_union_open_goods_query.ashx` (`keyword=item_id, pageIndex=1, pageSize=1`) 获取单品完整商业详情，
     并可按需联动 `open_jd_union_open_goods_bigfield_query.ashx` (`jd.union.open.goods.bigfield.query`) 补充真实数字 `mainSkuId` 与商详大图；
   - 第 2 级 (折淘客兜底): 调用 `api_detail.ashx` (仅在冷启动单品且第 1 级未命中时低频兜底，严禁后台批量预取)。
4. 统一推广转链 (`convert_promotion_link`):
   - 接口: `open_jing_union_open_promotion_byunionid_get.ashx` (`jd.union.open.promotion.byunionid.get`)；
   - 支持直接传入【商品 URL】、【加密 `itemId`】或【纯数字 `skuId` (自动转为标准商详 URL)】，无需为了转链而先调用一次详情接口；
   - `promoter_id` 只需传入纯数字（免后台建位，订单成交后在 `positionId` 原样透出归因）。
5. 增量订单拉取 (`fetch_orders`):
   - 一级主通道: 本地使用 `JD_APP_KEY` + `JD_APP_SECRET` 计算 MD5 签名，直连京东官方网关 `https://api.jd.com/routerjson` (`jd.union.open.order.row.query`)，
     既减少一次中继网络跳转，又彻底避免将 `jd_app_secret` 明文通过 HTTP 发送给第三方；
   - 二级兜底通道: 折淘客订单签名中继 `open_jing_union_openz_order_row_query.ashx`。
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 1. 核心有效配置 ====================
# 折淘客 (折京客) 凭据
ZTK_APPKEY = os.environ.get("ZTK_APPKEY", "41ce110973f744118c1227d5f7f72b65")

# 京东联盟账号 (用于定位归属账号) 与 站长默认推广位
JD_UNION_ID = os.environ.get("JD_UNION_ID", "2035679405")            # 联盟纯数字ID
DEFAULT_POSITION_ID = os.environ.get("JD_POSITION_ID", "3110104129")  # 站长自营默认推广位ID

# 京东开放平台应用凭据 (用于直连京东官方网关本地 MD5 签名，如榜单查询、订单行拉取等)
JD_APP_KEY = os.environ.get("JD_APP_KEY", "45cb55a17ecbad6e91b004c0c34babe2")
JD_APP_SECRET = os.environ.get("JD_APP_SECRET", "8d501939a5084e62aeed3377f9d6e7ac")

# 网关地址
JD_ROUTER_URL = "https://api.jd.com/routerjson"
ZTK_API_BASE = "http://api.zhetaoke.com:20000/api"

# 本地“列表即详情”内存快照池 (业务项目中对应 DETAIL_CACHE + MongoDB detail_snapshots)
# 注意：京东榜单与官方搜索已返回完整轮播图、券后价、佣金与店铺信息，沉淀快照后点击详情可实现 0 外部请求！
_LOCAL_DETAIL_CACHE: Dict[str, Dict[str, Any]] = {}

# 京东订单状态映射
JD_ORDER_STATUS_MAP = {
    -1: "未知",
    2: "无效-拆单",
    3: "成交未结算",
    15: "待付款",
    16: "已付款",
    17: "已完成",
    18: "已结算",
    19: "已失效/锁定",
    20: "平台拦截",
    21: "数据异常",
    22: "白条拒绝",
    23: "拒收",
    24: "支付后取消",
    25: "未支付取消",
    27: "售后中",
    28: "售后退款",
}


# ==================== 2. 京东宙斯官方网关本地签名与直连工具 ====================
def sign_jd_request(params: Dict[str, str], app_secret: str) -> str:
    """
    京东宙斯网关 (https://api.jd.com/routerjson) 标准 MD5 签名算法：
    1. 将除 sign 外的所有系统参数与业务参数 (360buy_param_json) 按参数名 ASCII 升序排列；
    2. 拼接为 key1value1key2value2...，首尾包裹 app_secret；
    3. 计算 MD5 并转为大写十六进制字符串。
    """
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    raw = app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def call_jd_router(
    method: str,
    param_json: Dict[str, Any],
    app_key: str = JD_APP_KEY,
    app_secret: str = JD_APP_SECRET,
    timeout: int = 10,
) -> Dict[str, Any]:
    """
    本地签名直连京东官方网关 `https://api.jd.com/routerjson`。
    【优势】：完全不经过第三方中继服务器，响应极快，且 `app_secret` 永远留在本地内存。
    """
    if not app_key or not app_secret:
        raise ValueError("缺少 JD_APP_KEY 或 JD_APP_SECRET，无法直连京东官方网关")

    sys_params = {
        "method": method,
        "app_key": app_key,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "1.0",
        "sign_method": "md5",
        "360buy_param_json": json.dumps(param_json, ensure_ascii=False, separators=(",", ":")),
    }
    sys_params["sign"] = sign_jd_request(sys_params, app_secret)
    resp = requests.post(JD_ROUTER_URL, data=sys_params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_jd_query_result(resp: Dict[str, Any], response_key_prefix: str) -> Dict[str, Any]:
    """
    兼容提取京东官方网关 / 折淘客官方协议包装的响应字典。
    注意：京东部分接口响应键名为 `..._responce` (历史拼写)，部分为 `..._response`，内部载荷可能为 `queryResult` 或 `result`。
    """
    if not isinstance(resp, dict):
        return {}
    if "error_response" in resp:
        err = resp.get("error_response") or {}
        raise RuntimeError(f"京东网关错误 [code={err.get('code')}]: {err.get('zh_desc') or err.get('en_desc')}")

    wrapper = (
        resp.get(f"{response_key_prefix}_responce")
        or resp.get(f"{response_key_prefix}_response")
        or {}
    )
    if not isinstance(wrapper, dict):
        return {}
    raw_res = wrapper.get("queryResult") if "queryResult" in wrapper else wrapper.get("result")
    if isinstance(raw_res, str):
        return json.loads(raw_res) if raw_res.strip() else {}
    if isinstance(raw_res, dict):
        return raw_res
    return {}


def _normalize_jd_url(raw_url: str, fallback_item_id: str = "") -> str:
    """规范化京东商品落地页 URL（补齐 https:// 协议头）。"""
    u = str(raw_url or "").strip()
    if u.startswith("//"):
        return f"https:{u}"
    if u.startswith(("http://", "https://")):
        return u
    if u:
        return f"https://{u.lstrip('/')}"
    if fallback_item_id:
        return f"https://item.jd.com/{fallback_item_id}.html"
    return ""


def _parse_official_goods_item(raw: Dict[str, Any], override_item_id: str = "") -> Dict[str, Any]:
    """
    将京东官方 `jd.union.open.goods.query` 单条商品结构解析为统一的摘要+详情字段。
    【注意】：
    1. 官方搜索与单品查询均返回此结构，内含 `imageInfo.imageList` (多张轮播图)、`priceInfo`、`commissionInfo`、`couponInfo`、`shopInfo`；
    2. 当使用 `keyword=item_id` 查询单品时，京东可能返回一个新生成的动态同源 `itemId`，此时传入 `override_item_id` 保持主键一致。
    """
    item_id = str(override_item_id or raw.get("itemId") or raw.get("skuId") or "").strip()
    sku_id = str(raw.get("skuId") or "").strip()
    title = str(raw.get("skuName") or raw.get("title") or "").strip()

    price_info = raw.get("priceInfo") if isinstance(raw.get("priceInfo"), dict) else {}
    purchase_info = raw.get("purchasePriceInfo") if isinstance(raw.get("purchasePriceInfo"), dict) else {}
    origin_price = str(
        price_info.get("price")
        or purchase_info.get("price")
        or raw.get("wlprice")
        or "0.00"
    )
    final_price = str(
        price_info.get("lowestCouponPrice")
        or purchase_info.get("purchasePrice")
        or price_info.get("lowestPrice")
        or origin_price
    )

    # 提取优惠券面额
    coupon_info = raw.get("couponInfo") if isinstance(raw.get("couponInfo"), dict) else {}
    coupon_list = coupon_info.get("couponList")
    if not isinstance(coupon_list, list):
        coupon_list = purchase_info.get("couponList") if isinstance(purchase_info.get("couponList"), list) else []
    best_discount = 0.0
    for c in coupon_list:
        if isinstance(c, dict):
            try:
                disc = float(c.get("discount") or 0.0)
                if c.get("isBest") == 1:
                    best_discount = disc
                    break
                if disc > best_discount:
                    best_discount = disc
            except (TypeError, ValueError):
                pass
    coupon_amount = f"{best_discount:g}" if best_discount > 0 else "0"

    # 提取佣金信息
    comm_info = raw.get("commissionInfo") if isinstance(raw.get("commissionInfo"), dict) else {}
    commission_rate = str(
        comm_info.get("commissionShare")
        or raw.get("commissionShare")
        or "0"
    )
    commission_amount = str(
        comm_info.get("couponCommission")
        or comm_info.get("commission")
        or raw.get("commission")
        or "0.00"
    )

    # 提取主图与轮播图
    image_info = raw.get("imageInfo") if isinstance(raw.get("imageInfo"), dict) else {}
    image_list_raw = image_info.get("imageList") if isinstance(image_info.get("imageList"), list) else []
    small_images: List[str] = []
    for img_entry in image_list_raw:
        if isinstance(img_entry, dict) and img_entry.get("url"):
            small_images.append(str(img_entry["url"]).strip())
        elif isinstance(img_entry, str) and img_entry.strip():
            small_images.append(img_entry.strip())
    if not small_images and isinstance(raw.get("imgList"), list):
        small_images = [str(u).strip() for u in raw.get("imgList", []) if str(u).strip()]

    pict_url = str(
        raw.get("imageUrl")
        or image_info.get("whiteImage")
        or (small_images[0] if small_images else "")
    ).strip()
    if pict_url and pict_url not in small_images:
        small_images.insert(0, pict_url)

    # 提取店铺与自营标识
    shop_info = raw.get("shopInfo") if isinstance(raw.get("shopInfo"), dict) else {}
    shop_title = str(shop_info.get("shopName") or raw.get("shopName") or "").strip()
    sku_tags = raw.get("skuTagList") if isinstance(raw.get("skuTagList"), list) else []
    is_self_operated = bool(
        raw.get("isJdSale") == 1
        or str(raw.get("owner") or "").lower() == "g"
        or "自营" in sku_tags
        or "自营" in shop_title
    )

    item_url = _normalize_jd_url(raw.get("materialUrl") or "", fallback_item_id=item_id)

    parsed = {
        "item_id": item_id,
        "sku_id": sku_id,
        "title": title,
        "shop_title": shop_title or ("京东自营" if is_self_operated else "京东优选店铺"),
        "is_self_operated": is_self_operated,
        "final_price": final_price,
        "origin_price": origin_price,
        "coupon_amount": coupon_amount,
        "commission_rate": commission_rate,
        "commission_amount": commission_amount,
        "pict_url": pict_url,
        "image_url": pict_url,
        "small_images": small_images,
        "item_url": item_url,
        "sales_30d": int(raw.get("inOrderCount30Days") or 0),
    }
    if item_id:
        _LOCAL_DETAIL_CACHE[item_id] = dict(parsed, source="local_snapshot_cache")
    return parsed


# ==================== 3. 功能 1: 实时榜单选品 (官方直连 + 折淘客双通道) ====================
def get_rank_goods(sort_type: int = 1, page_size: int = 3) -> List[Dict[str, Any]]:
    """
    拉取京东实时榜单 (`jd.union.open.goods.rank.query`)
    :param sort_type: 1=2小时实时热推榜, 3=24小时今日热销榜
    :param page_size: 拉取条数

    【多级策略与注意事项】：
    1. 一级主通道：优先使用本地 `JD_APP_KEY` + `JD_APP_SECRET` 签名直连 `https://api.jd.com/routerjson`，
       完全不消耗折淘客配额，无任何第三方风控限制；
    2. 二级兜底通道：若未配置官方密钥或网络异常，自动回退至折淘客 `open_jd_union_open_goods_rank_query.ashx`；
    3. 榜单返回自带 `imgList` 轮播图数组与 `purchasePriceInfo` 券后价信息，解析时同步写入 `_LOCAL_DETAIL_CACHE`。
    """
    items: List[Dict[str, Any]] = []
    channel = "official_direct"

    # 一级主通道：直连京东宙斯官方网关
    if JD_APP_KEY and JD_APP_SECRET:
        try:
            param_json = {
                "RankGoodsReq": {
                    "rankId": 200000,
                    "sortType": sort_type,
                    "pageIndex": 1,
                    "pageSize": page_size,
                }
            }
            resp = call_jd_router("jd.union.open.goods.rank.query", param_json)
            query_result = _extract_jd_query_result(resp, "jd_union_open_goods_rank_query")
            if query_result.get("code") == 200 and isinstance(query_result.get("data"), list):
                items = query_result["data"]
        except Exception:
            items = []

    # 二级兜底通道：折淘客官方协议中继
    if not items:
        channel = "zhetaoke_relay"
        url = f"{ZTK_API_BASE}/open_jd_union_open_goods_rank_query.ashx"
        params = {
            "appkey": ZTK_APPKEY,
            "unionId": str(JD_UNION_ID),
            "sortType": sort_type,
            "rankId": 200000,
            "pageIndex": 1,
            "pageSize": page_size,
        }
        try:
            resp = requests.get(url, params=params, timeout=10).json()
            query_result = _extract_jd_query_result(resp, "jd_union_open_goods_rank_query")
            items = query_result.get("data", []) or []
        except Exception as e:
            return [{"error": f"拉取榜单异常: {e}"}]

    result = []
    for idx, it in enumerate(items[:page_size], 1):
        parsed = _parse_official_goods_item(it)
        parsed["rank"] = idx
        parsed["price"] = parsed["final_price"]
        parsed["channel"] = channel
        result.append(parsed)
    return result


# ==================== 4. 功能 2: 全网关键词搜索 (官方搜索主通道 + 全网搜索兜底) ====================
def search_goods(keyword: str, page: int = 1, page_size: int = 3) -> List[Dict[str, Any]]:
    """
    全网搜索京东推广商品
    :param keyword: 搜索关键词
    :param page: 页码 (从 1 开始)
    :param page_size: 每页条数

    【多级策略与注意事项】：
    1. 一级主通道 (`open_jing_union_open_goods_query.ashx`)：
       - 底层为京东联盟官方 `jd.union.open.goods.query` 接口，稳定性极高；
       - 不需要把短词（如“可乐”）重复拼接到 4 个字符；
       - 返回全网京东 CPS 商品（不仅限于折淘客自有券池），且自带 8~10 张高清轮播图 (`imageInfo.imageList`)；
       - 搜索同时自动将完整商品快照写入 `_LOCAL_DETAIL_CACHE`，后续查看详情实现 0 外部调用。
    2. 二级兜底通道 (`api_quanwang.ashx`)：
       - 仅在一级主通道网络异常或无返回时作为降级兜底；
       - 注意 `api_quanwang.ashx` 要求关键词长度 >= 4，且受到折淘客严格的反爬频控限制。
    """
    clean_kw = keyword.strip()
    if not clean_kw:
        return []

    # 一级主通道：京东联盟官方搜索协议 `open_jing_union_open_goods_query.ashx`
    official_url = f"{ZTK_API_BASE}/open_jing_union_open_goods_query.ashx"
    official_params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "keyword": clean_kw,
        "pageIndex": page,
        "pageSize": page_size,
    }
    try:
        resp = requests.get(official_url, params=official_params, timeout=10).json()
        query_result = _extract_jd_query_result(resp, "jd_union_open_goods_query")
        raw_list = query_result.get("data")
        if query_result.get("code") == 200 and isinstance(raw_list, list) and raw_list:
            result = []
            for raw in raw_list[:page_size]:
                if isinstance(raw, dict):
                    parsed = _parse_official_goods_item(raw)
                    parsed["channel"] = "official_goods_query"
                    result.append(parsed)
            if result:
                return result
    except Exception:
        pass

    # 二级兜底通道：折淘客 `api_quanwang.ashx` (注意短关键词需补齐至 4 字符)
    fallback_query = clean_kw
    while len(fallback_query) < 4:
        fallback_query += fallback_query

    fallback_url = f"{ZTK_API_BASE}/api_quanwang.ashx"
    fallback_params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "q": fallback_query,
        "page": page,
        "page_size": page_size,
        "sort": "new",
        "youquan": 1,
        "type": 2,
    }
    try:
        resp = requests.get(fallback_url, params=fallback_params, timeout=10).json()
        if resp.get("status") != 200 or not isinstance(resp.get("content"), list):
            return []

        result = []
        for raw in resp.get("content", []):
            item_id = str(raw.get("tao_id", "")).strip()
            pict_url = str(raw.get("pict_url", "")).strip()
            small_imgs = [img for img in str(raw.get("small_images") or "").split("|") if img]
            if pict_url and pict_url not in small_imgs:
                small_imgs.insert(0, pict_url)
            item = {
                "item_id": item_id,
                "title": raw.get("title") or raw.get("tao_title") or "",
                "final_price": str(raw.get("quanhou_jiage") or raw.get("size") or "0.00"),
                "origin_price": str(raw.get("size") or "0.00"),
                "coupon_amount": str(raw.get("coupon_info_money") or "0"),
                "commission_rate": str(raw.get("tkrate3") or "0"),
                "commission_amount": str(raw.get("tkfee3") or "0.00"),
                "is_self_operated": raw.get("user_type") == "1",
                "shop_title": raw.get("shop_title", ""),
                "item_url": _normalize_jd_url(raw.get("item_url", ""), fallback_item_id=item_id),
                "pict_url": pict_url,
                "small_images": small_imgs,
                "channel": "zhetaoke_quanwang_fallback",
            }
            if item_id:
                _LOCAL_DETAIL_CACHE[item_id] = dict(item, source="local_snapshot_cache")
            result.append(item)
        return result
    except Exception:
        return []


# ==================== 5. 功能 3: 商品详情展示 (三级高可用查询) ====================
def get_goods_detail(item_id: str, use_local_cache: bool = True) -> Dict[str, Any]:
    """
    根据商品 ID / 加密 itemId 查询完整商品详情

    【多级策略与注意事项】：
    1. 第 0 级 (本地快照池 `_LOCAL_DETAIL_CACHE`)：
       - 若该商品此前已在榜单 (`get_rank_goods`) 或搜索 (`search_goods`) 中出现过，直接返回已缓存的完整详情，
         做到 **0 外部详情 API 请求**。业务系统中严禁对京东开启后台盲目批量预取 (`can_prefetch_detail = False`)。
    2. 第 1 级 (京东联盟官方协议主通道)：
       - 优先调用 `open_jing_union_open_goods_query.ashx` (`keyword=item_id, pageIndex=1, pageSize=1`)，
         一步拿到价格、优惠券、佣金、轮播图 (`imageInfo.imageList`) 与店铺信息；
       - 若轮播图不足或需要获取真实数字 `mainSkuId`，补充调用官方 `open_jd_union_open_goods_bigfield_query.ashx`
         (`jd.union.open.goods.bigfield.query`)。
       - 注意：自 2024-12-03 起京东联盟接口仅支持加密 `itemId`，直接传 `skuIds` 会报 `code=411`。
    3. 第 2 级 (折淘客 `api_detail.ashx` 兜底)：
       - 仅在外部直接传入冷启动 `item_id` 且第 1 级未命中时使用。
    """
    clean_id = str(item_id or "").strip()
    if not clean_id:
        return {"error": "item_id 不能为空"}

    # 第 0 级：优先命中“列表即详情”本地快照池 (若需补齐真实数字 sku_id 或更多轮播图，仍可在缺省时走官方接口)
    if use_local_cache and clean_id in _LOCAL_DETAIL_CACHE:
        cached = dict(_LOCAL_DETAIL_CACHE[clean_id])
        if len(cached.get("small_images") or []) >= 2:
            return cached

    # 第 1 级 A：京东联盟官方商品查询 (`open_jing_union_open_goods_query.ashx`)
    official_url = f"{ZTK_API_BASE}/open_jing_union_open_goods_query.ashx"
    official_params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "keyword": clean_id,
        "pageIndex": 1,
        "pageSize": 1,
    }
    detail_data: Optional[Dict[str, Any]] = None
    try:
        resp = requests.get(official_url, params=official_params, timeout=10).json()
        query_result = _extract_jd_query_result(resp, "jd_union_open_goods_query")
        raw_list = query_result.get("data")
        if query_result.get("code") == 200 and isinstance(raw_list, list) and len(raw_list) == 1 and isinstance(raw_list[0], dict):
            detail_data = _parse_official_goods_item(raw_list[0], override_item_id=clean_id)
            detail_data["source"] = "official_goods_query"
    except Exception:
        detail_data = None

    # 第 1 级 B：京东联盟官方大字段详情 (`open_jd_union_open_goods_bigfield_query.ashx`) 补充轮播图与真实 mainSkuId
    if detail_data is None or len(detail_data.get("small_images") or []) <= 1 or not detail_data.get("sku_id"):
        bigfield_url = f"{ZTK_API_BASE}/open_jd_union_open_goods_bigfield_query.ashx"
        bigfield_params = {
            "appkey": ZTK_APPKEY,
            "unionId": str(JD_UNION_ID),
            "content": clean_id,
        }
        try:
            bf_resp = requests.get(bigfield_url, params=bigfield_params, timeout=10).json()
            bf_result = _extract_jd_query_result(bf_resp, "jd_union_open_goods_bigfield_query")
            bf_list = bf_result.get("data")
            if bf_result.get("code") == 200 and isinstance(bf_list, list) and bf_list and isinstance(bf_list[0], dict):
                bf = bf_list[0]
                bf_img_info = bf.get("imageInfo") if isinstance(bf.get("imageInfo"), dict) else {}
                bf_imgs = [
                    str(it.get("url")).strip()
                    for it in (bf_img_info.get("imageList") or [])
                    if isinstance(it, dict) and it.get("url")
                ]
                main_sku_id = str(bf.get("mainSkuId") or bf.get("productId") or "").strip()
                if detail_data is not None:
                    if bf_imgs and len(bf_imgs) > len(detail_data.get("small_images") or []):
                        detail_data["small_images"] = bf_imgs
                    if main_sku_id and not detail_data.get("sku_id"):
                        detail_data["sku_id"] = main_sku_id
                elif clean_id in _LOCAL_DETAIL_CACHE:
                    # 将缓存中的价格/佣金与 BigField 的高清轮播图合并
                    merged = dict(_LOCAL_DETAIL_CACHE[clean_id])
                    if bf_imgs:
                        merged["small_images"] = bf_imgs
                    if main_sku_id:
                        merged["sku_id"] = main_sku_id
                    merged["source"] = "local_cache+official_bigfield"
                    detail_data = merged
        except Exception:
            pass

    if detail_data is not None:
        _LOCAL_DETAIL_CACHE[clean_id] = detail_data
        return detail_data

    # 第 2 级兜底：折淘客 `api_detail.ashx` (仅当上述官方接口均未命中时调用)
    fallback_url = f"{ZTK_API_BASE}/api_detail.ashx"
    fallback_params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "tao_id": clean_id,
        "type": 1,
    }
    try:
        resp = requests.get(fallback_url, params=fallback_params, timeout=10).json()
        if resp.get("status") != 200 or not resp.get("content"):
            if clean_id in _LOCAL_DETAIL_CACHE:
                return _LOCAL_DETAIL_CACHE[clean_id]
            return {"error": f"未查询到详情: {resp}"}

        c = resp["content"][0]
        small_imgs = [img for img in (c.get("small_images") or "").split("|") if img]
        pict_url = c.get("pict_url", "")
        if pict_url and pict_url not in small_imgs:
            small_imgs.insert(0, pict_url)
        fallback_detail = {
            "item_id": str(c.get("tao_id") or clean_id),
            "title": c.get("title") or c.get("tao_title") or "",
            "shop_title": c.get("shop_title", ""),
            "is_self_operated": c.get("user_type") == "1",
            "final_price": str(c.get("quanhou_jiage") or c.get("size") or "0.00"),
            "origin_price": str(c.get("size") or "0.00"),
            "commission_rate": str(c.get("tkrate3") or "0"),
            "commission_amount": str(c.get("tkfee3") or "0.00"),
            "coupon_amount": str(c.get("coupon_info_money") or "0"),
            "pict_url": pict_url,
            "small_images": small_imgs,
            "item_url": _normalize_jd_url(c.get("item_url", ""), fallback_item_id=clean_id),
            "source": "zhetaoke_detail_fallback",
        }
        _LOCAL_DETAIL_CACHE[clean_id] = fallback_detail
        return fallback_detail
    except Exception as e:
        if clean_id in _LOCAL_DETAIL_CACHE:
            return _LOCAL_DETAIL_CACHE[clean_id]
        return {"error": f"详情查询异常: {e}"}


# ==================== 6. 功能 4: 统一推广转链 (支持 URL / 加密 itemId / 纯数字 SKU 直转) ====================
def normalize_material_id(material_url_or_id: str) -> str:
    """
    规范化转链入参 `materialId`：
    【重要注意事项】：
    1. 若传入完整京东 URL (`https://item.jd.com/...`、`https://jingfen.jd.com/...`、`https://u.jd.com/...`)，原样使用；
    2. 若传入纯数字 `skuId`（如 `"100012043978"`），直接传给官方转链会报 `301 不支持数字id转链`，
       必须自动包装为标准商详 URL `https://item.jd.com/{sku}.html`；
    3. 若传入京东联盟加密 `itemId`（如 `"6xXY8Ko5uwFbKV5W..."`），实测可直接作为 `materialId` 传入转链，
       无需为了拿 URL 先调一次详情接口！
    """
    raw = str(material_url_or_id or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith(("item.jd.com/", "jingfen.jd.com/", "u.jd.com/")):
        return f"https://{raw}"
    if raw.isdigit():
        return f"https://item.jd.com/{raw}.html"
    return raw


def convert_promotion_link(
    material_url: str,
    promoter_id: Union[int, str] = DEFAULT_POSITION_ID,
) -> Dict[str, Any]:
    """
    统一推广转链函数 (`open_jing_union_open_promotion_byunionid_get.ashx`)

    【核心归因机制与参数说明】：
    1. `promoter_id` 只要是纯数字即可（例如：推手在业务系统里的用户数字 ID `8899`，或默认数字 `3110104129`）；
    2. 京东联盟完全不需要在后台预先创建推广位，转链传入任意纯数字，订单成交后都会在 `positionId` 中原封不动透出；
    3. `material_url` 支持直接传入商品 URL、加密 `itemId` 或纯数字 `skuId`，彻底解除转链对详情接口的前置依赖。
    """
    clean_id = str(promoter_id).strip()
    if not clean_id.isdigit():
        clean_id = str(DEFAULT_POSITION_ID)

    normalized_material = normalize_material_id(material_url)
    url = f"{ZTK_API_BASE}/open_jing_union_open_promotion_byunionid_get.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "materialId": normalized_material,
        "chainType": "3",  # 返回长链 + 短链
        "positionId": clean_id,
    }

    try:
        resp = requests.get(url, params=params, timeout=10).json()
        result_data = _extract_jd_query_result(resp, "jd_union_open_promotion_byunionid_get")

        if result_data.get("code") != 200:
            return {"error": f"转链未成功: {result_data.get('message') or resp}"}

        pdata = result_data.get("data") or {}
        return {
            "success": True,
            "promoter_id": clean_id,
            "material_id": normalized_material,
            "short_url": pdata.get("shortURL", ""),
            "click_url": pdata.get("clickURL", ""),
            "j_command": pdata.get("jCommand", ""),
            "j_short_command": pdata.get("jShortCommand", ""),
        }
    except Exception as e:
        return {"error": f"转链异常: {e}"}


# ==================== 7. 功能 5: 增量订单拉取 (官方网关本地签名直连 + 折淘客中继双通道) ====================
def _parse_order_query_result(query_res: Dict[str, Any], channel: str) -> Dict[str, Any]:
    """解析 `jd.union.open.order.row.query` 业务响应字典。"""
    if query_res.get("code") != 200:
        return {
            "success": False,
            "orders": [],
            "channel": channel,
            "error": f"京东业务响应: [code={query_res.get('code')}] {query_res.get('message')}",
        }

    data_block = query_res.get("data")
    raw_rows: List[Dict[str, Any]] = []
    if isinstance(data_block, list):
        raw_rows = data_block
    elif isinstance(data_block, dict):
        resp_data = data_block.get("orderRowResp", [])
        raw_rows = resp_data if isinstance(resp_data, list) else ([resp_data] if resp_data else [])

    parsed_orders = []
    for o in raw_rows:
        v_code = int(o.get("validCode") or 0)
        parsed_orders.append({
            "order_id": str(o.get("orderId", "")),                           # 订单号
            "row_id": str(o.get("id", "")),                                  # 行单号
            "order_time": o.get("orderTime", ""),                            # 下单时间
            "modify_time": o.get("modifyTime", ""),                          # 更新时间
            "finish_time": o.get("finishTime", ""),                          # 完成时间
            "valid_code": v_code,                                            # 订单状态码
            "status_name": JD_ORDER_STATUS_MAP.get(v_code, f"状态{v_code}"),
            "sku_id": str(o.get("skuId", "")),                               # 商品SKU
            "sku_name": o.get("skuName", ""),                                # 商品名称
            "sku_num": int(o.get("skuNum") or 1),                            # 商品数量
            "price": str(o.get("price") or "0.00"),                          # 单价
            "estimate_cos_price": str(o.get("estimateCosPrice") or "0.00"),  # 计佣/实付金额
            "commission_rate": str(o.get("commissionRate") or "0"),          # 佣金比例
            "estimate_fee": str(o.get("estimateFee") or "0.00"),             # 预估佣金金额
            "actual_fee": str(o.get("actualFee") or "0.00"),                 # 实际佣金金额
            "sub_union_id": str(o.get("subUnionId", "")),                    # 子渠道标识
            "position_id": str(o.get("positionId", "")),                     # 订单归属的推广位数字ID
        })

    return {
        "success": True,
        "orders": parsed_orders,
        "count": len(parsed_orders),
        "has_more": query_res.get("hasMore") in (True, "true"),
        "channel": channel,
        "raw_response": query_res,
    }


def fetch_orders(
    start_time: str,
    end_time: str,
    query_type: int = 1,
    page_index: int = 1,
    page_size: int = 50,
) -> Dict[str, Any]:
    """
    拉取京东真实订单行数据并提取核心业务字段（纯粹拉取，不擅自计算提成）
    :param start_time: 开始时间 yyyy-MM-dd HH:mm:ss (跨度不超过1小时)
    :param end_time: 结束时间 yyyy-MM-dd HH:mm:ss
    :param query_type: 1=按订单下单时间, 3=按订单最后更新时间 (推荐)

    【多级策略与安全注意事项】：
    1. 一级主通道 (本地签名直连 `https://api.jd.com/routerjson`)：
       - 直接在本地使用 `JD_APP_KEY` 和 `JD_APP_SECRET` 计算 MD5 签名请求京东网关；
       - 避免把 `jd_app_secret` 通过明文 HTTP (`http://api.zhetaoke.com:20000`) 发送给第三方，且减少一次网络往返。
    2. 二级兜底通道 (`open_jing_union_openz_order_row_query.ashx`)：
       - 当直连发生网络波动时，回退至折淘客签名中继获取签名 URL 后再请求京东官方网关。
    """
    # 一级主通道：本地 MD5 签名直连京东宙斯官方网关
    if JD_APP_KEY and JD_APP_SECRET:
        try:
            param_json = {
                "orderReq": {
                    "pageIndex": int(page_index),
                    "pageSize": int(page_size),
                    "type": int(query_type),
                    "startTime": start_time,
                    "endTime": end_time,
                    "fields": "goodsInfo,categoryInfo",
                }
            }
            jd_resp = call_jd_router("jd.union.open.order.row.query", param_json, timeout=12)
            query_res = _extract_jd_query_result(jd_resp, "jd_union_open_order_row_query")
            if query_res:
                return _parse_order_query_result(query_res, channel="official_direct")
        except Exception:
            pass

    # 二级兜底通道：折淘客签名中继 -> 京东官方网关
    url = f"{ZTK_API_BASE}/open_jing_union_openz_order_row_query.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "jd_app_key": JD_APP_KEY,
        "jd_app_secret": JD_APP_SECRET,
        "startTime": start_time,
        "endTime": end_time,
        "type": str(query_type),
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "fields": "goodsInfo,categoryInfo",
    }
    try:
        resp = requests.get(url, params=params, timeout=12).json()
        if resp.get("status") != 200:
            return {"success": False, "orders": [], "error": f"签名中继返回错误: {resp.get('content') or resp}"}

        signed_gateway_url = resp.get("content")
        if not isinstance(signed_gateway_url, str) or not signed_gateway_url.startswith("https://"):
            return {"success": False, "orders": [], "error": f"无效签名URL: {signed_gateway_url}"}

        jd_resp = requests.get(signed_gateway_url, timeout=10).json()
        query_res = _extract_jd_query_result(jd_resp, "jd_union_open_order_row_query")
        return _parse_order_query_result(query_res, channel="zhetaoke_relay")
    except Exception as e:
        return {"success": False, "orders": [], "error": f"订单查询请求异常: {e}"}


# ==================== 8. 主流程执行 ====================
if __name__ == "__main__":
    print("=" * 84)
    print("🚀 京东联盟 API 基础功能验证程序 (多级高可用架构基准版)")
    print(f"👉 当前联盟ID: {JD_UNION_ID} | 站长默认推广位: {DEFAULT_POSITION_ID}")
    print("=" * 84)

    # 1. 榜单测试 (2小时榜 & 24小时今日热销榜)
    print("\n【1. 实时榜单拉取 (优先官方网关直连 -> 折淘客中继兜底)】")
    b2h = get_rank_goods(sort_type=1, page_size=2)
    print("  * 2小时实时热推榜 TOP 2:")
    for g in b2h:
        print(
            f"    - [{g.get('rank')}][{g.get('channel')}] {g.get('title', '')[:22]}... "
            f"| 现价: ¥{g.get('price')} | 佣金: ¥{g.get('commission_amount')} | 轮播图: {len(g.get('small_images', []))}张"
        )

    b24h = get_rank_goods(sort_type=3, page_size=2)
    print("  * 24小时今日热销榜 TOP 2:")
    for g in b24h:
        print(
            f"    - [{g.get('rank')}][{g.get('channel')}] {g.get('title', '')[:22]}... "
            f"| 现价: ¥{g.get('price')} | 佣金: ¥{g.get('commission_amount')} | 轮播图: {len(g.get('small_images', []))}张"
        )

    # 2. 关键词搜索测试
    print("\n【2. 全网关键词搜索 (优先官方 goods.query -> api_quanwang 兜底)】(搜索: '可乐')")
    s_items = search_goods("可乐", page=1, page_size=5)
    for idx, it in enumerate(s_items, 1):
        print(
            f"  * ({idx})[{it.get('channel')}] {it.get('title', '')[:22]}... "
            f"| 券后: ¥{it.get('final_price')} | 佣金率: {it.get('commission_rate')}% (¥{it.get('commission_amount')}) "
            f"| 轮播图: {len(it.get('small_images', []))}张"
        )

    sample_url = s_items[0]["item_url"] if s_items else "https://u.jd.com/egVs7wU"
    sample_sku = s_items[0]["item_id"] if s_items else "6xXY8Ko5uwFbKV5W"

    # 3. 商品详情展示测试 (验证第 0 级缓存命中 + 第 1 级官方查询 + BigField 补充)
    print(f"\n【3. 商品详情展示】(itemId: {sample_sku[:24]}...)")
    det_cached = get_goods_detail(sample_sku, use_local_cache=True)
    print(f"  * [第0级-列表快照命中] 来源: {det_cached.get('source')} | 轮播图: {len(det_cached.get('small_images', []))} 张")

    det_upstream = get_goods_detail(sample_sku, use_local_cache=False)
    if "error" not in det_upstream:
        print(
            f"  * [第1级-官方远端直查] 来源: {det_upstream.get('source')} | 店铺: {det_upstream.get('shop_title')} "
            f"| 自营: {det_upstream.get('is_self_operated')} | 券后价: ¥{det_upstream.get('final_price')} "
            f"| 佣金: ¥{det_upstream.get('commission_amount')} | 真实SKU: {det_upstream.get('sku_id') or '加密态'}"
        )
        print(f"  * 轮播图数: {len(det_upstream.get('small_images', []))} 张 | 主图: {det_upstream.get('pict_url', '')[:60]}...")
    else:
        print(f"  * 详情获取失败: {det_upstream.get('error')}")

    # 4. 统一转链逻辑测试 (验证 URL 转链 + 加密 itemId 免详情直转)
    print(f"\n【4. 统一推广转链测试】")
    link_default = convert_promotion_link(sample_url, promoter_id=DEFAULT_POSITION_ID)
    print(f"  👉 [示例 A: 传入商品 URL 转链 (promoter_id={DEFAULT_POSITION_ID})]")
    print(f"     短链接: {link_default.get('short_url')} | 京口令: {link_default.get('j_short_command') or link_default.get('j_command')}")

    link_direct_id = convert_promotion_link(sample_sku, promoter_id=8899)
    print(f"\n  👉 [示例 B: 直接传入加密 itemId 免详情转链 (promoter_id=8899)]")
    print(f"     推手数字ID: {link_direct_id.get('promoter_id')} | 专属短链: {link_direct_id.get('short_url')}")
    print(f"     专属精简口令: {link_direct_id.get('j_short_command')}")

    # 5. 增量订单拉取与核心字段打印
    print("\n【5. 增量订单拉取与核心字段打印 (优先本地签名直连官方网关 -> 折淘客中继兜底)】")
    test_start = "2026-11-06 05:30:00"
    test_end = "2026-11-06 06:30:00"
    print(f"  查询窗口: {test_start} 至 {test_end} (type=1: 按下单时间)")
    order_res = fetch_orders(start_time=test_start, end_time=test_end, query_type=1)

    if order_res.get("success"):
        orders = order_res.get("orders", [])
        print(f"  * 订单接口调用成功 [通道: {order_res.get('channel')}]，共拉取到 {len(orders)} 笔订单数据:")
        for o in orders:
            print("  -------------------------------------------------------------")
            print(f"  订单号: {o.get('order_id')} (行单号: {o.get('row_id')})")
            print(f"  下单时间: {o.get('order_time')} | 状态: {o.get('status_name')} (validCode={o.get('valid_code')})")
            print(f"  商品: [{o.get('sku_id')}] {o.get('sku_name')}")
            print(f"  付款金额: ¥{o.get('estimate_cos_price')} | 佣金比例: {o.get('commission_rate')}% | 联盟预估佣金: ¥{o.get('estimate_fee')}")
            print(f"  👉 归属推手/推广位数字ID(positionId): {o.get('position_id')}")
        if not orders:
            print("  ℹ️ 当前窗口拉取到 0 笔订单。")
    else:
        print(f"  * 订单接口返回提示: {order_res.get('error')}")

    print("=" * 84)
    print("自检流程完毕。")
    print("=" * 84)
