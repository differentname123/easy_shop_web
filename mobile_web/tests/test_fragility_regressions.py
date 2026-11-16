# -*- coding: utf-8 -*-
"""针对全栈脆弱点重构（Batches 1, 2, 4）的专项回归测试。"""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MOBILE_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_MOBILE_WEB_DIR)
for _p in (_MOBILE_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_evict_product_from_caches_clears_5tuple_product_ref_keys():
    """验证下架驱逐能够精准清除 ProductRef.cache_keys 生成的 5 元组 DETAIL_CACHE 键。"""
    import cache
    from models.product import ProductRef

    cache.DETAIL_CACHE.clear()
    ref = ProductRef(platform="pdd", goods_id="998877", goods_sign="sign_998877")
    for k in ref.cache_keys(uid="user_1"):
        cache.DETAIL_CACHE.set(k, {"goods_id": "998877"})
    # 同时放入一个其他商品的 5 元组缓存，确保不误删
    other_ref = ProductRef(platform="pdd", goods_id="112233", goods_sign="sign_112233")
    for k in other_ref.cache_keys(uid="user_1"):
        cache.DETAIL_CACHE.set(k, {"goods_id": "112233"})

    cache.evict_product_from_caches("pdd", goods_id="998877", goods_sign="sign_998877")
    for k in ref.cache_keys(uid="user_1"):
        assert k not in cache.DETAIL_CACHE
    for k in other_ref.cache_keys(uid="user_1"):
        assert k in cache.DETAIL_CACHE
    assert cache.is_product_off_promotion("pdd", goods_id="998877") is True


def test_parse_remaining_seconds_compound_chinese_duration():
    """验证中文复合倒计时（如 '5小时30分'）不再因 \\w 吞吃中文字符而丢失小时部分。"""
    from models.group_help import parse_remaining_seconds

    assert parse_remaining_seconds("5小时30分") == 5 * 3600 + 30 * 60
    assert parse_remaining_seconds("剩余2小时15分10秒") == 2 * 3600 + 15 * 60 + 10
    assert parse_remaining_seconds("1h30m") == 5400
    assert parse_remaining_seconds("45分钟") == 2700


def test_unified_llm_json_extraction_strips_think_blocks():
    """验证 <think>/<thought> 内包含伪 JSON 片段时，三处提取器均能准确提取正文 JSON。"""
    from infrastructure.ai_api.model_api import extract_llm_json
    from services.group_help_service import extract_json_object
    from mobile_web.scripts.goods_info_format import string_to_object

    raw_dict_output = (
        '<think>先分析一下：如果返回 {"is_valid": false, "fake": 1} 会怎样？不行，再想想。</think>\n'
        '```json\n{"is_valid": true, "real_answer": 42}\n```'
    )
    assert extract_llm_json(raw_dict_output) == {"is_valid": True, "real_answer": 42}
    assert extract_json_object(raw_dict_output) == {"is_valid": True, "real_answer": 42}
    assert string_to_object(raw_dict_output) == {"is_valid": True, "real_answer": 42}

    raw_list_output = (
        '<thought>草稿：[{"sku_id": "wrong"}]</thought>\n'
        '[{"sku_id": "100_1", "price": 19.9}]'
    )
    assert extract_llm_json(raw_list_output) == [{"sku_id": "100_1", "price": 19.9}]
    assert string_to_object(raw_list_output) == [{"sku_id": "100_1", "price": 19.9}]


def test_product_repo_uppercase_g_maps_to_mass_not_storage():
    """验证大写 'G' 单位归一为质量（克=1000mg），消除与数据存储 GB 的键冲突。"""
    from repositories.product_repo import UNIT_TO_CATEGORY, UNIT_TO_MULTIPLIER

    assert UNIT_TO_CATEGORY["G"] == "质量"
    assert UNIT_TO_MULTIPLIER["G"] == 1000
    assert UNIT_TO_CATEGORY["g"] == "质量"
    assert UNIT_TO_MULTIPLIER["g"] == 1000
    assert UNIT_TO_CATEGORY["GB"] == "数据存储"


def test_user_repo_register_or_login_handles_concurrent_duplicate_key():
    """验证并发注册同一手机号触发 DuplicateKeyError 时：同密码幂等登录，异密码报明确业务错误。"""
    from auth import hash_password
    from repositories.user_repo import UserRepository

    pwd_hash = hash_password("secret123")
    existing_doc = {
        "uid": "u_existing",
        "phone": "13900001111",
        "password_hash": pwd_hash,
        "nickname": "老用户",
        "invite_code": "INV_888888",
    }

    col = MagicMock()
    # 第一次 get_user_by_phone 返回 None（模拟并发竞态窗口），insert_one 抛出唯一索引冲突，
    # 随后并发恢复查询 get_user_by_phone 返回已插入的文档
    col.find_one.side_effect = [
        None,                # initial phone lookup
        None,                # generate_unique_invite_code check
        dict(existing_doc),  # recovery phone lookup after DuplicateKeyError
    ]
    col.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error phone_1")

    db = MagicMock()
    db.get_collection.return_value = col
    db.__getitem__.return_value = col
    repo = UserRepository(db)

    user_ok = repo.register_or_login("13900001111", "secret123")
    assert user_ok["uid"] == "u_existing"
    assert "password_hash" not in user_ok

    # 密码不一致时必须拒绝登录
    col.find_one.side_effect = [None, None, dict(existing_doc)]
    with pytest.raises(ValueError, match="密码"):
        repo.register_or_login("13900001111", "wrong_pass")


def test_group_help_upload_rejects_oversized_content_length_with_413():
    """验证拼团海报上传接口在读取流之前拦截超过 15MB 的请求并返回 413。"""
    import main

    client = TestClient(main.app)
    resp = client.post(
        "/api/group_help/upload",
        content=b"tiny",
        headers={"Content-Length": str(16 * 1024 * 1024)},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["error_code"] == "payload_too_large"


def test_tb_client_never_clobbers_config_when_local_cfg_corrupt(monkeypatch):
    """验证淘宝刷新 Session 时优先遵循 WEB_CONFIG_FILE，且绝不用空字典覆盖损坏的原配置文件。"""
    from infrastructure import tb_client

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "custom_cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("{corrupt json")

        monkeypatch.setenv("WEB_CONFIG_FILE", cfg_path)
        monkeypatch.setenv("TB_REFRESH_TOKEN", "env_ref_tok")

        def _fake_api_call(**kwargs):
            return {
                "status": "success",
                "data": {
                    "top_auth_token_refresh_response": {
                        "token_result": json.dumps({"access_token": "new_sess_tok"})
                    }
                },
            }

        res = tb_client.reload_or_refresh_tb_credentials(
            app_key="k",
            app_secret="s",
            current_session="old_sess",
            _api_call=_fake_api_call,
        )
        assert res["session_key"] == "new_sess_tok"
        with open(cfg_path, "r", encoding="utf-8") as f:
            assert f.read() == "{corrupt json"


def test_jd_client_pace_backlog_fast_fails_instead_of_blocking(monkeypatch):
    """验证京东单 Key 排队积压超过阈值时立即返回 UPSTREAM_ERROR，避免阻塞 Web 线程池。"""
    from infrastructure.jd_client import JdClient

    client = JdClient(appkey_list=["busy_key"], union_id="100001")
    monkeypatch.setattr(JdClient, "_KEY_NEXT_ALLOWED_TS", {"busy_key": 1000.0})
    monkeypatch.setattr("infrastructure.jd_client.time.monotonic", lambda: 100.0)

    res = client._get("https://api.zhetaoke.com:10001/api/open_jing_union_open_goods_jingfen_query.ashx", params={"appkey": "busy_key"})
    assert res["status"] == "error"
    assert res["error_code"] == "UPSTREAM_ERROR"


def test_goods_info_format_minor_float_drift_and_skip_unchanged_success():
    """验证微小浮点尾差自动对齐，且已完成未变动的商品在 run_format_round 中零写库直接跳过。"""
    from mobile_web.scripts import goods_info_format as gif

    item = {
        "sku_id": "100_1",
        "price": 19.89,
        "core_entities": [{"name": "抽纸", "score": 10}],
        "decision_keywords": [{"attribute_name": "包数", "attribute_value": "24包", "score": 9}],
        "pricing_basis": {
            "is_inferred": False,
            "structure": [{"value": 24, "unit": "包"}],
            "total_value": 24,
            "base_unit": "包",
            "equivalent_description": None,
        },
    }
    valid, results, item_errors, _ = gif.check_format_info([item], ["100_1"], expected_prices={"100_1": 19.9})
    assert valid is True
    assert item_errors == {}
    assert results["100_1"]["price"] == 19.9

    # 真实错位价格必须拦截
    item_bad = dict(item, price=22.5)
    valid_bad, _, errors_bad, _ = gif.check_format_info([item_bad], ["100_1"], expected_prices={"100_1": 19.9})
    assert valid_bad is False
    assert "100_1" in errors_bad

    # 已成功清洗且价格未变的商品应零写库跳过
    unchanged_doc = {
        "_id": "oid_done",
        "platform": "pdd",
        "product_id": "100",
        "name": "维达抽纸24包",
        "activity_price": 19.9,
        "sku_info": [{"款式": "24包", "价格": "￥19.9"}],
        "format_status": "success",
        "format_sku_prices": {"100_1": "￥19.9"},
        "format_info": {"100_1": item},
    }
    mgr = MagicMock()
    mgr.query.return_value = [unchanged_doc]
    counts = gif.run_format_round(mgr)
    assert counts["success"] == 0
    assert counts["skipped"] == 0
    mgr.update.assert_not_called()


def test_deal_pool_parse_publish_time_epoch_china_tz_consistency():
    """验证东八区无时区字符串与显式 UTC Z 字符串解析结果严格一致，消除容器 UTC 8 小时漂移。"""
    from mobile_web.scripts.run_deal_pool_scheduler import parse_publish_time_epoch

    ts_cn = parse_publish_time_epoch("2026-11-12 14:30:00")
    ts_utc = parse_publish_time_epoch("2026-11-12T06:30:00Z")
    ts_iso_offset = parse_publish_time_epoch("2026-11-12T14:30:00+08:00")
    assert ts_cn is not None
    assert ts_cn == ts_utc == ts_iso_offset


def test_xianyu_discovery_transient_llm_outage_never_blacklists_candidate(monkeypatch):
    """验证闲鱼比价 Step 1 大模型网关故障时标记 llm_aborted=True，且绝不写入 xianyu_deal_evaluations 永久拉黑。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ModuleNotFoundError:
        import types
        pw_mod = types.ModuleType("playwright")
        pw_sync = types.ModuleType("playwright.sync_api")
        pw_sync.sync_playwright = MagicMock()
        sys.modules.setdefault("playwright", pw_mod)
        sys.modules.setdefault("playwright.sync_api", pw_sync)

    from mobile_web.scripts import run_xianyu_deal_discovery_scheduler as sched

    cand = {
        "source_type": sched.SOURCE_TYPE_OFFICIAL,
        "platform": "pdd",
        "product_id": "888001",
        "goods_sign": "sign_888001",
        "channel_type": 1,
        "category_name": "抽纸",
        "category_item_count": 5,
        "name": "维达抽纸整箱24包",
        "outer_name": "",
        "brand": "维达",
        "db_activity_price": 19.9,
        "activity_price": 19.9,
        "original_price": 29.9,
        "primary_unit": "包",
        "unit_quantities": {"包": 24.0},
        "unit_cost_performance": {"包": round(24.0 / 19.9, 6)},
        "decision_keywords_summary": "品牌:维达",
        "pricing_basis": {"base_unit": "包", "total_value": 24.0},
        "search_keyword": "",
        "backup_search_keyword": "",
        "selection_reason": "",
        "llm_keyword_output": {},
    }

    # 模拟大模型网关超时失败
    monkeypatch.setattr(
        sched,
        "_call_llm_json",
        lambda **kwargs: (False, {}, {"model_used": "mock", "attempt_used": 3}, "504 Gateway Timeout"),
    )

    rec = sched.compare_candidate_with_xianyu(
        candidate=cand,
        search_user_data_dir="dummy_search",
        guest_user_data_dir="dummy_guest",
    )
    assert rec["final_winner"] == "llm_error"
    assert rec["xianyu_audit_trail"]["llm_aborted"] is True
    assert rec["xianyu_audit_trail"]["ineligible"] is False

    # 验证 run_discovery_cycle 遇到 llm_aborted 时跳过 eval_col.update_one，保留下轮重试机会
    fake_eval_col = MagicMock()
    fake_items_col = MagicMock()
    fake_db = MagicMock()
    fake_db.get_collection.side_effect = lambda name: (
        fake_eval_col if name == sched.EVAL_COLLECTION_NAME else fake_items_col
    )
    monkeypatch.setattr(sched, "gen_db_object", lambda: fake_db)
    monkeypatch.setattr(
        sched,
        "select_round_candidates_for_source",
        lambda **kwargs: [cand] if kwargs.get("source_type") == sched.SOURCE_TYPE_OFFICIAL else [],
    )

    records = sched.run_discovery_cycle(max_categories=1)
    assert len(records) == 1
    assert records[0]["xianyu_audit_trail"]["llm_aborted"] is True
    fake_eval_col.update_one.assert_not_called()


def test_xianbao_detail_loop_circuit_breaks_on_429_and_caps_attempts(monkeypatch):
    """验证详情抓取遇到 429/502 立即熔断不再硬撞整页，且遇到 404 也严格按尝试次数封顶在 max_details。"""
    from mobile_web.scripts import test_deal_sites_scraper as scraper

    monkeypatch.setattr(scraper.time, "sleep", lambda _: None)

    li_items = "".join(
        f'<li class="article-list"><a href="/haodan/{1000 + i}.html" data-catename="京东" data-price="9.9">'
        f"测试线报商品 #{i}</a></li>"
        for i in range(25)
    )
    list_html = f"<ul>{li_items}</ul>"

    # 场景 1：第 1 条 200，第 2 条触发 429 -> 立即熔断，绝不继续轰炸后续 23 条详情页
    calls_429 = []

    def fake_get_429(url, headers=None, timeout=10.0):
        calls_429.append(url)
        resp = MagicMock()
        if url.endswith("haodan-hot.html"):
            resp.status_code = 200
            resp.text = list_html
        elif len(calls_429) == 2:
            resp.status_code = 200
            resp.text = '<div class="article-content"><a href="https://u.jd.com/abc123">直达</a></div><div id="foot"></div>'
        else:
            resp.status_code = 429
            resp.text = "Too Many Requests"
        return resp

    sess_429 = MagicMock()
    sess_429.get.side_effect = fake_get_429

    res_429 = scraper.fetch_xianbao_fun_deals(
        channel="haodan_hot",
        fetch_detail=True,
        max_details=15,
        session=sess_429,
    )
    # 1 次列表请求 + 1 次成功详情 + 1 次 429 熔断 = 总计仅 3 次 HTTP 请求（而非 26 次）
    assert len(calls_429) == 3
    assert res_429["count"] == 25
    assert res_429["detail_fetched"] == 1
    assert len(res_429["detail_errors"]) == 1
    assert res_429["detail_errors"][0]["http_status"] == 429

    # 场景 2：全部返回 404（非限流错误）时，也严格按 max_details=4 次尝试封顶，绝不穿透整页 25 条
    calls_404 = []

    def fake_get_404(url, headers=None, timeout=10.0):
        calls_404.append(url)
        resp = MagicMock()
        if url.endswith("haodan-hot.html"):
            resp.status_code = 200
            resp.text = list_html
        else:
            resp.status_code = 404
            resp.text = "Not Found"
        return resp

    sess_404 = MagicMock()
    sess_404.get.side_effect = fake_get_404

    res_404 = scraper.fetch_xianbao_fun_deals(
        channel="haodan_hot",
        fetch_detail=True,
        max_details=4,
        session=sess_404,
    )
    assert len(calls_404) == 1 + 4
    assert res_404["detail_fetched"] == 0
    assert len(res_404["detail_errors"]) == 4


def test_run_fetch_stage_short_circuits_same_site_after_rate_limit(monkeypatch, tmp_path):
    """验证同源站点在单轮内触发 429 后，后续同站点频道直接 cooldown_skipped 冷却跳过，且错误路径按频道聚合。"""
    from mobile_web.scripts import run_deal_pool_scheduler as dps

    monkeypatch.setattr(dps.time, "sleep", lambda _: None)
    monkeypatch.setattr(dps, "STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(dps, "LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"))
    monkeypatch.setattr(dps, "OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"))
    monkeypatch.setattr(dps, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"))

    xianbao_calls = []

    def fake_xianbao(**kwargs):
        xianbao_calls.append(kwargs.get("channel"))
        return {
            "status": "success",
            "http_status": 200,
            "request_url": "https://new.xianbao.fun/test",
            "count": 2,
            "detail_fetched": 0,
            "detail_errors": [
                {"item_id": "1", "error_url": "https://new.xianbao.fun/1.html", "http_status": 429, "error_type": "HTTP_429", "error": "429"},
                {"item_id": "2", "error_url": "https://new.xianbao.fun/2.html", "http_status": 429, "error_type": "HTTP_429", "error": "429"},
            ],
            "records": [{"source_site": "xianbao_fun", "source_item_id": "1", "has_full_detail": False}],
        }

    ok_stub = lambda **kwargs: {
        "status": "success",
        "http_status": 200,
        "request_url": "https://example.com/ok",
        "count": 1,
        "detail_fetched": 1,
        "detail_errors": [],
        "records": [{"source_site": "haodanku", "source_item_id": "h1", "has_full_detail": True, "extracted_links": [{"url": "https://u.jd.com/x"}]}],
    }

    monkeypatch.setattr(dps, "fetch_xianbao_fun_deals", fake_xianbao)
    monkeypatch.setattr(dps, "fetch_haodanku_wire_report", ok_stub)
    monkeypatch.setattr(dps, "fetch_zhetaoke_xianbao_groups", ok_stub)
    monkeypatch.setattr(dps, "fetch_guangdiu_deals", ok_stub)
    monkeypatch.setattr(dps, "fetch_xuandan_deals", ok_stub)
    monkeypatch.setattr(dps, "fetch_0818tuan_deals", ok_stub)
    monkeypatch.setattr(dps, "fetch_bacaoo_deals", ok_stub)
    monkeypatch.setattr(dps, "fetch_iehou_deals", ok_stub)

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 1, "updated": 0}
    repo.generate_pool_summary.return_value = {"total_records": 1}

    out = dps.run_fetch_stage(repo=repo, session=MagicMock(), cycle_num=1, run_all_tiers=False)
    # T0 共 7 个 xianbao_fun 频道：第 1 个触发 429 后，其余 6 个全部 cooldown_skipped，不再发请求
    assert len(xianbao_calls) == 1
    xb_tasks = [t for t in out["tasks"] if t["site_key"] == "xianbao_fun"]
    assert len(xb_tasks) == 7
    assert sum(1 for t in xb_tasks if t["status"] == "cooldown_skipped") == 6
    # 2 条 detail_errors 聚合成单条频道告警记录
    assert len(out["error_paths"]) == 1
    assert "共 2 条详情异常" in out["error_paths"][0]["error_message"]


def test_universal_convert_deleted_user_falls_back_to_station_master_floor(monkeypatch):
    """回归验证：万能转链在推手账号被删或个人转链失败时，必须自动回退站长默认推广位保底（绝不白白丢单）。"""
    from services import promote_service as ps_mod

    class FakeJdUniversalAdapter:
        active = True
        platform_code = "jd"
        platform_name = "京东"
        requires_numeric_promoter_id = True
        requires_authorized_promotion = False
        default_promoter_id = "3110104129"
        promotion_scope = "jd-scope"

        def __init__(self):
            self.calls = []

        def build_command_text(self, title, final_link, price=None, coupon=None):
            return f"{title} {final_link}"

        def resolve_and_convert_universal(self, text, uid=None, **kwargs):
            self.calls.append({"uid": uid, **kwargs})
            if kwargs.get("promoter_id") == "9999":
                return {"status": "error", "error_msg": "personal position failed"}
            return {
                "status": "success",
                "short_url": "https://u.jd.com/station_floor",
                "final_link": "https://u.jd.com/station_floor",
                "promotion_token": "！站长保底京口令！",
                "promoter_id": kwargs["promoter_id"],
                "attribution_status": "verified" if uid else "anonymous",
                "title": "京东测试商品",
                "price": 19.9,
            }

    adapter = FakeJdUniversalAdapter()
    users = MagicMock()
    users.get_numeric_promotion_context.return_value = {
        "status": "error",
        "error_code": "promoter_identity_unavailable",
        "error_msg": "推手用户不存在",
    }
    monkeypatch.setattr(ps_mod, "get_adapter", lambda plat: adapter)
    svc = ps_mod.PromoteService(product_repo=MagicMock(), user_service=users)

    # 1. 推手已被删库 -> 直接切换站长推广位保底成功
    res1 = svc.universal_convert("https://u.jd.com/abcDEF", platform="jd", uid="u_57f51d4c")
    assert res1["status"] == "success"
    assert res1["final_link"] == "https://u.jd.com/station_floor"
    assert res1["is_fallback"] is True and res1["attribution_status"] == "anonymous" and res1["cacheable"] is False
    assert adapter.calls[-1] == {"uid": None, "promoter_id": "3110104129"}

    # 2. 推手个人推广位上游生链失败 -> 自动重试站长推广位保底成功
    users.get_numeric_promotion_context.return_value = {"status": "success", "promoter_id": "9999"}
    res2 = svc.universal_convert("https://u.jd.com/abcDEF", platform="jd", uid="u_57f51d4c")
    assert res2["status"] == "success"
    assert res2["is_fallback"] is True and res2["attribution_status"] == "anonymous"
    assert [c["promoter_id"] for c in adapter.calls[-2:]] == ["9999", "3110104129"]


def test_jd_detail_rate_limit_fallback_uses_resilient_enrich_and_never_logs_red_cross(monkeypatch, caplog):
    """回归验证：前台京东详情优先走 enrich_detail_resilient，且即使降级返回本地缓存也不打印误导性的红色 ❌。"""
    import logging
    from cache import clear_all_caches
    from models.product import ProductRef
    from services import product_service as prod_mod

    clear_all_caches()
    enc_sign = "MHQuhO536DtWOykdGM94WOykdGM94Q_3RTWqX1JrEJSAPxknW"
    gid = "6ad1ed9fFa252fa1e_093532032071e8dc"

    class FakeJdDetailAdapter:
        active = True
        platform_code = "jd"
        platform_name = "京东"
        capabilities = {"detail": True}

        def resolve_product_ref(self, raw="", goods_id=None, goods_sign=None):
            return ProductRef("jd", goods_id or gid, goods_sign or enc_sign)

        def enrich_detail_resilient(self, goods_id_or_sign="", goods_id=None, goods_sign=None):
            return {"status": "error", "error_code": "RATE_LIMIT", "error_msg": "京东详情接口触发临时频控"}

        def get_goods_detail(self, **kwargs):
            raise AssertionError("foreground JD detail should prefer enrich_detail_resilient")

    repo = MagicMock()
    repo.get_product_detail.return_value = {
        "platform": "jd",
        "goods_id": gid,
        "goods_sign": enc_sign,
        "name": "小草先生纯黑芝麻油500ml",
        "price": 9.99,
        "original_price": 9.99,
        "image_url": "https://img14.360buyimg.com/pop/jfs/t1/1/2/3/4/6ad1ed9fFa252fa1e/093532032071e8dc.png",
    }
    monkeypatch.setattr(prod_mod, "get_adapter", lambda plat: FakeJdDetailAdapter())
    svc = prod_mod.ProductService(product_repo=repo)

    with caplog.at_level(logging.INFO):
        res = svc.get_goods_detail(platform="jd", goods_id=gid, goods_sign=enc_sign)
    assert res["status"] == "success"
    assert res["source"] == "database_fallback"
    assert all("❌" not in rec.getMessage() for rec in caplog.records)
    clear_all_caches()


