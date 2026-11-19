# -*- coding: utf-8 -*-
"""
线报拉取模块重构回归测试：
1. 100% 确定非淘/京/拼平台过滤器（严格防误杀）
2. 统一详情与实质教程门禁（无详情/无教程/空壳单品绝不入库）
3. 特惠采采（xhhcds.cn）增量去重不覆盖、coupon_urls/purchase_url 补全、水位线早停
4. 逛丢（guangdiu.com）跳过已知/非支持平台不发多余请求、平衡提取 #dabstract、to.php/iguangdiu 解包、时间提取
5. 调度器与主入口配置中心（仅开启《线报监控来源.txt》2站4频道、按频道独立频率与限流/验证码冷却）
"""
from unittest.mock import MagicMock


def test_confirmed_unsupported_platform_filter_prevents_false_kills():
    from mobile_web.services.deal_fetchers import is_confirmed_unsupported_deal

    # 1. 明确非淘京拼平台必须过滤
    assert is_confirmed_unsupported_deal(platform_code="vip") is True
    assert is_confirmed_unsupported_deal(mall_name="美团") is True
    assert is_confirmed_unsupported_deal(mall_name="唯品会") is True
    assert is_confirmed_unsupported_deal(mall_name="亚马逊中国") is True
    assert is_confirmed_unsupported_deal(
        mall_name="",
        links=[{"url": "https://iguangdiu.com/to.php?u=https%3A%2F%2Fclick.meituan.com%2Ft%3Ft%3D9"}],
    ) is True

    # 2. 淘/京/拼平台必须放行
    assert is_confirmed_unsupported_deal(platform_code="jd") is False
    assert is_confirmed_unsupported_deal(platform_code="pdd") is False
    assert is_confirmed_unsupported_deal(platform_code="tb") is False
    assert is_confirmed_unsupported_deal(mall_name="京东自营") is False
    assert is_confirmed_unsupported_deal(mall_name="天猫超市") is False
    assert is_confirmed_unsupported_deal(mall_name="肯德基官方旗舰店", links=[{"url": "https://s.click.taobao.com/t?e=1"}]) is False

    # 3. 未知商城或未知第三方短链绝不能误杀
    assert is_confirmed_unsupported_deal(mall_name="某品牌官方商城", links=[{"url": "https://llipin.cn/3IVaPCR"}]) is False
    assert is_confirmed_unsupported_deal(mall_name="", links=[{"url": "https://u.jd.com/oONAYrQ"}]) is False


def test_tehuicaicai_skips_known_drops_shell_and_merges_clean_urls():
    from mobile_web.services.deal_fetchers import fetch_tehuicaicai_deals

    list_payload = {
        "code": "OK",
        "data": {
            "items": [
                {"id": 1001, "title": "已知商品 250ml/盒", "platform_code": "jd", "platform_name": "京东", "display_price": "9.9"},
                {"id": 1002, "title": "唯品会女装", "platform_code": "vip", "platform_name": "唯品会", "display_price": "99"},
                {"id": 1003, "title": "无详情空壳商品", "platform_code": "jd", "platform_name": "京东", "display_price": "19.9"},
                {"id": 1004, "title": "优质牛奶", "platform_code": "jd", "platform_name": "京东", "display_price": "29.9"},
            ],
            "pagination": {"page": 1, "total": 4, "has_next": False},
        },
    }

    detail_called = []

    def fake_get(url, headers=None, timeout=10.0):
        resp = MagicMock()
        resp.status_code = 200
        if "home-products" in url:
            resp.json.return_value = list_payload
        elif "/products/1003/" in url:
            detail_called.append("1003")
            resp.json.return_value = {
                "code": "OK",
                "data": {
                    "id": 1003,
                    "title": "无详情空壳商品",
                    "platform_code": "jd",
                    "material_info": {"command_text": "", "purchase_url": "", "coupon_urls": []},
                    "description_html": "",
                    "tutorials": [],
                },
            }
        elif "/products/1004/" in url:
            detail_called.append("1004")
            resp.json.return_value = {
                "code": "OK",
                "data": {
                    "id": 1004,
                    "title": "优质牛奶",
                    "platform_code": "jd",
                    "platform_name": "京东",
                    "updated_at": "2026-11-19T08:00:00Z",
                    "material_info": {
                        "command_text": "领劵：https://u.jd.com///oaNLA4B\n【京东】优质牛奶\n下单：https://u.jd.com/oaNj0X8\n数量1 付29.9",
                        "purchase_url": "https://u.jd.com/oaNj0X8",
                        "coupon_urls": ["https://u.jd.com/oaNLA4B"],
                    },
                },
            }
        else:
            detail_called.append(url)
            resp.status_code = 404
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get

    res = fetch_tehuicaicai_deals(
        fetch_detail=True,
        max_details=10,
        detail_delay=0.0,
        skip_detail_ids={"1001"},
        session=sess,
    )
    assert res["status"] == "success"
    # 1001(已知) 与 1002(唯品会) 均未发起详情请求；1003(空壳)被丢弃；仅保留 1004
    assert detail_called == ["1003", "1004"]
    assert res["skipped_known"] == 1
    assert res["skipped_unsupported"] == 1
    assert res["skipped_no_detail"] == 1
    assert len(res["records"]) == 1

    rec = res["records"][0]
    assert rec["source_item_id"] == "1004"
    assert rec["has_full_detail"] is True
    assert rec["publish_time"] == "2026-11-19T08:00:00Z"
    urls = [lk["url"] for lk in rec["extracted_links"]]
    assert "https://u.jd.com/oaNLA4B" in urls
    assert "https://u.jd.com/oaNj0X8" in urls


def test_guangdiu_filters_before_requests_parses_nested_dabstract_and_drops_image_only():
    from mobile_web.services.deal_fetchers import fetch_guangdiu_deals

    list_html = """
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=101">已知商品</a>
        <a class="rightmallname">京东</a>
        <span class="latesttime">5分钟前</span>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=102">海底捞代金券</a>
        <a class="rightmallname">美团</a>
        <a class="innergototobuybtn" href="https://iguangdiu.com/to.php?u=https%3A%2F%2Fclick.meituan.com%2Ft%3Ft%3D9">直达</a>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=103">未知商家实为亚马逊</a>
        <a class="rightmallname"></a>
        <a class="innergototobuybtn" href="https://iguangdiu.com/to.php?u=https%3A%2F%2Fwww.amazon.cn%2Fdp%2FB001">直达</a>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=104">纯图无文字空壳</a>
        <a class="rightmallname">天猫</a>
    </div>
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=105">某品牌好物</a>
        <a class="rightmallname">神秘店铺</a>
        <span class="latesttime">1分钟前</span>
    </div>
    """

    called_urls = []

    def fake_get(url, headers=None, timeout=10.0):
        called_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if url == "https://guangdiu.com/":
            resp.text = list_html
        elif "detail.php?id=104" in url:
            # 只有封面图 div，没有教程文字 -> 必须丢弃且不得请求 go.php?id=104
            resp.text = """
            <div class="dabstract" id="dabstract">
                <div class="simgahead"><img src="https://img.test/cover.jpg"/></div>
            </div>
            """
        elif "detail.php?id=105" in url:
            # 含嵌套 div 且无 dgotobutton，检验平衡 div 提取 & 时间提取
            resp.text = """
            <span class="dinfoweibo"><span class="latesttime">刚刚</span></span>
            <div class="dabstract" id="dabstract">
                <div class="simgahead"><img src="https://img.test/105.jpg"/></div>
                <div class="gd-smzdm-buy-steps">
                    <div>1. 领取满100减40券</div>
                    <div>2. 下单实付59元 <a href="https://iguangdiu.com/to.php?u=https%3A%2F%2Fitem.jd.com%2F105.html">加购</a></div>
                </div>
            </div>
            <div class="clear"></div>
            """
        elif "go.php?id=105" in url:
            resp.text = "var target = 'https://union-click.jd.com/jdc?e=valid105';"
        else:
            resp.text = ""
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get

    res = fetch_guangdiu_deals(
        channel="latest",
        fetch_detail=True,
        max_details=10,
        detail_delay=0.0,
        skip_detail_ids={"101"},
        session=sess,
    )
    assert res["status"] == "success"
    assert res["skipped_known"] == 1
    assert res["skipped_unsupported"] == 2  # 102(美团) + 103(amazon.cn)
    assert res["skipped_no_detail"] == 1    # 104(纯图空壳)
    # 101/102/103 未发任何 detail/go 请求；104 未发 go.php 请求
    assert not any("id=101" in u or "id=102" in u or "id=103" in u for u in called_urls)
    assert not any("go.php?id=104" in u for u in called_urls)
    assert len(res["records"]) == 1

    rec = res["records"][0]
    assert rec["source_item_id"] == "105"
    assert rec["publish_time"] == "刚刚"
    assert "领取满100减40券" in rec["tutorial_text"]
    urls = [lk["url"] for lk in rec["extracted_links"]]
    assert "https://union-click.jd.com/jdc?e=valid105" in urls
    assert "https://item.jd.com/105.html" in urls


def test_parse_publish_time_epoch_relative_and_china_tz():
    import time
    from mobile_web.repositories.deal_raw_pool_repo import parse_publish_time_epoch

    now = time.time()
    ts_just = parse_publish_time_epoch("刚刚")
    assert ts_just is not None and abs(ts_just - now) < 5
    ts_sec = parse_publish_time_epoch("15秒前")
    assert ts_sec is not None and 10 <= (now - ts_sec) <= 25
    ts_min = parse_publish_time_epoch("5分钟前")
    assert ts_min is not None and 290 <= (now - ts_min) <= 315
    assert parse_publish_time_epoch("2026-11-19 14:30:00") == parse_publish_time_epoch("2026-11-19T06:30:00Z")


def test_scheduler_config_and_captcha_cooldown_behavior(monkeypatch, tmp_path):
    from mobile_web.scripts import run_deal_pool_scheduler as dps
    from mobile_web.services import deal_pipeline as dp_mod
    from mobile_web.services.deal_channel_config import get_active_channels

    # 1. 验证主入口仅启用《线报监控来源.txt》要求的 2 站 4 频道
    active = get_active_channels(dps.MONITORED_CHANNELS)
    assert [c.name for c in active] == [
        "特惠采采_首页精选",
        "逛丢_首页实时流",
        "逛丢_热门榜",
        "逛丢_小时风云榜",
    ]
    assert all(c.site_key in ("tehuicaicai", "guangdiu") for c in active)

    # 2. 验证逛丢触发 CaptchaChallengeDetected 后：同轮后续频道冷却跳过 + 跨轮进入站点冷却期，且特惠采采不受影响
    monkeypatch.setattr(dp_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(dp_mod, "STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(dp_mod, "LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setattr(dp_mod, "OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"))
    monkeypatch.setattr(dp_mod, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"))

    gd_calls = []

    def fake_gd_captcha(**kwargs):
        gd_calls.append(kwargs.get("channel"))
        return {
            "status": "error",
            "http_status": 200,
            "request_url": "https://guangdiu.com/",
            "raw_list_count": 10,
            "count": 0,
            "detail_fetched": 0,
            "rate_limited": True,
            "detail_errors": [{
                "item_id": "1",
                "error_url": "https://guangdiu.com/detail.php?id=1",
                "http_status": 200,
                "error_type": "CaptchaChallengeDetected",
                "error": "滑块验证",
            }],
            "records": [],
        }

    def fake_th_ok(**kwargs):
        return {
            "status": "success",
            "http_status": 200,
            "request_url": "https://xhhcds.cn/",
            "raw_list_count": 50,
            "count": 0,
            "detail_fetched": 0,
            "skipped_known": 50,
            "rate_limited": False,
            "detail_errors": [],
            "records": [],
        }

    monkeypatch.setattr(dps, "fetch_guangdiu_deals", fake_gd_captcha)
    monkeypatch.setattr(dps, "fetch_tehuicaicai_deals", fake_th_ok)

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 0, "updated": 0}
    repo.generate_pool_summary.return_value = {"total_records": 0}

    cooldown_map = {}
    last_run_map = {}
    health_tracker = {}
    out1 = dps.run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=1,
        channels=dps.MONITORED_CHANNELS,
        channel_last_run=last_run_map,
        site_cooldown_until=cooldown_map,
        channel_health_tracker=health_tracker,
    )
    # 逛丢仅首页发起过 1 次请求即触发熔断，热门榜与小时榜立即 cooldown_skipped
    assert gd_calls == ["latest"]
    assert cooldown_map.get("guangdiu", 0) > 0
    # 特惠采采返回 50 条全已抓过 (records=[])，状态为健康 success，不误报 empty_warning
    th_task = next(t for t in out1["tasks"] if t["site_key"] == "tehuicaicai")
    assert th_task["status"] == "success"
    assert th_task["consecutive_failures"] == 0

    # 3. 模拟特惠采采到点但逛丢仍在冷却期 -> 第 2 轮只跑特惠采采，逛丢全部 cooldown_skipped
    last_run_map["特惠采采_首页精选"] = 0.0
    last_run_map["逛丢_首页实时流"] = 0.0
    last_run_map["逛丢_热门榜"] = 0.0
    last_run_map["逛丢_小时风云榜"] = 0.0
    out2 = dps.run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=2,
        channels=dps.MONITORED_CHANNELS,
        channel_last_run=last_run_map,
        site_cooldown_until=cooldown_map,
        channel_health_tracker=health_tracker,
    )
    assert gd_calls == ["latest"]
    gd_tasks_2 = [t for t in out2["tasks"] if t["site_key"] == "guangdiu"]
    assert len(gd_tasks_2) == 3
    assert all(t["status"] == "cooldown_skipped" for t in gd_tasks_2)
