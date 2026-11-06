# -*- coding: utf-8 -*-
"""
拼多多 (多多进宝 DDK) 核心能力标准基线脚本 (生产级精简版)
================================================================================
本脚本作为全项目拼多多 API 的唯一权威基线，维护并验证项目用到的全部 6 项核心 API 操作：
1. 获取热销榜单/频道推荐商品 (pdd.ddk.goods.recommend.get: channel_type=5 实时热销 / 1 今日爆款)
2. 关键词搜索商品 (pdd.ddk.goods.search)
3. 获取商品详情及短链探测降级 (pdd.ddk.goods.detail + pdd.ddk.goods.zs.unit.url.gen 探测)
4. 三层智能兜底转链 (pdd.ddk.rp.prom.url.generate[23] -> pdd.ddk.goods.promotion.url.generate -> zs.unit.url.gen)
5. 推手授权备案查询与生成 (pdd.ddk.member.authority.query + pdd.ddk.rp.prom.url.generate[10])
6. 增量订单拉取与状态清洗 (pdd.ddk.order.list.increment.get)
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# 确保 Windows 终端下能够正常输出中文与特殊符号
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PDD_API_URL = "https://gw-api.pinduoduo.com/api/router"

SERVICE_TAG_MAP = {
    1: "全场包邮", 2: "七天退换", 3: "退货包运费", 4: "送货入户并安装",
    5: "送货入户", 6: "电子发票", 7: "诚信发货", 8: "缺重包赔",
    9: "坏果包赔", 10: "果重保证", 11: "闪电退款", 12: "24小时发货",
    13: "48小时发货", 15: "假一罚十", 16: "贴心服务", 17: "顺丰包邮",
    18: "只换不修", 19: "全国联保", 20: "分期付款", 21: "极速退款",
    22: "上门换新", 23: "材质保障", 24: "精准物流", 29: "预约配送",
    31: "电器城服务", 32: "顺丰冷链", 1000001: "正品发票",
}

UNIFIED_TAG_MAP = {
    24: "百亿补贴", 7: "百亿补贴", 10412: "百亿补贴",
    10564: "千万补贴", 6: "千万神券", 31: "品牌黑标",
}


# ==============================================================================
# 0. 配置加载与基础签名通信协议
# ==============================================================================
def load_local_config() -> dict:
    """自动向上寻找并加载 config.local.json 配置文件"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        candidate = os.path.join(current_dir, "config.local.json")
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        current_dir = os.path.dirname(current_dir)
    return {}


_cfg = load_local_config()
PDD_CLIENT_ID = _cfg.get("myself_pdd_client_id", "")
PDD_CLIENT_SECRET = _cfg.get("myself_pdd_client_secret", "")
PDD_PID = _cfg.get("myself_pdd_pid", "")


def generate_pdd_sign(params: Dict[str, Any], client_secret: str) -> str:
    """生成拼多多开放平台 MD5 签名（首尾拼接 client_secret，按 key 字典序升序）"""
    sorted_keys = sorted(params.keys())
    sign_str = client_secret
    for k in sorted_keys:
        v = params[k]
        if v is not None and k != "sign":
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            sign_str += f"{k}{v}"
    sign_str += client_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def call_pdd_api(
    client_id: str,
    client_secret: str,
    api_type: str,
    biz_params: Optional[Dict[str, Any]] = None,
    timeout: int = 12,
) -> Optional[Dict[str, Any]]:
    """通用拼多多网关请求封装"""
    if not client_id or not client_secret:
        return {"error_response": {"error_msg": "拼多多 API 密钥未配置", "error_code": "CONFIG_ERROR"}}

    payload: Dict[str, Any] = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
    }
    if biz_params:
        for k, v in biz_params.items():
            if v is not None:
                if isinstance(v, bool):
                    payload[k] = "true" if v else "false"
                elif isinstance(v, (dict, list)):
                    payload[k] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                else:
                    payload[k] = str(v)

    payload["sign"] = generate_pdd_sign(payload, client_secret)
    try:
        resp = requests.post(PDD_API_URL, data=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error_response": {"error_msg": f"网络或解析异常: {e}", "error_code": "NETWORK_ERROR"}}


def _build_custom_params(uid: Optional[str] = None) -> str:
    """构建拼多多多级归因 custom_parameters JSON 字符串"""
    safe_uid = str(uid or "").strip() or "default_user"
    return json.dumps({"uid": safe_uid}, ensure_ascii=False, separators=(",", ":"))


def clean_pdd_goods_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """将拼多多原始商品字典（分）清洗为标准业务字段（元）"""
    cleaned = dict(item)
    min_group_price = item.get("min_group_price", 0) or 0
    min_normal_price = item.get("min_normal_price", 0) or 0
    coupon_discount = item.get("coupon_discount", 0) or 0
    promotion_rate = item.get("promotion_rate", 0) or 0
    subsidy_amount = item.get("subsidy_amount", 0) or 0

    group_yuan = round(min_group_price / 100.0, 2)
    normal_yuan = round(min_normal_price / 100.0, 2)
    coupon_yuan = round(coupon_discount / 100.0, 2)
    subsidy_yuan = round(subsidy_amount / 100.0, 2)
    final_price_yuan = round(max(group_yuan - coupon_yuan, 0.0), 2)
    comm_rate_pct = round(promotion_rate / 10.0, 2)
    est_comm_yuan = round(final_price_yuan * (promotion_rate / 1000.0), 2)

    raw_goods_id = str(item.get("goods_id") or "").strip()
    if raw_goods_id in ("0", "None", "null"):
        raw_goods_id = ""
    raw_goods_sign = str(item.get("goods_sign") or "").strip()
    if raw_goods_sign in ("0", "None", "null"):
        raw_goods_sign = ""

    cleaned.update({
        "platform": "pdd",
        "goods_id": raw_goods_id or raw_goods_sign,
        "goods_sign": raw_goods_sign,
        "name": item.get("goods_name", ""),
        "image_url": item.get("goods_image_url") or item.get("goods_thumbnail_url", ""),
        "min_group_price_yuan": group_yuan,
        "min_normal_price_yuan": normal_yuan,
        "coupon_discount_yuan": coupon_yuan,
        "subsidy_amount_yuan": subsidy_yuan,
        "final_price_yuan": final_price_yuan,
        "price": final_price_yuan,
        "original_price": group_yuan,
        "coupon_amount": coupon_yuan,
        "has_coupon": bool(item.get("has_coupon", False) or coupon_discount > 0),
        "promotion_rate_percent": comm_rate_pct,
        "estimated_commission_yuan": est_comm_yuan,
        "commission_rate": comm_rate_pct,
        "commission_amount": est_comm_yuan,
        "sales_tip": str(item.get("sales_tip") or "0"),
        "mall_name": item.get("mall_name", ""),
        "brand_name": item.get("brand_name", ""),
        "category_name": item.get("opt_name") or item.get("cat_name", ""),
        "search_id": item.get("search_id", ""),
    })
    return cleaned


# ==============================================================================
# 核心功能 1：获取榜单/推荐商品 (pdd.ddk.goods.recommend.get)
# ==============================================================================
def get_pdd_recommend_goods(
    channel_type: int = 5,
    offset: int = 0,
    limit: int = 20,
    pid: Optional[str] = None,
    uid: Optional[str] = None,
    list_id: Optional[str] = None,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """
    获取多多进宝官方频道推荐/热销榜单商品：
    - channel_type=5: 实时热销榜
    - channel_type=1: 今日爆款榜
    """
    biz_params: Dict[str, Any] = {
        "channel_type": channel_type,
        "offset": offset,
        "limit": limit,
        "pid": pid or PDD_PID,
        "custom_parameters": _build_custom_params(uid),
    }
    if list_id:
        biz_params["list_id"] = list_id

    res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.recommend.get", biz_params)
    if not res or "error_response" in res:
        err = (res or {}).get("error_response", {})
        return {"status": "error", "error_msg": err.get("error_msg", "获取拼多多推荐列表失败")}

    body = res.get("goods_basic_detail_response", {})
    raw_list = body.get("list") or []
    cleaned_list = [clean_pdd_goods_item(item) for item in raw_list]
    return {
        "status": "success",
        "data": cleaned_list,
        "total_count": body.get("total", len(cleaned_list)),
        "list_id": body.get("list_id", ""),
        "search_id": body.get("search_id", ""),
        "next_offset": offset + len(cleaned_list),
        "has_more": len(cleaned_list) >= limit,
    }


# ==============================================================================
# 核心功能 2：关键词搜索商品 (pdd.ddk.goods.search)
# ==============================================================================
def search_pdd_goods(
    keyword: str,
    page: int = 1,
    page_size: int = 20,
    sort_type: int = 0,
    with_coupon: bool = False,
    pid: Optional[str] = None,
    uid: Optional[str] = None,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """拼多多全网关键词/短链/标题搜索 (pdd.ddk.goods.search)"""
    biz_params: Dict[str, Any] = {
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
        "sort_type": sort_type,
        "with_coupon": with_coupon,
        "pid": pid or PDD_PID,
        "custom_parameters": _build_custom_params(uid),
    }
    res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", biz_params)
    if not res or "error_response" in res:
        err = (res or {}).get("error_response", {})
        return {"status": "error", "error_msg": err.get("error_msg", "拼多多商品搜索失败")}

    body = res.get("goods_search_response", {})
    raw_list = body.get("goods_list") or []
    cleaned_list = [clean_pdd_goods_item(item) for item in raw_list]
    return {
        "status": "success",
        "data": cleaned_list,
        "total_count": body.get("total_count", len(cleaned_list)),
        "list_id": body.get("list_id", ""),
        "search_id": body.get("search_id", ""),
    }


# ==============================================================================
# 核心功能 3：获取商品详情及短链探测降级 (pdd.ddk.goods.detail)
# ==============================================================================
def _resolve_goods_sign_by_id(
    client_id: str,
    client_secret: str,
    goods_id: str,
    pid: Optional[str] = None,
) -> Optional[str]:
    """当仅持有纯数字 goods_id 时，借助 pdd.ddk.goods.zs.unit.url.gen 短链与搜索反查 goods_sign"""
    use_pid = pid or PDD_PID
    if use_pid:
        raw_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
        zs_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", {
            "source_url": raw_url,
            "pid": use_pid,
        })
        short_url = (
            (zs_res or {}).get("goods_zs_unit_generate_response", {}).get("short_url")
            or (zs_res or {}).get("goods_zs_unit_generate_response", {}).get("url")
        )
        if short_url:
            s_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {"keyword": short_url, "page_size": 5})
            for g in (s_res or {}).get("goods_search_response", {}).get("goods_list") or []:
                if g.get("goods_sign"):
                    return str(g["goods_sign"])

    s_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {"keyword": str(goods_id), "page_size": 5})
    for g in (s_res or {}).get("goods_search_response", {}).get("goods_list") or []:
        if str(g.get("goods_id")) == str(goods_id) and g.get("goods_sign"):
            return str(g["goods_sign"])
    return None


def get_pdd_goods_detail(
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
    goods_sign: Optional[str] = None,
    goods_id: Optional[Any] = None,
    pid: Optional[str] = None,
    custom_parameters: Optional[Any] = None,
    search_id: Optional[str] = None,
    goods_img_type: Optional[int] = None,
    need_sku_info: bool = True,
) -> Dict[str, Any]:
    """查询拼多多商品详情 (pdd.ddk.goods.detail)，并在仅有 goods_id 时自动触发短链探测降级"""
    if not goods_sign and not goods_id:
        return {"status": "error", "error_msg": "必须提供 goods_sign 或 goods_id 之一", "error_code": -1}

    biz_params: Dict[str, Any] = {"need_sku_info": need_sku_info}
    if goods_sign:
        biz_params["goods_sign"] = str(goods_sign).strip()
    elif goods_id is not None:
        biz_params["goods_id_list"] = [int(goods_id)] if str(goods_id).isdigit() else [str(goods_id)]

    if pid:
        biz_params["pid"] = pid
    if custom_parameters:
        biz_params["custom_parameters"] = custom_parameters
    if search_id:
        biz_params["search_id"] = search_id
    if goods_img_type is not None:
        biz_params["goods_img_type"] = goods_img_type

    raw_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", biz_params)

    # 若仅传了 goods_id 且被网关拒绝，自动通过短链+搜索探测出 goods_sign 重试
    if raw_res and "error_response" in raw_res and not goods_sign and goods_id:
        resolved_sign = _resolve_goods_sign_by_id(client_id, client_secret, str(goods_id), pid=pid)
        if resolved_sign:
            biz_params.pop("goods_id_list", None)
            biz_params["goods_sign"] = resolved_sign
            raw_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", biz_params)

    if not raw_res or "error_response" in raw_res:
        err = (raw_res or {}).get("error_response", {})
        return {
            "status": "error",
            "error_code": err.get("error_code", -1),
            "sub_code": err.get("sub_code", ""),
            "error_msg": err.get("error_msg", "获取商品详情失败"),
            "sub_msg": err.get("sub_msg", ""),
        }

    details = raw_res.get("goods_detail_response", {}).get("goods_details") or []
    if not details:
        return {"status": "error", "error_code": 404, "error_msg": "未找到对应的拼多多商品详情"}

    cleaned = clean_pdd_goods_item(details[0])
    gallery = cleaned.get("goods_gallery_urls") or []
    if not gallery and cleaned.get("image_url"):
        gallery = [cleaned["image_url"]]

    raw_utags = cleaned.get("unified_tags") or []
    parsed_utags = []
    for t in raw_utags:
        label = UNIFIED_TAG_MAP.get(t)
        if label and label not in parsed_utags:
            parsed_utags.append(label)

    raw_stags = cleaned.get("service_tags") or []
    parsed_services = []
    for s in raw_stags:
        s_label = SERVICE_TAG_MAP.get(s)
        if s_label and s_label not in parsed_services:
            parsed_services.append(s_label)

    detail_tags = list(parsed_utags)
    if cleaned.get("brand_name") and f"品牌:{cleaned['brand_name']}" not in detail_tags:
        detail_tags.append(f"品牌:{cleaned['brand_name']}")
    if cleaned.get("is_pre_sale"):
        detail_tags.append("预售商品")

    c_start = cleaned.get("coupon_start_time", 0)
    c_end = cleaned.get("coupon_end_time", 0)
    valid_tip = ""
    if c_start and c_end:
        try:
            s_str = datetime.fromtimestamp(c_start, tz=timezone.utc).strftime("%m.%d")
            e_str = datetime.fromtimestamp(c_end, tz=timezone.utc).strftime("%m.%d")
            valid_tip = f"{s_str} - {e_str} 有效"
        except Exception:
            pass

    cleaned.update({
        "desc": cleaned.get("goods_desc") or cleaned.get("goods_name", ""),
        "carousel_images": gallery,
        "detail_tags": detail_tags,
        "unified_tags": parsed_utags,
        "service_guarantees": parsed_services,
        "shop_ratings": {
            "desc_score": cleaned.get("desc_txt") or "",
            "serv_score": cleaned.get("serv_txt") or "",
            "lgst_score": cleaned.get("lgst_txt") or "",
        },
        "coupon_info": {
            "coupon_total_quantity": cleaned.get("coupon_total_quantity", 0),
            "coupon_remain_quantity": cleaned.get("coupon_remain_quantity", 0),
            "coupon_min_order_amount_yuan": round((cleaned.get("coupon_min_order_amount") or 0) / 100.0, 2),
            "valid_tip": valid_tip,
        },
    })
    return {"status": "success", "data": cleaned}


# ==============================================================================
# 核心功能 4：三层智能兜底转链 (rp.prom[23] -> goods.promotion -> zs.unit)
# ==============================================================================
def convert_pdd_promotion_url(
    goods_sign: Optional[str] = None,
    goods_id: Optional[str] = None,
    search_id: Optional[str] = None,
    source_url: Optional[str] = None,
    uid: Optional[str] = None,
    pid: Optional[str] = None,
    prefer_subsidy: bool = False,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """
    三层智能降级转链：
    1. 若 prefer_subsidy=True 且有 goods_sign，优先尝试百亿补贴营销频道 (pdd.ddk.rp.prom.url.generate, channel_type=23)
    2. 常规单品推广转链 (pdd.ddk.goods.promotion.url.generate)
    3. 招商长短链兜底转链 (pdd.ddk.goods.zs.unit.url.gen)
    """
    use_pid = pid or PDD_PID
    custom_params = _build_custom_params(uid)

    # 策略 1：百亿补贴/营销页转链
    if prefer_subsidy and goods_sign:
        rp_params = {
            "channel_type": 23,
            "p_id_list": [use_pid],
            "custom_parameters": custom_params,
            "generate_short_url": True,
            "generate_we_app": True,
            "diy_subsidy_param": {"goods_sign": goods_sign},
        }
        rp_res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", rp_params)
        url_list = (rp_res or {}).get("rp_promotion_url_generate_response", {}).get("url_list") or []
        if url_list:
            first = url_list[0]
            link = first.get("mobile_short_url") or first.get("short_url") or first.get("mobile_url") or first.get("url")
            if link:
                return {
                    "status": "success",
                    "link_type": "百亿补贴专属加补链",
                    "final_link": link,
                    "short_url": first.get("short_url", link),
                    "url": first.get("url", link),
                    "we_app_info": first.get("we_app_info", {}),
                }

    # 策略 2：常规单品推广转链
    if goods_sign:
        gen_params: Dict[str, Any] = {
            "p_id": use_pid,
            "goods_sign_list": [goods_sign],
            "custom_parameters": custom_params,
            "generate_short_url": True,
            "generate_we_app": True,
            "generate_authority_url": False,
        }
        if search_id:
            gen_params["search_id"] = search_id
        gen_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.promotion.url.generate", gen_params)
        url_list = (gen_res or {}).get("goods_promotion_url_generate_response", {}).get("goods_promotion_url_list") or []
        if url_list:
            first = url_list[0]
            link = first.get("mobile_short_url") or first.get("short_url") or first.get("mobile_url") or first.get("url")
            if link:
                return {
                    "status": "success",
                    "link_type": "常规单品领券推广链",
                    "final_link": link,
                    "short_url": first.get("short_url", link),
                    "url": first.get("url", link),
                    "we_app_info": first.get("we_app_info", {}),
                }

    # 策略 3：招商短链兜底转链
    target_source_url = source_url or (f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}" if goods_id else "")
    if target_source_url:
        zs_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", {
            "source_url": target_source_url,
            "pid": use_pid,
            "custom_parameters": custom_params,
        })
        zs_body = (zs_res or {}).get("goods_zs_unit_generate_response", {})
        link = zs_body.get("mobile_short_url") or zs_body.get("short_url") or zs_body.get("mobile_url") or zs_body.get("url")
        if link:
            return {
                "status": "success",
                "link_type": "招商长短链兜底推广",
                "final_link": link,
                "short_url": zs_body.get("short_url", link),
                "url": zs_body.get("url", link),
            }

    return {"status": "error", "error_msg": "拼多多三层推广转链均未获取到有效链接"}


# ==============================================================================
# 核心功能 5：推手授权备案查询与生成 (member.authority.query & rp.prom[10])
# ==============================================================================
def check_pdd_member_authority(
    uid: str = "default_user",
    pid: Optional[str] = None,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """查询推手是否已完成拼多多备案授权 (pdd.ddk.member.authority.query)"""
    res = call_pdd_api(client_id, client_secret, "pdd.ddk.member.authority.query", {
        "pid": pid or PDD_PID,
        "custom_parameters": _build_custom_params(uid),
    })
    if not res or "error_response" in res:
        err = (res or {}).get("error_response", {})
        return {"status": "error", "error_msg": err.get("error_msg", "查询拼多多备案状态失败")}

    bind = (res.get("authority_query_response") or {}).get("bind", 0)
    return {"status": "success", "bind": bind, "is_authorized": bind == 1}


def generate_pdd_authority_url(
    uid: str = "default_user",
    pid: Optional[str] = None,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """生成拼多多推手专属备案授权链接 (pdd.ddk.rp.prom.url.generate, channel_type=10)"""
    res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", {
        "channel_type": 10,
        "p_id_list": [pid or PDD_PID],
        "custom_parameters": _build_custom_params(uid),
        "generate_short_url": True,
        "generate_mobile": True,
    })
    if not res or "error_response" in res:
        err = (res or {}).get("error_response", {})
        return {"status": "error", "error_msg": err.get("error_msg", "生成拼多多授权链接失败")}

    url_list = (res.get("rp_promotion_url_generate_response") or {}).get("url_list") or []
    if not url_list:
        return {"status": "error", "error_msg": "拼多多返回授权链接列表为空"}

    first = url_list[0]
    auth_url = first.get("mobile_short_url") or first.get("mobile_url") or first.get("short_url") or first.get("url", "")
    return {
        "status": "success",
        "auth_url": auth_url,
        "url": first.get("url", ""),
        "short_url": first.get("short_url", ""),
        "mobile_url": first.get("mobile_url", ""),
    }


# ==============================================================================
# 核心功能 6：增量订单拉取与清洗 (pdd.ddk.order.list.increment.get)
# ==============================================================================
def fetch_pdd_increment_orders(
    start_update_time: int,
    end_update_time: int,
    page: int = 1,
    page_size: int = 50,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """拉取拼多多更新时间段内的增量订单列表 (pdd.ddk.order.list.increment.get)"""
    res = call_pdd_api(client_id, client_secret, "pdd.ddk.order.list.increment.get", {
        "start_update_time": int(start_update_time),
        "end_update_time": int(end_update_time),
        "page": page,
        "page_size": page_size,
        "return_count": True,
    })
    if not res or "error_response" in res:
        err = (res or {}).get("error_response", {})
        return {"status": "error", "error_msg": err.get("error_msg", "拉取拼多多增量订单失败"), "order_list": []}

    body = res.get("order_list_get_response", {})
    raw_orders = body.get("order_list") or []
    total_count = body.get("total_count", len(raw_orders))
    return {
        "status": "success",
        "total_count": total_count,
        "order_list": raw_orders,
        "has_more": page * page_size < total_count if isinstance(total_count, int) else len(raw_orders) >= page_size,
    }


# ==============================================================================
# 全链路自检验证主流程
# ==============================================================================
def main():
    print("================================================================================")
    print("拼多多 (多多进宝 DDK) 全链路标准基线验证脚本")
    print("================================================================================")
    if not PDD_CLIENT_ID or not PDD_CLIENT_SECRET:
        print("未检测到拼多多配置 (myself_pdd_client_id / secret)，请先检查 config.local.json。")
        return

    test_uid = "u_baseline_test"

    # 1. 验证热销榜单 (channel_type=5 实时热销榜 & channel_type=1 今日爆款榜)
    print("\n[1/6] 验证拼多多榜单拉取 (channel_type=5 实时热销榜)...")
    rec_res = get_pdd_recommend_goods(channel_type=5, limit=3, uid=test_uid)
    sample_sign = None
    sample_id = None
    if rec_res["status"] == "success" and rec_res["data"]:
        first = rec_res["data"][0]
        sample_sign = first.get("goods_sign")
        sample_id = first.get("goods_id")
        print(f" -> 成功拉取 {len(rec_res['data'])} 条推荐商品，首条: {first['name'][:25]} (¥{first['price']})")
    else:
        print(f" -> 推荐拉取失败: {rec_res.get('error_msg')}")

    # 2. 验证关键词搜索
    print("\n[2/6] 验证拼多多关键词搜索 (pdd.ddk.goods.search)...")
    search_res = search_pdd_goods("抽纸", page=1, page_size=3, uid=test_uid)
    if search_res["status"] == "success" and search_res["data"]:
        first = search_res["data"][0]
        sample_sign = sample_sign or first.get("goods_sign")
        sample_id = sample_id or first.get("goods_id")
        print(f" -> 搜索成功，共命中 {search_res['total_count']} 条，首条: {first['name'][:25]} (¥{first['price']})")
    else:
        print(f" -> 搜索失败: {search_res.get('error_msg')}")

    # 3. 验证商品详情
    print("\n[3/6] 验证拼多多商品详情 (pdd.ddk.goods.detail)...")
    if sample_sign or sample_id:
        detail_res = get_pdd_goods_detail(goods_sign=sample_sign, goods_id=sample_id, pid=PDD_PID)
        if detail_res["status"] == "success":
            d = detail_res["data"]
            print(f" -> 详情获取成功: {d['name'][:25]} | 店铺: {d.get('mall_name')} | 轮播图: {len(d.get('carousel_images', []))}张")
        else:
            print(f" -> 详情获取失败: {detail_res.get('error_msg')}")

    # 4. 验证三层智能转链
    print("\n[4/6] 验证拼多多三层智能兜底转链...")
    if sample_sign or sample_id:
        conv_res = convert_pdd_promotion_url(goods_sign=sample_sign, goods_id=sample_id, uid=test_uid)
        print(f" -> 转链结果: status={conv_res['status']}, type={conv_res.get('link_type')}, link={conv_res.get('final_link')}")

    # 5. 验证推手备案查询与授权链接生成
    print("\n[5/6] 验证拼多多推手备案查询与授权链接生成...")
    auth_res = check_pdd_member_authority(uid=test_uid)
    print(f" -> 备案查询结果: {auth_res}")
    if not auth_res.get("is_authorized"):
        url_res = generate_pdd_authority_url(uid=test_uid)
        print(f" -> 授权链接生成结果: status={url_res['status']}, auth_url={url_res.get('auth_url')}")

    # 6. 验证增量订单查询
    print("\n[6/6] 验证拼多多增量订单拉取 (最近 1 小时)...")
    now_ts = int(time.time())
    ord_res = fetch_pdd_increment_orders(now_ts - 3600, now_ts)
    print(f" -> 订单查询结果: status={ord_res['status']}, 数量={len(ord_res.get('order_list', []))}")


if __name__ == "__main__":
    main()
