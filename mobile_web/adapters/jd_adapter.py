# -*- coding: utf-8 -*-
"""京东字段/分页适配；无授权、用户仓储或个人转站长兜底。"""
import html
import re
import time
from typing import Any, Dict, Optional

from infrastructure.logging_utils import get_app_logger

logger = get_app_logger(__name__)
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
    JdClient, DEFAULT_API_BASE, ORDER_WINDOW_SECONDS,
    canonical_promoter_id, is_encrypted_jd_item_id, is_jd_official_venue_url, jd_coupon_url, product_url,
    resolve_jd_short_url,
)
from models.product import (
    JD_JINGFEN_ID_RE,
    ProductDetail,
    ProductRef,
    ProductSummary,
    URL_RE,
    clean_product_id,
    extract_canonical_goods_id,
    extract_jd_image_key,
    is_jd_image_key,
    normalize_jd_material_url,
)

_JD_SHORT_URL_RE = re.compile(r"https?://u\.jd\.com/[A-Za-z0-9_-]+")
_JD_UNION_CLICK_URL_RE = re.compile(r"https?://union-click\.jd\.com/[^\s<>'\"（）()【】]+")
_JD_NUMERIC_ITEM_URL_RE = re.compile(r"^https?://(?:item|item\.m)\.jd\.com/(?:product/)?(\d+)\.html(?:\?.*)?$", re.IGNORECASE)


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


_is_encrypted_jd_item_id = is_encrypted_jd_item_id


def _verified_jd_cipher_pair(requested_id, returned_id):
    """请求与上游返回均为标准京东联盟双段加密 itemId 时，证实由京东联盟解密自同一 SKU（兼容跨 Key 重签名与商家换主图）。"""
    return _is_encrypted_jd_item_id(requested_id) and _is_encrypted_jd_item_id(returned_id)


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
        super().__init__()
        if settings is None:
            from config import settings
        def setting(name, default=""):
            return overrides.get(name, getattr(settings, name, default))
        self.active = bool(setting("jd_enabled", False))
        self.default_promoter_id = canonical_promoter_id(setting("jd_position_id"))
        union_id = _text(setting("jd_union_id"))
        raw_appkeys = setting("ztk_appkey_list", ()) or setting("ztk_appkey", "")
        self.client = client if client is not None else JdClient(
            raw_appkeys, union_id, setting("jd_app_key"), setting("jd_app_secret"),
            setting("jd_api_base", DEFAULT_API_BASE))
        has_appkey = bool(getattr(self.client, "appkey_list", None) or setting("ztk_appkey") or setting("ztk_appkey_list"))
        client_jd_key = getattr(self.client, "app_key", None)
        client_jd_secret = getattr(self.client, "app_secret", None)
        has_client_official = (
            isinstance(client_jd_key, str) and bool(client_jd_key.strip())
            and isinstance(client_jd_secret, str) and bool(client_jd_secret.strip())
        )
        has_official_keys = bool((setting("jd_app_key") and setting("jd_app_secret")) or has_client_official)
        self._has_ztk_catalog = bool(client is not None or (has_appkey and canonical_promoter_id(union_id)))
        self._has_jd_official = has_official_keys
        self._has_ztk_orders = bool((client is not None and has_official_keys) or (has_appkey and has_official_keys))
        hdk_key = setting("hdk_api_key", "")
        hdk_keys = setting("hdk_api_key_list", ())
        has_hdk = bool(hdk_key or hdk_keys or overrides.get("hdk_client"))
        self.hdk = overrides.get("hdk_client") or (
            HdkClient(hdk_key, api_key_list=hdk_keys, union_id=union_id) if has_hdk else None
        )
        self._refresh_capabilities()

    def attach_hdk(self, hdk_client: HdkClient) -> None:
        """挂载好单库 (HDK) 作为京东搜索/详情/热榜/公共转链的有序多源通道，并刷新能力状态。"""
        self.hdk = hdk_client
        self._refresh_capabilities()

    @staticmethod
    def _guard_provider_result(res: Any) -> Dict[str, Any]:
        """当外部注入局部 Mock 客户端且某新增方法未配置时，视作 skipped 平滑顺延下一供应商。"""
        if not isinstance(res, dict):
            return {"status": "skipped", "reason": "unconfigured_provider_mock"}
        return res

    def _is_ztk_catalog_cooling(self) -> bool:
        """严格判断折淘客选品库是否处于冷却窗口（仅明确返回 True 时成立，避免 Mock 真值误判）。"""
        fn = getattr(getattr(self, "client", None), "is_catalog_cooling", None)
        return bool(callable(fn) and fn() is True)

    def _is_personal_promoter(self, uid: Any, promoter: str) -> bool:
        """好单库京东转链接口不接收 positionId，个人推手/非默认推广位必须交由折淘客保留完整归因。"""
        return bool(uid) or (bool(promoter) and promoter != self.default_promoter_id)

    def _normalize_hdk_summary(self, item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValueError("invalid hdk item")
        raw_itemid = _text(item.get("itemid"))
        raw_skuid = _text(item.get("skuid"))
        raw_sign = (
            raw_itemid if _is_encrypted_jd_item_id(raw_itemid)
            else (raw_skuid if _is_encrypted_jd_item_id(raw_skuid) else (raw_itemid or raw_skuid))
        )
        if not raw_sign:
            raise ValueError("missing goods identity")
        image_url = normalize_http_url(item.get("itempic") or item.get("white_image"))
        canonical_id = extract_canonical_goods_id(self.platform_code, raw_sign, image_url=image_url) or raw_sign
        end_p = clean_money(item.get("itemendprice"), None)
        orig_p = clean_money(item.get("itemprice"), None)
        price = end_p if (end_p is not None and end_p > 0) else orig_p
        if price is None or price < 0:
            raise ValueError("missing or invalid hdk price")
        original = orig_p if (orig_p is not None and orig_p >= price) else price
        raw_coupon = clean_money(item.get("couponmoney"), 0.0) or 0.0
        coupon_amt = raw_coupon
        # 好单库对京东折扣券常在 couponmoney 填折扣系数（如 0.95、0.8），需按价差纠正为真实优惠元数或门槛未抵扣时的 0
        if 0 < raw_coupon < 1.0:
            diff = round(original - price, 2)
            if diff > raw_coupon + 0.02:
                coupon_amt = diff
            elif diff <= 0.0:
                coupon_amt = 0.0
        coupon_url = jd_coupon_url(item.get("couponurl"))
        quota = clean_money(item.get("quota"), 0.0) or 0.0
        if not coupon_url or coupon_amt <= 0 or coupon_amt >= original or (quota > 0 and quota > original):
            coupon_amt = 0.0
        name = html.unescape(_text(item.get("goodsname")))
        if not name:
            raise ValueError("missing goods title")
        raw_rate = clean_money(_pick(item.get("commissionshare"), item.get("commissionShare")), 0.0) or 0.0
        # 好单库 jd_goods_search / jd_hot_rank 的 commissionshare 已是百分比（如 '1.00' = 1%），严禁再乘 100
        rate_percent = round(raw_rate, 2)
        comm_amount = clean_money(item.get("commission"), 0.0)
        if comm_amount is None or comm_amount <= 0:
            comm_amount = round(price * (rate_percent / 100.0), 2) if price and rate_percent > 0 else 0.0
        elif rate_percent <= 0 and price and price > 0:
            rate_percent = round((comm_amount / price) * 100, 2)
        else:
            comm_amount = round(comm_amount, 2)
        sales = _text(item.get("itemsale") or item.get("comments") or "0")
        mall = html.unescape(_text(item.get("shopname"))) or "京东店铺"
        brand = html.unescape(_text(item.get("brand_name"))) or "品牌甄选"
        cat_info = item.get("category_info") if isinstance(item.get("category_info"), dict) else {}
        cat = html.unescape(_text(cat_info.get("cid3Name") or cat_info.get("cid1Name"))) or "精选好物"

        if _is_encrypted_jd_item_id(raw_sign):
            mat_url = f"https://jingfen.jd.com/detail/{raw_sign}.html"
        elif raw_sign.isdigit() and raw_skuid.isdigit():
            mat_url = f"https://item.jd.com/{raw_skuid}.html"
        else:
            mat_url = normalize_jd_material_url(item.get("item_url") or "")

        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": canonical_id,
            "goods_sign": raw_sign,
            "name": name,
            "image_url": image_url,
            "price": price,
            "original_price": original,
            "coupon_amount": coupon_amt,
            "has_coupon": coupon_amt > 0 and bool(coupon_url),
            "commission_rate": rate_percent,
            "commission_amount": comm_amount,
            "sales_tip": sales,
            "mall_name": mall,
            "brand": brand,
            "category": cat,
        }
        if mat_url:
            data["product_url"] = mat_url
        if coupon_url:
            data["coupon_url"] = coupon_url
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _normalize_hdk_detail(self, item: dict) -> dict:
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

    @staticmethod
    def _norm_ts_seconds(val: Any) -> Optional[int]:
        parsed = clean_int(val)
        if parsed is None or parsed <= 0:
            return None
        return parsed // 1000 if parsed > 10_000_000_000 else parsed

    @classmethod
    def _extract_union_coupon(cls, item: dict, base_price: Optional[float]) -> tuple[float, str, dict]:
        """从京东联盟 goods.query / jingfen 结构中提取单件未过期的最优官方优惠券。"""
        if base_price is None or base_price <= 0:
            return 0.0, "", {}
        c_info = item.get("couponInfo") if isinstance(item.get("couponInfo"), dict) else {}
        p_info = item.get("purchasePriceInfo") if isinstance(item.get("purchasePriceInfo"), dict) else {}
        candidates = []
        for lst in (c_info.get("couponList"), p_info.get("couponList")):
            if isinstance(lst, list):
                candidates.extend(lst)
        now_s = int(time.time())
        best_disc, best_link, best_meta = 0.0, "", {}
        for entry in candidates:
            if not isinstance(entry, dict) or entry.get("couponStatus") not in (None, 0, "0"):
                continue
            remain = _money(entry.get("remainCnt"))
            if entry.get("remainCnt") not in (None, "") and (remain is None or remain <= 0):
                continue
            start_s = cls._norm_ts_seconds(entry.get("useStartTime") or entry.get("getStartTime"))
            end_s = cls._norm_ts_seconds(entry.get("useEndTime") or entry.get("getEndTime"))
            if end_s is not None and end_s < now_s:
                continue
            if start_s is not None and start_s > now_s:
                continue
            disc = _money(entry.get("discount"))
            raw_quota = entry.get("quota")
            quota = 0.0 if raw_quota in (None, "") else _money(raw_quota)
            link = jd_coupon_url(entry.get("link"))
            if link and disc is not None and quota is not None and disc >= 1.0 and disc < base_price and 0 <= quota <= base_price:
                if disc > best_disc:
                    best_disc = disc
                    best_link = link
                    best_meta = {
                        "coupon_amount": disc,
                        "coupon_url": link,
                        "quota": quota,
                        **({"coupon_start_time": start_s} if start_s else {}),
                        **({"coupon_end_time": end_s} if end_s else {}),
                    }
        return best_disc, best_link, best_meta

    def _resolve_union_pricing(self, item: dict) -> tuple[float, float, float, str, dict]:
        """统一计算京东联盟 goods.query / jingfen 的价格、原价与最优官方券，消除摘要与详情口径漂移。"""
        price_info = item.get("priceInfo") if isinstance(item.get("priceInfo"), dict) else {}
        purchase_info = item.get("purchasePriceInfo") if isinstance(item.get("purchasePriceInfo"), dict) else {}
        raw_lowest = _money(price_info.get("lowestPrice"))
        raw_price = _money(_pick(price_info.get("price"), purchase_info.get("price"), item.get("wlprice")))
        lowest_coupon_p = _money(price_info.get("lowestCouponPrice"))
        base_price = raw_lowest if (raw_lowest is not None and raw_lowest > 0) else raw_price
        if base_price is None:
            base_price = _money(_pick(raw_lowest, raw_price))
        if base_price is None or base_price < 0:
            raise ValueError("missing or invalid price")
        orig_price = raw_price if raw_price is not None else base_price
        coupon_base = orig_price if (lowest_coupon_p is not None and 0 < lowest_coupon_p < orig_price) else base_price
        coupon_amt, coupon_link, coupon_meta = self._extract_union_coupon(item, coupon_base)
        if coupon_amt > 0:
            if lowest_coupon_p is not None and 0 < lowest_coupon_p <= coupon_base:
                final_price = round(lowest_coupon_p, 2)
            else:
                final_price = round(max(0.0, coupon_base - coupon_amt), 2)
            original_price = max(orig_price, coupon_base)
        else:
            final_price = round(base_price, 2)
            original_price = round(max(orig_price, final_price), 2)
        return final_price, original_price, coupon_amt, coupon_link, coupon_meta

    def _normalize_union_goods_summary(self, item: dict) -> dict:
        """统一归一化京东联盟官方 goods.query / jingfen 返回格式（严格主图哈希解耦 + 正向实证券提取）。"""
        raw_id = _item_id(item)
        if not raw_id:
            raise ValueError("missing goods identity")
        name = html.unescape(_text(_pick(item.get("skuName"), item.get("title"), item.get("tao_title"))))
        if not name:
            raise ValueError("missing goods title")
        img_info = item.get("imageInfo") if isinstance(item.get("imageInfo"), dict) else {}
        gallery = []
        for entry in img_info.get("imageList") or []:
            u = normalize_http_url(entry.get("url") if isinstance(entry, dict) else entry)
            if u and u not in gallery:
                gallery.append(u)
        primary_img = gallery[0] if gallery else normalize_http_url(_pick(item.get("imageUrl"), item.get("pict_url")))
        if not primary_img:
            raise ValueError("missing goods image")

        final_price, original_price, coupon_amt, coupon_link, _ = self._resolve_union_pricing(item)

        comm_info = item.get("commissionInfo") if isinstance(item.get("commissionInfo"), dict) else {}
        comm_rate = _money(_pick(comm_info.get("commissionShare"), item.get("commissionShare"))) or 0.0
        raw_coupon_comm = _money(comm_info.get("couponCommission")) if coupon_amt > 0 else None
        comm_amt = _money(_pick(
            raw_coupon_comm if (raw_coupon_comm is not None and raw_coupon_comm > 0) else None,
            comm_info.get("commission"),
            item.get("commission"),
        )) or 0.0
        if comm_amt <= 0 and final_price > 0 and comm_rate > 0:
            comm_amt = round(final_price * comm_rate / 100.0, 2)

        shop_info = item.get("shopInfo") if isinstance(item.get("shopInfo"), dict) else {}
        cat_info = item.get("categoryInfo") if isinstance(item.get("categoryInfo"), dict) else {}
        mall_name = html.unescape(_text(_pick(shop_info.get("shopName"), item.get("shop_title")))) or "京东店铺"
        brand_name = html.unescape(_text(item.get("brandName"))) or "品牌甄选"
        cat_name = html.unescape(_text(_pick(cat_info.get("cid3Name"), cat_info.get("cid2Name"), cat_info.get("cid1Name")))) or "精选好物"

        sales_candidates = (
            _text(item.get("inOrderCount30Days")),
            _text(item.get("comments")),
            _text(item.get("volume")),
        )
        sales_tip = next((s for s in sales_candidates if s and s not in ("0", "0.0")), "0")
        canonical_id = extract_canonical_goods_id(self.platform_code, raw_id, image_url=primary_img) or raw_id
        explicit_item_url = product_url(item.get("item_url"))
        raw_mat = _text(item.get("materialUrl"))
        if explicit_item_url and "jingfen.jd.com/detail/" in explicit_item_url:
            mat_url = explicit_item_url
        elif _is_encrypted_jd_item_id(raw_id):
            mat_url = f"https://jingfen.jd.com/detail/{raw_id}.html"
        elif raw_mat:
            mat_url = normalize_jd_material_url("https://" + raw_mat if not raw_mat.startswith(("http://", "https://")) else raw_mat)
        else:
            mat_url = _material_url(item)

        data = {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "goods_id": canonical_id,
            "goods_sign": raw_id,
            "name": name,
            "image_url": primary_img,
            "price": final_price,
            "original_price": original_price,
            "coupon_amount": coupon_amt,
            "has_coupon": coupon_amt > 0 and bool(coupon_link),
            "commission_rate": round(comm_rate, 2),
            "commission_amount": round(comm_amt, 2),
            "sales_tip": sales_tip,
            "mall_name": mall_name,
            "brand": brand_name,
            "category": cat_name,
        }
        if mat_url:
            data["product_url"] = mat_url
        if coupon_link:
            data["coupon_url"] = coupon_link
        return ProductSummary(**data).to_dict(exclude_unset=True)

    def _normalize_union_goods_detail(self, item: dict) -> dict:
        summary_data = self._normalize_union_goods_summary(item)
        img_info = item.get("imageInfo") if isinstance(item.get("imageInfo"), dict) else {}
        gallery = []
        for entry in img_info.get("imageList") or []:
            u = normalize_http_url(entry.get("url") if isinstance(entry, dict) else entry)
            if u and u not in gallery:
                gallery.append(u)
        if summary_data.get("image_url") and summary_data["image_url"] not in gallery:
            gallery.insert(0, summary_data["image_url"])

        specs = []
        for label, val in (
            ("商品品牌", summary_data.get("brand")),
            ("商品类目", summary_data.get("category")),
            ("店铺名称", summary_data.get("mall_name")),
        ):
            if val:
                specs.append({"label": label, "value": val})

        shop_info = item.get("shopInfo") if isinstance(item.get("shopInfo"), dict) else {}
        ratings = {}
        for source, target in (("userEvaluateScore", "desc_score"), ("logisticsLvyueScore", "lgst_score"), ("afterServiceScore", "serv_score")):
            s_val = _text(shop_info.get(source))
            if s_val and s_val not in ("0", "0.0"):
                ratings[target] = s_val
        if not ratings and shop_info.get("shopLevel") not in (None, "", 0, "0"):
            ratings["desc_score"] = str(shop_info["shopLevel"])

        tags = []
        if summary_data.get("coupon_amount", 0) > 0:
            tags.append(f"{summary_data['coupon_amount']:g}元优惠券")
        for tag in item.get("skuTagList") or []:
            if isinstance(tag, dict) and tag.get("name"):
                t_name = _text(tag["name"])
                if t_name and t_name not in tags:
                    tags.append(t_name)
        p_info = item.get("purchasePriceInfo") if isinstance(item.get("purchasePriceInfo"), dict) else {}
        for pl in p_info.get("promotionLabelInfoList") or []:
            if isinstance(pl, dict) and pl.get("promotionLabel"):
                lbl = _text(pl["promotionLabel"])
                if lbl and lbl not in tags:
                    tags.append(lbl)

        _, _, _, _, coupon_meta = self._resolve_union_pricing(item)
        detail_payload = {
            **summary_data,
            "carousel_images": gallery,
            "detail_tags": tags,
            "service_guarantees": [],
            "shop_ratings": ratings,
            "specs": specs,
            "coupon_info": coupon_meta,
        }
        return ProductDetail(**detail_payload).to_dict(exclude_unset=True)

    def _refresh_capabilities(self) -> None:
        cat = getattr(self, "_has_ztk_catalog", False)
        off = getattr(self, "_has_jd_official", False)
        has_hdk = bool(getattr(self, "hdk", None))
        orders_ok = getattr(self, "_has_ztk_orders", False) or off
        self.capabilities = {
            "recommend": bool(cat or off or has_hdk or self._has_extra_providers("recommend")),
            "search": bool(cat or off or has_hdk or self._has_extra_providers("search")),
            "detail": bool(cat or off or has_hdk or self._has_extra_providers("detail")),
            "promotion": bool(((cat or has_hdk) and self.default_promoter_id) or self._has_extra_providers("promotion")),
            "universal": bool(((cat or has_hdk) and self.default_promoter_id) or self._has_extra_providers("universal")),
            "authorization": False,
            "orders": bool(orders_ok or self._has_extra_providers("orders")),
        }

    def _is_real_http(self) -> bool:
        return (
            is_real_requests_session(getattr(self.client, "http", None))
            or is_real_requests_session(getattr(self.hdk, "http", None))
        )

    def can_prefetch_detail(self, ref=None):
        """JD 榜单/搜索严禁后台盲刷详情预取，避免击穿好单库限额或级联跌入折淘客小时级千次配额。"""
        return False

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

        def _paged_rank(fetch_page_fn, normalizer_fn, *, skip_invalid_rows: bool = False):
            page, skip = offset // limit + 1, offset % limit
            items, total, more = [], None, False
            # Offset can straddle two upstream pages. Never publish a partial failed page.
            while len(items) < limit:
                result = self._guard_provider_result(fetch_page_fn(page, limit))
                if result.get("status") != "success":
                    return {**result, "data": []} if result.get("status") != "skipped" else result
                rows = result.get("data")
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    return _error("PROTOCOL_ERROR", "京东榜单商品列表无效", data=[])
                selected = rows[skip:skip + limit - len(items)]
                if skip_invalid_rows:
                    parsed_page = []
                    for item in selected:
                        try:
                            parsed_page.append(normalizer_fn(item))
                        except Exception:
                            continue
                    if selected and not parsed_page:
                        return _error("PROTOCOL_ERROR", "京东榜单商品身份或字段无效", data=[])
                    items.extend(parsed_page)
                else:
                    try:
                        items.extend(normalizer_fn(item) for item in selected)
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

        sort_val = {5: 1, 1: 3}[channel]
        elite_val = 22 if channel == 5 else 1
        base = []
        if self._has_ztk_catalog:
            base.append(("ztk_jd_rank", lambda: _paged_rank(
                lambda pg, sz: self.client.get_rank_goods(sort_val, page=pg, page_size=sz),
                self._summary,
            )))
        if self._has_ztk_catalog and hasattr(self.client, "query_jingfen_goods"):
            base.append(("ztk_jd_jingfen", lambda: _paged_rank(
                lambda pg, sz: self.client.query_jingfen_goods(elite_id=elite_val, page=pg, page_size=sz),
                self._normalize_union_goods_summary,
            )))
        if self.hdk and hasattr(self.hdk, "get_jd_hot_rank"):
            base.append(("hdk_jd_hot_rank", lambda: _paged_rank(
                lambda pg, sz: self.hdk.get_jd_hot_rank(page=pg, page_size=sz),
                self._normalize_hdk_summary,
                skip_invalid_rows=True,
            )))
        if not self._has_ztk_catalog and self._has_jd_official and hasattr(self.client, "get_rank_goods_direct"):
            base.append(("jd_official_rank", lambda: _paged_rank(
                lambda pg, sz: self.client.get_rank_goods_direct(sort_val, page=pg, page_size=sz),
                self._summary,
            )))

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

        def _wrap_search(raw_res, normalizer_fn, *, skip_invalid_rows: bool = False):
            res = self._guard_provider_result(raw_res)
            if res.get("status") != "success":
                return res
            raw_rows = res.get("data") or []
            if skip_invalid_rows:
                data = []
                for it in raw_rows:
                    try:
                        data.append(normalizer_fn(it))
                    except Exception:
                        continue
            else:
                try:
                    data = [normalizer_fn(it) for it in raw_rows]
                except (ValueError, TypeError, KeyError):
                    return _error("PROTOCOL_ERROR", "京东搜索商品身份或字段无效", data=[])
            return {**res, "data": data}

        base = []
        if self._has_ztk_catalog and hasattr(self.client, "query_union_goods"):
            base.append(("ztk_jd_goods_query", lambda: _wrap_search(
                self.client.query_union_goods(keyword=keyword, page=page, page_size=page_size),
                self._normalize_union_goods_summary,
            )))
        if self._has_ztk_catalog:
            base.append(("ztk_jd_quanwang", lambda: _wrap_search(
                self.client.search_goods(keyword, page=page, page_size=page_size),
                self._summary,
            )))
        if self.hdk is not None and hasattr(self.hdk, "search_goods"):
            base.append(("hdk_jd_search", lambda: _wrap_search(
                self.hdk.search_goods(keyword, page=page, page_size=page_size),
                self._normalize_hdk_summary,
                skip_invalid_rows=True,
            )))
        if not self._has_ztk_catalog and self._has_jd_official and hasattr(self.client, "query_union_goods_direct"):
            base.append(("jd_official_goods_query", lambda: _wrap_search(
                self.client.query_union_goods_direct(keyword=keyword, page=page, page_size=page_size),
                self._normalize_union_goods_summary,
            )))

        providers = self._compose_providers(
            "search", base, keyword=keyword, page=page, page_size=page_size, uid=uid, **kwargs,
        )
        res = self._execute_capability("search", providers, allow_persist=self._is_real_http())
        if res.get("status") != "success" and "data" not in res:
            res["data"] = []
        return res

    def _verify_detail_match(self, rows: Any, ref: ProductRef, target_id: str) -> Dict[str, Any]:
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return _error("PROTOCOL_ERROR", "京东详情商品列表无效")
        matches = [item for item in rows if _item_id(item) == target_id]
        if not matches and len(rows) == 1:
            single = dict(rows[0])
            img_url = _pick(single.get("pict_url"), single.get("imageUrl"))
            if not img_url and isinstance(single.get("imageInfo"), dict):
                imgs = single["imageInfo"].get("imageList") or []
                if imgs and isinstance(imgs[0], dict):
                    img_url = imgs[0].get("url")
            returned_img_key = extract_jd_image_key(img_url)
            fresh_id = _item_id(single)
            same_image_verified = bool(
                is_jd_image_key(ref.goods_id)
                and returned_img_key
                and returned_img_key == ref.goods_id
            )
            cipher_pair_verified = _verified_jd_cipher_pair(target_id, fresh_id)
            if _material_url(single) and (same_image_verified or cipher_pair_verified):
                if fresh_id and fresh_id != target_id and _is_encrypted_jd_item_id(fresh_id) and not single.get("item_url"):
                    single["item_url"] = f"https://jingfen.jd.com/detail/{fresh_id}.html"
                single["tao_id"] = target_id
                if not single.get("itemId"):
                    single["skuId"] = target_id
                matches = [single]
        if len(matches) != 1:
            return _error("PRODUCT_IDENTITY_MISMATCH", "京东详情未证实请求商品身份")
        return {"status": "success", "data": matches[0]}

    def _fetch_item(self, ref):
        target_id = ref.goods_sign or ref.goods_id
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东需要商品ID而非未经核验的请求URL")
        if is_jd_image_key(target_id):
            return _error("INVALID_ARGUMENT", "京东详情接口需要加密 itemId (goods_sign) 或数字 SKU，不能直接使用主图哈希 ID")
        result = self.client.get_goods_detail(target_id)
        if result.get("status") != "success":
            return result
        return self._verify_detail_match(result.get("data"), ref, target_id)

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

        ztk_rate_limit_res: Optional[Dict[str, Any]] = None

        def _detail_via_union_query(fetch_fn):
            nonlocal ztk_rate_limit_res
            if not _is_encrypted_jd_item_id(target_id):
                return {"status": "skipped", "reason": "union_goods_query_requires_encrypted_id"}
            raw_res = self._guard_provider_result(fetch_fn(target_id))
            if raw_res.get("status") != "success":
                if raw_res.get("error_code") == "RATE_LIMIT" or raw_res.get("catalog_cooling") is True:
                    ztk_rate_limit_res = ztk_rate_limit_res or raw_res
                return raw_res
            rows = raw_res.get("data") or []
            if not rows:
                return _error("NOT_FOUND", "京东联盟详情未命中该加密商品")
            matched = self._verify_detail_match(rows, ref, target_id)
            if matched.get("status") != "success":
                return matched
            try:
                return {"status": "success", "data": self._normalize_union_goods_detail(matched["data"])}
            except (ValueError, TypeError):
                return _error("PROTOCOL_ERROR", "京东联盟详情字段无效")

        def _detail_via_ztk():
            nonlocal ztk_rate_limit_res
            if "_" in target_id and len(target_id) >= 25 and not _is_encrypted_jd_item_id(target_id):
                return {"status": "skipped", "reason": "non_official_jd_item_id"}
            result = self._fetch_item(ref)
            if result.get("status") != "success":
                if (result.get("ztk_ambiguous_301") is True
                        and _is_encrypted_jd_item_id(target_id)
                        and self.default_promoter_id):
                    # 【交叉探针裁决】：折淘客 api_detail.ashx 返回 301 时，用官方转链探针区分限流 vs 停推。
                    probe = self.client.convert_promotion_link(
                        f"https://jingfen.jd.com/detail/{target_id}.html",
                        self.default_promoter_id,
                    )
                    if probe.get("status") == "success":
                        if hasattr(self.client, "mark_catalog_rate_limited"):
                            self.client.mark_catalog_rate_limited()
                        confirmed_rl = _error(
                            "RATE_LIMIT",
                            "京东详情接口触发临时频控，商品仍在正常推广中",
                            catalog_cooling=self._is_ztk_catalog_cooling() or True,
                        )
                        ztk_rate_limit_res = confirmed_rl
                        return confirmed_rl
                    if probe.get("jd_off_promotion") is True:
                        return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
                if result.get("error_code") == "RATE_LIMIT" or result.get("catalog_cooling") is True:
                    ztk_rate_limit_res = ztk_rate_limit_res or result
                return result
            try:
                return {"status": "success", "data": self._detail(result["data"])}
            except (ValueError, TypeError):
                return _error("PROTOCOL_ERROR", "京东详情字段无效")

        def _detail_via_hdk():
            if target_id.isdigit():
                return {"status": "skipped", "reason": "hdk_requires_encrypted_id"}
            res = self._guard_provider_result(self.hdk.get_goods_detail(target_id))
            if res.get("status") != "success":
                return res
            raw_item = res.get("data") or {}
            try:
                return {"status": "success", "data": self._normalize_hdk_detail(raw_item)}
            except Exception as exc:
                return _error("PROTOCOL_ERROR", f"好单库详情解析失败: {exc}")

        base = []
        if self._has_ztk_catalog and hasattr(self.client, "query_union_goods"):
            base.append(("ztk_jd_goods_query_detail", lambda: _detail_via_union_query(
                lambda tid: self.client.query_union_goods(item_ids=tid)
            )))
        if self._has_ztk_catalog:
            base.append(("ztk_jd_detail", _detail_via_ztk))
        if self.hdk is not None and hasattr(self.hdk, "get_goods_detail"):
            base.append(("hdk_jd_detail", _detail_via_hdk))
        if not self._has_ztk_catalog and self._has_jd_official and hasattr(self.client, "query_union_goods_direct"):
            base.append(("jd_official_goods_query_detail", lambda: _detail_via_union_query(
                lambda tid: self.client.query_union_goods_direct(item_ids=tid)
            )))

        providers = self._compose_providers(
            "detail",
            base,
            goods_id_or_sign=goods_id_or_sign,
            uid=uid,
            goods_id=ref.goods_id or goods_id,
            goods_sign=ref.goods_sign or goods_sign,
            **kwargs,
        )
        res = self._execute_capability("detail", providers, allow_persist=self._is_real_http())
        if (
            res.get("status") != "success"
            and ztk_rate_limit_res is not None
            and res.get("error_code") != "PRODUCT_OFF_PROMOTION"
        ):
            if (res.get("catalog_cooling") is True or self._is_ztk_catalog_cooling()) and not ztk_rate_limit_res.get("catalog_cooling"):
                return {**ztk_rate_limit_res, "catalog_cooling": True}
            return ztk_rate_limit_res
        return res

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
        is_personal = self._is_personal_promoter(uid, promoter)

        def _promote_via_ztk():
            material = product_url(kwargs.get("trusted_product_url"))
            is_non_official_hdk = (
                target_id.count("_") == 1
                and len(target_id) >= 35
                and not _is_encrypted_jd_item_id(target_id)
            )
            if is_non_official_hdk and (not material or target_id in material or _JD_NUMERIC_ITEM_URL_RE.match(material)):
                return {"status": "skipped", "reason": "non_official_jd_item_id"}
            clean_coupon = jd_coupon_url(kwargs.get("trusted_coupon_url"))
            refreshed_material_url = ""
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
                # 若榜单混淆 itemId 报 2001104，允许单次查详情看是否重签出了新 jingfen URL；若失败或未变化则确证停推
                detail = self._fetch_item(ref)
                if detail.get("status") == "success":
                    detail_material = _material_url(detail["data"])
                    if not clean_coupon:
                        clean_coupon = jd_coupon_url(detail["data"].get("coupon_id"))
                    if detail_material and detail_material != material:
                        material = detail_material
                        refreshed_material_url = detail_material
                        result = _convert_call(material, clean_coupon)
            if result.get("status") != "success":
                if result.get("jd_off_promotion") is True:
                    return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
                return result
            extra_payload = {}
            if clean_coupon and result.get("coupon_applied") is not False:
                extra_payload["coupon_url"] = clean_coupon
            if refreshed_material_url:
                extra_payload["resolved_material_url"] = refreshed_material_url
            return self._build_promotion_payload(
                result,
                promoter=promoter,
                goods_id=ref.goods_id,
                goods_sign=ref.goods_sign,
                uid=uid,
                **extra_payload,
            )

        def _promote_via_hdk():
            raw_trusted = product_url(kwargs.get("trusted_product_url"))
            if raw_trusted and raw_trusted.startswith("https://item.jd.com/") and raw_trusted.endswith(".html"):
                cand_id = raw_trusted[len("https://item.jd.com/"):-len(".html")]
                if _is_encrypted_jd_item_id(cand_id):
                    raw_trusted = f"https://jingfen.jd.com/detail/{cand_id}.html"
                elif cand_id.isdigit() and not target_id.isdigit():
                    raw_trusted = ""
            mat = raw_trusted or (
                f"https://jingfen.jd.com/detail/{target_id}.html" if _is_encrypted_jd_item_id(target_id) else target_id
            )
            coupon = jd_coupon_url(kwargs.get("trusted_coupon_url"))
            sub_id = kwargs.get("sub_union_id")
            used_coupon = bool(coupon)
            res = self._guard_provider_result(
                self.hdk.convert_promotion_link(mat, sub_union_id=sub_id, coupon_url=coupon)
            )
            if (
                coupon
                and res.get("status") != "success"
                and res.get("status") != "skipped"
                and res.get("error_code") not in ("RATE_LIMIT", "QUOTA_PACE_BACKLOG", "NETWORK_ERROR", "TIMEOUT", "CONFIG_ERROR")
            ):
                used_coupon = False
                res = self._guard_provider_result(
                    self.hdk.convert_promotion_link(mat, sub_union_id=sub_id)
                )
            if res.get("status") != "success":
                return res
            extra = {"coupon_url": coupon} if used_coupon else {}
            return self._build_promotion_payload(
                res,
                promoter=promoter,
                goods_id=ref.goods_id,
                goods_sign=ref.goods_sign,
                uid=uid,
                **extra,
            )

        base = []
        if self._has_ztk_catalog:
            base.append(("ztk_jd_byunionid", _promote_via_ztk))
        if self.hdk is not None and hasattr(self.hdk, "convert_promotion_link") and not is_personal:
            base.append(("hdk_jd_promotion", _promote_via_hdk))

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
            "cacheable": True,
            "link_type": "京东推广链接",
            **extra,
        }

    def _resolve_short_url_safe(self, short_url: str, *, allow_hdk: bool = True) -> Optional[Dict[str, Any]]:
        """轻量安全解包 u.jd.com 短链（仅在配置了 HDK 或真实 HTTP 连接池时执行，不干扰纯离线 Mock 测试）。"""
        if allow_hdk and self.hdk is not None and hasattr(self.hdk, "resolve_short_url"):
            try:
                res = self.hdk.resolve_short_url(short_url)
                if isinstance(res, dict):
                    return res
            except Exception:
                return None
        if is_real_requests_session(getattr(self.client, "http", None)):
            try:
                res = resolve_jd_short_url(short_url, http_session=self.client.http)
                if isinstance(res, dict):
                    return res
            except Exception:
                return None
        return None

    def resolve_product_from_link(self, raw_text: str, *, allow_hdk: bool = True, unwrap_fn: Any = None) -> Dict[str, Any]:
        """
        京东独立底层能力：按原始分享链接/短链/文案解析商品身份与官方详情卡（单向降级、零乒乓）：
        1. 拦截纯数字 SKU / item.jd.com/{digits}.html；
        2. 加密 itemId / jingfen.jd.com 详情链直查 `get_goods_detail`；
        3. 明确官方领券/活动会场链接直接正向实证 `is_venue=True` 止损；
        4. `u.jd.com` / `union-click.jd.com`：先走 118ms `ztk_jd_goods_query(keyword=url)` -> 再走 0 配额 `resolve_jd_short_url` -> 再走 HDK `jd_goods_search(url)`（已做 533 冷却熔断）。
        """
        text = _text(raw_text)
        if not text:
            return _error("INVALID_ARGUMENT", "请输入京东 u.jd.com 短链、京口令或京粉链接")
        if _JD_NUMERIC_ITEM_URL_RE.match(text) or text.isdigit():
            return _error(
                "UNSUPPORTED_JD_NUMERIC_SKU",
                "京东联盟接口已不支持纯数字商品ID转链，请粘贴 u.jd.com 短链、京口令或京粉链接",
            )

        target_id = ""
        jf_match = JD_JINGFEN_ID_RE.search(text)
        if jf_match:
            target_id = jf_match.group(1)
        elif _is_encrypted_jd_item_id(text):
            target_id = text

        short_match = _JD_SHORT_URL_RE.search(text)
        union_match = _JD_UNION_CLICK_URL_RE.search(text)
        union_url = union_match.group(0).rstrip(".,!?)];'\"") if union_match else ""
        anchor_url = short_match.group(0) if short_match else union_url

        is_venue = False
        venue_url = ""
        if not target_id and not anchor_url and is_jd_official_venue_url(text):
            cand_urls = [u.rstrip(".,!?)];'\"") for u in URL_RE.findall(text)]
            cand_venue = next((u for u in cand_urls if is_jd_official_venue_url(u)), "")
            cand_venue_base = cand_venue.split("#", 1)[0] if cand_venue else ""
            if cand_venue_base and product_url(cand_venue_base):
                sku_param_m = re.search(r"[?&](?:sku|mainSku|skuId)=([A-Za-z0-9_]+)", cand_venue_base, re.IGNORECASE)
                if sku_param_m:
                    target_id = sku_param_m.group(1)
                else:
                    is_venue = True
                    venue_url = cand_venue_base

        product_card = None
        detail_lookup_failed = False
        detail_catalog_cooling = False
        known_single_item = False
        is_numeric_sku = bool(target_id and target_id.isdigit())

        # 路径 1：已知加密商品 ID -> 直接走统一详情降级链
        if target_id and not is_numeric_sku:
            det = self.get_goods_detail(target_id)
            if isinstance(det, dict):
                if det.get("status") == "success" and isinstance(det.get("data"), dict):
                    product_card = det["data"]
                elif det.get("error_code") == "PRODUCT_OFF_PROMOTION":
                    return det
                elif det.get("error_code") in ("RATE_LIMIT", "UPSTREAM_ERROR", "NETWORK_ERROR"):
                    detail_lookup_failed = True
                    if det.get("catalog_cooling") is True:
                        detail_catalog_cooling = True

        # 路径 2：u.jd.com / union-click.jd.com -> 有序单向反查，绝不回头套娃
        if product_card is None and not is_venue and anchor_url and not (target_id and not is_numeric_sku):
            # 2a. 零配额短链轻量跳转/HDK 先行解包：识别会场/店铺主页直接止损，识别加密 sku 精准查卡，数字 SKU 不浪费详情配额
            unwrap = (
                unwrap_fn(anchor_url)
                if callable(unwrap_fn)
                else self._resolve_short_url_safe(anchor_url, allow_hdk=allow_hdk)
            )
            if isinstance(unwrap, dict) and unwrap.get("status") == "success" and isinstance(unwrap.get("data"), dict):
                ud = unwrap["data"]
                u_sku = _text(ud.get("sku_id"))
                if ud.get("is_venue") and not u_sku:
                    is_venue = True
                    venue_url = anchor_url
                elif u_sku:
                    target_id = u_sku
                    is_numeric_sku = u_sku.isdigit()
                    if is_numeric_sku:
                        known_single_item = True
                    else:
                        det = self.get_goods_detail(u_sku)
                        if isinstance(det, dict):
                            if det.get("status") == "success" and isinstance(det.get("data"), dict):
                                product_card = det["data"]
                            elif det.get("error_code") == "PRODUCT_OFF_PROMOTION":
                                return det
                            elif det.get("error_code") in ("RATE_LIMIT", "UPSTREAM_ERROR", "NETWORK_ERROR"):
                                detail_lookup_failed = True
                                if det.get("catalog_cooling") is True:
                                    detail_catalog_cooling = True

            # 2b. 折淘客 :20000 京东联盟官方全网查询直接支持 u.jd.com（118-243ms，不占选品库 1.35s 池；冷却期内跳过）
            if (
                product_card is None
                and not is_venue
                and not (target_id and not is_numeric_sku)
                and short_match
                and self._has_ztk_catalog
                and hasattr(self.client, "query_union_goods")
                and not self._is_ztk_catalog_cooling()
            ):
                uq = self._guard_provider_result(
                    self.client.query_union_goods(keyword=short_match.group(0), page=1, page_size=2)
                )
                uq_rows = uq.get("data") if (uq.get("status") == "success" and isinstance(uq.get("data"), list)) else []
                uq_single = uq_rows[0] if (
                    len(uq_rows) == 1
                    and isinstance(uq_rows[0], dict)
                    and not uq.get("has_more")
                    and uq.get("total_count") in (None, 1)
                ) else None
                if uq_single is not None and is_numeric_sku and target_id:
                    row_sku = _text(uq_single.get("skuId"))
                    if row_sku and row_sku != target_id:
                        uq_single = None
                if uq_single is not None:
                    try:
                        product_card = self._normalize_union_goods_detail(uq_single)
                        target_id = product_card.get("goods_sign") or product_card.get("goods_id") or target_id
                    except Exception:
                        product_card = None

            # 2c. 仍缺详情且未判定会场 -> 若配了 HDK 且支持详情，按原始链接查 1 次 HDK（allow_single_url_match=True）
            if (
                product_card is None
                and not is_venue
                and not (target_id and not is_numeric_sku)
                and allow_hdk
                and self.hdk is not None
                and hasattr(self.hdk, "get_goods_detail")
            ):
                hdk_kwargs = {"allow_single_url_match": True}
                if is_numeric_sku and target_id:
                    hdk_kwargs["expected_sku"] = target_id
                hdk_det = self._guard_provider_result(
                    self.hdk.get_goods_detail(anchor_url, **hdk_kwargs)
                )
                if hdk_det.get("status") == "success" and isinstance(hdk_det.get("data"), dict):
                    try:
                        product_card = self._normalize_hdk_detail(hdk_det["data"])
                        target_id = product_card.get("goods_sign") or product_card.get("goods_id") or target_id
                    except Exception:
                        product_card = None
                elif hdk_det.get("error_code") == "PRODUCT_OFF_PROMOTION":
                    return hdk_det

        mat_url = ""
        if venue_url:
            mat_url = venue_url
        elif short_match:
            mat_url = short_match.group(0)
        elif union_url:
            mat_url = union_url
        elif product_card and product_card.get("product_url"):
            mat_url = product_url(product_card["product_url"])
        elif target_id and _is_encrypted_jd_item_id(target_id):
            mat_url = f"https://jingfen.jd.com/detail/{target_id}.html"

        return {
            "status": "success",
            "is_venue": is_venue,
            "venue_url": venue_url,
            "product": product_card,
            "target_id": target_id,
            "is_numeric_sku": bool(target_id and target_id.isdigit()),
            "known_single_item": known_single_item or bool(product_card),
            "anchor_url": anchor_url,
            "material_url": mat_url,
            "detail_lookup_failed": detail_lookup_failed,
            "detail_catalog_cooling": detail_catalog_cooling,
        }

    def _convert_material_url_with_fallback(
        self,
        material_url: str,
        promoter: str,
        *,
        is_personal: bool,
        clean_coupon: str = "",
        goods_id: str = "",
        goods_sign: str = "",
        uid: Any = None,
        sub_union_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """已知物料链接的统一多源转链辅助（折淘客 byunionid_get 主通道 -> 好单库 get_jditems_link 公共兜底）。"""
        last_err: Optional[Dict[str, Any]] = None
        hdk_eligible = (
            not is_personal
            and self.hdk is not None
            and hasattr(self.hdk, "convert_promotion_link")
        )
        prefer_hdk_during_cooldown = self._is_ztk_catalog_cooling() and hdk_eligible

        def _try_ztk():
            nonlocal last_err
            ztk_res = (
                self.client.convert_promotion_link(material_url, promoter, coupon_url=clean_coupon)
                if clean_coupon
                else self.client.convert_promotion_link(material_url, promoter)
            )
            if isinstance(ztk_res, dict) and ztk_res.get("status") == "success":
                extra = {"coupon_url": clean_coupon} if (clean_coupon and ztk_res.get("coupon_applied") is not False) else {}
                payload = self._build_promotion_payload(
                    ztk_res, promoter=promoter, goods_id=goods_id, goods_sign=goods_sign, uid=uid, **extra,
                )
                payload["_provider"] = "ztk_jd_byunionid"
                return payload
            if isinstance(ztk_res, dict) and ztk_res.get("jd_off_promotion") is True:
                return _error("PRODUCT_OFF_PROMOTION", "该京东商品已取消推广或已下架，请挑选其他好物")
            if isinstance(ztk_res, dict):
                last_err = ztk_res
            return None

        def _try_hdk():
            nonlocal last_err
            used_coupon = bool(clean_coupon)
            hdk_res = self._guard_provider_result(
                self.hdk.convert_promotion_link(material_url, sub_union_id=sub_union_id, coupon_url=clean_coupon)
            )
            if (
                clean_coupon
                and hdk_res.get("status") != "success"
                and hdk_res.get("status") != "skipped"
                and hdk_res.get("error_code") not in ("RATE_LIMIT", "QUOTA_PACE_BACKLOG", "NETWORK_ERROR", "TIMEOUT", "CONFIG_ERROR")
            ):
                used_coupon = False
                hdk_res = self._guard_provider_result(
                    self.hdk.convert_promotion_link(material_url, sub_union_id=sub_union_id)
                )
            if hdk_res.get("status") == "success":
                extra = {"coupon_url": clean_coupon} if used_coupon else {}
                payload = self._build_promotion_payload(
                    hdk_res, promoter=promoter, goods_id=goods_id, goods_sign=goods_sign, uid=uid, **extra,
                )
                payload["_provider"] = "hdk_jd_promotion"
                return payload
            if hdk_res.get("status") != "skipped":
                last_err = hdk_res
            return None

        if prefer_hdk_during_cooldown:
            res = _try_hdk()
            if res is not None:
                return res
            if self._has_ztk_catalog:
                res = _try_ztk()
                if res is not None:
                    return res
        else:
            if self._has_ztk_catalog:
                res = _try_ztk()
                if res is not None:
                    return res
            if hdk_eligible:
                res = _try_hdk()
                if res is not None:
                    return res

        return last_err or _error("PROMOTION_FAILED", "暂未能生成京东推广链接")

    def resolve_and_convert_universal(self, raw_text: str, uid=None, **kwargs):
        """
        京东万能转链与详情反查（组合 `resolve_product_from_link` + 多源推广转链，彻底消灭跨供应商乒乓）：
        1. 先由 `resolve_product_from_link` 单向有序解析会场实证或完整商品卡片；
        2. 明确会场或已解析单品：直接二合一推广转链（折淘客 `byunionid_get` -> 公共兜底好单库 `get_jditems_link`）；
        3. 纯京口令/混合文案或未出卡短链：原样透传 `byunionid_get` + `tkl_piliang` (+ 必要时 `api_quanwang`) 兜底；
        4. 始终输出确定性四态 `resolution_kind`（未拿卡未知单品严格标记 `degraded_product`）。
        """
        if not self.active:
            return _error("platform_unavailable", f"{self.platform_name}暂未启用")
        if not (self.capabilities.get("promotion") or self._has_extra_providers("universal")):
            return _error("CONFIG_ERROR", f"{self.platform_name}该功能缺少必要配置")
        promoter = canonical_promoter_id(kwargs.get("promoter_id")) if uid else canonical_promoter_id(
            kwargs.get("promoter_id") or self.default_promoter_id
        )
        if not promoter:
            return _error("INVALID_PROMOTER_ID", "京东转链需要服务端数字推广ID")

        text = _text(raw_text)
        if not text:
            return _error("INVALID_ARGUMENT", "请输入京东 u.jd.com 短链、京口令或京粉链接")

        is_personal = self._is_personal_promoter(uid, promoter)

        def _do_resolve():
            if self._is_ztk_catalog_cooling() and not is_jd_official_venue_url(text):
                return _error(
                    "RATE_LIMIT",
                    "京东选品库处于短时频控冷却中，暂跳过避免空转",
                    catalog_cooling=True,
                )
            unwrap_memo: Dict[str, Any] = {}

            def _memo_unwrap(u: str):
                if u in unwrap_memo:
                    return unwrap_memo[u]
                r = self._resolve_short_url_safe(u, allow_hdk=not is_personal)
                if r is not None:
                    unwrap_memo[u] = r
                return r

            resolved = self.resolve_product_from_link(text, allow_hdk=not is_personal, unwrap_fn=_memo_unwrap)
            if resolved.get("status") != "success":
                return resolved

            is_confirmed_venue = bool(resolved.get("is_venue"))
            product_card = resolved.get("product")
            extracted_encrypted_id = _text(resolved.get("target_id")) if not resolved.get("is_numeric_sku") else ""
            anchor_url = _text(resolved.get("anchor_url"))
            detail_lookup_failed = bool(resolved.get("detail_lookup_failed"))
            detail_catalog_cooling = bool(resolved.get("detail_catalog_cooling"))

            fallback_title = "京东精选活动会场" if is_confirmed_venue else "京东精选推广好物"
            b_match = re.search(r"「([^」]{2,})」", text)
            if b_match and not is_confirmed_venue:
                fallback_title = b_match.group(1).strip()

            # A. 明确会场/官方领券页：直接正向转链，标 COUPON_OR_VENUE，不再浪费详情/piliang 配额
            if is_confirmed_venue and resolved.get("material_url"):
                conv = self._convert_material_url_with_fallback(
                    resolved["material_url"],
                    promoter,
                    is_personal=is_personal,
                    uid=uid,
                    sub_union_id=kwargs.get("sub_union_id"),
                )
                if conv.get("status") != "success":
                    return conv
                conv.update(
                    title=(product_card or {}).get("name") or fallback_title,
                    price=None,
                    coupon_amount=0.0,
                    product=None,
                    convert_status="coupon_or_activity",
                    detail_lookup_failed=False,
                )
                return stamp_universal_resolution(conv, verified_coupon_or_venue=True)

            # B. 已经从链接成功解析出官方商品卡片：优先保留原短链/权威物料链，绝不用纯数字 item.jd.com 覆盖 u.jd.com
            if product_card is not None:
                card_url = product_url(product_card.get("product_url"))
                if card_url and _JD_NUMERIC_ITEM_URL_RE.match(card_url):
                    card_url = ""
                mat_url = resolved.get("material_url") or card_url or anchor_url
                if mat_url and _JD_NUMERIC_ITEM_URL_RE.match(mat_url) and anchor_url:
                    mat_url = anchor_url
                used_coupon = jd_coupon_url(product_card.get("coupon_url"))
                if mat_url:
                    conv = self._convert_material_url_with_fallback(
                        mat_url,
                        promoter,
                        is_personal=is_personal,
                        clean_coupon=used_coupon,
                        goods_id=product_card.get("goods_id") or "",
                        goods_sign=product_card.get("goods_sign") or extracted_encrypted_id,
                        uid=uid,
                        sub_union_id=kwargs.get("sub_union_id"),
                    )
                    if conv.get("status") == "success":
                        conv.update(
                            title=product_card.get("name") or fallback_title,
                            price=product_card.get("price"),
                            coupon_amount=product_card.get("coupon_amount", 0.0),
                            product=product_card,
                        )
                        return stamp_universal_resolution(conv)
                    if conv.get("error_code") == "PRODUCT_OFF_PROMOTION":
                        return conv

            # C. 裸京口令 / 混排文案 / 前置均未出卡的短链：走 ZTK 万能解析转链通道（含 byunionid + tkl_piliang + api_quanwang）
            conv = None
            hdk_mat = anchor_url or resolved.get("material_url")
            can_hdk_fallback = (
                not is_personal
                and self.hdk is not None
                and hasattr(self.hdk, "convert_promotion_link")
                and bool(hdk_mat)
            )
            if self._has_ztk_catalog:
                if self._is_ztk_catalog_cooling() and not is_jd_official_venue_url(text):
                    if not can_hdk_fallback:
                        return _error(
                            "RATE_LIMIT",
                            "京东选品库处于短时频控冷却中，暂跳过避免空转",
                            catalog_cooling=True,
                        )
                else:
                    material_input = f"https://jingfen.jd.com/detail/{text}.html" if _is_encrypted_jd_item_id(text) else text
                    short_resolver = (
                        (lambda u: _memo_unwrap(u) or _error("NOT_FOUND", "解包失败"))
                        if ((not is_personal and self.hdk is not None) or is_real_requests_session(getattr(self.client, "http", None)))
                        else None
                    )
                    conv = (
                        self.client.convert_universal_material(material_input, promoter, short_url_resolver=short_resolver)
                        if short_resolver is not None
                        else self.client.convert_universal_material(material_input, promoter)
                    )
                    if isinstance(conv, dict) and conv.get("status") != "success" and conv.get("jd_off_promotion") is True:
                        return _error("PRODUCT_OFF_PROMOTION", "该京东商品已取消推广或已下架，请挑选其他好物")

            if (
                (not isinstance(conv, dict) or conv.get("status") != "success")
                and can_hdk_fallback
            ):
                hdk_conv = self._guard_provider_result(
                    self.hdk.convert_promotion_link(hdk_mat, sub_union_id=kwargs.get("sub_union_id"))
                )
                if hdk_conv.get("status") == "success":
                    cooling_flag = detail_catalog_cooling or self._is_ztk_catalog_cooling() or (
                        isinstance(conv, dict) and conv.get("catalog_cooling") is True
                    )
                    payload = self._build_promotion_payload(
                        hdk_conv,
                        promoter=promoter,
                        goods_id=extracted_encrypted_id or "",
                        goods_sign=extracted_encrypted_id or "",
                        uid=uid,
                        title=fallback_title,
                        price=None,
                        coupon_amount=0.0,
                        product=None,
                        detail_lookup_failed=True,
                        **({"catalog_cooling": True} if cooling_flag else {}),
                    )
                    payload["_provider"] = "hdk_jd_universal"
                    return stamp_universal_resolution(payload)

            if not isinstance(conv, dict) or conv.get("status") != "success":
                return conv if isinstance(conv, dict) else _error("UPSTREAM_ERROR", "京东万能转链失败")

            if conv.get("piliang_throttled"):
                detail_lookup_failed = True
            detail_catalog_cooling = detail_catalog_cooling or bool(conv.get("catalog_cooling") is True)
            if not extracted_encrypted_id and conv.get("tao_id"):
                extracted_encrypted_id = _text(conv.get("tao_id"))

            short = conv.get("short_url") or ""
            long = conv.get("click_url") or ""
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
                elif det.get("error_code") == "PRODUCT_OFF_PROMOTION":
                    return det
                elif det.get("error_code") in ("RATE_LIMIT", "UPSTREAM_ERROR", "NETWORK_ERROR"):
                    detail_lookup_failed = True
                    if det.get("catalog_cooling") is True:
                        detail_catalog_cooling = True

            lower_urls = f"{text} {short} {long}".lower()
            is_explicit_venue_url = is_jd_official_venue_url(lower_urls) or bool(conv.get("resolved_venue"))
            short_u_match = _JD_SHORT_URL_RE.search(short or "")

            is_cooling = self._is_ztk_catalog_cooling()
            if not product_card and not is_explicit_venue_url and not detail_lookup_failed and not is_cooling and short_u_match:
                qw = self.client.search_goods(short_u_match.group(0), page=1, page_size=5, has_coupon=False)
                rows = qw.get("data") if isinstance(qw, dict) and qw.get("status") == "success" else None
                if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
                    row = dict(rows[0])
                    if isinstance(row.get("small_images"), str):
                        row["small_images"] = [
                            seg.strip()
                            for seg in row["small_images"].split("|")
                            if seg.strip().startswith(("http://", "https://", "//"))
                        ]
                    try:
                        cand_card = self._detail(row)
                        if (cand_card.get("price") or 0) >= 0 and (cand_card.get("goods_id") or cand_card.get("goods_sign")):
                            product_card = cand_card
                            resolved_id = product_card.get("goods_sign") or product_card.get("goods_id") or resolved_id
                    except Exception:
                        pass
                if (
                    product_card is None
                    and conv.get("piliang_empty_tao_id")
                    and conv.get("known_single_item")
                    and hasattr(self.client, "mark_catalog_rate_limited")
                ):
                    self.client.mark_catalog_rate_limited()

            verified_coupon_or_venue = bool(
                not extracted_encrypted_id
                and is_explicit_venue_url
            )
            if product_card is None and not verified_coupon_or_venue:
                detail_lookup_failed = True

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
                if detail_catalog_cooling:
                    extra_fields["catalog_cooling"] = True

            payload = self._build_promotion_payload(
                conv,
                promoter=promoter,
                goods_id=canonical_id,
                goods_sign=sign_id,
                uid=uid,
                **extra_fields,
            )
            return stamp_universal_resolution(payload, verified_coupon_or_venue=verified_coupon_or_venue)

        base = [("ztk_jd_universal", _do_resolve)]
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

        def _orders_via_direct():
            return self._guard_provider_result(self.client.query_orders_direct(
                start_time, end_time, page=page, page_size=page_size, cursor=kwargs.get("cursor"),
            ))

        def _orders_via_ztk():
            return self._guard_provider_result(self.client.query_orders(
                start_time, end_time, page=page, page_size=page_size, cursor=kwargs.get("cursor"),
            ))

        base = []
        if self._has_jd_official and hasattr(self.client, "query_orders_direct"):
            base.append(("jd_official_orders", _orders_via_direct))
        if self._has_ztk_orders:
            base.append(("ztk_jd_orders", _orders_via_ztk))
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
