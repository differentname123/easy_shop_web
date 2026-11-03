# -*- coding: utf-8 -*-
"""京东仅保留未启用的兼容占位，不包含平台业务实现。"""
from adapters.base import UnavailablePlatformAdapter


class JdAdapter(UnavailablePlatformAdapter):
    platform_code = "jd"
    platform_name = "京东"
