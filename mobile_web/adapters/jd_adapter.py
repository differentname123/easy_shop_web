# -*- coding: utf-8 -*-
"""京东字段/分页适配；无授权、用户仓储或个人转站长兜底。"""
import hashlib
import math
import re
import time
from datetime import datetime
from typing import Optional

import requests
from adapters.base import BasePlatformAdapter
from infrastructure.api_governance import (
    is_real_requests_session,
    stamp_universal_resolution,
)
from infrastructure.hdk_client import HdkClient
from infrastructure.http_utils import (
    clean_int,
    clean_money,
    clean_text as _text,
    error_dict as _error,
    normalize_http_url,
    parse_finite_money as _money,
    parse_shanghai_timestamp as _timestamp,
)
from infrastructure.jd_client import (
    JdClient, DEFAULT_API_BASE, ORDER_WINDOW_SECONDS, SHANGHAI,
    canonical_promoter_id, is_jd_official_venue_url, jd_coupon_url, product_url,
)
from models.product import (
    ProductDetail,
    ProductRef,
    ProductSummary,
    clean_product_id,
    extract_canonical_goods_id,
    extract_jd_image_key,
    is_jd_image_key,
    normalize_jd_material_url,
)


JD_ORDER_STATUS = {
    2: "无效-拆单", 3: "成交未结算", 15: "待付款", 16: "已付款",
    17: "已完成", 18: "已结算", 19: "已失效/锁定", 20: "平台拦截",
    21: "数据异常", 22: "白条拒绝", 23: "拒收", 24: "支付后取消",
    25: "未支付取消", 27: "售后中", 28: "售后退款",
}


def _pick(*values):
    return next((value for value in values if value is not None and value != ""), None)


def _item_id(item):
    return next((clean_product_id(item.get(key)) for key in ("tao_id", "itemId", "skuId")
                 if clean_product_id(item.get(key))), "")


def _is_encrypted_jd_item_id(value):
    """识别京东联盟官方加密 itemId (如榜单/搜索返回的字母数字下划线混合加密 ID)，排除主图 CDN 哈希键。"""
    text = _text(value)
    if not text or is_jd_image_key(text):
        return False
    return len(text) >= 25 and "_" in text and all(c.isalnum() or c == "_" for c in text)


def _is_official_jd_cipher_id(value):
    """识别京东联盟标准双段加密 itemId (<前缀>_<密文后缀>，如 IwQNj..._cIOf4YLZawohplFr3g)。"""
    text = _text(value)
    if not text or is_jd_image_key(text) or text.count("_") != 1:
        return False
    prefix, suffix = text.split("_")
    return len(prefix) >= 20 and len(suffix) >= 12 and prefix.isalnum() and suffix.isalnum()


def _verified_jd_cipher_pair(requested_id, returned_id):
    """请求与上游返回均为标准京东联盟双段加密 itemId 时，证实由京东联盟解密自同一 SKU（兼容跨 Key 重签名与商家换主图）。"""
    return _is_official_jd_cipher_id(requested_id) and _is_official_jd_cipher_id(returned_id)


def _rank_coupon(purchase, base_price):
    """从京东榜单 purchasePriceInfo.couponList 提取单件可直接使用的最优官方优惠券 (discount, coupon_url)。"""
    coupons = purchase.get("couponList") if isinstance(purchase, dict) else None
    if not isinstance(coupons, list) or base_price is None or base_price <= 0:
        return 0.0, ""
    best = (0.0, "")
    for entry in coupons:
        if not isinstance(entry, dict) or entry.get("couponStatus") not in (None, 0, "0"):
            continue
        remain = _money(entry.get("remainCnt"))
        if entry.get("remainCnt") not in (None, "") and (remain is None or remain <= 0):
            continue
        disc, quota = _money(entry.get("discount")), _money(entry.get("quota"))
        link = jd_coupon_url(entry.get("link"))
        if link and disc is not None and quota is not None and 0 < disc < base_price and 0 <= quota <= base_price:
            if disc > best[0]:
                best = (disc, link)
    return best


def _material_url(item):
    source = product_url(item.get("item_url"))
    if source:
        if source.startswith("https://item.jd.com/") and source.endswith(".html"):
            candidate_id = source[len("https://item.jd.com/"):-len(".html")]
            if _is_encrypted_jd_item_id(candidate_id):
                return f"https://jingfen.jd.com/detail/{candidate_id}.html"
        return source
    # Only an explicit supplier SKU or official encrypted itemId is evidence for a canonical item URL.
    sku = canonical_promoter_id(item.get("skuId"))
    if sku:
        return f"https://item.jd.com/{sku}.html"
    raw_item_id = clean_product_id(item.get("itemId") or item.get("tao_id"))
    if _is_encrypted_jd_item_id(raw_item_id):
        return f"https://jingfen.jd.com/detail/{raw_item_id}.html"
    return ""


class JdAdapter(BasePlatformAdapter):
    platform_code = "jd"
    platform_name = "京东"
    requires_authorized_promotion = False
    requires_numeric_promoter_id = True
    requires_material_url = True
    supports_coupon_enrichment = True
    preserves_known_coupons_across_rank = True
    order_window_seconds = ORDER_WINDOW_SECONDS

    def __init__(self, settings=None, client=None, **overrides):
        self._ensure_provider_state()
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
        self._has_ztk_catalog = bool(client is not None or has_appkey and canonical_promoter_id(union_id))
        self._has_ztk_orders = bool(
            client is not None or has_appkey and setting("jd_app_key") and setting("jd_app_secret")
        )
        hdk_key = setting("hdk_api_key", "")
        self.hdk = overrides.get("hdk_client") or (HdkClient(hdk_key, union_id=union_id) if hdk_key else None)
        if self.hdk:
            self.attach_hdk(self.hdk)
        self._refresh_capabilities()

    def attach_hdk(self, hdk_client: HdkClient) -> None:
        """挂载好单库 (HDK) 为京东高配额能力提供者：搜索/详情为主力，转链/万能为兜底。"""
        self.hdk = hdk_client
        self.register_provider("search", "hdk_jd_search", self._make_hdk_search(hdk_client), prepend=True)
        self.register_provider("detail", "hdk_jd_detail", self._make_hdk_detail(hdk_client), prepend=True)
        self.register_provider("promotion", "hdk_jd_promotion", self._make_hdk_promotion(hdk_client), prepend=False)
        self.register_provider("universal", "hdk_jd_universal", self._make_hdk_universal(hdk_client), prepend=False)

    def _normalize_hdk_summary(self, item: dict) -> dict:
        raw_sign = _text(item.get("skuid") or item.get("itemid"))
        if not raw_sign:
            raise ValueError("missing goods identity")
        image_url = normalize_http_url(item.get("itempic") or item.get("white_image"))
        canonical_id = extract_canonical_goods_id(self.platform_code, raw_sign, image_url=image_url) or raw_sign
        price = clean_money(item.get("itemendprice") or item.get("itemprice"), 0.0)
        original = clean_money(item.get("itemprice") or price, price)
        if original is not None and price is not None and original < price:
            original = price
        coupon_amt = clean_money(item.get("couponmoney"), 0.0) or 0.0
        coupon_url = jd_coupon_url(item.get("couponurl"))
        name = _text(item.get("goodsname"))
        if not name:
            raise ValueError("missing goods title")
        rate = clean_money(item.get("commission"), 0.0) or 0.0
        rate_percent = round(rate * 100, 2) if 0 < rate <= 1.0 else rate
        comm_amount = round(price * (rate_percent / 100.0), 2) if price else 0.0
        sales = _text(item.get("itemsale") or item.get("comments") or "0")
        mall = _text(item.get("shopname")) or "京东店铺"
        brand = _text(item.get("brand_name")) or "品牌甄选"
        cat_info = item.get("category_info") if isinstance(item.get("category_info"), dict) else {}
        cat = _text(cat_info.get("cid3Name") or cat_info.get("cid1Name")) or "精选好物"

        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": canonical_id,
            "goods_sign": raw_sign,
            "name": name,
            "image_url": image_url,
            "product_url": f"https://jingfen.jd.com/detail/{raw_sign}.html",
            "price": price,
            "original_price": original if original is not None else price,
            "coupon_amount": coupon_amt,
            "has_coupon": coupon_amt > 0 and bool(coupon_url),
            "commission_rate": rate_percent,
            "commission_amount": comm_amount,
            "sales_tip": sales,
            "mall_name": mall,
            "brand": brand,
            "category": cat,
        }
        if coupon_url:
            data["coupon_url"] = coupon_url
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _normalize_hdk_detail(self, item: dict, ref: Optional[ProductRef] = None) -> dict:
        summary_data = self._normalize_hdk_summary(item)
        raw_images = _text(item.get("jd_image"))
        carousel = [
            normalize_http_url(img)
            for img in (raw_images.split(",") if raw_images else [])
            if normalize_http_url(img)
        ]
        if summary_data.get("image_url") and summary_data["image_url"] not in carousel:
            carousel.insert(0, summary_data["image_url"])

        specs = []
        if summary_data.get("brand"):
            specs.append({"label": "商品品牌", "value": summary_data["brand"]})
        if summary_data.get("category"):
            specs.append({"label": "商品类目", "value": summary_data["category"]})
        if summary_data.get("mall_name"):
            specs.append({"label": "店铺名称", "value": summary_data["mall_name"]})

        shop_info = item.get("shop_info") if isinstance(item.get("shop_info"), dict) else {}
        ratings = {}
        for source, target in (("userEvaluateScore", "desc_score"), ("logisticsLvyueScore", "lgst_score"), ("afterServiceScore", "serv_score")):
            s_val = _text(shop_info.get(source))
            if s_val and s_val not in ("0", "0.0"):
                ratings[target] = s_val

        tags = []
        if summary_data.get("coupon_amount", 0) > 0:
            tags.append(f"{summary_data['coupon_amount']:g}元优惠券")
        for tag in item.get("sku_tag_list") or []:
            if isinstance(tag, dict) and tag.get("name"):
                t_name = _text(tag["name"])
                if t_name and t_name not in tags:
                    tags.append(t_name)

        detail_payload = {
            **summary_data,
            "carousel_images": carousel,
            "detail_tags": tags,
            "specs": specs,
            "shop_ratings": ratings,
            "coupon_info": {
                "coupon_amount": summary_data.get("coupon_amount", 0.0),
                "coupon_url": summary_data.get("coupon_url", ""),
                "quota": clean_money(item.get("quota"), 0.0),
                "coupon_start_time": clean_int(item.get("couponstarttime")),
                "coupon_end_time": clean_int(item.get("couponendtime")),
            },
        }
        return ProductDetail(**detail_payload).to_dict(exclude_unset=True)

    def _make_hdk_search(self, client: HdkClient):
        def _search(keyword="", page=1, page_size=20, uid=None, **kwargs):
            res = client.search_goods(keyword, page=page, page_size=page_size)
            if res.get("status") != "success":
                return res
            items = res.get("data") or []
            summaries = []
            for it in items:
                try:
                    summaries.append(self._normalize_hdk_summary(it))
                except Exception:
                    continue
            return {
                "status": "success",
                "data": summaries,
                "total_count": res.get("total_count", len(summaries)),
                "page": page,
                "page_size": page_size,
                "has_more": bool(res.get("has_more")) if "has_more" in res else len(summaries) >= page_size,
            }
        return _search

    def _make_hdk_detail(self, client: HdkClient):
        def _detail(goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs):
            ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
            target_id = ref.goods_sign or ref.goods_id
            if not target_id or is_jd_image_key(target_id):
                return _error("INVALID_ARGUMENT", "京东详情接口需要加密 itemId (goods_sign) 或数字 SKU")
            res = client.get_goods_detail(target_id)
            if res.get("status") != "success":
                return res
            raw_item = res.get("data") or {}
            try:
                return {"status": "success", "data": self._normalize_hdk_detail(raw_item, ref)}
            except Exception as exc:
                return _error("PROTOCOL_ERROR", f"好单库详情解析失败: {exc}")
        return _detail

    def _make_hdk_promotion(self, client: HdkClient):
        def _promote(goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, promoter_id=None, **kwargs):
            ref = self.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
            target_id = ref.goods_sign or ref.goods_id
            if not target_id:
                return _error("INVALID_ARGUMENT", "京东转链缺少已识别商品ID")
            promoter = canonical_promoter_id(promoter_id or self.default_promoter_id)
            mat = kwargs.get("trusted_product_url") or (f"https://jingfen.jd.com/detail/{target_id}.html" if _is_encrypted_jd_item_id(target_id) else target_id)
            coupon = jd_coupon_url(kwargs.get("trusted_coupon_url"))
            sub_id = kwargs.get("sub_union_id")
            res = client.convert_promotion_link(mat, promoter_id=promoter, sub_union_id=sub_id, coupon_url=coupon)
            if res.get("status") != "success":
                return res
            extra = {"coupon_url": coupon} if coupon else {}
            return self._build_promotion_payload(
                res,
                promoter=promoter,
                goods_id=ref.goods_id,
                goods_sign=ref.goods_sign,
                uid=uid,
                **extra,
            )
        return _promote

    def _make_hdk_universal(self, client: HdkClient):
        def _universal(raw_text="", uid=None, promoter_id=None, **kwargs):
            text = _text(raw_text)
            if not text:
                return _error("INVALID_ARGUMENT", "请输入京东链接或商品信息")
            promoter = canonical_promoter_id(promoter_id or self.default_promoter_id)
            short_match = re.search(r"https?://u\.jd\.com/[A-Za-z0-9_-]+", text)
            card = None
            target_id = ""
            if short_match:
                res = client.resolve_short_url(short_match.group(0))
                if res.get("status") == "success" and isinstance(res.get("data"), dict):
                    try:
                        card = self._normalize_hdk_detail(res["data"])
                        target_id = card.get("goods_sign") or card.get("goods_id")
                    except Exception:
                        pass
            if not card:
                jf_match = re.search(r"jingfen\.jd\.com/detail/([A-Za-z0-9_]{20,})\.html", text)
                if jf_match:
                    target_id = jf_match.group(1)
                elif _is_encrypted_jd_item_id(text):
                    target_id = text
                if target_id:
                    det = self.get_goods_detail(target_id)
                    if det.get("status") == "success" and isinstance(det.get("data"), dict):
                        card = det["data"]
            if not target_id and not card:
                return _error("NOT_FOUND", "未能识别该京东链接或商品")

            mat = f"https://jingfen.jd.com/detail/{target_id}.html" if _is_encrypted_jd_item_id(target_id) else (short_match.group(0) if short_match else target_id)
            conv = client.convert_promotion_link(mat, promoter_id=promoter, sub_union_id=kwargs.get("sub_union_id"), coupon_url=(card or {}).get("coupon_url"))
            if conv.get("status") != "success":
                return conv
            short = conv.get("short_url") or ""
            long = conv.get("click_url") or ""
            title = (card or {}).get("name") or "京东精选好物"
            price = (card or {}).get("price")
            coupon = (card or {}).get("coupon_amount", 0.0)
            canonical_id = (card or {}).get("goods_id") or extract_canonical_goods_id(self.platform_code, target_id) or target_id
            sign_id = (card or {}).get("goods_sign") or target_id
            payload = {
                "status": "success",
                "final_link": short or long,
                "short_url": short,
                "click_url": long,
                "url": long or short,
                "title": title,
                "price": price,
                "coupon_amount": coupon,
                "product": card,
                "promoter_id": promoter,
                "goods_id": canonical_id,
                "goods_sign": sign_id,
                "attribution_status": "verified" if uid else "anonymous",
                "need_auth": False,
                "link_type": "京东推广链接",
            }
            if card is None:
                payload["detail_lookup_failed"] = True
            return stamp_universal_resolution(payload)
        return _universal

    def _refresh_capabilities(self) -> None:
        cat = getattr(self, "_has_ztk_catalog", False)
        orders_ok = getattr(self, "_has_ztk_orders", False)
        self.capabilities = {
            "recommend": bool(cat or self._has_extra_providers("recommend")),
            "search": bool(cat or self._has_extra_providers("search")),
            "detail": bool(cat or self._has_extra_providers("detail")),
            "promotion": bool((cat and self.default_promoter_id) or self._has_extra_providers("promotion")),
            "authorization": False,
            "orders": bool(orders_ok or self._has_extra_providers("orders")),
        }

    def _is_real_http(self) -> bool:
        return is_real_requests_session(getattr(self.client, "http", None))

    def can_prefetch_detail(self, ref=None):
        """JD 折淘客共用小时级千次配额：榜单/搜索绝不后台盲刷 api_detail，仅允许外挂独立详情源后台预取。"""
        if ref is None or not isinstance(ref, ProductRef):
            return False
        if not self._has_extra_providers("detail"):
            return False
        sign = ref.goods_sign
        return bool(_is_encrypted_jd_item_id(sign) or (sign and sign.isdigit()))

    def normalize_material_url(self, url: str) -> str:
        return normalize_jd_material_url(url)

    def is_authoritative_material_url(self, url: str) -> bool:
        return "jingfen.jd.com" in str(url or "")

    def extract_trusted_coupon_url(self, doc: Optional[dict]) -> str:
        return jd_coupon_url((doc or {}).get("coupon_url"))

    def should_enrich_promotion_coupon(self, ref: ProductRef, local_doc: Optional[dict]) -> bool:
        if (
            local_doc is not None
            and local_doc.get("detail_updated_at") is not None
            and float(local_doc.get("coupon_amount") or 0.0) <= 0
        ):
            return False
        target_sign = str((local_doc or {}).get("goods_sign") or ref.goods_sign or "").strip()
        return bool(
            _is_encrypted_jd_item_id(target_sign)
            or (target_sign.isdigit() and self._has_extra_providers("detail"))
        )

    def needs_coupon_upgrade(self, universal_result: dict, product: Optional[dict]) -> bool:
        return bool(
            isinstance(product, dict)
            and product.get("coupon_url")
            and product.get("product_url")
            and not universal_result.get("coupon_url")
        )

    def skip_prefetch_if_detail_checked(self, ref: ProductRef, item: Optional[dict]) -> Optional[float]:
        if (
            isinstance(item, dict)
            and item.get("detail_updated_at") is not None
            and (bool(item.get("coupon_url")) or float(item.get("coupon_amount") or 0.0) <= 0)
        ):
            return 86400.0
        return None

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
        sign = _text(goods_sign or goods_id_or_sign or goods_id)
        gid = clean_product_id(goods_id) or extract_canonical_goods_id(self.platform_code, sign)
        return ProductRef(self.platform_code, gid, sign)

    def _summary(self, item):
        """
        统一商品摘要映射入口（越简单越可靠 · 优惠券极简统一治理原则）：
          1. 单点统一提取：搜索/详情 (`coupon_id`) 与榜单 (`purchasePriceInfo.couponList`) 统一在此提取单件可用券；
          2. 只增不抹低耗：入库层对已知优惠券只增不抹（防无券榜单轮询覆盖），未核验商品全生命周期至多补查一次详情；
          3. 拒绝冗余清零：严禁为“券领完实时隐藏/清零”增加脆弱轮询，点击转链遇券失效自动无感降级普通推广链，不影响购买与锁佣。
        """
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
        coupon_link = jd_coupon_url(item.get("coupon_id"))
        if coupon <= 0 and not coupon_link and item.get("quanhou_jiage") in (None, ""):
            coupon, coupon_link = _rank_coupon(purchase, price)
            if coupon > 0:
                original = max(original if original is not None and original >= 0 else price, price)
                price = round(price - coupon, 2)
        image_url = _text(_pick(item.get("pict_url"), item.get("imageUrl")))
        canonical_id = extract_canonical_goods_id(self.platform_code, raw_id, image_url=image_url) or raw_id
        sales_candidates = (
            _text(item.get("volume")),
            _text(item.get("inOrderCount30Days")),
            _text(item.get("sellCount")),
            _text(item.get("comments")),
        )
        sales_tip = next((s for s in sales_candidates if s and s not in ("0", "0.0")), "0")
        data = {
            "platform": self.platform_code, "platform_name": self.platform_name,
            "goods_id": canonical_id, "goods_sign": raw_id,
            "name": name,
            "image_url": image_url,
            "price": price, "original_price": original if original is not None and original >= 0 else price,
            "coupon_amount": coupon, "has_coupon": coupon > 0,
            "commission_rate": _money(_pick(item.get("tkrate3"), item.get("commissionShare"))) or 0.0,
            "commission_amount": _money(_pick(item.get("tkfee3"), item.get("commission"))) or 0.0,
            "sales_tip": sales_tip,
            "mall_name": _text(item.get("shop_title")),
        }
        if coupon_link:
            data["coupon_url"] = coupon_link
        summary = ProductSummary(**data).to_dict(exclude_unset=True)
        material = _material_url(item)
        if material:
            summary["product_url"] = material
        return summary

    def _detail(self, item):
        data = self._summary(item)
        images = item.get("small_images") or item.get("imgList")
        images = images.split("|") if isinstance(images, str) else images if isinstance(images, list) else []
        images = list(dict.fromkeys(image.strip() for image in images if isinstance(image, str) and image.strip()))
        desc = _text(_pick(item.get("description"), item.get("jianjie")))
        specs = []
        seen_labels = set()
        for key, label in (
            ("brand", "商品品牌"),
            ("brandName", "商品品牌"),
            ("category_name", "商品类目"),
            ("cid1Name", "商品类目"),
            ("shop_title", "店铺名称"),
        ):
            val = _text(item.get(key))
            if val and label not in seen_labels:
                seen_labels.add(label)
                specs.append({"label": label, "value": val})
        ratings = {
            target: _text(item[source])
            for source, target in (("score1", "desc_score"), ("score2", "lgst_score"), ("score3", "serv_score"))
            if _text(item.get(source)) and _text(item.get(source)) not in ("0", "0.0")
        }
        tags = []
        coupon_str = _text(item.get("coupon_info"))
        if coupon_str:
            tags.append(coupon_str)
        elif data.get("coupon_amount", 0) > 0:
            tags.append(f"{data['coupon_amount']:g}元优惠券")
        coupon_info = {
            k: item[k]
            for k in ("coupon_start_time", "coupon_end_time", "coupon_total_count", "coupon_remain_count")
            if item.get(k) not in (None, "")
        }
        data.update(desc=desc, carousel_images=images,
                    product_url=_material_url(item), detail_tags=tags, service_guarantees=[],
                    shop_ratings=ratings, specs=specs, coupon_info=coupon_info)
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

        def _recommend_via_ztk():
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

        base = [("ztk_jd_rank", _recommend_via_ztk)] if self._has_ztk_catalog else []
        providers = self._compose_providers(
            "recommend", base, channel_type=channel, offset=offset, limit=limit,
            uid=uid, detail_sink=detail_sink, **kwargs,
        )
        res = self._execute_capability("recommend", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def search_goods(self, keyword, page=1, page_size=20, uid=None, **kwargs):
        unavailable = self._available("search")
        if unavailable:
            return {**unavailable, "data": []}

        def _search_via_ztk():
            result = self.client.search_goods(keyword, page=page, page_size=page_size)
            if result.get("status") != "success":
                return result
            try:
                data = [self._summary(item) for item in result["data"]]
            except (ValueError, TypeError, KeyError):
                return _error("PROTOCOL_ERROR", "京东搜索商品身份或字段无效", data=[])
            return {**result, "data": data}

        base = [("ztk_jd_quanwang", _search_via_ztk)] if self._has_ztk_catalog else []
        providers = self._compose_providers(
            "search", base, keyword=keyword, page=page, page_size=page_size, uid=uid, **kwargs,
        )
        res = self._execute_capability("search", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def _fetch_item(self, ref):
        target_id = ref.goods_sign or ref.goods_id
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东需要商品ID而非未经核验的请求URL")
        if is_jd_image_key(target_id):
            return _error("INVALID_ARGUMENT", "京东详情接口需要加密 itemId (goods_sign) 或数字 SKU，不能直接使用主图哈希 ID")
        result = self.client.get_goods_detail(target_id)
        if result.get("status") != "success":
            return result
        rows = result.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return _error("PROTOCOL_ERROR", "京东详情商品列表无效")
        matches = [item for item in rows if _item_id(item) == target_id]
        if not matches and len(rows) == 1:
            single = dict(rows[0])
            returned_img_key = extract_jd_image_key(_pick(single.get("pict_url"), single.get("imageUrl")))
            img_conflict = bool(
                is_jd_image_key(ref.goods_id)
                and returned_img_key
                and returned_img_key != ref.goods_id
                and not _verified_jd_cipher_pair(target_id, _item_id(single))
            )
            if _material_url(single) and not img_conflict:
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
        target_id = ref.goods_sign or ref.goods_id
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东需要商品ID而非未经核验的请求URL")
        if is_jd_image_key(target_id):
            return _error("INVALID_ARGUMENT", "京东详情接口需要加密 itemId (goods_sign) 或数字 SKU，不能直接使用主图哈希 ID")

        def _detail_via_ztk():
            result = self._fetch_item(ref)
            if result.get("status") != "success":
                if (result.get("ztk_ambiguous_301") is True
                        and _is_encrypted_jd_item_id(target_id)
                        and self.default_promoter_id):
                    # 【交叉探针裁决】：折淘客 api_detail.ashx 返回 {'status': 301, 'content': '对应宝贝已下架'} 存在二义性：
                    # 既可能是折淘客触发单品频控，也可能是商品真正取消了京东联盟推广。
                    # 通过官方转链接口探测 https://jingfen.jd.com/detail/{id}.html 即可精确裁决：
                    # - 若转链成功 (code=200)：说明商品仍在推广，301 纯属详情接口限流 -> 返回 RATE_LIMIT 以便备用源/缓存兜底；
                    # - 若京东联盟返回 2001104 (商品不在推广中)：确认已取消推广 -> 返回 PRODUCT_OFF_PROMOTION 触发全链路驱逐。
                    probe = self.client.convert_promotion_link(
                        f"https://jingfen.jd.com/detail/{target_id}.html",
                        self.default_promoter_id,
                    )
                    if probe.get("status") == "success":
                        short_url = probe.get("short_url") or probe.get("click_url") or ""
                        return _error(
                            "RATE_LIMIT",
                            "京东详情接口触发临时频控，商品仍在正常推广中",
                            probe_short_url=short_url,
                        )
                    if probe.get("jd_off_promotion") is True:
                        return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
                return result
            try:
                return {"status": "success", "data": self._detail(result["data"])}
            except (ValueError, TypeError):
                return _error("PROTOCOL_ERROR", "京东详情字段无效")

        base = [("ztk_jd_detail", _detail_via_ztk)] if self._has_ztk_catalog else []
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
        target_id = ref.goods_sign or ref.goods_id
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东转链缺少已识别商品ID")

        def _promote_via_ztk():
            # The service strips request kwargs before supplying these private bridges.
            material = product_url(kwargs.get("trusted_product_url"))
            clean_coupon = jd_coupon_url(kwargs.get("trusted_coupon_url"))
            # 【对齐 test_jd_api.py 实证基准】：
            # 加密 itemId 拼接为 https://item.jd.com/{id}.html 无法访问且转链必报 2001104 (商品不在推广中)；
            # 必须使用 https://jingfen.jd.com/detail/{id}.html 格式直接一步转链（若缓存中残留旧版 item.jd.com 拼接格式则自动纠正）。
            jingfen_encrypted_url = f"https://jingfen.jd.com/detail/{target_id}.html" if _is_encrypted_jd_item_id(target_id) else ""
            if jingfen_encrypted_url and (not material or material == f"https://item.jd.com/{target_id}.html"):
                material = jingfen_encrypted_url
            if not material:
                detail = self._fetch_item(ref)
                if detail.get("status") != "success":
                    return detail
                material = _material_url(detail["data"])
                if not clean_coupon:
                    clean_coupon = jd_coupon_url(detail["data"].get("coupon_id"))
            if not material:
                return _error("MATERIAL_URL_REQUIRED", "京东物料ID缺少已核验的商品链接")

            def _convert_call(mat_url, coup_url):
                if coup_url:
                    return self.client.convert_promotion_link(mat_url, promoter, coupon_url=coup_url)
                return self.client.convert_promotion_link(mat_url, promoter)

            result = _convert_call(material, clean_coupon)
            if result.get("status") != "success" and jingfen_encrypted_url and material == jingfen_encrypted_url:
                # 极端情况下若榜单混淆 itemId 需换取详情页重签名 tao_id，才回退调用一次 api_detail.ashx
                detail = self._fetch_item(ref)
                if detail.get("status") == "success":
                    detail_material = _material_url(detail["data"])
                    if not clean_coupon:
                        clean_coupon = jd_coupon_url(detail["data"].get("coupon_id"))
                    if detail_material and detail_material != material:
                        material = detail_material
                        try:
                            from cache import MATERIAL_CACHE
                            if ref.goods_sign:
                                MATERIAL_CACHE.set(("jd", "sign", ref.goods_sign), detail_material)
                            if ref.goods_id:
                                MATERIAL_CACHE.set(("jd", "id", ref.goods_id), detail_material)
                        except Exception:
                            pass
                        result = _convert_call(material, clean_coupon)
                elif result.get("jd_off_promotion") is True:
                    return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
            if result.get("status") != "success":
                if result.get("jd_off_promotion") is True:
                    return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
                return result
            extra_payload = {"coupon_url": clean_coupon} if clean_coupon else {}
            return self._build_promotion_payload(
                result,
                promoter=promoter,
                goods_id=ref.goods_id,
                goods_sign=ref.goods_sign,
                uid=uid,
                **extra_payload,
            )

        base = [("ztk_jd_byunionid", _promote_via_ztk)] if self._has_ztk_catalog else []
        promo_kw = {k: v for k, v in kwargs.items() if k not in ("goods_id", "goods_sign", "promoter_id")}
        providers = self._compose_providers(
            "promotion",
            base,
            goods_id_or_sign=goods_id_or_sign,
            uid=uid,
            goods_id=ref.goods_id,
            goods_sign=ref.goods_sign,
            promoter_id=promoter,
            **promo_kw,
        )
        return self._execute_capability("promotion", providers, allow_persist=self._is_real_http())

    def _build_promotion_payload(
        self,
        result: dict,
        *,
        promoter: str,
        goods_id: str = "",
        goods_sign: str = "",
        uid=None,
        **extra,
    ) -> dict:
        short = result.get("short_url") or ""
        long = result.get("click_url") or ""
        j_cmd = result.get("j_command") or ""
        j_short_cmd = result.get("j_short_command") or ""
        token = j_short_cmd or j_cmd
        return {
            "status": "success",
            "url": long or short,
            "final_link": short or long,
            "mobile_url": long or short,
            "short_url": short,
            "click_url": long,
            "j_command": j_cmd,
            "j_short_command": j_short_cmd,
            "promotion_token": token,
            "token_type": "jcommand" if token else "",
            "we_app_info": {},
            "promoter_id": promoter,
            "goods_id": goods_id,
            "goods_sign": goods_sign,
            "attribution_status": "verified" if uid else "anonymous",
            "need_auth": False,
            "is_fallback": False,
            "cacheable": False,
            "link_type": "京东推广链接",
            **extra,
        }

    def resolve_and_convert_universal(self, raw_text: str, uid=None, **kwargs):
        """
        京东万能转链与详情反查（对齐 scripts/test_jd_api.py 基准规范 六：直接原样透传折淘客，零模糊搜索兜底）：
        1. 直接将用户输入的原始文本透传给折淘客 `open_jing_union_open_promotion_byunionid_get.ashx` (`chainType=3`)，
           支持裸加密 itemId、jingfen.jd.com 详情链、u.jd.com 短链、union-click.jd.com 长链、京口令及社群混排长文案；
        2. 明确拦截纯数字 item.jd.com/{数字}.html 或纯数字 SKU，严禁在转链失败时提取文本标题调用 `search_goods` 模糊搜索取首条商品顶替；
        3. 转链成功后，通过 api_detail.ashx（加密ID）或 api_quanwang.ashx（转链所得 u.jd.com 短链，youquan=0）精准反查商品详情。
        """
        if not self.active:
            return _error("platform_unavailable", f"{self.platform_name}暂未启用")
        if not (self.capabilities.get("promotion") or self._has_extra_providers("universal")):
            return _error("CONFIG_ERROR", f"{self.platform_name}该功能缺少必要配置")
        promoter = canonical_promoter_id(kwargs.get("promoter_id") or self.default_promoter_id)
        if not promoter:
            return _error("INVALID_PROMOTER_ID", "京东转链需要服务端数字推广ID")

        text = _text(raw_text)
        if not text:
            return _error("INVALID_ARGUMENT", "请输入京东 u.jd.com 短链、京口令或京粉链接")

        def _do_resolve():
            extracted_encrypted_id = ""
            product_card = None
            detail_lookup_failed = False
            jf_match = re.search(r"jingfen\.jd\.com/detail/([A-Za-z0-9_]{20,})\.html", text)
            if jf_match:
                extracted_encrypted_id = jf_match.group(1)
            elif _is_encrypted_jd_item_id(text):
                extracted_encrypted_id = text

            material_input = f"https://jingfen.jd.com/detail/{text}.html" if _is_encrypted_jd_item_id(text) else text
            conv = self.client.convert_universal_material(material_input, promoter)

            if conv.get("status") != "success":
                if conv.get("jd_off_promotion") is True:
                    return _error("PRODUCT_OFF_PROMOTION", "该京东商品已取消推广或已下架，请挑选其他好物")
                return conv

            if not extracted_encrypted_id and conv.get("tao_id"):
                extracted_encrypted_id = _text(conv.get("tao_id"))

            short = conv.get("short_url") or ""
            long = conv.get("click_url") or ""
            j_cmd = conv.get("j_command") or ""

            resolved_id = extracted_encrypted_id
            if isinstance(conv.get("raw_item"), dict):
                try:
                    cand_card = self._detail(conv["raw_item"])
                    if (cand_card.get("price") or 0) > 0:
                        product_card = cand_card
                        resolved_id = product_card.get("goods_sign") or product_card.get("goods_id") or resolved_id
                except Exception:
                    pass
            if not product_card and extracted_encrypted_id:
                det = self.get_goods_detail(extracted_encrypted_id)
                if det.get("status") == "success" and isinstance(det.get("data"), dict):
                    product_card = det["data"]
                    resolved_id = product_card.get("goods_sign") or product_card.get("goods_id") or resolved_id
                elif det.get("error_code") in ("RATE_LIMIT", "UPSTREAM_ERROR", "NETWORK_ERROR"):
                    detail_lookup_failed = True
            lower_urls = f"{text} {short} {long}".lower()
            is_explicit_venue_url = is_jd_official_venue_url(lower_urls)
            verified_coupon_or_venue = bool(
                not extracted_encrypted_id
                and (is_explicit_venue_url or not j_cmd.strip())
            )

            if conv.get("ztk_catalog_throttled") or (product_card is None and not verified_coupon_or_venue):
                detail_lookup_failed = True

            fallback_title = "京东精选推广好物"
            b_match = re.search(r"「([^」]{2,})」", text)
            if b_match:
                fallback_title = b_match.group(1).strip()

            title = (product_card or {}).get("name") or fallback_title
            price = (product_card or {}).get("price")
            coupon_amount = (product_card or {}).get("coupon_amount")
            if coupon_amount is None:
                coupon_amount = 0.0

            canonical_id = (
                (product_card or {}).get("goods_id")
                or extract_canonical_goods_id(self.platform_code, resolved_id)
                or resolved_id
            )
            sign_id = (product_card or {}).get("goods_sign") or resolved_id

            extra_fields = {
                "title": title,
                "price": price,
                "coupon_amount": coupon_amount,
                "product": product_card,
            }
            if detail_lookup_failed and product_card is None:
                extra_fields["detail_lookup_failed"] = True

            payload = self._build_promotion_payload(
                conv,
                promoter=promoter,
                goods_id=canonical_id,
                goods_sign=sign_id,
                uid=uid,
                **extra_fields,
            )
            return stamp_universal_resolution(payload, verified_coupon_or_venue=verified_coupon_or_venue)

        base = [("ztk_jd_universal", _do_resolve)] if self._has_ztk_catalog else []
        univ_kw = {k: v for k, v in kwargs.items() if k != "promoter_id"}
        providers = self._compose_providers(
            "universal", base, raw_text=text, uid=uid, promoter_id=promoter, **univ_kw,
        )
        return self._execute_capability(
            "universal",
            providers,
            allow_persist=self._is_real_http(),
            metric_operation="resolve_universal",
        )

    def check_authority(self, uid):
        if not self.active:
            return _error("platform_unavailable", "京东暂未启用", is_authorized=False)
        return {"status": "not_required", "authorization_required": False, "is_authorized": False, "bind": 0}

    def generate_authority_url(self, uid, **kwargs):
        return self.check_authority(uid)

    def fetch_increment_orders(self, start_time, end_time, page=1, page_size=50, **kwargs):
        unavailable = self._available("orders")
        if unavailable:
            return {**unavailable, "order_list": [], "has_more": False, "next_cursor": None}

        def _orders_via_ztk():
            return self.client.query_orders(
                start_time, end_time, page=page, page_size=page_size, cursor=kwargs.get("cursor"),
            )

        base = [("ztk_jd_orders", _orders_via_ztk)] if self._has_ztk_orders else []
        providers = self._compose_providers(
            "orders", base, start_time=start_time, end_time=end_time, page=page, page_size=page_size, **kwargs,
        )
        result = self._execute_capability("orders", providers, allow_persist=self._is_real_http())
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
