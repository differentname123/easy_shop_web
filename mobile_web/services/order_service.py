# -*- coding: utf-8 -*-
"""订单查询只读本地；有界后台同步明确区分尝试、成功与部分完成。"""
import time
import threading
from typing import Dict, Any, Optional
from adapters import get_adapter, require_adapter
from models.product import normalize_platform
from repositories.order_repo import OrderRepository
from config import ORDER_SYNC_INTERVAL, ORDER_SYNC_MAX_PAGES, logger

# 兼容名称现在只记录确认完整处理的窗口终点，不再把启动尝试当成功。
_LAST_SYNC_TIMESTAMP: Dict[str, float] = {}
_LAST_SYNC_ATTEMPT: Dict[str, float] = {}
_SYNC_STATE: Dict[str, dict] = {}
_SYNC_LOCK = threading.Lock()


class OrderService:
    def __init__(self, order_repo: Optional[OrderRepository] = None):
        self.repo = order_repo or OrderRepository()

    def _sync_platform_orders_bg(self, platform: str, end_time: int) -> None:
        """保留最近 23 小时/页数预算的 best-effort 行为，不宣称历史全量补齐。"""
        processed = 0
        complete = False
        error = ""
        try:
            adapter = get_adapter(platform)
            start_time = end_time - 23 * 3600
            for page in range(1, ORDER_SYNC_MAX_PAGES + 1):
                response = adapter.fetch_increment_orders(
                    start_time=start_time, end_time=end_time, page=page, page_size=50
                )
                if not isinstance(response, dict) or response.get("status") != "success":
                    error = response.get("error_msg", "平台订单查询失败") if isinstance(response, dict) else "平台订单响应无效"
                    break
                orders = response.get("order_list")
                if not isinstance(orders, list):
                    error = "平台订单列表响应无效"
                    break
                if not orders:
                    if response.get("has_more") is True:
                        error = "平台返回空页但仍有后续订单"
                    else:
                        complete = True
                    break
                normalized = [adapter.normalize_order(order) for order in orders]
                written = self.repo.upsert_orders(normalized, platform=platform)
                if written != len(normalized):
                    error = "订单未完整入库"
                    break
                processed += len(normalized)
                has_more = response.get("has_more")
                if has_more is False or (has_more is None and len(orders) < 50):
                    complete = True
                    break
            if complete:
                _LAST_SYNC_TIMESTAMP[platform] = end_time
                state = {"status": "success", "error": "", "synced_count": processed}
                logger.info(f"[{platform}] 当前23小时窗口同步完成，处理 {processed} 笔订单")
            else:
                state = {
                    "status": "partial" if processed or not error else "error",
                    "error": error or "达到单次分页预算，窗口尚未完整同步",
                    "synced_count": processed,
                }
                logger.warning(f"[{platform}] 同步{state['status']}: {state['error']}，已处理 {processed} 笔")
            _SYNC_STATE[platform] = state
        except Exception as exc:
            _SYNC_STATE[platform] = {
                "status": "partial" if processed else "error",
                "error": "同步订单异常，请稍后重试", "synced_count": processed,
            }
            logger.error(f"[{platform}] 同步增量订单异常: {exc}")
        finally:
            _SYNC_LOCK.release()

    def _try_start_sync(self, platform: str, now: float) -> None:
        if now - _LAST_SYNC_ATTEMPT.get(platform, 0.0) < ORDER_SYNC_INTERVAL:
            return
        if not _SYNC_LOCK.acquire(blocking=False):
            return
        try:
            # 冷却检查必须在取得锁后再做一次，防止相邻请求先后启动任务。
            if now - _LAST_SYNC_ATTEMPT.get(platform, 0.0) < ORDER_SYNC_INTERVAL:
                _SYNC_LOCK.release()
                return
            _LAST_SYNC_ATTEMPT[platform] = now
            _SYNC_STATE[platform] = {"status": "running", "error": "", "synced_count": 0}
            threading.Thread(
                target=self._sync_platform_orders_bg, args=(platform, int(now)),
                name=f"bg-order-sync-{platform}", daemon=True,
            ).start()
        except Exception as exc:
            # start() / 线程构造失败时后台 finally 不会执行，调用方负责释放。
            _SYNC_STATE[platform] = {"status": "error", "error": "无法启动订单同步，请稍后重试", "synced_count": 0}
            _SYNC_LOCK.release()
            logger.error(f"[{platform}] 启动同步线程失败: {exc}")

    def sync_and_query_orders(
        self,
        platform: str = "pdd",
        uid: str = "",
        days: int = 7,
        force_sync: bool = False,
    ) -> Dict[str, Any]:
        """兼容 force 参数，但任何调用都不能穿透冷却或互斥锁。"""
        plat = normalize_platform(platform)
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return {"status": "error", "error_msg": "uid 不能为空"}
        if not isinstance(days, int) or not 0 <= days <= 90:
            return {"status": "error", "error_msg": "查询天数必须在0到90之间"}
        # 仅本地装配/启用检查，不调用 SDK。查询旧文档的解释规则也由适配器提供。
        adapter = require_adapter(plat)
        now = time.time()
        self._try_start_sync(plat, now)
        start_ts = int(now - days * 86400) if days else None
        try:
            result = self.repo.query_user_orders(
                platform=plat, promoter_uid=clean_uid, start_timestamp=start_ts,
                limit=100, legacy_order_statuses=adapter.legacy_order_statuses,
            )
        except Exception as exc:
            logger.error(f"[{plat}] 查询订单仓储失败: {exc}")
            return {"status": "error", "error_msg": "查询订单与收益失败，请稍后重试"}
        state = _SYNC_STATE.get(plat, {})
        result.update({
            "platform": plat, "uid": clean_uid, "query_days": days,
            "last_sync_time": int(_LAST_SYNC_TIMESTAMP.get(plat, 0)),
            "last_sync_attempt_time": int(_LAST_SYNC_ATTEMPT.get(plat, 0)),
            "sync_status": state.get("status", "idle"),
            "sync_error": state.get("error", ""),
            "synced_count": state.get("synced_count", 0),
        })
        return {"status": "success", "data": result}
