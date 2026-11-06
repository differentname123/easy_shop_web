# -*- coding: utf-8 -*-
"""数字推广归因及中性订单口径；只使用内存仓储，不连接 Mongo/联盟。"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models.order import OrderItem, normalize_order_fields
from services import order_service as module
from tests.test_order_sync_protocol import (
    Adapter, MemoryCollection, MemoryOrders, MemorySyncRepository, MemoryUserRepository,
    matches, pending, repository, run,
)


class PromoterUsers(MemoryUserRepository):
    def __init__(self):
        super().__init__()
        self.promoters = {}
        self.promoter_lookups = []
        self.auth_status = {"jd": {"is_authorized": False}}

    def get_uid_by_promoter_id(self, promoter_id):
        self.promoter_lookups.append(promoter_id)
        return self.promoters.get(promoter_id)


@pytest.fixture(autouse=True)
def isolated_sync_state():
    for state in (module._LAST_SYNC_TIMESTAMP, module._LAST_SYNC_ATTEMPT, module._SYNC_STATE):
        state.clear()
    yield
    for state in (module._LAST_SYNC_TIMESTAMP, module._LAST_SYNC_ATTEMPT, module._SYNC_STATE):
        state.clear()


def service_for(monkeypatch, adapter=None, repo=None, sync=None, users=None):
    adapter = adapter or Adapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    return module.OrderService(
        repo if repo is not None else MemoryOrders(),
        sync if sync is not None else MemorySyncRepository(),
        users if users is not None else PromoterUsers(),
    )


@pytest.mark.parametrize("platform", ["pdd", "taobao"])
@pytest.mark.parametrize("paid_time", [0, 1234, None])
def test_legacy_platforms_gain_neutral_fields_without_losing_zero_or_status(platform, paid_time):
    raw = {"order_sn": "legacy", "order_pay_time": paid_time, "order_amount_yuan": 0,
           "promotion_amount_yuan": 0, "estimated_commission_yuan": 0,
           "settled_commission_yuan": 0, "is_refund": False, "is_settled": False}
    before = deepcopy(raw)
    doc = normalize_order_fields(raw, platform)
    assert raw == before
    assert doc["order_time"] == paid_time
    assert doc["order_time_basis"] == ("paid" if paid_time is not None else "unknown")
    assert doc["order_amount_basis"] == "paid"
    for key in before:
        assert doc[key] == before[key]
    col = MemoryCollection()
    repository(col).upsert_orders([raw], platform)
    assert col.docs[0]["order_time"] == paid_time
    assert col.docs[0]["order_amount_basis"] == "paid"


def test_jd_created_time_and_zero_amounts_are_not_relabelled_as_paid():
    col = MemoryCollection()
    repo = repository(col)
    doc = {"order_sn": "line", "parent_order_sn": "parent", "goods_id": "sku",
           "promoter_uid": "owner", "order_modify_at": 20,
           "order_time": 1000, "order_time_basis": "created", "order_pay_time": None,
           "order_amount_yuan": 0, "order_amount_basis": "commissionable",
           "estimated_commission_yuan": 0, "settled_commission_yuan": 0,
           "promotion_amount_yuan": 0, "commission_basis": "actual",
           "order_status": 27, "is_refund": False, "is_settled": False, "is_invalid": False}
    repo.upsert_orders([doc], "jd")
    result = repo.query_user_orders("jd", "owner", start_timestamp=900)
    assert result["total_count"] == 1
    assert result["total_commission"] == result["settled_commission"] == result["refund_commission"] == 0
    row = result["order_list"][0]
    assert row["order_time"] == 1000 and row["order_time_str"]
    assert row["order_time_basis"] == "created" and row["order_amount_basis"] == "commissionable"
    assert row["order_pay_time"] is None and row["order_pay_time_str"] == ""
    assert row["parent_order_sn"] == "parent"
    assert not row["is_refund"] and not row["is_settled"] and not row["is_invalid"]
    for key in ("order_amount_yuan", "promotion_amount_yuan", "estimated_commission_yuan", "settled_commission_yuan"):
        assert row[key] == 0
    dto = OrderItem(**row)
    assert dto.order_time == 1000 and dto.order_pay_time is None
    assert dto.order_amount_basis == "commissionable"


def test_page_local_identity_cache_includes_misses_but_refreshes_next_page(monkeypatch):
    users = PromoterUsers()
    users.promoters["123"] = "numeric-owner"
    users.bind("future_shop", {"relation_id": "verified"}, "channel-owner")

    def fetch(**kwargs):
        page = kwargs["page"]
        if page == 2:
            users.promoters["999"] = "bound-between-pages"
        rows = []
        for number, promoter_id in enumerate(("00123", 123, " 123 ", "999", "999")):
            rows.append({"order_sn": f"{page}-pid-{number}", "promoter_id": promoter_id,
                         "promoter_uid": "never-trust-this-uid"})
        for number, relation in enumerate(("verified", "verified", "missing", "missing")):
            rows.append({"order_sn": f"{page}-channel-{number}",
                         "attribution_identity": {"relation_id": relation},
                         "promoter_uid": "never-trust-this-uid"})
        rows.append({"order_sn": f"{page}-legacy", "promoter_uid": "official-pdd-uid"})
        return {"status": "success", "order_list": rows, "has_more": page == 1}

    orders = MemoryOrders()
    service = service_for(monkeypatch, Adapter(fetch), repo=orders, users=users)
    run(service)
    assert users.promoter_lookups == ["123", "999", "123", "999"]
    assert len(users.lookups) == 4  # 两个渠道身份，每页各一次（包括未匹配）。
    assert users.auth_status["jd"]["is_authorized"] is False
    assert orders.docs["future_shop", "1-pid-0"]["promoter_id"] == "123"
    assert orders.docs["future_shop", "1-pid-1"]["promoter_uid"] == "numeric-owner"
    assert orders.docs["future_shop", "1-pid-3"]["promoter_uid"] == ""
    assert orders.docs["future_shop", "1-pid-3"]["attribution_status"] == "unmatched"
    assert orders.docs["future_shop", "2-pid-3"]["promoter_uid"] == "bound-between-pages"
    assert orders.docs["future_shop", "1-channel-0"]["promoter_uid"] == "channel-owner"
    assert orders.docs["future_shop", "1-channel-2"]["promoter_uid"] == ""
    assert orders.docs["future_shop", "1-legacy"]["promoter_uid"] == "official-pdd-uid"


def test_numeric_identity_does_not_call_verified_authorization_lookup(monkeypatch):
    users = PromoterUsers()
    users.promoters["123"] = "owner-without-authorization"
    users.get_uid_by_platform_identity = MagicMock(side_effect=AssertionError("不应查询授权"))
    service = service_for(monkeypatch, users=users)
    doc = service._attribute_order({"order_sn": "row", "promoter_id": "123",
                                    "attribution_identity": {"relation_id": "irrelevant"}}, "jd")
    assert doc["promoter_uid"] == "owner-without-authorization"
    users.get_uid_by_platform_identity.assert_not_called()


@pytest.mark.parametrize("invalid", [True, 1.0, "０１２３", "0", "-12", "1e3", "1" * 21])
def test_malformed_promoter_evidence_never_falls_back_to_untrusted_uid(monkeypatch, invalid):
    users = PromoterUsers()
    service = service_for(monkeypatch, users=users)
    with pytest.raises(ValueError):
        service._attribute_order({"order_sn": "row", "promoter_id": invalid,
                                  "promoter_uid": "attacker"}, "jd")
    assert users.promoter_lookups == []
    col = MemoryCollection()
    with pytest.raises(ValueError):
        repository(col).upsert_orders([{"order_sn": "row", "promoter_id": invalid}], "jd")
    assert not col.docs


def test_jd_one_hour_windows_keep_existing_budget_and_resume_initial_23_hours(monkeypatch):
    adapter = Adapter()
    adapter.order_window_seconds = 3600
    sync = MemorySyncRepository()
    service = service_for(monkeypatch, adapter, sync=sync)
    target = 200000
    run(service, "jd", now=target)
    assert len(adapter.calls) == module.ORDER_SYNC_MAX_PAGES == 5
    assert module._SYNC_STATE["jd"]["status"] == "partial"
    assert sync.states["jd"]["last_success_end"] == target - 18 * 3600
    assert sync.states["jd"]["pending"]["target_end"] == target
    restarted = service_for(monkeypatch, adapter, sync=sync)
    while sync.states["jd"]["pending"]:
        before = len(adapter.calls)
        run(restarted, "jd", now=target + 99999)
        assert len(adapter.calls) - before <= 5
    assert len(adapter.calls) == 23
    assert all(call["end_time"] - call["start_time"] == 3600 for call in adapter.calls)
    assert adapter.calls[0]["start_time"] == target - 23 * 3600
    assert adapter.calls[-1]["end_time"] == target
    assert module._SYNC_STATE["jd"]["status"] == "success"


def test_identity_lookup_failure_does_not_advance_checkpoint(monkeypatch):
    users = PromoterUsers()
    users.get_uid_by_promoter_id = MagicMock(side_effect=RuntimeError("identity storage offline"))
    sync = MemorySyncRepository({"jd": pending(success=90)})
    before = deepcopy(sync.states)
    orders = MemoryOrders()
    adapter = Adapter(lambda **kw: {"status": "success", "has_more": False,
                                    "order_list": [{"order_sn": "row", "promoter_id": "123"}]})
    service = service_for(monkeypatch, adapter, orders, sync, users)
    run(service, "jd")
    assert sync.states == before and not orders.pages
    assert module._LAST_SYNC_TIMESTAMP["jd"] == 90
    assert module._SYNC_STATE["jd"]["status"] == "error"


@pytest.mark.parametrize("source_time", [100, None])
def test_source_guards_protect_neutral_fields_and_canonical_promoter_evidence(source_time):
    col = MemoryCollection()
    repo = repository(col)
    latest = {"order_sn": "row", "order_modify_at": 200, "order_time": 1000,
              "order_time_basis": "created", "order_amount_basis": "commissionable",
              "order_amount_yuan": 0, "promotion_amount_yuan": 0,
              "is_settled": True, "is_refund": False, "promoter_id": "123",
              "promoter_uid": "owner", "attribution_status": "matched"}
    repo.upsert_orders([latest], "jd")
    stale = dict(latest, order_modify_at=source_time, order_time=9999, order_time_basis="paid",
                 order_amount_basis="paid", order_amount_yuan=100, promotion_amount_yuan=999,
                 is_settled=False, is_refund=True, promoter_id="999", promoter_uid="attacker")
    assert repo.upsert_orders([stale], "jd") == 1
    for key, value in latest.items():
        assert col.docs[0][key] == value
    fresh = dict(stale, order_modify_at=300)
    repo.upsert_orders([fresh], "jd")
    assert col.docs[0]["is_refund"] is True and col.docs[0]["order_time"] == 9999
    assert col.docs[0]["promoter_id"] == "123" and col.docs[0]["promoter_uid"] == "owner"
    assert col.docs[0]["attribution_status"] == "matched"


def test_unknown_promoter_evidence_survives_empty_page_and_normal_sync_replay():
    col = MemoryCollection()
    repo = repository(col)
    repo.upsert_orders([{"order_sn": "unknown", "order_modify_at": 100,
                         "promoter_id": "123", "promoter_uid": "", "attribution_status": "unmatched"}], "jd")
    repo.upsert_orders([{"order_sn": "unknown", "order_modify_at": 200,
                         "promoter_id": "", "promoter_uid": "", "attribution_status": "unmatched"}], "jd")
    assert col.docs[0]["promoter_id"] == "123"
    repo.upsert_orders([{"order_sn": "unknown", "order_modify_at": 200,
                         "promoter_id": "123", "promoter_uid": "new-owner", "attribution_status": "matched"}], "jd")
    repo.upsert_orders([{"order_sn": "unknown", "order_modify_at": 300,
                         "promoter_id": "999", "promoter_uid": "other-owner"}], "jd")
    assert col.docs[0]["promoter_id"] == "123" and col.docs[0]["promoter_uid"] == "new-owner"


def test_mixed_time_filter_sort_and_full_stats_do_not_require_backfill():
    docs = []
    for i in range(140):
        doc = {"platform": "jd", "promoter_uid": "owner", "order_sn": f"{i:03}",
               "promotion_amount_yuan": 2, "is_settled": True, "goods_id": "sku"}
        if i % 2:
            doc.update(order_time=1000 + i, order_time_basis="created", order_pay_time=None,
                       order_amount_basis="commissionable")
        else:
            doc.update(order_pay_time=1000 + i)
        docs.append(doc)
    excluded = [
        {"order_sn": "neutral-before", "order_time": 1, "order_pay_time": 9000},
        {"order_sn": "explicit-zero", "order_time": 0, "order_pay_time": 9000},
        {"order_sn": "old-before", "order_pay_time": 999},
        {"order_sn": "no-time"},
    ]
    for doc in excluded:
        doc.update(platform="jd", promoter_uid="owner", promotion_amount_yuan=9999)
    docs.extend(excluded)
    docs.extend([
        dict(docs[0], platform="pdd", promotion_amount_yuan=9999),
        dict(docs[0], promoter_uid="other-owner", promotion_amount_yuan=9999),
        {"platform": "jd", "promoter_uid": "owner", "order_sn": "null-neutral",
         "order_time": None, "order_pay_time": 1005, "promotion_amount_yuan": 3},
    ])
    col = MemoryCollection(docs)
    result = repository(col).query_user_orders("jd", "owner", start_timestamp=1000, limit=10000)
    assert result["total_count"] == 141
    assert result["total_commission"] == 283 and result["settled_commission"] == 280
    assert len(result["order_list"]) == 100
    assert [row["order_sn"] for row in result["order_list"][:3]] == ["139", "138", "137"]
    assert result["order_list"][0]["order_pay_time"] is None
    assert result["order_list"][1]["order_time"] == 1138
    assert result["order_list"][1]["order_time_basis"] == "paid"
    assert not any("$limit" in stage for stage in col.pipeline[1]["$facet"]["stats"])
    assert col.pipeline[0]["$match"]["$or"] == [
        {"order_time": {"$gte": 1000}}, {"order_time": None, "order_pay_time": {"$gte": 1000}},
    ]


def test_unlimited_historical_query_displays_unknown_time_and_null_fallback_safely():
    docs = [
        {"order_sn": "missing"},
        {"order_sn": "null", "order_time": None, "order_pay_time": 2000},
        {"order_sn": "zero", "order_time": 0, "order_time_basis": "created", "order_pay_time": 9000},
    ]
    for doc in docs:
        doc.update(platform="pdd", promoter_uid="owner")
    result = repository(MemoryCollection(docs)).query_user_orders("pdd", "owner", start_timestamp=None)
    by_sn = {row["order_sn"]: row for row in result["order_list"]}
    assert result["total_count"] == 3
    assert by_sn["missing"]["order_time"] is None and by_sn["missing"]["order_time_str"] == ""
    assert by_sn["null"]["order_time"] == 2000 and by_sn["null"]["order_time_basis"] == "paid"
    assert by_sn["zero"]["order_time"] == 0 and by_sn["zero"]["order_time_basis"] == "created"
    assert all(row["order_amount_basis"] == "paid" for row in result["order_list"])
