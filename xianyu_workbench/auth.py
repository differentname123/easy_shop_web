# -*- coding: utf-8 -*-
"""工作台独立账号认证：PBKDF2 密码哈希、HMAC 令牌与 Header/Cookie 双通道提取。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, status

COOKIE_NAME = "xy_token"
PBKDF2_ITERATIONS = 100_000


def hash_password(password: str) -> str:
    """生成带随机盐值的 PBKDF2-SHA256 密码哈希。"""
    raw = str(password or "")
    if len(raw) < 6:
        raise ValueError("密码长度至少需要 6 位")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """恒定时间校验密码哈希。"""
    raw = str(password or "")
    stored = str(stored_hash or "").strip()
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iters = int(parts[1])
    except ValueError:
        return False
    salt, expected_hex = parts[2], parts[3]
    actual_hex = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), iters).hex()
    return hmac.compare_digest(actual_hex, expected_hex)


def create_token(username: str, secret: str, ttl_seconds: int = 30 * 86400) -> str:
    """签发带过期时间的轻量 HMAC-SHA256 令牌。"""
    clean_user = str(username or "").strip()
    if not clean_user:
        raise ValueError("username 不能为空")
    exp = int(time.time()) + max(60, int(ttl_seconds))
    payload = f"{clean_user}.{exp}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: Optional[str], secret: str) -> Optional[str]:
    """校验令牌合法性与有效期，通过返回 username，否则返回 None。"""
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if raw.count(".") != 2:
        return None
    username, exp_str, sig = raw.split(".")
    if not username or not exp_str.isdigit():
        return None
    if int(exp_str) < int(time.time()):
        return None
    payload = f"{username}.{exp_str}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return username


def extract_token_from_request(request: Request) -> Optional[str]:
    """依次从 Authorization Header、X-Auth-Token Header 和 HttpOnly Cookie 提取令牌。"""
    for candidate in (
        request.headers.get("Authorization"),
        request.headers.get("X-Auth-Token"),
        request.cookies.get(COOKIE_NAME),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def require_current_username(request: Request, secret: str) -> str:
    """校验请求身份，未登录或过期统一抛出 401。"""
    token = extract_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录闲鱼选品工作台",
        )
    username = verify_token(token, secret)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期或凭证无效，请重新登录",
        )
    return username
