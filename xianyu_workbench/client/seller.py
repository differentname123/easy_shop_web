# -*- coding: utf-8 -*-
"""闲鱼卖家工作台与 IM 消息只读自动化能力：手动接管登录、在售查询与消息监控。"""

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .session import (
    DEFAULT_LOCK_TIMEOUT_SEC,
    DEFAULT_OPERATION_TIMEOUT_SEC,
    DEFAULT_USER_DATA_DIR,
    GOOFISH_HOME_URL,
    GOOFISH_IM_URL,
    GOOFISH_PERSONAL_URL,
    XianyuLockTimeoutError,
    XianyuOfflineError,
    _acquire_serial_browser_lock,
    _browser_session_guard,
    _build_lock_timeout_result,
    _build_offline_result,
    _dir_tag,
    _verify_account_online,
    clean_browser_cache,
    launch_persistent_context,
    logger,
    sync_playwright,
)


def open_browser_for_manual_use(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    home_url: str = GOOFISH_PERSONAL_URL,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    lock_scope: str = "global",
) -> None:
    """
    启动独立浏览器目录并移交人工操作（如首次扫码登录、掉线后重登、日常维护），直接关闭浏览器窗口即可自动保存并结束。
    """
    logger.info(f"\n{'=' * 60}\n[人工接管/启动] 正在打开闲鱼浏览器 | 目录: <{user_data_dir}>\n{'=' * 60}")
    with _acquire_serial_browser_lock(
        user_data_dir=user_data_dir,
        task_name="open_browser_for_manual_use",
        lock_timeout_sec=lock_timeout_sec,
        lock_scope=lock_scope,
    ):
        clean_browser_cache(user_data_dir)
        with sync_playwright() as p:
            context = None
            try:
                context = launch_persistent_context(
                    p, user_data_dir=user_data_dir, headless=False
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.bring_to_front()
                page.goto(home_url)
                logger.info("[人工接管/就绪] ✅ 浏览器已打开 | 提示: [扫码或操作完成后，直接关闭浏览器窗口即可自动保存]")
                page.wait_for_event("close", timeout=0)
            except Exception as e:
                logger.warning(f"[人工接管/关闭] 窗口已关闭: {e}")
            finally:
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
                logger.info(f"[人工接管/完成] 👋 窗口已关闭，账号登录态已持久化在 <{user_data_dir}>\n")


def check_login_status(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    *,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
    lock_scope: str = "global",
) -> Dict[str, Any]:
    """
    快速检测指定浏览器目录的闲鱼账号是否在线。
    在线返回 `{"ok": True, "is_offline": False, "unb": "...", "nick": "..."}`；
    掉线则打印醒目告警并返回 `{"ok": False, "is_offline": True, "error_code": "ACCOUNT_OFFLINE", ...}`。
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name="check_login_status",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
            lock_scope=lock_scope,
        ) as (context, page, auth_tracker):
            page.goto(GOOFISH_PERSONAL_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            unb = _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="check_login_status")
            nick = auth_tracker.get("display_name", "")
            elapsed = time.monotonic() - t0
            logger.info(
                f"[登录检查/在线] ✅ 账号目录={tag} | 昵称={nick or '未知'} (unb={unb}) | 耗时={elapsed:.2f}s"
            )
            return {
                "ok": True,
                "is_offline": False,
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
            }
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="check_login_status")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="check_login_status")


def _status_code_to_label(status_code: Any) -> Tuple[bool, str]:
    """
    根据闲鱼底层 `itemStatus` 判定商品是否真正正常在售：
      - `0`  -> (True, "在售(正常)")
      - `<0` -> (False, "已被下架/删除")  # 如 -9：虽残留在「在售」列表，但实际已被下架或删除
      - `1`  -> (False, "已售出/已下架")
    """
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False, f"未知状态({status_code})"
    if code == 0:
        return True, "在售(正常)"
    if code < 0:
        return False, f"已被下架/删除(status={code})"
    return False, f"非在售(status={code})"


# ==============================================================================
# 三、 核心函数 1：查询当前在售商品（闭环感知是否在架 / 被平台下架 / 是否掉线）
# ==============================================================================
def get_selling_items(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    expected_item_ids: Optional[List[str]] = None,
    debug: bool = False,
    *,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
    lock_scope: str = "global",
) -> Dict[str, Any]:
    """
    获取当前账号在闲鱼上的商品在售状态（精准区分 `itemStatus == 0` 真正正常在售 与 `itemStatus != 0` 被平台下架/删除）：
      - 若账号已掉线，立即打印醒目告警并返回 `{"ok": False, "is_offline": True, "error_code": "ACCOUNT_OFFLINE", ...}`
      - `online_items`    : `itemStatus == 0` 真正正常在架售卖的商品列表
      - `abnormal_items`  : 虽在「在售」标签页内但 `itemStatus != 0`（如 `-9` 已被平台下架/删除）的异常商品列表
    若额外传入 `expected_item_ids`（你记录的已上架商品 ID 列表），还会返回：
      - `online_item_ids`          : 仍正常在售的商品 ID
      - `missing_or_downshelf_ids` : 已被下架/删除或不在售的商品 ID
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    logger.info(
        f"[在售查询/开始] 账号目录={tag} (<{user_data_dir}>) | "
        f"待核对预期商品数={len(expected_item_ids) if expected_item_ids else 0}"
    )
    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name="get_selling_items",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
            lock_scope=lock_scope,
        ) as (context, page, auth_tracker):
            group_summary: Dict[str, int] = {}
            selling_packets: List[Dict[str, Any]] = []

            def on_response(resp):
                if "mtop.idle.web.xyh.item.list" in resp.url:
                    try:
                        body = resp.json()
                        data = body.get("data") or {}
                        for g in data.get("itemGroupList") or []:
                            if isinstance(g, dict) and g.get("groupName"):
                                group_summary[str(g["groupName"])] = int(g.get("itemNumber") or 0)
                        if data.get("cardList") is not None:
                            selling_packets.append(data)
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(GOOFISH_PERSONAL_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            unb = _verify_account_online(
                context, page, auth_tracker, user_data_dir, task_name="get_selling_items"
            )

            selling_packets.clear()
            zaisou_tab = page.get_by_text("在售", exact=False).first
            if zaisou_tab.count() > 0:
                zaisou_tab.click()
                for _ in range(20):
                    if selling_packets or auth_tracker.get("expired_rets"):
                        break
                    page.wait_for_timeout(300)
                page.wait_for_timeout(500)

                _verify_account_online(
                    context, page, auth_tracker, user_data_dir, task_name="get_selling_items"
                )

                while selling_packets and selling_packets[-1].get("nextPage") is True:
                    prev_len = len(selling_packets)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    for _ in range(15):
                        if len(selling_packets) > prev_len:
                            break
                        page.wait_for_timeout(300)
                    if len(selling_packets) == prev_len:
                        break
            else:
                logger.warning(
                    f"⚠️ [在售查询/DOM诊断] 账号目录={tag} | 在个人主页未找到「在售」Tab 元素 | 当前URL={page.url}"
                )

            online_items: List[Dict[str, Any]] = []
            abnormal_items: List[Dict[str, Any]] = []
            seen_ids = set()
            online_id_set = set()

            for pkt in selling_packets:
                for card in pkt.get("cardList") or []:
                    cd = (card.get("cardData") or {}) if isinstance(card, dict) else {}
                    dp = cd.get("detailParams") or {}
                    item_id = str(cd.get("id") or dp.get("itemId") or "").strip()
                    if not item_id or item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    want_text = ""
                    r3_tags = (((cd.get("itemLabelDataVO") or {}).get("labelData") or {}).get("r3") or {}).get("tagList") or []
                    if r3_tags and isinstance(r3_tags[0], dict):
                        want_text = str((r3_tags[0].get("data") or {}).get("content") or "")

                    raw_status = cd.get("itemStatus")
                    is_online, status_label = _status_code_to_label(raw_status)
                    title_str = str(cd.get("title") or dp.get("title") or "")
                    price_str = str((cd.get("priceInfo") or {}).get("price") or dp.get("soldPrice") or "")

                    item_entry = {
                        "item_id": item_id,
                        "title": title_str,
                        "price": price_str,
                        "post_info": str(dp.get("postInfo") or ""),
                        "category_id": cd.get("categoryId"),
                        "item_status": raw_status,
                        "is_online": is_online,
                        "status_label": status_label,
                        "want_info": want_text,
                        "pic_url": str((cd.get("picInfo") or {}).get("picUrl") or dp.get("picUrl") or ""),
                        "item_url": f"{GOOFISH_HOME_URL}/item?id={item_id}",
                    }
                    if is_online:
                        online_items.append(item_entry)
                        online_id_set.add(item_id)
                        logger.info(
                            f"   🟢 [在售正常] item_id={item_id} | ¥{price_str} | "
                            f"热度={want_text or '暂无'} | 标题={title_str[:28]}"
                        )
                    else:
                        abnormal_items.append(item_entry)
                        logger.warning(
                            f"   ⚠️ [在售异常/已下架或删除] item_id={item_id} | 状态={status_label} | "
                            f"¥{price_str} | 标题={title_str[:28]}"
                        )

            nick = auth_tracker.get("display_name", "")
            result: Dict[str, Any] = {
                "ok": True,
                "is_offline": False,
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "group_counts": group_summary,
                "online_count": len(online_items),
                "abnormal_count": len(abnormal_items),
                "online_items": online_items,
                "abnormal_items": abnormal_items,
            }

            if expected_item_ids is not None:
                expected_clean = [str(x).strip() for x in expected_item_ids if str(x).strip()]
                online_matched = [x for x in expected_clean if x in online_id_set]
                missing_ids = [x for x in expected_clean if x not in online_id_set]
                result["online_item_ids"] = online_matched
                result["missing_or_downshelf_ids"] = missing_ids
                if missing_ids:
                    logger.warning(
                        f"⚠️ [在售查询/掉架预警] 账号={nick or unb} ({tag}) | "
                        f"发现 {len(missing_ids)}/{len(expected_clean)} 件预期商品不在正常在售列表: {missing_ids}"
                    )

            elapsed = time.monotonic() - t0
            logger.info(
                f"[在售查询/完成] ✅ 账号={nick or unb} ({tag}) | "
                f"正常在售={len(online_items)}件 | 异常残留={len(abnormal_items)}件 | "
                f"分组统计={group_summary} | 总耗时={elapsed:.2f}s"
            )
            return result
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_selling_items")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_selling_items")


# ==============================================================================
# 四、 核心函数：消息监控 (`get_messages`)
# ==============================================================================
def _extract_left_conversations(page) -> List[Dict[str, Any]]:
    """提取 `/im` 页面左侧会话列表的摘要信息（跳过官方「通知消息」）。"""
    raw_list = page.evaluate(
        """() => {
        const items = Array.from(document.querySelectorAll('div[class*="conversation-item--"]'));
        return items.map((el, domIdx) => {
            const badgeEl = el.querySelector('.ant-badge-count, .ant-scroll-number-only-unit.current');
            const badgeText = badgeEl ? badgeEl.innerText.trim() : '';
            const trigger = el.querySelector('.ant-dropdown-trigger') || el;
            const rawLines = trigger.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
            return { domIdx, badgeText, rawLines };
        });
    }"""
    )

    conversations: List[Dict[str, Any]] = []
    for item in raw_list or []:
        badge_text = str(item.get("badgeText") or "").strip()
        unread_count = int(badge_text) if badge_text.isdigit() else 0
        lines = list(item.get("rawLines") or [])

        # 若首行是未读数字角标，先剥离首行角标
        if badge_text and lines and lines[0] == badge_text:
            lines = lines[1:]

        if not lines:
            continue

        user_nick = lines[0]
        if user_nick == "通知消息":
            continue

        last_msg_time = lines[-1] if len(lines) >= 2 else ""
        middle_lines = lines[1:-1] if len(lines) >= 3 else []
        filtered_mid = [
            ln for ln in middle_lines if "30分钟内回复能更快卖出哦" not in ln
        ]
        last_msg_summary = (
            filtered_mid[-1]
            if filtered_mid
            else (middle_lines[-1] if middle_lines else "")
        )

        conversations.append(
            {
                "dom_index": int(item["domIdx"]),
                "user_nick": user_nick,
                "unread_count": unread_count,
                "last_msg_summary": last_msg_summary,
                "last_msg_time": last_msg_time,
            }
        )
    return conversations


def _extract_current_chat_detail(page, fallback_time: str = "") -> Dict[str, Any]:
    """
    提取当前打开的右侧聊天窗口详情：
    1. 提取对方脱敏昵称 `peer_masked_nick` 与唯一用户 ID `peer_user_id`；
    2. 从 DOM 顶部商品栏兜底提取关联商品 `dom_item_id`、`dom_item_price`；
    3. 按视觉从上到下（即时间从早到晚）顺序提取每一条气泡消息，区分 `"peer"`（对方发来）与 `"me"`（我方发出）并关联时间戳。
    """
    detail = page.evaluate(
        """(fallbackTime) => {
        const main = document.querySelector('main[class*="chat-main"]') || document.querySelector('.ant-layout-content');
        if (!main) return { peer_masked_nick: '', peer_user_id: '', dom_item_id: '', dom_item_price: '', messages: [] };

        const topbar = main.querySelector('div[class*="message-topbar--"]');
        const topbarText = topbar ? topbar.innerText : '';
        const maskMatch = topbarText.match(/\\(([^)]+\\*[^)]+)\\)/);
        const peerMaskedNick = maskMatch ? maskMatch[1] : '';

        let peerUserId = '';
        if (topbar) {
            const userLink = topbar.querySelector('a[href*="userId="]');
            if (userLink && userLink.href) {
                const uidMatch = userLink.href.match(/userId=(\\d+)/);
                if (uidMatch) peerUserId = uidMatch[1];
            }
        }

        // DOM 顶部商品卡片兜底提取
        let domItemId = '';
        let domItemPrice = '';
        const headCard = main.querySelector('div[data-spm="head"]') || main;
        const itemLink = headCard.querySelector('a[href*="item?id="]');
        if (itemLink && itemLink.href) {
            const idMatch = itemLink.href.match(/id=(\\d+)/);
            if (idMatch) domItemId = idMatch[1];
            const priceMatch = (itemLink.innerText || '').match(/¥\\s*([0-9]+(?:\\.[0-9]+)?)/);
            if (priceMatch) domItemPrice = priceMatch[1];
        }

        const container = main.querySelector('#msg-list-container') || main.querySelector('div[class*="message-list"]') || main;
        const timeRegex = /^(?:\\d{4}-\\d{2}-\\d{2}\\s+)?\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}$|^(?:(?:昨天|前天|今天)\\s*)?\\d{1,2}:\\d{2}$|^\\d+\\s*(?:分钟|小时)前$/;

        // 1. 收集所有时间分隔文本及其垂直坐标 rect.top
        const timeNodes = [];
        const allEls = Array.from(container.querySelectorAll('div, span, li'));
        for (const el of allEls) {
            if (el.children.length === 0) {
                const txt = (el.innerText || '').trim();
                if (txt && timeRegex.test(txt)) {
                    timeNodes.push({ top: el.getBoundingClientRect().top, time: txt });
                }
            }
        }
        timeNodes.sort((a, b) => a.top - b.top);

        // 2. 收集所有聊天消息行 div[class*="message-row--"] 并按视觉从上到下排序
        const rowEls = Array.from(container.querySelectorAll('div[class*="message-row--"]'));
        const rowsWithTop = rowEls.map(row => ({
            row,
            top: row.getBoundingClientRect().top
        })).sort((a, b) => a.top - b.top);

        const messages = [];
        for (const item of rowsWithTop) {
            const row = item.row;
            const rightEl = row.querySelector('div[class*="message-text-right--"]');
            const leftEl = row.querySelector('div[class*="message-text-left--"]');
            const firstChild = row.firstElementChild;
            const isRowReverse = firstChild && (
                (firstChild.style && firstChild.style.flexDirection === 'row-reverse') ||
                window.getComputedStyle(firstChild).flexDirection === 'row-reverse'
            );

            const sender = (rightEl || isRowReverse) ? 'me' : 'peer';
            let text = '';
            if (rightEl) {
                text = rightEl.innerText.trim();
            } else if (leftEl) {
                text = leftEl.innerText.trim();
            } else {
                const contentEl = row.querySelector('div[class*="message-content--"]');
                if (contentEl) {
                    text = contentEl.innerText.trim();
                }
                if (!text && row.querySelector('img:not([class*="avatar"])')) {
                    text = '[图片]';
                }
            }
            if (!text) continue;

            // 匹配视觉位置在该消息上方最近的时间戳
            let matchedTime = fallbackTime || '';
            for (const tn of timeNodes) {
                if (tn.top <= item.top + 8) {
                    matchedTime = tn.time;
                } else {
                    break;
                }
            }

            messages.push({
                sender,
                text,
                time: matchedTime
            });
        }

        return {
            peer_masked_nick: peerMaskedNick,
            peer_user_id: peerUserId,
            dom_item_id: domItemId,
            dom_item_price: domItemPrice,
            messages
        };
    }""",
        fallback_time,
    )
    return detail or {
        "peer_masked_nick": "",
        "peer_user_id": "",
        "dom_item_id": "",
        "dom_item_price": "",
        "messages": [],
    }


def get_messages(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    *,
    only_unread: bool = False,
    only_my_items: bool = False,
    max_sessions: int = 5,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    纯消息监控函数（不附带任何自动回复动作，职责单一）：
    获取指定账号下：
      1. 谁发了消息 (`user_nick`、`peer_masked_nick`、`peer_user_id`)；
      2. 什么时候发的 (`last_message_time`、`last_peer_time` 以及每条消息的 `time`)；
      3. 发了什么内容 (`last_message_text`、`last_peer_message` 以及完整 `messages` 列表，区分 `"peer"` 对方 与 `"me"` 我方)；
      4. **正在谈论哪一个商品** (`item_info` 及顶层 `item_id`、`item_title`、`item_price`、`item_url`、`is_my_item`)：
         - `item_id`    : 商品唯一 ID（与 `publish_item` / `get_selling_items` 的 `item_id` 完全一致，可直接关联不同商品的专属回复策略）
         - `item_title` : 商品标题/文案首行
         - `item_price` : 商品价格
         - `is_my_item` : `True` 表示该商品是我方上架售卖的商品（对方是买家来咨询我）；`False` 表示是我去咨询别人的商品
    参数说明：
      - `user_data_dir` : 账号对应的独立浏览器目录
      - `only_unread`   : 若为 True，仅点开左侧有未读红点（`unread_count > 0`）的会话
      - `only_my_items` : 若为 True，结果中仅保留咨询我方在售商品（`is_my_item == True`）的买家会话
      - `max_sessions`  : 最多点开读取详情的会话数（默认 5）
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    logger.info(
        f"[消息监控/开始] 账号目录={tag} (<{user_data_dir}>) | "
        f"仅看未读={only_unread} | 仅看我方商品咨询={only_my_items} | 最大检查会话数={max_sessions}"
    )
    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name="get_messages",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
        ) as (context, page, auth_tracker):
            latest_headinfo: Dict[str, Any] = {}

            def on_response(resp):
                if "mtop.idle.trade.pc.message.headinfo" in resp.url:
                    try:
                        data = (resp.json() or {}).get("data") or {}
                        common = data.get("commonData") or {}
                        pre_raw = common.get("itemPreInfo") or "{}"
                        pre_info = json.loads(pre_raw) if isinstance(pre_raw, str) else (pre_raw or {})
                        mid_data = ((data.get("middle") or {}).get("data")) or {}
                        left_data = ((data.get("left") or {}).get("data")) or {}

                        item_id_val = str(common.get("itemId") or pre_info.get("itemId") or "").strip()
                        is_self_raw = str(pre_info.get("isSelf") or "").strip().lower()
                        is_my_item = is_self_raw == "true"

                        latest_headinfo.clear()
                        latest_headinfo.update(
                            {
                                "item_id": item_id_val,
                                "item_title": str(pre_info.get("title") or "").strip(),
                                "item_price": str(
                                    pre_info.get("soldPrice") or mid_data.get("price") or ""
                                ).strip(),
                                "item_post_info": str(
                                    pre_info.get("postInfo") or mid_data.get("subTitle") or ""
                                ).strip(),
                                "item_pic_url": str(left_data.get("picUrl") or "").strip(),
                                "item_url": f"{GOOFISH_HOME_URL}/item?id={item_id_val}" if item_id_val else "",
                                "is_my_item": is_my_item,
                            }
                        )
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(GOOFISH_IM_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            unb = _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="get_messages")
            nick = auth_tracker.get("display_name", "")

            all_convs = _extract_left_conversations(page)
            if only_unread:
                target_convs = [c for c in all_convs if c["unread_count"] > 0][: max(1, int(max_sessions))]
            else:
                target_convs = all_convs[: max(1, int(max_sessions))]

            conv_locator = page.locator('div[class*="conversation-item--"]')
            detailed_sessions: List[Dict[str, Any]] = []

            for idx, conv in enumerate(target_convs, start=1):
                dom_idx = conv["dom_index"]
                if dom_idx >= conv_locator.count():
                    continue

                latest_headinfo.clear()
                conv_locator.nth(dom_idx).click()
                # 等待 headinfo 接口返回（最长等待 2.0s），再留 400ms 让聊天 DOM 渲染完毕
                for _ in range(10):
                    if latest_headinfo:
                        break
                    page.wait_for_timeout(200)
                page.wait_for_timeout(400)

                chat_detail = _extract_current_chat_detail(page, fallback_time=conv["last_msg_time"])
                messages: List[Dict[str, str]] = chat_detail.get("messages") or []

                item_id = latest_headinfo.get("item_id") or chat_detail.get("dom_item_id") or ""
                item_title = latest_headinfo.get("item_title") or ""
                item_price = latest_headinfo.get("item_price") or chat_detail.get("dom_item_price") or ""
                item_post_info = latest_headinfo.get("item_post_info") or ""
                item_url = latest_headinfo.get("item_url") or (
                    f"{GOOFISH_HOME_URL}/item?id={item_id}" if item_id else ""
                )
                is_my_item = latest_headinfo.get("is_my_item")

                if only_my_items and is_my_item is False:
                    continue

                last_sender = messages[-1]["sender"] if messages else ""
                last_msg_text = messages[-1]["text"] if messages else conv["last_msg_summary"]
                last_msg_time = (messages[-1]["time"] if messages else "") or conv["last_msg_time"]

                peer_msgs = [m for m in messages if m.get("sender") == "peer"]
                last_peer_msg = peer_msgs[-1]["text"] if peer_msgs else ""
                last_peer_time = peer_msgs[-1]["time"] if peer_msgs else ""

                need_reply = bool(conv["unread_count"] > 0 or last_sender == "peer")

                item_info = {
                    "item_id": item_id,
                    "item_title": item_title,
                    "item_price": item_price,
                    "item_post_info": item_post_info,
                    "item_url": item_url,
                    "is_my_item": is_my_item,
                }

                session_entry = {
                    "user_nick": conv["user_nick"],
                    "peer_masked_nick": chat_detail.get("peer_masked_nick", ""),
                    "peer_user_id": chat_detail.get("peer_user_id", ""),
                    "unread_count": conv["unread_count"],
                    "need_reply": need_reply,
                    "last_sender": last_sender,
                    "last_message_text": last_msg_text,
                    "last_message_time": last_msg_time,
                    "last_peer_message": last_peer_msg,
                    "last_peer_time": last_peer_time,
                    "item_id": item_id,
                    "item_title": item_title,
                    "item_price": item_price,
                    "item_url": item_url,
                    "is_my_item": is_my_item,
                    "item_info": item_info,
                    "messages": messages,
                }
                detailed_sessions.append(session_entry)

                role_tag = (
                    "买家咨询我方商品"
                    if is_my_item is True
                    else ("我方咨询他人商品" if is_my_item is False else "未知归属商品")
                )
                status_tag = "👉[待回复]" if need_reply else "[已回复]"
                short_last = re.sub(r"\s+", " ", last_msg_text)[:35]
                logger.info(
                    f"   📩 [会话 #{idx}] {status_tag} 对方={conv['user_nick']}"
                    f"({chat_detail.get('peer_masked_nick') or '-'}, uid={chat_detail.get('peer_user_id') or '-'}) | "
                    f"场景=[{role_tag}] | 商品ID={item_id or '无'} (¥{item_price or '-'} | {item_title[:20] or '无标题'}) | "
                    f"未读={conv['unread_count']} | 最新({last_sender or '?'} @ {last_msg_time}): \"{short_last}\""
                )

            need_reply_sessions = [s for s in detailed_sessions if s["need_reply"]]
            my_item_need_reply = [
                s for s in need_reply_sessions if s.get("is_my_item") is not False
            ]
            elapsed = time.monotonic() - t0
            result: Dict[str, Any] = {
                "ok": True,
                "is_offline": False,
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "total_conversations": len(all_convs),
                "checked_sessions_count": len(detailed_sessions),
                "need_reply_count": len(need_reply_sessions),
                "my_item_need_reply_count": len(my_item_need_reply),
                "sessions": detailed_sessions,
            }
            logger.info(
                f"[消息监控/完成] ✅ 账号={nick or unb} ({tag}) | 左侧总会话={len(all_convs)} | "
                f"本次深查={len(detailed_sessions)} | 待回复总数={len(need_reply_sessions)} "
                f"(其中我方商品买家待回复={len(my_item_need_reply)}) | 总耗时={elapsed:.2f}s"
            )
            return result
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_messages")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_messages")
