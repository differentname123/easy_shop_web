# -*- coding: utf-8 -*-
"""推广生链编排：主标识隔离缓存，可信身份补全与当前请求文案各司其职。"""
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
    def _valid_deadline(expires_at) -> bool:
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return False
        remaining = expires_at - time.time()
        return math.isfinite(remaining) and 0 < remaining <= PROMOTION_LINK_TTL

    @staticmethod
    def _link_data(result: dict) -> dict:
        data = deepcopy(result)
        data.pop("command_text", None)
        data.pop("_expires_at", None)
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
        ref = adapter.resolve_product_ref(
            goods_id_or_sign, goods_id=kwargs.get("goods_id"), goods_sign=kwargs.get("goods_sign")
        )
        product_url = str(product_url or "").strip()
        if not ref.key and not product_url:
            return {"status": "error", "error_msg": "缺少有效商品标识或链接"}

        identity = {}
        if clean_uid and getattr(adapter, "requires_authorized_promotion", False):
            if self.user_service is None:
                from services.user_service import UserService
                self.user_service = UserService()
            context = self.user_service.get_promotion_context(clean_uid, plat)
            if context.get("status") != "success":
                return context
            identity = context.get("identity") or {}
        identity_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() if identity else ""
        # 个人渠道变化使旧链接失效；非渠道型平台仍可先命中缓存再补商品身份。
        kind = "sign" if ref.goods_sign else "id"
        cache_key = ("promotion", plat, kind, ref.key, product_url, clean_uid)
        if identity_key:
            cache_key += (identity_key,)
        cached = PROMOTION_CACHE.get(cache_key)
        if (isinstance(cached, tuple) and len(cached) == 2
                and self._valid_deadline(cached[0]) and self._cacheable(cached[1])):
            return self._with_command(cached[1], adapter, kwargs)
        if cached is not None:
            PROMOTION_CACHE.pop(cache_key)

        ref = self._resolve_product_ref(ref)
        if not clean_uid and ref.key:
            cached = self.repo.get_promotion_link_cache(
                platform=plat, goods_id=ref.goods_id or None,
                goods_sign=ref.goods_sign or None, goods_key=ref.key,
            )
            if self._cacheable(cached) and self._valid_deadline(cached.get("_expires_at")):
                expires_at = cached["_expires_at"]
                data = self._link_data(cached)
                PROMOTION_CACHE.set(cache_key, (expires_at, data))
                return self._with_command(data, adapter, kwargs)

        adapter_kwargs = {k: v for k, v in kwargs.items()
                          if k not in ("goods_id", "goods_sign", "identity", "relation_id", "special_id")}
        if getattr(adapter, "requires_authorized_promotion", False):
            adapter_kwargs["identity"] = identity
        result = adapter.convert_promotion_url(
            goods_id_or_sign=ref.key, uid=clean_uid or None,
            goods_id=ref.goods_id or None, goods_sign=ref.goods_sign or None,
            product_url=product_url or None, **adapter_kwargs
        )
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
        expires_at = time.time() + PROMOTION_LINK_TTL
        source_deadline = data.get("expires_at")
        if source_deadline is not None:
            if (isinstance(source_deadline, bool) or not isinstance(source_deadline, (int, float))
                    or not math.isfinite(source_deadline) or source_deadline <= time.time()):
                return {"status": "error", "error_msg": "平台返回的推广链接已过期或有效期无效"}
            expires_at = min(expires_at, source_deadline)
        if self._cacheable(data):
            PROMOTION_CACHE.set(cache_key, (expires_at, deepcopy(data)))
            if not clean_uid and ref.key:
                self.repo.save_promotion_link_cache(
                    platform=plat, data=deepcopy(data), goods_id=ref.goods_id or None,
                    goods_sign=ref.goods_sign or None, goods_key=ref.key, expires_at=expires_at,
                )
        return self._with_command(data, adapter, kwargs)
