# -*- coding: utf-8 -*-
"""闲鱼销售渠道适配器：服务端托管隔离目录、扫码登录、在线检测与真实在售同步。"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import logging
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Dict, List, Optional
import uuid

from PIL import Image

from ..client.seller import check_login_status, downshelf_item, get_selling_items, publish_item
from ..client.session import (
    GOOFISH_PERSONAL_URL,
    XianyuLockTimeoutError,
    _acquire_serial_browser_lock,
    _attach_auth_tracker,
    _detach_tracker_from_context,
    _extract_risk_reasons,
    launch_persistent_context,
    sync_playwright,
)
from .base import (
    ChannelAccountStatus,
    ChannelCapabilities,
    ChannelSyncResult,
    QrLoginSnapshot,
    SellingChannel,
)

logger = logging.getLogger("xianyu_workbench.channels.xianyu")

QR_LOGIN_TIMEOUT_SEC = 150.0
MAX_ACTIVE_QR_SESSIONS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_qr_png(raw_png: bytes) -> bytes:
    """放大二维码并补齐白色静区，确保手机相册保存后易于闲鱼 App 识别。"""
    with Image.open(io.BytesIO(raw_png)) as img:
        rgb = img.convert("RGB")
        w, h = rgb.size
        target = 280
        if max(w, h) < target and min(w, h) > 0:
            scale = max(1, (target + min(w, h) - 1) // min(w, h))
            rgb = rgb.resize((w * scale, h * scale), Image.Resampling.NEAREST)
        pad = 20
        canvas = Image.new("RGB", (rgb.width + pad * 2, rgb.height + pad * 2), (255, 255, 255))
        canvas.paste(rgb, (pad, pad))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()


def _rename_with_retry(src: Path, dst: Path, attempts: int = 8, delay_sec: float = 0.25) -> None:
    """等待 Chrome 子进程在关闭后彻底释放 Windows 目录句柄，再执行原子改名。"""
    last_err: Optional[OSError] = None
    for i in range(max(1, attempts)):
        try:
            src.rename(dst)
            return
        except OSError as exc:
            last_err = exc
            if i + 1 < attempts:
                time.sleep(delay_sec)
    assert last_err is not None
    raise last_err


def _replace_profile_dir(staging_dir: Path, target_dir: Path) -> None:
    """用扫码登录成功的全新目录原子替换正式托管目录，失败时自动回滚旧目录。"""
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_dir: Optional[Path] = None
    if target_dir.exists():
        backup_dir = target_dir.with_name(f"{target_dir.name}.bak_{uuid.uuid4().hex[:6]}")
        _rename_with_retry(target_dir, backup_dir)
    try:
        _rename_with_retry(staging_dir, target_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
            try:
                _rename_with_retry(backup_dir, target_dir)
            except Exception:
                pass
        raise
    if backup_dir is not None and backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)


class _XianyuQrLoginWorker:
    """单线程独占 Playwright 生命周期的闲鱼扫码登录工作器。"""

    def __init__(
        self,
        *,
        session_id: str,
        owner: str,
        profile_dir: Path,
        timeout_sec: float = QR_LOGIN_TIMEOUT_SEC,
    ) -> None:
        self.session_id = session_id
        self.owner = owner
        self.profile_dir = profile_dir
        self.timeout_sec = max(30.0, float(timeout_sec or QR_LOGIN_TIMEOUT_SEC))
        self.deadline = time.monotonic() + self.timeout_sec

        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._state = "starting"
        self._message = "正在打开闲鱼登录页..."
        self._qr_bytes: Optional[bytes] = None
        self._account_id = ""
        self._nickname = ""
        self._error_code = ""
        self._thread = threading.Thread(
            target=self._run,
            name=f"xy-qr-{session_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._lock:
            if not self._is_terminal_state(self._state):
                self._state = "cancelled"
                self._message = "已取消扫码登录"
                self._qr_bytes = None

    def join(self, timeout: float = 2.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @staticmethod
    def _is_terminal_state(state: str) -> bool:
        return state in ("success", "expired", "risk_blocked", "error", "cancelled")

    def is_active(self) -> bool:
        with self._lock:
            return not self._is_terminal_state(self._state) and self._thread.is_alive()

    def snapshot(self) -> QrLoginSnapshot:
        with self._lock:
            rem = max(0, int(round(self.deadline - time.monotonic())))
            return QrLoginSnapshot(
                session_id=self.session_id,
                channel="xianyu",
                state=self._state,
                message=self._message,
                has_qr=bool(self._qr_bytes),
                account_id=self._account_id,
                nickname=self._nickname,
                error_code=self._error_code,
                expires_in_sec=0 if self._is_terminal_state(self._state) else rem,
            )

    def qr_bytes(self) -> Optional[bytes]:
        with self._lock:
            return self._qr_bytes

    def _set_state(
        self,
        state: str,
        message: str,
        *,
        qr_bytes: Optional[bytes] = None,
        clear_qr: bool = False,
        account_id: Optional[str] = None,
        nickname: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        with self._lock:
            if self._cancel_event.is_set() and state != "cancelled":
                return
            self._state = state
            self._message = message
            if clear_qr:
                self._qr_bytes = None
            elif qr_bytes is not None:
                self._qr_bytes = qr_bytes
            if account_id is not None:
                self._account_id = account_id
            if nickname is not None:
                self._nickname = nickname
            if error_code is not None:
                self._error_code = error_code

    @staticmethod
    def _find_login_frame(page: Any) -> Any:
        for f in getattr(page, "frames", None) or []:
            url = str(getattr(f, "url", "") or "")
            if "passport.goofish.com/mini_login" in url or "login.taobao.com" in url:
                return f
        return None

    @staticmethod
    def _read_cookies(context: Any) -> Dict[str, str]:
        return {
            c["name"]: c["value"]
            for c in (context.cookies() or [])
            if isinstance(c, dict) and c.get("name") and c.get("value")
        }

    def _capture_qr_png(self, login_frame: Any) -> Optional[bytes]:
        for selector in ("#qrcode-img", "div.qrcode-img", "#qrcode-img canvas"):
            try:
                loc = login_frame.locator(selector).first
                if loc.count() <= 0:
                    continue
                box = loc.bounding_box()
                if not box or box.get("width", 0) < 60 or box.get("height", 0) < 60:
                    continue
                raw = loc.screenshot(type="png")
                if raw:
                    return _format_qr_png(raw)
            except Exception:
                continue
        return None

    def _click_keep_login_if_visible(self, login_frame: Any) -> bool:
        # 必须点击扫码区内的「保持」按钮；若错过倒计时，闲鱼会在该目录永久记住「不保持」
        try:
            btn = login_frame.locator(".qrcode-login button.keep-login-confirm-btn.primary").first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                return True
        except Exception:
            pass
        return False

    def _read_frame_status_text(self, login_frame: Any) -> str:
        try:
            return str(
                login_frame.evaluate("() => (document.body ? document.body.innerText : '').trim()") or ""
            )
        except Exception:
            return ""

    def _run(self) -> None:
        if sync_playwright is None:
            self._set_state(
                "error",
                "Playwright 未安装，无法发起扫码登录",
                clear_qr=True,
                error_code="PLAYWRIGHT_MISSING",
            )
            return

        profile_str = str(self.profile_dir)
        # 每次扫码均从全新的临时目录启动，确保必弹「保持登录状态」且不被历史「不保持」污染
        staging_dir = self.profile_dir.with_name(f"{self.profile_dir.name}.staging_{self.session_id}")
        pending_success: Optional[Dict[str, str]] = None

        try:
            with _acquire_serial_browser_lock(
                user_data_dir=profile_str,
                task_name="qr_login",
                lock_timeout_sec=15.0,
                lock_scope="profile",
            ):
                if self._cancel_event.is_set():
                    return
                staging_dir.mkdir(parents=True, exist_ok=True)
                with sync_playwright() as p:
                    context = None
                    auth_tracker: Optional[Dict[str, Any]] = None
                    on_page_cb = None
                    try:
                        context = launch_persistent_context(
                            p, user_data_dir=str(staging_dir), headless=True
                        )
                        context.set_default_timeout(30_000)
                        context.set_default_navigation_timeout(30_000)
                        page = context.pages[0] if context.pages else context.new_page()
                        auth_tracker = _attach_auth_tracker(page)
                        if hasattr(context, "on") and callable(auth_tracker.get("_on_response_cb")):
                            def _on_new_page(new_p):
                                new_p.on("response", auth_tracker["_on_response_cb"])
                            on_page_cb = _on_new_page
                            context.on("page", on_page_cb)

                        page.goto(GOOFISH_PERSONAL_URL, wait_until="domcontentloaded")
                        page.wait_for_timeout(2500)

                        last_qr_capture = 0.0
                        while not self._cancel_event.is_set():
                            if time.monotonic() >= self.deadline:
                                self._set_state("expired", "二维码已超时，请重新生成", clear_qr=True, error_code="TIMEOUT")
                                return

                            risk = _extract_risk_reasons(page, auth_tracker)
                            if risk:
                                self._set_state(
                                    "risk_blocked",
                                    f"触发闲鱼安全校验（{risk[0]}），已自动停止，请稍后再试",
                                    clear_qr=True,
                                    error_code="RISK_CONTROL_BLOCKED",
                                )
                                return

                            cookies = self._read_cookies(context)
                            login_frame = self._find_login_frame(page)
                            unb = str(cookies.get("unb") or auth_tracker.get("user_id") or "").strip()

                            if login_frame is not None and self._click_keep_login_if_visible(login_frame):
                                self._set_state("scanned", "已扫码，正在确认保持长效登录...")
                                page.wait_for_timeout(600)
                                continue

                            if (
                                unb
                                and cookies.get("cookie2")
                                and not auth_tracker.get("expired_rets")
                                and login_frame is None
                            ):
                                has_persistent_sso = bool(
                                    cookies.get("havana_lgc2_77") or cookies.get("_hvn_lgc_")
                                )
                                settle_deadline = time.monotonic() + 5.0
                                while time.monotonic() < settle_deadline and not self._cancel_event.is_set():
                                    if has_persistent_sso and auth_tracker.get("display_name"):
                                        break
                                    page.wait_for_timeout(300)
                                    lf_now = self._find_login_frame(page)
                                    if lf_now is not None:
                                        self._click_keep_login_if_visible(lf_now)
                                    cookies = self._read_cookies(context)
                                    has_persistent_sso = bool(
                                        cookies.get("havana_lgc2_77") or cookies.get("_hvn_lgc_")
                                    )

                                if not has_persistent_sso:
                                    logger.warning(
                                        f"[闲鱼扫码] 未获取到长效持久化 Cookie (havana_lgc2_77/_hvn_lgc_), owner={self.owner}"
                                    )
                                    self._set_state(
                                        "error",
                                        "未获取到闲鱼长效登录凭证（保持登录未生效），请重新扫码",
                                        clear_qr=True,
                                        error_code="NO_PERSISTENT_SESSION",
                                    )
                                    return

                                page.wait_for_timeout(800)
                                pending_success = {
                                    "account_id": unb,
                                    "nickname": str(auth_tracker.get("display_name") or "").strip(),
                                }
                                break

                            if login_frame is not None:
                                txt = self._read_frame_status_text(login_frame)
                                if "保持登录状态" in txt:
                                    self._set_state("scanned", "已扫码，正在确认保持长效登录...")
                                elif "请在手机上确认登录" in txt or "扫描成功" in txt:
                                    self._set_state("scanned", "已扫码，请在手机闲鱼 App 点击确认登录")
                                elif "二维码已失效" in txt or "已过期" in txt:
                                    self._set_state("expired", "二维码已失效，请点击刷新重扫", clear_qr=True, error_code="QR_EXPIRED")
                                    return
                                elif time.monotonic() - last_qr_capture >= 2.5 or self.qr_bytes() is None:
                                    png = self._capture_qr_png(login_frame)
                                    if png:
                                        last_qr_capture = time.monotonic()
                                        self._set_state(
                                            "waiting_scan",
                                            "请用手机闲鱼 App 扫一扫，或长按保存二维码后在闲鱼相册扫码",
                                            qr_bytes=png,
                                        )

                            page.wait_for_timeout(400)
                    finally:
                        _detach_tracker_from_context(context, auth_tracker, on_page_cb)
                        if context is not None:
                            try:
                                context.close()
                            except Exception:
                                pass

                if pending_success and not self._cancel_event.is_set():
                    _replace_profile_dir(staging_dir, self.profile_dir)
                    unb = pending_success["account_id"]
                    nick = pending_success["nickname"]
                    self._set_state(
                        "success",
                        f"已绑定闲鱼账号：{nick or unb}",
                        clear_qr=True,
                        account_id=unb,
                        nickname=nick,
                    )
        except XianyuLockTimeoutError:
            self._set_state(
                "error",
                "当前账号正在执行状态检测或同步，请几秒后再发起扫码",
                clear_qr=True,
                error_code="LOCK_TIMEOUT",
            )
        except Exception as exc:
            logger.exception(f"[闲鱼扫码登录异常] owner={self.owner}: {exc}")
            self._set_state(
                "error",
                f"闲鱼扫码绑定失败：{exc}",
                clear_qr=True,
                error_code="QR_WORKER_ERROR",
            )
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)


class XianyuChannelAdapter(SellingChannel):
    """闲鱼渠道实现。"""

    channel_code = "xianyu"
    channel_name = "闲鱼"
    capabilities = ChannelCapabilities(
        qr_login=True,
        status_check=True,
        sync_selling=True,
        auto_publish=True,
        auto_downshelf=True,
    )

    _UPLOAD_NAME_RE = re.compile(r"^upl_[0-9a-f]{16}\.jpg$")
    _DEAL_IMAGE_ID_RE = re.compile(r"^dsi_[0-9a-f]{20}$")
    _PUBFAIL_NAME_RE = re.compile(r"^pubfail_\d+_[0-9a-f]{8}\.jpg$")

    def __init__(
        self,
        profiles_root: Path,
        *,
        upload_dir: Optional[Path] = None,
        deal_image_dir: Optional[Path] = None,
        publish_fail_dir: Optional[Path] = None,
        login_timeout_sec: float = QR_LOGIN_TIMEOUT_SEC,
    ) -> None:
        super().__init__(profiles_root)
        self.upload_dir = Path(upload_dir) if upload_dir is not None else None
        self.deal_image_dir = Path(deal_image_dir) if deal_image_dir is not None else None
        self.publish_fail_dir = Path(publish_fail_dir) if publish_fail_dir is not None else None
        self.login_timeout_sec = login_timeout_sec
        self._sessions_lock = threading.Lock()
        self._sessions: Dict[str, _XianyuQrLoginWorker] = {}

    def _prune_inactive_locked(self) -> None:
        if len(self._sessions) <= 12:
            return
        finished = [sid for sid, w in self._sessions.items() if not w.is_active()]
        for sid in finished[: len(self._sessions) - 12]:
            self._sessions.pop(sid, None)

    def start_qr_login(self, username: str) -> QrLoginSnapshot:
        profile_dir = self.profile_dir_for(username)
        with self._sessions_lock:
            self._prune_inactive_locked()
            to_cancel = [w for w in self._sessions.values() if w.owner == username and w.is_active()]
            active_others = sum(
                1 for w in self._sessions.values() if w.owner != username and w.is_active()
            )
            if active_others >= MAX_ACTIVE_QR_SESSIONS:
                raise RuntimeError("当前扫码会话过多，请稍后再试")

        for old_worker in to_cancel:
            old_worker.cancel()
            old_worker.join(timeout=2.0)

        sid = f"qr_{uuid.uuid4().hex[:12]}"
        worker = _XianyuQrLoginWorker(
            session_id=sid,
            owner=username,
            profile_dir=profile_dir,
            timeout_sec=self.login_timeout_sec,
        )
        with self._sessions_lock:
            self._sessions[sid] = worker
        worker.start()
        return worker.snapshot()

    def _get_worker(self, username: str, session_id: str) -> Optional[_XianyuQrLoginWorker]:
        with self._sessions_lock:
            worker = self._sessions.get(str(session_id or "").strip())
            if worker is None or worker.owner != username:
                return None
            return worker

    def get_qr_login_status(self, username: str, session_id: str) -> Optional[QrLoginSnapshot]:
        worker = self._get_worker(username, session_id)
        return worker.snapshot() if worker else None

    def get_qr_image_bytes(self, username: str, session_id: str) -> Optional[bytes]:
        worker = self._get_worker(username, session_id)
        return worker.qr_bytes() if worker else None

    def cancel_qr_login(self, username: str, session_id: Optional[str] = None) -> bool:
        with self._sessions_lock:
            targets = [
                w
                for sid, w in self._sessions.items()
                if w.owner == username and (not session_id or sid == session_id)
            ]
        if not targets:
            return False
        for w in targets:
            w.cancel()
            w.join(timeout=2.0)
        return True

    @staticmethod
    def _friendly_error_message(error_code: str, default_msg: str) -> str:
        code = str(error_code or "").strip().upper()
        if code == "ACCOUNT_OFFLINE":
            return "账号未登录或已掉线，请点击「扫码绑定」重新登录"
        if code == "RISK_CONTROL_BLOCKED":
            return "触发闲鱼安全校验，已自动暂停，请稍后再试"
        if code == "LOCK_TIMEOUT":
            return "当前账号正在同步或检测中，请稍后再试"
        return default_msg

    def check_status(self, username: str) -> ChannelAccountStatus:
        profile_dir = self.profile_dir_for(username)
        res = check_login_status(
            user_data_dir=str(profile_dir),
            lock_timeout_sec=20.0,
            operation_timeout_sec=45.0,
            lock_scope="profile",
        )
        online = bool(res.get("ok"))
        err_code = "" if online else str(res.get("error_code") or "ACCOUNT_OFFLINE")
        msg = "账号在线" if online else self._friendly_error_message(err_code, "账号未登录或已掉线，请重新扫码绑定")
        return ChannelAccountStatus(
            channel=self.channel_code,
            online=online,
            account_id=str(res.get("unb") or "").strip(),
            nickname=str(res.get("nick") or "").strip(),
            error_code=err_code,
            message=msg,
            checked_at=_now_iso(),
        )

    def sync_selling_items(self, username: str) -> ChannelSyncResult:
        profile_dir = self.profile_dir_for(username)
        res = get_selling_items(
            user_data_dir=str(profile_dir),
            lock_timeout_sec=25.0,
            operation_timeout_sec=60.0,
            lock_scope="profile",
        )
        ok = bool(res.get("ok"))
        err_code = "" if ok else str(res.get("error_code") or "SYNC_FAILED")
        msg = "同步完成" if ok else self._friendly_error_message(err_code, "同步在售商品失败，请稍后重试")
        return ChannelSyncResult(
            ok=ok,
            channel=self.channel_code,
            online=ok,
            account_id=str(res.get("unb") or "").strip(),
            nickname=str(res.get("nick") or "").strip(),
            error_code=err_code,
            message=msg,
            online_items=list(res.get("online_items") or []),
            abnormal_items=list(res.get("abnormal_items") or []),
            group_counts=dict(res.get("group_counts") or {}),
            synced_at=_now_iso(),
        )

    def _resolve_publish_images(self, raw_images: Any) -> List[str]:
        """将素材库中的图片列表（远程 URL / 工作台本地上传 / 爆料实拍图）安全解析为可上传路径/URL。"""
        out: List[str] = []
        for entry in (raw_images or []):
            url = str(entry.get("url") if isinstance(entry, dict) else entry or "").strip()
            if not url:
                continue
            if url.startswith(("http://", "https://")):
                out.append(url)
                continue
            if url.startswith("/api/assets/uploads/") and self.upload_dir is not None:
                fname = url.rsplit("/", 1)[-1]
                if not self._UPLOAD_NAME_RE.fullmatch(fname):
                    raise ValueError(f"非法上传图片名: {fname}")
                base = self.upload_dir.resolve()
                cand = (base / fname).resolve()
                if cand.parent != base or not cand.is_file():
                    raise ValueError(f"本地上传图片不存在: {fname}")
                out.append(str(cand))
                continue
            if url.startswith("/api/deals/image/") and self.deal_image_dir is not None:
                img_id = url.rsplit("/", 1)[-1]
                if not self._DEAL_IMAGE_ID_RE.fullmatch(img_id):
                    raise ValueError(f"非法爆料图片ID: {img_id}")
                base = self.deal_image_dir.resolve()
                cand = (base / f"{img_id}.jpg").resolve()
                if cand.parent != base or not cand.is_file():
                    raise ValueError(f"本地爆料图片不存在: {img_id}")
                out.append(str(cand))
                continue
            raise ValueError(f"不支持的图片路径格式: {url}")
        if not out:
            raise ValueError("至少需要 1 张有效商品图片")
        return out

    def publish_item(self, username: str, item_payload: Dict[str, Any]) -> Dict[str, Any]:
        profile_dir = self.profile_dir_for(username)
        raw_price = (
            item_payload.get("price")
            if item_payload.get("price") is not None
            else item_payload.get("sell_price")
        )
        try:
            price = float(raw_price or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return {
                "ok": False,
                "is_offline": False,
                "error_code": "INVALID_PRICE",
                "message": "上架前请先设置大于 0 的标价",
            }
        try:
            images = self._resolve_publish_images(item_payload.get("images"))
            res = publish_item(
                title=str(item_payload.get("title") or "").strip(),
                desc=str(item_payload.get("description") or "").strip(),
                images=images,
                price=price,
                user_data_dir=str(profile_dir),
                lock_timeout_sec=30.0,
                operation_timeout_sec=90.0,
                lock_scope="profile",
                failure_screenshot_dir=str(self.publish_fail_dir) if self.publish_fail_dir is not None else None,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "is_offline": False,
                "error_code": "VALIDATION_ERROR",
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception(f"[闲鱼上架异常] owner={username}: {exc}")
            return {
                "ok": False,
                "is_offline": False,
                "error_code": "PUBLISH_ERROR",
                "message": f"上架失败：{exc}",
            }

        ok = bool(res.get("ok"))
        err_code = "" if ok else str(res.get("error_code") or "PUBLISH_FAILED")
        msg = "上架成功" if ok else self._friendly_error_message(
            err_code, str(res.get("message") or "上架失败，请稍后重试")
        )
        shot_file = str(res.get("screenshot_file") or "").strip()
        shot_url = (
            f"/api/assets/publish-failures/{shot_file}"
            if (not ok and shot_file and self._PUBFAIL_NAME_RE.fullmatch(shot_file))
            else ""
        )
        return {
            "ok": ok,
            "is_offline": bool(res.get("is_offline")),
            "error_code": err_code,
            "message": msg,
            "failure_screenshot_url": shot_url,
            "item_id": str(res.get("item_id") or "").strip(),
            "item_url": str(res.get("item_url") or "").strip(),
            "title": str(res.get("title") or "").strip(),
            "price": str(res.get("price") or f"{price:.2f}"),
            "account_id": str(res.get("unb") or "").strip(),
            "nickname": str(res.get("nick") or "").strip(),
        }

    def downshelf_item(self, username: str, external_item_id: str) -> Dict[str, Any]:
        profile_dir = self.profile_dir_for(username)
        try:
            res = downshelf_item(
                item_id=external_item_id,
                delete=False,
                user_data_dir=str(profile_dir),
                lock_timeout_sec=25.0,
                operation_timeout_sec=60.0,
                lock_scope="profile",
            )
        except ValueError as exc:
            return {
                "ok": False,
                "is_offline": False,
                "error_code": "VALIDATION_ERROR",
                "message": str(exc),
                "item_id": external_item_id,
            }
        except Exception as exc:
            logger.exception(f"[闲鱼下架异常] owner={username} item={external_item_id}: {exc}")
            return {
                "ok": False,
                "is_offline": False,
                "error_code": "DOWNSHELF_ERROR",
                "message": f"下架失败：{exc}",
                "item_id": external_item_id,
            }

        ok = bool(res.get("ok"))
        err_code = "" if ok else str(res.get("error_code") or "DOWNSHELF_FAILED")
        msg = "下架成功" if ok else self._friendly_error_message(
            err_code, str(res.get("message") or "下架失败，请稍后重试")
        )
        return {
            "ok": ok,
            "is_offline": bool(res.get("is_offline")),
            "error_code": err_code,
            "message": msg,
            "item_id": str(res.get("item_id") or external_item_id).strip(),
            "account_id": str(res.get("unb") or "").strip(),
            "nickname": str(res.get("nick") or "").strip(),
        }

    def shutdown(self) -> None:
        with self._sessions_lock:
            workers = list(self._sessions.values())
            self._sessions.clear()
        for w in workers:
            w.cancel()
        for w in workers:
            w.join(timeout=2.0)
