"""Index initialization concurrency without MongoDB, sleeps, or identity caches."""
import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from repositories import base_repo as module
from repositories.base_repo import BaseRepository


class MemoryDatabase:
    def __init__(self):
        self.collections = {}

    def get_collection(self, name):
        return self.collections.setdefault(name, object())


class SlottedDatabase:
    """Injected doubles need not allow attributes or weak references."""
    __slots__ = ()

    def get_collection(self, name):
        return name


class ProbeRepository(BaseRepository):
    def __init__(self, db=None, name="products", build=None):
        self.COLLECTION_NAME = name
        self.build = build or (lambda repo: True)
        self.attempts = 0
        super().__init__(db)

    def _init_indexes(self):
        self.attempts += 1
        # Real repository index builders use self.col too.
        assert self.col is not None
        return self.build(self)


def test_blocked_ddl_does_not_block_other_collections_databases_or_ready_reads():
    db, other_db = MemoryDatabase(), MemoryDatabase()
    ready = ProbeRepository(db, "ready")
    entered, release = threading.Event(), threading.Event()

    def blocked(repo):
        entered.set()
        assert release.wait(5)
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        slow = pool.submit(ProbeRepository, db, "slow", blocked)
        try:
            assert entered.wait(2)
            assert pool.submit(ProbeRepository, db, "unrelated").result(timeout=2).attempts == 1
            assert pool.submit(ProbeRepository, other_db, "slow").result(timeout=2).attempts == 1
            assert pool.submit(lambda: ready.col).result(timeout=2) is db.get_collection("ready")
            # A new repository for an already ready collection also avoids slow DDL.
            assert pool.submit(ProbeRepository, db, "ready").result(timeout=2).attempts == 0
        finally:
            release.set()
        assert slow.result(timeout=2).attempts == 1


def test_same_database_collection_initializes_once_and_waiters_recheck():
    db = MemoryDatabase()
    entered, release, waiter_at_lock = threading.Event(), threading.Event(), threading.Event()
    first = ProbeRepository()
    first._db = db
    state = first._get_collection_index_state()
    original_lock = state.lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "index-waiter":
                waiter_at_lock.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    state.lock = ObservedLock()

    def blocked(repo):
        entered.set()
        assert release.wait(5)
        return True

    first.build = blocked
    waiter_result = []
    waiter_error = []

    def wait_for_same_collection():
        try:
            waiter_result.append(ProbeRepository(db))
        except BaseException as exc:
            waiter_error.append(exc)

    waiter = threading.Thread(target=wait_for_same_collection, name="index-waiter")
    with ThreadPoolExecutor(max_workers=1) as pool:
        building = pool.submit(lambda: first.col)
        try:
            assert entered.wait(2)
            waiter.start()
            assert waiter_at_lock.wait(2)
            assert not waiter_result
        finally:
            release.set()
            waiter.join(timeout=2)
        assert building.result(timeout=2) is db.get_collection("products")
    assert not waiter.is_alive() and not waiter_error
    assert first.attempts == 1 and waiter_result[0].attempts == 0
    assert waiter_result[0]._get_collection_index_state() is state


@pytest.mark.parametrize("failure", [False, RuntimeError("temporary DDL failure")])
def test_failed_indexes_retry_and_recursive_col_access_does_not_deadlock(failure):
    def build(repo):
        if repo.attempts == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return True

    repo = ProbeRepository(MemoryDatabase(), build=build)
    assert repo.attempts == 1
    assert not repo._get_collection_index_state().ready
    assert repo.col is not None
    assert repo.col is not None
    assert repo.attempts == 2
    assert not repo._index_state.building


def test_ready_fast_path_never_takes_metadata_or_collection_lock():
    repo = ProbeRepository(MemoryDatabase())

    class UnexpectedLock:
        def __enter__(self):
            pytest.fail("ready repository should not acquire initialization locks")

        def __exit__(self, *args):
            return False

    repo._initialization_lock = UnexpectedLock()
    repo._get_collection_index_state().lock = UnexpectedLock()
    assert repo.col is not None
    assert repo.attempts == 1


def test_lazy_connections_are_per_instance_and_created_once(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    first = ProbeRepository()
    other = ProbeRepository()
    db, other_db = MemoryDatabase(), MemoryDatabase()
    calls = []

    def connect():
        calls.append(threading.current_thread().name)
        if threading.current_thread().name == "slow-connection":
            entered.set()
            assert release.wait(5)
            return db
        return other_db

    monkeypatch.setattr(module, "gen_db_object", connect)
    assert calls == []
    errors = []

    def open_first():
        try:
            assert first.col is db.get_collection("products")
        except BaseException as exc:
            errors.append(exc)

    slow = threading.Thread(target=open_first, name="slow-connection")
    slow.start()
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            assert entered.wait(2)
            assert pool.submit(lambda: other.col).result(timeout=2) is other_db.get_collection("products")
            waiting = pool.submit(lambda: first.col)
        finally:
            release.set()
            slow.join(timeout=2)
        assert waiting.result(timeout=2) is db.get_collection("products")
    assert not slow.is_alive() and not errors
    assert len(calls) == 2 and first.attempts == other.attempts == 1


def test_connection_failure_remains_retryable(monkeypatch):
    db = MemoryDatabase()
    attempts = []

    def connect():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("connection unavailable")
        return db

    monkeypatch.setattr(module, "gen_db_object", connect)
    repo = ProbeRepository()
    with pytest.raises(RuntimeError, match="connection unavailable"):
        _ = repo.col
    assert repo._db is None
    assert repo.col is db.get_collection("products")
    assert len(attempts) == 2


def test_mock_state_uses_database_identity_and_is_not_globally_retained():
    db, other_db = MagicMock(), MagicMock()
    first, second = ProbeRepository(db), ProbeRepository(db)
    distinct = ProbeRepository(other_db)
    assert (first.attempts, second.attempts, distinct.attempts) == (1, 0, 1)
    assert first._get_collection_index_state() is second._get_collection_index_state()
    assert first._get_collection_index_state() is not distinct._get_collection_index_state()
    reference = weakref.ref(db)
    del db, first, second
    gc.collect()
    assert reference() is None


def test_attribute_restricted_double_uses_safe_instance_fallback():
    db = SlottedDatabase()
    first, other = ProbeRepository(db), ProbeRepository(db)
    assert first.col == other.col == "products"
    assert first.attempts == other.attempts == 1
    # No global id-based memo can leak one injected double's result to another.
    distinct = ProbeRepository(SlottedDatabase())
    assert distinct.attempts == 1
    assert first._get_collection_index_state() is not other._get_collection_index_state()


def test_replacing_injected_database_does_not_reuse_old_success():
    repo = ProbeRepository(SimpleNamespace(get_collection=lambda name: name))
    repo._db = SimpleNamespace(get_collection=lambda name: name)
    assert repo.col == "products"
    assert repo.attempts == 2
