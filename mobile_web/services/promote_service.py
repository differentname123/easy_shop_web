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
from models.product import ProductRef, clean_product_id, normalize_platform
from repositories.product_repo import ProductRepository
from cache import PROMOTION_CACHE
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
            # Sign 是主标识。客户端另一 ID 必须经同平台仓储确认才可用作兜底。
            ref = ProductRef(ref.platform, "", ref.goods_sign)
            try:
                doc = self.repo.get_product_identity(ref.goods_sign, platform=ref.platform)
                if doc and normalize_platform(doc.get("platform", ref.platform)) == ref.platform:
                    goods_id = clean_product_id(doc.get("product_id")) or clean_product_id(doc.get("goods_id"))
                    ref = ProductRef(ref.platform, goods_id, ref.goods_sign)
            except Exception as exc:
                logger.warning(f"推广商品身份缓存读取失败: {exc}")
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

    def _get_link_data(self, ref: ProductRef, product_url: str, uid: str, identity: dict, adapter, kwargs: dict) -> dict:
        started = time.perf_counter()
        scope = "personal" if uid else "public"
        kind = "sign" if ref.goods_sign else "id"
        cache_key = ("promotion", ref.platform, kind, ref.key, product_url, uid)
        if identity:
            cache_key += (hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),)
        cached = PROMOTION_CACHE.get(cache_key)
        if (isinstance(cached, tuple) and len(cached) == 2 and self._valid_deadline(cached[0])
                and self._cacheable(cached[1]) and (uid or self._public_attribution(cached[1]))):
            logger.info("[promotion] platform=%s scope=%s source=memory elapsed_ms=%.1f",
                        ref.platform, scope, (time.perf_counter() - started) * 1000)
            return self._link_data(cached[1])
        if cached is not None:
            PROMOTION_CACHE.pop(cache_key)

        ref = self._resolve_product_ref(ref)
        if not uid and ref.key:
            cached = self.repo.get_promotion_link_cache(
                platform=ref.platform, goods_id=ref.goods_id or None,
                goods_sign=ref.goods_sign or None, goods_key=ref.key,
            )
            if (self._cacheable(cached) and self._public_attribution(cached)
                    and self._valid_deadline(cached.get("_expires_at"))):
                expires_at = cached["_expires_at"]
                data = self._link_data(cached)
                PROMOTION_CACHE.set(cache_key, (expires_at, deepcopy(data)))
                logger.info("[promotion] platform=%s scope=public source=database elapsed_ms=%.1f",
                            ref.platform, (time.perf_counter() - started) * 1000)
                return data

        adapter_kwargs = {k: v for k, v in kwargs.items()
                          if k not in ("goods_id", "goods_sign", "identity", "relation_id", "special_id")}
        if getattr(adapter, "requires_authorized_promotion", False):
            adapter_kwargs["identity"] = identity
        provider_started = time.perf_counter()
        try:
            result = adapter.convert_promotion_url(
                goods_id_or_sign=ref.key, uid=uid or None,
                goods_id=ref.goods_id or None, goods_sign=ref.goods_sign or None,
                product_url=product_url or None, **adapter_kwargs
            )
        finally:
            logger.info("[promotion] platform=%s scope=%s source=provider provider_ms=%.1f elapsed_ms=%.1f",
                        ref.platform, scope, (time.perf_counter() - provider_started) * 1000,
                        (time.perf_counter() - started) * 1000)
        if not isinstance(result, dict) or result.get("status") != "success":
            message = result.get("error_msg") if isinstance(result, dict) else None
            error = {"status": "error", "error_msg": message or "未能生成有效推广转链，商品可能已失效或缺少必要标识"}
            if isinstance(result, dict):
                error.update({key: result[key] for key in ("error_code", "need_auth", "auth_platform", "auth_msg", "attribution_status")
                              if key in result})
            return error

        data = self._link_data(result)
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
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        if not adapter.active:
            raise PlatformUnavailableError(f"{adapter.platform_name}暂未接入")
        clean_uid = uid.strip() if isinstance(uid, str) else ""
        if clean_uid and not _UID_PATTERN.fullmatch(clean_uid):
            clean_uid = ""
        resolved = adapter.resolve_product_ref(
            goods_id_or_sign, goods_id=kwargs.get("goods_id"), goods_sign=kwargs.get("goods_sign")
        )
        ref = ProductRef(plat, resolved.goods_id, resolved.goods_sign)
        product_url = str(product_url or "").strip()
        if not ref.key and not product_url:
            return {"status": "error", "error_msg": "缺少有效商品标识或链接"}

        identity, context = {}, {}
        effective_uid = clean_uid
        need_auth = False
        if clean_uid and getattr(adapter, "requires_authorized_promotion", False):
            if self.user_service is None:
                from services.user_service import UserService
                self.user_service = UserService()
            started = time.perf_counter()
            context = self.user_service.get_promotion_context(clean_uid, plat, allow_remote=False)
            logger.info("[promotion] platform=%s stage=local_authorization elapsed_ms=%.1f",
                        plat, (time.perf_counter() - started) * 1000)
            if context.get("status") != "success":
                return context
            if context.get("is_authorized") is True:
                identity = context.get("identity") or {}
            else:
                effective_uid = ""
                need_auth = True

        data = self._get_link_data(ref, product_url, effective_uid, identity, adapter, kwargs)
        personal_fallback = False
        if (effective_uid and getattr(adapter, "requires_authorized_promotion", False)
                and data.get("status") == "error"):
            code = data.get("error_code")
            if code == "AUTH_REQUIRED" and data.get("need_auth") is True:
                # 策略 B: 乐观重试/无损兜底。不在转链失败时物理注销数据库中的授权信息，防止网络抖动误伤推手；仅在本次转链降级为通用链接并提示
                context = {"authorization_status": "unauthorized"}
                need_auth = True
            elif code != "PERSONAL_PROMOTION_UNAVAILABLE":
                return data
            personal_fallback = True
            data = self._get_link_data(ref, product_url, "", {}, adapter, kwargs)
        if data.get("status") != "success":
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
        elif personal_fallback:
            result.update(is_fallback=True, attribution_status="anonymous")
        return result
