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
from bson.decimal128 import Decimal128
from repositories.base_repo import BaseRepository
from models.product import clean_product_id, normalize_platform
from config import PROMOTION_LINK_TTL, logger


class ProductRepository(BaseRepository):
    COLLECTION_NAME = "products"

    def _init_indexes(self):
        try:
            self.col.create_index([("platform", 1), ("product_id", 1)], unique=True, background=True)
            self.col.create_index("goods_sign", background=True)
            self.col.create_index([("updated_at", -1)], background=True)
            self.col.create_index([("platform", 1), ("updated_at", -1)], background=True)
            return True
        except Exception as e:
            logger.warning(f"商品集合索引创建提示: {e}")
            return False

    # 内部占位只用于兼容现有 unique(platform, product_id) 索引，不能当作真实 ID 返回。
    _SIGN_ID_PREFIX = "sign:"
    _platform = staticmethod(normalize_platform)
    _SUMMARY_PROJECTION = {
        "_id": 0,
        **dict.fromkeys((
            "platform", "product_id", "goods_id", "goods_sign", "name", "brand",
            "category", "activity_price", "price", "original_price", "saved_price",
            "coupon_amount", "has_coupon", "commission_rate", "commission_amount",
            "sales_tip", "image_url", "mall_name", "updated_at",
        ), 1),
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
        platform = cls._platform(item.get("platform"))
        goods_id = cls._real_goods_id(item.get("goods_id")) or cls._real_goods_id(item.get("product_id"))
        goods_sign = str(item.get("goods_sign") or "").strip()
        return platform, goods_id, goods_sign

    @staticmethod
    def _identity_filter(platform: str, goods_id: str, goods_sign: str) -> Dict[str, Any]:
        if goods_id and goods_sign:
            # 同时匹配 ID 与 Sign-only 旧记录，补到真实 ID 时更新原文档。
            return {"platform": platform, "$or": [{"product_id": goods_id}, {"goods_sign": goods_sign}]}
        if goods_id:
            return {"platform": platform, "product_id": goods_id}
        return {"platform": platform, "goods_sign": goods_sign}

    @classmethod
    def _identity_update(cls, platform: str, goods_id: str, goods_sign: str):
        update_set = {"platform": platform}
        insert_set = {}
        if goods_id:
            update_set["product_id"] = goods_id
        else:
            # $setOnInsert 防止不带 ID 的摘要抹掉已有真实 ID。
            insert_set["product_id"] = cls._SIGN_ID_PREFIX + goods_sign
        if goods_sign:
            update_set["goods_sign"] = goods_sign
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
                update_set[target] = str(value)

        update_set.update({
            "updated_at": now,
            "need_reformat": False,
            "format_status": "success",
            "format_retry_count": 999,
        })
        if item.get("source") is not None:
            update_set["source"] = item["source"]
        elif not detail:
            update_set["source"] = f"{platform}_recommend_stream"
        if not detail and channel_type is not None:
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
                "desc", "carousel_images", "product_url", "specs", "detail_tags",
                "unified_tags", "service_guarantees", "shop_ratings", "coupon_info", "search_id",
            ):
                if key in item and item[key] is not None:
                    update_set[key] = deepcopy(item[key])
        return self._identity_filter(platform, goods_id, goods_sign), {"$set": update_set, "$setOnInsert": insert_set}

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
        分页查询爆单库商品（游标探测无慢查询 count_documents）
        """
        page = max(1, min(50, int(page or 1)))
        page_size = max(1, min(50, int(page_size or 20)))
        skip = (page - 1) * page_size

        filter_query: Dict[str, Any] = {}

        if platform and self._platform(platform) != "all":
            filter_query["platform"] = self._platform(platform)

        if category and category.strip() and category != "全部":
            filter_query["category"] = category.strip()

        if keyword and keyword.strip():
            safe_kw = re.escape(keyword.strip()[:30])
            filter_query["name"] = {"$regex": safe_kw, "$options": "i"}

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
                    {"$and": [filter_query, self._displayable_content_filter()]},
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
            logger.error(f"[ProductRepository.query_boom_products] 查询异常: {e}")
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
        coupon_amount = self._amount_float(self._first_present(doc, "coupon_amount", "saved_price", default=0.0))
        commission_rate, commission_amount = self._calc_commission(activity_price, doc.get("commission_amount"))
        if doc.get("commission_rate") is not None:
            commission_rate = self._amount_float(doc["commission_rate"])
        updated_at_dt = doc.get("updated_at")
        updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")
        return {
            "platform": platform,
            "platform_name": platform_name(platform),
            "product_id": goods_id,
            "goods_id": goods_id,
            "goods_sign": goods_sign,
            "name": doc.get("name", "未命名商品"),
            "brand": doc.get("brand") or "",
            "category": doc.get("category") or "",
            "price": round(activity_price, 2),
            "original_price": round(original_price, 2),
            "saved_price": round(saved_price, 2),
            "coupon_amount": round(coupon_amount, 2),
            "has_coupon": bool(doc["has_coupon"]) if doc.get("has_coupon") is not None else coupon_amount > 0,
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
            "sales_tip": str(self._first_present(doc, "sales_tip", default="0")),
            "image_url": doc.get("image_url") or "",
            "mall_name": doc.get("mall_name") or "",
            "updated_at": updated_at_str,
        }

    def _normalize_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """返回存储的真实详情；未知商业字段保持空，不捏造优惠券/承诺/评分。"""
        result = self._normalize_doc_summary(doc)
        gallery = doc.get("carousel_images")
        clean_gallery = [u for u in gallery if isinstance(u, str) and u.startswith("http")] if isinstance(gallery, list) else []
        main_img = result["image_url"]
        if not clean_gallery and isinstance(main_img, str) and main_img.startswith("http"):
            clean_gallery = [main_img]
        result.update({
            "desc": doc.get("desc") or "",
            "brand_name": result["brand"],
            "category_name": result["category"],
            "carousel_images": list(dict.fromkeys(clean_gallery)),
            "product_url": doc.get("product_url") or "",
            "detail_tags": deepcopy(doc.get("detail_tags") or []),
            "unified_tags": deepcopy(doc.get("unified_tags") or []),
            "service_guarantees": deepcopy(doc.get("service_guarantees") or []),
            "shop_ratings": deepcopy(doc.get("shop_ratings") or {}),
            "specs": deepcopy(doc.get("specs") or []),
            "coupon_info": deepcopy(doc.get("coupon_info") or {}),
            "search_id": doc.get("search_id") or "",
            # 不把摘要 updated_at 冒充详情抓取时间；服务层负责过期决策。
            "detail_updated_at": doc.get("detail_updated_at"),
        })
        return result

    def get_product_detail(
        self, goods_id_or_sign: str, platform: str = "pdd", key_type: Optional[str] = None, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """按平台隔离查询；key_type 可为 goods_id/product_id/id 或 goods_sign/sign。"""
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
            logger.warning(f"从 Mongo 反查商品详情失败: {e}")
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
            logger.warning(f"从 Mongo 反查商品身份失败: {e}")
            return None

    def get_categories(self, platform: str = "all") -> List[str]:
        """获取类目列表"""
        filter_query = {}
        if platform and self._platform(platform) != "all":
            filter_query["platform"] = self._platform(platform)
        try:
            cats = self.col.distinct("category", {"$and": [filter_query, self._displayable_content_filter()]})
            return [c for c in cats if c and str(c).strip()]
        except Exception as e:
            logger.warning(f"获取类目失败: {e}")
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

        operations = []
        for item in goods_list:
            if not isinstance(item, dict):
                continue
            mapped = self._build_product_update(item, detail=False, channel_type=channel_type)
            if mapped is not None:
                filter_spec, update = mapped
                operations.append(UpdateOne(filter_spec, update, upsert=True))

        if not operations:
            return 0

        try:
            result = self.col.bulk_write(operations, ordered=False)
            upserted_count = result.upserted_count + result.modified_count
            logger.info(f"[ProductRepository.upsert_home_products] 成功入库/更新 {upserted_count} 件首页推荐商品")
            return upserted_count
        except Exception as e:
            logger.warning(f"[ProductRepository.upsert_home_products] 批量入库异常: {e}")
            return 0

    def upsert_product_detail(self, detail: Dict[str, Any]) -> bool:
        """将深度商品详情写入或更新到数据库"""
        if not detail or not isinstance(detail, dict):
            return False
        mapped = self._build_product_update(detail, detail=True)
        if mapped is None:
            return False
        filter_spec, update = mapped
        try:
            self.col.update_one(filter_spec, update, upsert=True)
            return True
        except Exception as e:
            logger.warning(f"[ProductRepository.upsert_product_detail] 详情入库异常: {e}")
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
            logger.warning(f"读取 products 表官方推广缓存异常: {e}")
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
            logger.warning(f"保存 products 表官方推广缓存异常: {e}")
            return False
