# -*- coding: utf-8 -*-
"""拼多多边界：SDK 调用、原始字段转换、商品标识与推广降级策略。"""
import re
from typing import Dict, Any
from urllib.parse import parse_qs, urlencode, urlsplit
from adapters.base import BasePlatformAdapter
from infrastructure.api_governance import (
    is_real_requests_session,
    stamp_universal_resolution,
)
from infrastructure.logging_utils import format_exc_summary
from models.product import ProductRef, ProductSummary, ProductDetail, clean_product_id, extract_search_keyword
from config import PDD_CLIENT_ID, PDD_CLIENT_SECRET, PDD_PID, logger, settings
from infrastructure.pdd_client import (
    _PDD_SESSION,
    call_pdd_api,
    clean_goods_item,
    check_member_authority,
    generate_authority_url as pdd_gen_auth_url,
    generate_smart_promotion_url,
    make_custom_parameters,
    convert_pdd_url,
    get_goods_detail as pdd_get_detail,
    query_increment_orders,
)


class PddAdapter(BasePlatformAdapter):
    platform_code = "pdd"
    platform_name = "拼多多"
    requires_authorized_promotion = True
    recommendation_limit = 400

    def __init__(self, client_id=PDD_CLIENT_ID, client_secret=PDD_CLIENT_SECRET, pid=PDD_PID, enabled=None):
        self._ensure_provider_state()
        self.active = settings.pdd_enabled if enabled is None else bool(enabled)
        self.client_id = client_id
        self.client_secret = client_secret
        self.pid = pid
        self._refresh_capabilities()

    def _has_official_creds(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _refresh_capabilities(self) -> None:
        cred_ok = self._has_official_creds()
        pid_ok = bool(cred_ok and self.pid)
        self.capabilities = {
            "recommend": bool(pid_ok or self._has_extra_providers("recommend")),
            "search": bool(pid_ok or self._has_extra_providers("search")),
            "detail": bool(pid_ok or self._has_extra_providers("detail")),
            "promotion": bool(pid_ok or self._has_extra_providers("promotion")),
            "authorization": bool(
                pid_ok
                or self._has_extra_providers("check_authority")
                or self._has_extra_providers("generate_authority_url")
            ),
            "orders": bool(cred_ok or self._has_extra_providers("orders")),
        }

    def _is_real_http(self) -> bool:
        return is_real_requests_session(_PDD_SESSION)

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
        price = float(raw_cleaned.get("min_group_price_yuan") or 0.0)
        has_coupon = bool(raw_cleaned.get("has_coupon"))
        coupon = float(raw_cleaned.get("coupon_discount_yuan") or 0.0) if has_coupon else 0.0
        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": clean_product_id(raw_cleaned.get("goods_id")),
            "goods_sign": raw_cleaned.get("goods_sign") or "",
            "name": raw_cleaned.get("goods_name") or "",
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
        data.update({
            "desc": raw_cleaned.get("goods_desc") or "",
            "carousel_images": gallery,
            "detail_tags": tags,
            "service_guarantees": raw.get("service_guarantees") or [],
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
        request_limit = min(limit, self.recommendation_limit - offset)

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
                res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.recommend.get", params)
                response = res.get("goods_basic_detail_response", {})
                raw_items = response.get("list", [])
                items = [self._normalize_goods_summary(clean_goods_item(item, source_type=f"pdd_recommend_{channel_type}"))
                         for item in raw_items if item]
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

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("search")
        if unavailable:
            return {**unavailable, "data": []}

        def _search_via_pdd():
            try:
                size = max(1, min(100, int(page_size or 20)))
                gateway_size = size
                params = {"keyword": keyword, "pid": self.pid, "page": page, "page_size": gateway_size,
                          "custom_parameters": make_custom_parameters(uid=uid) if uid else None, **kwargs}
                try:
                    res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
                except Exception as first_exc:
                    # 【对齐 test_pdd_api.py 实证基准】：
                    # 若当前登录用户尚未完成拼多多推手备案 (触发 60001 未传入已经授权备案过的相关参数)，
                    # 公共商品搜索自动回退为不传 custom_parameters（站长已备案 PID 级）重试，避免阻断用户搜索。
                    if size < 10 and ("pageSize" in str(first_exc) or "3000000" in str(first_exc)):
                        gateway_size = 10
                        params["page_size"] = 10
                        try:
                            res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
                        except Exception as retry_exc:
                            if uid and self._is_unauthorized_error(retry_exc):
                                params["custom_parameters"] = None
                                res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
                            else:
                                raise
                    elif uid and self._is_unauthorized_error(first_exc):
                        params["custom_parameters"] = None
                        res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
                    else:
                        raise
                response = res.get("goods_search_response", {})
                raw_items = response.get("goods_list", [])
                items = [self._normalize_goods_summary(clean_goods_item(item, source_type="pdd_search"))
                         for item in raw_items if item]
                total = response.get("total_count")
                has_more = page * size < total if total is not None else (len(items) > size if gateway_size > size else len(raw_items) >= gateway_size)
                items = items[:size]
                return {
                    "status": "success", "total_count": total if total is not None else len(items),
                    "page": page, "page_size": size, "list_id": response.get("list_id", ""),
                    "has_more": has_more,
                    "data": items,
                }
            except Exception as exc:
                logger.debug(
                    "【拼多多接口异常】搜索联盟商品异常，交由上层服务处理 | 关键词[%s] 页码[%s] 异常<%s>",
                    keyword, page, format_exc_summary(exc),
                )
                return {"status": "error", "error_msg": str(exc), "data": []}

        base = [("pdd_official_search", _search_via_pdd)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers(
            "search", base, keyword=keyword, page=page, page_size=page_size, uid=uid, **kwargs,
        )
        res = self._execute_capability("search", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs) -> Dict[str, Any]:
        unavailable = self._available("detail")
        if unavailable:
            return unavailable
        ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)

        def _detail_via_pdd():
            try:
                res = pdd_get_detail(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid,
                                     goods_sign=ref.goods_sign or None, goods_id=ref.goods_id or None, uid=uid, **kwargs)
                if res.get("status") != "success" or not res.get("data"):
                    code = str(res.get("error_code") or "UPSTREAM_ERROR")
                    if code == "PRODUCT_OFF_PROMOTION":
                        import time
                        time.sleep(0.15)
                        retry_res = pdd_get_detail(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid,
                                                   goods_sign=ref.goods_sign or None, goods_id=ref.goods_id or None, uid=uid, **kwargs)
                        if retry_res.get("status") == "success" and retry_res.get("data"):
                            res = retry_res
                        else:
                            return {
                                "status": "error",
                                "error_code": "PRODUCT_OFF_PROMOTION",
                                "error_msg": res.get("error_msg", "该拼多多商品已下架或无推广计划"),
                            }
                    else:
                        return {
                            "status": "error",
                            "error_code": code,
                            "error_msg": res.get("error_msg", "未查询到官方商品详情"),
                        }
                item = self._normalize_goods(res["data"])
                if ref.goods_sign and not item.get("goods_sign"):
                    item["goods_sign"] = ref.goods_sign
                if not ref.goods_sign and ref.goods_id and item.get("goods_id") not in ("", ref.goods_id):
                    return {"status": "error", "error_msg": "商品 ID 反查结果不匹配", "error_code": "product_identity_mismatch"}
                return {"status": "success", "data": item}
            except Exception as exc:
                logger.debug(
                    "【拼多多接口异常】查询商品详情异常，交由上层服务处理 | 商品[%s] 异常<%s>",
                    goods_id_or_sign or goods_id or goods_sign, format_exc_summary(exc),
                )
                err_code = str(getattr(exc, "error_code", "") or "").strip() or "INTERNAL_ERROR"
                return {"status": "error", "error_code": err_code, "error_msg": str(exc)}

        base = [("pdd_official_detail", _detail_via_pdd)] if (self._has_official_creds() and self.pid) else []
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
            msg = str(err)
        # 仅确认推手/渠道授权错误；AUTH、凭据或网络等通用失败不能撤销用户授权。
        return bool(re.search(r"\[\s*60001\s*[:\]]|\(\s*60001\s*\)", msg)
                    or "未传入已经授权备案过的相关参数" in msg
                    or re.search(r"(?:用户|推手|渠道).{0,12}(?:未授权|未备案)", msg)
                    or re.search(r"(?:^|[:：]\s*)(?:未授权|未备案|请授权)\s*$", msg))

    def _attempt_promotions(self, target, source_url, uid=None, **options) -> tuple:
        """
        每个有效 UID 只尝试一套策略，由 service 决定是否另走通用路径：
        1. 智能生链（内部已包含超级红包 → 单品标准推广）
        2. 必要时官方 URL 洗链 (zs.unit.url.gen)
        返回: (link, raw, link_type, is_unauthorized)
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
        if source_url:
            parsed = urlsplit(source_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme in ("https", "http") and any(
                    host == root or host.endswith("." + root) for root in ("pinduoduo.com", "yangkeduo.com")):
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
        from cache import is_product_off_promotion
        if is_product_off_promotion("pdd", goods_id, goods_sign):
            return {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "该拼多多商品已下架或无推广计划"}
        title = kwargs.get("title") or kwargs.get("name") or kwargs.get("goods_name") or "精选高性价比好物"
        price = kwargs.get("price")
        coupon = kwargs.get("coupon_amount") if kwargs.get("coupon_amount") is not None else kwargs.get("coupon")
        product_url = kwargs.get("product_url")
        options = {k: v for k, v in kwargs.items() if k not in
                   ("goods_id", "goods_sign", "product_url", "title", "name", "goods_name", "price", "coupon", "coupon_amount", "identity")}
        target = goods_sign or goods_id
        # 商品身份优先：客户端 URL 未经验证，不能把另一件商品洗链后写入当前商品缓存。
        source_url = product_url if not goods_id and not goods_sign else None
        if goods_id and str(goods_id).isdigit():
            source_url = "https://mobile.pinduoduo.com/goods.html?" + urlencode({"goods_id": goods_id})

        if not target and not source_url:
            return {"status": "error", "error_msg": "缺少商品 ID、Sign 或商品链接"}
        if not target:
            try:
                parsed = urlsplit(source_url)
                host = (parsed.hostname or "").lower()
                valid_source = parsed.scheme in ("https", "http") and any(
                    host == root or host.endswith("." + root) for root in ("pinduoduo.com", "yangkeduo.com"))
            except (TypeError, ValueError, AttributeError):
                valid_source = False
            if not valid_source:
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
        if clean_uid or res.get("error_code") in ("INVALID_ARGUMENT", "invalid_product_url", "platform_unavailable", "CONFIG_ERROR", "AUTH_REQUIRED"):
            return res

        # 匿名多供应商链均未返回推广链接时才使用原始商品直链；此保底不可缓存。
        final_direct_url = source_url or ("https://mobile.yangkeduo.com/goods.html?" + urlencode({"goods_sign": goods_sign}))
        return self._promotion_result(
            final_direct_url, {}, title, price, coupon, "商品直连保底", is_fallback=True,
        )

    def resolve_and_convert_universal(self, raw_text: str, uid=None, **kwargs) -> Dict[str, Any]:
        """
        拼多多万能转链与详情反查（对齐 scripts/test_pdd_api.py 基准规范 八）：
        1. 宁可报错也绝不返回与用户预期不符的商品（严禁无链接纯文本模糊搜索，链接搜索强制校验唯一单品命中 total_count == 1）；
        2. 流程极简：不设针对 goods.html / p.pinduoduo.com 的脆弱前置规则，亦不做多余的 zs.unit 预解包，直接将拼多多链接透传网关。
        """
        if not self.active:
            return {"status": "error", "error_code": "platform_unavailable", "error_msg": "拼多多暂未启用"}
        text = str(raw_text or "").strip()
        if not text:
            return {"status": "error", "error_code": "INVALID_ARGUMENT", "error_msg": "请输入拼多多分享链接、推广短链或 goods_sign"}

        extracted_sign = ""
        extracted_id = ""
        extracted_url = ""

        # 1. 直接匹配 goods_sign 参数或裸加密 goods_sign
        sign_param_match = re.search(r"goods_sign=([A-Za-z0-9_-]{20,})", text)
        if sign_param_match:
            extracted_sign = sign_param_match.group(1)
        elif re.match(r"^[EYc][A-Za-z0-9_-]{25,}$", text):
            extracted_sign = text

        # 2. 提取纯净 ASCII URL，防止混排文案中 `]` 或中文尾缀污染链接
        url_match = re.search(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", text)
        if url_match:
            extracted_url = url_match.group(0).rstrip(".,!?)];'\"")
            url_lower = extracted_url.lower()
            if not any(d in url_lower for d in ("pinduoduo.com", "yangkeduo.com")):
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

        def _do_pdd_universal():
            sign_val = extracted_sign
            id_val = extracted_id
            product_detail = None

            # 3. 若已有 goods_sign，优先直查商品详情
            if sign_val:
                det_res = self.get_goods_detail(goods_sign=sign_val, uid=None)
                if det_res.get("status") == "success" and isinstance(det_res.get("data"), dict):
                    product_detail = det_res["data"]
                    id_val = product_detail.get("goods_id") or id_val

            # 4. 若尚未拿到详情且存在拼多多链接，直接将链接透传给 search_goods 精准解析唯一单品
            if not product_detail and extracted_url:
                search_res = self.search_goods(extracted_url, page=1, page_size=10, uid=None)
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
                    }

                hits = search_res.get("data") or []
                if len(hits) != 1 or int(search_res.get("total_count") or 0) != 1:
                    return {
                        "status": "error",
                        "error_code": "UNRESOLVED_PDD_INPUT",
                        "error_msg": "该链接未能唯一定位到具体拼多多商品，请确认粘贴的是单品分享链接",
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

            if not product_detail or not sign_val:
                return {
                    "status": "error",
                    "error_code": "UNRESOLVED_PDD_INPUT",
                    "error_msg": "未能获取到对应的拼多多商品详情，商品可能已下架或取消推广",
                }

            title = product_detail.get("name") or "拼多多精选好物"
            price = product_detail.get("price")
            coupon = product_detail.get("coupon_amount")

            # 5. 执行三层智能生链（支持推手归因 uid）
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
                return conv_res
            if conv_res.get("is_fallback"):
                return {
                    "status": "error",
                    "error_code": "PROMOTION_FAILED",
                    "error_msg": "该拼多多商品暂不支持生成推广链接",
                    "product": product_detail,
                }

            conv_res["goods_id"] = id_val
            conv_res["goods_sign"] = sign_val
            conv_res["title"] = title
            conv_res["price"] = price
            conv_res["coupon_amount"] = coupon
            conv_res["product"] = product_detail
            return stamp_universal_resolution(conv_res, single_item_evidence=True)

        base = [("pdd_router_universal", _do_pdd_universal)] if (self._has_official_creds() and self.pid) else []
        providers = self._compose_providers("universal", base, raw_text=text, uid=uid, **kwargs)
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
