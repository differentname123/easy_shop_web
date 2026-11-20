# -*- coding: utf-8 -*-
"""拼多多多人团互助：独立数据模型、链接解析规则与序列化工具。"""
import difflib
import math
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

ENTRY_TYPE_POSTER = "poster"
ENTRY_TYPE_LINK = "link"
ENTRY_TYPE_TOKEN = "token"

SOURCE_USER_UPLOAD = "user_upload"
SOURCE_USER_TEXT = "user_text"

EXPORT_STATUS_SUCCESS = "success"
EXPORT_STATUS_FAILED = "failed"
EXPORT_TARGET_MIAOZHULI = "miaozhuli"

DEFAULT_GROUP_TTL_SECONDS = 86400  # 默认 24 小时后自动下线
DEFAULT_SYNC_INTERVAL_SECONDS = 600  # 外部多人团定时同步默认间隔：10 分钟
OFFLINE_REASON_UPSTREAM_MISSING = "upstream_missing"  # 两站外部源均已无此团导致自动下架
MAX_PARSE_ATTEMPTS = 3

# 防恶意下架极简阈值：内存级 10 分钟最多下架他人 3 个团；被团长恢复推翻满 2 次直接停用下架权限
TAKEDOWN_RATE_WINDOW_SECONDS = 600
MAX_TAKEDOWNS_PER_WINDOW = 3
PERMANENT_BAN_STRIKES = 2

GROUP_ID_PATH_PATTERN = r"^(?:\d{15,24}|tok_[A-Za-z0-9]{6,28})$"
_NUMERIC_GROUP_ORDER_ID_RE = re.compile(r"^\d{15,24}$")
_GROUP_ORDER_ID_RE = re.compile(GROUP_ID_PATH_PATTERN)
_PDD_TOKEN_CODE_RE = re.compile(r"⇥\s*([A-Za-z0-9]{6,28})\s*⇤")
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", flags=re.IGNORECASE)
_FORBIDDEN_SPAM_RE = re.compile(
    r"(?:加微信|微信号|微信联系|加\s*v\b|加\s*vx\b|\bvx\s*[:：]|wechat|qq群|加群|加\s*q\b|刷单|兼职|代拍|扫码领|t\.me/)",
    flags=re.IGNORECASE,
)
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


class GroupTextSubmitRequest(BaseModel):
    """无海报图片时，用户直接提交拼多多口令或官方拼团链接的请求载荷。"""
    share_text: str = Field(..., min_length=6, max_length=500, description="拼多多 ⇥...⇤ 拼团口令或官方拼团链接")
    goods_name: Optional[str] = Field(default="", max_length=120, description="商品名称（留空时自动从口令文案提取）")
    group_price: Optional[float] = Field(default=None, gt=0, le=50000, description="拼团价格（留空时自动从口令文案提取）")
    original_price: Optional[float] = Field(default=None, description="单买原价（可选）")
    total_people: int = Field(default=0, ge=0, le=50, description="成团总人数（可选，0表示未知）")
    remain_people: int = Field(default=1, ge=1, le=20, description="还差几人成团（默认1，口令含有时自动识别）")
    publisher_nick: Optional[str] = Field(default="", max_length=40, description="发布者昵称（可选）")
    owner_token: Optional[str] = Field(default=None, max_length=64, description="前端生成的团长本地管理口令")


def is_valid_group_order_id(value: Any) -> bool:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return False
    text = str(value).strip()
    return bool(_GROUP_ORDER_ID_RE.match(text))


def is_numeric_group_order_id(value: Any) -> bool:
    """判断是否为拼多多官方数字 group_order_id（可构造 group7.html 跳转链接）。"""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return False
    return bool(_NUMERIC_GROUP_ORDER_ID_RE.match(str(value).strip()))


def contains_forbidden_group_spam(text: Any) -> bool:
    """检查文案中是否包含站外引流、私加联系方式或刷单兼职等违规内容。"""
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_FORBIDDEN_SPAM_RE.search(text))


def is_allowed_pdd_url(url: str) -> bool:
    """白名单校验：仅允许拼多多官方域名（pinduoduo.com / yangkeduo.com）。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == dom or host.endswith(f".{dom}") for dom in _ALLOWED_PDD_DOMAINS)


def extract_pdd_urls(text: str) -> List[str]:
    """提取文本中的拼多多官方链接；若发现任何非拼多多外站链接立即抛出 ValueError。"""
    urls: List[str] = []
    seen = set()
    for match in _HTTP_URL_RE.findall(str(text or "")):
        cleaned = match.rstrip(".,;!?，。；！？)）】]\"'")
        if not cleaned:
            continue
        if not is_allowed_pdd_url(cleaned):
            raise ValueError("仅支持拼多多官方域名链接（pinduoduo.com / yangkeduo.com），已拦截站外链接")
        if cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def extract_pdd_share_token(text: Any) -> str:
    """提取拼多多 ⇥code⇤ 分享口令核心码。"""
    if not isinstance(text, str):
        return ""
    matched = _PDD_TOKEN_CODE_RE.search(text)
    return matched.group(1) if matched else ""


def extract_keywords_from_title(goods_name: str, limit: int = 4) -> List[str]:
    """从商品标题中确定性提取用于匹配与搜索的关键词（不使用易变营销词规则）。"""
    raw = str(goods_name or "").strip()
    if not raw:
        return []
    tokens = re.split(r"[\s/|｜,，。!！?？【】\[\]()（）+＋]+", raw)
    keywords: List[str] = []
    seen = set()
    for token in tokens:
        cleaned = token.strip("-_:：·")
        if len(cleaned) < 2:
            continue
        lowered = cleaned.lower()
        if lowered not in seen:
            seen.add(lowered)
            keywords.append(cleaned[:24])
            if len(keywords) >= limit:
                break
    return keywords


def _extract_numeric_group_id_query(url: str) -> Dict[str, str]:
    """从拼多多跳转链接 query 中提取数字型 group_order_id 与分润参数。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        qs = urllib.parse.parse_qs(parsed.query)
    except ValueError:
        return {}
    gid = (qs.get("group_order_id") or [""])[0].strip()
    if not is_numeric_group_order_id(gid):
        return {}
    return {
        "group_order_id": gid,
        "refer_share_uin": (qs.get("refer_share_uin") or [""])[0].strip(),
        "refer_share_channel": (qs.get("refer_share_channel") or [""])[0].strip(),
        "refer_share_id": (qs.get("refer_share_id") or [""])[0].strip(),
    }


def validate_optional_share_text(raw_text: Any) -> Dict[str, str]:
    """校验海报上传时附带的可选拼团口令/链接（防违规引流与外站链接）。"""
    text = str(raw_text or "").strip()
    if not text:
        return {"share_text": "", "share_code": ""}
    if len(text) > 500:
        raise ValueError("附带口令或文案长度不能超过 500 字符")
    if contains_forbidden_group_spam(text):
        raise ValueError("附带文案包含违规引流或非拼团内容，已拦截")
    extract_pdd_urls(text)
    return {
        "share_text": text,
        "share_code": extract_pdd_share_token(text),
    }


def extract_smart_fields_from_pdd_share_text(raw_text: str) -> Dict[str, Any]:
    """从拼多多分享口令原始文案中智能提取价格、差额人数、总人数与商品名称（SSOT 唯一真源）。"""
    text = str(raw_text or "").strip()
    res: Dict[str, Any] = {}
    if not text:
        return res

    price_m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元\s*拼团购买", text) or re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if price_m:
        try:
            val = float(price_m.group(1))
            if 0 < val <= 50000:
                res["group_price"] = round(val, 2)
        except ValueError:
            pass

    rem_m = re.search(r"仅剩\s*([0-9]+)\s*个名额", text) or re.search(r"(?:还差|差)\s*([0-9]+)\s*人", text)
    if rem_m:
        try:
            rval = int(rem_m.group(1))
            if 1 <= rval <= 20:
                res["remain_people"] = rval
        except ValueError:
            pass

    tot_m = re.search(r"([0-9]+)\s*人团", text)
    if tot_m:
        try:
            tval = int(tot_m.group(1))
            if 2 <= tval <= 50:
                res["total_people"] = tval
        except ValueError:
            pass

    title_m = re.search(r"拼团购买\s*(.*?)(?:\.\.\.|…|，仅剩|$)", text)
    if title_m and len(title_m.group(1).strip()) >= 2:
        t = title_m.group(1).strip()
        suffix_m = re.search(r"仅剩\s*[0-9]+\s*个名额[。，、\s]+(.+?)$", text)
        if suffix_m:
            s = suffix_m.group(1).strip("。，、 ")
            if len(s) >= 2 and s not in t and not re.search(r"(?:点击|复制|打开|微信|qq|私信)", s, re.I):
                t = f"{t}（{s[:24]}）"
        res["goods_name"] = t
    else:
        bracket_m = re.search(r"【([^【】]{3,80})】", text)
        if bracket_m and not re.match(r"^(拼多多|百亿补贴|多人团)$", bracket_m.group(1).strip()):
            res["goods_name"] = bracket_m.group(0).strip()
    return res


def parse_pdd_group_share_fields(
    *,
    share_text: str,
    goods_name: str = "",
    group_price: Any = None,
    original_price: Any = None,
    total_people: Any = 0,
    remain_people: Any = 1,
    publisher_nick: str = "",
) -> Dict[str, Any]:
    """无图拼团口令/链接唯一校验与结构化解析真源（供 C 端提交与外部导入脚本共用）。"""
    raw_share = str(share_text or "").strip()
    if not raw_share:
        raise ValueError("请填写拼多多 ⇥...⇤ 口令或官方拼团链接")
    if len(raw_share) > 500:
        raise ValueError("口令或链接内容过长（最多 500 字符）")

    # 智能补全缺省字段（当调用方未传具体商品名或价格时从文案自动识别）
    auto_fields = extract_smart_fields_from_pdd_share_text(raw_share)
    if not str(goods_name or "").strip() and auto_fields.get("goods_name"):
        goods_name = auto_fields["goods_name"]
    if (group_price is None or group_price == "" or str(group_price).strip() == "") and auto_fields.get("group_price"):
        group_price = auto_fields["group_price"]
    if (remain_people is None or remain_people == 1 or remain_people == "") and auto_fields.get("remain_people"):
        remain_people = auto_fields["remain_people"]
    if (not total_people or total_people == 0 or total_people == "") and auto_fields.get("total_people"):
        total_people = auto_fields["total_people"]

    cleaned_name = re.sub(r"\s+", " ", str(goods_name or "")).strip()
    cleaned_nick = re.sub(r"\s+", " ", str(publisher_nick or "")).strip()[:40]

    for field_val in (raw_share, cleaned_name, cleaned_nick):
        if contains_forbidden_group_spam(field_val):
            raise ValueError("内容包含违规引流或非拼团广告信息，已拦截")

    if _HTTP_URL_RE.search(cleaned_name) or _HTTP_URL_RE.search(cleaned_nick):
        raise ValueError("商品名称或昵称中不能包含网页链接")

    pdd_urls = extract_pdd_urls(raw_share)
    share_code = extract_pdd_share_token(raw_share)
    if not share_code and not pdd_urls:
        raise ValueError("请粘贴包含 ⇥...⇤ 的拼多多口令，或拼多多官方拼团链接")

    meaningful_chars = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", cleaned_name)
    if len(meaningful_chars) < 3 or len(cleaned_name) > 120:
        raise ValueError("商品名称至少需包含 3 个有效汉字/字母/数字，且不超过 120 字")

    try:
        price_val = float(group_price)
    except (TypeError, ValueError):
        raise ValueError("请填写有效的拼团价格（0 ~ 50000 元）")
    if isinstance(group_price, bool) or not math.isfinite(price_val) or price_val <= 0 or price_val > 50000:
        raise ValueError("请填写有效的拼团价格（0 ~ 50000 元）")
    price_val = round(price_val, 2)

    orig_val: Optional[float] = None
    if original_price is not None and not isinstance(original_price, bool) and str(original_price).strip() != "":
        try:
            cand_orig = float(original_price)
            if math.isfinite(cand_orig) and price_val < cand_orig <= 100000:
                orig_val = round(cand_orig, 2)
        except (TypeError, ValueError):
            orig_val = None

    if isinstance(remain_people, bool):
        raise ValueError("还差人数格式错误")
    try:
        rem_val = int(remain_people)
    except (TypeError, ValueError):
        raise ValueError("还差人数必须为整数")
    if rem_val < 1 or rem_val > 20:
        raise ValueError("还差人数必须在 1 ~ 20 人之间")

    if isinstance(total_people, bool):
        tot_val = 0
    else:
        try:
            tot_val = int(total_people or 0)
        except (TypeError, ValueError):
            raise ValueError("成团总人数必须为整数")
    if tot_val < 0 or tot_val > 50:
        raise ValueError("成团总人数必须在 0 ~ 50 人之间")
    if tot_val == 1:
        tot_val = 0
    if tot_val > 0 and rem_val > tot_val:
        rem_val = tot_val

    gid = ""
    redirect_url = ""
    short_url = pdd_urls[0] if pdd_urls else ""
    refer_uin = ""
    refer_channel = ""
    refer_id = ""
    for candidate_url in pdd_urls:
        parsed_q = _extract_numeric_group_id_query(candidate_url)
        if parsed_q:
            gid = parsed_q["group_order_id"]
            redirect_url = candidate_url
            refer_uin = parsed_q["refer_share_uin"]
            refer_channel = parsed_q["refer_share_channel"]
            refer_id = parsed_q["refer_share_id"]
            break

    if gid:
        entry_type = ENTRY_TYPE_LINK
        needs_url_resolve = False
        jumps = build_group_jump_urls(
            gid,
            refer_share_channel=refer_channel,
            refer_share_uin=refer_uin,
            refer_share_id=refer_id,
        )
    elif pdd_urls and not share_code:
        entry_type = ENTRY_TYPE_LINK
        needs_url_resolve = True
        jumps = {}
    else:
        gid = f"tok_{share_code}"
        entry_type = ENTRY_TYPE_TOKEN
        needs_url_resolve = bool(pdd_urls)
        jumps = {}

    return {
        "group_order_id": gid,
        "entry_type": entry_type,
        "needs_url_resolve": needs_url_resolve,
        "short_url": short_url,
        "redirect_url": redirect_url,
        "refer_share_uin": refer_uin,
        "refer_share_channel": refer_channel,
        "refer_share_id": refer_id,
        "app_wake_url": jumps.get("app_wake_url", ""),
        "h5_join_url": jumps.get("h5_join_url", ""),
        "brand_card_url": jumps.get("brand_card_url", ""),
        "share_text": raw_share,
        "share_code": share_code,
        "goods_name": cleaned_name,
        "keywords": extract_keywords_from_title(cleaned_name),
        "group_price": price_val,
        "original_price": orig_val,
        "total_people": tot_val,
        "remain_people": rem_val,
        "publisher_nick": cleaned_nick,
    }


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

    has_numeric_gid = is_numeric_group_order_id(gid)
    jumps = (
        build_group_jump_urls(
            gid,
            refer_share_channel=str(doc.get("refer_share_channel") or ""),
            refer_share_uin=str(doc.get("refer_share_uin") or ""),
            refer_share_id=str(doc.get("refer_share_id") or ""),
        )
        if has_numeric_gid
        else {}
    )
    has_poster = bool(str(doc.get("poster_path") or "").strip())
    default_entry_type = ENTRY_TYPE_POSTER if has_poster else (ENTRY_TYPE_LINK if has_numeric_gid else ENTRY_TYPE_TOKEN)
    entry_type = str(doc.get("entry_type") or default_entry_type).strip() or default_entry_type
    source = str(doc.get("source") or (SOURCE_USER_UPLOAD if has_poster else SOURCE_USER_TEXT)).strip()

    keywords: List[str] = [
        str(k).strip() for k in (doc.get("keywords") or []) if isinstance(k, str) and str(k).strip()
    ]
    parse_status = str(doc.get("parse_status") or PARSE_STATUS_PENDING)
    fallback_name = "商品信息识别未成功" if parse_status == PARSE_STATUS_FAILED else "正在识别商品名称..."

    return {
        "group_order_id": gid,
        "entry_type": entry_type,
        "source": source,
        "has_poster": has_poster,
        "goods_name": str(doc.get("goods_name") or "").strip() or fallback_name,
        "keywords": keywords,
        "group_price": doc.get("group_price"),
        "original_price": doc.get("original_price"),
        "total_people": int(doc.get("total_people") or 0),
        "remain_people": int(doc.get("remain_people") if doc.get("remain_people") is not None else 1),
        "parse_status": parse_status,
        "parse_error": str(doc.get("parse_error") or ""),
        "group_status": effective_status,
        "user_takedown": bool(doc.get("user_takedown")),
        "offline_reason": str(doc.get("offline_reason") or ""),
        "matched_group_order_id": str(doc.get("matched_group_order_id") or ""),
        "matched_group": doc.get("matched_group"),
        "click_count": int(doc.get("click_count") or 0),
        "feedback_count": int(doc.get("feedback_count") or 0),
        "created_at": int(created_at),
        "updated_at": int(doc.get("updated_at") or created_at),
        "expires_at": int(expires_at),
        "remaining_seconds": remaining_seconds,
        "exported_targets": doc.get("exported_targets") or {},
        "last_exported_at": doc.get("last_exported_at"),
        "poster_url": f"/api/group_help/poster/{gid}" if has_poster else "",
        "share_text": str(doc.get("share_text") or "").strip(),
        "share_code": str(doc.get("share_code") or "").strip(),
        "publisher_nick": str(doc.get("publisher_nick") or "").strip(),
        "app_wake_url": jumps.get("app_wake_url") or (doc.get("app_wake_url", "") if has_numeric_gid else ""),
        "h5_join_url": jumps.get("h5_join_url") or (doc.get("h5_join_url", "") if has_numeric_gid else ""),
        "brand_card_url": jumps.get("brand_card_url") or (doc.get("brand_card_url", "") if has_numeric_gid else ""),
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
        (r"(\d+(?:\.\d+)?)\s*(?:小时|时|hr|h)(?![a-zA-Z])", 3600.0),
        (r"(\d+(?:\.\d+)?)\s*(?:分钟|分|min|m)(?![a-zA-Z])", 60.0),
        (r"(\d+(?:\.\d+)?)\s*(?:秒钟|秒|sec|s)(?![a-zA-Z])", 1.0),
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


