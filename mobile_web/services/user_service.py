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
    def __init__(self, user_repo: Optional[UserRepository] = None, order_repo=None):
        self.repo = user_repo if user_repo is not None else UserRepository()
        self.order_repo = order_repo

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

    @staticmethod
    def _identity_complete(adapter, identity) -> bool:
        required = getattr(adapter, "required_identity_fields", ())
        return isinstance(identity, dict) and all(
            isinstance(identity.get(key), str) and identity[key].strip() for key in required
        )

    def get_promotion_context(self, uid: str, platform: str = "pdd") -> Dict[str, Any]:
        """内部可信归因上下文；查询授权不额外生成邀请链接。"""
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
            saved = (user.get("auth_status") or {}).get(plat) or {}
            identity = saved.get("identity") or {}
            if (saved.get("is_authorized") is True and self._auth_is_fresh(saved)
                    and self._identity_complete(adapter, identity) and identity.get("external_id", uid) == uid):
                return dict(self._authorized_response(uid, plat, adapter.platform_name, saved.get("bind", 0)),
                            identity=UserRepository.clean_platform_identity(identity))

            response = adapter.check_authority(uid=uid)
            if not isinstance(response, dict) or response.get("status") != "success":
                return {"status": "error", "platform": plat,
                        "error_code": response.get("error_code", "authorization_failed") if isinstance(response, dict) else "authorization_failed",
                        "error_msg": response.get("error_msg", "平台授权状态查询失败，请稍后重试") if isinstance(response, dict) else "平台授权状态响应无效"}
            if not isinstance(response.get("is_authorized"), bool):
                return {"status": "error", "platform": plat, "error_msg": "平台授权状态响应无效"}
            authorized = response["is_authorized"]
            identity = UserRepository.clean_platform_identity(response.get("identity") or {}) if authorized else {}
            if authorized and not self._identity_complete(adapter, identity):
                return {"status": "error", "error_code": "invalid_platform_identity", "platform": plat,
                        "error_msg": "平台未返回完整的用户渠道身份，请重新验证授权"}
            if identity.get("external_id", uid) != uid:
                return {"status": "error", "platform": plat, "error_msg": "平台渠道身份与当前用户不符"}
            bind = response.get("bind", 0)
            extra = {"identity": identity} if identity or getattr(adapter, "required_identity_fields", ()) else {}
            if not self.repo.update_auth_status(uid=uid, platform=plat, is_authorized=authorized, bind=bind,
                                                **({"extra": extra} if extra else {})):
                return {"status": "error", "platform": plat, "error_msg": "保存授权状态失败，请稍后重试"}
            if identity:
                self._assign_unmatched_orders(plat, identity, uid)
            if authorized:
                return dict(self._authorized_response(uid, plat, adapter.platform_name, bind), identity=identity)
            return {"status": "success", "platform": plat, "platform_name": adapter.platform_name,
                    "is_authorized": False, "bind": bind, "identity": {}}
        except Exception as exc:
            logger.error(f"[{plat}] 查询用户授权失败: {type(exc).__name__}")
            return {"status": "error", "platform": plat, "error_msg": "查询或保存授权状态失败，请稍后重试"}

    def _assign_unmatched_orders(self, platform: str, identity: dict, uid: str) -> None:
        try:
            if self.order_repo is None:
                from repositories.order_repo import OrderRepository
                self.order_repo = OrderRepository()
            # 查询订单只需平台声明的归因标识，external_id 是核验上下文而非订单回传。
            adapter = get_adapter(platform)
            attribution = {key: identity[key] for key in getattr(adapter, "required_identity_fields", ())}
            if attribution:
                self.order_repo.assign_unmatched_orders(platform, attribution, uid)
        except Exception as exc:
            logger.warning(f"[{platform}] 未匹配订单归因回填待重试: {type(exc).__name__}")

    def check_and_get_auth(self, uid: str, platform: str = "pdd") -> Dict[str, Any]:
        """只缓存已确认且完整的授权；上游故障不等价于用户未授权。"""
        result = self.get_promotion_context(uid, platform)
        result.pop("identity", None)
        if result.get("status") != "success" or result.get("is_authorized"):
            return result
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        try:
            url_result = adapter.generate_authority_url(uid=uid)
            if not isinstance(url_result, dict) or url_result.get("status") != "success":
                message = url_result.get("error_msg") if isinstance(url_result, dict) else None
                return {"status": "error", "platform": plat, "error_msg": message or "生成授权链接失败，请稍后重试"}
            auth_url = url_result.get("auth_url") or url_result.get("short_url") or ""
            if not isinstance(auth_url, str) or not auth_url.strip():
                return {"status": "error", "platform": plat, "error_msg": "平台未返回有效授权链接"}
            result.update(msg=f"您尚未在 {adapter.platform_name} 完成授权备案，请点击下方按钮前往授权",
                          auth_url=auth_url, short_url=url_result.get("short_url") or auth_url,
                          we_app_info=url_result.get("we_app_info", {}))
            return result
        except Exception as exc:
            logger.error(f"[{plat}] 生成用户授权链接失败: {type(exc).__name__}")
            return {"status": "error", "platform": plat, "error_msg": "生成授权链接失败，请稍后重试"}

    @staticmethod
    def _authorized_response(uid: str, platform: str, name: str, bind) -> Dict[str, Any]:
        return {
            "status": "success", "platform": platform, "platform_name": name,
            "is_authorized": True, "bind": bind,
            "msg": f"您已成功完成 {name} 渠道授权备案",
            "auth_url": "", "short_url": ""
        }
