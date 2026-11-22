# -*- coding: utf-8 -*-
"""折淘客渠道订单与高佣转链协议（支持 ZTK_APPKEY_LIST 自动轮询切换，与 scripts/test_tb_api.py 对齐）。"""
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit
from infrastructure.api_governance import (
    GLOBAL_QUOTA_PACER,
    GLOBAL_UPSTREAM_HEALTH,
    QuotaPaceBacklogError,
    is_real_requests_session,
)
from infrastructure.http_utils import (
    SHANGHAI,
    clean_text,
    error_dict as _error,
    execute_with_key_failover,
    normalize_http_url,
    parse_appkey_list,
)
from infrastructure.transport import create_http_session

ZTK_ORDER_URL = "https://api.zhetaoke.com:10001/api/open_dingdanchaxun2.ashx"
ZTK_CHANNEL_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl.ashx"
ZTK_CHANNEL_PILIANG_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl_piliang.ashx"
ZTK_PUBLISHER_GET_URL = "https://api.zhetaoke.com:10001/api/open_sc_publisher_get.ashx"
ZTK_INVITECODE_GET_URL = "https://api.zhetaoke.com:10001/api/open_sc_invitecode_get.ashx"
ZTK_TB_QUANWANG_URL = "https://api.zhetaoke.com:10001/api/api_quanwang.ashx"
ZTK_TB_DETAIL_URL = "https://api.zhetaoke.com:10001/api/api_detail.ashx"
ZTK_TB_GAOYONG_URL = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian.ashx"
ZTK_TB_SHANGPIN_ID_URL = "https://api.zhetaoke.com:10001/api/open_shangpin_id.ashx"
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
    def __init__(self, appkey="", sid="", http_session=None, timeout=(3.0, 12.0), signurl=2, appkey_list=None, sid_list=None):
        self.appkey_list = parse_appkey_list(appkey, appkey_list)
        self.appkey = self.appkey_list[0] if self.appkey_list else ""
        if isinstance(sid_list, (list, tuple)) and sid_list:
            explicit_sids = [
                part.strip()
                for entry in sid_list
                if isinstance(entry, str) and not isinstance(entry, bool)
                for part in entry.split(",")
                if part.strip() and not any(ord(c) < 33 for c in part.strip())
            ]
        else:
            explicit_sids = list(parse_appkey_list(sid_list))
        parsed_sids = explicit_sids if explicit_sids else list(parse_appkey_list(sid))
        self.sid_list = tuple(parsed_sids)
        self.sid = self.sid_list[0] if self.sid_list else ""
        self._active_key_index = 0
        self.http = http_session or create_http_session(pool_connections=16, pool_maxsize=32)
        self.timeout, self.signurl = timeout, signurl

    def _sid_for_key(self, current_key: str) -> str:
        if self.sid_list:
            try:
                idx = self.appkey_list.index(current_key)
                if idx < len(self.sid_list):
                    return self.sid_list[idx]
            except ValueError:
                pass
            return self.sid_list[0]
        return str(self.sid or "").strip()

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
        all_keys_have_paired_sid = len(self.appkey_list) > 1 and len(self.sid_list) >= len(self.appkey_list)
        return execute_with_key_failover(
            self,
            action,
            should_ignore_error=lambda res, _prev: (
                "sid和appkey不匹配" in str(res.get("error_msg") or "")
                or "sid过期" in str(res.get("error_msg") or "")
            ),
            round_robin=all_keys_have_paired_sid,
        )

    def query_orders(self, start_time, end_time, page=1, page_size=50, cursor=None):
        if not self.appkey_list or not (self.sid_list or str(self.sid or "").strip()):
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
                "signurl": self.signurl,
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
            params = {"appkey": current_key, "sid": self._sid_for_key(current_key), **base_params}
            real_http = is_real_requests_session(self.http)
            t0 = time.perf_counter()

            def _record_order_metric(outcome, code="", msg=""):
                if real_http:
                    GLOBAL_UPSTREAM_HEALTH.record_call(
                        platform="taobao",
                        provider="ztk_tb",
                        operation="open_dingdanchaxun2",
                        outcome=outcome,
                        latency_ms=(time.perf_counter() - t0) * 1000.0,
                        error_code=code,
                        error_msg=msg,
                        allow_persist=True,
                    )

            try:
                GLOBAL_QUOTA_PACER.pace(
                    "ztk_tb_order",
                    current_key,
                    0.32,
                    http_session=self.http,
                    foreground_max_wait=10.0,
                    background_max_wait=60.0,
                )
            except QuotaPaceBacklogError:
                _record_order_metric("rate_limit", "QUOTA_PACE_BACKLOG", "淘宝订单接口请求过于频繁")
                return _error("QUOTA_PACE_BACKLOG", "淘宝订单接口请求过于频繁，请稍后重试")

            try:
                body = self._get_response(ZTK_ORDER_URL, params=params)
                if (
                    real_http
                    and isinstance(body, dict)
                    and not any(root in body for root in ORDER_RESPONSE_ROOTS)
                    and "status" in body
                    and str(body.get("status")) not in ("0", "1", "200", "success", "error")
                ):
                    time.sleep(0.85)
                    body = self._get_response(ZTK_ORDER_URL, params=params)
                if isinstance(body, dict) and body.get("status") == "error":
                    _record_order_metric("error", body.get("error_code", ""), body.get("error_msg", ""))
                    return body
                signed_url = body if isinstance(body, str) else None
                if isinstance(body, dict) and not any(root in body for root in ORDER_RESPONSE_ROOTS):
                    # Recognize only explicit URL envelopes, never recursively chase arbitrary fields.
                    if body.get("error_response") or body.get("error"):
                        _record_order_metric("error", "UPSTREAM_ERROR", "折淘客订单返回业务错误")
                        return _error("UPSTREAM_ERROR", "折淘客订单返回业务错误")
                    if "code" in body and str(body["code"]) not in ("0", "1", "200"):
                        _record_order_metric("error", "UPSTREAM_ERROR", "折淘客订单返回业务错误")
                        return _error("UPSTREAM_ERROR", "折淘客订单返回业务错误")
                    if "status" in body and str(body["status"]) not in ("0", "1", "200", "success"):
                        _record_order_metric("rate_limit", f"ZTK_STATUS_{body.get('status')}", "折淘客订单接口繁忙或触发限流")
                        return _error("UPSTREAM_ERROR", "折淘客订单接口繁忙或触发限流，请稍后自动续传")
                    candidate = body.get("url")
                    if candidate is None and isinstance(body.get("data"), str):
                        candidate = body["data"]
                    signed_url = candidate
                if signed_url is not None:
                    if not _trusted_signed_url(signed_url):
                        _record_order_metric("error", "UNTRUSTED_GATEWAY", "拒绝非受信HTTPS官方TOP签名地址")
                        return _error("UNTRUSTED_GATEWAY", "拒绝非受信HTTPS官方TOP签名地址")
                    body = self._get_response(signed_url)
                    if isinstance(body, dict) and body.get("status") == "error":
                        _record_order_metric("error", body.get("error_code", ""), body.get("error_msg", ""))
                        return body
                parsed = parse_order_response(body)
            except Exception:
                # Never log exceptions or request URLs: both carry keys/signatures.
                _record_order_metric("error", "NETWORK_ERROR", "折淘客订单通信失败")
                return _error("NETWORK_ERROR", "折淘客订单通信失败")
            if parsed.get("status") != "success":
                _record_order_metric("error", parsed.get("error_code", ""), parsed.get("error_msg", ""))
                return parsed
            if parsed["has_more"] and page >= 100:
                _record_order_metric("error", "PAGINATION_LIMIT", "订单超过上游100页上限")
                return _error("PAGINATION_LIMIT", "订单超过上游100页上限，需要缩小时间窗")
            _record_order_metric("success")
            parsed["next_cursor"] = {"page_no": page + 1, "position_index": parsed["position_index"]} if parsed["has_more"] else None
            return parsed

        return self._run_with_key_failover(_query_once)

    def convert_channel_url(self, item_url: str, relation_id: str = "", pid: str = "", signurl: int = 5, *, allow_public: bool = False):
        if not self.appkey_list or not (self.sid_list or str(self.sid or "").strip()):
            return _error("CONFIG_ERROR", "折淘客渠道转链缺少 appkey 或 sid")
        item_url = str(item_url or "").strip()
        relation_id = str(relation_id or "").strip()
        pid = str(pid or "").strip()
        if not item_url or not pid or (not allow_public and not relation_id):
            return _error("INVALID_ARGUMENT", "折淘客渠道转链缺少有效商品链接、relation_id 或 pid")

        def _convert_once(current_key):
            cur_sid = self._sid_for_key(current_key)
            params = {
                "appkey": current_key,
                "sid": cur_sid,
                "pid": pid,
                "tkl": item_url,
                "signurl": signurl,
            }
            if relation_id:
                params["relation_id"] = relation_id
            try:
                GLOBAL_QUOTA_PACER.pace(
                    "ztk_tb_channel",
                    current_key,
                    0.25,
                    http_session=self.http,
                    foreground_max_wait=3.0,
                    background_max_wait=12.0,
                )
            except QuotaPaceBacklogError:
                return _error("QUOTA_PACE_BACKLOG", "淘宝转链请求过于频繁，请稍后重试")
            try:
                try:
                    body = self._get_response(ZTK_CHANNEL_URL, params=params)
                except Exception:
                    if is_real_requests_session(self.http):
                        time.sleep(0.4)
                        body = self._get_response(ZTK_CHANNEL_URL, params=params)
                    else:
                        raise
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

                err_lower = err_msg.lower()
                is_key_or_sid_err = "appkey" in err_lower or "sid" in err_lower
                is_unauthorized = (
                    not is_key_or_sid_err
                    and (
                        "relationid" in err_lower
                        or "relation_id" in err_lower
                        or "无效的relation" in err_lower
                        or (
                            bool(relation_id)
                            and any(keyword in err_msg for keyword in ("未授权", "未备案", "渠道关系无效", "渠道失效", "无效的渠道"))
                        )
                    )
                )

                if status_code != 200 or not content or not isinstance(content, list):
                    msg_text = err_msg or "折淘客渠道转链未返回有效物料"
                    if is_unauthorized:
                        return _error(
                            "AUTH_REQUIRED",
                            f"折淘客渠道授权失效: {msg_text}",
                            need_auth=True,
                            deterministic_all_keys=True,
                        )
                    if "不支持数字id" in msg_text.lower():
                        return _error(
                            "UNSUPPORTED_TB_NUMERIC_ID",
                            f"折淘客渠道转链失败: {msg_text}",
                            deterministic_all_keys=True,
                        )

                    if "商品id解析错误" in msg_text.lower() or "error tkl" in msg_text.lower():
                        try:
                            GLOBAL_QUOTA_PACER.pace(
                                "ztk_tb_piliang",
                                current_key,
                                0.35,
                                http_session=self.http,
                                foreground_max_wait=3.0,
                                background_max_wait=12.0,
                            )
                        except QuotaPaceBacklogError:
                            return _error("QUOTA_PACE_BACKLOG", "淘宝文案解析请求过于频繁，请稍后重试")
                        p_body = self._get_response(
                            ZTK_CHANNEL_PILIANG_URL,
                            params={
                                "appkey": current_key,
                                "sid": cur_sid,
                                "pid": pid,
                                "tkl": item_url,
                                "signurl": signurl,
                            },
                        )
                        p_tao_id = str(p_body.get("tao_id") or "").strip() if isinstance(p_body, dict) else ""
                        if isinstance(p_body, dict) and p_body.get("status") == 200 and p_tao_id:
                            if relation_id:
                                rel_body = self._get_response(
                                    ZTK_CHANNEL_URL,
                                    params={
                                        "appkey": current_key,
                                        "sid": cur_sid,
                                        "pid": pid,
                                        "tkl": f"https://uland.taobao.com/item/edetail?id={p_tao_id}",
                                        "signurl": signurl,
                                        "relation_id": relation_id,
                                    },
                                )
                                if isinstance(rel_body, dict):
                                    if rel_body.get("status") == 200 and isinstance(rel_body.get("content"), list) and rel_body["content"]:
                                        status_code = 200
                                        content = rel_body["content"]
                                    else:
                                        rel_err = str(
                                            rel_body.get("content") if isinstance(rel_body.get("content"), str) else (rel_body.get("msg") or "")
                                        ).strip()
                                        rel_lower = rel_err.lower()
                                        is_key_or_sid_err = "appkey" in rel_lower or "sid" in rel_lower
                                        if not is_key_or_sid_err and (
                                            "relationid" in rel_lower
                                            or "relation_id" in rel_lower
                                            or "无效的relation" in rel_lower
                                            or any(k in rel_err for k in ("未授权", "未备案", "渠道关系无效", "渠道失效", "无效的渠道"))
                                        ):
                                            return _error(
                                                "AUTH_REQUIRED",
                                                f"折淘客渠道授权失效: {rel_err}",
                                                need_auth=True,
                                                deterministic_all_keys=True,
                                            )
                                        msg_text = rel_err or "折淘客二跳渠道转链失败"
                                else:
                                    is_key_or_sid_err = False
                                    msg_text = "折淘客二跳渠道转链响应无效"
                            else:
                                c_item = dict(p_body)
                                c_item.setdefault("pict_url", c_item.get("pic") or "")
                                c_item.setdefault("tkl", c_item.get("result_tkl") or "")
                                c_item.setdefault("coupon_click_url", c_item.get("result_url") or "")
                                c_item.setdefault("item_url", f"https://uland.taobao.com/item/edetail?id={p_tao_id}")
                                status_code = 200
                                content = [c_item]

                    if status_code != 200 or not content or not isinstance(content, list):
                        if "error tkl" in msg_text.lower() and not is_key_or_sid_err:
                            return _error("INVALID_TKL", f"折淘客渠道转链失败: {msg_text}", deterministic_all_keys=True)
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
        if not self.appkey_list or not (self.sid_list or str(self.sid or "").strip()):
            return _error("CONFIG_ERROR", "折淘客渠道核验缺少 appkey 或 sid")
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return _error("INVALID_IDENTITY", "渠道核验需要站内 UID")

        def _check_once(current_key):
            try:
                cur_sid = self._sid_for_key(current_key)
                all_matches = []
                total_count = 0
                for page_no in range(1, 11):
                    try:
                        GLOBAL_QUOTA_PACER.pace(
                            "ztk_tb_channel", current_key, 0.25,
                            http_session=self.http, foreground_max_wait=2.0, background_max_wait=10.0,
                        )
                    except QuotaPaceBacklogError:
                        return _error("QUOTA_PACE_BACKLOG", "折淘客渠道核验请求过于频繁")
                    params = {
                        "appkey": current_key,
                        "sid": cur_sid,
                        "relation_app": "common",
                        "info_type": 1,
                        "external_id": clean_uid,
                        "page_no": page_no,
                        "page_size": 100,
                    }
                    body = self._get_response(ZTK_PUBLISHER_GET_URL, params=params)
                    if isinstance(body, dict) and body.get("status") == "error":
                        return body
                    if not isinstance(body, dict):
                        return _error("PROTOCOL_ERROR", "折淘客渠道核验响应不是对象")
                    if "error_response" in body or str(body.get("status") or "") in ("301", "1000"):
                        err_detail = clean_text(
                            body.get("content")
                            if isinstance(body.get("content"), str)
                            else (body.get("msg") or (body.get("error_response") or {}).get("sub_msg") or (body.get("error_response") or {}).get("msg"))
                        )
                        return _error("UPSTREAM_ERROR", f"折淘客渠道核验接口返回业务错误: {err_detail}" if err_detail else "折淘客渠道核验接口返回业务错误")
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
                    if matches:
                        all_matches.extend(matches)
                        break

                    raw_total = resp_data.get("total_count")
                    total_count = int(raw_total) if raw_total is not None and str(raw_total).isdigit() else 0
                    if not records or (total_count > 0 and page_no * 100 >= total_count) or (total_count == 0 and len(records) < 100):
                        break

                relations = {str(r["relation_id"]).strip() for r in all_matches}
                if len(relations) > 1:
                    return _error("AMBIGUOUS_IDENTITY", "官方返回多个渠道身份，需要人工核验")
                if relations:
                    matched = all_matches[0]
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
                        "records": all_matches,
                    }
                if total_count > 1000:
                    return _error("INCOMPLETE_IDENTITY", "渠道备案总数超过分页上限，无法确认备案状态")
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
        if not code and self.appkey_list and (self.sid_list or str(self.sid or "").strip()):
            def _fetch_code(current_key):
                params = {
                    "appkey": current_key,
                    "sid": self._sid_for_key(current_key),
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
            return _error("CONFIG_ERROR", "淘宝授权缺少邀请码配置，请在 config.local.json 中配置 taobao_inviter_code")
        url = "https://mos.m.taobao.com/inviter/register?" + urlencode({
            "inviterCode": code, "src": "pub", "app": "common", "externalId": clean_uid,
        })
        return {"status": "success", "auth_url": url, "short_url": "", "we_app_info": {}}

    def search_tb_goods(self, keyword: str, page: int = 1, page_size: int = 20, sort: str = "new", has_coupon: bool = False, pid: str = ""):
        """折淘客淘宝全网搜索备用通道 (api_quanwang.ashx, 需 appkey、sid 与 4 段式 pid)。"""
        if not self.appkey_list or not (self.sid_list or str(self.sid or "").strip()):
            return _error("CONFIG_ERROR", "折淘客淘宝搜索缺少 appkey 或 sid")
        kw = clean_text(keyword)
        if not kw:
            return _error("INVALID_ARGUMENT", "请输入搜索关键词")
        clean_pid = clean_text(pid)
        pg = max(1, int(page or 1))
        sz = max(1, min(100, int(page_size or 20)))

        def _search_once(current_key):
            try:
                GLOBAL_QUOTA_PACER.pace("ztk_tb_catalog", current_key, 0.25, http_session=self.http, foreground_max_wait=2.0, background_max_wait=10.0)
            except QuotaPaceBacklogError:
                return _error("QUOTA_PACE_BACKLOG", "折淘客淘宝搜索请求过于频繁")
            try:
                params = {
                    "appkey": current_key,
                    "sid": self._sid_for_key(current_key),
                    "q": kw,
                    "page": pg,
                    "page_size": sz,
                    "sort": clean_text(sort) or "new",
                    "youquan": 1 if has_coupon else 0,
                }
                if clean_pid:
                    params["pid"] = clean_pid
                body = self._get_response(
                    ZTK_TB_QUANWANG_URL,
                    params=params,
                )
                if isinstance(body, dict) and body.get("status") == "error":
                    return body
                if not isinstance(body, dict):
                    return _error("PROTOCOL_ERROR", "折淘客淘宝搜索响应不是对象")
                if body.get("status") != 200:
                    msg = clean_text(body.get("content") or body.get("msg"))
                    if any(w in msg for w in ("频", "上限", "次数", "限流")):
                        return _error("RATE_LIMIT", msg or "折淘客淘宝搜索限流")
                    return _error("UPSTREAM_ERROR", msg or "折淘客淘宝搜索返回业务错误")
                rows = body.get("content") or []
                if not isinstance(rows, list):
                    return _error("PROTOCOL_ERROR", "折淘客淘宝搜索结果格式无效")
                return {
                    "status": "success",
                    "data": rows,
                    "page": pg,
                    "page_size": sz,
                    "has_more": len(rows) >= sz,
                }
            except Exception:
                return _error("NETWORK_ERROR", "折淘客淘宝搜索通信失败")

        return self._run_with_key_failover(_search_once)

    def get_tb_goods_detail(self, item_id: str):
        """折淘客淘宝单品详情备用通道 (api_detail.ashx)。"""
        if not self.appkey_list:
            return _error("CONFIG_ERROR", "折淘客淘宝详情缺少 appkey")
        tid = clean_text(item_id)
        if not tid:
            return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")

        def _detail_once(current_key):
            try:
                GLOBAL_QUOTA_PACER.pace("ztk_tb_catalog", current_key, 0.25, http_session=self.http, foreground_max_wait=2.0, background_max_wait=10.0)
            except QuotaPaceBacklogError:
                return _error("QUOTA_PACE_BACKLOG", "折淘客淘宝详情请求过于频繁")
            try:
                body = self._get_response(
                    ZTK_TB_DETAIL_URL,
                    params={"appkey": current_key, "tao_id": tid},
                )
                if isinstance(body, dict) and body.get("status") == "error":
                    return body
                if not isinstance(body, dict):
                    return _error("PROTOCOL_ERROR", "折淘客淘宝详情响应不是对象")
                if body.get("status") != 200:
                    msg = clean_text(body.get("content") or body.get("msg"))
                    if any(w in msg for w in ("频", "上限", "次数", "限流")):
                        return _error("RATE_LIMIT", msg or "折淘客淘宝详情限流")
                    if "下架" in msg:
                        return _error("UPSTREAM_ERROR", msg)
                    return _error("NOT_FOUND" if "未找到" in msg else "UPSTREAM_ERROR", msg or "折淘客淘宝详情查询失败")
                rows = body.get("content")
                if not isinstance(rows, list) or not rows:
                    return _error("NOT_FOUND", "折淘客未找到该淘宝商品详情")
                valid_rows = [r for r in rows if isinstance(r, dict) and clean_text(r.get("tao_id"))]
                if not valid_rows:
                    return _error("NOT_FOUND", "折淘客未找到有效淘宝商品详情")
                return {"status": "success", "data": valid_rows}
            except Exception:
                return _error("NETWORK_ERROR", "折淘客淘宝详情通信失败")

        return execute_with_key_failover(self, _detail_once, round_robin=True)

    def convert_gaoyong_url(self, item_id: str, pid: str, relation_id: str = "", signurl: int = 5):
        """折淘客高佣转链直连通道 (open_gaoyongzhuanlian.ashx, 按 num_iid 转链且含短链与淘口令)。"""
        if not self.appkey_list or not (self.sid_list or str(self.sid or "").strip()):
            return _error("CONFIG_ERROR", "折淘客高佣转链缺少 appkey 或 sid")
        tid = clean_text(item_id)
        clean_pid = clean_text(pid)
        rel = clean_text(relation_id)
        if not tid or not clean_pid:
            return _error("INVALID_ARGUMENT", "折淘客高佣转链缺少商品标识或 PID")

        def _conv_once(current_key):
            params = {
                "appkey": current_key,
                "sid": self._sid_for_key(current_key),
                "pid": clean_pid,
                "num_iid": tid,
                "signurl": signurl,
            }
            if rel:
                params["relation_id"] = rel
            try:
                GLOBAL_QUOTA_PACER.pace("ztk_tb_channel", current_key, 0.25, http_session=self.http, foreground_max_wait=3.0, background_max_wait=12.0)
            except QuotaPaceBacklogError:
                return _error("QUOTA_PACE_BACKLOG", "折淘客高佣转链请求过于频繁")
            try:
                body = self._get_response(ZTK_TB_GAOYONG_URL, params=params)
                if isinstance(body, dict) and body.get("status") == "error":
                    return body
                if not isinstance(body, dict):
                    return _error("PROTOCOL_ERROR", "折淘客高佣转链响应不是对象")
                if body.get("status") != 200 or not isinstance(body.get("content"), list) or not body["content"]:
                    msg = clean_text(body.get("content") if isinstance(body.get("content"), str) else (body.get("msg") or body.get("error_msg")))
                    msg_l = msg.lower()
                    is_key_or_sid_err = "appkey" in msg_l or "sid" in msg_l
                    if not is_key_or_sid_err and (
                        "relationid" in msg_l
                        or "relation_id" in msg_l
                        or "无效的relation" in msg_l
                        or (bool(rel) and bool(re.search(r"(?:用户|推手|渠道).{0,12}(?:未授权|未备案|失效|无效)", msg)))
                    ):
                        return _error("AUTH_REQUIRED", f"折淘客渠道授权失效: {msg}", need_auth=True, deterministic_all_keys=True)
                    return _error("UPSTREAM_ERROR", msg or "折淘客高佣转链失败")
                c = body["content"][0]
                if not isinstance(c, dict):
                    return _error("PROTOCOL_ERROR", "折淘客高佣转链内容格式无效")
                target_url = normalize_http_url(c.get("coupon_click_url") or c.get("item_url"))
                short_url = normalize_http_url(c.get("shorturl"))
                tpwd = clean_text(c.get("tkl"))
                if not target_url and not short_url:
                    return _error("PROTOCOL_ERROR", "折淘客高佣转链未返回有效链接")
                return {
                    "status": "success",
                    "title": c.get("title") or c.get("tao_title") or "淘宝精选商品",
                    "final_price": str(c.get("quanhou_jiage") or c.get("size") or "0.00"),
                    "commission_rate": str(c.get("tkrate3") or "0"),
                    "commission_amount": str(c.get("tkfee3") or "0.00"),
                    "is_channel": bool(rel),
                    "pid": clean_pid,
                    "relation_id": rel,
                    "url": target_url or short_url,
                    "final_link": short_url or target_url,
                    "short_url": short_url or target_url,
                    "mobile_url": target_url or short_url,
                    "tpwd": tpwd,
                    "raw": c,
                }
            except Exception:
                return _error("NETWORK_ERROR", "折淘客高佣转链通信失败")

        return self._run_with_key_failover(_conv_once)

    def resolve_item_id(self, content: str, pid: str = ""):
        """折淘客淘宝口令/链接转商品ID接口 (open_shangpin_id.ashx, 需 appkey、sid 与可选 4 段式 pid)。"""
        if not self.appkey_list or not (self.sid_list or str(self.sid or "").strip()):
            return _error("CONFIG_ERROR", "折淘客口令解ID缺少 appkey 或 sid")
        text = clean_text(content)
        if not text:
            return _error("INVALID_ARGUMENT", "缺少口令或商品链接")
        clean_pid = clean_text(pid)

        def _resolve_once(current_key):
            try:
                GLOBAL_QUOTA_PACER.pace(
                    "ztk_tb_channel", current_key, 0.25,
                    http_session=self.http, foreground_max_wait=2.0, background_max_wait=10.0,
                )
            except QuotaPaceBacklogError:
                return _error("QUOTA_PACE_BACKLOG", "折淘客解ID请求过于频繁")
            try:
                params = {"appkey": current_key, "sid": self._sid_for_key(current_key), "content": text}
                if clean_pid:
                    params["pid"] = clean_pid
                body = self._get_response(
                    ZTK_TB_SHANGPIN_ID_URL,
                    params=params,
                )
                if isinstance(body, dict) and body.get("status") == "error":
                    return body
                if not isinstance(body, dict):
                    return _error("PROTOCOL_ERROR", "折淘客解ID响应不是对象")
                iid = clean_text(body.get("item_id"))
                if iid and iid != "0":
                    return {"status": "success", "item_id": iid, "raw": body}
                msg = clean_text(body.get("content") if isinstance(body.get("content"), str) else (body.get("msg") or ""))
                if any(w in msg for w in ("频", "上限", "次数", "限流")):
                    return _error("RATE_LIMIT", msg or "折淘客解ID限流")
                status_val = str(body.get("status") or "")
                msg_l = msg.lower()
                if ("appkey" in msg_l or "sid" in msg_l) or (
                    status_val in ("301", "1000") and not any(w in msg for w in ("未找到", "无结果", "解析失败"))
                ):
                    return _error("UPSTREAM_ERROR", msg or "折淘客解ID业务错误")
                return _error("NOT_FOUND", msg or "折淘客未能解析出淘宝商品ID")
            except Exception:
                return _error("NETWORK_ERROR", "折淘客解ID通信失败")

        return self._run_with_key_failover(_resolve_once)
