# -*- coding: utf-8 -*-
"""
好价线报与采集群定时拉取 (`deal_raw_tips`) + 双阶段 AI 提纯 & 官方转链核验 + 高分好价入库 (`products`) 一体化守护脚本
====================================================================================================================
零、【第一性原理与核心工程铁律（⚠️ 后续所有开发人员与 AI 迭代优化必须严格遵循，严禁违背）】
  ★ 最高准则（极简、确定、绝对可靠 —— KISS & Absolute Reliability First）：
    1. 不要为了一点边角转换率的提升而把系统变得复杂和脆弱！
       例如：面对纯数字商品 ID（`item.jd.com/{数字}.html`），**严禁采用“提取商品全称去反向模糊搜索再比对”这类链路冗长、极不稳定的方案**。
    2. 一切都要可靠！任何数据源、子频道、转链方式或降级兜底，只要不可靠、不稳定、需要靠“猜”或“碰运气”，**直接果断放弃该方案的应用**，绝不妥协！
       - 主商品链接若未转出官方单品详情（`convert_status != "product"`），**直接放弃整条线报**，严禁拿存活的凑单品链接顶替主商品；
       - AI-1 或 AI-2 若偶发调用失败或未返回结果，**直接放弃该条记录**，严禁用启发式规则伪造拆解或伪造高分强行入库；
       - AI-2 若未提取到明确的单一爆料价（`deal_price=null`），保持为空，**严禁回退使用旧爬虫正则抠出的数字**；
       - 对于全站仅提供纯数字 SKU 或加密跳板、无官方推广短链/口令的渠道（如购物党 `gwdang`、慢慢买 `category_man`），**直接从采集源中剔除放弃**。
    3. 代码与 AI 职责边界清晰（告别脆弱正则与启发式补丁，各司其职）：
       - 【确定性代码负责】：多源 HTTP 并发拉取、确定性源站跳板解包（逛丢 `to.php`/`go.php`、拔草哦 `go.bacaoo.com`）、
         源页面地址追溯记录（`source_list_page_url` / `source_detail_url`）、官方转链与商品详情 API 调用
         （`JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal()`）、链接占位符精确回填、
         MongoDB 幂等写入与 `quality_score` 高分保护机制。
       - 【双阶段 AI 负责】：消灭一切脆弱的价格正则、口令截断正则、词表黑名单与启发式规则！
         由 Stage 1 AI 负责群聊同商品附属碎片合并与多链接/口令无损拆解编号；
         由 Stage 2 AI 结合官方转链真实底牌，负责主商品/优惠券/凑单品角色定性、死链与货不对板铁面淘汰、
         爆料到手价提取、抄作业教程重构与 0~100 分综合质量评分（`quality_score`）。

  ★ 五阶段端到端流水线（5-Stage Dual-AI & Official API Pipeline）：
     - 【Stage 1：源站拉取与全量上下文留存 -> 入初步表 `deal_raw_tips`】
       * 每条数据强制记录 `source_list_page_url`（列表页/群聊页地址，方便人工打开网页比对是否漏抓）与 `source_detail_url`（详情页地址）。
       * 对于群聊流（`is_group_stream=True`，即折淘客 150+ 个采集群 `xianbao.aspx`）：**100% 消息全量入库**！
         含潜在商品信号的消息标记为 `clean_status="pending"`；纯文字补充说明或纯晒单截图消息标记为 `clean_status="context_only"`，
         确保群友“连发多条消息组成一个完整爆料”时上下文零丢失。
       * 对于网页/榜单独立帖（`is_group_stream=False`）：每条帖子天然独立，满足极简信号 `has_potential_product_signal` 即入库标记 `pending`。
     - 【Stage 2：AI 阶段一（原始拆解与转链提纯 `prompt/爆料阶段1_原始拆解与转链提纯.txt`）】
       * 若 `is_group_stream=True`，自动从 `deal_raw_tips` 查询同群 (`group_id`) 前后 ±3 分钟（`±180s`）内的相邻消息（含纯文字/纯晒单图消息）一并送入 AI-1；
         若 `is_group_stream=False`，仅传入单帖自身（`raw_messages` 长度为 1），绝不跨帖串味。
       * AI-1 仅合并属于同一主商品的附属碎片消息（严禁合并同群内两个独立商品），将所有电商链接/口令拆解为独立编号（`L1`, `L2`...）并在正文中原位替换为 `[#L1]`, `[#L2]` 占位符，汇总晒单图列表 `extracted_images`。
     - 【Stage 3：官方 API 逐一转链与验真（100% 确定性代码）】
       * 遍历 AI-1 拆出的 `extracted_targets`，逐一调用 `JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal(clean_input)`：
         ① `"product"`：成功返回具体 `goods_id`、`goods_sign` 与官方 `product` 详情字典（含真实标题、日常价、券后价、主图、店铺）及我方推广链 `my_promo_link` / 口令 `my_promo_token`；
         ② `"coupon_or_activity"`：纯优惠券、活动会场、补贴券链接（保留转链后的领券链接/口令或直达券链，绝不丢弃、绝不盲搜冒充单品）；
         ③ `"failed"`：死链、下架或纯数字无签名 ID。
     - 【Stage 4：AI 阶段二（底牌终审、教程重构与质量评分 `prompt/爆料阶段2_底牌终审与教程评分.txt`）-> 先落盘 `deal_raw_tips`】
       * 将 `combined_raw_text`、`extracted_images` 与官方转链底牌 `converted_links` 一并交给 AI-2。
       * AI-2 铁面淘汰“主商品未转出单品仅凑单品存活 (`MAIN_PRODUCT_DEAD_ONLY_FILLER`)”、“指鹿为马货不对板 (`PRODUCT_MISMATCH`)”、“虚拟垃圾 (`VIRTUAL_OR_JUNK`)”；
       * 锁定唯一主商品 `main_link_id`（必须严格为 `convert_status == "product"` 且非凑单品），归位前置优惠券 `coupon_links` 与凑单品 `filler_links`，提取爆料到手价 `deal_price` / 折合单价 `unit_deal_price` / 凑单逻辑 `price_logic`，重构带 `[#L1]` 占位符的 `tutorial_steps` 与 `tutorial_summary`，并给出 0~100 分 `quality_score`。
       * 代码将 `[#L1]` 精确替换为我方专属推广链接/口令后，**先将完整清洗结果与评分落盘回写至 `deal_raw_tips`**。
     - 【Stage 5：高分优品晋升入正式商品库 `products`（Schema 严格隔离 + 高分防覆盖保护）】
       * 仅将 `is_valid_deal=True` 且 `quality_score >= 60` 的高分爆料晋升写入正式表 `products`。
       * **顶层字段与 `deal_info` 严格隔离**：`products` 顶层字段（`name`, `price`, `original_price`, `activity_price`, `coupon_amount`, `image_url`, `mall_name`）**100% 保持官方接口真实原貌**，绝不用爆料价覆盖官方价格！爆料专属的 `deal_price`、`unit_deal_price`、`price_logic`、`coupon_links`、`filler_links`、`tutorial_steps`、`tutorial_text`、`user_share_images`、`quality_score`、`source_list_page_url`、`source_detail_url` 全部收纳于 `deal_info` 子文档中。
       * **高分防覆盖保护**：当同一 `(platform, product_id)` 在 12 小时保鲜期内被多条线报命中时，仅当新线报 `quality_score >= 已有 deal_info.quality_score` 时才更新 `deal_info` 教程与价格，彻底杜绝简陋线报覆盖带券带晒单图的精品作业！

一、【全链路可观测性与运维日志体系】
  - 主运行日志           : `logs/deal_pool_scheduler.log`
  - 最新一轮综合运维报告 : `logs/latest_cycle_ops_report.json`
  - 最新清洗专项统计报告 : `logs/latest_clean_ops_report.json`
  - 历史轮次时序流水     : `logs/cycle_ops_history.jsonl` & `logs/clean_ops_history.jsonl`
  - 抓取与清洗失败样本库 : `logs/deal_pool_errors.jsonl` & `logs/clean_failed_samples.jsonl`
  - 正式入库商品预览     : `logs/clean_success_preview.jsonl`
====================================================================================================================
"""

import argparse
import copy
import datetime
import html as html_lib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import zip_longest
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests
from pymongo import UpdateOne

# 确保 Windows 终端下正常输出中文与 Emoji
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
for _path in (PROJECT_ROOT, WORKSPACE_ROOT, CURRENT_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adapters import get_adapter, platform_name
from infrastructure.ai_api.model_api import generate_content
from infrastructure.mongo import gen_db_object
from models.product import (
    extract_canonical_goods_id,
    extract_jd_image_key,
    is_jd_image_key,
)
from mobile_web.scripts.goods_info_format import string_to_object
from mobile_web.scripts.test_deal_sites_scraper import (
    fetch_0818tuan_deals,
    fetch_bacaoo_deals,
    fetch_guangdiu_deals,
    fetch_gwdang_discounts,
    fetch_haodanku_weal_list,
    fetch_haodanku_wire_report,
    fetch_iehou_deals,
    fetch_xianbao_fun_deals,
    fetch_xuandan_deals,
    fetch_zhetaoke_rank_list,
    fetch_zhetaoke_xianbao_groups,
)

# ==============================================================================
# 集合名称、AI 提示词路径与日志文件路径常量
# ==============================================================================
DEFAULT_RAW_COLLECTION_NAME = "deal_raw_tips"
DEFAULT_FORMAL_PRODUCTS_COLLECTION = "products"
LEGACY_SHADOW_TEST_COLLECTION = "deal_cleaned_products_test"

PROMPT_DIR = os.path.join(PROJECT_ROOT, "prompt")
PROMPT_STAGE1_PATH = os.path.join(PROMPT_DIR, "爆料阶段1_原始拆解与转链提纯.txt")
PROMPT_STAGE2_PATH = os.path.join(PROMPT_DIR, "爆料阶段2_底牌终审与教程评分.txt")

QUALIFIED_DEAL_MIN_SCORE = 60
DEAL_INFO_PROTECTION_HOURS = 12.0

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

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


# ==============================================================================
# 【重要历史档案 1】采集阶段曾尝试并明确剔除的 19 个站点与频道备忘字典
# （均经实测验证违反“零风控稳定拉取”或“无唯一商品锚点”，固化于此严禁后人重复尝试）
# ==============================================================================
EXCLUDED_SITES_AND_CHANNELS_MANIFEST: Dict[str, str] = {
    "smzdm (什么值得买直连及镜像 zhidemai/zhidemai_hot)": (
        "【已尝试并放弃】违反铁律1与铁律2：faxian子域高频触发腾讯TCaptcha验证码；且全站外链均被重写为加密跳板 "
        "go.smzdm.com (带probe.js浏览器指纹校验)、图片重写为 zdmimg.com，实测 0/183 (0.0%) 含唯一商品直链/口令/SKU。"
        "（替代方案：其高优好价已通过【逛丢 guangdiu】服务端明文解包曲线捕获！）"
    ),
    "manmanbuy (慢慢买省钱控 cu.manmanbuy.com)": (
        "【已尝试并放弃】违反铁律2：详情页 discuxiao_{id}.aspx 采用前端 crypto-js 动态加密跳转，"
        "实测 0/43 (0.0%) 含唯一商品链接/口令/SKU，仅有泛泛的商品标题。"
    ),
    "55haitao (55海淘 55haitao.com/deals)": (
        "【已尝试并放弃】违反铁律2：实测 0/22 (0.0%) 含国内电商商品，100% 为海外欧美独立站海淘链接，无国内淘京拼 CPS 变现价值。"
    ),
    "dataoke (大淘客网页端 dataoke.com/xp/xb)": (
        "【已尝试并放弃】违反铁律1：强依赖网页登录态 Cookie 极易失效，且底层线报数据与免登录的好单库 (haodanku) 及折淘客 (zhetaoke) 100% 同源。"
    ),
    "shihuizhu & huim (实惠猪 shihuizhu.com / 惠喵 huim.com)": (
        "【已尝试并放弃】违反铁律1：直连请求被服务端防火墙直接 TCP Reset (ConnectionResetError 10054) 阻断，无法稳定自动拉取。"
    ),
    "qingtaoke & maishoudang & haopintui (轻淘客 / 买手党 / 好品推)": (
        "【已尝试并放弃】违反铁律1：实测连接超时、站点关停或接口已失效。"
    ),
    "meidebi (没得比 meidebi.com)": (
        "【已尝试并放弃】违反铁律2：全站商品外链均为内部加密跳板且正文无明文淘口令或数字 SKU，实测有效唯一商品直链为 0。"
    ),
    "juchaozhi (聚超值 best.pconline.com.cn)": (
        "【已尝试并放弃】违反铁律2：已转型为长篇数码评测文章站，528KB 首页仅 2 个静态广告位，无实时好价商品流。"
    ),
    "haodanku_fastbuy & duohaodan (好单库快抢接口 / 多好单 duohaodan.com)": (
        "【已尝试并放弃】违反铁律1：未登录态下返回 status:0 (fastwait.png) 占位图或强制要求登录"
        "（替代方案：同类快抢群发素材已由免登录的【选单网 xuandan】100% 覆盖）。"
    ),
    "zuanke8_origin & xiaodao & iqnew & huluxia (赚客吧原站 / 小刀娱乐网 / 爱Q生活网 / 葫芦侠)": (
        "【已尝试并放弃】违反铁律1与铁律2：原站存在防CC盾或超时；且小刀/爱Q/葫芦侠/免费福利板块实测全为网盘/游戏/软件资源而非电商商品"
        "（替代方案：赚客吧电商线报已通过 xianbao_fun/category_zuankeba 完整捕获）。"
    ),
    "xianbao_fun/weibo_chaohua_hot (线报酷-微博超话热榜)": (
        "【已尝试并放弃】违反铁律2：微博超话严厉封杀外链与淘口令，实测 0/90 (0.0%) 含唯一商品链接/口令/SKU，100% 为“买这个+纯订单截图”谜语帖。"
    ),
    "xianbao_fun/xiaodigu_hot (线报酷-小嘀咕热榜)": (
        "【已尝试并放弃】违反铁律2：实测 73 条中有效电商唯一商品为 0 (0.0%)，全为银行数币羊毛、闲聊水帖或连载小说。"
    ),
    "iehou/latest (网猴线报-首页最新流)": (
        "【已尝试并放弃】冗余低效：首页实时流被微博抽奖刷屏（实测有效率 0%），有价值内容已被 iehou/dayhot (24H热榜) 100% 覆盖。"
    ),
    "gwdang_all_channels (购物党全频道 0-hour_3 / 0-latest / 0-lowest / 0-s_lowest / 0-lower_11)": (
        "【已尝试并彻底放弃】违反绝对可靠原则：经 8 轮完整周期实测处理 320 条购物党记录，其中 319 条失败（99.7% 失败率："
        "`ERR_JD_NUMERIC_SKU`=130, `ERR_ADAPTER_CONVERT_FAILED`=81, `ERR_EMPTY_PRODUCT_IDENTITY`=53, `ERR_TB_NUMERIC_ID`=36），"
        "唯一 1 条成功记录系主商品纯数字京东链接失败后将凑单品误顶替为主商品。根因在于购物党全站主商品均仅提供纯数字 SKU 链接 "
        "(`item.jd.com/{数字}.html` / `detail.tmall.com/item.htm?id={数字}` / `goods.html?goods_id={数字}`)，"
        "官方三大联盟接口 100% 拒识纯数字 ID，且严禁使用脆弱的“商品全称反向模糊搜索”，故整站果断放弃！"
    ),
    "xianbao_fun/category_man (线报酷-慢友线报聚合 / 慢慢买镜像)": (
        "【已尝试并放弃】违反绝对可靠原则：实测 90 条记录中 87 条失败（97.8% 失败率），底层全为 `cu.manmanbuy.com/discuxiao_{id}.aspx` "
        "前端加密跳板，无明文商品推广链接或口令，果断剔除。"
    ),
    "zhetaoke_inactive_pdd_subgroups (折淘客特定拼多多群 id=10000039/10000068/10000107 及 q=拼多多 搜索)": (
        "【拼多多专项探索：已尝试并放弃】实测折淘客下拉框中的 `10000039 (MK拼多多采集群)`、`10000068 (高佣拼多多采集群)`、"
        "`10000107 (拼多北大学采集群)` 返回 0 条记录（群已停更）；且 `xianbao.aspx?q=拼多多` 服务端不支持按平台词过滤。"
        "（有效方案：`plat='3'` 即 `折淘客_拼多多采集群汇总` 已 100% 汇总折淘客全站所有活跃群的拼多多线报）。"
    ),
    "haodanku_wire_report_pdd_and_tehuigou (好单库 wire_report 各 type 参数及 /haodan/tehuigou.html)": (
        "【拼多多专项探索：已尝试并放弃】实测遍历 `get_wire_report_items` 的 `type=0~5` 及 `category_type=0,10001~10008,10367` "
        "均只有淘宝(type=1/2)与京东(type=3)，拼多多条数为 0；网页端 `/haodan/tehuigou.html` 仅为 460 字节前端 JS 空壳 (`cms_web.min.js`)，无 SSR 数据。"
        "（替代方案：好单库体系内的拼多多线报已通过【线报酷 `category_haodan_pdd` (`/category-haodan-pdd/`)】100% 捕获，实测 20/20 含 `p.pinduoduo.com` 短链）。"
    ),
    "xianbao_fun/category_zhidemai_pdd & category_manmanmai_pdd (线报酷-值得买拼多多 / 慢慢买拼多多子流)": (
        "【拼多多专项探索：已尝试并放弃】虽然 `xb-app.min.js` 中定义了 `/category-zhidemai-pdd/` 与 `/category-manmanmai-pdd/` 路由，"
        "但实测其详情页外链依然是 `go.smzdm.com` (带 probe.js 指纹锁) 和 `cu.manmanbuy.com/discuxiao_.aspx` (前端加密跳板)，0% 含明文 `p.pinduoduo.com` 短链。"
        "（替代方案：什么值得买的拼多多好价已通过【逛丢 `channel='pdd'` (`cate.php?m=拼多多`)】服务端 `go.php` 解包出带 `goods_sign` 的 `duo_coupon_landing.html` 100% 捕获）。"
    ),
    "guangdiu_cheaps_php_empty (逛丢九块九 cheaps.php 及 cheaps.php?m=拼多多)": (
        "【拼多多专项探索：已尝试并放弃】实测 `https://guangdiu.com/cheaps.php` 与 `cheaps.php?m=拼多多` 服务端返回的 `<div class='cheapcontent'></div>` 为空容器（0 条商品）。"
        "（有效方案：必须使用 `https://guangdiu.com/cate.php?m=%E6%8B%BC%E5%A4%9A%E5%A4%9A`，每页稳定返回 30 条纯拼多多好价）。"
    ),
}


# ==============================================================================
# 【核心工程准则备忘录】五阶段双 AI 赋能 + 官方转链验真 + 高分保护准则
# ==============================================================================
ENGINEERING_PHILOSOPHY_MANIFEST: Dict[str, str] = {
    "principle_00_kiss_and_absolute_reliability_first": (
        "【最高准则：极简与绝对可靠（KISS & Absolute Reliability First）】"
        "1) 不要为了一点边角转换率而把系统变得复杂脆弱（如严禁使用“商品全称反向模糊搜索解析纯数字ID”这种长链路不稳定方案）；"
        "2) 一切都要可靠，凡是不可靠、不稳定、需要靠猜或启发式硬凑的方案（如拿存活凑单品顶替主商品、AI 失败时伪造高分入库、回退旧正则价格、抓取全站纯数字ID源）一律直接放弃！"
    ),
    "principle_01_source_page_traceability_and_zero_context_loss": (
        "【Stage 1：源页面可追溯与群聊全量留存】每条数据必须记录 `source_list_page_url`（列表页/群聊页地址）与 `source_detail_url`（详情页地址），"
        "方便人工随时打开对应页面比对是否漏抓；折淘客实时采集群（`is_group_stream=True`）100% 保存群内所有消息（含纯文字教程补充与纯晒单截图 `context_only`），"
        "彻底解决群聊连发多条消息时的上下文粘连丢失问题。"
    ),
    "principle_02_ai_stage1_semantic_splitting_instead_of_regex": (
        "【Stage 2：用 Stage 1 AI 替代脆弱正则拆链与群聊合并】彻底取消用复杂正则表达式抠价格、抠口令或按顺序硬猜主商品的做法；"
        "对群聊流传入同群 ±3 分钟（`±180s`）上下文，对网页/榜单流传入单帖，由 Stage 1 AI 仅合并同一主商品的附属碎片（严禁合并同群不同商品），"
        "完成全部链接/口令无损拆解编号（`L1, L2...`）与正文 `[#L1]` 占位符替换。"
    ),
    "principle_03_official_api_conversion_for_both_products_and_coupons": (
        "【Stage 3：官方 API 转链验真且优惠券与主商品同等重要】遍历 Stage 1 AI 拆出的所有编号目标，调用官方 `Adapter.resolve_and_convert_universal()`："
        "既解析出唯一商品的官方真实详情（`convert_status='product'`），也完整保留优惠券/补贴券/活动会场的转链结果（`convert_status='coupon_or_activity'`），"
        "绝不丢弃前置领券链接，也绝不对优惠券链接或会场多商品链接做模糊搜索冒充单品。"
    ),
    "principle_04_ai_stage2_audit_and_strict_schema_separation": (
        "【Stage 4 & Stage 5：Stage 2 AI 底牌终审、评分防覆盖与 Schema 严格隔离】由 Stage 2 AI 结合官方真实商品详情与原始文案进行终审，"
        "淘汰“主商品未转出单品仅凑单存活 (`MAIN_PRODUCT_DEAD_ONLY_FILLER`)”及“货不对板 (`PRODUCT_MISMATCH`)”，生成步骤化教程并打分（0~100 `quality_score`）。"
        "结果先持久化至 `deal_raw_tips`，再将 `quality_score >= 60` 的优品晋升入 `products`：`products` 顶层价格/标题 100% 保持官方原貌，"
        "爆料价 `deal_price`、优惠券 `coupon_links`、凑单品 `filler_links`、教程与晒单图全部放入 `deal_info`，且高分爆料受保护不被低分简陋线报覆盖。"
    ),
    "principle_05_parallel_producer_consumer_and_early_termination": (
        "【高吞吐架构：拉取/清洗双线程并行 + 转链并发化 + 唯一商品标识提前终止（Early Termination）】"
        "1) Stage 1 多源定时拉取（Producer）与 Stage 2~5 积压清洗（Consumer）在持续模式下分为两个独立线程并行运行，互不耽搁时间；"
        "2) Stage 2/4 AI 采用 `batch_size=10` 批量分组 + `20` 线程并发请求；Stage 3 官方转链按 `(platform, clean_input)` 去重后采用 `10` 线程并发调用；"
        "3) 双层确定性提前终止（Early Termination）：当单锚点线报在 Stage 2 前命中 `anchor_cache` 已入库商品，或 Stage 3 转链拿到商品唯一标识 `(platform, goods_id)` "
        "发现该商品已在正式库 `products`（12h 保鲜期内）或同批次已有更优线报正在处理时，立即提前终止并跳过后续 AI 调用（标记 `OK_SKIPPED_ALREADY_CLEANED`），"
        "仅向正式表 `products` 累加跨群/跨站共振热度，大幅节省工作量与 AI 算力！"
    ),
}


# ==============================================================================
# 【重要历史档案 2】清洗与解包阶段曾尝试过的探索备忘录（含失败死胡同与验证成功方案）
# ==============================================================================
CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST: Dict[str, str] = {
    "dead_end_01_over_engineered_regex_extractors_and_price_guessing": (
        "【曾尝试并移除】编写冗长脆弱的正则表达式去提取价格、截断口令或抠取多链接，并用提取出的爆料价直接覆盖商品顶层 `activity_price`。"
        "【问题教训】正则无法应对“一行粘连多个链接”、“拍3件共29.9元 vs 单件9.9元”等复杂语义，且用爆料价覆盖官方价格会破坏商品库基准价格体系。"
        "【当前做法】由 Stage 1 AI 无损拆解多链接/口令，由官方 API 获取真实顶层商品价格，由 Stage 2 AI 提取 `deal_price` / `unit_deal_price` 独立存放于 `deal_info`。"
    ),
    "dead_end_02_sequential_first_link_wins_and_discarding_coupons": (
        "【曾尝试并移除】按顺序遍历候选链接，只要第一个链接转出商品就当成主商品 break 退出，且将转不出单品的优惠券短链当成失败丢弃。"
        "【问题教训】当第 1 个链接是优惠券（转不出单品被丢弃）、第 2 个主商品死链、第 3 个凑单品存活时，顺序遍历会把凑单品当成主商品上架（指鹿为马），"
        "且丢失前置优惠券会导致京东/淘宝商品购买时比爆料价贵得多。"
        "【当前做法】Stage 3 对拆出的全部链接逐一转链并区分 `product` 与 `coupon_or_activity`，Stage 4 由 AI-2 对照全量底牌锁定唯一主商品、归位优惠券与凑单品。"
    ),
    "dead_end_03_title_fuzzy_search_and_reverse_title_resolution": (
        "【曾尝试并严厉禁止】当商品链接转链失败、为纯数字 SKU (`item.jd.com/{数字}.html`) 或遇到优惠券短链时，用商品全称或标题调用全网搜索 `search_goods` 反向解析或取首条顶替。"
        "【问题教训】用商品全称反向搜索链路极长且极不稳定，极易抓到同款不同规格、第三方山寨店或凑单品，造成“牛头不对马嘴”事故。"
        "【当前做法】彻底放弃对纯数字 SKU 的反向标题解析；`JdAdapter` / `PddAdapter` 仅在推广短链 100% 唯一命中单件商品 (`len==1` 且 `total_count==1`) 时采纳详情，否则直接放弃。"
    ),
    "dead_end_04_heuristic_fallbacks_when_ai_or_main_link_fails": (
        "【曾尝试并彻底移除】1) 当 AI-2 选定的 `main_link_id` 不是 `product` 时，用 `prod_convs[0]` 盲目替补主商品（实测导致将凑单洗衣液当成主商品紫草油入库）；"
        "2) 当 AI-2 返回 `deal_price=null` 时，回退读取旧爬虫正则字段 `doc.get('price')`（实测将“80抽湿巾”抠成 80.0 元）；"
        "3) 当 AI-1/AI-2 超时失败时，用 `_fallback_stage1_extract` / `_fallback_stage2_evaluate` 伪造 72 分高分放行入库。"
        "【当前做法】严格执行“不可靠即直接放弃”：主商品非 `product` 直接淘汰，AI 未返回结果直接淘汰，`deal_price=null` 保持为空绝不回退旧正则。"
    ),
    "success_note_01_pdd_four_high_yield_channels_and_bacaoo_unwrap": (
        "【拼多多专项探索：已验证成功并上线】四大高转化拼多多专属源无缝融入五阶段管线：\n"
        "  1) 逛丢 `channel='pdd'` (`cate.php?m=拼多多`, P1/P2)：服务端 `go.php` 解包出带 `goods_sign` 的 `duo_coupon_landing.html`；\n"
        "  2) 线报酷 `channel='category_haodan_pdd'` (`/category-haodan-pdd/`, P1/P2)：每页 100 条纯拼多多好单线报，100% 含 `p.pinduoduo.com` 短链；\n"
        "  3) 线报酷 `channel='category_weibo_pdd'` (`/category-weibo-pdd/`) + `id='art-fujia'` 1L~5L 楼主同步评论区解析；\n"
        "  4) 拔草哦 `channel='pdd'` (`https://youhui.bacaoo.com/mall/1827`) + `POST https://go.bacaoo.com/` 解包单品与 50 款合集帖。"
    ),
}


# ==============================================================================
# 标准化清洗错误码体系 (`CLEAN_ERROR_CODES`) —— 供运行后统计分析各失败类型占比
# ==============================================================================
OK_SKIPPED_ALREADY_CLEANED = "OK_SKIPPED_ALREADY_CLEANED"     # 唯一商品标识(platform, goods_id)已在正式库/同批清洗过，提前终止跳过AI并累加共振
ERR_NO_ANCHOR = "ERR_NO_ANCHOR"                               # Stage 1 AI 未检测到任何有效电商链接或口令
ERR_MERGED_INTO_NEIGHBOR = "ERR_MERGED_INTO_NEIGHBOR"         # 群聊消息已作为补充碎片合并至同群相邻主线报
ERR_UNSUPPORTED_PLATFORM = "ERR_UNSUPPORTED_PLATFORM"         # 唯品会/美团/抖音等非淘京拼支持平台
ERR_TB_NUMERIC_ID = "ERR_TB_NUMERIC_ID"                       # 淘宝/天猫纯数字商品ID（上游接口要求加密ID/口令/短链）
ERR_PDD_NUMERIC_ID = "ERR_PDD_NUMERIC_ID"                     # 拼多多纯数字goods_id（上游接口要求goods_sign或推广短链）
ERR_JD_NUMERIC_SKU = "ERR_JD_NUMERIC_SKU"                     # 京东纯数字SKU（上游接口要求京粉加密链/u.jd.com短链/口令）
ERR_ADAPTER_OFF_PROMOTION = "ERR_ADAPTER_OFF_PROMOTION"       # 商品已下架、已取消推广或活动过期失效
ERR_ADAPTER_CONVERT_FAILED = "ERR_ADAPTER_CONVERT_FAILED"     # 上游转链接口未解析出有效结果（如口令过期、外链无效等）
ERR_EMPTY_PRODUCT_IDENTITY = "ERR_EMPTY_PRODUCT_IDENTITY"     # 仅含纯优惠券/红包/活动会场链接，无具体主商品
ERR_AI2_MAIN_DEAD_ONLY_FILLER = "ERR_AI2_MAIN_DEAD_ONLY_FILLER"  # 主商品死链、仅凑单品存活（铁面拦截）
ERR_AI2_PRODUCT_MISMATCH = "ERR_AI2_PRODUCT_MISMATCH"         # 转链出的官方商品与爆料文案严重货不对板（AI-2 铁面拦截）
ERR_AI2_VIRTUAL_OR_JUNK = "ERR_AI2_VIRTUAL_OR_JUNK"           # 0.01元引流/邮费补差价/虚拟垃圾商品（AI-2 铁面拦截）
ERR_LOW_QUALITY_SCORE = "ERR_LOW_QUALITY_SCORE"               # 有效商品但综合质量评分低于60分（仅留存初步表，不晋升正式表）


# ==============================================================================
# 运维人员故障处理手册 (`OPS_TROUBLESHOOTING_PLAYBOOK`)
# ==============================================================================
SITE_DISPLAY_NAMES: Dict[str, str] = {
    "zhetaoke": "折淘客 (150+采集群/爆款榜)",
    "haodanku": "好单库 (实时线报群/福利单)",
    "guangdiu": "逛丢 (全网/值得买/拼多多明文还原)",
    "xuandan": "选单网 (淘客快抢群发素材)",
    "0818tuan": "0818团 (实战凑单线报)",
    "xianbao_fun": "线报酷 (21频道全网与拼多多聚合)",
    "bacaoo": "拔草哦 (国内优惠与拼多多商城专区)",
    "iehou": "网猴线报 (24H热榜)",
}

OPS_TROUBLESHOOTING_PLAYBOOK: Dict[str, Dict[str, str]] = {
    "zhetaoke": {
        "endpoint": "https://www.zhetaoke.com/xianbao.aspx | https://www.zhetaoke.com/list/love_1_0_1.html",
        "http_403_429": "折淘客触发频率限制：请将任务间隔 pause_sec 从 0.4s 调高至 1.5s，或检查服务器出口 IP 是否被临时限流。",
        "empty_or_parse": "折淘客返回 0 条记录：若特定群 group_id 夜间无新消息属正常现象；若 all_groups/jd_groups 连续 3 轮为 0，请打开错误路径 URL 检查 `.sc-product` DOM 类名是否改版。",
        "timeout_or_conn": "折淘客连接超时/重置：偶发网络抖动会在下一轮自动恢复；若连续 ≥3 轮超时，请执行 `curl -I https://www.zhetaoke.com/xianbao.aspx` 检查 DNS 与连通性。",
    },
    "haodanku": {
        "endpoint": "https://www.haodanku.com/Index/get_wire_report_items | https://www.haodanku.com/Weal/get_weal_list",
        "http_403_429": "好单库 Ajax 接口返回 403/429：请检查 `test_deal_sites_scraper.py` 中 `Referer` 与 `X-Requested-With: XMLHttpRequest` 请求头是否完整。",
        "empty_or_parse": "好单库返回空列表：若仅 `飞猪旅行生活券群(10367)` 在夜间返回 0 条属群内暂无人发单；若京东(10004)/淘宝(10003)返回 0 条，请浏览器访问错误路径核查接口字段。",
        "timeout_or_conn": "好单库请求超时：偶发连接池陈旧套接字断开，脚本已在每轮开始时自动重建 Session，下一轮将自动恢复。",
    },
    "guangdiu": {
        "endpoint": "https://guangdiu.com/ | https://guangdiu.com/cate.php?m=%E6%8B%BC%E5%A4%9A%E5%A4%9A | https://guangdiu.com/go.php?id={id}",
        "http_403_429": "逛丢触发限流：请调低 `--max-new-details`（如从 20 调为 12）或增大详情页请求间隔。",
        "empty_or_parse": "逛丢列表或详情解析为 0：请访问报错的 `cate.php`、`detail.php?id=` 或 `go.php?id=` 路径，检查 `div.gooditem` 或 `go.php` 中的 `var <随机名>='...'` 脚本格式是否变更。",
        "timeout_or_conn": "逛丢请求超时：单次详情页超时不影响同批其余商品入库，已跳过的 ID 会在下一轮自动补抓。",
    },
    "xuandan": {
        "endpoint": "https://www.xuandan.com/NewHome/msKqList",
        "http_403_429": "选单网接口限流：请确认请求头包含 `Referer: https://www.xuandan.com/NewHome/fastBuy`。",
        "empty_or_parse": "选单网返回 0 条：整点场 (`hour_type=1`) 在非整点促销时段可能为空；若全天场 (`hour_type=''`) 为空，请检查 JSON 响应 `List` 数组结构。",
        "timeout_or_conn": "选单网连接超时：单轮超时无需人工干预，若连续 ≥3 轮失败请测试 `https://www.xuandan.com/NewHome/fastBuy` 可用性。",
    },
    "0818tuan": {
        "endpoint": "http://www.0818tuan.com/list-1-{page}.html | http://www.0818tuan.com/xb/{id}.html",
        "http_403_429": "0818团访问受限：请检查默认 User-Agent 或降低单轮详情页抓取上限。",
        "empty_or_parse": "0818团列表为空：请检查 `http://www.0818tuan.com/list-1-0.html` 中 `#redtag` 列表容器 DOM 是否改动。",
        "timeout_or_conn": "0818团详情页超时：单篇帖子超时已记录到 `deal_pool_errors.jsonl`，下一轮会自动重试未入库详情。",
    },
    "xianbao_fun": {
        "endpoint": "https://new.xianbao.fun/ | https://new.xianbao.fun/category-haodan-pdd/ | https://new.xianbao.fun/{cat}/{id}.html",
        "http_403_429": "线报酷触发频率限制：请将线报酷各频道间 `pause_sec` 从 0.5s 提升至 1.0s。",
        "empty_or_parse": "线报酷某分类返回 0 条：请访问错误路径 URL 检查 `tr.article-list` 表格行结构是否正常。",
        "timeout_or_conn": "线报酷单篇详情页超时：线报酷包含 22 个细分频道，单篇详情超时不影响整体入库，下一轮会自动补抓。",
    },
    "bacaoo": {
        "endpoint": "https://youhui.bacaoo.com/ | https://youhui.bacaoo.com/mall/1827 | https://go.bacaoo.com/",
        "http_403_429": "拔草哦返回 403/429：请调大请求间隔或检查 User-Agent。",
        "empty_or_parse": "拔草哦解析为 0 条：请访问 `https://youhui.bacaoo.com/mall/1827` 检查 `.publish-list` 节点或 `POST https://go.bacaoo.com/` 响应是否变更。",
        "timeout_or_conn": "拔草哦连接超时：站点带宽较小偶有抖动，连续 ≥3 轮失败再排查网络。",
    },
    "iehou": {
        "endpoint": "https://www.iehou.com/index/index/dayhot.html | https://www.iehou.com/Official/{id}.html",
        "http_403_429": "网猴线报触发限流：请检查请求间隔。",
        "empty_or_parse": "网猴线报 24H 热榜返回 0 条：请检查 `dayhot.html` 页面结构。",
        "timeout_or_conn": "网猴线报详情页超时：单篇超时会在下一轮自动补抓。",
    },
}


def get_ops_troubleshooting_advice(
    site_key: str,
    http_status: Optional[int],
    error_type: str,
    fetched_count: int,
    consecutive_failures: int,
) -> str:
    """根据站点标识、HTTP 状态码、错误类型及连续失败次数，生成明确的运维人员处理建议。"""
    playbook = OPS_TROUBLESHOOTING_PLAYBOOK.get(site_key, {})
    escalation = (
        f"【🚨 连续 {consecutive_failures} 轮异常，需人工介入】"
        if consecutive_failures >= 3
        else "【💡 偶发单轮波动，系统将在下轮自动重试，暂无需人工干预】"
    )
    if http_status in (401, 403, 429, 503) or "429" in error_type or "403" in error_type:
        base_advice = playbook.get(
            "http_403_429",
            "目标站点返回风控/限流状态码，请调大频道间 sleep 间隔或检查请求头/代理 IP。",
        )
    elif "Timeout" in error_type or "Connection" in error_type or "ChunkedEncoding" in error_type:
        base_advice = playbook.get(
            "timeout_or_conn",
            "网络请求超时或连接重置，系统将在下一轮自动重建连接池并重试；若持续超时请检查服务器出口网络。",
        )
    elif http_status == 200 and fetched_count == 0:
        base_advice = playbook.get(
            "empty_or_parse",
            "HTTP 200 但解析到 0 条记录，可能是该细分频道当前时段无新帖，或页面 DOM 结构改版，请点开错误路径 URL 核对。",
        )
    else:
        base_advice = playbook.get(
            "timeout_or_conn",
            "请点击错误路径 URL 在浏览器或 curl 中复现响应内容，核对接口状态。",
        )
    return f"{escalation} {base_advice}"


# ==============================================================================
# 阶段一极简信号检测常量（零复杂正则，仅用最简单稳定的关键字/符号判断）
# ==============================================================================
_SIGNAL_URL_OR_DOMAIN_KEYWORDS = (
    "http://",
    "https://",
    "jd.com",
    "3.cn",
    "jingfen",
    "tb.cn",
    "taobao",
    "tmall",
    "uland",
    "yangkeduo",
    "pinduoduo",
)

_SIGNAL_TOKEN_SYMBOLS = (
    "￥",
    "¥",
    "$",
    "€",
    "₴",
    "£",
    "¢",
    "!",
    "！",
    "/",
    "口令",
)

_SIGNAL_SKU_KEYWORDS = (
    "货号",
    "编号",
    "编码",
    "sku",
    "itemid",
    "商品id",
)

_UNSUPPORTED_EXTERNAL_DOMAINS = (
    "vip.com",
    "douyin.com",
    "meituan.com",
    "yuebai.plus",
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


# ==============================================================================
# 日志器初始化
# ==============================================================================
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
        maxBytes=15 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    sched_logger.addHandler(file_handler)
    return sched_logger


logger = setup_scheduler_logger()


def append_error_records_jsonl(error_records: List[Dict[str, Any]]):
    """将本轮捕获的所有列表级与详情级错误路径持久化追加写入 logs/deal_pool_errors.jsonl。"""
    if not error_records:
        return
    try:
        with open(ERROR_LOG_JSONL_PATH, "a", encoding="utf-8") as f:
            for item in error_records:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("写入错误路径日志 [%s] 失败: %s", ERROR_LOG_JSONL_PATH, e)


# ==============================================================================
# 阶段一核心：极简潜在商品信号判断 (`has_potential_product_signal`) 与初步表仓储
# ==============================================================================
def has_potential_product_signal(record: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    【遵循第一阶段极简准则】：不使用任何复杂脆弱的正则表达式，仅通过最基础的关键字或符号
    判断该记录中“是否可能包含想要的商品信息”。只要命中任一基础信号，就值得入初步表 `deal_raw_tips`。
    """
    signal_types: List[str] = []

    links = record.get("extracted_links") or []
    tokens = record.get("extracted_tokens") or []
    cand_id = str(record.get("candidate_goods_id") or "").strip()

    if any(isinstance(lk, dict) and (lk.get("url") or lk.get("item_id")) for lk in links):
        signal_types.append("extracted_links")
    if any(str(tk or "").strip() for tk in tokens):
        signal_types.append("extracted_tokens")
    if cand_id:
        signal_types.append("candidate_goods_id")

    text_blob = f"{record.get('title') or ''}\n{record.get('tutorial_text') or ''}".lower()
    if any(kw in text_blob for kw in _SIGNAL_URL_OR_DOMAIN_KEYWORDS):
        if "url_or_domain_keyword" not in signal_types:
            signal_types.append("url_or_domain_keyword")
    if any(sym in text_blob for sym in _SIGNAL_TOKEN_SYMBOLS):
        if "token_symbol" not in signal_types:
            signal_types.append("token_symbol")
    if any(kw in text_blob for kw in _SIGNAL_SKU_KEYWORDS):
        if "sku_keyword" not in signal_types:
            signal_types.append("sku_keyword")

    return (len(signal_types) > 0), signal_types


def evaluate_quality_signals(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算单条线报的极简信号结构 (`quality_signals`)，彻底移除复杂正则提取器。
    """
    has_signal, signal_types = has_potential_product_signal(record)
    links = record.get("extracted_links") or []
    tokens = record.get("extracted_tokens") or []
    images = record.get("tutorial_images") or []
    hot_metrics = record.get("hot_metrics") or {}

    comments = 0
    try:
        comments = int(hot_metrics.get("comments") or 0)
    except (ValueError, TypeError):
        comments = 0

    return {
        "has_unique_product_anchor": has_signal,
        "unique_anchor_types": signal_types,
        "quality_tier": "A_含潜在商品信号" if has_signal else "B_无潜在商品信号",
        "ecommerce_link_count": len(links),
        "has_multi_links": len(links) >= 2,
        "image_count": len(images),
        "token_count": len(tokens),
        "comments": comments,
        "has_price": record.get("price") is not None,
    }


class DealRawPoolRepository:
    """
    好价线报初步缓冲池仓储 (`deal_raw_tips`) + 正式商品表 (`products`) 关联管理
    """

    def __init__(
        self,
        collection_name: str = DEFAULT_RAW_COLLECTION_NAME,
        formal_collection_name: str = DEFAULT_FORMAL_PRODUCTS_COLLECTION,
        db_instance=None,
    ):
        self.collection_name = collection_name
        self.formal_collection_name = formal_collection_name
        self._db = db_instance
        self._indexes_ready = False

    @property
    def db(self):
        if self._db is None:
            self._db = gen_db_object()
        return self._db

    @property
    def col(self):
        if not self._indexes_ready:
            self._ensure_indexes()
        return self.db.get_collection(self.collection_name)

    @property
    def formal_col(self):
        if not self._indexes_ready:
            self._ensure_indexes()
        return self.db.get_collection(self.formal_collection_name)

    def _ensure_indexes(self):
        try:
            col = self.db.get_collection(self.collection_name)
            col.create_index([("source_site", 1), ("source_item_id", 1)], unique=True, background=True)
            col.create_index([("first_seen_at", -1)], background=True)
            col.create_index([("last_seen_at", -1)], background=True)
            col.create_index([("clean_status", 1), ("source_site", 1)], background=True)
            col.create_index([("clean_error_code", 1)], background=True)
            col.create_index([("quality_score", -1)], background=True)
            col.create_index(
                [("source_site", 1), ("is_group_stream", 1), ("group_id", 1), ("publish_ts", 1)],
                background=True,
            )
            col.create_index(
                [("quality_signals.has_unique_product_anchor", 1), ("clean_status", 1)],
                background=True,
            )
            col.create_index([("platform", 1), ("quality_signals.quality_tier", 1)], background=True)
            col.create_index([("candidate_goods_id", 1)], background=True)

            # 确保正式 products 表核心索引就绪（与 ProductRepository._init_indexes 保持一致）
            pcol = self.db.get_collection(self.formal_collection_name)
            pcol.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
            pcol.create_index("goods_sign", background=True)
            pcol.create_index([("updated_at", -1)], background=True)
            pcol.create_index([("deal_info.is_deal_tip", 1), ("deal_info.quality_score", -1), ("updated_at", -1)], background=True)
            self._indexes_ready = True
        except Exception as e:
            logger.warning("初始化集合索引时发生异常（稍后自动重试）: %s", e)

    def reset_raw_collection(self) -> int:
        """
        清空本功能涉及的所有数据库数据：
        1. 清空初步表 `deal_raw_tips` 中的全部数据；
        2. 清空正式表 `products` 中所有由好价线报功能写入或挂载的数据 (`source='deal_pool'` 或 `deal_info.is_deal_tip=True`)；
        3. 清理早期临时沙盒集合 `deal_cleaned_products_test`。
        """
        deleted_raw = 0
        try:
            res = self.col.delete_many({})
            deleted_raw = res.deleted_count
            logger.info("🧹 已清空初步表 [%s] 中的全部数据，共删除 %d 条记录。", self.collection_name, deleted_raw)
        except Exception as e:
            logger.error("清空初步表 [%s] 失败: %s", self.collection_name, e)

        try:
            res_formal = self.formal_col.delete_many({
                "$or": [
                    {"source": "deal_pool"},
                    {"last_clean_source": "deal_pool"},
                    {"deal_info.is_deal_tip": True},
                ]
            })
            logger.info(
                "🧹 已清空正式表 [%s] 中由好价线报入库的全部商品数据，共删除 %d 条记录。",
                self.formal_collection_name,
                res_formal.deleted_count,
            )
        except Exception as e:
            logger.error("清理正式表 [%s] 线报商品数据失败: %s", self.formal_collection_name, e)

        try:
            if LEGACY_SHADOW_TEST_COLLECTION in self.db.list_collection_names():
                self.db.drop_collection(LEGACY_SHADOW_TEST_COLLECTION)
                logger.info("🧹 已清理早期临时沙盒测试集合 [%s]。", LEGACY_SHADOW_TEST_COLLECTION)
        except Exception:
            pass
        return deleted_raw

    def get_existing_detailed_ids(self, source_site: str, limit: int = 5000) -> Set[str]:
        """获取某站点近期已经成功拉取过完整详情的 `source_item_id` 集合，避免重复请求详情页。"""
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
        【Stage 1：源站拉取入初步表 `deal_raw_tips`】
        1. 强制记录 `source_list_page_url`（列表页/群聊页地址）与 `source_detail_url`（详情页地址），方便人工打开页面核对是否漏抓；
        2. 若为连续群聊流（`is_group_stream=True`，即折淘客采集群）：**100% 消息全量入库**！
           - 含潜在商品信号（链接/口令/货号）的消息标记为 `clean_status='pending'`；
           - 纯文字教程补充或纯晒单截图消息标记为 `clean_status='context_only'`，供 Stage 2 AI 检索同群前后 ±3 分钟上下文；
        3. 若为独立网页/榜单帖（`is_group_stream=False`）：仅对包含潜在商品信号的记录入库并标记为 `clean_status='pending'`。
        """
        if not records:
            return {"inserted": 0, "updated": 0, "skipped_no_signal": 0, "context_only_saved": 0, "total": 0}

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ops = []
        skipped_no_signal = 0
        context_only_saved = 0

        for rec in records:
            if not isinstance(rec, dict):
                continue
            site = str(rec.get("source_site") or "").strip()
            item_id = str(rec.get("source_item_id") or "").strip()
            if not site or not item_id:
                continue

            has_full_detail = bool(rec.get("has_full_detail", True))
            quality_signals = evaluate_quality_signals(rec)
            is_group_stream = bool(
                rec.get("is_group_stream")
                or (site == "zhetaoke" and item_id.startswith("xb_"))
            )
            hot_metrics = rec.get("hot_metrics") if isinstance(rec.get("hot_metrics"), dict) else {}
            group_id = str(hot_metrics.get("group_id") or "").strip()
            pub_time_str = str(rec.get("publish_time") or "").strip()
            pub_ts = parse_publish_time_epoch(pub_time_str)
            if pub_ts is None:
                pub_ts = time.time()

            has_signal = bool(quality_signals["has_unique_product_anchor"])
            if has_full_detail and not has_signal:
                if is_group_stream:
                    # 群聊中的纯文字说明或纯晒单截图 100% 留存为 context_only，确保相邻 ±3 分钟聚合零丢失
                    initial_status = "context_only"
                    context_only_saved += 1
                else:
                    skipped_no_signal += 1
                    continue
            else:
                initial_status = "pending"

            channel = str(rec.get("source_channel") or "").strip()
            filter_spec = {"source_site": site, "source_item_id": item_id}

            source_url = str(rec.get("source_url") or "").strip()
            source_list_page_url = str(rec.get("source_list_page_url") or source_url or "").strip()
            source_detail_url = str(rec.get("source_detail_url") or source_url or source_list_page_url or "").strip()

            set_fields: Dict[str, Any] = {
                "last_seen_at": now,
                "last_task_name": task_name,
            }
            if hot_metrics:
                set_fields["hot_metrics"] = hot_metrics

            set_on_insert: Dict[str, Any] = {
                "source_site": site,
                "source_item_id": item_id,
                "first_seen_at": now,
                "clean_status": initial_status,
                "ai_clean_status": initial_status,
            }

            core_fields = {
                "source_channel": channel,
                "source_url": source_url or source_detail_url,
                "source_list_page_url": source_list_page_url,
                "source_detail_url": source_detail_url,
                "is_group_stream": is_group_stream,
                "group_id": group_id,
                "publish_ts": pub_ts,
                "fetched_at": rec.get("fetched_at") or "",
                "platform": rec.get("platform") or "unknown",
                "candidate_goods_id": rec.get("candidate_goods_id") or "",
                "title": rec.get("title") or "",
                "price_text": rec.get("price_text") or "",
                "price": rec.get("price"),
                "image_url": rec.get("image_url") or "",
                "mall_name": rec.get("mall_name") or "",
                "publish_time": pub_time_str,
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
            return {
                "inserted": 0,
                "updated": 0,
                "skipped_no_signal": skipped_no_signal,
                "context_only_saved": context_only_saved,
                "total": 0,
            }

        try:
            self.col.delete_many({"has_full_detail": False})
            res = self.col.bulk_write(ops, ordered=False)
            return {
                "inserted": res.upserted_count,
                "updated": res.modified_count,
                "skipped_no_signal": skipped_no_signal,
                "context_only_saved": context_only_saved,
                "total": len(ops),
            }
        except Exception as e:
            logger.error("❌ 批量写入 MongoDB [%s] 失败，降级写入本地 JSONL 备份: %s", self.collection_name, e)
            self._append_fallback_jsonl(records, task_name)
            return {
                "inserted": 0,
                "updated": 0,
                "skipped_no_signal": skipped_no_signal,
                "context_only_saved": context_only_saved,
                "total": len(ops),
                "error": str(e),
            }

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
        """生成初步表 `deal_raw_tips` + 正式表 `products` 线报商品的全局统计分析报告。"""
        col = self.col
        pcol = self.formal_col
        total_docs = col.count_documents({})
        full_detail_docs = col.count_documents({"has_full_detail": True})
        unique_anchor_docs = col.count_documents({"quality_signals.has_unique_product_anchor": True})
        cleaned_success_docs = col.count_documents({"clean_status": "cleaned_success"})
        cleaned_low_score_docs = col.count_documents({"clean_status": "cleaned_low_score"})
        merged_neighbor_docs = col.count_documents({"clean_status": "merged_into_parent"})
        context_only_docs = col.count_documents({"clean_status": "context_only"})
        cleaned_failed_docs = col.count_documents({"clean_status": "cleaned_failed"})
        pending_clean_docs = col.count_documents({"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]})

        formal_total_products = pcol.count_documents({})
        formal_deal_products = pcol.count_documents({"deal_info.is_deal_tip": True})

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
                        "cleaned_success_count": {
                            "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_success"]}, 1, 0]}
                        },
                        "cleaned_failed_count": {
                            "$sum": {"$cond": [{"$eq": ["$clean_status", "cleaned_failed"]}, 1, 0]}
                        },
                        "pending_count": {
                            "$sum": {"$cond": [{"$eq": ["$clean_status", "pending"]}, 1, 0]}
                        },
                    }
                },
                {"$sort": {"cleaned_success_count": -1, "unique_anchor_count": -1, "count": -1}},
            ])
        )

        by_clean_error = list(
            col.aggregate([
                {"$match": {"clean_status": {"$in": ["cleaned_failed", "cleaned_low_score"]}}},
                {"$group": {"_id": "$clean_error_code", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ])
        )

        by_platform = list(
            col.aggregate([
                {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ])
        )

        return {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_collection": self.collection_name,
            "formal_collection": self.formal_collection_name,
            "total_records": total_docs,
            "full_detail_records": full_detail_docs,
            "unique_product_anchor_records": unique_anchor_docs,
            "unique_product_anchor_ratio": round(unique_anchor_docs * 100.0 / max(1, total_docs), 1),
            "cleaned_success_records": cleaned_success_docs,
            "cleaned_low_score_records": cleaned_low_score_docs,
            "merged_into_parent_records": merged_neighbor_docs,
            "context_only_records": context_only_docs,
            "cleaned_failed_records": cleaned_failed_docs,
            "pending_clean_records": pending_clean_docs,
            "formal_products_total": formal_total_products,
            "formal_deal_products_count": formal_deal_products,
            "by_clean_error_code": {str(r["_id"]): r["count"] for r in by_clean_error},
            "by_platform": {str(r["_id"]): r["count"] for r in by_platform},
            "by_source_site": by_site,
            "engineering_philosophy_manifest": ENGINEERING_PHILOSOPHY_MANIFEST,
            "excluded_sites_manifest": EXCLUDED_SITES_AND_CHANNELS_MANIFEST,
            "attempted_dead_ends_manifest": CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
        }


# ==============================================================================
# Stage 2 ~ Stage 4 核心：双阶段 AI + 官方 API 转链验真 + 质量评分清洗器 (`DealPoolCleaner`)
# ==============================================================================
def _to_naive_utc(dt: Any) -> Optional[datetime.datetime]:
    """统一将任意 datetime 对象转换为无时区偏移 (offset-naive) 的 UTC 时间，彻底杜绝 MongoDB tz_aware 比较报错。"""
    if not isinstance(dt, datetime.datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def parse_publish_time_epoch(pub_str: Any) -> Optional[float]:
    """将常见发布时间字符串解析为 Unix 时间戳（秒），用于同群 ±3 分钟（±180s）上下文聚合。"""
    s = str(pub_str or "").strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.datetime.strptime(s[:19], fmt)
            return dt.timestamp()
        except Exception:
            continue
    return None


def detect_candidate_platform(raw_input: str, default_plat: str = "unknown") -> str:
    """判断单条链接或口令所属平台（jd / taobao / pdd）。"""
    s = (raw_input or "").strip().lower()
    if s.startswith(("http://", "https://")):
        path_only = s.split("?", 1)[0].split("#", 1)[0]
        if path_only.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")):
            return "unsupported_other"
    if any(d in s for d in _UNSUPPORTED_EXTERNAL_DOMAINS):
        return "unsupported_other"
    if any(k in s for k in ("jd.com", "3.cn", "jingfen")):
        return "jd"
    if any(k in s for k in ("taobao.com", "tmall.com", "tb.cn", "tb3.cn", "uland")):
        return "taobao"
    if any(k in s for k in ("yangkeduo.com", "pinduoduo.com")):
        return "pdd"
    if any(sym in raw_input for sym in ("￥", "¥", "$", "€", "₴", "£", "¢")):
        return "taobao"
    if "!" in raw_input or "！" in raw_input:
        return "jd"
    if "-" in raw_input and len(raw_input) >= 35 and not raw_input.startswith("http"):
        return "taobao"
    if default_plat in ("jd", "taobao", "pdd"):
        return default_plat
    return "unknown"


def is_direct_coupon_or_venue_url(raw_input: str, role_hint: str = "") -> bool:
    """判断一个 URL 是否为直接的优惠券领取页或活动会场页（无需转出单品也应保留给用户领券）。"""
    u = (raw_input or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    coupon_domains_or_paths = (
        "coupon.m.jd.com",
        "pro.m.jd.com",
        "prodev.m.jd.com",
        "h5.m.jd.com",
        "jdcoupon",
        "uland.taobao.com/quan",
        "market.m.taobao.com",
        "pages.tmall.com",
        "pages-fast.m.taobao.com",
        "taoquan.taobao.com",
    )
    if any(k in u for k in coupon_domains_or_paths):
        return True
    if role_hint == "coupon_candidate" and any(k in u for k in ("3.cn/", "u.jd.com/", "m.tb.cn/", "s.click.taobao.com/")):
        return True
    return False


_ROLE_HINT_NORMALIZE_MAP = {
    "main_candidate": "main_candidate",
    "main_product": "main_candidate",
    "main": "main_candidate",
    "coupon_candidate": "coupon_candidate",
    "coupon": "coupon_candidate",
    "filler_candidate": "filler_candidate",
    "filler_product": "filler_candidate",
    "filler": "filler_candidate",
}


class DealPoolCleaner:
    """
    五阶段双 AI 赋能 + 官方 API 转链清洗器：
    - Stage 2: 调用 AI-1 (`爆料阶段1_原始拆解与转链提纯.txt`) 合并同群 ±3 分钟相邻消息、无损拆解并编号全部链接/口令 (`L1`, `L2`...)；
    - Stage 3: 确定性调用 `JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal()`，区分单品 (`product`)、优惠券/会场 (`coupon_or_activity`) 与死链 (`failed`)；
    - Stage 4: 调用 AI-2 (`爆料阶段2_底牌终审与教程评分.txt`) 结合官方真实商品详情终审主商品/优惠券/凑单品角色、淘汰残次死链、提取爆料到手价、生成步骤化教程并给出 0~100 分 `quality_score`。
    """

    def __init__(
        self,
        use_ai_for_multi: bool = True,
        ai_batch_size: int = 10,
        ai_max_workers: int = 20,
        convert_max_workers: int = 10,
    ):
        self.use_ai = True
        self.ai_batch_size = max(1, ai_batch_size)
        self.ai_max_workers = max(1, ai_max_workers)
        self.convert_max_workers = max(1, convert_max_workers)
        self.adapters = {
            "jd": get_adapter("jd"),
            "taobao": get_adapter("taobao"),
            "pdd": get_adapter("pdd"),
        }
        self.prompt_stage1_template = self._load_prompt_file(PROMPT_STAGE1_PATH)
        self.prompt_stage2_template = self._load_prompt_file(PROMPT_STAGE2_PATH)

        # 线程安全的内存级转链结果缓存，避免不同采集群重复刷屏的同一链接/口令重复调用上游接口
        self._cache_lock = threading.Lock()
        self._cache_warmed_up = False
        self.anchor_cache: Dict[str, Dict[str, Any]] = {}
        self.stats_cache_hits = 0
        self.stats_ai1_calls = 0
        self.stats_ai1_model_hits = 0
        self.stats_ai1_fallbacks = 0
        self.stats_ai2_calls = 0
        self.stats_ai2_model_hits = 0
        self.stats_ai2_fallbacks = 0
        self.stats_early_terminated_pre_ai1 = 0
        self.stats_early_terminated_post_stage3 = 0
        self.last_batch_stage_timings: Dict[str, float] = {}

    def warmup_anchor_cache_from_db(self, raw_col) -> None:
        """
        启动时从初步表 `deal_raw_tips` 预热最近 12 小时内已成功转链的 `converted_links` 缓存，
        确保进程重启后遇到相同推广链接/口令也能 0ms 命中缓存并触发前置提前终止。
        """
        if self._cache_warmed_up or raw_col is None:
            return
        self._cache_warmed_up = True
        try:
            cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
                hours=DEAL_INFO_PROTECTION_HOURS
            )
            cursor = raw_col.find(
                {
                    "cleaned_at": {"$gte": cutoff},
                    "converted_links.0": {"$exists": True},
                },
                {"_id": 0, "converted_links": 1},
            ).limit(3000)
            loaded = 0
            with self._cache_lock:
                for doc in cursor:
                    for cl in doc.get("converted_links") or []:
                        if not isinstance(cl, dict):
                            continue
                        c_status = cl.get("convert_status")
                        c_plat = str(cl.get("platform") or "").strip()
                        c_inp = str(cl.get("clean_input") or "").strip()
                        if c_plat in ("jd", "taobao", "pdd") and c_inp and c_status in ("product", "coupon_or_activity"):
                            ckey = f"{c_plat}:{c_inp}"
                            if ckey not in self.anchor_cache:
                                self.anchor_cache[ckey] = copy.deepcopy(cl)
                                loaded += 1
            if loaded > 0:
                logger.info("🔥 [转链缓存预热] 已从 `deal_raw_tips` 恢复最近 12h 有效转链锚点 %d 个", loaded)
        except Exception as e:
            logger.warning("预热转链缓存异常: %s", e)

    @staticmethod
    def _load_prompt_file(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.error("读取 AI 提示词文件 [%s] 失败: %s", path, e)
            return ""

    @staticmethod
    def _format_raw_message_for_ai1(msg_doc: Dict[str, Any], is_target: bool = False) -> Dict[str, Any]:
        """
        将单条 `deal_raw_tips` 文档格式化为 Stage 1 AI 标准输入的 `raw_messages` 元素。
        确保 DOM 属性提取或源站中转解包出的链接/口令（如逛丢 `go.php`、拔草哦 `go.bacaoo.com`、好单库加密ID）
        100% 出现在 `content` 文本中，不遗漏任何隐藏锚点。
        """
        title = str(msg_doc.get("title") or "").strip()
        tut = str(msg_doc.get("tutorial_text") or "").strip()
        content = tut if tut else title
        if title and title not in content:
            content = f"【标题】{title}\n{content}".strip()

        extra_anchor_lines: List[str] = []
        for lk in msg_doc.get("extracted_links") or []:
            if not isinstance(lk, dict):
                continue
            u = html_lib.unescape(str(lk.get("url") or lk.get("item_id") or "")).strip()
            lbl = str(lk.get("label") or "附带链接").strip()
            if u and u not in content:
                extra_anchor_lines.append(f"{lbl}: {u}")
        for tk in msg_doc.get("extracted_tokens") or []:
            tk_s = html_lib.unescape(str(tk or "")).strip()
            if tk_s and tk_s not in content and not any(tk_s in ln for ln in extra_anchor_lines):
                extra_anchor_lines.append(f"附带口令/加密ID: {tk_s}")

        if extra_anchor_lines:
            content = f"{content}\n" + "\n".join(extra_anchor_lines)

        imgs: List[str] = []
        primary_img = str(msg_doc.get("image_url") or "").strip()
        if primary_img:
            imgs.append(primary_img)
        for im in msg_doc.get("tutorial_images") or []:
            im_s = str(im or "").strip()
            if im_s and im_s not in imgs:
                imgs.append(im_s)

        return {
            "tip_id": str(msg_doc.get("source_item_id") or ""),
            "is_target": bool(is_target),
            "publish_time": str(msg_doc.get("publish_time") or msg_doc.get("fetched_at") or ""),
            "content": content.strip(),
            "images": imgs,
        }

    def build_stage1_task_input(self, doc: Dict[str, Any], raw_col) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        """
        构造单条待清洗记录的 Stage 1 AI 输入：
        - 若 `is_group_stream=True`（折淘客采集群）：从 `deal_raw_tips` 查询同群 (`group_id`) 前后 ±3 分钟（`±180s`）内的相邻消息（含 `context_only` 纯文字/纯图片消息）；
        - 若 `is_group_stream=False`（网页/榜单独立帖）：仅包含自身单条消息（`raw_messages` 长度为 1）。
        返回 `(stage1_input_dict, tip_id_to_doc_map)`。
        """
        site = str(doc.get("source_site") or "")
        main_tip_id = str(doc.get("source_item_id") or "")
        is_group_stream = bool(
            doc.get("is_group_stream")
            or (site == "zhetaoke" and main_tip_id.startswith("xb_"))
        )
        group_id = str(doc.get("group_id") or (doc.get("hot_metrics") or {}).get("group_id") or "").strip()
        pub_ts = doc.get("publish_ts")
        if not isinstance(pub_ts, (int, float)):
            pub_ts = parse_publish_time_epoch(doc.get("publish_time"))

        neighbor_docs: List[Dict[str, Any]] = [doc]
        if is_group_stream and group_id and raw_col is not None and isinstance(pub_ts, (int, float)):
            try:
                cursor = (
                    raw_col.find({
                        "source_site": site,
                        "group_id": group_id,
                        "publish_ts": {"$gte": float(pub_ts) - 180.0, "$lte": float(pub_ts) + 180.0},
                    })
                    .sort([("publish_ts", 1), ("source_item_id", 1)])
                    .limit(7)
                )
                fetched_neighbors = list(cursor)
                if fetched_neighbors:
                    if not any(str(d.get("source_item_id")) == main_tip_id for d in fetched_neighbors):
                        fetched_neighbors.append(doc)
                    fetched_neighbors.sort(
                        key=lambda x: (
                            float(x.get("publish_ts") or pub_ts),
                            str(x.get("source_item_id") or ""),
                        )
                    )
                    neighbor_docs = fetched_neighbors
            except Exception as e:
                logger.warning("查询同群 [%s] 相邻 ±3 分钟消息异常: %s", group_id, e)

        tip_map: Dict[str, Dict[str, Any]] = {}
        raw_messages: List[Dict[str, Any]] = []
        for nd in neighbor_docs:
            tid = str(nd.get("source_item_id") or "")
            if not tid or tid in tip_map:
                continue
            tip_map[tid] = nd
            raw_messages.append(self._format_raw_message_for_ai1(nd, is_target=(tid == main_tip_id)))

        stage1_item = {
            "main_tip_id": main_tip_id,
            "source_site": site,
            "source_channel": str(doc.get("source_channel") or ""),
            "is_group_stream": is_group_stream,
            "raw_messages": raw_messages,
        }
        return stage1_item, tip_map

    @staticmethod
    def _strip_markdown_link_wrapper(raw_val: str) -> str:
        """若字符串被包裹在 `[...](https://...)` Markdown 语法中，提取圆括号内的真实 URL。"""
        s = html_lib.unescape(str(raw_val or "")).strip()
        if s.startswith("[") and "](" in s and s.endswith(")"):
            inner = s.split("](", 1)[-1][:-1].strip()
            if inner:
                return inner
        return s

    def _fallback_stage1_extract(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        【遵循核心铁律：Fail-Closed】
        当 Stage 1 AI 未返回某条记录的拆解结果时，严禁使用脆弱的本地启发式规则猜测链接角色，
        直接返回空的 `extracted_targets=[]` 拒绝本条记录，确保绝不把未受 AI-1 语义提纯的残缺锚点送入下游。
        """
        main_tip_id = str(doc.get("source_item_id") or "")
        msg = self._format_raw_message_for_ai1(doc, is_target=True)
        return {
            "main_tip_id": main_tip_id,
            "is_standalone_deal": True,
            "merged_tip_ids": [main_tip_id],
            "combined_raw_text": msg["content"].strip(),
            "extracted_images": msg["images"],
            "extracted_targets": [],
            "_from_fallback": True,
        }

    def _call_ai_json_array(self, prompt_template: str, batch_payload: List[Dict[str, Any]], stage_label: str) -> List[Dict[str, Any]]:
        """调用项目统一 AI 接口 `generate_content`，剥离思维链 `<thought>` 标签后使用 `string_to_object` 解析 JSON 数组。"""
        if not batch_payload or not prompt_template:
            return []
        payload_json = json.dumps(batch_payload, ensure_ascii=False, indent=2)
        if stage_label == "AI-Stage1":
            full_prompt = f"{prompt_template}\n<deal_stage1_input>\n{payload_json}\n</deal_stage1_input>"
        else:
            full_prompt = (
                f"{prompt_template}\n\n"
                f"==================== 本批次待处理输入 JSON 数据 ====================\n"
                f"{payload_json}"
            )
        for model_group in ("low", "medium"):
            try:
                resp = generate_content(prompt=full_prompt, preset_model_group=model_group)
                if not resp:
                    continue
                if isinstance(resp, dict):
                    if resp.get("status") != "✅ 成功":
                        detail = "；".join(str(err) for err in (resp.get("error_history") or []))
                        raise RuntimeError(detail or str(resp.get("content") or "模型调用失败"))
                    resp_text = str(resp.get("content") or "")
                else:
                    resp_text = str(resp)
                cleaned_text = resp_text.strip()
                if not cleaned_text:
                    continue
                # 剥离思考模型输出的 <thought>...</thought> 或 <thinking>...</thinking>，防止思维链中的 [#L1] 干扰 JSON 括号定位
                for close_tag in ("</thought>", "</thinking>"):
                    if close_tag in cleaned_text:
                        cleaned_text = cleaned_text.split(close_tag)[-1].strip()
                if "```" in cleaned_text:
                    parts = cleaned_text.split("```")
                    for seg in parts[1::2]:
                        body = seg.strip()
                        if body.lower().startswith("json"):
                            body = body[4:].strip()
                        if body.startswith("[") or body.startswith("{"):
                            cleaned_text = body
                            break
                parsed = string_to_object(cleaned_text)
                if isinstance(parsed, list):
                    return [item for item in parsed if isinstance(item, dict)]
                if isinstance(parsed, dict):
                    return [parsed]
            except Exception as e:
                err_brief = str(e).splitlines()[0][:200]
                logger.warning("[%s] 调用模型组 (%s) 解析 JSON 异常: %s", stage_label, model_group, err_brief)
        return []

    def run_ai_stage1_batch(self, stage1_inputs: List[Dict[str, Any]], docs_by_tip_id: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """并发执行 Stage 2（AI-1：原始拆解与转链提纯），返回 `main_tip_id -> ai1_result` 字典。"""
        results_by_id: Dict[str, Dict[str, Any]] = {}
        chunks = [
            stage1_inputs[i : i + self.ai_batch_size]
            for i in range(0, len(stage1_inputs), self.ai_batch_size)
        ]

        def _worker(chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            self.stats_ai1_calls += 1
            return self._call_ai_json_array(self.prompt_stage1_template, chunk, "AI-Stage1")

        if chunks:
            with ThreadPoolExecutor(max_workers=min(self.ai_max_workers, len(chunks))) as pool:
                futures = {pool.submit(_worker, ch): ch for ch in chunks}
                for fut in as_completed(futures):
                    try:
                        arr = fut.result()
                        for item in arr:
                            mid = str(item.get("main_tip_id") or item.get("task_id") or "").strip()
                            if mid and ":" in mid and mid not in docs_by_tip_id:
                                mid_suffix = mid.split(":", 1)[-1].strip()
                                if mid_suffix in docs_by_tip_id:
                                    mid = mid_suffix
                            if mid:
                                item["main_tip_id"] = mid
                                results_by_id[mid] = item
                    except Exception as e:
                        logger.warning("[AI-Stage1] 并发批次执行异常: %s", str(e).splitlines()[0][:200])

        # 若 AI-1 未返回某条记录，严格执行 Fail-Closed 拒绝；若正常返回，标准化 clean_input 与角色枚举
        for inp in stage1_inputs:
            mid = str(inp.get("main_tip_id") or "")
            orig_doc = docs_by_tip_id.get(mid) or {}
            ai1_res = results_by_id.get(mid)
            if not isinstance(ai1_res, dict) or "extracted_targets" not in ai1_res:
                self.stats_ai1_fallbacks += 1
                ai1_res = self._fallback_stage1_extract(orig_doc)
                results_by_id[mid] = ai1_res
            else:
                self.stats_ai1_model_hits += 1
                ai1_res["_from_fallback"] = False
                norm_targets: List[Dict[str, Any]] = []
                for idx_t, tgt in enumerate(ai1_res.get("extracted_targets") or [], 1):
                    if not isinstance(tgt, dict):
                        continue
                    c_inp = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
                    if not c_inp:
                        continue
                    raw_role = str(tgt.get("text_role_hint") or "main_candidate").strip().lower()
                    tgt["link_id"] = str(tgt.get("link_id") or f"L{idx_t}").strip()
                    tgt["clean_input"] = c_inp
                    tgt["text_role_hint"] = _ROLE_HINT_NORMALIZE_MAP.get(raw_role, "main_candidate")
                    norm_targets.append(tgt)
                ai1_res["extracted_targets"] = norm_targets
                if not ai1_res.get("merged_tip_ids"):
                    ai1_res["merged_tip_ids"] = [mid]
                else:
                    ai1_res["merged_tip_ids"] = [
                        (str(x).split(":", 1)[-1].strip() if ":" in str(x) else str(x).strip())
                        for x in ai1_res["merged_tip_ids"]
                        if str(x or "").strip()
                    ]
                if not ai1_res.get("extracted_images"):
                    ai1_res["extracted_images"] = self._format_raw_message_for_ai1(orig_doc)["images"]

        return results_by_id

    def convert_single_target(self, target: Dict[str, Any], default_plat: str = "unknown") -> Dict[str, Any]:
        """
        【Stage 3：官方 API 转链与底牌分类（100% 确定性代码，支持多线程并发与线程安全缓存）】
        对 Stage 1 AI 拆出的单个 `target` (`link_id`, `clean_input`, `platform`, `text_role_hint`) 调用官方 Adapter 转链，
        精准分类为 `"product"`（具体单品）、`"coupon_or_activity"`（优惠券/活动会场）或 `"failed"`（死链/不支持）。
        """
        link_id = str(target.get("link_id") or "L1").strip()
        clean_input = self._strip_markdown_link_wrapper(str(target.get("clean_input") or ""))
        role_hint = str(target.get("text_role_hint") or "main_candidate").strip()
        hint_plat = str(target.get("platform") or default_plat or "unknown").strip().lower()
        plat = detect_candidate_platform(clean_input, hint_plat if hint_plat in ("jd", "taobao", "pdd") else default_plat)

        if not clean_input:
            return {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "platform": plat,
                "error_code": ERR_NO_ANCHOR,
                "error_msg": "空链接或口令",
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }

        if plat == "unsupported_other":
            return {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "platform": plat,
                "error_code": ERR_UNSUPPORTED_PLATFORM,
                "error_msg": f"非淘京拼支持平台外链: {clean_input[:80]}",
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": {},
            }

        platforms_to_try = [plat] if plat in ("jd", "taobao", "pdd") else ["taobao", "jd", "pdd"]
        last_failed: Optional[Dict[str, Any]] = None

        for try_plat in platforms_to_try:
            cache_key = f"{try_plat}:{clean_input}"
            with self._cache_lock:
                if cache_key in self.anchor_cache:
                    cached = copy.deepcopy(self.anchor_cache[cache_key])
                    cached["link_id"] = link_id
                    cached["text_role_hint"] = role_hint
                    c_status = cached.get("convert_status")
                    if c_status == "coupon_or_activity":
                        self.stats_cache_hits += 1
                        return cached
                    if c_status == "product":
                        # 仅当缓存中包含完整的 adapter_res.product 字典时才直接复用；
                        # 若是从数据库预热的精简缓存（无 adapter_res）且未能触发前置早停（说明需要作为新商品入库），则重新请求官方接口补全完整字段
                        c_ares = cached.get("adapter_res")
                        if isinstance(c_ares, dict) and isinstance(c_ares.get("product"), dict) and c_ares.get("product"):
                            self.stats_cache_hits += 1
                            return cached
                    else:
                        self.stats_cache_hits += 1
                        last_failed = cached
                        continue

            adapter = self.adapters.get(try_plat)
            if not adapter:
                continue

            try:
                res = adapter.resolve_and_convert_universal(clean_input)
            except Exception as exc:
                out = {
                    "link_id": link_id,
                    "clean_input": clean_input,
                    "text_role_hint": role_hint,
                    "convert_status": "failed",
                    "platform": try_plat,
                    "error_code": ERR_ADAPTER_CONVERT_FAILED,
                    "error_msg": f"Adapter({try_plat})调用异常: {exc}",
                    "official_product": None,
                    "my_promo_link": None,
                    "my_promo_token": None,
                    "adapter_res": {},
                }
                with self._cache_lock:
                    self.anchor_cache[cache_key] = out
                last_failed = out
                continue

            if isinstance(res, dict) and res.get("status") == "success":
                goods_id = str(res.get("goods_id") or "").strip()
                goods_sign = str(res.get("goods_sign") or "").strip()
                product_card = res.get("product")
                promo_link = str(res.get("final_link") or res.get("url") or "").strip()
                promo_token = str(res.get("promotion_token") or "").strip()

                if goods_id and goods_sign and isinstance(product_card, dict):
                    official_price = float(product_card.get("price") or res.get("price") or 0.0)
                    orig_price = float(product_card.get("original_price") or official_price)
                    act_price = float(product_card.get("activity_price") or official_price)
                    cpn_amt = float(product_card.get("coupon_amount") or res.get("coupon_amount") or 0.0)
                    out = {
                        "link_id": link_id,
                        "clean_input": clean_input,
                        "text_role_hint": role_hint,
                        "convert_status": "product",
                        "platform": try_plat,
                        "goods_id": goods_id,
                        "goods_sign": goods_sign,
                        "error_code": "OK",
                        "error_msg": "",
                        "official_product": {
                            "title": str(product_card.get("name") or res.get("title") or "").strip(),
                            "price": round(official_price, 2),
                            "original_price": round(orig_price, 2),
                            "activity_price": round(act_price, 2),
                            "coupon_amount": round(cpn_amt, 2),
                            "shop_name": str(product_card.get("mall_name") or "").strip(),
                        },
                        "my_promo_link": promo_link or None,
                        "my_promo_token": promo_token or None,
                        "adapter_res": res,
                    }
                    with self._cache_lock:
                        self.anchor_cache[cache_key] = out
                    return copy.deepcopy(out)

                # 转链成功但无单一商品详情（例如京东/淘宝优惠券短链、活动会场推广链）
                if promo_link or promo_token:
                    out = {
                        "link_id": link_id,
                        "clean_input": clean_input,
                        "text_role_hint": role_hint,
                        "convert_status": "coupon_or_activity",
                        "platform": try_plat,
                        "goods_id": "",
                        "goods_sign": "",
                        "error_code": "OK_COUPON",
                        "error_msg": "",
                        "official_product": None,
                        "my_promo_link": promo_link or (clean_input if clean_input.startswith("http") else None),
                        "my_promo_token": promo_token or None,
                        "adapter_res": res,
                    }
                    with self._cache_lock:
                        self.anchor_cache[cache_key] = out
                    return copy.deepcopy(out)

            # 若上游未返回单品转链结果，但该链接本身就是可直接打开的官方领券/活动会场链接（如 coupon.m.jd.com），保留为可用优惠券链接
            if is_direct_coupon_or_venue_url(clean_input, role_hint):
                out = {
                    "link_id": link_id,
                    "clean_input": clean_input,
                    "text_role_hint": role_hint,
                    "convert_status": "coupon_or_activity",
                    "platform": try_plat,
                    "goods_id": "",
                    "goods_sign": "",
                    "error_code": "OK_DIRECT_COUPON",
                    "error_msg": "",
                    "official_product": None,
                    "my_promo_link": clean_input,
                    "my_promo_token": None,
                    "adapter_res": res if isinstance(res, dict) else {},
                }
                with self._cache_lock:
                    self.anchor_cache[cache_key] = out
                return copy.deepcopy(out)

            raw_code = str((res or {}).get("error_code") or "")
            raw_msg = str((res or {}).get("error_msg") or "转链未返回有效结果")
            if raw_code == "UNSUPPORTED_TB_NUMERIC_ID" or (try_plat == "taobao" and "不支持数字id" in raw_msg):
                err_code = ERR_TB_NUMERIC_ID
            elif raw_code == "UNSUPPORTED_PDD_ID_ONLY_URL" or (try_plat == "pdd" and "40009" in raw_msg):
                err_code = ERR_PDD_NUMERIC_ID
            elif raw_code == "UNSUPPORTED_JD_NUMERIC_SKU" or (try_plat == "jd" and "不支持数字id" in raw_msg):
                err_code = ERR_JD_NUMERIC_SKU
            elif raw_code == "PRODUCT_OFF_PROMOTION" or any(
                k in raw_msg for k in ("下架", "不在推广", "取消推广", "不支持该商品")
            ):
                err_code = ERR_ADAPTER_OFF_PROMOTION
            else:
                err_code = ERR_ADAPTER_CONVERT_FAILED

            out = {
                "link_id": link_id,
                "clean_input": clean_input,
                "text_role_hint": role_hint,
                "convert_status": "failed",
                "platform": try_plat,
                "goods_id": "",
                "goods_sign": "",
                "error_code": err_code,
                "error_msg": raw_msg,
                "official_product": None,
                "my_promo_link": None,
                "my_promo_token": None,
                "adapter_res": res if isinstance(res, dict) else {},
            }
            with self._cache_lock:
                self.anchor_cache[cache_key] = out
            last_failed = out

        return copy.deepcopy(last_failed) if last_failed else {
            "link_id": link_id,
            "clean_input": clean_input,
            "text_role_hint": role_hint,
            "convert_status": "failed",
            "platform": plat,
            "goods_id": "",
            "goods_sign": "",
            "error_code": ERR_ADAPTER_CONVERT_FAILED,
            "error_msg": "未能识别平台或转链失败",
            "official_product": None,
            "my_promo_link": None,
            "my_promo_token": None,
            "adapter_res": {},
        }

    def run_stage3_concurrent_conversions(self, unique_targets: List[Tuple[Dict[str, Any], str]]) -> None:
        """
        并发执行 Stage 3 官方 API 转链：
        先对本批次所有去重后的 `(target, default_plat)` 通过多线程池并发调用 `convert_single_target`，
        将转链结果写入线程安全的 `self.anchor_cache`，大幅缩短网络 IO 等待时间。
        """
        if not unique_targets:
            return

        def _conv_worker(item: Tuple[Dict[str, Any], str]) -> None:
            tgt, def_plat = item
            try:
                self.convert_single_target(tgt, default_plat=def_plat)
            except Exception as e:
                logger.warning("[Stage3-并发转链] 单目标转链异常: %s", str(e)[:160])

        with ThreadPoolExecutor(max_workers=min(self.convert_max_workers, len(unique_targets))) as pool:
            futures = [pool.submit(_conv_worker, it) for it in unique_targets]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    pass

    def run_ai_stage2_batch(self, stage2_inputs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """并发执行 Stage 4（AI-2：底牌终审、教程重构与质量评分），返回 `main_tip_id -> ai2_result` 字典。"""
        results_by_id: Dict[str, Dict[str, Any]] = {}
        if not stage2_inputs:
            return results_by_id

        chunks = [
            stage2_inputs[i : i + self.ai_batch_size]
            for i in range(0, len(stage2_inputs), self.ai_batch_size)
        ]

        def _worker(chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            self.stats_ai2_calls += 1
            return self._call_ai_json_array(self.prompt_stage2_template, chunk, "AI-Stage2")

        with ThreadPoolExecutor(max_workers=min(self.ai_max_workers, len(chunks))) as pool:
            futures = {pool.submit(_worker, ch): ch for ch in chunks}
            for fut in as_completed(futures):
                try:
                    arr = fut.result()
                    for item in arr:
                        mid = str(item.get("main_tip_id") or item.get("task_id") or "").strip()
                        if mid and ":" in mid:
                            mid = mid.split(":", 1)[-1].strip()
                        if mid:
                            item["main_tip_id"] = mid
                            results_by_id[mid] = item
                except Exception as e:
                    logger.warning("[AI-Stage2] 并发批次执行异常: %s", str(e).splitlines()[0][:200])

        return results_by_id

    @staticmethod
    def _fallback_stage2_evaluate(s2_input: Dict[str, Any]) -> Dict[str, Any]:
        """
        【遵循核心铁律：Fail-Closed】
        当 Stage 2 AI 未返回某条记录的终审结果时，严禁用本地规则伪造 72 分及格分蒙混入正式库 `products`，
        直接标记为 `is_valid_deal=False` 拦截，确保进入正式库的每一件商品 100% 经过 AI-2 铁面验真。
        """
        mid = str(s2_input.get("main_tip_id") or "")
        return {
            "main_tip_id": mid,
            "is_valid_deal": False,
            "reject_reason": "AI2_AUDIT_UNAVAILABLE",
            "main_link_id": None,
            "quality_score": 0,
            "_from_fallback": True,
        }

    @staticmethod
    def _inject_converted_links_into_text(text: str, link_map: Dict[str, Dict[str, Any]]) -> str:
        """将教程文本中的 `[#L1]`、`[#L2]` 占位符精确替换为我方转链后的专属推广链接/口令，并清除任何未映射的残留占位符。"""
        out = str(text or "")
        for lid, cl in sorted(link_map.items(), key=lambda kv: len(kv[0]), reverse=True):
            placeholder = f"[#{lid}]"
            if placeholder not in out:
                continue
            p_url = str(cl.get("my_promo_link") or "").strip()
            p_tok = str(cl.get("my_promo_token") or "").strip()
            if p_url and p_tok and p_url != p_tok:
                rep = f"{p_url} (口令: {p_tok})"
            elif p_url:
                rep = p_url
            elif p_tok:
                rep = p_tok
            else:
                rep = ""
            out = out.replace(placeholder, rep)
        # 清理任何未被映射或失效的残留 [#L...] 占位符，确保展示给用户的教程 100% 干净
        out = re.sub(r"\s*\[#L\d+\]", "", out)
        return out.strip()

    @classmethod
    def _build_clean_original_copywriting(
        cls,
        combined_raw_text: str,
        ai1_targets: List[Dict[str, Any]],
        link_map: Dict[str, Dict[str, Any]],
        doc: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        生成保留原博主“活人感话术”（Emoji、情绪词、神价卖点、原始排版）的纯净文案 `original_copywriting`：
        1. 剥离我方爬虫内部注入的头部/尾部标签（如 `【来源采集群:...】`、`【标题】`、`[教程图片...]`、`-->`、`附带链接:`）；
        2. 彻底剔除原文中残留的第三方原始链接与口令（含前后缀如 `1(...) CZ0001`、`-/ Cb79`），仅在对应位置保留 `[#L1]` 锚点；
        3. 将 `[#L1]`、`[#L2]` 原位替换为我方转链后的专属推广链接/口令（失败链接自动抹除，绝不泄露第三方链接）；
        4. 自动去重因标题拼接或群消息合并产生的相邻重复行。
        """
        out = str(combined_raw_text or "").strip()
        if not out and doc:
            out = str(doc.get("content") or doc.get("title") or "").strip()
        if not out:
            return ""

        # 1. 剥离爬虫内部附加标签
        out = re.sub(r"【(?:来源采集群:[^】]*|折淘客[^】]*)】\s*", "", out)
        out = re.sub(r"\[教程图片(?:\s*[:：][^\]]*)?\]\s*", "", out)
        out = re.sub(r"(?m)^\s*-->\s*$", "", out)
        out = re.sub(r"(?m)^\s*(?:附带链接|附带口令/加密ID|加密ID)\s*[:：].*$", "", out)
        out = re.sub(r"(?m)^\s*二合一落地页\s*[:：]\s*", "下单链接: ", out)

        # 2. 处理 AI-1 拆出的待转链目标：若原文同时存在原始串与 [#L...] 则删去原始串，若仅有原始串则替换为 [#L...]
        for t in ai1_targets or []:
            if not isinstance(t, dict):
                continue
            lid = str(t.get("link_id") or "").strip()
            c_in = cls._strip_markdown_link_wrapper(str(t.get("clean_input") or "").strip())
            if not c_in or len(c_in) < 4:
                continue
            placeholder = f"[#{lid}]" if lid else ""
            if placeholder and placeholder in out and c_in in out:
                out = out.replace(c_in, "")
            elif placeholder and placeholder not in out and c_in in out:
                out = out.replace(c_in, placeholder)

        # 兜底清理 doc 中预提取的原始第三方链接/口令
        if doc:
            for raw_tok in (doc.get("extracted_tokens") or []) + (doc.get("extracted_links") or []):
                rt = str(raw_tok or "").strip()
                if rt and len(rt) >= 6 and rt in out:
                    if "[#L1]" not in out and "L1" in link_map:
                        out = out.replace(rt, "[#L1]", 1)
                    out = out.replace(rt, "")

        # 清理 [#L...] 前后粘连的淘口令/京口令壳子（如 `7[#L1]/ CZ0001`、`1([#L1]) CZ4176`、`-/ Cb79`）及未替换的孤立口令残骸
        out = re.sub(
            r"(?i)(?:(?<=\s)|^)\d?[\(（\$￥!！/\-]*\s*(\[#L\d+\])\s*[\)）\$￥!！/\-]*(?:\s*(?:CZ|MF|HU|CA|ZH|MU|AC|CB)\s*\d+)?/*(?=\s|$)",
            r"\1",
            out,
        )
        out = re.sub(
            r"(?i)(?:(?<=\s)|^)\d?\([A-Za-z0-9]{8,16}\)\s*(?:/\s*)?(?:(?:CZ|MF|HU|CA|ZH|MU|AC|CB)\s*\d+)?/*(?=\s|$)",
            "",
            out,
        )
        out = re.sub(
            r"(?i)(?:(?<=\s)|^)[-/\\$￥!！\s]*(?:CZ|MF|HU|CA|ZH|MU|AC|CB)\s*\d{2,6}/*(?=\s|$)",
            "",
            out,
        )

        # 3. 注入我方转链后的专属推广链接/口令
        out = cls._inject_converted_links_into_text(out, link_map)

        # 防护：剔除任何未在我方推广链集合中的第三方电商链接残余（如已失效的第三方券链接）
        my_urls = {
            str(v.get("my_promo_link") or "").strip()
            for v in (link_map or {}).values()
            if str(v.get("my_promo_link") or "").strip()
        }

        def _strip_foreign_url(m: re.Match) -> str:
            u = m.group(0)
            return u if u in my_urls else ""

        out = re.sub(
            r"https?://(?:u\.jd\.com|3\.cn|coupon\.m\.jd\.com|item\.jd\.com|jingfen\.jd\.com|union-click\.jd\.com|m\.tb\.cn|s\.click\.taobao\.com|uland\.taobao\.com|item\.taobao\.com|detail\.tmall\.com|yangkeduo\.com|p\.pinduoduo\.com|t\.cn)/[^\s\u4e00-\u9fa5\"'<>）)]+",
            _strip_foreign_url,
            out,
        )

        # 4. 逐行清理 `【标题】` 前缀、死链剥离后残留的空引导词行（如孤立的 `"领"`），并消除相邻重复行
        cleaned_lines: List[str] = []
        prev_norm = ""
        for raw_ln in out.splitlines():
            ln = re.sub(r"^\s*【标题】\s*", "", raw_ln).strip()
            ln = re.sub(r"\s{2,}", " ", ln).strip()
            if not ln:
                continue
            if re.fullmatch(r"(?:领|领券|先领|再领|凑单|凑|拍)\s*[:：]?", ln):
                continue
            norm = re.sub(r"[\s,，.。!！~～:：;；\-—_（）()\[\]【】]", "", ln)
            if norm and norm == prev_norm:
                continue
            cleaned_lines.append(ln)
            if norm:
                prev_norm = norm

        return "\n".join(cleaned_lines).strip()

    @staticmethod
    def _load_active_formal_products(formal_col) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """
        从正式表 `products` 查询 12 小时保鲜期内已清洗入库的所有好价商品唯一标识 `(platform, product_id)`，
        用于双层提前终止（Early Termination）：凡是唯一标识已在正式库清洗过的商品，无需重复调用 AI-2 终审！
        """
        active_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if formal_col is None:
            return active_map
        try:
            cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
                hours=DEAL_INFO_PROTECTION_HOURS
            )
            cursor = formal_col.find(
                {
                    "deal_info.is_deal_tip": True,
                    "updated_at": {"$gte": cutoff},
                },
                {
                    "_id": 0,
                    "platform": 1,
                    "product_id": 1,
                    "goods_sign": 1,
                    "name": 1,
                    "price": 1,
                    "deal_info.quality_score": 1,
                    "deal_info.deal_price": 1,
                },
            )
            for d in cursor:
                plat = str(d.get("platform") or "").strip()
                pid = str(d.get("product_id") or "").strip()
                if plat and pid:
                    di = d.get("deal_info") if isinstance(d.get("deal_info"), dict) else {}
                    active_map[(plat, pid)] = {
                        "platform": plat,
                        "product_id": pid,
                        "goods_sign": str(d.get("goods_sign") or ""),
                        "official_title": str(d.get("name") or ""),
                        "official_price": d.get("price"),
                        "deal_price": di.get("deal_price"),
                        "quality_score": int(di.get("quality_score") or 75),
                    }
        except Exception as e:
            logger.warning("查询正式表已清洗活跃商品集合异常: %s", e)
        return active_map

    def clean_batch_records(
        self,
        selected_docs: List[Dict[str, Any]],
        raw_col,
        formal_col=None,
    ) -> List[Dict[str, Any]]:
        """
        对一批 `pending` 记录完整执行：
        - Tier 1 前置缓存提前终止 -> Stage 2 (AI-1 批量并发拆解) -> Stage 3 (官方 API 多线程并发转链) ->
        - Tier 2 唯一商品标识 (`platform`, `goods_id`) 提前终止 -> Stage 4 (AI-2 批量并发终审与评分)。
        """
        if not selected_docs:
            return []

        t_batch_start = time.perf_counter()
        self.warmup_anchor_cache_from_db(raw_col)
        active_formal_products = self._load_active_formal_products(formal_col)

        docs_by_tip_id: Dict[str, Dict[str, Any]] = {}
        neighbor_maps_by_id: Dict[str, Dict[str, Dict[str, Any]]] = {}
        stage1_inputs: List[Dict[str, Any]] = []
        pre_stage2_outcomes: Dict[str, Dict[str, Any]] = {}
        pre_ai1_early_count = 0

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if not mid:
                continue
            docs_by_tip_id[mid] = doc
            site = str(doc.get("source_site") or "")
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()

            # 【Tier 1 前置提前终止（Before AI-1）】：
            # 若该记录僅含单一明确链接/口令，且该链接/口令已命中 `anchor_cache` 的 `"product"`，
            # 同时其唯一商品标识 `(platform, goods_id)` 已在正式库 `products` 12h 保鲜期内清洗入库，
            # 则直接提前终止（跳过 AI-1、Stage 3 与 AI-2），仅累加跨站/跨群共振热度！
            raw_anchors: List[str] = []
            for lk in doc.get("extracted_links") or []:
                if isinstance(lk, dict):
                    u = self._strip_markdown_link_wrapper(str(lk.get("url") or lk.get("item_id") or ""))
                    if u and u not in raw_anchors:
                        raw_anchors.append(u)
            for tk in doc.get("extracted_tokens") or []:
                tk_s = self._strip_markdown_link_wrapper(str(tk or ""))
                if tk_s and tk_s not in raw_anchors:
                    raw_anchors.append(tk_s)

            matched_cached_conv: Optional[Dict[str, Any]] = None
            if len(raw_anchors) == 1:
                only_anchor = raw_anchors[0]
                def_plat = str(doc.get("platform") or "unknown").strip().lower()
                guess_plat = detect_candidate_platform(only_anchor, def_plat)
                plats_to_check = [guess_plat] if guess_plat in ("jd", "taobao", "pdd") else ["taobao", "jd", "pdd"]
                with self._cache_lock:
                    for cp in plats_to_check:
                        c_entry = self.anchor_cache.get(f"{cp}:{only_anchor}")
                        if c_entry and c_entry.get("convert_status") == "product":
                            c_pid = str(c_entry.get("goods_id") or "")
                            if (cp, c_pid) in active_formal_products:
                                matched_cached_conv = copy.deepcopy(c_entry)
                                self.stats_cache_hits += 1
                                break

            if matched_cached_conv is not None:
                c_plat = str(matched_cached_conv.get("platform") or "")
                c_gid = str(matched_cached_conv.get("goods_id") or "")
                c_gsign = str(matched_cached_conv.get("goods_sign") or "")
                ex_info = active_formal_products[(c_plat, c_gid)]
                self.stats_early_terminated_pre_ai1 += 1
                pre_ai1_early_count += 1
                pre_stage2_outcomes[mid] = {
                    "status": "early_terminated",
                    "early_stage": "pre_ai1",
                    "error_code": OK_SKIPPED_ALREADY_CLEANED,
                    "error_msg": f"前置锚点缓存命中已入库唯一商品({c_plat}:{c_gid})，提前终止跳过AI-1/转链/AI-2并累加共振",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": c_plat,
                    "product_id": c_gid,
                    "goods_sign": c_gsign,
                    "official_title": ex_info.get("official_title") or raw_title,
                    "official_price": ex_info.get("official_price"),
                    "deal_price": ex_info.get("deal_price"),
                    "title": raw_title,
                    "quality_score": ex_info.get("quality_score", 75),
                    "merged_tip_ids": [mid],
                    "ai_stage1_result": {"early_terminated": "pre_ai1_cache_hit"},
                    "converted_links": [{k: v for k, v in matched_cached_conv.items() if k != "adapter_res"}],
                    "ai_stage2_result": {"early_terminated": "pre_ai1_cache_hit"},
                    "candidate_count": 1,
                    "attempt_errors": [],
                    "resonance_update": {
                        "platform": c_plat,
                        "product_id": c_gid,
                        "seen_count": max(1, int(doc.get("seen_count") or 1)),
                        "source_site": site,
                        "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                    },
                }
                continue

            s1_inp, tip_map = self.build_stage1_task_input(doc, raw_col)
            neighbor_maps_by_id[mid] = tip_map
            stage1_inputs.append(s1_inp)

        if pre_ai1_early_count > 0:
            logger.info(
                "⚡ [Stage 2 前置缓存早停] 命中已清洗入库商品缓存提前终止=%d条 (直接跳过 AI-1 / 转链 / AI-2)",
                pre_ai1_early_count,
            )

        # ---------------- Stage 2: 调用 AI-1 合并群上下文并无损拆解全部链接/口令 ----------------
        t_ai1_start = time.perf_counter()
        ai1_hits_before = self.stats_ai1_model_hits
        ai1_fb_before = self.stats_ai1_fallbacks
        logger.info(
            "🤖 [Stage 2 / AI-1] 正在并发拆解 %d 条线报 (batch_size=%d, workers=%d)...",
            len(stage1_inputs),
            self.ai_batch_size,
            self.ai_max_workers,
        )
        ai1_results = self.run_ai_stage1_batch(stage1_inputs, docs_by_tip_id) if stage1_inputs else {}
        t_ai1_elapsed = round(time.perf_counter() - t_ai1_start, 2)
        logger.info(
            "🤖 [Stage 2 / AI-1 完成] 耗时=%.2fs | 本批模型直出=%d条, 保底兜底=%d条",
            t_ai1_elapsed,
            self.stats_ai1_model_hits - ai1_hits_before,
            self.stats_ai1_fallbacks - ai1_fb_before,
        )

        # ---------------- Stage 3: 官方 Adapter 多线程并发转链核验（区分单品、优惠券与死链） ----------------
        converted_by_tip_id: Dict[str, List[Dict[str, Any]]] = {}
        stage2_inputs: List[Dict[str, Any]] = []

        # 若同批次中某条消息已被相邻主线报吸收进 merged_tip_ids，直接标记为 merged_into_parent 避免重复转链。
        # 注意：按提取目标数与合并碎片数从多到少排序，且已被吸收的子碎片或已作为父节点吸收他人的主线报不可被反向吸收（防止 A 吸收 B、B 又吸收 A 导致双方同归于尽）
        absorbed_by_neighbor: Dict[str, str] = {}
        active_parent_ids: Set[str] = set()
        sorted_ai1_items = sorted(
            ai1_results.items(),
            key=lambda kv: (
                len((kv[1] or {}).get("extracted_targets") or []),
                len((kv[1] or {}).get("merged_tip_ids") or []),
            ),
            reverse=True,
        )
        for mid, ai1_res in sorted_ai1_items:
            if not ai1_res.get("is_standalone_deal", True):
                continue
            if mid in absorbed_by_neighbor:
                continue
            for m_tid in ai1_res.get("merged_tip_ids") or []:
                m_tid_str = str(m_tid or "").strip()
                if (
                    m_tid_str
                    and m_tid_str != mid
                    and m_tid_str not in absorbed_by_neighbor
                    and m_tid_str not in active_parent_ids
                ):
                    absorbed_by_neighbor[m_tid_str] = mid
                    active_parent_ids.add(mid)

        # 收集本批次所有需要转链的去重目标 (plat_hint, clean_input)，并发执行官方 Adapter 转链
        t_stage3_start = time.perf_counter()
        total_target_mentions = 0
        unique_target_map: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]] = {}
        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if mid in pre_stage2_outcomes or mid in absorbed_by_neighbor:
                continue
            ai1_res = ai1_results.get(mid) or {}
            if ai1_res.get("is_standalone_deal") is False:
                continue
            def_plat = str(doc.get("platform") or "unknown")
            for tgt in (ai1_res.get("extracted_targets") or [])[:8]:
                if not isinstance(tgt, dict):
                    continue
                c_inp = self._strip_markdown_link_wrapper(str(tgt.get("clean_input") or ""))
                if not c_inp:
                    continue
                total_target_mentions += 1
                hint_p = str(tgt.get("platform") or def_plat or "unknown").strip().lower()
                det_p = detect_candidate_platform(c_inp, hint_p if hint_p in ("jd", "taobao", "pdd") else def_plat)
                ukey = (det_p, c_inp)
                if ukey not in unique_target_map:
                    unique_target_map[ukey] = (tgt, def_plat)

        logger.info(
            "🔗 [Stage 3 / 并发官方转链] 正在并发核验 %d 个独立链接/口令 (总引用=%d次, workers=%d)...",
            len(unique_target_map),
            total_target_mentions,
            self.convert_max_workers,
        )
        self.run_stage3_concurrent_conversions(list(unique_target_map.values()))
        t_stage3_elapsed = round(time.perf_counter() - t_stage3_start, 2)
        logger.info(
            "🔗 [Stage 3 / 并发官方转链完成] 耗时=%.2fs | 累计缓存命中=%d次",
            t_stage3_elapsed,
            self.stats_cache_hits,
        )

        # 第一遍扫描 Stage 3 结果：处理合并碎片、无锚点、死链、纯凑单存活，以及【已在正式库 `products` 的唯一商品提前终止】
        post_stage3_candidates: List[Tuple[str, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], Optional[Tuple[str, str]], float]] = []
        post_stage3_db_early_count = 0

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if mid in pre_stage2_outcomes:
                continue
            site = str(doc.get("source_site") or "")
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()
            ai1_res = ai1_results.get(mid) or {}

            if mid in absorbed_by_neighbor or ai1_res.get("is_standalone_deal") is False:
                parent_id = absorbed_by_neighbor.get(mid) or ""
                pre_stage2_outcomes[mid] = {
                    "status": "merged_into_parent",
                    "error_code": ERR_MERGED_INTO_NEIGHBOR,
                    "error_msg": f"群聊连续碎片已合并入相邻主线报 {parent_id}".strip(),
                    "merged_into_tip_id": parent_id,
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [],
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": 0,
                    "attempt_errors": [],
                }
                continue

            targets = ai1_res.get("extracted_targets") or []
            if not targets:
                pre_stage2_outcomes[mid] = {
                    "status": "failed",
                    "error_code": ERR_NO_ANCHOR,
                    "error_msg": "Stage 1 AI 未在爆料文本中识别出任何淘/京/拼链接或口令",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [],
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": 0,
                    "attempt_errors": [],
                }
                continue

            conv_list: List[Dict[str, Any]] = []
            attempt_errors: List[Dict[str, str]] = []
            for tgt in targets[:8]:
                if not isinstance(tgt, dict):
                    continue
                c_res = self.convert_single_target(tgt, default_plat=str(doc.get("platform") or "unknown"))
                conv_list.append(c_res)
                if c_res["convert_status"] == "failed":
                    attempt_errors.append({
                        "link_id": c_res["link_id"],
                        "anchor": str(c_res.get("clean_input") or "")[:100],
                        "error_code": c_res["error_code"],
                        "error_msg": c_res["error_msg"],
                    })

            converted_by_tip_id[mid] = conv_list
            prod_convs = [c for c in conv_list if c["convert_status"] == "product"]
            coupon_convs = [c for c in conv_list if c["convert_status"] == "coupon_or_activity"]

            if not prod_convs:
                if coupon_convs and not attempt_errors:
                    primary_err_code = ERR_EMPTY_PRODUCT_IDENTITY
                    primary_err_msg = "仅包含优惠券或活动会场链接，无具体主商品"
                else:
                    primary_err_code = attempt_errors[0]["error_code"] if attempt_errors else ERR_ADAPTER_CONVERT_FAILED
                    primary_err_msg = attempt_errors[0]["error_msg"] if attempt_errors else "所有候选链接均转链失败"
                pre_stage2_outcomes[mid] = {
                    "status": "failed",
                    "error_code": primary_err_code,
                    "error_msg": primary_err_msg,
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [
                        {k: v for k, v in c.items() if k != "adapter_res"}
                        for c in conv_list
                    ],
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": len(targets),
                    "attempt_errors": attempt_errors,
                }
                continue

            # 若存活的单品链接 100% 是凑单品 (filler_candidate) 而主商品死链，直接按铁律提前拦截，无需浪费 AI-2
            main_prod_convs = [c for c in prod_convs if c.get("text_role_hint") != "filler_candidate"]
            if not main_prod_convs:
                pre_stage2_outcomes[mid] = {
                    "status": "failed",
                    "error_code": ERR_AI2_MAIN_DEAD_ONLY_FILLER,
                    "error_msg": "主商品链接转链失败，仅凑单品链接存活，提前终止拦截",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [
                        {k: v for k, v in c.items() if k != "adapter_res"}
                        for c in conv_list
                    ],
                    "ai_stage2_result": None,
                    "quality_score": 0,
                    "candidate_count": len(targets),
                    "attempt_errors": attempt_errors,
                }
                continue

            # 识别主候选商品的唯一标识 (platform, goods_id)
            unique_main_pids = {
                (str(c.get("platform") or ""), str(c.get("goods_id") or ""))
                for c in main_prod_convs
                if c.get("platform") and c.get("goods_id")
            }
            single_main_pid: Optional[Tuple[str, str]] = (
                next(iter(unique_main_pids)) if len(unique_main_pids) == 1 else None
            )

            # 【Tier 2 提前终止（Post-Stage 3 -> 正式库 `products` 已存在该唯一商品）】：
            # 当转链已经拿到主商品的唯一标识 `(platform, goods_id)` 且该商品在 12h 保鲜期内已在 `products` 中清洗过，
            # 立即提前跳过 Stage 4 (AI-2)，直接累加跨站/跨群共振热度！
            if single_main_pid is not None and single_main_pid in active_formal_products:
                c_plat, c_gid = single_main_pid
                primary_conv = main_prod_convs[0]
                c_gsign = str(primary_conv.get("goods_sign") or "")
                off_p = primary_conv.get("official_product") or {}
                ex_info = active_formal_products[single_main_pid]
                merged_tids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
                if mid not in merged_tids:
                    merged_tids.insert(0, mid)
                self.stats_early_terminated_post_stage3 += 1
                post_stage3_db_early_count += 1
                pre_stage2_outcomes[mid] = {
                    "status": "early_terminated",
                    "early_stage": "post_stage3_db",
                    "error_code": OK_SKIPPED_ALREADY_CLEANED,
                    "error_msg": f"转链唯一商品({c_plat}:{c_gid})已在正式库清洗完毕(12h内)，提前终止跳过AI-2并累加共振",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": c_plat,
                    "product_id": c_gid,
                    "goods_sign": c_gsign,
                    "official_title": off_p.get("title") or ex_info.get("official_title") or raw_title,
                    "official_price": off_p.get("price") or ex_info.get("official_price"),
                    "deal_price": ex_info.get("deal_price"),
                    "title": raw_title,
                    "quality_score": ex_info.get("quality_score", 75),
                    "merged_tip_ids": merged_tids,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [
                        {k: v for k, v in c.items() if k != "adapter_res"}
                        for c in conv_list
                    ],
                    "ai_stage2_result": {"early_terminated": "post_stage3_existing_product"},
                    "candidate_count": len(targets),
                    "attempt_errors": attempt_errors,
                    "resonance_update": {
                        "platform": c_plat,
                        "product_id": c_gid,
                        "seen_count": max(1, int(doc.get("seen_count") or 1)),
                        "source_site": site,
                        "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                    },
                }
                continue

            richness_score = (
                len(conv_list) * 10.0
                + len(ai1_res.get("extracted_images") or []) * 5.0
                + min(len(str(ai1_res.get("combined_raw_text") or "")), 500) / 50.0
            )
            post_stage3_candidates.append((mid, doc, ai1_res, conv_list, single_main_pid, richness_score))

        # 第二遍扫描 Stage 3 结果：【同批次内相同唯一商品 `(platform, goods_id)` 去重提前终止】
        # 若同批次有多条线报转链出同一个新 `(platform, goods_id)`，仅选信息最丰富的一条送入 Stage 4 (AI-2)，其余同批重复线报提前终止并累加共振！
        best_mid_by_pid: Dict[Tuple[str, str], Tuple[float, str]] = {}
        for mid, _doc, _ai1_res, _conv_list, single_pid, r_score in post_stage3_candidates:
            if single_pid is not None:
                prev = best_mid_by_pid.get(single_pid)
                if prev is None or r_score > prev[0]:
                    best_mid_by_pid[single_pid] = (r_score, mid)

        post_stage3_batch_early_count = 0
        for mid, doc, ai1_res, conv_list, single_pid, _r_score in post_stage3_candidates:
            site = str(doc.get("source_site") or "")
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()

            if single_pid is not None and best_mid_by_pid.get(single_pid, (0.0, mid))[1] != mid:
                winner_mid = best_mid_by_pid[single_pid][1]
                c_plat, c_gid = single_pid
                main_prod_convs = [c for c in conv_list if c["convert_status"] == "product" and c.get("text_role_hint") != "filler_candidate"]
                primary_conv = main_prod_convs[0]
                c_gsign = str(primary_conv.get("goods_sign") or "")
                off_p = primary_conv.get("official_product") or {}
                merged_tids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
                if mid not in merged_tids:
                    merged_tids.insert(0, mid)
                self.stats_early_terminated_post_stage3 += 1
                post_stage3_batch_early_count += 1
                pre_stage2_outcomes[mid] = {
                    "status": "early_terminated",
                    "early_stage": "post_stage3_batch",
                    "error_code": OK_SKIPPED_ALREADY_CLEANED,
                    "error_msg": f"同批次已有更优线报({winner_mid})命中同一唯一商品({c_plat}:{c_gid})，提前终止跳过AI-2并累加共振",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": c_plat,
                    "product_id": c_gid,
                    "goods_sign": c_gsign,
                    "official_title": off_p.get("title") or raw_title,
                    "official_price": off_p.get("price"),
                    "deal_price": None,
                    "title": raw_title,
                    "quality_score": 75,
                    "merged_tip_ids": merged_tids,
                    "ai_stage1_result": ai1_res,
                    "converted_links": [
                        {k: v for k, v in c.items() if k != "adapter_res"}
                        for c in conv_list
                    ],
                    "ai_stage2_result": {"early_terminated": "post_stage3_same_batch_duplicate", "winner_tip_id": winner_mid},
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                    "resonance_update": {
                        "platform": c_plat,
                        "product_id": c_gid,
                        "seen_count": max(1, int(doc.get("seen_count") or 1)),
                        "source_site": site,
                        "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                    },
                }
                continue

            # 构造送往 Stage 4 (AI-2) 的精简底牌数据（隐去庞大的 raw_item 字典以提升 AI 推理速度与准确度）
            ai2_conv_links = [
                {
                    "link_id": c["link_id"],
                    "text_role_hint": c["text_role_hint"],
                    "convert_status": c["convert_status"],
                    "platform": c["platform"],
                    "official_product": c["official_product"],
                    "my_promo_link": c["my_promo_link"],
                    "my_promo_token": c["my_promo_token"],
                }
                for c in conv_list
            ]
            stage2_inputs.append({
                "main_tip_id": mid,
                "source_site": site,
                "is_group_stream": bool(doc.get("is_group_stream")),
                "combined_raw_text": str(ai1_res.get("combined_raw_text") or ""),
                "extracted_images": ai1_res.get("extracted_images") or [],
                "converted_links": ai2_conv_links,
            })

        if post_stage3_db_early_count > 0 or post_stage3_batch_early_count > 0:
            logger.info(
                "⚡ [Stage 3 唯一商品标识早停] 转链后命中唯一商品 `(platform, goods_id)` 提前终止=%d条 "
                "(已在正式库=%d条, 同批去重=%d条) -> 直接跳过 Stage 4 (AI-2)！",
                post_stage3_db_early_count + post_stage3_batch_early_count,
                post_stage3_db_early_count,
                post_stage3_batch_early_count,
            )

        # ---------------- Stage 4: 调用 AI-2 进行底牌终审、教程重构与 0~100 质量评分 ----------------
        t_ai2_start = time.perf_counter()
        ai2_hits_before = self.stats_ai2_model_hits
        ai2_fb_before = self.stats_ai2_fallbacks
        logger.info(
            "🧠 [Stage 4 / AI-2] 正在并发终审 %d 条新唯一商品线报 (batch_size=%d, workers=%d)...",
            len(stage2_inputs),
            self.ai_batch_size,
            self.ai_max_workers,
        )
        ai2_results = self.run_ai_stage2_batch(stage2_inputs)
        t_ai2_elapsed = round(time.perf_counter() - t_ai2_start, 2)
        s2_input_map = {item["main_tip_id"]: item for item in stage2_inputs}
        self.last_batch_stage_timings = {
            "stage2_ai1_seconds": t_ai1_elapsed,
            "stage3_convert_seconds": t_stage3_elapsed,
            "stage4_ai2_seconds": t_ai2_elapsed,
        }

        avg_ms = round((time.perf_counter() - t_batch_start) * 1000.0 / max(1, len(selected_docs)), 1)
        final_results: List[Dict[str, Any]] = []

        for doc in selected_docs:
            mid = str(doc.get("source_item_id") or "")
            if mid in pre_stage2_outcomes:
                out = pre_stage2_outcomes[mid]
                out["elapsed_ms"] = avg_ms
                final_results.append(out)
                continue

            site = str(doc.get("source_site") or "")
            channel = str(doc.get("source_channel") or "")
            raw_title = str(doc.get("title") or "").strip()
            ai1_res = ai1_results.get(mid) or {}
            conv_list = converted_by_tip_id.get(mid) or []
            s2_inp = s2_input_map.get(mid) or {}
            ai2_res = ai2_results.get(mid)
            if not isinstance(ai2_res, dict) or "is_valid_deal" not in ai2_res:
                self.stats_ai2_fallbacks += 1
                ai2_res = self._fallback_stage2_evaluate(s2_inp)
            else:
                self.stats_ai2_model_hits += 1
                ai2_res["_from_fallback"] = False

            conv_summary_for_db = [
                {k: v for k, v in c.items() if k != "adapter_res"}
                for c in conv_list
            ]
            link_map = {c["link_id"]: c for c in conv_list}

            is_valid = bool(ai2_res.get("is_valid_deal"))
            main_lid = str(ai2_res.get("main_link_id") or "").strip()
            main_conv = link_map.get(main_lid)
            declared_filler_ids = {
                str(item.get("link_id") or "").strip()
                for item in (ai2_res.get("filler_links") or [])
                if isinstance(item, dict)
            }
            # 【严格遵循核心铁律】：主商品 main_link_id 必须指向真实存活的官方单品 ("product") 且绝不能位于凑单品列表内；
            # 若主商品链接无效，严禁在代码中盲目拿 prod_convs[0] 凑单品顶替主商品，必须直接判为无效拦截！
            if is_valid and (
                not main_conv
                or main_conv.get("convert_status") != "product"
                or main_lid in declared_filler_ids
            ):
                is_valid = False
                if not ai2_res.get("reject_reason"):
                    ai2_res["reject_reason"] = "MAIN_PRODUCT_DEAD_ONLY_FILLER"

            if not is_valid or not main_conv:
                rej = str(ai2_res.get("reject_reason") or "ALL_PRODUCTS_DEAD").upper()
                if "FILLER" in rej:
                    err_code = ERR_AI2_MAIN_DEAD_ONLY_FILLER
                elif "MISMATCH" in rej:
                    err_code = ERR_AI2_PRODUCT_MISMATCH
                elif "VIRTUAL" in rej or "JUNK" in rej:
                    err_code = ERR_AI2_VIRTUAL_OR_JUNK
                else:
                    err_code = ERR_ADAPTER_CONVERT_FAILED
                final_results.append({
                    "status": "failed",
                    "error_code": err_code,
                    "error_msg": f"AI-2 终审拦截 ({rej})",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "title": raw_title,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_for_db,
                    "ai_stage2_result": ai2_res,
                    "quality_score": 0,
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                    "elapsed_ms": avg_ms,
                })
                continue

            try:
                quality_score = int(float(ai2_res.get("quality_score") or 0))
            except Exception:
                quality_score = 0
            quality_score = max(0, min(100, quality_score))
            score_breakdown = ai2_res.get("score_breakdown") if isinstance(ai2_res.get("score_breakdown"), dict) else {}

            # 组装主商品官方真实详情与爆料专属 deal_info（严格隔离顶层官方价格与爆料价！）
            plat = str(main_conv.get("platform") or "")
            goods_id = str(main_conv.get("goods_id") or "")
            goods_sign = str(main_conv.get("goods_sign") or "")
            adapter_res = main_conv.get("adapter_res") if isinstance(main_conv.get("adapter_res"), dict) else {}
            product_card = copy.deepcopy(adapter_res.get("product") if isinstance(adapter_res.get("product"), dict) else {})
            off_prod_fallback = main_conv.get("official_product") if isinstance(main_conv.get("official_product"), dict) else {}
            if not product_card and off_prod_fallback:
                product_card = {
                    "name": off_prod_fallback.get("title") or "",
                    "price": off_prod_fallback.get("price") or 0.0,
                    "original_price": off_prod_fallback.get("original_price") or off_prod_fallback.get("price") or 0.0,
                    "activity_price": off_prod_fallback.get("activity_price") or off_prod_fallback.get("price") or 0.0,
                    "coupon_amount": off_prod_fallback.get("coupon_amount") or 0.0,
                    "mall_name": off_prod_fallback.get("shop_name") or "",
                }

            my_promo_link = str(main_conv.get("my_promo_link") or "")
            my_promo_token = str(main_conv.get("my_promo_token") or "")

            # 提取并校验 deal_price（仅信任 AI-2 语义核验后的 deal_price，严禁回退使用旧爬虫正则提取的 doc.get("price")！）
            raw_deal_p = ai2_res.get("deal_price")
            deal_price: Optional[float] = None
            if isinstance(raw_deal_p, (int, float)) and 0 < float(raw_deal_p) < 100000:
                deal_price = round(float(raw_deal_p), 2)
            elif isinstance(raw_deal_p, str):
                try:
                    val = float(re.sub(r"[^\d.]", "", raw_deal_p))
                    if 0 < val < 100000:
                        deal_price = round(val, 2)
                except Exception:
                    pass

            unit_deal_price = str(ai2_res.get("unit_deal_price") or "").strip() or None
            price_logic = str(ai2_res.get("price_logic") or "").strip() or None
            clean_title = str(ai2_res.get("clean_title") or product_card.get("name") or raw_title).strip()

            # 组装前置优惠券列表 coupon_links 与凑单商品列表 filler_links（100% 遵循 AI-2 终审结论，严禁盲目追加 AI-2 已剔除的无关外链）
            coupon_links: List[Dict[str, Any]] = []
            seen_cpn_ids: Set[str] = set()
            for item in ai2_res.get("coupon_links") or []:
                if not isinstance(item, dict):
                    continue
                lid = str(item.get("link_id") or "").strip()
                c_obj = link_map.get(lid)
                if not c_obj or c_obj.get("convert_status") == "failed" or lid == main_lid or lid in seen_cpn_ids:
                    continue
                seen_cpn_ids.add(lid)
                coupon_links.append({
                    "link_id": lid,
                    "label": str(item.get("label") or "先领优惠券").strip(),
                    "url": c_obj.get("my_promo_link") or "",
                    "token": c_obj.get("my_promo_token") or "",
                    "platform": c_obj.get("platform") or plat,
                })

            filler_links: List[Dict[str, Any]] = []
            seen_flr_ids: Set[str] = set()
            for item in ai2_res.get("filler_links") or []:
                if not isinstance(item, dict):
                    continue
                lid = str(item.get("link_id") or "").strip()
                c_obj = link_map.get(lid)
                if not c_obj or c_obj.get("convert_status") != "product" or lid == main_lid or lid in seen_flr_ids:
                    continue
                seen_flr_ids.add(lid)
                off_p = c_obj.get("official_product") or {}
                filler_links.append({
                    "link_id": lid,
                    "label": str(item.get("label") or f"凑单商品：{off_p.get('title', '')[:20]}").strip(),
                    "url": c_obj.get("my_promo_link") or "",
                    "token": c_obj.get("my_promo_token") or "",
                    "platform": c_obj.get("platform") or plat,
                    "goods_id": c_obj.get("goods_id") or "",
                    "official_title": off_p.get("title") or "",
                    "official_price": off_p.get("price"),
                })

            # 将 tutorial_steps 与 tutorial_summary 中的 [#L1] 占位符替换为我方推广链/口令
            raw_steps = ai2_res.get("tutorial_steps") if isinstance(ai2_res.get("tutorial_steps"), list) else []
            tutorial_steps = [
                self._inject_converted_links_into_text(str(st).strip(), link_map)
                for st in raw_steps
                if str(st or "").strip()
            ]
            raw_summary = str(ai2_res.get("tutorial_summary") or ai1_res.get("combined_raw_text") or "").strip()
            tutorial_text = self._inject_converted_links_into_text(raw_summary, link_map)
            if not tutorial_steps and tutorial_text:
                tutorial_steps = [ln.strip() for ln in tutorial_text.splitlines() if ln.strip()]

            # 生成保留原博主“活人感话术”（Emoji、情绪词、卖点文案）且剔除第三方链接/口令并注入我方推广链的纯净原始话术
            original_copywriting = self._build_clean_original_copywriting(
                str(ai1_res.get("combined_raw_text") or ""),
                ai1_res.get("extracted_targets") or [],
                link_map,
                doc=doc,
            )

            # 提取用户实付晒单图与教程图（排除与官方商品主图完全相同的 URL，并剥离偶发的 Markdown 链接包裹）
            official_img = str(product_card.get("image_url") or "").strip()
            extracted_imgs = [
                self._strip_markdown_link_wrapper(str(u).strip())
                for u in (ai1_res.get("extracted_images") or [])
                if str(u or "").strip()
            ]
            user_share_images: List[str] = []
            for img_u in extracted_imgs:
                if img_u and img_u != official_img and img_u not in user_share_images:
                    user_share_images.append(img_u)

            # 汇总合并的群消息 ID 与源页面 URL 列表（满足用户核对源页面的需求）
            merged_tip_ids = [str(x).strip() for x in (ai1_res.get("merged_tip_ids") or [mid]) if str(x or "").strip()]
            if mid not in merged_tip_ids:
                merged_tip_ids.insert(0, mid)
            tip_map = neighbor_maps_by_id.get(mid) or {mid: doc}
            merged_source_urls: List[str] = []
            for m_tid in merged_tip_ids:
                nd = tip_map.get(m_tid) or {}
                u_det = str(nd.get("source_detail_url") or nd.get("source_url") or "").strip()
                if u_det and u_det not in merged_source_urls:
                    merged_source_urls.append(u_det)

            source_list_page_url = str(doc.get("source_list_page_url") or doc.get("source_url") or "").strip()
            source_detail_url = str(doc.get("source_detail_url") or doc.get("source_url") or source_list_page_url).strip()

            # 严格保持顶层价格 100% 为官方 API 真实价格，绝不用爆料价覆盖！
            official_price = float(product_card.get("price") or adapter_res.get("price") or 0.0)
            original_price = float(product_card.get("original_price") or official_price)
            official_activity_price = float(product_card.get("activity_price") or official_price)
            coupon_amount = float(product_card.get("coupon_amount") or adapter_res.get("coupon_amount") or 0.0)
            saved_price = round(max(coupon_amount, max(0.0, original_price - official_activity_price)), 2)

            publish_time_str = str(doc.get("publish_time") or doc.get("fetched_at") or "").strip()
            aux_list = [*coupon_links, *filler_links]
            deal_price_text = f"{deal_price}元" if deal_price is not None else (unit_deal_price or "")

            deal_info = {
                "is_deal_tip": True,
                "quality_score": quality_score,
                "score_breakdown": score_breakdown,
                "clean_title": clean_title,
                "publish_time": publish_time_str,
                "expire_time": doc.get("expire_time"),
                "deal_price": deal_price,
                "deal_price_text": deal_price_text,
                "unit_deal_price": unit_deal_price,
                "price_logic": price_logic,
                "official_price": round(official_price, 2),
                "raw_tip_title": raw_title or str(product_card.get("name") or ""),
                "original_copywriting": original_copywriting,
                "coupon_links": coupon_links,
                "filler_links": filler_links,
                "auxiliary_links": aux_list,
                "tutorial_steps": tutorial_steps,
                "tutorial_text": tutorial_text,
                "user_share_images": user_share_images,
                "tutorial_images": user_share_images,
                "my_promotion_link": my_promo_link,
                "my_promotion_token": my_promo_token,
                "source_site": site,
                "source_channel": channel,
                "source_item_id": mid,
                "source_url": source_detail_url,
                "source_list_page_url": source_list_page_url,
                "source_detail_url": source_detail_url,
                "merged_tip_ids": merged_tip_ids,
                "merged_source_urls": merged_source_urls,
                "seen_count": int(doc.get("seen_count") or 1),
                "seen_channels": doc.get("seen_channels") or ([channel] if channel else []),
                "hot_metrics": doc.get("hot_metrics") or {},
            }

            product_doc = {
                **product_card,
                "platform": plat,
                "platform_name": product_card.get("platform_name") or platform_name(plat),
                "product_id": goods_id,
                "goods_id": goods_id,
                "goods_sign": goods_sign,
                "name": str(product_card.get("name") or adapter_res.get("title") or clean_title).strip(),
                "brand": str(product_card.get("brand") or product_card.get("brand_name") or ""),
                "category": str(product_card.get("category") or product_card.get("category_name") or ""),
                "price": round(official_price, 2),
                "original_price": round(original_price, 2),
                "activity_price": round(official_activity_price, 2),
                "saved_price": round(saved_price, 2),
                "coupon_amount": round(coupon_amount, 2),
                "has_coupon": coupon_amount > 0,
                "commission_rate": float(product_card.get("commission_rate") or 0.0),
                "commission_amount": float(product_card.get("commission_amount") or 0.0),
                "sales_tip": str(product_card.get("sales_tip") if product_card.get("sales_tip") is not None else "0"),
                "image_url": official_img or str(doc.get("image_url") or ""),
                "product_url": str(product_card.get("product_url") or my_promo_link or ""),
                "mall_name": str(product_card.get("mall_name") or doc.get("mall_name") or ""),
                "desc": str(product_card.get("desc") or ""),
                "carousel_images": product_card.get("carousel_images") or ([official_img] if official_img else []),
                "detail_tags": product_card.get("detail_tags") or [],
                "service_guarantees": product_card.get("service_guarantees") or [],
                "shop_ratings": product_card.get("shop_ratings") or {},
                "specs": product_card.get("specs") or [],
                "coupon_info": product_card.get("coupon_info") or {},
                "deal_info": deal_info,
            }

            if quality_score < QUALIFIED_DEAL_MIN_SCORE:
                final_results.append({
                    "status": "low_score",
                    "error_code": ERR_LOW_QUALITY_SCORE,
                    "error_msg": f"综合评分 {quality_score} < {QUALIFIED_DEAL_MIN_SCORE} 分，仅留存初步表不入正式表",
                    "source_site": site,
                    "source_channel": channel,
                    "source_item_id": mid,
                    "platform": plat,
                    "product_id": goods_id,
                    "goods_sign": goods_sign,
                    "official_title": product_doc["name"],
                    "title": raw_title,
                    "deal_price": deal_price,
                    "official_price": official_price,
                    "quality_score": quality_score,
                    "score_breakdown": score_breakdown,
                    "merged_tip_ids": merged_tip_ids,
                    "original_copywriting": original_copywriting,
                    "ai_stage1_result": ai1_res,
                    "converted_links": conv_summary_for_db,
                    "ai_stage2_result": ai2_res,
                    "candidate_count": len(conv_list),
                    "attempt_errors": [],
                    "elapsed_ms": avg_ms,
                })
                continue

            final_results.append({
                "status": "success",
                "error_code": "OK",
                "error_msg": "",
                "source_site": site,
                "source_channel": channel,
                "source_item_id": mid,
                "platform": plat,
                "product_id": goods_id,
                "goods_sign": goods_sign,
                "official_title": product_doc["name"],
                "raw_title": raw_title,
                "deal_price": deal_price,
                "official_price": official_price,
                "quality_score": quality_score,
                "score_breakdown": score_breakdown,
                "merged_tip_ids": merged_tip_ids,
                "original_copywriting": original_copywriting,
                "ai_stage1_result": ai1_res,
                "converted_links": conv_summary_for_db,
                "ai_stage2_result": ai2_res,
                "winning_anchor": str(main_conv.get("clean_input") or "")[:100],
                "winning_anchor_type": main_conv.get("text_role_hint") or "main_candidate",
                "candidate_count": len(conv_list),
                "tutorial_img_count": len(user_share_images),
                "aux_link_count": len(aux_list),
                "elapsed_ms": avg_ms,
                "product_doc": product_doc,
            })

        if stage2_inputs:
            logger.info(
                "🧠 [Stage 4 / AI-2 完成] 本批模型直出=%d条, 保底兜底=%d条",
                self.stats_ai2_model_hits - ai2_hits_before,
                self.stats_ai2_fallbacks - ai2_fb_before,
            )

        return final_results


def upsert_cleaned_products_to_formal_table(
    formal_col,
    product_docs: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    【Stage 5：高分好价商品晋升入正式表 `products`（含质量评分防覆盖保护）】
    - 严格以唯一索引 `{"platform": plat, "product_id": pid}` 作为过滤条件；
    - 顶层商品属性 (`name`, `price`, `original_price`, `activity_price` 等) 始终保持官方最新数据；
    - 高分保护法则：
      1) 同批次多条线报命中同一商品时，按 `deal_info.quality_score` 最高者胜出；
      2) 若 `products` 表中该商品已有 `deal_info.is_deal_tip == True` 且 `updated_at` 在 12 小时保鲜期内，
         仅当新线报 `quality_score >= 已有 deal_info.quality_score` 时才更新 `deal_info` 内容，
         防止低分简陋线报覆盖带优惠券、带凑单教程、带实付晒单图的高分精品线报！
    """
    if not product_docs:
        return {"upserted": 0, "modified": 0, "matched": 0, "protected_high_score": 0, "total_ops": 0}

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    protection_cutoff = now - datetime.timedelta(hours=DEAL_INFO_PROTECTION_HOURS)

    grouped_by_pid: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for doc in product_docs:
        grouped_by_pid[(doc["platform"], doc["product_id"])].append(doc)

    # 批量查询正式表中已存在的同款商品，检查其现有 deal_info.quality_score 与更新时间
    existing_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        or_queries = [{"platform": p, "product_id": pid} for (p, pid) in grouped_by_pid.keys()]
        if or_queries:
            for ex_doc in formal_col.find(
                {"$or": or_queries},
                {"_id": 0, "platform": 1, "product_id": 1, "updated_at": 1, "deal_info.is_deal_tip": 1, "deal_info.quality_score": 1},
            ):
                k = (str(ex_doc.get("platform") or ""), str(ex_doc.get("product_id") or ""))
                existing_map[k] = ex_doc
    except Exception as e:
        logger.warning("查询正式表已有商品评分异常: %s", e)

    ops = []
    protected_high_score = 0

    for (plat, pid), docs_for_item in grouped_by_pid.items():
        # 按 quality_score 升序排列，取最高分的一条作为胜出者
        docs_sorted = sorted(
            docs_for_item,
            key=lambda x: int((x.get("deal_info") or {}).get("quality_score") or 0),
        )
        best_doc = copy.deepcopy(docs_sorted[-1])
        deal_info = copy.deepcopy(best_doc.pop("deal_info", {}) or {})
        new_score = int(deal_info.get("quality_score") or 0)

        total_seen_inc = 0
        all_sites: List[str] = []
        all_channels: List[str] = []
        for d in docs_for_item:
            di = d.get("deal_info") or {}
            total_seen_inc += max(1, int(di.get("seen_count") or 1))
            s_site = str(di.get("source_site") or "").strip()
            if s_site and s_site not in all_sites:
                all_sites.append(s_site)
            for ch in di.get("seen_channels") or []:
                ch_s = str(ch or "").strip()
                if ch_s and ch_s not in all_channels:
                    all_channels.append(ch_s)

        deal_info.pop("seen_count", None)
        deal_info.pop("seen_channels", None)

        set_fields: Dict[str, Any] = {
            **best_doc,
            "updated_at": now,
            "detail_updated_at": now,
            "last_clean_source": "deal_pool",
        }

        ex_record = existing_map.get((plat, pid))
        ex_di = (ex_record or {}).get("deal_info") if isinstance((ex_record or {}).get("deal_info"), dict) else {}
        ex_score = int(ex_di.get("quality_score") or 0)
        ex_updated = _to_naive_utc((ex_record or {}).get("updated_at"))
        is_fresh_existing = bool(
            ex_di.get("is_deal_tip")
            and ex_updated is not None
            and ex_updated >= protection_cutoff
        )

        if is_fresh_existing and ex_score > new_score:
            # 触发高分保护：保留已有的高分 deal_info 教程与价格，仅更新顶层官方商品详情并累加跨群共振热度
            protected_high_score += 1
        else:
            for dk, dv in deal_info.items():
                set_fields[f"deal_info.{dk}"] = dv

        update_spec: Dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {
                "created_at": now,
                "source": "deal_pool",
            },
            "$inc": {
                "deal_info.total_seen_count": total_seen_inc,
                "deal_info.tip_hit_count": len(docs_for_item),
            },
        }
        add_to_set: Dict[str, Any] = {}
        if all_sites:
            add_to_set["deal_info.resonance_sites"] = {"$each": all_sites}
        if all_channels:
            add_to_set["deal_info.resonance_channels"] = {"$each": all_channels}
        if add_to_set:
            update_spec["$addToSet"] = add_to_set

        ops.append(UpdateOne({"platform": plat, "product_id": pid}, update_spec, upsert=True))

    res = formal_col.bulk_write(ops, ordered=False)
    return {
        "upserted": res.upserted_count,
        "modified": res.modified_count,
        "matched": res.matched_count,
        "protected_high_score": protected_high_score,
        "total_ops": len(ops),
    }


def run_clean_stage(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    cycle_num: int,
    clean_limit: int = 300,
) -> Dict[str, Any]:
    """
    执行 Stage 2 ~ Stage 5 完整闭环：
    从初步表 `deal_raw_tips` 读取 `clean_status='pending'` 的记录 ->
    采用【站点保底配额 (15条/站) + 最新积压优先】调度策略（既保证小站点零饥饿，又全速消化折淘客/线报酷高并发积压） ->
    Tier 1 前置缓存早停 -> Stage 2 (AI-1 批量拆解与群聊上下文合并) -> Stage 3 (官方 Adapter 10线程并发转链) ->
    Tier 2 唯一商品标识 (`platform`, `goods_id`) 早停 -> Stage 4 (AI-2 终审评分并先落盘 `deal_raw_tips`) ->
    Stage 5 (将 `quality_score >= 60` 的高分优品带高分保护写入正式表 `products`，并累加提前终止记录的跨站共振热度)。
    """
    t_start = time.perf_counter()
    raw_col = repo.col
    formal_col = repo.formal_col

    pending_docs = list(
        raw_col.find({"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]})
        .sort("last_seen_at", -1)
    )

    if not pending_docs:
        logger.info("✨ [阶段二~五：双AI+官方转链清洗] 当前初步表 `deal_raw_tips` 无待清洗 (`pending`) 记录，跳过本轮清洗。")
        return {
            "cycle_num": cycle_num,
            "total_pending_found": 0,
            "total_processed": 0,
            "resolved_success": 0,
            "early_terminated": 0,
            "formal_upserted": 0,
            "formal_modified": 0,
            "elapsed_s": 0.0,
        }

    # 【站点保底配额 + 全局最新积压优先调度】：
    # 第一步：每个站点先通过 Round-Robin 保底分配最多 15 条，确保小体量站点（0818tuan/iehou/xuandan/bacaoo/guangdiu/haodanku）每轮 0 积压；
    # 第二步：剩余配额按 `last_seen_at` 倒序从全局剩余 pending 中补齐至 `clean_limit`，彻底解除对折淘客 (zhetaoke) 与线报酷 (xianbao_fun) 的 1:1 限速！
    docs_by_site: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for doc in pending_docs:
        site = str(doc.get("source_site") or "unknown")
        docs_by_site[site].append(doc)

    base_per_site = 15
    base_queues = [docs_by_site[s][:base_per_site] for s in sorted(docs_by_site.keys())]
    selected_docs: List[Dict[str, Any]] = []
    selected_keys: Set[Tuple[str, str]] = set()

    for group in zip_longest(*base_queues):
        for item in group:
            if item is not None:
                k = (str(item.get("source_site") or ""), str(item.get("source_item_id") or ""))
                if k not in selected_keys:
                    selected_keys.add(k)
                    selected_docs.append(item)

    for doc in pending_docs:
        if clean_limit and clean_limit > 0 and len(selected_docs) >= clean_limit:
            break
        k = (str(doc.get("source_site") or ""), str(doc.get("source_item_id") or ""))
        if k not in selected_keys:
            selected_keys.add(k)
            selected_docs.append(doc)

    if clean_limit and clean_limit > 0:
        selected_docs = selected_docs[:clean_limit]

    logger.info(
        "🧼 ================ 开始第 #%d 轮 Stage 2~5：双阶段 AI 提纯 + 并发转链验真 + 唯一商品早停 + 高分晋升 "
        "(Pending总计=%d条, 本轮执行=%d条) ================",
        cycle_num,
        len(pending_docs),
        len(selected_docs),
    )

    all_processed_results = cleaner.clean_batch_records(selected_docs, raw_col, formal_col=formal_col)
    success_product_docs: List[Dict[str, Any]] = []
    early_terminated_results: List[Dict[str, Any]] = []

    for idx, res in enumerate(all_processed_results, 1):
        site = res["source_site"]
        if res["status"] == "success":
            success_product_docs.append(res["product_doc"])
            logger.info(
                "  ✅ [终审通过 %03d/%03d] [%-11s] %-6s | 评分=%2d | ID=%-22s | 爆料价=%-6s 官价=%-6s | %s",
                idx,
                len(all_processed_results),
                site,
                res["platform"],
                res.get("quality_score", 0),
                res["product_id"][:22],
                str(res["deal_price"]),
                str(res["official_price"]),
                str(res["official_title"])[:30],
            )
        elif res["status"] == "early_terminated":
            early_terminated_results.append(res)
            logger.info(
                "  ⚡ [提前终止 %03d/%03d] [%-11s] %-6s | ID=%-22s | %s | %s",
                idx,
                len(all_processed_results),
                site,
                res.get("platform") or "-",
                str(res.get("product_id") or "")[:22],
                res.get("error_msg", "")[:42],
                str(res.get("official_title") or res.get("title") or "")[:26],
            )
        elif res["status"] == "merged_into_parent":
            logger.info(
                "  🔗 [上下文合并 %03d/%03d] [%-11s] %s -> %s",
                idx,
                len(all_processed_results),
                site,
                res["source_item_id"],
                res["error_msg"],
            )
        elif res["status"] == "low_score":
            logger.info(
                "  ⚪ [低分留存 %03d/%03d] [%-11s] 评分=%2d | %s -> %s",
                idx,
                len(all_processed_results),
                site,
                res.get("quality_score", 0),
                str(res.get("title") or "")[:28],
                res["error_msg"],
            )
        else:
            logger.info(
                "  ❌ [清洗拦截 %03d/%03d] [%-11s] %-28s | %s -> %s",
                idx,
                len(all_processed_results),
                site,
                res["error_code"],
                str(res.get("title") or "")[:28],
                res["error_msg"][:45],
            )

    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # 1. Stage 4 落盘：先将所有记录的 AI-1 拆解结果、Stage 3 转链底牌、早停/AI-2 终审结果与评分回写至初步表 `deal_raw_tips`
    raw_status_ops = []
    failed_samples: List[Dict[str, Any]] = []
    for r in all_processed_results:
        s_site = r["source_site"]
        s_id = r["source_item_id"]
        common_ai_fields = {
            "cleaned_at": now_utc,
            "ai_stage1_result": r.get("ai_stage1_result"),
            "converted_links": r.get("converted_links") or [],
            "ai_stage2_result": r.get("ai_stage2_result"),
            "quality_score": r.get("quality_score", 0),
        }
        if r.get("original_copywriting"):
            common_ai_fields["original_copywriting"] = r["original_copywriting"]
        if r["status"] == "success":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_success",
                            "ai_clean_status": "cleaned_success",
                            "clean_error_code": "OK",
                            "clean_error_msg": "",
                            "resolved_platform": r["platform"],
                            "resolved_product_id": r["product_id"],
                            "resolved_goods_sign": r["goods_sign"],
                            "resolved_official_title": r["official_title"],
                            "merged_tip_ids": r.get("merged_tip_ids") or [s_id],
                        }
                    },
                )
            )
            for m_tid in r.get("merged_tip_ids") or []:
                if m_tid != s_id:
                    raw_status_ops.append(
                        UpdateOne(
                            {"source_site": s_site, "source_item_id": m_tid, "clean_status": {"$in": ["pending", "context_only"]}},
                            {
                                "$set": {
                                    "clean_status": "merged_into_parent",
                                    "ai_clean_status": "merged_into_parent",
                                    "merged_into_tip_id": s_id,
                                    "cleaned_at": now_utc,
                                }
                            },
                        )
                    )
        elif r["status"] == "early_terminated":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_success",
                            "ai_clean_status": "cleaned_early_terminated",
                            "clean_error_code": OK_SKIPPED_ALREADY_CLEANED,
                            "clean_error_msg": r["error_msg"],
                            "resolved_platform": r.get("platform"),
                            "resolved_product_id": r.get("product_id"),
                            "resolved_goods_sign": r.get("goods_sign") or "",
                            "resolved_official_title": r.get("official_title") or "",
                            "merged_tip_ids": r.get("merged_tip_ids") or [s_id],
                        }
                    },
                )
            )
            for m_tid in r.get("merged_tip_ids") or []:
                if m_tid != s_id:
                    raw_status_ops.append(
                        UpdateOne(
                            {"source_site": s_site, "source_item_id": m_tid, "clean_status": {"$in": ["pending", "context_only"]}},
                            {
                                "$set": {
                                    "clean_status": "merged_into_parent",
                                    "ai_clean_status": "merged_into_parent",
                                    "merged_into_tip_id": s_id,
                                    "cleaned_at": now_utc,
                                }
                            },
                        )
                    )
        elif r["status"] == "merged_into_parent":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "merged_into_parent",
                            "ai_clean_status": "merged_into_parent",
                            "merged_into_tip_id": r.get("merged_into_tip_id") or "",
                            "clean_error_code": r["error_code"],
                            "clean_error_msg": r["error_msg"],
                        }
                    },
                )
            )
        elif r["status"] == "low_score":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_low_score",
                            "ai_clean_status": "cleaned_low_score",
                            "clean_error_code": r["error_code"],
                            "clean_error_msg": r["error_msg"],
                            "resolved_platform": r.get("platform"),
                            "resolved_product_id": r.get("product_id"),
                            "resolved_goods_sign": r.get("goods_sign"),
                            "resolved_official_title": r.get("official_title"),
                        }
                    },
                )
            )
        else:
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            **common_ai_fields,
                            "clean_status": "cleaned_failed",
                            "ai_clean_status": "cleaned_failed",
                            "clean_error_code": r["error_code"],
                            "clean_error_msg": r["error_msg"],
                            "clean_attempt_errors": r.get("attempt_errors") or [],
                        }
                    },
                )
            )
            failed_samples.append({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "cycle_num": cycle_num,
                "source_site": s_site,
                "source_channel": r.get("source_channel"),
                "source_item_id": s_id,
                "error_code": r["error_code"],
                "error_msg": r["error_msg"],
                "title": r.get("title"),
                "attempt_errors": r.get("attempt_errors") or [],
            })

    if raw_status_ops:
        try:
            raw_col.bulk_write(raw_status_ops, ordered=False)
        except Exception as e:
            logger.error("回写初步表 `deal_raw_tips` 清洗状态异常: %s", e)

    # 2. Stage 5 晋升：将所有通过终审的高分商品（quality_score >= 60）带高分保护幂等写入正式表 `products`
    try:
        formal_write_res = upsert_cleaned_products_to_formal_table(formal_col, success_product_docs)
    except Exception as e:
        logger.exception("写入正式表 `products` 发生异常: %s", e)
        formal_write_res = {"upserted": 0, "modified": 0, "matched": 0, "protected_high_score": 0, "total_ops": 0}

    # 3. 对提前终止 (early_terminated) 的记录，在正式表 `products` 中原子累加跨站/跨群共振热度 (upsert=False，确保绝不新增未终审商品)
    early_resonance_modified = 0
    if early_terminated_results:
        grouped_res: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for er_item in early_terminated_results:
            ru = er_item.get("resonance_update") or {}
            plat = str(ru.get("platform") or er_item.get("platform") or "").strip()
            pid = str(ru.get("product_id") or er_item.get("product_id") or "").strip()
            if not plat or not pid:
                continue
            bucket = grouped_res.setdefault(
                (plat, pid),
                {"seen_inc": 0, "hit_inc": 0, "sites": set(), "channels": set()},
            )
            bucket["seen_inc"] += max(1, int(ru.get("seen_count") or 1))
            bucket["hit_inc"] += 1
            s_site = str(ru.get("source_site") or "").strip()
            if s_site:
                bucket["sites"].add(s_site)
            for ch in ru.get("seen_channels") or []:
                ch_s = str(ch or "").strip()
                if ch_s:
                    bucket["channels"].add(ch_s)

        early_ops = []
        for (plat, pid), b in grouped_res.items():
            upd_doc: Dict[str, Any] = {
                "$inc": {
                    "deal_info.total_seen_count": b["seen_inc"],
                    "deal_info.tip_hit_count": b["hit_inc"],
                }
            }
            add_set: Dict[str, Any] = {}
            if b["sites"]:
                add_set["deal_info.resonance_sites"] = {"$each": sorted(b["sites"])}
            if b["channels"]:
                add_set["deal_info.resonance_channels"] = {"$each": sorted(b["channels"])}
            if add_set:
                upd_doc["$addToSet"] = add_set
            early_ops.append(UpdateOne({"platform": plat, "product_id": pid}, upd_doc, upsert=False))
        if early_ops:
            try:
                er_res = formal_col.bulk_write(early_ops, ordered=False)
                early_resonance_modified = er_res.modified_count
            except Exception as e:
                logger.warning("累加提前终止商品共振热度异常: %s", e)

    batch_elapsed_s = round(time.perf_counter() - t_start, 2)

    # ==========================================================================
    # 构建阶段二~五全维度清洗运维统计报告 (`latest_clean_ops_report.json`)
    # ==========================================================================
    total_scanned = len(all_processed_results)
    has_candidate_cnt = sum(1 for r in all_processed_results if r.get("candidate_count", 0) > 0)
    resolved_cnt = len(success_product_docs)
    early_terminated_cnt = len(early_terminated_results)
    effective_resolved_cnt = resolved_cnt + early_terminated_cnt
    merged_cnt = sum(1 for r in all_processed_results if r["status"] == "merged_into_parent")
    low_score_cnt = sum(1 for r in all_processed_results if r["status"] == "low_score")
    unique_pids_in_batch = len({
        (r["platform"], r["product_id"])
        for r in all_processed_results
        if r["status"] in ("success", "early_terminated") and r.get("platform") and r.get("product_id")
    })

    formal_total_now = formal_col.count_documents({})
    formal_deal_total_now = formal_col.count_documents({"deal_info.is_deal_tip": True})

    sites_in_batch = sorted({r["source_site"] for r in all_processed_results})
    site_stats: Dict[str, Dict[str, Any]] = {}
    for site in sites_in_batch:
        s_rows = [r for r in all_processed_results if r["source_site"] == site]
        s_total = len(s_rows)
        s_has_cand = sum(1 for r in s_rows if r.get("candidate_count", 0) > 0)
        s_ok = sum(1 for r in s_rows if r["status"] in ("success", "early_terminated"))
        s_early = sum(1 for r in s_rows if r["status"] == "early_terminated")
        s_uniq = len({
            (r["platform"], r["product_id"])
            for r in s_rows
            if r["status"] in ("success", "early_terminated") and r.get("platform") and r.get("product_id")
        })
        err_counter = Counter(
            r["error_code"]
            for r in s_rows
            if r["status"] not in ("success", "early_terminated", "merged_into_parent")
        )
        site_stats[site] = {
            "scanned": s_total,
            "has_product_candidate": s_has_cand,
            "resolved_success": s_ok,
            "early_terminated_skipped_ai": s_early,
            "unique_products": s_uniq,
            "overall_resolution_rate_pct": round(s_ok / max(s_total, 1) * 100, 1),
            "candidate_resolution_rate_pct": round(s_ok / max(s_has_cand, 1) * 100, 1) if s_has_cand else 0.0,
            "failure_breakdown": dict(err_counter.most_common()),
        }

    global_err_counter = Counter(
        r["error_code"]
        for r in all_processed_results
        if r["status"] not in ("success", "early_terminated", "merged_into_parent")
    )
    plat_counter = Counter(
        r["platform"]
        for r in all_processed_results
        if r["status"] in ("success", "early_terminated") and r.get("platform")
    )

    with_publish_time = sum(1 for d in success_product_docs if d["deal_info"].get("publish_time"))
    with_tutorial_text = sum(1 for d in success_product_docs if len(d["deal_info"].get("tutorial_text") or "") >= 10)
    with_proof_images = sum(1 for d in success_product_docs if d["deal_info"].get("user_share_images"))
    with_aux_links = sum(1 for d in success_product_docs if d["deal_info"].get("auxiliary_links"))
    with_deal_price = sum(1 for d in success_product_docs if d["deal_info"].get("deal_price") is not None)
    avg_quality_score = round(
        sum(int(d["deal_info"].get("quality_score") or 0) for d in success_product_docs) / max(1, resolved_cnt),
        1,
    ) if resolved_cnt else 0.0

    stage_timings = getattr(cleaner, "last_batch_stage_timings", {}) or {}
    clean_report = {
        "cycle_num": cycle_num,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_formal_collection": repo.formal_collection_name,
        "formal_collection_status": {
            "formal_products_total_count": formal_total_now,
            "formal_deal_tip_products_count": formal_deal_total_now,
            "batch_upserted_new_products": formal_write_res["upserted"],
            "batch_modified_existing_products": formal_write_res["modified"] + early_resonance_modified,
            "batch_protected_high_score_products": formal_write_res.get("protected_high_score", 0),
        },
        "global_funnel": {
            "1_total_scanned_tips": total_scanned,
            "2_has_product_candidate_tips": has_candidate_cnt,
            "3_unique_product_resolved_tips": effective_resolved_cnt,
            "3a_ai2_audited_new_tips": resolved_cnt,
            "3b_early_terminated_skipped_ai_tips": early_terminated_cnt,
            "3c_merged_neighbor_tips": merged_cnt,
            "3d_low_score_filtered_tips": low_score_cnt,
            "4_deduplicated_unique_products_in_batch": unique_pids_in_batch,
            "avg_quality_score": avg_quality_score,
            "overall_resolution_rate_pct": round(effective_resolved_cnt / max(total_scanned, 1) * 100, 1),
            "candidate_resolution_rate_pct": round(effective_resolved_cnt / max(has_candidate_cnt, 1) * 100, 1) if has_candidate_cnt else 0.0,
            "cross_tip_duplication_rate_pct": round((1 - unique_pids_in_batch / max(effective_resolved_cnt, 1)) * 100, 1) if effective_resolved_cnt else 0.0,
            "remaining_pending_anchored_tips": max(0, len(pending_docs) - len(selected_docs)),
        },
        "resolved_by_platform": dict(plat_counter),
        "enrichment_retention_metrics": {
            "resolved_total": resolved_cnt,
            "retained_publish_time_count": with_publish_time,
            "retained_publish_time_pct": round(with_publish_time / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_tutorial_text_count": with_tutorial_text,
            "retained_tutorial_text_pct": round(with_tutorial_text / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_proof_or_guide_images_count": with_proof_images,
            "retained_proof_or_guide_images_pct": round(with_proof_images / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_auxiliary_coupon_or_filler_links_count": with_aux_links,
            "retained_auxiliary_coupon_or_filler_links_pct": round(with_aux_links / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
            "retained_deal_price_count": with_deal_price,
            "retained_deal_price_pct": round(with_deal_price / max(resolved_cnt, 1) * 100, 1) if resolved_cnt else 0.0,
        },
        "failure_taxonomy_breakdown": {
            code: {
                "count": cnt,
                "share_of_failures_pct": round(cnt / max(total_scanned - effective_resolved_cnt - merged_cnt, 1) * 100, 1),
            }
            for code, cnt in global_err_counter.most_common()
        },
        "by_source_site": site_stats,
        "engine_efficiency_metrics": {
            "batch_elapsed_seconds": batch_elapsed_s,
            "stage2_ai1_seconds": stage_timings.get("stage2_ai1_seconds", 0.0),
            "stage3_convert_seconds": stage_timings.get("stage3_convert_seconds", 0.0),
            "stage4_ai2_seconds": stage_timings.get("stage4_ai2_seconds", 0.0),
            "avg_ms_per_anchored_record": round(batch_elapsed_s * 1000 / max(has_candidate_cnt, 1), 1) if has_candidate_cnt else 0.0,
            "anchor_cache_hits": cleaner.stats_cache_hits,
            "early_terminated_pre_ai1": cleaner.stats_early_terminated_pre_ai1,
            "early_terminated_post_stage3": cleaner.stats_early_terminated_post_stage3,
            "ai_stage1_calls": cleaner.stats_ai1_calls,
            "ai_stage1_model_hits": cleaner.stats_ai1_model_hits,
            "ai_stage1_fallbacks": cleaner.stats_ai1_fallbacks,
            "ai_stage2_calls": cleaner.stats_ai2_calls,
            "ai_stage2_model_hits": cleaner.stats_ai2_model_hits,
            "ai_stage2_fallbacks": cleaner.stats_ai2_fallbacks,
        },
        "engineering_philosophy_manifest": ENGINEERING_PHILOSOPHY_MANIFEST,
        "attempted_dead_ends_manifest": CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST,
    }

    try:
        with open(LATEST_CLEAN_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(clean_report, f, ensure_ascii=False, indent=2)
        with open(CLEAN_OPS_HISTORY_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(clean_report, ensure_ascii=False) + "\n")
        with open(CLEAN_FAILED_SAMPLES_JSONL, "a", encoding="utf-8") as f:
            for item in failed_samples[:200]:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        with open(CLEAN_SUCCESS_PREVIEW_JSONL, "w", encoding="utf-8") as f:
            for doc in success_product_docs[:50]:
                f.write(json.dumps(doc, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("写入阶段二清洗报表文件异常: %s", e)

    gf = clean_report["global_funnel"]
    er = clean_report["enrichment_retention_metrics"]
    fc = clean_report["formal_collection_status"]
    em = clean_report["engine_efficiency_metrics"]
    plat_str = f"京东(jd)={plat_counter.get('jd', 0)} | 淘宝(taobao)={plat_counter.get('taobao', 0)} | 拼多多(pdd)={plat_counter.get('pdd', 0)}"
    top_global_errs = ", ".join(f"{k}:{v}" for k, v in global_err_counter.most_common(4)) or "无"
    lines = [
        "",
        "┌" + "─" * 98 + "┐",
        f"│ 🧼 【第 #{cycle_num} 轮 Stage 2~5：双阶段 AI + 并发转链 + 唯一商品早停 + 高分晋升 (`{repo.formal_collection_name}`) 报告】│",
        "├" + "─" * 98 + "┤",
        (
            f"│ • 全局转化漏斗 : 扫描Pending={gf['1_total_scanned_tips']}条 -> "
            f"含候选={gf['2_has_product_candidate_tips']}条 -> "
            f"有效命中={gf['3_unique_product_resolved_tips']}条 "
            f"(AI-2新审={resolved_cnt}条[均分{gf['avg_quality_score']}] + ⚡早停跳过AI={early_terminated_cnt}条) | 群聊合并={merged_cnt}条"
        ),
        (
            f"│ • 终审转化率   : 候选有效率={gf['candidate_resolution_rate_pct']}% | "
            f"总有效率={gf['overall_resolution_rate_pct']}% | "
            f"本批去重独立商品={gf['4_deduplicated_unique_products_in_batch']}个 | 剩余Pending={gf['remaining_pending_anchored_tips']}条"
        ),
        f"│ • 平台分布明细 : {plat_str}",
        (
            f"│ • 正式表入库   : 正式表 `{repo.formal_collection_name}` 本轮新增={fc['batch_upserted_new_products']}个, "
            f"更新共振={fc['batch_modified_existing_products']}个 (高分保护防覆盖={fc['batch_protected_high_score_products']}次) | "
            f"累计好价商品={fc['formal_deal_tip_products_count']}个"
        ),
        (
            f"│ • 引擎与AI指标 : 总耗={batch_elapsed_s}s (AI1={em['stage2_ai1_seconds']}s / 并发转链={em['stage3_convert_seconds']}s / AI2={em['stage4_ai2_seconds']}s) | "
            f"AI-1(直出={cleaner.stats_ai1_model_hits}) | AI-2(直出={cleaner.stats_ai2_model_hits}) | "
            f"早停累计={cleaner.stats_early_terminated_pre_ai1 + cleaner.stats_early_terminated_post_stage3} | 缓存命中={cleaner.stats_cache_hits}"
        ),
        (
            f"│ • 专有信息保留 : 爆料时间={er['retained_publish_time_pct']}% | "
            f"重构教程={er['retained_tutorial_text_pct']}% | "
            f"爆料价={er['retained_deal_price_pct']}% | "
            f"晒单/步骤图={er['retained_proof_or_guide_images_pct']}%({er['retained_proof_or_guide_images_count']}条) | "
            f"优惠券/凑单链={er['retained_auxiliary_coupon_or_filler_links_pct']}%({er['retained_auxiliary_coupon_or_filler_links_count']}条)"
        ),
        f"│ • 全局拦截分布 : {top_global_errs}",
        "├" + "─" * 98 + "┤",
        f"│ {'站点 (source_site)':<15} | {'扫描数':>6} | {'含候选数':>8} | {'有效命中':>8} | {'早停跳AI':>8} | {'有效率':>8} | {'独立商品':>6} | Top 拦截原因",
        "├" + "─" * 98 + "┤",
    ]
    for site, st in sorted(site_stats.items(), key=lambda x: x[1]["resolved_success"], reverse=True):
        top_err = ", ".join(f"{k}:{v}" for k, v in list(st["failure_breakdown"].items())[:2]) or "-"
        lines.append(
            f"│ {site:<15} | {st['scanned']:>6} | {st['has_product_candidate']:>8} | "
            f"{st['resolved_success']:>8} | {st['early_terminated_skipped_ai']:>8} | "
            f"{st['candidate_resolution_rate_pct']:>7.1f}% | "
            f"{st['unique_products']:>8} | {top_err}"
        )
    lines.append("└" + "─" * 98 + "┘")
    logger.info("\n".join(lines))

    return clean_report


# ==============================================================================
# 每轮综合运维拉取与清洗排障诊断报告生成器
# ==============================================================================
def build_and_log_cycle_ops_report(
    cycle_num: int,
    cycle_mode: str,
    fetch_elapsed_s: float,
    task_reports: List[Dict[str, Any]],
    cycle_error_paths: List[Dict[str, Any]],
    clean_report: Dict[str, Any],
    pool_summary: Dict[str, Any],
) -> Dict[str, Any]:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok_tasks = [t for t in task_reports if t["status"] == "success"]
    warn_tasks = [t for t in task_reports if t["status"] == "empty_warning"]
    fail_tasks = [t for t in task_reports if t["status"] == "error"]

    total_fetched = sum(t.get("fetched", 0) for t in task_reports)
    total_detail_fetched = sum(t.get("detail_fetched", 0) for t in task_reports)
    total_inserted = sum(t.get("inserted", 0) for t in task_reports)
    total_updated = sum(t.get("updated", 0) for t in task_reports)
    total_unique_anchor = sum(t.get("unique_anchor_count", 0) for t in task_reports)

    site_stats: Dict[str, Dict[str, Any]] = {}
    for t in task_reports:
        s_key = t.get("site_key") or "unknown"
        if s_key not in site_stats:
            site_stats[s_key] = {
                "site_key": s_key,
                "site_name": SITE_DISPLAY_NAMES.get(s_key, s_key),
                "tasks_total": 0,
                "tasks_ok": 0,
                "tasks_warn": 0,
                "tasks_fail": 0,
                "fetched": 0,
                "detail_fetched": 0,
                "inserted": 0,
                "updated": 0,
                "unique_anchor_count": 0,
                "detail_error_count": 0,
                "total_elapsed_ms": 0.0,
                "max_consecutive_failures": 0,
            }
        st = site_stats[s_key]
        st["tasks_total"] += 1
        if t["status"] == "success":
            st["tasks_ok"] += 1
        elif t["status"] == "empty_warning":
            st["tasks_warn"] += 1
        else:
            st["tasks_fail"] += 1
        st["fetched"] += t.get("fetched", 0)
        st["detail_fetched"] += t.get("detail_fetched", 0)
        st["inserted"] += t.get("inserted", 0)
        st["updated"] += t.get("updated", 0)
        st["unique_anchor_count"] += t.get("unique_anchor_count", 0)
        st["detail_error_count"] += t.get("detail_error_count", 0)
        st["total_elapsed_ms"] = round(st["total_elapsed_ms"] + t.get("elapsed_ms", 0.0), 1)
        st["max_consecutive_failures"] = max(
            st["max_consecutive_failures"], t.get("consecutive_failures", 0)
        )

    site_health_list: List[Dict[str, Any]] = []
    for s_key, st in site_stats.items():
        if st["tasks_fail"] > 0 and st["tasks_ok"] == 0:
            health_badge = "🔴站点异常"
        elif st["tasks_fail"] > 0 or st["tasks_warn"] > 0 or st["detail_error_count"] > 0:
            health_badge = "🟡部分告警"
        else:
            health_badge = "🟢健康正常"
        st["health_badge"] = health_badge
        st["avg_elapsed_ms"] = round(st["total_elapsed_ms"] / max(1, st["tasks_total"]), 1)
        denom = st["detail_fetched"] if st["detail_fetched"] > 0 else st["fetched"]
        st["unique_anchor_ratio"] = round(
            st["unique_anchor_count"] * 100.0 / max(1, denom), 1
        ) if denom > 0 else 0.0
        site_health_list.append(st)

    overall_health = (
        "🟢 全部正常 (HEALTHY)"
        if (not fail_tasks and not warn_tasks and not cycle_error_paths)
        else (
            "🔴 存在通道异常需关注 (ACTION_REQUIRED)"
            if any(t.get("consecutive_failures", 0) >= 3 for t in fail_tasks)
            else "🟡 存在轻微波动/单点告警 (DEGRADED_AUTO_RECOVERING)"
        )
    )

    cycle_denom = total_detail_fetched if total_detail_fetched > 0 else total_fetched
    ops_report = {
        "cycle_num": cycle_num,
        "generated_at": now_str,
        "cycle_mode": cycle_mode,
        "overall_health": overall_health,
        "fetch_elapsed_s": fetch_elapsed_s,
        "channels_scheduled": len(task_reports),
        "channels_ok": len(ok_tasks),
        "channels_warn": len(warn_tasks),
        "channels_fail": len(fail_tasks),
        "total_fetched": total_fetched,
        "total_detail_fetched": total_detail_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "cycle_unique_anchor_count": total_unique_anchor,
        "cycle_unique_anchor_ratio": round(total_unique_anchor * 100.0 / max(1, cycle_denom), 1),
        "pool_total_records": pool_summary.get("total_records", 0),
        "pool_unique_anchor_records": pool_summary.get("unique_product_anchor_records", 0),
        "pool_unique_anchor_ratio": pool_summary.get("unique_product_anchor_ratio", 0.0),
        "pool_cleaned_success_records": pool_summary.get("cleaned_success_records", 0),
        "pool_cleaned_failed_records": pool_summary.get("cleaned_failed_records", 0),
        "pool_pending_clean_records": pool_summary.get("pending_clean_records", 0),
        "formal_products_total": pool_summary.get("formal_products_total", 0),
        "formal_deal_products_count": pool_summary.get("formal_deal_products_count", 0),
        "site_health_matrix": site_health_list,
        "clean_stage_summary": clean_report,
        "error_paths_count": len(cycle_error_paths),
        "error_paths": cycle_error_paths,
    }

    try:
        with open(LATEST_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(ops_report, f, ensure_ascii=False, indent=2)
        with open(OPS_REPORT_HISTORY_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(ops_report, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("保存每轮运维报告文件失败: %s", e)

    lines = [
        "",
        "┌" + "─" * 98 + "┐",
        f"│ 📋 【第 #{cycle_num} 轮 阶段一：多站点拉取健康度与排障诊断报告】 ({now_str}) Mode={cycle_mode:<14} │",
        "├" + "─" * 98 + "┤",
        f"│ • 整体健康度 : {overall_health:<80} │",
        (
            f"│ • 本轮通道数 : 调度={len(task_reports)}个 "
            f"(🟢成功={len(ok_tasks)} | 🟡空列表告警={len(warn_tasks)} | 🔴失败={len(fail_tasks)}) "
            f"| 拉取耗时={fetch_elapsed_s:.2f}s"
        ),
        (
            f"│ • 本轮数据量 : 拉取={total_fetched}条 (新抓详情={total_detail_fetched}条) "
            f"| 入初步表新增={total_inserted}条, 更新={total_updated}条 "
            f"| 含唯一商品锚点={total_unique_anchor}条 ({ops_report['cycle_unique_anchor_ratio']}%)"
        ),
        (
            f"│ • 双表总状态 : 初步表 `deal_raw_tips`={ops_report['pool_total_records']}条 "
            f"(已清洗入库={ops_report['pool_cleaned_success_records']}, 已拦截={ops_report['pool_cleaned_failed_records']}, 待清洗={ops_report['pool_pending_clean_records']}) "
            f"| 正式表 `products` 线报商品={ops_report['formal_deal_products_count']}个"
        ),
        "├" + "─" * 98 + "┤",
        "│ 【一、各来源站点本轮拉取运行状态矩阵】                                                           │",
    ]
    for st in site_health_list:
        lines.append(
            f"│   {st['health_badge']} {st['site_key']:<12} | "
            f"通道:{st['tasks_ok']}/{st['tasks_total']}正常 | "
            f"拉取:{st['fetched']:>4}条(新详情:{st['detail_fetched']:>3}) | "
            f"新增:{st['inserted']:>3} 更新:{st['updated']:>4} | "
            f"唯一锚点:{st['unique_anchor_count']:>4}条({st['unique_anchor_ratio']:>5.1f}%) | "
            f"均耗:{st['avg_elapsed_ms']:>6.1f}ms | 异常路径:{st['tasks_fail'] + st['tasks_warn'] + st['detail_error_count']}个"
        )

    lines.append("├" + "─" * 98 + "┤")
    lines.append("│ 【二、本轮拉取错误路径记录与运维人员处理指南 (Error Paths & Ops Action Guide)】                  │")
    if not cycle_error_paths:
        lines.append("│   ✅ 本轮全部已调度列表与详情页 URL 路径 100% 正常响应，0 错误路径，无需任何运维处理！             │")
    else:
        for idx, err in enumerate(cycle_error_paths, 1):
            sev = "🔴[列表/通道失败]" if err["stage"] == "list_fetch" and err["severity"] == "error" else "🟡[详情/空列表告警]"
            lines.append(
                f"│   {idx}. {sev} 频道=[{err['task_name']}] | 阶段={err['stage']} | "
                f"连续异常={err.get('consecutive_failures', 1)}轮 | 状态码={err.get('http_status')} | 类型={err['error_type']}"
            )
            lines.append(f"│      🔗 错误路径(URL) : {err['error_url']}")
            if err.get("error_message"):
                lines.append(f"│      ⚠️ 错误详情      : {err['error_message'][:120]}")
            lines.append(f"│      🛠️ 运维处理建议  : {err['ops_advice']}")
    lines.append("└" + "─" * 98 + "┘")

    logger.info("\n".join(lines))
    return ops_report


# ==============================================================================
# 阶段一（Producer：多源拉取入 `deal_raw_tips`）与阶段二~五（Consumer：清洗入 `products`）编排
# ==============================================================================
def run_fetch_stage(
    repo: DealRawPoolRepository,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: int = 20,
    run_all_tiers: bool = False,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
    latest_clean_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    执行单轮【阶段一（Producer）：多站点好价拉取入初步表 `deal_raw_tips`】：
    - T0 核心源（24 个高可靠任务）全量覆盖 8 大高可靠站点与高转化拼多多专属源，每轮必跑；
    - T1 扩充源（27 个高可靠任务）涵盖细分采集群、拼多多扩展翻页与豆瓣/赚客吧/酷安专组，在 `--all-channels` 或偶数轮运行。
    - 已永久放弃 `gwdang`（购物党 100% 纯数字 SKU 0% 转化）与 `xianbao_fun/category_man`（慢慢买 `discuxiao_` 加密跳板壳）。
    """
    fetch_start = time.perf_counter()
    include_medium = run_all_tiers or (cycle_num % 2 == 0)
    cycle_mode = "FULL_51_RELIABLE_CHANNELS" if include_medium else "T0_24_ALL_8_RELIABLE_SITES"
    if channel_health_tracker is None:
        channel_health_tracker = {}

    xianbao_done = repo.get_existing_detailed_ids("xianbao_fun", limit=8000)
    guangdiu_done = repo.get_existing_detailed_ids("guangdiu", limit=4000)
    tuan0818_done = repo.get_existing_detailed_ids("0818tuan", limit=3000)
    bacaoo_done = repo.get_existing_detailed_ids("bacaoo", limit=2000)
    iehou_done = repo.get_existing_detailed_ids("iehou", limit=2000)

    # ---------------- T0 高频核心源（每轮必跑：24 个高产频道，100% 覆盖全部 8 大高可靠站点 + 四大拼多多专属源） ----------------
    tasks: List[Tuple[str, str, str, str, Callable[[], Dict[str, Any]], float]] = [
        (
            "好单库_京东实时采集群",
            "haodanku",
            "T0_high_freq",
            "https://www.haodanku.com/Index/get_wire_report_items?category_type=10004&page_no=1",
            lambda: fetch_haodanku_wire_report(category_type=10004, page_no=1, page_size=25, session=session),
            0.3,
        ),
        (
            "好单库_淘宝实时采集群",
            "haodanku",
            "T0_high_freq",
            "https://www.haodanku.com/Index/get_wire_report_items?category_type=10003&page_no=1",
            lambda: fetch_haodanku_wire_report(category_type=10003, page_no=1, page_size=25, session=session),
            0.3,
        ),
        (
            "好单库_淘宝百亿补贴群",
            "haodanku",
            "T0_high_freq",
            "https://www.haodanku.com/Index/get_wire_report_items?category_type=10005&page_no=1",
            lambda: fetch_haodanku_wire_report(category_type=10005, page_no=1, page_size=25, session=session),
            0.3,
        ),
        (
            "好单库_全品类综合群",
            "haodanku",
            "T0_high_freq",
            "https://www.haodanku.com/Index/get_wire_report_items?category_type=0&page_no=1",
            lambda: fetch_haodanku_wire_report(category_type=0, page_no=1, page_size=25, session=session),
            0.3,
        ),
        (
            "折淘客_京东采集群汇总",
            "zhetaoke",
            "T0_high_freq",
            "https://www.zhetaoke.com/xianbao.aspx?plat=2&page=1",
            lambda: fetch_zhetaoke_xianbao_groups(plat="2", page=1, session=session),
            0.4,
        ),
        (
            "折淘客_淘宝采集群汇总",
            "zhetaoke",
            "T0_high_freq",
            "https://www.zhetaoke.com/xianbao.aspx?plat=1&page=1",
            lambda: fetch_zhetaoke_xianbao_groups(plat="1", page=1, session=session),
            0.4,
        ),
        (
            "折淘客_拼多多采集群汇总",
            "zhetaoke",
            "T0_high_freq",
            "https://www.zhetaoke.com/xianbao.aspx?plat=3&page=1",
            lambda: fetch_zhetaoke_xianbao_groups(plat="3", page=1, session=session),
            0.4,
        ),
        (
            "折淘客_150群全网综合流",
            "zhetaoke",
            "T0_high_freq",
            "https://www.zhetaoke.com/xianbao.aspx?plat=&page=1",
            lambda: fetch_zhetaoke_xianbao_groups(plat="", page=1, session=session),
            0.4,
        ),
        (
            "折淘客_新京东带主图群",
            "zhetaoke",
            "T0_high_freq",
            "https://www.zhetaoke.com/xianbao.aspx?id=10000030&page=1",
            lambda: fetch_zhetaoke_xianbao_groups(group_id="10000030", page=1, session=session),
            0.4,
        ),
        (
            "折淘客_云线报群9京东",
            "zhetaoke",
            "T0_high_freq",
            "https://www.zhetaoke.com/xianbao.aspx?id=1067573294&page=1",
            lambda: fetch_zhetaoke_xianbao_groups(group_id="1067573294", page=1, session=session),
            0.4,
        ),
        (
            "逛丢_全网与值得买明文流",
            "guangdiu",
            "T0_high_freq",
            "https://guangdiu.com/",
            lambda: fetch_guangdiu_deals(
                channel="latest",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=guangdiu_done,
            ),
            0.5,
        ),
        (
            "逛丢_拼多多专属好价流",
            "guangdiu",
            "T0_high_freq",
            "https://guangdiu.com/cate.php?m=%E6%8B%BC%E5%A4%9A%E5%A4%9A",
            lambda: fetch_guangdiu_deals(
                channel="pdd",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=guangdiu_done,
            ),
            0.5,
        ),
        (
            "选单网_快抢群发素材_全天",
            "xuandan",
            "T0_high_freq",
            "https://www.xuandan.com/NewHome/msKqList?page=1&hour_type=",
            lambda: fetch_xuandan_deals(hour_type="", page=1, session=session),
            0.4,
        ),
        # 🚨【严禁恢复：购物党_最新好价折扣 (gwdang / 0-latest) 已永久剔除】
        # 原因：实测 320 条购物党记录中 319 条转链失败（99.7% 失败率），因其全站主商品 100% 为纯数字 SKU 裸链
        # (`item.jd.com/{数字}.html` / `detail.tmall.com/item.htm?id={数字}`)，三大联盟官方接口 100% 拒识纯数字 ID，
        # 且严禁使用脆弱的“商品全称反向模糊搜索”，有效产出为 0%，请勿以为遗漏而重新添加！
        (
            "0818团_实战凑单线报_P1",
            "0818tuan",
            "T0_high_freq",
            "http://www.0818tuan.com/list-1-0.html",
            lambda: fetch_0818tuan_deals(
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=tuan0818_done,
            ),
            0.5,
        ),
        (
            "拔草哦_国内优惠口令",
            "bacaoo",
            "T0_high_freq",
            "https://youhui.bacaoo.com/",
            lambda: fetch_bacaoo_deals(
                channel="youhui",
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=bacaoo_done,
            ),
            0.5,
        ),
        (
            "拔草哦_拼多多商城专区",
            "bacaoo",
            "T0_high_freq",
            "https://youhui.bacaoo.com/mall/1827",
            lambda: fetch_bacaoo_deals(
                channel="pdd",
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=bacaoo_done,
            ),
            0.5,
        ),
        (
            "网猴线报_24小时热榜",
            "iehou",
            "T0_high_freq",
            "https://www.iehou.com/index/index/dayhot.html",
            lambda: fetch_iehou_deals(
                mode="dayhot",
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=iehou_done,
            ),
            0.5,
        ),
        (
            "线报酷_1小时最热榜",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/plus/json/push.json",
            lambda: fetch_xianbao_fun_deals(
                channel="hot_1h",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
        (
            "线报酷_好单线报热榜",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/Category-haodan/hot.html",
            lambda: fetch_xianbao_fun_deals(
                channel="haodan_hot",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
        (
            "线报酷_好单实时聚合流",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/category-haodan/",
            lambda: fetch_xianbao_fun_deals(
                channel="category_haodan",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
        (
            "线报酷_好单拼多多专属流",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/category-haodan-pdd/",
            lambda: fetch_xianbao_fun_deals(
                channel="category_haodan_pdd",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
        (
            "线报酷_微博好价热榜",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/Category-weibo/hot.html",
            lambda: fetch_xianbao_fun_deals(
                channel="weibo_hot",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
        (
            "线报酷_豆瓣买组热榜",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/Category-douban/hot.html",
            lambda: fetch_xianbao_fun_deals(
                channel="douban_hot",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
        (
            "线报酷_全网实时最新流",
            "xianbao_fun",
            "T0_high_freq",
            "https://new.xianbao.fun/",
            lambda: fetch_xianbao_fun_deals(
                channel="latest",
                page=1,
                fetch_detail=True,
                max_details=max_new_details_per_channel,
                session=session,
                skip_detail_ids=xianbao_done,
            ),
            0.5,
        ),
    ]

    if include_medium:
        tasks.extend([
            # ---------------- T1 中频扩充源（27 个高可靠细分采集群、拼多多扩充翻页、豆瓣/赚客吧/酷安频道） ----------------
            (
                "好单库_飞猪旅行生活券群",
                "haodanku",
                "T1_med_freq",
                "https://www.haodanku.com/Index/get_wire_report_items?category_type=10367&page_no=1",
                lambda: fetch_haodanku_wire_report(category_type=10367, page_no=1, page_size=25, session=session),
                0.3,
            ),
            (
                "好单库_福利好价清单",
                "haodanku",
                "T1_med_freq",
                "https://www.haodanku.com/Weal/get_weal_list?page_no=1",
                lambda: fetch_haodanku_weal_list(page_no=1, page_size=25, session=session),
                0.4,
            ),
            (
                "折淘客_云线报群5综合",
                "zhetaoke",
                "T1_med_freq",
                "https://www.zhetaoke.com/xianbao.aspx?id=725556337&page=1",
                lambda: fetch_zhetaoke_xianbao_groups(group_id="725556337", page=1, session=session),
                0.4,
            ),
            (
                "折淘客_优质猫超线报群",
                "zhetaoke",
                "T1_med_freq",
                "https://www.zhetaoke.com/xianbao.aspx?id=10000072&page=1",
                lambda: fetch_zhetaoke_xianbao_groups(group_id="10000072", page=1, session=session),
                0.4,
            ),
            (
                "折淘客_宝妈羊毛购物群3",
                "zhetaoke",
                "T1_med_freq",
                "https://www.zhetaoke.com/xianbao.aspx?id=10000176&page=1",
                lambda: fetch_zhetaoke_xianbao_groups(group_id="10000176", page=1, session=session),
                0.4,
            ),
            (
                "折淘客_富贵淘宝京东4群",
                "zhetaoke",
                "T1_med_freq",
                "https://www.zhetaoke.com/xianbao.aspx?id=10000128&page=1",
                lambda: fetch_zhetaoke_xianbao_groups(group_id="10000128", page=1, session=session),
                0.4,
            ),
            (
                "折淘客_咚咚抢整点爆款榜",
                "zhetaoke",
                "T1_med_freq",
                "https://www.zhetaoke.com/list/dongdong.html",
                lambda: fetch_zhetaoke_rank_list(mode="dongdong", page=1, session=session),
                0.4,
            ),
            (
                "折淘客_全天实时热销爆款榜",
                "zhetaoke",
                "T1_med_freq",
                "https://www.zhetaoke.com/list/love_1_0_1.html",
                lambda: fetch_zhetaoke_rank_list(mode="realtime_hot", page=1, session=session),
                0.4,
            ),
            (
                "逛丢_全网与值得买明文流_P2",
                "guangdiu",
                "T1_med_freq",
                "https://guangdiu.com/index.php?p=2",
                lambda: fetch_guangdiu_deals(
                    channel="latest",
                    page=2,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=guangdiu_done,
                ),
                0.5,
            ),
            (
                "逛丢_拼多多专属好价流_P2",
                "guangdiu",
                "T1_med_freq",
                "https://guangdiu.com/cate.php?m=%E6%8B%BC%E5%A4%9A%E5%A4%9A&p=2",
                lambda: fetch_guangdiu_deals(
                    channel="pdd",
                    page=2,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=guangdiu_done,
                ),
                0.5,
            ),
            (
                "选单网_快抢群发素材_整点",
                "xuandan",
                "T1_med_freq",
                "https://www.xuandan.com/NewHome/msKqList?page=1&hour_type=1",
                lambda: fetch_xuandan_deals(hour_type="1", page=1, session=session),
                0.4,
            ),
            # 🚨【严禁恢复：购物党 T1 频道 (0-lowest / 0-s_lowest / 0-lower_11) 已永久剔除】
            # 原因同上：全站主商品 100% 为纯数字 SKU，官方转链 API 100% 拒识，实测 99.7% 失败率（详见顶部 EXCLUDED_SITES_AND_CHANNELS_MANIFEST）。
            (
                "0818团_实战凑单线报_P2",
                "0818tuan",
                "T1_med_freq",
                "http://www.0818tuan.com/list-1-1.html",
                lambda: fetch_0818tuan_deals(
                    page=2,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=tuan0818_done,
                ),
                0.5,
            ),
            (
                "线报酷_好单拼多多专属流_P2",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-haodan-pdd/page/2/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_haodan_pdd",
                    page=2,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_微博拼多多专属流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-weibo-pdd/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_weibo_pdd",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_微博实时聚合流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-weibo/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_weibo",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_豆瓣全组实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-douban/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_豆瓣买组实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-douban-maizu/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban_maizu",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_豆瓣拼组实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-douban-pinzu/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban_pinzu",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_豆瓣狗组实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-douban-gouzu/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban_gouzu",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_豆瓣爱猫澡盆流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-douban-maobathtub/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban_maobathtub",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_豆瓣爱猫生活流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-douban-maolife/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_douban_maolife",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_赚客吧实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-zuankeba/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_zuankeba",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_赚客吧热榜",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/Category-zuankeba/hot.html",
                lambda: fetch_xianbao_fun_deals(
                    channel="zuankeba_hot",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_新赚吧实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-xinzuanba/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_xinzuanba",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_新赚吧热榜",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/Category-xinzuanba/hot.html",
                lambda: fetch_xianbao_fun_deals(
                    channel="xinzuanba_hot",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_酷安实时流",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-kuan/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_kuan",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            (
                "线报酷_酷安热榜",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/Category-kuan/hot.html",
                lambda: fetch_xianbao_fun_deals(
                    channel="kuan_hot",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
            # 🚨【严禁恢复：线报酷_慢友线报聚合 (xianbao_fun / category_man) 已永久剔除】
            # 原因：实测 90 条记录中 87 条失败（97.8% 失败率），底层外链全为慢慢买 `cu.manmanbuy.com/discuxiao_{id}.aspx`
            # 前端加密跳板壳，无明文商品推广链或口令，请勿以为遗漏而重新添加！
        ])

    total_inserted = 0
    total_updated = 0
    total_fetched = 0
    task_reports: List[Dict[str, Any]] = []
    cycle_error_paths: List[Dict[str, Any]] = []

    logger.info("================ 开始第 #%d 轮阶段一：多站点好价拉取 (共 %d 个规范频道任务) ================", cycle_num, len(tasks))

    for task_name, site_key, freq_tier, default_url, fn, pause_sec in tasks:
        t0 = time.perf_counter()
        ch_state = channel_health_tracker.setdefault(
            task_name,
            {"consecutive_failures": 0, "total_runs": 0, "total_errors": 0, "last_error_url": ""},
        )
        ch_state["total_runs"] += 1

        try:
            res = fn()
            req_url = str(res.get("request_url") or default_url)
            http_status = res.get("http_status")
            records = res.get("records") or []
            detail_errors = res.get("detail_errors") or []

            for r in records:
                if r.get("has_full_detail") and r.get("source_item_id"):
                    sid = str(r["source_item_id"])
                    if site_key == "xianbao_fun":
                        xianbao_done.add(sid)
                    elif site_key == "guangdiu":
                        guangdiu_done.add(sid)
                    elif site_key == "0818tuan":
                        tuan0818_done.add(sid)
                    elif site_key == "bacaoo":
                        bacaoo_done.add(sid)
                    elif site_key == "iehou":
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

            for d_err in detail_errors:
                err_item = {
                    "occurred_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cycle_num": cycle_num,
                    "site_key": site_key,
                    "task_name": task_name,
                    "stage": "detail_fetch",
                    "severity": "warning",
                    "error_url": str(d_err.get("error_url") or req_url),
                    "item_id": str(d_err.get("item_id") or ""),
                    "http_status": d_err.get("http_status"),
                    "error_type": str(d_err.get("error_type") or "DetailFetchError"),
                    "error_message": str(d_err.get("error") or ""),
                    "consecutive_failures": 1,
                    "ops_advice": get_ops_troubleshooting_advice(
                        site_key=site_key,
                        http_status=d_err.get("http_status"),
                        error_type=str(d_err.get("error_type") or "DetailFetchError"),
                        fetched_count=cnt,
                        consecutive_failures=1,
                    ),
                }
                cycle_error_paths.append(err_item)
                logger.warning(
                    "  ⚠️ [%-22s] 详情页子路径异常 | URL=%s | 类型=%s | 详情=%s",
                    task_name,
                    err_item["error_url"],
                    err_item["error_type"],
                    err_item["error_message"],
                )

            if http_status == 200 and cnt > 0:
                ch_state["consecutive_failures"] = 0
                task_status = "success"
                logger.info(
                    "  ✅ [%-22s] 拉取=%3d条 (新抓详情=%2d, 详情异常=%d) | 入库新增=%3d 更新=%3d | 含唯一商品锚点=%3d条 | 耗时=%6.1fms",
                    task_name, cnt, det_cnt, len(detail_errors), ins, upd, unique_cnt, cost_ms,
                )
            else:
                ch_state["consecutive_failures"] += 1
                ch_state["total_errors"] += 1
                ch_state["last_error_url"] = req_url
                is_hard_error = http_status != 200
                task_status = "error" if is_hard_error else "empty_warning"
                err_type = f"HTTP_{http_status}" if is_hard_error else "EmptyRecordsReturned"
                advice = get_ops_troubleshooting_advice(
                    site_key=site_key,
                    http_status=http_status,
                    error_type=err_type,
                    fetched_count=cnt,
                    consecutive_failures=ch_state["consecutive_failures"],
                )
                err_item = {
                    "occurred_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cycle_num": cycle_num,
                    "site_key": site_key,
                    "task_name": task_name,
                    "stage": "list_fetch",
                    "severity": "error" if is_hard_error else "warning",
                    "error_url": req_url,
                    "http_status": http_status,
                    "error_type": err_type,
                    "error_message": f"HTTP={http_status}, 解析记录数={cnt}, 耗时={cost_ms}ms",
                    "consecutive_failures": ch_state["consecutive_failures"],
                    "ops_advice": advice,
                }
                cycle_error_paths.append(err_item)
                logger.warning(
                    "  ⚠️ [%-22s] 列表返回为空或状态码异常 (连续=%d轮) | 错误路径=%s | HTTP=%s | 耗时=%.1fms | 建议=%s",
                    task_name,
                    ch_state["consecutive_failures"],
                    req_url,
                    http_status,
                    cost_ms,
                    advice,
                )

            task_reports.append({
                "task_name": task_name,
                "site_key": site_key,
                "tier": freq_tier,
                "request_url": req_url,
                "http_status": http_status,
                "status": task_status,
                "fetched": cnt,
                "detail_fetched": det_cnt,
                "detail_error_count": len(detail_errors),
                "inserted": ins,
                "updated": upd,
                "unique_anchor_count": unique_cnt,
                "consecutive_failures": ch_state["consecutive_failures"],
                "elapsed_ms": cost_ms,
            })
        except Exception as e:
            cost_ms = round((time.perf_counter() - t0) * 1000, 1)
            ch_state["consecutive_failures"] += 1
            ch_state["total_errors"] += 1
            ch_state["last_error_url"] = default_url
            err_type = type(e).__name__
            advice = get_ops_troubleshooting_advice(
                site_key=site_key,
                http_status=None,
                error_type=err_type,
                fetched_count=0,
                consecutive_failures=ch_state["consecutive_failures"],
            )
            err_item = {
                "occurred_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cycle_num": cycle_num,
                "site_key": site_key,
                "task_name": task_name,
                "stage": "list_fetch",
                "severity": "error",
                "error_url": default_url,
                "http_status": None,
                "error_type": err_type,
                "error_message": str(e)[:240],
                "consecutive_failures": ch_state["consecutive_failures"],
                "ops_advice": advice,
            }
            cycle_error_paths.append(err_item)
            logger.warning(
                "  ❌ [%-22s] 拉取异常 (连续=%d轮, 耗时=%.1fms) | 错误路径=%s | 异常=%s: %s | 运维建议=%s",
                task_name,
                ch_state["consecutive_failures"],
                cost_ms,
                default_url,
                err_type,
                e,
                advice,
            )
            task_reports.append({
                "task_name": task_name,
                "site_key": site_key,
                "tier": freq_tier,
                "request_url": default_url,
                "http_status": None,
                "status": "error",
                "fetched": 0,
                "detail_fetched": 0,
                "detail_error_count": 0,
                "inserted": 0,
                "updated": 0,
                "unique_anchor_count": 0,
                "consecutive_failures": ch_state["consecutive_failures"],
                "error_type": err_type,
                "error": str(e)[:240],
                "elapsed_ms": cost_ms,
            })

        if pause_sec > 0:
            time.sleep(pause_sec)

    append_error_records_jsonl(cycle_error_paths)
    fetch_elapsed_s = round(time.perf_counter() - fetch_start, 2)

    summary = repo.generate_pool_summary()
    try:
        with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    ops_report = build_and_log_cycle_ops_report(
        cycle_num=cycle_num,
        cycle_mode=cycle_mode,
        fetch_elapsed_s=fetch_elapsed_s,
        task_reports=task_reports,
        cycle_error_paths=cycle_error_paths,
        clean_report=latest_clean_report or {},
        pool_summary=summary,
    )

    return {
        "cycle_num": cycle_num,
        "cycle_mode": cycle_mode,
        "fetch_elapsed_s": fetch_elapsed_s,
        "total_fetched": total_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "tasks": task_reports,
        "error_paths": cycle_error_paths,
        "ops_report": ops_report,
        "pool_summary": summary,
    }


def run_single_cycle(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: int = 20,
    run_all_tiers: bool = False,
    clean_limit: int = 300,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    执行单轮完整【阶段一：拉取入初步表】+【阶段二~五：清洗入正式表】同步闭环（供 `--once` 或单轮测试调用）。
    """
    fetch_res = run_fetch_stage(
        repo=repo,
        session=session,
        cycle_num=cycle_num,
        max_new_details_per_channel=max_new_details_per_channel,
        run_all_tiers=run_all_tiers,
        channel_health_tracker=channel_health_tracker,
    )
    clean_report = run_clean_stage(
        repo=repo,
        cleaner=cleaner,
        cycle_num=cycle_num,
        clean_limit=clean_limit,
    )
    summary = repo.generate_pool_summary()
    try:
        with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        if os.path.exists(LATEST_OPS_REPORT_PATH):
            with open(LATEST_OPS_REPORT_PATH, "r", encoding="utf-8") as f:
                ops_rep = json.load(f)
            ops_rep["clean_stage_summary"] = clean_report
            ops_rep["pool_cleaned_success_records"] = summary.get("cleaned_success_records", 0)
            ops_rep["pool_cleaned_failed_records"] = summary.get("cleaned_failed_records", 0)
            ops_rep["pool_pending_clean_records"] = summary.get("pending_clean_records", 0)
            ops_rep["formal_products_total"] = summary.get("formal_products_total", 0)
            ops_rep["formal_deal_products_count"] = summary.get("formal_deal_products_count", 0)
            with open(LATEST_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
                json.dump(ops_rep, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {
        **fetch_res,
        "clean_report": clean_report,
        "pool_summary": summary,
    }


def print_stats_report(repo: DealRawPoolRepository):
    """在终端打印直观的初步表 (`deal_raw_tips`) 与正式表 (`products`) 全景统计报告。"""
    summary = repo.generate_pool_summary()
    print("\n" + "=" * 98)
    print(f"📊 好价线报初步表 (`{summary['raw_collection']}`) -> 正式表 (`{summary['formal_collection']}`) 全景统计报告 ({summary['generated_at']})")
    print("=" * 98)
    print(
        f"  • 初步表累计去重记录数     : {summary['total_records']} 条 "
        f"(含完整详情: {summary['full_detail_records']} 条, 含唯一商品硬锚点: {summary['unique_product_anchor_records']} 条 / {summary['unique_product_anchor_ratio']}%)"
    )
    print(
        f"  • 初步表清洗状态分布       : 已解析入库(cleaned_success)={summary['cleaned_success_records']} 条 | "
        f"已识别拦截(cleaned_failed)={summary['cleaned_failed_records']} 条 | 待清洗(pending)={summary['pending_clean_records']} 条"
    )
    print(
        f"  • 正式表 `{summary['formal_collection']}` 商品统计 : 累计沉淀好价线报唯一商品={summary['formal_deal_products_count']} 个 "
        f"(正式表全库商品总数={summary['formal_products_total']} 个)"
    )
    print(f"  • 清洗拦截错误码分布       : {json.dumps(summary['by_clean_error_code'], ensure_ascii=False)}")
    print("-" * 98)
    print("  • 纳入监控的 8 大高可靠站点入池与清洗入库明细 :")
    for s in summary.get("by_source_site") or []:
        cnt = max(1, s["count"])
        u_cnt = s.get("unique_anchor_count", 0)
        ok_cnt = s.get("cleaned_success_count", 0)
        fail_cnt = s.get("cleaned_failed_count", 0)
        pend_cnt = s.get("pending_count", 0)
        print(
            f"    - {str(s['_id']):<14} : 初步表={s['count']:>4}条 | "
            f"含锚点={u_cnt:>4}条 ({u_cnt * 100.0 / cnt:>5.1f}%) | "
            f"已入正式表={ok_cnt:>4}条 | 已拦截={fail_cnt:>4}条 | 待清洗={pend_cnt:>4}条"
        )
    print("-" * 98)
    print(f"  • 核心工程准则备忘 ({len(ENGINEERING_PHILOSOPHY_MANIFEST)} 条铁律) :")
    for pk, pv in ENGINEERING_PHILOSOPHY_MANIFEST.items():
        print(f"    🧭 {pk}: {pv[:110]}...")
    print("-" * 98)
    print(f"  • 采集阶段已尝试并剔除的 {len(EXCLUDED_SITES_AND_CHANNELS_MANIFEST)} 个站点/频道备忘 (严禁重复踩坑) :")
    for ex_name, ex_reason in EXCLUDED_SITES_AND_CHANNELS_MANIFEST.items():
        print(f"    ❌ {ex_name}: {ex_reason[:110]}...")
    print("-" * 98)
    print(f"  • 清洗阶段历史探索与回归极简基线备忘 ({len(CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST)} 条) :")
    for de_key, de_desc in CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST.items():
        print(f"    🛑 {de_key}: {de_desc[:110]}...")
    print("=" * 98 + "\n")


def print_latest_ops_report():
    """读取并打印最近一轮的综合拉取与清洗运维报告。"""
    print("\n" + "=" * 98)
    print("🛠️ 最近一轮好价拉取与唯一商品清洗运维诊断报告 (Ops Fetch & Clean Report)")
    print("=" * 98)
    if os.path.exists(LATEST_OPS_REPORT_PATH):
        try:
            with open(LATEST_OPS_REPORT_PATH, "r", encoding="utf-8") as f:
                rep = json.load(f)
            print(f"  • 报告轮次/时间 : 第 #{rep.get('cycle_num')} 轮 ({rep.get('generated_at')}) | 模式={rep.get('cycle_mode')}")
            print(f"  • 整体健康度    : {rep.get('overall_health')}")
            print(
                f"  • 阶段一(拉取)  : 调度={rep.get('channels_scheduled')}个 "
                f"(🟢正常={rep.get('channels_ok')} | 🟡空列表={rep.get('channels_warn')} | 🔴失败={rep.get('channels_fail')}) "
                f"| 新增={rep.get('total_inserted')} 更新={rep.get('total_updated')} | 耗时={rep.get('fetch_elapsed_s')}s"
            )
            cl = rep.get("clean_stage_summary") or {}
            gf = cl.get("global_funnel") or {}
            fc = cl.get("formal_collection_status") or {}
            if gf:
                print(
                    f"  • 阶段二(清洗)  : 扫描={gf.get('1_total_scanned_tips')}条 -> "
                    f"含候选数={gf.get('2_has_product_candidate_tips')}条 -> "
                    f"有效命中={gf.get('3_unique_product_resolved_tips')}条 "
                    f"(AI新审={gf.get('3a_ai2_audited_new_tips', 0)} + 早停={gf.get('3b_early_terminated_skipped_ai_tips', 0)}) -> "
                    f"正式表新增={fc.get('batch_upserted_new_products')} 更新={fc.get('batch_modified_existing_products')}"
                )
        except Exception as ex:
            print(f"  读取 {LATEST_OPS_REPORT_PATH} 失败: {ex}")
    else:
        print(f"  尚未生成 `{LATEST_OPS_REPORT_PATH}`，请先运行至少一轮 (`--once` 或定时守护)。")
    print("=" * 98 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="好价线报与采集群定时拉取 (`deal_raw_tips`) + 唯一商品清洗入正式表 (`products`) 一体化守护脚本"
    )
    parser.add_argument(
        "--collection",
        type=str,
        default=DEFAULT_RAW_COLLECTION_NAME,
        help=f"MongoDB 初步缓冲池集合名称（默认: {DEFAULT_RAW_COLLECTION_NAME}）",
    )
    parser.add_argument(
        "--formal-collection",
        type=str,
        default=DEFAULT_FORMAL_PRODUCTS_COLLECTION,
        help=f"MongoDB 正式商品表集合名称（默认: {DEFAULT_FORMAL_PRODUCTS_COLLECTION}）",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="启动前清空本功能涉及的全部数据库数据 (`deal_raw_tips` + `products` 线报商品) 与全部日志统计",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=180,
        help="持续定时模式下每轮拉取间隔秒数（默认: 180 秒，即 3 分钟）",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="最大拉取执行轮数（0 表示无限持续运行）",
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
        default=15,
        help="单频道每轮最多新抓取详情页条数（默认: 15）",
    )
    parser.add_argument(
        "--clean-limit",
        type=int,
        default=300,
        help="每批阶段二~五最多清洗核验的 pending 记录条数（默认: 300）",
    )
    parser.add_argument(
        "--ai-batch-size",
        type=int,
        default=10,
        help="单次 AI 请求打包处理的线报条数（默认: 10，兼顾低调用次数与防 8k Token 输出截断）",
    )
    parser.add_argument(
        "--ai-workers",
        type=int,
        default=20,
        help="Stage 2 / Stage 4 AI 并发请求线程数（默认: 20）",
    )
    parser.add_argument(
        "--convert-workers",
        type=int,
        default=10,
        help="Stage 3 官方转链并发线程数（默认: 10）",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="强制使用串行模式（先拉取后清洗）；默认在持续守护模式下启用【拉取线程 + 清洗线程】双线程并行",
    )
    parser.add_argument(
        "--all-channels",
        action="store_true",
        help="强制每轮跑满全部 51 个高可靠频道（默认奇数轮跑 24 个 T0 核心频道，偶数轮跑满 51 个频道）",
    )
    parser.add_argument(
        "--enable-ai",
        action="store_true",
        help="保留兼容参数（当前架构默认全量启用双阶段 AI + 官方转链核验）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="立即执行完整一轮【阶段一拉取 + 阶段二清洗入正式表】并输出全套运维与统计报告后退出",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="仅执行阶段二~五：对初步表 `deal_raw_tips` 中现有的 pending 记录进行清洗并写入正式表 `products`",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="仅查询并打印当前初步表 `deal_raw_tips` 与正式表 `products` 的统计分析报告",
    )
    parser.add_argument(
        "--ops-report",
        action="store_true",
        help="仅查看最近一轮运维拉取与清洗健康度诊断报告",
    )
    args = parser.parse_args()

    repo = DealRawPoolRepository(
        collection_name=args.collection,
        formal_collection_name=args.formal_collection,
    )
    cleaner = DealPoolCleaner(
        use_ai_for_multi=True,
        ai_batch_size=args.ai_batch_size,
        ai_max_workers=args.ai_workers,
        convert_max_workers=args.convert_workers,
    )

    if args.reset_db:
        try:
            for fname in os.listdir(LOG_DIR):
                fpath = os.path.join(LOG_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                if fpath == LOG_FILE_PATH:
                    with open(fpath, "w", encoding="utf-8") as lf:
                        lf.write("")
                else:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
        except Exception:
            pass
        repo.reset_raw_collection()

    if args.stats:
        print_stats_report(repo)
        return

    if args.ops_report:
        print_latest_ops_report()
        return

    if args.clean_only:
        run_clean_stage(
            repo=repo,
            cleaner=cleaner,
            cycle_num=1,
            clean_limit=args.clean_limit,
        )
        print_stats_report(repo)
        return

    stop_requested = False
    new_fetch_event = threading.Event()

    def _handle_sig(signum, frame):
        nonlocal stop_requested
        logger.info("收到停止信号 (%s)，正在通知拉取与清洗线程优雅退出...", signum)
        stop_requested = True
        new_fetch_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    channel_health_tracker: Dict[str, Dict[str, Any]] = {}

    if args.once or args.max_cycles == 1:
        with requests.Session() as session:
            run_single_cycle(
                repo=repo,
                cleaner=cleaner,
                session=session,
                cycle_num=1,
                max_new_details_per_channel=args.max_new_details,
                run_all_tiers=args.all_channels,
                clean_limit=args.clean_limit,
                channel_health_tracker=channel_health_tracker,
            )
        print_stats_report(repo)
        return

    start_wall = time.time()
    deadline_wall = (start_wall + args.duration_hours * 3600.0) if args.duration_hours > 0 else None

    if args.serial:
        logger.info(
            "🚀 好价线报【串行模式：拉取 -> 清洗】守护已启动 | 初步表=[%s] -> 正式表=[%s] | "
            "轮询间隔=%ds | 清洗上限=%d/轮 | AI批次=%d(x%d线程) | 转链并发=%d线程",
            args.collection,
            args.formal_collection,
            args.interval,
            args.clean_limit,
            args.ai_batch_size,
            args.ai_workers,
            args.convert_workers,
        )
        cycle_num = 0
        while not stop_requested:
            cycle_num += 1
            try:
                with requests.Session() as session:
                    run_single_cycle(
                        repo=repo,
                        cleaner=cleaner,
                        session=session,
                        cycle_num=cycle_num,
                        max_new_details_per_channel=args.max_new_details,
                        run_all_tiers=args.all_channels or (cycle_num % 2 == 0),
                        clean_limit=args.clean_limit,
                        channel_health_tracker=channel_health_tracker,
                    )
            except Exception as cycle_exc:
                logger.exception("🚨 第 #%d 轮执行发生未捕获异常，将在下一轮自动恢复: %s", cycle_num, cycle_exc)

            if args.max_cycles > 0 and cycle_num >= args.max_cycles:
                logger.info("已达到设定的最大轮数 (--max-cycles=%d)，定时任务正常结束。", args.max_cycles)
                break
            if deadline_wall is not None and time.time() >= deadline_wall:
                logger.info("已达到设定的持续运行时长 (--duration-hours=%.2f)，定时任务正常结束。", args.duration_hours)
                break

            sleep_target = max(10, int(args.interval))
            for _ in range(sleep_target):
                if stop_requested or (deadline_wall is not None and time.time() >= deadline_wall):
                    break
                time.sleep(1)
        print_stats_report(repo)
        return

    # ==========================================================================
    # 默认高吞吐模式：【Producer 多源定时拉取线程】与【Consumer 积压连续清洗线程】双线程并行
    # ==========================================================================
    logger.info(
        "🚀 好价线报【双线程并行守护：Producer定时拉取 + Consumer连续清洗】已启动 | 初步表=[%s] -> 正式表=[%s] | "
        "拉取间隔=%ds | 清洗批次上限=%d/轮 | AI批次=%d(x%d线程) | 转链并发=%d线程 | 主日志=[%s]",
        args.collection,
        args.formal_collection,
        args.interval,
        args.clean_limit,
        args.ai_batch_size,
        args.ai_workers,
        args.convert_workers,
        LOG_FILE_PATH,
    )

    shared_state: Dict[str, Any] = {"latest_clean_report": {}}

    def _producer_fetch_worker() -> None:
        nonlocal stop_requested
        fetch_cycle = 0
        while not stop_requested:
            fetch_cycle += 1
            try:
                with requests.Session() as session:
                    fetch_res = run_fetch_stage(
                        repo=repo,
                        session=session,
                        cycle_num=fetch_cycle,
                        max_new_details_per_channel=args.max_new_details,
                        run_all_tiers=args.all_channels or (fetch_cycle % 2 == 0),
                        channel_health_tracker=channel_health_tracker,
                        latest_clean_report=shared_state.get("latest_clean_report"),
                    )
                    if fetch_res.get("total_inserted", 0) > 0:
                        new_fetch_event.set()
            except Exception as fetch_exc:
                logger.exception("🚨 [拉取线程/Producer] 第 #%d 轮拉取异常，将在下一轮自动恢复: %s", fetch_cycle, fetch_exc)

            if args.max_cycles > 0 and fetch_cycle >= args.max_cycles:
                logger.info("🏁 [拉取线程/Producer] 已完成设定最大拉取轮数 (--max-cycles=%d)，通知清洗线程收尾退出。", args.max_cycles)
                stop_requested = True
                new_fetch_event.set()
                break

            if deadline_wall is not None and time.time() >= deadline_wall:
                logger.info("🏁 [拉取线程/Producer] 已达到设定运行时长 (--duration-hours=%.2f)，通知清洗线程收尾退出。", args.duration_hours)
                stop_requested = True
                new_fetch_event.set()
                break

            sleep_target = max(10, int(args.interval))
            logger.info(
                "⏳ [拉取线程/Producer] 第 #%d 轮拉取完毕，休眠 %d 秒后进入第 #%d 轮拉取（清洗线程在后台并行运转中）...",
                fetch_cycle,
                sleep_target,
                fetch_cycle + 1,
            )
            for _ in range(sleep_target):
                if stop_requested or (deadline_wall is not None and time.time() >= deadline_wall):
                    stop_requested = True
                    new_fetch_event.set()
                    break
                time.sleep(1)

    def _consumer_clean_worker() -> None:
        nonlocal stop_requested
        clean_cycle = 0
        was_idle = False
        # 启动后先等 3 秒，让第一轮拉取率先启动连接
        time.sleep(3)
        while not stop_requested:
            if deadline_wall is not None and time.time() >= deadline_wall:
                stop_requested = True
                break
            try:
                pending_cnt = repo.col.count_documents(
                    {"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]}
                )
            except Exception as e:
                logger.warning("[清洗线程/Consumer] 查询 pending 数量异常: %s", e)
                pending_cnt = 0

            if pending_cnt <= 0:
                if not was_idle:
                    logger.info("✨ [清洗线程/Consumer] 当前初步表 `pending=0` 已全部清空，等待拉取线程注入新线报...")
                    was_idle = True
                new_fetch_event.wait(timeout=15.0)
                new_fetch_event.clear()
                continue

            was_idle = False
            clean_cycle += 1
            try:
                c_rep = run_clean_stage(
                    repo=repo,
                    cleaner=cleaner,
                    cycle_num=clean_cycle,
                    clean_limit=args.clean_limit,
                )
                shared_state["latest_clean_report"] = c_rep
                summary = repo.generate_pool_summary()
                with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)
            except Exception as clean_exc:
                logger.exception("🚨 [清洗线程/Consumer] 第 #%d 批清洗发生异常，3秒后自动继续: %s", clean_cycle, clean_exc)
                time.sleep(3)
                continue

            # 若仍有积压，仅短暂喘息 2 秒即无缝开始下一批清洗，彻底消除 180 秒空等！
            for _ in range(2):
                if stop_requested:
                    break
                time.sleep(1)

    producer_thread = threading.Thread(target=_producer_fetch_worker, name="DealProducerFetch", daemon=True)
    consumer_thread = threading.Thread(target=_consumer_clean_worker, name="DealConsumerClean", daemon=True)

    producer_thread.start()
    consumer_thread.start()

    try:
        while producer_thread.is_alive() or consumer_thread.is_alive():
            producer_thread.join(timeout=1.0)
            consumer_thread.join(timeout=1.0)
            if stop_requested and not producer_thread.is_alive():
                # 等待当前正在执行的清洗批次优雅收尾
                consumer_thread.join(timeout=5.0)
                break
    except KeyboardInterrupt:
        stop_requested = True
        new_fetch_event.set()

    print_stats_report(repo)


if __name__ == "__main__":
    main()
