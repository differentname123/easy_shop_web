# -*- coding: utf-8 -*-
"""
淘宝联盟订单底层数据快速反查工具
功能：
  输入任意订单号 (trade_id)，直接从淘宝官方数据网关拉取底层原始字段（包含点击时间、推广位、渠道relation_id、终端类型等）。

用法：
  python mobile_web/scripts/query_tb_order.py <订单编号>
  例如:
  python mobile_web/scripts/query_tb_order.py 3316982665307005362
"""

import sys
import json
import argparse
from datetime import datetime, timedelta
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 折淘客配置 (已授权您的淘宝联盟账号，192646 为 key=1 代理授权)
ZTK_APPKEY = "41ce110973f744118c1227d5f7f72b65"
ZTK_SID = "192646"
ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"


def fetch_slice(start_time: str, end_time: str, scene: str) -> list:
    """拉取单段订单"""
    params = {
        "appkey": ZTK_APPKEY,
        "sid": ZTK_SID,
        "start_time": start_time,
        "end_time": end_time,
        "query_type": "4",       # 4: 订单修改时间，能抓到最新状态变动的订单
        "order_scene": str(scene), # 1: 常规淘客订单, 2: 渠道推广订单
        "page_no": 1,
        "page_size": 50,
        "signurl": "1"
    }
    try:
        res = requests.get(ZTK_ORDER_URL, params=params, timeout=10).json()
        body = res.get("tbk_sc_order_details_get_response") or res.get("tbk_order_details_get_response", {})
        return body.get("data", {}).get("results", {}).get("publisher_order_dto", []) or []
    except Exception as e:
        return []


def query_order_by_id(trade_id: str, minutes: int = 120):
    trade_id = str(trade_id).strip()
    now = datetime.now()
    start = now - timedelta(minutes=minutes)

    # 自动切分 18 分钟安全窗口
    slices = []
    curr = start
    while curr < now:
        nxt = min(curr + timedelta(minutes=18), now)
        slices.append((curr.strftime("%Y-%m-%d %H:%M:%S"), nxt.strftime("%Y-%m-%d %H:%M:%S")))
        curr = nxt

    print("=" * 70)
    print(f">>> 正在从淘宝底层数据网关反查订单: {trade_id}")
    print(f"  * 检索时间范围: 最近 {minutes} 分钟 ({start.strftime('%Y-%m-%d %H:%M:%S')} ~ {now.strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 70)

    matched_order = None
    matched_scene = None

    # 先查渠道订单池 (scene=2)
    for s_start, s_end in slices:
        orders = fetch_slice(s_start, s_end, scene="2")
        for o in orders:
            if str(o.get("trade_id")) == trade_id:
                matched_order = o
                matched_scene = "渠道推广订单 (scene=2, 挂载渠道推手)"
                break
        if matched_order:
            break

    # 若未找到，查常规订单池 (scene=1)
    if not matched_order:
        for s_start, s_end in slices:
            orders = fetch_slice(s_start, s_end, scene="1")
            for o in orders:
                if str(o.get("trade_id")) == trade_id:
                    matched_order = o
                    matched_scene = "常规淘客订单 (scene=1, 未挂载推手)"
                    break
            if matched_order:
                break

    if not matched_order:
        print(f"\n❌ 未在最近 {minutes} 分钟内检索到订单 {trade_id}。")
        print("💡 建议：可指定更长的时间范围重新查询，例如：")
        print(f"   python mobile_web/scripts/query_tb_order.py {trade_id} --minutes 300\n")
        return

    # 状态映射
    status_map = {12: "已付款 (待结算)", 14: "已收货 (可结算)", 3: "已结算成功", 13: "已退款/已关闭"}
    tk_status = int(matched_order.get("tk_status") or 0)
    status_desc = status_map.get(tk_status, f"状态码:{tk_status}")

    print("\n" + "-" * 70)
    print("【📋 订单核心要素解析】")
    print("-" * 70)
    print(f"  * 订单编号 (trade_id)       : {matched_order.get('trade_id')}")
    print(f"  * 所属场景 (order_scene)    : {matched_scene}")
    print(f"  * 商品名称 (item_title)     : {matched_order.get('item_title')}")
    print(f"  * 店铺名称 (seller_shop)    : {matched_order.get('seller_shop_title')}")
    print(f"  * 支付金额 (alipay_price)   : ¥{matched_order.get('alipay_total_price')}")
    print(f"  * 订单状态 (status)         : {status_desc}")
    print(f"  * 推广位ID (adzone_id)      : {matched_order.get('adzone_id')} (推广位名称: {matched_order.get('adzone_name')})")
    print(f"  * 官方佣金 (pub_share_pre)  : ¥{matched_order.get('pub_share_pre_fee')} (提成率: {matched_order.get('total_commission_rate')}%)")
    print(f"  * 渠道推手 (relation_id)    : {matched_order.get('relation_id') or '无 (未绑定推手)'}")
    print(f"  * ★★★ 关键追踪时间 ★★★   :")
    print(f"    - 点击时间 (click_time)   : {matched_order.get('click_time')}")
    print(f"    - 下单时间 (create_time)  : {matched_order.get('tk_create_time')}")
    print(f"    - 付款时间 (paid_time)    : {matched_order.get('tb_paid_time')}")
    print(f"    - 终端类型 (terminal)     : {matched_order.get('terminal_type')}")
    print(f"    - 流量来源 (flow_source)  : {matched_order.get('flow_source')}")

    print("\n" + "-" * 70)
    print("【🔍 淘宝开放平台底层完整原始数据 (JSON)】")
    print("-" * 70)
    print(json.dumps(matched_order, indent=2, ensure_ascii=False))
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="查询指定淘宝订单号的底层详细数据")
    parser.add_argument("order_sn", nargs="?", default="3316999549002014591", help="要查询的淘宝订单号")
    parser.add_argument("--minutes", type=int, default=120, help="查询多少分钟内的订单 (默认 120 分钟)")
    args = parser.parse_args()

    order_sn = args.order_sn.strip()
    if not order_sn:
        try:
            order_sn = input("请输入要查询的淘宝订单号 (trade_id): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not order_sn:
        print("未输入订单号，退出查询。")
        return

    query_order_by_id(order_sn, minutes=args.minutes)


if __name__ == "__main__":
    main()
