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
# 公开商品身份到上游物料URL；不是完整详情或个人推广链接。
MATERIAL_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=PRODUCT_DETAIL_TTL)
# 规范化静态唯一标识 (platform, goods_id) 到上游动态混淆签名 (goods_sign) 的热映射
SIGN_BY_ID_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=PRODUCT_DETAIL_TTL)
# 已确认下架/取消推广的商品防复活黑名单（30分钟内拦截榜单滞后快照重新入库或展示）
OFF_PROMOTION_CACHE = ThreadSafeTTLCache(maxsize=2048, ttl=1800)


def is_product_off_promotion(platform: str, goods_id: str = "", goods_sign: str = "") -> bool:
    plat = str(platform or "").strip().lower()
    gid = str(goods_id or "").strip()
    gsign = str(goods_sign or "").strip()
    if gid and (plat, gid) in OFF_PROMOTION_CACHE:
        return True
    if gsign and (plat, gsign) in OFF_PROMOTION_CACHE:
        return True
    return False


def evict_product_from_caches(platform: str, goods_id: str = "", goods_sign: str = "") -> None:
    """当上游明确裁决商品已取消推广或下架时，从全内存缓存域驱逐并登记防复活黑名单。"""
    plat = str(platform or "").strip().lower()
    targets = {str(x).strip() for x in (goods_id, goods_sign) if str(x or "").strip()}
    if not plat or not targets:
        return
    for t in targets:
        OFF_PROMOTION_CACHE.set((plat, t), True)
        MATERIAL_CACHE.pop((plat, "id", t), None)
        MATERIAL_CACHE.pop((plat, "sign", t), None)
        SIGN_BY_ID_CACHE.pop((plat, t), None)
    for dkey in DETAIL_CACHE.keys():
        if isinstance(dkey, tuple) and len(dkey) >= 4 and dkey[0] == plat and dkey[3] in targets:
            DETAIL_CACHE.pop(dkey, None)
    for pkey in PROMOTION_CACHE.keys():
        if isinstance(pkey, tuple) and len(pkey) >= 4 and pkey[1] == plat and pkey[3] in targets:
            PROMOTION_CACHE.pop(pkey, None)
    for list_cache in (RECOMMEND_CACHE, SEARCH_CACHE):
        for lkey in list_cache.keys():
            if not (isinstance(lkey, tuple) and len(lkey) >= 1 and lkey[0] == plat):
                continue
            cached = list_cache.get(lkey)
            if not isinstance(cached, dict) or not isinstance(cached.get("data"), list):
                continue
            old_items = cached["data"]
            new_items = [
                item for item in old_items
                if not (
                    isinstance(item, dict)
                    and (
                        str(item.get("goods_id") or "").strip() in targets
                        or str(item.get("goods_sign") or "").strip() in targets
                        or str(item.get("product_id") or "").strip() in targets
                    )
                )
            ]
            if len(new_items) != len(old_items):
                updated = dict(cached, data=new_items)
                list_cache.set(lkey, updated)


def clear_all_caches():
    """方便单测与集成测试快速重置所有缓存"""
    RECOMMEND_CACHE.clear()
    DETAIL_CACHE.clear()
    SEARCH_CACHE.clear()
    CATEGORY_CACHE.clear()
    PROMOTION_CACHE.clear()
    MATERIAL_CACHE.clear()
    SIGN_BY_ID_CACHE.clear()
    OFF_PROMOTION_CACHE.clear()
