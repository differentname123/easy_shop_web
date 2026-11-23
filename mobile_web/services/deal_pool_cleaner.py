# -*- coding: utf-8 -*-
"""
好价线报清洗引擎对外门面 (`DealPoolCleaner`)。
================================================================================
底层核心实现已解耦至 `services/deal_cleaning/` 子包：
- `url_venue`      : 平台信号识别、官方公共会场/领券页白名单重组与 SSRF 安全短链解包
- `text_sanitizer` : 无链活人文案清洗、教程脱敏与时间线结构构建
- `cache_state`    : 运行期 LRU 转链锚点缓存 (`AnchorConversionCache`) 与正式库活跃商品批量去重查询
- `ai_client`      : 显式提示词路径、热加载缓存、单次 LLM 调用并发信号量与 JSON 解析
- `stages`         : Stage 1 (AI-1) / Stage 2 (并发转链) / Stage 3 (AI-2) 执行单元
- `workers`        : 状态机写库、阶段工作线程 (`StageWorker`) 与后台保鲜维护线程 (`MaintenanceWorker`)
"""
from __future__ import annotations

import http.cookiejar
import logging
import os
import socket
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests
from adapters import get_adapter
from infrastructure.transport import create_http_session
from models.deal_constants import (
    DEAL_FORMAL_EXPIRY_HOURS,
    DEFAULT_AI_TIMEOUT_SECONDS,
    MAX_ANCHOR_CACHE_SIZE,
    PROMPT_STAGE1_PATH,
    PROMPT_STAGE2_PATH,
)
from repositories.product_repo import upsert_cleaned_products_to_formal_table
from services.deal_cleaning.ai_client import DealAIClient
from services.deal_cleaning.cache_state import (
    AnchorConversionCache,
    query_active_formal_products,
    should_upgrade_existing_deal,
)
from services.deal_cleaning.stages import (
    build_stage1_task_input as _build_stage1_task_input_impl,
    clean_batch_records as _clean_batch_records_impl,
    convert_single_target as _convert_single_target_impl,
    process_ai1_unit as _process_ai1_unit_impl,
    process_ai2_unit as _process_ai2_unit_impl,
    process_convert_unit as _process_convert_unit_impl,
    run_ai_stage1_batch as _run_ai_stage1_batch_impl,
    run_ai_stage2_batch as _run_ai_stage2_batch_impl,
    run_stage3_concurrent_conversions as _run_stage3_concurrent_conversions_impl,
)
from services.deal_cleaning.text_sanitizer import (
    assemble_combined_text_and_images,
    build_clean_original_copywriting,
    clean_coupon_label,
    extract_doc_images,
    format_raw_message_for_ai1,
    normalize_inline_spaces,
    sanitize_card_highlight,
    sanitize_clean_tutorial_step,
    sanitize_polished_copywriting,
    strip_markdown_link_wrapper,
)
from services.deal_cleaning.url_venue import (
    _is_safe_public_http_url,
    _is_verified_station_promo_link,
    detect_candidate_platform,
    is_explicit_coupon_target,
    rebuild_clean_official_venue_url,
    unwrap_to_official_public_venue_url,
)
from services.deal_cleaning.workers import (
    MaintenanceWorker,
    StageWorker,
    _commit_batch_results_to_db,
    commit_batch_results_to_db,
)

logger = logging.getLogger("deal_pool_scheduler")


class DealPoolCleaner:
    """好价线报双阶段 AI 提纯 + 官方转链核验清洗门面。"""

    def __init__(
        self,
        ai_batch_size: int = 10,
        ai2_batch_size: Optional[int] = None,
        ai_max_workers: int = 8,
        convert_max_workers: int = 10,
        ai_timeout: float = DEFAULT_AI_TIMEOUT_SECONDS,
        unwrap_http_session: Any = None,
        unwrap_resolver: Optional[Callable[..., Any]] = None,
        *,
        stage1_prompt_path: str = PROMPT_STAGE1_PATH,
        stage2_prompt_path: str = PROMPT_STAGE2_PATH,
        ai_semaphore: Optional[threading.BoundedSemaphore] = None,
        enable_live_unwrap: Optional[bool] = None,
        allow_bisect: bool = True,
        ai_caller: Optional[Callable[..., Any]] = None,
    ):
        self.use_ai = True
        self.ai_batch_size = max(1, ai_batch_size)
        self.ai2_batch_size = max(1, ai2_batch_size if ai2_batch_size is not None else 10)
        self.ai_max_workers = max(1, ai_max_workers)
        self.convert_max_workers = max(1, convert_max_workers)
        self.ai_timeout = max(10.0, float(ai_timeout))
        self.unwrap_http_session = unwrap_http_session
        self.unwrap_resolver = unwrap_resolver or socket.getaddrinfo
        self.enable_live_unwrap = enable_live_unwrap

        self.adapters = {
            "jd": get_adapter("jd"),
            "taobao": get_adapter("taobao"),
            "pdd": get_adapter("pdd"),
        }

        self.ai_client = DealAIClient(
            stage1_prompt_path=stage1_prompt_path,
            stage2_prompt_path=stage2_prompt_path,
            ai_timeout=self.ai_timeout,
            ai_semaphore=ai_semaphore,
            allow_bisect=allow_bisect,
            ai_caller=ai_caller,
        )
        self._anchor_cache = AnchorConversionCache(max_size=MAX_ANCHOR_CACHE_SIZE)
        self._cache_lock = self._anchor_cache.lock
        self._shared_unwrap_session: Optional[requests.Session] = None

        self.stats_cache_hits = 0
        self.stats_ai1_calls = 0
        self.stats_ai1_model_hits = 0
        self.stats_ai1_fallbacks = 0
        self.stats_ai2_calls = 0
        self.stats_ai2_model_hits = 0
        self.stats_ai2_fallbacks = 0
        self.stats_ai_retries_scheduled = 0
        self.stats_early_terminated_post_stage3 = 0
        self.stats_deal_upgrades_post_stage3 = 0
        self.last_batch_stage_timings: Dict[str, float] = {}

    @property
    def anchor_cache(self):
        return self._anchor_cache.data

    @property
    def max_anchor_cache_size(self) -> int:
        return self._anchor_cache.max_size

    @max_anchor_cache_size.setter
    def max_anchor_cache_size(self, value: int) -> None:
        self._anchor_cache.max_size = max(1, int(value))

    def set_ai_semaphore(self, sem: Optional[threading.BoundedSemaphore]) -> None:
        self.ai_client.set_ai_semaphore(sem)

    def close(self) -> None:
        """确定性释放复用的短链解包 HTTP Session。"""
        with self._cache_lock:
            sess = self._shared_unwrap_session
            self._shared_unwrap_session = None
        if sess is not None and hasattr(sess, "close"):
            try:
                sess.close()
            except Exception:
                pass

    def __enter__(self) -> "DealPoolCleaner":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _set_anchor_cache_locked(self, key: str, value: Dict[str, Any]) -> None:
        self._anchor_cache.set_locked(key, value)

    def _get_anchor_cache_locked(self, key: str) -> Optional[Dict[str, Any]]:
        return self._anchor_cache.get_locked(key)

    @staticmethod
    def _slim_adapter_res_for_cache(res: Any, *, keep_product: bool) -> Dict[str, Any]:
        return AnchorConversionCache.slim_adapter_res(res, keep_product=keep_product)

    @staticmethod
    def _build_anchor_cache_keys(plat: str, raw_anchor: str) -> List[str]:
        return AnchorConversionCache.build_keys(plat, raw_anchor)

    def _evict_transient_failed_cache(self) -> None:
        self._anchor_cache.evict_transient_failures()

    def _get_unwrap_session(self) -> Any:
        if self.unwrap_http_session is not None:
            return self.unwrap_http_session
        with self._cache_lock:
            if self._shared_unwrap_session is None:
                sess = create_http_session(pool_connections=16, pool_maxsize=32)
                # 仅复用 TCP/TLS 连接池，禁止保存第三方短链追踪 Cookie 防广告劫持
                sess.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
                self._shared_unwrap_session = sess
            return self._shared_unwrap_session

    def _can_run_live_unwrap(self) -> bool:
        """生产默认开启短链安全解包；显式关闭或测试离线模式无外部 session 注入时静默跳过。"""
        if self.enable_live_unwrap is not None:
            return bool(self.enable_live_unwrap)
        if self.unwrap_http_session is not None:
            return True
        if os.environ.get("MOBILE_WEB_OFFLINE_MODE", "").strip() == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        return True

    def _get_stage1_prompt(self) -> str:
        return self.ai_client.get_stage1_prompt()

    def _get_stage2_prompt(self) -> str:
        return self.ai_client.get_stage2_prompt()

    def _call_ai_json_array(
        self,
        prompt_template: str,
        batch_payload: List[Dict[str, Any]],
        stage_label: str,
        *,
        _allow_bisect: bool = True,
    ) -> List[Dict[str, Any]]:
        return self.ai_client.call_json_array(
            prompt_template,
            batch_payload,
            stage_label,
            _allow_bisect=_allow_bisect,
        )

    def build_stage1_task_input(self, doc: Dict[str, Any], raw_col) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        return _build_stage1_task_input_impl(doc, raw_col)

    def run_ai_stage1_batch(
        self,
        stage1_inputs: List[Dict[str, Any]],
        docs_by_tip_id: Dict[str, Dict[str, Any]],
        neighbor_maps_by_id: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        quiet_sub_logs: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        return _run_ai_stage1_batch_impl(
            self,
            stage1_inputs,
            docs_by_tip_id,
            neighbor_maps_by_id=neighbor_maps_by_id,
            quiet_sub_logs=quiet_sub_logs,
        )

    def convert_single_target(self, target: Dict[str, Any], default_plat: str = "unknown") -> Dict[str, Any]:
        return _convert_single_target_impl(self, target, default_plat=default_plat)

    def run_stage3_concurrent_conversions(
        self,
        unique_targets: List[Tuple[Dict[str, Any], str]],
        quiet_sub_logs: bool = False,
    ) -> None:
        _run_stage3_concurrent_conversions_impl(self, unique_targets, quiet_sub_logs=quiet_sub_logs)

    def run_ai_stage2_batch(
        self,
        stage2_inputs: List[Dict[str, Any]],
        quiet_sub_logs: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        return _run_ai_stage2_batch_impl(self, stage2_inputs, quiet_sub_logs=quiet_sub_logs)

    def process_ai1_unit(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col,
        quiet_sub_logs: bool = False,
    ) -> Tuple[List[Dict[str, Any]], float, Dict[str, Dict[str, Dict[str, Any]]]]:
        return _process_ai1_unit_impl(
            self,
            selected_docs,
            raw_col,
            quiet_sub_logs=quiet_sub_logs,
        )

    def process_convert_unit(
        self,
        selected_docs: List[Dict[str, Any]],
        formal_col=None,
        shared_active_products: Any = None,
        shared_lock: Optional[threading.Lock] = None,
        quiet_sub_logs: bool = False,
    ) -> Tuple[List[Dict[str, Any]], float]:
        return _process_convert_unit_impl(
            self,
            selected_docs,
            formal_col=formal_col,
            shared_active_products=shared_active_products,
            shared_lock=shared_lock,
            quiet_sub_logs=quiet_sub_logs,
        )

    def process_ai2_unit(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col=None,
        neighbor_maps_by_id: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        quiet_sub_logs: bool = False,
    ) -> Tuple[List[Dict[str, Any]], float]:
        return _process_ai2_unit_impl(
            self,
            selected_docs,
            raw_col=raw_col,
            neighbor_maps_by_id=neighbor_maps_by_id,
            quiet_sub_logs=quiet_sub_logs,
        )

    def clean_batch_records(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col,
        formal_col=None,
        shared_active_products: Any = None,
        shared_lock: Optional[threading.Lock] = None,
        quiet_sub_logs: bool = False,
    ) -> List[Dict[str, Any]]:
        return _clean_batch_records_impl(
            self,
            selected_docs,
            raw_col,
            formal_col=formal_col,
            shared_active_products=shared_active_products,
            shared_lock=shared_lock,
            quiet_sub_logs=quiet_sub_logs,
        )

    # 文本处理与判定纯静态委托（供测试与上层调用）
    _strip_markdown_link_wrapper = staticmethod(strip_markdown_link_wrapper)
    _normalize_inline_spaces = staticmethod(normalize_inline_spaces)
    _clean_coupon_label = staticmethod(clean_coupon_label)
    _extract_doc_images = staticmethod(extract_doc_images)
    _format_raw_message_for_ai1 = staticmethod(format_raw_message_for_ai1)
    _assemble_combined_text_and_images = staticmethod(assemble_combined_text_and_images)
    _sanitize_clean_tutorial_step = staticmethod(sanitize_clean_tutorial_step)
    _sanitize_card_highlight = staticmethod(sanitize_card_highlight)
    _sanitize_polished_copywriting = staticmethod(sanitize_polished_copywriting)
    _is_explicit_coupon_target = staticmethod(is_explicit_coupon_target)
    _build_clean_original_copywriting = staticmethod(build_clean_original_copywriting)
    _should_upgrade_existing_deal = staticmethod(should_upgrade_existing_deal)

    @classmethod
    def _lookup_active_formal_products(
        cls,
        formal_col,
        pid_keys: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        return query_active_formal_products(formal_col, pid_keys=pid_keys)


__all__ = [
    "DEAL_FORMAL_EXPIRY_HOURS",
    "DealPoolCleaner",
    "MaintenanceWorker",
    "StageWorker",
    "_commit_batch_results_to_db",
    "_is_safe_public_http_url",
    "_is_verified_station_promo_link",
    "commit_batch_results_to_db",
    "detect_candidate_platform",
    "rebuild_clean_official_venue_url",
    "unwrap_to_official_public_venue_url",
    "upsert_cleaned_products_to_formal_table",
]
