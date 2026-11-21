# -*- coding: utf-8 -*-
"""闲鱼公开市场比价检索能力：搜索小号登录搜索概览 + 移动端 H5 免登录批量/单条获取商品完整详情。"""

import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

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


def _parse_search_result_item(item_wrap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析 `mtop.taobao.idlemtopsearch.pc.search` 单条商品卡片数据。"""
    if not isinstance(item_wrap, dict):
        return None
    ex = (
        ((item_wrap.get("data") or {}).get("item") or {})
        .get("main", {})
        .get("exContent")
    )
    if not isinstance(ex, dict):
        return None

    dp = ex.get("detailParams") or {}
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

    original_price = _parse_float_price(ex.get("oriPrice"), default=0.0)
    want_cnt = _parse_int_num(ex.get("wantCnt") or dp.get("wantCnt"), default=0)
    is_free_shipping = "包邮" in title
    credit_tags: List[str] = []

    fish_tags = ex.get("fishTags") or {}
    if isinstance(fish_tags, dict):
        for region_val in fish_tags.values():
            if not isinstance(region_val, dict):
                continue
            for t in region_val.get("tagList") or []:
                if not isinstance(t, dict):
                    continue
                content = str((t.get("data") or {}).get("content") or "").strip()
                if not content:
                    continue
                if "想要" in content and want_cnt == 0:
                    want_cnt = _parse_int_num(content, default=0)
                elif content == "freeShippingIcon" or "包邮" in content:
                    is_free_shipping = True
                elif not content.startswith("¥") and content not in credit_tags:
                    credit_tags.append(content)

    shop_label = ex.get("userFishShopLabel") or {}
    if isinstance(shop_label, dict):
        for t in shop_label.get("tagList") or []:
            if isinstance(t, dict):
                c = str((t.get("data") or {}).get("content") or "").strip()
                if c and c not in credit_tags:
                    credit_tags.append(c)

    pic_url = str(ex.get("picUrl") or "").strip()
    if pic_url.startswith("//"):
        pic_url = "https:" + pic_url

    return {
        "item_id": item_id,
        "item_url": f"{GOOFISH_HOME_URL}/item?id={item_id}",
        "title": title,
        "price": price,
        "raw_price_text": raw_price_text,
        "original_price": original_price,
        "want_cnt": want_cnt,
        "is_free_shipping": is_free_shipping,
        "area": str(ex.get("area") or "").strip(),
        "seller_nick": str(ex.get("userNickName") or dp.get("userNick") or "").strip(),
        "seller_tags": credit_tags,
        "image_url": pic_url,
    }


def _parse_sku_entry(sku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析商品详情 `itemDO.skuList` 中的单个规格（SKU）信息与对应价格。"""
    if not isinstance(sku, dict):
        return None

    price_cent = sku.get("priceInCent")
    if price_cent is None:
        price_cent = sku.get("price")
    sku_price = round(float(price_cent) / 100.0, 2) if price_cent is not None else 0.0

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
    quantity = _parse_int_num(qty_raw, default=0)

    return {
        "sku_id": str(sku.get("inventoryId") or sku.get("skuId") or "").strip(),
        "spec_text": " | ".join(spec_parts),
        "specs": specs,
        "price": sku_price,
        "quantity": quantity,
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
                for wrap in raw_list:
                    item_obj = _parse_search_result_item(wrap)
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
    original_price = _parse_float_price(item_do.get("originalPrice"), default=0.0)
    transport_fee = _parse_float_price(item_do.get("transportFee"), default=0.0)

    rel_tags = item_do.get("priceRelativeTags") or []
    has_baoyou_tag = any(
        isinstance(t, dict) and ("包邮" in str(t.get("text") or "") or (t.get("trackParams") or {}).get("tagType") == "baoyou")
        for t in rel_tags
    )
    is_free_shipping = bool(transport_fee == 0.0 and (has_baoyou_tag or "包邮" in desc or "包邮" in title))

    raw_skus = item_do.get("skuList") or item_do.get("idleItemSkuList") or []
    sku_list: List[Dict[str, Any]] = []
    if isinstance(raw_skus, list):
        for s in raw_skus:
            parsed_sku = _parse_sku_entry(s)
            if parsed_sku is not None:
                sku_list.append(parsed_sku)

    sku_prices = [s["price"] for s in sku_list if s["price"] > 0]
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
    for img in item_do.get("imageInfos") or []:
        if isinstance(img, dict):
            u = str(img.get("url") or "").strip()
            if u.startswith("//"):
                u = "https:" + u
            if u and u not in images:
                images.append(u)

    want_cnt = _parse_int_num(item_do.get("wantCnt"), default=0)
    browse_cnt = _parse_int_num(item_do.get("browseCnt"), default=0)
    quantity = _parse_int_num(item_do.get("quantity"), default=1)

    seller_nick = str(seller_do.get("nick") or "").strip()
    seller_city = str(seller_do.get("city") or seller_do.get("publishCity") or "").strip()
    seller_sold_count = _parse_int_num(seller_do.get("hasSoldNumInteger"), default=0)
    seller_good_rate = str(seller_do.get("newGoodRatioRate") or "").strip()

    return {
        "ok": True,
        "is_offline": False,
        "is_risk_blocked": False,
        "action": "get_xianyu_item_detail",
        "user_data_dir": user_data_dir,
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
        "want_cnt": want_cnt,
        "browse_cnt": browse_cnt,
        "quantity": quantity,
        "images": images,
        "seller_nick": seller_nick,
        "seller_city": seller_city,
        "seller_sold_count": seller_sold_count,
        "seller_good_rate": seller_good_rate,
    }


def get_xianyu_item_details_batch(
    item_ids: List[str],
    user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    ephemeral: bool = True,
    browser_pool: Optional[XianyuRoundBrowserPool] = None,
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
    )
    if not batch_res.get("ok"):
        res = dict(batch_res)
        res.pop("details_by_id", None)
        res["action"] = "get_xianyu_item_detail"
        res["item_id"] = item_id
        res["item_url"] = f"{GOOFISH_HOME_URL}/item?id={item_id}"
        return res
    return batch_res["details_by_id"][item_id]
