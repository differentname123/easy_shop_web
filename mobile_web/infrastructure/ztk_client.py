# -*- coding: utf-8 -*-
"""折淘客渠道订单与高佣转链协议（支持 ZTK_APPKEY_LIST 自动轮询切换，与 scripts/test_tb_api.py 对齐）。"""
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit
import requests
from infrastructure.http_utils import SHANGHAI, error_dict as _error, parse_appkey_list

ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"
ZTK_CHANNEL_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
ZTK_PUBLISHER_GET_URL = "https://api.zhetaoke.com:10001/api/open_sc_publisher_get.ashx"
ZTK_INVITECODE_GET_URL = "https://api.zhetaoke.com:10001/api/open_sc_invitecode_get.ashx"
ZTK_AUTH_INFO_URL = "https://api.zhetaoke.com:10001/api/open_taokeshouquaninfo.ashx"
TRUSTED_TOP_HOSTS = frozenset(("eco.taobao.com", "gw.api.taobao.com"))
ORDER_RESPONSE_ROOTS = ("tbk_sc_order_details_get_response", "tbk_order_details_get_response")
TRUSTED_ORDER_METHODS = frozenset(("taobao.tbk.sc.order.details.get", "taobao.tbk.order.details.get"))
ORDER_WINDOW_SECONDS = 20 * 60


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
    def __init__(self, appkey="", sid="", http_session=None, timeout=(3.0, 12.0), signurl=2, appkey_list=None):
        self.appkey_list = parse_appkey_list(appkey, appkey_list)
        self.appkey = self.appkey_list[0] if self.appkey_list else ""
        self._active_key_index = 0
        self.sid = sid
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

    def _run_with_key_failover(self, action):
        total = len(self.appkey_list)
        start_idx = self._active_key_index % total
        last_result = None
        for offset in range(total):
            idx = (start_idx + offset) % total
            current_key = self.appkey_list[idx]
            result = action(current_key)
            if result.get("status") == "success":
                self._active_key_index = idx
                self.appkey = current_key
                return result
            err_msg = str(result.get("error_msg") or "")
            if last_result is not None and ("sid和appkey不匹配" in err_msg or "sid过期" in err_msg):
                continue
            last_result = result
            if result.get("error_code") not in ("UPSTREAM_ERROR", "NETWORK_ERROR"):
                return result
        return last_result

    def query_orders(self, start_time, end_time, page=1, page_size=50, cursor=None):
        if not self.appkey_list or not self.sid:
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
            base_params = {
                "sid": self.sid, "signurl": self.signurl,
                "query_type": 4, "order_scene": 2,
                "start_time": datetime.fromtimestamp(start, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": datetime.fromtimestamp(end, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                "page_no": page, "page_size": size, "jump_type": 1,
            }
            if position:
                base_params["position_index"] = position
        except (TypeError, ValueError, AttributeError, OverflowError, OSError):
            return _error("INVALID_ARGUMENT", "订单时间窗、分页或游标无效（最大20分钟）")

        def _query_once(current_key):
            params = {"appkey": current_key, **base_params}
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

        return self._run_with_key_failover(_query_once)

    def convert_channel_url(self, item_url: str, relation_id: str = "", pid: str = "", signurl: int = 5, *, allow_public: bool = False):
        if not self.appkey_list or not self.sid:
            return _error("CONFIG_ERROR", "折淘客渠道转链缺少 appkey 或 sid")
        item_url = str(item_url or "").strip()
        relation_id = str(relation_id or "").strip()
        pid = str(pid or "").strip()
        if not item_url or not pid or (not allow_public and not relation_id):
            return _error("INVALID_ARGUMENT", "折淘客渠道转链缺少有效商品链接、relation_id 或 pid")

        def _convert_once(current_key):
            params = {
                "appkey": current_key,
                "sid": self.sid,
                "pid": pid,
                "tkl": item_url,
                "signurl": signurl,
            }
            if relation_id:
                params["relation_id"] = relation_id
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
                    "relationId" in err_msg
                    or "relation_id" in err_msg
                    or "无效的relation" in err_msg
                    or any(keyword in err_msg for keyword in ("未授权", "未备案", "渠道关系无效", "渠道失效", "无效的渠道"))
                )

                if status_code != 200 or not content or not isinstance(content, list):
                    msg_text = err_msg or "折淘客渠道转链未返回有效物料"
                    if is_unauthorized:
                        return _error("AUTH_REQUIRED", f"折淘客渠道授权失效: {msg_text}", need_auth=True)
                    if "不支持数字id" in msg_text.lower():
                        return _error("UNSUPPORTED_TB_NUMERIC_ID", f"折淘客渠道转链失败: {msg_text}")
                    if "error tkl" in msg_text.lower():
                        return _error("INVALID_TKL", f"折淘客渠道转链失败: {msg_text}")
                    return _error("UPSTREAM_ERROR", f"折淘客渠道转链失败: {msg_text}")

                if not content or not isinstance(content[0], dict):
                    return _error("PROTOCOL_ERROR", "折淘客渠道转链内容列表为空或格式无效")
                c = content[0]
                target_url = c.get("coupon_click_url") or c.get("item_url") or ""
                if target_url.startswith("//"):
                    target_url = "https:" + target_url
                short_url = str(c.get("shorturl") or "").strip()
                if short_url.startswith("//"):
                    short_url = "https:" + short_url
                short_url2 = str(c.get("shorturl2") or "").strip()
                if short_url2.startswith("//"):
                    short_url2 = "https:" + short_url2
                title = c.get("title") or c.get("tao_title") or "渠道推广商品"
                tpwd = str(c.get("tkl") or "").strip()
                return {
                    "status": "success",
                    "title": title,
                    "final_price": str(c.get("quanhou_jiage") or c.get("size") or "0.00"),
                    "commission_rate": str(c.get("tkrate3") or "0"),
                    "commission_amount": str(c.get("tkfee3") or "0.00"),
                    "is_channel": bool(relation_id),
                    "pid": pid,
                    "relation_id": relation_id,
                    "url": target_url,
                    "final_link": target_url,
                    "short_url": short_url or target_url,
                    "short_url2": short_url2,
                    "mobile_url": target_url,
                    "tpwd": tpwd,
                    "raw": c,
                }
            except Exception:
                return _error("NETWORK_ERROR", "折淘客渠道转链通信失败")

        return self._run_with_key_failover(_convert_once)

    def check_publisher_relation(self, uid: str):
        """
        通过折淘客 90 天长效 sid 调用 open_sc_publisher_get.ashx 核验推手淘宝渠道备案关系。
        当本地自用型 TOP 应用的 24h taobao_session 过期时，提供无缝长效渠道核验能力。
        """
        if not self.appkey_list or not str(self.sid or "").strip():
            return _error("CONFIG_ERROR", "折淘客渠道核验缺少 appkey 或 sid")
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return _error("INVALID_IDENTITY", "渠道核验需要站内 UID")

        def _check_once(current_key):
            params = {
                "appkey": current_key,
                "sid": str(self.sid).strip(),
                "relation_app": "common",
                "info_type": 1,
                "external_id": clean_uid,
                "page_no": 1,
                "page_size": 100,
            }
            try:
                body = self._get_response(ZTK_PUBLISHER_GET_URL, params=params)
                if isinstance(body, dict) and body.get("status") == "error":
                    return body
                if not isinstance(body, dict):
                    return _error("PROTOCOL_ERROR", "折淘客渠道核验响应不是对象")
                if "error_response" in body or body.get("status") in (301, 1000):
                    return _error("UPSTREAM_ERROR", "折淘客渠道核验接口返回业务错误")
                resp_data = (body.get("tbk_sc_publisher_info_get_response") or {}).get("data")
                if not isinstance(resp_data, dict):
                    return _error("PROTOCOL_ERROR", "折淘客渠道核验缺少结果封套")
                inviter_list = resp_data.get("inviter_list")
                records = []
                if isinstance(inviter_list, dict):
                    raw_map = inviter_list.get("map_data", [])
                    records = [raw_map] if isinstance(raw_map, dict) else (raw_map if isinstance(raw_map, list) else [])
                elif isinstance(inviter_list, list):
                    records = inviter_list

                # 折淘客中继在未命中特定 external_id 时可能返回该 sid 下的全量渠道记录，需按站内 UID 精确过滤
                matches = [
                    r for r in records
                    if isinstance(r, dict)
                    and str(r.get("external_id") or "").strip() == clean_uid
                    and str(r.get("relation_id") or "").strip() not in ("", "0")
                ]
                relations = {str(r["relation_id"]).strip() for r in matches}
                if len(relations) > 1:
                    return _error("AMBIGUOUS_IDENTITY", "官方返回多个渠道身份，需要人工核验")
                if relations:
                    matched = matches[0]
                    identity = {"relation_id": next(iter(relations)), "external_id": clean_uid}
                    if matched.get("account_name"):
                        identity["account_name"] = str(matched["account_name"]).strip()
                    if matched.get("create_date"):
                        identity["create_date"] = str(matched["create_date"]).strip()
                    return {
                        "status": "success",
                        "is_authorized": True,
                        "bind": 1,
                        "identity": identity,
                        "records": matches,
                    }
                return {
                    "status": "success",
                    "is_authorized": False,
                    "bind": 0,
                    "identity": {},
                    "records": [],
                }
            except Exception:
                return _error("NETWORK_ERROR", "折淘客渠道核验通信失败")

        return self._run_with_key_failover(_check_once)

    def generate_authority_url(self, uid: str, inviter_code: str = ""):
        """
        通过折淘客 90 天长效 sid 调用 open_sc_invitecode_get.ashx 生成渠道备案邀请链接。
        """
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return _error("INVALID_IDENTITY", "授权链接需要站内 UID")
        code = str(inviter_code or "").strip()
        if not code and self.appkey_list and str(self.sid or "").strip():
            def _fetch_code(current_key):
                params = {
                    "appkey": current_key,
                    "sid": str(self.sid).strip(),
                    "relation_app": "common",
                    "code_type": 1,
                }
                try:
                    body = self._get_response(ZTK_INVITECODE_GET_URL, params=params)
                    if not isinstance(body, dict) or "error_response" in body:
                        return _error("UPSTREAM_ERROR", "折淘客邀请码获取失败")
                    data = (body.get("tbk_sc_invitecode_get_response") or {}).get("data") or {}
                    fetched = str(data.get("inviter_code") or "").strip()
                    if not fetched:
                        return _error("UPSTREAM_ERROR", "折淘客未返回有效邀请码")
                    return {"status": "success", "inviter_code": fetched}
                except Exception:
                    return _error("NETWORK_ERROR", "折淘客邀请码通信失败")

            res = self._run_with_key_failover(_fetch_code)
            if isinstance(res, dict) and res.get("status") == "success":
                code = res.get("inviter_code") or ""
        if not code:
            code = "VK6WGA"
        url = "https://mos.m.taobao.com/inviter/register?" + urlencode({
            "inviterCode": code, "src": "pub", "app": "common", "externalId": clean_uid,
        })
        return {"status": "success", "auth_url": url, "short_url": "", "we_app_info": {}}

    def get_auth_info(self):
        """查询折淘客 sid 授权详情及过期时间 (open_taokeshouquaninfo.ashx)。"""
        if not self.appkey_list or not str(self.sid or "").strip():
            return _error("CONFIG_ERROR", "缺少折淘客 appkey 或 sid")

        def _info_once(current_key):
            try:
                body = self._get_response(ZTK_AUTH_INFO_URL, params={"appkey": current_key, "sid": str(self.sid).strip()})
                if not isinstance(body, dict) or body.get("status") != 200 or not isinstance(body.get("data"), list) or not body["data"]:
                    return _error("UPSTREAM_ERROR", "未查询到折淘客授权信息")
                return {"status": "success", "data": body["data"][0]}
            except Exception:
                return _error("NETWORK_ERROR", "折淘客授权信息查询失败")

        return self._run_with_key_failover(_info_once)
