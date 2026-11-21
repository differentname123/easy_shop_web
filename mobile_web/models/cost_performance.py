# -*- coding: utf-8 -*-
"""纯领域性价比计算模型：单位度量衡映射、规格校验、鲜活线报优先级与跨单位轮询排序。"""
import datetime
import math
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from bson.decimal128 import Decimal128

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
DEAL_FRESH_WINDOW_SECONDS = 12 * 3600
PLAIN_SKU_PRICE_RE = re.compile(r"^[￥¥]?\s*(\d+(?:\.\d+)?)\s*$")

SKU_UNIT_BASE_MAPPING: Dict[str, Dict[str, float]] = {
    "质量": {
        "mg": 1,
        "g": 1000,
        "G": 1000,
        "克": 1000,
        "斤": 500000,
        "kg": 1000000,
        "Kg": 1000000,
        "KG": 1000000,
        "千克": 1000000,
        "磅": 453592.37,
    },
    "容积与体积": {
        "ml": 1,
        "mL": 1,
        "ML": 1,
        "毫升": 1,
        "L": 1000,
        "升": 1000,
    },
    "数据存储": {
        "GB": 1,
        "TB": 1024,
    },
    "电池容量": {
        "mAh": 1,
        "Ah": 1000,
        "AH": 1000,
    },
    "功率": {
        "W": 1,
        "kW": 1000,
    },
    "生物活性成分": {
        "iu": 1,
        "IU": 1,
    },
    "速率与排量": {
        "L/日": 1,
        "升/天": 1,
    },
}

UNIT_TO_CATEGORY: Dict[str, str] = {}
UNIT_TO_MULTIPLIER: Dict[str, float] = {}
for _cat, _units in SKU_UNIT_BASE_MAPPING.items():
    for _unit, _mul in _units.items():
        UNIT_TO_CATEGORY[_unit] = _cat
        UNIT_TO_MULTIPLIER[_unit] = _mul


def _first_present(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """缺失或为 None 时回退；0、False、空字符串/集合视作已知值。"""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _amount_float(value: Any) -> float:
    """兼容数值、数值字符串及 BSON Decimal128 金额。"""
    return float(value.to_decimal() if isinstance(value, Decimal128) else value)


def is_specless_single_sku(sku_info: Any) -> bool:
    """判定商品是否属于无细分规格属性的单品（成交价统一以顶层 activity_price 为准）。"""
    if not isinstance(sku_info, list) or len(sku_info) == 0:
        return True
    if len(sku_info) == 1:
        item = sku_info[0]
        if not isinstance(item, dict):
            return True
        return not any(
            k not in ("价格", "price", "截图地址", "image_url", "img") and str(v or "").strip()
            for k, v in item.items()
        )
    return False


def parse_plain_sku_price(price_text: Any) -> Optional[float]:
    """解析纯数值规格价格（如 '￥41.4' / 41.4）；区间或多档文案返回 None。"""
    if price_text is None or isinstance(price_text, bool):
        return None
    if isinstance(price_text, (int, float, Decimal128)):
        try:
            val = _amount_float(price_text)
            return val if math.isfinite(val) and val > 0 else None
        except (ValueError, TypeError, OverflowError):
            return None
    match = PLAIN_SKU_PRICE_RE.fullmatch(str(price_text).strip())
    if not match:
        return None
    try:
        val = float(match.group(1))
        return val if math.isfinite(val) and val > 0 else None
    except (ValueError, OverflowError):
        return None


def is_valid_sku_price(
    price_val: Any,
    min_limit_price: Optional[float],
    *,
    exact_match: bool = False,
) -> bool:
    """
    校验规格价格有效性：
    1. 必须为大于 0 的有限浮点数/整数/Decimal128；
    2. 单规格商品（exact_match=True）下，若存在外层 activity_price，规格价格必须与外层当前价格一致；
    3. 多规格模式下，任何规格价格不得低于全局 activity_price。
    """
    if isinstance(price_val, bool) or not isinstance(price_val, (int, float, Decimal128)):
        return False
    try:
        p_float = _amount_float(price_val)
    except (ValueError, TypeError, OverflowError):
        return False
    if not math.isfinite(p_float) or p_float <= 0:
        return False
    if min_limit_price is not None:
        if exact_match:
            if abs(round(p_float, 2) - round(float(min_limit_price), 2)) > 0.01:
                return False
        elif round(p_float, 2) < round(float(min_limit_price), 2):
            return False
    return True


def parse_pricing_basis_entry(
    sku_price: float,
    sku_fmt: Dict[str, Any],
) -> Tuple[float, str, Dict[str, float]]:
    """将单条清洗后的规格 format_info 转换为 (sku_price, raw_base_unit, extracted_units)。"""
    pricing_basis = sku_fmt.get("pricing_basis") or {}
    raw_structure = pricing_basis.get("structure")
    raw_base_unit = str(pricing_basis.get("base_unit") or "").strip() or "件"
    raw_total_val = pricing_basis.get("total_value")

    total_val_float = None
    if raw_total_val is not None and not isinstance(raw_total_val, bool):
        try:
            tv = float(raw_total_val)
            if math.isfinite(tv) and tv > 0:
                total_val_float = tv
        except (ValueError, TypeError):
            total_val_float = None

    extracted_units: Dict[str, float] = {}
    current_accum = 1.0
    if isinstance(raw_structure, list) and raw_structure:
        for struct_item in raw_structure:
            if not isinstance(struct_item, dict):
                continue
            unit = str(struct_item.get("unit") or "").strip()
            raw_v = struct_item.get("value")
            val_float = 1.0
            if raw_v is not None and not isinstance(raw_v, bool):
                try:
                    vf = float(raw_v)
                    if math.isfinite(vf) and vf > 0:
                        val_float = vf
                except (ValueError, TypeError):
                    val_float = 1.0
            current_accum *= val_float
            if unit:
                extracted_units[unit] = current_accum

    if raw_base_unit:
        final_val = total_val_float if total_val_float is not None else current_accum
        extracted_units[raw_base_unit] = final_val

    if not extracted_units:
        extracted_units["件"] = 1.0

    return (sku_price, raw_base_unit, extracted_units)


def extract_sku_unit_entries(doc: Dict[str, Any]) -> List[Tuple[float, str, Dict[str, float]]]:
    """
    从商品的 format_info 中提取所有合法规格的 (sku_price, raw_base_unit, extracted_units)。
    尚未完成 format_info 清洗、标记 need_reformat 或外层价格已变但尚未重洗的商品返回空列表（归入末尾兜底组）。
    """
    if doc.get("need_reformat") is True:
        return []
    if "format_status" in doc and doc.get("format_status") not in (None, "success"):
        return []
    raw_format_info = doc.get("format_info")
    if not isinstance(raw_format_info, dict) or not raw_format_info:
        return []

    raw_act = _first_present(doc, "activity_price", "price")
    min_act_price = None
    if raw_act is not None and not isinstance(raw_act, bool):
        try:
            act_f = _amount_float(raw_act)
            if math.isfinite(act_f) and act_f > 0:
                min_act_price = float(act_f)
        except (ValueError, TypeError, OverflowError):
            min_act_price = None

    sku_info_raw = doc.get("sku_info")
    is_specless = is_specless_single_sku(sku_info_raw)

    raw_fmt_act = doc.get("format_activity_price")
    if is_specless and min_act_price is not None and raw_fmt_act is not None and not isinstance(raw_fmt_act, bool):
        try:
            fmt_act_f = _amount_float(raw_fmt_act)
        except (ValueError, TypeError, OverflowError):
            return []
        if not math.isfinite(fmt_act_f) or fmt_act_f <= 0 or abs(min_act_price - fmt_act_f) > 0.01:
            return []

    raw_sku_list: List[Tuple[float, Dict[str, Any]]] = []
    if "pricing_basis" in raw_format_info and isinstance(raw_format_info.get("pricing_basis"), dict):
        sku_price = raw_format_info.get("price")
        if is_valid_sku_price(sku_price, min_act_price, exact_match=is_specless):
            raw_sku_list.append((_amount_float(sku_price), raw_format_info))
    else:
        sku_info_list = sku_info_raw if isinstance(sku_info_raw, list) else []
        product_id = str(doc.get("product_id") or doc.get("goods_id") or doc.get("goods_sign") or "").strip()
        stale_sku_ids = set()
        if not is_specless and sku_info_list and product_id:
            snap_map = doc.get("format_sku_prices") if isinstance(doc.get("format_sku_prices"), dict) else {}
            for idx, sku_item in enumerate(sku_info_list, start=1):
                if not isinstance(sku_item, dict):
                    continue
                sku_id = f"{product_id}_{idx}"
                has_cn_price = "价格" in sku_item and sku_item.get("价格") is not None
                cur_p_str = str(sku_item.get("价格") or "").strip() if has_cn_price else ""
                if sku_id in snap_map and has_cn_price:
                    if str(snap_map[sku_id]).strip() != cur_p_str:
                        stale_sku_ids.add(sku_id)
                        continue
                sku_fmt_entry = raw_format_info.get(sku_id)
                if isinstance(sku_fmt_entry, dict):
                    cur_num = (
                        parse_plain_sku_price(cur_p_str)
                        if has_cn_price
                        else parse_plain_sku_price(sku_item.get("price"))
                    )
                    if cur_num is not None:
                        try:
                            fmt_p = _amount_float(sku_fmt_entry.get("price"))
                        except (ValueError, TypeError, OverflowError):
                            fmt_p = None
                        if fmt_p is None or not math.isfinite(fmt_p) or fmt_p <= 0 or abs(cur_num - fmt_p) > 0.01:
                            stale_sku_ids.add(sku_id)

        if sku_info_list and product_id:
            for idx in range(1, len(sku_info_list) + 1):
                sku_id = f"{product_id}_{idx}"
                if sku_id in stale_sku_ids:
                    continue
                sku_fmt = raw_format_info.get(sku_id)
                if not isinstance(sku_fmt, dict) or not isinstance(sku_fmt.get("pricing_basis"), dict):
                    continue
                sku_price = sku_fmt.get("price")
                if is_valid_sku_price(sku_price, min_act_price, exact_match=is_specless):
                    raw_sku_list.append((_amount_float(sku_price), sku_fmt))

        if not raw_sku_list:
            for sku_key, sku_fmt in raw_format_info.items():
                if sku_key in stale_sku_ids:
                    continue
                if isinstance(sku_fmt, dict) and isinstance(sku_fmt.get("pricing_basis"), dict):
                    sku_price = sku_fmt.get("price")
                    if is_valid_sku_price(sku_price, min_act_price, exact_match=is_specless):
                        raw_sku_list.append((_amount_float(sku_price), sku_fmt))

    return [parse_pricing_basis_entry(sku_price, sku_fmt) for sku_price, sku_fmt in raw_sku_list]


def parse_deal_publish_timestamp(val: Any) -> Optional[float]:
    """将线报 publish_time 解析为秒级 Unix 时间戳；naive datetime 按 UTC（Mongo 契约），无时区字符串按北京时间。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime.datetime):
        dt = val.replace(tzinfo=datetime.timezone.utc) if val.tzinfo is None else val
        return dt.timestamp()
    if isinstance(val, (int, float)):
        ts = float(val)
        if not math.isfinite(ts) or ts <= 0:
            return None
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        if "/" in s[:10]:
            s = s.replace("/", "-", 2)
        try:
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            return dt.timestamp()
        except ValueError:
            pass
        for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
            try:
                return datetime.datetime.strptime(s[:width], fmt).replace(tzinfo=CN_TZ).timestamp()
            except ValueError:
                continue
    return None


def extract_fresh_deal_unit_entries(
    doc: Dict[str, Any],
    *,
    now_ts: Optional[float] = None,
) -> List[Tuple[float, str, Dict[str, float]]]:
    """
    若商品附带 12 小时内发布的有效爆料（is_deal_tip=True）且线报已完成结构化清洗（deal_info.format_info），
    直接采用爆料清洗结构与爆料到手价计算性价比；否则返回空列表回退外层官方数据。
    """
    deal_info = doc.get("deal_info")
    if not isinstance(deal_info, dict) or deal_info.get("is_deal_tip") is not True:
        return []
    if deal_info.get("need_reformat") is True or deal_info.get("format_status") != "success":
        return []
    deal_fmt = deal_info.get("format_info")
    if not isinstance(deal_fmt, dict) or not isinstance(deal_fmt.get("pricing_basis"), dict):
        return []

    pub_ts = parse_deal_publish_timestamp(deal_info.get("publish_time"))
    cur_ts = float(now_ts) if now_ts is not None else time.time()
    if pub_ts is None or (cur_ts - pub_ts) > DEAL_FRESH_WINDOW_SECONDS or (pub_ts - cur_ts) > 300.0:
        return []

    raw_dp = deal_info.get("deal_price")
    if raw_dp is None or isinstance(raw_dp, bool):
        return []
    try:
        deal_price = _amount_float(raw_dp)
    except (ValueError, TypeError, OverflowError):
        return []
    if not math.isfinite(deal_price) or deal_price <= 0:
        return []

    for snap_val in (deal_info.get("format_deal_price"), deal_fmt.get("price")):
        if snap_val is not None:
            if isinstance(snap_val, bool):
                return []
            try:
                snap_f = _amount_float(snap_val)
            except (ValueError, TypeError, OverflowError):
                return []
            if not math.isfinite(snap_f) or snap_f <= 0 or abs(round(deal_price, 2) - round(snap_f, 2)) > 0.01:
                return []

    return [parse_pricing_basis_entry(deal_price, deal_fmt)]


def rank_products_by_cost_performance(
    docs: Sequence[Dict[str, Any]],
    *,
    normalize_summary: Callable[[Dict[str, Any]], Dict[str, Any]],
    now_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    按性价比对候选商品文档列表进行排序并输出规范化摘要列表：
    1. 12 小时内已清洗爆料优先按爆料单位结构计算性价比，否则回退官方 format_info；
    2. 同类度量衡按频次统一目标单位（如 kg/斤/克 -> g），多 SKU 取最优性价比；
    3. 各主单位分组内按性价比从高到低排序，并按单位数量比例轮询交织输出；
    4. 未完成 format_info 清洗的商品按原序列顺序排在末尾兜底。
    """
    formatted_candidates: List[Tuple[Dict[str, Any], List[Tuple[float, str, Dict[str, float]]]]] = []
    unformatted_items: List[Dict[str, Any]] = []
    category_unit_counts: Dict[str, Dict[str, int]] = {}
    cur_ts = float(now_ts) if now_ts is not None else time.time()

    for doc in docs:
        summary = normalize_summary(doc)
        sku_entries = (
            extract_fresh_deal_unit_entries(doc, now_ts=cur_ts)
            or extract_sku_unit_entries(doc)
        )
        if not sku_entries:
            unformatted_items.append(summary)
            continue

        formatted_candidates.append((summary, sku_entries))
        for _, _, extracted_units in sku_entries:
            for unit in extracted_units.keys():
                cat = UNIT_TO_CATEGORY.get(unit)
                if cat:
                    cat_counts = category_unit_counts.setdefault(cat, {})
                    cat_counts[unit] = cat_counts.get(unit, 0) + 1

    category_target_unit: Dict[str, str] = {
        cat: max(counts.items(), key=lambda x: x[1])[0]
        for cat, counts in category_unit_counts.items()
    }

    product_unit_scores: List[Tuple[str, Dict[str, float]]] = []
    active_base_units = set()

    for _, sku_entries in formatted_candidates:
        unit_best_scores: Dict[str, float] = {}
        best_primary_unit = "件"
        best_primary_score = -1.0

        for sku_price, raw_base_unit, extracted_units in sku_entries:
            normalized_units: Dict[str, float] = {}
            for unit, val in extracted_units.items():
                cat = UNIT_TO_CATEGORY.get(unit)
                final_u = unit
                final_val = val
                if cat and cat in category_target_unit:
                    tgt_u = category_target_unit[cat]
                    if unit != tgt_u:
                        final_val = val * (UNIT_TO_MULTIPLIER[unit] / UNIT_TO_MULTIPLIER[tgt_u])
                    final_u = tgt_u
                normalized_units[final_u] = round(final_val, 4) if isinstance(final_val, float) else final_val

            cat = UNIT_TO_CATEGORY.get(raw_base_unit)
            primary_base_unit = category_target_unit[cat] if (cat and cat in category_target_unit) else raw_base_unit
            if primary_base_unit not in normalized_units:
                primary_base_unit = next(iter(normalized_units.keys()), "件")
            active_base_units.add(primary_base_unit)

            for unit, raw_val in normalized_units.items():
                val_num = float(raw_val) if raw_val and raw_val > 0 else 1.0
                score = round(val_num / sku_price, 6) if sku_price and sku_price > 0 else 0.0
                if score > unit_best_scores.get(unit, -1.0):
                    unit_best_scores[unit] = score
                if unit == primary_base_unit and score > best_primary_score:
                    best_primary_score = score
                    best_primary_unit = primary_base_unit

        product_unit_scores.append((best_primary_unit, unit_best_scores))

    unit_groups: Dict[str, List[Tuple[float, int, int]]] = {}
    for idx, (best_primary_unit, unit_best_scores) in enumerate(product_unit_scores):
        matched_any = False
        for unit, score in unit_best_scores.items():
            if unit in active_base_units:
                unit_groups.setdefault(unit, []).append((score, -idx, idx))
                matched_any = True
        if not matched_any:
            score = unit_best_scores.get(best_primary_unit, 0.0)
            unit_groups.setdefault(best_primary_unit, []).append((score, -idx, idx))

    for group in unit_groups.values():
        group.sort(key=lambda x: (x[0], x[1]), reverse=True)

    ordered_units = sorted(
        unit_groups.keys(),
        key=lambda unit: (len(unit_groups[unit]), unit_groups[unit][0][0]),
        reverse=True,
    )

    sorted_items: List[Dict[str, Any]] = []
    if ordered_units:
        counts = [len(unit_groups[unit]) for unit in ordered_units]
        min_count = max(1, min(counts))
        max_count = max(counts)
        base_step = float(min_count) if (max_count / min_count) <= 5 else (max_count / 5.0)
        unit_quotas = {
            unit: max(1, int(len(unit_groups[unit]) / base_step + 0.5))
            for unit in ordered_units
        }

        unit_cursors = {unit: 0 for unit in ordered_units}
        emitted_indices = set()
        total_formatted = len(formatted_candidates)

        while len(emitted_indices) < total_formatted:
            progressed = False
            for unit in ordered_units:
                group = unit_groups[unit]
                cursor = unit_cursors[unit]
                quota = unit_quotas[unit]
                taken = 0
                while cursor < len(group) and taken < quota:
                    _, _, cand_idx = group[cursor]
                    cursor += 1
                    if cand_idx in emitted_indices:
                        continue
                    emitted_indices.add(cand_idx)
                    sorted_items.append(formatted_candidates[cand_idx][0])
                    taken += 1
                    progressed = True
                unit_cursors[unit] = cursor
            if not progressed:
                break

    sorted_items.extend(unformatted_items)
    return sorted_items
