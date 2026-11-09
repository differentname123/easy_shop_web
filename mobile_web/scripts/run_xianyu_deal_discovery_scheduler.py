# -*- coding: utf-8 -*-
"""
闲鱼高性价比商品探测与比价独立定时任务（极简 AI 驱动版）
================================================================================
核心设计原则（第一性原理：确定性代码只管搬运数据，主观选品/比价/定价全权交由 AI）：
  1. 【步骤 1：代码均衡粗筛 + AI-1 全局选品官（1 次 AI 调用）】
     - 代码从 MongoDB `products` 读取 72h 内更新、冷却期内未评估过的有效商品，按分类均衡抽取高折扣/好价线报商品组成候选池；
     - 由 AI-1 凭商业常识直接挑出最适合闲鱼代拍转卖的 Top N 候选商品，并输出闲鱼精准搜索词 `xianyu_search_keyword` 与规格锚点 `my_spec_anchor`。
  2. 【步骤 2：代码一站式采集证据 + AI-2 闲鱼全案比价审计官（每个候选 1 次 AI 调用）】
     - ① 调官方 `get_goods_detail` 实时核验我方最新到手价与扣除佣金后的净成本 `my_net_cost`；
     - ② 调 `search_xianyu_items` 免登录获取闲鱼前 20 条搜索概览；
     - ③ 直接取列表中价格最低的前 K 条（外加想要人数最高的 1 条）调 `get_xianyu_item_detail` 获取完整正文 `desc`、运费与多规格 `sku_list`；
     - ④ 将【我方商品底牌 + 闲鱼 20 条搜索概览 + 重点低价商品完整详情与 SKU 表】一次性交给 AI-2，
        由 AI-2 直接完成：识破引流假低价、对齐同款规格真实到手价、判定闲鱼最低基准价与主流价、给出 0~100 吸引力打分、评级与建议挂牌价。
  3. 【步骤 3：全链路可追溯落库 MongoDB `xianyu_deal_evaluations`】
     - 完整保留我方候选快照、AI 比价结论、确认可比的闲鱼基准清单、被剔除的低价引流陷阱说明，以及原始闲鱼搜索与详情证据。
================================================================================
"""

import argparse
import datetime
import json
import logging
import os
import sys
import time
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

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

EVAL_COLLECTION_NAME = "xianyu_deal_evaluations"
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "xianyu_deal_discovery.log")


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
        return float(val)
    except Exception:
        return default


def _ask_ai_json(prompt: str, tag: str, timeout: int = 75) -> Optional[Any]:
    """统一调用大模型并解析返回的 JSON，单模型超时快速切换备用模型。"""
    t0 = time.monotonic()
    res = generate_content(
        prompt=prompt,
        preset_model_group="medium",
        max_retries_per_model=1,
        timeout=timeout,
    )
    elapsed = time.monotonic() - t0
    content = str(res.get("content") or "").strip()
    model_used = (res.get("metrics") or {}).get("model_used") or "unknown"
    if "成功" not in str(res.get("status") or "") or not content:
        logger.warning(f"⚠️ [AI调用失败/{tag}] 模型={model_used} | 耗时={elapsed:.1f}s | 错误={res.get('error_history')}")
        return None
    try:
        parsed = string_to_object(content)
    except Exception as exc:
        logger.warning(f"⚠️ [AI解析JSON异常/{tag}] 模型={model_used} | 耗时={elapsed:.1f}s | 异常={exc}")
        return None
    logger.info(f"🤖 [AI调用成功/{tag}] 模型={model_used} | 耗时={elapsed:.1f}s")
    return parsed


# ==============================================================================
# 步骤 1：72h 商品库跨品类粗筛 + AI-1 全局选品官
# ==============================================================================
def _summarize_product_doc(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 MongoDB `products` 文档提取紧凑的客观摘要（零正则假设）。"""
    price = _num(doc.get("activity_price") or doc.get("price"))
    if price <= 5.0:  # 过滤 ≤5 元的极低客单价引流小物件（无代拍差价空间）
        return None
    orig_price = max(price, _num(doc.get("original_price"), price))
    coupon = _num(doc.get("coupon_amount") or doc.get("saved_price"))
    commission = _num(doc.get("commission_amount"))
    net_cost = round(max(0.01, price - commission), 2)

    # 提取已有 AI 结构化标签与规格（若有）
    fi = doc.get("format_info") if isinstance(doc.get("format_info"), dict) else {}
    first_sku = next((v for v in fi.values() if isinstance(v, dict)), {})
    ces = first_sku.get("core_entities") or []
    sub_cat = str(ces[0].get("name") if (ces and isinstance(ces[0], dict)) else "").strip()
    category = sub_cat or str(doc.get("category") or "综合").strip()

    kws = [
        f"{kw.get('attribute_name')}:{kw.get('attribute_value')}"
        for kw in (first_sku.get("decision_keywords") or [])
        if isinstance(kw, dict) and kw.get("attribute_value")
    ]
    pb = first_sku.get("pricing_basis") if isinstance(first_sku.get("pricing_basis"), dict) else {}
    spec_hint = str(pb.get("equivalent_description") or "").strip()
    if not spec_hint and pb.get("total_value") and pb.get("base_unit"):
        spec_hint = f"{pb.get('total_value')}{pb.get('base_unit')}"

    deal_info = doc.get("deal_info") if isinstance(doc.get("deal_info"), dict) else {}
    deal_price = _num(deal_info.get("deal_price"))
    deal_score = _num(deal_info.get("quality_score"))

    # 客观优惠吸引力度指标（用于同分类内部初筛排序，把最有潜力的喂给 AI-1）
    discount_saved = max(coupon, round(orig_price - price, 2), round(price - deal_price, 2) if deal_price > 0 else 0.0)
    discount_rate = round(discount_saved / orig_price, 3) if orig_price > 0 else 0.0

    return {
        "product_id": str(doc.get("product_id") or "").strip(),
        "goods_sign": str(doc.get("goods_sign") or "").strip(),
        "platform": normalize_platform(doc.get("platform") or "pdd"),
        "category_name": category,
        "brand": str(doc.get("brand") or "").strip(),
        "name": str(doc.get("name") or "").strip(),
        "attributes_summary": " | ".join(kws[:6]),
        "spec_hint": spec_hint,
        "db_price": round(price, 2),
        "db_original_price": round(orig_price, 2),
        "db_coupon_amount": round(coupon, 2),
        "db_commission_amount": round(commission, 2),
        "db_net_cost": net_cost,
        "deal_price": round(deal_price, 2) if deal_price > 0 else None,
        "deal_quality_score": deal_score,
        "discount_rate": discount_rate,
        "sales_tip": str(doc.get("sales_tip") or "0"),
        "product_url": str(doc.get("product_url") or ""),
        "image_url": str(doc.get("image_url") or ""),
        "updated_at": str(doc.get("updated_at") or ""),
    }


def select_candidates_by_ai(
    db: Any,
    hours_window: int = 72,
    max_candidates: int = 5,
    cooldown_hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    步骤 1：从 72h 商品库跨分类均衡采样约 40 个高折扣候选，交由 AI-1 一次性选出 Top N 最具闲鱼转卖潜力的商品。
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_dt = now_utc - datetime.timedelta(hours=hours_window)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 读取冷却期内已评估过的商品，避免重复探测
    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    cooldown_str = (now_utc - datetime.timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S")
    evaluated_keys = {
        (d.get("platform"), str(d.get("product_id")))
        for d in eval_col.find({"evaluated_at": {"$gte": cooldown_str}}, {"platform": 1, "product_id": 1})
    }

    p_col = db.get_collection("products")
    raw_docs = list(
        p_col.find(
            {
                "price": {"$gt": 5},
                "$or": [
                    {"updated_at": {"$gte": cutoff_dt.replace(tzinfo=None)}},
                    {"updated_at": {"$gte": cutoff_str}},
                ],
            }
        )
        .sort("updated_at", -1)
        .limit(2500)
    )

    by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for doc in raw_docs:
        item = _summarize_product_doc(doc)
        if not item or not item["product_id"] or not item["name"]:
            continue
        if (item["platform"], item["product_id"]) in evaluated_keys:
            continue
        by_cat[item["category_name"]].append(item)

    # 跨分类均衡采样：按类别内最高优惠力度对类别排序，每类最多取前 2 个，汇总约 40 个精简池喂给 AI-1
    ranked_cats = sorted(
        by_cat.items(),
        key=lambda kv: max((x["deal_quality_score"] * 0.01 + x["discount_rate"]) for x in kv[1]),
        reverse=True,
    )
    shortlist: List[Dict[str, Any]] = []
    for _, items in ranked_cats:
        items.sort(key=lambda x: (x["deal_quality_score"], x["discount_rate"], x["db_coupon_amount"]), reverse=True)
        shortlist.extend(items[:2])
        if len(shortlist) >= 40:
            break

    logger.info(
        f"📊 [步骤1/候选池采样] 72h内扫描商品={len(raw_docs)} | 过滤已评={len(evaluated_keys)} | "
        f"覆盖细分品类={len(by_cat)} | 送入AI-1精选池={len(shortlist)}条"
    )
    if not shortlist:
        return []

    ai_pool = [
        {
            "product_id": x["product_id"],
            "platform": x["platform"],
            "category": x["category_name"],
            "brand": x["brand"],
            "name": x["name"],
            "attributes": x["attributes_summary"],
            "spec_hint": x["spec_hint"],
            "original_price": x["db_original_price"],
            "current_price": x["db_price"],
            "coupon": x["db_coupon_amount"],
            "my_net_cost": x["db_net_cost"],
            "deal_price": x["deal_price"],
            "sales": x["sales_tip"],
        }
        for x in shortlist
    ]

    prompt_ai1 = (
        f"你是一位精通闲鱼无货源代拍套利的资深选品总监。请从以下 {len(ai_pool)} 款最近72小时内更新的内部高折扣商品池中，"
        f"挑选出 **最值得去闲鱼比价上架的前 {max_candidates} 款商品**（尽量覆盖不同品类）。\n\n"
        "【选品标准】：\n"
        "1. **硬通货品牌标品优先**：知名品牌、型号/规格清晰明确（如整箱饮料/纸巾、品牌个护家清、数码小家电、大牌日用等），买家在闲鱼搜词精准、无需选尺码且不易售后扯皮；\n"
        "2. **真实低成本优势**：当前 `my_net_cost`（或 `deal_price`）相比日常价 `original_price` 有显著折扣（如大额券、百亿补贴或高分好价），在闲鱼有充足的差价空间（建议优先选净成本 ≥ 12 元的商品，确保单笔绝对利润可观）；\n"
        "3. **生成精准闲鱼搜索词**：提取真实买家在闲鱼搜索该同款商品时最常用的自然精简关键词 `xianyu_search_keyword`（3~4个词，格式：`品牌 + 核心品名 + 关键规格/数量`，绝不要带电商促销废话）。\n\n"
        f"【候选商品池 JSON】：\n{json.dumps(ai_pool, ensure_ascii=False, indent=2)}\n\n"
        "请严格返回如下 JSON 数组格式（不要输出任何额外解释）：\n"
        "```json\n"
        "[\n"
        "  {\n"
        "    \"product_id\": \"入选商品的product_id\",\n"
        "    \"xianyu_search_keyword\": \"品牌 核心品名 关键规格\",\n"
        "    \"my_spec_anchor\": \"我方商品的清晰规格基准（如：120抽*20包整箱 / 500ml*24瓶）\",\n"
        "    \"select_reason\": \"简述为何该商品及价格在闲鱼极具代拍套利潜力\"\n"
        "  }\n"
        "]\n"
        "```"
    )

    ai_picks = _ask_ai_json(prompt_ai1, tag="AI1_全局选品与搜索词生成")
    if not isinstance(ai_picks, list):
        return []

    item_map = {x["product_id"]: x for x in shortlist}
    selected: List[Dict[str, Any]] = []
    for pick in ai_picks:
        if not isinstance(pick, dict):
            continue
        pid = str(pick.get("product_id") or "").strip()
        kw = str(pick.get("xianyu_search_keyword") or "").strip()
        if pid in item_map and kw:
            cand = dict(item_map[pid])
            cand["xianyu_search_keyword"] = kw
            cand["my_spec_summary"] = str(pick.get("my_spec_anchor") or cand["spec_hint"] or "").strip()
            cand["ai_select_reason"] = str(pick.get("select_reason") or "").strip()
            selected.append(cand)
            logger.info(
                f"🎯 [步骤1/AI入选 #{len(selected)}] 类别=【{cand['category_name']}】 | "
                f"净成本=¥{cand['db_net_cost']} (现价=¥{cand['db_price']}, 原价=¥{cand['db_original_price']}) | "
                f"搜索词=【{kw}】 | 规格=【{cand['my_spec_summary']}】 | 理由={cand['ai_select_reason']}"
            )
            if len(selected) >= max_candidates:
                break
    return selected


# ==============================================================================
# 步骤 2：官方接口实时验价 + 闲鱼搜索与前排详情采集 + AI-2 一锤定音比价审计
# ==============================================================================
def verify_official_live_price(cand: Dict[str, Any]) -> Dict[str, Any]:
    """调用官方平台 Adapter 核验实时价格与佣金净成本。"""
    cand = dict(cand)
    plat = cand["platform"]
    pid = cand["product_id"]
    gsign = cand.get("goods_sign") or ""
    cand["my_compare_price"] = cand["db_price"]
    cand["my_net_cost"] = cand["db_net_cost"]
    cand["official_live_verified"] = False

    try:
        adapter = get_adapter(plat)
        res = adapter.get_goods_detail(goods_id_or_sign=gsign or pid, goods_id=pid, goods_sign=gsign or None)
        if isinstance(res, dict) and res.get("status") == "success" and isinstance(res.get("data"), dict):
            d = res["data"]
            live_p = _num(d.get("activity_price") or d.get("price"), cand["db_price"])
            live_comm = _num(d.get("commission_amount"), cand["db_commission_amount"])
            if live_p > 0:
                cand["my_compare_price"] = round(live_p, 2)
                cand["my_net_cost"] = round(max(0.01, live_p - live_comm), 2)
                cand["official_live_verified"] = True
                logger.info(
                    f"🔄 [步骤2/官方实时验价] [{plat.upper()}:{pid}] 入库价=¥{cand['db_price']} -> "
                    f"实时价=¥{cand['my_compare_price']} (实时净成本=¥{cand['my_net_cost']})"
                )
                return cand
    except Exception as exc:
        logger.warning(f"⚠️ [步骤2/官方验价降级] [{plat.upper()}:{pid}] 沿用72h入库价(¥{cand['db_net_cost']}) | 原因={exc}")

    logger.info(f"ℹ️ [步骤2/官方验价] [{plat.upper()}:{pid}] 沿用72h入库价=¥{cand['my_compare_price']} (净成本=¥{cand['my_net_cost']})")
    return cand


def evaluate_candidate_with_xianyu_ai(
    cand: Dict[str, Any],
    guest_user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    max_search_items: int = 20,
    max_detail_checks: int = 4,
    debug_browser: bool = False,
) -> Dict[str, Any]:
    """
    对单个候选商品：
    1. 免登录搜闲鱼拿前 20 条概览；
    2. 自动对价格最低的前 K 条（外加想要人数最多的 1 条）抓取完整详情与 `sku_list`；
    3. 一次性交由 AI-2 完成真假低价鉴别、同款基准锁定、0~100 吸引力评分与建议定价。
    """
    kw = cand["xianyu_search_keyword"]
    my_cost = cand["my_net_cost"]
    logger.info(
        f"🔍 [步骤2/闲鱼采集] 候选=【{cand['name'][:30]}】 | 我方净成本=¥{my_cost} | 搜索词=【{kw}】"
    )

    search_res = search_xianyu_items(
        keyword=kw,
        max_items=max_search_items,
        user_data_dir=guest_user_data_dir,
        debug=debug_browser,
    )
    raw_items = search_res.get("items") or []
    if not search_res.get("ok") or not raw_items:
        logger.warning(f"⚠️ [闲鱼搜索无结果] 搜索词=【{kw}】 | msg={search_res.get('message')}")
        return _pack_evaluation_doc(cand, search_res, [], {})

    # 挑选最关键的闲鱼商品抓取完整详情与多规格表（过滤 < 我方净成本 25% 的 1 元跑腿/空盒引流项，取最便宜的前 K 条 + 最热 1 条）
    valid_price_items = [x for x in raw_items if _num(x.get("price")) >= max(2.0, my_cost * 0.25)]
    if not valid_price_items:
        valid_price_items = [x for x in raw_items if _num(x.get("price")) > 0]
    cheapest_targets = sorted(valid_price_items, key=lambda x: _num(x.get("price"), 999999.0))[:max_detail_checks]

    # 补充想要人数最多的 1 条（方便 AI 了解爆款同款卖家的真实规格与价格）
    hottest = max(valid_price_items, key=lambda x: int(x.get("want_cnt") or 0), default=None)
    if hottest and all(x["item_id"] != hottest["item_id"] for x in cheapest_targets):
        cheapest_targets.append(hottest)

    detail_records: List[Dict[str, Any]] = []
    for idx, t_item in enumerate(cheapest_targets, 1):
        if idx > 1:
            time.sleep(1.5)
        det = get_xianyu_item_detail(
            item_id_or_url=t_item["item_id"],
            user_data_dir=guest_user_data_dir,
            debug=debug_browser,
        )
        if det.get("ok"):
            detail_records.append({
                "item_id": det.get("item_id"),
                "item_url": det.get("item_url"),
                "title": det.get("title"),
                "desc": det.get("desc"),
                "display_price": det.get("price"),
                "transport_fee": det.get("transport_fee"),
                "is_free_shipping": det.get("is_free_shipping"),
                "min_price": det.get("min_price"),
                "max_price": det.get("max_price"),
                "has_multi_sku": det.get("has_multi_sku"),
                "sku_list": (det.get("sku_list") or [])[:15],
                "want_cnt": det.get("want_cnt"),
                "browse_cnt": det.get("browse_cnt"),
            })

    # 组装一次性喂给 AI-2 的全景证据包
    my_card = {
        "name": cand["name"],
        "brand": cand["brand"],
        "category": cand["category_name"],
        "my_spec_anchor": cand["my_spec_summary"],
        "attributes": cand["attributes_summary"],
        "my_live_price": cand["my_compare_price"],
        "my_net_cost": cand["my_net_cost"],
        "deal_price": cand.get("deal_price"),
    }
    overview_compact = [
        {
            "item_id": x["item_id"],
            "title": x["title"],
            "display_price": x["price"],
            "want_cnt": x["want_cnt"],
            "is_free_shipping": x["is_free_shipping"],
        }
        for x in raw_items
    ]

    prompt_ai2 = (
        "你是一位极其严谨的闲鱼比价审计官与定价专家。现在请将【我方候选商品】与【闲鱼搜索出的20条商品概览 + 前排重点低价商品的完整详情与SKU表】进行一次性全案比价与性价比评分。\n\n"
        f"### 一、 我方候选商品底牌\n```json\n{json.dumps(my_card, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### 二、 闲鱼搜索结果概览（共 {len(overview_compact)} 条）\n```json\n{json.dumps(overview_compact, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### 三、 闲鱼前排低价/高热度商品的完整详情与多规格 SKU 表（共 {len(detail_records)} 条）\n```json\n{json.dumps(detail_records, ensure_ascii=False, indent=2)}\n```\n\n"
        "### 四、 你的比价审计与打分准则\n"
        "1. **识破假低价与多规格引流（铁律）**：\n"
        "   - 闲鱼卖家经常在外显标出最小规格（如4瓶装、单包、空盒）的价格引流，而大规格整箱价格藏在 `sku_list` 或正文 `desc` 中；\n"
        "   - 请务必优先结合【第三部分：完整详情与SKU表】核实低价商品：若 `sku_list` 中有与我方规格（`my_spec_anchor`）一致的 SKU，必须采用该 SKU 的真实价格 + `transport_fee`；若某商品只是小规格明确标价（如6瓶卖7.9元，我方是24瓶），可按物理数量等比折算为与我方同规格的等效价并注明；\n"
        "   - 对于二手拆封、临期瑕疵、过期活动帖（如正文写“仅限某天”）、仅限自提不邮寄或完全不同品牌的商品，一律剔除并记录到 `rejected_low_price_traps` 中。\n"
        "2. **锁定真实市场基准价**：\n"
        "   - `xianyu_lowest_comparable_price`：经核实或等比折算后，闲鱼上**真正可买到的同款同规格最低到手价**；\n"
        "   - `xianyu_mainstream_price`：闲鱼上同款同规格的**主流/中位成交价**。\n"
        "3. **直接由你给出综合性价比得分 `total_score` (0~100分) 与评级 `grade`**：\n"
        "   - 请综合评估：① 我方净成本 `my_net_cost` 相比 `xianyu_lowest_comparable_price` 和 `xianyu_mainstream_price` 的真实利润空间（绝对差价元 & 折扣比例）；② 闲鱼同款的想要人数（`want_cnt`）反映的买家需求热度；\n"
        "   - 若我方净成本比闲鱼最低真同款还要贵，或差价不足 2~3 元几乎无利可图，`total_score` 应给 0~15 分（`C_无价格优势`）；\n"
        "   - 若有 3~6 元稳定小利（5%~15% 优势），给 20~49 分（`B_微利可做`）；\n"
        "   - 若有显著价格优势（比闲鱼最低同款便宜 15%+ 且单笔利润可观）且有一定想要人数，给 50~74 分（`A_有明显优势`）；\n"
        "   - 若我方净成本远低于闲鱼最低同款（便宜 25%+ 或单笔利润极高）且闲鱼需求旺盛，给 75~100 分（`S_极高性价比`）。\n\n"
        "请严格输出如下 JSON 对象（不要输出任何额外文字）：\n"
        "```json\n"
        "{\n"
        "  \"has_comparable_items\": true,\n"
        "  \"xianyu_lowest_comparable_price\": 24.99,\n"
        "  \"xianyu_mainstream_price\": 28.0,\n"
        "  \"xianyu_top_want_sum\": 58,\n"
        "  \"price_gap_yuan\": 5.61,\n"
        "  \"advantage_ratio\": 0.2245,\n"
        "  \"total_score\": 68.0,\n"
        "  \"grade\": \"S_极高性价比 或 A_有明显优势 或 B_微利可做 或 C_无价格优势\",\n"
        "  \"suggested_xianyu_price\": 23.5,\n"
        "  \"estimated_unit_profit\": 4.12,\n"
        "  \"audit_summary\": \"用一段清晰的话总结：我方净成本多少、闲鱼最低真同款是谁/卖多少、主流价多少、剔除了哪些假低价、为何给该分数\",\n"
        "  \"comparable_benchmarks\": [\n"
        "    {\n"
        "      \"item_id\": \"闲鱼商品ID\",\n"
        "      \"title\": \"商品标题简述\",\n"
        "      \"display_price\": 24.99,\n"
        "      \"true_comparable_price\": 24.99,\n"
        "      \"want_cnt\": 53,\n"
        "      \"matched_spec\": \"对齐的规格说明\",\n"
        "      \"verify_note\": \"详情/SKU核验或等比折算说明\"\n"
        "    }\n"
        "  ],\n"
        "  \"rejected_low_price_traps\": [\n"
        "    {\n"
        "      \"item_id\": \"被剔除的低价商品ID\",\n"
        "      \"display_price\": 22.22,\n"
        "      \"reject_reason\": \"为什么这个低价不能当基准（如：实为4瓶小规格SKU，12瓶SKU实售87.77元 / 过期帖 / 异品）\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "```"
    )

    ai_eval = _ask_ai_json(prompt_ai2, tag=f"AI2_闲鱼全案比价审计({kw})")
    if not isinstance(ai_eval, dict):
        ai_eval = {}

    return _pack_evaluation_doc(cand, search_res, detail_records, ai_eval)


def _pack_evaluation_doc(
    cand: Dict[str, Any],
    search_res: Dict[str, Any],
    detail_records: List[Dict[str, Any]],
    ai_eval: Dict[str, Any],
) -> Dict[str, Any]:
    """将候选商品、AI-2 全案审计结论与闲鱼原始证据组装为标准可追溯文档。"""
    my_cost = cand["my_net_cost"]
    my_price = cand["my_compare_price"]
    benchmarks = ai_eval.get("comparable_benchmarks") if isinstance(ai_eval.get("comparable_benchmarks"), list) else []
    rejected = ai_eval.get("rejected_low_price_traps") if isinstance(ai_eval.get("rejected_low_price_traps"), list) else []

    lowest_p = round(_num(ai_eval.get("xianyu_lowest_comparable_price")), 2)
    mainstream_p = round(_num(ai_eval.get("xianyu_mainstream_price"), lowest_p), 2)
    has_comp = bool(ai_eval.get("has_comparable_items")) and lowest_p > 0

    if has_comp:
        price_gap = round(lowest_p - my_cost, 2)
        adv_ratio = round(price_gap / lowest_p, 4) if lowest_p > 0 else 0.0
        total_score = round(_num(ai_eval.get("total_score")), 1)
        grade = str(ai_eval.get("grade") or ("A_有明显优势" if total_score >= 50 else "C_无价格优势")).strip()
        suggested_p = round(_num(ai_eval.get("suggested_xianyu_price"), max(my_cost + 2.0, lowest_p * 0.93)), 1)
        est_profit = round(suggested_p - my_cost, 2)
        summary_text = str(ai_eval.get("audit_summary") or "").strip()
    else:
        price_gap = 0.0
        adv_ratio = 0.0
        total_score = 0.0
        grade = "D_无同款基准"
        suggested_p = 0.0
        est_profit = 0.0
        summary_text = str(ai_eval.get("audit_summary") or f"闲鱼未找到与【{cand['xianyu_search_keyword']}】匹配的同款商品")

    # 输出关键可审查日志
    for trap in rejected[:3]:
        if isinstance(trap, dict):
            logger.info(
                f"   🚫 [AI识破低价陷阱] ID={trap.get('item_id')} | 外显价=¥{trap.get('display_price')} | "
                f"剔除原因={trap.get('reject_reason')}"
            )
    for bm in benchmarks[:3]:
        if isinstance(bm, dict):
            logger.info(
                f"   ✅ [AI锁定同款基准] ID={bm.get('item_id')} | 外显价=¥{bm.get('display_price')} -> "
                f"同规格真实价=¥{bm.get('true_comparable_price')} (想要={bm.get('want_cnt')}人) | 说明={bm.get('verify_note')}"
            )

    eval_result = {
        "has_comparable_xianyu_items": has_comp,
        "comparable_count": len(benchmarks),
        "xianyu_lowest_price": lowest_p,
        "xianyu_benchmark_price": lowest_p,
        "xianyu_mainstream_price": mainstream_p,
        "xianyu_benchmark_item_ids": [str(x.get("item_id")) for x in benchmarks[:3] if isinstance(x, dict) and x.get("item_id")],
        "xianyu_top5_want_sum": int(_num(ai_eval.get("xianyu_top_want_sum"))),
        "price_gap_yuan": price_gap,
        "advantage_ratio": adv_ratio,
        "total_score": total_score,
        "grade": grade,
        "suggested_xianyu_price": suggested_p,
        "estimated_unit_profit": est_profit,
        "summary_text": summary_text,
    }

    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "platform": cand["platform"],
        "product_id": cand["product_id"],
        "category_name": cand["category_name"],
        "brand": cand["brand"],
        "name": cand["name"],
        "evaluated_at": now_s,
        "updated_at": now_s,
        "total_score": total_score,
        "grade": grade,
        "candidate_product": cand,
        "evaluation_result": eval_result,
        "xianyu_audit_trail": {
            "search_keyword": cand["xianyu_search_keyword"],
            "search_ok": bool(search_res.get("ok")),
            "raw_search_count": len(search_res.get("items") or []),
            "comparable_benchmarks": benchmarks,
            "rejected_low_price_traps": rejected,
            "detail_checked_items": detail_records,
            "raw_search_overview": search_res.get("items") or [],
        },
    }


# ==============================================================================
# 步骤 3：主流程编排与定时运行入口
# ==============================================================================
def run_discovery_cycle(
    hours_window: int = 72,
    max_candidates: int = 5,
    max_search_items: int = 20,
    max_detail_checks: int = 4,
    cooldown_hours: int = 24,
    guest_user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug_browser: bool = False,
) -> List[Dict[str, Any]]:
    t0 = time.monotonic()
    batch_id = datetime.datetime.now().strftime("disc_%Y%m%d_%H%M%S")
    logger.info(
        f"\n{'=' * 88}\n"
        f"🚀 [闲鱼高性价比探测任务(极简AI版)/启动] 批次={batch_id} | 时间窗={hours_window}h | "
        f"目标探测数={max_candidates} | 访客目录=<{guest_user_data_dir}>\n"
        f"{'=' * 88}"
    )

    db = gen_db_object()
    eval_col = db.get_collection(EVAL_COLLECTION_NAME)
    eval_col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
    eval_col.create_index([("total_score", -1), ("evaluated_at", -1)], background=True)

    candidates = select_candidates_by_ai(
        db=db,
        hours_window=hours_window,
        max_candidates=max_candidates,
        cooldown_hours=cooldown_hours,
    )
    if not candidates:
        logger.warning("⚠️ [本轮结束] 未选出待评估候选商品。")
        return []

    records: List[Dict[str, Any]] = []
    for idx, cand in enumerate(candidates, 1):
        logger.info(f"\n--- ▶️ [正在比价第 {idx}/{len(candidates)} 个候选] 【{cand['category_name']}】{cand['name'][:40]} ---")
        live_cand = verify_official_live_price(cand)
        record = evaluate_candidate_with_xianyu_ai(
            cand=live_cand,
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
        ev = record["evaluation_result"]
        logger.info(
            f"🏁 [比价结论 #{idx}] 得分={ev['total_score']} [{ev['grade']}] | "
            f"我方净成本=¥{live_cand['my_net_cost']} vs 闲鱼最低真同款=¥{ev['xianyu_lowest_price']} "
            f"(主流价=¥{ev['xianyu_mainstream_price']}) | 差价=¥{ev['price_gap_yuan']:+.2f} ({ev['advantage_ratio']*100:+.1f}%) | "
            f"建议挂牌=¥{ev['suggested_xianyu_price']} (预估单利=¥{ev['estimated_unit_profit']}) | "
            f"摘要={ev['summary_text']}"
        )
        if idx < len(candidates):
            time.sleep(2.0)

    records.sort(key=lambda r: r.get("total_score", 0.0), reverse=True)
    elapsed = time.monotonic() - t0
    good_cnt = sum(1 for r in records if r.get("total_score", 0.0) >= 50.0)
    logger.info(
        f"\n{'=' * 88}\n"
        f"✅ [闲鱼高性价比探测任务/完成] 批次={batch_id} | 总耗时={elapsed:.1f}s | "
        f"评估商品数={len(records)} | 发现高性价比优品(>=50分)={good_cnt}\n"
        f"{'=' * 88}"
    )
    for r in records:
        ev = r["evaluation_result"]
        logger.info(
            f"   📌 [{ev['grade']}] 得分={ev['total_score']:<5} | 【{r['category_name']}】 "
            f"我方净成本=¥{r['candidate_product']['my_net_cost']:<6} vs 闲鱼底价=¥{ev['xianyu_lowest_price']:<6} | "
            f"建议挂牌=¥{ev['suggested_xianyu_price']:<6} (单利=¥{ev['estimated_unit_profit']}) | {r['name'][:36]}"
        )
    return records


def main():
    parser = argparse.ArgumentParser(description="闲鱼高性价比商品探测与比价定时任务（极简 AI 驱动版）")
    parser.add_argument("--hours-window", type=int, default=72, help="内部商品库扫描时间窗口（默认72小时）")
    parser.add_argument("--max-candidates", "--max-categories", dest="max_candidates", type=int, default=5, help="单轮探测商品数（默认5）")
    parser.add_argument("--max-search-items", type=int, default=20, help="每次闲鱼搜索抓取的商品概览条数（默认20）")
    parser.add_argument("--max-detail-checks", type=int, default=4, help="每次自动深查详情与SKU的低价商品数（默认4）")
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
                max_candidates=args.max_candidates,
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
