# -*- coding: utf-8 -*-
"""
淘宝客 (阿里妈妈 TOP + 好单库榜单 + 折淘客渠道/订单) 核心能力标准基线脚本 (生产级精简版)
================================================================================
本脚本作为全项目淘宝 API 的唯一权威基线，维护并验证项目用到的全部 6 项核心 API 操作：
1. 获取热销榜单商品 (好单库 v2.haodanku.com/sales_list [sale_type=1 2h / 2 24h] + 官方批量详情补全)
2. 关键词搜索商品 (taobao.tbk.dg.material.optional.upgrade)
3. 获取商品详情 (taobao.tbk.item.info.upgrade.get)
4. 双轨推广转链与淘口令生成 (折淘客 open_gaoyongzhuanlian_tkl.ashx 渠道轨 + 官方 material.optional.upgrade & tpwd.create 站长轨)
5. 推手渠道备案查询与授权邀请码生成 (taobao.tbk.sc.publisher.info.get + taobao.tbk.sc.invitecode.get)
6. 增量订单拉取与两跳签名网关解析 (折淘客 open_dingdanchaxun2.ashx, 支持 ZTK_APPKEY_LIST 多 Key 故障切换)
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

# 确保 Windows 终端下能够正常输出中文与特殊符号
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ==============================================================================
# 0. 配置加载与基础签名/故障切换协议
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


def parse_appkey_list(primary: Any, extra_list: Any = None) -> List[str]:
    """解析并去重折淘客 AppKey 列表（支持逗号分隔与数组混合配置）"""
    keys: List[str] = []
    raw_items: List[Any] = []
    if isinstance(primary, (list, tuple)):
        raw_items.extend(primary)
    elif primary:
        raw_items.append(primary)
    if isinstance(extra_list, (list, tuple)):
        raw_items.extend(extra_list)
    elif extra_list:
        raw_items.append(extra_list)

    for item in raw_items:
        for part in str(item or "").split(","):
            clean = part.strip()
            if clean and clean not in keys:
                keys.append(clean)
    return keys


_cfg = load_local_config()

TB_APP_KEY = _cfg.get("myself_taobao_app_key", "")
TB_APP_SECRET = _cfg.get("myself_taobao_app_secret", "")
TB_PID = _cfg.get("myself_taobao_pid", "")
TB_ADZONE_ID = _cfg.get("taobao_adzone_id") or (TB_PID.split("_")[-1] if "_" in TB_PID else "")
TB_SESSION_KEY = _cfg.get("taobao_session", "")
TB_INVITER_CODE = _cfg.get("taobao_inviter_code", "")
HDK_API_KEY = _cfg.get("haodanku_api_key", "")
ZTK_APPKEY_LIST = parse_appkey_list(_cfg.get("zhetaoke_appkey", ""), _cfg.get("zhetaoke_appkey_list"))
ZTK_APPKEY = ZTK_APPKEY_LIST[0] if ZTK_APPKEY_LIST else ""
ZTK_SID = _cfg.get("zhetaoke_sid", "")

TOP_GATEWAY_URL = "https://eco.taobao.com/router/rest"
HDK_SALES_URL = "http://v2.haodanku.com/sales_list"
ZTK_CONVERT_TKL_URL = "http://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"

_ZTK_KEY_FAILURE_KEYWORDS = (
    "appkey", "key", "频次", "频率", "超限", "上限", "次数", "黑名单",
    "封禁", "未授权", "到期", "过期", "积分", "余额", "限制",
)


def _should_failover_ztk_key(payload: Any) -> bool:
    """判断折淘客响应是否属于 AppKey 额度/权限/频次问题，以触发备用 Key 切换"""
    if not isinstance(payload, dict):
        return True
    status_val = payload.get("status")
    code_val = payload.get("code")
    if status_val in (200, "200") or code_val in (200, "200"):
        return False
    # 渠道 relationId 失效属于推手侧错误，换 AppKey 结果相同，无需切换
    msg = " ".join(str(payload.get(k) or "") for k in ("content", "msg", "message", "sub_msg")).lower()
    if "relationid" in msg:
        return False
    return any(kw in msg for kw in _ZTK_KEY_FAILURE_KEYWORDS) or status_val not in (None, 200, "200")


def request_ztk_with_failover(url: str, params: Dict[str, Any], timeout: int = 12) -> Dict[str, Any]:
    """带多 AppKey 自动故障切换的折淘客 GET 请求封装"""
    keys = ZTK_APPKEY_LIST or ([params["appkey"]] if params.get("appkey") else [])
    if not keys:
        return {"status": "error", "error_msg": "未配置折淘客 zhetaoke_appkey"}

    last_error = "折淘客请求失败"
    for idx, key in enumerate(keys):
        req_params = dict(params, appkey=key)
        try:
            resp = requests.get(url, params=req_params, timeout=timeout)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                data = {"raw_text": resp.text.strip()}

            if isinstance(data, dict) and not _should_failover_ztk_key(data):
                return {"status": "success", "data": data, "used_appkey": key}
            if isinstance(data, dict) and "raw_text" in data and data["raw_text"].startswith("http"):
                return {"status": "success", "data": data, "used_appkey": key}

            last_error = f"Key[{idx + 1}] 返回异常: {data}"
        except Exception as e:
            last_error = f"Key[{idx + 1}] 网络异常: {e}"
    return {"status": "error", "error_msg": last_error}


def generate_tb_sign(params: Dict[str, Any], app_secret: str) -> str:
    """阿里妈妈 TOP 标准 MD5 签名"""
    sorted_keys = sorted(params.keys())
    sign_str = app_secret
    for k in sorted_keys:
        v = params[k]
        if v is not None and k != "sign":
            sign_str += f"{k}{v}"
    sign_str += app_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def call_tb_api(
    app_key: str,
    app_secret: str,
    method: str,
    biz_params: Optional[Dict[str, Any]] = None,
    session_key: Optional[str] = None,
    timeout: int = 15,
) -> Dict[str, Any]:
    """通用淘宝开放平台 TOP 网关请求封装"""
    if not app_key or not app_secret:
        return {"status": "error", "error_msg": "淘宝 AppKey 或 AppSecret 未配置"}

    sys_params: Dict[str, Any] = {
        "method": method,
        "app_key": app_key,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
    }
    if session_key:
        sys_params["session"] = session_key

    all_params = dict(sys_params)
    if biz_params:
        for k, v in biz_params.items():
            if v is not None:
                if isinstance(v, bool):
                    all_params[k] = "true" if v else "false"
                elif isinstance(v, (dict, list)):
                    all_params[k] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                else:
                    all_params[k] = str(v)

    all_params["sign"] = generate_tb_sign(all_params, app_secret)
    try:
        resp = requests.post(
            TOP_GATEWAY_URL,
            data=all_params,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            timeout=timeout,
        )
        resp.raise_for_status()
        res_json = resp.json()
        if "error_response" in res_json:
            err = res_json["error_response"]
            return {
                "status": "error",
                "error_code": err.get("sub_code") or err.get("code"),
                "error_msg": err.get("sub_msg") or err.get("msg", "TOP 网关调用失败"),
            }
        return {"status": "success", "data": res_json}
    except Exception as e:
        return {"status": "error", "error_msg": f"网络或解析异常: {e}"}


def _fix_url(url: str) -> str:
    if not url:
        return ""
    u = str(url).strip()
    if u.startswith("//"):
        return "https:" + u
    return u


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def clean_tb_goods_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """统一清洗淘宝新版嵌套结构 (item_basic_info / price_promotion_info / publish_info) 与扁平结构"""
    basic = item.get("item_basic_info") or item
    price_info = item.get("price_promotion_info") or item
    pub_info = item.get("publish_info") or item
    income_info = pub_info.get("income_info") or {}

    item_id = str(item.get("item_id") or basic.get("item_id") or item.get("num_iid") or "")
    input_iid = str(item.get("input_item_iid") or "")

    zk_price = _safe_float(price_info.get("zk_final_price"))
    reserve_price = _safe_float(price_info.get("reserve_price"), zk_price)
    final_promo_price = _safe_float(price_info.get("final_promotion_price"))

    coupon_amount = _safe_float(price_info.get("coupon_amount") or pub_info.get("coupon_amount"))
    if coupon_amount == 0:
        promo_list = (price_info.get("final_promotion_path_list") or {}).get("final_promotion_path_map_data") or []
        for p in promo_list:
            if "券" in str(p.get("promotion_title", "")):
                coupon_amount += _safe_float(p.get("promotion_fee"))

    final_price = final_promo_price if final_promo_price > 0 else round(max(zk_price - coupon_amount, 0.0), 2)
    if coupon_amount == 0 and zk_price > final_price > 0:
        coupon_amount = round(zk_price - final_price, 2)

    raw_rate = _safe_float(
        pub_info.get("income_rate")
        or income_info.get("commission_rate")
        or item.get("commission_rate")
    )
    comm_rate = round(raw_rate / 100.0, 2) if raw_rate > 100 else round(raw_rate, 2)
    comm_amount = _safe_float(income_info.get("commission_amount") or item.get("commission_amount"))
    if comm_amount == 0 and final_price > 0 and comm_rate > 0:
        comm_amount = round(final_price * (comm_rate / 100.0), 2)

    small_imgs = basic.get("small_images", {})
    gallery = small_imgs.get("string", []) if isinstance(small_imgs, dict) else (small_imgs if isinstance(small_imgs, list) else [])
    gallery = [_fix_url(u) for u in gallery if u]
    main_img = _fix_url(basic.get("white_image") or basic.get("pict_url") or "")
    if main_img and main_img not in gallery:
        gallery.insert(0, main_img)

    return {
        "platform": "taobao",
        "goods_id": input_iid if input_iid.isdigit() else item_id,
        "goods_sign": item_id,
        "input_item_iid": input_iid,
        "title": basic.get("title", ""),
        "sub_title": basic.get("sub_title", ""),
        "name": basic.get("title", ""),
        "image_url": main_img,
        "carousel_images": gallery,
        "mall_name": basic.get("shop_title") or ("天猫旗舰店" if str(basic.get("user_type")) == "1" else "淘宝好店"),
        "brand_name": basic.get("brand_name", ""),
        "category_name": basic.get("level_one_category_name", "") or basic.get("cat_leaf_name", ""),
        "sales_tip": str(basic.get("annual_vol") or basic.get("volume") or "0"),
        "original_price": round(reserve_price or zk_price, 2),
        "zk_final_price": round(zk_price, 2),
        "price": round(final_price, 2),
        "coupon_amount": round(coupon_amount, 2),
        "has_coupon": coupon_amount > 0,
        "commission_rate": comm_rate,
        "commission_amount": round(comm_amount, 2),
        "click_url": _fix_url(pub_info.get("click_url", "")),
        "coupon_share_url": _fix_url(pub_info.get("coupon_share_url") or pub_info.get("click_url", "")),
    }


# ==============================================================================
# 核心功能 1：获取热销榜单商品 (好单库 sales_list + 淘宝官方详情补全)
# ==============================================================================
def get_tb_goods_details(item_ids: List[str]) -> Dict[str, Any]:
    """批量调用 taobao.tbk.item.info.upgrade.get 获取淘宝官方商品详情（单次最多 20 个 ID）"""
    if not item_ids:
        return {"status": "error", "error_msg": "item_ids 不能为空"}
    clean_ids = [str(i).strip() for i in item_ids[:20] if str(i).strip()]
    res = call_tb_api(TB_APP_KEY, TB_APP_SECRET, "taobao.tbk.item.info.upgrade.get", {
        "item_id": ",".join(clean_ids),
        "get_tlj_info": 0,
    })
    if res["status"] != "success":
        return res

    results = (
        res["data"]
        .get("tbk_item_info_upgrade_get_response", {})
        .get("results", {})
        .get("tbk_item_detail")
        or []
    )
    cleaned_list = [clean_tb_goods_item(item) for item in results]
    return {"status": "success", "total_count": len(cleaned_list), "data": cleaned_list}


def get_hot_rank_goods(sale_type: int = 1, min_id: int = 1, back: int = 20) -> Dict[str, Any]:
    """
    好单库实时榜单 + 阿里妈妈官方接口补全双引擎：
    - sale_type=1: 实时热销榜 (2小时销量榜, 对应前端 channel_type=5)
    - sale_type=2: 今日爆款榜 (全天销量榜, 对应前端 channel_type=1)
    """
    if not HDK_API_KEY:
        return {"status": "error", "error_msg": "未配置好单库 haodanku_api_key"}

    try:
        resp = requests.get(HDK_SALES_URL, params={
            "apikey": HDK_API_KEY,
            "sale_type": sale_type,
            "cid": 0,
            "min_id": min_id,
            "back": back,
        }, timeout=10)
        hdk_data = resp.json()
    except Exception as e:
        return {"status": "error", "error_msg": f"好单库请求异常: {e}"}

    if hdk_data.get("code") not in (1, 200):
        return {"status": "error", "error_msg": hdk_data.get("msg", "好单库返回错误")}

    raw_items = hdk_data.get("data") or []
    next_min_id = hdk_data.get("min_id", min_id + 1)
    item_ids = [str(x.get("itemid")).strip() for x in raw_items if x.get("itemid")]
    if not item_ids:
        return {"status": "success", "next_min_id": next_min_id, "data": []}

    enriched: List[Dict[str, Any]] = []
    for i in range(0, len(item_ids), 20):
        batch_res = get_tb_goods_details(item_ids[i : i + 20])
        if batch_res["status"] == "success":
            enriched.extend(batch_res["data"])

    return {
        "status": "success",
        "sale_type": sale_type,
        "next_min_id": next_min_id,
        "total_count": len(enriched),
        "data": enriched,
    }


# ==============================================================================
# 核心功能 2：关键词搜索商品 (taobao.tbk.dg.material.optional.upgrade)
# ==============================================================================
def search_tb_goods(
    keyword: str,
    page_no: int = 1,
    page_size: int = 20,
    has_coupon: bool = False,
    sort: Optional[str] = None,
    relation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """调用阿里妈妈通用物料搜索升级版 (taobao.tbk.dg.material.optional.upgrade)"""
    if not TB_ADZONE_ID:
        return {"status": "error", "error_msg": "未配置淘宝推广位 TB_ADZONE_ID"}

    biz_params: Dict[str, Any] = {
        "adzone_id": int(TB_ADZONE_ID) if str(TB_ADZONE_ID).isdigit() else TB_ADZONE_ID,
        "q": keyword,
        "page_no": page_no,
        "page_size": page_size,
        "has_coupon": has_coupon,
        "get_topn_rate": 0,
    }
    if sort:
        biz_params["sort"] = sort
    if relation_id:
        biz_params["relation_id"] = str(relation_id)

    res = call_tb_api(TB_APP_KEY, TB_APP_SECRET, "taobao.tbk.dg.material.optional.upgrade", biz_params)
    if res["status"] != "success":
        return res

    resp_body = res["data"].get("tbk_dg_material_optional_upgrade_response", {})
    raw_list = (resp_body.get("result_list") or {}).get("map_data") or []
    cleaned_list = [clean_tb_goods_item(item) for item in raw_list]
    return {
        "status": "success",
        "total_count": resp_body.get("total_results", len(cleaned_list)),
        "data": cleaned_list,
    }


# ==============================================================================
# 核心功能 3：单品详情获取 (get_tb_single_detail)
# ==============================================================================
def get_tb_single_detail(item_id: str) -> Dict[str, Any]:
    """获取单个淘宝商品详情"""
    res = get_tb_goods_details([item_id])
    if res["status"] != "success":
        return res
    if not res["data"]:
        return {"status": "error", "error_msg": f"未找到商品 {item_id} 的详情"}
    return {"status": "success", "data": res["data"][0]}


# ==============================================================================
# 核心功能 4：双轨推广转链与淘口令生成 (折淘客渠道轨 + 官方站长轨)
# ==============================================================================
def create_tpwd(url: str, text: str = "粉丝专享福利购") -> Dict[str, Any]:
    """调用 taobao.tbk.tpwd.create 将推广长链转换为淘口令"""
    target_url = _fix_url(url)
    if not target_url.startswith("http"):
        return {"status": "error", "error_msg": f"无效的转链 URL: {url}"}

    res = call_tb_api(TB_APP_KEY, TB_APP_SECRET, "taobao.tbk.tpwd.create", {
        "url": target_url,
        "text": text[:15] if text else "粉丝专享福利购",
    })
    if res["status"] != "success":
        return res

    data = (res["data"].get("tbk_tpwd_create_response") or {}).get("data") or {}
    return {
        "status": "success",
        "model": data.get("model", ""),
        "password_simple": data.get("password_simple", ""),
    }


def convert_channel_url_by_ztk(target_url_or_id: str, relation_id: str) -> Dict[str, Any]:
    """调用折淘客高佣转链接口生成带 relation_id 的专属渠道淘口令与二合一链接（支持多 Key 故障切换）"""
    if not ZTK_SID or not ZTK_APPKEY_LIST:
        return {"status": "error", "error_msg": "折淘客 SID 或 AppKey 未配置"}

    failover_res = request_ztk_with_failover(ZTK_CONVERT_TKL_URL, {
        "sid": ZTK_SID,
        "pid": TB_PID,
        "num_iid": target_url_or_id,
        "relation_id": str(relation_id),
        "signurl": 5,
    })
    if failover_res["status"] != "success":
        return failover_res

    res_data = failover_res["data"]
    if res_data.get("status") == 200 and isinstance(res_data.get("content"), list) and res_data["content"]:
        item_info = res_data["content"][0]
        final_link = _fix_url(item_info.get("coupon_click_url") or item_info.get("item_url", ""))
        return {
            "status": "success",
            "is_channel": True,
            "relation_id": str(relation_id),
            "final_link": final_link,
            "tpwd": item_info.get("tkl", ""),
            "title": item_info.get("title", ""),
            "final_price": item_info.get("quanhou_jiage", ""),
            "commission_rate": item_info.get("tkrate3", ""),
        }
    return {"status": "error", "error_msg": f"折淘客渠道转链失败: {res_data}"}


def convert_promotion_url(
    item_id: str,
    relation_id: Optional[str] = None,
    title: str = "粉丝专享福利购",
) -> Dict[str, Any]:
    """
    淘宝双轨制转链：
    - 若传入 relation_id：先通过官方接口拿标准商品链接，再走折淘客高佣渠道转链生成专属 relation_id 淘口令
    - 若无 relation_id：走阿里妈妈官方搜索/详情匹配 + taobao.tbk.tpwd.create 生成站长通用淘口令
    """
    if relation_id:
        detail_res = get_tb_single_detail(item_id)
        target_input = item_id
        if detail_res["status"] == "success":
            d = detail_res["data"]
            target_input = d.get("coupon_share_url") or d.get("click_url") or item_id
            title = d.get("title") or title
        return convert_channel_url_by_ztk(target_input, str(relation_id))

    search_res = search_tb_goods(item_id, page_no=1, page_size=10)
    target_item = None
    if search_res["status"] == "success":
        for candidate in search_res["data"]:
            if item_id in (candidate.get("goods_id"), candidate.get("goods_sign"), candidate.get("input_item_iid")):
                target_item = candidate
                break
        if not target_item and search_res["data"]:
            target_item = search_res["data"][0]

    if not target_item:
        detail_res = get_tb_single_detail(item_id)
        if detail_res["status"] == "success":
            target_item = detail_res["data"]

    if not target_item:
        return {"status": "error", "error_msg": f"未能解析商品 {item_id} 的推广链接"}

    promo_url = target_item.get("coupon_share_url") or target_item.get("click_url", "")
    if not promo_url:
        return {"status": "error", "error_msg": "官方返回商品缺失推广链接"}

    tpwd_res = create_tpwd(promo_url, text=target_item.get("title") or title)
    if tpwd_res["status"] != "success":
        return tpwd_res

    return {
        "status": "success",
        "is_channel": False,
        "final_link": promo_url,
        "tpwd": tpwd_res.get("password_simple") or tpwd_res.get("model", ""),
        "title": target_item.get("title", title),
    }


# ==============================================================================
# 核心功能 5：推手渠道备案查询与授权邀请码生成 (publisher.info.get & invitecode.get)
# ==============================================================================
def check_publisher_relation_by_user(uid: str) -> Dict[str, Any]:
    """查询指定 uid (external_id) 是否已在站长渠道下完成淘宝客备案及对应的 relation_id"""
    if not TB_SESSION_KEY:
        return {"status": "error", "error_msg": "未配置站长授权 SessionKey (taobao_session)"}

    res = call_tb_api(
        TB_APP_KEY,
        TB_APP_SECRET,
        "taobao.tbk.sc.publisher.info.get",
        {"info_type": 1, "relation_app": "common", "external_id": str(uid), "page_no": 1, "page_size": 10},
        session_key=TB_SESSION_KEY,
    )
    if res["status"] != "success":
        return res

    inviter_list = (
        (res["data"].get("tbk_sc_publisher_info_get_response") or {})
        .get("data", {})
        .get("inviter_list", {})
        .get("map_data")
        or []
    )
    for record in inviter_list:
        if str(record.get("external_id") or "") == str(uid) and record.get("relation_id"):
            return {
                "status": "success",
                "is_authorized": True,
                "bind": 1,
                "relation_id": str(record["relation_id"]),
                "account_name": record.get("account_name", ""),
            }
    return {"status": "success", "is_authorized": False, "bind": 0, "relation_id": None}


def generate_tb_authority_url(uid: str) -> Dict[str, Any]:
    """生成携带 externalId=uid 的手淘H5渠道备案邀请链接"""
    inviter_code = TB_INVITER_CODE
    if not inviter_code and TB_SESSION_KEY:
        res = call_tb_api(
            TB_APP_KEY,
            TB_APP_SECRET,
            "taobao.tbk.sc.invitecode.get",
            {"relation_app": "common", "code_type": 1},
            session_key=TB_SESSION_KEY,
        )
        if res["status"] == "success":
            inviter_code = (
                (res["data"].get("tbk_sc_invitecode_get_response") or {})
                .get("data", {})
                .get("inviter_code", "")
            )

    if not inviter_code:
        return {"status": "error", "error_msg": "无法获取淘宝渠道邀请码 inviter_code"}

    encoded_uid = quote_plus(str(uid))
    auth_url = f"https://mos.m.taobao.com/inviter/register?inviterCode={inviter_code}&src=pub&app=common&externalId={encoded_uid}"
    return {
        "status": "success",
        "inviter_code": inviter_code,
        "external_id": str(uid),
        "auth_url": auth_url,
    }


def sync_publisher_after_auth(uid: str) -> Dict[str, Any]:
    """用户在手淘完成授权后，回查并同步 relation_id"""
    return check_publisher_relation_by_user(uid)


# ==============================================================================
# 核心功能 6：增量订单拉取 (折淘客 open_dingdanchaxun2.ashx + 多 Key 故障切换)
# ==============================================================================
def fetch_increment_orders_strictly(
    start_time_str: str,
    end_time_str: str,
    query_type: int = 4,
    order_scene: int = 2,
    page_no: int = 1,
    page_size: int = 100,
    position_index: Optional[str] = None,
) -> Dict[str, Any]:
    """
    通过折淘客订单查询接口拉取淘宝联盟订单（支持直返 JSON 及 signurl=2 两跳网关，内置多 Key 切换）：
    - query_type=4: 按更新时间查询
    - order_scene=2: 渠道管理订单（含 relation_id）
    """
    if not ZTK_SID or not ZTK_APPKEY_LIST:
        return {"status": "error", "error_msg": "未配置折淘客 SID 或 AppKey"}

    params: Dict[str, Any] = {
        "sid": ZTK_SID,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "query_type": query_type,
        "order_scene": order_scene,
        "page_no": page_no,
        "page_size": min(page_size, 100),
        "jump_type": 1,
        "signurl": 2,
    }
    if position_index:
        params["position_index"] = position_index

    failover_res = request_ztk_with_failover(ZTK_ORDER_URL, params)
    if failover_res["status"] != "success":
        return {"status": "error", "error_msg": failover_res["error_msg"], "order_list": []}

    raw_data = failover_res["data"]
    # 处理 signurl=2 返回的两跳签名 URL
    signed_url = None
    if isinstance(raw_data, dict):
        if isinstance(raw_data.get("url"), str) and raw_data["url"].startswith("http"):
            signed_url = raw_data["url"]
        elif isinstance(raw_data.get("data"), str) and raw_data["data"].startswith("http"):
            signed_url = raw_data["data"]
        elif isinstance(raw_data.get("raw_text"), str) and raw_data["raw_text"].startswith("http"):
            signed_url = raw_data["raw_text"]

    if signed_url:
        try:
            hop_resp = requests.get(signed_url, timeout=15)
            hop_resp.raise_for_status()
            raw_data = hop_resp.json()
        except Exception as e:
            return {"status": "error", "error_msg": f"淘宝订单二跳请求失败: {e}", "order_list": []}

    resp_root = (
        raw_data.get("tbk_sc_order_details_get_response")
        or raw_data.get("tbk_order_details_get_response")
        or {}
    )
    data_node = resp_root.get("data") or {}
    raw_orders = (data_node.get("results") or {}).get("publisher_order_dto") or []
    if isinstance(raw_orders, dict):
        raw_orders = [raw_orders]

    return {
        "status": "success",
        "total_count": len(raw_orders),
        "has_next": bool(data_node.get("has_next", False)),
        "next_position_index": data_node.get("position_index", ""),
        "order_list": raw_orders,
    }


# ==============================================================================
# 全链路自检验证主流程
# ==============================================================================
def main():
    print("================================================================================")
    print("淘宝客 (TOP + 好单库 + 折淘客) 全链路标准基线验证脚本")
    print("================================================================================")
    if not TB_APP_KEY or not TB_APP_SECRET:
        print("未检测到淘宝配置 (myself_taobao_app_key / secret)，请先检查 config.local.json。")
        return

    test_uid = "u_baseline_test"
    sample_id = None

    # 1. 验证热销榜单 (sale_type=1 实时热销榜 2h)
    print("\n[1/6] 验证淘宝热销榜单拉取 (sale_type=1 实时热销榜)...")
    rank_res = get_hot_rank_goods(sale_type=1, min_id=1, back=10)
    if rank_res["status"] == "success" and rank_res["data"]:
        first = rank_res["data"][0]
        sample_id = first.get("goods_id") or first.get("goods_sign")
        print(f" -> 成功拉取 {len(rank_res['data'])} 条榜单商品，首条: {first['title'][:25]} (¥{first['price']})")
    else:
        print(f" -> 榜单拉取结果: {rank_res.get('error_msg')}")

    # 2. 验证关键词搜索
    print("\n[2/6] 验证淘宝关键词搜索 (taobao.tbk.dg.material.optional.upgrade)...")
    search_res = search_tb_goods("抽纸", page_no=1, page_size=5)
    if search_res["status"] == "success" and search_res["data"]:
        first = search_res["data"][0]
        sample_id = sample_id or first.get("goods_id") or first.get("goods_sign")
        print(f" -> 搜索成功，共命中 {search_res['total_count']} 条，首条: {first['title'][:25]} (¥{first['price']})")
    else:
        print(f" -> 搜索失败: {search_res.get('error_msg')}")

    # 3. 验证单品详情
    print("\n[3/6] 验证淘宝单品详情 (taobao.tbk.item.info.upgrade.get)...")
    if sample_id:
        detail_res = get_tb_single_detail(sample_id)
        if detail_res["status"] == "success":
            d = detail_res["data"]
            print(f" -> 详情成功: {d['title'][:25]} | 店铺: {d.get('mall_name')} | 佣金率: {d.get('commission_rate')}%")
        else:
            print(f" -> 详情失败: {detail_res.get('error_msg')}")

    # 4. 验证双轨转链与淘口令生成
    print("\n[4/6] 验证淘宝推广转链与淘口令生成...")
    if sample_id:
        conv_res = convert_promotion_url(sample_id)
        print(f" -> 官方站长轨转链结果: status={conv_res['status']}, tpwd={conv_res.get('tpwd')}")

    # 5. 验证推手渠道备案查询与邀请链接生成
    print("\n[5/6] 验证淘宝推手渠道备案查询与邀请码生成...")
    rel_res = check_publisher_relation_by_user(test_uid)
    print(f" -> 渠道备案查询: {rel_res}")
    if not rel_res.get("is_authorized"):
        auth_url_res = generate_tb_authority_url(test_uid)
        print(f" -> 备案邀请链接: status={auth_url_res['status']}, url={auth_url_res.get('auth_url')}")

    # 6. 验证增量订单查询 (20分钟时间窗)
    print("\n[6/6] 验证淘宝增量订单查询 (最近 20 分钟)...")
    now_dt = datetime.now()
    start_str = (now_dt - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    end_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    ord_res = fetch_increment_orders_strictly(start_str, end_str)
    print(f" -> 订单查询结果: status={ord_res['status']}, 数量={len(ord_res.get('order_list', []))}")


if __name__ == "__main__":
    main()