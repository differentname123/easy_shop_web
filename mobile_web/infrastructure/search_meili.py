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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from infrastructure.http_utils import clean_money
from infrastructure.logging_utils import format_exc_summary, get_app_logger
from infrastructure.settings import get_settings
from models.product import is_product_reliable

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

MEILI_SCHEMA_VERSION = 4
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
_PURE_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")


def _collect_entities_from_fmt(fmt: Any) -> Tuple[List[str], List[str]]:
    """从单个 format_info 节点提取主/次本体名称；任何字段异常均安全忽略。"""
    primary: List[str] = []
    secondary: List[str] = []
    if not isinstance(fmt, dict):
        return primary, secondary
    if "core_entities" in fmt or "pricing_basis" in fmt or "decision_keywords" in fmt:
        entries = [fmt]
    else:
        entries = [v for v in fmt.values() if isinstance(v, dict)]
    for ent in entries:
        raw_ces = ent.get("core_entities")
        if not isinstance(raw_ces, list):
            continue
        for idx, item in enumerate(raw_ces):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            raw_score = item.get("score")
            try:
                score = (
                    int(raw_score)
                    if raw_score is not None and not isinstance(raw_score, bool)
                    else (10 if idx == 0 else 5)
                )
            except (ValueError, TypeError):
                score = 10 if idx == 0 else 5
            if idx == 0 or score >= 9:
                if name not in primary:
                    primary.append(name)
            else:
                if name not in secondary:
                    secondary.append(name)
    return primary, secondary


def extract_core_entities(doc: Any) -> Tuple[List[str], List[str]]:
    """
    极简可选读取商品本体名 (`core_entities[].name`)：
    - 优先读取顶层官方 `format_info`（完整标题清洗）；
    - `deal_info.format_info` 仅在官方缺失时作为主本体兜底，否则仅补充为次本体别名；
    - 缺失或非预期类型时安全返回 `([], [])`，绝不强依赖或阻塞搜索。
    """
    if not isinstance(doc, dict):
        return [], []
    top_p, top_s = _collect_entities_from_fmt(doc.get("format_info"))
    deal_info = doc.get("deal_info")
    deal_fmt = deal_info.get("format_info") if isinstance(deal_info, dict) else None
    deal_p, deal_s = _collect_entities_from_fmt(deal_fmt)

    if top_p or top_s:
        primary = list(dict.fromkeys(top_p))
        sec_candidates = top_s + deal_p + deal_s
    else:
        primary = list(dict.fromkeys(deal_p))
        sec_candidates = deal_s

    primary_set = set(primary)
    secondary = [s for s in dict.fromkeys(sec_candidates) if s not in primary_set]
    return primary, secondary


def _expand_cjk_right_suffixes(phrases: Sequence[str]) -> str:
    """仅对短纯中文本体做右中心语后缀展开（如 生鲜牛肉 -> 生鲜牛肉 鲜牛肉 牛肉），缓解分词边界漏召回。"""
    tokens: List[str] = []
    for raw in phrases:
        phrase = str(raw or "").strip()
        if not phrase:
            continue
        if phrase not in tokens:
            tokens.append(phrase)
        if _PURE_CJK_RE.fullmatch(phrase) and 3 <= len(phrase) <= 6:
            for start in range(1, len(phrase) - 1):
                suffix = phrase[start:]
                if len(suffix) >= 2 and suffix not in tokens:
                    tokens.append(suffix)
    return " ".join(tokens)


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
    """将 MongoDB products 文档安全转换为极简 Meilisearch 索引文档结构"""
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

    primary_ents, secondary_ents = extract_core_entities(doc)

    return {
        "id": doc_id,
        "product_id": product_id,
        "platform": platform,
        "name": name,
        "brand": str(doc.get("brand") or "").strip(),
        "mall_name": str(doc.get("mall_name") or "").strip(),
        "category": str(doc.get("category") or "").strip(),
        "core_entity": _expand_cjk_right_suffixes(primary_ents),
        "core_entity_alias": _expand_cjk_right_suffixes(secondary_ents),
        "core_entities_raw": primary_ents,
        "core_entities_alias_raw": secondary_ents,
        "price": price,
        "activity_price": activity_price,
        "saved_price": saved_price,
        "is_off_promotion": bool(doc.get("is_off_promotion") is True or doc.get("superseded_by")),
        "is_deal_tip": is_deal_tip,
        "is_user_collected": bool(doc.get("is_user_collected") is True),
        "is_reliable": is_product_reliable(doc),
        "channel_type": channel_type,
        "image_url": str(doc.get("image_url") or "").strip(),
        "updated_at": ts,
        "sync_ts": sync_ts,
        "schema_ver": MEILI_SCHEMA_VERSION,
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


# 电商高频严格等价同义词（保持精准对应，避免跨形态泛化导致降维污染）
DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "可乐": ["可口可乐", "百事可乐"],
    "鲜牛肉": ["生鲜牛肉", "原切牛肉"],
    "生鲜牛肉": ["鲜牛肉"],
    "纯牛奶": ["纯奶"],
    "抽纸": ["面巾纸", "抽纸巾"],
    "卷纸": ["卷筒纸", "卫生纸"],
    "卫生纸": ["卷纸", "卷筒纸", "厕纸"],
    "湿巾": ["湿纸巾", "婴儿湿巾"],
    "酸奶": ["发酵乳", "风味发酵乳"],
    "咖啡": ["美式咖啡", "拿铁咖啡", "速溶咖啡"],
    "洗洁精": ["洗洁剂", "餐具洗涤剂"],
    "洗发水": ["洗发露"],
    "沐浴露": ["沐浴乳", "沐浴液"],
    "牙膏": ["洁齿膏"],
    "方便面": ["泡面", "速食面"],
    "挂面": ["面条", "手排面"],
    "充电宝": ["移动电源"],
    "数据线": ["充电线", "快充线"],
    "耳机": ["蓝牙耳机", "无线耳机"],
    "手机": ["智能手机"],
    "苹果手机": ["iPhone"],
}


def ensure_index_configured(force: bool = False) -> bool:
    """确保 products 索引及中文分词、关闭中文容错、极简检索字段配置已就绪。"""
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
                "searchableAttributes": ["core_entity", "core_entity_alias", "brand", "name"],
                "filterableAttributes": ["platform", "category", "is_off_promotion", "is_deal_tip", "is_user_collected", "is_reliable", "channel_type"],
                "sortableAttributes": ["is_reliable", "updated_at", "activity_price", "price", "saved_price"],
                "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
                "typoTolerance": {"enabled": False},
                "localizedAttributes": [{"attributePatterns": ["*"], "locales": ["cmn"]}],
                "synonyms": DEFAULT_SYNONYMS,
            })
            _INDEX_CONFIGURED = True
            logger.info("✅ 【Meilisearch索引就绪】products 极简中文索引配置已完成")
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
    通过 Meilisearch 执行无容错污染的精准中文分词搜索（matchingStrategy="all"）。
    :return: 命中商品列表；如果 Meilisearch 不可用则返回 None（触发上游回退 Mongo）
    """
    if not keyword or not keyword.strip():
        return None

    if not is_meili_available():
        return None

    client = get_meili_client()
    if not client:
        return None

    try:
        if not _INDEX_CONFIGURED:
            ensure_index_configured()
        index = client.index(INDEX_NAME)

        filter_exprs = ["NOT is_off_promotion = true"]
        if platform and platform.lower() != "all":
            filter_exprs.append(f'platform = "{_escape_filter_val(platform.lower())}"')
        if category and category.strip() and category.strip() != "全部":
            filter_exprs.append(f'category = "{_escape_filter_val(category.strip())}"')
        if only_active_boom:
            filter_exprs.append("(channel_type IS NOT NULL OR is_deal_tip = true OR is_user_collected = true)")

        sort_mapping = {
            "latest": ["is_reliable:desc", "updated_at:desc"],
            "price_asc": ["is_reliable:desc", "activity_price:asc"],
            "price_desc": ["is_reliable:desc", "activity_price:desc"],
            "save_desc": ["is_reliable:desc", "saved_price:desc"],
        }
        sort_rule = sort_mapping.get(sort_by) or ["is_reliable:desc", "updated_at:desc"]

        search_params: Dict[str, Any] = {
            "limit": max(1, min(1000, limit)),
            "offset": max(0, offset),
            "filter": " AND ".join(filter_exprs) if filter_exprs else None,
            "matchingStrategy": "all",
        }
        if sort_rule:
            search_params["sort"] = sort_rule

        search_params = {k: v for k, v in search_params.items() if v is not None}

        result = index.search(keyword.strip(), search_params)
        return result.get("hits") or []
    except Exception as e:
        _mark_unavailable()
        logger.warning(
            "❌ 【Meilisearch检索失败】关键词[%s] 平台[%s] 异常<%s>，自动回退到数据库查询",
            keyword, platform, format_exc_summary(e),
        )
        return None


def sync_single_product(doc: Dict[str, Any]) -> bool:
    """将单条新增/更新商品尽力实时推送到 Meilisearch；失败不影响业务且由定时差量兜底。"""
    if not is_meili_available():
        return False
    meili_doc = format_meili_doc(doc)
    if meili_doc is None:
        return False
    client = get_meili_client()
    if not client:
        return False
    try:
        if not _INDEX_CONFIGURED:
            ensure_index_configured()
        client.index(INDEX_NAME).add_documents([meili_doc])
        return True
    except Exception as exc:
        logger.debug("⚠️ 【Meilisearch单条推送跳过】商品[%s] 异常<%s>", meili_doc.get("id"), format_exc_summary(exc))
        return False


def _extract_item_search_fields(item: Dict[str, Any]) -> Tuple[str, str, List[str], List[str]]:
    name = str(item.get("name") or "").strip()
    brand = str(item.get("brand") or "").strip()
    if "core_entities_raw" in item or "core_entities_alias_raw" in item:
        raw_p = item.get("core_entities_raw")
        raw_s = item.get("core_entities_alias_raw")
        primary = [str(x).strip() for x in (raw_p if isinstance(raw_p, list) else ()) if str(x or "").strip()]
        secondary = [str(x).strip() for x in (raw_s if isinstance(raw_s, list) else ()) if str(x or "").strip()]
    else:
        primary, secondary = extract_core_entities(item)
    return name, brand, primary, secondary


def _match_modifier_head(query_lower: str, entities_lower: Sequence[str], mod_haystack: str) -> bool:
    """检查 '修饰词+核心本体' 拆分（如 苹果+手机、伊利+纯牛奶、鲜+牛肉）是否命中。"""
    if len(query_lower) < 3 or not entities_lower:
        return False
    for split_i in range(1, len(query_lower) - 1):
        mod = query_lower[:split_i]
        head = query_lower[split_i:]
        if len(head) >= 2 and any(e == head or e.endswith(head) for e in entities_lower):
            if mod in mod_haystack:
                return True
    return False


def partition_search_candidates(
    keyword: str,
    items: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    极简相关性分桶（SSOT）：不修改性价比/时间排序逻辑，仅在排序前净化候选集。
    - 无清洗数据时平滑退化为无容错标题/品牌精准命中（绝不依赖 AI 字段存在）；
    - 有可选 `core_entities` 时按中文右中心语 (`endswith`) 区分：
      L1 (strict_items): 主本体直击 / 修饰词+主本体 / 明确品牌匹配 / 品牌词自适应提升
      L2 (secondary_items): 次本体直击 / 本体衍生品包含（如 牛肉 -> 牛肉丸、牛肉辣酱）
      L3 (fallback_items): 仅标题蹭词且本体无关（如狗粮、手机壳、充电线、猫砂）
    """
    raw_q = str(keyword or "").strip()
    if not raw_q or not items:
        return list(items), [], []

    ql = raw_q.lower()
    tokens = [t for t in ql.split() if t]
    syn_list = [str(s).strip().lower() for s in DEFAULT_SYNONYMS.get(raw_q, []) if str(s or "").strip()]
    q_variants = list(dict.fromkeys([ql] + syn_list))

    tagged: List[Tuple[Dict[str, Any], int, bool]] = []
    head_entity_hits = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        name, brand, primary, secondary = _extract_item_search_fields(item)
        if not name and not brand and not primary and not secondary:
            tagged.append((item, 1, True))
            continue

        name_l = name.lower()
        brand_l = brand.lower()
        p_l = [p.lower() for p in primary]
        s_l = [s.lower() for s in secondary]
        all_e = p_l + s_l

        if len(tokens) >= 2:
            name_brand_ok = all(t in name_l or t in brand_l for t in tokens)
        else:
            name_brand_ok = any(qv in name_l or qv in brand_l for qv in q_variants)

        # Case A: 尚无清洗数据的商品，无缝退化为标题/品牌精准命中
        if not all_e:
            tagged.append((item, 1 if name_brand_ok else 3, name_brand_ok))
            continue

        # Case B: 多空格词查询（如 "伊利 纯牛奶"、"iPhone 17"）
        if len(tokens) >= 2:
            searchable_blob = f"{name_l} {brand_l} {' '.join(all_e)}"
            if not all(t in searchable_blob for t in tokens):
                tagged.append((item, 3, name_brand_ok))
                continue
            head = tokens[-1]
            if any(e == head or e.endswith(head) for e in p_l):
                head_entity_hits += 1
                tagged.append((item, 1, name_brand_ok))
            elif any(e == head or e.endswith(head) for e in s_l):
                head_entity_hits += 1
                tagged.append((item, 2, name_brand_ok))
            elif any(head in e for e in all_e):
                tagged.append((item, 2, name_brand_ok))
            else:
                tagged.append((item, 3, name_brand_ok))
            continue

        # Case C: 单词查询
        mod_haystack = f"{brand_l} {name_l} {' '.join(all_e)}"
        tier = 3
        is_head_ent = False

        if any(e == qv or e.endswith(qv) for qv in q_variants for e in p_l):
            tier = 1
            is_head_ent = True
        elif _match_modifier_head(ql, p_l, mod_haystack):
            tier = 1
            is_head_ent = True
        elif (
            len(ql) >= 4
            and len(q_variants) > 1
            and any(e == ql[-2:] or e.endswith(ql[-2:]) for e in p_l)
            and any(qv in name_l or qv in brand_l for qv in q_variants[1:])
        ):
            tier = 1
            is_head_ent = True
        elif brand_l and (ql == brand_l or (len(ql) >= 2 and ql in brand_l)):
            tier = 1
        elif any(e == qv or e.endswith(qv) for qv in q_variants for e in s_l) or _match_modifier_head(ql, s_l, mod_haystack):
            tier = 2
            is_head_ent = True
        elif any(qv in e for qv in q_variants for e in all_e):
            tier = 2

        if is_head_ent:
            head_entity_hits += 1
        tagged.append((item, tier, name_brand_ok))

    strict_items: List[Dict[str, Any]] = []
    secondary_items: List[Dict[str, Any]] = []
    fallback_items: List[Dict[str, Any]] = []
    promote_brand_or_model = head_entity_hits == 0

    for item, tier, name_brand_ok in tagged:
        if tier == 1 or (promote_brand_or_model and name_brand_ok):
            strict_items.append(item)
        elif tier == 2:
            secondary_items.append(item)
        else:
            fallback_items.append(item)

    return strict_items, secondary_items, fallback_items


def select_relevant_candidates(
    keyword: str,
    items: Sequence[Dict[str, Any]],
    *,
    min_primary: int = 5,
) -> List[List[Dict[str, Any]]]:
    """返回参与排序的分层候选桶列表：高相关 (L1+L2) 充足时屏蔽 L3 噪音；不足时附带 L3 兜底。"""
    strict_items, secondary_items, fallback_items = partition_search_candidates(keyword, items)
    buckets: List[List[Dict[str, Any]]] = []
    if strict_items:
        buckets.append(strict_items)
    if secondary_items:
        buckets.append(secondary_items)
    if (len(strict_items) + len(secondary_items)) < min_primary and fallback_items:
        buckets.append(fallback_items)
    return buckets


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
) -> Tuple[Dict[str, Tuple[Optional[int], Optional[int]]], bool]:
    """分页拉取 Meilisearch 全部文档的 (id -> (sync_ts, schema_ver))，并返回是否完整扫描完。"""
    step = max(1, int(page_size))
    meili_map: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    offset = 0
    complete = False
    for _ in range(max_pages):
        res = index.get_documents({"fields": ["id", "sync_ts", "schema_ver"], "limit": step, "offset": offset})
        results = res.get("results") if isinstance(res, dict) else getattr(res, "results", None)
        if not results:
            complete = True
            break
        for item in results:
            if isinstance(item, dict):
                doc_id = item.get("id")
                raw_ts = item.get("sync_ts")
                raw_ver = item.get("schema_ver")
            else:
                doc_id = getattr(item, "id", None)
                raw_ts = getattr(item, "sync_ts", None)
                raw_ver = getattr(item, "schema_ver", None)
            clean_id = str(doc_id or "").strip()
            if not clean_id:
                continue
            sync_ts = (
                int(raw_ts)
                if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool)
                else None
            )
            schema_ver = (
                int(raw_ver)
                if isinstance(raw_ver, (int, float)) and not isinstance(raw_ver, bool)
                else None
            )
            meili_map[clean_id] = (sync_ts, schema_ver)
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
        doc_id
        for doc_id, (_, _, m_ts) in mongo_meta.items()
        if meili_map.get(doc_id) != (m_ts, MEILI_SCHEMA_VERSION)
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
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def wake(self) -> None:
        self._wake_event.set()

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
        self._wake_event.set()
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
            self._wake_event.wait(timeout=self.interval_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
