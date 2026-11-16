# -*- coding: utf-8 -*-
"""单元测试：云端多官方 API Key 轮询池、独立海外代理隔离、自定义模型列表及 --check-ai 自检。"""
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from infrastructure.ai_api import model_api
import run as runner

_REAL_TIME_SLEEP = time.sleep


class _FakeRateLimitError(Exception):
    status_code = 429


def _make_fake_raw_response(text='{"ok": true}', model_name="gemini-3.8-flash", status_code=200):
    parsed = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        model=model_name,
    )
    return SimpleNamespace(status_code=status_code, parse=lambda: parsed)


@pytest.fixture(autouse=True)
def _reset_key_pool_state(tmp_path, monkeypatch):
    with model_api._KEY_POOL_LOCK:
        model_api._KEY_POOL_CURSORS.clear()
        model_api._KEY_COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(model_api, "MODEL_USAGE_JSON_PATH", tmp_path / "model_usage.json")
    monkeypatch.setattr(model_api.time, "sleep", lambda *_args, **_kwargs: None)
    # 确保测试期间按 WEB_CONFIG_FILE 动态加载，不受进程启动时初始值干扰
    monkeypatch.setattr(model_api, "_INITIAL_BASE_URL", model_api.BASE_URL)
    monkeypatch.setattr(model_api, "_INITIAL_API_KEY", model_api.API_KEY)


def test_multi_key_pool_rotates_and_skips_429_exhausted_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "cloud_config.json"
    cfg.write_text(
        json.dumps(
            {
                "local_api_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "local_api_key_list": [
                    "AIzaSyDummyExhaustedKey0001",
                    "AIzaSyDummyExhaustedKey0002",
                    "AIzaSyDummyHealthyKey000003",
                ],
                "ai_models": ["gemini-3.8-flash"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg))

    used_keys = []
    closed_clients = []

    def fake_openai_ctor(*, base_url, api_key, max_retries, timeout, http_client=None):
        def create_call(**kwargs):
            used_keys.append(api_key)
            if api_key != "AIzaSyDummyHealthyKey000003":
                raise _FakeRateLimitError(f"429 RESOURCE_EXHAUSTED for {api_key}")
            return _make_fake_raw_response('{"is_compliant": true}', model_name=kwargs["model"])

        cli = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=SimpleNamespace(create=create_call))),
            close=lambda: closed_clients.append(api_key),
        )
        return cli

    monkeypatch.setattr(model_api, "OpenAI", fake_openai_ctor)

    # 即使 max_retries_per_model=2，遇到 429 密钥限流也会自动跨越前两个耗尽 Key 命中第 3 个健康 Key
    res = model_api.generate_content(prompt="识别这张拼团海报", max_retries_per_model=2, timeout=30)
    assert res["status"] == "✅ 成功"
    assert res["content"] == '{"is_compliant": true}'
    assert used_keys == [
        "AIzaSyDummyExhaustedKey0001",
        "AIzaSyDummyExhaustedKey0002",
        "AIzaSyDummyHealthyKey000003",
    ]
    assert res["metrics"]["key_slot"] == "Key#3/3"
    assert len(closed_clients) == 3
    # 错误历史中绝不能泄露真实的 AIzaSy 密钥明文
    joined_errors = "\n".join(res["error_history"])
    assert "AIzaSyDummyExhaustedKey0001" not in joined_errors
    assert "AIzaSyDummyExhaustedKey0002" not in joined_errors

    # 下一次调用应直接跳过仍在冷却期的 Key#1 / Key#2，直接命中健康的 Key#3
    used_keys.clear()
    res2 = model_api.generate_content(prompt="识别第二张海报", max_retries_per_model=1, timeout=30)
    assert res2["status"] == "✅ 成功"
    assert used_keys == ["AIzaSyDummyHealthyKey000003"]


def test_isolated_ai_proxy_url_mounts_dedicated_httpx_client_and_closes_cleanly(tmp_path, monkeypatch):
    cfg = tmp_path / "proxy_config.json"
    cfg.write_text(
        json.dumps(
            {
                "local_api_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "local_api_key": "AIzaSyProxyTestSecretKey999",
                "ai_proxy_url": "socks5://proxyuser:proxypass@43.131.225.155:1080",
                "ai_models": ["gemini-3.8-flash", "gemini-3.6-flash"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg))

    created_httpx = []
    closed_httpx = []
    received_http_clients = []

    def fake_httpx_client(*, proxy, timeout, trust_env):
        assert proxy == "socks5://proxyuser:proxypass@43.131.225.155:1080"
        assert trust_env is False
        obj = SimpleNamespace(proxy=proxy, close=lambda: closed_httpx.append(True))
        created_httpx.append(obj)
        return obj

    def fake_openai_ctor(*, base_url, api_key, max_retries, timeout, http_client=None):
        received_http_clients.append(http_client)
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    with_raw_response=SimpleNamespace(
                        create=lambda **kw: _make_fake_raw_response('{"ok":1}', model_name=kw["model"])
                    )
                )
            ),
            close=lambda: None,
        )

    monkeypatch.setattr(model_api, "httpx", SimpleNamespace(Client=fake_httpx_client))
    monkeypatch.setattr(model_api, "OpenAI", fake_openai_ctor)

    res = model_api.generate_content(prompt="测试代理隔离", timeout=20)
    assert res["status"] == "✅ 成功"
    assert res["metrics"]["model_used"] == "gemini-3.8-flash"
    assert len(created_httpx) == 1
    assert received_http_clients == [created_httpx[0]]
    assert len(closed_httpx) == 1

    # 验证代理密码与 Google Key 均被严格脱敏
    redacted = model_api._redact("err socks5://proxyuser:proxypass@43.131.225.155:1080 key=AIzaSyProxyTestSecretKey999")
    assert "proxypass" not in redacted
    assert "proxyuser" not in redacted
    assert "AIzaSyProxyTestSecretKey999" not in redacted


def test_multi_provider_routing_falls_back_across_providers(tmp_path, monkeypatch):
    cfg = tmp_path / "providers_config.json"
    cfg.write_text(
        json.dumps(
            {
                "ai_providers": [
                    {
                        "name": "google_official",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                        "api_keys": ["AIzaSyGoogleKey1"],
                        "proxy_url": "http://43.131.225.155:7890",
                        "models": ["gemini-3.8-flash"],
                    },
                    {
                        "name": "domestic_backup",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
                        "api_keys": ["zhipu-direct-key"],
                        "proxy_url": "",
                        "models": ["glm-4v-flash"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg))

    calls = []
    monkeypatch.setattr(
        model_api,
        "httpx",
        SimpleNamespace(Client=lambda **kw: SimpleNamespace(kw=kw, close=lambda: None)),
    )

    def fake_openai_ctor(*, base_url, api_key, max_retries, timeout, http_client=None):
        def create_call(**kwargs):
            calls.append((base_url, api_key, http_client is not None, kwargs["model"]))
            if kwargs["model"] == "gemini-3.8-flash":
                raise RuntimeError("upstream 503 unavailable")
            return _make_fake_raw_response('{"provider":"backup"}', model_name=kwargs["model"])

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=SimpleNamespace(create=create_call))),
            close=lambda: None,
        )

    monkeypatch.setattr(model_api, "OpenAI", fake_openai_ctor)

    res = model_api.generate_content(prompt="跨厂商兜底测试", max_retries_per_model=1, timeout=20)
    assert res["status"] == "✅ 成功"
    assert res["metrics"]["model_used"] == "glm-4v-flash"
    assert calls == [
        ("https://generativelanguage.googleapis.com/v1beta/openai/", "AIzaSyGoogleKey1", True, "gemini-3.8-flash"),
        ("https://open.bigmodel.cn/api/paas/v4/", "zhipu-direct-key", False, "glm-4v-flash"),
    ]


def test_runner_check_ai_flag_succeeds_and_redacts_secrets_on_failure(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "runner_ai.json"
    cfg.write_text(
        json.dumps(
            {
                "myself_pdd_client_id": "cid",
                "myself_pdd_client_secret": "csecret",
                "myself_pdd_pid": "cpid",
                "local_mongo_host": "localhost",
                "local_mongo_port": 27017,
                "local_mongo_user": "",
                "local_mongo_password": "",
                "local_mongo_db_name": "crawler_data",
                "web_auth_secret": "runner-test-auth-secret-over-32-characters-long",
                "local_api_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "local_api_key_list": ["AIzaSySecretCanaryKey12345"],
                "ai_models": ["gemini-3.8-flash"],
            }
        ),
        encoding="utf-8",
    )
    from infrastructure.settings import get_settings

    get_settings.cache_clear()
    with monkeypatch.context() as mp:
        mp.setattr(
            model_api,
            "generate_content",
            lambda **kw: {
                "status": "✅ 成功",
                "content": '{"ok": true}',
                "metrics": {"model_used": "gemini-3.8-flash", "key_slot": "Key#1/2", "total_time_seconds": 0.42},
            },
        )
        assert runner.main(["--config", str(cfg), "--check-ai"]) == 0
    out = capsys.readouterr()
    assert "AI 连通性自检成功" in out.out
    assert "gemini-3.8-flash" in out.out
    assert "AIzaSySecretCanaryKey12345" not in out.out + out.err

    monkeypatch.setattr(model_api, "_RUNTIME_OS_NAME", "nt")
    monkeypatch.setattr(
        model_api,
        "OpenAI",
        lambda **_kw: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    with_raw_response=SimpleNamespace(
                        create=lambda **_ckw: (_ for _ in ()).throw(RuntimeError("401 invalid key AIzaSySecretCanaryKey12345"))
                    )
                )
            ),
            close=lambda: None,
        ),
    )
    assert runner.main(["--config", str(cfg), "--check-ai"]) == 1
    out_fail = capsys.readouterr()
    assert "AI 连通性自检失败" in out_fail.err
    assert "AIzaSySecretCanaryKey12345" not in out_fail.out + out_fail.err


def test_single_shared_config_auto_switches_between_windows_and_linux(tmp_path, monkeypatch):
    """同一份 config.local.json：在 Windows 下自动走本地 8317 与预置模型组，在 Linux 下自动启用 cloud_ai。"""
    shared_cfg = tmp_path / "shared_config.local.json"
    shared_cfg.write_text(
        json.dumps(
            {
                "local_api_url": "http://127.0.0.1:8317/v1",
                "local_api_key": "sk-windows-local-gateway-key",
                "cloud_ai": {
                    "api_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                    "api_keys": ["AIzaSyCloudKeyOne", "AIzaSyCloudKeyTwo"],
                    "proxy_url": "http://43.131.225.155:7890",
                    "models": ["gemini-3.8-flash", "gemini-3.6-flash"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(shared_cfg))
    monkeypatch.delenv("AI_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(
        model_api,
        "MEDIUM_MODEL_LIST",
        [{"model_name": "gemini-web-local-only-alias", "权重": 1}],
    )

    recorded = []
    monkeypatch.setattr(
        model_api,
        "httpx",
        SimpleNamespace(Client=lambda **kw: SimpleNamespace(proxy_kw=kw, close=lambda: None)),
    )

    def fake_openai_ctor(*, base_url, api_key, max_retries, timeout, http_client=None):
        proxy_used = getattr(http_client, "proxy_kw", {}).get("proxy") if http_client else None

        def create_call(**kwargs):
            recorded.append((base_url, api_key, proxy_used, kwargs["model"]))
            return _make_fake_raw_response('{"ok": true}', model_name=kwargs["model"])

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=SimpleNamespace(create=create_call))),
            close=lambda: None,
        )

    monkeypatch.setattr(model_api, "OpenAI", fake_openai_ctor)

    # 1. 在 Windows 环境：自动忽略 cloud_ai，使用本地 8317 网关与预置 MEDIUM_MODEL_LIST 模型组，不挂代理
    monkeypatch.setattr(model_api, "_RUNTIME_OS_NAME", "nt")
    win_res = model_api.generate_content(prompt="本地识图", preset_model_group="medium", max_retries_per_model=1)
    assert win_res["status"] == "✅ 成功"
    assert win_res["metrics"]["runtime_mode"] == "windows_local"
    assert recorded == [("http://127.0.0.1:8317/v1", "sk-windows-local-gateway-key", None, "gemini-web-local-only-alias")]

    # 2. 在 Windows 环境显式加 force_cloud=True（对应 run.py --check-ai --cloud）：可在上传前直接实测 cloud_ai
    recorded.clear()
    precheck_res = model_api.generate_content(prompt="本地预检云端配置", force_cloud=True, max_retries_per_model=1)
    assert precheck_res["status"] == "✅ 成功"
    assert precheck_res["metrics"]["runtime_mode"] == "linux_cloud"
    assert recorded == [
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "AIzaSyCloudKeyOne",
            "http://43.131.225.155:7890",
            "gemini-3.8-flash",
        )
    ]

    # 3. 同一份配置文件上传到 Linux 云服务器（posix）：无需任何参数自动切换至 cloud_ai 字典并轮询第二个 Key
    recorded.clear()
    monkeypatch.setattr(model_api, "_RUNTIME_OS_NAME", "posix")
    linux_res = model_api.generate_content(prompt="云端识图", preset_model_group="medium", max_retries_per_model=1)
    assert linux_res["status"] == "✅ 成功"
    assert linux_res["metrics"]["runtime_mode"] == "linux_cloud"
    assert linux_res["metrics"]["key_slot"] == "Key#2/2"
    assert recorded == [
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "AIzaSyCloudKeyTwo",
            "http://43.131.225.155:7890",
            "gemini-3.8-flash",
        )
    ]


def test_linux_missing_cloud_ai_fails_fast_with_clear_upload_hint(tmp_path, monkeypatch):
    """Linux 云端若忘记同步上传 config.local.json（缺少 cloud_ai），应秒级快速失败并给出明确提示，不再去连 127.0.0.1:8317。"""
    old_server_cfg = tmp_path / "old_server_config.local.json"
    old_server_cfg.write_text(
        json.dumps(
            {
                "local_api_url": "http://127.0.0.1:8317/v1",
                "local_api_key": "sk-old-local",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(old_server_cfg))
    monkeypatch.delenv("AI_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(model_api, "_RUNTIME_OS_NAME", "posix")

    res = model_api.generate_content(prompt='请仅回复 JSON: {"ok": true}', max_retries_per_model=2)
    assert res["status"] == "❌ 失败"
    assert res["metrics"]["attempts"] == 0
    assert res["metrics"]["runtime_mode"] == "linux_cloud"
    assert "cloud_ai" in res["content"]
    assert "config.local.json" in res["content"]


def test_openai_v3_default_httpx_client_works_without_legacy_httpx(monkeypatch):
    """兼容 openai>=3.x（仅安装 httpx2 / DefaultHttpxClient、未安装旧版 httpx）时的海外代理客户端构造。"""
    captured = {}

    def fake_default_httpx_client(**kwargs):
        captured["http_kwargs"] = kwargs
        return SimpleNamespace(close=lambda: None)

    def fake_openai_ctor(**kwargs):
        captured["openai_kwargs"] = kwargs
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(model_api, "httpx", None)
    monkeypatch.setattr(model_api, "_INITIAL_HTTPX", None)
    monkeypatch.setattr(model_api, "DefaultHttpxClient", fake_default_httpx_client)
    monkeypatch.setattr(model_api, "OpenAI", fake_openai_ctor)
    monkeypatch.setattr(model_api, "_INITIAL_OPENAI", fake_openai_ctor)

    oa_cli, http_cli = model_api._create_openai_client(
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "AIzaSyTestKey",
        "https://u:p@proxy.example.com:443",
        20,
    )
    assert oa_cli is not None and http_cli is not None
    assert captured["http_kwargs"] == {
        "proxy": "https://u:p@proxy.example.com:443",
        "timeout": 20,
        "trust_env": False,
    }
    assert captured["openai_kwargs"]["http_client"] is http_cli


def test_unified_llm_json_extraction_strips_think_blocks():
    from infrastructure.ai_api.model_api import extract_llm_json
    from services.group_help_service import extract_json_object
    from mobile_web.scripts.goods_info_format import string_to_object

    raw_dict_output = (
        '<think>先分析一下：如果返回 {"is_valid": false, "fake": 1} 会怎样？不行，再想想。</think>\n'
        '```json\n{"is_valid": true, "real_answer": 42}\n```'
    )
    assert extract_llm_json(raw_dict_output) == {"is_valid": True, "real_answer": 42}
    assert extract_json_object(raw_dict_output) == {"is_valid": True, "real_answer": 42}
    assert string_to_object(raw_dict_output) == {"is_valid": True, "real_answer": 42}

    raw_list_output = (
        '<thought>草稿：[{"sku_id": "wrong"}]</thought>\n'
        '[{"sku_id": "100_1", "price": 19.9}]'
    )
    assert extract_llm_json(raw_list_output) == [{"sku_id": "100_1", "price": 19.9}]
    assert string_to_object(raw_list_output) == [{"sku_id": "100_1", "price": 19.9}]


def test_select_models_excludes_zero_weight_models_from_primary_and_fallbacks():
    zero_low = {m["model_name"] for m in model_api.LOW_MODEL_LIST if m.get("权重", 0) <= 0}
    zero_med = {m["model_name"] for m in model_api.MEDIUM_MODEL_LIST if m.get("权重", 0) <= 0}
    assert "gemini-aistudio-api-key-3.5-flash" in zero_low

    for _ in range(100):
        chosen_low = model_api._select_models(None, None, "low")
        assert chosen_low and zero_low.isdisjoint(chosen_low)
        chosen_med = model_api._select_models(None, None, "medium")
        assert chosen_med and zero_med.isdisjoint(chosen_med)


def test_antigravity_client_defaults_to_3_concurrency_per_account_6_across_2(tmp_path, monkeypatch):
    import os
    from infrastructure.ai_api import antigravity_client as ag

    monkeypatch.setattr(ag, "GLOBAL_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(
        ag,
        "sync_all_known_accounts_to_sandboxes",
        lambda overwrite_from_desktop=False: {
            "guardianriedel689@gmail.com": tmp_path / "sb1",
            "richard.nowak@usa.com": tmp_path / "sb2",
        },
    )

    assert ag.DEFAULT_MAX_CONCURRENCY_PER_ACCOUNT == 3
    pool = ag.AntigravityPoolClient(
        account_pool=["guardianriedel689@gmail.com", "richard.nowak@usa.com"]
    )
    assert pool.max_concurrency_per_account == 3
    workers = pool.get_eligible_workers()
    assert len(workers) == 2
    claimed = []
    try:
        for w in workers:
            assert w._max_concurrency == 3
            for _ in range(3):
                fd = w.try_claim_idle_slot()
                assert fd is not None
                claimed.append((w, fd))
            assert w.try_claim_idle_slot() is None
        assert len(claimed) == 6
    finally:
        for w, fd in claimed:
            w._unlock_fd(fd)
            os.close(fd)
            w._concurrency_sem.release()


def test_windows_safe_timed_rotating_file_handler_and_per_daemon_log_isolation(tmp_path):
    import logging
    import os
    import time
    from unittest.mock import patch

    with patch("sys.argv", ["scripts/run_deal_pool_scheduler.py"]):
        assert model_api._resolve_process_log_stem("model_api") == "model_api_deal_pool"
    with patch("sys.argv", ["scripts/run_xianyu_deal_discovery_scheduler.py"]):
        assert model_api._resolve_process_log_stem("model_api") == "model_api_xianyu"
    with patch("sys.argv", ["run.py"]):
        assert model_api._resolve_process_log_stem("model_api") == "model_api"

    log_path = os.path.join(str(tmp_path), "model_api_deal_pool.log")
    handler = model_api._WindowsSafeTimedRotatingFileHandler(
        log_path, when="midnight", backupCount=2, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, "hello log line", (), None)
    handler.emit(rec)
    handler.rolloverAt = int(time.time()) - 10
    handler.doRollover()
    rotated_files = [f for f in os.listdir(tmp_path) if f.startswith("model_api_deal_pool.log.")]
    assert len(rotated_files) == 1

    handler.emit(rec)
    handler.rolloverAt = int(time.time()) - 10
    with patch.object(handler, "rotate", side_effect=PermissionError(32, "被占用")):
        handler.doRollover()
    assert handler.rolloverAt > int(time.time())
    handler.close()


def test_save_json_concurrent_unique_tmp_and_permission_retry(tmp_path, monkeypatch):
    import concurrent.futures
    import os
    from unittest.mock import patch

    monkeypatch.setattr(model_api.time, "sleep", _REAL_TIME_SLEEP)
    target = os.path.join(str(tmp_path), "api_keys_state.json")
    real_replace = os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    with patch.object(model_api.os, "replace", side_effect=_flaky_replace):
        model_api.save_json(target, {"status": "ok", "count": 3})

    assert calls["n"] == 3
    with open(target, "r", encoding="utf-8") as f:
        assert json.load(f) == {"status": "ok", "count": 3}
    assert [x for x in os.listdir(tmp_path) if x.endswith(".tmp")] == []

    def _worker(tid: int):
        for seq in range(15):
            model_api.save_json(target, {"tid": tid, "seq": seq})

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for fut in [pool.submit(_worker, i) for i in range(6)]:
            fut.result()

    with open(target, "r", encoding="utf-8") as f:
        final_data = json.load(f)
    assert "tid" in final_data and "seq" in final_data
    assert [x for x in os.listdir(tmp_path) if x.endswith(".tmp")] == []
