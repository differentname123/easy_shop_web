# -*- coding: utf-8 -*-
"""
多平台联盟抽象适配器基类
定义统一的导购商品查询、搜索、推广生链、推手授权备案协议。
"""
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from infrastructure.api_governance import execute_provider_chain
from infrastructure.http_utils import error_dict
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
    电商导购联盟适配器抽象基类（8 大最小底层基础模块契约入口，详见 `mobile_web/adapters/README.md`）：
    ① get_recommend_goods        榜单推荐
    ② search_goods               全网关键词搜索
    ③ get_goods_detail / batch_get_goods_details  单品与 20 批量权威查卡
    ④ resolve_product_from_link  外部链接/口令/文案解析（API 优先、按需解包）
    ⑤ convert_promotion_url      专属推广转链（区分匿名站长 vs 个人归因）
    ⑥ resolve_and_convert_universal  万能转链组合唯一入口（④ + ⑤）
    ⑦ check_authority / generate_authority_url  推手渠道备案核验与引导码
    ⑧ fetch_increment_orders     增量返利订单同步
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
    batch_detail_includes_gallery: bool = True
    default_promoter_id: str = ""
    order_window_seconds: int = 23 * 3600
    capabilities = None

    def __init__(self) -> None:
        self._ensure_provider_state()

    def _ensure_provider_state(self) -> None:
        if not hasattr(self, "_provider_lock"):
            self._provider_lock = threading.Lock()
        if not hasattr(self, "_extra_providers"):
            self._extra_providers: Dict[str, List[Tuple[str, Callable[..., Dict[str, Any]], bool]]] = {}

    def register_provider(
        self,
        operation: str,
        name: str,
        fn: Callable[..., Dict[str, Any]],
        *,
        prepend: bool = False,
    ) -> None:
        """
        向指定平台能力挂载额外的第三方供应商实现（与对应 Adapter 公开方法同签名）：
        - `operation`: 'recommend' | 'search' | 'detail' | 'promotion' | 'universal' | 'check_authority' | 'generate_authority_url' | 'orders'
        - `prepend=True` 可将其设为首选主通道，原内置供应商自动退居备用。
        """
        self._ensure_provider_state()
        op = str(operation or "").strip().lower()
        with self._provider_lock:
            entries = self._extra_providers.setdefault(op, [])
            entries[:] = [item for item in entries if item[0] != name]
            entries.append((name, fn, bool(prepend)))
        refresh = getattr(self, "_refresh_capabilities", None)
        if callable(refresh):
            refresh()

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
        allow_persist: bool = True,
        metric_operation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """统一执行某能力的多供应商主备降级链（复用全局健康与配额治理）。"""
        op = str(operation or "").strip().lower()
        return execute_provider_chain(
            self.platform_code or "unknown",
            metric_operation or op,
            list(providers),
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

    def warmup(self) -> None:
        """可选的启动预热，不要求通用入口了解任何平台 SDK。"""

    def normalize_order(self, order: dict) -> dict:
        """平台应返回金额元、时间秒及明确退款/结算标志。"""
        return dict(order)

    def build_command_text(self, title, final_link, price=None, coupon=None) -> str:
        return build_command_text(title, final_link, price, coupon, self.platform_name)

    def _retry_on_off_promotion(
        self,
        call_fn: Callable[[], Dict[str, Any]],
        is_off_fn: Callable[[Dict[str, Any]], bool],
        *,
        delay: float = 0.15,
    ) -> Tuple[Dict[str, Any], bool]:
        """短暂退避重试一次，仅当二次响应仍明确符合下架/停推证据时才确认下架，避免网络抖动误杀。"""
        res = call_fn()
        if not is_off_fn(res):
            return res, False
        if delay > 0:
            time.sleep(delay)
        retry_res = call_fn()
        if not is_off_fn(retry_res):
            return retry_res, False
        return retry_res, True

    def resolve_and_convert_universal(self, raw_text: str, uid: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """万能转链与商品详情反查统一接口（默认提示平台暂不支持）。"""
        return error_dict(
            "UNSUPPORTED_OPERATION",
            f"{self.platform_name or '该平台'}暂不支持万能转链",
            input_type="unsupported",
            need_auth=False,
        )


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

    def batch_get_goods_details(
        self,
        refs: Sequence[ProductRef],
        **kwargs,
    ) -> Dict[str, Dict[str, Any]]:
        """
        底层最小模块 ③ 批量查卡契约（单批最多 20 条，按请求 key 精确对齐且坏项不连坐）：
        - 默认兜底：逐条调用 `get_goods_detail`；
        - TB (`item.info.upgrade`) / JD (`goods_query` / `HDK`) 返回完整多图轮播 (`batch_detail_includes_gallery = True`)；
        - PDD (`pdd.ddk.goods.search(goods_sign_list)`) 不返回 `goods_gallery_urls` (`batch_detail_includes_gallery = False`)，仅用于 20 批量实时价格/促销状态核验，不得直接用于详情页图库缓存与预取。
        :return: Dict[ref.key, {"status": "success"|"error", "data": ProductDetail, ...}]
        """
        outcomes: Dict[str, Dict[str, Any]] = {}
        for item_ref in refs or ():
            resolved = self.resolve_product_ref(
                goods_id_or_sign=item_ref.key,
                goods_id=item_ref.goods_id,
                goods_sign=item_ref.goods_sign,
            )
            key = resolved.key or item_ref.key
            if not key:
                continue
            if key in outcomes:
                continue
            outcomes[key] = self.get_goods_detail(
                goods_id_or_sign=resolved.goods_sign or resolved.goods_id,
                goods_id=resolved.goods_id or None,
                goods_sign=resolved.goods_sign or None,
                **kwargs,
            )
        return outcomes

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

