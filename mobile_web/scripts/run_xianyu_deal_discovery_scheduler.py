# -*- coding: utf-8 -*-
"""
闲鱼高性价比商品探测与比价独立定时任务（精简稳定版 · 双通道物理隔离 + 三步 AI 严选含邮比价）
================================================================================
核心设计（越简单越稳定）：
  1. 【双独立商品源过滤与分类（仅针对同类型尚未比价过的商品）】：
     - `source_type="official"`（正式外层商品）：72h 内更新、有 `channel_type`、外层 `format_status == "success"`；
     - `source_type="deal_tip"`（线报爆料商品）：48h 保质期内、`deal_info.is_deal_tip == True`、内层清洗完成且价格一致；
     - 按 `(platform, product_id, source_type)` 独立去重入库，取 `core_entities` 最高分 `name` 作为分类。
  2. 【AI 第 1 步：类内前 20 候选选出 1 个高性价比标品 + 生成搜索词】：
     - 计算各商品 `unit_cost_performance[u] = unit_quantity[u] / activity_price`，取前 20 候选交由第 1 个 AI；
     - 正式商品通过官方接口实时核验 `activity_price`；线报商品直接沿用 48h 内 `deal_price`。
  3. 【AI 第 2 步：独立小号登录搜索概览 -> AI 初筛同款可比商品 ID】：
     - 使用独立搜索小号目录（`W:\\temp\\xianyu_xiaohao`）执行关键字搜索（未登录或风控立即停手熔断，0 次无效重试）；
     - 交由第 2 个 AI 筛选同品牌同款/同系列的可比闲鱼商品 `comparable_item_ids`。
  4. 【AI 第 3 步：移动端 H5 免登录批量拉取详情/多规格/运费 -> AI 终审含邮精准比价】：
     - 使用免登录访客目录（`W:\\temp\\xianyu_guest`）通过移动端 H5 公开页批量获取完整描述、多规格真实售价与运费（零消耗小号配额）；
     - 交由第 3 个 AI 基于含邮总价核算性价比，计算单位性价比比值并串行稳定入库。
================================================================================
"""

import concurrent.futures
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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pymongo import UpdateOne

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
    DEFAULT_SEARCH_USER_DATA_DIR,
    get_xianyu_item_details_batch,
    search_xianyu_items,
)
from repositories.product_repo import (
    ProductRepository,
    UNIT_TO_CATEGORY,
    UNIT_TO_MULTIPLIER,
)

EVAL_COLLECTION_NAME = "xianyu_deal_evaluations"
XIANYU_ITEMS_COLLECTION_NAME = "xianyu_items"
SOURCE_TYPE_OFFICIAL = "official"
SOURCE_TYPE_DEAL_TIP = "deal_tip"

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
ENABLE_LOOP = True                                      # 是否持续循环运行（True=循环运行，False=仅运行单轮后退出）
LOOP_INTERVAL_HOURS = 1.0                               # 正常每轮结束后的等待间隔小时数
RISK_COOLDOWN_HOURS = 2.0                               # 触发闲鱼限流/掉线熔断后的静默冷却小时数
ENABLE_PRE_ROUND_PROBE = True                           # 每轮 AI 选品前先执行 1 次轻量搜索探针，未登录或限流则跳过整轮
PRE_ROUND_PROBE_KEYWORD = "抽纸"                        # 开轮探针使用的公共搜索词
MAX_CONSECUTIVE_BROWSER_FAILS = 2                       # 单轮连续多少次浏览器检索失败即触发整轮熔断
ENABLE_OFFICIAL = True                                  # 是否开启【正式外层商品】比价
ENABLE_DEAL_TIP = True                                  # 是否开启【线报爆料商品】比价
SCAN_HOURS_WINDOW = 72                                  # 正式外层商品扫描保质期窗口（小时）
DEAL_SCAN_HOURS_WINDOW = 48                             # 线报爆料商品扫描保质期窗口（小时）
MAX_CATEGORIES_PER_ROUND = 20                           # 单类商品源每轮探测的有效标品分类数（0 表示不限）
MAX_XIANYU_SEARCH_ITEMS = 20                            # 每次闲鱼搜索抓取的概览商品数
MAX_DETAIL_CHECKS_PER_CATEGORY = 10                     # 每个分类最多通过 H5 免登录拉取多少个初筛同款商品详情
XIANYU_DETAIL_CACHE_DAYS = 7                            # 闲鱼商品详情数据库缓存有效天数（7天内已有详情直接复用，免重复请求H5）
STAGE1_WORKERS = 5                                      # 阶段 1（AI 选标品 + 官方验价）并发线程数
DEBUG_BROWSER = False                                   # 是否显示浏览器窗口（False=后台无头静默，True=显示窗口）
SEARCH_USER_DATA_DIR = DEFAULT_SEARCH_USER_DATA_DIR     # 【搜索小号独立目录】仅用于步骤2关键字搜索（W:\temp\xianyu_xiaohao）
GUEST_USER_DATA_DIR = DEFAULT_GUEST_USER_DATA_DIR       # 【H5免登录访客目录】仅用于步骤3批量拉商品详情（W:\temp\xianyu_guest）

LLM_MAX_ATTEMPTS = 3
LLM_CALL_TIMEOUT_SEC = 90
LLM_MAX_RETRIES_PER_MODEL = 1
MAX_CATEGORY_CANDIDATES_FOR_AI1 = 20

# 记录当前进程内按 (source_type, category_name) 因“无标品”被跳过的分类次数，均匀轮转
_SESSION_SKIPPED_CAT_COUNTS: Dict[Tuple[str, str], int] = defaultdict(int)
# 标记最近一轮是否因闲鱼风控/小号掉线熔断而中止，供主循环自动拉长静默冷却时间
_LAST_CYCLE_ABORTED_BY_RISK: bool = False
_CN_TZ = datetime.timezone(datetime.timedelta(hours=8))

PRECISE_PHYSICAL_UNITS = {"g", "kg", "斤", "两", "mg", "ml", "l", "L", "m", "cm", "mm"}
PRECISE_COUNT_UNITS = {"抽", "张", "片", "粒", "颗", "枚", "只", "双", "支", "根", "条", "节", "卷"}
PACK_UNITS = {"包", "袋", "板", "排", "组", "提"}
VAGUE_CONTAINER_UNITS = {"件", "套", "份", "单", "个", "瓶", "盒", "罐", "桶", "箱", "盆", "碗", "杯"}


def _normalize_source_type(source_type: Any) -> str:
    return SOURCE_TYPE_DEAL_TIP if str(source_type or "").strip().lower() == SOURCE_TYPE_DEAL_TIP else SOURCE_TYPE_OFFICIAL


def _source_label(source_type: Any) -> str:
    return "线报爆料" if _normalize_source_type(source_type) == SOURCE_TYPE_DEAL_TIP else "正式官价"


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
    """将底层 publish_to_xianyu 的 WARNING/ERROR 日志同步写入主任务日志文件，共享单句柄避免 Windows 轮转锁冲突。"""

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
# 统一大模型 JSON 调用与强校验重试工具 + 终端对齐排版工具
# ==============================================================================
def _call_llm_json(
    base_prompt: str,
    validator_fn: Callable[[Any], Tuple[bool, str, Dict[str, Any]]],
    step_label: str,
    id_hint_msg: str,
) -> Tuple[bool, Dict[str, Any], Dict[str, Any], str]:
    """统一封装三步 AI 的网关调用、JSON 解析与最多 LLM_MAX_ATTEMPTS 次强校验修正重试。"""
    last_err = ""
    meta: Dict[str, Any] = {}
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        prompt_to_send = base_prompt
        if last_err:
            prompt_to_send += (
                f"\n\n⚠️【上次返回校验失败修正提示（第 {attempt}/{LLM_MAX_ATTEMPTS} 次尝试）】：\n"
                f"你上一次返回的数据未通过校验，原因：{last_err}。\n"
                f"{id_hint_msg}"
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
        meta = {
            "model_used": model_used,
            "attempt_used": attempt,
            "trace_id": res.get("trace_id"),
            "elapsed_seconds": round(elapsed, 2),
            "raw_text": content,
        }

        if "成功" not in str(res.get("status") or "") or not content:
            last_err = f"模型网关调用失败({res.get('status')})"
            logger.warning(
                f"⚠️ [{step_label}/第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_err}"
            )
            continue

        try:
            parsed = string_to_object(content)
        except Exception as exc:
            last_err = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [{step_label}/第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_err}"
            )
            continue

        is_ok, err_reason, clean_dict = validator_fn(parsed)
        if not is_ok:
            last_err = err_reason
            logger.warning(
                f"⚠️ [{step_label}/第{attempt}/{LLM_MAX_ATTEMPTS}次校验未通过] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={err_reason}"
            )
            continue

        meta["parsed_json"] = dict(clean_dict)
        return True, clean_dict, meta, ""

    return False, {}, meta, last_err


def _disp_width(text: str) -> int:
    """计算字符串在终端等宽字体下的显示列宽（中文全角/Emoji占2列，英文数字占1列）。"""
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
    return ((" " * pad_len) + s) if align == "right" else (s + (" " * pad_len))


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
    return " / ".join(f"{_fmt_num_clean(qty_map[u])}{u}" for u in keys[:3])


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
# 步骤 1：源头过滤、按最高分 core_entity 分类、类内前 20 交由 AI 选拔标品并验价
# ==============================================================================
def _extract_primary_sku_and_category(doc: Dict[str, Any], activity_price: float) -> Optional[Tuple[str, Dict[str, Any]]]:
    """从商品 `format_info` 中提取主 SKU，并取 `core_entities` 中 `score` 最高的 `name` 作为分类。"""
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
    return str(ces[0]["name"]).strip(), primary_sku


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

    for kw in (sku_fmt.get("decision_keywords") or []):
        if isinstance(kw, dict) and str(kw.get("attribute_name") or "").strip() in ("品牌", "厂家", "厂商", "生产品牌"):
            val = str(kw.get("attribute_value") or "").strip()
            if val and val.lower() not in invalid_brands:
                return val
    return ""


def _select_top_candidates_for_category(
    cat_candidates: List[Dict[str, Any]],
    unit_groups: Dict[str, List[Tuple[float, int]]],
    max_count: int = MAX_CATEGORY_CANDIDATES_FOR_AI1,
) -> Tuple[str, List[Dict[str, Any]]]:
    """确定分类主对齐单位并按量化优先级（精确单位 > 非推测 > 有品牌 > 单位性价比）选出前 max_count 个候选。"""
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
        doc, sku_fmt = c["doc"], c["sku_fmt"]
        has_precise_u = 1 if any(_unit_priority_score(u) >= 3 for u in c["unit_quantities"]) else 0
        is_not_inferred = 0 if (sku_fmt.get("pricing_basis") or {}).get("is_inferred") else 1
        has_brand = 1 if _resolve_candidate_brand(doc, sku_fmt) else 0
        top_cp = c["unit_cost_performance"].get(top_unit, 0.0)
        max_cp = max(c["unit_cost_performance"].values() or [0.0])
        return (has_precise_u, is_not_inferred, has_brand, top_cp, max_cp)

    ranked = sorted(cat_candidates, key=_cand_rank_key, reverse=True)
    return top_unit, ranked[:max_count]


def _validate_step1_selection_response(
    parsed: Any,
    valid_candidate_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
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
            return False, f"selected_product_id='{sel_id}' 不在候选商品 product_id 列表 {sorted(valid_candidate_ids)} 中", {}
        if not str(parsed.get("search_keyword") or "").strip():
            return False, "has_comparable_standard_product=true 时 search_keyword 不能为空", {}

    return True, "ok", parsed


def _select_standard_product_and_keywords_by_llm(
    category_name: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[bool, Optional[Dict[str, Any]], str, str, Dict[str, Any]]:
    """【AI 第 1 步】：从类内前 20 候选中选出 1 个高性价比标品并生成闲鱼同款主/备搜索词。"""
    prompt_template = read_file_to_str(KEYWORD_PROMPT_PATH)
    cand_map: Dict[str, Dict[str, Any]] = {}
    candidates_for_llm: List[Dict[str, Any]] = []

    for c in candidates:
        doc, sku_fmt = c["doc"], c["sku_fmt"]
        pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        if not pid:
            continue
        cand_map[pid] = c
        candidates_for_llm.append({
            "product_id": pid,
            "platform": normalize_platform(doc.get("platform") or "pdd"),
            "name": str(doc.get("name") or "").strip(),
            "brand": _resolve_candidate_brand(doc, sku_fmt),
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

    ok, clean_dict, meta, last_err = _call_llm_json(
        base_prompt=base_prompt,
        validator_fn=lambda p: _validate_step1_selection_response(p, valid_ids),
        step_label=f"步骤1/AI标品选拔({category_name})",
        id_hint_msg=f"若存在可比商品，请务必确保 `selected_product_id` 严格取自给定候选列表：{sorted(valid_ids)}，并严格输出合法 JSON！",
    )
    if not ok:
        meta["parsed_json"] = {"has_comparable_standard_product": False, "selection_reason": f"AI选拔失败:{last_err}"}
        return False, None, "", "", meta

    if not clean_dict.get("has_comparable_standard_product"):
        return False, None, "", "", meta

    sel_id = str(clean_dict.get("selected_product_id") or "").strip()
    main_kw = str(clean_dict.get("search_keyword") or "").strip()
    backup_kw = str(clean_dict.get("backup_search_keyword") or "").strip() or main_kw
    return True, cand_map[sel_id], main_kw, backup_kw, meta


def _verify_live_activity_price(platform: str, product_id: str, goods_sign: str, fallback_price: float) -> Optional[float]:
    """调用官方 Adapter 验证正式商品实时 `activity_price`（下架淘汰返回 None，网络波动沿用入库价）。"""
    try:
        adapter = get_adapter(platform)
        res = adapter.get_goods_detail(
            goods_id_or_sign=goods_sign or product_id,
            goods_id=product_id,
            goods_sign=goods_sign or None,
        )
        if isinstance(res, dict):
            if str(res.get("error_code") or "").upper() == "PRODUCT_OFF_PROMOTION":
                logger.warning(f"🚫 [官方验价淘汰] [{platform.upper()}:{product_id}] 商品已下架或取消推广，跳过该候选")
                return None
            if res.get("status") == "success" and isinstance(res.get("data"), dict):
                live_p = _num(res["data"].get("activity_price") or res["data"].get("price"), 0.0)
                if live_p > 0:
                    return round(live_p, 2)
    except Exception as exc:
        logger.warning(f"⚠️ [官方验价异常] [{platform.upper()}:{product_id}] 沿用72h入库activity_price=¥{fallback_price} | 异常={exc}")
    return round(fallback_price, 2) if fallback_price > 0 else None


def _parse_deal_time_utc(val: Any, default_tz: datetime.timezone = _CN_TZ) -> Optional[datetime.datetime]:
    """将线报 publish_time 或 BSON datetime 统一解析为 UTC 时间。"""
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        return (val.replace(tzinfo=default_tz) if val.tzinfo is None else val).astimezone(datetime.timezone.utc)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
            return (dt.replace(tzinfo=default_tz) if dt.tzinfo is None else dt).astimezone(datetime.timezone.utc)
        except Exception:
            return None
    return None


def _build_deal_tip_virtual_doc(doc: Dict[str, Any], cutoff_utc: datetime.datetime) -> Optional[Dict[str, Any]]:
    """将满足 48h 保质期且结构化清洗完成的线报记录映射为与正式商品同构的独立商品对象。"""
    di = doc.get("deal_info")
    if not isinstance(di, dict) or di.get("is_deal_tip") is not True:
        return None
    if di.get("format_status") != "success" or di.get("need_reformat") is True:
        return None

    deal_price = _num(di.get("deal_price"), 0.0)
    if deal_price <= 0:
        return None
    fmt_deal_price = _num(di.get("format_deal_price"), 0.0)
    if fmt_deal_price > 0 and abs(deal_price - fmt_deal_price) >= 0.01:
        return None

    deal_fmt = di.get("format_info")
    if not isinstance(deal_fmt, dict) or not isinstance(deal_fmt.get("core_entities"), list):
        return None

    pub_utc = _parse_deal_time_utc(di.get("publish_time"), default_tz=_CN_TZ)
    if pub_utc is None:
        pub_utc = _parse_deal_time_utc(doc.get("updated_at"), default_tz=datetime.timezone.utc)
    if pub_utc is None or pub_utc < cutoff_utc:
        return None

    eff_title = str(di.get("clean_title") or doc.get("name") or "").strip()
    if not eff_title:
        return None

    clean_fmt = {**deal_fmt, "price": round(deal_price, 2)}
    return {
        **doc,
        "source_type": SOURCE_TYPE_DEAL_TIP,
        "name": eff_title,
        "activity_price": round(deal_price, 2),
        "price": round(deal_price, 2),
        "format_info": {"deal": clean_fmt},
        "format_status": "success",
        "deal_price_logic": str(di.get("price_logic") or "").strip(),
        "deal_quality_score": di.get("quality_score"),
        "deal_publish_time": str(di.get("publish_time") or "").strip(),
    }


def _evaluate_single_category_step1(
    cat_name: str,
    raw_items: List[Tuple[Dict[str, Any], Dict[str, Any], float]],
    source_type: str = SOURCE_TYPE_OFFICIAL,
) -> Optional[Dict[str, Any]]:
    """对单个分类执行 Step 1（计算单位性价比 -> 前20候选 -> 第1个AI选拔标品 -> 正式商品官方验价/线报直接沿用）。"""
    src_type = _normalize_source_type(source_type)
    src_label = _source_label(src_type)
    dim_counts: Dict[str, Dict[str, int]] = {}
    parsed_entries = []
    for doc, sku_fmt, act_price in raw_items:
        raw_base_unit, extracted_units = _extract_raw_units(sku_fmt)
        parsed_entries.append((doc, sku_fmt, act_price, raw_base_unit, extracted_units))
        for u in extracted_units:
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

    top_unit, top20_pool = _select_top_candidates_for_category(cat_candidates, unit_groups, MAX_CATEGORY_CANDIDATES_FOR_AI1)
    active_pool = list(top20_pool)
    winner: Optional[Dict[str, Any]] = None
    main_kw = ""
    backup_kw = ""
    llm_kw_meta: Dict[str, Any] = {}

    for _verify_round in range(1, 3):
        if not active_pool:
            break
        has_std, chosen_cand, m_kw, b_kw, kw_meta = _select_standard_product_and_keywords_by_llm(cat_name, active_pool)
        llm_kw_meta = kw_meta
        if not has_std or chosen_cand is None:
            reason = (kw_meta.get("parsed_json") or {}).get("selection_reason") or "无可比标品"
            logger.info(
                f"⏭️ [步骤1/AI判定无标品跳过][{src_label}] 分类=【{cat_name}】(共{len(raw_items)}款, 送审{len(active_pool)}款) | "
                f"模型={kw_meta.get('model_used')} | 耗时={kw_meta.get('elapsed_seconds', 0)}s | 原因={reason}"
            )
            break

        if src_type == SOURCE_TYPE_DEAL_TIP:
            chosen_cand["verified_activity_price"] = chosen_cand["db_activity_price"]
            winner, main_kw, backup_kw = chosen_cand, m_kw, b_kw
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
        winner, main_kw, backup_kw = chosen_cand, m_kw, b_kw
        break

    if not winner:
        return None

    w_doc, w_sku = winner["doc"], winner["sku_fmt"]
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
    return {
        "source_type": src_type,
        "platform": normalize_platform(w_doc.get("platform") or "pdd"),
        "product_id": str(w_doc.get("product_id") or w_doc.get("goods_id") or "").strip(),
        "goods_sign": str(w_doc.get("goods_sign") or "").strip(),
        "channel_type": w_doc.get("channel_type"),
        "category_name": cat_name,
        "category_item_count": len(raw_items),
        "category_top20_count": len(top20_pool),
        "name": str(w_doc.get("name") or "").strip(),
        "brand": _resolve_candidate_brand(w_doc, w_sku),
        "db_activity_price": winner["db_activity_price"],
        "activity_price": winner["verified_activity_price"],
        "primary_unit": best_primary_unit,
        "unit_quantities": winner["unit_quantities"],
        "unit_cost_performance": winner["unit_cost_performance"],
        "decision_keywords_summary": " | ".join(kws_list),
        "pricing_basis": w_sku.get("pricing_basis") or {},
        "search_keyword": main_kw,
        "backup_search_keyword": backup_kw,
        "selection_reason": str((llm_kw_meta.get("parsed_json") or {}).get("selection_reason") or "").strip(),
        "llm_keyword_output": llm_kw_meta,
        "product_url": str(w_doc.get("product_url") or ""),
        "image_url": str(w_doc.get("image_url") or ""),
        "updated_at": str(w_doc.get("updated_at") or ""),
        "deal_price_logic": str(w_doc.get("deal_price_logic") or ""),
        "deal_quality_score": w_doc.get("deal_quality_score"),
        "deal_publish_time": str(w_doc.get("deal_publish_time") or ""),
    }


def select_verified_category_champions(
    db: Any,
    hours_window: int = 72,
    max_categories: int = 20,
    stage1_workers: int = STAGE1_WORKERS,
    source_type: str = SOURCE_TYPE_OFFICIAL,
) -> List[Dict[str, Any]]:
    """按指定 `source_type` 独立过滤未比价商品并并发调用第 1 个 AI 选出可比标品与搜索词。"""
    src_type = _normalize_source_type(source_type)
    src_label = _source_label(src_type)
    repo = ProductRepository()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc - datetime.timedelta(hours=hours_window)
    cutoff_dt = cutoff_utc.replace(tzinfo=None)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    all_eval_docs = list(eval_col.find({}, {"platform": 1, "product_id": 1, "category_name": 1, "source_type": 1}))
    same_src_eval_docs = [d for d in all_eval_docs if _normalize_source_type(d.get("source_type")) == src_type]
    evaluated_keys = {
        (normalize_platform(d.get("platform") or "pdd"), str(d.get("product_id") or "").strip())
        for d in same_src_eval_docs
        if d.get("product_id")
    }
    cat_eval_counts: Dict[str, int] = defaultdict(int)
    for d in same_src_eval_docs:
        cname = str(d.get("category_name") or "").strip()
        if cname:
            cat_eval_counts[cname] += 1
    for (st, cname), skip_cnt in _SESSION_SKIPPED_CAT_COUNTS.items():
        if st == src_type:
            cat_eval_counts[cname] += skip_cnt

    if src_type == SOURCE_TYPE_DEAL_TIP:
        query = {
            "$and": [
                {"deal_info.is_deal_tip": True},
                {"deal_info.format_status": "success"},
                {"deal_info.need_reformat": {"$ne": True}},
                {"deal_info.deal_price": {"$type": "number", "$gt": 0}},
                {"$or": [{"updated_at": {"$gte": cutoff_dt}}, {"updated_at": {"$gte": cutoff_str}}]},
            ]
        }
        projection = {
            **repo._SUMMARY_PROJECTION,
            "channel_type": 1,
            "deal_info.is_deal_tip": 1,
            "deal_info.clean_title": 1,
            "deal_info.deal_price": 1,
            "deal_info.format_deal_price": 1,
            "deal_info.format_status": 1,
            "deal_info.need_reformat": 1,
            "deal_info.format_info": 1,
            "deal_info.price_logic": 1,
            "deal_info.quality_score": 1,
            "deal_info.publish_time": 1,
            "deal_info.format_updated_at": 1,
        }
        raw_docs = [
            vdoc
            for d in repo.col.find(query, projection).sort([("updated_at", -1)])
            if (vdoc := _build_deal_tip_virtual_doc(d, cutoff_utc=cutoff_utc)) is not None
        ]
    else:
        query = {
            "$and": [
                repo._boom_content_filter(),
                {
                    "format_status": "success",
                    "$or": [{"updated_at": {"$gte": cutoff_dt}}, {"updated_at": {"$gte": cutoff_str}}],
                },
            ]
        }
        projection = {**repo._COST_PERFORMANCE_PROJECTION, "channel_type": 1}
        raw_docs = [
            {**d, "source_type": SOURCE_TYPE_OFFICIAL}
            for d in repo.col.find(query, projection).sort([("updated_at", -1)])
        ]

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
        if cat_and_sku:
            by_cat[cat_and_sku[0]].append((doc, cat_and_sku[1], act_price))

    sorted_cats = sorted(by_cat.items(), key=lambda kv: (cat_eval_counts.get(kv[0], 0), -len(kv[1])))
    top_preview = ", ".join(f"{k}({len(v)}款/已比{cat_eval_counts.get(k, 0)}次)" for k, v in sorted_cats[:20])
    target_cat_desc = "全部" if max_categories <= 0 else str(max_categories)
    workers = max(1, int(stage1_workers or STAGE1_WORKERS))
    logger.info(
        f"📊 [步骤1/源头扫描][{src_label}/{hours_window}h] 满足保质期且已结构化商品={len(raw_docs)} | "
        f"已过滤同源历史已比价商品={skipped_evaluated_cnt} | 剩余未比价分类数={len(sorted_cats)} | "
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
                (cat_name, raw_items, executor.submit(_evaluate_single_category_step1, cat_name, raw_items, src_type))
                for cat_name, raw_items in batch_slice
            ]
            for cat_name, raw_items, fut in future_pairs:
                try:
                    champ = fut.result()
                except Exception as exc:
                    logger.warning(f"⚠️ [步骤1/分类选拔异常][{src_label}] 分类=【{cat_name}】 | 异常={exc}")
                    continue
                if champ is None:
                    _SESSION_SKIPPED_CAT_COUNTS[(src_type, cat_name)] += 1
                    continue
                if max_categories > 0 and len(champions) >= max_categories:
                    continue
                champions.append(champ)
                llm_kw_meta = champ.get("llm_keyword_output") or {}
                price_desc = (
                    f"线报deal_price=¥{champ['activity_price']}"
                    if src_type == SOURCE_TYPE_DEAL_TIP
                    else f"验价后activity_price=¥{champ['activity_price']} (入库=¥{champ['db_activity_price']})"
                )
                logger.info(
                    f"🎯 [步骤1/AI标品入选 #{len(champions)}][{src_label}] 分类=【{cat_name}】(共{len(raw_items)}款, AI审{champ['category_top20_count']}款) | "
                    f"[{champ['platform'].upper()}:{champ['product_id']}] | {price_desc} | "
                    f"各unit数量={champ['unit_quantities']} | 各unit性价比={champ['unit_cost_performance']} | "
                    f"闲鱼搜索词=【{champ['search_keyword']}】(备用=【{champ['backup_search_keyword']}】, 模型={llm_kw_meta.get('model_used')}) | "
                    f"选拔理由={champ['selection_reason']} | 标题={champ['name'][:36]}"
                )

    return champions


# ==============================================================================
# 步骤 2 & 步骤 3：小号搜索 -> AI 初筛 -> H5免登录批量详情 -> AI 终审含邮比价
# ==============================================================================
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


def _validate_step2_coarse_filter_response(
    parsed: Any,
    valid_xianyu_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
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
        return False, f"comparable_item_ids 中包含不在本次搜索结果列表中的非法ID: {invalid_ids}", {}
    if has_cand and not clean_ids:
        return False, "has_comparable_candidates=true 时 comparable_item_ids 不能为空列表", {}
    if not has_cand:
        clean_ids = []

    return True, "ok", {**parsed, "has_comparable_candidates": bool(clean_ids), "comparable_item_ids": clean_ids}


def _filter_comparable_xianyu_items_by_llm(
    my_candidate_for_llm: Dict[str, Any],
    coarse_items_for_llm: List[Dict[str, Any]],
    valid_xianyu_ids: Set[str],
) -> Tuple[List[str], Dict[str, Any], str]:
    """【AI 第 2 步】：筛选属于同品牌、同款/同系列的可比闲鱼商品 ID 列表。"""
    prompt_template = read_file_to_str(COARSE_FILTER_PROMPT_PATH)
    payload = {
        "my_candidate_product": my_candidate_for_llm,
        "xianyu_search_items": coarse_items_for_llm,
    }
    base_prompt = (
        f"{prompt_template}\n"
        f"<coarse_filter_input>\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</coarse_filter_input>"
    )

    ok, clean_dict, llm_meta, last_err = _call_llm_json(
        base_prompt=base_prompt,
        validator_fn=lambda p: _validate_step2_coarse_filter_response(p, valid_xianyu_ids),
        step_label="步骤2/AI同款初筛",
        id_hint_msg=f"请务必确保 `comparable_item_ids` 中的每个 ID 均严格取自给定的闲鱼列表 ID 集合：{sorted(valid_xianyu_ids)}，并严格输出合法 JSON！",
    )
    if not ok:
        return [], llm_meta, f"AI同款初筛校验失败: {last_err}"

    comp_ids = clean_dict.get("comparable_item_ids") or []
    filter_reason = str(clean_dict.get("filter_reason") or "").strip()
    logger.info(
        f"🔎 [步骤2/AI同款初筛完成] 模型={llm_meta.get('model_used')} | 尝试={llm_meta.get('attempt_used')}/{LLM_MAX_ATTEMPTS} | "
        f"耗时={llm_meta.get('elapsed_seconds', 0):.1f}s | 初筛同款可比数={len(comp_ids)}/{len(coarse_items_for_llm)} | "
        f"IDs={comp_ids} | 理由={filter_reason}"
    )
    return comp_ids, llm_meta, filter_reason


def _validate_llm_comparison_response(
    parsed: Any,
    valid_xianyu_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
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
            return False, f"返回的闲鱼商品编号 best_xianyu_item_id='{best_id}' 不在已拉取详情的闲鱼商品ID列表中({sorted(valid_xianyu_ids)})", {}
        if _num(parsed.get("best_xianyu_price"), 0.0) <= 0:
            return False, f"best_xianyu_price={parsed.get('best_xianyu_price')} 必须大于 0", {}
        if not isinstance(parsed.get("xianyu_unit_cost_performance"), dict):
            return False, "xianyu_unit_cost_performance 必须是字典(dict)", {}
    else:
        out["llm_winner"] = "no_comparable"
        out["best_xianyu_item_id"] = ""
        out["comparable_item_ids"] = []

    return True, "ok", out


# ==============================================================================
# 闲鱼商品表（xianyu_items）持久化与 7 天商品详情缓存复用
# ==============================================================================
def _save_xianyu_search_items(
    items_col: Optional[Any],
    raw_items: List[Dict[str, Any]],
    keyword: str = "",
) -> int:
    """
    将闲鱼搜索返回的商品概览数据保存/更新到 xianyu_items 表（按唯一标识 item_id 去重入库）。
    注意：仅更新搜索概览与更新时间，绝不覆盖该商品已有的完整详情字段（detail / has_detail / detail_updated_at）。
    """
    if items_col is None or not raw_items:
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_str = now_utc.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    kw_clean = str(keyword or "").strip()

    dedup_by_id: Dict[str, Dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("item_id") or "").strip()
        if iid and iid not in dedup_by_id:
            dedup_by_id[iid] = item

    if not dedup_by_id:
        return 0

    ops: List[UpdateOne] = []
    for iid, card in dedup_by_id.items():
        item_url = str(card.get("item_url") or f"https://www.goofish.com/item?id={iid}").strip()
        clean_card = {
            "item_id": iid,
            "item_url": item_url,
            "title": str(card.get("title") or ""),
            "price": _num(card.get("price"), 0.0),
            "raw_price_text": str(card.get("raw_price_text") or ""),
            "original_price": _num(card.get("original_price"), 0.0),
            "want_cnt": int(_num(card.get("want_cnt"), 0.0)),
            "is_free_shipping": bool(card.get("is_free_shipping")),
            "area": str(card.get("area") or ""),
            "seller_nick": str(card.get("seller_nick") or ""),
            "seller_tags": list(card.get("seller_tags") or []),
            "image_url": str(card.get("image_url") or ""),
        }
        set_fields: Dict[str, Any] = {
            "item_id": iid,
            "item_url": item_url,
            "search_card": clean_card,
            "last_search_title": clean_card["title"],
            "last_search_price": clean_card["price"],
            "search_updated_at": now_utc,
            "search_updated_at_str": now_str,
            "updated_at": now_utc,
            "updated_at_str": now_str,
        }
        if kw_clean:
            set_fields["last_search_keyword"] = kw_clean

        ops.append(
            UpdateOne(
                {"item_id": iid},
                {
                    "$set": set_fields,
                    "$setOnInsert": {
                        "created_at": now_utc,
                        "created_at_str": now_str,
                        "has_detail": False,
                    },
                },
                upsert=True,
            )
        )

    try:
        items_col.bulk_write(ops, ordered=False)
        return len(ops)
    except Exception as exc:
        logger.warning(f"⚠️ [闲鱼商品表/搜索概览入库异常] count={len(ops)} | 异常={exc}")
        return 0


def _load_cached_xianyu_details(
    items_col: Optional[Any],
    item_ids: List[str],
    cache_days: int = XIANYU_DETAIL_CACHE_DAYS,
) -> Dict[str, Dict[str, Any]]:
    """
    在拉取 H5 详情前先查询 xianyu_items 表：
    若商品已存有完整详情（has_detail=True）且 detail_updated_at 在有效期内（默认 7 天），直接返回复用，不再打开详情页。
    """
    if items_col is None or not item_ids or cache_days <= 0:
        return {}

    clean_ids = list(dict.fromkeys(str(x).strip() for x in item_ids if str(x).strip()))
    if not clean_ids:
        return {}

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc - datetime.timedelta(days=float(cache_days))
    cached_by_id: Dict[str, Dict[str, Any]] = {}

    try:
        cursor = items_col.find({"item_id": {"$in": clean_ids}, "has_detail": True})
        for doc in cursor or []:
            if not isinstance(doc, dict):
                continue
            iid = str(doc.get("item_id") or "").strip()
            if not iid:
                continue
            det_utc = _parse_deal_time_utc(doc.get("detail_updated_at"), default_tz=datetime.timezone.utc)
            if det_utc is None:
                det_utc = _parse_deal_time_utc(doc.get("detail_updated_at_str"), default_tz=_CN_TZ)
            if det_utc is None or det_utc < cutoff_utc:
                continue
            det = doc.get("detail")
            if not isinstance(det, dict) or not det.get("ok"):
                continue
            det_copy = dict(det)
            det_copy["ok"] = True
            det_copy["item_id"] = iid
            det_copy["_from_db_cache"] = True
            det_copy["_detail_updated_at_str"] = str(
                doc.get("detail_updated_at_str") or det_utc.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            )
            cached_by_id[iid] = det_copy
    except Exception as exc:
        logger.warning(f"⚠️ [闲鱼商品表/读取7天缓存详情异常] IDs={clean_ids} | 异常={exc}")
        return {}

    return cached_by_id


def _save_xianyu_item_details(
    items_col: Optional[Any],
    details_by_id: Dict[str, Dict[str, Any]],
) -> int:
    """
    将成功拉取的闲鱼商品详情数据（含完整文案、多规格价格、运费等）保存/更新至 xianyu_items 表，
    记录唯一标识 item_id 与 detail_updated_at，供后续 7 天内直接复用。失败或异常数据不写入详情有效态。
    """
    if items_col is None or not details_by_id:
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_str = now_utc.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    ops: List[UpdateOne] = []

    for raw_id, det in details_by_id.items():
        if not isinstance(det, dict) or not det.get("ok"):
            continue
        iid = str(det.get("item_id") or raw_id or "").strip()
        if not iid:
            continue
        item_url = str(det.get("item_url") or f"https://www.goofish.com/item?id={iid}").strip()
        price_val = _num(det.get("price"), 0.0)
        clean_det = {
            "ok": True,
            "item_id": iid,
            "item_url": item_url,
            "title": str(det.get("title") or ""),
            "desc": str(det.get("desc") or ""),
            "price": price_val,
            "original_price": _num(det.get("original_price"), 0.0),
            "transport_fee": None if det.get("transport_fee") is None else _num(det.get("transport_fee"), 0.0),
            "is_free_shipping": bool(det.get("is_free_shipping")),
            "min_price": _num(det.get("min_price"), price_val),
            "max_price": _num(det.get("max_price"), price_val),
            "has_multi_sku": bool(det.get("has_multi_sku")),
            "sku_list": list(det.get("sku_list") or []),
            "cpv_labels": list(det.get("cpv_labels") or []),
            "want_cnt": None if det.get("want_cnt") is None else int(_num(det.get("want_cnt"), 0.0)),
            "browse_cnt": int(_num(det.get("browse_cnt"), 0.0)),
            "quantity": int(_num(det.get("quantity"), 1.0)),
            "images": list(det.get("images") or []),
            "seller_nick": str(det.get("seller_nick") or ""),
            "seller_city": str(det.get("seller_city") or ""),
            "seller_sold_count": int(_num(det.get("seller_sold_count"), 0.0)),
            "seller_good_rate": str(det.get("seller_good_rate") or ""),
        }
        set_fields = {
            "item_id": iid,
            "item_url": item_url,
            "has_detail": True,
            "detail": clean_det,
            "detail_updated_at": now_utc,
            "detail_updated_at_str": now_str,
            "updated_at": now_utc,
            "updated_at_str": now_str,
            "title": clean_det["title"],
            "desc": clean_det["desc"],
            "price": clean_det["price"],
            "original_price": clean_det["original_price"],
            "transport_fee": clean_det["transport_fee"],
            "is_free_shipping": clean_det["is_free_shipping"],
            "min_price": clean_det["min_price"],
            "max_price": clean_det["max_price"],
            "has_multi_sku": clean_det["has_multi_sku"],
            "sku_list": clean_det["sku_list"],
            "cpv_labels": clean_det["cpv_labels"],
            "want_cnt": clean_det["want_cnt"],
            "browse_cnt": clean_det["browse_cnt"],
            "quantity": clean_det["quantity"],
            "images": clean_det["images"],
            "seller_nick": clean_det["seller_nick"],
            "seller_city": clean_det["seller_city"],
            "seller_sold_count": clean_det["seller_sold_count"],
            "seller_good_rate": clean_det["seller_good_rate"],
        }
        ops.append(
            UpdateOne(
                {"item_id": iid},
                {
                    "$set": set_fields,
                    "$setOnInsert": {
                        "created_at": now_utc,
                        "created_at_str": now_str,
                    },
                },
                upsert=True,
            )
        )

    if not ops:
        return 0

    try:
        items_col.bulk_write(ops, ordered=False)
        return len(ops)
    except Exception as exc:
        logger.warning(f"⚠️ [闲鱼商品表/商品详情入库异常] count={len(ops)} | 异常={exc}")
        return 0


def probe_xianyu_search_ready(
    search_user_data_dir: str = SEARCH_USER_DATA_DIR,
    debug_browser: bool = False,
    probe_keyword: str = PRE_ROUND_PROBE_KEYWORD,
) -> Tuple[bool, Dict[str, Any]]:
    """开轮轻量探针：在调用大模型选拔标品前，先用搜索小号目录执行 1 次轻量闲鱼搜索，未登录或限流立即中止整轮。"""
    kw = str(probe_keyword or "抽纸").strip() or "抽纸"
    logger.info(
        f"🩺 [开轮探针检测] 正在执行 1 次轻量闲鱼检索（小号目录=<{search_user_data_dir}> | 探针词=【{kw}】），确认搜索通道是否就绪..."
    )
    res = search_xianyu_items(
        keyword=kw,
        max_items=5,
        user_data_dir=search_user_data_dir,
        debug=debug_browser,
    )
    if res.get("ok"):
        logger.info(f"✅ [开轮探针通过] 搜索通道正常（返回 {len(res.get('items') or [])} 条），开始本轮标品选拔与比价...")
        return True, res
    return False, res


def compare_candidate_with_xianyu(
    candidate: Dict[str, Any],
    search_user_data_dir: str = SEARCH_USER_DATA_DIR,
    guest_user_data_dir: str = GUEST_USER_DATA_DIR,
    max_search_items: int = MAX_XIANYU_SEARCH_ITEMS,
    max_detail_checks: int = MAX_DETAIL_CHECKS_PER_CATEGORY,
    debug_browser: bool = False,
    items_col: Optional[Any] = None,
    detail_cache_days: int = XIANYU_DETAIL_CACHE_DAYS,
) -> Dict[str, Any]:
    """
    执行单分类标品闲鱼比价（小号仅搜索 + xianyu_items 7天详情缓存复用 + H5免登录按需补拉详情）：
    - 搜索报错（掉线/风控/异常）时立即返回 browser_aborted 记录，0 次备用词盲目重试；
    - 每次搜索成功后自动把闲鱼商品概览写入/更新到 xianyu_items 表；
    - 仅当主搜索成功但条数 < 6 或 AI 初筛 0 同款时，最多切换 1 次备用词扩召回；
    - 初筛同款先查 xianyu_items 表复用 7 天内已有的完整详情，仅对缺失/过期商品走免登录 H5 批量获取详情并回写数据库，再交由第 3 个 AI 终审比价。
    """
    kw = candidate["search_keyword"]
    backup_kw = candidate["backup_search_keyword"]
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

    def _run_search(target_kw: str, label: str) -> Dict[str, Any]:
        res = search_xianyu_items(
            keyword=target_kw,
            max_items=max_search_items,
            user_data_dir=search_user_data_dir,
            debug=debug_browser,
        )
        if not res.get("ok"):
            logger.error(
                f"❌ [步骤2/{label}失败] 分类=【{cat_name}】 | 搜索词=【{target_kw}】 | "
                f"错误码={res.get('error_code')} | 目录={res.get('user_data_dir')} | 原因={res.get('message')}"
            )
        elif res.get("items"):
            _save_xianyu_search_items(items_col, res.get("items") or [], keyword=target_kw)
        return res

    def _build_search_aborted_record(failed_kw: str, failed_res: Dict[str, Any]) -> Dict[str, Any]:
        err_msg = f"[闲鱼搜索异常:{failed_res.get('error_code') or 'UNKNOWN'}] {failed_res.get('message') or '未知检索异常'}"
        return _build_final_record(
            candidate=candidate,
            used_keyword=failed_kw,
            search_res=failed_res,
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

    logger.info(
        f"🔍 [步骤2/闲鱼小号搜索] 分类=【{cat_name}】 | 标品=【{candidate['name'][:32]}】 | "
        f"我方价=¥{candidate['activity_price']} | 搜索词=【{kw}】"
    )
    search_res = _run_search(kw, "闲鱼小号搜索")
    if not search_res.get("ok"):
        return _build_search_aborted_record(kw, search_res)

    raw_items = search_res.get("items") or []
    used_backup = False

    # 情况 A：主搜索正常成功但结果 < 6 条时，切换备用搜索词扩召回
    if len(raw_items) < 6 and backup_kw and backup_kw != kw:
        used_backup = True
        logger.info(f"🔄 [步骤2/搜索词扩召回] 分类=【{cat_name}】 | 主词【{kw}】仅返回 {len(raw_items)} 条，切换备用词【{backup_kw}】重搜...")
        retry_res = _run_search(backup_kw, "备用词搜索")
        if not retry_res.get("ok"):
            return _build_search_aborted_record(backup_kw, retry_res)
        if len(retry_res.get("items") or []) > len(raw_items):
            search_res, raw_items, kw = retry_res, retry_res.get("items") or [], backup_kw

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
    step2_comp_ids, step2_llm_meta, step2_reason = _filter_comparable_xianyu_items_by_llm(
        my_candidate_for_llm, coarse_items_for_llm, valid_coarse_ids
    )

    # 情况 B：主搜索词初筛同款为 0 且尚未试过备用词，切换备用词重搜 + 重新初筛 1 次
    if not step2_comp_ids and not used_backup and backup_kw and backup_kw != kw:
        logger.info(f"🔄 [步骤2/初筛0同款触发备用词重搜] 分类=【{cat_name}】 | 主词【{kw}】初筛同款为0，切换备用词【{backup_kw}】重搜...")
        retry_res = _run_search(backup_kw, "初筛后备用词搜索")
        if not retry_res.get("ok"):
            return _build_search_aborted_record(backup_kw, retry_res)
        retry_items = retry_res.get("items") or []
        if retry_items:
            search_res, raw_items, kw = retry_res, retry_items, backup_kw
            coarse_items_for_llm, raw_card_map, valid_coarse_ids = _prepare_coarse_items_for_llm(raw_items)
            step2_comp_ids, step2_llm_meta, step2_reason = _filter_comparable_xianyu_items_by_llm(
                my_candidate_for_llm, coarse_items_for_llm, valid_coarse_ids
            )

    if not step2_comp_ids:
        logger.info(f"⚪ [步骤2/初筛无同款可比商品] 分类=【{cat_name}】 | 搜索词=【{kw}】 | 原因={step2_reason}")
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
            browser_aborted=False,
            debug_browser=debug_browser,
        )

    # 步骤 3：先查 xianyu_items 表复用 7 天内已有的完整详情，仅对缺失/过期商品通过 H5 免登录批量补拉详情
    ids_to_fetch = step2_comp_ids[:max_detail_checks] if max_detail_checks > 0 else step2_comp_ids
    cached_details_by_id = _load_cached_xianyu_details(
        items_col=items_col,
        item_ids=ids_to_fetch,
        cache_days=detail_cache_days,
    )
    missing_ids = [iid for iid in ids_to_fetch if iid not in cached_details_by_id]
    detail_cache_hit_count = len(cached_details_by_id)
    detail_live_fetched_count = 0

    if not missing_ids:
        logger.info(
            f"⚡ [步骤3/闲鱼详情库缓存全命中] 分类=【{cat_name}】 | 初筛前 {len(ids_to_fetch)} 款同款在 {detail_cache_days} 天内均有完整详情，"
            f"直接复用数据库详情（0 次 H5 详情页请求）！"
        )
        batch_det_res: Dict[str, Any] = {
            "ok": True,
            "is_offline": False,
            "is_risk_blocked": False,
            "user_data_dir": guest_user_data_dir,
            "details_by_id": dict(cached_details_by_id),
        }
    else:
        if detail_cache_hit_count > 0:
            logger.info(
                f"📦 [步骤3/详情缓存复用+H5按需补拉] 分类=【{cat_name}】 | 待核验={len(ids_to_fetch)}款 "
                f"(库内{detail_cache_days}天缓存直接复用={detail_cache_hit_count}款，仅需H5补拉={len(missing_ids)}款) -> "
                f"免登录目录<{guest_user_data_dir}>批量拉取..."
            )
        else:
            logger.info(
                f"📦 [步骤3/H5免登录批量拉取详情] 分类=【{cat_name}】 | AI初筛同款={len(step2_comp_ids)}款 -> "
                f"免登录目录<{guest_user_data_dir}>批量拉取前 {len(ids_to_fetch)} 款详情..."
            )
        batch_det_res = get_xianyu_item_details_batch(
            item_ids=missing_ids,
            user_data_dir=guest_user_data_dir,
            debug=debug_browser,
        )
        live_details_map = batch_det_res.get("details_by_id") or {}
        detail_live_fetched_count = sum(1 for v in live_details_map.values() if isinstance(v, dict) and v.get("ok"))
        if detail_live_fetched_count > 0:
            _save_xianyu_item_details(items_col, live_details_map)

        if not batch_det_res.get("ok") or batch_det_res.get("is_risk_blocked"):
            logger.error(
                f"❌ [步骤3/批量详情会话异常中断] 分类=【{cat_name}】 | 错误码={batch_det_res.get('error_code')} | "
                f"目录={batch_det_res.get('user_data_dir')} | 已完成={len(live_details_map)}/{len(missing_ids)} | "
                f"原因={batch_det_res.get('message')}"
            )

    details_by_id = {**cached_details_by_id, **(batch_det_res.get("details_by_id") or {})}
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
            {"spec_text": s.get("spec_text"), "price": s.get("price"), "quantity": s.get("quantity")}
            for s in (det.get("sku_list") or [])[:30]
        ]
        valid_detailed_ids.add(iid)
        detailed_items_for_llm.append({
            "item_id": iid,
            "title": det.get("title") or raw_card.get("title"),
            "display_price": raw_card.get("price") or det.get("price"),
            "want_cnt": det.get("want_cnt") if det.get("want_cnt") is not None else raw_card.get("want_cnt"),
            "is_free_shipping": det.get("is_free_shipping"),
            "detail_verified_info": {
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
            },
        })
        det_src_tag = f"库缓存:{det.get('_detail_updated_at_str', '7天内')}" if det.get("_from_db_cache") else "H5新拉"
        logger.info(
            f"   ├─ ✅ ({idx}/{len(ids_to_fetch)}) 详情核验就绪 [XY:{iid}][{det_src_tag}] | "
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
            detail_cache_hit_count=detail_cache_hit_count,
            detail_live_fetched_count=detail_live_fetched_count,
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

    ok, validated_llm_data, llm_call_meta, last_error_msg = _call_llm_json(
        base_prompt=base_prompt,
        validator_fn=lambda p: _validate_llm_comparison_response(p, valid_detailed_ids),
        step_label=f"步骤3/AI终审比价({cat_name})",
        id_hint_msg=f"请务必确保 `best_xianyu_item_id` 严格取自给定的已拉取详情闲鱼列表 ID 集合：{sorted(valid_detailed_ids)}，并严格输出合法 JSON！",
    )
    if ok:
        logger.info(
            f"🤖 [步骤3/AI终审比价校验通过] 分类=【{cat_name}】 | 模型={llm_call_meta.get('model_used')} | "
            f"尝试={llm_call_meta.get('attempt_used')}/{LLM_MAX_ATTEMPTS} | 耗时={llm_call_meta.get('elapsed_seconds', 0):.1f}s | "
            f"最佳闲鱼ID={validated_llm_data.get('best_xianyu_item_id')} | 含邮总价=¥{validated_llm_data.get('best_xianyu_price')} | "
            f"llm_winner={validated_llm_data.get('llm_winner')}"
        )

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
        fallback_error="" if ok else last_error_msg,
        batch_det_res=batch_det_res,
        detail_failed_count=detail_failed_count,
        detail_cache_hit_count=detail_cache_hit_count,
        detail_live_fetched_count=detail_live_fetched_count,
        browser_aborted=False,
        debug_browser=debug_browser,
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
    batch_det_res: Optional[Dict[str, Any]] = None,
    detail_failed_count: int = 0,
    detail_cache_hit_count: int = 0,
    detail_live_fetched_count: int = 0,
    browser_aborted: bool = False,
    debug_browser: bool = False,
) -> Dict[str, Any]:
    """计算我方候选标品与最佳闲鱼同款商品的单位性价比比值并构建入库文档。"""
    my_cp_map: Dict[str, float] = candidate["unit_cost_performance"]
    has_comp = bool(llm_data.get("has_comparable_item"))
    best_xy_id = str(llm_data.get("best_xianyu_item_id") or "").strip()
    best_xy_price = round(_num(llm_data.get("best_xianyu_price"), 0.0), 2)

    raw_xy_qty = llm_data.get("xianyu_unit_quantities") if isinstance(llm_data.get("xianyu_unit_quantities"), dict) else {}
    raw_xy_cp = llm_data.get("xianyu_unit_cost_performance") if isinstance(llm_data.get("xianyu_unit_cost_performance"), dict) else {}

    xy_qty_clean: Dict[str, float] = {str(u).strip(): round(_num(v), 4) for u, v in raw_xy_qty.items() if _num(v) > 0}
    xy_cp_clean: Dict[str, float] = {str(u).strip(): round(_num(v), 6) for u, v in raw_xy_cp.items() if _num(v) > 0}
    if best_xy_price > 0:
        for u, q in xy_qty_clean.items():
            xy_cp_clean[u] = round(q / best_xy_price, 6)

    # 同物理度量衡单位换算对齐（如 kg <-> g, L <-> ml）
    for my_u in my_cp_map:
        if my_u in xy_cp_clean:
            continue
        my_dim = UNIT_TO_CATEGORY.get(my_u)
        if not my_dim:
            continue
        for xy_u, xy_cp_val in list(xy_cp_clean.items()):
            if UNIT_TO_CATEGORY.get(xy_u) == my_dim and xy_cp_val > 0:
                ratio_mult = UNIT_TO_MULTIPLIER[xy_u] / UNIT_TO_MULTIPLIER[my_u]
                xy_cp_clean[my_u] = round(xy_cp_val * ratio_mult, 6)
                if xy_u in xy_qty_clean and my_u not in xy_qty_clean:
                    xy_qty_clean[my_u] = round(xy_qty_clean[xy_u] * ratio_mult, 4)
                break

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
        ranked_matched_units = sorted(
            matched_unit_ratios.keys(),
            key=lambda u: (_unit_priority_score(u), 1 if u == primary_unit else 0),
            reverse=True,
        )
        chosen_unit = ranked_matched_units[0]
        cp_ratio = matched_unit_ratios[chosen_unit]
        comparison_basis = "unit_ratio"
        final_winner = "my_product" if cp_ratio > 1.0 else ("xianyu_product" if cp_ratio < 1.0 else "tie")
    else:
        chosen_unit = None
        cp_ratio = None
        if browser_aborted:
            comparison_basis = "browser_error"
            final_winner = "browser_error"
        else:
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

    src_type = _normalize_source_type(candidate.get("source_type"))
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "source_type": src_type,
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
        },
        "xianyu_audit_trail": {
            "browser_mode": "headless" if not debug_browser else "headed",
            "browser_aborted": bool(browser_aborted),
            "is_risk_blocked": bool(search_res.get("is_risk_blocked") or (batch_det_res or {}).get("is_risk_blocked")),
            "is_account_offline": bool(search_res.get("error_code") == "ACCOUNT_OFFLINE" or search_res.get("is_offline")),
            "search_keyword": used_keyword,
            "search_ok": bool(search_res.get("ok")),
            "search_error_code": str(search_res.get("error_code") or ""),
            "search_error_message": str(search_res.get("message") or "") if not search_res.get("ok") else "",
            "raw_search_count": len(coarse_items_for_llm),
            "step2_comparable_item_ids": step2_comparable_ids,
            "batch_detail_ok": bool((batch_det_res or {}).get("ok", True)),
            "batch_detail_error_code": str((batch_det_res or {}).get("error_code") or ""),
            "step3_detail_fetched_count": len(xianyu_items_for_llm),
            "step3_detail_cache_hit_count": int(detail_cache_hit_count),
            "step3_detail_live_fetched_count": int(detail_live_fetched_count),
            "step3_detail_failed_count": int(detail_failed_count),
            "comparable_item_ids": llm_data.get("comparable_item_ids") or [],
            "xianyu_coarse_items": coarse_items_for_llm,
            "xianyu_items_fed_to_llm": xianyu_items_for_llm,
        },
    }


# ==============================================================================
# 日志报表输出与数据库索引初始化
# ==============================================================================
def _build_verdict_summary(record: Dict[str, Any]) -> Tuple[str, str, str]:
    cr = record.get("comparison_result") or {}
    cand = record.get("candidate_product") or {}
    bxy = record.get("best_xianyu_product") or {}
    audit = record.get("xianyu_audit_trail") or {}
    final_winner = str(record.get("final_winner") or "no_comparable")
    basis = str(cr.get("comparison_basis") or "no_comparable")
    matched_u = cr.get("matched_unit")
    cp_ratio = cr.get("cost_performance_ratio")
    my_cp_map = cr.get("my_unit_cost_performance") or {}
    xy_cp_map = cr.get("xianyu_unit_cost_performance") or {}

    if audit.get("browser_aborted") or final_winner == "browser_error":
        return (
            "🚨 检索异常",
            "ERR",
            f"闲鱼浏览器检索异常中断：{cr.get('llm_reason') or '请搜索上方 ❌ 错误日志定位原因'}",
        )

    if not cr.get("has_comparable_item") or final_winner == "no_comparable":
        return "⚪ 无可比款", "N/A", "闲鱼未搜索到同品牌同规格且邮费/规格清晰的可比商品"

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
        if final_winner == "xianyu_product":
            pct = (1.0 - cp_ratio) * 100.0
            quant_str = (
                f"输在含邮单价偏高：按单位[{matched_u}]对齐，我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u} "
                f"(每元可得 {my_u_cp}{matched_u}) vs 闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u} "
                f"(每元可得 {xy_u_cp}{matched_u}) -> 我方性价比仅为闲鱼的 {cp_ratio:.2f} 倍 (低了 -{pct:.1f}%)"
            )
            return "🏳️ 闲鱼更优", ratio_str, quant_str
        return "🤝 双方持平", ratio_str, f"双方持平：按单位[{matched_u}]对齐，双方每元均可买 {my_u_cp}{matched_u} (比值 {cp_ratio:.2f}x)"

    ratio_str = "AI跨单位判定"
    if final_winner == "my_product":
        return (
            "🏆 我方胜出",
            ratio_str,
            f"赢在整体规格价格比（跨单位AI兜底判定）：我方 ¥{record['activity_price']:.2f} "
            f"[{_fmt_qty_human(cand.get('unit_quantities') or {})}] vs "
            f"闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} [{_fmt_qty_human(bxy.get('unit_quantities') or {})}]",
        )
    if final_winner == "xianyu_product":
        return (
            "🏳️ 闲鱼更优",
            ratio_str,
            f"输在整体规格价格比（跨单位AI兜底判定）：我方 ¥{record['activity_price']:.2f} "
            f"[{_fmt_qty_human(cand.get('unit_quantities') or {})}] vs "
            f"闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} [{_fmt_qty_human(bxy.get('unit_quantities') or {})}]",
        )
    return "🤝 双方持平", ratio_str, "跨单位AI判定双方性价比相当"


def _format_champions_summary_table(
    champions: List[Dict[str, Any]],
    source_type: Optional[str] = None,
) -> str:
    sep = "-" * 136
    if source_type:
        src_note = (
            "【线报爆料 / 48h保质期】直接采用线报 deal_price"
            if _normalize_source_type(source_type) == SOURCE_TYPE_DEAL_TIP
            else "【正式官价 / 72h保质期】已通过官方接口实时核验价格"
        )
    else:
        src_note = "包含启用商品源的全部入选标品"
    lines = [
        "",
        "=" * 136,
        f"📋 【阶段 1 汇总：本轮由第 1 个 AI 选出的 {len(champions)} 个分类高性价比标品清单】{src_note}",
        sep,
        (
            f"{_pad_col('序号', 4)} | {_pad_col('分类名称(库内数)', 18)} | {_pad_col('平台与商品ID', 19)} | "
            f"{_pad_col('比价基准价', 10)} | {_pad_col('规格数量', 16)} | {_pad_col('我方单位性价比(每1元)', 24)} | "
            f"{_pad_col('闲鱼搜索词', 18)} | 来源与我方标品标题"
        ),
        sep,
    ]
    for i, c in enumerate(champions, 1):
        src_lbl = _source_label(c.get("source_type"))
        cat_label = f"{c['category_name']}({c.get('category_item_count', 0)}款)"
        plat_id = f"{str(c['platform']).upper()}:{c['product_id']}"
        p_unit = c.get("primary_unit")
        qty_str = _fmt_qty_human(c.get("unit_quantities") or {}, p_unit)
        cp_str = _fmt_cp_human(c.get("unit_cost_performance") or {}, p_unit)
        desc_str = f"[{src_lbl}] {c['name']} [{c.get('decision_keywords_summary') or '无扩展属性'}]"
        price_str = f"¥{c['activity_price']:.2f}"
        lines.append(
            f"{_pad_col(f'#{i}', 4)} | {_pad_col(cat_label, 18)} | {_pad_col(plat_id, 19)} | "
            f"{_pad_col(price_str, 10)} | {_pad_col(qty_str, 16)} | "
            f"{_pad_col(cp_str, 24)} | {_pad_col(str(c.get('search_keyword') or ''), 18)} | {desc_str}"
        )
    lines.append("=" * 136)
    return "\n".join(lines)


def _format_single_comparison_card(idx: int, total: int, record: Dict[str, Any]) -> str:
    cand = record.get("candidate_product") or {}
    bxy = record.get("best_xianyu_product") or {}
    cr = record.get("comparison_result") or {}
    audit = record.get("xianyu_audit_trail") or {}
    src_type = _normalize_source_type(record.get("source_type") or cand.get("source_type"))
    src_lbl = _source_label(src_type)

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
    elif audit.get("browser_aborted"):
        xy_plat_id, xy_price, xy_qty, xy_cp = "XY :检索异常中断", "-", "-", "-"
        xy_desc = cr.get("llm_reason") or "闲鱼无头浏览器检索异常中断，详见上方 ❌ 错误日志"
    else:
        xy_plat_id, xy_price, xy_qty, xy_cp = "XY :无可比商品", "-", "-", "-"
        xy_desc = "未在闲鱼搜索结果中找到同品牌同规格且邮费清晰的可比商品"

    det_ok = audit.get("step3_detail_fetched_count", 0)
    det_cache = audit.get("step3_detail_cache_hit_count", 0)
    det_fail = audit.get("step3_detail_failed_count", 0)
    cache_tag = f"[缓存复用{det_cache}]" if det_cache > 0 else ""
    det_stat_str = (
        f"拉取详情{det_ok}款{cache_tag}"
        if det_fail == 0
        else f"拉取详情{det_ok}款{cache_tag}(失败{det_fail}款)"
    )

    border_eq = "=" * 140
    border_dash = "-" * 140
    header_line = (
        f"🏁 【单品比价结论 #{idx}/{total}】[{src_lbl}] "
        f"分类: 【{record['category_name']}】(库内{cand.get('category_item_count', '-')}款)  |  "
        f"结论: {badge}  |  性价比比值(我方/闲鱼): {ratio_str}  |  "
        f"闲鱼搜索: \"{audit.get('search_keyword', '')}\"(初搜{audit.get('raw_search_count', 0)}->"
        f"AI初筛同款{len(audit.get('step2_comparable_item_ids') or [])}->{det_stat_str})"
    )
    col_header = (
        f"{_pad_col('对比角色', 10)} | {_pad_col('平台与商品编号', 19)} | {_pad_col('含邮总价', 10)} | "
        f"{_pad_col('提取规格数量', 18)} | {_pad_col('单位性价比 (每1元可得)', 26)} | 核心规格与商品标题描述"
    )
    my_row = (
        f"{_pad_col(f'[{src_lbl}]', 10)} | {_pad_col(my_plat_id, 19)} | {_pad_col(my_price, 10)} | "
        f"{_pad_col(my_qty, 18)} | {_pad_col(my_cp, 26)} | {my_desc}"
    )
    xy_row = (
        f"{_pad_col('[闲鱼最优]', 10)} | {_pad_col(xy_plat_id, 19)} | {_pad_col(xy_price, 10)} | "
        f"{_pad_col(xy_qty, 18)} | {_pad_col(xy_cp, 26)} | {xy_desc}"
    )

    lines = ["", border_eq, header_line, border_dash, col_header, border_dash, my_row, xy_row, border_dash]
    if src_type == SOURCE_TYPE_DEAL_TIP and cand.get("deal_price_logic"):
        lines.append(f"💡 线报优惠方案 : {cand['deal_price_logic']}")
    lines.extend([
        f"⚖️  胜负关键点 : {quant_explain}",
        f"📝 AI详细理由 : {cr.get('llm_reason') or '-'}",
        border_eq,
    ])
    return "\n".join(lines)


def _format_batch_summary_report(batch_id: str, elapsed: float, records: List[Dict[str, Any]]) -> str:
    """整轮任务结束时输出简洁清晰的全局胜负一表通览（不重复打印逐商品卡片）。"""
    def _count_group(sub_recs: List[Dict[str, Any]]) -> Tuple[int, int, int, int, int]:
        w = sum(1 for r in sub_recs if r.get("final_winner") == "my_product")
        l = sum(1 for r in sub_recs if r.get("final_winner") == "xianyu_product")
        t = sum(1 for r in sub_recs if r.get("final_winner") == "tie")
        e = sum(1 for r in sub_recs if (r.get("xianyu_audit_trail") or {}).get("browser_aborted"))
        n = sum(
            1
            for r in sub_recs
            if r.get("final_winner") not in ("my_product", "xianyu_product", "tie")
            and not (r.get("xianyu_audit_trail") or {}).get("browser_aborted")
        )
        return w, l, t, n, e

    win_cnt, lose_cnt, tie_cnt, none_cnt, err_cnt = _count_group(records)
    off_recs = [r for r in records if _normalize_source_type(r.get("source_type")) == SOURCE_TYPE_OFFICIAL]
    deal_recs = [r for r in records if _normalize_source_type(r.get("source_type")) == SOURCE_TYPE_DEAL_TIP]
    ow, ol, ot, on, oe = _count_group(off_recs)
    dw, dl, dt_, dn, de = _count_group(deal_recs)

    search_err_cnt = sum(1 for r in records if not (r.get("xianyu_audit_trail") or {}).get("search_ok", True))
    risk_block_cnt = sum(1 for r in records if (r.get("xianyu_audit_trail") or {}).get("is_risk_blocked"))
    batch_abort_cnt = sum(1 for r in records if not (r.get("xianyu_audit_trail") or {}).get("batch_detail_ok", True))
    item_det_fail_cnt = sum(int((r.get("xianyu_audit_trail") or {}).get("step3_detail_failed_count") or 0) for r in records)
    item_det_ok_cnt = sum(int((r.get("xianyu_audit_trail") or {}).get("step3_detail_fetched_count") or 0) for r in records)
    item_det_cache_cnt = sum(int((r.get("xianyu_audit_trail") or {}).get("step3_detail_cache_hit_count") or 0) for r in records)
    item_det_live_cnt = sum(int((r.get("xianyu_audit_trail") or {}).get("step3_detail_live_fetched_count") or 0) for r in records)

    if search_err_cnt == 0 and risk_block_cnt == 0 and batch_abort_cnt == 0:
        health_status = (
            f"✅ 浏览器检索健康 (初搜失败=0 | 风控滑块=0 | 会话中断=0 | "
            f"详情成功={item_det_ok_cnt}个[7天缓存复用={item_det_cache_cnt}/H5新拉={item_det_live_cnt}], 单品未捕获={item_det_fail_cnt}个)"
        )
    else:
        health_status = (
            f"🚨 检测到浏览器检索异常！(初搜失败={search_err_cnt} | 风控滑块={risk_block_cnt} | "
            f"批量详情中断={batch_abort_cnt} | 详情成功={item_det_ok_cnt}个[缓存复用={item_det_cache_cnt}/H5新拉={item_det_live_cnt}], 失败={item_det_fail_cnt}个) "
            f"-> 请在日志中搜索 ❌ 或 🚨 排查现场诊断！"
        )

    eq_line = "=" * 144
    dash_line = "-" * 144
    lines = [
        "",
        eq_line,
        (
            f"📊 【闲鱼比价探测 · 本轮运维全局总结报告】 "
            f"批次: {batch_id}  |  总耗时: {elapsed:.1f}s  |  参评总计: {len(records)} 个  |  "
            f"🏆 我方胜出: {win_cnt}  |  🏳️ 闲鱼更优: {lose_cnt}  |  🤝 持平: {tie_cnt}  |  "
            f"⚪ 无可比: {none_cnt}  |  🚨 检索异常: {err_cnt}"
        ),
        (
            f"📌 分源独立统计: "
            f"【正式官价】参评 {len(off_recs)} (🏆胜{ow} / 🏳️负{ol} / 🤝平{ot} / ⚪无{on} / 🚨异{oe})   ||   "
            f"【线报爆料】参评 {len(deal_recs)} (🏆胜{dw} / 🏳️负{dl} / 🤝平{dt_} / ⚪无{dn} / 🚨异{de})"
        ),
        f"🌐 浏览器检索健康度: {health_status}",
        eq_line,
        (
            f"{_pad_col('序号', 4)} | {_pad_col('来源/分类(库内)', 18)} | {_pad_col('胜负结论', 12)} | "
            f"{_pad_col('比值(我/闲)', 14)} | {_pad_col('我方价格', 9)} | {_pad_col('闲鱼含邮价', 10)} | "
            f"{_pad_col('我方性价比(每1元)', 22)} | {_pad_col('闲鱼性价比(每1元)', 22)} | 胜负核心摘要"
        ),
        dash_line,
    ]

    for idx, r in enumerate(records, 1):
        cand = r.get("candidate_product") or {}
        bxy = r.get("best_xianyu_product") or {}
        cr = r.get("comparison_result") or {}
        badge, ratio_str, quant_explain = _build_verdict_summary(r)
        focus_u = cr.get("matched_unit") or cand.get("primary_unit")
        short_tag = "[报]" if _normalize_source_type(r.get("source_type")) == SOURCE_TYPE_DEAL_TIP else "[官]"

        cat_col = f"{short_tag}{r['category_name']}({cand.get('category_item_count', '-')})"
        my_p_col = f"¥{r['activity_price']:.2f}"
        xy_p_col = f"¥{_num(bxy.get('comparable_price'), 0.0):.2f}" if cr.get("has_comparable_item") else "-"
        my_cp_col = _fmt_cp_human(cr.get("my_unit_cost_performance") or {}, focus_u)
        xy_cp_col = _fmt_cp_human(cr.get("xianyu_unit_cost_performance") or {}, focus_u) if cr.get("has_comparable_item") else "-"

        lines.append(
            f"{_pad_col(f'#{idx}', 4)} | {_pad_col(cat_col, 18)} | {_pad_col(badge, 12)} | "
            f"{_pad_col(ratio_str, 14)} | {_pad_col(my_p_col, 9)} | {_pad_col(xy_p_col, 10)} | "
            f"{_pad_col(my_cp_col, 22)} | {_pad_col(xy_cp_col, 22)} | {quant_explain}"
        )

    lines.append(eq_line)
    return "\n".join(lines)


def _ensure_eval_indexes(eval_col: Any) -> None:
    """确保比价结果表采用 (platform, product_id, source_type) 复合唯一索引及常用查询排序索引。"""
    eval_col.create_index(
        [("platform", 1), ("product_id", 1), ("source_type", 1)],
        unique=True,
        background=True,
    )
    eval_col.create_index(
        [("source_type", 1), ("is_my_product_better", -1), ("cost_performance_ratio", -1), ("evaluated_at", -1)],
        background=True,
    )
    eval_col.create_index(
        [("is_my_product_better", -1), ("cost_performance_ratio", -1), ("evaluated_at", -1)],
        background=True,
    )


def _ensure_xianyu_items_indexes(items_col: Any) -> None:
    """确保闲鱼商品表 xianyu_items 采用 item_id 唯一索引及 7 天详情有效期查询索引。"""
    if items_col is None:
        return
    items_col.create_index([("item_id", 1)], unique=True, background=True)
    items_col.create_index([("has_detail", 1), ("detail_updated_at", -1)], background=True)
    items_col.create_index([("updated_at", -1)], background=True)


# ==============================================================================
# 主流程串行稳定编排与运行入口
# ==============================================================================
def run_discovery_cycle(
    hours_window: int = SCAN_HOURS_WINDOW,
    deal_hours_window: int = DEAL_SCAN_HOURS_WINDOW,
    enable_official: bool = ENABLE_OFFICIAL,
    enable_deal_tip: bool = ENABLE_DEAL_TIP,
    max_categories: int = MAX_CATEGORIES_PER_ROUND,
    max_search_items: int = MAX_XIANYU_SEARCH_ITEMS,
    max_detail_checks: int = MAX_DETAIL_CHECKS_PER_CATEGORY,
    detail_cache_days: int = XIANYU_DETAIL_CACHE_DAYS,
    search_user_data_dir: str = SEARCH_USER_DATA_DIR,
    guest_user_data_dir: str = GUEST_USER_DATA_DIR,
    debug_browser: bool = DEBUG_BROWSER,
    stage1_workers: int = STAGE1_WORKERS,
    enable_pre_round_probe: bool = ENABLE_PRE_ROUND_PROBE,
    probe_keyword: str = PRE_ROUND_PROBE_KEYWORD,
    max_consecutive_browser_fails: int = MAX_CONSECUTIVE_BROWSER_FAILS,
) -> List[Dict[str, Any]]:
    global _LAST_CYCLE_ABORTED_BY_RISK
    _LAST_CYCLE_ABORTED_BY_RISK = False

    if not enable_official and not enable_deal_tip:
        logger.warning("⚠️ [未启用任何商品源] ENABLE_OFFICIAL 与 ENABLE_DEAL_TIP 均为 False，本轮直接退出。")
        return []

    t0 = time.monotonic()
    batch_id = datetime.datetime.now().strftime("disc_%Y%m%d_%H%M%S")
    s1_workers = max(1, int(stage1_workers or STAGE1_WORKERS))
    max_fails = max(1, int(max_consecutive_browser_fails or 2))
    detail_cap_desc = f"前{max_detail_checks}款" if max_detail_checks > 0 else "全部"
    browser_mode_desc = "无头静默(headless)" if not debug_browser else "有头窗口(headed)"
    enabled_desc_parts = []
    if enable_official:
        enabled_desc_parts.append(f"正式官价({hours_window}h)")
    if enable_deal_tip:
        enabled_desc_parts.append(f"线报爆料({deal_hours_window}h)")
    enabled_desc = " + ".join(enabled_desc_parts)

    logger.info(
        f"\n{'=' * 96}\n"
        f"🚀 [闲鱼高性价比商品探测与比价任务/启动] 批次={batch_id} | 启用商品源=[{enabled_desc}] | "
        f"单源探测分类数={max_categories if max_categories > 0 else '全部'} | "
        f"执行模式=(阶段1选品={s1_workers}线程, 阶段2/3比价=顺序串行, 同款详情上限={detail_cap_desc}, 详情缓存={detail_cache_days}天) | "
        f"浏览器模式={browser_mode_desc} | 搜索小号目录=<{search_user_data_dir}> | H5免登录详情目录=<{guest_user_data_dir}>\n"
        f"{'=' * 96}"
    )

    probe_res: Dict[str, Any] = {}
    if enable_pre_round_probe:
        probe_ok, probe_res = probe_xianyu_search_ready(
            search_user_data_dir=search_user_data_dir,
            debug_browser=debug_browser,
            probe_keyword=probe_keyword,
        )
        if not probe_ok:
            _LAST_CYCLE_ABORTED_BY_RISK = True
            if probe_res.get("error_code") == "ACCOUNT_OFFLINE":
                logger.error(
                    f"\n{'!' * 96}\n"
                    f"🔑 [搜索小号未扫码登录/已掉线] 搜索目录 <{search_user_data_dir}> 当前无有效闲鱼登录态！\n"
                    f"   ├── 诊断详情 : {probe_res.get('message')}\n"
                    f"   └── 处理办法 : 直接运行 mobile_web/scripts/publish_to_xianyu.py 扫码登录闲鱼小号，关闭窗口保存后再运行！\n"
                    f"{'!' * 96}"
                )
            else:
                logger.error(
                    f"\n{'!' * 96}\n"
                    f"🛑 [开轮探针未通过/静默冷却保护] 闲鱼搜索通道当前不可用（错误码={probe_res.get('error_code')} | "
                    f"原因={probe_res.get('message')}）\n"
                    f"   └── 本轮已立即终止，跳过 AI 选品与全部后续闲鱼请求，进入静默冷却窗口！\n"
                    f"{'!' * 96}"
                )
            return []

    db = gen_db_object()
    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    _ensure_eval_indexes(eval_col)
    items_col = db.get_collection(XIANYU_ITEMS_COLLECTION_NAME)
    _ensure_xianyu_items_indexes(items_col)
    if probe_res.get("ok") and probe_res.get("items"):
        _save_xianyu_search_items(items_col, probe_res.get("items") or [], keyword=probe_keyword)

    champions: List[Dict[str, Any]] = []
    if enable_official:
        off_champions = select_verified_category_champions(
            db=db,
            hours_window=hours_window,
            max_categories=max_categories,
            stage1_workers=s1_workers,
            source_type=SOURCE_TYPE_OFFICIAL,
        )
        if off_champions:
            logger.info(_format_champions_summary_table(off_champions, source_type=SOURCE_TYPE_OFFICIAL))
            champions.extend(off_champions)

    if enable_deal_tip:
        deal_champions = select_verified_category_champions(
            db=db,
            hours_window=deal_hours_window,
            max_categories=max_categories,
            stage1_workers=s1_workers,
            source_type=SOURCE_TYPE_DEAL_TIP,
        )
        if deal_champions:
            logger.info(_format_champions_summary_table(deal_champions, source_type=SOURCE_TYPE_DEAL_TIP))
            champions.extend(deal_champions)

    if not champions:
        logger.warning("⚠️ [本轮结束] 启用的商品源均未选出满足条件的可比标品候选商品。")
        return []

    total_champions = len(champions)
    records: List[Dict[str, Any]] = []
    consecutive_fails = 0

    for idx, cand in enumerate(champions, 1):
        src_lbl = _source_label(cand.get("source_type"))
        logger.info(
            f"\n>>> ▶️ [开始比价第 {idx}/{total_champions} 个分类标品冠军][{src_lbl}] "
            f"分类=【{cand['category_name']}】(库内{cand.get('category_item_count', '-')}款) | "
            f"我方标品=【{cand['name'][:40]}】(¥{cand['activity_price']:.2f})"
        )
        rec = compare_candidate_with_xianyu(
            candidate=cand,
            search_user_data_dir=search_user_data_dir,
            guest_user_data_dir=guest_user_data_dir,
            max_search_items=max_search_items,
            max_detail_checks=max_detail_checks,
            debug_browser=debug_browser,
            items_col=items_col,
            detail_cache_days=detail_cache_days,
        )
        rec["batch_id"] = batch_id
        rec_src_type = _normalize_source_type(rec.get("source_type"))
        rec["source_type"] = rec_src_type
        records.append(rec)

        audit = rec.get("xianyu_audit_trail") or {}
        is_hard_stop = bool(audit.get("is_risk_blocked") or audit.get("is_account_offline"))
        if audit.get("browser_aborted"):
            consecutive_fails += 1
            logger.warning(
                f"⚠️ [跳过入库][{src_lbl}] 分类=【{rec['category_name']}】 | 商品=[{rec['platform'].upper()}:{rec['product_id']}] "
                f"因闲鱼浏览器检索异常中断，暂不写入比价记录库，保留后续轮次重试机会"
            )
            logger.info(_format_single_comparison_card(idx, total_champions, rec))

            if is_hard_stop or consecutive_fails >= max_fails:
                _LAST_CYCLE_ABORTED_BY_RISK = True
                remaining = total_champions - idx
                logger.error(
                    f"\n{'!' * 96}\n"
                    f"🛑 [整轮熔断保护已触发] 分类=【{rec['category_name']}】检测到风控/掉线或连续失败({consecutive_fails}次)！\n"
                    f"   └── 已立即停止本轮剩余 {remaining}/{total_champions} 个分类标品的闲鱼检索，进入静默冷却保护！\n"
                    f"{'!' * 96}"
                )
                break
        else:
            consecutive_fails = 0
            eval_col.update_one(
                {"platform": rec["platform"], "product_id": rec["product_id"], "source_type": rec_src_type},
                {"$set": rec},
                upsert=True,
            )
            logger.info(_format_single_comparison_card(idx, total_champions, rec))
            if is_hard_stop:
                _LAST_CYCLE_ABORTED_BY_RISK = True
                remaining = total_champions - idx
                logger.error(
                    f"\n{'!' * 96}\n"
                    f"🛑 [详情风控熔断保护已触发] 分类=【{rec['category_name']}】部分详情拉取期间检测到闲鱼风控拦截！\n"
                    f"   └── 当前已比价结果已保存，立即停止本轮剩余 {remaining}/{total_champions} 个分类检索，进入静默冷却保护！\n"
                    f"{'!' * 96}"
                )
                break

    elapsed = time.monotonic() - t0
    if records:
        logger.info(_format_batch_summary_report(batch_id, elapsed, records))
    return records


def main() -> None:
    while True:
        try:
            run_discovery_cycle()
        except KeyboardInterrupt:
            logger.info("👋 收到中断信号，任务退出。")
            break
        except Exception as exc:
            logger.exception(f"❌ [探测轮次异常] {exc}")
        if not ENABLE_LOOP:
            break
        if _LAST_CYCLE_ABORTED_BY_RISK:
            sleep_hours = max(float(LOOP_INTERVAL_HOURS or 1.0), float(RISK_COOLDOWN_HOURS or 2.0))
            logger.warning(
                f"🛑 本轮触发闲鱼限流/掉线保护，进入静默冷却：等待 {sleep_hours} 小时后再执行 1 次轻量探测（期间绝不发起重试）..."
            )
        else:
            sleep_hours = float(LOOP_INTERVAL_HOURS or 1.0)
            logger.info(f"⏳ 本轮执行完毕，等待 {sleep_hours} 小时后执行下一轮...")
        time.sleep(max(60.0, sleep_hours * 3600.0))


if __name__ == "__main__":
    main()
