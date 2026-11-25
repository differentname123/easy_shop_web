# -*- coding: utf-8 -*-
"""在测试收集/业务导入之前安装离线边界，而不是等普通 fixture 执行。"""
from tests.offline_support import BLOCKED_CALLS, assert_no_blocked_calls, install_offline_environment

install_offline_environment()

import pytest


import sys


@pytest.fixture(scope="session", autouse=True)
def offline_import_and_shutdown():
    assert_no_blocked_calls()
    yield
    assert_no_blocked_calls()


@pytest.fixture(autouse=True)
def offline_resources_and_registry(tmp_path):
    import adapters
    from cache import clear_all_caches
    import infrastructure.api_governance as api_gov

    registry = dict(adapters._ADAPTER_REGISTRY)
    orig_health_dir = api_gov._HEALTH_DIR
    orig_process_role = api_gov._PROCESS_ROLE
    had_playwright = "playwright" in sys.modules
    orig_playwright = sys.modules.get("playwright")

    api_gov._HEALTH_DIR = tmp_path / "runtime_health"
    api_gov.GLOBAL_UPSTREAM_HEALTH.reset()
    api_gov.GLOBAL_QUOTA_PACER.reset()
    model_api_mod = sys.modules.get("infrastructure.ai_api.model_api")
    orig_model_usage_path = getattr(model_api_mod, "MODEL_USAGE_JSON_PATH", None) if model_api_mod is not None else None
    if model_api_mod is not None:
        model_api_mod.MODEL_USAGE_JSON_PATH = tmp_path / "model_usage.json"

    start = len(BLOCKED_CALLS)
    clear_all_caches()
    try:
        yield
    finally:
        adapters._ADAPTER_REGISTRY.clear()
        adapters._ADAPTER_REGISTRY.update(registry)
        clear_all_caches()
        mongo_mod = sys.modules.get("infrastructure.mongo")
        if mongo_mod is not None:
            mongo_mod.close_shared_db()
        meili_mod = sys.modules.get("infrastructure.search_meili")
        if meili_mod is not None:
            meili_mod.reset_meili_state()
        api_gov._HEALTH_DIR = orig_health_dir
        api_gov._PROCESS_ROLE = orig_process_role
        api_gov.GLOBAL_UPSTREAM_HEALTH.reset()
        api_gov.GLOBAL_QUOTA_PACER.reset()
        if model_api_mod is None:
            model_api_mod = sys.modules.get("infrastructure.ai_api.model_api")
            orig_model_usage_path = None
        if model_api_mod is not None:
            model_api_mod.MODEL_USAGE_JSON_PATH = orig_model_usage_path
            model_api_mod.reset_ai_cooldowns()
            model_api_mod.reset_usage_tracker()
        api_order_mod = sys.modules.get("api.order")
        if api_order_mod is not None and hasattr(api_order_mod, "order_service"):
            reset_fn = getattr(api_order_mod.order_service, "reset_state", None)
            if callable(reset_fn):
                reset_fn()
        api_home_mod = sys.modules.get("api.home")
        if api_home_mod is not None and hasattr(api_home_mod, "product_stats_buffer"):
            reset_stats = getattr(api_home_mod.product_stats_buffer, "reset_state", None)
            if callable(reset_stats):
                reset_stats()
        if not had_playwright:
            for mod_name in list(sys.modules):
                if mod_name == "playwright" or mod_name.startswith("playwright."):
                    sys.modules.pop(mod_name, None)
        else:
            sys.modules["playwright"] = orig_playwright
        assert BLOCKED_CALLS[start:] == [], "测试尝试访问真实资源，即使业务代码捕获了异常也不算通过"

