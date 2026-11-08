# -*- coding: utf-8 -*-
r"""
=========================================================================================
[功能摘要] 闲鱼 (Goofish) 代拍商品自动化工具箱（纯函数调用版，基于独立浏览器目录持久化多账号）
[设计原则] 第一性原理，越简单越可靠：
    1. 独立浏览器目录（如 `W:\temp\xianyu_myself`）原生保存全部账号登录态，支持多账号对应多目录。
    2. 底层内置「跨进程 + 跨线程」串行排队锁与最长等待超时（默认 `lock_timeout_sec=120s`）：
       即使本机多进程/多线程同时发起上架、查商品、查消息或回复请求，底层也会严格串行排队执行，绝不并行冲突，超时自动退出防卡死。
    3. 精准识别「账号掉线/未登录」状态（MTOP `FAIL_SYS_SESSION_EXPIRED` / 登录弹窗 / 缺失 `unb`）：
       自动化函数遇到掉线立即打印醒目告警日志并返回 `{"ok": False, "is_offline": True, "error_code": "ACCOUNT_OFFLINE", ...}`，
       绝不伪装成普通异常，也绝不在后台死等扫码，避免上层业务无效重试浪费时间。
    4. 高信噪比可观测日志：每个阶段记录账号标识、关联商品 ID、MTOP 接口返回码、分步耗时与失败现场诊断信息，便于直接通过日志定位问题。
    5. 全部能力通过纯 Python 函数对外提供，职责单一清晰：
       - `publish_item(...)`                : 代拍商品极简上架（只填图片+文案+价格，自动匹配默认分类）
       - `get_selling_items(...)`           : 查询当前在售商品，自动区分「正常在售(itemStatus=0)」与「被平台下架/删除(itemStatus!=0)」
       - `downshelf_item(...)`              : 将指定 `item_id` 商品下架
       - `delete_item(...)`                 : 将指定 `item_id` 商品彻底删除
       - `get_messages(...)`                : 纯消息监控（获取谁发了消息、何时发的、发了什么内容、明确谈论的是哪件商品 `item_id`/`item_title`/`is_my_item`）
       - `reply_message(...)`               : 向指定买家（可选进一步限定 `item_id`）发送回复消息
       - `check_login_status(...)`          : 快速检测指定浏览器目录账号是否在线
       - `open_browser_for_manual_use(...)` : 手动打开该独立浏览器目录扫码登录或日常维护
=========================================================================================
"""

from contextlib import contextmanager
import json
import logging
import msvcrt
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] %(message)s")
logger = logging.getLogger("publish_to_xianyu")

# ==============================================================================
# 全局默认配置与串行锁配置
# ==============================================================================
DEFAULT_USER_DATA_DIR = r"W:\temp\xianyu_myself"
GOOFISH_HOME_URL = "https://www.goofish.com"
GOOFISH_PUBLISH_URL = "https://www.goofish.com/publish"
GOOFISH_PERSONAL_URL = "https://www.goofish.com/personal"
GOOFISH_IM_URL = "https://www.goofish.com/im"

DEFAULT_LOCK_TIMEOUT_SEC = 120.0
DEFAULT_OPERATION_TIMEOUT_SEC = 60.0

_PROCESS_THREAD_LOCK = threading.Lock()
_GLOBAL_LOCK_FILE_PATH = os.path.join(tempfile.gettempdir(), "xianyu_browser_serial.lock")

# 判定掉线的关键 MTOP 接口（这些接口一旦返回 SESSION_EXPIRED 等错误码即代表账号掉线）
_AUTH_CRITICAL_APIS: Tuple[str, ...] = (
    "mtop.idle.web.user.page.nav",
    "mtop.taobao.idlemessage.pc.loginuser.get",
    "mtop.taobao.idlemessage.pc.session.sync",
    "mtop.idle.web.xyh.item.list",
    "mtop.idle.pc.idleitem.publish",
    "mtop.taobao.idle.item.downshelf",
    "com.taobao.idle.item.delete",
)

_OFFLINE_RET_KEYWORDS: Tuple[str, ...] = (
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_USER_NOT_LOGIN",
    "ERR_SID_INVALID",
    "SESSION_EXPIRED",
    "请先登录",
    "未登录",
)


def _dir_tag(user_data_dir: str) -> str:
    """提取浏览器目录简短标识用于日志展示（如 `xianyu_myself`）。"""
    norm = os.path.normpath(str(user_data_dir or "")).rstrip("\\/")
    return os.path.basename(norm) or norm


# ==============================================================================
# 一、 底层异常定义、跨进程/跨线程串行排队锁、掉线精准检测
# ==============================================================================
class XianyuOfflineError(RuntimeError):
    """闲鱼账号未登录或会话已过期（掉线）专用异常。"""

    def __init__(self, user_data_dir: str, reason: str):
        self.user_data_dir = user_data_dir
        self.reason = reason
        super().__init__(f"闲鱼账号掉线/未登录 [{user_data_dir}]: {reason}")


class XianyuLockTimeoutError(TimeoutError):
    """等待本机闲鱼浏览器串行锁超时异常。"""

    def __init__(self, user_data_dir: str, timeout_sec: float):
        self.user_data_dir = user_data_dir
        self.timeout_sec = timeout_sec
        super().__init__(f"等待闲鱼浏览器串行锁超时 (>{timeout_sec}s) [{user_data_dir}]")


def _build_offline_result(user_data_dir: str, reason: str, action: str = "") -> Dict[str, Any]:
    """统一构造掉线返回结构，方便上层调用方通过 `res['is_offline']` 立即感知并停止重试。"""
    res: Dict[str, Any] = {
        "ok": False,
        "is_offline": True,
        "error_code": "ACCOUNT_OFFLINE",
        "user_data_dir": user_data_dir,
        "message": (
            f"闲鱼账号处于未登录或掉线状态 ({reason})，"
            f"请先调用 open_browser_for_manual_use(r'{user_data_dir}') 扫码恢复登录"
        ),
    }
    if action:
        res["action"] = action
    return res


def _build_lock_timeout_result(user_data_dir: str, timeout_sec: float, action: str = "") -> Dict[str, Any]:
    """统一构造排队等待锁超时返回结构。"""
    res: Dict[str, Any] = {
        "ok": False,
        "is_offline": False,
        "error_code": "LOCK_TIMEOUT",
        "user_data_dir": user_data_dir,
        "message": f"等待本机闲鱼浏览器排队锁超时 (最长等待 {timeout_sec}s)，已中止本次请求以防卡死",
    }
    if action:
        res["action"] = action
    return res


@contextmanager
def _acquire_serial_browser_lock(
    user_data_dir: str,
    task_name: str = "browser_task",
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
) -> Generator[None, None, None]:
    """
    本机「跨线程 + 跨进程」浏览器串行排队锁：
    1. 先获取当前进程内 `_PROCESS_THREAD_LOCK`（带超时）；
    2. 再获取操作系统内核级文件字节锁 `msvcrt.locking`（若持锁进程崩溃退出，Windows 内核会自动释放，绝不死锁）；
    3. 超过 `lock_timeout_sec` 仍未获取到锁则抛出 `XianyuLockTimeoutError`。
    """
    timeout_sec = max(1.0, float(lock_timeout_sec or DEFAULT_LOCK_TIMEOUT_SEC))
    start_ts = time.monotonic()
    deadline = start_ts + timeout_sec
    logged_waiting = False
    tag = _dir_tag(user_data_dir)

    # 1. 进程内线程锁排队
    if not _PROCESS_THREAD_LOCK.acquire(timeout=0.05):
        logged_waiting = True
        logger.info(
            f"⏳ [并发锁/排队中] 任务={task_name} | 账号目录={tag} | "
            f"原因=当前进程内有其他闲鱼浏览器任务正在运行 | 最长等待={timeout_sec:.0f}s"
        )
        remaining = max(0.05, deadline - time.monotonic())
        if not _PROCESS_THREAD_LOCK.acquire(timeout=remaining):
            waited = time.monotonic() - start_ts
            logger.error(
                f"❌ [并发锁/等待超时] 任务={task_name} | 账号目录={tag} (<{user_data_dir}>) | "
                f"已等待={waited:.2f}s (上限 {timeout_sec:.0f}s) | 判定=获取进程内线程锁超时，放弃本次执行以防卡死"
            )
            raise XianyuLockTimeoutError(user_data_dir, timeout_sec)

    lock_file = None
    file_locked = False
    try:
        os.makedirs(os.path.dirname(_GLOBAL_LOCK_FILE_PATH), exist_ok=True)
        lock_file = open(_GLOBAL_LOCK_FILE_PATH, "a+b")
        lock_file.seek(0)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()

        # 2. 跨进程 OS 内核级文件锁排队
        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                file_locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    waited = time.monotonic() - start_ts
                    logger.error(
                        f"❌ [并发锁/等待超时] 任务={task_name} | 账号目录={tag} (<{user_data_dir}>) | "
                        f"已等待={waited:.2f}s (上限 {timeout_sec:.0f}s) | 锁文件={_GLOBAL_LOCK_FILE_PATH} | "
                        f"判定=获取跨进程文件锁超时，放弃本次执行以防卡死"
                    )
                    raise XianyuLockTimeoutError(user_data_dir, timeout_sec)
                if not logged_waiting:
                    logged_waiting = True
                    logger.info(
                        f"⏳ [并发锁/排队中] 任务={task_name} | 账号目录={tag} | "
                        f"原因=本机其他进程正在占用闲鱼浏览器 | 最长等待={timeout_sec:.0f}s"
                    )
                time.sleep(0.2)

        waited = time.monotonic() - start_ts
        if logged_waiting or waited >= 0.5:
            logger.info(
                f"▶️ [并发锁/已获取] 任务={task_name} | 账号目录={tag} | 排队等待耗时={waited:.2f}s"
            )

        yield
    finally:
        if file_locked and lock_file is not None:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        if lock_file is not None:
            try:
                lock_file.close()
            except Exception:
                pass
        _PROCESS_THREAD_LOCK.release()


def clean_browser_cache(user_data_dir: str = DEFAULT_USER_DATA_DIR) -> None:
    """清理浏览器冗余缓存，严格保留 Cookie / LocalStorage 等核心登录凭证。"""
    if not os.path.exists(user_data_dir):
        return

    garbage_folders = (
        "Cache",
        "Code Cache",
        "GPUCache",
        "ShaderCache",
        "GrShaderCache",
        "Service Worker",
        "CacheStorage",
    )
    deleted_count = 0
    for base in (user_data_dir, os.path.join(user_data_dir, "Default")):
        for name in garbage_folders:
            path = os.path.join(base, name)
            if not os.path.exists(path):
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                deleted_count += 1
            except Exception:
                pass

    logger.info(f"[环境/缓存清理] 目录=<{user_data_dir}> | 清理缓存目录数={deleted_count}")


def launch_persistent_context(
    p,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    args: Optional[List[str]] = None,
    hide_automation: bool = True,
    headless: bool = False,
    offscreen: bool = False,
):
    """启动挂载独立浏览器目录的持久化 Chrome 上下文。"""
    os.makedirs(user_data_dir, exist_ok=True)
    default_args = list(args or ["--disable-blink-features=AutomationControlled"])
    if offscreen:
        default_args.append("--window-position=-2000,0")
    else:
        default_args.extend(["--start-maximized", "--window-position=0,0"])

    kwargs: Dict[str, Any] = {
        "channel": "chrome",
        "user_data_dir": user_data_dir,
        "headless": headless,
        "args": default_args,
        "no_viewport": True,
    }
    if hide_automation:
        kwargs["ignore_default_args"] = ["--enable-automation"]

    return p.chromium.launch_persistent_context(**kwargs)


def _attach_auth_tracker(page) -> Dict[str, Any]:
    """
    在页面上挂载 MTOP 登录态与异常监听器，实时捕获：
    - `user_id`      : `mtop.taobao.idlemessage.pc.loginuser.get` 返回的 userId
    - `display_name` : `mtop.idle.web.user.page.nav` 返回的昵称
    - `expired_rets` : 关键接口返回的掉线/未登录错误码列表
    - `failed_apis`  : 任意 MTOP 接口返回的非 SUCCESS 状态码（用于故障诊断日志）
    """
    tracker: Dict[str, Any] = {
        "user_id": "",
        "display_name": "",
        "expired_rets": [],
        "failed_apis": [],
    }

    def _on_response(resp):
        url = resp.url
        if "mtop." not in url:
            return
        api_short = url.split("/h5/")[-1].split("/")[0] if "/h5/" in url else url.split("?")[0]
        try:
            body = resp.json()
        except Exception:
            return
        if not isinstance(body, dict):
            return

        ret_list = body.get("ret") or []
        ret_str = " | ".join(str(x) for x in ret_list)

        if ret_str and "SUCCESS" not in ret_str and "FAIL_SYS_TOKEN_EMPTY" not in ret_str:
            tracker["failed_apis"].append(f"{api_short}=[{ret_str}]")

        if any(api in url for api in _AUTH_CRITICAL_APIS):
            for kw in _OFFLINE_RET_KEYWORDS:
                if kw in ret_str:
                    entry = f"{api_short} -> {ret_str}"
                    if entry not in tracker["expired_rets"]:
                        tracker["expired_rets"].append(entry)
                    break

        data = body.get("data") or {}
        if "mtop.taobao.idlemessage.pc.loginuser.get" in url and data.get("userId"):
            tracker["user_id"] = str(data["userId"])
        elif "mtop.idle.web.user.page.nav" in url:
            base = ((data.get("module") or {}).get("base")) or {}
            if base.get("displayName"):
                tracker["display_name"] = str(base["displayName"])

    page.on("response", _on_response)
    return tracker


def _verify_account_online(
    context,
    page,
    auth_tracker: Dict[str, Any],
    user_data_dir: str,
    task_name: str = "",
) -> str:
    """
    严格校验当前浏览器目录中的闲鱼账号是否处于在线登录状态：
    1. 检查关键 MTOP 接口是否返回 `FAIL_SYS_SESSION_EXPIRED` 等掉线错误；
    2. 检查页面是否弹出了 `passport.goofish.com/mini_login` 或 `login.taobao.com` 登录框 iframe；
    3. 检查浏览器是否存在 `unb` 登录 Cookie。
    一旦判定掉线，立即打印醒目告警日志并抛出 `XianyuOfflineError`（绝不死等扫码）。
    """
    cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name") and c.get("value")}
    unb = str(cookies.get("unb") or auth_tracker.get("user_id") or "").strip()

    offline_reasons: List[str] = []

    if auth_tracker.get("expired_rets"):
        offline_reasons.append(f"接口返回会话过期({auth_tracker['expired_rets'][0]})")

    login_frames = [
        f.url
        for f in page.frames
        if "passport.goofish.com/mini_login" in f.url or "login.taobao.com" in f.url
    ]
    if login_frames:
        offline_reasons.append("页面已弹出闲鱼登录框(mini_login)")

    if not unb:
        offline_reasons.append("浏览器缺失 unb 登录凭证 Cookie")

    if offline_reasons:
        reason_str = "；".join(offline_reasons)
        logger.error(
            f"\n{'!' * 76}\n"
            f"❌ [账号掉线告警] 检测到闲鱼账号未登录或登录态已失效(掉线)！\n"
            f"   ├── 触发任务   : {task_name or '自动化任务'}\n"
            f"   ├── 浏览器目录 : <{user_data_dir}>\n"
            f"   ├── 当前页面   : {page.url}\n"
            f"   ├── 掉线证据   : {reason_str}\n"
            f"   └── 恢复方式   : 请暂停该账号的后续自动任务，调用 open_browser_for_manual_use(r\"{user_data_dir}\") 重新扫码登录！\n"
            f"{'!' * 76}"
        )
        raise XianyuOfflineError(user_data_dir=user_data_dir, reason=reason_str)

    return unb


def _collect_page_error_tips(page) -> str:
    """收集页面上可见的 Ant Design 表单报错或 Toast 提示文本，用于失败时输出诊断日志。"""
    try:
        tips = page.evaluate(
            """() => {
            const selectors = [
                '.ant-message-notice-content',
                '.ant-notification-notice',
                '.ant-form-item-explain-error',
                '.ant-modal-body',
                '[class*="error"]',
                '[class*="warn"]'
            ];
            const out = [];
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (txt && txt.length <= 120 && !out.includes(txt)) {
                        out.push(txt);
                    }
                }
            }
            return out.slice(0, 5).join(' | ');
        }"""
        )
        return str(tips or "").strip()
    except Exception:
        return ""


@contextmanager
def _browser_session_guard(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    *,
    task_name: str = "browser_task",
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Generator[Tuple[Any, Any, Dict[str, Any]], None, None]:
    """
    统一浏览器自动化执行上下文：
    - 自动获取「跨线程 + 跨进程」串行锁（最长等待 `lock_timeout_sec` 秒）；
    - 启动持久化 Chrome 上下文并设置全局最大操作超时（`operation_timeout_sec` 秒），防止页面卡死；
    - 挂载 MTOP 登录态与错误监听器；
    - 退出时确保关闭浏览器并释放串行锁。
    """
    op_timeout_ms = int(max(10.0, float(operation_timeout_sec or DEFAULT_OPERATION_TIMEOUT_SEC)) * 1000)
    with _acquire_serial_browser_lock(
        user_data_dir=user_data_dir,
        task_name=task_name,
        lock_timeout_sec=lock_timeout_sec,
    ):
        with sync_playwright() as p:
            context = None
            try:
                context = launch_persistent_context(
                    p,
                    user_data_dir=user_data_dir,
                    headless=False,
                    offscreen=not debug,
                )
                context.set_default_timeout(op_timeout_ms)
                context.set_default_navigation_timeout(op_timeout_ms)
                page = context.pages[0] if context.pages else context.new_page()
                auth_tracker = _attach_auth_tracker(page)
                yield context, page, auth_tracker
            finally:
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass


def open_browser_for_manual_use(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    home_url: str = GOOFISH_PERSONAL_URL,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
) -> None:
    """
    启动独立浏览器目录并移交人工操作（如首次扫码登录、掉线后重登、日常维护），直接关闭浏览器窗口即可自动保存并结束。
    """
    logger.info(f"\n{'=' * 60}\n[人工接管/启动] 正在打开闲鱼浏览器 | 目录: <{user_data_dir}>\n{'=' * 60}")
    with _acquire_serial_browser_lock(
        user_data_dir=user_data_dir,
        task_name="open_browser_for_manual_use",
        lock_timeout_sec=lock_timeout_sec,
    ):
        clean_browser_cache(user_data_dir)
        with sync_playwright() as p:
            context = None
            try:
                context = launch_persistent_context(
                    p, user_data_dir=user_data_dir, headless=False, offscreen=False
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
        return _build_offline_result(user_data_dir, e.reason, action="check_login_status")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="check_login_status")


# ==============================================================================
# 二、 代拍文案净水与图片防撞处理（账号安全护栏）
# ==============================================================================
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_SHORTLINK_RE = re.compile(
    r"(?:u\.jd\.com|3\.cn|m\.tb\.cn|s\.click\.taobao\.com|yangkeduo\.com|pinduoduo\.com)/[A-Za-z0-9_/?=&.-]+",
    re.IGNORECASE,
)
_TAOKOULING_RE = re.compile(r"[￥¥$€£₴¢][0-9A-Za-z]{8,14}[￥¥$€£₴¢]|[\d]{1,2}\.[\d]+\s*[A-Za-z]{2,4}:/[A-Za-z0-9/]+")

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


def sanitize_content(title: str, desc: str) -> Tuple[str, str, List[str]]:
    """剥离推广短链与淘口令，替换极限广告词，拦截站外引流敏感词。"""
    clean_title = _TAOKOULING_RE.sub("", _SHORTLINK_RE.sub("", _URL_RE.sub("", str(title or ""))))
    clean_desc = _TAOKOULING_RE.sub("", _SHORTLINK_RE.sub("", _URL_RE.sub("", str(desc or ""))))

    replaced: List[str] = []
    for bad, good in _EXTREME_WORD_REPLACEMENTS:
        if bad in clean_title or bad in clean_desc:
            clean_title = clean_title.replace(bad, good)
            clean_desc = clean_desc.replace(bad, good)
            replaced.append(f"{bad}->{good}")

    clean_title = re.sub(r"\s+", " ", clean_title).strip()[:30].rstrip()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in clean_desc.splitlines()]
    clean_desc = "\n".join(ln for ln in lines if ln).strip()

    combined = f"{clean_title}\n{clean_desc}"
    for kw in _PROHIBITED_KEYWORDS:
        if kw.lower() in combined.lower():
            raise ValueError(f"文案触发闲鱼违禁/外联拦截词: 【{kw}】")

    if not clean_title:
        raise ValueError("商品标题不能为空")
    return clean_title, clean_desc, replaced


def _prepare_local_image_files(images: List[str], temp_dir: str) -> List[str]:
    """
    将本地图片或远程图片 URL 统一准备为临时本地图片文件列表，
    并对 JPEG 注入无损注释微扰文件 Hash，防止多商品撞图限流。
    """
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
        return _build_offline_result(user_data_dir, e.reason, action="get_selling_items")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_selling_items")


# ==============================================================================
# 四、 核心函数 2：代拍商品极简上架（免选商品类型，只填图+文案+价格）
# ==============================================================================
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
) -> Dict[str, Any]:
    """
    代拍商品极简上架：
    1. 自动清洗文案短链/极限词，准备本地图片（支持传入本地路径或 http/https 图片 URL）；
    2. 获取本机浏览器串行排队锁，打开 `https://www.goofish.com/publish` 并检测是否掉线；
    3. 仅上传图片、填入文案（标题+换行+描述）、填入价格（不专门选具体商品类型，由闲鱼自动匹配默认分类）；
    4. 点击「发布」并拦截 `mtop.idle.pc.idleitem.publish` 响应返回 `item_id`。
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    if price <= 0:
        raise ValueError("商品价格必须大于 0")
    clean_title, clean_desc, replaced = sanitize_content(title, desc or title)
    full_text = f"{clean_title}\n{clean_desc}" if clean_desc != clean_title else clean_title

    temp_dir = tempfile.mkdtemp(prefix="xianyu_pub_")
    try:
        t_img0 = time.monotonic()
        local_files = _prepare_local_image_files(images, temp_dir)
        img_prep_sec = time.monotonic() - t_img0
        logger.info(
            f"[商品上架/1-准备] 账号目录={tag} | 标题=【{clean_title}】 | 售价=¥{price:.2f} | "
            f"原价={f'¥{original_price:.2f}' if original_price else '未设'} | 邮费={delivery} | "
            f"图片数={len(local_files)}张 (准备耗时={img_prep_sec:.2f}s) | 敏感词替换={replaced or '无'}"
        )

        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name="publish_item",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
        ) as (context, page, auth_tracker):
            publish_resp_data: Dict[str, Any] = {}

            def on_response(resp):
                if "mtop.idle.pc.idleitem.publish" in resp.url:
                    try:
                        publish_resp_data.update(resp.json())
                    except Exception:
                        pass

            page.on("response", on_response)
            t_nav0 = time.monotonic()
            page.goto(GOOFISH_PUBLISH_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            unb = _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="publish_item")
            nick = auth_tracker.get("display_name", "")
            logger.info(
                f"[商品上架/2-页面就绪] 账号={nick or unb} ({tag}) | 发布页加载耗时={time.monotonic() - t_nav0:.2f}s"
            )

            # Step 1: 上传图片
            t_up0 = time.monotonic()
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(local_files)
            page.wait_for_timeout(2500 + 800 * len(local_files))
            up_sec = time.monotonic() - t_up0

            # Step 2: 填入宝贝描述（代拍商品无需专门选择分类，输入文案后闲鱼自动默认勾选分类）
            editor = page.locator('[contenteditable="true"]').first
            editor.click()
            editor.fill(full_text)
            page.wait_for_timeout(2500)

            # Step 3: 填入价格与可选原价
            price_input = page.locator('.ant-form-item:has-text("价格") input[placeholder="0.00"]').first
            price_input.click()
            price_input.fill(f"{price:.2f}")
            page.wait_for_timeout(500)

            if original_price and original_price > 0:
                orig_input = page.locator('.ant-form-item:has-text("原价") input[placeholder="0.00"]').first
                if orig_input.count() > 0:
                    orig_input.click()
                    orig_input.fill(f"{original_price:.2f}")
                    page.wait_for_timeout(400)

            if delivery and delivery != "包邮":
                radio = page.locator(f'label.ant-radio-wrapper:has-text("{delivery}")').first
                if radio.count() > 0:
                    radio.click()
                    page.wait_for_timeout(400)

            auto_cat = ""
            try:
                cat_item = page.locator('.ant-form-item:has-text("分类")')
                if cat_item.count() > 0:
                    auto_cat = re.sub(r"\s+", " ", cat_item.first.inner_text()).replace("分类 *", "").strip()
            except Exception:
                pass

            logger.info(
                f"[商品上架/3-表单已填] 账号={nick or unb} ({tag}) | 图片上传耗时={up_sec:.2f}s | "
                f"文案长度={len(full_text)}字 | 自动匹配分类=【{auto_cat or '默认'}】"
            )

            # Step 4: 点击「发布」按钮并等待 MTOP 响应
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
                logger.error(
                    f"❌ [商品上架/失败诊断] 账号={nick or unb} ({tag}) | 标题=【{clean_title}】 | "
                    f"MTOP发布响应={publish_resp_data} | 页面错误提示=【{page_tips or '无'}】 | "
                    f"其他失败MTOP={auth_tracker.get('failed_apis')} | 当前URL={page.url}"
                )
                raise RuntimeError(
                    f"上架未返回 itemId | MTOP响应: {publish_resp_data} | 页面提示: {page_tips or '无'}"
                )

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
                "price": f"{price:.2f}",
                "auto_category": auto_cat,
                "replaced_words": replaced,
                "ret": ret_str or "SUCCESS::调用成功",
            }
            logger.info(
                f"[商品上架/4-成功] 🎉 账号={nick or unb} ({tag}) | item_id={item_id} | "
                f"售价=¥{price:.2f} | 分类=【{auto_cat or '默认'}】 | MTOP={result['ret']} | "
                f"总耗时={elapsed:.2f}s | 链接={result['item_url']}"
            )
            return result
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, action="publish")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="publish")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==============================================================================
# 五、 核心函数 3：商品下架 (`downshelf_item`) 与彻底删除 (`delete_item`)
# ==============================================================================
def downshelf_item(
    item_id: str,
    *,
    delete: bool = False,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    对指定 `item_id` 的商品执行下架（或删除）：
    - `delete=False`（默认）：点击「下架」并确认；
    - `delete=True`：点击「删除」并确认。
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    target_id = str(item_id or "").strip()
    if not target_id:
        raise ValueError("item_id 不能为空")

    action_name = "delete" if delete else "downshelf"
    action_label = "删除" if delete else "下架"
    target_api = "com.taobao.idle.item.delete" if delete else "mtop.taobao.idle.item.downshelf"
    item_url = f"{GOOFISH_HOME_URL}/item?id={target_id}"
    logger.info(
        f"[商品{action_label}/开始] 账号目录={tag} | item_id={target_id} | URL={item_url}"
    )

    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name=f"{action_name}_item({target_id})",
            debug=debug,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
        ) as (context, page, auth_tracker):
            action_resp: Dict[str, Any] = {}

            def on_response(resp):
                if target_api in resp.url:
                    try:
                        action_resp.update(resp.json())
                    except Exception:
                        pass

            page.on("response", on_response)
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
                visible_btns = page.evaluate(
                    """() => Array.from(document.querySelectorAll('div[class*="sellerButton"], button'))
                        .map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 10)"""
                )
                page_tips = _collect_page_error_tips(page)
                logger.error(
                    f"❌ [商品{action_label}/失败诊断] 账号={nick or unb} ({tag}) | item_id={target_id} | "
                    f"原因=页面未找到【{action_label}】按钮 | 页面当前可见按钮={visible_btns} | "
                    f"页面提示=【{page_tips or '无'}】 | 当前URL={page.url}"
                )
                raise RuntimeError(
                    f"在商品页未找到【{action_label}】按钮 (可见按钮: {visible_btns})，"
                    f"请确认商品 {target_id} 属于当前账号且处于可{action_label}状态"
                )

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

            if ok:
                logger.info(
                    f"[商品{action_label}/成功] ✅ 账号={nick or unb} ({tag}) | item_id={target_id} | "
                    f"MTOP={ret_str} | 耗时={elapsed:.2f}s"
                )
            else:
                logger.error(
                    f"❌ [商品{action_label}/接口异常] 账号={nick or unb} ({tag}) | item_id={target_id} | "
                    f"MTOP响应={action_resp} | 耗时={elapsed:.2f}s"
                )

            return {
                "ok": ok,
                "is_offline": False,
                "action": action_name,
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "item_id": target_id,
                "ret": ret_str,
            }
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, action=action_name)
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action=action_name)


def delete_item(
    item_id: str,
    *,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """彻底删除指定 `item_id` 的商品（内部直接复用 `downshelf_item(..., delete=True)`）。"""
    return downshelf_item(
        item_id=item_id,
        delete=True,
        user_data_dir=user_data_dir,
        debug=debug,
        lock_timeout_sec=lock_timeout_sec,
        operation_timeout_sec=operation_timeout_sec,
    )


# ==============================================================================
# 六、 核心函数 4：消息监控 (`get_messages`) 与 消息回复 (`reply_message`)
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
    2. 从 DOM 顶部商品栏兜底提取关联商品 `dom_item_id`、`dom_item_price`、`dom_item_url`；
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


def _send_chat_message_in_current_pane(page, reply_text: str) -> bool:
    """在当前已打开的右侧聊天面板中输入 `reply_text` 并点击发送。"""
    clean_text = str(reply_text or "").strip()
    if not clean_text:
        raise ValueError("回复内容 reply_text 不能为空")

    textarea = page.locator('main[class*="chat-main"] textarea, textarea[placeholder*="请输入消息"]').first
    if textarea.count() == 0:
        raise RuntimeError("当前会话窗口未找到消息输入框 textarea")

    textarea.click()
    textarea.fill(clean_text)
    page.wait_for_timeout(400)

    send_btn = page.locator('main[class*="chat-main"] button:has-text("发")').first
    if send_btn.count() > 0 and not send_btn.is_disabled():
        send_btn.click()
    else:
        textarea.press("Enter")

    page.wait_for_timeout(1200)
    return True


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
        return _build_offline_result(user_data_dir, e.reason, action="get_messages")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_messages")


def reply_message(
    target_user: str,
    reply_text: str,
    *,
    item_id: Optional[str] = None,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    向指定买家 (`target_user` 昵称，可选配合 `item_id` 精确校验商品) 发送回复消息：
    1. 获取本机浏览器串行排队锁，打开 `https://www.goofish.com/im` 并检测是否掉线；
    2. 在左侧会话列表中定位 `target_user`（若同时传入了 `item_id`，还会校验点开的会话所关联的商品 ID 是否匹配）；
    3. 在输入框填入 `reply_text` 并点击发送，返回包含关联商品 `item_id`/`item_title` 的发送结果。
    """
    t0 = time.monotonic()
    tag = _dir_tag(user_data_dir)
    target_clean = str(target_user or "").strip()
    reply_clean = str(reply_text or "").strip()
    expected_item_id = str(item_id or "").strip()

    if not target_clean:
        raise ValueError("target_user (目标买家昵称) 不能为空")
    if not reply_clean:
        raise ValueError("reply_text (回复内容) 不能为空")

    logger.info(
        f"[消息回复/开始] 账号目录={tag} | 目标买家=【{target_clean}】 | "
        f"限定商品ID={expected_item_id or '不限'} | 回复内容=【{reply_clean}】"
    )
    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name=f"reply_message({target_clean})",
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
                        latest_headinfo.clear()
                        latest_headinfo.update(
                            {
                                "item_id": str(common.get("itemId") or pre_info.get("itemId") or "").strip(),
                                "item_title": str(pre_info.get("title") or "").strip(),
                            }
                        )
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(GOOFISH_IM_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            unb = _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="reply_message")
            nick = auth_tracker.get("display_name", "")

            all_convs = _extract_left_conversations(page)
            candidates = [c for c in all_convs if c["user_nick"] == target_clean]
            if not candidates:
                candidates = [c for c in all_convs if target_clean.lower() in c["user_nick"].lower()]

            if not candidates:
                available_nicks = [c["user_nick"] for c in all_convs]
                logger.warning(
                    f"⚠️ [消息回复/未找到买家] 账号={nick or unb} ({tag}) | 目标买家=【{target_clean}】 | "
                    f"当前可见会话列表={available_nicks}"
                )
                return {
                    "ok": False,
                    "is_offline": False,
                    "action": "reply_message",
                    "error_code": "TARGET_USER_NOT_FOUND",
                    "user_data_dir": user_data_dir,
                    "target_user": target_clean,
                    "available_users": available_nicks,
                    "message": f"在消息列表中未找到买家【{target_clean}】，当前可见会话: {available_nicks}",
                }

            conv_locator = page.locator('div[class*="conversation-item--"]')
            selected_conv = None
            current_item_id = ""
            current_item_title = ""

            for cand in candidates:
                latest_headinfo.clear()
                conv_locator.nth(cand["dom_index"]).click()
                for _ in range(10):
                    if latest_headinfo:
                        break
                    page.wait_for_timeout(200)
                page.wait_for_timeout(400)

                chat_detail = _extract_current_chat_detail(page)
                current_item_id = latest_headinfo.get("item_id") or chat_detail.get("dom_item_id") or ""
                current_item_title = latest_headinfo.get("item_title") or ""

                if not expected_item_id or current_item_id == expected_item_id:
                    selected_conv = cand
                    break

            if selected_conv is None:
                logger.warning(
                    f"⚠️ [消息回复/商品ID不匹配] 账号={nick or unb} ({tag}) | 目标买家=【{target_clean}】 | "
                    f"期望商品ID={expected_item_id} | 实际会话商品ID={current_item_id}"
                )
                return {
                    "ok": False,
                    "is_offline": False,
                    "action": "reply_message",
                    "error_code": "ITEM_ID_MISMATCH",
                    "user_data_dir": user_data_dir,
                    "target_user": target_clean,
                    "expected_item_id": expected_item_id,
                    "actual_item_id": current_item_id,
                    "message": f"找到买家【{target_clean}】，但其关联商品ID ({current_item_id}) 与指定商品ID ({expected_item_id}) 不一致",
                }

            _send_chat_message_in_current_pane(page, reply_clean)
            _verify_account_online(context, page, auth_tracker, user_data_dir, task_name="reply_message")

            elapsed = time.monotonic() - t0
            result = {
                "ok": True,
                "is_offline": False,
                "action": "reply_message",
                "user_data_dir": user_data_dir,
                "unb": unb,
                "nick": nick,
                "target_user": selected_conv["user_nick"],
                "item_id": current_item_id,
                "item_title": current_item_title,
                "reply_text": reply_clean,
            }
            logger.info(
                f"[消息回复/成功] ✅ 账号={nick or unb} ({tag}) | 买家=【{selected_conv['user_nick']}】 | "
                f"关联商品ID={current_item_id or '无'} ({current_item_title[:20] or '无标题'}) | "
                f"发送内容=【{reply_clean}】 | 耗时={elapsed:.2f}s"
            )
            return result
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, action="reply_message")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="reply_message")


# ==============================================================================
#                                  函数调用示例
# ==============================================================================
if __name__ == "__main__":
    USER_DATA_DIR = r"W:\temp\xianyu_myself"

    # # 场景一：手动打开独立浏览器目录扫码登录或日常维护（关闭窗口自动保存退出）
    # open_browser_for_manual_use(
    #     user_data_dir=USER_DATA_DIR,
    #     home_url="https://www.goofish.com/personal",
    # )

    # # 场景二：代拍商品极简上架（直接传参：标题、描述、图片路径或URL列表、价格）
    # pub_res = publish_item(
    #     title="维达抽纸整箱24包代拍好价直发",
    #     desc="全新正品代拍服务，拍下后安排官方旗舰渠道直发到家，全国包邮。",
    #     images=[r"W:\project\python_project\test.png"],
    #     price=19.90,
    #     original_price=39.90,
    #     delivery="包邮",
    #     user_data_dir=USER_DATA_DIR,
    #     debug=False,
    # )
    # print("上架结果:", json.dumps(pub_res, ensure_ascii=False, indent=2))

    # # 场景三：商品下架 / 彻底删除
    # down_res = downshelf_item(item_id="1097491138785", user_data_dir=USER_DATA_DIR)
    # del_res = delete_item(item_id="1097491138785", user_data_dir=USER_DATA_DIR)

    # # 场景四：查询当前在售商品列表 & 检测指定商品是否被平台下架
    # selling_res = get_selling_items(
    #     user_data_dir=USER_DATA_DIR,
    #     expected_item_ids=["1095288609282", "1094773946145"],
    #     debug=False,
    # )
    # print("在售查询结果:", json.dumps(selling_res, ensure_ascii=False, indent=2))

    # 场景五：纯消息监控（获取谁发了信息、何时发的、发了什么内容、明确谈论的是哪件商品）
    msg_res = get_messages(
        user_data_dir=USER_DATA_DIR,
        only_unread=False,
        only_my_items=False,
        max_sessions=3,
        debug=False,
    )
    print("消息监控结果:", json.dumps(msg_res, ensure_ascii=False, indent=2))

    # # 场景六：根据商品与买家定制话术并回复消息（支持可选传入 item_id 精准校验会话商品）
    # reply_res = reply_message(
    #     target_user="昵称未填写",
    #     item_id="1094773946145",
    #     reply_text="您好，在的，可以直接拍下~",
    #     user_data_dir=USER_DATA_DIR,
    #     debug=False,
    # )
    # print("消息回复结果:", json.dumps(reply_res, ensure_ascii=False, indent=2))
