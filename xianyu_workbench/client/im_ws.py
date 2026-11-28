# -*- coding: utf-8 -*-
"""闲鱼 (Goofish) 轻量级长连接消息中枢与静默无头签发凭证模块。

核心原则（第一性原理 · 最小职责）：
1. 控制面（mtop.taobao.idlemessage.pc.login.token）走真实无头浏览器沙盒签发，零逆向脆弱计算；
2. 数据面（wss://wss-goofish.dingtalk.com/）在单进程轻量协程中多路复用运行：
   - 20s 心跳实时感知在线状态；
   - 主动推送 (/s/sync, /s/vulcan) 先持久化落盘再回 ACK 与 /r/SyncStatus/ackDiff（绝不消手机端未读红点）；
   - 发送回复强制过合规门禁、控速与拟人延时，等待平台真实送达回执（超时则查线上记录二次核验）；
   - 任何滑块/掉线/重连耗尽立即按账号独立 Fail-Closed 熔断，绝不死循环硬撞，也绝不连坐其它账号。
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

from .seller import validate_reply_text
from .session import (
    GOOFISH_IM_URL,
    XianyuLockTimeoutError,
    XianyuOfflineError,
    _browser_session_guard,
    _resolve_desktop_user_agent,
    _verify_account_online,
    clean_browser_cache,
)

logger = logging.getLogger("xianyu_workbench.im_ws")

GOOFISH_WS_URL = "wss://wss-goofish.dingtalk.com/"
DEFAULT_IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"
DEFAULT_MAX_UINT64_CURSOR = 9007199254740991

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CHROME_MAJOR_RE = re.compile(r"Chrome/(\d+)")
_GENERIC_REMINDER_TITLES = frozenset({"交易消息", "你有一条新消息", "系统消息", "通知消息"})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _next_mid() -> str:
    return f"{random.randint(100, 999)}{int(time.time() * 1000)} 0"


def _build_ding_ua(desktop_ua: str) -> str:
    m = _CHROME_MAJOR_RE.search(desktop_ua or "")
    major = m.group(1) if m else "134"
    return (
        f"{desktop_ua} DingTalk(2.2.0) OS(Windows/10) "
        f"Browser(Chrome/{major}.0.0.0) DingWeb/2.2.0 IMPaaS DingWeb/2.2.0"
    )


def _strip_domain(uid_or_pair: Any) -> str:
    s = str(uid_or_pair or "").strip()
    if "@" in s:
        s = s.split("@", 1)[0].strip()
    return s


def _parse_peer_user_id(pair_first: Any, pair_second: Any, ext: Dict[str, Any], my_uid: str) -> str:
    p1 = _strip_domain(pair_first)
    p2 = _strip_domain(pair_second)
    me = str(my_uid or "").strip()
    for cand in (p1, p2):
        if cand and cand != me and cand != "0":
            return cand
    return str(ext.get("extUserId") or "").strip()


def _decode_custom_data(custom_data: Any) -> Optional[Dict[str, Any]]:
    raw = str(custom_data or "").strip()
    if not raw:
        return None
    try:
        decoded_bytes = base64.b64decode(raw)
        return json.loads(decoded_bytes.decode("utf-8", errors="replace"))
    except Exception:
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None


def normalize_lwp_message(model_or_wrap: Dict[str, Any], my_user_id: str) -> Optional[Dict[str, Any]]:
    """将单条 LWP userMessageModel 规范化为工作台消息字典。"""
    if not isinstance(model_or_wrap, dict):
        return None
    msg = model_or_wrap.get("message") if isinstance(model_or_wrap.get("message"), dict) else model_or_wrap
    if not isinstance(msg, dict):
        return None

    ext = msg.get("extension") if isinstance(msg.get("extension"), dict) else {}
    content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
    message_id = str(msg.get("messageId") or "").strip()
    client_uuid = str(msg.get("uuid") or "").strip()
    created_at_ms = int(msg.get("createAt") or 0)
    sender_uid = _strip_domain(
        ext.get("senderUserId")
        or (msg.get("sender") or {}).get("uid")
        or msg.get("senderId")
        or ""
    )
    sender = "me" if (my_user_id and sender_uid == str(my_user_id).strip()) else "peer"

    outer_ct = content.get("contentType")
    msg_type = "text"
    text = ""
    image_url = ""

    if outer_ct == 1 and isinstance(content.get("text"), dict):
        text = str(content["text"].get("content") or content["text"].get("text") or "").strip()
    elif isinstance(content.get("custom"), dict):
        cust = content["custom"]
        summary = str(cust.get("summary") or "").strip()
        inner = _decode_custom_data(cust.get("data"))
        if isinstance(inner, dict):
            ict = inner.get("contentType")
            if ict == 1:
                msg_type = "text"
                text_obj = inner.get("text") if isinstance(inner.get("text"), dict) else {}
                text = str(text_obj.get("text") or text_obj.get("content") or summary).strip()
            elif ict == 2:
                msg_type = "image"
                img_obj = inner.get("image") if isinstance(inner.get("image"), dict) else {}
                pics = img_obj.get("pics") if isinstance(img_obj.get("pics"), list) else []
                if pics and isinstance(pics[0], dict):
                    image_url = str(pics[0].get("url") or "").strip()
                text = "[图片]" if not summary else summary
            elif ict == 14:
                msg_type = "system"
                tip_obj = inner.get("tip") if isinstance(inner.get("tip"), dict) else {}
                raw_tip = str(tip_obj.get("tip") or summary or ext.get("reminderContent") or "").strip()
                text = _HTML_TAG_RE.sub("", raw_tip).strip()
            else:
                msg_type = "card"
                text = summary or str(ext.get("reminderContent") or "").strip()
        else:
            msg_type = "card" if summary else "text"
            text = summary or str(ext.get("reminderContent") or "").strip()
    else:
        text = str(ext.get("reminderContent") or "").strip()
        msg_type = "system" if text else "text"

    if not text and not image_url:
        return None

    created_dt = (
        datetime.fromtimestamp(created_at_ms / 1000.0, tz=timezone.utc)
        if created_at_ms > 0
        else _now_utc()
    )
    reminder_title = str(ext.get("reminderTitle") or "").strip()
    return {
        "message_id": message_id or client_uuid,
        "client_uuid": client_uuid,
        "sender": sender,
        "sender_uid": sender_uid,
        "msg_type": msg_type,
        "text": text,
        "image_url": image_url,
        "created_at_ms": created_at_ms,
        "created_at": created_dt,
        "delivery_status": "sent",
        "reminder_title": reminder_title,
        "error": "",
    }


def parse_conversations_payload(body_or_list: Any, my_user_id: str) -> List[Dict[str, Any]]:
    """从 /r/Conversation/listNewestPagination 响应体中提取我方商品的买家会话摘要。"""
    if isinstance(body_or_list, dict):
        user_convs = body_or_list.get("userConvs") or []
    elif isinstance(body_or_list, list):
        user_convs = body_or_list
    else:
        user_convs = []

    my_uid = str(my_user_id or "").strip()
    out: List[Dict[str, Any]] = []
    for entry in user_convs:
        if not isinstance(entry, dict):
            continue
        sc = entry.get("singleChatUserConversation")
        if not isinstance(sc, dict) or sc.get("visible") != 1:
            continue
        cm = sc.get("singleChatConversation")
        if not isinstance(cm, dict) or str(cm.get("bizType") or "") != "1":
            continue
        ext = cm.get("extension") if isinstance(cm.get("extension"), dict) else {}
        item_id = str(ext.get("itemId") or "").strip()
        seller_id = str(ext.get("itemSellerId") or "").strip()
        cid = str(cm.get("cid") or "").strip()
        if not cid or not item_id:
            continue
        # 仅监控别人咨询「我方商品」的会话（若 seller_id 存在且不等于自己则属于我去问别人，跳过）
        if seller_id and my_uid and seller_id != my_uid:
            continue

        peer_uid = _parse_peer_user_id(cm.get("pairFirst"), cm.get("pairSecond"), ext, my_uid)
        if not peer_uid:
            continue

        last_msg_raw = sc.get("lastMessage") if isinstance(sc.get("lastMessage"), dict) else None
        last_msg_norm = normalize_lwp_message(last_msg_raw, my_uid) if last_msg_raw else None
        mod_ms = int(sc.get("modifyTime") or (last_msg_norm or {}).get("created_at_ms") or 0)
        mod_dt = datetime.fromtimestamp(mod_ms / 1000.0, tz=timezone.utc) if mod_ms > 0 else _now_utc()

        peer_nick = ""
        if last_msg_norm and last_msg_norm.get("sender") == "peer":
            rt = str(last_msg_norm.get("reminder_title") or "").strip()
            if rt and rt not in _GENERIC_REMINDER_TITLES:
                peer_nick = rt
        if not peer_nick:
            peer_nick = f"买家{peer_uid[-4:]}" if len(peer_uid) >= 4 else f"买家{peer_uid}"

        out.append({
            "cid": cid,
            "external_item_id": item_id,
            "item_title": str(ext.get("itemTitle") or "").strip(),
            "item_pic": str(ext.get("itemMainPic") or "").strip(),
            "item_url": f"https://www.goofish.com/item?id={item_id}",
            "peer_user_id": peer_uid,
            "peer_nick": peer_nick,
            "unread_count": int(sc.get("redPoint") or 0),
            "modify_time_ms": mod_ms,
            "last_message_at": mod_dt,
            "last_message_id": str(((last_msg_raw or {}).get("message") or {}).get("messageId") or ""),
            "last_message_preview": str((last_msg_norm or {}).get("text") or ""),
            "last_message": last_msg_norm,
        })
    return out


def parse_messages_payload(body: Any, my_user_id: str) -> Tuple[List[Dict[str, Any]], str]:
    """解析 /r/MessageManager/listUserMessages 返回的完整消息列表并提取买家真实脱敏昵称。"""
    models = (body.get("userMessageModels") or []) if isinstance(body, dict) else []
    msgs: List[Dict[str, Any]] = []
    peer_nick = ""
    for raw in models:
        norm = normalize_lwp_message(raw, my_user_id)
        if norm is None:
            continue
        if not peer_nick and norm.get("sender") == "peer":
            rt = str(norm.get("reminder_title") or "").strip()
            if rt and rt not in _GENERIC_REMINDER_TITLES:
                peer_nick = rt
        norm.pop("reminder_title", None)
        msgs.append(norm)
    msgs.sort(key=lambda m: (int(m.get("created_at_ms") or 0), str(m.get("message_id") or "")))
    return msgs, peer_nick


def build_text_send_payload(
    *,
    cid: str,
    peer_user_id: str,
    my_user_id: str,
    text: str,
    client_uuid: str = "",
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """构造实时校验过的 /r/MessageSend/sendByReceiverScope 参数元组。"""
    uid_val = client_uuid or f"-{int(time.time() * 1000)}{random.randint(10000, 99999)}"
    inner = {"contentType": 1, "text": {"text": text}}
    b64_data = base64.b64encode(
        json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    msg_obj: Dict[str, Any] = {
        "uuid": uid_val,
        "cid": cid,
        "conversationType": 1,
        "content": {
            "contentType": 101,
            "custom": {"type": 1, "data": b64_data},
        },
        "redPointPolicy": 0,
        "extension": {"extJson": "{}"},
        "ctx": {"appVersion": "1.0", "platform": "web"},
        "mtags": {},
        "msgReadStatusSetting": 1,
    }
    scope_obj: Dict[str, Any] = {
        "actualReceivers": [
            f"{_strip_domain(peer_user_id)}@goofish",
            f"{_strip_domain(my_user_id)}@goofish",
        ]
    }
    return uid_val, msg_obj, scope_obj


def mint_im_credentials(
    user_data_dir: str,
    *,
    lock_timeout_sec: float = 30.0,
    operation_timeout_sec: float = 35.0,
) -> Dict[str, Any]:
    """
    通过真实静默无头 Chrome 打开 goofish.com/im 签发并截获最新 WS 通行证（Zero 逆向自算签名）：
    返回 {"ok": True, "user_id": ..., "nickname": ..., "access_token": ..., "device_id": ..., "app_key": ...}。
    遇掉线或风控滑块严格 Fail-Closed 返回 error_code="ACCOUNT_OFFLINE" / "RISK_CONTROL_BLOCKED"。
    """
    clean_browser_cache(user_data_dir)
    minted: Dict[str, Any] = {}
    desktop_ua = _resolve_desktop_user_agent()
    try:
        with _browser_session_guard(
            user_data_dir=user_data_dir,
            task_name="im_mint_token",
            debug=False,
            lock_timeout_sec=lock_timeout_sec,
            operation_timeout_sec=operation_timeout_sec,
            lock_scope="profile",
        ) as (context, page, auth_tracker):
            def _on_resp(resp: Any) -> None:
                url = getattr(resp, "url", "") or ""
                if "mtop.taobao.idlemessage.pc.login.token" not in url:
                    return
                try:
                    post = urllib.parse.parse_qs(resp.request.post_data or "")
                    req_data = json.loads((post.get("data") or ["{}"])[0])
                    body = resp.json()
                    data = (body.get("data") or {}) if isinstance(body, dict) else {}
                    if data.get("accessToken"):
                        minted["device_id"] = str(req_data.get("deviceId") or "").strip()
                        minted["app_key"] = str(req_data.get("appKey") or DEFAULT_IM_APP_KEY).strip()
                        minted["access_token"] = str(data.get("accessToken") or "").strip()
                        minted["expired_ms"] = int(data.get("accessTokenExpiredTime") or 86400000)
                except Exception:
                    pass

            page.on("response", _on_resp)
            page.goto(GOOFISH_IM_URL, wait_until="domcontentloaded")
            for _ in range(35):
                if (
                    minted.get("access_token")
                    or auth_tracker.get("risk_rets")
                    or auth_tracker.get("expired_rets")
                ):
                    break
                page.wait_for_timeout(250)

            unb = _verify_account_online(
                context, page, auth_tracker, user_data_dir, task_name="im_mint_token"
            )
            nick = str(auth_tracker.get("display_name") or "").strip()
            if not minted.get("access_token"):
                return {
                    "ok": False,
                    "error_code": "TOKEN_MINT_TIMEOUT",
                    "message": "未截获到闲鱼消息通行证，请稍后重试",
                    "user_id": unb,
                    "nickname": nick,
                }
            return {
                "ok": True,
                "user_id": unb,
                "nickname": nick,
                "access_token": minted["access_token"],
                "device_id": minted.get("device_id") or "",
                "app_key": minted.get("app_key") or DEFAULT_IM_APP_KEY,
                "expired_ms": int(minted.get("expired_ms") or 86400000),
                "desktop_ua": desktop_ua,
                "ding_ua": _build_ding_ua(desktop_ua),
            }
    except XianyuOfflineError as exc:
        return {
            "ok": False,
            "error_code": exc.error_code,
            "message": exc.reason,
        }
    except XianyuLockTimeoutError as exc:
        return {
            "ok": False,
            "error_code": "LOCK_TIMEOUT",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "MINT_ERROR",
            "message": str(exc),
        }


async def _connect_goofish_ws(headers: Dict[str, str]):
    if websockets is None:  # pragma: no cover
        raise RuntimeError("未安装 websockets 库")
    params = inspect.signature(websockets.connect).parameters
    kw: Dict[str, Any] = {"ping_interval": None, "max_size": 8 * 1024 * 1024}
    if "additional_headers" in params:
        kw["additional_headers"] = headers
    else:
        kw["extra_headers"] = headers
    return await websockets.connect(GOOFISH_WS_URL, **kw)


@dataclass
class _AccountRuntime:
    account_key: str
    profile_dir: str
    owner: str
    channel: str
    on_conversations: Callable[[str, List[Dict[str, Any]]], None]
    on_state_change: Callable[[str, Dict[str, Any]], None]

    state: str = "stopped"  # stopped | recovering | online | attention | paused_risk | offline
    message: str = "尚未启动"
    error_code: str = ""
    user_id: str = ""
    nickname: str = ""
    last_sync_at: Optional[datetime] = None

    credentials: Optional[Dict[str, Any]] = None
    token_minted_mono: float = 0.0
    sid: str = ""
    ws: Any = None
    task: Optional[asyncio.Task] = None
    stop_requested: bool = False
    send_lock: Optional[asyncio.Lock] = None
    mint_lock: Optional[asyncio.Lock] = None
    pending_rpcs: Dict[str, asyncio.Future] = field(default_factory=dict)
    conv_fingerprints: Dict[str, str] = field(default_factory=dict)
    send_timestamps: List[float] = field(default_factory=list)


class XianyuIMHub:
    """多账号共享单线程轻量级 LWP WebSocket 守护中枢（可独立脱机单测）。"""

    def __init__(
        self,
        *,
        mint_fn: Callable[[str], Dict[str, Any]] = mint_im_credentials,
        ws_connect_fn: Callable[[Dict[str, str]], Any] = _connect_goofish_ws,
        heartbeat_interval_sec: float = 20.0,
        heartbeat_timeout_sec: float = 10.0,
        delta_sync_interval_sec: float = 120.0,
        token_refresh_interval_sec: float = 14 * 3600.0,
        max_recovering_sec: float = 180.0,
        send_delay_range: Tuple[float, float] = (1.5, 3.5),
        max_sends_per_minute: int = 15,
    ) -> None:
        self._mint_fn = mint_fn
        self._ws_connect_fn = ws_connect_fn
        self._heartbeat_interval_sec = max(1.0, float(heartbeat_interval_sec))
        self._heartbeat_timeout_sec = max(1.0, float(heartbeat_timeout_sec))
        self._delta_sync_interval_sec = max(5.0, float(delta_sync_interval_sec))
        self._token_refresh_interval_sec = max(60.0, float(token_refresh_interval_sec))
        self._max_recovering_sec = max(5.0, float(max_recovering_sec))
        lo, hi = send_delay_range
        self._send_delay_range = (max(0.0, float(lo)), max(0.0, float(hi)))
        self._max_sends_per_minute = max(1, int(max_sends_per_minute))

        self._guard = threading.Lock()
        self._accounts: Dict[str, _AccountRuntime] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_ev = threading.Event()
        self._shutting_down = False

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._shutting_down:
                raise RuntimeError("IM 服务正在关闭")
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._ready_ev.clear()

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._ready_ev.set()
                try:
                    loop.run_forever()
                finally:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()

            t = threading.Thread(target=_runner, name="xy-im-ws-hub", daemon=True)
            self._thread = t
            t.start()
        self._ready_ev.wait(timeout=5.0)
        assert self._loop is not None
        return self._loop

    def ensure_started(
        self,
        account_key: str,
        *,
        profile_dir: str,
        owner: str,
        channel: str,
        on_conversations: Callable[[str, List[Dict[str, Any]]], None],
        on_state_change: Callable[[str, Dict[str, Any]], None],
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(
            self._async_ensure_started(
                account_key=account_key,
                profile_dir=profile_dir,
                owner=owner,
                channel=channel,
                on_conversations=on_conversations,
                on_state_change=on_state_change,
                force_refresh=force_refresh,
            ),
            loop,
        )
        return fut.result(timeout=5.0)

    def stop_account(self, account_key: str, *, timeout_sec: float = 5.0) -> None:
        with self._guard:
            loop = self._loop
            rt = self._accounts.get(account_key)
        if loop is None or rt is None or loop.is_closed():
            return
        fut = asyncio.run_coroutine_threadsafe(self._async_stop_account(rt), loop)
        try:
            fut.result(timeout=timeout_sec)
        except Exception:
            pass

    def sync_now(self, account_key: str, *, timeout_sec: float = 12.0) -> Dict[str, Any]:
        with self._guard:
            loop = self._loop
            rt = self._accounts.get(account_key)
        if loop is None or rt is None or loop.is_closed():
            return {"ok": False, "message": "消息监控尚未启动"}
        fut = asyncio.run_coroutine_threadsafe(self._async_sync_conversations(rt), loop)
        return fut.result(timeout=timeout_sec)

    def send_message(
        self,
        account_key: str,
        *,
        cid: str,
        peer_user_id: str,
        text: str,
        timeout_sec: float = 25.0,
    ) -> Dict[str, Any]:
        ok_txt, clean_or_err = validate_reply_text(text)
        if not ok_txt:
            return {
                "ok": False,
                "delivery_status": "failed",
                "error_code": "INVALID_REPLY_CONTENT",
                "message": clean_or_err,
            }
        with self._guard:
            loop = self._loop
            rt = self._accounts.get(account_key)
        if loop is None or rt is None or loop.is_closed():
            return {
                "ok": False,
                "delivery_status": "failed",
                "error_code": "IM_NOT_RUNNING",
                "message": "该账号实时消息监控未运行，请先检测在线或重连",
            }
        fut = asyncio.run_coroutine_threadsafe(
            self._async_send_message(rt, cid=cid, peer_user_id=peer_user_id, text=clean_or_err),
            loop,
        )
        return fut.result(timeout=timeout_sec)

    def get_status(self, account_key: str) -> Dict[str, Any]:
        with self._guard:
            rt = self._accounts.get(account_key)
            if rt is None:
                return {
                    "im_state": "stopped",
                    "im_message": "尚未启动实时监控",
                    "im_error_code": "",
                    "im_last_sync_at": "",
                    "user_id": "",
                    "nickname": "",
                }
            return {
                "im_state": rt.state,
                "im_message": rt.message,
                "im_error_code": rt.error_code,
                "im_last_sync_at": rt.last_sync_at.isoformat().replace("+00:00", "Z") if rt.last_sync_at else "",
                "user_id": rt.user_id,
                "nickname": rt.nickname,
            }

    def shutdown(self, timeout_sec: float = 5.0) -> None:
        with self._guard:
            self._shutting_down = True
            loop = self._loop
            thread = self._thread
            rts = list(self._accounts.values())
        if loop is not None and not loop.is_closed():
            async def _stop_all() -> None:
                for rt in rts:
                    await self._async_stop_account(rt)

            try:
                fut = asyncio.run_coroutine_threadsafe(_stop_all(), loop)
                fut.result(timeout=max(1.0, float(timeout_sec)))
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, float(timeout_sec)))
        with self._guard:
            self._loop = None
            self._thread = None
            self._shutting_down = False

    def _emit_state(
        self,
        rt: _AccountRuntime,
        state: str,
        message: str,
        *,
        error_code: str = "",
    ) -> None:
        rt.state = state
        rt.message = message
        rt.error_code = error_code
        try:
            rt.on_state_change(
                rt.account_key,
                {
                    "im_state": state,
                    "im_message": message,
                    "im_error_code": error_code,
                    "im_last_sync_at": rt.last_sync_at,
                    "user_id": rt.user_id,
                    "nickname": rt.nickname,
                },
            )
        except Exception as exc:
            logger.debug("[IM状态回调异常] key=%s: %s", rt.account_key, exc)

    async def _async_ensure_started(
        self,
        *,
        account_key: str,
        profile_dir: str,
        owner: str,
        channel: str,
        on_conversations: Callable[[str, List[Dict[str, Any]]], None],
        on_state_change: Callable[[str, Dict[str, Any]], None],
        force_refresh: bool,
    ) -> Dict[str, Any]:
        rt = self._accounts.get(account_key)
        if rt is None:
            rt = _AccountRuntime(
                account_key=account_key,
                profile_dir=profile_dir,
                owner=owner,
                channel=channel,
                on_conversations=on_conversations,
                on_state_change=on_state_change,
                send_lock=asyncio.Lock(),
                mint_lock=asyncio.Lock(),
            )
            self._accounts[account_key] = rt
        else:
            rt.profile_dir = profile_dir
            rt.on_conversations = on_conversations
            rt.on_state_change = on_state_change

        if force_refresh:
            await self._async_stop_account(rt)
            rt.credentials = None

        if rt.task is None or rt.task.done():
            rt.stop_requested = False
            self._emit_state(rt, "recovering", "正在连接闲鱼实时消息通道...")
            rt.task = asyncio.create_task(self._account_supervisor_loop(rt))

        return {
            "im_state": rt.state,
            "im_message": rt.message,
            "im_error_code": rt.error_code,
        }

    async def _async_stop_account(self, rt: _AccountRuntime) -> None:
        rt.stop_requested = True
        self._fail_pending_rpcs(rt, "连接已停止")
        ws = rt.ws
        rt.ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        task = rt.task
        rt.task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if rt.state not in ("paused_risk", "offline"):
            rt.state = "stopped"
            rt.message = "实时监控已暂停"

    @staticmethod
    def _fail_pending_rpcs(rt: _AccountRuntime, reason: str) -> None:
        for mid, fut in list(rt.pending_rpcs.items()):
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
            rt.pending_rpcs.pop(mid, None)

    async def _ensure_credentials(self, rt: _AccountRuntime, *, force: bool = False) -> Dict[str, Any]:
        assert rt.mint_lock is not None
        async with rt.mint_lock:
            age = time.monotonic() - rt.token_minted_mono
            if not force and rt.credentials and age < self._token_refresh_interval_sec:
                return {"ok": True, **rt.credentials}
            res = await asyncio.to_thread(self._mint_fn, rt.profile_dir)
            if not res.get("ok"):
                return res
            rt.credentials = {
                "user_id": str(res.get("user_id") or "").strip(),
                "nickname": str(res.get("nickname") or "").strip(),
                "access_token": str(res.get("access_token") or "").strip(),
                "device_id": str(res.get("device_id") or "").strip(),
                "app_key": str(res.get("app_key") or DEFAULT_IM_APP_KEY).strip(),
                "desktop_ua": str(res.get("desktop_ua") or _resolve_desktop_user_agent()),
                "ding_ua": str(res.get("ding_ua") or _build_ding_ua(_resolve_desktop_user_agent())),
            }
            rt.user_id = rt.credentials["user_id"] or rt.user_id
            rt.nickname = rt.credentials["nickname"] or rt.nickname
            rt.token_minted_mono = time.monotonic()
            return {"ok": True, **rt.credentials}

    async def _send_rpc(self, rt: _AccountRuntime, packet: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        ws = rt.ws
        if ws is None:
            raise ConnectionError("长连接未就绪")
        headers = packet.setdefault("headers", {})
        mid = str(headers.get("mid") or _next_mid())
        headers["mid"] = mid
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        rt.pending_rpcs[mid] = fut
        try:
            await ws.send(json.dumps(packet, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            rt.pending_rpcs.pop(mid, None)

    async def _run_session_once(self, rt: _AccountRuntime) -> str:
        """建立并维持一轮 WS 会话。返回 'refresh' / 'reconnect' / 'fatal_offline' / 'fatal_risk'。"""
        creds = await self._ensure_credentials(rt, force=(rt.credentials is None))
        if not creds.get("ok"):
            err_code = str(creds.get("error_code") or "MINT_FAILED")
            msg = str(creds.get("message") or "获取消息通行证失败")
            if err_code == "RISK_CONTROL_BLOCKED":
                self._emit_state(rt, "paused_risk", msg, error_code=err_code)
                return "fatal_risk"
            if err_code == "ACCOUNT_OFFLINE":
                self._emit_state(rt, "offline", msg, error_code=err_code)
                return "fatal_offline"
            rt.credentials = None
            return "reconnect"

        headers = {"Origin": "https://www.goofish.com", "User-Agent": creds["desktop_ua"]}
        ws = await self._ws_connect_fn(headers)
        rt.ws = ws
        try:
            reg_mid = _next_mid()
            await ws.send(json.dumps({
                "lwp": "/reg",
                "headers": {
                    "cache-header": "app-key token ua wv",
                    "app-key": creds["app_key"],
                    "token": creds["access_token"],
                    "ua": creds["ding_ua"],
                    "dt": "j",
                    "wv": "im:3,au:3,sy:6",
                    "sync": "0,0;0;0;",
                    "did": creds["device_id"],
                    "mid": reg_mid,
                },
            }))

            reg_resp: Optional[Dict[str, Any]] = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.monotonic()))
                pkt = json.loads(raw)
                if (pkt.get("headers") or {}).get("mid") == reg_mid:
                    reg_resp = pkt
                    break
            if not reg_resp or int(reg_resp.get("code") or 0) != 200:
                code = (reg_resp or {}).get("code")
                rt.credentials = None
                return "reconnect" if code != 401 else "refresh"

            rt.sid = str((reg_resp.get("headers") or {}).get("sid") or "").strip()
            rt.last_sync_at = _now_utc()
            self._emit_state(rt, "online", "实时消息监控中", error_code="")

            reader_task = asyncio.create_task(self._ws_reader_loop(rt, ws, creds))
            sync_pending_task = asyncio.create_task(self._initial_sync(rt))
            hb_task = asyncio.create_task(self._heartbeat_and_maintenance_loop(rt, creds))
            active_tasks = [reader_task, sync_pending_task, hb_task]
            try:
                done, pending = await asyncio.wait(
                    active_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # initial_sync 结束后继续等 reader 或 hb 结束
                if sync_pending_task in done and reader_task in pending and hb_task in pending:
                    if not sync_pending_task.cancelled():
                        _ = sync_pending_task.exception()
                    done2, pending = await asyncio.wait(
                        [reader_task, hb_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    done = done2
                for p in pending:
                    p.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                for d in done:
                    if d.cancelled():
                        continue
                    exc = d.exception()
                    if isinstance(exc, RuntimeError) and str(exc) == "NEED_TOKEN_REFRESH":
                        rt.credentials = None
                        return "refresh"
                return "reconnect"
            finally:
                for t in active_tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*active_tasks, return_exceptions=True)
        finally:
            self._fail_pending_rpcs(rt, "长连接已断开")
            rt.ws = None
            try:
                await ws.close()
            except Exception:
                pass

    async def _initial_sync(self, rt: _AccountRuntime) -> None:
        try:
            state_resp = await self._send_rpc(
                rt,
                {"lwp": "/r/SyncStatus/getState", "body": [{"topic": "sync"}]},
                timeout=8.0,
            )
            if int(state_resp.get("code") or 0) == 200 and isinstance(state_resp.get("body"), dict):
                await self._send_rpc(
                    rt,
                    {"lwp": "/r/SyncStatus/ackDiff", "body": [state_resp["body"]]},
                    timeout=8.0,
                )
        except Exception:
            pass
        await self._async_sync_conversations(rt, force_all=True)

    async def _ws_reader_loop(self, rt: _AccountRuntime, ws: Any, creds: Dict[str, Any]) -> None:
        sync_trigger_pending = False
        bg_tasks: set[asyncio.Task] = set()

        async def _persist_then_ack(mid: str, has_data: bool, max_pts: Any) -> None:
            nonlocal sync_trigger_pending
            try:
                if has_data and not sync_trigger_pending:
                    sync_trigger_pending = True
                    try:
                        sync_res = await self._async_sync_conversations(rt)
                        if not sync_res.get("ok"):
                            return
                    finally:
                        sync_trigger_pending = False

                # Persist-Before-Ack：仅在本地落盘同步完成后再向服务端回包确认
                await ws.send(json.dumps({
                    "code": 200,
                    "headers": {
                        "app-key": creds["app_key"],
                        "mid": mid,
                        "ua": creds["ding_ua"],
                        "sid": rt.sid,
                    },
                }))
                if has_data and max_pts:
                    await ws.send(json.dumps({
                        "lwp": "/r/SyncStatus/ackDiff",
                        "headers": {"mid": _next_mid()},
                        "body": [{"topic": "sync", "pts": max_pts, "timestamp": int(time.time() * 1000)}],
                    }))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        try:
            while not rt.stop_requested:
                try:
                    raw = await ws.recv()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if rt.stop_requested:
                        break
                    raise
                pkt = json.loads(raw)
                headers = pkt.get("headers") if isinstance(pkt.get("headers"), dict) else {}
                mid = str(headers.get("mid") or "")
                lwp = str(pkt.get("lwp") or "")

                if "code" in pkt and mid in rt.pending_rpcs:
                    fut = rt.pending_rpcs.pop(mid)
                    if not fut.done():
                        fut.set_result(pkt)
                    continue

                if lwp in ("/s/sync", "/s/vulcan"):
                    body = pkt.get("body") if isinstance(pkt.get("body"), dict) else {}
                    pkg = body.get("syncPushPackage") if isinstance(body.get("syncPushPackage"), dict) else {}
                    data_list = pkg.get("data") or []
                    max_pts = pkg.get("maxPts") or pkg.get("maxHighPts")
                    t = asyncio.create_task(_persist_then_ack(mid, bool(data_list), max_pts))
                    bg_tasks.add(t)
                    t.add_done_callback(bg_tasks.discard)
        finally:
            for t in list(bg_tasks):
                t.cancel()
            if bg_tasks:
                await asyncio.gather(*bg_tasks, return_exceptions=True)

    async def _heartbeat_and_maintenance_loop(self, rt: _AccountRuntime, _creds: Dict[str, Any]) -> None:
        missed = 0
        last_delta = time.monotonic()
        while not rt.stop_requested:
            await asyncio.sleep(self._heartbeat_interval_sec)
            if rt.stop_requested:
                return
            if time.monotonic() - rt.token_minted_mono >= self._token_refresh_interval_sec:
                raise RuntimeError("NEED_TOKEN_REFRESH")
            try:
                hb = await self._send_rpc(rt, {"lwp": "/!"}, timeout=self._heartbeat_timeout_sec)
                if int(hb.get("code") or 0) == 200:
                    missed = 0
                else:
                    missed += 1
            except Exception:
                missed += 1
            if missed >= 2:
                raise ConnectionError("心跳连续超时")

            if time.monotonic() - last_delta >= self._delta_sync_interval_sec:
                last_delta = time.monotonic()
                await self._async_sync_conversations(rt)

    async def _async_sync_conversations(self, rt: _AccountRuntime, *, force_all: bool = False) -> Dict[str, Any]:
        if rt.ws is None:
            return {"ok": False, "message": "长连接未就绪"}
        try:
            resp = await self._send_rpc(
                rt,
                {"lwp": "/r/Conversation/listNewestPagination", "body": [DEFAULT_MAX_UINT64_CURSOR, 50]},
                timeout=10.0,
            )
            if int(resp.get("code") or 0) != 200:
                return {"ok": False, "message": f"会话拉取返回 {resp.get('code')}"}
            convs = parse_conversations_payload(resp.get("body"), rt.user_id)
            enriched: List[Dict[str, Any]] = []
            for conv in convs:
                fp = f"{conv['modify_time_ms']}:{conv['last_message_id']}:{conv['unread_count']}"
                if not force_all and rt.conv_fingerprints.get(conv["cid"]) == fp:
                    continue
                msg_resp = await self._send_rpc(
                    rt,
                    {
                        "lwp": "/r/MessageManager/listUserMessages",
                        "body": [conv["cid"], False, DEFAULT_MAX_UINT64_CURSOR, 30, False],
                    },
                    timeout=10.0,
                )
                if int(msg_resp.get("code") or 0) != 200:
                    conv["has_gap"] = True
                    conv["messages"] = [conv["last_message"]] if conv.get("last_message") else []
                    self._emit_state(rt, "attention", f"会话 {conv['cid']} 历史消息存在补齐缺口", error_code="MSG_GAP")
                else:
                    msgs, extracted_nick = parse_messages_payload(msg_resp.get("body"), rt.user_id)
                    if extracted_nick:
                        conv["peer_nick"] = extracted_nick
                    conv["has_gap"] = False
                    conv["messages"] = msgs
                    rt.conv_fingerprints[conv["cid"]] = fp
                enriched.append(conv)

            if enriched:
                await asyncio.to_thread(rt.on_conversations, rt.account_key, enriched)
            rt.last_sync_at = _now_utc()
            if rt.state == "attention" and all(not c.get("has_gap") for c in enriched):
                self._emit_state(rt, "online", "实时消息监控中", error_code="")
            return {"ok": True, "updated": len(enriched)}
        except Exception as exc:
            logger.debug("[IM增量对账失败] key=%s: %s", rt.account_key, exc)
            return {"ok": False, "message": str(exc)}

    async def _async_send_message(
        self,
        rt: _AccountRuntime,
        *,
        cid: str,
        peer_user_id: str,
        text: str,
    ) -> Dict[str, Any]:
        assert rt.send_lock is not None
        async with rt.send_lock:
            now_mono = time.monotonic()
            rt.send_timestamps = [t for t in rt.send_timestamps if now_mono - t < 60.0]
            if len(rt.send_timestamps) >= self._max_sends_per_minute:
                return {
                    "ok": False,
                    "delivery_status": "failed",
                    "error_code": "RATE_LIMITED",
                    "message": "该账号发送消息过频（触发本地防风控限流），请几秒后再试",
                }
            if rt.ws is None or rt.state in ("offline", "paused_risk"):
                return {
                    "ok": False,
                    "delivery_status": "failed",
                    "error_code": rt.error_code or "IM_NOT_ONLINE",
                    "message": rt.message or "实时消息通道未在线",
                }

            lo, hi = self._send_delay_range
            if hi > 0:
                await asyncio.sleep(random.uniform(lo, max(lo, hi)))

            if rt.ws is None:
                return {
                    "ok": False,
                    "delivery_status": "failed",
                    "error_code": "IM_DISCONNECTED",
                    "message": "等待发送期间长连接已断开",
                }

            client_uuid, msg_obj, scope_obj = build_text_send_payload(
                cid=cid,
                peer_user_id=peer_user_id,
                my_user_id=rt.user_id,
                text=text,
            )
            rt.send_timestamps.append(time.monotonic())
            try:
                ack = await self._send_rpc(
                    rt,
                    {
                        "lwp": "/r/MessageSend/sendByReceiverScope",
                        "body": [msg_obj, scope_obj],
                    },
                    timeout=10.0,
                )
            except Exception:
                # 严禁盲猜：回执超时先查线上最近历史消息确认是否已发上
                confirmed = await self._verify_sent_on_server(rt, cid=cid, client_uuid=client_uuid, text=text)
                if confirmed:
                    return confirmed
                return {
                    "ok": False,
                    "delivery_status": "uncertain",
                    "client_uuid": client_uuid,
                    "error_code": "SEND_ACK_TIMEOUT",
                    "message": "发送回执超时且未在会话中查到确认，请勿盲目重发，先刷新核对",
                }

            code = int(ack.get("code") or 0)
            body = ack.get("body") if isinstance(ack.get("body"), dict) else {}
            if code == 200 and body.get("messageId"):
                created_ms = int(body.get("createAt") or int(time.time() * 1000))
                return {
                    "ok": True,
                    "delivery_status": "sent",
                    "client_uuid": str(body.get("uuid") or client_uuid),
                    "message_id": str(body["messageId"]),
                    "created_at_ms": created_ms,
                    "created_at": datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc),
                }
            return {
                "ok": False,
                "delivery_status": "failed",
                "client_uuid": client_uuid,
                "error_code": f"WS_SEND_{code}",
                "message": f"发送被闲鱼拒绝（状态码 {code}）",
            }

    async def _verify_sent_on_server(
        self,
        rt: _AccountRuntime,
        *,
        cid: str,
        client_uuid: str,
        text: str,
    ) -> Optional[Dict[str, Any]]:
        if rt.ws is None:
            return None
        try:
            resp = await self._send_rpc(
                rt,
                {
                    "lwp": "/r/MessageManager/listUserMessages",
                    "body": [cid, False, DEFAULT_MAX_UINT64_CURSOR, 10, False],
                },
                timeout=6.0,
            )
            if int(resp.get("code") or 0) != 200:
                return None
            msgs, _ = parse_messages_payload(resp.get("body"), rt.user_id)
            now_ms = int(time.time() * 1000)
            for m in reversed(msgs):
                if m.get("sender") != "me":
                    continue
                if m.get("client_uuid") == client_uuid or (
                    m.get("text") == text and abs(now_ms - int(m.get("created_at_ms") or 0)) <= 45000
                ):
                    return {
                        "ok": True,
                        "delivery_status": "sent",
                        "client_uuid": client_uuid,
                        "message_id": str(m.get("message_id") or ""),
                        "created_at_ms": int(m.get("created_at_ms") or now_ms),
                        "created_at": m.get("created_at") or _now_utc(),
                    }
        except Exception:
            return None
        return None

    async def _account_supervisor_loop(self, rt: _AccountRuntime) -> None:
        backoffs = [3.0, 8.0, 20.0, 45.0]
        attempt = 0
        mint_fail_streak = 0
        recovering_since = time.monotonic()

        while not rt.stop_requested:
            try:
                outcome = await self._run_session_once(rt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[IM长连接异常断开] key=%s: %s", rt.account_key, exc)
                outcome = "reconnect"

            if rt.stop_requested:
                return
            if outcome in ("fatal_risk", "fatal_offline"):
                return
            if outcome == "refresh":
                attempt = 0
                recovering_since = time.monotonic()
                continue

            if rt.state == "online":
                attempt = 0
                mint_fail_streak = 0
                recovering_since = time.monotonic()
            else:
                mint_fail_streak += 1 if rt.credentials is None else 0

            elapsed = time.monotonic() - recovering_since
            if elapsed >= self._max_recovering_sec or mint_fail_streak >= 3:
                self._emit_state(
                    rt,
                    "offline",
                    "实时消息重连或换证连续失败，已自动暂停监控（请检查网络或重新扫码）",
                    error_code="IM_RECONNECT_EXHAUSTED",
                )
                return

            base_delay = backoffs[min(attempt, len(backoffs) - 1)]
            wait_sec = base_delay * random.uniform(0.8, 1.2)
            attempt += 1
            self._emit_state(
                rt,
                "recovering",
                f"消息专线断开，{wait_sec:.0f}秒后自动重连（第{attempt}次）...",
                error_code="IM_RECONNECTING",
            )
            await asyncio.sleep(wait_sec)
