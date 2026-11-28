# -*- coding: utf-8 -*-
"""闲鱼卖家工作台与 IM 消息自动化能力：手动接管登录、在售查询、极简发布、下架、删除与消息监控。"""

import json
import os
from pathlib import Path
import random
import re
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit
import uuid

import requests

from ..copy_templates import strip_platform_names
from .session import (
    DEFAULT_LOCK_TIMEOUT_SEC,
    DEFAULT_OPERATION_TIMEOUT_SEC,
    DEFAULT_USER_DATA_DIR,
    GOOFISH_HOME_URL,
    GOOFISH_IM_URL,
    GOOFISH_PERSONAL_URL,
    GOOFISH_PUBLISH_URL,
    XianyuLockTimeoutError,
    XianyuOfflineError,
    _acquire_serial_browser_lock,
    _browser_session_guard,
    _build_lock_timeout_result,
    _build_offline_result,
    _collect_page_error_tips,
    _detach_listener_safely,
    _dir_tag,
    _verify_account_online,
    clean_browser_cache,
    launch_persistent_context,
    logger,
    sync_playwright,
)

TITLE_MAX_LEN = 30


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
def _find_zaisou_tab(page: Any) -> Any:
    """精确查找真实可见的「在售」子标签，避免误点商品名中包含「在售」二字的元素。"""
    try:
        loc = page.get_by_text(re.compile(r"^在售(?:\s*[（(]?\d+[）)]?)?$"))
        cnt = loc.count()
        for idx in range(min(cnt, 6)):
            cand = loc.nth(idx)
            try:
                if cand.is_visible():
                    return cand
            except Exception:
                continue
    except Exception:
        pass
    return None


def _extract_personal_baobei_count(page: Any) -> Optional[int]:
    """从闲鱼个人主页顶部标签读取可见的「宝贝 N」计数，用于 0 条结果时的交叉防误杀校验。"""
    try:
        txt = str(page.evaluate("() => (document.body ? document.body.innerText : '')") or "")
    except Exception:
        return None
    m = re.search(r"宝贝\s*[:：\n\r ]*(\d+)", txt)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _get_selling_items_on_page(
    context: Any,
    page: Any,
    auth_tracker: Dict[str, Any],
    *,
    user_data_dir: str,
    expected_item_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    在已就绪的浏览器页面上下文中查询当前闲鱼在售与异常商品列表（严格 Fail-Closed）。
    兼容旧版「在售」子标签与新版个人主页「宝贝」直出列表；若列表接口未返回有效响应或分页中断，
    明确返回 ok=False 报错，绝不把拉取失败误报成 0 件在售！
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    group_summary: Dict[str, int] = {}
    all_packets: List[Dict[str, Any]] = []
    list_api_errors: List[str] = []

    def on_response(resp):
        if "mtop.idle.web.xyh.item.list" not in resp.url:
            return
        try:
            body = resp.json()
        except Exception as exc:
            list_api_errors.append(f"响应解析异常({exc})")
            return
        if not isinstance(body, dict):
            list_api_errors.append("响应体非JSON对象")
            return
        ret_str = " | ".join(str(x) for x in (body.get("ret") or []))
        data = body.get("data") or {}
        if isinstance(data, dict):
            for g in data.get("itemGroupList") or []:
                if isinstance(g, dict) and g.get("groupName"):
                    group_summary[str(g["groupName"])] = int(g.get("itemNumber") or 0)
        if "SUCCESS" in ret_str and isinstance(data, dict) and isinstance(data.get("cardList"), list):
            all_packets.append(data)
        elif ret_str and "FAIL_SYS_TOKEN_EMPTY" not in ret_str:
            list_api_errors.append(ret_str)
        elif not isinstance(data, dict) or not isinstance(data.get("cardList"), list):
            list_api_errors.append(f"缺少cardList(ret={ret_str or 'empty'})")

    page.on("response", on_response)
    pagination_incomplete = False
    active_packets: List[Dict[str, Any]] = []
    dom_baobei_count: Optional[int] = None
    try:
        page.goto(GOOFISH_PERSONAL_URL, wait_until="domcontentloaded")
        for _ in range(18):
            if (
                all_packets
                or list_api_errors
                or auth_tracker.get("expired_rets")
                or auth_tracker.get("risk_rets")
            ):
                break
            page.wait_for_timeout(200)
        page.wait_for_timeout(500)

        unb = _verify_account_online(
            context, page, auth_tracker, user_data_dir, task_name="get_selling_items"
        )

        zaisou_tab = _find_zaisou_tab(page)
        if zaisou_tab is not None:
            # 记录点击「在售」前已到达的数据包；只有点击后确实触发新列表响应时才切换为筛选后包
            pre_click_len = len(all_packets)
            pre_click_err = len(list_api_errors)
            zaisou_tab.click()
            for _ in range(15):
                if (
                    len(all_packets) > pre_click_len
                    or len(list_api_errors) > pre_click_err
                    or auth_tracker.get("expired_rets")
                    or auth_tracker.get("risk_rets")
                ):
                    break
                page.wait_for_timeout(200)
            page.wait_for_timeout(300)
            active_packets = (
                list(all_packets[pre_click_len:])
                if len(all_packets) > pre_click_len
                else list(all_packets)
            )
        else:
            active_packets = list(all_packets)

        # 分页滚动加载：若 nextPage=True 但后续页未拉取成功，标记不完整并中止同步防漏掉在售商品
        while active_packets and active_packets[-1].get("nextPage") is True:
            prev_total_len = len(all_packets)
            prev_err_len = len(list_api_errors)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            for step_i in range(20):
                if (
                    len(all_packets) > prev_total_len
                    or len(list_api_errors) > prev_err_len
                    or auth_tracker.get("expired_rets")
                    or auth_tracker.get("risk_rets")
                ):
                    break
                if step_i == 8:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(250)
            if len(all_packets) > prev_total_len:
                active_packets.extend(all_packets[prev_total_len:])
            else:
                pagination_incomplete = True
                break

        _verify_account_online(
            context, page, auth_tracker, user_data_dir, task_name="get_selling_items"
        )
        dom_baobei_count = _extract_personal_baobei_count(page)
    finally:
        _detach_listener_safely(page, "response", on_response)

    nick = auth_tracker.get("display_name", "")

    if not active_packets:
        err_code = "SYNC_API_ERROR" if list_api_errors else "SYNC_EMPTY_RESPONSE"
        detail_hint = list_api_errors[-1] if list_api_errors else "页面未返回 mtop.idle.web.xyh.item.list 有效数据"
        msg = (
            f"未拉取到闲鱼在售商品列表（{detail_hint}），已保留原有在售快照不覆盖，请稍后重试"
        )
        logger.warning(f"⚠️ [在售查询/Fail-Closed] 账号={nick or unb} ({tag}) | {msg} | URL={page.url}")
        return {
            "ok": False,
            "is_offline": False,
            "error_code": err_code,
            "user_data_dir": user_data_dir,
            "unb": unb,
            "nick": nick,
            "message": msg,
        }

    if pagination_incomplete:
        err_detail = f"（{list_api_errors[-1]}）" if list_api_errors else ""
        msg = f"闲鱼在售商品分页拉取未完成{err_detail}，为防止漏掉商品已中止本次同步"
        logger.warning(f"⚠️ [在售查询/分页中断] 账号={nick or unb} ({tag}) | {msg}")
        return {
            "ok": False,
            "is_offline": False,
            "error_code": "SYNC_INCOMPLETE_PAGINATION",
            "user_data_dir": user_data_dir,
            "unb": unb,
            "nick": nick,
            "message": msg,
        }

    online_items: List[Dict[str, Any]] = []
    abnormal_items: List[Dict[str, Any]] = []
    seen_ids = set()
    online_id_set = set()

    for pkt in active_packets:
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

    if len(online_items) + len(abnormal_items) == 0:
        expected_hint = max(int(group_summary.get("在售") or 0), int(dom_baobei_count or 0))
        if expected_hint > 0:
            msg = f"闲鱼页面显示共有 {expected_hint} 件商品但解析结果为 0 条，已中止同步以防误判在售为0件"
            logger.warning(f"⚠️ [在售查询/数量矛盾] 账号={nick or unb} ({tag}) | {msg}")
            return {
                "ok": False,
                "is_offline": False,
                "error_code": "SYNC_MISMATCH_ERROR",
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "message": msg,
            }

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
            return _get_selling_items_on_page(
                context,
                page,
                auth_tracker,
                user_data_dir=user_data_dir,
                expected_item_ids=expected_item_ids,
            )
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_selling_items")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_selling_items")


# ==============================================================================
# 三-B、 帮拍文案净水、图片防撞准备、极简上架 (`publish_item`) 与下架 (`downshelf_item`)
# ==============================================================================
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_SHORTLINK_RE = re.compile(
    r"(?:u\.jd\.com|3\.cn|m\.tb\.cn|s\.click\.taobao\.com|yangkeduo\.com|pinduoduo\.com)/[A-Za-z0-9_/?=&.-]+",
    re.IGNORECASE,
)
_TAOKOULING_RE = re.compile(
    r"[￥¥$€£₴¢][0-9A-Za-z]{8,14}[￥¥$€£₴¢]|[\d]{1,2}\.[\d]+\s*[A-Za-z]{2,4}:/[A-Za-z0-9/]+"
)
# 闲鱼 PC 发布页规则明确禁止正文包含 Emoji；剔除补充平面图标、杂项符号与变体选择符，保留汉字/ASCII/常规标点/换行
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002300-\U000023FF"
    "\U00002600-\U000027BF"
    "\u2B00-\u2BFF"
    "\uFE0E\uFE0F\u200D\u20E3"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    """移除闲鱼网页发布框禁止的 Emoji 与装饰符号，完整保留汉字、标点与换行。"""
    return _EMOJI_RE.sub("", str(text or ""))

_EXTREME_WORD_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    ("全网最低价", "高性价比"),
    ("全网最低", "诚意好价"),
    ("全网底价", "超值好价"),
    ("史上最低", "超值特惠"),
    ("历史最低", "超值好价"),
    ("国家级", "高品质"),
    ("第一品牌", "口碑品牌"),
    ("最便宜", "实惠"),
    ("顶级", "优质"),
    ("极品", "精品"),
    ("绝对正品", "正品保障"),
    ("100%正品", "正品保障"),
    ("假一赔十", "支持验货"),
    ("假一罚十", "支持验货"),
    ("专柜正品", "正品好物"),
)

_PROHIBITED_KEYWORDS: Tuple[str, ...] = (
    "加微信",
    "加v",
    "加V",
    "+v",
    "+V",
    "微信号",
    "wx号",
    "VX号",
    "绿泡泡",
    "私聊转账",
    "站外交易",
    "先款后货",
    "刷单",
    "套现",
    "高仿",
    "精仿",
    "A货",
)


_REPLY_EXTRA_FORBIDDEN_KEYWORDS: Tuple[str, ...] = (
    "微信",
    "扣扣",
    "二维码",
    "手机号",
    "淘宝",
    "天猫",
    "拼多多",
    "京东",
    "<think>",
    "</think>",
)
_REPLY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:vx|wx|qq|pdd|jd)(?![A-Za-z0-9_])", re.IGNORECASE)


def validate_reply_text(text: Any) -> Tuple[bool, str]:
    """发前合规门禁：拦截空消息、站外导流/竞品词、外链、淘口令与模型思考标记。"""
    clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return False, "回复内容不能为空"
    if len(clean) > 500:
        return False, "单条回复不能超过 500 字"
    if _URL_RE.search(clean) or _SHORTLINK_RE.search(clean) or _TAOKOULING_RE.search(clean):
        return False, "回复中禁止包含外部链接、短链或淘口令"
    lowered = clean.lower()
    for kw in _PROHIBITED_KEYWORDS + _REPLY_EXTRA_FORBIDDEN_KEYWORDS:
        if kw.lower() in lowered:
            return False, f"回复触发闲鱼合规拦截词：【{kw}】"
    tok_m = _REPLY_TOKEN_RE.search(clean)
    if tok_m:
        return False, f"回复触发闲鱼合规拦截词：【{tok_m.group(0)}】"
    return True, clean


def sanitize_content(title: str, desc: str) -> Tuple[str, str, List[str]]:
    """剥离推广短链、淘口令、第三方平台词与 Emoji，替换极限广告词，拦截外联违禁词，并保留段落空行。"""
    raw_title = _TAOKOULING_RE.sub("", _SHORTLINK_RE.sub("", _URL_RE.sub("", str(title or ""))))
    raw_desc = _TAOKOULING_RE.sub("", _SHORTLINK_RE.sub("", _URL_RE.sub("", str(desc or ""))))
    no_emoji_title = strip_emojis(raw_title)
    no_emoji_desc = strip_emojis(raw_desc)
    clean_title = strip_platform_names(no_emoji_title) or no_emoji_title
    clean_desc = strip_platform_names(no_emoji_desc)

    replaced: List[str] = []
    if no_emoji_title != raw_title or no_emoji_desc != raw_desc:
        replaced.append("移除Emoji")
    if clean_title != no_emoji_title or clean_desc != no_emoji_desc:
        replaced.append("剥离平台词")

    for bad, good in _EXTREME_WORD_REPLACEMENTS:
        if bad in clean_title or bad in clean_desc:
            clean_title = clean_title.replace(bad, good)
            clean_desc = clean_desc.replace(bad, good)
            replaced.append(f"{bad}->{good}")

    clean_title = re.sub(r"\s+", " ", clean_title).strip()[:TITLE_MAX_LEN].rstrip()
    raw_lines = clean_desc.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    norm_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_lines]
    clean_desc = re.sub(r"\n{3,}", "\n\n", "\n".join(norm_lines)).strip()

    combined = f"{clean_title}\n{clean_desc}"
    for kw in _PROHIBITED_KEYWORDS:
        if kw.lower() in combined.lower():
            raise ValueError(f"文案触发闲鱼违禁/外联拦截词: 【{kw}】")

    if not clean_title:
        raise ValueError("商品标题不能为空")
    return clean_title, clean_desc, replaced


def format_publish_text(clean_title: str, clean_desc: str) -> str:
    """将清洗后的标题与正文拼装为闲鱼单文本框内容（标题与正文间保留空行）。"""
    title_str = str(clean_title or "").strip()
    desc_str = str(clean_desc or "").strip()
    if not desc_str or desc_str == title_str:
        return title_str
    if not title_str:
        return desc_str
    return f"{title_str}\n\n{desc_str}"


def _prepare_local_image_files(images: List[str], temp_dir: str) -> List[str]:
    """将本地图片或 http/https URL 准备为临时文件，并对 JPEG 注入注释微扰防撞图。"""
    local_files: List[str] = []
    for idx, src in enumerate(images[:9], start=1):
        src_str = str(src or "").strip()
        if not src_str:
            continue
        if src_str.lower().startswith(("http://", "https://")):
            resp = requests.get(src_str, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            resp.raise_for_status()
            raw_bytes = resp.content
            ext = os.path.splitext(urlsplit(src_str).path)[1].lower() or ".jpg"
        else:
            p = Path(src_str).expanduser().resolve()
            if not p.is_file():
                raise FileNotFoundError(f"本地图片不存在: {src_str}")
            raw_bytes = p.read_bytes()
            ext = p.suffix.lower() or ".jpg"

        if raw_bytes.startswith(b"\xff\xd8\xff") and raw_bytes.endswith(b"\xff\xd9"):
            ext = ".jpg"
            seed = f"xy_{int(time.time() * 1000000)}_{idx}_{random.randint(100, 999)}".encode("ascii")
            length = len(seed) + 2
            raw_bytes = raw_bytes[:-2] + b"\xff\xfe" + length.to_bytes(2, "big") + seed + b"\xff\xd9"
        elif raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"

        out_path = os.path.join(temp_dir, f"xianyu_img_{idx}{ext}")
        with open(out_path, "wb") as f:
            f.write(raw_bytes)
        local_files.append(out_path)

    if not local_files:
        raise ValueError("至少需要提供 1 张有效图片")
    return local_files


_MAX_PUBLISH_FAILURE_SCREENSHOTS = 50
_PUBLISH_FAIL_NAME_RE = re.compile(r"^pubfail_\d+_[0-9a-f]{8}\.jpg$")


def _prune_matching_files(out_dir: Path, pattern: re.Pattern[str], max_keep: int) -> None:
    files = sorted(
        (p for p in out_dir.iterdir() if p.is_file() and pattern.fullmatch(p.name)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in files[max_keep:]:
        stale.unlink(missing_ok=True)


def _capture_publish_failure_screenshot(page: Any, failure_screenshot_dir: Optional[str]) -> str:
    """在上架失败且浏览器页面仍打开时原子保存现场压缩截图，并有界保留最近 N 张。"""
    if not failure_screenshot_dir or page is None:
        return ""
    tmp_path: Optional[Path] = None
    try:
        out_dir = Path(failure_screenshot_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"pubfail_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
        target_path = out_dir / fname
        tmp_path = out_dir / f".tmp_{fname}"
        raw = page.screenshot(type="jpeg", quality=80)
        if not raw:
            return ""
        tmp_path.write_bytes(raw)
        os.replace(tmp_path, target_path)
        tmp_path = None
        _prune_matching_files(out_dir, _PUBLISH_FAIL_NAME_RE, _MAX_PUBLISH_FAILURE_SCREENSHOTS)
        return fname
    except Exception as exc:
        logger.debug(f"[商品上架/失败截图跳过] {exc}")
        return ""
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _wait_for_images_uploaded(page: Any, expected_count: int, max_wait_sec: float = 25.0) -> Optional[str]:
    """
    轮询闲鱼发布页真实图片 DOM（`preview-container--` + 协议相对 `//img.alicdn.com/...`）与上传中状态；
    若页面提示图片含二维码/超限或上传失败占位图，立即返回错误文案避免空等；兼容精简 Mock 页面。
    """
    if expected_count <= 0:
        return None
    deadline = time.monotonic() + max(2.0, float(max_wait_sec))
    while time.monotonic() < deadline:
        try:
            status = page.evaluate(
                """() => {
                    const excludeMarkers = [
                        'O1CN01EnTSix1cJEGq3h5LS',
                        'O1CN014eAYAJ1onFweWYWse',
                        'O1CN01CYtPWu1MUBqQAUK9D',
                        'O1CN01J8BeIR1JDIlwdKkqZ',
                        'O1CN016XczIR1iimNeEcZoh'
                    ];
                    const candidates = Array.from(document.querySelectorAll(
                        'div[class*="preview-container--"] img, div[class*="imgItem--"] img, .ant-upload-list-item img, [class*="image-list"] img'
                    ));
                    let uploaded = 0;
                    let errorImg = 0;
                    for (const img of candidates) {
                        const s = img.getAttribute('src') || '';
                        const st = window.getComputedStyle(img);
                        const vis = !st || st.display !== 'none';
                        if (!vis) continue;
                        if (s.includes('O1CN01J8BeIR1JDIlwdKkqZ')) {
                            errorImg += 1;
                            continue;
                        }
                        if (excludeMarkers.some((m) => s.includes(m))) continue;
                        if (s.startsWith('http') || s.startsWith('//') || s.startsWith('blob:') || s.startsWith('data:')) {
                            uploaded += 1;
                        }
                    }
                    const spinners = Array.from(document.querySelectorAll(
                        '.ant-upload-list-item-uploading, .ant-spin-spinning, [class*="uploading"], img[src*="O1CN01EnTSix1cJEGq3h5LS"]'
                    )).filter((el) => {
                        const st = window.getComputedStyle(el);
                        return st && st.display !== 'none' && st.visibility !== 'hidden';
                    }).length;
                    const errMsgs = Array.from(document.querySelectorAll('.ant-message-notice-content'))
                        .filter((el) => el.offsetParent !== null)
                        .map((el) => (el.innerText || '').trim())
                        .filter((t) => t.includes('二维码') || t.includes('图片') || t.includes('上传失败'));
                    return { uploading: spinners, uploaded, error_img: errorImg, error_msg: errMsgs[0] || '' };
                }"""
            )
        except Exception:
            page.wait_for_timeout(500)
            return None
        if not isinstance(status, dict):
            page.wait_for_timeout(500)
            return None
        if status.get("error_msg"):
            return str(status["error_msg"])
        if int(status.get("error_img") or 0) > 0 and int(status.get("uploading") or 0) == 0:
            return "存在上传失败的图片，请更换图片后重试"
        if int(status.get("uploaded") or 0) >= expected_count and int(status.get("uploading") or 0) == 0:
            page.wait_for_timeout(300)
            return None
        page.wait_for_timeout(350)
    logger.warning(f"[商品上架/传图等待超时] 期望图片={expected_count}张，已等待 {max_wait_sec:.0f}s，继续尝试填写")
    return None


def _extract_category_state(page: Any, recommend_resps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """解析类目预测卡片与「网页版暂不支持发布此分类」提示（对齐 `p_publish-index.js` `haveSpecialCard`）。"""
    category_name = ""
    required_fields: List[str] = []
    restricted_by_cards = False

    if recommend_resps:
        last_rec = recommend_resps[-1] if isinstance(recommend_resps[-1], dict) else {}
        data = last_rec.get("data") if isinstance(last_rec.get("data"), dict) else {}
        cards = data.get("cardList") if isinstance(data.get("cardList"), list) else []
        for card in cards:
            if not isinstance(card, dict):
                continue
            ctype = str(card.get("cardType") or "")
            cd = card.get("cardData") if isinstance(card.get("cardData"), dict) else {}
            pname = str(cd.get("propertyName") or "").strip()
            is_book_raw = str(cd.get("isBook") or "").strip().lower()
            is_book = is_book_raw == "true" or cd.get("isBook") is True
            if ctype and (ctype != "20401" or is_book):
                restricted_by_cards = True
                if pname and pname not in required_fields:
                    required_fields.append(pname)
            if str(cd.get("propertyId") or "") == "-10000":
                for val in (cd.get("valuesList") or []):
                    if isinstance(val, dict) and str(val.get("isClicked") or "") == "1":
                        category_name = str(val.get("catName") or "").strip()
                        break

    tip_text = ""
    try:
        dom_tip = page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('div[class*="tips--"]')) {
                    const st = window.getComputedStyle(el);
                    if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
                    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (t) return t;
                }
                return '';
            }"""
        )
        tip_text = str(dom_tip or "").strip() if isinstance(dom_tip, str) else ""
    except Exception:
        tip_text = ""

    restricted = restricted_by_cards or ("暂不支持发布此分类" in tip_text) or ("APP扫码继续发布" in tip_text)
    return {
        "restricted": restricted,
        "category_name": category_name,
        "required_fields": required_fields,
        "tip_text": tip_text,
    }


def _switch_category_to_other_idle(page: Any, recommend_resps: List[Dict[str, Any]]) -> bool:
    """当网页端触发资质类目限制时，自动将 `分类*` 下拉框切换为兜底综合类目（优先「其他闲置」，兼容「其他服务」）并确认提示消除。"""
    dd_prefix = ".ant-select-dropdown:not(.ant-select-dropdown-hidden)"
    option_selectors = (
        f'{dd_prefix} .ant-select-item-option[title="其他闲置"]',
        f'{dd_prefix} .ant-select-item-option[title="其他服务"]',
        f'{dd_prefix} .ant-select-item-option:has-text("其他闲置")',
        f'{dd_prefix} .ant-select-item-option:has-text("其他")',
    )
    for attempt in range(2):
        try:
            sel = page.locator('.ant-form-item:has-text("分类") .ant-select').first
            if sel.count() == 0:
                return False
            sel.click()
            page.wait_for_timeout(350)

            page.evaluate(
                """() => {
                    const holder = document.querySelector(
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .rc-virtual-list-holder'
                    );
                    if (holder) holder.scrollTop = holder.scrollHeight;
                }"""
            )
            page.wait_for_timeout(300)

            picked = None
            for opt_sel in option_selectors:
                cand = page.locator(opt_sel).first
                if cand.count() > 0:
                    picked = cand
                    break
            if picked is None:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                page.wait_for_timeout(300)
                continue

            prev_len = len(recommend_resps)
            picked.click()
            for _ in range(15):
                if len(recommend_resps) > prev_len:
                    break
                page.wait_for_timeout(200)
            page.wait_for_timeout(450)
            state = _extract_category_state(page, recommend_resps)
            if not state["restricted"]:
                return True
        except Exception as exc:
            logger.warning(f"[商品上架/自动切换兜底类目第{attempt + 1}次重试] {exc}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(300)
    return False


def _fill_contenteditable_text(page: Any, editor: Any, text: str) -> None:
    """按真实段落换行向 contenteditable 富文本写入文案，避免 `.fill()` 吞掉换行/空行。"""
    editor.click()
    editor.fill("")
    keyboard = getattr(page, "keyboard", None)
    if (
        keyboard is not None
        and callable(getattr(keyboard, "insert_text", None))
        and callable(getattr(keyboard, "press", None))
    ):
        lines = str(text or "").split("\n")
        for idx, line in enumerate(lines):
            if line:
                keyboard.insert_text(line)
            if idx < len(lines) - 1:
                keyboard.press("Enter")
    else:
        editor.fill(text)


def _disable_coin_deduction_if_present(page: Any) -> Dict[str, Any]:
    """检查并关闭发布页可能默认勾选的「闲鱼币抵扣/优推」开关（防成交强扣货款折币）。"""
    script = """(doClick) => {
        const nodes = Array.from(document.querySelectorAll('.ant-form-item, label, .ant-checkbox-wrapper, div[class*="switch"], div[class*="coin"]'));
        for (const el of nodes) {
            const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ');
            if (!txt || txt.length > 120 || !/(闲鱼币|优推|抵扣)/.test(txt)) continue;
            const sw = el.querySelector('.ant-switch, input[type="checkbox"], [role="switch"]');
            if (!sw) continue;
            const style = window.getComputedStyle(sw);
            if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
            const aria = sw.getAttribute('aria-checked');
            const isChecked = aria === 'true' || Boolean(sw.checked) || sw.classList.contains('ant-switch-checked');
            if (isChecked) {
                if (doClick) sw.click();
                return { found: true, checked: true, label: txt.slice(0, 40) };
            }
            return { found: true, checked: false, label: txt.slice(0, 40) };
        }
        return { found: false, checked: false, label: '' };
    }"""
    try:
        info = page.evaluate(script, False)
    except Exception:
        return {"found": False, "unchecked": False, "failed": False}
    if not isinstance(info, dict):
        return {"found": False, "unchecked": False, "failed": False}
    if not (info.get("found") and info.get("checked")):
        return {"found": bool(info.get("found")), "unchecked": False, "failed": False}
    try:
        page.evaluate(script, True)
        page.wait_for_timeout(250)
        recheck = page.evaluate(script, False)
        if isinstance(recheck, dict) and recheck.get("checked"):
            return {"found": True, "unchecked": False, "failed": True, "label": info.get("label", "")}
        return {"found": True, "unchecked": True, "failed": False, "label": info.get("label", "")}
    except Exception:
        return {"found": True, "unchecked": False, "failed": True, "label": info.get("label", "")}


def _publish_item_on_page(
    context: Any,
    page: Any,
    auth_tracker: Dict[str, Any],
    *,
    clean_title: str,
    clean_desc: str,
    local_files: List[str],
    price: float,
    original_price: Optional[float] = None,
    delivery: str = "包邮",
    user_data_dir: str = "",
    failure_screenshot_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """在已就绪的浏览器页面上下文中执行单件商品发布（自动切换网页受限类目至「其他闲置」）。"""
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    full_text = format_publish_text(clean_title, clean_desc)
    publish_resp_data: Dict[str, Any] = {}
    recommend_resps: List[Dict[str, Any]] = []
    badwords_resps: List[Dict[str, Any]] = []

    def on_response(resp):
        url = getattr(resp, "url", "") or ""
        try:
            if "mtop.idle.pc.idleitem.publish" in url:
                publish_resp_data.update(resp.json())
            elif "mtop.taobao.idle.kgraph.property.recommend" in url:
                recommend_resps.append(resp.json())
            elif "mtop.taobao.idleitem.badwords.prepubcheck" in url:
                badwords_resps.append(resp.json())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(GOOFISH_PUBLISH_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        unb = _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="publish_item")
        nick = auth_tracker.get("display_name", "")

        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(local_files)
        upload_err = _wait_for_images_uploaded(page, len(local_files))
        if upload_err:
            shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
            return {
                "ok": False,
                "is_offline": False,
                "action": "publish",
                "error_code": "IMAGE_UPLOAD_REJECTED",
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "screenshot_file": shot_file,
                "message": f"图片校验未通过：{upload_err}",
            }

        recs_before_text = len(recommend_resps)
        editor = page.locator('[contenteditable="true"]').first
        _fill_contenteditable_text(page, editor, full_text)

        price_input = page.locator('.ant-form-item:has-text("价格") input[placeholder="0.00"]').first
        if price_input.count() == 0:
            price_input = page.locator('input[placeholder="0.00"]').first
        price_input.click()
        price_input.fill(f"{float(price):.2f}")
        page.wait_for_timeout(300)

        if original_price and original_price > 0:
            orig_input = page.locator('.ant-form-item:has-text("原价") input[placeholder="0.00"]').first
            if orig_input.count() > 0:
                orig_input.click()
                orig_input.fill(f"{float(original_price):.2f}")
                page.wait_for_timeout(250)

        if delivery and delivery != "包邮":
            radio = page.locator(f'label.ant-radio-wrapper:has-text("{delivery}")').first
            if radio.count() > 0:
                radio.click()
                page.wait_for_timeout(250)

        # 等待写完文案后的类目识别（跳过仅传图触发的旧响应）与违禁词预检返回
        for _ in range(14):
            if len(recommend_resps) > recs_before_text:
                break
            page.wait_for_timeout(250)
        page.wait_for_timeout(400)

        if badwords_resps:
            bw_data = badwords_resps[-1].get("data") if isinstance(badwords_resps[-1], dict) else {}
            if isinstance(bw_data, dict) and bw_data.get("forbidPublish") is True:
                words: List[str] = []
                for it in ((bw_data.get("badWordAlertInfo") or {}).get("items") or []):
                    if isinstance(it, dict):
                        words.extend(str(w) for w in (it.get("words") or []) if w)
                shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
                bw_reason = str(bw_data.get("msg") or "文案触发闲鱼违禁词校验").strip()
                words_hint = f"（敏感词：{', '.join(words[:5])}）" if words else ""
                return {
                    "ok": False,
                    "is_offline": False,
                    "action": "publish",
                    "error_code": "FORBIDDEN_WORDS",
                    "user_data_dir": user_data_dir,
                    "unb": unb,
                    "nick": nick,
                    "screenshot_file": shot_file,
                    "message": f"文案拦截：{bw_reason}{words_hint}",
                }

        cat_state = _extract_category_state(page, recommend_resps)
        if cat_state["restricted"]:
            switched = _switch_category_to_other_idle(page, recommend_resps)
            if switched:
                logger.info(
                    f"[商品上架/自动切类目] 账号={nick or unb} | 受限分类=【{cat_state['category_name'] or '受限分类'}】"
                    " -> 已自动切换为「其他闲置」直接发布"
                )
            else:
                shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
                cat_label = cat_state["category_name"] or "当前分类"
                return {
                    "ok": False,
                    "is_offline": False,
                    "action": "publish",
                    "error_code": "CATEGORY_SWITCH_FAILED",
                    "user_data_dir": user_data_dir,
                    "unb": unb,
                    "nick": nick,
                    "screenshot_file": shot_file,
                    "message": f"分类「{cat_label}」网页端受限，自动切换「其他闲置」未成功，请稍后重试",
                }

        coin_guard = _disable_coin_deduction_if_present(page)
        if coin_guard.get("failed"):
            shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
            return {
                "ok": False,
                "is_offline": False,
                "action": "publish",
                "error_code": "COIN_DEDUCTION_UNCHECK_FAILED",
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "screenshot_file": shot_file,
                "message": "检测到「闲鱼币抵扣/优推」处于开启状态且自动关闭未生效，为防强扣货款已中止发布",
            }

        pub_btn = page.locator('button:has-text("发布")').first
        pub_btn.click()

        for _ in range(30):
            if publish_resp_data or "/item?id=" in page.url or auth_tracker.get("expired_rets"):
                break
            page.wait_for_timeout(500)

        _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="publish_item")

        ret_list = publish_resp_data.get("ret") or []
        ret_str = " | ".join(str(x) for x in ret_list)
        resp_data = publish_resp_data.get("data") or {}
        item_id = str(
            resp_data.get("itemId")
            or (resp_data.get("itemDO") or {}).get("itemId")
            or ""
        ).strip()

        if not item_id and "id=" in page.url:
            m = re.search(r"id=(\d+)", page.url)
            if m:
                item_id = m.group(1)

        if not item_id:
            page_tips = _collect_page_error_tips(page)
            shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
            logger.error(
                f"❌ [商品上架/失败诊断] 账号={nick or unb} ({tag}) | 标题=【{clean_title}】 | "
                f"MTOP响应={publish_resp_data} | 页面提示=【{page_tips or '无'}】 | 截图={shot_file or '无'} | URL={page.url}"
            )
            return {
                "ok": False,
                "is_offline": False,
                "action": "publish",
                "error_code": "PUBLISH_FAILED",
                "user_data_dir": user_data_dir,
                "screenshot_file": shot_file,
                "message": f"发布未成功：{page_tips or ret_str or '未返回商品ID'}",
            }

        elapsed = time.monotonic() - t0
        result = {
            "ok": True,
            "is_offline": False,
            "action": "publish",
            "user_data_dir": user_data_dir,
            "unb": unb,
            "nick": nick,
            "item_id": item_id,
            "item_url": f"{GOOFISH_HOME_URL}/item?id={item_id}",
            "title": clean_title,
            "price": f"{float(price):.2f}",
            "ret": ret_str or "SUCCESS::调用成功",
        }
        logger.info(
            f"[商品上架/成功] 🎉 账号={nick or unb} ({tag}) | item_id={item_id} | "
            f"售价=¥{float(price):.2f} | 总耗时={elapsed:.2f}s | 链接={result['item_url']}"
        )
        return result
    except XianyuOfflineError as e:
        shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
        res = _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="publish")
        res["screenshot_file"] = shot_file
        return res
    except Exception as exc:
        shot_file = _capture_publish_failure_screenshot(page, failure_screenshot_dir)
        page_tips = _collect_page_error_tips(page)
        logger.error(
            f"❌ [商品上架/页面异常] 账号目录={tag} | 标题=【{clean_title}】 | "
            f"错误={exc} | 页面提示=【{page_tips or '无'}】 | 截图={shot_file or '无'}"
        )
        return {
            "ok": False,
            "is_offline": False,
            "action": "publish",
            "error_code": "PUBLISH_ERROR",
            "user_data_dir": user_data_dir,
            "screenshot_file": shot_file,
            "message": f"发布过程异常：{page_tips or exc}",
        }
    finally:
        _detach_listener_safely(page, "response", on_response)


def publish_item(
    title: str,
    desc: str,
    images: List[str],
    price: float,
    *,
    original_price: Optional[float] = None,
    delivery: str = "包邮",
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
    lock_scope: str = "global",
    failure_screenshot_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """极简发布商品到闲鱼（图片 + 文案 + 价格，受限类目自动切「其他闲置」）并返回 `item_id` 与 `item_url`。"""
    tag = _dir_tag(user_data_dir)
    if float(price or 0) <= 0:
        raise ValueError("商品价格必须大于 0")
    clean_title, clean_desc, replaced = sanitize_content(title, desc or title)

    temp_dir = tempfile.mkdtemp(prefix="xianyu_pub_")
    try:
        local_files = _prepare_local_image_files(images, temp_dir)
        logger.info(
            f"[商品上架/1-准备] 账号目录={tag} | 标题=【{clean_title}】 | 售价=¥{price:.2f} | "
            f"图片数={len(local_files)}张 | 清洗={replaced or '无'}"
        )

        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name="publish_item",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
            lock_scope=lock_scope,
        ) as (context, page, auth_tracker):
            res = _publish_item_on_page(
                context,
                page,
                auth_tracker,
                clean_title=clean_title,
                clean_desc=clean_desc,
                local_files=local_files,
                price=price,
                original_price=original_price,
                delivery=delivery,
                user_data_dir=user_data_dir,
                failure_screenshot_dir=failure_screenshot_dir,
            )
            if res.get("ok"):
                res["replaced_words"] = replaced
            return res
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="publish")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="publish")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _downshelf_item_on_page(
    context: Any,
    page: Any,
    auth_tracker: Dict[str, Any],
    *,
    item_id: str,
    delete: bool = False,
    user_data_dir: str = "",
) -> Dict[str, Any]:
    """在已就绪的浏览器页面上下文中下架或删除指定商品。"""
    t0 = time.monotonic()
    target_id = str(item_id or "").strip()
    action_name = "delete" if delete else "downshelf"
    action_label = "删除" if delete else "下架"
    target_api = "com.taobao.idle.item.delete" if delete else "mtop.taobao.idle.item.downshelf"
    item_url = f"{GOOFISH_HOME_URL}/item?id={target_id}"
    action_resp: Dict[str, Any] = {}

    def on_response(resp):
        if target_api in resp.url:
            try:
                action_resp.update(resp.json())
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(item_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        unb = _verify_account_online(
            context, page, auth_tracker, user_data_dir, task_name=f"{action_name}_item"
        )
        nick = auth_tracker.get("display_name", "")

        btn = page.locator('div[class*="sellerButton"]:not([class*="Group"])').filter(has_text=action_label).first
        if btn.count() == 0:
            btn = page.get_by_text(action_label, exact=True).first
        if btn.count() == 0:
            page_tips = _collect_page_error_tips(page)
            return {
                "ok": False,
                "is_offline": False,
                "action": action_name,
                "error_code": "BUTTON_NOT_FOUND",
                "user_data_dir": user_data_dir,
                "item_id": target_id,
                "message": f"商品页未找到「{action_label}」按钮（{page_tips or '可能已下架或不属于当前账号'}）",
            }

        btn.click()
        page.wait_for_timeout(800)

        confirm_btn = page.locator(".ant-modal-content").get_by_text("确定", exact=True).last
        confirm_btn.click()

        for _ in range(20):
            if action_resp or auth_tracker.get("expired_rets"):
                break
            page.wait_for_timeout(400)

        _verify_account_online(
            context, page, auth_tracker, user_data_dir, task_name=f"{action_name}_item"
        )

        ret_list = action_resp.get("ret") or []
        ret_str = " | ".join(str(x) for x in ret_list)
        ok = "SUCCESS" in ret_str
        elapsed = time.monotonic() - t0
        logger.info(
            f"[商品{action_label}/结束] ok={ok} | item_id={target_id} | MTOP={ret_str} | 耗时={elapsed:.2f}s"
        )

        return {
            "ok": ok,
            "is_offline": False,
            "action": action_name,
            "error_code": "" if ok else "DOWNSHELF_FAILED",
            "user_data_dir": user_data_dir,
            "unb": unb,
            "nick": nick,
            "item_id": target_id,
            "ret": ret_str,
            "message": f"{action_label}成功" if ok else f"{action_label}失败：{ret_str or '接口未返回成功'}",
        }
    finally:
        _detach_listener_safely(page, "response", on_response)


def downshelf_item(
    item_id: str,
    *,
    delete: bool = False,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
    lock_scope: str = "global",
) -> Dict[str, Any]:
    """对指定 `item_id` 的商品执行下架（或删除）。"""
    tag = _dir_tag(user_data_dir)
    target_id = str(item_id or "").strip()
    if not target_id:
        raise ValueError("item_id 不能为空")

    action_name = "delete" if delete else "downshelf"
    action_label = "删除" if delete else "下架"
    item_url = f"{GOOFISH_HOME_URL}/item?id={target_id}"
    logger.info(f"[商品{action_label}/开始] 账号目录={tag} | item_id={target_id} | URL={item_url}")

    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name=f"{action_name}_item({target_id})",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
            lock_scope=lock_scope,
        ) as (context, page, auth_tracker):
            return _downshelf_item_on_page(
                context,
                page,
                auth_tracker,
                item_id=target_id,
                delete=delete,
                user_data_dir=user_data_dir,
            )
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action=action_name)
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action=action_name)


class XianyuSellerSession:
    """
    批处理卖家单会话复用器：
    在同一批量同步任务中惰性打开并复用一个 Chrome 上下文与串行锁，避免每件商品冷启一次浏览器。
    """

    def __init__(
        self,
        user_data_dir: str = DEFAULT_USER_DATA_DIR,
        *,
        debug: bool = False,
        lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
        operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
        lock_scope: str = "profile",
        failure_screenshot_dir: Optional[str] = None,
    ) -> None:
        self.user_data_dir = user_data_dir
        self.debug = debug
        self.lock_timeout_sec = lock_timeout_sec
        self.operation_timeout_sec = operation_timeout_sec
        self.lock_scope = lock_scope
        self.failure_screenshot_dir = failure_screenshot_dir
        self._guard_cm: Any = None
        self._context: Any = None
        self._page: Any = None
        self._auth_tracker: Optional[Dict[str, Any]] = None

    def __enter__(self) -> "XianyuSellerSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        cm = self._guard_cm
        self._guard_cm = None
        self._context = None
        self._page = None
        self._auth_tracker = None
        if cm is not None:
            cm.__exit__(None, None, None)

    def _ensure_open(self) -> Tuple[Any, Any, Dict[str, Any]]:
        if self._guard_cm is None:
            cm = _browser_session_guard(
                user_data_dir=self.user_data_dir,
                task_name="seller_batch_session",
                debug=self.debug,
                lock_timeout_sec=self.lock_timeout_sec,
                operation_timeout_sec=self.operation_timeout_sec,
                lock_scope=self.lock_scope,
            )
            ctx, page, tracker = cm.__enter__()
            self._guard_cm = cm
            self._context = ctx
            self._page = page
            self._auth_tracker = tracker
        else:
            pages = list(getattr(self._context, "pages", None) or [])
            while len(pages) > 1:
                extra = pages.pop()
                if extra is not self._page:
                    _detach_listener_safely(extra, "response", (self._auth_tracker or {}).get("_on_response_cb"))
                    try:
                        extra.close()
                    except Exception:
                        pass
            if self._auth_tracker is not None:
                self._auth_tracker["failed_apis"].clear()
        assert self._auth_tracker is not None
        return self._context, self._page, self._auth_tracker

    def get_selling_items(self, expected_item_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        try:
            ctx, page, tracker = self._ensure_open()
            return _get_selling_items_on_page(
                ctx,
                page,
                tracker,
                user_data_dir=self.user_data_dir,
                expected_item_ids=expected_item_ids,
            )
        except XianyuOfflineError as e:
            return _build_offline_result(self.user_data_dir, e.reason, error_code=e.error_code, action="get_selling_items")
        except XianyuLockTimeoutError as e:
            return _build_lock_timeout_result(self.user_data_dir, e.timeout_sec, action="get_selling_items")

    def publish_item(
        self,
        title: str,
        desc: str,
        images: List[str],
        price: float,
        *,
        original_price: Optional[float] = None,
        delivery: str = "包邮",
        failure_screenshot_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        if float(price or 0) <= 0:
            raise ValueError("商品价格必须大于 0")
        clean_title, clean_desc, replaced = sanitize_content(title, desc or title)
        temp_dir = tempfile.mkdtemp(prefix="xianyu_pub_")
        try:
            local_files = _prepare_local_image_files(images, temp_dir)
            ctx, page, tracker = self._ensure_open()
            res = _publish_item_on_page(
                ctx,
                page,
                tracker,
                clean_title=clean_title,
                clean_desc=clean_desc,
                local_files=local_files,
                price=price,
                original_price=original_price,
                delivery=delivery,
                user_data_dir=self.user_data_dir,
                failure_screenshot_dir=failure_screenshot_dir or self.failure_screenshot_dir,
            )
            if res.get("ok"):
                res["replaced_words"] = replaced
            return res
        except XianyuOfflineError as e:
            return _build_offline_result(self.user_data_dir, e.reason, error_code=e.error_code, action="publish")
        except XianyuLockTimeoutError as e:
            return _build_lock_timeout_result(self.user_data_dir, e.timeout_sec, action="publish")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def downshelf_item(self, item_id: str, *, delete: bool = False) -> Dict[str, Any]:
        target_id = str(item_id or "").strip()
        if not target_id:
            raise ValueError("item_id 不能为空")
        action_name = "delete" if delete else "downshelf"
        try:
            ctx, page, tracker = self._ensure_open()
            return _downshelf_item_on_page(
                ctx,
                page,
                tracker,
                item_id=target_id,
                delete=delete,
                user_data_dir=self.user_data_dir,
            )
        except XianyuOfflineError as e:
            return _build_offline_result(self.user_data_dir, e.reason, error_code=e.error_code, action=action_name)
        except XianyuLockTimeoutError as e:
            return _build_lock_timeout_result(self.user_data_dir, e.timeout_sec, action=action_name)


def delete_item(
    item_id: str,
    *,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
    lock_scope: str = "global",
) -> Dict[str, Any]:
    """彻底删除指定 `item_id` 的商品（复用 `downshelf_item(..., delete=True)`）。"""
    return downshelf_item(
        item_id=item_id,
        delete=True,
        user_data_dir=user_data_dir,
        debug=debug,
        lock_timeout_sec=lock_timeout_sec,
        operation_timeout_sec=operation_timeout_sec,
        lock_scope=lock_scope,
    )


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
    lock_scope: str = "profile",
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
            lock_scope=lock_scope,
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
