# -*- coding: utf-8 -*-
"""商品应用服务：平台调度、详情来源时间及前后台共用的商品解析。"""
import json
import time
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Sequence
from adapters import get_adapter
from infrastructure.http_utils import sanitize_user_error_message
from infrastructure.logging_utils import format_exc_summary, severity_icon
from models.product import (
    ProductRef,
    DetailSnapshot,
    DetailPrefetchResult,
    is_jd_image_key,
    normalize_platform,
    utc_millis,
)
from repositories.product_repo import ProductRepository
from services.boom_search import resolve_boom_sort
from cache import (
    RECOMMEND_CACHE, SEARCH_CACHE, BOOM_SORT_CACHE,
    MATERIAL_CACHE, SIGN_BY_ID_CACHE,
    cache_deal_tip,
    cache_product_detail,
    clear_product_off_promotion,
    evict_product_from_caches,
    get_cached_deal_tip,
    get_cached_product_detail,
    has_valid_product_images,
    is_product_off_promotion,
    make_detail_snapshot,
    parse_detail_deadline,
    snapshot_detail_deadline,
)
from config import PRODUCT_DETAIL_TTL, logger


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
                written_count = self.repo.upsert_home_products(items, channel_type=channel_type)
                if written_count > 0:
                    for it in items:
                        if isinstance(it, dict):
                            clear_product_off_promotion(it.get("platform"), it.get("goods_id"), it.get("goods_sign"))
            except Exception as exc:
                logger.warning(
                    "❌ 【商品摘要入库异常】后台线程执行推荐摘要入库失败 | 频道[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或写入负载>",
                    channel_type, format_exc_summary(exc),
                )
            finally:
                with self._write_lock:
                    self._background_writes.discard(thread)
                    self._summary_write_slots.release()
        with self._write_lock:
            if not self._writes_open:
                return
            if not self._summary_write_slots.acquire(blocking=False):
                logger.warning(
                    "【商品摘要背压】推荐摘要后台写入满载，跳过本次缓存性入库 | 频道[%s] 并发上限[%s] 排查方向<当前摘要写入并发已达上限，属正常背压保护>",
                    channel_type, self._MAX_BACKGROUND_WRITES,
                )
                return
            thread = None
            try:
                thread = threading.Thread(target=save, name="bg-upsert-home-products", daemon=True)
                self._background_writes.add(thread)
                thread.start()
            except Exception:
                self._background_writes.discard(thread)
                self._summary_write_slots.release()
                logger.warning(
                    "❌ 【商品摘要入库异常】推荐摘要后台任务未能启动 | 频道[%s] 排查方向<请检查系统线程资源或容器进程数限制>",
                    channel_type,
                )

    def evict_off_promotion_product(self, ref: ProductRef) -> None:
        """全链路驱逐已确认取消推广或下架的商品（内存各分域缓存 + 防复活黑名单 + MongoDB物理删除）。"""
        if not isinstance(ref, ProductRef) or not ref.key:
            return
        evict_product_from_caches(ref.platform, ref.goods_id, ref.goods_sign)
        try:
            self.repo.delete_product(ref.platform, goods_id=ref.goods_id or None, goods_sign=ref.goods_sign or None)
        except Exception as exc:
            logger.warning(
                "❌ 【失效商品清理异常】物理删除下架商品记录时发生异常 | 平台[%s] 商品[%s] 异常<%s>",
                ref.platform, ref.key, format_exc_summary(exc),
            )

    def _offer_products(self, items, channel_type=None, snapshots=(), adapter=None, skip_sign_and_prefetch: bool = False):
        # 转链所需公开映射先发布，不能被可丢弃的后台摘要写入或预取开关阻断；同时过滤已在防复活黑名单中的下架商品。
        valid_items = []
        adapter_cache = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = ProductRef(item.get("platform"), item.get("goods_id"), item.get("goods_sign"))
            if is_product_off_promotion(ref.platform, ref.goods_id, ref.goods_sign):
                continue
            valid_items.append(item)
            if skip_sign_and_prefetch:
                continue
            if (
                ref.goods_id
                and ref.goods_sign
                and ref.goods_id != ref.goods_sign
                and not (ref.platform == "jd" and is_jd_image_key(ref.goods_sign))
                and not (ref.platform == "pdd" and ref.goods_sign.isdigit())
            ):
                SIGN_BY_ID_CACHE.set((ref.platform, ref.goods_id), ref.goods_sign)
            url = item.get("product_url")
            if ref.key and isinstance(url, str) and url.strip():
                clean_url = url.strip()
                adp = adapter if (adapter is not None and getattr(adapter, "platform_code", ref.platform) == ref.platform) else adapter_cache.get(ref.platform)
                if adp is None and ref.platform not in adapter_cache:
                    try:
                        adp = get_adapter(ref.platform)
                    except Exception:
                        adp = None
                    adapter_cache[ref.platform] = adp
                auth_fn = None
                if adp is not None and getattr(adp, "requires_material_url", False) is True:
                    candidate_fn = getattr(adp, "is_authoritative_material_url", None)
                    if callable(candidate_fn):
                        auth_fn = candidate_fn
                for kind, val in (("sign", ref.goods_sign), ("id", ref.goods_id)):
                    if not val:
                        continue
                    cache_key = (ref.platform, kind, val)
                    existing_url = MATERIAL_CACHE.get(cache_key)
                    # 防止榜单低优先级摘要链接覆盖详情页已解析出的权威推广物料链接
                    if (
                        isinstance(existing_url, str)
                        and auth_fn is not None
                        and auth_fn(existing_url) is True
                        and auth_fn(clean_url) is not True
                    ):
                        continue
                    MATERIAL_CACHE.set(cache_key, clean_url)
        if skip_sign_and_prefetch:
            return
        if self.prefetcher is not None:
            try:
                self.prefetcher.offer_products(valid_items, channel_type=channel_type, snapshots=snapshots)
            except Exception as exc:
                logger.warning(
                    "❌ 【详情预取投递异常】向后台预取器投递商品列表时发生异常 | 频道[%s] 数量[%d] 异常<%s> 排查方向<请检查详情预取器队列状态>",
                    channel_type, len(valid_items), format_exc_summary(exc),
                )
        elif channel_type is not None and valid_items:
            self._save_summary_in_background(valid_items, channel_type)

    def _overlay_deal_card_info(self, plat: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将正式表已入库活跃线报的缩略图亮点、发布时间与可靠性标志叠加到首页列表摘要（失败或无覆盖能力时降级原样返回）。"""
        if not items or not hasattr(self.repo, "get_deal_card_overlays"):
            return items
        try:
            keys = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                for k in (it.get("goods_id"), it.get("goods_sign"), it.get("product_id")):
                    if k:
                        keys.append(k)
            overlays = self.repo.get_deal_card_overlays(plat, keys) if keys else None
            overlaid = (
                [ProductRepository.apply_deal_card_overlay(it, overlays) for it in items]
                if isinstance(overlays, dict) and overlays
                else items
            )
            return [
                (
                    {**it, "is_reliable": it.get("is_deal_tip") is not True}
                    if isinstance(it, dict) and "is_reliable" not in it
                    else it
                )
                for it in overlaid
            ]
        except Exception:
            return items

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
        # 公开推荐榜单不按用户作用域隔离，所有用户共享同一份内存缓存以节约上游配额
        cache_key = (plat, "", channel_type, offset, limit, cursor_key)
        if not force_refresh:
            cached = RECOMMEND_CACHE.get(cache_key)
            if cached is not None:
                self._offer_products(cached.get("data") or [], adapter=adapter)
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
            raw_items = result.get("data") or []
            result.setdefault("has_more", len(raw_items) >= limit)
            items = [
                item for item in raw_items
                if not (isinstance(item, dict) and is_product_off_promotion(plat, item.get("goods_id"), item.get("goods_sign")))
            ]
            if getattr(adapter, "preserves_known_coupons_across_rank", False) is True and items:
                try:
                    zero_keys = []
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        has_c = float(it.get("coupon_amount") or 0.0) > 0 and bool(str(it.get("coupon_url") or "").strip())
                        if not has_c:
                            for k in (it.get("goods_id"), it.get("goods_sign")):
                                if k:
                                    zero_keys.append(k)
                    overlays = self.repo.get_known_coupon_overlays(plat, zero_keys) if zero_keys else None
                    if isinstance(overlays, dict) and overlays:
                        items = [ProductRepository.apply_coupon_overlay(it, overlays) for it in items]
                except Exception:
                    pass
            result["data"] = self._overlay_deal_card_info(plat, items)
            RECOMMEND_CACHE.set(cache_key, result)
            # Already-verified public snapshots can be read immediately; database
            # persistence is still a separate, tracked background outcome.
            for snapshot in snapshots:
                if is_product_off_promotion(plat, snapshot.ref.goods_id, snapshot.ref.goods_sign):
                    continue
                deadline = self._snapshot_deadline(snapshot)
                if deadline > time.time():
                    self._cache_detail(plat, {"status": "success", "data": snapshot.data}, deadline)
            self._offer_products(items, channel_type=channel_type, snapshots=tuple(snapshots), adapter=adapter)
        else:
            logger.warning(
                "%s 【商品推荐失败】拉取平台首页推荐商品流失败 | 平台[%s] 频道[%s] 偏移[%s] 数量[%s] 失败原因<%s> 排查方向<请检查上游联盟接口配额、网络连接或应用密钥配置>",
                severity_icon(error_code=result.get("error_code")),
                plat, channel_type, offset, limit,
                result.get("error_msg") or result.get("error_code") or "未知错误",
            )
        return result

    def search_home_goods(self, keyword, page=1, page_size=20, uid=None, platform="pdd") -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        unavailable = self._capability_error(adapter, "search")
        if unavailable:
            return unavailable
        raw_kw = (keyword or "").strip()
        if not raw_kw:
            return {"status": "success", "total_count": 0, "page": page, "page_size": page_size, "has_more": False, "data": []}
        keyword = raw_kw[:80]
        # 公开关键词检索结果不按用户隔离，跨用户共享以避免同词重复消耗上游额度
        cache_key = (plat, "", keyword, page, page_size)
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            self._offer_products(cached.get("data") or [], adapter=adapter)
            return cached
        result = adapter.search_goods(keyword=keyword, page=page, page_size=page_size, uid=uid)
        if result.get("status") == "success":
            raw_items = result.get("data") or []
            items = [
                item for item in raw_items
                if not (isinstance(item, dict) and is_product_off_promotion(plat, item.get("goods_id"), item.get("goods_sign")))
            ]
            result["data"] = self._overlay_deal_card_info(plat, items)
            SEARCH_CACHE.set(cache_key, result)
            # 搜索物料链接也保留到摘要库，不能依赖可关闭的详情预取来补映射；hdk_pdd_search 仅作只读展示兜底，不写摘要/ID映射。
            is_hdk_pdd = (plat == "pdd" and result.get("_provider") == "hdk_pdd_search")
            if getattr(adapter, "requires_material_url", False) is True and not is_hdk_pdd:
                materials = [item for item in items if item.get("product_url")]
                if materials:
                    self._save_summary_in_background(materials, None)
            self._offer_products(items, adapter=adapter, skip_sign_and_prefetch=is_hdk_pdd)
        else:
            logger.warning(
                "%s 【商品搜索失败】按关键词检索平台联盟商品失败 | 平台[%s] 关键词[%s] 页码[%s] 失败原因<%s> 排查方向<请检查上游搜索接口配额、关键词合法性或网关连通性>",
                severity_icon(error_code=result.get("error_code")),
                plat, keyword, page,
                result.get("error_msg") or result.get("error_code") or "未知错误",
            )
        return result

    _has_valid_images = staticmethod(has_valid_product_images)
    _parse_detail_deadline = staticmethod(parse_detail_deadline)
    _snapshot_deadline = staticmethod(snapshot_detail_deadline)
    _cache_detail = staticmethod(cache_product_detail)
    _cached_detail = staticmethod(get_cached_product_detail)

    @classmethod
    def _detail_expires_at(cls, item: dict) -> float:
        if not has_valid_product_images(item):
            return 0.0
        return parse_detail_deadline(item.get("detail_updated_at"))

    @staticmethod
    def _database_detail(item: dict, source: str) -> dict:
        data = deepcopy(item)
        data.pop("detail_updated_at", None)
        return {"status": "success", "data": data, "source": source}

    def _apply_deal_tip(self, plat: str, payload: dict, ref: Optional[ProductRef] = None) -> dict:
        """对外返回详情前，用正式表的线报视图覆盖 payload 里的 deal_tip。

        详情 payload 可能来自转链种子、上游接口或旧缓存，这些来源天然不含 deal_info，
        所以“库里有线报、页面却没有”只取决于本次走的是哪条路径。这里统一按商品主键
        做一次只读投影（60 秒短缓存），让线报与详情来源解耦：
          - 文档存在  → 以库里视图为准覆盖（含“已过期应当隐藏”的情况）；
          - 文档缺失  → 原样返回，不猜、不删；
          - 查询失败  → 原样返回，避免把有效线报误清空。
        只改本次返回对象，不写缓存、不写数据库。
        """
        if not isinstance(payload, dict) or payload.get("status") != "success":
            return payload
        data = payload.get("data")
        if not isinstance(data, dict):
            return payload
        pid = str(
            data.get("goods_id") or data.get("product_id") or (ref.goods_id if ref else "") or ""
        ).strip()
        if not pid:
            return payload
        cached = get_cached_deal_tip(plat, pid)
        if cached is not None:
            state, deal_tip = cached
        else:
            try:
                res = self.repo.get_deal_tip_view(pid, platform=plat)
                if isinstance(res, (tuple, list)) and len(res) == 2:
                    state, deal_tip = res
                else:
                    state, deal_tip = "not_found", None
            except Exception as exc:
                logger.warning(
                    "❌ 【线报视图覆盖异常】读取线报视图失败，本次详情保持原样 | 平台[%s] 商品[%s] 异常<%s>",
                    plat, pid, format_exc_summary(exc),
                )
                return payload
            cache_deal_tip(plat, pid, state, deal_tip)
        if state != "ok":
            return payload
        data["deal_tip"] = deepcopy(deal_tip) if isinstance(deal_tip, dict) and deal_tip else None
        return payload

    def _read_detail(self, ref, strict=False):
        options = {"strict": True} if strict else {}
        if ref.platform in ("taobao", "jd") and ref.goods_id and (
            not ref.goods_sign
            or ref.goods_sign == ref.goods_id
            or (ref.platform == "jd" and is_jd_image_key(ref.goods_sign))
        ):
            doc = self.repo.get_product_detail(ref.goods_id, platform=ref.platform,
                                               key_type="id", **options)
            if doc is not None or not ref.goods_sign:
                return doc
        doc = self.repo.get_product_detail(ref.key, platform=ref.platform,
                                           key_type="sign" if ref.goods_sign else "id", **options)
        if doc is None and ref.goods_sign and ref.goods_id and ref.platform in ("taobao", "jd", "pdd"):
            doc = self.repo.get_product_detail(ref.goods_id, platform=ref.platform,
                                               key_type="id", **options)
        return doc

    @staticmethod
    def _upgrade_ref_sign(ref: ProductRef, item: Optional[dict] = None) -> ProductRef:
        if ref.platform not in ("taobao", "jd", "pdd") or not ref.goods_id:
            return ref
        needs_upgrade = (
            not ref.goods_sign
            or ref.goods_sign == ref.goods_id
            or (ref.platform == "jd" and is_jd_image_key(ref.goods_sign))
        )
        if not needs_upgrade:
            return ref
        if isinstance(item, dict):
            doc_sign = str(item.get("goods_sign") or "").strip()
            if doc_sign and doc_sign != ref.goods_id and not (ref.platform == "jd" and is_jd_image_key(doc_sign)):
                SIGN_BY_ID_CACHE.set((ref.platform, ref.goods_id), doc_sign)
                return ProductRef(ref.platform, ref.goods_id, doc_sign)
        cached_sign = SIGN_BY_ID_CACHE.get((ref.platform, ref.goods_id))
        if isinstance(cached_sign, str) and cached_sign.strip():
            return ProductRef(ref.platform, ref.goods_id, cached_sign.strip())
        return ref

    _make_snapshot = staticmethod(make_detail_snapshot)

    def _document_snapshot(self, item, platform):
        deadline = self._detail_expires_at(item)
        if not deadline:
            return None
        return self._make_snapshot(item, datetime.fromtimestamp(deadline - PRODUCT_DETAIL_TTL, timezone.utc), platform)

    def _commit_snapshot(self, snapshot, public=None, stop_event=None, requested_ref=None):
        raw_deadline = self._parse_detail_deadline(snapshot.fetched_at)
        if raw_deadline <= time.time():
            return _LoadedDetail({"status": "error", "error_code": "expired_detail", "error_msg": "详情快照已过期"})
        if stop_event is not None and stop_event.is_set():
            return _LoadedDetail({"status": "error", "error_code": "cancelled"})
        public = deepcopy(public) if public is not None else {"status": "success", "data": deepcopy(snapshot.data)}
        public["data"] = deepcopy(snapshot.data)
        if not self._has_valid_images(snapshot.data):
            try:
                if self.repo.upsert_home_products([snapshot.data]) > 0:
                    clear_product_off_promotion(snapshot.ref.platform, snapshot.ref.goods_id, snapshot.ref.goods_sign)
            except Exception:
                pass
            return _LoadedDetail(public, snapshot, persisted=False, retryable=False)
        deadline = raw_deadline
        try:
            written = self.repo.upsert_product_detail(dict(snapshot.data, detail_updated_at=snapshot.fetched_at)) is True
            if written:
                clear_product_off_promotion(snapshot.ref.platform, snapshot.ref.goods_id, snapshot.ref.goods_sign)
                if (
                    requested_ref
                    and requested_ref.goods_id
                    and snapshot.ref.goods_id
                    and snapshot.ref.goods_id != requested_ref.goods_id
                ):
                    try:
                        self.repo.mark_product_superseded(
                            snapshot.ref.platform,
                            requested_ref.goods_id,
                            snapshot.ref.goods_id,
                            requested_ref.goods_sign,
                        )
                        evict_product_from_caches(
                            snapshot.ref.platform,
                            goods_id=requested_ref.goods_id,
                            goods_sign=requested_ref.goods_sign,
                            blacklist=False,
                        )
                    except Exception as exc:
                        logger.warning("【商品身份迁移联动异常】%s", format_exc_summary(exc))
        except Exception as exc:
            written = False
            logger.warning(
                "❌ 【商品详情入库异常】提交商品详情快照到本地数据库失败 | 平台[%s] 异常<%s> 排查方向<请检查MongoDB连接状态或索引约束>",
                snapshot.ref.platform, format_exc_summary(exc),
            )
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
                    result = self._cache_detail(snapshot.ref.platform, result, self._detail_expires_at(winner), None,
                                                verified_ref=snapshot.ref)
                    return _LoadedDetail(result, winner_snapshot, persisted=True)
        public = self._cache_detail(snapshot.ref.platform, public, deadline, None)
        return _LoadedDetail(public, snapshot, persisted=written, retryable=not written)

    def _singleflight(self, ref, kwargs, load, stop_event=None):
        # 防并发击穿锁按平台与商品唯一标识聚合，同一瞬间多用户访问同款商品仅发起单次上游请求
        key = (ref.platform, "", "sign" if ref.goods_sign else "id", ref.key,
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

    def _load_official(self, ref, adapter, uid=None, kwargs=None, stop_event=None,
                       refresh_ahead_seconds=0, deal_tip=None):
        kwargs = kwargs or {}
        def load():
            # 公开商品详情直接查公共缓存，不再受当前调用方 uid 隔离
            cached = self._cached_detail(ref)
            if cached and cached[0] - time.time() > refresh_ahead_seconds:
                cached_data = dict(cached[1]["data"])
                if isinstance(deal_tip, dict) and deal_tip and not cached_data.get("deal_tip"):
                    cached_data["deal_tip"] = deepcopy(deal_tip)
                snapshot = self._make_snapshot(cached_data, datetime.fromtimestamp(cached[0] - PRODUCT_DETAIL_TTL, timezone.utc), ref.platform)
                return _LoadedDetail(cached[1], snapshot)
            if stop_event is not None and stop_event.is_set():
                return _LoadedDetail({"status": "error", "error_code": "cancelled"})
            detail_kwargs = dict(kwargs)
            if stop_event is not None:
                detail_kwargs.setdefault("allow_fallback", False)
            result = adapter.get_goods_detail(
                goods_id_or_sign=ref.key,
                goods_id=ref.goods_id or None,
                goods_sign=ref.goods_sign or None,
                uid=uid,
                **detail_kwargs,
            )
            if result.get("status") != "success" or not result.get("data"):
                code = str(result.get("error_code") or "").upper()
                return _LoadedDetail(result, retryable=code in {"NETWORK_ERROR", "TIMEOUT", "RATE_LIMIT", "SERVICE_UNAVAILABLE"})
            if normalize_platform(result["data"].get("platform") or ref.platform) != ref.platform:
                return _LoadedDetail({"status": "error", "error_msg": "商品返回平台不匹配", "error_code": "product_identity_mismatch"})
            detail_payload = dict(result["data"])
            if not str(detail_payload.get("name") or "").strip():
                return _LoadedDetail({"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "该商品已取消推广或已下架，请挑选其他好物"})
            if isinstance(deal_tip, dict) and deal_tip and not detail_payload.get("deal_tip"):
                detail_payload["deal_tip"] = deepcopy(deal_tip)
            if result.get("cacheable") is False:
                public_res = deepcopy(result)
                public_res["data"] = detail_payload
                return _LoadedDetail(public_res, snapshot=None, persisted=False, retryable=True)
            try:
                snapshot = self._make_snapshot(detail_payload, utc_millis(datetime.fromtimestamp(time.time(), timezone.utc)), ref.platform)
            except ValueError:
                return _LoadedDetail({"status": "error", "error_code": "invalid_product_detail", "error_msg": "平台商品详情身份无效"})
            return self._commit_snapshot(snapshot, result, stop_event, requested_ref=ref)
        return self._singleflight(ref, kwargs, load, stop_event)

    def get_goods_detail(self, platform="pdd", goods_id_or_sign="", goods_id=None,
                         goods_sign=None, uid=None, **kwargs) -> Dict[str, Any]:
        _visited = set(kwargs.pop("_superseded_visited", ()))
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        unavailable = self._capability_error(adapter, "detail")
        if unavailable:
            return unavailable
        ref = adapter.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        if not ref.key:
            return {"status": "error", "error_msg": "请提供商品 ID 或 Sign", "error_code": "missing_product_identity"}
        ref = self._upgrade_ref_sign(ref)
        if is_product_off_promotion(plat, ref.goods_id, ref.goods_sign):
            return {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "该商品已取消推广或已下架，请挑选其他好物"}
        # 公开商品详情优先命中全局共享内存缓存
        cached = self._cached_detail(ref)
        if cached:
            return self._apply_deal_tip(plat, cached[1], ref)
        item = self._read_detail(ref)
        ref = self._upgrade_ref_sign(ref, item)
        if isinstance(item, dict) and item.get("superseded_by"):
            superseded_id = str(item.get("superseded_by") or "").strip()
            has_usable_sign = bool(
                ref.goods_sign and not (plat == "jd" and is_jd_image_key(ref.goods_sign))
            )
            current_ids = {
                cid for cid in (ref.goods_id, str(item.get("goods_id") or item.get("product_id") or "").strip())
                if cid
            }
            if superseded_id and superseded_id not in current_ids and superseded_id not in _visited and len(_visited) < 2:
                logger.info(
                    "【商品身份透明转发】请求商品已被新主键替代，无缝跳转新商品 | 平台[%s] 旧商品[%s] 新商品[%s]",
                    plat, ref.goods_id, superseded_id,
                )
                fwd = self.get_goods_detail(
                    platform=plat,
                    goods_id_or_sign=superseded_id,
                    goods_id=superseded_id,
                    goods_sign=None,
                    uid=uid,
                    _superseded_visited=_visited | current_ids,
                    **kwargs
                )
                if isinstance(fwd, dict) and fwd.get("status") == "success":
                    return fwd
                if not has_usable_sign:
                    return fwd
            if has_usable_sign:
                item = None
            else:
                evict_product_from_caches(plat, ref.goods_id, ref.goods_sign)
                return {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "该商品已取消推广或已下架，请挑选其他好物"}
        if isinstance(item, dict) and item.get("is_off_promotion") is True:
            evict_product_from_caches(plat, ref.goods_id, ref.goods_sign)
            return {"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "该商品已取消推广或已下架，请挑选其他好物"}
        deadline = self._detail_expires_at(item) if item else 0.0
        if time.time() < deadline:
            result = self._apply_deal_tip(plat, self._database_detail(item, "database"), ref)
            return self._cache_detail(plat, result, deadline)
        loaded = self._load_official(
            ref, adapter, uid, kwargs,
            deal_tip=item.get("deal_tip") if isinstance(item, dict) else None,
        )
        if loaded.public.get("status") != "success":
            goods_tag = ref.goods_id or (ref.goods_sign[:16] + "..." if len(ref.goods_sign) > 16 else ref.goods_sign)
            reason = loaded.public.get("error_msg") or loaded.public.get("error_code") or "未知错误"
            err_code = str(loaded.public.get("error_code") or "").upper()
            if err_code == "PRODUCT_OFF_PROMOTION":
                self.evict_off_promotion_product(ref)
                logger.info(
                    "【商品已下架/取消推广】上游确认商品已停止推广，已自动从缓存与本地库清理 | 平台[%s] 商品[%s] 原因<%s>",
                    plat, goods_tag, reason,
                )
                return loaded.public
            if item and item.get("is_off_promotion") is not True:
                if err_code == "RATE_LIMIT":
                    logger.info(
                        "【商品详情缓存兜底】上游详情接口触发瞬时频控，已无缝返回本地缓存 | 平台[%s] 商品[%s]",
                        plat, goods_tag,
                    )
                else:
                    logger.warning(
                        "%s 【商品详情降级】实时拉取上游商品详情未成功，已降级返回本地历史缓存 | 平台[%s] 商品[%s] 上游原因<%s>",
                        severity_icon(error_code=err_code), plat, goods_tag, reason,
                    )
                return self._apply_deal_tip(plat, self._database_detail(item, "database_fallback"), ref)
            logger.warning(
                "%s 【商品详情失败】拉取商品详情失败且本地无可用缓存 | 平台[%s] 商品[%s] 失败原因<%s> 排查方向<请检查商品ID/Sign是否有效、商品是否已下架或上游接口状态>",
                severity_icon(error_code=err_code), plat, goods_tag, reason,
            )
            sanitized_err = dict(loaded.public)
            sanitized_err["error_msg"] = sanitize_user_error_message(
                err_code,
                loaded.public.get("error_msg"),
                platform_name=getattr(adapter, "platform_name", ""),
                scene="detail",
            )
            return self._apply_deal_tip(plat, sanitized_err, ref)
        return self._apply_deal_tip(plat, loaded.public, ref)

    def _finalize_prefetch_loaded(
        self,
        ref: ProductRef,
        loaded: _LoadedDetail,
        refresh_ahead_seconds: float,
        fallback_snapshot: Optional[DetailSnapshot] = None,
    ) -> DetailPrefetchResult:
        if loaded.snapshot and self._snapshot_deadline(loaded.snapshot) <= time.time():
            return DetailPrefetchResult("skipped")
        if loaded.persisted and loaded.snapshot:
            return DetailPrefetchResult(
                "ready",
                loaded.snapshot,
                next_check_at=self._snapshot_deadline(loaded.snapshot) - refresh_ahead_seconds,
            )
        if loaded.public.get("status") == "success" and loaded.snapshot:
            return DetailPrefetchResult("persist_failed", loaded.snapshot, retryable=True)
        err_code = str(loaded.public.get("error_code") or "").strip()
        err_msg = str(loaded.public.get("error_msg") or "").strip()
        if err_code.upper() == "PRODUCT_OFF_PROMOTION":
            self.evict_off_promotion_product(ref)
            logger.info(
                "【详情预取下架清理】上游确认商品已停止推广，已自动从缓存与本地库清理 | 平台[%s] 商品[%s]",
                ref.platform, ref.goods_id or ref.goods_sign,
            )
            return DetailPrefetchResult("skipped", error_code="PRODUCT_OFF_PROMOTION", next_check_at=time.time() + 3600)
        return DetailPrefetchResult(
            "error",
            fallback_snapshot if fallback_snapshot is not None else loaded.snapshot,
            retryable=loaded.retryable,
            error_code=err_code or "UPSTREAM_ERROR",
            error_msg=err_msg,
        )

    def prefetch_product_detail(self, ref, snapshot=None, refresh_ahead_seconds=330, stop_event=None):
        """只准备公开详情；内部结果明确区分已入库、确定下架与需告警失败。"""
        if stop_event is not None and stop_event.is_set():
            return DetailPrefetchResult("skipped")
        if is_product_off_promotion(ref.platform, ref.goods_id, ref.goods_sign):
            return DetailPrefetchResult("skipped", error_code="PRODUCT_OFF_PROMOTION")
        try:
            adapter = get_adapter(ref.platform)
            if self._capability_error(adapter, "detail"):
                return DetailPrefetchResult("skipped")
            allowed = getattr(adapter, "can_prefetch_detail", None)
            if snapshot is None and callable(allowed) and allowed(ref) is False:
                return DetailPrefetchResult("skipped")
            if snapshot is not None:
                if not isinstance(snapshot, DetailSnapshot) or snapshot.ref != ref:
                    return DetailPrefetchResult("error", error_code="INVALID_SNAPSHOT", error_msg="预取快照与商品身份不匹配")
                loaded = self._commit_snapshot(snapshot, stop_event=stop_event)
            else:
                ref = self._upgrade_ref_sign(ref)
                item = self._read_detail(ref, strict=True)
                ref = self._upgrade_ref_sign(ref, item)
                deadline = self._detail_expires_at(item) if item else 0.0
                if deadline - time.time() > refresh_ahead_seconds:
                    saved = self._document_snapshot(item, ref.platform)
                    return DetailPrefetchResult("ready", saved, next_check_at=deadline - refresh_ahead_seconds)
                local_deal_tip = item.get("deal_tip") if isinstance(item, dict) else None
                cached = self._cached_detail(ref)
                if cached and cached[0] > deadline and cached[0] - time.time() > refresh_ahead_seconds:
                    seed_data = dict(cached[1]["data"])
                    if isinstance(local_deal_tip, dict) and local_deal_tip and not seed_data.get("deal_tip"):
                        seed_data["deal_tip"] = deepcopy(local_deal_tip)
                    seed = self._make_snapshot(seed_data, datetime.fromtimestamp(cached[0] - PRODUCT_DETAIL_TTL, timezone.utc), ref.platform)
                    loaded = self._commit_snapshot(seed, stop_event=stop_event)
                else:
                    loaded = self._load_official(ref, adapter, stop_event=stop_event,
                                                 refresh_ahead_seconds=refresh_ahead_seconds,
                                                 deal_tip=local_deal_tip)
        except Exception as exc:
            exc_summary = format_exc_summary(exc)
            logger.warning(
                "❌ 【详情预取单品异常】后台预取公开商品详情时发生异常，下轮将自动重试 | 平台[%s] 异常<%s>",
                ref.platform, exc_summary,
            )
            return DetailPrefetchResult("error", snapshot, retryable=True, error_code="INTERNAL_ERROR", error_msg=exc_summary)
        return self._finalize_prefetch_loaded(ref, loaded, refresh_ahead_seconds, fallback_snapshot=snapshot)

    def prefetch_product_details_batch(
        self,
        refs: Sequence[ProductRef],
        *,
        stop_event=None,
        refresh_ahead_seconds: float = 330,
        deal_tips: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, DetailPrefetchResult]:
        """
        同平台批量详情预取：先本地库/内存缓存与下架黑名单短路，剩余待刷新商品按每批 <=20
        委托 `adapter.batch_get_goods_details`，结果逐条提交快照并严格隔离单条失败。
        """
        outcomes: Dict[str, DetailPrefetchResult] = {}
        if not refs:
            return outcomes
        if stop_event is not None and stop_event.is_set():
            for raw_ref in refs:
                if raw_ref.key:
                    outcomes[raw_ref.key] = DetailPrefetchResult("skipped")
            return outcomes

        pending_by_plat: Dict[str, List[tuple]] = {}
        seen_orig_keys = set()
        for raw_ref in refs:
            orig_key = raw_ref.key
            if not orig_key or orig_key in seen_orig_keys:
                continue
            seen_orig_keys.add(orig_key)
            if is_product_off_promotion(raw_ref.platform, raw_ref.goods_id, raw_ref.goods_sign):
                outcomes[orig_key] = DetailPrefetchResult("skipped", error_code="PRODUCT_OFF_PROMOTION")
                continue
            try:
                adapter = get_adapter(raw_ref.platform)
                if self._capability_error(adapter, "detail"):
                    outcomes[orig_key] = DetailPrefetchResult("skipped")
                    continue
                allowed = getattr(adapter, "can_prefetch_detail", None)
                if callable(allowed) and allowed(raw_ref) is False:
                    outcomes[orig_key] = DetailPrefetchResult("skipped")
                    continue
                upgraded = self._upgrade_ref_sign(raw_ref)
                item = self._read_detail(upgraded, strict=True)
                upgraded = self._upgrade_ref_sign(upgraded, item)
                if is_product_off_promotion(upgraded.platform, upgraded.goods_id, upgraded.goods_sign):
                    outcomes[orig_key] = DetailPrefetchResult("skipped", error_code="PRODUCT_OFF_PROMOTION")
                    continue
                deadline = self._detail_expires_at(item) if item else 0.0
                if deadline - time.time() > refresh_ahead_seconds:
                    saved = self._document_snapshot(item, upgraded.platform)
                    outcomes[orig_key] = DetailPrefetchResult("ready", saved, next_check_at=deadline - refresh_ahead_seconds)
                    continue
                local_deal_tip = item.get("deal_tip") if isinstance(item, dict) else None
                if not local_deal_tip and isinstance(deal_tips, dict):
                    local_deal_tip = deal_tips.get(orig_key)
                cached = self._cached_detail(upgraded)
                if cached and cached[0] > deadline and cached[0] - time.time() > refresh_ahead_seconds:
                    seed_data = dict(cached[1]["data"])
                    if isinstance(local_deal_tip, dict) and local_deal_tip and not seed_data.get("deal_tip"):
                        seed_data["deal_tip"] = deepcopy(local_deal_tip)
                    seed = self._make_snapshot(seed_data, datetime.fromtimestamp(cached[0] - PRODUCT_DETAIL_TTL, timezone.utc), upgraded.platform)
                    loaded = self._commit_snapshot(seed, stop_event=stop_event)
                    outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)
                    continue
                pending_by_plat.setdefault(upgraded.platform, []).append((orig_key, upgraded, adapter, local_deal_tip))
            except Exception as exc:
                exc_summary = format_exc_summary(exc)
                outcomes[orig_key] = DetailPrefetchResult("error", retryable=True, error_code="INTERNAL_ERROR", error_msg=exc_summary)

        detail_kwargs = {"allow_fallback": False} if stop_event is not None else {}
        retryable_codes = {"NETWORK_ERROR", "TIMEOUT", "RATE_LIMIT", "SERVICE_UNAVAILABLE"}

        for plat, entries in pending_by_plat.items():
            if stop_event is not None and stop_event.is_set():
                for orig_key, _u, _a, _dt in entries:
                    outcomes[orig_key] = DetailPrefetchResult("skipped")
                continue
            adapter = entries[0][2]
            batch_fn = getattr(adapter, "batch_get_goods_details", None)
            use_batch = callable(batch_fn) and getattr(adapter, "batch_detail_includes_gallery", True) is True
            if not use_batch:
                for orig_key, upgraded, ad_item, local_deal_tip in entries:
                    if stop_event is not None and stop_event.is_set():
                        outcomes[orig_key] = DetailPrefetchResult("skipped")
                        continue
                    try:
                        loaded = self._load_official(upgraded, ad_item, stop_event=stop_event,
                                                     refresh_ahead_seconds=refresh_ahead_seconds,
                                                     deal_tip=local_deal_tip)
                        outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)
                    except Exception as exc:
                        outcomes[orig_key] = DetailPrefetchResult("error", retryable=True, error_code="INTERNAL_ERROR", error_msg=format_exc_summary(exc))
                continue

            for idx in range(0, len(entries), 20):
                chunk_entries = entries[idx:idx + 20]
                if stop_event is not None and stop_event.is_set():
                    for orig_key, _u, _a, _dt in chunk_entries:
                        outcomes[orig_key] = DetailPrefetchResult("skipped")
                    continue
                try:
                    batch_map = batch_fn([e[1] for e in chunk_entries], **detail_kwargs) or {}
                except Exception as exc:
                    exc_summary = format_exc_summary(exc)
                    for orig_key, _u, _a, _dt in chunk_entries:
                        outcomes[orig_key] = DetailPrefetchResult("error", retryable=True, error_code="INTERNAL_ERROR", error_msg=exc_summary)
                    continue

                for orig_key, upgraded, _ad_item, local_deal_tip in chunk_entries:
                    raw_res = batch_map.get(upgraded.key) or batch_map.get(orig_key)
                    if not isinstance(raw_res, dict):
                        outcomes[orig_key] = DetailPrefetchResult("error", retryable=True, error_code="UPSTREAM_ERROR", error_msg="批量详情未返回结果")
                        continue
                    if raw_res.get("status") != "success" or not raw_res.get("data"):
                        code = str(raw_res.get("error_code") or "").upper()
                        loaded = _LoadedDetail(raw_res, retryable=code in retryable_codes)
                        outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)
                        continue
                    if normalize_platform(raw_res["data"].get("platform") or plat) != plat:
                        loaded = _LoadedDetail({"status": "error", "error_msg": "商品返回平台不匹配", "error_code": "product_identity_mismatch"})
                        outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)
                        continue
                    detail_payload = dict(raw_res["data"])
                    if not str(detail_payload.get("name") or "").strip():
                        loaded = _LoadedDetail({"status": "error", "error_code": "PRODUCT_OFF_PROMOTION", "error_msg": "该商品已取消推广或已下架，请挑选其他好物"})
                        outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)
                        continue
                    if isinstance(local_deal_tip, dict) and local_deal_tip and not detail_payload.get("deal_tip"):
                        detail_payload["deal_tip"] = deepcopy(local_deal_tip)
                    if raw_res.get("cacheable") is False:
                        public_res = deepcopy(raw_res)
                        public_res["data"] = detail_payload
                        loaded = _LoadedDetail(public_res, snapshot=None, persisted=False, retryable=True)
                        outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)
                        continue
                    try:
                        snap = self._make_snapshot(detail_payload, utc_millis(datetime.fromtimestamp(time.time(), timezone.utc)), plat)
                        loaded = self._commit_snapshot(snap, raw_res, stop_event, requested_ref=upgraded)
                    except ValueError:
                        loaded = _LoadedDetail({"status": "error", "error_code": "invalid_product_detail", "error_msg": "平台商品详情身份无效"})
                    outcomes[orig_key] = self._finalize_prefetch_loaded(upgraded, loaded, refresh_ahead_seconds)

        return outcomes

    def get_boom_products(self, platform="all", category=None, keyword=None, sort_by=None,
                          page=1, page_size=20) -> Dict[str, Any]:
        plat = normalize_platform(platform, "all")
        page = max(1, min(50, int(page or 1)))
        page_size = max(1, min(50, int(page_size or 20)))
        kw_key, effective_sort = resolve_boom_sort(keyword, sort_by)

        if effective_sort == "cost_performance" and kw_key and hasattr(self.repo, "rank_boom_products_by_cost_performance"):
            cat_key = category.strip() if category and category.strip() and category.strip() != "全部" else ""
            cache_key = (plat, cat_key, kw_key, effective_sort)
            sorted_items = BOOM_SORT_CACHE.get(cache_key)
            if sorted_items is None:
                try:
                    sorted_items = self.repo.rank_boom_products_by_cost_performance(
                        platform=plat, category=category, keyword=keyword,
                    )
                    BOOM_SORT_CACHE.set(cache_key, sorted_items)
                except Exception as exc:
                    logger.error(
                        "❌ 【爆品库查询失败】按性价比查询本地爆单商品列表时发生异常 | 平台[%s] 分类[%s] 关键词[%s] 排序[%s] 页码[%d] 异常<%s>",
                        plat, category or "全部", keyword or "-", effective_sort, page, format_exc_summary(exc),
                    )
                    return {
                        "status": "error",
                        "error_msg": str(exc),
                        "total_count": 0,
                        "page": page,
                        "page_size": page_size,
                        "has_more": False,
                        "data": [],
                    }
            skip = (page - 1) * page_size
            page_items = deepcopy(sorted_items[skip : skip + page_size])
            self._offer_products(page_items)
            return {
                "status": "success",
                "total_count": len(sorted_items),
                "page": page,
                "page_size": page_size,
                "has_more": (skip + page_size) < len(sorted_items),
                "data": page_items,
            }

        result = self.repo.query_boom_products(platform=plat, category=category,
                                              keyword=keyword, sort_by=effective_sort, page=page, page_size=page_size)
        if result.get("status") == "success":
            self._offer_products(result.get("data") or [])
        return result
