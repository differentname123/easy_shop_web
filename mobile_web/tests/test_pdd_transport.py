"""Offline transport checks: no real DNS, HTTP, configuration or file writes."""

import builtins
import io
import logging
from pathlib import Path
import socket
import sys
import types
from unittest.mock import Mock

import pytest
import requests

from infrastructure import pdd_client


@pytest.fixture
def native_dns(monkeypatch):
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setattr(pdd_client, "_ORIG_GETADDRINFO", resolver)
    monkeypatch.setattr(pdd_client, "_PDD_DNS_CACHE", {})
    monkeypatch.setattr(pdd_client, "_PDD_DNS_ENABLED", False)
    monkeypatch.setattr(pdd_client, "_PDD_DNS_INSTALLED", False)
    monkeypatch.setattr(pdd_client, "_PDD_SESSION", types.SimpleNamespace(trust_env=None, head=Mock()))
    return resolver


@pytest.mark.parametrize("platform,expected_trust", [("win32", False), ("linux", True)])
def test_import_only_builds_session_and_pool_without_global_patch_or_io(monkeypatch, platform, expected_trust):
    code = compile(Path(pdd_client.__file__).read_text(encoding="utf-8"), pdd_client.__file__, "exec")
    fresh_module = types.ModuleType("offline_pdd_import")
    denied = Mock(side_effect=AssertionError("import attempted external IO"))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(requests.sessions.Session, "request", denied)
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(io, "open", denied)
    monkeypatch.setattr(logging, "FileHandler", denied)
    original_import = builtins.__import__
    imports = []

    def checked_import(name, *args, **kwargs):
        imports.append(name)
        assert name.split(".")[0] not in {"common", "app", "config"}
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    exec(code, fresh_module.__dict__)
    assert socket.getaddrinfo is denied
    assert denied.call_count == 0
    assert fresh_module.logger is logging.getLogger("mobile_web")
    assert fresh_module._PDD_SESSION.trust_env is expected_trust
    assert fresh_module._PDD_SESSION.verify is True
    assert fresh_module._PDD_DNS_CACHE == {}
    assert fresh_module._PDD_DNS_INSTALLED is False
    assert fresh_module._PDD_SESSION.adapters["https://"] is fresh_module._PDD_ADAPTER
    assert fresh_module._PDD_SESSION.adapters["http://"] is fresh_module._PDD_ADAPTER
    assert fresh_module._PDD_ADAPTER._pool_connections == 30
    assert fresh_module._PDD_ADAPTER._pool_maxsize == 100
    assert fresh_module._PDD_ADAPTER.max_retries.total == 1
    assert imports
    fresh_module._PDD_SESSION.close()


@pytest.mark.parametrize("platform,enabled,trust", [("win32", True, False), ("linux", False, True)])
def test_auto_uses_platform_dns_and_proxy_defaults(monkeypatch, native_dns, platform, enabled, trust):
    monkeypatch.setattr(sys, "platform", platform)
    pdd_client.configure_transport()
    assert (socket.getaddrinfo is pdd_client._pdd_cached_getaddrinfo) is enabled
    assert pdd_client._PDD_DNS_ENABLED is enabled
    assert pdd_client._PDD_SESSION.trust_env is trust
    native_dns.assert_not_called()
    if enabled:
        result = socket.getaddrinfo("gw-api.pinduoduo.com", 8443)
        assert [entry[4] for entry in result] == [(ip, 8443) for ip in pdd_client._PDD_FALLBACK_IPS]
        native_dns.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_on_off_are_idempotent_and_preserve_original_resolver(monkeypatch, native_dns, platform):
    monkeypatch.setattr(sys, "platform", platform)
    pdd_client.configure_transport("on")
    first = socket.getaddrinfo
    cached_entry = pdd_client._PDD_DNS_CACHE["gw-api.pinduoduo.com"]
    pdd_client.configure_transport("on")
    assert socket.getaddrinfo is first
    assert pdd_client._ORIG_GETADDRINFO is native_dns
    assert pdd_client._PDD_DNS_CACHE["gw-api.pinduoduo.com"] is cached_entry
    socket.getaddrinfo("other.example", 80, socket.AF_INET6, socket.SOCK_DGRAM, 17, 2)
    native_dns.assert_called_once_with("other.example", 80, socket.AF_INET6, socket.SOCK_DGRAM, 17, 2)
    pdd_client.configure_transport("off")
    assert socket.getaddrinfo is native_dns
    pdd_client.configure_transport("off")
    assert socket.getaddrinfo is native_dns
    pdd_client.configure_transport("on")
    assert socket.getaddrinfo is first
    assert pdd_client._ORIG_GETADDRINFO is native_dns


def test_off_does_not_overwrite_later_wrapper_or_create_recursion(native_dns):
    pdd_client.configure_transport("on")
    ours = socket.getaddrinfo
    external = Mock(side_effect=lambda *args: ours(*args))
    socket.getaddrinfo = external
    pdd_client.configure_transport("off")
    assert socket.getaddrinfo is external
    socket.getaddrinfo("gw-api.pinduoduo.com", 443)
    native_dns.assert_called_once_with("gw-api.pinduoduo.com", 443, 0, 0, 0, 0)
    native_dns.reset_mock()
    pdd_client.configure_transport("on")
    pdd_client.configure_transport("on")
    assert socket.getaddrinfo is external
    assert pdd_client._ORIG_GETADDRINFO is native_dns
    assert len(socket.getaddrinfo("gw-api.pinduoduo.com", 443)) == 4
    native_dns.assert_not_called()
    pdd_client.configure_transport("off")
    assert socket.getaddrinfo is external


def test_on_respects_preexisting_wrapper_and_off_restores_it(monkeypatch, native_dns):
    external = Mock(side_effect=native_dns)
    monkeypatch.setattr(socket, "getaddrinfo", external)
    pdd_client.configure_transport("on")
    assert pdd_client._ORIG_GETADDRINFO is external
    socket.getaddrinfo("other.example", 443)
    external.assert_called_once()
    pdd_client.configure_transport("off")
    assert socket.getaddrinfo is external


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("trust_env", [True, False])
def test_explicit_trust_env_overrides_each_platform(monkeypatch, native_dns, platform, trust_env):
    monkeypatch.setattr(sys, "platform", platform)
    pdd_client.configure_transport("off", trust_env=trust_env)
    assert pdd_client._PDD_SESSION.trust_env is trust_env
    pdd_client.configure_transport("off", trust_env=None)
    assert pdd_client._PDD_SESSION.trust_env is (platform != "win32")


def test_expired_cache_refreshes_ipv4_and_preserves_port(monkeypatch, native_dns):
    monkeypatch.setattr(pdd_client.time, "time", lambda: 1000.0)
    pdd_client.configure_transport("on")
    pdd_client._PDD_DNS_CACHE["gw-api.pinduoduo.com"] = (999.0, native_dns.return_value)
    result = socket.getaddrinfo("gw-api.pinduoduo.com", 443, socket.AF_INET6, socket.SOCK_STREAM, 6, 2)
    native_dns.assert_called_once_with("gw-api.pinduoduo.com", 443, socket.AF_INET, socket.SOCK_STREAM, 6, 2)
    assert pdd_client._PDD_DNS_CACHE["gw-api.pinduoduo.com"] == (1600.0, result)
    assert socket.getaddrinfo("gw-api.pinduoduo.com", 80)[0][4] == ("192.0.2.1", 80)
    assert native_dns.call_count == 1


def test_dns_failure_keeps_stale_cache_then_original_fallback_ips(monkeypatch, native_dns):
    monkeypatch.setattr(pdd_client.time, "time", lambda: 1000.0)
    pdd_client.configure_transport("on")
    native_dns.side_effect = socket.gaierror("offline resolver failure")
    pdd_client._PDD_DNS_CACHE["gw-api.pinduoduo.com"] = (999.0, native_dns.return_value)
    assert socket.getaddrinfo("gw-api.pinduoduo.com", 8443)[0][4] == ("192.0.2.1", 8443)
    pdd_client._PDD_DNS_CACHE.clear()
    result = socket.getaddrinfo("gw-api.pinduoduo.com", None)
    assert [entry[4] for entry in result] == [(ip, 443) for ip in pdd_client._PDD_FALLBACK_IPS]
    assert pdd_client._PDD_DNS_CACHE["gw-api.pinduoduo.com"] == (1300.0, result)
    assert pdd_client._PDD_FALLBACK_IPS == ("101.226.73.254", "58.217.134.20", "101.226.73.255", "58.217.134.19")


@pytest.mark.parametrize("fails", [False, True])
def test_warmup_uses_existing_session_with_three_second_head_timeout(native_dns, fails):
    if fails:
        pdd_client._PDD_SESSION.head.side_effect = requests.Timeout("offline timeout")
    assert pdd_client.warmup() is None
    pdd_client._PDD_SESSION.head.assert_called_once_with("https://gw-api.pinduoduo.com", timeout=3.0)
    native_dns.assert_not_called()
    assert socket.getaddrinfo is native_dns


def test_invalid_configuration_does_not_mutate_transport(native_dns):
    with pytest.raises(ValueError, match="dns_mode"):
        pdd_client.configure_transport("invalid")
    with pytest.raises(TypeError, match="trust_env"):
        pdd_client.configure_transport("on", "false")
    assert socket.getaddrinfo is native_dns
    assert pdd_client._PDD_SESSION.trust_env is None
