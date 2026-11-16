# -*- coding: utf-8 -*-
"""
多平台联盟抽象适配器基类
定义统一的导购商品查询、搜索、推广生链、推手授权备案协议。
"""
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from infrastructure.api_governance import execute_provider_chain, rotate_providers
from models.product import ProductRef


def build_command_text(title: str, final_link: str, price=None, coupon=None, app_name: str = "") -> str:
    """共享文案结构；具体平台名称由适配器传入。"""
    lines = [f"【好物精选】{title or '精选高性价比好物'}"]
    if price is not None and float(price) > 0:
        has_coupon = coupon is not None and float(coupon) > 0
        if has_coupon:
            lines.append(f"【券后特惠】¥{price}（已立减 ¥{coupon}）")
        else:
            lines.append(f"【特惠参考价】¥{price}")
    destination = f"【{app_name}APP】" if app_name else "商品页面"
    lines.append(f"【抢购口令】长按复制本段全部信息，打开{destination}立即领券下单！")
    if final_link:
        lines.append(final_link)
    return "\n".join(lines)


DEFAULT_HOT_CHANNELS = (
    {"channel_type": 5, "name": "实时销量榜", "icon": "fire"},
    {"channel_type": 1, "name": "今日销量榜", "icon": "crown"},
)


class BasePlatformAdapter(ABC):
    """
    电商导购联盟适配器抽象基类
    """
    platform_code: str = ""
    platform_name: str = ""
    active: bool = True
    channels: tuple = DEFAULT_HOT_CHANNELS
    recommendation_limit: Optional[int] = None
    required_identity_fields: tuple = ()
    requires_authorized_promotion: bool = False
    requires_numeric_promoter_id: bool = False
    requires_material_url: bool = False
    supports_coupon_enrichment: bool = False
    preserves_known_coupons_across_rank: bool = False
    default_promoter_id: str = ""
    promotion_scope: str = ""
    order_window_seconds: int = 23 * 3600
    capabilities = None

    def _ensure_provider_state(self) -> None:
        if not hasattr(self, "_provider_lock"):
            self._provider_lock = threading.Lock()
        if not hasattr(self, "_extra_providers"):
            self._extra_providers: Dict[str, List[Tuple[str, Callable[..., Dict[str, Any]], bool]]] = {}
        if not hasattr(self, "_provider_strategies"):
            self._provider_strategies: Dict[str, str] = {}
        if not hasattr(self, "_rr_cursors"):
            self._rr_cursors: Dict[str, int] = {}

    def register_provider(
        self,
        operation: str,
        name: str,
        fn: Callable[..., Dict[str, Any]],
        *,
        prepend: bool = False,
        strategy: Optional[str] = None,
    ) -> None:
        """
        向指定平台能力挂载额外的第三方供应商实现（与对应 Adapter 公开方法同签名）：
        - `operation`: 'recommend' | 'search' | 'detail' | 'promotion' | 'universal' | 'check_authority' | 'generate_authority_url' | 'orders'
        - `prepend=True` 可将其设为首选主通道，原内置供应商自动退居备用；
        - `strategy`: 可选 'failover'（默认主备降级）或 'round_robin'（轮询分摊 + 单次故障顺延）。
        """
        self._ensure_provider_state()
        op = str(operation or "").strip().lower()
        with self._provider_lock:
            entries = self._extra_providers.setdefault(op, [])
            entries[:] = [item for item in entries if item[0] != name]
            entries.append((name, fn, bool(prepend)))
            if strategy in ("failover", "round_robin"):
                self._provider_strategies[op] = strategy
        refresh = getattr(self, "_refresh_capabilities", None)
        if callable(refresh):
            refresh()

    def set_provider_strategy(self, operation: str, strategy: str = "failover") -> None:
        """切换某项能力的多供应商调度策略：'failover' 或 'round_robin'。"""
        if strategy not in ("failover", "round_robin"):
            raise ValueError("strategy must be 'failover' or 'round_robin'")
        self._ensure_provider_state()
        op = str(operation or "").strip().lower()
        with self._provider_lock:
            self._provider_strategies[op] = strategy

    def _has_extra_providers(self, operation: str) -> bool:
        self._ensure_provider_state()
        return bool(self._extra_providers.get(str(operation or "").strip().lower()))

    def _compose_providers(
        self,
        operation: str,
        base_providers: Sequence[Tuple[str, Callable[[], Dict[str, Any]]]],
        **call_kwargs: Any,
    ) -> List[Tuple[str, Callable[[], Dict[str, Any]]]]:
        """将内置供应商与动态注册的第三方供应商拼装为标准化零参调用链。"""
        self._ensure_provider_state()
        op = str(operation or "").strip().lower()
        with self._provider_lock:
            extras = list(self._extra_providers.get(op, ()))
        front = [(name, (lambda f=fn, kw=call_kwargs: f(**kw))) for name, fn, prepend in extras if prepend]
        back = [(name, (lambda f=fn, kw=call_kwargs: f(**kw))) for name, fn, prepend in extras if not prepend]
        return [*front, *base_providers, *back]

    def _execute_capability(
        self,
        operation: str,
        providers: Sequence[Tuple[str, Callable[[], Dict[str, Any]]]],
        *,
        strategy: Optional[str] = None,
        allow_persist: bool = True,
        metric_operation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """统一执行某能力的多供应商链（支持主备降级与轮询分流，复用全局健康与配额治理）。"""
        self._ensure_provider_state()
        op = str(operation or "").strip().lower()
        with self._provider_lock:
            eff_strategy = strategy or self._provider_strategies.get(op, "failover")
            ordered = list(providers)
            if eff_strategy == "round_robin" and len(ordered) > 1:
                idx = self._rr_cursors.get(op, 0)
                self._rr_cursors[op] = (idx + 1) % len(ordered)
                ordered = rotate_providers(ordered, idx)
        return execute_provider_chain(
            self.platform_code or "unknown",
            metric_operation or op,
            ordered,
            allow_persist=allow_persist,
        )

    def _available(self, capability: str, message: str = "", **extra: Any) -> Optional[Dict[str, Any]]:
        """统一的平台启用与子能力配置守卫。"""
        if not self.active:
            return {
                "status": "error",
                "error_code": "platform_unavailable",
                "error_msg": f"{self.platform_name}暂未启用",
                **extra,
            }
        if self.capabilities is not None and not self.capabilities.get(capability):
            return {
                "status": "error",
                "error_code": "CONFIG_ERROR",
                "error_msg": message or f"{self.platform_name}该功能缺少必要配置",
                **extra,
            }
        return None

    def resolve_product_ref(self, goods_id_or_sign="", goods_id=None, goods_sign=None) -> ProductRef:
        """公共层不猜测非数字标识的含义；各平台自行解析其兼容输入。"""
        if not goods_id and not goods_sign:
            goods_id = goods_id_or_sign
        return ProductRef(self.platform_code, goods_id or "", goods_sign or "")

    def can_prefetch_detail(self, ref: ProductRef) -> bool:
        """后台公开详情的资格判断；不改变前台按需查询能力。"""
        return bool(ref.key)

    def normalize_material_url(self, url: str) -> str:
        """平台物料链接规范化（默认原样透传）。"""
        return str(url or "").strip()

    def is_authoritative_material_url(self, url: str) -> bool:
        """判断缓存中的物料链接是否为平台权威链接（防止低优先级摘要覆写）。"""
        return bool(str(url or "").strip())

    def extract_trusted_coupon_url(self, doc: Optional[dict]) -> str:
        """从本地摘要/详情记录中提取经平台校验的官方优惠券链接。"""
        return ""

    def should_enrich_promotion_coupon(self, ref: ProductRef, local_doc: Optional[dict]) -> bool:
        """判断点击转链前是否需要单次补查详情优惠券。"""
        return False

    def needs_coupon_upgrade(self, universal_result: dict, product: Optional[dict]) -> bool:
        """判断万能转链后是否需要叠加优惠券进行二合一升级转链。"""
        return False

    def skip_prefetch_if_detail_checked(self, ref: ProductRef, item: Optional[dict]) -> Optional[float]:
        """若平台对已拉取过详情的商品无需重复刷新，返回冷却秒数，否则返回 None。"""
        return None

    def warmup(self) -> None:
        """可选的启动预热，不要求通用入口了解任何平台 SDK。"""

    def normalize_order(self, order: dict) -> dict:
        """平台应返回金额元、时间秒及明确退款/结算标志。"""
        return dict(order)

    def build_command_text(self, title, final_link, price=None, coupon=None) -> str:
        return build_command_text(title, final_link, price, coupon, self.platform_name)

    def resolve_and_convert_universal(self, raw_text: str, uid: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """万能转链与商品详情反查统一接口（默认提示平台暂不支持）。"""
        return self._universal_error(
            "unsupported",
            f"{self.platform_name or '该平台'}暂不支持万能转链",
            error_code="UNSUPPORTED_OPERATION",
        )

    def _universal_error(
        self,
        input_type: str,
        error_msg: str,
        *,
        error_code: Optional[str] = None,
        need_auth: bool = False,
        product: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "status": "error",
            "input_type": input_type,
            "need_auth": bool(need_auth),
            "error_code": error_code,
            "error_msg": error_msg,
        }
        if product is not None:
            res["product"] = product
        return res

    def _universal_success(
        self,
        input_type: str,
        promotion: Dict[str, Any],
        product: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "status": "success",
            "platform": self.platform_code,
            "input_type": input_type,
            "promotion": promotion,
            "product": product or {},
        }


    @abstractmethod
    def get_recommend_goods(
        self,
        channel_type: int = 5,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        detail_sink=None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取官方推荐/热销榜单商品列表
        :param channel_type: 推荐频道代码
        :param offset: 分页偏移量
        :param limit: 每页数量
        :param uid: 推手/用户唯一UID
        :param detail_sink: 可选内部纯内存回调 sink(DetailSnapshot)，仅发布整页校验通过、
                            与卡片身份一致的白名单公开详情；fetched_at 是实际来源时间。
                            不转发供应商，不把完整详情或内部字段放入列表返回值；
                            不支持复用详情的平台可忽略，无回调不得增加详情工作。
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
    channels = ()

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
    resolve_and_convert_universal = _unavailable
    check_authority = _unavailable
    generate_authority_url = _unavailable
    fetch_increment_orders = _unavailable

