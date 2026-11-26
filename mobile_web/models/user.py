# -*- coding: utf-8 -*-
"""
用户领域统一数据模型 (DTO)
"""
import re
from typing import Optional
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
