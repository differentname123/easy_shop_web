# -*- coding: utf-8 -*-
"""
安全与回归单测：极简优惠券双门禁提取漏斗（零短链域名预设 + 100% 零他人佣金/红包外链泄露）
"""
from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

from models.deal_constants import ERR_AI2_COUPON_FAILED_OR_DROPPED
from services.deal_pool_cleaner import (
    DealPoolCleaner,
    _is_safe_public_http_url,
    _is_verified_station_promo_link,
    detect_candidate_platform,
    rebuild_clean_official_venue_url,
    unwrap_to_official_public_venue_url,
)


def _public_dns_resolver(host: str, port: int, *_args, **_kwargs):
    """离线确定性公网 DNS 解析桩（无真实网络请求）。"""
    private_map = {
        "internal.evil.test": "192.168.1.25",
        "metadata.evil.test": "169.254.169.254",
        "loopback.evil.test": "127.0.0.1",
    }
    ip = private_map.get(host.lower(), "93.184.216.34")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


class _FakeResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None, body: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._body_bytes = body.encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int = 4096):
        for idx in range(0, len(self._body_bytes), chunk_size):
            yield self._body_bytes[idx : idx + chunk_size]

    def close(self):
        self.closed = True


class _RouteSession:
    def __init__(self, routes: dict[str, _FakeResponse]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        resp = self.routes.get(url)
        if resp is None:
            return _FakeResponse(status_code=404, body="not found")
        return resp


def test_yuebai_plus_no_longer_hard_blocked_as_unsupported_platform():
    assert detect_candidate_platform("http://p.yuebai.plus/wNnv6GM", "jd") == "jd"
    assert detect_candidate_platform("http://w.yuebai.plus/lMX4moj", "unknown") == "unknown"
    # 真正的外站（如美团）依然被第一时间拦截
    assert detect_candidate_platform("https://click.meituan.com/t?t=1", "jd") == "unsupported_other"


def test_rebuild_clean_official_venue_url_blocks_alimama_super_red_packet_and_allows_prodev():
    # 1. 阿里妈妈双11超级红包落地页（含 /wow/z/lianmeng/ 或 wh_alimama=true）必须 100% 拦截
    for super_hb_url in (
        "https://pages.tmall.com/wow/z/lianmeng/default/route?budgetTag=2026012001&wh_alimama=true&es=otherPromoterLock",
        "https://pages-fast.m.taobao.com/wow/z/lianmeng/act/hb?wh_pid=daily-123",
        "https://pages.tmall.com/wow/a/act/tmall/dailygroup/123/wupr?wh_pid=daily-1&wh_alimama=true&es=locked",
    ):
        assert rebuild_clean_official_venue_url(super_hb_url) is None

    # 2. 正常官方公共领券/消费券会场页：保留白名单必要参数，彻底剥离追踪与返佣参数
    assert (
        rebuild_clean_official_venue_url(
            "https://coupon.m.jd.com/coupons/show.action?key=k123&roleId=r456&zdmcode=bad&utm_source=x&future_track=99"
        )
        == "https://coupon.m.jd.com/coupons/show.action?key=k123&roleId=r456"
    )
    assert (
        rebuild_clean_official_venue_url(
            "https://uland.taobao.com/quan/detail?sellerId=2201&activityId=act88&pid=mm_99_88_77&relationId=123"
        )
        == "https://uland.taobao.com/quan/detail?sellerId=2201&activityId=act88"
    )
    assert (
        rebuild_clean_official_venue_url(
            "https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479&es=drop_me&spm=a21"
        )
        == "https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479"
    )
    assert (
        rebuild_clean_official_venue_url("https://pages-fast.m.taobao.com/wow/bz/jingmi/1323?ut_sk=track123")
        == "https://pages-fast.m.taobao.com/wow/bz/jingmi/1323"
    )

    # 3. prodev.m.jd.com 与 pro.m.jd.com 会场并列放行并 100% 清空 query 参数
    assert (
        rebuild_clean_official_venue_url(
            "https://pro.m.jd.com/mall/active/2eh7EHb/index.html?union_lens=xxx"
        )
        == "https://pro.m.jd.com/mall/active/2eh7EHb/index.html"
    )
    assert (
        rebuild_clean_official_venue_url(
            "https://prodev.m.jd.com/mall/active/3xh9KLM/index.html?utm_campaign=t_1001695162_&cu=true"
        )
        == "https://prodev.m.jd.com/mall/active/3xh9KLM/index.html"
    )

    # 4. plogin.m.jd.com 登录包装页自动剥壳还原纯净领券链接
    assert (
        rebuild_clean_official_venue_url(
            "https://plogin.m.jd.com/cgi-bin/m/tmauth?returnurl=https%3A%2F%2Fcoupon.m.jd.com%2Fcoupons%2Fshow.action%3Fkey%3Dk9%26roleId%3Dr9%26utm_source%3Dbad"
        )
        == "https://coupon.m.jd.com/coupons/show.action?key=k9&roleId=r9"
    )

    # 5. 短链、加密 CPS 页或缺失必要参数的残缺链接一律返回 None
    for rejected in (
        "https://s.click.taobao.com/XNl8wji",
        "https://m.tb.cn/h.jPi2tHp",
        "https://u.jd.com/oaDAVcO",
        "https://3.cn/3b-MaSLj",
        "https://uland.taobao.com/coupon/edetail?e=encrypted_cps",
        "https://uland.taobao.com/quan/detail?sellerId=2201",
    ):
        assert rebuild_clean_official_venue_url(rejected) is None


def test_unwrap_rescues_302_meta_js_and_taobao_tu_into_clean_official_urls():
    session = _RouteSession({
        # 1. llipin.cn 302 跳转至京东公共补贴券（附带外部追踪参数）
        "https://llipin.cn/hjA2NeT": _FakeResponse(
            status_code=302,
            headers={
                "Location": "https://coupon.m.jd.com/coupons/show.action?key=k_cola&roleId=r_cola&utm_source=kong&utm_campaign=t_999_"
            },
        ),
        # 2. yuebai.plus HTTP 301 跳转至京东 linkKey 券
        "http://p.yuebai.plus/wNnv6GM": _FakeResponse(
            status_code=301,
            headers={"Location": "//coupon.m.jd.com/coupons/show.action?linkKey=LK_GOOD_888&from=yb"},
        ),
        # 3. jdpp.vip meta refresh（content 在 http-equiv 之前）跳转至淘宝店铺暗券
        "https://jdpp.vip/tL4XY8": _FakeResponse(
            status_code=200,
            body='<html><head><meta content="0;url=https://uland.taobao.com/quan/detail?sellerId=8811&amp;activityId=ac99&amp;pid=mm_other_1_2" http-equiv="refresh"></head></html>',
        ),
        # 4. kurl07.cn 前端 JS 转义斜杠 location.replace 跳转至京东优惠券
        "https://kurl07.cn/tL9OSP": _FakeResponse(
            status_code=200,
            body=r'<script>window.location.replace("https:\/\/coupon.m.jd.com\/coupons\/show.action?key=k_js&roleId=r_js&ad_od=1");</script>',
        ),
        # 5. 手淘 s.tb.cn var url + tu= 二段跳还原天猫 88VIP 消费券会场
        "https://s.tb.cn/h.jimwfaJ": _FakeResponse(
            status_code=200,
            body=(
                "<script>var url = 'https://s.tb.cn/redirect?tu="
                "https%3A%2F%2Fpages.tmall.com%2Fwow%2Fa%2Fact%2Ftmall%2Fdailygroup%2F23288%2Fwupr"
                "%3Fwh_pid%3Ddaily-544479%26spm%3Da21.bad';</script>"
            ),
        ),
    })

    assert (
        unwrap_to_official_public_venue_url(
            "https://llipin.cn/hjA2NeT", http_session=session, resolver=_public_dns_resolver
        )
        == "https://coupon.m.jd.com/coupons/show.action?key=k_cola&roleId=r_cola"
    )
    # 命中白名单后立即返回，无需再向 coupon.m.jd.com 发起第 2 次 HTTP 请求
    assert len(session.calls) == 1

    assert (
        unwrap_to_official_public_venue_url(
            "http://p.yuebai.plus/wNnv6GM", http_session=session, resolver=_public_dns_resolver
        )
        == "https://coupon.m.jd.com/coupons/show.action?linkKey=LK_GOOD_888"
    )
    assert (
        unwrap_to_official_public_venue_url(
            "https://jdpp.vip/tL4XY8", http_session=session, resolver=_public_dns_resolver
        )
        == "https://uland.taobao.com/quan/detail?sellerId=8811&activityId=ac99"
    )
    assert (
        unwrap_to_official_public_venue_url(
            "https://kurl07.cn/tL9OSP", http_session=session, resolver=_public_dns_resolver
        )
        == "https://coupon.m.jd.com/coupons/show.action?key=k_js&roleId=r_js"
    )
    assert (
        unwrap_to_official_public_venue_url(
            "https://s.tb.cn/h.jimwfaJ", http_session=session, resolver=_public_dns_resolver
        )
        == "https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479"
    )


def test_unwrap_strictly_blocks_super_red_packets_jingfen_cps_external_sites_and_ssrf():
    session = _RouteSession({
        # 1. 他人淘宝超级红包 s.click -> real_jump_address -> /wow/z/lianmeng/
        "https://s.click.taobao.com/XNl8wji": _FakeResponse(
            status_code=200,
            body="<script>var real_jump_address = 'https://pages.tmall.com/wow/z/lianmeng/default/route?budgetTag=2026012001&wh_alimama=true&es=someone_else';</script>",
        ),
        # 2. 他人加密京粉锁佣券 u.jd.com -> hrl -> union-click -> jingfen.jd.com/item?u_act_p=coupon
        "https://u.jd.com/eOoAPas": _FakeResponse(
            status_code=200,
            body="<script>var hrl='https://union-click.jd.com/jdc?e=&p=AyIGZRpr';</script>",
        ),
        "https://union-click.jd.com/jdc?e=&p=AyIGZRpr": _FakeResponse(
            status_code=302,
            headers={"Location": "https://jingfen.jd.com/item?u_act_p=coupon&q=ENC&utm_campaign=t_1001695162_"},
        ),
        "https://jingfen.jd.com/item?u_act_p=coupon&q=ENC&utm_campaign=t_1001695162_": _FakeResponse(
            status_code=200,
            body="<html>京粉领券页</html>",
        ),
        # 3. 跳往美团外站
        "http://p.yuebai.plus/meituan_ad": _FakeResponse(
            status_code=302,
            headers={"Location": "https://click.meituan.com/t?t=1&c=2"},
        ),
        "https://click.meituan.com/t?t=1&c=2": _FakeResponse(status_code=200, body="meituan"),
        # 4. 第 2 跳企图 SSRF 重定向到内网/云元数据地址
        "https://short.public.test/ssrf_hop": _FakeResponse(
            status_code=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        ),
        "https://short.public.test/dns_rebind": _FakeResponse(
            status_code=302,
            headers={"Location": "https://internal.evil.test/coupon"},
        ),
    })

    for blocked_url in (
        "https://s.click.taobao.com/XNl8wji",
        "https://u.jd.com/eOoAPas",
        "http://p.yuebai.plus/meituan_ad",
        "https://short.public.test/ssrf_hop",
        "https://short.public.test/dns_rebind",
        "http://127.0.0.1/coupons/show.action?key=a&roleId=b",
        "http://192.168.1.1/coupons",
        "https://short.public.test:8080/abc",
    ):
        assert (
            unwrap_to_official_public_venue_url(
                blocked_url, http_session=session, resolver=_public_dns_resolver
            )
            is None
        ), f"Expected {blocked_url} to be strictly blocked!"

    # 验证 SSRF 地址从未被实际发起请求
    called_urls = [u for u, _ in session.calls]
    assert not any("169.254.169.254" in u or "internal.evil.test" in u or "127.0.0.1" in u for u in called_urls)
    assert _is_safe_public_http_url("http://localhost/test", resolver=_public_dns_resolver) is False
    assert _is_safe_public_http_url("http://metadata.evil.test/test", resolver=_public_dns_resolver) is False


def test_gate1_blocks_echo_back_spoofing_and_prechecks_pure_non_cps_coupons():
    # 1. 严格拦截原样回显、仅 http->https 变化或非联盟推广域的伪成功返回
    assert _is_verified_station_promo_link(
        "http://s.click.taobao.com/XNl8wji", "https://s.click.taobao.com/XNl8wji/"
    ) is False
    assert _is_verified_station_promo_link(
        "https://llipin.cn/hjA2NeT", "https://llipin.cn/hjA2NeT"
    ) is False
    assert _is_verified_station_promo_link(
        "https://u.jd.com/eOoAPas", "https://jingfen.jd.com/item?u_act_p=coupon&utm_campaign=t_1_"
    ) is False
    assert _is_verified_station_promo_link(
        "https://u.jd.com/other_code", "https://u.jd.com/my_new_code"
    ) is True
    assert _is_verified_station_promo_link(
        "https://s.tb.cn/h.abc", "https://uland.taobao.com/coupon/edetail?e=my_encrypted"
    ) is True

    # 2. convert_single_target 端到端验证：
    #    - 纯无佣金 coupon.m.jd.com 直接前置命中，0 次上游 API 调用；
    #    - 折淘客 tkl_piliang 伪成功回显 http->https s.click 他人红包，经 Gate 1 + Gate 2 真访 /wow/z/lianmeng/ 后彻底拦截为 failed；
    #    - 第三方短链 yuebai.plus 转链失败后经安全解包重组为纯净官方 JD 券链 (OK_DIRECT_COUPON)。
    fake_session = _RouteSession({
        "http://s.click.taobao.com/XNl8wji": _FakeResponse(
            status_code=200,
            body="<script>var real_jump_address = 'https://pages.tmall.com/wow/z/lianmeng/default/route?wh_alimama=true';</script>",
        ),
        "http://p.yuebai.plus/wNnv6GM": _FakeResponse(
            status_code=302,
            headers={"Location": "https://coupon.m.jd.com/coupons/show.action?key=yb_k&roleId=yb_r&utm_source=yb"},
        ),
    })
    cleaner = DealPoolCleaner(
        ai_batch_size=2,
        ai_max_workers=1,
        convert_max_workers=1,
        unwrap_http_session=fake_session,
        unwrap_resolver=_public_dns_resolver,
    )
    jd_calls = []
    tb_calls = []
    cleaner.adapters = {
        "jd": SimpleNamespace(
            resolve_and_convert_universal=lambda u: jd_calls.append(u) or {"status": "error", "error_msg": "非联盟渠道券"}
        ),
        "taobao": SimpleNamespace(
            resolve_and_convert_universal=lambda u: tb_calls.append(u) or {
                "status": "success",
                # 模拟 tkl_piliang 原样回显（仅把 http 变 https 并补斜杠）
                "final_link": "https://s.click.taobao.com/XNl8wji/",
                "url": "https://s.click.taobao.com/XNl8wji/",
                "promotion_token": "",
                "product": None,
            }
        ),
        "pdd": SimpleNamespace(
            resolve_and_convert_universal=lambda _u: {"status": "error", "error_msg": "非拼多多"}
        ),
    }

    # (a) 纯 coupon.m.jd.com 前置直出，不浪费任何 JD API 调用
    direct_jd = cleaner.convert_single_target({
        "link_id": "L1",
        "clean_input": "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=r1&utm_source=junk",
        "platform": "jd",
        "text_role_hint": "coupon_candidate",
    })
    assert direct_jd["convert_status"] == "coupon_or_activity"
    assert direct_jd["error_code"] == "OK_DIRECT_COUPON"
    assert direct_jd["my_promo_link"] == "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=r1"
    assert jd_calls == []

    # (b) 伪成功回显他人超红短链 -> Gate 1 识破回显 + Gate 2 真正请求页面并识破 /wow/z/lianmeng/ -> 彻底判 failed
    spoof_tb = cleaner.convert_single_target({
        "link_id": "L2",
        "clean_input": "http://s.click.taobao.com/XNl8wji",
        "platform": "taobao",
        "text_role_hint": "coupon_candidate",
    })
    assert spoof_tb["convert_status"] == "failed"
    assert spoof_tb["my_promo_link"] is None
    assert any(u == "http://s.click.taobao.com/XNl8wji" for u, _ in fake_session.calls)

    # (c) yuebai.plus 未知短链 -> Gate 2 安全解包救回纯净 coupon.m.jd.com 并自动归位 platform="jd"
    rescued_yb = cleaner.convert_single_target({
        "link_id": "L3",
        "clean_input": "http://p.yuebai.plus/wNnv6GM",
        "platform": "unknown",
        "text_role_hint": "coupon_candidate",
    })
    assert rescued_yb["convert_status"] == "coupon_or_activity"
    assert rescued_yb["error_code"] == "OK_DIRECT_COUPON"
    assert rescued_yb["platform"] == "jd"
    assert rescued_yb["my_promo_link"] == "https://coupon.m.jd.com/coupons/show.action?key=yb_k&roleId=yb_r"


def test_stage5_filters_cross_platform_and_redundant_tb_sclick():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    # Stage 5 组装净化：淘宝商品自动剔除冗余单品 s.click 伪券链和跨平台京东广告尾巴，仅保留真实消费券会场
    cleaner.adapters = {
        "taobao": SimpleNamespace(
            resolve_and_convert_universal=lambda inp: (
                {
                    "status": "success",
                    "goods_id": "TB_MAIN_1",
                    "goods_sign": "TB_MAIN_1",
                    "final_link": "https://uland.taobao.com/coupon/edetail?e=main_2in1",
                    "promotion_token": "￥MainTok111A￥",
                    "product": {
                        "platform": "taobao",
                        "product_id": "TB_MAIN_1",
                        "name": "维达棉韧抽纸24包",
                        "price": 25.9,
                        "original_price": 39.9,
                        "activity_price": 25.9,
                        "coupon_amount": 5.0,
                        "mall_name": "维达官方旗舰店",
                    },
                }
                if inp == "MAIN_TB"
                else {
                    "status": "success",
                    "resolution_kind": "coupon_or_venue",
                    "goods_id": "",
                    "goods_sign": "",
                    "final_link": "https://s.click.taobao.com/redundant_tlj_item",
                    "promotion_token": "￥Redundant11￥",
                    "product": None,
                }
            )
        ),
        "jd": SimpleNamespace(
            resolve_and_convert_universal=lambda _inp: {
                "status": "success",
                "resolution_kind": "coupon_or_venue",
                "goods_id": "",
                "goods_sign": "",
                "final_link": "https://u.jd.com/cross_plat_ad",
                "promotion_token": "",
                "product": None,
            }
        ),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        "tb_post_1": {
            "main_tip_id": "tb_post_1",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tb_post_1"],
            "combined_raw_text": "维达好价 [#L1] [#L2] [#L3] [#L4]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "MAIN_TB", "clean_input": "MAIN_TB", "platform": "taobao", "text_role_hint": "main_candidate"},
                {"link_id": "L2", "raw_span": "SCLICK_DUP", "clean_input": "SCLICK_DUP", "platform": "taobao", "text_role_hint": "coupon_candidate"},
                {"link_id": "L3", "raw_span": "https://u.jd.com/other_ad", "clean_input": "https://u.jd.com/other_ad", "platform": "jd", "text_role_hint": "coupon_candidate"},
                {
                    "link_id": "L4",
                    "raw_span": "https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479",
                    "clean_input": "https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479",
                    "platform": "taobao",
                    "text_role_hint": "coupon_candidate",
                },
            ],
            "_from_fallback": False,
        }
    }
    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        "tb_post_1": {
            "main_tip_id": "tb_post_1",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": 19.9,
            "coupon_links": [
                {"link_id": "L2", "label": "礼金单品伪券"},
                {"link_id": "L3", "label": "京东红包小尾巴"},
                {"link_id": "L4", "label": "88VIP消费券会场"},
            ],
            "tutorial_steps": ["1. 先领88VIP消费券，再拍主商品实付19.9元"],
            "quality_score": 88,
            "score_breakdown": {},
        }
    }

    raw_col2 = MagicMock()
    raw_col2.find.return_value = []
    res = cleaner.clean_batch_records(
        [{
            "source_site": "xianbao_fun",
            "source_channel": "tb",
            "source_item_id": "tb_post_1",
            "platform": "taobao",
            "title": "维达抽纸19.9元",
            "tutorial_text": "维达抽纸好价",
            "extracted_links": [],
            "extracted_tokens": [],
        }],
        raw_col2,
        shared_active_products={},
        quiet_sub_logs=True,
    )
    assert res[0]["status"] == "success"
    cpn_links = res[0]["product_doc"]["deal_info"]["coupon_links"]
    assert len(cpn_links) == 1
    assert cpn_links[0]["link_id"] == "L4"
    assert cpn_links[0]["url"] == "https://pages.tmall.com/wow/a/act/tmall/dailygroup/23288/wupr?wh_pid=daily-544479"


def test_fail_closed_coupon_integrity_and_retention():
    """
    针对用户核心痛点的回归单测：
    1. 原始线报含优惠券链接且转链失败（failed）-> 必须被 Fail-Closed 淘汰拦截（绝不放行、绝不脑补直拍）；
    2. 原始线报含有效优惠券链接但 AI-2 漏填/丢弃（未保留入 coupon_links）-> 必须被 Fail-Closed 淘汰拦截；
    3. 原始线报本无优惠券链接（纯直降/店内满减）-> 正常放行，绝不误杀；
    4. 原始线报含重复优惠券链接（一残一全），有效链接成功保留 -> 正常放行；
    5. 淘系二合一链接（虽标注 coupon_candidate 但实际转出主商品自身）-> 正常放行。
    """
    cleaner = DealPoolCleaner(ai_batch_size=5, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    # 1. Case A: 原始线报含优惠券链接，但转链失败（如好单库截断的 3.cn/3cj）
    cleaner.adapters = {
        "jd": SimpleNamespace(
            resolve_and_convert_universal=lambda inp: {
                "status": "error" if ("DEAD_CPN" in inp or "item.jd.com" in inp) else "success",
                "goods_id": f"JD_{inp}",
                "goods_sign": f"JD_{inp}",
                "final_link": f"https://u.jd.com/{inp}" if "MAIN" in inp else "",
                "promotion_token": "",
                "product": {
                    "platform": "jd",
                    "product_id": f"JD_{inp}",
                    "name": f"商品_{inp}",
                    "price": 6.9 if "MAIN_7" in inp else 36.9,
                    "original_price": 6.9 if "MAIN_7" in inp else 36.9,
                    "activity_price": 6.9 if "MAIN_7" in inp else 36.9,
                    "coupon_amount": 0.0,
                    "mall_name": "官方旗舰店",
                } if "MAIN" in inp else None,
            }
        ),
        "taobao": SimpleNamespace(
            resolve_and_convert_universal=lambda inp: {
                "status": "success",
                "goods_id": "TB_2IN1",
                "goods_sign": "TB_2IN1",
                "final_link": "https://s.click.taobao.com/2in1",
                "promotion_token": "￥2in1￥",
                "product": {
                    "platform": "taobao",
                    "product_id": "TB_2IN1",
                    "name": "淘系二合一商品",
                    "price": 19.9,
                    "original_price": 29.9,
                    "activity_price": 19.9,
                    "coupon_amount": 10.0,
                    "mall_name": "天猫旗舰店",
                },
            }
        ),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    # 模拟 5 个典型场景
    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        # 场景 1: 券链转链失败
        "tip_cpn_dead": {
            "main_tip_id": "tip_cpn_dead",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_cpn_dead"],
            "combined_raw_text": "白象方便面24袋 [#L1] 拍下26.9元 [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "DEAD_CPN", "clean_input": "DEAD_CPN", "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L2", "raw_span": "MAIN_1", "clean_input": "MAIN_1", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
        # 场景 2: 券链转链成功，但 AI-2 遗漏未保留
        "tip_cpn_dropped": {
            "main_tip_id": "tip_cpn_dropped",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_cpn_dropped"],
            "combined_raw_text": "某商品好价 领券 [#L1] 下单 [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {
                    "link_id": "L1",
                    "raw_span": "https://coupon.m.jd.com/coupons/show.action?key=valid_key&roleId=123",
                    "clean_input": "https://coupon.m.jd.com/coupons/show.action?key=valid_key&roleId=123",
                    "platform": "jd",
                    "text_role_hint": "coupon_candidate",
                },
                {"link_id": "L2", "raw_span": "MAIN_2", "clean_input": "MAIN_2", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
        # 场景 3: 原始线报本无券链（纯直降）
        "tip_no_coupon": {
            "main_tip_id": "tip_no_coupon",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_no_coupon"],
            "combined_raw_text": "直降特惠29.9元 [#L1]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "MAIN_3", "clean_input": "MAIN_3", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
        # 场景 4: 重复券链（一残一全），有效链接成功保留
        "tip_cpn_dup": {
            "main_tip_id": "tip_cpn_dup",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_cpn_dup"],
            "combined_raw_text": "领券 [#L1] ( [#L2] ) 拍 [#L3]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "DEAD_CPN", "clean_input": "DEAD_CPN", "platform": "jd", "text_role_hint": "coupon_candidate"},
                {
                    "link_id": "L2",
                    "raw_span": "https://coupon.m.jd.com/coupons/show.action?key=full_key&roleId=456",
                    "clean_input": "https://coupon.m.jd.com/coupons/show.action?key=full_key&roleId=456",
                    "platform": "jd",
                    "text_role_hint": "coupon_candidate",
                },
                {"link_id": "L3", "raw_span": "MAIN_4", "clean_input": "MAIN_4", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
        # 场景 5: 淘系二合一链接（虽标注 coupon_candidate 但实际转出主商品自身）
        "tip_2in1": {
            "main_tip_id": "tip_2in1",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_2in1"],
            "combined_raw_text": "天猫二合一特惠 [#L1]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "https://uland.taobao.com/coupon/edetail?e=test", "clean_input": "https://uland.taobao.com/coupon/edetail?e=test", "platform": "taobao", "text_role_hint": "coupon_candidate"},
            ],
            "_from_fallback": False,
        },
        # 场景 6: 白象方便面原型（短链截断导致非凑单链接失效，且到手价低于官方售价）
        "tip_bx_truncated": {
            "main_tip_id": "tip_bx_truncated",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_bx_truncated"],
            "combined_raw_text": "白象方便面24袋 拍下26.9元 [#L1] [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "DEAD_CPN_TRUNCATED", "clean_input": "DEAD_CPN_TRUNCATED", "platform": "jd", "text_role_hint": "main_candidate"},
                {"link_id": "L2", "raw_span": "MAIN_BX", "clean_input": "MAIN_BX", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
        # 场景 7: 雪菲力盐汽水原型（冗余数字 SKU 链接 L1 转链失败，CPS 短链 L2 转链成功，到手价等于活动售价 6.9 元）
        "tip_direct_with_failed_anchor": {
            "main_tip_id": "tip_direct_with_failed_anchor",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_direct_with_failed_anchor"],
            "combined_raw_text": "雪菲力盐汽水 6.9元 [#L1] 购买方案：1 加购 [#L2] 2 下单实付6.9元",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "https://item.jd.com/100013333390.html", "clean_input": "https://item.jd.com/100013333390.html", "platform": "jd", "text_role_hint": "main_candidate"},
                {"link_id": "L2", "raw_span": "MAIN_7_CPS", "clean_input": "MAIN_7_CPS", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
    }

    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        # 场景 1: AI-2 试图放行（直拍特惠），但券链已死
        "tip_cpn_dead": {
            "main_tip_id": "tip_cpn_dead",
            "is_valid_deal": True,
            "main_link_id": "L2",
            "deal_price": 26.9,
            "coupon_links": [],
            "tutorial_steps": ["1. 进入本品详情页拍下，实付26.9元"],
            "quality_score": 85,
        },
        # 场景 2: 券链有效，但 AI-2 漏填 coupon_links
        "tip_cpn_dropped": {
            "main_tip_id": "tip_cpn_dropped",
            "is_valid_deal": True,
            "main_link_id": "L2",
            "deal_price": 29.9,
            "coupon_links": [],  # 漏填！
            "tutorial_steps": ["1. 进入本品详情页拍下"],
            "quality_score": 85,
        },
        # 场景 3: 原始无券，正常直拍
        "tip_no_coupon": {
            "main_tip_id": "tip_no_coupon",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": 29.9,
            "coupon_links": [],
            "tutorial_steps": ["1. 进入本品详情页下单，实付29.9元"],
            "quality_score": 85,
        },
        # 场景 4: 保留有效券 L2
        "tip_cpn_dup": {
            "main_tip_id": "tip_cpn_dup",
            "is_valid_deal": True,
            "main_link_id": "L3",
            "deal_price": 19.9,
            "coupon_links": [{"link_id": "L2", "label": "优惠券"}],
            "tutorial_steps": ["1. 上方领券", "2. 拍下商品"],
            "quality_score": 90,
        },
        # 场景 5: 二合一商品
        "tip_2in1": {
            "main_tip_id": "tip_2in1",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": 19.9,
            "coupon_links": [],
            "tutorial_steps": ["1. 拍下二合一商品"],
            "quality_score": 88,
        },
        # 场景 6: 白象原型，AI-2 试图脑补为无需用券直拍 26.9 元（官方价 36.9）
        "tip_bx_truncated": {
            "main_tip_id": "tip_bx_truncated",
            "is_valid_deal": True,
            "main_link_id": "L2",
            "deal_price": 26.9,
            "coupon_links": [],
            "tutorial_steps": ["1. 进入本品详情页拍下 1 件，实付 26.9 元"],
            "quality_score": 85,
        },
        # 场景 7: 雪菲力盐汽水原型
        "tip_direct_with_failed_anchor": {
            "main_tip_id": "tip_direct_with_failed_anchor",
            "is_valid_deal": True,
            "main_link_id": "L2",
            "deal_price": 6.9,
            "coupon_links": [],
            "tutorial_steps": ["1. 进入可口可乐雪菲力详情页加购1件", "2. 直接下单付款实付6.9元"],
            "quality_score": 94,
        },
    }

    mock_col = MagicMock()
    mock_col.find.return_value = []
    input_records = [
        {"source_site": "xianbao_fun", "source_channel": "jd", "source_item_id": "tip_cpn_dead", "platform": "jd", "title": "白象方便面", "tutorial_text": "白象方便面", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "xianbao_fun", "source_channel": "jd", "source_item_id": "tip_cpn_dropped", "platform": "jd", "title": "某商品", "tutorial_text": "某商品", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "xianbao_fun", "source_channel": "jd", "source_item_id": "tip_no_coupon", "platform": "jd", "title": "直降商品", "tutorial_text": "直降商品", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "xianbao_fun", "source_channel": "jd", "source_item_id": "tip_cpn_dup", "platform": "jd", "title": "重复券商品", "tutorial_text": "重复券商品", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "xianbao_fun", "source_channel": "tb", "source_item_id": "tip_2in1", "platform": "taobao", "title": "二合一商品", "tutorial_text": "二合一商品", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "haodanku", "source_channel": "jd", "source_item_id": "tip_bx_truncated", "platform": "jd", "title": "白象方便面组合", "tutorial_text": "白象方便面组合", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "guangdiu", "source_channel": "jd", "source_item_id": "tip_direct_with_failed_anchor", "platform": "jd", "title": "雪菲力柠檬味盐汽水", "tutorial_text": "雪菲力柠檬味盐汽水", "extracted_links": [], "extracted_tokens": []},
    ]

    results = cleaner.clean_batch_records(input_records, mock_col, shared_active_products={}, quiet_sub_logs=True)
    res_map = {r["source_item_id"]: r for r in results}

    # 验证 1: tip_cpn_dead 必须被拦截且错误码为 ERR_AI2_COUPON_FAILED_OR_DROPPED
    assert res_map["tip_cpn_dead"]["status"] == "failed"
    assert res_map["tip_cpn_dead"]["error_code"] == ERR_AI2_COUPON_FAILED_OR_DROPPED

    # 验证 2: tip_cpn_dropped 已成功转链为有效 coupon_or_activity，即使 AI-2 漏填仍确定性保留回 coupon_links（不误杀）
    assert res_map["tip_cpn_dropped"]["status"] == "success"
    rescued_cpns = res_map["tip_cpn_dropped"]["product_doc"]["deal_info"]["coupon_links"]
    assert len(rescued_cpns) == 1
    assert rescued_cpns[0]["link_id"] == "L1"
    assert rescued_cpns[0]["url"] == "https://coupon.m.jd.com/coupons/show.action?key=valid_key&roleId=123"

    # 验证 3: tip_no_coupon 正常成功放行入库
    assert res_map["tip_no_coupon"]["status"] == "success"
    assert res_map["tip_no_coupon"]["product_doc"]["deal_info"]["coupon_links"] == []

    # 验证 4: tip_cpn_dup 正常成功放行，且 coupon_links 包含有效券 L2
    assert res_map["tip_cpn_dup"]["status"] == "success"
    dup_cpns = res_map["tip_cpn_dup"]["product_doc"]["deal_info"]["coupon_links"]
    assert len(dup_cpns) == 1
    assert dup_cpns[0]["link_id"] == "L2"

    # 验证 5: tip_2in1 正常成功放行
    assert res_map["tip_2in1"]["status"] == "success"

    # 验证 6: tip_bx_truncated 必须被门禁三拦截（缺券断链且到手价低于官方售价）
    assert res_map["tip_bx_truncated"]["status"] == "failed"
    assert res_map["tip_bx_truncated"]["error_code"] == ERR_AI2_COUPON_FAILED_OR_DROPPED

    # 验证 7: tip_direct_with_failed_anchor 正常成功放行入库，绝不误杀
    assert res_map["tip_direct_with_failed_anchor"]["status"] == "success"
    assert res_map["tip_direct_with_failed_anchor"]["product_doc"]["deal_info"]["coupon_links"] == []
    assert res_map["tip_direct_with_failed_anchor"]["product_doc"]["deal_info"]["deal_price"] == 6.9
    assert res_map["tip_direct_with_failed_anchor"]["product_doc"]["deal_info"]["quality_score"] == 94


def test_guangdiu_deterministic_coupon_supplement():
    """
    验证方案二：针对逛丢（guangdiu）确定性 HTML DOM 结构，
    当 AI-1 偶发漏提提取出的官方优惠券链接时，
    Stage 1 规范化逻辑能确定性自动补齐入 extracted_targets，并顺利完成转链与卡片展示。
    """
    cleaner = DealPoolCleaner(ai_batch_size=5, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    cleaner.adapters = {
        "jd": SimpleNamespace(
            resolve_and_convert_universal=lambda inp: (
                {
                    "status": "success",
                    "resolution_kind": "product",
                    "goods_id": "JD_SOD",
                    "goods_sign": "JD_SOD",
                    "final_link": "https://u.jd.com/promo_sod",
                    "promotion_token": "！SOD！",
                    "product": {
                        "platform": "jd",
                        "product_id": "JD_SOD",
                        "name": "巴半SOD蜜",
                        "price": 5.9,
                        "original_price": 5.9,
                        "activity_price": 5.9,
                        "coupon_amount": 0.0,
                        "mall_name": "专营店",
                    },
                }
                if "union-click" in inp
                else {
                    "status": "success",
                    "resolution_kind": "coupon_or_venue",
                    "goods_id": "",
                    "goods_sign": "",
                    "final_link": "https://u.jd.com/cpn_5yuan",
                    "promotion_token": "！CPN5！",
                    "product": None,
                }
            )
        ),
        "taobao": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    # 模拟 AI-1 漏提：输入文案中有券，但 AI-1 只提取了主商品 L1
    cleaner._call_ai_json_array = lambda *_a, **_kw: [
        {
            "main_tip_id": "tip_guangdiu_sod",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_guangdiu_sod"],
            "extracted_targets": [
                {
                    "link_id": "L1",
                    "raw_span": "https://union-click.jd.com/jdc?e=test",
                    "clean_input": "https://union-click.jd.com/jdc?e=test",
                    "platform": "jd",
                    "text_role_hint": "main_candidate",
                }
            ],
            "deal_price": 0.9,
            "tutorial_score": 88,
        }
    ]

    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        "tip_guangdiu_sod": {
            "main_tip_id": "tip_guangdiu_sod",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": 0.9,
            "coupon_links": [{"link_id": "L2", "label": "满5.9减5元"}],
            "tutorial_steps": ["1. 领取5元券", "2. 加购下单实付0.9元"],
            "quality_score": 92,
        }
    }

    mock_col = MagicMock()
    mock_col.find.return_value = []

    raw_doc = {
        "source_site": "guangdiu",
        "source_channel": "jd",
        "source_item_id": "tip_guangdiu_sod",
        "platform": "jd",
        "title": "巴半 SOD蜜滋润霜 0.9元",
        "tutorial_text": "巴半 SOD蜜滋润霜 0.9元",
        "extracted_links": [
            {
                "label": "逛丢直达购买链",
                "url": "https://union-click.jd.com/jdc?e=test",
                "type": "guangdiu_go_target",
                "platform": "jd",
            },
            {
                "label": "专属隐藏券 满5.9元减5元（领取链接）",
                "url": "https://pro.m.jd.com/mall/active/4UzdvZvEJCjjsit1ir62F2xjTagf/index.html",
                "type": "anchor",
                "platform": "jd",
            },
        ],
        "extracted_tokens": [],
    }

    results = cleaner.clean_batch_records([raw_doc], mock_col, shared_active_products={}, quiet_sub_logs=True)
    res = results[0]

    # 断言：必须成功入库，且 coupon_links 包含自动补齐并成功转链的优惠券
    assert res["status"] == "success"
    deal_info = res["product_doc"]["deal_info"]
    assert deal_info["deal_price"] == 0.9
    assert len(deal_info["coupon_links"]) == 1
    assert deal_info["coupon_links"][0]["url"] == "https://u.jd.com/cpn_5yuan"
    assert deal_info["coupon_links"][0]["token"] == "！CPN5！"


def test_tun_fake_ip_dns_allowed_for_domains_but_blocked_as_literal():
    """验证域名解析返回 TUN Fake-IP (198.18.0.0/15) 允许解包，但直接访问裸 IP 198.18.x.x 及私网域名仍严格拦截。"""
    def _fake_ip_resolver(host: str, port: int, *_args, **_kwargs):
        ip_map = {
            "3.cn": "198.18.0.65",
            "internal.evil.test": "192.168.1.25",
        }
        ip = ip_map.get(host.lower(), "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    assert _is_safe_public_http_url("https://3.cn/3cKY-ZeN", resolver=_fake_ip_resolver) is True
    assert _is_safe_public_http_url("http://198.18.0.65/coupons", resolver=_fake_ip_resolver) is False
    assert _is_safe_public_http_url("https://internal.evil.test/coupon", resolver=_fake_ip_resolver) is False

    session = _RouteSession({
        "https://3.cn/3cKY-ZeN": _FakeResponse(
            status_code=302,
            headers={
                "Location": "https://coupon.m.jd.com/coupons/show.action?linkKey=LK_TUN_OK&to=m.jd.com&utm_source=x"
            },
        ),
    })
    assert (
        unwrap_to_official_public_venue_url(
            "https://3.cn/3cKY-ZeN",
            http_session=session,
            resolver=_fake_ip_resolver,
        )
        == "https://coupon.m.jd.com/coupons/show.action?linkKey=LK_TUN_OK"
    )


def test_shop_m_jd_whitelist_and_false_positive_coupon_gates():
    """验证 shop.m.jd.com 白名单提纯、淘宝双链接吸收放行、JD 冗余数字 SKU + 国补低价不误杀及维护常量导入完整。"""
    import services.deal_pool_cleaner as cleaner_mod

    assert isinstance(cleaner_mod.DEAL_FORMAL_EXPIRY_HOURS, (int, float))

    # 1. shop.m.jd.com 店铺首页与会员卡页参数提纯，缺关键参数则拒绝
    assert (
        rebuild_clean_official_venue_url(
            "https://shop.m.jd.com/mshop/gethomepage?venderid=648791&cu=true&utm=bad"
        )
        == "https://shop.m.jd.com/mshop/gethomepage?venderId=648791"
    )
    assert (
        rebuild_clean_official_venue_url(
            "https://shop.m.jd.com/member/card?venderId=200797238&shopId=200797238&channel=80545052&zdmcode=bad"
        )
        == "https://shop.m.jd.com/member/card?venderId=200797238&shopId=200797238"
    )
    assert rebuild_clean_official_venue_url("https://shop.m.jd.com/mshop/gethomepage?cu=true") is None

    # 1b. store.taobao.com 店铺首页同样参数白名单提纯且不当成优惠券误杀
    assert (
        rebuild_clean_official_venue_url(
            "https://store.taobao.com/shop/view_shop.htm?user_number_id=221234&spm=bad"
        )
        == "https://store.taobao.com/shop/view_shop.htm?user_number_id=221234"
    )
    assert rebuild_clean_official_venue_url("https://store.taobao.com/shop/view_shop.htm?spm=bad") is None
    assert DealPoolCleaner._is_explicit_coupon_target({"clean_input": "https://store.taobao.com/shop/view_shop.htm?user_number_id=221234"}) is False

    # 1c. _sanitize_card_highlight 清洗截断与空洞套话剔除
    assert DealPoolCleaner._sanitize_card_highlight("【到手4.9/件】[#L1] https://u.jd.com/abc") == "到手4.9/件"
    assert DealPoolCleaner._sanitize_card_highlight("好价推荐") == ""
    assert len(DealPoolCleaner._sanitize_card_highlight("拍4件叠加满200减20补贴到手价仅19.9元")) <= 16

    # 2. 端到端回归：
    #    - tip_tb_absorbed: 淘系二合一 L1(coupon_candidate) 与 L2(main_candidate) 转出同 goods_id，AI-2 填 coupon_links=[L1] 不被误杀
    #    - tip_jd_subsidy_with_sku: JD 国补低价 + 冗余数字 item.jd.com SKU(ERR_JD_NUMERIC_SKU) + 店铺首页 /mshop/gethomepage 正常放行
    cleaner = DealPoolCleaner(ai_batch_size=5, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    seen_ai2_link_ids = {}

    def _mock_jd_convert(inp: str):
        if "item.jd.com/" in inp:
            return {"status": "error", "error_code": "UNSUPPORTED_JD_NUMERIC_SKU", "error_msg": "不支持数字id"}
        return {
            "status": "success",
            "goods_id": "JD_PHONE_OK",
            "goods_sign": "JD_PHONE_OK",
            "final_link": "https://u.jd.com/ogcywB5",
            "promotion_token": "！PHONE！",
            "product": {
                "platform": "jd",
                "product_id": "JD_PHONE_OK",
                "name": "真我 GT8 Pro 5G手机",
                "price": 3989.0,
                "original_price": 3989.0,
                "activity_price": 3989.0,
                "coupon_amount": 0.0,
                "mall_name": "官方旗舰店",
            },
        }

    cleaner.adapters = {
        "jd": SimpleNamespace(resolve_and_convert_universal=_mock_jd_convert),
        "taobao": SimpleNamespace(
            resolve_and_convert_universal=lambda inp: {
                "status": "success",
                "goods_id": "GKM9yAVTqW",
                "goods_sign": "GKM9yAVTqW",
                "final_link": "https://s.click.taobao.com/costa2in1",
                "promotion_token": "￥COSTA￥",
                "product": {
                    "platform": "taobao",
                    "product_id": "GKM9yAVTqW",
                    "name": "COSTA 巧克力威化饼干",
                    "price": 23.2,
                    "original_price": 27.3,
                    "activity_price": 23.2,
                    "coupon_amount": 68.0,
                    "mall_name": "COSTA旗舰店",
                },
            }
        ),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        "tip_tb_absorbed": {
            "main_tip_id": "tip_tb_absorbed",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_tb_absorbed"],
            "combined_raw_text": "COSTA 威化饼干 24.6元 [#L1] [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "TB_2IN1_A", "clean_input": "https://s.click.taobao.com/a", "platform": "taobao", "text_role_hint": "coupon_candidate"},
                {"link_id": "L2", "raw_span": "TB_2IN1_B", "clean_input": "https://s.click.taobao.com/b", "platform": "taobao", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
        "tip_jd_subsidy_with_sku": {
            "main_tip_id": "tip_jd_subsidy_with_sku",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_jd_subsidy_with_sku"],
            "combined_raw_text": "店铺首页 [#L1] 真我手机 [#L2] 国补3169.61元 [#L3]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "https://shop.m.jd.com/mshop/gethomepage?venderid=1001&cu=true", "clean_input": "https://shop.m.jd.com/mshop/gethomepage?venderid=1001&cu=true", "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L2", "raw_span": "https://item.jd.com/100286262170.html", "clean_input": "https://item.jd.com/100286262170.html", "platform": "jd", "text_role_hint": "main_candidate"},
                {"link_id": "L3", "raw_span": "https://union-click.jd.com/jdc?e=phone", "clean_input": "https://union-click.jd.com/jdc?e=phone", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
    }

    def _mock_ai2(stage2_inputs, *_a, **_kw):
        for item in stage2_inputs:
            seen_ai2_link_ids[item["main_tip_id"]] = [lk["link_id"] for lk in item["converted_links"]]
        return {
            "tip_tb_absorbed": {
                "main_tip_id": "tip_tb_absorbed",
                "is_valid_deal": True,
                "main_link_id": "L2",
                "deal_price": 24.6,
                "card_highlight": " 领券立减到手24.6 ",
                "coupon_links": [{"link_id": "L1", "label": "商品优惠券"}],
                "tutorial_steps": ["1. 领券加购4件实付24.6元"],
                "quality_score": 91,
            },
            "tip_jd_subsidy_with_sku": {
                "main_tip_id": "tip_jd_subsidy_with_sku",
                "is_valid_deal": True,
                "main_link_id": "L3",
                "deal_price": 3169.61,
                "coupon_links": [],
                "tutorial_steps": ["1. 叠加国家补贴实付3169.61元"],
                "quality_score": 95,
            },
        }

    cleaner.run_ai_stage2_batch = _mock_ai2

    mock_col = MagicMock()
    mock_col.find.return_value = []
    input_records = [
        {"source_site": "guangdiu", "source_channel": "latest", "source_item_id": "tip_tb_absorbed", "platform": "taobao", "title": "COSTA 威化饼干", "tutorial_text": "COSTA 威化饼干", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "guangdiu", "source_channel": "latest", "source_item_id": "tip_jd_subsidy_with_sku", "platform": "jd", "title": "国家补贴：真我 GT8 Pro", "tutorial_text": "真我 GT8 Pro", "extracted_links": [], "extracted_tokens": []},
    ]

    results = cleaner.clean_batch_records(input_records, mock_col, shared_active_products={}, quiet_sub_logs=True)
    res_map = {r["source_item_id"]: r for r in results}

    assert res_map["tip_tb_absorbed"]["status"] == "success"
    tb_deal_info = res_map["tip_tb_absorbed"]["product_doc"]["deal_info"]
    assert tb_deal_info["coupon_links"] == []
    assert tb_deal_info["card_highlight"] == "领券立减到手24.6"
    assert "deal_price_text" not in tb_deal_info
    assert "tutorial_images" not in tb_deal_info

    assert res_map["tip_jd_subsidy_with_sku"]["status"] == "success"
    assert seen_ai2_link_ids["tip_jd_subsidy_with_sku"] == ["L1", "L3"]
    assert res_map["tip_jd_subsidy_with_sku"]["product_doc"]["deal_info"]["deal_price"] == 3169.61


def test_transient_degraded_retry_and_cross_platform_consistency():
    """
    验证直播审计修复的两项核心根因保护：
    1. 多链线报只要存在非凑单品链接触发详情反查暂态限流 (`resolution_kind == "degraded_product"`)，
       在未达重试上限前必须走 `retry_pending` 重试，而非直接送入 AI-2 误杀；达到上限则正常放行判定，
       且确定性永久失败 (`resolution_kind == "error"`) 不应触发无意义重试；
    2. `_is_explicit_coupon_target` 不得将标为主推的淘宝二合一单品页 (`uland.taobao.com/coupon/edetail`) 误判为独立券；
       异平台比价场景中，选对主平台不被异平台对比链误杀，选错且混配异平台券则防串货拦截。
    """
    from models.deal_constants import (
        CLEAN_STAGE_NEED_CONVERT,
        ERR_ADAPTER_CONVERT_FAILED,
        MAX_CLEAN_RETRIES,
    )

    # --- A. _is_explicit_coupon_target 二合一主链 vs 独立领券页 ---
    assert (
        DealPoolCleaner._is_explicit_coupon_target({
            "clean_input": "https://uland.taobao.com/coupon/edetail?e=abc",
            "text_role_hint": "main_candidate",
            "convert_status": "product",
        })
        is False
    )
    assert (
        DealPoolCleaner._is_explicit_coupon_target({
            "clean_input": "https://uland.taobao.com/coupon/edetail?e=abc",
            "text_role_hint": "main_candidate",
            "convert_status": "failed",
        })
        is False
    )
    assert (
        DealPoolCleaner._is_explicit_coupon_target({
            "clean_input": "https://uland.taobao.com/quan/detail?sellerId=1&activityId=2",
            "text_role_hint": "main_candidate",
            "convert_status": "failed",
        })
        is True
    )
    assert (
        DealPoolCleaner._is_explicit_coupon_target({
            "clean_input": "https://uland.taobao.com/coupon/edetail?e=abc",
            "text_role_hint": "coupon_candidate",
            "convert_status": "product",
        })
        is True
    )

    # --- B. 多链暂态 degraded_product 重试与上限收敛 ---
    cleaner = DealPoolCleaner(ai_batch_size=5, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    def _mock_jd_transient(inp: str):
        if "coupon_ok" in inp or "perm_main_ok" in inp:
            gid = "JD_PERM_OK" if "perm_main_ok" in inp else "JD_MAIN_OK"
            return {
                "status": "success",
                "goods_id": gid,
                "goods_sign": gid,
                "final_link": f"https://u.jd.com/{gid.lower()}",
                "promotion_token": f"！{gid}！",
                "product": {
                    "platform": "jd",
                    "product_id": gid,
                    "name": "主品运动鞋" if gid == "JD_MAIN_OK" else "葡口饮料",
                    "price": 129.0,
                    "original_price": 129.0,
                    "activity_price": 129.0,
                    "coupon_amount": 0.0,
                    "mall_name": "官方旗舰店",
                },
            }
        if "degraded_sec" in inp:
            return {
                "status": "error",
                "detail_lookup_failed": True,
                "resolution_kind": "degraded_product",
                "error_code": "DETAIL_RATE_LIMITED",
                "error_msg": "详情限流",
            }
        return {
            "status": "error",
            "resolution_kind": "error",
            "error_code": "PROMOTION_FAILED",
            "error_msg": "京东返回业务错误",
        }

    cleaner.adapters = {
        "jd": SimpleNamespace(resolve_and_convert_universal=_mock_jd_transient),
        "taobao": SimpleNamespace(
            resolve_and_convert_universal=lambda _inp: {
                "status": "success",
                "goods_id": "TB_MAIN_OK",
                "goods_sign": "TB_MAIN_OK",
                "final_link": "https://s.click.taobao.com/tb_main",
                "promotion_token": "￥TBMAIN￥",
                "product": {
                    "platform": "taobao",
                    "product_id": "TB_MAIN_OK",
                    "name": "喜美诺 100W 快充线",
                    "price": 9.9,
                    "original_price": 17.9,
                    "activity_price": 9.9,
                    "coupon_amount": 8.0,
                    "mall_name": "喜美诺旗舰店",
                },
            }
        ),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    s3_docs = [
        {
            "source_site": "zdzdm",
            "source_channel": "真的值得买_国内优惠",
            "source_item_id": "tip_deg_retry",
            "platform": "jd",
            "title": "运动鞋",
            "clean_retry_count": 0,
            "ai_stage1_result": {
                "main_tip_id": "tip_deg_retry",
                "is_standalone_deal": True,
                "merged_tip_ids": ["tip_deg_retry"],
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": "https://u.jd.com/coupon_ok", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L2", "clean_input": "https://union-click.jd.com/degraded_sec", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
            },
        },
        {
            "source_site": "zdzdm",
            "source_channel": "真的值得买_国内优惠",
            "source_item_id": "tip_deg_maxed",
            "platform": "jd",
            "title": "运动鞋",
            "clean_retry_count": MAX_CLEAN_RETRIES - 1,
            "ai_stage1_result": {
                "main_tip_id": "tip_deg_maxed",
                "is_standalone_deal": True,
                "merged_tip_ids": ["tip_deg_maxed"],
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": "https://u.jd.com/coupon_ok", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L2", "clean_input": "https://union-click.jd.com/degraded_sec", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
            },
        },
        {
            "source_site": "zzzdm",
            "source_channel": "值值得买_24小时热榜",
            "source_item_id": "tip_perm_err",
            "platform": "jd",
            "title": "饮料",
            "clean_retry_count": 0,
            "ai_stage1_result": {
                "main_tip_id": "tip_perm_err",
                "is_standalone_deal": True,
                "merged_tip_ids": ["tip_perm_err"],
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": "https://u.jd.com/perm_err", "platform": "jd", "text_role_hint": "coupon_candidate"},
                    {"link_id": "L2", "clean_input": "https://u.jd.com/perm_main_ok", "platform": "jd", "text_role_hint": "main_candidate"},
                ],
            },
        },
    ]

    s3_out, _ = cleaner.process_convert_unit(s3_docs, shared_active_products={}, quiet_sub_logs=True)
    s3_map = {o["source_item_id"]: o for o in s3_out}

    assert s3_map["tip_deg_retry"]["status"] == "retry_pending"
    assert s3_map["tip_deg_retry"]["clean_stage"] == CLEAN_STAGE_NEED_CONVERT
    assert s3_map["tip_deg_retry"]["error_code"] == ERR_ADAPTER_CONVERT_FAILED
    assert s3_map["tip_deg_retry"]["retry_count"] == 1

    assert s3_map["tip_deg_maxed"]["stage_outcome"] == "advance_to_ai2"
    assert s3_map["tip_perm_err"]["stage_outcome"] == "advance_to_ai2"

    # --- C. 跨平台比价贴一致性校验 ---
    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        "tip_cross_ok": {
            "main_tip_id": "tip_cross_ok",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_cross_ok"],
            "combined_raw_text": "喜美诺快充线 8.03元 [#L1] 京东现售29.9 [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "TB_URL", "clean_input": "https://uland.taobao.com/coupon/edetail?e=ok", "platform": "taobao", "text_role_hint": "main_candidate"},
                {"link_id": "L2", "raw_span": "JD_URL", "clean_input": "https://u.jd.com/coupon_ok", "platform": "jd", "text_role_hint": "coupon_candidate"},
            ],
            "_from_fallback": False,
        },
        "tip_cross_mix": {
            "main_tip_id": "tip_cross_mix",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_cross_mix"],
            "combined_raw_text": "喜美诺快充线 8.03元 [#L1] 京东现售29.9 [#L2]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "TB_URL", "clean_input": "https://uland.taobao.com/coupon/edetail?e=ok", "platform": "taobao", "text_role_hint": "coupon_candidate"},
                {"link_id": "L2", "raw_span": "JD_URL", "clean_input": "https://u.jd.com/coupon_ok", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        },
    }
    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        # 正确选中淘宝主商品 L1，不因文末京东比价链 L2 被误杀
        "tip_cross_ok": {
            "main_tip_id": "tip_cross_ok",
            "is_valid_deal": True,
            "main_link_id": "L1",
            "deal_price": 8.03,
            "coupon_links": [],
            "tutorial_steps": ["1. 领券拍下，淘金币抵扣后实付8.03元"],
            "quality_score": 88,
        },
        # 错误选中京东比价链 L2 且把淘宝 L1 塞进 coupon_links -> 必须拦截防跨平台串货
        "tip_cross_mix": {
            "main_tip_id": "tip_cross_mix",
            "is_valid_deal": True,
            "main_link_id": "L2",
            "deal_price": 8.03,
            "coupon_links": [{"link_id": "L1", "label": "8元优惠券"}],
            "tutorial_steps": ["1. 领淘宝券拍京东商品"],
            "quality_score": 85,
        },
    }
    mock_col = MagicMock()
    mock_col.find.return_value = []
    cross_inputs = [
        {"source_site": "ithome_lapin", "source_channel": "IT之家辣品_好物", "source_item_id": "tip_cross_ok", "platform": "taobao", "title": "喜美诺快充线", "tutorial_text": "喜美诺", "extracted_links": [], "extracted_tokens": []},
        {"source_site": "ithome_lapin", "source_channel": "IT之家辣品_好物", "source_item_id": "tip_cross_mix", "platform": "taobao", "title": "喜美诺快充线", "tutorial_text": "喜美诺", "extracted_links": [], "extracted_tokens": []},
    ]
    cross_res = cleaner.clean_batch_records(cross_inputs, mock_col, shared_active_products={}, quiet_sub_logs=True)
    cross_map = {r["source_item_id"]: r for r in cross_res}

    assert cross_map["tip_cross_ok"]["status"] == "success"
    assert cross_map["tip_cross_ok"]["product_doc"]["platform"] == "taobao"
    assert cross_map["tip_cross_mix"]["status"] == "failed"
    assert cross_map["tip_cross_mix"]["error_code"] == ERR_AI2_COUPON_FAILED_OR_DROPPED


def test_shop_homepage_exclusion_empty_coupon_step_rewrite_score_clamp_and_config_error_retry():
    """验证店铺首页不入 coupon_links、无券时改写上方【领券】假提示、剔除隐匿数字SKU占位符、分项评分钳位及 CONFIG_ERROR 可重试。"""
    from models.deal_constants import CLEAN_STAGE_NEED_CONVERT, ERR_ADAPTER_CONFIG_ERROR, ERR_JD_NUMERIC_SKU

    cleaner = DealPoolCleaner(ai_batch_size=4, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    cleaner.adapters = {
        "jd": SimpleNamespace(
            resolve_and_convert_universal=lambda inp: (
                {"status": "error", "error_code": "CONFIG_ERROR", "error_msg": "缺少推广位"}
                if "cfg_err" in inp
                else (
                    {"status": "error", "error_code": "UNSUPPORTED_JD_NUMERIC_SKU", "error_msg": "不支持数字id"}
                    if "item.jd.com/" in inp
                    else {
                        "status": "success",
                        "goods_id": "JD_MAIN_SHOP",
                        "goods_sign": "JD_MAIN_SHOP",
                        "final_link": "https://u.jd.com/main_shop",
                        "promotion_token": "！MAIN！",
                        "product": {
                            "platform": "jd",
                            "product_id": "JD_MAIN_SHOP",
                            "name": "维达抽纸24包",
                            "price": 29.9,
                            "original_price": 39.9,
                            "activity_price": 29.9,
                            "coupon_amount": 0.0,
                            "mall_name": "维达自营店",
                        },
                    }
                )
            )
        ),
        "taobao": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }

    # 1. CONFIG_ERROR 必须进入 retry_pending 而非永久 FAILED
    s3_out, _ = cleaner.process_convert_unit(
        [{
            "source_site": "guangdiu",
            "source_channel": "latest",
            "source_item_id": "tip_cfg_err",
            "platform": "jd",
            "title": "测试配置重试",
            "clean_retry_count": 0,
            "ai_stage1_result": {
                "main_tip_id": "tip_cfg_err",
                "is_standalone_deal": True,
                "merged_tip_ids": ["tip_cfg_err"],
                "extracted_targets": [
                    {"link_id": "L1", "clean_input": "https://u.jd.com/cfg_err", "platform": "jd", "text_role_hint": "main_candidate"}
                ],
            },
        }],
        shared_active_products={},
        quiet_sub_logs=True,
    )
    assert s3_out[0]["status"] == "retry_pending"
    assert s3_out[0]["clean_stage"] == CLEAN_STAGE_NEED_CONVERT
    assert s3_out[0]["error_code"] == ERR_ADAPTER_CONFIG_ERROR

    # 2. 店铺首页排除出 coupon_links + 无券步文改写 + 隐匿数字SKU占位符移除 + 负分项钳位
    captured_combined = []
    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        "tip_shop_and_rewrite": {
            "main_tip_id": "tip_shop_and_rewrite",
            "is_standalone_deal": True,
            "merged_tip_ids": ["tip_shop_and_rewrite"],
            "combined_raw_text": "先看数字 [#L1] 进店 [#L2] 买抽纸 [#L3]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": "https://item.jd.com/10001.html", "clean_input": "https://item.jd.com/10001.html", "platform": "jd", "text_role_hint": "main_candidate"},
                {"link_id": "L2", "raw_span": "https://shop.m.jd.com/mshop/gethomepage?venderId=123", "clean_input": "https://shop.m.jd.com/mshop/gethomepage?venderId=123", "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L3", "raw_span": "https://u.jd.com/main_item", "clean_input": "https://u.jd.com/main_item", "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        }
    }

    def _mock_ai2(stage2_inputs, *_a, **_kw):
        captured_combined.append(stage2_inputs[0]["combined_raw_text"])
        return {
            "tip_shop_and_rewrite": {
                "main_tip_id": "tip_shop_and_rewrite",
                "is_valid_deal": True,
                "main_link_id": "L3",
                "deal_price": 29.9,
                "coupon_links": [{"link_id": "L2", "label": "店铺首页"}],
                "tutorial_steps": ["1. 先在上方【领券】处领取会员券", "2. 下单实付29.9元"],
                "quality_score": 82,
                "score_breakdown": {"match_score": -5, "completeness_score": 45, "price_clarity_score": 15, "tutorial_and_image_score": 15},
            }
        }

    cleaner.run_ai_stage2_batch = _mock_ai2
    mock_col = MagicMock()
    mock_col.find.return_value = []
    res = cleaner.clean_batch_records(
        [{"source_site": "guangdiu", "source_channel": "latest", "source_item_id": "tip_shop_and_rewrite", "platform": "jd", "title": "维达抽纸", "tutorial_text": "维达抽纸", "extracted_links": [], "extracted_tokens": []}],
        mock_col,
        shared_active_products={},
        quiet_sub_logs=True,
    )
    assert res[0]["status"] == "success"
    assert "[#L1]" not in captured_combined[0]
    di = res[0]["product_doc"]["deal_info"]
    assert di["coupon_links"] == []
    assert "上方【领券】" not in di["tutorial_steps"][0]
    assert "通过下方购买链接领取会员券" in di["tutorial_steps"][0]
    assert di["score_breakdown"]["match_score"] == 0
    assert di["score_breakdown"]["completeness_score"] == 30


def test_ai1_verbatim_link_guard_isolated_retry_and_title_dedup():
    """
    验证 AI-1 提取链接原文逐字核验与标题折行/省略号去重：
    1. 批处理返回非原文错拼/跨条拼接 URL -> 立即单条隔离重提并成功救回；
    2. 单条重提仍幻觉/截断 URL -> Fail-Closed 打回 AI1_HALLUCINATED_LINK；
    3. 淘宝 11 位混合口令仅归一化两侧包裹符 -> 直接通过逐字校验，不误触发重提；
    4. `build_clean_original_copywriting` 自动对空白换行差异与省略号截断标题去重。
    """
    from services.deal_cleaning.text_sanitizer import build_clean_original_copywriting

    cleaner = DealPoolCleaner(ai_batch_size=3, ai_max_workers=1, convert_max_workers=1)
    good_u1 = "https://uland.taobao.com/item/edetail?id=ABC123DEF456GHI789"
    bad_u1 = "https://uland.taobao.com/item/edetail?id=ABC123xEF456GHI789"
    good_u2 = "https://union-click.jd.com/jdc?e=FULL999888777"
    bad_u2 = "https://union-click.jd.com/jdc?e=FULL999"

    call_log = []

    def _fake_ai(prompt_tpl, chunk, label):
        mids = [c["main_tip_id"] for c in chunk]
        call_log.append(mids)
        if len(chunk) > 1:
            return [
                {
                    "main_tip_id": "t_rescue",
                    "is_standalone_deal": True,
                    "merged_tip_ids": ["t_rescue"],
                    "extracted_targets": [{"link_id": "L1", "raw_span": bad_u1, "clean_input": bad_u1, "platform": "taobao", "text_role_hint": "main_candidate"}],
                },
                {
                    "main_tip_id": "t_hallucinate",
                    "is_standalone_deal": True,
                    "merged_tip_ids": ["t_hallucinate"],
                    "extracted_targets": [{"link_id": "L1", "raw_span": bad_u2, "clean_input": bad_u2, "platform": "jd", "text_role_hint": "main_candidate"}],
                },
                {
                    "main_tip_id": "t_tok_norm",
                    "is_standalone_deal": True,
                    "merged_tip_ids": ["t_tok_norm"],
                    "extracted_targets": [{"link_id": "L1", "raw_span": "(AbCdEf12345)", "clean_input": "￥AbCdEf12345￥", "platform": "taobao", "text_role_hint": "main_candidate"}],
                },
            ]
        # 单条隔离重提：t_rescue 返回逐字原文链接，t_hallucinate 仍返回前缀截断链接
        mid = mids[0]
        u = good_u1 if mid == "t_rescue" else bad_u2
        return [{
            "main_tip_id": mid,
            "is_standalone_deal": True,
            "merged_tip_ids": [mid],
            "extracted_targets": [{"link_id": "L1", "raw_span": u, "clean_input": u, "platform": "taobao" if mid == "t_rescue" else "jd", "text_role_hint": "main_candidate"}],
        }]

    cleaner._call_ai_json_array = _fake_ai
    docs = {
        "t_rescue": {"source_site": "xuandan", "source_item_id": "t_rescue", "title": "五年高考三年模拟", "tutorial_text": f"五年高考 {good_u1}", "extracted_links": [], "extracted_tokens": []},
        "t_hallucinate": {"source_site": "guangdiu", "source_item_id": "t_hallucinate", "title": "牛奶", "tutorial_text": f"特惠 {good_u2}", "extracted_links": [], "extracted_tokens": []},
        "t_tok_norm": {"source_site": "haodanku", "source_item_id": "t_tok_norm", "title": "抽纸", "tutorial_text": "复制口令 (AbCdEf12345) 下单", "extracted_links": [], "extracted_tokens": []},
    }
    inputs = [cleaner.build_stage1_task_input(d, None)[0] for d in docs.values()]
    out = cleaner.run_ai_stage1_batch(inputs, docs, quiet_sub_logs=True)

    assert call_log[0] == ["t_rescue", "t_hallucinate", "t_tok_norm"]
    assert ["t_rescue"] in call_log
    assert ["t_hallucinate"] in call_log
    assert ["t_tok_norm"] not in call_log  # 口令仅包裹符差异，直接放行不重试

    assert out["t_rescue"]["_from_fallback"] is False
    assert out["t_rescue"]["extracted_targets"][0]["clean_input"] == good_u1
    assert out["t_hallucinate"]["_from_fallback"] is True
    assert out["t_hallucinate"]["_fallback_reason"] == "AI1_HALLUCINATED_LINK"
    assert out["t_tok_norm"]["_from_fallback"] is False

    # 验证省略号截断/空白差异标题不再重复输出两遍，以及正文中穿插短链时仍识别标题前缀不重复
    clean_copy = build_clean_original_copywriting(
        ["0818_1"],
        {"0818_1": {"title": "超值抽纸 24包...", "tutorial_text": "超值 抽纸\n24包整箱特惠9.9元", "extracted_links": [], "extracted_tokens": []}},
        [],
    )
    assert clean_copy.count("超值") == 1

    clean_0818_url_split = build_clean_original_copywriting(
        ["0818_2"],
        {
            "0818_2": {
                "title": "金利来睡衣套装活.动入 金利来 长袖长裤家居服套装女 99元双面磨毛 亲",
                "tutorial_text": "金利来睡衣套装活.动入\n金利来 长袖长裤家居服套装女\nhttps://u.jd.com/oDXtZ6b\n99元双面磨毛 亲肤又柔软\n拍下99元",
                "extracted_links": [],
                "extracted_tokens": [],
            }
        },
        [],
    )
    assert clean_0818_url_split.count("金利来 长袖长裤家居服套装女") == 1
    assert "99元双面磨毛 亲肤又柔软" in clean_0818_url_split


def test_ai2_partial_coupon_drop_backfills_missing_distinct_coupon_and_html_unescape():
    """
    验证：
    1. Stage 3 成功转出多张独立有效券而 AI-2 仅保留其中 1 张时（如 guangdiu#30726305），
       消费端自动从 extracted_links / Stage 3 补回遗漏的独立券并保留标签；
    2. 不同 zdmcode 但同一官方 pro.m.jd.com 会场路径的重复活动页及店铺首页绝不重复/泄露入券链；
    3. extract_ztk_item_fields 正确解码淘宝/京东标题与店名中的 HTML 实体（如 &amp;）。
    """
    from infrastructure.api_governance import ResolutionKind
    from infrastructure.http_utils import extract_ztk_item_fields

    fields = extract_ztk_item_fields({
        "tao_id": "TB_HTML_1",
        "title": "佳乐滋&amp;CHIIKAWA金勺慕斯猫零食",
        "shop_title": "Pets&amp;More旗舰店",
        "quanhou_jiage": "41",
        "size": "41.2",
    })
    assert fields["title"] == "佳乐滋&CHIIKAWA金勺慕斯猫零食"
    assert fields["mall_name"] == "Pets&More旗舰店"

    active_url_1 = "https://pro.m.jd.com/mall/active/JAjtcpqK8RDvB8A4k1ZBgViBKAL/index.html?zdmcode=first111"
    active_url_dup = "https://pro.m.jd.com/mall/active/JAjtcpqK8RDvB8A4k1ZBgViBKAL/index.html?zdmcode=second222"
    direct_cpn_2 = "https://coupon.m.jd.com/coupons/show.action?linkKey=AAROH_distinct_9off"
    shop_home = "https://shop.m.jd.com/mshop/gethomepage?venderId=888999"
    main_url = "https://union-click.jd.com/jdc?e=main_huangjiu"

    cleaner = DealPoolCleaner(ai_batch_size=4, ai_max_workers=1, convert_max_workers=1)
    cleaner._cache_warmed_up = True

    def _mock_jd_univ(inp: str):
        if inp == active_url_1:
            return {
                "status": "success",
                "resolution_kind": ResolutionKind.COUPON_OR_VENUE,
                "final_link": "https://u.jd.com/oPromoCpn1",
                "short_url": "https://u.jd.com/oPromoCpn1",
                "product": None,
            }
        if inp == active_url_dup:
            return {
                "status": "success",
                "resolution_kind": ResolutionKind.COUPON_OR_VENUE,
                "final_link": "https://u.jd.com/oPromoCpnDup",
                "short_url": "https://u.jd.com/oPromoCpnDup",
                "product": None,
            }
        if inp == main_url:
            return {
                "status": "success",
                "resolution_kind": ResolutionKind.PRODUCT,
                "goods_id": "JD_HUANGJIU_1",
                "goods_sign": "JD_HUANGJIU_1",
                "final_link": "https://u.jd.com/oMainHJ",
                "promotion_token": "！MAINHJ！",
                "product": {
                    "platform": "jd",
                    "product_id": "JD_HUANGJIU_1",
                    "name": "塔牌锦丽春十年绍兴黄酒花雕酒428ml*6瓶",
                    "price": 218.0,
                    "original_price": 218.0,
                    "activity_price": 201.11,
                    "coupon_amount": 0.0,
                    "mall_name": "塔牌京东自营旗舰店",
                },
            }
        return {"status": "error"}

    cleaner.adapters = {
        "jd": SimpleNamespace(resolve_and_convert_universal=_mock_jd_univ),
        "taobao": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
        "pdd": SimpleNamespace(resolve_and_convert_universal=lambda _inp: {"status": "error"}),
    }
    cleaner.run_ai_stage1_batch = lambda *_a, **_kw: {
        "gd_30726305": {
            "main_tip_id": "gd_30726305",
            "is_standalone_deal": True,
            "merged_tip_ids": ["gd_30726305"],
            "combined_raw_text": "先领 [#L1] 和 [#L2] ( [#L3] [#L4] ) 下单 [#L5]",
            "extracted_images": [],
            "extracted_targets": [
                {"link_id": "L1", "raw_span": active_url_1, "clean_input": active_url_1, "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L2", "raw_span": direct_cpn_2, "clean_input": direct_cpn_2, "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L3", "raw_span": active_url_dup, "clean_input": active_url_dup, "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L4", "raw_span": shop_home, "clean_input": shop_home, "platform": "jd", "text_role_hint": "coupon_candidate"},
                {"link_id": "L5", "raw_span": main_url, "clean_input": main_url, "platform": "jd", "text_role_hint": "main_candidate"},
            ],
            "_from_fallback": False,
        }
    }
    # AI-2 仅保留了 L1，漏掉了独立有效券 L2
    cleaner.run_ai_stage2_batch = lambda *_a, **_kw: {
        "gd_30726305": {
            "main_tip_id": "gd_30726305",
            "is_valid_deal": True,
            "main_link_id": "L5",
            "deal_price": 150.94,
            "coupon_links": [{"link_id": "L1", "label": "满200减20元优惠券"}],
            "tutorial_steps": ["1. 先领券", "2. 下单实付150.94元"],
            "quality_score": 90,
        }
    }

    mock_col = MagicMock()
    mock_col.find.return_value = []
    res = cleaner.clean_batch_records(
        [
            {
                "source_site": "guangdiu",
                "source_channel": "latest",
                "source_item_id": "gd_30726305",
                "platform": "jd",
                "title": "塔牌 锦丽春十年 绍兴黄酒花雕酒 428ml*6瓶 券后150.94元",
                "tutorial_text": "参与满10元打9折，叠加200-20，实付150.94元",
                "extracted_links": [
                    {"label": "满200元减20元", "url": active_url_1},
                    {"label": "满10元打9折", "url": direct_cpn_2},
                    {"label": "进店逛逛", "url": shop_home},
                ],
                "extracted_tokens": [],
            }
        ],
        mock_col,
        shared_active_products={},
        quiet_sub_logs=True,
    )
    assert len(res) == 1
    assert res[0]["status"] == "success"
    cpns = res[0]["product_doc"]["deal_info"]["coupon_links"]
    assert [c["link_id"] for c in cpns] == ["L1", "L2"]
    assert cpns[0]["label"] == "满200减20元优惠券"
    assert cpns[1]["label"] == "满10元打9折"
    assert cpns[1]["url"] == direct_cpn_2





