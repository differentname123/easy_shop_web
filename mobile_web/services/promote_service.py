# -*- coding: utf-8 -*-
"""按本平台有效授权分流；公共链接缓存与个人提示分别处理。"""
import math
import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Dict, Any, Optional
from adapters import get_adapter, PlatformUnavailableError
from adapters.base import build_command_text
from infrastructure.logging_utils import format_exc_summary
from models.product import ProductRef, clean_product_id, normalize_platform
from models.user import normalize_promoter_id
from repositories.product_repo import ProductRepository
from cache import PROMOTION_CACHE, MATERIAL_CACHE
from config import PROMOTION_LINK_TTL, logger

_UID_PATTERN = re.compile(r"^u_[0-9a-f]{8}$")
_PROMOTION_CACHE = PROMOTION_CACHE


class PromoteService:
    def __init__(self, product_repo: Optional[ProductRepository] = None, user_service=None):
        self.repo = product_repo if product_repo is not None else ProductRepository()
        self.user_service = user_service

    # 保留旧调用入口，但共享层不包含具体平台名称。
    build_command_text = staticmethod(build_command_text)

    def _resolve_product_ref(self, ref: ProductRef) -> ProductRef:
        if ref.goods_sign:
            try:
                doc = self.repo.get_product_identity(ref.goods_sign, platform=ref.platform)
                if doc and normalize_platform(doc.get("platform", ref.platform)) == ref.platform:
                    raw_gid = clean_product_id(doc.get("goods_id"))
                    if not raw_gid:
                        pid = clean_product_id(doc.get("product_id"))
                        if pid and pid != doc.get("goods_sign"):
                            raw_gid = pid
                    goods_id = raw_gid or (ref.goods_sign if ref.platform in ("taobao", "jd") else "")
                    return ProductRef(ref.platform, goods_id, ref.goods_sign)
            except Exception as exc:
                logger.warning(
                    "❌ 【转链身份解析降级】读取商品身份缓存时发生异常，已降级使用原始参数继续转链 | 平台[%s] Sign[%s] 异常<%s> 排查方向<请检查MongoDB连接或products集合状态>",
                    ref.platform, ref.goods_sign, format_exc_summary(exc),
                )
            if ref.platform in ("taobao", "jd"):
                val = ref.goods_sign or ref.goods_id
                return ProductRef(ref.platform, val, val)
            return ProductRef(ref.platform, "", ref.goods_sign)
        if ref.platform in ("taobao", "jd"):
            val = ref.goods_id or ref.goods_sign
            return ProductRef(ref.platform, val, val)
        return ref

    @staticmethod
    def _final_link(result: dict) -> str:
        for field in ("final_link", "mobile_short_url", "short_url", "mobile_url", "url"):
            value = result.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _cacheable(cls, result) -> bool:
        return (isinstance(result, dict) and result.get("status") == "success"
                and not result.get("is_fallback") and result.get("cacheable") is not False
                and bool(cls._final_link(result)))

    @staticmethod
    def _public_attribution(result: dict) -> bool:
        return (result.get("attribution_status") in (None, "anonymous") and not result.get("need_auth")
                and not any(result.get(field) for field in
                            ("uid", "external_id", "identity", "relation_id", "special_id", "attribution_identity")))

    @staticmethod
    def _valid_deadline(expires_at) -> bool:
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return False
        remaining = expires_at - time.time()
        return math.isfinite(remaining) and 0 < remaining <= PROMOTION_LINK_TTL

    @staticmethod
    def _link_data(result: dict) -> dict:
        data = deepcopy(result)
        for field in ("command_text", "_expires_at", "auth_platform", "auth_platform_name", "auth_msg", "authorization_status"):
            data.pop(field, None)
        return data

    def _with_command(self, data: dict, adapter, kwargs: dict) -> dict:
        result = self._link_data(data)
        result["final_link"] = self._final_link(result)
        coupon = kwargs.get("coupon_amount")
        if coupon is None:
            coupon = kwargs.get("coupon")
        result["command_text"] = adapter.build_command_text(
            kwargs.get("title") or kwargs.get("name") or kwargs.get("goods_name") or "精选高性价比好物",
            result["final_link"], kwargs.get("price"), coupon,
        )
        token = result.get("promotion_token")
        if isinstance(token, str) and token.strip():
            result["command_text"] += f"\n【平台口令】{token.strip()}"
        return result

    def _trusted_material_url(self, ref: ProductRef) -> str:
        if not ref.key:
            return ""
        cache_key = (ref.platform, "sign" if ref.goods_sign else "id", ref.key)
        cached = MATERIAL_CACHE.get(cache_key)
        if isinstance(cached, str) and cached:
            return cached
        try:
            item = self.repo.get_product_detail(ref.key, platform=ref.platform,
                                                key_type="sign" if ref.goods_sign else "id")
            if not isinstance(item, dict) or normalize_platform(item.get("platform")) != ref.platform:
                return ""
            if ref.platform != "pdd":
                val = str(item.get("goods_id") or item.get("product_id") or item.get("goods_sign") or "").strip()
                actual = ProductRef(ref.platform, val, val)
            else:
                actual = ProductRef(ref.platform, item.get("goods_id") or item.get("product_id"), item.get("goods_sign"))
            if (ref.goods_sign and ref.goods_sign != actual.goods_sign
                    or not ref.goods_sign and ref.goods_id != actual.goods_id):
                return ""
            value = item.get("product_url")
            return value.strip() if isinstance(value, str) else ""
        except Exception as exc:
            logger.warning(
                "❌ 【转链物料读取降级】从本地数据库读取商品可信物料链接失败，将尝试直接调用上游转链 | 平台[%s] 商品[%s] 异常<%s> 排查方向<请检查MongoDB连接状态>",
                ref.platform, ref.key, format_exc_summary(exc),
            )
            return ""

    def _get_link_data(
        self, ref: ProductRef, product_url: str, uid: str, identity: dict, adapter, kwargs: dict,
        trace: Optional[dict] = None,
    ) -> dict:
        kind = "sign" if ref.goods_sign else "id"
        cache_key = ("promotion", ref.platform, kind, ref.key, product_url, uid)
        if identity:
            cache_key += (hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),)
        configured_scope = getattr(adapter, "promotion_scope", "")
        configured_scope = configured_scope if isinstance(configured_scope, str) else ""
        if configured_scope:
            cache_key += (configured_scope,)

        def matches_scope(data):
            return (not configured_scope or data.get("promotion_scope") == configured_scope) and (
                getattr(adapter, "requires_numeric_promoter_id", False) is not True
                or data.get("promoter_id") == identity.get("promoter_id"))

        cached = PROMOTION_CACHE.get(cache_key)
        if (isinstance(cached, tuple) and len(cached) == 2 and self._valid_deadline(cached[0])
                and self._cacheable(cached[1]) and matches_scope(cached[1])
                and (uid or self._public_attribution(cached[1]))):
            if trace is not None:
                trace.update(source="memory", provider_ms=0.0)
            return self._link_data(cached[1])
        if cached is not None:
            PROMOTION_CACHE.pop(cache_key)

        ref = self._resolve_product_ref(ref)
        if not uid and ref.key:
            cached = self.repo.get_promotion_link_cache(
                platform=ref.platform, goods_id=ref.goods_id or None,
                goods_sign=ref.goods_sign or None, goods_key=ref.key,
            )
            if (self._cacheable(cached) and self._public_attribution(cached) and matches_scope(cached)
                    and self._valid_deadline(cached.get("_expires_at"))):
                expires_at = cached["_expires_at"]
                data = self._link_data(cached)
                PROMOTION_CACHE.set(cache_key, (expires_at, deepcopy(data)))
                if trace is not None:
                    trace.update(source="database", provider_ms=0.0)
                return data

        adapter_kwargs = {k: v for k, v in kwargs.items()
                          if k not in ("goods_id", "goods_sign", "identity", "relation_id", "special_id",
                                       "promoter_id", "positionId", "position_id", "trusted_product_url",
                                       "promotion_scope", "unionId", "union_id")}
        if getattr(adapter, "requires_authorized_promotion", False):
            adapter_kwargs["identity"] = identity
        if getattr(adapter, "requires_numeric_promoter_id", False) is True:
            adapter_kwargs["promoter_id"] = identity["promoter_id"]
        if getattr(adapter, "requires_material_url", False) is True:
            adapter_kwargs["trusted_product_url"] = self._trusted_material_url(ref)
        provider_started = time.perf_counter()
        try:
            result = adapter.convert_promotion_url(
                goods_id_or_sign=ref.key, uid=uid or None,
                goods_id=ref.goods_id or None, goods_sign=ref.goods_sign or None,
                product_url=product_url or None, **adapter_kwargs
            )
        finally:
            if trace is not None:
                trace.update(source="provider", provider_ms=(time.perf_counter() - provider_started) * 1000)
        if not isinstance(result, dict) or result.get("status") != "success":
            message = result.get("error_msg") if isinstance(result, dict) else None
            error = {"status": "error", "error_msg": message or "未能生成有效推广转链，商品可能已失效或缺少必要标识"}
            if isinstance(result, dict):
                error.update({key: result[key] for key in ("error_code", "need_auth", "auth_platform", "auth_msg", "attribution_status")
                              if key in result})
            return error

        data = self._link_data(result)
        if getattr(adapter, "requires_numeric_promoter_id", False) is True and data.get("promoter_id") != identity.get("promoter_id"):
            return {"status": "error", "error_code": "invalid_promotion_identity",
                    "error_msg": "平台推广结果与当前推广身份不一致"}
        if configured_scope:
            data["promotion_scope"] = configured_scope
        final_link = self._final_link(data)
        if not final_link:
            return {"status": "error", "error_msg": "平台未返回有效推广链接"}
        data["final_link"] = final_link
        if not uid:
            if not self._public_attribution(data):
                return {"status": "error", "error_code": "invalid_public_promotion",
                        "error_msg": "平台通用推广结果包含个人归因，已拒绝复用"}
        expires_at = time.time() + PROMOTION_LINK_TTL
        source_deadline = data.get("expires_at")
        if source_deadline is not None:
            if (isinstance(source_deadline, bool) or not isinstance(source_deadline, (int, float))
                    or not math.isfinite(source_deadline) or source_deadline <= time.time()):
                return {"status": "error", "error_msg": "平台返回的推广链接已过期或有效期无效"}
            expires_at = min(expires_at, source_deadline)
        if self._cacheable(data):
            PROMOTION_CACHE.set(cache_key, (expires_at, deepcopy(data)))
            if not uid and ref.key:
                self.repo.save_promotion_link_cache(
                    platform=ref.platform, data=deepcopy(data), goods_id=ref.goods_id or None,
                    goods_sign=ref.goods_sign or None, goods_key=ref.key, expires_at=expires_at,
                )
        return data

    def convert_link(
        self,
        platform: str = "pdd",
        goods_id_or_sign: str = "",
        product_url: Optional[str] = None,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        total_started = time.perf_counter()
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        if not adapter.active:
            raise PlatformUnavailableError(f"{adapter.platform_name}暂未接入")
        capabilities = getattr(adapter, "capabilities", None)
        if isinstance(capabilities, dict) and capabilities.get("promotion") is False:
            return {"status": "error", "error_code": "capability_unavailable",
                    "error_msg": f"{adapter.platform_name}推广转链尚未配置"}
        clean_uid = uid.strip() if isinstance(uid, str) else ""
        if clean_uid and not _UID_PATTERN.fullmatch(clean_uid):
            if getattr(adapter, "requires_numeric_promoter_id", False) is True:
                return {"status": "error", "error_code": "invalid_promoter_uid", "error_msg": "无效的登录推广身份"}
            clean_uid = ""
        resolved = adapter.resolve_product_ref(
            goods_id_or_sign, goods_id=kwargs.get("goods_id"), goods_sign=kwargs.get("goods_sign")
        )
        ref = ProductRef(plat, resolved.goods_id, resolved.goods_sign)
        product_url = str(product_url or "").strip()
        if not ref.key and not product_url:
            return {"status": "error", "error_msg": "缺少有效商品标识或链接"}

        goods_tag = ref.goods_id or (ref.goods_sign[:16] + "..." if len(ref.goods_sign) > 16 else ref.goods_sign) or "url"
        user_tag = clean_uid or "匿名"
        identity, context = {}, {}
        effective_uid = clean_uid
        need_auth = False
        auth_ms = 0.0
        if getattr(adapter, "requires_numeric_promoter_id", False) is True:
            auth_started = time.perf_counter()
            if clean_uid:
                if self.user_service is None:
                    from services.user_service import UserService
                    self.user_service = UserService()
                context = self.user_service.get_numeric_promotion_context(
                    clean_uid, plat, getattr(adapter, "default_promoter_id", ""))
                auth_ms = (time.perf_counter() - auth_started) * 1000
                if context.get("status") != "success":
                    logger.warning(
                        "❌ 【推广转链失败】获取推手数字推广位身份时失败 | 平台[%s] 商品[%s] 用户[%s] 耗时[%.1fms] 失败原因<%s> 排查方向<请检查用户账号状态或默认推广位配置>",
                        plat, goods_tag, user_tag, (time.perf_counter() - total_started) * 1000,
                        context.get("error_msg") or context.get("error_code") or "推广身份异常",
                    )
                    return context
                numeric_id = context.get("promoter_id")
            else:
                numeric_id = getattr(adapter, "default_promoter_id", "")
            try:
                identity = {"promoter_id": normalize_promoter_id(numeric_id)}
            except ValueError:
                logger.warning(
                    "❌ 【推广转链失败】平台数字推广位ID格式无效 | 平台[%s] 商品[%s] 用户[%s] 排查方向<请检查环境变量中默认推广位ID(如JD_DEFAULT_POSITION_ID)是否为纯数字>",
                    plat, goods_tag, user_tag,
                )
                return {"status": "error", "error_code": "invalid_promoter_id",
                        "error_msg": "平台推广身份尚未正确配置"}
        if clean_uid and getattr(adapter, "requires_authorized_promotion", False):
            if self.user_service is None:
                from services.user_service import UserService
                self.user_service = UserService()
            auth_started = time.perf_counter()
            context = self.user_service.get_promotion_context(clean_uid, plat, allow_remote=False)
            auth_ms = (time.perf_counter() - auth_started) * 1000
            if context.get("status") != "success":
                logger.warning(
                    "❌ 【推广转链失败】读取推手平台渠道授权状态时失败 | 平台[%s] 商品[%s] 用户[%s] 授权耗时[%.1fms] 失败原因<%s> 排查方向<请检查用户中心数据库或授权状态配置>",
                    plat, goods_tag, user_tag, auth_ms,
                    context.get("error_msg") or context.get("error_code") or "授权上下文读取失败",
                )
                return context
            if context.get("is_authorized") is True:
                identity = context.get("identity") or {}
            else:
                effective_uid = ""
                need_auth = True

        trace: Dict[str, Any] = {}
        data = self._get_link_data(ref, product_url, effective_uid, identity, adapter, kwargs, trace=trace)
        personal_fallback = False
        if (effective_uid and getattr(adapter, "requires_authorized_promotion", False)
                and data.get("status") == "error"):
            code = data.get("error_code")
            if code == "AUTH_REQUIRED" and data.get("need_auth") is True:
                # 策略 B: 乐观重试/无损兜底。不在转链失败时物理注销数据库中的授权信息，防止网络抖动误伤推手；仅在本次转链降级为通用链接并提示
                context = {"authorization_status": "unauthorized"}
                need_auth = True
            elif code != "PERSONAL_PROMOTION_UNAVAILABLE":
                total_ms = (time.perf_counter() - total_started) * 1000
                logger.warning(
                    "❌ 【推广转链失败】正在生成个人专属推广链接时失败 | 平台[%s] 商品[%s] 用户[%s] 模式[personal] 来源[%s] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms] 失败原因<%s> 排查方向<请检查商品是否在推、PID/RelationId配置或上游联盟转链接口状态>",
                    plat, goods_tag, user_tag, trace.get("source", "provider"), auth_ms,
                    trace.get("provider_ms", 0.0), total_ms,
                    data.get("error_msg") or code or "未知错误",
                )
                return data
            personal_fallback = True
            fallback_trace: Dict[str, Any] = {}
            data = self._get_link_data(ref, product_url, "", {}, adapter, kwargs, trace=fallback_trace)
            trace["source"] = fallback_trace.get("source", trace.get("source", "provider"))
            trace["provider_ms"] = float(trace.get("provider_ms", 0.0)) + float(fallback_trace.get("provider_ms", 0.0))

        total_ms = (time.perf_counter() - total_started) * 1000
        source = trace.get("source", "provider")
        provider_ms = float(trace.get("provider_ms", 0.0))
        if data.get("status") != "success":
            logger.warning(
                "❌ 【推广转链失败】正在生成商品推广链接时失败 | 平台[%s] 商品[%s] 用户[%s] 模式[%s] 来源[%s] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms] 失败原因<%s> 排查方向<请检查商品是否在推、物料链接有效性或上游联盟转链网关状态>",
                plat, goods_tag, user_tag, "personal" if effective_uid else "public",
                source, auth_ms, provider_ms, total_ms,
                data.get("error_msg") or data.get("error_code") or "未知错误",
            )
            return data

        result = self._with_command(data, adapter, kwargs)
        if need_auth:
            authorization_status = context.get("authorization_status", "unauthorized")
            if personal_fallback:
                message = f"{adapter.platform_name}渠道授权未生效或已失效，请前往我的页重新校验"
            else:
                message = (f"您尚未在{adapter.platform_name}完成渠道授权备案" if authorization_status == "unauthorized"
                           else f"{adapter.platform_name}授权状态待确认，请前往我的页重新校验")
            result.update(need_auth=True, is_fallback=True, attribution_status="anonymous",
                          auth_platform=plat, auth_platform_name=adapter.platform_name,
                          authorization_status=authorization_status,
                          auth_msg=f"{message}；当前为通用链接，无法计入本人推广佣金。")
            logger.info(
                "【推广转链】平台[%s] 商品[%s] 用户[%s] 模式[fallback_public] 来源[%s] 结果[降级通用链] 授权状态[%s] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms]",
                plat, goods_tag, user_tag, source, authorization_status, auth_ms, provider_ms, total_ms,
            )
        elif personal_fallback:
            result.update(is_fallback=True, attribution_status="anonymous")
            logger.info(
                "【推广转链】平台[%s] 商品[%s] 用户[%s] 模式[fallback_public] 来源[%s] 结果[降级通用链] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms]",
                plat, goods_tag, user_tag, source, auth_ms, provider_ms, total_ms,
            )
        else:
            logger.info(
                "【推广转链】平台[%s] 商品[%s] 用户[%s] 模式[%s] 来源[%s] 结果[成功] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms]",
                plat, goods_tag, user_tag, "personal" if effective_uid else "public",
                source, auth_ms, provider_ms, total_ms,
            )
        return result
