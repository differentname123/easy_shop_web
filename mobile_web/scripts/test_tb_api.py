# -*- coding: utf-8 -*-
import hashlib
from datetime import datetime, timedelta
import requests

# ==================== 1. 核心固定配置 ====================
TB_APP_KEY = "35288611"
TB_APP_SECRET = "3f861aab50cd92d29870becf34d6fc64"
TB_SESSION = "610132387587dff525150c2b1ff43ed157529fa8ac9e9373365477897"

TB_PID_PREFIX = "mm_328750149_3161250458"
TB_CHANNEL_ADZONE_ID = "116343300219"   # 渠道专属推广位
TB_REGULAR_ADZONE_ID = "116341500041"   # 常规自营推广位
DEFAULT_RELATION_ID = "3400251845"      # 测试推手关系ID (t**0)
TB_INVITER_CODE = "VK6WGA"              # 官方渠道邀请码

HDK_API_KEY = "9D3E0E43E5FD"
ZTK_APPKEY = "41ce110973f744118c1227d5f7f72b65"
ZTK_SID = "192646"

TOP_URL = "https://eco.taobao.com/router/rest"
ZTK_CHANNEL_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"


# ==================== 2. 基础工具函数 ====================
def call_tb_api(method: str, biz_params: dict, session: str = None) -> dict:
    """淘宝开放平台标准 MD5 签名调用"""
    params = {
        "method": method,
        "app_key": TB_APP_KEY,
        "session": session or TB_SESSION,
        "v": "2.0",
        "format": "json",
        "sign_method": "md5",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **{k: str(v) for k, v in biz_params.items() if v is not None}
    }
    sign_str = TB_APP_SECRET + "".join(f"{k}{v}" for k, v in sorted(params.items())) + TB_APP_SECRET
    params["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()
    return requests.post(TOP_URL, data=params, timeout=10).json()


def _parse_item(raw: dict) -> dict:
    """解析淘宝官方商品结构"""
    b = raw.get("item_basic_info", {})
    p = raw.get("price_promotion_info", {})
    pub = raw.get("publish_info", {})
    inc = pub.get("income_info", {})

    promo_url = pub.get("coupon_share_url") or pub.get("click_url") or ""
    if promo_url.startswith("//"):
        promo_url = "https:" + promo_url

    item_id = raw.get("item_id") or raw.get("input_item_iid") or ""
    return {
        "item_id": item_id,
        "title": b.get("title", ""),
        "shop_title": b.get("shop_title", ""),
        "is_tmall": b.get("user_type") == 1,
        "final_price": str(p.get("final_promotion_price") or p.get("zk_final_price") or "0.00"),
        "origin_price": str(p.get("zk_final_price") or p.get("reserve_price") or "0.00"),
        "commission_rate": str(pub.get("income_rate") or inc.get("commission_rate") or "0"),
        "commission_amount": str(pub.get("commission_amount") or inc.get("commission_amount") or "0.00"),
        "item_url": f"https://uland.taobao.com/item/edetail?id={item_id}",
        "target_url": promo_url,
        "small_images": b.get("small_images", {}).get("string", [])
    }


def _create_tpwd(title: str, url: str) -> str:
    """调用官方接口生成淘口令"""
    res = call_tb_api("taobao.tbk.tpwd.create", {"text": title[:20] or "推荐好物", "url": url})
    return res.get("tbk_tpwd_create_response", {}).get("data", {}).get("model", "")


# ==================== 3. 五大核心业务功能 ====================

# 功能 1: 2小时热推榜 (好单库选品 + 淘宝官方核准)
def get_hot_rank_goods(sale_type=1, page_size=3) -> list:
    hdk_url = f"https://v2.api.haodanku.com/sales_list?apikey={HDK_API_KEY}&sale_type={sale_type}&cid=0&back=10&min_id=1"
    hdk_list = requests.get(hdk_url, timeout=8).json().get("data", [])[:page_size]
    item_ids = [str(g["itemid"]) for g in hdk_list if g.get("itemid")]
    if not item_ids:
        return []

    res = call_tb_api("taobao.tbk.item.info.upgrade.get", {"item_id": ",".join(item_ids)})
    details = res.get("tbk_item_info_upgrade_get_response", {}).get("results", {}).get("tbk_item_detail", [])
    official_map = {str(d.get("input_item_iid") or d.get("item_id")): d for d in details}

    result = []
    for idx, hdk in enumerate(hdk_list, 1):
        iid = str(hdk["itemid"])
        card = _parse_item(official_map.get(iid, {}))
        card.update({
            "rank": idx,
            "two_hour_sales": int(hdk.get("itemsale2", 0)),
            "hdk_price": str(hdk.get("itemendprice", "")),
            "hdk_rate": str(hdk.get("tkrates", "")),
        })
        result.append(card)
    return result


# 功能 2: 推广转链 (无渠道走官方 API / 有渠道直接走折淘客 API)
def convert_promotion_url(item_url: str, is_channel: bool = False, relation_id: str = DEFAULT_RELATION_ID) -> dict:
    if not is_channel:
        # 途径 A: 无渠道普通推广 -> 直接调淘宝官方 API
        res = call_tb_api("taobao.tbk.dg.material.optional.upgrade", {
            "adzone_id": TB_REGULAR_ADZONE_ID,
            "q": item_url,
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
        "tkl": item_url,
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


# 功能 3: 官方商品详情查询
def get_goods_detail(item_id: str) -> dict:
    res = call_tb_api("taobao.tbk.item.info.upgrade.get", {"item_id": item_id})
    details = res.get("tbk_item_info_upgrade_get_response", {}).get("results", {}).get("tbk_item_detail", [])
    return _parse_item(details[0]) if details else {"error": f"未查询到详情: {res}"}


# 功能 4.1: 官方推手渠道备案反查 (通过系统 user_id 反查绑定的 relation_id)
def check_publisher_relation_by_user(user_id: str, session: str = None) -> dict:
    res = call_tb_api("taobao.tbk.sc.publisher.info.get", {
        "relation_app": "common",
        "info_type": 1,
        "external_id": str(user_id)
    }, session=session)

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


# 功能 4.2: 防重复授权与专属授权链接生成
def generate_authority_url(user_id: str, session: str = None) -> dict:
    chk = check_publisher_relation_by_user(user_id, session)
    if chk.get("is_authorized"):
        return {
            "status": "EXISTING_RELATION",
            "is_authorized": True,
            "relation_id": chk["relation_id"],
            "account_name": chk.get("account_name"),
            "create_date": chk.get("create_date"),
            "auth_url": "",
            "msg": f"【防重复拦截】推手 {user_id} 已备案，渠道ID为 {chk['relation_id']}，无需重复授权！"
        }

    code_res = call_tb_api("taobao.tbk.sc.invitecode.get", {"relation_app": "common", "code_type": 1}, session=session)
    code = code_res.get("tbk_sc_invitecode_get_response", {}).get("data", {}).get("inviter_code") or TB_INVITER_CODE
    return {
        "status": "SUCCESS",
        "is_authorized": False,
        "inviter_code": code,
        "auth_url": f"https://mos.m.taobao.com/inviter/register?inviterCode={code}&src=pub&app=common&externalId={user_id}",
        "external_id": str(user_id),
        "msg": f"专属推手授权链接已生成 (externalId={user_id})"
    }


# 功能 4.3: 授权完成后主动识别并提取最终渠道 ID (闭环落库函数)
def sync_publisher_after_auth(user_id: str, session: str = None) -> dict:
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


# 功能 5: 渠道真实订单查询 (单次查询 <= 20分钟)
def fetch_increment_orders_strictly(minutes: int = 20) -> list:
    now = datetime.now()
    resp = requests.get(ZTK_ORDER_URL, params={
        "appkey": ZTK_APPKEY,
        "sid": ZTK_SID,
        "start_time": (now - timedelta(minutes=min(minutes, 20))).strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "query_type": "4",
        "order_scene": "2",  # 2 = 渠道订单池
        "page_no": 1,
        "page_size": 50,
        "signurl": "1"
    }, timeout=10).json()
    body = resp.get("tbk_sc_order_details_get_response") or resp.get("tbk_order_details_get_response", {})
    return body.get("data", {}).get("results", {}).get("publisher_order_dto", []) or []


# ==================== 4. 主流程验证 ====================
if __name__ == "__main__":
    print("=" * 70)
    print("1. 拉取【2小时热推榜】前 3 名商品:")
    hot_goods = get_hot_rank_goods(sale_type=1, page_size=3)
    for g in hot_goods:
        print(f"  [{g['rank']}] {g['title'][:22]}... | 到手: ¥{g['final_price']} | 佣金: ¥{g['commission_amount']} | ID: {g['item_id']}")

    first = hot_goods[0]
    print(f"\n🎯 锁定榜首商品 URL: {first['item_url']}")

    print("\n" + "=" * 70)
    print("2. 推广转链对比 (模式A 官方无渠道 vs 模式B 折淘客有渠道):")
    cp_reg = convert_promotion_url(first["item_url"], is_channel=False)
    print(f"👉 [模式A 无渠道] 推广位: {cp_reg.get('adzone_id')} | 淘口令: {cp_reg.get('tpwd')}")
    print(f"   落地页: {cp_reg.get('target_url')}")

    cp_chl = convert_promotion_url(first["item_url"], is_channel=True, relation_id=DEFAULT_RELATION_ID)
    print(f"\n👉 [模式B 有渠道] 推广位: {cp_chl.get('adzone_id')} | relation_id: {cp_chl.get('relation_id')} | 淘口令: {cp_chl.get('tpwd')}")
    print(f"   落地页: {cp_chl.get('target_url')}")

    print("\n" + "=" * 70)
    print("3. 官方商品详情查询:")
    detail = get_goods_detail(first["item_id"])
    print(f"  店铺: {detail.get('shop_title')} | 现价: ¥{detail.get('final_price')} | 主图数: {len(detail.get('small_images', []))} 张")

    print("\n" + "=" * 70)
    print("4. 推手渠道授权与授权后反查闭环 (测试用户 user_8899):")
    auth = generate_authority_url("user_8899")
    print(f"  [授权前生成] 状态: {auth['status']} | 授权链接: {auth['auth_url']} | 说明: {auth['msg']}")
    sync_res = sync_publisher_after_auth("user_8899")
    print(f"  [授权后反查] 成功: {sync_res['success']} | 最终渠道ID: {sync_res['final_relation_id']} | 说明: {sync_res['msg']}")

    print("\n" + "=" * 70)
    print("5. 最近 20 分钟渠道订单查询 (scene=2):")
    orders = fetch_increment_orders_strictly(minutes=20)
    print(f"  共查到 {len(orders)} 笔渠道订单")
    for o in orders:
        print(f"  * 订单号: {o.get('trade_id')} | 实付: ¥{o.get('alipay_total_price')} | relation_id: {o.get('relation_id')}")
    print("=" * 70)