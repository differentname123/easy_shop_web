# -*- coding: utf-8 -*-
"""线报清洗三核心阶段实现：Stage 1 (AI-1 拆解)、Stage 2 (官方转链核验)、Stage 3 (AI-2 终审评分)。"""
from __future__ import annotations

import copy
import datetime
import html as html_lib
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from adapters import platform_name
from config import PROMOTION_LINK_TTL
from infrastructure.api_governance import ResolutionKind, api_caller_context
from models.deal_constants import (
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    ERR_ADAPTER_CONFIG_ERROR,
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_ADAPTER_OFF_PROMOTION,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI2_MAIN_DEAD_ONLY_FILLER,
    ERR_AI2_PRODUCT_MISMATCH,
    ERR_AI2_VIRTUAL_OR_JUNK,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_AI_TIMEOUT_RETRY,
    ERR_EMPTY_PRODUCT_IDENTITY,
    ERR_JD_NUMERIC_SKU,
    ERR_LOW_QUALITY_SCORE,
    ERR_MERGED_INTO_NEIGHBOR,
    ERR_NO_ANCHOR,
    ERR_PDD_NUMERIC_ID,
    ERR_TB_NUMERIC_ID,
    ERR_UNSUPPORTED_PLATFORM,
    MAX_CLEAN_RETRIES,
    OK_SKIPPED_ALREADY_CLEANED,
    QUALIFIED_DEAL_MIN_SCORE,
    SOURCE_REPUTATION_BONUS,
    coerce_unix_timestamp,
    parse_publish_time_epoch,
)
from models.product import _TB_WRAP_CHARS, get_source_site_name
from services.deal_cleaning.cache_state import (
    AnchorConversionCache,
    query_active_formal_products,
    should_upgrade_existing_deal,
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
    ROLE_HINT_NORMALIZE_MAP,
    _is_official_ecommerce_http_url,
    _is_verified_station_promo_link,
    _precheck_direct_non_cps_venue_url,
    detect_candidate_platform,
    is_explicit_coupon_target,
    is_shop_homepage_url,
    rebuild_clean_official_venue_url,
    unwrap_to_official_public_venue_url,
)

logger = logging.getLogger("deal_pool_scheduler")

_AFFILIATE_MAIN_DOMAIN_HINTS = (
    "u.jd.com/",
    "union-click.jd.com/",
    "jingfen.jd.com/detail/",
    "s.click.taobao.com/",
    "uland.taobao.com/item/edetail",
    "m.tb.cn/",
    "e.tb.cn/",
    "p.pinduoduo.com/",
    "yangkeduo.com/",
)

_AI2_OFFICIAL_PRODUCT_KEYS = (
    "title",
    "price",
    "original_price",
    "activity_price",
    "coupon_amount",
    "shop_name",
    "commission_rate",
    "commission_amount",
    "category",
    "brand",
    "sales_tip",
)


def load_neighbor_docs(
    raw_col,
    site: str,
    merged_tip_ids: List[str],
    current_doc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """根据 merged_tip_ids 从初步表加载关联邻居文档供去链清洗使用。"""
    tip_map: Dict[str, Dict[str, Any]] = {}
    if current_doc:
        mid = str(current_doc.get("source_item_id") or "")
        if mid:
            tip_map[mid] = current_doc
    needed = [tid for tid in merged_tip_ids if tid and tid not in tip_map]
    if needed and raw_col is not None:
        try:
            for nd in raw_col.find(
                {"source_site": site, "source_item_id": {"$in": needed}},
                {"raw_payload": 0},
            ):
                ntid = str(nd.get("source_item_id") or "").strip()
                if ntid:
                    tip_map[ntid] = nd
        except Exception as e:
            logger.warning("补查邻居消息文档异常 (%s: %s): %s", site, needed, e)
    return tip_map


def build_stage1_task_input(doc: Dict[str, Any], raw_col) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """构造单条记录的 Stage 1 AI 输入，群聊流额外合并同群 ±3 分钟邻居上下文。"""
    site = str(doc.get("source_site") or "")
    main_tip_id = str(doc.get("source_item_id") or "")
    is_group_stream = bool(doc.get("is_group_stream") or (site == "zhetaoke" and main_tip_id.startswith("xb_")))
    group_id = str(doc.get("group_id") or (doc.get("hot_metrics") or {}).get("group_id") or "").strip()
    pub_ts = doc.get("publish_ts")
    if not isinstance(pub_ts, (int, float)):
        pub_ts = parse_publish_time_epoch(doc.get("publish_time"))

    neighbor_docs: List[Dict[str, Any]] = [doc]
    if is_group_stream and group_id and raw_col is not None and isinstance(pub_ts, (int, float)):
        try:
            cursor = (
                raw_col.find(
                    {
                        "source_site": site,
                        "group_id": group_id,
                        "publish_ts": {"$gte": float(pub_ts) - 180.0, "$lte": float(pub_ts) + 180.0},
                    },
                    {"raw_payload": 0},
                )
                .sort([("publish_ts", 1), ("source_item_id", 1)])
                .limit(7)
            )
            fetched_neighbors = list(cursor)
            if fetched_neighbors:
                if not any(str(d.get("source_item_id")) == main_tip_id for d in fetched_neighbors):
                    fetched_neighbors.append(doc)
                fetched_neighbors.sort(
                    key=lambda x: (
                        float(x.get("publish_ts") or pub_ts),
                        str(x.get("source_item_id") or ""),
                    )
                )
                neighbor_docs = fetched_neighbors
        except Exception as e:
            logger.warning("查询同群 [%s] 相邻 ±3 分钟消息异常: %s", group_id, e)

    tip_map: Dict[str, Dict[str, Any]] = {}
    raw_messages: List[Dict[str, Any]] = []
    for nd in neighbor_docs:
        tid = str(nd.get("source_item_id") or "")
        if not tid or tid in tip_map:
            continue
        tip_map[tid] = nd
        raw_messages.append(format_raw_message_for_ai1(nd, is_target=(tid == main_tip_id)))

    return {
        "main_tip_id": main_tip_id,
        "source_site": site,
        "source_channel": str(doc.get("source_channel") or ""),
        "is_group_stream": is_group_stream,
        "raw_messages": raw_messages,
    }, tip_map


def fallback_stage1_extract(doc: Dict[str, Any], fail_reason: str = "AI1_TIMEOUT_OR_EMPTY") -> Dict[str, Any]:
    """Fail-Closed：Stage 1 超时/空返回时不伪造拆解，标记 _from_fallback=True 走重试机制。"""
    main_tip_id = str(doc.get("source_item_id") or "")
    msg = format_raw_message_for_ai1(doc, is_target=True)
    return {
        "main_tip_id": main_tip_id,
        "is_standalone_deal": True,
        "merged_tip_ids": [main_tip_id],
        "combined_raw_text": msg["content"].strip(),
        "extracted_images": extract_doc_images(doc),
        "extracted_targets": [],
        "deal_price": None,
        "tutorial_score": 50,
        "_from_fallback": True,
        "_fallback_reason": fail_reason,
    }


def fallback_stage2_evaluate(s2_input: Dict[str, Any]) -> Dict[str, Any]:
    """Fail-Closed：Stage 2 超时/空返回时拦截入库，走重试计数。"""
    mid = str(s2_input.get("main_tip_id") or "")
    return {
        "main_tip_id": mid,
        "is_valid_deal": False,
        "reject_reason": "AI2_AUDIT_UNAVAILABLE",
        "main_link_id": None,
        "quality_score": 0,
        "_from_fallback": True,
    }


def _supplement_structured_links(
    norm_targets: List[Dict[str, Any]],
    merged_tip_ids: List[str],
    tip_map: Dict[str, Dict[str, Any]],
    orig_doc: Dict[str, Any],
) -> None:
    """统一按确定性结构化链接（联盟商品短链 / 官方白名单领券或会场页）补全 AI-1 漏提链接，不依赖站点特判与营销词。"""
    seen_clean_inputs = {t["clean_input"] for t in norm_targets}
    for m_tid in merged_tip_ids:
        src_d = tip_map.get(m_tid) or {}
        for lk in src_d.get("extracted_links") or []:
            if not isinstance(lk, dict) or len(norm_targets) >= 8:
                continue
            u_cand = strip_markdown_link_wrapper(str(lk.get("url") or "").strip())
            if not u_cand or u_cand in seen_clean_inputs:
                continue
            u_low = u_cand.lower()
            default_plat = str(lk.get("platform") or src_d.get("platform") or "unknown")
            is_venue = rebuild_clean_official_venue_url(u_cand) is not None

            if not is_venue and any(dom in u_low for dom in _AFFILIATE_MAIN_DOMAIN_HINTS):
                seen_clean_inputs.add(u_cand)
                norm_targets.append({
                    "link_id": f"L{len(norm_targets) + 1}",
                    "raw_span": u_cand,
                    "clean_input": u_cand,
                    "platform": detect_candidate_platform(u_cand, default_plat),
                    "text_role_hint": "main_candidate",
                })
                continue

            t_low = str(lk.get("type") or "").strip().lower()
            is_explicit_cpn = "duo_coupon_landing" not in u_low and (
                is_venue
                or t_low in ("coupon_url", "tb_coupon")
                or ("uland.taobao.com" in u_low and "/coupon/" in u_low)
            )
            if is_explicit_cpn:
                c_plat = detect_candidate_platform(u_cand, default_plat)
                if c_plat in ("jd", "taobao", "pdd"):
                    seen_clean_inputs.add(u_cand)
                    norm_targets.append({
                        "link_id": f"L{len(norm_targets) + 1}",
                        "raw_span": u_cand,
                        "clean_input": u_cand,
                        "platform": c_plat,
                        "text_role_hint": "coupon_candidate",
                    })


def _build_ai1_verbatim_corpus(
    merged_tip_ids: List[str],
    tip_map: Dict[str, Dict[str, Any]],
    orig_doc: Dict[str, Any],
) -> Tuple[str, Set[str]]:
    """构造单条 AI-1 任务允许引用的原始文本语料与精确链接/口令集合（严格隔离其它批内消息）。"""
    ordered_docs: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for tid in merged_tip_ids or []:
        tid_s = str(tid or "").strip()
        if tid_s and tid_s not in seen_ids and tid_s in (tip_map or {}):
            seen_ids.add(tid_s)
            ordered_docs.append(tip_map[tid_s])
    if not ordered_docs and orig_doc:
        ordered_docs.append(orig_doc)

    parts: List[str] = []
    exact_entries: Set[str] = set()
    for nd in ordered_docs:
        for field in ("title", "tutorial_text"):
            raw_v = str(nd.get(field) or "").strip()
            if raw_v:
                norm_v = normalize_inline_spaces(html_lib.unescape(raw_v)).strip()
                parts.append(raw_v)
                if norm_v != raw_v:
                    parts.append(norm_v)
        for lk in nd.get("extracted_links") or []:
            if isinstance(lk, dict):
                for k in ("url", "item_id"):
                    raw_u = str(lk.get(k) or "").strip()
                    if raw_u:
                        exact_entries.add(raw_u)
                        parts.append(raw_u)
                        norm_u = normalize_inline_spaces(strip_markdown_link_wrapper(raw_u))
                        if norm_u:
                            exact_entries.add(norm_u)
                            parts.append(norm_u)
        for tk in nd.get("extracted_tokens") or []:
            raw_t = str(tk or "").strip()
            if raw_t:
                exact_entries.add(raw_t)
                parts.append(raw_t)
                norm_t = normalize_inline_spaces(strip_markdown_link_wrapper(raw_t))
                if norm_t:
                    exact_entries.add(norm_t)
                    parts.append(norm_t)
    return "\n".join(parts), exact_entries


def _is_target_in_raw_corpus(clean_input: str, corpus: str, exact_entries: Set[str]) -> bool:
    """校验 AI-1 提取出的链接/口令是否完整、逐字存在于本条原始数据中，拦截字符幻觉或跨线报拼接。"""
    cand = normalize_inline_spaces(strip_markdown_link_wrapper(str(clean_input or ""))).strip()
    if not cand:
        return False
    if cand in exact_entries:
        return True

    if cand.lower().startswith(("http://", "https://")):
        scheme_idx = cand.find("://")
        candidates = [cand]
        if scheme_idx > 0 and len(cand[scheme_idx + 3 :]) >= 8:
            candidates.append(cand[scheme_idx + 3 :])
        for needle in candidates:
            start = 0
            while True:
                idx = corpus.find(needle, start)
                if idx < 0:
                    break
                end_idx = idx + len(needle)
                next_ch = corpus[end_idx] if end_idx < len(corpus) else ""
                if not next_ch or not re.match(r"[A-Za-z0-9_%&=+/?#.-]", next_ch):
                    return True
                start = idx + 1
        return False

    if re.fullmatch(r"[A-Za-z0-9_-]+", cand):
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(cand)}(?![A-Za-z0-9_-])", corpus):
            return True
    elif cand in corpus:
        return True

    core = cand.strip(_TB_WRAP_CHARS + " \t")
    if len(core) == 11 and core.isalnum() and any(c.isalpha() for c in core) and any(c.isdigit() for c in core):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(core)}(?![A-Za-z0-9])", corpus):
            return True
    return False


def _find_unverified_ai1_targets(
    norm_targets: List[Dict[str, Any]],
    merged_tip_ids: List[str],
    tip_map: Dict[str, Dict[str, Any]],
    orig_doc: Dict[str, Any],
) -> List[str]:
    corpus, exact_entries = _build_ai1_verbatim_corpus(merged_tip_ids, tip_map, orig_doc)
    return [
        t["clean_input"]
        for t in norm_targets
        if not _is_target_in_raw_corpus(t.get("clean_input", ""), corpus, exact_entries)
    ]


def _normalize_ai1_item(
    mid: str,
    ai1_res: Dict[str, Any],
    tip_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """规范化单条 Stage 1 模型输出，返回规范后的 `norm_targets` 并就地更新结构化字段。"""
    ai1_res["_from_fallback"] = False

    raw_ts = ai1_res.get("tutorial_score")
    ai1_res["tutorial_score"] = (
        int(raw_ts)
        if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) and 0 <= raw_ts <= 100
        else 50
    )

    raw_dp = ai1_res.get("deal_price")
    if isinstance(raw_dp, (int, float)) and not isinstance(raw_dp, bool) and 0.1 <= raw_dp <= 100000:
        ai1_res["deal_price"] = round(float(raw_dp), 2)
    elif isinstance(raw_dp, str):
        try:
            val = float(re.sub(r"[^\d.]", "", raw_dp))
            ai1_res["deal_price"] = round(val, 2) if 0.1 <= val <= 100000 else None
        except Exception:
            ai1_res["deal_price"] = None
    else:
        ai1_res["deal_price"] = None

    norm_targets: List[Dict[str, Any]] = []
    for idx_t, tgt in enumerate(ai1_res.get("extracted_targets") or [], 1):
        if not isinstance(tgt, dict):
            continue
        c_inp = strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
        if not c_inp:
            continue
        raw_role = str(tgt.get("text_role_hint") or "main_candidate").strip().lower()
        tgt["link_id"] = str(tgt.get("link_id") or f"L{idx_t}").strip()
        tgt["raw_span"] = normalize_inline_spaces(str(tgt.get("raw_span") or "")).strip()
        tgt["clean_input"] = c_inp
        tgt["text_role_hint"] = ROLE_HINT_NORMALIZE_MAP.get(raw_role, "main_candidate")
        norm_targets.append(tgt)

    if not ai1_res.get("merged_tip_ids"):
        ai1_res["merged_tip_ids"] = [mid]
    else:
        normalized_mids = [
            (str(x).split(":", 1)[-1].strip() if ":" in str(x) else str(x).strip())
            for x in ai1_res["merged_tip_ids"]
            if str(x or "").strip()
        ]
        if tip_map:
            normalized_mids = [tid for tid in normalized_mids if tid in tip_map]
        ai1_res["merged_tip_ids"] = normalized_mids or [mid]

    return norm_targets


def run_ai_stage1_batch(
    cleaner: Any,
    stage1_inputs: List[Dict[str, Any]],
    docs_by_tip_id: Dict[str, Dict[str, Any]],
    neighbor_maps_by_id: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    quiet_sub_logs: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """并发执行 Stage 1 (AI-1) 批处理，逐条核验链接逐字保真并由代码确定性拼接正文与图片。"""
    results_by_id: Dict[str, Dict[str, Any]] = {}
    total_items = len(stage1_inputs)
    chunks = [stage1_inputs[i : i + cleaner.ai_batch_size] for i in range(0, total_items, cleaner.ai_batch_size)]
    total_batches = len(chunks)
    prompt_tpl = cleaner._get_stage1_prompt()

    def _worker(chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cleaner.stats_ai1_calls += 1
        return cleaner._call_ai_json_array(prompt_tpl, chunk, "AI-Stage1")

    def _normalize_mid(raw_mid: Any) -> str:
        m = str(raw_mid or "").strip()
        if m and ":" in m and m not in docs_by_tip_id:
            suffix = m.split(":", 1)[-1].strip()
            if suffix in docs_by_tip_id:
                return suffix
        return m

    def _ingest_arr(arr: List[Dict[str, Any]]) -> None:
        for item in arr:
            if not isinstance(item, dict):
                continue
            mid = _normalize_mid(item.get("main_tip_id") or item.get("task_id") or "")
            if mid:
                item["main_tip_id"] = mid
                results_by_id[mid] = item

    if chunks:
        t_s1 = time.perf_counter()
        if total_batches == 1:
            try:
                _ingest_arr(_worker(chunks[0]))
            except Exception as e:
                logger.warning("[AI-Stage1] 单批次执行异常 (留待重试): %s", str(e).splitlines()[0][:200])
        else:
            done_batches = 0
            done_items = 0
            step_ai = max(2, total_batches // 4)
            ai_worker_count = min(cleaner.ai_max_workers, total_batches)
            with ThreadPoolExecutor(max_workers=ai_worker_count) as pool:
                futures = {pool.submit(_worker, ch): ch for ch in chunks}
                for fut in as_completed(futures):
                    ch = futures[fut]
                    done_batches += 1
                    done_items += len(ch)
                    try:
                        _ingest_arr(fut.result())
                    except Exception as e:
                        logger.warning(
                            "🚨 [AI-1 并发异常告警] 子批次(%d条)调用失败 (将留待重试) | 错误: %s | 并发=%d",
                            len(ch),
                            str(e).splitlines()[0][:250],
                            cleaner.ai_max_workers,
                        )
                    if not quiet_sub_logs and (done_batches % step_ai == 0 or done_batches == total_batches):
                        elapsed_ai = time.perf_counter() - t_s1
                        logger.info(
                            "  📊 [AI-1 拆解子进度 %02d/%02d组 (%3d%%)] 已完成=%3d/%3d条 | 耗时=%.1fs | 速度=%.2f条/s | 并发=%d",
                            done_batches,
                            total_batches,
                            int(done_batches * 100 / max(1, total_batches)),
                            done_items,
                            total_items,
                            elapsed_ai,
                            done_items / max(0.1, elapsed_ai),
                            ai_worker_count,
                        )

    for inp in stage1_inputs:
        mid = str(inp.get("main_tip_id") or "")
        orig_doc = docs_by_tip_id.get(mid) or {}
        ai1_res = results_by_id.get(mid)
        if not isinstance(ai1_res, dict) or "extracted_targets" not in ai1_res:
            cleaner.stats_ai1_fallbacks += 1
            results_by_id[mid] = fallback_stage1_extract(orig_doc)
            continue

        cleaner.stats_ai1_model_hits += 1
        tip_map = (neighbor_maps_by_id or {}).get(mid) or ({mid: orig_doc} if orig_doc else {})
        norm_targets = _normalize_ai1_item(mid, ai1_res, tip_map)

        unverified = _find_unverified_ai1_targets(norm_targets, ai1_res["merged_tip_ids"], tip_map, orig_doc)
        if unverified:
            logger.warning(
                "🚨 [AI-1 链接非原文告警] %s#%s 提取到非原文链接/口令 %s，立即触发单条隔离重提",
                str(orig_doc.get("source_site") or ""),
                mid,
                [u[:80] for u in unverified],
            )
            rescued = False
            try:
                retry_arr = _worker([inp]) or []
                cand = next(
                    (
                        item
                        for item in retry_arr
                        if isinstance(item, dict)
                        and (
                            _normalize_mid(item.get("main_tip_id") or item.get("task_id") or "") == mid
                            or (len(retry_arr) == 1 and not (item.get("main_tip_id") or item.get("task_id")))
                        )
                    ),
                    None,
                )
                if isinstance(cand, dict) and "extracted_targets" in cand:
                    cand["main_tip_id"] = mid
                    cand_targets = _normalize_ai1_item(mid, cand, tip_map)
                    cand_unverified = _find_unverified_ai1_targets(cand_targets, cand["merged_tip_ids"], tip_map, orig_doc)
                    if not cand_unverified:
                        ai1_res = cand
                        norm_targets = cand_targets
                        results_by_id[mid] = ai1_res
                        rescued = True
                        logger.info("✅ [AI-1 单条隔离重提成功] %s#%s 链接已通过原文逐字校验", str(orig_doc.get("source_site") or ""), mid)
                    else:
                        logger.warning(
                            "🚨 [AI-1 单条隔离重提仍未通过校验] %s#%s -> %s",
                            str(orig_doc.get("source_site") or ""),
                            mid,
                            [u[:80] for u in cand_unverified],
                        )
            except Exception as exc:
                logger.warning("[AI-1 单条隔离重提异常] %s#%s: %s", str(orig_doc.get("source_site") or ""), mid, str(exc)[:160])

            if not rescued:
                cleaner.stats_ai1_fallbacks += 1
                results_by_id[mid] = fallback_stage1_extract(orig_doc, fail_reason="AI1_HALLUCINATED_LINK")
                continue

        _supplement_structured_links(norm_targets, ai1_res["merged_tip_ids"], tip_map, orig_doc)

        ai1_res["extracted_targets"] = norm_targets
        raw_strips = ai1_res.get("strip_spans")
        ai1_res["strip_spans"] = (
            [normalize_inline_spaces(str(x)).strip() for x in raw_strips if str(x or "").strip()]
            if isinstance(raw_strips, list)
            else []
        )
        assembled_text, assembled_imgs = assemble_combined_text_and_images(
            ai1_res["merged_tip_ids"],
            norm_targets,
            tip_map,
            orig_doc,
        )
        ai1_res["combined_raw_text"] = assembled_text
        ai1_res["extracted_images"] = assembled_imgs

    return results_by_id


def _build_official_product_payload(
    try_plat: str,
    product_card: Dict[str, Any],
    res: Dict[str, Any],
    promo_link: str,
) -> Dict[str, Any]:
    """构建自包含但剥离 raw_item 的正式商品字段模板，保证多进程 Stage 2/3 守护模式与串行模式字段完全一致。"""
    official_price = float(product_card.get("price") or res.get("price") or 0.0)
    orig_price = float(product_card.get("original_price") or official_price)
    act_price = float(product_card.get("activity_price") or official_price)
    cpn_amt = float(product_card.get("coupon_amount") or res.get("coupon_amount") or 0.0)
    return {
        "title": str(product_card.get("name") or res.get("title") or "").strip(),
        "price": round(official_price, 2),
        "original_price": round(orig_price, 2),
        "activity_price": round(act_price, 2),
        "coupon_amount": round(cpn_amt, 2),
        "shop_name": str(product_card.get("mall_name") or "").strip(),
        "commission_rate": float(product_card.get("commission_rate") or res.get("commission_rate") or 0.0),
        "commission_amount": float(product_card.get("commission_amount") or res.get("commission_amount") or 0.0),
        "image_url": str(product_card.get("image_url") or res.get("image_url") or res.get("pict_url") or "").strip(),
        "carousel_images": list(product_card.get("carousel_images") or res.get("carousel_images") or []),
        "category": str(product_card.get("category") or product_card.get("category_name") or ""),
        "brand": str(product_card.get("brand") or product_card.get("brand_name") or ""),
        "sales_tip": str(product_card.get("sales_tip") if product_card.get("sales_tip") is not None else "0"),
        "platform_name": str(product_card.get("platform_name") or platform_name(try_plat)),
        "product_url": str(product_card.get("product_url") or promo_link or ""),
        "desc": str(product_card.get("desc") or ""),
        "detail_tags": list(product_card.get("detail_tags") or []),
        "service_guarantees": list(product_card.get("service_guarantees") or []),
        "shop_ratings": dict(product_card.get("shop_ratings") or {}),
        "specs": list(product_card.get("specs") or []),
        "coupon_info": dict(product_card.get("coupon_info") or {}),
    }


def _extract_product_card_from_conv(
    main_conv: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """从主商品转链记录恢复 product_card：优先读 adapter_res.product，守护进程跨阶段时回退读取自包含 official_product。"""
    adapter_res = main_conv.get("adapter_res") if isinstance(main_conv.get("adapter_res"), dict) else {}
    product_card = copy.deepcopy(adapter_res.get("product") if isinstance(adapter_res.get("product"), dict) else {})
    off_prod = main_conv.get("official_product") if isinstance(main_conv.get("official_product"), dict) else {}
    if not product_card and off_prod:
        product_card = {
            "name": off_prod.get("title") or "",
            "price": off_prod.get("price") or 0.0,
            "original_price": off_prod.get("original_price") or off_prod.get("price") or 0.0,
            "activity_price": off_prod.get("activity_price") or off_prod.get("price") or 0.0,
            "coupon_amount": off_prod.get("coupon_amount") or 0.0,
            "mall_name": off_prod.get("shop_name") or "",
            "commission_rate": float(off_prod.get("commission_rate") or 0.0),
            "commission_amount": float(off_prod.get("commission_amount") or 0.0),
            "image_url": str(off_prod.get("image_url") or "").strip(),
            "carousel_images": list(off_prod.get("carousel_images") or []),
            "category": str(off_prod.get("category") or ""),
            "brand": str(off_prod.get("brand") or ""),
            "sales_tip": str(off_prod.get("sales_tip") if off_prod.get("sales_tip") is not None else "0"),
            "platform_name": str(off_prod.get("platform_name") or ""),
            "product_url": str(off_prod.get("product_url") or ""),
            "desc": str(off_prod.get("desc") or ""),
            "detail_tags": list(off_prod.get("detail_tags") or []),
            "service_guarantees": list(off_prod.get("service_guarantees") or []),
            "shop_ratings": dict(off_prod.get("shop_ratings") or {}),
            "specs": list(off_prod.get("specs") or []),
            "coupon_info": dict(off_prod.get("coupon_info") or {}),
        }
    return product_card, adapter_res


def _attach_preseeded_default_promotion(
    *,
    plat: str,
    goods_id: str,
    goods_sign: str,
    main_conv: Dict[str, Any],
    adapter_res: Dict[str, Any],
    my_promo_link: str,
    my_promo_token: str,
    product_doc: Dict[str, Any],
) -> None:
    """将阶段二产出的站长公共推广链接按统一 3 天 TTL 预置到 formal product_doc，实现首个匿名点击 0 上游命中。"""
    raw_promo_payload = main_conv.get("promo_payload") if isinstance(main_conv.get("promo_payload"), dict) else {}
    promo_meta: Dict[str, Any] = {
        **raw_promo_payload,
        **{k: v for k, v in adapter_res.items() if k != "product" and v is not None},
    }
    raw_pid = str(promo_meta.get("promoter_id") or "").strip()
    final_link_val = str(
        promo_meta.get("final_link")
        or promo_meta.get("short_url")
        or promo_meta.get("url")
        or my_promo_link
        or ""
    ).strip()

    if (
        not final_link_val
        or promo_meta.get("is_fallback")
        or promo_meta.get("cacheable") is False
    ):
        return

    now_ts = time.time()
    default_exp_ts = now_ts + PROMOTION_LINK_TTL
    exp_ts = default_exp_ts
    raw_exp = promo_meta.get("expires_at")
    has_valid_upstream_exp = (
        isinstance(raw_exp, (int, float))
        and not isinstance(raw_exp, bool)
        and math.isfinite(raw_exp)
        and float(raw_exp) > now_ts
    )
    if has_valid_upstream_exp:
        exp_ts = min(default_exp_ts, float(raw_exp))

    now_utc_dt = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc)
    exp_utc_dt = datetime.datetime.fromtimestamp(math.floor(exp_ts * 1000) / 1000, datetime.timezone.utc)

    token_val = str(promo_meta.get("promotion_token") or my_promo_token or "")
    preseeded_promo: Dict[str, Any] = {
        "status": "success",
        "platform": plat,
        "goods_id": goods_id,
        "goods_sign": goods_sign,
        "url": str(promo_meta.get("url") or final_link_val),
        "final_link": final_link_val,
        "mobile_url": str(promo_meta.get("mobile_url") or promo_meta.get("url") or final_link_val),
        "short_url": str(promo_meta.get("short_url") or ""),
        "promotion_token": token_val,
        "token_type": str(promo_meta.get("token_type") or ("tpwd" if (plat == "taobao" and token_val) else "")),
        "we_app_info": dict(promo_meta.get("we_app_info") or {}) if isinstance(promo_meta.get("we_app_info"), dict) else {},
        "attribution_status": "anonymous",
        "need_auth": False,
        "is_fallback": False,
        "cacheable": True,
        "title": product_doc["name"],
        "price": product_doc["price"],
        "coupon_amount": product_doc["coupon_amount"],
    }
    if promo_meta.get("link_type"):
        preseeded_promo["link_type"] = str(promo_meta["link_type"])
    if promo_meta.get("schema_url"):
        preseeded_promo["schema_url"] = str(promo_meta["schema_url"])
    if promo_meta.get("coupon_url"):
        preseeded_promo["coupon_url"] = str(promo_meta["coupon_url"])
    if raw_pid:
        preseeded_promo["promoter_id"] = raw_pid
    if has_valid_upstream_exp:
        preseeded_promo["expires_at"] = float(raw_exp)

    product_doc["default_promotion"] = preseeded_promo
    product_doc["default_promotion_updated_at"] = now_utc_dt
    product_doc["default_promotion_expires_at"] = exp_utc_dt


def convert_single_target(
    cleaner: Any,
    target: Dict[str, Any],
    default_plat: str = "unknown",
) -> Dict[str, Any]:
    """对单个链接/口令执行官方转链、底牌四态校验（product / degraded_product / coupon_or_venue / error）与通道 2 安全解包。"""
    link_id = str(target.get("link_id") or "L1").strip()
    clean_input = strip_markdown_link_wrapper(str(target.get("clean_input") or ""))
    role_hint = str(target.get("text_role_hint") or "main_candidate").strip()
    hint_plat = str(target.get("platform") or default_plat or "unknown").strip().lower()
    plat = detect_candidate_platform(clean_input, hint_plat if hint_plat in ("jd", "taobao", "pdd") else default_plat)

    if not clean_input:
        return {
            "link_id": link_id,
            "clean_input": clean_input,
            "text_role_hint": role_hint,
            "convert_status": "failed",
            "platform": plat,
            "error_code": ERR_NO_ANCHOR,
            "error_msg": "空链接或口令",
            "official_product": None,
            "my_promo_link": None,
            "my_promo_token": None,
            "adapter_res": {},
        }

    if plat == "unsupported_other":
        return {
            "link_id": link_id,
            "clean_input": clean_input,
            "text_role_hint": role_hint,
            "convert_status": "failed",
            "platform": plat,
            "error_code": ERR_UNSUPPORTED_PLATFORM,
            "error_msg": f"非淘京拼支持平台外链: {clean_input[:80]}",
            "official_product": None,
            "my_promo_link": None,
            "my_promo_token": None,
            "adapter_res": {},
        }

    # 前置无佣金公共领券/会场直出
    pre_direct_url = _precheck_direct_non_cps_venue_url(clean_input)
    if pre_direct_url:
        direct_plat = detect_candidate_platform(pre_direct_url, plat if plat in ("jd", "taobao", "pdd") else "unknown")
        direct_keys = AnchorConversionCache.build_keys(direct_plat, clean_input)
        with cleaner._cache_lock:
            hit_direct = next((k for k in direct_keys if k in cleaner.anchor_cache), None)
            if hit_direct is not None:
                entry = cleaner._get_anchor_cache_locked(hit_direct)
                if entry is not None and entry.get("convert_status") == "coupon_or_activity":
                    cached = copy.deepcopy(entry)
                    cached["link_id"] = link_id
                    cached["text_role_hint"] = role_hint
                    cleaner.stats_cache_hits += 1
                    return cached
        out = {
            "link_id": link_id,
            "clean_input": clean_input,
            "text_role_hint": role_hint,
            "convert_status": "coupon_or_activity",
            "resolution_kind": ResolutionKind.COUPON_OR_VENUE,
            "platform": direct_plat,
            "goods_id": "",
            "goods_sign": "",
            "error_code": "OK_DIRECT_COUPON",
            "error_msg": "",
            "official_product": None,
            "my_promo_link": pre_direct_url,
            "my_promo_token": None,
            "adapter_res": {},
        }
        with cleaner._cache_lock:
            for ck in direct_keys:
                cleaner._set_anchor_cache_locked(ck, out)
        return copy.deepcopy(out)

    platforms_to_try = [plat] if plat in ("jd", "taobao", "pdd") else ["taobao", "jd", "pdd"]
    last_failed: Optional[Dict[str, Any]] = None
    pending_failed_cache: List[Tuple[List[str], Dict[str, Any]]] = []
    had_cache_miss = False
    any_detail_lookup_failed = False

    is_external_http_shortlink = (
        clean_input.lower().startswith(("http://", "https://"))
        and not _is_official_ecommerce_http_url(clean_input)
    )

    for try_plat in platforms_to_try:
        cache_keys = AnchorConversionCache.build_keys(try_plat, clean_input)
        with cleaner._cache_lock:
            hit_key = next((k for k in cache_keys if k in cleaner.anchor_cache), None)
            if hit_key is not None:
                entry = cleaner._get_anchor_cache_locked(hit_key) or {}
                cached = copy.deepcopy(entry)
                cached["link_id"] = link_id
                cached["text_role_hint"] = role_hint
                c_status = cached.get("convert_status")
                if c_status == "coupon_or_activity":
                    cleaner.stats_cache_hits += 1
                    return cached
                if c_status == "product":
                    c_ares = cached.get("adapter_res")
                    if isinstance(c_ares, dict) and isinstance(c_ares.get("product"), dict) and c_ares.get("product"):
                        cleaner.stats_cache_hits += 1
                        return cached
                else:
                    cleaner.stats_cache_hits += 1
                    last_failed = cached
                    continue

        if is_external_http_shortlink:
            had_cache_miss = True
            out = {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "resolution_kind": ResolutionKind.ERROR,
                "platform": try_plat,
                "goods_id": "",
                "goods_sign": "",
                "error_code": ERR_UNSUPPORTED_PLATFORM,
                "error_msg": "第三方短链未命中官方公共领券白名单",
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }
            for p_tag in platforms_to_try:
                pending_failed_cache.append((AnchorConversionCache.build_keys(p_tag, clean_input), out))
            last_failed = out
            break

        adapter = cleaner.adapters.get(try_plat)
        if not adapter:
            continue
        had_cache_miss = True

        try:
            with api_caller_context("background"):
                res = adapter.resolve_and_convert_universal(clean_input)
        except Exception as exc:
            out = {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "resolution_kind": ResolutionKind.ERROR,
                "platform": try_plat,
                "error_code": ERR_ADAPTER_CONVERT_FAILED,
                "error_msg": f"Adapter({try_plat})调用异常: {exc}",
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }
            pending_failed_cache.append((cache_keys, out))
            last_failed = out
            continue

        is_degraded_product = bool(
            isinstance(res, dict)
            and (
                res.get("detail_lookup_failed")
                or res.get("resolution_kind") == ResolutionKind.DEGRADED_PRODUCT
            )
        )
        is_catalog_cooling = bool(isinstance(res, dict) and res.get("catalog_cooling") is True)
        if is_degraded_product or is_catalog_cooling:
            any_detail_lookup_failed = True

        if isinstance(res, dict) and res.get("status") == "success":
            goods_id = str(res.get("goods_id") or "").strip()
            goods_sign = str(res.get("goods_sign") or "").strip()
            product_card = res.get("product")
            promo_link = str(res.get("final_link") or res.get("url") or "").strip()
            promo_token = str(res.get("promotion_token") or "").strip()

            if goods_id and goods_sign and isinstance(product_card, dict) and (promo_link or promo_token):
                out = {
                    "link_id": link_id,
                    "clean_input": clean_input,
                    "text_role_hint": role_hint,
                    "convert_status": "product",
                    "resolution_kind": ResolutionKind.PRODUCT,
                    "platform": try_plat,
                    "goods_id": goods_id,
                    "goods_sign": goods_sign,
                    "error_code": "OK",
                    "error_msg": "",
                    "official_product": _build_official_product_payload(try_plat, product_card, res, promo_link),
                    "my_promo_link": promo_link or None,
                    "my_promo_token": promo_token or None,
                    "promo_payload": AnchorConversionCache.extract_promo_payload(res),
                    "adapter_res": cleaner._slim_adapter_res_for_cache(res, keep_product=True),
                }
                with cleaner._cache_lock:
                    for ck in cache_keys:
                        cleaner._set_anchor_cache_locked(ck, out)
                return copy.deepcopy(out)

            # 通道 1（Fail-Closed 正向实证）：仅当上游明确标定 resolution_kind=='coupon_or_venue' 且产出新链/新口令时才认领 OK_COUPON
            if not is_degraded_product:
                is_verified_venue = res.get("resolution_kind") == ResolutionKind.COUPON_OR_VENUE
                is_new_station_link = _is_verified_station_promo_link(clean_input, promo_link)
                is_new_token = bool(promo_token) and promo_token != clean_input
                rebuilt_promo = rebuild_clean_official_venue_url(promo_link) if promo_link else None
                if is_verified_venue and (is_new_station_link or is_new_token):
                    out = {
                        "link_id": link_id,
                        "clean_input": clean_input,
                        "text_role_hint": role_hint,
                        "convert_status": "coupon_or_activity",
                        "resolution_kind": ResolutionKind.COUPON_OR_VENUE,
                        "platform": try_plat,
                        "goods_id": "",
                        "goods_sign": "",
                        "error_code": "OK_COUPON",
                        "error_msg": "",
                        "official_product": None,
                        "my_promo_link": promo_link if is_new_station_link else rebuilt_promo,
                        "my_promo_token": promo_token if is_new_token else None,
                        "adapter_res": {},
                    }
                    with cleaner._cache_lock:
                        for ck in cache_keys:
                            cleaner._set_anchor_cache_locked(ck, out)
                    return copy.deepcopy(out)
                if rebuilt_promo:
                    venue_plat = detect_candidate_platform(rebuilt_promo, try_plat)
                    out = {
                        "link_id": link_id,
                        "clean_input": clean_input,
                        "text_role_hint": role_hint,
                        "convert_status": "coupon_or_activity",
                        "resolution_kind": ResolutionKind.COUPON_OR_VENUE,
                        "platform": venue_plat,
                        "goods_id": "",
                        "goods_sign": "",
                        "error_code": "OK_DIRECT_COUPON",
                        "error_msg": "",
                        "official_product": None,
                        "my_promo_link": rebuilt_promo,
                        "my_promo_token": None,
                        "adapter_res": {},
                    }
                    with cleaner._cache_lock:
                        for p_tag in {try_plat, venue_plat}:
                            for ck in AnchorConversionCache.build_keys(p_tag, clean_input):
                                cleaner._set_anchor_cache_locked(ck, out)
                    return copy.deepcopy(out)

        raw_code = str((res or {}).get("error_code") or "")
        raw_msg = (
            "商品详情反查触发临时限流或网络波动"
            if is_degraded_product
            else str((res or {}).get("error_msg") or "转链未返回有效结果")
        )
        if raw_code == "UNSUPPORTED_TB_NUMERIC_ID" or (try_plat == "taobao" and "不支持数字id" in raw_msg):
            err_code = ERR_TB_NUMERIC_ID
        elif raw_code == "UNSUPPORTED_PDD_ID_ONLY_URL" or (try_plat == "pdd" and "40009" in raw_msg):
            err_code = ERR_PDD_NUMERIC_ID
        elif raw_code == "UNSUPPORTED_JD_NUMERIC_SKU" or (try_plat == "jd" and "不支持数字id" in raw_msg):
            err_code = ERR_JD_NUMERIC_SKU
        elif raw_code == "CONFIG_ERROR":
            err_code = ERR_ADAPTER_CONFIG_ERROR
            logger.error("[Stage3配置缺失] 平台=%s 返回 CONFIG_ERROR: %s", try_plat, raw_msg)
        elif not is_degraded_product and (
            raw_code == "PRODUCT_OFF_PROMOTION"
            or any(k in raw_msg for k in ("下架", "不在推广", "取消推广", "不支持该商品"))
        ):
            err_code = ERR_ADAPTER_OFF_PROMOTION
        else:
            err_code = ERR_ADAPTER_CONVERT_FAILED

        out = {
            "link_id": link_id,
            "clean_input": clean_input,
            "text_role_hint": role_hint,
            "convert_status": "failed",
            "resolution_kind": ResolutionKind.DEGRADED_PRODUCT if is_degraded_product else ResolutionKind.ERROR,
            "platform": try_plat,
            "goods_id": "",
            "goods_sign": "",
            "error_code": err_code,
            "error_msg": raw_msg,
            "official_product": None,
            "my_promo_link": None,
            "my_promo_token": None,
            "adapter_res": {},
            **({"catalog_cooling": True} if is_catalog_cooling else {}),
        }
        if err_code != ERR_ADAPTER_CONFIG_ERROR:
            pending_failed_cache.append((cache_keys, out))
        last_failed = out

    # 通道 2：安全重定向解包 -> 官方无佣金公共领券/会场白名单（非详情限流场景下执行）
    if had_cache_miss and not any_detail_lookup_failed:
        rebuilt_venue_url = rebuild_clean_official_venue_url(clean_input)
        if not rebuilt_venue_url and cleaner._can_run_live_unwrap():
            rebuilt_venue_url = unwrap_to_official_public_venue_url(
                clean_input,
                http_session=cleaner._get_unwrap_session(),
                resolver=cleaner.unwrap_resolver,
            )
        if rebuilt_venue_url:
            venue_plat = detect_candidate_platform(
                rebuilt_venue_url,
                plat if plat in ("jd", "taobao", "pdd") else "unknown",
            )
            out = {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "coupon_or_activity",
                "resolution_kind": ResolutionKind.COUPON_OR_VENUE,
                "platform": venue_plat,
                "goods_id": "",
                "goods_sign": "",
                "error_code": "OK_DIRECT_COUPON",
                "error_msg": "",
                "official_product": None,
                "my_promo_link": rebuilt_venue_url,
                "my_promo_token": None,
                "adapter_res": {},
            }
            with cleaner._cache_lock:
                for p_tag in set(platforms_to_try) | {venue_plat}:
                    if p_tag in ("jd", "taobao", "pdd"):
                        for ck in AnchorConversionCache.build_keys(p_tag, clean_input):
                            cleaner._set_anchor_cache_locked(ck, out)
            return copy.deepcopy(out)

    if pending_failed_cache:
        with cleaner._cache_lock:
            for cks, failed_out in pending_failed_cache:
                for ck in cks:
                    cleaner._set_anchor_cache_locked(ck, failed_out)

    return copy.deepcopy(last_failed) if last_failed else {
        "link_id": link_id,
        "clean_input": clean_input,
        "text_role_hint": role_hint,
        "convert_status": "failed",
        "platform": plat,
        "goods_id": "",
        "goods_sign": "",
        "error_code": ERR_ADAPTER_CONVERT_FAILED,
        "error_msg": "未能识别平台或转链失败",
        "official_product": None,
        "my_promo_link": None,
        "my_promo_token": None,
        "adapter_res": {},
    }


def run_stage3_concurrent_conversions(
    cleaner: Any,
    unique_targets: List[Tuple[Dict[str, Any], str]],
    quiet_sub_logs: bool = False,
) -> None:
    """并发执行 Stage 3 官方 API 转链，京东按 ZTK appkey 数量约束并发车道防自伤限流。"""
    if not unique_targets:
        return

    total_targets = len(unique_targets)
    step = max(20, total_targets // 5)
    t_s3 = time.perf_counter()
    done_cnt = 0
    prog_lock = threading.Lock()

    jd_targets: List[Tuple[Dict[str, Any], str]] = []
    other_targets: List[Tuple[Dict[str, Any], str]] = []
    for it in unique_targets:
        tgt, def_plat = it
        c_inp = strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
        hint_p = str(tgt.get("platform") or def_plat or "unknown").strip().lower()
        det_p = detect_candidate_platform(c_inp, hint_p if hint_p in ("jd", "taobao", "pdd") else def_plat)
        if det_p == "jd":
            jd_targets.append(it)
        else:
            other_targets.append(it)

    jd_adapter = cleaner.adapters.get("jd")
    ztk_key_count = len(getattr(getattr(jd_adapter, "client", None), "appkey_list", ()) or ())
    jd_lane_cap = min(max(1, ztk_key_count), 2) if ztk_key_count > 0 else 1
    jd_lane_count = min(cleaner.convert_max_workers, jd_lane_cap, len(jd_targets))
    other_slots = 0 if not other_targets else min(max(1, cleaner.convert_max_workers - jd_lane_count), len(other_targets))
    worker_slots = max(1, other_slots + jd_lane_count)

    if not quiet_sub_logs:
        logger.info(
            "🔗 [Stage 3-官方转链启动] 独立目标=%d个 (JD=%d, 其它=%d) | JD车道=%d, 其它=%d, 上限=%d",
            total_targets,
            len(jd_targets),
            len(other_targets),
            jd_lane_count,
            other_slots,
            worker_slots,
        )

    def _record_done() -> None:
        nonlocal done_cnt
        with prog_lock:
            done_cnt += 1
            cur_done = done_cnt
        if not quiet_sub_logs and (cur_done % step == 0 or cur_done == total_targets):
            elapsed_s3 = time.perf_counter() - t_s3
            spd_s3 = cur_done / max(0.1, elapsed_s3)
            logger.info(
                "  📊 [Stage 3-官方转链 子进度 %3d/%3d个 (%3d%%)] 耗时=%.1fs | 速度=%.1f个/s | 并发=%d",
                cur_done,
                total_targets,
                int(cur_done * 100 / max(1, total_targets)),
                elapsed_s3,
                spd_s3,
                worker_slots,
            )

    def _conv_worker(item: Tuple[Dict[str, Any], str]) -> None:
        tgt, def_plat = item
        c_inp = strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
        try:
            res = cleaner.convert_single_target(tgt, default_plat=def_plat)
            err_msg = str((res or {}).get("error_msg") or "")
            err_code = str((res or {}).get("error_code") or "")
            if "商品详情反查" not in err_msg and any(
                k in err_msg for k in ("频繁", "频控", "429", "限流", "风控", "黑名单", "系统繁忙")
            ):
                logger.warning(
                    "🚨 [Stage 3 转链上游频控/风控预警] input=%s | code=%s | msg=%s | 并发=%d",
                    c_inp[:60],
                    err_code,
                    err_msg[:120],
                    cleaner.convert_max_workers,
                )
        except Exception as e:
            logger.warning(
                "🚨 [Stage 3 转链执行异常] input=%s | 异常: %s | 并发=%d",
                c_inp[:60],
                str(e)[:150],
                cleaner.convert_max_workers,
            )
        finally:
            _record_done()

    if total_targets == 1:
        _conv_worker(unique_targets[0])
        return

    jd_iter = iter(jd_targets)
    jd_iter_lock = threading.Lock()

    def _jd_lane_runner() -> None:
        while True:
            with jd_iter_lock:
                jd_it = next(jd_iter, None)
            if jd_it is None:
                break
            _conv_worker(jd_it)

    with ThreadPoolExecutor(max_workers=worker_slots) as pool:
        futures = [pool.submit(_jd_lane_runner) for _ in range(jd_lane_count)]
        futures.extend(pool.submit(_conv_worker, o_it) for o_it in other_targets)
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass


def run_ai_stage2_batch(
    cleaner: Any,
    stage2_inputs: List[Dict[str, Any]],
    quiet_sub_logs: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """并发执行 Stage 4（AI-2：底牌终审、教程重构与质量评分）。"""
    results_by_id: Dict[str, Dict[str, Any]] = {}
    if not stage2_inputs:
        return results_by_id

    total_items = len(stage2_inputs)
    bs2 = max(1, getattr(cleaner, "ai2_batch_size", 6))
    chunks = [stage2_inputs[i : i + bs2] for i in range(0, total_items, bs2)]
    total_batches = len(chunks)
    t_s4 = time.perf_counter()
    prompt_tpl = cleaner._get_stage2_prompt()

    def _worker(chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cleaner.stats_ai2_calls += 1
        return cleaner._call_ai_json_array(prompt_tpl, chunk, "AI-Stage2")

    def _ingest_arr(arr: List[Dict[str, Any]]) -> None:
        for item in arr:
            mid = str(item.get("main_tip_id") or item.get("task_id") or "").strip()
            if mid and ":" in mid:
                mid = mid.split(":", 1)[-1].strip()
            if mid:
                item["main_tip_id"] = mid
                results_by_id[mid] = item

    if total_batches == 1:
        try:
            _ingest_arr(_worker(chunks[0]))
        except Exception as e:
            logger.debug("[AI-Stage2] 批次偶发执行异常 (留待重试): %s", str(e).splitlines()[0][:200])
    else:
        done_batches = 0
        done_items = 0
        with ThreadPoolExecutor(max_workers=min(cleaner.ai_max_workers, total_batches)) as pool:
            futures = {pool.submit(_worker, ch): ch for ch in chunks}
            for fut in as_completed(futures):
                ch = futures[fut]
                done_batches += 1
                done_items += len(ch)
                try:
                    _ingest_arr(fut.result())
                except Exception as e:
                    logger.debug("[AI-Stage2] 并发批次偶发执行异常 (留待重试): %s", str(e).splitlines()[0][:200])
                if not quiet_sub_logs:
                    logger.debug(
                        "  📊 [阶段4-AI终审 子进度 %02d/%02d批 (%3d%%)] %d/%d | 耗时=%.1fs",
                        done_batches,
                        total_batches,
                        int(done_batches * 100 / max(1, total_batches)),
                        done_items,
                        total_items,
                        time.perf_counter() - t_s4,
                    )
    return results_by_id


def _jd_catalog_cooldown_retry_delay_sec(cleaner: Any) -> float:
    """读取 JD 客户端选品库剩余冷却时间并追加 10s 缓冲，确保下轮重领在冷却结束后执行。"""
    jd_adapter = (getattr(cleaner, "adapters", None) or {}).get("jd")
    rem_fn = getattr(getattr(jd_adapter, "client", None), "catalog_cooldown_remaining", None)
    remaining = 0.0
    if callable(rem_fn):
        try:
            remaining = max(0.0, float(rem_fn()))
        except Exception:
            remaining = 0.0
    return round(max(15.0, remaining + 10.0), 1)


def _build_retry_or_max_outcome(
    *,
    doc: Dict[str, Any],
    site: str,
    channel: str,
    mid: str,
    raw_title: str,
    ai1_res: Any,
    conv_summary: List[Dict[str, Any]],
    ai2_res: Any,
    stage: str,
    retry_err_code: str,
    max_err_code: str,
    retry_msg: str,
    max_msg: str,
    attempt_errors: Optional[List[Dict[str, str]]] = None,
    candidate_count: int = 0,
    consume_retry: bool = True,
    retry_delay_sec: Optional[float] = None,
) -> Dict[str, Any]:
    cur_retries = int(doc.get("clean_retry_count") or 0)
    new_retries = (cur_retries + 1) if consume_retry else cur_retries
    is_retry = True if not consume_retry else (new_retries < MAX_CLEAN_RETRIES)
    return {
        "status": "retry_pending" if is_retry else "failed",
        "stage_outcome": "retry_pending" if is_retry else "failed",
        **({"clean_stage": stage} if is_retry else {}),
        **({"retry_delay_sec": round(max(15.0, float(retry_delay_sec)), 1)} if (is_retry and retry_delay_sec is not None) else {}),
        "error_code": retry_err_code if is_retry else max_err_code,
        "error_msg": retry_msg.format(new_retries=new_retries, max_retries=MAX_CLEAN_RETRIES)
        if is_retry
        else max_msg.format(new_retries=new_retries, max_retries=MAX_CLEAN_RETRIES),
        "retry_count": new_retries,
        "source_site": site,
        "source_channel": channel,
        "source_item_id": mid,
        "title": raw_title,
        "ai_stage1_result": ai1_res,
        "converted_links": conv_summary,
        "ai_stage2_result": ai2_res,
        "quality_score": 0,
        "candidate_count": candidate_count,
        "attempt_errors": attempt_errors or [],
    }


def _build_early_terminated_outcome(
    *,
    early_stage: str,
    error_msg: str,
    site: str,
    site_name: str,
    channel: str,
    mid: str,
    raw_title: str,
    c_plat: str,
    c_gid: str,
    c_gsign: str,
    official_title: str,
    official_price: Any,
    deal_price: Any,
    quality_score: int,
    merged_tids: List[str],
    ai1_res: Dict[str, Any],
    converted_links: List[Dict[str, Any]],
    ai2_marker: Dict[str, Any],
    candidate_count: int,
    attempt_errors: List[Dict[str, str]],
    seen_count: int,
    seen_channels: List[str],
    clean_copy: str,
) -> Dict[str, Any]:
    return {
        "status": "early_terminated",
        "stage_outcome": "early_terminated",
        "early_stage": early_stage,
        "error_code": OK_SKIPPED_ALREADY_CLEANED,
        "error_msg": error_msg,
        "source_site": site,
        "source_site_name": site_name,
        "deal_source": site,
        "deal_source_name": site_name,
        "source_channel": channel,
        "source_item_id": mid,
        "platform": c_plat,
        "product_id": c_gid,
        "goods_sign": c_gsign,
        "official_title": official_title,
        "official_price": official_price,
        "deal_price": deal_price,
        "title": raw_title,
        "quality_score": quality_score,
        "merged_tip_ids": merged_tids,
        "ai_stage1_result": ai1_res,
        "converted_links": converted_links,
        "ai_stage2_result": ai2_marker,
        "candidate_count": candidate_count,
        "attempt_errors": attempt_errors,
        "resonance_update": {
            "platform": c_plat,
            "product_id": c_gid,
            "seen_count": seen_count,
            "source_site": site,
            "seen_channels": seen_channels,
            "copywriting": clean_copy,
        },
    }


def process_ai1_unit(
    cleaner: Any,
    selected_docs: List[Dict[str, Any]],
    raw_col,
    quiet_sub_logs: bool = False,
) -> Tuple[List[Dict[str, Any]], float, Dict[str, Dict[str, Dict[str, Any]]]]:
    """阶段一/AI-1 拆解单元：复用有效重试 AI-1、调用 AI-1 拆解、邻居合并吸收判定。"""
    if not selected_docs:
        return [], 0.0, {}

    t_ai1_start = time.perf_counter()

    docs_by_tip_id: Dict[str, Dict[str, Any]] = {}
    neighbor_maps_by_id: Dict[str, Dict[str, Dict[str, Any]]] = {}
    stage1_inputs: List[Dict[str, Any]] = []
    outcomes_by_id: Dict[str, Dict[str, Any]] = {}

    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        if not mid:
            continue
        docs_by_tip_id[mid] = doc

        saved_ai1 = doc.get("ai_stage1_result")
        if (
            doc.get("clean_error_code") in (ERR_ADAPTER_CONVERT_FAILED, ERR_ADAPTER_CONFIG_ERROR, ERR_AI_TIMEOUT_RETRY)
            and isinstance(saved_ai1, dict)
            and not saved_ai1.get("_from_fallback")
            and saved_ai1.get("is_standalone_deal") is not False
            and saved_ai1.get("extracted_targets")
        ):
            extra_mids = [
                str(tid or "").strip()
                for tid in (saved_ai1.get("merged_tip_ids") or [])
                if str(tid or "").strip() and str(tid or "").strip() != mid
            ]
            site = str(doc.get("source_site") or "")
            neighbor_maps_by_id[mid] = load_neighbor_docs(raw_col, site, extra_mids, current_doc=doc)
            continue

        s1_inp, tip_map = build_stage1_task_input(doc, raw_col)
        neighbor_maps_by_id[mid] = tip_map
        stage1_inputs.append(s1_inp)

    ai1_hits_before = cleaner.stats_ai1_model_hits
    if not quiet_sub_logs:
        n_chunks = (len(stage1_inputs) + cleaner.ai_batch_size - 1) // cleaner.ai_batch_size if stage1_inputs else 0
        logger.info(
            "🤖 [AI-1 拆解启动] 本批=%d条 (组=%d, 并发=%d)",
            len(stage1_inputs),
            n_chunks,
            min(cleaner.ai_max_workers, n_chunks) if n_chunks else 1,
        )
    ai1_results = (
        cleaner.run_ai_stage1_batch(
            stage1_inputs,
            docs_by_tip_id,
            neighbor_maps_by_id=neighbor_maps_by_id,
            quiet_sub_logs=quiet_sub_logs,
        )
        if stage1_inputs
        else {}
    )
    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        if mid and mid not in ai1_results and mid not in outcomes_by_id:
            saved_ai1 = doc.get("ai_stage1_result")
            if isinstance(saved_ai1, dict) and not saved_ai1.get("_from_fallback") and saved_ai1.get("extracted_targets"):
                ai1_results[mid] = copy.deepcopy(saved_ai1)
    t_ai1_elapsed = round(time.perf_counter() - t_ai1_start, 2)

    # 邻居碎片吸收判定
    absorbed_by_neighbor: Dict[str, str] = {}
    active_parent_ids: Set[str] = set()
    sorted_ai1_items = sorted(
        ai1_results.items(),
        key=lambda kv: (
            len((kv[1] or {}).get("extracted_targets") or []),
            len((kv[1] or {}).get("merged_tip_ids") or []),
        ),
        reverse=True,
    )
    for mid, ai1_res in sorted_ai1_items:
        if ai1_res.get("_from_fallback") or not ai1_res.get("is_standalone_deal", True) or mid in absorbed_by_neighbor:
            continue
        for m_tid in ai1_res.get("merged_tip_ids") or []:
            m_tid_str = str(m_tid or "").strip()
            if (
                m_tid_str
                and m_tid_str != mid
                and m_tid_str not in absorbed_by_neighbor
                and m_tid_str not in active_parent_ids
            ):
                absorbed_by_neighbor[m_tid_str] = mid
                active_parent_ids.add(mid)

    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        if not mid or mid in outcomes_by_id:
            continue
        site = str(doc.get("source_site") or "")
        site_name = get_source_site_name(site)
        channel = str(doc.get("source_channel") or "")
        raw_title = str(doc.get("title") or "").strip()
        ai1_res = ai1_results.get(mid) or {}

        if mid in absorbed_by_neighbor or ai1_res.get("is_standalone_deal") is False:
            parent_id = absorbed_by_neighbor.get(mid) or ""
            outcomes_by_id[mid] = {
                "status": "merged_into_parent",
                "stage_outcome": "merged_into_parent",
                "error_code": ERR_MERGED_INTO_NEIGHBOR,
                "error_msg": f"群聊连续碎片已合并入相邻主线报 {parent_id}".strip(),
                "merged_into_tip_id": parent_id,
                "source_site": site,
                "source_site_name": site_name,
                "deal_source": site,
                "deal_source_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "converted_links": [],
                "ai_stage2_result": None,
                "quality_score": 0,
                "candidate_count": 0,
                "attempt_errors": [],
            }
            continue

        if ai1_res.get("_from_fallback"):
            cleaner.stats_ai_retries_scheduled += 1
            outcomes_by_id[mid] = _build_retry_or_max_outcome(
                doc=doc,
                site=site,
                channel=channel,
                mid=mid,
                raw_title=raw_title,
                ai1_res=ai1_res,
                conv_summary=[],
                ai2_res=None,
                stage=CLEAN_STAGE_NEED_AI1,
                retry_err_code=ERR_AI_TIMEOUT_RETRY,
                max_err_code=ERR_AI_MAX_RETRIES_EXCEEDED,
                retry_msg="AI-1 超时或未返回有效拆解，跳过本轮并累加重试 ({new_retries}/{max_retries})",
                max_msg="AI-1 连续 {new_retries} 次超时或未返回结果，达最大重试上限({max_retries}次)，永久放弃",
            )
            continue

        targets = ai1_res.get("extracted_targets") or []
        if not targets:
            outcomes_by_id[mid] = {
                "status": "failed",
                "stage_outcome": "failed",
                "error_code": ERR_NO_ANCHOR,
                "error_msg": "Stage 1 AI 未在爆料文本中识别出任何淘/京/拼链接或口令",
                "source_site": site,
                "source_site_name": site_name,
                "deal_source": site,
                "deal_source_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "converted_links": [],
                "ai_stage2_result": None,
                "quality_score": 0,
                "candidate_count": 0,
                "attempt_errors": [],
            }
            continue

        outcomes_by_id[mid] = {
            "status": "in_progress",
            "stage_outcome": "advance_to_convert",
            "source_site": site,
            "source_channel": channel,
            "source_item_id": mid,
            "title": raw_title,
            "ai_stage1_result": ai1_res,
            "candidate_count": len(targets),
        }

    out_list = [
        outcomes_by_id[str(d.get("source_item_id") or "")]
        for d in selected_docs
        if str(d.get("source_item_id") or "") in outcomes_by_id
    ]
    if not quiet_sub_logs:
        adv_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "advance_to_convert")
        retry_cnt = sum(1 for o in out_list if o.get("status") == "retry_pending")
        fail_cnt = sum(1 for o in out_list if o.get("stage_outcome") == "failed")
        logger.info(
            "🤖 [AI-1 拆解完成] 耗时=%.1fs | 认领=%d条 (直出=%d, 重试=%d) -> 待转链=%d (无链=%d)",
            t_ai1_elapsed,
            len(selected_docs),
            cleaner.stats_ai1_model_hits - ai1_hits_before,
            retry_cnt,
            adv_cnt,
            fail_cnt,
        )
    return out_list, t_ai1_elapsed, neighbor_maps_by_id


def process_convert_unit(
    cleaner: Any,
    selected_docs: List[Dict[str, Any]],
    formal_col=None,
    shared_active_products: Any = None,
    shared_lock: Optional[threading.Lock] = None,
    quiet_sub_logs: bool = False,
) -> Tuple[List[Dict[str, Any]], float]:
    """阶段二/官方转链核验单元：去重并发转链、暂态降级重试、统一在库去重/择优升级。"""
    if not selected_docs:
        return [], 0.0

    t_stage3_start = time.perf_counter()

    total_target_mentions = 0
    unique_target_map: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]] = {}
    for doc in selected_docs:
        ai1_res = doc.get("ai_stage1_result") or {}
        if ai1_res.get("_from_fallback") or ai1_res.get("is_standalone_deal") is False:
            continue
        def_plat = str(doc.get("platform") or "unknown")
        for tgt in (ai1_res.get("extracted_targets") or [])[:8]:
            if not isinstance(tgt, dict) or str(tgt.get("text_role_hint") or "").strip() == "filler_candidate":
                continue
            c_inp = strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
            if not c_inp:
                continue
            total_target_mentions += 1
            hint_p = str(tgt.get("platform") or def_plat or "unknown").strip().lower()
            det_p = detect_candidate_platform(c_inp, hint_p if hint_p in ("jd", "taobao", "pdd") else def_plat)
            unique_target_map.setdefault((det_p, c_inp), (tgt, def_plat))

    if not quiet_sub_logs:
        logger.info(
            "🔗 [官方转链启动] 认领=%d条 (独立=%d, 引用=%d, 并发=%d)",
            len(selected_docs),
            len(unique_target_map),
            total_target_mentions,
            cleaner.convert_max_workers,
        )
    cleaner.run_stage3_concurrent_conversions(list(unique_target_map.values()), quiet_sub_logs=quiet_sub_logs)
    t_stage3_elapsed = round(time.perf_counter() - t_stage3_start, 2)

    outcomes_by_id: Dict[str, Dict[str, Any]] = {}
    surviving_candidates: List[
        Tuple[str, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, str]], Optional[Tuple[str, str]]]
    ] = []

    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        site = str(doc.get("source_site") or "")
        site_name = get_source_site_name(site)
        channel = str(doc.get("source_channel") or "")
        raw_title = str(doc.get("title") or "").strip()
        ai1_res = doc.get("ai_stage1_result") or {}
        targets = ai1_res.get("extracted_targets") or []

        conv_list: List[Dict[str, Any]] = []
        attempt_errors: List[Dict[str, str]] = []
        def_doc_plat = str(doc.get("platform") or "unknown")
        for tgt in targets[:8]:
            if not isinstance(tgt, dict):
                continue
            if str(tgt.get("text_role_hint") or "").strip() == "filler_candidate":
                c_inp_f = strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
                hint_pf = str(tgt.get("platform") or def_doc_plat).strip().lower()
                conv_list.append({
                    "link_id": str(tgt.get("link_id") or ""),
                    "raw_span": str(tgt.get("raw_span") or ""),
                    "clean_input": c_inp_f,
                    "text_role_hint": "filler_candidate",
                    "platform": detect_candidate_platform(c_inp_f, hint_pf if hint_pf in ("jd", "taobao", "pdd") else def_doc_plat),
                    "convert_status": "skipped_filler",
                    "error_code": "SKIPPED_FILLER_NO_CONVERT",
                    "error_msg": "凑单辅助品跳过转链",
                    "goods_id": "",
                    "goods_sign": "",
                    "my_promo_link": "",
                    "my_promo_token": "",
                    "official_product": None,
                    "adapter_res": None,
                })
                continue
            c_res = cleaner.convert_single_target(tgt, default_plat=def_doc_plat)
            conv_list.append(c_res)
            if c_res["convert_status"] == "failed":
                attempt_errors.append({
                    "link_id": c_res["link_id"],
                    "anchor": str(c_res.get("clean_input") or "")[:100],
                    "error_code": c_res["error_code"],
                    "error_msg": c_res["error_msg"],
                })

        prod_convs = [c for c in conv_list if c["convert_status"] == "product"]
        coupon_convs = [c for c in conv_list if c["convert_status"] == "coupon_or_activity"]
        has_skipped_filler = any(c.get("convert_status") == "skipped_filler" for c in conv_list)
        conv_summary_s3 = [{k: v for k, v in c.items() if k != "adapter_res"} for c in conv_list]

        if not prod_convs:
            transient_err = next(
                (e for e in attempt_errors if e["error_code"] in (ERR_ADAPTER_CONVERT_FAILED, ERR_ADAPTER_CONFIG_ERROR)),
                None,
            )
            if transient_err is not None:
                transient_failed_convs = [
                    c for c in conv_list
                    if c.get("convert_status") == "failed"
                    and c.get("error_code") in (ERR_ADAPTER_CONVERT_FAILED, ERR_ADAPTER_CONFIG_ERROR)
                ]
                is_local_cooling_only = bool(transient_failed_convs) and any(
                    c.get("catalog_cooling") is True for c in transient_failed_convs
                )
                cleaner.stats_ai_retries_scheduled += 1
                retry_msg_tpl = (
                    f"京东选品库处于本地冷却窗口，延后重试（不消耗重试次数 {{new_retries}}/{{max_retries}}）: {transient_err['error_msg']}"
                    if is_local_cooling_only
                    else f"官方转链或反查暂态失败，保留待下轮重试 ({{new_retries}}/{{max_retries}}): {transient_err['error_msg']}"
                )
                outcomes_by_id[mid] = _build_retry_or_max_outcome(
                    doc=doc,
                    site=site,
                    channel=channel,
                    mid=mid,
                    raw_title=raw_title,
                    ai1_res=ai1_res,
                    conv_summary=conv_summary_s3,
                    ai2_res=None,
                    stage=CLEAN_STAGE_NEED_CONVERT,
                    retry_err_code=transient_err["error_code"],
                    max_err_code=transient_err["error_code"],
                    retry_msg=retry_msg_tpl,
                    max_msg=f"官方转链连续 {{new_retries}} 轮失败，达最大重试上限({{max_retries}}次)放弃: {transient_err['error_msg']}",
                    attempt_errors=attempt_errors,
                    candidate_count=len(targets),
                    consume_retry=not is_local_cooling_only,
                    retry_delay_sec=_jd_catalog_cooldown_retry_delay_sec(cleaner) if is_local_cooling_only else None,
                )
                continue

            if has_skipped_filler:
                primary_err_code = ERR_AI2_MAIN_DEAD_ONLY_FILLER
                primary_err_msg = "主商品未存活或仅含凑单辅助品，提前终止拦截"
            elif coupon_convs and not attempt_errors:
                primary_err_code = ERR_EMPTY_PRODUCT_IDENTITY
                primary_err_msg = "仅包含优惠券或活动会场链接，无具体主商品"
            else:
                primary_err_code = attempt_errors[0]["error_code"] if attempt_errors else ERR_ADAPTER_CONVERT_FAILED
                primary_err_msg = attempt_errors[0]["error_msg"] if attempt_errors else "所有候选链接均转链失败"
            outcomes_by_id[mid] = {
                "status": "failed",
                "stage_outcome": "failed",
                "error_code": primary_err_code,
                "error_msg": primary_err_msg,
                "source_site": site,
                "source_site_name": site_name,
                "deal_source": site,
                "deal_source_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_s3,
                "ai_stage2_result": None,
                "quality_score": 0,
                "candidate_count": len(targets),
                "attempt_errors": attempt_errors,
            }
            continue

        main_prod_convs = [c for c in prod_convs if c.get("text_role_hint") != "filler_candidate"]
        if not main_prod_convs:
            transient_main_convs = [
                c
                for c in conv_list
                if c["convert_status"] == "failed"
                and c["error_code"] in (ERR_ADAPTER_CONVERT_FAILED, ERR_ADAPTER_CONFIG_ERROR)
                and c.get("text_role_hint") != "filler_candidate"
            ]
            transient_main_err = transient_main_convs[0] if transient_main_convs else None
            is_local_cooling_only = bool(transient_main_convs) and any(
                c.get("catalog_cooling") is True for c in transient_main_convs
            )
            if transient_main_err is not None and (is_local_cooling_only or (int(doc.get("clean_retry_count") or 0) + 1) < MAX_CLEAN_RETRIES):
                cleaner.stats_ai_retries_scheduled += 1
                retry_msg_tpl = (
                    f"京东选品库处于本地冷却窗口，延后重试（不消耗重试次数 {{new_retries}}/{{max_retries}}）: {transient_main_err['error_msg']}"
                    if is_local_cooling_only
                    else f"主商品官方转链暂态失败，保留待下轮重试 ({{new_retries}}/{{max_retries}}): {transient_main_err['error_msg']}"
                )
                outcomes_by_id[mid] = _build_retry_or_max_outcome(
                    doc=doc,
                    site=site,
                    channel=channel,
                    mid=mid,
                    raw_title=raw_title,
                    ai1_res=ai1_res,
                    conv_summary=conv_summary_s3,
                    ai2_res=None,
                    stage=CLEAN_STAGE_NEED_CONVERT,
                    retry_err_code=transient_main_err["error_code"],
                    max_err_code=ERR_AI2_MAIN_DEAD_ONLY_FILLER,
                    retry_msg=retry_msg_tpl,
                    max_msg="主商品链接转链失败，仅凑单品存活",
                    attempt_errors=attempt_errors,
                    candidate_count=len(targets),
                    consume_retry=not is_local_cooling_only,
                    retry_delay_sec=_jd_catalog_cooldown_retry_delay_sec(cleaner) if is_local_cooling_only else None,
                )
                continue

            outcomes_by_id[mid] = {
                "status": "failed",
                "stage_outcome": "failed",
                "error_code": ERR_AI2_MAIN_DEAD_ONLY_FILLER,
                "error_msg": "主商品链接转链失败，仅凑单品链接存活，提前终止拦截",
                "source_site": site,
                "source_site_name": site_name,
                "deal_source": site,
                "deal_source_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_s3,
                "ai_stage2_result": None,
                "quality_score": 0,
                "candidate_count": len(targets),
                "attempt_errors": attempt_errors,
            }
            continue

        transient_degraded_convs = [
            c
            for c in conv_list
            if c.get("convert_status") == "failed"
            and (c.get("resolution_kind") == ResolutionKind.DEGRADED_PRODUCT or c.get("catalog_cooling") is True)
            and c.get("text_role_hint") != "filler_candidate"
        ]
        transient_degraded = transient_degraded_convs[0] if transient_degraded_convs else None
        is_local_cooling_only = bool(transient_degraded_convs) and any(
            c.get("catalog_cooling") is True for c in transient_degraded_convs
        )
        if transient_degraded is not None and (is_local_cooling_only or (int(doc.get("clean_retry_count") or 0) + 1) < MAX_CLEAN_RETRIES):
            cleaner.stats_ai_retries_scheduled += 1
            retry_msg_tpl = (
                f"京东选品库处于本地冷却窗口，延后重试（不消耗重试次数 {{new_retries}}/{{max_retries}}）: {transient_degraded.get('error_msg') or ''}"
                if is_local_cooling_only
                else f"存在非凑单链接触发详情反查暂态限流，保留待下轮重试 ({{new_retries}}/{{max_retries}}): {transient_degraded.get('error_msg') or ''}"
            )
            outcomes_by_id[mid] = _build_retry_or_max_outcome(
                doc=doc,
                site=site,
                channel=channel,
                mid=mid,
                raw_title=raw_title,
                ai1_res=ai1_res,
                conv_summary=conv_summary_s3,
                ai2_res=None,
                stage=CLEAN_STAGE_NEED_CONVERT,
                retry_err_code=ERR_ADAPTER_CONVERT_FAILED,
                max_err_code=ERR_ADAPTER_CONVERT_FAILED,
                retry_msg=retry_msg_tpl,
                max_msg="详情反查达重试上限",
                attempt_errors=attempt_errors,
                candidate_count=len(targets),
                consume_retry=not is_local_cooling_only,
                retry_delay_sec=_jd_catalog_cooldown_retry_delay_sec(cleaner) if is_local_cooling_only else None,
            )
            continue

        unique_main_pids = {
            (str(c.get("platform") or ""), str(c.get("goods_id") or ""))
            for c in main_prod_convs
            if c.get("platform") and c.get("goods_id")
        }
        single_main_pid: Optional[Tuple[str, str]] = next(iter(unique_main_pids)) if len(unique_main_pids) == 1 else None
        surviving_candidates.append(
            (mid, doc, ai1_res, conv_list, main_prod_convs, conv_summary_s3, attempt_errors, single_main_pid)
        )

    cleaner._evict_transient_failed_cache()

    batch_pids = {cand[7] for cand in surviving_candidates if cand[7] is not None}
    if isinstance(shared_active_products, dict):
        if shared_lock is not None:
            with shared_lock:
                active_formal_products = {pid: dict(shared_active_products[pid]) for pid in batch_pids if pid in shared_active_products}
        else:
            active_formal_products = shared_active_products
    elif formal_col is not None and batch_pids:
        active_formal_products = query_active_formal_products(formal_col, pid_keys=batch_pids)
    else:
        active_formal_products = {}

    post_stage3_db_early_count = 0
    for mid, doc, ai1_res, conv_list, main_prod_convs, conv_summary_s3, attempt_errors, single_main_pid in surviving_candidates:
        site = str(doc.get("source_site") or "")
        site_name = get_source_site_name(site)
        channel = str(doc.get("source_channel") or "")
        raw_title = str(doc.get("title") or "").strip()
        targets = ai1_res.get("extracted_targets") or []

        ex_info_s3 = active_formal_products.get(single_main_pid) if single_main_pid is not None else None
        should_upgrade = False
        if single_main_pid is not None and ex_info_s3 is not None:
            should_upgrade, upgrade_reason = should_upgrade_existing_deal(ex_info_s3, ai1_res, doc)
            if should_upgrade:
                cleaner.stats_deal_upgrades_post_stage3 += 1
                if not quiet_sub_logs:
                    logger.info(
                        "✨ [Stage 3 择优升级] (%s:%s): %s -> 推进 AI-2",
                        single_main_pid[0],
                        single_main_pid[1],
                        upgrade_reason,
                    )

        if single_main_pid is not None and ex_info_s3 is not None and not should_upgrade:
            c_plat, c_gid = single_main_pid
            primary_conv = main_prod_convs[0]
            c_gsign = str(primary_conv.get("goods_sign") or "")
            off_p = primary_conv.get("official_product") or {}
            merged_tids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
            if mid not in merged_tids:
                merged_tids.insert(0, mid)
            cleaner.stats_early_terminated_post_stage3 += 1
            post_stage3_db_early_count += 1
            clean_copy = sanitize_polished_copywriting(str(ai1_res.get("combined_raw_text") or raw_title))
            outcomes_by_id[mid] = _build_early_terminated_outcome(
                early_stage="post_stage3_db",
                error_msg=f"转链唯一商品({c_plat}:{c_gid})已在正式库清洗完毕(12h内)，提前终止跳过AI-2并累加共振",
                site=site,
                site_name=site_name,
                channel=channel,
                mid=mid,
                raw_title=raw_title,
                c_plat=c_plat,
                c_gid=c_gid,
                c_gsign=c_gsign,
                official_title=off_p.get("title") or ex_info_s3.get("official_title") or raw_title,
                official_price=off_p.get("price") or ex_info_s3.get("official_price"),
                deal_price=ex_info_s3.get("deal_price"),
                quality_score=ex_info_s3.get("quality_score", 75),
                merged_tids=merged_tids,
                ai1_res=ai1_res,
                converted_links=conv_summary_s3,
                ai2_marker={"early_terminated": "post_stage3_existing_product"},
                candidate_count=len(targets),
                attempt_errors=attempt_errors,
                seen_count=max(1, int(doc.get("seen_count") or 1)),
                seen_channels=doc.get("seen_channels") or ([channel] if channel else []),
                clean_copy=clean_copy,
            )
            continue

        outcomes_by_id[mid] = {
            "status": "in_progress",
            "stage_outcome": "advance_to_ai2",
            "source_site": site,
            "source_channel": channel,
            "source_item_id": mid,
            "title": raw_title,
            "ai_stage1_result": ai1_res,
            "converted_links": conv_summary_s3,
            "full_conv_list": conv_list,
            "candidate_count": len(conv_list),
        }

    if post_stage3_db_early_count > 0 and not quiet_sub_logs:
        logger.info("⚡ [Stage 2 在库去重早停] 唯一商品命中正式库跳过AI-2=%d条", post_stage3_db_early_count)

    out_list = [
        outcomes_by_id[str(d.get("source_item_id") or "")]
        for d in selected_docs
        if str(d.get("source_item_id") or "") in outcomes_by_id
    ]
    if not quiet_sub_logs:
        logger.info(
            "🔗 [官方转链完成] 耗时=%.1fs | 认领=%d条 (待终审=%d, 早停=%d, 重试=%d, 淘汰=%d) | 缓存命中=%d",
            t_stage3_elapsed,
            len(selected_docs),
            sum(1 for o in out_list if o.get("stage_outcome") == "advance_to_ai2"),
            sum(1 for o in out_list if o.get("stage_outcome") == "early_terminated"),
            sum(1 for o in out_list if o.get("status") == "retry_pending"),
            sum(1 for o in out_list if o.get("stage_outcome") == "failed"),
            cleaner.stats_cache_hits,
        )
    return out_list, t_stage3_elapsed


def _project_official_product_for_ai2(off_prod: Any) -> Any:
    """精简传给 AI-2 提示词的商品视图，避免 desc/specs/carousel_images 膨胀 Token，同时保留守护模式落地字段。"""
    if not isinstance(off_prod, dict):
        return off_prod
    return {k: off_prod[k] for k in _AI2_OFFICIAL_PRODUCT_KEYS if k in off_prod}


def process_ai2_unit(
    cleaner: Any,
    selected_docs: List[Dict[str, Any]],
    raw_col=None,
    neighbor_maps_by_id: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    quiet_sub_logs: bool = False,
) -> Tuple[List[Dict[str, Any]], float]:
    """阶段三/AI-2 终审单元：AI-2 质量终审、Fail-Closed 券链一致性拦截、完整正式商品文档构建。"""
    if not selected_docs:
        return [], 0.0

    t_ai2_start = time.perf_counter()
    draft_copy_by_id: Dict[str, str] = {}
    stage2_inputs: List[Dict[str, Any]] = []

    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        site = str(doc.get("source_site") or "")
        ai1_res = doc.get("ai_stage1_result") or {}
        conv_list = doc.get("full_conv_list") or doc.get("converted_links") or doc.get("stage3_converted_links") or []

        merged_tids_for_copy = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
        if mid not in merged_tids_for_copy:
            merged_tids_for_copy.insert(0, mid)
        tip_map_for_copy = (neighbor_maps_by_id.get(mid) if neighbor_maps_by_id else None) or load_neighbor_docs(
            raw_col, site, merged_tids_for_copy, current_doc=doc
        )
        draft_copywriting = build_clean_original_copywriting(
            merged_tids_for_copy,
            tip_map_for_copy,
            ai1_res.get("extracted_targets") or [],
            doc=doc,
            strip_spans=ai1_res.get("strip_spans") or [],
        )
        draft_copy_by_id[mid] = draft_copywriting

        has_valid_prod = any(
            c.get("convert_status") == "product" and c.get("text_role_hint") != "filler_candidate"
            for c in conv_list
        )
        omitted_lids: List[str] = []
        ai2_conv_links: List[Dict[str, Any]] = []
        for c in conv_list:
            if (
                has_valid_prod
                and c.get("convert_status") == "failed"
                and c.get("error_code") in (ERR_JD_NUMERIC_SKU, ERR_TB_NUMERIC_ID, ERR_PDD_NUMERIC_ID)
            ):
                omitted_lids.append(str(c["link_id"]))
                continue
            item_view: Dict[str, Any] = {
                "link_id": c["link_id"],
                "text_role_hint": c.get("text_role_hint") or "main_candidate",
                "convert_status": c["convert_status"],
                "platform": c["platform"],
                "official_product": _project_official_product_for_ai2(c.get("official_product")),
            }
            if c.get("convert_status") == "product" and c.get("goods_id"):
                item_view["goods_id"] = str(c["goods_id"])
            ai2_conv_links.append(item_view)

        combined_raw_for_ai2 = str(ai1_res.get("combined_raw_text") or "")
        for omitted_lid in omitted_lids:
            combined_raw_for_ai2 = re.sub(rf"[ \t]*\[#{re.escape(omitted_lid)}\]", "", combined_raw_for_ai2)
        if omitted_lids:
            combined_raw_for_ai2 = "\n".join(
                ln.rstrip() for ln in combined_raw_for_ai2.splitlines() if ln.strip()
            ).strip()

        stage2_inputs.append({
            "main_tip_id": mid,
            "source_site": site,
            "is_group_stream": bool(doc.get("is_group_stream")),
            "combined_raw_text": combined_raw_for_ai2,
            "draft_copywriting": draft_copywriting,
            "share_image_count": len(ai1_res.get("extracted_images") or []),
            "converted_links": ai2_conv_links,
        })

    if not quiet_sub_logs:
        bs2 = max(1, getattr(cleaner, "ai2_batch_size", 6))
        n_chunks = (len(stage2_inputs) + bs2 - 1) // bs2 if stage2_inputs else 0
        logger.info(
            "🧠 [AI-2 终审启动] 认领=%d款新商品 (每组=%d, 组数=%d, 并发=%d)",
            len(stage2_inputs),
            bs2,
            n_chunks,
            min(cleaner.ai_max_workers, n_chunks) if n_chunks else 1,
        )
    ai2_results = cleaner.run_ai_stage2_batch(stage2_inputs, quiet_sub_logs=quiet_sub_logs)
    t_ai2_elapsed = round(time.perf_counter() - t_ai2_start, 2)
    s2_input_map = {item["main_tip_id"]: item for item in stage2_inputs}

    outcomes_by_id: Dict[str, Dict[str, Any]] = {}
    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        site = str(doc.get("source_site") or "")
        site_name = get_source_site_name(site)
        channel = str(doc.get("source_channel") or "")
        raw_title = str(doc.get("title") or "").strip()
        ai1_res = doc.get("ai_stage1_result") or {}
        conv_list = doc.get("full_conv_list") or doc.get("converted_links") or doc.get("stage3_converted_links") or []
        s2_inp = s2_input_map.get(mid) or {}
        ai2_res = ai2_results.get(mid)
        if not isinstance(ai2_res, dict) or "is_valid_deal" not in ai2_res:
            cleaner.stats_ai2_fallbacks += 1
            ai2_res = fallback_stage2_evaluate(s2_inp)
        else:
            cleaner.stats_ai2_model_hits += 1
            ai2_res["_from_fallback"] = False

        conv_summary_for_db = [{k: v for k, v in c.items() if k != "adapter_res"} for c in conv_list]
        if ai2_res.get("_from_fallback"):
            cleaner.stats_ai_retries_scheduled += 1
            outcomes_by_id[mid] = _build_retry_or_max_outcome(
                doc=doc,
                site=site,
                channel=channel,
                mid=mid,
                raw_title=raw_title,
                ai1_res=ai1_res,
                conv_summary=conv_summary_for_db,
                ai2_res=ai2_res,
                stage=CLEAN_STAGE_NEED_AI2,
                retry_err_code=ERR_AI_TIMEOUT_RETRY,
                max_err_code=ERR_AI_MAX_RETRIES_EXCEEDED,
                retry_msg="AI-2 超时或未返回有效终审，跳过本轮并累加重试 ({new_retries}/{max_retries})",
                max_msg="AI-2 连续 {new_retries} 次超时或未返回结果，达最大重试上限({max_retries}次)，永久放弃",
                candidate_count=len(conv_list),
            )
            continue

        link_map = {c["link_id"]: c for c in conv_list}
        is_valid = bool(ai2_res.get("is_valid_deal"))
        main_lid = str(ai2_res.get("main_link_id") or "").strip()
        main_conv = link_map.get(main_lid)
        if is_valid and (
            not main_conv
            or main_conv.get("convert_status") != "product"
            or main_conv.get("text_role_hint") == "filler_candidate"
        ):
            is_valid = False
            if not ai2_res.get("reject_reason"):
                ai2_res["reject_reason"] = "MAIN_PRODUCT_DEAD_ONLY_FILLER"

        if not is_valid or not main_conv:
            rej = str(ai2_res.get("reject_reason") or "ALL_PRODUCTS_DEAD").upper()
            if "COUPON" in rej:
                err_code = ERR_AI2_COUPON_FAILED_OR_DROPPED
            elif "FILLER" in rej:
                err_code = ERR_AI2_MAIN_DEAD_ONLY_FILLER
            elif "MISMATCH" in rej:
                err_code = ERR_AI2_PRODUCT_MISMATCH
            elif "VIRTUAL" in rej or "JUNK" in rej:
                err_code = ERR_AI2_VIRTUAL_OR_JUNK
            else:
                err_code = ERR_ADAPTER_CONVERT_FAILED
            outcomes_by_id[mid] = {
                "status": "failed",
                "stage_outcome": "failed",
                "error_code": err_code,
                "error_msg": f"AI-2 终审拦截 ({rej})",
                "source_site": site,
                "source_site_name": site_name,
                "deal_source": site,
                "deal_source_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "title": raw_title,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_for_db,
                "ai_stage2_result": ai2_res,
                "quality_score": 0,
                "candidate_count": len(conv_list),
                "attempt_errors": [],
            }
            continue

        try:
            quality_score = int(float(ai2_res.get("quality_score") or 0))
        except Exception:
            quality_score = 0
        quality_score = max(0, min(100, quality_score))
        raw_breakdown = ai2_res.get("score_breakdown") if isinstance(ai2_res.get("score_breakdown"), dict) else {}
        breakdown_caps = {
            "match_score": 30,
            "completeness_score": 30,
            "price_clarity_score": 20,
            "tutorial_and_image_score": 20,
        }
        score_breakdown: Dict[str, Any] = {}
        for bk, bval in raw_breakdown.items():
            if bk in breakdown_caps and not isinstance(bval, bool):
                try:
                    score_breakdown[bk] = max(0, min(breakdown_caps[bk], int(float(bval))))
                except (TypeError, ValueError):
                    pass
            elif not isinstance(bval, bool):
                score_breakdown[bk] = bval

        source_bonus = SOURCE_REPUTATION_BONUS.get(f"{site}:{channel}") or SOURCE_REPUTATION_BONUS.get(site, 0)
        if source_bonus > 0 and quality_score >= QUALIFIED_DEAL_MIN_SCORE:
            raw_base_score = quality_score
            quality_score = min(100, quality_score + source_bonus)
            score_breakdown["base_quality_score"] = raw_base_score
            score_breakdown["source_reputation_bonus"] = source_bonus

        plat = str(main_conv.get("platform") or "")
        goods_id = str(main_conv.get("goods_id") or "")
        goods_sign = str(main_conv.get("goods_sign") or "")
        product_card, adapter_res = _extract_product_card_from_conv(main_conv)
        my_promo_link = str(main_conv.get("my_promo_link") or "")
        my_promo_token = str(main_conv.get("my_promo_token") or "")

        raw_deal_p = ai2_res.get("deal_price")
        deal_price: Optional[float] = None
        if isinstance(raw_deal_p, (int, float)) and 0 < float(raw_deal_p) < 100000:
            deal_price = round(float(raw_deal_p), 2)
        elif isinstance(raw_deal_p, str):
            try:
                val = float(re.sub(r"[^\d.]", "", raw_deal_p))
                if 0 < val < 100000:
                    deal_price = round(val, 2)
            except Exception:
                pass

        unit_deal_price = normalize_inline_spaces(str(ai2_res.get("unit_deal_price") or "")).strip() or None
        price_logic = normalize_inline_spaces(str(ai2_res.get("price_logic") or "")).strip() or None
        card_highlight = sanitize_card_highlight(ai2_res.get("card_highlight"))
        clean_title = normalize_inline_spaces(str(ai2_res.get("clean_title") or product_card.get("name") or raw_title)).strip()

        def _is_other_known_platform(c: Dict[str, Any]) -> bool:
            c_p = str(c.get("platform") or "").strip().lower()
            return bool(plat in ("jd", "taobao", "pdd") and c_p in ("jd", "taobao", "pdd") and c_p != plat)

        coupon_links: List[Dict[str, Any]] = []
        dropped_cross_platform_coupon = False
        seen_cpn_ids: Set[str] = set()
        seen_cpn_targets: Set[Tuple[str, str]] = set()
        seen_cpn_venues: Set[str] = set()

        def _append_coupon_candidate(c_obj: Dict[str, Any], raw_label: Any) -> bool:
            lid = str(c_obj.get("link_id") or "").strip()
            if (
                not lid
                or lid == main_lid
                or lid in seen_cpn_ids
                or c_obj.get("convert_status") in ("failed", "skipped_filler")
                or _is_other_known_platform(c_obj)
            ):
                return False
            if goods_id and str(c_obj.get("goods_id") or "").strip() == goods_id:
                return False
            c_plat = str(c_obj.get("platform") or plat).strip().lower()
            c_url = str(c_obj.get("my_promo_link") or "").strip()
            c_tok = str(c_obj.get("my_promo_token") or "").strip()
            c_inp = str(c_obj.get("clean_input") or "").strip()
            if is_shop_homepage_url(c_url) or is_shop_homepage_url(c_inp):
                return False
            if not c_url and not c_tok:
                return False
            if c_url and my_promo_link and c_url == my_promo_link:
                return False
            if plat == "taobao":
                c_host = (urlsplit(c_url).hostname or "").strip().lower() if c_url else ""
                if c_obj.get("convert_status") == "product" or c_host in ("s.click.taobao.com", "s.click.tmall.com"):
                    return False
            dedup_sig = (c_url, c_tok)
            if dedup_sig in seen_cpn_targets:
                return False
            venue_sigs = {
                v
                for v in (
                    rebuild_clean_official_venue_url(c_inp),
                    rebuild_clean_official_venue_url(c_url) if c_url else None,
                )
                if v
            }
            if venue_sigs & seen_cpn_venues:
                return False
            seen_cpn_ids.add(lid)
            seen_cpn_targets.add(dedup_sig)
            seen_cpn_venues.update(venue_sigs)
            coupon_links.append({
                "link_id": lid,
                "label": clean_coupon_label(raw_label or "优惠券"),
                "url": c_url,
                "token": c_tok,
                "platform": c_plat or plat,
            })
            return True

        for item in ai2_res.get("coupon_links") or []:
            if not isinstance(item, dict):
                continue
            lid = str(item.get("link_id") or "").strip()
            c_obj = link_map.get(lid)
            if not c_obj or c_obj.get("convert_status") in ("failed", "skipped_filler") or lid == main_lid or lid in seen_cpn_ids:
                continue
            if _is_other_known_platform(c_obj):
                dropped_cross_platform_coupon = True
                continue
            _append_coupon_candidate(c_obj, item.get("label") or item.get("coupon_desc") or "优惠券")

        def _is_coupon_absorbed_by_main(c: Dict[str, Any]) -> bool:
            if (
                c.get("link_id") == main_lid
                or c.get("convert_status") != "product"
                or _is_other_known_platform(c)
            ):
                return False
            c_gid = str(c.get("goods_id") or "").strip()
            c_url = str(c.get("my_promo_link") or "").strip()
            return bool(
                (goods_id and c_gid == goods_id)
                or (my_promo_link and c_url == my_promo_link)
            )

        raw_coupon_candidates = [
            c
            for c in conv_list
            if c.get("link_id") != main_lid
            and c.get("text_role_hint") != "filler_candidate"
            and not _is_other_known_platform(c)
            and not _is_coupon_absorbed_by_main(c)
            and is_explicit_coupon_target(c)
        ]

        if is_valid and not dropped_cross_platform_coupon and raw_coupon_candidates:
            has_failed_cpn = any(
                c.get("convert_status") in ("failed", "skipped_filler")
                for c in raw_coupon_candidates
            )
            if not has_failed_cpn:
                generic_link_labels = frozenset({"逛丢直达购买链", "领取链接", "直达链接", "领券链接", "优惠券链接", "点击领券"})
                url_label_map: Dict[str, str] = {}
                for lk in doc.get("extracted_links") or []:
                    if not isinstance(lk, dict):
                        continue
                    u_raw = html_lib.unescape(str(lk.get("url") or "")).strip()
                    lbl = normalize_inline_spaces(str(lk.get("label") or "")).strip()
                    if (
                        not u_raw
                        or not lbl
                        or lbl.lower().startswith(("http://", "https://"))
                        or len(lbl) > 24
                        or lbl in generic_link_labels
                    ):
                        continue
                    url_label_map.setdefault(u_raw, lbl)
                    u_venue = rebuild_clean_official_venue_url(u_raw)
                    if u_venue:
                        url_label_map.setdefault(u_venue, lbl)
                for c in raw_coupon_candidates:
                    if c.get("convert_status") != "coupon_or_activity":
                        continue
                    c_inp_raw = str(c.get("clean_input") or "").strip()
                    c_inp_venue = rebuild_clean_official_venue_url(c_inp_raw) or ""
                    cand_label = url_label_map.get(c_inp_raw) or url_label_map.get(c_inp_venue) or "优惠券"
                    _append_coupon_candidate(c, cand_label)

        has_absorbed_coupon = any(
            is_explicit_coupon_target(c) and _is_coupon_absorbed_by_main(c)
            for c in conv_list
        )
        if is_valid and not coupon_links and (dropped_cross_platform_coupon or not has_absorbed_coupon):
            should_reject = False
            reject_detail = ""
            if dropped_cross_platform_coupon:
                should_reject = True
                reject_detail = "AI-2 错将异平台链接混作主商品优惠券，防跨平台串货拦截"
            else:
                if raw_coupon_candidates:
                    should_reject = True
                    reject_detail = "原始优惠券链接转链失败或未被保留"
                elif deal_price is not None:
                    has_failed_non_filler = any(
                        c.get("link_id") != main_lid
                        and c.get("text_role_hint") != "filler_candidate"
                        and not _is_other_known_platform(c)
                        and c.get("convert_status") == "failed"
                        and c.get("error_code") not in (ERR_JD_NUMERIC_SKU, ERR_TB_NUMERIC_ID, ERR_PDD_NUMERIC_ID)
                        for c in conv_list
                    )
                    if has_failed_non_filler:
                        off_act_p = float(product_card.get("activity_price") or product_card.get("price") or 0.0)
                        if off_act_p > 0 and deal_price < off_act_p * 0.95:
                            should_reject = True
                            reject_detail = "依赖的前置优惠链接转链失效且到手价低于官方售价"

            if should_reject:
                ai2_res["is_valid_deal"] = False
                ai2_res["reject_reason"] = "COUPON_LINK_FAILED_OR_DROPPED"
                outcomes_by_id[mid] = {
                    "status": "failed",
                    "stage_outcome": "failed",
                    "error_code": ERR_AI2_COUPON_FAILED_OR_DROPPED,
                    "error_msg": f"AI-2 终审拦截 (COUPON_LINK_FAILED_OR_DROPPED: {reject_detail})",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_for_db,
                    "ai_stage2_result": ai2_res,
                    "quality_score": 0,
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                }
                continue

        raw_steps_val = ai2_res.get("tutorial_steps")
        if isinstance(raw_steps_val, list):
            raw_step_lines = [str(st or "") for st in raw_steps_val]
        elif isinstance(raw_steps_val, str) and raw_steps_val.strip():
            raw_step_lines = raw_steps_val.splitlines()
        else:
            raw_step_lines = str(ai2_res.get("tutorial_summary") or "").splitlines()
        tutorial_steps = [s for s in (sanitize_clean_tutorial_step(st) for st in raw_step_lines) if s]
        if not coupon_links:
            empty_cpn_ui_rewrites = (
                ("先在上方【领券】处", "通过下方购买链接"),
                ("在上方【领券】处", "通过下方购买链接"),
                ("上方【领券】处", "下方购买链接"),
                ("上方【领券】", "下方购买链接"),
                ("先在上方领券处", "通过下方购买链接"),
                ("在上方领券处", "通过下方购买链接"),
                ("在上方领券", "通过下方购买链接"),
                ("上方领券处", "下方购买链接"),
                ("上方领券", "下方购买链接"),
            )
            rewritten_steps: List[str] = []
            for step in tutorial_steps:
                for old_ph, new_ph in empty_cpn_ui_rewrites:
                    if old_ph in step:
                        step = step.replace(old_ph, new_ph)
                rewritten_steps.append(step)
            tutorial_steps = rewritten_steps
        tutorial_text = "\n".join(tutorial_steps)

        official_img = str(product_card.get("image_url") or "").strip()
        extracted_imgs = [
            strip_markdown_link_wrapper(str(u).strip())
            for u in (ai1_res.get("extracted_images") or [])
            if str(u or "").strip()
        ]
        user_share_images: List[str] = []
        for img_u in extracted_imgs:
            if img_u and img_u != official_img and img_u not in user_share_images:
                user_share_images.append(img_u)

        merged_tip_ids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
        if mid not in merged_tip_ids:
            merged_tip_ids.insert(0, mid)
        tip_map = (neighbor_maps_by_id.get(mid) if neighbor_maps_by_id else None) or load_neighbor_docs(
            raw_col, site, merged_tip_ids, current_doc=doc
        )
        merged_source_urls: List[str] = []
        for m_tid in merged_tip_ids:
            nd = tip_map.get(m_tid) or {}
            u_det = str(nd.get("source_detail_url") or nd.get("source_url") or "").strip()
            if u_det and u_det not in merged_source_urls:
                merged_source_urls.append(u_det)

        raw_polished_copy = str(ai2_res.get("polished_copywriting") or "").strip()
        original_copywriting = (
            (sanitize_polished_copywriting(raw_polished_copy) if raw_polished_copy else "")
            or draft_copy_by_id.get(mid)
            or build_clean_original_copywriting(
                merged_tip_ids,
                tip_map,
                ai1_res.get("extracted_targets") or [],
                doc=doc,
                strip_spans=ai1_res.get("strip_spans") or [],
            )
        )

        source_list_page_url = str(doc.get("source_list_page_url") or doc.get("source_url") or "").strip()
        source_detail_url = str(doc.get("source_detail_url") or doc.get("source_url") or source_list_page_url).strip()

        official_price = float(product_card.get("price") or adapter_res.get("price") or 0.0)
        original_price = float(product_card.get("original_price") or official_price)
        official_activity_price = float(product_card.get("activity_price") or official_price)
        coupon_amount = float(product_card.get("coupon_amount") or adapter_res.get("coupon_amount") or 0.0)
        saved_price = round(max(coupon_amount, max(0.0, original_price - official_activity_price)), 2)

        raw_ai1_tscore = ai1_res.get("tutorial_score")
        tutorial_score = (
            int(raw_ai1_tscore)
            if isinstance(raw_ai1_tscore, (int, float)) and not isinstance(raw_ai1_tscore, bool)
            else 50
        )

        pub_ts = (
            coerce_unix_timestamp(doc.get("publish_ts"))
            or parse_publish_time_epoch(doc.get("publish_time"))
            or coerce_unix_timestamp(doc.get("fetched_at"))
            or time.time()
        )

        deal_info = {
            "is_deal_tip": True,
            "quality_score": quality_score,
            "tutorial_score": tutorial_score,
            "score_breakdown": score_breakdown,
            "clean_title": clean_title,
            "card_highlight": card_highlight,
            "publish_ts": float(pub_ts),
            "expire_time": doc.get("expire_time"),
            "deal_price": deal_price,
            "unit_deal_price": unit_deal_price,
            "price_logic": price_logic,
            "official_price": round(official_price, 2),
            "raw_tip_title": raw_title or str(product_card.get("name") or ""),
            "original_copywriting": original_copywriting,
            "coupon_links": coupon_links,
            "tutorial_steps": tutorial_steps,
            "tutorial_text": tutorial_text,
            "user_share_images": user_share_images,
            "my_promotion_link": my_promo_link,
            "my_promotion_token": my_promo_token,
            "source_site": site,
            "source_site_name": site_name,
            "source_channel": channel,
            "source_item_id": mid,
            "provider_nickname": str((doc.get("hot_metrics") or {}).get("provider_nickname") or "").strip(),
            "source_url": source_detail_url,
            "source_list_page_url": source_list_page_url,
            "source_detail_url": source_detail_url,
            "merged_tip_ids": merged_tip_ids,
            "merged_source_urls": merged_source_urls,
            "seen_count": int(doc.get("seen_count") or 1),
            "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
            "hot_metrics": doc.get("hot_metrics") or {},
        }

        product_doc = {
            **product_card,
            "platform": plat,
            "platform_name": product_card.get("platform_name") or platform_name(plat),
            "product_id": goods_id,
            "goods_id": goods_id,
            "goods_sign": goods_sign,
            "deal_source": site,
            "deal_source_name": site_name,
            "name": str(product_card.get("name") or adapter_res.get("title") or clean_title).strip(),
            "brand": str(product_card.get("brand") or product_card.get("brand_name") or ""),
            "category": str(product_card.get("category") or product_card.get("category_name") or ""),
            "price": round(official_price, 2),
            "original_price": round(original_price, 2),
            "activity_price": round(official_activity_price, 2),
            "saved_price": round(saved_price, 2),
            "coupon_amount": round(coupon_amount, 2),
            "has_coupon": coupon_amount > 0,
            "commission_rate": float(product_card.get("commission_rate") or 0.0),
            "commission_amount": float(product_card.get("commission_amount") or 0.0),
            "sales_tip": str(product_card.get("sales_tip") if product_card.get("sales_tip") is not None else "0"),
            "image_url": official_img or str(doc.get("image_url") or ""),
            "product_url": str(product_card.get("product_url") or my_promo_link or ""),
            "mall_name": str(product_card.get("mall_name") or doc.get("mall_name") or ""),
            "desc": str(product_card.get("desc") or ""),
            "carousel_images": product_card.get("carousel_images") or ([official_img] if official_img else []),
            "detail_tags": product_card.get("detail_tags") or [],
            "service_guarantees": product_card.get("service_guarantees") or [],
            "shop_ratings": product_card.get("shop_ratings") or {},
            "specs": product_card.get("specs") or [],
            "coupon_info": product_card.get("coupon_info") or {},
            "deal_info": deal_info,
        }

        if quality_score < QUALIFIED_DEAL_MIN_SCORE:
            outcomes_by_id[mid] = {
                "status": "low_score",
                "stage_outcome": "low_score",
                "error_code": ERR_LOW_QUALITY_SCORE,
                "error_msg": f"综合评分 {quality_score} < {QUALIFIED_DEAL_MIN_SCORE} 分，仅留存初步表不入正式表",
                "source_site": site,
                "source_site_name": site_name,
                "deal_source": site,
                "deal_source_name": site_name,
                "source_channel": channel,
                "source_item_id": mid,
                "platform": plat,
                "product_id": goods_id,
                "goods_sign": goods_sign,
                "official_title": product_doc["name"],
                "title": raw_title,
                "deal_price": deal_price,
                "official_price": official_price,
                "quality_score": quality_score,
                "score_breakdown": score_breakdown,
                "merged_tip_ids": merged_tip_ids,
                "original_copywriting": original_copywriting,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_for_db,
                "ai_stage2_result": ai2_res,
                "candidate_count": len(conv_list),
                "attempt_errors": [],
            }
            continue

        _attach_preseeded_default_promotion(
            plat=plat,
            goods_id=goods_id,
            goods_sign=goods_sign,
            main_conv=main_conv,
            adapter_res=adapter_res,
            my_promo_link=my_promo_link,
            my_promo_token=my_promo_token,
            product_doc=product_doc,
        )

        outcomes_by_id[mid] = {
            "status": "success",
            "stage_outcome": "success",
            "error_code": "OK",
            "error_msg": "",
            "source_site": site,
            "source_channel": channel,
            "source_item_id": mid,
            "platform": plat,
            "product_id": goods_id,
            "goods_sign": goods_sign,
            "official_title": product_doc["name"],
            "raw_title": raw_title,
            "deal_price": deal_price,
            "official_price": official_price,
            "quality_score": quality_score,
            "score_breakdown": score_breakdown,
            "merged_tip_ids": merged_tip_ids,
            "original_copywriting": original_copywriting,
            "ai_stage1_result": ai1_res,
            "converted_links": conv_summary_for_db,
            "ai_stage2_result": ai2_res,
            "winning_anchor": str(main_conv.get("clean_input") or "")[:100],
            "winning_anchor_type": main_conv.get("text_role_hint") or "main_candidate",
            "candidate_count": len(conv_list),
            "tutorial_img_count": len(user_share_images),
            "coupon_link_count": len(coupon_links),
            "product_doc": product_doc,
        }

    out_list = [
        outcomes_by_id[str(d.get("source_item_id") or "")]
        for d in selected_docs
        if str(d.get("source_item_id") or "") in outcomes_by_id
    ]
    if not quiet_sub_logs:
        logger.info(
            "🧠 [AI-2 终审完成] 耗时=%.1fs | 认领=%d款 (入库=%d, 低分=%d, 重试=%d, 淘汰=%d)",
            t_ai2_elapsed,
            len(selected_docs),
            sum(1 for o in out_list if o.get("status") == "success"),
            sum(1 for o in out_list if o.get("status") in ("low_score", "cleaned_low_score")),
            sum(1 for o in out_list if o.get("status") == "retry_pending"),
            sum(1 for o in out_list if o.get("status") in ("failed", "cleaned_failed")),
        )
    return out_list, t_ai2_elapsed


def clean_batch_records(
    cleaner: Any,
    selected_docs: List[Dict[str, Any]],
    raw_col,
    formal_col=None,
    shared_active_products: Any = None,
    shared_lock: Optional[threading.Lock] = None,
    quiet_sub_logs: bool = False,
) -> List[Dict[str, Any]]:
    """串联执行 AI-1 -> 官方转链 -> AI-2 单窗口完整清洗闭环。"""
    if not selected_docs:
        return []

    t_batch_start = time.perf_counter()
    final_by_mid: Dict[str, Dict[str, Any]] = {}

    ai1_outcomes, t_ai1_elapsed, neighbor_maps = cleaner.process_ai1_unit(
        selected_docs,
        raw_col,
        quiet_sub_logs=quiet_sub_logs,
    )
    ai1_map = {str(r.get("source_item_id") or ""): r for r in ai1_outcomes}

    adv_to_convert: List[Dict[str, Any]] = []
    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        out = ai1_map.get(mid)
        if not out:
            continue
        if out.get("stage_outcome") == "advance_to_convert":
            d_copy = copy.deepcopy(doc)
            d_copy["ai_stage1_result"] = out.get("ai_stage1_result")
            adv_to_convert.append(d_copy)
        else:
            final_by_mid[mid] = out

    t_stage3_elapsed = 0.0
    adv_to_ai2: List[Dict[str, Any]] = []
    if adv_to_convert:
        conv_outcomes, t_stage3_elapsed = cleaner.process_convert_unit(
            adv_to_convert,
            formal_col=formal_col,
            shared_active_products=shared_active_products,
            shared_lock=shared_lock,
            quiet_sub_logs=quiet_sub_logs,
        )
        conv_map = {str(r.get("source_item_id") or ""): r for r in conv_outcomes}
        for doc in adv_to_convert:
            mid = str(doc.get("source_item_id") or "")
            out = conv_map.get(mid)
            if not out:
                continue
            if out.get("stage_outcome") == "advance_to_ai2":
                d_copy = copy.deepcopy(doc)
                d_copy["converted_links"] = out.get("converted_links") or []
                d_copy["full_conv_list"] = out.get("full_conv_list") or []
                adv_to_ai2.append(d_copy)
            else:
                final_by_mid[mid] = out

    t_ai2_elapsed = 0.0
    if adv_to_ai2:
        ai2_outcomes, t_ai2_elapsed = cleaner.process_ai2_unit(
            adv_to_ai2,
            raw_col=raw_col,
            neighbor_maps_by_id=neighbor_maps,
            quiet_sub_logs=quiet_sub_logs,
        )
        for out in ai2_outcomes:
            final_by_mid[str(out.get("source_item_id") or "")] = out

    cleaner.last_batch_stage_timings = {
        "stage2_ai1_seconds": t_ai1_elapsed,
        "stage3_convert_seconds": t_stage3_elapsed,
        "stage4_ai2_seconds": t_ai2_elapsed,
    }

    avg_ms = round((time.perf_counter() - t_batch_start) * 1000.0 / max(1, len(selected_docs)), 1)
    final_results: List[Dict[str, Any]] = []
    for doc in selected_docs:
        mid = str(doc.get("source_item_id") or "")
        if mid in final_by_mid:
            item = final_by_mid[mid]
            item["elapsed_ms"] = avg_ms
            final_results.append(item)

    if final_results:
        final_results[0]["_stage_timings"] = cleaner.last_batch_stage_timings

    return final_results
