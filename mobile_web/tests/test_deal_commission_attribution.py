# -*- coding: utf-8 -*-
"""用户爆料生效期归因与佣金统计引擎（Decimal 精确算账 + 三平台 Fail-Closed）综合测试。"""
from __future__ import annotations

import datetime
from dataclasses import replace

from fastapi.testclient import TestClient

import config
import main as main_module
from auth import create_access_token
from main import create_app
from models.deal_constants import USER_SUBMIT_SITE
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.order_repo import OrderRepository
from services.deal_commission_service import (
    classify_order_commission_bucket,
    compute_user_deals_commission_report,
    extract_deal_order_goods_ids,
    is_site_owner_default_order,
)
from services.deal_submit_service import DealSubmitService
from tests.test_user_deal_submit import _MemDB, _make_harness, _make_png_b64


def test_site_owner_default_order_filter_across_all_platforms():
    # 1. 拼多多：空 promoter_uid 且非 False is_self_operated 才算站长默认订单
    assert is_site_owner_default_order({"platform": "pdd", "promoter_uid": "", "is_self_operated": True}, "pdd") is True
    assert is_site_owner_default_order({"platform": "pdd", "promoter_uid": "u_other"}, "pdd") is False
    assert is_site_owner_default_order({"platform": "pdd", "promoter_uid": "", "is_self_operated": False}, "pdd") is False
    assert is_site_owner_default_order({"platform": "pdd", "attribution_status": "matched"}, "pdd") is False

    # 2. 淘宝：promoter_uid 为空但携带 relation_id / special_id / attribution_identity 一律视为渠道单剔除
    assert is_site_owner_default_order({"platform": "taobao", "promoter_uid": "", "relation_id": "", "special_id": ""}, "taobao") is True
    assert is_site_owner_default_order({"platform": "taobao", "relation_id": "0", "special_id": "0"}, "taobao") is True
    assert is_site_owner_default_order({"platform": "taobao", "promoter_uid": "", "relation_id": "23456"}, "taobao") is False
    assert is_site_owner_default_order({"platform": "taobao", "promoter_uid": "", "special_id": "998877"}, "taobao") is False
    assert is_site_owner_default_order({"platform": "taobao", "attribution_identity": {"relation_id": "23456"}}, "taobao") is False

    # 3. 京东：允许空/0 或与默认站长推广位一致；任何其他推广位或 sub_union_id 一律剔除
    assert is_site_owner_default_order({"platform": "jd", "position_id": "", "sub_union_id": ""}, "jd", "3001001") is True
    assert is_site_owner_default_order({"platform": "jd", "position_id": "0"}, "jd", "3001001") is True
    assert is_site_owner_default_order({"platform": "jd", "position_id": "3001001", "promoter_id": "3001001"}, "jd", "3001001") is True
    assert is_site_owner_default_order({"platform": "jd", "position_id": "3009999"}, "jd", "3001001") is False
    assert is_site_owner_default_order({"platform": "jd", "position_id": "3001001"}, "jd", "") is False
    assert is_site_owner_default_order({"platform": "jd", "position_id": "3001001", "sub_union_id": "u_sub1"}, "jd", "3001001") is False


def test_order_bucket_classification_and_decimal_precision():
    # 退款、失效、未付款统一归入 excluded
    for plat, st in (
        ("jd", 15), ("jd", 28), ("jd", 2), ("jd", 24),
        ("pdd", -1), ("pdd", 4), ("pdd", 8), ("pdd", 10),
        ("taobao", 13),
    ):
        bucket, _ = classify_order_commission_bucket({"order_status": st, "promotion_amount_yuan": 5.0}, plat)
        assert bucket == "excluded"

    assert classify_order_commission_bucket({"is_refund": True, "promotion_amount_yuan": 8.0}, "pdd")[0] == "excluded"
    assert classify_order_commission_bucket({"is_invalid": True, "promotion_amount_yuan": 8.0}, "taobao")[0] == "excluded"

    # 售后中（JD 27 / TB refund_tag=1）单独标 after_sale
    assert classify_order_commission_bucket({"order_status": 27, "is_after_sale": True, "promotion_amount_yuan": 3.5}, "jd")[0] == "after_sale"
    assert classify_order_commission_bucket({"order_status": 12, "refund_tag": "1", "promotion_amount_yuan": 4.2}, "taobao")[0] == "after_sale"

    # 已结算优先取 settled_commission_yuan
    b_set, val_set = classify_order_commission_bucket(
        {"is_settled": True, "settled_commission_yuan": 3.45, "promotion_amount_yuan": 4.0},
        "jd",
    )
    assert b_set == "settled" and str(val_set) == "3.45"

    b_pend, val_pend = classify_order_commission_bucket({"is_settled": False, "promotion_amount_yuan": 0.1}, "pdd")
    assert b_pend == "pending" and str(val_pend) == "0.10"


def test_jd_sku_resolution_and_unmapped_fail_closed():
    gids_mapped, unmapped = extract_deal_order_goods_ids(
        {
            "resolved_platform": "jd",
            "resolved_product_id": "jd_img_abc123",
            "resolved_goods_sign": "encrypted_sign_xyz",
            "converted_links": [{
                "link_id": "L1",
                "official_product": {"product_url": "https://item.jd.com/100012345678.html"},
            }],
        },
        formal_doc={"product_url": "https://item.jd.com/100012345678.html"},
    )
    assert gids_mapped == {"100012345678"}
    assert unmapped is False

    gids_missing, is_unmapped = extract_deal_order_goods_ids(
        {
            "resolved_platform": "jd",
            "resolved_product_id": "jd_img_abc123",
            "resolved_goods_sign": "encrypted_sign_xyz",
            "converted_links": [{
                "link_id": "L1",
                "official_product": {"product_url": "https://jingfen.jd.com/detail/encrypted.html"},
            }],
        },
        formal_doc=None,
    )
    assert gids_missing == set()
    assert is_unmapped is True


def test_half_open_window_dedup_utc_timezone_and_decimal_sum():
    db = _MemDB()
    raw_repo = DealRawPoolRepository(db_instance=db)
    order_repo = OrderRepository(db_instance=db)

    # 故意使用 naive UTC datetime（在 UTC+8 运行环境验证不会偏移 8 小时）
    t_a_start = datetime.datetime(2026, 11, 20, 2, 0, 0)   # UTC 02:00 = 北京 10:00
    t_switch = datetime.datetime(2026, 11, 20, 6, 0, 0)    # UTC 06:00 = 北京 14:00
    t_b_end = datetime.datetime(2026, 11, 20, 10, 0, 0)    # UTC 10:00 = 北京 18:00
    ts_switch = int(t_switch.replace(tzinfo=datetime.timezone.utc).timestamp())
    ts_b_end = int(t_b_end.replace(tzinfo=datetime.timezone.utc).timestamp())

    rows = [
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_deal_a",
            "resolved_platform": "pdd",
            "resolved_product_id": "555666",
            "display_started_at": t_a_start,
            "display_ended_at": t_switch,
            "replaced_by_site": USER_SUBMIT_SITE,
            "replaced_by_item_id": "us_deal_b",
        },
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_deal_b",
            "resolved_platform": "pdd",
            "resolved_product_id": "555666",
            "display_started_at": t_switch,
            "display_ended_at": t_b_end,
        },
    ]

    # 拼多多订单 goods_id 可能是 int（也混合浮点 0.1 + 0.2 检验 Decimal 无精度漂移）
    order_repo.col.docs.extend([
        {
            "platform": "pdd", "order_sn": "PDD_A_1", "goods_id": 555666,
            "order_time": ts_switch - 1, "order_amount_yuan": 19.9,
            "promotion_amount_yuan": 0.1, "is_settled": True, "settled_commission_yuan": 0.1,
            "order_status": 3, "order_status_desc": "已结算", "is_refund": False, "is_invalid": False,
            "promoter_uid": "", "is_self_operated": True,
        },
        {
            "platform": "pdd", "order_sn": "PDD_A_2", "goods_id": "555666",
            "order_time": ts_switch - 10, "order_amount_yuan": 29.9,
            "promotion_amount_yuan": 0.2, "is_settled": False,
            "order_status": 1, "order_status_desc": "已成团", "is_refund": False, "is_invalid": False,
            "promoter_uid": "", "is_self_operated": True,
        },
        {
            "platform": "pdd", "order_sn": "PDD_A_CH", "goods_id": 555666,
            "order_time": ts_switch - 20, "order_amount_yuan": 29.9,
            "promotion_amount_yuan": 9.9, "is_settled": True,
            "order_status": 3, "is_refund": False, "is_invalid": False,
            "promoter_uid": "u_promoter_1", "is_self_operated": False,
        },
        {
            "platform": "pdd", "order_sn": "PDD_A_REF", "goods_id": 555666,
            "order_time": ts_switch - 30, "order_amount_yuan": 29.9,
            "promotion_amount_yuan": 8.8, "is_settled": False,
            "order_status": 4, "order_status_desc": "审核失败", "is_refund": True, "is_invalid": False,
            "promoter_uid": "", "is_self_operated": True,
        },
        {
            # 恰好落在交接边界 14:00:00：按半开区间只归属 B，绝不双计给 A
            "platform": "pdd", "order_sn": "PDD_B_BOUNDARY", "goods_id": 555666,
            "order_time": ts_switch, "order_amount_yuan": 39.9,
            "promotion_amount_yuan": 1.25, "is_settled": True, "settled_commission_yuan": 1.25,
            "order_status": 3, "order_status_desc": "已结算", "is_refund": False, "is_invalid": False,
            "promoter_uid": "", "is_self_operated": True,
        },
        {
            # 恰好落在下线时间 18:00:00：已下线不计入 B
            "platform": "pdd", "order_sn": "PDD_AFTER_END", "goods_id": 555666,
            "order_time": ts_b_end, "order_amount_yuan": 39.9,
            "promotion_amount_yuan": 5.0, "is_settled": True,
            "order_status": 3, "is_refund": False, "is_invalid": False,
            "promoter_uid": "", "is_self_operated": True,
        },
    ])

    per_item, summary = compute_user_deals_commission_report(
        rows,
        order_repo=order_repo,
        formal_col=raw_repo.formal_col,
        now_ts=ts_b_end + 100,
    )

    a_stats = per_item["us_deal_a"]["commission_stats"]
    assert per_item["us_deal_a"]["display_duration_text"] == "4小时"
    assert per_item["us_deal_a"]["display_started_str"] == "11-20 10:00"
    assert per_item["us_deal_a"]["display_ended_str"] == "11-20 14:00"
    assert a_stats["valid_order_count"] == 2
    assert a_stats["settled_order_count"] == 1
    assert a_stats["pending_order_count"] == 1
    assert a_stats["excluded_channel_count"] == 1
    assert a_stats["excluded_refund_invalid_count"] == 1
    assert a_stats["estimated_commission_yuan"] == 0.3  # 0.10 + 0.20 精确等于 0.3
    assert a_stats["settled_commission_yuan"] == 0.1
    assert len(a_stats["recent_orders"]) == 2
    assert a_stats["recent_orders"][0]["order_sn"] in ("PD***_1", "PD***_2")

    b_stats = per_item["us_deal_b"]["commission_stats"]
    assert b_stats["recent_orders"][0]["order_sn"] == "PDD_****DARY"
    assert b_stats["valid_order_count"] == 1
    assert b_stats["estimated_commission_yuan"] == 1.25
    assert b_stats["settled_commission_yuan"] == 1.25

    assert summary["displayed_count"] == 2
    assert summary["valid_order_count"] == 3
    assert summary["estimated_commission_yuan"] == 1.55
    assert summary["settled_commission_yuan"] == 1.35
    assert summary["excluded_channel_count"] == 1
    assert summary["excluded_refund_invalid_count"] == 1


def test_overlapping_windows_and_active_72h_cap():
    db = _MemDB()
    raw_repo = DealRawPoolRepository(db_instance=db)
    order_repo = OrderRepository(db_instance=db)
    start_dt = datetime.datetime(2026, 11, 20, 0, 0, 0)
    start_ts = int(start_dt.replace(tzinfo=datetime.timezone.utc).timestamp())

    raw_repo.formal_col.docs.append({
        "platform": "jd",
        "product_id": "jd_img_888",
        "product_url": "https://item.jd.com/1000888999.html",
        "deal_info": {
            "is_deal_tip": True,
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_active_1",
            "updated_at": start_dt,
        },
    })
    rows = [
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_active_1",
            "resolved_platform": "jd",
            "resolved_product_id": "jd_img_888",
            "display_started_at": start_dt,
        },
        {
            # 异常历史重叠记录（模拟极端脏数据）检验 summary 绝对不双计同一 order_sn
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_overlap_2",
            "resolved_platform": "jd",
            "resolved_product_id": "jd_img_888",
            "display_started_at": start_dt,
            "display_ended_at": start_dt + datetime.timedelta(hours=10),
        },
    ]
    order_repo.col.docs.extend([
        {
            "platform": "jd", "order_sn": "JD_VALID_1", "goods_id": "1000888999",
            "order_create_time": start_ts + 3600, "order_time": start_ts + 3600,
            "order_amount_yuan": 99.0, "promotion_amount_yuan": 6.5, "is_settled": False,
            "order_status": 17, "order_status_desc": "已完成", "is_refund": False, "is_invalid": False,
            "position_id": "3001001", "promoter_id": "3001001", "sub_union_id": "",
        },
        {
            # 72 小时之后（尚未被维护线程扫到）订单绝不再归因给活跃爆料
            "platform": "jd", "order_sn": "JD_OVER_72H", "goods_id": "1000888999",
            "order_create_time": start_ts + 73 * 3600, "order_time": start_ts + 73 * 3600,
            "order_amount_yuan": 99.0, "promotion_amount_yuan": 12.0, "is_settled": True,
            "order_status": 18, "is_refund": False, "is_invalid": False,
            "position_id": "3001001", "promoter_id": "3001001",
        },
        {
            # 售后中订单隔离计数，不计入预估佣金
            "platform": "jd", "order_sn": "JD_AFTERSALE", "goods_id": "1000888999",
            "order_create_time": start_ts + 7200, "order_time": start_ts + 7200,
            "order_amount_yuan": 99.0, "promotion_amount_yuan": 4.0, "is_settled": False,
            "order_status": 27, "order_status_desc": "售后中", "is_after_sale": True,
            "position_id": "3001001", "promoter_id": "3001001",
        },
        {
            # 待付款 15 剔除
            "platform": "jd", "order_sn": "JD_UNPAID", "goods_id": "1000888999",
            "order_create_time": start_ts + 7300, "order_time": start_ts + 7300,
            "order_amount_yuan": 99.0, "promotion_amount_yuan": 4.0, "is_settled": False,
            "order_status": 15, "order_status_desc": "待付款",
            "position_id": "3001001", "promoter_id": "3001001",
        },
    ])

    per_item, summary = compute_user_deals_commission_report(
        rows,
        order_repo=order_repo,
        formal_col=raw_repo.formal_col,
        default_jd_position_id="3001001",
        now_ts=start_ts + 80 * 3600,
    )
    st1 = per_item["us_active_1"]["commission_stats"]
    assert per_item["us_active_1"]["display_status"] == "expired"
    assert per_item["us_active_1"]["display_duration_text"] == "3天"
    assert st1["valid_order_count"] == 1
    assert st1["after_sale_order_count"] == 1
    assert st1["excluded_refund_invalid_count"] == 1
    assert st1["estimated_commission_yuan"] == 6.5

    # 虽然两条记录都重叠命中 JD_VALID_1，总汇总按 order_sn 去重绝不翻倍成 13.0
    assert summary["valid_order_count"] == 1
    assert summary["estimated_commission_yuan"] == 6.5
    assert summary["after_sale_order_count"] == 1
    assert summary["excluded_refund_invalid_count"] == 1


def test_my_submissions_http_end_to_end_with_order_attribution(monkeypatch, tmp_path):
    import api.deal_submit as api_mod

    db, raw_repo, _, _, svc = _make_harness(image_dir=tmp_path / "imgs")
    order_repo = svc.order_repo

    start_dt = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=3)
    start_ts = int(start_dt.replace(tzinfo=datetime.timezone.utc).timestamp())

    res = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=24.9,
        tutorial_text="领券下单24.9元",
        images_base64=[_make_png_b64(320, 240)],
    )
    tip_id = res["data"]["tip_id"]
    raw_repo.col.update_many(
        {"source_item_id": tip_id},
        {
            "$set": {
                "clean_status": "cleaned_success",
                "clean_stage": "done",
                "display_started_at": start_dt,
            }
        },
    )
    raw_repo.formal_col.docs.append({
        "platform": "jd",
        "product_id": "1001001",
        "goods_id": "1001001",
        "product_url": "https://item.jd.com/1001001.html",
        "deal_info": {
            "is_deal_tip": True,
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": tip_id,
            "updated_at": start_dt,
        },
    })
    order_repo.col.docs.extend([
        {
            "platform": "jd",
            "order_sn": "2026112600001",
            "goods_id": "1001001",
            "order_create_time": start_ts + 600,
            "order_time": start_ts + 600,
            "order_amount_yuan": 24.9,
            "promotion_amount_yuan": 2.49,
            "settled_commission_yuan": 2.49,
            "is_settled": True,
            "order_status": 18,
            "order_status_desc": "已结算",
            "is_refund": False,
            "is_invalid": False,
            "position_id": "",
        },
        {
            "platform": "jd",
            "order_sn": "2026112600002",
            "goods_id": "1001001",
            "order_create_time": start_ts + 900,
            "order_time": start_ts + 900,
            "order_amount_yuan": 24.9,
            "promotion_amount_yuan": 2.49,
            "is_settled": False,
            "order_status": 16,
            "order_status_desc": "已付款",
            "is_refund": False,
            "is_invalid": False,
            "position_id": "",
        },
    ])

    monkeypatch.setattr(api_mod, "deal_submit_service", svc)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )
    app = create_app()
    with TestClient(app) as client:
        token = create_access_token("u_alice")
        resp = client.get("/api/deals/my_submissions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["summary"]["displayed_count"] == 1
        assert body["summary"]["active_count"] == 1
        assert body["summary"]["valid_order_count"] == 2
        assert body["summary"]["estimated_commission_yuan"] == 4.98
        assert body["summary"]["settled_commission_yuan"] == 2.49

        item = body["items"][0]
        assert item["display_status"] == "active"
        assert "小时" in item["display_duration_text"]
        assert item["commission_stats"]["valid_order_count"] == 2
        assert item["commission_stats"]["estimated_commission_yuan"] == 4.98
        assert len(item["commission_stats"]["recent_orders"]) == 2


def test_jd_canonical_image_key_extraction_order_attribution_and_partial_upsert():
    # 1. 34 位与历史 26 位 360buyimg 主图指纹及 image_url 均可解除 jd_sku_unmapped，占位符保持 Fail-Closed
    gids_34, unmap_34 = extract_deal_order_goods_ids(
        {"resolved_platform": "jd", "resolved_product_id": "66bde2a7Fb47aa096_69ff34903c83a560"},
        formal_doc=None,
    )
    assert gids_34 == {"66bde2a7Fb47aa096_69ff34903c83a560"} and unmap_34 is False

    gids_26, unmap_26 = extract_deal_order_goods_ids(
        {"resolved_platform": "jd", "resolved_product_id": "453643ee_5779ca2cNeebcc7e8"},
        formal_doc=None,
    )
    assert gids_26 == {"453643ee_5779ca2cNeebcc7e8"} and unmap_26 is False

    gids_img, unmap_img = extract_deal_order_goods_ids(
        {"resolved_platform": "jd", "resolved_product_id": "placeholder"},
        formal_doc={"image_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/6aca4d10F381a5657/0944320320d0cf17.jpg"},
    )
    assert gids_img == {"6aca4d10F381a5657_0944320320d0cf17"} and unmap_img is False

    # 2. 通过 canonical_goods_id 与 goods_thumbnail_url 归因（含 min_timestamp 组合过滤 & 同时命中数字SKU+指纹零重复）
    db = _MemDB()
    raw_repo = DealRawPoolRepository(db_instance=db)
    order_repo = OrderRepository(db_instance=db)
    start_dt = datetime.datetime(2026, 11, 25, 0, 0, 0)
    start_ts = int(start_dt.replace(tzinfo=datetime.timezone.utc).timestamp())

    rows = [
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_jd_img_1",
            "resolved_platform": "jd",
            "resolved_product_id": "66bde2a7Fb47aa096_69ff34903c83a560",
            "display_started_at": start_dt,
            "display_ended_at": start_dt + datetime.timedelta(hours=12),
        },
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_jd_both_2",
            "resolved_platform": "jd",
            "resolved_product_id": "6aca4d10F381a5657_0944320320d0cf17",
            "converted_links": [{
                "official_product": {"product_url": "https://item.jd.com/10235356702978.html"},
            }],
            "display_started_at": start_dt,
            "display_ended_at": start_dt + datetime.timedelta(hours=12),
        },
    ]
    order_repo.col.docs.extend([
        {
            "platform": "jd",
            "order_sn": "JD_CANON_1",
            "goods_id": "100122053502",
            "canonical_goods_id": "66bde2a7Fb47aa096_69ff34903c83a560",
            "order_create_time": start_ts + 1800,
            "order_time": start_ts + 1800,
            "order_amount_yuan": 49.9,
            "promotion_amount_yuan": 3.2,
            "is_settled": False,
            "order_status": 16,
            "order_status_desc": "已付款",
            "is_refund": False,
            "is_invalid": False,
            "position_id": "3110104129",
        },
        {
            # 缺 canonical_goods_id 但带 goods_thumbnail_url 同样可提取指纹归因，且同时命中 skuId 与指纹绝不双计
            "platform": "jd",
            "order_sn": "JD_THUMB_ONLY_2",
            "goods_id": "10235356702978",
            "goods_thumbnail_url": "https://img10.360buyimg.com/n1/jfs/t1/9/8/7/6aca4d10F381a5657/0944320320d0cf17.jpg",
            "order_create_time": start_ts + 2400,
            "order_time": start_ts + 2400,
            "order_amount_yuan": 29.9,
            "promotion_amount_yuan": 1.8,
            "settled_commission_yuan": 1.8,
            "is_settled": True,
            "order_status": 18,
            "order_status_desc": "已结算",
            "is_refund": False,
            "is_invalid": False,
            "position_id": "3110104129",
        },
        {
            # 展示前历史订单（早于 min_timestamp）：验证 $and + $or 不会错放窗口前订单
            "platform": "jd",
            "order_sn": "JD_BEFORE_START",
            "goods_id": "100122053502",
            "canonical_goods_id": "66bde2a7Fb47aa096_69ff34903c83a560",
            "order_create_time": start_ts - 10,
            "order_time": start_ts - 10,
            "order_amount_yuan": 49.9,
            "promotion_amount_yuan": 9.9,
            "is_settled": True,
            "settled_commission_yuan": 9.9,
            "order_status": 18,
            "is_refund": False,
            "is_invalid": False,
            "position_id": "3110104129",
        },
    ])

    per_item, summary = compute_user_deals_commission_report(
        rows,
        order_repo=order_repo,
        formal_col=raw_repo.formal_col,
        default_jd_position_id="3110104129",
        now_ts=start_ts + 20 * 3600,
    )
    st1 = per_item["us_jd_img_1"]["commission_stats"]
    st2 = per_item["us_jd_both_2"]["commission_stats"]
    assert st1["jd_sku_unmapped"] is False and st1["valid_order_count"] == 1 and st1["estimated_commission_yuan"] == 3.2
    assert st2["jd_sku_unmapped"] is False and st2["valid_order_count"] == 1 and st2["estimated_commission_yuan"] == 1.8
    assert summary["valid_order_count"] == 2
    assert summary["estimated_commission_yuan"] == 5.0
    assert summary["settled_commission_yuan"] == 1.8

    # 3. 仓储 upsert_orders 增量无图更新不抹掉已存 canonical_goods_id 与 goods_thumbnail_url
    from tests.test_order_sync_protocol import MemoryCollection, repository
    from tests.test_user_deal_submit import _MemCollection

    class _CountingMemoryCollection(MemoryCollection):
        def aggregate(self, pipeline):
            if len(pipeline) == 2 and "$match" in pipeline[0] and "$group" in pipeline[1]:
                match_spec = pipeline[0]["$match"]
                group_field = str(pipeline[1]["$group"]["_id"]).lstrip("$")
                counts = {}
                for doc in self.docs:
                    if _MemCollection._matches(doc, match_spec):
                        key = doc.get(group_field)
                        if key is not None and key != "":
                            counts[key] = counts.get(key, 0) + 1
                return [{"_id": k, "valid_orders": v} for k, v in counts.items()]
            return super().aggregate(pipeline)

    mem_col = _CountingMemoryCollection()
    real_repo = repository(mem_col)
    real_repo.upsert_orders([{
        "order_sn": "JD_PARTIAL_1",
        "goods_id": "100122053502",
        "canonical_goods_id": "66bde2a7Fb47aa096_69ff34903c83a560",
        "goods_thumbnail_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/66bde2a7Fb47aa096/69ff34903c83a560.jpg",
        "order_modify_at": 100.0,
        "order_time": 90,
        "is_refund": False,
        "is_invalid": False,
    }], "jd")
    real_repo.upsert_orders([{
        "order_sn": "JD_PARTIAL_1",
        "goods_id": "100122053502",
        "canonical_goods_id": "",
        "goods_thumbnail_url": "",
        "order_modify_at": 200.0,
        "order_time": 90,
        "is_refund": False,
        "is_invalid": False,
    }], "jd")
    saved = mem_col.docs[0]
    assert saved["canonical_goods_id"] == "66bde2a7Fb47aa096_69ff34903c83a560"
    assert saved["goods_thumbnail_url"] == "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/66bde2a7Fb47aa096/69ff34903c83a560.jpg"
    assert real_repo.count_valid_orders_by_goods_ids(
        "jd", ["66bde2a7Fb47aa096_69ff34903c83a560", "100122053502"]
    ) == {"66bde2a7Fb47aa096_69ff34903c83a560": 1, "100122053502": 1}
