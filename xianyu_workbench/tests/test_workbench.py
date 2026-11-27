# -*- coding: utf-8 -*-
"""闲鱼选品工作台端到端离线单元测试（内存仓储 + FastAPI TestClient）。"""
from __future__ import annotations

import base64
from copy import deepcopy
import datetime
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from pymongo.errors import DuplicateKeyError

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from xianyu_workbench.api import create_app
from xianyu_workbench.auth import hash_password
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
            if projection.get("_id", 1):
                if "_id" in out:
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


def _make_jpeg_base64(width=120, height=80, color=(220, 80, 30)) -> str:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _build_env(tmp_path: Path, ai_generator=None):
    db = _MemoryDB()
    cfg = WorkbenchConfig(
        auth_secret="test_secret_123456",
        data_dir=tmp_path / "data",
        screenshot_dir=tmp_path / "data" / "eval_screenshots",
        upload_dir=tmp_path / "data" / "uploads",
        deal_image_dir=tmp_path / "deal_images",
        default_official_image_count=3,
        max_images_per_listing=9,
    )
    cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
    cfg.upload_dir.mkdir(parents=True, exist_ok=True)
    cfg.deal_image_dir.mkdir(parents=True, exist_ok=True)

    repo = WorkbenchRepository(db)
    repo.ensure_indexes()
    svc = WorkbenchService(repo, cfg, ai_generator=ai_generator)
    app = create_app(cfg, db=db, service=svc)
    return db, cfg, repo, svc, app


def test_workbench_auth_binding_and_cli_user_management(tmp_path):
    db, _cfg, repo, _svc, app = _build_env(tmp_path)
    db.cols["users"].docs.append({"uid": "u_shop9999", "phone": "13800138000"})

    created = cmd_create_user(repo, username="alice", password="secret123", bind_phone="13800138000")
    assert created["bound_shop_uid"] == "u_shop9999"
    assert len(cmd_list_users(repo)) == 1
    assert cmd_reset_password(repo, username="alice", password="newpass456") is True

    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/candidates/evaluations").status_code == 401
        assert client.post("/api/listings/draft", json={"platform": "pdd", "product_id": "1"}).status_code == 401

        bad = client.post("/api/auth/login", json={"username": "alice", "password": "secret123"})
        assert bad.status_code == 401

        ok_login = client.post("/api/auth/login", json={"username": "alice", "password": "newpass456"})
        assert ok_login.status_code == 200
        body = ok_login.json()["data"]
        assert body["user"]["bound_shop_uid"] == "u_shop9999"
        assert "xy_token" in client.cookies

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["data"]["username"] == "alice"

        rebind = client.patch("/api/auth/binding", json={"shop_uid": "u_custom01"})
        assert rebind.status_code == 200
        assert rebind.json()["data"]["bound_shop_uid"] == "u_custom01"

        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").status_code == 401


def test_candidates_screenshot_fallback_and_rejection_filtering(tmp_path):
    db, cfg, repo, _svc, app = _build_env(tmp_path)
    repo.create_user("bob", hash_password("pass12345"), bound_shop_uid="u_bob_shop")

    # 写一张真实的闲鱼比价截图文件
    (cfg.screenshot_dir / "770011.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data")

    db.cols["products"].docs.extend([
        {
            "platform": "jd",
            "product_id": "jd_noodle_1",
            "goods_sign": "enc_noodle",
            "name": "康师傅红烧牛肉面整箱24桶",
            "image_url": "https://img.jd.com/main.jpg",
            "carousel_images": [
                "https://img.jd.com/main.jpg",
                "https://img.jd.com/2.jpg",
                "https://img.jd.com/3.jpg",
                "https://img.jd.com/4.jpg",
            ],
            "price": 59.9,
            "activity_price": 45.9,
            "is_off_promotion": False,
            "deal_info": {
                "is_deal_tip": True,
                "deal_price": 39.9,
                "card_highlight": "合1.66元/桶",
                "user_share_images": ["https://deal.img/share1.jpg"],
            },
        },
        {
            "platform": "pdd",
            "product_id": "pdd_legacy_2",
            "name": "维达抽纸24包",
            "image_url": "https://img.pdd.com/v1.jpg",
            "price": 29.9,
            "activity_price": 25.9,
            "is_off_promotion": False,
        },
        {
            "platform": "taobao",
            "product_id": "tb_off_3",
            "name": "已下架商品",
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
            "category_name": "方便速食",
            "name": "康师傅红烧牛肉面整箱24桶",
            "activity_price": 38.0,  # 实时爆料价涨到 39.9，应提示涨 1.9
            "evaluated_at": "2026-11-27 10:00:00",
            "final_winner": "my_product",
            "is_my_product_better": True,
            "cost_performance_ratio": 1.45,
            "comparison_result": {"matched_unit": "桶", "llm_reason": "我方39.9元24桶优于闲鱼55元"},
            "best_xianyu_product": {
                "item_id": "770011",
                "title": "康师傅牛肉面24桶包邮",
                "comparable_price": 55.0,
                "want_cnt": 18,
                "screenshot_file": "770011.jpg",
            },
        },
        {
            "platform": "pdd",
            "product_id": "pdd_legacy_2",
            "source_type": "official",
            "category_name": "纸品",
            "name": "维达抽纸24包",
            "activity_price": 25.9,
            "evaluated_at": "2026-11-26 09:00:00",
            "final_winner": "my_product",
            "is_my_product_better": True,
            "cost_performance_ratio": 1.22,
            "comparison_result": {"matched_unit": "包", "llm_reason": "我方更划算"},
            "best_xianyu_product": {"item_id": "880022", "title": "维达24包", "comparable_price": 31.5},
            "xianyu_audit_trail": {
                "xianyu_coarse_items": [{"item_id": "880022", "image_url": "https://img.goofish.com/legacy.jpg"}]
            },
        },
        {
            "platform": "taobao",
            "product_id": "tb_off_3",
            "source_type": "official",
            "category_name": "日用",
            "name": "已下架商品",
            "activity_price": 19.9,
            "evaluated_at": "2026-11-26 08:00:00",
            "final_winner": "my_product",
            "is_my_product_better": True,
            "cost_performance_ratio": 2.0,
            "best_xianyu_product": {"item_id": "990033", "title": "闲鱼款", "comparable_price": 39.8},
        },
    ])

    db.cols["favorites"].docs.append({
        "uid": "u_bob_shop",
        "platform": "pdd",
        "product_id": "pdd_legacy_2",
        "collected_price": 28.0,
        "snapshot": {"name": "维达抽纸24包", "image_url": "https://img.pdd.com/v1.jpg"},
        "created_at": datetime.datetime(2026, 11, 27, 1, 0, 0, tzinfo=datetime.timezone.utc),
    })

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "bob", "password": "pass12345"})

        ev_res = client.get("/api/candidates/evaluations").json()["data"]
        assert ev_res["total"] == 2  # 已下架 tb_off_3 默认过滤
        first, second = ev_res["items"][0], ev_res["items"][1]
        assert first["product_id"] == "jd_noodle_1"
        assert first["price_diff_from_eval"] == 1.9
        assert first["xianyu_ref"]["screenshot_url"] == "/api/assets/screenshots/770011.jpg"
        assert second["product_id"] == "pdd_legacy_2"
        assert second["xianyu_ref"]["screenshot_url"] == ""
        assert second["xianyu_ref"]["fallback_images"] == ["https://img.goofish.com/legacy.jpg"]

        # 通过 Cookie 鉴权直接读取受保护截图 + 防路径穿越
        assert client.get("/api/assets/screenshots/770011.jpg").status_code == 200
        assert client.get("/api/assets/screenshots/..%2Fsecret.jpg").status_code == 404

        # 收藏候选读取
        fav_res = client.get("/api/candidates/favorites").json()["data"]
        assert fav_res["total"] == 1
        assert fav_res["items"][0]["price_diff"] == -2.1

        # 淘汰 jd_noodle_1 后不再出现在待选池
        rej = client.post(
            "/api/candidates/reject",
            json={"platform": "jd", "product_id": "jd_noodle_1", "source_origin": "xianyu_eval"},
        )
        assert rej.status_code == 200
        assert rej.json()["data"]["status"] == "rejected"

        ev_after = client.get("/api/candidates/evaluations").json()["data"]
        assert [x["product_id"] for x in ev_after["items"]] == ["pdd_legacy_2"]


def test_draft_images_ai_copy_upload_ready_validation_and_risk_alert(tmp_path):
    ai_calls = []

    def _fake_ai(prompt: str):
        ai_calls.append(prompt)
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "title": "全新未拆 康师傅牛肉面24桶 包邮",
                    "description": "多囤了一箱实在吃不完，全新未拆封，日期新鲜，拍下安排直发包邮！",
                },
                ensure_ascii=False,
            ),
            "metrics": {"model_used": "mock-gemini"},
        }

    db, cfg, repo, _svc, app = _build_env(tmp_path, ai_generator=_fake_ai)
    repo.create_user("curator", hash_password("pass12345"))

    # 放入本地爆料图 dsi_0123456789abcdef0123.jpg
    deal_img_id = "dsi_0123456789abcdef0123"
    (cfg.deal_image_dir / f"{deal_img_id}.jpg").write_bytes(b"\xff\xd8\xff\xe0deal")

    db.cols["products"].docs.append({
        "platform": "jd",
        "product_id": "jd_box_9",
        "name": "康师傅红烧牛肉面整箱24桶装经典口味家庭囤货速食方便面",
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

        # 1. 创建草稿：默认前 3 张官方图 + 全部爆料图；默认价格优先取爆料价 39.9
        draft_resp = client.post(
            "/api/listings/draft",
            json={"platform": "jd", "product_id": "jd_box_9", "source_origin": "xianyu_eval"},
        )
        assert draft_resp.status_code == 200
        draft = draft_resp.json()["data"]
        lid = draft["listing_id"]
        assert draft["status"] == "draft"
        assert draft["sell_price"] == 39.9
        assert draft["cost_price"] == 39.9
        assert [x["url"] for x in draft["images"]] == [
            "https://img.jd.com/1.jpg",
            "https://img.jd.com/2.jpg",
            "https://img.jd.com/3.jpg",
            f"/api/deals/image/{deal_img_id}",
        ]
        assert draft["available_official_images"] == [
            "https://img.jd.com/1.jpg",
            "https://img.jd.com/2.jpg",
            "https://img.jd.com/3.jpg",
            "https://img.jd.com/4.jpg",
        ]
        assert client.get(f"/api/deals/image/{deal_img_id}").status_code == 200

        # 2. 描述为空时直接确认 ready 必须拦截
        bad_ready = client.patch(f"/api/listings/{lid}", json={"status": "ready"})
        assert bad_ready.status_code == 400

        # 3. 上传本地图片并删除其中一张官方图
        assert client.post("/api/assets/upload", json={"image_base64": "not-an-image"}).status_code == 400
        up_res = client.post("/api/assets/upload", json={"image_base64": _make_jpeg_base64()})
        assert up_res.status_code == 200
        uploaded_url = up_res.json()["data"]["url"]
        assert client.get(uploaded_url).status_code == 200

        # 4. AI 生成闲鱼话术（不直接篡改数据库）
        gen_res = client.post(f"/api/listings/{lid}/generate-copy", json={"style": "casual"})
        assert gen_res.status_code == 200
        gen_data = gen_res.json()["data"]
        assert len(ai_calls) == 1
        assert gen_data["title"] == "全新未拆 康师傅牛肉面24桶 包邮"

        # 5. 保存定稿进入标准库 ready
        edited_images = [
            draft["images"][0],
            draft["images"][3],
            {"url": uploaded_url, "origin": "upload"},
        ]
        ready_res = client.patch(
            f"/api/listings/{lid}",
            json={
                "title": gen_data["title"],
                "description": gen_data["description"],
                "images": edited_images,
                "sell_price": 46.0,
                "status": "ready",
            },
        )
        assert ready_res.status_code == 200
        ready_doc = ready_res.json()["data"]
        assert ready_doc["status"] == "ready"
        assert ready_doc["sell_price"] == 46.0
        assert len(ready_doc["images"]) == 3
        assert ready_doc["confirmed_at"] != ""

        # 6. 上游涨破止损价与下架均触发标准库实时风险预警
        db.cols["products"].docs[0]["deal_info"]["deal_price"] = 49.0
        list_res = client.get("/api/listings?status=ready").json()["data"]
        assert list_res["items"][0]["risk_alert"] == "price_exceeded"

        db.cols["products"].docs[0]["is_off_promotion"] = True
        list_res2 = client.get("/api/listings?status=ready").json()["data"]
        assert list_res2["items"][0]["risk_alert"] == "off_promotion"
