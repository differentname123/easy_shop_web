# -*- coding: utf-8 -*-
"""京东字段/分页适配；无授权、用户仓储或个人转站长兜底。"""
import hashlib
import math
from datetime import datetime

from adapters.base import BasePlatformAdapter
from infrastructure.jd_client import (
    JdClient, DEFAULT_API_BASE, ORDER_WINDOW_SECONDS, SHANGHAI,
    canonical_promoter_id, product_url,
)
from models.product import ProductDetail, ProductRef, ProductSummary, clean_product_id


JD_ORDER_STATUS = {
    2: "无效-拆单", 3: "成交未结算", 15: "待付款", 16: "已付款",
    17: "已完成", 18: "已结算", 19: "已失效/锁定", 20: "平台拦截",
    21: "数据异常", 22: "白条拒绝", 23: "拒收", 24: "支付后取消",
    25: "未支付取消", 27: "售后中", 28: "售后退款",
}


def _text(value):
    return str(value).strip() if value is not None else ""


def _pick(*values):
    return next((value for value in values if value is not None and value != ""), None)


def _money(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return round(number, 2) if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _timestamp(value, *, precise=False):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        seconds = number / 1000 if number > 10**11 else number
        return seconds if precise else int(seconds)
    except (ValueError, TypeError, OverflowError):
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            seconds = (date if date.tzinfo else date.replace(tzinfo=SHANGHAI)).timestamp()
            return seconds if precise else int(seconds)
        except (ValueError, TypeError, OverflowError, OSError):
            return None


def _error(code, message, **extra):
    return {"status": "error", "error_code": code, "error_msg": message, **extra}


def _item_id(item):
    return next((clean_product_id(item.get(key)) for key in ("tao_id", "itemId", "skuId")
                 if clean_product_id(item.get(key))), "")


def _is_encrypted_jd_item_id(value):
    """识别京东联盟官方加密 itemId (如榜单/搜索返回的字母数字下划线混合加密 ID)。"""
    text = _text(value)
    return len(text) >= 25 and "_" in text and all(c.isalnum() or c == "_" for c in text)


def _material_url(item):
    source = product_url(item.get("item_url"))
    if source:
        return source
    # Only an explicit supplier SKU or official encrypted itemId is evidence for a canonical item URL.
    sku = canonical_promoter_id(item.get("skuId"))
    if sku:
        return f"https://item.jd.com/{sku}.html"
    raw_item_id = clean_product_id(item.get("itemId"))
    if _is_encrypted_jd_item_id(raw_item_id):
        return f"https://item.jd.com/{raw_item_id}.html"
    return ""


class JdAdapter(BasePlatformAdapter):
    platform_code = "jd"
    platform_name = "京东"
    channels = (
        {"channel_type": 5, "name": "实时销量榜", "icon": "fire"},
        {"channel_type": 1, "name": "今日销量榜", "icon": "crown"},
    )
    requires_authorized_promotion = False
    requires_numeric_promoter_id = True
    requires_material_url = True
    order_window_seconds = ORDER_WINDOW_SECONDS

    def __init__(self, settings=None, client=None, **overrides):
        if settings is None:
            from config import settings
        def setting(name, default=""):
            return overrides.get(name, getattr(settings, name, default))
        self.active = bool(setting("jd_enabled", False))
        self.default_promoter_id = canonical_promoter_id(setting("jd_position_id"))
        union_id = _text(setting("jd_union_id"))
        self.promotion_scope = hashlib.sha256((union_id + ":" + self.default_promoter_id).encode()).hexdigest()[:24]
        raw_appkeys = setting("ztk_appkey_list", ()) or setting("ztk_appkey", "")
        self.client = client if client is not None else JdClient(
            raw_appkeys, union_id, setting("jd_app_key"), setting("jd_app_secret"),
            setting("jd_api_base", DEFAULT_API_BASE))
        has_appkey = bool(getattr(self.client, "appkey_list", None) or setting("ztk_appkey") or setting("ztk_appkey_list"))
        catalog = bool(client is not None or has_appkey and canonical_promoter_id(union_id))
        self.capabilities = {"recommend": catalog, "search": catalog, "detail": catalog,
                             "promotion": bool(catalog and self.default_promoter_id), "authorization": False,
                             "orders": bool(client is not None or has_appkey and
                                            setting("jd_app_key") and setting("jd_app_secret"))}

    def can_prefetch_detail(self, ref=None):
        """禁用后台盲目单品详情轮询，避免无故消耗 ZTK_APPKEY_LIST 配额。"""
        return False

    def _available(self, capability):
        if not self.active:
            return _error("platform_unavailable", "京东暂未启用")
        if not self.capabilities.get(capability):
            return _error("CONFIG_ERROR", "京东该功能缺少必要配置")
        return None

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
        val = _text(goods_sign or goods_id or goods_id_or_sign)
        return ProductRef(self.platform_code, val, val)

    def _summary(self, item):
        raw_id = _item_id(item)
        if not raw_id:
            raise ValueError("missing goods identity")
        purchase = item.get("purchasePriceInfo")
        purchase = purchase if isinstance(purchase, dict) else {}
        price = _pick(item.get("quanhou_jiage"), item.get("wlprice"), purchase.get("price"), item.get("size"))
        original = _pick(item.get("size"), purchase.get("price"), price)
        price, original = _money(price), _money(original)
        name = _text(_pick(item.get("title"), item.get("tao_title"), item.get("skuName")))
        if price is None or price < 0 or not name:
            raise ValueError("missing or invalid public product content")
        coupon = _money(item.get("coupon_info_money")) or 0.0
        data = {
            "platform": self.platform_code, "platform_name": self.platform_name,
            "goods_id": raw_id, "goods_sign": raw_id,
            "name": name,
            "image_url": _text(_pick(item.get("pict_url"), item.get("imageUrl"))),
            "price": price, "original_price": original if original is not None and original >= 0 else price,
            "coupon_amount": coupon, "has_coupon": coupon > 0,
            "commission_rate": _money(_pick(item.get("tkrate3"), item.get("commissionShare"))) or 0.0,
            "commission_amount": _money(_pick(item.get("tkfee3"), item.get("commission"))) or 0.0,
            "sales_tip": _text(_pick(item.get("volume"), item.get("inOrderCount30Days"), "0")),
            "mall_name": _text(item.get("shop_title")),
        }
        summary = ProductSummary(**data).to_dict(exclude_unset=True)
        material = _material_url(item)
        if material:
            summary["product_url"] = material
        return summary

    def _detail(self, item):
        data = self._summary(item)
        images = item.get("small_images")
        images = images.split("|") if isinstance(images, str) else images if isinstance(images, list) else []
        images = list(dict.fromkeys(image.strip() for image in images if isinstance(image, str) and image.strip()))
        data.update(desc=_text(item.get("description")), carousel_images=images,
                    product_url=_material_url(item), detail_tags=[], service_guarantees=[],
                    shop_ratings={}, specs=[], coupon_info={})
        return ProductDetail(**data).to_dict(exclude_unset=True)

    def get_recommend_goods(self, channel_type=5, offset=0, limit=20, uid=None, detail_sink=None, **kwargs):
        unavailable = self._available("recommend")
        if unavailable:
            return {**unavailable, "data": []}
        try:
            channel, offset, limit = int(channel_type), int(offset), int(limit)
            if channel not in (5, 1) or offset < 0 or not 1 <= limit <= 100:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return _error("INVALID_ARGUMENT", "京东榜单频道或分页无效", data=[])
        page, skip = offset // limit + 1, offset % limit
        items, total, more = [], None, False
        # Offset can straddle two upstream pages. Never publish a partial failed page.
        while len(items) < limit:
            result = self.client.get_rank_goods({5: 1, 1: 3}[channel], page=page, page_size=limit)
            if result.get("status") != "success":
                return {**result, "data": []}
            rows = result.get("data")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                return _error("PROTOCOL_ERROR", "京东榜单商品列表无效", data=[])
            selected = rows[skip:skip + limit - len(items)]
            try:
                items.extend(self._summary(item) for item in selected)
            except (ValueError, TypeError):
                return _error("PROTOCOL_ERROR", "京东榜单商品身份或字段无效", data=[])
            total = result.get("total_count")
            more = skip + len(selected) < len(rows) or bool(result.get("has_more"))
            if len(items) >= limit or not more or not rows:
                break
            skip, page = 0, page + 1
        # Rank summaries never claim full DetailSnapshots, even with a detail_sink.
        return {"status": "success", "data": items, "total_count": total,
                "offset": offset, "next_offset": offset + len(items), "has_more": more, "next_cursor": None}

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs):
        unavailable = self._available("search")
        if unavailable:
            return {**unavailable, "data": []}
        result = self.client.search_goods(keyword, page=page, page_size=page_size)
        if result.get("status") != "success":
            return result
        try:
            data = [self._summary(item) for item in result["data"]]
        except (ValueError, TypeError, KeyError):
            return _error("PROTOCOL_ERROR", "京东搜索商品身份或字段无效", data=[])
        return {**result, "data": data}

    def _fetch_item(self, ref):
        target_id = ref.goods_id or ref.goods_sign
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东需要商品ID而非未经核验的请求URL")
        result = self.client.get_goods_detail(target_id)
        if result.get("status") != "success":
            return result
        rows = result.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return _error("PROTOCOL_ERROR", "京东详情商品列表无效")
        matches = [item for item in rows if _item_id(item) == target_id]
        if not matches and len(rows) == 1:
            single = dict(rows[0])
            if _material_url(single):
                single["tao_id"] = target_id
                single["skuId"] = target_id
                matches = [single]
        if len(matches) != 1:
            return _error("PRODUCT_IDENTITY_MISMATCH", "京东详情未证实请求商品身份")
        return {"status": "success", "data": matches[0]}

    def get_goods_detail(self, goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs):
        unavailable = self._available("detail")
        if unavailable:
            return unavailable
        ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        result = self._fetch_item(ref)
        if result.get("status") != "success":
            return result
        try:
            return {"status": "success", "data": self._detail(result["data"])}
        except (ValueError, TypeError):
            return _error("PROTOCOL_ERROR", "京东详情字段无效")

    def convert_promotion_url(self, goods_id_or_sign="", uid=None, **kwargs):
        unavailable = self._available("promotion")
        if unavailable:
            return unavailable
        # Only the service resolves anonymous/default or authenticated attribution.
        # A malformed personal ID never falls back to default_promoter_id here.
        promoter = canonical_promoter_id(kwargs.get("promoter_id"))
        if not promoter:
            return _error("INVALID_PROMOTER_ID", "京东转链需要服务端数字推广ID")
        ref = self.resolve_product_ref(goods_id_or_sign, kwargs.get("goods_id"), kwargs.get("goods_sign"))
        target_id = ref.goods_id or ref.goods_sign
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东转链缺少已识别商品ID")
        # The service strips request kwargs before supplying this private bridge.
        material = product_url(kwargs.get("trusted_product_url"))
        if not material and _is_encrypted_jd_item_id(target_id):
            material = f"https://item.jd.com/{target_id}.html"
        if not material:
            detail = self._fetch_item(ref)
            if detail.get("status") != "success":
                return detail
            material = _material_url(detail["data"])
        if not material:
            return _error("MATERIAL_URL_REQUIRED", "京东物料ID缺少已核验的商品链接")
        result = self.client.convert_promotion_link(material, promoter)
        if result.get("status") != "success":
            return result
        short, long = result.get("short_url") or "", result.get("click_url") or ""
        token = result.get("j_short_command") or result.get("j_command") or ""
        return {"status": "success", "url": long or short, "final_link": short or long,
                "mobile_url": long or short, "short_url": short, "click_url": long,
                "j_command": result.get("j_command") or "", "j_short_command": result.get("j_short_command") or "",
                "promotion_token": token, "token_type": "jcommand" if token else "",
                "we_app_info": {}, "promoter_id": promoter, "goods_id": ref.goods_id, "goods_sign": ref.goods_sign,
                "attribution_status": "verified" if uid else "anonymous", "need_auth": False,
                "is_fallback": False, "cacheable": False, "link_type": "京东推广链接"}

    def check_authority(self, uid):
        if not self.active:
            return _error("platform_unavailable", "京东暂未启用", is_authorized=False)
        return {"status": "not_required", "authorization_required": False, "is_authorized": False, "bind": 0}

    def generate_authority_url(self, uid, **kwargs):
        return self.check_authority(uid)

    def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs):
        unavailable = self._available("orders")
        result = unavailable or self.client.query_orders(start_time, end_time, page=page,
                                                         page_size=page_size, cursor=kwargs.get("cursor"))
        if result.get("status") != "success":
            return {**result, "order_list": [], "has_more": False, "next_cursor": None}
        return result

    def normalize_order(self, order):
        if not isinstance(order, dict) or not _text(order.get("id")):
            raise ValueError("京东订单缺少行ID")
        try:
            status = int(order.get("validCode"))
        except (TypeError, ValueError):
            status = _text(order.get("validCode"))
        estimate, actual = _money(order.get("estimateFee")), _money(order.get("actualFee"))
        settled = status == 18
        commission = actual if settled else estimate
        created = _timestamp(order.get("orderTime"))
        # 来源时钟用于CAS，毫秒/小数不能截成同一秒后让旧页回滚状态。
        modified = _timestamp(order.get("modifyTime"), precise=True)
        promoter = canonical_promoter_id(order.get("positionId"))
        try:
            quantity = int(order.get("skuNum")) if order.get("skuNum") is not None else 1
        except (TypeError, ValueError, OverflowError):
            raise ValueError("京东订单数量无效") from None
        return {
            "platform": self.platform_code, "order_sn": _text(order["id"]),
            "parent_order_sn": _text(order.get("orderId")), "goods_id": _text(order.get("skuId")),
            "goods_name": _text(order.get("skuName")), "goods_thumbnail_url": "", "goods_quantity": quantity,
            "order_amount_yuan": _money(order.get("estimateCosPrice")) or 0.0,
            "order_amount_basis": "commissionable", "order_time": created, "order_time_basis": "created",
            "order_create_time": created, "order_pay_time": None, "order_pay_time_str": "",
            "order_finish_time": _timestamp(order.get("finishTime")), "order_settle_time": None,
            "promotion_amount_yuan": commission if commission is not None else 0.0,
            "estimated_commission_yuan": estimate, "settled_commission_yuan": actual,
            "commission_basis": ("settlement_reported" if actual is not None else "missing_settlement") if settled else
                                ("estimate" if estimate is not None else "missing_estimate"),
            "promotion_rate": _money(order.get("commissionRate")) or 0.0,
            "order_status": status, "order_status_desc": JD_ORDER_STATUS.get(status, "未知状态"),
            "is_settled": settled, "is_refund": status == 28,
            "is_invalid": status in (2, 19, 20, 21, 22, 23, 24, 25), "is_after_sale": status == 27,
            "promoter_id": promoter, "position_id": _text(order.get("positionId")),
            "sub_union_id": _text(order.get("subUnionId")), "attribution_identity": {},
            "source_updated_at": modified, "order_modify_at": modified,
        }
