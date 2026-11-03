# -*- coding: utf-8 -*-
"""拼多多边界：SDK 调用、原始字段转换、商品标识与推广降级策略。"""
from typing import Dict, Any
from urllib.parse import urlencode, urlsplit
from adapters.base import BasePlatformAdapter, build_command_text
from models.product import ProductRef, ProductSummary, ProductDetail, clean_product_id
from config import PDD_CLIENT_ID, PDD_CLIENT_SECRET, PDD_PID, logger
from infrastructure.pdd_client import (
    call_pdd_api,
    clean_goods_item,
    check_member_authority,
    generate_authority_url as pdd_gen_auth_url,
    generate_smart_promotion_url,
    generate_goods_promotion_url,
    make_custom_parameters,
    convert_pdd_url,
    get_goods_detail as pdd_get_detail,
    query_increment_orders,
)


class PddAdapter(BasePlatformAdapter):
    platform_code = "pdd"
    platform_name = "拼多多"
    recommendation_limit = 400
    channels = (
        {"channel_type": 5, "name": "实时热销", "icon": "fire"},
        {"channel_type": 1, "name": "今日爆款", "icon": "crown"},
        {"channel_type": 6, "name": "百亿补贴", "icon": "diamond"},
        {"channel_type": 3, "name": "高佣优选", "icon": "tag"},
        {"channel_type": 4, "name": "秒杀抢购", "icon": "clock"},
        {"channel_type": 7, "name": "品牌大促", "icon": "star"},
    )
    legacy_order_statuses = {"refund": [4], "settled": [3, 5]}

    def __init__(self, client_id=PDD_CLIENT_ID, client_secret=PDD_CLIENT_SECRET, pid=PDD_PID):
        self.client_id = client_id
        self.client_secret = client_secret
        self.pid = pid

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None) -> ProductRef:
        raw = str(goods_id_or_sign or "").strip()
        if raw and not goods_id and not goods_sign:
            if raw.isdigit():
                goods_id = raw
            else:
                goods_sign = raw
        return ProductRef(self.platform_code, goods_id or "", goods_sign or "")

    def warmup(self) -> None:
        from infrastructure.pdd_client import warmup
        warmup()

    def normalize_order(self, order: dict) -> dict:
        result = dict(order)
        status = result.get("order_status")
        for field, rule in (("is_refund", "refund"), ("is_settled", "settled")):
            result[field] = bool(result[field]) if result.get(field) is not None else status in self.legacy_order_statuses[rule]
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

    def get_recommend_goods(self, channel_type=5, offset=0, limit=20, uid=None, **kwargs) -> Dict[str, Any]:
        """400 条推荐上限是拼多多规则，不下沉到通用商品服务。"""
        if offset >= self.recommendation_limit:
            return {"status": "success", "total_count": self.recommendation_limit, "offset": offset,
                    "limit": limit, "has_more": False, "data": []}
        request_limit = min(limit, self.recommendation_limit - offset)
        try:
            params = {
                "channel_type": channel_type, "offset": offset, "limit": request_limit,
                "pid": self.pid, "custom_parameters": make_custom_parameters(uid=uid) if uid else None,
                **kwargs,
            }
            res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.recommend.get", params)
            response = res.get("goods_basic_detail_response", {})
            raw_items = response.get("list", [])
            items = [self._normalize_goods_summary(clean_goods_item(item, source_type=f"pdd_recommend_{channel_type}"))
                     for item in raw_items if item]
            return {
                "status": "success", "total_count": response.get("total", len(items)),
                "offset": offset, "limit": limit, "list_id": response.get("list_id", ""),
                "has_more": len(raw_items) >= request_limit and offset + request_limit < self.recommendation_limit,
                "data": items,
            }
        except Exception as exc:
            logger.error(f"[PddAdapter.get_recommend_goods] {exc}")
            return {"status": "error", "error_msg": str(exc), "data": []}

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs) -> Dict[str, Any]:
        try:
            size = max(1, min(100, int(page_size or 20)))
            params = {"keyword": keyword, "pid": self.pid, "page": page, "page_size": size,
                      "custom_parameters": make_custom_parameters(uid=uid) if uid else None, **kwargs}
            res = call_pdd_api(self.client_id, self.client_secret, "pdd.ddk.goods.search", params)
            response = res.get("goods_search_response", {})
            raw_items = response.get("goods_list", [])
            items = [self._normalize_goods_summary(clean_goods_item(item, source_type="pdd_search"))
                     for item in raw_items if item]
            total = response.get("total_count")
            return {
                "status": "success", "total_count": total if total is not None else len(items),
                "page": page, "page_size": size, "list_id": response.get("list_id", ""),
                "has_more": page * size < total if total is not None else len(raw_items) >= size,
                "data": items,
            }
        except Exception as exc:
            logger.error(f"[PddAdapter.search_goods] {exc}")
            return {"status": "error", "error_msg": str(exc), "data": []}

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs) -> Dict[str, Any]:
        try:
            ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
            res = pdd_get_detail(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid,
                                 goods_sign=ref.goods_sign or None, goods_id=ref.goods_id or None, uid=uid, **kwargs)
            if res.get("status") != "success" or not res.get("data"):
                return {"status": "error", "error_msg": res.get("error_msg", "未查询到官方商品详情")}
            item = self._normalize_goods(res["data"])
            if ref.goods_sign and not item.get("goods_sign"):
                item["goods_sign"] = ref.goods_sign
            if not ref.goods_sign and ref.goods_id and item.get("goods_id") not in ("", ref.goods_id):
                return {"status": "error", "error_msg": "商品 ID 反查结果不匹配", "error_code": "product_identity_mismatch"}
            return {"status": "success", "data": item}
        except Exception as exc:
            logger.error(f"[PddAdapter.get_goods_detail] {exc}")
            return {"status": "error", "error_msg": str(exc)}

    @staticmethod
    def _build_command_text(title, final_link, price=None, coupon=None) -> str:
        return build_command_text(title, final_link, price, coupon, "拼多多")

    def _resolve_goods_id(self, goods_id_or_sign: str, kwargs: dict) -> tuple:
        ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
        return ref.goods_id or None, ref.goods_sign or None

    def _promotion_result(self, link, raw, title, price, coupon, link_type, is_fallback=False) -> dict:
        """所有生链路径返回同一契约，降级标记不能在某个分支丢失。"""
        return {
            "status": "success", "need_auth": False, "is_fallback": is_fallback, "link_type": link_type,
            "final_link": link, "url": link, "short_url": link, "mobile_url": link, "mobile_short_url": link,
            "schema_url": raw.get("schema_url") or "",
            "command_text": self.build_command_text(title, link, price, coupon),
            "we_app_info": raw.get("we_app_info") or {}, "raw": raw,
        }

    def convert_promotion_url(self, goods_id_or_sign, uid=None, **kwargs) -> Dict[str, Any]:
        goods_id, goods_sign = self._resolve_goods_id(goods_id_or_sign, kwargs)
        title = kwargs.get("title") or kwargs.get("name") or kwargs.get("goods_name") or "精选高性价比好物"
        price = kwargs.get("price")
        coupon = kwargs.get("coupon_amount") if kwargs.get("coupon_amount") is not None else kwargs.get("coupon")
        product_url = kwargs.get("product_url")
        options = {k: v for k, v in kwargs.items() if k not in
                   ("goods_id", "goods_sign", "product_url", "title", "name", "goods_name", "price", "coupon", "coupon_amount")}
        target = goods_sign or goods_id
        if target:
            try:
                res = generate_smart_promotion_url(client_id=self.client_id, client_secret=self.client_secret,
                                                   pid=self.pid, goods_sign=target, uid=uid, **options)
                raw = res.get("data") or {}
                link = raw.get("mobile_short_url") or raw.get("short_url") or raw.get("mobile_url") or raw.get("url")
                if res.get("status") == "success" and link:
                    return self._promotion_result(link, raw, title, price, coupon, res.get("link_type", "专属推广链接"))
            except Exception as exc:
                logger.warning(f"[PddAdapter] 智能生链失败，进入官方转链兜底: {exc}")
        return self._handle_fallback_conversion(goods_id, goods_sign or "", uid, title, price, coupon,
                                                product_url=product_url, **options)

    def _handle_unauthorized_conversion(self, goods_sign, uid, **kwargs) -> Dict[str, Any]:
        return self.convert_promotion_url(goods_sign, uid=uid, **kwargs)

    def _handle_fallback_conversion(self, goods_id, goods_sign, uid, title="精选高性价比好物", price=None, coupon=None,
                                    product_url=None, **kwargs) -> Dict[str, Any]:
        # 已有主标识时不能让客户端另一条 URL 把商品切换成其它商品。
        source_url = product_url if not goods_id and not goods_sign else None
        if goods_id and str(goods_id).isdigit():
            source_url = "https://mobile.pinduoduo.com/goods.html?" + urlencode({"goods_id": goods_id})
        if source_url:
            parsed = urlsplit(source_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("https", "http") or not any(
                    host == root or host.endswith("." + root) for root in ("pinduoduo.com", "yangkeduo.com")):
                return {"status": "error", "error_msg": "请提供有效的拼多多商品链接"}
            try:
                res = convert_pdd_url(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid,
                                      source_url=source_url, uid=uid)
                if res.get("status") == "success" and res.get("jump_url"):
                    raw = res.get("raw_data") or {}
                    result = self._promotion_result(res["jump_url"], raw, title, price, coupon, "官方招商券推广")
                    result["schema_url"] = raw.get("schema_url") or raw.get("mobile_url") or ""
                    return result
            except Exception as exc:
                logger.warning(f"[PddAdapter] 招商券转链失败: {exc}")
        if goods_sign:
            try:
                res = generate_goods_promotion_url(client_id=self.client_id, client_secret=self.client_secret,
                                                   pid=self.pid, goods_sign=goods_sign, uid=None, **kwargs)
                raw = res.get("data") or {}
                link = raw.get("mobile_short_url") or raw.get("short_url") or raw.get("mobile_url") or raw.get("url")
                if res.get("status") == "success" and link:
                    # 这是原有的无 UID 兜底：登录用户不能将其视为已保证佣金归因。
                    return self._promotion_result(link, raw, title, price, coupon, "通用官方推广", is_fallback=bool(uid))
            except Exception as exc:
                logger.warning(f"[PddAdapter] 通用推广生链失败: {exc}")
        if not source_url and not goods_sign:
            return {"status": "error", "error_msg": "缺少商品 ID、Sign 或商品链接"}
        link = source_url or ("https://mobile.yangkeduo.com/goods.html?" + urlencode({"goods_sign": goods_sign}))
        return self._promotion_result(link, {}, title, price, coupon, "商品直连保底", is_fallback=True)

    def check_authority(self, uid) -> Dict[str, Any]:
        try:
            return check_member_authority(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid, uid=uid)
        except Exception as exc:
            logger.error(f"[PddAdapter.check_authority] {exc}")
            return {"status": "error", "is_authorized": False, "bind": 0, "error_msg": str(exc)}

    def generate_authority_url(self, uid, **kwargs) -> Dict[str, Any]:
        try:
            res = pdd_gen_auth_url(client_id=self.client_id, client_secret=self.client_secret, pid=self.pid, uid=uid, **kwargs)
            info = res.get("url_info") or {}
            link = info.get("mobile_short_url") or info.get("mobile_url") or info.get("short_url") or info.get("url")
            if res.get("status") == "success" and link:
                return {"status": "success", "auth_url": link, "short_url": link, "we_app_info": info.get("we_app_info", {})}
            return {"status": "error", "error_msg": res.get("error_msg", "生成授权链接失败")}
        except Exception as exc:
            logger.error(f"[PddAdapter.generate_authority_url] {exc}")
            return {"status": "error", "error_msg": str(exc)}

    def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs) -> Dict[str, Any]:
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
            logger.error(f"[PddAdapter.fetch_increment_orders] {exc}")
            return {"status": "error", "error_msg": str(exc), "order_list": [], "total_count": 0}
