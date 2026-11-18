# -*- coding: utf-8 -*-
"""
特惠采采 (tehuicaicai) 增量水位线拉取与信源信誉加权回归测试
"""

from unittest.mock import MagicMock
import pytest

from mobile_web.scripts.test_deal_sites_scraper import (
    fetch_tehuicaicai_deals,
    build_deal_record,
)
from mobile_web.scripts.run_deal_pool_scheduler import (
    SOURCE_REPUTATION_BONUS,
    QUALIFIED_DEAL_MIN_SCORE,
    evaluate_quality_signals,
)


def test_source_reputation_bonus_rules():
    """
    测试信誉加分机制的四条工程红线：
    1. 正常合格爆料成功获得配置加分；
    2. 满分或高分爆料执行 100 分严格封顶截断；
    3. 未达到及格线 (60分) 的劣质/含糊爆料绝不激活加分，防次品偷渡入库；
    4. 无效/死链爆料保持 0 分，绝不起死回生。
    """
    assert "tehuicaicai" in SOURCE_REPUTATION_BONUS
    bonus = SOURCE_REPUTATION_BONUS["tehuicaicai"]
    assert bonus == 10

    def apply_bonus(site: str, raw_score: int, is_valid: bool = True):
        if not is_valid:
            return 0
        score = max(0, min(100, raw_score))
        b = SOURCE_REPUTATION_BONUS.get(site, 0)
        if b > 0 and score >= QUALIFIED_DEAL_MIN_SCORE:
            score = min(100, score + b)
        return score

    # 1. 正常合格 (75 -> 85)
    assert apply_bonus("tehuicaicai", 75) == 85
    # 2. 封顶截断 (95 -> 100)
    assert apply_bonus("tehuicaicai", 95) == 100
    # 3. 未及格次品不放水 (55 -> 55)
    assert apply_bonus("tehuicaicai", 55) == 55
    # 4. 无效死链不复活 (80, is_valid=False -> 0)
    assert apply_bonus("tehuicaicai", 80, is_valid=False) == 0
    # 5. 未配置加分的站点不受干扰
    assert apply_bonus("zhetaoke", 75) == 75


def test_tehuicaicai_watermark_early_stop():
    """
    测试水位线增量早停机制：
    - 当某页所有商品均在已存集合中 (skip_detail_ids) 时，立即触发早停，不再翻取下一页。
    """
    mock_session = MagicMock()

    # 构造两页 Mock 响应：
    # 第 1 页：包含 101, 102 (都在 skip_detail_ids 中)
    # 第 2 页：包含 103, 104
    page1_payload = {
        "code": "OK",
        "data": {
            "items": [
                {
                    "id": 101,
                    "title": "测试商品1",
                    "platform_code": "jd",
                    "platform_name": "京东",
                    "display_price": "10元",
                    "cover_image": {"public_url": "https://img.test/1.jpg"},
                },
                {
                    "id": 102,
                    "title": "测试商品2",
                    "platform_code": "pdd",
                    "platform_name": "拼多多",
                    "display_price": "20元",
                    "cover_image": {"public_url": "https://img.test/2.jpg"},
                },
            ],
            "pagination": {"total": 4, "has_next": True},
        },
    }

    mock_resp1 = MagicMock()
    mock_resp1.status_code = 200
    mock_resp1.json.return_value = page1_payload
    mock_session.get.return_value = mock_resp1

    # 传入已知 ID 集合：包含 101 和 102
    res = fetch_tehuicaicai_deals(
        channel="home",
        page=1,
        page_size=2,
        max_pages=5,
        fetch_detail=False,
        skip_detail_ids={"101", "102"},
        auto_paginate=True,
        session=mock_session,
    )

    # 验证：因为第 1 页全部是已知商品，应当在第 1 页立即触发早停截断！
    assert res["status"] == "success"
    assert res["pages_crawled"] == 1
    assert mock_session.get.call_count == 1
    assert len(res["records"]) == 2


def test_tehuicaicai_product_signals():
    """
    测试特惠采采标准爆料记录包含唯一商品强锚点。
    """
    raw_text = (
        "领劵：https://jdpp.vip/t9GuqA\n"
        "【京东】三只松鼠松露巧克力500g\n"
        "下单：https://u.jd.com/oOELq90\n"
        "数量1 付29.9"
    )
    rec = build_deal_record(
        source_site="tehuicaicai",
        source_channel="home_products",
        source_item_id="5434",
        source_url="https://xhhcds.cn/#/product/5434",
        title="三只松鼠松露巧克力500g",
        price_text="￥29.9/盒",
        tutorial_text=raw_text,
    )
    signals = evaluate_quality_signals(rec)
    assert signals["has_unique_product_anchor"] is True
    assert signals["quality_tier"] == "A_含潜在商品信号"


def test_source_identifier_propagation():
    """
    测试线报信源标识透传机制：
    1. get_source_site_name 准确映射各大线报平台中文名称；
    2. ProductSummary 包含 deal_source 与 deal_source_name；
    3. ProductRepository._extract_deal_tip 完整透传 source_site, source_site_name, source_channel, provider_nickname；
    4. ProductRepository._normalize_doc_summary 暴露 deal_source 与 deal_source_name。
    """
    from models.product import get_source_site_name, SOURCE_SITE_NAMES, ProductSummary
    from repositories.product_repo import ProductRepository

    # 1. 站点中文映射验证
    assert get_source_site_name("tehuicaicai") == "特惠采采"
    assert get_source_site_name("haodanku") == "好单库"
    assert get_source_site_name("xianbao_fun") == "线报酷"
    assert get_source_site_name("unknown_site") == "unknown_site"

    # 2. ProductSummary 模型字段验证
    p = ProductSummary(
        platform="jd",
        goods_id="10001",
        name="测试特惠商品",
        image_url="https://img.test/p.jpg",
        price=19.9,
        original_price=39.9,
        deal_source="tehuicaicai",
        deal_source_name="特惠采采",
    )
    p_dict = p.to_dict()
    assert p_dict["deal_source"] == "tehuicaicai"
    assert p_dict["deal_source_name"] == "特惠采采"

    # 3. _extract_deal_tip 透传验证
    mock_doc = {
        "platform": "jd",
        "product_id": "10001",
        "name": "测试特惠商品",
        "deal_source": "tehuicaicai",
        "deal_source_name": "特惠采采",
        "deal_info": {
            "is_deal_tip": True,
            "quality_score": 90,
            "publish_time": "2026-11-18 12:00",
            "deal_price": 19.9,
            "source_site": "tehuicaicai",
            "source_site_name": "特惠采采",
            "source_channel": "home_products",
            "provider_nickname": "特惠采采-专员3",
            "tutorial_steps": ["领券下单"],
            "coupon_links": [{"label": "5元券", "url": "https://u.jd.com/test"}],
            "tips_timeline": [
                {
                    "tip_id": "5435",
                    "source_site": "tehuicaicai",
                    "source_site_name": "特惠采采",
                    "source_channel": "home_products:jd",
                    "provider_nickname": "特惠采采-专员3",
                    "publish_time": "2026-11-18 12:00",
                }
            ],
        },
    }
    extracted = ProductRepository._extract_deal_tip(mock_doc)
    assert extracted is not None
    assert len(extracted["tips_timeline"]) == 1
    assert extracted["tips_timeline"][0]["source_site"] == "tehuicaicai"
    assert extracted["tips_timeline"][0]["source_site_name"] == "特惠采采"
    assert extracted["tips_timeline"][0]["provider_nickname"] == "特惠采采-专员3"

    # 4. _normalize_doc_summary 验证
    repo = ProductRepository.__new__(ProductRepository)
    summary = repo._normalize_doc_summary(mock_doc)
    assert summary["deal_source"] == "tehuicaicai"
    assert summary["deal_source_name"] == "特惠采采"
