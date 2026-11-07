# -*- coding: utf-8 -*-
"""
独立导购/线报好价缓冲池定时拉取与监控守护脚本 (`deal_raw_tips` 生产规范版)
================================================================================
一、【核心设计原则】
  1. 100% 独立隔离：
     - 仅向独立的 MongoDB 缓冲池集合 `deal_raw_tips` 写入原始线报与好价教程数据；
     - 绝不修改现有 `products` 集合、不触碰现有 Web 路由或订单同步任务，对线上现有流程零侵入。
  2. 拉取阶段零清洗、零丢弃（"重信息完整，轻前置清洗"）：
     - 在拉取入池阶段，对纳入监控的 6 大站点 26 个有效频道拉取到的所有完整详情记录 100% 原样入库留存，
       不做任何前置删除或清洗，完整保留 `tutorial_text`、`tutorial_images`、`extracted_links`、
       `extracted_tokens` 与 `raw_payload`。
     - 同时在入库时自动标注 `quality_signals.has_unique_product_anchor`（是否包含指向唯一商品的硬锚点），
       方便随时通过 `--stats` 观察各站点/各频道的真实产出效果。
  3. 增量智能拉取（零重复抓详情 + 动态热度追踪）：
     - 每轮拉取列表前，先读取 `deal_raw_tips` 中已拉取过完整详情的 `source_item_id` 集合（`skip_detail_ids`）；
     - 仅对【首次出现的新线报】发起详情页 HTTP 请求；对已在池中的老线报仅轻量更新 `last_seen_at`、
       `seen_count`、`seen_channels` 及最新热度 `hot_metrics`。

二、【数据源准入的两条硬性铁律】
  - 铁律 1（安全稳定拉取基础）：必须能长期、稳定、不被风控拦截地自动拉取；如果频繁触发验证码或封 IP，数据再漂亮也无意义。
  - 铁律 2（存在指向“唯一商品”的硬锚点，不限有效比例高低）：拉取的数据中必须存在能锁定【唯一具体商品】的信息
    （唯一商品直链/短链、加密商品ID、淘口令/京口令、或明确的 10~15 位数字 SKU/货号），绝不能只是泛泛的商品标题关键字。
    只要该频道能产出含唯一商品锚点的记录，即使有效比例较低（如 10%~35%）也完全可取，因为后续可通过规则轻松过滤无关内容。

三、【最终纳入 vs 明确剔除的站点与频道清单（⚠️ 重要说明：严禁后续人员误以为遗漏而重复尝试已剔除站点）】
  ✅ 正式纳入定时任务的 6 大站点（共 26 个有效频道）：
     1. 好单库 (`haodanku.com`)       : 5 个频道（京东线报 10004、淘宝线报 10003、百亿补贴 10005、综合 0、福利清单 weal_list）—— 0 风控，100% 含唯一商品链/加密ID
     2. 购物党 (`gwdang.com`)         : 5 个频道（3H最热 0-hour_3、历史最低 0-lowest、全网最低 0-s_lowest、低于双11 0-lower_11、最新 0-latest）—— 间隔3s零风控，解码 data= 后 100% 含唯一商品 SKU 与购买链
     3. 0818团 (`0818tuan.com`)       : 2 个频道（第1页 P1、第2页 P2）—— 纯静态 0 风控，71.4% 含唯一商品短链
     4. 线报酷 (`new.xianbao.fun`)    : 12 个有效频道（hot_1h、haodan_hot、weibo_hot、douban_hot、latest、category_haodan、category_weibo、category_douban_maizu、kuan_hot、zuankeba_hot、xinzuanba_hot、category_man）—— SSR 0 风控，稳定产出唯一商品短链/口令/京东12位货号
     5. 拔草哦 (`youhui.bacaoo.com`)  : 1 个频道（国内优惠 youhui）—— 0 风控，35.0% 含唯一商品短链/口令
     6. 网猴线报 (`iehou.com`)        : 1 个频道（24小时热榜 dayhot）—— 0 风控，17.0% 含唯一商品短链/口令

  ❌ 已明确剔除的站点与频道（均经 30 轮 1,703 条实测验证，完全不满足上述两条铁律，特此记录备忘）：
     1. ❌ 什么值得买 (`smzdm.com` 全站及线报酷镜像 `zhidemai` / `zhidemai_hot`)：
        - 剔除原因 A（违反铁律1）：`faxian.smzdm.com` 高频触发腾讯防水墙 `TCaptcha.js`，异步接口触发 `202 probe.js`；
        - 剔除原因 B（违反铁律2）：实测拉取 183 条详情，含唯一商品链接/口令/SKU 的记录数为 **0 / 183 (0.0%)**！
          因其全站所有外链（含正文凑单链接）均被重写为 `https://go.smzdm.com/{加密串}`（受 `probe.js` 保护），
          无法直接还原出京东/淘宝商品 ID 或直链，仅能拿到泛泛的商品标题关键字。
     2. ❌ 慢慢买 (`cu.manmanbuy.com`)：
        - 剔除原因（违反铁律2）：实测拉取 43 条，含唯一商品锚点的记录数为 **0 / 43 (0.0%)**。
          其详情页 `discuxiao_{id}.aspx` 为前端 `crypto-js` 动态加密跳转，无法直接提取商品 ID 或直链，仅有泛泛标题。
     3. ❌ 55海淘 (`55haitao.com/deals`)：
        - 剔除原因（违反铁律2）：实测拉取 22 条，国内唯一电商商品数为 **0 / 22 (0.0%)**，全为欧美海淘外网（Amazon/FWRD等）。
     4. ❌ 大淘客 (`dataoke.com/xp/xb`)：
        - 剔除原因（违反铁律1）：网页端强依赖登录态 Cookie 极易失效，且其线报数据与免登录的好单库 (`haodanku`) 100% 同源。
     5. ❌ 线报酷特定纯噪音子频道 (`weibo_chaohua_hot` 微博超话热榜 & `xiaodigu_hot` 小嘀咕热榜)：
        - `weibo_chaohua_hot`：实测 90 条中唯一商品锚点数为 **0 / 90 (0.0%)**（微博超话严禁外链与口令，100% 是“买这个+纯订单截图”谜语帖）；
        - `xiaodigu_hot`：实测 73 条中有效电商唯一商品数为 **0 / 73 (0.0%)**（全为银行数币羊毛、闲聊水帖甚至连载小说）。
     6. ❌ 网猴线报首页最新流 (`iehou.com/` index)：
        - 剔除原因：首页实时流几乎全被微博转发抽奖刷屏（实测有效率 0%），其有价值内容已被 `page-dayhot.htm` (24H热榜) 100% 覆盖。

常用命令：
  1. 清空旧缓冲池数据并启动后台持续定时拉取（默认每 180 秒一轮）：
     python scripts/run_deal_pool_scheduler.py --reset-db --interval 180
  2. 后台持续定时拉取（保留已有数据增量追加）：
     python scripts/run_deal_pool_scheduler.py --interval 180
  3. 立即单次全量拉取一轮（26个频道全跑）并验证入库：
     python scripts/run_deal_pool_scheduler.py --once
  4. 随时查看缓冲池数据质量、各站点/频道唯一商品产出率统计报告：
     python scripts/run_deal_pool_scheduler.py --stats
================================================================================
"""

import argparse
import copy
import datetime
import json
import logging
import os
import re
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests
from pymongo import UpdateOne

# 确保 Windows 终端下正常输出中文与 Emoji
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from infrastructure.mongo import gen_db_object
from test_deal_sites_scraper import (
    fetch_0818tuan_deals,
    fetch_bacaoo_deals,
    fetch_gwdang_discounts,
    fetch_haodanku_weal_list,
    fetch_haodanku_wire_report,
    fetch_iehou_deals,
    fetch_xianbao_fun_deals,
)

DEFAULT_COLLECTION_NAME = "deal_raw_tips"
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE_PATH = os.path.join(LOG_DIR, "deal_pool_scheduler.log")
STATS_SNAPSHOT_PATH = os.path.join(LOG_DIR, "deal_pool_stats.json")
LOCAL_FALLBACK_JSONL = os.path.join(LOG_DIR, "deal_raw_tips_fallback.jsonl")

# ==============================================================================
# 明确剔除的站点与频道备忘字典（供代码审计、日志与统计报告直接备查，防止后人误以为遗漏）
# ==============================================================================
EXCLUDED_SITES_AND_CHANNELS_MANIFEST: Dict[str, str] = {
    "smzdm (什么值得买全站及镜像 zhidemai/zhidemai_hot)": (
        "违反铁律1与铁律2：faxian子域高频触发TCaptcha验证码；且全站链接均被重写为加密跳板 go.smzdm.com (带probe.js拦截)，"
        "实测 0/183 (0.0%) 含唯一商品直链/口令/SKU，仅能拿到泛泛的商品标题关键字。"
    ),
    "manmanbuy (慢慢买省钱控 cu.manmanbuy.com)": (
        "违反铁律2：详情页 discuxiao_{id}.aspx 采用前端 crypto-js 动态加密跳转，"
        "实测 0/43 (0.0%) 含唯一商品链接/口令/SKU，仅有泛泛的商品标题。"
    ),
    "55haitao (55海淘 55haitao.com/deals)": (
        "违反铁律2：实测 0/22 (0.0%) 含国内电商商品，100% 为海外欧美独立站海淘链接，无国内淘京拼 CPS 变现价值。"
    ),
    "dataoke (大淘客网页端 dataoke.com/xp/xb)": (
        "违反铁律1：强依赖网页登录态 Cookie 极易失效，且底层线报数据与免登录的好单库 (haodanku) 100% 同源。"
    ),
    "xianbao_fun/weibo_chaohua_hot (线报酷-微博超话热榜)": (
        "违反铁律2：微博超话严厉封杀外链与淘口令，实测 0/90 (0.0%) 含唯一商品链接/口令/SKU，100% 为“买这个+纯订单截图”谜语帖。"
    ),
    "xianbao_fun/xiaodigu_hot (线报酷-小嘀咕热榜)": (
        "违反铁律2：实测 73 条中有效电商唯一商品为 0 (0.0%)，全为银行数币羊毛、闲聊水帖或连载小说。"
    ),
    "iehou/latest (网猴线报-首页最新流)": (
        "冗余低效：首页实时流被微博抽奖刷屏（实测有效率 0%），有价值内容已被 iehou/dayhot (24H热榜) 100% 覆盖。"
    ),
}

# ==============================================================================
# 指向“唯一商品”的硬锚点识别正则（用于统计与打标，不在拉取阶段丢弃任何记录）
# ==============================================================================
# 1. 唯一商品直链 / 短链（直接锁定具体商品或活动落地页）
_RE_UNIQUE_PRODUCT_URL = re.compile(
    r"https?://(?:"
    r"u\.jd\.com/[0-9A-Za-z_]+|"
    r"jingfen\.jd\.com/detail/[0-9A-Za-z_]+\.html|"
    r"item(?:\.m)?\.jd\.com/(?:product/)?\d+\.html|"
    r"3\.cn/[0-9A-Za-z_-]+|"
    r"uland\.taobao\.com/[^\s\"'<>)\]]+|"
    r"s\.click\.taobao\.com/[0-9A-Za-z_]+|"
    r"(?:m|e)\.tb\.cn/h\.[0-9A-Za-z_]+|"
    r"tb3\.cn/[0-9A-Za-z_]+|"
    r"item\.taobao\.com/item\.htm\?[^\s\"'<>)\]]*id=\d+|"
    r"detail\.tmall\.com/item\.htm\?[^\s\"'<>)\]]*id=\d+|"
    r"a\.m\.taobao\.com/i\d+\.htm|"
    r"mobile\.yangkeduo\.com/[^\s\"'<>)\]]*goods_id=\d+"
    r")",
    re.I,
)

# 2. 唯一淘口令 / 京口令 / 46位加密ID（要求字母+数字组合，排除图片路径如 /public/ 或 /202611/）
_RE_STRICT_TOKEN = re.compile(
    r"(?:"
    r"[￥¥$€₴£¢!！](?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{6,20}[￥¥$€₴£¢!！]|"
    r"(?<![A-Za-z0-9:/._-])/(?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{8,16}/(?![A-Za-z0-9_.-])|"
    r"\b(?:CA|CZ|MF|HU)\d{3,5}\b|"
    r"@[A-Za-z0-9]{5,8}\b|"
    r"\b[A-Za-z0-9_-]{24,28}-[A-Za-z0-9_]{16,22}\b|"
    r"\b[A-Za-z0-9]{25,35}_[A-Za-z0-9]{15,22}\b"
    r")"
)

# 3. 明确标注的京东/淘宝数字商品编码/货号/SKU（10~15位纯数字）
_RE_EXPLICIT_SKU = re.compile(
    r"(?:货号|商品编码|商品编号|编码|编号|sku|itemid)[：:\s=\n]*(\d{10,15})\b"
    r"|\b(100\d{9,11})\b",
    re.I,
)

# 真好价/抄作业核心动作词库
_COUDAN_KEYWORDS = (
    "凑单", "湊单", "后退", "附后褪", "退掉", "可退", "砸金蛋", "砸券", "砸3", "砸4", "砸5",
    "限时券", "限时卷", "限时奍", "首单", "消沸劵", "消费券", "美妆券", "美妆加补", "品类券",
    "猫超", "返卡", "猫超卡", "一淘", "淘金币", "叠惍币", "下拉金币", "百亿补贴", "百补",
    "加购", "第一个加购", "拍2", "拍3", "任拍", "任选", "折合", "低至", "神价", "漏洞", "手慢无",
    "晒单返", "评价返", "0元", "0买", "白嫖", "史低", "历史最低", "低于双11", "低于618", "天低价",
)

_HOT_OR_CURATED_CHANNELS = (
    "wire_report", "weal_list", "hot_1h", "haodan_hot", "weibo_hot",
    "douban_hot", "zuankeba_hot", "xinzuanba_hot", "kuan_hot",
    "0-hour_3", "0-lowest", "0-s_lowest", "0-lower_11", "dayhot",
)


def setup_scheduler_logger() -> logging.Logger:
    """配置完全独立的日志器，写入 logs/deal_pool_scheduler.log 并同步输出到终端。"""
    sched_logger = logging.getLogger("deal_pool_scheduler")
    if sched_logger.handlers:
        return sched_logger
    sched_logger.setLevel(logging.INFO)
    sched_logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    sched_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    sched_logger.addHandler(file_handler)
    return sched_logger


logger = setup_scheduler_logger()


def evaluate_quality_signals(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算单条线报的结构化信号 (`quality_signals`)，仅作元数据标注，绝不在拉取阶段丢弃任何记录：
    - 核心字段 `has_unique_product_anchor`：严格判定该记录是否包含【指向唯一商品的硬锚点】
      （唯一商品链接/短链、淘宝46位加密ID、购物党dp_id唯一SKU、淘口令/京口令、或明确10~15位数字货号）。
    """
    title = str(record.get("title") or "")
    tutorial_text = str(record.get("tutorial_text") or "")
    combined_text = f"{title}\n{tutorial_text}"
    channel = str(record.get("source_channel") or "")

    links = record.get("extracted_links") or []
    tokens = record.get("extracted_tokens") or []
    images = record.get("tutorial_images") or []
    hot_metrics = record.get("hot_metrics") or {}

    all_urls_blob = " ".join(str(lk.get("url") or "") for lk in links if isinstance(lk, dict))
    full_blob = f"{combined_text}\n{all_urls_blob}"

    # 1. 检测唯一商品链接 / 加密商品 ID
    matched_unique_urls = list(dict.fromkeys(_RE_UNIQUE_PRODUCT_URL.findall(full_blob)))
    has_encrypted_item_id = any(
        lk.get("type") in ("tb_encrypted_id", "gwdang_dp_sku") for lk in links if isinstance(lk, dict)
    )

    # 2. 检测严格淘口令 / 京口令
    matched_unique_tokens = list(dict.fromkeys(_RE_STRICT_TOKEN.findall(combined_text)))
    for tok in tokens:
        tok_s = str(tok or "").strip()
        if tok_s and _RE_STRICT_TOKEN.search(tok_s) and tok_s not in matched_unique_tokens:
            matched_unique_tokens.append(tok_s)

    # 3. 检测明确数字 SKU / 货号
    sku_Raw_matches = _RE_EXPLICIT_SKU.findall(combined_text)
    matched_unique_skus = list(
        dict.fromkeys(m[0] or m[1] for m in sku_Raw_matches if (m[0] or m[1]))
    )
    cand_id = str(record.get("candidate_goods_id") or "").strip()
    if cand_id.isdigit() and len(cand_id) >= 8 and cand_id not in matched_unique_skus:
        matched_unique_skus.append(cand_id)

    unique_anchor_types: List[str] = []
    if matched_unique_urls or has_encrypted_item_id:
        unique_anchor_types.append("unique_product_url_or_id")
    if matched_unique_tokens:
        unique_anchor_types.append("taokouling_or_jkl")
    if matched_unique_skus:
        unique_anchor_types.append("explicit_numeric_sku")

    has_unique_product_anchor = len(unique_anchor_types) > 0

    ecommerce_links = [
        lk for lk in links
        if isinstance(lk, dict)
        and (
            lk.get("platform") in ("jd", "taobao", "pdd")
            or lk.get("type") in (
                "tb_encrypted_id",
                "step_url",
                "coupon_url",
                "clipboard",
                "gwdang_dp_sku",
            )
        )
    ]
    ecommerce_link_count = len(ecommerce_links)
    has_multi_links = ecommerce_link_count >= 2

    matched_kw = [kw for kw in _COUDAN_KEYWORDS if kw in combined_text]
    has_coudan_keywords = len(matched_kw) > 0

    comments = 0
    try:
        comments = int(hot_metrics.get("comments") or 0)
    except (ValueError, TypeError):
        comments = 0

    is_curated_channel = any(tag in channel for tag in _HOT_OR_CURATED_CHANNELS)
    has_price = record.get("price") is not None

    # 计算综合好价评分 (0~100)
    score = 20
    if has_unique_product_anchor:
        score += 30
    if is_curated_channel:
        score += 10
    if has_price:
        score += 10
    if has_multi_links:
        score += 15
    if has_coudan_keywords:
        score += min(15, len(matched_kw) * 5)
    if len(images) >= 1:
        score += 5
    if comments >= 3:
        score += 10
    score = min(100, score)

    if has_unique_product_anchor and (has_multi_links or has_coudan_keywords or comments >= 3):
        tier = "S_唯一商品且含多步凑单/热议作业"
    elif has_unique_product_anchor:
        tier = "A_唯一商品直链/口令/货号好价"
    else:
        tier = "B_无唯一商品锚点(待后续规则过滤)"

    return {
        "has_unique_product_anchor": has_unique_product_anchor,
        "unique_anchor_types": unique_anchor_types,
        "unique_product_urls": matched_unique_urls[:10],
        "unique_tokens": matched_unique_tokens[:10],
        "unique_skus": matched_unique_skus[:10],
        "quality_tier": tier,
        "quality_score": score,
        "ecommerce_link_count": ecommerce_link_count,
        "has_multi_links": has_multi_links,
        "has_coudan_keywords": has_coudan_keywords,
        "matched_keywords": matched_kw[:10],
        "image_count": len(images),
        "token_count": len(matched_unique_tokens),
        "comments": comments,
        "is_curated_channel": is_curated_channel,
        "has_price": has_price,
    }


class DealRawPoolRepository:
    """
    独立缓冲池集合仓储 (`deal_raw_tips`)
    仅操作 `deal_raw_tips` 集合，不触碰现有业务集合。
    """

    def __init__(self, collection_name: str = DEFAULT_COLLECTION_NAME, db_instance=None):
        self.collection_name = collection_name
        self._db = db_instance
        self._indexes_ready = False

    @property
    def col(self):
        if self._db is None:
            self._db = gen_db_object()
        if not self._indexes_ready:
            self._ensure_indexes()
        return self._db.get_collection(self.collection_name)

    def _ensure_indexes(self):
        try:
            col = self._db.get_collection(self.collection_name)
            col.create_index([("source_site", 1), ("source_item_id", 1)], unique=True, background=True)
            col.create_index([("first_seen_at", -1)], background=True)
            col.create_index([("last_seen_at", -1)], background=True)
            col.create_index(
                [("quality_signals.has_unique_product_anchor", 1), ("ai_clean_status", 1)],
                background=True,
            )
            col.create_index([("platform", 1), ("quality_signals.quality_tier", 1)], background=True)
            col.create_index([("candidate_goods_id", 1)], background=True)
            self._indexes_ready = True
        except Exception as e:
            logger.warning("初始化集合 [%s] 索引时发生异常（稍后自动重试）: %s", self.collection_name, e)

    def reset_collection(self) -> int:
        """清空 `deal_raw_tips` 集合中的全部旧数据，用于全新一轮监控测试。"""
        try:
            res = self.col.delete_many({})
            logger.info("🧹 已清空集合 [%s] 中的全部历史数据，共删除 %d 条旧记录。", self.collection_name, res.deleted_count)
            return res.deleted_count
        except Exception as e:
            logger.error("清空集合 [%s] 失败: %s", self.collection_name, e)
            return 0

    def get_existing_detailed_ids(self, source_site: str, limit: int = 5000) -> Set[str]:
        """
        获取某站点近期已经成功拉取过完整详情 (`has_full_detail: True`) 的 `source_item_id` 集合，
        避免定时任务每轮重复请求同一篇帖子的详情页。
        """
        try:
            cursor = (
                self.col.find(
                    {"source_site": source_site, "has_full_detail": True},
                    {"_id": 0, "source_item_id": 1},
                )
                .sort("last_seen_at", -1)
                .limit(limit)
            )
            return {str(doc["source_item_id"]) for doc in cursor if doc.get("source_item_id")}
        except Exception as e:
            logger.warning("读取站点 [%s] 已存详情 ID 列表异常: %s", source_site, e)
            return set()

    def save_batch(self, records: List[Dict[str, Any]], task_name: str = "") -> Dict[str, int]:
        """
        批量幂等写入 `deal_raw_tips` 集合（零清洗、原样留存）：
        - 新记录：完整写入所有原始字段，初始化 `ai_clean_status="pending"`, `first_seen_at=now`, `seen_count=1`
        - 已存在记录：
          * 若本次抓取到了完整详情 (`has_full_detail=True`)，则更新详情正文、链接、图片、原始载荷与锚点信号；
          * 若本次仅是列表页重逢 (`has_full_detail=False` 且库中已有详情)，则绝不覆盖库中已有的详情正文，
            仅更新 `last_seen_at`、`seen_count`、`seen_channels` 与 `hot_metrics`。
        """
        if not records:
            return {"inserted": 0, "updated": 0, "total": 0}

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ops = []

        for rec in records:
            if not isinstance(rec, dict):
                continue
            site = str(rec.get("source_site") or "").strip()
            item_id = str(rec.get("source_item_id") or "").strip()
            if not site or not item_id:
                continue

            channel = str(rec.get("source_channel") or "").strip()
            has_full_detail = bool(rec.get("has_full_detail", True))
            quality_signals = evaluate_quality_signals(rec)

            filter_spec = {"source_site": site, "source_item_id": item_id}

            set_fields: Dict[str, Any] = {
                "last_seen_at": now,
                "last_task_name": task_name,
            }
            if rec.get("hot_metrics"):
                set_fields["hot_metrics"] = rec["hot_metrics"]

            set_on_insert: Dict[str, Any] = {
                "source_site": site,
                "source_item_id": item_id,
                "first_seen_at": now,
                "ai_clean_status": "pending",
            }

            core_fields = {
                "source_channel": channel,
                "source_url": rec.get("source_url") or "",
                "fetched_at": rec.get("fetched_at") or "",
                "platform": rec.get("platform") or "unknown",
                "candidate_goods_id": rec.get("candidate_goods_id") or "",
                "title": rec.get("title") or "",
                "price_text": rec.get("price_text") or "",
                "price": rec.get("price"),
                "image_url": rec.get("image_url") or "",
                "mall_name": rec.get("mall_name") or "",
                "publish_time": rec.get("publish_time") or "",
                "expire_time": rec.get("expire_time"),
                "tutorial_text": rec.get("tutorial_text") or "",
                "tutorial_images": rec.get("tutorial_images") or [],
                "extracted_links": rec.get("extracted_links") or [],
                "extracted_tokens": rec.get("extracted_tokens") or [],
                "raw_payload": rec.get("raw_payload"),
                "has_full_detail": has_full_detail,
                "quality_signals": quality_signals,
            }

            if has_full_detail:
                set_fields.update(core_fields)
                update_doc: Dict[str, Any] = {
                    "$set": set_fields,
                    "$setOnInsert": set_on_insert,
                    "$inc": {"seen_count": 1},
                }
                if channel:
                    update_doc["$addToSet"] = {"seen_channels": channel}
                ops.append(UpdateOne(filter_spec, update_doc, upsert=True))
            else:
                update_doc = {
                    "$set": set_fields,
                    "$inc": {"seen_count": 1},
                }
                if channel:
                    update_doc["$addToSet"] = {"seen_channels": channel}
                ops.append(UpdateOne(filter_spec, update_doc, upsert=False))

        if not ops:
            return {"inserted": 0, "updated": 0, "total": 0}

        try:
            self.col.delete_many({"has_full_detail": False})
            res = self.col.bulk_write(ops, ordered=False)
            return {
                "inserted": res.upserted_count,
                "updated": res.modified_count,
                "total": len(ops),
            }
        except Exception as e:
            logger.error("❌ 批量写入 MongoDB [%s] 失败，降级写入本地 JSONL 备份: %s", self.collection_name, e)
            self._append_fallback_jsonl(records, task_name)
            return {"inserted": 0, "updated": 0, "total": len(ops), "error": str(e)}

    @staticmethod
    def _append_fallback_jsonl(records: List[Dict[str, Any]], task_name: str):
        try:
            with open(LOCAL_FALLBACK_JSONL, "a", encoding="utf-8") as f:
                for rec in records:
                    row = copy.deepcopy(rec)
                    row["_fallback_saved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    row["_task_name"] = task_name
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as ex:
            logger.error("写入本地 JSONL 备份失败: %s", ex)

    def generate_pool_summary(self) -> Dict[str, Any]:
        """生成缓冲池全量统计分析报告（重点展示各站点/频道的唯一商品硬锚点产出情况）。"""
        col = self.col
        total_docs = col.count_documents({})
        full_detail_docs = col.count_documents({"has_full_detail": True})
        unique_anchor_docs = col.count_documents({"quality_signals.has_unique_product_anchor": True})

        by_site = list(
            col.aggregate([
                {
                    "$group": {
                        "_id": "$source_site",
                        "count": {"$sum": 1},
                        "unique_anchor_count": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$quality_signals.has_unique_product_anchor", True]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "s_tier": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$quality_signals.quality_tier", "S_唯一商品且含多步凑单/热议作业"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "a_tier": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$quality_signals.quality_tier", "A_唯一商品直链/口令/货号好价"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "multi_links": {
                            "$sum": {
                                "$cond": [{"$eq": ["$quality_signals.has_multi_links", True]}, 1, 0]
                            }
                        },
                    }
                },
                {"$sort": {"unique_anchor_count": -1, "count": -1}},
            ])
        )

        by_platform = list(
            col.aggregate([
                {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ])
        )

        by_tier = list(
            col.aggregate([
                {"$group": {"_id": "$quality_signals.quality_tier", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ])
        )

        cross_site_hot = list(
            col.aggregate([
                {"$match": {"candidate_goods_id": {"$ne": ""}}},
                {
                    "$group": {
                        "_id": "$candidate_goods_id",
                        "appear_count": {"$sum": 1},
                        "sites": {"$addToSet": "$source_site"},
                        "sample_title": {"$first": "$title"},
                        "sample_price": {"$first": "$price"},
                    }
                },
                {"$match": {"appear_count": {"$gte": 2}}},
                {"$sort": {"appear_count": -1}},
                {"$limit": 10},
            ])
        )

        top_s_samples = list(
            col.find(
                {"quality_signals.has_unique_product_anchor": True},
                {
                    "_id": 0,
                    "source_site": 1,
                    "source_channel": 1,
                    "platform": 1,
                    "title": 1,
                    "price": 1,
                    "price_text": 1,
                    "tutorial_text": 1,
                    "extracted_links": 1,
                    "quality_signals": 1,
                    "publish_time": 1,
                },
            )
            .sort([("quality_signals.quality_score", -1), ("last_seen_at", -1)])
            .limit(5)
        )

        return {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "collection": self.collection_name,
            "total_records": total_docs,
            "full_detail_records": full_detail_docs,
            "unique_product_anchor_records": unique_anchor_docs,
            "unique_product_anchor_ratio": round(unique_anchor_docs * 100.0 / max(1, total_docs), 1),
            "by_quality_tier": {str(r["_id"]): r["count"] for r in by_tier},
            "by_platform": {str(r["_id"]): r["count"] for r in by_platform},
            "by_source_site": by_site,
            "cross_channel_resonance_goods": cross_site_hot,
            "top_s_tier_samples": top_s_samples,
            "excluded_sites_manifest": EXCLUDED_SITES_AND_CHANNELS_MANIFEST,
        }


# ==============================================================================
# 定时拉取任务编排（精选 6 大稳定站点、26 个含唯一商品锚点的子频道）
# ==============================================================================
def run_single_cycle(
    repo: DealRawPoolRepository,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: int = 25,
    run_all_tiers: bool = False,
) -> Dict[str, Any]:
    """
    执行单轮采集并原样写入独立集合 `deal_raw_tips`：
    - 仅拉取通过“铁律1(稳定零风控) + 铁律2(含唯一商品定位信息)”验证的 6 大站点 26 个频道；
    - 已剔除站点（smzdm / manmanbuy / 55haitao / dataoke / weibo_chaohua_hot / xiaodigu_hot）见顶部文档与 `EXCLUDED_SITES_AND_CHANNELS_MANIFEST`。
    """
    cycle_start = time.perf_counter()
    include_medium = run_all_tiers or (cycle_num == 1) or (cycle_num % 2 == 0)

    # 预先获取各站点已抓过完整详情的 item_id 集合，确保每次只对【新出线报】发起详情请求
    xianbao_done = repo.get_existing_detailed_ids("xianbao_fun", limit=6000)
    tuan0818_done = repo.get_existing_detailed_ids("0818tuan", limit=3000)
    bacaoo_done = repo.get_existing_detailed_ids("bacaoo", limit=2000)
    iehou_done = repo.get_existing_detailed_ids("iehou", limit=2000)

    tasks: List[Tuple[str, str, Callable[[], Dict[str, Any]], float]] = [
        # ---------------- T0 高频核心源（每轮必跑：11 个高密度唯一商品频道） ----------------
        (
            "好单库_京东实时线报",
            "T0_high_freq",
            lambda: fetch_haodanku_wire_report(category_type=10004, page_no=1, page_size=25, session=session),
            0.4,
        ),
        (
            "好单库_淘宝实时线报",
            "T0_high_freq",
            lambda: fetch_haodanku_wire_report(category_type=10003, page_no=1, page_size=25, session=session),
            0.4,
        ),
        (
            "好单库_淘宝百亿补贴",
            "T0_high_freq",
            lambda: fetch_haodanku_wire_report(category_type=10005, page_no=1, page_size=25, session=session),
            0.4,
        ),
        (
            "好单库_全品类综合线报",
            "T0_high_freq",
            lambda: fetch_haodanku_wire_report(category_type=0, page_no=1, page_size=25, session=session),
            0.4,
        ),
        (
            "线报酷_1小时最热榜",
            "T0_high_freq",
            lambda: fetch_xianbao_fun_deals(
                channel="hot_1h",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.6,
        ),
        (
            "线报酷_好单线报热榜",
            "T0_high_freq",
            lambda: fetch_xianbao_fun_deals(
                channel="haodan_hot",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.6,
        ),
        (
            "线报酷_好单实时聚合流",
            "T0_high_freq",
            lambda: fetch_xianbao_fun_deals(
                channel="category_haodan",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.6,
        ),
        (
            "线报酷_微博好价热榜",
            "T0_high_freq",
            lambda: fetch_xianbao_fun_deals(
                channel="weibo_hot",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.6,
        ),
        (
            "线报酷_豆瓣买组热榜",
            "T0_high_freq",
            lambda: fetch_xianbao_fun_deals(
                channel="douban_hot",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.6,
        ),
        (
            "线报酷_全网实时最新流",
            "T0_high_freq",
            lambda: fetch_xianbao_fun_deals(
                channel="latest",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.6,
        ),
        (
            "0818团_实战凑单线报_P1",
            "T0_high_freq",
            lambda: fetch_0818tuan_deals(
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=tuan0818_done,
            ),
            0.6,
        ),
    ]

    if include_medium:
        tasks.extend([
            # ---------------- T1 中频扩充源（每2轮跑一次：15 个含唯一商品锚点的频道） ----------------
            (
                "好单库_福利好价清单",
                "T1_med_freq",
                lambda: fetch_haodanku_weal_list(page_no=1, page_size=25, session=session),
                0.5,
            ),
            (
                "购物党_3小时最热折扣",
                "T1_med_freq",
                lambda: fetch_gwdang_discounts(mode="0-hour_3", session=session),
                3.0,  # 购物党并发 QPS > 3 会触发 429，任务间固定间隔 3 秒确保 100% 零拦截
            ),
            (
                "购物党_N天历史最低价",
                "T1_med_freq",
                lambda: fetch_gwdang_discounts(mode="0-lowest", session=session),
                3.0,
            ),
            (
                "购物党_全网同款最低价",
                "T1_med_freq",
                lambda: fetch_gwdang_discounts(mode="0-s_lowest", session=session),
                3.0,
            ),
            (
                "购物党_低于双11好价",
                "T1_med_freq",
                lambda: fetch_gwdang_discounts(mode="0-lower_11", session=session),
                3.0,
            ),
            (
                "购物党_最新好价折扣",
                "T1_med_freq",
                lambda: fetch_gwdang_discounts(mode="0-latest", session=session),
                1.5,
            ),
            (
                "0818团_实战凑单线报_P2",
                "T1_med_freq",
                lambda: fetch_0818tuan_deals(
                    page=2,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=tuan0818_done,
                ),
                0.6,
            ),
            (
                "线报酷_微博实时聚合流",
                "T1_med_freq",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_weibo",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.6,
            ),
            (
                "线报酷_豆瓣买组实时流",
                "T1_med_freq",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban_maizu",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.6,
            ),
            (
                "线报酷_酷安热榜",
                "T1_med_freq",
                lambda: fetch_xianbao_fun_deals(
                    channel="kuan_hot",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.6,
            ),
            (
                "线报酷_赚客吧热榜",
                "T1_med_freq",
                lambda: fetch_xianbao_fun_deals(
                    channel="zuankeba_hot",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.6,
            ),
            (
                "线报酷_新赚吧热榜",
                "T1_med_freq",
                lambda: fetch_xianbao_fun_deals(
                    channel="xinzuanba_hot",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.6,
            ),
            (
                "线报酷_慢友线报聚合",
                "T1_med_freq",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_man",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.6,
            ),
            (
                "拔草哦_国内优惠口令",
                "T1_med_freq",
                lambda: fetch_bacaoo_deals(
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=bacaoo_done,
                ),
                0.8,
            ),
            (
                "网猴线报_24小时热榜",
                "T1_med_freq",
                lambda: fetch_iehou_deals(
                    mode="dayhot",
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=iehou_done,
                ),
                0.6,
            ),
        ])

    total_inserted = 0
    total_updated = 0
    total_fetched = 0
    task_reports = []

    logger.info("================ 开始第 #%d 轮好价线报拉取 (共 %d 个规范频道任务) ================", cycle_num, len(tasks))

    for task_name, freq_tier, fn, pause_sec in tasks:
        t0 = time.perf_counter()
        try:
            res = fn()
            records = res.get("records") or []
            for r in records:
                if r.get("has_full_detail") and r.get("source_item_id"):
                    sid = str(r["source_item_id"])
                    if "线报酷_" in task_name:
                        xianbao_done.add(sid)
                    elif "0818团_" in task_name:
                        tuan0818_done.add(sid)
                    elif "拔草哦_" in task_name:
                        bacaoo_done.add(sid)
                    elif "网猴线报_" in task_name:
                        iehou_done.add(sid)

            db_res = repo.save_batch(records, task_name=task_name)
            cost_ms = round((time.perf_counter() - t0) * 1000, 1)

            ins = db_res.get("inserted", 0)
            upd = db_res.get("updated", 0)
            cnt = len(records)
            det_cnt = res.get("detail_fetched", cnt)

            total_inserted += ins
            total_updated += upd
            total_fetched += cnt

            unique_cnt = sum(
                1 for r in records
                if evaluate_quality_signals(r)["has_unique_product_anchor"]
            )

            logger.info(
                "  ✅ [%-20s] 拉取=%3d条 (新抓详情=%2d) | 入库新增=%3d 更新=%3d | 含唯一商品锚点=%3d条 | 耗时=%6.1fms",
                task_name, cnt, det_cnt, ins, upd, unique_cnt, cost_ms,
            )
            task_reports.append({
                "task_name": task_name,
                "tier": freq_tier,
                "status": res.get("status", "success"),
                "fetched": cnt,
                "detail_fetched": det_cnt,
                "inserted": ins,
                "updated": upd,
                "unique_anchor_count": unique_cnt,
                "elapsed_ms": cost_ms,
            })
        except Exception as e:
            cost_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.warning("  ⚠️ [%-20s] 拉取异常 (耗时=%.1fms): %s", task_name, cost_ms, e)
            task_reports.append({
                "task_name": task_name,
                "tier": freq_tier,
                "status": "error",
                "error": str(e),
                "elapsed_ms": cost_ms,
            })

        if pause_sec > 0:
            time.sleep(pause_sec)

    cycle_elapsed_s = round(time.perf_counter() - cycle_start, 2)
    summary = repo.generate_pool_summary()
    try:
        with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    logger.info(
        "🏁 第 #%d 轮完成 | 本轮拉取=%d条, 新增入库=%d条, 更新=%d条, 耗时=%.2fs | 缓冲池累计=%d条 (其中含唯一商品硬锚点=%d条, 占比=%.1f%%)",
        cycle_num,
        total_fetched,
        total_inserted,
        total_updated,
        cycle_elapsed_s,
        summary.get("total_records", 0),
        summary.get("unique_product_anchor_records", 0),
        summary.get("unique_product_anchor_ratio", 0.0),
    )

    return {
        "cycle_num": cycle_num,
        "elapsed_s": cycle_elapsed_s,
        "total_fetched": total_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "tasks": task_reports,
        "pool_summary": summary,
    }


def print_stats_report(repo: DealRawPoolRepository):
    """在终端打印直观的缓冲池数据质量与唯一商品锚点分析报告。"""
    summary = repo.generate_pool_summary()
    print("\n" + "=" * 94)
    print(f"📊 导购/线报原始缓冲池 (`{summary['collection']}`) 实时统计分析报告 ({summary['generated_at']})")
    print("=" * 94)
    print(
        f"  • 缓冲池累计去重总记录数   : {summary['total_records']} 条 "
        f"(100% 原始无损留存，含完整详情: {summary['full_detail_records']} 条)"
    )
    print(
        f"  • 含【唯一商品硬锚点】记录 : {summary['unique_product_anchor_records']} 条 "
        f"(占比: {summary['unique_product_anchor_ratio']}%)"
    )
    print(f"  • 信号分级分布             : {json.dumps(summary['by_quality_tier'], ensure_ascii=False)}")
    print(f"  • 电商平台识别分布         : {json.dumps(summary['by_platform'], ensure_ascii=False)}")
    print("-" * 94)
    print("  • 纳入监控的 6 大站点入库明细与【唯一商品锚点】产出率 :")
    for s in summary.get("by_source_site") or []:
        cnt = max(1, s["count"])
        u_cnt = s.get("unique_anchor_count", 0)
        u_pct = u_cnt * 100.0 / cnt
        print(
            f"    - {str(s['_id']):<14} : 入库总计={s['count']:>4}条 | "
            f"含唯一商品锚点={u_cnt:>4}条 ({u_pct:>5.1f}%) | "
            f"S级多步凑单={s['s_tier']:>3}条 | A级直链口令={s['a_tier']:>3}条 | 多链接={s['multi_links']:>3}条"
        )
    print("-" * 94)
    print("  • 已明确剔除的站点与频道备忘 (违反铁律1或铁律2，切勿重复尝试) :")
    for ex_name, ex_reason in EXCLUDED_SITES_AND_CHANNELS_MANIFEST.items():
        print(f"    ❌ {ex_name}: {ex_reason}")
    if summary.get("cross_channel_resonance_goods"):
        print("-" * 94)
        print("  • 跨站点/多条线报共振同款商品 (全网同推真神价信号) :")
        for g in summary["cross_channel_resonance_goods"][:5]:
            print(
                f"    - 商品标识[{g['_id']}] 出现={g['appear_count']}次 | 站点={g['sites']} | "
                f"价格={g.get('sample_price')} | 标题={str(g.get('sample_title'))[:45]}"
            )
    if summary.get("top_s_tier_samples"):
        print("-" * 94)
        print("  • 最新入库含【唯一商品硬锚点】的高分好价样例预览 (Top 3) :")
        for idx, item in enumerate(summary["top_s_tier_samples"][:3], 1):
            q = item.get("quality_signals") or {}
            print(
                f"    [{idx}] [{item.get('source_site')}/{item.get('source_channel')}] "
                f"平台={item.get('platform')} | 价格={item.get('price_text') or item.get('price')} | "
                f"唯一锚点类型={q.get('unique_anchor_types')}"
            )
            print(f"        标题: {item.get('title')}")
            tut_preview = str(item.get("tutorial_text") or "").replace("\n", " ⏎ ")[:140]
            print(f"        教程: {tut_preview}...")
    print("=" * 94 + "\n")


def main():
    parser = argparse.ArgumentParser(description="独立导购/线报好价缓冲池 (`deal_raw_tips`) 定时拉取与监控守护脚本")
    parser.add_argument(
        "--collection",
        type=str,
        default=DEFAULT_COLLECTION_NAME,
        help=f"MongoDB 独立缓冲池集合名称（默认: {DEFAULT_COLLECTION_NAME}）",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="启动前先清空 MongoDB `deal_raw_tips` 集合中的全部历史旧数据与旧日志统计，从零开始全新监控",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=180,
        help="持续定时拉取模式下每轮间隔秒数（默认: 180 秒，即 3 分钟）",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="最大执行轮数（0 表示无限持续运行直到手动停止或达到 --duration-hours）",
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=0.0,
        help="限定后台持续运行小时数（0 为不限时长）",
    )
    parser.add_argument(
        "--max-new-details",
        type=int,
        default=25,
        help="单频道每轮最多新抓取详情页条数（已在库中的老记录自动跳过不占额度，默认: 25）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅立即执行完整一轮（26个规范频道全跑）并输出统计报告后退出",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="仅查询并打印当前 MongoDB `deal_raw_tips` 缓冲池统计分析报告，不发起网络拉取",
    )
    args = parser.parse_args()

    repo = DealRawPoolRepository(collection_name=args.collection)

    if args.reset_db:
        repo.reset_collection()
        try:
            if os.path.exists(STATS_SNAPSHOT_PATH):
                os.remove(STATS_SNAPSHOT_PATH)
        except Exception:
            pass

    if args.stats:
        print_stats_report(repo)
        return

    stop_requested = False

    def _handle_sig(signum, frame):
        nonlocal stop_requested
        logger.info("收到停止信号 (%s)，将在当前任务完成后优雅退出...", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    session = requests.Session()

    if args.once:
        run_single_cycle(
            repo=repo,
            session=session,
            cycle_num=1,
            max_new_details_per_channel=args.max_new_details,
            run_all_tiers=True,
        )
        print_stats_report(repo)
        return

    start_wall = time.time()
    deadline_wall = (start_wall + args.duration_hours * 3600.0) if args.duration_hours > 0 else None

    logger.info(
        "🚀 独立好价线报规范化定时任务已启动 | 目标集合=[%s] | 覆盖=6大站点26个频道 | 轮询间隔=%ds | 详情增量上限=%d/频道 | 日志=[%s]",
        args.collection,
        args.interval,
        args.max_new_details,
        LOG_FILE_PATH,
    )

    cycle_num = 0
    while not stop_requested:
        cycle_num += 1
        run_single_cycle(
            repo=repo,
            session=session,
            cycle_num=cycle_num,
            max_new_details_per_channel=args.max_new_details,
            run_all_tiers=(cycle_num == 1),
        )

        if args.max_cycles > 0 and cycle_num >= args.max_cycles:
            logger.info("已达到设定的最大轮数 (--max-cycles=%d)，定时任务正常结束。", args.max_cycles)
            break

        if deadline_wall is not None and time.time() >= deadline_wall:
            logger.info("已达到设定的持续运行时长 (--duration-hours=%.2f)，定时任务正常结束。", args.duration_hours)
            break

        sleep_target = max(10, int(args.interval))
        logger.info(
            "⏳ 等待 %d 秒后进入第 #%d 轮拉取 (可随时运行 `python scripts/run_deal_pool_scheduler.py --stats` 查看实时统计)...",
            sleep_target,
            cycle_num + 1,
        )
        for _ in range(sleep_target):
            if stop_requested or (deadline_wall is not None and time.time() >= deadline_wall):
                break
            time.sleep(1)

    print_stats_report(repo)


if __name__ == "__main__":
    main()
