# -*- coding: utf-8 -*-
"""用户爆料生效期归因与佣金统计引擎（纯 Decimal 精确算账，严格 Fail-Closed）。

核心口径：
1. 仅统计用户爆料实际在商品正式表展示的窗口 `[display_started_at, display_ended_at)`（或未结束时至当前/72h上限）；
2. 严格排除个人/渠道推广订单（仅计入未带任何推手/渠道归因、走站长默认推广位的成交订单）；
3. 严格排除已退款、已失效、未付款订单；售后维权中订单单独隔离计数、不虚增预估/结算佣金；
4. 全部金额汇总采用 `Decimal(..., ROUND_HALF_UP)` 并按 `(platform, order_sn)` 去重防重复计数。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from config import logger
from infrastructure.logging_utils import format_exc_summary
from models.deal_constants import DEAL_FORMAL_EXPIRY_HOURS, USER_SUBMIT_SITE
from models.product import extract_canonical_goods_id, extract_jd_image_key, is_jd_image_key, normalize_platform
from models.user import normalize_promoter_id

_DISPLAY_TIMEZONE = timezone(timedelta(hours=8))
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_MAX_EXPIRE_SECONDS = float(DEAL_FORMAL_EXPIRY_HOURS) * 3600.0
_JD_ITEM_URL_SKU_RE = re.compile(r"https?://(?:item|item\.m)\.jd\.com/(?:product/)?(\d+)\.html", re.IGNORECASE)
_JD_LEGACY_IMAGE_KEY_RE = re.compile(r"^[0-9a-fA-F]{8,22}_[0-9a-fA-F]{6,12}[A-Za-z][0-9a-fA-F]{6,12}$")


def _is_jd_canonical_image_key(value: str) -> bool:
    return bool(value and (is_jd_image_key(value) or _JD_LEGACY_IMAGE_KEY_RE.match(value)))

_JD_EXCLUDED_STATUSES = frozenset({2, 15, 19, 20, 21, 22, 23, 24, 25, 28})
_PDD_EXCLUDED_STATUSES = frozenset({-1, 4, 8, 10})
_TB_EXCLUDED_STATUSES = frozenset({13})
_RECENT_ORDER_LIMIT = 20


def _safe_canonical_promoter_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return normalize_promoter_id(value)
    except ValueError:
        return ""


def _to_decimal_money(value: Any) -> Optional[Decimal]:
    """将金额字段转为 2 位小数 Decimal（四舍五入），非有限数或负数返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            d = value
        elif isinstance(value, int):
            d = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                return None
            d = Decimal(str(value))
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            d = Decimal(text)
        else:
            to_decimal = getattr(value, "to_decimal", None)
            if callable(to_decimal):
                d = Decimal(str(to_decimal()))
            else:
                return None
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite() or d < _ZERO:
        return None
    return d.quantize(_CENT, rounding=ROUND_HALF_UP)


def _money_float(val: Decimal) -> float:
    return float(val.quantize(_CENT, rounding=ROUND_HALF_UP))


def _dt_to_utc_ts(val: Any) -> Optional[float]:
    """将 naive UTC datetime / aware datetime / Unix 时间戳统一转为 UTC 秒级浮点时间戳。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
        return dt.timestamp()
    if isinstance(val, (int, float)):
        num = float(val)
        if not math.isfinite(num) or num <= 0:
            return None
        return num / 1000.0 if num > 1e12 else num
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            dt = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            return dt.timestamp()
        except ValueError:
            try:
                num = float(text)
                if math.isfinite(num) and num > 0:
                    return num / 1000.0 if num > 1e12 else num
            except ValueError:
                return None
    return None


def _format_bj_dt(ts: Optional[float]) -> str:
    if ts is None or not math.isfinite(ts) or ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, _DISPLAY_TIMEZONE).strftime("%m-%d %H:%M")


def _format_duration_cn(seconds: float) -> str:
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return "不足1分钟"
    total_mins = secs // 60
    days = total_mins // (24 * 60)
    rem_mins = total_mins % (24 * 60)
    hours = rem_mins // 60
    mins = rem_mins % 60
    if days > 0:
        return f"{days}天{hours}小时" if hours > 0 else f"{days}天"
    if hours > 0:
        return f"{hours}小时{mins}分" if mins > 0 else f"{hours}小时"
    return f"{mins}分钟"


def _mask_order_sn(order_sn: Any) -> str:
    sn = str(order_sn or "").strip()
    if len(sn) > 10:
        return f"{sn[:4]}****{sn[-4:]}"
    if len(sn) > 6:
        return f"{sn[:2]}***{sn[-2:]}"
    return sn


def is_site_owner_default_order(
    order: Dict[str, Any],
    platform: str,
    default_jd_position_id: str = "",
) -> bool:
    """严格 Fail-Closed 判定订单是否走站长默认推广链接（非任何个人推广/渠道订单）。"""
    if not isinstance(order, dict):
        return False
    plat = normalize_platform(platform or order.get("platform") or "", default="")

    if str(order.get("promoter_uid") or "").strip():
        return False
    if str(order.get("attribution_status") or "").strip().lower() == "matched":
        return False
    if bool(order.get("attribution_identity")):
        return False

    if plat == "taobao":
        if str(order.get("relation_id") or "").strip() not in ("", "0"):
            return False
        if str(order.get("special_id") or "").strip() not in ("", "0"):
            return False
    elif plat == "jd":
        if str(order.get("sub_union_id") or "").strip() not in ("", "0"):
            return False
        raw_pos = str(order.get("promoter_id") or order.get("position_id") or "").strip()
        if raw_pos not in ("", "0"):
            canon_default = _safe_canonical_promoter_id(default_jd_position_id)
            canon_order_pos = _safe_canonical_promoter_id(raw_pos)
            if not canon_default or not canon_order_pos or canon_order_pos != canon_default:
                return False
    elif plat == "pdd":
        if order.get("is_self_operated") is False:
            return False

    return True


def _parse_int_status(raw_status: Any) -> Optional[int]:
    if raw_status is None or isinstance(raw_status, bool):
        return None
    if isinstance(raw_status, int):
        return raw_status
    text = str(raw_status).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def classify_order_commission_bucket(order: Dict[str, Any], platform: str) -> Tuple[str, Decimal]:
    """将订单归类为 settled / pending / after_sale / excluded，并给出精确 Decimal 佣金。"""
    plat = normalize_platform(platform or order.get("platform") or "", default="")
    status_int = _parse_int_status(order.get("order_status"))

    settled_dec = _to_decimal_money(order.get("settled_commission_yuan"))
    promo_dec = _to_decimal_money(order.get("promotion_amount_yuan"))
    est_dec = _to_decimal_money(order.get("estimated_commission_yuan"))
    default_comm = promo_dec if promo_dec is not None else (est_dec if est_dec is not None else _ZERO)

    is_excluded = (
        order.get("is_refund") is True
        or order.get("is_invalid") is True
        or (plat == "jd" and status_int in _JD_EXCLUDED_STATUSES)
        or (plat == "pdd" and status_int in _PDD_EXCLUDED_STATUSES)
        or (plat == "taobao" and status_int in _TB_EXCLUDED_STATUSES)
    )
    if is_excluded:
        return "excluded", default_comm

    is_after_sale = (
        (plat == "jd" and (order.get("is_after_sale") is True or status_int == 27))
        or (plat == "taobao" and str(order.get("refund_tag") or "").strip() == "1")
    )
    if is_after_sale:
        return "after_sale", default_comm

    if order.get("is_settled") is True:
        settled_val = settled_dec if settled_dec is not None else default_comm
        return "settled", settled_val

    return "pending", default_comm


def extract_order_event_ts(order: Dict[str, Any]) -> Optional[float]:
    """提取用于归因窗口比对的下单事件时间（优先 order_create_time，无有效时间戳返回 None）。"""
    for field in ("order_create_time", "order_time", "order_pay_time"):
        ts = _dt_to_utc_ts(order.get(field))
        if ts is not None and ts > 0:
            return ts
    return None


def extract_deal_order_goods_ids(
    row: Dict[str, Any],
    formal_doc: Optional[Dict[str, Any]] = None,
) -> Tuple[Set[str], bool]:
    """从用户爆料记录与正式商品文档解析可与 orders.goods_id 匹配的规范商品 ID 集合。"""
    plat = normalize_platform(row.get("resolved_platform") or row.get("platform") or "", default="")
    conv_links = row.get("converted_links") if isinstance(row.get("converted_links"), list) else []
    main_link = conv_links[0] if conv_links and isinstance(conv_links[0], dict) else {}
    off_prod = main_link.get("official_product") if isinstance(main_link.get("official_product"), dict) else {}

    out_ids: Set[str] = set()
    if plat == "jd":
        for raw_id in (
            row.get("resolved_product_id"),
            row.get("resolved_goods_sign"),
            row.get("candidate_goods_id"),
            main_link.get("goods_id"),
            main_link.get("goods_sign"),
            (formal_doc or {}).get("goods_id"),
            (formal_doc or {}).get("product_id"),
            (formal_doc or {}).get("goods_sign"),
        ):
            text = str(raw_id or "").strip()
            if text.isdigit() or _is_jd_canonical_image_key(text):
                out_ids.add(text)
        for raw_img in (
            off_prod.get("image_url"),
            (formal_doc or {}).get("image_url"),
            row.get("image_url"),
            row.get("product_image"),
        ):
            img_key = extract_jd_image_key(raw_img)
            if img_key:
                out_ids.add(img_key)
        url_candidates: List[Any] = [
            off_prod.get("product_url"),
            main_link.get("clean_input"),
            (formal_doc or {}).get("product_url"),
        ]
        for lnk in row.get("extracted_links") or []:
            if isinstance(lnk, dict):
                url_candidates.append(lnk.get("url"))
        for raw_u in url_candidates:
            u = str(raw_u or "").strip()
            if not u:
                continue
            m = _JD_ITEM_URL_SKU_RE.search(u)
            if m:
                out_ids.add(m.group(1))
        return out_ids, (len(out_ids) == 0)

    if plat == "taobao":
        for raw_id in (
            row.get("resolved_product_id"),
            row.get("candidate_goods_id"),
            main_link.get("goods_id"),
            (formal_doc or {}).get("goods_id"),
            (formal_doc or {}).get("product_id"),
        ):
            text = str(raw_id or "").strip()
            if not text:
                continue
            canon = extract_canonical_goods_id("taobao", text)
            if canon:
                out_ids.add(canon)
            elif text.isdigit():
                out_ids.add(text)
        return out_ids, False

    for raw_id in (
        row.get("resolved_product_id"),
        row.get("candidate_goods_id"),
        main_link.get("goods_id"),
        (formal_doc or {}).get("goods_id"),
        (formal_doc or {}).get("product_id"),
    ):
        text = str(raw_id or "").strip()
        if text:
            out_ids.add(text)
    return out_ids, False


def resolve_deal_display_window(
    row: Dict[str, Any],
    formal_doc: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """解析爆料在正式商品上的生效展示时间窗 `[start_ts, end_ts)`（或活跃态闭区间至当前）。"""
    cur_ts = float(now_ts) if now_ts is not None else datetime.now(timezone.utc).timestamp()
    start_ts = _dt_to_utc_ts(row.get("display_started_at"))
    if start_ts is None or start_ts <= 0:
        return {
            "display_status": "none",
            "start_ts": None,
            "end_ts": None,
            "inclusive_end": False,
            "duration_seconds": 0.0,
            "duration_text": "",
            "started_str": "",
            "ended_str": "",
        }

    max_end = start_ts + _MAX_EXPIRE_SECONDS
    explicit_end_ts = _dt_to_utc_ts(row.get("display_ended_at"))
    if explicit_end_ts is not None:
        end_ts = max(start_ts, explicit_end_ts)
        status = "replaced" if str(row.get("replaced_by_item_id") or "").strip() else "expired"
        inclusive_end = False
    else:
        row_site = str(row.get("source_site") or USER_SUBMIT_SITE).strip()
        row_id = str(row.get("source_item_id") or "").strip()
        di = (formal_doc or {}).get("deal_info") if isinstance((formal_doc or {}).get("deal_info"), dict) else {}
        di_site = str(di.get("source_site") or "").strip()
        di_id = str(di.get("source_item_id") or "").strip()
        same_on_formal = bool(di_site and di_id and di_site == row_site and di_id == row_id)

        if same_on_formal and di.get("is_deal_tip") is True:
            if cur_ts < max_end:
                end_ts = max(start_ts, cur_ts)
                status = "active"
                inclusive_end = True
            else:
                end_ts = max_end
                status = "expired"
                inclusive_end = False
        else:
            if same_on_formal:
                cand_ts = _dt_to_utc_ts(di.get("expired_at")) or _dt_to_utc_ts(di.get("updated_at")) or max_end
            else:
                cand_ts = _dt_to_utc_ts(di.get("updated_at")) or max_end
            end_ts = min(max_end, max(start_ts, cand_ts))
            status = "expired"
            inclusive_end = False

    duration_secs = max(0.0, end_ts - start_ts)
    return {
        "display_status": status,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "inclusive_end": inclusive_end,
        "duration_seconds": int(round(duration_secs)),
        "duration_text": _format_duration_cn(duration_secs),
        "started_str": _format_bj_dt(start_ts),
        "ended_str": "" if status == "active" else _format_bj_dt(end_ts),
    }


def _empty_commission_stats(*, jd_sku_unmapped: bool = False, attribution_error: bool = False) -> Dict[str, Any]:
    return {
        "valid_order_count": 0,
        "settled_order_count": 0,
        "pending_order_count": 0,
        "after_sale_order_count": 0,
        "excluded_refund_invalid_count": 0,
        "excluded_channel_count": 0,
        "estimated_commission_yuan": 0.0,
        "settled_commission_yuan": 0.0,
        "pending_commission_yuan": 0.0,
        "after_sale_commission_yuan": 0.0,
        "jd_sku_unmapped": jd_sku_unmapped,
        "attribution_error": attribution_error,
        "recent_orders": [],
    }


def _empty_report_summary(*, attribution_error: bool = False) -> Dict[str, Any]:
    return {
        "total_submissions": 0,
        "displayed_count": 0,
        "active_count": 0,
        "valid_order_count": 0,
        "settled_order_count": 0,
        "pending_order_count": 0,
        "after_sale_order_count": 0,
        "excluded_refund_invalid_count": 0,
        "excluded_channel_count": 0,
        "estimated_commission_yuan": 0.0,
        "settled_commission_yuan": 0.0,
        "pending_commission_yuan": 0.0,
        "after_sale_commission_yuan": 0.0,
        "attribution_error": attribution_error,
    }


def _batch_load_formal_docs(
    formal_col: Any,
    keys: Iterable[Tuple[str, str]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    unique_keys = list(dict.fromkeys((p, pid) for (p, pid) in keys if p and pid))
    if formal_col is None or not unique_keys:
        return {}
    proj = {
        "_id": 0,
        "platform": 1,
        "product_id": 1,
        "goods_id": 1,
        "goods_sign": 1,
        "image_url": 1,
        "product_url": 1,
        "deal_info.is_deal_tip": 1,
        "deal_info.source_site": 1,
        "deal_info.source_item_id": 1,
        "deal_info.updated_at": 1,
        "deal_info.expired_at": 1,
    }
    docs_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for start in range(0, len(unique_keys), 100):
        chunk = unique_keys[start : start + 100]
        or_clauses = [{"platform": p, "product_id": pid} for p, pid in chunk]
        for doc in formal_col.find({"$or": or_clauses}, proj):
            if not isinstance(doc, dict):
                continue
            dp = normalize_platform(doc.get("platform") or "", default="")
            dpid = str(doc.get("product_id") or "").strip()
            if dp and dpid:
                docs_by_key[(dp, dpid)] = doc
    return docs_by_key


def compute_user_deals_commission_report(
    rows: List[Dict[str, Any]],
    *,
    order_repo: Any,
    formal_col: Any = None,
    default_jd_position_id: str = "",
    now_ts: Optional[float] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """批量计算用户爆料展示时长及生效期站长默认链接佣金汇总（按单防重、零浮点误差）。"""
    summary = _empty_report_summary()
    summary["total_submissions"] = len(rows or ())
    if not rows:
        return {}, summary

    cur_ts = float(now_ts) if now_ts is not None else datetime.now(timezone.utc).timestamp()
    product_keys: List[Tuple[str, str]] = []
    row_identities: Dict[str, Tuple[str, str]] = {}
    for row in rows:
        tip_id = str(row.get("source_item_id") or "").strip()
        plat = normalize_platform(row.get("resolved_platform") or row.get("platform") or "", default="")
        pid = str(row.get("resolved_product_id") or row.get("candidate_goods_id") or "").strip()
        if tip_id:
            row_identities[tip_id] = (plat, pid)
        if row.get("display_started_at") is not None and plat and pid:
            product_keys.append((plat, pid))

    formal_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        formal_map = _batch_load_formal_docs(formal_col, product_keys)
    except Exception as exc:
        logger.warning("⚠️ 【爆料归因】批量查询正式商品表异常: %s", format_exc_summary(exc))

    per_item: Dict[str, Dict[str, Any]] = {}
    goods_ids_by_plat: Dict[str, Set[str]] = {}
    min_start_by_plat: Dict[str, float] = {}
    row_context: List[Tuple[str, Dict[str, Any], Dict[str, Any], Set[str], bool]] = []

    for row in rows:
        tip_id = str(row.get("source_item_id") or "").strip()
        if not tip_id:
            continue
        plat, pid = row_identities.get(tip_id, ("", ""))
        formal_doc = formal_map.get((plat, pid))
        window = resolve_deal_display_window(row, formal_doc, now_ts=cur_ts)
        goods_ids, jd_unmapped = extract_deal_order_goods_ids(row, formal_doc)
        if window["display_status"] != "none":
            summary["displayed_count"] += 1
            if window["display_status"] == "active":
                summary["active_count"] += 1
            if plat and goods_ids and window["start_ts"] is not None:
                goods_ids_by_plat.setdefault(plat, set()).update(goods_ids)
                prev_min = min_start_by_plat.get(plat)
                st = float(window["start_ts"])
                if prev_min is None or st < prev_min:
                    min_start_by_plat[plat] = st
        row_context.append((tip_id, row, window, goods_ids, jd_unmapped))

    orders_by_plat_gid: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    attribution_error = False
    if order_repo is not None and goods_ids_by_plat:
        for plat, gids in goods_ids_by_plat.items():
            try:
                min_ts = max(0.0, min_start_by_plat[plat] - 86400.0) if plat in min_start_by_plat else None
                fetched = order_repo.find_orders_for_deal_attribution(plat, sorted(gids), min_timestamp=min_ts)
                for ord_doc in fetched or ():
                    if not isinstance(ord_doc, dict):
                        continue
                    ogid = str(ord_doc.get("goods_id") or "").strip()
                    if ogid:
                        orders_by_plat_gid.setdefault((plat, ogid), []).append(ord_doc)
                    if plat == "jd":
                        ocgid = (
                            str(ord_doc.get("canonical_goods_id") or "").strip()
                            or extract_jd_image_key(ord_doc.get("goods_thumbnail_url"))
                        )
                        if ocgid and ocgid != ogid:
                            orders_by_plat_gid.setdefault((plat, ocgid), []).append(ord_doc)
            except Exception as exc:
                attribution_error = True
                logger.warning("⚠️ 【爆料归因】查询平台[%s]订单失败: %s", plat, format_exc_summary(exc))

    summary["attribution_error"] = attribution_error
    seen_summary_orders: Dict[Tuple[str, str], Tuple[str, Decimal]] = {}
    seen_summary_excluded_ch: Set[Tuple[str, str]] = set()
    summary_excluded_ref = 0

    for tip_id, row, window, goods_ids, jd_unmapped in row_context:
        plat = normalize_platform(row.get("resolved_platform") or row.get("platform") or "", default="")
        stats = _empty_commission_stats(jd_sku_unmapped=jd_unmapped, attribution_error=attribution_error)
        start_ts = window["start_ts"]
        end_ts = window["end_ts"]
        inclusive_end = window["inclusive_end"]

        if start_ts is not None and end_ts is not None and goods_ids and plat:
            est_sum = _ZERO
            settled_sum = _ZERO
            pending_sum = _ZERO
            after_sale_sum = _ZERO
            audit_rows: List[Tuple[float, Dict[str, Any]]] = []
            seen_item_sn: Set[str] = set()

            for gid in goods_ids:
                for ord_doc in orders_by_plat_gid.get((plat, gid), ()):
                    raw_sn = str(ord_doc.get("order_sn") or "").strip()
                    dedup_item_key = raw_sn or f"_obj_{id(ord_doc)}"
                    if dedup_item_key in seen_item_sn:
                        continue

                    evt_ts = extract_order_event_ts(ord_doc)
                    if evt_ts is None:
                        continue
                    if evt_ts < start_ts:
                        continue
                    if inclusive_end:
                        if evt_ts > end_ts:
                            continue
                    else:
                        if evt_ts >= end_ts:
                            continue

                    seen_item_sn.add(dedup_item_key)
                    global_order_key = (plat, dedup_item_key)

                    if not is_site_owner_default_order(ord_doc, plat, default_jd_position_id=default_jd_position_id):
                        stats["excluded_channel_count"] += 1
                        seen_summary_excluded_ch.add(global_order_key)
                        continue

                    bucket, comm_dec = classify_order_commission_bucket(ord_doc, plat)
                    if bucket == "excluded":
                        stats["excluded_refund_invalid_count"] += 1
                        if global_order_key not in seen_summary_orders:
                            seen_summary_orders[global_order_key] = ("excluded", comm_dec)
                        continue

                    if bucket == "after_sale":
                        stats["after_sale_order_count"] += 1
                        after_sale_sum += comm_dec
                    elif bucket == "settled":
                        stats["valid_order_count"] += 1
                        stats["settled_order_count"] += 1
                        est_sum += comm_dec
                        settled_sum += comm_dec
                    else:
                        stats["valid_order_count"] += 1
                        stats["pending_order_count"] += 1
                        est_sum += comm_dec
                        pending_sum += comm_dec

                    if global_order_key not in seen_summary_orders or seen_summary_orders[global_order_key][0] == "excluded":
                        seen_summary_orders[global_order_key] = (bucket, comm_dec)

                    ord_amt_dec = _to_decimal_money(ord_doc.get("order_amount_yuan")) or _ZERO
                    audit_rows.append((
                        evt_ts,
                        {
                            "order_sn": _mask_order_sn(raw_sn),
                            "order_time_str": _format_bj_dt(evt_ts),
                            "order_amount_yuan": _money_float(ord_amt_dec),
                            "commission_yuan": _money_float(comm_dec),
                            "order_status_desc": str(ord_doc.get("order_status_desc") or "有效订单").strip(),
                            "bucket": bucket,
                            "goods_quantity": int(ord_doc.get("goods_quantity") or 1),
                        },
                    ))

            audit_rows.sort(key=lambda x: x[0], reverse=True)
            stats["recent_orders"] = [entry for _, entry in audit_rows[:_RECENT_ORDER_LIMIT]]
            stats["estimated_commission_yuan"] = _money_float(est_sum)
            stats["settled_commission_yuan"] = _money_float(settled_sum)
            stats["pending_commission_yuan"] = _money_float(pending_sum)
            stats["after_sale_commission_yuan"] = _money_float(after_sale_sum)

        per_item[tip_id] = {
            "display_started_at": row.get("display_started_at"),
            "display_ended_at": row.get("display_ended_at"),
            "display_started_str": window["started_str"],
            "display_ended_str": window["ended_str"],
            "display_status": window["display_status"],
            "display_duration_seconds": window["duration_seconds"],
            "display_duration_text": window["duration_text"],
            "commission_stats": stats,
        }

    sum_est = _ZERO
    sum_settled = _ZERO
    sum_pending = _ZERO
    sum_after_sale = _ZERO
    for bucket, comm_dec in seen_summary_orders.values():
        if bucket == "excluded":
            summary_excluded_ref += 1
        elif bucket == "after_sale":
            summary["after_sale_order_count"] += 1
            sum_after_sale += comm_dec
        elif bucket == "settled":
            summary["valid_order_count"] += 1
            summary["settled_order_count"] += 1
            sum_est += comm_dec
            sum_settled += comm_dec
        else:
            summary["valid_order_count"] += 1
            summary["pending_order_count"] += 1
            sum_est += comm_dec
            sum_pending += comm_dec

    summary["excluded_refund_invalid_count"] = summary_excluded_ref
    summary["excluded_channel_count"] = len(seen_summary_excluded_ch)
    summary["estimated_commission_yuan"] = _money_float(sum_est)
    summary["settled_commission_yuan"] = _money_float(sum_settled)
    summary["pending_commission_yuan"] = _money_float(sum_pending)
    summary["after_sale_commission_yuan"] = _money_float(sum_after_sale)
    return per_item, summary
