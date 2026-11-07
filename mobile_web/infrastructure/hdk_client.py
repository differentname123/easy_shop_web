# -*- coding: utf-8 -*-
"""好单库榜单HTTP边界，只输出来源ID与分页元数据，不输出商品商业内容。"""
import requests
from infrastructure.http_utils import error_dict as _error

HDK_SALES_URL = "https://v2.api.haodanku.com/sales_list"


class HdkClient:
    def __init__(self, api_key="", http_session=None, timeout=(3.0, 8.0)):
        self.api_key = api_key
        self.http = http_session or requests.Session()
        self.timeout = timeout

    def get_ranked_ids(self, sale_type, limit=20, cursor=None):
        if not self.api_key:
            return _error("CONFIG_ERROR", "淘宝榜单缺少好单库 API key")
        try:
            sale_type, limit = int(sale_type), int(limit)
            if sale_type not in (1, 2) or not 1 <= limit <= 100:
                raise ValueError
            # The example contract uses discrete back sizes; never send arbitrary
            # UI limits (e.g. 3 or 45). Smaller source pages are safe with next_offset.
            back = max(size for size in (1, 2, 5, 10, 20) if size <= limit)
            min_id = str((cursor or {}).get("min_id", "1"))
            if not min_id.isdigit() or int(min_id) < 1:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return _error("INVALID_ARGUMENT", "好单库榜单参数或游标无效")
        try:
            response = self.http.get(HDK_SALES_URL, params={
                "apikey": self.api_key, "sale_type": sale_type, "cid": 0, "back": back, "min_id": min_id,
            }, timeout=self.timeout, allow_redirects=False)
            if 300 <= getattr(response, "status_code", 200) < 400:
                return _error("PROTOCOL_ERROR", "好单库返回不受支持的重定向")
            response.raise_for_status()
            body = response.json()
        except Exception:
            # requests errors can include the credential-bearing query string.
            return _error("NETWORK_ERROR", "好单库榜单通信失败")
        if not isinstance(body, dict):
            return _error("PROTOCOL_ERROR", "好单库响应不是对象")
        if str(body.get("code")) == "0" and body.get("data") == [] and str(body.get("min_id")) == "0":
            return {"status": "success", "item_ids": [], "source_count": 0,
                    "has_more": False, "next_cursor": None, "total_count": body.get("total_count")}
        if str(body.get("code")) != "1":
            return _error("UPSTREAM_ERROR", "好单库榜单返回业务错误")
        rows = body.get("data")
        if not isinstance(rows, list):
            return _error("PROTOCOL_ERROR", "好单库响应缺少榜单列表")
        # Preserve invalid/empty slots too: pagination counts consumed source rows.
        ids = [str(row.get("itemid") or "").strip() if isinstance(row, dict) else "" for row in rows]
        next_id = body.get("min_id")
        explicit_more = body.get("has_more")
        if explicit_more is not None and explicit_more not in (True, False, 0, 1, "0", "1", "true", "false"):
            return _error("PROTOCOL_ERROR", "好单库 has_more 无效")
        has_more = explicit_more in (True, 1, "1", "true") if explicit_more is not None else bool(rows) and len(rows) >= back
        if next_id is not None and str(next_id) == "0" and explicit_more is None:
            has_more = False
        if back == 20 and next_id is not None and str(next_id).isdigit() and int(next_id) > 5 and explicit_more is None:
            has_more = False
        if has_more and (next_id is None or str(next_id) == min_id or not str(next_id).isdigit() or int(next_id) < 1):
            return _error("PAGINATION_ERROR", "好单库后续页缺少前进游标")
        return {"status": "success", "item_ids": ids, "source_count": len(rows),
                "has_more": has_more, "next_cursor": {"min_id": str(next_id)} if has_more else None,
                "total_count": body.get("total_count")}
