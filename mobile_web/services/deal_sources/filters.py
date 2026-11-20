# -*- coding: utf-8 -*-
"""
好价线报统一门禁层（单一真源）：
1. 【100% 确定非淘/京/拼平台过滤】：仅当百分百确认属于拼多多/京东/淘宝之外的平台时才过滤，绝不误杀；
2. 【宁缺毋滥 · 拒绝空壳单品】：没有完整详情、没有实质性教程说明或没有可操作外部电商锚点的空壳，绝不入库。
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from models.deal_constants import _UNSUPPORTED_EXTERNAL_DOMAINS
from models.product import detect_platform_signal

SUPPORTED_PLATFORMS: Tuple[str, ...] = ("jd", "taobao", "pdd")

_SUPPORTED_MALL_KEYWORDS: Tuple[str, ...] = (
    "京东",
    "京喜",
    "淘宝",
    "天猫",
    "聚划算",
    "淘特",
    "拼多多",
)

# 仅包含 100% 确定不属于淘宝/京东/拼多多的独立平台或线下餐饮/网盘/软件渠道
CONFIRMED_UNSUPPORTED_MALLS: frozenset = frozenset({
    "唯品会",
    "美团",
    "美团外卖",
    "饿了么",
    "抖音",
    "抖音电商",
    "快手",
    "小红书",
    "亚马逊",
    "亚马逊中国",
    "亚马逊海外购",
    "海外购",
    "考拉海购",
    "网易严选",
    "严选",
    "苏宁",
    "苏宁易购",
    "小米有品",
    "当当",
    "当当网",
    "1688",
    "阿里巴巴",
    "得物",
    "毒",
    "闲鱼",
    "转转",
    "叮咚买菜",
    "朴朴",
    "盒马",
    "山姆",
    "沃尔玛",
    "永辉",
    "永辉生活",
    "多点",
    "顺丰优选",
    "屈臣氏官网",
    "肯德基",
    "麦当劳",
    "星巴克",
    "瑞幸",
    "霸王茶姬",
    "奈雪",
    "蜜雪冰城",
    "喜茶",
    "汉堡王",
    "必胜客",
    "滴滴",
    "携程",
    "飞猪",
    "去哪儿",
    "同程",
    "百度网盘",
    "阿里云盘",
    "夸克网盘",
    "苹果官网",
    "apple官网",
    "华为商城",
    "小米商城",
})

# 所有采集源站自身域名（不可视为商品电商跳转锚点）
SOURCE_SELF_HOSTS: frozenset = frozenset({
    "guangdiu.com",
    "iguangdiu.com",
    "xhhcds.cn",
    "haodanku.com",
    "xuandan.com",
    "0818tuan.com",
    "zzzdm.com",
    "m.zzzdm.com",
    "t.me",
    "ithome.com",
    "digi.ithome.com",
    "lapin.ithome.com",
    "go.ruanmei.com",
    "kiees.com",
    "niuza.com",
    "zdzdm.com",
    "baicaio.com",
})

CONFIRMED_UNSUPPORTED_HOSTS: Tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            d
            for d in _UNSUPPORTED_EXTERNAL_DOMAINS
            if "/" not in d and d not in SOURCE_SELF_HOSTS
        ]
        + [
            "amazon.cn",
            "amazon.com",
            "suning.com",
            "kaola.com",
            "you.163.com",
            "xiaomiyoupin.com",
            "dangdang.com",
            "1688.com",
            "dewu.com",
            "poizon.com",
            "ele.me",
            "ctrip.com",
            "fliggy.com",
            "qunar.com",
            "apple.com",
            "apps.apple.com",
            "play.google.com",
            "vmall.com",
            "mi.com",
            "alipan.com",
            "aliyundrive.com",
            "pan.quark.cn",
            "pan.xunlei.com",
            "123pan.com",
            "lanzoui.com",
            "lanzoux.com",
            "lanzoup.com",
            "github.com",
            "store.steampowered.com",
            "epicgames.com",
            "iesdouyin.com",
            "mcdonalds.com.cn",
            "kfc.com.cn",
            "starbucks.com.cn",
        ]
    )
)

_IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
)


def normalize_explicit_platform_code(raw_code: Any) -> str:
    s = str(raw_code or "").strip().lower()
    if not s or s in ("unknown", "other", "all", "none"):
        return ""
    if s in ("tb", "tmall", "taobao"):
        return "taobao"
    if s in ("jd", "jingdong"):
        return "jd"
    if s in ("pdd", "pinduoduo", "yangkeduo"):
        return "pdd"
    return s


def unwrap_known_redirect_url(url_str: str) -> str:
    """提取已知内部跳转链（如 guangdiu/iguangdiu to.php?u=...）中的真实目标 URL。"""
    s = html_lib.unescape(str(url_str or "")).strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        if "guangdiu.com" in host and parts.path.endswith("to.php"):
            qs = parse_qs(parts.query)
            if qs.get("u"):
                inner = unquote(str(qs["u"][0] or "")).strip()
                if inner.startswith(("http://", "https://")):
                    return inner
    except Exception:
        pass
    return s


def is_host_match(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_confirmed_unsupported_deal(
    *,
    platform_code: str = "",
    mall_name: str = "",
    links: Optional[List[Dict[str, Any]]] = None,
    tokens: Optional[List[str]] = None,
) -> bool:
    """
    【100% 确定非淘/京/拼平台过滤器 · 防误杀铁律】：
    仅当平台信号【百分百确定】属于拼多多/京东/淘宝之外的平台时返回 True；
    凡存在任何淘京拼正面信号，或平台归属尚不明确（如未知品牌名、未知第三方短链），一律返回 False 放行。
    """
    norm_code = normalize_explicit_platform_code(platform_code)
    if norm_code and norm_code not in SUPPORTED_PLATFORMS:
        return True

    clean_mall = re.sub(r"\s+", "", str(mall_name or "")).strip()
    if clean_mall:
        if any(kw in clean_mall for kw in _SUPPORTED_MALL_KEYWORDS):
            return False
        mall_low = clean_mall.lower()
        if mall_low in CONFIRMED_UNSUPPORTED_MALLS or clean_mall in CONFIRMED_UNSUPPORTED_MALLS:
            return True
        first_seg = re.split(r"[·•\-|/]+", clean_mall)[0].strip()
        if first_seg and (
            first_seg in CONFIRMED_UNSUPPORTED_MALLS
            or first_seg.lower() in CONFIRMED_UNSUPPORTED_MALLS
        ):
            return True

    if tokens and any(str(t or "").strip() for t in tokens):
        for tk in tokens:
            if detect_platform_signal(tk) in SUPPORTED_PLATFORMS:
                return False

    dest_hosts: List[str] = []
    for lk in links or []:
        if not isinstance(lk, dict):
            continue
        raw_u = str(lk.get("url") or "").strip()
        if not raw_u.startswith(("http://", "https://")):
            continue
        u = unwrap_known_redirect_url(raw_u)
        try:
            parts = urlsplit(u)
            host = (parts.hostname or "").strip().lower()
            path_low = (parts.path or "").lower()
        except Exception:
            continue
        if not host or path_low.endswith(_IMAGE_EXTENSIONS):
            continue
        if any(is_host_match(host, self_h) for self_h in SOURCE_SELF_HOSTS):
            continue
        if detect_platform_signal(u) in SUPPORTED_PLATFORMS:
            return False
        dest_hosts.append(host)

    if norm_code in SUPPORTED_PLATFORMS:
        return False

    if dest_hosts and all(
        any(is_host_match(host, bad) for bad in CONFIRMED_UNSUPPORTED_HOSTS)
        for host in dest_hosts
    ):
        return True

    return False


def is_record_confirmed_unsupported(record: Dict[str, Any]) -> bool:
    """对标准化线报记录执行统一的非淘京拼平台确定性过滤检查。"""
    if not isinstance(record, dict):
        return True
    return is_confirmed_unsupported_deal(
        platform_code=str(record.get("platform") or ""),
        mall_name=str(record.get("mall_name") or ""),
        links=record.get("extracted_links") or [],
        tokens=record.get("extracted_tokens") or [],
    )


def _has_external_actionable_link(links: List[Dict[str, Any]]) -> bool:
    for lk in links:
        if not isinstance(lk, dict):
            continue
        if str(lk.get("item_id") or "").strip():
            return True
        raw_u = str(lk.get("url") or "").strip()
        if not raw_u:
            continue
        if not raw_u.startswith(("http://", "https://")):
            # 非 HTTP 剪贴板口令等可直接作为锚点
            return True
        try:
            parts = urlsplit(raw_u)
            host = (parts.hostname or "").strip().lower()
            path_low = (parts.path or "").lower()
        except Exception:
            continue
        if not host or path_low.endswith(_IMAGE_EXTENSIONS):
            continue
        if any(is_host_match(host, self_h) for self_h in SOURCE_SELF_HOSTS):
            continue
        return True
    return False


def has_valid_detail_and_tutorial(record: Dict[str, Any], min_text_chars: int = 4) -> bool:
    """
    【宁缺毋滥 · 拒绝空壳单品门禁】：
    验证线报记录既已拿到完整详情 (`has_full_detail=True`)，又包含实质性的爆料教程/方案文本与可操作外部购买锚点。
    """
    if not isinstance(record, dict) or not record.get("has_full_detail"):
        return False

    links = [lk for lk in (record.get("extracted_links") or []) if isinstance(lk, dict) and (lk.get("url") or lk.get("item_id"))]
    tokens = [str(tk).strip() for tk in (record.get("extracted_tokens") or []) if str(tk or "").strip()]
    cand_id = str(record.get("candidate_goods_id") or "").strip()
    if not (_has_external_actionable_link(links) or tokens or cand_id):
        return False

    tut = str(record.get("tutorial_text") or "").strip()
    if not tut or "[详情拉取异常" in tut:
        return False

    stripped = re.sub(r"\[(?:群聊晒单/教程图片|教程图片):[^\]]*\]", " ", tut)
    stripped = re.sub(r"https?://\S+", " ", stripped)
    stripped = re.sub(r"\s+", "", stripped)
    if len(stripped) >= min_text_chars:
        return True

    hot = record.get("hot_metrics") if isinstance(record.get("hot_metrics"), dict) else {}
    return bool(hot.get("video_tutorial_url"))
