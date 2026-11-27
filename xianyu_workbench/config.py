# -*- coding: utf-8 -*-
"""工作台独立配置：支持环境变量与工作台本地 JSON 覆盖，不强依赖主站配置。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

WORKBENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = WORKBENCH_DIR.parent
DEFAULT_CONFIG_PATH = WORKBENCH_DIR / "config.local.json"
DEFAULT_DEV_AUTH_SECRET = "xy_workbench_local_dev_secret_change_me"


def _read_local_json(config_path: Optional[Path] = None) -> Dict[str, Any]:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class WorkbenchConfig:
    host: str = "0.0.0.0"
    port: int = 8002
    auth_secret: str = DEFAULT_DEV_AUTH_SECRET
    token_ttl_seconds: int = 30 * 86400
    mongo_uri: str = ""
    mongo_db_name: str = "crawler_data"
    data_dir: Path = WORKBENCH_DIR / "data"
    screenshot_dir: Path = WORKBENCH_DIR / "data" / "eval_screenshots"
    upload_dir: Path = WORKBENCH_DIR / "data" / "uploads"
    channel_profiles_dir: Path = WORKBENCH_DIR / "data" / "channel_profiles"
    deal_image_dir: Path = REPO_ROOT / "mobile_web" / "runtime" / "deal_submit_images"
    default_official_image_count: int = 3
    max_images_per_listing: int = 9
    max_upload_bytes: int = 5 * 1024 * 1024


def load_config(config_path: Optional[Path] = None) -> WorkbenchConfig:
    """加载工作台配置，环境变量优先级高于本地 config.local.json。"""
    local = _read_local_json(config_path)
    data_dir = Path(os.environ.get("XY_DATA_DIR") or local.get("data_dir") or (WORKBENCH_DIR / "data"))
    screenshot_dir = Path(
        os.environ.get("XY_SCREENSHOT_DIR")
        or local.get("screenshot_dir")
        or (data_dir / "eval_screenshots")
    )
    upload_dir = Path(
        os.environ.get("XY_UPLOAD_DIR")
        or local.get("upload_dir")
        or (data_dir / "uploads")
    )
    channel_profiles_dir = Path(
        os.environ.get("XY_CHANNEL_PROFILES_DIR")
        or local.get("channel_profiles_dir")
        or (data_dir / "channel_profiles")
    )
    deal_image_dir = Path(
        os.environ.get("XY_DEAL_IMAGE_DIR")
        or local.get("deal_image_dir")
        or (REPO_ROOT / "mobile_web" / "runtime" / "deal_submit_images")
    )
    port_raw = os.environ.get("XY_WORKBENCH_PORT") or local.get("port") or 8002
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 8002

    return WorkbenchConfig(
        host=str(os.environ.get("XY_WORKBENCH_HOST") or local.get("host") or "0.0.0.0").strip(),
        port=port,
        auth_secret=str(
            os.environ.get("XY_AUTH_SECRET")
            or local.get("auth_secret")
            or DEFAULT_DEV_AUTH_SECRET
        ).strip(),
        token_ttl_seconds=int(local.get("token_ttl_seconds") or 30 * 86400),
        mongo_uri=str(os.environ.get("XY_MONGO_URI") or local.get("mongo_uri") or "").strip(),
        mongo_db_name=str(
            os.environ.get("XY_MONGO_DB_NAME") or local.get("mongo_db_name") or "crawler_data"
        ).strip(),
        data_dir=data_dir,
        screenshot_dir=screenshot_dir,
        upload_dir=upload_dir,
        channel_profiles_dir=channel_profiles_dir,
        deal_image_dir=deal_image_dir,
        default_official_image_count=max(1, int(local.get("default_official_image_count") or 3)),
        max_images_per_listing=max(1, int(local.get("max_images_per_listing") or 9)),
        max_upload_bytes=max(256 * 1024, int(local.get("max_upload_bytes") or 5 * 1024 * 1024)),
    )
