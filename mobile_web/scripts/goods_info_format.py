# [功能摘要] 批量将商品名称及规格转换为结构化信息，逐规格增量保存成功或失败结果。
# [输入数据] products 中的 {_id, platform, product_id, name, sku_info, activity_price}；本地提示词；模型响应。
# [数据流转/交互] 每轮查询最近一周内未格式化的候选商品、读取一次提示词 → 每批最多 20 个规格且 ID 不冲突 →
#                 多线程调用模型（最多 3 次）→ string_to_object 解析 → 单品校验 → 局部写回 format_info.{sku_id}。
# [输出数据] format_* 字段、原子失败次数及 success/failed/skipped 统计；每轮结束等待固定间隔。
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from pymongo import UpdateOne

# 将 mobile_web 目录加入 sys.path，支持从任意工作目录直接运行脚本
MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

from infrastructure.ai_api.model_api import generate_content, setup_logger
from infrastructure.mongo import gen_db_object

logger = setup_logger(app_name="goods_format")

PROMPT_FILE_PATH = MOBILE_WEB_DIR / "prompt" / "商品数据结构化清洗.txt"
LLM_MAX_RETRIES = 3
FORMAT_MAX_RETRIES = 3
LOCKED_RETRY_SENTINEL = 999
PRICE_COMPARE_ABS_TOL = 1e-4
FORMAT_WORKERS = 10
DEAL_FORMAT_WORKERS = 10
FORMAT_BATCH_SIZE = 20
DEAL_FORMAT_ROUND_LIMIT = 0
ROUND_INTERVAL_SECONDS = 60
RECENT_DAYS = 7
BSON_MAX_INT64 = 2 ** 63 - 1
COUNT_KEYS = ("success", "failed", "skipped")

_DEAL_URL_RE = re.compile(r"https?://\S+")
_DEAL_TOKEN_RE = re.compile(r"[（(]\s*口令\s*[:：][^）)]+[）)]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_PLAIN_PRICE_TEXT_RE = re.compile(r"^[￥¥]?\s*(\d+(?:\.\d+)?)\s*$")

DEAL_SUPPLEMENT_RULES = """
---
# 线报场景专属补充约束（最高优先级，若与上文冲突以本节为准）
当前输入为经核验的【电商好价线报】，`价格` 为已确认的成交到手价；`product_desc` 平铺提供了 `线报主商品`（本次锁定的目标主商品）、`原始爆料文案`（一手爆料原文）、`优惠方式/折算参考/购买步骤`（原文整理摘要，偶有漏抄或偏差）以及 `官方标题参考/官方日常价`（商品链接单件日常基准）：
1. **价格直接采信**：输入字段 `价格` 已是最终确定的有效成交金额（大于 0 的数字）。无论文本中是否含“起、预估、约、低至、折合、返卡/返红包后”等字样，**`price` 必须直接输出输入字段 `价格` 对应的浮点数，严禁返回 0.0**。
2. **主商品唯一隔离**：仅提取 `线报主商品` 指向的唯一主体；文本中的凑单品、顺手买、备选方案或同帖顺带提及的其他无关商品严禁混入任何输出字段；异品类的随单赠品仅记入 `equivalent_description`，不计入主商品物理总量。
3. **价格与交付总量严格对齐**：结合 `原始爆料文案`、购买方案与 `官方日常价`，核实输入 `价格` 这一笔金额实际对应的是“多件实付总价”还是“折合单件价”：
   - **多件总价口径**：若输入 `价格` 对应拍 N 件或同品买赠的实付总价（注意：“满3件打9折”、“买4免1”等活动门槛标签本身不等于实际拍下件数），`pricing_basis` 按该总价最终实际到手的全部主商品总量计算（若摘要标题中的数量已是拍 N 件后的汇总总数，勿再与 N 重复相乘）。
   - **单件折合价口径**：若方案虽需拍 N 件凑单，但输入 `价格` 实为折合 1 件的到手单价，则 `pricing_basis` **严格按 1 个购买单元内含的完整物理规格计算**（若 1 件本身为多袋/多瓶组合装如 `200g*4袋`，1 件即为 `4袋×200g=800g`，严禁错乘拍下件数 N，也严禁漏算单件内含规格）。
4. **真实物理规格核实**：
   - 对照 `原始爆料文案` 核实该 `价格` 实际对应的主商品确定规格（修正摘要漏抄或误抄的外层规格）；仅当原文未写单件基础净含量/片数且无冲突时，从 `官方标题参考` 补全单件基础规格。**严禁将促销门槛词（如 `【3件129】`、`【99选5】`）或“最多/最高/至高”类不确定上限词计入 `structure`**。
   - 同品类多规格组合装（如 `235ml*7瓶 + 950ml*2瓶`）无法用单一连乘表达时，直接求和输出单层总物理量（明细记入 `equivalent_description`），严禁丢弃其中一部分规格。
""".strip()


def read_file_to_str(
    filepath,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str:
    """
    读取文件并返回整个内容的字符串。
    """
    p = Path(filepath)
    with p.open("r", encoding=encoding, errors=errors) as f:
        return f.read()


def string_to_object(input_str: str):
    """
    从字符串中提取并解析出 Python 列表或字典对象，专为处理 LLM 输出设计。

    核心增强：
    1. 智能提取：自动忽略 Markdown 标记（如 ```json）和首尾废话。
    2. 安全清洗（String-Aware）：在移除注释和尾随逗号时，严格保护字符串内部的内容（如 URL、带 // 的规格名）。
    3. 双引擎解析：结合 json.loads 的高性能与 ast.literal_eval 的宽容度。
    """
    if not isinstance(input_str, str):
        raise TypeError(f"输入必须是字符串，但收到了 {type(input_str).__name__}。")

    def _create_error_message(reason: str) -> str:
        preview = (input_str[:500] + "...") if len(input_str) > 500 else input_str
        return f"{reason} | 输入内容预览: '\n\n{preview}\n\n'"

    # 1. 智能提取：在字符串中寻找对象边界
    first_bracket = input_str.find("[")
    first_curly = input_str.find("{")

    if first_bracket == -1 and first_curly == -1:
        raise ValueError(_create_error_message("输入字符串中未找到起始符号 '[' 或 '{'"))

    if first_bracket == -1:
        start_pos = first_curly
    elif first_curly == -1:
        start_pos = first_bracket
    else:
        start_pos = min(first_bracket, first_curly)

    end_pos = max(input_str.rfind("]"), input_str.rfind("}"))

    if end_pos <= start_pos:
        raise ValueError(_create_error_message("未找到与起始括号匹配的结束括号 ']' 或 '}'"))

    potential_obj_str = input_str[start_pos : end_pos + 1]

    # 2. 尝试直接解析（在不进行任何危险替换的情况下优先尝试）
    try:
        return json.loads(potential_obj_str)
    except json.JSONDecodeError:
        pass

    try:
        # ast.literal_eval 原生支持尾随逗号，能解决很多问题
        return ast.literal_eval(potential_obj_str)
    except (ValueError, SyntaxError, MemoryError):
        pass

    # 3. 字符串感知的安全清洗 (如果直接解析失败，再进行深度清洗)
    def safe_cleaner(match):
        if match.group(1):  # 如果是字符串内部的内容，原样保留
            return match.group(1)
        return ""  # 如果是注释或尾随逗号，删除它

    # 3.1 安全移除注释 (// 和 /* */)
    comment_pattern = r'(".*?(?<!\\)(?:\\\\)*"|\'.*?(?<!\\)(?:\\\\)*\')|(/\*[\s\S]*?\*/|//[^\r\n]*)'
    cleaned_str = re.sub(comment_pattern, safe_cleaner, potential_obj_str)

    # 3.2 安全移除尾随逗号 (例如 {"a": 1, } -> {"a": 1})
    comma_pattern = r'(".*?(?<!\\)(?:\\\\)*"|\'.*?(?<!\\)(?:\\\\)*\')|(,\s*(?=[\]}]))'
    cleaned_str = re.sub(comma_pattern, safe_cleaner, cleaned_str).strip()

    # 4. 使用清洗后的字符串进行最终解析
    try:
        return json.loads(cleaned_str)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(cleaned_str)
        except (ValueError, SyntaxError, MemoryError) as e:
            error_reason = f"无法将提取的内容解析为列表或字典，解析器错误: {e}"
            raise ValueError(
                f"{_create_error_message(error_reason)}\n"
                f"尝试解析的内容 (安全清洗后): '''{cleaned_str}'''"
            )


class ProductManager:
    """唯一身份为 (platform, product_id)，不填默认业务字段，也不替换整条商品。"""

    COLLECTION_NAME = "products"

    def __init__(self, db_instance):
        if db_instance is None:
            raise ValueError("必须提供有效的 MongoBase 实例")
        self._db = db_instance
        self._db.create_index(self.COLLECTION_NAME, [("platform", 1), ("product_id", 1)], unique=True)
        self._db.create_index(self.COLLECTION_NAME, [("platform", 1), ("updated_at", -1)])
        self._db.create_index(self.COLLECTION_NAME, [("deal_info.is_deal_tip", 1), ("updated_at", -1)])

    def query(self, query=None, projection=None, sort=None, limit=0):
        """入参沿用 Mongo 条件/投影/排序形貌；返回 list[dict]，业务过滤条件由应用层构造。"""
        return self._db.find_many(
            self.COLLECTION_NAME, query=query, projection=projection, sort=sort, limit=limit,
        )

    @staticmethod
    def _prepare_updates(records, condition, increments, upsert):
        """把补丁完整校验为 [(身份条件, 更新字典)]，确保非法批次在任何数据库写入前失败。"""
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
        """records: {platform, product_id, 需修改字段} 或其列表；返回 {new, update, modified}。
        只赋值显式字段；None/空值是显式赋值，嵌套局部更新使用 '父字段.子字段' 点路径。
        condition 是附加匹配条件，increments 是原子增量；带条件时禁止插入。
        字典/列表字段作为整体赋值；同批同身份按字段合并，重复字段以后一次为准，增量执行一次。
        """
        prepared = self._prepare_updates(records, condition, increments, upsert)
        if not prepared:
            return {"new": 0, "update": 0, "modified": 0}
        operations = [UpdateOne(query, update, upsert=upsert) for query, update in prepared]
        result = self._db.get_collection(self.COLLECTION_NAME).bulk_write(operations, ordered=False)
        if not result.acknowledged:
            raise RuntimeError("商品写入未获得数据库确认，请检查 write concern 配置")
        return {"new": result.upserted_count, "update": result.matched_count, "modified": result.modified_count}


def pending_format_query(recent_days=RECENT_DAYS):
    """
    候选商品查询条件：
    1. 仅限最近一周（recent_days 天）内更新的数据，且未被人工锁死（format_retry_count < 999）
    2. 显式标记了 need_reformat: True
    3. 单规格商品（无 sku_info，或 sku_info 仅含 1 条无规格属性的价格占位项）当前 activity_price 与上次清洗价格不一致
    4. 多规格/带规格属性商品中任一 SKU 的当前价格文本与上次清洗快照（或历史单值价格）不一致
    5. 尚未成功格式化，或 format_info 数量少于 sku_info 长度（支持断点补洗），且累计失败重试次数未达阈值
    """
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=recent_days)

    sku_list_expr = {
        "$cond": [
            {"$isArray": "$sku_info"},
            "$sku_info",
            [],
        ]
    }
    fmt_entries_expr = {
        "$cond": [
            {
                "$and": [
                    {"$eq": [{"$type": "$format_info"}, "object"]},
                    {"$eq": [{"$type": "$format_info.pricing_basis"}, "missing"]},
                ]
            },
            {"$objectToArray": "$format_info"},
            [],
        ]
    }
    sku0_obj_expr = {
        "$let": {
            "vars": {"first": {"$arrayElemAt": [sku_list_expr, 0]}},
            "in": {
                "$cond": [
                    {"$eq": [{"$type": "$$first"}, "object"]},
                    {"$objectToArray": "$$first"},
                    [],
                ]
            },
        }
    }
    specless_single_sku_expr = {
        "$or": [
            {"$eq": [{"$size": sku_list_expr}, 0]},
            {
                "$and": [
                    {"$eq": [{"$size": sku_list_expr}, 1]},
                    {
                        "$eq": [
                            {
                                "$size": {
                                    "$filter": {
                                        "input": sku0_obj_expr,
                                        "as": "kv",
                                        "cond": {
                                            "$and": [
                                                {"$not": [{"$in": ["$$kv.k", ["价格", "截图地址"]]}]},
                                                {
                                                    "$ne": [
                                                        {
                                                            "$trim": {
                                                                "input": {
                                                                    "$convert": {
                                                                        "input": "$$kv.v",
                                                                        "to": "string",
                                                                        "onError": "",
                                                                        "onNull": "",
                                                                    }
                                                                }
                                                            }
                                                        },
                                                        "",
                                                    ]
                                                },
                                            ]
                                        },
                                    }
                                }
                            },
                            0,
                        ]
                    },
                ]
            },
        ]
    }

    specless_price_changed_expr = {
        "$and": [
            specless_single_sku_expr,
            {"$isNumber": "$activity_price"},
            {"$gt": ["$activity_price", 0]},
            {
                "$cond": [
                    {"$isNumber": "$format_activity_price"},
                    {
                        "$gt": [
                            {"$abs": {"$subtract": ["$activity_price", "$format_activity_price"]}},
                            PRICE_COMPARE_ABS_TOL,
                        ]
                    },
                    {
                        "$let": {
                            "vars": {
                                "entries": fmt_entries_expr,
                                "exp_id": {
                                    "$concat": [
                                        {
                                            "$convert": {
                                                "input": "$product_id",
                                                "to": "string",
                                                "onError": "",
                                                "onNull": "",
                                            }
                                        },
                                        {"$cond": [{"$eq": [{"$size": sku_list_expr}, 1]}, "_1", "_0"]},
                                    ]
                                },
                            },
                            "in": {
                                "$and": [
                                    {"$gt": [{"$size": "$$entries"}, 0]},
                                    {
                                        "$let": {
                                            "vars": {"first_entry": {"$arrayElemAt": ["$$entries", 0]}},
                                            "in": {
                                                "$or": [
                                                    {"$ne": [{"$size": "$$entries"}, 1]},
                                                    {"$ne": ["$$first_entry.k", "$$exp_id"]},
                                                    {"$not": [{"$isNumber": "$$first_entry.v.price"}]},
                                                    {
                                                        "$gt": [
                                                            {
                                                                "$abs": {
                                                                    "$subtract": [
                                                                        "$activity_price",
                                                                        "$$first_entry.v.price",
                                                                    ]
                                                                }
                                                            },
                                                            PRICE_COMPARE_ABS_TOL,
                                                        ]
                                                    },
                                                ]
                                            },
                                        }
                                    },
                                ]
                            },
                        }
                    },
                ]
            },
        ]
    }

    fmt_sku_prices_entries_expr = {
        "$cond": [
            {"$eq": [{"$type": "$format_sku_prices"}, "object"]},
            {"$objectToArray": "$format_sku_prices"},
            [],
        ]
    }
    multi_sku_price_changed_expr = {
        "$and": [
            {"$not": [specless_single_sku_expr]},
            {
                "$or": [
                    {"$gt": [{"$size": fmt_entries_expr}, 0]},
                    {"$gt": [{"$size": fmt_sku_prices_entries_expr}, 0]},
                ]
            },
            {
                "$let": {
                    "vars": {
                        "skus": sku_list_expr,
                        "fmt_entries": fmt_entries_expr,
                        "snap_entries": fmt_sku_prices_entries_expr,
                        "pid_str": {
                            "$convert": {
                                "input": "$product_id",
                                "to": "string",
                                "onError": "",
                                "onNull": "",
                            }
                        },
                    },
                    "in": {
                        "$anyElementTrue": {
                            "$map": {
                                "input": {"$range": [0, {"$size": "$$skus"}]},
                                "as": "idx",
                                "in": {
                                    "$let": {
                                        "vars": {
                                            "sku_item": {"$arrayElemAt": ["$$skus", "$$idx"]},
                                            "sid": {
                                                "$concat": [
                                                    "$$pid_str",
                                                    "_",
                                                    {"$toString": {"$add": ["$$idx", 1]}},
                                                ]
                                            },
                                        },
                                        "in": {
                                            "$let": {
                                                "vars": {
                                                    "cur_p_str": {
                                                        "$trim": {
                                                            "input": {
                                                                "$convert": {
                                                                    "input": {
                                                                        "$cond": [
                                                                            {"$eq": [{"$type": "$$sku_item"}, "object"]},
                                                                            "$$sku_item.价格",
                                                                            "",
                                                                        ]
                                                                    },
                                                                    "to": "string",
                                                                    "onError": "",
                                                                    "onNull": "",
                                                                }
                                                            }
                                                        }
                                                    },
                                                    "snap_match": {
                                                        "$filter": {
                                                            "input": "$$snap_entries",
                                                            "as": "se",
                                                            "cond": {"$eq": ["$$se.k", "$$sid"]},
                                                        }
                                                    },
                                                    "fmt_match": {
                                                        "$filter": {
                                                            "input": "$$fmt_entries",
                                                            "as": "fe",
                                                            "cond": {"$eq": ["$$fe.k", "$$sid"]},
                                                        }
                                                    },
                                                },
                                                "in": {
                                                    "$cond": [
                                                        {"$gt": [{"$size": "$$snap_match"}, 0]},
                                                        {
                                                            "$ne": [
                                                                {"$arrayElemAt": ["$$snap_match.v", 0]},
                                                                "$$cur_p_str",
                                                            ]
                                                        },
                                                        {
                                                            "$and": [
                                                                {"$gt": [{"$size": "$$fmt_match"}, 0]},
                                                                {
                                                                    "$regexMatch": {
                                                                        "input": "$$cur_p_str",
                                                                        "regex": r"^[￥¥]?\s*\d+(\.\d+)?\s*$",
                                                                    }
                                                                },
                                                                {
                                                                    "$let": {
                                                                        "vars": {
                                                                            "num_p": {
                                                                                "$convert": {
                                                                                    "input": {
                                                                                        "$trim": {
                                                                                            "input": "$$cur_p_str",
                                                                                            "chars": "￥¥ \t\r\n",
                                                                                        }
                                                                                    },
                                                                                    "to": "double",
                                                                                    "onError": -1,
                                                                                    "onNull": -1,
                                                                                }
                                                                            },
                                                                            "fmt_obj": {"$arrayElemAt": ["$$fmt_match", 0]},
                                                                        },
                                                                        "in": {
                                                                            "$and": [
                                                                                {"$gt": ["$$num_p", 0]},
                                                                                {
                                                                                    "$or": [
                                                                                        {"$not": [{"$isNumber": "$$fmt_obj.v.price"}]},
                                                                                        {
                                                                                            "$gt": [
                                                                                                {
                                                                                                    "$abs": {
                                                                                                        "$subtract": [
                                                                                                            "$$num_p",
                                                                                                            "$$fmt_obj.v.price",
                                                                                                        ]
                                                                                                    }
                                                                                                },
                                                                                                PRICE_COMPARE_ABS_TOL,
                                                                                            ]
                                                                                        },
                                                                                    ]
                                                                                },
                                                                            ]
                                                                        },
                                                                    }
                                                                },
                                                            ]
                                                        },
                                                    ]
                                                },
                                            }
                                        },
                                    }
                                },
                            }
                        }
                    },
                }
            },
        ]
    }

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
                    {"$expr": specless_price_changed_expr},
                    {"$expr": multi_sku_price_changed_expr},
                    {
                        "$and": [
                            {
                                "$or": [
                                    {"format_status": {"$ne": "success"}},
                                    # 当存在 sku_info 列表且 format_info 未包含对应数量时允许继续补洗
                                    {
                                        "$expr": {
                                            "$gt": [
                                                {"$size": sku_list_expr},
                                                {"$size": fmt_entries_expr},
                                            ]
                                        }
                                    },
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


def pending_deal_format_query(recent_days=RECENT_DAYS):
    """
    候选好价线报商品查询条件（严格隔离于顶层官方 format_info，仅读写 deal_info.format_*）：
    1. 最近一周（recent_days 天）内更新的正式表线报商品（deal_info.is_deal_tip == True）
    2. 具备明确大于 0 的数值型线报到手价（deal_info.deal_price > 0；无确定价格的线报自动跳过不空转）
    3. 满足以下任一情况：
       - 显式标记了 deal_info.need_reformat == True；
       - 曾记录过上次清洗快照（format_deal_price）且当前线报价格或标题已变更（新线报覆盖后自动重洗）；
       - 尚未成功清洗（或缺少 pricing_basis）且累计失败重试次数未达阈值。
    """
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


def build_deal_prompt_text(base_prompt_text=None):
    """复用通用商品结构化清洗提示词，并在末尾挂载线报专属补充约束。"""
    if base_prompt_text is None:
        base_prompt_text = read_file_to_str(PROMPT_FILE_PATH)
    return f"{base_prompt_text.rstrip()}\n\n{DEAL_SUPPLEMENT_RULES}"


def _clean_deal_text(value):
    text = _DEAL_URL_RE.sub("", str(value or ""))
    text = _DEAL_TOKEN_RE.sub("", text)
    return _MULTI_SPACE_RE.sub(" ", text).strip()


def build_deal_product_desc(product):
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


def _is_clean_string(value):
    """拒绝空白、首尾空格和不能编码为 UTF-8 的文本，保护模型输出协议。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_positive_number(value):
    """数量支持整数和小数，但不能把布尔值当数量，也不能接收 NaN/无穷大。"""
    return type(value) in (int, float) and 0 < value <= BSON_MAX_INT64 and math.isfinite(value)


def _check_ranked_items(items, field, text_keys):
    """items 为 [{文本键, score}]；text_keys 指定 name 或 attribute_name/attribute_value，返回 (通过, 原因)。"""
    if not isinstance(items, list):
        return False, f"{field} 必须是列表"
    required_keys = {*text_keys, "score"}
    for index, item in enumerate(items):
        location = f"{field}[{index}]"
        if not isinstance(item, dict) or set(item) != required_keys:
            return False, f"{location} 必须且只能包含 {'、'.join((*text_keys, 'score'))}"
        if not all(_is_clean_string(item[key]) for key in text_keys):
            return False, f"{location} 的 {'、'.join(text_keys)} 必须是非空、无首尾空白的 UTF-8 字符串"
        if type(item["score"]) is not int or not 1 <= item["score"] <= 10:
            return False, f"{location}.score 必须是 1—10 的整数"
    return True, ""


def _check_single_item(format_info, index):
    """
    format_info 必含 sku_id/core_entities/decision_keywords/pricing_basis；返回 (通过, 原因)。
    pricing_basis 必含 is_inferred/structure/total_value/base_unit/equivalent_description，structure 为 [{value, unit}]。
    """
    if not isinstance(format_info, dict):
        return False, f"元素[{index}] 必须是对象"
    prefix = f"元素[{index}](sku_id={format_info.get('sku_id')})"
    if set(format_info) != {"sku_id", "price", "core_entities", "decision_keywords", "pricing_basis"}:
        return False, f"{prefix} 必须且只能包含 sku_id、core_entities、decision_keywords、pricing_basis"

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
    description = pricing["equivalent_description"]
    if description is not None and not _is_clean_string(description):
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
        if not _is_clean_string(layer["unit"]):
            return False, f"{location}.unit 必须是非空、无首尾空白的字符串"
        total *= layer["value"]
    total_value = pricing["total_value"]
    if not _is_positive_number(total_value):
        return False, f"{prefix} pricing_basis.total_value 必须是 (0, BSON_MAX_INT64] 范围内的有限数值"
    if not math.isclose(total_value, total, rel_tol=1e-5):
        return False, f"{prefix} pricing_basis.total_value ({total_value}) 必须等于各层 value 的乘积 ({total})"
    if not _is_clean_string(pricing["base_unit"]) or pricing["base_unit"] != structure[-1]["unit"]:
        return False, f"{prefix} pricing_basis.base_unit 必须与最后一层 unit 一致"
    return True, ""


def check_format_info(format_info_list, input_sku_ids, expected_prices=None):
    """
    模型返回列表形貌为 [{sku_id, core_entities, decision_keywords, pricing_basis}]。
    当提供 expected_prices ({sku_id: float}) 时，额外强校验模型输出 price 未偏离确认价格。
    返回: (至少一项成功, {sku_id: 合格对象}, {sku_id: 失败原因}, 批次整体错误信息)
    """
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


def gen_goods_format_info(sku_task_batch, prompt_text=None, enforce_input_price=False):
    """
    sku_task_batch 为拆解后的任务列表:
    [{sku_id, product_id, product_desc, sku_desc, 价格, product_ref}]
    返回: {status, results: {sku_id: 结构化对象}, item_errors: {sku_id: 原因}, model_used, error}
    """
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

        # 严格按照规范组装大模型入参格式
        inputs.append({
            "product_desc": name,
            "sku_id": clean_sku_id,
            "sku_desc": sku_desc,
            "价格": price,
        })
        input_sku_ids.append(clean_sku_id)

    if prompt_text is None:
        prompt_text = read_file_to_str(PROMPT_FILE_PATH)

    effective_expected_prices = expected_prices if expected_prices else None
    full_prompt = f"{prompt_text}\n<product_input>\n{json.dumps(inputs, ensure_ascii=False)}\n</product_input>"
    errors, content = [], ""
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            result = generate_content(
                prompt=full_prompt,
                preset_model_group="low",
                max_retries_per_model=1,
                timeout=150,
                slot_acquire_timeout=5.0,
            )
            model_used = (result.get("metrics") or {}).get("model_used")
            if model_used:
                outcome["model_used"] = model_used
            if result.get("status") != "✅ 成功":
                detail = "；".join(str(error) for error in result.get("error_history", []) or [])
                raise RuntimeError(detail or result.get("content") or "模型调用失败")
            content = result.get("content", "")
            valid, results, item_errors, batch_error = check_format_info(
                string_to_object(content),
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


def _clean_price_number_str(value):
    """将正数金额格式化为无浮点尾噪的字符串（如 35.4 -> '35.4'，55.0 -> '55'）。"""
    rounded = round(float(value), 4)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _extract_sku_desc(item):
    """提取单个 sku_info 条目的有效规格属性文本（排除 价格、截图地址）。"""
    if not isinstance(item, dict):
        return ""
    desc_parts = [
        f"{k}：{v}" for k, v in item.items()
        if k not in ("价格", "截图地址") and str(v or "").strip()
    ]
    return " | ".join(desc_parts)


def _is_specless_single_sku(sku_info):
    """
    判定商品是否属于“无细分规格属性的单品”：
    1. sku_info 为空 / 非列表；
    2. 或 sku_info 仅含 1 个元素，且除 '价格'、'截图地址' 外无任何有效规格属性（如 [{'价格': '￥41.4', '截图地址': ''}]）。
    此类商品的真实当前成交价统一以顶层 activity_price 为准。
    """
    if not isinstance(sku_info, list) or len(sku_info) == 0:
        return True
    if len(sku_info) == 1:
        return _extract_sku_desc(sku_info[0]) == ""
    return False


def _parse_plain_sku_price(price_text):
    """解析形如 '￥41.4' / '41.4' 的纯数值价格文本；若含区间、多档文案或非正数则返回 None。"""
    match = _PLAIN_PRICE_TEXT_RE.fullmatch(str(price_text or "").strip())
    if not match:
        return None
    val = float(match.group(1))
    return val if _is_positive_number(val) else None


def _detect_product_price_changes(product):
    """
    检测商品当前价格是否与上次清洗时认定的价格发生变动，与 pending_format_query 严格对齐。
    返回: (has_price_mismatch: bool, stale_sku_ids: set[str])
    """
    product_id = str(product.get("product_id") or "").strip()
    if not product_id:
        return False, set()

    sku_info = product.get("sku_info")
    raw_fmt = product.get("format_info")
    existing_fmt = (
        raw_fmt
        if isinstance(raw_fmt, dict) and "pricing_basis" not in raw_fmt
        else {}
    )

    if _is_specless_single_sku(sku_info):
        act_price = product.get("activity_price")
        if not _is_positive_number(act_price):
            return False, set()
        act_float = float(act_price)
        exp_sku_id = (
            f"{product_id}_1"
            if isinstance(sku_info, list) and len(sku_info) == 1
            else f"{product_id}_0"
        )
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

    raw_snap = product.get("format_sku_prices")
    snap_map = raw_snap if isinstance(raw_snap, dict) else {}
    if not existing_fmt and not snap_map:
        return False, set()

    stale_sku_ids = set()
    for idx, item in enumerate(sku_info, start=1):
        sku_id = f"{product_id}_{idx}"
        cur_p_str = str(item.get("价格", "")).strip() if isinstance(item, dict) else ""
        if sku_id in snap_map:
            if str(snap_map[sku_id]) != cur_p_str:
                stale_sku_ids.add(sku_id)
        elif sku_id in existing_fmt:
            plain_p = _parse_plain_sku_price(cur_p_str)
            fmt_entry = existing_fmt.get(sku_id)
            fmt_p = fmt_entry.get("price") if isinstance(fmt_entry, dict) else None
            if plain_p is not None and (
                not _is_positive_number(fmt_p)
                or abs(plain_p - float(fmt_p)) > PRICE_COMPARE_ABS_TOL
            ):
                stale_sku_ids.add(sku_id)

    return bool(stale_sku_ids), stale_sku_ids


def run_format_round(product_manager):
    """
    按 SKU 粒度并发生成结构化信息，支持断点补洗、价格变动自动重洗与增量写回 format_info.{sku_id}。
    """
    started = time.monotonic()
    query_condition = pending_format_query()

    products = product_manager.query(
        query_condition,
        projection={
            "_id": 1,
            "platform": 1,
            "product_id": 1,
            "name": 1,
            "need_reformat": 1,
            "sku_info": 1,
            "activity_price": 1,
            "format_info": 1,
            "format_activity_price": 1,
            "format_sku_prices": 1,
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

    # 1. 将商品按 SKU 拆解为待清洗的任务单元
    all_sku_tasks = []
    total_reformat_prods = 0
    # 记录本轮已累加过失败次数的商品 _id，保证同一商品在同一轮内最多只 +1 次 format_retry_count
    round_failed_docs = set()
    state_lock = threading.Lock()

    for product in products:
        product_id = str(product.get("product_id") or "").strip()
        name = str(product.get("name") or "").strip()
        sku_info = product.get("sku_info")
        is_specless = _is_specless_single_sku(sku_info)
        raw_format_info = product.get("format_info")
        has_invalid_format_info = "format_info" in product and not isinstance(raw_format_info, dict)
        has_legacy_flat_format = isinstance(raw_format_info, dict) and "pricing_basis" in raw_format_info
        existing_format_info = (
            raw_format_info
            if isinstance(raw_format_info, dict) and not has_legacy_flat_format
            else {}
        )
        raw_sku_prices = product.get("format_sku_prices")
        has_invalid_sku_prices = "format_sku_prices" in product and not isinstance(raw_sku_prices, dict)
        existing_sku_prices = raw_sku_prices.copy() if isinstance(raw_sku_prices, dict) else {}

        has_price_mismatch, stale_sku_ids = _detect_product_price_changes(product)
        explicit_reformat = bool(product.get("need_reformat"))
        is_reformat = explicit_reformat or has_price_mismatch
        if is_reformat:
            total_reformat_prods += 1

        if not product_id:
            counts["skipped"] += 1
            counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
            continue

        act_raw = product.get("activity_price")
        if not name or (is_specless and not _is_positive_number(act_raw)):
            fail_patch = {
                "platform": product["platform"],
                "product_id": product_id,
                "need_reformat": False,
                "format_status": "failed",
                "format_error": "缺少可清洗的商品标题" if not name else "单规格商品缺少大于0的有效 activity_price",
                "format_retry_count": FORMAT_MAX_RETRIES,
                "format_updated_at": datetime.now(timezone.utc),
            }
            if is_reformat or has_legacy_flat_format or has_invalid_format_info:
                fail_patch["format_info"] = {}
            if is_specless:
                fail_patch["format_activity_price"] = (
                    float(act_raw)
                    if type(act_raw) in (int, float) and math.isfinite(act_raw)
                    else 0.0
                )
            product_manager.update(
                fail_patch,
                condition={"_id": product["_id"]},
                upsert=False,
            )
            counts["skipped"] += 1
            counts["reformat_skipped" if is_reformat else "normal_skipped"] += 1
            continue

        # 针对重洗（或旧版单层结构、或 format_info/format_sku_prices 非字典）商品，拉取时立即落库重置状态与新价格快照，
        # 既确保重洗拥有全新的重试额度，也防止模型失败时因快照未推进而无限循环重洗。
        if is_reformat or has_legacy_flat_format or has_invalid_format_info or has_invalid_sku_prices:
            if explicit_reformat or has_legacy_flat_format or has_invalid_format_info or is_specless:
                retained_format_info = {}
            else:
                valid_sku_keys = {f"{product_id}_{i}" for i in range(1, len(sku_info) + 1)}
                retained_format_info = {
                    k: v for k, v in existing_format_info.items()
                    if k in valid_sku_keys and k not in stale_sku_ids
                }

            reset_patch = {
                "platform": product["platform"],
                "product_id": product_id,
                "need_reformat": False,
                "format_status": "success" if retained_format_info else "pending",
                "format_info": retained_format_info,
                "format_retry_count": 0,
            }
            if is_specless:
                act_float = float(act_raw)
                reset_patch["format_activity_price"] = act_float
                if isinstance(sku_info, list) and len(sku_info) == 1:
                    shot_path = str(sku_info[0].get("截图地址") or "") if isinstance(sku_info[0], dict) else ""
                    synced_sku_info = [{"价格": f"￥{_clean_price_number_str(act_float)}", "截图地址": shot_path}]
                    reset_patch["sku_info"] = synced_sku_info
                    sku_info = synced_sku_info
                    product["sku_info"] = synced_sku_info
            else:
                if explicit_reformat or has_invalid_sku_prices:
                    new_sku_prices = {}
                else:
                    valid_sku_keys = {f"{product_id}_{i}" for i in range(1, len(sku_info) + 1)}
                    new_sku_prices = {
                        k: v for k, v in existing_sku_prices.items()
                        if k in valid_sku_keys
                    }
                for sid in stale_sku_ids:
                    idx_num = int(sid.rsplit("_", 1)[1]) - 1
                    if 0 <= idx_num < len(sku_info) and isinstance(sku_info[idx_num], dict):
                        new_sku_prices[sid] = str(sku_info[idx_num].get("价格", "")).strip()
                reset_patch["format_sku_prices"] = new_sku_prices
                existing_sku_prices = new_sku_prices
                product["format_sku_prices"] = existing_sku_prices

            existing_format_info = retained_format_info
            product["format_info"] = existing_format_info
            product["need_reformat"] = False
            product_manager.update(
                reset_patch,
                condition={"_id": product["_id"]},
                upsert=False,
            )
        else:
            product["format_info"] = existing_format_info

        product_pending_tasks = []
        if is_specless:
            total_expected = 1
            has_one_placeholder = isinstance(sku_info, list) and len(sku_info) == 1
            sku_id = f"{product_id}_1" if has_one_placeholder else f"{product_id}_0"
            if sku_id not in existing_format_info:
                act_float = float(act_raw)
                act_price_str = _clean_price_number_str(act_float)
                synced_sku_info = None
                if has_one_placeholder:
                    shot_path = str(sku_info[0].get("截图地址") or "") if isinstance(sku_info[0], dict) else ""
                    cur_sku0_p = str(sku_info[0].get("价格") or "").strip() if isinstance(sku_info[0], dict) else ""
                    target_sku0_p = f"￥{act_price_str}"
                    if cur_sku0_p != target_sku0_p:
                        synced_sku_info = [{"价格": target_sku0_p, "截图地址": shot_path}]
                product_pending_tasks.append({
                    "sku_id": sku_id,
                    "product_id": product_id,
                    "product_desc": name,
                    "sku_desc": "",
                    "价格": act_price_str,
                    "expected_price": act_float,
                    "sync_specless_sku_info": synced_sku_info,
                    "record_sku_price_str": None,
                    "product_ref": product,
                    "total_expected_skus": 1,
                    "is_reformat": is_reformat,
                })
        else:
            total_expected = len(sku_info)
            for idx, item in enumerate(sku_info, start=1):
                sku_id = f"{product_id}_{idx}"
                if sku_id in existing_format_info:
                    continue

                sku_desc = _extract_sku_desc(item)
                price_str = str(item.get("价格", "")).strip() if isinstance(item, dict) else ""

                product_pending_tasks.append({
                    "sku_id": sku_id,
                    "product_id": product_id,
                    "product_desc": name,
                    "sku_desc": sku_desc,
                    "价格": price_str,
                    "expected_price": None,
                    "sync_specless_sku_info": None,
                    "record_sku_price_str": price_str,
                    "product_ref": product,
                    "total_expected_skus": total_expected,
                    "is_reformat": is_reformat,
                })

        # 如果该商品下所有规格其实已在上一轮全部生成完毕，直接补标为 success，防止空转
        if not product_pending_tasks and len(existing_format_info) >= total_expected:
            done_patch = {
                "platform": product["platform"],
                "product_id": product_id,
                "format_status": "success",
                "need_reformat": False,
                "format_error": "",
                "format_updated_at": datetime.now(timezone.utc),
            }
            if is_specless:
                done_patch["format_activity_price"] = float(act_raw)
            product_manager.update(
                done_patch,
                condition={"_id": product["_id"]},
                upsert=False,
            )
        else:
            all_sku_tasks.extend(product_pending_tasks)

    if not all_sku_tasks:
        logger.info("[调度/完成] 查到候选商品，其下规格均已生成完毕并已同步状态 | 耗时: [%.2f 秒]", time.monotonic() - started)
        return counts

    prompt_text = read_file_to_str(PROMPT_FILE_PATH)

    # 2. 将规格任务按 FORMAT_BATCH_SIZE 切批，同一批次内 sku_id 互不重复
    batches, current_batch, batch_sku_ids = [], [], set()
    for task in all_sku_tasks:
        if len(current_batch) == FORMAT_BATCH_SIZE or task["sku_id"] in batch_sku_ids:
            batches.append(current_batch)
            current_batch, batch_sku_ids = [], set()
        current_batch.append(task)
        batch_sku_ids.add(task["sku_id"])
    if current_batch:
        batches.append(current_batch)

    logger.info(
        "[调度/本轮] 开始格式化规格 | 涉及商品: [%d] (常规: %d, 重洗: %d) | 待洗SKU数: [%d] | 批次: [%d] | 并发: [%d]",
        len(products),
        len(products) - total_reformat_prods,
        total_reformat_prods,
        len(all_sku_tasks),
        len(batches),
        FORMAT_WORKERS,
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
            total_expected_skus = task["total_expected_skus"]

            format_info_item = generated["results"].get(sku_id)
            is_success = format_info_item is not None
            error = "" if is_success else (
                generated["error"] if generated["status"] == "failed"
                else generated["item_errors"].get(sku_id, "未知的规格解析错误")
            )

            condition = {"_id": doc_id}
            update_fields = {
                "platform": product["platform"],
                "product_id": product_id,
                "format_model": generated["model_used"],
                "format_updated_at": datetime.now(timezone.utc),
            }
            if task.get("expected_price") is not None:
                update_fields["format_activity_price"] = task["expected_price"]
                if task.get("sync_specless_sku_info") is not None:
                    update_fields["sku_info"] = task["sync_specless_sku_info"]
            if task.get("record_sku_price_str") is not None:
                update_fields[f"format_sku_prices.{sku_id}"] = task["record_sku_price_str"]

            increments_arg = None
            with state_lock:
                fmt_map = product.setdefault("format_info", {})
                if is_success:
                    update_fields[f"format_info.{sku_id}"] = format_info_item
                    fmt_map[sku_id] = format_info_item
                    # 只要已有有效规格成功入库，即标记 format_status 为 success（未齐的规格靠 $size 条件在后续轮次补洗）
                    update_fields["format_status"] = "success"
                    update_fields["need_reformat"] = False
                    if len(fmt_map) >= total_expected_skus:
                        update_fields["format_error"] = ""
                        update_fields["format_retry_count"] = 0
                else:
                    # 仅当该商品目前没有任何已成功规格时才标为 failed，防止单个异常规格屏蔽整件商品的其它正常规格
                    update_fields["format_status"] = "success" if fmt_map else "failed"
                    update_fields["format_error"] = error
                    # 同一商品在本轮最多只累加 1 次重试计数，防止多规格商品在单轮内直接累加满 3 次
                    if doc_id not in round_failed_docs:
                        round_failed_docs.add(doc_id)
                        increments_arg = {"format_retry_count": 1}

            saved = product_manager.update(
                update_fields,
                condition=condition,
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
        log = logger.error if all_failed else logger.warning if failed or batch_counts["skipped"] else logger.info
        message = "❌ [规格/批次完成] 全批生成失败" if all_failed else "[规格/批次完成] 规格结果已增量写入"
        detail = (
            " ".join(generated["error"].split())[:800]
            if generated["status"] == "failed"
            else "; ".join(failure_details)[:800]
        )

        log(
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

    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=FORMAT_WORKERS) as executor:
        futures = {}
        for batch in batches:
            batch_counts = {
                "success": 0, "failed": 0, "skipped": 0,
                "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
                "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0,
            }
            futures[executor.submit(process_batch, batch, batch_counts)] = (batch, batch_counts)

        for future in as_completed(futures):
            batch, batch_counts = futures[future]
            try:
                future.result()
            except Exception as exc:
                unfinished = len(batch) - (batch_counts["success"] + batch_counts["failed"] + batch_counts["skipped"])
                unexpected_errors += unfinished
                logger.exception(
                    "❌ [规格/批次异常] 本批未完成 | 首个 SKU: [%s] | 未完成: [%d] | 原因: %s",
                    batch[0]["sku_id"],
                    unfinished,
                    exc,
                )

            for key in counts.keys():
                counts[key] += batch_counts[key]

    log = logger.warning if unexpected_errors else logger.info
    log(
        "[调度/完成] 本轮处理结束 | 规格成功/失败/跳过: [%d/%d/%d] | 常规: [%d/%d/%d] | 重洗: [%d/%d/%d] | 耗时: [%.2f 秒]",
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

    return counts


def run_deal_format_round(product_manager, limit=DEAL_FORMAT_ROUND_LIMIT):
    """
    批量清洗正式表中好价线报（deal_info），结果独立写回 deal_info.format_info，绝不触碰顶层官方 format_info。
    """
    started = time.monotonic()
    query_condition = pending_deal_format_query()

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

    batches, current_batch, batch_sku_ids = [], [], set()
    for task in deal_tasks:
        if len(current_batch) == FORMAT_BATCH_SIZE or task["sku_id"] in batch_sku_ids:
            batches.append(current_batch)
            current_batch, batch_sku_ids = [], set()
        current_batch.append(task)
        batch_sku_ids.add(task["sku_id"])
    if current_batch:
        batches.append(current_batch)

    logger.info(
        "[线报调度/本轮] 开始格式化好价线报 | 待洗数: [%d] (常规: %d, 重洗: %d) | 批次: [%d] | 并发: [%d]",
        len(deal_tasks),
        len(deal_tasks) - total_reformat_deals,
        total_reformat_deals,
        len(batches),
        DEAL_FORMAT_WORKERS,
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
        log = logger.error if all_failed else logger.warning if failed or batch_counts["skipped"] else logger.info
        message = "❌ [线报/批次完成] 全批生成失败" if all_failed else "[线报/批次完成] 线报结果已增量写入"
        detail = (
            " ".join(generated["error"].split())[:800]
            if generated["status"] == "failed"
            else "; ".join(failure_details)[:800]
        )

        log(
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

    unexpected_errors = 0
    with ThreadPoolExecutor(max_workers=DEAL_FORMAT_WORKERS) as executor:
        futures = {}
        for batch in batches:
            batch_counts = {
                "success": 0, "failed": 0, "skipped": 0,
                "normal_success": 0, "normal_failed": 0, "normal_skipped": 0,
                "reformat_success": 0, "reformat_failed": 0, "reformat_skipped": 0,
            }
            futures[executor.submit(process_deal_batch, batch, batch_counts)] = (batch, batch_counts)

        for future in as_completed(futures):
            batch, batch_counts = futures[future]
            try:
                future.result()
            except Exception as exc:
                unfinished = len(batch) - (batch_counts["success"] + batch_counts["failed"] + batch_counts["skipped"])
                unexpected_errors += unfinished
                logger.exception(
                    "❌ [线报/批次异常] 本批未完成 | 首个 SKU: [%s] | 未完成: [%d] | 原因: %s",
                    batch[0]["sku_id"],
                    unfinished,
                    exc,
                )

            for key in counts.keys():
                counts[key] += batch_counts[key]

    log = logger.warning if unexpected_errors else logger.info
    log(
        "[线报调度/完成] 本轮处理结束 | 线报成功/失败/跳过: [%d/%d/%d] | 常规: [%d/%d/%d] | 重洗: [%d/%d/%d] | 耗时: [%.2f 秒]",
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

    return counts


def _task_worker_loop(task_name, round_func):
    """
    通用的后台任务守护循环。
    复用健康连接，按固定间隔执行一轮；
    每个线程维护自己独立的数据库连接对象，避免多线程下的网络读写冲突。
    """
    db_instance = product_manager = None
    try:
        while True:
            try:
                if product_manager is None:
                    if db_instance is not None:
                        db_instance.close()
                        db_instance = None
                    db_instance = gen_db_object()
                    db_instance.ping()
                    product_manager = ProductManager(db_instance)

                # 执行具体的一轮业务逻辑
                round_func(product_manager)

            except Exception as exc:
                product_manager = None
                logger.exception(
                    "❌ [%s调度/异常] 本轮终止，下轮重新连接 | 间隔: [%d 秒] | 原因: [%s] "
                    "| 排查: [数据库、候选查询与线程池或网络请求]",
                    task_name,
                    ROUND_INTERVAL_SECONDS,
                    " ".join(str(exc).split())[:400],
                )

            logger.info("[%s调度/等待] 本轮结束 | 等待: [%d 秒]", task_name, ROUND_INTERVAL_SECONDS)
            time.sleep(ROUND_INTERVAL_SECONDS)

    finally:
        if db_instance is not None:
            db_instance.close()


def format_task():
    """将商品格式化任务包装为无参函数，便于任务列表管理"""
    _task_worker_loop("格式化", run_format_round)


def deal_format_task():
    """将好价线报格式化任务包装为无参函数，独立写入 deal_info.format_info"""
    _task_worker_loop("线报格式化", run_deal_format_round)


def _run_task(task):
    """为后台入口的未处理异常补充上下文并重抛；保留线程退出、不自动重启的行为。"""
    try:
        task()
    except Exception:
        logger.exception(
            "[任务/退出] 后台任务异常结束 | 任务: [%s] | 结果: [当前线程停止] "
            "| 排查: [检查对应链路的数据、文件权限及外部服务]",
            task.__name__,
        )
        raise


if __name__ == "__main__":
    tasks = [
        format_task,
        deal_format_task,
    ]

    threads = []
    for task in tasks:
        thread = threading.Thread(target=_run_task, args=(task,), name=task.__name__)
        thread.daemon = True  # 设置为守护线程，这样主线程因中断退出时，所有任务也会立即中止
        thread.start()
        threads.append(thread)
        logger.info("[系统/启动] 已启动 %s 线程 (TID: %d)", task.__name__, thread.ident)

    try:
        # 使用带 timeout 的 join 轮询，避免完全阻塞主线程，使得 Ctrl+C 中断信号能够被正常捕获
        for thread in threads:
            while thread.is_alive():
                thread.join(1.0)
    except KeyboardInterrupt:
        logger.info("[系统/退出] 收到中断信号，正在停止所有后台并行任务...")
