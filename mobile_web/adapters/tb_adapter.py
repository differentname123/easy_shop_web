# -*- coding: utf-8 -*-
"""淘宝平台边界：官方商品、第三方ID榜单、核验身份与渠道订单映射。"""
import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit

from infrastructure.logging_utils import get_app_logger

logger = get_app_logger(__name__)

from adapters.base import BasePlatformAdapter
from infrastructure.api_governance import (
    GLOBAL_PROVIDER_COOLDOWN,
    is_real_requests_session,
    stamp_universal_resolution,
)
from infrastructure.http_utils import (
    SHANGHAI,
    clean_money,
    clean_text as _text,
    error_dict as _error,
    extract_ztk_item_fields,
    normalize_http_url,
    parse_carousel_images,
    parse_finite_money as _money,
    parse_shanghai_timestamp as _timestamp,
)
from models.product import (
    DetailSnapshot,
    ProductRef,
    ProductSummary,
    ProductDetail,
    TAOBAO_DOMAINS,
    URL_RE,
    clean_product_id,
    extract_canonical_goods_id,
    extract_taobao_token_core,
    text_has_disallowed_url,
)
from infrastructure.tb_client import TaobaoClient, _TB_SESSION
from infrastructure.hdk_client import HdkClient
from infrastructure.ztk_client import ZtkClient, ORDER_WINDOW_SECONDS


class TaobaoAdapter(BasePlatformAdapter):
    platform_code = "taobao"
    platform_name = "淘宝"
    required_identity_fields = ("relation_id",)
    requires_authorized_promotion = True
    order_window_seconds = ORDER_WINDOW_SECONDS

    def __init__(self, settings=None, official_client=None, hdk_client=None, ztk_client=None, **overrides):
        super().__init__()
        if settings is None:
            from config import settings
        def setting(name, default=""):
            return overrides.get(name, getattr(settings, name, default))
        self.active = bool(setting("taobao_enabled", False))
        key, secret = setting("tb_app_key"), setting("tb_app_secret")
        adzone = _text(setting("tb_adzone_id"))
        if not adzone:
            pid = _text(setting("tb_pid"))
            adzone = pid.rsplit("_", 1)[-1] if pid.startswith("mm_") and len(pid.split("_")) == 4 else ""
        self.official = official_client if official_client is not None else TaobaoClient(
            key, secret, adzone, setting("tb_session"), setting("tb_inviter_code"))
        hdk_keys = setting("hdk_api_key_list", ())
        self.hdk = hdk_client if hdk_client is not None else HdkClient(
            setting("hdk_api_key"), api_key_list=hdk_keys)
        ztk_keys = setting("ztk_appkey_list", ())
        ztk_sids = setting("ztk_sid_list", ())
        ztk_pub_sids = setting("ztk_publisher_sid_list", ())
        self.ztk = ztk_client if ztk_client is not None else ZtkClient(
            setting("ztk_appkey"), setting("ztk_sid"),
            appkey_list=ztk_keys, sid_list=ztk_sids, publisher_sid_list=ztk_pub_sids,
        )
        pid_val = _text(setting("tb_pid"))
        pid_prefix = _text(setting("tb_pid_prefix"))
        if not pid_prefix and pid_val.startswith("mm_") and len(pid_val.split("_")) == 4:
            pid_prefix = pid_val.rsplit("_", 1)[0]
        if not pid_prefix:
            off_prefix = getattr(self.official, "pid_prefix", "")
            pid_prefix = off_prefix.strip() if isinstance(off_prefix, str) else ""
        channel_adzone = _text(setting("tb_channel_adzone_id")) or adzone
        self.channel_pid = _text(setting("tb_channel_pid")) or (
            f"{pid_prefix}_{channel_adzone}" if pid_prefix and channel_adzone else ""
        )
        self._official_ok = bool(official_client is not None or key and secret)
        self._search_ok = bool(official_client is not None or self._official_ok and adzone)
        has_hdk_key = bool(getattr(self.hdk, "appkey_list", None) or setting("hdk_api_key") or hdk_keys)
        self._hdk_ok = bool(hdk_client is not None or has_hdk_key)
        has_ztk_key = bool(getattr(self.ztk, "appkey_list", None) or setting("ztk_appkey") or ztk_keys)
        has_ztk_sid = bool(getattr(self.ztk, "sid_list", None) or setting("ztk_sid") or ztk_sids)
        has_ztk_channel = bool(has_ztk_key and has_ztk_sid)
        self._ztk_channel_ok = bool(ztk_client is not None or has_ztk_channel)
        self._official_auth_ok = bool(official_client is not None or (self._official_ok and setting("tb_session")))
        raw_off_rt = getattr(official_client, "refresh_token", "")
        self._has_tb_refresh_token = bool(
            _text(setting("tb_refresh_token"))
            or (raw_off_rt.strip() if isinstance(raw_off_rt, str) else "")
        )
        self._orders_ok = bool(ztk_client is not None or has_ztk_channel)
        self.tb_name = _text(setting("hdk_tb_name"))
        if self._hdk_ok and self.hdk:
            self.attach_hdk(self.hdk)
        self._refresh_capabilities()
        if self.active and self._ztk_channel_ok and not self.channel_pid:
            logger.warning(
                "⚠️ 【淘宝配置告警】已启用淘宝并配置折淘客，但未形成 4 段式 channel_pid（请配置 taobao_channel_pid 或 taobao_pid_prefix），万能转链与推手渠道转链将不可用"
            )

    def attach_hdk(self, hdk_client: HdkClient) -> None:
        """仅当显式配置了 hdk_tb_name 时挂载好单库淘宝高佣转链；订单同步始终保留折淘客渠道通道。"""
        self.hdk = hdk_client
        if getattr(self, "tb_name", ""):
            self.register_provider("promotion", "hdk_tb_promotion", self._make_hdk_promotion(hdk_client), prepend=True)

    def _make_hdk_promotion(self, client: HdkClient):
        def _promote(goods_id_or_sign="", uid=None, **kwargs):
            ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
            target_id = self._preferred_tb_promo_id(ref)
            if not target_id:
                return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
            pid = self.channel_pid
            if not pid:
                return _error("CONFIG_ERROR", "缺少淘宝 PID 配置")
            identity = kwargs.get("identity") or {}
            relation_id = _text(identity.get("relation_id")) if isinstance(identity, dict) else ""
            title = _text(kwargs.get("title") or kwargs.get("name") or "精选好物")
            is_channel = bool(uid and relation_id)
            res = client.convert_tb_promotion_link(
                target_id,
                pid=pid,
                tb_name=self.tb_name,
                title=title,
                relation_id=(relation_id if is_channel else None),
            )
            if res.get("status") != "success":
                err_code = res.get("error_code")
                if err_code in ("AUTH_REQUIRED", "PROVIDER_UNAUTHORIZED", "UPSTREAM_PROVIDER_UNAUTHORIZED") or "未授权" in str(res.get("error_msg")):
                    logger.warning("[TB转链-好单库] 好单库淘宝推客未授权，平滑降级至折淘客通道: %s", res.get("error_msg"))
                    return _error("UPSTREAM_PROVIDER_UNAUTHORIZED", res.get("error_msg") or "好单库推客未授权")
                logger.warning("[TB转链-好单库] 转链失败(%s: %s)，准备降级至备用通道", err_code, res.get("error_msg"))
                return res
            logger.info("[TB转链-好单库主力] 转链成功: target_id=%s, tpwd=%s", target_id, res.get("tpwd"))
            link = res.get("final_link") or res.get("url")
            short_url = res.get("short_url") or link
            password = res.get("tpwd") or res.get("taoword") or ""
            return {
                "status": "success",
                "url": link,
                "final_link": short_url or link,
                "mobile_url": link,
                "short_url": short_url,
                "we_app_info": {},
                "promotion_token": password,
                "token_type": "tpwd" if password else "",
                "attribution_status": "verified" if is_channel else "anonymous",
                "relation_id": relation_id if is_channel else "",
                "need_auth": False,
                "is_fallback": False,
                "goods_id": ref.goods_id,
                "goods_sign": ref.goods_sign or ref.goods_id,
                "link_type": "好单库渠道淘口令" if is_channel else "好单库高佣淘口令",
            }
        return _promote

    def _ztk_catalog_ready(self) -> bool:
        appkeys = getattr(self.ztk, "appkey_list", None)
        return bool(isinstance(appkeys, (tuple, list)) and appkeys)

    @staticmethod
    def _guard_provider_result(res: Any) -> Dict[str, Any]:
        if not isinstance(res, dict):
            return {"status": "skipped", "reason": "unconfigured_provider_mock"}
        return res

    @staticmethod
    def _is_truncated_tb_suffix(value: str) -> bool:
        val = _text(value)
        return bool(
            15 <= len(val) <= 22
            and val.isalnum()
            and not val.isdigit()
            and any(c.isdigit() for c in val)
            and any(c.isalpha() for c in val)
            and "-" not in val
        )

    @classmethod
    def _preferred_tb_promo_id(cls, ref: ProductRef, fallback: str = "") -> str:
        """优先选择非纯数字、非截断的加密 goods_sign 用于推广转链，避免淘宝联盟 301 拒收数字 ID。"""
        for cand in (ref.goods_sign, fallback, ref.goods_id):
            val = _text(cand)
            if val and not val.isdigit() and not cls._is_truncated_tb_suffix(val):
                return val
        for cand in (ref.goods_sign, fallback, ref.goods_id):
            val = _text(cand)
            if val and not cls._is_truncated_tb_suffix(val):
                return val
        return _text(ref.goods_sign or fallback or ref.goods_id)

    @staticmethod
    def _verify_tb_external_detail_match(returned_id: str, ref: Optional[ProductRef]) -> bool:
        """严格校验 ZTK/HDK 淘宝详情返回的 itemid/tao_id 与请求商品身份一致，防串货伪造。"""
        ret = _text(returned_id)
        if not ret:
            return False
        if ref is None:
            return True
        req_keys = [k for k in (_text(ref.goods_sign), _text(ref.goods_id)) if k]
        if not req_keys:
            return True
        if ret in req_keys:
            return True
        ret_canon = extract_canonical_goods_id("taobao", ret)
        if ret_canon:
            for k in req_keys:
                k_canon = extract_canonical_goods_id("taobao", k)
                if k_canon and k_canon == ret_canon:
                    return True
        return False

    def _refresh_capabilities(self) -> None:
        has_ztk_catalog = self._ztk_catalog_ready()
        self.capabilities = {
            "recommend": bool((self._official_ok and self._hdk_ok) or self._search_ok or self._has_extra_providers("recommend")),
            "search": bool(self._search_ok or (self._ztk_channel_ready() and bool(self.channel_pid)) or self._hdk_ok or self._has_extra_providers("search")),
            "detail": bool(self._official_ok or has_ztk_catalog or self._hdk_ok or self._has_extra_providers("detail")),
            "promotion": bool(
                self._search_ok
                or (self._ztk_channel_ready() and bool(self.channel_pid))
                or self._has_extra_providers("promotion")
            ),
            "universal": bool(
                (self._ztk_channel_ready() and bool(self.channel_pid))
                or self._official_ok
                or self._has_extra_providers("universal")
            ),
            "authorization": bool(
                self._official_auth_ok
                or self._ztk_channel_ok
                or self._has_extra_providers("check_authority")
                or self._has_extra_providers("generate_authority_url")
            ),
            "orders": bool(self._orders_ok or self._has_extra_providers("orders")),
        }

    def _is_real_http(self) -> bool:
        ztk_http = getattr(self.ztk, "http", None)
        off_http = getattr(self.official, "http_session", None) or _TB_SESSION
        hdk_http = getattr(self.hdk, "http", None)
        return (
            is_real_requests_session(ztk_http)
            or is_real_requests_session(off_http)
            or is_real_requests_session(hdk_http)
        )

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
        sign = _text(goods_sign or goods_id_or_sign or goods_id)
        gid = extract_canonical_goods_id(self.platform_code, goods_id or sign)
        return ProductRef(self.platform_code, gid, sign)

    @staticmethod
    def _matches(item, ref):
        returned = _text(item.get("goods_sign"))
        echoed = _text(item.get("input_item_iid"))
        original = _text(item.get("goods_id"))
        key = ref.goods_sign or ref.goods_id
        if key and key in (returned, echoed, original):
            return True
        canonical_key = extract_canonical_goods_id("taobao", key or ref.goods_id)
        canonical_returned = extract_canonical_goods_id("taobao", returned or original)
        return bool(
            canonical_key
            and canonical_key != key
            and canonical_key in (canonical_returned, original)
        )

    @staticmethod
    def _official_product_url(value):
        if not isinstance(value, str):
            return ""
        value = "https:" + value if value.startswith("//") else value
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            trusted = any(host == domain or host.endswith("." + domain) for domain in ("taobao.com", "tmall.com"))
            return value if trusted and parsed.scheme in ("https", "http") and not parsed.username and not parsed.password else ""
        except ValueError:
            return ""

    @staticmethod
    def _same_official_product(source, candidate):
        # This only corroborates a SINGLE result from an exact, official item_url
        # query. Never use a title/image fingerprint to select a keyword result.
        return all(isinstance(source.get(key), str) and source[key] and source[key] == candidate.get(key)
                   for key in ("seller_id", "goods_name", "goods_image_url"))

    def _summary(self, item):
        raw_sign = _text(item.get("goods_sign") or item.get("goods_id"))
        canonical_id = extract_canonical_goods_id(self.platform_code, item.get("goods_id") or raw_sign) or raw_sign
        # Explicitly whitelist DTO fields. Never cache click_url/coupon_share_url/raw_item.
        data = {
            "platform": self.platform_code, "platform_name": self.platform_name,
            "goods_id": canonical_id, "goods_sign": raw_sign,
            "name": item.get("goods_name") or "", "image_url": item.get("goods_image_url") or "",
            "price": item.get("price") or 0.0, "original_price": item.get("original_price") or 0.0,
            "coupon_amount": item.get("coupon_amount") or 0.0, "has_coupon": bool(item.get("has_coupon")),
            "commission_rate": item.get("commission_rate") or 0.0,
            "commission_amount": item.get("commission_amount") or 0.0,
            "sales_tip": _text(item.get("sales_tip") or "0"), "mall_name": item.get("mall_name") or "",
        }
        for source, target in (("brand_name", "brand"), ("category_name", "category")):
            if item.get(source):
                data[target] = item[source]
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _detail(self, item):
        data = self._summary(item)
        specs = [
            {"label": label, "value": str(item[key])}
            for key, label in (
                ("brand_name", "商品品牌"),
                ("category_name", "商品类目"),
                ("mall_name", "店铺名称"),
                ("provcity", "发货地"),
            )
            if item.get(key)
        ]
        data.update({"desc": item.get("goods_desc") or "", "carousel_images": item.get("carousel_images") or [],
                     "detail_tags": item.get("detail_tags") or [], "service_guarantees": [],
                     "shop_ratings": {}, "specs": specs, "coupon_info": {},
                     "product_url": self._official_product_url(((item.get("raw_item") or {}).get("item_basic_info") or {}).get("item_url"))})
        return ProductDetail(**data).to_dict(exclude_unset=True)

    def _normalize_ztk_tb_summary(self, item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValueError("invalid ztk tb item")
        raw_end = _money(item.get("quanhou_jiage"))
        raw_size = _money(item.get("size"))
        has_valid_price = (raw_end is not None and raw_end >= 0) or (raw_size is not None and raw_size >= 0)
        f = extract_ztk_item_fields(item, zero_fallback_to_size=True, include_primary_in_carousel=False)
        raw_sign = f["tao_id"]
        if not raw_sign or not f["title"] or not has_valid_price or f["price"] < 0:
            raise ValueError("missing or invalid ztk tb summary")
        canonical_id = extract_canonical_goods_id(self.platform_code, raw_sign) or raw_sign
        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": canonical_id,
            "goods_sign": raw_sign,
            "name": f["title"],
            "image_url": f["image_url"],
            "price": f["price"],
            "original_price": f["original_price"],
            "coupon_amount": f["coupon_amount"],
            "has_coupon": f["coupon_amount"] > 0,
            "commission_rate": f["commission_rate"],
            "commission_amount": f["commission_amount"],
            "sales_tip": f["sales_tip"],
            "mall_name": f["mall_name"],
        }
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _normalize_ztk_tb_detail(self, item: dict, ref: Optional[ProductRef] = None) -> dict:
        summary = self._normalize_ztk_tb_summary(item)
        if ref is not None and self._verify_tb_external_detail_match(summary.get("goods_sign") or summary.get("goods_id") or "", ref):
            requested = ref.goods_sign or ref.goods_id
            if requested and not str(requested).isdigit():
                req_canon = extract_canonical_goods_id(self.platform_code, requested)
                if not req_canon or not summary.get("goods_id") or req_canon == summary["goods_id"]:
                    summary["goods_sign"] = requested
            if ref.goods_id and str(ref.goods_id).isdigit() and (not summary.get("goods_id") or summary["goods_id"] == ref.goods_id):
                summary["goods_id"] = ref.goods_id
        f = extract_ztk_item_fields(item, zero_fallback_to_size=True, include_primary_in_carousel=True)
        specs = []
        for src, label in (("nick", "店铺名称"), ("brand_name", "商品品牌"), ("category_name", "商品类目")):
            val = _text(item.get(src))
            if val:
                specs.append({"label": label, "value": val})
        ratings = {
            tgt: _text(item[src])
            for src, tgt in (("score1", "desc_score"), ("score2", "lgst_score"), ("score3", "serv_score"))
            if _text(item.get(src)) and _text(item.get(src)) not in ("0", "0.0")
        }
        tags = []
        if _text(item.get("coupon_info")):
            tags.append(_text(item.get("coupon_info")))
        elif summary.get("coupon_amount", 0) > 0:
            tags.append(f"{summary['coupon_amount']:g}元优惠券")
        coupon_info = {
            k: item[k]
            for k in ("coupon_start_time", "coupon_end_time", "coupon_total_count", "coupon_remain_count")
            if item.get(k) not in (None, "")
        }
        summary.update({
            "desc": html_lib.unescape(_text(item.get("tao_title") or item.get("item_description"))),
            "carousel_images": f["carousel_images"],
            "detail_tags": tags,
            "service_guarantees": [],
            "shop_ratings": ratings,
            "specs": specs,
            "coupon_info": coupon_info,
        })
        return ProductDetail(**summary).to_dict(exclude_unset=True)

    def _normalize_hdk_tb_summary(self, item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValueError("invalid hdk tb item")
        raw_id = _text(item.get("itemid"))
        title = html_lib.unescape(_text(item.get("itemtitle") or item.get("itemshorttitle") or item.get("goodsname")))
        if not raw_id or not title:
            raise ValueError("missing or invalid hdk tb item")
        image_url = normalize_http_url(item.get("itempic"))
        if not image_url and item.get("taobao_image"):
            pics = parse_carousel_images(item.get("taobao_image"))
            image_url = pics[0] if pics else ""
        end_p = clean_money(item.get("itemendprice"), None)
        orig_p = clean_money(item.get("itemprice"), None)
        price = end_p if (end_p is not None and end_p > 0) else orig_p
        if price is None or price < 0:
            raise ValueError("missing or invalid hdk tb price")
        original = orig_p if (orig_p is not None and orig_p >= price) else price
        coupon = clean_money(item.get("couponmoney"), 0.0) or 0.0
        rate = clean_money(item.get("tkrates") or item.get("commissionshare"), 0.0) or 0.0
        amt = clean_money(item.get("tkmoney") or item.get("commission"), 0.0) or 0.0
        if amt <= 0.0 and price > 0 and rate > 0:
            amt = round(price * rate / 100.0, 2)
        elif rate <= 0.0 and amt > 0 and price > 0:
            rate = round(amt / price * 100.0, 2)
        mall_name = html_lib.unescape(_text(item.get("shopname") or item.get("sellernick")))
        sales = _text(item.get("itemsale") or item.get("volume") or "0")
        canonical_id = extract_canonical_goods_id(self.platform_code, raw_id) or raw_id
        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": canonical_id,
            "goods_sign": raw_id,
            "name": title,
            "image_url": image_url,
            "price": round(max(0.0, price), 2),
            "original_price": round(max(original, price), 2),
            "coupon_amount": round(max(0.0, coupon), 2),
            "has_coupon": coupon > 0,
            "commission_rate": round(max(0.0, rate), 2),
            "commission_amount": round(max(0.0, amt), 2),
            "sales_tip": sales,
            "mall_name": mall_name,
        }
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _normalize_hdk_tb_detail(self, item: dict, ref: Optional[ProductRef] = None) -> dict:
        summary = self._normalize_hdk_tb_summary(item)
        if ref is not None and self._verify_tb_external_detail_match(summary.get("goods_sign") or summary.get("goods_id") or "", ref):
            requested = ref.goods_sign or ref.goods_id
            if requested and not str(requested).isdigit():
                req_canon = extract_canonical_goods_id(self.platform_code, requested)
                if not req_canon or not summary.get("goods_id") or req_canon == summary["goods_id"]:
                    summary["goods_sign"] = requested
            if ref.goods_id and str(ref.goods_id).isdigit() and (not summary.get("goods_id") or summary["goods_id"] == ref.goods_id):
                summary["goods_id"] = ref.goods_id
        gallery = parse_carousel_images(
            item.get("taobao_image") or item.get("small_images"),
            primary_image=summary.get("image_url") or "",
        )
        tags = []
        if _text(item.get("couponexplain")):
            tags.append(_text(item.get("couponexplain")))
        elif summary.get("coupon_amount", 0) > 0:
            tags.append(f"{summary['coupon_amount']:g}元优惠券")
        specs = []
        for k, label in (("shopname", "店铺名称"), ("brand_name", "商品品牌"), ("category_name", "商品类目")):
            val = _text(item.get(k))
            if val and not val.isdigit():
                specs.append({"label": label, "value": html_lib.unescape(val)})
        summary.update({
            "desc": html_lib.unescape(_text(item.get("itemdesc") or item.get("goodsdesc"))),
            "carousel_images": gallery,
            "detail_tags": tags,
            "service_guarantees": [],
            "shop_ratings": {},
            "specs": specs,
            "coupon_info": {},
        })
        return ProductDetail(**summary).to_dict(exclude_unset=True)

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs):
        unavailable = self._available("search")
        if unavailable:
            return {**unavailable, "data": []}

        def _search_via_official():
            # Catalog content is always public/site-wide, not personalized promotion content.
            res = self._guard_provider_result(self.official.search_goods(keyword, page=page, page_size=page_size))
            if res.get("status") != "success":
                return res
            return {**{key: value for key, value in res.items() if key not in ("data", "raw")},
                    "data": [self._summary(item) for item in res.get("data", [])]}

        def _search_via_ztk():
            res = self._guard_provider_result(
                self.ztk.search_tb_goods(keyword, page=page, page_size=page_size, pid=self.channel_pid)
            )
            if res.get("status") != "success":
                return res
            raw_rows = res.get("data") or []
            items = []
            for raw_it in raw_rows:
                try:
                    items.append(self._normalize_ztk_tb_summary(raw_it))
                except Exception:
                    continue
            if raw_rows and not items:
                return _error("PROTOCOL_ERROR", "折淘客淘宝搜索结果字段无效", data=[])
            return {**{key: value for key, value in res.items() if key not in ("data", "raw")}, "data": items}

        def _search_via_hdk():
            res = self._guard_provider_result(self.hdk.search_tb_goods(keyword, page=page, page_size=page_size))
            if res.get("status") != "success":
                return res
            raw_rows = res.get("data") or []
            items = []
            for raw_it in raw_rows:
                try:
                    items.append(self._normalize_hdk_tb_summary(raw_it))
                except Exception:
                    continue
            if raw_rows and not items:
                return _error("PROTOCOL_ERROR", "好单库淘宝搜索结果字段无效", data=[])
            return {**{key: value for key, value in res.items() if key not in ("data", "raw")}, "data": items}

        base = []
        if self._search_ok:
            base.append(("tb_official_search", _search_via_official))
        if self._ztk_channel_ready() and self.channel_pid and hasattr(self.ztk, "search_tb_goods"):
            base.append(("ztk_tb_search", _search_via_ztk))
        if self._hdk_ok and hasattr(self.hdk, "search_tb_goods"):
            base.append(("hdk_tb_search", _search_via_hdk))

        providers = self._compose_providers(
            "search", base, keyword=keyword, page=page, page_size=page_size, uid=uid, **kwargs,
        )
        res = self._execute_capability("search", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def get_recommend_goods(self, channel_type=5, offset=0, limit=20, uid=None, detail_sink=None, **kwargs):
        unavailable = self._available("recommend")
        if unavailable:
            return {**unavailable, "data": []}
        try:
            channel, offset, limit = int(channel_type), int(offset), int(limit)
            if channel not in (5, 1) or offset < 0 or not 1 <= limit <= 100:
                raise ValueError
            sale_type = {5: 1, 1: 2}[channel]
            cursor = kwargs.get("cursor")
            if isinstance(cursor, str):
                cursor = json.loads(cursor)
            source_cursor, consumed = None, 0
            if cursor is not None:
                if not isinstance(cursor, dict) or int(cursor.get("channel_type", -1)) != channel or int(cursor.get("offset", -1)) != offset:
                    raise ValueError
                source_cursor, consumed = cursor.get("source"), offset
        except (ValueError, TypeError):
            return _error("INVALID_ARGUMENT", "淘宝榜单频道、偏移量或游标无效", data=[])

        def _recommend_via_hdk_official():
            if isinstance(source_cursor, dict) and source_cursor.get("provider") == "tb_official_recommend":
                cur_source, cur_consumed = None, 0
            else:
                cur_source, cur_consumed = source_cursor, consumed
            # Legacy offset-only callers can replay source pages; normal callers pass cursor.
            while cur_consumed < offset:
                prior = self.hdk.get_ranked_ids(sale_type, limit=min(100, offset - cur_consumed), cursor=cur_source)
                if prior.get("status") != "success":
                    return prior
                count = prior.get("source_count", len(prior.get("item_ids", [])))
                if count < 1 or not prior.get("has_more"):
                    return {"status": "success", "data": [], "offset": offset, "next_offset": cur_consumed + count,
                            "next_cursor": None, "has_more": False, "total_count": prior.get("total_count")}
                cur_consumed += count
                cur_source = prior.get("next_cursor")
            source = self.hdk.get_ranked_ids(sale_type, limit=limit, cursor=cur_source)
            if source.get("status") != "success":
                return source
            ids = [_text(i) for i in source.get("item_ids", [])]
            unique_ids = list(dict.fromkeys(i for i in ids if i))
            by_id, source_by_sign = {}, {}
            for start in range(0, len(unique_ids), 20):
                batch_ids = unique_ids[start:start + 20]
                response = self.official.get_goods_details(batch_ids)
                # Fix source time at EACH response, not when the page is published or queued.
                fetched_at = datetime.now(timezone.utc) if detail_sink is not None else None
                if response.get("status") != "success":
                    return response  # A failed official batch must not advance the source cursor.
                batch_items = response.get("data", [])
                if not isinstance(batch_items, list):
                    return _error("PROTOCOL_ERROR", "淘宝官方详情列表无效", data=[])
                for item in batch_items:
                    if not isinstance(item, dict):
                        return _error("PROTOCOL_ERROR", "淘宝官方详情商品无效", data=[])
                    echoed, sign = _text(item.get("input_item_iid")), _text(item.get("goods_sign"))
                    # Map only against THIS official request, never position/title or
                    # an arbitrary opaque echo that merely looks like an original ID.
                    source_id = echoed if echoed else sign
                    if not sign or source_id not in batch_ids:
                        return _error("PRODUCT_IDENTITY_MISMATCH", "官方榜单详情未证实本批商品身份", data=[])
                    if ((source_id in by_id and by_id[source_id][0]["goods_sign"] != sign)
                            or (sign in source_by_sign and source_by_sign[sign] != source_id)):
                        return _error("PRODUCT_IDENTITY_MISMATCH", "官方榜单详情商品身份存在冲突", data=[])
                    verified = dict(item)
                    verified["goods_sign"] = sign
                    verified["goods_id"] = extract_canonical_goods_id(self.platform_code, sign) or sign
                    by_id[source_id] = (verified, fetched_at)
                    source_by_sign[sign] = source_id
            items, seen = [], set()
            seeds = [] if detail_sink is not None else None
            for source_id in ids:
                matched = by_id.get(source_id)
                if matched and matched[0]["goods_id"] not in seen:
                    item, fetched_at = matched
                    seen.add(item["goods_id"])
                    items.append(self._summary(item))
                    if seeds is not None:
                        seeds.append((item, fetched_at))
            try:
                count = int(source.get("source_count", len(ids)))
                if count < len(ids):
                    raise ValueError
            except (ValueError, TypeError):
                return _error("PAGINATION_ERROR", "淘宝榜单来源数量无效", data=[])
            next_offset = offset + count
            has_more = source.get("has_more", False)
            if (not isinstance(has_more, bool) or has_more and
                    (next_offset <= offset or not source.get("next_cursor")
                     or source.get("next_cursor") == cur_source)):
                return _error("PAGINATION_ERROR", "淘宝榜单缺少前进游标", data=[])
            result = {"status": "success", "data": items, "offset": offset, "limit": limit,
                      "next_offset": next_offset, "has_more": has_more, "total_count": source.get("total_count"),
                      "next_cursor": {"channel_type": channel, "offset": next_offset,
                                      "source": source["next_cursor"]} if has_more else None}
            if seeds is not None:
                # No partial page is published on a later batch/identity/paging error.
                # The sink only collects memory; persistence belongs to the service.
                snapshots = [DetailSnapshot(
                    ref=ProductRef(self.platform_code, item["goods_id"], item["goods_sign"]),
                    data=self._detail(item), fetched_at=fetched_at) for item, fetched_at in seeds]
                for snapshot in snapshots:
                    detail_sink(snapshot)
            return result

        def _recommend_via_official_material():
            material_id = 3756 if channel == 5 else 13366
            if isinstance(source_cursor, dict) and source_cursor.get("provider") == "tb_official_recommend" and source_cursor.get("page_no"):
                page_no = max(1, int(source_cursor["page_no"]))
                skip = 0
            else:
                page_no = offset // limit + 1
                skip = offset % limit
            res = self._guard_provider_result(
                self.official.get_material_recommend(material_id=material_id, page_no=page_no, page_size=limit)
            )
            if res.get("status") != "success":
                return res
            raw_items = res.get("data") or []
            if not isinstance(raw_items, list):
                return _error("PROTOCOL_ERROR", "淘宝官方物料推荐列表无效", data=[])
            selected = raw_items[skip:skip + limit]
            items = [self._summary(it) for it in selected]
            next_offset = offset + len(items)
            has_more = bool(items) and bool(res.get("has_more"))
            if detail_sink is not None:
                fetched_at = datetime.now(timezone.utc)
                snapshots = [
                    DetailSnapshot(
                        ref=ProductRef(self.platform_code, summ["goods_id"], summ["goods_sign"]),
                        data=self._detail(raw_it),
                        fetched_at=fetched_at,
                    )
                    for raw_it, summ in zip(selected, items)
                ]
                for snapshot in snapshots:
                    detail_sink(snapshot)
            return {
                "status": "success",
                "data": items,
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset,
                "has_more": has_more,
                "total_count": res.get("total_count"),
                "next_cursor": {
                    "channel_type": channel,
                    "offset": next_offset,
                    "source": {"provider": "tb_official_recommend", "page_no": page_no + 1},
                } if has_more else None,
            }

        base = []
        if self._official_ok and self._hdk_ok:
            base.append(("hdk_plus_tb_official_recommend", _recommend_via_hdk_official))
        if self._search_ok and hasattr(self.official, "get_material_recommend"):
            base.append(("tb_official_recommend", _recommend_via_official_material))

        providers = self._compose_providers(
            "recommend", base, channel_type=channel, offset=offset, limit=limit,
            uid=uid, detail_sink=detail_sink, **kwargs,
        )
        res = self._execute_capability("recommend", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def _finalize_official_detail_item(self, raw_item: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(raw_item)
        echoed = _text(item.get("input_item_iid"))
        if echoed and not echoed.isdigit():
            # Keep the proven request identity stable for front-end, Mongo and
            # detail-cache keys instead of replacing it on every encryption round.
            item["goods_sign"] = echoed
            item["goods_id"] = extract_canonical_goods_id(self.platform_code, echoed) or echoed
        else:
            canonical = _text(item.get("goods_sign") or item.get("goods_id"))
            item["goods_sign"] = canonical
            item["goods_id"] = extract_canonical_goods_id(self.platform_code, canonical) or canonical
        return item

    def _official_detail(self, ref):
        if not ref.key:
            return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
        if self._is_truncated_tb_suffix(ref.key):
            return _error("INVALID_IDENTITY", "淘宝商品标识不完整，请提供完整加密标识或数字商品ID")

        def _is_off(r):
            if r.get("status") != "success":
                code = str(r.get("error_code") or "")
                msg = str(r.get("error_msg") or "")
                return code in ("isv.item-not-exist", "isv.item-offline", "isv.item-delete", "PRODUCT_OFF_PROMOTION", "50001") or "无结果" in msg
            raw_data = r.get("data")
            return isinstance(raw_data, list) and len(raw_data) == 0

        res, confirmed_off = self._retry_on_off_promotion(
            lambda: self.official.get_goods_details([ref.key]),
            _is_off,
        )
        if res.get("status") != "success":
            if confirmed_off:
                return _error("PRODUCT_OFF_PROMOTION", "该淘宝商品已下架或已停止推广")
            return res
        items = [item for item in res.get("data", []) if self._matches(item, ref)]
        if not items:
            raw_data = res.get("data")
            if isinstance(raw_data, list) and len(raw_data) == 0:
                return _error("PRODUCT_OFF_PROMOTION", "该淘宝商品已下架或已停止推广")
            return _error("PRODUCT_IDENTITY_MISMATCH", "官方商品详情未证实请求商品身份")
        return {"status": "success", "data": self._finalize_official_detail_item(items[0])}

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs):
        unavailable = self._available("detail")
        if unavailable:
            return unavailable
        ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        if not ref.key:
            return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
        if self._is_truncated_tb_suffix(ref.key):
            return _error("INVALID_IDENTITY", "淘宝商品标识不完整，请提供完整加密标识或数字商品ID")

        def _detail_via_official():
            res = self._official_detail(ref)
            if res.get("status") != "success":
                return res
            return {"status": "success", "data": self._detail(res["data"])}

        def _detail_via_ztk():
            res = self._guard_provider_result(self.ztk.get_tb_goods_detail(ref.key))
            if res.get("status") != "success":
                return res
            rows = res.get("data") or []
            dict_rows = [r for r in rows if isinstance(r, dict) and _text(r.get("tao_id"))]
            if not dict_rows:
                return _error("NOT_FOUND", "折淘客未找到淘宝商品详情")
            matched = next(
                (r for r in dict_rows if self._verify_tb_external_detail_match(_text(r.get("tao_id")), ref)),
                None,
            )
            if matched is None:
                return _error("PRODUCT_IDENTITY_MISMATCH", "折淘客淘宝详情返回与请求商品不一致")
            try:
                return {"status": "success", "data": self._normalize_ztk_tb_detail(matched, ref)}
            except Exception as exc:
                return _error("PROTOCOL_ERROR", f"折淘客淘宝详情解析失败: {exc}")

        def _detail_via_hdk():
            res = self._guard_provider_result(self.hdk.get_tb_goods_detail(ref.key))
            if res.get("status") != "success":
                return res
            raw_row = res.get("data")
            if not isinstance(raw_row, dict) or not _text(raw_row.get("itemid")):
                return _error("NOT_FOUND", "好单库未找到淘宝商品详情")
            if not self._verify_tb_external_detail_match(_text(raw_row.get("itemid")), ref):
                return _error("PRODUCT_IDENTITY_MISMATCH", "好单库淘宝详情返回与请求商品不一致")
            try:
                return {"status": "success", "data": self._normalize_hdk_tb_detail(raw_row, ref)}
            except Exception as exc:
                return _error("PROTOCOL_ERROR", f"好单库淘宝详情解析失败: {exc}")

        base = []
        if self._official_ok:
            base.append(("tb_official_detail", _detail_via_official))
        if self._ztk_catalog_ready() and hasattr(self.ztk, "get_tb_goods_detail"):
            base.append(("ztk_tb_detail", _detail_via_ztk))
        if self._hdk_ok and hasattr(self.hdk, "get_tb_goods_detail"):
            base.append(("hdk_tb_detail", _detail_via_hdk))

        providers = self._compose_providers(
            "detail",
            base,
            goods_id_or_sign=goods_id_or_sign,
            uid=uid,
            goods_id=ref.goods_id or goods_id,
            goods_sign=ref.goods_sign or goods_sign,
            **kwargs,
        )
        return self._execute_capability("detail", providers, allow_persist=self._is_real_http())

    def batch_get_goods_details(self, refs: Sequence[ProductRef], uid=None, **kwargs) -> Dict[str, Dict[str, Any]]:
        """
        淘宝 20 批量官方查卡：首选官方 tbk.item.info.upgrade.get (<=20 靠 input_item_iid 精准对齐，
        单条坏/下架 ID 不连坐)，缺失或失败项回退单品 get_goods_detail（ZTK api_detail -> HDK item_detail）。
        """
        outcomes: Dict[str, Dict[str, Any]] = {}
        ordered_refs = []
        seen_keys = set()
        for raw_ref in refs or ():
            r = self.resolve_product_ref(raw_ref.key, raw_ref.goods_id, raw_ref.goods_sign)
            key = r.key or raw_ref.key
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            ordered_refs.append(r)
        if not ordered_refs:
            return outcomes

        unavailable = self._available("detail")
        if unavailable:
            for r in ordered_refs:
                outcomes[r.key] = dict(unavailable)
            return outcomes

        valid_refs = []
        for r in ordered_refs:
            if not r.key:
                outcomes[r.key] = _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
            elif self._is_truncated_tb_suffix(r.key):
                outcomes[r.key] = _error("INVALID_IDENTITY", "淘宝商品标识不完整，请提供完整加密标识或数字商品ID")
            else:
                valid_refs.append(r)

        if self._official_ok and hasattr(self.official, "get_goods_details"):
            for i in range(0, len(valid_refs), 20):
                chunk = valid_refs[i:i + 20]
                raw_res = self._guard_provider_result(
                    self.official.get_goods_details([r.key for r in chunk])
                )
                if raw_res.get("status") != "success":
                    continue
                rows = raw_res.get("data") or []
                if not isinstance(rows, list):
                    continue
                for r in chunk:
                    matched = [row for row in rows if isinstance(row, dict) and self._matches(row, r)]
                    if len(matched) == 1:
                        try:
                            outcomes[r.key] = {
                                "status": "success",
                                "data": self._detail(self._finalize_official_detail_item(matched[0])),
                                "_provider": "tb_official_detail_batch",
                            }
                        except Exception:
                            pass

        for r in valid_refs:
            if r.key in outcomes:
                continue
            outcomes[r.key] = self.get_goods_detail(
                r.key,
                uid=uid,
                goods_id=r.goods_id or None,
                goods_sign=r.goods_sign or None,
                **kwargs,
            )
        return outcomes

    def _is_unauthorized_error(self, err) -> bool:
        """识别淘宝/折淘客网关抛出的渠道未授权或 relationId 无效错误。"""
        if not err:
            return False
        if isinstance(err, dict):
            if err.get("need_auth") is True or err.get("error_code") == "AUTH_REQUIRED":
                return True
            msg = " ".join(str(err.get(key) or "") for key in ("error_msg", "msg", "content")).strip()
        else:
            msg = str(err)
        return bool("relationId" in msg or "relation_id" in msg or "无效的relation" in msg
                    or re.search(r"(?:用户|推手|渠道).{0,12}(?:未授权|未备案|失效|无效)", msg))

    def convert_promotion_url(self, goods_id_or_sign, uid=None, **kwargs):
        unavailable = self._available("promotion", need_auth=False, is_fallback=False)
        if unavailable:
            return unavailable
        identity = kwargs.get("identity") or {}
        relation_id = _text(identity.get("relation_id")) if isinstance(identity, dict) else ""
        if uid and (not relation_id or relation_id == "0"):
            return _error("AUTH_REQUIRED", "淘宝个人推广需要已核验渠道身份", need_auth=True, is_fallback=False)
        if uid and identity.get("external_id") is not None and _text(identity["external_id"]) != _text(uid):
            return _error("IDENTITY_MISMATCH", "淘宝推广身份与站内用户不一致", need_auth=True, is_fallback=False)
        ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
        ztk_tkl_attempted = False

        def _ztk_channel_convert(target, rel, goods_id_val, goods_sign_val, title_hint, *, allow_public=False):
            nonlocal ztk_tkl_attempted
            ztk_tkl_attempted = True
            ztk_res = (
                self.ztk.convert_channel_url(target, rel, self.channel_pid, allow_public=True)
                if allow_public
                else self.ztk.convert_channel_url(target, rel, self.channel_pid)
            )
            if not isinstance(ztk_res, dict) or ztk_res.get("status") != "success":
                err = ztk_res.get("error_msg") if isinstance(ztk_res, dict) else "折淘客渠道转链失败"
                is_unauth = self._is_unauthorized_error(ztk_res)
                code = "AUTH_REQUIRED" if is_unauth else (ztk_res.get("error_code") if isinstance(ztk_res, dict) else "PROMOTION_FAILED")
                return _error(code or "PROMOTION_FAILED", err, need_auth=is_unauth)
            link = ztk_res.get("final_link") or ztk_res.get("url")
            if not link or not str(link).startswith(("https://", "http://")):
                return _error("PROMOTION_UNAVAILABLE", "折淘客未返回有效渠道推广链接")
            password = ztk_res.get("tpwd") or ""
            if not password and self._official_ok:
                token = self.official.create_tpwd(link, text=title_hint or "精选好物")
                password = token.get("password_simple") or token.get("model") or ""
            if not password:
                return _error("PROTOCOL_ERROR", "渠道转链未生成有效淘口令")
            short_url = _text(ztk_res.get("short_url"))
            is_channel = bool(uid and rel)
            result = {
                "status": "success", "url": link, "final_link": short_url or link, "mobile_url": link, "short_url": short_url,
                "we_app_info": {}, "promotion_token": password, "token_type": "tpwd",
                "attribution_status": "verified" if is_channel else "anonymous",
                "relation_id": rel if is_channel else "",
                "need_auth": False,
                "is_fallback": False, "goods_id": goods_id_val,
                "goods_sign": goods_sign_val,
                "link_type": "折淘客渠道淘口令" if is_channel else "折淘客高佣淘口令",
            }
            expires = _timestamp(ztk_res.get("expires_at"))
            if expires:
                result["expires_at"] = expires
            result["cacheable"] = True
            return result

        def _promote_via_official_or_ztk():
            detail = self._official_detail(ref)
            if detail.get("status") != "success":
                return detail
            item = detail["data"]
            raw = item.get("raw_item") or {}
            # Do not search a title: product URL search may require extra API permission;
            # when denied, surface the official error rather than promote a different item.
            official_target = self._official_product_url((raw.get("item_basic_info") or {}).get("item_url"))
            target = official_target
            if not target:
                fallback_id = self._preferred_tb_promo_id(ref, item.get("goods_sign") or "")
                if str(fallback_id).isdigit():
                    target = "https://item.taobao.com/item.htm?" + urlencode({"id": fallback_id})
                else:
                    target = "https://uland.taobao.com/item/edetail?" + urlencode({"id": fallback_id})
            # 途径 B: 推手渠道推广 -> 调用折淘客高佣渠道转链
            if uid and relation_id:
                return _ztk_channel_convert(
                    target, relation_id, item.get("goods_id", ""), item.get("goods_sign", ""), item.get("goods_name"),
                )
            # 途径 A: 站长/自营/无渠道普通推广 -> 若无官方搜索权限但配置了 ZTK 通道，可走 ZTK 公共高佣转链
            if not self._search_ok and self._ztk_channel_ready() and self.channel_pid:
                return _ztk_channel_convert(
                    target, "", item.get("goods_id", ""), item.get("goods_sign", ""), item.get("goods_name"),
                    allow_public=True,
                )
            # 调用官方 TOP 物料升级接口
            res = self.official.search_goods(target, page=1, page_size=20)
            if res.get("status") != "success":
                return res
            expected = ProductRef(self.platform_code, item.get("goods_id", ""), item["goods_sign"])
            candidates = res.get("data") or []
            matches = [candidate for candidate in candidates if self._matches(candidate, expected)
                       and (not ref.goods_id or not candidate.get("goods_id")
                            or extract_canonical_goods_id(self.platform_code, candidate["goods_id"]) == ref.goods_id)]
            # Real TOP responses re-encrypt IDs even for the same exact product URL.
            # Only the official detail URL's single precise result can bridge that
            # change, with seller/title/image corroboration; never take a keyword's
            # first item or resolve an ambiguous/mismatching response by position.
            if (not matches and official_target and len(candidates) == 1
                    and self._same_official_product(item, candidates[0])
                    and (not ref.goods_id or not candidates[0].get("goods_id")
                         or extract_canonical_goods_id(self.platform_code, candidates[0]["goods_id"]) == ref.goods_id)):
                matches = candidates
            if len(matches) != 1:
                return _error("PRODUCT_IDENTITY_MISMATCH", "精准生链响应与请求商品不一致")
            promoted = matches[0]
            link = promoted.get("coupon_share_url") or promoted.get("click_url")
            if not link or not str(link).startswith(("https://", "http://")):
                return _error("PROMOTION_UNAVAILABLE", "官方未返回可用的券二合一推广链接")
            token = self.official.create_tpwd(link, text=item.get("goods_name") or "精选好物")
            if token.get("status") != "success":
                return token
            password = token.get("password_simple") or token.get("model")
            if not password:
                return _error("PROTOCOL_ERROR", "官方未返回真实淘口令")
            result = {"status": "success", "url": link, "final_link": link, "mobile_url": link, "short_url": "",
                      "we_app_info": {}, "promotion_token": password, "token_type": "tpwd",
                      "attribution_status": "anonymous", "need_auth": False,
                      "is_fallback": False, "goods_id": item.get("goods_id", ""),
                      "goods_sign": item.get("goods_sign", ""), "link_type": "淘宝官方淘口令"}
            expires = _timestamp(token.get("expires_at") or (token.get("raw") or {}).get("expires_at"))
            if expires:
                result["expires_at"] = expires
            result["cacheable"] = True
            return result

        def _promote_via_ztk_only():
            if ztk_tkl_attempted:
                return {"status": "skipped", "reason": "already_attempted_ztk_tkl"}
            target_id = self._preferred_tb_promo_id(ref)
            if not target_id:
                return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
            target = (
                f"https://item.taobao.com/item.htm?{urlencode({'id': target_id})}"
                if str(target_id).isdigit()
                else f"https://uland.taobao.com/item/edetail?{urlencode({'id': target_id})}"
            )
            rel = relation_id if (uid and relation_id) else ""
            return _ztk_channel_convert(
                target, rel, ref.goods_id, ref.goods_sign or ref.goods_id,
                _text(kwargs.get("title") or kwargs.get("name")), allow_public=(not rel),
            )

        def _promote_via_ztk_gaoyong():
            target_id = self._preferred_tb_promo_id(ref)
            if not target_id:
                return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
            rel = relation_id if (uid and relation_id) else ""
            ztk_res = self._guard_provider_result(
                self.ztk.convert_gaoyong_url(target_id, self.channel_pid, relation_id=rel)
            )
            if ztk_res.get("status") != "success":
                return ztk_res
            link = ztk_res.get("final_link") or ztk_res.get("url")
            if not link:
                return _error("PROMOTION_UNAVAILABLE", "折淘客高佣未返回有效链接")
            password = ztk_res.get("tpwd") or ""
            if not password and self._official_ok:
                token = self.official.create_tpwd(link, text=_text(kwargs.get("title") or kwargs.get("name")) or "精选好物")
                if isinstance(token, dict):
                    password = token.get("password_simple") or token.get("model") or ""
            if not password:
                return _error("PROTOCOL_ERROR", "折淘客高佣未生成有效淘口令")
            short_url = _text(ztk_res.get("short_url"))
            is_channel = bool(uid and rel)
            return {
                "status": "success",
                "url": ztk_res.get("url") or link,
                "final_link": short_url or link,
                "mobile_url": ztk_res.get("mobile_url") or link,
                "short_url": short_url,
                "we_app_info": {},
                "promotion_token": password,
                "token_type": "tpwd",
                "attribution_status": "verified" if is_channel else "anonymous",
                "relation_id": rel if is_channel else "",
                "need_auth": False,
                "is_fallback": False,
                "goods_id": ref.goods_id,
                "goods_sign": ref.goods_sign or ref.goods_id,
                "link_type": "折淘客渠道淘口令" if is_channel else "折淘客高佣淘口令",
                "cacheable": True,
            }

        base = []
        if self._official_ok:
            base.append(("tb_official_promote", _promote_via_official_or_ztk))
        if self._ztk_channel_ready() and self.channel_pid:
            base.append(("ztk_tb_promote", _promote_via_ztk_only))
            if hasattr(self.ztk, "convert_gaoyong_url"):
                base.append(("ztk_tb_gaoyong", _promote_via_ztk_gaoyong))

        providers = self._compose_providers(
            "promotion",
            base,
            goods_id_or_sign=goods_id_or_sign,
            uid=uid,
            **kwargs,
        )
        return self._execute_capability("promotion", providers, allow_persist=self._is_real_http())

    def resolve_product_from_link(self, raw_text: str) -> Dict[str, Any]:
        """淘宝独立底层能力：从原始口令、分享链接或加密 ID 提取商品标识并查询官方详情卡（绝不模糊搜标题顶替）。"""
        text = _text(raw_text)
        if not text:
            return _error("INVALID_ARGUMENT", "请输入淘宝商品链接、淘口令或加密商品标识")
        target_id = ""
        if not text.lower().startswith(("http://", "https://")) and re.match(r"^[A-Za-z0-9_-]{8,}$", text):
            target_id = text
        elif text.lower().startswith(("http://", "https://")):
            try:
                u_parts = urlsplit(text)
                if (u_parts.hostname or "").lower() == "uland.taobao.com" and (u_parts.path or "") == "/item/edetail":
                    cand_id = (parse_qs(u_parts.query).get("id") or [""])[0].strip()
                    if cand_id and re.match(r"^[A-Za-z0-9_-]{8,}$", cand_id):
                        target_id = cand_id
            except Exception:
                pass

        if not target_id and self._ztk_channel_ready() and hasattr(self.ztk, "resolve_item_id"):
            id_res = self._guard_provider_result(self.ztk.resolve_item_id(text, pid=self.channel_pid))
            if id_res.get("status") == "success" and id_res.get("item_id"):
                target_id = _text(id_res["item_id"])

        if not target_id or ("e.tb.cn" in text.lower() and target_id.isdigit()):
            if "e.tb.cn" in text.lower():
                return _error(
                    "UNSUPPORTED_TB_NUMERIC_ID",
                    "淘宝联盟已停用纯数字商品ID（及手淘原生 e.tb.cn 普通分享链接）直接转链，请粘贴淘口令、带推广属性的链接或在首页搜索该商品标题",
                )
            return _error("NOT_FOUND", "未能从该链接或口令解析出淘宝商品标识")

        if self._is_truncated_tb_suffix(target_id):
            return _error("INVALID_IDENTITY", "淘宝商品标识不完整，请提供完整加密标识或数字商品ID")

        det = self.get_goods_detail(target_id)
        if det.get("status") == "success" and isinstance(det.get("data"), dict):
            return {"status": "success", "item_id": target_id, "product": det["data"]}
        return det

    def resolve_and_convert_universal(self, raw_text: str, uid=None, **kwargs):
        """
        淘宝万能转链与详情一步反查（对齐 scripts/test_tb_api.py 基准规范 六：直接原样透传折淘客，零模糊搜索兜底）：
        1. 主路径：基于折淘客 `open_gaoyongzhuanlian_tkl.ashx` (`signurl=5`) + 4段式 `channel_pid`，
           单次 HTTP 请求同时完成口令/短链/推广链/加密 ID 转链与完整商品详情提取；
        2. 文案透传：当输入为夹带 `uland.taobao.com` / `item.taobao.com` 的长促销文案时，由 `ZtkClient.convert_channel_url`
           自动透传给折淘客文案接口 `open_gaoyongzhuanlian_tkl_piliang.ashx` 原生解析；
        3. 严禁在转链失败时提取文本标题调用 `search_goods` 模糊搜索取首条商品顶替。
        """
        if not self.active:
            return _error("platform_unavailable", "淘宝暂未启用")
        text = _text(raw_text)
        if not text:
            return _error("INVALID_ARGUMENT", "请输入淘宝链接、淘口令或商品ID")
        if text_has_disallowed_url(text, TAOBAO_DOMAINS):
            return _error("INVALID_TAOBAO_URL", "该链接不属于淘宝/天猫平台，请切换为「自动识别」或对应平台")

        identity = kwargs.get("identity") or {}
        relation_id = _text(identity.get("relation_id")) if isinstance(identity, dict) else ""
        if uid and (not relation_id or relation_id == "0"):
            return _error("AUTH_REQUIRED", "淘宝个人推广需要已核验渠道身份", need_auth=True, is_fallback=False)
        if uid and identity.get("external_id") is not None and _text(identity["external_id"]) != _text(uid):
            return _error("IDENTITY_MISMATCH", "淘宝推广身份与站内用户不一致", need_auth=True, is_fallback=False)

        if "alimama.com" in text.lower() and not re.search(r"[?&](?:itemId|id|item_id)=[A-Za-z0-9_-]{8,}", text):
            return _error(
                "UNSUPPORTED_TB_PORTAL_PAGE",
                "该链接为阿里妈妈选品库/频道列表页而非具体单品链接，请复制单品的推广链接、淘口令或手淘商品分享文案",
            )
        is_bare_id = bool(re.match(r"^[A-Za-z0-9_-]{8,}$", text)) and not text.startswith(("http://", "https://"))
        official_target_id = ""
        if is_bare_id:
            official_target_id = text
        elif text.lower().startswith(("http://", "https://")):
            try:
                u_parts = urlsplit(text)
                if (u_parts.hostname or "").lower() == "uland.taobao.com" and (u_parts.path or "") == "/item/edetail":
                    cand_id = (parse_qs(u_parts.query).get("id") or [""])[0].strip()
                    if cand_id and not cand_id.isdigit() and re.match(r"^[A-Za-z0-9_-]{8,}$", cand_id):
                        official_target_id = cand_id
            except Exception:
                pass
        tkl_input = f"https://uland.taobao.com/item/edetail?id={text}" if is_bare_id else text
        rel_arg = relation_id if (uid and relation_id) else ""

        def _do_ztk_resolve():
            ztk_res = self.ztk.convert_channel_url(
                tkl_input,
                rel_arg,
                self.channel_pid,
                allow_public=True,
            )

            if not isinstance(ztk_res, dict) or ztk_res.get("status") != "success":
                err = ztk_res.get("error_msg") if isinstance(ztk_res, dict) else "淘宝口令或链接解析转链失败"
                is_unauth = self._is_unauthorized_error(ztk_res)
                code = "AUTH_REQUIRED" if is_unauth else (ztk_res.get("error_code") if isinstance(ztk_res, dict) else "PROMOTION_FAILED")
                if code == "UNSUPPORTED_TB_NUMERIC_ID" or "不支持数字id" in _text(err) or ("e.tb.cn" in text.lower() and "商品id解析错误" in _text(err).lower()):
                    code = "UNSUPPORTED_TB_NUMERIC_ID"
                    err = "淘宝联盟已停用纯数字商品ID（及手淘原生 e.tb.cn 普通分享链接）直接转链，请粘贴淘口令、带推广属性的链接或在首页搜索该商品标题"
                return stamp_universal_resolution(_error(code or "PROMOTION_FAILED", err, need_auth=is_unauth))

            link = ztk_res.get("final_link") or ztk_res.get("url") or ""
            short_url = ztk_res.get("short_url") or link
            password = _text(ztk_res.get("tpwd"))
            raw_c = ztk_res.get("raw") if isinstance(ztk_res.get("raw"), dict) else {}

            ztk_fields = extract_ztk_item_fields(
                raw_c,
                zero_fallback_to_size=True,
                include_primary_in_carousel=True,
                default_mall_name="淘宝天猫好店",
                default_sales_tip="0",
            )
            raw_tao_id = clean_product_id(ztk_fields["tao_id"])
            if is_bare_id and raw_tao_id and not self._verify_tb_external_detail_match(raw_tao_id, self.resolve_product_ref(text)):
                return stamp_universal_resolution(_error("PRODUCT_IDENTITY_MISMATCH", "折淘客万能解析返回与请求淘宝商品不一致"))
            tao_id = raw_tao_id or (text if is_bare_id else "")
            canonical_tao_id = extract_canonical_goods_id(self.platform_code, tao_id) or tao_id
            raw_title_val = _text(ztk_fields["title"] or ztk_res.get("title"))
            raw_title = "" if raw_title_val == "渠道推广商品" else raw_title_val
            title = raw_title or "淘宝精选好物"
            pict_url = ztk_fields["image_url"]
            gallery = ztk_fields["carousel_images"]
            final_price = ztk_fields["price"]
            orig_price = ztk_fields["original_price"]
            coupon_money = ztk_fields["coupon_amount"]
            comm_rate = ztk_fields["commission_rate"]
            comm_fee = ztk_fields["commission_amount"]
            mall_name = ztk_fields["mall_name"]
            sales_tip = ztk_fields["sales_tip"]
            raw_item_url = ztk_fields["item_url"] or (f"https://uland.taobao.com/item/edetail?id={tao_id}" if tao_id else "")

            product_card = None
            detail_lookup_failed = False
            has_complete_card = bool(
                tao_id
                and raw_title
                and final_price is not None
                and final_price > 0
                and (pict_url or gallery)
            )
            if has_complete_card:
                detail_payload = {
                    "platform": self.platform_code,
                    "platform_name": self.platform_name,
                    "goods_id": canonical_tao_id,
                    "goods_sign": tao_id,
                    "name": title,
                    "image_url": pict_url or (gallery[0] if gallery else ""),
                    "price": final_price,
                    "original_price": orig_price,
                    "coupon_amount": coupon_money,
                    "has_coupon": coupon_money > 0,
                    "commission_rate": comm_rate,
                    "commission_amount": comm_fee,
                    "sales_tip": sales_tip,
                    "mall_name": mall_name,
                    "desc": _text(raw_c.get("jianjie") or title),
                    "carousel_images": gallery,
                    "product_url": raw_item_url,
                    "detail_tags": [],
                    "service_guarantees": [],
                    "shop_ratings": {},
                    "specs": [{"label": "店铺名称", "value": mall_name}] if mall_name else [],
                    "coupon_info": {},
                }
                product_card = ProductDetail(**detail_payload).to_dict(exclude_unset=True)
            elif tao_id:
                det = self.get_goods_detail(tao_id, goods_id=canonical_tao_id, goods_sign=tao_id)
                det_data = det.get("data") if isinstance(det, dict) else None
                if (
                    isinstance(det, dict)
                    and det.get("status") == "success"
                    and isinstance(det_data, dict)
                    and (det_data.get("price") or 0) > 0
                    and _text(det_data.get("name"))
                ):
                    product_card = det_data
                    title = _text(product_card.get("name")) or title
                    final_price = product_card.get("price") or final_price
                    coupon_money = product_card.get("coupon_amount", coupon_money)
                    canonical_tao_id = product_card.get("goods_id") or canonical_tao_id
                    tao_id = product_card.get("goods_sign") or tao_id
                elif isinstance(det, dict) and det.get("error_code") == "PRODUCT_OFF_PROMOTION":
                    return stamp_universal_resolution(det)
                else:
                    detail_lookup_failed = True

            lower_urls = f"{text} {link} {short_url}".lower()
            is_converted_link = (
                bool(link and link.strip() != text.strip())
                or bool(short_url and short_url.strip() != text.strip())
                or "s.click.taobao.com" in lower_urls
                or "ali_trackid=" in lower_urls
                or "union_lens=" in lower_urls
            )
            verified_coupon_or_venue = bool(
                not tao_id
                and not is_bare_id
                and any(
                    kw in lower_urls
                    for kw in (
                        "pages.tmall.com/wow/",
                        "pages-fast.m.taobao.com/wow/",
                        "uland.taobao.com/quan",
                        "mo.m.taobao.com",
                    )
                )
                and (is_converted_link or ("/wow/z/lianmeng/" not in lower_urls and "wh_alimama=" not in lower_urls))
            )
            payload = {
                "status": "success",
                "url": link,
                "final_link": short_url or link,
                "mobile_url": link,
                "short_url": short_url,
                "we_app_info": {},
                "promotion_token": password,
                "token_type": "tpwd" if password else "",
                "attribution_status": "verified" if (uid and relation_id) else "anonymous",
                "relation_id": relation_id if (uid and relation_id) else "",
                "need_auth": False,
                "is_fallback": False,
                "goods_id": canonical_tao_id,
                "goods_sign": tao_id,
                "title": title,
                "price": final_price,
                "coupon_amount": coupon_money,
                "link_type": "折淘客渠道淘口令" if (uid and relation_id) else "淘宝高佣淘口令",
                "product": product_card,
                **({"detail_lookup_failed": True} if (detail_lookup_failed and product_card is None) else {}),
            }
            return stamp_universal_resolution(
                payload,
                verified_coupon_or_venue=verified_coupon_or_venue,
            )

        def _do_official_id_resolve():
            conv = dict(self.convert_promotion_url(official_target_id, uid=uid, identity=identity))
            conv.pop("_provider", None)
            if conv.get("status") != "success":
                return stamp_universal_resolution(conv)
            det = self.get_goods_detail(official_target_id)
            if det.get("status") == "success":
                conv["product"] = det["data"]
            else:
                conv["detail_lookup_failed"] = True
            return stamp_universal_resolution(conv)

        base = []
        if self._ztk_channel_ready() and self.channel_pid:
            base.append(("ztk_tb_universal", _do_ztk_resolve))
            if official_target_id and self._official_ok and self._search_ok and not (uid and relation_id):
                base.append(("tb_official_universal", _do_official_id_resolve))
        elif official_target_id and self._official_ok:
            base.append(("tb_official_universal", _do_official_id_resolve))

        if not base and not self._has_extra_providers("universal"):
            return _error("CONFIG_ERROR", "淘宝折淘客万能转链未配置完整 (需 ZTK_APPKEY / ZTK_SID / TB_CHANNEL_PID)")

        providers = self._compose_providers("universal", base, raw_text=text, uid=uid, **kwargs)
        return self._execute_capability(
            "universal",
            providers,
            allow_persist=self._is_real_http(),
            metric_operation="resolve_universal",
        )

    def _ztk_channel_ready(self) -> bool:
        appkeys = getattr(self.ztk, "appkey_list", None)
        sid = getattr(self.ztk, "sid", None)
        return bool(isinstance(appkeys, (tuple, list)) and appkeys and isinstance(sid, str) and sid.strip())

    def check_authority(self, uid):
        unavailable = self._available("authorization", is_authorized=False)
        if unavailable:
            return unavailable

        def _auth_via_official():
            off_http = getattr(self.official, "http_session", None) or _TB_SESSION
            if GLOBAL_PROVIDER_COOLDOWN.is_cooling("taobao", "tb_official_session", http_session=off_http):
                return {"status": "skipped", "reason": "tb_official_session_cooling"}
            current_sess = getattr(self.official, "session_key", None)
            if getattr(self, "_last_invalid_session", None) and current_sess and current_sess == self._last_invalid_session:
                if hasattr(self.official, "try_refresh_session") and callable(self.official.try_refresh_session):
                    try:
                        self.official.try_refresh_session()
                    except Exception:
                        pass
                current_sess = getattr(self.official, "session_key", None)
                if current_sess == self._last_invalid_session:
                    GLOBAL_PROVIDER_COOLDOWN.mark_cooling(
                        "taobao", "tb_official_session", 300.0, reason="invalid_sessionkey", http_session=off_http,
                    )
                    return _error("SESSION_EXPIRED", "淘宝渠道授权服务正在刷新凭证，请稍后再试")

            res = self.official.check_authority(uid)
            if isinstance(res, dict) and res.get("status") == "success":
                self._last_invalid_session = None
                return res
            err_code = str((res or {}).get("error_code") or "")
            if err_code in ("IDENTITY_MISMATCH", "AMBIGUOUS_IDENTITY", "INVALID_IDENTITY", "INCOMPLETE_IDENTITY"):
                return res
            is_invalid_sess = isinstance(res, dict) and (
                "invalid-sessionkey" in str(res.get("error_msg", "")).lower() or err_code == "27"
            )
            if hasattr(self.official, "try_refresh_session") and callable(self.official.try_refresh_session):
                try:
                    if self.official.try_refresh_session():
                        retry_res = self.official.check_authority(uid)
                        if isinstance(retry_res, dict) and retry_res.get("status") == "success":
                            self._last_invalid_session = None
                            return retry_res
                except Exception:
                    pass
            if is_invalid_sess and isinstance(getattr(self.official, "session_key", None), str):
                self._last_invalid_session = self.official.session_key
            if is_invalid_sess:
                GLOBAL_PROVIDER_COOLDOWN.mark_cooling(
                    "taobao", "tb_official_session", 300.0, reason="invalid_sessionkey", http_session=off_http,
                )
                return _error("SESSION_EXPIRED", "淘宝渠道授权服务正在刷新凭证，请稍后再试")
            return res

        def _auth_via_ztk():
            return self.ztk.check_publisher_relation(uid)

        ztk_ready = self._ztk_channel_ready() and hasattr(self.ztk, "check_publisher_relation")
        off_ready = self._official_auth_ok and hasattr(self.official, "check_authority")
        ztk_first = ztk_ready and not self._has_tb_refresh_token

        base = []
        if ztk_first:
            base.append(("ztk_tb_auth", _auth_via_ztk))
            if off_ready:
                base.append(("tb_official_auth", _auth_via_official))
        else:
            if off_ready:
                base.append(("tb_official_auth", _auth_via_official))
            if ztk_ready:
                base.append(("ztk_tb_auth", _auth_via_ztk))

        providers = self._compose_providers("check_authority", base, uid=uid)
        return self._execute_capability("check_authority", providers, allow_persist=self._is_real_http())

    def generate_authority_url(self, uid, **kwargs):
        unavailable = self._available("authorization", is_authorized=False)
        if unavailable:
            return unavailable

        def _official_auth_url():
            off_http = getattr(self.official, "http_session", None) or _TB_SESSION
            local_inviter = bool(_text(getattr(self.official, "inviter_code", "")))
            if not local_inviter and GLOBAL_PROVIDER_COOLDOWN.is_cooling("taobao", "tb_official_session", http_session=off_http):
                return {"status": "skipped", "reason": "tb_official_session_cooling"}
            res = self.official.generate_authority_url(uid, **kwargs) if kwargs else self.official.generate_authority_url(uid)
            if not local_inviter and isinstance(res, dict) and res.get("status") != "success":
                err_code = str(res.get("error_code") or "")
                if "invalid-sessionkey" in str(res.get("error_msg", "")).lower() or err_code == "27":
                    GLOBAL_PROVIDER_COOLDOWN.mark_cooling(
                        "taobao", "tb_official_session", 300.0, reason="invalid_sessionkey", http_session=off_http,
                    )
            return res

        def _ztk_auth_url(with_inviter: bool = False):
            inv = getattr(self.official, "inviter_code", "") if with_inviter else ""
            if inv:
                return self.ztk.generate_authority_url(uid, inviter_code=inv)
            return self.ztk.generate_authority_url(uid)

        ztk_ready = self._ztk_channel_ready() and hasattr(self.ztk, "generate_authority_url")
        off_ready = self._official_auth_ok and hasattr(self.official, "generate_authority_url")
        ztk_first = ztk_ready and not getattr(self.official, "inviter_code", "")

        base = []
        if ztk_first:
            base.append(("ztk_tb_auth_url", lambda: _ztk_auth_url(False)))
            if off_ready:
                base.append(("tb_official_auth_url", _official_auth_url))
        else:
            if off_ready:
                base.append(("tb_official_auth_url", _official_auth_url))
            if ztk_ready:
                base.append(("ztk_tb_auth_url", lambda: _ztk_auth_url(True)))

        providers = self._compose_providers("generate_authority_url", base, uid=uid, **kwargs)
        return self._execute_capability("generate_authority_url", providers, allow_persist=self._is_real_http())

    def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs):
        unavailable = self._available("orders")
        if unavailable:
            return {**unavailable, "order_list": [], "has_more": False, "next_cursor": None}

        def _orders_via_ztk():
            return self.ztk.query_orders(
                start_time, end_time, page=page, page_size=page_size, cursor=kwargs.get("cursor"),
            )

        base = [("ztk_tb_orders", _orders_via_ztk)] if self._orders_ok else []
        providers = self._compose_providers(
            "orders", base, start_time=start_time, end_time=end_time, page=page, page_size=page_size, **kwargs,
        )
        result = self._execute_capability("orders", providers, allow_persist=self._is_real_http())
        if result.get("status") != "success":
            return {**result, "order_list": [], "has_more": False, "next_cursor": None}
        return result

    def normalize_order(self, order):
        try:
            status = int(order.get("tk_status"))
        except (TypeError, ValueError):
            status = _text(order.get("tk_status"))
        settled, invalid = status == 3, status == 13
        estimate, settlement = _money(order.get("pub_share_pre_fee")), _money(order.get("pub_share_fee"))
        commission = settlement if settled else estimate
        relation = _text(order.get("relation_id"))
        raw_item_id = _text(order.get("item_id"))
        canonical_gid = extract_canonical_goods_id(self.platform_code, raw_item_id) or raw_item_id
        result = {
            "platform": self.platform_code, "order_sn": _text(order.get("trade_id")),
            "trade_id": _text(order.get("trade_id")), "trade_parent_id": _text(order.get("trade_parent_id")),
            "goods_id": canonical_gid, "goods_name": order.get("item_title") or "",
            "goods_thumbnail_url": order.get("item_img") or "", "goods_quantity": int(order.get("item_num") or 1),
            "order_amount_yuan": _money(order.get("alipay_total_price")) or 0.0,
            "promotion_amount_yuan": commission if commission is not None else 0.0,
            "estimated_commission_yuan": estimate, "settled_commission_yuan": settlement,
            "commission_basis": "settlement_estimate" if settled and settlement is not None else
                                "missing_settlement" if settled else "payment_estimate" if estimate is not None else "missing_estimate",
            "order_status": status, "order_status_desc": {12: "已付款", 14: "已收货待结算", 3: "已结算", 13: "已失效"}.get(status, "未知状态"),
            "is_settled": settled, "is_invalid": invalid,
            # refund_tag indicates an after-sale claim, not a successful refund.
            "is_refund": False, "refund_tag": _text(order.get("refund_tag")),
            "relation_id": relation, "attribution_identity": {"relation_id": relation} if relation and relation != "0" else {},
            "order_pay_time": _timestamp(order.get("tk_paid_time") or order.get("tb_paid_time")),
            "order_create_time": _timestamp(order.get("tk_create_time")),
            "order_settle_time": _timestamp(order.get("tk_earning_time")),
            "source_updated_at": _timestamp(order.get("modified_time") or order.get("tk_modified_time")),
        }
        for key in ("adzone_id", "site_id", "special_id", "order_scene"):
            if order.get(key) is not None:
                result[key] = _text(order[key])
        result["order_modify_at"] = result["source_updated_at"]
        result["order_pay_time_str"] = datetime.fromtimestamp(result["order_pay_time"], SHANGHAI).strftime("%Y-%m-%d %H:%M:%S") if result["order_pay_time"] else ""
        return result
