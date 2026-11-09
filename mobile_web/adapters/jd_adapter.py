# -*- coding: utf-8 -*-
"""京东字段/分页适配；无授权、用户仓储或个人转站长兜底。"""
import hashlib
import math
import re
from datetime import datetime

from adapters.base import BasePlatformAdapter
from infrastructure.http_utils import (
    clean_text as _text,
    error_dict as _error,
    parse_finite_money as _money,
    parse_shanghai_timestamp as _timestamp,
)
from infrastructure.jd_client import (
    JdClient, DEFAULT_API_BASE, ORDER_WINDOW_SECONDS, SHANGHAI,
    canonical_promoter_id, product_url,
)
from models.product import (
    ProductDetail,
    ProductRef,
    ProductSummary,
    clean_product_id,
    extract_canonical_goods_id,
    extract_jd_image_key,
    extract_search_keyword,
    is_jd_image_key,
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

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
        sign = _text(goods_sign or goods_id_or_sign or goods_id)
        gid = clean_product_id(goods_id) or extract_canonical_goods_id(self.platform_code, sign)
        return ProductRef(self.platform_code, gid, sign)

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
            img_conflict = bool(is_jd_image_key(ref.goods_id) and returned_img_key and returned_img_key != ref.goods_id)
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
        result = self._fetch_item(ref)
        if result.get("status") != "success":
            target_id = ref.goods_sign or ref.goods_id
            if (result.get("ztk_ambiguous_301") is True
                    and _is_encrypted_jd_item_id(target_id)
                    and self.default_promoter_id):
                # 【交叉探针裁决】：折淘客 api_detail.ashx 返回 {'status': 301, 'content': '对应宝贝已下架'} 存在二义性：
                # 既可能是折淘客触发单品频控，也可能是商品真正取消了京东联盟推广。
                # 通过官方转链接口探测 https://jingfen.jd.com/detail/{id}.html 即可精确裁决：
                # - 若转链成功 (code=200)：说明商品仍在推广，301 纯属详情接口限流 -> 返回 RATE_LIMIT 以便上层启用本地缓存兜底；
                # - 若京东联盟返回 2001104 (商品不在推广中)：确认已取消推广 -> 返回 PRODUCT_OFF_PROMOTION 触发全链路驱逐。
                probe = self.client.convert_promotion_link(
                    f"https://jingfen.jd.com/detail/{target_id}.html",
                    self.default_promoter_id,
                )
                if probe.get("status") == "success":
                    return _error("RATE_LIMIT", "京东详情接口触发临时频控，商品仍在正常推广中")
                if probe.get("jd_off_promotion") is True:
                    return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
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
        target_id = ref.goods_sign or ref.goods_id
        if not target_id or "://" in target_id or target_id.startswith("//"):
            return _error("INVALID_ARGUMENT", "京东转链缺少已识别商品ID")
        # The service strips request kwargs before supplying this private bridge.
        material = product_url(kwargs.get("trusted_product_url"))
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
        if not material:
            return _error("MATERIAL_URL_REQUIRED", "京东物料ID缺少已核验的商品链接")
        result = self.client.convert_promotion_link(material, promoter)
        if result.get("status") != "success" and jingfen_encrypted_url and material == jingfen_encrypted_url:
            # 极端情况下若榜单混淆 itemId 需换取详情页重签名 tao_id，才回退调用一次 api_detail.ashx
            detail = self._fetch_item(ref)
            if detail.get("status") == "success":
                detail_material = _material_url(detail["data"])
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
                    result = self.client.convert_promotion_link(material, promoter)
            elif result.get("jd_off_promotion") is True:
                return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
        if result.get("status") != "success":
            if result.get("jd_off_promotion") is True:
                return _error("PRODUCT_OFF_PROMOTION", "该商品已取消推广或已下架，请挑选其他好物")
            return result
        return self._build_promotion_payload(
            result,
            promoter=promoter,
            goods_id=ref.goods_id,
            goods_sign=ref.goods_sign,
            uid=uid,
        )

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
        unavailable = self._available("promotion")
        if unavailable:
            return unavailable
        promoter = canonical_promoter_id(kwargs.get("promoter_id") or self.default_promoter_id)
        if not promoter:
            return _error("INVALID_PROMOTER_ID", "京东转链需要服务端数字推广ID")

        text = _text(raw_text)
        if not text:
            return _error("INVALID_ARGUMENT", "请输入京东 u.jd.com 短链、京口令或京粉链接")

        extracted_encrypted_id = ""
        product_card = None
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
        if not product_card:
            search_q = short or long
            is_coupon_or_venue_url = any(
                kw in (search_q or "").lower()
                for kw in ("coupon.m.jd.com", "pro.m.jd.com", "prodev.m.jd.com", "h5.m.jd.com", "jdcoupon")
            )
            if search_q and not is_coupon_or_venue_url:
                try:
                    q_res = self.client.search_goods(search_q, page=1, page_size=5, has_coupon=False)
                except TypeError:
                    q_res = self.client.search_goods(search_q, page=1, page_size=5)
                if q_res.get("status") == "success" and isinstance(q_res.get("data"), list) and q_res["data"]:
                    rows = q_res["data"]
                    total_cnt = int(q_res.get("total_count") or len(rows))
                    # 【遵循绝对可靠原则】：仅当转链所得短链 100% 唯一命中单件商品 (len(rows)==1 且 total_cnt==1) 时采纳，
                    # 严禁因口令含“抢购”字样就从多商品会场列表中盲取首条！
                    if len(rows) == 1 and total_cnt == 1:
                        try:
                            cand_card = self._detail(rows[0])
                            if (cand_card.get("price") or 0) > 0:
                                product_card = cand_card
                                resolved_id = product_card.get("goods_sign") or product_card.get("goods_id") or ""
                        except Exception:
                            pass

        # 从 jCommand 文案提取商品标题与优惠券金额作为限流兜底
        fallback_title = "京东精选推广好物"
        fallback_coupon = 0.0
        if j_cmd:
            t_match = re.search(r"抢购(.+?)(?:[！!]|$)", j_cmd)
            if t_match:
                fallback_title = t_match.group(1).strip()
            c_match = re.search(r"领(\d+(?:\.\d+)?)元优惠券", j_cmd)
            if c_match:
                try:
                    fallback_coupon = round(float(c_match.group(1)), 2)
                except Exception:
                    pass
        if fallback_title == "京东精选推广好物":
            b_match = re.search(r"「(.+?)」", text)
            if b_match:
                fallback_title = b_match.group(1).strip()

        title = (product_card or {}).get("name") or fallback_title
        price = (product_card or {}).get("price")
        coupon_amount = (product_card or {}).get("coupon_amount")
        if coupon_amount is None:
            coupon_amount = fallback_coupon

        canonical_id = (
            (product_card or {}).get("goods_id")
            or extract_canonical_goods_id(self.platform_code, resolved_id)
            or resolved_id
        )
        sign_id = (product_card or {}).get("goods_sign") or resolved_id

        return self._build_promotion_payload(
            conv,
            promoter=promoter,
            goods_id=canonical_id,
            goods_sign=sign_id,
            uid=uid,
            title=title,
            price=price,
            coupon_amount=coupon_amount,
            product=product_card,
        )

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
