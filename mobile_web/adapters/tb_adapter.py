# -*- coding: utf-8 -*-
"""淘宝平台边界：官方商品、第三方ID榜单、核验身份与渠道订单映射。"""
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

logger = logging.getLogger(__name__)

from adapters.base import BasePlatformAdapter
from infrastructure.api_governance import (
    is_real_requests_session,
    stamp_universal_resolution,
)
from infrastructure.http_utils import (
    SHANGHAI,
    clean_text as _text,
    error_dict as _error,
    extract_ztk_item_fields,
    parse_finite_money as _money,
    parse_shanghai_timestamp as _timestamp,
)
from models.product import (
    DetailSnapshot,
    ProductRef,
    ProductSummary,
    ProductDetail,
    clean_product_id,
    extract_canonical_goods_id,
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
        self._ensure_provider_state()
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
        self.ztk = ztk_client if ztk_client is not None else ZtkClient(
            setting("ztk_appkey"), setting("ztk_sid"), appkey_list=ztk_keys, sid_list=ztk_sids)
        pid_val = _text(setting("tb_pid"))
        pid_prefix = _text(setting("tb_pid_prefix"))
        if not pid_prefix and pid_val.startswith("mm_") and len(pid_val.split("_")) == 4:
            pid_prefix = pid_val.rsplit("_", 1)[0]
        if not pid_prefix:
            pid_prefix = getattr(self.official, "pid_prefix", "") or "mm_328750149_3161250458"
        channel_adzone = _text(setting("tb_channel_adzone_id")) or adzone or "116343300219"
        self.channel_pid = _text(setting("tb_channel_pid")) or f"{pid_prefix}_{channel_adzone}"
        self._official_ok = bool(official_client is not None or key and secret)
        self._search_ok = bool(official_client is not None or self._official_ok and adzone)
        has_hdk_key = bool(getattr(self.hdk, "appkey_list", None) or setting("hdk_api_key") or hdk_keys)
        self._hdk_ok = bool(hdk_client is not None or has_hdk_key)
        has_ztk_key = bool(getattr(self.ztk, "appkey_list", None) or setting("ztk_appkey") or ztk_keys)
        has_ztk_sid = bool(getattr(self.ztk, "sid_list", None) or setting("ztk_sid") or ztk_sids)
        has_ztk_channel = bool(has_ztk_key and has_ztk_sid)
        self._ztk_channel_ok = bool(ztk_client is not None or has_ztk_channel)
        self._official_auth_ok = bool(
            official_client is not None or self._official_ok and (setting("tb_session") or has_ztk_channel)
        )
        self._orders_ok = bool(ztk_client is not None or has_ztk_channel)
        self.tb_name = _text(setting("hdk_tb_name") or setting("tb_name") or setting("tb_account_name"))
        if self._hdk_ok and self.hdk:
            self.attach_hdk(self.hdk)
        self._refresh_capabilities()

    def attach_hdk(self, hdk_client: HdkClient) -> None:
        """挂载好单库 (HDK) 为淘宝高配额能力提供者：转链与订单能力设为主力/首选，未授权时自动无缝降级至折淘客。"""
        self.hdk = hdk_client
        self.register_provider("promotion", "hdk_tb_promotion", self._make_hdk_promotion(hdk_client), prepend=True)
        self.register_provider("orders", "hdk_tb_orders", self._make_hdk_orders(hdk_client), prepend=True)

    def _make_hdk_promotion(self, client: HdkClient):
        def _promote(goods_id_or_sign="", uid=None, **kwargs):
            ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
            target_id = ref.goods_id if str(ref.goods_id).isdigit() else (ref.goods_sign or ref.goods_id)
            if not target_id:
                return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
            if not getattr(self, "tb_name", ""):
                # 好单库 ratesurl 必填已在好单库授权的 tb_name，未配置时直接顺延给折淘客
                logger.info("[TB转链-好单库] 未配置 hdk_tb_name，自动顺延至折淘客通道: target_id=%s", target_id)
                return _error("UPSTREAM_PROVIDER_UNAUTHORIZED", "未配置好单库已授权淘宝账号(hdk_tb_name)")
            pid = self.channel_pid or getattr(self.official, "pid", "")
            if not pid:
                return _error("CONFIG_ERROR", "缺少淘宝 PID 配置")
            identity = kwargs.get("identity") or {}
            relation_id = _text(identity.get("relation_id")) if isinstance(identity, dict) else ""
            title = _text(kwargs.get("title") or kwargs.get("name") or "精选好物")
            res = client.convert_tb_promotion_link(
                target_id,
                pid=pid,
                tb_name=self.tb_name,
                title=title,
                relation_id=relation_id or None,
            )
            if res.get("status") != "success":
                err_code = res.get("error_code")
                if err_code in ("AUTH_REQUIRED", "PROVIDER_UNAUTHORIZED") or "未授权" in str(res.get("error_msg")):
                    logger.warning("[TB转链-好单库] 好单库淘宝推客未授权，平滑降级至折淘客通道: %s", res.get("error_msg"))
                    return _error("UPSTREAM_PROVIDER_UNAUTHORIZED", res.get("error_msg") or "好单库推客未授权")
                logger.warning("[TB转链-好单库] 转链失败(%s: %s)，准备降级至备用通道", err_code, res.get("error_msg"))
                return res
            logger.info("[TB转链-好单库主力] 转链成功: target_id=%s, tpwd=%s", target_id, res.get("tpwd"))
            link = res.get("final_link") or res.get("url")
            short_url = res.get("short_url") or link
            password = res.get("tpwd") or res.get("taoword") or ""
            is_channel = bool(uid and relation_id)
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

    def _make_hdk_orders(self, client: HdkClient):
        def _orders(start_time, end_time, page=1, page_size=50, **kwargs):
            try:
                s_ts = int(start_time.timestamp()) if hasattr(start_time, "timestamp") else int(start_time)
                e_ts = int(end_time.timestamp()) if hasattr(end_time, "timestamp") else int(end_time)
            except Exception:
                return _error("INVALID_ARGUMENT", "淘宝订单时间范围无效")
            cursor = kwargs.get("cursor")
            res = client.query_tb_orders(s_ts, e_ts, page=page, page_size=page_size, cursor=cursor)
            if res.get("status") != "success":
                logger.warning("[TB订单同步-好单库主力] 拉取失败(%s: %s)，顺延至折淘客通道", res.get("error_code"), res.get("error_msg"))
                return res
            logger.info("[TB订单同步-好单库主力] 拉取成功: 订单数=%d, has_more=%s", len(res.get("order_list") or []), res.get("has_more"))
            return res
        return _orders

    def _refresh_capabilities(self) -> None:
        self.capabilities = {
            "recommend": bool((self._official_ok and self._hdk_ok) or self._has_extra_providers("recommend")),
            "search": bool(self._search_ok or self._has_extra_providers("search")),
            "detail": bool(self._official_ok or self._has_extra_providers("detail")),
            "promotion": bool(
                self._search_ok
                or (self._ztk_channel_ready() and bool(self.channel_pid))
                or self._has_extra_providers("promotion")
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

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs):
        unavailable = self._available("search")
        if unavailable:
            return {**unavailable, "data": []}

        def _search_via_official():
            # Catalog content is always public/site-wide, not personalized promotion content.
            res = self.official.search_goods(keyword, page=page, page_size=page_size)
            if res.get("status") != "success":
                return res
            return {**{key: value for key, value in res.items() if key not in ("data", "raw")},
                    "data": [self._summary(item) for item in res.get("data", [])]}

        base = [("tb_official_search", _search_via_official)] if self._search_ok else []
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

        base = [("hdk_plus_tb_official_recommend", _recommend_via_hdk_official)] if (self._official_ok and self._hdk_ok) else []
        providers = self._compose_providers(
            "recommend", base, channel_type=channel, offset=offset, limit=limit,
            uid=uid, detail_sink=detail_sink, **kwargs,
        )
        res = self._execute_capability("recommend", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def _official_detail(self, ref):
        if not ref.key:
            return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")
        res = self.official.get_goods_details([ref.key])
        if res.get("status") != "success":
            return res
        items = [item for item in res.get("data", []) if self._matches(item, ref)]
        if not items:
            return _error("PRODUCT_IDENTITY_MISMATCH", "官方商品详情未证实请求商品身份")
        item = dict(items[0])
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
        return {"status": "success", "data": item}

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs):
        unavailable = self._available("detail")
        if unavailable:
            return unavailable
        ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        if not ref.key:
            return _error("INVALID_ARGUMENT", "缺少淘宝商品标识")

        def _detail_via_official():
            res = self._official_detail(ref)
            if res.get("status") != "success":
                return res
            return {"status": "success", "data": self._detail(res["data"])}

        base = [("tb_official_detail", _detail_via_official)] if self._official_ok else []
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

        def _ztk_channel_convert(target, rel, goods_id_val, goods_sign_val, title_hint, *, allow_public=False):
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
            result["cacheable"] = bool(expires)
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
                fallback_id = ref.goods_id if str(ref.goods_id).isdigit() else (ref.goods_sign or ref.goods_id or item["goods_sign"])
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
            result["cacheable"] = bool(expires)
            return result

        def _promote_via_ztk_only():
            target_id = ref.goods_id if str(ref.goods_id).isdigit() else (ref.goods_sign or ref.goods_id)
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

        base = []
        if self._official_ok:
            base.append(("tb_official_promote", _promote_via_official_or_ztk))
        elif self._ztk_channel_ready() and self.channel_pid:
            base.append(("ztk_tb_promote", _promote_via_ztk_only))

        providers = self._compose_providers(
            "promotion",
            base,
            goods_id_or_sign=goods_id_or_sign,
            uid=uid,
            **kwargs,
        )
        return self._execute_capability("promotion", providers, allow_persist=self._is_real_http())

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
            tao_id = clean_product_id(ztk_fields["tao_id"]) or (text if is_bare_id else "")
            canonical_tao_id = extract_canonical_goods_id(self.platform_code, tao_id) or tao_id
            title = ztk_fields["title"] or _text(ztk_res.get("title") or "淘宝精选好物")
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
            if tao_id and title:
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

            lower_urls = f"{text} {link} {short_url}".lower()
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
                and "/wow/z/lianmeng/" not in lower_urls
                and "wh_alimama=" not in lower_urls
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
            }
            return stamp_universal_resolution(
                payload,
                verified_coupon_or_venue=verified_coupon_or_venue,
            )

        def _do_bare_id_resolve():
            conv = self.convert_promotion_url(text, uid=uid, identity=identity)
            if conv.get("status") != "success":
                return stamp_universal_resolution(conv, single_item_evidence=True)
            det = self.get_goods_detail(text)
            if det.get("status") == "success":
                conv["product"] = det["data"]
            else:
                conv["detail_lookup_failed"] = True
            return stamp_universal_resolution(conv, single_item_evidence=True)

        if self._ztk_channel_ready() and self.channel_pid:
            base = [("ztk_tb_universal", _do_ztk_resolve)]
        elif is_bare_id and self._official_ok:
            base = [("tb_official_universal", _do_bare_id_resolve)]
        else:
            base = []

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
            current_sess = getattr(self.official, "session_key", None)
            if getattr(self, "_last_invalid_session", None) and current_sess and current_sess == self._last_invalid_session:
                if hasattr(self.official, "try_refresh_session") and callable(self.official.try_refresh_session):
                    try:
                        self.official.try_refresh_session()
                    except Exception:
                        pass
                current_sess = getattr(self.official, "session_key", None)
                if current_sess == self._last_invalid_session:
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
                return _error("SESSION_EXPIRED", "淘宝渠道授权服务正在刷新凭证，请稍后再试")
            return res

        def _auth_via_ztk():
            return self.ztk.check_publisher_relation(uid)

        base = []
        if self._official_auth_ok and hasattr(self.official, "check_authority"):
            base.append(("tb_official_auth", _auth_via_official))
        if self._ztk_channel_ready() and hasattr(self.ztk, "check_publisher_relation"):
            base.append(("ztk_tb_auth", _auth_via_ztk))

        providers = self._compose_providers("check_authority", base, uid=uid)
        return self._execute_capability("check_authority", providers, allow_persist=self._is_real_http())

    def generate_authority_url(self, uid, **kwargs):
        unavailable = self._available("authorization", is_authorized=False)
        if unavailable:
            return unavailable

        def _official_auth_url():
            return self.official.generate_authority_url(uid, **kwargs) if kwargs else self.official.generate_authority_url(uid)

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
        result = {
            "platform": self.platform_code, "order_sn": _text(order.get("trade_id")),
            "trade_id": _text(order.get("trade_id")), "trade_parent_id": _text(order.get("trade_parent_id")),
            "goods_id": _text(order.get("item_id")), "goods_name": order.get("item_title") or "",
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
