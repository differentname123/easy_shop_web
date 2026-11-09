# -*- coding: utf-8 -*-
"""京东/折京客薄协议边界；支持多 ZTK_APPKEY_LIST 自动轮询与故障切换，与 scripts/test_jd_api.py 严格对齐。"""
import json
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import requests
from infrastructure.http_utils import (
    SHANGHAI,
    error_dict as _error,
    execute_with_key_failover,
    parse_appkey_list,
)
from models.user import normalize_promoter_id

# Documented relay currently uses HTTP. Configure an approved secure alternative
# before sending production secrets; never silently substitute the Taobao relay.
DEFAULT_API_BASE = "http://api.zhetaoke.com:20000/api"
ORDER_WINDOW_SECONDS = 3600
ORDER_ROOT = "jd_union_open_order_row_query_responce"
RANK_ROOT = "jd_union_open_goods_rank_query_responce"
PROMOTION_ROOT = "jd_union_open_promotion_byunionid_get_response"
ORDER_METHOD = "jd.union.open.order.row.query"


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
    if "error_response" in body or "error" in body or (
        "status" in body and str(body.get("status")) != "200" and root not in body
    ):
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
        code_str = str(value["code"])
        msg_str = str(value.get("message") or "")
        if root == PROMOTION_ROOT and (
            code_str in ("2001104", "2001920")
            or "不在推广" in msg_str
            or "无法转链" in msg_str
        ):
            return _error("UPSTREAM_ERROR", "京东商品已取消推广或不在推广中", jd_off_promotion=True)
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
    has_empty_success = str(data.get("message") or "").lower() == "success" and bool(data.get("requestId"))
    if str(data.get("code")) != "200" or ("data" not in data and not has_empty_success):
        return _error("PROTOCOL_ERROR", "京东订单缺少成功代码或数据")
    block = data.get("data")
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
    京东折淘客协议客户端（与 scripts/test_jd_api.py 严格对齐）：
    支持传入单个 appkey、逗号分隔字符串或 `ZTK_APPKEY_LIST` 序列。
    由于折淘客频控限制绑定在单个 `appkey` 而非 IP 或 `unionId` 上，
    当当前 `appkey` 触发折淘客限流（返回 `UPSTREAM_ERROR` / `status=301`）或通信失败时，
    自动顺延切换列表中的下一个 `appkey` 重试，并记忆健康 Key 游标。
    """

    def __init__(self, appkey="", union_id="", app_key="", app_secret="",
                 api_base=DEFAULT_API_BASE, http_session=None, timeout=(3.0, 12.0),
                 appkey_list=None):
        self.appkey_list = parse_appkey_list(appkey_list if appkey_list is not None else appkey)
        self.appkey = self.appkey_list[0] if self.appkey_list else ""
        self._active_key_index = 0
        self.union_id = str(union_id).strip()
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

    def _validate_config(self, orders=False):
        if not self.appkey_list or (orders and not (self.app_key and self.app_secret)) or (not orders and not canonical_promoter_id(self.union_id)):
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
        return None

    def _execute_with_failover(self, endpoint, params, evaluator, *, orders=False):
        """
        使用 `self.appkey_list` 执行请求（委托 `execute_with_key_failover` 共享粘性游标容灾机制）：
        - 若遇到安全/协议/分页错误或京东已返回内层确定性业务封套，立即终止返回。
        """
        config_error = self._validate_config(orders=orders)
        if config_error is not None:
            return config_error

        last_body = None

        def _call(current_key):
            nonlocal last_body
            last_body = self._get(self.api_base + "/" + endpoint, params={"appkey": current_key, **params})
            return evaluator(last_body)

        return execute_with_key_failover(
            self,
            _call,
            should_stop_early=lambda _res: isinstance(last_body, dict) and (
                PROMOTION_ROOT in last_body or RANK_ROOT in last_body or ORDER_ROOT in last_body
            ),
        )

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

        def _evaluate(body):
            if body.get("status") == "error":
                return body
            result = _result(body, RANK_ROOT)
            if result["status"] != "success":
                return result
            data = result["data"]
            if not _rows(data.get("data")):
                return _error("PROTOCOL_ERROR", "京东榜单列表无效")
            return self._catalog_page(data["data"], page, size, data)

        return self._execute_with_failover("open_jd_union_open_goods_rank_query.ashx", {
            "unionId": self.union_id, "sortType": sort_type, "rankId": 200000,
            "pageIndex": page, "pageSize": size}, _evaluate)

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

    def search_goods(self, keyword, page=1, page_size=20, *, has_coupon=True):
        try:
            page, size = self._page(page, page_size)
            query = keyword.strip()
            if not query:
                raise ValueError
            # 【重要协议约束】折淘客全网搜索接口 (api_quanwang.ashx) 强制要求搜索词 q 的长度至少为 4 个字符；
            # 若少于 4 个字符（例如“纸巾”、“可乐”），折淘客上游会直接报错拒绝响应。
            # 因此此处必须对不足 4 个字符的关键词进行倍增补齐（如“纸巾” -> “纸巾纸巾”），切勿删除！
            while len(query) < 4:
                query += query
        except (TypeError, ValueError, AttributeError, OverflowError):
            return _error("INVALID_ARGUMENT", "京东搜索关键词或分页无效")
        return self._execute_with_failover(
            "api_quanwang.ashx",
            {"unionId": self.union_id, "q": query, "page": page, "page_size": size,
             "sort": "new", "youquan": 1 if has_coupon else 0, "type": 2},
            lambda body: self._content(body, page, size),
        )

    def get_goods_detail(self, goods_id):
        if not isinstance(goods_id, str) or not goods_id.strip():
            return _error("INVALID_ARGUMENT", "京东详情缺少商品ID")
        return self._execute_with_failover(
            "api_detail.ashx",
            {"unionId": self.union_id, "tao_id": goods_id, "type": 1},
            self._content,
        )

    def _content(self, body, page=None, size=None):
        if body.get("status") == "error":
            return body
        if str(body.get("status")) != "200":
            if str(body.get("status")) == "301" and "下架" in str(body.get("content") or ""):
                return _error("UPSTREAM_ERROR", "京东商品接口返回业务错误", ztk_ambiguous_301=True)
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

        def _evaluate(body):
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

        return self._execute_with_failover(
            "open_jing_union_open_promotion_byunionid_get.ashx",
            {"unionId": self.union_id, "materialId": material, "chainType": "3", "positionId": promoter},
            _evaluate,
        )

    def convert_universal_material(self, raw_material, promoter_id):
        """
        京东万能转链协议入口（对齐 scripts/test_jd_api.py 基准规范 六）：
        支持直接传入 u.jd.com 短链、京口令文案（含 ！...！）、jingfen.jd.com 详情链或加密 itemId。
        """
        promoter = canonical_promoter_id(promoter_id)
        material = str(raw_material or "").strip()
        if not promoter or not material or any(ord(c) < 32 and c not in "\r\n\t" for c in material):
            return _error("INVALID_ARGUMENT", "京东万能转链缺少有效口令/链接或数字推广ID")

        def _evaluate(body):
            if body.get("status") == "error":
                return body
            if isinstance(body, dict) and str(body.get("status")) == "301":
                content_msg = str(body.get("content") or "").strip()
                if "数字id" in content_msg or "数字ID" in content_msg:
                    return _error(
                        "UNSUPPORTED_JD_NUMERIC_SKU",
                        "京东联盟接口已不支持纯数字商品ID转链，请粘贴 u.jd.com 短链、京口令或京粉链接",
                    )
            result = _result(body, PROMOTION_ROOT, "result")
            if result["status"] != "success":
                return result
            result = result["data"]
            if str(result.get("code")) != "200" or not isinstance(result.get("data"), dict):
                return _error("PROTOCOL_ERROR", "京东转链结果无效")
            source = result["data"]
            data = {
                "short_url": product_url(source.get("shortURL")),
                "click_url": product_url(source.get("clickURL")),
                "j_command": source.get("jCommand") if isinstance(source.get("jCommand"), str) else "",
                "j_short_command": source.get("jShortCommand") if isinstance(source.get("jShortCommand"), str) else "",
            }
            if not (data["short_url"] or data["click_url"]):
                return _error("PROTOCOL_ERROR", "京东未返回有效推广链接")
            return {"status": "success", "promoter_id": promoter, **data}

        primary_res = self._execute_with_failover(
            "open_jing_union_open_promotion_byunionid_get.ashx",
            {"unionId": self.union_id, "materialId": material, "chainType": "3", "positionId": promoter},
            _evaluate,
        )
        if (
            primary_res.get("error_code") in ("UNSUPPORTED_JD_NUMERIC_SKU", "CONFIG_ERROR", "UNSAFE_REDIRECT")
            or primary_res.get("jd_off_promotion") is True
        ):
            return primary_res
        if primary_res.get("status") == "success" and "jingfen.jd.com/detail/" in material:
            return primary_res

        def _evaluate_piliang(body):
            if body.get("status") == "error":
                return body
            if str(body.get("status")) != "200":
                return _error("UPSTREAM_ERROR", "京东批量文案转链未成功")
            p_tao_id = str(body.get("tao_id") or "").strip()
            p_res_url = product_url(body.get("result_url"))
            p_res_tkl = str(body.get("result_tkl") or "").strip()
            if not (p_tao_id or p_res_url):
                return _error("UPSTREAM_ERROR", "京东批量文案转链未解析出商品")
            raw_item = None
            if p_tao_id:
                raw_item = dict(body)
                raw_item.setdefault("pict_url", raw_item.get("pic") or "")
                raw_item.setdefault("item_url", f"https://jingfen.jd.com/detail/{p_tao_id}.html")
            return {
                "status": "success",
                "promoter_id": promoter,
                "tao_id": p_tao_id,
                "short_url": p_res_url,
                "click_url": p_res_url,
                "j_command": p_res_tkl,
                "j_short_command": p_res_tkl,
                "raw_item": raw_item,
            }

        piliang_res = self._execute_with_failover(
            "open_gaoyongzhuanlian_tkl_piliang.ashx",
            {"unionId": self.union_id, "positionId": promoter, "tkl": material, "signurl": 5},
            _evaluate_piliang,
        )
        if primary_res.get("status") == "success":
            if piliang_res.get("status") == "success":
                primary_res["tao_id"] = piliang_res.get("tao_id", "")
                primary_res["raw_item"] = piliang_res.get("raw_item")
                if not primary_res.get("j_command") and piliang_res.get("j_command"):
                    primary_res["j_command"] = piliang_res["j_command"]
                if not primary_res.get("j_short_command") and piliang_res.get("j_short_command"):
                    primary_res["j_short_command"] = piliang_res["j_short_command"]
            return primary_res

        if piliang_res.get("status") != "success":
            return primary_res

        target_for_cmd = piliang_res.get("short_url") or (
            f"https://jingfen.jd.com/detail/{piliang_res['tao_id']}.html" if piliang_res.get("tao_id") else ""
        )
        if target_for_cmd:
            cmd_res = self._execute_with_failover(
                "open_jing_union_open_promotion_byunionid_get.ashx",
                {"unionId": self.union_id, "materialId": target_for_cmd, "chainType": "3", "positionId": promoter},
                _evaluate,
            )
            if cmd_res.get("status") == "success":
                cmd_res["tao_id"] = piliang_res.get("tao_id", "")
                cmd_res["raw_item"] = piliang_res.get("raw_item")
                return cmd_res
        return piliang_res

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

        def _evaluate_relay(body):
            if body.get("status") == "error":
                return body
            if str(body.get("status")) != "200":
                return _error("UPSTREAM_ERROR", "京东订单签名中继返回业务错误")
            signed = body.get("content")
            if not _trusted_signed_url(signed):
                return _error("UNTRUSTED_GATEWAY", "拒绝非受信HTTPS京东订单签名地址")
            return {"status": "success", "signed": signed}

        relay = self._execute_with_failover(
            "open_jing_union_openz_order_row_query.ashx", params, _evaluate_relay, orders=True
        )
        if relay.get("status") != "success":
            return relay
        body = self._get(relay["signed"])
        if body.get("status") == "error":
            return body
        result = parse_order_response(body)
        if result["status"] == "success":
            result["next_cursor"] = {"page": page + 1} if result["has_more"] else None
        return result
