# -*- coding: utf-8 -*-
"""可插拔渠道账号绑定、真实在售同步、手动上下架、发布追踪与声明式选品库对齐服务。"""
from __future__ import annotations

from datetime import datetime, timezone
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException

from .bridge import encode_main_site_item_code
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


def _normalize_listing_status(raw: Any) -> str:
    s = str(raw or "draft").strip().lower()
    if s in ("online", "offline"):
        return "ready"
    if s == "rejected":
        return "dismissed"
    return s


def _listing_price(doc: Dict[str, Any]) -> float:
    raw = doc.get("price") if doc.get("price") is not None else doc.get("sell_price")
    try:
        val = float(raw.to_decimal() if hasattr(raw, "to_decimal") else raw)
        return round(val, 2) if val > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


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
    """隔离具体销售平台的账户绑定、状态检测、在售同步、手动上下架与选品库对齐。"""

    def __init__(
        self,
        repo: WorkbenchRepository,
        registry: ChannelRegistry,
        *,
        inter_item_delay_range: Tuple[float, float] = (2.0, 4.5),
    ) -> None:
        self.repo = repo
        self.registry = registry
        lo, hi = inter_item_delay_range
        self._inter_item_delay_range = (max(0.0, float(lo)), max(0.0, float(hi)))

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
        publications = dict((selling_doc or {}).get("publications") or {})

        pub_ext_to_lid: Dict[str, str] = {}
        for lid, pub in publications.items():
            if isinstance(pub, dict):
                ext = str(pub.get("external_item_id") or "").strip()
                if ext:
                    pub_ext_to_lid.setdefault(ext, str(lid))

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

        matched_pairs: Dict[str, Tuple[Optional[Dict[str, Any]], str, str]] = {}
        missing_explicit_lids: List[str] = []
        for entry in online_raw + abnormal_raw:
            ext_id = str(entry.get("item_id") or "").strip()
            explicit_lid = str(manual_links.get(ext_id) or pub_ext_to_lid.get(ext_id) or "").strip()
            if explicit_lid and explicit_lid not in by_id:
                missing_explicit_lids.append(explicit_lid)
        hist_by_id = (
            self.repo.batch_get_listings_by_ids(owner, missing_explicit_lids)
            if missing_explicit_lids
            else {}
        )

        keys: List[Tuple[str, str]] = []
        for entry in online_raw + abnormal_raw:
            ext_id = str(entry.get("item_id") or "").strip()
            linked_lst: Optional[Dict[str, Any]] = None
            link_mode = "unlinked"
            resolved_lid = ""

            explicit_lid = str(manual_links.get(ext_id) or pub_ext_to_lid.get(ext_id) or "").strip()
            if explicit_lid:
                if explicit_lid in by_id:
                    linked_lst = by_id[explicit_lid]
                    link_mode = "manual"
                    resolved_lid = explicit_lid
                else:
                    # 曾由工作台发布或关联，现已移出选品库（dismissed）或记录已清理，仍保留溯源以便同步精准下架
                    linked_lst = hist_by_id.get(explicit_lid)
                    link_mode = "removed"
                    resolved_lid = explicit_lid
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
                        resolved_lid = str(linked_lst.get("listing_id") or "").strip()
            if resolved_lid:
                matched_pairs[ext_id] = (linked_lst, link_mode, resolved_lid)
                if linked_lst is not None:
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
                    "linked_listing_status": "",
                    "linked_platform": "",
                    "linked_product_id": "",
                    "upstream_status": "unlinked",
                    "upstream_live_price": None,
                    "off_promotion_alert": False,
                })
                return item

            lst, mode, lid = pair
            pub_rec = publications.get(lid) if isinstance(publications.get(lid), dict) else {}
            if lst is None:
                item.update({
                    "link_mode": mode,
                    "linked_listing_id": lid,
                    "linked_listing_title": str(pub_rec.get("title") or ""),
                    "linked_listing_status": "dismissed",
                    "linked_platform": "",
                    "linked_product_id": "",
                    "linked_main_item_code": "",
                    "upstream_status": "removed",
                    "upstream_live_price": None,
                    "off_promotion_alert": False,
                })
                return item

            plat = normalize_platform(lst.get("platform"))
            pid = clean_id(lst.get("product_id"))
            prod = prod_map.get((plat, pid))
            up_status, up_price = _live_price_of(prod)
            is_off = up_status == "off_promotion"
            if is_off and item.get("is_online") is not False and mode != "removed":
                off_promotion_count += 1
            gsign = str((prod or {}).get("goods_sign") or "").strip()
            item.update({
                "link_mode": mode,
                "linked_listing_id": lid,
                "linked_listing_title": str(lst.get("title") or pub_rec.get("title") or ""),
                "linked_listing_status": _normalize_listing_status(lst.get("status")),
                "linked_platform": plat,
                "linked_product_id": pid,
                "linked_main_item_code": encode_main_site_item_code(plat, pid, gsign),
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
            "publications": publications,
            "online_count": len(online_items),
            "abnormal_count": len(abnormal_items),
            "off_promotion_alert_count": off_promotion_count,
            "group_counts": dict((selling_doc or {}).get("group_counts") or {}),
            "synced_at": _iso_utc((selling_doc or {}).get("synced_at")),
        }

    def _build_selling_payload(
        self,
        owner: str,
        adapter: SellingChannel,
        selling_doc: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        doc = selling_doc if isinstance(selling_doc, dict) else (self.repo.get_channel_doc(owner, adapter.channel_code) or {})
        enriched = self._enrich_selling_items(owner, doc)
        return {
            "channel": adapter.channel_code,
            "name": adapter.channel_name,
            "capabilities": adapter.capabilities.to_dict(),
            "bound": bool(doc.get("bound")),
            "online": bool(doc.get("online")),
            "account_id": str(doc.get("account_id") or ""),
            "nickname": str(doc.get("nickname") or ""),
            "last_checked_at": _iso_utc(doc.get("last_checked_at")),
            **enriched,
        }

    def list_channels(self, owner: str) -> List[Dict[str, Any]]:
        ch_map = {
            str(d.get("channel") or ""): d for d in self.repo.list_channel_docs(owner)
        }
        out: List[Dict[str, Any]] = []
        for adapter in self.registry.list_all():
            doc = ch_map.get(adapter.channel_code) or {}
            enriched = self._enrich_selling_items(owner, doc) if doc else None
            out.append({
                "channel": adapter.channel_code,
                "name": adapter.channel_name,
                "capabilities": adapter.capabilities.to_dict(),
                "bound": bool(doc.get("bound")),
                "online": bool(doc.get("online")),
                "account_id": str(doc.get("account_id") or ""),
                "nickname": str(doc.get("nickname") or ""),
                "error_code": str(doc.get("error_code") or ""),
                "message": str(doc.get("message") or ("未绑定账号" if not doc.get("bound") else "")),
                "last_checked_at": _iso_utc(doc.get("last_checked_at")),
                "online_count": enriched["online_count"] if enriched else 0,
                "abnormal_count": enriched["abnormal_count"] if enriched else 0,
                "off_promotion_alert_count": enriched["off_promotion_alert_count"] if enriched else 0,
                "synced_at": enriched["synced_at"] if enriched else "",
            })
        return out

    # =========================================================================
    # 扫码登录绑定、状态检测、在售同步与手动关联
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
        payload = self._build_selling_payload(owner, adapter, saved)
        online_lids = [
            str(x.get("linked_listing_id") or "").strip()
            for x in (payload.get("online_items") or [])
            if isinstance(x, dict) and x.get("linked_listing_id") and x.get("link_mode") != "removed"
        ]
        if online_lids:
            hist = self.repo.batch_get_listings_by_ids(owner, online_lids)
            for lid in online_lids:
                lst = hist.get(lid)
                if isinstance(lst, dict) and lst.get("publish_failure"):
                    self.repo.clear_listing_publish_failure(owner, lid)
        return payload

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

    # =========================================================================
    # 单品手动上架、手动下架与发布追踪
    # =========================================================================
    def _execute_publish_one(
        self,
        owner: str,
        adapter: SellingChannel,
        lst: Dict[str, Any],
        *,
        product_doc: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        lid = str(lst.get("listing_id") or "").strip()
        title = str(lst.get("title") or "").strip()
        price = _listing_price(lst)
        images = lst.get("images") if isinstance(lst.get("images"), list) else []

        if not title:
            raise ValueError("商品标题不能为空")
        if price <= 0:
            raise ValueError("标价必须大于 0")
        if not images:
            raise ValueError("至少需要保留 1 张商品图片")

        if product_doc is None:
            product_doc = self.repo.get_product_one(lst.get("platform"), lst.get("product_id"))
        up_status, _ = _live_price_of(product_doc)
        if up_status == "off_promotion":
            raise ValueError("该商品上游活动已失效，暂不可上架")

        # 若为已完善好的草稿直接点上架，同步归入选品库 ready，避免对账误下架
        if _normalize_listing_status(lst.get("status")) != "ready":
            now = datetime.now(timezone.utc)
            self.repo.update_listing_by_id(owner, lid, {"status": "ready", "confirmed_at": now, "updated_at": now})

        payload = {
            "listing_id": lid,
            "title": title,
            "description": str(lst.get("description") or "").strip(),
            "images": images,
            "price": price,
        }
        res = adapter.publish_item(owner, payload)
        if not res.get("ok"):
            shot_url = str(res.get("failure_screenshot_url") or "").strip()
            err_code = str(res.get("error_code") or "PUBLISH_FAILED")
            self.repo.record_listing_publish_failure(
                owner,
                lid,
                {
                    "channel": adapter.channel_code,
                    "channel_name": adapter.channel_name,
                    "error_code": err_code,
                    "message": str(res.get("message") or "上架失败"),
                    "screenshot_url": shot_url,
                    "failed_at": _iso_utc(datetime.now(timezone.utc)),
                },
            )
            if res.get("is_offline") or err_code in ("ACCOUNT_OFFLINE", "RISK_CONTROL_BLOCKED"):
                self._persist_account_state(
                    owner,
                    adapter.channel_code,
                    bound=False,
                    online=False,
                    error_code=err_code,
                    message=str(res.get("message") or "账号掉线，请重新扫码绑定"),
                )
            return {**res, "failure_screenshot_url": shot_url}

        ext_id = str(res.get("item_id") or res.get("external_item_id") or "").strip()
        item_url = str(res.get("item_url") or "").strip()
        first_pic = ""
        if images and isinstance(images[0], dict):
            first_pic = str(images[0].get("url") or "")
        elif images and isinstance(images[0], str):
            first_pic = images[0]

        self._persist_account_state(
            owner,
            adapter.channel_code,
            bound=True,
            online=True,
            account_id=str(res.get("account_id") or ""),
            nickname=str(res.get("nickname") or ""),
            error_code="",
            message="商品上架成功",
        )
        self.repo.clear_listing_publish_failure(owner, lid)
        self.repo.record_channel_publication(
            owner,
            adapter.channel_code,
            listing_id=lid,
            external_item_id=ext_id,
            item_url=item_url,
            title=str(res.get("title") or title),
            price=res.get("price") or f"{price:.2f}",
            pic_url=first_pic,
        )
        return {
            "ok": True,
            "listing_id": lid,
            "item_id": ext_id,
            "item_url": item_url,
            "title": str(res.get("title") or title),
            "price": str(res.get("price") or f"{price:.2f}"),
        }

    def publish_item(self, owner: str, channel_code: str, listing_id: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.auto_publish:
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂未开启自动上架",
            )
        lst = self.repo.get_listing_by_id(owner, listing_id)
        if not lst:
            raise HTTPException(status_code=404, detail="选品库中未找到该商品")

        selling = self.get_selling_snapshot(owner, adapter.channel_code)
        for on in selling.get("online_items") or []:
            if str(on.get("linked_listing_id") or "") == str(lst.get("listing_id") or ""):
                raise HTTPException(
                    status_code=409,
                    detail=f"该商品已在{adapter.channel_name}上架（ID: {on.get('item_id')}），可直接点击查看",
                )

        try:
            res = self._execute_publish_one(owner, adapter, lst)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

        if not res.get("ok"):
            status_code = (
                409
                if res.get("is_offline") or res.get("error_code") in ("ACCOUNT_OFFLINE", "RISK_CONTROL_BLOCKED")
                else 400
            )
            raise HTTPException(status_code=status_code, detail=str(res.get("message") or "上架失败"))

        return {
            "published": True,
            **{k: res[k] for k in ("listing_id", "item_id", "item_url", "title", "price")},
            "selling": self.get_selling_snapshot(owner, adapter.channel_code),
        }

    def _execute_downshelf_one(
        self,
        owner: str,
        adapter: SellingChannel,
        external_item_id: str,
    ) -> Dict[str, Any]:
        ext_id = str(external_item_id or "").strip()
        if not ext_id:
            return {"ok": False, "error_code": "INVALID_ID", "message": "商品 ID 不能为空"}
        res = adapter.downshelf_item(owner, ext_id)
        if not res.get("ok"):
            if res.get("is_offline") or res.get("error_code") in ("ACCOUNT_OFFLINE", "RISK_CONTROL_BLOCKED"):
                self._persist_account_state(
                    owner,
                    adapter.channel_code,
                    bound=False,
                    online=False,
                    error_code=str(res.get("error_code") or "ACCOUNT_OFFLINE"),
                    message=str(res.get("message") or "账号掉线，请重新扫码绑定"),
                )
            return res

        self._persist_account_state(
            owner,
            adapter.channel_code,
            bound=True,
            online=True,
            account_id=str(res.get("account_id") or ""),
            nickname=str(res.get("nickname") or ""),
            error_code="",
            message="商品下架成功",
        )
        self.repo.mark_channel_downshelf(owner, adapter.channel_code, ext_id)
        return {"ok": True, "item_id": ext_id}

    def downshelf_item(self, owner: str, channel_code: str, external_item_id: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.auto_downshelf:
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂未开启自动下架",
            )
        try:
            res = self._execute_downshelf_one(owner, adapter, external_item_id)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

        if not res.get("ok"):
            status_code = (
                409
                if res.get("is_offline") or res.get("error_code") in ("ACCOUNT_OFFLINE", "RISK_CONTROL_BLOCKED")
                else 400
            )
            raise HTTPException(status_code=status_code, detail=str(res.get("message") or "下架失败"))

        return {
            "downshelved": True,
            "item_id": str(res["item_id"]),
            "selling": self.get_selling_snapshot(owner, adapter.channel_code),
        }

    # =========================================================================
    # 同步前置校验（供后台队列入队前即刻拦截非法请求）
    # =========================================================================
    def validate_sync_request(self, owner: str, channel_code: str) -> SellingChannel:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.sync_selling:
            raise HTTPException(status_code=501, detail=f"{adapter.channel_name} 暂不支持同步在售")
        return adapter

    def validate_publish_request(self, owner: str, channel_code: str, listing_id: str) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.auto_publish:
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂未开启自动上架",
            )
        lst = self.repo.get_listing_by_id(owner, listing_id)
        if not lst:
            raise HTTPException(status_code=404, detail="选品库中未找到该商品")
        selling = self.get_selling_snapshot(owner, adapter.channel_code)
        for on in selling.get("online_items") or []:
            if str(on.get("linked_listing_id") or "") == str(lst.get("listing_id") or ""):
                raise HTTPException(
                    status_code=409,
                    detail=f"该商品已在{adapter.channel_name}上架（ID: {on.get('item_id')}），可直接点击查看",
                )
        title = str(lst.get("title") or "").strip()
        price = _listing_price(lst)
        images = lst.get("images") if isinstance(lst.get("images"), list) else []
        if not title:
            raise HTTPException(status_code=400, detail="商品标题不能为空")
        if price <= 0:
            raise HTTPException(status_code=400, detail="标价必须大于 0")
        if not images:
            raise HTTPException(status_code=400, detail="至少需要保留 1 张商品图片")
        prod = self.repo.get_product_one(lst.get("platform"), lst.get("product_id"))
        up_status, _ = _live_price_of(prod)
        if up_status == "off_promotion":
            raise HTTPException(status_code=400, detail="该商品上游活动已失效，暂不可上架")
        return lst

    def validate_downshelf_request(self, owner: str, channel_code: str, external_item_id: str) -> str:
        adapter = self._adapter(channel_code)
        if not adapter.capabilities.auto_downshelf:
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂未开启自动下架",
            )
        ext_id = str(external_item_id or "").strip()
        if not ext_id:
            raise HTTPException(status_code=400, detail="商品 ID 不能为空")
        return ext_id

    def validate_reconcile_request(
        self,
        owner: str,
        channel_code: str,
        publish_listing_ids: Sequence[str],
        downshelf_item_ids: Sequence[str],
    ) -> Tuple[List[str], List[str]]:
        adapter = self._adapter(channel_code)
        if not (adapter.capabilities.auto_publish and adapter.capabilities.auto_downshelf):
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂不支持选品库自动同步对齐",
            )
        pub_ids = list(dict.fromkeys(str(x or "").strip() for x in publish_listing_ids if str(x or "").strip()))
        dw_ids = list(dict.fromkeys(str(x or "").strip() for x in downshelf_item_ids if str(x or "").strip()))
        if not pub_ids and not dw_ids:
            raise HTTPException(status_code=400, detail="请至少选择 1 件待上架或待下架商品")
        return pub_ids, dw_ids

    # =========================================================================
    # 声明式「同步选品库」对齐（缺少上架、多余下架；默认读取快照秒开，防误杀外部帖子）
    # =========================================================================
    def preview_reconcile(
        self,
        owner: str,
        channel_code: str,
        *,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not (adapter.capabilities.auto_publish and adapter.capabilities.auto_downshelf):
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂不支持选品库自动同步对齐",
            )

        selling = (
            self.sync_selling(owner, adapter.channel_code)
            if refresh
            else self.get_selling_snapshot(owner, adapter.channel_code)
        )
        all_active = self.repo.list_all_active_listings(owner)
        ready_listings = [
            d for d in all_active if _normalize_listing_status(d.get("status")) == "ready"
        ]
        ready_map = {str(d["listing_id"]): d for d in ready_listings if d.get("listing_id")}

        keys = [(d.get("platform"), d.get("product_id")) for d in ready_listings]
        prod_map = self.repo.batch_get_products(keys) if keys else {}

        to_downshelf: List[Dict[str, Any]] = []
        already_synced: List[Dict[str, Any]] = []
        online_matched_lids = set()

        for on in selling.get("online_items") or []:
            lid = str(on.get("linked_listing_id") or "").strip()
            lid_status = str(on.get("linked_listing_status") or "").strip()
            ext_id = str(on.get("item_id") or "").strip()
            if lid and lid in ready_map:
                lst = ready_map[lid]
                prod = prod_map.get((normalize_platform(lst.get("platform")), clean_id(lst.get("product_id"))))
                up_status, _ = _live_price_of(prod)
                if up_status == "off_promotion":
                    to_downshelf.append({
                        "item_id": ext_id,
                        "title": str(on.get("title") or ""),
                        "price": str(on.get("price") or ""),
                        "pic_url": str(on.get("pic_url") or ""),
                        "item_url": str(on.get("item_url") or ""),
                        "linked_listing_id": lid,
                        "downshelf_category": "off_promotion",
                        "recommended": True,
                        "reason": "上游活动已失效",
                    })
                else:
                    online_matched_lids.add(lid)
                    already_synced.append({
                        "listing_id": lid,
                        "item_id": ext_id,
                        "title": str(on.get("title") or ""),
                        "price": str(on.get("price") or ""),
                        "item_url": str(on.get("item_url") or ""),
                    })
            elif lid:
                to_downshelf.append({
                    "item_id": ext_id,
                    "title": str(on.get("title") or ""),
                    "price": str(on.get("price") or ""),
                    "pic_url": str(on.get("pic_url") or ""),
                    "item_url": str(on.get("item_url") or ""),
                    "linked_listing_id": lid,
                    "downshelf_category": "removed_from_library",
                    "recommended": True,
                    "reason": "已退回草稿" if lid_status == "draft" else "已从选品库删除",
                })
            else:
                to_downshelf.append({
                    "item_id": ext_id,
                    "title": str(on.get("title") or ""),
                    "price": str(on.get("price") or ""),
                    "pic_url": str(on.get("pic_url") or ""),
                    "item_url": str(on.get("item_url") or ""),
                    "linked_listing_id": "",
                    "downshelf_category": "external_unlinked",
                    "recommended": False,
                    "reason": "非选品库商品（外部发布）",
                })

        to_downshelf.sort(key=lambda x: 0 if x.get("recommended") else 1)

        to_publish: List[Dict[str, Any]] = []
        skipped_off_promotion: List[Dict[str, Any]] = []
        for lst in ready_listings:
            lid = str(lst.get("listing_id") or "").strip()
            if not lid or lid in online_matched_lids:
                continue
            prod = prod_map.get((normalize_platform(lst.get("platform")), clean_id(lst.get("product_id"))))
            up_status, _ = _live_price_of(prod)
            imgs = lst.get("images") if isinstance(lst.get("images"), list) else []
            pic_url = str(imgs[0].get("url") or "") if imgs and isinstance(imgs[0], dict) else ""
            entry = {
                "listing_id": lid,
                "title": str(lst.get("title") or "").strip(),
                "price": _listing_price(lst),
                "pic_url": pic_url,
                "platform": normalize_platform(lst.get("platform")),
            }
            if up_status == "off_promotion":
                skipped_off_promotion.append({**entry, "reason": "上游活动已失效"})
            else:
                to_publish.append(entry)

        return {
            "channel": adapter.channel_code,
            "channel_name": adapter.channel_name,
            "synced_at": str(selling.get("synced_at") or ""),
            "ready_count": len(ready_listings),
            "online_count": len(selling.get("online_items") or []),
            "to_publish": to_publish,
            "to_downshelf": to_downshelf,
            "recommended_downshelf_count": sum(1 for x in to_downshelf if x.get("recommended")),
            "already_synced": already_synced,
            "skipped_off_promotion": skipped_off_promotion,
            "selling": selling,
        }

    def _sleep_interruptible(self, should_cancel: Optional[Callable[[], bool]]) -> bool:
        lo, hi = self._inter_item_delay_range
        if hi <= 0:
            return bool(should_cancel and should_cancel())
        delay = random.uniform(lo, max(lo, hi))
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                return True
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return bool(should_cancel and should_cancel())

    def apply_reconcile(
        self,
        owner: str,
        channel_code: str,
        *,
        publish_listing_ids: Sequence[str] = (),
        downshelf_item_ids: Sequence[str] = (),
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        adapter = self._adapter(channel_code)
        if not (adapter.capabilities.auto_publish and adapter.capabilities.auto_downshelf):
            raise HTTPException(
                status_code=501,
                detail=f"{adapter.channel_name} 暂不支持选品库自动同步对齐",
            )

        dw_ids = list(dict.fromkeys(str(x or "").strip() for x in downshelf_item_ids if str(x or "").strip()))
        pub_ids = list(dict.fromkeys(str(x or "").strip() for x in publish_listing_ids if str(x or "").strip()))
        total = len(dw_ids) + len(pub_ids)
        completed = 0

        downshelved: List[Dict[str, Any]] = []
        published: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        aborted_reason = ""
        cancelled = False

        def _emit(current_label: str = "") -> None:
            if callable(on_progress):
                on_progress({
                    "total": total,
                    "completed": completed,
                    "current_label": current_label,
                    "published": list(published),
                    "downshelved": list(downshelved),
                    "failed": list(failed),
                    "aborted_reason": aborted_reason,
                })

        with adapter.batch_session(owner) as session_adapter:
            # 1. 先下架多余商品，避免占坑
            for idx, raw_ext_id in enumerate(dw_ids):
                if should_cancel and should_cancel():
                    cancelled = True
                    aborted_reason = "已按请求停止后续同步"
                    break
                _emit(f"正在下架 {raw_ext_id}")
                res = self._execute_downshelf_one(owner, session_adapter, raw_ext_id)
                completed += 1
                if res.get("ok"):
                    downshelved.append({"item_id": raw_ext_id})
                else:
                    failed.append({
                        "action": "downshelf",
                        "item_id": raw_ext_id,
                        "message": str(res.get("message") or "下架失败"),
                    })
                    if res.get("is_offline") or res.get("error_code") in ("ACCOUNT_OFFLINE", "RISK_CONTROL_BLOCKED"):
                        aborted_reason = str(res.get("message") or "账号掉线或触发安全校验，已中止同步")
                        _emit("")
                        break
                _emit("")
                has_more = (idx + 1 < len(dw_ids)) or bool(pub_ids)
                if has_more and self._sleep_interruptible(should_cancel):
                    cancelled = True
                    aborted_reason = "已按请求停止后续同步"
                    break

            # 2. 再补上架缺少的选品
            if not aborted_reason and not cancelled and pub_ids:
                current_online_lids = {
                    str(x.get("linked_listing_id") or "").strip()
                    for x in (self.get_selling_snapshot(owner, adapter.channel_code).get("online_items") or [])
                    if x.get("linked_listing_id")
                }
                for idx, raw_lid in enumerate(pub_ids):
                    if should_cancel and should_cancel():
                        cancelled = True
                        aborted_reason = "已按请求停止后续同步"
                        break
                    if raw_lid in current_online_lids:
                        completed += 1
                        _emit("")
                        continue
                    lst = self.repo.get_listing_by_id(owner, raw_lid)
                    if not lst:
                        completed += 1
                        failed.append({"action": "publish", "listing_id": raw_lid, "message": "选品不存在"})
                        _emit("")
                        continue

                    item_title = str(lst.get("title") or raw_lid).strip()[:20]
                    _emit(f"正在上架：{item_title}")
                    did_browser_op = True
                    try:
                        res = self._execute_publish_one(owner, session_adapter, lst)
                    except ValueError as exc:
                        completed += 1
                        failed.append({"action": "publish", "listing_id": raw_lid, "message": str(exc)})
                        _emit("")
                        continue

                    completed += 1
                    if res.get("ok"):
                        current_online_lids.add(res["listing_id"])
                        published.append({
                            "listing_id": res["listing_id"],
                            "item_id": res["item_id"],
                            "item_url": res["item_url"],
                            "title": res["title"],
                            "price": res["price"],
                        })
                    else:
                        failed.append({
                            "action": "publish",
                            "listing_id": raw_lid,
                            "error_code": str(res.get("error_code") or "PUBLISH_FAILED"),
                            "message": str(res.get("message") or "上架失败"),
                            "screenshot_url": str(res.get("failure_screenshot_url") or ""),
                        })
                        if res.get("is_offline") or res.get("error_code") in ("ACCOUNT_OFFLINE", "RISK_CONTROL_BLOCKED"):
                            aborted_reason = str(res.get("message") or "账号掉线或触发安全校验，已中止同步")
                            _emit("")
                            break
                    _emit("")
                    if did_browser_op and idx + 1 < len(pub_ids):
                        if self._sleep_interruptible(should_cancel):
                            cancelled = True
                            aborted_reason = "已按请求停止后续同步"
                            break

        return {
            "downshelved": downshelved,
            "published": published,
            "failed": failed,
            "aborted_reason": aborted_reason,
            "cancelled": cancelled,
            "selling": self.get_selling_snapshot(owner, adapter.channel_code),
        }
