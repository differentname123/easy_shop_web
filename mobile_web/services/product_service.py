# -*- coding: utf-8 -*-
"""商品应用服务：平台调度、详情来源时间及前后台共用的商品解析。"""
import math
import json
import time
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from adapters import get_adapter, normalize_platform
from models.product import ProductRef, DetailSnapshot, DetailPrefetchResult, utc_millis
from repositories.product_repo import ProductRepository
from cache import RECOMMEND_CACHE, DETAIL_CACHE, SEARCH_CACHE, CATEGORY_CACHE, MATERIAL_CACHE
from config import PRODUCT_DETAIL_TTL, logger

_RECOMMEND_CACHE = RECOMMEND_CACHE
_DETAIL_CACHE = DETAIL_CACHE
_SEARCH_CACHE = SEARCH_CACHE
_CATEGORY_CACHE = CATEGORY_CACHE
_DETAIL_CACHE_LOCK = threading.RLock()


@dataclass
class _LoadedDetail:
    public: dict
    snapshot: Optional[DetailSnapshot] = None
    persisted: bool = False
    retryable: bool = False


class ProductService:
    # 摘要只是缓存性写入：无排队，满载时跳过，避免每个榜单请求各自启动线程。
    _MAX_BACKGROUND_WRITES = 2

    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo if product_repo is not None else ProductRepository()
        self.prefetcher = None
        self._flight_lock = threading.Lock()
        self._flights = {}
        self._write_lock = threading.RLock()
        self._background_writes = set()
        self._summary_write_slots = threading.BoundedSemaphore(self._MAX_BACKGROUND_WRITES)
        self._writes_open = True

    @staticmethod
    def _capability_error(adapter, capability):
        if getattr(adapter, "active", True) is False:
            return {"status": "error", "error_code": "platform_unavailable",
                    "error_msg": f"{adapter.platform_name}暂未启用", "data": []}
        capabilities = getattr(adapter, "capabilities", None)
        if isinstance(capabilities, dict) and capabilities.get(capability) is False:
            return {"status": "error", "error_code": "capability_unavailable",
                    "error_msg": f"{adapter.platform_name}当前功能尚未配置", "data": []}
        return None

    def start_background_writes(self):
        with self._write_lock:
            self._writes_open = True

    def wait_for_background_writes(self):
        with self._write_lock:
            self._writes_open = False
        while True:
            with self._write_lock:
                pending = tuple(self._background_writes)
            if not pending:
                return
            for thread in pending:
                thread.join()

    def _save_summary_in_background(self, items, channel_type):
        def save():
            try:
                self.repo.upsert_home_products(items, channel_type=channel_type)
            except Exception as exc:
                logger.warning("推荐摘要入库失败: %s", type(exc).__name__)
            finally:
                with self._write_lock:
                    self._background_writes.discard(thread)
                    self._summary_write_slots.release()
        with self._write_lock:
            if not self._writes_open:
                return
            if not self._summary_write_slots.acquire(blocking=False):
                logger.warning("推荐摘要后台写入满载，跳过缓存性入库: limit=%s", self._MAX_BACKGROUND_WRITES)
                return
            thread = None
            try:
                thread = threading.Thread(target=save, name="bg-upsert-home-products", daemon=True)
                self._background_writes.add(thread)
                thread.start()
            except Exception:
                self._background_writes.discard(thread)
                self._summary_write_slots.release()
                logger.warning("推荐摘要后台任务未能启动")

    def _offer_products(self, items, channel_type=None, snapshots=()):
        # 转链所需公开映射先发布，不能被可丢弃的后台摘要写入或预取开关阻断。
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = ProductRef(item.get("platform"), item.get("goods_id"), item.get("goods_sign"))
            url = item.get("product_url")
            if ref.key and isinstance(url, str) and url.strip():
                kind = "sign" if ref.goods_sign else "id"
                MATERIAL_CACHE.set((ref.platform, kind, ref.key), url.strip())
        if self.prefetcher is not None:
            try:
                self.prefetcher.offer_products(items, channel_type=channel_type, snapshots=snapshots)
            except Exception as exc:
                logger.warning("详情预取投递失败: %s", type(exc).__name__)
        elif channel_type is not None and items:
            self._save_summary_in_background(items, channel_type)

    def get_home_recommendations(self, channel_type=5, offset=0, limit=20, uid=None,
                                 platform="pdd", force_refresh=False, cursor=None) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        unavailable = self._capability_error(adapter, "recommend")
        if unavailable:
            return unavailable
        channels = getattr(adapter, "channels", ())
        if isinstance(channels, (tuple, list)) and channels and channel_type not in {channel["channel_type"] for channel in channels}:
            return {"status": "error", "error_code": "invalid_channel", "error_msg": "不支持的榜单", "data": []}
        cursor_key = json.dumps(cursor, sort_keys=True, separators=(",", ":")) if isinstance(cursor, (dict, list)) else cursor or ""
        cache_key = (plat, uid or "", channel_type, offset, limit, cursor_key)
        if not force_refresh:
            cached = RECOMMEND_CACHE.get(cache_key)
            if cached is not None:
                self._offer_products(cached.get("data") or [])
                return cached
        snapshots = []
        def collect(snapshot):
            if isinstance(snapshot, DetailSnapshot) and len(snapshots) < 20 and snapshot.ref.platform == plat:
                snapshots.append(snapshot)
        paging = {"cursor": cursor} if cursor else {}
        if self.prefetcher is not None:
            paging["detail_sink"] = collect
        result = adapter.get_recommend_goods(channel_type=channel_type, offset=offset, limit=limit, uid=uid, **paging)
        if result.get("status") == "success":
            items = result.get("data") or []
            result.setdefault("has_more", len(items) >= limit)
            RECOMMEND_CACHE.set(cache_key, result)
            # Already-verified public snapshots can be read immediately; database
            # persistence is still a separate, tracked background outcome.
            for snapshot in snapshots:
                deadline = self._snapshot_deadline(snapshot)
                if deadline > time.time():
                    self._cache_detail(plat, {"status": "success", "data": snapshot.data}, deadline)
            self._offer_products(items, channel_type=channel_type, snapshots=tuple(snapshots))
        return result

    def search_home_goods(self, keyword, page=1, page_size=20, uid=None, platform="pdd") -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        unavailable = self._capability_error(adapter, "search")
        if unavailable:
            return unavailable
        keyword = (keyword or "").strip()[:30]
        cache_key = (plat, uid or "", keyword, page, page_size)
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            self._offer_products(cached.get("data") or [])
            return cached
        result = adapter.search_goods(keyword=keyword, page=page, page_size=page_size, uid=uid)
        if result.get("status") == "success":
            SEARCH_CACHE.set(cache_key, result)
            items = result.get("data") or []
            # 搜索物料链接也保留到摘要库，不能依赖可关闭的详情预取来补映射。
            if getattr(adapter, "requires_material_url", False) is True:
                materials = [item for item in items if item.get("product_url")]
                if materials:
                    self._save_summary_in_background(materials, None)
            self._offer_products(items)
        return result

    @staticmethod
    def _detail_expires_at(item: dict) -> float:
        updated = item.get("detail_updated_at")
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

    @classmethod
    def _detail_is_fresh(cls, item: dict) -> bool:
        return time.time() < cls._detail_expires_at(item)

    @staticmethod
    def _database_detail(item: dict, source: str) -> dict:
        data = deepcopy(item)
        data.pop("detail_updated_at", None)
        return {"status": "success", "data": data, "source": source}

    @staticmethod
    def _snapshot_deadline(snapshot):
        return ProductService._detail_expires_at({"detail_updated_at": snapshot.fetched_at})

    @staticmethod
    def _valid_cache_entry(entry):
        return (isinstance(entry, tuple) and len(entry) == 2
                and isinstance(entry[0], (int, float)) and not isinstance(entry[0], bool)
                and math.isfinite(entry[0]) and time.time() < entry[0] <= time.time() + PRODUCT_DETAIL_TTL
                and isinstance(entry[1], dict) and entry[1].get("status") == "success"
                and isinstance(entry[1].get("data"), dict))

    @staticmethod
    def _cache_detail(platform: str, result: dict, expires_at: float, uid=None, verified_ref=None):
        item = result.get("data") or {}
        ref = ProductRef(platform, item.get("goods_id") or item.get("product_id"), item.get("goods_sign"))
        keys = ref.cache_keys(uid)
        if (isinstance(verified_ref, ProductRef) and verified_ref.platform == ref.platform
                and ref.goods_id and ref.goods_id == verified_ref.goods_id):
            keys = list(dict.fromkeys(keys + verified_ref.cache_keys(uid)))
        if not math.isfinite(expires_at) or expires_at <= time.time():
            return deepcopy(result)
        entry = (expires_at, deepcopy(result))
        with _DETAIL_CACHE_LOCK:
            # A newer trusted alias wins for the whole snapshot, not just one key.
            for key in keys:
                current = DETAIL_CACHE.get(key)
                if ProductService._valid_cache_entry(current) and current[0] > entry[0]:
                    entry = current
            for key in keys:
                DETAIL_CACHE.set(key, (entry[0], deepcopy(entry[1])))
        return deepcopy(entry[1])

    @staticmethod
    def _cached_detail(ref, uid=None):
        primary = ProductRef(ref.platform, "" if ref.goods_sign else ref.goods_id, ref.goods_sign)
        with _DETAIL_CACHE_LOCK:
            for key in primary.cache_keys(uid):
                cached = DETAIL_CACHE.get(key)
                if ProductService._valid_cache_entry(cached):
                    return cached[0], deepcopy(cached[1])
                if cached is not None:
                    DETAIL_CACHE.pop(key)
        return None

    def _read_detail(self, ref, strict=False):
        options = {"strict": True} if strict else {}
        return self.repo.get_product_detail(ref.key, platform=ref.platform,
                                            key_type="sign" if ref.goods_sign else "id", **options)

    @staticmethod
    def _make_snapshot(data, fetched_at, platform):
        data = dict(data)
        actual_platform = normalize_platform(data.get("platform") or platform)
        ref = ProductRef(actual_platform, data.get("goods_id") or data.get("product_id"), data.get("goods_sign"))
        data.update(platform=actual_platform, goods_id=ref.goods_id, goods_sign=ref.goods_sign)
        return DetailSnapshot(ref, data, fetched_at)

    def _document_snapshot(self, item, platform):
        deadline = self._detail_expires_at(item)
        if not deadline:
            return None
        return self._make_snapshot(item, datetime.fromtimestamp(deadline - PRODUCT_DETAIL_TTL, timezone.utc), platform)

    def _commit_snapshot(self, snapshot, uid=None, public=None, stop_event=None):
        deadline = self._snapshot_deadline(snapshot)
        if deadline <= time.time():
            return _LoadedDetail({"status": "error", "error_code": "expired_detail", "error_msg": "详情快照已过期"})
        if stop_event is not None and stop_event.is_set():
            return _LoadedDetail({"status": "error", "error_code": "cancelled"})
        public = deepcopy(public) if public is not None else {"status": "success", "data": deepcopy(snapshot.data)}
        public["data"] = deepcopy(snapshot.data)
        try:
            written = self.repo.upsert_product_detail(dict(snapshot.data, detail_updated_at=snapshot.fetched_at)) is True
        except Exception as exc:
            written = False
            logger.warning("商品详情入库失败: %s", type(exc).__name__)
        if stop_event is not None and stop_event.is_set():
            return _LoadedDetail(public, snapshot, persisted=written)
        if not written:
            # False may mean either a failed write or a newer snapshot won CAS.
            # Only trusted snapshot aliases may be used for this second lookup.
            refs = [snapshot.ref]
            if snapshot.ref.goods_sign and snapshot.ref.goods_id:
                refs.append(ProductRef(snapshot.ref.platform, snapshot.ref.goods_id))
            for ref in refs:
                try:
                    winner = self._read_detail(ref, strict=True)
                except Exception:
                    break
                if winner and self._detail_expires_at(winner) >= deadline:
                    winner_snapshot = self._document_snapshot(winner, snapshot.ref.platform)
                    result = self._database_detail(winner, "database")
                    result = self._cache_detail(snapshot.ref.platform, result, self._detail_expires_at(winner), uid,
                                                verified_ref=snapshot.ref)
                    return _LoadedDetail(result, winner_snapshot, persisted=True)
        public = self._cache_detail(snapshot.ref.platform, public, deadline, uid)
        return _LoadedDetail(public, snapshot, persisted=written, retryable=not written)

    def _singleflight(self, ref, uid, kwargs, load, stop_event=None):
        key = (ref.platform, uid or "", "sign" if ref.goods_sign else "id", ref.key,
               ref.goods_id if ref.goods_sign else "", json.dumps(kwargs, sort_keys=True, default=str))
        with self._flight_lock:
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = {"event": threading.Event(), "result": None, "error": None}
                self._flights[key] = flight
        if not leader:
            while not flight["event"].wait(0.1):
                if stop_event is not None and stop_event.is_set():
                    return _LoadedDetail({"status": "error", "error_code": "cancelled"})
            if flight["error"] is not None:
                raise flight["error"]
            return deepcopy(flight["result"])
        try:
            flight["result"] = load()
            return flight["result"]
        except BaseException as exc:
            flight["error"] = exc
            raise
        finally:
            with self._flight_lock:
                self._flights.pop(key, None)
                flight["event"].set()

    def _load_official(self, ref, adapter, uid=None, kwargs=None, stop_event=None, refresh_ahead_seconds=0):
        kwargs = kwargs or {}
        def load():
            cached = self._cached_detail(ref, uid)
            if cached and cached[0] - time.time() > refresh_ahead_seconds:
                snapshot = self._make_snapshot(cached[1]["data"], datetime.fromtimestamp(cached[0] - PRODUCT_DETAIL_TTL, timezone.utc), ref.platform)
                return _LoadedDetail(cached[1], snapshot)
            if stop_event is not None and stop_event.is_set():
                return _LoadedDetail({"status": "error", "error_code": "cancelled"})
            result = adapter.get_goods_detail(goods_id_or_sign=ref.key, goods_id=ref.goods_id or None,
                                              goods_sign=ref.goods_sign or None, uid=uid, **kwargs)
            if result.get("status") != "success" or not result.get("data"):
                code = str(result.get("error_code") or "").upper()
                return _LoadedDetail(result, retryable=code in {"NETWORK_ERROR", "TIMEOUT", "RATE_LIMIT", "SERVICE_UNAVAILABLE"})
            if normalize_platform(result["data"].get("platform") or ref.platform) != ref.platform:
                return _LoadedDetail({"status": "error", "error_msg": "商品返回平台不匹配", "error_code": "product_identity_mismatch"})
            try:
                snapshot = self._make_snapshot(result["data"], utc_millis(datetime.fromtimestamp(time.time(), timezone.utc)), ref.platform)
            except ValueError:
                return _LoadedDetail({"status": "error", "error_code": "invalid_product_detail", "error_msg": "平台商品详情身份无效"})
            return self._commit_snapshot(snapshot, uid, result, stop_event)
        return self._singleflight(ref, uid, kwargs, load, stop_event)

    def get_goods_detail(self, platform="pdd", goods_id_or_sign="", goods_id=None,
                         goods_sign=None, uid=None, **kwargs) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        unavailable = self._capability_error(adapter, "detail")
        if unavailable:
            return unavailable
        ref = adapter.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        if not ref.key:
            return {"status": "error", "error_msg": "请提供商品 ID 或 Sign", "error_code": "missing_product_identity"}
        cached = self._cached_detail(ref, uid)
        if cached:
            return cached[1]
        item = self._read_detail(ref)
        deadline = self._detail_expires_at(item) if item else 0.0
        if time.time() < deadline:
            result = self._database_detail(item, "database")
            return self._cache_detail(plat, result, deadline, uid)
        loaded = self._load_official(ref, adapter, uid, kwargs)
        if loaded.public.get("status") != "success" and item:
            return self._database_detail(item, "database_fallback")
        return loaded.public

    def prefetch_product_detail(self, ref, snapshot=None, refresh_ahead_seconds=330, stop_event=None):
        """只准备公开详情；内部结果明确区分已入库、仅内存及失败。"""
        if stop_event is not None and stop_event.is_set():
            return DetailPrefetchResult("skipped")
        try:
            adapter = get_adapter(ref.platform)
            if self._capability_error(adapter, "detail"):
                return DetailPrefetchResult("skipped")
            allowed = getattr(adapter, "can_prefetch_detail", None)
            if snapshot is None and callable(allowed) and allowed(ref) is False:
                return DetailPrefetchResult("skipped")
            if snapshot is not None:
                if not isinstance(snapshot, DetailSnapshot) or snapshot.ref != ref:
                    return DetailPrefetchResult("error")
                loaded = self._commit_snapshot(snapshot, stop_event=stop_event)
            else:
                item = self._read_detail(ref, strict=True)
                deadline = self._detail_expires_at(item) if item else 0.0
                if deadline - time.time() > refresh_ahead_seconds:
                    saved = self._document_snapshot(item, ref.platform)
                    return DetailPrefetchResult("ready", saved, next_check_at=deadline - refresh_ahead_seconds)
                cached = self._cached_detail(ref)
                if cached and cached[0] > deadline and cached[0] - time.time() > refresh_ahead_seconds:
                    seed = self._make_snapshot(cached[1]["data"], datetime.fromtimestamp(cached[0] - PRODUCT_DETAIL_TTL, timezone.utc), ref.platform)
                    loaded = self._commit_snapshot(seed, stop_event=stop_event)
                else:
                    loaded = self._load_official(ref, adapter, stop_event=stop_event,
                                                 refresh_ahead_seconds=refresh_ahead_seconds)
        except Exception as exc:
            logger.warning("公开详情预取失败: platform=%s type=%s", ref.platform, type(exc).__name__)
            return DetailPrefetchResult("error", snapshot, retryable=True)
        if loaded.snapshot and self._snapshot_deadline(loaded.snapshot) <= time.time():
            return DetailPrefetchResult("skipped")
        if loaded.persisted and loaded.snapshot:
            return DetailPrefetchResult("ready", loaded.snapshot,
                                        next_check_at=self._snapshot_deadline(loaded.snapshot) - refresh_ahead_seconds)
        if loaded.public.get("status") == "success" and loaded.snapshot:
            return DetailPrefetchResult("persist_failed", loaded.snapshot, retryable=True)
        return DetailPrefetchResult("error", loaded.snapshot, retryable=loaded.retryable)

    def get_boom_products(self, platform="all", category=None, keyword=None, sort_by="latest",
                          page=1, page_size=20) -> Dict[str, Any]:
        result = self.repo.query_boom_products(platform=normalize_platform(platform, "all"), category=category,
                                              keyword=keyword, sort_by=sort_by, page=page, page_size=page_size)
        if result.get("status") == "success":
            self._offer_products(result.get("data") or [])
        return result

    def get_boom_categories(self, platform="all") -> List[str]:
        plat = normalize_platform(platform, "all")
        cached = CATEGORY_CACHE.get(plat)
        if cached is not None:
            return cached
        categories = self.repo.get_categories(platform=plat)
        if categories:
            CATEGORY_CACHE.set(plat, categories)
        return categories
