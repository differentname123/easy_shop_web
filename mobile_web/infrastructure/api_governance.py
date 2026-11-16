# -*- coding: utf-8 -*-
"""
统一上游商品与推广 API 治理层 (Upstream API Governance & Observability)
================================================================================
核心职责：
1. 统一配额池节流 (`UpstreamQuotaPacer`)：
   - 按真实上游共享配额池（如折淘客京东商品/搜索/批量转链共享池 `ztk_jd_catalog`）统一预约发包时间槽；
   - 区分调用上下文 (`api_caller_context`)：Web 前台默认最长等待 `3.0s` 保障交互响应，后台批处理任务默认最长等待 `12.0s` 平滑削峰不丢单；
   - 自动识别离线单元测试中的 Mock Session，测试态零等待透传。
2. 四态确定性解析契约 (`ResolutionKind`)：
   - `product`          : 已拿到完整可入库单品详情（非空 ID、标题、正数价格、主图）及推广链/口令；
   - `degraded_product` : 确证输入为具体单品且转链成功，但因上游详情限流/故障未能拿到完整商品卡片（前台保底返链，后台严禁误判为纯优惠券或写脏库）；
   - `coupon_or_venue`  : 确认为纯优惠券、店铺或活动会场链接；
   - `error`            : 明确失败（区分下架/不支持等永久错误与限流/网络等暂态错误）。
3. 可插拔多供应商降级链 (`execute_provider_chain`)：
   - 标准化主备 API 编排，遇到限流/网络异常/残缺降级自动顺延备用供应商，遇到下架/未授权等确定性业务结果立即短路返回；
   - 后续新增大淘客、订单侠或官方直连 API 时，仅需向平台供应商列表注册一个函数，上层业务与清洗调度零改动。
4. 跨进程可观测指标注册表 (`GLOBAL_UPSTREAM_HEALTH`)：
   - 实时统计各 `(platform, provider, operation)` 的调用量、成功率、限流次数、伪成功降级次数、平均与 P95 耗时；
   - 真实运行态按进程角色低频原子落盘至 `logs/runtime/upstream_health_<role>.json`，供 `/api/system/upstream-health` 汇总展示。
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple
import uuid

import requests


class ResolutionKind:
    PRODUCT = "product"
    DEGRADED_PRODUCT = "degraded_product"
    COUPON_OR_VENUE = "coupon_or_venue"
    ERROR = "error"


NON_FALLBACK_DETERMINISTIC_ERRORS = frozenset({
    "PRODUCT_OFF_PROMOTION",
    "AUTH_REQUIRED",
    "IDENTITY_MISMATCH",
    "AMBIGUOUS_IDENTITY",
    "INVALID_IDENTITY",
    "INCOMPLETE_IDENTITY",
    "INVALID_ARGUMENT",
    "INVALID_PROMOTER_ID",
    "INVALID_PDD_URL",
    "UNSUPPORTED_JD_NUMERIC_SKU",
    "UNSUPPORTED_TB_NUMERIC_ID",
    "UNSUPPORTED_PDD_ID_ONLY_URL",
    "UNSUPPORTED_TB_PORTAL_PAGE",
    "UNSAFE_REDIRECT",
    "UNTRUSTED_GATEWAY",
})

RATE_LIMIT_ERROR_CODES = frozenset({
    "RATE_LIMIT",
    "QUOTA_PACE_BACKLOG",
})

_CALLER_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "upstream_api_caller_context",
    default="foreground",
)

_PROCESS_ROLE: str = "web"
_APP_DIR = Path(__file__).resolve().parent.parent
_HEALTH_DIR = _APP_DIR / "logs" / "runtime"


def set_process_role(role: str) -> None:
    """标记当前进程角色（如 'web' | 'deal_scheduler' | 'xianyu_scheduler'），用于跨进程健康快照隔离。"""
    global _PROCESS_ROLE
    clean = "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in str(role or "").strip())
    if clean:
        _PROCESS_ROLE = clean[:32]


def get_caller_context() -> str:
    return _CALLER_CONTEXT.get()


@contextmanager
def api_caller_context(mode: str):
    """设置当前调用链的流量上下文：'foreground'（Web 实时请求）或 'background'（后台批处理调度）。"""
    normalized = "background" if str(mode or "").strip().lower() == "background" else "foreground"
    token = _CALLER_CONTEXT.set(normalized)
    try:
        yield normalized
    finally:
        _CALLER_CONTEXT.reset(token)


def is_real_requests_session(http_session: Any) -> bool:
    """仅当底层为真实未被打桩的 requests.Session 时返回 True，确保单元测试零睡眠、零磁盘副作用。"""
    if type(http_session) is not requests.Session:
        return False
    for attr in ("get", "post"):
        fn = getattr(http_session, attr, None)
        if fn is not None and not getattr(fn, "__module__", "").startswith("requests"):
            return False
    base_req = getattr(requests.sessions.Session, "request", None)
    if not getattr(base_req, "__module__", "").startswith("requests"):
        return False
    return True


class QuotaPaceBacklogError(RuntimeError):
    """当前配额池排队等待时长超过上下文最大容忍阈值时抛出。"""


class UpstreamQuotaPacer:
    """线程安全的共享配额池节流器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed_ts: Dict[Tuple[str, str], float] = {}

    def pace(
        self,
        pool_name: str,
        key_id: str,
        interval_sec: float,
        *,
        http_session: Any = None,
        foreground_max_wait: float = 3.0,
        background_max_wait: float = 12.0,
    ) -> float:
        if interval_sec <= 0 or not key_id:
            return 0.0
        if http_session is not None and not is_real_requests_session(http_session):
            return 0.0

        ctx = get_caller_context()
        max_wait = float(background_max_wait if ctx == "background" else foreground_max_wait)
        slot_key = (str(pool_name), str(key_id))

        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed_ts.get(slot_key, 0.0))
            wait_s = scheduled - now
            if max_wait > 0 and wait_s > max_wait:
                raise QuotaPaceBacklogError(
                    f"quota pool {pool_name} backlog {wait_s:.2f}s > max_wait {max_wait:.2f}s ({ctx})"
                )
            self._next_allowed_ts[slot_key] = scheduled + float(interval_sec)

        if wait_s > 0:
            time.sleep(wait_s)
        return wait_s

    def reset(self) -> None:
        with self._lock:
            self._next_allowed_ts.clear()


GLOBAL_QUOTA_PACER = UpstreamQuotaPacer()


def is_valid_product_card(product: Any) -> bool:
    """校验商品卡片是否具备完整核心身份与价格（对齐各适配器契约，不因偶发缺主图而误杀真实商品）。"""
    if not isinstance(product, dict):
        return False
    gid = str(product.get("goods_id") or product.get("product_id") or "").strip()
    gsign = str(product.get("goods_sign") or "").strip()
    if (not gid or gid in ("0", "None")) and (not gsign or gsign in ("0", "None")):
        return False
    name = str(product.get("name") or product.get("title") or "").strip()
    if not name:
        return False
    img = str(product.get("image_url") or "").strip()
    if img and not img.startswith(("http://", "https://", "//")):
        return False
    raw_price = product.get("activity_price") if product.get("activity_price") is not None else product.get("price")
    if raw_price is None or isinstance(raw_price, bool):
        return False
    try:
        p_val = float(raw_price)
    except (TypeError, ValueError):
        return False
    return math.isfinite(p_val) and p_val >= 0.0


def stamp_universal_resolution(
    result: Dict[str, Any],
    *,
    verified_coupon_or_venue: Optional[bool] = None,
    single_item_evidence: bool = False,
) -> Dict[str, Any]:
    """
    为 `resolve_and_convert_universal` 返回结果打上确定性的四态 `resolution_kind` 标签：
    - 若 status != 'success' -> `error`
    - 若包含通过校验的 `product` 卡片 -> `product`
    - 若转链成功但缺失商品卡片：优先依据正向实证 `verified_coupon_or_venue` 判定——
      仅明确证实为纯券/活动会场时归为 `coupon_or_venue`，其余未拿到商品卡的单品/未知短链归为 `degraded_product`
    """
    if not isinstance(result, dict):
        return {
            "status": "error",
            "resolution_kind": ResolutionKind.ERROR,
            "error_code": "PROTOCOL_ERROR",
            "error_msg": "上游解析结果格式无效",
        }
    if result.get("status") != "success":
        result["resolution_kind"] = ResolutionKind.ERROR
        return result

    product = result.get("product")
    if is_valid_product_card(product):
        result["resolution_kind"] = ResolutionKind.PRODUCT
        return result

    if verified_coupon_or_venue is True and not result.get("detail_lookup_failed"):
        result["resolution_kind"] = ResolutionKind.COUPON_OR_VENUE
        return result

    if verified_coupon_or_venue is False or result.get("detail_lookup_failed") or single_item_evidence:
        result["detail_lookup_failed"] = True
        result["resolution_kind"] = ResolutionKind.DEGRADED_PRODUCT
        return result

    result["resolution_kind"] = ResolutionKind.COUPON_OR_VENUE
    return result


class UpstreamHealthRegistry:
    """线程安全的上游商品/转链 API 实时健康度与耗时监控注册表。"""

    SNAPSHOT_FLUSH_INTERVAL_SEC = 15.0
    SNAPSHOT_MAX_AGE_SEC = 600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._last_flush_mono = 0.0

    def record_call(
        self,
        platform: str,
        provider: str,
        operation: str,
        *,
        outcome: str,
        latency_ms: float = 0.0,
        error_code: str = "",
        error_msg: str = "",
        allow_persist: bool = False,
    ) -> None:
        plat = str(platform or "unknown").strip().lower()
        prov = str(provider or "default").strip().lower()
        op = str(operation or "call").strip().lower()
        norm_outcome = outcome if outcome in ("success", "rate_limit", "degraded", "error") else "error"
        lat = max(0.0, float(latency_ms or 0.0)) if math.isfinite(float(latency_ms or 0.0)) else 0.0
        now_iso = datetime.now(timezone.utc).isoformat()
        should_flush = False

        with self._lock:
            key = (plat, prov, op)
            bucket = self._metrics.get(key)
            if bucket is None:
                bucket = {
                    "platform": plat,
                    "provider": prov,
                    "operation": op,
                    "total_calls": 0,
                    "success_calls": 0,
                    "rate_limit_calls": 0,
                    "degraded_calls": 0,
                    "error_calls": 0,
                    "total_latency_ms": 0.0,
                    "max_latency_ms": 0.0,
                    "recent_latencies": deque(maxlen=64),
                    "last_call_at": None,
                    "last_error_code": "",
                    "last_error_msg": "",
                    "last_error_at": None,
                }
                self._metrics[key] = bucket

            bucket["total_calls"] += 1
            bucket[f"{norm_outcome}_calls"] += 1
            bucket["total_latency_ms"] += lat
            if lat > bucket["max_latency_ms"]:
                bucket["max_latency_ms"] = lat
            bucket["recent_latencies"].append(lat)
            bucket["last_call_at"] = now_iso

            if norm_outcome != "success":
                bucket["last_error_code"] = str(error_code or norm_outcome.upper())[:64]
                bucket["last_error_msg"] = str(error_msg or "")[:160]
                bucket["last_error_at"] = now_iso

            now_mono = time.monotonic()
            if allow_persist and (now_mono - self._last_flush_mono >= self.SNAPSHOT_FLUSH_INTERVAL_SEC):
                self._last_flush_mono = now_mono
                should_flush = True

        if should_flush:
            self.flush_process_snapshot()

    @staticmethod
    def _serialize_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
        total = int(bucket["total_calls"])
        lats: Deque[float] = bucket["recent_latencies"]
        sorted_lats = sorted(lats)
        p95 = sorted_lats[min(len(sorted_lats) - 1, int(len(sorted_lats) * 0.95))] if sorted_lats else 0.0
        avg = (bucket["total_latency_ms"] / total) if total > 0 else 0.0
        ok = int(bucket["success_calls"])
        rl = int(bucket["rate_limit_calls"])
        deg = int(bucket["degraded_calls"])
        err = int(bucket["error_calls"])
        return {
            "platform": bucket["platform"],
            "provider": bucket["provider"],
            "operation": bucket["operation"],
            "total_calls": total,
            "success_calls": ok,
            "rate_limit_calls": rl,
            "degraded_calls": deg,
            "error_calls": err,
            "success_rate_pct": round((ok / total) * 100.0, 2) if total > 0 else 100.0,
            "avg_latency_ms": round(avg, 1),
            "p95_latency_ms": round(p95, 1),
            "max_latency_ms": round(float(bucket["max_latency_ms"]), 1),
            "last_call_at": bucket["last_call_at"],
            "last_error_code": bucket["last_error_code"],
            "last_error_msg": bucket["last_error_msg"],
            "last_error_at": bucket["last_error_at"],
        }

    def snapshot_local(self) -> Dict[str, Any]:
        with self._lock:
            entries = [self._serialize_bucket(b) for _, b in sorted(self._metrics.items())]
        return {
            "role": _PROCESS_ROLE,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_ts": time.time(),
            "endpoints": entries,
        }

    def flush_process_snapshot(self) -> None:
        if os.environ.get("WEB_LOG_DIR", "").strip() == "-":
            return
        try:
            _HEALTH_DIR.mkdir(parents=True, exist_ok=True)
            snap = self.snapshot_local()
            target = _HEALTH_DIR / f"upstream_health_{_PROCESS_ROLE}.json"
            tmp = _HEALTH_DIR / f".upstream_health_{_PROCESS_ROLE}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except Exception:
            pass

    def get_combined_health_report(self) -> Dict[str, Any]:
        """汇总当前进程内存指标与后台活跃守护进程（非过期）最近落盘的健康快照。"""
        processes: Dict[str, Any] = {_PROCESS_ROLE: self.snapshot_local()}
        now_ts = time.time()
        try:
            if _HEALTH_DIR.is_dir():
                for fp in sorted(_HEALTH_DIR.glob("upstream_health_*.json")):
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        role = str(data.get("role") or fp.stem.replace("upstream_health_", ""))
                        if role == _PROCESS_ROLE:
                            continue
                        upd_ts = float(data.get("updated_ts") or 0.0)
                        age_sec = now_ts - upd_ts
                        is_stale = age_sec > self.SNAPSHOT_MAX_AGE_SEC
                        if is_stale and (age_sec > 3600.0 or role.startswith("live_verifier")):
                            try:
                                fp.unlink()
                            except OSError:
                                pass
                            continue
                        data["stale"] = is_stale
                        processes[role] = data
                    except Exception:
                        continue
        except Exception:
            pass

        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for proc_data in processes.values():
            if proc_data.get("stale"):
                continue
            for ep in proc_data.get("endpoints") or []:
                k = (ep.get("platform", ""), ep.get("provider", ""), ep.get("operation", ""))
                acc = merged.get(k)
                if acc is None:
                    merged[k] = dict(ep)
                else:
                    tot = int(acc["total_calls"]) + int(ep.get("total_calls", 0))
                    ok = int(acc["success_calls"]) + int(ep.get("success_calls", 0))
                    w_lat = (
                        float(acc["avg_latency_ms"]) * int(acc["total_calls"])
                        + float(ep.get("avg_latency_ms", 0.0)) * int(ep.get("total_calls", 0))
                    )
                    acc["total_calls"] = tot
                    acc["success_calls"] = ok
                    acc["rate_limit_calls"] = int(acc["rate_limit_calls"]) + int(ep.get("rate_limit_calls", 0))
                    acc["degraded_calls"] = int(acc["degraded_calls"]) + int(ep.get("degraded_calls", 0))
                    acc["error_calls"] = int(acc["error_calls"]) + int(ep.get("error_calls", 0))
                    acc["success_rate_pct"] = round((ok / tot) * 100.0, 2) if tot > 0 else 100.0
                    acc["avg_latency_ms"] = round(w_lat / tot, 1) if tot > 0 else 0.0
                    acc["p95_latency_ms"] = max(float(acc["p95_latency_ms"]), float(ep.get("p95_latency_ms", 0.0)))
                    acc["max_latency_ms"] = max(float(acc["max_latency_ms"]), float(ep.get("max_latency_ms", 0.0)))
                    if str(ep.get("last_error_at") or "") > str(acc.get("last_error_at") or ""):
                        acc["last_error_at"] = ep.get("last_error_at")
                        acc["last_error_code"] = ep.get("last_error_code")
                        acc["last_error_msg"] = ep.get("last_error_msg")

        summary_list = [v for _, v in sorted(merged.items())]
        degraded_or_throttled = [
            f"{x['platform']}.{x['provider']}.{x['operation']}(rl={x['rate_limit_calls']},deg={x['degraded_calls']},err={x['error_calls']})"
            for x in summary_list
            if (x["rate_limit_calls"] + x["degraded_calls"] + x["error_calls"]) > 0
        ]
        return {
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "alert_summary": degraded_or_throttled,
            "aggregated_endpoints": summary_list,
            "processes": processes,
        }

    def reset(self) -> None:
        with self._lock:
            self._metrics.clear()
            self._last_flush_mono = 0.0


GLOBAL_UPSTREAM_HEALTH = UpstreamHealthRegistry()


def classify_call_outcome(result: Any) -> Tuple[str, str, str]:
    """根据标准响应字典判定监控结果分类 ('success' | 'rate_limit' | 'degraded' | 'error')。"""
    if not isinstance(result, dict):
        return "error", "INVALID_RESPONSE", "非字典响应"
    if result.get("status") != "success":
        code = str(result.get("error_code") or "ERROR").strip()
        msg = str(result.get("error_msg") or "").strip()
        if code.upper() in RATE_LIMIT_ERROR_CODES or any(k in msg for k in ("频", "限流", "过于频繁", "上限")):
            return "rate_limit", code, msg
        return "error", code, msg
    if (
        result.get("resolution_kind") == ResolutionKind.DEGRADED_PRODUCT
        or result.get("detail_lookup_failed")
    ):
        return "degraded", "DEGRADED_PARTIAL", "主链路成功但子详情触发限流降级"
    return "success", "", ""


def track_provider_call(
    platform: str,
    provider: str,
    operation: str,
    fn: Callable[[], Dict[str, Any]],
    *,
    allow_persist: bool = False,
) -> Dict[str, Any]:
    """包裹单次上游供应商调用，自动采集耗时、限流、伪成功降级与异常指标。"""
    t0 = time.perf_counter()
    try:
        res = fn()
    except Exception as exc:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        GLOBAL_UPSTREAM_HEALTH.record_call(
            platform,
            provider,
            operation,
            outcome="error",
            latency_ms=dt_ms,
            error_code=type(exc).__name__,
            error_msg=str(exc)[:120],
            allow_persist=allow_persist,
        )
        raise
    dt_ms = (time.perf_counter() - t0) * 1000.0
    outcome, code, msg = classify_call_outcome(res)
    GLOBAL_UPSTREAM_HEALTH.record_call(
        platform,
        provider,
        operation,
        outcome=outcome,
        latency_ms=dt_ms,
        error_code=code,
        error_msg=msg,
        allow_persist=allow_persist,
    )
    return res


def rotate_providers(
    providers: Sequence[Tuple[str, Callable[[], Dict[str, Any]]]],
    start_index: int = 0,
) -> List[Tuple[str, Callable[[], Dict[str, Any]]]]:
    """按起始游标旋转供应商列表，兼顾轮询负载均衡与单次故障自动顺延。"""
    items = list(providers)
    if len(items) <= 1:
        return items
    offset = int(start_index) % len(items)
    return items[offset:] + items[:offset]


def execute_provider_chain(
    platform: str,
    operation: str,
    providers: Sequence[Tuple[str, Callable[[], Dict[str, Any]]]],
    *,
    allow_persist: bool = False,
) -> Dict[str, Any]:
    """
    执行有序多 API 供应商降级链：
    - 若某供应商返回完整成功（非 `degraded_product`），立即返回；
    - 若某供应商返回确定性业务结果（如 `PRODUCT_OFF_PROMOTION`、未授权、非法参数），立即短路返回，不浪费后续供应商配额；
    - 若某供应商触发限流、网络异常、未捕获异常或仅拿到 `degraded_product`，顺延尝试下一供应商；
    - 若后续供应商均失败，但此前已有 `degraded_product`（带可用推广链接），保底返回该 `degraded_product`。
    """
    best_degraded: Optional[Dict[str, Any]] = None
    last_result: Dict[str, Any] = {
        "status": "error",
        "error_code": "NO_PROVIDER_AVAILABLE",
        "error_msg": f"{platform}.{operation} 未配置可用供应商",
    }

    for prov_name, prov_fn in providers:
        try:
            res = track_provider_call(platform, prov_name, operation, prov_fn, allow_persist=allow_persist)
        except Exception as exc:
            last_result = {
                "status": "error",
                "error_code": "PROVIDER_EXCEPTION",
                "error_msg": f"{prov_name} 执行异常: {type(exc).__name__}",
                "_provider": prov_name,
            }
            continue
        if not isinstance(res, dict):
            continue
        res.setdefault("_provider", prov_name)
        if res.get("status") == "success":
            if res.get("resolution_kind") == ResolutionKind.DEGRADED_PRODUCT or res.get("detail_lookup_failed"):
                if best_degraded is None:
                    best_degraded = res
                continue
            return res

        err_code = str(res.get("error_code") or "").strip().upper()
        last_result = res
        if err_code in NON_FALLBACK_DETERMINISTIC_ERRORS:
            return res

    if best_degraded is not None:
        return best_degraded
    return last_result
