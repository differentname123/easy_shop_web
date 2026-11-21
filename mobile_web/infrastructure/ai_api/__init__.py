# -*- coding: utf-8 -*-
"""AI 能力封装模块：默认轻量加载 model_api，仅按需延迟导入本地 Antigravity 客户端。"""
from __future__ import annotations

from typing import Any

from .json_parser import extract_llm_json, parse_llm_json, string_to_object
from .model_api import generate_content, get_combined_model_usage, reset_ai_cooldowns

_LAZY_ANTIGRAVITY_ATTRS = frozenset({
    "ANTIGRAVITY_MODEL_ALIASES",
    "SUB_ACCOUNT_EMAIL",
    "SUB_ACCOUNT_POOL",
    "AntigravityClient",
    "AntigravityPoolClient",
    "archive_current_desktop_account",
    "ask",
    "ask_antigravity",
    "ask_antigravity_json",
    "ask_json",
    "get_client",
    "set_sub_account",
    "set_sub_account_pool",
})


def __getattr__(name: str) -> Any:
    if name in _LAZY_ANTIGRAVITY_ATTRS:
        from . import antigravity_client

        val = getattr(antigravity_client, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | _LAZY_ANTIGRAVITY_ATTRS)


__all__ = [
    "generate_content",
    "parse_llm_json",
    "extract_llm_json",
    "string_to_object",
    "get_combined_model_usage",
    "reset_ai_cooldowns",
]
