# -*- coding: utf-8 -*-
"""
商品数据仓储模块 (MongoDB: products 集合)
负责商品数据的高效读写、索引保障与爆品库检索。
"""
from collections import defaultdict
import datetime
import math
import re
import time
from copy import deepcopy
from typing import Dict, Any, List, Optional, Set, Tuple
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError
from bson.decimal128 import Decimal128
from repositories.base_repo import BaseRepository
from infrastructure.logging_utils import format_exc_summary
from models.cost_performance import (
    parse_deal_publish_timestamp,
    rank_products_by_cost_performance,
)
from models.deal_constants import DEAL_INFO_PROTECTION_HOURS
from models.product import (
    _BARE_YUAN_TAG_RE,
    clean_product_id,
    extract_canonical_goods_id,
    extract_search_keyword,
    get_source_site_name,
    is_jd_image_key,
    normalize_jd_material_url,
    normalize_platform,
    resolve_item_identity,
    utc_millis,
)
from config import PROMOTION_LINK_TTL, logger
from infrastructure.search_meili import (
    is_meili_available,
    search_products,
)


def _to_naive_utc(dt: Any) -> Optional[datetime.datetime]:
    """统一将任意 datetime 转换为无时区偏移 (naive) UTC 时间。"""
    if not isinstance(dt, datetime.datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def upsert_cleaned_products_to_formal_table(
    formal_col,
    product_docs: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    【Stage 5：高分好价商品晋升入正式表 `products`（含质量评分防覆盖保护）】
    - 严格以唯一索引 `{"platform": plat, "product_id": pid}` 作为过滤条件；
    - 顶层商品属性 (`name`, `price`, `original_price`, `activity_price` 等) 始终保持官方最新数据；
    - 高分保护法则：
      1) 同批次多条线报命中同一商品时，按 `deal_info.quality_score` 最高者胜出；
      2) 若 `products` 表中该商品已有 `deal_info.is_deal_tip == True` 且 `updated_at` 在 12 小时保鲜期内，
         仅当新线报 `quality_score >= 已有 deal_info.quality_score`（或价格明显更低 <98%）时才更新 `deal_info` 内容，
         防止低分简陋线报覆盖高分精品线报。
    """
    if not product_docs:
        return {"upserted": 0, "modified": 0, "matched": 0, "protected_high_score": 0, "total_ops": 0}

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    protection_cutoff = now - datetime.timedelta(hours=DEAL_INFO_PROTECTION_HOURS)

    grouped_by_pid: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for doc in product_docs:
        grouped_by_pid[(doc["platform"], doc["product_id"])].append(doc)

    existing_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        or_queries = [{"platform": p, "product_id": pid} for (p, pid) in grouped_by_pid.keys()]
        if or_queries:
            for ex_doc in formal_col.find(
                {"$or": or_queries},
                {
                    "_id": 0,
                    "platform": 1,
                    "product_id": 1,
                    "deal_info.is_deal_tip": 1,
                    "deal_info.quality_score": 1,
                    "deal_info.deal_price": 1,
                    "deal_info.updated_at": 1,
                },
            ):
                k = (str(ex_doc.get("platform") or ""), str(ex_doc.get("product_id") or ""))
                existing_map[k] = ex_doc
    except Exception as e:
        logger.warning("查询正式表已有商品评分异常: %s", e)

    ops = []
    protected_high_score = 0

    for (plat, pid), docs_for_item in grouped_by_pid.items():
        docs_sorted = sorted(
            docs_for_item,
            key=lambda x: int((x.get("deal_info") or {}).get("quality_score") or 0),
        )
        best_doc = deepcopy(docs_sorted[-1])
        deal_info = deepcopy(best_doc.pop("deal_info", {}) or {})
        new_score = int(deal_info.get("quality_score") or 0)

        total_seen_inc = 0
        all_sites: List[str] = []
        all_channels: List[str] = []
        for d in docs_for_item:
            di = d.get("deal_info") or {}
            total_seen_inc += max(1, int(di.get("seen_count") or 1))
            s_site = str(di.get("source_site") or "").strip()
            if s_site and s_site not in all_sites:
                all_sites.append(s_site)
            for ch in di.get("seen_channels") or []:
                ch_s = str(ch or "").strip()
                if ch_s and ch_s not in all_channels:
                    all_channels.append(ch_s)

        deal_info.pop("seen_count", None)
        deal_info.pop("seen_channels", None)

        set_fields: Dict[str, Any] = {
            **best_doc,
            "updated_at": now,
            "last_clean_source": "deal_pool",
        }
        if len(best_doc.get("carousel_images") or []) > 1 and float(best_doc.get("commission_amount") or 0.0) > 0:
            set_fields["detail_updated_at"] = now

        ex_record = existing_map.get((plat, pid))
        ex_di = (ex_record or {}).get("deal_info") if isinstance((ex_record or {}).get("deal_info"), dict) else {}
        ex_score = int(ex_di.get("quality_score") or 0)
        ex_deal_updated = _to_naive_utc(ex_di.get("updated_at"))
        is_fresh_existing = bool(
            ex_di.get("is_deal_tip")
            and ex_deal_updated is not None
            and ex_deal_updated >= protection_cutoff
        )

        new_dp = (
            float(deal_info["deal_price"])
            if isinstance(deal_info.get("deal_price"), (int, float))
            and not isinstance(deal_info.get("deal_price"), bool)
            and float(deal_info["deal_price"]) > 0
            else None
        )
        ex_dp = (
            float(ex_di["deal_price"])
            if isinstance(ex_di.get("deal_price"), (int, float))
            and not isinstance(ex_di.get("deal_price"), bool)
            and float(ex_di["deal_price"]) > 0
            else None
        )
        price_improved = bool(new_dp is not None and ex_dp is not None and new_dp < ex_dp * 0.98)

        if is_fresh_existing and ex_score > new_score and not price_improved:
            protected_high_score += 1
        else:
            has_numeric_dp = (
                isinstance(deal_info.get("deal_price"), (int, float))
                and not isinstance(deal_info.get("deal_price"), bool)
                and float(deal_info["deal_price"]) > 0
            )
            deal_info["updated_at"] = now
            deal_info["expired_at"] = None
            deal_info["need_reformat"] = has_numeric_dp
            deal_info["format_status"] = "pending" if has_numeric_dp else "skipped"
            deal_info["format_info"] = None
            deal_info["format_deal_price"] = None
            deal_info["format_retry_count"] = 0
            deal_info["format_error"] = ""
            for dk, dv in deal_info.items():
                set_fields[f"deal_info.{dk}"] = dv

        batch_timeline_entries: List[Dict[str, Any]] = []
        seen_tids: Set[str] = set()
        for d in docs_for_item:
            di = d.get("deal_info") or {}
            mid = str(di.get("source_item_id") or d.get("product_id") or "")
            if mid and mid not in seen_tids:
                seen_tids.add(mid)
                pub_t = str(di.get("publish_time") or "")
                s_site = str(di.get("source_site") or "")
                s_name = str(di.get("source_site_name") or get_source_site_name(s_site))
                p_nick = str(di.get("provider_nickname") or (di.get("hot_metrics") or {}).get("provider_nickname") or "")
                batch_timeline_entries.append({
                    "tip_id": mid,
                    "source_site": s_site,
                    "source_site_name": s_name,
                    "source_channel": str(di.get("source_channel") or ""),
                    "provider_nickname": p_nick,
                    "publish_time": pub_t,
                    "publish_ts": parse_deal_publish_timestamp(pub_t) or 0.0,
                    "title": str(di.get("raw_tip_title") or d.get("name") or ""),
                    "copywriting": str(di.get("original_copywriting") or "")[:300],
                    "deal_price": di.get("deal_price"),
                    "tutorial_score": int(di.get("tutorial_score") or di.get("quality_score") or 75),
                })

        update_spec: Dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {
                "created_at": now,
                "source": "deal_pool",
            },
            "$inc": {
                "deal_info.total_seen_count": total_seen_inc,
                "deal_info.tip_hit_count": len(docs_for_item),
            },
        }
        if batch_timeline_entries:
            update_spec["$push"] = {
                "deal_info.tips_timeline": {
                    "$each": batch_timeline_entries,
                    "$sort": {"publish_ts": -1},
                    "$slice": 20,
                }
            }
        add_to_set: Dict[str, Any] = {}
        if all_sites:
            add_to_set["deal_info.resonance_sites"] = {"$each": all_sites}
        if all_channels:
            add_to_set["deal_info.resonance_channels"] = {"$each": all_channels}
        if add_to_set:
            update_spec["$addToSet"] = add_to_set

        ops.append(UpdateOne({"platform": plat, "product_id": pid}, update_spec, upsert=True))

    res = formal_col.bulk_write(ops, ordered=False)

    return {
        "upserted": res.upserted_count,
        "modified": res.modified_count,
        "matched": res.matched_count,
        "protected_high_score": protected_high_score,
        "total_ops": len(ops),
    }


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
            "sales_tip", "image_url", "mall_name", "updated_at", "product_url", "coupon_url",
            "deal_source", "deal_source_name",
            "deal_info.is_deal_tip", "deal_info.card_highlight", "deal_info.publish_time",
            "deal_info.source_site", "deal_info.source_site_name",
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
        "deal_info.is_deal_tip": 1,
        "deal_info.publish_time": 1,
        "deal_info.deal_price": 1,
        "deal_info.format_deal_price": 1,
        "deal_info.format_status": 1,
        "deal_info.need_reformat": 1,
        "deal_info.format_info": 1,
    }

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
        """爆单推广与类目聚合仅读取已入库的榜单商品或已清洗线报商品，排除搜索/详情散查裸记录与已软下架商品。"""
        content_filter = deepcopy(cls._displayable_content_filter())
        content_filter["is_off_promotion"] = {"$ne": True}
        content_filter["superseded_by"] = {"$exists": False}
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
        return resolve_item_identity(item)

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

    def _is_meili_searchable(self) -> bool:
        """仅在具备真实持久化集合且 Meilisearch 处于健康状态时启用全文检索；单元测试内存集合默认保留本地管道断言。"""
        if getattr(self, "_force_meili_enabled", None) is True:
            return True
        if getattr(self, "_force_meili_enabled", None) is False:
            return False
        col = getattr(self, "col", None)
        if col is None or type(col).__name__ != "Collection":
            return False
        return is_meili_available()

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
        raw_rate = item.get("commission_rate")
        effective_price = update_set.get("activity_price")
        if effective_price is None and "price" in update_set:
            effective_price = update_set.get("price")

        comm_amt = float(commission) if commission is not None else None
        comm_rate = float(raw_rate) if raw_rate is not None else None

        if comm_amt is not None and comm_amt > 0:
            update_set["commission_amount"] = comm_amt
            if comm_rate is not None:
                update_set["commission_rate"] = comm_rate
            elif effective_price and effective_price > 0:
                update_set["commission_rate"] = round(comm_amt / effective_price * 100.0, 2)
        elif comm_rate is not None and comm_rate > 0:
            update_set["commission_rate"] = comm_rate
            if effective_price and effective_price > 0:
                update_set["commission_amount"] = round(effective_price * comm_rate / 100.0, 2)
        else:
            if comm_amt is not None:
                update_set["commission_amount"] = comm_amt
            if comm_rate is not None:
                update_set["commission_rate"] = comm_rate

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
            if platform == "jd":
                clean_url = normalize_jd_material_url(clean_url, target_id) or clean_url
                if (
                    not detail
                    and target_id
                    and not target_id.isdigit()
                    and not is_jd_image_key(target_id)
                    and clean_url == f"https://jingfen.jd.com/detail/{target_id}.html"
                ):
                    insert_set["product_url"] = clean_url
                else:
                    update_set["product_url"] = clean_url
            else:
                update_set["product_url"] = clean_url

        coupon_url = item.get("coupon_url")
        if isinstance(coupon_url, str) and coupon_url.strip():
            update_set["coupon_url"] = coupon_url.strip()

        update_set["updated_at"] = now
        update_set["is_off_promotion"] = False
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

    def rank_boom_products_by_cost_performance(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询爆单库候选商品并委托纯领域性价比引擎 (`models.cost_performance`) 排序。
        """
        filter_query = self._build_boom_filter_query(platform=platform, category=category, keyword=keyword)
        raw_docs = None

        if keyword and keyword.strip() and self._is_meili_searchable():
            try:
                raw_kw = keyword.strip()
                cleaned_kw = extract_search_keyword(raw_kw, max_length=60) or raw_kw[:60]
                meili_hits = search_products(
                    keyword=cleaned_kw,
                    platform=platform,
                    category=category,
                    limit=self._MAX_COST_PERFORMANCE_CANDIDATES,
                    only_active_boom=True,
                )
                if meili_hits is not None:
                    doc_keys = [(h.get("platform"), h.get("product_id")) for h in meili_hits]
                    or_clauses = [{"platform": p, "product_id": pid} for p, pid in doc_keys if p and pid]
                    if or_clauses:
                        mongo_docs = list(self.col.find({"$and": [{"$or": or_clauses}, self._boom_content_filter()]}, self._COST_PERFORMANCE_PROJECTION))
                        docs_map = {(d.get("platform"), d.get("product_id")): d for d in mongo_docs}
                        raw_docs = [docs_map[(p, pid)] for p, pid in doc_keys if (p, pid) in docs_map]
                    else:
                        raw_docs = []
            except Exception as meili_exc:
                logger.warning("⚠️ 【Meilisearch性价比候选检索降级】: %s", format_exc_summary(meili_exc))
                raw_docs = None

        if raw_docs is None:
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

        return rank_products_by_cost_performance(
            raw_docs,
            normalize_summary=self._normalize_doc_summary,
            now_ts=time.time(),
        )

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
                    "total_count": len(sorted_items),
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

        if keyword and keyword.strip() and self._is_meili_searchable():
            try:
                raw_kw = keyword.strip()
                cleaned_kw = extract_search_keyword(raw_kw, max_length=60) or raw_kw[:60]
                meili_hits = search_products(
                    keyword=cleaned_kw,
                    platform=platform,
                    category=category,
                    sort_by=sort_by,
                    limit=page_size + 1,
                    offset=skip,
                    only_active_boom=True,
                )
                if meili_hits is not None:
                    if not meili_hits:
                        return {
                            "status": "success",
                            "total_count": 0,
                            "page": page,
                            "page_size": page_size,
                            "has_more": False,
                            "data": [],
                        }
                    doc_keys = [(h.get("platform"), h.get("product_id")) for h in meili_hits]
                    or_clauses = [{"platform": p, "product_id": pid} for p, pid in doc_keys if p and pid]
                    if or_clauses:
                        mongo_docs = list(self.col.find({"$and": [{"$or": or_clauses}, self._boom_content_filter()]}, self._SUMMARY_PROJECTION))
                        docs_map = {(d.get("platform"), d.get("product_id")): d for d in mongo_docs}
                        ordered_docs = [docs_map[(p, pid)] for p, pid in doc_keys if (p, pid) in docs_map]
                        has_more = len(ordered_docs) > page_size
                        items = [self._normalize_doc_summary(doc) for doc in ordered_docs[:page_size]]
                        return {
                            "status": "success",
                            "total_count": len(items),
                            "page": page,
                            "page_size": page_size,
                            "has_more": has_more,
                            "data": items,
                        }
            except Exception as meili_exc:
                logger.warning("⚠️ 【Meilisearch商品检索降级】: %s", format_exc_summary(meili_exc))

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
        deal_info = doc.get("deal_info") if isinstance(doc.get("deal_info"), dict) else {}
        deal_source = str(doc.get("deal_source") or deal_info.get("source_site") or "").strip()
        deal_source_name = str(
            doc.get("deal_source_name")
            or deal_info.get("source_site_name")
            or get_source_site_name(deal_source)
        ).strip()
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
        is_active_deal = bool(deal_info.get("is_deal_tip") is True)
        deal_highlight = str(deal_info.get("card_highlight") or "").strip() if is_active_deal else ""
        deal_publish_time = self._format_deal_datetime(deal_info.get("publish_time")) if is_active_deal else ""
        return {
            "platform": platform,
            "platform_name": platform_name(platform),
            "product_id": goods_id or goods_sign,
            "goods_id": goods_id,
            "goods_sign": goods_sign,
            "deal_source": deal_source,
            "deal_source_name": deal_source_name,
            "deal_highlight": deal_highlight,
            "deal_publish_time": deal_publish_time,
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
            "coupon_url": str(doc.get("coupon_url") or "").strip(),
            "mall_name": doc.get("mall_name") or "",
            "updated_at": updated_at_str,
        }

    @staticmethod
    def _format_deal_datetime(raw_val: Any) -> str:
        """统一将线报时间字段格式化为北京时间 `YYYY-MM-DD HH:MM`，naive datetime 按 UTC 契约转换，无效值返回空串。"""
        tz_cn = datetime.timezone(datetime.timedelta(hours=8))
        if isinstance(raw_val, datetime.datetime):
            dt = (raw_val.replace(tzinfo=datetime.timezone.utc) if raw_val.tzinfo is None else raw_val).astimezone(tz_cn)
            return dt.strftime("%Y-%m-%d %H:%M")
        if isinstance(raw_val, str) and raw_val.strip():
            s = raw_val.strip()
            try:
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone(tz_cn)
                return dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return s[:16].replace("T", " ") if len(s) >= 16 else s[:32]
        return ""

    @classmethod
    def _extract_deal_tip(cls, doc: Dict[str, Any], fallback_time: str = "") -> Optional[Dict[str, Any]]:
        """提取公开可展示的线报详情视图，严格过滤内部评分与爬虫源字段；非线报商品返回 None。

        秉持以用户体验为中心与信息可见原则：只要该商品记录中存在实质性的线报详情
        （如购买教程、线报话术、券口令/链接、实付到手价等），无论是否已超 72 小时从严选榜单摘标，
        在商品详情页均完整透出供用户参考，并通过 is_deal_tip 与 updated_at 明确区分时效状态。
        """
        deal_info = doc.get("deal_info")
        if not isinstance(deal_info, dict):
            return None

        publish_time = cls._format_deal_datetime(deal_info.get("publish_time")) or str(fallback_time or "").strip()
        updated_at = cls._format_deal_datetime(deal_info.get("updated_at"))

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
                raw_lbl = str(entry.get("label") or "").strip()
                raw_lbl = re.sub(r"^(?:先领|点击领取|领取|领券|优惠券)\s*[:：]\s*", "", raw_lbl).strip()
                raw_lbl = re.sub(r"^(?:先领|点击领取)(?=.{2,})", "", raw_lbl).strip()
                lbl = "优惠券" if (not raw_lbl or raw_lbl in ("领券", "领", "券")) else raw_lbl
                coupon_links.append({
                    "label": lbl,
                    "url": url,
                    "token": token,
                    "platform": cls._platform(entry.get("platform")) if entry.get("platform") else "",
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

        card_highlight = str(deal_info.get("card_highlight") or "").strip()
        if not (
            deal_price is not None
            or unit_deal_price
            or price_logic
            or card_highlight
            or coupon_links
            or tutorial_steps
            or original_copywriting
            or share_images
        ):
            return None

        return {
            "is_deal_tip": bool(deal_info.get("is_deal_tip")),
            "publish_time": publish_time,
            "updated_at": updated_at,
            "deal_price": deal_price,
            "unit_deal_price": unit_deal_price,
            "price_logic": price_logic,
            "card_highlight": card_highlight,
            "coupon_links": coupon_links,
            "tutorial_steps": tutorial_steps,
            "original_copywriting": original_copywriting,
            "share_images": share_images,
            "tips_timeline": deal_info.get("tips_timeline") or [],
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
            "is_off_promotion": doc.get("is_off_promotion", False),
            "off_promotion_at": doc.get("off_promotion_at"),
            "superseded_by": doc.get("superseded_by"),
            "off_promotion_reason": doc.get("off_promotion_reason"),
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

    def get_deal_tip_view(self, product_id: str, platform: str = "pdd") -> Tuple[str, Optional[Dict[str, Any]]]:
        """按正式表主键只读线报视图，供详情接口返回前做权威覆盖。

        只投影 deal_info，不读整条详情，也不做任何写入。
        返回 (state, deal_tip)：
          - state="ok"      文档存在，deal_tip 为当前权威视图（可能为 None，表示该商品暂无线报）；
          - state="missing" 正式表没有该主键（例如仅存在于上游的临时商品），调用方不得改写 payload；
          - state="error"   查询异常，调用方保持原 payload 不变，避免把可用线报误删。
        """
        key = self._real_goods_id(str(product_id or "").strip())
        if not key:
            return "missing", None
        try:
            # 必须保留 _id：商品没有 deal_info 时投影结果会是空字典，不能用真值判断文档是否存在
            doc = self.col.find_one(
                {"platform": self._platform(platform), "product_id": key},
                {"deal_info": 1},
            )
        except Exception as e:
            logger.warning(
                "❌ 【线报视图读取异常】按商品主键读取线报视图失败，本次详情保持原样 | 平台[%s] 商品[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或products集合索引>",
                self._platform(platform), key, format_exc_summary(e),
            )
            return "error", None
        if doc is None:
            return "missing", None
        return "ok", self._extract_deal_tip(doc)

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

    def get_known_coupon_overlays(self, platform: str, keys: List[Any]) -> Dict[str, Dict[str, Any]]:
        """批量读取本地库中已探明的优惠券与券后价快照，防止无券榜单轮询把已知优惠券抹零。"""
        plat = self._platform(platform)
        clean_keys = list(dict.fromkeys(
            str(k).strip() for k in (keys or ())
            if k is not None and str(k).strip() and str(k).strip() not in ("0", "None")
        ))
        if not clean_keys:
            return {}
        try:
            cursor = self.col.find(
                {
                    "platform": plat,
                    "$or": [
                        {"product_id": {"$in": clean_keys}},
                        {"goods_sign": {"$in": clean_keys}},
                    ],
                },
                {
                    "_id": 0,
                    "product_id": 1,
                    "goods_id": 1,
                    "goods_sign": 1,
                    "activity_price": 1,
                    "price": 1,
                    "original_price": 1,
                    "coupon_amount": 1,
                    "saved_price": 1,
                    "coupon_url": 1,
                },
            )
            overlays: Dict[str, Dict[str, Any]] = {}
            for doc in cursor:
                if not isinstance(doc, dict):
                    continue
                c_url = str(doc.get("coupon_url") or "").strip()
                c_amt = max(0.0, self._amount_float(self._first_present(doc, "coupon_amount", "saved_price", default=0.0)))
                if not c_url and c_amt <= 0:
                    continue
                act_p = max(0.0, self._amount_float(self._first_present(doc, "activity_price", "price", default=0.0)))
                orig_p = max(act_p, self._amount_float(self._first_present(doc, "original_price", default=act_p)))
                if c_amt > 0 and orig_p <= act_p:
                    orig_p = round(act_p + c_amt, 2)
                entry = {
                    "activity_price": round(act_p, 2),
                    "original_price": round(orig_p, 2),
                    "coupon_amount": round(c_amt, 2),
                    "coupon_url": c_url,
                }
                for alias in (doc.get("product_id"), doc.get("goods_id"), doc.get("goods_sign")):
                    ak = str(alias or "").strip()
                    if ak:
                        overlays[ak] = entry
            return overlays
        except Exception as e:
            logger.warning(
                "❌ 【优惠券快照读取降级】批量读取已知优惠券覆盖层失败，将继续常规写入 | 平台[%s] 异常<%s>",
                plat, format_exc_summary(e),
            )
            return {}

    @classmethod
    def apply_coupon_overlay(cls, item: Dict[str, Any], overlays: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """将已知优惠券与券后价无损叠加到无券榜单摘要字典上（不覆盖入参已有优惠券）。"""
        if not isinstance(item, dict) or not overlays:
            return item
        _, gid, gsign = cls._identity(item)
        ov = overlays.get(gid) or overlays.get(gsign) or overlays.get(str(item.get("product_id") or "").strip())
        if not ov:
            return item
        out = dict(item)
        if ov.get("coupon_url") and not str(out.get("coupon_url") or "").strip():
            out["coupon_url"] = ov["coupon_url"]
        try:
            cur_coupon = float(cls._first_present(out, "coupon_amount", "saved_price", "coupon_discount_yuan", default=0.0) or 0.0)
        except (TypeError, ValueError):
            cur_coupon = 0.0
        if cur_coupon <= 0 and ov.get("coupon_amount", 0.0) > 0:
            c_amt = ov["coupon_amount"]
            out["coupon_amount"] = c_amt
            if "saved_price" in out:
                out["saved_price"] = c_amt
            out["has_coupon"] = True
            if ov.get("activity_price", 0.0) > 0:
                orig_val = max(ov.get("original_price", 0.0), round(ov["activity_price"] + c_amt, 2))
                if "activity_price" in out:
                    out["activity_price"] = ov["activity_price"]
                    out["price"] = orig_val
                else:
                    out["price"] = ov["activity_price"]
                out["original_price"] = orig_val
        return out

    def get_deal_card_overlays(self, platform: str, keys: List[Any]) -> Dict[str, Dict[str, str]]:
        """批量读取正式表活跃线报的超短亮点与发布时间快照，供首页推荐流/搜索卡片叠加展示。"""
        plat = self._platform(platform)
        clean_keys = list(dict.fromkeys(
            str(k).strip() for k in (keys or ())
            if k is not None and str(k).strip() and str(k).strip() not in ("0", "None")
        ))
        if not clean_keys:
            return {}
        try:
            cursor = self.col.find(
                {
                    "platform": plat,
                    "deal_info.is_deal_tip": True,
                    "$or": [
                        {"product_id": {"$in": clean_keys}},
                        {"goods_sign": {"$in": clean_keys}},
                    ],
                },
                {
                    "_id": 0,
                    "product_id": 1,
                    "goods_id": 1,
                    "goods_sign": 1,
                    "deal_info.card_highlight": 1,
                    "deal_info.publish_time": 1,
                },
            )
            overlays: Dict[str, Dict[str, str]] = {}
            for doc in cursor:
                if not isinstance(doc, dict):
                    continue
                di = doc.get("deal_info") if isinstance(doc.get("deal_info"), dict) else {}
                highlight = str(di.get("card_highlight") or "").strip()
                pub_time = self._format_deal_datetime(di.get("publish_time"))
                if not highlight and not pub_time:
                    continue
                entry = {"deal_highlight": highlight, "deal_publish_time": pub_time}
                for alias in (doc.get("product_id"), doc.get("goods_id"), doc.get("goods_sign")):
                    ak = str(alias or "").strip()
                    if ak:
                        overlays[ak] = entry
            return overlays
        except Exception as e:
            logger.warning(
                "⚠️ 【线报卡片快照降级】批量读取线报摘要亮点失败 | 平台[%s] 异常<%s>",
                plat, format_exc_summary(e),
            )
            return {}

    @classmethod
    def apply_deal_card_overlay(cls, item: Dict[str, Any], overlays: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
        """将线报超短亮点与发布时间无损叠加到商品摘要字典（不覆盖已有非空值）。"""
        if not isinstance(item, dict) or not overlays:
            return item
        _, gid, gsign = cls._identity(item)
        ov = overlays.get(gid) or overlays.get(gsign) or overlays.get(str(item.get("product_id") or "").strip())
        if not ov:
            return item
        out = dict(item)
        if ov.get("deal_highlight") and not str(out.get("deal_highlight") or "").strip():
            out["deal_highlight"] = ov["deal_highlight"]
        if ov.get("deal_publish_time") and not str(out.get("deal_publish_time") or "").strip():
            out["deal_publish_time"] = ov["deal_publish_time"]
        return out

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

        effective_list = goods_list
        if channel_type is not None:
            jd_zero_keys = []
            for item in goods_list:
                if not isinstance(item, dict):
                    continue
                plat, gid, gsign = self._identity(item)
                if plat != "jd":
                    continue
                c_val = self._first_present(item, "coupon_amount", "saved_price", "coupon_discount_yuan", default=0.0)
                try:
                    has_c = float(c_val or 0.0) > 0 and bool(str(item.get("coupon_url") or "").strip())
                except (TypeError, ValueError):
                    has_c = False
                if not has_c:
                    if gid:
                        jd_zero_keys.append(gid)
                    if gsign and gsign != gid:
                        jd_zero_keys.append(gsign)
            if jd_zero_keys:
                overlays = self.get_known_coupon_overlays("jd", jd_zero_keys)
                if overlays:
                    effective_list = [
                        self.apply_coupon_overlay(it, overlays) if isinstance(it, dict) and self._identity(it)[0] == "jd" else it
                        for it in goods_list
                    ]

        ops_by_key = {}
        for item in effective_list:
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

    def upsert_cleaned_deal_products(self, product_docs: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        【单一真源 SSOT · 正式商品库好价线报统一入库中枢】
        将完成 AI 清洗与官方转链的高分好价商品晋升入库到正式表 (`products`)；
        Meilisearch 搜索索引由后台唯一 `MeiliSyncWorker` 周期性双向差量对齐，不在此做分散双写。
        """
        return upsert_cleaned_products_to_formal_table(self.col, product_docs)

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
                    if result.modified_count == 1:
                        return True
                    return False
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

        filter_spec = {"platform": platform, "is_off_promotion": {"$ne": True}}
        if gid:
            filter_spec["product_id"] = gid
        elif gsign:
            filter_spec["goods_sign"] = gsign
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
        *,
        only_if_exists: bool = False,
    ) -> bool:
        """保存官方转链；expires_at 为 UTC Unix 秒时间戳，省略时采用统一 TTL。only_if_exists=True 时禁止向 products 硬插空记录。"""
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
            if only_if_exists:
                if gid and gsign:
                    filter_spec = {"platform": platform, "$or": [{"product_id": gid}, {"goods_sign": gsign}]}
                self.col.update_one(filter_spec, {"$set": update_set}, upsert=False)
            else:
                self.col.update_one(
                    filter_spec,
                    {"$set": update_set, "$setOnInsert": insert_set},
                    upsert=True,
                )
            return True
        except Exception as e:
            logger.warning(
                "❌ 【转链缓存写入异常】保存通用推广链接缓存到products集合失败 | 平台[%s] 商品[%s] 异常<%s> 排查方向<请检查MongoDB写入状态或唯一索引冲突>",
                platform, gid or gsign, format_exc_summary(e),
            )
            return False

    def _record_deletion_tombstones(self, platform: str, docs_to_delete: List[Dict[str, Any]]) -> None:
        """向 deleted_products_log 写入 7 天自动过期的删除墓碑，供云端同步守护进程跨库清理已下架死链。"""
        if not docs_to_delete or self._db is None or not hasattr(self._db, "get_collection"):
            return
        try:
            tomb_col = self._db.get_collection("deleted_products_log")
            if not getattr(self, "_tombstone_idx_ready", False):
                try:
                    tomb_col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
                    tomb_col.create_index([("deleted_at", 1)], expireAfterSeconds=7 * 86400, background=True)
                    self._tombstone_idx_ready = True
                except Exception:
                    pass
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            for d in docs_to_delete:
                pid = self._real_goods_id(d.get("product_id") or d.get("goods_id"))
                if not pid:
                    continue
                tomb_col.update_one(
                    {"platform": platform, "product_id": pid},
                    {
                        "$set": {
                            "platform": platform,
                            "product_id": pid,
                            "goods_sign": str(d.get("goods_sign") or "").strip(),
                            "deleted_at": now_utc,
                        }
                    },
                    upsert=True,
                )
        except Exception:
            pass

    def delete_product(
        self,
        platform: str,
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
    ) -> bool:
        """软下架已确认取消推广或下架的商品记录，避免首页/爆单库或详情降级继续展示失效商品。"""
        plat = self._platform(platform)
        gid = self._real_goods_id(goods_id)
        gsign = str(goods_sign or "").strip()
        if not gid and not gsign:
            return False
        filter_spec = self._identity_filter(plat, gid, gsign)
        try:
            matched_docs: List[Dict[str, Any]] = []
            try:
                matched_docs = list(self.col.find(filter_spec, {"_id": 0, "product_id": 1, "goods_id": 1, "goods_sign": 1}))
            except Exception:
                matched_docs = [{"product_id": gid, "goods_sign": gsign}] if gid else []
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            res = self.col.update_many(
                filter_spec,
                {
                    "$set": {"is_off_promotion": True, "off_promotion_at": now_utc},
                    "$unset": {"default_promotion": "", "default_promotion_expires_at": ""},
                },
            )
            raw_mod = getattr(res, "modified_count", 0)
            modified = int(raw_mod) if isinstance(raw_mod, (int, float)) else 0
            deleted_pids: List[str] = []
            seen_pids: Set[str] = set()
            for d in matched_docs:
                pid = self._real_goods_id(d.get("product_id") or d.get("goods_id"))
                if pid and pid not in seen_pids:
                    seen_pids.add(pid)
                    deleted_pids.append(pid)
            if gid and gid not in seen_pids:
                seen_pids.add(gid)
                deleted_pids.append(gid)
            tombstone_docs = matched_docs or ([{"product_id": gid, "goods_sign": gsign}] if gid else [])
            if tombstone_docs and deleted_pids:
                self._record_deletion_tombstones(plat, tombstone_docs)
            if modified > 0:
                logger.info(
                    "【失效商品软下架】已将取消推广/下架商品标记软隔离 | 平台[%s] 商品[%s] 影响条数[%d]",
                    plat, gid or gsign, modified,
                )
            return modified > 0 or len(matched_docs) > 0
        except Exception as e:
            logger.warning(
                "❌ 【失效商品清理异常】从products集合软下架商品失败 | 平台[%s] 商品[%s] 异常<%s>",
                plat, gid or gsign, format_exc_summary(e),
            )
            return False

    def mark_product_superseded(
        self,
        platform: str,
        old_goods_id: str,
        new_goods_id: str,
        old_goods_sign: Optional[str] = None,
    ) -> bool:
        """
        将因主图变更、加盐签名或规格迁移被新主键替代的旧商品软隔离（置为无效），
        记录替代关系 (superseded_by)，同时保留旧记录供排查审计，避免首页/榜单/搜索继续展示老旧价格。
        """
        plat = self._platform(platform)
        old_id = self._real_goods_id(old_goods_id)
        new_id = self._real_goods_id(new_goods_id)
        old_sign = str(old_goods_sign or "").strip()
        if not old_id and not old_sign:
            return False
        if old_id == new_id:
            return False

        filter_spec = {"platform": plat}
        if old_id and old_sign and old_id != old_sign:
            filter_spec["$or"] = [{"product_id": old_id}, {"goods_sign": old_sign}]
        else:
            target = old_id or old_sign
            filter_spec["$or"] = [{"product_id": target}, {"goods_sign": target}]

        # 确保不误伤新商品自身
        filter_spec["product_id"] = {"$ne": new_id}

        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            res = self.col.update_many(
                filter_spec,
                {
                    "$set": {
                        "is_off_promotion": True,
                        "off_promotion_reason": "superseded",
                        "superseded_by": new_id,
                        "off_promotion_at": now_utc,
                    },
                    "$unset": {"default_promotion": "", "default_promotion_expires_at": ""},
                },
            )
            raw_mod = getattr(res, "modified_count", 0)
            modified = int(raw_mod) if isinstance(raw_mod, (int, float)) else 0
            if modified > 0:
                logger.info(
                    "【商品身份迁移软下架】旧商品已被新商品主键替代，已置为无效并记录映射 | 平台[%s] 旧商品[%s] 新商品[%s] 影响条数[%d]",
                    plat, old_id or old_sign, new_id, modified,
                )
            return modified > 0
        except Exception as e:
            logger.warning(
                "❌ 【商品身份迁移异常】软下架旧商品记录失败 | 平台[%s] 旧商品[%s] 新商品[%s] 异常<%s>",
                plat, old_id or old_sign, new_id, format_exc_summary(e),
            )
            return False
