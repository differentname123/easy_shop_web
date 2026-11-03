# -*- coding: utf-8 -*-
"""
用户与返利授权业务服务
处理推手手机号合规注册/登录、密码校验、时效令牌签发、敏感信息脱敏与渠道备案授权。
"""
import re
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from config import AUTH_STATUS_TTL
from models.product import normalize_platform
from repositories.user_repo import UserRepository
from adapters import get_adapter, PlatformUnavailableError
from auth import create_access_token, mask_phone
from config import logger

PHONE_REGEX = re.compile(r"^1[3-9]\d{9}$")


class UserService:
    def __init__(self, user_repo: Optional[UserRepository] = None):
        self.repo = user_repo or UserRepository()

    @staticmethod
    def validate_phone(phone: str) -> str:
        """严格校验并清洗手机号"""
        if not phone or not isinstance(phone, str):
            raise ValueError("手机号不能为空")
        cleaned = phone.strip()
        if not PHONE_REGEX.match(cleaned):
            raise ValueError("手机号格式不正确，请输入有效的11位中国大陆手机号")
        return cleaned

    @staticmethod
    def validate_password(password: str) -> str:
        """校验密码有效性"""
        if not password or not isinstance(password, str):
            raise ValueError("密码不能为空")
        cleaned = password.strip()
        if len(cleaned) < 6:
            raise ValueError("密码长度不能少于6位")
        if len(cleaned) > 32:
            raise ValueError("密码长度不能超过32位")
        return cleaned

    def register_or_login(
        self,
        phone: str,
        password: str,
        nickname: Optional[str] = None,
        invite_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        手机号注册或登录：
        - 验证成功后自动签发带 30 天时效签名的 auth_token；
        - 返回前对手机号进行掩码脱敏 (138****0000)，杜绝明文返回。
        """
        try:
            valid_phone = self.validate_phone(phone)
            valid_pwd = self.validate_password(password)
            user = self.repo.register_or_login(
                phone=valid_phone,
                password=valid_pwd,
                nickname=nickname,
                invite_code=invite_code
            )
            auth_token = create_access_token(user["uid"])
            user_data = dict(user)
            user_data["auth_token"] = auth_token
            user_data["phone"] = mask_phone(user_data.get("phone"))
            return {"status": "success", "user": user_data}
        except ValueError as e:
            return {"status": "error", "error_msg": str(e)}
        except Exception as e:
            logger.error(f"注册登录服务异常: {e}")
            return {"status": "error", "error_msg": f"系统异常: {str(e)}"}

    def get_user_info(self, uid: str) -> Dict[str, Any]:
        """按已鉴权的权威 uid 获取用户信息及各平台授权状态（手机号脱敏）"""
        if not uid:
            return {"status": "error", "error_msg": "uid 不能为空"}
        user = self.repo.get_user_by_uid(uid)
        if not user:
            return {"status": "error", "error_msg": "用户未找到"}

        user_data = dict(user)
        user_data.pop("password_hash", None)
        user_data["phone"] = mask_phone(user_data.get("phone"))
        return {"status": "success", "user": user_data}

    @staticmethod
    def _auth_is_fresh(auth_status: dict) -> bool:
        """Mongo 的 naive datetime 按 UTC 解读；无更新时间的旧记录必须复核。"""
        updated_at = auth_status.get("updated_at")
        if isinstance(updated_at, datetime):
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            updated_at = updated_at.timestamp()
        if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
            return False
        age = time.time() - updated_at
        return 0 <= age < AUTH_STATUS_TTL

    def check_and_get_auth(self, uid: str, platform: str = "pdd") -> Dict[str, Any]:
        """只缓存已确认且未过期的授权；上游失败不等价于未授权。"""
        plat = normalize_platform(platform)
        if not uid:
            return {"status": "error", "error_msg": "uid 不能为空"}
        adapter = get_adapter(plat)
        if not adapter.active:
            raise PlatformUnavailableError(f"{adapter.platform_name}暂未接入")

        try:
            user = self.repo.get_user_by_uid(uid)
            if not user:
                return {"status": "error", "error_msg": "推手用户不存在"}
            auth_status = (user.get("auth_status") or {}).get(plat) or {}
            if auth_status.get("is_authorized") is True and self._auth_is_fresh(auth_status):
                return self._authorized_response(uid, plat, adapter.platform_name, auth_status.get("bind", 0))

            auth_res = adapter.check_authority(uid=uid)
            if not isinstance(auth_res, dict) or auth_res.get("status") != "success":
                message = auth_res.get("error_msg") if isinstance(auth_res, dict) else None
                return {"status": "error", "platform": plat, "error_msg": message or "平台授权状态查询失败，请稍后重试"}
            if not isinstance(auth_res.get("is_authorized"), bool):
                return {"status": "error", "platform": plat, "error_msg": "平台授权状态响应无效"}

            is_authorized = auth_res["is_authorized"]
            bind = auth_res.get("bind", 0)
            if not self.repo.update_auth_status(uid=uid, platform=plat, is_authorized=is_authorized, bind=bind):
                return {"status": "error", "platform": plat, "error_msg": "保存授权状态失败，请稍后重试"}
            if is_authorized:
                return self._authorized_response(uid, plat, adapter.platform_name, bind)

            url_result = adapter.generate_authority_url(uid=uid)
            if not isinstance(url_result, dict) or url_result.get("status") != "success":
                message = url_result.get("error_msg") if isinstance(url_result, dict) else None
                return {"status": "error", "platform": plat, "error_msg": message or "生成授权链接失败，请稍后重试"}
            auth_url = url_result.get("auth_url") or url_result.get("short_url") or ""
            if not isinstance(auth_url, str) or not auth_url.strip():
                return {"status": "error", "platform": plat, "error_msg": "平台未返回有效授权链接"}
            return {
                "status": "success", "platform": plat, "platform_name": adapter.platform_name,
                "is_authorized": False, "bind": bind,
                "msg": f"您尚未在 {adapter.platform_name} 完成授权备案，请点击下方按钮前往授权",
                "auth_url": auth_url, "short_url": url_result.get("short_url") or auth_url,
                "we_app_info": url_result.get("we_app_info", {})
            }
        except Exception as exc:
            logger.error(f"[{plat}] 查询用户授权失败: {exc}")
            return {"status": "error", "platform": plat, "error_msg": "查询或保存授权状态失败，请稍后重试"}

    @staticmethod
    def _authorized_response(uid: str, platform: str, name: str, bind) -> Dict[str, Any]:
        return {
            "status": "success", "platform": platform, "platform_name": name,
            "is_authorized": True, "bind": bind,
            "msg": f"您已成功完成 {name} 渠道授权备案",
            "auth_url": "", "short_url": ""
        }
