# -*- coding: utf-8 -*-
"""拼多多边界：SDK 调用、原始字段转换、商品标识与推广降级策略。"""
import html as html_lib
import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlencode
from adapters.base import BasePlatformAdapter
from infrastructure.api_governance import (
    is_real_requests_session,
    stamp_universal_resolution,
)
from infrastructure.hdk_client import HdkClient
from infrastructure.http_utils import clean_money, error_dict as _error, normalize_http_url
from infrastructure.logging_utils import format_exc_summary, get_app_logger
from models.product import (
    PDD_DOMAINS,
    PDD_GOODS_SIGN_RE,
    ProductDetail,
    ProductRef,
    ProductSummary,
    URL_RE,
    clean_product_id,
    url_matches_domains,
)
import infrastructure.pdd_client as _pdd_client_mod
from infrastructure.pdd_client import (
    call_pdd_api,
    clean_goods_item,
    check_member_authority,
    generate_authority_url as pdd_gen_auth_url,
    generate_smart_promotion_url,
    make_custom_parameters,
    convert_pdd_url,
    get_goods_detail as pdd_get_detail,
    query_increment_orders,
    search_goods_by_sign_list,
)

logger = get_app_logger(__name__)

PDD_SERVICE_TAG_MAP = {
    1: "包邮",
    3: "退货包运费",
    4: "送货入户并安装",
    5: "送货入户",
    6: "电子发票",
    9: "坏果包赔",
    11: "闪电退款",
    12: "24小时发货",
    13: "48小时发货",
    17: "顺丰包邮",
    18: "只换不修",
    19: "全国联保",
    24: "极速退款",
    25: "品质险",
    26: "缺重包退",
}


class PddAdapter(BasePlatformAdapter):
    platform_code = "pdd"
    platform_name = "拼多多"
    requires_authorized_promotion = True
    recommendation_limit = 400
    batch_detail_includes_gallery = False

    def __init__(self, client_id=None, client_secret=None, pid=None, enabled=None, settings=None, **overrides):
        super().__init__()
        if settings is None and any(v is None for v in (client_id, client_secret, pid, enabled)):
            from config import settings as default_settings
            settings = default_settings
        self.active = bool(settings.pdd_enabled) if enabled is None else bool(enabled)
        self.client_id = settings.pdd_client_id if client_id is None else client_id
        self.client_secret = settings.pdd_client_secret if client_secret is None else client_secret
        self.pid = settings.pdd_pid if pid is None else pid
        self.hdk = overrides.get("hdk_client")
        if self.hdk is None and settings is not None:
            hdk_key = getattr(settings, "hdk_api_key", "")
            hdk_keys = getattr(settings, "hdk_api_key_list", ())
            if hdk_key or hdk_keys:
                self.hdk = HdkClient(hdk_key, api_key_list=hdk_keys)
        self._refresh_capabilities()

    def _has_official_creds(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _refresh_capabilities(self) -> None:
        cred_ok = self._has_official_creds()
        pid_ok = bool(cred_ok and self.pid)
        self.capabilities = {
            "recommend": bool(pid_ok or self._has_extra_providers("recommend")),
            "search": bool(pid_ok or self.hdk is not None or self._has_extra_providers("search")),
            "detail": bool(pid_ok or self._has_extra_providers("detail")),
            "promotion": bool(pid_ok or self._has_extra_providers("promotion")),
            "universal": bool(pid_ok or self._has_extra_providers("universal")),
            "authorization": bool(
                pid_ok
                or self._has_extra_providers("check_authority")
                or self._has_extra_providers("generate_authority_url")
            ),
            "orders": bool(cred_ok or self._has_extra_providers("orders")),
        }

    def _is_real_http(self, *, include_hdk: bool = False) -> bool:
        return is_real_requests_session(_pdd_client_mod._PDD_SESSION) or (
            include_hdk and is_real_requests_session(getattr(self.hdk, "http", None))
        )

    @staticmethod
    def _guard_provider_result(res: Any) -> Dict[str, Any]:
        if not isinstance(res, dict):
            return {"status": "skipped", "reason": "unconfigured_provider_mock"}
        return res

    @staticmethod
    def _is_valid_pdd_url(url: str) -> bool:
        return url_matches_domains(url, PDD_DOMAINS)

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None) -> ProductRef:
        raw = str(goods_id_or_sign or "").strip()
        if raw and not goods_id and not goods_sign:
            if raw.isdigit():
                goods_id = raw
            else:
                goods_sign = raw
        return ProductRef(self.platform_code, goods_id or "", goods_sign or "")

    def can_prefetch_detail(self, ref: ProductRef) -> bool:
        """后台只使用 Sign 直查，不触发 ID-only 洗链或搜索兜底。"""
        return bool(ref.goods_sign)

    def warmup(self) -> None:
        from infrastructure.pdd_client import warmup
        warmup()

    def normalize_order(self, order: dict) -> dict:
        result = dict(order)
        status = result.get("order_status")
        status_rules = {"is_refund": (4,), "is_settled": (3, 5)}
        for field, codes in status_rules.items():
            result[field] = bool(result[field]) if result.get(field) is not None else status in codes
        return result

    def _normalize_goods_summary(self, raw_cleaned: dict) -> dict:
        """摘要是详情的公共子集，金额/佣金只在平台边界转换一次。"""
        if not isinstance(raw_cleaned, dict):
            raise ValueError("missing or invalid public product content")
        name = str(raw_cleaned.get("goods_name") or "").strip()
        price = float(raw_cleaned.get("min_group_price_yuan") or 0.0)
        if not name or price < 0:
            raise ValueError("missing or invalid public product content")
        has_coupon = bool(raw_cleaned.get("has_coupon"))
        coupon = float(raw_cleaned.get("coupon_discount_yuan") or 0.0) if has_coupon else 0.0
        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": clean_product_id(raw_cleaned.get("goods_id")),
            "goods_sign": raw_cleaned.get("goods_sign") or "",
            "name": name,
            "image_url": raw_cleaned.get("goods_image_url") or raw_cleaned.get("goods_thumbnail_url") or "",
            "price": round(max(0.0, price - coupon), 2),
            "original_price": price,
            "coupon_amount": coupon,
            "has_coupon": has_coupon,
            "commission_rate": round(float(raw_cleaned.get("promotion_rate") or 0) / 10.0, 1),
            "commission_amount": float(raw_cleaned.get("estimated_commission_yuan") or 0.0),
            "sales_tip": str(raw_cleaned.get("sales_tip") or "0"),
        }
        for source, target in (("brand_name", "brand"), ("category_name", "category"), ("mall_name", "mall_name")):
            if raw_cleaned.get(source):
                data[target] = raw_cleaned[source]
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _normalize_goods(self, raw_cleaned: dict) -> dict:
        """商业承诺、评分与券库存只使用来源数据，缺失即留空。"""
        data = self._normalize_goods_summary(raw_cleaned)
        raw = raw_cleaned.get("_raw_data") or raw_cleaned
        gallery = raw.get("goods_gallery_urls") or [data["image_url"]]
        gallery = list(dict.fromkeys(u for u in gallery if isinstance(u, str) and u))
        tags = raw.get("unified_tags") or []
        ratings = {
            target: str(raw[source]) for source, target in
            (("desc_txt", "desc_score"), ("serv_txt", "serv_score"), ("lgst_txt", "lgst_score"))
            if raw.get(source) is not None
        }
        specs = [
            {"label": label, "value": str(raw_cleaned[key])}
            for key, label in (("brand_name", "商品品牌"), ("category_name", "商品类目"), ("mall_name", "店铺名称"))
            if raw_cleaned.get(key)
        ]
        coupon_info = {
            key: raw[key] for key in ("coupon_remain_quantity", "coupon_total_quantity", "coupon_start_time", "coupon_end_time")
            if key in raw
        }
        guarantees = list(raw.get("service_guarantees") or [])
        if not guarantees and isinstance(raw.get("service_tags"), list):
            for tag_id in raw["service_tags"]:
                try:
                    label = PDD_SERVICE_TAG_MAP.get(int(tag_id))
                except (TypeError, ValueError):
                    label = None
                if label and label not in guarantees:
                    guarantees.append(label)
        data.update({
            "desc": raw_cleaned.get("goods_desc") or "",
            "carousel_images": gallery,
            "detail_tags": tags,
            "service_guarantees": guarantees,
            "shop_ratings": ratings,
            "specs": specs,
            "coupon_info": coupon_info,
        })
        result = ProductDetail(**data).to_dict(exclude_unset=True)
        result.update({
            "brand_name": raw_cleaned.get("brand_name") or "",
            "category_name": raw_cleaned.get("category_name") or "",
            "search_id": raw_cleaned.get("search_id") or "",
            "unified_tags": tags,
        })
        return result

    def get_recommend_goods(self, channel_type=5, offset=0, limit=20, uid=None, detail_sink=None, **kwargs) -> Dict[str, Any]:
        """400 条推荐上限是拼多多规则；内部 detail_sink 不转发到供应商。"""
        unavailable = self._available("recommend")
        if unavailable:
            return {**unavailable, "data": []}
        if channel_type not in {channel["channel_type"] for channel in self.channels}:
            return {"status": "error", "error_code": "invalid_channel", "error_msg": "不支持的榜单", "data": []}
        if offset >= self.recommendation_limit:
            return {"status": "success", "total_count": self.recommendation_limit, "offset": offset,
                    "limit": limit, "has_more": False, "data": []}
        request_limit = min(50, max(1, int(limit or 20)), self.recommendation_limit - offset)

        def _recommend_via_pdd():
            call_kw = dict(kwargs)
            try:
                cursor = call_kw.pop("cursor", None)
                if cursor and "list_id" not in call_kw:
                    if isinstance(cursor, dict) and cursor.get("list_id"):
                        call_kw["list_id"] = str(cursor["list_id"]).strip()
                    elif isinstance(cursor, str) and cursor.strip():
                        call_kw["list_id"] = cursor.strip()
                params = {
                    "channel_type": channel_type, "offset": offset, "limit": request_limit,
                    "pid": self.pid, "custom_parameters": make_custom_parameters(uid=uid) if uid else None,
                    **call_kw,
                }
                try:
                    res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.recommend.get", params)
                except Exception as first_exc:
                    if uid and self._is_unauthorized_error(first_exc):
                        params["custom_parameters"] = None
                        res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.recommend.get", params)
                    else:
                        raise
                response = res.get("goods_basic_detail_response", {})
                raw_items = response.get("list", [])
                items = []
                for item in raw_items:
                    if not item:
                        continue
                    try:
                        items.append(self._normalize_goods_summary(clean_goods_item(item, source_type=f"pdd_recommend_{channel_type}")))
                    except (ValueError, TypeError):
                        continue
                total = response.get("total")
                has_total = isinstance(total, int) and not isinstance(total, bool) and total > 0
                effective_limit = min(self.recommendation_limit, total) if has_total else self.recommendation_limit
                has_more = (
                    bool(raw_items) and offset + request_limit < effective_limit
                    if has_total
                    else len(raw_items) >= request_limit and offset + request_limit < self.recommendation_limit
                )
                next_offset = offset + request_limit if (has_total and raw_items) else offset + len(raw_items)
                list_id = response.get("list_id", "") or call_kw.get("list_id", "")
                return {
                    "status": "success", "total_count": total if total is not None else len(items),
                    "offset": offset, "limit": limit, "next_offset": next_offset,
                    "list_id": list_id,
                    "next_cursor": list_id if (has_more and list_id) else None,
                    "has_more": has_more,
                    "data": items,
                }
            except Exception as exc:
                logger.debug(
                    "【拼多多接口异常】拉取推荐商品流异常，交由上层服务处理 | 频道[%s] 偏移[%s] 异常<%s>",
                    channel_type, offset, format_exc_summary(exc),
                )
                return {"status": "error", "error_msg": str(exc), "data": []}

        base = [("pdd_official_recommend", _recommend_via_pdd)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers(
            "recommend", base, channel_type=channel_type, offset=offset, limit=limit,
            uid=uid, detail_sink=detail_sink, **kwargs,
        )
        res = self._execute_capability("recommend", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def _normalize_hdk_pdd_summary(self, item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValueError("invalid hdk pdd item")
        raw_sign = str(item.get("goods_sign") or "").strip()
        if raw_sign.isdigit():
            raw_sign = ""
        raw_gid = clean_product_id(item.get("goods_id"))
        name = html_lib.unescape(str(item.get("goods_name") or item.get("goodsname") or "").strip())
        if not (raw_sign or raw_gid) or not name:
            raise ValueError("missing or invalid hdk pdd item")
        image_url = normalize_http_url(item.get("goods_image_url") or item.get("goods_thumbnail_url") or item.get("itempic"))
        orig_p = clean_money(item.get("min_group_price") or item.get("itemprice"), None)
        coupon = clean_money(item.get("coupon_discount") or item.get("couponmoney"), 0.0) or 0.0
        end_p = clean_money(item.get("end_price") or item.get("itemendprice"), None)
        if end_p is not None and end_p > 0:
            price = end_p
        elif orig_p is not None and orig_p >= 0:
            price = round(max(0.0, orig_p - coupon), 2)
        else:
            raise ValueError("missing or invalid hdk pdd price")
        orig = orig_p if (orig_p is not None and orig_p >= price) else price
        # 注意：好单库 v2/pdd_goods_search 返回的 promotion_rate 已为百分比（如 2.88 代表 2.88%），不可再除以 10
        rate = clean_money(item.get("promotion_rate") or item.get("tkrates") or item.get("commissionshare"), 0.0) or 0.0
        amt = clean_money(item.get("commission") or item.get("tkmoney"), None)
        if amt is None or amt <= 0:
            amt = round(price * rate / 100.0, 2) if (price > 0 and rate > 0) else 0.0
        elif rate <= 0 and amt > 0 and price > 0:
            rate = round(amt / price * 100.0, 2)
        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": raw_gid,
            "goods_sign": raw_sign,
            "name": name,
            "image_url": image_url,
            "price": round(max(0.0, price), 2),
            "original_price": round(max(orig, price), 2),
            "coupon_amount": round(max(0.0, coupon), 2),
            "has_coupon": coupon > 0,
            "commission_rate": round(max(0.0, rate), 2),
            "commission_amount": round(max(0.0, amt), 2),
            "sales_tip": str(item.get("sales_tip") or item.get("itemsale") or "0"),
        }
        if item.get("mall_name"):
            data["mall_name"] = html_lib.unescape(str(item["mall_name"]).strip())
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _search_via_pdd_official(self, keyword, page=1, page_size=20, uid=None, **kwargs) -> Dict[str, Any]:
        try:
            size = max(1, min(100, int(page_size or 20)))
            gateway_size = max(10, size)
            params = {"keyword": keyword, "pid": self.pid, "page": page, "page_size": gateway_size,
                      "custom_parameters": make_custom_parameters(uid=uid) if uid else None, **kwargs}
            try:
                res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
            except Exception as first_exc:
                if uid and self._is_unauthorized_error(first_exc):
                    params["custom_parameters"] = None
                    res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
                else:
                    raise
            response = res.get("goods_search_response", {})
            raw_items = response.get("goods_list", [])
            items = []
            raw_goods_signs = []
            for item in raw_items:
                if not item:
                    continue
                if isinstance(item, dict):
                    gs = str(item.get("goods_sign") or "").strip()
                    if gs:
                        raw_goods_signs.append(gs)
                try:
                    items.append(self._normalize_goods_summary(clean_goods_item(item, source_type="pdd_search")))
                except (ValueError, TypeError):
                    continue
            total = response.get("total_count")
            has_more = page * size < total if total is not None else (len(items) > size if gateway_size > size else len(raw_items) >= gateway_size)
            items = items[:size]
            return {
                "status": "success", "total_count": total if total is not None else len(items),
                "page": page, "page_size": size, "list_id": response.get("list_id", ""),
                "has_more": has_more,
                "data": items,
                "raw_goods_signs": raw_goods_signs,
            }
        except Exception as exc:
            logger.debug(
                "【拼多多接口异常】搜索联盟商品异常，交由上层服务处理 | 关键词[%s] 页码[%s] 异常<%s>",
                keyword, page, format_exc_summary(exc),
            )
            return {
                "status": "error",
                "error_code": str(getattr(exc, "error_code", "") or "").strip() or "UPSTREAM_ERROR",
                "error_msg": str(exc),
                "data": [],
            }

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("search")
        if unavailable:
            return {**unavailable, "data": []}

        def _search_via_pdd():
            return self._search_via_pdd_official(keyword, page=page, page_size=page_size, uid=uid, **kwargs)

        def _search_via_hdk():
            size = max(1, min(100, int(page_size or 20)))
            res = self._guard_provider_result(self.hdk.search_pdd_goods(keyword, page=page, page_size=size))
            if res.get("status") != "success":
                return res
            items = []
            for raw_it in res.get("data") or []:
                try:
                    items.append(self._normalize_hdk_pdd_summary(raw_it))
                except Exception:
                    continue
            return {
                "status": "success",
                "total_count": len(items),
                "page": page,
                "page_size": size,
                "has_more": bool(res.get("has_more")),
                "data": items[:size],
            }

        base = []
        if self._has_official_creds() and self.pid:
            base.append(("pdd_official_search", _search_via_pdd))
        if self.hdk is not None and hasattr(self.hdk, "search_pdd_goods"):
            base.append(("hdk_pdd_search", _search_via_hdk))
        providers = self._compose_providers(
            "search", base, keyword=keyword, page=page, page_size=page_size, uid=uid, **kwargs,
        )
        res = self._execute_capability("search", providers, allow_persist=self._is_real_http(include_hdk=True))
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def _normalize_and_verify_pdd_detail(self, raw_data: Dict[str, Any], ref: ProductRef) -> Dict[str, Any]:
        try:
            item = self._normalize_goods(raw_data)
        except (ValueError, TypeError):
            return _error("PRODUCT_OFF_PROMOTION", "该拼多多商品已下架或无推广计划")
        if ref.goods_sign and not item.get("goods_sign"):
            item["goods_sign"] = ref.goods_sign
        if not ref.goods_sign and ref.goods_id and not item.get("goods_id"):
            return _error("PRODUCT_OFF_PROMOTION", "该拼多多商品已下架或无推广计划")
        if ref.goods_id and item.get("goods_id") and item.get("goods_id") != ref.goods_id:
            return {"status": "error", "error_msg": "商品 ID 反查结果不匹配", "error_code": "product_identity_mismatch"}
        return {"status": "success", "data": item}

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("detail")
        if unavailable:
            return unavailable
        ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        detail_rate_limited = False

        def _detail_via_pdd():
            nonlocal detail_rate_limited
            try:
                def _call_det():
                    return pdd_get_detail(
                        client_id=self.client_id, client_secret=self.client_secret, pid=self.pid,
                        goods_sign=ref.goods_sign or None, goods_id=ref.goods_id or None, uid=uid, **kwargs,
                    )

                def _is_off(r):
                    return str(r.get("error_code") or "UPSTREAM_ERROR") == "PRODUCT_OFF_PROMOTION"

                if self._is_real_http():
                    res, confirmed_off = self._retry_on_off_promotion(_call_det, _is_off)
                else:
                    res = _call_det()
                    confirmed_off = _is_off(res)
                if res.get("status") != "success" or not res.get("data"):
                    code = str(res.get("error_code") or "UPSTREAM_ERROR")
                    if confirmed_off or code == "PRODUCT_OFF_PROMOTION":
                        return _error(
                            "PRODUCT_OFF_PROMOTION",
                            res.get("error_msg", "该拼多多商品已下架或无推广计划"),
                        )
                    if code in ("RATE_LIMIT", "QUOTA_PACE_BACKLOG"):
                        detail_rate_limited = True
                    return _error(code, res.get("error_msg", "未查询到官方商品详情"))
                return self._normalize_and_verify_pdd_detail(res["data"], ref)
            except Exception as exc:
                logger.debug(
                    "【拼多多接口异常】查询商品详情异常，交由上层服务处理 | 商品[%s] 异常<%s>",
                    goods_id_or_sign or goods_id or goods_sign, format_exc_summary(exc),
                )
                err_code = str(getattr(exc, "error_code", "") or "").strip() or "INTERNAL_ERROR"
                return {"status": "error", "error_code": err_code, "error_msg": str(exc)}

        def _detail_via_sign_search():
            if not (detail_rate_limited and ref.goods_sign):
                return {"status": "skipped", "reason": "not_rate_limited"}
            res = search_goods_by_sign_list(
                self.client_id, self.client_secret, self.pid, [ref.goods_sign], uid=uid,
            )
            if res.get("status") != "success":
                return res
            rows = res.get("data") or []
            matched = next(
                (
                    r for r in rows
                    if isinstance(r, dict)
                    and (
                        r.get("goods_sign") == ref.goods_sign
                        or (ref.goods_id and clean_product_id(r.get("goods_id")) == ref.goods_id)
                    )
                ),
                None,
            )
            if not matched:
                return _error("NOT_FOUND", "Sign 列表反查未命中目标商品")
            verified = self._normalize_and_verify_pdd_detail(matched, ref)
            if verified.get("status") == "success":
                verified["cacheable"] = False
            return verified

        base = []
        if self._has_official_creds() and self.pid:
            base.append(("pdd_official_detail", _detail_via_pdd))
            base.append(("pdd_goods_search_sign_detail", _detail_via_sign_search))
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
        拼多多 20 批量查卡：按每批 <=20 个 goods_sign 调用 pdd.ddk.goods.search(goods_sign_list)
        严格按 goods_sign / goods_id 精准对齐，未命中或无 sign 项独立回退 pdd.ddk.goods.detail（绝不连坐正常项）。
        注意：`pdd.ddk.goods.search` 不返回 `goods_gallery_urls`（`batch_detail_includes_gallery = False`），
        仅用于 20 批量实时价格/优惠/下架核验；完整详情页预取由 `ProductService` 回退单品 `get_goods_detail`。
        """
        outcomes: Dict[str, Dict[str, Any]] = {}
        ordered_refs: List[ProductRef] = []
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

        sign_refs = [r for r in ordered_refs if r.goods_sign]
        if self._has_official_creds() and self.pid:
            for i in range(0, len(sign_refs), 20):
                chunk = sign_refs[i:i + 20]
                res = search_goods_by_sign_list(
                    self.client_id,
                    self.client_secret,
                    self.pid,
                    [r.goods_sign for r in chunk],
                    uid=uid,
                )
                if res.get("status") != "success":
                    continue
                rows = res.get("data") or []
                for r in chunk:
                    matched = next(
                        (
                            row for row in rows
                            if isinstance(row, dict)
                            and (
                                row.get("goods_sign") == r.goods_sign
                                or (r.goods_id and clean_product_id(row.get("goods_id")) == r.goods_id)
                            )
                        ),
                        None,
                    )
                    if matched is None:
                        continue
                    verified = self._normalize_and_verify_pdd_detail(matched, r)
                    if verified.get("status") == "success":
                        outcomes[r.key] = {**verified, "_provider": "pdd_goods_search_sign_batch"}
                    elif verified.get("error_code") == "product_identity_mismatch":
                        outcomes[r.key] = verified

        for r in ordered_refs:
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

    def _resolve_goods_id(self, goods_id_or_sign: str, kwargs: dict) -> tuple:
        ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
        return ref.goods_id or None, ref.goods_sign or None

    def _is_unauthorized_error(self, err) -> bool:
        """识别拼多多网关抛出的推手未备案/未授权错误。"""
        if not err:
            return False
        if isinstance(err, dict):
            if str(err.get("error_code")) in ("60001", "AUTH_REQUIRED") or str(err.get("sub_code")) == "60001":
                return True
            if any(self._is_unauthorized_error(item) for item in (err.get("strategy_errors") or [])):
                return True
            msg = " ".join(str(err.get(key) or "") for key in ("error_msg", "sub_msg")).strip()
        else:
            if str(getattr(err, "sub_code", "") or "") == "60001" or str(getattr(err, "error_code", "") or "") == "60001":
                return True
            msg = str(err)
        # 仅确认推手/渠道授权错误；AUTH、凭据或网络等通用失败不能撤销用户授权。
        return bool(re.search(r"\[\s*(?:\d+\s*:\s*)?60001\s*[:\]]|\(\s*60001\s*\)", msg)
                    or "未传入已经授权备案过的相关参数" in msg
                    or re.search(r"(?:用户|推手|渠道).{0,12}(?:未授权|未备案)", msg)
                    or re.search(r"(?:^|[:：]\s*)(?:未授权|未备案|请授权)\s*$", msg))

    def _attempt_promotions(self, target, source_url, uid=None, **options) -> tuple:
        """
        每个有效 UID 只尝试一套策略，由 service 决定是否另走通用路径：
        1. 智能生链（内部已包含超级红包 → 单品标准推广，遇 PRODUCT_OFF_PROMOTION 立即短路）
        2. 必要时官方 URL 洗链 (zs.unit.url.gen)
        返回: (link, raw, link_type, is_unauthorized) 或 (None, {"_off_promotion": True}, None, False)
        """
        is_unauthorized = False

        # 1. 智能生链内部已做红包 → 标准降级，外层不得重复标准请求。
        if target:
            try:
                res = generate_smart_promotion_url(
                    client_id=self.client_id, client_secret=self.client_secret,
                    pid=self.pid, goods_sign=target, uid=uid, **options
                )
                if isinstance(res, dict) and res.get("status") == "success":
                    raw = res.get("data") or {}
                    link = raw.get("mobile_short_url") or raw.get("short_url") or raw.get("mobile_url") or raw.get("url")
                    if link:
                        return link, raw, res.get("link_type", "官方超级红包"), False
                elif isinstance(res, dict) and res.get("error_code") == "PRODUCT_OFF_PROMOTION":
                    return None, {"_off_promotion": True}, None, False
                elif self._is_unauthorized_error(res):
                    is_unauthorized = True
            except Exception as exc:
                if self._is_unauthorized_error(exc):
                    is_unauthorized = True
                logger.warning(
                    "【拼多多转链降级】超级红包智能生链异常，继续尝试招商券洗链或通用兜底 | 目标[%s] 用户[%s] 异常<%s> 排查方向<请检查推手是否完成渠道备案(60001)或商品是否支持推广>",
                    target[:16] + "..." if isinstance(target, str) and len(target) > 16 else target,
                    uid or "匿名",
                    format_exc_summary(exc),
                )

        # 2. 官方招商券转链 / 洗链 (zs.unit.url.gen)
        if source_url and self._is_valid_pdd_url(source_url):
            try:
                res = convert_pdd_url(
                    client_id=self.client_id, client_secret=self.client_secret, pid=self.pid,
                    source_url=source_url, uid=uid
                )
                if isinstance(res, dict) and res.get("status") == "success" and res.get("jump_url"):
                    raw = res.get("raw_data") or {}
                    return res["jump_url"], raw, "官方招商券推广", False
                elif self._is_unauthorized_error(res):
                    is_unauthorized = True
            except Exception as exc:
                if self._is_unauthorized_error(exc):
                    is_unauthorized = True
                logger.warning(
                    "【拼多多转链降级】招商券洗链异常，将回退保底策略 | 用户[%s] 异常<%s> 排查方向<请检查商品链接有效性或PID权限>",
                    uid or "匿名", format_exc_summary(exc),
                )

        return None, None, None, is_unauthorized

    def _promotion_result(self, link, raw, title, price, coupon, link_type, is_fallback=False, need_auth=False) -> dict:
        """所有生链路径返回同一契约，降级标记不能在某个分支丢失。"""
        result = {
            "status": "success", "need_auth": bool(need_auth), "is_fallback": is_fallback, "link_type": link_type,
            "final_link": link, "url": link, "short_url": link, "mobile_url": link, "mobile_short_url": link,
            "schema_url": raw.get("schema_url") or "",
            "command_text": self.build_command_text(title, link, price, coupon),
            "we_app_info": raw.get("we_app_info") or {}, "raw": raw,
        }
        if is_fallback:
            result["cacheable"] = False
        if need_auth:
            result["auth_platform"] = "pdd"
            result["auth_msg"] = "检测到您尚未在拼多多完成渠道授权备案，当前链接为官方通用链接，无法计入本人推广佣金"
        return result

    def convert_promotion_url(self, goods_id_or_sign, uid=None, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("promotion", need_auth=False, is_fallback=False)
        if unavailable:
            return unavailable
        goods_id, goods_sign = self._resolve_goods_id(goods_id_or_sign, kwargs)
        title = kwargs.get("title") or kwargs.get("name") or kwargs.get("goods_name") or "精选高性价比好物"
        price = kwargs.get("price")
        coupon = kwargs.get("coupon_amount") if kwargs.get("coupon_amount") is not None else kwargs.get("coupon")
        product_url = kwargs.get("product_url")
        options = {k: v for k, v in kwargs.items() if k not in
                   ("goods_id", "goods_sign", "product_url", "title", "name", "goods_name",
                    "price", "coupon", "coupon_amount", "identity", "allow_fallback",
                    "trusted_product_url", "promoter_id", "coupon_url")}
        # 仅真实 goods_sign 可进入智能红包/标准生链；纯数字 goods_id 绝不能喂给 goods_sign（否则网关会返回无关通用红包页造成串货）
        target = goods_sign if (goods_sign and not str(goods_sign).isdigit()) else None
        # 商品身份优先：客户端 URL 未经验证，不能把另一件商品洗链后写入当前商品缓存。
        source_url = product_url if not goods_id and not goods_sign else None
        if goods_id and str(goods_id).isdigit():
            source_url = "https://mobile.pinduoduo.com/goods.html?" + urlencode({"goods_id": goods_id})

        if not target and not source_url:
            return {"status": "error", "error_msg": "缺少商品 ID、Sign 或商品链接"}
        if not target and not self._is_valid_pdd_url(source_url):
            return {"status": "error", "error_code": "invalid_product_url", "need_auth": False,
                    "error_msg": "仅支持拼多多官方 HTTP(S) 商品链接"}

        clean_uid = uid.strip() if isinstance(uid, str) and uid.strip() else None

        def _promote_via_pdd():
            # 仅使用编排层给出的有效 UID；个人失败不在 adapter 内匿名重试，避免污染个人缓存。
            link, raw, link_type, is_unauthorized = self._attempt_promotions(
                target, source_url, uid=clean_uid, **options
            )
            if link:
                return self._promotion_result(link, raw, title, price, coupon, link_type)
            if isinstance(raw, dict) and raw.get("_off_promotion"):
                return _error("PRODUCT_OFF_PROMOTION", "该拼多多商品已下架或无推广计划")
            if clean_uid:
                result = {
                    "status": "error", "need_auth": is_unauthorized,
                    "error_code": "AUTH_REQUIRED" if is_unauthorized else "PERSONAL_PROMOTION_UNAVAILABLE",
                    "error_msg": "拼多多渠道授权未完成或已失效" if is_unauthorized else "暂未能生成拼多多个人推广链接",
                }
                if is_unauthorized:
                    result["auth_platform"] = "pdd"
                    result["auth_msg"] = "请到我的页重新校验拼多多渠道授权备案"
                return result
            return {"status": "error", "error_code": "PROMOTION_FAILED", "error_msg": "暂未能生成拼多多推广链接"}

        base = [("pdd_official_promote", _promote_via_pdd)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers(
            "promotion",
            base,
            goods_id_or_sign=goods_id_or_sign,
            uid=clean_uid,
            **kwargs,
        )
        res = self._execute_capability("promotion", providers, allow_persist=self._is_real_http())
        if res.get("status") == "success":
            return res
        if (
            clean_uid
            or not (source_url or target)
            or res.get("error_code") in (
                "INVALID_ARGUMENT", "invalid_product_url", "platform_unavailable",
                "CONFIG_ERROR", "AUTH_REQUIRED", "PRODUCT_OFF_PROMOTION",
            )
        ):
            return res

        # 匿名多供应商链均未返回推广链接时才使用原始商品直链；此保底不可缓存。
        final_direct_url = source_url or ("https://mobile.yangkeduo.com/goods.html?" + urlencode({"goods_sign": target}))
        return self._promotion_result(
            final_direct_url, {}, title, price, coupon, "商品直连保底", is_fallback=True,
        )

    def resolve_product_from_link(self, raw_text: str) -> Dict[str, Any]:
        """拼多多独立底层能力：校验官方域名并从原始分享短链/长链/goods_sign 解析商品身份与详情卡（严禁模糊搜索或 40009 洗链绕过）。"""
        text = str(raw_text or "").strip()
        if not text:
            return {"status": "error", "error_code": "INVALID_ARGUMENT", "error_msg": "请输入拼多多分享链接、推广短链或 goods_sign"}

        extracted_sign = ""
        extracted_url = ""
        sign_param_match = re.search(r"goods_sign=([A-Za-z0-9_-]{20,})", text)
        if sign_param_match:
            extracted_sign = sign_param_match.group(1)
        elif PDD_GOODS_SIGN_RE.match(text):
            extracted_sign = text

        url_match = URL_RE.search(text)
        if url_match:
            extracted_url = url_match.group(0).rstrip(".,!?)];'\"")
            if not self._is_valid_pdd_url(extracted_url):
                return {
                    "status": "error",
                    "error_code": "INVALID_PDD_URL",
                    "error_msg": "该链接不属于拼多多平台，请切换对应平台或粘贴拼多多分享链接",
                }

        if not extracted_sign and not extracted_url:
            return {
                "status": "error",
                "error_code": "UNRESOLVED_PDD_INPUT",
                "error_msg": "未能识别出拼多多商品链接，请粘贴拼多多分享短链或长链；若只有商品名称请前往「首页」搜索",
            }

        product_detail = None
        id_val = ""
        sign_val = extracted_sign
        if sign_val:
            det_res = self.get_goods_detail(goods_sign=sign_val, uid=None)
            if det_res.get("status") == "success" and isinstance(det_res.get("data"), dict):
                product_detail = det_res["data"]
                id_val = product_detail.get("goods_id") or ""
            elif det_res.get("error_code") == "PRODUCT_OFF_PROMOTION":
                return det_res
            elif not extracted_url and det_res.get("error_code") in ("RATE_LIMIT", "QUOTA_PACE_BACKLOG", "NETWORK_ERROR", "TIMEOUT"):
                return det_res

        if not product_detail and extracted_url:
            if not (self._has_official_creds() and self.pid):
                return self._available("universal") or _error("CONFIG_ERROR", "拼多多官方接口配置不完整")
            search_res = self._search_via_pdd_official(extracted_url, page=1, page_size=10, uid=None)
            if search_res.get("status") != "success":
                err_msg = str(search_res.get("error_msg") or "")
                if "40009" in err_msg or "原始链接" in err_msg:
                    return {
                        "status": "error",
                        "error_code": "UNSUPPORTED_PDD_ID_ONLY_URL",
                        "error_msg": "拼多多官方接口 (40009) 已禁止仅含纯数字 goods_id 的非原始链接查询，请粘贴拼多多 App 分享链接或推广短链",
                    }
                return {
                    "status": "error",
                    "error_code": "UNRESOLVED_PDD_INPUT",
                    "error_msg": "未能解析该拼多多链接，请确认链接有效且商品在推广中",
                    "extracted_url": extracted_url,
                }
            hits = search_res.get("data") or []
            total_cnt = int(search_res.get("total_count") or 0)
            raw_signs = [s for s in (search_res.get("raw_goods_signs") or []) if isinstance(s, str) and s.strip()]
            if not hits and total_cnt == 1 and len(raw_signs) == 1:
                det_res = self.get_goods_detail(goods_sign=raw_signs[0], uid=None)
                if det_res.get("status") == "success" and isinstance(det_res.get("data"), dict):
                    product_detail = det_res["data"]
                    sign_val = product_detail.get("goods_sign") or raw_signs[0]
                    id_val = product_detail.get("goods_id") or id_val
                else:
                    return det_res if det_res.get("status") == "error" else {
                        "status": "error",
                        "error_code": "PRODUCT_OFF_PROMOTION",
                        "error_msg": "该拼多多商品已下架或无推广计划",
                        "extracted_url": extracted_url,
                    }
            else:
                if len(hits) != 1 or total_cnt != 1:
                    return {
                        "status": "error",
                        "error_code": "UNRESOLVED_PDD_INPUT",
                        "error_msg": "该链接未能唯一定位到具体拼多多商品，请确认粘贴的是单品分享链接",
                        "is_ambiguous": bool(len(hits) > 1 or total_cnt > 1),
                        "extracted_url": extracted_url,
                    }
                first_hit = hits[0]
                sign_val = first_hit.get("goods_sign") or sign_val
                id_val = first_hit.get("goods_id") or id_val
                product_detail = first_hit
                if sign_val:
                    det_res = self.get_goods_detail(goods_sign=sign_val, goods_id=id_val or None, uid=None)
                    if det_res.get("status") == "success" and isinstance(det_res.get("data"), dict):
                        product_detail = det_res["data"]
                        id_val = product_detail.get("goods_id") or id_val
                    elif det_res.get("error_code") == "PRODUCT_OFF_PROMOTION":
                        return det_res

        return {
            "status": "success",
            "goods_id": id_val,
            "goods_sign": sign_val,
            "extracted_url": extracted_url,
            "product": product_detail,
        }

    def resolve_and_convert_universal(self, raw_text: str, uid=None, **kwargs) -> Dict[str, Any]:
        """
        拼多多万能转链与详情反查：
        1. 复用 `resolve_product_from_link` 进行严格域名核验与唯一单品反查（绝不绕过 40009 或多结果拦截）；
        2. 合法原链接（如 p.pinduoduo.com 推广短链）若搜索未收录且非 40009/非多结果冲突，自动回退 `zs.unit.url.gen` 洗链（标为 `degraded_product`）。
        """
        def _do_pdd_universal():
            resolved = self.resolve_product_from_link(raw_text)
            if resolved.get("status") != "success":
                err_code = resolved.get("error_code")
                ext_url = resolved.get("extracted_url") or ""
                is_id_only_url = bool(ext_url and re.search(r"[?&]goods_id=\d+", ext_url, re.IGNORECASE))
                # 仅对明确非 40009、非多结果（如 p.pinduoduo.com 活动/分享短链）在 searches 返回空结果或非40009错误时允许 zs.unit 洗链降级
                if (
                    err_code == "UNRESOLVED_PDD_INPUT"
                    and ext_url
                    and not is_id_only_url
                    and not resolved.get("is_ambiguous")
                    and self._has_official_creds()
                    and self.pid
                ):
                    zs_res = convert_pdd_url(self.client_id, self.client_secret, pid=self.pid, source_url=ext_url, uid=uid)
                    if isinstance(zs_res, dict) and zs_res.get("status") == "success" and zs_res.get("jump_url"):
                        payload = self._promotion_result(
                            zs_res["jump_url"],
                            zs_res.get("raw_data") or {},
                            "拼多多精选推广链接",
                            None,
                            None,
                            "拼多多官方洗链",
                            need_auth=False,
                        )
                        payload["detail_lookup_failed"] = True
                        return stamp_universal_resolution(payload, verified_coupon_or_venue=False)
                    if self._is_unauthorized_error(zs_res):
                        return _error("AUTH_REQUIRED", "拼多多渠道授权未完成或已失效", need_auth=True, auth_platform="pdd")
                resolved.pop("is_ambiguous", None)
                resolved.pop("extracted_url", None)
                return resolved

            sign_val = resolved.get("goods_sign") or ""
            id_val = resolved.get("goods_id") or ""
            extracted_url = resolved.get("extracted_url") or ""
            product_detail = resolved.get("product")
            if not product_detail or not sign_val:
                return {
                    "status": "error",
                    "error_code": "UNRESOLVED_PDD_INPUT",
                    "error_msg": "未能获取到对应的拼多多商品详情，请确认链接或标识有效后重试",
                }

            title = product_detail.get("name") or "拼多多精选好物"
            price = product_detail.get("price")
            coupon = product_detail.get("coupon_amount")
            conv_res = self.convert_promotion_url(
                goods_id_or_sign=sign_val or id_val,
                uid=uid,
                goods_id=id_val or None,
                goods_sign=sign_val or None,
                product_url=extracted_url or None,
                title=title,
                price=price,
                coupon_amount=coupon,
            )
            if conv_res.get("status") != "success":
                conv_res["product"] = product_detail
                return stamp_universal_resolution(conv_res)
            if conv_res.get("is_fallback"):
                err_res = {
                    "status": "error",
                    "error_code": "PROMOTION_FAILED",
                    "error_msg": "该拼多多商品暂不支持生成推广链接",
                    "product": product_detail,
                }
                return stamp_universal_resolution(err_res)

            conv_res["goods_id"] = id_val
            conv_res["goods_sign"] = sign_val
            conv_res["title"] = title
            conv_res["price"] = price
            conv_res["coupon_amount"] = coupon
            conv_res["product"] = product_detail
            return stamp_universal_resolution(conv_res)

        unavailable = self._available("universal")
        if unavailable:
            return unavailable
        base = [("pdd_router_universal", _do_pdd_universal)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers("universal", base, raw_text=str(raw_text or "").strip(), uid=uid, **kwargs)
        return self._execute_capability(
            "universal",
            providers,
            allow_persist=self._is_real_http(),
            metric_operation="resolve_universal",
        )

    def check_authority(self, uid) -> Dict[str, Any]:
        unavailable = self._available("authorization", is_authorized=False)
        if unavailable:
            return unavailable

        def _check_via_pdd():
            try:
                return check_member_authority(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid, uid=uid)
            except Exception as exc:
                logger.debug("【拼多多接口异常】核验备案状态异常，交由上层服务处理 | 用户[%s] 异常<%s>", uid, format_exc_summary(exc))
                return {"status": "error", "is_authorized": False, "bind": 0, "error_msg": str(exc)}

        base = [("pdd_official_auth", _check_via_pdd)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers("check_authority", base, uid=uid)
        return self._execute_capability("check_authority", providers, allow_persist=self._is_real_http())

    def generate_authority_url(self, uid, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("authorization", is_authorized=False)
        if unavailable:
            return unavailable

        def _gen_via_pdd():
            try:
                res = pdd_gen_auth_url(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid, uid=uid, **kwargs)
                info = res.get("url_info") or {}
                link = info.get("mobile_short_url") or info.get("mobile_url") or info.get("short_url") or info.get("url")
                if res.get("status") == "success" and link:
                    return {"status": "success", "auth_url": link, "short_url": link, "we_app_info": info.get("we_app_info", {})}
                return {"status": "error", "error_msg": res.get("error_msg", "生成授权链接失败")}
            except Exception as exc:
                logger.debug("【拼多多接口异常】生成备案引导链接异常，交由上层服务处理 | 用户[%s] 异常<%s>", uid, format_exc_summary(exc))
                return {"status": "error", "error_msg": str(exc)}

        base = [("pdd_official_auth_url", _gen_via_pdd)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers("generate_authority_url", base, uid=uid, **kwargs)
        return self._execute_capability("generate_authority_url", providers, allow_persist=self._is_real_http())

    def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("orders")
        if unavailable:
            return {**unavailable, "order_list": [], "total_count": 0}

        def _orders_via_pdd():
            try:
                res = query_increment_orders(client_id=self.client_id, client_secret=self.client_secret,
                                             start_time=start_time, end_time=end_time, page=page, page_size=page_size, **kwargs)
                if not isinstance(res, dict) or res.get("status") != "success":
                    message = res.get("error_msg", "增量订单查询失败") if isinstance(res, dict) else "增量订单响应无效"
                    return {"status": "error", "error_msg": message, "order_list": [], "total_count": 0}
                orders = [self.normalize_order(order) for order in res.get("order_list", [])]
                total = res.get("total_count", len(orders))
                return {"status": "success", "order_list": orders, "total_count": total,
                        "has_more": page * page_size < total}
            except Exception as exc:
                logger.debug("【拼多多接口异常】拉取增量订单异常，交由上层服务处理 | 页码[%s] 异常<%s>", page, format_exc_summary(exc))
                return {"status": "error", "error_msg": str(exc), "order_list": [], "total_count": 0}

        base = [("pdd_official_orders", _orders_via_pdd)] if self._has_official_creds() else []
        providers = self._compose_providers(
            "orders", base, start_time=start_time, end_time=end_time, page=page, page_size=page_size, **kwargs,
        )
        result = self._execute_capability("orders", providers, allow_persist=self._is_real_http())
        if result.get("status") != "success":
            result.setdefault("order_list", [])
            result.setdefault("total_count", 0)
        return result
