# -*- coding: utf-8 -*-
"""
用户认证鉴权与密码安全核心模块
- 基于 HMAC-SHA256 的轻量有界时效性访问令牌 (Token)
- PBKDF2 密码哈希与平滑向后兼容旧哈希校验
- FastAPI 标准 Depends(get_current_uid) 鉴权依赖注入
"""
import hmac
import hashlib
import secrets
import time
from typing import Optional

from fastapi import Header, Query, HTTPException, status
from config import AUTH_SECRET

TOKEN_TTL_SECONDS = 30 * 86400  # 访问令牌默认有效期 30 天
PBKDF2_ITERATIONS = 100_000
LEGACY_SALT = "easy_shop_salt_2026"


def create_access_token(uid: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    """
    签发轻量、带过期时间戳与 HMAC 防篡改签名的访问令牌
    令牌结构: {uid}.{exp_timestamp}.{hmac_sha256_hex}
    """
    if not uid or not isinstance(uid, str):
        raise ValueError("uid 不能为空")
    exp = int(time.time()) + int(ttl)
    payload = f"{uid.strip()}.{exp}"
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_access_token(token: Optional[str]) -> Optional[str]:
    """
    校验访问令牌合法性与有效期，成功返回授权 uid，失败返回 None
    """
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    # 支持 Bearer token 前缀清洗
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.count(".") != 2:
        return None

    try:
        uid, exp_str, sig = token.split(".")
        if not exp_str.isdigit():
            return None
        if int(exp_str) < int(time.time()):
            return None  # 已过期
        payload = f"{uid}.{exp_str}"
        expected = hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return uid
    except Exception:
        return None
    return None


async def get_current_uid(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_auth_token: Optional[str] = Header(None, alias="X-Auth-Token"),
    token: Optional[str] = Query(None, description="登录鉴权令牌"),
) -> str:
    """
    FastAPI 统一身份鉴权依赖项
    依次从 Authorization 请求头、X-Auth-Token 头或 URL Query token 参数提取令牌并验证。
    通过后返回权威 uid，未通过抛出标准 401 未认证异常。
    """
    raw_token = None
    for candidate in (authorization, x_auth_token, token):
        if isinstance(candidate, str) and candidate.strip():
            raw_token = candidate.strip()
            break

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少身份鉴权凭证，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    uid = verify_access_token(raw_token)
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已过期或令牌不合法，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return uid


def hash_password(password: str) -> str:
    """
    使用 PBKDF2-HMAC-SHA256 生成强密码哈希（含随机加盐）
    格式: pbkdf2_sha256${iterations}${salt_hex}${digest_hex}
    """
    if not password or not isinstance(password, str):
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: Optional[str]) -> bool:
    """
    校验密码哈希，平滑兼容旧加盐 SHA256 格式
    """
    if not password or not stored_hash or not isinstance(stored_hash, str):
        return False
    # 1. 尝试新标准 PBKDF2 校验
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            parts = stored_hash.split("$")
            if len(parts) == 4:
                _, iter_str, salt_hex, digest_hex = parts
                salt = bytes.fromhex(salt_hex)
                expected = bytes.fromhex(digest_hex)
                actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_str))
                return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    # 2. 向后兼容旧格式加盐单次 SHA-256
    legacy_expected = hashlib.sha256(f"{LEGACY_SALT}_{password}_{LEGACY_SALT}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(stored_hash, legacy_expected)


def mask_phone(phone: Optional[str]) -> str:
    """手机号脱敏显示，例如 13800138000 -> 138****8000"""
    if not phone or not isinstance(phone, str) or len(phone) != 11:
        return str(phone or "")
    return f"{phone[:3]}****{phone[7:]}"
