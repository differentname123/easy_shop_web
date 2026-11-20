# -*- coding: utf-8 -*-
"""
好价线报初步缓冲池仓储 (`deal_raw_tips`) 与正式商品表 (`products`) 关联管理。
统一收敛存储、TTL 索引、增量去重、状态机租约、指标聚合与审计落盘。
"""
import copy
import datetime
import html as html_lib
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
from services.deal_channel_config import (
    get_channel_max_age_days,
    get_site_max_age_days_map,
)
from models.deal_constants import (
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
    DEAL_FORMAL_EXPIRY_HOURS,
    DEAL_MAX_CLEAN_AGE_DAYS,
    DEAL_RAW_TTL_DAYS,
    DEFAULT_FORMAL_PRODUCTS_COLLECTION,
    DEFAULT_RAW_COLLECTION_NAME,
    ENGINEERING_PHILOSOPHY_MANIFEST,
    ERROR_LOG_JSONL_PATH,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_EMPTY_PRODUCT_IDENTITY,
    ERR_EXPIRED_OVER_7_DAYS,
    EXCLUDED_SITES_AND_CHANNELS_MANIFEST,
    LEGACY_SHADOW_TEST_COLLECTION,
    LOCAL_FALLBACK_JSONL,
    MAX_JSONL_FILE_BYTES,
    _UNSUPPORTED_EXTERNAL_DOMAINS,
)

logger = logging.getLogger("deal_pool_scheduler")

_JSONL_WRITE_LOCK = threading.Lock()


def _append_bounded_jsonl_lines(
    path: str,
    lines: List[str],
    *,
    max_bytes: int = MAX_JSONL_FILE_BYTES,
    backup_count: int = 2,
) -> None:
    """线程安全且受磁盘容量上限约束地追加 JSONL 记录，超限时自动按 .1 / .2 滚动归档。"""
    if not lines:
        return
    payload = "".join(line if line.endswith("\n") else f"{line}\n" for line in lines)
    encoded_len = len(payload.encode("utf-8", errors="ignore"))
    with _JSONL_WRITE_LOCK:
        try:
            if max_bytes > 0 and os.path.exists(path):
                cur_size = os.path.getsize(path)
                if cur_size + encoded_len > max_bytes:
                    for idx in range(backup_count, 0, -1):
                        src = path if idx == 1 else f"{path}.{idx - 1}"
                        dst = f"{path}.{idx}"
                        if os.path.exists(src):
                            try:
                                if os.path.exists(dst):
                                    os.remove(dst)
                                os.replace(src, dst)
                            except OSError:
                                pass
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(payload)


def append_error_records_jsonl(error_records: List[Dict[str, Any]]) -> None:
    """将本轮捕获的所有列表级与详情级错误路径持久化追加写入 logs/deal_pool_errors.jsonl。"""
    if not error_records:
        return
    try:
        _append_bounded_jsonl_lines(
            ERROR_LOG_JSONL_PATH,
            [json.dumps(item, ensure_ascii=False) for item in error_records],
        )
    except Exception as e:
        logger.error("写入错误路径日志 [%s] 失败: %s", ERROR_LOG_JSONL_PATH, e)


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
    cand_id = str(record.get("candidate_goods_id") or "").strip()

    if any(isinstance(lk, dict) and (lk.get("url") or lk.get("item_id")) for lk in links):
        signal_types.append("extracted_links")
    if any(str(tk or "").strip() for tk in tokens):
        signal_types.append("extracted_tokens")
    if cand_id:
        signal_types.append("candidate_goods_id")

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
    if str(record.get("candidate_goods_id") or "").strip():
        return False
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


_CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


def parse_publish_time_epoch(pub_time_str: str) -> Optional[float]:
    """解析多样化发布时间文本为 UTC epoch 时间戳（秒），无时区字符串统一按东八区解析。"""
    if not pub_time_str:
        return None
    s = str(pub_time_str).strip()
    if not s:
        return None
    try:
        if s.isdigit() and len(s) in (10, 13):
            val = float(s)
            return val / 1000.0 if len(s) == 13 else val
    except Exception:
        pass

    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CHINA_TZ)
        return dt.timestamp()
    except Exception:
        pass

    now_cn = datetime.datetime.now(_CHINA_TZ)
    if s == "刚刚":
        return now_cn.timestamp()

    m_rel = re.search(r"(\d+)\s*(秒|分钟|小时|天)前", s)
    if m_rel:
        num = int(m_rel.group(1))
        unit = m_rel.group(2)
        if unit == "秒":
            return (now_cn - datetime.timedelta(seconds=num)).timestamp()
        elif unit == "分钟":
            return (now_cn - datetime.timedelta(minutes=num)).timestamp()
        elif unit == "小时":
            return (now_cn - datetime.timedelta(hours=num)).timestamp()
        elif unit == "天":
            return (now_cn - datetime.timedelta(days=num)).timestamp()

    m_day_hm = re.match(r"^(今天|昨天)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m_day_hm:
        day_delta = 0 if m_day_hm.group(1) == "今天" else 1
        base_dt = (now_cn - datetime.timedelta(days=day_delta)).replace(
            hour=int(m_day_hm.group(2)),
            minute=int(m_day_hm.group(3)),
            second=int(m_day_hm.group(4) or 0),
            microsecond=0,
        )
        return base_dt.timestamp()

    patterns = [
        ("%Y-%m-%d %H:%M:%S", None),
        ("%Y-%m-%d %H:%M", None),
        ("%Y/%m/%d %H:%M:%S", None),
        ("%Y/%m/%d %H:%M", None),
        ("%m-%d %H:%M", now_cn.year),
    ]
    for fmt, yr in patterns:
        try:
            parsed = datetime.datetime.strptime(s, fmt)
            if yr:
                parsed = parsed.replace(year=yr)
            return parsed.replace(tzinfo=_CHINA_TZ).timestamp()
        except Exception:
            pass
    return None


class DealRawPoolRepository:
    """好价线报初步缓冲池仓储 (`deal_raw_tips`) + 正式商品表 (`products`) 关联管理。"""

    RECOVERY_VERSION_TAG = "20261116_v3"

    def __init__(
        self,
        collection_name: str = DEFAULT_RAW_COLLECTION_NAME,
        formal_collection_name: str = DEFAULT_FORMAL_PRODUCTS_COLLECTION,
        db_instance=None,
    ):
        self.collection_name = collection_name
        self.formal_collection_name = formal_collection_name
        self._db = db_instance
        self._indexes_ready = False

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
            col.create_index([("first_seen_at", -1)], background=True)
            col.create_index([("last_seen_at", -1)], background=True)
            col.create_index([("cleaned_at", -1)], background=True)
            col.create_index([("clean_status", 1), ("source_site", 1)], background=True)
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
            col.create_index([("candidate_goods_id", 1)], background=True)
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

    def recover_recent_misclassified_tips(self, lookback_hours: float = 48.0) -> Dict[str, int]:
        """按版本号幂等救援近期误杀线报回 pending。"""
        cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
            hours=max(1.0, float(lookback_hours))
        )
        jd_cps_re = r"(?:u\.jd\.com|union-click\.jd\.com)"
        reset_payload = {
            "$set": {
                "clean_status": "pending",
                "clean_stage": CLEAN_STAGE_NEED_AI1,
                "ai_clean_status": "pending",
                "clean_retry_count": 0,
                "clean_error_code": "",
                "clean_error_msg": "",
                "recovery_version": self.RECOVERY_VERSION_TAG,
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
        }
        jd_recovered = 0
        tb_recovered = 0
        try:
            r_jd = self.col.update_many(
                {
                    "clean_status": "cleaned_failed",
                    "clean_error_code": {
                        "$in": [
                            ERR_EMPTY_PRODUCT_IDENTITY,
                            "ERR_JD_NUMERIC_SKU",
                            "ERR_AI2_MAIN_DEAD_ONLY_FILLER",
                        ]
                    },
                    "first_seen_at": {"$gte": cutoff},
                    "recovery_version": {"$ne": self.RECOVERY_VERSION_TAG},
                    "$or": [
                        {"tutorial_text": {"$regex": jd_cps_re, "$options": "i"}},
                        {"source_url": {"$regex": jd_cps_re, "$options": "i"}},
                        {"extracted_links.url": {"$regex": jd_cps_re, "$options": "i"}},
                        {"converted_links.clean_input": {"$regex": jd_cps_re, "$options": "i"}},
                    ],
                },
                reset_payload,
            )
            jd_recovered = int(getattr(r_jd, "modified_count", 0) or 0)
            r_tb = self.col.update_many(
                {
                    "clean_status": "cleaned_failed",
                    "first_seen_at": {"$gte": cutoff},
                    "recovery_version": {"$ne": self.RECOVERY_VERSION_TAG},
                    "clean_error_msg": {"$regex": "京东.*业务错误"},
                },
                reset_payload,
            )
            tb_recovered = int(getattr(r_tb, "modified_count", 0) or 0)
        except Exception as e:
            logger.warning("按版本幂等救援近期误杀线报异常: %s", e)
        return {"jd_recovered": jd_recovered, "tb_recovered": tb_recovered}

    def reset_raw_collection(self) -> int:
        """清空初步表与挂载到正式表的数据。"""
        deleted_raw = 0
        try:
            res = self.col.delete_many({})
            deleted_raw = res.deleted_count
            logger.info("🧹 已清空初步表 [%s]，共删除 %d 条记录。", self.collection_name, deleted_raw)
        except Exception as e:
            logger.error("清空初步表 [%s] 失败: %s", self.collection_name, e)

        try:
            res_formal = self.formal_col.delete_many({
                "$or": [
                    {"source": "deal_pool"},
                    {"last_clean_source": "deal_pool"},
                    {"deal_info.is_deal_tip": True},
                ]
            })
            logger.info("🧹 已清空正式表中的好价单品，共删除 %d 条。", res_formal.deleted_count)
        except Exception:
            pass

        try:
            self.db.get_collection(LEGACY_SHADOW_TEST_COLLECTION).drop()
        except Exception:
            pass
        return deleted_raw

    def get_existing_detailed_ids(self, source_site: str, limit: int = 5000) -> Set[str]:
        """获取某站点近期已经成功拉取过完整详情的 `source_item_id` 集合，避免重复请求详情页。"""
        try:
            cursor = (
                self.col.find(
                    {"source_site": source_site, "has_full_detail": True},
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
        at: Optional[datetime.datetime] = None,
    ) -> int:
        """把"本轮在列表里再次见到"的已存商品标记为仍在架：只刷新 `last_seen_at`。

        刻意不动两处：
        - `seen_count`：它会汇总成 `deal_info.total_seen_count`（代表"被重复推荐次数"），
          每轮全量巡视都 +1 会把这个指标刷废；
        - `publish_time` / `publish_ts`：那是源站自述的爆料时间，参与 7 天过期判定，
          不能被本地拉取时间污染。
        返回匹配到的已存文档数（不在库里的 ID 直接忽略）。
        """
        site = str(source_site or "").strip()
        ids = [str(i).strip() for i in (item_ids or []) if str(i or "").strip()]
        if not site or not ids:
            return 0
        now_dt = at or datetime.datetime.now(datetime.timezone.utc)
        now = now_dt.replace(tzinfo=None)
        update_doc: Dict[str, Any] = {"$set": {"last_seen_at": now}}
        if channel:
            update_doc["$addToSet"] = {"seen_channels": channel}

        # 特惠采采专员严选数据：在架即代表有效好价，巡视再次见到时直接刷新用于后续判断的发布时间与保鲜时间
        if site == "tehuicaicai":
            now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            now_ts = now_dt.timestamp()
            update_doc["$set"]["publish_time"] = now_iso
            update_doc["$set"]["publish_ts"] = now_ts

        ops = [
            UpdateOne({"source_site": site, "source_item_id": item_id}, update_doc)
            for item_id in ids
        ]
        matched_cnt = 0
        try:
            res = self.col.bulk_write(ops, ordered=False)
            matched_cnt = int(res.matched_count)
        except Exception as e:
            logger.warning("刷新站点 [%s] 本轮确认时间失败: %s", site, e)

        if site == "tehuicaicai" and ids:
            try:
                now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                id_variants = list(set(ids + [int(i) for i in ids if i.isdigit()]))
                self.formal_col.update_many(
                    {
                        "deal_info.source_site": "tehuicaicai",
                        "$or": [
                            {"deal_info.source_item_id": {"$in": id_variants}},
                            {"deal_info.merged_tip_ids": {"$in": id_variants}},
                        ],
                    },
                    {
                        "$set": {
                            "deal_info.publish_time": now_iso,
                            "deal_info.updated_at": now,
                            "deal_info.is_deal_tip": True,
                        }
                    },
                )
            except Exception as e:
                logger.warning("刷新正式表特惠采采商品确认时间失败: %s", e)

        return matched_cnt

    def save_batch(self, records: List[Dict[str, Any]], task_name: str = "") -> Dict[str, int]:
        """【Stage 1：源站拉取入初步表 `deal_raw_tips`】"""
        if not records:
            return {"inserted": 0, "updated": 0, "skipped_no_signal": 0, "context_only_saved": 0, "total": 0}

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ops = []
        skipped_no_signal = 0
        context_only_saved = 0

        for rec in records:
            if not isinstance(rec, dict):
                continue
            site = str(rec.get("source_site") or "").strip()
            item_id = str(rec.get("source_item_id") or "").strip()
            if not site or not item_id:
                continue

            has_full_detail = bool(rec.get("has_full_detail", True))
            quality_signals = evaluate_quality_signals(rec)
            is_group_stream = bool(
                rec.get("is_group_stream")
                or (site == "zhetaoke" and item_id.startswith("xb_"))
            )
            hot_metrics = rec.get("hot_metrics") if isinstance(rec.get("hot_metrics"), dict) else {}
            group_id = str(hot_metrics.get("group_id") or "").strip()
            channel = str(rec.get("source_channel") or "").strip()
            pub_time_str = str(rec.get("publish_time") or "").strip()
            parsed_pub_ts = parse_publish_time_epoch(pub_time_str)
            pub_ts = parsed_pub_ts if parsed_pub_ts is not None else time.time()
            channel_max_age = get_channel_max_age_days(
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
            if site == "tehuicaicai" and pub_time_str:
                set_fields["publish_time"] = pub_time_str
                set_fields["publish_ts"] = pub_ts
                set_fields["clean_cutoff_ts"] = clean_cutoff_ts
                set_fields["clean_max_age_days"] = channel_max_age

            set_on_insert: Dict[str, Any] = {
                "source_site": site,
                "source_item_id": item_id,
                "first_seen_at": now,
                "clean_status": initial_status,
                "clean_stage": CLEAN_STAGE_NEED_AI1 if initial_status == "pending" else CLEAN_STAGE_DONE,
                "ai_clean_status": initial_status,
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
                "publish_time": pub_time_str,
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
        """按站点倒序各取候选记录，公平轮询抽取最多 `limit` 个待认领 _id。"""
        if limit <= 0:
            return []
        sites = [
            str(s or "").strip()
            for s in self.col.distinct("source_site", base_query)
            if str(s or "").strip()
        ]
        sorted_sites = sorted(set(sites))
        if not sorted_sites:
            cursor = (
                self.col.find(base_query, {"_id": 1})
                .sort([("publish_ts", -1), ("last_seen_at", -1)])
                .limit(limit)
            )
            return [doc["_id"] for doc in cursor]

        per_site_candidates: Dict[str, List[Any]] = {}
        for s_site in sorted_sites:
            q = {"$and": [{"source_site": s_site}, base_query]}
            docs = list(
                self.col.find(q, {"_id": 1, "clean_retry_count": 1})
                .sort([("publish_ts", -1), ("last_seen_at", -1)])
                .limit(limit)
            )
            docs.sort(key=lambda d: int(d.get("clean_retry_count") or 0))
            per_site_candidates[s_site] = [d["_id"] for d in docs]

        chosen_ids: List[Any] = []
        chosen_set = set()
        queues = [per_site_candidates[s] for s in sorted_sites if per_site_candidates.get(s)]
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

        site_age_map = get_site_max_age_days_map()
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

    def generate_pool_summary(self) -> Dict[str, Any]:
        """聚合初步表与正式表的核心健康指标字典。"""
        col = self.col
        pcol = self.formal_col
        total_docs = col.count_documents({})
        full_detail_docs = col.count_documents({"has_full_detail": True})
        unique_anchor_docs = col.count_documents({"quality_signals.has_unique_product_anchor": True})
        cleaned_success_docs = col.count_documents({"clean_status": "cleaned_success"})
        cleaned_low_score_docs = col.count_documents({"clean_status": "cleaned_low_score"})
        merged_neighbor_docs = col.count_documents({"clean_status": "merged_into_parent"})
        context_only_docs = col.count_documents({"clean_status": "context_only"})
        cleaned_failed_docs = col.count_documents({"clean_status": "cleaned_failed"})
        pending_clean_docs = col.count_documents({"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]})

        formal_total_products = pcol.count_documents({})
        formal_deal_products = pcol.count_documents({"deal_info.is_deal_tip": True})

        by_site = list(
            col.aggregate([
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
            ])
        )

        by_clean_error = list(
            col.aggregate([
                {"$match": {"clean_status": {"$in": ["cleaned_failed", "cleaned_low_score"]}}},
                {"$group": {"_id": "$clean_error_code", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ])
        )

        by_platform = list(
            col.aggregate([
                {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ])
        )

        pending_by_stage = {
            CLEAN_STAGE_NEED_AI1: col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI1}),
            CLEAN_STAGE_NEED_CONVERT: col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_CONVERT}),
            CLEAN_STAGE_NEED_AI2: col.count_documents({"clean_status": "pending", "clean_stage": CLEAN_STAGE_NEED_AI2}),
        }

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
            "engineering_philosophy_manifest": ENGINEERING_PHILOSOPHY_MANIFEST,
            "excluded_sites_manifest": EXCLUDED_SITES_AND_CHANNELS_MANIFEST,
            "attempted_dead_ends_manifest": CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
        }
