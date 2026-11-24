# -*- coding: utf-8 -*-
"""有界转链锚点 LRU 缓存 (`AnchorConversionCache`) 与正式表活跃商品批量去重查询。"""
from __future__ import annotations

import datetime
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from models.deal_constants import (
    DEAL_INFO_PROTECTION_HOURS,
    ERR_ADAPTER_CONVERT_FAILED,
    MAX_ANCHOR_CACHE_SIZE,
)
from models.product import extract_taobao_token_core

logger = logging.getLogger("deal_pool_scheduler")


class AnchorConversionCache:
    """线程安全的有界 LRU 转链结果缓存，屏蔽冗余大对象，仅保留运行期有效结果。"""

    def __init__(self, max_size: int = MAX_ANCHOR_CACHE_SIZE):
        self.lock = threading.Lock()
        self.max_size = max(1, int(max_size))
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
                "deal_info.source_is_reliable": 1,
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
                    "source_is_reliable": bool(di.get("source_is_reliable")),
                }
    except Exception as e:
        logger.warning("查询正式表已清洗活跃商品集合异常: %s", e)
    return active_map


def should_upgrade_existing_deal(
    ex_info: Dict[str, Any],
    ai1_res: Dict[str, Any],
    doc: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """判断新线报是否满足库内同款商品替换条件：高可靠来源优先于非高可靠来源，同级别按 >=5% 价格突破或 >=15 分教程质量提升。"""
    ex_reliable = bool(ex_info.get("source_is_reliable"))
    new_reliable = bool((doc or {}).get("source_is_reliable"))
    if new_reliable != ex_reliable:
        return (True, "高可靠来源线报覆盖非高可靠来源") if new_reliable else (False, "")

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
