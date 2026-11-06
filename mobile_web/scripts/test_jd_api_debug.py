# -*- coding: utf-8 -*-
"""
第一性原理实证测试脚本：
验证折淘客京东“老接口 (api_quanwang / api_detail)”与“官方协议透传接口 (open_jing_union_open_goods_query / bigfield)”
在连续调用触发限流时的真实表现：
1. 从成功状态开始连续调用老接口，观测第几次触发失败；
2. 触发失败的同一毫秒，立即调用透传接口 (goods.query / bigfield)，验证透传是否真有独立额度、能否救活请求。
"""
import json
import os
import sys
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import settings

ZTK_APPKEY = settings.ztk_appkey
JD_UNION_ID = settings.jd_union_id
ZTK_BASE = "http://api.zhetaoke.com:20000/api"
SESSION = requests.Session()


def call_old_search(keyword: str) -> tuple[bool, str]:
    q = keyword
    while len(q) < 4:
        q += q
    resp = SESSION.get(f"{ZTK_BASE}/api_quanwang.ashx", params={
        "appkey": ZTK_APPKEY, "unionId": JD_UNION_ID, "q": q,
        "page": 1, "page_size": 10, "sort": "new", "youquan": 1, "type": 2
    }, timeout=8)
    data = resp.json()
    if str(data.get("status")) == "200" and isinstance(data.get("content"), list) and data["content"]:
        return True, f"成功(返回{len(data['content'])}条)"
    return False, f"失败: {json.dumps(data, ensure_ascii=False)}"


def call_passthrough_search(keyword: str) -> tuple[bool, str]:
    resp = SESSION.get(f"{ZTK_BASE}/open_jing_union_open_goods_query.ashx", params={
        "appkey": ZTK_APPKEY, "unionId": JD_UNION_ID, "keyword": keyword,
        "pageIndex": 1, "pageSize": 10
    }, timeout=8)
    data = resp.json()
    root = data.get("jd_union_open_goods_query_response") or data.get("jd_union_open_goods_query_responce")
    if isinstance(root, dict):
        raw = root.get("result") or root.get("queryResult") or "{}"
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if str(parsed.get("code")) == "200" and parsed.get("data"):
            return True, f"成功(code=200, 返回{len(parsed['data'])}条)"
        return False, f"业务失败: {json.dumps(parsed, ensure_ascii=False)}"
    return False, f"失败: {json.dumps(data, ensure_ascii=False)}"


def call_old_detail(item_id: str) -> tuple[bool, str]:
    resp = SESSION.get(f"{ZTK_BASE}/api_detail.ashx", params={
        "appkey": ZTK_APPKEY, "unionId": JD_UNION_ID, "tao_id": item_id, "type": 1
    }, timeout=8)
    data = resp.json()
    if str(data.get("status")) == "200" and isinstance(data.get("content"), list) and data["content"]:
        return True, f"成功(title={data['content'][0].get('title', '')[:16]})"
    return False, f"失败: {json.dumps(data, ensure_ascii=False)}"


def call_passthrough_detail_goods_query(item_id: str) -> tuple[bool, str]:
    resp = SESSION.get(f"{ZTK_BASE}/open_jing_union_open_goods_query.ashx", params={
        "appkey": ZTK_APPKEY, "unionId": JD_UNION_ID, "keyword": item_id,
        "pageIndex": 1, "pageSize": 1
    }, timeout=8)
    data = resp.json()
    root = data.get("jd_union_open_goods_query_response") or data.get("jd_union_open_goods_query_responce")
    if isinstance(root, dict):
        raw = root.get("result") or root.get("queryResult") or "{}"
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if str(parsed.get("code")) == "200" and parsed.get("data"):
            return True, f"成功(skuName={parsed['data'][0].get('skuName', '')[:16]})"
        return False, f"业务失败: {json.dumps(parsed, ensure_ascii=False)}"
    return False, f"失败: {json.dumps(data, ensure_ascii=False)}"


def call_passthrough_detail_bigfield(item_id: str) -> tuple[bool, str]:
    resp = SESSION.get(f"{ZTK_BASE}/open_jd_union_open_goods_bigfield_query.ashx", params={
        "appkey": ZTK_APPKEY, "unionId": JD_UNION_ID, "content": item_id
    }, timeout=8)
    data = resp.json()
    root = data.get("jd_union_open_goods_bigfield_query_response") or data.get("jd_union_open_goods_bigfield_query_responce")
    if isinstance(root, dict):
        raw = root.get("result") or root.get("queryResult") or "{}"
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if str(parsed.get("code")) == "200" and parsed.get("data"):
            return True, f"成功(mainSkuId={parsed['data'][0].get('mainSkuId')}, 轮播图={len((parsed['data'][0].get('imageInfo') or {}).get('imageList') or [])}张)"
        return False, f"业务失败: {json.dumps(parsed, ensure_ascii=False)}"
    return False, f"失败: {json.dumps(data, ensure_ascii=False)}"


if __name__ == "__main__":
    r_rank = SESSION.get(f"{ZTK_BASE}/open_jd_union_open_goods_rank_query.ashx", params={
        "appkey": ZTK_APPKEY, "unionId": JD_UNION_ID, "rankId": 200000, "sortType": 1, "pageIndex": 1, "pageSize": 10
    }, timeout=8).json()
    ztk_ids = [r["itemId"] for r in json.loads(r_rank["jd_union_open_goods_rank_query_responce"]["queryResult"])["data"]]

    print("=" * 95)
    print("【实验 A】连续调用老详情接口 (api_detail.ashx) 直到触发失败，失败瞬间测试透传接口 (goods.query & bigfield)")
    print("=" * 95)
    for i in range(1, 26):
        tid = ztk_ids[(i - 1) % len(ztk_ids)]
        ok, msg = call_old_detail(tid)
        print(f"  [第{i:02d}次] 老详情(api_detail): {'✅ ' + msg if ok else '❌ ' + msg}")
        if not ok:
            pt_ok, pt_msg = call_passthrough_detail_goods_query(tid)
            bf_ok, bf_msg = call_passthrough_detail_bigfield(tid)
            print(f"           ↳ 立即测试【透传 goods.query】: {'✅ ' + pt_msg if pt_ok else '❌ ' + pt_msg}")
            print(f"           ↳ 立即测试【透传 bigfield】   : {'✅ ' + bf_msg if bf_ok else '❌ ' + bf_msg}")
            break

    print("\n" + "=" * 95)
    print("【实验 B】连续调用老搜索接口 (api_quanwang.ashx, page=1) 直到触发失败，失败瞬间测试透传搜索 (goods.query)")
    print("=" * 95)
    kws = ["抽纸", "牛奶", "蓝牙耳机", "洗发水套装", "智能手机", "洗衣液", "充电宝", "保温杯", "纸巾", "零食大礼包"]
    for i in range(1, 26):
        kw = kws[(i - 1) % len(kws)]
        ok, msg = call_old_search(kw)
        print(f"  [第{i:02d}次] 老搜索(api_quanwang, kw='{kw}'): {'✅ ' + msg if ok else '❌ ' + msg}")
        if not ok:
            pt_ok, pt_msg = call_passthrough_search(kw)
            print(f"           ↳ 立即测试【透传搜索 goods.query】: {'✅ ' + pt_msg if pt_ok else '❌ ' + pt_msg}")
            break