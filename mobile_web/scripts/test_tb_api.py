# -*- coding: utf-8 -*-
"""
淘宝客 (阿里妈妈 TOP + 好单库榜单 + 折淘客渠道/订单) 核心能力标准基线脚本 (生产级实证版)
================================================================================
本脚本作为全项目淘宝 API 的唯一权威基线，维护并验证项目用到的全部 7 项核心 API 操作：
1. 获取热销榜单商品 (好单库 https://v2.api.haodanku.com/sales_list [sale_type=1 2h / 2 24h] + 官方批量详情补全)
2. 关键词搜索商品 (taobao.tbk.dg.material.optional.upgrade)
3. 获取商品详情 (taobao.tbk.item.info.upgrade.get)
4. 双轨推广转链与淘口令生成 (折淘客渠道轨 + 官方 uland 精准生链 & tpwd.create 站长轨)
5. 推手渠道备案查询与授权邀请码生成 (官方 TOP 优先 + 折淘客 90 天长效 sid 自动回退)
6. 增量订单拉取与两跳签名网关解析 (折淘客 open_dingdanchaxun2.ashx, 支持 ZTK_APPKEY_LIST 多 Key 故障切换)
7. 万能转链与口令解析反查 (convert_and_resolve_universal_input: 淘口令/手淘分享短链 m.tb.cn/e.tb.cn/s.click/uland/商品链接一步转链+详情提取)

================================================================================
【淘宝客 API 全链路实证基准规范：哪些可用 (✅) vs 哪些不可用 (❌)】
--------------------------------------------------------------------------------
一、 商品 ID 体系与唯一标识 (`goods_id` / `product_id`) vs 动态调用令牌 (`goods_sign`) 解耦铁律
  1. ✅ 加密长串结构实证：新版淘宝联盟 TOP 接口（`material.optional.upgrade`、`item.info.upgrade.get`）与折淘客接口
     全面采用由 `-` 分隔的两段式加密长串 `item_id`（形如 `0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd`）：
     - **前半段（`-` 之前，26 位）**：动态请求上下文与时间戳盐值前缀（每次请求、不同批次或不同接口调用都会重新生成，如 `0AqROdBH7t8b9oJzGJT5n6C2t6` vs `7b4f7m5p92H8b6B69R97Yf6t6`）；
     - **后半段（`-` 之后，19~21 位）**：商品真实 ID 的确定性密文后缀（如 `0v7DKOgI5JqnZd4Nhxd`，同一商品在榜单、搜索、详情、折淘客高佣转链所有接口中 **100% 恒定不变，且零碰撞**）。
  2. ❌ 为什么不能把完整加密串直接作为数据库唯一主键 `product_id`？
     - 若将包含动态前缀的完整 `item_id` 存入 `product_id`，同一商品在每次榜单刷新或搜索时都会因前缀变化绕过 `(platform, product_id)` 唯一索引重复入库（实测曾导致 305 个唯一商品产生 9084 条重复文档）。
     - 同时，不能把前缀剥离后的后缀传给 TOP/折淘客接口（上游接口校验完整 `<prefix>-<suffix>` 签名，仅传后缀会报参数错误）。
  3. ✅ 第一性原理最佳唯一标识方案（`goods_id` 与 `goods_sign` 职责彻底分离）：
     - `goods_id` / `product_id`：若入参回显 `input_item_iid` 为纯数字则取纯数字，否则提取加密串 `-` 之后的**确定性商品密文后缀**（`extract_tb_canonical_goods_id`），专职作为 MongoDB `(platform, product_id)` 唯一索引主键与前端去重键；
     - `goods_sign`：保留完整的 `<动态前缀>-<稳定后缀>` 加密长串，并在每次上游返回新密文时就地刷新，专职用于调用 TOP `item.info.upgrade.get`、拼装 `https://uland.taobao.com/item/edetail?id={goods_sign}` 及折淘客转链。
  4. ✅ 可用：`taobao.tbk.item.info.upgrade.get` 详情接口支持同时传入【纯数字旧版 `itemid`】
     （如好单库榜单返回的纯数字 ID）或【完整加密长串 `goods_sign`】，单次最多逗号分隔传 20 个 ID。
  5. ✅ 可用：详情接口 `item_basic_info.item_url` 返回的官方手淘落地页格式固定为：
     `https://uland.taobao.com/item/edetail?id={encrypted_item_id}`。

二、 热销榜单获取 (get_hot_rank_goods)
  1. ✅ 可用：好单库榜单接口域名必须使用 `https://v2.api.haodanku.com/sales_list`
     （`sale_type=1` 实时2小时榜 / `sale_type=2` 今日24小时榜），且分页条数 `back` 仅支持离散档位
     `(1, 2, 5, 10, 20, 50, 100)`；拿到好单库 `itemid` 列表后分批（每批 ≤20）调用官方
     `taobao.tbk.item.info.upgrade.get` 补全权威券后价、白底图与加密 `item_id`。
  2. ❌ 不可用（严禁使用）：旧域名 `http://v2.haodanku.com/sales_list`（缺少 `.api`）DNS 无法解析！
  3. ❌ 不可用：`back` 传入非离散档位（如 `back=3`、`back=15`）会被好单库接口拒绝。

三、 推广转链与淘口令生成 (convert_promotion_url 双轨制)
  1. ❌ 不可用：官方详情接口 `taobao.tbk.item.info.upgrade.get` 【绝对不返回】 `click_url` 或
     `coupon_share_url`（其 `publish_info` 仅含佣金率，无任何推广链接）！不能指望仅靠详情接口拿推广长链。
  2. ❌ 不可用：调用官方 `taobao.tbk.dg.material.optional.upgrade` 时，若将 `q` 设为裸加密串
     （`q="{encrypted_id}"`）或 `q="https://item.taobao.com/item.htm?id={encrypted_id}"`，
     上游必定报错 `50001: 无结果`！
  3. ✅ 可用（官方站长轨，无需 relation_id）：
     - 第一步：将 `q` 设为 `https://uland.taobao.com/item/edetail?id={encrypted_id}`（即详情返回的
       `item_url` 或用加密 ID 拼装的 `uland.taobao.com/item/edetail` 链接）调用
       `taobao.tbk.dg.material.optional.upgrade`，可 100% 精准命中单品（`total_results=1`），
       返回新鲜的 `coupon_share_url` 与 `click_url`（注：返回的 `item_id` 会被重新加密）。
     - 第二步：将 `coupon_share_url` 或 `click_url` 传给 `taobao.tbk.tpwd.create`，生成真实淘口令。
  4. ❌ 不可用（折淘客高佣/渠道转链错误示范）：
     - `pid` 参数若只传 1 段 `adzone_id`（如 `"116341500041"`），折淘客必报 `301: 很抱歉！转链失败！`
     - 调用 `open_gaoyongzhuanlian_tkl.ashx` 时若误传参数名 `num_iid` 而不传 `tkl`，必报 `1000: error tkl`！
  5. ✅ 可用（折淘客高佣/渠道轨，支持 relation_id）：
     - `pid` 必须传入完整的 **4 段式 `mm_memberId_siteId_adzoneId`**（如 `mm_328750149_3161250458_116343300219`）。
     - 方式 A：`open_gaoyongzhuanlian_tkl.ashx` 传 `tkl=https://uland.taobao.com/item/edetail?id={encrypted_id}`
       + `pid={4段式PID}` + 可选 `relation_id` + `signurl=5`，一步返回 `coupon_click_url` 与 `tkl` 淘口令。
     - 方式 B：`open_gaoyongzhuanlian.ashx` 传 `num_iid={encrypted_id}` + `pid={4段式PID}`
       + 可选 `relation_id` + `signurl=5`，同样一步返回 `coupon_click_url` 与 `tkl` 淘口令。
     - 若传入未在当前站长下备案的 `relation_id`，折淘客返回 `301: 很抱歉！无效的relationId！`，据此判定 `need_auth=True`。

四、 推手渠道备案核验与邀请码生成 (check_publisher_relation_by_user & generate_tb_authority_url)
  1. ❌ 不可用：自用型 TOP 应用的 `taobao_session`（`TB_SESSION_KEY`）有效期仅 24 小时，过期后直接调用
     官方 `taobao.tbk.sc.publisher.info.get` 或 `taobao.tbk.sc.invitecode.get` 会报错 `27: invalid-sessionkey`。
  2. ✅ 可用（折淘客 90 天长效 sid 自动回退通道）：
     - 渠道核验：`https://api.zhetaoke.com:10001/api/open_sc_publisher_get.ashx`（传 `sid`、`relation_app="common"`、
       `info_type=1`、`external_id=uid`，并在返回列表中按 `external_id == uid` 精确过滤）。
     - 邀请码获取：`https://api.zhetaoke.com:10001/api/open_sc_invitecode_get.ashx`（传 `sid`、`relation_app="common"`、
       `code_type=1`），拼装手淘 H5 备案链接 `https://mos.m.taobao.com/inviter/register?inviterCode={code}&src=pub&app=common&externalId={uid}`。

五、 折淘客 ZTK_SID 与多 AppKey 绑定规则
  1. ⚠️ 实证注意：淘宝折淘客的 `ZTK_SID`（如 `192646`）严格绑定在授权该淘宝账号的折淘客主 `appkey` 下。
     若使用未绑定该 `sid` 的备用 `appkey` 请求，会返回 `301: 授权账号sid过期或者sid和appkey不匹配！`。
     因此多 Key 故障切换时，遇到业务确定性错误（如 `无效的relationId`）不得盲目切 Key 覆盖真实原因。

六、 万能转链与口令解析反查 (`convert_and_resolve_universal_input` 第一性原理实现)
  1. ✅ 主路径（一步闭环最优解）：折淘客 `open_gaoyongzhuanlian_tkl.ashx` 配合 `signurl=5`（详版返回）及 4段式 `TB_CHANNEL_PID`，
     可直接接收任意带淘宝客推广属性或加密 ID 的输入，并在 **单次 HTTP 请求 (~400ms)** 内同时返回转链结果与完整商品详情：
     - ✅ 裸加密商品 ID (`item_id` / `goods_sign`，自动包装为 `https://uland.taobao.com/item/edetail?id={id}`)
     - ✅ 官方手淘落地页：`https://uland.taobao.com/item/edetail?id={encrypted_id}`
     - ✅ 加密 ID 淘宝直链：`https://item.taobao.com/item.htm?id={encrypted_id}`
     - ✅ 折淘客淘口令 (`tkl`)、推广短链 (`https://s.click.taobao.com/xxx`)、手淘短链 (`https://s.tb.cn/h.xxx`)、二合一长链 (`https://uland.taobao.com/coupon/edetail?...`)
     - ✅ 官方 `taobao.tbk.tpwd.create` 生成的完整口令文案 (`model`，含 `https://m.tb.cn/h.xxx`) 及裸 `https://m.tb.cn/h.xxx` 短链
     - ✅ 本平台生成的完整推广口令文案 (`command_text`)
  2. ✅ 兜底路径（普通手淘商品分享 `e.tb.cn` / 数字 ID 链接 + `「商品标题」`）：
     - 阿里妈妈与折淘客已全面废除纯数字 `num_iid` 直接转链（普通手淘 App 分享链接解包后为纯数字 ID，折淘客返回 `301: 很抱歉！不支持数字id转链！！`）。
     - 当主路径遇到 `不支持数字id转链` 或输入为普通商品标题文案时，自动从输入中提取 `「商品标题」`（或清洗 URL/口令后的标题文本），
       调用官方 `taobao.tbk.dg.material.optional.upgrade` (`search_tb_goods`) 检索出加密 `goods_sign`，再交由折淘客一步转链，实现 11 种格式 100% 闭环。
  3. ❌ 不可用输入：不含任何商品标题的纯数字 ID 或 `https://item.taobao.com/item.htm?id={纯数字}`（明确返回 `UNSUPPORTED_TB_NUMERIC_ID` 引导用户粘贴含 `「商品标题」` 的手淘分享文案或淘口令）。

七、 商品唯一标识 (`product_id` / `goods_id`) 与动态调用令牌 (`goods_sign`) 职责解耦及防 `50001` 报错铁律
  1. **静态主键 vs 动态签名**：
     - 淘宝 TOP 27939 权限包与折淘客返回的混淆 `item_id` 固定为 `<26位动态盐值前缀>-<19位稳定密文后缀>`（如 `BjGgNQpU6t85xbZdyxiM9xurte-5n9p6DJF6yr3QZWzFNY`）。
     - `goods_id` / `product_id` 提取 `-` 后的 19 位稳定后缀（如 `5n9p6DJF6yr3QZWzFNY`），专职用于 MongoDB `(platform, product_id)` 唯一索引去重；
     - `goods_sign` 保留完整的 46 位 `<prefix>-<suffix>` 动态密文，专职用于请求上游 `taobao.tbk.item.info.upgrade.get` 详情接口与转链。
  2. **严禁拿 19 位后缀 `goods_id` 直接请求 TOP 详情接口**：
     - 若将剥离了 `<prefix>-` 的 19 位 `goods_id`（如 `5n9p6DJF6yr3QZWzFNY`）直接作为 `item_id` 传给 `taobao.tbk.item.info.upgrade.get`，淘宝开放平台必报 `[15:50001] 无结果`！
     - 防护闭环：前端列表/卡片跳转详情时必须分别透传 `goods_id` 与完整 `goods_sign`（严禁以 `goods_id` 覆盖 `goods_sign`）；当后端收到 `goods_sign == goods_id`（或缺失 `goods_sign`）的请求时，必须先通过内存 `SIGN_BY_ID_CACHE` 或 MongoDB `{"platform": "taobao", "product_id": goods_id}` 索引反查还原出完整的 46 位 `goods_sign`，再命中缓存或调用上游接口。
================================================================================
"""

import hashlib
import json
import os
import re
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
TB_PID = str(_cfg.get("myself_taobao_pid", "")).strip()
TB_ADZONE_ID = str(_cfg.get("taobao_adzone_id") or (TB_PID.split("_")[-1] if "_" in TB_PID else TB_PID)).strip()
TB_PID_PREFIX = str(_cfg.get("taobao_pid_prefix") or (TB_PID.rsplit("_", 1)[0] if TB_PID.startswith("mm_") and len(TB_PID.split("_")) == 4 else "mm_328750149_3161250458")).strip()
TB_FULL_PID = TB_PID if (TB_PID.startswith("mm_") and len(TB_PID.split("_")) == 4) else f"{TB_PID_PREFIX}_{TB_ADZONE_ID or '116343300219'}"
TB_CHANNEL_PID = str(_cfg.get("taobao_channel_pid") or f"{TB_PID_PREFIX}_{_cfg.get('taobao_channel_adzone_id') or TB_ADZONE_ID or '116343300219'}").strip()
TB_SESSION_KEY = _cfg.get("taobao_session", "")
TB_INVITER_CODE = _cfg.get("taobao_inviter_code", "")
HDK_API_KEY = _cfg.get("haodanku_api_key", "")
ZTK_APPKEY_LIST = parse_appkey_list(_cfg.get("zhetaoke_appkey", ""), _cfg.get("zhetaoke_appkey_list"))
ZTK_APPKEY = ZTK_APPKEY_LIST[0] if ZTK_APPKEY_LIST else ""
ZTK_SID = str(_cfg.get("zhetaoke_sid", "")).strip()

TOP_GATEWAY_URL = "https://eco.taobao.com/router/rest"
# ✅ 实证可用域名为 https://v2.api.haodanku.com/sales_list (❌ http://v2.haodanku.com/sales_list DNS 无法解析)
HDK_SALES_URL = "https://v2.api.haodanku.com/sales_list"
ZTK_CONVERT_TKL_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
ZTK_CONVERT_IID_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian.ashx"
ZTK_PUBLISHER_GET_URL = "https://api.zhetaoke.com:10001/api/open_sc_publisher_get.ashx"
ZTK_INVITECODE_GET_URL = "https://api.zhetaoke.com:10001/api/open_sc_invitecode_get.ashx"
ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"

_ZTK_KEY_FAILURE_KEYWORDS = (
    "appkey", "key", "频次", "频率", "超限", "上限", "次数", "黑名单",
    "封禁", "未授权", "到期", "过期", "积分", "余额", "限制",
)


def _should_failover_ztk_key(payload: Any) -> bool:
    """判断折淘客响应是否属于 AppKey 额度/权限/频次问题，以触发备用 Key 切换（确定性业务错误不切 Key）"""
    if not isinstance(payload, dict):
        return True
    if "tbk_sc_publisher_info_get_response" in payload or "tbk_sc_invitecode_get_response" in payload:
        return False
    if "tbk_sc_order_details_get_response" in payload or "tbk_order_details_get_response" in payload:
        return False
    status_val = payload.get("status")
    code_val = payload.get("code")
    if status_val in (200, "200") or code_val in (200, "200"):
        return False
    # 确定性业务错误（relationId无效、不支持数字id转链、口令格式错误等），换 Key 结果相同且备用 Key 可能未绑 sid 报 sid不匹配，禁止切 Key
    msg = " ".join(str(payload.get(k) or "") for k in ("content", "msg", "message", "sub_msg")).lower()
    if any(b in msg for b in ("relationid", "无效的relation", "不支持数字id", "error tkl", "参数错误")):
        return False
    return any(kw in msg for kw in _ZTK_KEY_FAILURE_KEYWORDS) or status_val not in (None, 200, "200")


def request_ztk_with_failover(url: str, params: Dict[str, Any], timeout: int = 12) -> Dict[str, Any]:
    """带多 AppKey 自动故障切换的折淘客 GET 请求封装（保护主 Key 业务错误不被未绑 sid 的备用 Key 覆盖）"""
    keys = ZTK_APPKEY_LIST or ([params["appkey"]] if params.get("appkey") else [])
    if not keys:
        return {"status": "error", "error_msg": "未配置折淘客 zhetaoke_appkey"}

    last_error = "折淘客请求失败"
    last_data: Optional[Dict[str, Any]] = None
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

            if isinstance(data, dict):
                cur_msg = str(data.get("content") or data.get("msg") or "")
                # 若已有主 Key 返回的具体错误，且备用 Key 仅因未绑定当前 sid 报 sid和appkey不匹配，则保留主 Key 的错误
                if last_data is not None and ("sid和appkey不匹配" in cur_msg or "sid过期" in cur_msg):
                    continue
                last_data = data
            last_error = f"Key[{idx + 1}] 返回异常: {data}"
        except Exception as e:
            last_error = f"Key[{idx + 1}] 网络异常: {e}"
    return {"status": "error", "error_msg": last_error, "data": last_data}


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


def extract_tb_canonical_goods_id(raw_id_or_sign: str) -> str:
    """
    提取淘宝商品全链路恒定唯一标识 (goods_id / product_id)：
    - 若为纯数字 itemid（如好单库榜单回显），直接返回纯数字；
    - 若为 TOP / 折淘客两段式加密串 `<26位动态盐值前缀>-<19~21位确定性密文后缀>`
      (如 "0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd")，提取 `-` 之后的确定性商品密文后缀
      ("0v7DKOgI5JqnZd4Nhxd")，确保同一商品跨时间、跨接口拥有唯一不变的 goods_id。
    """
    text = str(raw_id_or_sign or "").strip()
    if not text or text.isdigit():
        return text
    if "-" in text:
        prefix, suffix = text.split("-", 1)
        if len(prefix) >= 15 and len(suffix) >= 12 and re.match(r"^[A-Za-z0-9_]+$", suffix):
            return suffix
    return text


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

    item_url = _fix_url(basic.get("item_url") or "")
    if not item_url and item_id:
        item_url = f"https://uland.taobao.com/item/edetail?id={item_id}"

    canonical_goods_id = input_iid if input_iid.isdigit() else extract_tb_canonical_goods_id(item_id)

    return {
        "platform": "taobao",
        "goods_id": canonical_goods_id,
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
        "item_url": item_url,
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
    - 域名基准: 必须使用 https://v2.api.haodanku.com/sales_list (❌ v2.haodanku.com 无法解析)
    - sale_type=1: 实时热销榜 (2小时销量榜, 对应前端 channel_type=5)
    - sale_type=2: 今日爆款榜 (全天销量榜, 对应前端 channel_type=1)
    - back: 仅支持离散档位 (1, 2, 5, 10, 20, 50, 100)
    """
    if not HDK_API_KEY:
        return {"status": "error", "error_msg": "未配置好单库 haodanku_api_key"}

    valid_backs = (1, 2, 5, 10, 20, 50, 100)
    req_back = next((b for b in valid_backs if b >= back), 100)

    try:
        resp = requests.get(HDK_SALES_URL, params={
            "apikey": HDK_API_KEY,
            "sale_type": sale_type,
            "cid": 0,
            "min_id": min_id,
            "back": req_back,
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
        "data": enriched[:back],
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
    """
    获取单个淘宝商品详情 (taobao.tbk.item.info.upgrade.get)：
    ⚠️ 基准规范：该接口仅返回商品基础信息、券面额与 item_url (https://uland.taobao.com/item/edetail?id=...)，
    【绝对不返回】click_url 或 coupon_share_url！
    """
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


def convert_channel_url_by_ztk(target_url_or_id: str, relation_id: Optional[str] = None) -> Dict[str, Any]:
    """
    调用折淘客高佣转链接口生成专属淘口令与二合一链接（支持多 Key 故障切换）：
    - ✅ 基准规范 1: pid 必须传入完整的 4 段式 `mm_memberId_siteId_adzoneId` (TB_CHANNEL_PID)，
      ❌ 若只传 1 段 adzone_id (如 "116341500041") 必定报错 `301: 很抱歉！转链失败！`。
    - ✅ 基准规范 2: `open_gaoyongzhuanlian_tkl.ashx` 接收参数名为 `tkl` (传入 uland/item/m.tb.cn 链接或淘口令)，
      ❌ 若误传 `num_iid` 必定报错 `1000: error tkl`。
    """
    if not ZTK_SID or not ZTK_APPKEY_LIST:
        return {"status": "error", "error_msg": "折淘客 SID 或 AppKey 未配置"}

    raw_target = str(target_url_or_id or "").strip()
    if not raw_target:
        return {"status": "error", "error_msg": "转链目标不能为空"}

    # 判断是否为纯商品 ID（数字 ID 或不含空白/口令符/URL 的加密 ID）
    is_bare_id = bool(re.match(r"^[A-Za-z0-9_-]{8,}$", raw_target)) and not raw_target.startswith(("http://", "https://"))
    tkl_input = f"https://uland.taobao.com/item/edetail?id={raw_target}" if is_bare_id else raw_target

    req_params: Dict[str, Any] = {
        "sid": ZTK_SID,
        "pid": TB_CHANNEL_PID,
        "tkl": tkl_input,
        "signurl": 5,
    }
    if relation_id:
        req_params["relation_id"] = str(relation_id).strip()

    failover_res = request_ztk_with_failover(ZTK_CONVERT_TKL_URL, req_params)
    res_data = failover_res.get("data") if isinstance(failover_res.get("data"), dict) else {}
    if failover_res["status"] != "success" and not res_data:
        return failover_res

    if res_data.get("status") == 200 and isinstance(res_data.get("content"), list) and res_data["content"]:
        item_info = res_data["content"][0]
        final_link = _fix_url(item_info.get("coupon_click_url") or item_info.get("item_url", ""))
        return {
            "status": "success",
            "is_channel": bool(relation_id),
            "relation_id": str(relation_id) if relation_id else None,
            "final_link": final_link,
            "short_url": _fix_url(item_info.get("shorturl") or final_link),
            "short_url2": _fix_url(item_info.get("shorturl2") or ""),
            "tpwd": item_info.get("tkl", ""),
            "title": item_info.get("title", ""),
            "final_price": item_info.get("quanhou_jiage", ""),
            "commission_rate": item_info.get("tkrate3", ""),
            "raw_item": item_info,
        }

    err_text = str(res_data.get("content") or res_data.get("msg") or failover_res.get("error_msg") or "")
    is_unauth = "relationid" in err_text.lower() or "无效的relation" in err_text.lower()
    is_num_id = "不支持数字id" in err_text.lower()
    return {
        "status": "error",
        "error_code": "AUTH_REQUIRED" if is_unauth else ("UNSUPPORTED_TB_NUMERIC_ID" if is_num_id else "UPSTREAM_ERROR"),
        "need_auth": is_unauth,
        "error_msg": f"折淘客渠道转链失败: {err_text or res_data}",
    }


def convert_promotion_url(
    item_id: str,
    relation_id: Optional[str] = None,
    title: str = "粉丝专享福利购",
) -> Dict[str, Any]:
    """
    淘宝双轨制转链标准基线：
    1. 先调用官方详情 `get_tb_single_detail(item_id)` 拿到权威 `item_url` (`https://uland.taobao.com/item/edetail?id={encrypted_id}`)。
    2. 轨道 B (传入 relation_id 推手渠道轨)：
       将 `item_url` + 4段式 `TB_CHANNEL_PID` + `relation_id` 传给折淘客 `open_gaoyongzhuanlian_tkl.ashx` 生成渠道专属淘口令。
    3. 轨道 A (无 relation_id 官方站长轨)：
       - ❌ 严禁直接把裸加密串 `item_id` 或 `https://item.taobao.com/item.htm?id={encrypted_id}` 传给 `search_tb_goods`，会报 `50001 无结果`！
       - ✅ 必须将 `q=https://uland.taobao.com/item/edetail?id={encrypted_id}` 传给 `taobao.tbk.dg.material.optional.upgrade`，
         精准命中单品拿到 `coupon_share_url` / `click_url`，再调用 `taobao.tbk.tpwd.create` 生成淘口令。
    """
    detail_res = get_tb_single_detail(item_id)
    detail_item = detail_res["data"] if detail_res["status"] == "success" else {}
    encrypted_sign = detail_item.get("goods_sign") or item_id
    uland_url = detail_item.get("item_url") or f"https://uland.taobao.com/item/edetail?id={encrypted_sign}"
    resolved_title = detail_item.get("title") or title

    if relation_id:
        return convert_channel_url_by_ztk(uland_url, str(relation_id))

    # 官方站长轨：必须使用 uland.taobao.com/item/edetail?id=... 作为搜索词 q
    search_res = search_tb_goods(uland_url, page_no=1, page_size=20)
    target_item = None
    if search_res["status"] == "success" and search_res["data"]:
        for candidate in search_res["data"]:
            if item_id in (candidate.get("goods_id"), candidate.get("goods_sign"), candidate.get("input_item_iid")):
                target_item = candidate
                break
        # TOP 对 uland 链接精准查询每次都会重新加密 item_id，当精确返回唯一 1 条商品时即为目标单品
        if not target_item and len(search_res["data"]) == 1:
            target_item = search_res["data"][0]

    if not target_item:
        return {"status": "error", "error_msg": f"官方 uland 精准生链未命中商品: {uland_url}"}

    promo_url = target_item.get("coupon_share_url") or target_item.get("click_url", "")
    if not promo_url:
        return {"status": "error", "error_msg": "官方返回商品缺失推广链接"}

    tpwd_res = create_tpwd(promo_url, text=target_item.get("title") or resolved_title)
    if tpwd_res["status"] != "success":
        return tpwd_res

    return {
        "status": "success",
        "is_channel": False,
        "final_link": promo_url,
        "tpwd": tpwd_res.get("password_simple") or tpwd_res.get("model", ""),
        "model": tpwd_res.get("model", ""),
        "title": target_item.get("title", resolved_title),
    }


# ==============================================================================
# 核心功能 5：推手渠道备案查询与授权邀请码生成 (官方 TOP + 折淘客 90 天长效 sid 自动回退)
# ==============================================================================
def check_publisher_relation_by_user(uid: str) -> Dict[str, Any]:
    """
    查询指定 uid (external_id) 是否已在站长渠道下完成淘宝客备案及对应的 relation_id：
    - 优先尝试阿里妈妈官方 TOP `taobao.tbk.sc.publisher.info.get` (需 24h 有效的 TB_SESSION_KEY)
    - 当 `TB_SESSION_KEY` 过期报错 `27: invalid-sessionkey` 时，自动无缝回退至折淘客 90 天长效 `ZTK_SID`
      接口 `open_sc_publisher_get.ashx`，并按 `external_id == uid` 精确过滤。
    """
    clean_uid = str(uid or "").strip()
    if not clean_uid:
        return {"status": "error", "error_msg": "uid 不能为空"}

    if TB_SESSION_KEY:
        res = call_tb_api(
            TB_APP_KEY,
            TB_APP_SECRET,
            "taobao.tbk.sc.publisher.info.get",
            {"info_type": 1, "relation_app": "common", "external_id": clean_uid, "page_no": 1, "page_size": 10},
            session_key=TB_SESSION_KEY,
        )
        if res["status"] == "success":
            inviter_list = (
                (res["data"].get("tbk_sc_publisher_info_get_response") or {})
                .get("data", {})
                .get("inviter_list", {})
                .get("map_data")
                or []
            )
            if isinstance(inviter_list, dict):
                inviter_list = [inviter_list]
            for record in inviter_list:
                if str(record.get("external_id") or "") == clean_uid and record.get("relation_id"):
                    return {
                        "status": "success",
                        "channel": "official_top",
                        "is_authorized": True,
                        "bind": 1,
                        "relation_id": str(record["relation_id"]),
                        "account_name": record.get("account_name", ""),
                    }
            return {"status": "success", "channel": "official_top", "is_authorized": False, "bind": 0, "relation_id": None}

    # 回退通道：折淘客 90 天长效 ZTK_SID 渠道备案核验接口 (open_sc_publisher_get.ashx)
    if ZTK_SID and ZTK_APPKEY_LIST:
        ztk_res = request_ztk_with_failover(ZTK_PUBLISHER_GET_URL, {
            "sid": ZTK_SID,
            "relation_app": "common",
            "info_type": 1,
            "external_id": clean_uid,
            "page_no": 1,
            "page_size": 100,
        })
        if ztk_res["status"] == "success" and isinstance(ztk_res.get("data"), dict):
            resp_data = (ztk_res["data"].get("tbk_sc_publisher_info_get_response") or {}).get("data") or {}
            inviter_list = resp_data.get("inviter_list") or {}
            records = inviter_list.get("map_data", []) if isinstance(inviter_list, dict) else (inviter_list if isinstance(inviter_list, list) else [])
            if isinstance(records, dict):
                records = [records]
            for record in records:
                if isinstance(record, dict) and str(record.get("external_id") or "").strip() == clean_uid and record.get("relation_id"):
                    return {
                        "status": "success",
                        "channel": "zhetaoke_sid_fallback",
                        "is_authorized": True,
                        "bind": 1,
                        "relation_id": str(record["relation_id"]),
                        "account_name": record.get("account_name", ""),
                    }
            return {
                "status": "success",
                "channel": "zhetaoke_sid_fallback",
                "is_authorized": False,
                "bind": 0,
                "relation_id": None,
                "total_channel_records": resp_data.get("total_count", len(records)),
            }

    return {"status": "error", "error_msg": "官方 TB_SESSION_KEY 已过期且折淘客 ZTK_SID 回退通道不可用"}


def generate_tb_authority_url(uid: str) -> Dict[str, Any]:
    """
    生成携带 externalId=uid 的手淘 H5 渠道备案邀请链接：
    - 优先使用配置的 `TB_INVITER_CODE` 或官方 `taobao.tbk.sc.invitecode.get`
    - 当 `TB_SESSION_KEY` 过期时，自动通过折淘客 90 天长效 `ZTK_SID` 调用 `open_sc_invitecode_get.ashx` 获取真实邀请码
    """
    inviter_code = str(TB_INVITER_CODE or "").strip()
    channel = "config"
    if not inviter_code and TB_SESSION_KEY:
        res = call_tb_api(
            TB_APP_KEY,
            TB_APP_SECRET,
            "taobao.tbk.sc.invitecode.get",
            {"relation_app": "common", "code_type": 1},
            session_key=TB_SESSION_KEY,
        )
        if res["status"] == "success":
            inviter_code = str(
                ((res["data"].get("tbk_sc_invitecode_get_response") or {}).get("data") or {}).get("inviter_code") or ""
            ).strip()
            if inviter_code:
                channel = "official_top"

    if not inviter_code and ZTK_SID and ZTK_APPKEY_LIST:
        ztk_res = request_ztk_with_failover(ZTK_INVITECODE_GET_URL, {
            "sid": ZTK_SID,
            "relation_app": "common",
            "code_type": 1,
        })
        if ztk_res["status"] == "success" and isinstance(ztk_res.get("data"), dict):
            inviter_code = str(
                ((ztk_res["data"].get("tbk_sc_invitecode_get_response") or {}).get("data") or {}).get("inviter_code") or ""
            ).strip()
            if inviter_code:
                channel = "zhetaoke_sid_fallback"

    if not inviter_code:
        return {"status": "error", "error_msg": "无法获取淘宝渠道邀请码 inviter_code"}

    encoded_uid = quote_plus(str(uid))
    auth_url = f"https://mos.m.taobao.com/inviter/register?inviterCode={inviter_code}&src=pub&app=common&externalId={encoded_uid}"
    return {
        "status": "success",
        "channel": channel,
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
# 核心功能 7：万能转链与口令解析反查 (支持淘口令 / m.tb.cn / e.tb.cn / s.click / uland / 商品ID)
# ==============================================================================
def _extract_tb_candidate_title(text: str) -> str:
    """从手淘分享文案或纯标题中提取用于 TOP 检索的商品标题（不含 URL/口令/引导语）"""
    bracket_match = re.search(r"「([^」]{4,})」", text)
    if bracket_match:
        return bracket_match.group(1).strip()
    cleaned = re.sub(r"https?://\S+", " ", text)
    cleaned = re.sub(r"[￥¥$€₴£¢()（）/][A-Za-z0-9]{6,20}[￥¥$€₴£¢()（）/]", " ", cleaned)
    cleaned = re.sub(r"\b(?:CZ|CA|ZH|MF|HU|TG)\s*\d{2,6}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"【(?:淘宝|天猫|好物推荐|手淘分享)[^】]*】", " ", cleaned)
    cleaned = re.sub(r"\d+\s*[\$￥¥!！:/]+\s*", " ", cleaned)
    cleaned = re.sub(r"(?:点击链接直接打开|手机淘宝|淘宝App|复制本段|打开手淘)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。！？、,!?:：-")
    if len(cleaned) >= 4 and not re.match(r"^[A-Za-z0-9_-]+$", cleaned):
        return cleaned
    return ""


def convert_and_resolve_universal_input(
    raw_text: str,
    relation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    淘宝万能转链与详情一步反查基线函数（第一性原理：折淘客 signurl=5 一步主轨 + 官方 TOP 标题搜加密 ID 兜底）：
    支持全部 11 种真实输入格式：
    1. 裸加密商品 ID (item_id / goods_sign)
    2. 官方手淘落地页：https://uland.taobao.com/item/edetail?id=...
    3. 加密 ID 淘宝直链：https://item.taobao.com/item.htm?id=...
    4. 折淘客淘口令 (tkl)
    5. 折淘客推广短链：https://s.click.taobao.com/xxx
    6. 折淘客手淘短链：https://s.tb.cn/h.xxx
    7. 折淘客二合一领券长链：https://uland.taobao.com/coupon/edetail?...
    8. 官方 tpwd.create 完整口令文案 (model，含 https://m.tb.cn/h.xxx)
    9. 裸官方手淘短链：https://m.tb.cn/h.xxx
    10. 本平台自生成多行推广文案 (command_text)
    11. 普通手淘商品分享文案（如 `【淘宝】https://e.tb.cn/h.xxx 「商品标题」` 或含数字 ID 直链 + `「商品标题」`）
    """
    text = str(raw_text or "").strip()
    if not text:
        return {"status": "error", "error_msg": "输入内容不能为空"}

    if any(k in text for k in ("m.tb.cn", "e.tb.cn", "s.tb.cn", "tb.cn")):
        input_type = "tb_share_short_url_or_tkl"
    elif "s.click.taobao.com" in text or "uland.taobao.com" in text:
        input_type = "tbk_promotion_url"
    elif "item.taobao.com" in text or "detail.tmall.com" in text:
        input_type = "tb_item_url"
    elif re.search(r"[￥¥$€₴£¢()（）][A-Za-z0-9]{6,}[￥¥$€₴£¢()（）]", text):
        input_type = "tkl_command"
    else:
        input_type = "item_id_or_text"

    # 若是纯数字裸 ID 或无标题的纯数字 item.taobao.com 链接，提前识别
    is_bare_numeric = text.isdigit() or bool(
        re.search(r"(?:item\.taobao\.com|detail\.tmall\.com)/item\.htm\?[^#\s]*id=\d+\b", text)
        and not _extract_tb_candidate_title(text)
    )
    if is_bare_numeric:
        return {
            "status": "error",
            "input_type": input_type,
            "error_code": "UNSUPPORTED_TB_NUMERIC_ID",
            "error_msg": "淘宝联盟已停用纯数字商品ID直接转链，请粘贴手淘App分享的完整文案（含「商品标题」或淘口令）或推广链接",
        }

    # 主路径：直接交由折淘客 open_gaoyongzhuanlian_tkl.ashx (signurl=5) 一步解析+转链
    conv_res = convert_channel_url_by_ztk(text, relation_id=relation_id)

    # 兜底路径：当输入为普通手淘分享（e.tb.cn 解包为数字 ID 报 301 不支持数字id转链）或纯标题文案时，提取标题调官方 TOP 搜索拿加密 goods_sign 再转链
    if conv_res["status"] != "success" and not conv_res.get("need_auth"):
        candidate_title = _extract_tb_candidate_title(text)
        if candidate_title:
            search_res = search_tb_goods(candidate_title, page_no=1, page_size=5)
            if search_res["status"] == "success" and search_res.get("data"):
                first_hit = search_res["data"][0]
                enc_sign = first_hit.get("goods_sign") or first_hit.get("goods_id")
                if enc_sign:
                    input_type = "title_search_fallback"
                    conv_res = convert_channel_url_by_ztk(str(enc_sign), relation_id=relation_id)

    if conv_res["status"] != "success":
        err_code = conv_res.get("error_code")
        err_msg = conv_res.get("error_msg", "淘宝万能转链解析失败")
        if err_code == "UNSUPPORTED_TB_NUMERIC_ID" or "不支持数字id" in str(err_msg):
            err_code = "UNSUPPORTED_TB_NUMERIC_ID"
            err_msg = "淘宝联盟已停用纯数字商品ID直接转链，请粘贴手淘App分享的完整文案（含「商品标题」或淘口令）或推广链接"
        return {
            "status": "error",
            "input_type": input_type,
            "need_auth": conv_res.get("need_auth", False),
            "error_code": err_code,
            "error_msg": err_msg,
        }

    raw_item = conv_res.get("raw_item") or {}
    tao_id = str(raw_item.get("tao_id") or "")
    title = str(raw_item.get("title") or conv_res.get("title") or "淘宝精选商品")
    pict_url = _fix_url(raw_item.get("pict_url") or "")
    small_images_raw = raw_item.get("small_images") or ""
    if isinstance(small_images_raw, str):
        carousel = [_fix_url(u) for u in small_images_raw.split("|") if u.strip()]
    elif isinstance(small_images_raw, list):
        carousel = [_fix_url(u) for u in small_images_raw if str(u).strip()]
    else:
        carousel = []
    if pict_url and pict_url not in carousel:
        carousel.insert(0, pict_url)

    try:
        orig_price = round(float(raw_item.get("size") or 0), 2)
    except Exception:
        orig_price = 0.0
    try:
        final_price = round(float(raw_item.get("quanhou_jiage") or orig_price or 0), 2)
    except Exception:
        final_price = orig_price
    try:
        coupon_money = round(float(raw_item.get("coupon_info_money") or 0), 2)
    except Exception:
        coupon_money = 0.0
    try:
        comm_rate = round(float(raw_item.get("tkrate3") or 0), 2)
    except Exception:
        comm_rate = 0.0
    try:
        comm_fee = round(float(raw_item.get("tkfee3") or (final_price * comm_rate / 100.0)), 2)
    except Exception:
        comm_fee = 0.0

    item_url = _fix_url(raw_item.get("item_url") or (f"https://uland.taobao.com/item/edetail?id={tao_id}" if tao_id else ""))
    product_card = {
        "platform": "taobao",
        "goods_id": tao_id,
        "goods_sign": tao_id,
        "title": title,
        "name": title,
        "image_url": pict_url,
        "carousel_images": carousel,
        "price": final_price,
        "original_price": orig_price if orig_price >= final_price else round(final_price + coupon_money, 2),
        "coupon_amount": coupon_money,
        "commission_rate": comm_rate,
        "commission_amount": comm_fee,
        "mall_name": raw_item.get("shop_title") or raw_item.get("nick") or "淘宝天猫好店",
        "sales_tip": str(raw_item.get("volume") or "热销中"),
        "item_url": item_url,
        "product_url": item_url,
    }

    tpwd = conv_res.get("tpwd") or ""
    final_link = conv_res.get("final_link") or ""
    short_url = conv_res.get("short_url") or final_link
    share_command = f"{tpwd} {short_url} {title}".strip() if tpwd else final_link

    return {
        "status": "success",
        "platform": "taobao",
        "input_type": input_type,
        "promotion": {
            "final_link": final_link,
            "short_url": short_url,
            "tpwd": tpwd,
            "command": share_command,
            "is_channel": bool(relation_id),
        },
        "product": product_card,
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
    sample_title = ""

    # 1. 验证热销榜单 (sale_type=1 实时热销榜 2h, https://v2.api.haodanku.com/sales_list)
    print("\n[1/7] 验证淘宝热销榜单拉取 (https://v2.api.haodanku.com/sales_list, sale_type=1)...")
    rank_res = get_hot_rank_goods(sale_type=1, min_id=1, back=10)
    if rank_res["status"] == "success" and rank_res["data"]:
        first = rank_res["data"][0]
        sample_id = first.get("goods_sign") or first.get("goods_id")
        sample_title = first.get("title", "")
        print(f" -> 成功拉取 {len(rank_res['data'])} 条榜单商品，首条: {first['title'][:25]} (¥{first['price']})")
        print(f" -> 首条加密 goods_sign: {str(sample_id)[:38]}...")
    else:
        print(f" -> 榜单拉取结果: {rank_res.get('error_msg')}")

    # 2. 验证关键词搜索
    print("\n[2/7] 验证淘宝关键词搜索 (taobao.tbk.dg.material.optional.upgrade)...")
    search_res = search_tb_goods("抽纸", page_no=1, page_size=5)
    if search_res["status"] == "success" and search_res["data"]:
        first = search_res["data"][0]
        sample_id = sample_id or first.get("goods_sign") or first.get("goods_id")
        sample_title = sample_title or first.get("title", "")
        print(f" -> 搜索成功，共命中 {search_res['total_count']} 条，首条: {first['title'][:25]} (¥{first['price']})")
    else:
        print(f" -> 搜索失败: {search_res.get('error_msg')}")

    # 3. 验证单品详情
    print("\n[3/7] 验证淘宝单品详情 (taobao.tbk.item.info.upgrade.get)...")
    if sample_id:
        detail_res = get_tb_single_detail(sample_id)
        if detail_res["status"] == "success":
            d = detail_res["data"]
            sample_title = d.get("title") or sample_title
            print(f" -> 详情成功: {d['title'][:25]} | 店铺: {d.get('mall_name')} | 佣金率: {d.get('commission_rate')}%")
            print(f" -> 官方落地页 item_url: {d.get('item_url', '')[:65]}...")
        else:
            print(f" -> 详情失败: {detail_res.get('error_msg')}")

    # 4. 验证双轨转链与淘口令生成
    print("\n[4/7] 验证淘宝双轨推广转链与淘口令生成...")
    conv_res: Dict[str, Any] = {}
    ztk_std_res: Dict[str, Any] = {}
    if sample_id:
        # 4a. 官方站长轨 (uland 精准查询 -> tpwd.create)
        conv_res = convert_promotion_url(sample_id)
        print(f" -> [4a] 官方站长轨 (uland -> optional.upgrade -> tpwd.create): status={conv_res['status']}, tpwd={conv_res.get('tpwd')}")
        # 4b. 折淘客高佣转链 (4段式 PID: TB_CHANNEL_PID)
        ztk_std_res = convert_channel_url_by_ztk(sample_id, relation_id=None)
        print(f" -> [4b] 折淘客高佣转链 (pid={TB_CHANNEL_PID}, 无relation_id): status={ztk_std_res['status']}, tpwd={ztk_std_res.get('tpwd')}")
        # 4c. 折淘客渠道转链传入无效 relation_id 时的未授权拦截识别
        ztk_bad_rel = convert_channel_url_by_ztk(sample_id, relation_id="9999999999")
        print(f" -> [4c] 未备案/无效 relation_id 拦截验证: status={ztk_bad_rel['status']}, error_code={ztk_bad_rel.get('error_code')}, need_auth={ztk_bad_rel.get('need_auth')}")

    # 5. 验证推手渠道备案查询与邀请链接生成 (含折淘客 90 天 ZTK_SID 自动回退)
    print("\n[5/7] 验证淘宝推手渠道备案查询与邀请码生成 (TOP 优先 + 折淘客 90天 SID 自动回退)...")
    rel_res = check_publisher_relation_by_user(test_uid)
    print(f" -> 渠道备案查询: {rel_res}")
    if not rel_res.get("is_authorized"):
        auth_url_res = generate_tb_authority_url(test_uid)
        print(f" -> 备案邀请链接: status={auth_url_res['status']}, channel={auth_url_res.get('channel')}, code={auth_url_res.get('inviter_code')}, url={auth_url_res.get('auth_url')}")

    # 6. 验证增量订单查询 (20分钟时间窗)
    print("\n[6/7] 验证淘宝增量订单查询 (最近 20 分钟)...")
    now_dt = datetime.now()
    start_str = (now_dt - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    end_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    ord_res = fetch_increment_orders_strictly(start_str, end_str)
    print(f" -> 订单查询结果: status={ord_res['status']}, 数量={len(ord_res.get('order_list', []))}")

    # 7. 验证万能转链与口令解析反查全矩阵 (11 种真实格式自生自测 + 纯数字无标题拦截验证)
    print("\n[7/7] 验证淘宝万能转链与口令解析反查全矩阵 (11 种真实格式自生自测)...")
    if sample_id and ztk_std_res.get("status") == "success":
        ztk_tkl = ztk_std_res.get("tpwd") or ""
        ztk_s_click = ztk_std_res.get("short_url") or ""
        ztk_s_tb = ztk_std_res.get("short_url2") or ztk_s_click
        ztk_coupon_url = ztk_std_res.get("final_link") or ""
        top_model = conv_res.get("model") or ztk_tkl
        m_tb_match = re.search(r"https?://m\.tb\.cn/\S+", top_model)
        bare_m_tb = m_tb_match.group(0) if m_tb_match else ztk_s_tb
        platform_cmd = f"【好物推荐】{sample_title}\n👉 复制整段口令打开手机淘宝：{ztk_tkl}\n🔗 抢购链接：{ztk_s_click}"
        normal_app_share = f"【淘宝】https://item.taobao.com/item.htm?id=654321098765 CZ0001 「{sample_title}」\n点击链接直接打开"

        tb_cases = [
            ("7a. 裸加密 item_id", sample_id),
            ("7b. 官方手淘落地页 (uland item/edetail)", f"https://uland.taobao.com/item/edetail?id={sample_id}"),
            ("7c. 加密ID淘宝直链 (item.taobao.com)", f"https://item.taobao.com/item.htm?id={sample_id}"),
            ("7d. 折淘客淘口令 (tkl)", f"67{ztk_tkl} 【手淘好物分享】"),
            ("7e. 折淘客推广短链 (s.click.taobao.com)", ztk_s_click),
            ("7f. 折淘客手淘短链 (s.tb.cn)", ztk_s_tb),
            ("7g. 折淘客二合一长链 (uland coupon/edetail)", ztk_coupon_url),
            ("7h. 官方 tpwd.create 完整口令文案 (含 m.tb.cn)", top_model),
            ("7i. 官方手淘短链 (裸 m.tb.cn)", bare_m_tb),
            ("7j. 平台自生成多行推广文案", platform_cmd),
            ("7k. 普通手淘商品分享文案 (含「商品标题」兜底)", normal_app_share),
        ]
        for label, text_input in tb_cases:
            u_res = convert_and_resolve_universal_input(text_input, relation_id=None)
            prod = u_res.get("product") or {}
            promo = u_res.get("promotion") or {}
            print(
                f" -> [{label}] status={u_res['status']} | type={u_res.get('input_type')} "
                f"| 反查商品={prod.get('name', '')[:18]} (¥{prod.get('price')}) | 新口令={promo.get('tpwd')}"
            )

        u_bad = convert_and_resolve_universal_input("https://item.taobao.com/item.htm?id=654321098765", relation_id=None)
        print(
            f" -> [7l. 纯数字无标题直链拦截验证] status={u_bad['status']} | code={u_bad.get('error_code')} "
            f"| msg={u_bad.get('error_msg', '')[:38]}..."
        )


if __name__ == "__main__":
    main()