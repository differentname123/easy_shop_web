# -*- coding: utf-8 -*-
"""AI 能力封装模块：统一暴露 model_api 与本地 Antigravity 客户端接口。"""

from .antigravity_client import (
    MODEL_ALIASES as ANTIGRAVITY_MODEL_ALIASES,
    SUB_ACCOUNT_EMAIL,
    AntigravityClient,
    archive_current_desktop_account,
    ask,
    ask_antigravity,
    ask_antigravity_json,
    ask_json,
    get_client,
    set_sub_account,
)
from .model_api import generate_content, probe_models

__all__ = [
    "ANTIGRAVITY_MODEL_ALIASES",
    "SUB_ACCOUNT_EMAIL",
    "AntigravityClient",
    "archive_current_desktop_account",
    "ask",
    "ask_antigravity",
    "ask_antigravity_json",
    "ask_json",
    "generate_content",
    "get_client",
    "probe_models",
    "set_sub_account",
]
