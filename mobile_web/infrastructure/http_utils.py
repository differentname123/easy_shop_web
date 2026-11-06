# -*- coding: utf-8 -*-
"""跨平台客户端与适配器的公共解析、时间转换与折淘客多 Key 容灾工具。"""
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SHANGHAI = timezone(timedelta(hours=8))

ZTK_KEY_FAILURE_KEYWORDS = (
    "appkey", "key", "频次", "频率", "超限", "上限", "次数", "黑名单",
    "封禁", "未授权", "到期", "过期", "积分", "余额", "限制",
)


def clean_text(value: Any) -> str:
    """将任意标量安全转换为去首尾空白的字符串，忽略布尔值与空占位。"""
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return "" if text in ("None", "null") else text


def clean_money(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """将金额或费率字段安全转换为非负两位小数浮点数。"""
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0:
        return default
    return round(number, 2)


def clean_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """将整数标识或状态码安全转换为 int。"""
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_http_url(value: Any) -> str:
    """标准化 http/https 链接，支持补全协议相对路径 //。"""
    raw = clean_text(value)
    if raw.startswith("//"):
        raw = "https:" + raw
    return raw if raw.startswith(("https://", "http://")) else ""


def format_shanghai_time(timestamp: Any) -> Optional[str]:
    """将秒级时间戳转换为北京时间 YYYY-MM-DD HH:MM:SS 字符串。"""
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        return None
    return datetime.fromtimestamp(int(timestamp), tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def parse_shanghai_timestamp(value: Any) -> Optional[int]:
    """将北京时间字符串或秒级/毫秒级时间戳统一转换为秒级 UTC 时间戳。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        number = int(value)
        return number // 1000 if number > 10_000_000_000 else number
    raw = clean_text(value)
    if raw.isdigit():
        number = int(raw)
        return number // 1000 if number > 10_000_000_000 else (number if number > 0 else None)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(raw, fmt).replace(tzinfo=SHANGHAI).timestamp())
        except ValueError:
            continue
    return None


def parse_appkey_list(raw: Any, extra_list: Any = None) -> Tuple[str, ...]:
    """
    将单个折淘客 appkey、逗号分隔字符串或字符串列表/元组规范化为去重后的非空 appkey 元组。
    拒绝布尔值、非字符串及包含控制字符的非法输入。
    """
    def _extract(val: Any) -> Optional[List[str]]:
        if val is None:
            return []
        if isinstance(val, bool):
            return None
        if isinstance(val, str):
            return val.split(",")
        if isinstance(val, (list, tuple)):
            items: List[str] = []
            for item in val:
                if not isinstance(item, str) or isinstance(item, bool):
                    return None
                items.extend(item.split(","))
            return items
        return None

    first = _extract(raw)
    second = _extract(extra_list)
    if first is None or second is None:
        return ()
    seen: List[str] = []
    for key in first + second:
        clean = key.strip()
        if not clean:
            continue
        if any(ord(c) < 33 for c in clean):
            return ()
        if clean not in seen:
            seen.append(clean)
    return tuple(seen)


def should_failover_ztk_key(payload: Any) -> bool:
    """判断折淘客响应是否因 AppKey 额度/频次/权限异常需要切换到下一个备用 Key。"""
    if not isinstance(payload, dict):
        return True
    status_val = payload.get("status")
    code_val = payload.get("code")
    if status_val in (200, "200") or code_val in (200, "200"):
        return False
    msg = " ".join(clean_text(payload.get(k)) for k in ("content", "msg", "message", "sub_msg")).lower()
    if "relationid" in msg:
        return False
    return any(kw in msg for kw in ZTK_KEY_FAILURE_KEYWORDS) or status_val not in (None, 200, "200")


def error_dict(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """构造统一的错误返回字典。"""
    return {"status": "error", "error_code": code, "error_msg": message, **extra}
