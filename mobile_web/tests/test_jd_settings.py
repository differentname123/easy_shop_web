"""JD public configuration is optional and capability credentials are independent."""
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.settings import SettingsError, load_settings
from tests.offline_support import _CONFIG


APP_DIR = Path(__file__).resolve().parent.parent


def private_config(tmp_path, **values):
    path = tmp_path / "private-jd.json"
    path.write_text(json.dumps(dict(_CONFIG, **values)), encoding="utf-8")
    return path


def jd_config(tmp_path, **values):
    return private_config(tmp_path, **dict({
        "pdd_enabled": False, "taobao_enabled": False, "jd_enabled": True,
        "myself_pdd_client_id": "", "myself_pdd_client_secret": "", "myself_pdd_pid": "",
        "myself_jd_union_id": "123456789", "zhetaoke_appkey": "offline-ztk-key",
    }, **values))


def test_jd_defaults_off_without_affecting_existing_pdd_config(tmp_path):
    settings = load_settings(private_config(tmp_path))
    assert settings.pdd_enabled and not settings.jd_enabled
    assert settings.jd_api_base == "http://api.zhetaoke.com:20000/api"
    assert not any((settings.jd_union_id, settings.jd_position_id, settings.jd_app_key, settings.jd_app_secret))


def test_jd_only_catalog_needs_neither_default_position_nor_order_app(tmp_path):
    settings = load_settings(jd_config(tmp_path))
    assert settings.jd_enabled and not settings.pdd_enabled and not settings.taobao_enabled
    assert settings.jd_union_id == "123456789" and settings.ztk_appkey == "offline-ztk-key"
    assert not settings.jd_position_id and not settings.jd_app_key and not settings.jd_app_secret


@pytest.mark.parametrize("key", ["zhetaoke_appkey", "myself_jd_union_id", "jd_api_base"])
def test_enabled_jd_requires_only_catalog_fields_and_reports_names(tmp_path, key):
    with pytest.raises(SettingsError) as error:
        load_settings(jd_config(tmp_path, **{key: "", "myself_jd_app_secret": "secret-canary-never-display"}))
    assert key in str(error.value)
    assert "secret-canary-never-display" not in str(error.value)


def test_disabled_jd_does_not_require_any_jd_credentials(tmp_path):
    settings = load_settings(private_config(tmp_path, jd_enabled=False))
    assert not settings.jd_enabled


def test_no_platform_enabled_still_fails(tmp_path):
    with pytest.raises(SettingsError, match="至少需要启用一个联盟平台"):
        load_settings(private_config(tmp_path, pdd_enabled=False, taobao_enabled=False, jd_enabled=False))


@pytest.mark.parametrize("key", ["myself_jd_union_id", "myself_jd_position_id"])
@pytest.mark.parametrize("value", [False, True, -1, "0", "-1", "1.5", "１", "abc"])
def test_jd_identity_requires_ascii_positive_numeric_string(tmp_path, key, value):
    with pytest.raises(SettingsError) as error:
        load_settings(jd_config(tmp_path, **{key: value}))
    assert key in str(error.value)


def test_jd_numbers_remain_strings_and_secrets_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("JD_UNION_ID", "cannot-override-private-json")
    settings = load_settings(jd_config(tmp_path, myself_jd_union_id="9007199254740993",
        myself_jd_position_id=1234567, myself_jd_app_key="app-key",
        myself_jd_app_secret="jd-private-secret-canary", zhetaoke_appkey="ztk-private-canary"))
    assert settings.jd_union_id == "9007199254740993"
    assert settings.jd_position_id == "1234567"
    assert settings.jd_app_key == "app-key"
    assert "jd-private-secret-canary" not in repr(settings)
    assert "ztk-private-canary" not in repr(settings)


def test_private_gateway_selection_is_central_not_an_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("JD_API_BASE", "https://attacker.invalid")
    settings = load_settings(jd_config(tmp_path, jd_api_base=" http://api.zhetaoke.com:20000/api/ "))
    assert settings.jd_api_base == "http://api.zhetaoke.com:20000/api/"


def test_public_template_and_initializer_share_jd_names_and_safe_defaults():
    FIELDS = runpy.run_path(str(APP_DIR / "scripts" / "init_config.py"))["FIELDS"]
    template = json.loads((APP_DIR / "config.example.json").read_text(encoding="utf-8"))
    for key in ("jd_enabled", "myself_jd_union_id", "myself_jd_position_id", "myself_jd_app_key",
                "myself_jd_app_secret", "jd_api_base", "zhetaoke_appkey"):
        assert template[key] == FIELDS[key]
    assert template["jd_enabled"] is False
    assert template["myself_jd_app_secret"] == template["zhetaoke_appkey"] == ""


def test_initializer_migrates_jd_whitelist_without_changing_auth_or_overwriting(tmp_path, capsys):
    main = runpy.run_path(str(APP_DIR / "scripts" / "init_config.py"))["main"]
    source = jd_config(tmp_path, myself_jd_position_id="12345", myself_jd_app_key="fake-key",
                       myself_jd_app_secret="init-secret-canary", unrelated_secret="must-not-copy")
    target = tmp_path / "generated.json"
    assert main(["--source", str(source), "--output", str(target)]) == 0
    values = json.loads(target.read_text(encoding="utf-8"))
    assert values["myself_jd_app_secret"] == "init-secret-canary"
    assert values["web_auth_secret"] == _CONFIG["web_auth_secret"]
    assert "unrelated_secret" not in values
    before = target.read_bytes()
    assert main(["--output", str(target)]) == 0
    assert target.read_bytes() == before
    assert "init-secret-canary" not in capsys.readouterr().out
    assert load_settings(target).jd_enabled


def test_public_authorization_required_comes_from_adapter_declaration(monkeypatch):
    import adapters
    fixtures = {
        "jd": SimpleNamespace(platform_name="京东", active=True, requires_authorized_promotion=False,
                              capabilities={"promotion": True, "authorization": False}),
        "taobao": SimpleNamespace(platform_name="淘宝", active=True, requires_authorized_promotion=True,
                                  capabilities={"promotion": True, "authorization": False}),
    }
    monkeypatch.setattr(adapters, "_ADAPTER_TYPES", {})
    monkeypatch.setattr(adapters, "_ADAPTER_REGISTRY", fixtures)
    descriptors = {item["code"]: item for item in adapters.get_all_platforms()}
    assert descriptors["jd"]["authorization_required"] is False
    assert descriptors["taobao"]["authorization_required"] is True
    assert not descriptors["jd"]["capabilities"]["authorization"]
    assert not descriptors["taobao"]["capabilities"]["authorization"]


def test_zhetaoke_appkey_list_merges_deduplicates_and_redacts_all_keys(tmp_path):
    settings = load_settings(jd_config(
        tmp_path,
        zhetaoke_appkey="key-alpha, key-beta",
        zhetaoke_appkey_list=["key-beta", "key-gamma"],
    ))
    assert settings.ztk_appkey == "key-alpha"
    assert settings.ztk_appkey_list == ("key-alpha", "key-beta", "key-gamma")
    assert "key-alpha" not in repr(settings) and "key-gamma" not in repr(settings)

