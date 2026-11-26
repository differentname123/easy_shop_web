# -*- coding: utf-8 -*-
"""线报流水线批量入库提交、异步阶段工作线程 (`StageWorker`) 与后台保鲜清扫线程 (`MaintenanceWorker`)。"""
from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pymongo import UpdateOne

from infrastructure.runtime_io import atomic_write_json, should_write_runtime_artifact
from models.deal_constants import (
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEFAULT_STAGE_POLL_INTERVAL_SECONDS,
    LOG_DIR,
    OK_SKIPPED_ALREADY_CLEANED,
    STATS_SNAPSHOT_PATH,
)
from repositories.deal_raw_pool_repo import DealRawPoolRepository, record_deal_display_transitions
from repositories.product_repo import upsert_cleaned_products_to_formal_table

logger = logging.getLogger("deal_pool_scheduler")


def commit_batch_results_to_db(
    batch_results: List[Dict[str, Any]],
    raw_col,
    formal_col,
    cycle_num: int,
) -> Dict[str, Any]:
    """将阶段产出统一写回初步表 `deal_raw_tips` 与正式表 `products`。"""
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    raw_status_ops = []
    batch_success_product_docs: List[Dict[str, Any]] = []
    batch_early_terminated_results: List[Dict[str, Any]] = []
    batch_failed_samples: List[Dict[str, Any]] = []
    retry_pending_count = 0

    for r in batch_results:
        s_site = r["source_site"]
        s_id = r["source_item_id"]
        common_ai_fields = {
            "cleaned_at": now_utc,
            "ai_stage1_result": r.get("ai_stage1_result"),
            "converted_links": r.get("converted_links") or [],
            "ai_stage2_result": r.get("ai_stage2_result"),
        }
        if r.get("original_copywriting"):
            common_ai_fields["original_copywriting"] = r["original_copywriting"]

        stage_outcome = r.get("stage_outcome")
        if stage_outcome == "advance_to_convert":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            "clean_stage": CLEAN_STAGE_NEED_CONVERT,
                            "ai_stage1_result": r.get("ai_stage1_result"),
                            "stage1_cleaned_at": now_utc,
                        },
                        "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                    },
                )
            )
        elif stage_outcome == "advance_to_ai2":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            "clean_stage": CLEAN_STAGE_NEED_AI2,
                            "converted_links": r.get("converted_links") or [],
                            "stage2_converted_at": now_utc,
                        },
                        "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                    },
                )
            )
        elif r["status"] == "success":
            batch_success_product_docs.append(r["product_doc"])
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_success",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": "OK",
                            "clean_error_msg": "",
                            "resolved_platform": r["platform"],
                            "resolved_product_id": r["product_id"],
                            "resolved_goods_sign": r["goods_sign"],
                            "resolved_official_title": r["official_title"],
                            "merged_tip_ids": r.get("merged_tip_ids") or [s_id],
                        },
                        "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                    },
                )
            )
        elif r["status"] == "early_terminated":
            batch_early_terminated_results.append(r)
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_success",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": OK_SKIPPED_ALREADY_CLEANED,
                            "clean_error_msg": r["error_msg"],
                            "resolved_platform": r.get("platform"),
                            "resolved_product_id": r.get("product_id"),
                            "resolved_goods_sign": r.get("goods_sign") or "",
                            "resolved_official_title": r.get("official_title") or "",
                            "merged_tip_ids": r.get("merged_tip_ids") or [s_id],
                        },
                        "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                    },
                )
            )
        elif r["status"] == "retry_pending":
            retry_pending_count += 1
            raw_rcnt = r.get("retry_count")
            r_cnt = int(raw_rcnt) if raw_rcnt is not None else 1
            base_retry_sec = 300.0 if r.get("clean_stage") == CLEAN_STAGE_NEED_CONVERT else 180.0
            raw_delay = r.get("retry_delay_sec")
            retry_delay = (
                max(15.0, float(raw_delay))
                if raw_delay is not None
                else (base_retry_sec * max(1, r_cnt))
            )
            retry_set = {
                "last_clean_attempt_at": now_utc,
                "clean_status": "pending",
                "clean_retry_count": r_cnt,
                "next_retry_after_ts": round(time.time() + retry_delay, 1),
                "clean_error_code": r["error_code"],
                "clean_error_msg": r["error_msg"],
                "ai_stage1_result": r.get("ai_stage1_result"),
                "converted_links": r.get("converted_links") or [],
                "ai_stage2_result": r.get("ai_stage2_result"),
            }
            if r.get("clean_stage"):
                retry_set["clean_stage"] = r["clean_stage"]
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": retry_set,
                        "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                    },
                )
            )
        else:
            fail_set: Dict[str, Any] = {
                **common_ai_fields,
                "clean_status": "cleaned_failed",
                "clean_stage": CLEAN_STAGE_DONE,
                "clean_error_code": r["error_code"],
                "clean_error_msg": r["error_msg"],
                "clean_attempt_errors": r.get("attempt_errors") or [],
            }
            if "retry_count" in r:
                fail_set["clean_retry_count"] = int(r["retry_count"])
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": fail_set,
                        "$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""},
                    },
                )
            )
            batch_failed_samples.append({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "cycle_num": cycle_num,
                "source_site": s_site,
                "source_channel": r.get("source_channel"),
                "source_item_id": s_id,
                "error_code": r["error_code"],
                "error_msg": r["error_msg"],
                "title": r.get("title"),
                "attempt_errors": r.get("attempt_errors") or [],
            })

    if raw_status_ops:
        try:
            raw_col.bulk_write(raw_status_ops, ordered=False)
        except Exception as e:
            logger.error("回写初步表 `deal_raw_tips` 清洗状态异常: %s", e)

    formal_write_res = {"upserted": 0, "modified": 0, "matched": 0, "protected_high_score": 0, "total_ops": 0}
    if batch_success_product_docs:
        try:
            formal_write_res = upsert_cleaned_products_to_formal_table(formal_col, batch_success_product_docs)
            if formal_write_res.get("started_deals") or formal_write_res.get("replaced_deals"):
                record_deal_display_transitions(
                    raw_col,
                    formal_write_res.get("started_deals") or [],
                    formal_write_res.get("replaced_deals") or [],
                )
        except Exception as e:
            logger.exception("写入正式表 `products` 发生异常: %s", e)

    early_resonance_modified = 0
    if batch_early_terminated_results:
        grouped_res: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for er_item in batch_early_terminated_results:
            ru = er_item.get("resonance_update") or {}
            plat = str(ru.get("platform") or er_item.get("platform") or "").strip()
            pid = str(ru.get("product_id") or er_item.get("product_id") or "").strip()
            if not plat or not pid:
                continue
            bucket = grouped_res.setdefault(
                (plat, pid),
                {"seen_inc": 0, "hit_inc": 0, "sites": set(), "channels": set()},
            )
            bucket["seen_inc"] += max(1, int(ru.get("seen_count") or 1))
            bucket["hit_inc"] += 1
            s_site = str(ru.get("source_site") or "").strip()
            if s_site:
                bucket["sites"].add(s_site)
            for ch in ru.get("seen_channels") or []:
                ch_s = str(ch or "").strip()
                if ch_s:
                    bucket["channels"].add(ch_s)

        early_ops = []
        for (plat, pid), b in grouped_res.items():
            upd_doc: Dict[str, Any] = {
                "$inc": {
                    "deal_info.total_seen_count": b["seen_inc"],
                    "deal_info.tip_hit_count": b["hit_inc"],
                }
            }
            add_set: Dict[str, Any] = {}
            if b["sites"]:
                add_set["deal_info.resonance_sites"] = {"$each": sorted(b["sites"])}
            if b["channels"]:
                add_set["deal_info.resonance_channels"] = {"$each": sorted(b["channels"])}
            if add_set:
                upd_doc["$addToSet"] = add_set
            early_ops.append(UpdateOne({"platform": plat, "product_id": pid}, upd_doc, upsert=False))
        if early_ops:
            try:
                er_res = formal_col.bulk_write(early_ops, ordered=False)
                early_resonance_modified = er_res.modified_count
            except Exception as e:
                logger.warning("累加提前终止商品共振热度异常: %s", e)

    return {
        "formal_write_res": formal_write_res,
        "early_resonance_modified": early_resonance_modified,
        "success_product_docs": batch_success_product_docs,
        "early_terminated_results": batch_early_terminated_results,
        "failed_samples": batch_failed_samples,
        "retry_pending_count": retry_pending_count,
    }


_commit_batch_results_to_db = commit_batch_results_to_db


class StageWorker(threading.Thread):
    """阶段工作线程：按阶段原子认领任务并即时推进至下一阶段。"""

    def __init__(
        self,
        name: str,
        stage: str,
        repo: DealRawPoolRepository,
        cleaner: Any,
        batch_size: int,
        lease_seconds: int = 300,
        poll_interval: float = DEFAULT_STAGE_POLL_INTERVAL_SECONDS,
        stop_event: Optional[threading.Event] = None,
        ai_semaphore: Optional[threading.BoundedSemaphore] = None,
        upstream_event: Optional[threading.Event] = None,
        downstream_event: Optional[threading.Event] = None,
        quiet_sub_logs: bool = False,
    ):
        super().__init__(name=name, daemon=True)
        self.stage = stage
        self.repo = repo
        self.cleaner = cleaner
        if ai_semaphore is not None and hasattr(cleaner, "set_ai_semaphore"):
            cleaner.set_ai_semaphore(ai_semaphore)
        self.batch_size = max(1, batch_size)
        self.lease_seconds = max(10, lease_seconds)
        self.poll_interval = max(0.5, float(poll_interval))
        self.stop_event = stop_event or threading.Event()
        self.ai_semaphore = ai_semaphore
        self.upstream_event = upstream_event
        self.downstream_event = downstream_event
        self.quiet_sub_logs = quiet_sub_logs
        self.worker_id = f"{name}_{os.getpid()}_{threading.get_ident()}"
        self.total_processed = 0

    def _compute_safe_lease(self, limit: int) -> int:
        if limit <= 0:
            return self.lease_seconds
        if self.stage in (CLEAN_STAGE_NEED_AI1, CLEAN_STAGE_NEED_AI2):
            ai_chunk_size = max(
                1,
                getattr(self.cleaner, "ai2_batch_size" if self.stage == CLEAN_STAGE_NEED_AI2 else "ai_batch_size", 4),
            )
            ai_workers = max(1, getattr(self.cleaner, "ai_max_workers", 8))
            ai_timeout = float(getattr(self.cleaner, "ai_timeout", 90.0))
            rounds = (limit + (ai_chunk_size * ai_workers) - 1) // (ai_chunk_size * ai_workers)
            return max(self.lease_seconds, int((rounds + 2) * ai_timeout * 1.5))
        if self.stage == CLEAN_STAGE_NEED_CONVERT:
            conv_workers = max(1, getattr(self.cleaner, "convert_max_workers", 10))
            return max(self.lease_seconds, int((limit / conv_workers) * 2.0) + 120)
        return self.lease_seconds

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                safe_lease = self._compute_safe_lease(self.batch_size)
                claimed = self.repo.claim_stage_docs(
                    self.stage,
                    limit=self.batch_size,
                    lease_seconds=safe_lease,
                    worker_id=self.worker_id,
                )
                if not claimed:
                    if self.upstream_event is not None:
                        self.upstream_event.wait(timeout=self.poll_interval)
                        self.upstream_event.clear()
                    else:
                        self.stop_event.wait(timeout=self.poll_interval)
                    continue

                if self.stage == CLEAN_STAGE_NEED_AI1:
                    outcomes, _elapsed, _ = self.cleaner.process_ai1_unit(
                        claimed,
                        self.repo.col,
                        quiet_sub_logs=self.quiet_sub_logs,
                    )
                elif self.stage == CLEAN_STAGE_NEED_CONVERT:
                    outcomes, _elapsed = self.cleaner.process_convert_unit(
                        claimed,
                        formal_col=self.repo.formal_col,
                        quiet_sub_logs=self.quiet_sub_logs,
                    )
                elif self.stage == CLEAN_STAGE_NEED_AI2:
                    outcomes, _elapsed = self.cleaner.process_ai2_unit(
                        claimed,
                        raw_col=self.repo.col,
                        quiet_sub_logs=self.quiet_sub_logs,
                    )
                else:
                    logger.warning("[%s] 未知阶段标记: %s", self.name, self.stage)
                    self.stop_event.wait(timeout=self.poll_interval)
                    continue

                if outcomes:
                    commit_batch_results_to_db(
                        outcomes,
                        self.repo.col,
                        self.repo.formal_col,
                        cycle_num=0,
                    )
                    self.total_processed += len(outcomes)
                    adv_count = sum(
                        1
                        for o in outcomes
                        if o.get("stage_outcome") in ("advance_to_convert", "advance_to_ai2", "success")
                    )
                    if self.downstream_event is not None and adv_count > 0:
                        self.downstream_event.set()
            except Exception as e:
                logger.exception("🚨 [%s] 阶段工作线程处理异常，稍后继续: %s", self.name, e)
                self.stop_event.wait(timeout=self.poll_interval)


class MaintenanceWorker(threading.Thread):
    """后台清扫维护线程：租约回收、统一保鲜与重试上限归档、正式库好价标摘除与统计看板更新。"""

    def __init__(
        self,
        repo: DealRawPoolRepository,
        poll_interval: float = 30.0,
        stop_event: Optional[threading.Event] = None,
    ):
        super().__init__(name="DealMaintenanceWorker", daemon=True)
        self.repo = repo
        self.poll_interval = max(5.0, float(poll_interval))
        self.stop_event = stop_event or threading.Event()

    def run_once(self, tick: int = 1) -> None:
        """执行单次维护周期，便于确定性单测与常驻循环复用。"""
        reclaimed = self.repo.reclaim_expired_leases()
        if reclaimed > 0:
            logger.info("🧹 [维护清扫] 释放超时租约 %d 条回队", reclaimed)

        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        sweep_res = self.repo.sweep_expired_and_max_retry(now_utc=now_utc)
        if sweep_res["expired_swept"] > 0 or sweep_res["max_retry_swept"] > 0:
            logger.info(
                "🧹 [维护保鲜清扫] 保鲜过期=%d条, 重试满限=%d条",
                sweep_res["expired_swept"],
                sweep_res["max_retry_swept"],
            )

        if tick % 30 == 0:
            exp_cnt = self.repo.expire_stale_formal_deals(now_utc=now_utc)
            if exp_cnt > 0:
                logger.info(
                    "🧹 [正式库好价保鲜] 已摘除超 %.0f 小时未刷新好价标 %d 个",
                    DEAL_FORMAL_EXPIRY_HOURS,
                    exp_cnt,
                )

        summary = self.repo.generate_pool_summary()
        if should_write_runtime_artifact(STATS_SNAPSHOT_PATH, LOG_DIR):
            atomic_write_json(STATS_SNAPSHOT_PATH, summary)

        if tick % 10 == 0:
            p_stages = summary.get("pending_by_stage") or {}
            logger.info(
                "📈 [全库阶段心跳看板] 总线报=%d | 成功=%d, 失败=%d | 待清洗=%d (AI1=%d, 转链=%d, AI2=%d) | 正式好价=%d",
                summary.get("total_records", 0),
                summary.get("cleaned_success_records", 0),
                summary.get("cleaned_failed_records", 0),
                summary.get("pending_clean_records", 0),
                p_stages.get("need_ai1", 0),
                p_stages.get("need_convert", 0),
                p_stages.get("need_ai2", 0),
                summary.get("formal_deal_products_count", 0),
            )

    def run(self) -> None:
        tick = 0
        while not self.stop_event.is_set():
            tick += 1
            try:
                self.run_once(tick)
            except Exception as e:
                logger.warning("维护线程执行异常: %s", e)

            self.stop_event.wait(timeout=self.poll_interval)
