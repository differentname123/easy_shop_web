# -*- coding: utf-8 -*-
"""
多平台联盟抽象适配器基类
定义统一的导购商品查询、搜索、推广生链、推手授权备案协议。
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from models.product import ProductRef


def build_command_text(title: str, final_link: str, price=None, coupon=None, app_name: str = "") -> str:
    """共享文案结构；具体平台名称由适配器传入。"""
    lines = [f"【好物精选】{title or '精选高性价比好物'}"]
    if price is not None and float(price) > 0:
        price_text = f"【券后特惠】¥{price}"
        if coupon is not None and float(coupon) > 0:
            price_text += f"（已立减 ¥{coupon}）"
        lines.append(price_text)
    destination = f"【{app_name}APP】" if app_name else "商品页面"
    lines.extend([f"【抢购口令】长按复制本段全部信息，打开{destination}立即领券下单！", final_link])
    return "\n".join(lines)


class BasePlatformAdapter(ABC):
    """
    电商导购联盟适配器抽象基类
    """
    platform_code: str = ""
    platform_name: str = ""
    active: bool = True
    channels: tuple = ()
    recommendation_limit: Optional[int] = None
    legacy_order_statuses: dict = {}

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None) -> ProductRef:
        """公共层不猜测非数字标识的含义；各平台自行解析其兼容输入。"""
        if not goods_id and not goods_sign:
            goods_id = goods_id_or_sign
        return ProductRef(self.platform_code, goods_id or "", goods_sign or "")

    def warmup(self) -> None:
        """可选的启动预热，不要求通用入口了解任何平台 SDK。"""

    def normalize_order(self, order: dict) -> dict:
        """平台应返回金额元、时间秒及明确退款/结算标志。"""
        return dict(order)

    def build_command_text(self, title, final_link, price=None, coupon=None) -> str:
        return build_command_text(title, final_link, price, coupon, self.platform_name)

    @abstractmethod
    def get_recommend_goods(
        self,
        channel_type: int = 5,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取官方推荐/热销榜单商品列表
        :param channel_type: 推荐频道代码
        :param offset: 分页偏移量
        :param limit: 每页数量
        :param uid: 推手/用户唯一UID
        :return: {"status": "success", "total_count": int, "data": List[Dict]}
        """
        pass

    @abstractmethod
    def search_goods(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        根据关键词搜索联盟商品
        :param keyword: 关键词
        :param page: 页码
        :param page_size: 每页条数
        :param uid: 推手/用户唯一UID
        :return: {"status": "success", "data": List[Dict]}
        """
        pass

    @abstractmethod
    def get_goods_detail(
        self,
        goods_id_or_sign: str = "",
        uid: Optional[str] = None,
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取官方商品完整规范化详情 (含全量实拍轮播图、满减优惠、店铺与服务保障)
        :param goods_id_or_sign: 商品ID或Sign
        :param uid: 用户UID
        :param goods_id: 商品纯数字ID
        :param goods_sign: 商品加密Sign
        :return: {"status": "success", "data": Dict}
        """
        pass

    @abstractmethod
    def convert_promotion_url(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        根据商品生成附带推手 UID 的推广转链
        :param goods_id_or_sign: 商品ID或加密商品Sign
        :param uid: 用户唯一识别码（用于后续订单归因与返利计算）
        :return: {"status": "success", "url": str, "short_url": str, "mobile_url": str, "we_app_info": dict, "raw": dict}
        """
        pass

    @abstractmethod
    def check_authority(self, uid: str) -> Dict[str, Any]:
        """
        检查指定用户在该平台的推广位备案/授权状态
        :param uid: 用户唯一识别码
        :return: {"status": "success", "is_authorized": bool, "bind": int}
        """
        pass

    @abstractmethod
    def generate_authority_url(self, uid: str, **kwargs) -> Dict[str, Any]:
        """
        生成该用户在该平台专属的备案授权链接
        :param uid: 用户唯一识别码
        :return: {"status": "success", "auth_url": str, "short_url": str, "we_app_info": dict}
        """
        pass

    @abstractmethod
    def fetch_increment_orders(
        self,
        start_time: int,
        end_time: int,
        page: int = 1,
        page_size: int = 50,
        **kwargs
    ) -> Dict[str, Any]:
        """
        拉取该平台在指定时间范围内的增量订单列表
        :param start_time: 起始时间戳 (秒)
        :param end_time: 截止时间戳 (秒)
        :param page: 页码
        :param page_size: 每页数量
        :return: {"status": "success", "order_list": List[Dict], "total_count": int}
        """
        pass


class UnavailablePlatformAdapter(BasePlatformAdapter):
    """未接入平台只保留兼容入口，不能生成假链接或假授权成功。"""
    active = False

    def _unavailable(self, *args, **kwargs):
        return {
            "status": "error",
            "error_code": "platform_unavailable",
            "error_msg": f"{self.platform_name}暂未接入",
            "data": [],
            "order_list": [],
            "total_count": 0,
            "is_authorized": False,
        }

    get_recommend_goods = _unavailable
    search_goods = _unavailable
    get_goods_detail = _unavailable
    convert_promotion_url = _unavailable
    check_authority = _unavailable
    generate_authority_url = _unavailable
    fetch_increment_orders = _unavailable

