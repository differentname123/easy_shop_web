# -*- coding: utf-8 -*-
"""可插拔销售渠道抽象协议与通用数据契约（核心选品与素材层严禁直接依赖具体渠道）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, Generator, List, Optional

_SAFE_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


@dataclass(frozen=True)
class ChannelCapabilities:
    qr_login: bool = False
    status_check: bool = False
    sync_selling: bool = False
    auto_publish: bool = False
    auto_downshelf: bool = False
    im_messaging: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass
class ChannelAccountStatus:
    channel: str
    online: bool
    account_id: str = ""
    nickname: str = ""
    error_code: str = ""
    message: str = ""
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QrLoginSnapshot:
    session_id: str
    channel: str
    state: str  # starting | waiting_scan | scanned | success | expired | risk_blocked | error | cancelled
    message: str = ""
    has_qr: bool = False
    account_id: str = ""
    nickname: str = ""
    error_code: str = ""
    expires_in_sec: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelSyncResult:
    ok: bool
    channel: str
    online: bool
    account_id: str = ""
    nickname: str = ""
    error_code: str = ""
    message: str = ""
    online_items: List[Dict[str, Any]] = field(default_factory=list)
    abnormal_items: List[Dict[str, Any]] = field(default_factory=list)
    group_counts: Dict[str, int] = field(default_factory=dict)
    synced_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SellingChannel(ABC):
    """销售渠道适配器抽象基类（闲鱼、淘宝等渠道均实现同一契约）。"""

    channel_code: str = ""
    channel_name: str = ""
    capabilities: ChannelCapabilities = ChannelCapabilities()

    def __init__(self, profiles_root: Path) -> None:
        self.profiles_root = Path(profiles_root)

    def profile_dir_for(self, username: str) -> Path:
        """
        为工作台用户自动分配并创建服务端托管的隔离浏览器目录。
        严禁接受客户端传入任意路径。
        """
        clean_user = str(username or "").strip()
        if not _SAFE_USERNAME_RE.fullmatch(clean_user):
            raise ValueError(f"非法用户名，无法创建渠道托管目录: {username!r}")
        channel_dir = (self.profiles_root / self.channel_code).resolve()
        candidate = (channel_dir / clean_user).resolve()
        if candidate.parent != channel_dir:
            raise ValueError("渠道托管目录路径校验失败")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    @abstractmethod
    def start_qr_login(self, username: str) -> QrLoginSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_qr_login_status(self, username: str, session_id: str) -> Optional[QrLoginSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def get_qr_image_bytes(self, username: str, session_id: str) -> Optional[bytes]:
        raise NotImplementedError

    @abstractmethod
    def cancel_qr_login(self, username: str, session_id: Optional[str] = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def check_status(self, username: str) -> ChannelAccountStatus:
        raise NotImplementedError

    @abstractmethod
    def sync_selling_items(self, username: str) -> ChannelSyncResult:
        raise NotImplementedError

    def publish_item(self, username: str, item_payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError(f"渠道「{self.channel_name or self.channel_code}」暂未开启自动上架")

    def downshelf_item(self, username: str, external_item_id: str) -> Dict[str, Any]:
        raise NotImplementedError(f"渠道「{self.channel_name or self.channel_code}」暂未开启自动下架")

    @contextmanager
    def batch_session(self, username: str) -> Generator["SellingChannel", None, None]:
        """可选的批处理浏览器会话复用上下文，默认退化为逐次调用 self。"""
        yield self

    def shutdown(self) -> None:
        return None
