# -*- coding: utf-8 -*-
"""商品领域数据契约及跨缓存、仓储使用的商品身份。"""
import math
import re
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit
from pydantic import BaseModel, Field

_BARE_YUAN_TAG_RE = re.compile(r"^\d+(?:\.\d+)?元$")


def normalize_platform(platform: Optional[str], default: str = "pdd") -> str:
    code = (platform or default).strip().lower()
    return "taobao" if code == "tb" else code


def extract_search_keyword(raw_text: Any, max_length: int = 60) -> str:
    """
    跨平台统一分享文案清洗与搜索关键词提取器：
    1. 若包含「商品标题」书名号且内部非纯口令/链接，优先提取「...」内的商品标题；
    2. 自动剥离 HTTP(S) 链接、淘口令/京口令乱码、平台角标括号及促销引导语；
    3. 压缩空白并安全截断至 max_length，供三端搜索与万能转链标题反查共用。
    """
    text = str(raw_text or "").strip()
    if not text:
        return ""
    bracket_match = re.search(r"「([^」]{2,})」", text)
    if bracket_match:
        candidate = bracket_match.group(1).strip()
        if candidate and not candidate.startswith(("http://", "https://")):
            return candidate[:max_length].strip()

    cleaned = re.sub(r"https?://\S+", " ", text)
    cleaned = re.sub(r"[￥¥$€₴£¢()（）/!！$%&*][A-Za-z0-9]{6,22}[￥¥$€₴£¢()（）/!！$%&*]", " ", cleaned)
    cleaned = re.sub(r"\b(?:CZ|CA|ZH|MF|HU|TG)\s*\d{2,6}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"【(?:淘宝|天猫|京东|京东自营|拼多多|多多进宝|百亿补贴|好物推荐|好物分享|好物精选|手淘分享|券后特惠|特惠参考价|抢购口令|平台口令)[^】]*】",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?:^|\s)\d+\s*[\$￥¥!！:/]+\s*", " ", cleaned)
    cleaned = re.sub(
        r"(?:点击链接直接打开|手机淘宝|淘宝App|复制本段|打开手淘|长按复制[^\n]*|💰[^\n]*|👉[^\n]*)",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。！？、,!?:：-")
    if not cleaned:
        return ""
    return cleaned[:max_length].strip()


def detect_platform_signal(raw_text: Any) -> Optional[str]:
    """
    跨平台统一信号识别器：
    从链接域名、口令包裹符、平台标识词或裸加密商品 ID 中识别明确平台 ('jd' | 'taobao' | 'pdd')。
    若输入为不含任何平台特征的纯商品标题，返回 None（交由调用方决定默认行为或提示用户选择平台）。
    """
    text = str(raw_text or "").strip()
    if not text:
        return None
    lower = text.lower()

    # 1. 显式域名特征识别
    if any(
        d in lower
        for d in ("u.jd.com", "jingfen.jd.com", "union-click.jd.com", "item.jd.com", "m.jd.com", "3.cn", "jingxi.com", "jd.com")
    ):
        return "jd"
    if any(
        d in lower
        for d in (
            "m.tb.cn", "e.tb.cn", "s.tb.cn", "tb.cn", "s.click.taobao.com",
            "uland.taobao.com", "item.taobao.com", "detail.tmall.com", "taobao.com", "tmall.com", "alimama.com",
        )
    ):
        return "taobao"
    if any(d in lower for d in ("p.pinduoduo.com", "yangkeduo.com", "pinduoduo.com", "goods_sign=")):
        return "pdd"

    # 2. 口令与文案特征识别
    if re.search(r"[！!][A-Za-z0-9]{10,20}[！!]", text) or any(
        k in text for k in ("京东", "猄", "崠", "崬", "京口令", "Doōδng")
    ):
        return "jd"
    if re.search(r"[￥¥$€₴£¢()（）][A-Za-z0-9]{8,15}[￥¥$€₴£¢()（）]", text) or any(
        k in text for k in ("淘口令", "手淘", "淘宝", "天猫", "阿里妈妈")
    ):
        return "taobao"
    if any(k in text for k in ("拼多多", "多多进宝", "百亿补贴")) or re.match(r"^[EYc][A-Za-z0-9_-]{25,}$", text):
        return "pdd"

    # 3. 裸加密商品 ID 特征识别
    if len(text) >= 25 and "-" in text and re.match(r"^[A-Za-z0-9_-]{25,}$", text):
        return "taobao"
    if len(text) >= 25 and not text.isdigit() and re.match(r"^[A-Za-z0-9_]{25,}$", text):
        return "jd"

    return None


_SUPPORTED_ECOMMERCE_DOMAINS = (
    "jd.com", "3.cn", "jingxi.com",
    "tb.cn", "taobao.com", "tmall.com", "alimama.com",
    "pinduoduo.com", "yangkeduo.com",
)
_TPWD_TOKEN_RE = re.compile(r"[￥¥$€₴£¢()（）/!！$%&*][A-Za-z0-9]{8,22}[￥¥$€₴£¢()（）/!！$%&*]")
_JCMD_TOKEN_RE = re.compile(r"[！!][A-Za-z0-9]{10,20}[！!]")
_SHARE_CODE_RE = re.compile(r"\b(?:CZ|CA|ZH|MF|HU|TG)\s*\d{3,6}\b", re.IGNORECASE)
_BARE_ENCRYPTED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")


def classify_user_input(raw_text: Any) -> Dict[str, Any]:
    """
    跨入口（首页搜索 / 严选搜索 / 链接解析）统一输入分类器：
    从第一性原理区分「纯搜索词」、「电商分享链接/口令/加密ID」与「非支持的外站链接」，
    避免搜索框把纯链接当关键词盲搜，也避免转链框把纯商品名发往上游接口报错。
    """
    text = str(raw_text or "").strip()
    if not text:
        return {
            "kind": "empty",
            "platform": None,
            "keyword": "",
            "has_link_or_token": False,
        }

    lower = text.lower()
    has_http = bool(re.search(r"https?://", lower))
    has_supported_domain = any(d in lower for d in _SUPPORTED_ECOMMERCE_DOMAINS)
    if has_http and not has_supported_domain:
        return {
            "kind": "unsupported_url",
            "platform": None,
            "keyword": extract_search_keyword(text, max_length=60),
            "has_link_or_token": True,
        }

    is_bare_encrypted_id = bool(
        len(text) >= 25 and not text.isdigit() and _BARE_ENCRYPTED_ID_RE.match(text)
    )
    has_token_signal = bool(
        has_supported_domain
        or "goods_sign=" in lower
        or _TPWD_TOKEN_RE.search(text)
        or _JCMD_TOKEN_RE.search(text)
        or _SHARE_CODE_RE.search(text)
        or is_bare_encrypted_id
    )
    platform = detect_platform_signal(text)
    keyword = "" if is_bare_encrypted_id else extract_search_keyword(text, max_length=60)

    return {
        "kind": "link_or_token" if has_token_signal else "keyword",
        "platform": platform,
        "keyword": keyword,
        "has_link_or_token": has_token_signal,
    }



def clean_product_id(value: Any) -> str:
    """0 是旧清洗函数的缺省值，不可作为真实商品 ID。"""
    if isinstance(value, bool) or isinstance(value, (int, float)) and value == 0:
        return ""
    text = str(value).strip() if value is not None else ""
    return "" if text in ("", "0", "0.0", "None") else text


import hashlib

_JD_IMAGE_KEY_RE = re.compile(r"^[0-9a-fA-F]{14,22}_[0-9a-fA-F]{12,20}$")
_JD_JFS_HASH_RE = re.compile(r"^jfs/[0-9a-f]{16}$")
_JD_ITEM_OR_JINGFEN_URL_RE = re.compile(
    r"^https?://(?:jingfen\.jd\.com/detail|item\.jd\.com)/([^/?#.]+)\.html$",
    re.IGNORECASE,
)
_JD_ITEM_URL_RE = re.compile(r"^https?://item\.jd\.com/([^/?#.]+)\.html$", re.IGNORECASE)


def is_jd_image_key(value: Any) -> bool:
    """判断字符串是否为京东主图 CDN 提取的 <目录哈希>_<文件哈希> 静态唯一标识。"""
    text = clean_product_id(value)
    return bool(text and _JD_IMAGE_KEY_RE.match(text))


def is_jd_jfs_hash_id(value: Any) -> bool:
    """判断字符串是否为京东主图 CDN 提取的 jfs/<16位sha1> 静态唯一标识。"""
    text = clean_product_id(value)
    return bool(text and _JD_JFS_HASH_RE.match(text))


def extract_jd_jfs_hash_id(image_url: Any, *fallback_urls: Any) -> str:
    """从京东官方 CDN 主图 URL (360buyimg.com/jfs/...) 提取跨通道恒定的 16 位十六进制哈希作为稳定 goods_id。"""
    for candidate in (image_url, *fallback_urls):
        if isinstance(candidate, (list, tuple)):
            for sub in candidate:
                res = extract_jd_jfs_hash_id(sub)
                if res:
                    return res
            continue
        raw = str(candidate or "").strip()
        if raw.startswith("//"):
            raw = "https:" + raw
        if not raw.startswith(("http://", "https://")):
            continue
        parsed = urlsplit(raw)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if "360buyimg.com" not in host and "/jfs/" not in path:
            continue
        idx = path.find("/jfs/")
        stable_key = (path[idx:].lower() if idx >= 0 else path.lower()).strip("/")
        if len(stable_key) >= 12:
            digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:16]
            return f"jfs/{digest}"
    return ""


def extract_jd_encrypted_id_from_url(url: Any) -> str:
    """从京东 jingfen.jd.com/detail/{id}.html 或加密版 item.jd.com/{id}.html 中提取加密 itemId。"""
    raw = str(url or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    match = _JD_ITEM_OR_JINGFEN_URL_RE.match(raw)
    if not match:
        return ""
    token = clean_product_id(match.group(1))
    if not token or token.isdigit() or is_jd_jfs_hash_id(token):
        return ""
    return token


def normalize_jd_material_url(raw_url: Any, candidate_id: Any = "") -> str:
    """
    规范化京东商品物料链接：
    - 将用加密 itemId 误拼的 https://item.jd.com/{encrypted_id}.html 统一转换为 https://jingfen.jd.com/detail/{encrypted_id}.html；
    - 当 raw_url 为空且提供 candidate_id 时，数字 SKU 生成 item.jd.com 链接，加密 itemId 生成 jingfen.jd.com 链接。
    """
    url = str(raw_url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith(("http://", "https://")):
        url = ""
    cid = clean_product_id(candidate_id)
    if is_jd_jfs_hash_id(cid):
        cid = ""

    if url:
        m_item = _JD_ITEM_URL_RE.match(url)
        if m_item:
            token = clean_product_id(m_item.group(1))
            if token and not token.isdigit():
                target = cid or ("" if is_jd_jfs_hash_id(token) else token)
                return f"https://jingfen.jd.com/detail/{target}.html" if target else ""
        return url

    if not cid:
        return ""
    if cid.isdigit():
        return f"https://item.jd.com/{cid}.html"
    return f"https://jingfen.jd.com/detail/{cid}.html"


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


def resolve_item_identity(
    item: Dict[str, Any],
    default_platform: Optional[str] = "pdd",
    *,
    allow_jd_sign_desalt: bool = False,
) -> tuple[str, str, str]:
    """从商品字典中统一提取规范化三元组 (platform, goods_id, goods_sign)。"""
    platform = normalize_platform(item.get("platform") or default_platform)
    raw_gid = clean_product_id(item.get("goods_id"))
    raw_pid = clean_product_id(item.get("product_id"))
    raw_sign = str(item.get("goods_sign") or "").strip()

    if platform == "pdd":
        goods_id = raw_gid or (raw_pid if raw_pid != raw_sign else "")
        goods_sign = raw_sign or (raw_pid if not goods_id else "")
    else:
        val = raw_gid or raw_sign or raw_pid
        img_url = str(
            item.get("image_url")
            or item.get("goods_image_url")
            or item.get("goods_thumbnail_url")
            or ""
        ).strip()
        raw_id_candidate = raw_gid or raw_pid or val
        goods_id = (
            extract_canonical_goods_id(
                platform,
                raw_id_candidate,
                image_url=img_url,
                allow_jd_sign_desalt=allow_jd_sign_desalt,
            )
            or raw_id_candidate
        )
        goods_sign = raw_sign or val

    return platform, goods_id, goods_sign


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

    def __init__(self, **data: Any):
        if "coupon_amount" in data or "has_coupon" in data:
            try:
                c_val = round(max(0.0, float(data.get("coupon_amount") or 0.0)), 2)
            except (TypeError, ValueError):
                c_val = 0.0
            if "coupon_amount" in data:
                data["coupon_amount"] = c_val
            if "has_coupon" in data or c_val > 0:
                data["has_coupon"] = c_val > 0
        if "price" in data and "original_price" in data:
            try:
                p_val = float(data["price"])
                o_val = float(data["original_price"])
                c_val = round(max(0.0, float(data.get("coupon_amount") or 0.0)), 2)
                if math.isfinite(p_val) and math.isfinite(o_val):
                    if c_val > 0 and o_val <= p_val:
                        data["original_price"] = round(p_val + c_val, 2)
                    elif o_val < p_val:
                        data["original_price"] = round(p_val, 2)
            except (TypeError, ValueError):
                pass
        super().__init__(**data)

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

    def __init__(self, **data: Any):
        raw_tags = data.get("detail_tags")
        if isinstance(raw_tags, list):
            normalized_tags: List[str] = []
            for tag in raw_tags:
                t = str(tag or "").strip()
                if not t:
                    continue
                if _BARE_YUAN_TAG_RE.match(t):
                    t = f"{t}优惠券"
                if t not in normalized_tags:
                    normalized_tags.append(t)
            data["detail_tags"] = normalized_tags
        super().__init__(**data)


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
