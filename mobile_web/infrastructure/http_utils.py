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


def parse_finite_money(value: Any) -> Optional[float]:
    """将金额或费率字段转换为有限两位小数浮点数（保留正负号以供调用方校验非法负值）。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return round(number, 2) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def clean_money(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """将金额或费率字段安全转换为非负两位小数浮点数。"""
    number = parse_finite_money(value)
    if number is None or number < 0:
        return default
    return number


def clean_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """将整数标识或状态码安全转换为 int。"""
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_http_url(value: Any) -> str:
    """标准化 http/https 链接，支持补全协议相对路径 // 并验证主机名非空。"""
    raw = clean_text(value)
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.startswith(("https://", "http://")):
        return ""
    rest = raw.split("://", 1)[1]
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    return raw if host else ""


def parse_carousel_images(raw_images: Any, primary_image: str = "") -> List[str]:
    """解析折淘客/通用商品轮播图列表（支持竖线分隔字符串或列表），自动去重并校验 HTTP URL。"""
    if isinstance(raw_images, str):
        candidates = raw_images.split("|")
    elif isinstance(raw_images, (list, tuple)):
        candidates = list(raw_images)
    else:
        candidates = []
    images: List[str] = []
    for item in candidates:
        normalized = normalize_http_url(item)
        if normalized and normalized not in images:
            images.append(normalized)
    primary = normalize_http_url(primary_image)
    if primary and primary not in images:
        images.insert(0, primary)
    return images


def extract_ztk_item_fields(
    raw_item: Any,
    *,
    zero_fallback_to_size: bool = False,
    include_primary_in_carousel: bool = False,
    default_mall_name: str = "",
    default_sales_tip: str = "",
) -> Dict[str, Any]:
    """
    统一解析折淘客 (ZTK) 商品字典字段（复用于淘宝/京东搜索、详情及万能转链反查）。
    """
    raw = raw_item if isinstance(raw_item, dict) else {}
    tao_id = clean_text(raw.get("tao_id") or raw.get("skuId") or raw.get("item_id"))
    title = clean_text(raw.get("title") or raw.get("tao_title") or raw.get("skuName"))
    image_url = normalize_http_url(
        raw.get("pict_url") or raw.get("pic") or raw.get("imageUrl") or raw.get("white_image")
    )
    carousel_images = parse_carousel_images(
        raw.get("small_images") or raw.get("pcDescContent"),
        primary_image=image_url if include_primary_in_carousel else "",
    )
    size_price = max(0.0, clean_money(raw.get("size"), 0.0) or 0.0)
    raw_quanhou = raw.get("quanhou_jiage")
    if zero_fallback_to_size:
        quanhou = max(0.0, clean_money(raw_quanhou, 0.0) or 0.0)
        price = quanhou if quanhou > 0 else size_price
    else:
        fallback_val = size_price if raw_quanhou is None or clean_text(raw_quanhou) == "" else raw_quanhou
        price = max(0.0, clean_money(fallback_val, 0.0) or 0.0)
    coupon_amount = max(0.0, clean_money(raw.get("coupon_info_money"), 0.0) or 0.0)
    if zero_fallback_to_size:
        original_price = size_price if size_price >= price else round(price + coupon_amount, 2)
    else:
        original_price = max(size_price, price)
    commission_rate = max(0.0, clean_money(raw.get("tkrate3"), 0.0) or 0.0)
    raw_tkfee = max(0.0, clean_money(raw.get("tkfee3"), 0.0) or 0.0)
    if zero_fallback_to_size and raw_tkfee <= 0 and price > 0 and commission_rate > 0:
        commission_amount = round(price * commission_rate / 100.0, 2)
    else:
        commission_amount = raw_tkfee

    return {
        "tao_id": tao_id,
        "title": title,
        "image_url": image_url,
        "carousel_images": carousel_images,
        "price": price,
        "original_price": original_price,
        "coupon_amount": coupon_amount,
        "has_coupon": coupon_amount > 0,
        "commission_rate": commission_rate,
        "commission_amount": commission_amount,
        "mall_name": clean_text(raw.get("shop_title") or raw.get("nick")) or default_mall_name,
        "sales_tip": clean_text(raw.get("volume") or raw.get("TKtotalSales")) or default_sales_tip,
        "item_url": normalize_http_url(raw.get("item_url")),
    }



def format_shanghai_time(timestamp: Any) -> Optional[str]:
    """将秒级时间戳转换为北京时间 YYYY-MM-DD HH:MM:SS 字符串。"""
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        return None
    return datetime.fromtimestamp(int(timestamp), tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def parse_shanghai_timestamp(value: Any, *, precise: bool = False) -> Any:
    """将北京时间字符串或秒级/毫秒级时间戳统一转换为 UTC 秒级时间戳（precise=True 时保留浮点秒）。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        seconds = number / 1000 if number > 10**11 else number
        return seconds if precise else int(seconds)
    except (TypeError, ValueError, OverflowError):
        try:
            date = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            seconds = (date if date.tzinfo else date.replace(tzinfo=SHANGHAI)).timestamp()
            return seconds if precise else int(seconds)
        except (ValueError, TypeError, OverflowError, OSError):
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


def execute_with_key_failover(
    client: Any,
    action: Any,
    *,
    should_ignore_error: Any = None,
    should_stop_early: Any = None,
) -> Dict[str, Any]:
    """
    通用多 AppKey 粘性游标故障转移执行器：
    - 从 client._active_key_index 开始轮询 client.appkey_list；
    - 成功时更新 client._active_key_index 和 client.appkey；
    - 遇到非 UPSTREAM_ERROR / NETWORK_ERROR 的确定性错误或 should_stop_early 命中时立即终止返回。
    """
    keys = getattr(client, "appkey_list", ()) or ()
    total = len(keys)
    start_idx = getattr(client, "_active_key_index", 0) % total
    last_result: Optional[Dict[str, Any]] = None

    for offset in range(total):
        idx = (start_idx + offset) % total
        current_key = keys[idx]
        result = action(current_key)
        if result.get("status") == "success":
            client._active_key_index = idx
            client.appkey = current_key
            return result
        if last_result is not None and callable(should_ignore_error) and should_ignore_error(result, last_result):
            continue
        last_result = result
        if result.get("error_code") not in ("UPSTREAM_ERROR", "NETWORK_ERROR"):
            return result
        if callable(should_stop_early) and should_stop_early(result):
            return result
    return last_result or error_dict("UPSTREAM_ERROR", "请求失败")
