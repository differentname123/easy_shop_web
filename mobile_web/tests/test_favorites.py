# -*- coding: utf-8 -*-
"""单清单商品收藏夹单元测试：鉴权、多用户隔离、幂等去重、上限拦截、批量实时联查。"""
from copy import deepcopy
from dataclasses import replace
import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import config
import main as main_module
from auth import create_access_token
from main import create_app
from repositories.favorite_repo import FavoriteRepository, MAX_FAVORITES_PER_USER
from repositories.product_repo import ProductRepository
from services.favorite_service import FavoriteService
from tests.test_product_repository import MemoryCollection as ProductMemoryCollection


class _FavCursor:
    def __init__(self, docs):
        self.docs = list(docs)
        self._skip = 0
        self._limit = None

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.docs.sort(key=lambda d: d.get(key) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=direction < 0)
        return self

    def skip(self, count):
        self._skip = count
        return self

    def limit(self, count):
        self._limit = count
        return self

    def __iter__(self):
        end = None if self._limit is None else self._skip + self._limit
        return iter(self.docs[self._skip:end])


class _FavMemoryCollection:
    def __init__(self):
        self.docs = []
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if ProductMemoryCollection.matches(doc, query):
                return self._project(doc, projection)
        return None

    @staticmethod
    def _project(doc, projection):
        if projection is None:
            return deepcopy(doc)
        if projection == {"_id": 0}:
            out = deepcopy(doc)
            out.pop("_id", None)
            return out
        return ProductMemoryCollection.project(doc, projection)

    def count_documents(self, query):
        return sum(1 for doc in self.docs if ProductMemoryCollection.matches(doc, query))

    def insert_one(self, doc):
        for existing in self.docs:
            if (
                existing.get("uid") == doc.get("uid")
                and existing.get("platform") == doc.get("platform")
                and existing.get("product_id") == doc.get("product_id")
            ):
                raise DuplicateKeyError("duplicate favorite")
        stored = deepcopy(doc)
        stored.setdefault("_id", f"fav_{len(self.docs) + 1}")
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def delete_one(self, query):
        for idx, doc in enumerate(self.docs):
            if ProductMemoryCollection.matches(doc, query):
                self.docs.pop(idx)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def find(self, query=None, projection=None):
        q = query or {}
        matched = [self._project(doc, projection) for doc in self.docs if ProductMemoryCollection.matches(doc, q)]
        return _FavCursor(matched)


def _setup_services(monkeypatch):
    fav_col = _FavMemoryCollection()
    prod_col = ProductMemoryCollection()
    db = SimpleNamespace(
        get_collection=lambda name: fav_col if name == "favorites" else prod_col
    )
    prod_repo = ProductRepository(db_instance=db)
    fav_repo = FavoriteRepository(db_instance=db)
    svc = FavoriteService(favorite_repo=fav_repo, product_repo=prod_repo)
    import api.favorite as fav_api
    import api.home as home_api
    monkeypatch.setattr(fav_api, "favorite_service", svc)
    monkeypatch.setattr(home_api, "favorite_repo", fav_repo)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )
    return svc, fav_col, prod_col


def test_favorites_routes_require_auth_and_enforce_isolation(monkeypatch):
    _setup_services(monkeypatch)
    app = create_app()
    tok_a = create_access_token("u_aaaaaaaa", ttl=3600)
    tok_b = create_access_token("u_bbbbbbbb", ttl=3600)

    with TestClient(app) as client:
        assert client.get("/api/favorites/list").status_code == 401
        assert client.post("/api/favorites/add", json={"platform": "pdd", "product_id": "101"}).status_code == 401
        assert client.post("/api/favorites/remove", json={"platform": "pdd", "product_id": "101"}).status_code == 401

        r_add = client.post(
            "/api/favorites/add",
            headers={"Authorization": f"Bearer {tok_a}"},
            json={"platform": "pdd", "product_id": "101", "name": "用户A商品", "price": 19.9},
        )
        assert r_add.status_code == 200
        assert r_add.json()["data"]["is_favorited"] is True

        # 用户 B 看不到也删不掉用户 A 的收藏
        r_list_b = client.get("/api/favorites/list", headers={"Authorization": f"Bearer {tok_b}"})
        assert r_list_b.json()["data"]["total"] == 0

        r_rem_b = client.post(
            "/api/favorites/remove",
            headers={"Authorization": f"Bearer {tok_b}"},
            json={"platform": "pdd", "product_id": "101"},
        )
        assert r_rem_b.json()["data"]["removed"] is False

        r_list_a = client.get("/api/favorites/list", headers={"Authorization": f"Bearer {tok_a}"})
        assert r_list_a.json()["data"]["total"] == 1


def test_favorites_idempotent_add_remove_and_live_product_join(monkeypatch):
    _svc, fav_col, prod_col = _setup_services(monkeypatch)
    prod_col.docs.extend([
        {
            "platform": "jd",
            "product_id": "jd_img_1",
            "goods_id": "jd_img_1",
            "goods_sign": "jd_enc_1",
            "name": "维达抽纸24包",
            "image_url": "https://img.jd.com/1.jpg",
            "price": 39.9,
            "activity_price": 29.9,
            "deal_info": {"is_deal_tip": True, "deal_price": 19.9},
            "is_off_promotion": False,
        },
        {
            "platform": "taobao",
            "product_id": "tb_1",
            "goods_id": "tb_1",
            "goods_sign": "tb_sign_1",
            "name": "淘宝测试商品",
            "image_url": "https://img.alicdn.com/1.jpg",
            "price": 50.0,
            "activity_price": 40.0,
            "is_off_promotion": False,
        },
    ])

    app = create_app()
    tok = create_access_token("u_11223344", ttl=3600)
    headers = {"Authorization": f"Bearer {tok}"}

    with TestClient(app) as client:
        # 1. 收藏线报商品时优先记录 deal_price=19.9
        r1 = client.post("/api/favorites/add", headers=headers, json={"platform": "jd", "product_id": "jd_img_1"})
        assert r1.status_code == 200
        assert r1.json()["data"]["already_exists"] is False

        # 2. 商品降价后再次重复收藏，不覆盖首次 collected_price=19.9
        prod_col.docs[0]["deal_info"]["deal_price"] = 15.9
        r2 = client.post("/api/favorites/add", headers=headers, json={"platform": "jd", "product_id": "jd_img_1"})
        assert r2.status_code == 200
        assert r2.json()["data"]["already_exists"] is True

        # 3. 收藏淘宝商品后将其标记下架，并再收藏一件库中不存在的兜底商品
        client.post("/api/favorites/add", headers=headers, json={"platform": "taobao", "product_id": "tb_1"})
        prod_col.docs[1]["is_off_promotion"] = True
        client.post("/api/favorites/add", headers=headers, json={
            "platform": "pdd", "product_id": "pdd_missing", "goods_sign": "sign_m",
            "name": "快照商品", "image_url": "https://img.pdd.com/m.jpg", "price": 9.9,
        })

        # 让创建时间有确定顺序便于断言倒序
        base = datetime.datetime(2026, 11, 25, 10, 0, 0, tzinfo=datetime.timezone.utc)
        for i, d in enumerate(fav_col.docs):
            d["created_at"] = base + datetime.timedelta(minutes=i)

        res = client.get("/api/favorites/list?page=1&page_size=20", headers=headers).json()["data"]
        assert res["total"] == 3
        items = res["items"]
        assert items[0]["product_id"] == "pdd_missing"
        assert items[0]["live_status"] == "missing"
        assert items[0]["current_price"] is None
        assert items[0]["product"]["name"] == "快照商品"

        assert items[1]["product_id"] == "tb_1"
        assert items[1]["live_status"] == "off_promotion"
        assert items[1]["is_off_promotion"] is True
        assert items[1]["current_price"] is None

        assert items[2]["product_id"] == "jd_img_1"
        assert items[2]["live_status"] == "online"
        assert items[2]["collected_price"] == 19.9
        assert items[2]["current_price"] == 15.9
        assert items[2]["price_diff"] == -4.0

        # 4. 幂等移出
        assert client.post("/api/favorites/remove", headers=headers, json={"platform": "jd", "product_id": "jd_img_1"}).json()["data"]["removed"] is True
        assert client.post("/api/favorites/remove", headers=headers, json={"platform": "jd", "product_id": "jd_img_1"}).json()["data"]["removed"] is False


def test_favorites_500_item_cap(monkeypatch):
    svc, fav_col, _ = _setup_services(monkeypatch)
    for i in range(MAX_FAVORITES_PER_USER):
        fav_col.docs.append({"uid": "u_cap12345", "platform": "pdd", "product_id": f"p_{i}"})
    app = create_app()
    tok = create_access_token("u_cap12345", ttl=3600)
    with TestClient(app) as client:
        resp = client.post(
            "/api/favorites/add",
            headers={"Authorization": f"Bearer {tok}"},
            json={"platform": "pdd", "product_id": "overflow_item", "price": 10.0},
        )
        assert resp.status_code == 400
