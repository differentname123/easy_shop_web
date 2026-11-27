# -*- coding: utf-8 -*-
"""商品互动计数、订单有效单数回写、万能转链入库可搜索与云同步保护测试。"""
from copy import deepcopy
from dataclasses import replace
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import config
import main as main_module
from main import create_app
from models.product import ProductRef
from repositories.order_repo import OrderRepository
from repositories.product_repo import ProductRepository
from services import product_cloud_sync as sync_mod
from services.order_service import OrderService
from services.product_stats_service import MAX_PENDING_PRODUCTS, ProductStatsBuffer
from services.promote_service import PromoteService
from tests.test_order_sync_protocol import Adapter, MemorySyncRepository, MemoryUserRepository, page
from tests.test_product_cloud_sync import _FakeSyncCollection
from tests.test_product_repository import MemoryCollection as ProductMemoryCollection


def _apply_update_doc(target: dict, update_doc: dict) -> bool:
    before = deepcopy(target)
    for k, v in (update_doc.get("$set") or {}).items():
        if "." in k:
            parent, child = k.split(".", 1)
            if not isinstance(target.get(parent), dict):
                target[parent] = {}
            target[parent][child] = deepcopy(v)
        else:
            target[k] = deepcopy(v)
    for k, v in (update_doc.get("$inc") or {}).items():
        if "." in k:
            parent, child = k.split(".", 1)
            if not isinstance(target.get(parent), dict):
                target[parent] = {}
            target[parent][child] = int(target[parent].get(child, 0) or 0) + int(v)
        else:
            target[k] = int(target.get(k, 0) or 0) + int(v)
    for k in (update_doc.get("$unset") or {}).keys():
        if "." in k:
            parent, child = k.split(".", 1)
            if isinstance(target.get(parent), dict):
                target[parent].pop(child, None)
        else:
            target.pop(k, None)
    return target != before


class _FullMemoryProductCollection(ProductMemoryCollection):
    """在标准 ProductMemoryCollection 基础上支持 bulk_write ($set/$inc, upsert=False) 与单条 find_one。"""

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if self.matches(doc, query):
                return self.project(doc, projection)
        return None

    def update_one(self, filter_spec, update_doc, upsert=False):
        with self.lock:
            for doc in self.docs:
                if self.matches(doc, filter_spec):
                    modified = _apply_update_doc(doc, update_doc)
                    return SimpleNamespace(acknowledged=True, matched_count=1, modified_count=int(modified), upserted_id=None)
            if upsert:
                new_doc = {}
                for k, v in (update_doc.get("$setOnInsert") or {}).items():
                    new_doc[k] = deepcopy(v)
                _apply_update_doc(new_doc, update_doc)
                new_doc.setdefault("_id", f"oid_{len(self.docs) + 1}")
                self.docs.append(new_doc)
                return SimpleNamespace(acknowledged=True, matched_count=0, modified_count=0, upserted_id=new_doc["_id"])
            return SimpleNamespace(acknowledged=True, matched_count=0, modified_count=0, upserted_id=None)

    def bulk_write(self, ops, ordered=False):
        matched = 0
        modified = 0
        upserted = 0
        with self.lock:
            for op in ops:
                allow_upsert = bool(getattr(op, "_upsert", True))
                hit = False
                for doc in self.docs:
                    if self.matches(doc, op._filter):
                        hit = True
                        matched += 1
                        if _apply_update_doc(doc, op._doc):
                            modified += 1
                        break
                if not hit and allow_upsert:
                    new_doc = {}
                    for k, v in (op._doc.get("$setOnInsert") or {}).items():
                        new_doc[k] = deepcopy(v)
                    _apply_update_doc(new_doc, op._doc)
                    self.docs.append(new_doc)
                    upserted += 1
        return SimpleNamespace(acknowledged=True, matched_count=matched, modified_count=modified, upserted_count=upserted)


class _MemoryOrderCollection:
    def __init__(self):
        self.docs = []
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def upsert_raw(self, orders, platform):
        for o in orders:
            sn = o["order_sn"]
            existing = next((d for d in self.docs if d.get("platform") == platform and d.get("order_sn") == sn), None)
            item = {**deepcopy(o), "platform": platform}
            if existing is None:
                self.docs.append(item)
            else:
                existing.clear()
                existing.update(item)
        return len(orders)

    def aggregate(self, pipeline):
        match_q = pipeline[0]["$match"]
        plat = match_q["platform"]
        gids = set(match_q["goods_id"]["$in"])
        counts = {}
        for d in self.docs:
            if d.get("platform") != plat or d.get("goods_id") not in gids:
                continue
            if d.get("is_refund") is True or d.get("is_invalid") is True:
                continue
            counts[d["goods_id"]] = counts.get(d["goods_id"], 0) + 1
        return [{"_id": gid, "valid_orders": cnt} for gid, cnt in counts.items()]


def test_product_stats_buffer_dedup_bounds_and_never_touches_updated_at():
    col = _FullMemoryProductCollection()
    old_updated = datetime.datetime(2026, 11, 20, 8, 0, 0)
    col.docs.append({
        "platform": "pdd",
        "product_id": "10001",
        "name": "测试抽纸",
        "activity_price": 19.9,
        "updated_at": old_updated,
    })
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: col))
    buf = ProductStatsBuffer(lambda: repo)

    # 空桶刷盘绝不访问仓储
    assert buf.flush() == 0

    # 同主体 5 分钟内重复 view / intent 去重
    assert buf.record("pdd", "10001", "view", "ip:1.1.1.1") is True
    assert buf.record("pdd", "10001", "view", "ip:1.1.1.1") is False
    assert buf.record("pdd", "10001", "view", "ip:2.2.2.2") is True
    assert buf.record("pdd", "10001", "intent", "ip:1.1.1.1") is True

    # buyable 与 unbuyable 共用同一价格反馈防重锁
    assert buf.record("pdd", "10001", "buyable", "u:u1") is True
    assert buf.record("pdd", "10001", "unbuyable", "u:u1") is False

    # 非法动作拒绝
    assert buf.record("pdd", "10001", "hack_field", "u:u1") is False

    # 不存在的商品也会安全被 upsert=False 忽略，绝不生成裸文档
    assert buf.record("pdd", "999999", "share", "u:u2") is True

    flushed = buf.flush()
    assert flushed == 2
    assert len(col.docs) == 1
    doc = col.docs[0]
    assert doc["updated_at"] == old_updated
    assert doc["user_stats"]["view"] == 2
    assert doc["user_stats"]["intent"] == 1
    assert doc["user_stats"]["buyable"] == 1
    assert doc["user_stats"].get("unbuyable", 0) == 0
    assert isinstance(doc["user_stats"]["updated_at"], datetime.datetime)

    # 队列容量上限保护
    for i in range(MAX_PENDING_PRODUCTS + 10):
        buf.record("pdd", f"cap_{i}", "view", "")
    assert len(buf._pending) == MAX_PENDING_PRODUCTS


def test_routes_record_view_intent_and_client_actions(monkeypatch):
    col = _FullMemoryProductCollection()
    col.docs.append({
        "platform": "pdd",
        "product_id": "555",
        "goods_id": "555",
        "goods_sign": "SIGN_555",
        "name": "路线测试商品",
        "price": 19.9,
        "activity_price": 19.9,
        "updated_at": datetime.datetime(2026, 11, 20, 0, 0, 0),
    })
    prod_repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: col))
    import api.home as home_api
    import api.promote as promo_api

    monkeypatch.setattr(home_api.product_service, "repo", prod_repo)
    monkeypatch.setattr(
        home_api.product_service,
        "get_goods_detail",
        lambda **k: {
            "status": "success",
            "data": {"platform": "pdd", "product_id": "555", "goods_id": "555", "goods_sign": "SIGN_555", "name": "路线测试商品", "price": 19.9},
        },
    )
    monkeypatch.setattr(
        promo_api.promote_service,
        "convert_link",
        lambda **k: {"status": "success", "final_link": "https://p.pinduoduo.com/ok"},
    )

    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )
    from public_boundary import encode_item_code

    app = create_app()
    code_555 = encode_item_code("pdd", "555")
    with TestClient(app) as client:
        assert client.get(f"/api/home/detail?item_code={code_555}").status_code == 200
        assert client.post("/api/promote/convert", json={"item_code": code_555}).status_code == 200
        assert client.post("/api/goods/action", json={"item_code": code_555, "action": "share"}).status_code == 200
        assert client.post("/api/goods/action", json={"item_code": code_555, "action": "buyable"}).status_code == 200
        assert client.post("/api/goods/action", json={"item_code": code_555, "action": "bad_action"}).status_code == 422

    # lifespan stop() 已自动 flush
    stats = col.docs[0]["user_stats"]
    assert stats["view"] == 1
    assert stats["intent"] == 1
    assert stats["share"] == 1
    assert stats["buyable"] == 1


def test_order_sync_backfills_valid_orders_idempotently_and_handles_refunds():
    prod_col = _FullMemoryProductCollection()
    old_updated = datetime.datetime(2026, 11, 20, 0, 0, 0)
    prod_col.docs.extend([
        {"platform": "pdd", "product_id": "G_PDD_1", "name": "PDD商品", "activity_price": 10.0, "updated_at": old_updated},
        {"platform": "jd", "product_id": "jd_img_hash_1", "product_url": "https://item.jd.com/1000999.html", "name": "JD商品", "activity_price": 20.0, "updated_at": old_updated},
    ])
    prod_repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: prod_col))
    order_col = _MemoryOrderCollection()
    order_repo = OrderRepository(db_instance=SimpleNamespace(get_collection=lambda _: order_col))
    order_repo.upsert_orders = lambda orders, platform="pdd": order_col.upsert_raw(orders, platform)

    current_orders = [
        {"order_sn": "O1", "goods_id": "G_PDD_1", "is_refund": False, "is_invalid": False},
        {"order_sn": "O2", "goods_id": "G_PDD_1", "is_refund": False, "is_invalid": False},
    ]
    adapter = Adapter(fetch=lambda **k: {"status": "success", "order_list": current_orders, "has_more": False})
    sync_repo = MemorySyncRepository()
    svc = OrderService(order_repo=order_repo, sync_repo=sync_repo, user_repo=MemoryUserRepository(), product_repo=prod_repo)

    with patch("services.order_service.get_adapter", return_value=adapter):
        assert svc._platform_lock("pdd").acquire(blocking=False)
        svc._sync_platform_orders_bg("pdd", end_time=1000, auto_continue=False)
        assert prod_col.docs[0]["user_stats"]["valid_orders"] == 2
        assert prod_col.docs[0]["updated_at"] == old_updated

        # 重叠窗再次同步同一批订单：结果幂等仍为 2
        sync_repo.states["pdd"]["pending"] = None
        assert svc._platform_lock("pdd").acquire(blocking=False)
        svc._sync_platform_orders_bg("pdd", end_time=1100, auto_continue=False)
        assert prod_col.docs[0]["user_stats"]["valid_orders"] == 2

        # 一笔退款、一笔失效后重算：精确归 0
        current_orders[0]["is_refund"] = True
        current_orders[1]["is_invalid"] = True
        sync_repo.states["pdd"]["pending"] = None
        assert svc._platform_lock("pdd").acquire(blocking=False)
        svc._sync_platform_orders_bg("pdd", end_time=1200, auto_continue=False)
        assert prod_col.docs[0]["user_stats"]["valid_orders"] == 0

        # JD 通过 product_url 反查回写
        jd_adapter = Adapter(fetch=lambda **k: page("JD1", goods_id="1000999", is_refund=False, is_invalid=False))
        with patch("services.order_service.get_adapter", return_value=jd_adapter):
            assert svc._platform_lock("jd").acquire(blocking=False)
            svc._sync_platform_orders_bg("jd", end_time=1000, auto_continue=False)
        assert prod_col.docs[1]["user_stats"]["valid_orders"] == 1


def test_universal_collect_persistence_search_visibility_and_cloud_sync_protection():
    prod_col = _FullMemoryProductCollection()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: prod_col))
    svc = PromoteService(product_repo=repo)

    valid_card = {
        "platform": "pdd",
        "goods_id": "77889900",
        "goods_sign": "sign_uc_77889900",
        "name": "维达超韧抽取式面巾纸",
        "image_url": "https://img.pddpic.com/vinda.jpg",
        "price": 24.9,
        "original_price": 34.9,
        "coupon_amount": 10.0,
        "has_coupon": True,
    }
    valid_promo = {
        "status": "success",
        "resolution_kind": "product",
        "final_link": "https://p.pinduoduo.com/uc",
        "product": valid_card,
    }
    assert svc._is_persistable_universal_product(valid_promo, valid_card) is True
    assert svc._is_persistable_universal_product({"status": "success", "resolution_kind": "coupon_or_venue"}, valid_card) is False
    assert svc._is_persistable_universal_product({"status": "success", "resolution_kind": "degraded_product"}, valid_card) is False

    svc._persist_universal_product_sync("pdd", valid_card, valid_promo, is_public=True)
    assert len(prod_col.docs) == 1
    stored = prod_col.docs[0]
    assert stored["is_user_collected"] is True
    assert stored["source"] == "universal_collect"
    assert stored.get("channel_type") is None

    # 爆单库内容过滤与关键词搜索能直接搜到该商品
    boom_res = repo.query_boom_products(platform="all", keyword="维达")
    assert len(boom_res["data"]) == 1
    assert boom_res["data"][0]["goods_id"] == "77889900"

    # 若原本是热销榜/线报商品，补入不抹除 channel_type 与 source
    prod_col.docs.append({
        "platform": "pdd",
        "product_id": "88990011",
        "goods_id": "88990011",
        "name": "清风原木抽纸",
        "image_url": "https://img.pddpic.com/qf.jpg",
        "price": 19.9,
        "activity_price": 19.9,
        "channel_type": 5,
        "source": "pdd_recommend_stream",
        "detail_updated_at": datetime.datetime(2026, 11, 20, 0, 0, 0),
    })
    svc._persist_universal_product_sync(
        "pdd",
        {
            "platform": "pdd",
            "goods_id": "88990011",
            "goods_sign": "sign_qf",
            "name": "清风原木抽纸",
            "image_url": "https://img.pddpic.com/qf.jpg",
            "price": 19.9,
        },
        {"status": "success", "resolution_kind": "product"},
        is_public=False,
    )
    qf_doc = next(d for d in prod_col.docs if d["product_id"] == "88990011")
    assert qf_doc["is_user_collected"] is True
    assert qf_doc["channel_type"] == 5
    assert qf_doc["source"] == "pdd_recommend_stream"

    # 云同步保护：本地旧/无 user_stats 与 is_user_collected=False 绝不覆盖云端已有值
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    cloud_col = _FakeSyncCollection(initial_docs=[{
        "_id": "cloud_1",
        "platform": "pdd",
        "product_id": "77889900",
        "name": "维达超韧抽取式面巾纸",
        "activity_price": 24.9,
        "updated_at": now - datetime.timedelta(minutes=5),
        "user_stats": {"view": 99, "intent": 22, "valid_orders": 5},
        "is_user_collected": True,
    }])
    local_newer = {
        "platform": "pdd",
        "product_id": "77889900",
        "name": "维达超韧抽取式面巾纸",
        "activity_price": 22.9,
        "updated_at": now,
        "user_stats": {"view": 0},
        "is_user_collected": False,
    }
    cloud_fp = sync_mod.extract_sync_fingerprint(cloud_col.docs[0])
    op = sync_mod.build_product_merge_operation(local_newer, cloud_fp)
    assert op is not None
    cloud_col.bulk_write([op])
    synced_doc = cloud_col.docs[0]
    assert synced_doc["activity_price"] == 22.9
    assert synced_doc["user_stats"] == {"view": 99, "intent": 22, "valid_orders": 5}
    assert synced_doc["is_user_collected"] is True


def test_order_count_supports_int64_and_user_stats_score_saturation():
    from bson import Int64
    from models.product import compute_user_stats_score

    # 1. 真实 PDD 订单 goods_id 可能是 Int64 / int，也必须精确命中并剔除退款/失效
    order_col = _MemoryOrderCollection()
    order_col.docs.extend([
        {"platform": "pdd", "order_sn": "O_INT64_1", "goods_id": Int64(783134917270), "is_refund": False, "is_invalid": None},
        {"platform": "pdd", "order_sn": "O_STR_1", "goods_id": "783134917270", "is_refund": False, "is_invalid": False},
        {"platform": "pdd", "order_sn": "O_REFUND", "goods_id": Int64(783134917270), "is_refund": True, "is_invalid": False},
    ])
    order_repo = OrderRepository(db_instance=SimpleNamespace(get_collection=lambda _: order_col))
    counts = order_repo.count_valid_orders_by_goods_ids("pdd", ["783134917270", "999999"])
    assert counts == {"783134917270": 2, "999999": 0}

    # 2. 维度饱和公式验证：默认维度权重=1(上限10)，valid_orders 权重=10(上限100)，防单维刷量且突出真实成交
    assert compute_user_stats_score(None) == 0.0
    assert compute_user_stats_score({"updated_at": datetime.datetime(2026, 11, 25), "flag": True, "zero": 0, "neg": -5, "bad": "2026-11-25"}) == 0.0
    single_dim_huge = compute_user_stats_score({"view": 100000})
    multi_dim_clicks = compute_user_stats_score({"view": 5, "intent": 5, "share": "3"})
    one_order_score = compute_user_stats_score({"valid_orders": Int64(1)})
    huge_orders_score = compute_user_stats_score({"valid_orders": 100000})
    assert 9.9 < single_dim_huge < 10.0
    assert multi_dim_clicks > single_dim_huge
    assert abs(one_order_score - 16.6667) < 1e-4
    assert one_order_score > multi_dim_clicks
    assert 99.0 < huge_orders_score < 100.0

    # 3. 计数写入与订单回写均自动重算 user_stats_score；清零自动移除；新增维度支持全量重算
    prod_col = _FullMemoryProductCollection()
    old_updated = datetime.datetime(2026, 11, 20, 0, 0, 0)
    prod_col.docs.extend([
        {"platform": "pdd", "product_id": "783134917270", "name": "高互动", "activity_price": 19.9, "channel_type": 5, "updated_at": old_updated - datetime.timedelta(hours=1)},
        {"platform": "pdd", "product_id": "222222", "name": "纯新品", "activity_price": 19.9, "channel_type": 5, "updated_at": old_updated},
    ])
    prod_repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: prod_col))

    prod_repo.bulk_inc_user_stats({("pdd", "783134917270"): {"view": 2, "intent": 1}})
    doc1 = prod_col.docs[0]
    assert doc1["updated_at"] == old_updated - datetime.timedelta(hours=1)
    score_after_inc = doc1["user_stats_score"]
    assert abs(score_after_inc - compute_user_stats_score({"view": 2, "intent": 1})) < 1e-9

    prod_repo.bulk_set_valid_orders("pdd", {"783134917270": 1})
    assert doc1["user_stats"]["valid_orders"] == 1
    assert abs(doc1["user_stats_score"] - (score_after_inc + one_order_score)) < 1e-3

    # 全部维度归零后，清理 user_stats_score 使其与无互动新品按时间竞争
    doc1["user_stats"] = {"valid_orders": 0}
    prod_repo.bulk_set_valid_orders("pdd", {"783134917270": 0})
    assert "user_stats_score" not in doc1

    # 后续新增任意整数字段后可直接全量重算
    doc1["user_stats"]["custom_extra_counter"] = 5
    prod_repo._refresh_user_stats_scores([{"$or": [{"user_stats": {"$exists": True}}, {"user_stats_score": {"$exists": True}}]}])
    assert abs(doc1["user_stats_score"] - 5.0) < 1e-9

    # 默认严选好价排序：高分品排在时间更新的无互动品之前
    boom_res = prod_repo.query_boom_products(platform="pdd")
    assert [x["product_id"] for x in boom_res["data"]] == ["783134917270", "222222"]
    assert boom_res["data"][0]["user_stats_score"] == 5.0
    assert boom_res["data"][1]["user_stats_score"] == 0.0

    # 云同步同样严格保护 user_stats_score 不被覆盖
    assert "user_stats_score" in sync_mod.NEVER_OVERWRITE_FIELDS

    # 4. unbuyable 权重 = -10 真实扣分，最低截断为 0.0 并从库内清理 user_stats_score
    penalized = compute_user_stats_score({"valid_orders": 1, "unbuyable": 1})
    assert penalized == 0.0
    pos_minus_unbuyable = compute_user_stats_score({"valid_orders": 5, "unbuyable": 1})
    assert abs(pos_minus_unbuyable - (50.0 - 100.0 * (1 / 6))) < 1e-4

    prod_col2 = _FullMemoryProductCollection()
    prod_col2.docs.append({
        "platform": "jd",
        "product_id": "NEG_TEST_1",
        "name": "扣分测试商品",
        "activity_price": 19.9,
        "user_stats": {"valid_orders": 1, "unbuyable": 0},
        "user_stats_score": 16.6667,
    })
    prod_repo2 = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: prod_col2))
    prod_repo2.bulk_inc_user_stats({("jd", "NEG_TEST_1"): {"unbuyable": 1}})
    assert prod_col2.docs[0]["user_stats"]["unbuyable"] == 1
    assert "user_stats_score" not in prod_col2.docs[0]

    # 5. extract_actor_key 防伪造 X-Forwarded-For，及 price_feedback 6 小时去重窗口
    from services.product_stats_service import PRICE_FEEDBACK_DEDUP_TTL_SECONDS, extract_actor_key

    assert PRICE_FEEDBACK_DEDUP_TTL_SECONDS == 21600
    spoof_req = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.99"),
        headers={"x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8"},
    )
    assert extract_actor_key(spoof_req, uid=None) == "ip:203.0.113.99"
    proxy_req = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "1.2.3.4, 9.9.9.9"},
    )
    assert extract_actor_key(proxy_req, uid=None) == "ip:9.9.9.9"

    buf = ProductStatsBuffer(repo_provider=lambda: prod_repo2)
    assert buf.record("jd", "NEG_TEST_1", "buyable", "u:test_u1") is True
    assert buf.record("jd", "NEG_TEST_1", "unbuyable", "u:test_u1") is False


def test_universal_collect_preserves_existing_search_caches_and_stays_searchable():
    from cache import BOOM_SORT_CACHE
    from services.product_service import ProductService

    prod_col = _FullMemoryProductCollection()
    repo = ProductRepository(db_instance=SimpleNamespace(get_collection=lambda _: prod_col))
    promo_svc = PromoteService(product_repo=repo)
    prod_svc = ProductService(product_repo=repo)

    BOOM_SORT_CACHE.clear()
    BOOM_SORT_CACHE.set(("all", "", "无关词", "cost_performance"), [{"product_id": "OTHER"}])

    card = {
        "platform": "jd",
        "goods_id": "6aca4d10F381a5657_0944320320d0cf17",
        "goods_sign": "sign_seasoning_1",
        "name": "海天等品牌厨房调料组合套装12件套",
        "category": "调料",
        "image_url": "https://img14.360buyimg.com/pop/jfs/t1/1/1/1/1/6aca4d10F381a5657/0944320320d0cf17.jpg",
        "price": 18.9,
        "activity_price": 18.9,
    }
    promo_svc._persist_universal_product_sync(
        "jd",
        card,
        {"status": "success", "resolution_kind": "product"},
        is_public=True,
    )

    # 不清空全站已有查询缓存（靠 120s TTL 自然过期，避免缓存击穿）
    assert BOOM_SORT_CACHE.get(("all", "", "无关词", "cost_performance")) == [{"product_id": "OTHER"}]

    # 未命中旧缓存的正常搜索可直接查到该商品
    found = prod_svc.get_boom_products(platform="all", keyword="厨房调料")
    assert len(found["data"]) == 1
    assert found["data"][0]["goods_id"] == "6aca4d10F381a5657_0944320320d0cf17"
