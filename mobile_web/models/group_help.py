# -*- coding: utf-8 -*-
"""拼多多多人团互助：独立数据模型、链接解析规则与序列化工具。"""
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

PARSE_STATUS_PENDING = "pending"
PARSE_STATUS_PARSED = "parsed"
PARSE_STATUS_FAILED = "failed"

GROUP_STATUS_ACTIVE = "active"
GROUP_STATUS_FULL = "full"
GROUP_STATUS_EXPIRED = "expired"

DEFAULT_GROUP_TTL_SECONDS = 86400  # 默认 24 小时后自动下线
MAX_PARSE_ATTEMPTS = 3

_GROUP_ORDER_ID_RE = re.compile(r"^\d{15,24}$")
_ALLOWED_PDD_DOMAINS = (
    "pinduoduo.com",
    "yangkeduo.com",
)


class GroupFeedbackRequest(BaseModel):
    """用户共创维护拼团状态的请求载荷。"""
    action: str = Field(..., description="动作类型: joined_one | mark_full | mark_expired | set_remain")
    remain_people: Optional[int] = Field(default=None, description="当 action=set_remain 时指定的剩余差额人数")


def is_valid_group_order_id(value: Any) -> bool:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return False
    text = str(value).strip()
    return bool(_GROUP_ORDER_ID_RE.match(text))


def is_pdd_qr_url(url: str) -> bool:
    """校验二维码解析出的文本是否为拼多多合法域名链接。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _ALLOWED_PDD_DOMAINS)


def build_group_jump_urls(group_order_id: str) -> Dict[str, str]:
    """根据 19 位 group_order_id 构造跨端参团链接。"""
    gid = str(group_order_id).strip()
    return {
        "app_wake_url": f"pinduoduo://com.xunmeng.pinduoduo/group7.html?group_order_id={gid}",
        "h5_join_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid}",
        "brand_card_url": (
            "https://mobile.yangkeduo.com/pincard_ask.html"
            f"?__rp_name=brand_amazing_price_group&_pdd_tc=ffffff&_pdd_sbs=1&group_order_id={gid}"
        ),
    }


def parse_group_redirect_url(url: str) -> Dict[str, str]:
    """从 307 重定向地址或直接长链中提取 group_order_id 及分享元数据。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("重定向地址为空")
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in _ALLOWED_PDD_DOMAINS):
        raise ValueError("非拼多多官方域名链接")

    qs = urllib.parse.parse_qs(parsed.query)
    group_order_id = (qs.get("group_order_id") or [""])[0].strip()
    if not is_valid_group_order_id(group_order_id):
        raise ValueError("未在链接中检测到有效的拼团单号(group_order_id)，请确认上传的是【拼团邀请二维码图片】而非普通商品图")

    refer_share_channel = (qs.get("refer_share_channel") or [""])[0].strip()
    refer_share_uin = (qs.get("refer_share_uin") or [""])[0].strip()
    refer_share_id = (qs.get("refer_share_id") or [""])[0].strip()

    jumps = build_group_jump_urls(group_order_id)
    return {
        "group_order_id": group_order_id,
        "redirect_url": url.strip(),
        "refer_share_channel": refer_share_channel,
        "refer_share_uin": refer_share_uin,
        "refer_share_id": refer_share_id,
        **jumps,
    }


def format_group_doc(doc: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """将数据库文档清洗为前端可直接渲染的字典。"""
    current_ts = float(now if now is not None else time.time())
    gid = str(doc.get("group_order_id") or "")
    created_at = float(doc.get("created_at") or current_ts)
    expires_at = float(doc.get("expires_at") or (created_at + DEFAULT_GROUP_TTL_SECONDS))
    remaining_seconds = max(0, int(expires_at - current_ts))

    raw_status = str(doc.get("group_status") or GROUP_STATUS_ACTIVE)
    if remaining_seconds <= 0 and raw_status == GROUP_STATUS_ACTIVE:
        effective_status = GROUP_STATUS_EXPIRED
    else:
        effective_status = raw_status

    jumps = build_group_jump_urls(gid) if gid else {}
    keywords: List[str] = [
        str(k).strip() for k in (doc.get("keywords") or []) if isinstance(k, str) and str(k).strip()
    ]

    return {
        "group_order_id": gid,
        "goods_name": str(doc.get("goods_name") or "").strip() or "正在识别商品名称...",
        "keywords": keywords,
        "group_price": doc.get("group_price"),
        "original_price": doc.get("original_price"),
        "total_people": int(doc.get("total_people") or 0),
        "remain_people": int(doc.get("remain_people") if doc.get("remain_people") is not None else 1),
        "parse_status": str(doc.get("parse_status") or PARSE_STATUS_PENDING),
        "group_status": effective_status,
        "click_count": int(doc.get("click_count") or 0),
        "feedback_count": int(doc.get("feedback_count") or 0),
        "created_at": int(created_at),
        "updated_at": int(doc.get("updated_at") or created_at),
        "expires_at": int(expires_at),
        "remaining_seconds": remaining_seconds,
        "poster_url": f"/api/group_help/poster/{gid}",
        "app_wake_url": doc.get("app_wake_url") or jumps.get("app_wake_url", ""),
        "h5_join_url": doc.get("h5_join_url") or jumps.get("h5_join_url", ""),
        "brand_card_url": doc.get("brand_card_url") or jumps.get("brand_card_url", ""),
    }
