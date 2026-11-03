# -*- coding: utf-8 -*-
"""淘宝仅保留未启用的兼容占位，不包含平台业务实现。"""
from adapters.base import UnavailablePlatformAdapter


class TaobaoAdapter(UnavailablePlatformAdapter):
    platform_code = "taobao"
    platform_name = "淘宝"
