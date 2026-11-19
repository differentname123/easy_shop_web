# -*- coding: utf-8 -*-
"""好价线报缓冲池 (`deal_raw_tips`) 与清洗调度器全局常量定义。"""
import os
from typing import Dict

# 目录路径计算（单一真源）
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)

DEFAULT_RAW_COLLECTION_NAME = "deal_raw_tips"
DEFAULT_FORMAL_PRODUCTS_COLLECTION = "products"
LEGACY_SHADOW_TEST_COLLECTION = "deal_cleaned_products_test"

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
DEFAULT_CLEAN_INTERVAL_SECONDS = 60
DEFAULT_AI_TIMEOUT_SECONDS = 90.0

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

# 清洗与转链状态与错误码定义（单一真源）
OK_SKIPPED_ALREADY_CLEANED = "OK_SKIPPED_ALREADY_CLEANED"
ERR_NO_ANCHOR = "ERR_NO_ANCHOR"
ERR_MERGED_INTO_NEIGHBOR = "ERR_MERGED_INTO_NEIGHBOR"
ERR_UNSUPPORTED_PLATFORM = "ERR_UNSUPPORTED_PLATFORM"
ERR_TB_NUMERIC_ID = "ERR_TB_NUMERIC_ID"
ERR_PDD_NUMERIC_ID = "ERR_PDD_NUMERIC_ID"
ERR_JD_NUMERIC_SKU = "ERR_JD_NUMERIC_SKU"
ERR_ADAPTER_OFF_PROMOTION = "ERR_ADAPTER_OFF_PROMOTION"
ERR_ADAPTER_CONVERT_FAILED = "ERR_ADAPTER_CONVERT_FAILED"
ERR_EMPTY_PRODUCT_IDENTITY = "ERR_EMPTY_PRODUCT_IDENTITY"
ERR_MAIN_PRODUCT_DEAD = "ERR_MAIN_PRODUCT_DEAD_ONLY_FILLER"
ERR_AI2_MAIN_DEAD_ONLY_FILLER = "ERR_AI2_MAIN_DEAD_ONLY_FILLER"
ERR_AI2_COUPON_FAILED_OR_DROPPED = "ERR_AI2_COUPON_FAILED_OR_DROPPED"
ERR_AI2_PRODUCT_MISMATCH = "ERR_AI2_PRODUCT_MISMATCH"
ERR_AI2_VIRTUAL_OR_JUNK = "ERR_AI2_VIRTUAL_OR_JUNK"
ERR_LOW_QUALITY_SCORE = "ERR_LOW_QUALITY_SCORE"
ERR_EXPIRED_OVER_7_DAYS = "ERR_EXPIRED_OVER_7_DAYS"
ERR_AI_TIMEOUT_RETRY = "ERR_AI_TIMEOUT_RETRY"
ERR_AI_MAX_RETRIES_EXCEEDED = "ERR_AI_MAX_RETRIES_EXCEEDED"

# 不支持的非淘京拼外链与泛娱乐域名（Fail-Fast 排除）
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
    "new.xianbao.fun",
    "youhui.bacaoo.com",
    "0818tuan.com",
    "iehou.com",
)

# 曾尝试并放弃的信源备忘清单（防重复造轮子/踩坑）
EXCLUDED_SITES_AND_CHANNELS_MANIFEST: Dict[str, str] = {
    "smzdm (什么值得买直连及镜像 zhidemai/zhidemai_hot)": "验证码风控与加密跳板 probe.js，已由逛丢服务端解包覆盖。",
    "manmanbuy (慢慢买省钱控 cu.manmanbuy.com)": "前端 crypto-js 动态加密跳转，有效商品率为 0。",
    "55haitao (55海淘 55haitao.com/deals)": "100% 欧美独立站海淘，无淘京拼 CPS 价值。",
    "dataoke (大淘客网页端 dataoke.com/xp/xb)": "强依赖登录态 Cookie，数据已由好单库和折淘客覆盖。",
    "shihuizhu & huim (实惠猪 / 惠喵)": "服务端 TCP Reset 阻断。",
    "gwdang_all_channels (购物党全频道)": "100% 纯数字 SKU 拒识，99.7% 失败率。",
    "xianbao_fun/category_man (慢慢买镜像)": "前端加密跳板壳，无明文推广链。",
}

ENGINEERING_PHILOSOPHY_MANIFEST: Dict[str, str] = {
    "zero_foreign_commission": "宁少勿脏 · 绝不替他人做嫁衣：严禁任何未脱敏或未重置推广位的新链入库。",
    "minimalist_unified_reliable": "越简单越可靠：全局逻辑统一收口，拒绝碎片化补丁与脆弱正则表达式。",
}

CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST: Dict[str, str] = {
    "heuristic_regex_pricing": "已彻底废弃：通过正则表达式匹配各种营销文案抓取价格极其脆弱，现全量统一为 AI-1 与 AI-2 两阶段结构化核验。",
}
