# -*- coding: utf-8 -*-
"""本地订单查询与有界、可续传的同步编排；平台差异只由适配器声明。"""
from copy import deepcopy
import time
import threading
from typing import Dict, Any, Optional
from adapters import get_adapter, require_adapter
from models.product import normalize_platform
from models.order import normalize_order_fields
from models.user import normalize_promoter_id
from repositories.order_repo import OrderRepository
from repositories.order_sync_repo import OrderSyncRepository
from repositories.user_repo import UserRepository
from config import ORDER_SYNC_INTERVAL, ORDER_SYNC_MAX_PAGES, logger

# 兼容字段只记录已持久化确认的窗口终点，不把启动尝试当成功。
_LAST_SYNC_TIMESTAMP: Dict[str, float] = {}
_LAST_SYNC_ATTEMPT: Dict[str, float] = {}
_SYNC_STATE: Dict[str, dict] = {}
_SYNC_LOCK = threading.Lock()  # 仅为旧调用方保留默认平台锁，不再是全平台互斥。
_SYNC_LOCKS = {"pdd": _SYNC_LOCK}
_SYNC_LOCKS_GUARD = threading.Lock()
_INITIAL_LOOKBACK_SECONDS = 23 * 3600
_SYNC_OVERLAP_SECONDS = 5 * 60
_PAGE_SIZE = 50


def _platform_lock(platform: str):
    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(platform, threading.Lock())


class OrderService:
    def __init__(self, order_repo: Optional[OrderRepository] = None,
                 sync_repo: Optional[OrderSyncRepository] = None,
                 user_repo: Optional[UserRepository] = None):
        # 仓储构造均为惰性；测试必须显式注入检查点与身份仓储替身。
        self.repo = order_repo if order_repo is not None else OrderRepository()
        self.sync_repo = sync_repo if sync_repo is not None else OrderSyncRepository()
        self.user_repo = user_repo if user_repo is not None else UserRepository()

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

    def _sync_platform_orders_bg(self, platform: str, end_time: int) -> None:
        """每次最多处理分页预算；同一窗口冻结时间、页码和不透明游标。"""
        processed = 0
        advanced = False
        complete = False
        error = ""
        try:
            adapter = get_adapter(platform)
            window_seconds = int(getattr(adapter, "order_window_seconds", _INITIAL_LOOKBACK_SECONDS))
            if window_seconds <= 0:
                raise ValueError("平台订单时间窗口必须大于零")
            state = deepcopy(self.sync_repo.get_state(platform)) or {"last_success_end": 0, "pending": None}
            success_end = int(state.get("last_success_end") or 0)
            if success_end:
                _LAST_SYNC_TIMESTAMP[platform] = success_end
            if not state.get("pending"):
                # 首次仅最近23h；以后从成功水位重叠查询，未完成目标绝不被新请求替换。
                start_time = (success_end - min(_SYNC_OVERLAP_SECONDS, window_seconds)
                              if success_end else end_time - _INITIAL_LOOKBACK_SECONDS)
                target_end = max(int(end_time), success_end)
                state["pending"] = self._new_pending(start_time, target_end, window_seconds)
                self.sync_repo.save_state(platform, state)
            for _ in range(ORDER_SYNC_MAX_PAGES):
                pending = state["pending"]
                response = adapter.fetch_increment_orders(
                    start_time=pending["start_time"], end_time=pending["end_time"],
                    page=pending["page"], page_size=_PAGE_SIZE,
                    cursor=deepcopy(pending.get("cursor")),
                )
                if not isinstance(response, dict) or response.get("status") != "success":
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
                    _LAST_SYNC_TIMESTAMP[platform] = state["last_success_end"]
                if state["pending"] is None:
                    complete = True
                    break
            if complete:
                summary = {"status": "success", "error": "", "synced_count": processed}
                logger.info(f"[{platform}] 当前目标窗口同步完成，处理 {processed} 笔订单")
            else:
                summary = {"status": "partial" if processed or advanced or not error else "error",
                           "error": error or "达到单次分页预算，窗口尚未完整同步",
                           "synced_count": processed}
                logger.warning(f"[{platform}] 同步{summary['status']}: {summary['error']}，已处理 {processed} 笔")
            _SYNC_STATE[platform] = summary
        except Exception as exc:
            _SYNC_STATE[platform] = {"status": "partial" if processed or advanced else "error",
                                     "error": "同步订单异常，请稍后重试", "synced_count": processed}
            logger.error(f"[{platform}] 同步增量订单异常: {exc}")
        finally:
            _platform_lock(platform).release()

    def _try_start_sync(self, platform: str, now: float) -> None:
        if now - _LAST_SYNC_ATTEMPT.get(platform, 0.0) < ORDER_SYNC_INTERVAL:
            return
        lock = _platform_lock(platform)
        if not lock.acquire(blocking=False):
            return
        try:
            if now - _LAST_SYNC_ATTEMPT.get(platform, 0.0) < ORDER_SYNC_INTERVAL:
                lock.release()
                return
            _LAST_SYNC_ATTEMPT[platform] = now
            _SYNC_STATE[platform] = {"status": "running", "error": "", "synced_count": 0}
            threading.Thread(
                target=self._sync_platform_orders_bg, args=(platform, int(now)),
                name=f"bg-order-sync-{platform}", daemon=True,
            ).start()
        except Exception as exc:
            _SYNC_STATE[platform] = {"status": "error", "error": "无法启动订单同步，请稍后重试", "synced_count": 0}
            lock.release()
            logger.error(f"[{platform}] 启动同步线程失败: {exc}")

    def sync_and_query_orders(
        self,
        platform: str = "pdd",
        uid: str = "",
        days: int = 7,
        force_sync: bool = False,
    ) -> Dict[str, Any]:
        """兼容 force 参数，但任何调用都不能穿透冷却或平台互斥锁。"""
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
