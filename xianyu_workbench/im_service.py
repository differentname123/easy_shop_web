# -*- coding: utf-8 -*-
"""渠道实时消息与回复服务：衔接轻量长连接中枢与数据库商品对话记录，职责内聚。"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import io
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from fastapi import HTTPException
from PIL import Image

from .channels import ChannelRegistry
from .client.im_ws import XianyuIMHub, sanitize_peer_nick
from .client.seller import validate_reply_text
from .repo import WorkbenchRepository

logger = logging.getLogger("xianyu_workbench.im_service")


def _iso_utc(val: Any) -> str:
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(val or "")


def _account_key(owner: str, channel: str) -> str:
    return f"{str(channel or '').strip().lower()}::{str(owner or '').strip()}"


def _parse_account_key(key: str) -> Tuple[str, str]:
    ch, _, owner = str(key or "").partition("::")
    return owner, ch


def _serialize_message(m: Dict[str, Any]) -> Dict[str, Any]:
    raw_sender = m.get("sender")
    sender = raw_sender if raw_sender in ("me", "peer", "system") else "peer"
    return {
        "message_id": str(m.get("message_id") or ""),
        "client_uuid": str(m.get("client_uuid") or ""),
        "sender": sender,
        "msg_type": str(m.get("msg_type") or "text"),
        "text": str(m.get("text") or ""),
        "image_url": str(m.get("image_url") or ""),
        "created_at_ms": int(m.get("created_at_ms") or 0),
        "created_at": _iso_utc(m.get("created_at")),
        "delivery_status": str(m.get("delivery_status") or "sent"),
        "error": str(m.get("error") or ""),
    }


def _serialize_conversation(c: Dict[str, Any]) -> Dict[str, Any]:
    peer_uid = str(c.get("peer_user_id") or "")
    return {
        "cid": str(c.get("cid") or ""),
        "peer_user_id": peer_uid,
        "peer_nick": sanitize_peer_nick(c.get("peer_nick"), peer_uid),
        "unread_count": int(c.get("unread_count") or 0),
        "has_gap": bool(c.get("has_gap", False)),
        "last_message_preview": str(c.get("last_message_preview") or ""),
        "last_message_at": _iso_utc(c.get("last_message_at")),
        "messages": [_serialize_message(m) for m in (c.get("messages") or []) if isinstance(m, dict)],
    }


_MAX_CHAT_IMAGE_BYTES = 5 * 1024 * 1024


def _decode_chat_image_base64(image_base64: str) -> Tuple[bytes, str, str]:
    raw = str(image_base64 or "").strip()
    if "," in raw and raw[:40].lower().startswith("data:image/"):
        raw = raw.split(",", 1)[1].strip()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="图片编码格式无效") from exc
    if not decoded or len(decoded) > _MAX_CHAT_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="图片不能为空且大小不能超过 5MB")
    try:
        with Image.open(io.BytesIO(decoded)) as im:
            fmt = (im.format or "JPEG").upper()
            out = io.BytesIO()
            if fmt == "PNG":
                im.save(out, format="PNG", optimize=True)
                return out.getvalue(), "chat.png", "image/png"
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, format="JPEG", quality=88, optimize=True)
            return out.getvalue(), "chat.jpg", "image/jpeg"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法识别的图片文件，请重新选择") from exc


class ChannelIMService:
    """统一管理各账号实时消息监听、状态同步、按商品对话查询与指定回复发送。"""

    def __init__(
        self,
        repo: WorkbenchRepository,
        registry: ChannelRegistry,
        *,
        hub: Optional[XianyuIMHub] = None,
        auto_start: bool = True,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.hub = hub if hub is not None else XianyuIMHub()
        self.auto_start = bool(auto_start)
        self._bind_adapter_hooks()

    def _bind_adapter_hooks(self) -> None:
        for adapter in self.registry.list_all():
            if hasattr(adapter, "set_im_hooks") and getattr(adapter.capabilities, "im_messaging", False):
                ch = adapter.channel_code
                adapter.set_im_hooks(
                    on_before_profile_replace=lambda owner, c=ch: self.stop_account_monitoring(owner, c),
                    live_status_probe=lambda owner, c=ch: self.hub.get_status(_account_key(owner, c)),
                )

    def _adapter(self, channel_code: str):
        try:
            adapter = self.registry.get(channel_code)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"未接入销售渠道: {channel_code}") from exc
        if not getattr(adapter.capabilities, "im_messaging", False):
            raise HTTPException(status_code=501, detail=f"{adapter.channel_name} 暂未开启实时消息功能")
        return adapter

    def _on_conversations_sync(self, key: str, convs: List[Dict[str, Any]]) -> None:
        owner, channel = _parse_account_key(key)
        if not owner or not channel:
            return
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        item_metas: Dict[str, Dict[str, Any]] = {}
        for c in convs:
            ext_id = str(c.get("external_item_id") or "").strip()
            if not ext_id:
                continue
            grouped.setdefault(ext_id, []).append(c)
            if ext_id not in item_metas:
                item_metas[ext_id] = {
                    "title": c.get("item_title") or "",
                    "pic_url": c.get("item_pic") or "",
                    "item_url": c.get("item_url") or "",
                }
        for ext_id, group_convs in grouped.items():
            self.repo.upsert_channel_item_conversations(
                owner,
                channel,
                ext_id,
                conversations=group_convs,
                item_meta=item_metas.get(ext_id),
            )

    def _on_im_state_change(self, key: str, info: Dict[str, Any]) -> None:
        owner, channel = _parse_account_key(key)
        if not owner or not channel:
            return
        self.repo.update_channel_im_state(
            owner,
            channel,
            im_state=str(info.get("im_state") or "stopped"),
            im_message=str(info.get("im_message") or ""),
            im_error_code=str(info.get("im_error_code") or ""),
            im_last_sync_at=info.get("im_last_sync_at"),
            account_id=str(info.get("user_id") or ""),
            nickname=str(info.get("nickname") or ""),
        )

    def start_all_bound_accounts(self) -> int:
        if not self.auto_start:
            return 0
        started = 0
        for adapter in self.registry.list_all():
            if not getattr(adapter.capabilities, "im_messaging", False):
                continue
            for acct in self.repo.list_all_bound_channel_accounts(adapter.channel_code):
                owner = str(acct.get("owner") or "").strip()
                if not owner:
                    continue
                try:
                    self.ensure_account_monitoring(owner, adapter.channel_code)
                    started += 1
                except Exception as exc:
                    logger.warning("[自动启动IM监控失败] owner=%s ch=%s: %s", owner, adapter.channel_code, exc)
        return started

    def ensure_account_monitoring(
        self,
        owner: str,
        channel_code: str,
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        profile_dir = str(adapter.profile_dir_for(owner))
        key = _account_key(owner, adapter.channel_code)
        return self.hub.ensure_started(
            key,
            profile_dir=profile_dir,
            owner=owner,
            channel=adapter.channel_code,
            on_conversations=self._on_conversations_sync,
            on_state_change=self._on_im_state_change,
            force_refresh=force_refresh,
        )

    def stop_account_monitoring(self, owner: str, channel_code: str) -> None:
        clean_ch = str(channel_code or "").strip().lower()
        self.hub.stop_account(_account_key(owner, clean_ch))

    def reconnect_account(
        self,
        owner: str,
        channel_code: str,
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        acct = self.repo.get_channel_account(owner, adapter.channel_code) or {}
        if not acct.get("bound"):
            raise HTTPException(status_code=409, detail=f"{adapter.channel_name}账号尚未绑定，请先扫码绑定")
        st = self.hub.get_status(_account_key(owner, adapter.channel_code))
        if st.get("im_state") == "online" and not force_refresh:
            self.hub.sync_now(_account_key(owner, adapter.channel_code))
        else:
            self.ensure_account_monitoring(owner, adapter.channel_code, force_refresh=force_refresh)
        return self.get_im_status(owner, adapter.channel_code)

    def get_im_status(self, owner: str, channel_code: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        acct = self.repo.get_channel_account(owner, adapter.channel_code) or {}
        live = self.hub.get_status(_account_key(owner, adapter.channel_code))
        im_state = live.get("im_state")
        if not acct.get("bound"):
            im_state = "stopped"
        elif im_state == "stopped" and acct.get("im_state"):
            im_state = str(acct.get("im_state") or "stopped")

        items = self.repo.list_channel_items(owner, adapter.channel_code)
        total_unread = sum(int(it.get("unread_message_count") or 0) for it in items)
        return {
            "channel": adapter.channel_code,
            "name": adapter.channel_name,
            "bound": bool(acct.get("bound")),
            "online": bool(acct.get("online")),
            "im_state": im_state,
            "im_message": str(live.get("im_message") or acct.get("im_message") or ""),
            "im_error_code": str(live.get("im_error_code") or acct.get("im_error_code") or ""),
            "im_last_sync_at": str(live.get("im_last_sync_at") or _iso_utc(acct.get("im_last_sync_at"))),
            "total_unread_messages": total_unread,
        }

    def get_item_conversations(
        self,
        owner: str,
        channel_code: str,
        external_item_id: str,
    ) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        ext_id = str(external_item_id or "").strip()
        if not ext_id:
            raise HTTPException(status_code=400, detail="无效的商品 ID")
        doc = self.repo.get_channel_item(owner, adapter.channel_code, ext_id)
        if not doc or doc.get("status") == "pending":
            raise HTTPException(status_code=404, detail="未找到该渠道商品")

        convs = [_serialize_conversation(c) for c in (doc.get("conversations") or []) if isinstance(c, dict)]
        im_st = self.get_im_status(owner, adapter.channel_code)
        return {
            "channel": adapter.channel_code,
            "external_item_id": ext_id,
            "title": str(doc.get("title") or ""),
            "price": str(doc.get("price") or ""),
            "pic_url": str(doc.get("pic_url") or ""),
            "item_url": str(doc.get("item_url") or ""),
            "status": str(doc.get("status") or ""),
            "im_state": im_st["im_state"],
            "im_message": im_st["im_message"],
            "unread_message_count": int(doc.get("unread_message_count") or 0),
            "conversation_count": len(convs),
            "conversations": convs,
        }

    def _prepare_reply_context(
        self,
        owner: str,
        channel_code: str,
        external_item_id: str,
        cid: str,
    ) -> Tuple[Any, str, str, str, str]:
        adapter = self._adapter(channel_code)
        ext_id = str(external_item_id or "").strip()
        clean_cid = str(cid or "").strip()
        if not ext_id or not clean_cid:
            raise HTTPException(status_code=400, detail="缺少商品或会话标识")

        doc = self.repo.get_channel_item(owner, adapter.channel_code, ext_id)
        if not doc or doc.get("status") == "pending":
            raise HTTPException(status_code=404, detail="未找到该渠道商品")
        conv = next(
            (c for c in (doc.get("conversations") or []) if isinstance(c, dict) and str(c.get("cid") or "") == clean_cid),
            None,
        )
        if conv is None:
            raise HTTPException(status_code=404, detail="未找到该商品下对应的买家会话")

        peer_uid = str(conv.get("peer_user_id") or "").strip()
        if not peer_uid:
            raise HTTPException(status_code=400, detail="该会话缺失买家身份标识，请刷新后重试")

        acct = self.repo.get_channel_account(owner, adapter.channel_code) or {}
        if not acct.get("bound"):
            raise HTTPException(status_code=409, detail="渠道账号尚未绑定，请先扫码绑定")

        key = _account_key(owner, adapter.channel_code)
        live = self.hub.get_status(key)
        if live.get("im_state") in ("offline", "paused_risk"):
            raise HTTPException(status_code=409, detail=live.get("im_message") or "实时消息通道已暂停，请先重连或扫码")

        if live.get("im_state") == "stopped":
            self.ensure_account_monitoring(owner, adapter.channel_code)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                live = self.hub.get_status(key)
                if live.get("im_state") in ("online", "offline", "paused_risk"):
                    break
                time.sleep(0.1)
            if live.get("im_state") != "online":
                raise HTTPException(status_code=409, detail=live.get("im_message") or "实时消息通道正在连接中，请稍候重试")

        return adapter, ext_id, clean_cid, peer_uid, key

    def _execute_reply(
        self,
        owner: str,
        adapter: Any,
        ext_id: str,
        clean_cid: str,
        *,
        msg_type: str,
        preview_text: str,
        send_fn: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        client_uuid = f"-{int(time.time() * 1000)}{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc)
        pending_msg = {
            "message_id": client_uuid,
            "client_uuid": client_uuid,
            "sender": "me",
            "msg_type": msg_type,
            "text": preview_text,
            "image_url": "",
            "created_at_ms": int(now.timestamp() * 1000),
            "created_at": now,
            "delivery_status": "sending",
            "error": "",
        }
        self.repo.append_channel_item_message(owner, adapter.channel_code, ext_id, clean_cid, pending_msg)

        send_res = send_fn()
        delivery_status = str(send_res.get("delivery_status") or ("sent" if send_res.get("ok") else "failed"))
        updated_doc = self.repo.update_channel_item_message_status(
            owner,
            adapter.channel_code,
            ext_id,
            clean_cid,
            client_uuid,
            delivery_status=delivery_status,
            message_id=str(send_res.get("message_id") or ""),
            image_url=str(send_res.get("image_url") or ""),
            created_at=send_res.get("created_at"),
            created_at_ms=int(send_res.get("created_at_ms") or 0),
            error="" if send_res.get("ok") else str(send_res.get("message") or "发送失败"),
        )
        updated_convs = [
            _serialize_conversation(c)
            for c in ((updated_doc or {}).get("conversations") or [])
            if isinstance(c, dict)
        ]
        target_conv = next((c for c in updated_convs if c["cid"] == clean_cid), None)
        return {
            "ok": bool(send_res.get("ok")),
            "delivery_status": delivery_status,
            "error_code": "" if send_res.get("ok") else str(send_res.get("error_code") or "SEND_FAILED"),
            "message": "已发送" if send_res.get("ok") else str(send_res.get("message") or "发送失败"),
            "conversation": target_conv,
            "conversations": updated_convs,
        }

    def send_item_reply(
        self,
        owner: str,
        channel_code: str,
        external_item_id: str,
        cid: str,
        text: str,
    ) -> Dict[str, Any]:
        ok_txt, clean_or_err = validate_reply_text(text)
        if not ok_txt:
            raise HTTPException(status_code=400, detail=clean_or_err)
        adapter, ext_id, clean_cid, peer_uid, key = self._prepare_reply_context(
            owner, channel_code, external_item_id, cid
        )
        return self._execute_reply(
            owner,
            adapter,
            ext_id,
            clean_cid,
            msg_type="text",
            preview_text=clean_or_err,
            send_fn=lambda: self.hub.send_message(
                key,
                cid=clean_cid,
                peer_user_id=peer_uid,
                text=clean_or_err,
            ),
        )

    def send_item_image_reply(
        self,
        owner: str,
        channel_code: str,
        external_item_id: str,
        cid: str,
        image_base64: str,
    ) -> Dict[str, Any]:
        image_bytes, filename, mime_type = _decode_chat_image_base64(image_base64)
        adapter, ext_id, clean_cid, peer_uid, key = self._prepare_reply_context(
            owner, channel_code, external_item_id, cid
        )
        return self._execute_reply(
            owner,
            adapter,
            ext_id,
            clean_cid,
            msg_type="image",
            preview_text="[图片]",
            send_fn=lambda: self.hub.send_image_message(
                key,
                cid=clean_cid,
                peer_user_id=peer_uid,
                image_bytes=image_bytes,
                filename=filename,
                mime_type=mime_type,
            ),
        )

    def shutdown(self) -> None:
        self.hub.shutdown()
