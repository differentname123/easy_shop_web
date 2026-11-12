# -*- coding: utf-8 -*-
"""
本地商品库 (`crawler_data.products`) -> 云端商品库 (`crawler_data.products`) 定时检查合并同步脚本
================================================================================================
核心机制（遵循极简、幂等、可靠原则）：
  1. 范围选取：
     - 启动首轮（及后续每 20 轮 / 约 1 小时兜底对账，或 `--once` 单次模式）：完整检查本地 `products` 集合中最近 N 天（默认 3 天）内有新增、详情更新或 AI 规格/线报清洗变动的全部有效可展示商品（不局限于线报商品）；
     - 守护进程常规轮次（默认每 3 分钟）：自动采用带 120 秒安全重叠的滚动增量时间窗，毫秒级捕获本轮新变动商品，极致节省云端公网带宽。
  2. 双库指纹检查：按唯一联合主键 `(platform, product_id)` 分批查询云端 `products` 现有轻量状态指纹（服务端直接计算 `_fmt_count`，不拉取大字段），仅筛选出「云端缺失需新增」或「本地有新变动需合并」的商品，杜绝重复无效写库。
  3. 字段级安全合并（以本地为准 + 云端关键字段保护）：
     - 本地有的有效业务字段直接通过 `$set` 覆盖/补齐到云端，云端独有的已有字段（如本地纯线报缺失的 `channel_type`）天然保留不被抹除；
     - 严格剔除不可跨库覆盖的云端专属字段：`_id`、`created_at`（仅首次插入写入 `$setOnInsert`）及云端短效转链缓存 `default_promotion*`；
     - 未完成清洗（`format_status != "success"` 或 `format_info` 为空）的半成品规格字段不同步；
     - 若云端同款商品刚刚被线上实时刷新过（`云端 updated_at > 本地 updated_at`），保留云端更新鲜的实时价格与动态 `goods_sign`，仅把本地新增/更新的 `format_info`、`deal_info`、`channel_type` 等资产合并上去。
  4. 表级硬隔离：死锁仅操作 `products` 集合，绝不触碰云端 `users`、`orders`、`order_sync_state`、`pdd_multi_groups` 等线上业务表。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit, urlunsplit

from bson.decimal128 import Decimal128
from pymongo import MongoClient, UpdateOne

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CURRENT_DIR = Path(__file__).resolve().parent
MOBILE_WEB_DIR = CURRENT_DIR.parent
WORKSPACE_ROOT = MOBILE_WEB_DIR.parent
for _p in (str(MOBILE_WEB_DIR), str(WORKSPACE_ROOT), str(CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from infrastructure.mongo import gen_db_object
from infrastructure.settings import DEFAULT_CONFIG_FILE
from models.product import clean_product_id, normalize_platform
from repositories.product_repo import ProductRepository

COLLECTION_NAME = "products"
DEFAULT_SYNC_WINDOW_DAYS = 3.0
DEFAULT_SYNC_INTERVAL_SECONDS = 180        # 默认每 3 分钟检查合并一轮
FULL_SWEEP_EVERY_N_CYCLES = 20             # 守护模式下每 20 轮（约 1 小时）做一次完整 3 天窗口兜底对账
ROLLING_OVERLAP_SECONDS = 120              # 滚动增量窗口重叠冗余秒数，防止并发写入边界漏扫
DEFAULT_BATCH_SIZE = 200
DEFAULT_QUERY_CHUNK_SIZE = 500
DEFAULT_CONNECT_TIMEOUT_MS = 10000
DEFAULT_SOCKET_TIMEOUT_MS = 60000
PROGRESS_LOG_STEP = 2000

LOG_DIR = MOBILE_WEB_DIR / "logs"
LOG_FILE_PATH = LOG_DIR / "product_cloud_sync.log"
LATEST_SYNC_REPORT_PATH = LOG_DIR / "latest_cloud_sync_report.json"

# 永远不从本地覆盖到云端的物理主键、创建时间与云端短效转链缓存字段
NEVER_OVERWRITE_FIELDS = frozenset({
    "_id",
    "created_at",
    "default_promotion",
    "default_promotion_updated_at",
    "default_promotion_expires_at",
})

# 外层规格结构化清洗相关字段：仅当 format_status == "success" 且 format_info 非空时才向云端同步
FORMAT_FAMILY_FIELDS = frozenset({
    "format_info",
    "format_status",
    "format_updated_at",
    "format_model",
    "format_error",
    "format_retry_count",
    "format_activity_price",
    "format_sku_prices",
    "need_reformat",
})

# 云端实时商业快照字段：仅当云端 updated_at 比本地更新鲜时受保护，防止本地旧快照回退线上实时价格和动态加密签名
CLOUD_LIVE_COMMERCIAL_FIELDS = frozenset({
    "price",
    "original_price",
    "activity_price",
    "saved_price",
    "coupon_amount",
    "has_coupon",
    "commission_rate",
    "commission_amount",
    "sales_tip",
    "goods_sign",
    "product_url",
    "search_id",
    "coupon_info",
    "updated_at",
})

# 商品富详情字段：当云端 detail_updated_at 比本地更新鲜时受保护
CLOUD_LIVE_DETAIL_FIELDS = frozenset({
    "desc",
    "carousel_images",
    "specs",
    "detail_tags",
    "unified_tags",
    "service_guarantees",
    "shop_ratings",
    "detail_updated_at",
})

# 双库检查阶段使用的超轻量状态指纹投影（服务端计算 _fmt_count，免传输数 KB 的 format_info 原文）
FINGERPRINT_PROJECTION: Dict[str, Any] = {
    "_id": 0,
    "platform": 1,
    "product_id": 1,
    "goods_id": 1,
    "updated_at": 1,
    "detail_updated_at": 1,
    "channel_type": 1,
    "format_status": 1,
    "format_updated_at": 1,
    "_fmt_count": {
        "$cond": [
            {"$eq": [{"$type": "$format_info"}, "object"]},
            {"$size": {"$objectToArray": "$format_info"}},
            0,
        ]
    },
    "deal_info.is_deal_tip": 1,
    "deal_info.source_item_id": 1,
    "deal_info.quality_score": 1,
    "deal_info.deal_price": 1,
    "deal_info.format_status": 1,
    "deal_info.format_updated_at": 1,
}

_LOCAL_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def setup_sync_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sync_logger = logging.getLogger("product_cloud_sync")
    sync_logger.setLevel(logging.INFO)
    sync_logger.propagate = False
    if not sync_logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        fh = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        sync_logger.addHandler(sh)
        sync_logger.addHandler(fh)
    return sync_logger


def mask_mongo_uri(uri: str) -> str:
    """日志脱敏：隐藏 MongoDB URI 中的密码明文。"""
    raw = str(uri or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.password is None:
            return raw
        user = parts.username or ""
        host = parts.hostname or ""
        port_part = f":{parts.port}" if parts.port else ""
        netloc = f"{user}:***@{host}{port_part}" if user else f"{host}{port_part}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return re.sub(r"(mongodb(?:\+srv)?://[^:/@]+:)([^@]+)(@)", r"\1***\3", raw)


def resolve_cloud_mongo_target(
    config_file: Optional[Path] = None,
    explicit_uri: Optional[str] = None,
) -> Tuple[str, str, str, int]:
    """
    解析云端 MongoDB 连接 URI、目标库名、主机与端口。
    优先级：命令行 `--cloud-uri` > `config.local.json` 中的 `cloud_mongo_uri` > `remote_mongo_host` 组合。
    """
    cfg_path = Path(config_file) if config_file else DEFAULT_CONFIG_FILE
    values: Dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                values = loaded

    uri = str(explicit_uri or values.get("cloud_mongo_uri") or "").strip()
    default_db = str(values.get("cloud_mongo_db_name") or values.get("local_mongo_db_name") or "crawler_data").strip()

    if not uri:
        remote_host = str(values.get("remote_mongo_host") or "").strip()
        if not remote_host:
            raise ValueError(
                f"未找到云端数据库配置：请在 {cfg_path} 中配置 `cloud_mongo_uri`，或通过 `--cloud-uri` 传入"
            )
        port = int(values.get("cloud_mongo_port") or values.get("local_mongo_port") or 27017)
        user = str(values.get("cloud_mongo_user") or values.get("local_mongo_user") or "").strip()
        password = str(values.get("cloud_mongo_password") or values.get("local_mongo_password") or "")
        auth_source = str(values.get("mongo_auth_source") or "admin").strip()
        if user and password:
            u_enc = quote_plus(user)
            p_enc = quote_plus(password)
            uri = f"mongodb://{u_enc}:{p_enc}@{remote_host}:{port}/{default_db}?authSource={auth_source}"
        else:
            uri = f"mongodb://{remote_host}:{port}/{default_db}"

    parsed = urlsplit(uri)
    if parsed.scheme not in ("mongodb", "mongodb+srv") or not parsed.hostname:
        raise ValueError(f"非法的云端 MongoDB URI 格式: {mask_mongo_uri(uri)}")

    path_db = (parsed.path or "").strip("/")
    db_name = path_db if path_db and path_db != "admin" else default_db
    host = parsed.hostname.strip().lower()
    port = int(parsed.port or 27017)

    local_host = str(values.get("local_mongo_host") or "localhost").strip().lower()
    local_port = int(values.get("local_mongo_port") or 27017)
    local_db = str(values.get("local_mongo_db_name") or "crawler_data").strip()

    both_loopback = (host in _LOCAL_LOOPBACK_HOSTS) and (local_host in _LOCAL_LOOPBACK_HOSTS)
    same_endpoint = (host == local_host or both_loopback) and port == local_port and db_name == local_db
    if same_endpoint:
        raise ValueError(
            f"安全拦截：云端目标库 ({host}:{port}/{db_name}) 与本地源库指向同一实例，禁止自同步覆盖"
        )

    return uri, db_name, host, port


def to_utc_millis(value: Any) -> Optional[int]:
    """将 naive UTC / aware datetime 统一转换为 BSON Date 毫秒级整数时间戳，消除微秒截断假差异与时区报错。"""
    if not isinstance(value, datetime.datetime):
        return None
    dt = value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value.astimezone(datetime.timezone.utc)
    return int(math.floor(dt.timestamp() * 1000))


def _number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value.to_decimal() if isinstance(value, Decimal128) else value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(num, 4) if math.isfinite(num) else None


def is_valid_syncable_product(doc: Dict[str, Any]) -> bool:
    """校验文档是否具备有效身份与可展示内容（排除仅含 ID/转链缓存的身份裸记录）。"""
    if not isinstance(doc, dict):
        return False
    platform = normalize_platform(doc.get("platform") or "")
    product_id = clean_product_id(doc.get("product_id") or doc.get("goods_id"))
    if not platform or not product_id:
        return False
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    raw_price = doc.get("activity_price") if doc.get("activity_price") is not None else doc.get("price")
    if isinstance(raw_price, str):
        try:
            raw_price = float(raw_price.strip())
        except ValueError:
            return False
    price_val = _number_or_none(raw_price)
    return price_val is not None and price_val >= 0.0


def build_recent_local_candidates_query(
    window_days: float = DEFAULT_SYNC_WINDOW_DAYS,
    now_utc: Optional[datetime.datetime] = None,
    since_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    构造本地活跃商品查询条件：
    同时覆盖 `updated_at`、`detail_updated_at`、`format_updated_at` 与 `deal_info.format_updated_at`，
    确保无论是新入库榜单品、新晋升线报品，还是刚由 goods_info_format.py 补洗完规格的老底子商品，均不遗漏。
    """
    if since_utc is not None:
        cutoff = since_utc.replace(tzinfo=datetime.timezone.utc) if since_utc.tzinfo is None else since_utc.astimezone(datetime.timezone.utc)
    else:
        days = max(0.001, float(window_days))
        ref_now = now_utc or datetime.datetime.now(datetime.timezone.utc)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=datetime.timezone.utc)
        cutoff = ref_now - datetime.timedelta(days=days)

    time_filter = {
        "$or": [
            {"updated_at": {"$gte": cutoff}},
            {"detail_updated_at": {"$gte": cutoff}},
            {"format_updated_at": {"$gte": cutoff}},
            {"deal_info.format_updated_at": {"$gte": cutoff}},
        ]
    }
    return {"$and": [time_filter, ProductRepository._displayable_content_filter()]}


def extract_sync_fingerprint(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """提取单条商品用于双库状态比对的轻量指纹（同时兼容服务端 _fmt_count 投影与完整文档）。"""
    if not isinstance(doc, dict):
        return None
    platform = normalize_platform(doc.get("platform") or "")
    product_id = clean_product_id(doc.get("product_id") or doc.get("goods_id"))
    if not platform or not product_id:
        return None

    ch = doc.get("channel_type")
    has_channel = isinstance(ch, int) and not isinstance(ch, bool)

    if "_fmt_count" in doc and isinstance(doc.get("_fmt_count"), int):
        fmt_count = max(0, int(doc["_fmt_count"]))
    elif isinstance(doc.get("format_info"), dict):
        fmt_count = len(doc["format_info"])
    else:
        fmt_count = 0

    fmt_ready = doc.get("format_status") == "success" and fmt_count > 0

    deal_info = doc.get("deal_info") if isinstance(doc.get("deal_info"), dict) else {}
    deal_ready = deal_info.get("is_deal_tip") is True
    deal_sig = None
    if deal_ready:
        deal_sig = (
            str(deal_info.get("source_item_id") or ""),
            int(deal_info.get("quality_score") or 0),
            _number_or_none(deal_info.get("deal_price")),
            str(deal_info.get("format_status") or ""),
            to_utc_millis(deal_info.get("format_updated_at")),
        )

    return {
        "platform": platform,
        "product_id": product_id,
        "updated_ms": to_utc_millis(doc.get("updated_at")),
        "detail_ms": to_utc_millis(doc.get("detail_updated_at")),
        "has_channel_type": has_channel,
        "channel_type": ch if has_channel else None,
        "fmt_ready": fmt_ready,
        "fmt_ms": to_utc_millis(doc.get("format_updated_at")) if fmt_ready else None,
        "fmt_sku_count": fmt_count if fmt_ready else 0,
        "deal_ready": deal_ready,
        "deal_sig": deal_sig,
    }


def needs_cloud_merge(
    local_fp: Dict[str, Any],
    cloud_fp: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """
    判定本地商品是否需要向云端新增或合并更新，并返回原因标签：
    - 云端缺失 -> 'insert'
    - 本地主快照/详情更新 -> 'newer_updated_at' / 'newer_detail'
    - 云端缺 channel_type 而本地有 -> 'fill_channel_type'
    - 本地有已成功的规格清洗 format_info 且云端缺失或更旧 -> 'format_info_changed'
    - 本地有好价线报 deal_info 且云端缺失或内容/规格重洗已变更 -> 'deal_info_changed'
    """
    if cloud_fp is None:
        return True, "insert"

    l_upd, c_upd = local_fp["updated_ms"], cloud_fp["updated_ms"]
    if l_upd is not None and (c_upd is None or l_upd > c_upd):
        return True, "newer_updated_at"

    l_det, c_det = local_fp["detail_ms"], cloud_fp["detail_ms"]
    if l_det is not None and (c_det is None or l_det > c_det):
        return True, "newer_detail"

    if local_fp["has_channel_type"] and not cloud_fp["has_channel_type"]:
        return True, "fill_channel_type"

    if local_fp["fmt_ready"]:
        if not cloud_fp["fmt_ready"]:
            return True, "format_info_changed"
        l_fms, c_fms = local_fp["fmt_ms"], cloud_fp["fmt_ms"]
        if l_fms is not None and (c_fms is None or l_fms > c_fms):
            return True, "format_info_changed"
        if l_fms == c_fms and local_fp["fmt_sku_count"] != cloud_fp["fmt_sku_count"]:
            return True, "format_info_changed"

    if local_fp["deal_ready"]:
        if not cloud_fp["deal_ready"] or local_fp["deal_sig"] != cloud_fp["deal_sig"]:
            return True, "deal_info_changed"

    return False, "up_to_date"


def build_product_merge_operation(
    local_doc: Dict[str, Any],
    cloud_fp: Optional[Dict[str, Any]] = None,
) -> Optional[UpdateOne]:
    """
    按 `(platform, product_id)` 构造幂等的 `UpdateOne` 合并操作：
    - 本地存在的有效字段直接写入 `$set`（值为 None 的顶层字段跳过，不冲掉云端原有非空字段如 `channel_type`）；
    - 永远剥离 `_id`、`created_at`（放入 `$setOnInsert`）和 `default_promotion*`；
    - 未清洗成功的 `format_*` 半成品字段不同步；
    - 若云端已存在该商品且 `云端 updated_at > 本地 updated_at`，保护云端更新鲜的实时价格与动态签名字段不被旧值倒退。
    """
    if not is_valid_syncable_product(local_doc):
        return None

    platform = normalize_platform(local_doc.get("platform"))
    product_id = clean_product_id(local_doc.get("product_id") or local_doc.get("goods_id"))

    local_fp = extract_sync_fingerprint(local_doc)
    if local_fp is None:
        return None

    cloud_is_newer_commercial = bool(
        cloud_fp is not None
        and cloud_fp["updated_ms"] is not None
        and (local_fp["updated_ms"] is None or cloud_fp["updated_ms"] > local_fp["updated_ms"])
    )
    cloud_is_newer_detail = bool(
        cloud_fp is not None
        and cloud_fp["detail_ms"] is not None
        and (local_fp["detail_ms"] is None or cloud_fp["detail_ms"] > local_fp["detail_ms"])
    )

    fmt_ready = local_fp["fmt_ready"]
    deal_ready = local_fp["deal_ready"]

    set_fields: Dict[str, Any] = {}
    for key, value in local_doc.items():
        if key in NEVER_OVERWRITE_FIELDS or key == "_fmt_count":
            continue
        if value is None:
            continue
        if key in FORMAT_FAMILY_FIELDS:
            if not fmt_ready:
                continue
            if key in ("format_error", "format_retry_count"):
                continue
        if key == "deal_info":
            if not deal_ready:
                continue
        if key == "channel_type":
            if not (isinstance(value, int) and not isinstance(value, bool)):
                continue
            if cloud_is_newer_commercial and cloud_fp and cloud_fp["has_channel_type"]:
                continue
        if cloud_is_newer_commercial and key in CLOUD_LIVE_COMMERCIAL_FIELDS:
            continue
        if cloud_is_newer_detail and key in CLOUD_LIVE_DETAIL_FIELDS:
            continue

        set_fields[key] = copy.deepcopy(value)

    set_fields["platform"] = platform
    set_fields["product_id"] = product_id
    if not set_fields.get("goods_id"):
        set_fields["goods_id"] = product_id

    if fmt_ready:
        set_fields["need_reformat"] = False

    meaningful_keys = set(set_fields.keys()) - {"platform", "product_id", "goods_id"}
    if not meaningful_keys:
        return None

    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    created_at = local_doc.get("created_at")
    if not isinstance(created_at, datetime.datetime):
        created_at = local_doc.get("updated_at") if isinstance(local_doc.get("updated_at"), datetime.datetime) else now_utc

    update_doc: Dict[str, Any] = {
        "$set": set_fields,
        "$setOnInsert": {"created_at": created_at},
    }
    return UpdateOne({"platform": platform, "product_id": product_id}, update_doc, upsert=True)


def _chunked(items: List[Any], chunk_size: int) -> Iterable[List[Any]]:
    step = max(1, int(chunk_size))
    for idx in range(0, len(items), step):
        yield items[idx : idx + step]


def fetch_cloud_fingerprints_map(
    cloud_col,
    keys_by_platform: Dict[str, List[str]],
    chunk_size: int = DEFAULT_QUERY_CHUNK_SIZE,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """按平台分组并分批使用 `(platform, product_id)` 唯一索引高效拉取云端已有商品指纹。"""
    cloud_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for plat, pid_list in keys_by_platform.items():
        unique_pids = list(dict.fromkeys(p for p in pid_list if p))
        for pid_chunk in _chunked(unique_pids, chunk_size):
            cursor = cloud_col.find(
                {"platform": plat, "product_id": {"$in": pid_chunk}},
                FINGERPRINT_PROJECTION,
            )
            for c_doc in cursor:
                fp = extract_sync_fingerprint(c_doc)
                if fp is not None:
                    cloud_map[(fp["platform"], fp["product_id"])] = fp
    return cloud_map


def ensure_cloud_products_indexes(cloud_col) -> None:
    """确保云端 products 集合具备核心唯一索引与线报查询索引（幂等后台创建）。"""
    cloud_col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
    cloud_col.create_index([("updated_at", -1)], background=True)
    cloud_col.create_index([("platform", 1), ("updated_at", -1)], background=True)
    cloud_col.create_index([("deal_info.is_deal_tip", 1), ("updated_at", -1)], background=True)


def run_sync_round(
    local_col,
    cloud_col,
    *,
    window_days: float = DEFAULT_SYNC_WINDOW_DAYS,
    since_utc: Optional[datetime.datetime] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    执行单轮「本地商品库 -> 云端商品库」检查合并同步。
    """
    t0 = time.perf_counter()
    query = build_recent_local_candidates_query(window_days=window_days, since_utc=since_utc)

    # 1. 先拉取本地窗口内候选商品的超轻量指纹
    local_fp_docs = list(local_col.find(query, FINGERPRINT_PROJECTION))
    local_fps: Dict[Tuple[str, str], Dict[str, Any]] = {}
    keys_by_platform: Dict[str, List[str]] = defaultdict(list)

    for raw_fp_doc in local_fp_docs:
        fp = extract_sync_fingerprint(raw_fp_doc)
        if fp is None:
            continue
        key = (fp["platform"], fp["product_id"])
        local_fps[key] = fp
        keys_by_platform[fp["platform"]].append(fp["product_id"])

    # 2. 按唯一索引分批拉取云端对应商品的轻量指纹并计算差异
    cloud_fps = fetch_cloud_fingerprints_map(cloud_col, keys_by_platform) if local_fps else {}

    diff_keys_by_plat: Dict[str, List[str]] = defaultdict(list)
    reason_counter: Counter = Counter()
    skipped_up_to_date = 0

    for (plat, pid), l_fp in local_fps.items():
        c_fp = cloud_fps.get((plat, pid))
        should_sync, reason = needs_cloud_merge(l_fp, c_fp)
        if should_sync:
            diff_keys_by_plat[plat].append(pid)
            reason_counter[reason] += 1
        else:
            skipped_up_to_date += 1

    total_candidates = len(local_fps)
    total_need_sync = sum(len(v) for v in diff_keys_by_plat.values())

    upserted_total = 0
    modified_total = 0
    matched_total = 0
    written_ops_total = 0
    next_progress_mark = PROGRESS_LOG_STEP

    # 3. 仅针对需要新增或合并更新的商品，从本地读取完整文档并分批写入云端
    if total_need_sync > 0 and not dry_run:
        pending_ops: List[UpdateOne] = []
        eff_batch = max(1, int(batch_size))

        for plat, pids in diff_keys_by_plat.items():
            for pid_chunk in _chunked(pids, DEFAULT_QUERY_CHUNK_SIZE):
                for full_doc in local_col.find({"platform": plat, "product_id": {"$in": pid_chunk}}):
                    norm_plat = normalize_platform(full_doc.get("platform"))
                    norm_pid = clean_product_id(full_doc.get("product_id") or full_doc.get("goods_id"))
                    op = build_product_merge_operation(full_doc, cloud_fps.get((norm_plat, norm_pid)))
                    if op is None:
                        continue
                    pending_ops.append(op)
                    if len(pending_ops) >= eff_batch:
                        res = cloud_col.bulk_write(pending_ops, ordered=False)
                        upserted_total += int(res.upserted_count or 0)
                        modified_total += int(res.modified_count or 0)
                        matched_total += int(res.matched_count or 0)
                        written_ops_total += len(pending_ops)
                        pending_ops.clear()
                        if logger is not None and total_need_sync >= PROGRESS_LOG_STEP and written_ops_total >= next_progress_mark:
                            logger.info(
                                "  ⏳ [大批量同步进度 %d/%d (%.1f%%)] 云端已新增=%d条 合并更新=%d条 | 已耗时=%.1fs",
                                written_ops_total,
                                total_need_sync,
                                written_ops_total * 100.0 / max(1, total_need_sync),
                                upserted_total,
                                modified_total,
                                time.perf_counter() - t0,
                            )
                            next_progress_mark += PROGRESS_LOG_STEP

        if pending_ops:
            res = cloud_col.bulk_write(pending_ops, ordered=False)
            upserted_total += int(res.upserted_count or 0)
            modified_total += int(res.modified_count or 0)
            matched_total += int(res.matched_count or 0)
            written_ops_total += len(pending_ops)
            pending_ops.clear()

    elapsed_s = round(time.perf_counter() - t0, 2)
    scope_desc = (
        f"滚动增量(自 {since_utc.astimezone(datetime.timezone.utc).strftime('%H:%M:%S')} UTC)"
        if since_utc is not None
        else f"完整窗口({window_days:.1f}天)"
    )
    summary = {
        "synced_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": bool(dry_run),
        "scope": scope_desc,
        "window_days": float(window_days),
        "collection": COLLECTION_NAME,
        "scanned_local_candidates": total_candidates,
        "already_up_to_date": skipped_up_to_date,
        "planned_sync_count": total_need_sync,
        "reason_breakdown": dict(reason_counter),
        "written_ops": written_ops_total,
        "cloud_upserted_new": upserted_total,
        "cloud_matched_existing": matched_total,
        "cloud_modified_existing": modified_total,
        "elapsed_seconds": elapsed_s,
    }

    if logger is not None:
        mode_tag = "[DRY-RUN 演练]" if dry_run else "[正式合并]"
        logger.info(
            "%s %s | 本地候选=%d条 | 已一致跳过=%d条 | 待同步=%d条 %s | "
            "云端新增=%d条 合并更新=%d条 | 耗时=%.2fs",
            mode_tag,
            scope_desc,
            total_candidates,
            skipped_up_to_date,
            total_need_sync,
            json.dumps(dict(reason_counter), ensure_ascii=False),
            upserted_total,
            modified_total,
            elapsed_s,
        )

    return summary


def _save_latest_report(report: Dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = LATEST_SYNC_REPORT_PATH.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, LATEST_SYNC_REPORT_PATH)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地商品库 -> 云端商品库 (`crawler_data.products`) 定时检查合并同步守护脚本"
    )
    parser.add_argument(
        "--days",
        type=float,
        default=DEFAULT_SYNC_WINDOW_DAYS,
        help=f"检查本地最近多少天内有更新/清洗变动的商品（默认: {DEFAULT_SYNC_WINDOW_DAYS} 天）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_SYNC_INTERVAL_SECONDS,
        help=f"持续定时模式下每轮检查合并间隔秒数（默认: {DEFAULT_SYNC_INTERVAL_SECONDS} 秒，即 3 分钟）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"云端 bulk_write 每批提交条数（默认: {DEFAULT_BATCH_SIZE}）",
    )
    parser.add_argument(
        "--cloud-uri",
        type=str,
        default="",
        help="可选覆盖云端 MongoDB URI（默认从 config.local.json 的 cloud_mongo_uri 读取）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行单轮完整窗口检查合并后立即退出（不进入后台循环）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：仅连接双库对比并输出差异统计，不对云端做任何写入",
    )
    args = parser.parse_args(argv)

    logger = setup_sync_logger()
    try:
        cloud_uri, cloud_db_name, cloud_host, cloud_port = resolve_cloud_mongo_target(
            explicit_uri=args.cloud_uri or None,
        )
    except Exception as exc:
        logger.error("❌ 云端数据库配置解析失败: %s", exc)
        return 1

    interval_sec = max(10, int(args.interval))
    logger.info(
        "🚀 启动商品双库检查合并同步器 | 集合=[%s] | 基准窗口=[最近 %.1f 天] | 周期=[%s] | 云端目标=[%s (%s:%d/%s)]",
        COLLECTION_NAME,
        args.days,
        "单次运行(--once)" if args.once else f"每 {interval_sec} 秒 (每 {FULL_SWEEP_EVERY_N_CYCLES} 轮全窗口对账)",
        mask_mongo_uri(cloud_uri),
        cloud_host,
        cloud_port,
        cloud_db_name,
    )

    local_db = None
    cloud_client: Optional[MongoClient] = None
    cloud_indexes_ready = False
    last_successful_round_start_utc: Optional[datetime.datetime] = None
    cycle_num = 0

    try:
        while True:
            cycle_num += 1
            round_start_utc = datetime.datetime.now(datetime.timezone.utc)
            is_full_sweep = (
                args.once
                or last_successful_round_start_utc is None
                or (cycle_num - 1) % FULL_SWEEP_EVERY_N_CYCLES == 0
            )
            since_utc = (
                None
                if is_full_sweep
                else (last_successful_round_start_utc - datetime.timedelta(seconds=ROLLING_OVERLAP_SECONDS))
            )

            try:
                if local_db is None:
                    local_db = gen_db_object()
                    local_db.ping()
                if cloud_client is None:
                    cloud_client = MongoClient(
                        cloud_uri,
                        maxPoolSize=10,
                        connect=False,
                        tz_aware=True,
                        compressors="zlib",
                        serverSelectionTimeoutMS=DEFAULT_CONNECT_TIMEOUT_MS,
                        connectTimeoutMS=DEFAULT_CONNECT_TIMEOUT_MS,
                        socketTimeoutMS=DEFAULT_SOCKET_TIMEOUT_MS,
                    )
                    cloud_client.admin.command("ping")
                    cloud_indexes_ready = False

                local_col = local_db.get_collection(COLLECTION_NAME)
                cloud_col = cloud_client[cloud_db_name][COLLECTION_NAME]

                if not cloud_indexes_ready and not args.dry_run:
                    ensure_cloud_products_indexes(cloud_col)
                    cloud_indexes_ready = True

                report = run_sync_round(
                    local_col=local_col,
                    cloud_col=cloud_col,
                    window_days=args.days,
                    since_utc=since_utc,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    logger=logger,
                )
                report["cycle_num"] = cycle_num
                _save_latest_report(report)
                last_successful_round_start_utc = round_start_utc

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.warning(
                    "❌ [第 #%d 轮同步异常] 连接或读写发生异常，下轮将自动重连并回退全窗口对账 | 原因: %s",
                    cycle_num,
                    exc,
                )
                last_successful_round_start_utc = None
                if local_db is not None:
                    try:
                        local_db.close()
                    except Exception:
                        pass
                    local_db = None
                if cloud_client is not None:
                    try:
                        cloud_client.close()
                    except Exception:
                        pass
                    cloud_client = None
                    cloud_indexes_ready = False
                if args.once:
                    return 1

            if args.once:
                break

            time.sleep(interval_sec)

    except KeyboardInterrupt:
        logger.info("🛑 收到中断信号，正在安全关闭本地与云端数据库连接...")
    finally:
        if local_db is not None:
            try:
                local_db.close()
            except Exception:
                pass
        if cloud_client is not None:
            try:
                cloud_client.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
