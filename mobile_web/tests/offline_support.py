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
    replacements = (
        patch.dict(os.environ, {
            "WEB_CONFIG_FILE": str(config_path),
            "WEB_AUTH_SECRET": _CONFIG["web_auth_secret"],
            "PDD_CLIENT_ID": _CONFIG["myself_pdd_client_id"],
            "PDD_CLIENT_SECRET": _CONFIG["myself_pdd_client_secret"],
            "PDD_PID": _CONFIG["myself_pdd_pid"],
            "MONGO_HOST": "localhost", "MONGO_PORT": "27017",
            "MONGO_USER": "", "MONGO_PASSWORD": "", "MONGO_DB_NAME": "mobile_web_offline",
            "MONGO_AUTH_SOURCE": "admin", "WEB_HOST": "127.0.0.1", "WEB_PORT": "8000",
            "WEB_LOG_DIR": "-", "WEB_LOG_LEVEL": "INFO", "WEB_WARMUP_ENABLED": "1",
            "PDD_DNS_MODE": "off", "PDD_TRUST_ENV": "0",
        }),
        patch("pymongo.MongoClient", new=_deny("MongoClient")),
        patch("requests.sessions.Session.request", new=_deny("platform HTTP")),
    )
    for replacement in replacements:
        replacement.start()
        _PATCHES.append(replacement)
