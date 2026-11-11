# -*- coding: utf-8 -*-
"""拼多多多人团互助：独立数据模型、链接解析规则与序列化工具。"""
import difflib
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

PARSE_STATUS_PENDING = "pending"
PARSE_STATUS_PARSED = "parsed"
PARSE_STATUS_FAILED = "failed"

GROUP_STATUS_ACTIVE = "active"
GROUP_STATUS_PENDING_CONFIRM = "pending_confirm"
GROUP_STATUS_FULL = "full"
GROUP_STATUS_EXPIRED = "expired"

DEFAULT_GROUP_TTL_SECONDS = 86400  # 默认 24 小时后自动下线
MAX_PARSE_ATTEMPTS = 3

# 防恶意下架极简阈值：内存级 10 分钟最多下架他人 3 个团；被团长恢复推翻满 2 次直接停用下架权限
TAKEDOWN_RATE_WINDOW_SECONDS = 600
MAX_TAKEDOWNS_PER_WINDOW = 3
PERMANENT_BAN_STRIKES = 2

_GROUP_ORDER_ID_RE = re.compile(r"^\d{15,24}$")
_ALLOWED_PDD_DOMAINS = (
    "pinduoduo.com",
    "yangkeduo.com",
)


class GroupHelpAuthRequiredError(Exception):
    """执行他人拼团下架类操作但未登录时抛出（映射 HTTP 401）。"""

    def __init__(self, message: str = "为防止恶意乱标下架，执行该操作请先登录账号"):
        super().__init__(message)
        self.error_code = "auth_required"


class GroupHelpPermissionError(Exception):
    """下架权限被停用、触发短时限频或非团长恢复他人拼团时抛出（映射 HTTP 403 / 429）。"""

    def __init__(self, message: str, error_code: str = "takedown_banned", status_code: int = 403):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


def is_takedown_feedback(doc: Dict[str, Any], action: str, remain_people: Optional[int] = None) -> bool:
    """判断本次反馈操作是否属于会将拼团下架（置为已满或已失效）的动作。"""
    act = str(action or "").strip()
    if act in ("mark_full", "mark_expired"):
        return True
    if act == "set_remain" and remain_people is not None and not isinstance(remain_people, bool):
        return int(remain_people) <= 0
    if act == "joined_one":
        cur_rem = int(doc.get("remain_people") if doc.get("remain_people") is not None else 1)
        return cur_rem <= 1
    return False


def is_relist_feedback(doc: Dict[str, Any], action: str, remain_people: Optional[int] = None) -> bool:
    """判断本次反馈操作是否属于将已下架（已满/已失效）拼团重新恢复上架的动作。"""
    act = str(action or "").strip()
    if act != "set_remain" or remain_people is None or isinstance(remain_people, bool):
        return False
    if int(remain_people) < 1:
        return False
    cur_status = str(doc.get("group_status") or GROUP_STATUS_ACTIVE)
    cur_rem = int(doc.get("remain_people") if doc.get("remain_people") is not None else 1)
    return cur_status in (GROUP_STATUS_FULL, GROUP_STATUS_EXPIRED) or cur_rem <= 0


class GroupFeedbackRequest(BaseModel):
    """用户共创维护拼团状态的请求载荷。"""
    action: str = Field(..., description="动作类型: joined_one | mark_full | mark_expired | set_remain | confirm_publish")
    remain_people: Optional[int] = Field(default=None, description="当 action=set_remain 时指定的剩余差额人数")
    owner_token: Optional[str] = Field(default=None, max_length=64, description="发团团长管理口令（仅团长本人本地持有）")


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
    raw_expires = float(doc.get("expires_at") or (created_at + DEFAULT_GROUP_TTL_SECONDS))
    expires_at = min(raw_expires, created_at + DEFAULT_GROUP_TTL_SECONDS)
    remaining_seconds = max(0, int(expires_at - current_ts))

    raw_status = str(doc.get("group_status") or GROUP_STATUS_ACTIVE)
    if raw_status == GROUP_STATUS_EXPIRED:
        effective_status = GROUP_STATUS_EXPIRED
        remaining_seconds = 0
    elif remaining_seconds <= 0:
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
        "matched_group_order_id": str(doc.get("matched_group_order_id") or ""),
        "matched_group": doc.get("matched_group"),
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
    对大模型返回的海报解析结果执行严格的合规性、纯粹性与必填字段校验：
    1. 必须通过内容合规与官方原生纯粹性审核（is_compliant != False）；
    2. 必须是完整的拼多多多人团分享海报（is_group_poster != False）；
    3. 必须同时具备有效的商品名称、拼团价格、差额人数，且未过期。
    不满足任一条件均抛出可读 ValueError 拒绝上线。
    """
    if not isinstance(parsed_data, dict):
        raise ValueError("海报识别数据结构不合法")

    raw_reject = parsed_data.get("reject_reason")
    reject_reason = ""
    if isinstance(raw_reject, str) and raw_reject.strip().lower() not in ("", "null", "none", "无"):
        reject_reason = raw_reject.strip()[:120]

    if parsed_data.get("is_compliant") is False:
        raise ValueError(
            reject_reason
            or "图片包含明显违规或外部引流广告信息，不予上架（请上传正常的拼多多分享海报）"
        )

    if parsed_data.get("is_group_poster") is False:
        raise ValueError(
            reject_reason
            or "图片不是完整的拼多多多人团分享海报，请上传包含商品图、商品标题、拼团价及二维码的完整海报（勿仅截取局部二维码）"
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
        "is_compliant": True,
        "goods_name": goods_name,
        "keywords": keywords,
        "group_price": group_price,
        "original_price": original_price,
        "total_people": total_people,
        "remain_people": remain_people,
        "remaining_seconds": rem_sec,
    }


def _normalize_goods_title(title: str) -> str:
    """去除商品名称中的标点符号与空白，保留中英文与数字供相似度比对。"""
    if not isinstance(title, str):
        return ""
    return re.sub(r"[\s\W_]+", "", title.lower(), flags=re.UNICODE)


def is_highly_similar_goods(name_a: str, name_b: str) -> bool:
    """
    判断两个商品名称是否高度相似（用于同价在拼商品匹配）：
    1. 若双方均含数字规格（如 3层130抽24包 vs 3层130抽18包）且不互为子集（存在冲突数字），判定为不同规格；
    2. 清洗标点后若一方完整包含另一方（短方长度>=6），或字符二元组 Dice 重合度 >= 0.75，则判定为同款商品。
    """
    norm_a = _normalize_goods_title(name_a)
    norm_b = _normalize_goods_title(name_b)
    if len(norm_a) < 3 or len(norm_b) < 3:
        return False
    if norm_a == norm_b:
        return True

    nums_a = set(re.findall(r"\d+(?:\.\d+)?", str(name_a).lower()))
    nums_b = set(re.findall(r"\d+(?:\.\d+)?", str(name_b).lower()))
    if nums_a and nums_b and not (nums_a <= nums_b or nums_b <= nums_a):
        return False

    if min(len(norm_a), len(norm_b)) >= 6 and (norm_a in norm_b or norm_b in norm_a):
        return True

    bg_a = {norm_a[i : i + 2] for i in range(len(norm_a) - 1)}
    bg_b = {norm_b[i : i + 2] for i in range(len(norm_b) - 1)}
    dice = (2.0 * len(bg_a & bg_b)) / (len(bg_a) + len(bg_b)) if (bg_a and bg_b) else 0.0
    return dice >= 0.75


