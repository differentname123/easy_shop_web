# -*- coding: utf-8 -*-
"""闲鱼交易与发货售后模板单一真源（SSOT）、多选有序拼装及第三方电商平台词清洗。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

DEFAULT_TEMPLATE_ID = "direct_fast"
DEFAULT_TEMPLATE_IDS: List[str] = [DEFAULT_TEMPLATE_ID]
NONE_TEMPLATE_ID = "none"

# 精选 8 个全品类全价位通用模板（5 个吸引买家亮点 + 3 个提前说清须知；零售后赔付引导，支持多选按序空行分隔拼接）
COPY_TEMPLATES: List[Dict[str, str]] = [
    {
        "id": "direct_fast",
        "group": "attract",
        "group_label": "吸引与服务承诺",
        "name": "全自动秒发",
        "badge": "默认推荐",
        "summary": "24h自动系统接单锁发，同步单号、非偏远包邮",
        "content": "已接入24小时全自动下单系统，有货直接拍就行，拍下后极速锁单发出并自动同步快递单号，非偏远地区包邮。",
    },
    {
        "id": "brand_auth",
        "group": "attract",
        "group_label": "吸引与服务承诺",
        "name": "官旗查单保真",
        "badge": "透明溯源",
        "summary": "官旗直发不经手，全新原封正品可发带地址订单截图",
        "content": "品牌官方旗舰店直发，不经个人转手，全新原封正品；拍下可提供带您收货地址的官方订单截图，透明更放心。",
    },
    {
        "id": "spec_note",
        "group": "attract",
        "group_label": "吸引与服务承诺",
        "name": "规格款式任选",
        "badge": "自由搭配",
        "summary": "多款式/型号/规格支持备注或私聊选款，未备注发主图款",
        "content": "有多种款式、型号或规格需要的朋友，拍下时留言备注或私聊告诉我要哪款就行，未备注默认发主图经典款。",
    },
    {
        "id": "fresh_batch",
        "group": "attract",
        "group_label": "吸引与服务承诺",
        "name": "官方全新批次",
        "badge": "拒绝旧货",
        "summary": "官方仓按近期最新出库批次直发，全新未拆非积压旧货",
        "content": "官方仓按近期最新出库批次直发，全新未拆封，不是积压旧货，日常自用或囤货都很放心。",
    },
    {
        "id": "vip_service",
        "group": "attract",
        "group_label": "吸引与服务承诺",
        "name": "其他商品问价",
        "badge": "跨品好价引流",
        "summary": "引导买家私信发图/发商品名查更多品类惊喜好价，多接单",
        "content": "平时有其他想买的商品，不管哪类都可以直接发图或名称私信问我，我这边一般都有惊喜好价，多问不吃亏～",
    },
    {
        "id": "no_refund",
        "group": "rules",
        "group_label": "提前说清免纠纷",
        "name": "特价不议不退",
        "badge": "诚意透明",
        "summary": "微利走量不议价，仓发快不改地址，非质量不退不换",
        "content": "本身微利走量，标价已经是诚意特惠价啦，不议价见谅；因直发仓出库很快无法拦截或改地址，拍前请核对好信息，发出后非质量问题不退不换。",
    },
    {
        "id": "shipping_extra",
        "group": "rules",
        "group_label": "提前说清免纠纷",
        "name": "偏远邮差说明",
        "badge": "运费透明",
        "summary": "明确新藏蒙青宁甘琼港澳台需先私聊补差价，其余包邮",
        "content": "非偏远地区全部包邮！新疆、西藏、内蒙古、青海、宁夏、甘肃、海南及港澳台运费确实较高，拍前请先私聊补少量邮差，请勿直接拒收哦。",
    },
    {
        "id": "order_guide",
        "group": "rules",
        "group_label": "提前说清免纠纷",
        "name": "拍前先私聊",
        "badge": "锁库存不跑空",
        "summary": "活动名额和地区库存变动快，拍前问一句确认有货或领入口",
        "content": "活动名额和部分地区库存实时都在变动，想要的朋友拍前先私聊问我一声，立刻帮你查好库存或发专属下单方式。",
    },
]

_TEMPLATE_BY_ID: Dict[str, Dict[str, str]] = {t["id"]: t for t in COPY_TEMPLATES}

# 先匹配复合店铺/自营表达并替换为中性官方表述，再剥离残余第三方电商平台名称
_COMPOUND_PLATFORM_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:京东自营|天猫超市|天猫国际|京东国际|拼多多百亿补贴|淘宝百亿补贴)(?:官方)?旗舰店直发"), "官方旗舰店直发"),
    (re.compile(r"(?:京东自营|天猫超市|天猫国际|京东国际|拼多多百亿补贴|淘宝百亿补贴)(?:官方)?旗舰店"), "官方旗舰店"),
    (re.compile(r"(?:京东|天猫|淘宝|拼多多|抖店|快手)自营官方(?:旗舰)?店"), "官方旗舰店"),
    (re.compile(r"(?:京东|天猫|淘宝|拼多多|抖店|快手)(?:官方)?旗舰店"), "官方旗舰店"),
    (re.compile(r"(?:京东|天猫|淘宝|拼多多)自营(?:官方)?店"), "官方直发店"),
    (re.compile(r"京东指数交易官方店"), "官方直发店"),
    (re.compile(r"(?:京东自营|天猫超市|天猫国际|京东国际)直发"), "官方直发"),
    (re.compile(r"(?:京东|天猫|淘宝|拼多多)官方直发"), "官方直发"),
    (re.compile(r"(?:京东自营|天猫超市|天猫国际|京东国际)"), "官方直发"),
    (re.compile(r"(?:拼多多|多多|淘宝|京东|天猫)「?品牌好货」?(?:店铺)?"), "品牌好货渠道"),
    (re.compile(r"品牌方?天猫|天猫官旗"), "品牌官旗"),
)

_STANDALONE_PLATFORM_RE = re.compile(
    r"(?:拼多多|淘宝网|淘宝|天猫|猫超|京东|抖店|抖音商城|快手小店|1688|阿里巴巴|唯品会|得物|\bPDD\b|\bJD\b)",
    re.IGNORECASE,
)


def list_copy_templates() -> List[Dict[str, Any]]:
    """返回供前端展示与用户多选搭配的精选短模板列表（含默认标记）。"""
    return [
        {
            **tpl,
            "is_default": tpl["id"] == DEFAULT_TEMPLATE_ID,
        }
        for tpl in COPY_TEMPLATES
    ]


def normalize_template_id(raw: Any, *, default: str = DEFAULT_TEMPLATE_ID) -> str:
    """校验并规范化单个模板 ID；空值/None 自动回落到默认模板 `direct_fast`。"""
    clean = str(raw or "").strip().lower()
    if not clean:
        return default
    if clean == NONE_TEMPLATE_ID or clean in _TEMPLATE_BY_ID:
        return clean
    raise ValueError(f"不支持的话术模板: {raw}")


def normalize_template_ids(
    raw: Any,
    *,
    default: Optional[List[str]] = None,
) -> List[str]:
    """
    校验并规范化多选模板 ID 列表（严格保留用户选择先后顺序并去重）。
    - `raw is None`: 回落到 `default`（默认 `[DEFAULT_TEMPLATE_ID]`）；
    - `raw == []` 或 `["none"]` / `"none"`: 返回空列表 `[]`（表示用户显式关闭所有模板）；
    - 存在未知 ID: 抛出 ValueError。
    """
    if raw is None:
        return list(default if default is not None else DEFAULT_TEMPLATE_IDS)
    if isinstance(raw, str):
        parts = [x.strip().lower() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(x or "").strip().lower() for x in raw]
    else:
        raise ValueError(f"话术模板列表格式无效: {raw}")

    ordered: List[str] = []
    seen = set()
    for item in parts:
        if not item or item == NONE_TEMPLATE_ID:
            continue
        if item not in _TEMPLATE_BY_ID:
            raise ValueError(f"不支持的话术模板: {item}")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def resolve_listing_template_ids(doc: Optional[Dict[str, Any]]) -> List[str]:
    """
    从商品文档兼容解析最终生效的模板 ID 列表：
    1. 优先读取 `template_ids`（即便是 `[]` 也尊重用户明确取消所有模板的意图）；
    2. 否则向后兼容读取旧字段 `template_id`；都未配置时默认使用 `["direct_fast"]`。
    """
    src = doc if isinstance(doc, dict) else {}
    if "template_ids" in src and src.get("template_ids") is not None:
        try:
            return normalize_template_ids(src.get("template_ids"))
        except ValueError:
            return list(DEFAULT_TEMPLATE_IDS)
    if "template_id" in src and src.get("template_id") is not None:
        try:
            single = normalize_template_id(src.get("template_id"))
        except ValueError:
            return list(DEFAULT_TEMPLATE_IDS)
        return [] if single == NONE_TEMPLATE_ID else [single]
    return list(DEFAULT_TEMPLATE_IDS)


def _coerce_template_ids(template_ids_or_id: Any) -> List[str]:
    if template_ids_or_id is None:
        return list(DEFAULT_TEMPLATE_IDS)
    if isinstance(template_ids_or_id, (list, tuple, set)):
        return normalize_template_ids(template_ids_or_id)
    single = normalize_template_id(template_ids_or_id)
    return [] if single == NONE_TEMPLATE_ID else [single]


def get_template_content(template_id: Any) -> str:
    """读取单个模板正文；`none` 返回空字符串。"""
    norm_id = normalize_template_id(template_id)
    if norm_id == NONE_TEMPLATE_ID:
        return ""
    return _TEMPLATE_BY_ID[norm_id]["content"]


def get_template_name(template_id: Any) -> str:
    """读取单个模板展示名称。"""
    norm_id = normalize_template_id(template_id)
    if norm_id == NONE_TEMPLATE_ID:
        return "不用模板"
    return _TEMPLATE_BY_ID[norm_id]["name"]


def get_combined_template_content(template_ids_or_id: Any) -> str:
    """按选定顺序将多条模板正文用空行 `\\n\\n` 分段拼接，确保排版疏朗好读不密集。"""
    ids = _coerce_template_ids(template_ids_or_id)
    parts = [_TEMPLATE_BY_ID[tid]["content"].strip() for tid in ids if tid in _TEMPLATE_BY_ID]
    return "\n\n".join(p for p in parts if p)


def get_combined_template_name(template_ids_or_id: Any) -> str:
    """返回多选模板的展示名称摘要（如 `现货速发 + 拍下不退`；空选返回 `不用模板`）。"""
    ids = _coerce_template_ids(template_ids_or_id)
    if not ids:
        return "不用模板"
    return " + ".join(_TEMPLATE_BY_ID[tid]["name"] for tid in ids if tid in _TEMPLATE_BY_ID)


def compose_full_description(product_desc: str, template_ids_or_id: Any = None) -> str:
    """将纯商品介绍正文与选定的一组有序交易模板按 `\\n\\n` 拼装成最终发布正文。"""
    body = str(product_desc or "").strip()
    footer = get_combined_template_content(template_ids_or_id).strip()
    if not footer:
        return body
    if not body:
        return footer
    if body.endswith(footer):
        return body
    return f"{body}\n\n{footer}"


def strip_platform_names(text: Any) -> str:
    """清除第三方电商平台字样（京/淘/拼/猫超等），保留安全的“官方旗舰店/官方直发”表述。"""
    s = str(text or "")
    if not s:
        return ""
    for pattern, repl in _COMPOUND_PLATFORM_REPLACEMENTS:
        s = pattern.sub(repl, s)
    s = _STANDALONE_PLATFORM_RE.sub("", s)
    # 清理去词后可能遗留的连续空白与孤立括号，保留原有换行段落结构
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in s.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [re.sub(r"[（(\[【]\s*[)）\]】]", "", ln).strip() for ln in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
