# -*- coding: utf-8 -*-
"""
用户领域统一数据模型 (DTO)
"""
import re
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


def normalize_promoter_id(value) -> str:
    """推广 ID 是正十进制字符串；不接受浮点、布尔或 Unicode 数字。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("promoter_id 必须是正整数标识")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,20}", text) or int(text) == 0:
        raise ValueError("promoter_id 必须是正整数标识")
    return str(int(text))


class RegisterOrLoginDTO(BaseModel):
    """注册/登录请求参数校验"""
    phone: str = Field(..., description="11位中国大陆手机号")
    password: str = Field(..., min_length=6, max_length=32, description="登录密码")
    nickname: Optional[str] = Field(None, max_length=20, description="用户昵称")
    invite_code: Optional[str] = Field(None, max_length=20, description="上级邀请码 (选填)")
    mode: Optional[str] = Field(None, pattern="^(login|register)$", description="操作意图: login 或 register (选填)")


class UserProfile(BaseModel):
    """用户对外的公开安全资料模型 (已脱敏)"""
    uid: str
    promoter_id: Optional[str] = None
    phone: Optional[str] = None
    nickname: str
    avatar: str = "https://img.pddpic.com/mms-avatar/default_avatar.png"
    invite_code: str
    invited_by: Optional[str] = None
    balance: float = 0.0
    total_rebate: float = 0.0
    pending_rebate: float = 0.0
    auth_status: Dict[str, Any] = Field(default_factory=dict)
    auth_token: Optional[str] = None
