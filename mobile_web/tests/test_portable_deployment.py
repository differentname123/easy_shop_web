# -*- coding: utf-8 -*-
"""Portable production configuration, startup and UTC regressions; never use live services."""
import dataclasses
import datetime
import json
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from infrastructure import settings as settings_module
from infrastructure.settings import SettingsError, load_settings
from tests.standalone_smoke import (
    SMOKE_SECRET, copy_production_tree, isolated_environment, source_boundary, write_fake_config,
)


APP_DIR = Path(__file__).resolve().parent.parent
PRIVATE = {
    "myself_pdd_client_id": "json-client", "myself_pdd_client_secret": "json-client-secret",
    "myself_pdd_pid": "json-pid", "local_mongo_host": "json-mongo.invalid",
    "local_mongo_port": 27018, "local_mongo_user": "json-user", "local_mongo_password": "json-password",
    "local_mongo_db_name": "json_database", "mongo_auth_source": "json_auth",
    "web_auth_secret": "json-private-auth-secret-at-least-32-characters",
    "web_host": "127.0.0.2", "web_port": 8123,
}


@pytest.fixture
def private_config(tmp_path):
    path = tmp_path / "private settings 中文.json"
    path.write_text(json.dumps(PRIVATE), encoding="utf-8")
    return path


def test_legacy_json_configuration_and_frozen_settings(private_config):
    settings = load_settings(private_config, environ={})
    assert settings.pdd_client_id == PRIVATE["myself_pdd_client_id"]
    assert settings.pdd_client_secret == PRIVATE["myself_pdd_client_secret"]
    assert settings.pdd_pid == PRIVATE["myself_pdd_pid"]
    assert settings.mongo_host == PRIVATE["local_mongo_host"]
    assert settings.mongo_port == 27018
    assert settings.mongo_user == "json-user"
    assert settings.mongo_password == "json-password"
    assert settings.mongo_db_name == "json_database"
    assert settings.mongo_auth_source == "json_auth"
    assert settings.web_host == "127.0.0.2"
    assert settings.web_port == 8123
    assert settings.auth_secret == PRIVATE["web_auth_secret"]
    assert settings.log_dir == settings_module.APP_DIR / "runtime" / "logs"
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.web_port = 8001


def test_settings_repr_hides_private_secrets(private_config):
    settings = load_settings(private_config, environ={})
    rendered = repr(settings)
    for field in ("myself_pdd_client_secret", "local_mongo_password", "web_auth_secret"):
        assert PRIVATE[field] not in rendered
    assert "json-mongo.invalid" in rendered


def test_json_pure_configuration(private_config):
    settings = load_settings(private_config)
    assert settings.pdd_client_id == PRIVATE["myself_pdd_client_id"]
    assert settings.pdd_client_secret == PRIVATE["myself_pdd_client_secret"]
    assert settings.pdd_pid == PRIVATE["myself_pdd_pid"]
    assert settings.mongo_host == PRIVATE["local_mongo_host"]
    assert settings.mongo_port == PRIVATE["local_mongo_port"]
    assert settings.mongo_user == PRIVATE["local_mongo_user"]
    assert settings.mongo_password == PRIVATE["local_mongo_password"]
    assert settings.mongo_db_name == PRIVATE["local_mongo_db_name"]
    assert settings.mongo_auth_source == PRIVATE["mongo_auth_source"]
    assert settings.auth_secret == PRIVATE["web_auth_secret"]
    assert settings.web_host == PRIVATE["web_host"]
    assert settings.web_port == PRIVATE["web_port"]


@pytest.mark.parametrize("field,key", [
    ("myself_pdd_client_id", "myself_pdd_client_id"),
    ("myself_pdd_client_secret", "myself_pdd_client_secret"),
    ("myself_pdd_pid", "myself_pdd_pid"),
    ("web_auth_secret", "web_auth_secret"),
])
def test_explicit_empty_credentials_in_json_are_rejected(tmp_path, field, key):
    path = tmp_path / "empty_cred.json"
    data = dict(PRIVATE)
    data[key] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        load_settings(path)
    assert field in str(error.value)
    assert PRIVATE["myself_pdd_client_secret"] not in str(error.value)
    assert PRIVATE["web_auth_secret"] not in str(error.value)


@pytest.mark.parametrize("secret", ["", "short", "easy_shop_secure_auth_secret_key_2026_v2"])
def test_missing_weak_and_public_old_auth_secrets_are_rejected(tmp_path, secret):
    path = tmp_path / "weak_auth.json"
    data = dict(PRIVATE)
    data["web_auth_secret"] = secret
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SettingsError, match="web_auth_secret"):
        load_settings(path)


@pytest.mark.parametrize("field,value", [
    ("web_port", ""), ("web_port", "0"), ("web_port", "65536"), ("web_port", "1.5"),
    ("local_mongo_port", ""), ("local_mongo_port", "-1"), ("local_mongo_port", "65536"), ("local_mongo_port", "not-a-port"),
    ("local_mongo_host", ""), ("local_mongo_db_name", ""), ("mongo_auth_source", ""),
    ("web_warmup_enabled", ""), ("web_warmup_enabled", "perhaps"), ("pdd_trust_env", "perhaps"),
    ("web_detail_prefetch_enabled", "perhaps"),
    ("web_detail_prefetch_interval_seconds", 59), ("web_detail_prefetch_interval_seconds", 301),
    ("web_detail_prefetch_interval_seconds", True), ("web_detail_prefetch_interval_seconds", 60.5),
    ("web_detail_prefetch_interval_seconds", "1.5"), ("web_detail_prefetch_interval_seconds", ""),
    ("pdd_dns_mode", "global"), ("web_log_level", "secret-log-canary"),
])
def test_require_credentials_false_still_validates_noncredential_fields(tmp_path, field, value):
    path = tmp_path / "empty private.json"
    path.write_text(json.dumps({field: value}), encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        load_settings(path, require_credentials=False)
    assert field in str(error.value)
    if value == "secret-log-canary":
        assert value not in str(error.value)


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("on", True),
    ("yes", True), ("false", False), ("0", False), ("off", False), ("no", False)])
def test_boolean_json_values(tmp_path, value, expected):
    path = tmp_path / "bool.json"
    data = dict(PRIVATE)
    data["web_warmup_enabled"] = value
    data["pdd_trust_env"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    settings = load_settings(path)
    assert settings.warmup_enabled is expected
    assert settings.pdd_trust_env is expected


@pytest.mark.parametrize("warmup,explicit,expected", [
    (True, None, True), (False, None, False), (True, True, True),
    (True, False, False), (False, True, True), (False, False, False),
])
def test_detail_prefetch_defaults_follow_warmup_and_json_explicit_value_wins(tmp_path, warmup, explicit, expected):
    path = tmp_path / "prefetch.json"
    data = dict(PRIVATE, web_warmup_enabled=warmup)
    if explicit is not None:
        data["web_detail_prefetch_enabled"] = explicit
    path.write_text(json.dumps(data), encoding="utf-8")
    settings = load_settings(path, environ={"WEB_DETAIL_PREFETCH_ENABLED": "opposite-value-ignored"})
    assert settings.warmup_enabled is warmup
    assert settings.detail_prefetch_enabled is expected
    assert settings.detail_prefetch_interval_seconds == 300


@pytest.mark.parametrize("value,expected", [(60, 60), (300, 300), ("120", 120)])
def test_detail_prefetch_interval_is_a_bounded_integer(tmp_path, value, expected):
    path = tmp_path / "interval.json"
    path.write_text(json.dumps(dict(PRIVATE, web_detail_prefetch_interval_seconds=value)), encoding="utf-8")
    assert load_settings(path).detail_prefetch_interval_seconds == expected


def test_public_template_enables_prefetch_and_standalone_fake_explicitly_disables_it(tmp_path):
    template = json.loads((APP_DIR / "config.example.json").read_text(encoding="utf-8"))
    assert template["web_detail_prefetch_enabled"] is True
    assert template["web_detail_prefetch_interval_seconds"] == 300
    path = tmp_path / "standalone-fake.json"
    write_fake_config(path)
    fake = json.loads(path.read_text(encoding="utf-8"))
    assert fake["web_detail_prefetch_enabled"] is False
    assert load_settings(path).detail_prefetch_enabled is False


@pytest.mark.parametrize("value", ["", "-"])
def test_file_logging_can_be_disabled(tmp_path, value):
    path = tmp_path / "logging.json"
    data = dict(PRIVATE)
    data["web_log_dir"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_settings(path).log_dir is None


def test_config_and_log_relative_paths_are_anchored_to_application_not_cwd(tmp_path, monkeypatch):
    application = tmp_path / "deployment 中文"
    application.mkdir()
    private = application / "relative private.json"
    data = dict(PRIVATE)
    data["web_log_dir"] = "runtime/custom logs"
    private.write_text(json.dumps(data), encoding="utf-8")
    unrelated = tmp_path / "other cwd"
    unrelated.mkdir()
    monkeypatch.setattr(settings_module, "APP_DIR", application)
    monkeypatch.chdir(unrelated)
    settings = load_settings(private)
    assert settings.pdd_client_id == "json-client"
    assert settings.log_dir == application / "runtime" / "custom logs"
    assert not (unrelated / "runtime").exists()


def test_missing_default_file_raises_settings_error(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_CONFIG_FILE", raising=False)
    monkeypatch.setattr(settings_module, "DEFAULT_CONFIG_FILE", tmp_path / "missing-default.json")
    with pytest.raises(SettingsError):
        load_settings()


@pytest.mark.parametrize("text", ['{"password":"malformed-json-secret-canary",', "[]", "null", '"string"'])
def test_invalid_explicit_config_has_safe_value_error(tmp_path, text):
    path = tmp_path / "bad-config.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        load_settings(path, environ={})
    assert isinstance(error.value, ValueError)
    assert "malformed-json-secret-canary" not in str(error.value)


def test_explicit_missing_config_is_not_silently_ignored(tmp_path):
    with pytest.raises(SettingsError):
        load_settings(tmp_path / "does not exist.json", environ={"PDD_CLIENT_ID": "env",
            "PDD_CLIENT_SECRET": "env-secret", "PDD_PID": "env-pid", "WEB_AUTH_SECRET": SMOKE_SECRET})


def test_missing_credentials_can_be_skipped_only_when_requested(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("{}", encoding="utf-8")
    settings = load_settings(path, environ={}, require_credentials=False)
    assert settings.pdd_client_id == settings.pdd_client_secret == settings.pdd_pid == settings.auth_secret == ""
    with pytest.raises(SettingsError):
        load_settings(path, environ={})


def test_get_settings_caches_the_unified_loader(monkeypatch):
    expected = object()
    calls = []
    def loader():
        calls.append(True)
        return expected
    settings_module.get_settings.cache_clear()
    try:
        monkeypatch.setattr(settings_module, "load_settings", loader)
        assert settings_module.get_settings() is expected
        assert settings_module.get_settings() is expected
        assert calls == [True]
    finally:
        settings_module.get_settings.cache_clear()


def test_mongo_factory_uses_unified_settings_and_never_connects(tmp_path, monkeypatch):
    from infrastructure import mongo
    custom_config = tmp_path / "mongo_esc.json"
    data = dict(PRIVATE)
    data.update({"local_mongo_user": "name@realm", "local_mongo_password": "pass:/ word"})
    custom_config.write_text(json.dumps(data), encoding="utf-8")
    settings = load_settings(custom_config, environ={})
    client = MagicMock()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(mongo, "MongoClient", factory)
    monkeypatch.setattr(mongo, "get_settings", lambda: settings)
    db = mongo.gen_db_object()
    uri = factory.call_args.args[0]
    options = factory.call_args.kwargs
    assert uri == "mongodb://name%40realm:pass%3A%2F+word@json-mongo.invalid:27018/json_auth"
    assert options["connect"] is False
    assert options["tz_aware"] is True
    assert options["serverSelectionTimeoutMS"] > 0
    assert options["connectTimeoutMS"] > 0
    assert options["socketTimeoutMS"] > 0
    client.__getitem__.assert_called_once_with("json_database")
    client.admin.command.assert_not_called()
    db.close()
    db.close()
    client.close.assert_called_once()


@pytest.mark.parametrize("detail", [False, True])
def test_new_product_timestamps_are_utc_naive_not_machine_local(detail, monkeypatch):
    from repositories import product_repo
    utc = datetime.datetime(2026, 11, 3, 4, 5, 6, 123456, tzinfo=datetime.timezone.utc)
    calls = []
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            # A local-clock call deliberately differs by eight hours.
            return utc.astimezone(tz) if tz else (utc + datetime.timedelta(hours=8)).replace(tzinfo=None)
    monkeypatch.setattr(product_repo, "datetime", SimpleNamespace(datetime=Clock, timezone=datetime.timezone))
    item = {"platform": "pdd", "goods_id": "123", "goods_sign": "UTC_SIGN", "name": "UTC product", "price": 1}
    if detail:
        item["detail_updated_at"] = Clock(2026, 11, 3, 12, 5, 6, 123456,
            tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    _, update = product_repo.ProductRepository()._build_product_update(item, detail=detail)
    expected = utc.replace(tzinfo=None)
    assert update["$set"]["updated_at"] == expected
    assert update["$setOnInsert"]["created_at"] == expected
    assert update["$set"]["updated_at"].tzinfo is None
    assert update["$setOnInsert"]["created_at"].tzinfo is None
    if detail:
        assert update["$set"]["detail_updated_at"] == expected
        assert update["$set"]["detail_updated_at"].tzinfo is None
    assert calls == [datetime.timezone.utc]


@pytest.fixture
def production_copy(tmp_path):
    # Stay outside the actual checkout, or the application itself for standalone
    # uploads. Deployment paths have spaces/non-ASCII; no test fixture is copied.
    assert not tmp_path.resolve().is_relative_to(source_boundary(APP_DIR))
    deployment = copy_production_tree(APP_DIR, tmp_path / "production only 中文 copy")
    private = tmp_path / "private smoke.json"
    write_fake_config(private)
    assert not (deployment / "tests").exists()
    assert not (deployment / "config.local.json").exists()
    return deployment, private


def run_isolated(production_copy, code):
    deployment, private = production_copy
    cwd = deployment.parent / "unrelated working directory"
    cwd.mkdir(exist_ok=True)
    bootstrap = """
import json, pathlib, socket, sys
sys.path.insert(0, sys.argv[1])
APP = pathlib.Path(sys.argv[1])
blocked = []
def deny(*args, **kwargs):
    blocked.append('socket')
    raise AssertionError('portable deployment test forbids external sockets')
socket.getaddrinfo = socket.create_connection = deny
socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = deny
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", textwrap.dedent(bootstrap) + "\n" +
        textwrap.dedent(code) + "\nassert blocked == [], blocked\n", str(deployment)],
        cwd=cwd, env=isolated_environment(private), capture_output=True, text=True, encoding="utf-8", timeout=90)
    return result


def assert_child_passed(result):
    assert result.returncode == 0, f"child exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_standalone_boundary_without_git_is_only_the_uploaded_application(tmp_path):
    deployment = tmp_path / "standalone upload 中文"
    deployment.mkdir()
    assert source_boundary(deployment) == deployment.resolve()


@pytest.mark.parametrize("marker_kind", ["directory", "file"])
def test_standalone_boundary_finds_actual_checkout_ancestor(tmp_path, marker_kind):
    checkout = tmp_path / "checkout"
    source = checkout / "nested" / "application"
    source.mkdir(parents=True)
    marker = checkout / ".git"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: unused-test-marker", encoding="utf-8")
    assert source_boundary(source) == checkout.resolve()


def test_standalone_smoke_accepts_upload_without_git(production_copy):
    result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(APP_DIR / "tests" / "standalone_smoke.py"),
        "--app-dir", str(production_copy[0])], cwd=production_copy[0].parent,
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert_child_passed(result)
    assert "STANDALONE_SMOKE_OK" in result.stdout


def test_standalone_runtime_smoke_in_fresh_process():
    # This script creates its own production-only copy and a second isolated
    # child, without importing conftest, offline_support, preview_app or pytest.
    result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(APP_DIR / "tests" / "standalone_smoke.py")],
        cwd=APP_DIR.parent, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert_child_passed(result)
    assert "STANDALONE_SMOKE_OK" in result.stdout
    assert "Ran 4 tests" in result.stderr


def test_absolute_runner_check_from_unrelated_cwd_is_offline(production_copy):
    result = run_isolated(production_copy, """
import runpy
sys.argv = [str(APP / 'run.py'), '--check']
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as exc:
    assert exc.code in (None, 0), exc.code
else:
    raise AssertionError('real CLI main guard was not executed')
assert 'main' not in sys.modules
assert 'infrastructure.mongo' not in sys.modules
""")
    assert_child_passed(result)
    assert SMOKE_SECRET not in result.stdout + result.stderr
    assert not list(production_copy[0].rglob("*.log"))


@pytest.mark.parametrize("arguments,reload", [
    ([], False),
    (["--reload"], True),
])
def test_runner_uvicorn_options_without_starting_a_server(production_copy, arguments, reload):
    code = f"""
from unittest.mock import patch
import run
calls = []
with patch('uvicorn.run', side_effect=lambda *args, **kwargs: calls.append((args, kwargs))):
    assert run.main({arguments!r}) == 0
assert len(calls) == 1
args, options = calls[0]
assert args == ('main:app',), args
assert options['reload'] is {reload!r}
assert options['reload_dirs'] == ([str(APP)] if {reload!r} else None)
assert options['workers'] == 1
"""
    assert_child_passed(run_isolated(production_copy, code))


def test_direct_main_entry_applies_flags_before_loading_configuration(production_copy):
    result = run_isolated(production_copy, """
import runpy
from unittest.mock import patch
sys.argv = [str(APP / 'main.py'), '--reload']
calls = []
with patch('uvicorn.run', side_effect=lambda *args, **kwargs: calls.append((args, kwargs))):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except SystemExit as exc:
        assert exc.code == 0, exc.code
    else:
        raise AssertionError('direct main entry did not delegate to runner')
assert len(calls) == 1
args, options = calls[0]
assert args == ('main:app',)
assert options['reload'] is True
assert 'config' not in sys.modules
assert 'main' not in sys.modules
""")
    assert_child_passed(result)


def test_runner_bad_config_error_is_safe_and_does_not_import_app(production_copy):
    result = run_isolated(production_copy, """
import json, os, run
from pathlib import Path
cfg = Path(os.environ['WEB_CONFIG_FILE'])
data = json.loads(cfg.read_text(encoding='utf-8'))
data['local_mongo_port'] = 'private-port-canary'
cfg.write_text(json.dumps(data), encoding='utf-8')
assert run.main(['--check']) == 2
assert 'main' not in sys.modules
""")
    assert_child_passed(result)
    assert "private-port-canary" not in result.stdout + result.stderr
    assert SMOKE_SECRET not in result.stdout + result.stderr


def test_runner_check_mongo_is_explicit_read_only_ping(production_copy):
    result = run_isolated(production_copy, """
from unittest.mock import MagicMock, patch
import run
from infrastructure import mongo
client = MagicMock()
with patch.object(mongo, 'MongoClient', return_value=client):
    assert run.main(['--check-mongo']) == 0
client.admin.command.assert_called_once_with('ping')
client.close.assert_called_once()
assert 'main' not in sys.modules
assert 'repositories.base_repo' not in sys.modules
""")
    assert_child_passed(result)


def test_runner_check_mongo_failure_is_safe_and_closes_client(production_copy):
    result = run_isolated(production_copy, """
from unittest.mock import MagicMock, patch
import run
from infrastructure import mongo
client = MagicMock()
client.admin.command.side_effect = RuntimeError('mongodb://private-user:private-password@host')
with patch.object(mongo, 'MongoClient', return_value=client):
    assert run.main(['--check-mongo']) == 1
client.admin.command.assert_called_once_with('ping')
client.close.assert_called_once()
assert 'main' not in sys.modules
""")
    assert_child_passed(result)
    assert "private-password" not in result.stdout + result.stderr
    assert "private-user" not in result.stdout + result.stderr


def test_initializer_whitelists_source_and_never_overwrites_existing_file(production_copy):
    result = run_isolated(production_copy, """
import runpy
source = APP.parent / 'legacy source.json'
source.write_text(json.dumps({
    'myself_pdd_client_id': 'migrated-client', 'myself_pdd_client_secret': 'migrated-secret',
    'myself_pdd_pid': 'migrated-pid', 'local_mongo_host': 'localhost', 'local_mongo_port': 27017,
    'local_mongo_user': '', 'local_mongo_password': '', 'local_mongo_db_name': 'migrated_db',
    'mongo_auth_source': 'migrated_auth',
    'web_auth_secret': 'preserved-private-auth-secret-over-32-characters',
    'unrelated_api_secret': 'must-not-copy-canary', 'web_port': 9999,
}), encoding='utf-8')
script = APP / 'scripts' / 'init_config.py'
def initialize():
    sys.argv = [str(script), '--source', str(source)]
    try:
        runpy.run_path(str(script), run_name='__main__')
    except SystemExit as exc:
        return exc.code or 0
    return 0
assert initialize() == 0
output = APP / 'config.local.json'
values = json.loads(output.read_text(encoding='utf-8'))
allowed = {'myself_pdd_client_id', 'myself_pdd_client_secret', 'myself_pdd_pid',
    'local_mongo_host', 'local_mongo_port', 'local_mongo_user', 'local_mongo_password',
    'local_mongo_db_name', 'mongo_auth_source', 'web_auth_secret',
    'pdd_enabled', 'taobao_enabled', 'myself_taobao_app_key', 'myself_taobao_app_secret',
    'myself_taobao_pid', 'taobao_adzone_id', 'taobao_session', 'taobao_inviter_code',
    'haodanku_api_key', 'zhetaoke_appkey', 'zhetaoke_sid', 'jd_enabled',
    'myself_jd_union_id', 'myself_jd_position_id', 'myself_jd_app_key', 'myself_jd_app_secret', 'jd_api_base'}
assert set(values) <= allowed, set(values) - allowed
assert values['mongo_auth_source'] == 'migrated_auth'
assert values['myself_pdd_client_id'] == 'migrated-client'
assert values['web_auth_secret'] == 'preserved-private-auth-secret-over-32-characters'
assert 'must-not-copy-canary' not in output.read_text(encoding='utf-8')
before = output.read_bytes()
initialize()
assert output.read_bytes() == before
""")
    assert_child_passed(result)
    assert "must-not-copy-canary" not in result.stdout + result.stderr


@pytest.mark.parametrize("empty_value", [None, ""], ids=["null", "empty-string"])
def test_initializer_normalizes_empty_mongo_fields_only_during_import(production_copy, empty_value):
    code = f"EMPTY = {empty_value!r}\n" + """
import runpy
from infrastructure.settings import load_settings
source = APP.parent / 'fake empty mongo source.json'
source.write_text(json.dumps({
    'myself_pdd_client_id': 'migrated-client', 'myself_pdd_client_secret': 'migrated-secret',
    'myself_pdd_pid': 'migrated-pid', 'local_mongo_host': EMPTY, 'local_mongo_port': EMPTY,
    'local_mongo_db_name': EMPTY, 'mongo_auth_source': EMPTY,
    'web_auth_secret': 'preserved-private-auth-secret-over-32-characters',
}), encoding='utf-8')
output = APP / 'scripts' / 'config.local.json'
script = APP / 'scripts' / 'init_config.py'
sys.argv = [str(script), '--source', str(source), '--output', str(output)]
try:
    runpy.run_path(str(script), run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0, exc.code
else:
    raise AssertionError('initializer main guard was not executed')
values = json.loads(output.read_text(encoding='utf-8'))
assert values['local_mongo_host'] == 'localhost'
assert values['local_mongo_port'] == 27017
assert values['local_mongo_db_name'] == 'crawler_data'
assert 'mongo_auth_source' not in values
assert values['web_auth_secret'] == 'preserved-private-auth-secret-over-32-characters'
settings = load_settings(output, environ={})
assert (settings.mongo_host, settings.mongo_port, settings.mongo_db_name,
        settings.mongo_auth_source) == ('localhost', 27017, 'crawler_data', 'admin')
import run
assert run.main(['--config', str(output), '--check']) == 0
assert 'main' not in sys.modules
assert 'infrastructure.mongo' not in sys.modules
"""
    assert_child_passed(run_isolated(production_copy, code))


def test_initializer_generates_private_secret_once_and_accepts_custom_output(production_copy):
    result = run_isolated(production_copy, """
import os, runpy
os.environ.pop('WEB_AUTH_SECRET', None)
output = APP.parent / 'custom generated 中文.json'
script = APP / 'scripts' / 'init_config.py'
sys.argv = [str(script), '--output', str(output)]
try:
    runpy.run_path(str(script), run_name='__main__')
except SystemExit as exc:
    assert exc.code in (None, 0), exc.code
values = json.loads(output.read_text(encoding='utf-8'))
assert len(values['web_auth_secret']) >= 32
assert values['web_auth_secret'] != 'easy_shop_secure_auth_secret_key_2026_v2'
assert not (APP / 'config.local.json').exists()
""")
    assert_child_passed(result)


def test_initializer_preserves_safe_source_auth_secret(production_copy):
    result = run_isolated(production_copy, """
import json, runpy
source = APP.parent / 'source auth.json'
source.write_text(json.dumps({'web_auth_secret': 'source-private-auth-secret-at-least-32-characters'}), encoding='utf-8')
output = APP.parent / 'selected auth.json'
script = APP / 'scripts' / 'init_config.py'
sys.argv = [str(script), '--source', str(source), '--output', str(output)]
try:
    runpy.run_path(str(script), run_name='__main__')
except SystemExit as exc:
    assert exc.code in (None, 0), exc.code
values = json.loads(output.read_text(encoding='utf-8'))
assert values['web_auth_secret'] == 'source-private-auth-secret-at-least-32-characters'
""")
    assert_child_passed(result)
    assert "source-private-auth-secret-at-least-32-characters" not in result.stdout + result.stderr
