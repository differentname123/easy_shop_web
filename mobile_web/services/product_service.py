# -*- coding: utf-8 -*-
"""商品应用服务：平台调度、缓存与本地商品库编排，不解释平台原始字段。"""
import math
import time
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from adapters import get_adapter, normalize_platform
from models.product import ProductRef
from repositories.product_repo import ProductRepository
from cache import RECOMMEND_CACHE, DETAIL_CACHE, SEARCH_CACHE, CATEGORY_CACHE
from config import PRODUCT_DETAIL_TTL, logger

# 兼容统一缓存分域别名，供测试及向后兼容使用
_RECOMMEND_CACHE = RECOMMEND_CACHE
_DETAIL_CACHE = DETAIL_CACHE
_SEARCH_CACHE = SEARCH_CACHE
_CATEGORY_CACHE = CATEGORY_CACHE


class ProductService:
    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo if product_repo is not None else ProductRepository()

    def get_home_recommendations(self, channel_type=5, offset=0, limit=20, uid=None,
                                 platform="pdd", force_refresh=False) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        cache_key = (plat, uid or "", channel_type, offset, limit)
        if not force_refresh:
            cached = RECOMMEND_CACHE.get(cache_key)
            if cached is not None:
                return cached
        result = adapter.get_recommend_goods(channel_type=channel_type, offset=offset, limit=limit, uid=uid)
        if result.get("status") == "success":
            items = result.get("data") or []
            result.setdefault("has_more", len(items) >= limit)
            RECOMMEND_CACHE.set(cache_key, result)
            if items:
                def _bg_upsert():
                    try:
                        self.repo.upsert_home_products(items, channel_type=channel_type)
                    except Exception as exc:
                        logger.warning(f"推荐商品入库失败，不影响在线结果: {exc}")
                threading.Thread(target=_bg_upsert, name="bg-upsert-home-products", daemon=True).start()
        return result

    def search_home_goods(self, keyword, page=1, page_size=20, uid=None, platform="pdd") -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        keyword = (keyword or "").strip()[:30]
        cache_key = (plat, uid or "", keyword, page, page_size)
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            return cached
        result = adapter.search_goods(keyword=keyword, page=page, page_size=page_size, uid=uid)
        if result.get("status") == "success":
            SEARCH_CACHE.set(cache_key, result)
        return result

    @staticmethod
    def _detail_expires_at(item: dict) -> float:
        # 摘要时间不能延长完整详情的寿命；Mongo 的 naive 详情时间遵循 UTC 契约。
        updated = item.get("detail_updated_at")
        if isinstance(updated, bool):
            return 0.0
        if isinstance(updated, datetime):
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            timestamp = updated.timestamp()
        elif isinstance(updated, (int, float)):
            timestamp = float(updated)
        elif isinstance(updated, str):
            try:
                parsed = datetime.fromisoformat(updated)
                timestamp = (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                return 0.0
        else:
            return 0.0
        if not math.isfinite(timestamp) or timestamp > time.time():
            return 0.0
        return timestamp + PRODUCT_DETAIL_TTL

    @classmethod
    def _detail_is_fresh(cls, item: dict) -> bool:
        return time.time() < cls._detail_expires_at(item)

    @staticmethod
    def _database_detail(item: dict, source: str) -> dict:
        data = deepcopy(item)
        data.pop("detail_updated_at", None)
        return {"status": "success", "data": data, "source": source}

    @staticmethod
    def _cache_detail(platform: str, result: dict, expires_at: float, uid=None):
        """缓存私有截止时间及可信别名，不将回填时刻当成新的来源时间。"""
        item = result.get("data") or {}
        ref = ProductRef(platform, item.get("goods_id") or item.get("product_id"), item.get("goods_sign"))
        entry = (expires_at, deepcopy(result))
        for key in ref.cache_keys(uid):
            DETAIL_CACHE.set(key, entry)

    def get_goods_detail(self, platform="pdd", goods_id_or_sign="", goods_id=None,
                         goods_sign=None, uid=None, **kwargs) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        if adapter.active is False:
            return adapter.get_goods_detail(goods_id_or_sign=goods_id_or_sign)
        ref = adapter.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        if not ref.key:
            return {"status": "error", "error_msg": "请提供商品 ID 或 Sign", "error_code": "missing_product_identity"}

        # 同时提供不一致的 Sign/ID 时只按主标识查，不在另一个键中命中错误商品。
        lookup_ref = ProductRef(plat, "" if ref.goods_sign else ref.goods_id, ref.goods_sign)
        for key in lookup_ref.cache_keys(uid):
            cached = DETAIL_CACHE.get(key)
            if isinstance(cached, tuple) and len(cached) == 2 and time.time() < cached[0]:
                return deepcopy(cached[1])
            if cached is not None:
                DETAIL_CACHE.pop(key)

        item = self.repo.get_product_detail(ref.key, platform=plat, key_type="sign" if ref.goods_sign else "id")
        expires_at = self._detail_expires_at(item) if item else 0.0
        if time.time() < expires_at:
            result = self._database_detail(item, "database")
            self._cache_detail(plat, result, expires_at, uid)
            return result

        result = adapter.get_goods_detail(goods_id_or_sign=ref.key, goods_id=ref.goods_id or None,
                                          goods_sign=ref.goods_sign or None, uid=uid, **kwargs)
        if result.get("status") == "success" and result.get("data"):
            data = result["data"]
            if normalize_platform(data.get("platform") or plat) != plat:
                return {"status": "error", "error_msg": "商品返回平台不匹配", "error_code": "product_identity_mismatch"}
            data.setdefault("platform", plat)
            fetched_at = datetime.fromtimestamp(time.time(), timezone.utc)
            persisted = dict(data, detail_updated_at=fetched_at)
            try:
                self.repo.upsert_product_detail(persisted)
            except Exception as exc:
                logger.warning(f"商品详情入库失败，不影响在线结果: {exc}")
            self._cache_detail(plat, result, fetched_at.timestamp() + PRODUCT_DETAIL_TTL, uid)
            return result
        if item:
            # 上游故障仅作本次降级，不缓存整段 TTL，以便恢复后立即重试最新价格。
            return self._database_detail(item, "database_fallback")
        return result

    def get_boom_products(self, platform="all", category=None, keyword=None, sort_by="latest",
                          page=1, page_size=20) -> Dict[str, Any]:
        return self.repo.query_boom_products(platform=normalize_platform(platform, "all"), category=category,
                                              keyword=keyword, sort_by=sort_by, page=page, page_size=page_size)

    def get_boom_categories(self, platform="all") -> List[str]:
        plat = normalize_platform(platform, "all")
        cached = CATEGORY_CACHE.get(plat)
        if cached is not None:
            return cached
        categories = self.repo.get_categories(platform=plat)
        if categories:
            CATEGORY_CACHE.set(plat, categories)
        return categories
