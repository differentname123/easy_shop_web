# -*- coding: utf-8 -*-
"""
独立导购/线报好价与采集群缓冲池定时拉取与运维监控守护脚本 (`deal_raw_tips` 终版规范)
================================================================================
一、【核心设计原则】
  1. 100% 独立隔离：
     - 仅向独立的 MongoDB 缓冲池集合 `deal_raw_tips` 写入原始线报、采集群消息与好价教程数据；
     - 绝不修改现有 `products` 集合、不触碰现有 Web 路由或订单同步任务，对线上现有流程零侵入。
  2. 拉取阶段零清洗、零丢弃（"重信息完整，轻前置清洗，不怕数据多，只怕有遗漏"）：
     - 在拉取入池阶段，对纳入监控的 **9 大站点、50 个优选频道（含 160+ 个真实 QQ/微信线报采集群）**
       拉取到的所有完整记录 100% 原样入库留存，不做任何前置删除或清洗，完整保留 `tutorial_text`、
       `tutorial_images`、`extracted_links`、`extracted_tokens` 与 `raw_payload`。
     - 入库时自动标注 `quality_signals.has_unique_product_anchor`（是否包含指向唯一商品的硬锚点），
       供后续 AI 清洗与转链工作流按需消费。
  3. 增量智能拉取（零重复抓详情 + 动态热度追踪）：
     - 每轮拉取列表前，先读取 `deal_raw_tips` 中已拉取过完整详情的 `source_item_id` 集合（`skip_detail_ids`）；
     - 仅对【首次出现的新线报】发起详情页 HTTP 请求；对已在池中的老线报仅轻量更新 `last_seen_at`、
       `seen_count`、`seen_channels` 及最新热度 `hot_metrics`。
  4. 全链路错误路径记录与每轮运维诊断报告：
     - 无论是列表页拉取异常、返回 0 条空数据，还是单篇详情页/跳转页请求失败，均精确记录其【错误 URL 路径 (`error_url`)】、
       HTTP 状态码、异常类型、连续失败轮数，并持久化到 `logs/deal_pool_errors.jsonl`；
     - 每一轮拉取结束时，自动输出结构化的【每轮运维拉取与排障诊断报告】（同步输出到终端/日志与
       `logs/latest_cycle_ops_report.json`、`logs/cycle_ops_history.jsonl`），明确给出各站点健康状态及针对性的运维排障指令。

二、【数据源准入的两条硬性铁律】
  - 铁律 1（安全稳定拉取基础）：必须能长期、稳定、不被风控拦截地自动拉取；如果频繁触发验证码、封 IP 或频繁超时空包，必须剔除。
  - 铁律 2（存在指向“唯一商品”的硬锚点，不限有效比例高低）：拉取的数据中必须存在能锁定【唯一具体商品】的信息
    （唯一商品直链/短链、加密商品ID、淘口令/京口令、或明确的 10~15 位数字 SKU/货号），绝不能只是泛泛的商品标题关键字。
    只要该频道能稳定产出含唯一商品锚点的记录，即使有效比例偏低（如豆瓣专组 14%~28%、拔草哦 16.5%）也完全保留，后续用规则过滤即可。

三、【最终纳入 vs 明确排查剔除的站点与频道清单（⚠️ 严禁后续人员误以为遗漏而重复尝试已剔除项）】
  ✅ 正式纳入定时任务的 9 大站点（共 50 个稳定频道任务，覆盖全网 160+ 个真实 QQ/微信采集群与核心导购池）：
     1. 折淘客 (`zhetaoke.com`)       : 12 个任务（直连 150+ 个真实 QQ/微信采集群：京东全群汇总 plat=2、淘宝全群汇总 plat=1、
                                        拼多多全群汇总 plat=3、全平台综合群流 plat=""、新京东带主图群 10000030、云线报群9京东 1067573294、
                                        云线报群5综合 725556337、优质猫超线报群 10000072、宝妈羊毛群③ 10000176、富贵淘宝京东4群 10000128
                                        + 咚咚抢整点榜 dongdong + 全天实时热销榜 realtime_hot）—— 2小时实测入库 3,461 条，唯一商品率 92.3%！
     2. 好单库 (`haodanku.com`)       : 6 个任务（京东采集群 10004、淘宝采集群 10003、百亿补贴群 10005、飞猪生活券群 10367、
                                        全品类综合群 0、福利好价清单 weal_list）—— 0 风控，唯一商品率 98.4%！
     3. 逛丢 (`guangdiu.com`)         : 2 个任务（国内实时折扣流 latest P1 & P2）—— 0 风控；通过 `detail.php` 解包 `to.php?u=`
                                        及 `go.php?id=` 随机变量提取明文 `item.jd.com` / `union-click.jd.com` / `s.click.taobao.com`，
                                        在服务端把同步自“什么值得买”的好价自动还原为真实电商直链！
     4. 选单网 (`xuandan.com`)        : 2 个任务（快抢群发素材全天流 fastbuy_hall、整点场 fastbuy_h1）—— 免登 JSON 0 风控，
                                        100% 同时返回 12 位淘宝纯数字 `ShoopGoodsId`、`uland.taobao.com` 加密链与群发凑单文案 `TjRemark`。
     5. 购物党 (`gwdang.com`)         : 4 个任务（历史最低 0-lowest、全网最低 0-s_lowest、低于双11 0-lower_11、最新好价 0-latest）
                                        —— 间隔 3s 零风控，解码 `data=` 后 100.0% 含唯一商品 SKU 与购买链（已剔除经常超时的 `0-hour_3`）。
     6. 0818团 (`0818tuan.com`)       : 2 个任务（第1页 P1、第2页 P2）—— 纯静态 0 风控，实测唯一商品率 55.2%~71.4%。
     7. 线报酷 (`new.xianbao.fun`)    : 20 个任务（涵盖全站热榜 hot_1h、好单热榜/实时流、微博热榜/实时流、豆瓣全组/买组/拼组/狗组/爱猫澡盆/爱猫生活、
                                        赚客吧热榜/实时流、新赚吧热榜/实时流、酷安热榜/实时流、慢友聚合、全网最新流）—— SSR 0 风控，2小时入库 2,285 条。
     8. 拔草哦 (`youhui.bacaoo.com`)  : 1 个任务（国内优惠 youhui）—— 0 风控，含唯一商品短链/口令（唯一商品率 16.5%）。
     9. 网猴线报 (`iehou.com`)        : 1 个任务（24小时热榜 dayhot）—— 0 风控，含唯一商品短链/口令（唯一商品率 31.2%）。

  ❌ 已明确尝试排查并剔除的 14 个站点与频道（均经实测验证违反铁律1或铁律2，特此在代码固化备忘，严禁后人重复踩坑）：
     详见下方 `EXCLUDED_SITES_AND_CHANNELS_MANIFEST` 字典。

常用命令：
  1. 后台持续定时拉取（保留已有数据增量追加，每轮自动输出运维报告）：
     python scripts/run_deal_pool_scheduler.py --interval 180
  2. 清空旧缓冲池数据与日志并从零启动定时拉取：
     python scripts/run_deal_pool_scheduler.py --reset-db --interval 180
  3. 立即单次全量拉取一轮（50个规范频道任务全跑）并输出运维报告：
     python scripts/run_deal_pool_scheduler.py --once
  4. 随时查看缓冲池数据质量、各站点/频道唯一商品产出率统计报告：
     python scripts/run_deal_pool_scheduler.py --stats
  5. 随时查看最近一轮运维拉取健康度与错误路径诊断报告：
     python scripts/run_deal_pool_scheduler.py --ops-report
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

DEFAULT_COLLECTION_NAME = "deal_raw_tips"
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE_PATH = os.path.join(LOG_DIR, "deal_pool_scheduler.log")
STATS_SNAPSHOT_PATH = os.path.join(LOG_DIR, "deal_pool_stats.json")
LOCAL_FALLBACK_JSONL = os.path.join(LOG_DIR, "deal_raw_tips_fallback.jsonl")
ERROR_LOG_JSONL_PATH = os.path.join(LOG_DIR, "deal_pool_errors.jsonl")
LATEST_OPS_REPORT_PATH = os.path.join(LOG_DIR, "latest_cycle_ops_report.json")
OPS_REPORT_HISTORY_JSONL = os.path.join(LOG_DIR, "cycle_ops_history.jsonl")

# ==============================================================================
# 明确排查并剔除的 14 个站点与频道备忘字典（供代码审计、日志与统计报告直接备查，防止后人误以为遗漏）
# ==============================================================================
EXCLUDED_SITES_AND_CHANNELS_MANIFEST: Dict[str, str] = {
    "smzdm (什么值得买直连及镜像 zhidemai/zhidemai_hot)": (
        "违反铁律1与铁律2：faxian子域高频触发TCaptcha验证码；且全站外链均被重写为加密跳板 go.smzdm.com (带probe.js拦截)、"
        "图片重写为 zdmimg.com，实测 0/183 (0.0%) 含唯一商品直链/口令/SKU。（注：其高优好价已通过【逛丢 guangdiu】服务端明文解包曲线捕获！）"
    ),
    "manmanbuy (慢慢买省钱控 cu.manmanbuy.com)": (
        "违反铁律2：详情页 discuxiao_{id}.aspx 采用前端 crypto-js 动态加密跳转，"
        "实测 0/43 (0.0%) 含唯一商品链接/口令/SKU，仅有泛泛的商品标题。"
    ),
    "55haitao (55海淘 55haitao.com/deals)": (
        "违反铁律2：实测 0/22 (0.0%) 含国内电商商品，100% 为海外欧美独立站海淘链接，无国内淘京拼 CPS 变现价值。"
    ),
    "dataoke (大淘客网页端 dataoke.com/xp/xb)": (
        "违反铁律1：强依赖网页登录态 Cookie 极易失效，且底层线报数据与免登录的好单库 (haodanku) 及折淘客 (zhetaoke) 100% 同源。"
    ),
    "shihuizhu & huim (实惠猪 shihuizhu.com / 惠喵 huim.com)": (
        "违反铁律1：直连请求被服务端防火墙直接 TCP Reset (ConnectionResetError 10054) 阻断，无法稳定自动拉取。"
    ),
    "qingtaoke & maishoudang & haopintui (轻淘客 / 买手党 / 好品推)": (
        "违反铁律1：实测连接超时、站点关停或接口已失效。"
    ),
    "meidebi (没得比 meidebi.com)": (
        "违反铁律2：全站商品外链均为内部加密跳板且正文无明文淘口令或数字 SKU，实测有效唯一商品直链为 0。"
    ),
    "juchaozhi (聚超值 best.pconline.com.cn)": (
        "违反铁律2：已转型为长篇数码评测文章站，528KB 首页仅 2 个静态广告位，无实时好价商品流。"
    ),
    "haodanku_fastbuy & duohaodan (好单库快抢接口 / 多好单 duohaodan.com)": (
        "违反铁律1：未登录态下返回 status:0 (fastwait.png) 占位图或强制要求登录（同类快抢群发素材已由免登录的【选单网 xuandan】100% 覆盖）。"
    ),
    "zuanke8_origin & xiaodao & iqnew & huluxia (赚客吧原站 / 小刀娱乐网 / 爱Q生活网 / 葫芦侠)": (
        "违反铁律1与铁律2：原站存在防CC盾或超时；且小刀/爱Q/葫芦侠/免费福利(mianfei)板块实测全为网盘/游戏/软件资源而非电商商品"
        "（赚客吧电商线报已通过 xianbao_fun/category_zuankeba 完整捕获）。"
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
    "gwdang/0-hour_3 (购物党-3小时最热折扣)": (
        "违反铁律1稳定性要求：2小时14轮实测中有7轮返回0条空包且单次阻塞高达11.4秒（失败率50%），"
        "且其有效商品已被 100% 零报错的 0-latest / 0-lowest / 0-s_lowest / 0-lower_11 四个频道完全覆盖。"
    ),
}

# ==============================================================================
# 运维人员故障处理手册 (`OPS_TROUBLESHOOTING_PLAYBOOK`)
# 当某站点或某条 URL 路径出现异常时，自动在每轮报告中匹配输出具体的运维处置方案
# ==============================================================================
SITE_DISPLAY_NAMES: Dict[str, str] = {
    "zhetaoke": "折淘客 (150+采集群/爆款榜)",
    "haodanku": "好单库 (实时线报群/福利单)",
    "guangdiu": "逛丢 (全网与值得买明文还原)",
    "xuandan": "选单网 (淘客快抢群发素材)",
    "gwdang": "购物党 (历史低价/比价精选)",
    "0818tuan": "0818团 (实战凑单线报)",
    "xianbao_fun": "线报酷 (20频道全网社区聚合)",
    "bacaoo": "拔草哦 (国内优惠口令)",
    "iehou": "网猴线报 (24H热榜)",
}

OPS_TROUBLESHOOTING_PLAYBOOK: Dict[str, Dict[str, str]] = {
    "zhetaoke": {
        "endpoint": "https://www.zhetaoke.com/xianbao.aspx | https://www.zhetaoke.com/list/love_1_0_1.html",
        "http_403_429": "折淘客触发频率限制：请将任务间隔 pause_sec 从 0.4s 调高至 1.5s，或检查服务器出口 IP 是否被临时限流。",
        "empty_or_parse": "折淘客返回 0 条记录：若特定群 group_id 夜间无新消息属正常现象；若 all_groups/jd_groups 连续 3 轮为 0，请打开错误路径 URL 检查 `.sc-product` DOM 类名是否改版。",
        "timeout_or_conn": "折淘客连接超时/重置：偶发网络抖动会在下一轮（3分钟后）自动恢复；若连续 ≥3 轮超时，请执行 `curl -I https://www.zhetaoke.com/xianbao.aspx` 检查 DNS 与连通性。",
    },
    "haodanku": {
        "endpoint": "https://www.haodanku.com/Index/get_wire_report_items | https://www.haodanku.com/Weal/get_weal_list",
        "http_403_429": "好单库 Ajax 接口返回 403/429：请检查 `test_deal_sites_scraper.py` 中 `Referer: https://www.haodanku.com/wire_report/index.html` 与 `X-Requested-With: XMLHttpRequest` 请求头是否完整。",
        "empty_or_parse": "好单库返回空列表 (`min_id/data` 为空)：若仅 `飞猪旅行生活券群(10367)` 在夜间返回 0 条属群内暂无人发单；若京东(10004)/淘宝(10003)返回 0 条，请浏览器访问错误路径核查接口字段。",
        "timeout_or_conn": "好单库请求超时：偶发连接池陈旧套接字断开，脚本已在每轮开始时自动重建 Session，下一轮将自动恢复；连续 ≥3 轮失败请排查服务器外网或配置代理。",
    },
    "guangdiu": {
        "endpoint": "https://guangdiu.com/ | https://guangdiu.com/detail.php?id={id} | https://guangdiu.com/go.php?id={id}",
        "http_403_429": "逛丢触发限流：请调低 `--max-new-details`（如从 25 调为 15）或增大详情页请求间隔。",
        "empty_or_parse": "逛丢列表或详情解析为 0：请访问报错的 `detail.php?id=` 或 `go.php?id=` 路径，检查 `div.gooditem` 或 `go.php` 中的 `var <随机名>='...'` 脚本格式是否变更。",
        "timeout_or_conn": "逛丢请求超时（服务器位于海外/跨境链路偶有波动）：单次详情页超时不影响同批其余商品入库，已跳过的 ID 会在下一轮自动补抓。",
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
        "endpoint": "https://new.xianbao.fun/ | https://new.xianbao.fun/plus/json/push.json | https://new.xianbao.fun/{cat}/{id}.html",
        "http_403_429": "线报酷触发频率限制：请将线报酷各频道间 `pause_sec` 从 0.5s 提升至 1.0s。",
        "empty_or_parse": "线报酷某分类返回 0 条：请访问错误路径 URL 检查 `tr.article-list` 表格行结构是否正常。",
        "timeout_or_conn": "线报酷单篇详情页超时：线报酷包含 20 个细分频道，单篇详情超时不影响整体入库，下一轮会自动补抓。",
    },
    "bacaoo": {
        "endpoint": "https://youhui.bacaoo.com/ | https://youhui.bacaoo.com/info_{id}.html",
        "http_403_429": "拔草哦返回 403/429：请调大请求间隔或检查 User-Agent。",
        "empty_or_parse": "拔草哦解析为 0 条：请访问 `https://youhui.bacaoo.com/` 检查 `.publish-list` 节点是否变更。",
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
# 指向“唯一商品”的硬锚点识别正则（用于统计与打标，不在拉取阶段丢弃任何记录）
# ==============================================================================
# 1. 唯一商品直链 / 短链（直接锁定具体商品或活动落地页）
_RE_UNIQUE_PRODUCT_URL = re.compile(
    r"https?://(?:"
    r"u\.jd\.com/[0-9A-Za-z_]+|"
    r"union-click\.jd\.com/jdc\?[^\s\"'<>)\]]*p=[0-9A-Za-z_-]+|"
    r"jingfen\.jd\.com/detail/[0-9A-Za-z_]+\.html|"
    r"item(?:\.m)?\.jd\.com/(?:product/)?\d+\.html|"
    r"3\.cn/[0-9A-Za-z_-]+|"
    r"uland\.taobao\.com/[^\s\"'<>)\]]+|"
    r"s\.click\.taobao\.com/[0-9A-Za-z_]+|"
    r"(?:m|e|s)\.tb\.cn/h\.[0-9A-Za-z_]+|"
    r"tb3\.cn/[0-9A-Za-z_]+|"
    r"item\.taobao\.com/item\.htm\?[^\s\"'<>)\]]*id=\d+|"
    r"detail\.tmall\.com/item\.htm\?[^\s\"'<>)\]]*id=\d+|"
    r"a\.m\.taobao\.com/i\d+\.htm|"
    r"p\.pinduoduo\.com/[0-9A-Za-z_]+|"
    r"mobile\.yangkeduo\.com/[^\s\"'<>)\]]*(?:goods_id=\d+|ps=[0-9A-Za-z_-]+)|"
    r"v\.douyin\.com/[0-9A-Za-z_-]+|"
    r"t\.vip\.com/[0-9A-Za-z_-]+|"
    r"(?:q|f)\.yuebai\.plus/[0-9A-Za-z_-]+|"
    r"dpurl\.cn/[0-9A-Za-z_-]+"
    r")",
    re.I,
)

# 2. 唯一淘口令 / 京口令 / 括号防屏蔽口令 `(Pf8f6c6Sbdu)` / 46位加密ID（要求字母+数字组合）
_RE_STRICT_TOKEN = re.compile(
    r"(?:"
    r"[￥¥$€₴£¢!！](?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{6,20}[￥¥$€₴£¢!！]|"
    r"[（(](?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{8,16}[)）]|"
    r"(?<![A-Za-z0-9:/._-])/(?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{8,16}/(?![A-Za-z0-9_.-])|"
    r"\b(?:CA|CZ|MF|HU|MU|AC)\d{2,5}\b|"
    r"@[A-Za-z0-9]{5,8}\b|"
    r"\b[A-Za-z0-9_-]{24,28}-[A-Za-z0-9_]{16,22}\b|"
    r"\b[A-Za-z0-9]{25,35}_[A-Za-z0-9]{15,22}\b"
    r")"
)

# 3. 明确标注的京东/淘宝数字商品编码/货号/SKU（10~15位纯数字）
_RE_EXPLICIT_SKU = re.compile(
    r"(?:货号|商品编码|商品编号|编码|编号|淘宝商品ID|sku|itemid)[：:\s=\n]*(\d{10,15})\b"
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
    "0-lowest", "0-s_lowest", "0-lower_11", "dayhot",
    "jd_groups", "tb_groups", "pdd_groups", "all_groups", "group_",
    "rank_dongdong", "rank_realtime_hot", "fastbuy_",
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
        lk.get("type") in ("tb_encrypted_id", "tb_numeric_sku", "gwdang_dp_sku")
        for lk in links if isinstance(lk, dict)
    )

    # 2. 检测严格淘口令 / 京口令
    matched_unique_tokens = list(dict.fromkeys(_RE_STRICT_TOKEN.findall(combined_text)))
    for tok in tokens:
        tok_s = str(tok or "").strip()
        if tok_s and _RE_STRICT_TOKEN.search(tok_s) and tok_s not in matched_unique_tokens:
            matched_unique_tokens.append(tok_s)

    # 3. 检测明确数字 SKU / 货号
    sku_raw_matches = _RE_EXPLICIT_SKU.findall(combined_text)
    matched_unique_skus = list(
        dict.fromkeys(m[0] or m[1] for m in sku_raw_matches if (m[0] or m[1]))
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
                "tb_numeric_sku",
                "guangdiu_go_target",
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
# 每轮运维拉取健康度与排障诊断报告生成器
# ==============================================================================
def build_and_log_cycle_ops_report(
    cycle_num: int,
    cycle_mode: str,
    cycle_elapsed_s: float,
    task_reports: List[Dict[str, Any]],
    cycle_error_paths: List[Dict[str, Any]],
    pool_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """
    生成每轮运维拉取与排障诊断报告：
    1. 按 9 大站点聚合健康状态 (`🟢正常` / `🟡部分降级或详情告警` / `🔴列表拉取失败`)；
    2. 汇总本轮所有错误路径（列表 URL 与详情页 URL）、HTTP 状态码、连续失败轮数与具体运维处置建议；
    3. 格式化输出到日志/终端，并写入 `logs/latest_cycle_ops_report.json` 与 `logs/cycle_ops_history.jsonl`。
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok_tasks = [t for t in task_reports if t["status"] == "success"]
    warn_tasks = [t for t in task_reports if t["status"] == "empty_warning"]
    fail_tasks = [t for t in task_reports if t["status"] == "error"]

    total_fetched = sum(t.get("fetched", 0) for t in task_reports)
    total_detail_fetched = sum(t.get("detail_fetched", 0) for t in task_reports)
    total_inserted = sum(t.get("inserted", 0) for t in task_reports)
    total_updated = sum(t.get("updated", 0) for t in task_reports)
    total_unique_anchor = sum(t.get("unique_anchor_count", 0) for t in task_reports)

    # 按站点聚合本轮健康状况
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
        "elapsed_s": cycle_elapsed_s,
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
        "site_health_matrix": site_health_list,
        "error_paths_count": len(cycle_error_paths),
        "error_paths": cycle_error_paths,
    }

    # 持久化最新一轮运维报告与历史流水
    try:
        with open(LATEST_OPS_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(ops_report, f, ensure_ascii=False, indent=2)
        with open(OPS_REPORT_HISTORY_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(ops_report, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("保存每轮运维报告文件失败: %s", e)

    # 输出直观、运维友好的控制台/日志报告
    lines = [
        "",
        "┌" + "─" * 98 + "┐",
        f"│ 📋 【第 #{cycle_num} 轮 运维拉取健康度与排障诊断报告】 ({now_str}) Mode={cycle_mode:<16} │",
        "├" + "─" * 98 + "┤",
        f"│ • 整体健康度 : {overall_health:<80} │",
        (
            f"│ • 本轮通道数 : 调度={len(task_reports)}个 "
            f"(🟢成功={len(ok_tasks)} | 🟡空列表告警={len(warn_tasks)} | 🔴失败={len(fail_tasks)}) "
            f"| 总耗时={cycle_elapsed_s:.2f}s"
        ),
        (
            f"│ • 本轮数据量 : 拉取={total_fetched}条 (新抓详情={total_detail_fetched}条) "
            f"| 入库新增={total_inserted}条, 更新={total_updated}条 "
            f"| 本轮含唯一商品锚点={total_unique_anchor}条 ({ops_report['cycle_unique_anchor_ratio']}%)"
        ),
        (
            f"│ • 缓冲池累计 : 去重总记录={ops_report['pool_total_records']}条 "
            f"| 累计含【唯一商品硬锚点】={ops_report['pool_unique_anchor_records']}条 "
            f"(占比={ops_report['pool_unique_anchor_ratio']}%)"
        ),
        "├" + "─" * 98 + "┤",
        "│ 【一、各来源站点本轮运行状态矩阵】                                                               │",
    ]
    for st in site_health_list:
        lines.append(
            f"│   {st['health_badge']} {st['site_key']:<12} | "
            f"通道:{st['tasks_ok']}/{st['tasks_total']}正常 | "
            f"拉取:{st['fetched']:>4}条(新详情:{st['detail_fetched']:>3}) | "
            f"新增:{st['inserted']:>3} 更新:{st['updated']:>4} | "
            f"唯一商品:{st['unique_anchor_count']:>4}条({st['unique_anchor_ratio']:>5.1f}%) | "
            f"均耗:{st['avg_elapsed_ms']:>6.1f}ms | 异常路径:{st['tasks_fail'] + st['tasks_warn'] + st['detail_error_count']}个"
        )

    lines.append("├" + "─" * 98 + "┤")
    lines.append("│ 【二、本轮错误路径记录与运维人员处理指南 (Error Paths & Ops Action Guide)】                      │")
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
# 定时拉取任务编排（全景覆盖 9 大稳定站点、50 个含唯一商品锚点的子频道与 160+ 采集群）
# ==============================================================================
def run_single_cycle(
    repo: DealRawPoolRepository,
    session: requests.Session,
    cycle_num: int,
    max_new_details_per_channel: int = 25,
    run_all_tiers: bool = False,
    channel_health_tracker: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    执行单轮采集、记录所有错误路径、原样写入独立集合 `deal_raw_tips`，并在轮末输出运维诊断报告：
    - 覆盖通过“铁律1(稳定零风控) + 铁律2(含唯一商品定位信息)”验证的 9 大站点 50 个频道任务（含好单库与折淘客 160+ 个真实采集群）；
    - 已排查并剔除的 14 个站点/频道见顶部文档与 `EXCLUDED_SITES_AND_CHANNELS_MANIFEST`。
    """
    cycle_start = time.perf_counter()
    include_medium = run_all_tiers or (cycle_num == 1) or (cycle_num % 2 == 0)
    cycle_mode = "FULL_50_CHANNELS" if include_medium else "T0_19_CHANNELS"
    if channel_health_tracker is None:
        channel_health_tracker = {}

    # 预先获取需二次请求详情页的站点已入库 item_id 集合，确保每次只对【新出线报】发起详情请求
    xianbao_done = repo.get_existing_detailed_ids("xianbao_fun", limit=8000)
    guangdiu_done = repo.get_existing_detailed_ids("guangdiu", limit=4000)
    tuan0818_done = repo.get_existing_detailed_ids("0818tuan", limit=3000)
    bacaoo_done = repo.get_existing_detailed_ids("bacaoo", limit=2000)
    iehou_done = repo.get_existing_detailed_ids("iehou", limit=2000)

    # 任务元组结构: (task_name, site_key, freq_tier, default_request_url, callable_fn, pause_sec)
    tasks: List[Tuple[str, str, str, str, Callable[[], Dict[str, Any]], float]] = [
        # ---------------- T0 高频核心源（每轮必跑：19 个高产采集群与唯一商品频道） ----------------
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
            "选单网_快抢群发素材_全天",
            "xuandan",
            "T0_high_freq",
            "https://www.xuandan.com/NewHome/msKqList?page=1&hour_type=",
            lambda: fetch_xuandan_deals(hour_type="", page=1, session=session),
            0.4,
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
    ]

    if include_medium:
        tasks.extend([
            # ---------------- T1 中频扩充源（每2轮跑一次：31 个细分采集群、豆瓣专组与比价榜频道） ----------------
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
                3.0,  # 购物党并发 QPS > 3 会触发 429，任务间固定间隔 3 秒确保 100% 零拦截
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
                "购物党_最新好价折扣",
                "gwdang",
                "T1_med_freq",
                "https://www.gwdang.com/v2/home/discount/0-latest",
                lambda: fetch_gwdang_discounts(mode="0-latest", session=session),
                1.2,
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
            (
                "拔草哦_国内优惠口令",
                "bacaoo",
                "T1_med_freq",
                "https://youhui.bacaoo.com/",
                lambda: fetch_bacaoo_deals(
                    fetch_detail=True,
                    max_details=max_new_details_per_channel,
                    session=session,
                    skip_detail_ids=bacaoo_done,
                ),
                0.6,
            ),
            (
                "网猴线报_24小时热榜",
                "iehou",
                "T1_med_freq",
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
        ])

    total_inserted = 0
    total_updated = 0
    total_fetched = 0
    task_reports: List[Dict[str, Any]] = []
    cycle_error_paths: List[Dict[str, Any]] = []

    logger.info("================ 开始第 #%d 轮好价线报拉取 (共 %d 个规范频道任务) ================", cycle_num, len(tasks))

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

            # 记录详情页子请求错误路径（如有）
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

    # 持久化本轮错误路径记录
    append_error_records_jsonl(cycle_error_paths)

    cycle_elapsed_s = round(time.perf_counter() - cycle_start, 2)
    summary = repo.generate_pool_summary()
    try:
        with open(STATS_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    logger.info(
        "🏁 第 #%d 轮完成 | 本轮拉取=%d条, 新增入库=%d条, 更新=%d条, 错误路径=%d个, 耗时=%.2fs | 缓冲池累计=%d条 (其中含唯一商品硬锚点=%d条, 占比=%.1f%%)",
        cycle_num,
        total_fetched,
        total_inserted,
        total_updated,
        len(cycle_error_paths),
        cycle_elapsed_s,
        summary.get("total_records", 0),
        summary.get("unique_product_anchor_records", 0),
        summary.get("unique_product_anchor_ratio", 0.0),
    )

    ops_report = build_and_log_cycle_ops_report(
        cycle_num=cycle_num,
        cycle_mode=cycle_mode,
        cycle_elapsed_s=cycle_elapsed_s,
        task_reports=task_reports,
        cycle_error_paths=cycle_error_paths,
        pool_summary=summary,
    )

    return {
        "cycle_num": cycle_num,
        "elapsed_s": cycle_elapsed_s,
        "total_fetched": total_fetched,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "tasks": task_reports,
        "error_paths": cycle_error_paths,
        "ops_report": ops_report,
        "pool_summary": summary,
    }


def print_stats_report(repo: DealRawPoolRepository):
    """在终端打印直观的缓冲池数据质量与唯一商品锚点分析报告。"""
    summary = repo.generate_pool_summary()
    print("\n" + "=" * 98)
    print(f"📊 导购/线报与采集群原始缓冲池 (`{summary['collection']}`) 实时统计分析报告 ({summary['generated_at']})")
    print("=" * 98)
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
    print("-" * 98)
    print("  • 纳入监控的 9 大站点入库明细与【唯一商品锚点】产出率 :")
    for s in summary.get("by_source_site") or []:
        cnt = max(1, s["count"])
        u_cnt = s.get("unique_anchor_count", 0)
        u_pct = u_cnt * 100.0 / cnt
        print(
            f"    - {str(s['_id']):<14} : 入库总计={s['count']:>4}条 | "
            f"含唯一商品锚点={u_cnt:>4}条 ({u_pct:>5.1f}%) | "
            f"S级多步凑单={s['s_tier']:>4}条 | A级直链口令={s['a_tier']:>4}条 | 多链接={s['multi_links']:>4}条"
        )
    print("-" * 98)
    print(f"  • 已明确尝试排查并剔除的 {len(EXCLUDED_SITES_AND_CHANNELS_MANIFEST)} 个站点与频道备忘 (违反铁律1或铁律2，严禁后人重复踩坑) :")
    for ex_name, ex_reason in EXCLUDED_SITES_AND_CHANNELS_MANIFEST.items():
        print(f"    ❌ {ex_name}: {ex_reason}")
    if summary.get("cross_channel_resonance_goods"):
        print("-" * 98)
        print("  • 跨站点/多条线报共振同款商品 (全网同推真神价信号) :")
        for g in summary["cross_channel_resonance_goods"][:5]:
            print(
                f"    - 商品标识[{g['_id']}] 出现={g['appear_count']}次 | 站点={g['sites']} | "
                f"价格={g.get('sample_price')} | 标题={str(g.get('sample_title'))[:45]}"
            )
    if summary.get("top_s_tier_samples"):
        print("-" * 98)
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
    print("=" * 98 + "\n")


def print_latest_ops_report():
    """读取并打印最近一轮的运维拉取健康度与错误路径诊断报告。"""
    print("\n" + "=" * 98)
    print("🛠️ 最近一轮运维拉取健康度与错误路径诊断报告 (Ops Pull & Troubleshooting Report)")
    print("=" * 98)
    if os.path.exists(LATEST_OPS_REPORT_PATH):
        try:
            with open(LATEST_OPS_REPORT_PATH, "r", encoding="utf-8") as f:
                rep = json.load(f)
            print(f"  • 报告轮次/时间 : 第 #{rep.get('cycle_num')} 轮 ({rep.get('generated_at')}) | 模式={rep.get('cycle_mode')}")
            print(f"  • 整体健康度    : {rep.get('overall_health')}")
            print(
                f"  • 通道执行统计  : 调度={rep.get('channels_scheduled')}个 "
                f"(🟢正常={rep.get('channels_ok')} | 🟡空列表={rep.get('channels_warn')} | 🔴失败={rep.get('channels_fail')}) "
                f"| 耗时={rep.get('elapsed_s')}s"
            )
            print(
                f"  • 本轮数据入池  : 拉取={rep.get('total_fetched')}条 (新详情={rep.get('total_detail_fetched')}) "
                f"| 新增={rep.get('total_inserted')} 更新={rep.get('total_updated')} "
                f"| 唯一商品锚点={rep.get('cycle_unique_anchor_count')}条 ({rep.get('cycle_unique_anchor_ratio')}%)"
            )
            print("-" * 98)
            print("  • 站点级健康度矩阵 :")
            for st in rep.get("site_health_matrix") or []:
                print(
                    f"    {st.get('health_badge')} {st.get('site_key'):<12} ({st.get('site_name')}): "
                    f"通道={st.get('tasks_ok')}/{st.get('tasks_total')} | "
                    f"拉取={st.get('fetched')}条 | 唯一商品率={st.get('unique_anchor_ratio')}% | "
                    f"均耗={st.get('avg_elapsed_ms')}ms"
                )
            print("-" * 98)
            errs = rep.get("error_paths") or []
            if not errs:
                print("  ✅ 本轮无任何错误路径（所有列表与详情页请求 100% 正常）。")
            else:
                print(f"  ⚠️ 本轮捕获到的错误路径明细 ({len(errs)} 条) :")
                for idx, e in enumerate(errs, 1):
                    print(
                        f"    [{idx}] [{e.get('task_name')}] 阶段={e.get('stage')} | "
                        f"URL={e.get('error_url')} | 状态={e.get('http_status')} | 类型={e.get('error_type')}"
                    )
                    print(f"        🛠️ 运维处理建议: {e.get('ops_advice')}")
        except Exception as ex:
            print(f"  读取 {LATEST_OPS_REPORT_PATH} 失败: {ex}")
    else:
        print(f"  尚未生成 `{LATEST_OPS_REPORT_PATH}`，请先运行至少一轮拉取 (`--once` 或定时守护)。")

    if os.path.exists(ERROR_LOG_JSONL_PATH):
        print("-" * 98)
        print(f"  • 历史错误路径日志文件 (`{ERROR_LOG_JSONL_PATH}`) 最近 10 条记录 :")
        try:
            with open(ERROR_LOG_JSONL_PATH, "r", encoding="utf-8") as ef:
                tail_lines = [ln.strip() for ln in ef if ln.strip()][-10:]
            if not tail_lines:
                print("    (日志文件为空，0 历史错误)")
            for ln in tail_lines:
                obj = json.loads(ln)
                print(
                    f"    - [{obj.get('occurred_at')}] 轮次#{obj.get('cycle_num')} [{obj.get('task_name')}] "
                    f"stage={obj.get('stage')} | URL={obj.get('error_url')} | "
                    f"type={obj.get('error_type')} | 连续失败={obj.get('consecutive_failures')}轮"
                )
        except Exception as ex:
            print(f"    读取历史错误日志异常: {ex}")
    print("=" * 98 + "\n")


def main():
    parser = argparse.ArgumentParser(description="独立导购/线报与采集群缓冲池 (`deal_raw_tips`) 定时拉取与运维监控守护脚本")
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
        help="仅立即执行完整一轮（50个规范频道任务全跑）并输出运维报告与统计报告后退出",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="仅查询并打印当前 MongoDB `deal_raw_tips` 缓冲池统计分析报告，不发起网络拉取",
    )
    parser.add_argument(
        "--ops-report",
        action="store_true",
        help="仅查看最近一轮运维拉取健康度、错误路径记录与排障处理建议，不发起网络拉取",
    )
    args = parser.parse_args()

    repo = DealRawPoolRepository(collection_name=args.collection)

    if args.reset_db:
        for p in (
            LOG_FILE_PATH,
            STATS_SNAPSHOT_PATH,
            ERROR_LOG_JSONL_PATH,
            LATEST_OPS_REPORT_PATH,
            OPS_REPORT_HISTORY_JSONL,
        ):
            try:
                if os.path.exists(p):
                    if p == LOG_FILE_PATH:
                        with open(p, "w", encoding="utf-8") as lf:
                            lf.write("")
                    else:
                        os.remove(p)
            except Exception:
                pass
        repo.reset_collection()

    if args.stats:
        print_stats_report(repo)
        return

    if args.ops_report:
        print_latest_ops_report()
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
                session=session,
                cycle_num=1,
                max_new_details_per_channel=args.max_new_details,
                run_all_tiers=True,
                channel_health_tracker=channel_health_tracker,
            )
        print_stats_report(repo)
        return

    start_wall = time.time()
    deadline_wall = (start_wall + args.duration_hours * 3600.0) if args.duration_hours > 0 else None

    logger.info(
        "🚀 独立好价线报与采集群全景定时任务已启动 | 目标集合=[%s] | 覆盖=9大站点50个优选频道任务(含160+采集群) | 轮询间隔=%ds | 详情增量上限=%d/频道 | 主日志=[%s] | 错误路径日志=[%s] | 每轮运维报告=[%s]",
        args.collection,
        args.interval,
        args.max_new_details,
        LOG_FILE_PATH,
        ERROR_LOG_JSONL_PATH,
        LATEST_OPS_REPORT_PATH,
    )

    cycle_num = 0
    while not stop_requested:
        cycle_num += 1
        # 每轮使用全新 Session 连接池，彻底避免休眠 180 秒后陈旧 Keep-Alive 套接字导致首包 ReadTimeout
        with requests.Session() as session:
            run_single_cycle(
                repo=repo,
                session=session,
                cycle_num=cycle_num,
                max_new_details_per_channel=args.max_new_details,
                run_all_tiers=(cycle_num == 1),
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
            "⏳ 等待 %d 秒后进入第 #%d 轮拉取 (可随时运行 `--stats` 查看入库质量，或运行 `--ops-report` 查看最新运维排障报告)...",
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
