# -*- coding: utf-8 -*-
"""
京东联盟推手平台 API 自检与验证程序 (极简精炼版)
基于折淘客 (折京客) 及京东官方网关协议，剔除所有无关及未起作用的代码。

【核心功能】：
1. 榜单选品 (2小时实时榜 + 24小时今日热销榜)
2. 全网关键词搜索
3. 商品详情展示
4. 统一推广转链 (promoter_id 传入纯数字即可，免后台建位，纯数字原样透出直通归因)
5. 增量订单拉取与关键字段打印 (纯净拉取，不擅自计算分成)
"""

import os
import sys
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Union
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 1. 核心有效配置 ====================
# 折淘客 (折京客) 凭据
ZTK_APPKEY = os.environ.get("ZTK_APPKEY", "41ce110973f744118c1227d5f7f72b65")

# 京东联盟账号 (用于折淘客定位归属账号) 与 站长默认推广位
JD_UNION_ID = os.environ.get("JD_UNION_ID", "2035679405")           # 联盟纯数字ID
DEFAULT_POSITION_ID = os.environ.get("JD_POSITION_ID", "3110104129") # 站长自营默认推广位ID

# 京东开放平台应用凭据 (仅在订单行接口计算签名时使用)
JD_APP_KEY = os.environ.get("JD_APP_KEY", "45cb55a17ecbad6e91b004c0c34babe2")
JD_APP_SECRET = os.environ.get("JD_APP_SECRET", "8d501939a5084e62aeed3377f9d6e7ac")

# 折淘客 API 网关
ZTK_API_BASE = "http://api.zhetaoke.com:20000/api"

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
    28: "售后退款"
}


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
        "rankId": 200000,
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
    while len(query) < 4:
        query += query

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


# ==================== 5. 功能 4: 统一推广转链 (归因直通) ====================
def convert_promotion_link(
    material_url: str,
    promoter_id: Union[int, str] = DEFAULT_POSITION_ID
) -> Dict[str, Any]:
    """
    统一推广转链函数
    
    【核心归因机制与参数说明】：
    1. promoter_id 只要是纯数字即可（例如：推手在业务系统里的用户数字 ID 8899，或默认数字 3110104129）；
    2. 京东联盟完全不需要在后台预先创建推广位，转链传入任意纯数字，订单成交后都会在 positionId 中原封不动透出；
    3. 不再涉及任何 owner 特殊分支逻辑，所有推手与站长统一使用数字 ID 透传与归因。
    
    :param material_url: 商品链接 / 物料链接
    :param promoter_id: 推广标识（必须为纯数字，如 8899 或 "8899"，未传入时默认使用 DEFAULT_POSITION_ID）
    """
    clean_id = str(promoter_id).strip()
    if not clean_id.isdigit():
        # 若传入非纯数字，回退到默认推广位数字
        clean_id = str(DEFAULT_POSITION_ID)

    url = f"{ZTK_API_BASE}/open_jing_union_open_promotion_byunionid_get.ashx"
    params = {
        "appkey": ZTK_APPKEY,
        "unionId": str(JD_UNION_ID),
        "materialId": material_url,
        "chainType": "3",  # 返回长链 + 短链
        "positionId": clean_id
    }

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
            "promoter_id": clean_id,
            "short_url": pdata.get("shortURL", ""),
            "click_url": pdata.get("clickURL", ""),
            "j_command": pdata.get("jCommand", ""),
            "j_short_command": pdata.get("jShortCommand", "")
        }
    except Exception as e:
        return {"error": f"转链异常: {e}"}


# ==================== 6. 功能 5: 增量订单拉取与关键信息打印 ====================
def fetch_orders(
    start_time: str,
    end_time: str,
    query_type: int = 1,
    page_index: int = 1,
    page_size: int = 50
) -> Dict[str, Any]:
    """
    拉取京东真实订单行数据并提取核心业务字段（纯粹拉取，不擅自计算提成）
    :param start_time: 开始时间 yyyy-MM-dd HH:mm:ss (跨度不超过1小时)
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
        # 折淘客中继计算签名并返回官方 URL
        resp = requests.get(url, params=params, timeout=12).json()
        if resp.get("status") != 200:
            return {"success": False, "orders": [], "error": f"签名中继返回错误: {resp.get('content') or resp}"}

        signed_gateway_url = resp.get("content")
        if not isinstance(signed_gateway_url, str) or not signed_gateway_url.startswith("https://"):
            return {"success": False, "orders": [], "error": f"无效签名URL: {signed_gateway_url}"}

        # 本地直接向京东官方网关发起请求
        jd_resp = requests.get(signed_gateway_url, timeout=10).json()
        
        # 官方网关错误处理
        if "error_response" in jd_resp:
            err = jd_resp["error_response"]
            return {"success": False, "orders": [], "error": f"京东网关返回错误 [code={err.get('code')}]: {err.get('zh_desc') or err.get('en_desc')}"}

        query_wrapper = jd_resp.get("jd_union_open_order_row_query_responce", {}).get("queryResult", {})
        query_res = json.loads(query_wrapper) if isinstance(query_wrapper, str) else query_wrapper

        if query_res.get("code") != 200:
            return {"success": False, "orders": [], "error": f"京东业务响应: [code={query_res.get('code')}] {query_res.get('message')}"}

        data_block = query_res.get("data")
        raw_rows = []
        if isinstance(data_block, list):
            raw_rows = data_block
        elif isinstance(data_block, dict):
            resp_data = data_block.get("orderRowResp", [])
            raw_rows = resp_data if isinstance(resp_data, list) else [resp_data] if resp_data else []
        elif not data_block:
            raw_rows = []

        parsed_orders = []
        for o in raw_rows:
            v_code = int(o.get("validCode") or 0)
            parsed_orders.append({
                "order_id": str(o.get("orderId", "")),           # 订单号
                "row_id": str(o.get("id", "")),                  # 行单号
                "order_time": o.get("orderTime", ""),            # 下单时间
                "modify_time": o.get("modifyTime", ""),          # 更新时间
                "finish_time": o.get("finishTime", ""),          # 完成时间
                "valid_code": v_code,                            # 订单状态码
                "status_name": JD_ORDER_STATUS_MAP.get(v_code, f"状态{v_code}"),
                "sku_id": str(o.get("skuId", "")),               # 商品SKU
                "sku_name": o.get("skuName", ""),                # 商品名称
                "sku_num": int(o.get("skuNum") or 1),            # 商品数量
                "price": str(o.get("price") or "0.00"),          # 单价
                "estimate_cos_price": str(o.get("estimateCosPrice") or "0.00"), # 计佣/实付金额
                "commission_rate": str(o.get("commissionRate") or "0"),        # 佣金比例
                "estimate_fee": str(o.get("estimateFee") or "0.00"),           # 预估佣金金额
                "actual_fee": str(o.get("actualFee") or "0.00"),               # 实际佣金金额
                "sub_union_id": str(o.get("subUnionId", "")),    # 订单归属的推手标识 (如 'owner', 'user_8899')
                "position_id": str(o.get("positionId", ""))      # 订单归属的推广位ID
            })

        return {
            "success": True,
            "orders": parsed_orders,
            "count": len(parsed_orders),
            "has_more": query_res.get("hasMore") in (True, "true"),
            "raw_response": query_res
        }
    except Exception as e:
        return {"success": False, "orders": [], "error": f"订单查询请求异常: {e}"}


# ==================== 7. 主流程执行 ====================
if __name__ == "__main__":
    print("=" * 80)
    print("🚀 京东联盟 API 基础功能验证程序 (极简精炼版)")
    print(f"👉 当前联盟ID: {JD_UNION_ID} | 站长默认推广位: {DEFAULT_POSITION_ID}")
    print("=" * 80)

    # 1. 榜单测试 (2小时榜 & 24小时今日热销榜)
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
    print("\n【2. 全网关键词搜索】(搜索: '可乐')")
    s_items = search_goods("可乐", page=1, page_size=20)
    for idx, it in enumerate(s_items, 1):
        print(f"  * ({idx}) {it.get('title')[:22]}... | 券后: ¥{it.get('final_price')} | 佣金率: {it.get('commission_rate')}% (¥{it.get('commission_amount')})")

    sample_url = s_items[0]["item_url"] if s_items else "https://u.jd.com/egVs7wU"
    sample_sku = s_items[0]["item_id"] if s_items else "6xXY8Ko5uwFbKV5W"

    # 3. 商品详情展示测试
    print(f"\n【3. 商品详情展示】(SKU: {sample_sku[:20]}...)")
    det = get_goods_detail(sample_sku)
    if "error" not in det:
        print(f"  * 店铺: {det.get('shop_title')} | 自营: {det.get('is_self_operated')} | 券后价: ¥{det.get('final_price')} | 佣金: ¥{det.get('commission_amount')}")
        print(f"  * 轮播图数: {len(det.get('small_images', []))} 张 | 主图: {det.get('pict_url')[:60]}...")
    else:
        print(f"  * 详情获取失败: {det.get('error')}")

    # 4. 统一转链逻辑测试 (标识只要是纯数字即可，免去后台建位)
    print(f"\n【4. 统一推广转链测试】 {sample_url}")
    # 说明：promoter_id 只要是纯数字即可（如推手用户ID或站长默认位），京东无需后台预先创建推广位，订单会原样透出该数字作为归因依据
    
    # 示例 A: 默认/站长转链 (使用默认数字位)
    link_default = convert_promotion_link(sample_url, promoter_id=DEFAULT_POSITION_ID)
    print(f"  👉 [示例 A: 默认/站长转链 (promoter_id={DEFAULT_POSITION_ID})]")
    print(f"     推广数字ID: {link_default.get('promoter_id')}")
    print(f"     短链接: {link_default.get('short_url')}")
    print(f"     京口令: {link_default.get('j_short_command') or link_default.get('j_command')}")

    # 示例 B: 推手专属转链 (直接传入推手在系统中的纯数字ID，如 8899)
    link_promoter = convert_promotion_link(sample_url, promoter_id=8899)
    print(f"\n  👉 [示例 B: 推手专属转链 (直接传入推手纯数字ID，如 promoter_id=8899)]")
    print(f"     推手数字ID: {link_promoter.get('promoter_id')}")
    print(f"     专属短链接: {link_promoter.get('short_url')}")
    print(f"     专属精简口令: {link_promoter.get('j_short_command')}")
    print(f"     专属完整口令: {link_promoter.get('j_command')}")

    # 5. 增量订单拉取与核心字段打印 (无擅自分成计算)
    print("\n【5. 增量订单拉取与核心字段打印】")
    # 针对你刚才下的测试单对应时间段查询
    test_start = "2026-11-06 05:30:00"
    test_end = "2026-11-06 06:30:00"
    print(f"  查询窗口: {test_start} 至 {test_end} (type=1: 按下单时间)")
    order_res = fetch_orders(start_time=test_start, end_time=test_end, query_type=1)
    
    if order_res.get("success"):
        orders = order_res.get("orders", [])
        print(f"  * 订单接口调用成功，共拉取到 {len(orders)} 笔订单数据:")
        for o in orders:
            print(f"  -------------------------------------------------------------")
            print(f"  订单号: {o.get('order_id')} (行单号: {o.get('row_id')})")
            print(f"  下单时间: {o.get('order_time')} | 状态: {o.get('status_name')} (validCode={o.get('valid_code')})")
            print(f"  商品: [{o.get('sku_id')}] {o.get('sku_name')}")
            print(f"  付款金额: ¥{o.get('estimate_cos_price')} | 佣金比例: {o.get('commission_rate')}% | 联盟预估佣金: ¥{o.get('estimate_fee')}")
            print(f"  👉 归属推手/推广位数字ID(positionId): {o.get('position_id')}")
        if not orders:
            print("  ℹ️ 当前窗口拉取到 0 笔订单。")
    else:
        print(f"  * 订单接口返回提示: {order_res.get('error')}")

    print("=" * 80)
    print("自检流程完毕。")
    print("=" * 80)
