# -*- coding: utf-8 -*-
"""拼多多多人团互助服务层：同步二维码解码与 307 免登录核验 + 后台定时大模型识图任务。"""
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import cv2
import numpy as np
import requests
from PIL import Image

from config import logger
from infrastructure.logging_utils import format_exc_summary
from models.group_help import (
    MAX_TAKEDOWNS_PER_WINDOW,
    TAKEDOWN_RATE_WINDOW_SECONDS,
    GroupHelpAuthRequiredError,
    GroupHelpPermissionError,
    format_group_doc,
    is_pdd_qr_url,
    is_relist_feedback,
    is_takedown_feedback,
    is_valid_group_order_id,
    parse_group_redirect_url,
    validate_and_normalize_parsed_poster,
)
from repositories.group_help_repo import GroupHelpRepository
from repositories.user_repo import UserRepository

_APP_DIR = Path(__file__).resolve().parent.parent
_POSTER_DIR = _APP_DIR / "runtime" / "group_posters"
_PROMPT_FILE = _APP_DIR / "prompt" / "多人团海报识别.txt"
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def decode_qr_from_image_bytes(image_bytes: bytes) -> str:
    """
    对整张图片进行全局二维码识别（不假设二维码固定在某角落）：
    1. 先对全图做直接识别与二值化识别；
    2. 若二维码在整图中占比过小，使用覆盖全图 100% 区域的均匀重叠滑动窗口（上/中/下 × 左/中/右）扫描全图。
    """
    if not image_bytes:
        return ""
    try:
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise ValueError("无法读取图片文件，请上传有效的 JPG/PNG/WEBP 格式图片") from exc

    rgb_arr = np.array(pil_img)
    gray_full = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)
    h, w = gray_full.shape[:2]
    if h == 0 or w == 0:
        return ""

    detector = cv2.QRCodeDetector()

    # 1. 全图优先，随后按均匀重叠网格覆盖全图所有区域（上/中/下、左/中/右一视同仁，不绑定特定角落）
    regions = [gray_full]
    if h >= 200 and w >= 200:
        for y0, y1 in ((0.0, 0.6), (0.2, 0.8), (0.4, 1.0)):
            band = gray_full[int(h * y0) : int(h * y1), :]
            regions.append(band)
            for x0, x1 in ((0.0, 0.65), (0.2, 0.8), (0.35, 1.0)):
                regions.append(band[:, int(w * x0) : int(w * x1)])

    for reg in regions:
        if reg.size == 0:
            continue
        rh, rw = reg.shape[:2]
        scales = [reg]
        if max(rh, rw) < 1000:
            scales.append(
                cv2.resize(reg, (int(rw * 1.5), int(rh * 1.5)), interpolation=cv2.INTER_CUBIC)
            )
        for img in scales:
            try:
                text, _, _ = detector.detectAndDecode(img)
                if isinstance(text, str) and text.strip():
                    return text.strip()
            except Exception:
                pass
            try:
                _, thresh = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
                text, _, _ = detector.detectAndDecode(thresh)
                if isinstance(text, str) and text.strip():
                    return text.strip()
            except Exception:
                pass

    return ""


def resolve_pdd_qr_link(qr_url: str, http_get: Optional[Callable] = None) -> Dict[str, str]:
    """免登录核验二维码中的拼多多短链，通过 307 重定向提取 group_order_id 等关键元数据。"""
    clean_url = str(qr_url or "").strip()
    if not is_pdd_qr_url(clean_url):
        raise ValueError("二维码内容不是拼多多官方链接，请上传拼多多【多人团邀请分享图】")

    # 若二维码本身就是包含 group_order_id 的长链接，直接解析
    if "group_order_id=" in clean_url:
        parsed = parse_group_redirect_url(clean_url)
        parsed["short_url"] = clean_url
        return parsed

    getter = http_get or requests.get
    try:
        resp = getter(clean_url, headers=_DEFAULT_HEADERS, allow_redirects=False, timeout=8)
    except Exception as exc:
        raise ValueError(f"核验拼团短链网络超时，请稍后重试 ({type(exc).__name__})") from exc

    location = resp.headers.get("Location") or resp.headers.get("location") or ""
    if resp.status_code in (301, 302, 303, 307, 308) and location:
        parsed = parse_group_redirect_url(location)
        parsed["short_url"] = clean_url
        return parsed

    # 如果未返回重定向，检查响应体中是否带有 href
    m_href = re.search(r'href=["\'](https?://[^"\']+group_order_id=\d+[^"\']*)["\']', resp.text or "")
    if m_href:
        parsed = parse_group_redirect_url(m_href.group(1).replace("&amp;", "&"))
        parsed["short_url"] = clean_url
        return parsed

    raise ValueError("该二维码链接无法解析出拼团单号，请确认上传的是【拼多多多人团邀请好友海报】")


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """从大模型输出文本中稳健提取 JSON 字典。"""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("模型返回内容为空")
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        obj = json.loads(cleaned[start : end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("无法从模型输出中解析合法 JSON 对象")


class GroupHelpService:
    """多人团互助核心业务服务。"""

    def __init__(
        self,
        repo: Optional[GroupHelpRepository] = None,
        poster_dir: Optional[Path] = None,
        http_get: Optional[Callable] = None,
        ai_generator: Optional[Callable] = None,
        user_repo: Optional[UserRepository] = None,
    ):
        self.repo = repo if repo is not None else GroupHelpRepository()
        if user_repo is not None:
            self.user_repo = user_repo
        elif getattr(self.repo, "_db", None) is not None:
            self.user_repo = UserRepository(db_instance=self.repo._db)
        else:
            self.user_repo = UserRepository()
        self.poster_dir = Path(poster_dir) if poster_dir is not None else _POSTER_DIR
        self._http_get = http_get
        self._ai_generator = ai_generator
        self._wake_callback: Optional[Callable[[], None]] = None
        self._takedown_ts: Dict[str, list] = {}

    def set_wake_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._wake_callback = callback

    @staticmethod
    def _has_owner_binding(doc: Dict[str, Any]) -> bool:
        return bool(
            str(doc.get("owner_token") or "").strip()
            or str(doc.get("owner_uid") or "").strip()
        )

    @staticmethod
    def _is_group_owner(doc: Dict[str, Any], owner_token: str = "", viewer_uid: str = "") -> bool:
        stored_uid = str(doc.get("owner_uid") or "").strip()
        clean_uid = str(viewer_uid or "").strip()
        if stored_uid and clean_uid and stored_uid == clean_uid:
            return True
        stored = str(doc.get("owner_token") or "").strip()
        candidate = str(owner_token or "").strip()
        return bool(stored and candidate and hmac.compare_digest(stored, candidate))

    def get_viewer_guard(
        self,
        viewer_uid: Optional[str] = None,
        token_expired: bool = False,
    ) -> Dict[str, Any]:
        """返回当前访问者在多人团互助中的登录状态与下架权限信息。"""
        clean_uid = str(viewer_uid or "").strip()
        if not clean_uid:
            return {
                "logged_in": False,
                "token_expired": bool(token_expired),
                "uid": "",
                "phone": "",
                "can_takedown": False,
                "strikes": 0,
                "reason": "",
            }
        guard = self.user_repo.get_group_help_guard(clean_uid)
        if not guard:
            return {
                "logged_in": False,
                "token_expired": True,
                "uid": "",
                "phone": "",
                "can_takedown": False,
                "strikes": 0,
                "reason": "",
            }
        return {**guard, "token_expired": False}

    def _save_poster_image(self, group_order_id: str, image_bytes: bytes) -> Path:
        self.poster_dir.mkdir(parents=True, exist_ok=True)
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        max_dim = 1280
        w, h = pil_img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            pil_img = pil_img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        target_path = self.poster_dir / f"{group_order_id}.jpg"
        pil_img.save(target_path, format="JPEG", quality=85, optimize=True)
        return target_path

    def get_poster_file_path(self, group_order_id: str) -> Optional[Path]:
        if not is_valid_group_order_id(group_order_id):
            return None
        gid = str(group_order_id).strip()
        doc = self.repo.get_by_group_order_id(gid)
        # 仅对外展示已通过合规审核与字段校验的拼团海报，未审核或审核失败的图片保留在服务器磁盘但对外不可访问
        if not doc or doc.get("parse_status") != "parsed":
            return None
        path = self.poster_dir / f"{gid}.jpg"
        return path if path.is_file() else None

    def _format_with_matched_group(self, doc: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        current_ts = float(now if now is not None else time.time())
        formatted = format_group_doc(doc, now=current_ts)
        matched_gid = str(doc.get("matched_group_order_id") or "").strip()
        if formatted["group_status"] == "pending_confirm" and matched_gid:
            matched_doc = self.repo.get_by_group_order_id(matched_gid)
            is_match_still_active = bool(
                matched_doc
                and matched_doc.get("parse_status") == "parsed"
                and matched_doc.get("group_status") == "active"
                and int(matched_doc.get("remain_people") or 0) > 0
                and float(matched_doc.get("expires_at") or 0) > current_ts
            )
            if is_match_still_active:
                formatted["matched_group"] = format_group_doc(matched_doc, now=current_ts)
            else:
                # 若此前匹配到的在拼团已满或已过期，自动将该用户的团转正上架
                promoted = self.repo.apply_user_feedback(
                    group_order_id=formatted["group_order_id"],
                    action="confirm_publish",
                    now=current_ts,
                )
                if promoted:
                    formatted = format_group_doc(promoted, now=current_ts)
        return formatted

    def submit_poster_image(
        self,
        image_bytes: bytes,
        owner_token: str = "",
        viewer_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        第一步（同步执行）：
        1. 校验图片体积与格式；
        2. 识别图片中的二维码；
        3. 免登录请求 307 重定向提取 group_order_id 等关键字段；
        4. 为新团签发随机 owner_token，保存海报并在 pdd_multi_groups 表写入待解析记录。
        """
        if not image_bytes:
            raise ValueError("上传的图片内容为空")
        if len(image_bytes) > _MAX_UPLOAD_BYTES:
            raise ValueError("图片大小不能超过 10MB")

        qr_text = decode_qr_from_image_bytes(image_bytes)
        if not qr_text:
            raise ValueError("未在图片中识别到二维码，请上传拼多多生成的【多人团分享海报图】（需包含右下角二维码）")

        link_meta = resolve_pdd_qr_link(qr_text, http_get=self._http_get)
        gid = link_meta["group_order_id"]

        now = time.time()
        existing = self.repo.get_by_group_order_id(gid)
        issued_owner_token = ""
        clean_uid = str(viewer_uid or "").strip()

        if existing is not None:
            if float(existing.get("expires_at") or 0) <= now:
                raise ValueError(
                    "该拼团已超过有效期失效（拼多多拼团仅24小时有效），请在拼多多重新发起新团后再上传最新海报"
                )
            if self._is_group_owner(existing, owner_token=owner_token, viewer_uid=clean_uid) and owner_token:
                issued_owner_token = str(owner_token).strip()
        else:
            issued_owner_token = secrets.token_hex(12)

        # 每次上传的图片都必须保存并进入 pending 重新走大模型审核（严禁直接复用旧审核结果导致局部二维码图蒙混过关）
        poster_path = self._save_poster_image(gid, image_bytes)
        record_input: Dict[str, Any] = {
            **link_meta,
            "poster_path": str(poster_path),
            "created_at": now,
        }
        if existing is None:
            record_input["owner_token"] = issued_owner_token
            record_input["owner_uid"] = clean_uid

        doc, is_new = self.repo.create_or_refresh_initial_group(record_input)

        # 唤醒后台定时任务立即处理
        if callable(self._wake_callback):
            try:
                self._wake_callback()
            except Exception:
                pass

        formatted = self._format_with_matched_group(doc, now=now)
        msg = "✅ 二维码核验通过，已加入【我的团】！正在自动审核并识别商品关键信息（约 10~20 秒），完成后立即反馈。"

        result: Dict[str, Any] = {
            "status": "success",
            "is_new": is_new,
            "message": msg,
            "data": formatted,
        }
        if issued_owner_token:
            result["owner_token"] = issued_owner_token
        return result

    def _load_prompt(self) -> str:
        if _PROMPT_FILE.is_file():
            try:
                return _PROMPT_FILE.read_text(encoding="utf-8-sig").strip()
            except Exception:
                pass
        return (
            "请审核并识别这张拼多多多人团分享海报图中的合规性(is_compliant)、是否完整海报(is_group_poster)、"
            "商品名称(goods_name)、搜索关键词数组(keywords)、拼团价(group_price)、几人团(total_people)、"
            "还差几人(remain_people)、剩余时间(remaining_time, HH:MM:SS格式)，严格只输出 JSON 对象。"
        )

    def parse_single_pending_group(self, doc: Dict[str, Any]) -> bool:
        """调用多模态大模型解析单条待处理的多人团海报图。"""
        gid = str(doc.get("group_order_id") or "").strip()
        if not gid:
            return False

        poster_path_str = str(doc.get("poster_path") or "").strip()
        poster_path = Path(poster_path_str) if poster_path_str else (self.poster_dir / f"{gid}.jpg")
        if not poster_path.is_file():
            self.repo.mark_parse_failed(gid, "海报图片文件不存在", permanent=True)
            return False

        prompt = self._load_prompt()
        generator = self._ai_generator
        if generator is None:
            from infrastructure.ai_api.model_api import generate_content
            generator = generate_content

        try:
            res = generator(
                prompt=prompt,
                file_paths=[str(poster_path)],
                preset_model_group="medium",
                max_retries_per_model=2,
                timeout=90,
            )
            if not isinstance(res, dict) or res.get("status") != "✅ 成功":
                err_hist = "; ".join((res or {}).get("error_history") or []) or "大模型调用未返回成功状态"
                self.repo.mark_parse_failed(gid, err_hist, permanent=False)
                logger.warning("【多人团识图告警】团单[%s] 大模型识图失败 | 原因<%s>", gid, err_hist[:200])
                return False

            parsed_json = extract_json_object(res.get("content", ""))
            try:
                validated_data = validate_and_normalize_parsed_poster(parsed_json)
            except ValueError as val_err:
                reason = str(val_err)
                # 保留磁盘上的原始图片供后续复盘分析，仅在数据库标记 failed 阻断外部访问
                self.repo.mark_parse_failed(gid, reason, permanent=True)
                logger.info("【多人团识图拦截】团单[%s] 图片未通过审核拒绝上线 | 原因<%s>", gid, reason)
                return False

            self.repo.mark_parse_succeeded(gid, validated_data)
            logger.info(
                "【多人团识图完成】团单[%s] 商品[%s] 价格[%s] 差额[%s人] 剩余秒数[%s]",
                gid,
                validated_data.get("goods_name"),
                validated_data.get("group_price"),
                validated_data.get("remain_people"),
                validated_data.get("remaining_seconds"),
            )
            return True
        except Exception as exc:
            self.repo.mark_parse_failed(gid, format_exc_summary(exc), permanent=False)
            logger.warning("【多人团识图异常】团单[%s] 解析发生异常 | 异常<%s>", gid, format_exc_summary(exc))
            return False

    def process_pending_batch(self, batch_size: int = 2) -> Dict[str, int]:
        """定时任务单次执行入口：先清理超24h过期团，再批量解析待识别图片。"""
        expired_count = 0
        parsed_count = 0
        failed_count = 0
        try:
            expired_count = self.repo.expire_outdated_groups()
        except Exception as exc:
            logger.warning("【多人团定时任务】自动下线过期团异常 | 异常<%s>", format_exc_summary(exc))

        try:
            pending_list = self.repo.fetch_pending_for_parse(limit=batch_size)
        except Exception as exc:
            logger.warning("【多人团定时任务】拉取待解析队列异常 | 异常<%s>", format_exc_summary(exc))
            return {"expired": expired_count, "parsed": 0, "failed": 0}

        for doc in pending_list:
            ok = self.parse_single_pending_group(doc)
            if ok:
                parsed_count += 1
            else:
                failed_count += 1

        return {"expired": expired_count, "parsed": parsed_count, "failed": failed_count}

    def list_groups(
        self,
        keyword: str = "",
        filter_mode: str = "active",
        offset: int = 0,
        limit: int = 20,
        viewer_uid: Optional[str] = None,
        token_expired: bool = False,
    ) -> Dict[str, Any]:
        now = time.time()
        raw = self.repo.search_groups(
            keyword=keyword,
            filter_mode=filter_mode,
            offset=offset,
            limit=limit,
            now=now,
        )
        return {
            "items": [format_group_doc(item, now=now) for item in raw["items"]],
            "total": raw["total"],
            "has_more": raw["has_more"],
            "pending_count": raw["pending_count"],
            "viewer_guard": self.get_viewer_guard(
                viewer_uid=viewer_uid, token_expired=token_expired
            ),
        }

    def get_group_detail(self, group_order_id: str) -> Optional[Dict[str, Any]]:
        doc = self.repo.get_by_group_order_id(group_order_id)
        if not doc:
            return None
        return self._format_with_matched_group(doc)

    def record_jump_click(self, group_order_id: str) -> Optional[Dict[str, Any]]:
        doc = self.repo.increment_click(group_order_id)
        if not doc:
            return None
        return format_group_doc(doc)

    def submit_feedback_detailed(
        self,
        group_order_id: str,
        action: str,
        remain_people: Optional[int] = None,
        viewer_uid: Optional[str] = None,
        owner_token: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        统一处理多人团状态反馈与极简防破坏闭环：
        1. 团长本人（凭本地 owner_token）可免登录自由管理自己的团；
        2. 非团长执行会导致拼团下架的操作（已满/已失效/仅剩1人时点我已参团）必须登录且未被停用下架权限；
        3. 非下架类操作（如多人差额时点“我已参团”人数-1、向上校准人数）免登录可用，被禁下架权限的用户仍可使用；
        4. 当团长本人点击「未满恢复」将已下架的拼团恢复上架时，自动对最近一次下架该团的非团长账号计数 +1（满2次停用下架权限）。
        """
        now = time.time()
        existing = self.repo.get_by_group_order_id(group_order_id)
        if not existing:
            return None

        act = str(action or "").strip()
        if float(existing.get("expires_at") or 0) <= now:
            expired_doc = self.repo.apply_user_feedback(
                group_order_id=group_order_id,
                action=act,
                remain_people=remain_people,
                now=now,
            )
            if not expired_doc:
                return None
            return {
                "data": format_group_doc(expired_doc, now=now),
                "penalized_saboteur": False,
                "viewer_guard": self.get_viewer_guard(viewer_uid),
            }

        guard = self.get_viewer_guard(viewer_uid)
        verified_uid = guard["uid"] if guard["logged_in"] else ""
        is_owner = self._is_group_owner(existing, owner_token=owner_token, viewer_uid=verified_uid)
        has_owner = self._has_owner_binding(existing)

        if act == "confirm_publish":
            if has_owner and not is_owner:
                raise GroupHelpPermissionError(
                    "仅发团团长本人可确认上架该拼团", error_code="owner_only", status_code=403
                )
            updated = self.repo.apply_user_feedback(
                group_order_id=group_order_id,
                action=act,
                remain_people=remain_people,
                now=now,
                operator_uid=verified_uid,
                is_owner=is_owner,
            )
            return (
                {
                    "data": format_group_doc(updated, now=now),
                    "penalized_saboteur": False,
                    "viewer_guard": guard,
                }
                if updated
                else None
            )

        if is_relist_feedback(existing, act, remain_people):
            if has_owner and not is_owner:
                raise GroupHelpPermissionError(
                    "该拼团已下架，如实际未满仅限发团团长本人恢复上架",
                    error_code="owner_only",
                    status_code=403,
                )
            updated = self.repo.apply_user_feedback(
                group_order_id=group_order_id,
                action=act,
                remain_people=remain_people,
                now=now,
                operator_uid=verified_uid,
                is_owner=is_owner,
            )
            penalized_saboteur = False
            if is_owner and updated and updated.get("group_status") == "active":
                saboteur_uid = self.repo.pop_last_takedown_uid(
                    group_order_id, exclude_uid=verified_uid
                )
                if saboteur_uid:
                    self.user_repo.inc_false_group_takedown(saboteur_uid, group_order_id)
                    penalized_saboteur = True
            return (
                {
                    "data": format_group_doc(updated, now=now),
                    "penalized_saboteur": penalized_saboteur,
                    "viewer_guard": self.get_viewer_guard(verified_uid),
                }
                if updated
                else None
            )

        if is_takedown_feedback(existing, act, remain_people):
            recent_ts = []
            if not is_owner:
                if not guard["logged_in"]:
                    raise GroupHelpAuthRequiredError(
                        "为防止恶意乱标下架他人拼团，执行下架类反馈请先登录账号（10秒即可完成，参团与发团无需登录）"
                    )
                if not guard["can_takedown"]:
                    raise GroupHelpPermissionError(
                        guard["reason"], error_code="takedown_banned", status_code=403
                    )
                cutoff = now - TAKEDOWN_RATE_WINDOW_SECONDS
                recent_ts = [t for t in self._takedown_ts.get(verified_uid, []) if t >= cutoff]
                if len(recent_ts) >= MAX_TAKEDOWNS_PER_WINDOW:
                    self._takedown_ts[verified_uid] = recent_ts
                    raise GroupHelpPermissionError(
                        "您短时间内下架他人拼团过于频繁，请稍后再试",
                        error_code="takedown_rate_limited",
                        status_code=429,
                    )
            updated = self.repo.apply_user_feedback(
                group_order_id=group_order_id,
                action=act,
                remain_people=remain_people,
                now=now,
                operator_uid=verified_uid,
                is_owner=is_owner,
            )
            if not is_owner and verified_uid and updated:
                self._takedown_ts[verified_uid] = recent_ts + [now]
            return (
                {
                    "data": format_group_doc(updated, now=now),
                    "penalized_saboteur": False,
                    "viewer_guard": self.get_viewer_guard(verified_uid),
                }
                if updated
                else None
            )

        # 非下架类反馈（多人差额时点“我已参团”人数-1，或向上校准差额人数）：免登录可用，被禁下架权限用户仍可使用
        if act == "set_remain" and not is_owner and remain_people is not None and not isinstance(remain_people, bool):
            cur_rem = int(existing.get("remain_people") if existing.get("remain_people") is not None else 1)
            if 0 < int(remain_people) < cur_rem:
                raise ValueError("参团减少人数请直接点击「我已参团」")

        updated = self.repo.apply_user_feedback(
            group_order_id=group_order_id,
            action=act,
            remain_people=remain_people,
            now=now,
            operator_uid=verified_uid,
            is_owner=is_owner,
        )
        return (
            {
                "data": format_group_doc(updated, now=now),
                "penalized_saboteur": False,
                "viewer_guard": guard,
            }
            if updated
            else None
        )

    def submit_feedback(
        self,
        group_order_id: str,
        action: str,
        remain_people: Optional[int] = None,
        viewer_uid: Optional[str] = None,
        owner_token: str = "",
    ) -> Optional[Dict[str, Any]]:
        res = self.submit_feedback_detailed(
            group_order_id=group_order_id,
            action=action,
            remain_people=remain_people,
            viewer_uid=viewer_uid,
            owner_token=owner_token,
        )
        return res["data"] if res else None


class GroupHelpWorker:
    """独立后台定时任务线程：定期将 pending 状态的海报图送入大模型解析，并自动下线超24小时的团。"""

    def __init__(self, service: GroupHelpService, interval_seconds: float = 8.0):
        self.service = service
        self.interval_seconds = max(2.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.service.set_wake_callback(self.wake)

    def wake(self) -> None:
        self._wake_event.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="group-help-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("【多人团定时任务】后台识图与过期清理线程已启动 (周期 %.1fs)", self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._wake_event.set()
            self._thread.join(timeout=5.0)
        self._thread = None
        self._wake_event.clear()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self.interval_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.service.process_pending_batch(batch_size=2)
            except Exception as exc:
                logger.warning("【多人团定时任务】轮询执行异常 | 异常<%s>", format_exc_summary(exc))
