# -*- coding: utf-8 -*-
"""
单元测试：本地商品库 -> 云端商品库 (`products`) 检查合并同步协议回归验证。
"""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

_MOBILE_WEB_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _MOBILE_WEB_DIR.parent
for _p in (str(_REPO_ROOT), str(_MOBILE_WEB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mobile_web.scripts import run_product_cloud_sync_scheduler as sync_mod
from tests.test_product_repository import MemoryCollection as ProductMemoryCollection


class _FakeBulkWriteResult:
    def __init__(self, matched: int, modified: int, upserted: int):
        self.acknowledged = True
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_count = upserted


class _FakeSyncCollection:
    """支持真实 Mongo 过滤、投影与 UpdateOne(upsert=True) 的离线内存集合。"""

    def __init__(self, initial_docs: list[dict] | None = None):
        self.docs: list[dict] = [copy.deepcopy(d) for d in (initial_docs or [])]
        self.bulk_write_calls = 0

    def _project(self, doc: dict, projection: dict | None) -> dict:
        if not projection:
            return copy.deepcopy(doc)
        out: dict = {}
        for k, flag in projection.items():
            if k == "_id" and flag == 0:
                continue
            if k == "_fmt_count" and isinstance(flag, dict):
                fi = doc.get("format_info")
                out["_fmt_count"] = len(fi) if isinstance(fi, dict) else 0
                continue
            if not flag:
                continue
            if "." in k:
                parent, child = k.split(".", 1)
                if isinstance(doc.get(parent), dict) and child in doc[parent]:
                    out.setdefault(parent, {})[child] = copy.deepcopy(doc[parent][child])
            elif k in doc:
                out[k] = copy.deepcopy(doc[k])
        return out

    def find(self, query: dict | None = None, projection: dict | None = None):
        q = query or {}
        return [self._project(doc, projection) for doc in self.docs if ProductMemoryCollection.matches(doc, q)]

    def delete_many(self, filt: dict):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not ProductMemoryCollection.matches(d, filt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def bulk_write(self, operations, ordered: bool = False):
        self.bulk_write_calls += 1
        matched = 0
        modified = 0
        upserted = 0
        for op in operations:
            filt = op._filter
            upd = op._doc
            target = next(
                (
                    d for d in self.docs
                    if d.get("platform") == filt["platform"] and d.get("product_id") == filt["product_id"]
                ),
                None,
            )
            if target is None:
                new_doc = {
                    "_id": f"cloud_oid_{len(self.docs) + 1}",
                    "platform": filt["platform"],
                    "product_id": filt["product_id"],
                }
                for k, v in upd.get("$setOnInsert", {}).items():
                    new_doc[k] = copy.deepcopy(v)
                for k, v in upd.get("$set", {}).items():
                    new_doc[k] = copy.deepcopy(v)
                self.docs.append(new_doc)
                upserted += 1
            else:
                matched += 1
                before = copy.deepcopy(target)
                for k, v in upd.get("$set", {}).items():
                    target[k] = copy.deepcopy(v)
                if target != before:
                    modified += 1
        return _FakeBulkWriteResult(matched=matched, modified=modified, upserted=upserted)


def test_uri_masking_and_self_sync_guard(tmp_path):
    raw_uri = "mongodb://localUser:localPassword123@124.220.169.204:27017/crawler_data?authSource=admin"
    masked = sync_mod.mask_mongo_uri(raw_uri)
    assert "localPassword123" not in masked
    assert "localUser:***@124.220.169.204:27017/crawler_data" in masked

    bad_cfg = tmp_path / "bad_config.json"
    bad_cfg.write_text(
        json.dumps({
            "local_mongo_host": "localhost",
            "local_mongo_port": 27017,
            "local_mongo_db_name": "crawler_data",
            "cloud_mongo_uri": "mongodb://u:p@127.0.0.1:27017/crawler_data?authSource=admin",
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="安全拦截"):
        sync_mod.resolve_cloud_mongo_target(config_file=bad_cfg)

    good_cfg = tmp_path / "good_config.json"
    good_cfg.write_text(
        json.dumps({
            "local_mongo_host": "localhost",
            "local_mongo_port": 27017,
            "local_mongo_db_name": "crawler_data",
            "cloud_mongo_uri": raw_uri,
        }),
        encoding="utf-8",
    )
    uri, db_name, host, port = sync_mod.resolve_cloud_mongo_target(config_file=good_cfg)
    assert uri == raw_uri
    assert db_name == "crawler_data"
    assert host == "124.220.169.204"
    assert port == 27017


def test_sync_round_insert_merge_protect_cloud_fields_and_idempotency():
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    t_old = now - datetime.timedelta(days=2)
    t_mid = now - datetime.timedelta(hours=2)
    t_recent = now - datetime.timedelta(minutes=5)
    t_ancient = now - datetime.timedelta(days=10)

    local_docs = [
        # 1. 全新线报商品（云端不存在）-> 应整条新增，但剥离本地 _id 与 default_promotion
        {
            "_id": "local_oid_1",
            "platform": "jd",
            "product_id": "6ae3f40fF84b3cfef_0083320320005d73",
            "goods_sign": "jd_enc_sign_fresh",
            "name": "维达抽纸24包整箱",
            "price": 39.9,
            "activity_price": 29.9,
            "created_at": t_mid,
            "updated_at": t_recent,
            "default_promotion": {"final_link": "https://u.jd.com/local_only"},
            "default_promotion_expires_at": now + datetime.timedelta(minutes=20),
            "deal_info": {
                "is_deal_tip": True,
                "quality_score": 88,
                "source_item_id": "tip_1001",
                "deal_price": 19.9,
                "clean_title": "维达抽纸24包 到手19.9元",
                "tutorial_text": "领券拍1件实付19.9元",
                "format_status": "success",
                "format_updated_at": t_recent,
            },
        },
        # 2. 本地比云端更新的同款商品，但本地无 channel_type、云端有 channel_type=5 和线上转链缓存 ->
        #    本地字段直接覆盖，但云端 channel_type=5、created_at、default_promotion 必须完整保留！
        {
            "_id": "local_oid_2",
            "platform": "pdd",
            "product_id": "8800112233",
            "goods_sign": "pdd_sign_newer_from_local",
            "name": "农夫山泉饮用天然水550ml*24瓶",
            "price": 28.8,
            "activity_price": 24.5,
            "updated_at": t_recent,
            "format_status": "failed",  # 失败态 format 字段不应污染云端
            "format_error": "timeout",
            "deal_info": {
                "is_deal_tip": True,
                "quality_score": 82,
                "source_item_id": "tip_1002",
                "deal_price": 18.8,
                "clean_title": "农夫山泉24瓶整箱",
            },
        },
        # 3. 本地基础快照较旧（2天前），但今天刚由 goods_info_format.py 补洗完 format_info；
        #    而云端 5 分钟前刚刷新了最新实时价格和最新 goods_sign ->
        #    保护云端新价格与新 goods_sign 不回退，仅把本地洗好的 format_info 合并上云！
        {
            "_id": "local_oid_3",
            "platform": "taobao",
            "product_id": "0v7DKOgI5JqnZd4Nhxd",
            "goods_sign": "stale_tb_prefix-0v7DKOgI5JqnZd4Nhxd",
            "name": "蓝月亮深层洁净洗衣液4kg*2",
            "price": 79.9,
            "activity_price": 69.9,
            "updated_at": t_old,
            "format_status": "success",
            "format_updated_at": t_recent,
            "format_info": {
                "0v7DKOgI5JqnZd4Nhxd_1": {
                    "price": 69.9,
                    "pricing_basis": {"base_unit": "g", "total_value": 8000},
                }
            },
        },
        # 4. 超过 3 天窗口的历史陈旧记录（10天前）-> 不在 3 天检查窗口内，跳过
        {
            "_id": "local_oid_4",
            "platform": "pdd",
            "product_id": "99999999",
            "name": "十天前的过期商品",
            "price": 9.9,
            "activity_price": 9.9,
            "updated_at": t_ancient,
        },
        # 5. 身份裸记录（无商品名/无价格）-> 自动过滤不同步
        {
            "_id": "local_oid_5",
            "platform": "pdd",
            "product_id": "77777777",
            "goods_sign": "bare_sign_only",
            "updated_at": t_recent,
        },
    ]

    cloud_initial_docs = [
        {
            "_id": "cloud_existing_2",
            "platform": "pdd",
            "product_id": "8800112233",
            "goods_sign": "pdd_sign_old_on_cloud",
            "name": "农夫山泉550ml*24瓶(旧标题)",
            "price": 29.9,
            "activity_price": 26.9,
            "channel_type": 5,
            "created_at": t_ancient,
            "updated_at": t_mid,
            "default_promotion": {"final_link": "https://p.pinduoduo.com/cloud_cached"},
            "format_status": "success",
            "format_updated_at": t_old,
            "format_info": {"8800112233_1": {"price": 26.9, "pricing_basis": {"base_unit": "ml", "total_value": 13200}}},
        },
        {
            "_id": "cloud_existing_3",
            "platform": "taobao",
            "product_id": "0v7DKOgI5JqnZd4Nhxd",
            "goods_sign": "fresh_cloud_prefix-0v7DKOgI5JqnZd4Nhxd",
            "name": "蓝月亮深层洁净洗衣液4kg*2",
            "price": 75.0,
            "activity_price": 58.0,  # 云端 5 分钟前刚刷出的最新低价
            "channel_type": 1,
            "created_at": t_old,
            "updated_at": t_recent,
        },
    ]

    local_col = _FakeSyncCollection(local_docs)
    cloud_col = _FakeSyncCollection(cloud_initial_docs)

    # 先测 dry_run：0 写入
    dry_summary = sync_mod.run_sync_round(local_col, cloud_col, window_days=3.0, dry_run=True)
    assert dry_summary["scanned_local_candidates"] == 3
    assert dry_summary["planned_sync_count"] == 3
    assert dry_summary["written_ops"] == 0
    assert cloud_col.bulk_write_calls == 0

    # 第 1 轮正式同步
    r1 = sync_mod.run_sync_round(local_col, cloud_col, window_days=3.0, batch_size=2, dry_run=False)
    assert r1["scanned_local_candidates"] == 3
    assert r1["planned_sync_count"] == 3
    assert r1["cloud_upserted_new"] == 1
    assert r1["cloud_modified_existing"] == 2

    by_key = {(d["platform"], d["product_id"]): d for d in cloud_col.docs}
    assert ("pdd", "99999999") not in by_key
    assert ("pdd", "77777777") not in by_key

    # 验证商品 1：整条新增且剥离本地 _id 和 default_promotion
    doc1 = by_key[("jd", "6ae3f40fF84b3cfef_0083320320005d73")]
    assert doc1["_id"] != "local_oid_1"
    assert "default_promotion" not in doc1
    assert doc1["deal_info"]["deal_price"] == 19.9
    assert doc1["created_at"] == t_mid

    # 验证商品 2：本地更新鲜 -> 价格/签名/线报按本地直接覆盖，但云端 channel_type=5、created_at、default_promotion 保留，且本地失败的 format_status 未污染云端已有成功 format_info
    doc2 = by_key[("pdd", "8800112233")]
    assert doc2["_id"] == "cloud_existing_2"
    assert doc2["activity_price"] == 24.5
    assert doc2["goods_sign"] == "pdd_sign_newer_from_local"
    assert doc2["deal_info"]["deal_price"] == 18.8
    assert doc2["channel_type"] == 5
    assert doc2["created_at"] == t_ancient
    assert doc2["default_promotion"] == {"final_link": "https://p.pinduoduo.com/cloud_cached"}
    assert doc2["format_status"] == "success"

    # 验证商品 3：云端实时快照更新鲜 -> 云端 58.0 新价格与 fresh_cloud_prefix 签名保留，本地新洗出的 format_info 成功合并上云
    doc3 = by_key[("taobao", "0v7DKOgI5JqnZd4Nhxd")]
    assert doc3["activity_price"] == 58.0
    assert doc3["goods_sign"] == "fresh_cloud_prefix-0v7DKOgI5JqnZd4Nhxd"
    assert doc3["format_status"] == "success"
    assert doc3["format_info"]["0v7DKOgI5JqnZd4Nhxd_1"]["pricing_basis"]["total_value"] == 8000

    # 第 2 轮紧接着再跑：双库已完全对齐，应 100% 跳过、0 次写库！
    writes_before = cloud_col.bulk_write_calls
    r2 = sync_mod.run_sync_round(local_col, cloud_col, window_days=3.0, dry_run=False)
    assert r2["scanned_local_candidates"] == 3
    assert r2["already_up_to_date"] == 3
    assert r2["planned_sync_count"] == 0
    assert r2["written_ops"] == 0
    assert cloud_col.bulk_write_calls == writes_before


def test_sync_detects_deal_info_updated_at_and_never_propagates_sku_info():
    now = datetime.datetime(2026, 11, 13, 3, 0, 0, tzinfo=datetime.timezone.utc)
    base_doc = {
        "platform": "jd",
        "product_id": "jd_deal_1",
        "name": "清风抽纸24包",
        "price": 39.9,
        "activity_price": 29.9,
        "updated_at": now - datetime.timedelta(hours=2),
        "sku_info": [{"价格": "￥29.9"}],
        "format_sku_prices": {"jd_deal_1_1": "￥29.9"},
        "deal_info": {
            "is_deal_tip": True,
            "source_item_id": "xb_1",
            "quality_score": 85,
            "deal_price": 19.9,
            "updated_at": now - datetime.timedelta(hours=2),
            "format_status": "success",
            "format_updated_at": now - datetime.timedelta(hours=2),
        },
    }
    cloud_fp = sync_mod.extract_sync_fingerprint(base_doc)
    refreshed_local = copy.deepcopy(base_doc)
    refreshed_local["deal_info"]["updated_at"] = now
    local_fp = sync_mod.extract_sync_fingerprint(refreshed_local)

    should_merge, reason = sync_mod.needs_cloud_merge(local_fp, cloud_fp)
    assert should_merge is True
    assert reason == "deal_info_changed"

    op = sync_mod.build_product_merge_operation(refreshed_local, cloud_fp)
    assert "sku_info" not in op._doc["$set"]
    assert "format_sku_prices" not in op._doc["$set"]


def test_cloud_sync_tombstones_deal_expiry_and_cloud_only_pullback():
    from unittest.mock import MagicMock

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    t_old = now - datetime.timedelta(hours=80)

    # 1) 本地已超 72h 摘除好价标 (is_deal_tip=False + expired_at)，云端仍为 is_deal_tip=True -> 自动同步摘标
    local_expired_deal = {
        "platform": "jd",
        "product_id": "jd_stale_deal_1",
        "name": "过期好价纸巾",
        "price": 29.9,
        "activity_price": 29.9,
        "updated_at": t_old,
        "deal_info": {
            "is_deal_tip": False,
            "expired_at": now,
            "updated_at": now,
            "deal_price": 9.9,
        },
    }
    cloud_active_deal = {
        "platform": "jd",
        "product_id": "jd_stale_deal_1",
        "name": "过期好价纸巾",
        "price": 29.9,
        "activity_price": 29.9,
        "updated_at": t_old,
        "deal_info": {
            "is_deal_tip": True,
            "updated_at": t_old,
            "deal_price": 9.9,
        },
    }
    l_fp = sync_mod.extract_sync_fingerprint(local_expired_deal)
    c_fp = sync_mod.extract_sync_fingerprint(cloud_active_deal)
    should_sync, reason = sync_mod.needs_cloud_merge(l_fp, c_fp)
    assert should_sync is True and reason == "deal_info_expired"
    op = sync_mod.build_product_merge_operation(local_expired_deal, c_fp)
    assert op._doc["$set"]["deal_info"]["is_deal_tip"] is False

    # 2) 墓碑跨云端删除 + 云端独有未清洗推荐流商品回拉到本地（自动排除已墓碑删除商品，走真实 Query 过滤）
    tombstone_col = MagicMock()
    tombstone_col.find.return_value = [{"platform": "pdd", "product_id": "dead_off_promo_1", "deleted_at": now}]

    local_col = _FakeSyncCollection([])
    cloud_col = _FakeSyncCollection(
        [
            {
                "_id": "c_dead",
                "platform": "pdd",
                "product_id": "dead_off_promo_1",
                "name": "已下架死链商品",
                "price": 19.9,
                "activity_price": 19.9,
                "updated_at": now,
            },
            {
                "_id": "c_stream_unfmt",
                "platform": "pdd",
                "product_id": "cloud_stream_new_1",
                "goods_sign": "E9_STREAM_1",
                "name": "云端推荐流新商品待规格清洗",
                "price": 15.8,
                "activity_price": 15.8,
                "image_url": "https://img.pddpic.com/s1.jpg",
                "updated_at": now,
                "default_promotion": {"final_link": "https://p.pinduoduo.com/cloud_only"},
            },
            {
                "_id": "c_already_fmt",
                "platform": "pdd",
                "product_id": "cloud_already_formatted",
                "name": "云端已清洗商品不应被回拉",
                "price": 25.0,
                "activity_price": 25.0,
                "updated_at": now,
                "format_status": "success",
            },
        ]
    )

    rep = sync_mod.run_sync_round(
        local_col,
        cloud_col,
        window_days=3.0,
        tombstone_col=tombstone_col,
        pull_cloud_unformatted=True,
    )
    assert rep["cloud_deleted_tombstones"] == 1
    assert rep["local_pulled_cloud_unformatted"] == 1
    assert all(d["product_id"] != "dead_off_promo_1" for d in cloud_col.docs)
    assert len(local_col.docs) == 1
    assert local_col.docs[0]["product_id"] == "cloud_stream_new_1"
    assert "default_promotion" not in local_col.docs[0]

