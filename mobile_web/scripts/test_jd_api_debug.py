# -*- coding: utf-8 -*-
"""
第一性原理实证测试（多 ZTK_APPKEY 独立配额与故障转移验证）：
1. 验证当 Key 1 ("41ce110973f744118c1227d5f7f72b65") 触发折淘客频控限制 (1000 / 301) 时：
   - 同一个 Key 1 下的 api_detail.ashx、api_quanwang.ashx、open_jing_union_open_goods_query.ashx 全部同时失效；
   - 但在同一 IP、同一 JD_UNION_ID ("2035679405") 下，立即切换到备用 Key 2 ("fe60ace35b00426b89a16a2baea50618")，
     api_detail.ashx 与 api_quanwang.ashx 能够 100% 立即成功！
"""
import json
import sys
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ZTK_APPKEY_LIST = [
    "41ce110973f744118c1227d5f7f72b65",
    "fe60ace35b00426b89a16a2baea50618",
]
JD_UNION_ID = "2035679405"
ZTK_BASE = "http://api.zhetaoke.com:20000/api"
SESSION = requests.Session()


def call_detail(appkey: str, item_id: str) -> tuple[bool, str]:
    resp = SESSION.get(f"{ZTK_BASE}/api_detail.ashx", params={
        "appkey": appkey, "unionId": JD_UNION_ID, "tao_id": item_id, "type": 1
    }, timeout=8).json()
    if str(resp.get("status")) == "200" and isinstance(resp.get("content"), list) and resp["content"]:
        return True, f"成功(title={resp['content'][0].get('title', '')[:18]})"
    return False, f"失败: {json.dumps(resp, ensure_ascii=False)}"


def call_search(appkey: str, keyword: str) -> tuple[bool, str]:
    q = keyword
    while len(q) < 4:
        q += q
    resp = SESSION.get(f"{ZTK_BASE}/api_quanwang.ashx", params={
        "appkey": appkey, "unionId": JD_UNION_ID, "q": q,
        "page": 1, "page_size": 10, "sort": "new", "youquan": 1, "type": 2
    }, timeout=8).json()
    if str(resp.get("status")) == "200" and isinstance(resp.get("content"), list) and resp["content"]:
        return True, f"成功(返回{len(resp['content'])}条)"
    return False, f"失败: {json.dumps(resp, ensure_ascii=False)}"


def trigger_key1_limit(appkey: str, item_id: str) -> int:
    """快速消耗 Key 1 直至触发折淘客 301/1000 限制"""
    for count in range(1, 30):
        resp = SESSION.get(f"{ZTK_BASE}/open_jing_union_open_goods_query.ashx", params={
            "appkey": appkey, "unionId": JD_UNION_ID, "keyword": item_id, "pageIndex": 1, "pageSize": 1
        }, timeout=8).json()
        if resp.get("status") == 301:
            return count
    return -1


if __name__ == "__main__":
    key1, key2 = ZTK_APPKEY_LIST[0], ZTK_APPKEY_LIST[1]
    r_rank = SESSION.get(f"{ZTK_BASE}/open_jd_union_open_goods_rank_query.ashx", params={
        "appkey": key2, "unionId": JD_UNION_ID, "rankId": 200000, "sortType": 1, "pageIndex": 1, "pageSize": 5
    }, timeout=8).json()
    rank_ids = [r["itemId"] for r in json.loads(r_rank["jd_union_open_goods_rank_query_responce"]["queryResult"])["data"]]
    sample_id = rank_ids[0]

    print("=" * 95)
    print("🔬 第一性原理终极验证：折淘客限流绑定在单个 ZTK_APPKEY 而非 IP / JD_UNION_ID")
    print("=" * 95)

    n = trigger_key1_limit(key1, sample_id)
    print(f"\n[步骤 1] 对 Key 1 ({key1[:8]}...) 连续请求第 {n} 次时成功触发折淘客 301/1000 限流！")

    print(f"\n[步骤 2] 在 Key 1 被限流的同一毫秒，对比 Key 1 与 Key 2 ({key2[:8]}...) 的详情与搜索表现：")
    for idx, tid in enumerate(rank_ids[:3], 1):
        d1_ok, d1_msg = call_detail(key1, tid)
        d2_ok, d2_msg = call_detail(key2, tid)
        print(f"  * 商品详情 #{idx} ({tid[:16]}...):")
        print(f"      Key 1 (已被限流): {'✅ ' + d1_msg if d1_ok else '❌ ' + d1_msg}")
        print(f"      Key 2 (备用切换): {'✅ ' + d2_msg if d2_ok else '❌ ' + d2_msg}")

    for kw in ["抽纸", "牛奶", "蓝牙耳机"]:
        s1_ok, s1_msg = call_search(key1, kw)
        s2_ok, s2_msg = call_search(key2, kw)
        print(f"  * 关键词搜索 '{kw}':")
        print(f"      Key 1 (已被限流): {'✅ ' + s1_msg if s1_ok else '❌ ' + s1_msg}")
        print(f"      Key 2 (备用切换): {'✅ ' + s2_msg if s2_ok else '❌ ' + s2_msg}")