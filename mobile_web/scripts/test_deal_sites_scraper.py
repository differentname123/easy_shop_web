# -*- coding: utf-8 -*-
"""
导购/线报网站好价商品拉取与反爬虫边界/冷却恢复实证基线脚本 (生产级实证版)
================================================================================
本脚本面向“导购/线报网站好价商品监控拉取”需求，作为全项目外部好价数据源探索与验证的唯一权威基线：

一、【核心数据获取与留存原则（重信息完整，轻前置清洗，零丢弃铁律）】
  1. 绝不因格式混乱、DOM 不统一或正则未命中价格而丢弃任何一条线报记录。
  2. 商品基础字段（`platform`, `title`, `price`, `price_text`, `image_url`, `mall_name`, `candidate_goods_id`）
     与“购买/凑单教程上下文”同等重要：
     - `tutorial_text`：将 HTML 中的超链接 (`<a href="URL">锚文本</a>`) 原位展开为 `锚文本 (URL)`，
       将剪贴板属性 (`data-clipboard-text`) 原位展开为 `[口令:...]`，将教程/实付截图 (`<img>`) 原位展开为
       `[教程图片: URL]`，完整保留多步骤换行结构，供下游 AI 模型直接阅读；
     - `tutorial_images`：独立提取的教程截图、实付订单图、商品主图 URL 数组；
     - `extracted_links`：独立提取的所有前置领券链接、主商品链接、凑单退单链接及站内攻略引用链；
     - `extracted_tokens`：独立提取的淘口令、京口令、淘宝 46 位加密 `item_id`、京东 50 位加密 `itemId`；
     - `raw_payload`：100% 留存未裁剪的原始 HTML 正文片段或原始 JSON 节点，作为 AI 清洗的终极兜底。

二、【数据源准入的两条硬性铁律（决定哪些站点入驻定时任务、哪些彻底剔除）】
  - 铁律 1（安全稳定拉取基础）：必须能长期、稳定、不被风控拦截地自动拉取；
  - 铁律 2（存在指向“唯一商品”的硬锚点，不限有效比例高低）：拉取到的数据中必须存在能够锁定【唯一具体商品】的标识
    （包括：唯一商品直链/短链 `u.jd.com`/`jingfen.jd.com`/`item.jd.com`/`3.cn`/`m.tb.cn`/`uland.taobao.com`、
    淘宝46位加密 `item_id`、淘口令/京口令、或明确的 10~15 位数字 SKU/货号），绝不能只是泛泛的商品标题关键字。
    只要通道能稳定产出含唯一商品锚点的记录，即使混有杂讯（有效比例低）也完全可取，后续通过正则/关键字规则过滤即可。

三、【10 个候选网站与细分频道最终取舍结论（严禁后续重复踩坑尝试已剔除站点）】
  1. ✅ 正式纳入定时任务的 9 大站点（49 个有效子频道）：
     - 【好单库 (`haodanku.com`)】：免登纯净 JSON（`wire_report` 京东10004/淘宝10003/百补10005/飞猪10367/综合0 + `weal_list` 福利清单），0 风控，
       100% 包含 `https://u.jd.com/xxx` 短链或淘宝 46 位加密 `item_id`。
     - 【折淘客 (`zhetaoke.com`)】：免登直连 150+ 个真实 QQ/微信采集群实时大厅 (`xianbao.aspx` 京东群/淘宝群/拼多多群/综合群及高产群专流) +
       咚咚抢 (`/list/dongdong.html`) + 实时热销爆款榜 (`/list/love_1_0_1.html`)，单页直出 60 条群消息，无需二次抓详情，0 风控，高密产出 `u.jd.com`、`p.pinduoduo.com`、括号淘口令 `(11位码) MU918` 与 `uland.taobao.com`。
     - 【逛丢 (`guangdiu.com`)】：全网实时折扣 (`index.php` + `cheaps.php` 九块九)，通过 `detail.php` 解码 `to.php?u=` 加上 `go.php?id=` 提取明文 `item.jd.com/{sku}.html`、`union-click.jd.com`、`s.click.taobao.com`，
       不仅覆盖逛丢自有好价，还能在服务端合法还原其同步自“什么值得买”好价的真实电商直链！
     - 【选单网 (`xuandan.com`)】：免登淘客快抢群发素材 JSON 接口 (`/NewHome/msKqList`)，100% 同时返回 12 位淘宝纯数字 `ShoopGoodsId`、`uland.taobao.com` 二合一链、券链与社群推广文案 `TjRemark`。
     - 【购物党 (`gwdang.com`)】：5 大折扣榜（`0-hour_3`/`0-lowest`/`0-s_lowest`/`0-lower_11`/`0-latest`），间隔 >=3s 零风控；
       通过解码内嵌 `data=` Base64 + zlib 载荷，100% 提取原始平台唯一 SKU (`dp_id`) 与 `buy_steps` 中的领券/购买链接。
     - 【0818团 (`0818tuan.com`)】：纯静态 HTML（第1~2页），0 风控，71.4% 直接包含京东/手淘唯一商品短链。
     - 【线报酷 (`new.xianbao.fun`) 20 个有效频道】：SSR 静态直出，0 风控，涵盖全站热榜、好单、微博、豆瓣各买组/拼组/狗组/猫组、赚客吧、新赚吧、酷安、慢友等实时流与热榜。
     - 【拔草哦 (`youhui.bacaoo.com`)】：0 风控，35.0% 含唯一商品短链或口令。
     - 【网猴线报 (`iehou.com/page-dayhot.htm`)】：24H 热榜 0 风控，17.0% 含唯一商品短链或口令。

  2. ❌ 已明确排查并剔除的 13 个站点与子频道（代码保留说明，严禁后续人员误以为遗漏而重复浪费时间尝试）：
     - ❌ 【什么值得买直连 (`smzdm.com` 及其线报酷镜像 `zhidemai`/`zhidemai_hot`) —— 违反铁律 1 & 铁律 2】：
       ① 风控严苛：`faxian.smzdm.com` 高频触发腾讯防水墙 `TCaptcha.js`，异步接口 `/json_more` 触发 `202 probe.js`；
       ② 无唯一商品锚点 (0/183 = 0.0%)：全站所有商品外链均被替换为加密跳板 `https://go.smzdm.com/{加密串}`（带 `probe.js` 拦截），图片也被重写为 `zdmimg.com`，无法直接还原目标商品 ID。（注：其高优好价已通过【逛丢 `guangdiu`】服务端明文跳板曲线捕获！）
     - ❌ 【慢慢买 (`cu.manmanbuy.com`) —— 违反铁律 2】：
       详情页 `discuxiao_{id}.aspx` 为前端 `crypto-js` 动态加密跳转，实测 0/43 (0.0%) 含唯一商品链接、口令或数字 SKU。
     - ❌ 【55海淘 (`55haitao.com/deals`) —— 违反铁律 2】：
       实测 0/22 (0.0%) 含国内电商商品，100% 为海外美网/英网海淘链接，无国内淘京拼 CPS 价值。
     - ❌ 【大淘客网页端 (`dataoke.com/xp/xb`) —— 违反铁律 1】：
       网页端强依赖登录态 Cookie 极易失效，且底层数据与免登录的好单库 (`haodanku`) 100% 同源。
     - ❌ 【实惠猪 (`shihuizhu.com`) & 惠喵 (`huim.com`) —— 违反铁律 1】：
       直连请求直接被服务端防火墙 RST 断开 (`ConnectionResetError 10054`) 或拒绝非浏览器 TLS 指纹。
     - ❌ 【轻淘客 (`qingtaoke.com`) & 买手党 (`maishoudang.com`) & 好品推 (`haopintui.com`) —— 违反铁律 1】：
       站点连接超时、关停或接口已失效，无法稳定拉取。
     - ❌ 【没得比 (`meidebi.com`) —— 违反铁律 2】：
       全站商品外链均为内部加密跳转且无明文口令/SKU，实测有效唯一商品直链为 0。
     - ❌ 【聚超值 (`best.pconline.com.cn`) —— 违反铁律 2】：
       改版后以长篇评测图文为主，528KB 首页仅 2 个静态推广位，无实时好价线报流。
     - ❌ 【好单库快抢页 (`/Index/get_fastbuy_items`) & 多好单 (`duohaodan.com`) —— 违反铁律 1】：
       未登录态下返回 `status: 0 (fastwait.png)` 占位或强制要求联盟账号登录。
     - ❌ 【赚客吧原站 (`zuanke8.com`) / 小刀娱乐网 (`xiaodao.la`) / 爱Q生活网 (`iqnew.com`) / 葫芦侠 (`huluxia`)】：
       原站存在防 CC 盾或超时；且小刀/爱Q/葫芦侠/免费福利 (`mianfei`) 板块实测全为网盘资源、游戏辅助、软件绿软等非电商内容（赚客吧电商线报已通过 `xianbao_fun/category_zuankeba` 完整捕获）。
     - ❌ 【线报酷子频道：微博超话 (`weibo_chaohua_hot`) & 小嘀咕 (`xiaodigu_hot`) —— 违反铁律 2】：
       * `weibo_chaohua_hot`：实测 0/90 (0.0%) 含唯一商品锚点（超话封杀外链与口令，100% 为“买这个+纯订单截图”谜语帖）；
       * `xiaodigu_hot`：实测 0/73 (0.0%) 含电商唯一商品（全为银行数币/水帖/连载小说）。
     - ❌ 【网猴线报首页最新流 (`iehou.com/` index) —— 重复且低效】：
       首页最新流几乎全被微博抽奖刷屏，有效好价已被其 `page-dayhot.htm` (24H热榜) 100% 覆盖。
================================================================================
"""

import argparse
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin

import requests

# 确保 Windows 终端下正常输出中文与 Emoji
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 引入项目现有的跨平台信号识别与京东主图指纹提取工具
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from models.product import detect_platform_signal, extract_jd_image_key
except Exception:
    def detect_platform_signal(raw_text: Any) -> Optional[str]:
        text = str(raw_text or "").strip()
        lower = text.lower()
        if any(d in lower for d in ("u.jd.com", "jingfen.jd.com", "item.jd.com", "3.cn", "jd.com", "京东", "京口令")):
            return "jd"
        if any(d in lower for d in ("tb.cn", "taobao.com", "tmall.com", "淘口令", "手淘", "淘宝", "天猫")):
            return "taobao"
        if any(d in lower for d in ("pinduoduo.com", "yangkeduo.com", "拼多多", "百亿补贴")):
            return "pdd"
        return None

    def extract_jd_image_key(image_url: Any) -> str:
        return ""


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_EXPLICIT_YUAN_RE = re.compile(
    r"(?:(?:￥|¥|💰)\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:元|r\b|R\b|块))",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    r"(?:[￥¥$€₴£¢()（）!！](?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{6,22}[￥¥$€₴£¢()（）!！]"
    r"|(?<![A-Za-z0-9:/._-])/(?=[0-9]*[A-Za-z])(?=[A-Za-z]*[0-9])[A-Za-z0-9]{8,16}/(?![A-Za-z0-9_.-])"
    r"|\b[A-Za-z0-9_-]{24,28}-[A-Za-z0-9_]{16,22}\b"
    r"|\b[A-Za-z0-9]{25,35}_[A-Za-z0-9]{15,22}\b)"
)
_RAW_URL_RE = re.compile(r"https?://[^\s<>\"'\]）】]+", re.IGNORECASE)


# ==============================================================================
# 1. 通用无损信息提取与标准化工具函数（重信息完整，轻前置清洗）
# ==============================================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_first_price(*texts: Any) -> Tuple[Optional[float], str]:
    """
    依次从多个候选文本（price_text / title / tutorial_text）中提取数值参考价格与原始价格短语。
    提取不到数值时返回 (None, "")，绝不因无法解析价格而丢弃记录或拿标题冒充价格。
    """
    for text in texts:
        raw = str(text or "").strip()
        if not raw:
            continue
        if re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
            try:
                return float(raw), f"{raw}元"
            except ValueError:
                pass
        # 优先匹配 【1.45】、【194】 等线报标准到手价括号标注
        m_bracket = re.search(r"【\s*(\d+(?:\.\d{1,2})?)\s*(?:元|r|R|块|包邮|起)?\s*】", raw)
        if m_bracket:
            try:
                return float(m_bracket.group(1)), m_bracket.group(0).strip()
            except ValueError:
                pass
        m = _EXPLICIT_YUAN_RE.search(raw)
        if m:
            num_str = m.group(1) or m.group(2)
            try:
                return float(num_str), m.group(0).strip()
            except ValueError:
                pass
    return None, ""


def extract_links_images_and_tokens(
    raw_html: str,
    base_url: str = "",
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    从 HTML 片段或文本中无损提取：
    1. extracted_links: 所有 <a href="...">、data-clipboard-text 及裸 URL（含锚文本、绝对链接、平台信号）
    2. tutorial_images: 所有 <img> 的 data-src / data-original / src 绝对链接
    3. extracted_tokens: 淘口令、京口令、淘宝46位加密ID、京东50位加密ID等
    """
    html_str = str(raw_html or "")
    links: List[Dict[str, Any]] = []
    seen_urls: set = set()

    for m_a in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html_str, re.S | re.I):
        attrs, inner = m_a.group(1), m_a.group(2)
        anchor_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(inner))).strip()

        clip_m = re.search(r'data-clipboard-text=["\']([^"\']+)["\']', attrs, re.I)
        if clip_m:
            clip_val = html_lib.unescape(clip_m.group(1)).strip()
            if clip_val and clip_val not in seen_urls:
                seen_urls.add(clip_val)
                links.append({
                    "label": anchor_text or "剪贴板口令/活动词",
                    "url": clip_val,
                    "type": "clipboard",
                    "platform": detect_platform_signal(clip_val) or "unknown",
                })

        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not href_m:
            continue
        raw_href = html_lib.unescape(href_m.group(1)).strip()
        if not raw_href or raw_href.startswith(("javascript:", "#", "mailto:")):
            continue
        full_url = urljoin(base_url, raw_href) if base_url else raw_href
        if full_url not in seen_urls:
            seen_urls.add(full_url)
            links.append({
                "label": anchor_text or "链接",
                "url": full_url,
                "type": "anchor",
                "platform": detect_platform_signal(full_url) or detect_platform_signal(anchor_text) or "unknown",
            })

    stripped_tags = re.sub(r"<[^>]+>", " ", html_str)
    for m_url in _RAW_URL_RE.finditer(html_lib.unescape(stripped_tags)):
        u = m_url.group(0).rstrip(".,;，。；!！")
        if u not in seen_urls:
            seen_urls.add(u)
            links.append({
                "label": "正文裸链",
                "url": u,
                "type": "raw_url",
                "platform": detect_platform_signal(u) or "unknown",
            })

    images: List[str] = []
    for m_img in re.finditer(r"<img\b([^>]*)>", html_str, re.S | re.I):
        attrs = m_img.group(1)
        img_candidate = ""
        for attr_name in ("data-src", "data-original", "src"):
            m_attr = re.search(rf'{attr_name}=["\']([^"\']+)["\']', attrs, re.I)
            if m_attr and m_attr.group(1).strip() and not m_attr.group(1).strip().startswith("data:"):
                img_candidate = html_lib.unescape(m_attr.group(1).strip())
                break
        if img_candidate:
            if img_candidate.startswith("//"):
                img_candidate = "https:" + img_candidate
            elif base_url:
                img_candidate = urljoin(base_url, img_candidate)
            if img_candidate not in images:
                images.append(img_candidate)

    tokens: List[str] = []
    for m_tok in _TOKEN_RE.finditer(html_lib.unescape(html_str)):
        tok = m_tok.group(0).strip()
        if tok not in tokens:
            tokens.append(tok)

    return links, images, tokens


def html_to_tutorial_text(raw_html: str, base_url: str = "") -> str:
    """
    将包含购买/凑单教程的 HTML 转换为结构化纯文本（供 AI 直接阅读）：
    - 将 `<a href="URL">锚文本</a>` 原位转换为 `锚文本 (URL)`，防止去标签清洗丢失关键领券/凑单链接；
    - 将 `data-clipboard-text="口令"` 原位保留为 `[口令:...]`；
    - 将 `<img src="IMG">` 原位保留为 `[教程图片: IMG]`；
    - 保留 `<br>`、`</p>`、`</div>`、`</li>` 的段落换行关系。
    """
    if not raw_html:
        return ""
    work = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", str(raw_html), flags=re.S | re.I)

    def _replace_anchor(match: re.Match) -> str:
        attrs, inner = match.group(1), match.group(2)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(inner))).strip()
        clip_m = re.search(r'data-clipboard-text=["\']([^"\']+)["\']', attrs, re.I)
        if clip_m:
            clip_val = html_lib.unescape(clip_m.group(1)).strip()
            if clip_val and clip_val not in text:
                return f"{text} [口令:{clip_val}]" if text else f"[口令:{clip_val}]"
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not href_m:
            return text
        raw_href = html_lib.unescape(href_m.group(1)).strip()
        if not raw_href or raw_href.startswith(("javascript:", "#", "mailto:")):
            return text
        full_url = urljoin(base_url, raw_href) if base_url else raw_href
        if not text:
            return full_url
        if full_url in text or raw_href in text:
            return text
        return f"{text} ({full_url})"

    def _replace_img(match: re.Match) -> str:
        attrs = match.group(1)
        for attr_name in ("data-src", "data-original", "src"):
            m_attr = re.search(rf'{attr_name}=["\']([^"\']+)["\']', attrs, re.I)
            if m_attr and m_attr.group(1).strip() and not m_attr.group(1).strip().startswith("data:"):
                u = html_lib.unescape(m_attr.group(1).strip())
                if u.startswith("//"):
                    u = "https:" + u
                elif base_url:
                    u = urljoin(base_url, u)
                return f"\n[教程图片: {u}]\n"
        return ""

    work = re.sub(r"<a\b([^>]*)>(.*?)</a>", _replace_anchor, work, flags=re.S | re.I)
    work = re.sub(r"<img\b([^>]*)>", _replace_img, work, flags=re.S | re.I)
    work = re.sub(r"<(?:br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", "\n", work, flags=re.I)
    work = re.sub(r"<[^>]+>", " ", work)
    work = html_lib.unescape(work)

    lines = []
    for line in work.splitlines():
        clean_line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if clean_line:
            lines.append(clean_line)
    return "\n".join(lines)


def build_deal_record(
    *,
    source_site: str,
    source_channel: str,
    source_item_id: str,
    source_url: str,
    title: str,
    price_text: str = "",
    price: Optional[float] = None,
    image_url: str = "",
    mall_name: str = "",
    publish_time: str = "",
    expire_time: Optional[str] = None,
    hot_metrics: Optional[Dict[str, Any]] = None,
    tutorial_text: str = "",
    tutorial_images: Optional[List[str]] = None,
    extracted_links: Optional[List[Dict[str, Any]]] = None,
    extracted_tokens: Optional[List[str]] = None,
    raw_payload: Any = None,
    explicit_platform: Optional[str] = None,
) -> Dict[str, Any]:
    """
    构造标准化好价商品与教程留存记录（DealRawRecord）。
    """
    clean_title = re.sub(r"\s+", " ", html_lib.unescape(str(title or ""))).strip()
    if price is None:
        price, inferred_price_text = extract_first_price(price_text, clean_title, tutorial_text)
        if not price_text and inferred_price_text:
            price_text = inferred_price_text

    links = extracted_links or []
    images = tutorial_images or []
    tokens = extracted_tokens or []
    primary_img = image_url or (images[0] if images else "")

    platform = explicit_platform
    if not platform or platform == "unknown":
        probe_blob = " ".join([
            clean_title,
            mall_name,
            tutorial_text,
            " ".join(l.get("url", "") for l in links),
            " ".join(tokens),
        ])
        platform = detect_platform_signal(probe_blob) or "unknown"

    jd_image_key = extract_jd_image_key(primary_img)
    if not jd_image_key:
        for img in images:
            jd_image_key = extract_jd_image_key(img)
            if jd_image_key:
                break
    if jd_image_key and platform == "unknown":
        platform = "jd"

    return {
        "source_site": source_site,
        "source_channel": source_channel,
        "source_item_id": str(source_item_id or "").strip(),
        "source_url": str(source_url or "").strip(),
        "fetched_at": utc_now_iso(),
        "platform": platform,
        "candidate_goods_id": jd_image_key or "",
        "title": clean_title,
        "price_text": str(price_text or "").strip(),
        "price": price,
        "image_url": primary_img,
        "mall_name": str(mall_name or "").strip(),
        "publish_time": str(publish_time or "").strip(),
        "expire_time": str(expire_time).strip() if expire_time not in (None, "", "0", 0) else None,
        "hot_metrics": hot_metrics or {},
        "tutorial_text": tutorial_text,
        "tutorial_images": images,
        "extracted_links": links,
        "extracted_tokens": tokens,
        "raw_payload": raw_payload,
    }


# ==============================================================================
# 2. T0 级主力站点拉取器（好单库免登 JSON / 线报酷全网聚合 / 0818团）
# ==============================================================================
def fetch_haodanku_wire_report(
    category_type: int = 0,
    page_no: int = 1,
    page_size: int = 20,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T0-1A】好单库实时线报免登 JSON 接口 (`https://www.haodanku.com/HolidayActivity/wire_report`)
    - category_type:
      0 = 全部 | 10003 = 淘宝实时线报 | 10004 = 京东实时线报 | 10005 = 淘宝百亿补贴 | 10367 = 飞猪旅行
    """
    http = session or requests.Session()
    url = "https://www.haodanku.com/HolidayActivity/wire_report"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.haodanku.com/activity/tip_off",
        "X-Requested-With": "XMLHttpRequest",
    }
    t0 = time.perf_counter()
    resp = http.get(
        url,
        params={"page_no": page_no, "page_size": page_size, "type": category_type, "is_images": 1},
        headers=headers,
        timeout=timeout,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    body = resp.json()
    raw_items = (body.get("data") or {}).get("items") or []

    records: List[Dict[str, Any]] = []
    for item in raw_items:
        wire_id = str(item.get("wire_id") or "")
        cate_name = str(item.get("cate_id") or "好单线报")
        plat_code = str(item.get("platform") or "")
        explicit_plat = "taobao" if plat_code in ("1", "3") else ("jd" if plat_code == "2" else "unknown")

        step_lines: List[str] = []
        content_lines: List[str] = []
        links: List[Dict[str, Any]] = []
        tokens: List[str] = []

        for idx, step in enumerate(item.get("list") or []):
            if not isinstance(step, dict):
                continue
            content = str(step.get("content") or "").strip()
            item_id_or_url = str(step.get("item_id") or "").strip()
            coupon_url = str(step.get("coupon_url") or step.get("activity_url") or "").strip()
            if content:
                step_lines.append(content)
                content_lines.append(content)
            if coupon_url and coupon_url.startswith(("http://", "https://")):
                step_lines.append(f"  -> 领券/活动链接: {coupon_url}")
                links.append({
                    "label": content_lines[-1] if content_lines else f"步骤{idx + 1}领券/会场",
                    "url": coupon_url,
                    "type": "coupon_url",
                    "platform": detect_platform_signal(coupon_url) or explicit_plat,
                })
            if item_id_or_url:
                if item_id_or_url.startswith(("http://", "https://")):
                    step_lines.append(f"  -> 链接: {item_id_or_url}")
                    links.append({
                        "label": content_lines[-1] if content_lines else f"步骤{idx + 1}链接",
                        "url": item_id_or_url,
                        "type": "step_url",
                        "platform": detect_platform_signal(item_id_or_url) or explicit_plat,
                    })
                else:
                    uland_url = f"https://uland.taobao.com/item/edetail?id={item_id_or_url}"
                    step_lines.append(f"  -> 淘宝加密商品ID: {item_id_or_url} ({uland_url})")
                    tokens.append(item_id_or_url)
                    links.append({
                        "label": content_lines[-1] if content_lines else f"步骤{idx + 1}商品",
                        "url": uland_url,
                        "item_id": item_id_or_url,
                        "type": "tb_encrypted_id",
                        "platform": "taobao",
                    })

        # 挑选最能代表商品本身的标题行（跳过纯领券/会场引导语）
        product_lines = [
            c for c in content_lines
            if len(c) >= 4
            and not c.startswith(("领", "先领", "下拉领", "✅", "①", "②", "③", "④", "0❶", "1.", "2.", "3."))
            and not any(
                k in c
                for k in ("秒杀会场", "直达第一个", "搜标题或关键字", "凑单品", "消沸劵", "消费券", "美妆加补", "附后褪", "凑一件")
            )
        ]
        title_candidate = " | ".join(product_lines[:2]) if product_lines else " | ".join(content_lines[:2])
        title_candidate = title_candidate[:120]

        images = [str(img).strip() for img in (item.get("images") or []) if str(img or "").strip()]
        tutorial_text = "\n".join(step_lines)

        rec = build_deal_record(
            source_site="haodanku",
            source_channel=f"wire_report:{cate_name}",
            source_item_id=wire_id,
            source_url=f"https://www.haodanku.com/activity/tip_off#wire_{wire_id}",
            title=title_candidate or f"好单库线报 #{wire_id}",
            image_url=images[0] if images else "",
            mall_name=cate_name,
            publish_time=str(item.get("starttime") or ""),
            expire_time=str(item.get("endtime") or ""),
            tutorial_text=tutorial_text,
            tutorial_images=images,
            extracted_links=links,
            extracted_tokens=tokens,
            raw_payload=item,
            explicit_platform=explicit_plat,
        )
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and str(body.get("code")) == "200" else "error",
        "http_status": resp.status_code,
        "request_url": str(resp.url),
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "total": (body.get("data") or {}).get("total"),
        "detail_errors": [],
        "records": records,
    }


def fetch_haodanku_weal_list(
    page_no: int = 1,
    page_size: int = 20,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T0-1B】好单库福利好价清单免登 JSON 接口 (`https://www.haodanku.com/HolidayActivity/weal_list`)
    """
    http = session or requests.Session()
    url = "https://www.haodanku.com/HolidayActivity/weal_list"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.haodanku.com/activity/tip_off",
        "X-Requested-With": "XMLHttpRequest",
    }
    t0 = time.perf_counter()
    resp = http.get(url, params={"page_no": page_no, "page_size": page_size}, headers=headers, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    body = resp.json()
    raw_items = (body.get("data") or {}).get("items") or []

    records: List[Dict[str, Any]] = []
    for item in raw_items:
        weal_id = str(item.get("id") or "")
        enc_itemid = str(item.get("itemid") or "").strip()
        short_title = str(item.get("itemshorttitle") or item.get("itemtitle") or "").strip()
        desc = str(item.get("itemdesc") or "").strip()
        end_price_str = str(item.get("itemendprice") or "").strip()
        try:
            price_val = float(end_price_str) if end_price_str else None
        except ValueError:
            price_val = None

        imgs_raw = str(item.get("images") or "").split(",") if item.get("images") else []
        images = [u.strip() for u in imgs_raw if u.strip()]
        if item.get("itempic") and str(item["itempic"]).strip() not in images:
            images.insert(0, str(item["itempic"]).strip())

        uland_url = f"https://uland.taobao.com/item/edetail?id={enc_itemid}" if enc_itemid else ""
        links = [{"label": short_title or "淘宝商品落地页", "url": uland_url, "item_id": enc_itemid, "type": "tb_encrypted_id", "platform": "taobao"}] if uland_url else []

        rec = build_deal_record(
            source_site="haodanku",
            source_channel="weal_list",
            source_item_id=weal_id or enc_itemid,
            source_url=uland_url or "https://www.haodanku.com/activity/tip_off",
            title=short_title,
            price_text=f"{end_price_str}元" if end_price_str else "",
            price=price_val,
            image_url=images[0] if images else "",
            mall_name="淘宝/天猫",
            hot_metrics={
                "todaysale": item.get("todaysale"),
                "itemsale": item.get("itemsale"),
                "tkrates": item.get("tkrates"),
                "tkmoney": item.get("tkmoney"),
            },
            tutorial_text=f"{desc}\n商品加密ID: {enc_itemid}\n落地页: {uland_url}".strip(),
            tutorial_images=images,
            extracted_links=links,
            extracted_tokens=[enc_itemid] if enc_itemid else [],
            raw_payload=item,
            explicit_platform="taobao",
        )
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and str(body.get("code")) == "200" else "error",
        "http_status": resp.status_code,
        "request_url": str(resp.url),
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "total": (body.get("data") or {}).get("total"),
        "detail_errors": [],
        "records": records,
    }


def fetch_xianbao_fun_deals(
    channel: str = "hot_1h",
    page: int = 1,
    fetch_detail: bool = True,
    max_details: int = 5,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """
    【T0-2】线报酷全网聚合站 (`https://new.xianbao.fun/`)
    - 支持全站热榜与全品类聚合频道 `channel`:
      * `"hot_1h"`             -> `/yixiaoshi-hot.html` (全站1小时最热好价/作业)
      * `"haodan_hot"`         -> `/haodan-hot.html` (好单线报热榜 - 100%含口令/链接)
      * `"weibo_hot"`          -> `/weibo-hot.html` (微博好价热榜)
      * `"weibo_chaohua_hot"`  -> `/weibo-chaohua-hot.html` (微博超话热榜)
      * `"zhidemai_hot"`       -> `/zhidemai-hot.html` (值得买热榜聚合)
      * `"douban_hot"`         -> `/douban-hot.html` (豆瓣买组热榜聚合)
      * `"zuankeba_hot"`       -> `/zuankeba-hot.html` (赚客吧热榜聚合)
      * `"xinzuanba_hot"`      -> `/xinzuanba-hot.html` (新赚吧热榜聚合)
      * `"xiaodigu_hot"`       -> `/xiaodigu-hot.html` (小嘀咕热榜聚合)
      * `"kuan_hot"`           -> `/kuan-hot.html` (酷安热榜聚合)
      * `"zhidemai"`           -> `/category-zhidemai/` (什么值得买实时聚合流)
      * `"category_haodan"`    -> `/category-haodan/` (好单线报实时聚合流)
      * `"category_weibo"`     -> `/category-weibo/` (微博线报实时聚合流)
      * `"category_douban_maizu"` -> `/category-douban-maizu/` (豆瓣买组实时流)
      * `"category_douban_pinzu"` -> `/category-douban-pinzu/` (豆瓣拼组实时流)
      * `"category_zuankeba"`  -> `/category-zuankeba/` (赚客吧实时流)
      * `"category_xinzuanba"` -> `/category-xinzuanba/` (新赚吧实时流)
      * `"category_man"`       -> `/category-man/` (慢慢买省钱控实时聚合流)
      * `"latest"`             -> `/` 或 `/page/{page}/` (全网实时聚合流，100条/页)
    """
    http = session or requests.Session()
    base = "https://new.xianbao.fun"
    channel_map = {
        "hot_1h": "/yixiaoshi-hot.html",
        "haodan_hot": "/haodan-hot.html",
        "weibo_hot": "/weibo-hot.html",
        "weibo_chaohua_hot": "/weibo-chaohua-hot.html",
        "zhidemai_hot": "/zhidemai-hot.html",
        "douban_hot": "/douban-hot.html",
        "zuankeba_hot": "/zuankeba-hot.html",
        "xinzuanba_hot": "/xinzuanba-hot.html",
        "xiaodigu_hot": "/xiaodigu-hot.html",
        "kuan_hot": "/kuan-hot.html",
        "zhidemai": "/category-zhidemai/" if page <= 1 else f"/category-zhidemai/page/{page}/",
        "category_haodan": "/category-haodan/" if page <= 1 else f"/category-haodan/page/{page}/",
        "category_weibo": "/category-weibo/" if page <= 1 else f"/category-weibo/page/{page}/",
        "category_weibo_chaohua": "/category-weibo-chaohua/" if page <= 1 else f"/category-weibo-chaohua/page/{page}/",
        "category_douban": "/category-douban/" if page <= 1 else f"/category-douban/page/{page}/",
        "category_douban_maizu": "/category-douban-maizu/" if page <= 1 else f"/category-douban-maizu/page/{page}/",
        "category_douban_pinzu": "/category-douban-pinzu/" if page <= 1 else f"/category-douban-pinzu/page/{page}/",
        "category_douban_gouzu": "/category-douban-gouzu/" if page <= 1 else f"/category-douban-gouzu/page/{page}/",
        "category_douban_maobathtub": "/category-douban-maobathtub/" if page <= 1 else f"/category-douban-maobathtub/page/{page}/",
        "category_douban_maolife": "/category-douban-maolife/" if page <= 1 else f"/category-douban-maolife/page/{page}/",
        "category_zuankeba": "/category-zuankeba/" if page <= 1 else f"/category-zuankeba/page/{page}/",
        "category_xinzuanba": "/category-xinzuanba/" if page <= 1 else f"/category-xinzuanba/page/{page}/",
        "category_kuan": "/category-kuan/" if page <= 1 else f"/category-kuan/page/{page}/",
        "category_man": "/category-man/" if page <= 1 else f"/category-man/page/{page}/",
        "latest": "/" if page <= 1 else f"/page/{page}/",
    }
    path = channel_map.get(channel, channel if channel.startswith("/") else "/")
    list_url = urljoin(base, path)

    t0 = time.perf_counter()
    resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    html_text = resp.text

    li_blocks = re.findall(
        r'(<li[^>]*class=["\'][^"\']*article-list[^"\']*["\'][^>]*>.*?</li>)',
        html_text,
        re.S | re.I,
    )

    records: List[Dict[str, Any]] = []
    detail_errors: List[Dict[str, Any]] = []
    detail_fetched = 0
    skip_set = skip_detail_ids or set()

    for li_html in li_blocks:
        if "置顶" in li_html and "/gonggao/" in li_html:
            continue
        m_a = re.search(
            r'<a\b([^>]*href=["\']((?:/[^"\']+\.html|https?://www\.smzdm\.com/p/\d+/?|https?://cu\.manmanbuy\.com/discuxiao_\d+\.aspx))["\'][^>]*)>(.*?)</a>',
            li_html,
            re.S | re.I,
        )
        if not m_a:
            continue
        a_attrs, raw_href, a_inner = m_a.group(1), m_a.group(2), m_a.group(3)
        detail_url = urljoin(base, raw_href)
        m_id = re.search(r"(?:/|discuxiao_)(\d+)(?:\.html|\.aspx|/?$)", raw_href)
        item_id = m_id.group(1) if m_id else raw_href

        title = re.sub(r"<[^>]+>", "", html_lib.unescape(a_inner)).strip()
        catename_m = re.search(r'data-(?:catename|mall_name)=["\']([^"\']*)["\']', a_attrs, re.I)
        price_m = re.search(r'data-price=["\']([^"\']*)["\']', a_attrs, re.I)
        content_preview_m = re.search(r'data-content=["\']([^"\']*)["\']', a_attrs, re.I)
        comments_m = re.search(r'data-comments=["\']([^"\']*)["\']', a_attrs, re.I)
        time_m = re.search(r'<time[^>]*title=["\']([^"\']+)["\'][^>]*>(.*?)</time>', li_html, re.S | re.I)

        catename = html_lib.unescape(catename_m.group(1)).strip() if catename_m else ""
        price_attr = html_lib.unescape(price_m.group(1)).strip() if price_m else ""
        preview_text = html_lib.unescape(content_preview_m.group(1)).strip() if content_preview_m else ""
        pub_time = html_lib.unescape(time_m.group(1)).strip() if time_m else ""
        comments_cnt = int(comments_m.group(1)) if (comments_m and comments_m.group(1).isdigit()) else 0

        tutorial_text = preview_text
        tutorial_images: List[str] = []
        extracted_links: List[Dict[str, Any]] = []
        extracted_tokens: List[str] = []
        raw_payload: Any = li_html
        has_full_detail = False
        extra_hot_metrics: Dict[str, Any] = {"comments": comments_cnt}

        if "cu.manmanbuy.com/discuxiao_" in detail_url:
            # 慢慢买聚合条目在列表属性中已包含完整摘要，discuxiao 页为纯前端跳转壳
            has_full_detail = True
            extracted_links, tutorial_images, extracted_tokens = extract_links_images_and_tokens(
                preview_text or li_html, base_url=detail_url
            )
            extracted_links.append({
                "label": "慢慢买优惠页",
                "url": detail_url,
                "type": "manmanbuy_detail",
                "platform": detect_platform_signal(f"{catename} {title} {preview_text}") or "unknown",
            })
        elif fetch_detail and detail_fetched < max_details and item_id not in skip_set:
            try:
                rd = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
                if rd.status_code == 200:
                    detail_fetched += 1
                    has_full_detail = True
                    det_html = rd.text
                    extra_go_links: List[Dict[str, Any]] = []
                    extra_ld_imgs: List[str] = []
                    if "smzdm.com/p/" in detail_url:
                        m_ld = re.search(
                            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                            det_html,
                            re.S | re.I,
                        )
                        if m_ld:
                            try:
                                ld_obj = json.loads(m_ld.group(1).strip())
                                pub_time = str(ld_obj.get("pubDate") or ld_obj.get("upDate") or pub_time)
                                for ld_img in ld_obj.get("images") or []:
                                    if ld_img and ld_img not in extra_ld_imgs:
                                        extra_ld_imgs.append(ld_img)
                            except Exception:
                                pass
                        # 提取值得买详情页 dataLayer 中的品牌、商城、分类等结构化商品元数据
                        m_brand = re.search(r"['\"]品牌['\"]\s*:\s*['\"]([^'\"]+)['\"]", det_html)
                        m_mall_dl = re.search(r"['\"]商城['\"]\s*:\s*['\"]([^'\"]+)['\"]", det_html)
                        m_cate_dl = re.search(r"['\"]category['\"]\s*:\s*['\"]([^'\"]+)['\"]", det_html)
                        if m_brand:
                            extra_hot_metrics["brand"] = m_brand.group(1).strip()
                        if m_mall_dl and not catename:
                            catename = m_mall_dl.group(1).strip()
                        if m_cate_dl:
                            extra_hot_metrics["category"] = m_cate_dl.group(1).strip()
                        for go_u in dict.fromkeys(re.findall(r"https?://go\.smzdm\.com/[a-zA-Z0-9_/]+", det_html)):
                            extra_go_links.append({
                                "label": "值得买直达跳转链",
                                "url": go_u,
                                "type": "smzdm_go_link",
                                "platform": detect_platform_signal(catename) or "unknown",
                            })
                        m_art = re.search(
                            r'(<article[^>]*class=["\'][^"\']*txt-detail[^"\']*["\'][^>]*>.*?</article>)',
                            det_html,
                            re.S | re.I,
                        )
                    else:
                        m_time_full = re.search(
                            r'<time[^>]*class=["\'][^"\']*time[^"\']*["\'][^>]*title=["\']([^"\']+)["\']',
                            det_html,
                            re.I,
                        )
                        if m_time_full:
                            pub_time = html_lib.unescape(m_time_full.group(1)).strip()
                        m_art = re.search(
                            r'(<div[^>]*class=["\'][^"\']*article-content[^"\']*["\'][^>]*>.*?</div>)\s*(?:<div|<footer|</article)',
                            det_html,
                            re.S | re.I,
                        )
                        if not m_art:
                            m_art = re.search(r"(<article[^>]*>.*?</article>)", det_html, re.S | re.I)

                    if m_art:
                        art_fragment = m_art.group(1)
                        raw_payload = art_fragment
                        extracted_links, tutorial_images, extracted_tokens = extract_links_images_and_tokens(
                            art_fragment, base_url=detail_url
                        )
                        for im in extra_ld_imgs:
                            if im not in tutorial_images:
                                tutorial_images.append(im)
                        extracted_links.extend(extra_go_links)
                        tutorial_text = html_to_tutorial_text(art_fragment, base_url=detail_url)
                        if extra_hot_metrics.get("brand") or extra_hot_metrics.get("category"):
                            meta_prefix = f"【商城:{catename} | 品牌:{extra_hot_metrics.get('brand', '')} | 分类:{extra_hot_metrics.get('category', '')}】"
                            tutorial_text = f"{meta_prefix}\n{tutorial_text}".strip()
                else:
                    detail_errors.append({
                        "item_id": item_id,
                        "detail_url": detail_url,
                        "stage": "detail_fetch",
                        "http_status": rd.status_code,
                        "error_type": f"HTTP_{rd.status_code}",
                        "error": f"详情页返回非200状态码: {rd.status_code}",
                    })
            except Exception as e:
                detail_errors.append({
                    "item_id": item_id,
                    "detail_url": detail_url,
                    "stage": "detail_fetch",
                    "http_status": None,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                tutorial_text = f"{preview_text}\n[详情拉取异常: {type(e).__name__}: {e}]".strip()

        rec = build_deal_record(
            source_site="xianbao_fun",
            source_channel=f"{channel}:{catename}" if catename else channel,
            source_item_id=item_id,
            source_url=detail_url,
            title=title,
            price_text=f"{price_attr}元" if price_attr else "",
            mall_name=catename,
            publish_time=pub_time,
            hot_metrics=extra_hot_metrics,
            tutorial_text=tutorial_text or title,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=raw_payload,
        )
        rec["has_full_detail"] = has_full_detail
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": list_url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "detail_errors": detail_errors,
        "records": records,
    }


def fetch_0818tuan_deals(
    page: int = 1,
    fetch_detail: bool = True,
    max_details: int = 5,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """
    【T0-3】0818团最新线报活动 (`http://www.0818tuan.com/list-1-{page-1}.html`)
    """
    http = session or requests.Session()
    base = "http://www.0818tuan.com"
    page_idx = max(0, int(page) - 1)
    list_url = f"{base}/list-1-{page_idx}.html"

    t0 = time.perf_counter()
    resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    anchors = re.findall(
        r'(<a\b[^>]*href=["\'](/xbhd/(\d+)\.html)["\'][^>]*>.*?</a>)',
        resp.text,
        re.S | re.I,
    )

    records: List[Dict[str, Any]] = []
    detail_errors: List[Dict[str, Any]] = []
    seen_ids: set = set()
    detail_fetched = 0
    skip_set = skip_detail_ids or set()

    for full_a, rel_href, item_id in anchors:
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        detail_url = urljoin(base, rel_href)

        time_m = re.search(r'<span[^>]*class=["\'][^"\']*badge[^"\']*["\'][^>]*>(.*?)</span>', full_a, re.S | re.I)
        pub_time = re.sub(r"<[^>]+>", "", time_m.group(1)).strip() if time_m else ""
        title_m = re.search(r'title=["\']([^"\']+)["\']', full_a, re.I)
        if title_m:
            title = html_lib.unescape(title_m.group(1)).strip()
        else:
            title = re.sub(r"<span[^>]*>.*?</span>", "", full_a, flags=re.S | re.I)
            title = re.sub(r"<[^>]+>", "", html_lib.unescape(title)).strip()

        tutorial_text = title
        tutorial_images: List[str] = []
        extracted_links: List[Dict[str, Any]] = []
        extracted_tokens: List[str] = []
        raw_payload: Any = full_a
        has_full_detail = False

        if fetch_detail and detail_fetched < max_details and item_id not in skip_set:
            try:
                rd = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
                rd.encoding = "utf-8"
                if rd.status_code == 200:
                    detail_fetched += 1
                    has_full_detail = True
                    det_html = rd.text
                    m_full_time = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)", det_html)
                    if m_full_time:
                        pub_time = m_full_time.group(1)

                    m_xb = re.search(
                        r'(<div[^>]*id=["\']xbcontent["\'][^>]*>.*?</div>)\s*(?:<p[^>]*class=["\']pageLink|<div)',
                        det_html,
                        re.S | re.I,
                    )
                    if not m_xb:
                        m_xb = re.search(r'(<div[^>]*class=["\'][^"\']*post-content[^"\']*["\'][^>]*>.*?</div>)', det_html, re.S | re.I)
                    if m_xb:
                        xb_fragment = m_xb.group(1)
                        raw_payload = xb_fragment
                        # 剥离站内底部导航（加入收藏/上一篇/下一篇），避免混入电商凑单链接列表，raw_payload 仍保留完整原始片段
                        clean_xb = re.sub(
                            r"<p[^>]*>\s*(?:<a[^>]*href=[\"'][^\"']*member/fava[^\"']*[\"'][^>]*>.*?</a>|上一篇：.*?|下一篇：.*?)\s*</p>",
                            "",
                            xb_fragment,
                            flags=re.S | re.I,
                        )
                        extracted_links, tutorial_images, extracted_tokens = extract_links_images_and_tokens(
                            clean_xb, base_url=detail_url
                        )
                        tutorial_text = html_to_tutorial_text(clean_xb, base_url=detail_url)
                else:
                    detail_errors.append({
                        "item_id": item_id,
                        "detail_url": detail_url,
                        "stage": "detail_fetch",
                        "http_status": rd.status_code,
                        "error_type": f"HTTP_{rd.status_code}",
                        "error": f"详情页返回非200状态码: {rd.status_code}",
                    })
            except Exception as e:
                detail_errors.append({
                    "item_id": item_id,
                    "detail_url": detail_url,
                    "stage": "detail_fetch",
                    "http_status": None,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                tutorial_text = f"{title}\n[详情拉取异常: {type(e).__name__}: {e}]"

        rec = build_deal_record(
            source_site="0818tuan",
            source_channel="list-1",
            source_item_id=item_id,
            source_url=detail_url,
            title=title,
            publish_time=pub_time,
            tutorial_text=tutorial_text,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=raw_payload,
        )
        rec["has_full_detail"] = has_full_detail
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": list_url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "detail_errors": detail_errors,
        "records": records,
    }


# ==============================================================================
# 3. T1 级高价值补充站点拉取器（什么值得买三路高可用 / 拔草哦 / 网猴线报 / 购物党）
# ==============================================================================
def _enrich_smzdm_detail(
    http: requests.Session,
    item_id: str,
    title: str,
    price_text: str,
    mall_name: str,
    img_url: str,
    pub_time: str,
    raw_card: Any,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """拉取 `https://www.smzdm.com/p/{item_id}/` 详情页，提取 JSON-LD、dataLayer、爆料原文、小编补充与站内凑单引用链。"""
    detail_url = f"https://www.smzdm.com/p/{item_id}/"
    tutorial_text = f"【{mall_name}】{title} | {price_text}".strip()
    tutorial_images: List[str] = [img_url] if img_url else []
    extracted_links: List[Dict[str, Any]] = []
    extracted_tokens: List[str] = []
    raw_payload: Any = raw_card
    hot_metrics: Dict[str, Any] = {}

    rd = http.get(detail_url, headers=DEFAULT_HEADERS, timeout=timeout)
    if rd.status_code == 200:
        det_html = rd.text
        m_ld = re.search(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            det_html,
            re.S | re.I,
        )
        if m_ld:
            try:
                ld_obj = json.loads(m_ld.group(1).strip())
                if not title and ld_obj.get("title"):
                    title = str(ld_obj["title"]).strip()
                pub_time = str(ld_obj.get("pubDate") or ld_obj.get("upDate") or pub_time)
                for ld_img in ld_obj.get("images") or []:
                    if ld_img and ld_img not in tutorial_images:
                        tutorial_images.append(ld_img)
            except Exception:
                pass

        m_brand = re.search(r"['\"]品牌['\"]\s*:\s*['\"]([^'\"]+)['\"]", det_html)
        m_mall_dl = re.search(r"['\"]商城['\"]\s*:\s*['\"]([^'\"]+)['\"]", det_html)
        m_cate_dl = re.search(r"['\"]category['\"]\s*:\s*['\"]([^'\"]+)['\"]", det_html)
        if m_brand:
            hot_metrics["brand"] = m_brand.group(1).strip()
        if m_mall_dl and not mall_name:
            mall_name = m_mall_dl.group(1).strip()
        if m_cate_dl:
            hot_metrics["category"] = m_cate_dl.group(1).strip()

        m_art = re.search(
            r'(<article[^>]*class=["\'][^"\']*txt-detail[^"\']*["\'][^>]*>.*?</article>)',
            det_html,
            re.S | re.I,
        )
        if m_art:
            art_html = m_art.group(1)
            raw_payload = art_html
            links, imgs, toks = extract_links_images_and_tokens(art_html, base_url=detail_url)
            extracted_links.extend(links)
            for im in imgs:
                if im not in tutorial_images:
                    tutorial_images.append(im)
            extracted_tokens.extend(toks)
            art_text = html_to_tutorial_text(art_html, base_url=detail_url)
            meta_str = f"【{mall_name} | 品牌:{hot_metrics.get('brand', '')} | 分类:{hot_metrics.get('category', '')}】" if hot_metrics else f"【{mall_name}】"
            tutorial_text = f"{meta_str}{title} | {price_text}\n{art_text}".strip()

        for go_u in dict.fromkeys(re.findall(r"https?://go\.smzdm\.com/[a-zA-Z0-9_/]+", det_html)):
            extracted_links.append({
                "label": "值得买直达跳转链",
                "url": go_u,
                "type": "smzdm_go_link",
                "platform": detect_platform_signal(mall_name) or "unknown",
            })

    rec = build_deal_record(
        source_site="smzdm",
        source_channel="smzdm_good_price",
        source_item_id=item_id,
        source_url=detail_url,
        title=title,
        price_text=price_text,
        image_url=tutorial_images[0] if tutorial_images else img_url,
        mall_name=mall_name,
        publish_time=pub_time,
        hot_metrics=hot_metrics,
        tutorial_text=tutorial_text,
        tutorial_images=tutorial_images,
        extracted_links=extracted_links,
        extracted_tokens=extracted_tokens,
        raw_payload=raw_payload,
    )
    rec["has_full_detail"] = True
    return rec


def fetch_smzdm_faxian_deals(
    sort: str = "hot_3h",
    page: int = 1,
    fetch_detail: bool = True,
    max_details: int = 5,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """
    【T1-1】什么值得买好价拉取器（支持三路高可用自动切换：faxian SSR -> 线报酷值得买实时镜像 + www.smzdm.com/p/{id}/ 详情）
    - 当 `faxian.smzdm.com` 子域名因突发并发触发腾讯防水墙 (`TCaptcha.js`) 时，自动无缝切换至
      `https://new.xianbao.fun/category-zhidemai/` 获取最新值得买好价 ID、价格、商城与品牌，并直连
      未受限的 `https://www.smzdm.com/p/{id}/` 详情页提取完整 `爆料原文` 与 `小编补充`！
    """
    http = session or requests.Session()
    sort_map = {
        "hot_3h": f"https://faxian.smzdm.com/h2s0t0f0c0p{page}/",
        "hot_24h": f"https://faxian.smzdm.com/h3s0t0f0c0p{page}/",
        "latest": "https://faxian.smzdm.com/" if page <= 1 else f"https://faxian.smzdm.com/p{page}/",
    }
    list_url = sort_map.get(sort, sort_map["hot_3h"])

    t0 = time.perf_counter()
    resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=timeout)
    cards = re.findall(
        r'(<div[^>]*class=["\'][^"\']*feed-block-ver[^"\']*["\'][^>]*>.*?</div>\s*</div>)',
        resp.text,
        re.S | re.I,
    )

    # 若 faxian.smzdm.com 子域名处于 TCaptcha 冷却期，自动无缝切换至线报酷值得买镜像列表 + www.smzdm.com/p/{id}/ 官方详情
    if not cards or "TCaptcha.js" in resp.text:
        mirror_res = fetch_xianbao_fun_deals(
            channel="zhidemai_hot" if sort in ("hot_3h", "hot_24h") else "zhidemai",
            page=page,
            fetch_detail=fetch_detail,
            max_details=max_details,
            session=http,
            timeout=timeout,
            skip_detail_ids=skip_detail_ids,
        )
        for rec in mirror_res.get("records") or []:
            rec["source_site"] = "smzdm"
            rec["source_channel"] = f"{sort}_via_mirror"
        mirror_res["failover_used"] = "xianbao_zhidemai_mirror"
        return mirror_res

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    records: List[Dict[str, Any]] = []
    detail_fetched = 0
    skip_set = skip_detail_ids or set()

    for card_html in cards:
        m_link = re.search(r'https://www\.smzdm\.com/p/(\d+)/?', card_html)
        if not m_link:
            continue
        item_id = m_link.group(1)
        detail_url = f"https://www.smzdm.com/p/{item_id}/"

        m_title = re.search(r'<h5[^>]*class=["\'][^"\']*feed-ver-title[^"\']*["\'][^>]*>(.*?)</h5>', card_html, re.S | re.I)
        title = re.sub(r"<[^>]+>", "", html_lib.unescape(m_title.group(1))).strip() if m_title else ""

        m_price = re.search(r'<div[^>]*class=["\'][^"\']*z-highlight[^"\']*["\'][^>]*>(.*?)</div>', card_html, re.S | re.I)
        price_text = re.sub(r"<[^>]+>", "", html_lib.unescape(m_price.group(1))).strip() if m_price else ""

        m_mall = re.search(r'class=["\'][^"\']*tag-bottom-right[^"\']*["\'][^>]*>(.*?)</a>', card_html, re.S | re.I)
        mall_name = re.sub(r"<[^>]+>", "", html_lib.unescape(m_mall.group(1))).strip() if m_mall else ""

        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""
        if img_url.startswith("//"):
            img_url = "https:" + img_url

        if fetch_detail and detail_fetched < max_details and item_id not in skip_set:
            try:
                rec = _enrich_smzdm_detail(
                    http, item_id, title, price_text, mall_name, img_url, "", card_html, timeout=timeout
                )
                rec["source_channel"] = sort
                records.append(rec)
                detail_fetched += 1
                continue
            except Exception:
                pass

        rec = build_deal_record(
            source_site="smzdm",
            source_channel=sort,
            source_item_id=item_id,
            source_url=detail_url,
            title=title,
            price_text=price_text,
            image_url=img_url,
            mall_name=mall_name,
            tutorial_text=f"【{mall_name}】{title} | {price_text}",
            tutorial_images=[img_url] if img_url else [],
            raw_payload=card_html,
        )
        rec["has_full_detail"] = False
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "records": records,
    }


def fetch_bacaoo_deals(
    fetch_detail: bool = True,
    max_details: int = 5,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """
    【T1-2】拔草哦国内优惠 (`https://youhui.bacaoo.com/` + `/p/{id}`)
    """
    http = session or requests.Session()
    base = "https://youhui.bacaoo.com"
    t0 = time.perf_counter()
    resp = http.get(base + "/", headers=DEFAULT_HEADERS, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    nav_ids = {"2642", "40625", "59702"}
    all_pids = list(dict.fromkeys(re.findall(r'href=["\']/p/(\d+)["\']', resp.text)))
    item_ids = [pid for pid in all_pids if pid not in nav_ids]

    records: List[Dict[str, Any]] = []
    detail_errors: List[Dict[str, Any]] = []
    detail_fetched = 0
    skip_set = skip_detail_ids or set()

    for pid in item_ids[: max(max_details, 15)]:
        detail_url = f"{base}/p/{pid}"
        m_anchor = re.search(rf'<a[^>]*href=["\']/p/{pid}["\'][^>]*>(.*?)</a>', resp.text, re.S | re.I)
        title = re.sub(r"<[^>]+>", "", html_lib.unescape(m_anchor.group(1))).strip() if m_anchor else f"拔草哦优惠 #{pid}"

        pub_time = ""
        tutorial_text = title
        tutorial_images: List[str] = []
        extracted_links: List[Dict[str, Any]] = []
        extracted_tokens: List[str] = []
        raw_payload: Any = ""
        has_full_detail = False

        if fetch_detail and detail_fetched < max_details and pid not in skip_set:
            try:
                rd = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": base + "/"}, timeout=timeout)
                if rd.status_code == 200:
                    detail_fetched += 1
                    has_full_detail = True
                    det_html = rd.text
                    m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>|<title>(.*?)</title>", det_html, re.S | re.I)
                    if m_h1:
                        raw_t = (m_h1.group(1) or m_h1.group(2) or "").split("_拔草哦")[0]
                        title = re.sub(r"<[^>]+>", "", html_lib.unescape(raw_t)).strip() or title

                    m_time = re.search(r'<div[^>]*class=["\'][^"\']*p-time[^"\']*["\'][^>]*>(.*?)</div>', det_html, re.S | re.I)
                    if m_time:
                        pub_time = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(m_time.group(1)))).strip()

                    m_desc = re.search(
                        r'(<div[^>]*class=["\'][^"\']*describe\s+art-con[^"\']*["\'][^>]*>.*?</div>\s*</div>)',
                        det_html,
                        re.S | re.I,
                    )
                    if m_desc:
                        desc_html = m_desc.group(1)
                        raw_payload = desc_html
                        extracted_links, tutorial_images, extracted_tokens = extract_links_images_and_tokens(
                            desc_html, base_url=detail_url
                        )
                        tutorial_text = html_to_tutorial_text(desc_html, base_url=detail_url)
                else:
                    detail_errors.append({
                        "item_id": pid,
                        "detail_url": detail_url,
                        "stage": "detail_fetch",
                        "http_status": rd.status_code,
                        "error_type": f"HTTP_{rd.status_code}",
                        "error": f"详情页返回非200状态码: {rd.status_code}",
                    })
            except Exception as e:
                detail_errors.append({
                    "item_id": pid,
                    "detail_url": detail_url,
                    "stage": "detail_fetch",
                    "http_status": None,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                tutorial_text = f"{title}\n[详情拉取异常: {type(e).__name__}: {e}]"

        rec = build_deal_record(
            source_site="bacaoo",
            source_channel="youhui",
            source_item_id=pid,
            source_url=detail_url,
            title=title,
            publish_time=pub_time,
            tutorial_text=tutorial_text,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=raw_payload,
        )
        rec["has_full_detail"] = has_full_detail
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": f"{base}/",
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "detail_errors": detail_errors,
        "records": records,
    }


def fetch_iehou_deals(
    mode: str = "dayhot",
    fetch_detail: bool = True,
    max_details: int = 5,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """
    【T1-3】网猴线报 (`https://iehou.com/page-dayhot.htm` 或首页 `https://iehou.com/`)
    """
    http = session or requests.Session()
    base = "https://iehou.com"
    list_url = f"{base}/page-dayhot.htm" if mode == "dayhot" else f"{base}/"

    t0 = time.perf_counter()
    resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    items = re.findall(
        r'<a[^>]*href=["\'](?:https?://iehou\.com/)?(xianbao-(\d+)\.htm)["\'][^>]*title=["\']([^"\']+)["\']',
        resp.text,
        re.I,
    )

    records: List[Dict[str, Any]] = []
    detail_errors: List[Dict[str, Any]] = []
    seen_ids: set = set()
    detail_fetched = 0
    skip_set = skip_detail_ids or set()

    for rel_href, item_id, raw_title in items:
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        detail_url = f"{base}/{rel_href}"
        title = html_lib.unescape(raw_title).strip()

        pub_time = ""
        tutorial_text = title
        tutorial_images: List[str] = []
        extracted_links: List[Dict[str, Any]] = []
        extracted_tokens: List[str] = []
        raw_payload: Any = ""
        has_full_detail = False

        if fetch_detail and detail_fetched < max_details and item_id not in skip_set:
            try:
                rd = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
                if rd.status_code == 200:
                    detail_fetched += 1
                    has_full_detail = True
                    det_html = rd.text
                    m_msg = re.search(
                        r'(<div[^>]*class=["\'][^"\']*thread-content\s+message[^"\']*["\'][^>]*>.*?</div>)',
                        det_html,
                        re.S | re.I,
                    )
                    if m_msg:
                        msg_html = m_msg.group(1)
                        raw_payload = msg_html
                        extracted_links, tutorial_images, extracted_tokens = extract_links_images_and_tokens(
                            msg_html, base_url=detail_url
                        )
                        tutorial_text = html_to_tutorial_text(msg_html, base_url=detail_url)
                else:
                    detail_errors.append({
                        "item_id": item_id,
                        "detail_url": detail_url,
                        "stage": "detail_fetch",
                        "http_status": rd.status_code,
                        "error_type": f"HTTP_{rd.status_code}",
                        "error": f"详情页返回非200状态码: {rd.status_code}",
                    })
            except Exception as e:
                detail_errors.append({
                    "item_id": item_id,
                    "detail_url": detail_url,
                    "stage": "detail_fetch",
                    "http_status": None,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                tutorial_text = f"{title}\n[详情拉取异常: {type(e).__name__}: {e}]"

        rec = build_deal_record(
            source_site="iehou",
            source_channel=mode,
            source_item_id=item_id,
            source_url=detail_url,
            title=title,
            publish_time=pub_time,
            tutorial_text=tutorial_text,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=raw_payload,
        )
        rec["has_full_detail"] = has_full_detail
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": list_url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "detail_errors": detail_errors,
        "records": records,
    }


def _decode_gwdang_data_param(b64_str: str) -> Dict[str, Any]:
    """
    解码购物党 `<a href="/v2/zhi/detail/1?price=1&data=...">` 中的 Base64 / Base64+zlib JSON 载荷。
    - `JTdC...` 为 Base64(URL-encoded JSON)，包含 `dp_id`, `site_id`, `title`, `img_url`, `price`(分), `update_time` 等；
    - `eJy...` 为 Base64(zlib(URL-encoded JSON))，包含 `dp_id`, `price_info`, `usp`(历史低价标签), `buy_steps`(含领券与电商购买链接的HTML), `brand`, `shop_name`, `site_name`。
    """
    import base64
    import zlib
    from urllib.parse import unquote

    s = html_lib.unescape(str(b64_str or "")).strip()
    if not s:
        return {}
    s = unquote(s)
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    try:
        raw_bytes = base64.b64decode(s)
    except Exception:
        return {}
    try:
        decompressed = zlib.decompress(raw_bytes)
        text = unquote(decompressed.decode("utf-8", errors="replace"))
    except Exception:
        text = unquote(raw_bytes.decode("utf-8", errors="replace"))
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def fetch_gwdang_discounts(
    mode: str = "0-hour_3",
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T1-4】购物党折扣精选 (`https://www.gwdang.com/v2/home/discount/{mode}`)
    - 推荐高含金量模式：
      * `"0-hour_3"`   -> 3小时最热好价
      * `"0-lowest"`   -> N天历史低价
      * `"0-s_lowest"` -> 全网同款最低价
      * `"0-lower_11"` -> 低于双11价
      * `"0-latest"`   -> 全站最新好价折扣
    - 核心升级：自动解码页面内嵌的 `data=` Base64 + zlib 压缩 JSON 载荷，100% 提取完整商品标题、
      品牌、店铺、商城、历史低价标签 (`usp`) 以及包含领券链 (`coupon.m.jd.com`) 与商品购买链
      (`jingfen.jd.com` / `item.jd.com` / 天猫淘宝) 的完整 `buy_steps` 凑单教程！
    """
    import hashlib as _hashlib

    http = session or requests.Session()
    url = f"https://www.gwdang.com/v2/home/discount/{mode}"
    t0 = time.perf_counter()
    resp = http.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # 1. 解码所有 data= 参数（先 html.unescape 将 &amp;data= 还原为 &data=）并按 dp_id 合并两段 JSON
    unescaped_html = html_lib.unescape(resp.text)
    merged_by_dp: Dict[str, Dict[str, Any]] = {}
    for m_data in re.finditer(r'[?&]data=([^&"\'\s>]+)', unescaped_html):
        decoded = _decode_gwdang_data_param(m_data.group(1))
        dp_id = str(decoded.get("dp_id") or "").strip()
        if not dp_id:
            continue
        if dp_id not in merged_by_dp:
            merged_by_dp[dp_id] = {}
        merged_by_dp[dp_id].update(decoded)

    records: List[Dict[str, Any]] = []
    if merged_by_dp:
        for dp_id, info in merged_by_dp.items():
            title = str(info.get("title") or "").strip()
            price_info = str(info.get("price_info") or "").strip()
            price_val, _ = extract_first_price(price_info)
            if price_val is None:
                raw_p = info.get("price")
                if isinstance(raw_p, (int, float)) and raw_p > 0:
                    price_val = round(float(raw_p) / 100.0, 2) if isinstance(raw_p, int) and raw_p > 1000 else round(float(raw_p), 2)

            img_url = str(info.get("img_url") or "").strip()
            site_name = str(info.get("site_name") or "").strip()
            shop_name = str(info.get("shop_name") or "").strip()
            brand = str(info.get("brand") or "").strip()
            mall_display = f"{site_name} ({shop_name})" if (site_name and shop_name) else (site_name or shop_name)
            usp_list = [str(u).strip() for u in (info.get("usp") or []) if str(u or "").strip()]
            buy_steps_html = str(info.get("buy_steps") or "").strip()

            links, step_imgs, tokens = extract_links_images_and_tokens(buy_steps_html, base_url=url)
            if img_url and img_url not in step_imgs:
                step_imgs.insert(0, img_url)

            # 从 dp_id (如 "10048223326472-3") 提取原始平台商品 SKU 链接作为双保险
            raw_sku = dp_id.split("-")[0].strip()
            site_id_str = str(info.get("site_id") or (dp_id.split("-")[1] if "-" in dp_id else "")).strip()
            explicit_plat = detect_platform_signal(f"{site_name} {shop_name}")
            if not explicit_plat:
                if site_id_str == "3" or "京东" in site_name:
                    explicit_plat = "jd"
                elif site_id_str in ("2", "83") or any(k in site_name for k in ("天猫", "淘宝")):
                    explicit_plat = "taobao"
                elif "拼多多" in site_name:
                    explicit_plat = "pdd"

            if raw_sku.isdigit():
                if explicit_plat == "jd":
                    sku_url = f"https://item.jd.com/{raw_sku}.html"
                    if not any(lk.get("url") == sku_url for lk in links):
                        links.append({
                            "label": "京东商品直达页(dp_id解码)",
                            "url": sku_url,
                            "item_id": raw_sku,
                            "type": "gwdang_dp_sku",
                            "platform": "jd",
                        })
                elif explicit_plat == "taobao":
                    sku_url = f"https://detail.tmall.com/item.htm?id={raw_sku}"
                    if not any(lk.get("url") == sku_url for lk in links):
                        links.append({
                            "label": "淘宝/天猫商品直达页(dp_id解码)",
                            "url": sku_url,
                            "item_id": raw_sku,
                            "type": "gwdang_dp_sku",
                            "platform": "taobao",
                        })

            steps_text = html_to_tutorial_text(buy_steps_html, base_url=url)
            usp_str = f" | 低价标签: {', '.join(usp_list)}" if usp_list else ""
            brand_str = f" | 品牌: {brand}" if brand else ""
            tutorial_text = f"【{mall_display or '购物党精选'}{brand_str}{usp_str}】{title}\n到手价: {price_info or (f'{price_val}元' if price_val else '')}\n购买步骤: {steps_text}".strip()

            upd_ts = info.get("update_time")
            pub_time = ""
            if isinstance(upd_ts, (int, float)) and upd_ts > 1000000000:
                pub_time = datetime.fromtimestamp(upd_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            rec = build_deal_record(
                source_site="gwdang",
                source_channel=mode,
                source_item_id=dp_id,
                source_url=f"https://www.gwdang.com/v2/zhi/detail/1?dp_id={dp_id}",
                title=title or f"购物党好价 #{dp_id}",
                price_text=price_info or (f"{price_val}元" if price_val else ""),
                price=price_val,
                image_url=img_url,
                mall_name=mall_display,
                publish_time=pub_time,
                hot_metrics={
                    "usp": usp_list,
                    "brand": brand,
                    "shop_name": shop_name,
                    "site_name": site_name,
                    "self_support": info.get("self_support"),
                },
                tutorial_text=tutorial_text,
                tutorial_images=step_imgs,
                extracted_links=links,
                extracted_tokens=tokens,
                raw_payload=info,
                explicit_platform=explicit_plat,
            )
            if raw_sku.isdigit() and not rec.get("candidate_goods_id"):
                rec["candidate_goods_id"] = raw_sku
            rec["has_full_detail"] = True
            records.append(rec)
    else:
        # 兜底：若页面未匹配到 data= 参数，则回退至 DOM 卡片解析
        blocks = re.findall(
            r'(<div[^>]*class=["\'][^"\']*gwd-row\s+gwd-align\s+item-img[^"\']*["\'][^>]*>.*?<div[^>]*class=["\'][^"\']*site-info[^"\']*["\'][^>]*>.*?</div>\s*</div>)',
            resp.text,
            re.S | re.I,
        )
        for idx, blk in enumerate(blocks):
            m_img = re.search(
                r'<img[^>]+(?:data-src|data-original)=["\']([^"\']+)["\']|<img[^>]+src=["\']([^"\']+)["\']',
                blk,
                re.I,
            )
            img_url = ""
            if m_img:
                img_url = html_lib.unescape(m_img.group(1) or m_img.group(2) or "").strip()
            if "loading_" in img_url:
                m_real = re.search(r'data-src=["\']([^"\']+)["\']', blk, re.I)
                if m_real:
                    img_url = html_lib.unescape(m_real.group(1)).strip()

            m_shop = re.search(r'class=["\'][^"\']*shop-name[^"\']*["\'][^>]*>(.*?)</span>', blk, re.S | re.I)
            mall_name = re.sub(r"<[^>]+>", "", html_lib.unescape(m_shop.group(1))).strip() if m_shop else ""

            spans = [
                re.sub(r"<[^>]+>", "", html_lib.unescape(s)).strip()
                for s in re.findall(r"<span[^>]*>(.*?)</span>", blk, re.S | re.I)
            ]
            spans = [s for s in spans if s]
            price_span = next((s for s in spans if "元" in s or "¥" in s), "")
            title_candidates = [
                s for s in spans if s != price_span and s != mall_name and s not in ("自营", "旗舰店") and len(s) >= 4
            ]
            title = title_candidates[0] if title_candidates else (spans[0] if spans else f"购物党好价 #{idx + 1}")

            jd_key = extract_jd_image_key(img_url)
            stable_id = jd_key or _hashlib.md5(f"{title}|{img_url}".encode("utf-8")).hexdigest()[:16]

            rec = build_deal_record(
                source_site="gwdang",
                source_channel=mode,
                source_item_id=stable_id,
                source_url=url,
                title=title,
                price_text=price_span,
                image_url=img_url,
                mall_name=mall_name,
                tutorial_text=f"【{mall_name or '购物党精选'}】{title}\n优惠价格: {price_span} (榜单: {mode})",
                tutorial_images=[img_url] if img_url else [],
                raw_payload=blk,
            )
            rec["has_full_detail"] = True
            records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": getattr(resp, "url", None) or url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": len(records),
        "detail_errors": [],
        "records": records,
    }


def fetch_zhetaoke_xianbao_groups(
    plat: str = "",
    group_id: str = "",
    page: int = 1,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T0-4A】折淘客 150+ 个真实 QQ/微信采集群实时线报大厅 (`https://www.zhetaoke.com/xianbao.aspx`)
    - 免登录、零风控，单次 GET 直接返回 60 条完整群聊线报（无需二次请求详情页！）。
    - 参数说明：
      * `plat`: `""` (全平台综合群流), `"1"` (淘宝全群汇总), `"2"` (京东全群汇总), `"3"` (拼多多全群汇总)
      * `group_id`: 指定单个采集群 ID（如 `"10000030"` 新京东带主图采集群、`"1067573294"` 云线报群9京东、
        `"725556337"` 云线报群5综合、`"10000072"` 优质猫超线报群、`"10000176"` 宝妈羊毛群③ 等）
    """
    http = session or requests.Session()
    url = "https://www.zhetaoke.com/xianbao.aspx"
    params = {
        "id": str(group_id or ""),
        "page": str(page),
        "q": "",
        "plat": str(plat or ""),
    }
    t0 = time.perf_counter()
    resp = http.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    cards = re.findall(
        r'(<div\s+class=["\']sc-product["\'][^>]*>.*?<div\s+class=["\']product-bottom["\'][^>]*>.*?</div>\s*</div>\s*</div>)',
        resp.text,
        re.S | re.I,
    )

    plat_label_map = {"1": "tb_groups", "2": "jd_groups", "3": "pdd_groups", "": "all_groups"}
    base_channel = f"group_{group_id}" if group_id else plat_label_map.get(str(plat), f"plat_{plat}")

    records: List[Dict[str, Any]] = []
    for card_html in cards:
        m_id = re.search(r'class=["\'][^"\']*copytext-btn1[^"\']*["\'][^>]*data-id=["\'](\d+)["\']', card_html, re.I)
        if not m_id:
            m_id = re.search(r'data-id=["\'](\d+)["\']', card_html, re.I)
        item_id = m_id.group(1) if m_id else ""
        if not item_id:
            continue

        m_tit = re.search(r'<div\s+class=["\']product-tit1["\'][^>]*>(.*?)</div>', card_html, re.S | re.I)
        tit_html = m_tit.group(1) if m_tit else ""
        msg_with_lines = re.sub(r"<br\s*/?>", "\n", tit_html, flags=re.I)
        raw_msg = html_lib.unescape(re.sub(r"<[^>]+>", "", msg_with_lines)).strip()
        if not raw_msg:
            continue

        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, re.I)
        img_url = ""
        if m_img:
            cand_img = html_lib.unescape(m_img.group(1)).strip()
            if cand_img and "loading.png" not in cand_img:
                img_url = ("https:" + cand_img) if cand_img.startswith("//") else urljoin(url, cand_img)

        m_time = re.search(r'<p\s+class=["\']month-sales["\'][^>]*>(.*?)</p>', card_html, re.S | re.I)
        pub_time = ""
        if m_time:
            t_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(m_time.group(1)))).strip()
            m_dt = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)", t_txt)
            pub_time = m_dt.group(1) if m_dt else t_txt

        m_grp = re.search(
            r'<div\s+class=["\']product-bottom["\'][^>]*>.*?<a[^>]+href=["\']/xianbao\.aspx\?id=(\d+)[^"\']*["\'][^>]*>(.*?)</a>',
            card_html,
            re.S | re.I,
        )
        src_grp_id = m_grp.group(1).strip() if m_grp else str(group_id or "")
        src_grp_name = re.sub(r"<[^>]+>", "", html_lib.unescape(m_grp.group(2))).strip() if m_grp else ""

        links, imgs, tokens = extract_links_images_and_tokens(tit_html, base_url=url)
        if img_url and img_url not in imgs:
            imgs.insert(0, img_url)

        # 提取首行有效商品描述作为标题，完整保留群聊多行教程在 tutorial_text
        lines = [ln.strip() for ln in raw_msg.splitlines() if ln.strip()]
        title_line = ""
        for ln in lines:
            if not ln.startswith("http") and not re.fullmatch(r"[￥¥$€₴£¢()（）!！/A-Za-z0-9\s_-]{6,26}", ln):
                title_line = ln
                break
        if not title_line:
            title_line = lines[0] if lines else f"折淘客群线报 #{item_id}"
        title_clean = re.sub(r"https?://\S+", "", title_line).strip() or title_line[:80]

        explicit_plat = None
        if str(plat) == "2" or "京东" in src_grp_name:
            explicit_plat = "jd"
        elif str(plat) == "1" or any(k in src_grp_name for k in ("淘宝", "猫超", "天猫")):
            explicit_plat = "taobao"
        elif str(plat) == "3" or "拼多多" in src_grp_name:
            explicit_plat = "pdd"

        grp_prefix = f"【来源采集群: {src_grp_name} (群ID:{src_grp_id})】\n" if src_grp_name else ""
        tutorial_text = f"{grp_prefix}{raw_msg}".strip()

        rec = build_deal_record(
            source_site="zhetaoke",
            source_channel=f"{base_channel}:{src_grp_name}" if src_grp_name else base_channel,
            source_item_id=f"xb_{item_id}",
            source_url=f"https://www.zhetaoke.com/xianbao.aspx?id={src_grp_id or ''}&plat={plat or ''}",
            title=title_clean[:120],
            image_url=img_url,
            mall_name=src_grp_name or "折淘客云线报群",
            publish_time=pub_time,
            hot_metrics={
                "group_id": src_grp_id,
                "group_name": src_grp_name,
                "plat_filter": str(plat or "all"),
            },
            tutorial_text=tutorial_text,
            tutorial_images=imgs,
            extracted_links=links,
            extracted_tokens=tokens,
            raw_payload=card_html,
            explicit_platform=explicit_plat,
        )
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": getattr(resp, "url", None) or url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": len(records),
        "detail_errors": [],
        "records": records,
    }


def fetch_zhetaoke_rank_list(
    mode: str = "dongdong",
    page: int = 1,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T0-4B】折淘客咚咚抢 & 全天实时热销爆款榜 (`https://www.zhetaoke.com/list/dongdong.html` / `/list/love_1_0_1.html`)
    - 100% 包含 `https://uland.taobao.com/item/edetail?id=...` 二合一领券链与淘宝 46 位加密 `data-goods-id`。
    """
    http = session or requests.Session()
    if mode == "dongdong":
        url = "https://www.zhetaoke.com/list/dongdong.html" if page <= 1 else f"https://www.zhetaoke.com/list/dongdong_{page}.html"
    else:
        url = f"https://www.zhetaoke.com/list/love_1_0_{page}.html"

    t0 = time.perf_counter()
    resp = http.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    cards = re.split(r'<div\s+class=["\']sc-product["\'][^>]*>', resp.text, flags=re.I)[1:]

    records: List[Dict[str, Any]] = []
    for card_html in cards:
        m_btn = re.search(
            r'data-goods-id=["\']([^"\']+)["\'][^>]*data-id=["\'](\d+)["\']',
            card_html,
            re.I,
        )
        enc_goods_id = m_btn.group(1).strip() if m_btn else ""
        ztk_id = m_btn.group(2).strip() if m_btn else ""

        m_uland = re.search(r'href=["\'](https?://uland\.taobao\.com/[^"\']+)["\']', card_html, re.I)
        uland_url = html_lib.unescape(m_uland.group(1)).strip() if m_uland else (
            f"https://uland.taobao.com/item/edetail?id={enc_goods_id}" if enc_goods_id else ""
        )
        if not ztk_id and not enc_goods_id:
            continue

        m_tit = re.search(r'<div\s+class=["\']product-tit["\'][^>]*>.*?<a[^>]*>(.*?)</a>', card_html, re.S | re.I)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_lib.unescape(m_tit.group(1)))).strip() if m_tit else f"折淘客爆款 #{ztk_id}"

        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, re.I)
        img_url = ""
        if m_img:
            cand_img = html_lib.unescape(m_img.group(1)).strip()
            if cand_img and "loading.png" not in cand_img:
                img_url = ("https:" + cand_img) if cand_img.startswith("//") else urljoin(url, cand_img)

        m_price = re.search(r'<span\s+class=["\']price-num["\'][^>]*>\s*([\d.]+)', card_html, re.I)
        price_str = m_price.group(1).strip() if m_price else ""
        try:
            price_val = float(price_str) if price_str else None
        except ValueError:
            price_val = None

        m_sales = re.search(r'<p\s+class=["\']month-sales["\'][^>]*>.*?<span>(\d+)</span>', card_html, re.S | re.I)
        sales_cnt = int(m_sales.group(1)) if m_sales else 0

        links = []
        if uland_url:
            links.append({
                "label": title or "淘宝二合一领券页",
                "url": uland_url,
                "item_id": enc_goods_id,
                "type": "tb_encrypted_id",
                "platform": "taobao",
            })

        rec = build_deal_record(
            source_site="zhetaoke",
            source_channel=f"rank_{mode}",
            source_item_id=f"rank_{ztk_id or enc_goods_id[:24]}",
            source_url=uland_url or url,
            title=title,
            price_text=f"券后{price_str}元" if price_str else "",
            price=price_val,
            image_url=img_url,
            mall_name="淘宝/天猫",
            hot_metrics={"month_sales": sales_cnt, "rank_mode": mode},
            tutorial_text=f"【折淘客{mode}爆款】{title}\n券后价: {price_str}元 (月销:{sales_cnt})\n二合一落地页: {uland_url}\n加密ID: {enc_goods_id}".strip(),
            tutorial_images=[img_url] if img_url else [],
            extracted_links=links,
            extracted_tokens=[enc_goods_id] if enc_goods_id else [],
            raw_payload=card_html,
            explicit_platform="taobao",
        )
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": getattr(resp, "url", None) or url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": len(records),
        "detail_errors": [],
        "records": records,
    }


def fetch_guangdiu_deals(
    channel: str = "latest",
    page: int = 1,
    fetch_detail: bool = True,
    max_details: int = 15,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
    skip_detail_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """
    【T0-5】逛丢全网实时折扣与值得买明文还原源 (`https://guangdiu.com/`)
    - 重大价值：逛丢实时同步全网及“什么值得买”好价，并在服务端将值得买的加密链还原为明文
      `to.php?u=https%3A%2F%2Fitem.jd.com%2F{sku}.html` 或 `go.php?id={id}` 中的 `union-click.jd.com` / `s.click.taobao.com`！
    - 支持频道 `channel`:
      * `"latest"` -> `https://guangdiu.com/` 或 `https://guangdiu.com/index.php?p={page}` (国内实时折扣流)
      * `"cheaps"` -> `https://guangdiu.com/cheaps.php` (九块九白菜好价流)
    """
    http = session or requests.Session()
    base = "https://guangdiu.com"
    if channel == "cheaps":
        list_url = f"{base}/cheaps.php" if page <= 1 else f"{base}/cheaps.php?p={page}"
    else:
        list_url = f"{base}/" if page <= 1 else f"{base}/index.php?p={page}"

    t0 = time.perf_counter()
    resp = http.get(list_url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    blocks = re.findall(
        r'(<div\s+class=["\']gooditem\b[^"\']*["\'][^>]*>.*?<div\s+class=["\']infocontent["\'][^>]*>.*?</div>\s*</div>\s*</div>\s*</div>)',
        resp.text,
        re.S | re.I,
    )
    if not blocks:
        # 兼容 cheaps.php 的精简卡片结构
        blocks = re.findall(
            r'(<div\s+class=["\'][^"\']*\bgooditem\b[^"\']*["\'][^>]*>.*?detail\.php\?id=\d+.*?</div>\s*</div>)',
            resp.text,
            re.S | re.I,
        )

    records: List[Dict[str, Any]] = []
    seen_ids: set = set()
    detail_fetched = 0
    detail_errors: List[Dict[str, Any]] = []
    skip_set = skip_detail_ids or set()

    for blk in blocks:
        m_a = re.search(
            r'<a[^>]+href=["\'](?:https?://guangdiu\.com/)?detail\.php\?id=(\d+)["\'][^>]*>(.*?)</a>',
            blk,
            re.S | re.I,
        )
        if not m_a:
            continue
        item_id = m_a.group(1).strip()
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        detail_url = f"{base}/detail.php?id={item_id}"
        go_url = f"{base}/go.php?id={item_id}"

        a_inner = m_a.group(2)
        m_emph = re.search(r'<span\s+class=["\']emphricepart["\'][^>]*>(.*?)</span>', a_inner, re.S | re.I)
        price_span = re.sub(r"<[^>]+>", "", html_lib.unescape(m_emph.group(1))).strip() if m_emph else ""
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_lib.unescape(a_inner))).strip()

        m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blk, re.I)
        img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

        m_mall = re.search(r'class=["\'][^"\']*rightmallname[^"\']*["\'][^>]*>(.*?)</a>', blk, re.S | re.I)
        mall_name = re.sub(r"<[^>]+>", "", html_lib.unescape(m_mall.group(1))).strip() if m_mall else ""

        m_abs = re.search(r'<div\s+class=["\']abstractcontent["\'][^>]*>(.*?)</div>', blk, re.S | re.I)
        preview_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(m_abs.group(1)))).strip() if m_abs else ""

        tutorial_text = f"【{mall_name or '逛丢好价'}】{title}\n{preview_text}".strip()
        tutorial_images: List[str] = [img_url] if img_url else []
        extracted_links: List[Dict[str, Any]] = []
        extracted_tokens: List[str] = []
        raw_payload: Any = blk
        has_full_detail = False

        if fetch_detail and detail_fetched < max_details and item_id not in skip_set:
            try:
                rd = http.get(detail_url, headers={**DEFAULT_HEADERS, "Referer": list_url}, timeout=timeout)
                rd.encoding = "utf-8"
                if rd.status_code == 200:
                    detail_fetched += 1
                    has_full_detail = True
                    det_html = rd.text

                    m_dabs = re.search(
                        r'(<div[^>]+id=["\']dabstract["\'][^>]*>.*?</div>)\s*<a[^>]+class=["\']dgotobutton',
                        det_html,
                        re.S | re.I,
                    )
                    if not m_dabs:
                        m_dabs = re.search(r'(<div[^>]+id=["\']dabstract["\'][^>]*>.*?</div>)', det_html, re.S | re.I)

                    dabs_html = m_dabs.group(1) if m_dabs else ""
                    # 将逛丢内部跳板 to.php?u=https%3A%2F%2Fitem.jd.com%2F... 自动解包为真实电商直链
                    def _unwrap_to_php(m_u: re.Match) -> str:
                        return html_lib.unescape(unquote(m_u.group(1)))

                    unwrapped_dabs = re.sub(
                        r"(?:https?://guangdiu\.com/)?to\.php\?[^\s\"'<>]*\bu=(https?%3A%2F%2F[^&\"'\s<>]+)",
                        _unwrap_to_php,
                        dabs_html,
                        flags=re.I,
                    )
                    raw_payload = unwrapped_dabs or dabs_html or det_html[:4000]
                    extracted_links, step_imgs, extracted_tokens = extract_links_images_and_tokens(
                        unwrapped_dabs or det_html, base_url=detail_url
                    )
                    for im in step_imgs:
                        if im not in tutorial_images:
                            tutorial_images.append(im)

                    # 请求轻量级 go.php?id={item_id} (~200字节)，兼容随机 8 位 JS 变量名提取主商品直达链接
                    try:
                        r_go = http.get(go_url, headers={**DEFAULT_HEADERS, "Referer": detail_url}, timeout=timeout)
                        r_go.encoding = "utf-8"
                        if r_go.status_code == 200:
                            m_var = re.search(
                                r"var\s+[A-Za-z0-9_]+\s*=\s*['\"](https?://[^'\"]+|/[^'\"]+)['\"]",
                                r_go.text,
                                re.I,
                            )
                            if m_var:
                                go_target = html_lib.unescape(m_var.group(1)).strip()
                                m_inner_u = re.search(r"[?&]u=(https?%3A%2F%2F[^&\"'\s]+)", go_target, re.I)
                                if m_inner_u:
                                    go_target = unquote(m_inner_u.group(1))
                                elif go_target.startswith("/"):
                                    go_target = urljoin(base, go_target)
                                if go_target and not any(lk.get("url") == go_target for lk in extracted_links):
                                    extracted_links.insert(0, {
                                        "label": "逛丢直达购买链",
                                        "url": go_target,
                                        "type": "guangdiu_go_target",
                                        "platform": detect_platform_signal(f"{go_target} {mall_name}") or "unknown",
                                    })
                        else:
                            detail_errors.append({
                                "item_id": item_id,
                                "error_url": go_url,
                                "http_status": r_go.status_code,
                                "error_type": f"GoRedirectHTTP_{r_go.status_code}",
                            })
                    except Exception as e_go:
                        detail_errors.append({
                            "item_id": item_id,
                            "error_url": go_url,
                            "http_status": None,
                            "error_type": type(e_go).__name__,
                            "error": str(e_go)[:160],
                        })

                    dabs_text = html_to_tutorial_text(unwrapped_dabs, base_url=detail_url) if unwrapped_dabs else preview_text
                    go_urls_str = "\n".join(
                        f"直达链接: {lk['url']}" for lk in extracted_links if lk.get("type") == "guangdiu_go_target"
                    )
                    tutorial_text = f"【{mall_name or '逛丢好价'}】{title}\n{dabs_text}\n{go_urls_str}".strip()
                else:
                    detail_errors.append({
                        "item_id": item_id,
                        "error_url": detail_url,
                        "http_status": rd.status_code,
                        "error_type": f"HTTP_{rd.status_code}",
                    })
            except Exception as e:
                detail_errors.append({
                    "item_id": item_id,
                    "error_url": detail_url,
                    "http_status": None,
                    "error_type": type(e).__name__,
                    "error": str(e)[:160],
                })
                tutorial_text = f"{tutorial_text}\n[详情拉取异常: {type(e).__name__}: {e}]"

        rec = build_deal_record(
            source_site="guangdiu",
            source_channel=f"{channel}:{mall_name}" if mall_name else channel,
            source_item_id=item_id,
            source_url=detail_url,
            title=title,
            price_text=price_span,
            image_url=img_url,
            mall_name=mall_name,
            tutorial_text=tutorial_text,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=raw_payload,
        )
        rec["has_full_detail"] = has_full_detail
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": list_url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": detail_fetched,
        "detail_errors": detail_errors,
        "records": records,
    }


def fetch_xuandan_deals(
    hour_type: str = "",
    page: int = 1,
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T0-6】选单网秒杀快抢群发素材免登 JSON 接口 (`https://www.xuandan.com/NewHome/msKqList`)
    - 100% 同时返回淘宝 12 位纯数字商品 ID (`ShoopGoodsId`)、`uland.taobao.com` 46位加密二合一链 (`GoodsLink`)、
      优惠券链接 (`ActLink`) 以及社群发单员编写的湊单/金币实付文案 (`TjRemark`)！
    """
    http = session or requests.Session()
    url = "https://www.xuandan.com/NewHome/msKqList"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.xuandan.com/NewHome/fastBuy",
        "X-Requested-With": "XMLHttpRequest",
    }
    params = {"page": str(page), "hour_type": str(hour_type or "")}
    t0 = time.perf_counter()
    resp = http.get(url, params=params, headers=headers, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    body = resp.json()
    raw_items = body.get("List") if isinstance(body, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []

    records: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        tk_id = str(item.get("tk_GoodsId") or "").strip()
        tb_num_id = str(item.get("ShoopGoodsId") or "").strip()
        if not tk_id and not tb_num_id:
            continue

        title = str(item.get("GoodsName") or "").strip()
        remark = html_lib.unescape(str(item.get("TjRemark") or "")).strip()
        goods_link = str(item.get("GoodsLink") or "").strip()
        act_link = str(item.get("ActLink") or "").strip()
        img_url = str(item.get("ImgUrl") or "").strip()
        shop_name = str(item.get("shopName") or "淘宝/天猫").strip()
        last_price_str = str(item.get("LastPrice") or "").strip()
        try:
            price_val = float(last_price_str) if last_price_str else None
        except ValueError:
            price_val = None

        links: List[Dict[str, Any]] = []
        if goods_link:
            links.append({
                "label": title or "淘宝二合一领券页",
                "url": goods_link,
                "item_id": tb_num_id,
                "type": "tb_encrypted_id",
                "platform": "taobao",
            })
        if tb_num_id.isdigit():
            tb_direct = f"https://item.taobao.com/item.htm?id={tb_num_id}"
            links.append({
                "label": "淘宝商品数字ID直达页",
                "url": tb_direct,
                "item_id": tb_num_id,
                "type": "tb_numeric_sku",
                "platform": "taobao",
            })
        if act_link:
            links.append({
                "label": "选单网优惠券链接",
                "url": act_link,
                "type": "coupon_url",
                "platform": "taobao",
            })

        extra_links, extra_imgs, extra_tokens = extract_links_images_and_tokens(remark, base_url="https://www.xuandan.com")
        for lk in extra_links:
            if not any(x.get("url") == lk.get("url") for x in links):
                links.append(lk)

        images = [img_url] if img_url else []
        for im in extra_imgs:
            if im not in images:
                images.append(im)

        tutorial_text = (
            f"【{shop_name}】{title}\n"
            f"群发凑单文案: {remark}\n"
            f"到手价: {last_price_str}元 | 淘宝商品ID: {tb_num_id}\n"
            f"二合一链接: {goods_link}"
        ).strip()

        rec = build_deal_record(
            source_site="xuandan",
            source_channel=f"fastbuy_h{hour_type or 'all'}",
            source_item_id=tk_id or tb_num_id,
            source_url=goods_link or f"https://item.taobao.com/item.htm?id={tb_num_id}",
            title=title,
            price_text=f"{last_price_str}元" if last_price_str else "",
            price=price_val,
            image_url=img_url,
            mall_name=shop_name,
            publish_time=str(item.get("BeginTime") or ""),
            expire_time=str(item.get("EndTime") or "") or None,
            hot_metrics={
                "sale_count": item.get("SaleCount"),
                "tk_money": item.get("TkMoney"),
                "act_money": item.get("ActMoney"),
            },
            tutorial_text=tutorial_text,
            tutorial_images=images,
            extracted_links=links,
            extracted_tokens=extra_tokens,
            raw_payload=item,
            explicit_platform="taobao",
        )
        if tb_num_id.isdigit():
            rec["candidate_goods_id"] = tb_num_id
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "request_url": getattr(resp, "url", None) or url,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "detail_fetched": len(records),
        "detail_errors": [],
        "records": records,
    }


# ==============================================================================
# 4. T2 级扩展站点拉取器（慢慢买列表 / 55海淘 EdgeOne 解算）
# ==============================================================================
def fetch_manmanbuy_list(
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T2-1】慢慢买省钱控 (`https://cu.manmanbuy.com/`)
    """
    http = session or requests.Session()
    url = "https://cu.manmanbuy.com/"
    t0 = time.perf_counter()
    resp = http.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    matches = re.findall(
        r'<a[^>]*href\s*=\s*["\'](https?://cu\.manmanbuy\.com/discuxiao_(\d+)\.aspx)["\'][^>]*title=["\']([^"\']+)["\'][^>]*>\s*<img[^>]*src\s*=\s*["\']([^"\']+)["\']',
        resp.text,
        re.S | re.I,
    )
    records: List[Dict[str, Any]] = []
    seen: set = set()
    for href, item_id, raw_title, img_url in matches:
        if item_id in seen:
            continue
        seen.add(item_id)
        title = html_lib.unescape(raw_title).strip()
        clean_img = html_lib.unescape(img_url).strip()
        plat = detect_platform_signal(title) or ("jd" if extract_jd_image_key(clean_img) else "unknown")
        rec = build_deal_record(
            source_site="manmanbuy",
            source_channel="cu_index",
            source_item_id=item_id,
            source_url=href,
            title=title,
            image_url=clean_img,
            tutorial_text=f"{title}\n慢慢买详情页: {href}",
            tutorial_images=[clean_img] if clean_img else [],
            extracted_links=[{
                "label": "慢慢买优惠页",
                "url": href,
                "type": "manmanbuy_detail",
                "platform": plat,
            }],
            raw_payload={"href": href, "title": title, "img": clean_img},
            explicit_platform=plat,
        )
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if resp.status_code == 200 and len(records) > 0 else "error",
        "http_status": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "count": len(records),
        "records": records,
    }


def fetch_55haitao_deals(
    session: Optional[requests.Session] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    【T2-2】55海淘优惠 (`https://www.55haitao.com/deals`)
    - 内置腾讯云 EdgeOne 静态算术 JS Cookie 挑战（`__tst_status` + `EO_Bot_Ssid`）自动解算。
    """
    http = session or requests.Session()
    url = "https://www.55haitao.com/deals"
    t0 = time.perf_counter()
    r1 = http.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    challenge_solved = False

    if "__tst_status=" in r1.text and "EO_Bot_Ssid=" in r1.text:
        nums = [int(x) for x in re.findall(r":(\d{7,11})", r1.text)]
        ssid_m = re.search(r"\(t,(\d{7,11})\)", r1.text)
        if len(nums) >= 3 and ssid_m:
            total = sum(nums[:3])
            http.cookies.set("__tst_status", f"{total}#", domain="www.55haitao.com")
            http.cookies.set("EO_Bot_Ssid", ssid_m.group(1), domain="www.55haitao.com")
            r1 = http.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            challenge_solved = True

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    deal_links = list(dict.fromkeys(re.findall(r"https?://www\.55haitao\.com/deals/(\d+)\.html", r1.text)))

    records: List[Dict[str, Any]] = []
    for did in deal_links[:20]:
        d_url = f"https://www.55haitao.com/deals/{did}.html"
        m_a = re.search(rf'<a[^>]*href=["\']{re.escape(d_url)}["\'][^>]*>(.*?)</a>', r1.text, re.S | re.I)
        title = re.sub(r"<[^>]+>", "", html_lib.unescape(m_a.group(1))).strip() if m_a else f"55海淘优惠 #{did}"
        rec = build_deal_record(
            source_site="55haitao",
            source_channel="deals",
            source_item_id=did,
            source_url=d_url,
            title=title,
            tutorial_text=f"{title}\n55海淘详情页: {d_url}",
            extracted_links=[{
                "label": "55海淘优惠详情",
                "url": d_url,
                "type": "55haitao_detail",
                "platform": detect_platform_signal(title) or "unknown",
            }],
            raw_payload={"deal_id": did, "challenge_solved": challenge_solved},
        )
        rec["has_full_detail"] = True
        records.append(rec)

    return {
        "status": "success" if r1.status_code == 200 and len(records) > 0 else "error",
        "http_status": r1.status_code,
        "elapsed_ms": elapsed_ms,
        "challenge_solved": challenge_solved,
        "count": len(records),
        "records": records,
    }


# ==============================================================================
# 5. 反爬虫边界探测与冷却自动恢复实测框架
# ==============================================================================
def detect_anti_crawl_signal(resp: requests.Response, body_text: str) -> Optional[str]:
    """
    检测响应中是否触发了反爬/风控拦截：返回拦截类型描述字符串；若正常则返回 None。
    """
    if resp.status_code == 202 or ("probe.js" in body_text and len(body_text) < 2000):
        return f"HTTP_{resp.status_code}_JS_PROBE (probe.js 动态指纹校验)"
    if "TCaptcha.js" in body_text or "TencentCaptcha" in body_text:
        return "TENCENT_CAPTCHA_BLOCK (TCaptcha.js 腾讯防水墙验证码)"
    if resp.status_code in (401, 403, 412, 429, 503, 521):
        return f"HTTP_{resp.status_code}_WAF_BLOCK"
    if "__tst_status=" in body_text and "EO_Bot_Ssid=" in body_text and len(body_text) < 3000:
        return "EDGEONE_JS_COOKIE_CHALLENGE (__tst_status 算术挑战)"
    if "acw_sc__v2" in body_text and len(body_text) < 5000:
        return "ALIYUN_WAF_SLIDER_OR_ACW_SC_V2 (阿里云滑块/动态混淆 Cookie)"
    if any(k in body_text[:2000] for k in ("访问过于频繁", "请求过于频繁", "安全验证", "人机验证", "请输入验证码")):
        return "CONTENT_RATE_LIMIT_OR_CAPTCHA (页面提示频繁/验证码)"
    return None


def run_anti_crawl_boundary_test(
    site_name: str,
    url_builder: Callable[[int], Tuple[str, Dict[str, Any]]],
    item_validator: Callable[[requests.Response, str], int],
    *,
    max_requests: int = 25,
    interval_sec: float = 0.15,
    use_session: bool = True,
    cooldown_schedule_sec: Tuple[int, ...] = (5, 15, 30),
) -> Dict[str, Any]:
    """
    对单个目标站点执行高频连续拉取压测 + 冷却恢复测试。
    """
    print(f"\n[反爬压测] 启动站点 <{site_name}> | 计划请求={max_requests}次 | 间隔={interval_sec}s | Session复用={use_session}")
    http = requests.Session() if use_session else requests
    history: List[Dict[str, Any]] = []
    triggered_at: Optional[int] = None
    trigger_reason: Optional[str] = None

    for req_idx in range(1, max_requests + 1):
        url, req_kwargs = url_builder(req_idx)
        headers = {**DEFAULT_HEADERS, **(req_kwargs.get("headers") or {})}
        params = req_kwargs.get("params")
        t0 = time.perf_counter()
        try:
            resp = http.get(url, headers=headers, params=params, timeout=10)
            if "0818tuan.com" in url:
                resp.encoding = "utf-8"
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            text = resp.text
            anti_sig = detect_anti_crawl_signal(resp, text)
            valid_items = 0 if anti_sig else item_validator(resp, text)
            if not anti_sig and valid_items <= 0:
                anti_sig = f"ZERO_VALID_ITEMS (status={resp.status_code}, len={len(text)})"

            step_stat = {
                "seq": req_idx,
                "http_status": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "body_len": len(text),
                "valid_items": valid_items,
                "anti_crawl_signal": anti_sig,
            }
            history.append(step_stat)

            if anti_sig:
                triggered_at = req_idx
                trigger_reason = anti_sig
                print(f"  ⚠️ 第 {req_idx} 次请求触发风控/异常: {anti_sig} | 耗时={elapsed_ms}ms")
                break
            elif req_idx == 1 or req_idx % 5 == 0 or req_idx == max_requests:
                print(f"  ✅ 第 {req_idx}/{max_requests} 次成功 | status={resp.status_code} | 有效条目={valid_items} | 耗时={elapsed_ms}ms")
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            triggered_at = req_idx
            trigger_reason = f"NETWORK_EXCEPTION ({type(e).__name__}: {e})"
            history.append({
                "seq": req_idx,
                "http_status": 0,
                "elapsed_ms": elapsed_ms,
                "body_len": 0,
                "valid_items": 0,
                "anti_crawl_signal": trigger_reason,
            })
            print(f"  ❌ 第 {req_idx} 次请求发生网络异常: {trigger_reason}")
            break

        if req_idx < max_requests and interval_sec > 0:
            time.sleep(interval_sec)

    cooldown_results: List[Dict[str, Any]] = []
    recovered = False
    if triggered_at is not None:
        print(f"  ⏳ 开始对 <{site_name}> 执行不破解验证的【冷却恢复测试】...")
        for wait_s in cooldown_schedule_sec:
            print(f"     -> 暂停冷却 {wait_s} 秒...")
            time.sleep(wait_s)
            url, req_kwargs = url_builder(1)
            headers = {**DEFAULT_HEADERS, **(req_kwargs.get("headers") or {})}
            params = req_kwargs.get("params")
            t0 = time.perf_counter()
            try:
                probe_http = requests.Session()
                resp = probe_http.get(url, headers=headers, params=params, timeout=10)
                if "0818tuan.com" in url:
                    resp.encoding = "utf-8"
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                anti_sig = detect_anti_crawl_signal(resp, resp.text)
                valid_items = 0 if anti_sig else item_validator(resp, resp.text)
                is_ok = (anti_sig is None and valid_items > 0)
                cooldown_results.append({
                    "cooldown_sec": wait_s,
                    "http_status": resp.status_code,
                    "valid_items": valid_items,
                    "anti_crawl_signal": anti_sig,
                    "recovered": is_ok,
                })
                if is_ok:
                    recovered = True
                    print(f"     ✅ 冷却 {wait_s} 秒后自动恢复正常！有效条目={valid_items} | 耗时={elapsed_ms}ms")
                    break
                else:
                    print(f"     ❌ 冷却 {wait_s} 秒后仍受限: {anti_sig or '有效条目为0'}")
            except Exception as e:
                cooldown_results.append({
                    "cooldown_sec": wait_s,
                    "http_status": 0,
                    "valid_items": 0,
                    "anti_crawl_signal": str(e),
                    "recovered": False,
                })

    success_cnt = sum(1 for h in history if not h.get("anti_crawl_signal"))
    avg_ms = round(sum(h["elapsed_ms"] for h in history) / max(1, len(history)), 1)
    return {
        "site_name": site_name,
        "max_requests_planned": max_requests,
        "interval_sec": interval_sec,
        "requests_executed": len(history),
        "success_count": success_cnt,
        "avg_elapsed_ms": avg_ms,
        "triggered_at_request": triggered_at,
        "trigger_reason": trigger_reason,
        "cooldown_tested": triggered_at is not None,
        "cooldown_recovered": recovered if triggered_at is not None else True,
        "cooldown_results": cooldown_results,
    }


def run_all_sample_verifications(output_path: Optional[str] = None) -> Dict[str, Any]:
    """
    执行全站点实盘样本拉取验证，检验商品基础字段与“购买/凑单教程、截图、链接、时间”完整留存效果。
    """
    print("=" * 80)
    print("🚀 阶段一：执行候选导购/线报网站好价商品与凑单教程完整拉取实测")
    print("=" * 80)

    tasks: List[Tuple[str, Callable[[], Dict[str, Any]]]] = [
        ("T0_好单库_实时线报(京东)", lambda: fetch_haodanku_wire_report(category_type=10004, page_no=1, page_size=10)),
        ("T0_好单库_实时线报(淘宝)", lambda: fetch_haodanku_wire_report(category_type=10003, page_no=1, page_size=10)),
        ("T0_好单库_福利好价清单", lambda: fetch_haodanku_weal_list(page_no=1, page_size=10)),
        ("T0_线报酷_1小时最热榜(含详情教程)", lambda: fetch_xianbao_fun_deals(channel="hot_1h", fetch_detail=True, max_details=4)),
        ("T0_线报酷_实时聚合流(含详情教程)", lambda: fetch_xianbao_fun_deals(channel="latest", page=1, fetch_detail=True, max_details=4)),
        ("T0_0818团_最新线报(含详情凑单作业)", lambda: fetch_0818tuan_deals(page=1, fetch_detail=True, max_details=4)),
        ("T1_什么值得买_好价(含三路高可用切换与详情)", lambda: fetch_smzdm_faxian_deals(sort="hot_3h", page=1, fetch_detail=True, max_details=3)),
        ("T1_拔草哦_优惠线报(含详情口令)", lambda: fetch_bacaoo_deals(fetch_detail=True, max_details=3)),
        ("T1_网猴线报_24小时热榜(含详情)", lambda: fetch_iehou_deals(mode="dayhot", fetch_detail=True, max_details=3)),
        ("T1_购物党_最新折扣", lambda: fetch_gwdang_discounts(mode="0-latest")),
        ("T2_慢慢买_省钱控列表摘要", lambda: fetch_manmanbuy_list()),
        ("T2_55海淘_EdgeOne算术Cookie绕过", lambda: fetch_55haitao_deals()),
    ]

    summary_report: Dict[str, Any] = {}
    for label, fn in tasks:
        try:
            res = fn()
            recs = res.get("records") or []
            with_links = sum(1 for r in recs if r.get("extracted_links"))
            with_imgs = sum(1 for r in recs if r.get("tutorial_images"))
            with_tut = sum(1 for r in recs if len(str(r.get("tutorial_text") or "")) > 10)
            sample_rec = recs[0] if recs else {}
            print(
                f"[{label}] 状态={res.get('status')} | 条数={len(recs)} | 耗时={res.get('elapsed_ms')}ms "
                f"| 含教程文本={with_tut} | 含图片={with_imgs} | 含提取链接={with_links}"
            )
            if sample_rec:
                tut_preview = re.sub(r"\s+", " ", str(sample_rec.get("tutorial_text") or ""))[:140]
                print(
                    f"   📌 样本首条: [{sample_rec.get('platform')}] {sample_rec.get('title')[:48]} "
                    f"| 价格={sample_rec.get('price_text') or sample_rec.get('price')} | 发布={sample_rec.get('publish_time')}"
                )
                print(f"   📝 教程/上下文预览: {tut_preview}...")
            summary_report[label] = {
                "status": res.get("status"),
                "http_status": res.get("http_status"),
                "elapsed_ms": res.get("elapsed_ms"),
                "count": len(recs),
                "with_tutorial_text": with_tut,
                "with_images": with_imgs,
                "with_extracted_links": with_links,
                "sample_records": recs[:2],
            }
        except Exception as e:
            print(f"[{label}] ❌ 执行异常: {type(e).__name__}: {e}")
            summary_report[label] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary_report, f, ensure_ascii=False, indent=2)
        print(f"\n💾 样本数据已完整保存至: {output_path}")

    return summary_report


def run_all_stress_and_cooldown_tests(max_requests: int = 20, interval_sec: float = 0.1) -> List[Dict[str, Any]]:
    """
    执行高频拉取反爬虫边界与冷却恢复测试。
    """
    print("\n" + "=" * 80)
    print(f"🛡️ 阶段二：高频反爬虫边界探测与冷却自动恢复实测 (单站点连续 {max_requests} 次, 间隔 {interval_sec}s)")
    print("=" * 80)

    results: List[Dict[str, Any]] = []

    # 1. 好单库 wire_report JSON 接口高频翻页压测
    results.append(
        run_anti_crawl_boundary_test(
            "好单库_wire_report_JSON",
            lambda i: (
                "https://www.haodanku.com/HolidayActivity/wire_report",
                {
                    "params": {"page_no": ((i - 1) % 10) + 1, "page_size": 20, "type": 0, "is_images": 1},
                    "headers": {"Referer": "https://www.haodanku.com/activity/tip_off", "X-Requested-With": "XMLHttpRequest"},
                },
            ),
            lambda r, t: len(((r.json().get("data") or {}).get("items")) or []),
            max_requests=max_requests,
            interval_sec=interval_sec,
        )
    )

    # 2. 线报酷 列表+分页高频压测
    results.append(
        run_anti_crawl_boundary_test(
            "线报酷_new.xianbao.fun_SSR",
            lambda i: (
                f"https://new.xianbao.fun/page/{((i - 1) % 5) + 1}/" if i > 1 else "https://new.xianbao.fun/",
                {},
            ),
            lambda r, t: len(re.findall(r'class=["\'][^"\']*article-list', t)),
            max_requests=max_requests,
            interval_sec=interval_sec,
        )
    )

    # 3. 0818团 列表分页高频压测
    results.append(
        run_anti_crawl_boundary_test(
            "0818团_0818tuan.com_静态页",
            lambda i: (f"http://www.0818tuan.com/list-1-{(i - 1) % 5}.html", {}),
            lambda r, t: len(re.findall(r"/xbhd/\d+\.html", t)),
            max_requests=max_requests,
            interval_sec=interval_sec,
        )
    )

    # 4. 什么值得买 主站精选 SSR (www.smzdm.com/jingxuan/) 高频压测
    results.append(
        run_anti_crawl_boundary_test(
            "什么值得买_主站精选SSR(www.smzdm.com/jingxuan/)",
            lambda i: ("https://www.smzdm.com/jingxuan/", {}),
            lambda r, t: len(re.findall(r"www\.smzdm\.com/p/\d+", t)),
            max_requests=max_requests,
            interval_sec=interval_sec,
        )
    )

    # 5. 拔草哦 & 网猴线报 高频压测
    results.append(
        run_anti_crawl_boundary_test(
            "拔草哦_youhui.bacaoo.com",
            lambda i: ("https://youhui.bacaoo.com/", {}),
            lambda r, t: len(re.findall(r'href=["\']/p/\d+["\']', t)),
            max_requests=max_requests,
            interval_sec=interval_sec,
        )
    )
    results.append(
        run_anti_crawl_boundary_test(
            "网猴线报_iehou.com",
            lambda i: ("https://iehou.com/page-dayhot.htm", {}),
            lambda r, t: len(re.findall(r"xianbao-\d+\.htm", t)),
            max_requests=max_requests,
            interval_sec=interval_sec,
        )
    )

    print("\n" + "=" * 80)
    print("📊 反爬虫阈值与冷却恢复实测汇总表")
    print("=" * 80)
    for item in results:
        trig = f"第 {item['triggered_at_request']} 次 ({item['trigger_reason']})" if item["triggered_at_request"] else "未触发风控 (100% 通过)"
        cool = "无需冷却" if not item["cooldown_tested"] else ("✅ 自动恢复" if item["cooldown_recovered"] else "❌ 需解算指纹/Cookie")
        print(
            f"- {item['site_name']}: 成功 {item['success_count']}/{item['requests_executed']} "
            f"| 平均耗时={item['avg_elapsed_ms']}ms | 触发阈值={trig} | 冷却恢复={cool}"
        )

    return results


def main():
    parser = argparse.ArgumentParser(description="导购/线报网站好价商品拉取与反爬边界实测脚本")
    parser.add_argument(
        "--mode",
        choices=("all", "sample", "stress"),
        default="all",
        help="运行模式: sample=仅拉取各站样本数据, stress=仅执行高频反爬与冷却恢复压测, all=完整执行",
    )
    parser.add_argument("--max-requests", type=int, default=20, help="单站点高频压测请求次数 (默认 20)")
    parser.add_argument("--interval", type=float, default=0.1, help="高频压测请求间隔秒数 (默认 0.1s)")
    parser.add_argument("--output", type=str, default="", help="可选：样本与压测报告输出 JSON 路径")
    args = parser.parse_args()

    out_data: Dict[str, Any] = {}
    if args.mode in ("all", "sample"):
        out_data["sample_verification"] = run_all_sample_verifications()
    if args.mode in ("all", "stress"):
        out_data["stress_and_cooldown"] = run_all_stress_and_cooldown_tests(
            max_requests=args.max_requests,
            interval_sec=args.interval,
        )

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 完整测试报告已写入: {args.output}")


if __name__ == "__main__":
    main()
