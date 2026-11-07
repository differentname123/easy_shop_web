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
from models.product import (
    ProductRef,
    clean_product_id,
    detect_platform_signal,
    extract_canonical_goods_id,
    is_jd_image_key,
    normalize_platform,
)
from models.user import normalize_promoter_id
from repositories.product_repo import ProductRepository
from cache import PROMOTION_CACHE, MATERIAL_CACHE, SIGN_BY_ID_CACHE, evict_product_from_caches
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
        if ref.platform in ("taobao", "jd") and ref.goods_id:
            if not ref.goods_sign or ref.goods_sign == ref.goods_id or (ref.platform == "jd" and is_jd_image_key(ref.goods_sign)):
                cached_sign = SIGN_BY_ID_CACHE.get((ref.platform, ref.goods_id))
                if isinstance(cached_sign, str) and cached_sign.strip():
                    ref = ProductRef(ref.platform, ref.goods_id, cached_sign.strip())
        if ref.goods_sign:
            try:
                doc = self.repo.get_product_identity(ref.goods_sign, platform=ref.platform)
                if doc and normalize_platform(doc.get("platform", ref.platform)) == ref.platform:
                    raw_gid = clean_product_id(doc.get("goods_id"))
                    if not raw_gid:
                        pid = clean_product_id(doc.get("product_id"))
                        if pid and pid != doc.get("goods_sign"):
                            raw_gid = pid
                    goods_id = (
                        raw_gid
                        or ref.goods_id
                        or (extract_canonical_goods_id(ref.platform, ref.goods_sign) if ref.platform in ("taobao", "jd") else "")
                    )
                    doc_sign = str(doc.get("goods_sign") or "").strip() or ref.goods_sign
                    return ProductRef(ref.platform, goods_id, doc_sign)
            except Exception as exc:
                logger.warning(
                    "❌ 【转链身份解析降级】读取商品身份缓存时发生异常，已降级使用原始参数继续转链 | 平台[%s] Sign[%s] 异常<%s> 排查方向<请检查MongoDB连接或products集合状态>",
                    ref.platform, ref.goods_sign, format_exc_summary(exc),
                )
            if ref.platform in ("taobao", "jd"):
                sign = ref.goods_sign or ref.goods_id
                gid = ref.goods_id or extract_canonical_goods_id(ref.platform, sign)
                return ProductRef(ref.platform, gid, sign)
            return ProductRef(ref.platform, "", ref.goods_sign)
        if ref.platform in ("taobao", "jd"):
            sign = ref.goods_sign or ref.goods_id
            gid = extract_canonical_goods_id(ref.platform, ref.goods_id or sign)
            return ProductRef(ref.platform, gid, sign)
        return ref

    @staticmethod
    def _final_link(result: dict) -> str:
        for field in ("short_url", "mobile_short_url", "final_link", "mobile_url", "url"):
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
        token = result.get("promotion_token")
        has_token = isinstance(token, str) and bool(token.strip())
        link_for_text = "" if (has_token and len(result["final_link"]) > 120) else result["final_link"]
        result["command_text"] = adapter.build_command_text(
            kwargs.get("title") or kwargs.get("name") or kwargs.get("goods_name") or "精选高性价比好物",
            link_for_text, kwargs.get("price"), coupon,
        )
        if has_token:
            result["command_text"] += f"\n【平台口令】{token.strip()}"
        return result

    def _trusted_material_url(self, ref: ProductRef) -> str:
        if not ref.key:
            return ""
        kind = "sign" if ref.goods_sign else "id"
        cache_key = (ref.platform, kind, ref.key)
        cached = MATERIAL_CACHE.get(cache_key)
        if not (isinstance(cached, str) and cached) and ref.goods_id:
            cached = MATERIAL_CACHE.get((ref.platform, "id", ref.goods_id))
        if isinstance(cached, str) and cached:
            if ref.platform != "jd" or "jingfen.jd.com" in cached:
                return cached
        # 若 DETAIL_CACHE 中已有详情页最新拉取的权威快照（如 jingfen.jd.com），优先采用并刷新 MATERIAL_CACHE
        try:
            from cache import DETAIL_CACHE
            for dkey in ref.cache_keys():
                entry = DETAIL_CACHE.get(dkey)
                if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], dict):
                    detail_url = (entry[1].get("data") or {}).get("product_url")
                    if isinstance(detail_url, str) and detail_url.strip():
                        clean_detail_url = detail_url.strip()
                        MATERIAL_CACHE.set(cache_key, clean_detail_url)
                        return clean_detail_url
        except Exception:
            pass
        if isinstance(cached, str) and cached:
            return cached
        try:
            item = self.repo.get_product_detail(ref.key, platform=ref.platform,
                                                key_type=kind)
            if not isinstance(item, dict) and ref.goods_sign and ref.goods_id and ref.platform in ("taobao", "jd"):
                item = self.repo.get_product_detail(ref.goods_id, platform=ref.platform,
                                                    key_type="id")
            if not isinstance(item, dict) or normalize_platform(item.get("platform")) != ref.platform:
                return ""
            if ref.platform != "pdd":
                gid = str(item.get("goods_id") or item.get("product_id") or "").strip()
                sign = str(item.get("goods_sign") or gid).strip()
                actual = ProductRef(ref.platform, gid or sign, sign or gid)
            else:
                actual = ProductRef(ref.platform, item.get("goods_id") or item.get("product_id"), item.get("goods_sign"))
            if ref.goods_sign and ref.goods_sign != actual.goods_sign:
                if not (ref.platform in ("taobao", "jd") and ref.goods_id and ref.goods_id == actual.goods_id):
                    return ""
            elif not ref.goods_sign and ref.goods_id != actual.goods_id:
                return ""
            value = item.get("product_url")
            if isinstance(value, str) and value.strip():
                clean_val = value.strip()
                if (ref.platform == "jd" and clean_val.startswith("https://item.jd.com/")
                        and clean_val.endswith(".html")):
                    cand_id = clean_val[len("https://item.jd.com/"):-len(".html")]
                    if len(cand_id) >= 25 and "_" in cand_id and not is_jd_image_key(cand_id):
                        clean_val = f"https://jingfen.jd.com/detail/{cand_id}.html"
                MATERIAL_CACHE.set(cache_key, clean_val)
                return clean_val
            return ""
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
        err_resp, effective_uid, identity, context, need_auth, auth_ms = self._resolve_user_promotion_context(
            plat, adapter, clean_uid, goods_tag=goods_tag, total_started=total_started, log_errors=True
        )
        if err_resp is not None:
            return err_resp

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
            if str(data.get("error_code") or "").upper() == "PRODUCT_OFF_PROMOTION" and ref.key:
                evict_product_from_caches(plat, ref.goods_id, ref.goods_sign)
                delete_fn = getattr(self.repo, "delete_product", None)
                if callable(delete_fn):
                    try:
                        delete_fn(plat, goods_id=ref.goods_id or None, goods_sign=ref.goods_sign or None)
                    except Exception:
                        pass
            logger.warning(
                "❌ 【推广转链失败】正在生成商品推广链接时失败 | 平台[%s] 商品[%s] 用户[%s] 模式[%s] 来源[%s] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms] 失败原因<%s> 排查方向<请检查商品是否在推、物料链接有效性或上游联盟转链网关状态>",
                plat, goods_tag, user_tag, "personal" if effective_uid else "public",
                source, auth_ms, provider_ms, total_ms,
                data.get("error_msg") or data.get("error_code") or "未知错误",
            )
            return data

        result = self._with_command(data, adapter, kwargs)
        self._decorate_auth_fallback(
            result, plat, adapter, context, need_auth=need_auth, personal_fallback=personal_fallback, hint_location="我的页"
        )
        if need_auth:
            logger.info(
                "【推广转链】平台[%s] 商品[%s] 用户[%s] 模式[fallback_public] 来源[%s] 结果[降级通用链] 授权状态[%s] 授权耗时[%.1fms] 转链耗时[%.1fms] 总耗时[%.1fms]",
                plat, goods_tag, user_tag, source, result.get("authorization_status", "unauthorized"), auth_ms, provider_ms, total_ms,
            )
        elif personal_fallback:
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

    def _ensure_user_service(self):
        if self.user_service is None:
            from services.user_service import UserService
            self.user_service = UserService()
        return self.user_service

    def _resolve_user_promotion_context(
        self,
        plat: str,
        adapter: Any,
        clean_uid: str,
        *,
        goods_tag: str = "url",
        total_started: float = 0.0,
        log_errors: bool = False,
    ):
        """统一解析推手推广位身份（数字 position_id 制或渠道授权备案制）。"""
        user_tag = clean_uid or "匿名"
        identity: Dict[str, Any] = {}
        context: Dict[str, Any] = {}
        effective_uid = clean_uid
        need_auth = False
        auth_ms = 0.0

        if getattr(adapter, "requires_numeric_promoter_id", False) is True:
            auth_started = time.perf_counter()
            if clean_uid:
                context = self._ensure_user_service().get_numeric_promotion_context(
                    clean_uid, plat, getattr(adapter, "default_promoter_id", "")
                )
                auth_ms = (time.perf_counter() - auth_started) * 1000
                if context.get("status") != "success":
                    if log_errors:
                        logger.warning(
                            "❌ 【推广转链失败】获取推手数字推广位身份时失败 | 平台[%s] 商品[%s] 用户[%s] 耗时[%.1fms] 失败原因<%s> 排查方向<请检查用户账号状态或默认推广位配置>",
                            plat, goods_tag, user_tag, (time.perf_counter() - total_started) * 1000,
                            context.get("error_msg") or context.get("error_code") or "推广身份异常",
                        )
                    return context, effective_uid, identity, context, need_auth, auth_ms
                numeric_id = context.get("promoter_id")
            else:
                numeric_id = getattr(adapter, "default_promoter_id", "")
            try:
                identity = {"promoter_id": normalize_promoter_id(numeric_id)}
            except ValueError:
                if log_errors:
                    logger.warning(
                        "❌ 【推广转链失败】平台数字推广位ID格式无效 | 平台[%s] 商品[%s] 用户[%s] 排查方向<请检查环境变量中默认推广位ID(如JD_DEFAULT_POSITION_ID)是否为纯数字>",
                        plat, goods_tag, user_tag,
                    )
                err = {"status": "error", "error_code": "invalid_promoter_id", "error_msg": "平台推广身份尚未正确配置"}
                return err, effective_uid, identity, context, need_auth, auth_ms

        if clean_uid and getattr(adapter, "requires_authorized_promotion", False):
            auth_started = time.perf_counter()
            context = self._ensure_user_service().get_promotion_context(clean_uid, plat, allow_remote=False)
            auth_ms = (time.perf_counter() - auth_started) * 1000
            if context.get("status") != "success":
                if log_errors:
                    logger.warning(
                        "❌ 【推广转链失败】读取推手平台渠道授权状态时失败 | 平台[%s] 商品[%s] 用户[%s] 授权耗时[%.1fms] 失败原因<%s> 排查方向<请检查用户中心数据库或授权状态配置>",
                        plat, goods_tag, user_tag, auth_ms,
                        context.get("error_msg") or context.get("error_code") or "授权上下文读取失败",
                    )
                return context, effective_uid, identity, context, need_auth, auth_ms
            if context.get("is_authorized") is True:
                identity = context.get("identity") or {}
            else:
                effective_uid = ""
                need_auth = True

        return None, effective_uid, identity, context, need_auth, auth_ms

    @staticmethod
    def _decorate_auth_fallback(
        result: Dict[str, Any],
        plat: str,
        adapter: Any,
        context: Dict[str, Any],
        *,
        need_auth: bool,
        personal_fallback: bool,
        hint_location: str = "我的页",
    ) -> None:
        """统一装配未备案/降级通用链的前端提示元数据。"""
        if need_auth:
            authorization_status = context.get("authorization_status", "unauthorized")
            if personal_fallback:
                message = f"{adapter.platform_name}渠道授权未生效或已失效，请前往{hint_location}重新校验"
            else:
                message = (
                    f"您尚未在{adapter.platform_name}完成渠道授权备案"
                    if authorization_status == "unauthorized"
                    else f"{adapter.platform_name}授权状态待确认，请前往{hint_location}重新校验"
                )
            result.update(
                need_auth=True,
                is_fallback=True,
                attribution_status="anonymous",
                auth_platform=plat,
                auth_platform_name=adapter.platform_name,
                authorization_status=authorization_status,
                auth_msg=f"{message}；当前为通用链接，无法计入本人推广佣金。",
            )
        elif personal_fallback:
            result.update(is_fallback=True, attribution_status="anonymous")

    @staticmethod
    def detect_platform_from_text(raw_text: str) -> str:
        """
        根据用户粘贴的链接、口令或商品标识智能识别所属电商平台 ('pdd' / 'jd' / 'taobao')。
        """
        return detect_platform_signal(raw_text) or "pdd"

    def _seed_resolved_product(self, platform: str, product: Optional[dict], uid: Optional[str] = None) -> None:
        """将万能转链反查到的商品详情写入 DETAIL_CACHE、MATERIAL_CACHE 与本地商品库，确保点击卡片进详情页秒开。"""
        if not isinstance(product, dict):
            return
        gid = clean_product_id(product.get("goods_id"))
        gsign = str(product.get("goods_sign") or "").strip()
        if not gid and not gsign:
            return
        snapshot = None
        try:
            from datetime import datetime, timezone
            from models.product import utc_millis
            from services.product_service import ProductService
            snapshot = ProductService._make_snapshot(
                product,
                utc_millis(datetime.fromtimestamp(time.time(), timezone.utc)),
                platform,
            )
            ProductService._cache_detail(
                platform,
                {"status": "success", "data": deepcopy(snapshot.data)},
                ProductService._snapshot_deadline(snapshot),
                uid=uid,
            )
        except Exception:
            pass

    def universal_convert(
        self,
        raw_text: str,
        platform: str = "auto",
        uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        万能转链与商品详情反查统一编排入口：
        支持粘贴淘宝/京东/拼多多的商品链接、推广短链、二合一长链、淘口令、京口令或分享文案，
        自动识别平台（或按指定平台），生成当前登录推手专属（或未备案降级通用）推广链接/口令，
        并返回规范化商品卡片以供前端直接预览、复制口令、领券购买或进入详情页。
        """
        total_started = time.perf_counter()
        text = str(raw_text or "").strip()
        if not text:
            return {"status": "error", "error_code": "INVALID_ARGUMENT", "error_msg": "请粘贴需要转链的商品链接、推广链接或口令文案"}

        lower = text.lower()
        if re.search(r"https?://", lower) and not any(
            d in lower
            for d in (
                "jd.com", "3.cn", "jingxi.com",
                "tb.cn", "taobao.com", "tmall.com", "alimama.com",
                "pinduoduo.com", "yangkeduo.com",
            )
        ):
            return {
                "status": "error",
                "error_code": "UNSUPPORTED_URL_DOMAIN",
                "error_msg": "仅支持淘宝/天猫、京东、拼多多的商品链接或推广短链，请检查粘贴的链接是否正确",
            }

        req_plat = str(platform or "auto").strip().lower()
        is_auto = req_plat in ("", "auto")
        if is_auto:
            signal_plat = detect_platform_signal(text)
            if not signal_plat:
                return {
                    "status": "error",
                    "error_code": "AMBIGUOUS_PLATFORM_TEXT",
                    "error_msg": "未能从文本中识别出所属平台特征，请在上方选择目标平台（拼多多/淘宝/京东）或粘贴完整分享链接/口令",
                }
            detected_plat = signal_plat
        else:
            detected_plat = normalize_platform(req_plat)

        clean_uid = uid.strip() if isinstance(uid, str) else ""
        if clean_uid and not _UID_PATTERN.fullmatch(clean_uid):
            clean_uid = ""

        # 第一性原理：确定性单平台路由，禁止在某平台转链失败后盲目跨平台回退到其他平台全文检索造成张冠李戴
        candidate_platforms = [detected_plat]

        last_error: Dict[str, Any] = {"status": "error", "error_msg": "未能识别或转换该内容，请检查链接或口令是否有效"}
        for plat in candidate_platforms:
            try:
                adapter = get_adapter(plat)
            except Exception:
                continue
            if not getattr(adapter, "active", False):
                last_error = {"status": "error", "error_code": "platform_unavailable", "error_msg": f"{adapter.platform_name}暂未接入"}
                continue
            resolve_fn = getattr(adapter, "resolve_and_convert_universal", None)
            if not callable(resolve_fn):
                continue

            err_resp, effective_uid, identity, context, need_auth, _ = self._resolve_user_promotion_context(
                plat, adapter, clean_uid, total_started=total_started, log_errors=False
            )
            if err_resp is not None:
                return err_resp

            adapter_kwargs: Dict[str, Any] = {}
            if getattr(adapter, "requires_authorized_promotion", False):
                adapter_kwargs["identity"] = identity
            if getattr(adapter, "requires_numeric_promoter_id", False) is True:
                adapter_kwargs["promoter_id"] = identity["promoter_id"]

            data = resolve_fn(text, uid=effective_uid or None, **adapter_kwargs)
            personal_fallback = False
            if (
                effective_uid
                and getattr(adapter, "requires_authorized_promotion", False)
                and isinstance(data, dict)
                and data.get("status") == "error"
            ):
                code = data.get("error_code")
                if code == "AUTH_REQUIRED" and data.get("need_auth") is True:
                    context = {"authorization_status": "unauthorized"}
                    need_auth = True
                    personal_fallback = True
                elif code == "PERSONAL_PROMOTION_UNAVAILABLE":
                    personal_fallback = True
                if personal_fallback:
                    data = resolve_fn(text, uid=None, identity={})

            if not isinstance(data, dict) or data.get("status") != "success":
                last_error = data if isinstance(data, dict) else last_error
                # 若是明确的数字 SKU 不支持或已下架，直接返回给用户，不再盲目尝试其他平台
                if isinstance(data, dict) and data.get("error_code") in (
                    "UNSUPPORTED_JD_NUMERIC_SKU",
                    "UNSUPPORTED_PDD_ID_ONLY_URL",
                    "PRODUCT_OFF_PROMOTION",
                ):
                    break
                continue

            product = data.get("product") if isinstance(data.get("product"), dict) else None
            if product:
                self._seed_resolved_product(plat, product, uid=clean_uid or None)

            cmd_kwargs = {
                "title": data.get("title") or (product or {}).get("name") or "精选高性价比好物",
                "price": data.get("price") if data.get("price") is not None else (product or {}).get("price"),
                "coupon_amount": data.get("coupon_amount") if data.get("coupon_amount") is not None else (product or {}).get("coupon_amount"),
            }
            result = self._with_command(data, adapter, cmd_kwargs)
            result["platform"] = plat
            result["platform_name"] = adapter.platform_name
            if product:
                result["product"] = product

            self._decorate_auth_fallback(
                result, plat, adapter, context, need_auth=need_auth, personal_fallback=personal_fallback, hint_location="上方授权区"
            )

            total_ms = (time.perf_counter() - total_started) * 1000
            logger.info(
                "【万能转链】平台[%s] 识别模式[%s] 用户[%s] 商品[%s] 结果[成功] 总耗时[%.1fms]",
                plat,
                "auto" if is_auto else "manual",
                clean_uid or "匿名",
                (product or {}).get("goods_id") or data.get("goods_id") or "url",
                total_ms,
            )
            return result

        total_ms = (time.perf_counter() - total_started) * 1000
        logger.warning(
            "❌ 【万能转链失败】平台[%s] 用户[%s] 总耗时[%.1fms] 失败原因<%s>",
            detected_plat,
            clean_uid or "匿名",
            total_ms,
            last_error.get("error_msg") or last_error.get("error_code") or "未知错误",
        )
        return last_error

