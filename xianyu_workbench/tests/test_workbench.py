# -*- coding: utf-8 -*-
"""帮拍选品工作台端到端离线单元测试（平台无关核心 + 可插拔渠道 + 锁隔离 + 兼容迁移）。"""
from __future__ import annotations

import base64
from copy import deepcopy
import datetime
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient
from PIL import Image
from pymongo.errors import DuplicateKeyError
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from xianyu_workbench.api import create_app
from xianyu_workbench.auth import hash_password
from xianyu_workbench.channel_service import ChannelService
from xianyu_workbench.channels import ChannelRegistry
from xianyu_workbench.channels.base import (
    ChannelAccountStatus,
    ChannelCapabilities,
    ChannelSyncResult,
    QrLoginSnapshot,
    SellingChannel,
)
from xianyu_workbench.client import session as xy_session
from xianyu_workbench.config import WorkbenchConfig
from xianyu_workbench.repo import WorkbenchRepository
from xianyu_workbench.run import cmd_create_user, cmd_list_users, cmd_reset_password
from xianyu_workbench.service import WorkbenchService


class _MemoryCursor:
    def __init__(self, docs):
        self.docs = list(docs)
        self._skip = 0
        self._limit = None

    def sort(self, spec):
        def _key_fn(doc, field):
            val = doc.get(field)
            if val is None:
                return (0, "")
            if isinstance(val, datetime.datetime):
                dt = val.replace(tzinfo=datetime.timezone.utc) if val.tzinfo is None else val.astimezone(datetime.timezone.utc)
                return (1, dt.timestamp())
            if isinstance(val, bool):
                return (1, int(val))
            if isinstance(val, (int, float)):
                return (1, float(val))
            return (1, str(val))

        for field, direction in reversed(spec):
            self.docs.sort(key=lambda d, f=field: _key_fn(d, f), reverse=direction < 0)
        return self

    def skip(self, count):
        self._skip = int(count or 0)
        return self

    def limit(self, count):
        self._limit = int(count)
        return self

    def __iter__(self):
        end = None if self._limit is None else self._skip + self._limit
        return iter(self.docs[self._skip:end])


def _match_query(doc, query):
    if not query:
        return True
    for k, v in query.items():
        if k == "$or":
            if not any(_match_query(doc, sub) for sub in v):
                return False
            continue
        val = doc.get(k)
        if isinstance(v, dict):
            for op, target in v.items():
                if op == "$in":
                    if val not in target:
                        return False
                elif op == "$gte":
                    if val is None or val < target:
                        return False
                else:
                    return False
        else:
            if val != v:
                return False
    return True


def _apply_update(target, update_doc):
    for k, v in (update_doc.get("$set") or {}).items():
        target[k] = deepcopy(v)


class _MemoryCollection:
    def __init__(self, unique_keys=()):
        self.docs = []
        self.indexes = []
        self._unique_keys = list(unique_keys)

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))
        if kwargs.get("unique"):
            fields = tuple(k for k, _ in keys)
            if fields not in self._unique_keys:
                self._unique_keys.append(fields)

    def _check_unique(self, candidate, skip_idx=None):
        for fields in self._unique_keys:
            key_tuple = tuple(candidate.get(f) for f in fields)
            for idx, existing in enumerate(self.docs):
                if idx == skip_idx:
                    continue
                if tuple(existing.get(f) for f in fields) == key_tuple:
                    raise DuplicateKeyError(f"duplicate key on {fields}")

    def find_one(self, query=None, projection=None):
        for doc in self.docs:
            if _match_query(doc, query or {}):
                return self._project(doc, projection)
        return None

    def find(self, query=None, projection=None):
        matched = [self._project(doc, projection) for doc in self.docs if _match_query(doc, query or {})]
        return _MemoryCursor(matched)

    def count_documents(self, query=None):
        return sum(1 for doc in self.docs if _match_query(doc, query or {}))

    def insert_one(self, doc):
        stored = deepcopy(doc)
        self._check_unique(stored)
        stored.setdefault("_id", f"id_{len(self.docs) + 1}")
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def update_one(self, flt, update_doc, upsert=False):
        for idx, doc in enumerate(self.docs):
            if _match_query(doc, flt):
                _apply_update(doc, update_doc)
                self._check_unique(doc, skip_idx=idx)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            new_doc = deepcopy(flt)
            for k, v in (update_doc.get("$setOnInsert") or {}).items():
                new_doc[k] = deepcopy(v)
            _apply_update(new_doc, update_doc)
            self._check_unique(new_doc)
            new_doc.setdefault("_id", f"id_{len(self.docs) + 1}")
            self.docs.append(new_doc)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=new_doc["_id"])
        return SimpleNamespace(matched_count=0, modified_count=0)

    @staticmethod
    def _project(doc, projection):
        out = deepcopy(doc)
        if projection is None:
            return out
        if projection == {"_id": 0}:
            out.pop("_id", None)
            return out
        include_keys = [k for k, v in projection.items() if v]
        exclude_keys = [k for k, v in projection.items() if not v]
        if include_keys:
            res = {k: out[k] for k in include_keys if k in out}
            if projection.get("_id", 1) and "_id" in out:
                res["_id"] = out["_id"]
            return res
        for k in exclude_keys:
            out.pop(k, None)
        return out


class _MemoryDB:
    def __init__(self):
        self.cols = {
            "xy_users": _MemoryCollection(),
            "xy_listings": _MemoryCollection(),
            "xy_channel_accounts": _MemoryCollection(),
            "xy_channel_selling": _MemoryCollection(),
            "favorites": _MemoryCollection(),
            "products": _MemoryCollection(),
            "xianyu_deal_evaluations": _MemoryCollection(),
            "xianyu_items": _MemoryCollection(),
            "users": _MemoryCollection(),
        }

    def get_collection(self, name):
        if name not in self.cols:
            self.cols[name] = _MemoryCollection()
        return self.cols[name]


class _FakeChannelAdapter(SellingChannel):
    """用于验证渠道低耦合插件化、扫码全流程与状态同步的可控内存适配器。"""

    def __init__(
        self,
        profiles_root: Path,
        *,
        channel_code: str = "xianyu",
        channel_name: str = "闲鱼",
        auto_publish: bool = False,
    ) -> None:
        super().__init__(profiles_root)
        self.channel_code = channel_code
        self.channel_name = channel_name
        self.capabilities = ChannelCapabilities(
            qr_login=True,
            status_check=True,
            sync_selling=True,
            auto_publish=auto_publish,
            auto_downshelf=False,
        )
        self.qr_poll_sequence = ["waiting_scan", "waiting_scan", "scanned", "success"]
        self._poll_idx: Dict[str, int] = {}
        self.online_state = False
        self.account_id = "unb_888999"
        self.nickname = "小王好价帮拍"
        self.online_items = []
        self.abnormal_items = []
        self.shutdown_called = False
        self.cancelled_sessions = []

    def start_qr_login(self, username: str) -> QrLoginSnapshot:
        self.profile_dir_for(username)
        sid = f"qr_test_{self.channel_code}"
        self._poll_idx[sid] = 0
        return QrLoginSnapshot(
            session_id=sid,
            channel=self.channel_code,
            state="waiting_scan",
            message="请扫码",
            has_qr=True,
            expires_in_sec=120,
        )

    def get_qr_login_status(self, username: str, session_id: str) -> Optional[QrLoginSnapshot]:
        if session_id not in self._poll_idx:
            return None
        idx = self._poll_idx[session_id]
        state = self.qr_poll_sequence[min(idx, len(self.qr_poll_sequence) - 1)]
        self._poll_idx[session_id] = idx + 1
        if state == "success":
            self.online_state = True
            return QrLoginSnapshot(
                session_id=session_id,
                channel=self.channel_code,
                state="success",
                message=f"已绑定：{self.nickname}",
                has_qr=False,
                account_id=self.account_id,
                nickname=self.nickname,
            )
        return QrLoginSnapshot(
            session_id=session_id,
            channel=self.channel_code,
            state=state,
            message="已扫码" if state == "scanned" else "等待扫码",
            has_qr=True,
            expires_in_sec=100,
        )

    def get_qr_image_bytes(self, username: str, session_id: str) -> Optional[bytes]:
        if session_id not in self._poll_idx:
            return None
        img = Image.new("RGB", (64, 64), (10, 10, 10))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def cancel_qr_login(self, username: str, session_id: Optional[str] = None) -> bool:
        self.cancelled_sessions.append((username, session_id))
        return True

    def check_status(self, username: str) -> ChannelAccountStatus:
        self.profile_dir_for(username)
        return ChannelAccountStatus(
            channel=self.channel_code,
            online=self.online_state,
            account_id=self.account_id if self.online_state else "",
            nickname=self.nickname if self.online_state else "",
            error_code="" if self.online_state else "ACCOUNT_OFFLINE",
            message="账号在线" if self.online_state else "账号掉线",
            checked_at="2026-11-27T10:00:00Z",
        )

    def sync_selling_items(self, username: str) -> ChannelSyncResult:
        self.profile_dir_for(username)
        if not self.online_state:
            return ChannelSyncResult(
                ok=False,
                channel=self.channel_code,
                online=False,
                error_code="ACCOUNT_OFFLINE",
                message="账号未登录，请重新扫码",
            )
        return ChannelSyncResult(
            ok=True,
            channel=self.channel_code,
            online=True,
            account_id=self.account_id,
            nickname=self.nickname,
            online_items=list(self.online_items),
            abnormal_items=list(self.abnormal_items),
            group_counts={"在售": len(self.online_items)},
            synced_at="2026-11-27T10:01:00Z",
        )

    def publish_item(self, username: str, item_payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.capabilities.auto_publish:
            raise NotImplementedError("未开启自动上架")
        return {"published": True, "external_item_id": "tb_999", "listing_id": item_payload.get("listing_id")}

    def shutdown(self) -> None:
        self.shutdown_called = True


def _make_jpeg_base64(width=120, height=80, color=(220, 80, 30)) -> str:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _build_env(tmp_path: Path, ai_generator=None, adapters=None):
    db = _MemoryDB()
    cfg = WorkbenchConfig(
        auth_secret="test_secret_123456",
        data_dir=tmp_path / "data",
        screenshot_dir=tmp_path / "data" / "eval_screenshots",
        upload_dir=tmp_path / "data" / "uploads",
        channel_profiles_dir=tmp_path / "data" / "channel_profiles",
        deal_image_dir=tmp_path / "deal_images",
        default_official_image_count=3,
        max_images_per_listing=9,
    )
    cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
    cfg.upload_dir.mkdir(parents=True, exist_ok=True)
    cfg.channel_profiles_dir.mkdir(parents=True, exist_ok=True)
    cfg.deal_image_dir.mkdir(parents=True, exist_ok=True)

    repo = WorkbenchRepository(db)
    repo.ensure_indexes()
    svc = WorkbenchService(repo, cfg, ai_generator=ai_generator)

    registry = ChannelRegistry()
    if adapters is None:
        adapters = [_FakeChannelAdapter(cfg.channel_profiles_dir)]
    for ad in adapters:
        registry.register(ad)
    ch_svc = ChannelService(repo, registry)

    app = create_app(cfg, db=db, service=svc, channel_registry=registry, channel_service=ch_svc)
    return db, cfg, repo, svc, registry, ch_svc, app


def test_auth_cli_and_legacy_document_compatibility(tmp_path):
    db, _cfg, repo, _svc, _reg, _ch_svc, app = _build_env(tmp_path)
    db.cols["users"].docs.append({"uid": "u_d41eadbe", "phone": "15310625037"})

    created = cmd_create_user(repo, username="15310625037", password="secret123", bind_phone="15310625037")
    assert created["bound_shop_uid"] == "u_d41eadbe"
    assert len(cmd_list_users(repo)) == 1
    assert cmd_reset_password(repo, username="15310625037", password="123456") is True

    # 插入一条 v1 遗留字段文档 (sell_price + rejected + online)，验证平滑兼容读取
    db.cols["xy_listings"].docs.extend([
        {
            "listing_id": "xyl_legacy_1",
            "owner": "15310625037",
            "platform": "jd",
            "product_id": "jd_old_1",
            "status": "online",
            "title": "老版本历史商品",
            "description": "老描述",
            "images": [{"url": "https://img.jd.com/old.jpg", "origin": "official"}],
            "sell_price": 19.9,
            "cost_price": 19.9,
            "max_cost_price": 25.0,
        },
        {
            "listing_id": "xyl_legacy_rej",
            "owner": "15310625037",
            "platform": "pdd",
            "product_id": "pdd_old_2",
            "status": "rejected",
            "title": "历史跳过商品",
            "images": [],
            "sell_price": 9.9,
        },
    ])

    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert client.post("/api/items/draft", json={"platform": "pdd", "product_id": "1"}).status_code == 401

        login = client.post("/api/auth/login", json={"username": "15310625037", "password": "123456"})
        assert login.status_code == 200
        assert "xy_token" in client.cookies

        me = client.get("/api/auth/me").json()["data"]
        assert me["bound_shop_uid"] == "u_d41eadbe"
        assert me["item_counts"] == {"draft": 0, "ready": 1, "dismissed": 1, "active": 1}

        items = client.get("/api/items?status=active").json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["price"] == 19.9
        assert items[0]["status"] == "ready"
        # 彻底剥离无用字段
        for removed_key in ("cost_price", "max_cost_price", "shipping_fee"):
            assert removed_key not in items[0]

        rebind = client.patch("/api/auth/binding", json={"shop_uid": "u_other01"})
        assert rebind.status_code == 200
        assert rebind.json()["data"]["bound_shop_uid"] == "u_other01"

        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").status_code == 401


def test_candidates_screenshot_and_dismiss(tmp_path):
    db, cfg, repo, _svc, _reg, _ch_svc, app = _build_env(tmp_path)
    repo.create_user("bob", hash_password("pass12345"), bound_shop_uid="u_bob_shop")
    (cfg.screenshot_dir / "770011.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data")

    db.cols["products"].docs.extend([
        {
            "platform": "jd",
            "product_id": "jd_noodle_1",
            "name": "康师傅红烧牛肉面整箱24桶",
            "image_url": "https://img.jd.com/main.jpg",
            "price": 59.9,
            "activity_price": 45.9,
            "is_off_promotion": False,
            "deal_info": {"is_deal_tip": True, "deal_price": 39.9, "card_highlight": "合1.66元/桶"},
        },
        {
            "platform": "taobao",
            "product_id": "tb_off_3",
            "name": "已失效商品",
            "image_url": "https://img.tb.com/off.jpg",
            "price": 19.9,
            "is_off_promotion": True,
        },
    ])

    db.cols["xianyu_deal_evaluations"].docs.extend([
        {
            "platform": "jd",
            "product_id": "jd_noodle_1",
            "source_type": "deal_tip",
            "name": "康师傅红烧牛肉面整箱24桶",
            "activity_price": 38.0,
            "evaluated_at": "2026-11-27 10:00:00",
            "final_winner": "my_product",
            "is_my_product_better": True,
            "cost_performance_ratio": 1.45,
            "comparison_result": {"llm_reason": "我方39.9元24桶优于闲鱼55元"},
            "best_xianyu_product": {
                "item_id": "770011",
                "title": "康师傅牛肉面24桶包邮",
                "comparable_price": 55.0,
                "want_cnt": 18,
                "screenshot_file": "770011.jpg",
            },
        },
        {
            "platform": "taobao",
            "product_id": "tb_off_3",
            "is_my_product_better": True,
            "cost_performance_ratio": 2.0,
            "best_xianyu_product": {"item_id": "990033", "comparable_price": 39.8},
        },
    ])

    db.cols["favorites"].docs.append({
        "uid": "u_bob_shop",
        "platform": "jd",
        "product_id": "jd_noodle_1",
        "collected_price": 42.0,
        "snapshot": {"name": "康师傅红烧牛肉面整箱24桶", "image_url": "https://img.jd.com/main.jpg"},
        "created_at": datetime.datetime(2026, 11, 27, 1, 0, 0, tzinfo=datetime.timezone.utc),
    })

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "bob", "password": "pass12345"})

        ev_res = client.get("/api/candidates/evaluations").json()["data"]
        assert ev_res["total"] == 1
        assert ev_res["items"][0]["price"] == 39.9
        assert ev_res["items"][0]["xianyu_ref"]["screenshot_url"] == "/api/assets/screenshots/770011.jpg"
        assert client.get("/api/assets/screenshots/770011.jpg").status_code == 200
        assert client.get("/api/assets/screenshots/..%2Fsecret.jpg").status_code == 404

        fav_res = client.get("/api/candidates/favorites").json()["data"]
        assert fav_res["total"] == 1
        assert fav_res["items"][0]["price"] == 39.9

        dis = client.post(
            "/api/candidates/dismiss",
            json={"platform": "jd", "product_id": "jd_noodle_1", "source_origin": "xianyu_eval"},
        )
        assert dis.status_code == 200
        assert dis.json()["data"]["status"] == "dismissed"
        assert client.get("/api/candidates/evaluations").json()["data"]["total"] == 0


def test_core_draft_images_proxy_buy_copy_and_upload(tmp_path):
    ai_prompts = []

    def _fake_ai(prompt: str):
        ai_prompts.append(prompt)
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "title": "康师傅牛肉面24桶 官旗代拍直发",
                    "description": "官方旗舰店活动好价，支持帮拍代下单直发，也可以私聊了解同款渠道好价，拍前请先问库存与实时价格。",
                },
                ensure_ascii=False,
            ),
            "metrics": {"model_used": "mock-gemini"},
        }

    db, cfg, repo, _svc, _reg, _ch_svc, app = _build_env(tmp_path, ai_generator=_fake_ai)
    repo.create_user("curator", hash_password("pass12345"))

    deal_img_id = "dsi_0123456789abcdef0123"
    (cfg.deal_image_dir / f"{deal_img_id}.jpg").write_bytes(b"\xff\xd8\xff\xe0deal")

    db.cols["products"].docs.append({
        "platform": "jd",
        "product_id": "jd_box_9",
        "name": "康师傅红烧牛肉面整箱24桶装",
        "image_url": "https://img.jd.com/1.jpg",
        "carousel_images": [
            "https://img.jd.com/1.jpg",
            "https://img.jd.com/2.jpg",
            "https://img.jd.com/3.jpg",
            "https://img.jd.com/4.jpg",
        ],
        "price": 59.9,
        "activity_price": 45.9,
        "deal_info": {
            "is_deal_tip": True,
            "deal_price": 39.9,
            "user_share_images": [f"/api/deals/image/{deal_img_id}"],
        },
    })

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "curator", "password": "pass12345"})

        draft = client.post(
            "/api/items/draft",
            json={"platform": "jd", "product_id": "jd_box_9", "source_origin": "xianyu_eval"},
        ).json()["data"]
        lid = draft["listing_id"]
        assert draft["status"] == "draft"
        assert draft["price"] == 39.9
        assert "cost_price" not in draft
        assert "max_cost_price" not in draft
        assert "shipping_fee" not in draft
        assert [x["url"] for x in draft["images"]] == [
            "https://img.jd.com/1.jpg",
            "https://img.jd.com/2.jpg",
            "https://img.jd.com/3.jpg",
            f"/api/deals/image/{deal_img_id}",
        ]

        # 文案为空不能直接标为 ready
        assert client.patch(f"/api/items/{lid}", json={"status": "ready"}).status_code == 400

        # 上传本地图片 + 调用帮拍话术生成
        assert client.post("/api/assets/upload", json={"image_base64": "not-an-image"}).status_code == 400
        up_url = client.post("/api/assets/upload", json={"image_base64": _make_jpeg_base64()}).json()["data"]["url"]
        assert client.get(up_url).status_code == 200

        gen = client.post(f"/api/items/{lid}/generate-copy").json()["data"]
        assert len(ai_prompts) == 1
        assert "帮拍" in ai_prompts[0] and "代下单" in ai_prompts[0]
        assert "个人闲置" in ai_prompts[0]  # 仅在“不要编造”约束中否定提及，不应出现成本毛利字样
        assert "cost_price" not in ai_prompts[0]
        assert "shipping_fee" not in ai_prompts[0]

        ready = client.patch(
            f"/api/items/{lid}",
            json={
                "title": gen["title"],
                "description": gen["description"],
                "images": [draft["images"][0], {"url": up_url, "origin": "upload"}],
                "price": 41.5,
                "status": "ready",
            },
        ).json()["data"]
        assert ready["status"] == "ready"
        assert ready["price"] == 41.5
        assert ready["confirmed_at"] != ""


def test_pluggable_channels_qr_login_sync_reconcile_and_shutdown(tmp_path):
    cfg_dir = tmp_path / "data" / "channel_profiles"
    xianyu_adapter = _FakeChannelAdapter(cfg_dir, channel_code="xianyu", channel_name="闲鱼", auto_publish=False)
    taobao_adapter = _FakeChannelAdapter(cfg_dir, channel_code="taobao", channel_name="淘宝", auto_publish=True)

    db, cfg, repo, _svc, registry, _ch_svc, app = _build_env(
        tmp_path,
        adapters=[xianyu_adapter, taobao_adapter],
    )
    repo.create_user("15310625037", hash_password("123456"))

    # 托管目录严格落在 channel_profiles_dir/<channel>/<username>，绝不信任非法路径
    p_dir = xianyu_adapter.profile_dir_for("15310625037")
    assert p_dir == (cfg.channel_profiles_dir / "xianyu" / "15310625037").resolve()
    try:
        xianyu_adapter.profile_dir_for("../escape")
        raise AssertionError("非法用户名应被拦截")
    except ValueError:
        pass

    # 本地商品素材与上游源品
    db.cols["products"].docs.extend([
        {
            "platform": "jd",
            "product_id": "jd_on_1",
            "name": "维达抽纸整箱24包超韧系列",
            "price": 39.9,
            "activity_price": 26.9,
            "is_off_promotion": False,
        },
        {
            "platform": "pdd",
            "product_id": "pdd_off_2",
            "name": "蒙牛纯牛奶250ml*16盒整箱",
            "price": 49.0,
            "activity_price": 32.0,
            "is_off_promotion": True,
        },
    ])
    db.cols["xy_listings"].docs.extend([
        {
            "listing_id": "xyl_auto_match",
            "owner": "15310625037",
            "platform": "jd",
            "product_id": "jd_on_1",
            "status": "ready",
            "title": "维达抽纸整箱24包超韧系列 官旗代拍",
            "description": "官旗直发",
            "images": [{"url": "https://img.jd.com/a.jpg", "origin": "official"}],
            "price": 26.9,
        },
        {
            "listing_id": "xyl_manual_match",
            "owner": "15310625037",
            "platform": "pdd",
            "product_id": "pdd_off_2",
            "status": "ready",
            "title": "纯牛奶整箱好价帮拍",
            "description": "支持帮拍",
            "images": [{"url": "https://img.pdd.com/b.jpg", "origin": "official"}],
            "price": 32.0,
        },
    ])

    xianyu_adapter.online_items = [
        {
            "item_id": "xy_item_1",
            "title": "维达抽纸整箱24包超韧系列 官旗代拍",
            "price": "26.9",
            "want_info": "5人想要",
            "pic_url": "https://img.goofish.com/1.jpg",
            "item_url": "https://www.goofish.com/item?id=xy_item_1",
            "is_online": True,
        },
        {
            "item_id": "xy_item_2",
            "title": "营养早餐奶箱装",
            "price": "32.0",
            "want_info": "2人想要",
            "pic_url": "https://img.goofish.com/2.jpg",
            "item_url": "https://www.goofish.com/item?id=xy_item_2",
            "is_online": True,
        },
    ]
    xianyu_adapter.abnormal_items = [
        {
            "item_id": "xy_item_abn",
            "title": "已下架残留款",
            "price": "19.9",
            "status_label": "已被下架/删除(status=-9)",
            "is_online": False,
        }
    ]

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "15310625037", "password": "123456"})

        # 1. 多渠道注册表同时支持闲鱼与淘宝，核心未改动
        chs = client.get("/api/channels").json()["data"]["channels"]
        assert [c["channel"] for c in chs] == ["xianyu", "taobao"]
        assert chs[0]["bound"] is False

        # 未登录同步返回 409
        assert client.post("/api/channels/xianyu/sync").status_code == 409

        # 2. 扫码登录全生命周期：start -> qr image -> waiting -> scanned -> success
        start = client.post("/api/channels/xianyu/login/start").json()["data"]
        sid = start["session_id"]
        qr_img_resp = client.get(f"/api/channels/xianyu/login/qr/{sid}")
        assert qr_img_resp.status_code == 200
        assert qr_img_resp.headers["content-type"].startswith("image/png")
        assert "no-store" in qr_img_resp.headers.get("cache-control", "")

        assert client.get(f"/api/channels/xianyu/login/status/{sid}").json()["data"]["state"] == "waiting_scan"
        assert client.get(f"/api/channels/xianyu/login/status/{sid}").json()["data"]["state"] == "scanned"
        done = client.get(f"/api/channels/xianyu/login/status/{sid}").json()["data"]
        assert done["state"] == "success"
        assert done["nickname"] == "小王好价帮拍"

        # 成功后账号状态持久化在 xy_channel_accounts
        chs_after = client.get("/api/channels").json()["data"]["channels"]
        assert chs_after[0]["bound"] is True
        assert chs_after[0]["online"] is True
        assert chs_after[0]["nickname"] == "小王好价帮拍"

        # 3. 同步闲鱼在售并自动标题对账
        synced = client.post("/api/channels/xianyu/sync").json()["data"]
        assert synced["online_count"] == 2
        assert synced["abnormal_count"] == 1
        assert synced["online_items"][0]["link_mode"] == "auto_title"
        assert synced["online_items"][0]["linked_listing_id"] == "xyl_auto_match"
        assert synced["online_items"][0]["upstream_status"] == "online"
        assert synced["online_items"][0]["upstream_live_price"] == 26.9
        assert synced["online_items"][1]["link_mode"] == "unlinked"

        # 4. 手动关联 xy_item_2 -> xyl_manual_match，立刻识别上游已下架预警
        linked = client.post(
            "/api/channels/xianyu/selling/xy_item_2/link",
            json={"listing_id": "xyl_manual_match"},
        ).json()["data"]
        item2 = next(x for x in linked["online_items"] if x["item_id"] == "xy_item_2")
        assert item2["link_mode"] == "manual"
        assert item2["upstream_status"] == "off_promotion"
        assert item2["off_promotion_alert"] is True
        assert linked["off_promotion_alert_count"] == 1

        # 5. 能力门控：闲鱼不支持自动上架诚实返回 501；支持该能力的淘宝返回 200
        assert client.post("/api/channels/xianyu/publish", json={"listing_id": "xyl_auto_match"}).status_code == 501
        assert client.post("/api/channels/xianyu/downshelf", json={"external_item_id": "xy_item_1"}).status_code == 501
        tb_pub = client.post("/api/channels/taobao/publish", json={"listing_id": "xyl_auto_match"})
        assert tb_pub.status_code == 200
        assert tb_pub.json()["data"]["published"] is True

        # 取消接口支持无 body 和带 session_id
        assert client.post("/api/channels/xianyu/login/cancel").status_code == 200
        assert client.post("/api/channels/xianyu/login/cancel", json={"session_id": sid}).status_code == 200

    # TestClient 退出 lifespan 时确定性调用 registry.shutdown()
    assert xianyu_adapter.shutdown_called is True
    assert taobao_adapter.shutdown_called is True


def test_profile_lock_scope_isolates_seller_from_global_discovery_lock(tmp_path):
    profile_a = str(tmp_path / "profiles" / "xianyu" / "user_a")
    profile_b = str(tmp_path / "profiles" / "xianyu" / "user_b")
    test_global_lock = str(tmp_path / "global_test.lock")

    # 在全局锁被 discovery 持有的情况下，profile 锁必须零等待直接拿到（故障域隔离）
    entered = threading.Event()
    done = threading.Event()

    with patch.object(xy_session, "_GLOBAL_LOCK_FILE_PATH", test_global_lock):
        def _hold_global():
            with xy_session._acquire_serial_browser_lock(
                "W:\\temp\\xianyu_xiaohao", task_name="discovery", lock_timeout_sec=5.0
            ):
                entered.set()
                done.wait(timeout=3.0)

        t = threading.Thread(target=_hold_global, daemon=True)
        t.start()
        try:
            assert entered.wait(timeout=2.0)

            t0 = time.monotonic()
            with xy_session._acquire_serial_browser_lock(
                profile_a, task_name="qr_login", lock_timeout_sec=1.5, lock_scope="profile"
            ):
                with xy_session._acquire_serial_browser_lock(
                    profile_b, task_name="qr_login", lock_timeout_sec=1.5, lock_scope="profile"
                ):
                    elapsed = time.monotonic() - t0
            assert elapsed < 1.0
        finally:
            done.set()
            t.join(timeout=3.0)


def test_xianyu_qr_worker_keep_login_atomic_replace_and_sanitized_errors(tmp_path):
    from xianyu_workbench.channels.xianyu import (
        XianyuChannelAdapter,
        _XianyuQrLoginWorker,
        _replace_profile_dir,
    )

    class _FakeBtn:
        def __init__(self, visible: bool):
            self._visible = visible
            self.clicked = False

        def count(self) -> int:
            return 1

        def is_visible(self) -> bool:
            return self._visible

        def click(self) -> None:
            self.clicked = True

    class _FakeLocator:
        def __init__(self, btns):
            self._btns = btns

        @property
        def first(self):
            return self._btns[0]

        def count(self) -> int:
            return len(self._btns)

    qr_btn = _FakeBtn(visible=True)

    class _FakeLoginFrame:
        def locator(self, selector: str):
            assert selector == ".qrcode-login button.keep-login-confirm-btn.primary"
            return _FakeLocator([qr_btn])

    worker = _XianyuQrLoginWorker(session_id="qr_t1", owner="15310625037", profile_dir=tmp_path / "p")
    assert worker._click_keep_login_if_visible(_FakeLoginFrame()) is True
    assert qr_btn.clicked is True

    # 验证全新扫码临时目录原子替换已有旧目录（清除历史“不保持”污染且无残留目录）
    target = tmp_path / "profiles" / "xianyu" / "15310625037"
    target.mkdir(parents=True)
    (target / "stale_not_keep.txt").write_text("old", encoding="utf-8")

    staging = target.with_name("15310625037.staging_qr123")
    staging.mkdir(parents=True)
    (staging / "fresh_keep.txt").write_text("new", encoding="utf-8")

    _replace_profile_dir(staging, target)
    assert not staging.exists()
    assert not (target / "stale_not_keep.txt").exists()
    assert (target / "fresh_keep.txt").read_text(encoding="utf-8") == "new"
    assert [p.name for p in target.parent.iterdir()] == ["15310625037"]

    # 验证渠道错误文案不透出 open_browser_for_manual_use 或本机目录路径
    adapter = XianyuChannelAdapter(tmp_path / "profiles")
    raw_offline = {
        "ok": False,
        "error_code": "ACCOUNT_OFFLINE",
        "message": "请暂停自动重试并调用 open_browser_for_manual_use(r'W:\\secret\\path') 人工处理",
    }
    with patch("xianyu_workbench.channels.xianyu.check_login_status", return_value=raw_offline), patch(
        "xianyu_workbench.channels.xianyu.get_selling_items", return_value=raw_offline
    ):
        st = adapter.check_status("15310625037")
        assert st.online is False
        assert "open_browser_for_manual_use" not in st.message
        assert "secret" not in st.message
        assert "扫码绑定" in st.message

        sy = adapter.sync_selling_items("15310625037")
        assert sy.ok is False
        assert "open_browser_for_manual_use" not in sy.message
        assert "扫码绑定" in sy.message

