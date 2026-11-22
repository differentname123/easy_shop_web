# -*- coding: utf-8 -*-
"""
全局线程安全有界 LRU-TTL 缓存容器与分域管理
"""
import math
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from cache_utils import ThreadSafeTTLCache
from config import (
    CACHE_MAX_RECOMMEND,
    CACHE_MAX_DETAIL,
    CACHE_MAX_SEARCH,
    CACHE_MAX_CATEGORY,
    CACHE_MAX_PROMOTION,
    PRODUCT_DETAIL_TTL,
)
from models.product import DetailSnapshot, ProductRef, is_jd_image_key, resolve_item_identity

_DETAIL_CACHE_LOCK = threading.RLock()

# 统一分域缓存池定义
RECOMMEND_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_RECOMMEND, ttl=300)   # 首页推荐流 5 分钟
DETAIL_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=PRODUCT_DETAIL_TTL)         # 商品详情 10 分钟
SEARCH_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_SEARCH, ttl=180)         # 关键词搜索 3 分钟
CATEGORY_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_CATEGORY, ttl=600)     # 爆单库类目 10 分钟
BOOM_SORT_CACHE = ThreadSafeTTLCache(maxsize=64, ttl=120)                    # 爆单库性价比全局排序快照 2 分钟
PROMOTION_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_PROMOTION, ttl=1800)  # 转链口令 30 分钟
# 公开商品身份到上游物料URL；不是完整详情或个人推广链接。
MATERIAL_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=PRODUCT_DETAIL_TTL)
# 规范化静态唯一标识 (platform, goods_id) 到上游动态混淆签名 (goods_sign) 的热映射
SIGN_BY_ID_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=PRODUCT_DETAIL_TTL)
# 已确认下架/取消推广的商品防复活黑名单（30分钟内拦截榜单滞后快照重新入库或展示）
OFF_PROMOTION_CACHE = ThreadSafeTTLCache(maxsize=2048, ttl=1800)
# 线报视图的权威短缓存：详情缓存可能存着转链/上游拼出的浅快照（不含线报），
# 每次对外返回前用这份 60 秒投影结果覆盖，保证线报合入后最迟一分钟可见。
# 条目值为 (found, deal_tip)：found=False 表示正式表无此文档，此时不改写详情 payload。
DEAL_TIP_CACHE = ThreadSafeTTLCache(maxsize=4096, ttl=60)


def is_product_off_promotion(platform: str, goods_id: str = "", goods_sign: str = "") -> bool:
    plat = str(platform or "").strip().lower()
    gid = str(goods_id or "").strip()
    gsign = str(goods_sign or "").strip()
    if gid and (plat, gid) in OFF_PROMOTION_CACHE:
        return True
    if gsign and (plat, gsign) in OFF_PROMOTION_CACHE:
        return True
    return False


def clear_product_off_promotion(platform: str, goods_id: str = "", goods_sign: str = "") -> None:
    """当重新抓取到商品最新在售数据时，将其从防复活黑名单中移出以实现自愈复活。"""
    plat = str(platform or "").strip().lower()
    targets = {str(x).strip() for x in (goods_id, goods_sign) if str(x or "").strip()}
    if not plat or not targets:
        return
    for t in targets:
        OFF_PROMOTION_CACHE.pop((plat, t), None)


def evict_product_from_caches(platform: str, goods_id: str = "", goods_sign: str = "", blacklist: bool = True) -> None:
    """当上游明确裁决商品已取消推广或下架时，从全内存缓存域驱逐并登记防复活黑名单。若 blacklist=False，则只清理各级展示与详情缓存，不记录黑名单以支持透明转发。"""
    plat = str(platform or "").strip().lower()
    targets = {str(x).strip() for x in (goods_id, goods_sign) if str(x or "").strip()}
    if not plat or not targets:
        return
    for t in targets:
        if blacklist:
            OFF_PROMOTION_CACHE.set((plat, t), True)
        MATERIAL_CACHE.pop((plat, "id", t), None)
        MATERIAL_CACHE.pop((plat, "sign", t), None)
        SIGN_BY_ID_CACHE.pop((plat, t), None)
    for dkey in DETAIL_CACHE.keys():
        if not isinstance(dkey, tuple):
            continue
        if len(dkey) >= 5 and dkey[0] == "detail" and dkey[1] == plat and dkey[4] in targets:
            DETAIL_CACHE.pop(dkey, None)
    for pkey in PROMOTION_CACHE.keys():
        if isinstance(pkey, tuple) and len(pkey) >= 4 and pkey[1] == plat and pkey[3] in targets:
            PROMOTION_CACHE.pop(pkey, None)
    for bkey in BOOM_SORT_CACHE.keys():
        if not (isinstance(bkey, tuple) and len(bkey) >= 1 and bkey[0] in (plat, "all")):
            continue
        cached_list = BOOM_SORT_CACHE.get(bkey)
        if not isinstance(cached_list, list):
            continue
        filtered_list = [
            item for item in cached_list
            if not (
                isinstance(item, dict)
                and str(item.get("platform") or "").strip().lower() == plat
                and (
                    str(item.get("goods_id") or "").strip() in targets
                    or str(item.get("goods_sign") or "").strip() in targets
                    or str(item.get("product_id") or "").strip() in targets
                )
            )
        ]
        if len(filtered_list) != len(cached_list):
            BOOM_SORT_CACHE.set(bkey, filtered_list)
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


def is_valid_image_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    return raw.startswith(("https://", "http://", "/")) and len(raw) > 1


def has_valid_product_images(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if is_valid_image_url(item.get("image_url")):
        return True
    gallery = item.get("carousel_images")
    if isinstance(gallery, list):
        return any(is_valid_image_url(u) for u in gallery)
    return False


def parse_detail_deadline(updated: Any) -> float:
    if isinstance(updated, bool):
        return 0.0
    if isinstance(updated, datetime):
        updated = updated.replace(tzinfo=timezone.utc) if updated.tzinfo is None else updated
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


def snapshot_detail_deadline(snapshot: DetailSnapshot) -> float:
    if not has_valid_product_images(snapshot.data):
        return 0.0
    return parse_detail_deadline(snapshot.fetched_at)


def is_valid_detail_cache_entry(entry: Any) -> bool:
    return (
        isinstance(entry, tuple)
        and len(entry) == 2
        and isinstance(entry[0], (int, float))
        and not isinstance(entry[0], bool)
        and math.isfinite(entry[0])
        and time.time() < entry[0] <= time.time() + PRODUCT_DETAIL_TTL
        and isinstance(entry[1], dict)
        and entry[1].get("status") == "success"
        and has_valid_product_images(entry[1].get("data"))
    )


def make_detail_snapshot(data: dict, fetched_at: datetime, platform: str) -> DetailSnapshot:
    payload = dict(data)
    actual_platform, gid, sign = resolve_item_identity(payload, default_platform=platform)
    ref = ProductRef(actual_platform, gid, sign)
    payload.update(platform=actual_platform, goods_id=ref.goods_id, goods_sign=ref.goods_sign)
    return DetailSnapshot(ref, payload, fetched_at)


def get_cached_deal_tip(platform: str, product_id: str):
    """读取线报视图短缓存；返回 (state, deal_tip) 或 None（未缓存）。"""
    return DEAL_TIP_CACHE.get(("deal_tip", str(platform or ""), str(product_id or "")))


def cache_deal_tip(platform: str, product_id: str, state: str, deal_tip) -> None:
    """写入线报视图短缓存；只缓存确定的结论（ok/missing），error 不缓存以便重试。"""
    if state not in ("ok", "missing"):
        return
    DEAL_TIP_CACHE.set(("deal_tip", str(platform or ""), str(product_id or "")), (state, deepcopy(deal_tip)))


def clear_cached_deal_tip(platform: str = "", product_id: str = "") -> None:
    """线报合并/过期后可主动失效；空参数表示清空整区。"""
    if not platform and not product_id:
        DEAL_TIP_CACHE.clear()
        return
    for key in DEAL_TIP_CACHE.keys():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        if (not platform or key[1] == platform) and (not product_id or key[2] == product_id):
            DEAL_TIP_CACHE.pop(key, None)


def cache_product_detail(
    platform: str,
    result: dict,
    expires_at: float,
    uid: Optional[str] = None,
    verified_ref: Optional[ProductRef] = None,
) -> dict:
    item = result.get("data") or {}
    ref = ProductRef(platform, item.get("goods_id") or item.get("product_id"), item.get("goods_sign"))
    if (
        ref.goods_id
        and ref.goods_sign
        and ref.goods_id != ref.goods_sign
        and not (ref.platform == "jd" and is_jd_image_key(ref.goods_sign))
        and not (ref.platform == "pdd" and ref.goods_sign.isdigit())
    ):
        SIGN_BY_ID_CACHE.set((ref.platform, ref.goods_id), ref.goods_sign)
    url = item.get("product_url")
    if isinstance(url, str) and url.strip():
        clean_url = url.strip()
        if ref.goods_sign:
            MATERIAL_CACHE.set((ref.platform, "sign", ref.goods_sign), clean_url)
        if ref.goods_id:
            MATERIAL_CACHE.set((ref.platform, "id", ref.goods_id), clean_url)
        if isinstance(verified_ref, ProductRef) and verified_ref.platform == ref.platform:
            if verified_ref.goods_sign:
                MATERIAL_CACHE.set((verified_ref.platform, "sign", verified_ref.goods_sign), clean_url)
            if verified_ref.goods_id:
                MATERIAL_CACHE.set((verified_ref.platform, "id", verified_ref.goods_id), clean_url)
    keys = ref.cache_keys(uid)
    if (
        isinstance(verified_ref, ProductRef)
        and verified_ref.platform == ref.platform
        and ref.goods_id
        and ref.goods_id == verified_ref.goods_id
    ):
        keys = list(dict.fromkeys(keys + verified_ref.cache_keys(uid)))
    if not math.isfinite(expires_at) or expires_at <= time.time() or not has_valid_product_images(item):
        return deepcopy(result)
    entry = (expires_at, deepcopy(result))
    with _DETAIL_CACHE_LOCK:
        # A newer trusted alias wins for the whole snapshot, not just one key.
        for key in keys:
            current = DETAIL_CACHE.get(key)
            if is_valid_detail_cache_entry(current) and current[0] > entry[0]:
                entry = current
        for key in keys:
            DETAIL_CACHE.set(key, (entry[0], deepcopy(entry[1])))
    return deepcopy(entry[1])


def get_cached_product_detail(ref: ProductRef, uid: Optional[str] = None):
    primary = ProductRef(ref.platform, "" if ref.goods_sign else ref.goods_id, ref.goods_sign)
    keys = list(primary.cache_keys(uid))
    if ref.platform in ("taobao", "jd") and ref.goods_id and ref.goods_sign == ref.goods_id:
        id_key = ("detail", ref.platform, uid or "", "id", ref.goods_id)
        if id_key not in keys:
            keys.append(id_key)
    with _DETAIL_CACHE_LOCK:
        for key in keys:
            cached = DETAIL_CACHE.get(key)
            if is_valid_detail_cache_entry(cached):
                return cached[0], deepcopy(cached[1])
            if cached is not None:
                DETAIL_CACHE.pop(key)
    return None


def clear_all_caches():
    """方便单测与集成测试快速重置所有缓存"""
    RECOMMEND_CACHE.clear()
    DETAIL_CACHE.clear()
    SEARCH_CACHE.clear()
    CATEGORY_CACHE.clear()
    BOOM_SORT_CACHE.clear()
    PROMOTION_CACHE.clear()
    MATERIAL_CACHE.clear()
    SIGN_BY_ID_CACHE.clear()
    OFF_PROMOTION_CACHE.clear()
    DEAL_TIP_CACHE.clear()
