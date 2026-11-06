# -*- coding: utf-8 -*-
"""折淘客渠道订单协议。无仓储依赖，拒绝未知响应及任意签名URL跳转。"""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import requests

ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"
ZTK_CHANNEL_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
SHANGHAI = timezone(timedelta(hours=8))
TRUSTED_TOP_HOSTS = frozenset(("eco.taobao.com", "gw.api.taobao.com"))
ORDER_RESPONSE_ROOTS = ("tbk_sc_order_details_get_response", "tbk_order_details_get_response")
TRUSTED_ORDER_METHODS = frozenset(("taobao.tbk.sc.order.details.get", "taobao.tbk.order.details.get"))
ORDER_WINDOW_SECONDS = 20 * 60


def _error(code, message, **extra):
    return {"status": "error", "error_code": code, "error_msg": message, **extra}


def _trusted_signed_url(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        methods = parse_qs(url.query, keep_blank_values=True).get("method", [])
        return (url.scheme == "https" and url.hostname in TRUSTED_TOP_HOSTS and
                url.port in (None, 443) and not url.username and not url.password and
                not url.fragment and url.path == "/router/rest" and
                len(methods) == 1 and methods[0] in TRUSTED_ORDER_METHODS)
    except ValueError:
        return False


def _boolean(value):
    if value in (True, 1, "1", "true"):
        return True
    if value in (False, 0, "0", "false"):
        return False
    raise ValueError("invalid boolean")


def parse_order_response(body):
    """只认可公开的TOP结果封套；错误不能转成成功空订单。"""
    if not isinstance(body, dict):
        return _error("PROTOCOL_ERROR", "订单响应不是对象")
    if "error_response" in body:
        return _error("UPSTREAM_ERROR", "淘宝订单网关返回业务错误")
    roots = [key for key in ORDER_RESPONSE_ROOTS if key in body]
    if len(roots) != 1:
        return _error("PROTOCOL_ERROR", "订单响应缺少唯一受支持的TOP结果封套")
    wrapper = body[roots[0]]
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("data"), dict):
        return _error("PROTOCOL_ERROR", "订单响应缺少受支持的TOP结果封套")
    data = wrapper["data"]
    try:
        has_more = _boolean(data["has_next"])
    except (KeyError, ValueError):
        return _error("PROTOCOL_ERROR", "订单响应缺少有效 has_next")
    results = data.get("results")
    # TOP can omit the array or results object for a genuinely empty last page.
    if results is None and not has_more:
        orders = []
    elif isinstance(results, dict):
        orders = results.get("publisher_order_dto", [])
        if isinstance(orders, dict):
            orders = [orders]
    else:
        return _error("PROTOCOL_ERROR", "订单响应的 results 无效")
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        return _error("PROTOCOL_ERROR", "订单响应的子订单列表无效")
    position = str(data.get("position_index") or "").strip()
    if has_more and not position:
        return _error("PAGINATION_ERROR", "订单存在后续页但缺少 position_index")
    return {"status": "success", "order_list": orders, "has_more": has_more,
            "position_index": position}


class ZtkClient:
    def __init__(self, appkey="", sid="", http_session=None, timeout=(3.0, 12.0), signurl=2):
        self.appkey, self.sid = appkey, sid
        self.http = http_session or requests.Session()
        self.timeout, self.signurl = timeout, signurl

    def _get_response(self, url, **kwargs):
        response = self.http.get(url, timeout=self.timeout, allow_redirects=False, **kwargs)
        if 300 <= getattr(response, "status_code", 200) < 400:
            return _error("UNSAFE_REDIRECT", "订单接口重定向被拒绝")
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            # One documented mode returns a bare signed gateway URL.
            return getattr(response, "text", "").strip()

    def query_orders(self, start_time, end_time, page=1, page_size=50, cursor=None):
        if not self.appkey or not self.sid:
            return _error("CONFIG_ERROR", "淘宝订单缺少折淘客 appkey 或 sid")
        try:
            start, end = int(start_time), int(end_time)
            page, size = int(page), int(page_size)
            if end < start or end - start > ORDER_WINDOW_SECONDS or not 1 <= page <= 100 or not 1 <= size <= 100:
                raise ValueError
            if self.signurl not in (1, 2):
                raise ValueError
            cursor = cursor or {}
            position = str(cursor.get("position_index") or "").strip()
            if cursor.get("page_no") is not None:
                page = int(cursor["page_no"])
            if not 1 <= page <= 100 or page > 1 and not position:
                raise ValueError
            params = {"appkey": self.appkey, "sid": self.sid, "signurl": self.signurl,
                      "query_type": 4, "order_scene": 2,
                      "start_time": datetime.fromtimestamp(start, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                      "end_time": datetime.fromtimestamp(end, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                      "page_no": page, "page_size": size, "jump_type": 1}
            if position:
                params["position_index"] = position
        except (TypeError, ValueError, AttributeError, OverflowError, OSError):
            return _error("INVALID_ARGUMENT", "订单时间窗、分页或游标无效（最大20分钟）")
        try:
            body = self._get_response(ZTK_ORDER_URL, params=params)
            if isinstance(body, dict) and body.get("status") == "error":
                return body
            signed_url = body if isinstance(body, str) else None
            if isinstance(body, dict) and not any(root in body for root in ORDER_RESPONSE_ROOTS):
                # Recognize only explicit URL envelopes, never recursively chase arbitrary fields.
                if body.get("error_response") or body.get("error"):
                    return _error("UPSTREAM_ERROR", "折淘客订单返回业务错误")
                if "code" in body and str(body["code"]) not in ("0", "1", "200"):
                    return _error("UPSTREAM_ERROR", "折淘客订单返回业务错误")
                candidate = body.get("url")
                if candidate is None and isinstance(body.get("data"), str):
                    candidate = body["data"]
                signed_url = candidate
            if signed_url is not None:
                if not _trusted_signed_url(signed_url):
                    return _error("UNTRUSTED_GATEWAY", "拒绝非受信HTTPS官方TOP签名地址")
                body = self._get_response(signed_url)
                if isinstance(body, dict) and body.get("status") == "error":
                    return body
            parsed = parse_order_response(body)
        except Exception:
            # Never log exceptions or request URLs: both carry keys/signatures.
            return _error("NETWORK_ERROR", "折淘客订单通信失败")
        if parsed.get("status") != "success":
            return parsed
        if parsed["has_more"] and page >= 100:
            return _error("PAGINATION_LIMIT", "订单超过上游100页上限，需要缩小时间窗")
        parsed["next_cursor"] = {"page_no": page + 1, "position_index": parsed["position_index"]} if parsed["has_more"] else None
        return parsed

    def convert_channel_url(self, item_url: str, relation_id: str, pid: str, signurl: int = 5):
        if not self.appkey or not self.sid:
            return _error("CONFIG_ERROR", "折淘客渠道转链缺少 appkey 或 sid")
        item_url = str(item_url or "").strip()
        relation_id = str(relation_id or "").strip()
        pid = str(pid or "").strip()
        if not item_url or not relation_id or not pid:
            return _error("INVALID_ARGUMENT", "折淘客渠道转链缺少有效商品链接、relation_id 或 pid")
        params = {
            "appkey": self.appkey,
            "sid": self.sid,
            "pid": pid,
            "tkl": item_url,
            "relation_id": relation_id,
            "signurl": signurl,
        }
        try:
            body = self._get_response(ZTK_CHANNEL_URL, params=params)
            if isinstance(body, dict) and body.get("status") == "error":
                return body
            if not isinstance(body, dict):
                return _error("PROTOCOL_ERROR", "折淘客渠道转链响应不是对象")
            status_code = body.get("status")
            content = body.get("content")

            # 折淘客在失败时，错误信息可能在 content (如 '很抱歉！无效的relationId！'), msg, 或 error_msg
            err_msg = ""
            if isinstance(content, str):
                err_msg = content.strip()
            if not err_msg:
                err_msg = str(body.get("msg") or body.get("error_msg") or "").strip()

            is_unauthorized = (
                status_code == 301
                or "relationId" in err_msg
                or "relation_id" in err_msg
                or "无效的relation" in err_msg
                or any(keyword in err_msg for keyword in ("未授权", "未备案", "渠道关系无效", "渠道失效", "无效的渠道"))
            )

            if status_code != 200 or not content or not isinstance(content, list):
                msg_text = err_msg or "折淘客渠道转链未返回有效物料"
                if is_unauthorized:
                    return _error("AUTH_REQUIRED", f"折淘客渠道授权失效: {msg_text}", need_auth=True)
                return _error("UPSTREAM_ERROR", f"折淘客渠道转链失败: {msg_text}")

            if not content or not isinstance(content[0], dict):
                return _error("PROTOCOL_ERROR", "折淘客渠道转链内容列表为空或格式无效")
            c = content[0]
            target_url = c.get("coupon_click_url") or c.get("item_url") or ""
            if target_url.startswith("//"):
                target_url = "https:" + target_url
            title = c.get("title") or c.get("tao_title") or "渠道推广商品"
            tpwd = str(c.get("tkl") or "").strip()
            return {
                "status": "success",
                "title": title,
                "final_price": str(c.get("quanhou_jiage") or c.get("size") or "0.00"),
                "commission_rate": str(c.get("tkrate3") or "0"),
                "commission_amount": str(c.get("tkfee3") or "0.00"),
                "is_channel": True,
                "pid": pid,
                "relation_id": relation_id,
                "url": target_url,
                "final_link": target_url,
                "mobile_url": target_url,
                "tpwd": tpwd,
                "raw": c,
            }
        except Exception:
            return _error("NETWORK_ERROR", "折淘客渠道转链通信失败")
