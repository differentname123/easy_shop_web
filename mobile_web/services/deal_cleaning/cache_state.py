# -*- coding: utf-8 -*-
"""有界转链锚点 LRU 缓存 (`AnchorConversionCache`) 与 TTL 活跃正式商品索引 (`ActiveProductIndex`)。"""
from __future__ import annotations

import copy
import datetime
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from models.deal_constants import (
    DEAL_INFO_PROTECTION_HOURS,
    ERR_ADAPTER_CONVERT_FAILED,
    MAX_ANCHOR_CACHE_SIZE,
)
from models.product import extract_taobao_token_core
from services.deal_cleaning.text_sanitizer import strip_markdown_link_wrapper
from services.deal_cleaning.url_venue import (
    _is_verified_station_promo_link,
    rebuild_clean_official_venue_url,
)

logger = logging.getLogger("deal_pool_scheduler")


class AnchorConversionCache:
    """线程安全的有界 LRU 转链结果缓存，屏蔽冗余大对象并自动防历史脏数据预热。"""

    def __init__(self, max_size: int = MAX_ANCHOR_CACHE_SIZE):
        self.lock = threading.Lock()
        self.max_size = max(1, int(max_size))
        self.warmed_up = False
        self.data: OrderedDict[str, Dict[str, Any]] = OrderedDict()

    @staticmethod
    def build_keys(plat: str, raw_anchor: str) -> List[str]:
        """生成锚点缓存主键：完整字符串 + 淘宝标准 11 位核心字母数字口令二级键。"""
        s = str(raw_anchor or "").strip()
        if not s:
            return []
        keys = [f"{plat}:{s}"]
        if plat == "taobao" and not s.lower().startswith("http"):
            tk = extract_taobao_token_core(s)
            if len(tk) == 11:
                tk_key = f"taobao:tk:{tk}"
                if tk_key not in keys:
                    keys.append(tk_key)
        return keys

    def set_locked(self, key: str, value: Dict[str, Any]) -> None:
        if not key:
            return
        if key in self.data:
            self.data.move_to_end(key)
        self.data[key] = value
        while len(self.data) > self.max_size:
            self.data.popitem(last=False)

    def get_locked(self, key: str) -> Optional[Dict[str, Any]]:
        val = self.data.get(key)
        if val is not None:
            self.data.move_to_end(key)
        return val

    _SLIM_PROMO_KEYS = (
        "url",
        "mobile_url",
        "short_url",
        "final_link",
        "promotion_token",
        "token_type",
        "coupon_url",
        "promoter_id",
        "link_type",
        "schema_url",
        "expires_at",
        "cacheable",
        "is_fallback",
    )

    @classmethod
    def extract_promo_payload(cls, res: Any) -> Dict[str, Any]:
        """提取可跨阶段/写回预置的轻量推广标量字段（严格剥离 product/raw_item 及私有归因字段）。"""
        if not isinstance(res, dict):
            return {}
        payload: Dict[str, Any] = {}
        for key in cls._SLIM_PROMO_KEYS:
            if key in res and res[key] is not None:
                payload[key] = res[key]
        we_app_info = res.get("we_app_info")
        if isinstance(we_app_info, dict) and we_app_info:
            payload["we_app_info"] = dict(we_app_info)
        return payload

    @classmethod
    def slim_adapter_res(cls, res: Any, *, keep_product: bool) -> Dict[str, Any]:
        if not keep_product or not isinstance(res, dict):
            return {}
        prod = res.get("product")
        if not isinstance(prod, dict) or not prod:
            return {}
        slim_prod = {k: v for k, v in prod.items() if k != "raw_item"}
        slimmed: Dict[str, Any] = {
            "status": "success",
            "product": slim_prod,
            "price": res.get("price"),
            "coupon_amount": res.get("coupon_amount"),
            "title": res.get("title"),
        }
        slimmed.update(cls.extract_promo_payload(res))
        return slimmed

    def evict_transient_failures(self) -> None:
        """驱逐暂态失败缓存项 (`ERR_ADAPTER_CONVERT_FAILED`)，保留确定性永久失败。"""
        with self.lock:
            stale_keys = [
                k
                for k, v in self.data.items()
                if isinstance(v, dict)
                and v.get("convert_status") == "failed"
                and v.get("error_code") == ERR_ADAPTER_CONVERT_FAILED
            ]
            for k in stale_keys:
                self.data.pop(k, None)

    def warmup_from_db(self, raw_col) -> None:
        """启动时从初步表恢复最近 12h 内有效转链锚点（屏蔽未实证的 JD 短链 OK_COUPON 历史记录）。"""
        if self.warmed_up or raw_col is None:
            return
        self.warmed_up = True
        try:
            cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
                hours=DEAL_INFO_PROTECTION_HOURS
            )
            cursor = (
                raw_col.find(
                    {
                        "cleaned_at": {"$gte": cutoff},
                        "converted_links.0": {"$exists": True},
                    },
                    {"_id": 0, "converted_links": 1},
                )
                .sort("cleaned_at", -1)
                .limit(3000)
            )
            loaded = 0
            with self.lock:
                for doc in cursor:
                    for cl in doc.get("converted_links") or []:
                        if not isinstance(cl, dict):
                            continue
                        c_status = cl.get("convert_status")
                        c_plat = str(cl.get("platform") or "").strip()
                        c_inp = strip_markdown_link_wrapper(str(cl.get("clean_input") or "").strip())
                        if c_plat not in ("jd", "taobao", "pdd") or not c_inp:
                            continue
                        p_link = str(cl.get("my_promo_link") or "").strip()
                        p_tok = str(cl.get("my_promo_token") or "").strip()
                        cl_copy = copy.deepcopy(cl)
                        cl_copy["clean_input"] = c_inp
                        cl_copy.pop("adapter_res", None)
                        if c_status == "product":
                            if not (p_link or p_tok):
                                continue
                        elif c_status == "coupon_or_activity":
                            c_inp_low = c_inp.lower()
                            if (
                                c_plat == "jd"
                                and cl.get("error_code") == "OK_COUPON"
                                and cl.get("resolution_kind") != "coupon_or_venue"
                                and ("u.jd.com" in c_inp_low or "union-click.jd.com" in c_inp_low)
                            ):
                                continue
                            is_new_link = (
                                cl.get("error_code") != "OK_DIRECT_COUPON"
                                and _is_verified_station_promo_link(c_inp, p_link)
                            )
                            is_new_tok = (
                                cl.get("error_code") != "OK_DIRECT_COUPON"
                                and bool(p_tok)
                                and p_tok != c_inp
                            )
                            if is_new_link or is_new_tok:
                                rebuilt_promo = rebuild_clean_official_venue_url(p_link) if p_link else None
                                cl_copy["my_promo_link"] = p_link if is_new_link else rebuilt_promo
                                cl_copy["my_promo_token"] = p_tok if is_new_tok else None
                            else:
                                cand_venue = (
                                    p_link if cl.get("error_code") == "OK_DIRECT_COUPON" and p_link else c_inp
                                )
                                rebuilt = rebuild_clean_official_venue_url(cand_venue)
                                if not rebuilt:
                                    continue
                                cl_copy["error_code"] = "OK_DIRECT_COUPON"
                                cl_copy["my_promo_link"] = rebuilt
                                cl_copy["my_promo_token"] = None
                        else:
                            continue
                        for ckey in self.build_keys(c_plat, c_inp):
                            if ckey not in self.data:
                                self.set_locked(ckey, cl_copy)
                                loaded += 1
            if loaded > 0:
                logger.info("🔥 [转链缓存预热] 已从 `deal_raw_tips` 恢复最近 12h 有效转链锚点 %d 个", loaded)
        except Exception as e:
            logger.warning("预热转链缓存异常: %s", e)


class ActiveProductIndex:
    """线程安全、带 TTL 与 LRU 容量上限及显式失效接口的活跃正式商品内存索引。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEAL_INFO_PROTECTION_HOURS * 3600.0,
        max_size: int = 10000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._lock = threading.RLock()
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_size = max(1, int(max_size))
        self._clock = clock
        self._entries: OrderedDict[Tuple[str, str], Dict[str, Any]] = OrderedDict()

    def _is_fresh_locked(self, record: Dict[str, Any]) -> bool:
        return (self._clock() - float(record["cached_at"])) <= self.ttl_seconds

    def _prune_expired_locked(self) -> None:
        now_val = self._clock()
        expired = [k for k, rec in self._entries.items() if (now_val - float(rec["cached_at"])) > self.ttl_seconds]
        for k in expired:
            self._entries.pop(k, None)

    def get(self, key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._entries.get(key)
            if rec is None:
                return None
            if not self._is_fresh_locked(rec):
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(rec["data"])

    def upsert(self, key: Tuple[str, str], entry: Dict[str, Any], *, source_site: str = "") -> None:
        plat = str(key[0] or "").strip()
        pid = str(key[1] or "").strip()
        if not plat or not pid or not isinstance(entry, dict):
            return
        site = str(source_site or entry.get("source_site") or "").strip()
        with self._lock:
            norm_key = (plat, pid)
            if norm_key in self._entries:
                self._entries.move_to_end(norm_key)
            self._entries[norm_key] = {
                "data": copy.deepcopy(entry),
                "cached_at": self._clock(),
                "source_site": site,
            }
            while len(self._entries) > self.max_size:
                self._entries.popitem(last=False)

    def upsert_from_product_doc(self, pdoc: Dict[str, Any]) -> None:
        if not isinstance(pdoc, dict):
            return
        plat = str(pdoc.get("platform") or "").strip()
        pid = str(pdoc.get("product_id") or "").strip()
        if not plat or not pid:
            return
        di = pdoc.get("deal_info") if isinstance(pdoc.get("deal_info"), dict) else {}
        new_q = int(di.get("quality_score") or 75)
        new_dp = _positive_float(di.get("deal_price"))
        prev_entry = self.get((plat, pid))
        ex_dp = _positive_float(prev_entry.get("deal_price")) if prev_entry else None
        improved = bool(new_dp is not None and ex_dp is not None and new_dp < ex_dp * 0.98)
        if prev_entry is None or new_q >= int(prev_entry.get("quality_score") or 0) or improved:
            t_steps = di.get("tutorial_steps")
            t_cnt = len(t_steps) if isinstance(t_steps, list) else (1 if t_steps else 0)
            t_score = int(di.get("tutorial_score") if di.get("tutorial_score") is not None else (di.get("quality_score") or new_q))
            site = str(di.get("source_site") or pdoc.get("deal_source") or "").strip()
            self.upsert(
                (plat, pid),
                {
                    "platform": plat,
                    "product_id": pid,
                    "goods_sign": str(pdoc.get("goods_sign") or ""),
                    "official_title": str(pdoc.get("name") or ""),
                    "official_price": pdoc.get("price"),
                    "deal_price": di.get("deal_price"),
                    "quality_score": new_q,
                    "tutorial_steps_count": t_cnt,
                    "tutorial_score": t_score,
                    "source_site": site,
                },
                source_site=site,
            )

    def invalidate(self, platform: str, product_id: str) -> bool:
        key = (str(platform or "").strip(), str(product_id or "").strip())
        with self._lock:
            return self._entries.pop(key, None) is not None

    def invalidate_site(self, source_site: str) -> int:
        site = str(source_site or "").strip()
        if not site:
            return 0
        with self._lock:
            to_drop = [k for k, rec in self._entries.items() if rec.get("source_site") == site]
            for k in to_drop:
                self._entries.pop(k, None)
            return len(to_drop)

    def invalidate_all(self) -> int:
        with self._lock:
            cnt = len(self._entries)
            self._entries.clear()
            return cnt

    def clear(self) -> None:
        self.invalidate_all()

    def load_from_formal_col(
        self,
        formal_col,
        pid_keys: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> "ActiveProductIndex":
        entries = query_active_formal_products(formal_col, pid_keys=pid_keys)
        with self._lock:
            for k, val in entries.items():
                self.upsert(k, val, source_site=str(val.get("source_site") or ""))
        return self

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        with self._lock:
            rec = self._entries.get(key)
            if rec is None:
                return False
            if not self._is_fresh_locked(rec):
                self._entries.pop(key, None)
                return False
            return True

    def __getitem__(self, key: Tuple[str, str]) -> Dict[str, Any]:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key: Tuple[str, str], value: Dict[str, Any]) -> None:
        self.upsert(key, value)

    def __len__(self) -> int:
        with self._lock:
            self._prune_expired_locked()
            return len(self._entries)


def _positive_float(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) > 0:
        return float(val)
    return None


def query_active_formal_products(
    formal_col,
    pid_keys: Optional[Iterable[Tuple[str, str]]] = None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """精确查询正式库 12h 内活跃好价商品摘要映射。"""
    active_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if formal_col is None:
        return active_map
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(
            hours=DEAL_INFO_PROTECTION_HOURS
        )
        base_q: Dict[str, Any] = {
            "deal_info.is_deal_tip": True,
            "deal_info.updated_at": {"$gte": cutoff},
        }
        if pid_keys is not None:
            keys_list = [(str(p or "").strip(), str(i or "").strip()) for p, i in pid_keys if p and i]
            if not keys_list:
                return active_map
            if len(keys_list) == 1:
                base_q["platform"] = keys_list[0][0]
                base_q["product_id"] = keys_list[0][1]
            else:
                base_q["$or"] = [{"platform": p, "product_id": pid} for p, pid in keys_list]

        cursor = formal_col.find(
            base_q,
            {
                "_id": 0,
                "platform": 1,
                "product_id": 1,
                "goods_sign": 1,
                "name": 1,
                "price": 1,
                "deal_source": 1,
                "deal_info.source_site": 1,
                "deal_info.quality_score": 1,
                "deal_info.deal_price": 1,
                "deal_info.tutorial_steps": 1,
                "deal_info.tutorial_score": 1,
            },
        )
        for d in cursor:
            plat = str(d.get("platform") or "").strip()
            pid = str(d.get("product_id") or "").strip()
            if plat and pid:
                di = d.get("deal_info") if isinstance(d.get("deal_info"), dict) else {}
                t_steps = di.get("tutorial_steps")
                t_cnt = len(t_steps) if isinstance(t_steps, list) else (1 if t_steps else 0)
                active_map[(plat, pid)] = {
                    "platform": plat,
                    "product_id": pid,
                    "goods_sign": str(d.get("goods_sign") or ""),
                    "official_title": str(d.get("name") or ""),
                    "official_price": d.get("price"),
                    "deal_price": di.get("deal_price"),
                    "quality_score": int(di.get("quality_score") or 75),
                    "tutorial_steps_count": t_cnt,
                    "tutorial_score": int(di.get("tutorial_score") or di.get("quality_score") or 75),
                    "source_site": str(di.get("source_site") or d.get("deal_source") or ""),
                }
    except Exception as e:
        logger.warning("查询正式表已清洗活跃商品集合异常: %s", e)
    return active_map


def lookup_active_product(
    store: Any,
    key: Optional[Tuple[str, str]],
    lock: Optional[threading.Lock] = None,
) -> Optional[Dict[str, Any]]:
    """兼容 ActiveProductIndex 与普通 dict 读取活跃商品。"""
    if store is None or key is None:
        return None
    if isinstance(store, ActiveProductIndex):
        return store.get(key)
    if lock is not None:
        with lock:
            v = store.get(key)
            return copy.deepcopy(v) if v is not None else None
    v = store.get(key)
    return copy.deepcopy(v) if v is not None else None


def update_active_product_store(
    store: Any,
    product_docs: List[Dict[str, Any]],
    lock: Optional[threading.Lock] = None,
) -> None:
    """兼容 ActiveProductIndex 与普通 dict 回写最新成功商品。"""
    if store is None or not product_docs:
        return
    if isinstance(store, ActiveProductIndex):
        for pdoc in product_docs:
            store.upsert_from_product_doc(pdoc)
        return

    def _update_dict() -> None:
        for pdoc in product_docs:
            plat = str(pdoc.get("platform") or "").strip()
            pid = str(pdoc.get("product_id") or "").strip()
            if not plat or not pid:
                continue
            di = pdoc.get("deal_info") if isinstance(pdoc.get("deal_info"), dict) else {}
            new_q = int(di.get("quality_score") or 75)
            prev_entry = store.get((plat, pid))
            new_dp = _positive_float(di.get("deal_price"))
            ex_dp = _positive_float(prev_entry.get("deal_price")) if prev_entry else None
            improved = bool(new_dp is not None and ex_dp is not None and new_dp < ex_dp * 0.98)
            if prev_entry is None or new_q >= int(prev_entry.get("quality_score") or 0) or improved:
                t_steps = di.get("tutorial_steps")
                store[(plat, pid)] = {
                    "platform": plat,
                    "product_id": pid,
                    "goods_sign": str(pdoc.get("goods_sign") or ""),
                    "official_title": str(pdoc.get("name") or ""),
                    "official_price": pdoc.get("price"),
                    "deal_price": di.get("deal_price"),
                    "quality_score": new_q,
                    "tutorial_steps_count": len(t_steps) if isinstance(t_steps, list) else (1 if t_steps else 0),
                    "tutorial_score": int(di.get("tutorial_score") or di.get("quality_score") or new_q),
                    "source_site": str(di.get("source_site") or pdoc.get("deal_source") or ""),
                }

    if lock is not None:
        with lock:
            _update_dict()
    else:
        _update_dict()


def should_upgrade_existing_deal(
    ex_info: Dict[str, Any],
    ai1_res: Dict[str, Any],
    doc: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """判断新线报是否满足库内同款商品择优升级条件（>=5% 价格突破或 >=15 分教程质量提升）。"""
    _ = doc
    new_dp = ai1_res.get("deal_price")
    ex_dp = ex_info.get("deal_price")
    new_score = int(ai1_res.get("tutorial_score") or 0)
    ex_score = int(ex_info.get("tutorial_score") or ex_info.get("quality_score") or 75)

    if (
        isinstance(new_dp, (int, float))
        and isinstance(ex_dp, (int, float))
        and float(ex_dp) > 0
        and float(new_dp) > 0
        and float(new_dp) < float(ex_dp) * 0.95
    ):
        return True, f"新线报预估价({float(new_dp):.2f}元)显著低于库内旧价格({float(ex_dp):.2f}元)，触发价格择优升级"

    if new_score >= 75 and new_score >= ex_score + 15:
        return True, f"新线报教程实操分({new_score})显著超越库内现有水平({ex_score})，触发教程补全升级"

    return False, ""
