# -*- coding: utf-8 -*-
"""京东/折京客多级高可用薄协议边界；与 scripts/test_jd_api.py 基准实现严格对齐。"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import requests
from models.user import normalize_promoter_id

# 网关与协议根常量
DEFAULT_API_BASE = "http://api.zhetaoke.com:20000/api"
JD_ROUTER_URL = "https://api.jd.com/routerjson"
ORDER_WINDOW_SECONDS = 3600
SHANGHAI = timezone(timedelta(hours=8))

ORDER_ROOT = "jd_union_open_order_row_query_responce"
RANK_ROOT = "jd_union_open_goods_rank_query_responce"
GOODS_QUERY_ROOT = "jd_union_open_goods_query_response"
BIGFIELD_ROOT = "jd_union_open_goods_bigfield_query_response"
PROMOTION_ROOT = "jd_union_open_promotion_byunionid_get_response"

ORDER_METHOD = "jd.union.open.order.row.query"
RANK_METHOD = "jd.union.open.goods.rank.query"


def _error(code, message):
    return {"status": "error", "error_code": code, "error_msg": message}


def canonical_promoter_id(value):
    """Optional evidence/configuration uses the shared strict validator."""
    try:
        return normalize_promoter_id(value)
    except ValueError:
        return ""


def sign_jd_request(params, app_secret):
    """
    京东宙斯网关 (https://api.jd.com/routerjson) 标准 MD5 签名算法：
    按参数名 ASCII 升序排列非空参数 (排除 sign)，首尾包裹 app_secret 后计算大写 MD5。
    """
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    raw = str(app_secret) + "".join(f"{k}{v}" for k, v in sorted_items) + str(app_secret)
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def product_url(value):
    """Validate supplier/server-owned JD material URLs, without fetching them."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith(("item.jd.com/", "jingfen.jd.com/", "u.jd.com/", "union-click.jd.com/")):
        value = "https://" + value
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


def _result(body, root, field="queryResult", alt_root=None, alt_field=None):
    if not isinstance(body, dict):
        return _error("PROTOCOL_ERROR", "京东响应不是对象")
    if "error_response" in body or "error" in body:
        return _error("UPSTREAM_ERROR", "京东网关返回业务错误")
    wrapper = body.get(root)
    if not isinstance(wrapper, dict) and alt_root:
        wrapper = body.get(alt_root)
    if not isinstance(wrapper, dict):
        return _error("PROTOCOL_ERROR", "京东响应缺少受支持的结果封套")
    if field in wrapper:
        value = wrapper[field]
    elif alt_field and alt_field in wrapper:
        value = wrapper[alt_field]
    else:
        return _error("PROTOCOL_ERROR", "京东响应缺少受支持的结果封套")
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


def _extract_image_urls(item):
    """提取官方商品结构中的轮播图列表，用于判断是否需要调用 BigField 补充图片。"""
    if not isinstance(item, dict):
        return []
    image_info = item.get("imageInfo")
    if isinstance(image_info, dict) and isinstance(image_info.get("imageList"), list):
        urls = [
            str(entry.get("url")).strip() if isinstance(entry, dict) else str(entry).strip()
            for entry in image_info["imageList"]
            if (isinstance(entry, dict) and entry.get("url")) or (isinstance(entry, str) and entry.strip())
        ]
        if urls:
            return urls
    if isinstance(item.get("imgList"), list):
        return [str(u).strip() for u in item["imgList"] if str(u).strip()]
    return []


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
    """
    京东联盟多级高可用客户端 (对齐 scripts/test_jd_api.py):
    1. 实时榜单 (`get_rank_goods`):
       - 一级主通道: 本地 MD5 签名直连 `https://api.jd.com/routerjson` (`jd.union.open.goods.rank.query`)
       - 二级兜底通道: 折淘客官方协议中继 `open_jd_union_open_goods_rank_query.ashx`
    2. 关键词搜索 (`search_goods`):
       - 一级主通道: 京东联盟官方搜索协议 `open_jing_union_open_goods_query.ashx` (`jd.union.open.goods.query`)
       - 二级兜底通道: 折淘客 `api_quanwang.ashx` (短词自动补齐至 4 字符)
    3. 商品详情 (`get_goods_detail`):
       - 一级主通道 A: `open_jing_union_open_goods_query.ashx` (`keyword=goods_id, pageIndex=1, pageSize=1`)
       - 一级主通道 B: 当轮播图不足时联动 `open_jd_union_open_goods_bigfield_query.ashx` 补充高清图与 `mainSkuId`
       - 二级兜底通道: 折淘客 `api_detail.ashx`
    4. 推广转链 (`convert_promotion_link`):
       - `open_jing_union_open_promotion_byunionid_get.ashx`
    5. 增量订单 (`query_orders`):
       - 一级主通道: 本地 MD5 签名直连 `https://api.jd.com/routerjson` (`jd.union.open.order.row.query`)
       - 二级兜底通道: 折淘客签名中继 `open_jing_union_openz_order_row_query.ashx` -> 官方网关
    """

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

    def _valid_api_base(self):
        try:
            base = urlsplit(self.api_base)
            return bool(
                base.scheme in ("http", "https") and base.hostname and
                base.username is None and base.password is None and not base.query and not base.fragment and
                base.path == "/api" and base.port != 0 and "\\" not in self.api_base and
                not any(ord(c) < 33 for c in self.api_base)
            )
        except ValueError:
            return False

    def _request(self, endpoint, params, orders=False):
        if not self.appkey or (orders and not (self.app_key and self.app_secret)) or (not orders and not canonical_promoter_id(self.union_id)):
            return _error("CONFIG_ERROR", "京东接口缺少必要配置")
        if not self._valid_api_base():
            return _error("CONFIG_ERROR", "京东中继网关配置无效")
        return self._get(self.api_base + "/" + endpoint, params={"appkey": self.appkey, **params})

    def _router_request(self, method, param_json):
        """本地计算 MD5 签名直连京东官方网关 https://api.jd.com/routerjson，无需将 secret 发送给第三方。"""
        if not (self.app_key and self.app_secret):
            return _error("CONFIG_ERROR", "京东官方网关缺少应用密钥")
        sys_params = {
            "method": method,
            "app_key": self.app_key,
            "timestamp": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "1.0",
            "sign_method": "md5",
            "360buy_param_json": json.dumps(param_json, ensure_ascii=False, separators=(",", ":")),
        }
        sys_params["sign"] = sign_jd_request(sys_params, self.app_secret)
        return self._get(JD_ROUTER_URL, params=sys_params)

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
        if not self.appkey or not canonical_promoter_id(self.union_id):
            return _error("CONFIG_ERROR", "京东接口缺少必要配置")
        if not self._valid_api_base():
            return _error("CONFIG_ERROR", "京东中继网关配置无效")

        # 一级主通道：本地签名直连京东宙斯官方网关 (若已配置 app_key 与 app_secret)
        if self.app_key and self.app_secret:
            direct_body = self._router_request(RANK_METHOD, {
                "RankGoodsReq": {
                    "rankId": 200000,
                    "sortType": sort_type,
                    "pageIndex": page,
                    "pageSize": size,
                }
            })
            if direct_body.get("error_code") == "UNSAFE_REDIRECT":
                return direct_body
            if direct_body.get("status") != "error":
                direct_res = _result(
                    direct_body, RANK_ROOT, "queryResult",
                    alt_root="jd_union_open_goods_rank_query_response", alt_field="result",
                )
                if direct_res["status"] == "success":
                    data = direct_res["data"]
                    if not _rows(data.get("data")):
                        return _error("PROTOCOL_ERROR", "京东榜单列表无效")
                    return self._catalog_page(data["data"], page, size, data)

        # 二级兜底通道：折淘客官方协议中继
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
            clean_kw = keyword.strip()
            if not clean_kw:
                raise ValueError
        except (TypeError, ValueError, AttributeError, OverflowError):
            return _error("INVALID_ARGUMENT", "京东搜索关键词或分页无效")

        # 一级主通道：京东联盟官方商品查询协议 `open_jing_union_open_goods_query.ashx` (无需短词补齐，自带轮播图与券后价)
        official_body = self._request("open_jing_union_open_goods_query.ashx", {
            "unionId": self.union_id,
            "keyword": clean_kw,
            "pageIndex": page,
            "pageSize": size,
        })
        if official_body.get("error_code") in ("CONFIG_ERROR", "UNSAFE_REDIRECT"):
            return official_body
        if official_body.get("status") != "error":
            official_res = _result(
                official_body, GOODS_QUERY_ROOT, "result",
                alt_root="jd_union_open_goods_query_responce", alt_field="queryResult",
            )
            if official_res["status"] == "success":
                data = official_res["data"]
                if not _rows(data.get("data")):
                    return _error("PROTOCOL_ERROR", "京东搜索商品列表无效")
                page_res = self._catalog_page(data["data"], page, size, data)
                if page_res.get("error_code") == "PAGINATION_ERROR" or page_res.get("data"):
                    return page_res

        # 二级兜底通道：折淘客 `api_quanwang.ashx` (短词需补齐至 4 字符)
        query = clean_kw
        while len(query) < 4:
            query += query
        body = self._request("api_quanwang.ashx", {"unionId": self.union_id, "q": query,
            "page": page, "page_size": size, "sort": "new", "youquan": 1, "type": 2})
        return self._content(body, page, size)

    def get_goods_detail(self, goods_id):
        if not isinstance(goods_id, str) or not goods_id.strip():
            return _error("INVALID_ARGUMENT", "京东详情缺少商品ID")
        clean_id = goods_id.strip()

        # 一级主通道 A：京东联盟官方商品查询 (`open_jing_union_open_goods_query.ashx`)
        official_body = self._request("open_jing_union_open_goods_query.ashx", {
            "unionId": self.union_id,
            "keyword": clean_id,
            "pageIndex": 1,
            "pageSize": 1,
        })
        if official_body.get("error_code") in ("CONFIG_ERROR", "UNSAFE_REDIRECT"):
            return official_body
        if official_body.get("status") != "error":
            official_res = _result(
                official_body, GOODS_QUERY_ROOT, "result",
                alt_root="jd_union_open_goods_query_responce", alt_field="queryResult",
            )
            if official_res["status"] == "success":
                rows = official_res["data"].get("data")
                if not _rows(rows):
                    return _error("PROTOCOL_ERROR", "京东详情商品列表无效")
                if len(rows) == 1:
                    item = dict(rows[0])
                    # 一级主通道 B：仅当轮播图不足 2 张或缺少真实数字 skuId 时，按需调用 BigField 补充
                    if len(_extract_image_urls(item)) < 2:
                        bf_body = self._request("open_jd_union_open_goods_bigfield_query.ashx", {
                            "unionId": self.union_id,
                            "content": clean_id,
                        })
                        if bf_body.get("status") != "error":
                            bf_res = _result(
                                bf_body, BIGFIELD_ROOT, "result",
                                alt_root="jd_union_open_goods_bigfield_query_responce", alt_field="queryResult",
                            )
                            if bf_res["status"] == "success" and _rows(bf_res["data"].get("data")) and bf_res["data"]["data"]:
                                bf_item = bf_res["data"]["data"][0]
                                if isinstance(bf_item.get("imageInfo"), dict):
                                    item["imageInfo"] = bf_item["imageInfo"]
                                if bf_item.get("mainSkuId") and not item.get("skuId"):
                                    item["skuId"] = bf_item["mainSkuId"]
                    return {"status": "success", "data": [item]}
                if len(rows) > 1:
                    return {"status": "success", "data": rows}

        # 二级兜底通道：折淘客 `api_detail.ashx`
        body = self._request("api_detail.ashx", {"unionId": self.union_id, "tao_id": clean_id, "type": 1})
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
            start_str = datetime.fromtimestamp(start, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
            end_str = datetime.fromtimestamp(end, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
            params = {"jd_app_key": self.app_key, "jd_app_secret": self.app_secret,
                      "startTime": start_str, "endTime": end_str,
                      "type": "3", "pageIndex": str(page), "pageSize": str(size),
                      "fields": "goodsInfo,categoryInfo"}
        except (TypeError, ValueError, OverflowError, OSError):
            return _error("INVALID_ARGUMENT", "京东订单时间窗、分页或游标无效（最大1小时）")
        if not self.appkey or not (self.app_key and self.app_secret):
            return _error("CONFIG_ERROR", "京东接口缺少必要配置")
        if not self._valid_api_base():
            return _error("CONFIG_ERROR", "京东中继网关配置无效")

        # 一级主通道：本地 MD5 签名直连京东宙斯官方网关 (避免将 jd_app_secret 明文通过 HTTP 发送给第三方)
        direct_body = self._router_request(ORDER_METHOD, {
            "orderReq": {
                "pageIndex": page,
                "pageSize": size,
                "type": 3,
                "startTime": start_str,
                "endTime": end_str,
                "fields": "goodsInfo,categoryInfo",
            }
        })
        if direct_body.get("error_code") == "UNSAFE_REDIRECT":
            return direct_body
        if direct_body.get("status") != "error" and ORDER_ROOT in direct_body:
            direct_res = parse_order_response(direct_body)
            if direct_res["status"] == "success":
                direct_res["next_cursor"] = {"page": page + 1} if direct_res["has_more"] else None
                return direct_res
            if direct_res.get("error_code") == "PAGINATION_ERROR":
                return direct_res

        # 二级兜底通道：折淘客签名中继 -> 京东官方网关
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
