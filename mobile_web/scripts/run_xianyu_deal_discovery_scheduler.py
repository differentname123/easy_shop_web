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
from mobile_web.scripts.goods_info_format import string_to_object
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


def _build_search_keywords(doc: Dict[str, Any], sku_fmt: Dict[str, Any], category_name: str) -> Tuple[str, str]:
    """从候选商品结构化标签构建闲鱼主搜索词（品牌+分类+主规格）与精简兜底词（品牌+分类）。"""
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

    pb = sku_fmt.get("pricing_basis") if isinstance(sku_fmt.get("pricing_basis"), dict) else {}
    struct = pb.get("structure") if isinstance(pb.get("structure"), list) else []
    spec_tokens: List[str] = []
    for s in reversed(struct):
        if not isinstance(s, dict):
            continue
        u = str(s.get("unit") or "").strip()
        v = _num(s.get("value"), 0.0)
        if not u or v <= 0 or (v == 1.0 and u in ("个", "件", "套", "瓶", "包", "盒", "袋", "双")):
            continue
        spec_tokens.append(f"{v:g}{u}")

    base_parts: List[str] = []
    if brand and brand not in category_name:
        base_parts.append(brand)
    base_parts.append(category_name)
    backup_kw = " ".join(base_parts).strip()

    main_parts = list(base_parts) + spec_tokens[:2]
    main_kw = " ".join(main_parts).strip()
    return main_kw or category_name, backup_kw or category_name


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
    max_categories: int = 5,
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
    evaluated_keys = {
        (d.get("platform"), str(d.get("product_id")))
        for d in eval_col.find({"evaluated_at": {"$gte": cooldown_str}}, {"platform": 1, "product_id": 1})
    }

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
    raw_docs = list(
        repo.col.find(query, projection)
        .sort([("updated_at", -1)])
        .limit(4000)
    )

    by_cat: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any], float]]] = defaultdict(list)
    for doc in raw_docs:
        plat = normalize_platform(doc.get("platform") or "pdd")
        pid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        if not pid or (plat, pid) in evaluated_keys:
            continue
        act_price = _num(doc.get("activity_price") or doc.get("price"), 0.0)
        if act_price <= 0:
            continue
        cat_and_sku = _extract_primary_sku_and_category(doc, act_price)
        if not cat_and_sku:
            continue
        cat_name, primary_sku = cat_and_sku
        by_cat[cat_name].append((doc, primary_sku, act_price))

    # 按分类下商品丰富度从高到低遍历分类
    sorted_cats = sorted(by_cat.items(), key=lambda kv: len(kv[1]), reverse=True)
    logger.info(
        f"📊 [步骤1/源头扫描] 72h内有channel_type且已结构化商品={len(raw_docs)} | "
        f"归入core_entities最高分分类数={len(sorted_cats)} | 本轮目标选取分类数={max_categories}"
    )

    champions: List[Dict[str, Any]] = []
    for cat_name, raw_items in sorted_cats:
        if len(champions) >= max_categories:
            break

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
        main_kw, backup_kw = _build_search_keywords(w_doc, w_sku, cat_name)

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
            f"闲鱼搜索词=【{main_kw}】 | 标题={champion_record['name'][:36]}"
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
    max_detail_checks: int = 4,
    debug_browser: bool = False,
) -> Dict[str, Any]:
    """
    查询闲鱼商品，并将候选商品信息（含已算好的各 unit 性价比值）与闲鱼商品信息提供给大模型：
    - 大模型找出可比商品并选出闲鱼中性价比最高的 1 个商品；
    - 返回该最佳闲鱼商品匹配的各个 unit 的性价比值，以及大模型的判断字段；
    - 校验闲鱼商品编号，支持最多 3 次重试；
    - 计算我方与闲鱼的性价比比值（或回退大模型判断）并组装完整入库文档。
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

    # 若带具体规格数字的主搜索词返回结果过少（< 3条），自动用「品牌 + 分类」兜底词重搜
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
        return _build_final_record(candidate, kw, search_res, [], {}, "no_xianyu_search_results")

    # 为前排低价闲鱼商品补充完整详情描述 desc 与多规格表 sku_list，帮助大模型准确识别真实规格与价格
    cheapest_items = sorted(
        [x for x in raw_items if _num(x.get("price")) > 0],
        key=lambda x: _num(x.get("price"), 999999.0),
    )[:max_detail_checks]

    detail_map: Dict[str, Dict[str, Any]] = {}
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

    # 构造提供给大模型的闲鱼商品列表（合并概览与详情）
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
        "unit_cost_performance_formula": "unit_cost_performance[unit] = unit_quantities[unit] / activity_price （即每1元人民币能买到的该unit数量，数值越大代表性价比越高）",
    }

    base_prompt = (
        "你是一位严谨的商品比价与性价比计算专家。我将为你提供【我方候选商品信息（含已计算好的各个unit的性价比值）】与【查询到的闲鱼商品列表】。\n\n"
        f"### 一、 我方候选商品信息\n```json\n{json.dumps(my_candidate_for_llm, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### 二、 查询到的闲鱼商品列表（共 {len(xianyu_items_for_llm)} 条）\n```json\n{json.dumps(xianyu_items_for_llm, ensure_ascii=False, indent=2)}\n```\n\n"
        "### 三、 你的任务与计算规则\n"
        "1. **筛选可比商品**：\n"
        "   - 从上述闲鱼商品列表中，找出与我方候选商品**具有可比性**的全新同品类/同款商品；\n"
        "   - 严格剔除完全没有可比性的商品（如：不同品类、配件/空瓶、临期破损瑕疵品、过期帖、仅限自提不邮寄，或外显低价实为非同款引流）；\n"
        "   - 若某闲鱼商品附带了 `detail_verified_info`（含正文 `desc`、运费 `transport_fee`、多规格表 `sku_list`），请务必以其中匹配的真实规格价格 + 运费作为该闲鱼商品的真实价格。\n"
        "2. **选出闲鱼中性价比最高的 1 个商品**：\n"
        "   - 在所有可比的闲鱼商品中，选出**性价比最高**的那 1 个闲鱼商品，返回其商品编号 `best_xianyu_item_id`（**必须严格来自上述闲鱼列表中的 `item_id`，严禁编造**）及真实可比价格 `best_xianyu_price`。\n"
        "3. **计算并返回匹配的各个 `unit` 的性价比值 (`xianyu_unit_cost_performance`)**：\n"
        f"   - 我方候选商品已提供如下 `unit` 的性价比值：`{json.dumps(candidate['unit_cost_performance'], ensure_ascii=False)}`（计算公式：`性价比值 = 该unit总数量 / 商品价格`，即每1元能买到多少个该unit，值越大性价比越高）；\n"
        f"   - 请尽可能将选出的最佳闲鱼商品的规格换算为与我方相同的 `unit`（即优先对齐 `{list(candidate['unit_cost_performance'].keys())}`，例如 1L=1000ml，1kg=2斤=1000g），填入 `xianyu_unit_quantities`；\n"
        "   - 再按完全相同的公式 `xianyu_unit_quantities[unit] / best_xianyu_price`（保留6位小数）计算出 `xianyu_unit_cost_performance`。\n"
        "4. **返回你的判断字段 (`llm_winner` 与 `llm_reason`)**：\n"
        "   - 无论是否能提取出相同的 `unit`，都请给出你的判断结果 `llm_winner`：\n"
        "     * 若我方候选商品性价比高于最佳闲鱼商品，填 `\"my_product\"`；\n"
        "     * 若最佳闲鱼商品性价比高于我方候选商品，填 `\"xianyu_product\"`；\n"
        "     * 若双方性价比基本持平，填 `\"tie\"`；\n"
        "     * 若闲鱼列表中没有任何可比商品，将 `has_comparable_item` 设为 `false`，`llm_winner` 填 `\"no_comparable\"`。\n\n"
        "请严格返回如下 JSON 对象格式（不要输出任何额外解释文字）：\n"
        "```json\n"
        "{\n"
        "  \"has_comparable_item\": true,\n"
        "  \"comparable_item_ids\": [\"可比的闲鱼item_id列表\"],\n"
        "  \"best_xianyu_item_id\": \"闲鱼中性价比最高的那个商品的item_id\",\n"
        "  \"best_xianyu_title\": \"该最佳闲鱼商品的标题\",\n"
        "  \"best_xianyu_price\": 35.2,\n"
        "  \"best_xianyu_spec\": \"该最佳闲鱼商品的真实规格描述\",\n"
        "  \"xianyu_unit_quantities\": {\n"
        "    \"与我方匹配的unit名\": 1000.0\n"
        "  },\n"
        "  \"xianyu_unit_cost_performance\": {\n"
        "    \"与我方匹配的unit名\": 28.409091\n"
        "  },\n"
        "  \"llm_winner\": \"my_product 或 xianyu_product 或 tie 或 no_comparable\",\n"
        "  \"llm_reason\": \"详细说明：剔除了哪些不可比/假低价商品、为何选该闲鱼商品作为最佳基准、其真实价格与unit性价比如何计算、以及双方谁性价比更高\"\n"
        "}\n"
        "```"
    )

    # 大模型调用与关键数据强校验（最多 3 次机会）
    validated_llm_data: Dict[str, Any] = {}
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
            max_retries_per_model=1,
            timeout=75,
        )
        elapsed = time.monotonic() - t0
        content = str(res.get("content") or "").strip()
        model_used = (res.get("metrics") or {}).get("model_used") or "unknown"

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

        clean_dict["_model_used"] = model_used
        clean_dict["_attempt_used"] = attempt
        validated_llm_data = clean_dict
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
        fallback_error=last_error_msg if not validated_llm_data else "",
    )


def _build_final_record(
    candidate: Dict[str, Any],
    used_keyword: str,
    search_res: Dict[str, Any],
    xianyu_items_for_llm: List[Dict[str, Any]],
    llm_data: Dict[str, Any],
    fallback_error: str = "",
) -> Dict[str, Any]:
    """
    步骤 4：
    计算我方候选商品与最佳闲鱼商品的 `unit` 性价比比值 (`my_unit_cp / xianyu_unit_cp`)；
    若无相同 `unit` 的性价比值，则退而求其次采用大模型返回的判断字段 `llm_winner` 作为比较结果；
    组装完整可复盘结构入库。
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
        # 优先使用主单位的比值作为核心分数
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

    # 从原始搜索列表中找回最佳闲鱼商品的完整卡片信息以便留存
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
        "model_used": llm_data.get("_model_used"),
        "attempt_used": llm_data.get("_attempt_used"),
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
        "xianyu_audit_trail": {
            "search_keyword": used_keyword,
            "search_ok": bool(search_res.get("ok")),
            "raw_search_count": len(xianyu_items_for_llm),
            "comparable_item_ids": llm_data.get("comparable_item_ids") or [],
            "xianyu_items_fed_to_llm": xianyu_items_for_llm,
        },
    }


# ==============================================================================
# 主流程编排与运行入口
# ==============================================================================
def run_discovery_cycle(
    hours_window: int = 72,
    max_categories: int = 5,
    max_search_items: int = 20,
    max_detail_checks: int = 4,
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

    records: List[Dict[str, Any]] = []
    for idx, cand in enumerate(champions, 1):
        logger.info(
            f"\n--- ▶️ [正在比价第 {idx}/{len(champions)} 个分类冠军] "
            f"分类=【{cand['category_name']}】 | {cand['name'][:42]} ---"
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

        cr = record["comparison_result"]
        bxy = record["best_xianyu_product"]
        logger.info(
            f"🏁 [分类比价结论 #{idx}] 分类=【{record['category_name']}】 | "
            f"结论={'✅我方更优' if record['is_my_product_better'] else '❌闲鱼更优/无优势'} (final_winner={record['final_winner']}) | "
            f"依据={cr['comparison_basis']} | 核心比值(我方/闲鱼)={cr['cost_performance_ratio']} (各unit比值={cr['unit_cp_ratios']}) | "
            f"大模型判断={cr['llm_winner']} | 我方价=¥{record['activity_price']}({cr['my_unit_cost_performance']}) vs "
            f"闲鱼最佳ID={bxy['item_id']}(¥{bxy['comparable_price']}, {cr['xianyu_unit_cost_performance']}) | "
            f"理由={cr['llm_reason']}"
        )
        if idx < len(champions):
            time.sleep(2.0)

    elapsed = time.monotonic() - t0
    better_cnt = sum(1 for r in records if r.get("is_my_product_better"))
    logger.info(
        f"\n{'=' * 92}\n"
        f"✅ [闲鱼高性价比探测任务/完成] 批次={batch_id} | 总耗时={elapsed:.1f}s | "
        f"评估分类数={len(records)} | 我方性价比高于闲鱼数={better_cnt}\n"
        f"{'=' * 92}"
    )
    for r in records:
        cr = r["comparison_result"]
        bxy = r["best_xianyu_product"]
        tag = "✅我方胜" if r["is_my_product_better"] else "❌闲鱼胜"
        logger.info(
            f"   📌 [{tag}] 分类=【{r['category_name']}】 | 比值={str(cr['cost_performance_ratio']):<8} "
            f"(依据={cr['comparison_basis']}, LLM判断={cr['llm_winner']}) | "
            f"我方¥{r['activity_price']} vs 闲鱼最佳({bxy['item_id']})¥{bxy['comparable_price']} | {r['name'][:34]}"
        )
    return records


def main():
    parser = argparse.ArgumentParser(description="闲鱼高性价比商品探测与比价定时任务")
    parser.add_argument("--hours-window", type=int, default=72, help="内部商品库扫描时间窗口（默认72小时）")
    parser.add_argument("--max-categories", "--max-candidates", dest="max_categories", type=int, default=5, help="单轮探测分类数（默认5）")
    parser.add_argument("--max-search-items", type=int, default=20, help="每次闲鱼搜索抓取的商品数（默认20）")
    parser.add_argument("--max-detail-checks", type=int, default=4, help="每次自动补充详情与SKU的前排低价商品数（默认4）")
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
