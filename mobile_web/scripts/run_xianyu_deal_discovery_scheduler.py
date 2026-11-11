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
import concurrent.futures
import contextlib
import datetime
import json
import logging
import math
import os
import queue
import re
import sys
import time
import unicodedata
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

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
    get_xianyu_item_details_batch,
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

# ==============================================================================
# 运行控制变量（直接在此处修改变量值即可控制脚本行为）
# ==============================================================================
ENABLE_LOOP = True                                 # 是否持续循环运行（True=循环运行，False=仅运行单轮后退出）
LOOP_INTERVAL_HOURS = 1.0                          # 每轮结束后的等待间隔小时数（1.0 表示每隔 1 小时运行一轮）
SCAN_HOURS_WINDOW = 72                             # 内部商品库扫描时间窗口（小时）
MAX_CATEGORIES_PER_ROUND = 20                      # 单轮探测的有效标品分类数（设为 0 表示不限数量）
MAX_XIANYU_SEARCH_ITEMS = 20                       # 每次闲鱼搜索抓取的概览商品数
MAX_DETAIL_CHECKS_PER_CATEGORY = 20                # 每个分类最多拉取多少个初筛同款闲鱼商品详情
STAGE1_WORKERS = 5                                 # 阶段 1（AI 选标品 + 官方验价）并发线程数
COMPARE_WORKERS = 3                                # 阶段 2/3（闲鱼比价流水线）并发线程数
DEBUG_BROWSER = False                              # 是否显示浏览器窗口（False=后台无头模式，True=显示窗口）
GUEST_USER_DATA_DIR = DEFAULT_GUEST_USER_DATA_DIR  # 免登录闲鱼浏览器缓存目录

LLM_MAX_ATTEMPTS = 3
LLM_CALL_TIMEOUT_SEC = 90
LLM_MAX_RETRIES_PER_MODEL = 1
MAX_CATEGORY_CANDIDATES_FOR_AI1 = 20
DEFAULT_STAGE1_WORKERS = STAGE1_WORKERS
DEFAULT_COMPARE_WORKERS = COMPARE_WORKERS
DEFAULT_MAX_DETAIL_CHECKS = MAX_DETAIL_CHECKS_PER_CATEGORY

# 记录当前进程内因“无标品”被跳过的分类次数，配合数据库历史比价次数实现全分类均匀轮转
_SESSION_SKIPPED_CAT_COUNTS: Dict[str, int] = defaultdict(int)

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


class _ForwardWarningToFileHandler(logging.Handler):
    """将底层浏览器模块（publish_to_xianyu）的 WARNING/ERROR/EXCEPTION 日志同步写入主任务日志文件，共享单句柄避免 Windows 轮转锁冲突。"""

    def __init__(self, target_fh: logging.Handler):
        super().__init__(level=logging.WARNING)
        self._target_fh = target_fh

    def emit(self, record: logging.LogRecord) -> None:
        self._target_fh.handle(record)


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

    xy_lg = logging.getLogger("publish_to_xianyu")
    if not any(isinstance(h, _ForwardWarningToFileHandler) for h in xy_lg.handlers):
        xy_lg.addHandler(_ForwardWarningToFileHandler(fh))
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


def _resolve_candidate_brand(doc: Dict[str, Any], sku_fmt: Dict[str, Any]) -> str:
    """优先从 `doc.brand` 或 `sku_fmt.decision_keywords` 提取有效品牌名（过滤无品牌/店铺名）。"""
    invalid_brands = {"无", "其他", "无品牌", "无品牌/无注册商标", "品牌随机", "未知", "通用", "other", "none"}
    raw_b = str(doc.get("brand") or "").strip()
    if raw_b and raw_b.lower() not in invalid_brands and not any(w in raw_b for w in ("店", "专营", "旗舰", "超市", "生活馆")):
        return raw_b

    kws = sku_fmt.get("decision_keywords") or []
    for kw in kws:
        if isinstance(kw, dict) and str(kw.get("attribute_name") or "").strip() in ("品牌", "厂家", "厂商", "生产品牌"):
            val = str(kw.get("attribute_value") or "").strip()
            if val and val.lower() not in invalid_brands:
                return val
    return ""


def _build_fallback_search_keywords(doc: Dict[str, Any], sku_fmt: Dict[str, Any], category_name: str) -> Tuple[str, str]:
    """当大模型搜索词字段缺失时的保底拼接规则。"""
    brand = _resolve_candidate_brand(doc, sku_fmt)
    if "/" in brand:
        brand = brand.split("/")[-1].strip()

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
    排序与筛选策略（纯通用量化指标，不硬编码特定品类或词汇）：
    1. 优先精确物理/计数单位（优先级 >= 3），避免仅有模糊容器单位的商品挤占精确量化商品；
    2. 优先非推测（not is_inferred）且具备品牌的商品；
    3. 同等条件下按单位性价比 `unit_cost_performance` 从高到低排序。
    """
    for grp in unit_groups.values():
        grp.sort(key=lambda x: x[0], reverse=True)

    ordered_units = sorted(
        unit_groups.keys(),
        key=lambda u: (
            2 if _unit_priority_score(u) >= 3 else (1 if _unit_priority_score(u) == 2 else 0),
            len(unit_groups[u]),
            _unit_priority_score(u),
            unit_groups[u][0][0],
        ),
        reverse=True,
    )
    top_unit = ordered_units[0]

    def _cand_rank_key(c: Dict[str, Any]) -> Tuple[Any, ...]:
        doc = c["doc"]
        sku_fmt = c["sku_fmt"]
        has_precise_u = 1 if any(_unit_priority_score(u) >= 3 for u in c["unit_quantities"].keys()) else 0
        is_not_inferred = 0 if (sku_fmt.get("pricing_basis") or {}).get("is_inferred") else 1
        has_brand = 1 if _resolve_candidate_brand(doc, sku_fmt) else 0
        top_cp = c["unit_cost_performance"].get(top_unit, 0.0)
        max_cp = max(c["unit_cost_performance"].values() or [0.0])
        return (
            has_precise_u,
            is_not_inferred,
            has_brand,
            top_cp,
            max_cp,
        )

    if len(cat_candidates) <= max_count:
        ranked = sorted(cat_candidates, key=_cand_rank_key, reverse=True)
        return top_unit, ranked

    selected_indices: List[int] = []
    seen_indices: Set[int] = set()

    # 1. 从主单位取前排高性价比商品
    for _, idx in unit_groups[top_unit]:
        if len(selected_indices) >= max(14, (max_count * 2) // 3):
            break
        if idx not in seen_indices:
            seen_indices.add(idx)
            selected_indices.append(idx)

    # 2. 从其他精确物理/计数单位轮询补充高性价比商品，避免因单位表记差异漏选
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

    # 3. 若仍不足 max_count，则按综合排序从剩余候选中补齐
    remaining_indices = [i for i in range(len(cat_candidates)) if i not in seen_indices]
    remaining_indices.sort(key=lambda i: _cand_rank_key(cat_candidates[i]), reverse=True)
    for idx in remaining_indices:
        if len(selected_indices) >= max_count:
            break
        seen_indices.add(idx)
        selected_indices.append(idx)

    selected_list = [cat_candidates[i] for i in selected_indices[:max_count]]
    selected_list.sort(key=_cand_rank_key, reverse=True)
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
    - 判断是否存在可在闲鱼进行同款一对一比价的商品（含定量标品及有具体系列/款号/货号的鞋服内衣）；
    - 选出性价比最高的 1 个正装商品 `selected_product_id`；
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
        resolved_brand = _resolve_candidate_brand(doc, sku_fmt)
        candidates_for_llm.append({
            "product_id": pid,
            "platform": normalize_platform(doc.get("platform") or "pdd"),
            "name": str(doc.get("name") or "").strip(),
            "brand": resolved_brand,
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
                f"若存在可比商品，请务必确保 `selected_product_id` 严格取自给定候选列表：{sorted(valid_ids)}，并严格输出合法 JSON！"
            )

        t0 = time.monotonic()
        res = generate_content(
            prompt=prompt_to_send,
            preset_model_group="medium",
            timeout=LLM_CALL_TIMEOUT_SEC,
            max_retries_per_model=LLM_MAX_RETRIES_PER_MODEL,
        )
        elapsed = time.monotonic() - t0
        last_model = (res.get("metrics") or {}).get("model_used")
        last_trace = res.get("trace_id")
        last_raw = str(res.get("content") or "").strip()

        if "成功" not in str(res.get("status") or "") or not last_raw:
            last_err = f"模型网关调用失败({res.get('status')})"
            logger.warning(
                f"⚠️ [步骤1/AI标品选拔第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 分类=【{category_name}】 | 模型={last_model} | 耗时={elapsed:.1f}s | 原因={last_err}"
            )
            continue

        try:
            parsed = string_to_object(last_raw)
        except Exception as exc:
            last_err = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [步骤1/AI标品选拔第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 分类=【{category_name}】 | 耗时={elapsed:.1f}s | 原因={last_err}"
            )
            continue

        is_ok, err_reason, clean_dict = _validate_step1_selection_response(parsed, valid_ids)
        if not is_ok:
            last_err = err_reason
            logger.warning(
                f"⚠️ [步骤1/AI标品选拔第{attempt}/{LLM_MAX_ATTEMPTS}次校验未通过] 分类=【{category_name}】 | 耗时={elapsed:.1f}s | 原因={err_reason}"
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


def _evaluate_single_category_step1(
    cat_name: str,
    raw_items: List[Tuple[Dict[str, Any], Dict[str, Any], float]],
) -> Optional[Dict[str, Any]]:
    """对单个分类执行 Step 1（计算单位性价比 -> 筛选前20候选 -> 第1个AI选拔标品与搜索词 -> 官方接口验价）。"""
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
        return None

    top_unit, top20_pool = _select_top_candidates_for_category(
        cat_candidates=cat_candidates,
        unit_groups=unit_groups,
        max_count=MAX_CATEGORY_CANDIDATES_FOR_AI1,
    )

    active_pool = list(top20_pool)
    winner: Optional[Dict[str, Any]] = None
    main_kw = ""
    backup_kw = ""
    llm_kw_meta: Dict[str, Any] = {}

    for _verify_round in range(1, 3):
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
                f"模型={kw_meta.get('model_used')} | 耗时={kw_meta.get('elapsed_seconds', 0)}s | 原因={reason}"
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
        return None

    w_doc = winner["doc"]
    w_sku = winner["sku_fmt"]
    if not main_kw:
        main_kw, backup_kw = _build_fallback_search_keywords(w_doc, w_sku, cat_name)

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
    resolved_brand = _resolve_candidate_brand(w_doc, w_sku)
    return {
        "platform": normalize_platform(w_doc.get("platform") or "pdd"),
        "product_id": str(w_doc.get("product_id") or w_doc.get("goods_id") or "").strip(),
        "goods_sign": str(w_doc.get("goods_sign") or "").strip(),
        "channel_type": w_doc.get("channel_type"),
        "category_name": cat_name,
        "category_item_count": len(raw_items),
        "category_top20_count": len(top20_pool),
        "name": str(w_doc.get("name") or "").strip(),
        "brand": resolved_brand,
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


def select_verified_category_champions(
    db: Any,
    hours_window: int = 72,
    max_categories: int = 20,
    cooldown_hours: int = 24,
    stage1_workers: int = DEFAULT_STAGE1_WORKERS,
) -> List[Dict[str, Any]]:
    """
    步骤 1（源头过滤 + 多线程并发调用第 1 个 AI 选出可比标品与搜索词 + 官方实时价格验证）：
    1. 查询 72h 内、`channel_type` 存在且有值、`format_status == "success"` 的商品；
    2. 过滤掉已在 `xianyu_deal_evaluations` 中存在记录的商品（确保只选尚未比价过的商品）；
    3. 按 `core_entities` 最高 `score` 的 `name` 归类（保留所有分类）；
    4. 使用线程池并发对分类前 20 个商品交由【第 1 个 AI】选出 1 个高性价比标品并完成官方实时验价。
    """
    repo = ProductRepository()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_dt = (now_utc - datetime.timedelta(hours=hours_window)).replace(tzinfo=None)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    all_eval_docs = list(
        eval_col.find(
            {},
            {"platform": 1, "product_id": 1, "category_name": 1},
        )
    )
    evaluated_keys = {
        (normalize_platform(d.get("platform") or "pdd"), str(d.get("product_id") or "").strip())
        for d in all_eval_docs
        if d.get("product_id")
    }
    cat_eval_counts: Dict[str, int] = defaultdict(int)
    for d in all_eval_docs:
        cname = str(d.get("category_name") or "").strip()
        if cname:
            cat_eval_counts[cname] += 1
    for cname, skip_cnt in _SESSION_SKIPPED_CAT_COUNTS.items():
        cat_eval_counts[cname] += skip_cnt

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

    # 均匀轮转排序：优先选择已比价次数最少（如0次）的分类，同次数下再按分类商品数从多到少排序
    sorted_cats = sorted(
        by_cat.items(),
        key=lambda kv: (cat_eval_counts.get(kv[0], 0), -len(kv[1])),
    )
    top_preview = ", ".join(
        f"{k}({len(v)}款/已比{cat_eval_counts.get(k, 0)}次)" for k, v in sorted_cats[:20]
    )
    target_cat_desc = "全部" if max_categories <= 0 else str(max_categories)
    workers = max(1, int(stage1_workers or DEFAULT_STAGE1_WORKERS))
    logger.info(
        f"📊 [步骤1/源头扫描] 72h内有channel_type且已结构化商品={len(raw_docs)} | "
        f"已过滤历史已比价商品={skipped_evaluated_cnt} | 剩余未比价分类数={len(sorted_cats)} | "
        f"本轮目标选出标品分类数={target_cat_desc} (并发线程={workers}) | 本轮优先轮转前20分类=[{top_preview}]"
    )

    champions: List[Dict[str, Any]] = []
    cursor = 0
    total_cats = len(sorted_cats)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while cursor < total_cats:
            if max_categories > 0 and len(champions) >= max_categories:
                break
            needed = (max_categories - len(champions)) if max_categories > 0 else workers * 2
            batch_size = min(total_cats - cursor, max(workers, needed + 2))
            batch_slice = sorted_cats[cursor : cursor + batch_size]
            cursor += batch_size

            future_pairs = [
                (cat_name, raw_items, executor.submit(_evaluate_single_category_step1, cat_name, raw_items))
                for cat_name, raw_items in batch_slice
            ]
            for cat_name, raw_items, fut in future_pairs:
                try:
                    champ = fut.result()
                except Exception as exc:
                    logger.warning(f"⚠️ [步骤1/分类选拔异常] 分类=【{cat_name}】 | 异常={exc}")
                    continue
                if champ is None:
                    _SESSION_SKIPPED_CAT_COUNTS[cat_name] += 1
                    continue
                if max_categories > 0 and len(champions) >= max_categories:
                    continue
                champions.append(champ)
                llm_kw_meta = champ.get("llm_keyword_output") or {}
                logger.info(
                    f"🎯 [步骤1/AI标品入选 #{len(champions)}] 分类=【{cat_name}】(共{len(raw_items)}款, AI审{champ['category_top20_count']}款) | "
                    f"[{champ['platform'].upper()}:{champ['product_id']}] | "
                    f"验价后activity_price=¥{champ['activity_price']} (入库=¥{champ['db_activity_price']}) | "
                    f"各unit数量={champ['unit_quantities']} | 各unit性价比={champ['unit_cost_performance']} | "
                    f"闲鱼搜索词=【{champ['search_keyword']}】(备用=【{champ['backup_search_keyword']}】, 模型={llm_kw_meta.get('model_used')}) | "
                    f"选拔理由={champ['selection_reason']} | 标题={champ['name'][:36]}"
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
        res = generate_content(
            prompt=prompt_to_send,
            preset_model_group="medium",
            timeout=LLM_CALL_TIMEOUT_SEC,
            max_retries_per_model=LLM_MAX_RETRIES_PER_MODEL,
        )
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
@contextlib.contextmanager
def _borrow_guest_dir(
    default_dir: str,
    guest_dir_pool: Optional["queue.Queue[str]"] = None,
) -> Iterator[str]:
    """仅在执行浏览器操作（初搜或批量拉取详情）期间借用访客浏览器目录，进入 AI 思考前立即归还。"""
    if guest_dir_pool is None:
        yield default_dir
        return
    borrowed = guest_dir_pool.get()
    try:
        yield borrowed
    finally:
        guest_dir_pool.put(borrowed)


def _prepare_coarse_items_for_llm(
    raw_items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Set[str]]:
    coarse_items_for_llm: List[Dict[str, Any]] = []
    raw_card_map: Dict[str, Dict[str, Any]] = {}
    valid_coarse_ids: Set[str] = set()
    for x in raw_items:
        iid = str(x.get("item_id") or "").strip()
        if not iid or iid in valid_coarse_ids:
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
    return coarse_items_for_llm, raw_card_map, valid_coarse_ids


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
    max_detail_checks: int = DEFAULT_MAX_DETAIL_CHECKS,
    debug_browser: bool = False,
    guest_dir_pool: Optional["queue.Queue[str]"] = None,
) -> Dict[str, Any]:
    """
    完整执行第 2 步与第 3 步：
    1. 搜索闲鱼最多 20 条商品概览（仅在浏览器搜索期间占用访客目录）；
    2. 调用【第 2 个 AI】筛选出所有与我方标品属于同款可比商品的 `comparable_item_ids`；
       - 若主搜索词返回结果 < 6 条，或第 2 个 AI 判定同款可比商品为 0 条，自动切换备用词 `backup_search_keyword` 重搜并重新初筛；
    3. 仅针对第 2 个 AI 筛出的同款商品（最多前 `max_detail_checks` 个），在同一个浏览器会话内调用 `get_xianyu_item_details_batch` 批量获取完整描述 `desc`、多规格 `sku_list` 与真实运费 `transport_fee`（自动清理登录弹窗）；
    4. 调用【第 3 个 AI】基于完整详情、多规格价格和邮费进行终审比价；
    5. 计算单位性价比比值并组装完整入库文档。
    """
    kw = candidate["search_keyword"]
    backup_kw = candidate["backup_search_keyword"]
    my_price = candidate["activity_price"]
    cat_name = candidate["category_name"]

    my_candidate_for_llm = {
        "product_id": candidate["product_id"],
        "platform": candidate["platform"],
        "category_name": cat_name,
        "name": candidate["name"],
        "brand": candidate["brand"],
        "decision_keywords": candidate["decision_keywords_summary"],
        "pricing_basis": candidate.get("pricing_basis") or {},
        "activity_price": candidate["activity_price"],
        "unit_quantities": candidate["unit_quantities"],
        "unit_cost_performance": candidate["unit_cost_performance"],
    }

    logger.info(
        f"🔍 [步骤2/闲鱼搜索] 分类=【{cat_name}】 | 标品=【{candidate['name'][:32]}】 | "
        f"我方activity_price=¥{my_price} | 我方unit性价比={candidate['unit_cost_performance']} | 搜索词=【{kw}】"
    )

    with _borrow_guest_dir(guest_user_data_dir, guest_dir_pool) as active_gdir:
        search_res = search_xianyu_items(
            keyword=kw,
            max_items=max_search_items,
            user_data_dir=active_gdir,
            debug=debug_browser,
        )
    if not search_res.get("ok"):
        logger.error(
            f"❌ [步骤2/闲鱼搜索异常失败] 分类=【{cat_name}】 | 搜索词=【{kw}】 | "
            f"错误码={search_res.get('error_code')} | 目录={search_res.get('user_data_dir')} | 原因={search_res.get('message')}"
        )
    raw_items = search_res.get("items") or []
    used_backup = False

    # 情况 A：若未触发风控滑块且主搜索词返回结果过少（< 6条），直接用备用搜索词扩召回
    if not search_res.get("is_risk_blocked") and len(raw_items) < 6 and backup_kw and backup_kw != kw:
        logger.info(f"🔄 [步骤2/搜索词扩召回] 分类=【{cat_name}】 | 主词【{kw}】仅返回 {len(raw_items)} 条，切换备用词【{backup_kw}】重搜...")
        with _borrow_guest_dir(guest_user_data_dir, guest_dir_pool) as active_gdir:
            retry_res = search_xianyu_items(
                keyword=backup_kw,
                max_items=max_search_items,
                user_data_dir=active_gdir,
                debug=debug_browser,
            )
        used_backup = True
        if not retry_res.get("ok"):
            logger.error(
                f"❌ [步骤2/备用词搜索异常失败] 分类=【{cat_name}】 | 备用词=【{backup_kw}】 | "
                f"错误码={retry_res.get('error_code')} | 目录={retry_res.get('user_data_dir')} | 原因={retry_res.get('message')}"
            )
            if not search_res.get("ok"):
                search_res = retry_res
        elif not search_res.get("ok") or len(retry_res.get("items") or []) > len(raw_items):
            search_res = retry_res
            raw_items = retry_res.get("items") or []
            kw = backup_kw

    if not search_res.get("ok"):
        err_msg = f"[闲鱼搜索异常:{search_res.get('error_code') or 'UNKNOWN'}] {search_res.get('message') or '未知检索异常'}"
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
            fallback_error=err_msg,
            browser_aborted=True,
            debug_browser=debug_browser,
        )

    if not raw_items:
        logger.info(f"⚪ [步骤2/闲鱼搜索无结果] 分类=【{cat_name}】 | 搜索词=【{kw}】 | 闲鱼返回 0 条商品")
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
            fallback_error="闲鱼搜索正常返回 0 条商品",
            browser_aborted=False,
            debug_browser=debug_browser,
        )

    coarse_items_for_llm, raw_card_map, valid_coarse_ids = _prepare_coarse_items_for_llm(raw_items)

    # 调用【第 2 个 AI】筛选同款可比商品 ID 列表
    step2_comp_ids, step2_llm_meta, step2_reason = _filter_comparable_xianyu_items_by_llm(
        my_candidate_for_llm=my_candidate_for_llm,
        coarse_items_for_llm=coarse_items_for_llm,
        valid_xianyu_ids=valid_coarse_ids,
    )

    # 情况 B：若主搜索词返回的全是异品牌推荐（导致第 2 个 AI 初筛同款数为 0），且尚未试过备用搜索词，自动切换备用词重搜 + 重新初筛！
    if not step2_comp_ids and not used_backup and backup_kw and backup_kw != kw:
        logger.info(
            f"🔄 [步骤2/初筛0同款触发备用词重搜] 分类=【{cat_name}】 | 主词【{kw}】初筛同款为0，切换备用词【{backup_kw}】重搜..."
        )
        with _borrow_guest_dir(guest_user_data_dir, guest_dir_pool) as active_gdir:
            retry_res = search_xianyu_items(
                keyword=backup_kw,
                max_items=max_search_items,
                user_data_dir=active_gdir,
                debug=debug_browser,
            )
        if not retry_res.get("ok"):
            logger.error(
                f"❌ [步骤2/初筛0同款后备用词重搜异常失败] 分类=【{cat_name}】 | 备用词=【{backup_kw}】 | "
                f"错误码={retry_res.get('error_code')} | 目录={retry_res.get('user_data_dir')} | 原因={retry_res.get('message')}"
            )
        retry_items = retry_res.get("items") or []
        if retry_res.get("ok") and retry_items:
            search_res = retry_res
            raw_items = retry_items
            kw = backup_kw
            coarse_items_for_llm, raw_card_map, valid_coarse_ids = _prepare_coarse_items_for_llm(raw_items)
            step2_comp_ids, step2_llm_meta, step2_reason = _filter_comparable_xianyu_items_by_llm(
                my_candidate_for_llm=my_candidate_for_llm,
                coarse_items_for_llm=coarse_items_for_llm,
                valid_xianyu_ids=valid_coarse_ids,
            )

    if not step2_comp_ids:
        logger.info(
            f"⚪ [步骤2/初筛无同款可比商品] 分类=【{cat_name}】 | 搜索词=【{kw}】 | 原因={step2_reason}"
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
            browser_aborted=False,
            debug_browser=debug_browser,
        )

    # 步骤 3：仅对第 2 个 AI 筛出的同款可比商品（默认最多取前 20 个）在单次浏览器会话内批量拉取详情、SKU 价格与运费
    ids_to_fetch = step2_comp_ids[:max_detail_checks] if max_detail_checks > 0 else step2_comp_ids
    logger.info(
        f"📦 [步骤3/批量拉取闲鱼同款详情] 分类=【{cat_name}】 | AI初筛同款={len(step2_comp_ids)}款 -> "
        f"单次浏览器会话批量拉取前 {len(ids_to_fetch)} 款的完整详情、SKU价格与运费..."
    )

    with _borrow_guest_dir(guest_user_data_dir, guest_dir_pool) as active_gdir:
        batch_det_res = get_xianyu_item_details_batch(
            item_ids=ids_to_fetch,
            user_data_dir=active_gdir,
            debug=debug_browser,
        )
    details_by_id = batch_det_res.get("details_by_id") or {}
    if not batch_det_res.get("ok"):
        logger.error(
            f"❌ [步骤3/批量详情会话异常中断] 分类=【{cat_name}】 | 错误码={batch_det_res.get('error_code')} | "
            f"目录={batch_det_res.get('user_data_dir')} | 已完成={len(details_by_id)}/{len(ids_to_fetch)} | "
            f"原因={batch_det_res.get('message')}"
        )

    detailed_items_for_llm: List[Dict[str, Any]] = []
    valid_detailed_ids: Set[str] = set()
    detail_failed_count = 0
    first_detail_err_msg = ""
    for idx, iid in enumerate(ids_to_fetch, 1):
        det = details_by_id.get(iid) or {}
        raw_card = raw_card_map.get(iid) or {}
        if not det.get("ok"):
            detail_failed_count += 1
            item_err_code = det.get("error_code") or batch_det_res.get("error_code") or "UNKNOWN"
            item_err_msg = det.get("message") or batch_det_res.get("message") or "批量会话提前中断未拉取"
            if not first_detail_err_msg:
                first_detail_err_msg = f"[{item_err_code}] {item_err_msg}"
            logger.warning(
                f"⚠️ [步骤3/详情拉取失败剔除] ({idx}/{len(ids_to_fetch)}) 分类=【{cat_name}】 | item_id={iid} | "
                f"错误码={item_err_code} | 原因={item_err_msg}"
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
            f"   ├─ ✅ ({idx}/{len(ids_to_fetch)}) 详情核验就绪 [XY:{iid}] | "
            f"详情价=¥{det.get('price')} (区间:¥{det.get('min_price')}~¥{det.get('max_price')}) | "
            f"运费=¥{det.get('transport_fee')}({'包邮' if det.get('is_free_shipping') else '不包邮'}) | "
            f"多规格={det.get('has_multi_sku')}({len(sku_list_clean)}个SKU) | 标题={(det.get('title') or raw_card.get('title') or '')[:30]}"
        )

    if not detailed_items_for_llm:
        is_session_abort = not bool(batch_det_res.get("ok", True))
        logger.error(
            f"❌ [步骤3/同款详情全部拉取失败] 分类=【{cat_name}】 | 初筛IDs={ids_to_fetch} | "
            f"会话中断={is_session_abort} | 首条失败原因={first_detail_err_msg}"
        )
        fail_reason_text = (
            f"[批量详情检索异常中断] {first_detail_err_msg}"
            if is_session_abort
            else f"初筛同款商品({len(ids_to_fetch)}款)详情拉取均未拿到有效数据（如已下架/售出），首条诊断：{first_detail_err_msg}"
        )
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
                "llm_reason": fail_reason_text,
            },
            llm_call_meta={},
            fallback_error="all_detail_fetches_failed",
            batch_det_res=batch_det_res,
            detail_failed_count=detail_failed_count,
            browser_aborted=is_session_abort,
            debug_browser=debug_browser,
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
            timeout=LLM_CALL_TIMEOUT_SEC,
            max_retries_per_model=LLM_MAX_RETRIES_PER_MODEL,
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
                f"⚠️ [步骤3/AI终审比价第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 分类=【{cat_name}】 | 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_error_msg}"
            )
            continue

        try:
            parsed = string_to_object(content)
        except Exception as exc:
            last_error_msg = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [步骤3/AI终审比价第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 分类=【{cat_name}】 | 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_error_msg}"
            )
            continue

        is_ok, err_reason, clean_dict = _validate_llm_comparison_response(parsed, valid_detailed_ids)
        if not is_ok:
            last_error_msg = err_reason
            logger.warning(
                f"⚠️ [步骤3/AI终审比价第{attempt}/{LLM_MAX_ATTEMPTS}次数据校验未通过] 分类=【{cat_name}】 | 模型={model_used} | 耗时={elapsed:.1f}s | 校验失败原因={err_reason}"
            )
            continue

        validated_llm_data = clean_dict
        llm_call_meta["parsed_json"] = dict(clean_dict)
        logger.info(
            f"🤖 [步骤3/AI终审比价校验通过] 分类=【{cat_name}】 | 模型={model_used} | 尝试={attempt}/{LLM_MAX_ATTEMPTS} | 耗时={elapsed:.1f}s | "
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
    max_detail_checks: int = DEFAULT_MAX_DETAIL_CHECKS,
    cooldown_hours: int = 24,
    guest_user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug_browser: bool = False,
    stage1_workers: int = DEFAULT_STAGE1_WORKERS,
    compare_workers: int = DEFAULT_COMPARE_WORKERS,
) -> List[Dict[str, Any]]:
    t0 = time.monotonic()
    batch_id = datetime.datetime.now().strftime("disc_%Y%m%d_%H%M%S")
    s1_workers = max(1, int(stage1_workers or DEFAULT_STAGE1_WORKERS))
    cmp_workers = max(1, int(compare_workers or DEFAULT_COMPARE_WORKERS))
    detail_cap_desc = f"前{max_detail_checks}款" if max_detail_checks > 0 else "全部"
    logger.info(
        f"\n{'=' * 92}\n"
        f"🚀 [闲鱼高性价比商品探测与比价任务/启动] 批次={batch_id} | 时间窗={hours_window}h | "
        f"探测标品分类数={max_categories if max_categories > 0 else '全部'} | "
        f"并发配置=(阶段1选品={s1_workers}线程, 阶段2/3比价={cmp_workers}线程, 同款详情上限={detail_cap_desc}) | "
        f"访客主目录=<{guest_user_data_dir}>\n"
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
        stage1_workers=s1_workers,
    )
    if not champions:
        logger.warning("⚠️ [本轮结束] 未选出满足条件的可比标品候选商品。")
        return []

    logger.info(_format_champions_summary_table(champions))

    # 构建浏览器访客目录池：仅在执行闲鱼初搜/批量详情时借用，AI 思考期间立即归还，实现浏览器与 AI 流水线重叠
    guest_dir_pool: "queue.Queue[str]" = queue.Queue()
    num_guest_dirs = min(2, cmp_workers) if cmp_workers > 1 else 1
    for i in range(num_guest_dirs):
        gdir = guest_user_data_dir if i == 0 else f"{guest_user_data_dir}_{i + 1}"
        guest_dir_pool.put(gdir)

    total_champions = len(champions)
    indexed_records: Dict[int, Dict[str, Any]] = {}

    def _run_single_champion(idx: int, cand: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        logger.info(
            f"\n>>> ▶️ [开始比价第 {idx}/{total_champions} 个分类标品冠军] "
            f"分类=【{cand['category_name']}】(库内{cand.get('category_item_count', '-')}款) | "
            f"我方标品=【{cand['name'][:40]}】(¥{cand['activity_price']:.2f})"
        )
        rec = compare_candidate_with_xianyu(
            candidate=cand,
            guest_user_data_dir=guest_user_data_dir,
            max_search_items=max_search_items,
            max_detail_checks=max_detail_checks,
            debug_browser=debug_browser,
            guest_dir_pool=guest_dir_pool,
        )
        rec["batch_id"] = batch_id
        eval_col.update_one(
            {"platform": rec["platform"], "product_id": rec["product_id"]},
            {"$set": rec},
            upsert=True,
        )
        logger.info(_format_single_comparison_card(idx, total_champions, rec))
        return idx, rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=cmp_workers) as executor:
        futures = [
            executor.submit(_run_single_champion, idx, cand)
            for idx, cand in enumerate(champions, 1)
        ]
        for fut in concurrent.futures.as_completed(futures):
            try:
                idx, rec = fut.result()
                indexed_records[idx] = rec
            except Exception as exc:
                logger.exception(f"❌ [分类比价子任务异常] {exc}")

    records = [indexed_records[i] for i in sorted(indexed_records.keys())]
    elapsed = time.monotonic() - t0
    logger.info(_format_batch_summary_report(batch_id, elapsed, records))
    return records


def main():
    while True:
        try:
            run_discovery_cycle(
                hours_window=SCAN_HOURS_WINDOW,
                max_categories=MAX_CATEGORIES_PER_ROUND,
                max_search_items=MAX_XIANYU_SEARCH_ITEMS,
                max_detail_checks=MAX_DETAIL_CHECKS_PER_CATEGORY,
                guest_user_data_dir=GUEST_USER_DATA_DIR,
                debug_browser=DEBUG_BROWSER,
                stage1_workers=STAGE1_WORKERS,
                compare_workers=COMPARE_WORKERS,
            )
        except KeyboardInterrupt:
            logger.info("👋 收到中断信号，任务退出。")
            break
        except Exception as exc:
            logger.exception(f"❌ [探测轮次异常] {exc}")
        if not ENABLE_LOOP:
            break
        logger.info(f"⏳ 本轮执行完毕，等待 {LOOP_INTERVAL_HOURS} 小时后执行下一轮...")
        time.sleep(max(60.0, LOOP_INTERVAL_HOURS * 3600.0))


if __name__ == "__main__":
    main()

