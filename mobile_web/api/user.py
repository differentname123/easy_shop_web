# -*- coding: utf-8 -*-
"""
用户中心接口路由：推手手机号合规注册/登录、时效凭证验证与多平台渠道备案授权
"""
from typing import Optional
from fastapi import APIRouter, Query, Depends, HTTPException, status
from models.user import RegisterOrLoginDTO
from services.user_service import UserService
from auth import get_current_uid

router = APIRouter(prefix="/api/user", tags=["用户与授权"])
user_service = UserService()


# 保留旧导入名，参数约束统一由领域 DTO 定义。
RegisterOrLoginRequest = RegisterOrLoginDTO


@router.post("/register_or_login")
def register_or_login(req: RegisterOrLoginRequest):
    """
    推手手机号注册与登录一体化接口
    - 手机号不存在：自动注册新账号，绑定邀请码，签发专属 auth_token 与 UID；
    - 手机号已存在：校验密码并登录，平滑升级密码哈希，返回 auth_token 与脱敏手机号。
    """
    res = user_service.register_or_login(
        phone=req.phone,
        password=req.password,
        nickname=req.nickname,
        invite_code=req.invite_code,
    )
    if res.get("status") != "success":
        raise HTTPException(status_code=400, detail=res.get("error_msg", "操作失败"))
    return res


@router.get("/info")
def get_user_info(
    current_uid: str = Depends(get_current_uid),
    uid: Optional[str] = Query(None, description="推手唯一UID (兼容参数，服务端严格校验)"),
    phone: Optional[str] = Query(None, description="手机号 (兼容旧参数，必须与当前登录身份一致)"),
):
    """
    获取当前登录推手信息及各平台授权状态 (手机号脱敏返回)
    - 必须携带有效 Authorization Bearer 或 Token，严禁任何未授权匿名手机号探针探测。
    """
    if uid and uid.strip() != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权查询非本人名下的用户资料")

    res = user_service.get_user_info(uid=current_uid)
    if res.get("status") != "success":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=res.get("error_msg", "用户不存在"))
    return res


@router.get("/auth")
def check_or_get_auth(
    current_uid: str = Depends(get_current_uid),
    uid: Optional[str] = Query(None, description="用户唯一UID (兼容参数)"),
    platform: str = Query("pdd", max_length=32, pattern="^[a-z][a-z0-9_]*$", description="平台代码"),
):
    """
    查询当前认证推手在指定平台的授权备案状态。若未授权，则自动返回专属备案授权链接。
    """
    if uid and uid.strip() != current_uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作非本人名下的备案授权")
    return user_service.check_and_get_auth(uid=current_uid, platform=platform)
