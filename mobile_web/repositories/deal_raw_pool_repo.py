# -*- coding: utf-8 -*-
"""
好价线报初步缓冲池仓储 (`deal_raw_tips`) 与正式商品表 (`products`) 关联管理。
统一收敛存储、TTL 索引、增量去重、状态机租约、指标聚合与审计落盘。
"""
import copy
import datetime
import json
import logging
import os
import re
import threading
import time
from itertools import zip_longest
from typing import Any, Dict, List, Optional, Set, Tuple

from pymongo import UpdateOne

from infrastructure.mongo import gen_db_object
from infrastructure.runtime_io import append_bounded_jsonl, should_write_runtime_artifact
from models.deal_constants import (
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEAL_MAX_CLEAN_AGE_DAYS,
    DEAL_RAW_TTL_DAYS,
    DEFAULT_FORMAL_PRODUCTS_COLLECTION,
    DEFAULT_RAW_COLLECTION_NAME,
    ERROR_LOG_JSONL_PATH,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_EXPIRED_OVER_7_DAYS,
    LOCAL_FALLBACK_JSONL,
    LOG_DIR,
    MAX_CLEAN_RETRIES,
    MAX_JSONL_FILE_BYTES,
    UPSTREAM_DISAPPEARED,
    get_default_site_max_age_days_map,
    parse_publish_time_epoch,
    resolve_deal_max_age_days,
)

logger = logging.getLogger("deal_pool_scheduler")


def _append_bounded_jsonl_lines(
    path: str,
    lines: List[str],
    *,
    max_bytes: int = MAX_JSONL_FILE_BYTES,
    backup_count: int = 2,
) -> None:
    """线程安全且受磁盘容量上限约束地追加 JSONL 记录，超限时自动按 .1 / .2 滚动归档。"""
    if not should_write_runtime_artifact(path, LOG_DIR):
        return
    append_bounded_jsonl(path, lines, max_bytes=max_bytes, backup_count=backup_count)


def append_error_records_jsonl(
    error_records: List[Dict[str, Any]],
    *,
    path: Optional[str] = None,
) -> None:
    """将本轮捕获的所有列表级与详情级错误路径持久化追加写入 logs/deal_pool_errors.jsonl。"""
    if not error_records:
        return
    target = path or ERROR_LOG_JSONL_PATH
    try:
        _append_bounded_jsonl_lines(
            target,
            [json.dumps(item, ensure_ascii=False) for item in error_records],
        )
    except Exception as e:
        logger.error("写入错误路径日志 [%s] 失败: %s", target, e)


_SIGNAL_URL_OR_DOMAIN_KEYWORDS = (
    "http://",
    "https://",
    "jd.com",
    "3.cn",
    "jingfen",
    "tb.cn",
    "taobao",
    "tmall",
    "uland",
    "yangkeduo",
    "pinduoduo",
)

_SIGNAL_TOKEN_SYMBOLS = (
    "￥",
    "¥",
    "$",
    "€",
    "₴",
    "£",
    "¢",
    "!",
    "！",
    "/",
    "口令",
)

_SIGNAL_SKU_KEYWORDS = (
    "货号",
    "编号",
    "编码",
    "sku",
    "itemid",
    "商品id",
)


def has_potential_product_signal(record: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    【遵循第一阶段极简准则】：不使用任何复杂脆弱的正则表达式，仅通过最基础的关键字或符号
    判断该记录中“是否可能包含想要的商品信息”。只要命中任一基础信号，就值得入初步表 `deal_raw_tips`。
    """
    signal_types: List[str] = []

    links = record.get("extracted_links") or []
    tokens = record.get("extracted_tokens") or []

    if any(isinstance(lk, dict) and (lk.get("url") or lk.get("item_id")) for lk in links):
        signal_types.append("extracted_links")
    if any(str(tk or "").strip() for tk in tokens):
        signal_types.append("extracted_tokens")

    text_blob = f"{record.get('title') or ''}\n{record.get('tutorial_text') or ''}".lower()
    if any(kw in text_blob for kw in _SIGNAL_URL_OR_DOMAIN_KEYWORDS):
        if "url_or_domain_keyword" not in signal_types:
            signal_types.append("url_or_domain_keyword")
    if any(sym in text_blob for sym in _SIGNAL_TOKEN_SYMBOLS):
        if "token_symbol" not in signal_types:
            signal_types.append("token_symbol")
    if any(kw in text_blob for kw in _SIGNAL_SKU_KEYWORDS):
        if "sku_keyword" not in signal_types:
            signal_types.append("sku_keyword")

    return (len(signal_types) > 0), signal_types


def _is_pure_jd_coupon_only_record(record: Dict[str, Any]) -> bool:
    """
    识别“整条消息仅包含京东官方原生领券链接 (`coupon.m.jd.com`) 且不含任何其他商品链接或口令”的纯防身领券广播：
    - 群聊流 (`is_group_stream=True`) 中将其存为 `context_only`；
    - 独立帖流 (`is_group_stream=False`) 中无具体主商品可直接跳过。
    """
    tokens = record.get("extracted_tokens") or []
    if any(str(tk or "").strip() for tk in tokens):
        return False
    links = record.get("extracted_links") or []
    urls = [
        str(lk.get("url") or lk.get("item_id") or "").strip()
        for lk in links
        if isinstance(lk, dict) and str(lk.get("url") or lk.get("item_id") or "").strip()
    ]
    if not urls or not all("coupon.m.jd.com" in u.lower() for u in urls):
        return False

    text_rest = f"{record.get('title') or ''}\n{record.get('tutorial_text') or ''}"
    for u in urls:
        text_rest = text_rest.replace(u, " ")
    text_rest = re.sub(r"\[(?:群聊晒单/教程图片|教程图片):\s*https?://[^\]]+\]", " ", text_rest)
    text_rest = re.sub(r"https?://\S*coupon\.m\.jd\.com\S*", " ", text_rest, flags=re.I)
    lower_rest = text_rest.lower()
    if any(kw in lower_rest for kw in ("http://", "https://", "u.jd.com", "3.cn", "jingfen", "tb.cn", "taobao", "tmall", "uland", "yangkeduo", "pinduoduo")):
        return False
    if any(sym in text_rest for sym in ("￥", "¥", "$", "€", "₴", "£", "¢", "口令")):
        return False
    return True


def evaluate_quality_signals(record: Dict[str, Any]) -> Dict[str, Any]:
    """计算单条线报的极简信号结构 (`quality_signals`)，彻底移除复杂正则提取器。"""
    has_signal, signal_types = has_potential_product_signal(record)
    links = record.get("extracted_links") or []
    tokens = record.get("extracted_tokens") or []
    images = record.get("tutorial_images") or []
    hot_metrics = record.get("hot_metrics") or {}

    comments = 0
    if isinstance(hot_metrics, dict):
        try:
            comments = int(hot_metrics.get("comment_count") or hot_metrics.get("comments") or 0)
        except Exception:
            comments = 0

    return {
        "has_unique_product_anchor": has_signal,
        "unique_anchor_types": signal_types,
        "quality_tier": "A_含潜在商品信号" if has_signal else "B_无明显商品信号",
        "has_price": bool(record.get("price") is not None or record.get("price_text")),
        "ecommerce_link_count": len(links),
        "token_count": len(tokens),
        "image_count": len(images) if images else (1 if record.get("image_url") else 0),
        "has_multi_links": len(links) > 1,
        "comments": comments,
    }


class DealRawPoolRepository:
    """好价线报初步缓冲池仓储 (`deal_raw_tips`) + 正式商品表 (`products`) 关联管理。"""

    def __init__(
        self,
        collection_name: str = DEFAULT_RAW_COLLECTION_NAME,
        formal_collection_name: str = DEFAULT_FORMAL_PRODUCTS_COLLECTION,
        db_instance=None,
        site_max_age_map: Optional[Dict[str, float]] = None,
    ):
        self.collection_name = collection_name
        self.formal_collection_name = formal_collection_name
        self._db = db_instance
        self._indexes_ready = False
        self.site_max_age_map: Dict[str, float] = (
            dict(site_max_age_map) if site_max_age_map is not None else get_default_site_max_age_days_map()
        )

    @property
    def db(self):
        if self._db is None:
            self._db = gen_db_object()
        return self._db

    @property
    def col(self):
        if not self._indexes_ready:
            self._ensure_indexes()
        return self.db.get_collection(self.collection_name)

    @property
    def formal_col(self):
        if not self._indexes_ready:
            self._ensure_indexes()
        return self.db.get_collection(self.formal_collection_name)

    def _ensure_indexes(self):
        try:
            col = self.db.get_collection(self.collection_name)
            col.create_index([("source_site", 1), ("source_item_id", 1)], unique=True, background=True)
            col.create_index([("last_seen_at", -1)], background=True)
            col.create_index([("cleaned_at", -1)], background=True)
            col.create_index(
                [("clean_status", 1), ("source_site", 1), ("publish_ts", -1), ("last_seen_at", -1)],
                background=True,
            )
            col.create_index([("clean_error_code", 1)], background=True)
            col.create_index([("quality_score", -1)], background=True)
            col.create_index(
                [("source_site", 1), ("is_group_stream", 1), ("group_id", 1), ("publish_ts", 1)],
                background=True,
            )
            col.create_index(
                [("quality_signals.has_unique_product_anchor", 1), ("clean_status", 1)],
                background=True,
            )
            col.create_index([("platform", 1), ("quality_signals.quality_tier", 1)], background=True)
            col.create_index(
                [("clean_status", 1), ("clean_stage", 1), ("stage_lease_until_ts", 1), ("publish_ts", -1)],
                background=True,
            )
            col.create_index(
                [("stage_lease_until_ts", 1), ("stage_lease_owner", 1)],
                background=True,
            )
            col.create_index(
                [("first_seen_at", 1)],
                expireAfterSeconds=int(DEAL_RAW_TTL_DAYS * 86400),
                name="idx_deal_raw_tips_ttl_14d",
                background=True,
            )

            pcol = self.db.get_collection(self.formal_collection_name)
            pcol.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
            pcol.create_index("goods_sign", background=True)
            pcol.create_index([("updated_at", -1)], background=True)
            pcol.create_index([("deal_info.is_deal_tip", 1), ("deal_info.quality_score", -1), ("updated_at", -1)], background=True)
            pcol.create_index([("deal_info.is_deal_tip", 1), ("deal_info.updated_at", 1)], background=True)
            self._indexes_ready = True
        except Exception as e:
            logger.warning("初始化集合索引时发生异常（稍后自动重试）: %s", e)

    def expire_stale_formal_deals(
        self,
        now_utc: Optional[datetime.datetime] = None,
        max_age_hours: float = DEAL_FORMAL_EXPIRY_HOURS,
    ) -> int:
        """将超过保鲜窗口未再被任何新线报刷新的好价商品自动摘除实时好价标 (`deal_info.is_deal_tip = False`)。"""
        ref_now = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        cutoff = ref_now - datetime.timedelta(hours=max(1.0, float(max_age_hours)))
        try:
            res = self.formal_col.update_many(
                {
                    "deal_info.is_deal_tip": True,
                    "deal_info.updated_at": {"$lt": cutoff},
                },
                {
                    "$set": {
                        "deal_info.is_deal_tip": False,
                        "deal_info.expired_at": ref_now,
                        "deal_info.updated_at": ref_now,
                    }
                },
            )
            return int(getattr(res, "modified_count", 0) or 0)
        except Exception as e:
            logger.warning("清理超期好价标识 (`deal_info.is_deal_tip`) 异常: %s", e)
            return 0

    def reset_raw_collection(self) -> int:
        """清空初步表，并彻底清除正式表内的好价线报单品及重叠推荐流单品上的好价清洗字段。"""
        deleted_raw = 0
        try:
            res = self.col.delete_many({})
            deleted_raw = int(getattr(res, "deleted_count", 0) or 0)
            logger.info("🧹 已清空初步表 [%s]，共删除 %d 条记录。", self.collection_name, deleted_raw)
        except Exception as e:
            logger.error("清空初步表 [%s] 失败: %s", self.collection_name, e)

        try:
            res_formal_del = self.formal_col.delete_many({
                "$or": [
                    {"source": "deal_pool"},
                    {"source": {"$exists": False}, "last_clean_source": "deal_pool"},
                ]
            })
            res_formal_unset = self.formal_col.update_many(
                {
                    "$or": [
                        {"deal_info": {"$exists": True}},
                        {"deal_source": {"$exists": True}},
                        {"deal_source_name": {"$exists": True}},
                        {"last_clean_source": "deal_pool"},
                    ]
                },
                {
                    "$unset": {
                        "deal_info": "",
                        "deal_source": "",
                        "deal_source_name": "",
                        "last_clean_source": "",
                    }
                },
            )
            logger.info(
                "🧹 已清理正式表 [%s] 好价数据：删除纯线报单品 %d 条，剥离推荐流重叠单品好价字段 %d 条。",
                self.formal_collection_name,
                int(getattr(res_formal_del, "deleted_count", 0) or 0),
                int(getattr(res_formal_unset, "modified_count", 0) or 0),
            )
        except Exception as e:
            logger.warning("清理正式表好价数据异常: %s", e)

        return deleted_raw

    def get_existing_detailed_ids(
        self,
        source_site: str,
        limit: int = 50000,
        channel: str = "",
    ) -> Set[str]:
        """获取某站点已落盘且无需重复拉取详情的 `source_item_id` 集合：
        包含 pending、cleaned_success、cleaned_low_score、merged_into_parent 以及所有终态 cleaned_failed；
        仅放行因上次全量巡视暂离榜单 (`UPSTREAM_DISAPPEARED`) 而下架的记录，使其重新上榜时可被再次抓取。
        """
        try:
            query: Dict[str, Any] = {
                "source_site": source_site,
                "has_full_detail": True,
                "$or": [
                    {"clean_status": {"$in": ["pending", "cleaned_success", "cleaned_low_score", "merged_into_parent"]}},
                    {"clean_status": "cleaned_failed", "clean_error_code": {"$ne": UPSTREAM_DISAPPEARED}},
                ],
            }
            if channel:
                query["source_channel"] = channel
            cursor = (
                self.col.find(
                    query,
                    {"_id": 0, "source_item_id": 1},
                )
                .sort("last_seen_at", -1)
                .limit(limit)
            )
            return {str(doc["source_item_id"]) for doc in cursor if doc.get("source_item_id")}
        except Exception as e:
            logger.warning("读取站点 [%s] 已存详情 ID 列表异常: %s", source_site, e)
            return set()

    def touch_seen(
        self,
        source_site: str,
        item_ids: Any,
        *,
        channel: str = "",
        refresh_publish_time: bool = False,
        max_age_days: Optional[float] = None,
        at: Optional[datetime.datetime] = None,
    ) -> int:
        """把"本轮在列表里再次见到"的已存商品标记为仍在架：刷新 `last_seen_at`。

        当频道配置显式传入 `refresh_publish_time=True`（即该源以每次拉取时间为保鲜基准）时，
        同时刷新 `publish_time` / `publish_ts` / `clean_cutoff_ts` 并同步更新正式表 `deal_info`。
        `seen_count` 刻意不自增，避免全量巡视污染“被重复推荐次数”指标。
        """
        site = str(source_site or "").strip()
        ids = [str(i).strip() for i in (item_ids or []) if str(i or "").strip()]
        if not site or not ids:
            return 0
        if at is None:
            now_dt = datetime.datetime.now(datetime.timezone.utc)
        elif at.tzinfo is None:
            now_dt = at.replace(tzinfo=datetime.timezone.utc)
        else:
            now_dt = at.astimezone(datetime.timezone.utc)
        now = now_dt.replace(tzinfo=None)
        update_doc: Dict[str, Any] = {"$set": {"last_seen_at": now}}
        if channel:
            update_doc["$addToSet"] = {"seen_channels": channel}

        now_ts = now_dt.timestamp()
        if refresh_publish_time:
            eff_age = float(max_age_days) if max_age_days is not None else resolve_deal_max_age_days(
                channel_name=channel,
                site_key=site,
                default_days=DEAL_MAX_CLEAN_AGE_DAYS,
            )
            update_doc["$set"]["publish_ts"] = now_ts
            update_doc["$set"]["clean_cutoff_ts"] = round(now_ts + eff_age * 86400.0, 1)
            update_doc["$set"]["clean_max_age_days"] = eff_age

        ops = [
            UpdateOne(
                {
                    "source_site": site,
                    "source_item_id": item_id,
                    "clean_status": {"$in": ["pending", "cleaned_success"]},
                },
                update_doc,
            )
            for item_id in ids
        ]
        matched_cnt = 0
        try:
            res = self.col.bulk_write(ops, ordered=False)
            matched_cnt = int(res.matched_count)
        except Exception as e:
            logger.warning("刷新站点 [%s] 本轮确认时间失败: %s", site, e)

        if refresh_publish_time and ids:
            try:
                id_variants = list(set(ids + [int(i) for i in ids if i.isdigit()]))
                self.formal_col.update_many(
                    {
                        "deal_info.source_site": site,
                        "$or": [
                            {"deal_info.source_item_id": {"$in": id_variants}},
                            {"deal_info.merged_tip_ids": {"$in": id_variants}},
                        ],
                    },
                    {
                        "$set": {
                            "deal_info.publish_ts": now_ts,
                            "deal_info.updated_at": now,
                            "deal_info.is_deal_tip": True,
                        }
                    },
                )
            except Exception as e:
                logger.warning("刷新正式表商品确认时间失败: %s", e)

        return matched_cnt

    def expire_missing_from_full_sweep(
        self,
        source_site: str,
        channel: str,
        sweep_start_time: datetime.datetime,
    ) -> Dict[str, int]:
        """【全量巡视 Fail-Closed 失效清除机制】：
        对执行全量拉取 (full_sweep=True) 的站点或频道，未在本次拉取列表中出现的历史商品（last_seen_at < sweep_start_time），
        代表上游已下架或失效：
        1. 初步表 deal_raw_tips：处于 pending 状态的记录直接置为 cleaned_failed，
           错误码 UPSTREAM_DISAPPEARED，避免无效消耗下游 AI 算力与转链请求；
        2. 正式表 products：属于该站点对应频道的在架好价商品 (deal_info.is_deal_tip=True)，
           若 updated_at < sweep_start_time，立即摘除好价标 (is_deal_tip=False) 并记录 expired_at。
        """
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_naive = now_dt.replace(tzinfo=None)
        res = {"raw_expired": 0, "formal_expired": 0}

        try:
            # 1. 清理初步表中 pending 的失效记录
            raw_query: Dict[str, Any] = {
                "source_site": source_site,
                "last_seen_at": {"$lt": sweep_start_time},
                "$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}],
            }
            if channel:
                raw_query["source_channel"] = channel
            raw_res = self.col.update_many(
                raw_query,
                {
                    "$set": {
                        "clean_status": "cleaned_failed",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": UPSTREAM_DISAPPEARED,
                        "clean_error_msg": "全量线报巡视未包含此商品，上游已下架失效",
                        "cleaned_at": now_naive,
                    },
                    "$unset": {
                        "stage_lease_owner": "",
                        "stage_lease_until_ts": "",
                    },
                },
            )
            res["raw_expired"] = int(getattr(raw_res, "modified_count", 0) or 0)

            # 2. 清理正式表中属于该频道的好价标
            formal_query: Dict[str, Any] = {
                "deal_info.source_site": source_site,
                "deal_info.is_deal_tip": True,
                "deal_info.updated_at": {"$lt": sweep_start_time},
            }
            if channel:
                formal_query["$or"] = [
                    {"deal_info.source_channel": channel},
                    {"deal_info.seen_channels": channel},
                ]
            formal_res = self.formal_col.update_many(
                formal_query,
                {
                    "$set": {
                        "deal_info.is_deal_tip": False,
                        "deal_info.expired_at": now_naive,
                        "deal_info.updated_at": now_naive,
                        "deal_info.expire_reason": "full_sweep_disappeared",
                    }
                },
            )
            res["formal_expired"] = int(getattr(formal_res, "modified_count", 0) or 0)
            if res["raw_expired"] or res["formal_expired"]:
                logger.info(
                    "  🧹 [全量巡视-失效清除] 站点 [%s] 频道 [%s]: 清理初步表待清洗 %d 条, 摘除正式表失效好价 %d 条",
                    source_site,
                    channel,
                    res["raw_expired"],
                    res["formal_expired"],
                )
        except Exception as e:
            logger.warning("全量巡视失效清除异常 [%s - %s]: %s", source_site, channel, e)

        return res

    def save_batch(
        self,
        records: List[Dict[str, Any]],
        task_name: str = "",
        *,
        refresh_publish_time: bool = False,
        max_age_days: Optional[float] = None,
    ) -> Dict[str, int]:
        """【Stage 1：源站拉取入初步表 `deal_raw_tips`】"""
        if not records:
            return {"inserted": 0, "updated": 0, "skipped_no_signal": 0, "context_only_saved": 0, "total": 0}

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ops = []
        skipped_no_signal = 0
        context_only_saved = 0

        # 仅当此前因全量巡视暂离榜单 (UPSTREAM_DISAPPEARED) 而下架的记录重新出现时，才将其重置为 pending 重新清洗；
        # 其余确定性失败（如已停止推广、纯数字SKU、券链失效、货不对板、低分等）严禁重复洗库。
        ids_by_site: Dict[str, List[str]] = {}
        for rec in records:
            if not isinstance(rec, dict):
                continue
            s_key = str(rec.get("source_site") or "").strip()
            s_id = str(rec.get("source_item_id") or "").strip()
            if s_key and s_id:
                ids_by_site.setdefault(s_key, []).append(s_id)
        for s_key, s_ids in ids_by_site.items():
            try:
                self.col.update_many(
                    {
                        "source_site": s_key,
                        "source_item_id": {"$in": s_ids},
                        "clean_status": "cleaned_failed",
                        "clean_error_code": UPSTREAM_DISAPPEARED,
                    },
                    {
                        "$set": {
                            "clean_status": "pending",
                            "clean_stage": CLEAN_STAGE_NEED_AI1,
                            "clean_retry_count": 0,
                            "clean_error_code": "",
                            "clean_error_msg": "",
                        },
                        "$unset": {
                            "ai_stage1_result": "",
                            "converted_links": "",
                            "stage3_converted_links": "",
                            "ai_stage2_result": "",
                            "next_retry_after_ts": "",
                            "stage_lease_owner": "",
                            "stage_lease_until_ts": "",
                        },
                    },
                )
            except Exception as e:
                logger.warning("重置重新上榜的暂离线报状态异常 (%s): %s", s_key, e)

        for rec in records:
            if not isinstance(rec, dict):
                continue
            site = str(rec.get("source_site") or "").strip()
            item_id = str(rec.get("source_item_id") or "").strip()
            if not site or not item_id:
                continue

            has_full_detail = bool(rec.get("has_full_detail", True))
            quality_signals = evaluate_quality_signals(rec)
            is_group_stream = bool(rec.get("is_group_stream"))
            hot_metrics = rec.get("hot_metrics") if isinstance(rec.get("hot_metrics"), dict) else {}
            group_id = str(hot_metrics.get("group_id") or "").strip()
            channel = str(rec.get("source_channel") or "").strip()
            raw_pub = rec.get("publish_ts") if rec.get("publish_ts") is not None else rec.get("publish_time")
            parsed_pub_ts = parse_publish_time_epoch(raw_pub)
            pub_ts = parsed_pub_ts if parsed_pub_ts is not None else time.time()
            channel_max_age = float(max_age_days) if max_age_days is not None else resolve_deal_max_age_days(
                channel_name=channel,
                site_key=site,
                default_days=DEAL_MAX_CLEAN_AGE_DAYS,
            )
            clean_cutoff_ts = round(pub_ts + channel_max_age * 86400.0, 1)
            is_expired_over_age = (
                parsed_pub_ts is not None
                and clean_cutoff_ts < time.time()
            )

            has_signal = bool(quality_signals["has_unique_product_anchor"])
            is_pure_coupon = has_full_detail and _is_pure_jd_coupon_only_record(rec)
            if has_full_detail and (not has_signal or is_pure_coupon):
                if is_group_stream:
                    initial_status = "context_only"
                    context_only_saved += 1
                else:
                    skipped_no_signal += 1
                    continue
            elif is_expired_over_age:
                initial_status = "cleaned_failed"
            else:
                initial_status = "pending"

            filter_spec = {"source_site": site, "source_item_id": item_id}

            source_url = str(rec.get("source_url") or "").strip()
            source_list_page_url = str(rec.get("source_list_page_url") or source_url or "").strip()
            source_detail_url = str(rec.get("source_detail_url") or source_url or source_list_page_url or "").strip()

            set_fields: Dict[str, Any] = {
                "last_seen_at": now,
                "last_task_name": task_name,
            }
            if hot_metrics:
                set_fields["hot_metrics"] = hot_metrics
            eff_refresh_pub = bool(refresh_publish_time or rec.get("_refresh_publish_time"))
            if eff_refresh_pub:
                set_fields["publish_ts"] = pub_ts
                set_fields["clean_cutoff_ts"] = clean_cutoff_ts
                set_fields["clean_max_age_days"] = channel_max_age

            set_on_insert: Dict[str, Any] = {
                "source_site": site,
                "source_item_id": item_id,
                "first_seen_at": now,
                "clean_status": initial_status,
                "clean_stage": CLEAN_STAGE_NEED_AI1 if initial_status == "pending" else CLEAN_STAGE_DONE,
                "clean_retry_count": 0,
                "clean_max_age_days": channel_max_age,
                "clean_cutoff_ts": clean_cutoff_ts,
            }
            if parsed_pub_ts is None:
                set_on_insert["publish_ts"] = pub_ts
            if is_expired_over_age and initial_status == "cleaned_failed":
                set_on_insert["clean_error_code"] = ERR_EXPIRED_OVER_7_DAYS
                set_on_insert["clean_error_msg"] = f"爆料发布时间超过渠道保鲜期 ({channel_max_age:.0f}天)，自动跳过清洗"

            core_fields = {
                "source_channel": channel,
                "source_url": source_url or source_detail_url,
                "source_list_page_url": source_list_page_url,
                "source_detail_url": source_detail_url,
                "is_group_stream": is_group_stream,
                "group_id": group_id,
                "fetched_at": rec.get("fetched_at") or "",
                "platform": rec.get("platform") or "unknown",
                "candidate_goods_id": rec.get("candidate_goods_id") or "",
                "title": rec.get("title") or "",
                "price_text": rec.get("price_text") or "",
                "price": rec.get("price"),
                "image_url": rec.get("image_url") or "",
                "mall_name": rec.get("mall_name") or "",
                "expire_time": rec.get("expire_time"),
                "tutorial_text": rec.get("tutorial_text") or "",
                "tutorial_images": rec.get("tutorial_images") or [],
                "extracted_links": rec.get("extracted_links") or [],
                "extracted_tokens": rec.get("extracted_tokens") or [],
                "raw_payload": rec.get("raw_payload"),
                "has_full_detail": has_full_detail,
                "quality_signals": quality_signals,
            }
            if parsed_pub_ts is not None:
                core_fields["publish_ts"] = parsed_pub_ts

            if has_full_detail:
                set_fields.update(core_fields)
                # MongoDB 禁止同一路径同时出现在 $set 与 $setOnInsert（错误码 40），
                # 以本次刷新的 $set 为准，把重复键从插入模板里剔除
                for _dup_key in [k for k in set_on_insert if k in set_fields]:
                    set_on_insert.pop(_dup_key)
                update_doc: Dict[str, Any] = {
                    "$set": set_fields,
                    "$setOnInsert": set_on_insert,
                    "$inc": {"seen_count": 1},
                }
                if channel:
                    update_doc["$addToSet"] = {"seen_channels": channel}
                ops.append(UpdateOne(filter_spec, update_doc, upsert=True))
            else:
                update_doc = {
                    "$set": set_fields,
                    "$inc": {"seen_count": 1},
                }
                if channel:
                    update_doc["$addToSet"] = {"seen_channels": channel}
                ops.append(UpdateOne(filter_spec, update_doc, upsert=False))

        if not ops:
            return {"inserted": 0, "updated": 0, "skipped_no_signal": skipped_no_signal, "context_only_saved": context_only_saved, "total": 0}

        try:
            res = self.col.bulk_write(ops, ordered=False)
            return {
                "inserted": res.upserted_count,
                "updated": res.modified_count,
                "skipped_no_signal": skipped_no_signal,
                "context_only_saved": context_only_saved,
                "total": len(ops),
            }
        except Exception as e:
            logger.error("❌ 批量写入 MongoDB [%s] 失败，降级写入本地 JSONL 备份: %s", self.collection_name, e)
            self._append_fallback_jsonl(records, task_name)
            return {
                "inserted": 0,
                "updated": 0,
                "skipped_no_signal": skipped_no_signal,
                "context_only_saved": context_only_saved,
                "total": len(ops),
                "error": str(e),
            }

    @staticmethod
    def _append_fallback_jsonl(records: List[Dict[str, Any]], task_name: str):
        try:
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            rows = []
            for rec in records:
                row = copy.deepcopy(rec)
                row["_fallback_saved_at"] = now_iso
                row["_task_name"] = task_name
                rows.append(json.dumps(row, ensure_ascii=False))
            _append_bounded_jsonl_lines(LOCAL_FALLBACK_JSONL, rows)
        except Exception as ex:
            logger.error("写入本地 JSONL 备份失败: %s", ex)

    def migrate_pending_to_stage_machine(self) -> Dict[str, int]:
        """全量幂等迁移历史数据至阶段状态机。"""
        res_pending = self.col.update_many(
            {"clean_status": "pending", "clean_stage": {"$exists": False}},
            {"$set": {"clean_stage": CLEAN_STAGE_NEED_AI1}},
        )
        res_done = self.col.update_many(
            {"clean_status": {"$ne": "pending"}, "clean_stage": {"$exists": False}},
            {"$set": {"clean_stage": CLEAN_STAGE_DONE}},
        )
        res_leases = self.col.update_many(
            {"clean_status": {"$ne": "pending"}, "stage_lease_owner": {"$exists": True}},
            {"$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""}},
        )
        migrated_pending = int(getattr(res_pending, "modified_count", 0) or 0)
        migrated_done = int(getattr(res_done, "modified_count", 0) or 0)
        cleared_leases = int(getattr(res_leases, "modified_count", 0) or 0)
        if migrated_pending > 0 or migrated_done > 0:
            logger.info(
                "🚀 [状态机版本迁移] 幂等补齐阶段标记: pending->need_ai1=%d条, non-pending->done=%d条, 清理租约=%d条",
                migrated_pending,
                migrated_done,
                cleared_leases,
            )
        return {
            "migrated_pending": migrated_pending,
            "migrated_done": migrated_done,
            "cleared_leases": cleared_leases,
        }

    def _select_fair_candidate_ids(
        self,
        base_query: Dict[str, Any],
        limit: int,
    ) -> List[Any]:
        """单次查询按最近发布序抽取上限内候选集，再按站点公平轮询选择最多 `limit` 个待认领 _id。"""
        if limit <= 0:
            return []
        fetch_cap = max(limit * 6, 120)
        docs = list(
            self.col.find(base_query, {"_id": 1, "source_site": 1, "clean_retry_count": 1})
            .sort([("publish_ts", -1), ("last_seen_at", -1)])
            .limit(fetch_cap)
        )
        if not docs:
            return []

        per_site: Dict[str, List[Dict[str, Any]]] = {}
        for doc in docs:
            s_key = str(doc.get("source_site") or "").strip() or "_unknown"
            per_site.setdefault(s_key, []).append(doc)

        queues: List[List[Any]] = []
        for s_key in sorted(per_site):
            bucket = per_site[s_key]
            bucket.sort(key=lambda d: int(d.get("clean_retry_count") or 0))
            queues.append([d["_id"] for d in bucket])

        chosen_ids: List[Any] = []
        chosen_set = set()
        for group in zip_longest(*queues):
            for doc_id in group:
                if doc_id is not None and doc_id not in chosen_set:
                    chosen_set.add(doc_id)
                    chosen_ids.append(doc_id)
                    if len(chosen_ids) >= limit:
                        return chosen_ids
        return chosen_ids

    def claim_stage_docs(
        self,
        stage: str,
        limit: int,
        lease_seconds: int = 300,
        worker_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """原子认领指定阶段的待处理记录（租约认领机制）。"""
        if limit <= 0:
            return []
        now_ts = time.time()
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        cutoff_ts = now_ts - DEAL_MAX_CLEAN_AGE_DAYS * 86400.0
        cutoff_dt = now_utc - datetime.timedelta(days=DEAL_MAX_CLEAN_AGE_DAYS)

        site_age_map = dict(self.site_max_age_map)
        # 消费端直接基于数据库自包含字段 clean_cutoff_ts 判定，无该字段时兼容老数据渠道映射
        nor_conditions: List[Dict[str, Any]] = [
            {"clean_cutoff_ts": {"$gt": 0, "$lt": now_ts}},
            {"next_retry_after_ts": {"$gt": round(now_ts, 1)}},
        ]
        # 兼容老数据兜底：仅当未落盘 clean_cutoff_ts 时，按站点映射计算
        for s_key, age_days in site_age_map.items():
            s_ts = now_ts - age_days * 86400.0
            s_dt = now_utc - datetime.timedelta(days=age_days)
            nor_conditions.append({
                "clean_cutoff_ts": {"$exists": False},
                "source_site": s_key,
                "$or": [
                    {"publish_ts": {"$gt": 0, "$lt": s_ts}},
                    {"first_seen_at": {"$lt": s_dt}},
                ],
            })
        configured_sites = list(site_age_map.keys())
        nor_conditions.append({
            "clean_cutoff_ts": {"$exists": False},
            "source_site": {"$nin": configured_sites},
            "$or": [
                {"publish_ts": {"$gt": 0, "$lt": cutoff_ts}},
                {"first_seen_at": {"$lt": cutoff_dt}},
            ],
        })

        base_query: Dict[str, Any] = {
            "clean_status": "pending",
            "clean_stage": stage,
            "$nor": nor_conditions,
            "$or": [
                {"stage_lease_until_ts": {"$exists": False}},
                {"stage_lease_until_ts": {"$lt": round(now_ts, 1)}},
            ],
        }

        cand_ids = self._select_fair_candidate_ids(base_query, limit)
        if not cand_ids:
            return []

        w_tag = str(worker_id or "w")
        claim_token = f"{w_tag}_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
        lease_until = round(now_ts + max(10, lease_seconds), 1)

        claim_filter = {
            "_id": {"$in": cand_ids},
            "clean_status": "pending",
            "clean_stage": stage,
            "$or": [
                {"stage_lease_until_ts": {"$exists": False}},
                {"stage_lease_until_ts": {"$lt": round(now_ts, 1)}},
            ],
        }

        try:
            upd_res = self.col.update_many(
                claim_filter,
                {
                    "$set": {
                        "stage_lease_owner": claim_token,
                        "stage_lease_until_ts": lease_until,
                    }
                },
            )
            claimed_cnt = int(getattr(upd_res, "modified_count", 0) or 0)
            if claimed_cnt <= 0:
                return []
            claimed_docs = list(
                self.col.find({"stage_lease_owner": claim_token}, {"raw_payload": 0})
            )
            return claimed_docs
        except Exception as e:
            logger.warning("认领阶段 [%s] 任务发生异常: %s", stage, e)
            return []

    def reclaim_expired_leases(self, now_ts: Optional[float] = None) -> int:
        """显式清扫已过期的租约。"""
        if now_ts is None:
            now_ts = time.time()
        try:
            res = self.col.update_many(
                {
                    "clean_status": "pending",
                    "stage_lease_until_ts": {"$exists": True, "$lt": round(now_ts, 1)},
                },
                {"$unset": {"stage_lease_owner": "", "stage_lease_until_ts": ""}},
            )
            return int(getattr(res, "modified_count", 0) or 0)
        except Exception as e:
            logger.warning("清扫过期租约异常: %s", e)
            return 0

    def sweep_expired_and_max_retry(
        self,
        now_utc: Optional[datetime.datetime] = None,
        site_max_age_map: Optional[Dict[str, float]] = None,
    ) -> Dict[str, int]:
        """统一保鲜到期与最大重试上限清扫（SSOT）：确保状态切换至 done 并清除残留租约。"""
        ref_now = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        now_ts = time.time()
        age_map = dict(site_max_age_map) if site_max_age_map is not None else dict(self.site_max_age_map)
        pending_filter: Dict[str, Any] = {
            "$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]
        }
        lease_unset = {"stage_lease_owner": "", "stage_lease_until_ts": ""}
        expired_cnt = 0
        max_retry_cnt = 0

        try:
            # 1. 字段自包含保鲜截止期清扫 (`clean_cutoff_ts`)
            r_cutoff = self.col.update_many(
                {
                    "$and": [
                        pending_filter,
                        {"clean_cutoff_ts": {"$gt": 0, "$lt": now_ts}},
                    ]
                },
                {
                    "$set": {
                        "clean_status": "cleaned_failed",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                        "clean_error_msg": "已超过本爆料专属保鲜截止期，自动清扫",
                        "cleaned_at": ref_now,
                    },
                    "$unset": lease_unset,
                },
            )
            expired_cnt += int(getattr(r_cutoff, "modified_count", 0) or 0)

            # 2. 未落盘 clean_cutoff_ts 的历史记录：按站点自定义保鲜期清扫
            for s_key, age_days in age_map.items():
                s_ts = now_ts - float(age_days) * 86400.0
                s_dt = ref_now - datetime.timedelta(days=float(age_days))
                r_site = self.col.update_many(
                    {
                        "$and": [
                            pending_filter,
                            {"clean_cutoff_ts": {"$exists": False}},
                            {"source_site": s_key},
                            {
                                "$or": [
                                    {"publish_ts": {"$gt": 0, "$lt": s_ts}},
                                    {"first_seen_at": {"$lt": s_dt}},
                                ]
                            },
                        ]
                    },
                    {
                        "$set": {
                            "clean_status": "cleaned_failed",
                            "clean_stage": CLEAN_STAGE_DONE,
                            "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                            "clean_error_msg": f"超过渠道 {s_key} 保鲜期 ({age_days:.0f}天)，自动清扫",
                            "cleaned_at": ref_now,
                        },
                        "$unset": lease_unset,
                    },
                )
                expired_cnt += int(getattr(r_site, "modified_count", 0) or 0)

            # 3. 其余站点历史记录：按默认保鲜期清扫
            configured_sites = list(age_map.keys())
            fallback_ts = now_ts - DEAL_MAX_CLEAN_AGE_DAYS * 86400.0
            fallback_dt = ref_now - datetime.timedelta(days=DEAL_MAX_CLEAN_AGE_DAYS)
            r_fb = self.col.update_many(
                {
                    "$and": [
                        pending_filter,
                        {"clean_cutoff_ts": {"$exists": False}},
                        {"source_site": {"$nin": configured_sites}},
                        {
                            "$or": [
                                {"publish_ts": {"$gt": 0, "$lt": fallback_ts}},
                                {"first_seen_at": {"$lt": fallback_dt}},
                            ]
                        },
                    ]
                },
                {
                    "$set": {
                        "clean_status": "cleaned_failed",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": ERR_EXPIRED_OVER_7_DAYS,
                        "clean_error_msg": f"超过默认保鲜期 ({DEAL_MAX_CLEAN_AGE_DAYS}天)，自动清扫",
                        "cleaned_at": ref_now,
                    },
                    "$unset": lease_unset,
                },
            )
            expired_cnt += int(getattr(r_fb, "modified_count", 0) or 0)

            # 4. 重试达到上限清扫
            r_retry = self.col.update_many(
                {
                    "$and": [
                        pending_filter,
                        {"clean_retry_count": {"$gte": MAX_CLEAN_RETRIES}},
                    ]
                },
                {
                    "$set": {
                        "clean_status": "cleaned_failed",
                        "clean_stage": CLEAN_STAGE_DONE,
                        "clean_error_code": ERR_AI_MAX_RETRIES_EXCEEDED,
                        "clean_error_msg": f"已达最大重试上限({MAX_CLEAN_RETRIES}次)，自动放弃避免占用清洗队列",
                        "cleaned_at": ref_now,
                    },
                    "$unset": lease_unset,
                },
            )
            max_retry_cnt = int(getattr(r_retry, "modified_count", 0) or 0)
        except Exception as e:
            logger.warning("保鲜过期与重试上限清扫异常: %s", e)

        return {"expired_swept": expired_cnt, "max_retry_swept": max_retry_cnt}

    def generate_pool_summary(self) -> Dict[str, Any]:
        """单次 $facet 聚合初步表指标与正式表计数，保持对外指标字典不变。"""
        col = self.col
        pcol = self.formal_col
        pending_stages = (
            CLEAN_STAGE_NEED_AI1,
            CLEAN_STAGE_NEED_CONVERT,
            CLEAN_STAGE_NEED_AI2,
        )
        facet_rows = list(
            col.aggregate([
                {
                    "$facet": {
                        "totals": [
                            {
                                "$group": {
                                    "_id": None,
                                    "total_docs": {"$sum": 1},
                                    "full_detail_docs": {
                                        "$sum": {"$cond": [{"$eq": ["$has_full_detail", True]}, 1, 0]}
                                    },
                                    "unique_anchor_docs": {
                                        "$sum": {
                                            "$cond": [
                                                {"$eq": ["$quality_signals.has_unique_product_anchor", True]},
                                                1,
                                                0,
                                            ]
                                        }
                                    },
                                    "cleaned_success_docs": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_success"]}, 1, 0]}
                                    },
                                    "cleaned_low_score_docs": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_low_score"]}, 1, 0]}
                                    },
                                    "merged_neighbor_docs": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "merged_into_parent"]}, 1, 0]}
                                    },
                                    "context_only_docs": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "context_only"]}, 1, 0]}
                                    },
                                    "cleaned_failed_docs": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_failed"]}, 1, 0]}
                                    },
                                    "pending_clean_docs": {
                                        "$sum": {
                                            "$cond": [
                                                {
                                                    "$or": [
                                                        {"$eq": ["$clean_status", "pending"]},
                                                        {"$eq": [{"$type": "$clean_status"}, "missing"]},
                                                    ]
                                                },
                                                1,
                                                0,
                                            ]
                                        }
                                    },
                                }
                            }
                        ],
                        "by_site": [
                            {
                                "$group": {
                                    "_id": "$source_site",
                                    "count": {"$sum": 1},
                                    "unique_anchor_count": {
                                        "$sum": {
                                            "$cond": [
                                                {"$eq": ["$quality_signals.has_unique_product_anchor", True]},
                                                1,
                                                0,
                                            ]
                                        }
                                    },
                                    "cleaned_success_count": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_success"]}, 1, 0]}
                                    },
                                    "cleaned_failed_count": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_failed"]}, 1, 0]}
                                    },
                                    "pending_count": {
                                        "$sum": {"$cond": [{"$eq": ["$clean_status", "pending"]}, 1, 0]}
                                    },
                                }
                            },
                            {"$sort": {"cleaned_success_count": -1, "unique_anchor_count": -1, "count": -1}},
                        ],
                        "by_clean_error": [
                            {"$match": {"clean_status": {"$in": ["cleaned_failed", "cleaned_low_score"]}}},
                            {"$group": {"_id": "$clean_error_code", "count": {"$sum": 1}}},
                            {"$sort": {"count": -1}},
                        ],
                        "by_platform": [
                            {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
                            {"$sort": {"count": -1}},
                        ],
                        "pending_by_stage": [
                            {
                                "$match": {
                                    "clean_status": "pending",
                                    "clean_stage": {"$in": list(pending_stages)},
                                }
                            },
                            {"$group": {"_id": "$clean_stage", "count": {"$sum": 1}}},
                        ],
                    }
                }
            ])
        )
        facet = facet_rows[0] if facet_rows else {}
        totals = (facet.get("totals") or [{}])[0]

        total_docs = int(totals.get("total_docs") or 0)
        full_detail_docs = int(totals.get("full_detail_docs") or 0)
        unique_anchor_docs = int(totals.get("unique_anchor_docs") or 0)
        cleaned_success_docs = int(totals.get("cleaned_success_docs") or 0)
        cleaned_low_score_docs = int(totals.get("cleaned_low_score_docs") or 0)
        merged_neighbor_docs = int(totals.get("merged_neighbor_docs") or 0)
        context_only_docs = int(totals.get("context_only_docs") or 0)
        cleaned_failed_docs = int(totals.get("cleaned_failed_docs") or 0)
        pending_clean_docs = int(totals.get("pending_clean_docs") or 0)

        by_site = list(facet.get("by_site") or [])
        by_clean_error = list(facet.get("by_clean_error") or [])
        by_platform = list(facet.get("by_platform") or [])
        stage_counts = {str(r["_id"]): int(r["count"]) for r in (facet.get("pending_by_stage") or [])}
        pending_by_stage = {stage: stage_counts.get(stage, 0) for stage in pending_stages}

        formal_total_products = pcol.count_documents({})
        formal_deal_products = pcol.count_documents({"deal_info.is_deal_tip": True})

        return {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pending_by_stage": pending_by_stage,
            "raw_collection": self.collection_name,
            "formal_collection": self.formal_collection_name,
            "total_records": total_docs,
            "full_detail_records": full_detail_docs,
            "unique_product_anchor_records": unique_anchor_docs,
            "unique_product_anchor_ratio": round(unique_anchor_docs * 100.0 / max(1, total_docs), 1),
            "cleaned_success_records": cleaned_success_docs,
            "cleaned_low_score_records": cleaned_low_score_docs,
            "merged_into_parent_records": merged_neighbor_docs,
            "context_only_records": context_only_docs,
            "cleaned_failed_records": cleaned_failed_docs,
            "pending_clean_records": pending_clean_docs,
            "formal_products_total": formal_total_products,
            "formal_deal_products_count": formal_deal_products,
            "by_clean_error_code": {str(r["_id"]): r["count"] for r in by_clean_error},
            "by_platform": {str(r["_id"]): r["count"] for r in by_platform},
            "by_source_site": by_site,
        }
