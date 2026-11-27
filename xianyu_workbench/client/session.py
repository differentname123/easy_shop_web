# -*- coding: utf-8 -*-
r"""
闲鱼 (Goofish) Playwright 会话生命周期、跨进程/跨线程串行排队锁、掉线/风控识别与轮次复用池。
=========================================================================================
[设计原则] 第一性原理，越简单越可靠：
    1. 独立浏览器目录（默认可通过 XIANYU_*_USER_DATA_DIR 环境变量覆写）原生持久化多账号登录态。
    2. 底层内置「跨进程 + 跨线程」串行排队锁与最长等待超时（默认 lock_timeout_sec=120s）。
    3. 精准识别「账号掉线/未登录」与「风控滑块拦截」，快速明确报错而不死等。
    4. 库模块严禁在 import 时配置 logging.basicConfig 或写入日志文件。
=========================================================================================
"""

from contextlib import ExitStack, contextmanager
import gc
import logging
import os
import random
import shutil
import tempfile
import threading
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl
from typing import Any, Dict, Generator, List, Optional, Tuple

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

logger = logging.getLogger("publish_to_xianyu")

# ==============================================================================
# 全局默认配置与串行锁配置
# ==============================================================================
DEFAULT_USER_DATA_DIR = os.environ.get("XIANYU_USER_DATA_DIR", "").strip() or r"W:\temp\xianyu_myself"
DEFAULT_SEARCH_USER_DATA_DIR = os.environ.get("XIANYU_SEARCH_USER_DATA_DIR", "").strip() or r"W:\temp\xianyu_xiaohao"
DEFAULT_GUEST_USER_DATA_DIR = os.environ.get("XIANYU_GUEST_USER_DATA_DIR", "").strip() or r"W:\temp\xianyu_guest"
GOOFISH_HOME_URL = "https://www.goofish.com"
GOOFISH_H5_HOME_URL = "https://h5.m.goofish.com"
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
    "mtop.taobao.idlemtopsearch.pc.search/",
)

# H5 免登录详情核心接口：在访客模式下，一旦详情接口返回 SESSION_EXPIRED / 请先登录 等错误码，即代表闲鱼已限制当前 IP 的匿名访问
_GUEST_CORE_DATA_APIS: Tuple[str, ...] = (
    "mtop.taobao.idle.awesome.detail/",
    "mtop.taobao.idle.pc.detail/",
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
    is_guest: bool = False,
) -> Dict[str, Any]:
    """统一构造掉线或风控滑块中断返回结构，方便上层立即停止重试。"""
    is_risk = error_code == "RISK_CONTROL_BLOCKED"
    if is_guest:
        msg = (
            f"H5免登录详情接口触发闲鱼限流保护 ({reason})，"
            f"已立即中止本次拉取并进入静默冷却保护（纯内存访客会话无需人工打开浏览器目录）"
        )
    else:
        msg = (
            f"闲鱼账号{'触发风控滑块验证' if is_risk else '处于未登录或掉线状态'} ({reason})，"
            f"请暂停自动重试并调用 open_browser_for_manual_use(r'{user_data_dir}') 人工处理"
        )
    res: Dict[str, Any] = {
        "ok": False,
        "is_offline": False if is_guest else True,
        "is_risk_blocked": is_risk,
        "error_code": error_code,
        "user_data_dir": user_data_dir,
        "message": msg,
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
        fd = os.open(_GLOBAL_LOCK_FILE_PATH, os.O_CREAT | os.O_RDWR)
        lock_file = os.fdopen(fd, "r+b")
        if os.fstat(lock_file.fileno()).st_size == 0:
            lock_file.write(b"0")
            lock_file.flush()

        # 2. 跨进程 OS 内核级文件锁排队
        while True:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
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


DEFAULT_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)
DEFAULT_MOBILE_H5_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Mobile Safari/537.36"
)
DEFAULT_HEADLESS_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_MOBILE_H5_VIEWPORT = {"width": 390, "height": 844}


def launch_persistent_context(
    p,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    args: Optional[List[str]] = None,
    hide_automation: bool = True,
    headless: bool = False,
    mobile_h5: bool = False,
):
    """启动挂载独立浏览器目录的持久化 Chrome 上下文（支持桌面端 PC 模式与移动端 H5 免登录分享页模式）。"""
    os.makedirs(user_data_dir, exist_ok=True)
    default_args = list(args or ["--disable-blink-features=AutomationControlled"])
    if headless and "--headless=new" not in default_args:
        default_args.append("--headless=new")

    kwargs: Dict[str, Any] = {
        "channel": "chrome",
        "user_data_dir": user_data_dir,
        "headless": headless,
        "args": default_args,
    }
    if mobile_h5:
        kwargs["viewport"] = dict(DEFAULT_MOBILE_H5_VIEWPORT)
        kwargs["user_agent"] = DEFAULT_MOBILE_H5_USER_AGENT
        kwargs["is_mobile"] = True
        kwargs["has_touch"] = True
    elif headless:
        kwargs["viewport"] = dict(DEFAULT_HEADLESS_VIEWPORT)
        kwargs["user_agent"] = DEFAULT_DESKTOP_USER_AGENT
    else:
        default_args.extend(["--start-maximized", "--window-position=0,0"])
        kwargs["no_viewport"] = True

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
        "guest_login_blocked_rets": [],
        "failed_apis": [],
    }

    expired_by_api: Dict[str, str] = {}

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

        if any(api in url for api in _GUEST_CORE_DATA_APIS):
            for kw in _OFFLINE_RET_KEYWORDS:
                if kw in ret_str:
                    entry = f"{api_short} -> {ret_str}"
                    if entry not in tracker["guest_login_blocked_rets"]:
                        tracker["guest_login_blocked_rets"].append(entry)
                    break

        if any(api in url for api in _AUTH_CRITICAL_APIS):
            if "SUCCESS" in ret_str:
                expired_by_api.pop(api_short, None)
            else:
                for kw in _OFFLINE_RET_KEYWORDS:
                    if kw in ret_str:
                        expired_by_api[api_short] = f"{api_short} -> {ret_str}"
                        break

        data = body.get("data") or {}
        if "mtop.taobao.idlemessage.pc.loginuser.get" in url and data.get("userId"):
            tracker["user_id"] = str(data["userId"])
            expired_by_api.clear()
        elif "mtop.idle.web.user.page.nav" in url:
            base = ((data.get("module") or {}).get("base")) or {}
            if base.get("displayName"):
                tracker["display_name"] = str(base["displayName"])
                expired_by_api.clear()

        tracker["expired_rets"][:] = list(expired_by_api.values())

    page.on("response", _on_response)
    tracker["_on_response_cb"] = _on_response
    return tracker


def _extract_risk_reasons(page, auth_tracker: Dict[str, Any]) -> List[str]:
    """检测是否触发阿里滑块/验证码拦截（MTOP 返回码或 baxia/punish 验证框）。"""
    reasons: List[str] = []
    if auth_tracker.get("risk_rets"):
        reasons.append(f"接口返回风控滑块拦截({auth_tracker['risk_rets'][0]})")
    punish_frames = [
        f.url
        for f in page.frames
        if ("punish" in f.url or "baxia" in f.url or "sec.taobao.com" in f.url)
        and "mini_login" not in f.url
    ]
    if punish_frames:
        reasons.append("页面已弹出阿里安全滑块验证框(baxia/punish)")
    return reasons


def _verify_account_online(
    context,
    page,
    auth_tracker: Dict[str, Any],
    user_data_dir: str,
    task_name: str = "",
) -> str:
    """
    严格校验当前浏览器目录中的闲鱼账号在线状态：
    - 兼容浏览器冷启动时的 Havana `sdkSilent` 静默换取 `cookie2` 会话过程（约 1~2.5s）；
    - 若未扫码登录、静默续期失败真掉线或触发滑块验证，立即抛出 XianyuOfflineError。
    """
    def _read_cookies() -> Dict[str, str]:
        return {c["name"]: c["value"] for c in context.cookies() if c.get("name") and c.get("value")}

    def _has_login_frame() -> bool:
        return any("passport.goofish.com/mini_login" in f.url or "login.taobao.com" in f.url for f in page.frames)

    cookies = _read_cookies()
    has_persistent_sso = bool(
        cookies.get("unb") or cookies.get("havana_lgc2_77") or cookies.get("_hvn_lgc_") or auth_tracker.get("user_id")
    )
    # 浏览器刚启动时 cookie2（会话级 Cookie）尚未生成，闲鱼前端会通过 havana_lgc2_77 静默换取 cookie2 并重发 loginuser.get
    if has_persistent_sso and (not cookies.get("cookie2") or auth_tracker.get("expired_rets") or _has_login_frame()):
        for _ in range(25):
            if _extract_risk_reasons(page, auth_tracker):
                break
            page.wait_for_timeout(200)
            cookies = _read_cookies()
            if cookies.get("cookie2") and not auth_tracker.get("expired_rets") and not _has_login_frame():
                break

    risk_reasons = _extract_risk_reasons(page, auth_tracker)
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

    unb = str(cookies.get("unb") or auth_tracker.get("user_id") or "").strip()

    offline_reasons: List[str] = []
    if auth_tracker.get("expired_rets"):
        offline_reasons.append(f"接口返回会话过期({auth_tracker['expired_rets'][0]})")
    if _has_login_frame():
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


def _format_failure_diag(context: Any, page: Any, auth_tracker: Dict[str, Any], *, headless: bool = True) -> str:
    """在浏览器请求未拿到预期数据包时，生成简洁单行现场诊断摘要（URL、令牌状态与最近失败接口）。"""
    cur_url = ""
    has_tk = False
    try:
        cur_url = str(page.url or "")
        cookies = {c["name"]: c["value"] for c in context.cookies() if c.get("name")}
        has_tk = bool(cookies.get("_m_h5_tk"))
    except Exception:
        pass
    failed_apis = list(dict.fromkeys(auth_tracker.get("failed_apis") or []))[-3:]
    return (
        f"模式={'headless' if headless else 'headed'} | "
        f"令牌_m_h5_tk={'已获取' if has_tk else '缺失'} | "
        f"URL={cur_url or '-'} | "
        f"失败MTOP={failed_apis if failed_apis else '无'}"
    )


def _detach_listener_safely(emitter: Any, event: str, callback: Any) -> None:
    if emitter is None or not callable(callback):
        return
    remove_fn = getattr(emitter, "remove_listener", None)
    if callable(remove_fn):
        try:
            remove_fn(event, callback)
        except Exception:
            pass


def _detach_tracker_from_context(context: Any, auth_tracker: Optional[Dict[str, Any]], on_page_cb: Any = None) -> None:
    if context is None:
        return
    if callable(on_page_cb):
        _detach_listener_safely(context, "page", on_page_cb)
    resp_cb = (auth_tracker or {}).get("_on_response_cb")
    if callable(resp_cb):
        for p_item in list(getattr(context, "pages", None) or []):
            _detach_listener_safely(p_item, "response", resp_cb)


@contextmanager
def _browser_session_guard(
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    *,
    task_name: str = "browser_task",
    debug: bool = False,
    mobile_h5: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Generator[Tuple[Any, Any, Dict[str, Any]], None, None]:
    """
    统一浏览器自动化执行上下文：
    - 自动获取「跨线程 + 跨进程」串行锁（最长等待 `lock_timeout_sec` 秒）；
    - 默认（`debug=False`）以真无头模式（`headless=True`）静默运行，不弹任何浏览器窗口；`debug=True` 时打开可见窗口；
    - 启动持久化 Chrome 上下文并设置全局最大操作超时（`operation_timeout_sec` 秒），防止页面卡死；
    - 挂载 MTOP 登录态与错误监听器；
    - 退出时确保卸载监听器、关闭浏览器并释放串行锁。
    """
    op_timeout_ms = int(max(10.0, float(operation_timeout_sec or DEFAULT_OPERATION_TIMEOUT_SEC)) * 1000)
    use_headless = not bool(debug)
    with _acquire_serial_browser_lock(
        user_data_dir=user_data_dir,
        task_name=task_name,
        lock_timeout_sec=lock_timeout_sec,
    ):
        with sync_playwright() as p:
            context = None
            auth_tracker: Optional[Dict[str, Any]] = None
            on_page_cb = None
            try:
                context = launch_persistent_context(
                    p,
                    user_data_dir=user_data_dir,
                    headless=use_headless,
                    mobile_h5=mobile_h5,
                )
                context.set_default_timeout(op_timeout_ms)
                context.set_default_navigation_timeout(op_timeout_ms)
                page = context.pages[0] if context.pages else context.new_page()
                auth_tracker = _attach_auth_tracker(page)
                if hasattr(context, "on") and callable(auth_tracker.get("_on_response_cb")):
                    def _on_new_page(new_p):
                        new_p.on("response", auth_tracker["_on_response_cb"])
                    on_page_cb = _on_new_page
                    context.on("page", on_page_cb)
                yield context, page, auth_tracker
            finally:
                _detach_tracker_from_context(context, auth_tracker, on_page_cb)
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
        gc.collect()


@contextmanager
def _ephemeral_h5_session_guard(
    lock_key_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
    *,
    task_name: str = "ephemeral_h5_task",
    debug: bool = False,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
) -> Generator[Tuple[Any, Any, Dict[str, Any]], None, None]:
    """
    纯内存无痕移动端 H5 会话上下文（零持久化脏 Cookie 累积）：
    - 仍通过 `lock_key_dir` 获取跨线程/跨进程串行锁，防止本机多进程并发挤压同一出口 IP；
    - 使用 `p.chromium.launch(...)` + `browser.new_context(...)` 在内存中动态换取新鲜 `_m_h5_tk`，
      绝不读写磁盘 `Cookies` 数据库，彻底根除跨轮次 `cna` / `x5secdata` 计数累积触发 `RGV587_ERROR`；
    - 退出时自动卸载监听器并销毁内存上下文与浏览器实例。
    """
    op_timeout_ms = int(max(10.0, float(operation_timeout_sec or DEFAULT_OPERATION_TIMEOUT_SEC)) * 1000)
    use_headless = not bool(debug)
    launch_args = ["--disable-blink-features=AutomationControlled"]
    if use_headless:
        launch_args.append("--headless=new")

    with _acquire_serial_browser_lock(
        user_data_dir=lock_key_dir,
        task_name=task_name,
        lock_timeout_sec=lock_timeout_sec,
    ):
        with sync_playwright() as p:
            browser = None
            context = None
            auth_tracker: Optional[Dict[str, Any]] = None
            on_page_cb = None
            try:
                browser = p.chromium.launch(
                    channel="chrome",
                    headless=use_headless,
                    args=launch_args,
                    ignore_default_args=["--enable-automation"],
                )
                context = browser.new_context(
                    viewport=dict(DEFAULT_MOBILE_H5_VIEWPORT),
                    user_agent=DEFAULT_MOBILE_H5_USER_AGENT,
                    is_mobile=True,
                    has_touch=True,
                )
                context.set_default_timeout(op_timeout_ms)
                context.set_default_navigation_timeout(op_timeout_ms)
                page = context.new_page()
                auth_tracker = _attach_auth_tracker(page)
                if hasattr(context, "on") and callable(auth_tracker.get("_on_response_cb")):
                    def _on_new_page(new_p):
                        new_p.on("response", auth_tracker["_on_response_cb"])
                    on_page_cb = _on_new_page
                    context.on("page", on_page_cb)
                yield context, page, auth_tracker
            finally:
                _detach_tracker_from_context(context, auth_tracker, on_page_cb)
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
        gc.collect()


class XianyuRoundBrowserPool:
    """
    单轮任务级惰性浏览器会话复用池：
    - 惰性初始化：若本轮无真实搜索/详情请求（如单元测试 Mock 或全部命中缓存），零进程开销；
    - 搜索上下文单轮复用：复用同一个已登录 `xianyu_xiaohao` 持久化上下文，避免每搜 1 个词就冷启动 Chrome；
    - 无痕 H5 详情会话自然复用 + 定时轮换：同一内存 H5 会话先在首页自然预热一次 `_m_h5_tk`，
      最多连续查看 `max_h5_items_per_context`（默认 10）件商品后自动轮换全新内存上下文，兼顾人类访问轨迹与防累积风控；
    - 退出时强制卸载所有监听器、关闭全部上下文与 Playwright 进程并执行 GC，彻底根除句柄累积。
    """

    def __init__(
        self,
        *,
        max_h5_items_per_context: int = 10,
        search_min_interval_sec: float = 2.8,
        h5_detail_min_interval_sec: float = 4.2,
        operation_timeout_sec: float = DEFAULT_OPERATION_TIMEOUT_SEC,
    ) -> None:
        self.max_h5_items_per_context = max(2, int(max_h5_items_per_context))
        self.search_min_interval_sec = max(0.5, float(search_min_interval_sec))
        self.h5_detail_min_interval_sec = max(0.5, float(h5_detail_min_interval_sec))
        self.operation_timeout_sec = max(10.0, float(operation_timeout_sec or DEFAULT_OPERATION_TIMEOUT_SEC))

        self._pw_cm: Any = None
        self._pw: Any = None
        self._lock_stack = ExitStack()
        self._global_lock_held: bool = False

        self._search_dir: str = ""
        self._search_ctx: Any = None
        self._search_page: Any = None
        self._search_tracker: Optional[Dict[str, Any]] = None
        self._search_on_page_cb: Any = None
        self._last_search_ts: float = 0.0

        self._h5_browser: Any = None
        self._h5_ctx: Any = None
        self._h5_page: Any = None
        self._h5_tracker: Optional[Dict[str, Any]] = None
        self._h5_on_page_cb: Any = None
        self._h5_items_served: int = 0
        self._last_h5_ts: float = 0.0

    def __enter__(self) -> "XianyuRoundBrowserPool":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _ensure_playwright(self) -> Any:
        if self._pw is None:
            self._pw_cm = sync_playwright()
            self._pw = self._pw_cm.__enter__()
        return self._pw

    def _ensure_dir_lock(self, user_data_dir: str, task_name: str, lock_timeout_sec: float) -> None:
        if not self._global_lock_held:
            self._lock_stack.enter_context(
                _acquire_serial_browser_lock(
                    user_data_dir=user_data_dir,
                    task_name=task_name,
                    lock_timeout_sec=lock_timeout_sec,
                )
            )
            self._global_lock_held = True

    def _close_h5_context_only(self) -> None:
        _detach_tracker_from_context(self._h5_ctx, self._h5_tracker, self._h5_on_page_cb)
        if self._h5_ctx is not None:
            try:
                self._h5_ctx.close()
            except Exception:
                pass
        self._h5_ctx = None
        self._h5_page = None
        self._h5_tracker = None
        self._h5_on_page_cb = None
        self._h5_items_served = 0

    def _close_search_context_only(self) -> None:
        _detach_tracker_from_context(self._search_ctx, self._search_tracker, self._search_on_page_cb)
        if self._search_ctx is not None:
            try:
                self._search_ctx.close()
            except Exception:
                pass
        self._search_ctx = None
        self._search_page = None
        self._search_tracker = None
        self._search_on_page_cb = None
        self._search_dir = ""

    @contextmanager
    def acquire_search_session(
        self,
        user_data_dir: str = DEFAULT_SEARCH_USER_DATA_DIR,
        *,
        debug: bool = False,
        lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
        task_name: str = "round_search",
    ) -> Generator[Tuple[Any, Any, Dict[str, Any]], None, None]:
        self._ensure_dir_lock(user_data_dir, task_name, lock_timeout_sec)
        p = self._ensure_playwright()
        op_timeout_ms = int(self.operation_timeout_sec * 1000)
        use_headless = not bool(debug)

        if self._search_ctx is not None and self._search_dir != user_data_dir:
            self._close_search_context_only()

        if self._search_ctx is None:
            ctx = launch_persistent_context(
                p,
                user_data_dir=user_data_dir,
                headless=use_headless,
                mobile_h5=False,
            )
            ctx.set_default_timeout(op_timeout_ms)
            ctx.set_default_navigation_timeout(op_timeout_ms)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            tracker = _attach_auth_tracker(page)

            def _on_new_page(new_p):
                new_p.on("response", tracker["_on_response_cb"])

            if hasattr(ctx, "on"):
                ctx.on("page", _on_new_page)
            self._search_ctx = ctx
            self._search_page = page
            self._search_tracker = tracker
            self._search_on_page_cb = _on_new_page
            self._search_dir = user_data_dir
        else:
            gap = (self._last_search_ts + self.search_min_interval_sec + random.uniform(0.0, 1.4)) - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            # 清理上一轮搜索可能打开的多余新标签页，防止 Page 句柄堆积
            pages = list(self._search_ctx.pages or [])
            while len(pages) > 1:
                extra_p = pages.pop()
                if extra_p is not self._search_page:
                    _detach_listener_safely(extra_p, "response", (self._search_tracker or {}).get("_on_response_cb"))
                    try:
                        extra_p.close()
                    except Exception:
                        pass
            if self._search_tracker is not None:
                self._search_tracker["failed_apis"].clear()

        try:
            yield self._search_ctx, self._search_page, self._search_tracker
        except Exception:
            self._close_search_context_only()
            raise
        finally:
            self._last_search_ts = time.monotonic()

    @contextmanager
    def acquire_h5_detail_session(
        self,
        lock_key_dir: str = DEFAULT_GUEST_USER_DATA_DIR,
        *,
        debug: bool = False,
        lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
        task_name: str = "round_h5_detail",
        item_count: int = 1,
    ) -> Generator[Tuple[Any, Any, Dict[str, Any]], None, None]:
        self._ensure_dir_lock(lock_key_dir, task_name, lock_timeout_sec)
        p = self._ensure_playwright()
        op_timeout_ms = int(self.operation_timeout_sec * 1000)
        use_headless = not bool(debug)

        if self._h5_browser is not None:
            try:
                if hasattr(self._h5_browser, "is_connected") and not self._h5_browser.is_connected():
                    self._close_h5_context_only()
                    self._h5_browser = None
            except Exception:
                self._close_h5_context_only()
                self._h5_browser = None

        if self._h5_browser is None:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if use_headless:
                launch_args.append("--headless=new")
            self._h5_browser = p.chromium.launch(
                channel="chrome",
                headless=use_headless,
                args=launch_args,
                ignore_default_args=["--enable-automation"],
            )

        if self._h5_ctx is not None and self._h5_items_served >= self.max_h5_items_per_context:
            self._close_h5_context_only()

        if self._h5_ctx is None:
            ctx = self._h5_browser.new_context(
                viewport=dict(DEFAULT_MOBILE_H5_VIEWPORT),
                user_agent=DEFAULT_MOBILE_H5_USER_AGENT,
                is_mobile=True,
                has_touch=True,
            )
            ctx.set_default_timeout(op_timeout_ms)
            ctx.set_default_navigation_timeout(op_timeout_ms)
            page = ctx.new_page()
            tracker = _attach_auth_tracker(page)

            def _on_new_page(new_p):
                new_p.on("response", tracker["_on_response_cb"])

            if hasattr(ctx, "on"):
                ctx.on("page", _on_new_page)
            # 先访问一次 H5 首页自然换取 _m_h5_tk 令牌，避免冷启动裸访详情页触发 RGV587_ERROR
            try:
                page.goto(GOOFISH_H5_HOME_URL, wait_until="domcontentloaded", timeout=20000)
                for _ in range(10):
                    cookies = {c["name"]: c["value"] for c in ctx.cookies() if c.get("name")}
                    if cookies.get("_m_h5_tk"):
                        break
                    page.wait_for_timeout(200)
                page.wait_for_timeout(random.randint(500, 900))
            except Exception:
                pass
            tracker["failed_apis"].clear()
            tracker["risk_rets"].clear()
            tracker["guest_login_blocked_rets"].clear()
            self._h5_ctx = ctx
            self._h5_page = page
            self._h5_tracker = tracker
            self._h5_on_page_cb = _on_new_page
            self._h5_items_served = 0
        else:
            gap = (self._last_h5_ts + self.h5_detail_min_interval_sec + random.uniform(0.0, 2.0)) - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            if self._h5_tracker is not None:
                self._h5_tracker["failed_apis"].clear()

        try:
            yield self._h5_ctx, self._h5_page, self._h5_tracker
        except Exception:
            self._close_h5_context_only()
            raise
        finally:
            self._h5_items_served += max(1, int(item_count))
            self._last_h5_ts = time.monotonic()

    def close(self) -> None:
        self._close_h5_context_only()
        if self._h5_browser is not None:
            try:
                self._h5_browser.close()
            except Exception:
                pass
            self._h5_browser = None
        self._close_search_context_only()
        if self._pw_cm is not None:
            try:
                self._pw_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._pw_cm = None
            self._pw = None
        try:
            self._lock_stack.close()
        except Exception:
            pass
        self._global_lock_held = False
        gc.collect()
