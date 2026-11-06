# -*- coding: utf-8 -*-
"""淘宝平台边界：官方商品、第三方ID榜单、核验身份与渠道订单映射。"""
import math
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from adapters.base import BasePlatformAdapter
from models.product import DetailSnapshot, ProductRef, ProductSummary, ProductDetail
from infrastructure.tb_client import TaobaoClient
from infrastructure.hdk_client import HdkClient
from infrastructure.ztk_client import ZtkClient, ORDER_WINDOW_SECONDS

SHANGHAI = timezone(timedelta(hours=8))


def _text(value):
    return str(value).strip() if value is not None else ""


def _money(value):
    try:
        value = float(value)
        return round(value, 2) if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _timestamp(value):
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return int(numeric / 1000 if numeric > 10**11 else numeric)
    except (TypeError, ValueError):
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return int((date if date.tzinfo else date.replace(tzinfo=SHANGHAI)).timestamp())
        except (ValueError, TypeError, OverflowError):
            return None


def _error(code, message, **extra):
    return {"status": "error", "error_code": code, "error_msg": message, **extra}


class TaobaoAdapter(BasePlatformAdapter):
    platform_code = "taobao"
    platform_name = "淘宝"
    channels = (
        {"channel_type": 5, "name": "实时销量榜", "icon": "fire"},
        {"channel_type": 1, "name": "今日销量榜", "icon": "crown"},
    )
    required_identity_fields = ("relation_id",)
    requires_authorized_promotion = True
    order_window_seconds = ORDER_WINDOW_SECONDS
    legacy_order_statuses = {"settled": [3], "refund": []}

    def __init__(self, settings=None, official_client=None, hdk_client=None, ztk_client=None, **overrides):
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
        self.hdk = hdk_client if hdk_client is not None else HdkClient(setting("hdk_api_key"))
        self.ztk = ztk_client if ztk_client is not None else ZtkClient(setting("ztk_appkey"), setting("ztk_sid"))
        pid_val = _text(setting("tb_pid"))
        pid_prefix = _text(setting("tb_pid_prefix"))
        if not pid_prefix and pid_val.startswith("mm_") and len(pid_val.split("_")) == 4:
            pid_prefix = pid_val.rsplit("_", 1)[0]
        if not pid_prefix:
            pid_prefix = getattr(self.official, "pid_prefix", "") or "mm_328750149_3161250458"
        channel_adzone = _text(setting("tb_channel_adzone_id")) or adzone or "116343300219"
        self.channel_pid = _text(setting("tb_channel_pid")) or f"{pid_prefix}_{channel_adzone}"
        official_ok = bool(official_client is not None or key and secret)
        search_ok = bool(official_client is not None or official_ok and adzone)
        self.capabilities = {
            "recommend": bool(official_ok and (hdk_client is not None or setting("hdk_api_key"))),
            "search": search_ok, "detail": official_ok, "promotion": search_ok,
            "authorization": bool(official_client is not None or official_ok and setting("tb_session")),
            "orders": bool(ztk_client is not None or setting("ztk_appkey") and setting("ztk_sid")),
        }

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
        val = _text(goods_sign or goods_id or goods_id_or_sign)
        return ProductRef(self.platform_code, val, val)

    @staticmethod
    def _matches(item, ref):
        returned = _text(item.get("goods_sign"))
        echoed = _text(item.get("input_item_iid"))
        original = _text(item.get("goods_id"))
        key = ref.goods_sign or ref.goods_id
        return bool(key and key in (returned, echoed, original))

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
        raw_id = _text(item.get("goods_sign") or item.get("goods_id"))
        # Explicitly whitelist DTO fields. Never cache click_url/coupon_share_url/raw_item.
        data = {
            "platform": self.platform_code, "platform_name": self.platform_name,
            "goods_id": raw_id, "goods_sign": raw_id,
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
        data.update({"desc": item.get("goods_desc") or "", "carousel_images": item.get("carousel_images") or [],
                     "detail_tags": item.get("detail_tags") or [], "service_guarantees": [],
                     "shop_ratings": {}, "specs": [], "coupon_info": {},
                     "product_url": self._official_product_url(((item.get("raw_item") or {}).get("item_basic_info") or {}).get("item_url"))})
        return ProductDetail(**data).to_dict(exclude_unset=True)

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs):
        # Catalog content is always public/site-wide, not personalized promotion content.
        res = self.official.search_goods(keyword, page=page, page_size=page_size)
        if res.get("status") != "success":
            return res
        return {**{key: value for key, value in res.items() if key not in ("data", "raw")},
                "data": [self._summary(item) for item in res.get("data", [])]}

    def get_recommend_goods(self, channel_type=5, offset=0, limit=20, uid=None, detail_sink=None, **kwargs):
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
        # Legacy offset-only callers can replay source pages; normal callers pass cursor.
        while consumed < offset:
            prior = self.hdk.get_ranked_ids(sale_type, limit=min(100, offset - consumed), cursor=source_cursor)
            if prior.get("status") != "success":
                return prior
            count = prior.get("source_count", len(prior.get("item_ids", [])))
            if count < 1 or not prior.get("has_more"):
                return {"status": "success", "data": [], "offset": offset, "next_offset": consumed + count,
                        "next_cursor": None, "has_more": False, "total_count": prior.get("total_count")}
            consumed += count
            source_cursor = prior.get("next_cursor")
        source = self.hdk.get_ranked_ids(sale_type, limit=limit, cursor=source_cursor)
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
                verified["goods_id"] = sign
                by_id[source_id] = (verified, fetched_at)
                source_by_sign[sign] = source_id
        items, seen = [], set()
        seeds = [] if detail_sink is not None else None
        for source_id in ids:
            matched = by_id.get(source_id)
            if matched and matched[0]["goods_sign"] not in seen:
                item, fetched_at = matched
                seen.add(item["goods_sign"])
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
                 or source.get("next_cursor") == source_cursor)):
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
            item["goods_id"] = echoed
        else:
            canonical = _text(item.get("goods_sign") or item.get("goods_id"))
            item["goods_sign"] = canonical
            item["goods_id"] = canonical
        return {"status": "success", "data": item}

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs):
        res = self._official_detail(self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign))
        if res.get("status") != "success":
            return res
        return {"status": "success", "data": self._detail(res["data"])}

    def _is_unauthorized_error(self, err) -> bool:
        """识别淘宝/折淘客网关抛出的渠道未授权或 relationId 无效错误。"""
        if not err:
            return False
        if isinstance(err, dict):
            if err.get("need_auth") is True or err.get("error_code") == "AUTH_REQUIRED":
                return True
            if err.get("status") == 301:
                return True
            msg = " ".join(str(err.get(key) or "") for key in ("error_msg", "msg", "content")).strip()
        else:
            msg = str(err)
        return bool("relationId" in msg or "relation_id" in msg or "无效的relation" in msg
                    or re.search(r"(?:用户|推手|渠道).{0,12}(?:未授权|未备案|失效|无效)", msg))

    def convert_promotion_url(self, goods_id_or_sign, uid=None, **kwargs):
        identity = kwargs.get("identity") or {}
        relation_id = _text(identity.get("relation_id")) if isinstance(identity, dict) else ""
        if uid and (not relation_id or relation_id == "0"):
            return _error("AUTH_REQUIRED", "淘宝个人推广需要已核验渠道身份", need_auth=True, is_fallback=False)
        if uid and identity.get("external_id") is not None and _text(identity["external_id"]) != _text(uid):
            return _error("IDENTITY_MISMATCH", "淘宝推广身份与站内用户不一致", need_auth=True, is_fallback=False)
        ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
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
            target = "https://item.taobao.com/item.htm?" + urlencode({"id": ref.goods_id or item["goods_sign"]})
        # 途径 B: 推手渠道推广 -> 调用折淘客高佣渠道转链
        if uid and relation_id:
            ztk_res = self.ztk.convert_channel_url(target, relation_id, self.channel_pid)
            if not isinstance(ztk_res, dict) or ztk_res.get("status") != "success":
                err = ztk_res.get("error_msg") if isinstance(ztk_res, dict) else "折淘客渠道转链失败"
                is_unauth = self._is_unauthorized_error(ztk_res)
                code = "AUTH_REQUIRED" if is_unauth else (ztk_res.get("error_code") if isinstance(ztk_res, dict) else "PROMOTION_FAILED")
                return _error(code or "PROMOTION_FAILED", err, need_auth=is_unauth)
            link = ztk_res.get("final_link") or ztk_res.get("url")
            if not link or not str(link).startswith(("https://", "http://")):
                return _error("PROMOTION_UNAVAILABLE", "折淘客未返回有效渠道推广链接")
            password = ztk_res.get("tpwd") or ""
            if not password:
                token = self.official.create_tpwd(link, text=item.get("goods_name") or "精选好物")
                password = token.get("password_simple") or token.get("model") or ""
            if not password:
                return _error("PROTOCOL_ERROR", "渠道转链未生成有效淘口令")
            result = {
                "status": "success", "url": link, "final_link": link, "mobile_url": link, "short_url": "",
                "we_app_info": {}, "promotion_token": password, "token_type": "tpwd",
                "attribution_status": "verified", "relation_id": relation_id, "need_auth": False,
                "is_fallback": False, "goods_id": item.get("goods_id", ""),
                "goods_sign": item.get("goods_sign", ""), "link_type": "折淘客渠道淘口令",
            }
            expires = _timestamp(ztk_res.get("expires_at"))
            if expires:
                result["expires_at"] = expires
            result["cacheable"] = bool(expires)
            return result

        # 途径 A: 站长/自营/无渠道普通推广 -> 调用官方 TOP 物料升级接口
        res = self.official.search_goods(target, page=1, page_size=20)
        if res.get("status") != "success":
            return res
        expected = ProductRef(self.platform_code, "", item["goods_sign"])
        candidates = res.get("data") or []
        matches = [candidate for candidate in candidates if self._matches(candidate, expected)
                   and (not ref.goods_id or not candidate.get("goods_id") or candidate["goods_id"] == ref.goods_id)]
        # Real TOP responses re-encrypt IDs even for the same exact product URL.
        # Only the official detail URL's single precise result can bridge that
        # change, with seller/title/image corroboration; never take a keyword's
        # first item or resolve an ambiguous/mismatching response by position.
        if (not matches and official_target and len(candidates) == 1
                and self._same_official_product(item, candidates[0])
                and (not ref.goods_id or not candidates[0].get("goods_id") or candidates[0]["goods_id"] == ref.goods_id)):
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

    def check_authority(self, uid):
        return self.official.check_authority(uid)

    def generate_authority_url(self, uid, **kwargs):
        return self.official.generate_authority_url(uid)

    def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs):
        result = self.ztk.query_orders(start_time, end_time, page=page, page_size=page_size, cursor=kwargs.get("cursor"))
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
