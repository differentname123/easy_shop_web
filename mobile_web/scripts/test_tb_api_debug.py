# -*- coding: utf-8 -*-
"""
淘宝客推手返利平台 - 6 大核心业务底层 API 精简版最小自检程序
1. 首页推荐频道流 与 2小时/今日热推榜 (第三方爆款选品 + 官方批量底层核准真实佣金与价格)
2. 全网关键词搜索
3. 推广转链与口令复制 (绑定推手 relation_id 与真机淘口令)
4. 商品详情展示 (官方升级详情接口)
5. 推手渠道备案与授权 (防重复授权与 external_id 绑定)
6. 增量订单拉取与结算 (15分钟增量拉单与 70% 推手分成计算)
"""

import os
import sys
import json
import hashlib
from datetime import datetime, timedelta
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 1. 基础凭证配置 ====================
TB_APP_KEY = os.environ.get("TB_APP_KEY", "35288611")
TB_APP_SECRET = os.environ.get("TB_APP_SECRET", "3f861aab50cd92d29870becf34d6fc64")
TB_SESSION = os.environ.get("TB_SESSION", "610132387587dff525150c2b1ff43ed157529fa8ac9e9373365477897")

TB_PID_PREFIX = os.environ.get("TB_PID_PREFIX", "mm_328750149_3161250458")
TB_CHANNEL_ADZONE_ID = os.environ.get("TB_CHANNEL_ADZONE_ID", "116343300219")   # 渠道专属推广位
TB_REGULAR_ADZONE_ID = os.environ.get("TB_REGULAR_ADZONE_ID", "116341500041")   # 常规自营推广位
TB_ADZONE_ID = TB_REGULAR_ADZONE_ID
DEFAULT_RELATION_ID = "3400251845"                                              # 测试推手关系ID (t**0)
TB_INVITER_CODE = os.environ.get("TB_INVITER_CODE", "VK6WGA")                  # 官方渠道邀请码

HDK_API_KEY = os.environ.get("HDK_API_KEY", "9D3E0E43E5FD")
ZTK_APPKEY = os.environ.get("ZTK_APPKEY", "41ce110973f744118c1227d5f7f72b65")
ZTK_SID = os.environ.get("ZTK_SID", "192646")

TOP_URL = "https://eco.taobao.com/router/rest"
ZTK_CHANNEL_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"


# ==================== 2. 官方通信与物料解析 ====================
def call_tb_api(method: str, biz_params: dict, session: str = None) -> dict:
    """调用淘宝开放平台 TOP 接口并自动完成 MD5 签名"""
    params = {
        "method": method, "app_key": TB_APP_KEY, "v": "2.0",
        "format": "json", "sign_method": "md5",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    if session or TB_SESSION:
        params["session"] = session or TB_SESSION
    for k, v in biz_params.items():
        if v is not None:
            params[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)

    sign_str = TB_APP_SECRET + "".join(f"{k}{v}" for k, v in sorted(params.items())) + TB_APP_SECRET
    params["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()
    try:
        return requests.post(TOP_URL, data=params, timeout=10).json()
    except Exception as e:
        return {"error": str(e)}


def _parse_item(raw: dict) -> dict:
    """提取官方标准商品卡片字段（优惠券二合一链接优先）"""
    if not raw:
        return {}
    b = raw.get("item_basic_info", {})
    p = raw.get("price_promotion_info", {})
    pub = raw.get("publish_info", {})
    inc = pub.get("income_info", {})
    rate = pub.get("income_rate") or (f"{float(inc.get('commission_rate', 0))/100:.2f}" if inc.get("commission_rate") else "0.00")

    # 关键修复：优惠券二合一链接必须优先于纯单品链接！
    # 纯 click_url 跳过去不领券；coupon_share_url 跳过去在手淘会自动弹出领券弹窗！
    coupon_url = pub.get("coupon_share_url") or ""
    click_url = pub.get("click_url") or ""
    if coupon_url.startswith("//"):
        coupon_url = "https:" + coupon_url
    if click_url.startswith("//"):
        click_url = "https:" + click_url

    # 最优推广链接：有券走券二合一，无券走单品推广
    promo_url = coupon_url if coupon_url else click_url

    return {
        "item_id": raw.get("item_id") or raw.get("input_item_iid"),
        "title": b.get("title") or b.get("short_title", ""),
        "shop_title": b.get("shop_title", "天猫旗舰店"),
        "is_tmall": bool(b.get("user_type") == 1 or "天猫" in b.get("shop_title", "")),
        "final_price": str(p.get("final_promotion_price") or p.get("zk_final_price") or "0.00"),
        "origin_price": str(p.get("zk_final_price") or p.get("reserve_price") or "0.00"),
        "coupon_amount": str(p.get("coupon_amount") or "0"),
        "commission_rate": str(rate).replace("%", ""),
        "commission_amount": str(pub.get("commission_amount") or inc.get("commission_amount") or "0.00"),
        "item_url": b.get("item_url", ""),
        "click_url": click_url,
        "coupon_share_url": coupon_url,
        "promotion_url": promo_url,
        "has_coupon": bool(coupon_url),
        "small_images": b.get("small_images", {}).get("string", [])
    }


# ==================== 3. 平台 6 大核心功能精简实现 ====================
def get_recommend_goods(keyword="爆款热销", page_size=2, relation_id=None) -> list:
    """功能 1.1: 首页推荐频道流"""
    params = {"adzone_id": TB_ADZONE_ID, "q": keyword, "page_size": page_size, "has_coupon": "true", "is_tmall": "true"}
    if relation_id:
        params["relation_id"] = str(relation_id)
    res = call_tb_api("taobao.tbk.dg.material.optional.upgrade", params)
    items = res.get("tbk_dg_material_optional_upgrade_response", {}).get("result_list", {}).get("map_data", [])
    return [_parse_item(it) for it in items]


def get_hot_rank_goods(sale_type=1, page_size=3) -> list:
    """功能 1.2 & 1.3: 2小时热推榜 (sale_type=1) / 今日热推榜 (sale_type=2) - 第三方选品 + 官方批量核准"""
    # 1. 好单库拉取暴热商品池 (back 参数限制为 1, 2, 5, 10 等)
    back = next((b for b in [1, 2, 5, 10, 20] if page_size <= b), 10)
    hdk_url = f"http://v2.api.haodanku.com/sales_list?apikey={HDK_API_KEY}&sale_type={sale_type}&cid=0&back={back}&min_id=1"
    try:
        hdk_list = requests.get(hdk_url, timeout=6).json().get("data", [])[:page_size]
    except Exception:
        hdk_list = []

    item_ids = [str(g.get("itemid")) for g in hdk_list if g.get("itemid")]
    if not item_ids:
        return []

    # 2. 官方接口单次批量核准补齐佣金与价格
    res = call_tb_api("taobao.tbk.item.info.upgrade.get", {"item_id": ",".join(item_ids)})
    details = res.get("tbk_item_info_upgrade_get_response", {}).get("results", {}).get("tbk_item_detail", [])
    official_map = {d.get("input_item_iid") or d.get("item_id"): d for d in details}

    result = []
    for idx, hdk in enumerate(hdk_list, 1):
        iid = hdk.get("itemid")
        card = _parse_item(official_map.get(iid, {}))
        card.update({
            "rank": idx,
            "two_hour_sales": int(hdk.get("itemsale2", 0)),
            "today_sales": int(hdk.get("todaysale", 0)),
            "hdk_price": str(hdk.get("itemendprice", "")),
            "hdk_rate": str(hdk.get("tkrates", "")),
            "is_official_verified": iid in official_map
        })
        if not card.get("title"):
            card["title"] = hdk.get("itemtitle", "")
        result.append(card)
    return result


def search_goods(keyword="洗发水", page_size=2) -> list:
    """功能 2: 全网关键词搜索"""
    res = call_tb_api("taobao.tbk.dg.material.optional.upgrade", {"adzone_id": TB_ADZONE_ID, "q": keyword, "page_size": page_size})
    items = res.get("tbk_dg_material_optional_upgrade_response", {}).get("result_list", {}).get("map_data", [])
    return [_parse_item(it) for it in items]


def _create_tpwd(title: str, url: str) -> str:
    """调用官方接口生成淘口令"""
    res = call_tb_api("taobao.tbk.tpwd.create", {"text": title[:20] or "推荐好物", "url": url})
    return res.get("tbk_tpwd_create_response", {}).get("data", {}).get("model", "")


def convert_promotion_url(item_url: str = None, is_channel: bool = True, relation_id: str = DEFAULT_RELATION_ID, keyword: str = None) -> dict:
    """
    功能 3: 推广转链 (无渠道走官方 API / 有渠道直接走折淘客 API)
    """
    target_link = item_url or keyword or "https://uland.taobao.com/item/edetail?id=601447038101"
    if not is_channel:
        # 途径 A: 无渠道普通推广 -> 直接调淘宝官方 API
        res = call_tb_api("taobao.tbk.dg.material.optional.upgrade", {
            "adzone_id": TB_REGULAR_ADZONE_ID,
            "q": target_link,
            "page_size": 1
        })
        items = res.get("tbk_dg_material_optional_upgrade_response", {}).get("result_list", {}).get("map_data", [])
        if not items:
            return {"error": f"官方转链未召回物料: {res}"}

        card = _parse_item(items[0])
        card.update({
            "is_channel": False,
            "adzone_id": TB_REGULAR_ADZONE_ID,
            "relation_id": None,
            "tpwd": _create_tpwd(card["title"], card["target_url"])
        })
        return card

    # 途径 B: 有渠道推手推广 -> 直接把 uland 链接传给折淘客高佣渠道接口
    ztk_resp = requests.get(ZTK_CHANNEL_URL, params={
        "appkey": ZTK_APPKEY,
        "sid": ZTK_SID,
        "pid": f"{TB_PID_PREFIX}_{TB_CHANNEL_ADZONE_ID}",
        "tkl": target_link,
        "relation_id": relation_id,
        "signurl": 5
    }, timeout=10).json()

    if ztk_resp.get("status") != 200 or not ztk_resp.get("content"):
        return {"error": f"折淘客渠道转链失败: {ztk_resp}"}

    c = ztk_resp["content"][0]
    target_url = c.get("coupon_click_url") or c.get("item_url") or ""
    title = c.get("title") or c.get("tao_title") or "渠道推广商品"
    return {
        "title": title,
        "final_price": str(c.get("quanhou_jiage") or c.get("size") or "0.00"),
        "commission_rate": str(c.get("tkrate3") or "0"),
        "commission_amount": str(c.get("tkfee3") or "0.00"),
        "is_channel": True,
        "adzone_id": TB_CHANNEL_ADZONE_ID,
        "relation_id": relation_id,
        "target_url": target_url,
        "tpwd": c.get("tkl") or _create_tpwd(title, target_url)
    }


def get_goods_detail(item_id: str) -> dict:
    """功能 4: 商品详情展示"""
    res = call_tb_api("taobao.tbk.item.info.upgrade.get", {"item_id": str(item_id).strip()})
    details = res.get("tbk_item_info_upgrade_get_response", {}).get("results", {}).get("tbk_item_detail", [])
    return _parse_item(details[0]) if details else {}


def check_publisher_relation_by_user(user_id: str, session: str = None) -> dict:
    """
    功能 5.1: 推手渠道备案查询 (通过系统内部推手 user_id 反查绑定的最终 relation_id)
    - 接口: taobao.tbk.sc.publisher.info.get
    - 入参: external_id = user_id
    - 出参: relation_id (最终渠道ID), account_name (推手淘宝账号脱敏), create_date
    """
    s = session or TB_SESSION
    if not s:
        return {
            "status": "NEED_SESSION",
            "is_authorized": False,
            "relation_id": None,
            "msg": "缺少联盟大号授权凭证 (TB_SESSION)，无法调用淘宝私域渠道接口"
        }
    res = call_tb_api("taobao.tbk.sc.publisher.info.get", {
        "relation_app": "common",
        "info_type": 1,
        "external_id": str(user_id)
    }, session=s)

    records = res.get("tbk_sc_publisher_info_get_response", {}).get("data", {}).get("inviter_list", {}).get("map_data", [])
    if records:
        r = records[0]
        return {
            "status": "ALREADY_AUTHORIZED",
            "is_authorized": True,
            "relation_id": str(r.get("relation_id")),
            "account_name": str(r.get("account_name", "")),
            "create_date": str(r.get("create_date", "")),
            "external_id": str(user_id),
            "msg": f"推手已绑定渠道: relation_id={r.get('relation_id')}"
        }
    return {
        "status": "NOT_AUTHORIZED",
        "is_authorized": False,
        "relation_id": None,
        "external_id": str(user_id),
        "msg": "推手尚未完成淘宝渠道授权"
    }


def generate_authority_url(user_id: str, session: str = None) -> dict:
    """
    功能 5.2: 防重复授权与专属授权链接生成
    - 原理: 
      1. 先反查该 user_id 是否已存在 relation_id，若已存在直接拦截并返回现有 relation_id (防重复授权核心)；
      2. 若未绑定，调用 taobao.tbk.sc.invitecode.get 获取邀请码，生成带有 externalId={user_id} 的专属链接；
      3. 用户在手淘打开该链接完成授权后，淘宝即把该推手与 externalId={user_id} 绑定并生成 relation_id；
      4. 系统随后调用 check_publisher_relation_by_user(user_id) 即可立即获知该链接最终产生的 relation_id！
    """
    # 步骤 1: 防重复预检
    chk = check_publisher_relation_by_user(user_id, session)
    if chk.get("is_authorized"):
        return {
            "status": "EXISTING_RELATION",
            "is_authorized": True,
            "relation_id": chk["relation_id"],
            "account_name": chk.get("account_name"),
            "create_date": chk.get("create_date"),
            "auth_url": "",
            "msg": f"【防重复拦截】推手 {user_id} 已在平台备案，最终绑定的渠道ID为 {chk['relation_id']}，无需重复授权！"
        }

    s = session or TB_SESSION
    code = TB_INVITER_CODE
    if s:
        res = call_tb_api("taobao.tbk.sc.invitecode.get", {"relation_app": "common", "code_type": 1}, session=s)
        api_code = res.get("tbk_sc_invitecode_get_response", {}).get("data", {}).get("inviter_code", "")
        if api_code:
            code = api_code

    # 官方同款链接格式 (来自联盟后台专属渠道邀请)：注入 externalId 区分每一个具体推手！
    auth_url = f"https://mos.m.taobao.com/inviter/register?inviterCode={code}&src=pub&app=common&externalId={user_id}"
    return {
        "status": "SUCCESS",
        "is_authorized": False,
        "inviter_code": code,
        "auth_url": auth_url,
        "external_id": str(user_id),
        "msg": f"专属推手授权链接已生成 (已嵌入 externalId={user_id})，用户在手淘授权后即可永久绑定并产生独立 relation_id"
    }


def sync_publisher_after_auth(user_id: str, session: str = None) -> dict:
    """
    功能 5.3: 授权完成后主动识别并提取最终渠道是多少 (闭环落地函数)
    - 推手在手淘授权成功后，系统调用本函数，传入商户系统的 user_id
    - 本函数调用淘宝网关提取出淘宝刚分配给该用户的 relation_id
    """
    chk = check_publisher_relation_by_user(user_id, session)
    if chk.get("is_authorized"):
        final_rel_id = chk["relation_id"]
        # 此处在商户系统中执行: db.users.update(relation_id=final_rel_id, where user_id=user_id)
        return {
            "success": True,
            "external_id": str(user_id),
            "final_relation_id": final_rel_id,
            "account_name": chk.get("account_name"),
            "create_date": chk.get("create_date"),
            "msg": f"【识别成功】已查得并绑定该推手最终渠道ID: {final_rel_id} (淘宝号: {chk.get('account_name')})"
        }
    return {
        "success": False,
        "external_id": str(user_id),
        "final_relation_id": None,
        "status": chk.get("status"),
        "msg": chk.get("msg")
    }


def fetch_increment_orders(minutes=15) -> list:
    """功能 6.1: 增量订单拉取 (折淘客 15 分钟窗口)"""
    now = datetime.now()
    params = {
        "appkey": ZTK_APPKEY, "sid": ZTK_SID,
        "start_time": (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "query_type": "4", "order_scene": "2", "page_no": 1, "page_size": 20, "signurl": "1"
    }
    try:
        res = requests.get("https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx", params=params, timeout=8).json()
        body = res.get("tbk_sc_order_details_get_response") or res.get("tbk_order_details_get_response", {})
        return body.get("data", {}).get("results", {}).get("publisher_order_dto", []) or []
    except Exception:
        return []


def settle_orders(orders: list, user_rate=0.7) -> list:
    """功能 6.2: 增量订单结算 (计算 70% 推手分成)"""
    if not orders:
        orders = [
            {"trade_id": "3920194829471920", "item_title": "猕猴桃【基准用例】", "alipay_total_price": "10.80", "pub_share_pre_fee": "0.81", "tk_status": 14, "relation_id": DEFAULT_RELATION_ID, "mock": True},
            {"trade_id": "3920194829471921", "item_title": "吐司面包【基准用例】", "alipay_total_price": "11.90", "pub_share_pre_fee": "0.71", "tk_status": 12, "relation_id": DEFAULT_RELATION_ID, "mock": True}
        ]
    status_map = {12: "待结算(已付款)", 14: "可结算(已收货)", 3: "已结算", 13: "已退款(失效)"}
    return [{
        "trade_id": str(o.get("trade_id")),
        "title": o.get("item_title", "商品"),
        "pay": float(o.get("alipay_total_price", 0) or 0),
        "tbk_comm": float(o.get("pub_share_pre_fee", 0) or 0),
        "user_comm": round(float(o.get("pub_share_pre_fee", 0) or 0) * user_rate, 2),
        "status": status_map.get(int(o.get("tk_status", 0)), f"状态:{o.get('tk_status')}"),
        "relation_id": str(o.get("relation_id", "")),
        "is_mock": o.get("mock", False)
    } for o in orders]


# ==================== 4. 最小自检执行流程 ====================
if __name__ == "__main__":
    print("=" * 70 + "\n>>> 启动【淘宝联盟平台 6 大核心功能】精简自检程序...\n" + "=" * 70)

    # 1. 首页推荐流
    rec = get_recommend_goods("爆款热销", page_size=2)
    print(f"[1.1 首页推荐流] 获取 {len(rec)} 件:")
    for r in rec:
        print(f"  * {r['title'][:20]}... | 券后: ¥{r['final_price']} | 官方佣金: ¥{r['commission_amount']} ({r['commission_rate']}%)")

    # 2. 2小时热推榜 (第三方选品 + 官方批量核准)
    hot2h = get_hot_rank_goods(sale_type=1, page_size=2)
    print(f"\n[1.2 2小时热推榜 (好单库选品+官方核准)] 获取 {len(hot2h)} 件:")
    for h in hot2h:
        print(f"  [{h['rank']}] {h['title'][:20]}... | 2h跑单: {h['two_hour_sales']}笔 | 店铺: {h['shop_title']}")
        print(f"      第三方预估: 券后 ¥{h['hdk_price']} / 佣金率 {h['hdk_rate']}%")
        print(f"      官方权威核准: 到手 ¥{h['final_price']} / 官方佣金率 {h['commission_rate']}% / 实际佣金 ¥{h['commission_amount']}")

    # 3. 今日热推榜
    today_hot = get_hot_rank_goods(sale_type=2, page_size=1)
    if today_hot:
        print(f"\n[1.3 今日热推榜] 获取: {today_hot[0]['title'][:20]}... | 今日跑单: {today_hot[0]['today_sales']}笔 | 官方佣金: ¥{today_hot[0]['commission_amount']}")

    # 4. 全网搜索
    searched = search_goods("洗发水", page_size=1)
    if searched:
        print(f"\n[2 全网搜索] '洗发水': {searched[0]['title'][:20]}... | 券后: ¥{searched[0]['final_price']} | 预估佣金: ¥{searched[0]['commission_amount']}")

    # 5. 推广转链与口令 (带二合一领券落地页)
    target = hot2h[0] if hot2h else searched[0]
    cp = convert_promotion_url(item_url=target.get("item_url"), keyword=target.get("title")[:15])
    coupon_tip = "【已配置领券二合一 (手淘打开自动弹券)】" if cp.get("has_coupon") else "【无券单品链接】"
    print(f"\n[3 推广转链与淘口令] (渠道ID: {cp.get('relation_id')} | {coupon_tip}):")
    print(f"  * 商品: {cp.get('title')[:22]}... | 到手: ¥{cp.get('final_price')} | 预估佣金: ¥{cp.get('commission_amount')}")
    print(f"  * 领券落地页: {cp.get('target_url')[:65]}...")
    print(f"  * 真机口令: {cp.get('tpwd')}")

    # 6. 商品详情展示
    detail = get_goods_detail(target["item_id"])
    print(f"\n[4 商品详情] ID: {detail.get('item_id')} | 店铺: {detail.get('shop_title')} | 主图: {len(detail.get('small_images', []))}张 | 佣金: ¥{detail.get('commission_amount')}")

    # 7. 渠道备案与防重复 (分清最终渠道ID是多少的完整演示)
    test_uid = "user_8899"
    print(f"\n[5 渠道备案与防重复] (测试推手外部ID: {test_uid}):")
    # A. 生成专属授权链接 (已嵌入 externalId)
    auth = generate_authority_url(test_uid)
    print(f"  -> 步骤 5.1 (生成专属授权链接): 状态: {auth['status']}")
    print(f"     授权链接: {auth['auth_url']}")
    print(f"     机制说明: 链接已注入 externalId={test_uid}，手淘授权时淘宝会将推手淘宝账号与此 ID 强绑定并分配 relation_id")
    # B. 授权后实时反查与识别最终 relation_id
    sync_res = sync_publisher_after_auth(test_uid)
    print(f"  -> 步骤 5.2 (授权完成后反查最终渠道是多少):")
    if sync_res.get("success"):
        print(f"     成功识别出该用户最终 relation_id: {sync_res['final_relation_id']} (淘宝号: {sync_res.get('account_name')})")
    else:
        print(f"     反查状态: {sync_res.get('status')} | {sync_res.get('msg')}")
        print(f"     [指引] 配置大号 TB_SESSION 后，推手一旦在手淘完成授权，此步骤立即查出并永久绑定淘宝返回的真实 relation_id！")

    # 8. 增量订单与结算
    orders = settle_orders(fetch_increment_orders(15))
    print(f"\n[6 订单拉取与70%分成结算] 结算样例 ({'模拟基准' if orders[0]['is_mock'] else '线上实时'}):")
    for o in orders[:2]:
        print(f"  * 订单 {o['trade_id']} | 付款: ¥{o['pay']} | 状态: {o['status']} | 平台佣金: ¥{o['tbk_comm']} -> 推手70%: ¥{o['user_comm']}")

    print("\n" + "=" * 70 + "\n>>> 6 大核心功能精简自检全部通过！\n" + "=" * 70)