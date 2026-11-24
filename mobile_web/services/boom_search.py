# -*- coding: utf-8 -*-
"""
严选好价（默认浏览 + 全文搜索）高内聚查询与排序引擎。
================================================================================
统一收口严选好价的：
1. 默认排序策略解析 (`resolve_boom_sort`) 与筛选条件构建 (`build_boom_filter_query`)；
2. 关键词检索双通道召回 (`fetch_boom_search_docs`: Meilisearch 主路 + MongoDB 可靠优先降级)；
3. 全局相关性去噪 + 可靠优先分层 + 性价比/常规排序 (`order_boom_search_docs`)；
4. 无搜索词零全表扫描双段索引分页 (`paginate_boom_browse_mongo`)。
================================================================================
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from infrastructure.logging_utils import format_exc_summary
from infrastructure.search_meili import select_relevant_candidates
from models.cost_performance import rank_products_by_cost_performance
from models.product import (
    extract_search_keyword,
    is_product_reliable,
    normalize_platform,
    reliable_product_mongo_filter,
    unreliable_product_mongo_filter,
)

BOOM_SORT_SPEC_MAP: Dict[str, List[Tuple[str, int]]] = {
    "latest": [("updated_at", -1)],
    "save_desc": [("saved_price", -1), ("updated_at", -1)],
    "price_asc": [("activity_price", 1)],
    "price_desc": [("activity_price", -1)],
}


def resolve_boom_sort(keyword: Optional[str], sort_by: Optional[str]) -> Tuple[str, str]:
    """归一关键词并返回当前请求应使用的生效排序键 `(cleaned_keyword, effective_sort)`。"""
    raw_kw = keyword.strip() if isinstance(keyword, str) and keyword.strip() else ""
    kw_key = (extract_search_keyword(raw_kw, max_length=60) or raw_kw[:60]) if raw_kw else ""
    if not kw_key:
        effective_sort = "latest" if sort_by in (None, "", "cost_performance") else str(sort_by)
    else:
        effective_sort = "cost_performance" if sort_by in (None, "") else str(sort_by)
    return kw_key, effective_sort


def build_boom_filter_query(
    platform: str = "all",
    category: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """统一构造严选好价 MongoDB 筛选条件（平台、类目、正则安全转义关键词）。"""
    filter_query: Dict[str, Any] = {}
    norm_plat = normalize_platform(platform, "all") if platform else "all"
    if norm_plat != "all":
        filter_query["platform"] = norm_plat
    if isinstance(category, str) and category.strip() and category.strip() != "全部":
        filter_query["category"] = category.strip()
    cleaned_kw, _ = resolve_boom_sort(keyword, None)
    if cleaned_kw:
        filter_query["name"] = {"$regex": re.escape(cleaned_kw), "$options": "i"}
    return filter_query


def _doc_identity_key(doc: Dict[str, Any]) -> Tuple[str, str]:
    plat = str(doc.get("platform") or "").strip().lower()
    pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
    return plat, pid


def _dedup_docs_preserve_order(docs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique_docs: List[Dict[str, Any]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        key = _doc_identity_key(doc)
        if key[0] and key[1]:
            if key in seen:
                continue
            seen.add(key)
        unique_docs.append(doc)
    return unique_docs


def _has_any_doc(col: Any, query: Dict[str, Any]) -> bool:
    """用 1 条轻探针判断是否存在匹配文档（兼容真实集合与测试 Double）。"""
    probe = list(col.find(query, {"_id": 1}).limit(1).max_time_ms(2000))
    return len(probe) > 0


def _count_matching_docs(col: Any, query: Dict[str, Any]) -> int:
    count_fn = getattr(col, "count_documents", None)
    if callable(count_fn):
        raw_cnt = count_fn(query)
        if isinstance(raw_cnt, int) and not isinstance(raw_cnt, bool):
            return raw_cnt
    return len(list(col.find(query, {"_id": 1}).max_time_ms(2000)))


def _fetch_meili_docs(
    col: Any,
    *,
    cleaned_kw: str,
    platform: str,
    category: Optional[str],
    sort_by: str,
    projection: Dict[str, Any],
    boom_filter: Dict[str, Any],
    max_candidates: int,
    search_fn: Callable[..., Optional[List[Dict[str, Any]]]],
    logger: Optional[logging.Logger] = None,
) -> Optional[List[Dict[str, Any]]]:
    try:
        hits = search_fn(
            keyword=cleaned_kw,
            platform=platform,
            category=category,
            sort_by=None if sort_by == "cost_performance" else sort_by,
            limit=max_candidates,
            offset=0,
            only_active_boom=True,
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("⚠️ 【Meilisearch商品检索降级】: %s", format_exc_summary(exc))
        return None

    if hits is None:
        return None
    if not hits:
        return []

    doc_keys = [(h.get("platform"), h.get("product_id")) for h in hits if h.get("platform") and h.get("product_id")]
    if not doc_keys:
        return []

    docs_map: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for start in range(0, len(doc_keys), 500):
        or_clauses = [{"platform": p, "product_id": pid} for p, pid in doc_keys[start : start + 500]]
        for d in col.find({"$and": [{"$or": or_clauses}, boom_filter]}, projection):
            if isinstance(d, dict):
                docs_map[(d.get("platform"), d.get("product_id"))] = d

    return _dedup_docs_preserve_order(
        [docs_map[(p, pid)] for p, pid in doc_keys if (p, pid) in docs_map]
    )


def _fetch_mongo_search_docs(
    col: Any,
    *,
    filter_query: Dict[str, Any],
    boom_filter: Dict[str, Any],
    projection: Dict[str, Any],
    sort_spec: List[Tuple[str, int]],
    max_candidates: int,
) -> List[Dict[str, Any]]:
    base_and = [filter_query, boom_filter]
    rel_docs = list(
        col.find({"$and": [*base_and, reliable_product_mongo_filter()]}, projection)
        .sort(sort_spec)
        .limit(max_candidates)
        .max_time_ms(2000)
    )
    docs = _dedup_docs_preserve_order(rel_docs)
    remaining = max_candidates - len(docs)
    if remaining > 0:
        unrel_docs = list(
            col.find({"$and": [*base_and, unreliable_product_mongo_filter()]}, projection)
            .sort(sort_spec)
            .limit(remaining)
            .max_time_ms(2000)
        )
        docs = _dedup_docs_preserve_order([*docs, *unrel_docs])
    return docs


def fetch_boom_search_docs(
    col: Any,
    *,
    platform: str,
    category: Optional[str],
    keyword: str,
    sort_by: str,
    projection: Dict[str, Any],
    boom_filter: Dict[str, Any],
    max_candidates: int,
    search_fn: Optional[Callable[..., Optional[List[Dict[str, Any]]]]] = None,
    meili_enabled: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    关键词检索统一召回通道：
    - Meilisearch 可用时由引擎按相关度与 `is_reliable:desc` 召回后回表过滤；
    - 不可用时降级 MongoDB 可靠池优先召回、未满上限再补不可靠池。
    返回 `(docs, used_meili)`。
    """
    cleaned_kw, _ = resolve_boom_sort(keyword, sort_by)
    if not cleaned_kw:
        return [], False

    if meili_enabled and search_fn is not None:
        meili_docs = _fetch_meili_docs(
            col,
            cleaned_kw=cleaned_kw,
            platform=platform,
            category=category,
            sort_by=sort_by,
            projection=projection,
            boom_filter=boom_filter,
            max_candidates=max_candidates,
            search_fn=search_fn,
            logger=logger,
        )
        if meili_docs is not None:
            return meili_docs, True

    filter_query = build_boom_filter_query(platform=platform, category=category, keyword=cleaned_kw)
    sort_spec = BOOM_SORT_SPEC_MAP.get(sort_by, [("updated_at", -1)])
    mongo_docs = _fetch_mongo_search_docs(
        col,
        filter_query=filter_query,
        boom_filter=boom_filter,
        projection=projection,
        sort_spec=sort_spec,
        max_candidates=max_candidates,
    )
    return mongo_docs, False


def order_boom_search_docs(
    docs: Sequence[Dict[str, Any]],
    *,
    keyword: Optional[str],
    sort_by: str,
    normalize_summary: Callable[[Dict[str, Any]], Dict[str, Any]],
    now_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    统一排序与分层：
    1. 有搜索词时先全局按相关性剔除低相关 L3 噪音（防可靠噪音挤掉不可靠真品）；
    2. 将保留下来的全部相关桶严格拆为「全部可靠桶在前 -> 全部不可靠桶在后」；
    3. 每个桶内按 `cost_performance` 性价比排序或保持外层 `sort_by` 顺序。
    """
    doc_list = [d for d in docs if isinstance(d, dict)]
    if not doc_list:
        return []

    cleaned_kw, _ = resolve_boom_sort(keyword, sort_by)
    buckets = select_relevant_candidates(cleaned_kw, doc_list, min_primary=1) if cleaned_kw else [doc_list]
    reliable_buckets = [[d for d in b if is_product_reliable(d)] for b in buckets]
    unreliable_buckets = [[d for d in b if not is_product_reliable(d)] for b in buckets]
    tiered_buckets = [b for b in reliable_buckets if b] + [b for b in unreliable_buckets if b]

    ordered: List[Dict[str, Any]] = []
    for bucket in tiered_buckets:
        if sort_by == "cost_performance":
            ordered.extend(
                rank_products_by_cost_performance(
                    bucket,
                    normalize_summary=normalize_summary,
                    now_ts=now_ts,
                )
            )
        else:
            ordered.extend(normalize_summary(doc) for doc in bucket)
    return ordered


def paginate_boom_browse_mongo(
    col: Any,
    *,
    base_filter: Dict[str, Any],
    projection: Dict[str, Any],
    sort_spec: List[Tuple[str, int]],
    skip: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], bool, int]:
    """
    无搜索词默认浏览的可信优先双段索引分页（零全表扫描）：
    - 前段优先游标扫描可靠池，满页即走仅 1~2 次轻查询；
    - 交界页自动去重无缝拼接不可靠池；
    - 进入尾段后按 `skip - reliable_total` 精确偏移读取不可靠池。
    """
    rel_query = {"$and": [base_filter, reliable_product_mongo_filter()]}
    unrel_query = {"$and": [base_filter, unreliable_product_mongo_filter()]}

    rel_docs = list(
        col.find(rel_query, projection)
        .sort(sort_spec)
        .skip(skip)
        .limit(page_size + 1)
        .max_time_ms(2000)
    )
    if len(rel_docs) > page_size:
        return rel_docs[:page_size], True, skip + len(rel_docs)

    if len(rel_docs) == page_size:
        has_unrel = _has_any_doc(col, unrel_query)
        return rel_docs, has_unrel, skip + page_size + (1 if has_unrel else 0)

    if rel_docs:
        need = page_size - len(rel_docs)
        unrel_tail = list(
            col.find(unrel_query, projection)
            .sort(sort_spec)
            .skip(0)
            .limit(need + 1)
            .max_time_ms(2000)
        )
        combined = _dedup_docs_preserve_order([*rel_docs, *unrel_tail])
        has_more = len(combined) > page_size
        return combined[:page_size], has_more, skip + len(combined)

    rel_total = _count_matching_docs(col, rel_query) if skip > 0 else 0
    unrel_skip = max(0, skip - rel_total)
    unrel_docs = list(
        col.find(unrel_query, projection)
        .sort(sort_spec)
        .skip(unrel_skip)
        .limit(page_size + 1)
        .max_time_ms(2000)
    )
    has_more = len(unrel_docs) > page_size
    return unrel_docs[:page_size], has_more, skip + len(unrel_docs)


def rank_boom_cost_performance(
    col: Any,
    *,
    platform: str = "all",
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    projection: Dict[str, Any],
    boom_filter: Dict[str, Any],
    max_candidates: int,
    normalize_summary: Callable[[Dict[str, Any]], Dict[str, Any]],
    search_fn: Optional[Callable[..., Optional[List[Dict[str, Any]]]]] = None,
    meili_enabled: bool = False,
    now_ts: Optional[float] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """性价比模式检索入口：兼顾有词检索与无词直调排序，统一保障可靠优先与性价比分层。"""
    cleaned_kw, _ = resolve_boom_sort(keyword, "cost_performance")
    if cleaned_kw:
        raw_docs, _ = fetch_boom_search_docs(
            col,
            platform=platform,
            category=category,
            keyword=cleaned_kw,
            sort_by="cost_performance",
            projection=projection,
            boom_filter=boom_filter,
            max_candidates=max_candidates,
            search_fn=search_fn,
            meili_enabled=meili_enabled,
            logger=logger,
        )
    else:
        filter_query = build_boom_filter_query(platform=platform, category=category)
        raw_docs = list(
            col.find({"$and": [filter_query, boom_filter]}, projection)
            .sort([("updated_at", -1)])
            .limit(max_candidates)
            .max_time_ms(2000)
        )

    return order_boom_search_docs(
        raw_docs,
        keyword=cleaned_kw,
        sort_by="cost_performance",
        normalize_summary=normalize_summary,
        now_ts=now_ts,
    )


def query_boom_products_page(
    col: Any,
    *,
    platform: str = "all",
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    sort_by: str = "latest",
    page: int = 1,
    page_size: int = 20,
    summary_projection: Dict[str, Any],
    cost_perf_projection: Dict[str, Any],
    boom_filter: Dict[str, Any],
    max_cost_perf_candidates: int,
    normalize_summary: Callable[[Dict[str, Any]], Dict[str, Any]],
    search_fn: Optional[Callable[..., Optional[List[Dict[str, Any]]]]] = None,
    meili_enabled: bool = False,
    now_ts: Optional[float] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """严选好价统一分页查询入口（性价比、搜索与默认浏览共用）。"""
    page = max(1, min(50, int(page or 1)))
    page_size = max(1, min(50, int(page_size or 20)))
    skip = (page - 1) * page_size
    cleaned_kw, _ = resolve_boom_sort(keyword, sort_by)

    if sort_by == "cost_performance" and cleaned_kw:
        sorted_items = rank_boom_cost_performance(
            col,
            platform=platform,
            category=category,
            keyword=cleaned_kw,
            projection=cost_perf_projection,
            boom_filter=boom_filter,
            max_candidates=max_cost_perf_candidates,
            normalize_summary=normalize_summary,
            search_fn=search_fn,
            meili_enabled=meili_enabled,
            now_ts=now_ts,
            logger=logger,
        )
        page_items = sorted_items[skip : skip + page_size]
        return {
            "status": "success",
            "total_count": len(sorted_items),
            "page": page,
            "page_size": page_size,
            "has_more": (skip + page_size) < len(sorted_items),
            "data": page_items,
        }

    if cleaned_kw:
        search_limit = min(500, max(200, (skip + page_size) * 4))
        raw_docs, _ = fetch_boom_search_docs(
            col,
            platform=platform,
            category=category,
            keyword=cleaned_kw,
            sort_by=sort_by,
            projection=summary_projection,
            boom_filter=boom_filter,
            max_candidates=search_limit,
            search_fn=search_fn,
            meili_enabled=meili_enabled,
            logger=logger,
        )
        ordered_items = order_boom_search_docs(
            raw_docs,
            keyword=cleaned_kw,
            sort_by=sort_by,
            normalize_summary=normalize_summary,
            now_ts=now_ts,
        )
        page_items = ordered_items[skip : skip + page_size]
        return {
            "status": "success",
            "total_count": len(ordered_items),
            "page": page,
            "page_size": page_size,
            "has_more": (skip + page_size) < len(ordered_items),
            "data": page_items,
        }

    filter_query = build_boom_filter_query(platform=platform, category=category)
    sort_spec = BOOM_SORT_SPEC_MAP.get(sort_by, [("updated_at", -1)])
    page_docs, has_more, approx_total = paginate_boom_browse_mongo(
        col,
        base_filter={"$and": [filter_query, boom_filter]},
        projection=summary_projection,
        sort_spec=sort_spec,
        skip=skip,
        page_size=page_size,
    )
    items = [normalize_summary(doc) for doc in page_docs]
    return {
        "status": "success",
        "total_count": approx_total,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        "data": items,
    }
