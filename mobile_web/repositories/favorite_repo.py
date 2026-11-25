# -*- coding: utf-8 -*-
"""单清单商品收藏夹仓储：独立 favorites 集合，零侵入商品表与用户表。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pymongo.errors import DuplicateKeyError

from config import logger
from infrastructure.logging_utils import format_exc_summary
from models.product import normalize_platform
from repositories.base_repo import BaseRepository

MAX_FAVORITES_PER_USER = 500


class FavoriteRepository(BaseRepository):
    COLLECTION_NAME = "favorites"

    def _init_indexes(self):
        try:
            self.col.create_index([("uid", 1), ("platform", 1), ("product_id", 1)], unique=True)
            self.col.create_index([("uid", 1), ("created_at", -1)])
            return True
        except Exception as exc:
            logger.warning(
                "❌ 【数据库索引告警】创建收藏夹集合索引时发生异常 | 集合[favorites] 异常<%s>",
                format_exc_summary(exc),
            )
            return False

    def is_favorited(self, uid: str, platform: str, product_id: str) -> bool:
        clean_uid = str(uid or "").strip()
        plat = normalize_platform(platform or "")
        pid = str(product_id or "").strip()
        if not clean_uid or not plat or not pid:
            return False
        doc = self.col.find_one({"uid": clean_uid, "platform": plat, "product_id": pid}, {"_id": 1})
        return doc is not None

    def count_by_uid(self, uid: str) -> int:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return 0
        return int(self.col.count_documents({"uid": clean_uid}))

    def add_favorite(
        self,
        uid: str,
        platform: str,
        product_id: str,
        collected_price: Optional[float],
        snapshot: Dict[str, str],
    ) -> Tuple[bool, bool]:
        """写入单条收藏记录；重复收藏保持幂等且不覆盖首次 collected_price。返回 (ok, already_exists)。"""
        clean_uid = str(uid or "").strip()
        plat = normalize_platform(platform or "")
        pid = str(product_id or "").strip()
        if not clean_uid or not plat or not pid:
            return False, False
        now = datetime.now(timezone.utc)
        doc = {
            "uid": clean_uid,
            "platform": plat,
            "product_id": pid,
            "collected_price": collected_price,
            "snapshot": {
                "name": str((snapshot or {}).get("name") or "").strip(),
                "image_url": str((snapshot or {}).get("image_url") or "").strip(),
                "goods_sign": str((snapshot or {}).get("goods_sign") or "").strip(),
            },
            "created_at": now,
        }
        try:
            self.col.insert_one(doc)
            return True, False
        except DuplicateKeyError:
            return True, True

    def remove_favorite(self, uid: str, platform: str, product_id: str) -> bool:
        clean_uid = str(uid or "").strip()
        plat = normalize_platform(platform or "")
        pid = str(product_id or "").strip()
        if not clean_uid or not plat or not pid:
            return False
        res = self.col.delete_one({"uid": clean_uid, "platform": plat, "product_id": pid})
        return int(getattr(res, "deleted_count", 0) or 0) > 0

    def list_by_uid(
        self,
        uid: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return [], 0
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(500, int(page_size or 20)))
        skip = (safe_page - 1) * safe_size
        total = int(self.col.count_documents({"uid": clean_uid}))
        if total == 0 or skip >= total:
            return [], total
        cursor = (
            self.col.find({"uid": clean_uid}, {"_id": 0})
            .sort([("created_at", -1)])
            .skip(skip)
            .limit(safe_size)
        )
        return list(cursor), total
