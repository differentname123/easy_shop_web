"""Numeric promotion identities, CAS and uniqueness: in-memory Mongo subset only."""
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pymongo.errors import DuplicateKeyError
from pymongo.results import UpdateResult

from auth import create_access_token, verify_access_token
from models.user import normalize_promoter_id
from repositories import user_repo as repository_module
from repositories.user_repo import UserRepository
from services import user_service as service_module
from services.user_service import UserService


UID_A, UID_B = "u_1234abcd", "u_5678efab"
DEFAULT_ID = "100000000"


class UserCursor:
    def __init__(self, rows, limits):
        self.rows, self.limits = rows, limits

    def limit(self, count):
        self.limits.append(count)
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class MemoryUsers:
    """Implement only exercised Mongo semantics; reject unsupported operations."""
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))
        self.lock = threading.RLock()
        self.indexes, self.queries, self.updates, self.limits = [], [], [], []
        self.unique_ready = False
        self.fail_index = False
        self.acknowledged = True
        self.read_barrier = None
        self.barrier_reads = 0
        self.before_update = None

    @staticmethod
    def field(row, path):
        current = row
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current

    @classmethod
    def matches(cls, row, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(cls.matches(row, branch) for branch in expected):
                    return False
                continue
            exists, value = cls.field(row, key)
            if isinstance(expected, dict):
                assert set(expected) == {"$exists"}, expected
                if exists != expected["$exists"]:
                    return False
            elif value != expected:
                return False
        return True

    @staticmethod
    def project(row, projection):
        if projection is None:
            return deepcopy(row)
        projected = {key: deepcopy(value) for key, value in row.items() if projection.get(key)}
        # Mongo includes _id unless explicitly excluded, even if promoter_id is missing.
        if projection.get("_id", 1):
            projected["_id"] = row.get("_id", row.get("uid", "fixture-id"))
        return projected

    def create_index(self, keys, **kwargs):
        with self.lock:
            self.indexes.append((deepcopy(keys), deepcopy(kwargs)))
            if keys == [("promoter_id", 1)]:
                if self.fail_index:
                    raise RuntimeError("unique index unavailable")
                assert kwargs == {"unique": True, "name": "promoter_id_unique",
                                  "partialFilterExpression": {"promoter_id": {"$type": "string"}}}
                ids = [row["promoter_id"] for row in self.rows if isinstance(row.get("promoter_id"), str)]
                if len(ids) != len(set(ids)):
                    raise DuplicateKeyError("historical duplicate promoter IDs")
                self.unique_ready = True
        return kwargs.get("name", "fixture-index")

    def find_one(self, query, projection=None):
        with self.lock:
            self.queries.append(deepcopy(query))
            result = next((self.project(row, projection) for row in self.rows if self.matches(row, query)), None)
            wait = self.read_barrier is not None and self.barrier_reads < 2 and projection == {"promoter_id": 1}
            if wait:
                self.barrier_reads += 1
        # Capture two concurrent pre-CAS snapshots; never wait while owning the collection lock.
        if wait:
            self.read_barrier.wait(timeout=3)
        return result

    def find(self, query, projection=None):
        with self.lock:
            self.queries.append(deepcopy(query))
            return UserCursor([self.project(row, projection) for row in self.rows if self.matches(row, query)],
                              self.limits)

    def update_one(self, query, update):
        with self.lock:
            assert self.unique_ready, "numeric identity must never be written without its unique constraint"
            assert set(update) == {"$set"}
            self.updates.append((deepcopy(query), deepcopy(update)))
            if self.before_update is not None:
                hook, self.before_update = self.before_update, None
                hook(self, query, update)
            if not self.acknowledged:
                return UpdateResult({}, acknowledged=False)
            target = next((row for row in self.rows if self.matches(row, query)), None)
            if target is None:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            proposed = dict(target, **update["$set"])
            candidate = proposed.get("promoter_id")
            if isinstance(candidate, str) and any(row is not target and row.get("promoter_id") == candidate
                                                   for row in self.rows):
                raise DuplicateKeyError("numeric promoter ID already occupied")
            modified = proposed != target
            target.update(update["$set"])
            return UpdateResult({"n": 1, "nModified": int(modified)}, acknowledged=True)


def repository(rows, **options):
    collection = MemoryUsers(rows)
    for name, value in options.items():
        setattr(collection, name, value)
    return UserRepository(SimpleNamespace(get_collection=lambda name: collection)), collection


@pytest.mark.parametrize("value,expected", [
    (1, "1"), ("1", "1"), (" 000123 ", "123"), ("00000000000000000001", "1"),
    ("99999999999999999999", "99999999999999999999"), (100000000, "100000000"),
])
def test_numeric_identity_is_canonical_ascii_decimal_string(value, expected):
    assert normalize_promoter_id(value) == expected


@pytest.mark.parametrize("value", [
    None, True, False, 0, -1, 1.0, 1.5, "", " ", "0", "000", "-1", "+1", "1.0", "1e9",
    "１２３", "١٢٣", "12 3", "u_1234abcd", "1" * 21, 10 ** 20, [], {}, b"123",
])
def test_invalid_numeric_identity_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_promoter_id(value)


def test_partial_unique_index_allows_legacy_users_without_ids():
    repo, col = repository([{"uid": UID_A}, {"uid": UID_B}, {"uid": "u_00000000", "promoter_id": None}])
    assert col.unique_ready
    assert col.indexes[0] == ([("promoter_id", 1)], {
        "unique": True, "name": "promoter_id_unique",
        "partialFilterExpression": {"promoter_id": {"$type": "string"}},
    })
    assert col.updates == []
    assert all(row.get("promoter_id") is None for row in col.rows)


@pytest.mark.parametrize("initial", [None, "", "missing"])
def test_assignment_is_lazy_persistent_and_skips_reserved_default(monkeypatch, initial):
    row = {"uid": UID_A, "auth_status": {"jd": {"is_authorized": False}}}
    if initial != "missing":
        row["promoter_id"] = initial
    repo, col = repository([row])
    candidates = Mock(side_effect=[0, 17])
    monkeypatch.setattr(repository_module.secrets, "randbelow", candidates)
    assert repo.ensure_promoter_id(UID_A, reserved_ids=(DEFAULT_ID,)) == "100000017"
    assert repo.ensure_promoter_id(UID_A, reserved_ids=(DEFAULT_ID,)) == "100000017"
    assert len(col.updates) == 1 and candidates.call_count == 2
    assert col.rows[0]["uid"] == UID_A
    assert col.rows[0]["auth_status"] == row["auth_status"]
    assert col.updates[0][0] == {"uid": UID_A, "$or": [
        {"promoter_id": {"$exists": False}}, {"promoter_id": None}, {"promoter_id": ""},
    ]}


def test_same_user_concurrent_assignment_uses_cas_and_returns_single_winner(monkeypatch):
    repo, col = repository([{"uid": UID_A}])
    col.read_barrier = threading.Barrier(2)
    candidates = Mock(side_effect=[31, 32])
    monkeypatch.setattr(repository_module.secrets, "randbelow", candidates)
    with ThreadPoolExecutor(max_workers=2) as callers:
        futures = [callers.submit(repo.ensure_promoter_id, UID_A) for _ in range(2)]
        results = [future.result(timeout=4) for future in futures]
    assert len(set(results)) == 1
    assert results[0] in {"100000031", "100000032"}
    assert col.rows == [{"uid": UID_A, "promoter_id": results[0]}]
    assert len(col.updates) == 2
    assert candidates.call_count == 2


def test_candidate_collision_retries_without_changing_other_account(monkeypatch):
    repo, col = repository([{"uid": UID_A}, {"uid": UID_B, "promoter_id": "100000001"}])
    monkeypatch.setattr(repository_module.secrets, "randbelow", Mock(side_effect=[1, 2]))
    assert repo.ensure_promoter_id(UID_A) == "100000002"
    assert col.rows[1] == {"uid": UID_B, "promoter_id": "100000001"}
    assert len(col.updates) == 2


@pytest.mark.parametrize("reason", ["reserved", "occupied"])
def test_assignment_retry_budget_is_bounded(monkeypatch, reason):
    rows = [{"uid": UID_A}]
    if reason == "occupied":
        rows.append({"uid": UID_B, "promoter_id": DEFAULT_ID})
    repo, col = repository(rows)
    candidates = Mock(return_value=0)
    monkeypatch.setattr(repository_module.secrets, "randbelow", candidates)
    with pytest.raises(RuntimeError, match="无法分配"):
        repo.ensure_promoter_id(UID_A, reserved_ids=(DEFAULT_ID,) if reason == "reserved" else ())
    assert candidates.call_count == 12
    assert "promoter_id" not in col.rows[0]
    assert len(col.updates) == (0 if reason == "reserved" else 12)


def test_unique_index_failure_is_fail_closed_and_can_retry(monkeypatch):
    repo, col = repository([{"uid": UID_A}], fail_index=True)
    monkeypatch.setattr(repository_module.secrets, "randbelow", Mock(return_value=5))
    invoke = lambda: repo.ensure_promoter_id(UID_A)
    with pytest.raises(RuntimeError, match="index unavailable"):
        invoke()
    assert not col.unique_ready and col.updates == []
    assert "promoter_id" not in col.rows[0]
    col.fail_index = False
    assert invoke() == "100000005"
    assert col.unique_ready and len(col.updates) == 1


def test_unacknowledged_identity_write_cannot_report_success(monkeypatch):
    repo, col = repository([{"uid": UID_A}], acknowledged=False)
    monkeypatch.setattr(repository_module.secrets, "randbelow", Mock(return_value=5))
    with pytest.raises(RuntimeError, match="未确认"):
        repo.ensure_promoter_id(UID_A)
    assert "promoter_id" not in col.rows[0]


@pytest.mark.parametrize("saved", [123, "000123", " 123 ", "bad", False, "0"])
def test_existing_noncanonical_id_fails_instead_of_silently_reassigning(saved):
    repo, col = repository([{"uid": UID_A, "promoter_id": saved}])
    with pytest.raises(ValueError):
        repo.ensure_promoter_id(UID_A)
    assert col.rows[0]["promoter_id"] == saved
    assert col.updates == []


@pytest.mark.parametrize("uid", [None, "", " ", "u_missing0"])
def test_missing_user_is_never_created_during_numeric_allocation(uid):
    repo, col = repository([{"uid": UID_A}])
    with pytest.raises(ValueError, match="不存在"):
        repo.ensure_promoter_id(uid)
    assert col.rows == [{"uid": UID_A}] and not col.updates


def test_numeric_reverse_lookup_does_not_require_authorization_and_is_bounded():
    auth = {"jd": {"is_authorized": False}, "taobao": {"is_authorized": False}}
    repo, col = repository([{"uid": UID_A, "promoter_id": "123", "auth_status": auth}])
    assert repo.get_uid_by_promoter_id(" 000123 ") == UID_A
    assert col.queries[-1] == {"promoter_id": "123"}
    assert col.limits[-1] == 2
    before = len(col.queries)
    assert repo.get_uid_by_promoter_id(True) is None
    assert len(col.queries) == before
    assert col.rows[0]["auth_status"] == auth
    assert repo.get_uid_by_promoter_id("456") is None
    assert repo.get_uid_by_platform_identity("taobao", {"relation_id": "123"}) is None
    assert col.queries[-1]["auth_status.taobao.is_authorized"] is True


def test_reverse_lookup_refuses_historical_ambiguous_or_missing_uid():
    repo, _ = repository([{"uid": UID_A, "promoter_id": "123"}, {"uid": UID_B, "promoter_id": "123"}])
    assert repo.get_uid_by_promoter_id("123") is None
    repo, _ = repository([{"promoter_id": "123"}])
    assert repo.get_uid_by_promoter_id("123") is None


def test_numeric_context_reserves_default_and_never_reads_platform_authorization(monkeypatch):
    auth = {"jd": {"is_authorized": False, "bind": 0}}
    repo, col = repository([{"uid": UID_A, "auth_status": auth}])
    monkeypatch.setattr(repository_module.secrets, "randbelow", Mock(side_effect=[0, 18]))
    monkeypatch.setattr(service_module, "get_adapter", Mock(side_effect=AssertionError("must not check authorization")))
    orders = Mock()
    service = UserService(repo, order_repo=orders)
    result = service.get_numeric_promotion_context(UID_A, "JD", DEFAULT_ID)
    assert result == {"status": "success", "uid": UID_A, "promoter_id": "100000018"}
    assert orders.mock_calls == []
    assert col.rows[0]["auth_status"] == auth
    assert all(set(update["$set"]) == {"promoter_id"} for _, update in col.updates)
    assert "is_authorized" not in result and "auth_url" not in result


@pytest.mark.parametrize("failure", [ValueError("missing account"), RuntimeError("index failure")])
def test_numeric_context_failure_never_falls_back_to_default_identity(failure):
    repo = SimpleNamespace(ensure_promoter_id=Mock(side_effect=failure))
    orders = Mock()
    result = UserService(repo, order_repo=orders).get_numeric_promotion_context(UID_A, "jd", DEFAULT_ID)
    assert result["status"] == "error" and result["error_code"] == "promoter_identity_unavailable"
    assert "promoter_id" not in result and "uid" not in result
    repo.ensure_promoter_id.assert_called_once_with(UID_A, reserved_ids=(DEFAULT_ID,))
    assert orders.mock_calls == []


def test_invalid_default_context_is_fail_closed_before_allocation():
    repo = SimpleNamespace(ensure_promoter_id=Mock())
    orders = Mock()
    result = UserService(repo, order_repo=orders).get_numeric_promotion_context(UID_A, "jd", "invalid")
    assert result["status"] == "error"
    repo.ensure_promoter_id.assert_not_called()
    assert orders.mock_calls == []


def test_legacy_uid_and_existing_token_remain_valid_after_numeric_id_assignment(monkeypatch):
    repo, col = repository([{"uid": UID_A, "invite_code": "INV_LEGACY", "auth_status": {}}])
    token = create_access_token(UID_A, ttl=3600)
    monkeypatch.setattr(repository_module.secrets, "randbelow", Mock(return_value=55))
    assert repo.ensure_promoter_id(UID_A, reserved_ids=(DEFAULT_ID,)) == "100000055"
    assert verify_access_token(token) == UID_A
    assert verify_access_token("Bearer " + token) == UID_A
    assert col.rows[0]["uid"] == UID_A and col.rows[0]["invite_code"] == "INV_LEGACY"
    assert re.fullmatch(r"u_[0-9a-f]{8}", UserRepository.generate_uid())
