# -*- coding: utf-8 -*-
"""
拼多多 (多多进宝 DDK) 核心能力标准基线脚本 (生产级精简版)
================================================================================
本脚本作为全项目拼多多 API 的唯一权威基线，维护并验证项目用到的全部 7 项核心 API 操作：
1. 获取热销榜单/频道推荐商品 (pdd.ddk.goods.recommend.get: channel_type=5 实时热销 / 1 今日爆款)
2. 关键词搜索商品 (pdd.ddk.goods.search)
3. 获取商品详情及短链探测降级 (pdd.ddk.goods.detail + pdd.ddk.goods.zs.unit.url.gen 探测)
4. 三层智能兜底转链 (pdd.ddk.rp.prom.url.generate[23] -> pdd.ddk.goods.promotion.url.generate -> zs.unit.url.gen)
5. 推手授权备案查询与生成 (pdd.ddk.member.authority.query + pdd.ddk.rp.prom.url.generate[10])
6. 增量订单拉取与状态清洗 (pdd.ddk.order.list.increment.get)
7. 万能转链与详情反查 (convert_and_resolve_universal_input: 支持短链/长链/goods_sign/分享文案)

================================================================================
【拼多多 DDK API 全链路实证基准规范：哪些可用 (✅) vs 哪些不可用 (❌)】
================================================================================
所有后续业务代码（pdd_client.py / pdd_adapter.py）必须严格以本文件实测结论为准，严禁发散猜测：

一、【核心归因与备案机制：`custom_parameters` 与 `sub_code=60001` 铁律】
  - 拼多多备案按 `(pid, custom_parameters)` 组合独立校验：
    * ✅ 站长级（不传 `custom_parameters` 或传 `None`）：当前 `PDD_PID` 已完成站长备案（`bind=1`），
      搜索、详情、三层转链全部 100% 正常可用。
    * ✅ 已备案推手级（传入已完成 H5 备案的 `custom_parameters='{"uid":"xxx"}'`，`bind=1`）：
      搜索与个人归因转链均 100% 可用，成交后订单通过 `custom_parameters` 原样透出 `{"uid":"xxx"}`。
    * ❌ 未备案推手级（传入尚未在拼多多 H5 完成备案的 `custom_parameters='{"uid":"xxx"}'`，`bind=0`）：
      1. ❌ 调用关键词搜索 (`pdd.ddk.goods.search`) 必报 `[50001:60001] 未传入已经授权备案过的相关参数(pid/custom_parameters)`！
      2. ❌ 调用三层推广转链 (`rp.prom.url.generate[23]` / `goods.promotion.url.generate` / `zs.unit.url.gen`) 必报 `[50001:60001]`！
      3. ❌ 严禁在 `uid=None` 时伪造默认 `"default_user"` 塞入 `custom_parameters`（会导致站长级调用也因 `"default_user"` 未备案而全部报 `60001`）。
      4. ✅ 搜索自动降级铁律：商品搜索属于公共目录浏览能力，若传入登录用户的 `uid` 触发 `60001` 未备案错误，
         必须自动移除 `custom_parameters`（回退站长级）重试，绝不能阻断未备案用户搜索商品。

二、【功能 1：频道推荐/热销榜单 (`pdd.ddk.goods.recommend.get`)】
  - ✅ 可用：`channel_type=5` (实时热销榜)、`channel_type=1` (今日爆款榜)，分页上限 `400` 条 (`offset < 400`)。
  - ✅ 返回新版加密 `goods_sign` 与 `goods_id`，且该接口不校验 `custom_parameters` 是否已备案。

三、【功能 2：全网关键词搜索 (`pdd.ddk.goods.search`)】
  - ✅ 可用：支持关键词、商品标题或 `zs.unit.url.gen` 生成的拼多多短链搜索。
  - ❌ 不可用（参数范围铁律）：`page_size < 10` 或 `page_size > 100` 必报 `参数校验失败：pageSize的取值范围是10-100！`
    （哪怕在短链反查或小页预览场景，传给上游网关的 `page_size` 也必须 `>= 10`）。
  - ❌ 不可用：传入未备案 `custom_parameters` 会直接报错 `60001`（需省略或自动降级为不传 `custom_parameters`）。

四、【功能 3：商品详情 (`pdd.ddk.goods.detail`)】
  - ✅ 可用唯一权威路径：必须传入加密 `goods_sign`（如 `"E932Ts1bVtZgMvVTwfvBVEJF7LgQI9Rz_JQwP5lJQN3"`），
    一步获取完整详情、轮播图与标签。
  - ❌ 不可用（严禁使用，必报 `sub_code=40009`）：
    1. 仅传纯数字 `goods_id_list=[1016415496258]` 调 `pdd.ddk.goods.detail`，网关直接拒绝并报错：
       `[50001:40009] 该查询不是来自终端用户的原始链接，请使用原始链接进行查询 (questionId=388)`！
    2. 手工拼接 `https://mobile.yangkeduo.com/goods.html?goods_id={纯数字ID}` 调 `pdd.ddk.goods.zs.unit.url.gen`，
       同样会被网关以 `sub_code=40009` 拦截！因此列表与详情卡片必须全程携带并透传 `goods_sign`。

五、【功能 4：三层智能兜底转链 (`convert_pdd_promotion_url`)】
  - ⚠️ 注意三个接口的推广位参数名各不相同（严禁混用）：
    1. ✅ 策略 1（超级红包/百亿补贴，`pdd.ddk.rp.prom.url.generate`, `channel_type=23`）：
       推广位参数名为 `p_id_list=[pid]`（数组），商品参数为 `diy_sp_red_packet_param={"goods_sign": goods_sign}`。
    2. ✅ 策略 2（常规单品领券推广，`pdd.ddk.goods.promotion.url.generate`）：
       推广位参数名为 `p_id=pid`（字符串），商品参数为 `goods_sign_list=[goods_sign]`（数组）。
    3. ✅ 策略 3（招商短链清洗，`pdd.ddk.goods.zs.unit.url.gen`）：
       推广位参数名为 `pid=pid`（字符串），`source_url` 仅支持真实生成的拼多多推广短链（如 `https://p.pinduoduo.com/xxx`），
       ❌ 不支持手工拼接的 `goods.html?goods_id={纯数字ID}`（会报 `40009`）。

六、【功能 5：推手授权备案查询与生成 (`member.authority.query` & `rp.prom.url.generate[10]`)】
  - ✅ `pdd.ddk.member.authority.query`：不传 `custom_parameters` 时查询站长 `pid` 备案状态（`bind=1`）；
    传入 `custom_parameters={"uid": uid}` 时查询该推手独立备案状态。
  - ✅ `pdd.ddk.rp.prom.url.generate` (`channel_type=10`, `p_id_list=[pid]`, `custom_parameters={"uid": uid}`)：
    生成该推手专属 H5/小程序授权备案链接。

七、【功能 6：增量订单拉取 (`pdd.ddk.order.list.increment.get`)】
  - ✅ 可用：入参为秒级时间戳 `start_update_time` 与 `end_update_time`（建议窗口 `<= 3600s`），金额单位均为“分”。

八、【功能 7：万能转链与详情反查 (`convert_and_resolve_universal_input` 第一性原理实现：零模糊搜索兜底)】
  - ✅ 核心原则：仅对包含明确推广短链 (`p.pinduoduo.com`)、带 `goods_sign` 的长链 (`yangkeduo.com`) 或裸加密 `goods_sign` 的输入进行确定性解析转链，
    严禁在无链接/无 `goods_sign` 时提取文本标题调用 `pdd.ddk.goods.search` 模糊搜索取首条商品顶替（杜绝转错商品）。
  - ✅ 可用用户自带输入格式（均支持反查出权威 `goods_sign`、商品详情及生成三层最优百亿补贴/领券推广短链，共 8 类全覆盖）：
    1. ✅ 裸加密 `goods_sign`（如 `E932Ts1b...` 或 `Y932Ts1b...`）
    2. ✅ 策略 1 超级红包/百亿补贴推广短链：`https://p.pinduoduo.com/xxx`（含该短链的混合分享文案）
    3. ✅ 策略 1 超级红包/百亿补贴长链：`https://mobile.yangkeduo.com/duo_red_packet.html?...`
    4. ✅ 策略 2 常规单品推广短链：`https://p.pinduoduo.com/xxx`
    5. ✅ 策略 2 常规单品二合一长链：`https://mobile.yangkeduo.com/duo_coupon_landing.html?...&goods_sign=E9...`
    6. ✅ 策略 3 招商推广短链：`pdd.ddk.goods.zs.unit.url.gen` 生成的 `https://p.pinduoduo.com/xxx`
    7. ✅ 本平台生成的完整推广口令文案 (`command_text`，多行含 `【拼多多】`、价格与 `p.pinduoduo.com` 链接)
    8. ✅ 社群混排长促销文案（如 `★拼多多百亿补贴★...[下单链接：https://p.pinduoduo.com/xxx]返红包...`，自动剥离 `]` 及中文后缀提取纯净短链）
       - 短链解包路径 A：先用站长已备案 `pid` 调 `pdd.ddk.goods.zs.unit.url.gen` 解包，从返回的 `url` / `mobile_url` / `multi_group_url` (`duo_coupon_landing.html?...&goods_sign=E9...`) 中直接正则提取 `goods_sign`；
       - 短链解包路径 B：直接将纯净 `https://p.pinduoduo.com/xxx` 短链本身作为 `keyword` 传给 `pdd.ddk.goods.search` (`page_size=10`)，精准反查唯一 1 条商品及 `goods_sign`。
  - ❌ 不可用输入：仅包含纯数字 `goods_id` 的普通链接 `https://mobile.yangkeduo.com/goods.html?goods_id=123456` 或无链接/无 `goods_sign` 的纯文本标题（直接拦截报错，绝不模糊搜索兜底）。

九、【三平台统一商品唯一标识 (`product_id` / `goods_id`) 与动态调用令牌 (`goods_sign`) 职责解耦基准】
  - 拼多多天然区分了 **稳定商品主键 (`goods_id` / `product_id`)** 与 **动态加密调用凭证 (`goods_sign`)**，并作为全站三平台（拼多多、淘宝、京东）数据模型的第一性原理基准：
    1. **`goods_sign`（动态上游调用令牌）**：如 `E932Ts1bVtZgMvVTwfvBVEJF7LgQI9Rz_JQwP5lJQN3` 或 `Y932Ts1b...`，每次列表/搜索请求其前缀可能随上下文变化，但却是调用 `pdd.ddk.goods.detail` 与三层推广转链接口的必传凭证（缺省必报 `40009`）。
    2. **`goods_id` / `product_id`（恒定商品唯一标识）**：专职用于 MongoDB `products` 集合 `(platform, product_id)` 唯一索引去重及前端卡片列表去重。当推荐流某批次仅返回 `goods_sign` 时，仓储层 `ProductRepository._identity` 先以 `goods_sign` 占位，一旦详情或搜索返回真实 `goods_id`，立即通过 `$or: [{"product_id": goods_id}, {"goods_sign": goods_sign}]` 原地升级同一文档的 `product_id`，绝不分裂出重复文档。
    3. **横向对齐淘宝与京东**：
       - 淘宝 (`test_tb_api.py`)：从两段式加密串 `<26位动态盐值前缀>-<19~21位稳定密文后缀>` 中提取 `-` 之后的稳定密文后缀作为 `goods_id` (`product_id`)，完整加密串保留在 `goods_sign`；
       - 京东 (`test_jd_api.py`)：从 100% 必返的京东官方 CDN 主图 URL (`360buyimg.com/.../jfs/.../<parent_hash>/<file_stem>.jpg`) 中提取 `<parent_hash>_<file_stem>` 作为跨接口恒定的 `goods_id` (`product_id`)，完整 50 位加密 `itemId` 保留在 `goods_sign`。
================================================================================
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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


def _build_custom_params(uid: Optional[str] = None) -> Optional[str]:
    """
    构建拼多多多级归因 custom_parameters JSON 字符串：
    - 【实证铁律】当 uid 为 None 或空字符串时，必须返回 None（即不传 custom_parameters 字段，走已备案的站长 PID 级）；
      严禁伪造未备案的 '{"uid":"default_user"}'，否则会导致搜索与转链全部触发 60001 未备案错误！
    """
    safe_uid = str(uid or "").strip()
    if not safe_uid:
        return None
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
    """
    拼多多全网关键词/短链/标题搜索 (pdd.ddk.goods.search)：
    - 【实证保护 1】上游网关强制要求 page_size 取值范围为 [10, 100]，小于 10 必报 `pageSize的取值范围是10-100！`。
    - 【实证保护 2】若传入了未备案推手 uid，拼多多网关会返回 sub_code=60001 (未传入已授权备案过的相关参数)；
      此时自动移除 custom_parameters 回退为已备案的站长 PID 级检索，确保任何用户均可正常搜索商品。
    """
    req_page_size = max(10, min(100, int(page_size or 20)))
    biz_params: Dict[str, Any] = {
        "keyword": keyword,
        "page": page,
        "page_size": req_page_size,
        "sort_type": sort_type,
        "with_coupon": with_coupon,
        "pid": pid or PDD_PID,
        "custom_parameters": _build_custom_params(uid),
    }
    res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", biz_params)
    if res and "error_response" in res and biz_params.get("custom_parameters"):
        err = res.get("error_response", {})
        if str(err.get("sub_code")) == "60001" or "授权备案" in str(err.get("sub_msg", "")):
            biz_params.pop("custom_parameters", None)
            res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", biz_params)

    if not res or "error_response" in res:
        err = (res or {}).get("error_response", {})
        return {
            "status": "error",
            "error_code": err.get("error_code"),
            "sub_code": err.get("sub_code"),
            "error_msg": err.get("sub_msg") or err.get("error_msg", "拼多多商品搜索失败"),
        }

    body = res.get("goods_search_response", {})
    raw_list = body.get("goods_list") or []
    cleaned_list = [clean_pdd_goods_item(item) for item in raw_list][: int(page_size or 20)]
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
    """当仅持有纯数字 goods_id 时，借助 pdd.ddk.goods.zs.unit.url.gen 短链与搜索(page_size=10)反查 goods_sign"""
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
            s_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {
                "keyword": short_url,
                "pid": use_pid,
                "page": 1,
                "page_size": 10,
            })
            for g in (s_res or {}).get("goods_search_response", {}).get("goods_list") or []:
                if g.get("goods_sign"):
                    return str(g["goods_sign"])

    s_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", {
        "keyword": str(goods_id),
        "pid": use_pid,
        "page": 1,
        "page_size": 10,
    })
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

    # 【对齐 PddAdapter.resolve_product_ref】：若调用方把非纯数字的加密串传在了 goods_id 参数上，自动归位为 goods_sign
    if not goods_sign and goods_id is not None and not str(goods_id).strip().isdigit():
        goods_sign = str(goods_id).strip()
        goods_id = None

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
    prefer_subsidy: bool = True,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """
    三层智能降级转链：
    1. 优先尝试官方超级红包/百亿补贴营销频道 (pdd.ddk.rp.prom.url.generate, channel_type=23, diy_sp_red_packet_param)
    2. 常规单品推广转链 (pdd.ddk.goods.promotion.url.generate)
    3. 招商长短链兜底转链 (pdd.ddk.goods.zs.unit.url.gen)
    - 若传入的 uid 尚未在拼多多完成备案 (sub_code=60001)，明确返回 need_auth=True 提示前端引导授权。
    """
    use_pid = pid or PDD_PID
    custom_params = _build_custom_params(uid)
    hit_unauth = False
    last_err_msg = ""

    def _record_err(resp: Optional[Dict[str, Any]]) -> None:
        nonlocal hit_unauth, last_err_msg
        if resp and "error_response" in resp:
            err = resp["error_response"]
            if str(err.get("sub_code")) == "60001" or "授权备案" in str(err.get("sub_msg", "")):
                hit_unauth = True
            last_err_msg = err.get("sub_msg") or err.get("error_msg") or last_err_msg

    # 策略 1：官方超级红包/百亿补贴频道转链 (channel_type=23)
    if prefer_subsidy and goods_sign:
        rp_params = {
            "channel_type": 23,
            "p_id_list": [use_pid],
            "custom_parameters": custom_params,
            "generate_short_url": True,
            "generate_we_app": True,
            "diy_sp_red_packet_param": {"goods_sign": goods_sign},
        }
        rp_res = call_pdd_api(client_id, client_secret, "pdd.ddk.rp.prom.url.generate", rp_params)
        _record_err(rp_res)
        url_list = (rp_res or {}).get("rp_promotion_url_generate_response", {}).get("url_list") or []
        if url_list:
            first = url_list[0]
            link = first.get("mobile_short_url") or first.get("short_url") or first.get("mobile_url") or first.get("url")
            if link:
                return {
                    "status": "success",
                    "link_type": "官方超级红包/百亿补贴链",
                    "final_link": link,
                    "short_url": first.get("short_url", link),
                    "url": first.get("url", link),
                    "we_app_info": first.get("we_app_info", {}),
                }

    # 策略 2：常规单品推广转链
    if goods_sign and not hit_unauth:
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
        _record_err(gen_res)
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
    if target_source_url and not hit_unauth:
        zs_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", {
            "source_url": target_source_url,
            "pid": use_pid,
            "custom_parameters": custom_params,
        })
        _record_err(zs_res)
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

    if hit_unauth:
        return {
            "status": "error",
            "need_auth": True,
            "error_code": "60001",
            "error_msg": f"当前推手 uid={uid} 尚未完成拼多多备案授权 (60001): {last_err_msg}",
        }
    return {"status": "error", "error_msg": last_err_msg or "拼多多三层推广转链均未获取到有效链接"}


# ==============================================================================
# 核心功能 5：推手授权备案查询与生成 (member.authority.query & rp.prom[10])
# ==============================================================================
def check_pdd_member_authority(
    uid: Optional[str] = None,
    pid: Optional[str] = None,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """
    查询是否已完成拼多多备案授权 (pdd.ddk.member.authority.query)：
    - uid=None 时查询站长 PID 本身的备案状态 (bind=1)；
    - uid="xxx" 时查询该推手 custom_parameters={"uid":"xxx"} 的独立备案状态。
    """
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
    uid: str = "u_baseline_test",
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
# 核心功能 7：万能转链与详情反查 (支持 p.pinduoduo.com 短链 / 长链 / goods_sign，零模糊搜索兜底)
# ==============================================================================
def convert_and_resolve_universal_input(
    raw_text: str,
    uid: Optional[str] = None,
    pid: Optional[str] = None,
    prefer_subsidy: bool = True,
    client_id: str = PDD_CLIENT_ID,
    client_secret: str = PDD_CLIENT_SECRET,
) -> Dict[str, Any]:
    """
    拼多多万能转链与详情反查基线函数（第一性原理：仅解析确定性短链/长链/goods_sign，严禁使用标题模糊搜索兜底顶替商品）：
    支持输入类型：
    1. 拼多多推广短链：https://p.pinduoduo.com/xxx (或包含该短链的混合分享文案)
    2. 拼多多二合一/落地长链：https://mobile.yangkeduo.com/duo_coupon_landing.html?...&goods_sign=E9...
    3. 裸加密 goods_sign (如 E932Ts1b... / Y932Ts1b...)
    4. 社群混排长促销文案（如 `★拼多多百亿补贴★...[下单链接：https://p.pinduoduo.com/xxx]返红包...`）
    """
    text = str(raw_text or "").strip()
    if not text:
        return {"status": "error", "error_msg": "输入内容不能为空"}

    use_pid = pid or PDD_PID
    input_type = "text_or_title"
    extracted_sign: Optional[str] = None
    extracted_id: Optional[str] = None
    extracted_url: Optional[str] = None
    product_detail: Optional[Dict[str, Any]] = None

    # 1. 检查是否直接包含 goods_sign 参数或裸 goods_sign
    sign_param_match = re.search(r"goods_sign=([A-Za-z0-9_-]{20,})", text)
    if sign_param_match:
        extracted_sign = sign_param_match.group(1)
        input_type = "long_url_with_sign"
    elif re.match(r"^[E|Y|c][A-Za-z0-9_-]{25,}$", text):
        extracted_sign = text
        input_type = "encrypted_goods_sign"

    # 2. 提取纯净 ASCII URL (p.pinduoduo.com 或 yangkeduo.com)，防止紧贴 `]` 或中文尾缀污染 URL
    url_match = re.search(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", text)
    if url_match:
        extracted_url = url_match.group(0).rstrip(".,!?)];'\"")
        if "p.pinduoduo.com" in extracted_url:
            input_type = "pdd_short_url"
        elif not extracted_sign:
            try:
                qs = parse_qs(urlparse(extracted_url).query)
                if qs.get("goods_sign"):
                    extracted_sign = qs["goods_sign"][0]
                    input_type = "long_url_with_sign"
                if qs.get("goods_id"):
                    extracted_id = qs["goods_id"][0]
            except Exception:
                pass

    if not extracted_sign and extracted_url and "goods.html" in extracted_url and "p.pinduoduo.com" not in extracted_url:
        return {
            "status": "error",
            "input_type": input_type,
            "error_code": "UNSUPPORTED_PDD_ID_ONLY_URL",
            "error_msg": "拼多多官方接口 (40009) 已禁止仅含纯数字 goods_id 的普通链接查询，请粘贴 p.pinduoduo.com 推广短链或含 goods_sign 的长链",
        }

    # 3. 若是 p.pinduoduo.com 短链或未知 goods_sign 的长链，先用站长已备案 PID 调 zs.unit.url.gen 解包出 goods_sign
    if not extracted_sign and extracted_url:
        zs_probe = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.zs.unit.url.gen", {
            "source_url": extracted_url,
            "pid": use_pid,
        })
        zs_body = (zs_probe or {}).get("goods_zs_unit_generate_response") or {}
        unpacked_url = zs_body.get("url") or zs_body.get("mobile_url") or zs_body.get("multi_group_url") or ""
        if unpacked_url:
            try:
                qs = parse_qs(urlparse(unpacked_url).query)
                if qs.get("goods_sign"):
                    extracted_sign = qs["goods_sign"][0]
                if qs.get("goods_id"):
                    extracted_id = qs["goods_id"][0]
            except Exception:
                pass
            if not extracted_sign:
                m_sign = re.search(r"goods_sign=([A-Za-z0-9_-]{20,})", unpacked_url)
                if m_sign:
                    extracted_sign = m_sign.group(1)

    # 4. 若仍未拿到 goods_sign，仅当存在 p.pinduoduo.com 短链时将短链本身传给 pdd.ddk.goods.search 精准反查（严禁搜文本标题！）
    if not extracted_sign and extracted_url and "p.pinduoduo.com" in extracted_url:
        search_res = search_pdd_goods(
            extracted_url, page=1, page_size=10, uid=None, pid=use_pid, client_id=client_id, client_secret=client_secret
        )
        if search_res["status"] == "success" and search_res.get("data"):
            first_hit = search_res["data"][0]
            extracted_sign = first_hit.get("goods_sign")
            extracted_id = first_hit.get("goods_id") or extracted_id
            product_detail = first_hit

    if not extracted_sign and not (extracted_url and "p.pinduoduo.com" in extracted_url):
        return {
            "status": "error",
            "input_type": input_type,
            "error_code": "UNSUPPORTED_PDD_INPUT",
            "error_msg": "未能从输入内容中解析出有效的拼多多推广链接或商品标识 (goods_sign)，请提供 p.pinduoduo.com 推广短链或含 goods_sign 的链接",
        }

    # 5. 获取权威商品详情 (pdd.ddk.goods.detail)
    if extracted_sign:
        detail_res = get_pdd_goods_detail(
            goods_sign=extracted_sign,
            goods_id=extracted_id,
            pid=use_pid,
            client_id=client_id,
            client_secret=client_secret,
        )
        if detail_res["status"] == "success" and detail_res.get("data"):
            product_detail = detail_res["data"]
            extracted_id = product_detail.get("goods_id") or extracted_id

    # 6. 执行三层智能推广转链 (支持推手归因 uid，若推手未备案则返回 need_auth=True 并附带站长级预览链)
    conv_res = convert_pdd_promotion_url(
        goods_sign=extracted_sign,
        goods_id=extracted_id,
        source_url=extracted_url,
        uid=uid,
        pid=use_pid,
        prefer_subsidy=prefer_subsidy,
        client_id=client_id,
        client_secret=client_secret,
    )
    if conv_res["status"] != "success":
        return {
            "status": "error",
            "input_type": input_type,
            "need_auth": conv_res.get("need_auth", False),
            "error_code": conv_res.get("error_code"),
            "error_msg": conv_res.get("error_msg", "拼多多万能转链失败"),
            "product": product_detail,
        }

    title = (product_detail or {}).get("name") or "拼多多精选好物"
    price = (product_detail or {}).get("price") or 0
    final_link = conv_res.get("final_link") or conv_res.get("short_url") or ""
    share_command = f"【拼多多】{title}\n💰到手价：¥{price}\n👉抢购链接：{final_link}"

    return {
        "status": "success",
        "platform": "pdd",
        "input_type": input_type,
        "promotion": {
            "final_link": final_link,
            "short_url": conv_res.get("short_url", final_link),
            "long_url": conv_res.get("url", final_link),
            "link_type": conv_res.get("link_type", ""),
            "we_app_info": conv_res.get("we_app_info", {}),
            "command": share_command,
        },
        "product": product_detail,
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

    unfiled_test_uid = "u_baseline_test"

    # 1. 验证热销榜单 (channel_type=5 实时热销榜 & channel_type=1 今日爆款榜)
    print("\n[1/7] 验证拼多多榜单拉取 (channel_type=5 实时热销榜)...")
    rec_res = get_pdd_recommend_goods(channel_type=5, limit=3, uid=None)
    sample_sign = None
    sample_id = None
    sample_name = ""
    if rec_res["status"] == "success" and rec_res["data"]:
        first = rec_res["data"][0]
        sample_sign = first.get("goods_sign")
        sample_id = first.get("goods_id")
        sample_name = first.get("name", "")
        print(f" -> 成功拉取 {len(rec_res['data'])} 条推荐商品，首条: {first['name'][:25]} (¥{first['price']})")
    else:
        print(f" -> 推荐拉取失败: {rec_res.get('error_msg')}")

    # 2. 验证关键词搜索 (同时验证传入未备案 uid 自动降级回退站长级检索)
    print("\n[2/7] 验证拼多多关键词搜索 (pdd.ddk.goods.search, 含未备案 uid 自动降级保护)...")
    search_res = search_pdd_goods("抽纸", page=1, page_size=3, uid=unfiled_test_uid)
    if search_res["status"] == "success" and search_res["data"]:
        first = search_res["data"][0]
        sample_sign = sample_sign or first.get("goods_sign")
        sample_id = sample_id or first.get("goods_id")
        sample_name = sample_name or first.get("name", "")
        print(f" -> 搜索成功，共命中 {search_res['total_count']} 条，首条: {first['name'][:25]} (¥{first['price']})")
    else:
        print(f" -> 搜索失败: {search_res.get('error_msg')}")

    # 3. 验证商品详情 (✅ goods_sign 权威主路径 vs ❌ 纯数字 goods_id 触发 40009 拦截验证)
    print("\n[3/7] 验证拼多多商品详情 (pdd.ddk.goods.detail)...")
    if sample_sign or sample_id:
        detail_res = get_pdd_goods_detail(goods_sign=sample_sign, goods_id=sample_id, pid=PDD_PID)
        if detail_res["status"] == "success":
            d = detail_res["data"]
            sample_name = d.get("name") or sample_name
            print(f" -> [✅ goods_sign 主路径] 详情成功: {d['name'][:25]} | 轮播图: {len(d.get('carousel_images', []))}张")
        else:
            print(f" -> [goods_sign 主路径] 详情失败: {detail_res.get('error_msg')}")
    if sample_id:
        id_only_res = get_pdd_goods_detail(goods_sign=None, goods_id=sample_id, pid=PDD_PID)
        print(f" -> [❌ 仅传纯数字 goods_id 预期触发 40009 拦截]: status={id_only_res['status']}, sub_code={id_only_res.get('sub_code')} ({id_only_res.get('sub_msg', '')[:32]}...)")

    # 4. 验证三层智能转链 (站长已备案级 uid=None vs 未备案推手 uid 触发 60001 拦截)
    print("\n[4/7] 验证拼多多三层智能兜底转链...")
    conv_owner: Dict[str, Any] = {}
    conv_std: Dict[str, Any] = {}
    if sample_sign or sample_id:
        conv_owner = convert_pdd_promotion_url(goods_sign=sample_sign, goods_id=sample_id, uid=None, prefer_subsidy=True)
        print(f" -> [A. 站长已备案级 (uid=None, 超级红包轨)] status={conv_owner['status']}, type={conv_owner.get('link_type')}, link={conv_owner.get('final_link')}")
        conv_std = convert_pdd_promotion_url(goods_sign=sample_sign, goods_id=sample_id, uid=None, prefer_subsidy=False)
        print(f" -> [B. 站长已备案级 (uid=None, 常规单品轨)] status={conv_std['status']}, type={conv_std.get('link_type')}, link={conv_std.get('final_link')}")
        conv_unauth = convert_pdd_promotion_url(goods_sign=sample_sign, goods_id=sample_id, uid=unfiled_test_uid)
        print(f" -> [C. 未备案推手级 (uid={unfiled_test_uid})] 预期触发 60001 授权拦截: need_auth={conv_unauth.get('need_auth')}, code={conv_unauth.get('error_code')}")

    # 5. 验证推手备案查询与授权链接生成
    print("\n[5/7] 验证拼多多备案查询 (站长级 vs 未备案推手级) 与授权链接生成...")
    owner_auth = check_pdd_member_authority(uid=None)
    print(f" -> [站长 PID 级备案状态 (uid=None)]: {owner_auth}")
    user_auth = check_pdd_member_authority(uid=unfiled_test_uid)
    print(f" -> [测试推手备案状态 (uid={unfiled_test_uid})]: {user_auth}")
    if not user_auth.get("is_authorized"):
        url_res = generate_pdd_authority_url(uid=unfiled_test_uid)
        print(f" -> [生成推手 H5 备案授权链接]: status={url_res['status']}, auth_url={url_res.get('auth_url')}")

    # 6. 验证增量订单查询
    print("\n[6/7] 验证拼多多增量订单拉取 (最近 1 小时)...")
    now_ts = int(time.time())
    ord_res = fetch_pdd_increment_orders(now_ts - 3600, now_ts)
    print(f" -> 订单查询结果: status={ord_res['status']}, 数量={len(ord_res.get('order_list', []))}")

    # 7. 验证万能转链与详情反查全矩阵 (8 种真实格式自生自测 + 纯数字 goods_id 拦截验证)
    print("\n[7/7] 验证拼多多万能转链与详情反查全矩阵 (8 种真实格式自生自测，零模糊搜索兜底)...")
    if sample_sign and conv_owner.get("status") == "success" and conv_std.get("status") == "success":
        rp_short = conv_owner.get("short_url") or conv_owner.get("final_link") or ""
        rp_long = conv_owner.get("url") or ""
        std_short = conv_std.get("short_url") or conv_std.get("final_link") or ""
        std_long = conv_std.get("url") or ""
        zs_gen = convert_pdd_promotion_url(goods_sign=None, source_url=std_short, uid=None, prefer_subsidy=False)
        zs_short = zs_gen.get("short_url") or zs_gen.get("final_link") or std_short
        platform_cmd = f"【拼多多】{sample_name}\n💰到手价：¥19.9\n👉抢购链接：{std_short}"
        mixed_pdd_copy = f"★拼多多百亿补贴★ {sample_name} 售价：19.9元[下单链接：{std_short}]返2元红包[红包] 手慢无！"

        pdd_cases = [
            ("7a. 裸加密 goods_sign", sample_sign),
            ("7b. 策略1百亿补贴短链 (p.pinduoduo.com)", f"拼多多百亿补贴好物推荐：{rp_short}"),
            ("7c. 策略1百亿补贴长链 (duo_red_packet)", rp_long),
            ("7d. 策略2常规单品短链 (p.pinduoduo.com)", std_short),
            ("7e. 策略2二合一长链 (duo_coupon_landing)", std_long),
            ("7f. 策略3招商短链 (zs.unit.url.gen)", zs_short),
            ("7g. 平台自生成多行推广文案", platform_cmd),
            ("7h. 社群混排长文案夹带[下单链接：p.pinduoduo.com]", mixed_pdd_copy),
        ]
        for label, text_input in pdd_cases:
            u_res = convert_and_resolve_universal_input(text_input, uid=None)
            prod = u_res.get("product") or {}
            promo = u_res.get("promotion") or {}
            print(
                f" -> [{label}] status={u_res['status']} | type={u_res.get('input_type')} "
                f"| 反查商品={prod.get('name', '')[:18]} (¥{prod.get('price')}) | 新短链={promo.get('final_link')}"
            )

        u_bad = convert_and_resolve_universal_input("https://mobile.yangkeduo.com/goods.html?goods_id=1016415496258", uid=None)
        print(
            f" -> [7i. 纯数字直链拦截验证 (零兜底)] status={u_bad['status']} | code={u_bad.get('error_code')} "
            f"| msg={u_bad.get('error_msg', '')[:38]}..."
        )


if __name__ == "__main__":
    main()


