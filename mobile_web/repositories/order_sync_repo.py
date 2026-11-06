# -*- coding: utf-8 -*-
"""单 worker 的最小订单检查点；不是多 worker 租约或分布式锁。"""
from copy import deepcopy
from typing import Optional

from models.product import normalize_platform
from repositories.base_repo import BaseRepository


class OrderSyncRepository(BaseRepository):
    COLLECTION_NAME = "order_sync_state"

    def get_state(self, platform: str) -> Optional[dict]:
        # _id 就是平台唯一键，无需额外索引或旧订单迁移。
        doc = self.col.find_one({"_id": normalize_platform(platform)})
        if doc is None:
            return None
        return {"last_success_end": doc.get("last_success_end", 0),
                "pending": deepcopy(doc.get("pending"))}

    def save_state(self, platform: str, state: dict) -> None:
        """一次写入窗口和成功终点；失败必须上抛，调用方只能重放该页。"""
        result = self.col.update_one(
            {"_id": normalize_platform(platform)},
            {"$set": {"last_success_end": state.get("last_success_end", 0),
                      "pending": deepcopy(state.get("pending"))}},
            upsert=True,
        )
        if not result.acknowledged:
            raise RuntimeError("订单同步检查点未确认写入")
