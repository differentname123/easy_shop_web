"""Adapter snapshot hand-off contracts, using only offline supplier substitutes."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.pdd_adapter import PddAdapter
from adapters.tb_adapter import TaobaoAdapter
from infrastructure.tb_client import clean_tb_goods_item
from models.product import DetailSnapshot, ProductRef


def official_item(source_id, sign=None):
    return clean_tb_goods_item({
        "item_id": sign or f"sign-{source_id}", "input_item_iid": source_id,
        "item_basic_info": {
            "title": f"Official {source_id}", "sub_title": "Full official description",
            "pict_url": "https://img.test/card.jpg", "small_images": {"string": ["https://img.test/gallery.jpg"]},
            "item_url": f"https://item.taobao.com/item.htm?id={source_id}",
        },
        "price_promotion_info": {"zk_final_price": "12.50"},
        "publish_info": {
            "click_url": "https://private.test/?relation_id=private-user",
            "coupon_share_url": "https://private.test/coupon",
        },
    })


def make_adapter(ids, fetch=None, **source_fields):
    source = {"status": "success", "item_ids": ids, "source_count": len(ids), "has_more": False, **source_fields}
    hdk = SimpleNamespace(get_ranked_ids=Mock(return_value=source))
    official = SimpleNamespace(get_goods_details=Mock(side_effect=fetch or (
        lambda batch: {"status": "success", "data": [official_item(i) for i in batch]})))
    return TaobaoAdapter(settings=SimpleNamespace(taobao_enabled=True), official_client=official,
                         hdk_client=hdk, ztk_client=Mock()), official


def test_rank_snapshot_is_whitelisted_and_exactly_matches_summary_without_duplicate_fetch():
    row = official_item("101")
    row.update(raw={"secret": "private-user"}, _details={"private": True}, relation_id="private-user")
    platform, official = make_adapter(["101"], lambda batch: {"status": "success", "data": [row]})
    snapshots = []
    result = platform.get_recommend_goods(detail_sink=snapshots.append)
    assert result["status"] == "success"
    assert len(snapshots) == 1 and isinstance(snapshots[0], DetailSnapshot)
    snapshot, card = snapshots[0], result["data"][0]
    assert snapshot.ref == ProductRef("taobao", "101", "sign-101")
    assert snapshot.data["goods_id"] == card["goods_id"] == snapshot.ref.goods_id
    assert snapshot.data["goods_sign"] == card["goods_sign"] == snapshot.ref.goods_sign
    assert {key: snapshot.data[key] for key in card} == card
    assert snapshot.data["desc"] == "Full official description"
    assert snapshot.data["carousel_images"] == ["https://img.test/gallery.jpg"]
    assert snapshot.fetched_at.tzinfo == timezone.utc
    assert "desc" not in card and "carousel_images" not in card
    for value in (result, snapshot.data):
        serialized = json.dumps(value)
        for forbidden in ("raw_item", "_details", "click_url", "coupon_share_url", "private-user", "relation_id", "detail_sink", "fetched_at"):
            assert forbidden not in serialized
    official.get_goods_details.assert_called_once_with(["101"])


def test_snapshot_source_time_is_recorded_per_batch_before_later_requests_or_publication(monkeypatch):
    origin = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    clock = SimpleNamespace(value=origin)
    now = Mock(side_effect=lambda tz: clock.value)
    monkeypatch.setattr("adapters.tb_adapter.datetime", SimpleNamespace(now=now))
    ids = [str(i) for i in range(101, 146)]
    snapshots, published_at = [], []

    def fetch(batch):
        assert snapshots == [], "No seeds may be published before the entire page is checked"
        clock.value += timedelta(seconds=10)
        return {"status": "success", "data": [official_item(i) for i in reversed(batch) if i != "102"]}

    def sink(snapshot):
        published_at.append(clock.value)
        snapshots.append(snapshot)
        clock.value += timedelta(minutes=1)  # Time spent queued/collected must not refresh source age.

    platform, official = make_adapter(ids, fetch)
    result = platform.get_recommend_goods(limit=45, detail_sink=sink)
    expected_ids = [i for i in ids if i != "102"]
    assert [card["goods_id"] for card in result["data"]] == expected_ids
    assert [s.ref.goods_id for s in snapshots] == expected_ids
    for snapshot in snapshots:
        batch_index = ids.index(snapshot.ref.goods_id) // 20
        assert snapshot.fetched_at == origin + timedelta(seconds=10 * (batch_index + 1))
    assert published_at[0] == origin + timedelta(seconds=30)
    assert [len(call.args[0]) for call in official.get_goods_details.call_args_list] == [20, 20, 5]
    assert now.call_count == 3
    assert official.get_goods_details.call_count == 3


def test_no_sink_does_not_build_snapshots_details_or_read_source_clock(monkeypatch):
    platform, official = make_adapter(["101"])
    details = Mock(side_effect=AssertionError("No full detail work without a sink"))
    now = Mock(side_effect=AssertionError("No source clock work without a sink"))
    monkeypatch.setattr(platform, "_detail", details)
    monkeypatch.setattr("adapters.tb_adapter.datetime", SimpleNamespace(now=now))
    snapshot_type = Mock(side_effect=AssertionError("No snapshots without a sink"))
    monkeypatch.setattr("adapters.tb_adapter.DetailSnapshot", snapshot_type)
    assert platform.get_recommend_goods()["data"][0]["goods_sign"] == "sign-101"
    official.get_goods_details.assert_called_once_with(["101"])
    details.assert_not_called()
    now.assert_not_called()
    snapshot_type.assert_not_called()


@pytest.mark.parametrize("failure", ["batch", "identity", "conflict", "cross_batch", "cursor", "has_more", "count"])
def test_failed_page_never_publishes_partial_snapshots(failure):
    ids = [str(i) for i in range(101, 122)]
    calls = []

    def fetch(batch):
        calls.append(batch)
        rows = [official_item(i) for i in batch]
        if len(calls) == 2:
            if failure == "batch":
                return {"status": "error", "error_code": "NETWORK_ERROR"}
            if failure == "identity":
                rows = [official_item("unrequested-id")]
            if failure == "conflict":
                rows.append(official_item(batch[0], "different-sign-for-same-input"))
        elif failure == "cross_batch":
            rows.append(official_item(ids[-1]))
        return {"status": "success", "data": rows}

    source_fields = {
        "cursor": {"has_more": True, "next_cursor": None},
        "has_more": {"has_more": "false"},
        "count": {"source_count": -1},
    }.get(failure, {})
    platform, _ = make_adapter(ids, fetch, **source_fields)
    snapshots = []
    result = platform.get_recommend_goods(limit=len(ids), detail_sink=snapshots.append)
    assert result["status"] == "error" and "next_offset" not in result
    assert snapshots == []


@pytest.mark.parametrize("next_cursor", [None, {}, "", {"min_id": "same"}])
def test_invalid_or_nonadvancing_cursor_cannot_publish_snapshots(next_cursor):
    cursor = {"channel_type": 5, "offset": 20, "source": {"min_id": "same"}}
    platform, _ = make_adapter(["101"], has_more=True, next_cursor=next_cursor)
    snapshots = []
    result = platform.get_recommend_goods(offset=20, cursor=cursor, detail_sink=snapshots.append)
    assert result["error_code"] == "PAGINATION_ERROR" and snapshots == []


def test_all_missing_details_advance_source_without_publishing_any_snapshots():
    platform, official = make_adapter(["101", "102"], lambda batch: {"status": "success", "data": []},
                                      has_more=True, next_cursor={"min_id": "next"})
    sink = Mock()
    result = platform.get_recommend_goods(detail_sink=sink)
    assert result["status"] == "success" and result["data"] == []
    assert result["next_offset"] == 2 and result["has_more"] is True
    official.get_goods_details.assert_called_once_with(["101", "102"])
    sink.assert_not_called()


def test_rank_deduplicates_inputs_and_preserves_missing_item_paging():
    platform, official = make_adapter(
        ["101", "102", "101", "103"],
        lambda batch: {"status": "success", "data": [official_item("103"), official_item("101")]},
        has_more=True, next_cursor={"min_id": "next"},
    )
    snapshots = []
    result = platform.get_recommend_goods(limit=4, detail_sink=snapshots.append)
    assert [card["goods_id"] for card in result["data"]] == ["101", "103"]
    assert [s.ref.goods_id for s in snapshots] == ["101", "103"]
    assert result["next_offset"] == 4 and result["has_more"] is True
    assert result["next_cursor"]["source"] == {"min_id": "next"}
    official.get_goods_details.assert_called_once_with(["101", "102", "103"])


@pytest.mark.parametrize("source_id,echoed,sign,expected_id", [
    ("101", "101", "fresh-sign", "101"),
    ("opaque-source-sign", "opaque-source-sign", "fresh-sign", ""),
    ("101", None, "101", ""),
])
def test_rank_original_id_requires_numeric_source_and_exact_official_echo(source_id, echoed, sign, expected_id):
    row = official_item(source_id, sign)
    row["input_item_iid"] = echoed
    row["goods_id"] = "999-untrusted-cleaner-field"
    platform, _ = make_adapter([source_id], lambda batch: {"status": "success", "data": [row]})
    snapshots = []
    result = platform.get_recommend_goods(detail_sink=snapshots.append)
    assert result["status"] == "success"
    assert result["data"][0]["goods_id"] == snapshots[0].ref.goods_id == expected_id
    assert result["data"][0]["goods_sign"] == snapshots[0].ref.goods_sign == sign


def test_pdd_ignores_internal_sink_and_does_not_forward_it_to_supplier(monkeypatch):
    supplier = Mock(return_value={"goods_basic_detail_response": {"list": [
        {"goods_id": 101, "goods_sign": "pdd-sign", "goods_name": "PDD card", "min_group_price": 1000},
    ]}})
    monkeypatch.setattr("adapters.pdd_adapter.call_pdd_api", supplier)
    platform = PddAdapter(enabled=True)
    sink = Mock(side_effect=AssertionError("PDD recommendation has no complete details"))
    result = platform.get_recommend_goods(detail_sink=sink, list_id="supplier-list")
    assert result["status"] == "success"
    params = supplier.call_args.args[-1]
    assert params["list_id"] == "supplier-list" and "detail_sink" not in params
    sink.assert_not_called()


def test_background_eligibility_does_not_remove_pdd_foreground_id_only_support(monkeypatch):
    pdd = PddAdapter(enabled=True)
    assert not pdd.can_prefetch_detail(ProductRef("pdd", "101"))
    assert not pdd.can_prefetch_detail(ProductRef("pdd"))
    assert pdd.can_prefetch_detail(ProductRef("pdd", "101", "pdd-sign"))
    assert pdd.can_prefetch_detail(ProductRef("pdd", "", "pdd-sign"))
    foreground = Mock(return_value={"status": "success", "data": {
        "goods_id": "101", "goods_name": "PDD detail", "min_group_price_yuan": 10,
    }})
    monkeypatch.setattr("adapters.pdd_adapter.pdd_get_detail", foreground)
    assert pdd.get_goods_detail(goods_id="101")["status"] == "success"
    assert foreground.call_args.kwargs["goods_id"] == "101"
    tb, _ = make_adapter([])
    assert tb.can_prefetch_detail(ProductRef("taobao", "101"))
    assert tb.can_prefetch_detail(ProductRef("taobao", "", "verified-sign"))
    assert not tb.can_prefetch_detail(ProductRef("taobao"))
