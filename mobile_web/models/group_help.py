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


def build_group_jump_urls(
    group_order_id: str,
    refer_share_channel: str = "",
    refer_share_uin: str = "",
    refer_share_id: str = "",
) -> Dict[str, str]:
    """根据 19 位 group_order_id（及可选分享追踪参数）构造跨端参团链接。"""
    gid = str(group_order_id).strip()
    params = [("group_order_id", gid)]
    if refer_share_channel:
        params.append(("refer_share_channel", str(refer_share_channel).strip()))
    if refer_share_uin:
        params.append(("refer_share_uin", str(refer_share_uin).strip()))
    if refer_share_id:
        params.append(("refer_share_id", str(refer_share_id).strip()))
    query_str = urllib.parse.urlencode(params)
    return {
        "app_wake_url": f"pinduoduo://com.xunmeng.pinduoduo/group7.html?{query_str}",
        "h5_join_url": f"https://mobile.yangkeduo.com/group7.html?{query_str}",
        "brand_card_url": (
            "https://mobile.yangkeduo.com/pincard_ask.html"
            f"?__rp_name=brand_amazing_price_group&_pdd_tc=ffffff&_pdd_sbs=1&{query_str}"
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

    jumps = build_group_jump_urls(
        group_order_id,
        refer_share_channel=refer_share_channel,
        refer_share_uin=refer_share_uin,
        refer_share_id=refer_share_id,
    )
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

    jumps = (
        build_group_jump_urls(
            gid,
            refer_share_channel=str(doc.get("refer_share_channel") or ""),
            refer_share_uin=str(doc.get("refer_share_uin") or ""),
            refer_share_id=str(doc.get("refer_share_id") or ""),
        )
        if gid
        else {}
    )
    keywords: List[str] = [
        str(k).strip() for k in (doc.get("keywords") or []) if isinstance(k, str) and str(k).strip()
    ]
    parse_status = str(doc.get("parse_status") or PARSE_STATUS_PENDING)
    fallback_name = "商品信息识别未成功" if parse_status == PARSE_STATUS_FAILED else "正在识别商品名称..."

    return {
        "group_order_id": gid,
        "goods_name": str(doc.get("goods_name") or "").strip() or fallback_name,
        "keywords": keywords,
        "group_price": doc.get("group_price"),
        "original_price": doc.get("original_price"),
        "total_people": int(doc.get("total_people") or 0),
        "remain_people": int(doc.get("remain_people") if doc.get("remain_people") is not None else 1),
        "parse_status": parse_status,
        "parse_error": str(doc.get("parse_error") or ""),
        "group_status": effective_status,
        "click_count": int(doc.get("click_count") or 0),
        "feedback_count": int(doc.get("feedback_count") or 0),
        "created_at": int(created_at),
        "updated_at": int(doc.get("updated_at") or created_at),
        "expires_at": int(expires_at),
        "remaining_seconds": remaining_seconds,
        "poster_url": f"/api/group_help/poster/{gid}",
        "app_wake_url": jumps.get("app_wake_url") or doc.get("app_wake_url", ""),
        "h5_join_url": jumps.get("h5_join_url") or doc.get("h5_join_url", ""),
        "brand_card_url": jumps.get("brand_card_url") or doc.get("brand_card_url", ""),
    }


_INVALID_GOODS_NAMES = {
    "商品完整名称",
    "拼多多百亿补贴多人团商品",
    "拼多多多人团商品",
    "未知商品",
    "未识别",
    "无",
    "null",
    "none",
    "商品名称",
}


def _parse_single_time_value(val: Any) -> Optional[int]:
    """将单个时间值（HH:MM:SS 字符串、中文时长字符串或秒数数值）解析为秒数。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        sec = int(round(float(val)))
        return sec if sec >= 0 else None
    if not isinstance(val, str):
        return None

    s = val.strip().replace("：", ":")
    if not s or s.lower() in ("null", "none", "n/a", "未知", "无"):
        return None

    # 1. 纯数字字符串（秒数）
    if re.match(r"^\d+(?:\.\d+)?$", s):
        return int(round(float(s)))

    # 2. 标准 HH:MM:SS 或 HH:MM 格式
    m_colon = re.search(r"(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?", s)
    if m_colon:
        h = int(m_colon.group(1))
        m = int(m_colon.group(2))
        sec = int(m_colon.group(3)) if m_colon.group(3) is not None else 0
        return h * 3600 + m * 60 + sec

    # 3. 含中文或英文单位的时长字符串（如“剩余 23小时”、“5小时30分”、“45分钟”）
    total_sec = 0.0
    matched_unit = False
    for pat, mult in (
        (r"(\d+(?:\.\d+)?)\s*(?:天|日)", 86400.0),
        (r"(\d+(?:\.\d+)?)\s*(?:小时|时|hr|h)(?!\w)", 3600.0),
        (r"(\d+(?:\.\d+)?)\s*(?:分钟|分|min|m)(?!\w)", 60.0),
        (r"(\d+(?:\.\d+)?)\s*(?:秒钟|秒|sec|s)(?!\w)", 1.0),
    ):
        m_u = re.search(pat, s, flags=re.IGNORECASE)
        if m_u:
            total_sec += float(m_u.group(1)) * mult
            matched_unit = True
    if matched_unit:
        return int(round(total_sec))

    return None


def parse_remaining_seconds(raw_time: Any = None, raw_seconds: Any = None) -> Optional[int]:
    """
    统一解析大模型返回的剩余时间（优先使用标准 HH:MM:SS / 时长文本，其次使用秒数）：
    - 若明确为 0 秒，返回 0（表示海报显示该团已结束）；
    - 若为有效正数，限制在 [60, DEFAULT_GROUP_TTL_SECONDS] 区间并返回整数秒；
    - 若未提供或无法解析，返回 None。
    """
    sec = _parse_single_time_value(raw_time)
    if sec is None:
        sec = _parse_single_time_value(raw_seconds)
    if sec is None:
        return None
    if sec <= 0:
        return 0
    return min(max(60, sec), DEFAULT_GROUP_TTL_SECONDS)


def _parse_positive_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
    elif isinstance(val, str):
        m = re.search(r"\d+(?:\.\d+)?", val.replace(",", ""))
        if not m:
            return None
        num = float(m.group(0))
    else:
        return None
    if num <= 0 or num > 100000:
        return None
    return round(num, 2)


def _parse_int_field(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(round(float(val)))
    if isinstance(val, str):
        m = re.search(r"\d+", val)
        if m:
            return int(m.group(0))
    return None


def validate_and_normalize_parsed_poster(parsed_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    对大模型返回的海报解析结果执行严格的字段校验与标准化：
    必须同时具备有效的商品名称、拼团价格、差额人数，且未过期，否则拒绝上线并抛出可读原因。
    """
    if not isinstance(parsed_data, dict):
        raise ValueError("海报识别数据结构不合法")

    if parsed_data.get("is_group_poster") is False:
        raise ValueError(
            "图片不是完整的拼多多多人团分享海报，请上传包含商品图、商品标题、拼团价及二维码的完整海报（勿仅截取局部二维码）"
        )

    goods_name = str(parsed_data.get("goods_name") or "").strip()
    if len(goods_name) < 3 or goods_name.lower() in _INVALID_GOODS_NAMES:
        raise ValueError(
            "图片中未识别到商品名称，请上传包含商品主图、标题、价格和二维码的完整拼团海报（勿仅截取局部二维码）"
        )

    group_price = _parse_positive_float(parsed_data.get("group_price"))
    if group_price is None:
        raise ValueError("图片中未识别到有效的拼团价格，请上传包含商品标题与拼团价的完整分享海报")

    remain_people = _parse_int_field(parsed_data.get("remain_people"))
    if remain_people is None or remain_people < 1 or remain_people > 20:
        raise ValueError("图片中未识别到有效的成团差额人数（如“还差X人”），请上传完整的拼团邀请海报")

    total_people = _parse_int_field(parsed_data.get("total_people")) or 0
    if total_people < 0 or total_people > 50:
        total_people = 0
    if total_people > 0 and remain_people >= total_people:
        remain_people = max(1, total_people - 1)

    rem_sec = parse_remaining_seconds(
        raw_time=parsed_data.get("remaining_time"),
        raw_seconds=parsed_data.get("remaining_seconds"),
    )
    if rem_sec == 0:
        raise ValueError("图片上显示的拼团剩余时间已结束，无法上架")

    original_price = _parse_positive_float(parsed_data.get("original_price"))

    raw_keywords = parsed_data.get("keywords") or []
    keywords: List[str] = []
    if isinstance(raw_keywords, list):
        for kw in raw_keywords:
            if isinstance(kw, str) and kw.strip() and kw.strip() not in keywords:
                keywords.append(kw.strip()[:32])

    return {
        "is_group_poster": True,
        "goods_name": goods_name,
        "keywords": keywords,
        "group_price": group_price,
        "original_price": original_price,
        "total_people": total_people,
        "remain_people": remain_people,
        "remaining_seconds": rem_sec,
    }

