# -*- coding: utf-8 -*-
"""
好单库线报数据流 (haodanku wire_report_new) 采集、清洗转化与全量失效清除回归测试。
================================================================================
测试重点：
1. 好单库线报数据提取与标准化契约：
   - 淘宝/京东正确映射，飞猪等非电商平台确定性过滤；
   - 结构化 list 文本与链接/口令抽取；
   - 严格以当前拉取时间作为发布时间（不采纳 upstream starttime）；
   - 已知 ID 正确归入 touch_ids 且不重复产出冗余详情。
2. 全量巡视 Fail-Closed 失效清除测试：
   - 本轮全量扫描中未出现的商品（last_seen_at < sweep_start_time）：
     * 初步表中 pending 状态的记录直接置为 cleaned_failed (UPSTREAM_DISAPPEARED)；
     * 正式表中属于该频道的活跃好价 (deal_info.is_deal_tip=True) 摘除好价标 (is_deal_tip=False) 并记录 expired_at；
   - 本轮扫描在架的商品不受任何影响，保持有效。
3. 真实上游最小样本接口契约验证。
================================================================================
"""
import datetime
from unittest.mock import MagicMock
import pytest

from mobile_web.services.deal_fetchers import (
    fetch_haodanku_wire_deals,
    build_deal_record,
)
from mobile_web.services.deal_channel_config import (
    DEFAULT_CHANNEL_CONFIGS,
    get_active_channels,
)
from mobile_web.repositories.deal_raw_pool_repo import DealRawPoolRepository


def test_hdk_channels_configuration():
    """验证好单库 3 大频道在配置中心正确登记，且均为 20 分钟全量巡视。"""
    active = get_active_channels(DEFAULT_CHANNEL_CONFIGS)
    hdk_channels = [c for c in active if c.site_key == "haodanku"]
    assert len(hdk_channels) == 3

    names = {c.name for c in hdk_channels}
    assert names == {
        "好单库_淘宝实时线报",
        "好单库_京东实时线报",
        "好单库_淘宝百亿补贴",
    }
    for c in hdk_channels:
        assert c.interval_seconds == 1200  # 20分钟
        assert c.fetcher_kwargs.get("full_sweep") is True
        assert c.fetcher_name == "fetch_haodanku_wire_deals"

    # 特惠采采也是 20 分钟全量
    th_channel = next(c for c in active if c.name == "特惠采采_首页精选")
    assert th_channel.interval_seconds == 1200
    assert th_channel.fetcher_kwargs.get("full_sweep") is True


def test_hdk_wire_fetcher_parsing_and_filtering():
    """验证好单库线报数据解析：平台识别、飞猪过滤、图文链接拼接与时间戳。"""
    mock_session = MagicMock()

    # 模拟 API 返回包含 3 条数据：
    # 1. 淘宝单品 (platform=1)
    # 2. 京东单品 (platform=2)
    # 3. 飞猪旅行 (platform=3，应过滤)
    mock_payload = {
        "code": 200,
        "msg": "SUCCESS",
        "min_id": 2,
        "data": [
            {
                "wire_id": "9001",
                "platform": "1",
                "cate_id": ["10003"],
                "starttime": "1630000000",  # 历史旧时间
                "images": ["https://img.alicdn.com/img1.jpg,https://img.alicdn.com/img2.jpg"],
                "list": [
                    {"content": "豪车❗旗舰店"},
                    {"content": "【34.9】高洁丝 纯棉夜安裤20条"},
                    {"content": "折1.74/条！透气轻薄"},
                    {"item_id": "bJZkm9ksOhoR707wwZIy7buQtR-ebK049oszM0NnNZQhq"},
                ],
            },
            {
                "wire_id": "9002",
                "platform": "2",
                "cate_id": ["10004"],
                "starttime": "1630000000",
                "images": ["https://img14.360buyimg.com/jd1.jpg"],
                "list": [
                    {"content": "自营良品铺子 柠檬去骨凤爪400g"},
                    {"content": "plus拍下【29.9元】之前都是34.9！"},
                    {"item_id": "https://u.jd.com/o1H337p"},
                ],
            },
            {
                "wire_id": "9003",
                "platform": "3",  # 飞猪
                "cate_id": ["10367"],
                "starttime": "1630000000",
                "images": ["https://img.alicdn.com/fliggy.jpg"],
                "list": [
                    {"content": "泰国普吉岛6天5晚游 1880元"},
                    {"item_id": "fliggy_item_code_xxx"},
                ],
            },
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    mock_session.get.return_value = mock_resp

    before_fetch = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M")

    res = fetch_haodanku_wire_deals(
        label_id=10003,
        channel_name="好单库_淘宝实时线报",
        session=mock_session,
        max_pages=1,
        skip_detail_ids={"9002"},  # 9002 是已知 ID
    )

    assert res["status"] == "ok"
    assert res["raw_list_count"] == 3
    assert res["skipped_unsupported"] == 1  # 飞猪被过滤
    assert res["skipped_known"] == 1        # 9002 是已知商品
    assert "9002" in res["touch_ids"]       # 9002 被归入 touch_ids 待刷新时间

    records = res["records"]
    assert len(records) == 1
    rec = records[0]
    assert rec["source_item_id"] == "9001"
    assert rec["source_site"] == "haodanku"
    assert rec["source_channel"] == "好单库_淘宝实时线报"
    assert rec["platform"] == "taobao"
    assert rec["mall_name"] == "淘宝"
    assert len(rec["tutorial_images"]) == 2  # 逗号分割的图片正确展开
    assert rec["has_full_detail"] is True

    # 验证时间戳：必须为当前拉取时间，绝不能是上游的 1630000000 (2021年)
    assert rec["publish_time"].startswith(before_fetch)
    assert rec["hot_metrics"]["upstream_starttime"] == "1630000000"

    # 验证提取到的链接
    extracted_urls = [lk["url"] for lk in rec["extracted_links"]]
    assert any("uland.taobao.com" in u and "bJZkm9ksOho" in u for u in extracted_urls)


def test_expire_missing_from_full_sweep_logic():
    """验证全量巡视失效清除逻辑 (Fail-Closed)。"""
    from types import SimpleNamespace

    mock_col = MagicMock()
    mock_formal_col = MagicMock()

    mock_raw_update = MagicMock()
    mock_raw_update.modified_count = 5
    mock_col.update_many.return_value = mock_raw_update

    mock_formal_update = MagicMock()
    mock_formal_update.modified_count = 3
    mock_formal_col.update_many.return_value = mock_formal_update

    db_mock = SimpleNamespace(
        get_collection=lambda n: mock_col if "raw" in n else mock_formal_col
    )
    repo = DealRawPoolRepository(db_instance=db_mock)
    repo._indexes_ready = True

    sweep_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    res = repo.expire_missing_from_full_sweep(
        source_site="haodanku",
        channel="好单库_京东实时线报",
        sweep_start_time=sweep_time,
    )

    assert res["raw_expired"] == 5
    assert res["formal_expired"] == 3

    # 验证初步表查询参数
    raw_call_args = mock_col.update_many.call_args[0]
    raw_query = raw_call_args[0]
    assert raw_query["source_site"] == "haodanku"
    assert raw_query["source_channel"] == "好单库_京东实时线报"
    assert raw_query["last_seen_at"]["$lt"] == sweep_time
    assert raw_call_args[1]["$set"]["clean_error_code"] == "UPSTREAM_DISAPPEARED"

    # 验证正式表查询参数
    formal_call_args = mock_formal_col.update_many.call_args[0]
    formal_query = formal_call_args[0]
    assert formal_query["deal_info.source_site"] == "haodanku"
    assert formal_query["deal_info.is_deal_tip"] is True
    assert formal_query["deal_info.updated_at"]["$lt"] == sweep_time
    assert formal_call_args[1]["$set"]["deal_info.is_deal_tip"] is False


def test_hdk_wire_contract_with_mocked_upstream():
    """验证好单库京东线报上游接口契约映射。"""
    mock_session = MagicMock()
    mock_payload = {
        "code": 200,
        "msg": "SUCCESS",
        "min_id": 2,
        "data": [
            {
                "wire_id": "8888",
                "platform": "2",
                "cate_id": ["10004"],
                "starttime": "1795165920",
                "images": ["https://img14.360buyimg.com/pop/jfs/t1/1.jpg"],
                "list": [
                    {"content": "自营良品铺子 柠檬去骨凤爪400g"},
                    {"content": "plus拍下【29.9元】之前都是34.9！"},
                    {"item_id": "https://u.jd.com/orHYT9i"},
                ],
            }
        ],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    mock_session.get.return_value = mock_resp

    res = fetch_haodanku_wire_deals(
        label_id=10004,
        channel_name="好单库_京东实时线报",
        session=mock_session,
        apikey="test",
        page_size=5,
        max_pages=1,
    )
    assert res["status"] == "ok"
    assert res["pages_crawled"] >= 1
    assert len(res["records"]) == 1

    first = res["records"][0]
    assert first["source_site"] == "haodanku"
    assert first["platform"] == "jd"
    assert first["has_full_detail"] is True
    assert "良品铺子" in first["title"]
    assert any("u.jd.com" in lk["url"] for lk in first["extracted_links"])


if __name__ == "__main__":
    # 单独运行时执行真实网络请求冒烟验证
    import requests
    real_res = fetch_haodanku_wire_deals(
        label_id=10004,
        channel_name="好单库_京东实时线报",
        apikey="test",
        page_size=3,
        max_pages=1,
    )
    print("Live HDK smoke test:", real_res["status"], "records:", len(real_res["records"]))
