# -*- coding: utf-8 -*-
"""可插拔销售渠道注册表（未来新增淘宝等渠道仅需注册新适配器）。"""
from __future__ import annotations

from typing import Dict, List

from ..config import WorkbenchConfig
from .base import SellingChannel
from .xianyu import XianyuChannelAdapter


class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: Dict[str, SellingChannel] = {}

    def register(self, adapter: SellingChannel) -> None:
        code = str(adapter.channel_code or "").strip().lower()
        if not code:
            raise ValueError("channel_code 不能为空")
        self._channels[code] = adapter

    def get(self, channel_code: str) -> SellingChannel:
        code = str(channel_code or "").strip().lower()
        adapter = self._channels.get(code)
        if adapter is None:
            raise KeyError(f"不支持的销售渠道: {channel_code}")
        return adapter

    def list_all(self) -> List[SellingChannel]:
        return list(self._channels.values())

    def shutdown(self) -> None:
        for adapter in self._channels.values():
            try:
                adapter.shutdown()
            except Exception:
                pass


def build_default_channel_registry(config: WorkbenchConfig) -> ChannelRegistry:
    registry = ChannelRegistry()
    registry.register(XianyuChannelAdapter(config.channel_profiles_dir))
    return registry
