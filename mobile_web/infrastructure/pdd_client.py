# -*- coding: utf-8 -*-
"""Local PDD runtime subset extracted from app/pdd_utils.py.

Keeps the mobile web gateway/protocol behavior without shared configuration or
logger setup. DNS optimization is installed only by configure_transport().
"""

import hashlib
import json
import logging
import socket
import sys
import threading
import time

import requests
from requests.adapters import HTTPAdapter


logger = logging.getLogger("mobile_web")

_ORIG_GETADDRINFO = socket.getaddrinfo
_PDD_FALLBACK_IPS = ("101.226.73.254", "58.217.134.20", "101.226.73.255", "58.217.134.19")
_PDD_DNS_CACHE = {}
_PDD_DNS_LOCK = threading.Lock()
_PDD_TRANSPORT_LOCK = threading.Lock()
_PDD_DNS_ENABLED = False
_PDD_DNS_INSTALLED = False


def _is_windows():
    return sys.platform == "win32"


def _pdd_cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if _PDD_DNS_ENABLED and host == "gw-api.pinduoduo.com":
        now = time.time()
        entry = _PDD_DNS_CACHE.get(host)
        if entry and now < entry[0]:
            return [(f, t, p, c, (addr[0], port or addr[1])) for f, t, p, c, addr in entry[1]]
        with _PDD_DNS_LOCK:
            entry = _PDD_DNS_CACHE.get(host)
            if entry and now < entry[0]:
                return [(f, t, p, c, (addr[0], port or addr[1])) for f, t, p, c, addr in entry[1]]
            try:
                res = _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
                _PDD_DNS_CACHE[host] = (now + 600, res)
                return res
            except Exception:
                if entry:
                    return [(f, t, p, c, (addr[0], port or addr[1])) for f, t, p, c, addr in entry[1]]
                fallback_res = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))
                    for ip in _PDD_FALLBACK_IPS
                ]
                _PDD_DNS_CACHE[host] = (now + 300, fallback_res)
                return fallback_res
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


# Session/pool construction performs no requests, configuration reads or writes.
_PDD_SESSION = requests.Session()
_PDD_SESSION.trust_env = not _is_windows()
_PDD_ADAPTER = HTTPAdapter(pool_connections=30, pool_maxsize=100, max_retries=1)
_PDD_SESSION.mount("https://", _PDD_ADAPTER)
_PDD_SESSION.mount("http://", _PDD_ADAPTER)
_PDD_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
})


def configure_transport(dns_mode="auto", trust_env=None):
    """Configure platform defaults explicitly; never resolve a host here.

    auto keeps the original cached IPv4/fallback optimization on Windows and
    uses native DNS elsewhere. on/off override DNS on either platform. A later
    third-party resolver is never overwritten when disabling our wrapper; any
    retained reference to our wrapper becomes a transparent pass-through.
    """
    global _ORIG_GETADDRINFO, _PDD_DNS_ENABLED, _PDD_DNS_INSTALLED

    if dns_mode not in ("auto", "on", "off"):
        raise ValueError("dns_mode must be 'auto', 'on', or 'off'")
    if trust_env is not None and not isinstance(trust_env, bool):
        raise TypeError("trust_env must be a bool or None")

    enabled = dns_mode == "on" or (dns_mode == "auto" and _is_windows())
    with _PDD_TRANSPORT_LOCK:
        _PDD_SESSION.trust_env = not _is_windows() if trust_env is None else trust_env
        if enabled:
            if not _PDD_DNS_INSTALLED:
                _ORIG_GETADDRINFO = socket.getaddrinfo
                with _PDD_DNS_LOCK:
                    if not _PDD_DNS_CACHE:
                        _PDD_DNS_CACHE["gw-api.pinduoduo.com"] = (
                            time.time() + 600,
                            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
                             for ip in _PDD_FALLBACK_IPS],
                        )
                _PDD_DNS_ENABLED = True
                socket.getaddrinfo = _pdd_cached_getaddrinfo
                _PDD_DNS_INSTALLED = True
            else:
                # A third party may now wrap us. Do not capture that wrapper as
                # our upstream: doing so would create a resolver recursion loop.
                _PDD_DNS_ENABLED = True
        else:
            _PDD_DNS_ENABLED = False
            if _PDD_DNS_INSTALLED and socket.getaddrinfo is _pdd_cached_getaddrinfo:
                socket.getaddrinfo = _ORIG_GETADDRINFO
                _PDD_DNS_INSTALLED = False


def warmup():
    """Best-effort connection-pool warmup, invoked explicitly at startup."""
    try:
        _PDD_SESSION.head("https://gw-api.pinduoduo.com", timeout=3.0)
    except Exception:
        logger.debug("拼多多连接池预热未完成，首次请求时重试")


def make_custom_parameters(uid=None, extra_custom=None):
    """
    通用参数序列化：构建拼多多标准 custom_parameters 字符串
    """
    custom_dict = {}
    if uid is not None and str(uid).strip():
        custom_dict["uid"] = str(uid).strip()
    if extra_custom and isinstance(extra_custom, dict):
        custom_dict.update(extra_custom)

    if not custom_dict:
        return None

    return json.dumps(custom_dict, ensure_ascii=False, separators=(',', ':'))

def extract_short_url(response_dict):
    """
    通用提取器：从各类转链/生链返回结果中提取最优短链接
    """
    if not isinstance(response_dict, dict):
        return None
    return (
        response_dict.get("mobile_short_url")
        or response_dict.get("short_url")
        or response_dict.get("url")
    )

def call_pdd_api(client_id, client_secret, api_type, business_params=None, timeout=(2.5, 4.0)):
    """
    拼多多网关核心请求与 MD5 自动签名引擎 (使用连接池加速)
    """
    url = "https://gw-api.pinduoduo.com/api/router"
    business_params = business_params or {}

    # 1. 过滤并组装初始参数
    raw_params = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "version": "V1"
    }
    raw_params.update({k: v for k, v in business_params.items() if v is not None})

    # 2. 参数彻底扁平化/字符串化（防止 Java 网关解析类型歧义）
    params = {}
    for key, val in raw_params.items():
        if isinstance(val, bool):
            params[key] = "true" if val else "false"
        elif isinstance(val, (list, dict)):
            params[key] = json.dumps(val, ensure_ascii=False, separators=(',', ':'))
        else:
            params[key] = str(val)

    # 3. 字典序排列后加盐计算 MD5 签名
    sign_str = client_secret
    for key in sorted(params.keys()):
        sign_str += f"{key}{params[key]}"
    sign_str += client_secret

    params["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    # 4. 发起 HTTP POST 请求 (连接池复用)
    try:
        response = _PDD_SESSION.post(
            url,
            json=params,
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=timeout
        )
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        raise RuntimeError(f"拼多多网络请求层崩溃: {str(e)}")

    # 5. 网关级与业务级错误校验拦截
    if "error_response" in result:
        err_resp = result["error_response"]
        error_msg = err_resp.get("error_msg", "未知业务错误")
        sub_msg = err_resp.get("sub_msg", "")
        error_code = err_resp.get("error_code", "")
        sub_code = err_resp.get("sub_code", "")
        full_error = f"[{error_code}:{sub_code}] {error_msg} - {sub_msg}".strip(" -:")
        raise RuntimeError(f"PDD网关拒绝: {full_error}")

    return result

def clean_goods_item(raw_data, source_type=""):
    """
    通用商品数据清洗：抹平单品详情、全网搜索、智能推荐接口字段差异
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    min_group_price = raw_data.get("min_group_price", 0)
    coupon_discount = raw_data.get("coupon_discount", 0)
    promotion_rate = raw_data.get("promotion_rate", 0)

    # 计算预估佣金（分）
    estimated_commission = int((min_group_price - coupon_discount) * promotion_rate / 1000) if promotion_rate > 0 else 0

    return {
        "goods_id": raw_data.get("goods_id", 0),
        "goods_sign": raw_data.get("goods_sign", ""),
        "goods_name": raw_data.get("goods_name", ""),
        "goods_desc": raw_data.get("goods_desc", ""),
        "category_name": raw_data.get("category_name", ""),
        "brand_name": raw_data.get("brand_name", ""),
        "goods_thumbnail_url": raw_data.get("goods_thumbnail_url", ""),
        "goods_image_url": raw_data.get("goods_image_url", ""),
        "min_group_price": min_group_price,  # 拼团最低价(分)
        "min_group_price_yuan": round(min_group_price / 100.0, 2),
        "min_normal_price": raw_data.get("min_normal_price", min_group_price),
        "promotion_rate": promotion_rate,  # 千分比，如 200 代表 20%
        "estimated_commission": estimated_commission,  # 预估佣金(分)
        "estimated_commission_yuan": round(estimated_commission / 100.0, 2),
        "sales_tip": raw_data.get("sales_tip", "0"),
        "unified_tags": raw_data.get("unified_tags", []),
        "mall_name": raw_data.get("mall_name", ""),
        "merchant_type": raw_data.get("merchant_type", 1),
        "desc_txt": raw_data.get("desc_txt", "平"),
        "serv_txt": raw_data.get("serv_txt", "平"),
        "lgst_txt": raw_data.get("lgst_txt", "平"),
        "has_coupon": raw_data.get("has_coupon", False),
        "coupon_discount": coupon_discount,
        "coupon_discount_yuan": round(coupon_discount / 100.0, 2),
        "coupon_min_order_amount": raw_data.get("coupon_min_order_amount", 0),
        "coupon_remain_quantity": raw_data.get("coupon_remain_quantity", 0),
        "coupon_total_quantity": raw_data.get("coupon_total_quantity", 0),
        "coupon_start_time": raw_data.get("coupon_start_time", 0),
        "coupon_end_time": raw_data.get("coupon_end_time", 0),
        "has_mall_coupon": raw_data.get("has_mall_coupon", False),
        "search_id": raw_data.get("search_id", ""),
        "_source_api": source_type,
        "_raw_data": raw_data
    }

def clean_order_item(order):
    """
    通用订单数据清洗：提取推手归属、状态映射、分转元算费
    """
    raw_custom = order.get("custom_parameters", "")
    promoter_uid = None

    if raw_custom:
        try:
            custom_dict = json.loads(raw_custom)
            if isinstance(custom_dict, dict) and "uid" in custom_dict:
                promoter_uid = str(custom_dict["uid"])
            else:
                promoter_uid = str(raw_custom)
        except Exception:
            promoter_uid = str(raw_custom)

    status_code = order.get("order_status")
    status_desc = order.get("order_status_desc", "未知状态")
    order_amount_fen = order.get("order_amount", 0)
    promotion_amount_fen = order.get("promotion_amount", 0)
    goods_price_fen = order.get("goods_price", 0)

    return {
        "order_sn": order.get("order_sn"),
        "goods_id": order.get("goods_id"),
        "goods_name": order.get("goods_name"),
        "goods_thumbnail_url": order.get("goods_thumbnail_url"),
        "goods_quantity": order.get("goods_quantity", 1),
        "goods_price_yuan": round(goods_price_fen / 100.0, 2),
        "order_amount_yuan": round(order_amount_fen / 100.0, 2),
        "promotion_amount_yuan": round(promotion_amount_fen / 100.0, 2),
        "promotion_rate": round(order.get("promotion_rate", 0) / 10.0, 2),
        "order_status": status_code,
        "order_status_desc": status_desc,
        "is_refund": (status_code == 4),  # 审核失败/维权退款
        "is_settled": (status_code in [3, 5]),  # 已结算成功
        "promoter_uid": promoter_uid,
        "is_self_operated": not bool(promoter_uid),
        "pid": order.get("p_id"),
        "order_pay_time": order.get("order_pay_time"),
        "order_receive_time": order.get("order_receive_time"),
        "order_settle_time": order.get("order_settle_time"),
        "order_modify_at": order.get("order_modify_at"),
    }

def check_member_authority(client_id, client_secret, pid, uid):
    """
    [基础函数] 查询指定推手/渠道是否已在拼多多完成绑定备案
    接口：pdd.ddk.member.authority.query
    """
    custom_params = make_custom_parameters(uid=uid)
    params = {"pid": pid, "custom_parameters": custom_params}

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.member.authority.query", params)
        resp_data = res.get("authority_query_response", {})
        bind = resp_data.get("bind", 0)
        return {
            "status": "success",
            "bind": bind,
            "is_authorized": (bind == 1),
            "request_id": resp_data.get("request_id")
        }
    except Exception as e:
        logger.error(f"[备案查询异常] uid={uid}, 错误: {str(e)}")
        return {"status": "error", "bind": 0, "is_authorized": False, "error_msg": str(e)}

def generate_authority_url(client_id, client_secret, pid, uid=None, **kwargs):
    """
    [基础函数] 生成推手/渠道专属授权备案链接 (channel_type=10)
    接口：pdd.ddk.rp.prom.url.generate
    """
    business_params = {
        "p_id_list": [pid],
        "channel_type": 10,
        "generate_we_app": True,
        "generate_short_url": True,
        "generate_mobile": True,
        "custom_parameters": make_custom_parameters(uid=uid)
    }
    business_params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", business_params)
        url_list = res.get("rp_promotion_url_generate_response", {}).get("url_list", [])
        if url_list:
            return {"status": "success", "url_info": url_list[0]}
        return {"status": "error", "error_msg": "接口调用成功但未返回可用链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"生成备案链接失败: {str(e)}"}

def convert_pdd_url(client_id, client_secret, pid, source_url, uid=None, **kwargs):
    """
    [基础函数] 外部/单条拼多多链接清洗转换 (洗出带有自己 PID 的推广链)
    接口：pdd.ddk.goods.zs.unit.url.gen
    """
    params = {
        "pid": pid,
        "source_url": source_url,
        "custom_parameters": make_custom_parameters(uid=uid)
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", params)
        data = res.get("goods_zs_unit_generate_response", {})
        jump_url = extract_short_url(data)
        if jump_url:
            return {"status": "success", "jump_url": jump_url, "raw_data": data}
        return {"status": "error", "error_msg": "接口返回正常但未包含可用的跳转链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"转链失败: {str(e)}"}

def generate_goods_promotion_url(client_id, client_secret, pid, goods_sign, uid=None, search_id=None, **kwargs):
    """
    [基础函数] 使用官方标准接口直接将 goods_sign 生成推广短链
    接口：pdd.ddk.goods.promotion.url.generate
    """
    params = {
        "p_id": pid,
        "goods_sign_list": [goods_sign],
        "generate_short_url": True,
        "generate_mobile": True,
        "generate_schema_url": True,
        "generate_we_app": True,
        "custom_parameters": make_custom_parameters(uid=uid),
        "search_id": search_id
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.promotion.url.generate", params)
        url_list = res.get("goods_promotion_url_generate_response", {}).get("goods_promotion_url_list", [])
        if url_list:
            return {"status": "success", "data": url_list[0]}
        return {"status": "error", "error_msg": "未能生成推广链接"}
    except Exception as e:
        return {"status": "error", "error_msg": f"生成商品推广链接异常: {str(e)}"}

def generate_smart_promotion_url(client_id, client_secret, pid, goods_sign, cash_gift_id=None, uid=None, **kwargs):
    """
    [复合应用函数] 终极智能降级生链：礼金 > 官方超级红包(Channel 23) > 普通推广链接
    """
    custom_params = make_custom_parameters(uid=uid)

    # 策略 1：如果传入了礼金活动 ID，尝试生成礼金秒杀单
    if cash_gift_id:
        try:
            params = {
                "p_id": pid,
                "goods_sign_list": [goods_sign],
                "cash_gift_id": int(cash_gift_id),
                "generate_short_url": True,
                "custom_parameters": custom_params
            }
            params.update(kwargs)
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.promotion.url.generate", params)
            url_list = res.get("goods_promotion_url_generate_response", {}).get("goods_promotion_url_list", [])
            if url_list:
                return {"status": "success", "link_type": "多多礼金秒杀", "data": url_list[0]}
        except Exception:
            pass  # 礼金降级，平滑进入下一级

    # 策略 2：尝试获取官方超级红包卡券池链接
    try:
        hb_params = {
            "p_id_list": [pid],
            "channel_type": 23,
            "generate_short_url": True,
            "diy_sp_red_packet_param": {"goods_sign": goods_sign},
            "custom_parameters": custom_params
        }
        hb_res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", hb_params)
        hb_url_list = hb_res.get("rp_promotion_url_generate_response", {}).get("url_list", [])
        if hb_url_list:
            return {"status": "success", "link_type": "官方超级红包", "data": hb_url_list[0]}
    except Exception:
        pass  # 降级进入常规普通生链

    # 策略 3：标准兜底推广
    fallback_res = generate_goods_promotion_url(client_id, client_secret, pid, goods_sign, uid=uid, **kwargs)
    if fallback_res.get("status") == "success":
        fallback_res["link_type"] = "常规推广"
    return fallback_res

def get_goods_detail(client_id, client_secret, pid=None, goods_sign=None, goods_id=None, uid=None, **kwargs):
    """
    [基础函数] 统一单品详情查询器：
    - 优先根据 goods_sign 直连官方 Detail 接口；
    - 仅有 goods_id 时，自动执行 “转短链 -> 关键词截胡搜索” 的双跨策略。
    """
    if not goods_sign and not goods_id:
        return {"status": "error", "error_msg": "必须提供 goods_sign 或 goods_id 其中之一"}

    custom_params = make_custom_parameters(uid=uid)

    # 路径 A：标准 goods_sign 查询
    if goods_sign:
        try:
            params = {
                "goods_sign": goods_sign,
                "pid": pid,
                "custom_parameters": custom_params
            }
            params.update(kwargs)
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", params)
            goods_list = res.get("goods_detail_response", {}).get("goods_details", [])
            if goods_list:
                cleaned = clean_goods_item(goods_list[0], source_type="detail_api")
                return {"status": "success", "data": cleaned}
            return {"status": "error", "error_msg": "Detail 接口未找到商品，可能已下架或无推广计划"}
        except Exception as e:
            return {"status": "error", "error_msg": f"Detail 查询失败: {str(e)}"}

    # 路径 B：goods_id 降级策略（通过短链反查）
    try:
        source_url = f"https://mobile.pinduoduo.com/goods.html?goods_id={goods_id}"
        zs_res = convert_pdd_url(client_id, client_secret, pid=pid, source_url=source_url, uid=uid)
        short_url = zs_res.get("jump_url")
        if not short_url:
            return {"status": "error", "error_msg": "通过 goods_id 生成探针短链失败"}

        search_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {
            "keyword": short_url,
            "pid": pid,
            "custom_parameters": custom_params,
            "page": 1,
            "page_size": 10
        })
        goods_list = search_res.get("goods_search_response", {}).get("goods_list", [])
        if goods_list:
            cleaned = clean_goods_item(goods_list[0], source_type="search_api_probe")
            return {"status": "success", "data": cleaned}
        return {"status": "error", "error_msg": "探针短链搜索未命中，商品可能不可推广"}
    except Exception as e:
        return {"status": "error", "error_msg": f"goods_id 反查中断: {str(e)}"}

def query_increment_orders(client_id, client_secret, start_time, end_time, page=1, page_size=50, **kwargs):
    """
    [订单同步] 按订单更新时间增量同步（用于佣金核算、处理退款与订单变更）
    接口：pdd.ddk.order.list.increment.get
    """
    params = {
        "start_update_time": int(start_time),
        "end_update_time": int(end_time),
        "page": int(page),
        "page_size": int(page_size),
        "return_count": True
    }
    params.update(kwargs)

    try:
        res = call_pdd_api(client_id, client_secret, "pdd.ddk.order.list.increment.get", params)
        resp_data = res.get("order_list_get_response", {})
        raw_orders = resp_data.get("order_list", [])
        total_count = resp_data.get("total_count", 0)

        cleaned_orders = [clean_order_item(item) for item in raw_orders]
        return {
            "status": "success",
            "total_count": total_count,
            "order_list": cleaned_orders
        }
    except Exception as e:
        return {"status": "error", "error_msg": f"增量订单查询失败: {str(e)}", "order_list": []}
