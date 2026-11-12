# -*- coding: utf-8 -*-
"""
商品数据仓储模块 (MongoDB: products 集合)
负责商品数据的高效读写、索引保障与爆品库检索。
"""
import re
import datetime
import math
import time
from copy import deepcopy
from typing import Dict, Any, List, Optional, Tuple
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError
from bson.decimal128 import Decimal128
from repositories.base_repo import BaseRepository
from infrastructure.logging_utils import format_exc_summary
from models.product import (
    _BARE_YUAN_TAG_RE,
    clean_product_id,
    extract_canonical_goods_id,
    extract_search_keyword,
    is_jd_image_key,
    normalize_platform,
    resolve_item_identity,
    utc_millis,
)
from config import PROMOTION_LINK_TTL, logger


SKU_UNIT_BASE_MAPPING = {
    "质量": {
        "mg": 1,
        "g": 1000,
        "克": 1000,
        "斤": 500000,
        "kg": 1000000,
        "Kg": 1000000,
        "KG": 1000000,
        "千克": 1000000,
        "磅": 453592.37,
    },
    "容积与体积": {
        "ml": 1,
        "mL": 1,
        "ML": 1,
        "毫升": 1,
        "L": 1000,
        "升": 1000,
    },
    "数据存储": {
        "G": 1,
        "GB": 1,
        "TB": 1024,
    },
    "电池容量": {
        "mAh": 1,
        "Ah": 1000,
        "AH": 1000,
    },
    "功率": {
        "W": 1,
        "kW": 1000,
    },
    "生物活性成分": {
        "iu": 1,
        "IU": 1,
    },
    "速率与排量": {
        "L/日": 1,
        "升/天": 1,
    },
}

UNIT_TO_CATEGORY: Dict[str, str] = {}
UNIT_TO_MULTIPLIER: Dict[str, float] = {}
for _cat, _units in SKU_UNIT_BASE_MAPPING.items():
    for _u, _mul in _units.items():
        UNIT_TO_CATEGORY[_u] = _cat
        UNIT_TO_MULTIPLIER[_u] = _mul


class ProductRepository(BaseRepository):
    COLLECTION_NAME = "products"
    _MAX_COST_PERFORMANCE_CANDIDATES = 2000

    def _init_indexes(self):
        try:
            self.col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
            self.col.create_index("goods_sign", background=True)
            self.col.create_index([("updated_at", -1)], background=True)
            self.col.create_index([("platform", 1), ("updated_at", -1)], background=True)
            # 价格正/反序共用同一索引；全站与单平台查询分别保留排序前缀。
            self.col.create_index([("activity_price", 1)], background=True)
            self.col.create_index([("platform", 1), ("activity_price", 1)], background=True)
            self.col.create_index([("saved_price", -1), ("updated_at", -1)], background=True)
            self.col.create_index([("platform", 1), ("saved_price", -1), ("updated_at", -1)], background=True)
            return True
        except Exception as e:
            logger.warning(
                "❌ 【数据库索引告警】创建商品集合索引时发生异常 | 集合[products] 异常<%s> 排查方向<请检查MongoDB连接状态或products集合(platform+product_id)唯一索引冲突>",
                format_exc_summary(e),
            )
            return False

    _platform = staticmethod(normalize_platform)
    _SUMMARY_PROJECTION = {
        "_id": 0,
        **dict.fromkeys((
            "platform", "product_id", "goods_id", "goods_sign", "name", "brand",
            "category", "activity_price", "price", "original_price", "saved_price",
            "coupon_amount", "has_coupon", "commission_rate", "commission_amount",
            "sales_tip", "image_url", "mall_name", "updated_at", "product_url",
        ), 1),
    }
    _COST_PERFORMANCE_PROJECTION = {
        **_SUMMARY_PROJECTION,
        "format_info": 1,
        "format_status": 1,
        "sku_info": 1,
        "need_reformat": 1,
        "format_activity_price": 1,
        "format_sku_prices": 1,
    }
    _PLAIN_SKU_PRICE_RE = re.compile(r"^[￥¥]?\s*(\d+(?:\.\d+)?)\s*$")

    @staticmethod
    def _displayable_content_filter() -> Dict[str, Any]:
        """在 Mongo 分页前排除身份裸记录；数值字符串等旧金额仍可展示，真 0 有效。"""
        return {
            "name": {"$regex": r"\S"},
            "$expr": {
                "$let": {
                    "vars": {
                        "display_price": {
                            "$convert": {
                                "input": {"$ifNull": ["$activity_price", "$price"]},
                                "to": "double", "onError": None, "onNull": None,
                            },
                        },
                    },
                    "in": {"$and": [
                        # 查询级 $type 会匹配字符串数组，表达式级类型才保证 name 是标量。
                        {"$eq": [{"$type": "$name"}, "string"]},
                        {"$in": [
                            {"$type": {"$ifNull": ["$activity_price", "$price"]}},
                            ["double", "int", "long", "decimal", "string"],
                        ]},
                        {"$gte": ["$$display_price", 0]},
                        {"$lt": ["$$display_price", float("inf")]},
                    ]},
                },
            },
        }

    @classmethod
    def _boom_content_filter(cls) -> Dict[str, Any]:
        """爆单推广与类目聚合仅读取已入库的榜单商品或已清洗线报商品，排除搜索/详情散查裸记录。"""
        content_filter = deepcopy(cls._displayable_content_filter())
        content_filter["$or"] = [
            {"channel_type": {"$exists": True, "$ne": None}},
            {"deal_info.is_deal_tip": True},
        ]
        content_filter["$expr"]["$let"]["in"]["$and"].append({
            "$or": [
                {"$in": [{"$type": "$channel_type"}, ["int", "long"]]},
                {"$eq": ["$deal_info.is_deal_tip", True]},
            ]
        })
        return content_filter

    @staticmethod
    def _first_present(data: Dict[str, Any], *keys, default=None):
        """缺失/None 才回退；0、False、空集合是已知值。"""
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
        return default

    _real_goods_id = staticmethod(clean_product_id)

    @classmethod
    def _identity(cls, item: Dict[str, Any]) -> Tuple[str, str, str]:
        return resolve_item_identity(item, allow_jd_sign_desalt=True)

    @staticmethod
    def _identity_filter(platform: str, goods_id: str, goods_sign: str) -> Dict[str, Any]:
        if goods_id and goods_sign and goods_id != goods_sign:
            return {"platform": platform, "$or": [{"product_id": goods_id}, {"goods_sign": goods_sign}]}
        target = goods_id or goods_sign
        return {"platform": platform, "$or": [{"product_id": target}, {"goods_sign": target}]}

    @classmethod
    def _identity_update(cls, platform: str, goods_id: str, goods_sign: str):
        update_set = {"platform": platform}
        insert_set = {}
        if goods_id:
            update_set["product_id"] = goods_id
        elif goods_sign:
            insert_set["product_id"] = goods_sign

        if goods_sign:
            if platform == "jd" and is_jd_image_key(goods_sign):
                insert_set["goods_sign"] = goods_sign
            else:
                update_set["goods_sign"] = goods_sign
        elif platform != "pdd" and goods_id:
            insert_set["goods_sign"] = goods_id

        return update_set, insert_set

    @staticmethod
    def _amount_float(value: Any) -> float:
        """兼容既有数值、数值字符串及 BSON Decimal128 金额。"""
        return float(value.to_decimal() if isinstance(value, Decimal128) else value)

    @classmethod
    def _calc_commission(cls, activity_price: float, raw_commission: Any) -> Tuple[float, float]:
        """只计算已知佣金，不为缺失值或真实 0 佣金虚构补贴。"""
        act_price = cls._amount_float(activity_price if activity_price is not None else 0.0)
        commission_amount = round(cls._amount_float(raw_commission if raw_commission is not None else 0.0), 2)
        commission_rate = round((commission_amount / act_price) * 100, 1) if act_price > 0 else 0.0
        return commission_rate, commission_amount

    def _build_product_update(self, item: Dict[str, Any], *, detail: bool, channel_type=None):
        """摘要与详情共用金额/身份映射；只更新本次实际提供的字段。"""
        platform, goods_id, goods_sign = self._identity(item)
        if not goods_id and not goods_sign:
            return None
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        update_set, insert_set = self._identity_update(platform, goods_id, goods_sign)
        insert_set["created_at"] = now

        # 标准 DTO 与原始清洗字典共用同一存储布局，金额单位仍为元。
        price = self._first_present(item, "activity_price", "price", "min_group_price_yuan")
        original_price = self._first_present(item, "original_price")
        if price is not None:
            update_set["activity_price"] = float(price)
            if original_price is None:
                insert_set["price"] = float(price)
                insert_set["original_price"] = float(price)
        if original_price is not None:
            update_set["price"] = float(original_price)
            update_set["original_price"] = float(original_price)

        coupon = self._first_present(item, "coupon_amount", "saved_price", "coupon_discount_yuan")
        if coupon is not None:
            update_set["saved_price"] = float(coupon)
            update_set["coupon_amount"] = float(coupon)
            update_set["has_coupon"] = bool(item["has_coupon"]) if item.get("has_coupon") is not None else float(coupon) > 0
        elif item.get("has_coupon") is not None:
            update_set["has_coupon"] = bool(item["has_coupon"])

        commission = self._first_present(item, "commission_amount", "estimated_commission_yuan")
        if commission is not None:
            update_set["commission_amount"] = float(commission)
        if item.get("commission_rate") is not None:
            update_set["commission_rate"] = float(item["commission_rate"])

        text_fields = {
            "name": ("name", "goods_name"),
            "sales_tip": ("sales_tip",),
            "image_url": ("image_url", "goods_image_url", "goods_thumbnail_url"),
            "brand": ("brand_name", "brand"),
            "category": ("category_name", "category"),
            "mall_name": ("mall_name",),
        }
        for target, aliases in text_fields.items():
            value = next((item[key] for key in aliases if key in item and item[key] is not None and str(item[key]).strip()), None)
            if value is not None:
                text_val = str(value).strip() if target == "image_url" else str(value)
                if target == "image_url" and text_val.startswith("//"):
                    text_val = "https:" + text_val
                update_set[target] = text_val

        # 供应商摘要也可带可信物料链接，但不是完整详情，不推进详情来源时钟。
        # 对于京东加密 ID，统一规范化为 https://jingfen.jd.com/detail/{encrypted_id}.html；榜单摘要仅在首次插入时写入，避免覆盖详情页已解析出的长物料链接。
        product_url = item.get("product_url")
        if isinstance(product_url, str) and product_url.strip():
            clean_url = product_url.strip()
            target_id = goods_sign or goods_id
            if platform == "jd" and len(target_id) >= 25 and "_" in target_id and not is_jd_image_key(target_id):
                if clean_url == f"https://item.jd.com/{target_id}.html":
                    clean_url = f"https://jingfen.jd.com/detail/{target_id}.html"
                if not detail and clean_url == f"https://jingfen.jd.com/detail/{target_id}.html":
                    insert_set["product_url"] = clean_url
                else:
                    update_set["product_url"] = clean_url
            else:
                update_set["product_url"] = clean_url

        update_set["updated_at"] = now
        if item.get("source") is not None:
            update_set["source"] = item["source"]
        elif not detail:
            update_set["source"] = f"{platform}_recommend_stream"
        if not detail and isinstance(channel_type, int) and not isinstance(channel_type, bool):
            update_set["channel_type"] = channel_type

        if detail:
            detail_updated_at = item.get("detail_updated_at")
            if not isinstance(detail_updated_at, datetime.datetime):
                detail_updated_at = datetime.datetime.now(datetime.timezone.utc)
            elif detail_updated_at.tzinfo is None:
                # 与 Mongo 默认解码一致：该字段的 naive datetime 契约是 UTC。
                detail_updated_at = detail_updated_at.replace(tzinfo=datetime.timezone.utc)
            update_set["detail_updated_at"] = detail_updated_at.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            for key in (
                "desc", "carousel_images", "specs", "detail_tags",
                "unified_tags", "service_guarantees", "shop_ratings", "coupon_info", "search_id",
            ):
                if key in item and item[key] is not None:
                    if key == "carousel_images" and isinstance(item[key], list):
                        cleaned_carousel = []
                        for img_item in item[key]:
                            if not isinstance(img_item, str) or not img_item.strip():
                                continue
                            u = img_item.strip()
                            if u.startswith("//"):
                                u = "https:" + u
                            if u not in cleaned_carousel:
                                cleaned_carousel.append(u)
                        if cleaned_carousel:
                            update_set["carousel_images"] = cleaned_carousel
                        else:
                            insert_set["carousel_images"] = []
                    else:
                        update_set[key] = deepcopy(item[key])
        return self._identity_filter(platform, goods_id, goods_sign), {"$set": update_set, "$setOnInsert": insert_set}

    def _build_boom_filter_query(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        filter_query: Dict[str, Any] = {}
        if platform and self._platform(platform) != "all":
            filter_query["platform"] = self._platform(platform)
        if category and category.strip() and category != "全部":
            filter_query["category"] = category.strip()
        if keyword and keyword.strip():
            raw_kw = keyword.strip()
            cleaned_kw = extract_search_keyword(raw_kw, max_length=60) or raw_kw[:60]
            safe_kw = re.escape(cleaned_kw)
            filter_query["name"] = {"$regex": safe_kw, "$options": "i"}
        return filter_query

    @staticmethod
    def _is_specless_single_sku(sku_info: Any) -> bool:
        """判定商品是否属于无细分规格属性的单品（成交价统一以顶层 activity_price 为准）。"""
        if not isinstance(sku_info, list) or len(sku_info) == 0:
            return True
        if len(sku_info) == 1:
            item = sku_info[0]
            if not isinstance(item, dict):
                return True
            return not any(
                k not in ("价格", "price", "截图地址", "image_url", "img") and str(v or "").strip()
                for k, v in item.items()
            )
        return False

    @classmethod
    def _parse_plain_sku_price(cls, price_text: Any) -> Optional[float]:
        """解析纯数值规格价格（如 '￥41.4' / 41.4）；区间或多档文案返回 None。"""
        if price_text is None or isinstance(price_text, bool):
            return None
        if isinstance(price_text, (int, float, Decimal128)):
            try:
                val = cls._amount_float(price_text)
                return val if math.isfinite(val) and val > 0 else None
            except (ValueError, TypeError, OverflowError):
                return None
        match = cls._PLAIN_SKU_PRICE_RE.fullmatch(str(price_text).strip())
        if not match:
            return None
        try:
            val = float(match.group(1))
            return val if math.isfinite(val) and val > 0 else None
        except (ValueError, OverflowError):
            return None

    @classmethod
    def _is_valid_sku_price(cls, price_val: Any, min_limit_price: Optional[float], *, exact_match: bool = False) -> bool:
        """
        校验规格价格有效性：
        1. 必须为大于 0 的有限浮点数/整数/Decimal128；
        2. 单规格商品（exact_match=True）下，若存在外层 activity_price，规格价格必须与外层当前价格一致；
        3. 多规格模式下，任何规格价格不得低于全局 activity_price。
        """
        if isinstance(price_val, bool) or not isinstance(price_val, (int, float, Decimal128)):
            return False
        try:
            p_float = cls._amount_float(price_val)
        except (ValueError, TypeError, OverflowError):
            return False
        if not math.isfinite(p_float) or p_float <= 0:
            return False
        if min_limit_price is not None:
            if exact_match:
                if abs(round(p_float, 2) - round(float(min_limit_price), 2)) > 0.01:
                    return False
            elif round(p_float, 2) < round(float(min_limit_price), 2):
                return False
        return True

    @classmethod
    def _extract_sku_unit_entries(cls, doc: Dict[str, Any]) -> List[Tuple[float, str, Dict[str, float]]]:
        """
        从商品的 format_info 中提取所有合法规格的 (sku_price, raw_base_unit, extracted_units)。
        尚未完成 format_info 清洗、标记 need_reformat 或外层价格已变但尚未重洗的商品返回空列表（归入末尾兜底组）。
        """
        if doc.get("need_reformat") is True:
            return []
        if "format_status" in doc and doc.get("format_status") not in (None, "success"):
            return []
        raw_format_info = doc.get("format_info")
        if not isinstance(raw_format_info, dict) or not raw_format_info:
            return []

        raw_act = cls._first_present(doc, "activity_price", "price")
        min_act_price = None
        if raw_act is not None and not isinstance(raw_act, bool):
            try:
                act_f = cls._amount_float(raw_act)
                if math.isfinite(act_f) and act_f > 0:
                    min_act_price = float(act_f)
            except (ValueError, TypeError, OverflowError):
                min_act_price = None

        sku_info_raw = doc.get("sku_info")
        is_specless = cls._is_specless_single_sku(sku_info_raw)

        raw_fmt_act = doc.get("format_activity_price")
        if is_specless and min_act_price is not None and raw_fmt_act is not None and not isinstance(raw_fmt_act, bool):
            try:
                fmt_act_f = cls._amount_float(raw_fmt_act)
            except (ValueError, TypeError, OverflowError):
                return []
            if not math.isfinite(fmt_act_f) or fmt_act_f <= 0 or abs(min_act_price - fmt_act_f) > 0.01:
                return []

        raw_sku_list: List[Tuple[float, Dict[str, Any]]] = []
        if "pricing_basis" in raw_format_info and isinstance(raw_format_info.get("pricing_basis"), dict):
            sku_price = raw_format_info.get("price")
            if cls._is_valid_sku_price(sku_price, min_act_price, exact_match=is_specless):
                raw_sku_list.append((cls._amount_float(sku_price), raw_format_info))
        else:
            sku_info_list = sku_info_raw if isinstance(sku_info_raw, list) else []
            product_id = str(doc.get("product_id") or doc.get("goods_id") or doc.get("goods_sign") or "").strip()
            stale_sku_ids = set()
            if not is_specless and sku_info_list and product_id:
                snap_map = doc.get("format_sku_prices") if isinstance(doc.get("format_sku_prices"), dict) else {}
                for idx, sku_item in enumerate(sku_info_list, start=1):
                    if not isinstance(sku_item, dict):
                        continue
                    sku_id = f"{product_id}_{idx}"
                    has_cn_price = "价格" in sku_item and sku_item.get("价格") is not None
                    cur_p_str = str(sku_item.get("价格") or "").strip() if has_cn_price else ""
                    if sku_id in snap_map and has_cn_price:
                        if str(snap_map[sku_id]).strip() != cur_p_str:
                            stale_sku_ids.add(sku_id)
                            continue
                    sku_fmt_entry = raw_format_info.get(sku_id)
                    if isinstance(sku_fmt_entry, dict):
                        cur_num = (
                            cls._parse_plain_sku_price(cur_p_str)
                            if has_cn_price
                            else cls._parse_plain_sku_price(sku_item.get("price"))
                        )
                        if cur_num is not None:
                            try:
                                fmt_p = cls._amount_float(sku_fmt_entry.get("price"))
                            except (ValueError, TypeError, OverflowError):
                                fmt_p = None
                            if fmt_p is None or not math.isfinite(fmt_p) or fmt_p <= 0 or abs(cur_num - fmt_p) > 0.01:
                                stale_sku_ids.add(sku_id)

            if sku_info_list and product_id:
                for idx in range(1, len(sku_info_list) + 1):
                    sku_id = f"{product_id}_{idx}"
                    if sku_id in stale_sku_ids:
                        continue
                    sku_fmt = raw_format_info.get(sku_id)
                    if not isinstance(sku_fmt, dict) or not isinstance(sku_fmt.get("pricing_basis"), dict):
                        continue
                    sku_price = sku_fmt.get("price")
                    if cls._is_valid_sku_price(sku_price, min_act_price, exact_match=is_specless):
                        raw_sku_list.append((cls._amount_float(sku_price), sku_fmt))

            if not raw_sku_list:
                for sku_key, sku_fmt in raw_format_info.items():
                    if sku_key in stale_sku_ids:
                        continue
                    if isinstance(sku_fmt, dict) and isinstance(sku_fmt.get("pricing_basis"), dict):
                        sku_price = sku_fmt.get("price")
                        if cls._is_valid_sku_price(sku_price, min_act_price, exact_match=is_specless):
                            raw_sku_list.append((cls._amount_float(sku_price), sku_fmt))

        entries: List[Tuple[float, str, Dict[str, float]]] = []
        for sku_price, sku_fmt in raw_sku_list:
            pricing_basis = sku_fmt.get("pricing_basis") or {}
            raw_structure = pricing_basis.get("structure")
            raw_base_unit = str(pricing_basis.get("base_unit") or "").strip() or "件"
            raw_total_val = pricing_basis.get("total_value")

            total_val_float = None
            if raw_total_val is not None and not isinstance(raw_total_val, bool):
                try:
                    tv = float(raw_total_val)
                    if math.isfinite(tv) and tv > 0:
                        total_val_float = tv
                except (ValueError, TypeError):
                    total_val_float = None

            extracted_units: Dict[str, float] = {}
            current_accum = 1.0
            if isinstance(raw_structure, list) and raw_structure:
                for struct_item in raw_structure:
                    if not isinstance(struct_item, dict):
                        continue
                    u = str(struct_item.get("unit") or "").strip()
                    v = struct_item.get("value")
                    val_float = 1.0
                    if v is not None and not isinstance(v, bool):
                        try:
                            vf = float(v)
                            if math.isfinite(vf) and vf > 0:
                                val_float = vf
                        except (ValueError, TypeError):
                            val_float = 1.0
                    current_accum *= val_float
                    if u:
                        extracted_units[u] = current_accum

            if raw_base_unit:
                final_val = total_val_float if total_val_float is not None else current_accum
                extracted_units[raw_base_unit] = final_val

            if not extracted_units:
                extracted_units["件"] = 1.0

            entries.append((sku_price, raw_base_unit, extracted_units))

        return entries

    def rank_boom_products_by_cost_performance(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询爆单库候选商品并按性价比排序：
        1. 提取各商品合法规格的单位结构，按同类度量衡出现频次统一目标单位（如 kg/斤/克 -> g）；
        2. 多 SKU 商品取其合法规格中性价比最高的主规格参与排序；
        3. 按出现最多的单位优先、同单位内性价比从高到低排序，第二多的单位次之，依此类推；
        4. 尚未完成 format_info 清洗的商品按最新上架顺序排在末尾用作兜底。
        """
        filter_query = self._build_boom_filter_query(platform=platform, category=category, keyword=keyword)
        cursor = (
            self.col.find(
                {"$and": [filter_query, self._boom_content_filter()]},
                self._COST_PERFORMANCE_PROJECTION,
            )
            .sort([("updated_at", -1)])
            .limit(self._MAX_COST_PERFORMANCE_CANDIDATES)
            .max_time_ms(2000)
        )
        raw_docs = list(cursor)

        formatted_candidates: List[Tuple[Dict[str, Any], List[Tuple[float, str, Dict[str, float]]]]] = []
        unformatted_items: List[Dict[str, Any]] = []
        category_unit_counts: Dict[str, Dict[str, int]] = {}

        for doc in raw_docs:
            summary = self._normalize_doc_summary(doc)
            sku_entries = self._extract_sku_unit_entries(doc)
            if not sku_entries:
                unformatted_items.append(summary)
                continue

            formatted_candidates.append((summary, sku_entries))
            for _, _, extracted_units in sku_entries:
                for u in extracted_units.keys():
                    cat = UNIT_TO_CATEGORY.get(u)
                    if cat:
                        if cat not in category_unit_counts:
                            category_unit_counts[cat] = {}
                        category_unit_counts[cat][u] = category_unit_counts[cat].get(u, 0) + 1

        category_target_unit: Dict[str, str] = {}
        for cat, counts in category_unit_counts.items():
            category_target_unit[cat] = max(counts.items(), key=lambda x: x[1])[0]

        # 1. 收集所有商品的主单位集合 (active_base_units) 及每个商品在各单位下的最高性价比得分
        product_unit_scores: List[Tuple[str, Dict[str, float]]] = []
        active_base_units = set()

        for idx, (summary, sku_entries) in enumerate(formatted_candidates):
            unit_best_scores: Dict[str, float] = {}
            best_primary_unit = "件"
            best_primary_score = -1.0

            for sku_price, raw_base_unit, extracted_units in sku_entries:
                normalized_units: Dict[str, float] = {}
                for u, val in extracted_units.items():
                    cat = UNIT_TO_CATEGORY.get(u)
                    final_u = u
                    final_val = val
                    if cat and cat in category_target_unit:
                        tgt_u = category_target_unit[cat]
                        if u != tgt_u:
                            orig_mul = UNIT_TO_MULTIPLIER[u]
                            tgt_mul = UNIT_TO_MULTIPLIER[tgt_u]
                            final_val = val * (orig_mul / tgt_mul)
                        final_u = tgt_u
                    normalized_units[final_u] = round(final_val, 4) if isinstance(final_val, float) else final_val

                cat = UNIT_TO_CATEGORY.get(raw_base_unit)
                primary_base_unit = category_target_unit[cat] if (cat and cat in category_target_unit) else raw_base_unit
                if primary_base_unit not in normalized_units:
                    primary_base_unit = next(iter(normalized_units.keys()), "件")
                active_base_units.add(primary_base_unit)

                for u, raw_val in normalized_units.items():
                    val_num = float(raw_val) if raw_val and raw_val > 0 else 1.0
                    score = round(val_num / sku_price, 6) if sku_price and sku_price > 0 else 0.0
                    if score > unit_best_scores.get(u, -1.0):
                        unit_best_scores[u] = score
                    if u == primary_base_unit and score > best_primary_score:
                        best_primary_score = score
                        best_primary_unit = primary_base_unit

            product_unit_scores.append((best_primary_unit, unit_best_scores))

        # 2. 将商品放入其支持的各个主单位分组中（同一单位内部按性价比从高到低排序）
        unit_groups: Dict[str, List[Tuple[float, int, int]]] = {}
        for idx, (best_primary_unit, unit_best_scores) in enumerate(product_unit_scores):
            matched_any = False
            for u, score in unit_best_scores.items():
                if u in active_base_units:
                    unit_groups.setdefault(u, []).append((score, -idx, idx))
                    matched_any = True
            if not matched_any:
                score = unit_best_scores.get(best_primary_unit, 0.0)
                unit_groups.setdefault(best_primary_unit, []).append((score, -idx, idx))

        for group in unit_groups.values():
            group.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # 3. 不同单位之间：按商品数量从多到少排序，并按数量比例计算每轮轮询名额 (quota)
        ordered_units = sorted(
            unit_groups.keys(),
            key=lambda u: (len(unit_groups[u]), unit_groups[u][0][0]),
            reverse=True,
        )

        sorted_items: List[Dict[str, Any]] = []
        if ordered_units:
            counts = [len(unit_groups[u]) for u in ordered_units]
            min_count = max(1, min(counts))
            max_count = max(counts)
            # 若最大组与最小组悬殊过大（超过 5 倍），将单轮最大名额封顶为 5，确保少见单位的好商品也能在首屏靠前展示
            base_step = float(min_count) if (max_count / min_count) <= 5 else (max_count / 5.0)
            unit_quotas = {
                u: max(1, int(len(unit_groups[u]) / base_step + 0.5))
                for u in ordered_units
            }

            unit_cursors = {u: 0 for u in ordered_units}
            emitted_indices = set()
            total_formatted = len(formatted_candidates)

            while len(emitted_indices) < total_formatted:
                progressed = False
                for u in ordered_units:
                    group = unit_groups[u]
                    cursor = unit_cursors[u]
                    quota = unit_quotas[u]
                    taken = 0
                    while cursor < len(group) and taken < quota:
                        _, _, cand_idx = group[cursor]
                        cursor += 1
                        if cand_idx in emitted_indices:
                            continue
                        emitted_indices.add(cand_idx)
                        sorted_items.append(formatted_candidates[cand_idx][0])
                        taken += 1
                        progressed = True
                    unit_cursors[u] = cursor
                if not progressed:
                    break

        # 尚未完成 format_info 清洗的商品排在末尾用作兜底
        sorted_items.extend(unformatted_items)
        return sorted_items

    def query_boom_products(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        sort_by: str = "latest",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        分页查询爆单库商品（支持 cost_performance 性价比全局分组排序及 latest 游标探测分页）
        """
        page = max(1, min(50, int(page or 1)))
        page_size = max(1, min(50, int(page_size or 20)))
        skip = (page - 1) * page_size

        if sort_by == "cost_performance" and keyword and keyword.strip():
            try:
                sorted_items = self.rank_boom_products_by_cost_performance(
                    platform=platform, category=category, keyword=keyword,
                )
                page_items = sorted_items[skip : skip + page_size]
                has_more = (skip + page_size) < len(sorted_items)
                return {
                    "status": "success",
                    "total_count": len(page_items) if keyword else len(sorted_items),
                    "page": page,
                    "page_size": page_size,
                    "has_more": has_more,
                    "data": page_items,
                }
            except Exception as e:
                logger.error(
                    "❌ 【爆品库查询失败】按性价比查询本地爆单商品列表时发生异常 | 平台[%s] 分类[%s] 关键词[%s] 排序[%s] 页码[%d] 异常<%s> 排查方向<请检查MongoDB连接状态、查询超时(2000ms)或format_info结构>",
                    self._platform(platform) if platform else "all",
                    category or "全部",
                    keyword or "-",
                    sort_by,
                    page,
                    format_exc_summary(e),
                )
                return {
                    "status": "error",
                    "error_msg": str(e),
                    "total_count": 0,
                    "page": page,
                    "page_size": page_size,
                    "has_more": False,
                    "data": [],
                }

        filter_query = self._build_boom_filter_query(platform=platform, category=category, keyword=keyword)

        sort_mapping = {
            "latest": [("updated_at", -1)],
            "save_desc": [("saved_price", -1), ("updated_at", -1)],
            "price_asc": [("activity_price", 1)],
            "price_desc": [("activity_price", -1)],
        }
        sort_spec = sort_mapping.get(sort_by, [("updated_at", -1)])

        try:
            cursor = (
                self.col.find(
                    {"$and": [filter_query, self._boom_content_filter()]},
                    self._SUMMARY_PROJECTION,
                )
                .sort(sort_spec)
                .skip(skip)
                .limit(page_size + 1)
                .max_time_ms(2000)
            )
            raw_docs = list(cursor)
            has_more = len(raw_docs) > page_size
            items = [self._normalize_doc_summary(doc) for doc in raw_docs[:page_size]]

            return {
                "status": "success",
                "total_count": len(items) if keyword else (skip + len(raw_docs)),
                "page": page,
                "page_size": page_size,
                "has_more": has_more,
                "data": items,
            }
        except Exception as e:
            logger.error(
                "❌ 【爆品库查询失败】分页查询本地爆单商品列表时发生数据库异常 | 平台[%s] 分类[%s] 关键词[%s] 排序[%s] 页码[%d] 异常<%s> 排查方向<请检查MongoDB连接状态、查询超时(2000ms)或正则表达式性能>",
                self._platform(platform) if platform else "all",
                category or "全部",
                keyword or "-",
                sort_by,
                page,
                format_exc_summary(e),
            )
            return {
                "status": "error",
                "error_msg": str(e),
                "total_count": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "data": [],
            }

    def _normalize_doc_summary(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """列表与详情共用的公共字段视图，保留金额 0 和字符串身份别名。"""
        from adapters import platform_name

        platform, goods_id, goods_sign = self._identity(doc)
        activity_price = self._amount_float(self._first_present(doc, "activity_price", "price", default=0.0))
        original_price = self._amount_float(self._first_present(doc, "original_price", default=activity_price))
        saved_price = self._amount_float(self._first_present(doc, "saved_price", "coupon_amount", default=max(0.0, round(original_price - activity_price, 2))))
        coupon_amount = max(0.0, self._amount_float(self._first_present(doc, "coupon_amount", "saved_price", default=0.0)))
        if coupon_amount > 0 and original_price <= activity_price:
            original_price = round(activity_price + coupon_amount, 2)
        elif original_price < activity_price:
            original_price = activity_price
        commission_rate, commission_amount = self._calc_commission(activity_price, doc.get("commission_amount"))
        if doc.get("commission_rate") is not None:
            commission_rate = self._amount_float(doc["commission_rate"])
        updated_at_dt = doc.get("updated_at")
        updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")
        raw_doc_img = doc.get("image_url")
        clean_doc_img = str(raw_doc_img).strip() if isinstance(raw_doc_img, str) else ""
        if clean_doc_img.startswith("//"):
            clean_doc_img = "https:" + clean_doc_img
        return {
            "platform": platform,
            "platform_name": platform_name(platform),
            "product_id": goods_id or goods_sign,
            "goods_id": goods_id,
            "goods_sign": goods_sign,
            "name": doc.get("name", "未命名商品"),
            "brand": doc.get("brand") or "",
            "category": doc.get("category") or "",
            "price": round(activity_price, 2),
            "original_price": round(original_price, 2),
            "saved_price": round(saved_price, 2),
            "coupon_amount": round(coupon_amount, 2),
            "has_coupon": coupon_amount > 0,
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
            "sales_tip": str(self._first_present(doc, "sales_tip", default="0")),
            "image_url": clean_doc_img,
            "product_url": doc.get("product_url") or "",
            "mall_name": doc.get("mall_name") or "",
            "updated_at": updated_at_str,
        }

    @classmethod
    def _extract_deal_tip(cls, doc: Dict[str, Any], fallback_time: str = "") -> Optional[Dict[str, Any]]:
        """提取公开可展示的线报详情视图，严格过滤内部评分与爬虫源字段；非线报商品返回 None。"""
        deal_info = doc.get("deal_info")
        if not isinstance(deal_info, dict) or deal_info.get("is_deal_tip") is not True:
            return None

        tz_cn = datetime.timezone(datetime.timedelta(hours=8))
        publish_time = ""
        raw_pub = deal_info.get("publish_time")
        if isinstance(raw_pub, datetime.datetime):
            dt = raw_pub.astimezone(tz_cn) if raw_pub.tzinfo is not None else raw_pub
            publish_time = dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(raw_pub, str) and raw_pub.strip():
            s = raw_pub.strip()
            try:
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone(tz_cn)
                publish_time = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                publish_time = s[:16].replace("T", " ") if len(s) >= 16 else s[:32]
        if not publish_time:
            publish_time = str(fallback_time or "").strip()

        deal_price: Optional[float] = None
        raw_dp = deal_info.get("deal_price")
        if raw_dp is not None and not isinstance(raw_dp, bool):
            try:
                dp_val = cls._amount_float(raw_dp)
                if math.isfinite(dp_val) and dp_val > 0:
                    deal_price = round(dp_val, 2)
            except (TypeError, ValueError, OverflowError):
                deal_price = None

        unit_deal_price = str(deal_info.get("unit_deal_price") or "").strip()
        price_logic = str(deal_info.get("price_logic") or "").strip()

        coupon_links: List[Dict[str, Any]] = []
        raw_coupons = deal_info.get("coupon_links")
        if isinstance(raw_coupons, list):
            for entry in raw_coupons:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "").strip()
                if url and not url.startswith(("http://", "https://")):
                    url = ""
                token = str(entry.get("token") or "").strip()
                if not url and not token:
                    continue
                coupon_links.append({
                    "label": str(entry.get("label") or "优惠券").strip() or "优惠券",
                    "url": url,
                    "token": token,
                    "platform": cls._platform(entry.get("platform")) if entry.get("platform") else "",
                })

        filler_links: List[Dict[str, Any]] = []
        raw_fillers = deal_info.get("filler_links")
        if isinstance(raw_fillers, list):
            for entry in raw_fillers:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "").strip()
                if url and not url.startswith(("http://", "https://")):
                    url = ""
                token = str(entry.get("token") or "").strip()
                if not url and not token:
                    continue
                f_price: Optional[float] = None
                raw_fp = entry.get("official_price")
                if raw_fp is not None and not isinstance(raw_fp, bool):
                    try:
                        fp_val = cls._amount_float(raw_fp)
                        if math.isfinite(fp_val) and fp_val > 0:
                            f_price = round(fp_val, 2)
                    except (TypeError, ValueError, OverflowError):
                        f_price = None
                filler_links.append({
                    "label": str(entry.get("label") or entry.get("official_title") or "凑单品").strip() or "凑单品",
                    "url": url,
                    "token": token,
                    "platform": cls._platform(entry.get("platform")) if entry.get("platform") else "",
                    "official_price": f_price,
                })

        tutorial_steps: List[str] = []
        raw_steps = deal_info.get("tutorial_steps")
        if isinstance(raw_steps, list):
            for step in raw_steps:
                text = str(step or "").strip()
                if text:
                    tutorial_steps.append(text)
        if not tutorial_steps and isinstance(deal_info.get("tutorial_text"), str):
            for line in deal_info["tutorial_text"].splitlines():
                text = line.strip()
                if text:
                    tutorial_steps.append(text)

        original_copywriting = str(deal_info.get("original_copywriting") or "").strip()

        share_images: List[str] = []
        raw_imgs = deal_info.get("user_share_images")
        if isinstance(raw_imgs, list):
            for img in raw_imgs:
                u = str(img or "").strip()
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith(("http://", "https://")) and u not in share_images:
                    share_images.append(u)

        if not (
            deal_price is not None
            or unit_deal_price
            or price_logic
            or coupon_links
            or filler_links
            or tutorial_steps
            or original_copywriting
            or share_images
        ):
            return None

        return {
            "publish_time": publish_time,
            "deal_price": deal_price,
            "unit_deal_price": unit_deal_price,
            "price_logic": price_logic,
            "coupon_links": coupon_links,
            "filler_links": filler_links,
            "tutorial_steps": tutorial_steps,
            "original_copywriting": original_copywriting,
            "share_images": share_images,
        }

    def _normalize_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """返回存储的真实详情；未知商业字段保持空，不捏造优惠券/承诺/评分。"""
        result = self._normalize_doc_summary(doc)
        gallery = doc.get("carousel_images")
        clean_gallery: List[str] = []
        if isinstance(gallery, list):
            for raw_u in gallery:
                if not isinstance(raw_u, str):
                    continue
                u = raw_u.strip()
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith(("http://", "https://")):
                    clean_gallery.append(u)
        main_img = result["image_url"]
        if not clean_gallery and isinstance(main_img, str) and main_img.startswith(("http://", "https://")):
            clean_gallery = [main_img]
        raw_tags = doc.get("detail_tags") or []
        clean_tags: List[str] = []
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                t = str(tag or "").strip()
                if not t:
                    continue
                if _BARE_YUAN_TAG_RE.match(t):
                    t = f"{t}优惠券"
                if t not in clean_tags:
                    clean_tags.append(t)
        result.update({
            "desc": doc.get("desc") or "",
            "brand_name": result["brand"],
            "category_name": result["category"],
            "carousel_images": list(dict.fromkeys(clean_gallery)),
            "product_url": doc.get("product_url") or "",
            "detail_tags": clean_tags,
            "unified_tags": deepcopy(doc.get("unified_tags") or []),
            "service_guarantees": deepcopy(doc.get("service_guarantees") or []),
            "shop_ratings": deepcopy(doc.get("shop_ratings") or {}),
            "specs": deepcopy(doc.get("specs") or []),
            "coupon_info": deepcopy(doc.get("coupon_info") or {}),
            "search_id": doc.get("search_id") or "",
            "deal_tip": self._extract_deal_tip(doc, result["updated_at"]),
            # 不把摘要 updated_at 冒充详情抓取时间；服务层负责过期决策。
            "detail_updated_at": doc.get("detail_updated_at"),
        })
        return result

    def get_product_detail(
        self, goods_id_or_sign: str, platform: str = "pdd", key_type: Optional[str] = None,
        *, strict: bool = False, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """按平台隔离查询；strict=True 传播数据库故障，避免后台误判缺失后请求平台。"""
        key = str(goods_id_or_sign).strip() if goods_id_or_sign is not None else ""
        if not key:
            return None
        query: Dict[str, Any] = {"platform": self._platform(platform)}
        if key_type in ("goods_id", "product_id", "id"):
            real_id = self._real_goods_id(key)
            if not real_id:
                return None
            query["product_id"] = real_id
        elif key_type in ("goods_sign", "sign"):
            query["goods_sign"] = key
        elif key_type is None:
            conds = [{"goods_sign": key}]
            if self._real_goods_id(key):
                conds.insert(0, {"product_id": key})
            query["$or"] = conds
        else:
            raise ValueError(f"不支持的商品查询键类型: {key_type}")
        try:
            doc = self.col.find_one({"$and": [query, self._displayable_content_filter()]})
            if doc:
                return self._normalize_doc(doc)
        except Exception as e:
            logger.warning(
                "❌ 【商品缓存读取异常】从本地数据库反查商品详情时发生异常 | 平台[%s] 商品[%s] 键类型[%s] 严格模式[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或products集合索引>",
                self._platform(platform), key, key_type or "auto", strict, format_exc_summary(e),
            )
            if strict:
                raise
        return None

    def get_product_identity(self, goods_sign: str, platform: str = "pdd") -> Optional[Dict[str, Any]]:
        """按同平台 Sign 反查可信身份，不要求该记录已有可展示商品内容。"""
        sign = str(goods_sign or "").strip()
        if not sign:
            return None
        try:
            return self.col.find_one(
                {"platform": self._platform(platform), "goods_sign": sign},
                {"_id": 0, "platform": 1, "product_id": 1, "goods_id": 1, "goods_sign": 1},
            )
        except Exception as e:
            logger.warning(
                "❌ 【商品身份反查异常】从本地数据库按Sign反查商品ID时发生异常 | 平台[%s] Sign[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或goods_sign索引>",
                self._platform(platform), sign, format_exc_summary(e),
            )
            return None

    def get_categories(self, platform: str = "all") -> List[str]:
        """获取类目列表"""
        filter_query = {}
        if platform and self._platform(platform) != "all":
            filter_query["platform"] = self._platform(platform)
        try:
            cats = self.col.distinct("category", {"$and": [filter_query, self._boom_content_filter()]})
            result = []
            for c in cats:
                text = str(c).strip() if c is not None else ""
                if text and len(text) <= 20 and not text.startswith("未知"):
                    result.append(text)
            return result
        except Exception as e:
            logger.warning(
                "❌ 【商品类目读取降级】查询本地爆品库分类列表失败，已降级返回默认分类 | 平台[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或distinct聚合性能>",
                self._platform(platform) if platform else "all", format_exc_summary(e),
            )
            return ["全部", "食品", "美妆", "百货", "数码", "服饰"]

    def upsert_home_products(
        self,
        goods_list: List[Dict[str, Any]],
        channel_type: Optional[int] = None
    ) -> int:
        """
        批量更新/插入首页官方推荐流商品到 MongoDB products 集合
        隔绝大模型开销标记 (need_reformat: False, format_status: 'success')
        """
        if not goods_list or not isinstance(goods_list, list):
            return 0

        ops_by_key = {}
        for item in goods_list:
            if not isinstance(item, dict):
                continue
            mapped = self._build_product_update(item, detail=False, channel_type=channel_type)
            if mapped is not None:
                filter_spec, update = mapped
                platform, goods_id, goods_sign = self._identity(item)
                dedup_key = (platform, goods_id or goods_sign)
                ops_by_key[dedup_key] = UpdateOne(filter_spec, update, upsert=True)
        operations = list(ops_by_key.values())

        if not operations:
            return 0

        try:
            result = self.col.bulk_write(operations, ordered=False)
            upserted_count = result.upserted_count + result.modified_count
            logger.debug(
                "【商品摘要入库】批量写入首页推荐商品完成 | 频道[%s] 变更数量[%d条] 提交总数[%d条]",
                channel_type if channel_type is not None else "-", upserted_count, len(operations),
            )
            return upserted_count
        except Exception as e:
            logger.warning(
                "❌ 【商品摘要入库异常】批量写入首页推荐流商品到本地数据库失败 | 频道[%s] 待写数量[%d条] 异常<%s> 排查方向<请检查MongoDB写入连接、磁盘空间或唯一索引冲突>",
                channel_type if channel_type is not None else "-", len(operations), format_exc_summary(e),
            )
            return 0

    _DETAIL_CAS_ATTEMPTS = 4

    @staticmethod
    def _detail_source_time(value: Any) -> Optional[datetime.datetime]:
        """BSON 毫秒精度的 naive UTC；naive 来源时间与 Mongo 默认解码一样表示 UTC。"""
        try:
            return utc_millis(value).replace(tzinfo=None)
        except (ValueError, OverflowError, TypeError):
            return None

    def upsert_product_detail(self, detail: Dict[str, Any]) -> bool:
        """按来源时间有限 CAS；仅确认本快照入库返回 True，旧/同毫秒版本返回 False。"""
        if not detail or not isinstance(detail, dict):
            return False
        now = self._detail_source_time(datetime.datetime.now(datetime.timezone.utc))
        # 兼容旧调用未给时间；明确给了非法/未来时间则拒绝，不能在落库时续期。
        source_time = self._detail_source_time(detail.get("detail_updated_at", now))
        if source_time is None or source_time > now:
            return False
        try:
            mapped = self._build_product_update(detail, detail=True)
            if mapped is None:
                return False
            filter_spec, update = mapped
            update["$set"]["detail_updated_at"] = source_time
            # 身份读取不走详情可展示条件，摘要/推广身份裸记录也必须参与 CAS。
            collection = self.col
            inserted_id = None
            for _ in range(self._DETAIL_CAS_ATTEMPTS):
                doc = collection.find_one(filter_spec, {"_id": 1, "detail_updated_at": 1})
                if doc is None:
                    # 只有身份条件参与 upsert；匹配并发创建的文档时绝不覆盖其字段。
                    insert_doc = {**update["$setOnInsert"], **update["$set"]}
                    try:
                        result = collection.update_one(
                            filter_spec, {"$setOnInsert": insert_doc}, upsert=True,
                        )
                    except DuplicateKeyError:
                        # 另一线程可能已创建/补齐可信 ID；下轮按身份重读，预算有限。
                        continue
                    if getattr(result, "acknowledged", False) is not True:
                        return False
                    inserted_id = result.upserted_id
                    # 即使已插入，也重读确认；matched-only 的 setOnInsert 不算本快照成功。
                    continue

                stored_time = self._detail_source_time(doc.get("detail_updated_at"))
                # 不能用本次调用开始时的 now 判定未来：阻塞期间写入的新前台来源同样合法。
                read_now = self._detail_source_time(datetime.datetime.now(datetime.timezone.utc))
                if stored_time is not None and stored_time <= read_now and stored_time >= source_time:
                    return bool(inserted_id is not None and doc["_id"] == inserted_id
                                and stored_time == source_time)

                # 用所读原值（而非归一化后的值）做条件；null 与缺字段也必须区分。
                old_time = ({"$eq": doc["detail_updated_at"], "$exists": True}
                            if "detail_updated_at" in doc else {"$exists": False})
                try:
                    result = collection.update_one(
                        {"_id": doc["_id"], "detail_updated_at": old_time},
                        {"$set": update["$set"]}, upsert=False,
                    )
                except DuplicateKeyError:
                    continue
                if getattr(result, "acknowledged", False) is not True:
                    return False
                if result.matched_count == 1:
                    return result.modified_count == 1
                # 条件失败说明读后有新详情写入，重新判断胜出来源时间，不无条件重试覆盖。
            return False
        except Exception as e:
            logger.warning(
                "❌ 【商品详情入库异常】写入商品完整详情到本地数据库时发生异常 | 平台[%s] 商品[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或文档CAS并发冲突>",
                detail.get("platform") or "-",
                detail.get("goods_id") or detail.get("goods_sign") or "-",
                format_exc_summary(e),
            )
            return False

    @classmethod
    def _promotion_identity(
        cls, goods_id: Optional[str], goods_sign: Optional[str], goods_key: Optional[str],
    ) -> Tuple[str, str]:
        """显式身份不猜类型；仅没有显式身份时兼容旧 goods_key。"""
        raw_id = str(goods_id).strip() if goods_id is not None else ""
        sign = str(goods_sign).strip() if goods_sign is not None else ""
        gid = cls._real_goods_id(raw_id)
        if not raw_id and not sign:
            legacy_key = str(goods_key).strip() if goods_key is not None else ""
            if legacy_key.isdigit():
                gid = cls._real_goods_id(legacy_key)
            else:
                sign = legacy_key
        return gid, sign

    def get_promotion_link_cache(
        self,
        platform: str,
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        goods_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """从 products 集合中读取站长官方推广链接缓存"""
        platform = self._platform(platform)
        gid, gsign = self._promotion_identity(goods_id, goods_sign, goods_key)

        if gid:
            filter_spec = {"platform": platform, "product_id": gid}
        elif gsign:
            filter_spec = {"platform": platform, "goods_sign": gsign}
        else:
            return None

        try:
            doc = self.col.find_one(filter_spec, {
                "_id": 0, "default_promotion": 1, "default_promotion_expires_at": 1,
            })
            if not doc:
                return None
            promo_data = doc.get("default_promotion")
            expiry = doc.get("default_promotion_expires_at")
            if not isinstance(promo_data, dict) or not promo_data.get("final_link"):
                return None
            if not isinstance(expiry, datetime.datetime):
                return None
            # 新字段只按 UTC 解码；不从旧的本地 updated_at 推测生命周期。
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            expires_at = expiry.timestamp()
            now = time.time()
            if not math.isfinite(expires_at) or not now < expires_at <= now + PROMOTION_LINK_TTL:
                return None
            result = deepcopy(promo_data)
            result["from_cache"] = True
            result["_expires_at"] = expires_at
            return result
        except Exception as e:
            logger.warning(
                "❌ 【转链缓存读取异常】从products集合读取通用推广链接缓存失败，将回退实时转链 | 平台[%s] 商品[%s] 异常<%s> 排查方向<请检查MongoDB连接状态>",
                platform, gid or gsign, format_exc_summary(e),
            )
        return None

    def save_promotion_link_cache(
        self,
        platform: str,
        data: Dict[str, Any],
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        goods_key: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> bool:
        """保存官方转链；expires_at 为 UTC Unix 秒时间戳，省略时采用统一 TTL。"""
        if not data or not isinstance(data, dict):
            return False

        now_ts = time.time()
        if expires_at is None:
            expires_at = now_ts + PROMOTION_LINK_TTL
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return False
        if not math.isfinite(expires_at) or not now_ts < expires_at <= now_ts + PROMOTION_LINK_TTL:
            return False
        now = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc)
        # BSON datetime 仅有毫秒精度，向下取整，避免内存/持久缓存意外延长寿命。
        expiry = datetime.datetime.fromtimestamp(math.floor(expires_at * 1000) / 1000, datetime.timezone.utc)
        cache_data = {
            key: deepcopy(value) for key, value in data.items()
            if key not in ("command_text", "from_cache") and not str(key).startswith("_")
        }

        platform = self._platform(platform)
        gid, gsign = self._promotion_identity(goods_id, goods_sign, goods_key)

        if gid:
            filter_spec = {"platform": platform, "product_id": gid}
        elif gsign:
            filter_spec = {"platform": platform, "goods_sign": gsign}
        else:
            return False

        update_set, insert_set = self._identity_update(platform, gid, gsign)
        update_set.update({
            "default_promotion": cache_data,
            "default_promotion_updated_at": now,
            "default_promotion_expires_at": expiry,
        })
        insert_set["created_at"] = now

        try:
            self.col.update_one(
                filter_spec,
                {"$set": update_set, "$setOnInsert": insert_set},
                upsert=True
            )
            return True
        except Exception as e:
            logger.warning(
                "❌ 【转链缓存写入异常】保存通用推广链接缓存到products集合失败 | 平台[%s] 商品[%s] 异常<%s> 排查方向<请检查MongoDB写入状态或唯一索引冲突>",
                platform, gid or gsign, format_exc_summary(e),
            )
            return False

    def delete_product(
        self,
        platform: str,
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
    ) -> bool:
        """物理删除已确认取消推广或下架的商品记录，避免首页/爆单库或详情降级继续展示失效商品。"""
        plat = self._platform(platform)
        gid = self._real_goods_id(goods_id)
        gsign = str(goods_sign or "").strip()
        if not gid and not gsign:
            return False
        filter_spec = self._identity_filter(plat, gid, gsign)
        try:
            res = self.col.delete_many(filter_spec)
            deleted = int(getattr(res, "deleted_count", 0) or 0)
            if deleted > 0:
                logger.info(
                    "【失效商品清理】已从本地数据库物理移除取消推广/下架商品 | 平台[%s] 商品[%s] 移除条数[%d]",
                    plat, gid or gsign, deleted,
                )
            return deleted > 0
        except Exception as e:
            logger.warning(
                "❌ 【失效商品清理异常】从products集合删除下架商品失败 | 平台[%s] 商品[%s] 异常<%s>",
                plat, gid or gsign, format_exc_summary(e),
            )
            return False
