# -*- coding: utf-8 -*-
"""
用户数据仓储模块 (MongoDB: users 集合)
管理推手用户的手机号认证、密码哈希、专属 UID、邀请码及渠道授权状态。
"""
import uuid
import random
import string
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from repositories.base_repo import BaseRepository
from auth import hash_password, verify_password
from config import logger
from models.product import normalize_platform

DEFAULT_AVATAR = "https://img.pddpic.com/mms-avatar/default_avatar.png"


class UserRepository(BaseRepository):
    COLLECTION_NAME = "users"

    def _init_indexes(self):
        try:
            self.col.create_index([("uid", 1)], unique=True)
            self.col.create_index([("phone", 1)], unique=True, sparse=True)
            self.col.create_index([("invite_code", 1)], unique=True, sparse=True)
            self.col.create_index([("created_at", -1)])
            return True
        except Exception as e:
            logger.warning(f"用户集合索引创建提示: {e}")
            return False

    @staticmethod
    def generate_uid() -> str:
        """生成带前缀的高可读性推手唯一标识，如 u_9b2e61a4"""
        token = uuid.uuid4().hex[:8]
        return f"u_{token}"

    def generate_unique_invite_code(self) -> str:
        """生成唯一的 6 位大写字母/数字邀请码"""
        chars = string.ascii_uppercase + "23456789"
        for _ in range(10):
            code = "INV_" + "".join(random.choices(chars, k=6))
            if not self.col.find_one({"invite_code": code}):
                return code
        return f"INV_{uuid.uuid4().hex[:6].upper()}"

    def get_user_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        """按 uid 查询用户"""
        if not uid:
            return None
        doc = self.col.find_one({"uid": str(uid).strip()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def get_user_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """按手机号查询用户"""
        if not phone:
            return None
        doc = self.col.find_one({"phone": str(phone).strip()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def get_user_by_invite_code(self, invite_code: str) -> Optional[Dict[str, Any]]:
        """按邀请码查询用户"""
        if not invite_code:
            return None
        doc = self.col.find_one({"invite_code": str(invite_code).strip().upper()})
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def register_or_login(
        self,
        phone: str,
        password: str,
        nickname: Optional[str] = None,
        invite_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        手机号注册登录一体化：
        - 若手机号已存在：校验密码，成功则登录并平滑升级旧哈希；
        - 若手机号未存在：自动注册新账号，绑定邀请码，直接登录。
        """
        phone = str(phone).strip()
        now = datetime.now(timezone.utc)

        existing = self.get_user_by_phone(phone)
        if existing:
            stored_hash = existing.get("password_hash")
            if not verify_password(password, stored_hash):
                raise ValueError("手机号已注册，输入的密码不正确")

            update_fields: Dict[str, Any] = {"last_login_at": now, "last_active_at": now}
            if not str(stored_hash or "").startswith("pbkdf2_sha256$"):
                update_fields["password_hash"] = hash_password(password)

            self.col.update_one({"phone": phone}, {"$set": update_fields})
            existing["last_login_at"] = now
            existing["last_active_at"] = now
            existing.pop("password_hash", None)
            return existing

        # 新用户注册
        pwd_hash = hash_password(password)
        new_uid = self.generate_uid()
        my_invite_code = self.generate_unique_invite_code()
        final_nickname = nickname.strip() if (nickname and nickname.strip()) else f"推手_{phone[-4:]}"

        invited_by = None
        if invite_code and invite_code.strip():
            inviter = self.get_user_by_invite_code(invite_code.strip())
            if inviter:
                invited_by = inviter.get("uid")

        user_doc = {
            "uid": new_uid,
            "phone": phone,
            "password_hash": pwd_hash,
            "nickname": final_nickname,
            "avatar": DEFAULT_AVATAR,
            "invite_code": my_invite_code,
            "invited_by": invited_by,
            "created_at": now,
            "last_login_at": now,
            "last_active_at": now,
            "balance": 0.0,
            "total_rebate": 0.0,
            "pending_rebate": 0.0,
            "auth_status": {
                "pdd": {"is_authorized": False, "bind": 0, "updated_at": None},
                "taobao": {"is_authorized": False, "bind": 0, "updated_at": None},
                "jd": {"is_authorized": False, "bind": 0, "updated_at": None},
            },
            "meta": {}
        }

        self.col.insert_one(user_doc)
        user_doc.pop("_id", None)
        user_doc.pop("password_hash", None)
        logger.info(f"新用户注册成功: phone={phone}, uid={new_uid}, invite_code={my_invite_code}")
        return user_doc

    def create_user(self, uid: Optional[str] = None, nickname: Optional[str] = None) -> Dict[str, Any]:
        """访客用户初始化"""
        now = datetime.now(timezone.utc)
        final_uid = str(uid).strip() if uid else self.generate_uid()
        final_nickname = nickname or f"推手_{final_uid[-4:]}"
        my_invite_code = self.generate_unique_invite_code()

        user_doc = {
            "uid": final_uid,
            "phone": None,
            "password_hash": None,
            "nickname": final_nickname,
            "avatar": DEFAULT_AVATAR,
            "invite_code": my_invite_code,
            "invited_by": None,
            "created_at": now,
            "last_active_at": now,
            "balance": 0.0,
            "total_rebate": 0.0,
            "pending_rebate": 0.0,
            "auth_status": {
                "pdd": {"is_authorized": False, "bind": 0, "updated_at": None},
                "taobao": {"is_authorized": False, "bind": 0, "updated_at": None},
                "jd": {"is_authorized": False, "bind": 0, "updated_at": None},
            },
            "meta": {}
        }

        try:
            self.col.insert_one(user_doc)
            user_doc.pop("_id", None)
            return user_doc
        except Exception:
            existing = self.get_user_by_uid(final_uid)
            if existing:
                existing.pop("password_hash", None)
                return existing
            raise

    def get_or_create_user(self, uid: Optional[str] = None) -> Dict[str, Any]:
        """按 uid 获取，不存在则创建"""
        if uid:
            user = self.get_user_by_uid(uid)
            if user:
                user.pop("password_hash", None)
                return user
        return self.create_user(uid=uid)

    def update_auth_status(
        self,
        uid: str,
        platform: str,
        is_authorized: bool,
        bind: int = 0,
        extra: Optional[dict] = None
    ) -> bool:
        """更新推手在指定平台的渠道备案授权状态"""
        now = datetime.now(timezone.utc)
        plat = normalize_platform(platform)
        if len(plat) > 32 or not plat.isascii() or not plat.replace("_", "a").isalnum() or not plat[0].isalpha():
            raise ValueError("无效的平台代码")
        field_prefix = f"auth_status.{plat}"

        update_fields = {
            f"{field_prefix}.is_authorized": bool(is_authorized),
            f"{field_prefix}.bind": int(bind),
            f"{field_prefix}.updated_at": now,
            "last_active_at": now
        }
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                update_fields[f"{field_prefix}.{k}"] = v

        res = self.col.update_one(
            {"uid": str(uid).strip()},
            {"$set": update_fields}
        )
        return res.modified_count > 0 or res.matched_count > 0
