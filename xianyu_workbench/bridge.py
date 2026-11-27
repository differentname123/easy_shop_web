# -*- coding: utf-8 -*-
"""
唯一外部依赖隔离层：
未来将 `xianyu_workbench/` 迁移到独立仓库时，只需替换本文件中的数据库与 AI 调用实现。
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable, Dict, Optional, Tuple

from pymongo import MongoClient

from .config import REPO_ROOT, WORKBENCH_DIR, WorkbenchConfig, load_config

MOBILE_WEB_DIR = REPO_ROOT / "mobile_web"


def ensure_mobile_web_on_path() -> None:
    """将 mobile_web 与仓库根目录前置到 sys.path，并移除工作台裸目录防止 config/auth 遮蔽。"""
    def _norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p or "."))

    workbench_norm = _norm(str(WORKBENCH_DIR))
    mobile_norm = _norm(str(MOBILE_WEB_DIR))
    repo_norm = _norm(str(REPO_ROOT))
    drop_norms = {workbench_norm, mobile_norm, repo_norm}

    rest = [entry for entry in sys.path if _norm(entry) not in drop_norms]
    front = [str(MOBILE_WEB_DIR), str(REPO_ROOT)] if MOBILE_WEB_DIR.is_dir() else [str(REPO_ROOT)]
    sys.path[:] = front + rest


_ensure_mobile_web_on_path = ensure_mobile_web_on_path


def open_mongo_connection(config: Optional[WorkbenchConfig] = None) -> Tuple[Any, Callable[[], None]]:
    """打开数据库连接并返回 `(db, close_fn)`；若配置了 XY_MONGO_URI 则完全脱离主站配置。"""
    cfg = config or load_config()
    if cfg.mongo_uri:
        client = MongoClient(
            cfg.mongo_uri,
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=15000,
        )
        return client[cfg.mongo_db_name], client.close

    _ensure_mobile_web_on_path()
    from infrastructure.mongo import gen_db_object

    mongo_base = gen_db_object()
    return mongo_base.db, mongo_base.close


def generate_ai_copy(prompt: str, timeout: float = 90.0) -> Dict[str, Any]:
    """统一封装大模型文本生成调用。"""
    _ensure_mobile_web_on_path()
    from infrastructure.ai_api.model_api import generate_content

    return generate_content(
        prompt=prompt,
        preset_model_group="medium",
        timeout=timeout,
        max_retries_per_model=2,
    )


def get_main_site_port(default: int = 8001) -> int:
    """读取主站 Web 服务监听端口（供拼接本站商品详情直达链接）。"""
    try:
        _ensure_mobile_web_on_path()
        from infrastructure.settings import get_settings

        return int(getattr(get_settings(), "web_port", default) or default)
    except Exception:
        return int(default)


def encode_main_site_item_code(
    platform: str,
    product_id: str,
    goods_sign: str = "",
) -> str:
    """复用主站无状态编码器生成 `detail?item=...` 所需的 `p1_...` 商品编码。"""
    try:
        _ensure_mobile_web_on_path()
        from public_boundary import encode_item_code

        target = str(product_id or goods_sign or "").strip()
        if not platform or not target:
            return ""
        return str(encode_item_code(platform, target) or "").strip()
    except Exception:
        return ""


def parse_ai_json(raw_text: str) -> Any:
    """解析大模型返回的 JSON 文本，优先复用主站容错解析器，失败时走标准库兜底。"""
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("AI 返回内容为空")
    try:
        _ensure_mobile_web_on_path()
        from infrastructure.ai_api.json_parser import parse_llm_json

        return parse_llm_json(text)
    except Exception:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", cleaned)
            if m:
                return json.loads(m.group(0))
            raise
