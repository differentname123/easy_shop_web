# -*- coding: utf-8 -*-
"""详情来源时间 CAS：使用有条件匹配/唯一索引/真实 UpdateResult 的离线集合。"""
import datetime
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bson import BSON
from pymongo.errors import ConnectionFailure
from pymongo.results import UpdateResult

from repositories.product_repo import ProductRepository
from tests.test_product_repository import MemoryCollection


UTC = datetime.timezone.utc
SOURCE = datetime.datetime(2025, 1, 2, 3, 4, 5, 123000, tzinfo=UTC)
MILLISECOND = datetime.timedelta(milliseconds=1)


def snapshot(source=SOURCE, **fields):
    return {"platform": "pdd", "goods_id": "123", "goods_sign": "SIGN",
            "name": "商品", "price": 10, "desc": "来源详情",
            "detail_updated_at": source, **fields}


def repository(collection=None):
    collection = collection if collection is not None else MemoryCollection()
    repo = ProductRepository(SimpleNamespace(get_collection=lambda name: collection))
    return repo, collection


def bson_time(source):
    return BSON.encode({"time": source}).decode()["time"]


def assert_safe_writes(collection):
    for query, update, upsert in collection.write_calls:
        if upsert:
            assert set(update) == {"$setOnInsert"}
            assert "detail_updated_at" not in query
            assert "_id" not in query
        else:
            assert set(query) == {"_id", "detail_updated_at"}
            assert set(update) == {"$set"}


def test_old_prefetch_finishing_after_new_foreground_cannot_roll_back():
    read = threading.Event()
    release = threading.Event()

    class PausedRead(MemoryCollection):
        def find_one(self, query, projection=None):
            doc = super().find_one(query, projection)
            if threading.current_thread().name == "old-prefetch" and not read.is_set():
                read.set()
                assert release.wait(5), "foreground did not release prefetch"
            return doc

    repo, col = repository(PausedRead())
    assert repo.upsert_product_detail(snapshot(SOURCE - MILLISECOND, desc="最初版本"))
    results = []
    errors = []

    def prefetch():
        try:
            results.append(repo.upsert_product_detail(snapshot(SOURCE, price=11, desc="旧预取")))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=prefetch, name="old-prefetch")
    worker.start()
    try:
        assert read.wait(5)
        assert repo.upsert_product_detail(snapshot(SOURCE + MILLISECOND, price=12, desc="新前台"))
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert results == [False]
    assert col.docs[0]["desc"] == "新前台"
    assert col.docs[0]["activity_price"] == 12
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE + MILLISECOND)
    assert_safe_writes(col)


def test_new_foreground_time_after_call_start_is_not_mistaken_for_a_future_clock():
    clock = SimpleNamespace(now=SOURCE + 100 * MILLISECOND)
    datetime_type = datetime.datetime

    class Clock(datetime_type):
        @classmethod
        def now(cls, tz=None):
            return clock.now.replace(tzinfo=None) if tz is None else clock.now.astimezone(tz)

    class LaterForeground(MemoryCollection):
        raced = False

        def update_one(self, query, update, upsert=False):
            if "detail_updated_at" in query and not self.raced:
                self.raced = True
                clock.now += 100 * MILLISECOND
                super().update_one({"_id": query["_id"]}, {"$set": {
                    "detail_updated_at": bson_time(SOURCE + 150 * MILLISECOND), "desc": "较晚的新前台",
                }})
            return super().update_one(query, update, upsert)

    repo, col = repository(LaterForeground())
    col.docs = [{"platform": "pdd", "product_id": "123", "detail_updated_at": bson_time(SOURCE)}]
    with patch("repositories.product_repo.datetime.datetime", Clock):
        assert not repo.upsert_product_detail(snapshot(SOURCE + MILLISECOND, desc="旧预取"))
    assert col.docs[0]["desc"] == "较晚的新前台"
    assert len(col.queries) == 2


@pytest.mark.parametrize("offset", [0, 1, 998])
def test_same_bson_millisecond_is_not_a_new_version(offset):
    repo, col = repository()
    assert repo.upsert_product_detail(snapshot(SOURCE, desc="先到版本"))
    before = deepcopy(col.docs)
    assert not repo.upsert_product_detail(snapshot(SOURCE + datetime.timedelta(microseconds=offset), desc="后到同毫秒"))
    assert col.docs == before
    assert col.docs[0]["detail_updated_at"].microsecond == 123000


@pytest.mark.parametrize("timezone", [None, UTC, datetime.timezone(datetime.timedelta(hours=8))])
def test_source_is_stored_at_bson_milliseconds_in_utc_without_mutating_input(timezone):
    source = SOURCE + datetime.timedelta(microseconds=987)
    source = source.replace(tzinfo=None) if timezone is None else source.astimezone(timezone)
    payload = snapshot(source)
    before = deepcopy(payload)
    repo, col = repository()
    assert repo.upsert_product_detail(payload)
    assert col.docs[0]["detail_updated_at"] == bson_time(source)
    assert col.docs[0]["detail_updated_at"].tzinfo is None
    assert payload == before


@pytest.mark.parametrize("source", [None, True, 123, "2025-01-02", {}, [], float("nan"), float("inf")])
def test_explicit_invalid_source_is_rejected_not_replaced_by_write_time(source):
    repo, col = repository()
    assert not repo.upsert_product_detail(snapshot(source))
    assert not col.queries
    assert not col.write_calls
    assert not col.docs


def test_future_source_is_rejected_without_displacing_existing_data():
    repo, col = repository()
    assert repo.upsert_product_detail(snapshot())
    before = deepcopy(col.docs)
    writes = len(col.write_calls)
    assert not repo.upsert_product_detail(snapshot(datetime.datetime.now(UTC) + datetime.timedelta(days=1)))
    assert len(col.write_calls) == writes
    assert col.docs == before


def test_missing_source_retains_legacy_now_contract_at_millisecond_precision():
    repo, col = repository()
    payload = snapshot()
    del payload["detail_updated_at"]
    before = bson_time(datetime.datetime.now(UTC))
    assert repo.upsert_product_detail(payload)
    after = bson_time(datetime.datetime.now(UTC))
    assert before <= col.docs[0]["detail_updated_at"] <= after
    assert "detail_updated_at" not in payload


@pytest.mark.parametrize("old_source", [None, "broken", 100, True, {"$gt": "not an operator"}, []])
def test_malformed_existing_clock_is_repaired_with_original_value_cas(old_source):
    repo, col = repository()
    col.docs = [{"platform": "pdd", "product_id": "123", "detail_updated_at": deepcopy(old_source)}]
    assert repo.upsert_product_detail(snapshot())
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE)
    assert col.write_calls[0][0]["detail_updated_at"] == {"$eq": old_source, "$exists": True}
    assert_safe_writes(col)


def test_future_existing_clock_does_not_poison_all_future_refreshes():
    repo, col = repository()
    future = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
    col.docs = [{"platform": "pdd", "product_id": "123", "detail_updated_at": future}]
    assert repo.upsert_product_detail(snapshot())
    assert col.write_calls[0][0]["detail_updated_at"] == {"$eq": future, "$exists": True}
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE)


@pytest.mark.parametrize("record", [
    {"platform": "pdd", "product_id": "123"},
    {"platform": "pdd", "product_id": "123", "name": "只有摘要", "activity_price": 5},
    {"platform": "pdd", "product_id": "SIGN", "goods_sign": "SIGN"},
])
def test_identity_and_summary_only_records_are_updated_in_place(record):
    repo, col = repository()
    col.docs = [deepcopy(record)]
    assert repo.upsert_product_detail(snapshot())
    assert len(col.docs) == 1
    assert col.docs[0]["product_id"] == "123"
    assert col.docs[0]["desc"] == "来源详情"
    assert col.write_calls[0][0]["detail_updated_at"] == {"$exists": False}
    assert not any(upsert for _, _, upsert in col.write_calls)


def test_present_null_does_not_match_concurrent_field_removal():
    class RemoveNull(MemoryCollection):
        def update_one(self, query, update, upsert=False):
            if "_id" in query and "detail_updated_at" in self.docs[0]:
                with self.lock:
                    del self.docs[0]["detail_updated_at"]
            return super().update_one(query, update, upsert)

    repo, col = repository(RemoveNull())
    col.docs = [{"platform": "pdd", "product_id": "123", "detail_updated_at": None}]
    assert repo.upsert_product_detail(snapshot())
    assert [call[0]["detail_updated_at"] for call in col.write_calls] == [
        {"$eq": None, "$exists": True}, {"$exists": False},
    ]
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE)


def test_summary_updates_shared_price_but_never_renews_detail_source_clock():
    repo, col = repository()
    assert repo.upsert_product_detail(snapshot())
    assert repo.upsert_home_products([{
        "goods_id": "123", "price": 99, "name": "摘要新标题", "desc": "摘要不许覆盖",
        "detail_updated_at": datetime.datetime.now(UTC),
    }]) == 1
    assert col.docs[0]["activity_price"] == 99
    assert col.docs[0]["desc"] == "来源详情"
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE)
    assert "detail_updated_at" not in col.write_calls[-1][1]["$set"]
    assert not repo.upsert_product_detail(snapshot(SOURCE - MILLISECOND, price=1))
    assert col.docs[0]["activity_price"] == 99


def test_competing_initial_creations_are_bounded_and_newest_source_wins():
    barrier = threading.Barrier(2)

    class ConcurrentCreate(MemoryCollection):
        def __init__(self):
            super().__init__()
            self.first_reads = set()

        def find_one(self, query, projection=None):
            doc = super().find_one(query, projection)
            name = threading.current_thread().name
            if name in ("create-old", "create-new") and name not in self.first_reads:
                with self.lock:
                    self.first_reads.add(name)
                assert doc is None
                barrier.wait(5)
            return doc

    repo, col = repository(ConcurrentCreate())
    results = {}
    errors = []

    def save(name, source):
        try:
            results[name] = repo.upsert_product_detail(snapshot(source, desc=name))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=save, args=(name, source), name=name) for name, source in
               (("create-old", SOURCE), ("create-new", SOURCE + MILLISECOND))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert all(not worker.is_alive() for worker in workers)
    assert not errors
    assert results["create-new"] is True
    assert len(col.docs) == 1
    assert col.docs[0]["desc"] == "create-new"
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE + MILLISECOND)
    assert len(col.write_calls) <= 4
    assert_safe_writes(col)


@pytest.mark.parametrize("winner_offset, expected", [(-1, True), (0, False), (1, False)])
def test_unique_creation_conflict_reloads_the_actual_winner(winner_offset, expected):
    class UniqueCreationRace(MemoryCollection):
        raced = False

        def update_one(self, query, update, upsert=False):
            if upsert and not self.raced:
                self.raced = True
                winner = deepcopy(update["$setOnInsert"])
                winner.update(detail_updated_at=bson_time(SOURCE + winner_offset * MILLISECOND), desc="并发创建")
                super().update_one(query, {"$setOnInsert": winner}, upsert=True)
                # 用确实违反复合唯一索引的写操作模拟 Mongo 查询与插入间的并发冲突。
                return super().update_one({"platform": "pdd", "goods_sign": "another-sign"},
                                          {"$setOnInsert": winner}, upsert=True)
            return super().update_one(query, update, upsert)

    repo, col = repository(UniqueCreationRace())
    assert repo.upsert_product_detail(snapshot()) is expected
    assert len(col.docs) == 1
    assert col.docs[0]["desc"] == ("来源详情" if expected else "并发创建")
    assert len(col.queries) == 2


def test_repeated_unique_conflicts_during_sign_id_upgrade_are_bounded():
    repo, col = repository()
    col.docs = [
        {"platform": "pdd", "product_id": "SIGN", "goods_sign": "SIGN"},
        {"platform": "pdd", "product_id": "123", "goods_sign": "OTHER"},
    ]
    assert not repo.upsert_product_detail(snapshot())
    assert len(col.write_calls) == repo._DETAIL_CAS_ATTEMPTS
    assert len(col.queries) == repo._DETAIL_CAS_ATTEMPTS
    assert len(col.docs) == 2
    assert all("detail_updated_at" not in doc for doc in col.docs)
    assert_safe_writes(col)


def test_failed_condition_reloads_and_retries_only_if_incoming_is_still_newer():
    class IntermediateWriter(MemoryCollection):
        raced = False
        cas_matches = []

        def update_one(self, query, update, upsert=False):
            if "_id" in query and not self.raced:
                self.raced = True
                super().update_one({"_id": query["_id"]}, {"$set": {
                    "detail_updated_at": bson_time(SOURCE - MILLISECOND), "desc": "中间版本",
                }})
            result = super().update_one(query, update, upsert)
            if "detail_updated_at" in query:
                self.cas_matches.append(result.matched_count)
            return result

    repo, col = repository(IntermediateWriter())
    col.docs = [{"platform": "pdd", "product_id": "123",
                 "detail_updated_at": bson_time(SOURCE - 2 * MILLISECOND)}]
    assert repo.upsert_product_detail(snapshot())
    assert col.cas_matches == [0, 1]
    assert col.docs[0]["desc"] == "来源详情"


def test_repeated_real_condition_misses_exhaust_budget_without_unconditional_overwrite():
    class AlwaysRacing(MemoryCollection):
        def update_one(self, query, update, upsert=False):
            if "detail_updated_at" in query:
                next_time = self.docs[0]["detail_updated_at"] + MILLISECOND
                super().update_one({"_id": query["_id"]}, {"$set": {
                    "detail_updated_at": next_time, "desc": "并发更新",
                }})
            return super().update_one(query, update, upsert)

    repo, col = repository(AlwaysRacing())
    col.docs = [{"platform": "pdd", "product_id": "123",
                 "detail_updated_at": bson_time(SOURCE - datetime.timedelta(days=1))}]
    assert not repo.upsert_product_detail(snapshot())
    cas_calls = [call for call in col.write_calls if "detail_updated_at" in call[0]]
    assert len(cas_calls) == repo._DETAIL_CAS_ATTEMPTS
    assert len(col.queries) == repo._DETAIL_CAS_ATTEMPTS
    assert col.docs[0]["desc"] == "并发更新"
    assert all(not upsert for _, _, upsert in cas_calls)


@pytest.mark.parametrize("existing", [False, True])
def test_unacknowledged_write_never_reports_persistence_success(existing):
    class Unacknowledged(MemoryCollection):
        def update_one(self, query, update, upsert=False):
            super().update_one(query, update, upsert)
            return UpdateResult(None, acknowledged=False)

    repo, col = repository(Unacknowledged())
    if existing:
        col.docs = [{"platform": "pdd", "product_id": "123"}]
    assert repo.upsert_product_detail(snapshot()) is False
    assert len(col.write_calls) == 1
    # 即使数据可能写入，也没有确认成功的权利；不读取 unacknowledged 的计数属性。
    assert col.docs[0]["detail_updated_at"] == bson_time(SOURCE)


@pytest.mark.parametrize("existing", [False, True])
def test_database_write_exception_never_reports_success(existing):
    class BrokenWrite(MemoryCollection):
        def update_one(self, query, update, upsert=False):
            raise ConnectionFailure("offline injected write failure")

    repo, col = repository(BrokenWrite())
    if existing:
        col.docs = [{"platform": "pdd", "product_id": "123"}]
    assert repo.upsert_product_detail(snapshot()) is False
    assert all("detail_updated_at" not in doc for doc in col.docs)


def test_insert_is_read_back_before_success_and_cannot_hide_a_newer_winner():
    class ReplaceAfterInsert(MemoryCollection):
        def update_one(self, query, update, upsert=False):
            result = super().update_one(query, update, upsert)
            if result.upserted_id is not None:
                super().update_one({"_id": result.upserted_id}, {"$set": {
                    "detail_updated_at": bson_time(SOURCE + MILLISECOND), "desc": "更新版本",
                }})
            return result

    repo, col = repository(ReplaceAfterInsert())
    assert not repo.upsert_product_detail(snapshot())
    assert len(col.queries) == 2
    assert col.docs[0]["desc"] == "更新版本"


def test_detail_cas_preserves_whitelist_and_does_not_store_private_provider_data():
    repo, col = repository()
    assert repo.upsert_product_detail(snapshot(
        raw={"secret": "not persisted"}, promotion_url="https://example.com/private",
        _id="provider-id", _debug="private", uid="not-public", product_url="https://example.com/public",
    ))
    assert not {"raw", "promotion_url", "_debug", "uid"}.intersection(col.docs[0])
    assert col.docs[0]["_id"] != "provider-id"
    assert col.docs[0]["product_url"] == "https://example.com/public"


def test_database_failure_is_false_for_write_and_propagates_only_for_strict_read():
    failure = ConnectionFailure("offline injected Mongo failure")

    class BrokenRead(MemoryCollection):
        def find_one(self, query, projection=None):
            raise failure

    repo, col = repository(BrokenRead())
    assert repo.get_product_detail("123", key_type="id") is None
    with pytest.raises(ConnectionFailure) as raised:
        repo.get_product_detail("123", key_type="id", strict=True)
    assert raised.value is failure
    assert repo.upsert_product_detail(snapshot()) is False
    assert not col.write_calls


def test_strict_read_still_returns_none_for_a_genuinely_missing_product():
    repo, _ = repository()
    assert repo.get_product_detail("123", key_type="id", strict=True) is None
    assert repo.get_product_detail("SIGN", key_type="sign", strict=True) is None
