# -*- coding: utf-8 -*-
"""
Antigravity Official Local Client (100% Safe, Native Cascade + Sub-Account Sandbox Isolation)
=============================================================================================

原理说明（为什么绝对安全、不会封号、且大号小号完全物理隔离）：
1. 本模块【绝不伪造】任何发往 Google 的请求头或 TLS 指纹，100% 由官方原版 `language_server.exe` 发起通信。
2. 支持【小号沙箱隔离模式（SUB_ACCOUNT_EMAIL）】：
   - 利用官方 `language_server.exe` 内置的 `SSH_CONNECTION` 文件凭证存储机制与独立 `USERPROFILE` 沙箱目录，
     在同一台 Windows 电脑上启动与桌面端大号完全物理隔离的小号专属 `language_server.exe` 进程；
   - 桌面端大号继续使用 Windows 凭据管理器（`gemini:antigravity`），小号沙箱只读写独立沙箱目录
     `%LOCALAPPDATA%\\Antigravity\\sandboxes\\<小号邮箱>\\.gemini\\jetski-standalone-oauth-token`；
   - 每次连接时强制调用 `GetUserStatus` 校验云端返回的真实邮箱（`userStatus.email`），若与配置的小号不一致立即熔断抛错，
     100% 杜绝误用大号客户端能力。
3. 所有请求（无论纯文本还是多模态图片）100% 严格向桌面客户端原生使用方式看齐：
   统一走 `StartCascade` -> `SendUserCascadeMessage` -> `WaitForConversationFullyIdle` -> `GetCascadeTrajectory`，
   并在 `finally` 中立即调用 `DeleteCascadeTrajectory` + `JetboxDeleteSummary` 自动无痕销毁会话。
4. 内置基于操作系统内核文件锁的【多进程全局并发限制（默认全局并发=2）+ 跨进程错峰发包】，
   即使同时运行多个 Python 脚本进程，整机总并发也严格不超过设定上限。
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
from datetime import datetime, timezone
import json
import os
import random
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

if os.name == "nt":
    from ctypes import wintypes
    import msvcrt
else:
    import fcntl


# ==============================================================================
# 【核心配置】指定用于后台 AI 调用的独立小号邮箱（Sub-Account Sandbox）
# - 填入小号邮箱（如 "richard.nowak@usa.com"）：强制使用与桌面端大号完全物理隔离的小号沙箱进程；
# - 设为 None 或 ""：默认直接复用桌面端当前登录的账号；
# - 也可通过环境变量 ANTIGRAVITY_SUB_ACCOUNT 动态覆盖。
# ==============================================================================
SUB_ACCOUNT_EMAIL: Optional[str] = (
    os.environ.get("ANTIGRAVITY_SUB_ACCOUNT", "richard.nowak@usa.com").strip() or None
)


# 暴露指定的 Antigravity 本地模型名（同时兼容原名）
MODEL_ALIASES: Dict[str, str] = {
    "antigravity-argon-high": "MODEL_PLACEHOLDER_M417",
    "gemini-4-argon-high": "MODEL_PLACEHOLDER_M417",
    "antigravity-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
    "gemini-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
    "antigravity-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
    "gemini-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
}

DEFAULT_LOG_PATH = Path(os.environ.get("APPDATA", "")) / "Antigravity" / "logs" / "language_server.log"
HEADLESS_LOG_PATH = Path(os.environ.get("APPDATA", "")) / "Antigravity" / "logs" / "language_server_headless.log"
DEFAULT_LS_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs"
    / "antigravity"
    / "resources"
    / "bin"
    / "language_server.exe"
)
ANTIGRAVITY_LOCAL_ROOT = (
    Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "Antigravity"
)
GLOBAL_LOCK_DIR = ANTIGRAVITY_LOCAL_ROOT / "client_locks"
SANDBOX_ROOT_DIR = ANTIGRAVITY_LOCAL_ROOT / "sandboxes"
USER_GEMINI_TOKEN_PATH = Path.home() / ".gemini" / "jetski-standalone-oauth-token"
ANTIGRAVITY_TOOLS_ACCOUNTS_DIR = Path.home() / ".antigravity_tools" / "accounts"


def _sanitize_email_dirname(email: str) -> str:
    """将邮箱名转换为安全的文件夹名称。"""
    clean = email.strip().lower().replace("@", "_at_")
    return re.sub(r"[^a-z0-9._\-]", "_", clean)


def _extract_email_from_jwt(id_token: str) -> Optional[str]:
    """从 Google OAuth id_token (JWT) 载荷中解析出真实的邮箱地址。"""
    if not id_token or "." not in id_token:
        return None
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="ignore"))
        email = payload.get("email")
        return str(email).strip().lower() if email else None
    except Exception:
        return None


def _build_minimal_id_token(email: str) -> str:
    """当外部导入的凭证缺少 id_token 时，构造仅用于本地标识归属邮箱的轻量 JWT 载荷。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("ascii").rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"email": email.strip().lower()}).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{header}.{payload}."


def _read_windows_keyring_oauth_blob() -> Optional[Dict[str, Any]]:
    """
    直接读取官方 Antigravity 桌面客户端保存在 Windows 凭据管理器 (`gemini:antigravity`) 中的登录凭证。
    当用户在官方桌面客户端登录新小号时，可通过此函数自动将其归档到小号专属沙箱目录。
    """
    if os.name != "nt":
        return None

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    try:
        advapi32 = ctypes.windll.advapi32
        cred_ptr = ctypes.POINTER(_CREDENTIAL)()
        if not advapi32.CredReadW("gemini:antigravity", 1, 0, ctypes.byref(cred_ptr)):
            return None
        try:
            cred = cred_ptr.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        finally:
            advapi32.CredFree(cred_ptr)
    except Exception:
        return None


def _write_sandbox_oauth_blob(email: str, oauth_blob: Dict[str, Any], overwrite: bool = False) -> Path:
    """将指定邮箱的 OAuth 凭证写入其专属沙箱目录 `<sandbox_home>/.gemini/jetski-standalone-oauth-token`。"""
    norm_email = email.strip().lower()
    sandbox_dir = SANDBOX_ROOT_DIR / _sanitize_email_dirname(norm_email)
    gemini_dir = sandbox_dir / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)

    token_path = gemini_dir / "jetski-standalone-oauth-token"
    meta_path = sandbox_dir / "sandbox_meta.json"
    install_id_path = gemini_dir / "installation_id"

    if not install_id_path.exists():
        install_id_path.write_text(str(uuid.uuid4()), encoding="utf-8")

    new_refresh = ((oauth_blob.get("token") or {}).get("refresh_token") or "").strip()
    if not new_refresh:
        raise ValueError(f"账号 {norm_email} 的凭证缺少有效的 refresh_token")

    should_write = overwrite or (not token_path.exists())
    if not should_write and token_path.exists():
        try:
            existing = json.loads(token_path.read_text(encoding="utf-8"))
            old_refresh = ((existing.get("token") or {}).get("refresh_token") or "").strip()
            if old_refresh != new_refresh:
                should_write = True
        except Exception:
            should_write = True

    if should_write:
        blob_copy = dict(oauth_blob)
        if not _extract_email_from_jwt(blob_copy.get("id_token") or ""):
            blob_copy["id_token"] = _build_minimal_id_token(norm_email)
        token_path.write_text(json.dumps(blob_copy, ensure_ascii=False), encoding="utf-8")

    meta_path.write_text(
        json.dumps(
            {
                "email": norm_email,
                "sandbox_dir": str(sandbox_dir),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return sandbox_dir


def sync_all_known_accounts_to_sandboxes(overwrite_from_desktop: bool = False) -> Dict[str, Path]:
    """
    自动扫描本机所有可用的 Antigravity 账号凭证来源，并归档到 `%LOCALAPPDATA%\\Antigravity\\sandboxes\\`：
    1. 官方桌面客户端当前登录态（Windows 凭据管理器 `gemini:antigravity` 及 `~/.gemini/jetski-standalone-oauth-token`）；
    2. `~/.antigravity_tools/accounts/*.json` 中保存的所有账号；
    3. 已经存在于 `sandboxes/` 目录下的历史归档小号。
    返回 `{email: sandbox_dir}` 映射。
    """
    SANDBOX_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    discovered: Dict[str, Path] = {}

    # 1. 先加载已有的沙箱目录
    for sub in SANDBOX_ROOT_DIR.iterdir():
        if not sub.is_dir():
            continue
        tok_file = sub / ".gemini" / "jetski-standalone-oauth-token"
        meta_file = sub / "sandbox_meta.json"
        if tok_file.exists():
            email = None
            if meta_file.exists():
                try:
                    email = json.loads(meta_file.read_text(encoding="utf-8")).get("email")
                except Exception:
                    pass
            if not email:
                try:
                    blob = json.loads(tok_file.read_text(encoding="utf-8"))
                    email = _extract_email_from_jwt(blob.get("id_token") or "")
                except Exception:
                    pass
            if email:
                discovered[email.strip().lower()] = sub

    # 2. 扫描官方桌面客户端当前登录的凭证（Windows 凭据管理器 + ~/.gemini/jetski-standalone-oauth-token）
    desktop_blobs: List[Dict[str, Any]] = []
    kr_blob = _read_windows_keyring_oauth_blob()
    if kr_blob:
        desktop_blobs.append(kr_blob)
    if USER_GEMINI_TOKEN_PATH.exists():
        try:
            file_blob = json.loads(USER_GEMINI_TOKEN_PATH.read_text(encoding="utf-8"))
            if isinstance(file_blob, dict):
                desktop_blobs.append(file_blob)
        except Exception:
            pass

    for blob in desktop_blobs:
        email = _extract_email_from_jwt(blob.get("id_token") or "")
        if email and ((blob.get("token") or {}).get("refresh_token")):
            try:
                sdir = _write_sandbox_oauth_blob(email, blob, overwrite=overwrite_from_desktop)
                discovered[email] = sdir
            except Exception:
                pass

    # 3. 扫描 ~/.antigravity_tools/accounts/*.json
    if ANTIGRAVITY_TOOLS_ACCOUNTS_DIR.exists():
        for acc_file in ANTIGRAVITY_TOOLS_ACCOUNTS_DIR.glob("*.json"):
            try:
                data = json.loads(acc_file.read_text(encoding="utf-8"))
                email = (data.get("email") or "").strip().lower()
                tok = data.get("token") or {}
                refresh_token = (tok.get("refresh_token") or "").strip()
                if not email or not refresh_token or data.get("disabled"):
                    continue
                oauth_blob = {
                    "token": {
                        "access_token": tok.get("access_token") or "",
                        "token_type": tok.get("token_type") or "Bearer",
                        "refresh_token": refresh_token,
                        "expiry": "2020-01-01T00:00:00Z",
                    },
                    "auth_method": "consumer",
                    "id_token": _build_minimal_id_token(email),
                }
                sdir = _write_sandbox_oauth_blob(email, oauth_blob, overwrite=False)
                discovered[email] = sdir
            except Exception:
                pass

    return discovered


def archive_current_desktop_account() -> Optional[str]:
    """
    当您在官方 Antigravity 桌面客户端登录了一个新小号后，调用此函数（或运行本模块 CLI），
    即可立即将当前桌面客户端的登录凭证永久快照归档到独立沙箱目录中，随后桌面客户端即可切回大号。
    返回成功归档的账号邮箱。
    """
    blob = _read_windows_keyring_oauth_blob()
    if not blob and USER_GEMINI_TOKEN_PATH.exists():
        try:
            blob = json.loads(USER_GEMINI_TOKEN_PATH.read_text(encoding="utf-8"))
        except Exception:
            blob = None
    if not blob:
        return None
    email = _extract_email_from_jwt(blob.get("id_token") or "")
    if not email:
        return None
    _write_sandbox_oauth_blob(email, blob, overwrite=True)
    return email


class AntigravityClient:
    """
    基于官方本地 language_server.exe 的安全调用客户端（支持小号沙箱物理隔离）。
    - 当设置了 `sub_account_email`（默认读取模块顶部 `SUB_ACCOUNT_EMAIL`）：
      强制连接/启动该小号专属的隔离沙箱 `language_server.exe` 进程，并通过 `GetUserStatus`
      硬性校验云端返回的真实邮箱（`userStatus.email`），绝不触碰桌面端正在使用的大号；
    - 当 `sub_account_email=None`：
      直接复用桌面客户端当前打开的后台进程（若关闭则静默自启）；
    - 内置【跨进程全局并发控制（默认 max_concurrency=2 对多进程全局生效）+ 跨进程错峰发包间隔（Stagger）】。
    """

    def __init__(
        self,
        http_port: Optional[int] = None,
        csrf_token: Optional[str] = None,
        default_model: str = "antigravity-3.8-flash-high",
        timeout: float = 120.0,
        auto_start: bool = True,
        max_concurrency: int = 2,
        min_stagger_seconds: float = 0.8,
        max_stagger_seconds: float = 1.8,
        sub_account_email: Optional[str] = SUB_ACCOUNT_EMAIL,
    ) -> None:
        self.default_model = default_model
        self.timeout = timeout
        self.auto_start = auto_start
        self.sub_account_email = sub_account_email.strip().lower() if sub_account_email else None
        self._http_port = http_port
        self._csrf_token = csrf_token
        self._server_pid: Optional[int] = None
        self._verified_email: Optional[str] = None

        self._max_concurrency = max(1, int(max_concurrency))
        self._concurrency_sem = threading.BoundedSemaphore(self._max_concurrency)
        self._pacing_lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._last_dispatch_ts: float = 0.0
        self._min_stagger = max(0.0, float(min_stagger_seconds))
        self._max_stagger = max(self._min_stagger, float(max_stagger_seconds))

    @property
    def verified_email(self) -> Optional[str]:
        """返回经 Google 云端 GetUserStatus 实名验证过的当前连接邮箱。"""
        return self._verified_email

    @staticmethod
    def _try_lock_fd(fd: int) -> bool:
        """尝试对文件描述符第 0 字节加非阻塞排他锁（OS 内核级锁，进程退出自动释放）。"""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        """释放文件描述符第 0 字节的排他锁。"""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    @contextlib.contextmanager
    def _acquire_global_slot(self):
        """
        跨进程 + 跨线程全局并发槽位锁（如 max_concurrency=2 对本机所有 Python 进程同时生效）：
        1. 先获取当前进程内 BoundedSemaphore；
        2. 轮询争抢 0 .. max_concurrency-1 号全局锁文件槽位；
        3. 获取槽位后执行跨进程错峰延迟 (_wait_for_stagger_slot)。
        """
        with self._concurrency_sem:
            GLOBAL_LOCK_DIR.mkdir(parents=True, exist_ok=True)
            locked_fd: Optional[int] = None
            while locked_fd is None:
                for slot_idx in range(self._max_concurrency):
                    slot_file = GLOBAL_LOCK_DIR / f"ag_concurrency_slot_{slot_idx}.lock"
                    fd: Optional[int] = None
                    try:
                        fd = os.open(str(slot_file), os.O_CREAT | os.O_RDWR)
                        if os.fstat(fd).st_size == 0:
                            try:
                                os.write(fd, b"0")
                            except OSError:
                                pass
                        if self._try_lock_fd(fd):
                            locked_fd = fd
                            break
                        os.close(fd)
                    except OSError:
                        if fd is not None:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                if locked_fd is None:
                    time.sleep(random.uniform(0.08, 0.16))

            try:
                self._wait_for_stagger_slot()
                yield
            finally:
                self._unlock_fd(locked_fd)
                try:
                    os.close(locked_fd)
                except OSError:
                    pass

    @staticmethod
    def _get_live_ls_processes() -> Dict[int, Dict[str, str]]:
        """获取当前正在运行的 language_server.exe 进程列表: {pid: {'csrf_token': ..., 'cmdline': ...}}。"""
        live: Dict[int, Dict[str, str]] = {}
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='language_server.exe'\" "
                "| Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress",
            ]
            out = subprocess.check_output(cmd, text=True, timeout=10, stderr=subprocess.DEVNULL).strip()
            if not out:
                return live
            data = json.loads(out)
            items = data if isinstance(data, list) else [data]
            for item in items:
                pid = item.get("ProcessId")
                cmdline = item.get("CommandLine") or ""
                m_token = re.search(r"--csrf_token\s+([0-9a-fA-F\-]+)", cmdline)
                if pid and m_token:
                    live[int(pid)] = {
                        "csrf_token": m_token.group(1),
                        "cmdline": cmdline,
                    }
        except Exception:
            pass
        return live

    @staticmethod
    def _parse_log_pid_and_port(log_path: Path) -> tuple[Optional[int], Optional[int]]:
        """从指定日志文件中解析 language_server 的 PID 和 HTTP 端口。"""
        if not log_path.exists():
            return None, None
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(65536)
            m_pid = re.search(r"Starting language server process with pid (\d+)", head)
            m_port = re.search(r"Language server listening on random port at (\d+) for HTTP\b", head)
            pid = int(m_pid.group(1)) if m_pid else None
            port = int(m_port.group(1)) if m_port else None
            return pid, port
        except OSError:
            return None, None

    @staticmethod
    def _detect_ide_version() -> str:
        """从官方日志中动态读取当前 language_server.exe 的真实版本号。"""
        for log_path in (DEFAULT_LOG_PATH, HEADLESS_LOG_PATH):
            if log_path.exists():
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(8192)
                    m = re.search(r"Language server version:\s*([0-9]+\.[0-9]+\.[0-9]+)", head)
                    if m:
                        return m.group(1)
                except OSError:
                    pass
        return "2.16.0"

    def _raw_rpc_call(
        self,
        port: int,
        csrf_token: str,
        method: str,
        payload: Dict[str, Any],
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """向指定端口发送单次底层 RPC 请求。"""
        url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf_token,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def _verify_and_bind_connection(
        self,
        port: int,
        csrf_token: str,
        pid: Optional[int] = None,
        wait_seconds: float = 10.0,
    ) -> tuple[int, str]:
        """
        【防串号核心铁闸】：连接建立后立即调用 GetUserStatus，向 Google 云端核实当前进程的真实账号邮箱。
        如果配置了 `self.sub_account_email`，则返回的 `userStatus.email` 必须 100% 严格匹配该小号，
        否则立即抛出异常熔断，绝不允许误用大号！
        """
        deadline = time.time() + max(1.0, wait_seconds)
        actual_email: Optional[str] = None
        last_err: Optional[Exception] = None

        while time.time() < deadline:
            try:
                status = self._raw_rpc_call(port, csrf_token, "GetUserStatus", {}, timeout=8.0)
                email = ((status.get("userStatus") or {}).get("email") or "").strip().lower()
                if email:
                    actual_email = email
                    break
            except Exception as exc:
                last_err = exc
            time.sleep(0.4)

        if not actual_email:
            raise RuntimeError(
                f"无法从本地 language_server (port={port}) 验证当前登录的账号邮箱"
                + (f": {last_err}" if last_err else "")
            )

        if self.sub_account_email and actual_email != self.sub_account_email:
            raise RuntimeError(
                f"【防串号安全熔断】当前连接的 language_server (PID={pid}, Port={port}) "
                f"实际账号为 [{actual_email}]，与您配置的小号 [{self.sub_account_email}] 不一致！"
                f"已强制拦截本次调用，确保绝不消耗大号额度。"
            )

        self._http_port = port
        self._csrf_token = csrf_token
        self._server_pid = pid
        self._verified_email = actual_email
        return port, csrf_token

    def _start_sandbox_or_headless_server(self, sandbox_dir: Optional[Path] = None) -> tuple[int, str]:
        """
        后台静默启动官方 language_server.exe（无 GUI 窗口）：
        - 若传入 `sandbox_dir`（小号沙箱模式）：通过 `SSH_CONNECTION` + 独立 `USERPROFILE` 启动完全物理隔离的进程；
        - 若未传 `sandbox_dir`（默认模式）：启动默认无头进程。
        """
        if not DEFAULT_LS_BIN.exists():
            raise RuntimeError(f"未找到官方 language_server.exe: {DEFAULT_LS_BIN}")

        ide_version = self._detect_ide_version()
        csrf_token = str(uuid.uuid4())

        env = os.environ.copy()
        if sandbox_dir is not None:
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir = sandbox_dir / ".gemini"
            roaming_dir = sandbox_dir / "AppData" / "Roaming"
            local_dir = sandbox_dir / "AppData" / "Local"
            gemini_dir.mkdir(parents=True, exist_ok=True)
            roaming_dir.mkdir(parents=True, exist_ok=True)
            local_dir.mkdir(parents=True, exist_ok=True)

            log_path = sandbox_dir / "language_server_sandbox.log"
            # 触发官方 language_server.exe 内置的文件凭证模式，彻底隔离 Windows 凭据管理器中的大号凭证
            env["SSH_CONNECTION"] = "127.0.0.1 22 127.0.0.1 22"
            env["USERPROFILE"] = str(sandbox_dir)
            env["HOME"] = str(sandbox_dir)
            env["APPDATA"] = str(roaming_dir)
            env["LOCALAPPDATA"] = str(local_dir)
        else:
            log_path = HEADLESS_LOG_PATH
            log_path.parent.mkdir(parents=True, exist_ok=True)

        log_file = open(log_path, "w", encoding="utf-8")

        args = [
            str(DEFAULT_LS_BIN),
            "--standalone",
            "--override_ide_name", "antigravity",
            "--subclient_type", "hub",
            "--override_ide_version", ide_version,
            "--override_user_agent_name", "antigravity",
            "--https_server_port", "0",
            "--csrf_token", csrf_token,
            "--app_data_dir", "antigravity",
            "--api_server_url", "https://generativelanguage.googleapis.com",
            "--cloud_code_endpoint", "https://daily-cloudcode-pa.googleapis.com",
            "--enable_sidecars",
        ]
        if sandbox_dir is not None:
            args.extend(["--gemini_dir", str(sandbox_dir / ".gemini")])

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
        proc = subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
            close_fds=True,
        )

        http_port: Optional[int] = None
        deadline = time.time() + 18.0
        while time.time() < deadline:
            try:
                content = log_path.read_text(encoding="utf-8", errors="ignore")
                if not http_port:
                    m = re.search(r"Language server listening on random port at (\d+) for HTTP\b", content)
                    if m:
                        http_port = int(m.group(1))
                if http_port and (
                    "Auth succeeded" in content
                    or "initialized server successfully" in content
                    or "Token refreshed successfully" in content
                ):
                    break
            except OSError:
                pass
            time.sleep(0.25)

        if not http_port:
            raise RuntimeError(f"后台静默启动官方 language_server.exe 超时，请检查日志: {log_path}")

        return self._verify_and_bind_connection(
            port=http_port,
            csrf_token=csrf_token,
            pid=proc.pid,
            wait_seconds=12.0,
        )

    @contextlib.contextmanager
    def _acquire_discovery_file_lock(self):
        """跨进程服务发现/启动互斥锁：防止多个 Python 进程同时启动时重复拉起多个沙箱 language_server.exe。"""
        GLOBAL_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = GLOBAL_LOCK_DIR / "ag_discovery.lock"
        fd: Optional[int] = None
        while fd is None:
            candidate_fd: Optional[int] = None
            try:
                candidate_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
                if os.fstat(candidate_fd).st_size == 0:
                    try:
                        os.write(candidate_fd, b"0")
                    except OSError:
                        pass
                if self._try_lock_fd(candidate_fd):
                    fd = candidate_fd
                    break
                os.close(candidate_fd)
            except OSError:
                if candidate_fd is not None:
                    try:
                        os.close(candidate_fd)
                    except OSError:
                        pass
            time.sleep(random.uniform(0.05, 0.15))
        try:
            yield
        finally:
            self._unlock_fd(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    def _discover_local_server(self) -> tuple[int, str]:
        """
        自动发现或拉起官方 language_server.exe：
        - 当配置了 `self.sub_account_email`（小号沙箱模式）时：
          【绝不读取】桌面端大号的 `DEFAULT_LOG_PATH`，只连接或启动该小号专属沙箱目录下的进程，
          并强制通过 `GetUserStatus` 验证云端账号身份！
        - 当未配置 `self.sub_account_email` 时：
          复用桌面端进程或启动无头进程。
        """
        with self._discovery_lock:
            if self._http_port and self._csrf_token:
                return self._http_port, self._csrf_token

            with self._acquire_discovery_file_lock():
                live_procs = self._get_live_ls_processes()

                # 分支 A：小号沙箱隔离模式（100% 物理隔离桌面端大号）
                if self.sub_account_email:
                    known_sandboxes = sync_all_known_accounts_to_sandboxes()
                    sandbox_dir = known_sandboxes.get(self.sub_account_email)
                    if not sandbox_dir:
                        available = sorted(known_sandboxes.keys())
                        raise RuntimeError(
                            f"未找到小号 [{self.sub_account_email}] 的已归档登录凭证！\n"
                            f"当前已归档可用的账号列表: {available}\n"
                            f"👉 若要添加新小号：请先在 Antigravity 客户端登录一次该新小号，然后运行 "
                            f"`python -m mobile_web.infrastructure.ai_api.antigravity_client` 自动完成凭证归档，之后客户端即可切回大号。"
                        )

                    sandbox_log = sandbox_dir / "language_server_sandbox.log"
                    pid, port = self._parse_log_pid_and_port(sandbox_log)
                    if pid and port and pid in live_procs:
                        cmdline = live_procs[pid].get("cmdline") or ""
                        # 确保该 PID 确实属于当前小号沙箱目录
                        if str(sandbox_dir).lower() in cmdline.lower():
                            try:
                                return self._verify_and_bind_connection(
                                    port=port,
                                    csrf_token=live_procs[pid]["csrf_token"],
                                    pid=pid,
                                    wait_seconds=5.0,
                                )
                            except Exception:
                                pass

                    if self.auto_start:
                        return self._start_sandbox_or_headless_server(sandbox_dir=sandbox_dir)

                    raise RuntimeError(
                        f"小号 [{self.sub_account_email}] 的沙箱 language_server.exe 未运行且 auto_start=False。"
                    )

                # 分支 B：默认模式（未指定小号，直接复用桌面客户端当前账号）
                for log_path in (DEFAULT_LOG_PATH, HEADLESS_LOG_PATH):
                    pid, port = self._parse_log_pid_and_port(log_path)
                    if pid and port and pid in live_procs:
                        return self._verify_and_bind_connection(
                            port=port,
                            csrf_token=live_procs[pid]["csrf_token"],
                            pid=pid,
                            wait_seconds=5.0,
                        )

                if self.auto_start:
                    return self._start_sandbox_or_headless_server(sandbox_dir=None)

                raise RuntimeError(
                    "未能自动检测到正在运行的 Antigravity (language_server.exe)。"
                )

    def _ensure_connection_info(self) -> tuple[int, str]:
        if self._http_port and self._csrf_token:
            return self._http_port, self._csrf_token
        return self._discover_local_server()

    def _wait_for_stagger_slot(self) -> None:
        """跨进程确保每次大模型请求的发起时间错开 0.8~1.8 秒，消除『同一毫秒并发轰炸』的脚本特征。"""
        if self._max_stagger <= 0:
            return
        with self._pacing_lock:
            GLOBAL_LOCK_DIR.mkdir(parents=True, exist_ok=True)
            pacing_lock_file = GLOBAL_LOCK_DIR / "ag_pacing.lock"
            pacing_state_file = GLOBAL_LOCK_DIR / "ag_pacing.state"
            fd: Optional[int] = None
            while fd is None:
                candidate_fd: Optional[int] = None
                try:
                    candidate_fd = os.open(str(pacing_lock_file), os.O_CREAT | os.O_RDWR)
                    if os.fstat(candidate_fd).st_size == 0:
                        try:
                            os.write(candidate_fd, b"0")
                        except OSError:
                            pass
                    if self._try_lock_fd(candidate_fd):
                        fd = candidate_fd
                        break
                    os.close(candidate_fd)
                except OSError:
                    if candidate_fd is not None:
                        try:
                            os.close(candidate_fd)
                        except OSError:
                            pass
                time.sleep(random.uniform(0.03, 0.08))

            try:
                now = time.time()
                last_ts = self._last_dispatch_ts
                if pacing_state_file.exists():
                    try:
                        raw_ts = pacing_state_file.read_text(encoding="utf-8").strip()
                        if raw_ts:
                            last_ts = max(last_ts, float(raw_ts))
                    except (OSError, ValueError):
                        pass

                target_gap = random.uniform(self._min_stagger, self._max_stagger)
                wait_sec = (last_ts + target_gap) - now
                if 0 < wait_sec <= (self._max_stagger + 5.0):
                    time.sleep(wait_sec)

                dispatch_ts = time.time()
                self._last_dispatch_ts = dispatch_ts
                try:
                    pacing_state_file.write_text(f"{dispatch_ts:.6f}", encoding="utf-8")
                except OSError:
                    pass
            finally:
                self._unlock_fd(fd)
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _post_rpc(
        self,
        method: str,
        payload: Dict[str, Any],
        retry_discovery: bool = True,
        apply_rate_limit: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """调用本地官方 LanguageServerService ConnectRPC 接口（强制直连 127.0.0.1，不经过外部代理）。"""
        if apply_rate_limit:
            with self._acquire_global_slot():
                return self._post_rpc_inner(method, payload, retry_discovery=retry_discovery, timeout=timeout)
        return self._post_rpc_inner(method, payload, retry_discovery=retry_discovery, timeout=timeout)

    def _post_rpc_inner(
        self,
        method: str,
        payload: Dict[str, Any],
        retry_discovery: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        port, token = self._ensure_connection_info()
        url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": token,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req_timeout = float(timeout) if timeout is not None else self.timeout
        try:
            with opener.open(req, timeout=req_timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            err = RuntimeError(
                f"Antigravity 服务端返回 HTTP {exc.code} ({method}): {err_body or exc.reason}"
            )
            setattr(err, "status_code", exc.code)
            raise err from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError(f"调用本地 Antigravity LanguageServer ({method}) 响应超时 ({req_timeout}s)") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TimeoutError(
                    f"调用本地 Antigravity LanguageServer ({method}) 响应超时 ({req_timeout}s)"
                ) from exc
            if retry_discovery:
                with self._discovery_lock:
                    self._http_port = None
                    self._csrf_token = None
                return self._post_rpc_inner(method, payload, retry_discovery=False, timeout=timeout)
            raise RuntimeError(f"调用本地 Antigravity LanguageServer ({method}) 网络连接失败: {exc}") from exc

    @staticmethod
    def resolve_model_enum(model: str) -> str:
        """将模型名称转换为内部枚举名。"""
        if model.startswith("MODEL_"):
            return model
        if model in MODEL_ALIASES:
            return MODEL_ALIASES[model]
        raise ValueError(
            f"不支持的 Antigravity 本地模型名: {model}。"
            f"可用模型名: {list(MODEL_ALIASES.keys())}"
        )

    def ask_with_usage(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        media: Optional[List[Dict[str, str]]] = None,
        timeout: Optional[float] = None,
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        """
        统一的模型生成接口（严格与桌面客户端原生对话行为保持一致，返回 (reply_text, usage_dict)）。
        - 无论纯文本还是多模态图片，统一走客户端原生 Cascade 会话流：
          StartCascade -> SendUserCascadeMessage -> WaitForConversationFullyIdle -> GetCascadeTrajectory
        - 在 finally 中立即调用 DeleteCascadeTrajectory + JetboxDeleteSummary 彻底无痕销毁临时会话。
        - 全程受跨进程全局并发锁（默认 max_concurrency=2）与错峰发包保护。
        - 返回的 usage_dict 中额外携带经云端验证的 `account_email` 与 `sandbox_port`，便于日志审计。
        """
        target_model = self.resolve_model_enum(model or self.default_model)
        full_prompt = f"{system_prompt.strip()}\n\n{prompt}" if system_prompt else prompt
        req_timeout = float(timeout) if timeout is not None else self.timeout
        workspace_uri = Path.cwd().resolve().as_uri()

        with self._acquire_global_slot():
            start_resp = self._post_rpc_inner(
                "StartCascade",
                {
                    "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
                    "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
                    "requestedModel": target_model,
                    "workspaceUris": [workspace_uri],
                },
                timeout=min(req_timeout, 30.0),
            )
            cascade_id = start_resp.get("cascadeId")
            if not cascade_id:
                raise RuntimeError("StartCascade 未返回有效的 cascadeId")

            try:
                no_tool_guard = (
                    "【系统强制约束：当前为非交互式分析与生成模式，严禁调用任何工具(tools/subagents/commands)，"
                    "请直接阅读下方提供的上下文与提示词，直接输出最终文本或JSON结果。】\n\n"
                )
                send_payload: Dict[str, Any] = {
                    "cascadeId": cascade_id,
                    "items": [{"text": no_tool_guard + full_prompt}],
                    "cascadeConfig": {
                        "plannerConfig": {
                            "conversational": {"agenticMode": False},
                            "requestedModel": {"model": target_model},
                            "planModel": target_model,
                            "useAiCredits": True,
                            "supportsLatexRendering": True,
                        },
                        "executorConfig": {
                            "useCoreDirect": True,
                        },
                        "conversationHistoryConfig": {},
                    },
                }
                if media:
                    send_payload["media"] = media

                self._post_rpc_inner(
                    "SendUserCascadeMessage",
                    send_payload,
                    timeout=req_timeout,
                )
                wait_resp = self._post_rpc_inner(
                    "WaitForConversationFullyIdle",
                    {
                        "conversationId": cascade_id,
                        "inactivityTimeoutSeconds": max(15, int(req_timeout)),
                        "returnOnExecutorError": True,
                    },
                    timeout=req_timeout + 15.0,
                )
                if wait_resp.get("timedOut"):
                    raise TimeoutError(f"Antigravity Cascade 生成等待超时 ({req_timeout}s)")

                traj_resp = self._post_rpc_inner(
                    "GetCascadeTrajectory",
                    {"cascadeId": cascade_id},
                    timeout=30.0,
                )
                steps = traj_resp.get("trajectory", {}).get("steps", [])
                planner_steps = [
                    s for s in steps
                    if s.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
                    and (s.get("plannerResponse") or {}).get("response")
                ]
                if not planner_steps:
                    error_steps = [
                        s for s in steps
                        if s.get("status") in ("CORTEX_STEP_STATUS_ERROR", "CORTEX_STEP_STATUS_CANCELED")
                    ]
                    if error_steps:
                        raise RuntimeError(
                            f"Antigravity Cascade 执行异常结束: {error_steps[-1].get('status')}"
                        )
                    raise RuntimeError("Antigravity Cascade 调用未返回有效的 plannerResponse 文本")

                last_step = planner_steps[-1]
                reply_text = (last_step.get("plannerResponse") or {}).get("response", "")
                model_usage = (last_step.get("metadata") or {}).get("modelUsage") or {}
                p_tok = int(model_usage.get("inputTokens") or 0)
                c_tok = int(model_usage.get("outputTokens") or 0)
                usage_dict: Dict[str, Any] = {
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "total_tokens": p_tok + c_tok,
                    "account_email": self._verified_email or self.sub_account_email or "desktop",
                    "sandbox_port": self._http_port,
                    "sandbox_pid": self._server_pid,
                }
                return reply_text, usage_dict
            finally:
                # 无论成功或异常，立即无痕删除临时会话与桌面端摘要记录
                for cleanup_method in ("DeleteCascadeTrajectory", "JetboxDeleteSummary"):
                    try:
                        self._post_rpc_inner(
                            cleanup_method,
                            {"cascadeId": cascade_id},
                            retry_discovery=False,
                            timeout=10.0,
                        )
                    except Exception:
                        pass

    def ask(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        media: Optional[List[Dict[str, str]]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        reply, _ = self.ask_with_usage(
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            media=media,
            timeout=timeout,
        )
        return reply

    def ask_json(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        media: Optional[List[Dict[str, str]]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """要求模型返回 JSON 格式并自动解析为 Python 字典。"""
        json_instruction = (
            (system_prompt or "")
            + "\n请严格只输出合法的 JSON 对象，不要包含 ```json 代码块包裹或任何额外解释文字。"
        ).strip()
        raw = self.ask(
            prompt=prompt,
            model=model,
            system_prompt=json_instruction,
            media=media,
            timeout=timeout,
        ).strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)

    def get_user_status(self) -> Dict[str, Any]:
        """获取当前连接的账号信息、会员等级以及各模型剩余额度比例。"""
        return self._post_rpc("GetUserStatus", {})

    def get_active_account_info(self) -> Dict[str, Any]:
        """
        获取当前客户端实际连接的账号详情，并与桌面客户端大号进行物理隔离对比校验。
        不依赖剩余额度百分比，直接通过 Google 云端返回的 `userStatus.email` + 进程 PID + 端口进行 100% 实名确权。
        """
        status = self.get_user_status().get("userStatus", {})
        active_email = (status.get("email") or "").strip().lower()
        tier_name = ((status.get("userTier") or {}).get("name")) or "Unknown"

        # 检测桌面客户端当前进程与邮箱（仅作只读对比展示）
        desktop_email = None
        kr_blob = _read_windows_keyring_oauth_blob()
        if kr_blob:
            desktop_email = _extract_email_from_jwt(kr_blob.get("id_token") or "")
        desktop_pid, desktop_port = self._parse_log_pid_and_port(DEFAULT_LOG_PATH)

        return {
            "configured_sub_account": self.sub_account_email,
            "verified_active_email": active_email,
            "tier": tier_name,
            "active_pid": self._server_pid,
            "active_port": self._http_port,
            "desktop_main_email": desktop_email,
            "desktop_main_pid": desktop_pid,
            "desktop_main_port": desktop_port,
            "is_process_isolated": (self._server_pid is not None and self._server_pid != desktop_pid),
            "is_account_isolated": (bool(active_email) and active_email != desktop_email),
        }

    def list_models(self) -> List[Dict[str, Any]]:
        """列出当前账号可用的所有模型及其剩余额度百分比与重置时间。"""
        status = self.get_user_status()
        configs = (
            status.get("userStatus", {})
            .get("cascadeModelConfigData", {})
            .get("clientModelConfigs", [])
        )
        result = []
        for cfg in configs:
            quota = cfg.get("quotaInfo", {})
            result.append(
                {
                    "label": cfg.get("label"),
                    "model_id": cfg.get("modelId"),
                    "model_enum": cfg.get("modelOrAlias", {}).get("model"),
                    "remaining_fraction": quota.get("remainingFraction"),
                    "reset_time": quota.get("resetTime"),
                }
            )
        return result


_default_client_lock = threading.Lock()
_default_client: Optional[AntigravityClient] = None


def get_client(
    max_concurrency: int = 2,
    sub_account_email: Optional[str] = None,
) -> AntigravityClient:
    """获取进程级全局共享的 AntigravityClient 单例（自动绑定配置的小号沙箱）。"""
    global _default_client
    target_email = (
        sub_account_email.strip().lower()
        if sub_account_email is not None
        else (SUB_ACCOUNT_EMAIL.strip().lower() if SUB_ACCOUNT_EMAIL else None)
    )
    with _default_client_lock:
        if _default_client is None or _default_client.sub_account_email != target_email:
            _default_client = AntigravityClient(
                max_concurrency=max_concurrency,
                sub_account_email=target_email,
            )
    return _default_client


def set_sub_account(email: Optional[str], max_concurrency: int = 2) -> AntigravityClient:
    """在代码中动态切换当前使用的小号邮箱，并返回绑定新小号的客户端实例。"""
    global SUB_ACCOUNT_EMAIL
    SUB_ACCOUNT_EMAIL = email.strip().lower() if email and email.strip() else None
    return get_client(max_concurrency=max_concurrency, sub_account_email=SUB_ACCOUNT_EMAIL)


def ask_antigravity(
    prompt: str,
    model: str = "antigravity-3.8-flash-high",
    system_prompt: Optional[str] = None,
) -> str:
    return get_client().ask(prompt=prompt, model=model, system_prompt=system_prompt)


def ask_antigravity_json(
    prompt: str,
    model: str = "antigravity-3.8-flash-high",
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    return get_client().ask_json(prompt=prompt, model=model, system_prompt=system_prompt)


ask = ask_antigravity
ask_json = ask_antigravity_json


if __name__ == "__main__":
    print("=" * 76)
    print("Antigravity 小号沙箱隔离状态自检 & 凭证自动归档工具")
    print("=" * 76)
    archived_desktop = archive_current_desktop_account()
    all_sandboxes = sync_all_known_accounts_to_sandboxes()
    print(f"[1] 桌面端当前登录账号自动快照: {archived_desktop or '未检测到'}")
    print(f"[2] 本机已归档可直接配置使用的小号沙箱 ({len(all_sandboxes)} 个):")
    for idx, (em, sdir) in enumerate(sorted(all_sandboxes.items()), 1):
        marker = "  <-- 当前 SUB_ACCOUNT_EMAIL 配置" if em == (SUB_ACCOUNT_EMAIL or "").lower() else ""
        print(f"    {idx}. {em}  ({sdir}){marker}")

    print("-" * 76)
    client = get_client()
    info = client.get_active_account_info()
    print("[3] 实时物理隔离与账号实名核验结果 (基于 Google GetUserStatus.email):")
    print(f"    - 桌面客户端 (大号) : Email={info['desktop_main_email']} | PID={info['desktop_main_pid']} | Port={info['desktop_main_port']}")
    print(f"    - 代码沙箱端 (小号) : Email={info['verified_active_email']} | PID={info['active_pid']} | Port={info['active_port']} | 等级={info['tier']}")
    print(f"    - 进程物理隔离校验  : {'✅ 已完全隔离 (独立进程/独立端口)' if info['is_process_isolated'] else '⚠️ 共用进程'}")
    print(f"    - 大小号身份隔离校验: {'✅ 确认正在使用小号 (绝不触碰大号)' if info['is_account_isolated'] else '⚠️ 当前与桌面端为同一账号'}")
    print("=" * 76)
