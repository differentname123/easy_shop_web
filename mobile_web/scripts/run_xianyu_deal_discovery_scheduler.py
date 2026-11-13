# -*- coding: utf-8 -*-
r"""
闲鱼高性价比商品探测与比价定时任务（极简稳定版）
================================================================================
核心流程：
  1. 【候选商品选拔（双源各最多 20 个，按类别已比价次数升序轮转，1:1 交替）】：
     - `official`（正式官方商品）：72h 时效窗口内、尚未比价过，并通过官方接口实时校验价格未失效暴涨；
     - `deal_tip`（线报爆料商品）：48h 时效窗口内、尚未比价过且清洗价格一致；
     - 按每个类别历史出现比价的次数升序排列，防止单一类别被重复比价，每源各取最多 20 个候选商品并 1:1 交替合并。
  2. 【逐商品一气呵成串行闭环（不做前置探测搜索，不搞分阶段批量预跑）】：
     - 第 1 次大模型调用：判断当前商品是否有资格进行闲鱼比价，若有资格则提取主/备搜索关键字；无资格直接记录跳过；
     - 闲鱼小号搜索：检索概览商品并按 `item_id` 保存/更新至 `xianyu_items`（绝不覆盖已有详情）；
     - 第 2 次大模型调用：从搜索概览中选出真正具备可比性的同款商品并按预估含邮性价比排序；
     - 仅保留前 2 个商品补充详情：若 `xianyu_items` 中已有 7 天内完整详情则直接复用，缺失才走无痕 H5 拉取并入库；
     - 第 3 次大模型调用：独立校准我方真实规格并与闲鱼完整详情终审比价，直接以大模型最终给出的比例与胜负为准保存入库。
================================================================================
"""

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
# 运行控制变量
# ==============================================================================
ENABLE_LOOP = True                                      # 是否持续循环运行（True=循环运行，False=单轮退出）
LOOP_INTERVAL_HOURS = 1.0                               # 正常每轮结束后的等待间隔小时数
RISK_COOLDOWN_HOURS = 2.0                               # 触发闲鱼限流/掉线熔断后的静默冷却小时数
MAX_CONSECUTIVE_BROWSER_FAILS = 2                       # 单轮连续多少次浏览器检索失败即触发整轮熔断
ENABLE_OFFICIAL = True                                  # 是否开启【正式官方商品】比价
ENABLE_DEAL_TIP = True                                  # 是否开启【线报爆料商品】比价
SCAN_HOURS_WINDOW = 72                                  # 正式官方商品时效性窗口（小时）
DEAL_SCAN_HOURS_WINDOW = 48                             # 线报爆料商品时效性窗口（小时）
MAX_CATEGORIES_PER_ROUND = 20                           # 每个数据源每轮选取的商品类别上限（官方20 + 线报20）
MAX_XIANYU_SEARCH_ITEMS = 20                            # 每次闲鱼搜索抓取的概览商品数
MAX_DETAIL_CHECKS_PER_CATEGORY = 2                      # 每个商品最多保留前 2 个可比闲鱼商品补充详情
XIANYU_DETAIL_CACHE_DAYS = 7                            # 闲鱼商品详情数据库缓存有效天数（7天内免重复拉取）
DEBUG_BROWSER = False                                   # 是否显示浏览器窗口（False=后台无头静默）
SEARCH_USER_DATA_DIR = DEFAULT_SEARCH_USER_DATA_DIR     # 【搜索小号目录】仅用于步骤2关键字搜索（W:\temp\xianyu_xiaohao）
GUEST_USER_DATA_DIR = DEFAULT_GUEST_USER_DATA_DIR       # 【H5详情串行锁标识】默认纯内存无痕H5会话（W:\temp\xianyu_guest）

LLM_MAX_ATTEMPTS = 3
LLM_CALL_TIMEOUT_SEC = 90
LLM_MAX_RETRIES_PER_MODEL = 1
OFFICIAL_PRICE_VERIFY_ATTEMPTS_PER_CAT = 3              # 同一正式分类若首款官价失效，最多顺延核验几款

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
    """将底层 publish_to_xianyu 的 WARNING/ERROR 日志同步写入主任务日志文件。"""

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
# 统一大模型 JSON 调用与强校验重试工具 + 终端排版辅助
# ==============================================================================
def _call_llm_json(
    base_prompt: str,
    validator_fn: Callable[[Any], Tuple[bool, str, Dict[str, Any]]],
    step_label: str,
    id_hint_msg: str,
) -> Tuple[bool, Dict[str, Any], Dict[str, Any], str]:
    """统一封装大模型调用、JSON 解析与最多 LLM_MAX_ATTEMPTS 次校验修正重试。"""
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
# 候选商品选拔（客观时效过滤 + 类别比价次数轮转排序 + 官方实时验价 + 1:1交替）
# ==============================================================================
def _extract_primary_sku_and_category(doc: Dict[str, Any], activity_price: float) -> Optional[Tuple[str, Dict[str, Any]]]:
    """从商品 `format_info` 中提取主 SKU，并取 `core_entities` 最高分 `name` 作为分类名。"""
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
    """从预清洗 `pricing_basis` 提取初步单位数量供类内初排参考（最终规格与比例由第3步大模型校准决定）。"""
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
            accum *= v if v > 0 else 1.0
            if u:
                extracted[u] = round(accum, 4)

    if raw_base_unit:
        extracted[raw_base_unit] = round(total_val if total_val > 0 else accum, 4)
    if not extracted:
        extracted["件"] = 1.0
    return raw_base_unit, extracted


def _compute_unit_cp(unit_quantities: Dict[str, float], activity_price: float) -> Dict[str, float]:
    if activity_price <= 0:
        return {}
    return {
        u: round(float(qty) / activity_price, 6)
        for u, qty in unit_quantities.items()
        if float(qty) > 0
    }


def _resolve_candidate_brand(doc: Dict[str, Any], sku_fmt: Dict[str, Any]) -> str:
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


def _parse_deal_time_utc(val: Any, default_tz: datetime.timezone = _CN_TZ) -> Optional[datetime.datetime]:
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        return val.astimezone(datetime.timezone.utc) if val.tzinfo else val.replace(tzinfo=datetime.timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=default_tz).astimezone(datetime.timezone.utc)
        except Exception:
            continue
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _build_deal_tip_virtual_doc(doc: Dict[str, Any], cutoff_utc: datetime.datetime) -> Optional[Dict[str, Any]]:
    """校验线报商品时效性与清洗状态，组装统一候选视图文档。"""
    di = doc.get("deal_info")
    if not isinstance(di, dict) or not di.get("is_deal_tip"):
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

    outer_name = str(doc.get("name") or "").strip()
    eff_title = str(di.get("clean_title") or outer_name).strip()
    if not eff_title:
        return None

    off_ref_price = _num(doc.get("activity_price") or doc.get("price"), 0.0)
    clean_fmt = {**deal_fmt, "price": round(deal_price, 2)}
    return {
        **doc,
        "source_type": SOURCE_TYPE_DEAL_TIP,
        "outer_name": outer_name,
        "name": eff_title,
        "official_reference_price": round(off_ref_price, 2) if off_ref_price > 0 else 0.0,
        "activity_price": round(deal_price, 2),
        "price": round(deal_price, 2),
        "format_info": {"deal": clean_fmt},
        "format_status": "success",
        "deal_price_logic": str(di.get("price_logic") or "").strip(),
        "deal_unit_price_text": str(di.get("unit_deal_price") or "").strip(),
        "deal_quality_score": di.get("quality_score"),
        "deal_publish_time": str(di.get("publish_time") or "").strip(),
    }


def _verify_live_activity_price(platform: str, product_id: str, goods_sign: str, fallback_price: float) -> Optional[float]:
    """调用官方 Adapter 验证正式商品实时价格（下架淘汰返回 None，网络波动沿用入库价）。"""
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
        logger.warning(f"⚠️ [官方验价异常] [{platform.upper()}:{product_id}] 沿用入库价=¥{fallback_price} | 异常={exc}")
    return round(fallback_price, 2) if fallback_price > 0 else None


def _rank_category_candidates(
    raw_items: List[Tuple[Dict[str, Any], Dict[str, Any], float]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """同度量衡换算对齐后，按初步单位性价比选出类内候选顺序。"""
    dim_counts: Dict[str, Dict[str, int]] = {}
    parsed_entries = []
    for doc, sku_fmt, act_price in raw_items:
        _, extracted_units = _extract_raw_units(sku_fmt)
        parsed_entries.append((doc, sku_fmt, act_price, extracted_units))
        for u in extracted_units:
            dim = UNIT_TO_CATEGORY.get(u)
            if dim:
                dim_counts.setdefault(dim, {})
                dim_counts[dim][u] = dim_counts[dim].get(u, 0) + 1

    dim_target_unit = {dim: max(cnts.items(), key=lambda x: x[1])[0] for dim, cnts in dim_counts.items()}
    cat_candidates: List[Dict[str, Any]] = []
    unit_groups: Dict[str, List[Tuple[float, int]]] = defaultdict(list)

    for idx, (doc, sku_fmt, act_price, extracted_units) in enumerate(parsed_entries):
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
        cat_candidates.append({
            "doc": doc,
            "sku_fmt": sku_fmt,
            "db_activity_price": round(act_price, 2),
            "unit_quantities": unit_quantities,
            "unit_cost_performance": unit_cp,
        })
        for u, cp_val in unit_cp.items():
            unit_groups[u].append((cp_val, idx))

    if not cat_candidates:
        return "件", []

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
    return top_unit, ranked


def _pick_verified_candidate_from_category(
    cat_name: str,
    raw_items: List[Tuple[Dict[str, Any], Dict[str, Any], float]],
    source_type: str = SOURCE_TYPE_OFFICIAL,
) -> Optional[Dict[str, Any]]:
    """从单个类别选出 1 个通过官方价格校验（正式源）或时效校验（线报源）的待比价候选商品。"""
    src_type = _normalize_source_type(source_type)
    top_unit, ranked = _rank_category_candidates(raw_items)
    if not ranked:
        return None

    chosen: Optional[Dict[str, Any]] = None
    max_try = 1 if src_type == SOURCE_TYPE_DEAL_TIP else min(len(ranked), OFFICIAL_PRICE_VERIFY_ATTEMPTS_PER_CAT)
    for cand in ranked[:max_try]:
        doc = cand["doc"]
        if src_type == SOURCE_TYPE_DEAL_TIP:
            cand["verified_activity_price"] = cand["db_activity_price"]
            chosen = cand
            break

        plat = normalize_platform(doc.get("platform") or "pdd")
        pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        gsign = str(doc.get("goods_sign") or "").strip()
        live_price = _verify_live_activity_price(plat, pid, gsign, cand["db_activity_price"])
        if live_price is None or live_price <= 0:
            continue
        if live_price > cand["db_activity_price"] * 1.30:
            logger.warning(
                f"🚫 [官方验价优惠失效] [{plat.upper()}:{pid}] 实时价¥{live_price} 较入库价¥{cand['db_activity_price']} 涨幅超30%，优惠已失效，跳过该商品"
            )
            continue
        cand["verified_activity_price"] = live_price
        cand["unit_cost_performance"] = _compute_unit_cp(cand["unit_quantities"], live_price)
        chosen = cand
        break

    if chosen is None:
        return None

    w_doc, w_sku = chosen["doc"], chosen["sku_fmt"]
    best_primary_unit = top_unit
    if best_primary_unit not in chosen["unit_cost_performance"] or _unit_priority_score(best_primary_unit) == 0:
        sorted_cand_units = _ordered_unit_keys(chosen["unit_cost_performance"])
        if sorted_cand_units:
            best_primary_unit = sorted_cand_units[0]

    kws_list = [
        f"{k.get('attribute_name')}:{k.get('attribute_value')}"
        for k in (w_sku.get("decision_keywords") or [])
        if isinstance(k, dict) and k.get("attribute_value")
    ]
    orig_p = _num(w_doc.get("official_reference_price") or w_doc.get("original_price"), 0.0)
    return {
        "source_type": src_type,
        "platform": normalize_platform(w_doc.get("platform") or "pdd"),
        "product_id": str(w_doc.get("product_id") or w_doc.get("goods_id") or "").strip(),
        "goods_sign": str(w_doc.get("goods_sign") or "").strip(),
        "channel_type": w_doc.get("channel_type"),
        "category_name": cat_name,
        "category_item_count": len(raw_items),
        "name": str(w_doc.get("name") or "").strip(),
        "outer_name": str(w_doc.get("outer_name") or "").strip(),
        "brand": _resolve_candidate_brand(w_doc, w_sku),
        "db_activity_price": chosen["db_activity_price"],
        "activity_price": chosen["verified_activity_price"],
        "original_price": round(orig_p, 2) if orig_p > 0 else None,
        "primary_unit": best_primary_unit,
        "unit_quantities": chosen["unit_quantities"],
        "unit_cost_performance": chosen["unit_cost_performance"],
        "decision_keywords_summary": " | ".join(kws_list),
        "pricing_basis": w_sku.get("pricing_basis") or {},
        "search_keyword": "",
        "backup_search_keyword": "",
        "selection_reason": "",
        "llm_keyword_output": {},
        "product_url": str(w_doc.get("product_url") or ""),
        "image_url": str(w_doc.get("image_url") or ""),
        "updated_at": str(w_doc.get("updated_at") or ""),
        "deal_price_logic": str(w_doc.get("deal_price_logic") or ""),
        "deal_unit_price_text": str(w_doc.get("deal_unit_price_text") or ""),
        "deal_quality_score": w_doc.get("deal_quality_score"),
        "deal_publish_time": str(w_doc.get("deal_publish_time") or ""),
    }


def select_round_candidates_for_source(
    db: Any,
    hours_window: int = 72,
    max_categories: int = 20,
    source_type: str = SOURCE_TYPE_OFFICIAL,
) -> List[Dict[str, Any]]:
    """
    按类别已出现比价的次数升序排列，经时效过滤与官方价格变化过滤后，选出本轮该数据源最多 max_categories 个待比价候选商品。
    """
    src_type = _normalize_source_type(source_type)
    src_label = _source_label(src_type)
    repo = ProductRepository()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc - datetime.timedelta(hours=hours_window)
    cutoff_dt = cutoff_utc.replace(tzinfo=None)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    all_eval_docs = list(
        eval_col.find({}, {"platform": 1, "product_id": 1, "category_name": 1, "source_type": 1})
    )
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

    global_cat_eval_counts: Dict[str, int] = defaultdict(int)
    for d in all_eval_docs:
        cname = str(d.get("category_name") or "").strip()
        if cname:
            global_cat_eval_counts[cname] += 1

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
            "deal_info.unit_deal_price": 1,
            "deal_info.format_status": 1,
            "deal_info.need_reformat": 1,
            "deal_info.format_info": 1,
            "deal_info.price_logic": 1,
            "deal_info.quality_score": 1,
            "deal_info.publish_time": 1,
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
        if not cat_and_sku:
            continue
        by_cat[cat_and_sku[0]].append((doc, cat_and_sku[1], act_price))

    # 严格按每个类别历史出现比价的次数升序排序（防止某一类一直被重复比价）
    sorted_cats = sorted(
        by_cat.items(),
        key=lambda kv: (cat_eval_counts.get(kv[0], 0), global_cat_eval_counts.get(kv[0], 0), -len(kv[1])),
    )
    top_preview = ", ".join(f"{k}({len(v)}款/已比{cat_eval_counts.get(k, 0)}次)" for k, v in sorted_cats[:12])
    target_desc = "全部" if max_categories <= 0 else str(max_categories)

    candidates: List[Dict[str, Any]] = []
    for cat_name, raw_items in sorted_cats:
        if max_categories > 0 and len(candidates) >= max_categories:
            break
        picked = _pick_verified_candidate_from_category(cat_name, raw_items, source_type=src_type)
        if picked is not None:
            candidates.append(picked)

    logger.info(
        f"📊 [候选商品选拔][{src_label}/{hours_window}h] 时效内商品={len(raw_docs)} | 已比价跳过={skipped_evaluated_cnt} | "
        f"可选类别={len(sorted_cats)} -> 本轮入选={len(candidates)}/{target_desc} | 轮转优先类别=[{top_preview}]"
    )
    return candidates


def interleave_candidates_one_to_one(
    official_candidates: List[Dict[str, Any]],
    deal_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """将正式官方候选与线报候选按 1:1 交替编排。"""
    out: List[Dict[str, Any]] = []
    max_len = max(len(official_candidates), len(deal_candidates))
    for i in range(max_len):
        if i < len(official_candidates):
            out.append(official_candidates[i])
        if i < len(deal_candidates):
            out.append(deal_candidates[i])
    return out


# ==============================================================================
# 大模型第 1 步（单商品比价资格判断 + 搜索关键字生成）
# ==============================================================================
def _validate_step1_eligibility_response(parsed: Any) -> Tuple[bool, str, Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return False, f"返回结构不是JSON对象(dict)，实际为 {type(parsed).__name__}", {}

    is_elig = parsed.get("is_eligible")
    if not isinstance(is_elig, bool):
        return False, "缺少布尔字段 is_eligible", {}

    main_kw = str(parsed.get("search_keyword") or "").strip()
    backup_kw = str(parsed.get("backup_search_keyword") or "").strip()
    reason = str(parsed.get("reason") or "").strip()

    if is_elig and not main_kw:
        return False, "is_eligible=true 时 search_keyword 不能为空", {}
    if not is_elig:
        main_kw, backup_kw = "", ""
    elif not backup_kw:
        backup_kw = main_kw

    return True, "ok", {
        "is_eligible": is_elig,
        "search_keyword": main_kw,
        "backup_search_keyword": backup_kw,
        "reason": reason,
    }


def evaluate_candidate_eligibility_and_keywords(
    candidate: Dict[str, Any],
) -> Tuple[bool, str, str, str, Dict[str, Any]]:
    """【大模型第 1 次调用】：判断当前单个候选商品是否有资格进行闲鱼比价，并生成主/备搜索关键字。"""
    prompt_template = read_file_to_str(KEYWORD_PROMPT_PATH)
    payload: Dict[str, Any] = {
        "category_name": candidate.get("category_name"),
        "source_type": candidate.get("source_type"),
        "platform": candidate.get("platform"),
        "product_id": candidate.get("product_id"),
        "name": candidate.get("name"),
        "brand": candidate.get("brand"),
        "activity_price": candidate.get("activity_price"),
        "decision_keywords": candidate.get("decision_keywords_summary"),
        "pricing_basis": candidate.get("pricing_basis") or {},
    }
    if candidate.get("outer_name") and candidate.get("outer_name") != candidate.get("name"):
        payload["outer_name"] = candidate.get("outer_name")
    if candidate.get("original_price"):
        payload["original_price"] = candidate.get("original_price")
    if candidate.get("deal_price_logic"):
        payload["deal_price_logic"] = candidate.get("deal_price_logic")
    if candidate.get("deal_unit_price_text"):
        payload["unit_deal_price"] = candidate.get("deal_unit_price_text")

    base_prompt = (
        f"{prompt_template}\n"
        f"<candidate_product_input>\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</candidate_product_input>"
    )
    ok, clean_dict, meta, last_err = _call_llm_json(
        base_prompt=base_prompt,
        validator_fn=_validate_step1_eligibility_response,
        step_label=f"步骤1/AI资格审查({candidate.get('category_name')})",
        id_hint_msg="请严格按要求输出包含布尔字段 `is_eligible`、`search_keyword`、`backup_search_keyword`、`reason` 的合法 JSON 对象！",
    )
    if not ok:
        fail_reason = f"AI资格审查校验失败: {last_err}"
        meta["parsed_json"] = {"is_eligible": False, "reason": fail_reason}
        return False, "", "", fail_reason, meta

    is_elig = bool(clean_dict["is_eligible"])
    main_kw = clean_dict["search_keyword"]
    backup_kw = clean_dict["backup_search_keyword"]
    reason = clean_dict["reason"]
    if is_elig:
        logger.info(
            f"🎯 [步骤1/AI资格通过] 分类=【{candidate.get('category_name')}】 | [{str(candidate.get('platform')).upper()}:{candidate.get('product_id')}] | "
            f"价格=¥{candidate.get('activity_price')} | 搜索词=【{main_kw}】(备用=【{backup_kw}】) | 模型={meta.get('model_used')} | 理由={reason}"
        )
    else:
        logger.info(
            f"⏭️ [步骤1/AI判定无资格跳过] 分类=【{candidate.get('category_name')}】 | [{str(candidate.get('platform')).upper()}:{candidate.get('product_id')}] | "
            f"模型={meta.get('model_used')} | 原因={reason} | 标题={str(candidate.get('name') or '')[:36]}"
        )
    return is_elig, main_kw, backup_kw, reason, meta


# ==============================================================================
# 大模型第 2 步（搜索概览可比性初筛） & 第 3 步（详情终审与最终性价比比例判定）
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
    """【大模型第 2 次调用】：筛选真正具有可比性的闲鱼商品 ID 列表并按预估性价比排序。"""
    prompt_template = read_file_to_str(COARSE_FILTER_PROMPT_PATH)
    payload = {
        "current_date": datetime.datetime.now(_CN_TZ).strftime("%Y-%m-%d"),
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
        f"耗时={llm_meta.get('elapsed_seconds', 0):.1f}s | 初筛可比数={len(comp_ids)}/{len(coarse_items_for_llm)} | "
        f"IDs={comp_ids} | 理由={filter_reason}"
    )
    return comp_ids, llm_meta, filter_reason


def _validate_llm_comparison_response(
    parsed: Any,
    valid_xianyu_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
    """校验第 3 次大模型输出的最终性价比比例与胜负结果。"""
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
            return False, "has_comparable_item=true 时 llm_winner 不能为 no_comparable", {}
        best_id = str(parsed.get("best_xianyu_item_id") or "").strip()
        if not best_id:
            return False, "has_comparable_item=true 时 best_xianyu_item_id 不能为空", {}
        if best_id not in valid_xianyu_ids:
            return False, f"best_xianyu_item_id='{best_id}' 不在已提供详情的闲鱼ID集合 {sorted(valid_xianyu_ids)} 中", {}
        best_price = _num(parsed.get("best_xianyu_price"), 0.0)
        if best_price <= 0:
            return False, f"best_xianyu_price={parsed.get('best_xianyu_price')} 必须大于 0", {}

        matched_u = str(parsed.get("matched_unit") or "").strip()
        my_qty = _num(parsed.get("my_unit_quantity"), 0.0)
        my_cp = _num(parsed.get("my_unit_cost_performance"), 0.0)
        xy_qty = _num(parsed.get("xianyu_unit_quantity"), 0.0)
        xy_cp = _num(parsed.get("xianyu_unit_cost_performance"), 0.0)
        cp_ratio = _num(parsed.get("cost_performance_ratio"), 0.0)

        if cp_ratio <= 0 and my_cp > 0 and xy_cp > 0:
            cp_ratio = round(my_cp / xy_cp, 4)

        if cp_ratio <= 0:
            return False, "has_comparable_item=true 时必须给出大于 0 的 cost_performance_ratio（以及 my_unit_cost_performance / xianyu_unit_cost_performance）", {}

        if my_cp > 0 and xy_cp > 0:
            expected_ratio = my_cp / xy_cp
            if abs(cp_ratio - expected_ratio) / expected_ratio > 0.15:
                return (
                    False,
                    f"cost_performance_ratio={cp_ratio} 与 my_unit_cost_performance/xianyu_unit_cost_performance={expected_ratio:.4f} 偏差过大，请重新核对算式",
                    {},
                )

        if llm_winner == "my_product" and cp_ratio < 1.0:
            return False, f"llm_winner='my_product' 与 cost_performance_ratio={cp_ratio} (<1.0) 矛盾", {}
        if llm_winner == "xianyu_product" and cp_ratio > 1.0:
            return False, f"llm_winner='xianyu_product' 与 cost_performance_ratio={cp_ratio} (>1.0) 矛盾", {}
        if llm_winner == "tie" and abs(cp_ratio - 1.0) > 0.05:
            return False, f"llm_winner='tie' 与 cost_performance_ratio={cp_ratio} 偏离 1.0 过大", {}

        out["matched_unit"] = matched_u
        out["my_unit_quantity"] = round(my_qty, 4) if my_qty > 0 else 0.0
        out["my_unit_cost_performance"] = round(my_cp, 6) if my_cp > 0 else 0.0
        out["xianyu_unit_quantity"] = round(xy_qty, 4) if xy_qty > 0 else 0.0
        out["xianyu_unit_cost_performance"] = round(xy_cp, 6) if xy_cp > 0 else 0.0
        out["cost_performance_ratio"] = round(cp_ratio, 4)
    else:
        out["llm_winner"] = "no_comparable"
        out["best_xianyu_item_id"] = ""
        out["comparable_item_ids"] = []
        out["matched_unit"] = ""
        out["cost_performance_ratio"] = None

    return True, "ok", out


# ==============================================================================
# 闲鱼商品表（xianyu_items）搜索概览保存与 7 天详情缓存复用
# ==============================================================================
def _save_xianyu_search_items(
    items_col: Optional[Any],
    raw_items: List[Dict[str, Any]],
) -> int:
    """将搜索返回的闲鱼商品概览按唯一标识 item_id 入库（绝不覆盖该商品已有的详情字段）。"""
    if items_col is None or not raw_items:
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_str = now_utc.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

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
        ops.append(
            UpdateOne(
                {"item_id": iid},
                {
                    "$set": {
                        "item_id": iid,
                        "item_url": item_url,
                        "search_card": clean_card,
                        "search_updated_at": now_utc,
                        "search_updated_at_str": now_str,
                        "updated_at": now_utc,
                        "updated_at_str": now_str,
                    },
                    "$setOnInsert": {
                        "created_at": now_utc,
                        "created_at_str": now_str,
                        "has_detail": False,
                        "title": clean_card["title"],
                        "price": clean_card["price"],
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
    """查询 xianyu_items 表复用有效期内（默认 7 天）已有的完整详情。"""
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
    """将成功拉取的闲鱼商品详情写入 xianyu_items 表，标记 detail_updated_at 供 7 天内复用。"""
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
        ops.append(
            UpdateOne(
                {"item_id": iid},
                {
                    "$set": {
                        "item_id": iid,
                        "item_url": item_url,
                        "title": clean_det["title"],
                        "price": clean_det["price"],
                        "transport_fee": clean_det["transport_fee"],
                        "is_free_shipping": clean_det["is_free_shipping"],
                        "has_multi_sku": clean_det["has_multi_sku"],
                        "has_detail": True,
                        "detail": clean_det,
                        "detail_updated_at": now_utc,
                        "detail_updated_at_str": now_str,
                        "updated_at": now_utc,
                        "updated_at_str": now_str,
                    },
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


# ==============================================================================
# 单商品完整闭环执行（步骤1资格与关键字 -> 搜索 -> 步骤2初筛 -> 前2详情 -> 步骤3终审）
# ==============================================================================
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
    """对单个候选商品一次性串行跑完：资格审查与搜词 -> 闲鱼搜索 -> 同款初筛 -> 前2款详情补齐 -> 大模型终审比价。"""
    cand = dict(candidate)
    cat_name = str(cand.get("category_name") or "").strip()

    # 第 1 次大模型调用：若尚未提取搜索关键字，先审查该商品是否有资格进行比价并生成关键字
    if not str(cand.get("search_keyword") or "").strip():
        is_elig, main_kw, backup_kw, elig_reason, step1_meta = evaluate_candidate_eligibility_and_keywords(cand)
        cand["search_keyword"] = main_kw
        cand["backup_search_keyword"] = backup_kw
        cand["selection_reason"] = elig_reason
        cand["llm_keyword_output"] = step1_meta
        if not is_elig:
            return _build_final_record(
                candidate=cand,
                used_keyword="",
                search_res={"ok": True},
                coarse_items_for_llm=[],
                step2_comparable_ids=[],
                step2_llm_meta={},
                xianyu_items_for_llm=[],
                llm_data={
                    "has_comparable_item": False,
                    "comparable_item_ids": [],
                    "best_xianyu_item_id": "",
                    "llm_winner": "ineligible",
                    "llm_reason": f"[不具备比价资格] {elig_reason}",
                },
                llm_call_meta={},
                browser_aborted=False,
                debug_browser=debug_browser,
                ineligible=True,
            )

    kw = str(cand["search_keyword"]).strip()
    backup_kw = str(cand.get("backup_search_keyword") or "").strip()

    my_candidate_for_llm: Dict[str, Any] = {
        "product_id": cand["product_id"],
        "platform": cand["platform"],
        "category_name": cat_name,
        "name": cand["name"],
        "brand": cand["brand"],
        "decision_keywords": cand.get("decision_keywords_summary") or "",
        "activity_price": cand["activity_price"],
        "pre_cleaned_reference": {
            "note": "预清洗规格仅供参考，可能存在解析误差，请以原始标题与价格说明核实的真实数量为准",
            "pricing_basis": cand.get("pricing_basis") or {},
            "unit_quantities": cand.get("unit_quantities") or {},
            "unit_cost_performance": cand.get("unit_cost_performance") or {},
        },
    }
    if cand.get("outer_name") and cand.get("outer_name") != cand.get("name"):
        my_candidate_for_llm["outer_name"] = cand["outer_name"]
    if cand.get("deal_price_logic"):
        my_candidate_for_llm["deal_price_logic"] = cand["deal_price_logic"]
    if cand.get("deal_unit_price_text"):
        my_candidate_for_llm["unit_deal_price"] = cand["deal_unit_price_text"]

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
            _save_xianyu_search_items(items_col, res.get("items") or [])
        return res

    def _build_search_aborted_record(failed_kw: str, failed_res: Dict[str, Any]) -> Dict[str, Any]:
        err_msg = f"[闲鱼搜索异常:{failed_res.get('error_code') or 'UNKNOWN'}] {failed_res.get('message') or '未知检索异常'}"
        return _build_final_record(
            candidate=cand,
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
        f"🔍 [步骤2/闲鱼小号搜索] 分类=【{cat_name}】 | 标品=【{cand['name'][:32]}】 | "
        f"我方价=¥{cand['activity_price']} | 搜索词=【{kw}】"
    )
    search_res = _run_search(kw, "闲鱼小号搜索")
    if not search_res.get("ok"):
        return _build_search_aborted_record(kw, search_res)

    raw_items = search_res.get("items") or []
    used_backup = False

    # 主搜索结果过少（<6条）时用备用词扩召回一次
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
            candidate=cand,
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

    # 第 2 次大模型调用：从概览选出真正可比的同款商品 ID
    coarse_items_for_llm, raw_card_map, valid_coarse_ids = _prepare_coarse_items_for_llm(raw_items)
    step2_comp_ids, step2_llm_meta, step2_reason = _filter_comparable_xianyu_items_by_llm(
        my_candidate_for_llm, coarse_items_for_llm, valid_coarse_ids
    )

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
            candidate=cand,
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
            llm_call_meta={},
            browser_aborted=False,
            debug_browser=debug_browser,
        )

    # 仅保留前 max_detail_checks（默认前 2 个）可比商品补充详情：7天内库里有详情直接复用，缺失才拉取
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
            f"⚡ [步骤3/闲鱼详情库缓存全命中] 分类=【{cat_name}】 | 前 {len(ids_to_fetch)} 款同款在 {detail_cache_days} 天内均有完整详情，"
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
                f"📦 [步骤3/详情缓存复用+无痕H5按需补拉] 分类=【{cat_name}】 | 保留前 {len(ids_to_fetch)} 款 "
                f"(库内{detail_cache_days}天缓存复用={detail_cache_hit_count}款，仅需补拉={len(missing_ids)}款)..."
            )
        else:
            logger.info(
                f"📦 [步骤3/无痕H5免登录拉取详情] 分类=【{cat_name}】 | AI初筛同款={len(step2_comp_ids)}款 -> "
                f"仅保留前 {len(ids_to_fetch)} 款拉取详情..."
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
                f"已完成={len(live_details_map)}/{len(missing_ids)} | 原因={batch_det_res.get('message')}"
            )

    details_by_id = {**cached_details_by_id, **(batch_det_res.get("details_by_id") or {})}
    detailed_items_for_llm: List[Dict[str, Any]] = []
    valid_detailed_ids: Set[str] = set()
    detail_failed_count = 0
    first_detail_err_msg = ""
    unfetched_due_to_abort_ids: List[str] = []
    batch_aborted_early = not bool(batch_det_res.get("ok", True))

    for idx, iid in enumerate(ids_to_fetch, 1):
        det = details_by_id.get(iid) or {}
        raw_card = raw_card_map.get(iid) or {}
        if not det.get("ok"):
            detail_failed_count += 1
            item_err_code = det.get("error_code") or batch_det_res.get("error_code") or "UNKNOWN"
            item_err_msg = det.get("message") or batch_det_res.get("message") or "批量会话提前中断未拉取"
            if not first_detail_err_msg:
                first_detail_err_msg = f"[{item_err_code}] {item_err_msg}"
            if not det and batch_aborted_early:
                unfetched_due_to_abort_ids.append(iid)
            else:
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
                "desc": det.get("desc"),
                "detail_price": det.get("price"),
                "original_price": det.get("original_price"),
                "transport_fee": det.get("transport_fee"),
                "is_free_shipping": det.get("is_free_shipping"),
                "has_multi_sku": det.get("has_multi_sku"),
                "min_sku_price": det.get("min_price"),
                "max_sku_price": det.get("max_price"),
                "sku_list": sku_list_clean,
                "cpv_labels": det.get("cpv_labels") or [],
            },
        })
        src_badge = f"库缓存:{det.get('_detail_updated_at_str')}" if det.get("_from_db_cache") else "H5新拉"
        logger.info(
            f"   ├─ ✅ ({idx}/{len(ids_to_fetch)}) 详情就绪 [XY:{iid}][{src_badge}] | "
            f"详情价=¥{det.get('price')} (区间:¥{det.get('min_price')}~¥{det.get('max_price')}) | "
            f"运费=¥{det.get('transport_fee')}({'包邮' if det.get('is_free_shipping') else '非包邮'}) | "
            f"多规格={det.get('has_multi_sku')}({len(sku_list_clean)}个SKU) | 标题={str(det.get('title') or raw_card.get('title') or '')[:32]}"
        )

    if unfetched_due_to_abort_ids:
        logger.warning(
            f"⚠️ [步骤3/会话熔断跳过剩余详情] 分类=【{cat_name}】 | 其余 {len(unfetched_due_to_abort_ids)} 款因会话提前中止未继续请求: {unfetched_due_to_abort_ids}"
        )

    if not detailed_items_for_llm:
        is_session_abort = not batch_det_res.get("ok", True) or bool(batch_det_res.get("is_risk_blocked"))
        abort_err = (
            f"[闲鱼详情异常:{batch_det_res.get('error_code') or 'DETAIL_ALL_FAILED'}] "
            f"{batch_det_res.get('message') or first_detail_err_msg or '同款候选拉取详情全部失败'}"
        )
        return _build_final_record(
            candidate=cand,
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
                "llm_reason": abort_err,
            },
            llm_call_meta={},
            fallback_error=abort_err,
            batch_det_res=batch_det_res,
            detail_failed_count=detail_failed_count,
            detail_cache_hit_count=detail_cache_hit_count,
            detail_live_fetched_count=detail_live_fetched_count,
            browser_aborted=is_session_abort,
            debug_browser=debug_browser,
        )

    # 第 3 次大模型调用：结合完整详情校准双方真实规格，直接以大模型最终给出的比例与胜负为准
    prompt_template = read_file_to_str(COMPARISON_PROMPT_PATH)
    comparison_payload = {
        "current_date": datetime.datetime.now(_CN_TZ).strftime("%Y-%m-%d"),
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
        id_hint_msg=f"请务必确保 `best_xianyu_item_id` 严格取自给定详情列表 ID 集合：{sorted(valid_detailed_ids)}，且 `cost_performance_ratio` 与 `llm_winner` 自洽，严格输出合法 JSON！",
    )
    if ok:
        logger.info(
            f"🤖 [步骤3/AI终审比价完成] 分类=【{cat_name}】 | 模型={llm_call_meta.get('model_used')} | "
            f"尝试={llm_call_meta.get('attempt_used')}/{LLM_MAX_ATTEMPTS} | 耗时={llm_call_meta.get('elapsed_seconds', 0):.1f}s | "
            f"最佳闲鱼ID={validated_llm_data.get('best_xianyu_item_id')} | 含邮总价=¥{validated_llm_data.get('best_xianyu_price')} | "
            f"比值={validated_llm_data.get('cost_performance_ratio')} | 胜负={validated_llm_data.get('llm_winner')}"
        )

    return _build_final_record(
        candidate=cand,
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
    ineligible: bool = False,
) -> Dict[str, Any]:
    """直接以第 3 次大模型最终判定的性价比比例与胜负结果构建入库文档（避免受预清洗误差干扰）。"""
    has_comp = bool(llm_data.get("has_comparable_item"))
    best_xy_id = str(llm_data.get("best_xianyu_item_id") or "").strip()
    best_xy_price = round(_num(llm_data.get("best_xianyu_price"), 0.0), 2)
    llm_winner = str(llm_data.get("llm_winner") or ("ineligible" if ineligible else "no_comparable")).strip()
    llm_reason = str(llm_data.get("llm_reason") or fallback_error or "未找到可比商品").strip()

    matched_u = str(llm_data.get("matched_unit") or "").strip() or None
    my_u_qty = _num(llm_data.get("my_unit_quantity"), 0.0)
    my_u_cp = _num(llm_data.get("my_unit_cost_performance"), 0.0)
    xy_u_qty = _num(llm_data.get("xianyu_unit_quantity"), 0.0)
    xy_u_cp = _num(llm_data.get("xianyu_unit_cost_performance"), 0.0)
    raw_ratio = _num(llm_data.get("cost_performance_ratio"), 0.0)

    my_qty_map: Dict[str, float] = dict(candidate.get("unit_quantities") or {})
    my_cp_map: Dict[str, float] = dict(candidate.get("unit_cost_performance") or {})
    xy_qty_clean: Dict[str, float] = {}
    xy_cp_clean: Dict[str, float] = {}

    if has_comp and matched_u:
        if my_u_qty > 0:
            my_qty_map = {matched_u: round(my_u_qty, 4)}
        if my_u_cp > 0:
            my_cp_map = {matched_u: round(my_u_cp, 6)}
        elif my_u_qty > 0 and _num(candidate.get("activity_price"), 0.0) > 0:
            my_cp_map = {matched_u: round(my_u_qty / float(candidate["activity_price"]), 6)}

        if xy_u_qty > 0:
            xy_qty_clean[matched_u] = round(xy_u_qty, 4)
        if xy_u_cp > 0:
            xy_cp_clean[matched_u] = round(xy_u_cp, 6)
        elif xy_u_qty > 0 and best_xy_price > 0:
            xy_cp_clean[matched_u] = round(xy_u_qty / best_xy_price, 6)

    if browser_aborted:
        comparison_basis = "browser_error"
        final_winner = "browser_error"
        cp_ratio = None
        matched_u = None
    elif ineligible:
        comparison_basis = "ineligible"
        final_winner = "ineligible"
        cp_ratio = None
        matched_u = None
    elif has_comp:
        comparison_basis = "llm_final_judgment"
        final_winner = llm_winner
        cp_ratio = round(raw_ratio, 4) if raw_ratio > 0 else None
    else:
        comparison_basis = "no_comparable"
        final_winner = "no_comparable"
        cp_ratio = None
        matched_u = None

    is_my_better = final_winner == "my_product"
    raw_xy_card = next((x for x in xianyu_items_for_llm if x.get("item_id") == best_xy_id), {})
    best_det_info = raw_xy_card.get("detail_verified_info") if isinstance(raw_xy_card.get("detail_verified_info"), dict) else {}

    best_xianyu_product = {
        "item_id": best_xy_id,
        "item_url": f"https://www.goofish.com/item?id={best_xy_id}" if best_xy_id else "",
        "title": str(raw_xy_card.get("title") or ""),
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
        "matched_unit": matched_u,
        "cost_performance_ratio": cp_ratio,
        "my_unit_quantities": my_qty_map,
        "my_unit_cost_performance": my_cp_map,
        "xianyu_unit_quantities": xy_qty_clean,
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
    step3_parsed_json = llm_call_meta.get("parsed_json") if llm_call_meta else None
    return {
        "source_type": src_type,
        "platform": candidate["platform"],
        "product_id": candidate["product_id"],
        "channel_type": candidate.get("channel_type"),
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
                "parsed_json": step3_parsed_json,
                "raw_text": llm_call_meta.get("raw_text") or "",
            },
        },
        "xianyu_audit_trail": {
            "browser_mode": "headless" if not debug_browser else "headed",
            "browser_aborted": bool(browser_aborted),
            "ineligible": bool(ineligible),
            "is_risk_blocked": bool(search_res.get("is_risk_blocked") or (batch_det_res or {}).get("is_risk_blocked")),
            "is_account_offline": bool(
                search_res.get("error_code") == "ACCOUNT_OFFLINE"
                or (search_res.get("is_offline") and not search_res.get("is_risk_blocked"))
            ),
            "search_keyword": used_keyword,
            "search_ok": bool(search_res.get("ok", True)),
            "search_error_code": str(search_res.get("error_code") or ""),
            "search_error_message": str(search_res.get("message") or "") if not search_res.get("ok", True) else "",
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
    bxy = record.get("best_xianyu_product") or {}
    audit = record.get("xianyu_audit_trail") or {}
    final_winner = str(record.get("final_winner") or "no_comparable")
    matched_u = cr.get("matched_unit")
    cp_ratio = cr.get("cost_performance_ratio")
    my_qty_map = cr.get("my_unit_quantities") or {}
    my_cp_map = cr.get("my_unit_cost_performance") or {}
    xy_qty_map = cr.get("xianyu_unit_quantities") or {}
    xy_cp_map = cr.get("xianyu_unit_cost_performance") or {}

    if audit.get("browser_aborted") or final_winner == "browser_error":
        return (
            "🚨 检索异常",
            "ERR",
            f"闲鱼浏览器检索异常中断：{cr.get('llm_reason') or '请搜索上方 ❌ 错误日志定位原因'}",
        )

    if audit.get("ineligible") or final_winner == "ineligible":
        return "⏭️ 无资格跳过", "-", str(cr.get("llm_reason") or "AI判定不具备闲鱼同款比价资格")

    if not cr.get("has_comparable_item") or final_winner == "no_comparable":
        return "⚪ 无可比款", "N/A", str(cr.get("llm_reason") or "闲鱼未找到具备可比性的同款商品")

    ratio_str = f"{cp_ratio:.2f}x ({matched_u})" if (cp_ratio is not None and matched_u) else (f"{cp_ratio:.2f}x" if cp_ratio is not None else "AI判定")
    if cp_ratio is not None and matched_u:
        my_u_cp = _fmt_num_clean(my_cp_map.get(matched_u, 0.0))
        xy_u_cp = _fmt_num_clean(xy_cp_map.get(matched_u, 0.0))
        my_u_qty = _fmt_num_clean(my_qty_map.get(matched_u, 0.0))
        xy_u_qty = _fmt_num_clean(xy_qty_map.get(matched_u, 0.0))
        if final_winner == "my_product":
            pct = (cp_ratio - 1.0) * 100.0
            return (
                "🏆 我方胜出",
                ratio_str,
                f"按[{matched_u}]对齐：我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u}({my_u_cp}{matched_u}/元) vs "
                f"闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u}({xy_u_cp}{matched_u}/元) -> 我方为闲鱼的 {cp_ratio:.2f} 倍(+{pct:.1f}%)",
            )
        if final_winner == "xianyu_product":
            pct = (1.0 - cp_ratio) * 100.0
            return (
                "🏳️ 闲鱼更优",
                ratio_str,
                f"按[{matched_u}]对齐：我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u}({my_u_cp}{matched_u}/元) vs "
                f"闲鱼含邮 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u}({xy_u_cp}{matched_u}/元) -> 我方为闲鱼的 {cp_ratio:.2f} 倍(-{pct:.1f}%)",
            )
        return "🤝 双方持平", ratio_str, f"按[{matched_u}]对齐，双方性价比持平 (比值 {cp_ratio:.2f}x)"

    if final_winner == "my_product":
        return "🏆 我方胜出", ratio_str, str(cr.get("llm_reason") or "AI终审判定我方性价比更高")
    if final_winner == "xianyu_product":
        return "🏳️ 闲鱼更优", ratio_str, str(cr.get("llm_reason") or "AI终审判定闲鱼性价比更高")
    return "🤝 双方持平", ratio_str, str(cr.get("llm_reason") or "AI终审判定双方性价比持平")


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
    my_qty = _fmt_qty_human(cr.get("my_unit_quantities") or cand.get("unit_quantities") or {}, focus_u)
    my_cp = _fmt_cp_human(cr.get("my_unit_cost_performance") or cand.get("unit_cost_performance") or {}, focus_u)
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
        xy_desc = cr.get("llm_reason") or "闲鱼无头浏览器检索异常中断"
    elif audit.get("ineligible"):
        xy_plat_id, xy_price, xy_qty, xy_cp = "XY :未发起检索", "-", "-", "-"
        xy_desc = cr.get("llm_reason") or "AI判定当前商品不具备比价资格，未消耗闲鱼配额"
    else:
        xy_plat_id, xy_price, xy_qty, xy_cp = "XY :无可比商品", "-", "-", "-"
        xy_desc = cr.get("llm_reason") or "未在闲鱼搜索结果中找到具备可比性的同款商品"

    det_ok = audit.get("step3_detail_fetched_count", 0)
    det_cache = audit.get("step3_detail_cache_hit_count", 0)
    det_fail = audit.get("step3_detail_failed_count", 0)
    cache_tag = f"[缓存复用{det_cache}]" if det_cache > 0 else ""
    det_stat_str = (
        f"详情{det_ok}款{cache_tag}"
        if det_fail == 0
        else f"详情{det_ok}款{cache_tag}(失败{det_fail}款)"
    )

    border_eq = "=" * 140
    border_dash = "-" * 140
    header_line = (
        f"🏁 【单品比价结论 #{idx}/{total}】[{src_lbl}] "
        f"分类: 【{record['category_name']}】(库内{cand.get('category_item_count', '-')}款)  |  "
        f"结论: {badge}  |  性价比比值(我方/闲鱼): {ratio_str}  |  "
        f"闲鱼搜索: \"{audit.get('search_keyword', '')}\"(初搜{audit.get('raw_search_count', 0)}->"
        f"AI初筛{len(audit.get('step2_comparable_item_ids') or [])}->{det_stat_str})"
    )
    col_header = (
        f"{_pad_col('对比角色', 10)} | {_pad_col('平台与商品编号', 19)} | {_pad_col('含邮总价', 10)} | "
        f"{_pad_col('核定规格数量', 18)} | {_pad_col('单位性价比 (每1元可得)', 26)} | 核心规格与商品标题描述"
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
    def _count_group(sub_recs: List[Dict[str, Any]]) -> Tuple[int, int, int, int, int, int]:
        w = sum(1 for r in sub_recs if r.get("final_winner") == "my_product")
        l = sum(1 for r in sub_recs if r.get("final_winner") == "xianyu_product")
        t = sum(1 for r in sub_recs if r.get("final_winner") == "tie")
        inel = sum(1 for r in sub_recs if r.get("final_winner") == "ineligible")
        e = sum(1 for r in sub_recs if (r.get("xianyu_audit_trail") or {}).get("browser_aborted"))
        n = sum(
            1
            for r in sub_recs
            if r.get("final_winner") not in ("my_product", "xianyu_product", "tie", "ineligible")
            and not (r.get("xianyu_audit_trail") or {}).get("browser_aborted")
        )
        return w, l, t, n, inel, e

    win_cnt, lose_cnt, tie_cnt, none_cnt, inel_cnt, err_cnt = _count_group(records)
    off_recs = [r for r in records if _normalize_source_type(r.get("source_type")) == SOURCE_TYPE_OFFICIAL]
    deal_recs = [r for r in records if _normalize_source_type(r.get("source_type")) == SOURCE_TYPE_DEAL_TIP]
    ow, ol, ot, on, oin, oe = _count_group(off_recs)
    dw, dl, dt_, dn, din, de = _count_group(deal_recs)

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
            f"详情成功={item_det_ok_cnt}个[7天缓存复用={item_det_cache_cnt}/无痕H5新拉={item_det_live_cnt}], 单品未捕获={item_det_fail_cnt}个)"
        )
    else:
        health_status = (
            f"🚨 检测到浏览器检索异常！(初搜失败={search_err_cnt} | 风控滑块={risk_block_cnt} | "
            f"批量详情中断={batch_abort_cnt} | 详情成功={item_det_ok_cnt}个[缓存复用={item_det_cache_cnt}/无痕H5新拉={item_det_live_cnt}], 失败={item_det_fail_cnt}个)"
        )

    eq_line = "=" * 144
    dash_line = "-" * 144
    lines = [
        "",
        eq_line,
        (
            f"📊 【闲鱼比价探测 · 本轮全局总结报告】 "
            f"批次: {batch_id}  |  总耗时: {elapsed:.1f}s  |  处理总计: {len(records)} 个  |  "
            f"🏆 我方胜出: {win_cnt}  |  🏳️ 闲鱼更优: {lose_cnt}  |  🤝 持平: {tie_cnt}  |  "
            f"⚪ 无可比: {none_cnt}  |  ⏭️ 无资格跳过: {inel_cnt}  |  🚨 检索异常: {err_cnt}"
        ),
        (
            f"📌 分源统计: "
            f"【正式官价】{len(off_recs)} (🏆胜{ow}/🏳️负{ol}/🤝平{ot}/⚪无{on}/⏭️跳{oin}/🚨异{oe})   ||   "
            f"【线报爆料】{len(deal_recs)} (🏆胜{dw}/🏳️负{dl}/🤝平{dt_}/⚪无{dn}/⏭️跳{din}/🚨异{de})"
        ),
        f"🌐 浏览器检索健康度: {health_status}",
        eq_line,
        (
            f"{_pad_col('序号', 4)} | {_pad_col('来源/分类(库内)', 18)} | {_pad_col('胜负结论', 13)} | "
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
        my_cp_col = _fmt_cp_human(cr.get("my_unit_cost_performance") or cand.get("unit_cost_performance") or {}, focus_u)
        xy_cp_col = _fmt_cp_human(cr.get("xianyu_unit_cost_performance") or {}, focus_u) if cr.get("has_comparable_item") else "-"

        lines.append(
            f"{_pad_col(f'#{idx}', 4)} | {_pad_col(cat_col, 18)} | {_pad_col(badge, 13)} | "
            f"{_pad_col(ratio_str, 14)} | {_pad_col(my_p_col, 9)} | {_pad_col(xy_p_col, 10)} | "
            f"{_pad_col(my_cp_col, 22)} | {_pad_col(xy_cp_col, 22)} | {quant_explain}"
        )

    lines.append(eq_line)
    return "\n".join(lines)


def _ensure_eval_indexes(eval_col: Any) -> None:
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
    if items_col is None:
        return
    items_col.create_index([("item_id", 1)], unique=True, background=True)
    items_col.create_index([("has_detail", 1), ("detail_updated_at", -1)], background=True)
    items_col.create_index([("updated_at", -1)], background=True)


# ==============================================================================
# 主流程：选出本轮候选商品（1:1交替） -> 逐商品一气呵成跑完全流程
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
    max_consecutive_browser_fails: int = MAX_CONSECUTIVE_BROWSER_FAILS,
) -> List[Dict[str, Any]]:
    global _LAST_CYCLE_ABORTED_BY_RISK
    _LAST_CYCLE_ABORTED_BY_RISK = False

    if not enable_official and not enable_deal_tip:
        logger.warning("⚠️ [未启用任何商品源] ENABLE_OFFICIAL 与 ENABLE_DEAL_TIP 均为 False，本轮直接退出。")
        return []

    t0 = time.monotonic()
    batch_id = datetime.datetime.now().strftime("disc_%Y%m%d_%H%M%S")
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
        f"🚀 [闲鱼高性价比商品探测与比价任务/启动] 批次={batch_id} | 启用商品源=[{enabled_desc}] (1:1交替) | "
        f"单源类别上限={max_categories if max_categories > 0 else '全部'} | "
        f"执行模式=(单商品全流程串行闭环, 补充详情上限={detail_cap_desc}, 详情缓存={detail_cache_days}天) | "
        f"浏览器模式={browser_mode_desc} | 搜索小号=<{search_user_data_dir}> | 详情模式=纯内存无痕H5\n"
        f"{'=' * 96}"
    )

    db = gen_db_object()
    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    _ensure_eval_indexes(eval_col)
    items_col = db.get_collection(XIANYU_ITEMS_COLLECTION_NAME)
    _ensure_xianyu_items_indexes(items_col)

    official_candidates = (
        select_round_candidates_for_source(
            db=db,
            hours_window=hours_window,
            max_categories=max_categories,
            source_type=SOURCE_TYPE_OFFICIAL,
        )
        if enable_official
        else []
    )
    deal_candidates = (
        select_round_candidates_for_source(
            db=db,
            hours_window=deal_hours_window,
            max_categories=max_categories,
            source_type=SOURCE_TYPE_DEAL_TIP,
        )
        if enable_deal_tip
        else []
    )
    candidates = interleave_candidates_one_to_one(official_candidates, deal_candidates)
    if not candidates:
        logger.warning("⚠️ [本轮结束] 启用的商品源均无满足时效与价格条件的未比价候选商品。")
        return []

    total_candidates = len(candidates)
    records: List[Dict[str, Any]] = []
    consecutive_fails = 0

    for idx, cand in enumerate(candidates, 1):
        src_lbl = _source_label(cand.get("source_type"))
        logger.info(
            f"\n>>> ▶️ [开始处理第 {idx}/{total_candidates} 个候选商品][{src_lbl}] "
            f"分类=【{cand['category_name']}】(库内{cand.get('category_item_count', '-')}款) | "
            f"候选商品=【{cand['name'][:40]}】(¥{cand['activity_price']:.2f})"
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
            logger.info(_format_single_comparison_card(idx, total_candidates, rec))

            if is_hard_stop or consecutive_fails >= max_fails:
                _LAST_CYCLE_ABORTED_BY_RISK = True
                remaining = total_candidates - idx
                logger.error(
                    f"\n{'!' * 96}\n"
                    f"🛑 [整轮熔断保护已触发] 分类=【{rec['category_name']}】检测到风控/掉线或连续失败({consecutive_fails}次)！\n"
                    f"   └── 已立即停止本轮剩余 {remaining}/{total_candidates} 个候选商品的后续处理，进入静默冷却保护！\n"
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
            logger.info(_format_single_comparison_card(idx, total_candidates, rec))
            if is_hard_stop:
                _LAST_CYCLE_ABORTED_BY_RISK = True
                remaining = total_candidates - idx
                logger.error(
                    f"\n{'!' * 96}\n"
                    f"🛑 [详情风控熔断保护已触发] 分类=【{rec['category_name']}】详情拉取期间检测到闲鱼风控拦截！\n"
                    f"   └── 当前已比价结果已保存，立即停止本轮剩余 {remaining}/{total_candidates} 个候选商品处理，进入静默冷却保护！\n"
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
            logger.exception(f"💥 [探测轮次异常] {exc}")
        if not ENABLE_LOOP:
            break
        if _LAST_CYCLE_ABORTED_BY_RISK:
            sleep_hours = max(float(LOOP_INTERVAL_HOURS or 1.0), float(RISK_COOLDOWN_HOURS or 2.0))
            logger.warning(
                f"🛑 本轮触发闲鱼限流/掉线保护，进入静默冷却：等待 {sleep_hours} 小时后再执行下一轮..."
            )
        else:
            sleep_hours = float(LOOP_INTERVAL_HOURS or 1.0)
            logger.info(f"⏳ 本轮执行完毕，等待 {sleep_hours} 小时后执行下一轮...")
        time.sleep(max(60.0, sleep_hours * 3600.0))


if __name__ == "__main__":
    main()
