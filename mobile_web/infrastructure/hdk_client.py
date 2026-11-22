# -*- coding: utf-8 -*-
"""好单库 (Haodanku) 第三方开放平台统一客户端：
1. 淘宝平台：高佣榜单获取（输出 item_ids 与游标分页元数据）；
2. 京东平台：高配额全网搜索、详情反查及专属转链支持。
"""
import time
from typing import Any, Dict, Optional
import requests

from infrastructure.api_governance import GLOBAL_UPSTREAM_HEALTH, is_real_requests_session
from infrastructure.http_utils import (
    clean_text,
    error_dict as _error,
    execute_with_key_failover,
    parse_appkey_list,
)
from infrastructure.jd_client import resolve_jd_short_url
from infrastructure.logging_utils import get_app_logger
from infrastructure.transport import create_http_session

logger = get_app_logger(__name__)

HDK_SALES_URL = "https://v2.api.haodanku.com/sales_list"
HDK_DEFAULT_API_BASE = "https://v3.api.haodanku.com"
HDK_RATE_LIMIT_KEYWORDS = (
    "频次", "频率", "超限", "上限", "次数", "限制", "频繁", "请稍后", "并发", "额度", "500",
)


class HdkClient:
    """好单库 API 客户端，封装淘宝榜单及京东全网搜索、短链反查与推广转链。"""

    def __init__(
        self,
        api_key: str = "",
        http_session: Any = None,
        timeout: Any = (3.0, 8.0),
        api_key_list: Any = None,
        union_id: str = "",
        api_base: str = HDK_DEFAULT_API_BASE,
    ) -> None:
        self.appkey_list = parse_appkey_list(api_key, api_key_list)
        self.appkey = self.appkey_list[0] if self.appkey_list else clean_text(api_key)
        self.api_key = self.appkey
        self._active_key_index = 0
        self.union_id = clean_text(union_id)
        self.http = http_session if http_session is not None else create_http_session(pool_connections=16, pool_maxsize=32)
        self.timeout = timeout
        self.api_base = clean_text(api_base).rstrip("/") or HDK_DEFAULT_API_BASE

    def get_ranked_ids(self, sale_type, limit=20, cursor=None):
        """淘宝高佣榜单获取 (兼容历史基准)。"""
        if not self.appkey_list:
            return _error("CONFIG_ERROR", "淘宝榜单缺少好单库 API key")
        try:
            sale_type, limit = int(sale_type), int(limit)
            if sale_type not in (1, 2) or not 1 <= limit <= 100:
                raise ValueError
            back = max(size for size in (1, 2, 5, 10, 20) if size <= limit)
            min_id = str((cursor or {}).get("min_id", "1"))
            if not min_id.isdigit() or int(min_id) < 1:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return _error("INVALID_ARGUMENT", "好单库榜单参数或游标无效")

        def _fetch_once(current_key):
            try:
                response = self.http.get(
                    HDK_SALES_URL,
                    params={
                        "apikey": current_key,
                        "sale_type": sale_type,
                        "cid": 0,
                        "back": back,
                        "min_id": min_id,
                    },
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if 300 <= getattr(response, "status_code", 200) < 400:
                    return _error("PROTOCOL_ERROR", "好单库返回不受支持的重定向")
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                body = response.json() if callable(getattr(response, "json", None)) else {}
            except Exception:
                return _error("NETWORK_ERROR", "好单库榜单通信失败")
            if not isinstance(body, dict):
                return _error("PROTOCOL_ERROR", "好单库响应不是对象")
            if str(body.get("code")) == "0" and body.get("data") == [] and str(body.get("min_id")) == "0":
                return {
                    "status": "success",
                    "item_ids": [],
                    "source_count": 0,
                    "has_more": False,
                    "next_cursor": None,
                    "total_count": body.get("total_count"),
                }
            if str(body.get("code")) != "1":
                return _error("UPSTREAM_ERROR", "好单库榜单返回业务错误")
            rows = body.get("data")
            if not isinstance(rows, list):
                return _error("PROTOCOL_ERROR", "好单库响应缺少榜单列表")
            ids = [str(row.get("itemid") or "").strip() if isinstance(row, dict) else "" for row in rows]
            next_id = body.get("min_id")
            explicit_more = body.get("has_more")
            if explicit_more is not None and explicit_more not in (True, False, 0, 1, "0", "1", "true", "false"):
                return _error("PROTOCOL_ERROR", "好单库 has_more 无效")
            has_more = (
                explicit_more in (True, 1, "1", "true")
                if explicit_more is not None
                else bool(rows) and len(rows) >= back
            )
            if next_id is not None and str(next_id) == "0" and explicit_more is None:
                has_more = False
            if back == 20 and next_id is not None and str(next_id).isdigit() and int(next_id) > 5 and explicit_more is None:
                has_more = False
            if has_more and (next_id is None or str(next_id) == min_id or not str(next_id).isdigit() or int(next_id) < 1):
                return _error("PAGINATION_ERROR", "好单库后续页缺少前进游标")
            return {
                "status": "success",
                "item_ids": ids,
                "source_count": len(rows),
                "has_more": has_more,
                "next_cursor": {"min_id": str(next_id)} if has_more else None,
                "total_count": body.get("total_count"),
            }

        res = execute_with_key_failover(self, _fetch_once, round_robin=True)
        self.api_key = self.appkey
        return res

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.appkey_list and not self.api_key:
            return _error("CONFIG_ERROR", "好单库缺少 API Key 配置")
        url = f"{self.api_base}/{path.lstrip('/')}"
        is_real = is_real_requests_session(self.http)
        plat = "jd" if ("jd" in path.lower() or "jing" in path.lower()) else "taobao"

        def _do_single_request(current_key: str) -> Dict[str, Any]:
            t0 = time.monotonic()
            clean_p = {k: v for k, v in (params or {}).items() if v is not None}
            clean_d = {k: v for k, v in (data or {}).items() if v is not None}
            if method.upper() == "POST":
                clean_d.setdefault("apikey", current_key)
            else:
                clean_p.setdefault("apikey", current_key)
            try:
                if method.upper() == "POST":
                    resp = self.http.post(url, params=clean_p or None, data=clean_d or None, timeout=self.timeout)
                else:
                    resp = self.http.get(url, params=clean_p, timeout=self.timeout)
                if hasattr(resp, "raise_for_status"):
                    resp.raise_for_status()
                payload = resp.json() if callable(getattr(resp, "json", None)) else {}
            except requests.exceptions.Timeout:
                dt_ms = (time.monotonic() - t0) * 1000.0
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="error", latency_ms=dt_ms, error_code="TIMEOUT", allow_persist=is_real)
                return _error("TIMEOUT", "好单库接口请求超时")
            except requests.exceptions.RequestException as exc:
                dt_ms = (time.monotonic() - t0) * 1000.0
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="error", latency_ms=dt_ms, error_code="NETWORK_ERROR", allow_persist=is_real)
                return _error("NETWORK_ERROR", f"好单库网络请求失败: {exc}")
            except Exception as exc:
                dt_ms = (time.monotonic() - t0) * 1000.0
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="error", latency_ms=dt_ms, error_code="PROTOCOL_ERROR", allow_persist=is_real)
                return _error("PROTOCOL_ERROR", f"好单库响应解析失败: {exc}")

            dt_ms = (time.monotonic() - t0) * 1000.0
            if not isinstance(payload, dict):
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="error", latency_ms=dt_ms, error_code="PROTOCOL_ERROR", allow_persist=is_real)
                return _error("PROTOCOL_ERROR", "好单库返回非 JSON 字典")

            code = payload.get("code")
            msg = clean_text(payload.get("msg") or payload.get("sub_msg") or "")
            sub_msg = clean_text(payload.get("sub_msg") or "")
            err_detail = f"{msg} ({sub_msg})" if (msg and sub_msg and sub_msg != msg) else (msg or sub_msg)

            if code in (200, 1, "200", "1"):
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="success", latency_ms=dt_ms, allow_persist=is_real)
                return {"status": "success", "data": payload.get("data"), "raw": payload}

            err_msg_lower = err_detail.lower()
            if any(kw in err_detail for kw in HDK_RATE_LIMIT_KEYWORDS) or "429" in err_msg_lower:
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="rate_limit", latency_ms=dt_ms, error_code="RATE_LIMIT", error_msg=err_detail[:120], allow_persist=is_real)
                return _error("RATE_LIMIT", msg or "好单库接口调用频次超限", sub_msg=payload.get("sub_msg"))
            if "未授权" in err_detail or "授权" in err_detail or "绑定" in err_detail:
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="degraded", latency_ms=dt_ms, error_code="AUTH_REQUIRED", error_msg=err_detail[:120], allow_persist=is_real)
                return _error("AUTH_REQUIRED", msg or "好单库尚未绑定推客账号", sub_msg=payload.get("sub_msg"))
            if "下架" in err_detail or "不在推广" in err_detail:
                GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="degraded", latency_ms=dt_ms, error_code="PRODUCT_OFF_PROMOTION", error_msg=err_detail[:120], allow_persist=is_real)
                return _error("PRODUCT_OFF_PROMOTION", msg or "该商品不在推广中或已下架")
            GLOBAL_UPSTREAM_HEALTH.record_call(plat, "hdk", path, outcome="error", latency_ms=dt_ms, error_code=f"HDK_{code}", error_msg=err_detail[:120], allow_persist=is_real)
            return _error("UPSTREAM_ERROR", err_detail or f"好单库返回错误码 {code}", upstream_code=code, sub_msg=payload.get("sub_msg"))

        if self.appkey_list:
            res = execute_with_key_failover(self, _do_single_request, round_robin=True)
            self.api_key = self.appkey
            return res
        return _do_single_request(self.api_key)

    def search_goods(
        self,
        keyword: str,
        *,
        page: int = 1,
        page_size: int = 20,
        union_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """全网京东联盟在推商品搜索 (接口 id=50: v3/jd_goods_search)。"""
        kw = clean_text(keyword)
        if not kw:
            return _error("INVALID_ARGUMENT", "搜索关键词不能为空")
        uid = clean_text(union_id) or self.union_id
        cur_page = max(1, int(page or 1))
        size = min(100, max(1, int(page_size or 20)))
        params = {
            "keyword": kw,
            "min_id": cur_page,
            "page": cur_page,
            "back": size,
            "page_size": size,
            "union_id": uid or None,
        }
        res = self._request("GET", "jd_goods_search", params=params)
        if res.get("status") != "success":
            return res
        rows = res.get("data")
        items = rows if isinstance(rows, list) else []
        return {
            "status": "success",
            "data": items,
            "total_count": len(items),
            "page": cur_page,
            "page_size": size,
            "has_more": len(items) >= size,
        }

    def get_goods_detail(
        self,
        goods_id_or_sign: str,
        *,
        union_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """通过加密 itemId 或数字 SKU 反查商品完整卡片详情 (复用 id=50 搜索接口精准匹配)。"""
        target = clean_text(goods_id_or_sign)
        if not target:
            return _error("INVALID_ARGUMENT", "商品标识不能为空")
        search_res = self.search_goods(target, page=1, page_size=10, union_id=union_id)
        if search_res.get("status") != "success":
            return search_res
        rows = search_res.get("data") or []
        if not rows:
            return _error("NOT_FOUND", "好单库未找到该商品详情", not_found=True)
        matched = next(
            (row for row in rows if clean_text(row.get("skuid")) == target or clean_text(row.get("itemid")) == target),
            None,
        )
        if not matched and len(rows) == 1:
            matched = rows[0]
        if not matched:
            return _error("PRODUCT_IDENTITY_MISMATCH", "好单库搜索返回与目标商品身份不一致")
        return {"status": "success", "data": matched}

    def resolve_short_url(
        self,
        short_url: str,
        *,
        union_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """委托无状态京东短链安全解包函数（不消耗好单库 Key 配额）。"""
        _ = union_id
        return resolve_jd_short_url(short_url, http_session=self.http)

    def convert_promotion_link(
        self,
        material_id: str,
        *,
        union_id: Optional[str] = None,
        sub_union_id: Optional[str] = None,
        coupon_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """京东高佣转链生成专属推广短链 (接口 id=48: v3/get_jditems_link；注意该接口不支持 positionId)。"""
        mat = clean_text(material_id)
        if not mat:
            return _error("INVALID_ARGUMENT", "缺少物料ID或商品链接")
        uid = clean_text(union_id) or self.union_id
        if not uid:
            return _error("CONFIG_ERROR", "京东转链缺少 union_id 配置")
        payload = {
            "union_id": uid,
            "material_id": mat,
            "sceneId": 1,
        }
        sub_id = clean_text(sub_union_id)
        if sub_id:
            payload["subUnionId"] = sub_id
        c_url = clean_text(coupon_url)
        if c_url:
            payload["coupon_url"] = c_url

        res = self._request("POST", "get_jditems_link", data=payload)
        if res.get("status") != "success":
            return res
        data = res.get("data")
        if not isinstance(data, dict):
            return _error("PROTOCOL_ERROR", "好单库转链返回数据异常")
        short = clean_text(data.get("short_url"))
        click = clean_text(data.get("click_url"))
        if not short and not click:
            return _error("PROTOCOL_ERROR", "好单库未返回有效推广短链")
        return {
            "status": "success",
            "short_url": short or click,
            "click_url": click or short,
            "final_link": short or click,
            "union_id": uid,
        }

    def convert_tb_promotion_link(
        self,
        item_id: str,
        pid: str,
        tb_name: str,
        title: str = "",
        relation_id: Optional[str] = None,
        special_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """淘宝高佣商品转链 (接口 id=1: v3/ratesurl)。"""
        iid = clean_text(item_id)
        if not iid:
            return _error("INVALID_ARGUMENT", "缺少淘宝商品ID")
        p = clean_text(pid)
        if not p:
            return _error("CONFIG_ERROR", "缺少淘宝 PID 配置")
        name = clean_text(tb_name)
        if not name:
            return _error("PROVIDER_UNAUTHORIZED", "缺少好单库已授权淘宝账号昵称 (tb_name)")
        payload = {
            "itemid": iid,
            "pid": p,
            "tb_name": name,
            "title": clean_text(title) or "精选好物",
            "get_taoword": 1,
        }
        if relation_id:
            payload["relation_id"] = clean_text(relation_id)
        if special_id:
            payload["special_id"] = clean_text(special_id)

        res = self._request("POST", "ratesurl", data=payload)
        if res.get("status") != "success":
            return res
        data = res.get("data")
        if not isinstance(data, dict):
            return _error("PROTOCOL_ERROR", "好单库淘宝转链返回数据异常")
        click_url = clean_text(data.get("coupon_click_url") or data.get("item_url"))
        short_url = clean_text(data.get("coupon_click_url") or data.get("item_url"))
        taoword = clean_text(data.get("taoword") or data.get("new_model"))
        if not click_url and not taoword:
            return _error("PROTOCOL_ERROR", "好单库未返回有效淘宝推广链接或口令")
        return {
            "status": "success",
            "final_link": short_url or click_url,
            "url": click_url or short_url,
            "short_url": short_url,
            "click_url": click_url,
            "tpwd": taoword,
            "taoword": taoword,
            "raw": data,
        }
