# -*- coding: utf-8 -*-
"""统一对外边界：无状态确定性商品/券编号与全局 HTTP 响应自动脱敏。"""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import hmac
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

import config
from cache import SIGN_BY_ID_CACHE
from models.product import (
    clean_product_id,
    is_jd_image_key,
    normalize_platform,
    resolve_item_identity,
    strip_ecommerce_tokens_and_urls,
)

ITEM_CODE_PREFIX = "p1_"
COUPON_CODE_PREFIX = "c1_"
_MAX_CODE_LEN = 256
_TAG_LEN = 12
_B64_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
logger = logging.getLogger("mobile_web")
_SANITIZE_ERROR_BODY = json.dumps(
    {"status": "error", "error_msg": "服务响应异常，请稍后重试"},
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")

FORBIDDEN_PLAINTEXT_QUERY_KEYS = frozenset({
    "goods_id",
    "goods_sign",
    "product_id",
    "product_url",
    "coupon_url",
    "platform",
})

_IDENTITY_SOURCE_KEYS = (
    "goods_id",
    "goods_sign",
    "product_id",
    "canonical_goods_id",
    "candidate_goods_id",
    "resolved_product_id",
    "resolved_goods_sign",
    "item_id",
    "tao_id",
    "num_iid",
)
_IDENTITY_SOURCE_KEY_SET = frozenset(_IDENTITY_SOURCE_KEYS)

_IDENTITY_KEYS = frozenset({
    *_IDENTITY_SOURCE_KEYS,
    "sku_id",
    "superseded_by",
})

_PROMO_TOP_REMOVE_KEYS = _IDENTITY_KEYS | frozenset({
    "product_url",
    "coupon_url",
    "item_url",
    "material_url",
    "coupon_share_url",
    "coupon_click_url",
    "raw",
    "raw_item",
    "search_id",
})

_REGULAR_REMOVE_KEYS = _PROMO_TOP_REMOVE_KEYS | frozenset({
    "click_url",
    "short_url",
    "mobile_url",
    "mobile_short_url",
    "final_link",
    "schema_url",
    "promotion_token",
    "j_command",
    "j_short_command",
    "command_text",
    "my_promo_link",
    "my_promo_token",
    "my_promotion_link",
    "my_promotion_token",
    "we_app_info",
})

_EXEMPT_PREFIXES = ("/api/group_help/", "/api/user/", "/api/system/")
_EXEMPT_EXACT = frozenset({
    "/api/group_help",
    "/api/user",
    "/api/system",
    "/api/home/platforms",
    "/api/home/channels",
})
_PROMO_PATHS = frozenset({"/api/promote/convert", "/api/promote/universal"})


@lru_cache(maxsize=8)
def _derive_keys_cached(secret_str: str, kind: str) -> Tuple[bytes, bytes]:
    secret = secret_str.encode("utf-8")
    k_mac = hmac.new(secret, f"easy_shop:{kind}:mac:v1".encode("utf-8"), hashlib.sha256).digest()
    k_enc = hmac.new(secret, f"easy_shop:{kind}:enc:v1".encode("utf-8"), hashlib.sha256).digest()
    return k_mac, k_enc


def _derive_keys(kind: str) -> Tuple[bytes, bytes]:
    secret_str = str(getattr(config, "AUTH_SECRET", "") or "").strip()
    if not secret_str:
        raise RuntimeError("AUTH_SECRET 未配置")
    return _derive_keys_cached(secret_str, kind)


def _ctr_keystream(k_enc: bytes, iv: bytes, length: int) -> bytes:
    chunks: List[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        block = hashlib.sha256(k_enc + iv + counter.to_bytes(4, "big")).digest()
        chunks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(chunks)[:length]


def _seal(kind: str, prefix: str, payload: str) -> str:
    raw = payload.encode("utf-8")
    k_mac, k_enc = _derive_keys(kind)
    tag = hmac.new(k_mac, raw, hashlib.sha256).digest()[:_TAG_LEN]
    stream = _ctr_keystream(k_enc, tag, len(raw))
    ct = bytes(a ^ b for a, b in zip(raw, stream))
    encoded = base64.urlsafe_b64encode(tag + ct).decode("ascii").rstrip("=")
    return prefix + encoded


def _unseal(kind: str, prefix: str, code: Any) -> str:
    if not isinstance(code, str):
        raise ValueError("编号格式无效")
    s = code.strip()
    if not s or len(s) > _MAX_CODE_LEN or not s.startswith(prefix):
        raise ValueError("编号格式无效")
    body = s[len(prefix):]
    if not body or not _B64_PART_RE.fullmatch(body):
        raise ValueError("编号格式无效")
    padded = body + "=" * (-len(body) % 4)
    try:
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ValueError("编号解码失败") from exc
    if len(raw_bytes) < _TAG_LEN + 1:
        raise ValueError("编号长度无效")
    tag, ct = raw_bytes[:_TAG_LEN], raw_bytes[_TAG_LEN:]
    k_mac, k_enc = _derive_keys(kind)
    stream = _ctr_keystream(k_enc, tag, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, stream))
    expected_tag = hmac.new(k_mac, pt, hashlib.sha256).digest()[:_TAG_LEN]
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("编号校验失败")
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("编号内容无效") from exc


def encode_item_code(platform: str, canonical_id: str) -> str:
    plat = normalize_platform(platform, default="")
    cid = clean_product_id(canonical_id)
    if not plat or not cid:
        raise ValueError("无法为缺少平台或标识的商品编码")
    return _seal("item", ITEM_CODE_PREFIX, f"{plat}\x1f{cid}")


def decode_item_code(code: Any) -> Tuple[str, str]:
    text = _unseal("item", ITEM_CODE_PREFIX, code)
    parts = text.split("\x1f")
    if len(parts) != 2:
        raise ValueError("商品编号载荷无效")
    raw_plat, raw_cid = parts
    plat = normalize_platform(raw_plat, default="")
    cid = clean_product_id(raw_cid)
    if not plat or plat != raw_plat or not cid:
        raise ValueError("商品编号内容无效")
    return plat, cid


def coupon_link_digest(url: str, token: str) -> str:
    u = str(url or "").strip()
    t = str(token or "").strip()
    return hashlib.sha256(f"{u}|{t}".encode("utf-8")).hexdigest()[:12]


def encode_coupon_code(
    platform: str,
    canonical_id: str,
    url: str = "",
    token: str = "",
    *,
    digest: str = "",
) -> str:
    plat = normalize_platform(platform, default="")
    cid = clean_product_id(canonical_id)
    dg = str(digest or coupon_link_digest(url, token)).strip()
    if not plat or not cid or not dg:
        raise ValueError("无法为缺少身份的优惠券编码")
    return _seal("coupon", COUPON_CODE_PREFIX, f"{plat}\x1f{cid}\x1f{dg}")


def decode_coupon_code(code: Any) -> Tuple[str, str, str]:
    text = _unseal("coupon", COUPON_CODE_PREFIX, code)
    parts = text.split("\x1f")
    if len(parts) != 3:
        raise ValueError("优惠券编号载荷无效")
    raw_plat, raw_cid, dg = parts
    plat = normalize_platform(raw_plat, default="")
    cid = clean_product_id(raw_cid)
    if not plat or plat != raw_plat or not cid or not dg:
        raise ValueError("优惠券编号内容无效")
    return plat, cid, dg


def require_item_code(code: Any) -> Tuple[str, str]:
    try:
        return decode_item_code(code)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的商品编号") from None


def require_coupon_code(code: Any) -> Tuple[str, str, str]:
    try:
        return decode_coupon_code(code)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的优惠券编号") from None


def resolve_internal_product_keys(platform: str, canonical_id: str) -> Tuple[str, str, str]:
    """将解码后的 (platform, canonical_id) 还原为内部 (platform, goods_id, goods_sign)。"""
    plat = normalize_platform(platform)
    cid = clean_product_id(canonical_id)
    if not plat or not cid:
        return plat, "", ""
    if plat == "pdd" and not cid.isdigit():
        return "pdd", "", cid
    gsign = ""
    cached_sign = SIGN_BY_ID_CACHE.get((plat, cid))
    if isinstance(cached_sign, str):
        cs = cached_sign.strip()
        if (
            cs
            and cs != cid
            and not (plat == "jd" and is_jd_image_key(cs))
            and not (plat == "pdd" and cs.isdigit())
        ):
            gsign = cs
    return plat, cid, gsign


def _extract_node_identity(
    node: Dict[str, Any],
    *,
    ctx_platform: str = "",
) -> Tuple[str, str]:
    """从含商品定位键的字典中提取规范 `(platform, canonical_id)`，并顺手刷新热映射缓存。"""
    if _IDENTITY_SOURCE_KEY_SET.isdisjoint(node):
        return "", ""
    if not any(clean_product_id(node.get(k)) for k in _IDENTITY_SOURCE_KEYS if k in node):
        return "", ""
    plat_hint = str(node.get("resolved_platform") or node.get("platform") or ctx_platform or "pdd").strip()
    probe = {
        "platform": plat_hint,
        "goods_id": (
            node.get("goods_id")
            or node.get("canonical_goods_id")
            or node.get("resolved_product_id")
            or node.get("candidate_goods_id")
            or node.get("item_id")
            or node.get("tao_id")
            or node.get("num_iid")
            or ""
        ),
        "product_id": node.get("product_id") or "",
        "goods_sign": node.get("goods_sign") or node.get("resolved_goods_sign") or "",
        "image_url": (
            node.get("image_url")
            or node.get("goods_image_url")
            or node.get("goods_thumbnail_url")
            or ""
        ),
    }
    plat, gid, gsign = resolve_item_identity(probe, default_platform=plat_hint or "pdd")
    cid = clean_product_id(gid) or clean_product_id(gsign)
    if not plat or not cid:
        return "", ""
    if (
        gid
        and gsign
        and gid != gsign
        and not (plat == "jd" and is_jd_image_key(gsign))
        and not (plat == "pdd" and gsign.isdigit())
    ):
        SIGN_BY_ID_CACHE.set((plat, gid), gsign)
    return plat, cid


def _sanitize_clean_text(raw: Any, *, preserve_newlines: bool) -> str:
    cleaned = strip_ecommerce_tokens_and_urls(raw, preserve_newlines=preserve_newlines)
    if preserve_newlines:
        lines = [ln.rstrip() for ln in cleaned.splitlines() if ln.strip()]
        return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()
    return re.sub(r"\s+", " ", cleaned).strip()


def _sanitize_deal_tip(deal_tip: Dict[str, Any], plat: str, cid: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in deal_tip.items():
        if key in _REGULAR_REMOVE_KEYS:
            continue
        if key == "coupon_links" and isinstance(val, list):
            safe_coupons: List[Dict[str, Any]] = []
            for entry in val:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "").strip()
                token = str(entry.get("token") or "").strip()
                if not url and not token:
                    continue
                if not plat or not cid:
                    continue
                lbl = str(entry.get("label") or "优惠券").strip() or "优惠券"
                entry_plat = normalize_platform(entry.get("platform") or plat, default=plat)
                safe_coupons.append({
                    "label": lbl,
                    "coupon_code": encode_coupon_code(plat, cid, url, token),
                    "has_url": bool(url),
                    "has_token": bool(token),
                    "platform": entry_plat,
                })
            out["coupon_links"] = safe_coupons
        elif key == "original_copywriting" and isinstance(val, str):
            out[key] = _sanitize_clean_text(val, preserve_newlines=True)
        elif key == "tutorial_steps" and isinstance(val, list):
            out[key] = [
                txt
                for txt in (_sanitize_clean_text(step, preserve_newlines=False) for step in val)
                if txt
            ]
        elif key == "price_logic" and isinstance(val, str):
            out[key] = _sanitize_clean_text(val, preserve_newlines=False)
        else:
            out[key] = _sanitize_node(val, ctx_platform=plat, ctx_cid=cid, promo_top=False)
    return out


def _sanitize_node(
    node: Any,
    *,
    ctx_platform: str = "",
    ctx_cid: str = "",
    promo_top: bool = False,
) -> Any:
    if isinstance(node, list):
        return [_sanitize_node(item, ctx_platform=ctx_platform, ctx_cid=ctx_cid, promo_top=False) for item in node]
    if not isinstance(node, dict):
        return node

    plat, cid = _extract_node_identity(node, ctx_platform=ctx_platform)
    next_plat = plat or ctx_platform
    next_cid = cid or ctx_cid
    remove_keys = _PROMO_TOP_REMOVE_KEYS if promo_top else _REGULAR_REMOVE_KEYS

    out: Dict[str, Any] = {}
    if not promo_top and plat and cid:
        out["item_code"] = encode_item_code(plat, cid)

    for key, val in node.items():
        if key in remove_keys:
            continue
        if key == "item_code" and "item_code" in out:
            continue
        if key == "deal_tip" and isinstance(val, dict):
            out[key] = _sanitize_deal_tip(val, next_plat, next_cid)
        elif key == "desc" and isinstance(val, str) and not promo_top:
            out[key] = _sanitize_clean_text(val, preserve_newlines=True)
        else:
            out[key] = _sanitize_node(val, ctx_platform=next_plat, ctx_cid=next_cid, promo_top=False)
    return out


def sanitize_public_payload(payload: Any, *, promo_mode: bool = False) -> Any:
    """对出站 JSON 载荷执行递归脱敏与加密编号注入。"""
    return _sanitize_node(payload, promo_top=promo_mode)


def _is_exempt_api_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return True
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


class PublicBoundaryMiddleware:
    """纯 ASGI 全局出站脱敏中间件（位于 GZipMiddleware 内层）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if _is_exempt_api_path(path):
            await self.app(scope, receive, send)
            return

        promo_mode = path in _PROMO_PATHS
        start_message: Optional[Dict[str, Any]] = None
        is_json = False
        body_chunks: List[bytes] = []

        async def send_wrapper(message):
            nonlocal start_message, is_json
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                headers = message.get("headers") or []
                content_type = ""
                for k, v in headers:
                    if k.lower() == b"content-type":
                        content_type = v.decode("latin-1").lower()
                        break
                is_json = "application/json" in content_type
                if not is_json:
                    await send(message)
                else:
                    start_message = message
                return

            if msg_type == "http.response.body":
                if not is_json:
                    await send(message)
                    return
                chunk = message.get("body") or b""
                if chunk:
                    body_chunks.append(chunk)
                if message.get("more_body"):
                    return

                raw_body = b"".join(body_chunks)
                out_body = raw_body
                status_override: Optional[int] = None
                if raw_body:
                    try:
                        parsed = json.loads(raw_body.decode("utf-8"))
                        sanitized = sanitize_public_payload(parsed, promo_mode=promo_mode)
                        out_body = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    except Exception:
                        logger.exception("❌ 【对外边界脱敏异常】响应脱敏失败，已阻断原始明文输出 | 路径[%s]", path)
                        status_override = 500
                        out_body = _SANITIZE_ERROR_BODY

                assert start_message is not None
                if status_override is not None:
                    new_headers = [
                        (k, v)
                        for k, v in (start_message.get("headers") or [])
                        if k.lower() not in (b"content-length", b"content-type")
                    ]
                    new_headers.append((b"content-type", b"application/json; charset=utf-8"))
                    new_headers.append((b"content-length", str(len(out_body)).encode("ascii")))
                    await send({**start_message, "status": status_override, "headers": new_headers})
                else:
                    new_headers = [
                        (k, v)
                        for k, v in (start_message.get("headers") or [])
                        if k.lower() != b"content-length"
                    ]
                    new_headers.append((b"content-length", str(len(out_body)).encode("ascii")))
                    await send({**start_message, "headers": new_headers})
                await send({"type": "http.response.body", "body": out_body, "more_body": False})
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)
