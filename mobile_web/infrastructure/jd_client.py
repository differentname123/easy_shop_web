# -*- coding: utf-8 -*-
"""京东/折京客薄协议边界；不读取用户、仓储或示例中的凭据。"""
import json
from models.user import normalize_promoter_id
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import requests

# Documented relay currently uses HTTP. Configure an approved secure alternative
# before sending production secrets; never silently substitute the Taobao relay.
DEFAULT_API_BASE = "http://api.zhetaoke.com:20000/api"
ORDER_WINDOW_SECONDS = 3600
SHANGHAI = timezone(timedelta(hours=8))
ORDER_ROOT = "jd_union_open_order_row_query_responce"
RANK_ROOT = "jd_union_open_goods_rank_query_responce"
PROMOTION_ROOT = "jd_union_open_promotion_byunionid_get_response"
ORDER_METHOD = "jd.union.open.order.row.query"


def _error(code, message):
    return {"status": "error", "error_code": code, "error_msg": message}


def canonical_promoter_id(value):
    """Optional evidence/configuration uses the shared strict validator."""
    try:
        return normalize_promoter_id(value)
    except ValueError:
        return ""


def product_url(value):
    """Validate supplier/server-owned JD material URLs, without fetching them."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    value = "https:" + value if value.startswith("//") else value
    if "\\" in value or any(ord(char) < 33 for char in value):
        return ""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        trusted = host == "jd.com" or host.endswith(".jd.com")
        return value if (trusted and url.scheme in ("https", "http") and
                         url.port in (None, 443 if url.scheme == "https" else 80) and
                         url.username is None and url.password is None and not url.fragment) else ""
    except ValueError:
        return ""


def _trusted_signed_url(value):
    if not isinstance(value, str) or "\\" in value or any(ord(c) < 33 for c in value):
        return False
    try:
        url = urlsplit(value)
        methods = parse_qs(url.query, keep_blank_values=True).get("method", [])
        return (url.scheme == "https" and url.hostname == "api.jd.com" and
                url.netloc.lower() in ("api.jd.com", "api.jd.com:443") and
                url.port in (None, 443) and url.username is None and url.password is None and
                not url.fragment and url.path == "/routerjson" and methods == [ORDER_METHOD])
    except ValueError:
        return False


def _result(body, root, field="queryResult"):
    if not isinstance(body, dict):
        return _error("PROTOCOL_ERROR", "京东响应不是对象")
    if "error_response" in body or "error" in body:
        return _error("UPSTREAM_ERROR", "京东网关返回业务错误")
    wrapper = body.get(root)
    if not isinstance(wrapper, dict) or field not in wrapper:
        return _error("PROTOCOL_ERROR", "京东响应缺少受支持的结果封套")
    value = wrapper[field]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return _error("PROTOCOL_ERROR", "京东结果不是有效JSON")
    if not isinstance(value, dict):
        return _error("PROTOCOL_ERROR", "京东结果不是对象")
    if "error_response" in value or "error" in value:
        return _error("UPSTREAM_ERROR", "京东返回业务错误")
    if "code" in value and str(value["code"]) != "200":
        return _error("UPSTREAM_ERROR", "京东返回业务错误")
    return {"status": "success", "data": value}


def _rows(value):
    return isinstance(value, list) and all(isinstance(row, dict) for row in value)


def _has_more(value):
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("invalid hasMore")


def parse_order_response(body):
    result = _result(body, ORDER_ROOT)
    if result["status"] != "success":
        return result
    data = result["data"]
    if str(data.get("code")) != "200" or "data" not in data:
        return _error("PROTOCOL_ERROR", "京东订单缺少成功代码或数据")
    block = data["data"]
    if isinstance(block, list):
        rows = block
    elif isinstance(block, dict) and "orderRowResp" in block:
        rows = block["orderRowResp"]
        rows = [rows] if isinstance(rows, dict) else rows
    elif block is None:
        rows = []
    else:
        return _error("PROTOCOL_ERROR", "京东订单行结构无效")
    if not _rows(rows):
        return _error("PROTOCOL_ERROR", "京东订单行列表无效")
    try:
        # The demonstrated API omits hasMore on a terminal result.
        has_more = _has_more(data.get("hasMore", False))
    except ValueError:
        return _error("PROTOCOL_ERROR", "京东订单hasMore无效")
    if has_more and not rows:
        return _error("PAGINATION_ERROR", "京东空订单页不能声明有后续页")
    return {"status": "success", "order_list": rows, "has_more": has_more,
            "total_count": len(rows)}


class JdClient:
    def __init__(self, appkey="", union_id="", app_key="", app_secret="",
                 api_base=DEFAULT_API_BASE, http_session=None, timeout=(3.0, 12.0)):
        self.appkey, self.union_id = appkey, str(union_id).strip()
        self.app_key, self.app_secret = app_key, app_secret
        self.api_base = str(api_base).rstrip("/")
        self.http = http_session if http_session is not None else requests.Session()
        self.timeout = timeout

    def _get(self, url, **kwargs):
        try:
            response = self.http.get(url, timeout=self.timeout, allow_redirects=False, **kwargs)
            if 300 <= response.status_code < 400:
                return _error("UNSAFE_REDIRECT", "京东接口重定向被拒绝")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                return _error("PROTOCOL_ERROR", "京东响应不是对象")
            if body.get("status") == "error" or "error" in body or "error_response" in body:
                return _error("UPSTREAM_ERROR", "京东接口返回业务错误")
            return body
        except ValueError:
            return _error("PROTOCOL_ERROR", "京东响应不是有效JSON")
        except Exception:
            # Request exceptions and supplier messages may contain keys/signed URLs.
            return _error("NETWORK_ERROR", "京东接口通信失败")

    def _request(self, endpoint, params, orders=False):
        if not self.appkey or (orders and not (self.app_key and self.app_secret)) or (not orders and not canonical_promoter_id(self.union_id)):
            return _error("CONFIG_ERROR", "京东接口缺少必要配置")
        try:
            base = urlsplit(self.api_base)
            valid = (base.scheme in ("http", "https") and base.hostname and
                     base.username is None and base.password is None and not base.query and not base.fragment and
                     base.path == "/api" and base.port != 0 and "\\" not in self.api_base and
                     not any(ord(c) < 33 for c in self.api_base))
        except ValueError:
            valid = False
        if not valid:
            return _error("CONFIG_ERROR", "京东中继网关配置无效")
        return self._get(self.api_base + "/" + endpoint, params={"appkey": self.appkey, **params})

    @staticmethod
    def _page(page, size):
        if (isinstance(page, bool) or isinstance(size, bool) or
                not isinstance(page, (str, int)) or not isinstance(size, (str, int))):
            raise ValueError
        page, size = int(page), int(size)
        if not 1 <= page <= 100000 or not 1 <= size <= 100:
            raise ValueError
        return page, size

    def get_rank_goods(self, sort_type=1, page=1, page_size=20):
        try:
            page, size = self._page(page, page_size)
            if sort_type not in (1, 3):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return _error("INVALID_ARGUMENT", "京东榜单分页或类型无效")
        body = self._request("open_jd_union_open_goods_rank_query.ashx", {
            "unionId": self.union_id, "sortType": sort_type, "rankId": 200000,
            "pageIndex": page, "pageSize": size})
        if body.get("status") == "error":
            return body
        result = _result(body, RANK_ROOT)
        if result["status"] != "success":
            return result
        data = result["data"]
        if not _rows(data.get("data")):
            return _error("PROTOCOL_ERROR", "京东榜单列表无效")
        return self._catalog_page(data["data"], page, size, data)

    @staticmethod
    def _catalog_page(rows, page, size, envelope):
        total = envelope.get("totalCount")
        try:
            total = int(total) if total is not None else None
            if total is not None and total < 0:
                raise ValueError
            more = _has_more(envelope["hasMore"]) if "hasMore" in envelope else (
                page * size < total if total is not None else len(rows) >= size)
        except (TypeError, ValueError, OverflowError):
            return _error("PROTOCOL_ERROR", "京东商品分页数据无效")
        if len(rows) > size or not rows and more:
            return _error("PAGINATION_ERROR", "京东商品页长与分页声明不一致")
        return {"status": "success", "data": rows, "has_more": more, "total_count": total}

    def search_goods(self, keyword, page=1, page_size=20):
        try:
            page, size = self._page(page, page_size)
            query = keyword.strip()
            if not query:
                raise ValueError
            while len(query) < 4:
                query += query
        except (TypeError, ValueError, AttributeError, OverflowError):
            return _error("INVALID_ARGUMENT", "京东搜索关键词或分页无效")
        body = self._request("api_quanwang.ashx", {"unionId": self.union_id, "q": query,
            "page": page, "page_size": size, "sort": "new", "youquan": 1, "type": 2})
        return self._content(body, page, size)

    def get_goods_detail(self, goods_id):
        if not isinstance(goods_id, str) or not goods_id.strip():
            return _error("INVALID_ARGUMENT", "京东详情缺少商品ID")
        body = self._request("api_detail.ashx", {"unionId": self.union_id, "tao_id": goods_id, "type": 1})
        return self._content(body)

    def _content(self, body, page=None, size=None):
        if body.get("status") == "error":
            return body
        if str(body.get("status")) != "200":
            return _error("UPSTREAM_ERROR", "京东商品接口返回业务错误")
        if not _rows(body.get("content")):
            return _error("PROTOCOL_ERROR", "京东商品content列表无效")
        return (self._catalog_page(body["content"], page, size, body) if page is not None else
                {"status": "success", "data": body["content"]})

    def convert_promotion_link(self, material_url, promoter_id):
        promoter = canonical_promoter_id(promoter_id)
        material = product_url(material_url)
        if not promoter or not material:
            return _error("INVALID_ARGUMENT", "京东转链缺少有效物料URL或数字推广ID")
        body = self._request("open_jing_union_open_promotion_byunionid_get.ashx", {
            "unionId": self.union_id, "materialId": material, "chainType": "3", "positionId": promoter})
        if body.get("status") == "error":
            return body
        result = _result(body, PROMOTION_ROOT, "result")
        if result["status"] != "success":
            return result
        result = result["data"]
        if str(result.get("code")) != "200" or not isinstance(result.get("data"), dict):
            return _error("PROTOCOL_ERROR", "京东转链结果无效")
        source = result["data"]
        data = {"short_url": product_url(source.get("shortURL")),
                "click_url": product_url(source.get("clickURL")),
                "j_command": source.get("jCommand") if isinstance(source.get("jCommand"), str) else "",
                "j_short_command": source.get("jShortCommand") if isinstance(source.get("jShortCommand"), str) else ""}
        if not (data["short_url"] or data["click_url"]):
            return _error("PROTOCOL_ERROR", "京东未返回有效推广链接")
        return {"status": "success", "promoter_id": promoter, **data}

    def query_orders(self, start_time, end_time, page=1, page_size=50, cursor=None):
        try:
            start, end = int(start_time), int(end_time)
            page, size = self._page(page, page_size)
            if end < start or end - start > ORDER_WINDOW_SECONDS:
                raise ValueError
            if cursor is not None and (not isinstance(cursor, dict) or cursor != {"page": page}):
                raise ValueError
            params = {"jd_app_key": self.app_key, "jd_app_secret": self.app_secret,
                      "startTime": datetime.fromtimestamp(start, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                      "endTime": datetime.fromtimestamp(end, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                      "type": "3", "pageIndex": str(page), "pageSize": str(size),
                      "fields": "goodsInfo,categoryInfo"}
        except (TypeError, ValueError, OverflowError, OSError):
            return _error("INVALID_ARGUMENT", "京东订单时间窗、分页或游标无效（最大1小时）")
        body = self._request("open_jing_union_openz_order_row_query.ashx", params, orders=True)
        if body.get("status") == "error":
            return body
        if str(body.get("status")) != "200":
            return _error("UPSTREAM_ERROR", "京东订单签名中继返回业务错误")
        signed = body.get("content")
        if not _trusted_signed_url(signed):
            return _error("UNTRUSTED_GATEWAY", "拒绝非受信HTTPS京东订单签名地址")
        body = self._get(signed)
        if body.get("status") == "error":
            return body
        result = parse_order_response(body)
        if result["status"] == "success":
            result["next_cursor"] = {"page": page + 1} if result["has_more"] else None
        return result
