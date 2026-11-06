# -*- coding: utf-8 -*-
"""
京东联盟推手平台 API 自检与验证程序 (极简精炼版 · 支持多 ZTK_APPKEY_LIST 自动轮询与故障切换)
基于折淘客 (折京客) 及京东官方网关协议，作为全项目京东功能的唯一基准实现。

================================================================================
【京东联盟 API 全链路实证基准规范：哪些可用 (✅) vs 哪些不可用 (❌)】
================================================================================
所有后续业务代码（jd_client.py / jd_adapter.py）必须严格以本文件实测结论为准，严禁发散猜测：

一、【功能 1：实时榜单选品 (`open_jd_union_open_goods_rank_query.ashx`)】
  - ✅ 可用：`sortType=1` (2小时实时热推榜)、`sortType=3` (24小时今日热销榜)，`rankId=200000`，
    返回京东联盟加密 `itemId`（如 `2JQkZkZ2nGOIkKHE8K75IkKfe6mXtQ_3SD7M7MGcugkXyuI4w`）。
  - ✅ 可用商品链接拼接格式：`https://jingfen.jd.com/detail/{itemId}.html`
    （实测既可在浏览器正常访问，也可直接传入转链接口完成转链）。
  - ❌ 严禁拼接格式：`https://item.jd.com/{itemId}.html`
    （加密 `itemId` 拼接到 `item.jd.com` 后浏览器无法打开 [404]，且传入转链接口必报 `2001104 商品不在推广中`）。
  - ❌ 严禁使用个人 `jd_app_key` 直连京东官方榜单：京东生成的加密 `itemId` 与调用方 `app_key` 强绑定，
    用个人 `jd_app_key` 拿到的榜单 `itemId` 传给折淘客转链接口会有 30%~40% 报 `2001104` 或 `2001917`。

二、【功能 2：全网关键词搜索 (`api_quanwang.ashx`)】
  - ✅ 可用：`type=2`, `youquan=1`, `sort="new"`，返回加密 `tao_id` 及 `item_url` (`https://jingfen.jd.com/detail/{tao_id}.html`)。
  - ❌ 不可用搜索词长度：搜索词 `q` 长度 `< 4` 个字符（如 `"可乐"`、`"纸巾"`）会被折淘客上游直接报错拒绝。
  - ✅ 必选补齐规则：当 `len(query) < 4` 时必须倍增补齐（`while len(query) < 4: query += query`，如 `"可乐"` -> `"可乐可乐"`）。

三、【功能 3：单品详情查询 (`api_detail.ashx`) 与 `301: 对应宝贝已下架` 二义性裁决规范】
  - ✅ 可用入参：传入加密 `tao_id` / `itemId`（无论来自关键词搜索还是实时榜单），`type=1`，
    均可返回完整详情、轮播图 `small_images`（`|` 分隔）与权威 `https://jingfen.jd.com/detail/{id}.html` 链接。
  - ❌ 不可用入参：纯数字旧版 SKU ID（如 `"100012043978"`），接口直接报错 `{"status": 301, "content": "对应宝贝已下架"}`。
  - ⚠️ 【核心二义性问题与完备裁决方案】：
    当 `api_detail.ashx` 返回 `{"status": 301, "content": "对应宝贝已下架"}` 时存在二义性——
    既可能是单 `ZTK_APPKEY` 触发折淘客频控伪装报错，也可能是商品真正取消了京东联盟推广（例如 `oxXpYeb9FgS3eszB2h2f3eszB2h2fX_3TYrAutPKopeY0fpzO`）。
    必须执行以下两步交叉裁决（已在 `jd_client.py` + `jd_adapter.py` + `product_service.py` 闭环落地）：
    1. **多 Key 轮询防误判**：先在 `ZTK_APPKEY_LIST` 所有 Key 间自动故障切换；若仅单 Key 限流，备用 Key 直接返回 `status=200`。
    2. **官方转链探针终局裁决**：若全部 Key 均返回 `301: 对应宝贝已下架`，调用 `open_jing_union_open_promotion_byunionid_get.ashx`
       探测 `https://jingfen.jd.com/detail/{id}.html`：
       - 若转链返回 `code=200`（成功生成短链/口令）-> 证明商品仍在推广，详情 `301` 纯属频控（返回 `RATE_LIMIT`，保留并启用本地缓存兜底）；
       - 若转链返回京东联盟官方内层码 `2001104`（`商品不在推广中`）或 `2001920` -> 100% 确诊商品已取消推广（返回 `PRODUCT_OFF_PROMOTION`，
         立即从 `DETAIL_CACHE`/`MATERIAL_CACHE`/`RECOMMEND_CACHE`/`SEARCH_CACHE` 及 MongoDB `products` 集合物理驱逐，并加入 `OFF_PROMOTION_CACHE` 防复活黑名单）。
  - ❌ 严禁在转链时无条件预调 `api_detail.ashx`：`api_detail.ashx` 与 `api_quanwang.ashx` 共享单 `ZTK_APPKEY` 的 `1000` 频控池，
    而加密 ID 本身即可直接一步转链，无需浪费详情配额。

四、【功能 4：统一推广转链 (`open_jing_union_open_promotion_byunionid_get.ashx`)】
  - ✅ 可用 `materialId` 格式（以下 3 种格式实测均 100% 一步转链成功）：
    1. 直接传入裸加密 ID：`materialId = "2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w"`
    2. 传入京粉详情页链接：`materialId = "https://jingfen.jd.com/detail/2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w.html"`
    3. 传入联盟推广短链接：`materialId = "https://u.jd.com/xxx"`
  - ❌ 不可用 `materialId` 格式（严禁使用）：
    1. ❌ `https://item.jd.com/{encrypted_id}.html`：无法访问且无法转链，必报 `2001104 商品不在推广中`。
    2. ❌ 纯数字 SKU ID（如 `100012043978` 或 `https://item.jd.com/100012043978.html` 或 `https://jingfen.jd.com/detail/100012043978.html`）：
       接口直接报错 `{"status": 301, "content": "很抱歉！不支持数字id转链！"}`。
  - ✅ 可用 `positionId` (`promoter_id`)：传入任意纯数字（如站长默认 `3110104129` 或推手数字 ID `8899`），
    免去后台建位，成交后在订单 `positionId` 字段原封不动透出。

五、【功能 5：增量订单拉取 (`open_jing_union_openz_order_row_query.ashx` 二跳官方网关)】
  - ✅ 可用：折淘客使用 `jd_app_key` + `jd_app_secret` 签名并返回 `https://api.jd.com/routerjson?...`，
    本地直接请求京东官方网关获取真实订单行。
  - ⚠️ 协议约束：查询时间窗跨度不得超过 1 小时 (`<= 3600s`)，推荐 `type=3` (按更新时间查询)。

六、【频控与高可用唯一有效方案 (`ZTK_APPKEY_LIST` 自动故障切换)】
  - ❌ 已证伪：同一 `appkey` 下用 `open_jing_union_open_goods_query.ashx` 给 `api_quanwang.ashx` / `api_detail.ashx` 兜底（共享同一 `1000` 限流计数器，救活率 0% 且双倍消耗配额）。
  - ✅ 唯一有效：折淘客频控绑定在单个 `ZTK_APPKEY` 上，不限制 IP 与 `JD_UNION_ID`。使用 `ZTK_APPKEY_LIST` 多 Key 轮询与自动故障切换，救活率 100%。
================================================================================
"""

import os
import sys
import json
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, List, Optional, Sequence, Union
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 1. 核心有效配置 ====================
# 折淘客 (折京客) 凭据池：支持配置多个绑定同一京东授权的 appkey，实现额度叠加与限流自动切换
DEFAULT_ZTK_APPKEY_LIST: List[str] = [
    "41ce110973f744118c1227d5f7f72b65",
    "fe60ace35b00426b89a16a2baea50618",
]


def parse_appkey_list(raw: Union[str, Sequence[str], None]) -> List[str]:
    """将字符串（支持逗号分隔）或字符串序列规范化为去重后的非空 appkey 列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        candidates = []
        for item in raw:
            if isinstance(item, str):
                candidates.extend(item.split(","))
    else:
        return []
    seen = []
    for key in candidates:
        clean = key.strip()
        if clean and clean not in seen:
            seen.append(clean)
    return seen


_env_keys = os.environ.get("ZTK_APPKEY_LIST") or os.environ.get("ZTK_APPKEY")
ZTK_APPKEY_LIST: List[str] = parse_appkey_list(_env_keys) or list(DEFAULT_ZTK_APPKEY_LIST)
# 兼容单 Key 常量引用
ZTK_APPKEY: str = ZTK_APPKEY_LIST[0]

# 京东联盟账号 (用于折淘客定位归属账号) 与 站长默认推广位
JD_UNION_ID = os.environ.get("JD_UNION_ID", "2035679405")           # 联盟纯数字ID
DEFAULT_POSITION_ID = os.environ.get("JD_POSITION_ID", "3110104129") # 站长自营默认推广位ID

# 京东开放平台应用凭据 (仅在订单行接口计算签名时使用)
JD_APP_KEY = os.environ.get("JD_APP_KEY", "45cb55a17ecbad6e91b004c0c34babe2")
JD_APP_SECRET = os.environ.get("JD_APP_SECRET", "8d501939a5084e62aeed3377f9d6e7ac")

# 折淘客 API 网关
ZTK_API_BASE = "http://api.zhetaoke.com:20000/api"

# 当前健康可用的 appkey 游标索引（当某个 Key 触发限流切换到下一个 Key 成功后，自动更新游标避免重复命中限流 Key）
_CURRENT_KEY_INDEX: int = 0

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


def request_ztk_with_failover(
    endpoint: str,
    params: Dict[str, Any],
    is_success_fn: Callable[[Dict[str, Any]], bool],
    appkey_list: Optional[Sequence[str]] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """
    基于 ZTK_APPKEY_LIST 的折淘客通用请求与自动故障转移（Failover）器：
    1. 默认从当前健康游标 `_CURRENT_KEY_INDEX` 对应的 appkey 开始请求；
    2. 若当前 appkey 触发折淘客频控限制（如返回 status=301）或网络异常，立即顺延尝试列表中的下一个 appkey；
    3. 一旦某个 appkey 成功通过 `is_success_fn` 校验，立即将 `_CURRENT_KEY_INDEX` 更新为该健康 key 并返回响应。
    """
    global _CURRENT_KEY_INDEX
    keys = parse_appkey_list(appkey_list) if appkey_list is not None else list(ZTK_APPKEY_LIST)
    if not keys:
        raise ValueError("ZTK_APPKEY_LIST 不能为空")

    url = f"{ZTK_API_BASE}/{endpoint.lstrip('/')}"
    total = len(keys)
    start_idx = _CURRENT_KEY_INDEX % total
    last_resp: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None

    for offset in range(total):
        idx = (start_idx + offset) % total
        current_key = keys[idx]
        req_params = {"appkey": current_key, **params}
        try:
            resp = requests.get(url, params=req_params, timeout=timeout).json()
            if isinstance(resp, dict):
                last_resp = resp
                if is_success_fn(resp):
                    _CURRENT_KEY_INDEX = idx
                    return resp
        except Exception as exc:
            last_exc = exc

    if last_resp:
        return last_resp
    if last_exc is not None:
        raise last_exc
    return {}


# ==================== 2. 功能 1: 实时榜单选品 ====================
def get_rank_goods(
    sort_type: int = 1,
    page_size: int = 3,
    appkey_list: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """
    拉取京东实时榜单
    :param sort_type: 1=2小时实时热推榜, 3=24小时今日热销榜
    :param page_size: 拉取条数
    :param appkey_list: 可选折淘客 appkey 列表，默认使用全局 ZTK_APPKEY_LIST
    """
    params = {
        "unionId": str(JD_UNION_ID),
        "sortType": sort_type,
        "rankId": 200000,
        "pageIndex": 1,
        "pageSize": page_size
    }

    def _check_rank(resp: Dict[str, Any]) -> bool:
        raw_res = resp.get("jd_union_open_goods_rank_query_responce", {}).get("queryResult")
        if not raw_res:
            return False
        parsed = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
        return isinstance(parsed, dict) and parsed.get("code") == 200 and isinstance(parsed.get("data"), list)

    try:
        resp = request_ztk_with_failover(
            "open_jd_union_open_goods_rank_query.ashx", params, _check_rank, appkey_list=appkey_list
        )
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
                # 【实证规范】加密 itemId 严禁拼接为 https://item.jd.com/{item_id}.html (无法访问且转链必报 2001104)；
                # 必须使用 https://jingfen.jd.com/detail/{item_id}.html 格式（可正常访问且支持直接转链）。
                "item_url": f"https://jingfen.jd.com/detail/{item_id}.html" if item_id else ""
            })
        return result
    except Exception as e:
        return [{"error": f"拉取榜单异常: {e}"}]


# ==================== 3. 功能 2: 全网关键词搜索 ====================
def search_goods(
    keyword: str,
    page: int = 1,
    page_size: int = 3,
    appkey_list: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """
    全网搜索京东推广商品（当主 appkey 触发折淘客频控返回 status=301 时，自动切换备用 appkey 重试）
    """
    query = keyword.strip()
    # 【重要协议约束】折淘客全网搜索接口 (api_quanwang.ashx) 强制要求搜索词 q 的长度至少为 4 个字符；
    # 若少于 4 个字符（例如“纸巾”、“可乐”），折淘客上游会直接报错拒绝响应。
    # 因此此处必须对不足 4 个字符的关键词进行倍增补齐（如“纸巾” -> “纸巾纸巾”），切勿删除！
    while len(query) < 4:
        query += query

    params = {
        "unionId": str(JD_UNION_ID),
        "q": query,
        "page": page,
        "page_size": page_size,
        "sort": "new",
        "youquan": 1,
        "type": 2
    }

    def _check_search(resp: Dict[str, Any]) -> bool:
        return str(resp.get("status")) == "200" and isinstance(resp.get("content"), list)

    try:
        resp = request_ztk_with_failover(
            "api_quanwang.ashx", params, _check_search, appkey_list=appkey_list
        )
        if str(resp.get("status")) != "200" or not isinstance(resp.get("content"), list):
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
def get_goods_detail(
    item_id: str,
    appkey_list: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    根据商品 ID / 物料 ID 查询完整商品详情（当主 appkey 触发折淘客频控返回 status=301 时，自动切换备用 appkey 重试）
    """
    params = {
        "unionId": str(JD_UNION_ID),
        "tao_id": item_id,
        "type": 1
    }

    def _check_detail(resp: Dict[str, Any]) -> bool:
        return str(resp.get("status")) == "200" and isinstance(resp.get("content"), list) and bool(resp.get("content"))

    try:
        resp = request_ztk_with_failover(
            "api_detail.ashx", params, _check_detail, appkey_list=appkey_list
        )
        if str(resp.get("status")) != "200" or not resp.get("content"):
            return {"error": f"未查询到详情: {resp}"}

        c = resp["content"][0]
        small_imgs = [img for img in (c.get("small_images") or "").split("|") if img]
        return {
            "item_id": str(c.get("tao_id", "")),
            "item_url": c.get("item_url", ""),
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
    promoter_id: Union[int, str] = DEFAULT_POSITION_ID,
    appkey_list: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    统一推广转链函数 (open_jing_union_open_promotion_byunionid_get.ashx)

    【实证 materialId 入参规范（请严格遵守）】：
    - ✅ 可用格式 1 (裸加密 ID)：直接传入榜单 itemId 或搜索 tao_id，如 "2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w"
    - ✅ 可用格式 2 (京粉详情页)："https://jingfen.jd.com/detail/2JQgZkZ2nGOIkKfYjWWTIkKfYjWWTQ_3SD7M7MGcugkXyuI4w.html"
    - ✅ 可用格式 3 (联盟短链接)："https://u.jd.com/xxx"
    - ❌ 严禁格式 A："https://item.jd.com/{encrypted_id}.html" -> 无法访问且必报 2001104 商品不在推广中
    - ❌ 严禁格式 B：纯数字 SKU ID (如 "100012043978") -> 必报 301 不支持数字id转链

    【核心归因机制与参数说明】：
    1. promoter_id 只要是纯数字即可（例如：推手在业务系统里的用户数字 ID 8899，或默认数字 3110104129）；
    2. 京东联盟完全不需要在后台预先创建推广位，转链传入任意纯数字，订单成交后都会在 positionId 中原封不动透出；
    3. 不再涉及任何 owner 特殊分支逻辑，所有推手与站长统一使用数字 ID 透传与归因。

    :param material_url: 加密商品 ID / 京粉商品链接 (https://jingfen.jd.com/detail/{id}.html) / 联盟短链
    :param promoter_id: 推广标识（必须为纯数字，如 8899 或 "8899"，未传入时默认使用 DEFAULT_POSITION_ID）
    :param appkey_list: 可选折淘客 appkey 列表，默认使用全局 ZTK_APPKEY_LIST
    """
    clean_id = str(promoter_id).strip()
    if not clean_id.isdigit():
        # 若传入非纯数字，回退到默认推广位数字
        clean_id = str(DEFAULT_POSITION_ID)

    params = {
        "unionId": str(JD_UNION_ID),
        "materialId": material_url,
        "chainType": "3",  # 返回长链 + 短链
        "positionId": clean_id
    }

    def _check_promotion(resp: Dict[str, Any]) -> bool:
        resp_obj = resp.get("jd_union_open_promotion_byunionid_get_response")
        if not isinstance(resp_obj, dict):
            return False
        raw_res = resp_obj.get("result", "{}")
        parsed = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
        return isinstance(parsed, dict) and parsed.get("code") == 200

    try:
        resp = request_ztk_with_failover(
            "open_jing_union_open_promotion_byunionid_get.ashx", params, _check_promotion, appkey_list=appkey_list
        )
        resp_obj = resp.get("jd_union_open_promotion_byunionid_get_response", {})
        raw_res = resp_obj.get("result", "{}")
        result_data = json.loads(raw_res) if isinstance(raw_res, str) else raw_res

        if result_data.get("code") != 200:
            return {"error": f"转链未成功: {result_data.get('message') or resp}"}

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
    page_size: int = 50,
    appkey_list: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    拉取京东真实订单行数据并提取核心业务字段（纯粹拉取，不擅自计算提成）
    :param start_time: 开始时间 yyyy-MM-dd HH:mm:ss (跨度不超过1小时)
    :param end_time: 结束时间 yyyy-MM-dd HH:mm:ss
    :param query_type: 1=按订单下单时间, 3=按订单最后更新时间 (推荐)
    :param appkey_list: 可选折淘客 appkey 列表，默认使用全局 ZTK_APPKEY_LIST
    """
    params = {
        "jd_app_key": JD_APP_KEY,
        "jd_app_secret": JD_APP_SECRET,
        "startTime": start_time,
        "endTime": end_time,
        "type": str(query_type),
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "fields": "goodsInfo,categoryInfo"
    }

    def _check_order_relay(resp: Dict[str, Any]) -> bool:
        return str(resp.get("status")) == "200" and isinstance(resp.get("content"), str)

    try:
        # 折淘客中继计算签名并返回官方 URL（支持多 appkey 故障切换）
        resp = request_ztk_with_failover(
            "open_jing_union_openz_order_row_query.ashx", params, _check_order_relay,
            appkey_list=appkey_list, timeout=12
        )
        if str(resp.get("status")) != "200":
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
    print("=" * 85)
    print("🚀 京东联盟 API 基础功能验证程序 (极简精炼版 · 多 ZTK_APPKEY_LIST 自动轮询)")
    print(f"👉 当前联盟ID: {JD_UNION_ID} | 站长默认推广位: {DEFAULT_POSITION_ID} | 可用 Key 数量: {len(ZTK_APPKEY_LIST)}")
    print("=" * 85)

    # 1. 榜单测试 (2小时榜 & 24小时今日热销榜)
    print("\n【1. 实时榜单拉取】")
    b2h = get_rank_goods(sort_type=1, page_size=2)
    print("  * 2小时实时热推榜 TOP 2:")
    for g in b2h:
        print(f"    - [{g.get('rank')}] {g.get('title', '')[:22]}... | 现价: ¥{g.get('price')} | 预估佣金: ¥{g.get('commission_amount')}")

    b24h = get_rank_goods(sort_type=3, page_size=2)
    print("  * 24小时今日热销榜 TOP 2:")
    for g in b24h:
        print(f"    - [{g.get('rank')}] {g.get('title', '')[:22]}... | 现价: ¥{g.get('price')} | 预估佣金: ¥{g.get('commission_amount')}")

    # 2. 关键词搜索测试
    print("\n【2. 全网关键词搜索】(搜索: '可乐')")
    s_items = search_goods("可乐", page=1, page_size=5)
    for idx, it in enumerate(s_items, 1):
        print(f"  * ({idx}) {it.get('title', '')[:22]}... | 券后: ¥{it.get('final_price')} | 佣金率: {it.get('commission_rate')}% (¥{it.get('commission_amount')})")

    sample_url = s_items[0]["item_url"] if s_items else (b2h[0]["item_url"] if b2h and "item_url" in b2h[0] else "https://u.jd.com/egVs7wU")
    sample_sku = s_items[0]["item_id"] if s_items else (b2h[0]["item_id"] if b2h and "item_id" in b2h[0] else "6xXY8Ko5uwFbKV5W")

    # 3. 商品详情展示测试
    print(f"\n【3. 商品详情展示】(SKU: {sample_sku[:20]}...)")
    det = get_goods_detail(sample_sku)
    if "error" not in det:
        print(f"  * 店铺: {det.get('shop_title')} | 自营: {det.get('is_self_operated')} | 券后价: ¥{det.get('final_price')} | 佣金: ¥{det.get('commission_amount')}")
        print(f"  * 轮播图数: {len(det.get('small_images', []))} 张 | 主图: {det.get('pict_url', '')[:60]}...")
    else:
        print(f"  * 详情获取失败: {det.get('error')}")

    # 4. 统一转链逻辑测试 (验证京粉链接转链 + 裸加密 ID 直接转链 + 推手纯数字 ID 归因)
    print(f"\n【4. 统一推广转链测试】")
    link_default = convert_promotion_link(sample_url, promoter_id=DEFAULT_POSITION_ID)
    print(f"  👉 [示例 A: 京粉详情链接转链 ({sample_url[:55]}... | promoter_id={DEFAULT_POSITION_ID})]")
    print(f"     推广数字ID: {link_default.get('promoter_id')}")
    print(f"     短链接: {link_default.get('short_url')}")
    print(f"     京口令: {link_default.get('j_short_command') or link_default.get('j_command')}")

    link_raw_id = convert_promotion_link(sample_sku, promoter_id=8899)
    print(f"\n  👉 [示例 B: 裸加密ID直接转链 + 推手专属纯数字归因 (materialId={sample_sku[:28]}... | promoter_id=8899)]")
    print(f"     推手数字ID: {link_raw_id.get('promoter_id')}")
    print(f"     专属短链接: {link_raw_id.get('short_url')}")
    print(f"     专属精简口令: {link_raw_id.get('j_short_command')}")
    print(f"     专属完整口令: {link_raw_id.get('j_command')}")

    # 5. 增量订单拉取与核心字段打印 (无擅自分成计算)
    print("\n【5. 增量订单拉取与核心字段打印】")
    test_start = "2026-11-06 05:30:00"
    test_end = "2026-11-06 06:30:00"
    print(f"  查询窗口: {test_start} 至 {test_end} (type=1: 按下单时间)")
    order_res = fetch_orders(start_time=test_start, end_time=test_end, query_type=1)

    if order_res.get("success"):
        orders = order_res.get("orders", [])
        print(f"  * 订单接口调用成功，共拉取到 {len(orders)} 笔订单数据:")
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

    print("=" * 85)
    print("自检流程完毕。")
    print("=" * 85)
