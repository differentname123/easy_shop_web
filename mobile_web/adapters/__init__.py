# -*- coding: utf-8 -*-
"""显式平台装配表：新增适配器后在此登记，不使用插件发现或动态加载。"""
from functools import lru_cache
from typing import Dict, List, Any
from adapters.base import BasePlatformAdapter
from adapters.pdd_adapter import PddAdapter
from adapters.tb_adapter import TaobaoAdapter
from adapters.jd_adapter import JdAdapter
from models.product import normalize_platform


_ADAPTER_TYPES = {
    "pdd": PddAdapter,
    "taobao": TaobaoAdapter,
    "jd": JdAdapter,
}
_ADAPTER_REGISTRY: Dict[str, BasePlatformAdapter] = {}


class PlatformUnavailableError(ValueError):
    """未知或未启用的平台，供 API 统一转换为可读业务错误。"""


def register_adapter(platform_code: str, adapter_instance: BasePlatformAdapter):
    """显式装配入口，也用于测试替身；没有运行期插件加载机制。"""
    _ADAPTER_REGISTRY[normalize_platform(platform_code)] = adapter_instance


@lru_cache(maxsize=None)
def _create_adapter(platform: str) -> BasePlatformAdapter:
    return _ADAPTER_TYPES[platform]()


def get_adapter(platform: str = "pdd") -> BasePlatformAdapter:
    """兼容获取占位适配器；业务入口须检查 active 或使用 require_adapter。"""
    code = normalize_platform(platform)
    if code in _ADAPTER_REGISTRY:
        return _ADAPTER_REGISTRY[code]
    if code not in _ADAPTER_TYPES:
        raise PlatformUnavailableError(f"不支持的平台: {platform}")
    return _create_adapter(code)


def require_adapter(platform: str = "pdd") -> BasePlatformAdapter:
    adapter = get_adapter(platform)
    if not adapter.active:
        raise PlatformUnavailableError(f"{adapter.platform_name}暂未接入")
    return adapter


def platform_name(platform: str) -> str:
    code = normalize_platform(platform)
    descriptor = _ADAPTER_REGISTRY.get(code) or _ADAPTER_TYPES.get(code)
    return descriptor.platform_name if descriptor else code


def get_all_platforms() -> List[Dict[str, Any]]:
    """平台名称与是否启用均来自同一装配表，前后端不再各自猜测。"""
    descriptors = {**_ADAPTER_TYPES, **_ADAPTER_REGISTRY}
    return [
        {"code": code, "name": adapter.platform_name, "active": adapter.active}
        for code, adapter in descriptors.items()
    ]
