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
    assert rec["publish_time"]  # 特惠采采拉取时统一更新为当前保鲜判定时间
    urls = [lk["url"] for lk in rec["extracted_links"]]
    assert "https://u.jd.com/oaNLA4B" in urls
    assert "https://u.jd.com/oaNj0X8" in urls


def test_tehuicaicai_full_sweep_pages_all_and_touches_known_without_detail_requests():
    """全量巡视：翻到末页、详情配额用完也不提前收工，已抓 ID 只回传 touch_ids 不重复抓详情。"""
    from mobile_web.services.deal_fetchers import fetch_tehuicaicai_deals

    pages = {
        "page=1": [
            {"id": 2001, "title": "已知商品A", "platform_code": "jd", "platform_name": "京东", "display_price": "9.9"},
            {"id": 2002, "title": "新商品B", "platform_code": "jd", "platform_name": "京东", "display_price": "19.9"},
        ],
        "page=2": [
            {"id": 2003, "title": "新商品C", "platform_code": "jd", "platform_name": "京东", "display_price": "29.9"},
        ],
    }
    url_count = {"n": 0}
    detail_called = []

    def fake_get(url, headers=None, timeout=10.0):
        resp = MagicMock()
        resp.status_code = 200
        if "home-products" in url:
            url_count["n"] += 1
            if "page=1" in url:
                resp.json.return_value = {"data": {"items": pages["page=1"],
                                                  "pagination": {"page": 1, "total": 3, "has_next": True}}}
            else:
                resp.json.return_value = {"data": {"items": pages["page=2"],
                                                  "pagination": {"page": 2, "total": 3, "has_next": False}}}
        else:
            detail_called.append(url)
            resp.json.return_value = {"data": {}}
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get

    res = fetch_tehuicaicai_deals(
        fetch_detail=True, max_details=0, detail_delay=0.0,
        skip_detail_ids={"2001"}, session=sess, full_sweep=True,
    )

    assert url_count["n"] == 2, "全量巡视必须翻到末页，不能因详情配额用完就停"
    assert res["pages_crawled"] == 2
    assert res["raw_list_count"] == 3
    assert res["touch_ids"] == ["2001"], "已抓 ID 只回传确认名单，不重新抓详情"
    assert detail_called == [], "详情配额为 0 时不应发起任何详情请求"

    # 对照组：关掉全量巡视则第一页即收工（保持原有增量语义，逛丢等反爬站仍走这条）
    url_count["n"] = 0
    res2 = fetch_tehuicaicai_deals(
        fetch_detail=True, max_details=0, detail_delay=0.0,
        skip_detail_ids={"2001"}, session=sess, full_sweep=False,
    )
    assert res2["pages_crawled"] == 1
    assert res2["touch_ids"] == []


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
    assert all(c.max_details >= 20 for c in active if c.site_key == "guangdiu")

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
    # 逛丢站内频道随机顺位触发：首个到期频道命中验证码熔断后，同轮其余 2 个频道立即 cooldown_skipped
    assert len(gd_calls) == 1
    assert gd_calls[0] in ("latest", "hots", "rank")
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
    assert len(gd_calls) == 1
    gd_tasks_2 = [t for t in out2["tasks"] if t["site_key"] == "guangdiu"]
    assert len(gd_tasks_2) == 3
    assert all(t["status"] == "cooldown_skipped" for t in gd_tasks_2)


def test_run_fetch_stage_supports_pure_list_fetcher_without_skip_detail_kwargs(monkeypatch, tmp_path):
    """新增好单库/折淘客/选单等不接收 skip_detail_ids/max_details 的纯列表抓取器时，不报错且能正确增量去重。"""
    from mobile_web.scripts import run_deal_pool_scheduler as dps
    from mobile_web.services import deal_pipeline as dp_mod
    from mobile_web.services.deal_channel_config import DealChannelConfig

    monkeypatch.setattr(dp_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(dp_mod, "STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(dp_mod, "LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setattr(dp_mod, "OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"))
    monkeypatch.setattr(dp_mod, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"))

    saved_batches = []
    known_ids = {"hdk_known_1"}

    def strict_pure_list_fetcher(min_id=1, session=None):
        return {
            "status": "success",
            "http_status": 200,
            "request_url": f"http://v2.api.haodanku.com/get_wire_report?min_id={min_id}",
            "raw_list_count": 2,
            "records": [
                {
                    "source_site": "haodanku",
                    "source_item_id": "hdk_known_1",
                    "title": "旧好单库线报",
                    "has_full_detail": True,
                    "extracted_links": [{"url": "https://item.jd.com/1.html"}],
                },
                {
                    "source_site": "haodanku",
                    "source_item_id": "hdk_new_2",
                    "title": "新好单库线报",
                    "has_full_detail": True,
                    "extracted_links": [{"url": "https://item.jd.com/2.html"}],
                },
            ],
        }

    monkeypatch.setattr(dps, "fetch_haodanku_wire_report", strict_pure_list_fetcher)

    repo = MagicMock()
    repo.get_existing_detailed_ids.side_effect = lambda site, limit=5000: set(known_ids) if site == "haodanku" else set()
    repo.generate_pool_summary.return_value = {"total_records": 1}

    def fake_save_batch(records, task_name=""):
        saved_batches.append((task_name, [r["source_item_id"] for r in records]))
        for r in records:
            known_ids.add(str(r["source_item_id"]))
        return {"inserted": len(records), "updated": 0}

    repo.save_batch.side_effect = fake_save_batch

    pure_ch = [
        DealChannelConfig(
            name="好单库_实时线报",
            site_key="haodanku",
            default_url="http://v2.api.haodanku.com/get_wire_report",
            fetcher_name="fetch_haodanku_wire_report",
            fetcher_kwargs={"min_id": 1},
            enabled=True,
            interval_seconds=180,
            sleep_after=0.0,
        )
    ]

    last_run_map = {}
    out1 = dps.run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=1,
        channels=pure_ch,
        channel_last_run=last_run_map,
    )
    assert out1["tasks"][0]["status"] == "success"
    assert out1["tasks"][0]["skipped_known"] == 1
    assert out1["tasks"][0]["fetched"] == 1
    assert saved_batches == [("好单库_实时线报", ["hdk_new_2"])]

    # 第二轮重拉同一列表时，两条均已入库去重
    last_run_map["好单库_实时线报"] = 0.0
    out2 = dps.run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=2,
        channels=pure_ch,
        channel_last_run=last_run_map,
    )
    assert out2["tasks"][0]["skipped_known"] == 2
    assert out2["tasks"][0]["fetched"] == 0


def test_run_fetch_stage_runs_sites_concurrently_and_shuffles_same_site(monkeypatch, tmp_path):
    """不同 site_key 在独立线程并行运行；同站点多频道严格串行且顺位随机轮转。"""
    import threading
    from mobile_web.scripts import run_deal_pool_scheduler as dps
    from mobile_web.services import deal_pipeline as dp_mod
    from mobile_web.services.deal_channel_config import DealChannelConfig

    monkeypatch.setattr(dp_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(dp_mod, "STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(dp_mod, "LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setattr(dp_mod, "OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"))
    monkeypatch.setattr(dp_mod, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"))

    # 1. 验证不同 site_key 真正并发（Barrier 必须双线程同时到达）
    barrier = threading.Barrier(2, timeout=2.0)
    seen_threads = {}

    def concurrent_fetcher(site="", **kwargs):
        seen_threads[site] = threading.get_ident()
        barrier.wait()
        return {
            "status": "success",
            "http_status": 200,
            "request_url": f"https://{site}.example.com",
            "raw_list_count": 0,
            "records": [],
        }

    monkeypatch.setattr(dps, "fetch_tehuicaicai_deals", concurrent_fetcher)
    monkeypatch.setattr(dps, "fetch_guangdiu_deals", concurrent_fetcher)

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 0, "updated": 0}
    repo.generate_pool_summary.return_value = {"total_records": 0}

    parallel_channels = [
        DealChannelConfig(
            name="站点A_主频",
            site_key="site_a",
            default_url="https://site_a.example.com",
            fetcher_name="fetch_tehuicaicai_deals",
            fetcher_kwargs={"site": "site_a"},
            sleep_after=0.0,
        ),
        DealChannelConfig(
            name="站点B_主频",
            site_key="site_b",
            default_url="https://site_b.example.com",
            fetcher_name="fetch_guangdiu_deals",
            fetcher_kwargs={"site": "site_b"},
            sleep_after=0.0,
        ),
    ]
    dps.run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=1,
        channels=parallel_channels,
        channel_last_run={},
    )
    assert seen_threads["site_a"] != seen_threads["site_b"]

    # 2. 验证同站点三榜严格串行且经多轮调用出现不止一种随机顺位
    active_gd_count = 0
    same_site_orders = []

    def serial_gd_fetcher(channel="", **kwargs):
        nonlocal active_gd_count
        active_gd_count += 1
        assert active_gd_count == 1
        same_site_orders[-1].append(channel)
        active_gd_count -= 1
        return {
            "status": "success",
            "http_status": 200,
            "request_url": f"https://guangdiu.com/{channel}",
            "raw_list_count": 0,
            "records": [],
        }

    monkeypatch.setattr(dps, "fetch_guangdiu_deals", serial_gd_fetcher)
    gd_channels = [
        DealChannelConfig(
            name=f"逛丢_{ch_id}",
            site_key="guangdiu",
            default_url=f"https://guangdiu.com/{ch_id}",
            fetcher_name="fetch_guangdiu_deals",
            fetcher_kwargs={"channel": ch_id},
            sleep_after=0.0,
        )
        for ch_id in ("latest", "hots", "rank")
    ]
    for cycle in range(15):
        same_site_orders.append([])
        dps.run_fetch_stage(
            repo=repo,
            session=MagicMock(),
            cycle_num=cycle + 1,
            channels=gd_channels,
            channel_last_run={},
        )

    assert all(sorted(order) == ["hots", "latest", "rank"] for order in same_site_orders)
    assert len({tuple(order) for order in same_site_orders}) > 1


def test_tehuicaicai_watermark_stops_when_page_all_known():
    """验证特惠采采水位线增量翻页：当当前页全部为已知商品时，确定性在第1页终止，不发多余网络请求。"""
    from mobile_web.services.deal_fetchers import fetch_tehuicaicai_deals

    requested_pages = []

    def fake_get(url, headers=None, timeout=10.0):
        resp = MagicMock()
        resp.status_code = 200
        if "page=1" in url:
            requested_pages.append(1)
            resp.json.return_value = {
                "code": "OK",
                "data": {
                    "items": [
                        {"id": 3001, "title": "已知旧物1", "platform_code": "jd", "platform_name": "京东", "display_price": "9.9"},
                        {"id": 3002, "title": "已知旧物2", "platform_code": "jd", "platform_name": "京东", "display_price": "19.9"},
                    ],
                    "pagination": {"page": 1, "total": 20, "has_next": True},
                },
            }
        elif "page=2" in url:
            requested_pages.append(2)
            resp.json.return_value = {"code": "OK", "data": {"items": [], "pagination": {"page": 2, "total": 20, "has_next": False}}}
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get

    res = fetch_tehuicaicai_deals(
        fetch_detail=True,
        max_details=10,
        detail_delay=0.0,
        skip_detail_ids={"3001", "3002"},
        session=sess,
        max_pages=5,
        auto_paginate=True,
    )

    assert res["status"] == "success"
    # 当第1页全为已知数据时，极简且确定性地在第1页早停，绝不向后发请求
    assert requested_pages == [1]
    assert len(res["records"]) == 0


def test_guangdiu_relative_gophp_button_is_unpacked_not_used_as_direct_link():
    """列表按钮给的是站内相对 go.php 时，必须交给 go.php 解包，不能当直达链入库。

    实网依据：逛丢首页/热门对部分无联盟链接的帖子（京东直采等）给出的按钮 href 就是
    `go.php?id=xxx`（相对、无域名），`_unwrap_known_redirect_url` 原样返回它，
    若直接采信就会把自家相对链当成"直达购买链"，清洗端剥离站内链后只剩店铺页，
    最终误判 ERR_EMPTY_PRODUCT_IDENTITY（如 30701581）。
    """
    from mobile_web.services.deal_fetchers import fetch_guangdiu_deals

    list_html = """
    <div class="gooditem">
        <a class="goodname" href="detail.php?id=201">相对链商品</a>
        <a class="rightmallname">京东</a>
        <span class="latesttime">3分钟前</span>
        <a class="innergototobuybtn" href="go.php?id=201">直达</a>
    </div>
    """
    called_urls = []

    def fake_get(url, headers=None, timeout=10.0):
        called_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if url == "https://guangdiu.com/":
            resp.text = list_html
        elif "detail.php?id=201" in url:
            resp.text = """
            <div class="dabstract" id="dabstract">
                <div>1. 店铺 <a href="https://shop.m.jd.com/mshop/gethomepage?venderid=1">某某旗舰店</a></div>
                <div>2. 实付 79.9 元</div>
            </div>
            """
        elif "go.php?id=201" in url:
            resp.text = "var target = 'https://item.jd.com/201.html';"
        else:
            resp.text = ""
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get

    res = fetch_guangdiu_deals(
        channel="latest",
        fetch_detail=True,
        max_details=5,
        detail_delay=0.0,
        session=sess,
    )

    assert res["status"] == "success"
    assert len(res["records"]) == 1
    # 相对 go.php 必须触发一次真正的解包请求
    assert any(u.startswith("https://guangdiu.com/go.php?id=201") for u in called_urls)
    urls = [lk["url"] for lk in res["records"][0]["extracted_links"]]
    assert "go.php?id=201" not in urls
    assert "https://item.jd.com/201.html" in urls




