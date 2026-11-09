# -*- coding: utf-8 -*-
"""
闲鱼高性价比商品探测与比价独立定时任务
================================================================================
执行流程（简洁、稳定、可追溯）：
  1. 【源头过滤与分类（步骤 1）】：
     - 从 MongoDB `products` 读取最近 72h 更新、`channel_type` 存在且有值、`format_status == "success"` 的商品；
     - 直接以 `activity_price` 作为我方成本（不计算净成本/不扣佣金）；
     - 分类依据：直接取 `format_info` 中 `core_entities` 得分（`score`）最高的 `name`。
  2. 【类内最高性价比选拔与官方实时价格验证（步骤 2）】：
     - 在每个分类内计算各商品在各个 `unit` 下的性价比值：`unit_cost_performance[u] = unit_quantity[u] / activity_price`（即每1元能买到的该单位数量，值越大性价比越高）；
     - 选出每个分类中性价比最高的 1 个商品，并立即调用官方接口 `get_goods_detail` 验证其实时 `activity_price`（若下架或失败则顺延同分类下一名，若价格变动则按实时 `activity_price` 重算性价比并确保仍是类内最高），无需任何“优势>15%”限制。
  3. 【闲鱼免登录搜索与大模型比价（步骤 3，含闲鱼商品编号强校验與 3 次重试）】：
     - 搜索闲鱼商品列表（并对前排低价商品抓取详情 `desc` 与多规格 `sku_list`）；
     - 将【我方候选商品信息（含已算好的各个 `unit` 性价比值）】与【查询到的闲鱼商品信息】提供给大模型：
       ① 让大模型找出可与我方相比的闲鱼商品，并从中选出性价比最高的 1 个最佳闲鱼商品（返回 `best_xianyu_item_id`）；
       ② 返回该最佳闲鱼商品尽可能与我方匹配的各个 `unit` 的性价比值 `xianyu_unit_cost_performance`；
       ③ 同时返回大模型的主观判断字段 `llm_winner` 与 `llm_reason`（作为无相同 `unit` 时的回退依据）；
     - 强校验大模型返回的 `best_xianyu_item_id` 是否真实存在于本次查询的闲鱼商品列表中，若校验失败或异常，大模型最多重试 3 次。
  4. 【比值计算与完整入库（步骤 4）】：
     - 若存在相同 `unit` 的性价比值，直接计算我方与闲鱼的性价比比值：`ratio = my_unit_cp[u] / xianyu_unit_cp[u]`（`> 1.0` 代表我方性价比更高）；
     - 若大模型未返回相同 `unit` 的性价比值，则采用大模型返回的判断字段 `llm_winner` 作为比较结果；
     - 将完整比较结果与原始证据写入 MongoDB `xianyu_deal_evaluations` 集合。
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
COMPARISON_PROMPT_PATH = os.path.join(PROMPT_DIR, "闲鱼同款比价与性价比计算.txt")
LLM_MAX_ATTEMPTS = 3


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
    keys = list(d.keys())
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
    for idx, u in enumerate(keys[:3]):
        prefix = "★" if (focus_unit and u == focus_unit and len(keys) > 1) else ""
        parts.append(f"{prefix}{_fmt_num_clean(cp_map[u])}{u}/元")
    return " | ".join(parts)



# ==============================================================================
# 步骤 1 & 2：源头过滤、按最高分 core_entity 分类、类内选最高性价比并验证官方实时价格
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
            valid_skus.append(( abs(sku_p - activity_price), v ))

    if not valid_skus:
        for v in fi.values():
            if isinstance(v, dict) and isinstance(v.get("core_entities"), list):
                valid_skus.append((0.0, v))
                break

    if not valid_skus:
        return None

    valid_skus.sort(key=lambda x: x[0])
    primary_sku = valid_skus[0][1]

    # 分类依据：直接取 core_entities 得分最高的 score 对应的 name
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
    """当大模型关键字生成异常时的保底规则拼接。"""
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


def _generate_search_keywords_by_llm(
    doc: Dict[str, Any],
    sku_fmt: Dict[str, Any],
    category_name: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """
    调用简单模型 (`preset_model_group="low"`) + 外部提示词文件 `闲鱼搜索关键字生成.txt` 生成闲鱼主搜索词与备用搜索词。
    """
    fb_main, fb_backup = _build_fallback_search_keywords(doc, sku_fmt, category_name)
    prompt_template = read_file_to_str(KEYWORD_PROMPT_PATH)
    candidate_payload = {
        "category_name": category_name,
        "name": str(doc.get("name") or "").strip(),
        "brand": str(doc.get("brand") or "").strip(),
        "decision_keywords": sku_fmt.get("decision_keywords") or [],
        "pricing_basis": sku_fmt.get("pricing_basis") or {},
    }
    full_prompt = (
        f"{prompt_template}\n"
        f"<candidate_input>\n{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}\n</candidate_input>"
    )

    last_raw = ""
    last_model = None
    last_trace = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        res = generate_content(prompt=full_prompt, preset_model_group="low")
        last_model = (res.get("metrics") or {}).get("model_used")
        last_trace = res.get("trace_id")
        last_raw = str(res.get("content") or "").strip()
        if "成功" not in str(res.get("status") or "") or not last_raw:
            continue
        try:
            parsed = string_to_object(last_raw)
            if isinstance(parsed, dict):
                main_kw = str(parsed.get("search_keyword") or "").strip()
                backup_kw = str(parsed.get("backup_search_keyword") or "").strip() or main_kw
                if main_kw:
                    return (
                        main_kw,
                        backup_kw,
                        {
                            "model_used": last_model,
                            "attempt_used": attempt,
                            "trace_id": last_trace,
                            "parsed_json": parsed,
                            "raw_text": last_raw,
                        },
                    )
        except Exception as exc:
            logger.warning(f"⚠️ [搜索词生成JSON解析重试 {attempt}/{LLM_MAX_ATTEMPTS}] 原因={exc}")

    return (
        fb_main,
        fb_backup,
        {
            "model_used": last_model,
            "attempt_used": LLM_MAX_ATTEMPTS,
            "trace_id": last_trace,
            "parsed_json": {"search_keyword": fb_main, "backup_search_keyword": fb_backup, "reason": "fallback_rule"},
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
    步骤 1 & 2：
    1. 查询 72h 内、`channel_type` 存在且有值、`format_status == "success"` 的商品；
    2. 按 `core_entities` 最高 `score` 的 `name` 归类；
    3. 每个分类内按 `unit_cost_performance` (`数量 / activity_price`) 选出性价比最高的 1 个候选商品，
       并在此步完成官方实时 `activity_price` 核验与性价比重算。
    """
    repo = ProductRepository()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_dt = (now_utc - datetime.timedelta(hours=hours_window)).replace(tzinfo=None)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    cooldown_str = (datetime.datetime.now() - datetime.timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S")
    recent_eval_docs = list(
        eval_col.find(
            {"evaluated_at": {"$gte": cooldown_str}},
            {"platform": 1, "product_id": 1, "category_name": 1},
        )
    )
    evaluated_keys = {(d.get("platform"), str(d.get("product_id"))) for d in recent_eval_docs}
    evaluated_categories = {str(d.get("category_name") or "").strip() for d in recent_eval_docs if d.get("category_name")}

    query = {
        "$and": [
            repo._boom_content_filter(),  # 强制要求 channel_type 存在且有值，且金额/名称合法
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
    for doc in raw_docs:
        pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        if not pid:
            continue
        act_price = _num(doc.get("activity_price") or doc.get("price"), 0.0)
        if act_price <= 0:
            continue
        cat_and_sku = _extract_primary_sku_and_category(doc, act_price)
        if not cat_and_sku:
            continue
        cat_name, primary_sku = cat_and_sku
        by_cat[cat_name].append((doc, primary_sku, act_price))

    # 严格按每个类别对应的商品总数量从多到少排序选择
    sorted_cats = sorted(by_cat.items(), key=lambda kv: len(kv[1]), reverse=True)
    top_preview = ", ".join(f"{k}({len(v)}款)" for k, v in sorted_cats[:20])
    logger.info(
        f"📊 [步骤1/源头扫描] 72h内有channel_type且已结构化商品={len(raw_docs)} | "
        f"归入core_entities最高分分类数={len(sorted_cats)} | 本轮目标选取分类数={max_categories} | "
        f"商品数量最多前20分类=[{top_preview}]"
    )

    champions: List[Dict[str, Any]] = []
    for cat_name, raw_items in sorted_cats:
        if len(champions) >= max_categories:
            break
        if cat_name in evaluated_categories:
            continue

        # 统计该分类内各物理维度的最高频标准单位（与 ProductRepository 单位归一化保持一致）
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

        # 计算每个商品的标准化单位数量与各 unit 性价比值
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

        for grp in unit_groups.values():
            grp.sort(key=lambda x: x[0], reverse=True)
        ordered_units = sorted(
            unit_groups.keys(),
            key=lambda u: (len(unit_groups[u]), unit_groups[u][0][0]),
            reverse=True,
        )
        top_unit = ordered_units[0]
        ranked_indices = [idx for _, idx in unit_groups[top_unit]]

        # 对该分类前排候选依次进行官方实时价格验证，锁定实时验证后性价比最高的 1 个商品
        verified_pool: List[Dict[str, Any]] = []
        for pos, cand_idx in enumerate(ranked_indices[:3]):
            c = cat_candidates[cand_idx]
            doc = c["doc"]
            plat = normalize_platform(doc.get("platform") or "pdd")
            pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
            gsign = str(doc.get("goods_sign") or "").strip()

            live_price = _verify_live_activity_price(plat, pid, gsign, c["db_activity_price"])
            if live_price is None or live_price <= 0:
                continue

            live_unit_cp = _compute_unit_cp(c["unit_quantities"], live_price)
            c["verified_activity_price"] = live_price
            c["unit_cost_performance"] = live_unit_cp
            c["live_top_unit_cp"] = live_unit_cp.get(top_unit, 0.0)
            verified_pool.append(c)

            # 若第 1 名实时价格未涨价（或实时性价比依然 >= 第 2 名的入库性价比），无需再查第 2 名，直接当选！
            next_db_cp = unit_groups[top_unit][pos + 1][0] if (pos + 1) < len(unit_groups[top_unit]) else -1.0
            if c["live_top_unit_cp"] >= next_db_cp:
                break

        if not verified_pool:
            continue

        verified_pool.sort(key=lambda x: x["live_top_unit_cp"], reverse=True)
        winner = verified_pool[0]
        w_doc = winner["doc"]
        w_sku = winner["sku_fmt"]
        main_kw, backup_kw, llm_kw_meta = _generate_search_keywords_by_llm(w_doc, w_sku, cat_name)

        kws_list = [
            f"{k.get('attribute_name')}:{k.get('attribute_value')}"
            for k in (w_sku.get("decision_keywords") or [])
            if isinstance(k, dict) and k.get("attribute_value")
        ]
        champion_record = {
            "platform": normalize_platform(w_doc.get("platform") or "pdd"),
            "product_id": str(w_doc.get("product_id") or w_doc.get("goods_id") or "").strip(),
            "goods_sign": str(w_doc.get("goods_sign") or "").strip(),
            "channel_type": w_doc.get("channel_type"),
            "category_name": cat_name,
            "category_item_count": len(raw_items),
            "name": str(w_doc.get("name") or "").strip(),
            "brand": str(w_doc.get("brand") or "").strip(),
            "db_activity_price": winner["db_activity_price"],
            "activity_price": winner["verified_activity_price"],
            "primary_unit": top_unit,
            "unit_quantities": winner["unit_quantities"],
            "unit_cost_performance": winner["unit_cost_performance"],
            "decision_keywords_summary": " | ".join(kws_list),
            "pricing_basis": w_sku.get("pricing_basis") or {},
            "search_keyword": main_kw,
            "backup_search_keyword": backup_kw,
            "llm_keyword_output": llm_kw_meta,
            "product_url": str(w_doc.get("product_url") or ""),
            "image_url": str(w_doc.get("image_url") or ""),
            "updated_at": str(w_doc.get("updated_at") or ""),
        }
        champions.append(champion_record)
        logger.info(
            f"🎯 [步骤2/分类最高性价比入选 #{len(champions)}] 分类=【{cat_name}】(共{len(raw_items)}款) | "
            f"[{champion_record['platform'].upper()}:{champion_record['product_id']}] | "
            f"验价后activity_price=¥{champion_record['activity_price']} (入库=¥{champion_record['db_activity_price']}) | "
            f"各unit数量={champion_record['unit_quantities']} | 各unit性价比={champion_record['unit_cost_performance']} | "
            f"闲鱼搜索词=【{main_kw}】(备用=【{backup_kw}】, 模型={llm_kw_meta.get('model_used')}) | "
            f"标题={champion_record['name'][:36]}"
        )

    return champions


# ==============================================================================
# 步骤 3 & 4：闲鱼商品搜索 + 大模型找最高性价比可比商品（含编号强校验与 3 次重试）+ 比值计算
# ==============================================================================
def _validate_llm_comparison_response(
    parsed: Any,
    valid_xianyu_ids: Set[str],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    强校验大模型返回的比价 JSON 数据：
    1. 必须是字典；
    2. `has_comparable_item` 必须为布尔值；
    3. 若 `has_comparable_item == True`：
       - `best_xianyu_item_id` 必须非空，且**真实存在于本次查询的闲鱼商品编号集合 `valid_xianyu_ids` 中**；
       - `best_xianyu_price` 必须 > 0；
       - `xianyu_unit_cost_performance` 必须是字典；
    4. `llm_winner` 必须属于 `{"my_product", "xianyu_product", "tie", "no_comparable"}`。
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

    if has_comp:
        best_id = str(parsed.get("best_xianyu_item_id") or "").strip()
        if not best_id:
            return False, "has_comparable_item=true 时 best_xianyu_item_id 不能为空", {}
        if best_id not in valid_xianyu_ids:
            return (
                False,
                f"返回的闲鱼商品编号 best_xianyu_item_id='{best_id}' 不在本次查询提供的闲鱼商品ID列表中({sorted(valid_xianyu_ids)[:8]}...)",
                {},
            )
        best_p = _num(parsed.get("best_xianyu_price"), 0.0)
        if best_p <= 0:
            return False, f"best_xianyu_price={parsed.get('best_xianyu_price')} 必须大于 0", {}
        xy_cp = parsed.get("xianyu_unit_cost_performance")
        if not isinstance(xy_cp, dict):
            return False, "xianyu_unit_cost_performance 必须是字典(dict)", {}

    return True, "ok", parsed


def compare_candidate_with_xianyu(
    candidate: Dict[str, Any],
    guest_user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    max_search_items: int = 20,
    max_detail_checks: int = 0,
    debug_browser: bool = False,
) -> Dict[str, Any]:
    """
    查询闲鱼商品，读取外部提示词文件 `闲鱼同款比价与性价比计算.txt`，
    将候选商品信息（含已算好的各 unit 性价比值）与查询到的闲鱼商品信息拼接在末尾提供给大模型：
    - 大模型找出可比商品并选出闲鱼中性价比最高的 1 个商品；
    - 返回该最佳闲鱼商品匹配的各个 unit 的性价比值，以及大模型的判断字段；
    - 校验闲鱼商品编号，支持最多 3 次重试；
    - 计算我方与闲鱼的性价比比值（或回退大模型判断）并组装完整入库文档（包含完整大模型原始输出）。
    """
    kw = candidate["search_keyword"]
    backup_kw = candidate["backup_search_keyword"]
    my_price = candidate["activity_price"]

    logger.info(
        f"🔍 [步骤3/闲鱼搜索] 分类=【{candidate['category_name']}】 | 候选=【{candidate['name'][:32]}】 | "
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
        logger.info(f"🔄 [步骤3/搜索词扩召回] 主搜索词【{kw}】仅返回 {len(raw_items)} 条，切换【{backup_kw}】重搜...")
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
        logger.warning(f"⚠️ [步骤3/闲鱼搜索为空] 搜索词=【{kw}】 | msg={search_res.get('message')}")
        return _build_final_record(candidate, kw, search_res, [], {}, {}, "no_xianyu_search_results")

    detail_map: Dict[str, Dict[str, Any]] = {}
    if max_detail_checks > 0:
        cheapest_items = sorted(
            [x for x in raw_items if _num(x.get("price")) > 0],
            key=lambda x: _num(x.get("price"), 999999.0),
        )[:max_detail_checks]
        for idx, item in enumerate(cheapest_items, 1):
            if idx > 1:
                time.sleep(1.2)
            det = get_xianyu_item_detail(
                item_id_or_url=item["item_id"],
                user_data_dir=guest_user_data_dir,
                debug=debug_browser,
            )
            if det.get("ok"):
                detail_map[str(item["item_id"])] = {
                    "desc": det.get("desc"),
                    "transport_fee": det.get("transport_fee"),
                    "has_multi_sku": det.get("has_multi_sku"),
                    "sku_list": (det.get("sku_list") or [])[:15],
                }

    # 构造提供给大模型的闲鱼商品列表
    xianyu_items_for_llm: List[Dict[str, Any]] = []
    valid_xianyu_ids: Set[str] = set()
    for x in raw_items:
        iid = str(x.get("item_id") or "").strip()
        if not iid:
            continue
        valid_xianyu_ids.add(iid)
        entry: Dict[str, Any] = {
            "item_id": iid,
            "title": x.get("title"),
            "display_price": x.get("price"),
            "want_cnt": x.get("want_cnt"),
            "is_free_shipping": x.get("is_free_shipping"),
        }
        if iid in detail_map:
            entry["detail_verified_info"] = detail_map[iid]
        xianyu_items_for_llm.append(entry)

    my_candidate_for_llm = {
        "product_id": candidate["product_id"],
        "platform": candidate["platform"],
        "category_name": candidate["category_name"],
        "name": candidate["name"],
        "brand": candidate["brand"],
        "decision_keywords": candidate["decision_keywords_summary"],
        "activity_price": candidate["activity_price"],
        "unit_quantities": candidate["unit_quantities"],
        "unit_cost_performance": candidate["unit_cost_performance"],
    }

    prompt_template = read_file_to_str(COMPARISON_PROMPT_PATH)
    comparison_payload = {
        "my_candidate_product": my_candidate_for_llm,
        "xianyu_search_items": xianyu_items_for_llm,
    }
    base_prompt = (
        f"{prompt_template}\n"
        f"<comparison_input>\n{json.dumps(comparison_payload, ensure_ascii=False, indent=2)}\n</comparison_input>"
    )

    # 大模型调用与关键数据强校验（最多 3 次机会，使用 model_api 内置超时与重试机制）
    validated_llm_data: Dict[str, Any] = {}
    llm_call_meta: Dict[str, Any] = {}
    last_error_msg = ""
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        prompt_to_send = base_prompt
        if last_error_msg:
            prompt_to_send += (
                f"\n\n⚠️【上次返回校验失败修正提示（第 {attempt}/{LLM_MAX_ATTEMPTS} 次尝试）】：\n"
                f"你上一次返回的数据未通过校验，原因：{last_error_msg}。\n"
                f"请务必确保 `best_xianyu_item_id` 严格取自给定的闲鱼列表 ID 集合：{sorted(valid_xianyu_ids)}，并严格输出合法 JSON！"
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
                f"⚠️ [步骤3/大模型第{attempt}/{LLM_MAX_ATTEMPTS}次调用失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_error_msg}"
            )
            continue

        try:
            parsed = string_to_object(content)
        except Exception as exc:
            last_error_msg = f"JSON解析异常: {exc}"
            logger.warning(
                f"⚠️ [步骤3/大模型第{attempt}/{LLM_MAX_ATTEMPTS}次JSON解析失败] 模型={model_used} | 耗时={elapsed:.1f}s | 原因={last_error_msg}"
            )
            continue

        is_ok, err_reason, clean_dict = _validate_llm_comparison_response(parsed, valid_xianyu_ids)
        if not is_ok:
            last_error_msg = err_reason
            logger.warning(
                f"⚠️ [步骤3/大模型第{attempt}/{LLM_MAX_ATTEMPTS}次数据校验未通过] 模型={model_used} | 耗时={elapsed:.1f}s | 校验失败原因={err_reason}"
            )
            continue

        validated_llm_data = clean_dict
        llm_call_meta["parsed_json"] = dict(clean_dict)
        logger.info(
            f"🤖 [步骤3/大模型比价校验通过] 模型={model_used} | 尝试={attempt}/{LLM_MAX_ATTEMPTS} | 耗时={elapsed:.1f}s | "
            f"最佳闲鱼ID={clean_dict.get('best_xianyu_item_id')} | llm_winner={clean_dict.get('llm_winner')}"
        )
        break

    return _build_final_record(
        candidate=candidate,
        used_keyword=kw,
        search_res=search_res,
        xianyu_items_for_llm=xianyu_items_for_llm,
        llm_data=validated_llm_data,
        llm_call_meta=llm_call_meta,
        fallback_error=last_error_msg if not validated_llm_data else "",
    )


def _build_final_record(
    candidate: Dict[str, Any],
    used_keyword: str,
    search_res: Dict[str, Any],
    xianyu_items_for_llm: List[Dict[str, Any]],
    llm_data: Dict[str, Any],
    llm_call_meta: Dict[str, Any],
    fallback_error: str = "",
) -> Dict[str, Any]:
    """
    步骤 4：
    计算我方候选商品与最佳闲鱼商品的 `unit` 性价比比值 (`my_unit_cp / xianyu_unit_cp`)；
    若无相同 `unit` 的性价比值，则退而求其次采用大模型返回的判断字段 `llm_winner` 作为比较结果；
    同时将大模型核心原始返回结果 (`llm_raw_output`) 完整保存到数据库中以便复盘。
    """
    my_cp_map: Dict[str, float] = candidate["unit_cost_performance"]
    has_comp = bool(llm_data.get("has_comparable_item"))
    best_xy_id = str(llm_data.get("best_xianyu_item_id") or "").strip()
    best_xy_price = round(_num(llm_data.get("best_xianyu_price"), 0.0), 2)

    # 用最佳闲鱼商品的 unit_quantities 和 best_xy_price 校准/补齐 xianyu_unit_cost_performance，防止大模型算术小数误差
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
        chosen_unit = primary_unit if primary_unit in matched_unit_ratios else next(iter(matched_unit_ratios.keys()))
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
        final_winner = llm_winner

    is_my_better = final_winner == "my_product"

    raw_xy_card = next((x for x in xianyu_items_for_llm if x.get("item_id") == best_xy_id), {})

    best_xianyu_product = {
        "item_id": best_xy_id,
        "item_url": f"https://www.goofish.com/item?id={best_xy_id}" if best_xy_id else "",
        "title": str(llm_data.get("best_xianyu_title") or raw_xy_card.get("title") or ""),
        "display_price": raw_xy_card.get("display_price"),
        "comparable_price": best_xy_price,
        "spec_description": str(llm_data.get("best_xianyu_spec") or ""),
        "want_cnt": raw_xy_card.get("want_cnt"),
        "is_free_shipping": raw_xy_card.get("is_free_shipping"),
        "unit_quantities": xy_qty_clean,
        "unit_cost_performance": xy_cp_clean,
    }

    comparison_result = {
        "has_comparable_item": has_comp,
        "comparison_basis": comparison_basis,  # "unit_ratio"（精确单位性价比比值） 或 "llm_judgment_fallback"（大模型兜底判断）
        "matched_unit": chosen_unit,
        "cost_performance_ratio": cp_ratio,  # 我方性价比 / 闲鱼性价比（>1.0 代表我方更高）
        "unit_cp_ratios": matched_unit_ratios,  # 所有共同 unit 的比值字典
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
            "comparison_llm": {
                "model_used": llm_call_meta.get("model_used"),
                "attempt_used": llm_call_meta.get("attempt_used"),
                "trace_id": llm_call_meta.get("trace_id"),
                "elapsed_seconds": llm_call_meta.get("elapsed_seconds"),
                "parsed_json": llm_call_meta.get("parsed_json") or llm_data,
                "raw_text": llm_call_meta.get("raw_text") or "",
            },
            "keyword_llm": candidate.get("llm_keyword_output") or {},
        },
        "xianyu_audit_trail": {
            "search_keyword": used_keyword,
            "search_ok": bool(search_res.get("ok")),
            "raw_search_count": len(xianyu_items_for_llm),
            "comparable_item_ids": llm_data.get("comparable_item_ids") or [],
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
            "闲鱼未搜索到同品牌同规格的可比商品",
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
                f"赢在单价更低：按单位[{matched_u}]对齐，我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u} "
                f"(每元可得 {my_u_cp}{matched_u}) vs 闲鱼 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u} "
                f"(每元可得 {xy_u_cp}{matched_u}) -> 我方性价比是闲鱼的 {cp_ratio:.2f} 倍 (高出 +{pct:.1f}%)"
            )
            return "🏆 我方胜出", ratio_str, quant_str
        elif final_winner == "xianyu_product":
            pct = (1.0 - cp_ratio) * 100.0
            quant_str = (
                f"输在单价偏高：按单位[{matched_u}]对齐，我方 ¥{record['activity_price']:.2f} 买 {my_u_qty}{matched_u} "
                f"(每元可得 {my_u_cp}{matched_u}) vs 闲鱼 ¥{bxy.get('comparable_price', 0):.2f} 买 {xy_u_qty}{matched_u} "
                f"(每元可得 {xy_u_cp}{matched_u}) -> 我方性价比仅为闲鱼的 {cp_ratio:.2f} 倍 (低了 -{pct:.1f}%)"
            )
            return "🏳️ 闲鱼更优", ratio_str, quant_str
        else:
            quant_str = (
                f"双方持平：按单位[{matched_u}]对齐，双方每元均可买 {my_u_cp}{matched_u} (比值 {cp_ratio:.2f}x)"
            )
            return "🤝 双方持平", ratio_str, quant_str

    # 跨单位 AI 兜底判断
    ratio_str = "AI跨单位判定"
    if final_winner == "my_product":
        quant_str = (
            f"赢在整体规格价格比（跨单位AI兜底判定）：我方 ¥{record['activity_price']:.2f} "
            f"[{_fmt_qty_human(cand.get('unit_quantities') or {})}] vs "
            f"闲鱼 ¥{bxy.get('comparable_price', 0):.2f} [{_fmt_qty_human(bxy.get('unit_quantities') or {})}]"
        )
        return "🏆 我方胜出", ratio_str, quant_str
    elif final_winner == "xianyu_product":
        quant_str = (
            f"输在整体规格价格比（跨单位AI兜底判定）：我方 ¥{record['activity_price']:.2f} "
            f"[{_fmt_qty_human(cand.get('unit_quantities') or {})}] vs "
            f"闲鱼 ¥{bxy.get('comparable_price', 0):.2f} [{_fmt_qty_human(bxy.get('unit_quantities') or {})}]"
        )
        return "🏳️ 闲鱼更优", ratio_str, quant_str
    return "🤝 双方持平", ratio_str, "跨单位AI判定双方性价比相当"


def _format_champions_summary_table(champions: List[Dict[str, Any]]) -> str:
    """将步骤 2 选出的所有分类候选冠军格式化为对齐表格，方便运维在比价开始前一眼看清候选池。"""
    sep = "-" * 136
    lines = [
        "",
        "=" * 136,
        f"📋 【阶段 1 汇总：本轮入选的 {len(champions)} 个大类性价比冠军候选清单】（已通过官方接口实时核验价格）",
        sep,
        (
            f"{_pad_col('序号', 4)} | "
            f"{_pad_col('分类名称(库内数)', 18)} | "
            f"{_pad_col('平台与商品ID', 19)} | "
            f"{_pad_col('核验价格', 10)} | "
            f"{_pad_col('规格数量', 16)} | "
            f"{_pad_col('我方单位性价比(每1元)', 24)} | "
            f"{_pad_col('闲鱼搜索词', 18)} | "
            f"我方商品标题与属性"
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
    将单个商品与闲鱼最优商品的比价结果排版为严格上下对齐的【对战卡片】，
    一眼看清：我方描述/价格/性价比 vs 他方最优描述/价格/性价比，以及赢在哪里、输在哪里。
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
        xy_desc = f"【提取规格: {xy_spec or '见标题'}】 标题: {bxy.get('title') or ''}"
    else:
        xy_plat_id = "XY :无可比商品"
        xy_price = "-"
        xy_qty = "-"
        xy_cp = "-"
        xy_desc = "未在闲鱼搜索结果中找到同款或同规格可比商品"

    border_eq = "=" * 140
    border_dash = "-" * 140
    header_line = (
        f"🏁 【单品比价结论 #{idx}/{total}】 "
        f"分类: 【{record['category_name']}】(库内{cand.get('category_item_count', '-')}款)  |  "
        f"结论: {badge}  |  性价比比值(我方/闲鱼): {ratio_str}  |  "
        f"闲鱼搜索词: \"{audit.get('search_keyword', '')}\"(召回{audit.get('raw_search_count', 0)}款)"
    )
    col_header = (
        f"{_pad_col('对比角色', 10)} | "
        f"{_pad_col('平台与商品编号', 19)} | "
        f"{_pad_col('对比价格', 10)} | "
        f"{_pad_col('提取规格数量', 18)} | "
        f"{_pad_col('单位性价比 (每1元可得)', 26)} | "
        f"核心规格与商品标题描述"
    )
    my_row = (
        f"{_pad_col('[我方商品]', 10)} | "
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
    在整轮任务结束时输出【运维全局总结汇总报告】：
    包含：
      1. 顶部汇总表格（所有分类一行一个，严格列对齐）；
      2. 逐个分类的【我方商品 vs 闲鱼最优】上下对齐明细汇总，无需往上翻日志即可复盘全部商品。
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
            f"参评分类: {len(records)} 个  |  "
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
            f"{_pad_col('闲鱼最优价', 10)} | "
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
        "【二、逐商品 [我方候选 vs 闲鱼最优] 对齐明细汇总（直接上下对比描述、价格、规格与性价比）】",
        dash_line,
        (
            f"{_pad_col('序号/角色', 13)} | "
            f"{_pad_col('分类名称', 12)} | "
            f"{_pad_col('平台与编号', 19)} | "
            f"{_pad_col('对比价格', 9)} | "
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
            f"{_pad_col(f'[#{idx} 我方商品]', 13)} | "
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
        f"探测分类数={max_categories} | 访客目录=<{guest_user_data_dir}>\n"
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
        logger.warning("⚠️ [本轮结束] 未选出满足条件的分类候选商品。")
        return []

    logger.info(_format_champions_summary_table(champions))

    records: List[Dict[str, Any]] = []
    for idx, cand in enumerate(champions, 1):
        logger.info(
            f"\n>>> ▶️ [开始比价第 {idx}/{len(champions)} 个分类冠军] "
            f"分类=【{cand['category_name']}】(库内{cand.get('category_item_count', '-')}款) | "
            f"我方候选=【{cand['name'][:40]}】(¥{cand['activity_price']:.2f})"
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
    parser = argparse.ArgumentParser(description="闲鱼高性价比商品探测与比价定时任务")
    parser.add_argument("--hours-window", type=int, default=72, help="内部商品库扫描时间窗口（默认72小时）")
    parser.add_argument("--max-categories", "--max-candidates", dest="max_categories", type=int, default=20, help="单轮探测分类数（默认20）")
    parser.add_argument("--max-search-items", type=int, default=20, help="每次闲鱼搜索抓取的商品数（默认20）")
    parser.add_argument("--max-detail-checks", type=int, default=0, help="每次自动补充详情与SKU的前排低价商品数（默认0，仅搜索不额外开详情）")
    parser.add_argument("--cooldown-hours", type=int, default=24, help="同一商品重复比价冷却期（默认24小时）")
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
