# -*- coding: utf-8 -*-
"""商品领域数据契约及跨缓存、仓储使用的商品身份。"""
import re
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit
from pydantic import BaseModel, Field


def normalize_platform(platform: Optional[str], default: str = "pdd") -> str:
    code = (platform or default).strip().lower()
    return "taobao" if code == "tb" else code


def clean_product_id(value: Any) -> str:
    """0 是旧清洗函数的缺省值，不可作为真实商品 ID。"""
    if isinstance(value, bool) or isinstance(value, (int, float)) and value == 0:
        return ""
    text = str(value).strip() if value is not None else ""
    return "" if text in ("", "0", "0.0", "None") else text


_JD_IMAGE_KEY_RE = re.compile(r"^[0-9a-fA-F]{14,22}_[0-9a-fA-F]{12,20}$")


def is_jd_image_key(value: Any) -> bool:
    """判断字符串是否为京东主图 CDN 提取的 <目录哈希>_<文件哈希> 静态唯一标识。"""
    text = clean_product_id(value)
    return bool(text and _JD_IMAGE_KEY_RE.match(text))


def extract_jd_image_key(image_url: Any) -> str:
    """
    从京东官方 CDN 主图 URL (360buyimg.com / jfs) 中提取稳定的商品唯一标识：
    例如 https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg
    提取末尾两级哈希路径 -> '6ae3f40fF84b3cfef_0083320320005d73'。
    无论商品来自实时榜单、全网搜索、单品详情还是万能转链，也无论底层使用哪个 ZTK_APPKEY / unionId，
    同一京东商品的主图 CDN 哈希标识始终恒定不变。
    """
    text = str(image_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit("https:" + text if text.startswith("//") else text)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if not (host == "360buyimg.com" or host.endswith(".360buyimg.com") or "/jfs/" in path):
            return ""
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            stem = re.sub(r"\.(?:jpg|jpeg|png|webp|gif|avif)+$", "", parts[-1], flags=re.IGNORECASE)
            parent = parts[-2]
            if (
                len(parent) >= 8
                and len(stem) >= 8
                and all(c.isalnum() or c in "_-" for c in parent)
                and all(c.isalnum() or c in "_-" for c in stem)
            ):
                return f"{parent}_{stem}"
    except ValueError:
        return ""
    return ""


def extract_canonical_goods_id(
    platform: Optional[str],
    raw_id_or_sign: Any = "",
    *,
    image_url: Any = "",
    allow_jd_sign_desalt: bool = False,
) -> str:
    """
    第一性原理商品唯一主键 (goods_id / product_id) 规范化提取器：
    将“用于数据库去重与缓存锚定的静态唯一标识 (goods_id)”与“用于调用上游接口的动态混淆签名 (goods_sign)”解耦：
    1. 淘宝 (taobao)：
       TOP 27939 权限包与折淘客返回的加密 item_id 固定为 '<动态请求盐值前缀>-<稳定商品密文后缀>'
       （如 '0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd'）。
       每次请求 '-' 前缀都会变化，但 '-' 后的商品密文后缀（如 '0v7DKOgI5JqnZd4Nhxd'）在榜单、搜索、详情、转链间 100% 恒定唯一。
    2. 京东 (jd)：
       - 优先从京东主图 URL (360buyimg.com/jfs/...) 提取 '<目录哈希>_<文件哈希>' 作为跨通道恒定唯一标识；
       - 当仅提供 50 位京东加密 itemId 且开启 allow_jd_sign_desalt 时，剥离中间随时间变化的 6 位动态盐值，
         提取 '<14位前缀>_<下划线后18位后缀>' 作为稳定标识。
    3. 拼多多 (pdd)：
       保持原生数字 goods_id 不变。
    """
    plat = normalize_platform(platform)
    raw = clean_product_id(raw_id_or_sign)
    if plat == "taobao":
        if "-" in raw:
            prefix, suffix = raw.split("-", 1)
            if (
                len(prefix) >= 15
                and len(suffix) >= 12
                and all(c.isalnum() or c == "_" for c in suffix)
            ):
                return suffix
        return raw
    if plat == "jd":
        img_key = extract_jd_image_key(image_url)
        if img_key:
            return img_key
        if allow_jd_sign_desalt and "_" in raw and len(raw) >= 35:
            prefix, suffix = raw.split("_", 1)
            if len(prefix) >= 14 and len(suffix) >= 12:
                return f"{prefix[:14]}_{suffix}"
        return raw
    return raw



@dataclass(frozen=True)
class ProductRef:
    """平台、真实 ID 与加密 Sign 独立保存，不将请求里的两者视为已验证别名。"""
    platform: str = "pdd"
    goods_id: str = ""
    goods_sign: str = ""

    def __post_init__(self):
        object.__setattr__(self, "platform", normalize_platform(self.platform))
        object.__setattr__(self, "goods_id", clean_product_id(self.goods_id))
        object.__setattr__(self, "goods_sign", str(self.goods_sign or "").strip())

    @property
    def key(self) -> str:
        return self.goods_sign or self.goods_id

    def cache_keys(self, uid: Optional[str] = None) -> list:
        scope = ("detail", self.platform, uid or "")
        return [scope + (kind, value) for kind, value in
                (("sign", self.goods_sign), ("id", self.goods_id)) if value]


def utc_millis(value: datetime) -> datetime:
    """来源时间统一为UTC及BSON毫秒精度，不在此生成新的来源时钟。"""
    if not isinstance(value, datetime):
        raise ValueError("详情来源时间必须为 datetime")
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


class ProductSummary(BaseModel):
    """商品卡片摘要。goods_id 可为空，不能用 Sign 冒充数字 ID。"""
    platform: str = "pdd"
    platform_name: str = "拼多多"
    goods_id: str = ""
    goods_sign: str = ""
    name: str
    image_url: str
    product_url: str = ""
    price: float
    original_price: float
    coupon_amount: float = 0.0
    has_coupon: bool = False
    commission_rate: float = 0.0
    commission_amount: float = 0.0
    sales_tip: str = "0"
    mall_name: str = "品牌官方店"
    brand: str = "官方甄选"
    category: str = "精选好物"
    updated_at: Optional[str] = None

    def model_dump(self, **kwargs) -> Dict[str, Any]:
        """兼顾 Pydantic v1 (.dict) 与 v2 (.model_dump)"""
        dump_fn = getattr(super(), "model_dump", None)
        if callable(dump_fn):
            return dump_fn(**kwargs)
        return self.dict(exclude_unset=kwargs.get("exclude_unset", False))

    def to_dict(self, **kwargs) -> Dict[str, Any]:
        return self.model_dump(**kwargs)


class ProductDetail(ProductSummary):
    """完整商品详情，商业承诺和券信息仅保存来源提供的真实值。"""
    desc: str = ""
    carousel_images: List[str] = Field(default_factory=list)
    detail_tags: List[str] = Field(default_factory=list)
    service_guarantees: List[str] = Field(default_factory=list)
    shop_ratings: Dict[str, str] = Field(default_factory=dict)
    specs: List[Dict[str, str]] = Field(default_factory=list)
    coupon_info: Dict[str, Any] = Field(default_factory=dict)


_DETAIL_FIELDS = frozenset((getattr(ProductDetail, "model_fields", None) or ProductDetail.__fields__).keys()) | {
    "brand_name", "category_name", "unified_tags", "search_id",
}


@dataclass(frozen=True)
class DetailSnapshot:
    """内部公共详情快照；与HTTP DTO分离，禁止夹带推广及用户上下文。"""
    ref: ProductRef
    data: Dict[str, Any]
    fetched_at: datetime

    def __post_init__(self):
        if not isinstance(self.ref, ProductRef) or not self.ref.key or not isinstance(self.data, dict):
            raise ValueError("详情快照缺少可信商品身份或内容")
        data = {key: deepcopy(value) for key, value in self.data.items() if key in _DETAIL_FIELDS}
        platform = normalize_platform(data.get("platform") or self.ref.platform)
        actual = ProductRef(platform, data.get("goods_id"), data.get("goods_sign"))
        if (platform != self.ref.platform or actual.goods_id != self.ref.goods_id
                or actual.goods_sign != self.ref.goods_sign):
            raise ValueError("详情快照与商品身份不一致")
        data["platform"] = platform
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "fetched_at", utc_millis(self.fetched_at))


@dataclass(frozen=True)
class DetailPrefetchResult:
    """预取内部结果不冒用HTTP成功；只有ready代表已确认持久化。"""
    status: str
    snapshot: Optional[DetailSnapshot] = None
    retryable: bool = False
    next_check_at: float = 0.0
