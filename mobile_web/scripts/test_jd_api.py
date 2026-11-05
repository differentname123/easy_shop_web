# -*- coding: utf-8 -*-
"""
京东联盟推手平台 API 自检与验证程序 (精简生产版)
基于折淘客 (折京客) 及京东官方网关协议，剔除所有无关及未起作用的代码。

【核心功能】：
1. 榜单选品 (2小时实时榜 + 24小时今日热销榜)
2. 全网关键词搜索
3. 商品详情展示
4. 双轨制推广转链 (方式1: 站长自营; 方式2: 带推手标识归因)
5. 增量订单拉取与 70% 结算引擎
"""

import os
import sys
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 1. 核心有效配置 ====================
# 折淘客 (折京客) 凭据
ZTK_APPKEY = os.environ.get("ZTK_APPKEY", "41ce110973f744118c1227d5f7f72b65")

# 京东联盟账号与站长自营推广位
JD_UNION_ID = os.environ.get("JD_UNION_ID", "2035679405")           # 联盟纯数字ID
JD_POSITION_ID = os.environ.get("JD_POSITION_ID", "3110104129")     # 站长自营固定推广位ID

# 京东开放平台应用凭据 (仅订单行查询使用)
JD_APP_KEY = os.environ.get("JD_APP_KEY", "45cb55a17ecbad6e91b004c0c34babe2")
JD_APP_SECRET = os.environ.get("JD_APP_SECRET", "8d501939a5084e62aeed3377f9d6e7ac")

# 折淘客 API 网关
ZTK_API_BASE = "http://api.zhetaoke.com:20000/api"


# ==================== 2. 功能 1: 实时榜单选品 ====================
def get_rank_goods(sort_type: int = 1, page_size: int = 3) -> List[Dict[str, Any]]:
    """
    拉取京东实时榜单
    :param sort_type: 1=2小时实时热推榜, 3=24小时今日热销榜
    :param page_size: 拉取条数
    """
    url = f"{ZTK_API_BASE}/open_jd_union_open_goods_rank_query.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "sortType": sort_type,
        "rankId": 200000,  # 全部分类
        "pageIndex": 1,
        "pageSize": page_size
    }
    try:
        resp = requests.get(url, params=params, timeout=10).json()
        raw_res = resp.get("jd_union_open_goods_rank_query_responce", {}).get("queryResult", {})
        query_result = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
        items = query_result.get("data", []) or []
        
        result = []
        for idx, it in enumerate(items[:page_size], 1):
            price = str(it.get("wlprice") or it.get("purchasePriceInfo", {}).get("price") or "0.00")
            item_id = str(it.get("itemId") or it.get("skuId") or "")
            result.append({
                "rank": idx,
                "item_id": item_id,
                "title": it.get("skuName", ""),
                "price": price,
                "commission_rate": str(it.get("commissionShare") or "0"),
                "commission_amount": str(it.get("commission") or "0.00"),
                "image_url": it.get("imageUrl", ""),
                "item_url": f"https://item.jd.com/{item_id}.html" if item_id else ""
            })
        return result
    except Exception as e:
        return [{"error": f"拉取榜单异常: {e}"}]


# ==================== 3. 功能 2: 全网关键词搜索 ====================
def search_goods(keyword: str, page: int = 1, page_size: int = 3) -> List[Dict[str, Any]]:
    """
    全网搜索京东推广商品
    """
    query = keyword.strip()
    if len(query) < 4:
        query = f"{query}热销商品"

    url = f"{ZTK_API_BASE}/api_quanwang.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "q": query,
        "page": page,
        "page_size": page_size,
        "sort": "new",
        "youquan": 1,
        "type": 2
    }
    try:
        resp = requests.get(url, params=params, timeout=10).json()
        if resp.get("status") != 200 or not isinstance(resp.get("content"), list):
            return []

        result = []
        for raw in resp.get("content", []):
            result.append({
                "item_id": str(raw.get("tao_id", "")),
                "title": raw.get("title") or raw.get("tao_title") or "",
                "final_price": str(raw.get("quanhou_jiage") or raw.get("size") or "0.00"),
                "origin_price": str(raw.get("size") or "0.00"),
                "commission_rate": str(raw.get("tkrate3") or "0"),
                "commission_amount": str(raw.get("tkfee3") or "0.00"),
                "is_self_operated": raw.get("user_type") == "1",
                "shop_title": raw.get("shop_title", ""),
                "item_url": raw.get("item_url", ""),
                "pict_url": raw.get("pict_url", "")
            })
        return result
    except Exception:
        return []


# ==================== 4. 功能 3: 官方商品详情展示 ====================
def get_goods_detail(item_id: str) -> Dict[str, Any]:
    """
    根据商品 ID / 物料 ID 查询完整商品详情
    """
    url = f"{ZTK_API_BASE}/api_detail.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "tao_id": item_id,
        "type": 1
    }
    try:
        resp = requests.get(url, params=params, timeout=10).json()
        if resp.get("status") != 200 or not resp.get("content"):
            return {"error": f"未查询到详情: {resp}"}

        c = resp["content"][0]
        small_imgs = [img for img in (c.get("small_images") or "").split("|") if img]
        return {
            "item_id": str(c.get("tao_id", "")),
            "title": c.get("title") or c.get("tao_title") or "",
            "shop_title": c.get("shop_title", ""),
            "is_self_operated": c.get("user_type") == "1",
            "final_price": str(c.get("quanhou_jiage") or c.get("size") or "0.00"),
            "origin_price": str(c.get("size") or "0.00"),
            "commission_rate": str(c.get("tkrate3") or "0"),
            "commission_amount": str(c.get("tkfee3") or "0.00"),
            "coupon_amount": str(c.get("coupon_info_money") or "0"),
            "pict_url": c.get("pict_url", ""),
            "small_images": small_imgs
        }
    except Exception as e:
        return {"error": f"详情查询异常: {e}"}


# ==================== 5. 功能 4: 推广转链 (两种明确方式) ====================
def convert_link_station_owner(material_url: str) -> Dict[str, Any]:
    """
    方式 1: 站长自营推广 (无推手归因)
    使用站长在京东后台创建的官方媒体固定推广位
    """
    return _convert_promotion_core(
        material_url=material_url,
        sub_union_id=None,
        position_id=JD_POSITION_ID
    )


def convert_link_for_promoter(material_url: str, promoter_id: str, custom_position_id: str = None) -> Dict[str, Any]:
    """
    方式 2: 带推手标识的专属推广 (100% 精准归因)
    无需推手做任何授权，通过注入 subUnionId 和可选 positionId 实现归因
    :param material_url: 商品链接 / 物料链接
    :param promoter_id: 推手用户唯一标识 (如 'user_8899', 系统 UID)
    :param custom_position_id: 可选自定义纯数字推广位 (如 '998877')
    """
    clean_promoter = str(promoter_id).strip()
    return _convert_promotion_core(
        material_url=material_url,
        sub_union_id=clean_promoter,
        position_id=custom_position_id or "998877"
    )


def _convert_promotion_core(material_url: str, sub_union_id: Optional[str], position_id: str) -> Dict[str, Any]:
    """底层真实转链引擎"""
    url = f"{ZTK_API_BASE}/open_jing_union_open_promotion_byunionid_get.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "materialId": material_url,
        "chainType": "3"  # 返回长链 + 短链
    }
    if position_id:
        params["positionId"] = str(position_id)
    if sub_union_id:
        params["subUnionId"] = str(sub_union_id)

    try:
        resp = requests.get(url, params=params, timeout=10).json()
        resp_obj = resp.get("jd_union_open_promotion_byunionid_get_response", {})
        raw_res = resp_obj.get("result", "{}")
        result_data = json.loads(raw_res) if isinstance(raw_res, str) else raw_res

        if result_data.get("code") != 200:
            return {"error": f"转链未成功: {result_data.get('message')}"}

        pdata = result_data.get("data", {})
        return {
            "success": True,
            "is_promoter_mode": bool(sub_union_id),
            "sub_union_id": sub_union_id,
            "position_id": position_id,
            "short_url": pdata.get("shortURL", ""),
            "click_url": pdata.get("clickURL", ""),
            "j_command": pdata.get("jCommand", ""),
            "j_short_command": pdata.get("jShortCommand", ""),
            "share_token": pdata.get("jShortCommand") or pdata.get("jCommand") or pdata.get("shortURL", "")
        }
    except Exception as e:
        return {"error": f"转链异常: {e}"}


# ==================== 6. 功能 5: 增量订单拉取与 70% 结算 ====================
def fetch_orders(start_time: str, end_time: str, query_type: int = 1, page_index: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """
    拉取京东真实订单行数据
    :param start_time: 开始时间 yyyy-MM-dd HH:mm:ss (与结束时间跨度不超过1小时)
    :param end_time: 结束时间 yyyy-MM-dd HH:mm:ss
    :param query_type: 1=按订单下单时间, 3=按订单最后更新时间 (推荐)
    """
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
        "fields": "goodsInfo,categoryInfo"
    }
    try:
        # 折淘客中继计算签名，返回官方请求 URL
        resp = requests.get(url, params=params, timeout=12).json()
        if resp.get("status") != 200:
            return {"success": False, "orders": [], "error": f"签名中继返回错误: {resp.get('content') or resp}"}

        signed_gateway_url = resp.get("content")
        if not isinstance(signed_gateway_url, str) or not signed_gateway_url.startswith("https://"):
            return {"success": False, "orders": [], "error": f"无效签名URL: {signed_gateway_url}"}

        # 本地直接向京东官方网关发起请求
        jd_resp = requests.get(signed_gateway_url, timeout=10).json()
        
        # 官方网关错误检测 (如签名无效等)
        if "error_response" in jd_resp:
            err = jd_resp["error_response"]
            return {"success": False, "orders": [], "error": f"京东网关返回错误: [code={err.get('code')}] {err.get('zh_desc') or err.get('en_desc')}"}

        # 官方业务返回检测
        query_wrapper = jd_resp.get("jd_union_open_order_row_query_responce", {}).get("queryResult", {})
        query_res = json.loads(query_wrapper) if isinstance(query_wrapper, str) else query_wrapper

        if query_res.get("code") != 200:
            return {"success": False, "orders": [], "error": f"京东业务响应: [code={query_res.get('code')}] {query_res.get('message')}"}

        raw_rows = query_res.get("data", {}).get("orderRowResp", [])
        if isinstance(raw_rows, dict):
            raw_rows = [raw_rows]

        parsed_orders = []
        for o in raw_rows:
            try:
                estimate_fee = float(o.get("estimateFee") or 0.0)
                actual_fee = float(o.get("actualFee") or 0.0)
            except (TypeError, ValueError):
                estimate_fee, actual_fee = 0.0, 0.0

            parsed_orders.append({
                "order_id": str(o.get("orderId", "")),
                "row_id": str(o.get("id", "")),
                "sku_id": str(o.get("skuId", "")),
                "sku_name": o.get("skuName", ""),
                "sub_union_id": str(o.get("subUnionId", "")),    # 订单透出的推手标识
                "position_id": str(o.get("positionId", "")),    # 订单透出的推广位
                "valid_code": int(o.get("validCode") or 0),
                "order_time": o.get("orderTime", ""),
                "finish_time": o.get("finishTime", ""),
                "modify_time": o.get("modifyTime", ""),
                "estimate_cos_price": str(o.get("estimateCosPrice") or "0.00"),
                "estimate_fee": str(estimate_fee),
                "actual_fee": str(actual_fee),
                # 70% 推手分成结算
                "settlement": {
                    "promoter_share_profit": round(estimate_fee * 0.70, 2),
                    "platform_share_profit": round(estimate_fee * 0.30, 2)
                }
            })

        return {
            "success": True,
            "orders": parsed_orders,
            "count": len(parsed_orders),
            "has_more": query_res.get("hasMore") in (True, "true")
        }
    except Exception as e:
        return {"success": False, "orders": [], "error": f"订单查询请求异常: {e}"}


# ==================== 7. 主流程执行 ====================
if __name__ == "__main__":
    print("=" * 80)
    print("🚀 京东联盟 API 基础功能验证程序 (精简版)")
    print(f"👉 当前联盟ID: {JD_UNION_ID} | 站长官方推广位: {JD_POSITION_ID}")
    print("=" * 80)

    # 1. 榜单测试 (2小时榜 & 24小时今日榜)
    print("\n【1. 实时榜单拉取】")
    b2h = get_rank_goods(sort_type=1, page_size=2)
    print("  * 2小时实时热推榜 TOP 2:")
    for g in b2h:
        print(f"    - [{g.get('rank')}] {g.get('title')[:22]}... | 现价: ¥{g.get('price')} | 预估佣金: ¥{g.get('commission_amount')}")

    b24h = get_rank_goods(sort_type=3, page_size=2)
    print("  * 24小时今日热销榜 TOP 2:")
    for g in b24h:
        print(f"    - [{g.get('rank')}] {g.get('title')[:22]}... | 现价: ¥{g.get('price')} | 预估佣金: ¥{g.get('commission_amount')}")

    # 2. 关键词搜索测试
    print("\n【2. 全网关键词搜索】(搜索: '心相印抽纸')")
    s_items = search_goods("心相印抽纸", page=1, page_size=2)
    for idx, it in enumerate(s_items, 1):
        print(f"  * ({idx}) {it.get('title')[:22]}... | 券后: ¥{it.get('final_price')} | 佣金率: {it.get('commission_rate')}% (¥{it.get('commission_amount')})")

    # 选取一件真实物料
    sample_url = s_items[0]["item_url"] if s_items else "https://jingfen.jd.com/detail/6xXK8Ko5uwFbKWz.html"
    sample_sku = s_items[0]["item_id"] if s_items else "6xXY8Ko5uwFbKV5W"

    # 3. 商品详情展示测试
    print(f"\n【3. 商品详情展示】(SKU: {sample_sku[:20]}...)")
    det = get_goods_detail(sample_sku)
    if "error" not in det:
        print(f"  * 店铺: {det.get('shop_title')} | 自营: {det.get('is_self_operated')} | 券后价: ¥{det.get('final_price')} | 佣金: ¥{det.get('commission_amount')}")
        print(f"  * 轮播图数: {len(det.get('small_images', []))} 张 | 主图: {det.get('pict_url')[:60]}...")
    else:
        print(f"  * 详情获取失败: {det.get('error')}")

    # 4. 推广转链 (两种核心方式)
    print("\n【4. 推广转链对比 (极简双轨制)】")
    
    # 方式 1: 站长自营推广 (挂站长官方推广位)
    link_owner = convert_link_station_owner(sample_url)
    print("  👉 [方式 1: 站长自营推广]")
    print(f"     推广位: {link_owner.get('position_id')}")
    print(f"     短链接: {link_owner.get('short_url')}")
    print(f"     京口令: {link_owner.get('share_token')}")

    # 方式 2: 带推手标识专属推广 (推手 user_8899)
    link_promoter = convert_link_for_promoter(sample_url, promoter_id="user_8899", custom_position_id="998877")
    print("\n  👉 [方式 2: 推手专属推广 (带标识归因)]")
    print(f"     推手归因标识 (subUnionId): {link_promoter.get('sub_union_id')} | 自定义推广位: {link_promoter.get('position_id')}")
    print(f"     专属短链接: {link_promoter.get('short_url')}")
    print(f"     专属精简口令: {link_promoter.get('j_short_command')}")
    print(f"     专属完整口令: {link_promoter.get('j_command')}")

    # 5. 增量订单拉取与 70% 结算
    print("\n【5. 增量订单拉取与 70% 结算】")
    # 针对 04:00 至 05:00 窗口查询
    order_res = fetch_orders(start_time="2026-11-06 04:00:00", end_time="2026-11-06 05:00:00", query_type=1)
    if order_res.get("success"):
        orders = order_res.get("orders", [])
        print(f"  * 接口调用成功，拉取到 {len(orders)} 笔订单数据:")
        for o in orders:
            st = o.get("settlement", {})
            print(f"    - 订单号: {o.get('order_id')} | SKU: {o.get('sku_name')[:15]}...")
            print(f"      付款金额: ¥{o.get('estimate_cos_price')} | 联盟佣金: ¥{o.get('estimate_fee')}")
            print(f"      👉 归属推手: {o.get('sub_union_id')} | 推广位: {o.get('position_id')}")
            print(f"      👉 推手70%提成: ¥{st.get('promoter_share_profit')} | 平台留存: ¥{st.get('platform_share_profit')}")
        if not orders:
            print("    ℹ️ 当前时间窗口返回 0 笔订单 (说明: 京东官方订单行接口通常在下单后约 5~15 分钟完成批次同步)。")
    else:
        print(f"  * 订单接口返回提示: {order_res.get('error')}")

    print("=" * 80)
    print("自检流程完毕。")
    print("=" * 80)
