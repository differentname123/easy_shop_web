# -*- coding: utf-8 -*-
"""
Meilisearch 全文搜索引擎客户端基础设施
负责商品数据的毫秒级搜索、高精度拼写容错、分词与分面过滤，
并在服务不可用或被禁用时自动优雅降级，保障业务零中断。
"""
import re
import time
import datetime
from typing import Dict, Any, List, Optional, Tuple
from threading import RLock

from config import logger
from infrastructure.settings import get_settings
from infrastructure.logging_utils import format_exc_summary

try:
    import meilisearch
    from meilisearch.errors import MeilisearchError, MeilisearchCommunicationError
    _MEILISEARCH_INSTALLED = True
except ImportError:
    meilisearch = None
    MeilisearchError = Exception
    MeilisearchCommunicationError = Exception
    _MEILISEARCH_INSTALLED = False

INDEX_NAME = "products"
_CLIENT_LOCK = RLock()
_CLIENT_INSTANCE: Optional[Any] = None
_INDEX_CONFIGURED = False
_LAST_AVAILABILITY_CHECK = 0.0
_IS_AVAILABLE = False
_AVAILABILITY_CACHE_TTL = 10.0  # 健康检测缓存 10 秒，避免高频 I/O 阻塞主线程

_INVALID_ID_CHARS_RE = re.compile(r"[^a-zA-Z0-9-_]")


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

    def _safe_float(val: Any, default: float = 0.0) -> float:
        if val is None or isinstance(val, bool):
            return default
        try:
            f = float(val)
            return f if f == f and f != float("inf") and f != float("-inf") else default
        except (ValueError, TypeError):
            return default

    price = _safe_float(doc.get("price"))
    activity_price = _safe_float(doc.get("activity_price"), default=price)
    saved_price = _safe_float(doc.get("saved_price"))
    if saved_price <= 0 and price > activity_price > 0:
        saved_price = round(price - activity_price, 2)

    updated_at = doc.get("updated_at")
    if isinstance(updated_at, datetime.datetime):
        ts = int(updated_at.timestamp())
    elif isinstance(updated_at, (int, float)):
        ts = int(updated_at / 1000) if updated_at > 1e12 else int(updated_at)
    else:
        ts = int(time.time())

    deal_info = doc.get("deal_info") or {}
    is_deal_tip = bool(deal_info.get("is_deal_tip"))
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


def is_meili_available(force_check: bool = False) -> bool:
    """带 TTL 缓存的健康状态检测，避免服务宕机时阻塞主干流程"""
    global _LAST_AVAILABILITY_CHECK, _IS_AVAILABLE
    if not _MEILISEARCH_INSTALLED:
        return False

    client = get_meili_client()
    if client is None:
        return False

    now = time.time()
    if not force_check and (now - _LAST_AVAILABILITY_CHECK) < _AVAILABILITY_CACHE_TTL:
        return _IS_AVAILABLE

    with _CLIENT_LOCK:
        if not force_check and (time.time() - _LAST_AVAILABILITY_CHECK) < _AVAILABILITY_CACHE_TTL:
            return _IS_AVAILABLE
        try:
            health = client.health()
            _IS_AVAILABLE = bool(health and health.get("status") == "available")
            _LAST_AVAILABILITY_CHECK = time.time()
            return _IS_AVAILABLE
        except Exception as e:
            _IS_AVAILABLE = False
            _LAST_AVAILABILITY_CHECK = time.time()
            logger.debug("⚠️ 【Meilisearch健康检测失败】服务当前不可用，将自动降级: %s", format_exc_summary(e))
            return False


# 电商高频搜索同义词与简称对照表
DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    # 饮料冲调
    "可乐": ["可口可乐", "百事可乐"],
    "可口可乐": ["可乐"],
    "百事可乐": ["可乐"],
    "雪碧": ["柠檬汽水", "雪碧汽水"],
    "芬达": ["橙味汽水", "芬达汽水"],
    "纯牛奶": ["牛奶", "鲜奶"],
    "牛奶": ["纯牛奶", "鲜奶"],
    "酸奶": ["发酵乳", "风味发酵乳"],
    "咖啡": ["美式咖啡", "拿铁咖啡", "速溶咖啡"],
    # 纸品湿巾与家居日化
    "抽纸": ["纸巾", "面巾纸", "抽纸巾"],
    "纸巾": ["抽纸", "面巾纸", "餐巾纸"],
    "卷纸": ["卷筒纸", "卫生纸"],
    "湿巾": ["湿纸巾", "婴儿湿巾"],
    "洗衣液": ["洗衣凝珠", "洗衣粉", "洗衣皂"],
    "洗洁精": ["洗洁剂", "餐具洗涤剂"],
    "洗发水": ["洗发露", "洗发膏"],
    "沐浴露": ["沐浴乳", "沐浴液"],
    "牙膏": ["洁齿膏"],
    # 粮油速食
    "方便面": ["泡面", "速食面"],
    "挂面": ["面条", "手排面"],
    "食用油": ["花生油", "大豆油", "菜籽油", "玉米油"],
    # 数码配件
    "充电宝": ["移动电源"],
    "数据线": ["充电线", "快充线"],
    "耳机": ["蓝牙耳机", "无线耳机"],
    "手机": ["智能手机"],
}


def ensure_index_configured() -> bool:
    """确保 products 索引及分词过滤属性已就绪"""
    global _INDEX_CONFIGURED
    if _INDEX_CONFIGURED:
        return True

    client = get_meili_client()
    if not client or not is_meili_available():
        return False

    with _CLIENT_LOCK:
        if _INDEX_CONFIGURED:
            return True
        try:
            index = client.index(INDEX_NAME)
            try:
                index.get_raw_info()
            except Exception:
                client.create_index(INDEX_NAME, {"primaryKey": "id"})
                index = client.index(INDEX_NAME)

            # 配置可搜索字段、可过滤字段、可排序字段与排序规则
            index.update_searchable_attributes(["name", "brand", "mall_name", "category", "search_keywords"])
            index.update_filterable_attributes(["platform", "category", "is_off_promotion", "is_deal_tip", "channel_type"])
            index.update_sortable_attributes(["updated_at", "activity_price", "price", "saved_price"])
            index.update_ranking_rules(["words", "typo", "proximity", "attribute", "sort", "exactness"])
            index.update_synonyms(DEFAULT_SYNONYMS)
            _INDEX_CONFIGURED = True
            logger.info("✅ 【Meilisearch索引就绪】products 索引及同义词配置已完成")
            return True
        except Exception as e:
            logger.warning("❌ 【Meilisearch配置索引异常】: %s", format_exc_summary(e))
            return False


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
            filter_exprs.append(f'platform = "{platform.lower()}"')
        if category and category.strip() and category.strip() != "全部":
            safe_cat = category.strip().replace('"', '\\"')
            filter_exprs.append(f'category = "{safe_cat}"')
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

        # 过滤掉 None 值的键
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
        global _IS_AVAILABLE
        _IS_AVAILABLE = False
        logger.warning(
            "❌ 【Meilisearch检索失败】关键词[%s] 平台[%s] 异常<%s>，自动回退到数据库查询",
            keyword, platform, format_exc_summary(e),
        )
        return None


def index_documents(docs: List[Dict[str, Any]]) -> int:
    """批量向 Meilisearch 同步或更新文档（安全静默执行）"""
    if not docs:
        return 0
    if not is_meili_available():
        return 0
    client = get_meili_client()
    if not client:
        return 0

    formatted = []
    for d in docs:
        item = format_meili_doc(d)
        if item:
            formatted.append(item)
    if not formatted:
        return 0

    try:
        ensure_index_configured()
        index = client.index(INDEX_NAME)
        task = index.add_documents(formatted)
        return len(formatted)
    except Exception as e:
        logger.warning("❌ 【Meilisearch增量同步失败】提交[%d]条文档发生异常: %s", len(formatted), format_exc_summary(e))
        return 0


def delete_documents(doc_ids: List[str]) -> int:
    """从 Meilisearch 删除指定文档（安全静默执行）"""
    if not doc_ids:
        return 0
    if not is_meili_available():
        return 0
    client = get_meili_client()
    if not client:
        return 0

    try:
        index = client.index(INDEX_NAME)
        index.delete_documents(doc_ids)
        return len(doc_ids)
    except Exception as e:
        logger.warning("❌ 【Meilisearch删除文档失败】: %s", format_exc_summary(e))
        return 0


def sync_all_products(mongo_db, batch_size: int = 2000, progress_cb=None) -> int:
    """
    全量将 MongoDB products 集合同步到 Meilisearch
    :param mongo_db: MongoDB 数据库实例
    :param batch_size: 批处理数量
    :param progress_cb: 进度回调函数 callback(synced_count, total_count)
    :return: 成功同步的总记录数
    """
    client = get_meili_client()
    if not client or not is_meili_available():
        raise RuntimeError("Meilisearch 服务未就绪，无法执行全量同步")

    ensure_index_configured()
    index = client.index(INDEX_NAME)
    col = mongo_db.get_collection("products") if hasattr(mongo_db, "get_collection") else mongo_db["products"]

    query = {"is_off_promotion": {"$ne": True}}
    total = col.count_documents(query)
    logger.info("🚀 【Meilisearch全量同步开始】MongoDB待同步商品总数: %d 条", total)

    cursor = col.find(query).batch_size(batch_size)
    batch = []
    synced = 0

    for doc in cursor:
        item = format_meili_doc(doc)
        if item:
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

    logger.info("🎉 【Meilisearch全量同步完成】累计同步成功 %d 条商品", synced)
    return synced
