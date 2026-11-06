# -*- coding: utf-8 -*-
"""共用订单协议：全内存检查点/身份/集合，绝不依赖真实 Mongo 或平台 HTTP。"""
from copy import deepcopy
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock, patch

import pytest

from models.order import OrderItem
from repositories.order_repo import OrderRepository
from repositories.order_sync_repo import OrderSyncRepository
from services import order_service as module


class MemorySyncRepository:
    def __init__(self, states=None):
        self.states = deepcopy(states or {})
        self.saves = []
        self.fail_save = None

    def get_state(self, platform):
        return deepcopy(self.states.get(platform))

    def save_state(self, platform, state):
        self.saves.append((platform, deepcopy(state)))
        if self.fail_save == len(self.saves):
            raise RuntimeError("checkpoint unavailable")
        self.states[platform] = deepcopy(state)


class MemoryUserRepository:
    def __init__(self):
        self.identities = {}
        self.lookups = []

    def get_uid_by_platform_identity(self, platform, identity):
        self.lookups.append((platform, deepcopy(identity)))
        return self.identities.get((platform, tuple(sorted(identity.items()))))

    def bind(self, platform, identity, uid):
        self.identities[platform, tuple(sorted(identity.items()))] = uid


class MemoryOrders:
    def __init__(self):
        self.docs = {}
        self.pages = []

    def upsert_orders(self, orders, platform):
        self.pages.append(deepcopy(orders))
        for order in orders:
            self.docs[platform, order["order_sn"]] = deepcopy(order)
        return len(orders)

    def query_user_orders(self, **kwargs):
        return {"total_count": 0, "order_list": []}


class Adapter:
    order_window_seconds = 23 * 3600
    active = True

    def __init__(self, fetch=None):
        self.fetch = fetch or (lambda **kwargs: {"status": "success", "order_list": [], "has_more": False})
        self.calls = []

    def fetch_increment_orders(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.fetch(**kwargs)

    @staticmethod
    def normalize_order(order):
        return dict(order)


def page(order_sn="a", has_more=False, cursor=None, **fields):
    return {"status": "success", "order_list": [dict(order_sn=order_sn, **fields)],
            "has_more": has_more, "next_cursor": cursor}


def pending(start=100, end=200, target=200, page_no=1, cursor=None, success=0):
    return {"last_success_end": success, "pending": {
        "start_time": start, "end_time": end, "target_end": target,
        "page": page_no, "cursor": deepcopy(cursor),
    }}


@pytest.fixture(autouse=True)
def clean_sync_state():
    for state in (module._LAST_SYNC_TIMESTAMP, module._LAST_SYNC_ATTEMPT, module._SYNC_STATE):
        state.clear()
    yield
    for state in (module._LAST_SYNC_TIMESTAMP, module._LAST_SYNC_ATTEMPT, module._SYNC_STATE):
        state.clear()


@pytest.fixture
def setup_service(monkeypatch):
    def build(adapter=None, sync=None, repo=None, users=None, platform="future_shop"):
        adapter = adapter or Adapter()
        monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
        monkeypatch.setattr(module, "require_adapter", lambda _: adapter)
        sync = sync if sync is not None else MemorySyncRepository()
        repo = repo if repo is not None else MemoryOrders()
        users = users if users is not None else MemoryUserRepository()
        return module.OrderService(repo, sync_repo=sync, user_repo=users), adapter, sync, repo, users
    return build


def run(service, platform="future_shop", now=200000):
    lock = module._platform_lock(platform)
    assert lock.acquire(blocking=False)
    service._sync_platform_orders_bg(platform, now)
    assert not lock.locked()


def test_more_than_five_pages_resume_after_restart_with_opaque_cursor(setup_service):
    def fetch(**kwargs):
        n = kwargs["page"]
        assert kwargs["cursor"] == (None if n == 1 else {"position": f"opaque-{n - 1}", "jump": [n - 1, "秘密"]})
        return page(str(n), n < 8, {"position": f"opaque-{n}", "jump": [n, "秘密"]})
    service, adapter, sync, repo, users = setup_service(Adapter(fetch))
    run(service)
    frozen = deepcopy(sync.states["future_shop"]["pending"])
    assert len(adapter.calls) == 5
    assert frozen["page"] == 6
    assert frozen["start_time"] == 200000 - 23 * 3600
    assert frozen["end_time"] == frozen["target_end"] == 200000
    assert module._SYNC_STATE["future_shop"]["status"] == "partial"
    assert sync.states["future_shop"]["last_success_end"] == 0
    # 模拟进程重启：模块内存丢失，但同一持久仓储仍有窗口/页/游标。
    module._LAST_SYNC_TIMESTAMP.clear()
    module._SYNC_STATE.clear()
    restarted = module.OrderService(repo, sync_repo=sync, user_repo=users)
    run(restarted, now=999999)
    assert [call["page"] for call in adapter.calls] == list(range(1, 9))
    assert all(call["start_time"] == frozen["start_time"] and call["end_time"] == 200000 for call in adapter.calls)
    assert len(repo.docs) == 8
    assert sync.states["future_shop"] == {"last_success_end": 200000, "pending": None}
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == 200000
    assert module._SYNC_STATE["future_shop"]["status"] == "success"


def test_twenty_minute_windows_cover_only_initial_recent_23h_and_resume(setup_service):
    adapter = Adapter()
    adapter.order_window_seconds = 20 * 60
    service, adapter, sync, _, _ = setup_service(adapter)
    target = 200000
    run(service, now=target)
    first_batch = deepcopy(adapter.calls)
    assert len(first_batch) == 5
    assert all(call["end_time"] - call["start_time"] == 1200 for call in first_batch)
    assert sync.states["future_shop"]["last_success_end"] == target - 23 * 3600 + 5 * 1200
    assert sync.states["future_shop"]["pending"]["target_end"] == target
    while sync.states["future_shop"]["pending"]:
        run(service, now=target + 10000)
    assert len(adapter.calls) == 69
    assert adapter.calls[0]["start_time"] == target - 23 * 3600
    assert adapter.calls[-1]["end_time"] == target
    assert all(left["end_time"] == right["start_time"] for left, right in zip(adapter.calls, adapter.calls[1:]))
    assert all(call["page"] == 1 and call["cursor"] is None for call in adapter.calls)
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == target


def test_pdd_legacy_short_page_without_has_more_or_cursor_still_completes(setup_service):
    adapter = Adapter(lambda **kw: {"status": "success", "order_list": [{"order_sn": str(i)} for i in range(3)]})
    service, adapter, sync, repo, _ = setup_service(adapter)
    run(service)
    assert len(adapter.calls) == 1
    assert sync.states["future_shop"]["pending"] is None
    assert len(repo.docs) == 3


def test_pdd_legacy_full_page_uses_next_page_without_cursor(setup_service):
    adapter = Adapter(lambda **kw: {"status": "success", "order_list": (
        [{"order_sn": str(i)} for i in range(50)] if kw["page"] == 1 else [])})
    service, adapter, sync, _, _ = setup_service(adapter)
    run(service)
    assert [call["page"] for call in adapter.calls] == [1, 2]
    assert all(call["cursor"] is None for call in adapter.calls)
    assert sync.states["future_shop"]["last_success_end"] == 200000


@pytest.mark.parametrize("failure", ["upstream", "write", "short_write", "normalize", "bad_list", "empty_more"])
def test_failure_does_not_move_pending_page_or_success_watermark(setup_service, failure):
    sync = MemorySyncRepository({"future_shop": pending(page_no=3, cursor={"x": "original"}, success=90)})
    adapter = Adapter(lambda **kw: page("a"))
    service, adapter, sync, repo, _ = setup_service(adapter, sync=sync)
    before = deepcopy(sync.states)
    if failure == "upstream":
        adapter.fetch = lambda **kw: {"status": "error", "error_msg": "quota"}
    elif failure == "write":
        repo.upsert_orders = MagicMock(side_effect=RuntimeError("database offline"))
    elif failure == "short_write":
        repo.upsert_orders = MagicMock(return_value=0)
    elif failure == "normalize":
        adapter.normalize_order = MagicMock(side_effect=ValueError("bad order"))
    elif failure == "bad_list":
        adapter.fetch = lambda **kw: {"status": "success", "order_list": {}}
    else:
        adapter.fetch = lambda **kw: {"status": "success", "order_list": [], "has_more": True}
    run(service)
    assert sync.states == before
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == 90
    assert module._SYNC_STATE["future_shop"]["status"] == "error"
    assert adapter.calls[0]["page"] == 3
    assert adapter.calls[0]["cursor"] == {"x": "original"}


@pytest.mark.parametrize("has_more", [True, False])
def test_checkpoint_failure_after_full_write_replays_page_safely(setup_service, has_more):
    sync = MemorySyncRepository({"future_shop": pending(success=90)})
    sync.fail_save = 1
    adapter = Adapter(lambda **kw: page(str(kw["page"]), has_more and kw["page"] == 1, {"next": "opaque"}))
    service, adapter, sync, repo, _ = setup_service(adapter, sync=sync)
    before = deepcopy(sync.states)
    run(service)
    assert sync.states == before
    assert len(repo.pages) == 1
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == 90
    assert module._SYNC_STATE["future_shop"]["status"] == "partial"
    sync.fail_save = None
    run(service, now=800000)
    assert [call["page"] for call in adapter.calls][:2] == [1, 1]
    assert len(repo.docs) == (2 if has_more else 1)
    assert sync.states["future_shop"] == {"last_success_end": 200, "pending": None}


def test_initial_checkpoint_failure_never_fetches_platform(setup_service):
    sync = MemorySyncRepository()
    sync.fail_save = 1
    service, adapter, sync, repo, _ = setup_service(sync=sync)
    run(service)
    assert not adapter.calls and not repo.pages and not sync.states
    assert "future_shop" not in module._LAST_SYNC_TIMESTAMP


def test_next_page_is_saved_only_after_complete_page_write(setup_service):
    sync = MemorySyncRepository({"future_shop": pending()})
    adapter = Adapter(lambda **kw: page(str(kw["page"]), kw["page"] < 2, {"opaque": kw["page"]}))
    service, _, sync, repo, _ = setup_service(adapter, sync=sync)
    original = repo.upsert_orders

    def write(orders, platform):
        current = sync.states[platform]["pending"]
        assert current["page"] == int(orders[0]["order_sn"])
        assert len(sync.saves) == current["page"] - 1
        return original(orders, platform)
    repo.upsert_orders = write
    run(service)
    assert sync.saves[0][1]["pending"]["page"] == 2
    assert sync.saves[0][1]["pending"]["cursor"] == {"opaque": 1}


def test_cursor_resets_when_completed_window_advances(setup_service):
    sync = MemorySyncRepository({"future_shop": pending(start=0, end=1200, target=2400)})
    adapter = Adapter(lambda **kw: page(str((kw["start_time"], kw["page"])),
                                       kw["start_time"] == 0 and kw["page"] == 1,
                                       {"position_index": "keep:opaque", "page_no": 2}))
    adapter.order_window_seconds = 1200
    service, adapter, sync, _, _ = setup_service(adapter, sync=sync)
    run(service)
    assert [(call["start_time"], call["end_time"], call["page"], call["cursor"])
            for call in adapter.calls] == [
        (0, 1200, 1, None),
        (0, 1200, 2, {"position_index": "keep:opaque", "page_no": 2}),
        (1200, 2400, 1, None),
    ]
    assert sync.states["future_shop"] == {"last_success_end": 2400, "pending": None}


def test_failed_empty_window_checkpoint_does_not_advance_watermark(setup_service):
    sync = MemorySyncRepository({"future_shop": pending(success=90)})
    sync.fail_save = 1
    service, _, sync, _, _ = setup_service(sync=sync)
    run(service)
    assert sync.states["future_shop"] == pending(success=90)
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == 90
    assert module._SYNC_STATE["future_shop"]["status"] == "error"


def test_failure_in_next_window_keeps_only_completed_window_watermark(setup_service):
    sync = MemorySyncRepository({"future_shop": pending(start=0, end=1200, target=2400)})
    adapter = Adapter(lambda **kw: ({"status": "success", "order_list": []}
                                   if kw["start_time"] == 0 else {"status": "error", "error_msg": "quota"}))
    adapter.order_window_seconds = 1200
    service, _, sync, _, _ = setup_service(adapter, sync=sync)
    run(service)
    assert sync.states["future_shop"] == pending(start=1200, end=2400, target=2400, success=1200)
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == 1200
    assert module._SYNC_STATE["future_shop"]["status"] == "partial"
    assert module._SYNC_STATE["future_shop"]["synced_count"] == 0


def test_new_attempt_overlaps_last_completed_watermark(setup_service):
    service, adapter, sync, _, _ = setup_service()
    run(service)
    run(service, now=200200)
    assert adapter.calls[-1]["start_time"] == 200000 - 300
    assert adapter.calls[-1]["end_time"] == 200200
    assert sync.states["future_shop"]["last_success_end"] == 200200


def test_two_platforms_do_not_block_each_other_but_same_platform_is_single_flight(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished_other = threading.Event()
    created = []

    def slow(**kw):
        started.set()
        assert release.wait(3)
        return {"status": "success", "order_list": []}

    def fast(**kw):
        finished_other.set()
        return {"status": "success", "order_list": []}

    adapters = {"slow_shop": Adapter(slow), "fast_shop": Adapter(fast)}
    monkeypatch.setattr(module, "get_adapter", adapters.__getitem__)
    service = module.OrderService(MemoryOrders(), MemorySyncRepository(), MemoryUserRepository())
    original_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        created.append(thread)
        return thread
    monkeypatch.setattr(module.threading, "Thread", thread_factory)
    try:
        service._try_start_sync("slow_shop", 200000)
        assert started.wait(2)
        service._try_start_sync("slow_shop", 200181)
        service._try_start_sync("fast_shop", 200000)
        assert finished_other.wait(2)
        assert len(created) == 2
        assert len(adapters["slow_shop"].calls) == 1
        assert not release.is_set()
    finally:
        release.set()
        for thread in created:
            thread.join(3)
    assert all(not thread.is_alive() for thread in created)
    assert module._SYNC_STATE["slow_shop"]["status"] == "success"
    assert module._SYNC_STATE["fast_shop"]["status"] == "success"


def test_force_request_cannot_bypass_180_second_cooldown(setup_service):
    service, _, _, _, _ = setup_service()
    module._LAST_SYNC_ATTEMPT["future_shop"] = 1000
    with patch.object(module.time, "time", return_value=1179), patch.object(module.threading, "Thread") as thread:
        result = service.sync_and_query_orders("future_shop", "request-user", force_sync=True)
    assert result["status"] == "success"
    assert module.ORDER_SYNC_INTERVAL == 180
    thread.assert_not_called()


def test_unknown_attribution_saved_and_verified_match_ignores_request_uid(setup_service):
    adapter = Adapter(lambda **kw: {"status": "success", "has_more": False, "order_list": [
        {"order_sn": "unknown", "attribution_identity": {"relation_id": "missing"}, "promoter_uid": "untrusted"},
        {"order_sn": "known", "attribution_identity": {"relation_id": "verified"}},
        {"order_sn": "no-evidence"},
        {"order_sn": "legacy", "promoter_uid": "legacy-source-user"},
    ]})
    service, _, _, repo, users = setup_service(adapter)
    users.bind("future_shop", {"relation_id": "verified"}, "actual-owner")
    # 只让本次后台任务内联执行，查询 UID 与平台订单归因没有关系。
    with patch.object(module.threading, "Thread") as thread:
        thread.side_effect = lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args))
        result = service.sync_and_query_orders("future_shop", "request-user")
    assert result["status"] == "success"
    unknown = repo.docs["future_shop", "unknown"]
    assert unknown["attribution_status"] == "unmatched" and not unknown["promoter_uid"]
    assert unknown["attribution_identity"] == {"relation_id": "missing"}
    assert repo.docs["future_shop", "known"]["promoter_uid"] == "actual-owner"
    assert repo.docs["future_shop", "known"]["attribution_status"] == "matched"
    assert not repo.docs["future_shop", "no-evidence"]["promoter_uid"]
    assert repo.docs["future_shop", "legacy"]["promoter_uid"] == "legacy-source-user"
    assert len(users.lookups) == 2


# 只解释本仓储使用的 Mongo 表达式，不通过 mongomock 或真实数据库执行。
_MISSING = object()


def value_at(doc, field, default=None):
    current = doc
    for key in field.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def evaluate(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        return value_at(doc, expr[1:])
    if isinstance(expr, list):
        return [evaluate(item, doc) for item in expr]
    if not isinstance(expr, dict):
        return expr
    operator, arg = next(iter(expr.items()))
    if operator == "$literal":
        return deepcopy(arg)
    if operator == "$cond":
        condition, yes, no = arg
        return evaluate(yes if evaluate(condition, doc) else no, doc)
    if operator == "$type":
        item = value_at(doc, arg[1:], _MISSING)
        return "missing" if item is _MISSING else "other"
    if operator == "$convert":
        item = evaluate(arg["input"], doc)
        if item is None:
            return arg["onNull"]
        try:
            return float(item)
        except (TypeError, ValueError):
            return arg["onError"]
    if operator == "$ifNull":
        return next((item for item in evaluate(arg, doc) if item is not None), None)
    args = evaluate(arg, doc)
    if operator == "$eq": return args[0] == args[1]
    if operator == "$ne": return args[0] != args[1]
    if operator == "$lte": return args[0] <= args[1]
    if operator == "$in": return args[0] in args[1]
    if operator == "$and": return all(args)
    if operator == "$not": return not args[0]
    raise AssertionError(f"unsupported expression {expr}")


def matches(doc, query):
    for key, condition in query.items():
        if key == "$or":
            if not any(matches(doc, branch) for branch in condition): return False
            continue
        value = value_at(doc, key)
        if isinstance(condition, dict):
            for operator, rhs in condition.items():
                if operator == "$in" and value not in rhs: return False
                if operator == "$gte" and (value is None or value < rhs): return False
                if operator == "$gt" and (value is None or value <= rhs): return False
                if operator == "$exists" and (value_at(doc, key, _MISSING) is not _MISSING) != rhs: return False
                assert operator in ("$in", "$gte", "$gt", "$exists"), f"unsupported match {condition}"
        elif value != condition:
            return False
    return True


class MemoryCollection:
    def __init__(self, docs=()):
        self.docs = deepcopy(list(docs))
        self.indexes = []
        self.pipeline = None

    def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return "offline"

    def find_one(self, query):
        return deepcopy(next((doc for doc in self.docs if matches(doc, query)), None))

    def bulk_write(self, operations, ordered=False):
        assert ordered is False
        for operation in operations:
            doc = next((doc for doc in self.docs if matches(doc, operation._filter)), None)
            if doc is None:
                assert operation._upsert
                doc = deepcopy(operation._filter)
                self.docs.append(doc)
            assert isinstance(operation._doc, list), "生产更新必须原子比较来源时间/归属"
            for stage in operation._doc:
                before = deepcopy(doc)
                doc.update({key: evaluate(expr, before) for key, expr in stage["$set"].items()})
        return SimpleNamespace(acknowledged=True)

    def update_many(self, query, update):
        count = 0
        for doc in self.docs:
            if matches(doc, query):
                doc.update(deepcopy(update["$set"]))
                count += 1
        return SimpleNamespace(acknowledged=True, modified_count=count)

    def update_one(self, query, update, upsert=False):
        doc = next((doc for doc in self.docs if matches(doc, query)), None)
        if doc is None:
            assert upsert
            doc = deepcopy(query)
            self.docs.append(doc)
        doc.update(deepcopy(update["$set"]))
        return SimpleNamespace(acknowledged=True)

    def aggregate(self, pipeline):
        self.pipeline = deepcopy(pipeline)
        docs = [doc for doc in self.docs if matches(doc, pipeline[0]["$match"])]
        result = {}
        for facet, stages in pipeline[1]["$facet"].items():
            rows = deepcopy(docs)
            for stage in stages:
                operator, arg = next(iter(stage.items()))
                if operator == "$project":
                    rows = ([{key: value for key, value in row.items() if key not in arg} for row in rows]
                            if all(value == 0 for value in arg.values()) else
                            [{key: evaluate(expr, row) for key, expr in arg.items()} for row in rows])
                elif operator == "$set":
                    rows = [dict(row, **{key: evaluate(expr, row) for key, expr in arg.items()}) for row in rows]
                elif operator == "$group":
                    rows = [{key: sum(evaluate(expr["$sum"], row) for row in rows)
                             for key, expr in arg.items() if key != "_id"}] if rows else []
                elif operator == "$sort":
                    for key, direction in reversed(list(arg.items())):
                        rows.sort(key=lambda doc: (doc.get(key) is not None, doc.get(key) or 0), reverse=direction < 0)
                elif operator == "$limit":
                    rows = rows[:arg]
                else:
                    raise AssertionError(f"unsupported stage {stage}")
            result[facet] = rows
        return [result]


def repository(collection, cls=OrderRepository):
    db = SimpleNamespace(get_collection=lambda name: collection)
    return cls(db_instance=db)


def test_sync_repository_persists_only_platform_checkpoint_and_defensive_copies():
    col = MemoryCollection()
    repo = repository(col, OrderSyncRepository)
    assert repo.get_state("future_shop") is None
    state = pending(cursor={"position": "opaque", "jump": [1, 2]}, success=80)
    repo.save_state("future_shop", state)
    state["pending"]["cursor"]["jump"].append(99)
    loaded = repo.get_state("future_shop")
    assert loaded["pending"]["cursor"]["jump"] == [1, 2]
    loaded["pending"]["page"] = 88
    assert repo.get_state("future_shop")["pending"]["page"] == 1
    assert col.docs[0]["_id"] == "future_shop"
    assert set(col.docs[0]) == {"_id", "pending", "last_success_end"}
    assert repo.get_state("other_shop") is None
    col.update_one = MagicMock(return_value=SimpleNamespace(acknowledged=False))
    with pytest.raises(RuntimeError):
        repo.save_state("future_shop", state)


def test_unknown_order_binding_backfills_only_matching_unowned_orders(setup_service):
    identity = {"relation_id": "known-later"}
    col = MemoryCollection()
    repo = repository(col)
    adapter = Adapter(lambda **kw: page("unknown", attribution_identity=identity, order_modify_at=100))
    service, _, sync, _, users = setup_service(adapter, repo=repo)
    run(service)
    assert col.docs[0]["attribution_status"] == "unmatched"
    assert not col.docs[0]["promoter_uid"]
    repo.upsert_orders([
        {"order_sn": "already-owned", "attribution_identity": identity, "promoter_uid": "other-owner", "attribution_status": "matched"},
        {"order_sn": "different-identity", "attribution_identity": {"relation_id": "someone-else"}},
    ], "future_shop")
    repo.upsert_orders([{"order_sn": "other-platform", "attribution_identity": identity}], "other_shop")
    users.bind("future_shop", identity, "new-owner")
    uid = users.get_uid_by_platform_identity("future_shop", identity)
    assert repo.assign_unmatched_orders("future_shop", identity, uid) == 1
    assert repo.assign_unmatched_orders("future_shop", identity, uid) == 0
    by_sn = {doc["order_sn"]: doc for doc in col.docs}
    assert by_sn["unknown"]["promoter_uid"] == "new-owner"
    assert by_sn["unknown"]["attribution_status"] == "matched"
    assert by_sn["already-owned"]["promoter_uid"] == "other-owner"
    assert not by_sn["different-identity"].get("promoter_uid")
    assert not by_sn["other-platform"].get("promoter_uid")
    # 较新的平台页缺归因字段，也不抹掉已回填归属及原来的证据。
    repo.upsert_orders([{"order_sn": "unknown", "order_modify_at": 110, "promoter_uid": "", "attribution_status": "unmatched"}], "future_shop")
    assert by_sn["unknown"]["promoter_uid"] == "new-owner"
    assert by_sn["unknown"]["attribution_identity"] == identity


@pytest.mark.parametrize("old_time", [100, None])
@pytest.mark.parametrize("new_state", ["refund", "settled"])
def test_old_or_undated_response_cannot_erase_newer_status_or_trusted_attribution(old_time, new_state):
    col = MemoryCollection()
    repo = repository(col)
    latest = {"order_sn": "child", "order_modify_at": 200, "order_status": new_state,
              "is_refund": new_state == "refund", "is_settled": new_state == "settled",
              "promotion_amount_yuan": 4.2, "settled_commission_yuan": 4.2,
              "commission_basis": "settlement_estimate", "promoter_uid": "owner",
              "attribution_identity": {"relation_id": "original"}, "attribution_status": "matched"}
    repo.upsert_orders([latest], "future_shop")
    stale = {"order_sn": "child", "order_modify_at": old_time, "order_status": "paid",
             "is_refund": False, "is_settled": False, "promotion_amount_yuan": 9,
             "promoter_uid": "attacker", "attribution_identity": {"relation_id": "new"},
             "attribution_status": "matched"}
    assert repo.upsert_orders([stale], "future_shop") == 1
    doc = col.docs[0]
    for field, value in latest.items():
        assert doc[field] == value
    assert len(col.docs) == 1


def test_newer_status_can_change_but_existing_owner_is_never_reassigned():
    col = MemoryCollection()
    repo = repository(col)
    repo.upsert_orders([{"order_sn": "child", "order_modify_at": 100, "promoter_uid": "owner",
                         "attribution_identity": {"relation_id": "original"}}], "future_shop")
    repo.upsert_orders([{"order_sn": "child", "order_modify_at": 200, "is_refund": True,
                         "promoter_uid": "new-person", "attribution_status": "matched",
                         "attribution_identity": {"relation_id": "replacement"}}], "future_shop")
    assert col.docs[0]["is_refund"] is True
    assert col.docs[0]["promoter_uid"] == "owner"
    assert col.docs[0]["attribution_identity"] == {"relation_id": "original"}
    assert col.docs[0]["attribution_status"] == "matched"


def test_overlap_replays_order_idempotently_and_applies_delayed_refund(setup_service):
    col = MemoryCollection()
    repo = repository(col)
    responses = iter([
        page("same-child", promoter_uid="owner", order_modify_at=199800,
             is_refund=False, promotion_amount_yuan=10),
        page("same-child", promoter_uid="owner", order_modify_at=199950,
             is_refund=True, promotion_amount_yuan=10),
    ])
    adapter = Adapter(lambda **kw: next(responses))
    service, adapter, sync, _, _ = setup_service(adapter, repo=repo)
    run(service, now=200000)
    module._LAST_SYNC_TIMESTAMP.clear()  # 重启后以持久成功终点而非内存作为重叠起点。
    run(module.OrderService(repo, sync_repo=sync, user_repo=MemoryUserRepository()), now=200200)
    assert adapter.calls[1]["start_time"] == 199700
    assert len(col.docs) == 1
    result = repo.query_user_orders("future_shop", "owner")
    assert result["total_count"] == result["refund_count"] == 1
    assert result["total_commission"] == 0 and result["refund_commission"] == 10
    assert module._LAST_SYNC_TIMESTAMP["future_shop"] == 200200


def test_child_order_idempotence_is_scoped_by_platform_and_not_parent():
    col = MemoryCollection()
    repo = repository(col)
    children = [{"order_sn": "child-a", "trade_parent_id": "parent", "order_modify_at": 1},
                {"order_sn": "child-b", "trade_parent_id": "parent", "order_modify_at": 1}]
    assert repo.upsert_orders(children, "future_shop") == 2
    assert repo.upsert_orders(children, "future_shop") == 2
    assert repo.upsert_orders(children[:1], "other_shop") == 1
    assert len(col.docs) == 3
    assert ([('platform', 1), ('order_sn', 1)], {"unique": True}) in col.indexes


def test_invalid_is_not_refund_and_commission_bases_remain_explicit():
    def order(sn, amount, **fields):
        return dict(platform="future_shop", promoter_uid="owner", order_sn=sn,
                    order_pay_time=100, goods_id="sku", promotion_amount_yuan=amount, **fields)
    docs = [
        order("invalid", 100, is_invalid=True, is_refund=False, is_settled=True),
        order("refund", 7, is_invalid=True, is_refund=True, is_settled=False),
        order("payment", 10, is_settled=False, estimated_commission_yuan=10,
              settled_commission_yuan=None, commission_basis="payment_estimate"),
        order("settled", 8, is_settled=True, estimated_commission_yuan=10,
              settled_commission_yuan=8, commission_basis="settlement_estimate"),
        order("missing-settlement", 0, is_settled=True, estimated_commission_yuan=15,
              settled_commission_yuan=None, commission_basis="settlement_unavailable"),
        order("legacy", 2, is_settled=True),  # 旧文档缺 is_invalid，默认有效。
    ]
    result = repository(MemoryCollection(docs)).query_user_orders("future_shop", "owner")
    assert result["total_count"] == 6
    assert result["total_commission"] == 20
    assert result["settled_commission"] == 10
    assert result["refund_count"] == 1 and result["refund_commission"] == 7
    by_sn = {doc["order_sn"]: doc for doc in result["order_list"]}
    assert by_sn["invalid"]["is_invalid"] and not by_sn["invalid"]["is_refund"]
    assert not by_sn["legacy"]["is_invalid"]
    assert by_sn["missing-settlement"]["promotion_amount_yuan"] == 0
    assert by_sn["missing-settlement"]["estimated_commission_yuan"] == 15
    assert by_sn["missing-settlement"]["settled_commission_yuan"] is None
    for doc in result["order_list"]:
        assert doc["platform"] == "future_shop"
        dto = OrderItem(**doc)
        assert dto.is_invalid == doc["is_invalid"]
        assert dto.commission_basis == doc["commission_basis"]


def test_aggregation_keeps_full_stats_and_maximum_100_details():
    docs = [dict(platform="future_shop", promoter_uid="owner", order_sn=f"{i:03}",
                 order_pay_time=100 + i, promotion_amount_yuan=2, is_settled=True)
            for i in range(135)]
    col = MemoryCollection(docs)
    result = repository(col).query_user_orders("future_shop", "owner", limit=9999)
    assert result["total_count"] == 135
    assert result["total_commission"] == result["settled_commission"] == 270
    assert len(result["order_list"]) == 100
    assert result["order_list"][0]["order_sn"] == "134"
    assert not any("$limit" in stage for stage in col.pipeline[1]["$facet"]["stats"])
