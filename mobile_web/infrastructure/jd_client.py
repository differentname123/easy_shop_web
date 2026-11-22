# -*- coding: utf-8 -*-
"""京东/折京客薄协议边界；支持多 ZTK_APPKEY_LIST 自动轮询与故障切换，与 scripts/test_jd_api.py 严格对齐。"""
import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlsplit

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
    parse_appkey_list,
)
from infrastructure.logging_utils import get_app_logger
from infrastructure.transport import create_http_session
from models.product import is_jd_image_key
from models.user import normalize_promoter_id

logger = get_app_logger(__name__)

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


def jd_coupon_url(value):
    """Validate JD official coupon URLs (coupon.m.jd.com / coupon.jd.com) without fetching them."""
    if not isinstance(value, str):
        return ""
    raw = value.strip().split("#", 1)[0].strip()
    url = product_url(raw)
    if not url:
        return ""
    host = (urlsplit(url).hostname or "").lower()
    return url if (host in ("coupon.m.jd.com", "coupon.jd.com") or host.endswith(".coupon.jd.com")) else ""


JD_OFFICIAL_VENUE_URL_MARKERS = (
    "coupon.m.jd.com",
    "coupon.jd.com",
    "pro.m.jd.com",
    "prodev.m.jd.com",
    "h5.m.jd.com",
    "jdcoupon",
    "u_act_p=coupon",
)


def is_jd_official_venue_url(text: str) -> bool:
    """判断文本/URL是否命中京东官方公开领券/活动会场域名白名单。"""
    lower = str(text or "").lower()
    return any(marker in lower for marker in JD_OFFICIAL_VENUE_URL_MARKERS)


def is_encrypted_jd_item_id(value) -> bool:
    """识别京东联盟官方加密 itemId（如榜单/搜索返回的字母数字下划线混合加密 ID），排除主图 CDN 哈希键。"""
    text = str(value or "").strip()
    if not text or is_jd_image_key(text):
        return False
    return len(text) >= 25 and "_" in text and all(c.isalnum() or c == "_" for c in text)


def _is_trusted_jd_redirect_url(candidate: Any) -> bool:
    """校验短链跳转/落地 URL 必须为合法无凭证且属于 jd.com / 3.cn 官方域名（防止 SSRF）。"""
    if not isinstance(candidate, str):
        return False
    raw = candidate.strip()
    if not raw or "\\" in raw or any(ord(c) < 33 for c in raw):
        return False
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme in ("http", "https")
        and bool(host)
        and parts.username is None
        and parts.password is None
        and (host == "jd.com" or host.endswith(".jd.com") or host == "3.cn" or host.endswith(".3.cn"))
    )


def resolve_jd_short_url(short_url: str, *, http_session: Optional[Any] = None) -> Dict[str, Any]:
    """
    轻量解包京东 u.jd.com 推广短链，判定是活动会场页还是单品商品：
    1. 跟随一次内部 302/hrl 跳转拿到落地真实 URL；
    2. 若落地为活动/会场/券包（pro.m.jd.com、jingfen 活动包、coupon.m.jd.com）：返回 is_venue=True；
    3. 若落地为单品页（item.m.jd.com / item.jd.com / skuId）：提取确定性 sku_id 返回。
    """
    url = clean_text(short_url)
    if not url or "\\" in url or any(ord(c) < 33 for c in url):
        return _error("INVALID_ARGUMENT", "短链格式无效")
    try:
        in_parts = urlsplit(url)
    except ValueError:
        return _error("INVALID_ARGUMENT", "短链格式无效")
    if in_parts.scheme.lower() not in ("http", "https") or not in_parts.hostname or in_parts.username is not None or in_parts.password is not None:
        return _error("INVALID_ARGUMENT", "短链格式无效")
    if in_parts.hostname.lower() != "u.jd.com":
        return _error("NOT_SUPPORTED", "仅支持 u.jd.com 短链解包")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        )
    }
    owned_session = http_session is None
    sess = create_http_session(pool_connections=4, pool_maxsize=4) if owned_session else http_session
    dest_url = ""
    try:
        timeout = (2.0, 3.5)
        resp = sess.get(url, headers=headers, timeout=timeout)
        if getattr(resp, "status_code", 0) == 200:
            m = re.search(r"var\s+hrl\s*=\s*['\"]([^'\"]+)['\"]", getattr(resp, "text", "") or "")
            if m:
                hrl = m.group(1).strip()
                if not _is_trusted_jd_redirect_url(hrl):
                    return _error("UNSAFE_REDIRECT", "拒绝非京东域名的短链跳转地址")
                r2 = sess.get(hrl, headers=headers, allow_redirects=True, timeout=timeout)
                dest_url = str(getattr(r2, "url", "") or "").strip()
            elif getattr(resp, "url", "") and resp.url != url:
                dest_url = str(resp.url).strip()
        elif getattr(resp, "status_code", 0) in (301, 302, 303, 307, 308) and getattr(resp, "headers", None):
            dest_url = str(resp.headers.get("Location") or "").strip()
    except Exception as e:
        logger.debug("[JD短链解包] 访问异常: url=%s, err=%s", url, e)
        return _error("UPSTREAM_ERROR", f"京东短链解包失败: {e}")
    finally:
        if owned_session and hasattr(sess, "close"):
            try:
                sess.close()
            except Exception:
                pass

    if not dest_url:
        return _error("NOT_FOUND", "未能解包该京东短链落地页")
    if not _is_trusted_jd_redirect_url(dest_url):
        return _error("UNSAFE_REDIRECT", "短链最终落地地址不属于京东官方域名")

    parts = urlsplit(dest_url)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    qs = parse_qs(parts.query)

    sku_id = ""
    for param_key in ("sku", "mainSku", "skuId"):
        cand = qs.get(param_key, [""])[0].strip()
        if cand:
            sku_id = cand
            break

    if not sku_id:
        path_m = re.search(r"/(?:product|item)/(\d+)\.html", path, re.IGNORECASE)
        if path_m:
            sku_id = path_m.group(1)

    if not sku_id and "returnurl" in qs:
        ret_url = unquote(qs["returnurl"][0])
        ret_parts = urlsplit(ret_url)
        ret_m = re.search(r"/(?:product|item)/(\d+)\.html", ret_parts.path, re.IGNORECASE) or re.search(r"[?&]sku(?:Id)?=([A-Za-z0-9_]+)", ret_parts.query)
        if ret_m:
            sku_id = ret_m.group(1)

    is_venue = False
    if not sku_id:
        is_venue = (
            is_jd_official_venue_url(dest_url)
            or host in ("shop.m.jd.com", "mall.jd.com")
            or (
                host == "jingfen.jd.com"
                and (path == "/item" or "union-activity" in dest_url or "union_page_id" in dest_url)
            )
        )

    return {
        "status": "success",
        "data": {
            "dest_url": dest_url,
            "is_venue": is_venue,
            "sku_id": sku_id,
        },
    }


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
        if http_session is not None:
            self.http = http_session
        else:
            self.http = create_http_session(pool_connections=16, pool_maxsize=32)
        self.timeout = timeout
        self._catalog_cooldown_lock = threading.Lock()
        self._catalog_cooldown_until = 0.0
        self._catalog_cooldown_streak = 0

    _MIN_KEY_DISPATCH_INTERVAL_SEC = 0.22
    _DETAIL_KEY_DISPATCH_INTERVAL_SEC = 1.35
    _CATALOG_BURST_COOLDOWN_SEC = 65.0
    _CATALOG_COOLDOWN_STEPS_SEC = (65.0, 120.0, 240.0, 300.0)
    _MAX_KEY_PACE_WAIT_SEC = 2.0
    _MAX_DETAIL_KEY_PACE_WAIT_SEC = 3.0
    _MAX_BG_PACE_WAIT_SEC = 12.0
    _SHARED_CATALOG_QUOTA_ENDPOINTS = (
        "api_detail.ashx",
        "api_quanwang.ashx",
        "open_gaoyongzhuanlian_tkl_piliang.ashx",
    )

    def is_catalog_cooling(self) -> bool:
        """折淘客京东选品库 (tkl_piliang / api_detail / api_quanwang) 是否处于短时频控冷却窗口内。"""
        with self._catalog_cooldown_lock:
            return time.monotonic() < self._catalog_cooldown_until

    def catalog_cooldown_remaining(self) -> float:
        """返回折淘客京东选品库当前剩余冷却秒数（未处于冷却期时返回 0.0）。"""
        with self._catalog_cooldown_lock:
            return max(0.0, self._catalog_cooldown_until - time.monotonic())

    def reset_catalog_cooldown_streak(self) -> None:
        """选品库接口恢复正常返回时重置连续冷却退避计数。"""
        with self._catalog_cooldown_lock:
            self._catalog_cooldown_streak = 0

    def mark_catalog_rate_limited(
        self,
        cooldown_sec: Optional[float] = None,
        *,
        force: bool = False,
    ) -> None:
        """确证触发折淘客短时突发限流后开启全局选品库冷却窗口（带上限的阶梯退避），避免多 Key/并发自伤雪崩。"""
        if not force and not is_real_requests_session(self.http):
            return
        with self._catalog_cooldown_lock:
            now = time.monotonic()
            if cooldown_sec is None:
                if now < self._catalog_cooldown_until:
                    return
                step_idx = min(self._catalog_cooldown_streak, len(self._CATALOG_COOLDOWN_STEPS_SEC) - 1)
                dur = self._CATALOG_COOLDOWN_STEPS_SEC[step_idx]
                self._catalog_cooldown_until = now + dur
                self._catalog_cooldown_streak += 1
            else:
                self._catalog_cooldown_until = max(
                    self._catalog_cooldown_until,
                    now + max(0.0, float(cooldown_sec)),
                )

    def _pace_real_http_by_key(self, kwargs, *, url=""):
        """委托 GLOBAL_QUOTA_PACER 按单 appkey 预约发包时间槽；详情/搜索/批量解析共享折淘客 1000 限流池统一节流。"""
        params = kwargs.get("params")
        appkey = str(params.get("appkey") or "") if isinstance(params, dict) else ""
        if not appkey:
            return 0.0
        url_str = str(url or "")
        is_catalog_pool = any(ep in url_str for ep in self._SHARED_CATALOG_QUOTA_ENDPOINTS)
        return GLOBAL_QUOTA_PACER.pace(
            "ztk_jd_catalog" if is_catalog_pool else "ztk_jd_general",
            appkey,
            self._DETAIL_KEY_DISPATCH_INTERVAL_SEC if is_catalog_pool else self._MIN_KEY_DISPATCH_INTERVAL_SEC,
            http_session=self.http,
            foreground_max_wait=self._MAX_DETAIL_KEY_PACE_WAIT_SEC if is_catalog_pool else self._MAX_KEY_PACE_WAIT_SEC,
            background_max_wait=self._MAX_BG_PACE_WAIT_SEC,
        )

    @staticmethod
    def _endpoint_op_name(url: str) -> str:
        raw = str(url or "").split("?", 1)[0].rstrip("/")
        tail = raw.rsplit("/", 1)[-1] or "http_get"
        return tail[:-5] if tail.endswith(".ashx") else tail

    def _get(self, url, **kwargs):
        is_real = is_real_requests_session(self.http)
        op_name = self._endpoint_op_name(url)
        is_catalog_pool = any(ep in str(url or "") for ep in self._SHARED_CATALOG_QUOTA_ENDPOINTS)
        if is_catalog_pool and self.is_catalog_cooling():
            GLOBAL_UPSTREAM_HEALTH.record_call(
                "jd",
                "ztk_jd",
                op_name,
                outcome="rate_limit",
                latency_ms=0.0,
                error_code="CATALOG_COOLDOWN",
                error_msg="京东选品库处于短时频控冷却中",
                allow_persist=is_real,
            )
            return _error("RATE_LIMIT", "京东选品库处于短时频控冷却中，暂跳过避免雪崩", catalog_cooling=True)
        t0 = time.perf_counter()
        try:
            self._pace_real_http_by_key(kwargs, url=url)
            response = self.http.get(url, timeout=self.timeout, allow_redirects=False, **kwargs)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            if 300 <= response.status_code < 400:
                GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="error", latency_ms=dt_ms, error_code="UNSAFE_REDIRECT", allow_persist=is_real)
                return _error("UNSAFE_REDIRECT", "京东接口重定向被拒绝")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="error", latency_ms=dt_ms, error_code="PROTOCOL_ERROR", allow_persist=is_real)
                return _error("PROTOCOL_ERROR", "京东响应不是对象")
            if body.get("status") == "error" or "error" in body or "error_response" in body:
                GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="error", latency_ms=dt_ms, error_code="UPSTREAM_ERROR", allow_persist=is_real)
                return _error("UPSTREAM_ERROR", "京东接口返回业务错误")
            st_str = str(body.get("status") or "")
            if st_str in ("300", "301"):
                GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="degraded", latency_ms=dt_ms, error_code=f"ZTK_{st_str}", error_msg=str(body.get("content") or "")[:80], allow_persist=is_real)
            elif op_name == "open_gaoyongzhuanlian_tkl_piliang" and not str(body.get("tao_id") or "").strip():
                GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="degraded", latency_ms=dt_ms, error_code="ZTK_EMPTY_TAO_ID", error_msg="status=200 with empty tao_id", allow_persist=is_real)
            else:
                GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="success", latency_ms=dt_ms, allow_persist=is_real)
                if is_catalog_pool:
                    self.reset_catalog_cooldown_streak()
            return body
        except QuotaPaceBacklogError:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="rate_limit", latency_ms=dt_ms, error_code="QUOTA_PACE_BACKLOG", allow_persist=is_real)
            return _error("QUOTA_PACE_BACKLOG", "京东接口请求过于频繁，请稍后重试")
        except ValueError:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="error", latency_ms=dt_ms, error_code="PROTOCOL_ERROR", allow_persist=is_real)
            return _error("PROTOCOL_ERROR", "京东响应不是有效JSON")
        except Exception as exc:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            GLOBAL_UPSTREAM_HEALTH.record_call("jd", "ztk_jd", op_name, outcome="error", latency_ms=dt_ms, error_code=type(exc).__name__, allow_persist=is_real)
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

    def _execute_with_failover(self, endpoint, params, evaluator, *, orders=False, extra_stop_early=None):
        """
        使用 `self.appkey_list` 执行请求（委托 `execute_with_key_failover` 共享健康轮询与容灾机制）：
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
            should_stop_early=lambda res: (
                (isinstance(res, dict) and res.get("catalog_cooling") is True)
                or (isinstance(last_body, dict) and (
                    PROMOTION_ROOT in last_body or RANK_ROOT in last_body or ORDER_ROOT in last_body
                ))
                or (callable(extra_stop_early) and extra_stop_early(res))
            ),
            round_robin=True,
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

    def search_goods(self, keyword, page=1, page_size=20, *, has_coupon=False):
        try:
            page, size = self._page(page, page_size)
            query = keyword.strip()
            while len(query) < 4:
                query += query
        except (TypeError, ValueError, AttributeError, OverflowError):
            return _error("INVALID_ARGUMENT", "京东搜索关键词或分页无效")
        config_error = self._validate_config()
        if config_error is not None:
            return config_error
        ambiguous_empty_hits = 0

        def _evaluate_search(body):
            nonlocal ambiguous_empty_hits
            if (
                isinstance(body, dict)
                and str(body.get("status")) in ("300", "301")
                and any(
                    kw in str(body.get("content") or body.get("msg") or "")
                    for kw in ("无符合条件的数据", "无数据", "没有数据", "暂无数据", "没有找到")
                )
            ):
                ambiguous_empty_hits += 1
                return _error("UPSTREAM_ERROR", "京东商品接口返回业务错误", ztk_ambiguous_empty=True)
            return self._content(body, page, size)

        res = self._execute_with_failover(
            "api_quanwang.ashx",
            {"unionId": self.union_id, "q": query, "page": page, "page_size": size,
             "sort": "new", "youquan": 1 if has_coupon else 0, "type": 2},
            _evaluate_search,
            extra_stop_early=lambda _r: ambiguous_empty_hits >= 2,
        )
        if res.get("ztk_ambiguous_empty"):
            return {"status": "success", "data": [], "has_more": False, "total_count": 0}
        if res.get("ztk_explicit_rate_limit"):
            self.mark_catalog_rate_limited()
        return res

    def get_goods_detail(self, goods_id):
        if not isinstance(goods_id, str) or not goods_id.strip():
            return _error("INVALID_ARGUMENT", "京东详情缺少商品ID")
        res = self._execute_with_failover(
            "api_detail.ashx",
            {"unionId": self.union_id, "tao_id": goods_id, "type": 1},
            self._content,
        )
        if res.get("ztk_explicit_rate_limit"):
            self.mark_catalog_rate_limited()
        return res

    def _content(self, body, page=None, size=None):
        if body.get("status") == "error":
            return body
        if str(body.get("status")) != "200":
            c_msg = str(body.get("content") or "")
            if str(body.get("status")) == "301":
                if any(kw in c_msg for kw in ("1000", "频繁", "频控", "上限", "超限")):
                    return _error("RATE_LIMIT", "京东选品库接口触发频控", ztk_explicit_rate_limit=True)
                if "下架" in c_msg:
                    return _error("UPSTREAM_ERROR", "京东商品接口返回业务错误", ztk_ambiguous_301=True)
            return _error("UPSTREAM_ERROR", "京东商品接口返回业务错误")
        if not _rows(body.get("content")):
            return _error("PROTOCOL_ERROR", "京东商品content列表无效")
        return (self._catalog_page(body["content"], page, size, body) if page is not None else
                {"status": "success", "data": body["content"]})

    def convert_promotion_link(self, material_url, promoter_id, coupon_url=None):
        promoter = canonical_promoter_id(promoter_id)
        material = product_url(material_url)
        if not promoter or not material:
            return _error("INVALID_ARGUMENT", "京东转链缺少有效物料URL或数字推广ID")
        clean_coupon = jd_coupon_url(coupon_url)

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

        base_params = {"unionId": self.union_id, "materialId": material, "chainType": "3", "positionId": promoter}
        if clean_coupon:
            coupon_res = self._execute_with_failover(
                "open_jing_union_open_promotion_byunionid_get.ashx",
                {**base_params, "couponUrl": clean_coupon},
                _evaluate,
            )
            if (
                coupon_res.get("status") == "success"
                or coupon_res.get("jd_off_promotion") is True
                or coupon_res.get("error_code") in ("CONFIG_ERROR", "UNSAFE_REDIRECT", "NETWORK_ERROR")
            ):
                return coupon_res
        return self._execute_with_failover(
            "open_jing_union_open_promotion_byunionid_get.ashx",
            base_params,
            _evaluate,
        )

    def convert_universal_material(self, raw_material, promoter_id, *, short_url_resolver=None):
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

        primary_ok = primary_res.get("status") == "success"
        known_single_item = False
        if primary_ok:
            lower_mat = material.lower()
            is_known_jingfen_or_venue = (
                "jingfen.jd.com/detail/" in lower_mat
                or is_jd_official_venue_url(lower_mat)
            )
            if is_known_jingfen_or_venue:
                return primary_res

            # Zero-quota short-URL unwrap before hitting rate-limited tkl_piliang:
            # catches shop/venue pages (0 catalog calls) and Babel encrypted SKUs (skip tkl_piliang -> 1 api_detail call).
            if callable(short_url_resolver) and primary_res.get("short_url"):
                try:
                    unwrap_res = short_url_resolver(primary_res["short_url"])
                except Exception:
                    unwrap_res = None
                if isinstance(unwrap_res, dict) and unwrap_res.get("status") == "success":
                    u_data = unwrap_res.get("data") or {}
                    u_sku = str(u_data.get("sku_id") or "").strip()
                    if u_data.get("is_venue") and not u_sku:
                        primary_res["resolved_venue"] = True
                        return primary_res
                    if u_sku and is_encrypted_jd_item_id(u_sku):
                        primary_res["tao_id"] = u_sku
                        return primary_res
                    if u_sku:
                        known_single_item = True
                        primary_res["known_single_item"] = True

        piliang_empty_tao_id_hits = 0

        def _evaluate_piliang(body):
            nonlocal piliang_empty_tao_id_hits
            if body.get("status") == "error":
                return body
            if str(body.get("status")) != "200":
                c_msg = str(body.get("content") or "")
                if str(body.get("status")) == "301" and any(kw in c_msg for kw in ("1000", "频繁", "频控", "上限", "超限")):
                    return _error("RATE_LIMIT", "京东批量转链触发频控", ztk_explicit_rate_limit=True)
                return _error("UPSTREAM_ERROR", "京东批量文案转链未成功")
            p_tao_id = str(body.get("tao_id") or "").strip()
            p_res_url = product_url(body.get("result_url"))
            p_res_tkl = str(body.get("result_tkl") or "").strip()
            # 当 primary_res 已成功获取短链时，调用 piliang 的唯一目的是拿加密 tao_id 与商品详情；
            # 若单 Key 因频控返回空 tao_id，触发多 Key 轮询切换至健康 Key
            if primary_ok and not p_tao_id:
                piliang_empty_tao_id_hits += 1
                return _error("UPSTREAM_ERROR", "京东批量转链未返回商品ID")
            if not (p_tao_id or p_res_url):
                return _error("UPSTREAM_ERROR", "京东批量文案转链未解析出商品")
            raw_item = None
            if p_tao_id:
                raw_item = dict(body)
                raw_item.setdefault("pict_url", raw_item.get("pic") or "")
                raw_item.setdefault("shop_title", raw_item.get("nick") or "")
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

        piliang_tkl = (
            (primary_res.get("short_url") or material)
            if primary_ok
            else material
        )
        piliang_res = self._execute_with_failover(
            "open_gaoyongzhuanlian_tkl_piliang.ashx",
            {"unionId": self.union_id, "positionId": promoter, "tkl": piliang_tkl, "signurl": 5},
            _evaluate_piliang,
            extra_stop_early=lambda _r: piliang_empty_tao_id_hits >= 2,
        )
        if piliang_res.get("ztk_explicit_rate_limit"):
            self.mark_catalog_rate_limited()
        if primary_ok:
            if piliang_res.get("status") == "success":
                if piliang_res.get("tao_id"):
                    primary_res["tao_id"] = piliang_res["tao_id"]
                if piliang_res.get("raw_item"):
                    primary_res["raw_item"] = piliang_res["raw_item"]
                if not primary_res.get("j_command") and piliang_res.get("j_command"):
                    primary_res["j_command"] = piliang_res["j_command"]
                if not primary_res.get("j_short_command") and piliang_res.get("j_short_command"):
                    primary_res["j_short_command"] = piliang_res["j_short_command"]
            elif (
                piliang_res.get("catalog_cooling") is True
                or piliang_res.get("error_code") == "RATE_LIMIT"
            ):
                primary_res["piliang_throttled"] = True
                if piliang_res.get("catalog_cooling") is True:
                    primary_res["catalog_cooling"] = True
            elif piliang_empty_tao_id_hits >= 2:
                primary_res["piliang_empty_tao_id"] = True
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
