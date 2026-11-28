# -*- coding: utf-8 -*-
"""闲鱼公开市场比价检索能力：搜索小号登录搜索概览 + 移动端 H5 免登录批量/单条获取商品完整详情。"""

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

XIANYU_ITEM_SCHEMA_VERSION = 2

from .session import (
    DEFAULT_GUEST_USER_DATA_DIR,
    DEFAULT_LOCK_TIMEOUT_SEC,
    DEFAULT_SEARCH_USER_DATA_DIR,
    GOOFISH_H5_HOME_URL,
    GOOFISH_HOME_URL,
    XianyuLockTimeoutError,
    XianyuOfflineError,
    XianyuRoundBrowserPool,
    _browser_session_guard,
    _build_lock_timeout_result,
    _build_offline_result,
    _detach_listener_safely,
    _dir_tag,
    _ephemeral_h5_session_guard,
    _extract_risk_reasons,
    _format_failure_diag,
    _verify_account_online,
    logger,
)

# ==============================================================================
# 七、 公开市场比价检索（独立小号登录搜索概览 + 移动端 H5 免登录批量获取商品详情）
# ==============================================================================
def _verify_guest_no_risk_block(
    page,
    auth_tracker: Dict[str, Any],
    user_data_dir: str,
    task_name: str = "",
) -> None:
    """H5 免登录详情专用风控与匿名配额限流检测：遇滑块或详情接口强制要求登录时立即熔断停手。"""
    risk_reasons = _extract_risk_reasons(page, auth_tracker)
    if auth_tracker.get("guest_login_blocked_rets"):
        risk_reasons.append(f"免登录检索被强制要求登录/IP匿名配额限流({auth_tracker['guest_login_blocked_rets'][0]})")

    if risk_reasons:
        reason_str = "；".join(risk_reasons)
        logger.error(
            f"\n{'!' * 76}\n"
            f"🚨 [风控/免登录限流告警] 检测到闲鱼拦截当前免登录检索，已立即中止操作以静默冷却！\n"
            f"   ├── 触发任务   : {task_name or '免登录详情任务'}\n"
            f"   ├── 浏览器目录 : <{user_data_dir}>\n"
            f"   ├── 当前页面   : {page.url}\n"
            f"   └── 拦截证据   : {reason_str}\n"
            f"{'!' * 76}"
        )
        raise XianyuOfflineError(
            user_data_dir=user_data_dir,
            reason=reason_str,
            error_code="RISK_CONTROL_BLOCKED",
        )


def _extract_item_id(raw_target: Any) -> str:
    """从商品链接或字符串中提取纯闲鱼商品 item_id。"""
    s = str(raw_target or "").strip()
    if not s:
        return ""
    id_match = re.search(r"[?&]id=(\d+)", s)
    if id_match:
        return id_match.group(1)
    digits_match = re.search(r"(\d{8,})", s)
    return digits_match.group(1) if digits_match else s


def _parse_float_price(val: Any, default: float = 0.0) -> float:
    """从字符串（如 '¥55'、'38.99'）或数字中安全提取浮点数价格。"""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    s = str(val).strip()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return round(float(m.group(1)), 2) if m else default


def _parse_int_num(val: Any, default: int = 0) -> int:
    """从字符串（如 '19人想要'、'187'）或数字中安全提取整数。"""
    if val is None:
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else default


_CN_TZ = timezone(timedelta(hours=8))


def _parse_optional_int(val: Any) -> Optional[int]:
    """仅当存在有效整数时返回 int；缺失、布尔或空值一律返回 None，避免用 0 污染统计。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    m = re.fullmatch(r"(-?\d+)(?:\.\d+)?", s)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(-?\d+)", s)
    return int(m2.group(1)) if m2 else None


def _parse_optional_float(val: Any) -> Optional[float]:
    """仅当存在有效有限数字时返回 float；缺失或空值返回 None。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    s = str(val).strip()
    if not s:
        return None
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _parse_optional_price(val: Any) -> Optional[float]:
    """解析可选正数价格字段（兼容 '¥55.5' 或数字），缺失、0 或负数一律返回 None，避免假 0 污染原价统计。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return round(f, 2) if (math.isfinite(f) and f > 0) else None
    s = str(val).strip()
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    f = round(float(m.group(1)), 2)
    return f if (math.isfinite(f) and f > 0) else None


_FAKE_ZERO_SHOP_LABELS = frozenset({"0条评价", "好评率0%"})


def _text_has_free_shipping(text: str) -> bool:
    """正向识别包邮文本，防止 '不包邮' / '偏远地区不包邮' 被子串误判为包邮。"""
    s = str(text or "").strip()
    return bool(s and "包邮" in s and "不包邮" not in s)


def _parse_optional_bool(val: Any) -> Optional[bool]:
    """解析可选布尔字段，仅当明确为 bool 或布尔字符串时返回 bool，否则返回 None。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


def _parse_bool_flag(val: Any) -> bool:
    """安全解析布尔开关（兼容 bool / 'true' / '1' / 1）。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val or "").strip().lower()
    return s in ("true", "1", "yes")


def _parse_utc_datetime(val: Any) -> Optional[datetime]:
    """将 datetime、13 位毫秒/10 位秒级时间戳或北京时间字符串统一转为 UTC-aware datetime。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc) if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        ts = float(val)
        if not math.isfinite(ts) or ts <= 0:
            return None
        if ts >= 1e12:
            ts /= 1000.0
        if ts < 1_000_000_000:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    s = str(val).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return _parse_utc_datetime(float(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_CN_TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_CN_TZ).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_reply_ratio(seller_do: Dict[str, Any]) -> Optional[float]:
    """优先提取浮点 24h 回复率，否则回退解析百分比字符串（如 '85%' -> 0.85）。"""
    ratio = _parse_optional_float(seller_do.get("replyIn24hRatioDouble"))
    if ratio is not None:
        return round(ratio, 4)
    raw_pct = str(seller_do.get("replyRatio24h") or "").strip()
    if raw_pct:
        m = re.search(r"(\d+(?:\.\d+)?)", raw_pct)
        if m:
            return round(float(m.group(1)) / 100.0, 4)
    return None


def normalize_search_card(
    card: Dict[str, Any],
    *,
    search_keyword: str = "",
) -> Optional[Dict[str, Any]]:
    """SSOT：将任意闲鱼搜索卡片字典规范化为 Schema v2 标准结构。"""
    if not isinstance(card, dict):
        return None
    item_id = str(card.get("item_id") or "").strip()
    if not item_id:
        return None

    item_url = str(card.get("item_url") or f"{GOOFISH_HOME_URL}/item?id={item_id}").strip()
    pic_url = str(card.get("image_url") or "").strip()
    if pic_url.startswith("//"):
        pic_url = "https:" + pic_url

    return {
        "item_id": item_id,
        "item_url": item_url,
        "title": str(card.get("title") or "").strip(),
        "price": _parse_float_price(card.get("price"), default=0.0),
        "raw_price_text": str(card.get("raw_price_text") or "").strip(),
        "original_price": _parse_optional_price(card.get("original_price")),
        "price_drop_amount": _parse_optional_price(card.get("price_drop_amount")),
        "want_cnt": _parse_optional_int(card.get("want_cnt")),
        "is_free_shipping": bool(card.get("is_free_shipping")),
        "has_nfr_badge": bool(card.get("has_nfr_badge")),
        "area": str(card.get("area") or "").strip(),
        "seller_id_enc": str(card.get("seller_id_enc") or "").strip(),
        "seller_nick": str(card.get("seller_nick") or "").strip(),
        "seller_tags": [str(x).strip() for x in (card.get("seller_tags") or []) if str(x).strip()],
        "image_url": pic_url,
        "publish_at": _parse_utc_datetime(card.get("publish_at")),
        "pic_width": _parse_optional_int(card.get("pic_width")),
        "pic_height": _parse_optional_int(card.get("pic_height")),
        "is_video": bool(card.get("is_video")),
        "is_ad": bool(card.get("is_ad")),
        "is_fish_shop": bool(card.get("is_fish_shop")),
        "coin_discount": bool(card.get("coin_discount")),
        "cat_id": str(card.get("cat_id") or "").strip(),
        "channel_cat_id": str(card.get("channel_cat_id") or "").strip(),
        "tb_cat_id": str(card.get("tb_cat_id") or "").strip(),
        "official_tag_codes": str(card.get("official_tag_codes") or "").strip(),
        "official_tag_names": str(card.get("official_tag_names") or "").strip(),
        "search_keyword": str(card.get("search_keyword") or search_keyword or "").strip(),
        "search_rank": _parse_optional_int(card.get("search_rank")),
    }


def _parse_search_result_item(
    item_wrap: Dict[str, Any],
    *,
    keyword: str = "",
    rank: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """解析 `mtop.taobao.idlemtopsearch.pc.search` 单条商品卡片数据。"""
    if not isinstance(item_wrap, dict):
        return None
    main = ((item_wrap.get("data") or {}).get("item") or {}).get("main") or {}
    if not isinstance(main, dict):
        return None
    ex = main.get("exContent")
    if not isinstance(ex, dict):
        return None

    dp = ex.get("detailParams") or {}
    cp_args = ((main.get("clickParam") or {}).get("args")) or {}
    if not isinstance(cp_args, dict):
        cp_args = {}
    item_id = str(ex.get("itemId") or dp.get("itemId") or "").strip()
    if not item_id:
        return None

    title = str(dp.get("title") or ex.get("title") or "").strip()

    raw_sold_price = dp.get("soldPrice")
    if raw_sold_price is not None and str(raw_sold_price).strip() != "":
        price = _parse_float_price(raw_sold_price)
        raw_price_text = str(raw_sold_price).strip()
    else:
        price_parts = ex.get("price") or []
        if isinstance(price_parts, list):
            joined = "".join(str(p.get("text") or "") for p in price_parts if isinstance(p, dict))
        else:
            joined = str(price_parts)
        price = _parse_float_price(joined)
        raw_price_text = joined.strip()

    original_price = _parse_optional_price(ex.get("oriPrice"))
    price_drop_amount: Optional[float] = None
    want_cnt = _parse_optional_int(ex.get("wantCnt") or dp.get("wantCnt"))
    official_tag_codes = str(cp_args.get("tag") or "").strip()
    is_free_shipping = "freeship" in official_tag_codes.lower()
    has_nfr_badge = False
    credit_tags: List[str] = []

    fish_tags = ex.get("fishTags") or {}
    if isinstance(fish_tags, dict):
        for region_val in fish_tags.values():
            if not isinstance(region_val, dict):
                continue
            for t in region_val.get("tagList") or []:
                if not isinstance(t, dict):
                    continue
                tag_data = t.get("data") or {}
                if not isinstance(tag_data, dict):
                    continue
                content = str(tag_data.get("content") or "").strip()
                if not content:
                    continue
                label_id = str(tag_data.get("labelId") or "").strip()
                is_strike = _parse_bool_flag(tag_data.get("isStrikeThrough"))
                if (is_strike or label_id == "41") and content.startswith("¥"):
                    if original_price is None:
                        original_price = _parse_optional_price(content)
                    continue
                if label_id == "749" or "降价" in content:
                    if price_drop_amount is None:
                        price_drop_amount = _parse_optional_price(content)
                    continue
                if content == "nfrIcon" or label_id == "664":
                    has_nfr_badge = True
                    continue
                if "想要" in content and want_cnt is None:
                    want_cnt = _parse_optional_int(content)
                elif content == "freeShippingIcon" or _text_has_free_shipping(content):
                    is_free_shipping = True
                elif not content.startswith("¥") and content not in _FAKE_ZERO_SHOP_LABELS and content not in credit_tags:
                    credit_tags.append(content)

    shop_label = ex.get("userFishShopLabel") or {}
    if isinstance(shop_label, dict):
        for t in shop_label.get("tagList") or []:
            if isinstance(t, dict):
                c = str((t.get("data") or {}).get("content") or "").strip()
                if c and c not in _FAKE_ZERO_SHOP_LABELS and c not in credit_tags:
                    credit_tags.append(c)

    pic_url = str(ex.get("picUrl") or "").strip()
    if pic_url.startswith("//"):
        pic_url = "https:" + pic_url

    publish_at = _parse_utc_datetime(cp_args.get("publishTime") or main.get("publishTime"))
    pic_width = _parse_optional_int(dp.get("picWidth"))
    if pic_width is None:
        pic_width = _parse_optional_int(ex.get("picWidth"))
    pic_height = _parse_optional_int(dp.get("picHeight"))
    if pic_height is None:
        pic_height = _parse_optional_int(ex.get("picHeight"))

    jump_args = ((ex.get("jump2XianYuHao") or {}).get("clickParam") or {}).get("args") or {}
    if not isinstance(jump_args, dict):
        jump_args = {}
    seller_id_enc = str(cp_args.get("seller_id") or jump_args.get("user_id") or "").strip()

    resolved_rank: Optional[int] = rank if isinstance(rank, int) and rank > 0 else None
    if resolved_rank is None:
        pos_val = _parse_optional_int(cp_args.get("position"))
        if pos_val is None:
            pos_val = _parse_optional_int(cp_args.get("index"))
        if pos_val is not None and pos_val >= 0:
            resolved_rank = pos_val + 1

    return normalize_search_card(
        {
            "item_id": item_id,
            "item_url": f"{GOOFISH_HOME_URL}/item?id={item_id}",
            "title": title,
            "price": price,
            "raw_price_text": raw_price_text,
            "original_price": original_price,
            "price_drop_amount": price_drop_amount,
            "want_cnt": want_cnt,
            "is_free_shipping": is_free_shipping,
            "has_nfr_badge": has_nfr_badge,
            "area": str(ex.get("area") or "").strip(),
            "seller_id_enc": seller_id_enc,
            "seller_nick": str(ex.get("userNickName") or dp.get("userNick") or "").strip(),
            "seller_tags": credit_tags,
            "image_url": pic_url,
            "publish_at": publish_at,
            "pic_width": pic_width,
            "pic_height": pic_height,
            "is_video": _parse_bool_flag(dp.get("isVideo")) or bool(ex.get("showVideoIcon")),
            "is_ad": _parse_bool_flag(ex.get("isAliMaMaAD")),
            "is_fish_shop": _parse_bool_flag(ex.get("userIsUseFishShopCard")) or _parse_bool_flag(cp_args.get("userIsUseFishShopCard")),
            "coin_discount": _parse_bool_flag(cp_args.get("CoinPay_Morediscount")),
            "cat_id": str(cp_args.get("catId") or "").strip(),
            "channel_cat_id": str(cp_args.get("cCatId") or "").strip(),
            "tb_cat_id": str(cp_args.get("tbCatId") or "").strip(),
            "official_tag_codes": official_tag_codes,
            "official_tag_names": str(cp_args.get("tagname") or "").strip(),
            "search_keyword": str(keyword or cp_args.get("keyword") or "").strip(),
            "search_rank": resolved_rank,
        },
        search_keyword=keyword,
    )


def _parse_sku_entry(sku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析商品详情 `itemDO.skuList` 中的单个规格（SKU）信息与对应价格。"""
    if not isinstance(sku, dict):
        return None

    price_cent = sku.get("priceInCent")
    if price_cent is None:
        price_cent = sku.get("price")
    price_cent_val = _parse_optional_float(price_cent)
    sku_price = round(price_cent_val / 100.0, 2) if price_cent_val is not None else None

    specs: Dict[str, str] = {}
    spec_parts: List[str] = []

    prop_list = sku.get("propertyList") or []
    if isinstance(prop_list, list) and prop_list:
        for p in prop_list:
            if not isinstance(p, dict):
                continue
            p_name = str(p.get("propertyText") or p.get("propertyName") or "").strip()
            p_val = str(p.get("actualValueText") or p.get("valueText") or "").strip()
            if p_name or p_val:
                key = p_name or "规格"
                specs[key] = p_val
                spec_parts.append(f"{key}: {p_val}" if p_name else p_val)

    features = sku.get("features") or {}
    if not spec_parts and isinstance(features, dict):
        pv_pairs = str(features.get("idlePvPairs") or "").strip()
        if pv_pairs:
            for seg in pv_pairs.split("$"):
                if "^" in seg:
                    left, right = seg.split("^", 1)
                    p_name = left.split("#", 1)[-1].strip() if "#" in left else left.strip()
                    p_val = right.split("#", 1)[-1].strip() if "#" in right else right.strip()
                    if p_name or p_val:
                        key = p_name or "规格"
                        specs[key] = p_val
                        spec_parts.append(f"{key}: {p_val}" if p_name else p_val)

    if not spec_parts:
        pv_do = ((sku.get("propertyImage") or {}).get("pvPairDO")) or {}
        if isinstance(pv_do, dict):
            p_name = str(pv_do.get("propertyText") or "").strip()
            p_val = str(pv_do.get("actualValueText") or pv_do.get("valueText") or "").strip()
            if p_name or p_val:
                key = p_name or "规格"
                specs[key] = p_val
                spec_parts.append(f"{key}: {p_val}" if p_name else p_val)

    qty_raw = sku.get("quantity")
    if qty_raw is None and isinstance(features, dict):
        qty_raw = features.get("idleOriginalQuantity")
    quantity = _parse_optional_int(qty_raw)

    return {
        "sku_id": str(sku.get("inventoryId") or sku.get("skuId") or "").strip(),
        "spec_text": " | ".join(spec_parts),
        "specs": specs,
        "price": sku_price,
        "quantity": quantity,
        "original_quantity": _parse_optional_int(features.get("idleOriginalQuantity")) if isinstance(features, dict) else None,
    }


def search_xianyu_items(
    keyword: str,
    max_items: int = 30,
    user_data_dir: str = DEFAULT_SEARCH_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    browser_pool: Optional[XianyuRoundBrowserPool] = None,
) -> Dict[str, Any]:
    """
    使用已登录的独立闲鱼目录（默认搜索小号 `W:\\temp\\xianyu_xiaohao`）搜索关键字商品概览：
    - 搜索前后强校验在线与风控状态，若未扫码登录、掉线或触发滑块立即返回 `ACCOUNT_OFFLINE` / `RISK_CONTROL_BLOCKED`，绝不盲目重试；
    - 拦截解析 `mtop.taobao.idlemtopsearch.pc.search` 返回的商品列表；
    - 可选传入 `browser_pool` 单轮复用持久化搜索上下文，结束时自动卸载局部监听器避免句柄泄漏。
    """
    kw_clean = str(keyword or "").strip()
    if not kw_clean:
        raise ValueError("search_xianyu_items: 搜索关键字 keyword 不能为空")

    limit = max(1, int(max_items or 30))
    tag = _dir_tag(user_data_dir)
    t0 = time.monotonic()

    session_cm = (
        browser_pool.acquire_search_session(
            user_data_dir=user_data_dir,
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            task_name=f"search_xianyu_items:{kw_clean[:20]}",
        )
        if browser_pool is not None
        else _browser_session_guard(
            user_data_dir=user_data_dir,
            debug=debug,
            task_name=f"search_xianyu_items:{kw_clean[:20]}",
            lock_timeout_sec=lock_timeout_sec,
        )
    )

    try:
        with session_cm as (context, page, auth_tracker):
            search_packets: List[Dict[str, Any]] = []
            bound_pages: List[Any] = []

            def _on_search_response(resp):
                if "mtop.taobao.idlemtopsearch.pc.search/" in resp.url:
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            search_packets.append(body)
                    except Exception:
                        pass

            def _bind_search_listener(target_page) -> None:
                if target_page not in bound_pages:
                    bound_pages.append(target_page)
                    target_page.on("response", _on_search_response)

            _bind_search_listener(page)
            if hasattr(context, "on"):
                context.on("page", _bind_search_listener)

            try:
                cur_url = str(getattr(page, "url", "") or "")
                if "goofish.com" not in cur_url:
                    page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                    for _ in range(15):
                        cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
                        if cookies.get("_m_h5_tk"):
                            break
                        page.wait_for_timeout(200)
                    page.wait_for_timeout(800)
                _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="search_xianyu_items")

                search_packets.clear()
                search_input = page.locator("input[class*='search-input'], input[type='text']").first
                triggered = False
                if search_input.count() > 0:
                    try:
                        search_input.click(timeout=3000)
                        search_input.fill(kw_clean, timeout=3000)
                        page.wait_for_timeout(150)
                        if search_input.input_value().strip() == kw_clean:
                            search_btn = page.locator("button[class*='search-icon'], button:has-text('搜索')").first
                            if search_btn.count() > 0:
                                search_btn.click(timeout=3000)
                            else:
                                search_input.press("Enter", timeout=3000)
                            triggered = True
                    except Exception:
                        triggered = False

                if not triggered:
                    page.goto(f"{GOOFISH_HOME_URL}/search?q={quote(kw_clean)}", wait_until="domcontentloaded", timeout=30000)

                success_packet: Optional[Dict[str, Any]] = None
                for _ in range(32):
                    for pkt in reversed(search_packets):
                        ret_str = " | ".join(str(x) for x in (pkt.get("ret") or []))
                        if "SUCCESS" in ret_str:
                            success_packet = pkt
                            break
                    if success_packet is not None or auth_tracker.get("risk_rets") or auth_tracker.get("expired_rets"):
                        break
                    page.wait_for_timeout(250)

                active_page = context.pages[-1] if context.pages else page
                _verify_account_online(context, active_page, auth_tracker, user_data_dir, task_name="search_xianyu_items")

                if success_packet is None:
                    diag_str = _format_failure_diag(context, active_page, auth_tracker, headless=not debug)
                    elapsed = time.monotonic() - t0
                    logger.error(
                        f"❌ [闲鱼搜索/未捕获有效数据包] 目录={tag} | 关键字=【{kw_clean}】 | "
                        f"已捕获包数={len(search_packets)} | 耗时={elapsed:.2f}s | 诊断=({diag_str})"
                    )
                    return {
                        "ok": False,
                        "is_offline": False,
                        "is_risk_blocked": False,
                        "action": "search_xianyu_items",
                        "error_code": "SEARCH_PACKET_EMPTY",
                        "user_data_dir": user_data_dir,
                        "keyword": kw_clean,
                        "total_count": 0,
                        "items": [],
                        "message": f"未捕获到关键字【{kw_clean}】的有效闲鱼搜索数据包 ({diag_str})",
                    }

                raw_list = (success_packet.get("data") or {}).get("resultList") or []
                parsed_items: List[Dict[str, Any]] = []
                for idx, wrap in enumerate(raw_list):
                    item_obj = _parse_search_result_item(wrap, keyword=kw_clean, rank=idx + 1)
                    if item_obj is not None:
                        parsed_items.append(item_obj)
                        if len(parsed_items) >= limit:
                            break

                prices = sorted(x["price"] for x in parsed_items if x["price"] > 0)
                min_p = prices[0] if prices else 0.0
                max_p = prices[-1] if prices else 0.0
                med_p = prices[len(prices) // 2] if prices else 0.0
                elapsed = time.monotonic() - t0

                logger.info(
                    f"[闲鱼搜索/完成] ✅ 目录={tag} | 关键字=【{kw_clean}】 | 返回商品数={len(parsed_items)} | "
                    f"价格区间=¥{min_p}~¥{max_p} (中位价=¥{med_p}) | 耗时={elapsed:.2f}s"
                )
                return {
                    "ok": True,
                    "is_offline": False,
                    "is_risk_blocked": False,
                    "action": "search_xianyu_items",
                    "user_data_dir": user_data_dir,
                    "keyword": kw_clean,
                    "total_count": len(parsed_items),
                    "price_summary": {"min_price": min_p, "median_price": med_p, "max_price": max_p},
                    "items": parsed_items,
                }
            finally:
                _detach_listener_safely(context, "page", _bind_search_listener)
                for bp in bound_pages:
                    _detach_listener_safely(bp, "response", _on_search_response)
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="search_xianyu_items")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="search_xianyu_items")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception(
            f"❌ [闲鱼搜索/浏览器运行异常] 目录={tag} (<{user_data_dir}>) | "
            f"关键字=【{kw_clean}】 | 耗时={elapsed:.2f}s | 异常={type(exc).__name__}: {exc}"
        )
        return {
            "ok": False,
            "is_offline": False,
            "is_risk_blocked": False,
            "action": "search_xianyu_items",
            "error_code": "BROWSER_EXCEPTION",
            "user_data_dir": user_data_dir,
            "keyword": kw_clean,
            "total_count": 0,
            "items": [],
            "message": f"浏览器执行搜索异常 ({type(exc).__name__}: {exc})",
        }


def normalize_item_detail(det: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SSOT：将任意闲鱼商品详情字典规范化为 Schema v2 标准结构（含嵌套 seller 与 UTC 时间）。"""
    if not isinstance(det, dict):
        return None
    item_id = str(det.get("item_id") or "").strip()
    if not item_id:
        return None

    item_url = str(det.get("item_url") or f"{GOOFISH_HOME_URL}/item?id={item_id}").strip()
    price = _parse_float_price(det.get("price"), default=0.0)
    sku_list = [dict(s) for s in (det.get("sku_list") or []) if isinstance(s, dict)]
    sku_prices = [p for p in (_parse_optional_price(s.get("price")) for s in sku_list) if p is not None]
    min_fallback = min(sku_prices) if sku_prices else price
    max_fallback = max(sku_prices) if sku_prices else price

    images: List[str] = []
    for raw_u in det.get("images") or []:
        u = str(raw_u or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        if u and u not in images:
            images.append(u)

    image_meta = [dict(m) for m in (det.get("image_meta") or []) if isinstance(m, dict)]
    seller_src = det.get("seller") if isinstance(det.get("seller"), dict) else {}
    seller = {
        "seller_id": str(seller_src.get("seller_id") or "").strip(),
        "nick": str(seller_src.get("nick") or "").strip(),
        "city": str(seller_src.get("city") or "").strip(),
        "register_at": _parse_utc_datetime(seller_src.get("register_at")),
        "seller_level": _parse_optional_int(seller_src.get("seller_level")),
        "sold_count": _parse_optional_int(seller_src.get("sold_count")),
        "good_rate": str(seller_src.get("good_rate") or "").strip(),
        "item_count": _parse_optional_int(seller_src.get("item_count")),
        "reg_days": _parse_optional_int(seller_src.get("reg_days")),
        "last_visit": str(seller_src.get("last_visit") or "").strip(),
        "reply_ratio_24h": _parse_optional_float(seller_src.get("reply_ratio_24h")),
        "reply_interval": str(seller_src.get("reply_interval") or "").strip(),
        "avg_reply_30d_min": _parse_optional_int(seller_src.get("avg_reply_30d_min")),
        "business_type": _parse_optional_int(seller_src.get("business_type")),
        "is_yxp_pro": _parse_optional_bool(seller_src.get("is_yxp_pro")),
        "zhima_auth": _parse_optional_bool(seller_src.get("zhima_auth")),
        "zhima_level": str(seller_src.get("zhima_level") or "").strip(),
        "good_remark_cnt": _parse_optional_int(seller_src.get("good_remark_cnt")),
        "bad_remark_cnt": _parse_optional_int(seller_src.get("bad_remark_cnt")),
        "default_remark_cnt": _parse_optional_int(seller_src.get("default_remark_cnt")),
    }

    result = {
        "ok": True,
        "item_id": item_id,
        "item_url": item_url,
        "title": str(det.get("title") or "").strip(),
        "desc": str(det.get("desc") or "").strip(),
        "price": price,
        "original_price": _parse_optional_price(det.get("original_price")),
        "transport_fee": _parse_optional_float(det.get("transport_fee")),
        "is_free_shipping": bool(det.get("is_free_shipping")),
        "min_price": _parse_float_price(det.get("min_price"), default=min_fallback),
        "max_price": _parse_float_price(det.get("max_price"), default=max_fallback),
        "has_multi_sku": bool(sku_list) if sku_list else bool(det.get("has_multi_sku")),
        "sku_list": sku_list,
        "cpv_labels": [dict(lbl) for lbl in (det.get("cpv_labels") or []) if isinstance(lbl, dict)],
        "want_cnt": _parse_optional_int(det.get("want_cnt")),
        "browse_cnt": _parse_optional_int(det.get("browse_cnt")),
        "quantity": _parse_optional_int(det.get("quantity")),
        "collect_cnt": _parse_optional_int(det.get("collect_cnt")),
        "coin_discount": bool(det.get("coin_discount")),
        "publish_at": _parse_utc_datetime(det.get("publish_at")),
        "item_status": _parse_optional_int(det.get("item_status")),
        "item_status_str": str(det.get("item_status_str") or "").strip(),
        "bargained": bool(det.get("bargained")),
        "title_is_user_input": _parse_optional_bool(det.get("title_is_user_input")),
        "category_id": str(det.get("category_id") or "").strip(),
        "item_cat": dict(det.get("item_cat") or {}) if isinstance(det.get("item_cat"), dict) else {},
        "item_labels": [str(x).strip() for x in (det.get("item_labels") or []) if str(x).strip()],
        "service_tags": [str(x).strip() for x in (det.get("service_tags") or []) if str(x).strip()],
        "images": images,
        "image_meta": image_meta,
        "first_pic_width": _parse_optional_int(det.get("first_pic_width")),
        "first_pic_height": _parse_optional_int(det.get("first_pic_height")),
        "has_video": bool(det.get("has_video")),
        "seller": seller,
    }
    shot_file = str(det.get("screenshot_file") or "").strip()
    if shot_file:
        result["screenshot_file"] = shot_file
    return result


def _parse_detail_packet_to_dict(
    success_packet: Dict[str, Any],
    item_id: str,
    detail_url: str,
    user_data_dir: str,
) -> Dict[str, Any]:
    """将捕获到的 `mtop.taobao.idle.awesome.detail` / `mtop.taobao.idle.pc.detail` 成功响应解析为标准商品详情字典。"""
    data = success_packet.get("data") or {}
    item_do = data.get("itemDO") or {}
    seller_do = data.get("sellerDO") or {}

    desc = str(item_do.get("desc") or "").strip()
    title = str(item_do.get("title") or "").strip()
    if not title and desc:
        title = desc.splitlines()[0].strip()

    price = _parse_float_price(item_do.get("soldPrice"), default=0.0)
    original_price = _parse_optional_price(item_do.get("originalPrice"))
    transport_fee = _parse_optional_float(item_do.get("transportFee"))

    rel_tags = item_do.get("priceRelativeTags") or []
    common_tags = item_do.get("commonTags") or []
    price_text_tags = item_do.get("priceTextTags") or []
    all_tag_dicts = [
        t
        for t in (
            *(price_text_tags if isinstance(price_text_tags, list) else []),
            *(common_tags if isinstance(common_tags, list) else []),
            *(rel_tags if isinstance(rel_tags, list) else []),
        )
        if isinstance(t, dict)
    ]
    has_baoyou_tag = any(
        _text_has_free_shipping(str(t.get("text") or "")) or (t.get("trackParams") or {}).get("tagType") == "baoyou"
        for t in all_tag_dicts
    )
    coin_discount = any((t.get("trackParams") or {}).get("tagType") == "coin_pay" for t in all_tag_dicts)
    is_free_shipping = bool(
        (transport_fee is None or transport_fee == 0.0)
        and (has_baoyou_tag or _text_has_free_shipping(desc) or _text_has_free_shipping(title))
    )

    raw_skus = item_do.get("skuList") or item_do.get("idleItemSkuList") or []
    sku_list: List[Dict[str, Any]] = []
    if isinstance(raw_skus, list):
        for s in raw_skus:
            parsed_sku = _parse_sku_entry(s)
            if parsed_sku is not None:
                sku_list.append(parsed_sku)

    sku_prices = [s["price"] for s in sku_list if s.get("price") is not None and s["price"] > 0]
    min_price = _parse_float_price(item_do.get("minPrice"), default=(min(sku_prices) if sku_prices else price))
    max_price = _parse_float_price(item_do.get("maxPrice"), default=(max(sku_prices) if sku_prices else price))

    cpv_labels: List[Dict[str, str]] = []
    for lbl in item_do.get("cpvLabels") or []:
        if isinstance(lbl, dict):
            p_name = str(lbl.get("propertyName") or "").strip()
            v_name = str(lbl.get("valueName") or "").strip()
            if p_name or v_name:
                cpv_labels.append({"name": p_name, "value": v_name})

    images: List[str] = []
    image_meta: List[Dict[str, Any]] = []
    for img in item_do.get("imageInfos") or []:
        if isinstance(img, dict):
            u = str(img.get("url") or "").strip()
            if u.startswith("//"):
                u = "https:" + u
            if u and u not in images:
                images.append(u)
            image_meta.append({
                "width": _parse_optional_int(img.get("widthSize")),
                "height": _parse_optional_int(img.get("heightSize")),
                "major": bool(img.get("major")),
                "type": _parse_optional_int(img.get("type")),
            })

    major_img_meta = next((m for m in image_meta if m.get("major")), image_meta[0] if image_meta else {})
    first_pic_width = major_img_meta.get("width") if isinstance(major_img_meta, dict) else None
    first_pic_height = major_img_meta.get("height") if isinstance(major_img_meta, dict) else None
    has_video = bool(item_do.get("videoInfo") or item_do.get("video")) or any(
        (m.get("type") or 0) != 0 for m in image_meta
    )

    publish_at = _parse_utc_datetime(item_do.get("gmtCreate"))
    if publish_at is None:
        publish_at = _parse_utc_datetime(item_do.get("GMT_CREATE_DATE_KEY"))

    raw_cat = item_do.get("itemCatDTO")
    item_cat: Dict[str, Any] = {}
    if isinstance(raw_cat, dict):
        for k in ("catId", "tbCatId", "channelCatId", "rootChannelCatId", "level2ChannelCatId", "level3ChannelCatId", "leafId"):
            if raw_cat.get(k) is not None and raw_cat.get(k) != "":
                item_cat[k] = raw_cat.get(k)

    item_labels: List[str] = []
    for ext in item_do.get("itemLabelExtList") or []:
        if isinstance(ext, dict):
            txt = str(ext.get("valueText") or ext.get("text") or "").strip()
            if txt and txt not in item_labels:
                item_labels.append(txt)

    service_tags: List[str] = []
    for tag_list in (rel_tags, common_tags):
        if isinstance(tag_list, list):
            for t in tag_list:
                if isinstance(t, dict):
                    txt = str(t.get("text") or "").strip()
                    if txt and txt not in service_tags:
                        service_tags.append(txt)

    remark_do = seller_do.get("remarkDO") if isinstance(seller_do.get("remarkDO"), dict) else {}
    zhima_info = seller_do.get("zhimaLevelInfo") if isinstance(seller_do.get("zhimaLevelInfo"), dict) else {}
    credit_tag = seller_do.get("idleFishCreditTag") if isinstance(seller_do.get("idleFishCreditTag"), dict) else {}
    credit_tp = credit_tag.get("trackParams") if isinstance(credit_tag.get("trackParams"), dict) else {}

    clean_det = normalize_item_detail(
        {
            "item_id": item_id,
            "item_url": detail_url,
            "title": title,
            "desc": desc,
            "price": price,
            "original_price": original_price,
            "transport_fee": transport_fee,
            "is_free_shipping": is_free_shipping,
            "min_price": min_price,
            "max_price": max_price,
            "has_multi_sku": len(sku_list) > 0,
            "sku_list": sku_list,
            "cpv_labels": cpv_labels,
            "want_cnt": _parse_optional_int(item_do.get("wantCnt")),
            "browse_cnt": _parse_optional_int(item_do.get("browseCnt")),
            "quantity": _parse_optional_int(item_do.get("quantity")),
            "collect_cnt": _parse_optional_int(item_do.get("collectCnt")),
            "coin_discount": coin_discount,
            "publish_at": publish_at,
            "item_status": _parse_optional_int(item_do.get("itemStatus")),
            "item_status_str": str(item_do.get("itemStatusStr") or "").strip(),
            "bargained": bool(item_do.get("bargained")),
            "title_is_user_input": _parse_optional_bool(item_do.get("titleIsUserInput")),
            "category_id": str(item_do.get("categoryId") or "").strip(),
            "item_cat": item_cat,
            "item_labels": item_labels,
            "service_tags": service_tags,
            "images": images,
            "image_meta": image_meta,
            "first_pic_width": first_pic_width,
            "first_pic_height": first_pic_height,
            "has_video": has_video,
            "seller": {
                "seller_id": str(seller_do.get("sellerId") or "").strip(),
                "nick": str(seller_do.get("nick") or "").strip(),
                "city": str(seller_do.get("city") or seller_do.get("publishCity") or "").strip(),
                "register_at": _parse_utc_datetime(seller_do.get("registerTime")),
                "seller_level": _parse_optional_int(credit_tp.get("sellerLevel")),
                "sold_count": _parse_optional_int(seller_do.get("hasSoldNumInteger")),
                "good_rate": str(seller_do.get("newGoodRatioRate") or "").strip(),
                "item_count": _parse_optional_int(seller_do.get("itemCount")),
                "reg_days": _parse_optional_int(seller_do.get("userRegDay")),
                "last_visit": str(seller_do.get("lastVisitTime") or "").strip(),
                "reply_ratio_24h": _parse_reply_ratio(seller_do),
                "reply_interval": str(seller_do.get("replyInterval") or "").strip(),
                "avg_reply_30d_min": _parse_optional_int(seller_do.get("avgReply30dLong")),
                "business_type": _parse_optional_int(seller_do.get("businessType")),
                "is_yxp_pro": _parse_optional_bool(seller_do.get("yxpPro")),
                "zhima_auth": _parse_optional_bool(seller_do.get("zhimaAuth")),
                "zhima_level": str(zhima_info.get("levelName") or "").strip(),
                "good_remark_cnt": _parse_optional_int(remark_do.get("sellerGoodRemarkCnt")),
                "bad_remark_cnt": _parse_optional_int(remark_do.get("sellerBadRemarkCnt")),
                "default_remark_cnt": _parse_optional_int(remark_do.get("sellerDefaultRemarkCnt")),
            },
        }
    ) or {}

    return {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "action": "get_xianyu_item_detail",
        "user_data_dir": user_data_dir,
        **clean_det,
    }


_SCREENSHOT_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _capture_item_screenshot(page: Any, item_id: str, screenshot_dir: Optional[str]) -> str:
    """在已打开的闲鱼 H5 详情页捕获首屏 JPEG 截图，异常绝不阻断详情抓取。"""
    clean_dir = str(screenshot_dir or "").strip()
    clean_id = str(item_id or "").strip()
    if not clean_dir or not _SCREENSHOT_ITEM_ID_RE.fullmatch(clean_id):
        return ""
    if not callable(getattr(page, "screenshot", None)):
        return ""

    target_dir = Path(clean_dir)
    filename = f"{clean_id}.jpg"
    target_path = target_dir / filename
    tmp_path = target_dir / f".tmp_{clean_id}_{os.getpid()}.jpg"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        wait_fn = getattr(page, "wait_for_timeout", None)
        if callable(wait_fn):
            wait_fn(400)
        page.screenshot(path=str(tmp_path), type="jpeg", quality=75, full_page=False)
        if tmp_path.is_file() and tmp_path.stat().st_size > 0:
            os.replace(tmp_path, target_path)
            return filename
    except Exception as exc:
        logger.warning(f"⚠️ [H5商品截图保存失败] item_id={clean_id} | 原因={exc}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return ""


def get_xianyu_item_details_batch(
    item_ids: List[str],
    user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    ephemeral: bool = True,
    browser_pool: Optional[XianyuRoundBrowserPool] = None,
    screenshot_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    通过闲鱼移动端 H5 公开分享页免登录批量获取多个商品详情（描述、多规格 SKU 价格、库存与运费）：
    - 默认采用纯内存无痕会话（`ephemeral=True`），以 `user_data_dir` 作为串行排队锁标识，绝不持久化累积访客 Cookie；
    - 完全不占用搜索小号请求配额，自动识别风控拦截并立即熔断停手；
    - 可选传入 `browser_pool` 单轮复用 H5 `_m_h5_tk` 会话（每 10 件自动轮换），根除频繁冷启动换令牌导致的 `RGV587_ERROR`。
    """
    clean_ids: List[str] = []
    for raw_target in item_ids or []:
        cid = _extract_item_id(raw_target)
        if cid and cid not in clean_ids:
            clean_ids.append(cid)

    if not clean_ids:
        return {"ok": True, "is_risk_blocked": False, "details_by_id": {}}

    tag = f"ephemeral_h5({ _dir_tag(user_data_dir) })" if ephemeral else _dir_tag(user_data_dir)
    t0 = time.monotonic()
    details_by_id: Dict[str, Dict[str, Any]] = {}
    if browser_pool is not None and ephemeral:
        session_cm = browser_pool.acquire_h5_detail_session(
            lock_key_dir=user_data_dir,
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            task_name=f"get_xianyu_item_details_batch:{len(clean_ids)}items",
            item_count=len(clean_ids),
        )
    elif ephemeral:
        session_cm = _ephemeral_h5_session_guard(
            lock_key_dir=user_data_dir,
            debug=debug,
            task_name=f"get_xianyu_item_details_batch:{len(clean_ids)}items",
            lock_timeout_sec=lock_timeout_sec,
        )
    else:
        session_cm = _browser_session_guard(
            user_data_dir=user_data_dir,
            debug=debug,
            mobile_h5=True,
            task_name=f"get_xianyu_item_details_batch:{len(clean_ids)}items",
            lock_timeout_sec=lock_timeout_sec,
        )

    try:
        with session_cm as (context, page, auth_tracker):
            detail_packets: List[Dict[str, Any]] = []

            def _on_response(resp):
                if "mtop.taobao.idle.awesome.detail/" in resp.url or "mtop.taobao.idle.pc.detail/" in resp.url:
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            detail_packets.append(body)
                    except Exception:
                        pass

            page.on("response", _on_response)
            try:
                for idx, item_id in enumerate(clean_ids):
                    t_item = time.monotonic()
                    pc_detail_url = f"{GOOFISH_HOME_URL}/item?id={item_id}"
                    h5_detail_url = f"{GOOFISH_H5_HOME_URL}/item?id={item_id}"
                    success_packet: Optional[Dict[str, Any]] = None
                    biz_not_found = False
                    auth_tracker["failed_apis"].clear()

                    for attempt in range(2):
                        detail_packets.clear()
                        page.goto(h5_detail_url, wait_until="domcontentloaded", timeout=30000)
                        for _ in range(28):
                            for pkt in reversed(detail_packets):
                                ret_str = " | ".join(str(x) for x in (pkt.get("ret") or []))
                                if "SUCCESS" in ret_str and isinstance((pkt.get("data") or {}).get("itemDO"), dict):
                                    success_packet = pkt
                                    break
                                if "FAIL_BIZ_" in ret_str or "ITEM_NOT_FOUND" in ret_str:
                                    biz_not_found = True
                                    break
                            if (
                                success_packet is not None
                                or biz_not_found
                                or auth_tracker.get("risk_rets")
                                or auth_tracker.get("guest_login_blocked_rets")
                            ):
                                break
                            page.wait_for_timeout(200)

                        _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name=f"batch_detail:{item_id}")
                        if success_packet is not None or biz_not_found:
                            break
                        if attempt == 0:
                            page.wait_for_timeout(900)

                    if success_packet is not None:
                        parsed_detail = _parse_detail_packet_to_dict(
                            success_packet=success_packet,
                            item_id=item_id,
                            detail_url=pc_detail_url,
                            user_data_dir=user_data_dir,
                        )
                        if screenshot_dir:
                            shot_file = _capture_item_screenshot(page, item_id, screenshot_dir)
                            if shot_file:
                                parsed_detail["screenshot_file"] = shot_file
                        details_by_id[item_id] = parsed_detail
                        logger.info(
                            f"[H5免登录批量详情/{idx + 1}/{len(clean_ids)}] ✅ 会话={tag} | 商品ID={item_id} | "
                            f"售价=¥{parsed_detail['price']} | 运费=¥{parsed_detail['transport_fee']} | "
                            f"SKU数={len(parsed_detail['sku_list'])} | 耗时={time.monotonic() - t_item:.2f}s"
                        )
                    else:
                        diag_str = _format_failure_diag(context, page, auth_tracker, headless=not debug)
                        logger.warning(
                            f"⚠️ [H5免登录批量详情/{idx + 1}/{len(clean_ids)}/失败] 会话={tag} | 商品ID={item_id} | "
                            f"已捕获包数={len(detail_packets)} | 耗时={time.monotonic() - t_item:.2f}s | 诊断=({diag_str})"
                        )
                        details_by_id[item_id] = {
                            "ok": False,
                            "is_offline": False,
                            "is_risk_blocked": False,
                            "action": "get_xianyu_item_detail",
                            "error_code": "DETAIL_PACKET_EMPTY",
                            "user_data_dir": user_data_dir,
                            "item_id": item_id,
                            "item_url": pc_detail_url,
                            "message": f"未捕获到商品 ({item_id}) 的有效详情数据包 ({diag_str})",
                        }

                    if idx < len(clean_ids) - 1:
                        page.wait_for_timeout(random.randint(3600, 5800))

                ok_cnt = sum(1 for v in details_by_id.values() if v.get("ok"))
                fail_cnt = len(clean_ids) - ok_cnt
                logger.info(
                    f"[H5免登录批量详情/完成] 📊 会话={tag} | 成功={ok_cnt}/{len(clean_ids)} (失败={fail_cnt}) | "
                    f"总耗时={time.monotonic() - t0:.2f}s"
                )
                return {
                    "ok": True,
                    "is_offline": False,
                    "is_risk_blocked": False,
                    "action": "get_xianyu_item_details_batch",
                    "user_data_dir": user_data_dir,
                    "details_by_id": details_by_id,
                }
            finally:
                _detach_listener_safely(page, "response", _on_response)
    except XianyuOfflineError as e:
        res = _build_offline_result(
            user_data_dir,
            e.reason,
            error_code=e.error_code,
            action="get_xianyu_item_details_batch",
            is_guest=True,
        )
        res["details_by_id"] = details_by_id
        return res
    except XianyuLockTimeoutError as e:
        res = _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_xianyu_item_details_batch")
        res["details_by_id"] = details_by_id
        return res
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception(
            f"❌ [H5免登录批量详情/浏览器运行异常] 目录={tag} (<{user_data_dir}>) | "
            f"已完成={len(details_by_id)}/{len(clean_ids)} | 耗时={elapsed:.2f}s | 异常={type(exc).__name__}: {exc}"
        )
        return {
            "ok": False,
            "is_offline": False,
            "is_risk_blocked": False,
            "action": "get_xianyu_item_details_batch",
            "error_code": "BROWSER_EXCEPTION",
            "user_data_dir": user_data_dir,
            "details_by_id": details_by_id,
            "message": f"批量拉取详情浏览器异常 ({type(exc).__name__}: {exc})",
        }


def get_xianyu_item_detail(
    item_id_or_url: str,
    user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    screenshot_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """移动端 H5 免登录获取单个商品完整详情（内部直接复用 get_xianyu_item_details_batch，零重复逻辑）。"""
    item_id = _extract_item_id(item_id_or_url)
    if not item_id:
        raise ValueError("get_xianyu_item_detail: item_id_or_url 不能为空")
    batch_res = get_xianyu_item_details_batch(
        item_ids=[item_id],
        user_data_dir=user_data_dir,
        debug=debug,
        lock_timeout_sec=lock_timeout_sec,
        screenshot_dir=screenshot_dir,
    )
    if not batch_res.get("ok"):
        res = dict(batch_res)
        res.pop("details_by_id", None)
        res["action"] = "get_xianyu_item_detail"
        res["item_id"] = item_id
        res["item_url"] = f"{GOOFISH_HOME_URL}/item?id={item_id}"
        return res
    return batch_res["details_by_id"][item_id]
