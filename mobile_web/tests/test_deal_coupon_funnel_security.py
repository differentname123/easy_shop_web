# -*- coding: utf-8 -*-
"""
安全与回归单测：极简优惠券双门禁提取漏斗（零短链域名预设 + 100% 零他人佣金/红包外链泄露）
"""
from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

from mobile_web.scripts.run_deal_pool_scheduler import (
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

    # 2. 正常官方 88VIP 消费券 / 百亿补贴活动页：保留路径与 wh_pid，彻底剥离 tkSid / spm / ut_sk 等追踪参数
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
    #    - 折淘客 tkl_piliang 伪成功回显 http->https s.click 他人红包，被双门禁彻底拦截为 failed；
    #    - 第三方短链 yuebai.plus 转链失败后经安全解包重组为纯净官方 JD 券链 (OK_DIRECT_COUPON)。
    fake_session = _RouteSession({
        "https://s.click.taobao.com/XNl8wji": _FakeResponse(
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

    # (b) 伪成功回显他人超红短链 -> Gate 1 识破回显 + Gate 2 识破 /lianmeng/ -> 彻底判 failed
    spoof_tb = cleaner.convert_single_target({
        "link_id": "L2",
        "clean_input": "http://s.click.taobao.com/XNl8wji",
        "platform": "taobao",
        "text_role_hint": "coupon_candidate",
    })
    assert spoof_tb["convert_status"] == "failed"
    assert spoof_tb["my_promo_link"] is None

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


def test_warmup_cache_and_stage5_filter_cross_platform_and_redundant_tb_sclick():
    cleaner = DealPoolCleaner(ai_batch_size=2, ai_max_workers=1, convert_max_workers=1)

    # 1. 缓存预热测试：拒绝历史伪转链与超红残留，保留真实解包纯净券
    class _FakeWarmupCursor:
        def sort(self, *_a):
            return self

        def limit(self, *_a):
            return [{
                "converted_links": [
                    {
                        "link_id": "L_BAD_ECHO",
                        "clean_input": "http://s.click.taobao.com/XNl8wji",
                        "convert_status": "coupon_or_activity",
                        "error_code": "OK_COUPON",
                        "platform": "taobao",
                        "my_promo_link": "https://s.click.taobao.com/XNl8wji/",
                    },
                    {
                        "link_id": "L_BAD_LIANMENG",
                        "clean_input": "https://s.tb.cn/h.jScxcAd",
                        "convert_status": "coupon_or_activity",
                        "error_code": "OK_DIRECT_COUPON",
                        "platform": "taobao",
                        "my_promo_link": "https://pages.tmall.com/wow/z/lianmeng/default/route?wh_alimama=true",
                    },
                    {
                        "link_id": "L_GOOD_UNWRAP",
                        "clean_input": "https://llipin.cn/hjA2NeT",
                        "convert_status": "coupon_or_activity",
                        "error_code": "OK_DIRECT_COUPON",
                        "platform": "jd",
                        "my_promo_link": "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=r1",
                    },
                ]
            }]

    raw_col = MagicMock()
    raw_col.find.return_value = _FakeWarmupCursor()
    cleaner.warmup_anchor_cache_from_db(raw_col)
    assert "taobao:http://s.click.taobao.com/XNl8wji" not in cleaner.anchor_cache
    assert "taobao:https://s.tb.cn/h.jScxcAd" not in cleaner.anchor_cache
    assert cleaner.anchor_cache["jd:https://llipin.cn/hjA2NeT"]["my_promo_link"] == (
        "https://coupon.m.jd.com/coupons/show.action?key=k1&roleId=r1"
    )

    # 2. Stage 5 组装净化测试：
    #    - 淘宝商品自动剔除冗余单品 s.click 伪券链和跨平台京东广告尾巴，仅保留真实店铺暗券 + 88VIP 消费券会场
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
