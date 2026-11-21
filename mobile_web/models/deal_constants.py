# -*- coding: utf-8 -*-
"""好价线报缓冲池 (`deal_raw_tips`) 与清洗调度器全局常量定义。"""
import os
from typing import Dict, Tuple

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

LATEST_CLEAN_OPS_REPORT_PATH = os.path.join(LOG_DIR, "latest_clean_ops_report.json")
CLEAN_OPS_HISTORY_JSONL = os.path.join(LOG_DIR, "clean_ops_history.jsonl")
CLEAN_FAILED_SAMPLES_JSONL = os.path.join(LOG_DIR, "clean_failed_samples.jsonl")
CLEAN_SUCCESS_PREVIEW_JSONL = os.path.join(LOG_DIR, "clean_success_preview.jsonl")
DEAL_POOL_RUNTIME_HEALTH_PATH = os.path.join(LOG_DIR, "runtime", "upstream_health_deal_scheduler.json")
DEAL_POOL_LOG_PREFIXES: Tuple[str, ...] = (
    "deal_pool_",
    "deal_raw_tips_fallback",
    "cycle_ops_",
    "latest_cycle_ops_",
    "clean_ops_",
    "latest_clean_ops_",
    "clean_failed_samples",
    "clean_success_preview",
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
ERR_EMPTY_PRODUCT_IDENTITY = "ERR_EMPTY_PRODUCT_IDENTITY"
ERR_AI2_MAIN_DEAD_ONLY_FILLER = "ERR_AI2_MAIN_DEAD_ONLY_FILLER"
ERR_AI2_COUPON_FAILED_OR_DROPPED = "ERR_AI2_COUPON_FAILED_OR_DROPPED"
ERR_AI2_PRODUCT_MISMATCH = "ERR_AI2_PRODUCT_MISMATCH"
ERR_AI2_VIRTUAL_OR_JUNK = "ERR_AI2_VIRTUAL_OR_JUNK"
ERR_LOW_QUALITY_SCORE = "ERR_LOW_QUALITY_SCORE"
ERR_EXPIRED_OVER_7_DAYS = "ERR_EXPIRED_OVER_7_DAYS"
ERR_AI_TIMEOUT_RETRY = "ERR_AI_TIMEOUT_RETRY"
ERR_AI_MAX_RETRIES_EXCEEDED = "ERR_AI_MAX_RETRIES_EXCEEDED"

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
