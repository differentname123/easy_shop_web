# -*- coding: utf-8 -*-
"""
商品与好价线报结构化规格清洗服务 (`services.goods_formatter`)。
================================================================================
- 按 SKU 粒度并发生成 `format_info.{sku_id}`（官方商品）与 `deal_info.format_info`（线报商品）；
- 提示词从 `prompt/` 目录热加载，JSON 解析复用 `infrastructure.ai_api.json_parser.parse_llm_json`；
- 分享文案与口令清理复用 `models.product.strip_ecommerce_tokens_and_urls`。
================================================================================
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Dict, Optional
from pymongo import UpdateOne

from infrastructure.ai_api.json_parser import parse_llm_json
from infrastructure.ai_api.model_api import generate_content
from infrastructure.runtime_io import load_prompt_cached
from models.product import strip_ecommerce_tokens_and_urls

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
PROMPT_FILE_PATH = MOBILE_WEB_DIR / "prompt" / "商品数据结构化清洗.txt"
DEAL_SUPPLEMENT_PROMPT_PATH = MOBILE_WEB_DIR / "prompt" / "商品数据结构化清洗_线报补充.txt"

LLM_MAX_RETRIES = 1
FORMAT_MAX_RETRIES = 3
LOCKED_RETRY_SENTINEL = 999
PRICE_COMPARE_ABS_TOL = 1e-4
FORMAT_WORKERS = 10
DEAL_FORMAT_WORKERS = 10
FORMAT_BATCH_SIZE = 10
DEAL_FORMAT_ROUND_LIMIT = 0
ROUND_INTERVAL_SECONDS = 60
RECENT_DAYS = 7
BSON_MAX_INT64 = 2 ** 63 - 1
COUNT_KEYS = ("success", "failed", "skipped")

_MULTI_SPACE_RE = re.compile(r"\s+")

logger = logging.getLogger("goods_format")


class ProductManager:
    """唯一身份为 `(platform, product_id)`，不填默认业务字段，也不替换整条商品。"""

    COLLECTION_NAME = "products"

    def __init__(self, db_instance):
        if db_instance is None:
            raise ValueError("必须提供有效的 MongoBase 实例")
        self._db = db_instance
        self._db.create_index(self.COLLECTION_NAME, [("platform", 1), ("product_id", 1)], unique=True)
        self._db.create_index(self.COLLECTION_NAME, [("updated_at", -1)])
        self._db.create_index(self.COLLECTION_NAME, [("platform", 1), ("updated_at", -1)])
        self._db.create_index(self.COLLECTION_NAME, [("deal_info.is_deal_tip", 1), ("updated_at", -1)])

    def query(self, query=None, projection=None, sort=None, limit=0):
        """入参沿用 Mongo 条件/投影/排序形貌；返回 `list[dict]`。"""
        return self._db.find_many(
            self.COLLECTION_NAME, query=query, projection=projection, sort=sort, limit=limit,
        )

    @staticmethod
    def _prepare_updates(records, condition, increments, upsert):
        """把补丁完整校验为 `[(身份条件, 更新字典)]`，确保非法批次在任何数据库写入前失败。"""
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, (list, tuple)):
            raise TypeError("records 必须是商品字典或商品字典列表")
        if type(upsert) is not bool:
            raise TypeError("upsert 必须是布尔值")
        if condition is not None and not isinstance(condition, dict):
            raise TypeError("condition 必须是 Mongo 查询字典")
        if condition and upsert:
            raise ValueError("带附加条件的更新必须设置 upsert=False，避免条件不符时误插入")
        if increments is not None and not isinstance(increments, dict):
            raise TypeError("increments 必须是 {字段: 增量} 字典")
        increments = {} if increments is None else increments.copy()
        for field, value in increments.items():
            if not isinstance(field, str) or field.split(".")[0] in {"_id", "platform", "product_id"}:
                raise ValueError("不能递增商品身份字段，增量字段名必须是字符串")
            valid_int = type(value) is int and -(2 ** 63) <= value < 2 ** 63
            valid_float = type(value) is float and math.isfinite(value)
            if not (valid_int or valid_float):
                raise ValueError(f"增量必须是可存储的有限数值: {field}")

        batch = {}
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("每个商品补丁必须是字典")
            item = record.copy()
            item.pop("_id", None)
            platform, product_id = item.get("platform"), item.get("product_id")
            if not isinstance(platform, str) or not platform.strip():
                raise ValueError("platform 必须是非空字符串")
            if type(product_id) not in (str, int) or not str(product_id).strip():
                raise ValueError("product_id 必须是非空字符串或整数，不能是布尔值")
            item.update(platform=platform.strip().lower(), product_id=str(product_id).strip())
            identity = (item["platform"], item["product_id"])
            batch.setdefault(identity, {}).update(item)

        prepared = []
        for (platform, product_id), fields in batch.items():
            paths = list(fields) + list(increments)
            if any(
                not isinstance(path, str)
                or "\x00" in path
                or any(not part or part.startswith("$") for part in path.split("."))
                for path in paths
            ):
                raise ValueError("更新字段名不能含空路径、空字符或以 $ 开头的路径片段")
            if any(path.split(".")[0] in {"_id", "platform", "product_id"} and "." in path for path in paths):
                raise ValueError("不能更新商品身份的子字段")
            if len(set(paths)) != len(paths):
                raise ValueError("同一字段不能同时赋值和递增")
            path_set = set(paths)
            if any(
                ".".join(path.split(".")[:index]) in path_set
                for path in paths
                for index in range(1, len(path.split(".")))
            ):
                raise ValueError("同一次更新不能同时提交父字段和它的子字段")
            identity = {"platform": platform, "product_id": product_id}
            query = {"$and": [identity, condition]} if condition else identity
            update = {"$set": fields}
            if increments:
                update["$inc"] = increments
            prepared.append((query, update))
        return prepared

    def update(self, records, *, condition=None, increments=None, upsert=True):
        """增量更新商品字段；返回 `{new, update, modified}`。"""
        prepared = self._prepare_updates(records, condition, increments, upsert)
        if not prepared:
            return {"new": 0, "update": 0, "modified": 0}
        operations = [UpdateOne(query, update, upsert=upsert) for query, update in prepared]
        result = self._db.get_collection(self.COLLECTION_NAME).bulk_write(operations, ordered=False)
        if not result.acknowledged:
            raise RuntimeError("商品写入未获得数据库确认，请检查 write concern 配置")
        return {"new": result.upserted_count, "update": result.matched_count, "modified": result.modified_count}


def pending_format_query(recent_days: int = RECENT_DAYS) -> Dict[str, Any]:
    """候选商品索引查询条件：最近 N 天内更新，下推未成功/缺快照/改价/重洗条件，排除人工锁死 (`format_retry_count >= 999`)。"""
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=recent_days)
    return {
        "$and": [
            {"updated_at": {"$gte": cutoff_time}},
            {
                "$or": [
                    {"format_retry_count": {"$exists": False}},
                    {"format_retry_count": {"$lt": LOCKED_RETRY_SENTINEL}},
                ]
            },
            {
                "$or": [
                    {"need_reformat": True},
                    {
                        "$and": [
                            {"format_activity_price": {"$exists": True}},
                            {"activity_price": {"$type": "number", "$gt": 0}},
                            {"$expr": {"$ne": ["$activity_price", "$format_activity_price"]}},
                        ]
                    },
                    {
                        "$and": [
                            {
                                "$or": [
                                    {"format_status": {"$ne": "success"}},
                                    {"format_activity_price": {"$exists": False}},
                                ]
                            },
                            {
                                "$or": [
                                    {"format_retry_count": {"$exists": False}},
                                    {"format_retry_count": {"$lt": FORMAT_MAX_RETRIES}},
                                ]
                            },
                        ]
                    },
                ]
            },
        ]
    }


def pending_deal_format_query(recent_days: int = RECENT_DAYS) -> Dict[str, Any]:
    """候选好价线报商品查询条件（严格隔离于顶层官方 `format_info`，仅读写 `deal_info.format_*`）。"""
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=recent_days)
    deal_content_changed = {
        "$expr": {
            "$or": [
                {"$ne": ["$deal_info.deal_price", "$deal_info.format_deal_price"]},
                {
                    "$ne": [
                        {"$ifNull": ["$deal_info.clean_title", ""]},
                        {"$ifNull": ["$deal_info.format_clean_title", ""]},
                    ]
                },
            ]
        }
    }
    return {
        "$and": [
            {"updated_at": {"$gte": cutoff_time}},
            {"deal_info.is_deal_tip": True},
            {"deal_info.deal_price": {"$type": "number", "$gt": 0}},
            {
                "$or": [
                    {"deal_info.need_reformat": True},
                    {
                        "$and": [
                            {"deal_info.format_deal_price": {"$exists": True}},
                            deal_content_changed,
                        ]
                    },
                    {
                        "$and": [
                            {
                                "$or": [
                                    {"deal_info.format_status": {"$ne": "success"}},
                                    {"deal_info.format_info.pricing_basis": {"$exists": False}},
                                ]
                            },
                            {
                                "$or": [
                                    {"deal_info.format_retry_count": {"$exists": False}},
                                    {"deal_info.format_retry_count": {"$lt": FORMAT_MAX_RETRIES}},
                                ]
                            },
                        ]
                    },
                ]
            },
        ]
    }


def build_deal_prompt_text(base_prompt_text: Optional[str] = None) -> str:
    """复用通用商品结构化清洗提示词，并在末尾挂载线报专属补充约束。"""
    base = load_prompt_cached(PROMPT_FILE_PATH) if base_prompt_text is None else base_prompt_text
    supplement = load_prompt_cached(DEAL_SUPPLEMENT_PROMPT_PATH).strip()
    return f"{base.rstrip()}\n\n{supplement}"


def _clean_deal_text(value: Any) -> str:
    text = strip_ecommerce_tokens_and_urls(value, preserve_newlines=False)
    return _MULTI_SPACE_RE.sub(" ", text).strip()


def build_deal_product_desc(product: Dict[str, Any]) -> str:
    """平铺组装线报商品已有文本信息与官方日常价（去链接与口令）。"""
    deal_info = product.get("deal_info") if isinstance(product.get("deal_info"), dict) else {}
    clean_title = _clean_deal_text(deal_info.get("clean_title"))
    official_name = _clean_deal_text(product.get("name"))
    if not clean_title and not official_name:
        return ""

    act_p = product.get("activity_price")
    act_str = f"{float(act_p):.2f}".rstrip("0").rstrip(".") + "元" if _is_positive_number(act_p) else ""
    fields = [
        ("线报主商品", clean_title or official_name),
        ("官方标题参考", official_name),
        ("官方日常价", act_str),
        ("优惠方式", _clean_deal_text(deal_info.get("price_logic"))),
        ("折算参考", _clean_deal_text(deal_info.get("unit_deal_price"))),
        ("购买步骤", _clean_deal_text(deal_info.get("tutorial_text"))),
        ("原始爆料文案", _clean_deal_text(deal_info.get("original_copywriting"))),
    ]
    return " | ".join(f"{label}：{val}" for label, val in fields if val)


def _is_clean_string(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _clean_stripped_str(container: Dict[str, Any], key: str) -> bool:
    val = container.get(key)
    if isinstance(val, str):
        val = val.strip()
        container[key] = val
    return _is_clean_string(val)


def _is_positive_number(value: Any) -> bool:
    return type(value) in (int, float) and 0 < value <= BSON_MAX_INT64 and math.isfinite(value)


def _check_ranked_items(items, field, text_keys):
    if not isinstance(items, list):
        return False, f"{field} 必须是列表"
    required_keys = {*text_keys, "score"}
    for index, item in enumerate(items):
        location = f"{field}[{index}]"
        if not isinstance(item, dict) or set(item) != required_keys:
            return False, f"{location} 必须且只能包含 {'、'.join((*text_keys, 'score'))}"
        if not all(_clean_stripped_str(item, key) for key in text_keys):
            return False, f"{location} 的 {'、'.join(text_keys)} 必须是非空、无首尾空白的 UTF-8 字符串"
        if type(item["score"]) is not int or not 1 <= item["score"] <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"
    return True, ""


def _check_single_item(format_info, index):
    if not isinstance(format_info, dict):
        return False, f"元素[{index}] 必须是对象"
    prefix = f"元素[{index}](sku_id={format_info.get('sku_id')})"
    if set(format_info) != {"sku_id", "price", "core_entities", "decision_keywords", "pricing_basis"}:
        return False, f"{prefix} 必须且只能包含 sku_id、price、core_entities、decision_keywords、pricing_basis"

    price = format_info["price"]
    if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
        return False, f"{prefix} price 必须是大于0的有限数字"
    for field, text_keys in (
        ("core_entities", ("name",)),
        ("decision_keywords", ("attribute_name", "attribute_value")),
    ):
        valid, error = _check_ranked_items(format_info[field], field, text_keys)
        if not valid:
            return False, f"{prefix} {error}"
    pricing = format_info["pricing_basis"]
    if not isinstance(pricing, dict) or set(pricing) != {
        "is_inferred", "structure", "total_value", "base_unit", "equivalent_description",
    }:
        return False, f"{prefix} pricing_basis 必须是包含指定字段的完整对象，不能为 null"
    if type(pricing["is_inferred"]) is not bool:
        return False, f"{prefix} pricing_basis.is_inferred 必须是布尔值"
    if pricing["equivalent_description"] is not None and not _clean_stripped_str(pricing, "equivalent_description"):
        return False, f"{prefix} pricing_basis.equivalent_description 必须为 null 或非空、无首尾空白的字符串"
    structure = pricing["structure"]
    if not isinstance(structure, list) or not structure:
        return False, f"{prefix} pricing_basis.structure 必须是非空列表"
    total = 1.0
    for layer_index, layer in enumerate(structure):
        location = f"{prefix} pricing_basis.structure[{layer_index}]"
        if not isinstance(layer, dict) or set(layer) != {"value", "unit"}:
            return False, f"{location} 必须且只能包含 value、unit"
        if not _is_positive_number(layer["value"]):
            return False, f"{location}.value 必须是 (0, BSON_MAX_INT64] 范围内的有限数值，允许小数"
        if not _clean_stripped_str(layer, "unit"):
            return False, f"{location}.unit 必须是非空、无首尾空白的字符串"
        total *= layer["value"]
    total_value = pricing["total_value"]
    if not _is_positive_number(total_value):
        return False, f"{prefix} pricing_basis.total_value 必须是 (0, BSON_MAX_INT64] 范围内的有限数值"
    if not _is_positive_number(total):
        return False, f"{prefix} pricing_basis.structure 各层乘积越界或非法"
    if not math.isclose(total_value, total, rel_tol=1e-5):
        if math.isclose(total_value, total, rel_tol=1e-2):
            pricing["total_value"] = round(total, 6)
        else:
            return False, f"{prefix} pricing_basis.total_value ({total_value}) 必须等于各层 value 的乘积 ({total})"
    if not _clean_stripped_str(pricing, "base_unit") or pricing["base_unit"] != structure[-1]["unit"]:
        return False, f"{prefix} pricing_basis.base_unit 必须与最后一层 unit 一致"
    return True, ""


def check_format_info(format_info_list, input_sku_ids, expected_prices=None):
    """校验模型返回的结构化列表与可选确认价一致性。"""
    if not isinstance(format_info_list, list):
        return False, {}, {}, "顶层结构必须是一个列表"
    output_map = {}
    for index, item in enumerate(format_info_list):
        if not isinstance(item, dict):
            continue
        sku_id = item.get("sku_id")
        if type(sku_id) not in (str, int):
            continue
        output_map[str(sku_id).strip()] = (index, item)
    results, item_errors = {}, {}
    for sku_id in input_sku_ids:
        if sku_id not in output_map:
            item_errors[sku_id] = "模型未返回该规格的数据或 sku_id 缺失"
            continue
        index, item = output_map[sku_id]
        valid, error = _check_single_item(item, index)
        if not valid:
            item_errors[sku_id] = error
            continue
        if isinstance(expected_prices, dict) and sku_id in expected_prices:
            exp_p = expected_prices[sku_id]
            if not math.isclose(float(item["price"]), float(exp_p), rel_tol=1e-3, abs_tol=0.02):
                item_errors[sku_id] = (
                    f"元素[{index}](sku_id={sku_id}) 输出 price ({item['price']}) 与确认到手价 ({exp_p}) 不一致"
                )
                continue
            item["price"] = float(exp_p)
        results[sku_id] = item
    if not results:
        return False, {}, item_errors, "批次内所有规格均解析或校验失败"
    return True, results, item_errors, ""


def gen_goods_format_info(
    sku_task_batch,
    prompt_text: Optional[str] = None,
    enforce_input_price: bool = False,
    *,
    ai_caller: Optional[Callable[..., Dict[str, Any]]] = None,
):
    """调用大模型批量生成 SKU 结构化规格信息。"""
    outcome = {"status": "failed", "results": {}, "item_errors": {}, "model_used": None, "error": ""}
    if not isinstance(sku_task_batch, list) or not sku_task_batch:
        outcome["error"] = "批处理输入必须是非空列表"
        return outcome

    inputs = []
    input_sku_ids = []
    expected_prices = {}
    for task in sku_task_batch:
        sku_id = task.get("sku_id")
        name = task.get("product_desc")
        sku_desc = task.get("sku_desc", "")
        price = task.get("价格", "")
        task_expected_price = task.get("expected_price")

        if not sku_id or not str(sku_id).strip():
            outcome["error"] = "存在无效的 sku_id"
            return outcome
        if not isinstance(name, str) or not name.strip():
            outcome["error"] = f"规格(sku_id={sku_id}) product_desc 必须是非空字符串"
            return outcome
        clean_sku_id = str(sku_id).strip()
        if clean_sku_id in input_sku_ids:
            raise ValueError(f"同一次模型请求不能包含重复的 sku_id: {clean_sku_id}")

        if enforce_input_price:
            if not _is_positive_number(price):
                outcome["error"] = f"规格(sku_id={clean_sku_id}) 价格必须是大于0的有限数值"
                return outcome
            expected_prices[clean_sku_id] = float(price)
        elif task_expected_price is not None:
            if not _is_positive_number(task_expected_price):
                outcome["error"] = f"规格(sku_id={clean_sku_id}) expected_price 必须是大于0的有限数值"
                return outcome
            expected_prices[clean_sku_id] = float(task_expected_price)

        inputs.append({
            "product_desc": name,
            "sku_id": clean_sku_id,
            "sku_desc": sku_desc,
            "价格": price,
        })
        input_sku_ids.append(clean_sku_id)

    if prompt_text is None:
        prompt_text = load_prompt_cached(PROMPT_FILE_PATH)

    caller = ai_caller if ai_caller is not None else generate_content
    effective_expected_prices = expected_prices if expected_prices else None
    full_prompt = f"{prompt_text}\n<product_input>\n{json.dumps(inputs, ensure_ascii=False)}\n</product_input>"
    errors, content = [], ""
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = caller(
                prompt=full_prompt,
                preset_model_group="low",
                max_retries_per_model=1,
                timeout=150,
            )
            model_used = (result.get("metrics") or {}).get("model_used")
            if model_used:
                outcome["model_used"] = model_used
            if result.get("status") != "✅ 成功":
                detail = "；".join(str(error) for error in result.get("error_history", []) or [])
                raise RuntimeError(detail or result.get("content") or "模型调用失败")
            content = result.get("content", "")
            valid, results, item_errors, batch_error = check_format_info(
                parse_llm_json(content),
                input_sku_ids,
                expected_prices=effective_expected_prices,
            )
            if not valid:
                raise ValueError(f"{batch_error} | 抽样详情: {str(item_errors)[:200]}")
            outcome.update(status="success", results=results, item_errors=item_errors)
            return outcome
        except Exception as exc:
            detail = f"尝试 {attempt}/{LLM_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            if attempt < LLM_MAX_RETRIES:
                delay = 2 ** attempt
                logger.warning(
                    "[商品/重试] 模型调用、解析或校验未通过 | 规格批次大小: [%d] | 首个 SKU: [%s] "
                    "| 尝试: [%d/%d] | 等待: [%d 秒] | 原因: [%s]",
                    len(sku_task_batch),
                    input_sku_ids[0],
                    attempt,
                    LLM_MAX_RETRIES,
                    delay,
                    " ".join(detail.split())[:400],
                )
                time.sleep(delay)

    outcome["error"] = "；".join(errors) + (f"\n模型原文：{content}" if content else "")
    return outcome


def _clean_price_number_str(value: float) -> str:
    rounded = round(float(value), 4)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _resolve_single_spec_key(product_id: str, existing_fmt: Dict[str, Any]) -> str:
    """单规格主键统一为 `{product_id}_0`；兼容库中已存在的旧键 `{product_id}_1`。"""
    legacy_key = f"{product_id}_1"
    if isinstance(existing_fmt, dict) and legacy_key in existing_fmt and f"{product_id}_0" not in existing_fmt:
        return legacy_key
    return f"{product_id}_0"


def _detect_product_price_changes(product: Dict[str, Any]):
    """检测单规格商品当前活动价与上次清洗认定的价格是否发生变动。"""
    product_id = str(product.get("product_id") or "").strip()
    if not product_id:
        return False, set()

    raw_fmt = product.get("format_info")
    existing_fmt = raw_fmt if isinstance(raw_fmt, dict) and "pricing_basis" not in raw_fmt else {}
    act_price = product.get("activity_price")
    if not _is_positive_number(act_price):
        return False, set()
    act_float = float(act_price)
    exp_sku_id = _resolve_single_spec_key(product_id, existing_fmt)

    prev_act = product.get("format_activity_price")
    if type(prev_act) in (int, float) and math.isfinite(prev_act):
        if abs(act_float - float(prev_act)) > PRICE_COMPARE_ABS_TOL:
            return True, {exp_sku_id}
        return False, set()

    if not existing_fmt:
        return False, set()
    first_entry = existing_fmt.get(exp_sku_id)
    if (
        len(existing_fmt) != 1
        or not isinstance(first_entry, dict)
        or not _is_positive_number(first_entry.get("price"))
        or abs(act_float - float(first_entry["price"])) > PRICE_COMPARE_ABS_TOL
    ):
        return True, {exp_sku_id}
    return False, set()


def _chunk_sku_batches(tasks: list, batch_size: int):
    eff_size = max(1, int(batch_size))
    batches, current_batch, batch_sku_ids = [], [], set()
    for task in tasks:
        if len(current_batch) == eff_size or task["sku_id"] in batch_sku_ids:
            batches.append(current_batch)
            current_batch, batch_sku_ids = [], set()
        current_batch.append(task)
        batch_sku_ids.add(task["sku_id"])
    if current_batch:
        batches.append(current_batch)
    return batches


def _execute_format_batches(
    batches: list,
    eff_workers: int,
    process_batch_fn: Callable[[list, dict], None],
    counts: Dict[str, int],
    *,
    err_tag: str,
    done_prefix: str,
    started: float,
) -> None:
    """并发调度格式化批次并汇总成功/失败/跳过计数与统一日志。"""
    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=eff_workers) as executor:
        futures = {}
        for batch in batches:
            batch_counts = {
                "success": 0, "failed": 0, "skipped": 0,
                "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
                "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0,
            }
            futures[executor.submit(process_batch_fn, batch, batch_counts)] = (batch, batch_counts)

        for future in as_completed(futures):
            batch, batch_counts = futures[future]
            try:
                future.result()
            except Exception as exc:
                unfinished = len(batch) - (batch_counts["success"] + batch_counts["failed"] + batch_counts["skipped"])
                unexpected_errors += unfinished
                logger.exception(
                    "❌ [%s/批次异常] 本批未完成 | 首个 SKU: [%s] | 未完成: [%d] | 原因: %s",
                    err_tag,
                    batch[0]["sku_id"],
                    unfinished,
                    exc,
                )

            for key in counts:
                counts[key] += batch_counts[key]

    done_log = logger.warning if unexpected_errors else logger.info
    done_log(
        "[%s] 本轮处理结束 | 成功/失败/跳过: [%d/%d/%d] | 常规: [%d/%d/%d] | 重洗: [%d/%d/%d] | 耗时: [%.2f 秒]",
        done_prefix,
        counts["success"],
        counts["failed"],
        counts["skipped"],
        counts["normal_success"],
        counts["normal_failed"],
        counts["normal_skipped"],
        counts["reformat_success"],
        counts["reformat_failed"],
        counts["reformat_skipped"],
        time.monotonic() - started,
    )


def run_format_round(
    product_manager: ProductManager,
    *,
    workers: Optional[int] = None,
    batch_size: Optional[int] = None,
    recent_days: Optional[int] = None,
):
    """并发生成单规格结构化信息，支持价格变动自动重洗与写回 `format_info.{product_id}_0`。"""
    started = time.monotonic()
    eff_workers = max(1, int(workers if workers is not None else FORMAT_WORKERS))
    eff_batch_size = max(1, int(batch_size if batch_size is not None else FORMAT_BATCH_SIZE))
    eff_recent_days = max(1, int(recent_days if recent_days is not None else RECENT_DAYS))
    query_condition = pending_format_query(recent_days=eff_recent_days)

    products = product_manager.query(
        query_condition,
        projection={
            "_id": 1,
            "platform": 1,
            "product_id": 1,
            "name": 1,
            "need_reformat": 1,
            "activity_price": 1,
            "format_info": 1,
            "format_status": 1,
            "format_retry_count": 1,
            "format_activity_price": 1,
        },
        sort=[("updated_at", -1)],
    )

    counts = {
        "success": 0, "failed": 0, "skipped": 0,
        "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
        "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0,
    }

    if not products:
        logger.info("[调度/完成] 本轮无待处理商品 | 数量: [0] | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    all_sku_tasks = []
    total_reformat_prods = 0
    round_failed_docs = set()
    state_lock = threading.Lock()

    for product in products:
        retry_cnt = product.get("format_retry_count")
        if type(retry_cnt) in (int, float) and retry_cnt >= LOCKED_RETRY_SENTINEL:
            continue

        product_id = str(product.get("product_id") or "").strip()
        name = str(product.get("name") or "").strip()
        raw_format_info = product.get("format_info")
        has_invalid_format_info = "format_info" in product and not isinstance(raw_format_info, dict)
        has_legacy_flat_format = isinstance(raw_format_info, dict) and "pricing_basis" in raw_format_info
        existing_format_info = (
            raw_format_info
            if isinstance(raw_format_info, dict) and not has_legacy_flat_format
            else {}
        )

        has_price_mismatch, _ = _detect_product_price_changes(product)
        explicit_reformat = bool(product.get("need_reformat"))
        is_reformat = explicit_reformat or has_price_mismatch

        if not is_reformat and not has_legacy_flat_format and not has_invalid_format_info:
            if type(retry_cnt) in (int, float) and retry_cnt >= FORMAT_MAX_RETRIES:
                continue
            if product_id and product.get("format_status") == "success" and "format_activity_price" in product:
                exp_key = _resolve_single_spec_key(product_id, existing_format_info)
                if exp_key in existing_format_info:
                    continue

        if is_reformat:
            total_reformat_prods += 1

        if not product_id:
            counts["skipped"] += 1
            counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
            continue

        act_raw = product.get("activity_price")
        if not name or not _is_positive_number(act_raw):
            fail_patch = {
                "platform": product["platform"],
                "product_id": product_id,
                "need_reformat": False,
                "format_status": "failed",
                "format_error": "缺少可清洗的商品标题" if not name else "单规格商品缺少大于0的有效 activity_price",
                "format_retry_count": FORMAT_MAX_RETRIES,
                "format_updated_at": datetime.now(timezone.utc),
                "format_activity_price": (
                    float(act_raw)
                    if type(act_raw) in (int, float) and math.isfinite(act_raw)
                    else 0.0
                ),
            }
            if is_reformat or has_legacy_flat_format or has_invalid_format_info:
                fail_patch["format_info"] = {}
            product_manager.update(fail_patch, condition={"_id": product["_id"]}, upsert=False)
            counts["skipped"] += 1
            counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
            continue

        act_float = float(act_raw)
        if is_reformat or has_legacy_flat_format or has_invalid_format_info:
            retained_format_info = {}
            reset_patch = {
                "platform": product["platform"],
                "product_id": product_id,
                "need_reformat": False,
                "format_status": "pending",
                "format_info": retained_format_info,
                "format_retry_count": 0,
                "format_activity_price": act_float,
            }
            existing_format_info = retained_format_info
            product["format_info"] = existing_format_info
            product["need_reformat"] = False
            product_manager.update(reset_patch, condition={"_id": product["_id"]}, upsert=False)
        else:
            product["format_info"] = existing_format_info

        sku_id = _resolve_single_spec_key(product_id, existing_format_info)
        if sku_id in existing_format_info:
            done_patch = {
                "platform": product["platform"],
                "product_id": product_id,
                "format_status": "success",
                "need_reformat": False,
                "format_error": "",
                "format_updated_at": datetime.now(timezone.utc),
                "format_activity_price": act_float,
            }
            product_manager.update(done_patch, condition={"_id": product["_id"]}, upsert=False)
            continue

        all_sku_tasks.append({
            "sku_id": sku_id,
            "product_id": product_id,
            "product_desc": name,
            "sku_desc": "",
            "价格": _clean_price_number_str(act_float),
            "expected_price": act_float,
            "product_ref": product,
            "is_reformat": is_reformat,
        })

    if not all_sku_tasks:
        logger.info("[调度/完成] 查到候选商品，其下规格均已生成完毕并已同步状态 | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    prompt_text = load_prompt_cached(PROMPT_FILE_PATH)
    batches = _chunk_sku_batches(all_sku_tasks, eff_batch_size)

    logger.info(
        "[调度/本轮] 开始格式化规格 | 涉及商品: [%d] (常规: %d, 重洗: %d) | 待洗SKU数: [%d] | 批次: [%d] | 并发: [%d]",
        len(products),
        len(products) - total_reformat_prods,
        total_reformat_prods,
        len(all_sku_tasks),
        len(batches),
        eff_workers,
    )

    def process_batch(batch, batch_counts):
        batch_started = time.monotonic()
        generated = gen_goods_format_info(batch, prompt_text)
        failure_details, skipped_ids = [], []

        for task in batch:
            sku_id = task["sku_id"]
            product = task["product_ref"]
            doc_id = product["_id"]
            product_id = product["product_id"]
            is_reformat = task["is_reformat"]

            format_info_item = generated["results"].get(sku_id)
            is_success = format_info_item is not None
            error = "" if is_success else (
                generated["error"] if generated["status"] == "failed"
                else generated["item_errors"].get(sku_id, "未知的规格解析错误")
            )

            update_fields = {
                "platform": product["platform"],
                "product_id": product_id,
                "format_model": generated["model_used"],
                "format_updated_at": datetime.now(timezone.utc),
                "format_activity_price": task["expected_price"],
            }

            increments_arg = None
            with state_lock:
                fmt_map = product.setdefault("format_info", {})
                if is_success:
                    update_fields[f"format_info.{sku_id}"] = format_info_item
                    fmt_map[sku_id] = format_info_item
                    update_fields["format_status"] = "success"
                    update_fields["need_reformat"] = False
                    update_fields["format_error"] = ""
                    update_fields["format_retry_count"] = 0
                else:
                    update_fields["format_status"] = "success" if fmt_map else "failed"
                    update_fields["format_error"] = error
                    if doc_id not in round_failed_docs:
                        round_failed_docs.add(doc_id)
                        increments_arg = {"format_retry_count": 1}

            saved = product_manager.update(
                update_fields,
                condition={"_id": doc_id},
                increments=increments_arg,
                upsert=False,
            )

            status_key = "success" if is_success else "failed"
            if not saved.get("update"):
                batch_counts["skipped"] += 1
                batch_counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
                skipped_ids.append(sku_id)
                continue

            batch_counts[status_key] += 1
            batch_counts[f"reformat_{status_key}" if is_reformat else f"normal_{status_key}"] += 1

            if not is_success:
                failure_details.append(f"{sku_id}: {' '.join(error.split())[:240]}")

        failed = batch_counts["failed"]
        all_failed = failed == len(batch)
        batch_log = logger.error if all_failed else logger.warning if failed or batch_counts["skipped"] else logger.info
        message = "❌ [规格/批次完成] 全批生成失败" if all_failed else "[规格/批次完成] 规格结果已增量写入"
        detail = (
            " ".join(generated["error"].split())[:800]
            if generated["status"] == "failed"
            else "; ".join(failure_details)[:800]
        )

        batch_log(
            "%s | 规格列表: [%s] | 成功/失败/跳过: [%d/%d/%d] | 耗时: [%.2f 秒] | 失败摘要: [%s] | 跳过: [%s]",
            message,
            ", ".join(t["sku_id"] for t in batch),
            batch_counts["success"],
            failed,
            batch_counts["skipped"],
            time.monotonic() - batch_started,
            detail or "无",
            ", ".join(skipped_ids) or "无",
        )

    _execute_format_batches(
        batches,
        eff_workers,
        process_batch,
        counts,
        err_tag="规格",
        done_prefix="调度/完成",
        started=started,
    )
    return counts


def run_deal_format_round(
    product_manager: ProductManager,
    limit: int = DEAL_FORMAT_ROUND_LIMIT,
    *,
    workers: Optional[int] = None,
    batch_size: Optional[int] = None,
    recent_days: Optional[int] = None,
):
    """批量清洗正式表中好价线报（`deal_info`），结果独立写回 `deal_info.format_info`，绝不触碰顶层官方 `format_info`。"""
    started = time.monotonic()
    eff_workers = max(1, int(workers if workers is not None else DEAL_FORMAT_WORKERS))
    eff_batch_size = max(1, int(batch_size if batch_size is not None else FORMAT_BATCH_SIZE))
    eff_recent_days = max(1, int(recent_days if recent_days is not None else RECENT_DAYS))
    query_condition = pending_deal_format_query(recent_days=eff_recent_days)

    products = product_manager.query(
        query_condition,
        projection={
            "_id": 1,
            "platform": 1,
            "product_id": 1,
            "name": 1,
            "activity_price": 1,
            "deal_info.is_deal_tip": 1,
            "deal_info.clean_title": 1,
            "deal_info.deal_price": 1,
            "deal_info.unit_deal_price": 1,
            "deal_info.price_logic": 1,
            "deal_info.tutorial_text": 1,
            "deal_info.original_copywriting": 1,
            "deal_info.need_reformat": 1,
            "deal_info.format_deal_price": 1,
            "deal_info.format_clean_title": 1,
            "deal_info.format_status": 1,
            "deal_info.format_retry_count": 1,
        },
        sort=[("updated_at", -1)],
        limit=limit,
    )

    counts = {
        "success": 0, "failed": 0, "skipped": 0,
        "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
        "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0,
    }

    if not products:
        logger.info("[线报调度/完成] 本轮无待处理线报商品 | 数量: [0] | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    deal_tasks = []
    total_reformat_deals = 0

    for product in products:
        product_id = str(product.get("product_id") or "").strip()
        deal_info = product.get("deal_info") if isinstance(product.get("deal_info"), dict) else {}
        raw_deal_price = deal_info.get("deal_price")
        raw_clean_title = str(deal_info.get("clean_title") or "")

        if not product_id or not _is_positive_number(raw_deal_price):
            counts["skipped"] += 1
            counts["normal_skipped"] += 1
            continue

        has_prev = "format_deal_price" in deal_info
        prev_price = deal_info.get("format_deal_price")
        price_changed = has_prev and (
            not _is_positive_number(prev_price)
            or not math.isclose(float(prev_price), float(raw_deal_price), rel_tol=1e-6, abs_tol=1e-6)
        )
        title_changed = has_prev and (str(deal_info.get("format_clean_title") or "") != raw_clean_title)
        is_reformat = bool(deal_info.get("need_reformat")) or price_changed or title_changed
        if is_reformat:
            total_reformat_deals += 1

        desc = build_deal_product_desc(product)
        if not desc:
            product_manager.update(
                {
                    "platform": product["platform"],
                    "product_id": product_id,
                    "deal_info.format_status": "failed",
                    "deal_info.need_reformat": False,
                    "deal_info.format_error": "缺少可清洗的线报标题与商品标题",
                    "deal_info.format_deal_price": raw_deal_price,
                    "deal_info.format_clean_title": raw_clean_title,
                    "deal_info.format_retry_count": FORMAT_MAX_RETRIES,
                    "deal_info.format_updated_at": datetime.now(timezone.utc),
                },
                condition={"_id": product["_id"], "deal_info.is_deal_tip": True},
                upsert=False,
            )
            counts["skipped"] += 1
            counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
            continue

        deal_tasks.append({
            "sku_id": f"{product_id}_deal",
            "product_id": product_id,
            "product_desc": desc,
            "sku_desc": "",
            "价格": float(raw_deal_price),
            "raw_deal_price": raw_deal_price,
            "raw_clean_title": raw_clean_title,
            "product_ref": product,
            "is_reformat": is_reformat,
        })

    if not deal_tasks:
        logger.info("[线报调度/完成] 候选线报均无需送洗 | 跳过: [%d] | 耗时: [%.2f 秒]", counts["skipped"], time.monotonic() - started)
        return counts

    deal_prompt_text = build_deal_prompt_text()
    batches = _chunk_sku_batches(deal_tasks, eff_batch_size)

    logger.info(
        "[线报调度/本轮] 开始格式化好价线报 | 待洗数: [%d] (常规: %d, 重洗: %d) | 批次: [%d] | 并发: [%d]",
        len(deal_tasks),
        len(deal_tasks) - total_reformat_deals,
        total_reformat_deals,
        len(batches),
        eff_workers,
    )

    def process_deal_batch(batch, batch_counts):
        batch_started = time.monotonic()
        generated = gen_goods_format_info(batch, prompt_text=deal_prompt_text, enforce_input_price=True)
        failure_details, skipped_ids = [], []

        for task in batch:
            sku_id = task["sku_id"]
            product = task["product_ref"]
            doc_id = product["_id"]
            product_id = task["product_id"]
            is_reformat = task["is_reformat"]

            format_info_item = generated["results"].get(sku_id)
            is_success = format_info_item is not None
            error = "" if is_success else (
                generated["error"] if generated["status"] == "failed"
                else generated["item_errors"].get(sku_id, "未知的线报解析错误")
            )

            update_fields = {
                "platform": product["platform"],
                "product_id": product_id,
                "deal_info.need_reformat": False,
                "deal_info.format_model": generated["model_used"],
                "deal_info.format_updated_at": datetime.now(timezone.utc),
                "deal_info.format_deal_price": task["raw_deal_price"],
                "deal_info.format_clean_title": task["raw_clean_title"],
            }
            increments = None

            if is_success:
                update_fields["deal_info.format_info"] = format_info_item
                update_fields["deal_info.format_status"] = "success"
                update_fields["deal_info.format_error"] = ""
                update_fields["deal_info.format_retry_count"] = 0
            else:
                update_fields["deal_info.format_status"] = "failed"
                update_fields["deal_info.format_error"] = error
                if is_reformat:
                    update_fields["deal_info.format_info"] = None
                    update_fields["deal_info.format_retry_count"] = 1
                else:
                    increments = {"deal_info.format_retry_count": 1}

            saved = product_manager.update(
                update_fields,
                condition={"_id": doc_id, "deal_info.is_deal_tip": True},
                increments=increments,
                upsert=False,
            )

            status_key = "success" if is_success else "failed"
            if not saved.get("update"):
                batch_counts["skipped"] += 1
                batch_counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
                skipped_ids.append(sku_id)
                continue

            batch_counts[status_key] += 1
            batch_counts[f"reformat_{status_key}" if is_reformat else f"normal_{status_key}"] += 1

            if not is_success:
                failure_details.append(f"{sku_id}: {' '.join(error.split())[:240]}")

        failed = batch_counts["failed"]
        all_failed = failed == len(batch)
        batch_log = logger.error if all_failed else logger.warning if failed or batch_counts["skipped"] else logger.info
        message = "❌ [线报/批次完成] 全批生成失败" if all_failed else "[线报/批次完成] 线报结果已增量写入"
        detail = (
            " ".join(generated["error"].split())[:800]
            if generated["status"] == "failed"
            else "; ".join(failure_details)[:800]
        )

        batch_log(
            "%s | 列表: [%s] | 成功/失败/跳过: [%d/%d/%d] | 耗时: [%.2f 秒] | 失败摘要: [%s] | 跳过: [%s]",
            message,
            ", ".join(t["sku_id"] for t in batch),
            batch_counts["success"],
            failed,
            batch_counts["skipped"],
            time.monotonic() - batch_started,
            detail or "无",
            ", ".join(skipped_ids) or "无",
        )

    _execute_format_batches(
        batches,
        eff_workers,
        process_deal_batch,
        counts,
        err_tag="线报",
        done_prefix="线报调度/完成",
        started=started,
    )
    return counts
