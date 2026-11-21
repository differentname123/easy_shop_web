# -*- coding: utf-8 -*-
"""
Meilisearch 全文搜索引擎客户端基础设施
负责商品数据的毫秒级搜索、高精度拼写容错、分词与分面过滤，
并在服务不可用或被禁用时自动优雅降级，保障业务零中断。
"""
import datetime
import re
import threading
import time
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.http_utils import clean_money
from infrastructure.logging_utils import format_exc_summary, get_app_logger
from infrastructure.settings import get_settings

logger = get_app_logger("search_meili")

try:
    import meilisearch
    _MEILISEARCH_INSTALLED = True
except ImportError:
    meilisearch = None
    _MEILISEARCH_INSTALLED = False

INDEX_NAME = "products"
_CLIENT_LOCK = RLock()
_CLIENT_INSTANCE: Optional[Any] = None
_INDEX_CONFIGURED = False
_LAST_AVAILABILITY_CHECK = 0.0
_IS_AVAILABLE = False
_AVAILABILITY_CACHE_TTL = 10.0  # 健康检测缓存 10 秒，避免高频 I/O 阻塞主线程

DEFAULT_MEILI_SYNC_INTERVAL_SECONDS = 120.0
DEFAULT_MEILI_SYNC_INITIAL_DELAY_SECONDS = 5.0
_ACTIVE_PRODUCTS_QUERY: Dict[str, Any] = {
    "is_off_promotion": {"$ne": True},
    "superseded_by": {"$exists": False},
}
_SYNC_PROJECTION: Dict[str, int] = {
    "_id": 0,
    "platform": 1,
    "product_id": 1,
    "name": 1,
    "updated_at": 1,
    "created_at": 1,
    "format_updated_at": 1,
    "deal_info.updated_at": 1,
    "deal_info.format_updated_at": 1,
}

_INVALID_ID_CHARS_RE = re.compile(r"[^a-zA-Z0-9-_]")


def _to_unix_ts(value: Any) -> Optional[int]:
    """将 UTC naive/aware datetime 或时间戳转为秒级整型时间戳，无效返回 None。"""
    if isinstance(value, datetime.datetime):
        dt = value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value
        return int(dt.timestamp())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value / 1000) if value > 1e12 else int(value)
    return None


def _extract_doc_sync_ts(doc: Dict[str, Any]) -> int:
    """提取用于差量对账的确定性版本戳（全缺失时固定返回 0，绝不使用当前时钟）。"""
    if not isinstance(doc, dict):
        return 0
    deal_info = doc.get("deal_info")
    candidates = [
        _to_unix_ts(doc.get("updated_at")),
        _to_unix_ts(doc.get("format_updated_at")),
        _to_unix_ts(doc.get("created_at")),
    ]
    if isinstance(deal_info, dict):
        candidates.extend([
            _to_unix_ts(deal_info.get("updated_at")),
            _to_unix_ts(deal_info.get("format_updated_at")),
        ])
    valid = [c for c in candidates if c is not None]
    return max(valid) if valid else 0


def reset_meili_state() -> None:
    """重置 Meilisearch 客户端与健康缓存（供测试或配置重置使用）。"""
    global _CLIENT_INSTANCE, _INDEX_CONFIGURED, _LAST_AVAILABILITY_CHECK, _IS_AVAILABLE
    with _CLIENT_LOCK:
        _CLIENT_INSTANCE = None
        _INDEX_CONFIGURED = False
        _LAST_AVAILABILITY_CHECK = 0.0
        _IS_AVAILABLE = False


def make_meili_doc_id(platform: str, product_id: str) -> str:
    """生成合法的 Meilisearch primary key (仅含 a-zA-Z0-9-_)"""
    raw_id = f"{platform}_{product_id}"
    return _INVALID_ID_CHARS_RE.sub("_", raw_id)


def format_meili_doc(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将 MongoDB products 文档安全转换为 Meilisearch 索引文档结构"""
    if not isinstance(doc, dict):
        return None

    product_id = str(doc.get("product_id") or "").strip()
    if not product_id:
        return None

    name = str(doc.get("name") or "").strip()
    if not name:
        return None

    platform = str(doc.get("platform") or "other").strip().lower()
    doc_id = make_meili_doc_id(platform, product_id)

    price = clean_money(doc.get("price"), 0.0) or 0.0
    activity_price = clean_money(doc.get("activity_price"), price)
    if activity_price is None:
        activity_price = price
    saved_price = clean_money(doc.get("saved_price"), 0.0) or 0.0
    if saved_price <= 0 and price > activity_price > 0:
        saved_price = round(price - activity_price, 2)

    sync_ts = _extract_doc_sync_ts(doc)
    ts = _to_unix_ts(doc.get("updated_at"))
    if ts is None:
        ts = sync_ts

    deal_info = doc.get("deal_info") or {}
    is_deal_tip = bool(isinstance(deal_info, dict) and deal_info.get("is_deal_tip"))
    channel_type = doc.get("channel_type")
    if channel_type is not None and not isinstance(channel_type, int):
        try:
            channel_type = int(channel_type)
        except (ValueError, TypeError):
            channel_type = None

    search_keywords_list: List[str] = []
    if isinstance(deal_info, dict):
        raw_t = str(deal_info.get("raw_tip_title") or "").strip()
        if raw_t and raw_t != name:
            search_keywords_list.append(raw_t)
        fmt = deal_info.get("format_info")
        if isinstance(fmt, dict):
            for e in fmt.get("core_entities") or []:
                if isinstance(e, dict) and e.get("name"):
                    search_keywords_list.append(str(e["name"]).strip())
            for dk in fmt.get("decision_keywords") or []:
                if isinstance(dk, dict) and dk.get("attribute_value"):
                    search_keywords_list.append(str(dk["attribute_value"]).strip())

    return {
        "id": doc_id,
        "product_id": product_id,
        "platform": platform,
        "name": name,
        "brand": str(doc.get("brand") or "").strip(),
        "mall_name": str(doc.get("mall_name") or "").strip(),
        "category": str(doc.get("category") or "").strip(),
        "search_keywords": " ".join(dict.fromkeys(search_keywords_list)),
        "price": price,
        "activity_price": activity_price,
        "saved_price": saved_price,
        "is_off_promotion": bool(doc.get("is_off_promotion") is True or doc.get("superseded_by")),
        "is_deal_tip": is_deal_tip,
        "channel_type": channel_type,
        "image_url": str(doc.get("image_url") or "").strip(),
        "updated_at": ts,
        "sync_ts": sync_ts,
    }


def get_meili_client() -> Optional[Any]:
    """获取单例 Meilisearch Client（支持线程安全懒加载与禁用判定）"""
    global _CLIENT_INSTANCE
    if not _MEILISEARCH_INSTALLED:
        return None

    settings = get_settings()
    if not getattr(settings, "meilisearch_enabled", True):
        return None

    if _CLIENT_INSTANCE is not None:
        return _CLIENT_INSTANCE

    with _CLIENT_LOCK:
        if _CLIENT_INSTANCE is not None:
            return _CLIENT_INSTANCE
        url = getattr(settings, "meilisearch_url", "http://localhost:7700")
        key = getattr(settings, "meilisearch_key", "")
        try:
            client = meilisearch.Client(url, key)
            _CLIENT_INSTANCE = client
            return _CLIENT_INSTANCE
        except Exception as e:
            logger.warning("❌ 【Meilisearch初始化异常】创建客户端失败: %s", format_exc_summary(e))
            return None


def _mark_unavailable() -> None:
    global _IS_AVAILABLE, _LAST_AVAILABILITY_CHECK, _INDEX_CONFIGURED
    with _CLIENT_LOCK:
        _IS_AVAILABLE = False
        _INDEX_CONFIGURED = False
        _LAST_AVAILABILITY_CHECK = time.monotonic()


def is_meili_available(force_check: bool = False) -> bool:
    """带单调时钟 TTL 缓存的健康状态检测，避免服务宕机时阻塞主干流程"""
    global _LAST_AVAILABILITY_CHECK, _IS_AVAILABLE
    if not _MEILISEARCH_INSTALLED:
        return False

    client = get_meili_client()
    if client is None:
        return False

    now = time.monotonic()
    if not force_check and _LAST_AVAILABILITY_CHECK > 0.0 and (now - _LAST_AVAILABILITY_CHECK) < _AVAILABILITY_CACHE_TTL:
        return _IS_AVAILABLE

    with _CLIENT_LOCK:
        now = time.monotonic()
        if not force_check and _LAST_AVAILABILITY_CHECK > 0.0 and (now - _LAST_AVAILABILITY_CHECK) < _AVAILABILITY_CACHE_TTL:
            return _IS_AVAILABLE
        try:
            health = client.health()
            _IS_AVAILABLE = bool(health and health.get("status") == "available")
            _LAST_AVAILABILITY_CHECK = time.monotonic()
            return _IS_AVAILABLE
        except Exception as e:
            _IS_AVAILABLE = False
            _LAST_AVAILABILITY_CHECK = time.monotonic()
            logger.debug("⚠️ 【Meilisearch健康检测失败】服务当前不可用，将自动降级: %s", format_exc_summary(e))
            return False


# 电商高频搜索同义词与简称对照表
DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "可乐": ["可口可乐", "百事可乐"],
    "可口可乐": ["可乐"],
    "百事可乐": ["可乐"],
    "雪碧": ["柠檬汽水", "雪碧汽水"],
    "芬达": ["橙味汽水", "芬达汽水"],
    "纯牛奶": ["牛奶", "鲜奶"],
    "牛奶": ["纯牛奶", "鲜奶"],
    "酸奶": ["发酵乳", "风味发酵乳"],
    "咖啡": ["美式咖啡", "拿铁咖啡", "速溶咖啡"],
    "抽纸": ["纸巾", "面巾纸", "抽纸巾"],
    "纸巾": ["抽纸", "面巾纸", "餐巾纸"],
    "卷纸": ["卷筒纸", "卫生纸"],
    "湿巾": ["湿纸巾", "婴儿湿巾"],
    "洗衣液": ["洗衣凝珠", "洗衣粉", "洗衣皂"],
    "洗洁精": ["洗洁剂", "餐具洗涤剂"],
    "洗发水": ["洗发露", "洗发膏"],
    "沐浴露": ["沐浴乳", "沐浴液"],
    "牙膏": ["洁齿膏"],
    "方便面": ["泡面", "速食面"],
    "挂面": ["面条", "手排面"],
    "食用油": ["花生油", "大豆油", "菜籽油", "玉米油"],
    "充电宝": ["移动电源"],
    "数据线": ["充电线", "快充线"],
    "耳机": ["蓝牙耳机", "无线耳机"],
    "手机": ["智能手机"],
}


def ensure_index_configured(force: bool = False) -> bool:
    """确保 products 索引及分词过滤属性已就绪（单次原子 update_settings）。"""
    global _INDEX_CONFIGURED
    if _INDEX_CONFIGURED and not force:
        return True

    client = get_meili_client()
    if not client or not is_meili_available():
        return False

    with _CLIENT_LOCK:
        if _INDEX_CONFIGURED and not force:
            return True
        try:
            index = client.index(INDEX_NAME)
            try:
                index.get_raw_info()
            except Exception:
                task = client.create_index(INDEX_NAME, {"primaryKey": "id"})
                uid = task.get("taskUid") if isinstance(task, dict) else getattr(task, "task_uid", None)
                if isinstance(uid, int) and callable(getattr(client, "wait_for_task", None)):
                    try:
                        client.wait_for_task(uid, timeout_in_ms=5000)
                    except Exception:
                        pass
                index = client.index(INDEX_NAME)

            index.update_settings({
                "searchableAttributes": ["name", "brand", "mall_name", "category", "search_keywords"],
                "filterableAttributes": ["platform", "category", "is_off_promotion", "is_deal_tip", "channel_type"],
                "sortableAttributes": ["updated_at", "activity_price", "price", "saved_price"],
                "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
                "synonyms": DEFAULT_SYNONYMS,
            })
            _INDEX_CONFIGURED = True
            logger.info("✅ 【Meilisearch索引就绪】products 索引及同义词配置已完成")
            return True
        except Exception as e:
            logger.warning("❌ 【Meilisearch配置索引异常】: %s", format_exc_summary(e))
            return False


def _escape_filter_val(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"')


def search_products(
    keyword: str,
    platform: str = "all",
    category: Optional[str] = None,
    sort_by: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    only_active_boom: bool = False,
) -> Optional[List[Dict[str, Any]]]:
    """
    通过 Meilisearch 执行高精度分词与模糊搜索
    :return: 命中商品列表（保持高相关性顺序）；如果 Meilisearch 不可用则返回 None（触发上游回退 Mongo）
    """
    if not keyword or not keyword.strip():
        return None

    if not is_meili_available():
        return None

    client = get_meili_client()
    if not client:
        return None

    try:
        index = client.index(INDEX_NAME)

        filter_exprs = ["NOT is_off_promotion = true"]
        if platform and platform.lower() != "all":
            filter_exprs.append(f'platform = "{_escape_filter_val(platform.lower())}"')
        if category and category.strip() and category.strip() != "全部":
            filter_exprs.append(f'category = "{_escape_filter_val(category.strip())}"')
        if only_active_boom:
            filter_exprs.append("(channel_type IS NOT NULL OR is_deal_tip = true)")

        sort_mapping = {
            "latest": ["updated_at:desc"],
            "price_asc": ["activity_price:asc"],
            "price_desc": ["activity_price:desc"],
            "save_desc": ["saved_price:desc"],
        }
        sort_rule = sort_mapping.get(sort_by)

        search_params: Dict[str, Any] = {
            "limit": max(1, min(200, limit)),
            "offset": max(0, offset),
            "filter": " AND ".join(filter_exprs) if filter_exprs else None,
            "matchingStrategy": "all",
            "rankingScoreThreshold": 0.6,
        }
        if sort_rule:
            search_params["sort"] = sort_rule

        search_params = {k: v for k, v in search_params.items() if v is not None}

        result = index.search(keyword.strip(), search_params)
        hits = result.get("hits") or []

        # 若高相关性阈值未召回结果，则平滑放宽阈值至 0.3 并放宽至 matchingStrategy="last"
        if not hits and offset == 0:
            search_params["matchingStrategy"] = "last"
            search_params["rankingScoreThreshold"] = 0.3
            result_fallback = index.search(keyword.strip(), search_params)
            hits = result_fallback.get("hits") or []

        return hits
    except Exception as e:
        _mark_unavailable()
        logger.warning(
            "❌ 【Meilisearch检索失败】关键词[%s] 平台[%s] 异常<%s>，自动回退到数据库查询",
            keyword, platform, format_exc_summary(e),
        )
        return None


def _resolve_products_collection(mongo_db):
    if mongo_db is None:
        return None
    if hasattr(mongo_db, "col") and not hasattr(mongo_db, "find"):
        return mongo_db.col
    if hasattr(mongo_db, "find") and not hasattr(mongo_db, "get_collection"):
        return mongo_db
    if hasattr(mongo_db, "get_collection"):
        return mongo_db.get_collection("products")
    if hasattr(mongo_db, "db") and getattr(mongo_db, "db", None) is not None:
        return _resolve_products_collection(mongo_db.db)
    return mongo_db["products"]


def _fetch_meili_sync_map(
    index,
    *,
    page_size: int = 2000,
    max_pages: int = 1000,
) -> Tuple[Dict[str, Optional[int]], bool]:
    """分页拉取 Meilisearch 全部文档的 (id -> sync_ts)，并返回是否完整扫描完。"""
    step = max(1, int(page_size))
    meili_map: Dict[str, Optional[int]] = {}
    offset = 0
    complete = False
    for _ in range(max_pages):
        res = index.get_documents({"fields": ["id", "sync_ts"], "limit": step, "offset": offset})
        results = res.get("results") if isinstance(res, dict) else getattr(res, "results", None)
        if not results:
            complete = True
            break
        for item in results:
            if isinstance(item, dict):
                doc_id = item.get("id")
                raw_ts = item.get("sync_ts")
            else:
                doc_id = getattr(item, "id", None)
                raw_ts = getattr(item, "sync_ts", None)
            clean_id = str(doc_id or "").strip()
            if not clean_id:
                continue
            sync_ts = (
                int(raw_ts)
                if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool)
                else None
            )
            meili_map[clean_id] = sync_ts
        offset += len(results)
        total = res.get("total") if isinstance(res, dict) else getattr(res, "total", None)
        if len(results) < step or (isinstance(total, int) and offset >= total):
            complete = True
            break
    return meili_map, complete


def sync_products_diff(
    mongo_db,
    *,
    page_size: int = 2000,
    batch_size: int = 1000,
    delete_batch_size: int = 1000,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, int]:
    """以 MongoDB `products` 为唯一真源执行 Fail-Closed 双向差量同步（新增/更新/删除与建索引自愈）。"""
    empty = {"upserted": 0, "deleted": 0, "skipped": 0}
    if mongo_db is None or not is_meili_available():
        return empty
    client = get_meili_client()
    if not client:
        return empty
    col = _resolve_products_collection(mongo_db)
    if col is None:
        return empty
    if not ensure_index_configured():
        return empty

    started = time.monotonic()
    try:
        cursor = col.find(_ACTIVE_PRODUCTS_QUERY, _SYNC_PROJECTION)
        mongo_meta: Dict[str, Tuple[Any, Any, int]] = {}
        for doc in cursor:
            if not isinstance(doc, dict):
                continue
            raw_pid = doc.get("product_id")
            clean_pid = str(raw_pid or "").strip()
            name = str(doc.get("name") or "").strip()
            if not clean_pid or not name:
                continue
            raw_platform = doc.get("platform")
            norm_platform = str(raw_platform or "other").strip().lower()
            doc_id = make_meili_doc_id(norm_platform, clean_pid)
            mongo_meta[doc_id] = (raw_platform, raw_pid, _extract_doc_sync_ts(doc))
    except Exception as exc:
        logger.warning("⚠️ 【Meilisearch对账跳过】读取 MongoDB 商品摘要失败: %s", format_exc_summary(exc))
        return empty

    index = client.index(INDEX_NAME)
    try:
        meili_map, complete = _fetch_meili_sync_map(index, page_size=page_size)
    except Exception as exc:
        if "index_not_found" in str(exc).lower():
            if not ensure_index_configured(force=True):
                return empty
            index = client.index(INDEX_NAME)
            meili_map, complete = {}, True
        else:
            _mark_unavailable()
            logger.warning("❌ 【Meilisearch对账拉取失败】: %s", format_exc_summary(exc))
            return empty

    if not complete:
        logger.warning("⚠️ 【Meilisearch对账中止】索引扫描未完成（超出分页上限），为防误删已跳过本轮同步")
        return empty

    stale_ids = [doc_id for doc_id in meili_map if doc_id not in mongo_meta]
    upsert_ids = [
        doc_id for doc_id, (_, _, m_ts) in mongo_meta.items() if meili_map.get(doc_id) != m_ts
    ]
    skipped = len(mongo_meta) - len(upsert_ids)

    del_step = max(1, int(delete_batch_size))
    add_step = max(1, int(batch_size))
    deleted = 0
    upserted = 0

    try:
        for i in range(0, len(stale_ids), del_step):
            if stop_event is not None and stop_event.is_set():
                return {"upserted": upserted, "deleted": deleted, "skipped": skipped}
            chunk = stale_ids[i : i + del_step]
            index.delete_documents(chunk)
            deleted += len(chunk)

        by_platform: Dict[Any, List[Any]] = {}
        for doc_id in upsert_ids:
            raw_platform, raw_pid, _ = mongo_meta[doc_id]
            by_platform.setdefault(raw_platform, []).append(raw_pid)

        batch: List[Dict[str, Any]] = []

        def _flush_batch() -> None:
            nonlocal upserted, batch
            if not batch:
                return
            index.add_documents(batch)
            upserted += len(batch)
            batch = []

        stopped = False
        for raw_platform, pids in by_platform.items():
            for i in range(0, len(pids), 500):
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                pid_chunk = pids[i : i + 500]
                docs_cursor = col.find(
                    {"platform": raw_platform, "product_id": {"$in": pid_chunk}, **_ACTIVE_PRODUCTS_QUERY}
                )
                for doc in docs_cursor:
                    if stop_event is not None and stop_event.is_set():
                        stopped = True
                        break
                    item = format_meili_doc(doc)
                    if item:
                        batch.append(item)
                        if len(batch) >= add_step:
                            _flush_batch()
                if stopped:
                    break
            if stopped:
                break

        if batch and not (stop_event is not None and stop_event.is_set()):
            _flush_batch()
    except Exception as exc:
        _mark_unavailable()
        logger.warning("❌ 【Meilisearch差量同步异常】: %s", format_exc_summary(exc))
        return {"upserted": upserted, "deleted": deleted, "skipped": skipped}

    elapsed = time.monotonic() - started
    if upserted > 0 or deleted > 0:
        logger.info(
            "🔄 【Meilisearch定时同步】新增/更新 %d 条，清理幽灵/下架 %d 条，已一致跳过 %d 条（耗时 %.2fs）",
            upserted, deleted, skipped, elapsed,
        )
    else:
        logger.debug("【Meilisearch定时同步】索引与数据库完全一致（跳过 %d 条，耗时 %.2fs）", skipped, elapsed)

    return {"upserted": upserted, "deleted": deleted, "skipped": skipped}


def sync_all_products(mongo_db, batch_size: int = 2000, progress_cb=None) -> int:
    """全量将 MongoDB products 集合同步到 Meilisearch"""
    client = get_meili_client()
    if not client or not is_meili_available():
        raise RuntimeError("Meilisearch 服务未就绪，无法执行全量同步")

    ensure_index_configured()
    index = client.index(INDEX_NAME)
    col = _resolve_products_collection(mongo_db)

    query = _ACTIVE_PRODUCTS_QUERY
    total = col.count_documents(query)
    logger.info("🚀 【Meilisearch全量同步开始】MongoDB待同步商品总数: %d 条", total)

    cursor = col.find(query).batch_size(batch_size)
    batch = []
    valid_ids = set()
    synced = 0

    for doc in cursor:
        item = format_meili_doc(doc)
        if item:
            valid_ids.add(item["id"])
            batch.append(item)

        if len(batch) >= batch_size:
            index.add_documents(batch)
            synced += len(batch)
            if progress_cb:
                progress_cb(synced, total)
            logger.info("📦 【Meilisearch同步中】已推送 %d / %d 条商品数据", synced, total)
            batch = []

    if batch:
        index.add_documents(batch)
        synced += len(batch)
        if progress_cb:
            progress_cb(synced, total)
        logger.info("📦 【Meilisearch同步中】已推送 %d / %d 条商品数据", synced, total)

    meili_map, complete = _fetch_meili_sync_map(index, page_size=max(2000, batch_size))
    if complete:
        stale_ids = [did for did in meili_map if did not in valid_ids]
        for idx in range(0, len(stale_ids), 1000):
            index.delete_documents(stale_ids[idx : idx + 1000])
        if stale_ids:
            logger.info("🧹 【Meilisearch全量同步清理】已移除 %d 条幽灵/已下架索引文档", len(stale_ids))

    logger.info("🎉 【Meilisearch全量同步完成】累计同步成功 %d 条商品", synced)
    return synced


class MeiliSyncWorker:
    """唯一后台定时双向同步守护线程：以 MongoDB `products` 为真源周期性对齐 Meilisearch。"""

    def __init__(
        self,
        target_provider,
        *,
        interval_seconds: float = DEFAULT_MEILI_SYNC_INTERVAL_SECONDS,
        initial_delay_seconds: float = DEFAULT_MEILI_SYNC_INITIAL_DELAY_SECONDS,
    ) -> None:
        self._target_provider = target_provider
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="meili-sync-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "【Meilisearch定时同步】后台双向同步守护线程已启动 (首轮延迟 %.0fs, 同步周期 %.0fs)",
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
                target = self._target_provider() if callable(self._target_provider) else self._target_provider
                if target is not None:
                    sync_products_diff(target, stop_event=self._stop_event)
            except Exception as exc:
                logger.warning("【Meilisearch定时同步】后台同步巡检异常 | 异常<%s>", format_exc_summary(exc))
            if self._stop_event.wait(timeout=self.interval_seconds):
                break
