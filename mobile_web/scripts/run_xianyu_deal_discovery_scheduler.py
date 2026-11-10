# -*- coding: utf-8 -*-
"""
闲鱼高性价比商品探测与比价独立定时任务（三步 AI 严选标品与详情含邮精准比价版）
================================================================================
执行流程（简洁、稳定、可追溯）：
  1. 【源头过滤与分类（仅针对未比价过的商品）】：
     - 从 MongoDB `products` 读取最近 72h 更新、`channel_type` 存在且有值、`format_status == "success"` 的商品；
     - 过滤掉已在 `xianyu_deal_evaluations` 中存在比价记录的商品（确保只对尚未比价过的商品进行比价）；
     - 分类依据：保持全部分类（直接取 `format_info` 中 `core_entities` 得分 `score` 最高的 `name`，不合并小分类）。
  2. 【AI 第 1 步：类内前 20 商品选出 1 个高性价比标品 + 生成同款搜索词 + 官方实时验价】：
     - 在每个分类内计算各商品在各个 `unit` 下的性价比值：`unit_cost_performance[u] = unit_quantity[u] / activity_price`；
     - 筛选出该分类下排名前 20 的候选商品，交给【第 1 个 AI】（使用 `medium` 模型组 + `闲鱼搜索关键字生成.txt`）：
       ① 判断该类别候选商品中是否存在可在闲鱼进行「同款一对一比价」的标品（剔除卫衣/连衣裙等非标服饰及无规格杂货）；若无标品则直接跳过该分类；
       ② 从可比标品中选出性价比最高、规格最清晰的 1 个标品（`selected_product_id`）；
       ③ 生成用于在闲鱼搜索该同款标品的主搜索词 `search_keyword` 与备用搜索词 `backup_search_keyword`；
     - 调用官方接口 `get_goods_detail` 验证所选标品的实时 `activity_price`（若已下架或优惠失效暴涨，则剔除后顺延重选）。
  3. 【AI 第 2 步：闲鱼搜索 20 条概览数据 -> AI 初筛同款可比商品 ID 列表】：
     - 调用 `search_xianyu_items` 搜索最多 20 条闲鱼商品概览数据（若结果 < 3 条自动用备用词扩召回）；
     - 将【我方标品信息】与【20 条闲鱼概览数据】交给【第 2 个 AI】（使用 `medium` 模型组 + `闲鱼同款商品初筛.txt`），
       筛选出所有与我方商品属于「同品牌、同款/同系列」且具备可比性的闲鱼商品 ID 列表 `comparable_item_ids`。
  4. 【AI 第 3 步：逐一拉取同款候选的完整详情/多规格价格/邮费 -> AI 终审含邮精准比价】：
     - 对第 2 步筛出的每个 `comparable_item_ids`，依次调用 `get_xianyu_item_detail` 拉取完整正文 `desc`、多规格列表 `sku_list`（含各规格真实售价与库存）及明确运费 `transport_fee`；
     - 将【我方标品信息】与【附带完整详情、SKU 价格及运费的同款闲鱼商品列表】交给【第 3 个 AI】（使用 `medium` 模型组 + `闲鱼同款比价与性价比计算.txt`）：
       ① 剔除无具体规格、邮费不明、非同款、二手或无货的商品；
       ② 基于「有效在售 SKU 真实售价 + 运费 `transport_fee`」核算真实可比含邮总价，选出性价比最高的 1 个闲鱼商品 `best_xianyu_item_id`；
       ③ 返回匹配的各个 `unit` 的数量与性价比值 `xianyu_unit_cost_performance` 及胜负结论 `llm_winner`、`llm_reason`；
     - 强校验返回的 `best_xianyu_item_id` 与字段合法性（支持最多 3 次重试），计算性价比比值并完整写入 MongoDB `xianyu_deal_evaluations`。
================================================================================
"""

import argparse
import datetime
import json
import logging
import math
import os
import sys
import time
import unicodedata
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Set, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
for _p in (PROJECT_ROOT, WORKSPACE_ROOT, CURRENT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from adapters import get_adapter, normalize_platform
from infrastructure.ai_api.model_api import generate_content
from infrastructure.mongo import gen_db_object
from mobile_web.scripts.goods_info_format import read_file_to_str, string_to_object
from mobile_web.scripts.publish_to_xianyu import (
    DEFAULT_GUEST_USER_DATA_DIR,
    get_xianyu_item_detail,
    search_xianyu_items,
)
from repositories.product_repo import (
    ProductRepository,
    UNIT_TO_CATEGORY,
    UNIT_TO_MULTIPLIER,
)

EVAL_COLLECTION_NAME = "xianyu_deal_evaluations"
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "xianyu_deal_discovery.log")
PROMPT_DIR = os.path.join(PROJECT_ROOT, "prompt")
KEYWORD_PROMPT_PATH = os.path.join(PROMPT_DIR, "闲鱼搜索关键字生成.txt")
COARSE_FILTER_PROMPT_PATH = os.path.join(PROMPT_DIR, "闲鱼同款商品初筛.txt")
COMPARISON_PROMPT_PATH = os.path.join(PROMPT_DIR, "闲鱼同款比价与性价比计算.txt")

LLM_MAX_ATTEMPTS = 3
MAX_CATEGORY_CANDIDATES_FOR_AI1 = 20

# 单位精确度优先级：物理度量衡 > 精细计数单位 > 包装计数单位 > 模糊容器/兜底单位
PRECISE_PHYSICAL_UNITS = {"g", "kg", "斤", "两", "mg", "ml", "l", "L", "m", "cm", "mm"}
PRECISE_COUNT_UNITS = {"抽", "张", "片", "粒", "颗", "枚", "只", "双", "支", "根", "条", "节", "卷"}
PACK_UNITS = {"包", "袋", "板", "排", "组", "提"}
VAGUE_CONTAINER_UNITS = {"件", "套", "份", "单", "个", "瓶", "盒", "罐", "桶", "箱", "盆", "碗", "杯"}


def _unit_priority_score(unit: str) -> int:
    u = str(unit or "").strip()
    if u in PRECISE_PHYSICAL_UNITS:
        return 4
    if u in PRECISE_COUNT_UNITS:
        return 3
    if u in PACK_UNITS:
        return 2
    if u in VAGUE_CONTAINER_UNITS:
        return 0
    return 1


def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("xianyu_deal_discovery")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.propagate = False
    return lg


logger = _setup_logger()


def _num(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except Exception:
        return default


# ==============================================================================
# 运维可读性日志排版工具（中英文等宽对齐 + 性价比/规格人类可读格式化）
# ==============================================================================
def _disp_width(text: str) -> int:
    """计算字符串在终端等宽字体下的显示列宽（中文全角/Emoji占2列，英文数字占1列，组合变体符占0列）。"""
    w = 0
    for ch in str(text or ""):
        cp = ord(ch)
        if cp in (0xFE0E, 0xFE0F, 0x200D) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        if (
            unicodedata.east_asian_width(ch) in ("W", "F")
            or (0x1F300 <= cp <= 0x1FAFF)
            or (0x2600 <= cp <= 0x27BF)
        ):
            w += 2
        else:
            w += 1
    return w


def _pad_col(val: Any, width: int, align: str = "left", truncate: bool = True) -> str:
    """按终端显示列宽对齐字段，保证中英文混排表格严格上下对齐。"""
    s = str(val if val is not None else "").replace("\r", " ").replace("\n", " ").strip()
    cur_w = _disp_width(s)
    if truncate and cur_w > width:
        out_chars: List[str] = []
        acc = 0
        target = max(1, width - 2)
        for ch in s:
            cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if acc + cw > target:
                break
            out_chars.append(ch)
            acc += cw
        s = "".join(out_chars) + ".."
        cur_w = _disp_width(s)
    pad_len = max(0, width - cur_w)
    if align == "right":
        return (" " * pad_len) + s
    return s + (" " * pad_len)


def _fmt_num_clean(v: Any) -> str:
    f = _num(v, 0.0)
    if f <= 0:
        return "0"
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    if f >= 100:
        return f"{f:.1f}".rstrip("0").rstrip(".")
    if f >= 1:
        return f"{f:.2f}".rstrip("0").rstrip(".")
    return f"{f:.4f}".rstrip("0").rstrip(".")


def _ordered_unit_keys(d: Dict[str, Any], focus_unit: Optional[str] = None) -> List[str]:
    if not isinstance(d, dict):
        return []
    keys = sorted(d.keys(), key=lambda u: _unit_priority_score(u), reverse=True)
    if focus_unit and focus_unit in keys:
        keys.remove(focus_unit)
        keys.insert(0, focus_unit)
    return keys


def _fmt_qty_human(qty_map: Dict[str, float], focus_unit: Optional[str] = None) -> str:
    if not isinstance(qty_map, dict) or not qty_map:
        return "-"
    keys = _ordered_unit_keys(qty_map, focus_unit)
    parts = [f"{_fmt_num_clean(qty_map[u])}{u}" for u in keys[:3]]
    return " / ".join(parts)


def _fmt_cp_human(cp_map: Dict[str, float], focus_unit: Optional[str] = None) -> str:
    if not isinstance(cp_map, dict) or not cp_map:
        return "-"
    keys = _ordered_unit_keys(cp_map, focus_unit)
    parts: List[str] = []
    for u in keys[:3]:
        prefix = "★" if (focus_unit and u == focus_unit and len(keys) > 1) else ""
        parts.append(f"{prefix}{_fmt_num_clean(cp_map[u])}{u}/元")
    return " | ".join(parts)


# ==============================================================================
# 步骤 1：源头过滤（仅未比价商品）、按最高分 core_entity 分类、类内前 20 交由 AI 选拔标品并验价
# ==============================================================================
def _extract_primary_sku_and_category(doc: Dict[str, Any], activity_price: float) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    从商品的 `format_info` 中提取主 SKU，并取 `core_entities` 中 `score` 最高的 `name` 作为分类。
    """
    fi = doc.get("format_info")
    if not isinstance(fi, dict) or not fi:
        return None

    valid_skus: List[Tuple[float, Dict[str, Any]]] = []
    for v in fi.values():
        if not isinstance(v, dict) or not isinstance(v.get("core_entities"), list):
            continue
        sku_p = _num(v.get("price"), activity_price)
        if round(sku_p, 2) >= round(activity_price, 2):
            valid_skus.append((abs(sku_p - activity_price), v))

    if not valid_skus:
        for v in fi.values():
            if isinstance(v, dict) and isinstance(v.get("core_entities"), list):
                valid_skus.append((0.0, v))
                break

    if not valid_skus:
        return None

    valid_skus.sort(key=lambda x: x[0])
    primary_sku = valid_skus[0][1]

    ces = [
        c for c in (primary_sku.get("core_entities") or [])
        if isinstance(c, dict) and str(c.get("name") or "").strip()
    ]
    if not ces:
        return None
    ces.sort(key=lambda c: _num(c.get("score"), 0.0), reverse=True)
    category_name = str(ces[0]["name"]).strip()
    return category_name, primary_sku


def _extract_raw_units(sku_fmt: Dict[str, Any]) -> Tuple[str, Dict[str, float]]:
    """从 SKU 的 `pricing_basis` 提取各层级的 `(unit: 数量)`。"""
    pb = sku_fmt.get("pricing_basis") if isinstance(sku_fmt.get("pricing_basis"), dict) else {}
    raw_structure = pb.get("structure")
    raw_base_unit = str(pb.get("base_unit") or "").strip() or "件"
    total_val = _num(pb.get("total_value"), 0.0)

    extracted: Dict[str, float] = {}
    accum = 1.0
    if isinstance(raw_structure, list) and raw_structure:
        for item in raw_structure:
            if not isinstance(item, dict):
                continue
            u = str(item.get("unit") or "").strip()
            v = _num(item.get("value"), 1.0)
            if v <= 0:
                v = 1.0
            accum *= v
            if u:
                extracted[u] = round(accum, 4)

    if raw_base_unit:
        extracted[raw_base_unit] = round(total_val if total_val > 0 else accum, 4)
    if not extracted:
        extracted["件"] = 1.0
    return raw_base_unit, extracted


def _compute_unit_cp(unit_quantities: Dict[str, float], activity_price: float) -> Dict[str, float]:
    """根据 `activity_price` 计算各个 unit 的性价比值 (`unit数量 / activity_price`)。"""
    if activity_price <= 0:
        return {}
    return {
        u: round(float(qty) / activity_price, 6)
        for u, qty in unit_quantities.items()
        if float(qty) > 0
    }


def _build_fallback_search_keywords(doc: Dict[str, Any], sku_fmt: Dict[str, Any], category_name: str) -> Tuple[str, str]:
    """当大模型搜索词字段缺失时的保底拼接规则。"""
    kws = sku_fmt.get("decision_keywords") or []
    brand = ""
    for kw in kws:
        if isinstance(kw, dict) and kw.get("attribute_name") == "品牌":
            val = str(kw.get("attribute_value") or "").strip()
            if val and val not in ("无", "其他", "无品牌", "品牌随机", "未知", "通用"):
                brand = val
                break
    if not brand:
        raw_b = str(doc.get("brand") or "").strip()
        if raw_b and not any(w in raw_b for w in ("店", "专营", "旗舰", "超市", "生活馆", "官方")):
            brand = raw_b.split("/")[-1].strip()

    base_parts: List[str] = []
    if brand and brand not in category_name:
        base_parts.append(brand)
    base_parts.append(category_name)
    kw_str = " ".join(base_parts).strip() or category_name
    return kw_str, category_name


def _select_top_candidates_for_category(
    cat_candidates: List[Dict[str, Any]],
    unit_groups: Dict[str, List[Tuple[float, int]]],
    max_count: int = MAX_CATEGORY_CANDIDATES_FOR_AI1,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    从当前分类的所有候选商品中，综合各主要单位的性价比排名筛选出前 `max_count`（最多 20）个候选商品。
    优先选择拥有明确物理/计数定量单位（如 g/ml/抽/张/片/包）的高性价比商品。
    """
    for grp in unit_groups.values():
        grp.sort(key=lambda x: x[0], reverse=True)

    ordered_units = sorted(
        unit_groups.keys(),
        key=lambda u: (
            1 if _unit_priority_score(u) >= 2 else 0,
            len(unit_groups[u]),
            _unit_priority_score(u),
            unit_groups[u][0][0],
        ),
        reverse=True,
    )
    top_unit = ordered_units[0]

    if len(cat_candidates) <= max_count:
        ranked = sorted(
            cat_candidates,
            key=lambda c: (
                0 if (c["sku_fmt"].get("pricing_basis") or {}).get("is_inferred") else 1,
                c["unit_cost_performance"].get(top_unit, 0.0),
                max(c["unit_cost_performance"].values() or [0.0]),
            ),
            reverse=True,
        )
        return top_unit, ranked

    selected_indices: List[int] = []
    seen_indices: Set[int] = set()

    # 先从主单位取前排高性价比商品
    for _, idx in unit_groups[top_unit]:
        if len(selected_indices) >= max(12, max_count // 2):
            break
        if idx not in seen_indices:
            seen_indices.add(idx)
            selected_indices.append(idx)

    # 再从其他精确物理/计数单位轮询补充高性价比商品，避免因单位表記差异漏掉极高性价比标品
    other_units = [u for u in ordered_units[1:] if _unit_priority_score(u) >= 2]
    round_pos = 0
    while len(selected_indices) < max_count and other_units:
        added_any = False
        for u in other_units:
            grp = unit_groups[u]
            if round_pos < len(grp):
                _, idx = grp[round_pos]
                if idx not in seen_indices:
                    seen_indices.add(idx)
                    selected_indices.append(idx)
                    if len(selected_indices) >= max_count:
                        break
                added_any = True
        if not added_any:
            break
        round_pos += 1

    # 若仍不足 max_count，则从主单位及剩余候选中补齐
    for _, idx in unit_groups[top_unit]:
        if len(selected_indices) >= max_count:
            break
        if idx not in seen_indices:
            seen_indices.add(idx)
            selected_indices.append(idx)

    for idx in range(len(cat_candidates)):
        if len(selected_indices) >= max_count:
            break
        if idx not in seen_indices:
            seen_indices.add(idx)
            selected_indices.append(idx)

    selected_list = [cat_candidates[i] for i in selected_indices[:max_count]]
    selected_list.sort(
        key=lambda c: (
            c["unit_cost_performance"].get(top_unit, 0.0),
            max(c["unit_cost_performance"].values() or [0.0]),
        ),
        reverse=True,
    )
    return top_unit, selected_list


def _validate_step1_selection_response(
    parsed: Any,
    valid_candidate_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    强校验【第 1 个 AI】返回的标品选拔与搜索词 JSON：
    1. 必须是字典；
    2. `has_comparable_standard_product` 必须为布尔值；
    3. 若 `has_comparable_standard_product == True`：
       - `selected_product_id` 必须非空，且严格在输入的候选商品 `valid_candidate_ids` 中；
       - `search_keyword` 必须非空。
    """
    if not isinstance(parsed, dict):
        return False, f"返回结构不是JSON对象(dict)，实际为 {type(parsed).__name__}", {}

    has_std = parsed.get("has_comparable_standard_product")
    if not isinstance(has_std, bool):
        return False, "缺少布尔字段 has_comparable_standard_product", {}

    if has_std:
        sel_id = str(parsed.get("selected_product_id") or "").strip()
        if not sel_id:
            return False, "has_comparable_standard_product=true 时 selected_product_id 不能为空", {}
        if sel_id not in valid_candidate_ids:
            return (
                False,
                f"selected_product_id='{sel_id}' 不在候选商品 product_id 列表 {sorted(valid_candidate_ids)} 中",
                {},
            )
        main_kw = str(parsed.get("search_keyword") or "").strip()
        if not main_kw:
            return False, "has_comparable_standard_product=true 时 search_keyword 不能为空", {}

    return True, "ok", parsed


def _select_standard_product_and_keywords_by_llm(
    category_name: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[bool, Optional[Dict[str, Any]], str, str, Dict[str, Any]]:
    """
    【AI 第 1 步】：将该分类下的前 20 个候选商品交给 `medium` 模型组 + `闲鱼搜索关键字生成.txt`：
    - 判断是否存在可在闲鱼进行同款一对一比价的标品；
    - 选出性价比最高的 1 个标品 `selected_product_id`；
    - 生成闲鱼同款主搜索词 `search_keyword` 与备用搜索词 `backup_search_keyword`。
    """
    prompt_template = read_file_to_str(KEYWORD_PROMPT_PATH)
    cand_map: Dict[str, Dict[str, Any]] = {}
    candidates_for_llm: List[Dict[str, Any]] = []

    for c in candidates:
        doc = c["doc"]
        sku_fmt = c["sku_fmt"]
        pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        if not pid:
            continue
        cand_map[pid] = c
        candidates_for_llm.append({
            "product_id": pid,
            "platform": normalize_platform(doc.get("platform") or "pdd"),
            "name": str(doc.get("name") or "").strip(),
            "brand": str(doc.get("brand") or "").strip(),
            "activity_price": c["db_activity_price"],
            "decision_keywords": sku_fmt.get("decision_keywords") or [],
            "pricing_basis": sku_fmt.get("pricing_basis") or {},
            "unit_quantities": c["unit_quantities"],
            "unit_cost_performance": c["unit_cost_performance"],
        })

    valid_ids = set(cand_map.keys())
    payload = {
        "category_name": category_name,
        "candidate_count": len(candidates_for_llm),
        "candidates": candidates_for_llm,
    }
    base_prompt = (
        f"{prompt_template}\n"
        f"<category_candidates_input>\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</category_candidates_input>"
    )

    last_raw = ""
    last_model = None
    last_trace = None
    last_err = ""
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        prompt_to_send = base_prompt
        if last_err:
            prompt_to_send += (
                f"\n\n⚠️【上次返回校验失败修正提示（第 {attempt}/{LLM_MAX_ATTEMPTS} 次尝试）】：\n"
                f"你上一次返回的数据未通过校验，原因：{last_err}。\n"
                f"若存在可比标品，请务必确保 `selected_product_id` 严格取自给定候选列表：{sorted(valid_ids)}，并严格输出合法 JSON！"
            )

        t0 = time.monotonic()
        res = generate_content(prompt=prompt_to_send, preset_model_group="medium")
        elapsed = time.monotonic() - t0
        last_model = (res.get("metrics") or {}).get("model_used")
        last_trace = res.get("trace_id")
        last_raw = str(res.get("content") or "").strip()

        if "成功" not in str(res.get("status") or "") or not last_raw:
            last_err = f"模型网关调用失败({res.get('status')})"
            logger.warning(
                f"⚠️ [步骤1/AI标品选拔第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 分类=【{category_name}】 | 模型={last_model} | 原因={last_err}"
            )
            continue

        try:
            parsed = string_to_object(last_raw)
        except Exception as exc:
            last_err = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [步骤1/AI标品选拔第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 分类=【{category_name}】 | 原因={last_err}"
            )
            continue

        is_ok, err_reason, clean_dict = _validate_step1_selection_response(parsed, valid_ids)
        if not is_ok:
            last_err = err_reason
            logger.warning(
                f"⚠️ [步骤1/AI标品选拔第{attempt}/{LLM_MAX_ATTEMPTS}次校验未通过] 分类=【{category_name}】 | 原因={err_reason}"
            )
            continue

        meta = {
            "model_used": last_model,
            "attempt_used": attempt,
            "trace_id": last_trace,
            "elapsed_seconds": round(elapsed, 2),
            "parsed_json": clean_dict,
            "raw_text": last_raw,
        }
        has_std = bool(clean_dict.get("has_comparable_standard_product"))
        if not has_std:
            return False, None, "", "", meta

        sel_id = str(clean_dict.get("selected_product_id") or "").strip()
        chosen_cand = cand_map[sel_id]
        main_kw = str(clean_dict.get("search_keyword") or "").strip()
        backup_kw = str(clean_dict.get("backup_search_keyword") or "").strip() or main_kw
        return True, chosen_cand, main_kw, backup_kw, meta

    return (
        False,
        None,
        "",
        "",
        {
            "model_used": last_model,
            "attempt_used": LLM_MAX_ATTEMPTS,
            "trace_id": last_trace,
            "parsed_json": {"has_comparable_standard_product": False, "selection_reason": f"AI选拔失败:{last_err}"},
            "raw_text": last_raw,
        },
    )


def _verify_live_activity_price(platform: str, product_id: str, goods_sign: str, fallback_price: float) -> Optional[float]:
    """
    调用官方 Adapter 验证候选商品的实时 `activity_price`：
    - 若商品明确已下架 (`PRODUCT_OFF_PROMOTION`) 则返回 `None`；
    - 若接口返回最新有效 `activity_price` 则返回实时价格；
    - 若接口暂时网络波动或未配置则返回入库 `activity_price`。
    """
    try:
        adapter = get_adapter(platform)
        res = adapter.get_goods_detail(
            goods_id_or_sign=goods_sign or product_id,
            goods_id=product_id,
            goods_sign=goods_sign or None,
        )
        if isinstance(res, dict):
            err_code = str(res.get("error_code") or "").upper()
            if err_code == "PRODUCT_OFF_PROMOTION":
                logger.warning(f"🚫 [官方验价淘汰] [{platform.upper()}:{product_id}] 商品已下架或取消推广，跳过该候选")
                return None
            if res.get("status") == "success" and isinstance(res.get("data"), dict):
                d = res["data"]
                live_p = _num(d.get("activity_price") or d.get("price"), 0.0)
                if live_p > 0:
                    return round(live_p, 2)
    except Exception as exc:
        logger.warning(f"⚠️ [官方验价异常] [{platform.upper()}:{product_id}] 沿用72h入库activity_price=¥{fallback_price} | 异常={exc}")
    return round(fallback_price, 2) if fallback_price > 0 else None


def select_verified_category_champions(
    db: Any,
    hours_window: int = 72,
    max_categories: int = 20,
    cooldown_hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    步骤 1（源头过滤 + 类内前 20 交由第 1 个 AI 选出可比标品与搜索词 + 官方实时价格验证）：
    1. 查询 72h 内、`channel_type` 存在且有值、`format_status == "success"` 的商品；
    2. 过滤掉已在 `xianyu_deal_evaluations` 中存在记录的商品（确保只选尚未比价过的商品）；
    3. 按 `core_entities` 最高 `score` 的 `name` 归类（保留所有分类，分的再小也不合并）；
    4. 每个分类筛选出前 20 个商品交由【第 1 个 AI】选出 1 个高性价比标品并生成同款搜索词，同时完成官方实时验价。
    """
    repo = ProductRepository()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_dt = (now_utc - datetime.timedelta(hours=hours_window)).replace(tzinfo=None)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    all_eval_docs = list(
        eval_col.find(
            {},
            {"platform": 1, "product_id": 1},
        )
    )
    evaluated_keys = {
        (normalize_platform(d.get("platform") or "pdd"), str(d.get("product_id") or "").strip())
        for d in all_eval_docs
        if d.get("product_id")
    }

    query = {
        "$and": [
            repo._boom_content_filter(),
            {
                "format_status": "success",
                "$or": [
                    {"updated_at": {"$gte": cutoff_dt}},
                    {"updated_at": {"$gte": cutoff_str}},
                ],
            },
        ]
    }
    projection = {**repo._COST_PERFORMANCE_PROJECTION, "channel_type": 1}
    raw_docs = list(repo.col.find(query, projection).sort([("updated_at", -1)]))

    by_cat: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any], float]]] = defaultdict(list)
    skipped_evaluated_cnt = 0
    for doc in raw_docs:
        pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        if not pid:
            continue
        plat = normalize_platform(doc.get("platform") or "pdd")
        if (plat, pid) in evaluated_keys:
            skipped_evaluated_cnt += 1
            continue
        act_price = _num(doc.get("activity_price") or doc.get("price"), 0.0)
        if act_price <= 0:
            continue
        cat_and_sku = _extract_primary_sku_and_category(doc, act_price)
        if not cat_and_sku:
            continue
        cat_name, primary_sku = cat_and_sku
        by_cat[cat_name].append((doc, primary_sku, act_price))

    sorted_cats = sorted(by_cat.items(), key=lambda kv: len(kv[1]), reverse=True)
    top_preview = ", ".join(f"{k}({len(v)}款)" for k, v in sorted_cats[:20])
    target_cat_desc = "全部" if max_categories <= 0 else str(max_categories)
    logger.info(
        f"📊 [步骤1/源头扫描] 72h内有channel_type且已结构化商品={len(raw_docs)} | "
        f"已过滤历史已比价商品={skipped_evaluated_cnt} | 剩余未比价分类数={len(sorted_cats)} | "
        f"本轮目标选出标品分类数={target_cat_desc} | 商品数量最多前20分类=[{top_preview}]"
    )

    champions: List[Dict[str, Any]] = []
    for cat_name, raw_items in sorted_cats:
        if max_categories > 0 and len(champions) >= max_categories:
            break

        # 统计该分类内各物理维度的最高频标准单位
        dim_counts: Dict[str, Dict[str, int]] = {}
        parsed_entries = []
        for doc, sku_fmt, act_price in raw_items:
            raw_base_unit, extracted_units = _extract_raw_units(sku_fmt)
            parsed_entries.append((doc, sku_fmt, act_price, raw_base_unit, extracted_units))
            for u in extracted_units.keys():
                dim = UNIT_TO_CATEGORY.get(u)
                if dim:
                    dim_counts.setdefault(dim, {})
                    dim_counts[dim][u] = dim_counts[dim].get(u, 0) + 1

        dim_target_unit = {dim: max(cnts.items(), key=lambda x: x[1])[0] for dim, cnts in dim_counts.items()}

        cat_candidates: List[Dict[str, Any]] = []
        unit_groups: Dict[str, List[Tuple[float, int]]] = defaultdict(list)

        for idx, (doc, sku_fmt, act_price, raw_base_unit, extracted_units) in enumerate(parsed_entries):
            unit_quantities: Dict[str, float] = {}
            for u, val in extracted_units.items():
                unit_quantities[u] = round(float(val), 4)
                dim = UNIT_TO_CATEGORY.get(u)
                if dim and dim in dim_target_unit:
                    tgt_u = dim_target_unit[dim]
                    if tgt_u != u:
                        conv_val = float(val) * (UNIT_TO_MULTIPLIER[u] / UNIT_TO_MULTIPLIER[tgt_u])
                        unit_quantities[tgt_u] = round(conv_val, 4)

            unit_cp = _compute_unit_cp(unit_quantities, act_price)
            dim = UNIT_TO_CATEGORY.get(raw_base_unit)
            primary_unit = dim_target_unit[dim] if (dim and dim in dim_target_unit) else raw_base_unit
            if primary_unit not in unit_cp:
                primary_unit = next(iter(unit_cp.keys()), "件")

            cat_candidates.append({
                "doc": doc,
                "sku_fmt": sku_fmt,
                "db_activity_price": round(act_price, 2),
                "primary_unit": primary_unit,
                "unit_quantities": unit_quantities,
                "unit_cost_performance": unit_cp,
            })
            for u, cp_val in unit_cp.items():
                unit_groups[u].append((cp_val, idx))

        if not cat_candidates:
            continue

        top_unit, top20_pool = _select_top_candidates_for_category(
            cat_candidates=cat_candidates,
            unit_groups=unit_groups,
            max_count=MAX_CATEGORY_CANDIDATES_FOR_AI1,
        )

        # 调用【第 1 个 AI】从该类前 20 商品中选出 1 个高性价比标品 + 生成搜索词，并验证官方实时价格
        active_pool = list(top20_pool)
        winner: Optional[Dict[str, Any]] = None
        main_kw = ""
        backup_kw = ""
        llm_kw_meta: Dict[str, Any] = {}

        for verify_round in range(1, 3):
            if not active_pool:
                break
            has_std, chosen_cand, m_kw, b_kw, kw_meta = _select_standard_product_and_keywords_by_llm(
                category_name=cat_name,
                candidates=active_pool,
            )
            llm_kw_meta = kw_meta
            if not has_std or chosen_cand is None:
                reason = (kw_meta.get("parsed_json") or {}).get("selection_reason") or "无可比标品"
                logger.info(
                    f"⏭️ [步骤1/AI判定无标品跳过] 分类=【{cat_name}】(共{len(raw_items)}款, 送审{len(active_pool)}款) | "
                    f"模型={kw_meta.get('model_used')} | 原因={reason}"
                )
                break

            w_doc = chosen_cand["doc"]
            plat = normalize_platform(w_doc.get("platform") or "pdd")
            pid = str(w_doc.get("product_id") or w_doc.get("goods_id") or "").strip()
            gsign = str(w_doc.get("goods_sign") or "").strip()

            live_price = _verify_live_activity_price(plat, pid, gsign, chosen_cand["db_activity_price"])
            if live_price is None or live_price <= 0:
                active_pool = [
                    c for c in active_pool
                    if str(c["doc"].get("product_id") or c["doc"].get("goods_id") or "").strip() != pid
                ]
                continue

            # 若实时价格比入库价格暴涨超过 30%，说明原优惠已失效，剔除后重选
            if live_price > chosen_cand["db_activity_price"] * 1.30 and len(active_pool) > 1:
                logger.warning(
                    f"⚠️ [官方验价优惠失效] [{plat.upper()}:{pid}] 实时价¥{live_price} 较入库价¥{chosen_cand['db_activity_price']} 暴涨，顺延重选"
                )
                active_pool = [
                    c for c in active_pool
                    if str(c["doc"].get("product_id") or c["doc"].get("goods_id") or "").strip() != pid
                ]
                continue

            chosen_cand["verified_activity_price"] = live_price
            chosen_cand["unit_cost_performance"] = _compute_unit_cp(chosen_cand["unit_quantities"], live_price)
            winner = chosen_cand
            main_kw = m_kw
            backup_kw = b_kw
            break

        if not winner:
            continue

        w_doc = winner["doc"]
        w_sku = winner["sku_fmt"]
        if not main_kw:
            main_kw, backup_kw = _build_fallback_search_keywords(w_doc, w_sku, cat_name)

        # 选择该商品最精确的单位作为 primary_unit
        best_primary_unit = top_unit
        if best_primary_unit not in winner["unit_cost_performance"] or _unit_priority_score(best_primary_unit) == 0:
            sorted_cand_units = _ordered_unit_keys(winner["unit_cost_performance"])
            if sorted_cand_units:
                best_primary_unit = sorted_cand_units[0]

        kws_list = [
            f"{k.get('attribute_name')}:{k.get('attribute_value')}"
            for k in (w_sku.get("decision_keywords") or [])
            if isinstance(k, dict) and k.get("attribute_value")
        ]
        sel_reason = str((llm_kw_meta.get("parsed_json") or {}).get("selection_reason") or "").strip()
        champion_record = {
            "platform": normalize_platform(w_doc.get("platform") or "pdd"),
            "product_id": str(w_doc.get("product_id") or w_doc.get("goods_id") or "").strip(),
            "goods_sign": str(w_doc.get("goods_sign") or "").strip(),
            "channel_type": w_doc.get("channel_type"),
            "category_name": cat_name,
            "category_item_count": len(raw_items),
            "category_top20_count": len(top20_pool),
            "name": str(w_doc.get("name") or "").strip(),
            "brand": str(w_doc.get("brand") or "").strip(),
            "db_activity_price": winner["db_activity_price"],
            "activity_price": winner["verified_activity_price"],
            "primary_unit": best_primary_unit,
            "unit_quantities": winner["unit_quantities"],
            "unit_cost_performance": winner["unit_cost_performance"],
            "decision_keywords_summary": " | ".join(kws_list),
            "pricing_basis": w_sku.get("pricing_basis") or {},
            "search_keyword": main_kw,
            "backup_search_keyword": backup_kw,
            "selection_reason": sel_reason,
            "llm_keyword_output": llm_kw_meta,
            "product_url": str(w_doc.get("product_url") or ""),
            "image_url": str(w_doc.get("image_url") or ""),
            "updated_at": str(w_doc.get("updated_at") or ""),
        }
        champions.append(champion_record)
        logger.info(
            f"🎯 [步骤1/AI标品入选 #{len(champions)}] 分类=【{cat_name}】(共{len(raw_items)}款, AI审{len(top20_pool)}款) | "
            f"[{champion_record['platform'].upper()}:{champion_record['product_id']}] | "
            f"验价后activity_price=¥{champion_record['activity_price']} (入库=¥{champion_record['db_activity_price']}) | "
            f"各unit数量={champion_record['unit_quantities']} | 各unit性价比={champion_record['unit_cost_performance']} | "
            f"闲鱼搜索词=【{main_kw}】(备用=【{backup_kw}】, 模型={llm_kw_meta.get('model_used')}) | "
            f"选拔理由={sel_reason} | 标题={champion_record['name'][:36]}"
        )

    return champions


# ==============================================================================
# 步骤 2：闲鱼搜索 20 条概览数据 -> 第 2 个 AI 筛选出同款可比商品 ID 列表
# ==============================================================================
def _validate_step2_coarse_filter_response(
    parsed: Any,
    valid_xianyu_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    强校验【第 2 个 AI】返回的同款商品初筛 JSON：
    1. 必须是字典；
    2. `has_comparable_candidates` 必须为布尔值；
    3. `comparable_item_ids` 必须为列表，且其中每个 ID 必须真实存在于 `valid_xianyu_ids` 中。
    """
    if not isinstance(parsed, dict):
        return False, f"返回结构不是JSON对象(dict)，实际为 {type(parsed).__name__}", {}

    has_cand = parsed.get("has_comparable_candidates")
    if not isinstance(has_cand, bool):
        return False, "缺少布尔字段 has_comparable_candidates", {}

    raw_ids = parsed.get("comparable_item_ids")
    if not isinstance(raw_ids, list):
        return False, "comparable_item_ids 必须是列表(list)", {}

    clean_ids: List[str] = []
    invalid_ids: List[str] = []
    for x in raw_ids:
        sid = str(x or "").strip()
        if not sid:
            continue
        if sid in valid_xianyu_ids:
            if sid not in clean_ids:
                clean_ids.append(sid)
        else:
            invalid_ids.append(sid)

    if invalid_ids:
        return (
            False,
            f"comparable_item_ids 中包含不在本次搜索结果列表中的非法ID: {invalid_ids}",
            {},
        )

    if has_cand and not clean_ids:
        return False, "has_comparable_candidates=true 时 comparable_item_ids 不能为空列表", {}

    if not has_cand:
        clean_ids = []

    out = dict(parsed)
    out["has_comparable_candidates"] = bool(clean_ids)
    out["comparable_item_ids"] = clean_ids
    return True, "ok", out


def _filter_comparable_xianyu_items_by_llm(
    my_candidate_for_llm: Dict[str, Any],
    coarse_items_for_llm: List[Dict[str, Any]],
    valid_xianyu_ids: Set[str],
) -> Tuple[List[str], Dict[str, Any], str]:
    """
    【AI 第 2 步】：将我方标品与闲鱼初筛的 20 条概览数据交给 `medium` 模型组 + `闲鱼同款商品初筛.txt`，
    返回属于「同品牌、同款/同系列」的所有可比闲鱼商品 ID 列表 `comparable_item_ids`。
    """
    prompt_template = read_file_to_str(COARSE_FILTER_PROMPT_PATH)
    payload = {
        "my_candidate_product": my_candidate_for_llm,
        "xianyu_search_items": coarse_items_for_llm,
    }
    base_prompt = (
        f"{prompt_template}\n"
        f"<coarse_filter_input>\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</coarse_filter_input>"
    )

    last_err = ""
    llm_meta: Dict[str, Any] = {}
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        prompt_to_send = base_prompt
        if last_err:
            prompt_to_send += (
                f"\n\n⚠️【上次返回校验失败修正提示（第 {attempt}/{LLM_MAX_ATTEMPTS} 次尝试）】：\n"
                f"你上一次返回的数据未通过校验，原因：{last_err}。\n"
                f"请务必确保 `comparable_item_ids` 中的每个 ID 均严格取自给定的闲鱼列表 ID 集合：{sorted(valid_xianyu_ids)}，并严格输出合法 JSON！"
            )

        t0 = time.monotonic()
        res = generate_content(prompt=prompt_to_send, preset_model_group="medium")
        elapsed = time.monotonic() - t0
        content = str(res.get("content") or "").strip()
        model_used = (res.get("metrics") or {}).get("model_used") or "unknown"
        trace_id = res.get("trace_id")
        llm_meta = {
            "model_used": model_used,
            "attempt_used": attempt,
            "trace_id": trace_id,
            "elapsed_seconds": round(elapsed, 2),
            "raw_text": content,
        }

        if "成功" not in str(res.get("status") or "") or not content:
            last_err = f"模型网关调用失败({res.get('status')})"
            logger.warning(
                f"⚠️ [步骤2/AI同款初筛第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_err}"
            )
            continue

        try:
            parsed = string_to_object(content)
        except Exception as exc:
            last_err = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [步骤2/AI同款初筛第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_err}"
            )
            continue

        is_ok, err_reason, clean_dict = _validate_step2_coarse_filter_response(parsed, valid_xianyu_ids)
        if not is_ok:
            last_err = err_reason
            logger.warning(
                f"⚠️ [步骤2/AI同款初筛第{attempt}/{LLM_MAX_ATTEMPTS}次校验未通过] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={err_reason}"
            )
            continue

        llm_meta["parsed_json"] = dict(clean_dict)
        comp_ids = clean_dict.get("comparable_item_ids") or []
        filter_reason = str(clean_dict.get("filter_reason") or "").strip()
        logger.info(
            f"🔎 [步骤2/AI同款初筛完成] 模型={model_used} | 尝试={attempt}/{LLM_MAX_ATTEMPTS} | 耗时={elapsed:.1f}s | "
            f"初筛同款可比数={len(comp_ids)}/{len(coarse_items_for_llm)} | IDs={comp_ids} | 理由={filter_reason}"
        )
        return comp_ids, llm_meta, filter_reason

    return [], llm_meta, f"AI同款初筛校验失败: {last_err}"


# ==============================================================================
# 步骤 3 & 4：拉取可比商品详情/多规格/运费 + 第 3 个 AI 终审比价 + 比值计算
# ==============================================================================
def _validate_llm_comparison_response(
    parsed: Any,
    valid_xianyu_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    强校验【第 3 个 AI】返回的终审比价 JSON 数据：
    1. 必须是字典；
    2. `has_comparable_item` 必须为布尔值；
    3. 若 `has_comparable_item == True`：
       - `best_xianyu_item_id` 必须非空，且真实存在于本次已拉取详情的闲鱼商品编号集合 `valid_xianyu_ids` 中；
       - `best_xianyu_price` 必须 > 0；
       - `xianyu_unit_cost_performance` 必须是字典；
       - `llm_winner` 必须属于 `{"my_product", "xianyu_product", "tie"}`；
    4. 若 `has_comparable_item == False`，自动规范化 `llm_winner = "no_comparable"`。
    """
    if not isinstance(parsed, dict):
        return False, f"返回结构不是JSON对象(dict)，实际为 {type(parsed).__name__}", {}

    has_comp = parsed.get("has_comparable_item")
    if not isinstance(has_comp, bool):
        return False, "缺少布尔字段 has_comparable_item", {}

    llm_winner = str(parsed.get("llm_winner") or "").strip()
    valid_winners = {"my_product", "xianyu_product", "tie", "no_comparable"}
    if llm_winner not in valid_winners:
        return False, f"llm_winner='{llm_winner}' 非法，必须为 {valid_winners} 之一", {}

    out = dict(parsed)
    if has_comp:
        if llm_winner == "no_comparable":
            return False, "has_comparable_item=true 时 llm_winner 不能为 no_comparable，必须明确给出胜负或持平", {}
        best_id = str(parsed.get("best_xianyu_item_id") or "").strip()
        if not best_id:
            return False, "has_comparable_item=true 时 best_xianyu_item_id 不能为空", {}
        if best_id not in valid_xianyu_ids:
            return (
                False,
                f"返回的闲鱼商品编号 best_xianyu_item_id='{best_id}' 不在已拉取详情的闲鱼商品ID列表中({sorted(valid_xianyu_ids)})",
                {},
            )
        best_p = _num(parsed.get("best_xianyu_price"), 0.0)
        if best_p <= 0:
            return False, f"best_xianyu_price={parsed.get('best_xianyu_price')} 必须大于 0", {}
        xy_cp = parsed.get("xianyu_unit_cost_performance")
        if not isinstance(xy_cp, dict):
            return False, "xianyu_unit_cost_performance 必须是字典(dict)", {}
    else:
        out["llm_winner"] = "no_comparable"
        out["best_xianyu_item_id"] = ""
        out["comparable_item_ids"] = []

    return True, "ok", out


def compare_candidate_with_xianyu(
    candidate: Dict[str, Any],
    guest_user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    max_search_items: int = 20,
    max_detail_checks: int = 0,
    debug_browser: bool = False,
) -> Dict[str, Any]:
    """
    完整执行第 2 步与第 3 步：
    1. 搜索闲鱼最多 20 条商品概览；
    2. 调用【第 2 个 AI】筛选出所有与我方标品属于同款可比商品的 `comparable_item_ids`；
    3. 对筛出的每个 `comparable_item_ids` 依次调用 `get_xianyu_item_detail`，获取完整描述 `desc`、多规格 `sku_list` 与真实运费 `transport_fee`；
    4. 调用【第 3 个 AI】基于完整详情、多规格价格和邮费进行终审比价；
    5. 计算单位性价比比值并组装完整入库文档。
    """
    kw = candidate["search_keyword"]
    backup_kw = candidate["backup_search_keyword"]
    my_price = candidate["activity_price"]

    logger.info(
        f"🔍 [步骤2/闲鱼搜索] 分类=【{candidate['category_name']}】 | 标品=【{candidate['name'][:32]}】 | "
        f"我方activity_price=¥{my_price} | 我方unit性价比={candidate['unit_cost_performance']} | 搜索词=【{kw}】"
    )

    search_res = search_xianyu_items(
        keyword=kw,
        max_items=max_search_items,
        user_data_dir=guest_user_data_dir,
        debug=debug_browser,
    )
    raw_items = search_res.get("items") or []

    # 若主搜索词返回结果过少（< 3条），自动用备用搜索词重搜
    if len(raw_items) < 3 and backup_kw and backup_kw != kw:
        logger.info(f"🔄 [步骤2/搜索词扩召回] 主搜索词【{kw}】仅返回 {len(raw_items)} 条，切换【{backup_kw}】重搜...")
        time.sleep(1.5)
        retry_res = search_xianyu_items(
            keyword=backup_kw,
            max_items=max_search_items,
            user_data_dir=guest_user_data_dir,
            debug=debug_browser,
        )
        if retry_res.get("ok") and len(retry_res.get("items") or []) > len(raw_items):
            search_res = retry_res
            raw_items = retry_res["items"]
            kw = backup_kw

    if not search_res.get("ok") or not raw_items:
        logger.warning(f"⚠️ [步骤2/闲鱼搜索为空] 搜索词=【{kw}】 | msg={search_res.get('message')}")
        return _build_final_record(
            candidate=candidate,
            used_keyword=kw,
            search_res=search_res,
            coarse_items_for_llm=[],
            step2_comparable_ids=[],
            step2_llm_meta={},
            xianyu_items_for_llm=[],
            llm_data={},
            llm_call_meta={},
            fallback_error="no_xianyu_search_results",
        )

    # 构造提供给【第 2 个 AI】的 20 条闲鱼概览列表
    coarse_items_for_llm: List[Dict[str, Any]] = []
    raw_card_map: Dict[str, Dict[str, Any]] = {}
    valid_coarse_ids: Set[str] = set()
    for x in raw_items:
        iid = str(x.get("item_id") or "").strip()
        if not iid:
            continue
        valid_coarse_ids.add(iid)
        raw_card_map[iid] = x
        coarse_items_for_llm.append({
            "item_id": iid,
            "title": x.get("title"),
            "display_price": x.get("price"),
            "want_cnt": x.get("want_cnt"),
            "is_free_shipping": x.get("is_free_shipping"),
            "seller_nick": x.get("seller_nick"),
            "area": x.get("area"),
        })

    my_candidate_for_llm = {
        "product_id": candidate["product_id"],
        "platform": candidate["platform"],
        "category_name": candidate["category_name"],
        "name": candidate["name"],
        "brand": candidate["brand"],
        "decision_keywords": candidate["decision_keywords_summary"],
        "pricing_basis": candidate.get("pricing_basis") or {},
        "activity_price": candidate["activity_price"],
        "unit_quantities": candidate["unit_quantities"],
        "unit_cost_performance": candidate["unit_cost_performance"],
    }

    # 调用【第 2 个 AI】筛选同款可比商品 ID 列表
    step2_comp_ids, step2_llm_meta, step2_reason = _filter_comparable_xianyu_items_by_llm(
        my_candidate_for_llm=my_candidate_for_llm,
        coarse_items_for_llm=coarse_items_for_llm,
        valid_xianyu_ids=valid_coarse_ids,
    )

    if not step2_comp_ids:
        logger.info(
            f"⚪ [步骤2/初筛无同款可比商品] 分类=【{candidate['category_name']}】 | 搜索词=【{kw}】 | 原因={step2_reason}"
        )
        return _build_final_record(
            candidate=candidate,
            used_keyword=kw,
            search_res=search_res,
            coarse_items_for_llm=coarse_items_for_llm,
            step2_comparable_ids=[],
            step2_llm_meta=step2_llm_meta,
            xianyu_items_for_llm=[],
            llm_data={
                "has_comparable_item": False,
                "comparable_item_ids": [],
                "best_xianyu_item_id": "",
                "llm_winner": "no_comparable",
                "llm_reason": f"[AI初筛无同款可比商品] {step2_reason}",
            },
            llm_call_meta=step2_llm_meta,
            fallback_error="",
        )

    # 步骤 3：对第 2 个 AI 筛出的所有同款可比商品依次调用 get_xianyu_item_detail 获取详情、多规格价格与运费
    ids_to_fetch = step2_comp_ids[:max_detail_checks] if max_detail_checks > 0 else step2_comp_ids
    logger.info(
        f"📦 [步骤3/拉取闲鱼同款详情] 准备依次调用 get_xianyu_item_detail 拉取 {len(ids_to_fetch)} 个同款候选商品的完整详情、SKU价格与运费..."
    )

    detailed_items_for_llm: List[Dict[str, Any]] = []
    valid_detailed_ids: Set[str] = set()
    for idx, iid in enumerate(ids_to_fetch, 1):
        if idx > 1:
            time.sleep(1.2)
        det = get_xianyu_item_detail(
            item_id_or_url=iid,
            user_data_dir=guest_user_data_dir,
            debug=debug_browser,
        )
        raw_card = raw_card_map.get(iid) or {}
        if not det.get("ok"):
            logger.warning(
                f"⚠️ [步骤3/详情拉取失败剔除] ({idx}/{len(ids_to_fetch)}) item_id={iid} | msg={det.get('message')}"
            )
            continue

        sku_list_clean = [
            {
                "spec_text": s.get("spec_text"),
                "price": s.get("price"),
                "quantity": s.get("quantity"),
            }
            for s in (det.get("sku_list") or [])[:30]
        ]
        detail_info = {
            "desc": det.get("desc") or "",
            "price": det.get("price"),
            "original_price": det.get("original_price"),
            "transport_fee": det.get("transport_fee"),
            "is_free_shipping": det.get("is_free_shipping"),
            "has_multi_sku": det.get("has_multi_sku"),
            "min_price": det.get("min_price"),
            "max_price": det.get("max_price"),
            "sku_list": sku_list_clean,
            "cpv_labels": det.get("cpv_labels") or [],
        }
        valid_detailed_ids.add(iid)
        detailed_items_for_llm.append({
            "item_id": iid,
            "title": det.get("title") or raw_card.get("title"),
            "display_price": raw_card.get("price") or det.get("price"),
            "want_cnt": det.get("want_cnt") if det.get("want_cnt") is not None else raw_card.get("want_cnt"),
            "is_free_shipping": det.get("is_free_shipping"),
            "detail_verified_info": detail_info,
        })
        logger.info(
            f"   ├─ ✅ ({idx}/{len(ids_to_fetch)}) 详情拉取成功 [XY:{iid}] | "
            f"详情价=¥{det.get('price')} (区间:¥{det.get('min_price')}~¥{det.get('max_price')}) | "
            f"运费=¥{det.get('transport_fee')}({'包邮' if det.get('is_free_shipping') else '不包邮'}) | "
            f"多规格={det.get('has_multi_sku')}({len(sku_list_clean)}个SKU) | 标题={(det.get('title') or raw_card.get('title') or '')[:30]}"
        )

    if not detailed_items_for_llm:
        logger.warning(f"⚠️ [步骤3/同款详情全部拉取失败] 分类=【{candidate['category_name']}】 | 初筛IDs={ids_to_fetch}")
        return _build_final_record(
            candidate=candidate,
            used_keyword=kw,
            search_res=search_res,
            coarse_items_for_llm=coarse_items_for_llm,
            step2_comparable_ids=step2_comp_ids,
            step2_llm_meta=step2_llm_meta,
            xianyu_items_for_llm=[],
            llm_data={
                "has_comparable_item": False,
                "comparable_item_ids": [],
                "best_xianyu_item_id": "",
                "llm_winner": "no_comparable",
                "llm_reason": "初筛同款商品详情拉取均失败，缺少邮费与具体规格信息，不予比价",
            },
            llm_call_meta={},
            fallback_error="all_detail_fetches_failed",
        )

    # 调用【第 3 个 AI】基于完整详情、SKU 价格与运费进行终审精准比价
    prompt_template = read_file_to_str(COMPARISON_PROMPT_PATH)
    comparison_payload = {
        "my_candidate_product": my_candidate_for_llm,
        "xianyu_detailed_items": detailed_items_for_llm,
    }
    base_prompt = (
        f"{prompt_template}\n"
        f"<comparison_input>\n{json.dumps(comparison_payload, ensure_ascii=False, indent=2)}\n</comparison_input>"
    )

    validated_llm_data: Dict[str, Any] = {}
    llm_call_meta: Dict[str, Any] = {}
    last_error_msg = ""
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        prompt_to_send = base_prompt
        if last_error_msg:
            prompt_to_send += (
                f"\n\n⚠️【上次返回校验失败修正提示（第 {attempt}/{LLM_MAX_ATTEMPTS} 次尝试）】：\n"
                f"你上一次返回的数据未通过校验，原因：{last_error_msg}。\n"
                f"请务必确保 `best_xianyu_item_id` 严格取自给定的已拉取详情闲鱼列表 ID 集合：{sorted(valid_detailed_ids)}，并严格输出合法 JSON！"
            )

        t0 = time.monotonic()
        res = generate_content(
            prompt=prompt_to_send,
            preset_model_group="medium",
        )
        elapsed = time.monotonic() - t0
        content = str(res.get("content") or "").strip()
        model_used = (res.get("metrics") or {}).get("model_used") or "unknown"
        trace_id = res.get("trace_id")
        llm_call_meta = {
            "model_used": model_used,
            "attempt_used": attempt,
            "trace_id": trace_id,
            "elapsed_seconds": round(elapsed, 2),
            "raw_text": content,
        }

        if "成功" not in str(res.get("status") or "") or not content:
            last_error_msg = f"模型网关调用失败({res.get('status')})"
            logger.warning(
                f"⚠️ [步骤3/AI终审比价第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_error_msg}"
            )
            continue

        try:
            parsed = string_to_object(content)
        except Exception as exc:
            last_error_msg = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [步骤3/AI终审比价第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_error_msg}"
            )
            continue

        is_ok, err_reason, clean_dict = _validate_llm_comparison_response(parsed, valid_detailed_ids)
        if not is_ok:
            last_error_msg = err_reason
            logger.warning(
                f"⚠️ [步骤3/AI终审比价第{attempt}/{LLM_MAX_ATTEMPTS}次数据校验未通过] 模型={model_used} | 耗时={elapsed:.1f}s | 校验失败原因={err_reason}"
            )
            continue

        validated_llm_data = clean_dict
        llm_call_meta["parsed_json"] = dict(clean_dict)
        logger.info(
            f"🤖 [步骤3/AI终审比价校验通过] 模型={model_used} | 尝试={attempt}/{LLM_MAX_ATTEMPTS} | 耗时={elapsed:.1f}s | "
            f"最佳闲鱼ID={clean_dict.get('best_xianyu_item_id')} | 含邮总价=¥{clean_dict.get('best_xianyu_price')} | llm_winner={clean_dict.get('llm_winner')}"
        )
        break

    return _build_final_record(
        candidate=candidate,
        used_keyword=kw,
        search_res=search_res,
        coarse_items_for_llm=coarse_items_for_llm,
        step2_comparable_ids=step2_comp_ids,
        step2_llm_meta=step2_llm_meta,
        xianyu_items_for_llm=detailed_items_for_llm,
        llm_data=validated_llm_data,
        llm_call_meta=llm_call_meta,
        fallback_error=last_error_msg if not validated_llm_data else "",
    )


def _build_final_record(
    candidate: Dict[str, Any],
    used_keyword: str,
    search_res: Dict[str, Any],
    coarse_items_for_llm: List[Dict[str, Any]],
    step2_comparable_ids: List[str],
    step2_llm_meta: Dict[str, Any],
    xianyu_items_for_llm: List[Dict[str, Any]],
    llm_data: Dict[str, Any],
    llm_call_meta: Dict[str, Any],
    fallback_error: str = "",
) -> Dict[str, Any]:
    """
    步骤 4：
    计算我方候选标品与最佳闲鱼同款商品的 `unit` 性价比比值 (`my_unit_cp / xianyu_unit_cp`)；
    优先对齐高精度的物理/计数单位（如 g/ml/抽/张/片/包），若无相同 `unit` 的性价比值，则采用大模型返回的判断字段 `llm_winner`；
    同时将三步 AI 的完整原始返回结果与闲鱼商品详情证据保存到数据库中。
    """
    my_cp_map: Dict[str, float] = candidate["unit_cost_performance"]
    has_comp = bool(llm_data.get("has_comparable_item"))
    best_xy_id = str(llm_data.get("best_xianyu_item_id") or "").strip()
    best_xy_price = round(_num(llm_data.get("best_xianyu_price"), 0.0), 2)

    raw_xy_qty = llm_data.get("xianyu_unit_quantities") if isinstance(llm_data.get("xianyu_unit_quantities"), dict) else {}
    raw_xy_cp = llm_data.get("xianyu_unit_cost_performance") if isinstance(llm_data.get("xianyu_unit_cost_performance"), dict) else {}

    xy_qty_clean: Dict[str, float] = {str(u).strip(): round(_num(v), 4) for u, v in raw_xy_qty.items() if _num(v) > 0}
    xy_cp_clean: Dict[str, float] = {}
    for u, v in raw_xy_cp.items():
        u_str = str(u).strip()
        val = _num(v, 0.0)
        if val > 0:
            xy_cp_clean[u_str] = round(val, 6)
    if best_xy_price > 0:
        for u, q in xy_qty_clean.items():
            xy_cp_clean[u] = round(q / best_xy_price, 6)

    # 若闲鱼返回的单位与我方单位属于同一物理度量衡（如 kg vs g，L vs ml），自动补充转换后的单位以便对齐比值
    for my_u in my_cp_map.keys():
        if my_u in xy_cp_clean:
            continue
        my_dim = UNIT_TO_CATEGORY.get(my_u)
        if not my_dim:
            continue
        for xy_u, xy_cp_val in list(xy_cp_clean.items()):
            if UNIT_TO_CATEGORY.get(xy_u) == my_dim and xy_cp_val > 0:
                conv_cp = xy_cp_val * (UNIT_TO_MULTIPLIER[xy_u] / UNIT_TO_MULTIPLIER[my_u])
                xy_cp_clean[my_u] = round(conv_cp, 6)
                if xy_u in xy_qty_clean and my_u not in xy_qty_clean:
                    conv_q = xy_qty_clean[xy_u] * (UNIT_TO_MULTIPLIER[xy_u] / UNIT_TO_MULTIPLIER[my_u])
                    xy_qty_clean[my_u] = round(conv_q, 4)
                break

    # 计算共同 unit 的性价比比值：ratio = 我方unit性价比 / 闲鱼unit性价比（> 1.0 代表我方性价比更高）
    matched_unit_ratios: Dict[str, float] = {}
    if has_comp:
        for u, my_val in my_cp_map.items():
            xy_val = xy_cp_clean.get(u, 0.0)
            if my_val > 0 and xy_val > 0:
                matched_unit_ratios[u] = round(my_val / xy_val, 4)

    llm_winner = str(llm_data.get("llm_winner") or "no_comparable").strip()
    llm_reason = str(llm_data.get("llm_reason") or fallback_error or "未找到可比商品").strip()

    primary_unit = candidate.get("primary_unit") or ""
    if matched_unit_ratios:
        # 优先选择精确物理/计数单位（如 g, ml, 张, 抽, 片, 包），避免误用模糊容器单位（如 瓶, 盒, 件）覆盖精确单位
        ranked_matched_units = sorted(
            matched_unit_ratios.keys(),
            key=lambda u: (_unit_priority_score(u), 1 if u == primary_unit else 0),
            reverse=True,
        )
        chosen_unit = ranked_matched_units[0]
        cp_ratio = matched_unit_ratios[chosen_unit]
        comparison_basis = "unit_ratio"
        if cp_ratio > 1.0:
            final_winner = "my_product"
        elif cp_ratio < 1.0:
            final_winner = "xianyu_product"
        else:
            final_winner = "tie"
    else:
        chosen_unit = None
        cp_ratio = None
        comparison_basis = "llm_judgment_fallback" if has_comp else "no_comparable"
        final_winner = llm_winner if has_comp else "no_comparable"

    is_my_better = final_winner == "my_product"

    raw_xy_card = next((x for x in xianyu_items_for_llm if x.get("item_id") == best_xy_id), {})
    best_det_info = raw_xy_card.get("detail_verified_info") if isinstance(raw_xy_card.get("detail_verified_info"), dict) else {}

    best_xianyu_product = {
        "item_id": best_xy_id,
        "item_url": f"https://www.goofish.com/item?id={best_xy_id}" if best_xy_id else "",
        "title": str(llm_data.get("best_xianyu_title") or raw_xy_card.get("title") or ""),
        "display_price": raw_xy_card.get("display_price"),
        "comparable_price": best_xy_price,
        "transport_fee": best_det_info.get("transport_fee"),
        "is_free_shipping": best_det_info.get("is_free_shipping", raw_xy_card.get("is_free_shipping")),
        "has_multi_sku": best_det_info.get("has_multi_sku"),
        "sku_list": best_det_info.get("sku_list") or [],
        "desc": best_det_info.get("desc") or "",
        "spec_description": str(llm_data.get("best_xianyu_spec") or ""),
        "want_cnt": raw_xy_card.get("want_cnt"),
        "unit_quantities": xy_qty_clean,
        "unit_cost_performance": xy_cp_clean,
    }

    comparison_result = {
        "has_comparable_item": has_comp,
        "comparison_basis": comparison_basis,
        "matched_unit": chosen_unit,
        "cost_performance_ratio": cp_ratio,
        "unit_cp_ratios": matched_unit_ratios,
        "my_unit_cost_performance": my_cp_map,
        "xianyu_unit_cost_performance": xy_cp_clean,
        "llm_winner": llm_winner,
        "final_winner": final_winner,
        "is_my_product_better": is_my_better,
        "llm_reason": llm_reason,
        "model_used": llm_call_meta.get("model_used"),
        "attempt_used": llm_call_meta.get("attempt_used"),
    }

    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "platform": candidate["platform"],
        "product_id": candidate["product_id"],
        "channel_type": candidate["channel_type"],
        "category_name": candidate["category_name"],
        "name": candidate["name"],
        "activity_price": candidate["activity_price"],
        "evaluated_at": now_s,
        "updated_at": now_s,
        "comparison_basis": comparison_basis,
        "cost_performance_ratio": cp_ratio,
        "llm_winner": llm_winner,
        "final_winner": final_winner,
        "is_my_product_better": is_my_better,
        "candidate_product": candidate,
        "best_xianyu_product": best_xianyu_product,
        "comparison_result": comparison_result,
        "llm_raw_output": {
            "step1_selection_llm": candidate.get("llm_keyword_output") or {},
            "step2_coarse_filter_llm": step2_llm_meta,
            "step3_comparison_llm": {
                "model_used": llm_call_meta.get("model_used"),
                "attempt_used": llm_call_meta.get("attempt_used"),
                "trace_id": llm_call_meta.get("trace_id"),
                "elapsed_seconds": llm_call_meta.get("elapsed_seconds"),
                "parsed_json": llm_call_meta.get("parsed_json") or llm_data,
                "raw_text": llm_call_meta.get("raw_text") or "",
            },
            # 保留兼容别名
            "keyword_llm": candidate.get("llm_keyword_output") or {},
            "comparison_llm": {
                "model_used": llm_call_meta.get("model_used"),
                "attempt_used": llm_call_meta.get("attempt_used"),
                "trace_id": llm_call_meta.get("trace_id"),
                "elapsed_seconds": llm_call_meta.get("elapsed_seconds"),
                "parsed_json": llm_call_meta.get("parsed_json") or llm_data,
                "raw_text": llm_call_meta.get("raw_text") or "",
            },
        },
        "xianyu_audit_trail": {
            "search_keyword": used_keyword,
            "search_ok": bool(search_res.get("ok")),
            "raw_search_count": len(coarse_items_for_llm),
            "step2_comparable_item_ids": step2_comparable_ids,
            "step3_detail_fetched_count": len(xianyu_items_for_llm),
            "comparable_item_ids": llm_data.get("comparable_item_ids") or [],
            "xianyu_coarse_items": coarse_items_for_llm,
            "xianyu_items_fed_to_llm": xianyu_items_for_llm,
        },
    }


def _build_verdict_summary(record: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    返回 (简短结论标签, 比值显示文本, 量化胜负一句话解释)
    """
    cr = record.get("comparison_result") or {}
    cand = record.get("candidate_product") or {}
    bxy = record.get("best_xianyu_product") or {}
    final_winner = str(record.get("final_winner") or "no_comparable")
    basis = str(cr.get("comparison_basis") or "no_comparable")
    matched_u = cr.get("matched_unit")
    cp_ratio = cr.get("cost_performance_ratio")
    my_cp_map = cr.get("my_unit_cost_performance") or {}
    xy_cp_map = cr.get("xianyu_unit_cost_performance") or {}

    if not cr.get("has_comparable_item") or final_winner == "no_comparable":
        return (
            "⚪ 无可比款",
            "N/A",
            "闲鱼未搜索到同品牌同规格且邮费/规格清晰的可比商品",
        )

    if basis == "unit_ratio" and cp_ratio is not None and matched_u:
        my_u_cp = _fmt_num_clean(my_cp_map.get(matched_u, 0.0))
        xy_u_cp = _fmt_num_clean(xy_cp_map.get(matched_u, 0.0))
        my_u_qty = _fmt_num_clean((cand.get("unit_quantities") or {}).get(matched_u, 0.0))
        xy_u_qty = _fmt_num_clean((bxy.get("unit_quantities") or {}).get(matched_u, 0.0))
        ratio_str = f"{cp_ratio:.2f}x ({matched_u})"
        if final_winner == "my_product":
            pct = (cp_ratio - 1.0) * 100.0
            quant_str = (
                f"赢在含邮单价更低：按单位[{matched_u}]对齐，我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u} "
                f"(每元可得 {my_u_cp}{matched_u}) vs 闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u} "
                f"(每元可得 {xy_u_cp}{matched_u}) -> 我方性价比是闲鱼的 {cp_ratio:.2f} 倍 (高出 +{pct:.1f}%)"
            )
            return "🏆 我方胜出", ratio_str, quant_str
        elif final_winner == "xianyu_product":
            pct = (1.0 - cp_ratio) * 100.0
            quant_str = (
                f"输在含邮单价偏高：按单位[{matched_u}]对齐，我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u} "
                f"(每元可得 {my_u_cp}{matched_u}) vs 闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u} "
                f"(每元可得 {xy_u_cp}{matched_u}) -> 我方性价比仅为闲鱼的 {cp_ratio:.2f} 倍 (低了 -{pct:.1f}%)"
            )
            return "🏳️ 闲鱼更优", ratio_str, quant_str
        else:
            quant_str = (
                f"双方持平：按单位[{matched_u}]对齐，双方每元均可买 {my_u_cp}{matched_u} (比值 {cp_ratio:.2f}x)"
            )
            return "🤝 双方持平", ratio_str, quant_str

    ratio_str = "AI跨单位判定"
    if final_winner == "my_product":
        quant_str = (
            f"赢在整体规格价格比（跨单位AI兜底判定）：我方 ¥{record['activity_price']:.2f} "
            f"[{_fmt_qty_human(cand.get('unit_quantities') or {})}] vs "
            f"闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} [{_fmt_qty_human(bxy.get('unit_quantities') or {})}]"
        )
        return "🏆 我方胜出", ratio_str, quant_str
    elif final_winner == "xianyu_product":
        quant_str = (
            f"输在整体规格价格比（跨单位AI兜底判定）：我方 ¥{record['activity_price']:.2f} "
            f"[{_fmt_qty_human(cand.get('unit_quantities') or {})}] vs "
            f"闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} [{_fmt_qty_human(bxy.get('unit_quantities') or {})}]"
        )
        return "🏳️ 闲鱼更优", ratio_str, quant_str
    return "🤝 双方持平", ratio_str, "跨单位AI判定双方性价比相当"


def _format_champions_summary_table(champions: List[Dict[str, Any]]) -> str:
    """将步骤 1 选出的所有分类高性价比标品冠军格式化为对齐表格。"""
    sep = "-" * 136
    lines = [
        "",
        "=" * 136,
        f"📋 【阶段 1 汇总：本轮由第 1 个 AI 选出的 {len(champions)} 个分类高性价比标品清单】（已通过官方接口实时核验价格）",
        sep,
        (
            f"{_pad_col('序号', 4)} | "
            f"{_pad_col('分类名称(库内数)', 18)} | "
            f"{_pad_col('平台与商品ID', 19)} | "
            f"{_pad_col('核验价格', 10)} | "
            f"{_pad_col('规格数量', 16)} | "
            f"{_pad_col('我方单位性价比(每1元)', 24)} | "
            f"{_pad_col('闲鱼搜索词', 18)} | "
            f"我方标品标题与属性"
        ),
        sep,
    ]
    for i, c in enumerate(champions, 1):
        cat_label = f"{c['category_name']}({c.get('category_item_count', 0)}款)"
        plat_id = f"{str(c['platform']).upper()}:{c['product_id']}"
        price_str = f"¥{c['activity_price']:.2f}"
        p_unit = c.get("primary_unit")
        qty_str = _fmt_qty_human(c.get("unit_quantities") or {}, p_unit)
        cp_str = _fmt_cp_human(c.get("unit_cost_performance") or {}, p_unit)
        kw_str = str(c.get("search_keyword") or "")
        desc_str = f"{c['name']} [{c.get('decision_keywords_summary') or '无扩展属性'}]"
        lines.append(
            f"{_pad_col(f'#{i}', 4)} | "
            f"{_pad_col(cat_label, 18)} | "
            f"{_pad_col(plat_id, 19)} | "
            f"{_pad_col(price_str, 10)} | "
            f"{_pad_col(qty_str, 16)} | "
            f"{_pad_col(cp_str, 24)} | "
            f"{_pad_col(kw_str, 18)} | "
            f"{desc_str}"
        )
    lines.append("=" * 136)
    return "\n".join(lines)


def _format_single_comparison_card(idx: int, total: int, record: Dict[str, Any]) -> str:
    """
    将单个商品与闲鱼最优同款商品的比价结果排版为严格上下对齐的【对战卡片】。
    """
    cand = record.get("candidate_product") or {}
    bxy = record.get("best_xianyu_product") or {}
    cr = record.get("comparison_result") or {}
    audit = record.get("xianyu_audit_trail") or {}

    badge, ratio_str, quant_explain = _build_verdict_summary(record)
    focus_u = cr.get("matched_unit") or cand.get("primary_unit")

    my_plat_id = f"{str(record['platform']).upper()}:{record['product_id']}"
    my_price = f"¥{record['activity_price']:.2f}"
    my_qty = _fmt_qty_human(cand.get("unit_quantities") or {}, focus_u)
    my_cp = _fmt_cp_human(cr.get("my_unit_cost_performance") or {}, focus_u)
    my_desc = f"【规格属性: {cand.get('decision_keywords_summary') or '默认规格'}】 标题: {record['name']}"

    if cr.get("has_comparable_item") and bxy.get("item_id"):
        xy_plat_id = f"XY :{bxy['item_id']}"
        xy_price = f"¥{_num(bxy.get('comparable_price'), 0.0):.2f}"
        xy_qty = _fmt_qty_human(bxy.get("unit_quantities") or {}, focus_u)
        xy_cp = _fmt_cp_human(cr.get("xianyu_unit_cost_performance") or {}, focus_u)
        xy_spec = str(bxy.get("spec_description") or "").strip()
        tfee = bxy.get("transport_fee")
        fee_tag = f"运费¥{tfee}" if tfee is not None else ("包邮" if bxy.get("is_free_shipping") else "运费见详情")
        xy_desc = f"【提取规格({fee_tag}): {xy_spec or '见标题'}】 标题: {bxy.get('title') or ''}"
    else:
        xy_plat_id = "XY :无可比商品"
        xy_price = "-"
        xy_qty = "-"
        xy_cp = "-"
        xy_desc = "未在闲鱼搜索结果中找到同品牌同规格且邮费清晰的可比商品"

    border_eq = "=" * 140
    border_dash = "-" * 140
    header_line = (
        f"🏁 【单品比价结论 #{idx}/{total}】 "
        f"分类: 【{record['category_name']}】(库内{cand.get('category_item_count', '-')}款)  |  "
        f"结论: {badge}  |  性价比比值(我方/闲鱼): {ratio_str}  |  "
        f"闲鱼搜索: \"{audit.get('search_keyword', '')}\"(初搜{audit.get('raw_search_count', 0)}->"
        f"AI初筛同款{len(audit.get('step2_comparable_item_ids') or [])}->拉取详情{audit.get('step3_detail_fetched_count', 0)}款)"
    )
    col_header = (
        f"{_pad_col('对比角色', 10)} | "
        f"{_pad_col('平台与商品编号', 19)} | "
        f"{_pad_col('含邮总价', 10)} | "
        f"{_pad_col('提取规格数量', 18)} | "
        f"{_pad_col('单位性价比 (每1元可得)', 26)} | "
        f"核心规格与商品标题描述"
    )
    my_row = (
        f"{_pad_col('[我方标品]', 10)} | "
        f"{_pad_col(my_plat_id, 19)} | "
        f"{_pad_col(my_price, 10)} | "
        f"{_pad_col(my_qty, 18)} | "
        f"{_pad_col(my_cp, 26)} | "
        f"{my_desc}"
    )
    xy_row = (
        f"{_pad_col('[闲鱼最优]', 10)} | "
        f"{_pad_col(xy_plat_id, 19)} | "
        f"{_pad_col(xy_price, 10)} | "
        f"{_pad_col(xy_qty, 18)} | "
        f"{_pad_col(xy_cp, 26)} | "
        f"{xy_desc}"
    )

    lines = [
        "",
        border_eq,
        header_line,
        border_dash,
        col_header,
        border_dash,
        my_row,
        xy_row,
        border_dash,
        f"⚖️  胜负关键点 : {quant_explain}",
        f"📝 AI详细理由 : {cr.get('llm_reason') or '-'}",
        border_eq,
    ]
    return "\n".join(lines)


def _format_batch_summary_report(batch_id: str, elapsed: float, records: List[Dict[str, Any]]) -> str:
    """
    在整轮任务结束时输出【运维全局总结汇总报告】。
    """
    win_cnt = sum(1 for r in records if r.get("final_winner") == "my_product")
    lose_cnt = sum(1 for r in records if r.get("final_winner") == "xianyu_product")
    tie_cnt = sum(1 for r in records if r.get("final_winner") == "tie")
    none_cnt = sum(1 for r in records if r.get("final_winner") not in ("my_product", "xianyu_product", "tie"))

    eq_line = "=" * 144
    dash_line = "-" * 144
    lines = [
        "",
        eq_line,
        (
            f"📊 【闲鱼比价探测 · 本轮运维全局总结报告】 "
            f"批次: {batch_id}  |  总耗时: {elapsed:.1f}s  |  "
            f"参评标品分类: {len(records)} 个  |  "
            f"🏆 我方胜出: {win_cnt}  |  🏳️ 闲鱼更优: {lose_cnt}  |  🤝 持平: {tie_cnt}  |  ⚪ 无可比: {none_cnt}"
        ),
        eq_line,
        "【一、全部分类胜负一表通览】",
        dash_line,
        (
            f"{_pad_col('序号', 4)} | "
            f"{_pad_col('分类(库内数)', 16)} | "
            f"{_pad_col('胜负结论', 12)} | "
            f"{_pad_col('比值(我/闲)', 14)} | "
            f"{_pad_col('我方价格', 9)} | "
            f"{_pad_col('闲鱼含邮价', 10)} | "
            f"{_pad_col('我方性价比(每1元)', 22)} | "
            f"{_pad_col('闲鱼性价比(每1元)', 22)} | "
            f"胜负核心摘要"
        ),
        dash_line,
    ]

    for idx, r in enumerate(records, 1):
        cand = r.get("candidate_product") or {}
        bxy = r.get("best_xianyu_product") or {}
        cr = r.get("comparison_result") or {}
        badge, ratio_str, quant_explain = _build_verdict_summary(r)
        focus_u = cr.get("matched_unit") or cand.get("primary_unit")

        cat_col = f"{r['category_name']}({cand.get('category_item_count', '-')}款)"
        my_p_col = f"¥{r['activity_price']:.2f}"
        xy_p_col = f"¥{_num(bxy.get('comparable_price'), 0.0):.2f}" if cr.get("has_comparable_item") else "-"
        my_cp_col = _fmt_cp_human(cr.get("my_unit_cost_performance") or {}, focus_u)
        xy_cp_col = _fmt_cp_human(cr.get("xianyu_unit_cost_performance") or {}, focus_u) if cr.get("has_comparable_item") else "-"

        lines.append(
            f"{_pad_col(f'#{idx}', 4)} | "
            f"{_pad_col(cat_col, 16)} | "
            f"{_pad_col(badge, 12)} | "
            f"{_pad_col(ratio_str, 14)} | "
            f"{_pad_col(my_p_col, 9)} | "
            f"{_pad_col(xy_p_col, 10)} | "
            f"{_pad_col(my_cp_col, 22)} | "
            f"{_pad_col(xy_cp_col, 22)} | "
            f"{quant_explain}"
        )

    lines.extend([
        dash_line,
        "",
        "【二、逐商品 [我方标品 vs 闲鱼最优] 对齐明细汇总（直接上下对比描述、含邮总价、规格与性价比）】",
        dash_line,
        (
            f"{_pad_col('序号/角色', 13)} | "
            f"{_pad_col('分类名称', 12)} | "
            f"{_pad_col('平台与编号', 19)} | "
            f"{_pad_col('含邮总价', 9)} | "
            f"{_pad_col('提取规格量', 16)} | "
            f"{_pad_col('单位性价比(每1元)', 24)} | "
            f"核心规格与商品完整描述"
        ),
        dash_line,
    ])

    for idx, r in enumerate(records, 1):
        cand = r.get("candidate_product") or {}
        bxy = r.get("best_xianyu_product") or {}
        cr = r.get("comparison_result") or {}
        badge, ratio_str, quant_explain = _build_verdict_summary(r)
        focus_u = cr.get("matched_unit") or cand.get("primary_unit")

        my_plat_id = f"{str(r['platform']).upper()}:{r['product_id']}"
        my_price = f"¥{r['activity_price']:.2f}"
        my_qty = _fmt_qty_human(cand.get("unit_quantities") or {}, focus_u)
        my_cp = _fmt_cp_human(cr.get("my_unit_cost_performance") or {}, focus_u)
        my_desc = f"【规格属性: {cand.get('decision_keywords_summary') or '默认规格'}】 标题: {r['name']}"

        if cr.get("has_comparable_item") and bxy.get("item_id"):
            xy_plat_id = f"XY :{bxy['item_id']}"
            xy_price = f"¥{_num(bxy.get('comparable_price'), 0.0):.2f}"
            xy_qty = _fmt_qty_human(bxy.get("unit_quantities") or {}, focus_u)
            xy_cp = _fmt_cp_human(cr.get("xianyu_unit_cost_performance") or {}, focus_u)
            xy_spec = str(bxy.get("spec_description") or "").strip()
            xy_desc = f"【提取规格: {xy_spec or '见标题'}】 标题: {bxy.get('title') or ''}"
        else:
            xy_plat_id = "XY :无可比商品"
            xy_price = "-"
            xy_qty = "-"
            xy_cp = "-"
            xy_desc = "未找到可比商品"

        lines.append(
            f"{_pad_col(f'[#{idx} 我方标品]', 13)} | "
            f"{_pad_col(r['category_name'], 12)} | "
            f"{_pad_col(my_plat_id, 19)} | "
            f"{_pad_col(my_price, 9)} | "
            f"{_pad_col(my_qty, 16)} | "
            f"{_pad_col(my_cp, 24)} | "
            f"{my_desc}"
        )
        lines.append(
            f"{_pad_col(f'[#{idx} 闲鱼最优]', 13)} | "
            f"{_pad_col(r['category_name'], 12)} | "
            f"{_pad_col(xy_plat_id, 19)} | "
            f"{_pad_col(xy_price, 9)} | "
            f"{_pad_col(xy_qty, 16)} | "
            f"{_pad_col(xy_cp, 24)} | "
            f"{xy_desc}"
        )
        lines.append(f"   ├─ ⚖️ 胜负解析: {badge} (比值: {ratio_str}) | {quant_explain}")
        lines.append(f"   └─ 📝 AI说明  : {cr.get('llm_reason') or '-'}")
        lines.append(dash_line)

    lines.append(eq_line)
    return "\n".join(lines)


# ==============================================================================
# 主流程编排与运行入口
# ==============================================================================
def run_discovery_cycle(
    hours_window: int = 72,
    max_categories: int = 20,
    max_search_items: int = 20,
    max_detail_checks: int = 0,
    cooldown_hours: int = 24,
    guest_user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug_browser: bool = False,
) -> List[Dict[str, Any]]:
    t0 = time.monotonic()
    batch_id = datetime.datetime.now().strftime("disc_%Y%m%d_%H%M%S")
    logger.info(
        f"\n{'=' * 92}\n"
        f"🚀 [闲鱼高性价比商品探测与比价任务/启动] 批次={batch_id} | 时间窗={hours_window}h | "
        f"探测标品分类数={max_categories if max_categories > 0 else '全部'} | 访客目录=<{guest_user_data_dir}>\n"
        f"{'=' * 92}"
    )

    db = gen_db_object()
    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    eval_col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
    eval_col.create_index([("is_my_product_better", -1), ("cost_performance_ratio", -1), ("evaluated_at", -1)], background=True)

    champions = select_verified_category_champions(
        db=db,
        hours_window=hours_window,
        max_categories=max_categories,
        cooldown_hours=cooldown_hours,
    )
    if not champions:
        logger.warning("⚠️ [本轮结束] 未选出满足条件的可比标品候选商品。")
        return []

    logger.info(_format_champions_summary_table(champions))

    records: List[Dict[str, Any]] = []
    for idx, cand in enumerate(champions, 1):
        logger.info(
            f"\n>>> ▶️ [开始比价第 {idx}/{len(champions)} 个分类标品冠军] "
            f"分类=【{cand['category_name']}】(库内{cand.get('category_item_count', '-')}款) | "
            f"我方标品=【{cand['name'][:40]}】(¥{cand['activity_price']:.2f})"
        )
        record = compare_candidate_with_xianyu(
            candidate=cand,
            guest_user_data_dir=guest_user_data_dir,
            max_search_items=max_search_items,
            max_detail_checks=max_detail_checks,
            debug_browser=debug_browser,
        )
        record["batch_id"] = batch_id
        eval_col.update_one(
            {"platform": record["platform"], "product_id": record["product_id"]},
            {"$set": record},
            upsert=True,
        )
        records.append(record)

        logger.info(_format_single_comparison_card(idx, len(champions), record))
        if idx < len(champions):
            time.sleep(2.0)

    elapsed = time.monotonic() - t0
    logger.info(_format_batch_summary_report(batch_id, elapsed, records))
    return records


def main():
    parser = argparse.ArgumentParser(description="闲鱼高性价比商品探测与比价定时任务（三步 AI 严选标品与详情含邮精准比价版）")
    parser.add_argument("--hours-window", type=int, default=72, help="内部商品库扫描时间窗口（默认72小时）")
    parser.add_argument("--max-categories", "--max-candidates", dest="max_categories", type=int, default=20, help="单轮探测有效标品分类数（默认20，传0表示不限数量遍历所有分类）")
    parser.add_argument("--max-search-items", type=int, default=20, help="每次闲鱼搜索抓取的概览商品数（默认20）")
    parser.add_argument("--max-detail-checks", type=int, default=0, help="对第2步AI筛出的同款商品最多拉取多少个详情（默认0表示全部拉取详情）")
    parser.add_argument("--cooldown-hours", type=int, default=24, help="保留参数兼容（已默认过滤所有在xianyu_deal_evaluations中有记录的商品）")
    parser.add_argument("--guest-dir", type=str, default=DEFAULT_GUEST_USER_DATA_DIR, help="免登录闲鱼浏览器目录")
    parser.add_argument("--debug-browser", action="store_true", help="是否显示浏览器窗口")
    parser.add_argument("--loop", action="store_true", help="是否启用常驻定时循环模式")
    parser.add_argument("--interval-hours", type=float, default=4.0, help="循环模式间隔小时数（默认4小时）")
    args = parser.parse_args()

    while True:
        try:
            run_discovery_cycle(
                hours_window=args.hours_window,
                max_categories=args.max_categories,
                max_search_items=args.max_search_items,
                max_detail_checks=args.max_detail_checks,
                cooldown_hours=args.cooldown_hours,
                guest_user_data_dir=args.guest_dir,
                debug_browser=args.debug_browser,
            )
        except KeyboardInterrupt:
            logger.info("👋 收到中断信号，任务退出。")
            break
        except Exception as exc:
            logger.exception(f"❌ [探测轮次异常] {exc}")
        if not args.loop:
            break
        logger.info(f"⏳ 等待 {args.interval_hours} 小时后执行下一轮...")
        time.sleep(max(60.0, args.interval_hours * 3600.0))


if __name__ == "__main__":
    main()
