# -*- coding: utf-8 -*-
"""
用户与返利授权业务服务
处理推手手机号合规注册/登录、密码校验、时效令牌签发、敏感信息脱敏与渠道备案授权。
"""
import re
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from config import AUTH_STATUS_TTL, AUTH_NEGATIVE_STATUS_TTL
from models.product import normalize_platform
from models.user import normalize_promoter_id
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

    def get_numeric_promotion_context(self, uid: str, platform: str, default_promoter_id) -> Dict[str, Any]:
        """数字推广身份与渠道备案无关，已分配身份失败时不能变成站长身份。"""
        plat = normalize_platform(platform)
        try:
            default_id = normalize_promoter_id(default_promoter_id)
            promoter_id = self.repo.ensure_promoter_id(uid, reserved_ids=(default_id,))
        except Exception as exc:
            logger.warning("[%s] 读取数字推广身份失败: %s", plat, type(exc).__name__)
            return {"status": "error", "error_code": "promoter_identity_unavailable",
                    "error_msg": "无法确认本人推广身份，请稍后重试；未使用站长推广位代替"}
        return {"status": "success", "uid": uid, "promoter_id": promoter_id}

    @staticmethod
    def _auth_is_fresh(auth_status: dict, ttl: Optional[int] = None) -> bool:
        """Mongo 的 naive datetime 按 UTC 解读；无更新时间的旧记录必须复核。
        若 ttl 为 None，表示正向授权长期有效，不设人工定时过期，仅验证时间戳合法（非未来时间）。
        若 ttl 有值，则在 [0, ttl) 窗口内有效（如未授权状态按短缓存 AUTH_NEGATIVE_STATUS_TTL 防刷）。
        """
        updated_at = auth_status.get("updated_at")
        if isinstance(updated_at, datetime):
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            updated_at = updated_at.timestamp()
        if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
            return False
        age = time.time() - updated_at
        if ttl is None:
            return age >= -60
        return 0 <= age < ttl

    @staticmethod
    def _identity_complete(adapter, identity) -> bool:
        required = getattr(adapter, "required_identity_fields", ())
        return isinstance(identity, dict) and all(
            isinstance(identity.get(key), str) and identity[key].strip() not in ("", "0") for key in required
        )

    def get_promotion_context(
        self, uid: str, platform: str = "pdd", *, allow_remote: bool = True, force: bool = False,
    ) -> Dict[str, Any]:
        """转链只读本地资格；我的页可权威核验，均不额外生成邀请链接。"""
        plat = normalize_platform(platform)
        if not uid:
            return {"status": "error", "error_msg": "uid 不能为空"}
        adapter = get_adapter(plat)
        if not adapter.active:
            raise PlatformUnavailableError(f"{adapter.platform_name}暂未接入")
        if getattr(adapter, "requires_numeric_promoter_id", False) is True:
            return {"status": "success", "platform": plat, "platform_name": adapter.platform_name,
                    "authorization_required": False, "authorization_status": "not_required",
                    "is_authorized": False, "bind": 0, "identity": {}, "msg": "该平台无需授权备案"}
        try:
            user = self.repo.get_user_by_uid(uid)
            if not user:
                return {"status": "error", "error_msg": "推手用户不存在"}
            saved = (user.get("auth_status") or {}).get(plat) or {}
            identity = saved.get("identity") or {}
            if not isinstance(identity, dict):
                if not allow_remote:
                    return {"status": "error", "platform": plat, "error_code": "invalid_platform_identity",
                            "error_msg": "平台渠道身份格式无效，请重新验证授权"}
                identity = {}
            identity_matches = identity.get("external_id", uid) == uid
            if not identity_matches and not allow_remote:
                return {"status": "error", "platform": plat, "error_code": "IDENTITY_MISMATCH",
                        "error_msg": "平台渠道身份与当前用户不符"}
            if (not force and saved.get("is_authorized") is True and self._auth_is_fresh(saved)
                    and identity_matches and self._identity_complete(adapter, identity)):
                return dict(self._authorized_response(uid, plat, adapter.platform_name, saved.get("bind", 0)),
                            identity=UserRepository.clean_platform_identity(identity))
            negative_fresh = (saved.get("is_authorized") is False
                              and self._auth_is_fresh(saved, AUTH_NEGATIVE_STATUS_TTL))
            if not allow_remote or (not force and negative_fresh):
                return {"status": "success", "platform": plat, "platform_name": adapter.platform_name,
                        "is_authorized": False, "bind": 0, "identity": {},
                        "authorization_status": "unauthorized" if negative_fresh else "unknown"}

            started = time.perf_counter()
            try:
                response = adapter.check_authority(uid=uid)
            finally:
                logger.info("[authorization] platform=%s stage=remote_check elapsed_ms=%.1f",
                            plat, (time.perf_counter() - started) * 1000)
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
            if not self.repo.update_auth_status(uid=uid, platform=plat, is_authorized=authorized, bind=bind,
                                                extra={"identity": identity}):
                return {"status": "error", "platform": plat, "error_msg": "保存授权状态失败，请稍后重试"}
            if identity:
                self._assign_unmatched_orders(plat, identity, uid)
            if authorized:
                return dict(self._authorized_response(uid, plat, adapter.platform_name, bind), identity=identity)
            return {"status": "success", "platform": plat, "platform_name": adapter.platform_name,
                    "is_authorized": False, "bind": bind, "identity": {}, "authorization_status": "unauthorized"}
        except Exception as exc:
            logger.error(f"[{plat}] 查询用户授权失败: {type(exc).__name__}")
            return {"status": "error", "platform": plat, "error_msg": "查询或保存授权状态失败，请稍后重试"}

    def invalidate_promotion_authorization(self, uid: str, platform: str) -> bool:
        """只用于平台转链明确拒绝授权，普通供应商失败不能撤销本地资格。"""
        try:
            return bool(self.repo.update_auth_status(
                uid=uid, platform=normalize_platform(platform), is_authorized=False, bind=0,
                extra={"identity": {}},
            ))
        except Exception as exc:
            logger.warning("[%s] 保存转链授权失效状态失败: %s", platform, type(exc).__name__)
            return False

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

    def check_and_get_auth(
        self, uid: str, platform: str = "pdd", *, force: bool = False, check_only: bool = False,
    ) -> Dict[str, Any]:
        """获取授权入口或只核验状态；主动复查不反复生成邀请链接。"""
        result = self.get_promotion_context(uid, platform, force=force)
        result.pop("identity", None)
        if result.get("authorization_required") is False:
            return result
        if check_only:
            if result.get("status") == "success" and result.get("is_authorized") is False:
                result["msg"] = f"尚未在{result['platform_name']}完成渠道授权备案，请完成授权后重新校验"
            return result
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
