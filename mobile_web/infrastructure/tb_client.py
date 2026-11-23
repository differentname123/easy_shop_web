# -*- coding: utf-8 -*-
"""
淘宝开放平台 (TOP / TBK) 基础设施客户端
适配最新 27939 权限包与官方 API 规范：
- 网关 HTTP 请求与连接池复用
- TOP MD5 自动签名引擎
- 物料搜索与升级查询 (taobao.tbk.dg.material.optional.upgrade)
- 真实淘口令生成 (taobao.tbk.tpwd.create)
- 渠道与推手关系查询 (taobao.tbk.sc.publisher.info.get)
"""
import hashlib
import json
import math
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from infrastructure.http_utils import (
    SHANGHAI,
    clean_text as _text,
    error_dict as _error,
    normalize_http_url,
)
from infrastructure.logging_utils import get_app_logger
from infrastructure.settings import persist_config_updates, resolve_config_path
from infrastructure.transport import create_http_session

logger = get_app_logger("tb_client")

TOP_GATEWAY_URL = "https://eco.taobao.com/router/rest"

_TB_SESSION = create_http_session(
    pool_connections=30,
    pool_maxsize=100,
    max_retries=1,
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    },
)


def generate_tb_sign(params: dict, app_secret: str) -> str:
    """计算淘宝开放平台 (TOP) 标准 MD5 签名。"""
    valid_items = [
        (k, str(v)) for k, v in params.items() if v is not None and k != "sign"
    ]
    sorted_items = sorted(valid_items, key=lambda x: x[0])
    query_str = (
        app_secret + "".join(f"{k}{v}" for k, v in sorted_items) + app_secret
    )
    return hashlib.md5(query_str.encode("utf-8")).hexdigest().upper()


def _all_full_tb_encrypted_ids(raw_item_id: Any) -> bool:
    """判断传入的淘宝 item_id 是否全为合法完整加密 ID（<prefix>-<suffix>）。"""
    raw = str(raw_item_id or "").strip()
    if not raw:
        return False
    parts = [p.strip() for p in raw.split(",")]
    if not parts or any(not p for p in parts):
        return False
    for part in parts:
        if "-" not in part:
            return False
        prefix, suffix = part.split("-", 1)
        if (
            len(prefix) < 15
            or len(suffix) < 12
            or not all(c.isalnum() or c == "_" for c in prefix)
            or not all(c.isalnum() or c == "_" for c in suffix)
        ):
            return False
    return True


def call_tb_api(
    app_key: str,
    app_secret: str,
    method: str,
    biz_params: Optional[dict] = None,
    session_key: Optional[str] = None,
    timeout=(2.0, 5.0),
    http_session=None
) -> Dict[str, Any]:
    """统一调用淘宝开放平台接口并处理网关与业务级响应。"""
    if not app_key or not app_secret:
        return {
            "status": "error",
            "error_code": "CONFIG_ERROR",
            "error_msg": "未配置淘宝开放平台 AppKey 或 AppSecret",
            "raw": {},
        }

    biz_params = biz_params or {}
    params = {
        "method": method,
        "app_key": str(app_key).strip(),
        "timestamp": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
    }
    if session_key:
        params["session"] = str(session_key).strip()

    for k, v in biz_params.items():
        if v is not None:
            if isinstance(v, bool):
                params[k] = "true" if v else "false"
            elif isinstance(v, (list, dict)):
                params[k] = json.dumps(v, ensure_ascii=False, separators=(',', ':'))
            else:
                params[k] = str(v)

    params["sign"] = generate_tb_sign(params, app_secret)

    try:
        resp = (http_session or _TB_SESSION).post(
            TOP_GATEWAY_URL,
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            timeout=timeout,
            allow_redirects=False,
        )
        if 300 <= getattr(resp, "status_code", 200) < 400:
            return _error("PROTOCOL_ERROR", "淘宝网关返回不受支持的重定向")
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        # Exception messages may contain a URL or signed request; never echo credentials.
        logger.warning(
            "❌ 【淘宝网关异常】调用淘宝开放平台通信失败 | 接口[%s] | 异常类型<%s> | 排查方向<请检查淘宝TOP网关网络连通性或是否触发限流>",
            method,
            type(exc).__name__,
        )
        return _error("NETWORK_ERROR", "淘宝开放平台通信失败")

    if not isinstance(result, dict):
        return _error("PROTOCOL_ERROR", "淘宝网关响应不是对象")
    if "error_response" in result:
        err_body = result["error_response"]
        if not isinstance(err_body, dict):
            return _error("PROTOCOL_ERROR", "淘宝网关错误响应无效")
        code = err_body.get("code")
        sub_code = err_body.get("sub_code", "")
        msg = err_body.get("msg", "未知错误")
        sub_msg = err_body.get("sub_msg", "")
        error_detail = f"[{code}:{sub_code}] {sub_msg or msg}".strip()
        for credential in (app_key, app_secret, session_key):
            if credential:
                error_detail = error_detail.replace(str(credential), "[REDACTED]")
        sub_code_str = str(sub_code or "").strip()
        if sub_code_str == "invalid-sessionkey" or str(code) == "27":
            logger.info(
                "【淘宝凭证切换】官方24h SessionKey已过期，将自动切换至折淘客90天长效SID通道 | 接口[%s] 状态[%s]",
                method,
                sub_code or code,
            )
        elif sub_code_str == "50001" and method == "taobao.tbk.dg.material.optional.upgrade":
            logger.debug("【淘宝接口无结果】接口[%s] 返回空结果 [50001]", method)
        elif (
            sub_code_str == "50001"
            and method == "taobao.tbk.item.info.upgrade.get"
            and _all_full_tb_encrypted_ids(biz_params.get("item_id"))
        ):
            logger.debug(
                "【淘宝商品已下架或无推广】接口[%s] 返回空结果 [50001] (完整加密ID，属正常业务下架)",
                method,
            )
        else:
            logger.warning(
                "❌ 【淘宝网关异常】淘宝开放平台返回业务错误 | 接口[%s] 错误码[%s] | 详情<%s> | 排查方向<请检查TOP应用权限、SessionKey有效期或请求参数>",
                method,
                sub_code or code or "UNKNOWN",
                error_detail,
            )
        return _error(str(sub_code or code), error_detail)

    return {"status": "success", "data": result, "raw": result}


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _list(value, key):
    if isinstance(value, dict):
        value = value.get(key, [])
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def clean_tb_goods_item(raw_item: dict, source_type: str = "") -> Optional[Dict[str, Any]]:
    """保留已有扁平协议；只有官方证实的原ID映射可写 goods_id。"""
    if not isinstance(raw_item, dict) or not _text(raw_item.get("item_id")):
        return None
    base = raw_item.get("item_basic_info") or {}
    price = raw_item.get("price_promotion_info") or {}
    pub = raw_item.get("publish_info") or {}
    if not all(isinstance(value, dict) for value in (base, price, pub)):
        return None
    income = pub.get("income_info") or {}
    if not isinstance(income, dict):
        return None
    # Unknown or malformed prices are not zero-yuan offers. Explicit zero is valid.
    reported_prices = [price[key] for key in ("final_promotion_price", "zk_final_price", "reserve_price")
                       if price.get(key) not in (None, "")]
    if not reported_prices or any(isinstance(value, bool) for value in reported_prices):
        return None
    if any(_number(value, -1) < 0 for value in reported_prices):
        return None
    if not (base.get("title") or base.get("short_title")):
        return None
    item_id = _text(raw_item["item_id"])
    input_id = _text(raw_item.get("input_item_iid"))
    picture = normalize_http_url(base.get("pict_url"))
    small_images = base.get("small_images") or []
    if isinstance(small_images, dict):
        small_images = small_images.get("string", [])
    if isinstance(small_images, str):
        small_images = [small_images]
    images = [normalize_http_url(u) for u in small_images if normalize_http_url(u)] if isinstance(small_images, list) else []
    original = _number(price.get("reserve_price"), _number(price.get("zk_final_price")))
    final = _number(price.get("final_promotion_price"), _number(price.get("zk_final_price"), original))
    # Discounted price != coupon amount. Only explicit coupon evidence is admissible.
    coupon = _number(price.get("coupon_amount"))
    paths = _list(price.get("final_promotion_path_list"), "final_promotion_path_map_data")
    if not coupon:
        coupon = sum(_number(p.get("promotion_fee")) for p in paths
                     if isinstance(p, dict) and "券" in _text(p.get("promotion_title")))
    tags = _list(price.get("promotion_tag_list"), "promotion_tag_map_data")
    # income_rate is documented as percent (including subsidy). The nested raw
    # commission_rate has no documented scale; do not invent a /100 conversion.
    rate = _number(_text(pub.get("income_rate")).replace("%", ""))
    if not pub.get("income_rate") and _text(income.get("commission_rate")).endswith("%"):
        rate = _number(_text(income["commission_rate"])[:-1])
    comm_amount = _number(income.get("commission_amount"), _number(pub.get("commission_amount")))
    if comm_amount <= 0 and final > 0 and rate > 0:
        comm_amount = round(final * rate / 100.0, 2)
    return {
        # An opaque input echo proves the requested Sign, not an original ID.
        "goods_id": input_id if input_id.isdigit() and input_id != item_id else "",
        "goods_sign": item_id,
        "input_item_iid": input_id,
        "goods_name": base.get("title") or base.get("short_title") or "",
        "goods_desc": base.get("sub_title") or base.get("short_title") or "",
        "category_name": base.get("category_name") or "",
        "brand_name": base.get("brand_name") or "",
        "goods_thumbnail_url": picture, "goods_image_url": picture,
        "carousel_images": images or ([picture] if picture else []),
        "price": round(max(0, final), 2), "original_price": round(max(original, final), 2),
        "coupon_amount": round(max(0, coupon), 2), "has_coupon": coupon > 0,
        "commission_amount": comm_amount,
        "commission_rate": rate, "sales_tip": _text(base.get("tk_total_sales") or base.get("volume") or base.get("annual_vol") or "0"),
        "mall_name": base.get("shop_title") or "", "provcity": base.get("provcity") or "",
        "click_url": normalize_http_url(pub.get("click_url")),
        "coupon_share_url": normalize_http_url(pub.get("coupon_share_url")),
        "detail_tags": [t["tag_name"] for t in tags if isinstance(t, dict) and t.get("tag_name")],
        "seller_id": _text(base.get("seller_id")), "raw_item": raw_item, "_source_api": source_type,
        "item_url": f"https://uland.taobao.com/item/edetail?id={input_id if input_id and input_id.isdigit() else item_id}",
    }


def search_tb_material_upgrade(
    app_key: str,
    app_secret: str,
    adzone_id: str,
    keyword: Optional[str] = None,
    material_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    uid: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """使用 27939 升级接口 (taobao.tbk.dg.material.optional.upgrade) 查询物料列表。"""
    biz_params = {
        "adzone_id": str(adzone_id).strip(),
        "page_no": max(1, int(page)),
        "page_size": max(1, min(100, int(page_size))),
    }
    if keyword and str(keyword).strip():
        biz_params["q"] = str(keyword).strip()
    else:
        biz_params["q"] = "精选"

    if material_id:
        biz_params["material_id"] = str(material_id).strip()

    if uid and str(uid).strip():
        biz_params["external_id"] = str(uid).strip()

    for k, v in kwargs.items():
        if v is not None and k not in biz_params:
            biz_params[k] = v

    api_call = kwargs.pop("_api_call", call_tb_api)
    biz_params.pop("_api_call", None)
    res = api_call(
        app_key=app_key,
        app_secret=app_secret,
        method="taobao.tbk.dg.material.optional.upgrade",
        biz_params=biz_params,
    )
    if res.get("status") != "success":
        err_code = str(res.get("error_code") or "")
        err_msg = str(res.get("error_msg") or "")
        if err_code == "50001" or "无结果" in err_msg:
            return {
                "status": "success",
                "total_count": 0,
                "page": biz_params["page_no"],
                "page_size": biz_params["page_size"],
                "has_more": False,
                "data": [],
            }
        return {**res, "data": [], "total_count": 0}

    resp_body = res.get("data", {}).get("tbk_dg_material_optional_upgrade_response")
    if not isinstance(resp_body, dict) or "result_list" not in resp_body:
        return _error("PROTOCOL_ERROR", "物料查询响应缺少结果列表")
    raw_list = _list(resp_body.get("result_list"), "map_data")
    total_value = resp_body.get("total_results")
    total = int(total_value) if total_value is not None else None

    items = [
        clean_tb_goods_item(item, source_type="tbk_upgrade")
        for item in raw_list if isinstance(item, dict)
    ]
    items = [i for i in items if i is not None]

    return {
        "status": "success",
        "total_count": total,
        "page": biz_params["page_no"],
        "page_size": biz_params["page_size"],
        "has_more": biz_params["page_no"] * biz_params["page_size"] < total if total is not None else len(raw_list) >= biz_params["page_size"],
        "data": items,
    }


def get_tb_material_recommend(
    app_key: str,
    app_secret: str,
    adzone_id: str,
    *,
    material_id: int = 3756,
    page_no: int = 1,
    page_size: int = 20,
    http_session: Optional[Any] = None,
    _api_call: Any = None,
) -> Dict[str, Any]:
    """使用官方物料精选推荐接口 (taobao.tbk.dg.material.recommend) 获取高佣热销榜单。"""
    az = str(adzone_id or "").strip()
    if not app_key or not app_secret or not az:
        return _error("CONFIG_ERROR", "淘宝物料推荐缺少 app_key、app_secret 或 adzone_id")
    pg = max(1, int(page_no))
    sz = max(1, min(100, int(page_size)))
    biz_params = {
        "adzone_id": az,
        "material_id": int(material_id),
        "page_no": pg,
        "page_size": sz,
    }
    res = (
        _api_call(app_key=app_key, app_secret=app_secret, method="taobao.tbk.dg.material.recommend", biz_params=biz_params)
        if _api_call is not None
        else call_tb_api(app_key=app_key, app_secret=app_secret, method="taobao.tbk.dg.material.recommend", biz_params=biz_params, http_session=http_session)
    )
    if res.get("status") != "success":
        err_code = str(res.get("error_code") or "")
        err_msg = str(res.get("error_msg") or "")
        if err_code == "50001" or "无结果" in err_msg:
            return {"status": "success", "total_count": 0, "page": pg, "page_size": sz, "has_more": False, "data": []}
        return res
    body = res.get("data")
    resp_body = body.get("tbk_dg_material_recommend_response") if isinstance(body, dict) else None
    if not isinstance(resp_body, dict) or "result_list" not in resp_body:
        return _error("PROTOCOL_ERROR", "淘宝物料推荐响应缺少结果列表")
    raw_list = _list(resp_body.get("result_list"), "map_data")
    items = [
        cleaned for item in raw_list if isinstance(item, dict)
        for cleaned in (clean_tb_goods_item(item, source_type="tbk_recommend"),)
        if cleaned is not None
    ]
    raw_total = resp_body.get("total_count")
    try:
        parsed_total = int(raw_total) if raw_total is not None and str(raw_total).strip() != "" else None
    except (TypeError, ValueError):
        parsed_total = None
    total = parsed_total if parsed_total is not None else len(items)
    has_more = bool(items) and (pg * sz < parsed_total if parsed_total is not None else len(raw_list) >= sz)
    return {
        "status": "success",
        "total_count": total,
        "page": pg,
        "page_size": sz,
        "has_more": has_more,
        "data": items,
    }


def create_tb_tpwd(
    app_key: str,
    app_secret: str,
    url: str,
    text: str = "精选好物大促抢购口令",
    _api_call=None
) -> Dict[str, Any]:
    """调用 11655 接口 (taobao.tbk.tpwd.create) 生成真机淘口令。"""
    clean_url = str(url or "").strip()
    if clean_url.startswith("//"):
        clean_url = "https:" + clean_url

    if not clean_url:
        return {"status": "error", "error_msg": "推广落地页链接不能为空"}

    biz_params = {
        "url": clean_url,
        "text": text or "精选好物大促抢购口令",
    }
    res = (_api_call or call_tb_api)(
        app_key=app_key,
        app_secret=app_secret,
        method="taobao.tbk.tpwd.create",
        biz_params=biz_params,
    )
    if res.get("status") != "success":
        return res

    tpwd_body = res.get("data", {}).get("tbk_tpwd_create_response", {}).get("data", {})
    password_simple = tpwd_body.get("password_simple") or ""
    model = tpwd_body.get("model") or ""
    if not (password_simple or model):
        return _error("PROTOCOL_ERROR", "官方淘口令响应缺少口令")

    return {
        "status": "success",
        "password_simple": password_simple,
        "model": model,
        "raw": tpwd_body,
    }


def check_publisher_relation(
    app_key: str,
    app_secret: str,
    uid: str,
    session_key: Optional[str] = None,
    _api_call=None
) -> Dict[str, Any]:
    """官方 external_id 精确核验；session 错误绝不能降级为未授权。"""
    if not session_key:
        return _error("CONFIG_ERROR", "淘宝渠道核验缺少 session")
    if not _text(uid):
        return _error("INVALID_IDENTITY", "渠道核验需要站内 UID")
    call_kwargs = {
        "app_key": app_key, "app_secret": app_secret,
        "method": "taobao.tbk.sc.publisher.info.get",
        "biz_params": {"relation_app": "common", "info_type": 1, "external_id": _text(uid),
                       "page_no": 1, "page_size": 100},
        "session_key": session_key,
    }
    call_kwargs["timeout"] = (2.0, 3.0)
    res = (_api_call or call_tb_api)(**call_kwargs)
    if res.get("status") != "success":
        return res
    body = res.get("data", {}).get("tbk_sc_publisher_info_get_response", {}).get("data")
    if not isinstance(body, dict) or "inviter_list" not in body and body.get("total_count") not in (0, "0"):
        return _error("PROTOCOL_ERROR", "渠道核验响应缺少关系列表")
    records = _list(body.get("inviter_list"), "map_data")
    if not records:
        return {"status": "success", "is_authorized": False, "bind": 0, "identity": {}, "records": []}
    if body.get("total_count") is not None:
        try:
            total = int(body["total_count"])
        except (TypeError, ValueError):
            return _error("PROTOCOL_ERROR", "渠道核验响应数量无效")
        if total < 0 or total != len(records):
            return _error("INCOMPLETE_IDENTITY", "渠道关系响应不完整，不能确认用户身份")
    matches = [r for r in records if isinstance(r, dict) and _text(r.get("external_id")) == _text(uid)
               and _text(r.get("relation_id")) not in ("", "0")]
    if records and len(matches) != len(records):
        return _error("IDENTITY_MISMATCH", "渠道核验响应未证实当前用户身份")
    relations = {_text(r["relation_id"]) for r in matches}
    if len(relations) > 1:
        return _error("AMBIGUOUS_IDENTITY", "官方返回多个渠道身份，需要人工核验")
    if relations:
        matched = matches[0] if matches else {}
        identity = {"relation_id": next(iter(relations)), "external_id": _text(uid)}
        if matched.get("account_name"):
            identity["account_name"] = _text(matched["account_name"])
        if matched.get("create_date"):
            identity["create_date"] = _text(matched["create_date"])
    else:
        identity = {}
    return {"status": "success", "is_authorized": bool(identity), "bind": int(bool(identity)),
            "identity": identity, "records": records}


def get_tb_goods_details(app_key, app_secret, item_ids, relation_id=None, _api_call=None):
    """升级官方详情：每批最多20个，保持字符串ID，不以列表位置推断映射。"""
    ids = [_text(i) for i in item_ids if _text(i)]
    if not ids or len(ids) > 20 or any("," in i for i in ids):
        return _error("INVALID_ARGUMENT", "官方详情每批需要1至20个商品ID")
    res = (_api_call or call_tb_api)(
        app_key=app_key, app_secret=app_secret, method="taobao.tbk.item.info.upgrade.get",
        biz_params={"item_id": ",".join(ids), "relation_id": relation_id},
    )
    if res.get("status") != "success":
        return res
    body = res.get("data", {}).get("tbk_item_info_upgrade_get_response")
    if not isinstance(body, dict) or "results" not in body:
        return _error("PROTOCOL_ERROR", "官方详情响应缺少结果列表")
    items = [clean_tb_goods_item(i, "tbk_item_info_upgrade")
             for i in _list(body.get("results"), "tbk_item_detail")]
    return {"status": "success", "data": [i for i in items if i]}


def generate_tb_authority_url(app_key, app_secret, uid, session_key=None, inviter_code=None, _api_call=None):
    """邀请码可配置或由官方获取；每个邀请显式携带站内 external_id。"""
    if not app_key or not app_secret:
        return _error("CONFIG_ERROR", "未配置淘宝开放平台 AppKey 或 AppSecret")
    if not _text(uid):
        return _error("INVALID_IDENTITY", "授权链接需要站内 UID")
    code = _text(inviter_code)
    if not code:
        if not session_key:
            return _error("CONFIG_ERROR", "淘宝授权缺少邀请码及 session")
        res = (_api_call or call_tb_api)(
            app_key=app_key, app_secret=app_secret, session_key=session_key,
            method="taobao.tbk.sc.invitecode.get", biz_params={"relation_app": "common", "code_type": 1},
        )
        if res.get("status") == "success":
            code = _text(res.get("data", {}).get("tbk_sc_invitecode_get_response", {}).get("data", {}).get("inviter_code"))
        if not code:
            return _error("CONFIG_ERROR", "淘宝授权缺少邀请码配置，请在 config.local.json 中配置 taobao_inviter_code")
    url = "https://mos.m.taobao.com/inviter/register?" + urlencode({
        "inviterCode": code, "src": "pub", "app": "common", "externalId": _text(uid),
    })
    return {"status": "success", "auth_url": url, "short_url": "", "we_app_info": {}}


_TB_CFG_WRITE_LOCK = threading.Lock()


def reload_or_refresh_tb_credentials(app_key: str, app_secret: str, current_session: str = "", _api_call=None) -> Dict[str, str]:
    """
    当检测到 taobao_session 缺失或触发 27:invalid-sessionkey 时：
    1. 检查 config.local.json 是否已更新了新的 taobao_session（支持热重载免重启）；
    2. 若配置了 taobao_refresh_token，尝试调用淘宝开放平台 taobao.top.auth.token.refresh 自动刷新并原子持久化。
    """
    result = {"session_key": _text(current_session)}
    cfg_env = os.environ.get("WEB_CONFIG_FILE", "").strip()
    config_path = resolve_config_path()
    if not cfg_env and not config_path.is_file():
        alt = config_path.parent.parent / "config.local.json"
        if alt.is_file():
            config_path = alt

    local_cfg = None
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                local_cfg = loaded
        except Exception:
            local_cfg = None

    cfg_view = local_cfg or {}
    file_session = _text(cfg_view.get("taobao_session") or cfg_view.get("TB_SESSION"))
    if file_session and file_session != _text(current_session):
        result["session_key"] = file_session
        return result

    refresh_token = _text(
        os.environ.get("TB_REFRESH_TOKEN")
        or cfg_view.get("taobao_refresh_token")
        or cfg_view.get("TB_REFRESH_TOKEN")
    )
    if refresh_token and app_key and app_secret:
        res = (_api_call or call_tb_api)(
            app_key=app_key,
            app_secret=app_secret,
            method="taobao.top.auth.token.refresh",
            biz_params={"refresh_token": refresh_token},
        )
        if res.get("status") == "success":
            token_result = (res.get("data") or {}).get("top_auth_token_refresh_response", {}).get("token_result")
            if isinstance(token_result, str):
                try:
                    token_result = json.loads(token_result)
                except Exception:
                    token_result = {}
            if isinstance(token_result, dict):
                new_session = _text(token_result.get("access_token"))
                new_refresh = _text(token_result.get("refresh_token"))
                if new_session:
                    result["session_key"] = new_session
                    # 仅当原始配置文件已被成功解析为非空字典时才原子写回，避免覆写损坏的配置文件
                    if config_path.is_file() and isinstance(local_cfg, dict) and local_cfg:
                        updates = {"taobao_session": new_session}
                        if new_refresh:
                            updates["taobao_refresh_token"] = new_refresh
                        with _TB_CFG_WRITE_LOCK:
                            try:
                                persist_config_updates(updates, config_file=config_path)
                            except Exception:
                                pass
    return result


class TaobaoClient:
    """实例边界用于默认配置装配与无网络协议测试；保留现有模块函数兼容。"""
    def __init__(self, app_key, app_secret, adzone_id="", session_key="", inviter_code="",
                 pid_prefix="", http_session=None):
        self.app_key, self.app_secret = app_key, app_secret
        self.adzone_id = adzone_id
        self.session_key = session_key
        self.inviter_code = inviter_code
        self.pid_prefix = pid_prefix or ""
        self.http_session = http_session

    def _call(self, **kwargs):
        return call_tb_api(**kwargs, http_session=self.http_session)

    def try_refresh_session(self) -> str:
        """尝试从 config.local.json 热重载或通过 refresh_token 刷新 session_key。"""
        refreshed = reload_or_refresh_tb_credentials(
            self.app_key, self.app_secret, self.session_key, _api_call=self._call
        )
        new_session = refreshed.get("session_key") or ""
        if new_session and new_session != self.session_key:
            self.session_key = new_session
            return new_session
        return ""

    def search_goods(self, keyword, page=1, page_size=20, **kwargs):
        if not self.adzone_id:
            return _error("CONFIG_ERROR", "淘宝物料搜索缺少广告位 adzone_id")
        return search_tb_material_upgrade(self.app_key, self.app_secret, self.adzone_id,
                                         keyword=keyword, page=page, page_size=page_size,
                                         _api_call=self._call, **kwargs)

    def get_material_recommend(self, material_id=3756, page_no=1, page_size=20):
        if not self.adzone_id:
            return _error("CONFIG_ERROR", "淘宝物料推荐缺少广告位 adzone_id")
        return get_tb_material_recommend(
            self.app_key,
            self.app_secret,
            self.adzone_id,
            material_id=material_id,
            page_no=page_no,
            page_size=page_size,
            _api_call=self._call,
        )

    def get_goods_details(self, item_ids, **kwargs):
        return get_tb_goods_details(self.app_key, self.app_secret, item_ids, _api_call=self._call, **kwargs)

    def create_tpwd(self, url, text="精选好物"):
        return create_tb_tpwd(self.app_key, self.app_secret, url, text, _api_call=self._call)

    def check_authority(self, uid):
        return check_publisher_relation(self.app_key, self.app_secret, uid, self.session_key, _api_call=self._call)

    def generate_authority_url(self, uid):
        return generate_tb_authority_url(self.app_key, self.app_secret, uid, self.session_key,
                                         self.inviter_code, _api_call=self._call)
