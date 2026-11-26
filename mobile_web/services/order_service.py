# -*- coding: utf-8 -*-
"""本地订单查询与有界、可续传的同步编排；平台差异只由适配器声明。"""
from copy import deepcopy
import math
import time
import threading
from typing import Dict, Any, Optional
from adapters import get_adapter, require_adapter
from infrastructure.api_governance import api_caller_context
from infrastructure.logging_utils import format_exc_summary, severity_icon
from models.product import normalize_platform
from models.order import normalize_order_fields
from models.user import normalize_promoter_id
from repositories.order_repo import OrderRepository
from repositories.order_sync_repo import OrderSyncRepository
from repositories.product_repo import ProductRepository
from repositories.user_repo import UserRepository
from config import ORDER_SYNC_INTERVAL, ORDER_SYNC_MAX_PAGES, logger

_INITIAL_LOOKBACK_SECONDS = 23 * 3600
_SYNC_OVERLAP_SECONDS = 5 * 60
_PAGE_SIZE = 50


class OrderService:
    def __init__(self, order_repo: Optional[OrderRepository] = None,
                 sync_repo: Optional[OrderSyncRepository] = None,
                 user_repo: Optional[UserRepository] = None,
                 product_repo: Optional[ProductRepository] = None):
        # 仓储构造均为惰性；测试必须显式注入检查点与身份仓储替身。
        self.repo = order_repo if order_repo is not None else OrderRepository()
        self.sync_repo = sync_repo if sync_repo is not None else OrderSyncRepository()
        self.user_repo = user_repo if user_repo is not None else UserRepository()
        self.product_repo = product_repo
        # 运行态游标与锁归属于服务实例（只记录已持久化确认的水位与冷却窗口）
        self._last_sync_timestamp: Dict[str, float] = {}
        self._last_sync_attempt: Dict[str, float] = {}
        self._sync_state: Dict[str, dict] = {}
        self._sync_locks: Dict[str, threading.Lock] = {}
        self._sync_locks_guard = threading.Lock()
        self._bg_threads_lock = threading.Lock()
        self._bg_threads: Dict[int, threading.Thread] = {}

    def _platform_lock(self, platform: str) -> threading.Lock:
        with self._sync_locks_guard:
            return self._sync_locks.setdefault(platform, threading.Lock())

    def reset_state(self) -> None:
        self._last_sync_timestamp.clear()
        self._last_sync_attempt.clear()
        self._sync_state.clear()

    def wait_for_sync_threads(self, timeout: float = 5.0) -> None:
        """等待所有已启动的后台增量拉单线程自然收敛退出，用于优雅停机。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._bg_threads_lock:
                pending = tuple(t for t in self._bg_threads.values() if t.is_alive())
            if not pending:
                return
            for thread in pending:
                if thread is threading.current_thread():
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                thread.join(timeout=remaining)

    def _attribute_order(self, order: dict, platform: str, identity_cache: Optional[dict] = None) -> dict:
        """只信适配器归因证据；数字推广身份与平台授权状态无关。

        cache 仅由当前同步页持有，包含未匹配结果；下一页会重新反查新绑定。
        """
        if not isinstance(order, dict):
            raise ValueError("平台订单规范化响应无效")
        doc = normalize_order_fields(order, platform)
        cache = identity_cache if identity_cache is not None else {}
        promoter_id = doc.get("promoter_id")
        identity = doc.get("attribution_identity")
        if promoter_id is not None and promoter_id != "":
            promoter_id = normalize_promoter_id(promoter_id)
            doc["promoter_id"] = promoter_id
            key = ("promoter_id", promoter_id)
            if key not in cache:
                cache[key] = self.user_repo.get_uid_by_promoter_id(promoter_id)
            uid = cache[key]
            doc["promoter_uid"] = str(uid).strip() if uid else ""
        elif identity:
            if not isinstance(identity, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in identity.items()
            ):
                raise ValueError("平台订单归因身份无效")
            key = ("platform_identity", platform, tuple(sorted(identity.items())))
            if key not in cache:
                cache[key] = self.user_repo.get_uid_by_platform_identity(platform, identity)
            uid = cache[key]
            doc["promoter_uid"] = str(uid).strip() if uid else ""
        else:
            # 兼容 PDD 从官方 custom_parameters 提取的规范 UID。
            doc.pop("attribution_identity", None)
            doc["promoter_uid"] = str(doc.get("promoter_uid") or "").strip()
        if promoter_id is None or promoter_id == "":
            doc.pop("promoter_id", None)
        doc["attribution_status"] = "matched" if doc["promoter_uid"] else "unmatched"
        return doc

    @staticmethod
    def _new_pending(start_time: int, target_end: int, window_seconds: int) -> dict:
        return {"start_time": start_time, "end_time": min(start_time + window_seconds, target_end),
                "target_end": target_end, "page": 1, "cursor": None}

    def _sync_product_valid_order_counts(self, platform: str, orders: list) -> None:
        """订单同步入库后，顺带精确重算本批变动商品的有效订单数（剔除退款/失效）并覆盖写回商品表。"""
        if self.product_repo is None:
            return
        count_fn = getattr(self.repo, "count_valid_orders_by_goods_ids", None)
        write_fn = getattr(self.product_repo, "bulk_set_valid_orders", None)
        if not callable(count_fn) or not callable(write_fn):
            return
        raw_ids: list[str] = []
        for o in orders or ():
            if not isinstance(o, dict):
                continue
            if platform == "jd":
                cgid = str(o.get("canonical_goods_id") or "").strip()
                if cgid:
                    raw_ids.append(cgid)
            gid = str(o.get("goods_id") or "").strip()
            if gid:
                raw_ids.append(gid)
        goods_ids = list(dict.fromkeys(raw_ids))
        if not goods_ids:
            return
        try:
            counts = count_fn(platform, goods_ids)
            if counts:
                write_fn(platform, counts)
        except Exception as exc:
            logger.warning(
                "⚠️ 【订单计数回写异常】同步商品有效下单数失败，已忽略以保护订单同步主链路 | 平台[%s] 异常<%s>",
                platform, format_exc_summary(exc),
            )

    def _sync_platform_orders_bg(self, platform: str, end_time: int, auto_continue: bool = False) -> None:
        """每次最多处理分页预算；后台线程触发时自动续传追平最近23h时间切片。"""
        sync_started = time.perf_counter()
        processed = 0
        pages_fetched = 0
        advanced = False
        complete = False
        error = ""
        error_code = ""
        try:
            adapter = get_adapter(platform)
            window_seconds = int(getattr(adapter, "order_window_seconds", _INITIAL_LOOKBACK_SECONDS))
            if window_seconds <= 0:
                raise ValueError("平台订单时间窗口必须大于零")
            state = deepcopy(self.sync_repo.get_state(platform)) or {"last_success_end": 0, "pending": None}
            success_end = int(state.get("last_success_end") or 0)
            if success_end:
                self._last_sync_timestamp[platform] = success_end
            earliest_allowed = int(end_time) - _INITIAL_LOOKBACK_SECONDS
            if auto_continue and isinstance(state.get("pending"), dict):
                pending_start = int(state["pending"].get("start_time") or 0)
                if pending_start < earliest_allowed:
                    # 跨天重启后若遗留超过23h的陈旧未完成检查点，直接快进至最近23h窗口并对齐当前目标终点
                    resume_start = max(
                        (success_end - min(_SYNC_OVERLAP_SECONDS, window_seconds)) if success_end else earliest_allowed,
                        earliest_allowed,
                    )
                    target_end = max(int(end_time), int(state["pending"].get("target_end") or 0), success_end)
                    state["pending"] = self._new_pending(resume_start, target_end, window_seconds)
                    self.sync_repo.save_state(platform, state)
            if not state.get("pending"):
                # 首次仅最近23h；以后从成功水位重叠查询，未完成目标绝不被新请求替换。
                start_time = (success_end - min(_SYNC_OVERLAP_SECONDS, window_seconds)
                              if success_end else earliest_allowed)
                if auto_continue and start_time < earliest_allowed:
                    start_time = earliest_allowed
                target_end = max(int(end_time), success_end)
                state["pending"] = self._new_pending(start_time, target_end, window_seconds)
                self.sync_repo.save_state(platform, state)
            max_batches = 20 if auto_continue else 1
            for batch_idx in range(max_batches):
                for _ in range(ORDER_SYNC_MAX_PAGES):
                    pending = state["pending"]
                    with api_caller_context("background"):
                        response = adapter.fetch_increment_orders(
                            start_time=pending["start_time"], end_time=pending["end_time"],
                            page=pending["page"], page_size=_PAGE_SIZE,
                            cursor=deepcopy(pending.get("cursor")),
                        )
                    if not isinstance(response, dict) or response.get("status") != "success":
                        error_code = str(response.get("error_code") or "") if isinstance(response, dict) else ""
                        error = response.get("error_msg", "平台订单查询失败") if isinstance(response, dict) else "平台订单响应无效"
                        break
                    orders = response.get("order_list")
                    if not isinstance(orders, list):
                        error = "平台订单列表响应无效"
                        break
                    has_more = response.get("has_more")
                    if has_more is not None and not isinstance(has_more, bool):
                        error = "平台订单分页响应无效"
                        break
                    pages_fetched += 1
                    # 旧分页协议没有 has_more / cursor 时，仍按原始页长判断。
                    has_more = has_more if has_more is not None else len(orders) >= _PAGE_SIZE
                    if not orders and has_more:
                        error = "平台返回空页但仍有后续订单"
                        break
                    identity_cache = {}
                    normalized = [self._attribute_order(adapter.normalize_order(order), platform, identity_cache)
                                  for order in orders]
                    if normalized:
                        written = self.repo.upsert_orders(normalized, platform=platform)
                        if written != len(normalized):
                            error = "订单未完整入库"
                            break
                        processed += len(normalized)
                        self._sync_product_valid_order_counts(platform, normalized)
                    next_state = deepcopy(state)
                    if has_more:
                        next_state["pending"].update(page=pending["page"] + 1,
                                                     cursor=deepcopy(response.get("next_cursor")))
                    else:
                        # 窗口全部写入后，成功水位与下一窗口一起原子保存。
                        next_state["last_success_end"] = max(int(state.get("last_success_end") or 0), pending["end_time"])
                        next_state["pending"] = (
                            self._new_pending(pending["end_time"], pending["target_end"], window_seconds)
                            if pending["end_time"] < pending["target_end"] else None
                        )
                    # 即使这里失败也不移动内存游标/水位，下次读取旧检查点幂等重放。
                    self.sync_repo.save_state(platform, next_state)
                    state = next_state
                    advanced = advanced or not has_more
                    if state.get("last_success_end"):
                        self._last_sync_timestamp[platform] = state["last_success_end"]
                    if state["pending"] is None:
                        if auto_continue and int(state.get("last_success_end") or 0) < int(end_time) - 60:
                            # 旧检查点追平后，若距离当前时间仍有缺口，继续推进至当前 end_time
                            next_start = max(
                                int(state["last_success_end"]) - min(_SYNC_OVERLAP_SECONDS, window_seconds),
                                earliest_allowed,
                            )
                            state["pending"] = self._new_pending(next_start, int(end_time), window_seconds)
                            self.sync_repo.save_state(platform, state)
                            continue
                        complete = True
                        break
                if complete or error or not auto_continue or state.get("pending") is None:
                    break
                if batch_idx + 1 < max_batches:
                    time.sleep(0.05)
            elapsed_ms = (time.perf_counter() - sync_started) * 1000
            if complete:
                summary = {"status": "success", "error": "", "synced_count": processed}
                logger.info(
                    "【订单同步】平台[%s] 状态[success] 拉取页数[%d] 入库订单[%d笔] 耗时[%.1fms]",
                    platform, pages_fetched, processed, elapsed_ms,
                )
            else:
                summary = {"status": "partial" if processed or advanced or not error else "error",
                           "error": error or "达到单次分页预算，窗口尚未完整同步",
                           "synced_count": processed}
                if error:
                    logger.warning(
                        "%s 【订单同步中断】拉取或写入增量订单时未完整完成 | 平台[%s] 状态[%s] 已处理页数[%d] 已入库[%d笔] 耗时[%.1fms] 中断原因<%s> 排查方向<请检查上游联盟订单接口状态、密钥配额或稍后自动续传>",
                        severity_icon(error_code=error_code),
                        platform, summary["status"], pages_fetched, processed, elapsed_ms, summary["error"],
                    )
                else:
                    logger.info(
                        "【订单同步】平台[%s] 状态[partial] 已处理页数[%d] 已入库[%d笔] 耗时[%.1fms] 说明<已达单次分页预算，剩余窗口将在下一周期继续同步>",
                        platform, pages_fetched, processed, elapsed_ms,
                    )
            self._sync_state[platform] = summary
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - sync_started) * 1000
            self._sync_state[platform] = {"status": "partial" if processed or advanced else "error",
                                     "error": "同步订单异常，请稍后重试", "synced_count": processed}
            logger.error(
                "❌ 【订单同步异常】后台同步增量订单时发生系统异常 | 平台[%s] 已入库[%d笔] 耗时[%.1fms] 异常<%s> 排查方向<请检查MongoDB orders/order_sync_state集合连接状态、订单字段格式或上游接口>",
                platform, processed, elapsed_ms, format_exc_summary(exc),
            )
        finally:
            with self._bg_threads_lock:
                self._bg_threads.pop(id(threading.current_thread()), None)
            self._platform_lock(platform).release()

    def _try_start_sync(self, platform: str, now: float) -> None:
        if now - self._last_sync_attempt.get(platform, 0.0) < ORDER_SYNC_INTERVAL:
            return
        lock = self._platform_lock(platform)
        if not lock.acquire(blocking=False):
            return
        thread = None
        try:
            if now - self._last_sync_attempt.get(platform, 0.0) < ORDER_SYNC_INTERVAL:
                lock.release()
                return
            self._last_sync_attempt[platform] = now
            self._sync_state[platform] = {"status": "running", "error": "", "synced_count": 0}
            thread = threading.Thread(
                target=self._sync_platform_orders_bg, args=(platform, int(now), True),
                name=f"bg-order-sync-{platform}", daemon=True,
            )
            with self._bg_threads_lock:
                self._bg_threads[id(thread)] = thread
            thread.start()
        except Exception as exc:
            if thread is not None:
                with self._bg_threads_lock:
                    self._bg_threads.pop(id(thread), None)
            self._sync_state[platform] = {"status": "error", "error": "无法启动订单同步，请稍后重试", "synced_count": 0}
            lock.release()
            logger.error(
                "❌ 【订单同步异常】启动后台订单增量同步线程失败 | 平台[%s] 异常<%s> 排查方向<请检查系统线程资源限制>",
                platform, format_exc_summary(exc),
            )

    def sync_and_query_orders(
        self,
        platform: str = "pdd",
        uid: str = "",
        days: int = 7,
    ) -> Dict[str, Any]:
        """异步触发受控频增量拉单，并查询当前认证推手本地订单及收益汇总。"""
        plat = normalize_platform(platform)
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return {"status": "error", "error_msg": "uid 不能为空"}
        if not isinstance(days, int) or not 0 <= days <= 90:
            return {"status": "error", "error_msg": "查询天数必须在0到90之间"}
        adapter = require_adapter(plat)
        capabilities = getattr(adapter, "capabilities", None)
        if isinstance(capabilities, dict) and capabilities.get("orders") is False:
            return {"status": "error", "error_code": "capability_unavailable", "platform": plat,
                    "error_msg": f"{adapter.platform_name}订单查询尚未配置，不能同步订单"}
        if plat not in self._last_sync_timestamp:
            try:
                saved_state = self.sync_repo.get_state(plat)
                if isinstance(saved_state, dict):
                    saved_end = int(saved_state.get("last_success_end") or 0)
                    if saved_end > 0:
                        self._last_sync_timestamp[plat] = saved_end
            except Exception:
                pass
        now = time.time()
        last_attempt_before = float(self._last_sync_attempt.get(plat, 0.0) or 0.0)
        elapsed_before = now - last_attempt_before
        sync_cooldown_remaining = (
            max(1, int(math.ceil(ORDER_SYNC_INTERVAL - elapsed_before)))
            if (last_attempt_before > 0 and 0 <= elapsed_before < ORDER_SYNC_INTERVAL)
            else 0
        )
        self._try_start_sync(plat, now)
        start_ts = int(now - days * 86400) if days else None
        try:
            result = self.repo.query_user_orders(
                platform=plat, promoter_uid=clean_uid, start_timestamp=start_ts,
                limit=100,
            )
        except Exception as exc:
            logger.error(
                "❌ 【订单查询失败】查询用户本地返利订单列表及统计汇总失败 | 平台[%s] 用户[%s] 时间跨度[%d天] 异常<%s> 排查方向<请检查MongoDB服务连接状态、orders集合索引或聚合管线兼容性>",
                plat, clean_uid, days, format_exc_summary(exc),
            )
            return {"status": "error", "error_msg": "查询订单与收益失败，请稍后重试"}
        state = self._sync_state.get(plat, {})
        result.update({
            "platform": plat, "uid": clean_uid, "query_days": days,
            "last_sync_time": int(self._last_sync_timestamp.get(plat, 0)),
            "last_sync_attempt_time": int(self._last_sync_attempt.get(plat, 0)),
            "sync_cooldown_remaining": sync_cooldown_remaining,
            "sync_status": state.get("status", "idle"),
            "sync_error": state.get("error", ""),
            "synced_count": state.get("synced_count", 0),
        })
        return {"status": "success", "data": result}

    def run_scheduled_sync_round(self, stop_event: Optional[threading.Event] = None) -> Dict[str, str]:
        """由后台守护线程周期性调用：在单线程内串行推进各已启用平台的增量订单检查点，防止无人查单超 23h 漏单。"""
        from adapters import get_all_platforms
        outcomes: Dict[str, str] = {}
        for p_info in get_all_platforms():
            if stop_event is not None and stop_event.is_set():
                break
            if not p_info.get("active"):
                continue
            caps = p_info.get("capabilities") or {}
            if caps.get("orders") is False:
                continue
            plat = normalize_platform(p_info.get("code") or "")
            if not plat:
                continue
            now = time.time()
            if now - self._last_sync_attempt.get(plat, 0.0) < ORDER_SYNC_INTERVAL:
                outcomes[plat] = "skipped_cooldown"
                continue
            lock = self._platform_lock(plat)
            if not lock.acquire(blocking=False):
                outcomes[plat] = "skipped_busy"
                continue
            self._last_sync_attempt[plat] = now
            self._sync_state[plat] = {"status": "running", "error": "", "synced_count": 0}
            # _sync_platform_orders_bg 内部 finally 会负责释放 _platform_lock(plat)
            self._sync_platform_orders_bg(plat, int(now), True)
            outcomes[plat] = (self._sync_state.get(plat) or {}).get("status", "done")
        return outcomes


class OrderSyncWorker:
    """常驻轻量后台订单同步守护线程（默认启动延迟 45s，每 15 分钟巡检一次三平台增量订单水位）。"""

    def __init__(
        self,
        service: OrderService,
        *,
        interval_seconds: float = 900.0,
        initial_delay_seconds: float = 45.0,
    ) -> None:
        self.service = service
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="order-sync-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "【订单定时同步】后台增量拉单守护线程已启动 (首轮延迟 %.0fs, 巡检周期 %.0fs)",
            self.initial_delay_seconds,
            self.interval_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None

    def _run_loop(self) -> None:
        if self.initial_delay_seconds > 0 and self._stop_event.wait(timeout=self.initial_delay_seconds):
            return
        while not self._stop_event.is_set():
            try:
                self.service.run_scheduled_sync_round(stop_event=self._stop_event)
            except Exception as exc:
                logger.warning("【订单定时同步】本轮后台拉单巡检异常 | 异常<%s>", format_exc_summary(exc))
            if self._stop_event.wait(timeout=self.interval_seconds):
                break
