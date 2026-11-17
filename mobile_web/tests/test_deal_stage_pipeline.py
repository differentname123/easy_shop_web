"""
单元测试：阶段化流水线状态机、租约认领与互斥、阶段单元函数与并发工作线程验证
"""
from __future__ import annotations

import copy
import datetime
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from mobile_web.scripts.run_deal_pool_scheduler import (
    CLEAN_STAGE_DONE,
    CLEAN_STAGE_NEED_AI1,
    CLEAN_STAGE_NEED_AI2,
    CLEAN_STAGE_NEED_CONVERT,
    DEFAULT_STAGE_LEASE_SECONDS_AI,
    DEFAULT_STAGE_LEASE_SECONDS_CONVERT,
    ERR_ADAPTER_CONVERT_FAILED,
    ERR_AI_TIMEOUT_RETRY,
    ERR_NO_ANCHOR,
    DealPoolCleaner,
    DealRawPoolRepository,
    StageWorker,
    _commit_batch_results_to_db,
    run_stage_pipeline_drain,
)


class _MemMongoCollection:
    """简易内存 MongoDB Collection，支持 filter, update_many, find, count_documents 等。"""
    def __init__(self, initial_docs: Optional[List[Dict[str, Any]]] = None):
        self.docs: List[Dict[str, Any]] = copy.deepcopy(initial_docs or [])

    def create_index(self, *args, **kwargs):
        pass

    def distinct(self, key, flt=None):
        vals = set()
        for doc in self.docs:
            if flt is None or self._match(doc, flt):
                v = doc.get(key)
                if v:
                    vals.add(v)
        return list(vals)

    def _match(self, doc: Dict[str, Any], flt: Dict[str, Any]) -> bool:
        if not flt:
            return True
        for k, v in flt.items():
            if k == "$and":
                if not all(self._match(doc, cond) for cond in v):
                    return False
            elif k == "$or":
                if not any(self._match(doc, cond) for cond in v):
                    return False
            elif k == "$nor":
                if any(self._match(doc, cond) for cond in v):
                    return False
            elif k == "_id":
                if isinstance(v, dict) and "$in" in v:
                    if doc.get("_id") not in v["$in"]:
                        return False
                elif doc.get("_id") != v:
                    return False
            elif isinstance(v, dict):
                field_val = doc.get(k)
                for op, op_val in v.items():
                    if op == "$exists":
                        exists = k in doc and doc[k] is not None
                        if exists != op_val:
                            return False
                    elif op == "$ne":
                        if field_val == op_val:
                            return False
                    elif op == "$in":
                        if field_val not in op_val:
                            return False
                    elif op == "$lt":
                        if field_val is None or not (field_val < op_val):
                            return False
                    elif op == "$gt":
                        if field_val is None or not (field_val > op_val):
                            return False
                    elif op == "$gte":
                        if field_val is None or not (field_val >= op_val):
                            return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    def find(self, flt: Optional[Dict[str, Any]] = None, projection: Optional[Dict[str, Any]] = None):
        flt = flt or {}
        matched = [copy.deepcopy(d) for d in self.docs if self._match(d, flt)]

        class _Cursor:
            def __init__(self, data):
                self.data = data

            def sort(self, sort_spec):
                # 简单模拟排序
                return self

            def limit(self, n):
                self.data = self.data[:n]
                return self

            def __iter__(self):
                return iter(self.data)

            def __list__(self):
                return self.data

        return _Cursor(matched)

    def count_documents(self, flt: Optional[Dict[str, Any]] = None) -> int:
        flt = flt or {}
        return sum(1 for d in self.docs if self._match(d, flt))

    def update_many(self, flt: Dict[str, Any], upd: Dict[str, Any]):
        modified = 0
        for doc in self.docs:
            if self._match(doc, flt):
                if "$set" in upd:
                    for k, v in upd["$set"].items():
                        doc[k] = v
                if "$unset" in upd:
                    for k in upd["$unset"]:
                        doc.pop(k, None)
                modified += 1
        return SimpleNamespace(modified_count=modified)

    def bulk_write(self, ops, ordered=False):
        modified = 0
        for op in ops:
            flt = op._filter
            upd = op._doc
            res = self.update_many(flt, upd)
            modified += res.modified_count
        return SimpleNamespace(modified_count=modified)


def test_stage_constants_and_repository_migration():
    """测试状态机常量与历史数据迁移幂等性。"""
    assert CLEAN_STAGE_NEED_AI1 == "need_ai1"
    assert CLEAN_STAGE_NEED_CONVERT == "need_convert"
    assert CLEAN_STAGE_NEED_AI2 == "need_ai2"
    assert CLEAN_STAGE_DONE == "done"

    initial_docs = [
        {"_id": "1", "source_site": "zhetaoke", "source_item_id": "1", "clean_status": "pending"},
        {"_id": "2", "source_site": "guangdiu", "source_item_id": "2", "clean_status": "cleaned_success"},
        {"_id": "3", "source_site": "xianbao_fun", "source_item_id": "3", "clean_status": "cleaned_failed", "stage_lease_owner": "old_lease"},
    ]
    raw_col = _MemMongoCollection(initial_docs)
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: raw_col))
    repo._indexes_ready = True

    mig_res = repo.migrate_pending_to_stage_machine()
    assert mig_res["migrated_pending"] == 1
    assert mig_res["migrated_done"] == 2
    assert mig_res["cleared_leases"] == 1

    docs_by_id = {d["_id"]: d for d in raw_col.docs}
    assert docs_by_id["1"]["clean_stage"] == CLEAN_STAGE_NEED_AI1
    assert docs_by_id["2"]["clean_stage"] == CLEAN_STAGE_DONE
    assert docs_by_id["3"]["clean_stage"] == CLEAN_STAGE_DONE
    assert "stage_lease_owner" not in docs_by_id["3"]

    # 再次运行应幂等（0 变更）
    mig_res2 = repo.migrate_pending_to_stage_machine()
    assert mig_res2["migrated_pending"] == 0
    assert mig_res2["migrated_done"] == 0


def test_atomic_claim_stage_docs_and_lease_expiry():
    """测试阶段租约认领、互斥隔离与过期清扫。"""
    now = time.time()
    initial_docs = [
        {"_id": "d1", "source_site": "zhetaoke", "source_item_id": "d1", "clean_status": "pending", "clean_stage": "need_ai1", "publish_ts": now - 100},
        {"_id": "d2", "source_site": "guangdiu", "source_item_id": "d2", "clean_status": "pending", "clean_stage": "need_ai1", "publish_ts": now - 90},
        {"_id": "d3", "source_site": "xianbao_fun", "source_item_id": "d3", "clean_status": "pending", "clean_stage": "need_convert", "publish_ts": now - 80},
    ]
    raw_col = _MemMongoCollection(initial_docs)
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda _n: raw_col))
    repo._indexes_ready = True

    # 1. Worker A 认领 stage='need_ai1'，limit=2
    claimed_a = repo.claim_stage_docs(stage="need_ai1", limit=2, lease_seconds=60, worker_id="worker_A")
    assert len(claimed_a) == 2
    for d in claimed_a:
        assert d["_id"] in ("d1", "d2")
        assert "worker_A" in d["stage_lease_owner"]
        assert d["stage_lease_until_ts"] > now

    # 2. Worker B 此时尝试认领 stage='need_ai1'，已被 Worker A 锁定，应该返回 0 条
    claimed_b = repo.claim_stage_docs(stage="need_ai1", limit=2, lease_seconds=60, worker_id="worker_B")
    assert len(claimed_b) == 0

    # 3. Worker C 认领 stage='need_convert'，互不影响
    claimed_c = repo.claim_stage_docs(stage="need_convert", limit=2, lease_seconds=60, worker_id="worker_C")
    assert len(claimed_c) == 1
    assert claimed_c[0]["_id"] == "d3"

    # 4. 模拟租约过期
    for d in raw_col.docs:
        if d["_id"] == "d1":
            d["stage_lease_until_ts"] = now - 10.0  # 过期 10 秒

    reclaimed = repo.reclaim_expired_leases(now_ts=now)
    assert reclaimed == 1
    d1_now = next(d for d in raw_col.docs if d["_id"] == "d1")
    assert "stage_lease_owner" not in d1_now
    assert "stage_lease_until_ts" not in d1_now

    # 5. d1 租约释放后，Worker B 可以成功认领
    claimed_b2 = repo.claim_stage_docs(stage="need_ai1", limit=1, lease_seconds=60, worker_id="worker_B")
    assert len(claimed_b2) == 1
    assert claimed_b2[0]["_id"] == "d1"
    assert "worker_B" in claimed_b2[0]["stage_lease_owner"]


def test_commit_batch_results_stage_transitions():
    """测试 _commit_batch_results_to_db 正确处理中间阶段流转与终态落盘。"""
    raw_col = _MemMongoCollection([
        {"_id": "1", "source_site": "zhetaoke", "source_item_id": "1", "clean_status": "pending", "clean_stage": "need_ai1", "stage_lease_owner": "w1"},
        {"_id": "2", "source_site": "guangdiu", "source_item_id": "2", "clean_status": "pending", "clean_stage": "need_convert", "stage_lease_owner": "w2"},
        {"_id": "3", "source_site": "smzdm", "source_item_id": "3", "clean_status": "pending", "clean_stage": "need_ai2", "stage_lease_owner": "w3"},
    ])
    formal_col = _MemMongoCollection([])

    results = [
        # AI-1 产出：推进至 need_convert
        {
            "source_site": "zhetaoke",
            "source_item_id": "1",
            "status": "in_progress",
            "stage_outcome": "advance_to_convert",
            "ai_stage1_result": {"extracted_targets": [{"link_id": "L1"}]},
        },
        # 转链 产出：推进至 need_ai2
        {
            "source_site": "guangdiu",
            "source_item_id": "2",
            "status": "in_progress",
            "stage_outcome": "advance_to_ai2",
            "converted_links": [{"link_id": "L1", "convert_status": "product"}],
        },
        # AI-2 终审产出：终态 success
        {
            "source_site": "smzdm",
            "source_item_id": "3",
            "status": "success",
            "stage_outcome": "success",
            "platform": "jd",
            "product_id": "10001",
            "goods_sign": "gs_10001",
            "official_title": "测试商品",
            "product_doc": {"platform": "jd", "product_id": "10001", "name": "测试商品", "price": 99.0},
        },
    ]

    commit_res = _commit_batch_results_to_db(results, raw_col, formal_col, cycle_num=1)
    assert len(commit_res["success_product_docs"]) == 1

    docs_by_id = {d["source_item_id"]: d for d in raw_col.docs}

    # 1 号线报：推进至 need_convert，清除租约
    assert docs_by_id["1"]["clean_stage"] == CLEAN_STAGE_NEED_CONVERT
    assert docs_by_id["1"]["clean_status"] == "pending"
    assert "stage_lease_owner" not in docs_by_id["1"]
    assert docs_by_id["1"]["ai_stage1_result"]["extracted_targets"][0]["link_id"] == "L1"

    # 2 号线报：推进至 need_ai2，清除租约
    assert docs_by_id["2"]["clean_stage"] == CLEAN_STAGE_NEED_AI2
    assert docs_by_id["2"]["clean_status"] == "pending"
    assert "stage_lease_owner" not in docs_by_id["2"]
    assert len(docs_by_id["2"]["converted_links"]) == 1

    # 3 号线报：终态 success，clean_stage=done，清除租约
    assert docs_by_id["3"]["clean_stage"] == CLEAN_STAGE_DONE
    assert docs_by_id["3"]["clean_status"] == "cleaned_success"
    assert "stage_lease_owner" not in docs_by_id["3"]


def test_cleaner_modular_units_and_clean_batch_records():
    """测试 Cleaner 三个独立单元函数及其统一驱动 clean_batch_records。"""
    cleaner = DealPoolCleaner(ai_batch_size=10, ai_max_workers=2, convert_max_workers=2)
    cleaner._cache_warmed_up = True

    raw_docs = [
        {
            "source_site": "guangdiu",
            "source_item_id": "t100",
            "source_channel": "guangdiu:最新",
            "title": "维达抽纸 3包",
            "tutorial_text": "拍3件仅19.9包邮 https://item.jd.com/10001.html",
            "extracted_links": [{"url": "https://item.jd.com/10001.html"}],
        }
    ]

    # Mock Stage 1
    mock_ai1_res = {
        "is_standalone_deal": True,
        "combined_raw_text": "拍3件仅19.9包邮 https://item.jd.com/10001.html",
        "extracted_targets": [
            {
                "link_id": "L1",
                "clean_input": "https://item.jd.com/10001.html",
                "platform": "jd",
                "text_role_hint": "main_candidate",
            }
        ],
    }

    # Mock Stage 3 转链
    mock_conv_res = {
        "link_id": "L1",
        "raw_span": "https://item.jd.com/10001.html",
        "clean_input": "https://item.jd.com/10001.html",
        "text_role_hint": "main_candidate",
        "platform": "jd",
        "convert_status": "product",
        "goods_id": "10001",
        "goods_sign": "gs_10001",
        "my_promo_link": "https://u.jd.com/my_promo",
        "my_promo_token": "",
        "official_product": {"title": "维达超韧抽纸 100抽*3包", "price": 29.9},
        "adapter_res": {"product": {"name": "维达超韧抽纸 100抽*3包", "price": 29.9}},
    }

    # Mock Stage 4 终审
    mock_ai2_res = {
        "is_valid_deal": True,
        "main_link_id": "L1",
        "deal_price": 19.9,
        "quality_score": 85,
        "clean_title": "维达超韧抽纸 3包",
        "tutorial_steps": ["1. 领券下单拍3件立减"],
    }

    with patch.object(cleaner, "run_ai_stage1_batch", return_value={"t100": mock_ai1_res}), \
         patch.object(cleaner, "run_stage3_concurrent_conversions", return_value=None), \
         patch.object(cleaner, "convert_single_target", return_value=mock_conv_res), \
         patch.object(cleaner, "run_ai_stage2_batch", return_value={"t100": mock_ai2_res}):

        # 1. 单独测试 process_ai1_unit
        ai1_out, t_ai1, _ = cleaner.process_ai1_unit(raw_docs, raw_col=None)
        assert len(ai1_out) == 1
        assert ai1_out[0]["stage_outcome"] == "advance_to_convert"

        # 2. 单独测试 process_convert_unit
        adv_doc = copy.deepcopy(raw_docs[0])
        adv_doc["ai_stage1_result"] = ai1_out[0]["ai_stage1_result"]
        conv_out, t_conv = cleaner.process_convert_unit([adv_doc])
        assert len(conv_out) == 1
        assert conv_out[0]["stage_outcome"] == "advance_to_ai2"

        # 3. 单独测试 process_ai2_unit
        adv_ai2_doc = copy.deepcopy(adv_doc)
        adv_ai2_doc["converted_links"] = conv_out[0]["converted_links"]
        adv_ai2_doc["full_conv_list"] = conv_out[0]["full_conv_list"]
        ai2_out, t_ai2 = cleaner.process_ai2_unit([adv_ai2_doc])
        assert len(ai2_out) == 1
        assert ai2_out[0]["stage_outcome"] == "success"
        assert ai2_out[0]["deal_price"] == 19.9
        assert ai2_out[0]["quality_score"] == 85

        # 4. 综合测试 clean_batch_records 驱动
        final_results = cleaner.clean_batch_records(raw_docs, raw_col=None)
        assert len(final_results) == 1
        assert final_results[0]["status"] == "success"
        assert final_results[0]["deal_price"] == 19.9
        assert final_results[0]["product_id"] == "10001"
        assert "_stage_timings" in final_results[0]


def test_stage_workers_pipeline_drain_parallel():
    """测试 StageWorker 并发流水线排空模式。"""
    raw_docs = [
        {"_id": "m1", "source_site": "guangdiu", "source_item_id": "m1", "clean_status": "pending", "clean_stage": "need_ai1", "title": "测试1", "publish_ts": time.time()},
        {"_id": "m2", "source_site": "zhetaoke", "source_item_id": "m2", "clean_status": "pending", "clean_stage": "need_ai1", "title": "测试2", "publish_ts": time.time()},
    ]
    raw_col = _MemMongoCollection(raw_docs)
    formal_col = _MemMongoCollection([])
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda n: raw_col if n == "deal_raw_tips" else formal_col))
    repo._indexes_ready = True

    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=2, convert_max_workers=2)
    cleaner._cache_warmed_up = True

    # Mock 各阶段纯单元输出
    def mock_ai1_unit(docs, *args, **kwargs):
        out = []
        for d in docs:
            out.append({
                "source_site": d["source_site"],
                "source_item_id": d["source_item_id"],
                "status": "in_progress",
                "stage_outcome": "advance_to_convert",
                "ai_stage1_result": {"extracted_targets": [{"link_id": "L1"}]},
            })
        return out, 0.01, {}

    def mock_convert_unit(docs, *args, **kwargs):
        out = []
        for d in docs:
            out.append({
                "source_site": d["source_site"],
                "source_item_id": d["source_item_id"],
                "status": "in_progress",
                "stage_outcome": "advance_to_ai2",
                "converted_links": [{"link_id": "L1", "convert_status": "product"}],
                "full_conv_list": [{"link_id": "L1", "convert_status": "product"}],
            })
        return out, 0.01

    def mock_ai2_unit(docs, *args, **kwargs):
        out = []
        for d in docs:
            out.append({
                "source_site": d["source_site"],
                "source_item_id": d["source_item_id"],
                "status": "success",
                "stage_outcome": "success",
                "platform": "jd",
                "product_id": f"p_{d['source_item_id']}",
                "goods_sign": "gs",
                "official_title": "正式名",
                "quality_score": 80,
                "product_doc": {"platform": "jd", "product_id": f"p_{d['source_item_id']}", "name": "正式名", "price": 10.0},
            })
        return out, 0.01

    with patch.object(cleaner, "process_ai1_unit", side_effect=mock_ai1_unit), \
         patch.object(cleaner, "process_convert_unit", side_effect=mock_convert_unit), \
         patch.object(cleaner, "process_ai2_unit", side_effect=mock_ai2_unit):

        drain_rep = run_stage_pipeline_drain(repo=repo, cleaner=cleaner, timeout_seconds=10.0)
        assert drain_rep["ai1_processed"] == 2
        assert drain_rep["convert_processed"] == 2
        assert drain_rep["ai2_processed"] == 2

        # 验证所有文档已全部到达 CLEAN_STAGE_DONE 且 clean_status='cleaned_success'
        for d in raw_col.docs:
            assert d["clean_stage"] == CLEAN_STAGE_DONE
            assert d["clean_status"] == "cleaned_success"
            assert "stage_lease_owner" not in d


def test_stage_worker_compute_safe_lease():
    """测试 StageWorker 自适应动态安全租约计算：小批量保底、大批量自适应扩容防超时。"""
    cleaner = DealPoolCleaner(ai_batch_size=4, ai_max_workers=8, convert_max_workers=10, ai_timeout=90.0)
    repo = MagicMock()

    worker_ai1 = StageWorker(
        name="StageWorker-AI1",
        stage=CLEAN_STAGE_NEED_AI1,
        repo=repo,
        cleaner=cleaner,
        batch_size=64,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_AI,  # 300
    )
    # 4条小批次时，租约不低于默认 300s
    assert worker_ai1._compute_safe_lease(4) >= DEFAULT_STAGE_LEASE_SECONDS_AI

    # 128条大批次时：rounds = 128 / (4*8) = 4轮，est_sec = (4+2)*90*1.5 = 810s，自适应扩容至 810s
    lease_128 = worker_ai1._compute_safe_lease(128)
    assert lease_128 >= 800

    # Convert 转链 Worker：5000条全量认领时自适应扩容租约
    worker_conv = StageWorker(
        name="StageWorker-Convert",
        stage=CLEAN_STAGE_NEED_CONVERT,
        repo=repo,
        cleaner=cleaner,
        batch_size=5000,
        lease_seconds=DEFAULT_STAGE_LEASE_SECONDS_CONVERT,  # 180
    )
    lease_conv = worker_conv._compute_safe_lease(5000)
    assert lease_conv > 1000  # 自动大幅扩容，防止转链途中被误回收

