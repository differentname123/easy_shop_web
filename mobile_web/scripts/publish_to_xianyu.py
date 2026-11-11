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
       - `search_xianyu_items(...)`         : 免登录搜索指定关键字的闲鱼商品概览列表（含标题描述、价格、想要人数、是否包邮等）
       - `get_xianyu_item_detail(...)`      : 免登录获取指定商品的完整详情（含完整文字描述 `desc`、多规格 `sku_list` 及各规格对应价格与库存）
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
from urllib.parse import quote, urlsplit

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
DEFAULT_GUEST_USER_DATA_DIR = r"W:\temp\xianyu_guest"
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

_RISK_BLOCK_RET_KEYWORDS: Tuple[str, ...] = (
    "RGV587_ERROR",
    "FAIL_SYS_USER_VALIDATE",
    "被挤爆啦",
    "滑动验证",
)


def _dir_tag(user_data_dir: str) -> str:
    """提取浏览器目录简短标识用于日志展示（如 `xianyu_myself`）。"""
    norm = os.path.normpath(str(user_data_dir or "")).rstrip("\\/")
    return os.path.basename(norm) or norm


# ==============================================================================
# 一、 底层异常定义、跨进程/跨线程串行排队锁、掉线与风控滑块精准检测
# ==============================================================================
class XianyuOfflineError(RuntimeError):
    """闲鱼账号未登录/掉线 或 触发风控滑块验证 专用中断异常。"""

    def __init__(self, user_data_dir: str, reason: str, error_code: str = "ACCOUNT_OFFLINE"):
        self.user_data_dir = user_data_dir
        self.reason = reason
        self.error_code = error_code
        super().__init__(f"闲鱼账号异常中断({error_code}) [{user_data_dir}]: {reason}")


class XianyuLockTimeoutError(TimeoutError):
    """等待本机闲鱼浏览器串行锁超时异常。"""

    def __init__(self, user_data_dir: str, timeout_sec: float):
        self.user_data_dir = user_data_dir
        self.timeout_sec = timeout_sec
        super().__init__(f"等待闲鱼浏览器串行锁超时 (>{timeout_sec}s) [{user_data_dir}]")


def _build_offline_result(
    user_data_dir: str,
    reason: str,
    action: str = "",
    error_code: str = "ACCOUNT_OFFLINE",
) -> Dict[str, Any]:
    """统一构造掉线或风控滑块中断返回结构，方便上层通过 `res['is_offline']` 立即停止重试。"""
    is_risk = error_code == "RISK_CONTROL_BLOCKED"
    res: Dict[str, Any] = {
        "ok": False,
        "is_offline": True,
        "is_risk_blocked": is_risk,
        "error_code": error_code,
        "user_data_dir": user_data_dir,
        "message": (
            f"闲鱼账号{'触发风控滑块验证' if is_risk else '处于未登录或掉线状态'} ({reason})，"
            f"请暂停自动重试并调用 open_browser_for_manual_use(r'{user_data_dir}') 人工处理"
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
    在页面上挂载 MTOP 登录态与风控监听器，实时捕获：
    - `user_id`      : `mtop.taobao.idlemessage.pc.loginuser.get` 返回的 userId
    - `display_name` : `mtop.idle.web.user.page.nav` 返回的昵称
    - `expired_rets` : 关键接口返回的掉线/未登录错误码列表
    - `risk_rets`    : 触发阿里滑块/验证码限流（如 `RGV587_ERROR`）的接口列表
    - `failed_apis`  : 任意 MTOP 接口返回的非 SUCCESS 状态码（用于故障诊断日志）
    """
    tracker: Dict[str, Any] = {
        "user_id": "",
        "display_name": "",
        "expired_rets": [],
        "risk_rets": [],
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

        for rkw in _RISK_BLOCK_RET_KEYWORDS:
            if rkw in ret_str:
                entry = f"{api_short} -> {ret_str}"
                if entry not in tracker["risk_rets"]:
                    tracker["risk_rets"].append(entry)
                break

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
    严格校验当前浏览器目录中的闲鱼账号状态（掉线检测 + 滑块风控检测）：
    1. 检查是否触发阿里滑块/验证码（`RGV587_ERROR` 或页面出现 `punish` / `.baxia-dialog` 滑块弹窗）；
    2. 检查关键 MTOP 接口是否返回 `FAIL_SYS_SESSION_EXPIRED` 等掉线错误；
    3. 检查页面是否弹出了 `passport.goofish.com/mini_login` 登录框或缺失 `unb` Cookie。
    一旦发现异常，立即打印醒目告警日志并抛出 `XianyuOfflineError` 中止当前操作，绝不盲目重试。
    """
    # 1. 优先检查是否触发滑块/风控验证
    risk_reasons: List[str] = []
    if auth_tracker.get("risk_rets"):
        risk_reasons.append(f"接口返回风控滑块拦截({auth_tracker['risk_rets'][0]})")

    punish_frames = [
        f.url
        for f in page.frames
        if ("punish" in f.url or "baxia" in f.url or "sec.taobao.com" in f.url)
        and "mini_login" not in f.url
    ]
    if punish_frames:
        risk_reasons.append("页面已弹出阿里安全滑块验证框(baxia/punish)")

    if risk_reasons:
        reason_str = "；".join(risk_reasons)
        logger.error(
            f"\n{'!' * 76}\n"
            f"🚨 [风控滑块告警] 检测到闲鱼触发安全验证/滑块限制，已立即中止操作以保护账号！\n"
            f"   ├── 触发任务   : {task_name or '自动化任务'}\n"
            f"   ├── 浏览器目录 : <{user_data_dir}>\n"
            f"   ├── 当前页面   : {page.url}\n"
            f"   ├── 风控证据   : {reason_str}\n"
            f"   └── 恢复方式   : 请立即停止自动任务，调用 open_browser_for_manual_use(r\"{user_data_dir}\") 手动过滑块或休息后再试！\n"
            f"{'!' * 76}"
        )
        raise XianyuOfflineError(
            user_data_dir=user_data_dir,
            reason=reason_str,
            error_code="RISK_CONTROL_BLOCKED",
        )

    # 2. 检查是否账号掉线/未登录
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
        raise XianyuOfflineError(
            user_data_dir=user_data_dir,
            reason=reason_str,
            error_code="ACCOUNT_OFFLINE",
        )

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
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="check_login_status")
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
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_selling_items")
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
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="publish")
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
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action=action_name)
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
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_messages")
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
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="reply_message")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="reply_message")


# ==============================================================================
# 六、 免登录公开市场比价检索（搜索关键字商品概览 + 获取指定商品完整详情与多规格价格）
# ==============================================================================
def _verify_guest_no_risk_block(
    page,
    auth_tracker: Dict[str, Any],
    user_data_dir: str,
    task_name: str = "",
) -> None:
    """
    免登录（访客模式）专用风控检测：
    不要求账号登录（允许无 `unb` Cookie、允许存在 `mini_login` 弹窗），
    但一旦检测到触发阿里滑块/限流（`RGV587_ERROR` 或页面出现 `baxia/punish` 滑块验证框），
    立即打印告警并抛出 `XianyuOfflineError(error_code="RISK_CONTROL_BLOCKED")` 中止操作。
    """
    risk_reasons: List[str] = []
    if auth_tracker.get("risk_rets"):
        risk_reasons.append(f"接口返回风控滑块拦截({auth_tracker['risk_rets'][0]})")

    punish_frames = [
        f.url
        for f in page.frames
        if ("punish" in f.url or "baxia" in f.url or "sec.taobao.com" in f.url)
        and "mini_login" not in f.url
    ]
    if punish_frames:
        risk_reasons.append("页面已弹出阿里安全滑块验证框(baxia/punish)")

    if risk_reasons:
        reason_str = "；".join(risk_reasons)
        logger.error(
            f"\n{'!' * 76}\n"
            f"🚨 [风控滑块告警/免登录检索] 检测到闲鱼触发安全验证/滑块限制，已立即中止操作！\n"
            f"   ├── 触发任务   : {task_name or '免登录检索任务'}\n"
            f"   ├── 浏览器目录 : <{user_data_dir}>\n"
            f"   ├── 当前页面   : {page.url}\n"
            f"   ├── 风控证据   : {reason_str}\n"
            f"   └── 恢复方式   : 请暂停检索或调用 open_browser_for_manual_use(r\"{user_data_dir}\") 手动过滑块，或更换新的访客目录！\n"
            f"{'!' * 76}"
        )
        raise XianyuOfflineError(
            user_data_dir=user_data_dir,
            reason=reason_str,
            error_code="RISK_CONTROL_BLOCKED",
        )


def _dismiss_guest_login_modal(page) -> None:
    """点击关闭叉号并移除未登录状态下前端弹出的登录遮罩层与 mini_login 弹窗（不影响页面正常搜索与详情浏览）。"""
    try:
        page.evaluate(
            """() => {
                // 1. 优先尝试点击弹窗右上角的关闭叉号按钮
                document.querySelectorAll(
                    '.ant-modal-close, [class*="closeIcon"], [class*="close-icon"], [class*="modal-close"], .baxia-dialog-close'
                ).forEach(btn => {
                    try { btn.click(); } catch (e) {}
                });
                // 2. 移除常见登录弹窗容器与遮罩层
                document.querySelectorAll(
                    '.ant-modal-root, .ant-modal-mask, .ant-modal-wrap, [class*="login-modal"], [class*="loginModal"], [class*="LoginModal"], [id*="alibaba-login-box"]'
                ).forEach(el => {
                    try { el.remove(); } catch (e) {}
                });
                // 3. 若存在 mini_login / passport.goofish.com 登录 iframe 浮层，连同外层遮罩容器一起移除
                document.querySelectorAll('iframe[src*="mini_login"], iframe[src*="passport.goofish.com"], iframe[src*="login.taobao.com"]').forEach(ifr => {
                    let parent = ifr.parentElement;
                    while (parent && parent !== document.body) {
                        const style = window.getComputedStyle(parent);
                        const cls = String(parent.className || '');
                        if (style.position === 'fixed' || style.position === 'absolute' || cls.includes('modal') || cls.includes('dialog')) {
                            try { parent.remove(); } catch (e) {}
                            break;
                        }
                        parent = parent.parentElement;
                    }
                });
                if (document.body && document.body.style) {
                    document.body.style.overflow = 'auto';
                }
            }"""
        )
    except Exception:
        pass


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

    # 标题与外显描述：优先取 detailParams.title（含换行完整摘要），其次取 ex.title
    title = str(dp.get("title") or ex.get("title") or "").strip()

    # 价格提取：优先取 detailParams.soldPrice，其次拼接 ex.price 数组
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

    # 解析 fishTags（提取 "XX人想要"、是否包邮、卖家信用标签等）
    want_cnt = _parse_int_num(ex.get("wantCnt") or dp.get("wantCnt"), default=0)
    is_free_shipping = "包邮" in title
    credit_tags: List[str] = []

    fish_tags = ex.get("fishTags") or {}
    if isinstance(fish_tags, dict):
        for region_val in fish_tags.values():
            if not isinstance(region_val, dict):
                continue
            tlist = region_val.get("tagList") or []
            for t in tlist:
                if not isinstance(t, dict):
                    continue
                tdata = t.get("data") or {}
                content = str(tdata.get("content") or "").strip()
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
        "item_url": f"https://www.goofish.com/item?id={item_id}",
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

    # 价格：优先使用 priceInCent / price（单位均为分）
    price_cent = sku.get("priceInCent")
    if price_cent is None:
        price_cent = sku.get("price")
    sku_price = round(float(price_cent) / 100.0, 2) if price_cent is not None else 0.0

    specs: Dict[str, str] = {}
    spec_parts: List[str] = []

    # 方式1：从 propertyList 提取规格名和规格值
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

    # 方式2：从 features.idlePvPairs 提取（格式如 "228680331#颜色^0#邮票修狗$100018377#适用型号^0#15pro"）
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

    # 方式3：兜底从 propertyImage.pvPairDO 提取
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
    user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    免登录搜索指定关键字的闲鱼商品概览信息：
    - 使用指定的 `user_data_dir`（默认 `W:\\temp\\xianyu_guest`，无需登录任何闲鱼账号）；
    - 自动访问首页完成匿名 `_m_h5_tk` 令牌握手并移除登录遮罩，随后触发关键字搜索；
    - 拦截并解析 `mtop.taobao.idlemtopsearch.pc.search` 接口返回的商品列表（单次最多 30 条），
      提取每个商品的 `item_id`、`title`（含列表外显描述）、`price`、`original_price`、`want_cnt`（想要人数）、
      `is_free_shipping`（是否包邮）、`area`、`seller_nick`、`seller_tags`、`image_url` 等概览数据。
    """
    kw_clean = str(keyword or "").strip()
    if not kw_clean:
        raise ValueError("search_xianyu_items: 搜索关键字 keyword 不能为空")

    limit = max(1, int(max_items or 30))
    tag = _dir_tag(user_data_dir)
    t0 = time.monotonic()

    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            debug=debug,
            task_name=f"search_xianyu_items:{kw_clean[:20]}",
            lock_timeout_sec=lock_timeout_sec,
        ) as (context, page, auth_tracker):
            search_packets: List[Dict[str, Any]] = []

            def _bind_search_listener(target_page) -> None:
                def _on_response(resp):
                    if "mtop.taobao.idlemtopsearch.pc.search/" in resp.url:
                        try:
                            body = resp.json()
                            if isinstance(body, dict):
                                search_packets.append(body)
                        except Exception:
                            pass

                target_page.on("response", _on_response)

            _bind_search_listener(page)
            context.on("page", _bind_search_listener)

            # 1. 打开闲鱼首页初始化/刷新匿名 _m_h5_tk 令牌
            page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            for _ in range(15):
                cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
                if cookies.get("_m_h5_tk"):
                    break
                page.wait_for_timeout(200)
            page.wait_for_timeout(1000)

            _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name="search_xianyu_items")
            _dismiss_guest_login_modal(page)

            # 2. 在首页搜索框聚焦填入关键字（强校验 input_value 防止被默认热词占位符顶替）并点击“搜索”按钮
            search_packets.clear()
            search_input = page.locator("input[class*='search-input'], input[type='text']").first
            input_ready = False
            if search_input.count() > 0:
                for _ in range(3):
                    _dismiss_guest_login_modal(page)
                    try:
                        search_input.click(timeout=3000)
                        search_input.fill(kw_clean, timeout=3000)
                    except Exception:
                        _dismiss_guest_login_modal(page)
                        continue
                    page.wait_for_timeout(150)
                    if search_input.input_value().strip() == kw_clean:
                        input_ready = True
                        break
            if input_ready:
                _dismiss_guest_login_modal(page)
                search_btn = page.locator("button[class*='search-icon'], button:has-text('搜索')").first
                try:
                    if search_btn.count() > 0:
                        search_btn.click(timeout=3000)
                    else:
                        search_input.press("Enter", timeout=3000)
                except Exception:
                    _dismiss_guest_login_modal(page)
                    page.goto(f"{GOOFISH_HOME_URL}/search?q={quote(kw_clean)}", wait_until="domcontentloaded", timeout=30000)
            else:
                page.goto(f"{GOOFISH_HOME_URL}/search?q={quote(kw_clean)}", wait_until="domcontentloaded", timeout=30000)

            # 3. 等待搜索 MTOP 数据包返回（最长等待 8 秒）
            success_packet: Optional[Dict[str, Any]] = None
            for _ in range(32):
                for pkt in reversed(search_packets):
                    ret_str = " | ".join(str(x) for x in (pkt.get("ret") or []))
                    if "SUCCESS" in ret_str:
                        success_packet = pkt
                        break
                if success_packet is not None or auth_tracker.get("risk_rets"):
                    break
                page.wait_for_timeout(250)

            active_page = context.pages[-1] if context.pages else page
            _verify_guest_no_risk_block(active_page, auth_tracker, user_data_dir, task_name="search_xianyu_items")

            if success_packet is None:
                elapsed = time.monotonic() - t0
                logger.warning(
                    f"⚠️ [免登录搜索/未捕获有效数据包] 目录={tag} | 关键字=【{kw_clean}】 | "
                    f"已捕获包数={len(search_packets)} | 失败接口={auth_tracker.get('failed_apis')} | 耗时={elapsed:.2f}s"
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
                    "message": f"未捕获到关键字【{kw_clean}】的有效闲鱼搜索数据包",
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
                f"[免登录搜索/完成] ✅ 目录={tag} | 关键字=【{kw_clean}】 | 返回商品数={len(parsed_items)} | "
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
                "price_summary": {
                    "min_price": min_p,
                    "median_price": med_p,
                    "max_price": max_p,
                },
                "items": parsed_items,
            }
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="search_xianyu_items")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="search_xianyu_items")


def get_xianyu_item_detail(
    item_id_or_url: str,
    user_data_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    免登录获取指定闲鱼商品的完整详情（含完整文字描述 + 多规格 SKU 及对应价格明细）：
    - 支持直接传入商品数字 ID（如 `"1069653908592"`）或商品详情页链接（如 `"https://www.goofish.com/item?id=1069653908592"`）；
    - 使用指定的 `user_data_dir`（默认 `W:\\temp\\xianyu_guest`，无需登录任何闲鱼账号）；
    - 自动校验/初始化匿名 `_m_h5_tk` 令牌并拦截 `mtop.taobao.idle.pc.detail` 响应；
    - 返回完整文字描述 (`desc`)、售价 (`price`)、运费 (`transport_fee`)、想要人数 (`want_cnt`)、浏览量 (`browse_cnt`)，
      以及当商品存在多规格时完整的 `sku_list`（每个规格的名称 `spec_text`、结构化属性 `specs`、对应价格 `price`、库存 `quantity`）。
    """
    raw_target = str(item_id_or_url or "").strip()
    if not raw_target:
        raise ValueError("get_xianyu_item_detail: item_id_or_url 不能为空")

    id_match = re.search(r"[?&]id=(\d+)", raw_target)
    if id_match:
        item_id = id_match.group(1)
    else:
        digits_match = re.search(r"(\d{8,})", raw_target)
        item_id = digits_match.group(1) if digits_match else raw_target

    detail_url = f"{GOOFISH_HOME_URL}/item?id={item_id}"
    tag = _dir_tag(user_data_dir)
    t0 = time.monotonic()

    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            debug=debug,
            task_name=f"get_xianyu_item_detail:{item_id}",
            lock_timeout_sec=lock_timeout_sec,
        ) as (context, page, auth_tracker):
            detail_packets: List[Dict[str, Any]] = []

            def _on_response(resp):
                if "mtop.taobao.idle.pc.detail/" in resp.url:
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            detail_packets.append(body)
                    except Exception:
                        pass

            page.on("response", _on_response)

            # 1. 若当前浏览器目录尚未生成匿名 _m_h5_tk 令牌，先访问首页初始化
            cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
            if not cookies.get("_m_h5_tk"):
                page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                for _ in range(15):
                    cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
                    if cookies.get("_m_h5_tk"):
                        break
                    page.wait_for_timeout(200)
                page.wait_for_timeout(600)
                _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name="get_xianyu_item_detail")
                _dismiss_guest_login_modal(page)

            # 2. 访问目标商品详情页并等待 mtop.taobao.idle.pc.detail 响应
            success_packet: Optional[Dict[str, Any]] = None
            for attempt in range(2):
                detail_packets.clear()
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                _dismiss_guest_login_modal(page)
                for _ in range(32):
                    for pkt in reversed(detail_packets):
                        ret_str = " | ".join(str(x) for x in (pkt.get("ret") or []))
                        if "SUCCESS" in ret_str and isinstance((pkt.get("data") or {}).get("itemDO"), dict):
                            success_packet = pkt
                            break
                    if success_packet is not None or auth_tracker.get("risk_rets"):
                        break
                    page.wait_for_timeout(200)

                _dismiss_guest_login_modal(page)
                _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name="get_xianyu_item_detail")
                if success_packet is not None:
                    break

                # 若首次因匿名令牌过期未拿到数据包，回首页刷新一次匿名令牌后重试
                if attempt == 0:
                    page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)
                    _dismiss_guest_login_modal(page)
                    _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name="get_xianyu_item_detail")

            if success_packet is None:
                elapsed = time.monotonic() - t0
                logger.warning(
                    f"⚠️ [免登录详情/未捕获有效数据包] 目录={tag} | 商品ID={item_id} | "
                    f"已捕获包数={len(detail_packets)} | 失败接口={auth_tracker.get('failed_apis')} | 耗时={elapsed:.2f}s"
                )
                return {
                    "ok": False,
                    "is_offline": False,
                    "is_risk_blocked": False,
                    "action": "get_xianyu_item_detail",
                    "error_code": "DETAIL_PACKET_EMPTY",
                    "user_data_dir": user_data_dir,
                    "item_id": item_id,
                    "item_url": detail_url,
                    "message": f"未捕获到商品 ({item_id}) 的有效详情数据包",
                }

            parsed_detail = _parse_detail_packet_to_dict(
                success_packet=success_packet,
                item_id=item_id,
                detail_url=detail_url,
                user_data_dir=user_data_dir,
            )
            elapsed = time.monotonic() - t0
            logger.info(
                f"[免登录详情/完成] ✅ 目录={tag} | 商品ID={item_id} | 售价=¥{parsed_detail['price']} "
                f"(区间=¥{parsed_detail['min_price']}~¥{parsed_detail['max_price']}) | "
                f"多规格数={len(parsed_detail['sku_list'])} | 想要={parsed_detail['want_cnt']} | "
                f"浏览={parsed_detail['browse_cnt']} | 耗时={elapsed:.2f}s"
            )
            return parsed_detail
    except XianyuOfflineError as e:
        return _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_xianyu_item_detail")
    except XianyuLockTimeoutError as e:
        return _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_xianyu_item_detail")


def _parse_detail_packet_to_dict(
    success_packet: Dict[str, Any],
    item_id: str,
    detail_url: str,
    user_data_dir: str,
) -> Dict[str, Any]:
    """将捕获到的 `mtop.taobao.idle.pc.detail` 成功响应解析为标准商品详情字典。"""
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
) -> Dict[str, Any]:
    """
    在同一个浏览器会话内批量获取多个闲鱼同款候选商品的完整详情（含完整描述、多规格 SKU 价格、库存与运费）：
    - 避免每个商品重复启动/关闭 Chromium 进程，大幅缩短批量拉取耗时；
    - 每次进入详情页自动点击叉号并清理未登录弹窗遮罩（`_dismiss_guest_login_modal`）；
    - 返回字典包含 `ok`、`is_risk_blocked`、`details_by_id`（`{item_id: detail_dict}`）。
    """
    clean_ids: List[str] = []
    for raw_target in item_ids or []:
        s = str(raw_target or "").strip()
        if not s:
            continue
        id_match = re.search(r"[?&]id=(\d+)", s)
        if id_match:
            cid = id_match.group(1)
        else:
            digits_match = re.search(r"(\d{8,})", s)
            cid = digits_match.group(1) if digits_match else s
        if cid and cid not in clean_ids:
            clean_ids.append(cid)

    if not clean_ids:
        return {"ok": True, "is_risk_blocked": False, "details_by_id": {}}

    tag = _dir_tag(user_data_dir)
    t0 = time.monotonic()
    details_by_id: Dict[str, Dict[str, Any]] = {}

    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            debug=debug,
            task_name=f"get_xianyu_item_details_batch:{len(clean_ids)}items",
            lock_timeout_sec=lock_timeout_sec,
        ) as (context, page, auth_tracker):
            detail_packets: List[Dict[str, Any]] = []

            def _on_response(resp):
                if "mtop.taobao.idle.pc.detail/" in resp.url:
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            detail_packets.append(body)
                    except Exception:
                        pass

            page.on("response", _on_response)

            cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
            if not cookies.get("_m_h5_tk"):
                page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                for _ in range(15):
                    cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
                    if cookies.get("_m_h5_tk"):
                        break
                    page.wait_for_timeout(200)
                page.wait_for_timeout(500)
                _dismiss_guest_login_modal(page)
                _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name="get_xianyu_item_details_batch")

            for idx, item_id in enumerate(clean_ids):
                t_item = time.monotonic()
                detail_url = f"{GOOFISH_HOME_URL}/item?id={item_id}"
                success_packet: Optional[Dict[str, Any]] = None

                for attempt in range(2):
                    detail_packets.clear()
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                    _dismiss_guest_login_modal(page)
                    for _ in range(28):
                        for pkt in reversed(detail_packets):
                            ret_str = " | ".join(str(x) for x in (pkt.get("ret") or []))
                            if "SUCCESS" in ret_str and isinstance((pkt.get("data") or {}).get("itemDO"), dict):
                                success_packet = pkt
                                break
                        if success_packet is not None or auth_tracker.get("risk_rets"):
                            break
                        page.wait_for_timeout(200)

                    _dismiss_guest_login_modal(page)
                    _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name=f"batch_detail:{item_id}")
                    if success_packet is not None:
                        break

                    if attempt == 0:
                        page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(1000)
                        _dismiss_guest_login_modal(page)
                        _verify_guest_no_risk_block(page, auth_tracker, user_data_dir, task_name=f"batch_detail:{item_id}")

                if success_packet is not None:
                    parsed_detail = _parse_detail_packet_to_dict(
                        success_packet=success_packet,
                        item_id=item_id,
                        detail_url=detail_url,
                        user_data_dir=user_data_dir,
                    )
                    details_by_id[item_id] = parsed_detail
                    logger.info(
                        f"[免登录批量详情/{idx + 1}/{len(clean_ids)}] ✅ 目录={tag} | 商品ID={item_id} | "
                        f"售价=¥{parsed_detail['price']} | 运费=¥{parsed_detail['transport_fee']} | "
                        f"SKU数={len(parsed_detail['sku_list'])} | 耗时={time.monotonic() - t_item:.2f}s"
                    )
                else:
                    details_by_id[item_id] = {
                        "ok": False,
                        "is_offline": False,
                        "is_risk_blocked": False,
                        "action": "get_xianyu_item_detail",
                        "error_code": "DETAIL_PACKET_EMPTY",
                        "user_data_dir": user_data_dir,
                        "item_id": item_id,
                        "item_url": detail_url,
                        "message": f"未捕获到商品 ({item_id}) 的有效详情数据包",
                    }

                if idx < len(clean_ids) - 1:
                    page.wait_for_timeout(350)

            logger.info(
                f"[免登录批量详情/完成] 📊 目录={tag} | 成功={sum(1 for v in details_by_id.values() if v.get('ok'))}/{len(clean_ids)} | "
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
    except XianyuOfflineError as e:
        res = _build_offline_result(user_data_dir, e.reason, error_code=e.error_code, action="get_xianyu_item_details_batch")
        res["details_by_id"] = details_by_id
        return res
    except XianyuLockTimeoutError as e:
        res = _build_lock_timeout_result(user_data_dir, e.timeout_sec, action="get_xianyu_item_details_batch")
        res["details_by_id"] = details_by_id
        return res



# ==============================================================================
#                                  函数调用示例
# ==============================================================================
if __name__ == "__main__":
    USER_DATA_DIR = r"W:\temp\xianyu_myself"
    GUEST_USER_DATA_DIR = r"W:\temp\xianyu_guest"

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

    # # 场景五：纯消息监控（获取谁发了信息、何时发的、发了什么内容、明确谈论的是哪件商品）
    # msg_res = get_messages(
    #     user_data_dir=USER_DATA_DIR,
    #     only_unread=False,
    #     only_my_items=False,
    #     max_sessions=3,
    #     debug=False,
    # )
    # print("消息监控结果:", json.dumps(msg_res, ensure_ascii=False, indent=2))

    # # 场景六：根据商品与买家定制话术并回复消息（支持可选传入 item_id 精准校验会话商品）
    # reply_res = reply_message(
    #     target_user="昵称未填写",
    #     item_id="1094773946145",
    #     reply_text="您好，在的，可以直接拍下~",
    #     user_data_dir=USER_DATA_DIR,
    #     debug=False,
    # )
    # print("消息回复结果:", json.dumps(reply_res, ensure_ascii=False, indent=2))

    # 场景七：免登录搜索指定关键字的闲鱼商品概览 + 获取指定商品完整详情（含完整描述与多规格SKU价格）
    search_res = search_xianyu_items(
        keyword="可口可乐 330ml 24罐",
        max_items=5,
        user_data_dir=GUEST_USER_DATA_DIR,
        debug=False,
    )
    print("免登录搜索结果:", json.dumps(search_res, ensure_ascii=False, indent=2))

    if search_res.get("ok") and search_res.get("items"):
        first_id = search_res["items"][0]["item_id"]
        detail_res = get_xianyu_item_detail(
            item_id_or_url=first_id,
            user_data_dir=GUEST_USER_DATA_DIR,
            debug=False,
        )
        print("免登录商品详情:", json.dumps(detail_res, ensure_ascii=False, indent=2))

