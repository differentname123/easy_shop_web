# -*- coding: utf-8 -*-
"""测试/预览专用资源边界；必须在导入应用之前安装，生产入口不得使用。"""
import atexit
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

BLOCKED_CALLS = []
_PATCHES = []
_CONFIG = {
    "myself_pdd_client_id": "offline-client",
    "myself_pdd_client_secret": "offline-secret",
    "myself_pdd_pid": "offline-pid",
    "local_mongo_host": "localhost",
    "local_mongo_port": 27017,
    "local_mongo_user": "",
    "local_mongo_password": "",
    "local_mongo_db_name": "mobile_web_offline",
    "web_auth_secret": "mobile-web-offline-test-secret-not-for-deployment",
    "web_host": "127.0.0.1",
    "web_log_dir": "-",
    "web_warmup_enabled": True,
    "web_detail_prefetch_enabled": False,
    "web_deal_submit_worker_enabled": False,
    "pdd_dns_mode": "off",
    "pdd_trust_env": False,
    # 离线边界必须关掉 Meilisearch：清洗结果写正式表时会调 is_meili_available() 探活，
    # enabled 默认 True 就会真的去打 http://localhost:7700/health，被离线守卫判为"访问真实资源"。
    "meilisearch_enabled": False,
}


def _deny(resource):
    def blocked(*args, **kwargs):
        BLOCKED_CALLS.append(resource)
        raise AssertionError(f"离线验证禁止访问真实资源: {resource}")
    return blocked


def assert_no_blocked_calls():
    assert BLOCKED_CALLS == [], f"离线验证曾尝试访问真实资源: {BLOCKED_CALLS}"


def install_offline_environment():
    """注入完整假环境，但使用真实配置解析器；阻断 Mongo/HTTP 的实际出口。"""
    if _PATCHES:
        return
    directory = tempfile.TemporaryDirectory(prefix="mobile-web-offline-")
    atexit.register(directory.cleanup)
    config_path = Path(directory.name) / "config.json"
    config_path.write_text(json.dumps(_CONFIG), encoding="utf-8")
    replacements = [
        patch.dict(os.environ, {
            "WEB_CONFIG_FILE": str(config_path),
            "WEB_LOG_DIR": "-",
            "MODEL_USAGE_JSON_PATH": str(Path(directory.name) / "model_usage.json"),
            "MOBILE_WEB_OFFLINE_MODE": "1",
        }),
        patch("pymongo.MongoClient", new=_deny("MongoClient")),
        patch("requests.sessions.Session.request", new=_deny("platform HTTP")),
    ]
    try:
        import httpx  # noqa: F401
        replacements.append(patch("httpx.HTTPTransport.handle_request", new=_deny("httpx HTTP")))
        replacements.append(patch("httpx.AsyncHTTPTransport.handle_async_request", new=_deny("httpx async HTTP")))
    except ImportError:
        pass
    for replacement in replacements:
        replacement.start()
        _PATCHES.append(replacement)
