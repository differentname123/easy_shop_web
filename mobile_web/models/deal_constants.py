# -*- coding: utf-8 -*-
"""好价线报缓冲池 (`deal_raw_tips`) 与清洗调度器全局常量及时间唯一真源定义。"""
import datetime
import math
import os
import re
from typing import Any, Dict, Optional, Tuple

# 目录路径计算（单一真源）
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)

DEFAULT_RAW_COLLECTION_NAME = "deal_raw_tips"
DEFAULT_FORMAL_PRODUCTS_COLLECTION = "products"

PROMPT_DIR = os.path.join(PROJECT_ROOT, "prompt")
PROMPT_STAGE1_PATH = os.path.join(PROMPT_DIR, "爆料阶段1_原始拆解与转链提纯.txt")
PROMPT_STAGE2_PATH = os.path.join(PROMPT_DIR, "爆料阶段2_底牌终审与教程评分.txt")

QUALIFIED_DEAL_MIN_SCORE = 60
DEAL_INFO_PROTECTION_HOURS = 12.0
DEAL_FORMAL_EXPIRY_HOURS = 72.0
DEAL_RAW_TTL_DAYS = 14
DEAL_MAX_CLEAN_AGE_DAYS = 2

# 高质量信源与专员严选渠道信誉加分表 (Source Reputation Bonus)
SOURCE_REPUTATION_BONUS: Dict[str, int] = {
    "tehuicaicai": 10,  # 特惠采采/惠享购：专员人工严选集采，自带高清白底图与视频教程，信噪比极高
}
MAX_CLEAN_RETRIES = 3
MAX_ANCHOR_CACHE_SIZE = 8000
MAX_JSONL_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_CLEAN_WINDOW = 200
DEFAULT_AI_TIMEOUT_SECONDS = 90.0

# 站点与频道默认保鲜天数映射（纯领域常量，供仓储与清洗统一读取）
SITE_MAX_AGE_DAYS: Dict[str, float] = {
    "tehuicaicai": 7.0,
    "ithome_lapin": 2.5,
}
CHANNEL_MAX_AGE_DAYS: Dict[str, float] = {
    "特惠采采_首页精选": 7.0,
    "IT之家辣品_好物": 2.5,
}


def resolve_deal_max_age_days(
    channel_name: str = "",
    site_key: str = "",
    default_days: float = DEAL_MAX_CLEAN_AGE_DAYS,
) -> float:
    """根据频道名称或站点标识获取保鲜天数（单一纯领域真源）。"""
    ch = str(channel_name or "").strip()
    if ch and ch in CHANNEL_MAX_AGE_DAYS:
        return float(CHANNEL_MAX_AGE_DAYS[ch])
    sk = str(site_key or "").strip()
    if sk and sk in SITE_MAX_AGE_DAYS:
        return float(SITE_MAX_AGE_DAYS[sk])
    return float(default_days)


def get_default_site_max_age_days_map() -> Dict[str, float]:
    """返回站点级保鲜天数映射副本。"""
    return dict(SITE_MAX_AGE_DAYS)

# 阶段化独立流水线常量 (Stage Pipeline Constants)
CLEAN_STAGE_NEED_AI1 = "need_ai1"
CLEAN_STAGE_NEED_CONVERT = "need_convert"
CLEAN_STAGE_NEED_AI2 = "need_ai2"
CLEAN_STAGE_DONE = "done"

DEFAULT_STAGE_POLL_INTERVAL_SECONDS = 3
DEFAULT_STAGE_LEASE_SECONDS_AI = 300
DEFAULT_STAGE_LEASE_SECONDS_CONVERT = 180

# 日志与报告文件路径
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
LOG_FILE_PATH = os.path.join(LOG_DIR, "deal_pool_scheduler.log")
STATS_SNAPSHOT_PATH = os.path.join(LOG_DIR, "deal_pool_stats.json")
LOCAL_FALLBACK_JSONL = os.path.join(LOG_DIR, "deal_raw_tips_fallback.jsonl")
ERROR_LOG_JSONL_PATH = os.path.join(LOG_DIR, "deal_pool_errors.jsonl")
LATEST_OPS_REPORT_PATH = os.path.join(LOG_DIR, "latest_cycle_ops_report.json")
OPS_REPORT_HISTORY_JSONL = os.path.join(LOG_DIR, "cycle_ops_history.jsonl")

DEAL_POOL_RUNTIME_HEALTH_PATH = os.path.join(LOG_DIR, "runtime", "upstream_health_deal_scheduler.json")
DEAL_POOL_LOG_PREFIXES: Tuple[str, ...] = (
    "deal_pool_",
    "deal_raw_tips_fallback",
    "cycle_ops_",
    "latest_cycle_ops_",
    "model_api_deal_pool.log",
)

# 清洗与转链状态与错误码定义（单一真源）
OK_SKIPPED_ALREADY_CLEANED = "OK_SKIPPED_ALREADY_CLEANED"
UPSTREAM_DISAPPEARED = "UPSTREAM_DISAPPEARED"
ERR_NO_ANCHOR = "ERR_NO_ANCHOR"
ERR_MERGED_INTO_NEIGHBOR = "ERR_MERGED_INTO_NEIGHBOR"
ERR_UNSUPPORTED_PLATFORM = "ERR_UNSUPPORTED_PLATFORM"
ERR_TB_NUMERIC_ID = "ERR_TB_NUMERIC_ID"
ERR_PDD_NUMERIC_ID = "ERR_PDD_NUMERIC_ID"
ERR_JD_NUMERIC_SKU = "ERR_JD_NUMERIC_SKU"
ERR_ADAPTER_OFF_PROMOTION = "ERR_ADAPTER_OFF_PROMOTION"
ERR_ADAPTER_CONVERT_FAILED = "ERR_ADAPTER_CONVERT_FAILED"
ERR_ADAPTER_CONFIG_ERROR = "ERR_ADAPTER_CONFIG_ERROR"
ERR_EMPTY_PRODUCT_IDENTITY = "ERR_EMPTY_PRODUCT_IDENTITY"
ERR_AI2_MAIN_DEAD_ONLY_FILLER = "ERR_AI2_MAIN_DEAD_ONLY_FILLER"
ERR_AI2_COUPON_FAILED_OR_DROPPED = "ERR_AI2_COUPON_FAILED_OR_DROPPED"
ERR_AI2_PRODUCT_MISMATCH = "ERR_AI2_PRODUCT_MISMATCH"
ERR_AI2_VIRTUAL_OR_JUNK = "ERR_AI2_VIRTUAL_OR_JUNK"
ERR_AI2_PRICE_INTEGRITY = "ERR_AI2_PRICE_INTEGRITY"
ERR_MULTI_PRODUCT_COLLECTION = "ERR_MULTI_PRODUCT_COLLECTION"
ERR_LOW_QUALITY_SCORE = "ERR_LOW_QUALITY_SCORE"
ERR_EXPIRED_OVER_7_DAYS = "ERR_EXPIRED_OVER_7_DAYS"
ERR_AI_TIMEOUT_RETRY = "ERR_AI_TIMEOUT_RETRY"
ERR_AI_MAX_RETRIES_EXCEEDED = "ERR_AI_MAX_RETRIES_EXCEEDED"

# 多商品合集帖确定性拦截阈值（AI-1 主推商品候选超过 2 个直接拦截，其余由转链真实 goods_id 判定）
MAX_AI1_MAIN_CANDIDATES = 2

# 不支持的非淘京拼外链与源站自身域（Fail-Fast 排除）
_UNSUPPORTED_EXTERNAL_DOMAINS = (
    "vip.com",
    "douyin.com",
    "meituan.com",
    "dpurl.cn",
    "mp.weixin.qq.com",
    "didi.cn",
    "icbc.com.cn",
    "pan.baidu.com",
    "xiaohongshu.com",
    "xhslink.com",
    "b23.tv",
    "bilibili.com",
    "weibo.com",
    "kuaishou.com",
    "guangdiu.com/go.php",
    "0818tuan.com",
    "go.ruanmei.com",
    "ithome.com",
    "zzzdm.com",
    "kiees.com",
    "niuza.com",
    "zdzdm.com",
    "baicaio.com",
    "t.me",
    "xuandan.com",
)

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
_EPOCH_STR_RE = re.compile(r"^(?:\d{10}|\d{13})(?:\.\d+)?$")


def coerce_unix_timestamp(val: Any) -> Optional[float]:
    """严格将已入库时间戳（float/int/datetime/绝对时间字符串）归一化为秒级 Unix 时间戳；不猜测相对文案。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if not math.isfinite(ts) or ts <= 0:
            return None
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(val, datetime.datetime):
        dt = val.replace(tzinfo=datetime.timezone.utc) if val.tzinfo is None else val
        return dt.timestamp()
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if _EPOCH_STR_RE.fullmatch(s):
            ts = float(s)
            return ts / 1000.0 if ts > 1e12 else ts
        iso_cand = re.sub(r"(\.\d{6})\d+", r"\1", s)
        if iso_cand.endswith(("Z", "z")):
            iso_cand = iso_cand[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(iso_cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            return dt.timestamp()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.datetime.strptime(s, fmt).replace(tzinfo=CN_TZ).timestamp()
            except ValueError:
                continue
    return None


def parse_publish_time_epoch(val: Any) -> Optional[float]:
    """采集入口唯一真源：将任意站点发布时间（数字/datetime/绝对时间/相对文案/HH:MM）归一为 UTC epoch 秒。"""
    exact = coerce_unix_timestamp(val)
    if exact is not None:
        return exact
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None

    now_cn = datetime.datetime.now(CN_TZ)
    if s == "刚刚":
        return now_cn.timestamp()

    m_hm_rel = re.search(r"(\d+)\s*小时\s*(\d+)\s*分钟前", s)
    if m_hm_rel:
        hrs = int(m_hm_rel.group(1))
        mins = int(m_hm_rel.group(2))
        return (now_cn - datetime.timedelta(hours=hrs, minutes=mins)).timestamp()

    m_rel = re.search(r"(\d+)\s*(秒|分钟|小时|天)前", s)
    if m_rel:
        num = int(m_rel.group(1))
        unit = m_rel.group(2)
        if unit == "秒":
            return (now_cn - datetime.timedelta(seconds=num)).timestamp()
        if unit == "分钟":
            return (now_cn - datetime.timedelta(minutes=num)).timestamp()
        if unit == "小时":
            return (now_cn - datetime.timedelta(hours=num)).timestamp()
        if unit == "天":
            return (now_cn - datetime.timedelta(days=num)).timestamp()

    m_day_hm = re.match(r"^(今天|昨天)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m_day_hm:
        day_delta = 0 if m_day_hm.group(1) == "今天" else 1
        base_dt = (now_cn - datetime.timedelta(days=day_delta)).replace(
            hour=int(m_day_hm.group(2)),
            minute=int(m_day_hm.group(3)),
            second=int(m_day_hm.group(4) or 0),
            microsecond=0,
        )
        return base_dt.timestamp()

    m_bare_hm = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m_bare_hm:
        cand_dt = now_cn.replace(
            hour=int(m_bare_hm.group(1)),
            minute=int(m_bare_hm.group(2)),
            second=int(m_bare_hm.group(3) or 0),
            microsecond=0,
        )
        if cand_dt > now_cn + datetime.timedelta(minutes=5):
            cand_dt -= datetime.timedelta(days=1)
        return cand_dt.timestamp()

    try:
        parsed = datetime.datetime.strptime(s, "%m-%d %H:%M").replace(year=now_cn.year, tzinfo=CN_TZ)
        return parsed.timestamp()
    except ValueError:
        return None


def get_deal_publish_ts(deal_info: Any) -> Optional[float]:
    """统一读取线报发布时间戳（优先读取规范数值 `publish_ts`，兼容历史未迁移记录 `publish_time`）。"""
    if not isinstance(deal_info, dict):
        return None
    ts = coerce_unix_timestamp(deal_info.get("publish_ts"))
    if ts is not None:
        return ts
    return coerce_unix_timestamp(deal_info.get("publish_time"))


def format_timestamp_cn(val: Any, *, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """将任意有效时间戳/绝对时间在展示边界转换为北京时间本地字符串，无效返回空串。"""
    ts = coerce_unix_timestamp(val)
    if ts is None:
        return ""
    return datetime.datetime.fromtimestamp(ts, tz=CN_TZ).strftime(fmt)


USER_SUBMIT_SITE = "user_submit"
