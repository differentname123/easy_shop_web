# -*- coding: utf-8 -*-
"""
全局线程安全有界 LRU-TTL 缓存容器与分域管理
"""
from cache_utils import ThreadSafeTTLCache
from config import (
    CACHE_MAX_RECOMMEND,
    CACHE_MAX_DETAIL,
    CACHE_MAX_SEARCH,
    CACHE_MAX_CATEGORY,
    CACHE_MAX_PROMOTION,
    PRODUCT_DETAIL_TTL,
)

# 统一分域缓存池定义
RECOMMEND_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_RECOMMEND, ttl=300)   # 首页推荐流 5 分钟
DETAIL_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=PRODUCT_DETAIL_TTL)         # 商品详情 10 分钟
SEARCH_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_SEARCH, ttl=180)         # 关键词搜索 3 分钟
CATEGORY_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_CATEGORY, ttl=600)     # 爆单库类目 10 分钟
PROMOTION_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_PROMOTION, ttl=1800)  # 转链口令 30 分钟


def clear_all_caches():
    """方便单测与集成测试快速重置所有缓存"""
    RECOMMEND_CACHE.clear()
    DETAIL_CACHE.clear()
    SEARCH_CACHE.clear()
    CATEGORY_CACHE.clear()
    PROMOTION_CACHE.clear()
