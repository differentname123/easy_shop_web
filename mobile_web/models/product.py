# -*- coding: utf-8 -*-
"""商品领域数据契约及跨缓存、仓储使用的商品身份。"""
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


def normalize_platform(platform: Optional[str], default: str = "pdd") -> str:
    code = (platform or default).strip().lower()
    return "taobao" if code == "tb" else code


def clean_product_id(value: Any) -> str:
    """0 是旧清洗函数的缺省值，不可作为真实商品 ID。"""
    if isinstance(value, bool) or isinstance(value, (int, float)) and value == 0:
        return ""
    text = str(value).strip() if value is not None else ""
    return "" if text in ("", "0", "0.0", "None") or text.startswith("sign:") else text


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


class ProductSummary(BaseModel):
    """商品卡片摘要。goods_id 可为空，不能用 Sign 冒充数字 ID。"""
    platform: str = "pdd"
    platform_name: str = "拼多多"
    goods_id: str = ""
    goods_sign: str = ""
    name: str
    image_url: str
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
    product_url: str = ""
    detail_tags: List[str] = Field(default_factory=list)
    service_guarantees: List[str] = Field(default_factory=list)
    shop_ratings: Dict[str, str] = Field(default_factory=dict)
    specs: List[Dict[str, str]] = Field(default_factory=list)
    coupon_info: Dict[str, Any] = Field(default_factory=dict)
