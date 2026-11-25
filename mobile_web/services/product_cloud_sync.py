# -*- coding: utf-8 -*-
"""
本地商品库 (`crawler_data.products`) <-> 云端商品库 (`crawler_data.products`) 检查合并同步服务。
================================================================================
核心机制（极简、幂等、可靠）：
1. 轻量双库指纹比对：服务端计算 `_fmt_count` 投影，按 `(platform, product_id)` 分批差量拉取；
2. 字段级安全合并：剥离 `_id`、`created_at` 与云端短效 `default_promotion*` 缓存；仅同步已完成清洗的 `format_*`；
   云端 `updated_at` / `detail_updated_at` 更新鲜时保护云端实时价格与动态 `goods_sign` 不被旧快照回退；
3. 软下架/迁移同步：将本地 `is_off_promotion=True`（含 `superseded_by`）幂等同步到云端，并在候选查询与回拉中排除；
4. 云端待洗回拉：全量对账轮次将云端独有未清洗规格商品回拉到本地，交由 `goods_info_format` 补洗后回合并。
================================================================================
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import datetime
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit, urlunsplit

from bson.decimal128 import Decimal128
from pymongo import UpdateOne

from infrastructure.runtime_io import atomic_write_json, should_write_runtime_artifact
from infrastructure.settings import resolve_config_path
from models.product import clean_product_id, normalize_platform
from repositories.product_repo import ProductRepository

MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
COLLECTION_NAME = "products"
DEFAULT_SYNC_WINDOW_DAYS = 3.0
DEFAULT_SYNC_INTERVAL_SECONDS = 180
FULL_SWEEP_EVERY_N_CYCLES = 20
ROLLING_OVERLAP_SECONDS = 120
DEFAULT_BATCH_SIZE = 200
DEFAULT_QUERY_CHUNK_SIZE = 500
DEFAULT_CLOUD_PULL_MAX_PER_SWEEP = 300
DEFAULT_CONNECT_TIMEOUT_MS = 10000
DEFAULT_SOCKET_TIMEOUT_MS = 60000
PROGRESS_LOG_STEP = 2000

LOG_DIR = MOBILE_WEB_DIR / "logs"
LOG_FILE_PATH = LOG_DIR / "product_cloud_sync.log"
LATEST_SYNC_REPORT_PATH = LOG_DIR / "latest_cloud_sync_report.json"

NEVER_OVERWRITE_FIELDS = frozenset({
    "_id",
    "created_at",
    "sku_info",
    "format_sku_prices",
    "default_promotion",
    "default_promotion_updated_at",
    "default_promotion_expires_at",
    "user_stats",
    "user_stats_score",
})

FORMAT_FAMILY_FIELDS = frozenset({
    "format_info",
    "format_status",
    "format_updated_at",
    "format_model",
    "format_error",
    "format_retry_count",
    "format_activity_price",
    "need_reformat",
})

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
    "deal_info.expired_at": 1,
    "deal_info.source_item_id": 1,
    "deal_info.source_is_reliable": 1,
    "deal_info.price_is_reliable": 1,
    "deal_info.price_reliability_reason": 1,
    "deal_info.quality_score": 1,
    "deal_info.deal_price": 1,
    "deal_info.updated_at": 1,
    "deal_info.format_status": 1,
    "deal_info.format_updated_at": 1,
    "is_user_collected": 1,
}

_LOCAL_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


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
    """解析云端 MongoDB URI、目标库名、主机与端口，并阻止自同步覆盖本地源库。"""
    cfg_path = resolve_config_path(config_file)
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
            uri = f"mongodb://{quote_plus(user)}:{quote_plus(password)}@{remote_host}:{port}/{default_db}?authSource={auth_source}"
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
    """将 datetime 统一转换为 BSON 毫秒整数时间戳，消除微秒截断假差异。"""
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
    """校验文档是否具备有效身份与可展示内容。"""
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
    """构造本地活跃商品查询条件，同时覆盖主表、详情、规格与线报更新时间。"""
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
            {"deal_info.updated_at": {"$gte": cutoff}},
        ]
    }
    return {"$and": [time_filter, {"is_off_promotion": {"$ne": True}}, ProductRepository._displayable_content_filter()]}


def extract_sync_fingerprint(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """提取单条商品用于双库状态比对的轻量指纹。"""
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
    deal_expired = (deal_info.get("is_deal_tip") is False) and (deal_info.get("expired_at") is not None)
    deal_sig = None
    if deal_ready:
        deal_sig = (
            str(deal_info.get("source_item_id") or ""),
            int(deal_info.get("quality_score") or 0),
            _number_or_none(deal_info.get("deal_price")),
            to_utc_millis(deal_info.get("updated_at")),
            str(deal_info.get("format_status") or ""),
            to_utc_millis(deal_info.get("format_updated_at")),
            bool(deal_info.get("source_is_reliable")),
            deal_info.get("price_is_reliable") if isinstance(deal_info.get("price_is_reliable"), bool) else None,
            str(deal_info.get("price_reliability_reason") or "").strip(),
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
        "deal_expired": deal_expired,
        "deal_sig": deal_sig,
        "is_user_collected": bool(doc.get("is_user_collected") is True),
    }


def needs_cloud_merge(
    local_fp: Dict[str, Any],
    cloud_fp: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """判定本地商品是否需要向云端新增或合并更新，并返回原因标签。"""
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
    elif local_fp.get("deal_expired") and cloud_fp.get("deal_ready"):
        return True, "deal_info_expired"

    return False, "up_to_date"


def build_product_merge_operation(
    local_doc: Dict[str, Any],
    cloud_fp: Optional[Dict[str, Any]] = None,
) -> Optional[UpdateOne]:
    """按 `(platform, product_id)` 构造幂等合并 `UpdateOne` 操作，严格保护云端专属字段与更新鲜快照。"""
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
    deal_expired_sync = bool(local_fp.get("deal_expired") and cloud_fp is not None and cloud_fp.get("deal_ready"))

    set_fields: Dict[str, Any] = {}
    for key, value in local_doc.items():
        if key in NEVER_OVERWRITE_FIELDS or key == "_fmt_count":
            continue
        if value is None:
            continue
        if key in FORMAT_FAMILY_FIELDS:
            if not fmt_ready or key in ("format_error", "format_retry_count"):
                continue
        if key == "deal_info" and not (deal_ready or deal_expired_sync):
            continue
        if key == "is_user_collected" and value is not True:
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

    if not (set(set_fields.keys()) - {"platform", "product_id", "goods_id"}):
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
    """按平台分组并分批使用 `(platform, product_id)` 唯一索引拉取云端已有商品指纹。"""
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
    """确保云端 `products` 集合具备核心唯一索引与线报查询索引（幂等后台创建）。"""
    cloud_col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
    cloud_col.create_index([("updated_at", -1)], background=True)
    cloud_col.create_index([("platform", 1), ("updated_at", -1)], background=True)
    cloud_col.create_index([("deal_info.is_deal_tip", 1), ("updated_at", -1)], background=True)


def sync_off_promotion_to_cloud(
    local_col,
    cloud_col,
    *,
    window_days: float = DEFAULT_SYNC_WINDOW_DAYS,
    since_utc: Optional[datetime.datetime] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Tuple[int, set[Tuple[str, str]]]:
    """将本地最近窗口内软下架/身份替代状态 (`is_off_promotion=True`) 幂等传播到云端 `products`。"""
    if local_col is None or cloud_col is None:
        return 0, set()
    if since_utc is not None:
        cutoff = since_utc.replace(tzinfo=datetime.timezone.utc) if since_utc.tzinfo is None else since_utc.astimezone(datetime.timezone.utc)
    else:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max(0.001, float(window_days)))
    cutoff_naive = cutoff.replace(tzinfo=None)

    off_promo_keys: set[Tuple[str, str]] = set()
    local_docs_by_plat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    try:
        cursor = local_col.find(
            {
                "is_off_promotion": True,
                "$or": [{"off_promotion_at": {"$gte": cutoff}}, {"off_promotion_at": {"$gte": cutoff_naive}}],
            },
            {"_id": 0, "platform": 1, "product_id": 1, "off_promotion_at": 1, "superseded_by": 1, "off_promotion_reason": 1},
        )
        for doc in cursor:
            plat = normalize_platform(doc.get("platform") or "")
            pid = clean_product_id(doc.get("product_id"))
            if not plat or not pid or (plat, pid) in off_promo_keys:
                continue
            off_promo_keys.add((plat, pid))
            local_docs_by_plat[plat].append({**doc, "platform": plat, "product_id": pid})
    except Exception as exc:
        if logger is not None:
            logger.warning("⚠️ 读取本地软下架商品状态失败: %s", exc)
        return 0, off_promo_keys

    if not local_docs_by_plat:
        return 0, off_promo_keys

    cloud_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for plat, docs in local_docs_by_plat.items():
        pids = [d["product_id"] for d in docs]
        for pid_chunk in _chunked(pids, DEFAULT_QUERY_CHUNK_SIZE):
            for c_doc in cloud_col.find(
                {"platform": plat, "product_id": {"$in": pid_chunk}},
                {"_id": 0, "platform": 1, "product_id": 1, "is_off_promotion": 1, "superseded_by": 1},
            ):
                cp = normalize_platform(c_doc.get("platform") or plat)
                cpid = clean_product_id(c_doc.get("product_id"))
                if cp and cpid:
                    cloud_state[(cp, cpid)] = c_doc

    ops: List[UpdateOne] = []
    for plat, docs in local_docs_by_plat.items():
        for doc in docs:
            pid = doc["product_id"]
            c_doc = cloud_state.get((plat, pid))
            if c_doc is None:
                continue
            sup_val = doc.get("superseded_by")
            if c_doc.get("is_off_promotion") is True and (not sup_val or c_doc.get("superseded_by") == sup_val):
                continue
            set_doc: Dict[str, Any] = {"is_off_promotion": True}
            if isinstance(doc.get("off_promotion_at"), datetime.datetime):
                set_doc["off_promotion_at"] = doc["off_promotion_at"]
            if doc.get("off_promotion_reason"):
                set_doc["off_promotion_reason"] = doc["off_promotion_reason"]
            if sup_val:
                set_doc["superseded_by"] = sup_val
            ops.append(
                UpdateOne(
                    {"platform": plat, "product_id": pid},
                    {"$set": set_doc, "$unset": {"default_promotion": "", "default_promotion_expires_at": ""}},
                    upsert=False,
                )
            )

    synced_cloud = 0
    if ops and not dry_run and callable(getattr(cloud_col, "bulk_write", None)):
        eff_batch = max(1, int(batch_size))
        for i in range(0, len(ops), eff_batch):
            res = cloud_col.bulk_write(ops[i : i + eff_batch], ordered=False)
            synced_cloud += int(getattr(res, "modified_count", 0) or 0)
        if synced_cloud > 0 and logger is not None:
            logger.info("🗑️ [软下架同步] 已将本地已下架/被替代商品状态同步至云端 `products` %d 条", synced_cloud)
    return synced_cloud, off_promo_keys


def pull_cloud_only_unformatted_products(
    local_col,
    cloud_col,
    *,
    window_days: float = DEFAULT_SYNC_WINDOW_DAYS,
    max_pull: int = DEFAULT_CLOUD_PULL_MAX_PER_SWEEP,
    batch_size: int = DEFAULT_BATCH_SIZE,
    exclude_keys: Optional[set[Tuple[str, str]]] = None,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> int:
    """全量对账轮次反向拉取云端独有且未执行规格清洗的新近有效商品到本地。"""
    if local_col is None or cloud_col is None or max_pull <= 0:
        return 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max(0.001, float(window_days)))
    cloud_query = {
        "$and": [
            {"updated_at": {"$gte": cutoff}},
            {"is_off_promotion": {"$ne": True}},
            {"$or": [{"format_status": None}, {"format_status": {"$exists": False}}]},
            ProductRepository._displayable_content_filter(),
        ]
    }
    try:
        raw_cursor = cloud_col.find(cloud_query)
        if not isinstance(raw_cursor, (list, tuple)):
            if hasattr(raw_cursor, "sort"):
                raw_cursor = raw_cursor.sort([("updated_at", -1)])
            if hasattr(raw_cursor, "limit"):
                raw_cursor = raw_cursor.limit(int(max_pull))
        cloud_docs = list(raw_cursor)[: int(max_pull)]
    except Exception as exc:
        if logger is not None:
            logger.warning("⚠️ 查询云端待补洗规格商品异常: %s", exc)
        return 0

    if not cloud_docs:
        return 0

    excluded = exclude_keys or set()
    cand_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    pids_by_plat: Dict[str, List[str]] = defaultdict(list)
    for c_doc in cloud_docs:
        if not is_valid_syncable_product(c_doc):
            continue
        plat = normalize_platform(c_doc.get("platform") or "")
        pid = clean_product_id(c_doc.get("product_id") or c_doc.get("goods_id"))
        if not plat or not pid or (plat, pid) in excluded or (plat, pid) in cand_by_key:
            continue
        cand_by_key[(plat, pid)] = c_doc
        pids_by_plat[plat].append(pid)

    if not cand_by_key:
        return 0

    existing_local_keys: set[Tuple[str, str]] = set()
    for plat, pids in pids_by_plat.items():
        for pid_chunk in _chunked(pids, DEFAULT_QUERY_CHUNK_SIZE):
            for l_doc in local_col.find(
                {"platform": plat, "product_id": {"$in": pid_chunk}},
                {"_id": 0, "platform": 1, "product_id": 1},
            ):
                lp = normalize_platform(l_doc.get("platform") or "")
                lpid = clean_product_id(l_doc.get("product_id"))
                if lp and lpid:
                    existing_local_keys.add((lp, lpid))

    missing_keys = [k for k in cand_by_key if k not in existing_local_keys]
    if not missing_keys or dry_run:
        return len(missing_keys) if dry_run else 0

    pulled_upserted = 0
    now_naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    ops: List[UpdateOne] = []
    for plat, pid in missing_keys:
        src_doc = cand_by_key[(plat, pid)]
        clean_insert: Dict[str, Any] = {}
        for k, v in src_doc.items():
            if k in NEVER_OVERWRITE_FIELDS or v is None:
                continue
            if isinstance(v, datetime.datetime) and v.tzinfo is not None:
                clean_insert[k] = v.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            else:
                clean_insert[k] = copy.deepcopy(v)
        clean_insert["platform"] = plat
        clean_insert["product_id"] = pid
        if not clean_insert.get("goods_id"):
            clean_insert["goods_id"] = pid
        raw_created = src_doc.get("created_at")
        if isinstance(raw_created, datetime.datetime):
            clean_insert["created_at"] = (
                raw_created.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                if raw_created.tzinfo is not None
                else raw_created
            )
        else:
            clean_insert["created_at"] = clean_insert.get("updated_at") or now_naive
        ops.append(UpdateOne({"platform": plat, "product_id": pid}, {"$setOnInsert": clean_insert}, upsert=True))
        if len(ops) >= max(1, int(batch_size)):
            res = local_col.bulk_write(ops, ordered=False)
            pulled_upserted += int(res.upserted_count or 0)
            ops.clear()

    if ops:
        res = local_col.bulk_write(ops, ordered=False)
        pulled_upserted += int(res.upserted_count or 0)

    if pulled_upserted > 0 and logger is not None:
        logger.info(
            "📥 [云端推荐流回拉补洗] 已将 %d 条云端独有未清洗商品回拉至本地 `products` 交由 `goods_info_format` 补齐规格",
            pulled_upserted,
        )
    return pulled_upserted


def run_sync_round(
    local_col,
    cloud_col,
    *,
    window_days: float = DEFAULT_SYNC_WINDOW_DAYS,
    since_utc: Optional[datetime.datetime] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
    pull_cloud_unformatted: bool = False,
) -> Dict[str, Any]:
    """执行单轮「本地商品库 <-> 云端商品库」检查合并同步。"""
    t0 = time.perf_counter()
    cloud_off_promotion_synced, off_promo_keys = sync_off_promotion_to_cloud(
        local_col,
        cloud_col,
        window_days=window_days,
        since_utc=since_utc,
        batch_size=batch_size,
        dry_run=dry_run,
        logger=logger,
    )
    pulled_cloud_only = 0
    if pull_cloud_unformatted:
        pulled_cloud_only = pull_cloud_only_unformatted_products(
            local_col,
            cloud_col,
            window_days=window_days,
            batch_size=batch_size,
            exclude_keys=off_promo_keys,
            dry_run=dry_run,
            logger=logger,
        )

    query = build_recent_local_candidates_query(window_days=window_days, since_utc=since_utc)
    local_fps: Dict[Tuple[str, str], Dict[str, Any]] = {}
    keys_by_platform: Dict[str, List[str]] = defaultdict(list)

    for raw_fp_doc in local_col.find(query, FINGERPRINT_PROJECTION):
        fp = extract_sync_fingerprint(raw_fp_doc)
        if fp is None:
            continue
        key = (fp["platform"], fp["product_id"])
        local_fps[key] = fp
        keys_by_platform[fp["platform"]].append(fp["product_id"])

    cloud_fps = fetch_cloud_fingerprints_map(cloud_col, keys_by_platform) if local_fps else {}

    diff_keys_by_plat: Dict[str, List[str]] = defaultdict(list)
    reason_counter: Counter = Counter()
    skipped_up_to_date = 0

    for (plat, pid), l_fp in local_fps.items():
        should_sync, reason = needs_cloud_merge(l_fp, cloud_fps.get((plat, pid)))
        if should_sync:
            diff_keys_by_plat[plat].append(pid)
            reason_counter[reason] += 1
        else:
            skipped_up_to_date += 1

    total_candidates = len(local_fps)
    total_need_sync = sum(len(v) for v in diff_keys_by_plat.values())
    upserted_total = modified_total = matched_total = written_ops_total = 0
    next_progress_mark = PROGRESS_LOG_STEP

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
        "cloud_off_promotion_synced": cloud_off_promotion_synced,
        "local_pulled_cloud_unformatted": pulled_cloud_only,
        "elapsed_seconds": elapsed_s,
    }

    if logger is not None:
        mode_tag = "[DRY-RUN 演练]" if dry_run else "[正式合并]"
        logger.info(
            "%s %s | 本地候选=%d条 | 已一致跳过=%d条 | 待同步=%d条 %s | "
            "云端新增=%d条 合并更新=%d条 下架清理=%d条 回拉待洗=%d条 | 耗时=%.2fs",
            mode_tag,
            scope_desc,
            total_candidates,
            skipped_up_to_date,
            total_need_sync,
            json.dumps(dict(reason_counter), ensure_ascii=False),
            upserted_total,
            modified_total,
            cloud_off_promotion_synced,
            pulled_cloud_only,
            elapsed_s,
        )

    return summary


def save_latest_sync_report(report: Dict[str, Any], path: Path = LATEST_SYNC_REPORT_PATH) -> None:
    if not should_write_runtime_artifact(path, LOG_DIR):
        return
    try:
        atomic_write_json(path, report)
    except Exception:
        pass
