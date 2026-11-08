# -*- coding: utf-8 -*-
"""
好价线报与采集群定时拉取 (`deal_raw_tips`) + 唯一商品清洗入库 (`products`) 一体化守护脚本
================================================================================================
零、【核心工程思想与演进铁律（⚠️ 后续所有开发人员与 AI 迭代优化必须严格遵循，严禁违背）】
  1. 极简基线先行（Baseline First，如无必要勿增实体）：
     - 越简单越可靠。系统首要目标是建立一个【最简单、最稳定、零脆弱特判】的基准线（Baseline），
       绝对不要为了在一开始追求极致的转化率，而在代码中堆砌大量复杂的正则表达式、第三方非公开桥接接口、
       HTML 页面二次爬虫或易误伤的关键词黑名单。任何脆弱的特判都会在长期无人值守运行中成为维护噩梦。
  2. 第一阶段（极简信号粗筛 -> 入初步表 `deal_raw_tips`）：
     - 第一阶段绝不使用复杂的正则提取器去抠取商品 ID，只需用最基础、最稳定的【关键字或符号存在性检查】
       （`has_potential_product_signal`：如包含网址前缀 `http`、主流电商域名关键字、口令符号 `￥/¥/$/!/！`
       或商品编号关键字 `货号/编号/sku` 等），判断这条数据“是否可能包含想要的商品信息”。
     - 只要具备潜在商品信号，就值得将其原样幂等存入初步表 `deal_raw_tips`（标记 `clean_status="pending"`）。
  3. 第二阶段（直接转链核验 -> 入正式表 `products`）：
     - 清洗的核心生死线是：**能否通过官方转链解析出唯一的商品 (`platform + product_id + goods_sign`)**。
       如果连唯一商品都转链不出来，一切教程与截图均无意义。
     - 直接将初步表记录中的候选链接、口令或文本交给项目标准的平台适配器
       （`JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal()`）进行转链：
       * 若能够直接转链出具体的唯一商品（返回非空 `goods_id`、`goods_sign` 与官方 `product` 详情字典），
         则自然证明该商品真实有效！随后提取并维护爆料时间 (`publish_time`)、到手价 (`deal_price`)、
         购买教程 (`tutorial_text`)、实付截图 (`tutorial_images`) 等锦上添花信息，幂等入库正式表 `products`。
       * 若转链失败，不做任何脆弱的偏门回退，直接将上游真实错误码与原因记录到 `deal_raw_tips` 与失败样本日志。
  4. 数据驱动的渐进式迭代（Data-Driven Iteration）：
     - 先让这套最简单稳定的基线系统持续运行数小时乃至数天，积累真实的成功/失败分布日志。
     - 后续优化必须严格基于日志统计数据：只针对失败占比最高、投入产出比（ROI）最高、且能以简单稳定方式解决的环节
       做渐进式迭代，绝不盲目打补丁。

一、【两大核心阶段职责】
  1. 阶段一：多站点增量拉取与潜在商品信号粗筛 (`run_single_cycle` -> 写入初步表 `deal_raw_tips`)
     - 覆盖 9 大站点、56 个优选频道（T0 核心 25 个频道每轮必跑，T1 扩充 31 个频道偶数轮或 `--all-channels` 跑）；
     - 增量抓取：自动跳过 `deal_raw_tips` 中已拉取过完整详情的 `source_item_id`，仅对新线报抓详情；
     - 仅当 `has_potential_product_signal(record)` 为 True 时入库 `deal_raw_tips`，新记录初始化 `clean_status="pending"`。
  2. 阶段二：直接转链唯一商品核验与正式表入库 (`run_clean_stage` -> 写入正式表 `products` 并回写 `deal_raw_tips`)
     - 从 `deal_raw_tips` 读取 `clean_status="pending"` 的记录，按 9 大站点均衡轮询；
     - 提取记录中的候选输入（链接 / 口令 / 正文），直接调用对应平台 `Adapter.resolve_and_convert_universal()` 转链；
     - 转链成功且拿到唯一商品身份 (`goods_id` + `goods_sign` + `product`) -> 组装完整商品文档 + `deal_info` 专有信息，
       按 `(platform, product_id)` 唯一索引幂等 Upsert 入正式表 `products`，并回写 `clean_status="cleaned_success"`；
     - 转链未返回唯一商品 -> 回写 `clean_status="cleaned_failed"` 及精确错误码，输出全维度统计报告供后续观测分析。

二、【历史尝试过的方法与工程备忘录】
  1. 核心工程设计准则备忘：详见常量 `ENGINEERING_PHILOSOPHY_MANIFEST`。
  2. 采集阶段曾尝试并剔除的 19 个站点/频道（含拼多多专项全网排查失败项与替代方案）：详见常量 `EXCLUDED_SITES_AND_CHANNELS_MANIFEST`。
  3. 清洗与解包阶段曾尝试过的探索（含失败死胡同与验证成功的方案）：详见常量 `CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST`。

三、【全链路可观测性与运维日志体系】
  - 主运行日志           : `logs/deal_pool_scheduler.log`
  - 最新一轮综合运维报告 : `logs/latest_cycle_ops_report.json`
  - 最新清洗专项统计报告 : `logs/latest_clean_ops_report.json`
  - 历史轮次时序流水     : `logs/cycle_ops_history.jsonl` & `logs/clean_ops_history.jsonl`
  - 抓取与清洗失败样本库 : `logs/deal_pool_errors.jsonl` & `logs/clean_failed_samples.jsonl`
  - 正式入库商品预览     : `logs/clean_success_preview.jsonl`
================================================================================================
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
import time
from collections import Counter, defaultdict
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
from infrastructure.ai_api import ask_json
from infrastructure.mongo import gen_db_object
from models.product import (
    extract_canonical_goods_id,
    extract_jd_image_key,
    is_jd_image_key,
)
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
# 集合名称与日志文件路径常量
# ==============================================================================
DEFAULT_RAW_COLLECTION_NAME = "deal_raw_tips"
DEFAULT_FORMAL_PRODUCTS_COLLECTION = "products"
LEGACY_SHADOW_TEST_COLLECTION = "deal_cleaned_products_test"

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
    "gwdang/0-hour_3 (购物党-3小时最热折扣)": (
        "【已尝试并放弃】违反铁律1稳定性要求：2小时14轮实测中有7轮返回0条空包且单次阻塞高达11.4秒（失败率50%），"
        "且其有效商品已被 100% 零报错的 0-latest / 0-lowest / 0-s_lowest / 0-lower_11 四个频道完全覆盖。"
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
    "gwdang_pdd_numeric_only_site_370 (购物党 site_id=370 拼多多商品及详情页 /v2/zhi/detail/1)": (
        "【拼多多专项探索：已尝试并放弃】购物党折扣流中虽包含 `site_id=370` (拼多多) 记录，但其实测 Base64 `buy_steps` 解码结果与详情页 "
        "`/v2/zhi/detail/1?dp_id={dp_id}-370` 均仅提供纯数字 `goods_id` 链接 (`https://mobile.yangkeduo.com/goods.html?goods_id=990159885102`)，"
        "不含 `goods_sign` 或 `p.pinduoduo.com` 推广短链，多多进宝官方转链网关 100% 拒识 (`sub_code=40009, ERR_PDD_NUMERIC_ID`)，严禁无效二次请求详情页。"
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
# 【核心工程准则备忘录】必须长期坚守的极简基线与数据驱动迭代思想
# ==============================================================================
ENGINEERING_PHILOSOPHY_MANIFEST: Dict[str, str] = {
    "principle_01_baseline_first": (
        "【极简基线先行】越简单越可靠，遵循第一性原理（如无必要，勿增实体）。不追求一上来就用复杂的特判补丁把转化率推高，"
        "而是先建立最简单、最稳定、零脆弱特判的基准线（Baseline），确保系统长期无人值守运行时绝对稳健。"
    ),
    "principle_02_minimal_signal_check_in_stage1": (
        "【阶段一拒绝复杂正则提取器】在拉取入初步表 `deal_raw_tips` 阶段，绝不使用脆弱的复杂正则表达式去预抠商品ID或截断URL；"
        "只需通过最简单的关键字或符号（如网址前缀、主流电商域名、口令符号、货号字样或非空链接/口令数组）判断记录中"
        "『是否可能包含想要的商品信息』。只要具备潜在信号就入初步表，避免正则漏匹配或误截断。"
    ),
    "principle_03_direct_conversion_as_single_truth_in_stage2": (
        "【阶段二以直接转链为唯一试金石】在清洗入正式表 `products` 阶段，直接把候选链接/口令/文本交给项目官方平台适配器 "
        "(`JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal()`) 进行转链。"
        "只要能直接转链出具体的唯一商品 (`goods_id` + `goods_sign` + `product` 字典)，就自然证明该商品真实有效，"
        "随即提取维护爆料时间、到手价、购买教程、实付截图等关键信息并幂等入库正式表 `products`。"
    ),
    "principle_04_data_driven_iteration_from_logs": (
        "【基于真实运行日志渐进式优化】严禁脱离真实运行数据去臆造第三方非公开桥接接口、HTML二次爬虫或复杂的文本分词黑名单。"
        "所有未转链成功的记录必须保留精确的上游错误码与失败样本日志，待基线运行数小时至数天后，仅针对日志统计中占比最高、"
        "性价比最高且不引入脆弱性的真实问题做渐进式迭代。"
    ),
}


# ==============================================================================
# 【重要历史档案 2】清洗与解包阶段曾尝试过的探索备忘录（含失败死胡同与验证成功方案）
# （固化于此，防止后续开发人员走回头路或盲目堆砌脆弱特判）
# ==============================================================================
CLEANING_ATTEMPTED_DEAD_ENDS_MANIFEST: Dict[str, str] = {
    "dead_end_01_over_engineered_regex_extractors": (
        "【曾尝试并移除】在阶段一与阶段二编写冗长复杂的正则表达式（`_RE_UNIQUE_PRODUCT_URL`, `_RE_STRICT_TOKEN`, `_RE_EXPLICIT_SKU`）"
        "试图预先精确抠出所有平台的链接与口令。"
        "【问题教训】复杂正则极度脆弱（例如曾把带参数的 `https://s.click.taobao.com/t?e=...` 截断成残缺的 `/t`，或漏掉新型短链），"
        "且平台 `Adapter.resolve_and_convert_universal()` 本身就支持直接传入原始推广文本/链接/口令进行官方解析。"
        "【当前基线做法】阶段一仅做最简单的关键字/符号信号判断 (`has_potential_product_signal`)，阶段二直接交由官方 Adapter 转链。"
    ),
    "dead_end_02_premature_third_party_bridges_and_html_scraping": (
        "【曾尝试并移除】为了强行拉高早期转化率，在清洗器内部耦合了购物党非公开跳板 (`m.gwdang.com/union/go` 桥接京东数字SKU)、"
        "逛丢 `go.php` 动态脚本正则抓取、以及 `u.jd.com` 网页 `hrl` 302 探测与人工伪造兜底 `product_card`。"
        "【问题教训】外部未公开接口与网页内嵌 JS 变量随时会改版或触发限流，一旦失效会导致清洗管线大面积报错甚至合成残缺商品污染正式表。"
        "【当前基线做法】回归极简稳定基线，完全依赖项目标准 `Adapter.resolve_and_convert_universal()` 官方转链结果；"
        "对纯数字 SKU 等上游不支持直接转链的情况，诚实记录精确错误码 (`ERR_JD_NUMERIC_SKU` / `ERR_TB_NUMERIC_ID` / `ERR_PDD_NUMERIC_ID`)，"
        "待基线运行积累充分统计数据后再评估是否需要官方稳健方案。"
    ),
    "dead_end_03_fragile_keyword_blacklists_and_bigram_heuristics": (
        "【曾尝试并移除】在清洗器中硬编码几十个领券词/凑单词黑名单（含异体字“凑単”）、3行滑窗价格正则以及中文二元组分词重叠率校验。"
        "【问题教训】硬编码词表与分词启发式规则永远无法穷举社区千变万化的文案，极易误杀正常好价商品（如“凑单到手XX元”的标题本身就是主商品），"
        "导致代码膨胀到上千行且难以维护。"
        "【当前基线做法】移除所有脆弱的启发式词表过滤，直接将候选输入送入官方 Adapter 转链核验唯一商品。"
    ),
    "dead_end_04_title_fuzzy_search_fallback": (
        "【曾尝试并严厉禁止】当商品链接转链失败时，拿线报标题调用全网模糊搜索 `search_goods(title)` 取第一条商品顶替。"
        "【问题教训】模糊搜索会返回不同店铺、不同规格甚至手机壳配件，造成严重的“张冠李戴（假商品）”事故，违背“核心是解析出唯一商品”的第一性原理。"
        "【当前基线做法】全面禁止标题模糊搜索顶替，100% 要求通过官方转链核验出唯一商品身份。"
    ),
    "dead_end_05_trusting_convert_success_without_product_identity": (
        "【曾尝试并修正】仅判断转链接口返回 `status == 'success'` 就入库，忽略了检验 `goods_id`、`goods_sign` 与 `product` 字典是否非空。"
        "【问题教训】部分平台红包/活动会场短链在联盟接口同样能转链成功，但返回的 `goods_id` 与 `product` 为空，直接入库会产生无商品身份的幽灵记录。"
        "【当前基线做法】以 `status == 'success' and bool(goods_id and goods_sign and isinstance(product, dict))` 作为唯一商品核验通过的黄金准则。"
    ),
    "dead_end_06_local_ai_gateway_sync_blocking": (
        "【曾尝试并移除】在清洗主循环中同步调用本地大模型网关对每条多链接线报做消歧。"
        "【问题教训】单次 AI 网关超时重试可阻塞达 60 秒，导致定时任务严重堆积且引入外部不稳定性。"
        "【当前基线做法】清洗主流程 100% 采用直接转链核验，零外部 AI 阻塞依赖，单条耗时稳定在毫秒至百毫秒级。"
    ),
    "dead_end_07_pdd_numeric_goods_id_direct_or_search_api": (
        "【拼多多专项探索：已尝试并确认上游限制】针对 `https://mobile.yangkeduo.com/goods.html?goods_id=855830242126` 纯数字 ID 链接（主要来自购物党），"
        "曾验证多多进宝转链 API (`pdd.ddk.goods.zs.unit.url.gen`) 与搜索 API (`pdd.ddk.goods.search`)，上游网关自 2021 年起全面废除纯数字 `goods_id` 查询，"
        "强制返回 `sub_code=40009 (仅支持通过goodsSign查询)`；同时测试直接请求 `mobile.yangkeduo.com/goods.html` 网页提取 `goods_sign`，"
        "未登录态 100% 302 跳转至 `login.html` 滑块验证。"
        "【当前基线做法】不对纯数字 PDD `goods_id` 做无效网页爬虫，快速拦截记为 `ERR_PDD_NUMERIC_ID`，并通过扩充携带合法 `goods_sign` / `p.pinduoduo.com` 短链的四大拼多多源头频道解决源头供给。"
    ),
    "success_note_01_pdd_four_high_yield_channels_and_bacaoo_unwrap": (
        "【拼多多专项探索：已验证成功并上线】经全网 9 大站点排查，拼多多此前入库少的主因并非转链率低（携带 `p.pinduoduo.com` 短链或 `goods_sign` 的记录转链成功率达 99.1%），"
        "而是源头仅开启了折淘客 `plat=3` 1 个专属频道，其余站点的拼多多好价被淘京主洪流稀释截断。现已实测验证并上线四大高转化拼多多专属源：\n"
        "  1) 逛丢 `channel='pdd'` (`cate.php?m=拼多多`, P1/P2)：每页 30 条纯拼多多好价，服务端 `go.php` 100% 解包出带 `goods_sign=E9...` 的 `duo_coupon_landing.html`，`PddAdapter` 100% 转链成功（同时修复了 `fetch_guangdiu_deals` 旧正则提前在 `div.showpic` 闭合导致标题与商城名为空的 Bug）；\n"
        "  2) 线报酷 `channel='category_haodan_pdd'` (`/category-haodan-pdd/`, P1/P2)：从 `xb-app.min.js` 的 `window.__xbCfTags` 发现的隐藏官方子流，每页 100 条纯拼多多好单线报，实测 20/20 (100%) 含 `https://p.pinduoduo.com/xxx` 短链；\n"
        "  3) 线报酷 `channel='category_weibo_pdd'` (`/category-weibo-pdd/`) + `id='art-fujia'` 1L~5L 源站同步评论区解析：微博/豆瓣/酷安博主防限流常将购买短链放在 1L 楼主评论区（位于 `</article>` 之后的 `div#art-fujia .c-neirong`），补充提取后实测 `category_weibo_pdd` 短链提取与转链率从 40% 提升至 100%；\n"
        "  4) 拔草哦 `channel='pdd'` (`https://youhui.bacaoo.com/mall/1827`) + `POST https://go.bacaoo.com/` 免风控解包：解析 `direct-alink-url` 单品跳板与每 2 小时发布的 50 款《PDD热销产品汇总》合集帖 (`subproduct-alink-url`)，100% 还原出 `https://p.pinduoduo.com/xxx?sc=EFAC` 短链。"
    ),
}


# ==============================================================================
# 标准化清洗错误码体系 (`CLEAN_ERROR_CODES`) —— 供运行后统计分析各失败类型占比
# ==============================================================================
ERR_NO_ANCHOR = "ERR_NO_ANCHOR"                               # 未检测到任何潜在商品信号或候选输入为空
ERR_UNSUPPORTED_PLATFORM = "ERR_UNSUPPORTED_PLATFORM"         # 唯品会/美团/抖音等非淘京拼支持平台
ERR_TB_NUMERIC_ID = "ERR_TB_NUMERIC_ID"                       # 淘宝/天猫纯数字商品ID（上游接口要求加密ID/口令/短链）
ERR_PDD_NUMERIC_ID = "ERR_PDD_NUMERIC_ID"                     # 拼多多纯数字goods_id（上游接口要求goods_sign或推广短链）
ERR_JD_NUMERIC_SKU = "ERR_JD_NUMERIC_SKU"                     # 京东纯数字SKU（上游接口要求京粉加密链/u.jd.com短链/口令）
ERR_ADAPTER_OFF_PROMOTION = "ERR_ADAPTER_OFF_PROMOTION"       # 商品已下架、已取消推广或活动过期失效
ERR_ADAPTER_CONVERT_FAILED = "ERR_ADAPTER_CONVERT_FAILED"     # 上游转链接口未解析出有效结果（如口令过期、外链无效等）
ERR_EMPTY_PRODUCT_IDENTITY = "ERR_EMPTY_PRODUCT_IDENTITY"     # 转链成功但未指向具体单品（如红包/领券/活动会场链接）


# ==============================================================================
# 运维人员故障处理手册 (`OPS_TROUBLESHOOTING_PLAYBOOK`)
# ==============================================================================
SITE_DISPLAY_NAMES: Dict[str, str] = {
    "zhetaoke": "折淘客 (150+采集群/爆款榜)",
    "haodanku": "好单库 (实时线报群/福利单)",
    "guangdiu": "逛丢 (全网/值得买/拼多多明文还原)",
    "xuandan": "选单网 (淘客快抢群发素材)",
    "gwdang": "购物党 (历史低价/比价精选)",
    "0818tuan": "0818团 (实战凑单线报)",
    "xianbao_fun": "线报酷 (22频道全网与拼多多聚合)",
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
    "gwdang": {
        "endpoint": "https://www.gwdang.com/v2/home/discount/{mode}",
        "http_403_429": "购物党触发 429 频率保护：请确保各购物党频道之间的 `pause_sec` 保持 ≥ 3.0 秒，严禁并发请求。",
        "empty_or_parse": "购物党返回 0 条：请访问对应错误 URL，检查页面内 `data = [...]` 内嵌 JSON 变量名是否变更。",
        "timeout_or_conn": "购物党接口响应慢/超时：购物党部分聚合查询偶有后端慢查询，若某子频道连续 ≥5 轮超时可将其从 T1 任务列表中注释隔离。",
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
            pcol.create_index([("deal_info.is_deal_tip", 1), ("updated_at", -1)], background=True)
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
        第一阶段入初步表 `deal_raw_tips`：
        仅对包含潜在商品信号（`has_potential_product_signal(rec) == True`）的记录幂等入库，
        新记录初始化 `clean_status='pending'`，交由第二阶段直接转链核验。
        """
        if not records:
            return {"inserted": 0, "updated": 0, "skipped_no_signal": 0, "total": 0}

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ops = []
        skipped_no_signal = 0

        for rec in records:
            if not isinstance(rec, dict):
                continue
            site = str(rec.get("source_site") or "").strip()
            item_id = str(rec.get("source_item_id") or "").strip()
            if not site or not item_id:
                continue

            has_full_detail = bool(rec.get("has_full_detail", True))
            quality_signals = evaluate_quality_signals(rec)

            # 第一阶段判断：若已抓完详情但没有任何可能指向商品的关键字或符号，直接跳过不入库
            if has_full_detail and not quality_signals["has_unique_product_anchor"]:
                skipped_no_signal += 1
                continue

            channel = str(rec.get("source_channel") or "").strip()
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
                "clean_status": "pending",
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
            return {"inserted": 0, "updated": 0, "skipped_no_signal": skipped_no_signal, "total": 0}

        try:
            self.col.delete_many({"has_full_detail": False})
            res = self.col.bulk_write(ops, ordered=False)
            return {
                "inserted": res.upserted_count,
                "updated": res.modified_count,
                "skipped_no_signal": skipped_no_signal,
                "total": len(ops),
            }
        except Exception as e:
            logger.error("❌ 批量写入 MongoDB [%s] 失败，降级写入本地 JSONL 备份: %s", self.collection_name, e)
            self._append_fallback_jsonl(records, task_name)
            return {"inserted": 0, "updated": 0, "skipped_no_signal": skipped_no_signal, "total": len(ops), "error": str(e)}

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
                {"$match": {"clean_status": "cleaned_failed"}},
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
# 阶段二核心：好价线报极简基线清洗器 (`DealPoolCleaner`)
# 遵循第一性原理：零脆弱特判，直接调用项目官方 Adapter 进行转链核验唯一商品
# ==============================================================================
def detect_candidate_platform(raw_input: str, default_plat: str = "unknown") -> str:
    """用最简单的子串包含关系判断候选输入所属平台（jd / taobao / pdd）。"""
    s = (raw_input or "").lower()
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


class DealPoolCleaner:
    """
    好价线报缓冲池极简基线清洗器：
    直接将初步表记录中的候选链接/口令/文本交给 `JDAdapter` / `TaobaoAdapter` / `PddAdapter.resolve_and_convert_universal()`
    进行转链。若能成功转链出具体唯一商品 (`goods_id` + `goods_sign` + `product`)，则提取维护关键信息并入库正式表 `products`。
    """

    def __init__(self, use_ai_for_multi: bool = False):
        self.use_ai_for_multi = use_ai_for_multi
        self.adapters = {
            "jd": get_adapter("jd"),
            "taobao": get_adapter("taobao"),
            "pdd": get_adapter("pdd"),
        }
        # 内存级转链结果缓存，避免不同采集群重复刷屏的同一链接/口令重复调用上游接口
        self.anchor_cache: Dict[str, Dict[str, Any]] = {}
        self.stats_cache_hits = 0
        self.stats_ai_calls = 0
        self.stats_jd_sku_bridged = 0
        self.stats_guangdiu_go_bridged = 0

    def collect_candidate_inputs(self, doc: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        从单条 `deal_raw_tips` 记录中按直观顺序收集待转链的候选输入（不做任何复杂正则截取或黑名单特判）：
        1. `extracted_links` 中的 URL 或加密 item_id；
        2. `extracted_tokens` 中的口令字符串；
        3. 若正文 `tutorial_text` 含有信号且尚未覆盖，则将 `tutorial_text` 作为整体交由 Adapter 识别。
        """
        doc_plat = str(doc.get("platform") or "unknown").strip()
        candidates: List[Dict[str, str]] = []
        seen: Set[str] = set()

        for lk in doc.get("extracted_links") or []:
            if not isinstance(lk, dict):
                continue
            raw_u = html_lib.unescape(str(lk.get("url") or lk.get("item_id") or "")).strip()
            if not raw_u or raw_u in seen:
                continue
            seen.add(raw_u)
            plat = detect_candidate_platform(raw_u, str(lk.get("platform") or doc_plat))
            candidates.append({
                "anchor": raw_u,
                "type": str(lk.get("type") or "link"),
                "label": str(lk.get("label") or ""),
                "platform": plat,
            })

        for tk in doc.get("extracted_tokens") or []:
            tk_s = html_lib.unescape(str(tk or "")).strip()
            if not tk_s or tk_s in seen:
                continue
            seen.add(tk_s)
            plat = detect_candidate_platform(tk_s, doc_plat)
            candidates.append({
                "anchor": tk_s,
                "type": "token",
                "label": "口令",
                "platform": plat,
            })

        tutorial_text = html_lib.unescape(str(doc.get("tutorial_text") or "")).strip()
        if not candidates and tutorial_text:
            plat = detect_candidate_platform(tutorial_text, doc_plat)
            candidates.append({
                "anchor": tutorial_text,
                "type": "raw_text",
                "label": "正文文本",
                "platform": plat,
            })

        return candidates

    def convert_single_candidate(self, cand: Dict[str, str]) -> Dict[str, Any]:
        """
        直接调用对应平台的 `Adapter.resolve_and_convert_universal(anchor)` 进行转链。
        """
        anchor = cand["anchor"]
        plat = cand["platform"]

        if plat == "unsupported_other":
            return {
                "resolved": False,
                "error_code": ERR_UNSUPPORTED_PLATFORM,
                "error_msg": f"非淘京拼支持平台外链: {anchor[:80]}",
                "adapter_res": {},
            }

        # 若平台暂未明确，按 taobao -> jd -> pdd 顺序尝试识别，或若默认已知则直接调对应 Adapter
        platforms_to_try = [plat] if plat in ("jd", "taobao", "pdd") else ["taobao", "jd", "pdd"]

        last_out: Dict[str, Any] = {
            "resolved": False,
            "error_code": ERR_ADAPTER_CONVERT_FAILED,
            "error_msg": "未能识别平台或转链失败",
            "adapter_res": {},
        }

        for try_plat in platforms_to_try:
            cache_key = f"{try_plat}:{anchor}"
            if cache_key in self.anchor_cache:
                self.stats_cache_hits += 1
                cached = copy.deepcopy(self.anchor_cache[cache_key])
                if cached.get("resolved"):
                    return cached
                last_out = cached
                continue

            adapter = self.adapters.get(try_plat)
            if not adapter:
                continue

            try:
                res = adapter.resolve_and_convert_universal(anchor)
            except Exception as exc:
                out = {
                    "resolved": False,
                    "error_code": ERR_ADAPTER_CONVERT_FAILED,
                    "error_msg": f"Adapter({try_plat})调用异常: {exc}",
                    "adapter_res": {},
                }
                self.anchor_cache[cache_key] = out
                last_out = out
                continue

            if not isinstance(res, dict) or res.get("status") != "success":
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
                    "resolved": False,
                    "error_code": err_code,
                    "error_msg": raw_msg,
                    "adapter_res": res or {},
                }
                self.anchor_cache[cache_key] = out
                last_out = out
                continue

            goods_id = str(res.get("goods_id") or "").strip()
            goods_sign = str(res.get("goods_sign") or "").strip()
            product_card = res.get("product")

            # 黄金准则：必须同时返回非空 goods_id、goods_sign 及官方商品字典才算转链出具体唯一商品
            if not goods_id or not goods_sign or not isinstance(product_card, dict):
                out = {
                    "resolved": False,
                    "error_code": ERR_EMPTY_PRODUCT_IDENTITY,
                    "error_msg": "转链成功但未指向具体唯一单品（如红包/领券/活动会场链接）",
                    "adapter_res": res,
                }
                self.anchor_cache[cache_key] = out
                last_out = out
                continue

            out = {
                "resolved": True,
                "error_code": "OK",
                "error_msg": "",
                "platform": try_plat,
                "goods_id": goods_id,
                "goods_sign": goods_sign,
                "adapter_res": res,
            }
            self.anchor_cache[cache_key] = out
            return copy.deepcopy(out)

        return copy.deepcopy(last_out)

    def clean_one_record(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        单条记录的极简基线清洗闭环：
        收集候选输入 -> 顺位调用官方 Adapter 直接转链 -> 若转链出具体唯一商品则提取维护关键信息并构造正式表文档。
        """
        t0 = time.perf_counter()
        site = str(doc.get("source_site") or "")
        channel = str(doc.get("source_channel") or "")
        item_id = str(doc.get("source_item_id") or "")
        raw_title = str(doc.get("title") or "").strip()

        candidates = self.collect_candidate_inputs(doc)
        if not candidates:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return {
                "status": "failed",
                "error_code": ERR_NO_ANCHOR,
                "error_msg": "记录中无可用候选链接、口令或文本",
                "source_site": site,
                "source_channel": channel,
                "source_item_id": item_id,
                "title": raw_title,
                "elapsed_ms": elapsed_ms,
                "candidate_count": 0,
                "attempt_errors": [],
            }

        resolved_hit: Optional[Dict[str, Any]] = None
        winning_cand: Optional[Dict[str, str]] = None
        attempt_errors: List[Dict[str, str]] = []

        for cand in candidates[:5]:
            r = self.convert_single_candidate(cand)
            if r.get("resolved"):
                resolved_hit = r
                winning_cand = cand
                break
            attempt_errors.append({
                "anchor": cand["anchor"][:100],
                "error_code": r["error_code"],
                "error_msg": r["error_msg"],
            })

        if not resolved_hit or not winning_cand:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            primary_err_code = attempt_errors[0]["error_code"] if attempt_errors else ERR_ADAPTER_CONVERT_FAILED
            primary_err_msg = attempt_errors[0]["error_msg"] if attempt_errors else "所有候选输入均未能转链出具体商品"
            return {
                "status": "failed",
                "error_code": primary_err_code,
                "error_msg": primary_err_msg,
                "source_site": site,
                "source_channel": channel,
                "source_item_id": item_id,
                "title": raw_title,
                "elapsed_ms": elapsed_ms,
                "candidate_count": len(candidates),
                "attempt_errors": attempt_errors,
            }

        # ======================================================================
        # 直接转链成功解析出唯一商品！提取并维护关键线报信息 (`deal_info`) 准备入正式表 `products`
        # ======================================================================
        plat = resolved_hit["platform"]
        goods_id = resolved_hit["goods_id"]
        goods_sign = resolved_hit["goods_sign"]
        adapter_res = resolved_hit["adapter_res"]
        product_card = copy.deepcopy(adapter_res.get("product") or {})

        my_promo_link = str(adapter_res.get("final_link") or adapter_res.get("url") or "")
        my_promo_token = str(adapter_res.get("promotion_token") or "")

        # 将原教程中的原始商品链接或口令替换为转链后的推广链接/口令
        raw_tutorial = str(doc.get("tutorial_text") or "").strip()
        cleaned_tutorial = raw_tutorial
        old_anchor_str = winning_cand["anchor"]
        if my_promo_link and old_anchor_str and old_anchor_str.startswith("http") and old_anchor_str in cleaned_tutorial:
            cleaned_tutorial = cleaned_tutorial.replace(old_anchor_str, my_promo_link)
        elif my_promo_token and old_anchor_str and old_anchor_str in cleaned_tutorial:
            cleaned_tutorial = cleaned_tutorial.replace(old_anchor_str, my_promo_token)

        # 保留同帖中的其余链接作为附属参考链接
        aux_list: List[Dict[str, Any]] = []
        for c in candidates:
            if c["anchor"] != winning_cand["anchor"] and c["type"] != "raw_text":
                aux_list.append({
                    "label": c.get("label") or "附属链接",
                    "url": c["anchor"],
                })

        # 保留教程图片/买家实付截图
        official_img = str(product_card.get("image_url") or "")
        raw_tutorial_imgs = [str(u).strip() for u in (doc.get("tutorial_images") or []) if str(u or "").strip()]
        proof_and_guide_images: List[str] = []
        for img_u in raw_tutorial_imgs:
            if img_u != official_img and img_u not in proof_and_guide_images:
                proof_and_guide_images.append(img_u)

        raw_p = doc.get("price")
        deal_price = round(float(raw_p), 2) if isinstance(raw_p, (int, float)) and 0 < float(raw_p) < 100000 else None

        official_price = float(product_card.get("price") or adapter_res.get("price") or deal_price or 0.0)
        original_price = float(product_card.get("original_price") or official_price)
        activity_price = float(deal_price) if (isinstance(deal_price, (int, float)) and deal_price > 0) else official_price
        coupon_amount = float(product_card.get("coupon_amount") or adapter_res.get("coupon_amount") or 0.0)
        saved_price = round(max(coupon_amount, max(0.0, original_price - activity_price)), 2)

        publish_time_str = str(doc.get("publish_time") or doc.get("fetched_at") or "").strip()

        deal_info = {
            "is_deal_tip": True,
            "publish_time": publish_time_str,
            "expire_time": doc.get("expire_time"),
            "deal_price": deal_price,
            "deal_price_text": str(doc.get("price_text") or (f"{deal_price}元" if deal_price else "")),
            "official_price": official_price,
            "raw_tip_title": raw_title or str(product_card.get("name") or ""),
            "tutorial_text": cleaned_tutorial,
            "tutorial_images": proof_and_guide_images,
            "auxiliary_links": aux_list,
            "my_promotion_link": my_promo_link,
            "my_promotion_token": my_promo_token,
            "source_site": site,
            "source_channel": channel,
            "source_item_id": item_id,
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
            "name": str(product_card.get("name") or adapter_res.get("title") or raw_title).strip(),
            "brand": str(product_card.get("brand") or product_card.get("brand_name") or ""),
            "category": str(product_card.get("category") or product_card.get("category_name") or ""),
            "price": round(official_price, 2),
            "original_price": round(original_price, 2),
            "activity_price": round(activity_price, 2),
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

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "success",
            "error_code": "OK",
            "error_msg": "",
            "source_site": site,
            "source_channel": channel,
            "source_item_id": item_id,
            "platform": plat,
            "product_id": goods_id,
            "goods_sign": goods_sign,
            "official_title": product_doc["name"],
            "raw_title": raw_title,
            "deal_price": deal_price,
            "official_price": official_price,
            "winning_anchor": winning_cand["anchor"][:100],
            "winning_anchor_type": winning_cand["type"],
            "candidate_count": len(candidates),
            "tutorial_img_count": len(proof_and_guide_images),
            "aux_link_count": len(aux_list),
            "elapsed_ms": elapsed_ms,
            "product_doc": product_doc,
        }


def upsert_cleaned_products_to_formal_table(
    formal_col,
    product_docs: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    将直接转链核验通过的唯一商品幂等写入正式表 `products`：
    - 严格以唯一索引 `{"platform": plat, "product_id": pid}` 作为过滤条件；
    - 更新商品最新详情并挂载 `deal_info`（爆料时间、购买教程、实付截图、推广链接与跨群共振热度）。
    """
    if not product_docs:
        return {"upserted": 0, "modified": 0, "matched": 0, "total_ops": 0}

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    grouped_by_pid: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for doc in product_docs:
        grouped_by_pid[(doc["platform"], doc["product_id"])].append(doc)

    ops = []
    for (plat, pid), docs_for_item in grouped_by_pid.items():
        latest_doc = copy.deepcopy(docs_for_item[-1])
        deal_info = copy.deepcopy(latest_doc.pop("deal_info", {}) or {})

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
            **latest_doc,
            "updated_at": now,
            "detail_updated_at": now,
            "last_clean_source": "deal_pool",
        }
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
        "total_ops": len(ops),
    }


def run_clean_stage(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    cycle_num: int,
    clean_limit: int = 200,
) -> Dict[str, Any]:
    """
    阶段二（极简基线版）：从初步表 `deal_raw_tips` 读取 `clean_status='pending'` 的记录，
    按站点均衡轮询，直接调用官方 Adapter 转链核验唯一商品并幂等写入正式表 `products`。
    """
    t_start = time.perf_counter()
    raw_col = repo.col
    formal_col = repo.formal_col

    pending_docs = list(
        raw_col.find({"$or": [{"clean_status": "pending"}, {"clean_status": {"$exists": False}}]})
        .sort("last_seen_at", -1)
    )

    if not pending_docs:
        logger.info("✨ [阶段二：清洗入库] 当前初步表 `deal_raw_tips` 无待清洗 (`pending`) 记录，跳过本轮清洗。")
        return {
            "cycle_num": cycle_num,
            "total_pending_found": 0,
            "total_processed": 0,
            "resolved_success": 0,
            "formal_upserted": 0,
            "formal_modified": 0,
            "elapsed_s": 0.0,
        }

    # 按 9 大站点 Round-Robin 交错排列，保证各站点在每轮清洗中得到均匀覆盖与统计观测
    docs_by_site: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for doc in pending_docs:
        site = str(doc.get("source_site") or "unknown")
        docs_by_site[site].append(doc)

    site_queues = [docs_by_site[s] for s in sorted(docs_by_site.keys())]
    interleaved_docs: List[Dict[str, Any]] = []
    for group in zip_longest(*site_queues):
        for item in group:
            if item is not None:
                interleaved_docs.append(item)

    if clean_limit and clean_limit > 0:
        selected_docs = interleaved_docs[:clean_limit]
    else:
        selected_docs = interleaved_docs

    logger.info(
        "🧼 ================ 开始第 #%d 轮阶段二：极简基线直接转链清洗与正式表入库 "
        "(Pending总计=%d条, 本轮执行转链核验=%d条) ================",
        cycle_num,
        len(pending_docs),
        len(selected_docs),
    )

    all_processed_results: List[Dict[str, Any]] = []
    success_product_docs: List[Dict[str, Any]] = []

    for idx, doc in enumerate(selected_docs, 1):
        res = cleaner.clean_one_record(doc)
        all_processed_results.append(res)
        site = res["source_site"]
        if res["status"] == "success":
            success_product_docs.append(res["product_doc"])
            logger.info(
                "  ✅ [转链成功 %03d/%03d] [%-11s] %-6s | ID=%-24s | 线报价=%-6s 官价=%-6s | %s (%.1fms)",
                idx,
                len(selected_docs),
                site,
                res["platform"],
                res["product_id"][:24],
                str(res["deal_price"]),
                str(res["official_price"]),
                str(res["official_title"])[:32],
                res["elapsed_ms"],
            )
        else:
            logger.info(
                "  ❌ [转链未过 %03d/%03d] [%-11s] %-26s | %s -> %s (%.1fms)",
                idx,
                len(selected_docs),
                site,
                res["error_code"],
                str(res.get("title") or "")[:30],
                res["error_msg"][:45],
                res["elapsed_ms"],
            )

    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # 1. 将所有成功转链出的唯一商品幂等写入正式表 `products`
    formal_write_res = upsert_cleaned_products_to_formal_table(formal_col, success_product_docs)

    # 2. 将每条记录的清洗状态与错误码回写至初步表 `deal_raw_tips`
    raw_status_ops = []
    failed_samples: List[Dict[str, Any]] = []
    for r in all_processed_results:
        s_site = r["source_site"]
        s_id = r["source_item_id"]
        if r["status"] == "success":
            raw_status_ops.append(
                UpdateOne(
                    {"source_site": s_site, "source_item_id": s_id},
                    {
                        "$set": {
                            "clean_status": "cleaned_success",
                            "ai_clean_status": "cleaned_success",
                            "cleaned_at": now_utc,
                            "clean_error_code": "OK",
                            "clean_error_msg": "",
                            "resolved_platform": r["platform"],
                            "resolved_product_id": r["product_id"],
                            "resolved_goods_sign": r["goods_sign"],
                            "resolved_official_title": r["official_title"],
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
                            "clean_status": "cleaned_failed",
                            "ai_clean_status": "cleaned_failed",
                            "cleaned_at": now_utc,
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

    batch_elapsed_s = round(time.perf_counter() - t_start, 2)

    # ==========================================================================
    # 构建阶段二全维度清洗运维统计报告 (`latest_clean_ops_report.json`)
    # ==========================================================================
    total_scanned = len(all_processed_results)
    has_candidate_cnt = sum(1 for r in all_processed_results if r.get("candidate_count", 0) > 0)
    resolved_cnt = len(success_product_docs)
    unique_pids_in_batch = len({(r["platform"], r["product_id"]) for r in all_processed_results if r["status"] == "success"})

    formal_total_now = formal_col.count_documents({})
    formal_deal_total_now = formal_col.count_documents({"deal_info.is_deal_tip": True})

    sites_in_batch = sorted({r["source_site"] for r in all_processed_results})
    site_stats: Dict[str, Dict[str, Any]] = {}
    for site in sites_in_batch:
        s_rows = [r for r in all_processed_results if r["source_site"] == site]
        s_total = len(s_rows)
        s_has_cand = sum(1 for r in s_rows if r.get("candidate_count", 0) > 0)
        s_ok = sum(1 for r in s_rows if r["status"] == "success")
        s_uniq = len({(r["platform"], r["product_id"]) for r in s_rows if r["status"] == "success"})
        err_counter = Counter(r["error_code"] for r in s_rows if r["status"] != "success")
        site_stats[site] = {
            "scanned": s_total,
            "has_product_candidate": s_has_cand,
            "resolved_success": s_ok,
            "unique_products": s_uniq,
            "overall_resolution_rate_pct": round(s_ok / max(s_total, 1) * 100, 1),
            "candidate_resolution_rate_pct": round(s_ok / max(s_has_cand, 1) * 100, 1) if s_has_cand else 0.0,
            "failure_breakdown": dict(err_counter.most_common()),
        }

    global_err_counter = Counter(r["error_code"] for r in all_processed_results if r["status"] != "success")
    plat_counter = Counter(r["platform"] for r in all_processed_results if r["status"] == "success")

    with_publish_time = sum(1 for d in success_product_docs if d["deal_info"].get("publish_time"))
    with_tutorial_text = sum(1 for d in success_product_docs if len(d["deal_info"].get("tutorial_text") or "") >= 10)
    with_proof_images = sum(1 for d in success_product_docs if d["deal_info"].get("tutorial_images"))
    with_aux_links = sum(1 for d in success_product_docs if d["deal_info"].get("auxiliary_links"))
    with_deal_price = sum(1 for d in success_product_docs if d["deal_info"].get("deal_price") is not None)

    clean_report = {
        "cycle_num": cycle_num,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_formal_collection": repo.formal_collection_name,
        "formal_collection_status": {
            "formal_products_total_count": formal_total_now,
            "formal_deal_tip_products_count": formal_deal_total_now,
            "batch_upserted_new_products": formal_write_res["upserted"],
            "batch_modified_existing_products": formal_write_res["modified"],
        },
        "global_funnel": {
            "1_total_scanned_tips": total_scanned,
            "2_has_product_candidate_tips": has_candidate_cnt,
            "3_unique_product_resolved_tips": resolved_cnt,
            "4_deduplicated_unique_products_in_batch": unique_pids_in_batch,
            "overall_resolution_rate_pct": round(resolved_cnt / max(total_scanned, 1) * 100, 1),
            "candidate_resolution_rate_pct": round(resolved_cnt / max(has_candidate_cnt, 1) * 100, 1) if has_candidate_cnt else 0.0,
            "cross_tip_duplication_rate_pct": round((1 - unique_pids_in_batch / max(resolved_cnt, 1)) * 100, 1) if resolved_cnt else 0.0,
            "remaining_pending_anchored_tips": max(0, len(interleaved_docs) - len(selected_docs)),
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
                "share_of_failures_pct": round(cnt / max(total_scanned - resolved_cnt, 1) * 100, 1),
            }
            for code, cnt in global_err_counter.most_common()
        },
        "by_source_site": site_stats,
        "engine_efficiency_metrics": {
            "batch_elapsed_seconds": batch_elapsed_s,
            "avg_ms_per_anchored_record": round(batch_elapsed_s * 1000 / max(has_candidate_cnt, 1), 1) if has_candidate_cnt else 0.0,
            "anchor_cache_hits": cleaner.stats_cache_hits,
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
    lines = [
        "",
        "┌" + "─" * 98 + "┐",
        f"│ 🧼 【第 #{cycle_num} 轮 阶段二（极简基线）：直接转链清洗与正式表 (`{repo.formal_collection_name}`) 入库报告】              │",
        "├" + "─" * 98 + "┤",
        (
            f"│ • 全局转化漏斗 : 本轮扫描Pending={gf['1_total_scanned_tips']}条 -> "
            f"含候选输入={gf['2_has_product_candidate_tips']}条 -> "
            f"直接转链成功唯一商品={gf['3_unique_product_resolved_tips']}条"
        ),
        (
            f"│ • 基线转化率   : 直接转链解析率={gf['candidate_resolution_rate_pct']}% | "
            f"总解析率={gf['overall_resolution_rate_pct']}% | "
            f"本批去重独立商品={gf['4_deduplicated_unique_products_in_batch']}个 (跨帖同品率={gf['cross_tip_duplication_rate_pct']}%)"
        ),
        (
            f"│ • 正式表入库   : 正式表 `{repo.formal_collection_name}` 本轮新增={fc['batch_upserted_new_products']}个, "
            f"更新共振={fc['batch_modified_existing_products']}个 | "
            f"正式表累计好价线报商品={fc['formal_deal_tip_products_count']}个 (全表总商品={fc['formal_products_total_count']}个)"
        ),
        (
            f"│ • 引擎效率指标 : 阶段耗时={batch_elapsed_s}s | 单条均耗={clean_report['engine_efficiency_metrics']['avg_ms_per_anchored_record']}ms | "
            f"转链缓存命中={cleaner.stats_cache_hits}次"
        ),
        (
            f"│ • 专有信息保留 : 爆料时间={er['retained_publish_time_pct']}% | "
            f"完整教程={er['retained_tutorial_text_pct']}% | "
            f"到手价={er['retained_deal_price_pct']}% | "
            f"实付/步骤截图={er['retained_proof_or_guide_images_pct']}%({er['retained_proof_or_guide_images_count']}条) | "
            f"附属链接={er['retained_auxiliary_coupon_or_filler_links_pct']}%({er['retained_auxiliary_coupon_or_filler_links_count']}条)"
        ),
        "├" + "─" * 98 + "┤",
        f"│ {'站点 (source_site)':<15} | {'扫描数':>6} | {'含候选数':>8} | {'转链成功':>8} | {'直接转链率':>10} | {'独立商品':>6} | Top 未转链原因 (供后续分析)",
        "├" + "─" * 98 + "┤",
    ]
    for site, st in sorted(site_stats.items(), key=lambda x: x[1]["resolved_success"], reverse=True):
        top_err = ", ".join(f"{k}:{v}" for k, v in list(st["failure_breakdown"].items())[:2]) or "-"
        lines.append(
            f"│ {site:<15} | {st['scanned']:>6} | {st['has_product_candidate']:>10} | "
            f"{st['resolved_success']:>8} | {st['candidate_resolution_rate_pct']:>9.1f}% | "
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
# 单轮完整闭环编排：阶段一（多源拉取入 `deal_raw_tips`）+ 阶段二（唯一商品清洗入 `products`）
# ==============================================================================
def run_single_cycle(
    repo: DealRawPoolRepository,
    cleaner: DealPoolCleaner,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: int = 20,
    run_all_tiers: bool = False,
    clean_limit: int = 200,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    执行单轮完整【阶段一：拉取入初步表】+【阶段二：清洗入正式表】闭环：
    - T0 核心源（25 个任务）全量覆盖 9 大站点与高转化拼多多专属源，每轮必跑；
    - T1 扩充源（31 个任务）涵盖细分采集群、拼多多扩展翻页、豆瓣专组与比价榜，在 `--all-channels` 或偶数轮运行。
    """
    fetch_start = time.perf_counter()
    include_medium = run_all_tiers or (cycle_num % 2 == 0)
    cycle_mode = "FULL_56_CHANNELS" if include_medium else "T0_25_ALL_9_SITES"
    if channel_health_tracker is None:
        channel_health_tracker = {}

    xianbao_done = repo.get_existing_detailed_ids("xianbao_fun", limit=8000)
    guangdiu_done = repo.get_existing_detailed_ids("guangdiu", limit=4000)
    tuan0818_done = repo.get_existing_detailed_ids("0818tuan", limit=3000)
    bacaoo_done = repo.get_existing_detailed_ids("bacaoo", limit=2000)
    iehou_done = repo.get_existing_detailed_ids("iehou", limit=2000)

    # ---------------- T0 高频核心源（每轮必跑：25 个高产频道，100% 覆盖全部 9 大站点 + 四大拼多多专属源） ----------------
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
        (
            "购物党_最新好价折扣",
            "gwdang",
            "T0_high_freq",
            "https://www.gwdang.com/v2/home/discount/0-latest",
            lambda: fetch_gwdang_discounts(mode="0-latest", session=session),
            1.2,
        ),
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
            # ---------------- T1 中频扩充源（31 个细分采集群、拼多多扩充翻页、豆瓣专组与比价榜频道） ----------------
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
            (
                "购物党_N天历史最低价",
                "gwdang",
                "T1_med_freq",
                "https://www.gwdang.com/v2/home/discount/0-lowest",
                lambda: fetch_gwdang_discounts(mode="0-lowest", session=session),
                3.0,
            ),
            (
                "购物党_全网同款最低价",
                "gwdang",
                "T1_med_freq",
                "https://www.gwdang.com/v2/home/discount/0-s_lowest",
                lambda: fetch_gwdang_discounts(mode="0-s_lowest", session=session),
                3.0,
            ),
            (
                "购物党_低于双11好价",
                "gwdang",
                "T1_med_freq",
                "https://www.gwdang.com/v2/home/discount/0-lower_11",
                lambda: fetch_gwdang_discounts(mode="0-lower_11", session=session),
                3.0,
            ),
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
            (
                "线报酷_慢友线报聚合",
                "xianbao_fun",
                "T1_med_freq",
                "https://new.xianbao.fun/category-man/",
                lambda: fetch_xianbao_fun_deals(
                    channel="category_man",
                    page=1,
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=xianbao_done,
                ),
                0.5,
            ),
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

    # 紧接着执行阶段二：唯一商品清洗与正式表 `products` 入库
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
    except Exception:
        pass

    ops_report = build_and_log_cycle_ops_report(
        cycle_num=cycle_num,
        cycle_mode=cycle_mode,
        fetch_elapsed_s=fetch_elapsed_s,
        task_reports=task_reports,
        cycle_error_paths=cycle_error_paths,
        clean_report=clean_report,
        pool_summary=summary,
    )

    return {
        "cycle_num": cycle_num,
        "fetch_elapsed_s": fetch_elapsed_s,
        "total_fetched": total_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "tasks": task_reports,
        "error_paths": cycle_error_paths,
        "clean_report": clean_report,
        "ops_report": ops_report,
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
    print("  • 纳入监控的 9 大站点入池与清洗入库明细 :")
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
                    f"直接转链成功={gf.get('3_unique_product_resolved_tips')}条 "
                    f"(直接转链率={gf.get('candidate_resolution_rate_pct')}%) -> "
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
        help="持续定时模式下每轮间隔秒数（默认: 180 秒，即 3 分钟）",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="最大执行轮数（0 表示无限持续运行）",
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
        default=180,
        help="每轮阶段二最多调用上游接口核验的记录条数（按9大站点均衡轮询，0为不限，默认: 180）",
    )
    parser.add_argument(
        "--all-channels",
        action="store_true",
        help="强制每轮跑满全部 50 个频道（默认第1轮跑覆盖全部9大站点的22个T0核心频道，偶数轮跑满50个频道）",
    )
    parser.add_argument(
        "--enable-ai",
        action="store_true",
        help="预留参数（当前极简基线默认关闭 AI，100%% 使用官方 Adapter 直接转链）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="立即执行完整一轮【阶段一拉取 + 阶段二清洗入正式表】并输出全套运维与统计报告后退出",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="仅执行阶段二：对初步表 `deal_raw_tips` 中现有的 pending 记录进行清洗并写入正式表 `products`",
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
    cleaner = DealPoolCleaner(use_ai_for_multi=args.enable_ai)

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

    def _handle_sig(signum, frame):
        nonlocal stop_requested
        logger.info("收到停止信号 (%s)，将在当前任务完成后优雅退出...", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    channel_health_tracker: Dict[str, Dict[str, Any]] = {}

    if args.once:
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

    logger.info(
        "🚀 好价线报【多源拉取 + 唯一商品清洗入正式表】一体化定时守护已启动 | 初步表=[%s] -> 正式表=[%s] | "
        "轮询间隔=%ds | 详情上限=%d/频道 | 清洗上限=%d/轮 | 主日志=[%s] | 综合运维报告=[%s] | 清洗专项报告=[%s]",
        args.collection,
        args.formal_collection,
        args.interval,
        args.max_new_details,
        args.clean_limit,
        LOG_FILE_PATH,
        LATEST_OPS_REPORT_PATH,
        LATEST_CLEAN_OPS_REPORT_PATH,
    )

    cycle_num = 0
    while not stop_requested:
        cycle_num += 1
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

        if args.max_cycles > 0 and cycle_num >= args.max_cycles:
            logger.info("已达到设定的最大轮数 (--max-cycles=%d)，定时任务正常结束。", args.max_cycles)
            break

        if deadline_wall is not None and time.time() >= deadline_wall:
            logger.info("已达到设定的持续运行时长 (--duration-hours=%.2f)，定时任务正常结束。", args.duration_hours)
            break

        sleep_target = max(10, int(args.interval))
        logger.info(
            "⏳ 等待 %d 秒后进入第 #%d 轮【拉取 + 清洗入正式表】闭环...",
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
