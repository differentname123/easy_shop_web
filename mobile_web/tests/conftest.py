# -*- coding: utf-8 -*-
"""在测试收集/业务导入之前安装离线边界，而不是等普通 fixture 执行。"""
from tests.offline_support import BLOCKED_CALLS, assert_no_blocked_calls, install_offline_environment

install_offline_environment()

import pytest


@pytest.fixture(scope="session", autouse=True)
def offline_import_and_shutdown():
    assert_no_blocked_calls()
    yield
    assert_no_blocked_calls()


@pytest.fixture(autouse=True)
def offline_resources_and_registry():
    import adapters
    from cache import clear_all_caches

    registry = dict(adapters._ADAPTER_REGISTRY)
    start = len(BLOCKED_CALLS)
    clear_all_caches()
    yield
    adapters._ADAPTER_REGISTRY.clear()
    adapters._ADAPTER_REGISTRY.update(registry)
    clear_all_caches()
    assert BLOCKED_CALLS[start:] == [], "测试尝试访问真实资源，即使业务代码捕获了异常也不算通过"
