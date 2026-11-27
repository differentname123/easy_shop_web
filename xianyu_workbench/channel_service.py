# -*- coding: utf-8 -*-
"""可插拔渠道账号绑定、真实在售同步与上游商品对账服务。"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from .channels import ChannelRegistry
from .channels.base import SellingChannel
from .repo import WorkbenchRepository, clean_id, normalize_platform

_NORM_TITLE_RE = re.compile(r"[\W_]+", re.UNICODE)


def _norm_title(text: Any) -> str:
    return _NORM_TITLE_RE.sub("", str(text or "").lower())


def _iso_utc(val: Any) -> str:
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(val or "")


def _live_price_of(product_doc: Optional[Dict[str, Any]]) -> Tuple[str, Optional[float]]:
    if not isinstance(product_doc, dict):
        return "missing", None
    if product_doc.get("is_off_promotion") is True:
        return "off_promotion", None
    di = product_doc.get("deal_info") if isinstance(product_doc.get("deal_info"), dict) else {}
    raw_price = (
        di.get("deal_price") if di.get("is_deal_tip") is True else None
    ) or product_doc.get("activity_price") or product_doc.get("price")
    try:
        num = float(raw_price.to_decimal() if hasattr(raw_price, "to_decimal") else raw_price)
        return "online", (round(num, 2) if num > 0 else None)
    except (TypeError, ValueError):
        return "online", None


class ChannelService:
    """隔离具体销售平台的账户绑定、状态检测、在售同步与上游失效对账。"""

    def __init__(self, repo: WorkbenchRepository, registry: ChannelRegistry) -> None:
        self.repo = repo
        self.registry = registry

    def _adapter(self, channel_code: str) -> SellingChannel:
        try:
            return self.registry.get(channel_code)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"未接入销售渠道: {channel_code}") from exc

    def _persist_account_state(
        self,
        owner: str,
        channel_code: str,
        *,
        bound: bool,
        online: bool,
        account_id: str = "",
        nickname: str = "",
        error_code: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        existing = self.repo.get_channel_account(owner, channel_code) or {}
        eff_account_id = account_id or str(existing.get("account_id") or "").strip()
        eff_nick = nickname or str(existing.get("nickname") or "").strip()
        eff_bound = bool(bound or existing.get("bound") or eff_account_id)
        return self.repo.upsert_channel_account(
            owner,
            channel_code,
            {
                "bound": eff_bound,
                "online": bool(online),
                "account_id": eff_account_id,
                "nickname": eff_nick,
                "error_code": error_code,
                "message": message,
                "last_checked_at": now,
                "updated_at": now,
            },
        )

    def _enrich_selling_items(
        self,
        owner: str,
        selling_doc: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        online_raw = list((selling_doc or {}).get("online_items") or [])
        abnormal_raw = list((selling_doc or {}).get("abnormal_items") or [])
        manual_links = dict((selling_doc or {}).get("manual_links") or {})

        listings = self.repo.list_all_active_listings(owner)
        by_id: Dict[str, Dict[str, Any]] = {}
        by_norm_title: Dict[str, Dict[str, Any]] = {}
        listing_norms: List[Tuple[str, Dict[str, Any]]] = []
        for lst in listings:
            lid = str(lst.get("listing_id") or "").strip()
            if lid:
                by_id[lid] = lst
            nt = _norm_title(lst.get("title"))
            if nt:
                by_norm_title.setdefault(nt, lst)
                listing_norms.append((nt, lst))

        # 收集可能关联到的上游商品并一次性批量查库
        matched_pairs: Dict[str, Tuple[Dict[str, Any], str]] = {}
        keys: List[Tuple[str, str]] = []
        for entry in online_raw + abnormal_raw:
            ext_id = str(entry.get("item_id") or "").strip()
            linked_lst: Optional[Dict[str, Any]] = None
            link_mode = "unlinked"

            manual_lid = str(manual_links.get(ext_id) or "").strip()
            if manual_lid and manual_lid in by_id:
                linked_lst = by_id[manual_lid]
                link_mode = "manual"
            else:
                ext_norm = _norm_title(entry.get("title"))
                if ext_norm:
                    if ext_norm in by_norm_title:
                        linked_lst = by_norm_title[ext_norm]
                        link_mode = "auto_title"
                    elif len(ext_norm) >= 10:
                        for lnorm, lst in listing_norms:
                            if len(lnorm) >= 10 and (ext_norm in lnorm or lnorm in ext_norm):
                                linked_lst = lst
                                link_mode = "auto_title"
                                break
            if linked_lst is not None:
                matched_pairs[ext_id] = (linked_lst, link_mode)
                keys.append((linked_lst.get("platform"), linked_lst.get("product_id")))

        prod_map = self.repo.batch_get_products(keys) if keys else {}

        off_promotion_count = 0

        def _decorate(raw_entry: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal off_promotion_count
            item = dict(raw_entry)
            ext_id = str(item.get("item_id") or "").strip()
            pair = matched_pairs.get(ext_id)
            if not pair:
                item.update({
                    "link_mode": "unlinked",
                    "linked_listing_id": "",
                    "linked_listing_title": "",
                    "linked_platform": "",
                    "linked_product_id": "",
                    "upstream_status": "unlinked",
                    "upstream_live_price": None,
                    "off_promotion_alert": False,
                })
                return item

            lst, mode = pair
            plat = normalize_platform(lst.get("platform"))
            pid = clean_id(lst.get("product_id"))
            prod = prod_map.get((plat, pid))
            up_status, up_price = _live_price_of(prod)
            is_off = up_status == "off_promotion"
            if is_off and item.get("is_online") is not False:
                off_promotion_count += 1
            item.update({
                "link_mode": mode,
                "linked_listing_id": str(lst.get("listing_id") or ""),
                "linked_listing_title": str(lst.get("title") or ""),
                "linked_platform": plat,
                "linked_product_id": pid,
                "upstream_status": up_status,
                "upstream_live_price": up_price,
                "off_promotion_alert": is_off,
            })
            return item

        online_items = [_decorate(x) for x in online_raw]
        abnormal_items = [_decorate(x) for x in abnormal_raw]
        return {
            "online_items": online_items,
            "abnormal_items": abnormal_items,
            "online_count": len(online_items),
            "abnormal_count": len(abnormal_items),
            "off_promotion_alert_count": off_promotion_count,
            "group_counts": dict((selling_doc or {}).get("group_counts") or {}),
            "synced_at": _iso_utc((selling_doc or {}).get("synced_at")),
        }

    def list_channels(self, owner: str) -> List[Dict[str, Any]]:
        acct_map = {
            str(d.get("channel") or ""): d for d in self.repo.list_channel_accounts(owner)
        }
        out: List[Dict[str, Any]] = []
        for adapter in self.registry.list_all():
            acct = acct_map.get(adapter.channel_code) or {}
            selling_doc = self.repo.get_channel_selling(owner, adapter.channel_code)
            enriched = self._enrich_selling_items(owner, selling_doc) if selling_doc else None
            out.append({
                "channel": adapter.channel_code,
                "name": adapter.channel_name,
                "capabilities": adapter.capabilities.to_dict(),
                "bound": bool(acct.get("bound")),
                "online": bool(acct.get("online")),
                "account_id": str(acct.get("account_id") or ""),
                "nickname": str(acct.get("nickname") or ""),
                "error_code": str(acct.get("error_code") or ""),
                "message": str(acct.get("message") or ("未绑定账号" if not acct.get("bound") else "")),
                "last_checked_at": _iso_utc(acct.get("last_checked_at")),
                "online_count": enriched["online_count"] if enriched else 0,
                "abnormal_count": enriched["abnormal_count"] if enriched else 0,
                "off_promotion_alert_count": enriched["off_promotion_alert_count"] if enriched else 0,
                "synced_at": enriched["synced_at"] if enriched else "",
            })
        return out

    # =========================================================================
    # 扫码登录绑定、状态检测、在售同步与关联
    # =========================================================================
    def start_qr_login(self, owner: str, channel_code: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.qr_login:
            raise HTTPException(status_code=501, detail=f"{adapter.channel_name} 暂不支持扫码绑定")
        try:
            snap = adapter.start_qr_login(owner)
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if snap.state == "success":
            self._persist_account_state(
                owner,
                adapter.channel_code,
                bound=True,
                online=True,
                account_id=snap.account_id,
                nickname=snap.nickname,
                message=snap.message,
            )
        return snap.to_dict()

    def get_qr_login_status(self, owner: str, channel_code: str, session_id: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        snap = adapter.get_qr_login_status(owner, session_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="扫码会话不存在或已清理")
        if snap.state == "success":
            self._persist_account_state(
                owner,
                adapter.channel_code,
                bound=True,
                online=True,
                account_id=snap.account_id,
                nickname=snap.nickname,
                message=snap.message,
            )
        elif snap.state == "risk_blocked":
            self._persist_account_state(
                owner,
                adapter.channel_code,
                bound=False,
                online=False,
                error_code=snap.error_code or "RISK_CONTROL_BLOCKED",
                message=snap.message,
            )
        return snap.to_dict()

    def get_qr_image_bytes(self, owner: str, channel_code: str, session_id: str) -> bytes:
        adapter = self._adapter(channel_code)
        snap = adapter.get_qr_login_status(owner, session_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="扫码会话不存在")
        png = adapter.get_qr_image_bytes(owner, session_id)
        if not png:
            raise HTTPException(status_code=404, detail="二维码尚未就绪")
        return png

    def cancel_qr_login(self, owner: str, channel_code: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        cancelled = adapter.cancel_qr_login(owner, session_id)
        return {"cancelled": cancelled}

    def check_account_status(self, owner: str, channel_code: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.status_check:
            raise HTTPException(status_code=501, detail=f"{adapter.channel_name} 暂不支持在线检测")
        st = adapter.check_status(owner)
        acct = self._persist_account_state(
            owner,
            adapter.channel_code,
            bound=st.online,
            online=st.online,
            account_id=st.account_id,
            nickname=st.nickname,
            error_code=st.error_code,
            message=st.message,
        )
        return {
            "channel": adapter.channel_code,
            "name": adapter.channel_name,
            "bound": bool(acct.get("bound")),
            "online": bool(acct.get("online")),
            "account_id": str(acct.get("account_id") or ""),
            "nickname": str(acct.get("nickname") or ""),
            "error_code": str(acct.get("error_code") or ""),
            "message": str(acct.get("message") or ""),
            "last_checked_at": _iso_utc(acct.get("last_checked_at")),
        }

    def sync_selling(self, owner: str, channel_code: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.sync_selling:
            raise HTTPException(status_code=501, detail=f"{adapter.channel_name} 暂不支持同步在售")
        res = adapter.sync_selling_items(owner)
        if not res.ok:
            self._persist_account_state(
                owner,
                adapter.channel_code,
                bound=False,
                online=False,
                account_id=res.account_id,
                nickname=res.nickname,
                error_code=res.error_code,
                message=res.message,
            )
            raise HTTPException(
                status_code=409,
                detail=res.message or "渠道账号未登录或已掉线，请重新扫码绑定",
            )

        self._persist_account_state(
            owner,
            adapter.channel_code,
            bound=True,
            online=True,
            account_id=res.account_id,
            nickname=res.nickname,
            error_code="",
            message="在售同步成功",
        )
        now = datetime.now(timezone.utc)
        saved = self.repo.upsert_channel_selling(
            owner,
            adapter.channel_code,
            online_items=res.online_items,
            abnormal_items=res.abnormal_items,
            group_counts=res.group_counts,
            synced_at=now,
        )
        return self._build_selling_payload(owner, adapter, saved)

    def _build_selling_payload(
        self,
        owner: str,
        adapter: SellingChannel,
        selling_doc: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        acct = self.repo.get_channel_account(owner, adapter.channel_code) or {}
        enriched = self._enrich_selling_items(owner, selling_doc)
        return {
            "channel": adapter.channel_code,
            "name": adapter.channel_name,
            "capabilities": adapter.capabilities.to_dict(),
            "bound": bool(acct.get("bound")),
            "online": bool(acct.get("online")),
            "account_id": str(acct.get("account_id") or ""),
            "nickname": str(acct.get("nickname") or ""),
            "last_checked_at": _iso_utc(acct.get("last_checked_at")),
            **enriched,
        }

    def get_selling_snapshot(self, owner: str, channel_code: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        selling_doc = self.repo.get_channel_selling(owner, adapter.channel_code)
        return self._build_selling_payload(owner, adapter, selling_doc)

    def link_selling_item(
        self,
        owner: str,
        channel_code: str,
        external_item_id: str,
        listing_id: str,
    ) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        ext_id = str(external_item_id or "").strip()
        if not ext_id:
            raise HTTPException(status_code=400, detail="无效的渠道商品 ID")
        clean_lid = str(listing_id or "").strip()
        if clean_lid:
            lst = self.repo.get_listing_by_id(owner, clean_lid)
            if not lst:
                raise HTTPException(status_code=404, detail="待关联的本地商品不存在")
        updated = self.repo.update_channel_manual_link(owner, adapter.channel_code, ext_id, clean_lid)
        return self._build_selling_payload(owner, adapter, updated)

    def publish_item(self, owner: str, channel_code: str, listing_id: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.auto_publish:
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂未开启一键自动上架，请复制文案与图片在 {adapter.channel_name} App 发布后点击同步在售",
            )
        lst = self.repo.get_listing_by_id(owner, listing_id)
        if not lst:
            raise HTTPException(status_code=404, detail="本地商品不存在")
        try:
            return adapter.publish_item(owner, lst)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

    def downshelf_item(self, owner: str, channel_code: str, external_item_id: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.auto_downshelf:
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂未开启自动下架，请在 {adapter.channel_name} App 下架后点击同步在售",
            )
        try:
            return adapter.downshelf_item(owner, external_item_id)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
