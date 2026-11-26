# -*- coding: utf-8 -*-
"""
全新线报采集插件体系 (`services.deal_sources`) 全量离线回归测试。
================================================================================
覆盖范围（100% 离线 Mock HTTP / 无真实网络或 MongoDB）：
1. 23 频道 / 12 站点声明式配置中心与注册表一致性；
2. HTTP 会话隔离工厂 (`direct` vs `proxy`, `close_connection`) 与瞬断重试；
3. 统一门禁 (`filters`)、HTML 平衡标签解析 (`parsing`)、相对时间解析；
4. 全部 11 个采集插件类 (`XuandanSource`, `Tuan0818Source`, `MZzzdmSource`,
   `TelegramChannelSource`, `IthomeLapinSource`, `KieesSource`, `NiuzaSource`,
   `ZdzdmSource`, `BaicaioSource`, `TehuicaicaiSource`, `GuangdiuSource`, `HaodankuSource`)；
5. `deal_pipeline.run_fetch_stage` 会话关闭、策略时间覆盖、全量失效触发条件与失败记账。
================================================================================
"""
from __future__ import annotations

import datetime
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests

from models.product import SOURCE_SITE_NAMES
from repositories.deal_raw_pool_repo import parse_publish_time_epoch
from services import deal_pipeline as dp_mod
from services.deal_sources import (
    DEFAULT_CHANNEL_CONFIGS,
    BaicaioSource,
    BaseDealSource,
    ChannelConfig,
    FetchContext,
    FetchResult,
    HaodankuSource,
    IthomeLapinSource,
    KieesSource,
    MZzzdmSource,
    NiuzaSource,
    TelegramChannelSource,
    XuandanSource,
    ZdzdmSource,
    create_site_session,
    extract_balanced_tag,
    extract_div_by_id,
    get_active_channels,
    get_source,
    has_valid_detail_and_tutorial,
    is_confirmed_unsupported_deal,
    list_sources,
    request_with_retry,
)


def _mk_resp(status_code: int = 200, text: str = "", url: str = "", json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.url = url
    resp.headers = headers or {}
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


# ------------------------------------------------------------------------------
# 1. 注册表与 23 频道配置中心完整性验证
# ------------------------------------------------------------------------------
def test_catalog_and_registry_completeness():
    assert len(DEFAULT_CHANNEL_CONFIGS) == 23
    active = get_active_channels(DEFAULT_CHANNEL_CONFIGS)
    assert len(active) == 23

    names = [c.name for c in DEFAULT_CHANNEL_CONFIGS]
    assert len(set(names)) == 23

    sources = list_sources()
    for ch in DEFAULT_CHANNEL_CONFIGS:
        assert ch.resolved_source_key in sources
        assert get_source(ch.resolved_source_key) is not None
        assert ch.site_key in SOURCE_SITE_NAMES

    by_name = {c.name: c for c in DEFAULT_CHANNEL_CONFIGS}

    # #1 特惠采采
    assert by_name["特惠采采_首页精选"].interval_seconds == 1200
    assert by_name["特惠采采_首页精选"].fetch_mode == "full_sweep"
    assert by_name["特惠采采_首页精选"].use_fetch_time_as_publish_time is True
    assert by_name["特惠采采_首页精选"].expire_missing_on_sweep is True
    assert by_name["特惠采采_首页精选"].source_is_reliable is True
    assert all(
        ch.source_is_reliable is False
        for ch in DEFAULT_CHANNEL_CONFIGS
        if ch.name != "特惠采采_首页精选"
    )

    # #2 逛丢
    assert by_name["逛丢_首页实时流"].interval_seconds == 180
    assert by_name["逛丢_热门榜"].interval_seconds == 1200
    assert by_name["逛丢_小时风云榜"].interval_seconds == 1200

    # #3 好单库
    for n in ("好单库_淘宝实时线报", "好单库_京东实时线报", "好单库_淘宝百亿补贴"):
        assert by_name[n].interval_seconds == 1800
        assert by_name[n].fetch_mode == "full_sweep"
        assert by_name[n].use_fetch_time_as_publish_time is True

    # #4 选单网
    for n in ("选单网_社群文案榜", "选单网_隐藏券销量榜", "选单网_低价销量榜"):
        assert by_name[n].interval_seconds == 1800
        assert by_name[n].fetch_mode == "full_sweep"
        assert by_name[n].use_fetch_time_as_publish_time is True

    # #5 0818团
    assert by_name["0818团_最新线报"].interval_seconds == 3600
    assert by_name["0818团_最新线报"].fetch_mode == "single_page"

    # #6 值值得买
    assert by_name["值值得买_24小时热榜"].site_key == "zzzdm"
    assert by_name["值值得买_24小时热榜"].resolved_source_key == "m_zzzdm"
    assert by_name["值值得买_24小时热榜"].interval_seconds == 1800
    assert by_name["值值得买_24小时热榜"].use_fetch_time_as_publish_time is True

    # #7 #13 #14 Telegram
    assert by_name["TG_真正值得买"].lookback_seconds == 3600
    assert by_name["TG_真正值得买"].interval_seconds == 1800
    assert by_name["TG_真正值得买"].network_mode == "proxy"
    assert by_name["TG_彼岸情报"].lookback_seconds == 900
    assert by_name["TG_彼岸情报"].interval_seconds == 600
    assert by_name["TG_彼岸情报"].network_mode == "proxy"
    assert by_name["TG_福利资源"].lookback_seconds == 900
    assert by_name["TG_福利资源"].interval_seconds == 600
    assert by_name["TG_福利资源"].network_mode == "proxy"

    # #8 IT之家辣品
    assert by_name["IT之家辣品_好物"].interval_seconds == 3600
    assert by_name["IT之家辣品_好物"].lookback_seconds == 172800

    # #9 #10 发现值得买 & 牛杂网
    for n in ("发现值得买_首页", "发现值得买_24H热榜", "牛杂网_首页", "牛杂网_24H热榜"):
        assert by_name[n].close_connection is True
        assert by_name[n].use_fetch_time_as_publish_time is True
        assert by_name[n].interval_seconds == 1800

    # #11 真的值得买
    assert by_name["真的值得买_国内优惠"].interval_seconds == 1800
    assert by_name["真的值得买_国内优惠"].fetch_mode == "single_page"

    # #12 白菜哦
    assert by_name["白菜哦_编辑推荐"].fetch_mode == "single_page"
    assert by_name["白菜哦_编辑推荐"].use_fetch_time_as_publish_time is False
    assert by_name["白菜哦_小时榜"].fetch_mode == "full_sweep"
    assert by_name["白菜哦_小时榜"].use_fetch_time_as_publish_time is True


# ------------------------------------------------------------------------------
# 2. HTTP 客户端、通用解析与门禁边界验证
# ------------------------------------------------------------------------------
def test_http_client_session_isolation_and_retry(monkeypatch):
    s_direct = create_site_session(network_mode="direct", close_connection=False)
    try:
        assert s_direct.trust_env is False
        assert s_direct.headers.get("Connection") != "close"
    finally:
        s_direct.close()

    s_proxy = create_site_session(network_mode="proxy", close_connection=True)
    try:
        assert s_proxy.trust_env is True
        assert s_proxy.headers.get("Connection") == "close"
    finally:
        s_proxy.close()

    s_explicit = create_site_session(network_mode="proxy", proxy="http://127.0.0.1:7890")
    try:
        assert s_explicit.trust_env is False
        assert s_explicit.proxies["https"] == "http://127.0.0.1:7890"
    finally:
        s_explicit.close()

    calls = {"n": 0}
    mock_sess = MagicMock()

    def flaky_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("RemoteDisconnected")
        return _mk_resp(200, "ok")

    mock_sess.get.side_effect = flaky_get
    r = request_with_retry(mock_sess, "get", "https://example.com", retries=1, backoff=0.0)
    assert r.status_code == 200
    assert calls["n"] == 2

    # HTTP 404 不应触发重试
    calls["n"] = 0
    mock_sess.get.side_effect = lambda url, **kw: (calls.__setitem__("n", calls["n"] + 1) or _mk_resp(404, "not found"))
    r404 = request_with_retry(mock_sess, "get", "https://example.com", retries=2, backoff=0.0)
    assert r404.status_code == 404
    assert calls["n"] == 1


def test_filters_parsing_and_publish_time_variants():
    # 自站链接（guangdiu/go.ruanmei/t.me/zdzdm）不算有效外部电商锚点
    shell_rec = {
        "has_full_detail": True,
        "tutorial_text": "领券立减20元实付19.9",
        "extracted_links": [
            {"url": "https://go.ruanmei.com/url.aspx?linkid=797"},
            {"url": "https://t.me/s/Pojieapp"},
            {"url": "https://www.zdzdm.com/p/1"},
        ],
        "extracted_tokens": [],
    }
    assert has_valid_detail_and_tutorial(shell_rec) is False

    # 嵌套 balanced tag 与 extract_div_by_id
    html_sample = (
        '<div id="wrap"><div id="dabstract"><div class="inner">步骤1</div><span>步骤2</span></div>'
        '<div id="sidebar">侧边栏</div></div>'
    )
    extracted = extract_div_by_id(html_sample, "dabstract")
    assert "步骤1" in extracted and "步骤2" in extracted and "侧边栏" not in extracted
    assert "侧边栏" in extract_balanced_tag(html_sample, "div", r'id="sidebar"')

    # 复合相对时间与纯时分格式解析
    now = time.time()
    ts_comp = parse_publish_time_epoch("1 小时15分钟前")
    assert ts_comp is not None and 4450 <= (now - ts_comp) <= 4550
    ts_space_min = parse_publish_time_epoch("20 分钟前")
    assert ts_space_min is not None and 1150 <= (now - ts_space_min) <= 1250
    ts_hhmm = parse_publish_time_epoch("08:30")
    assert ts_hhmm is not None and abs(now - ts_hhmm) <= 86400 + 60


# ------------------------------------------------------------------------------
# 3. 各新增光源插件行为单元测试
# ------------------------------------------------------------------------------
def test_xuandan_source_inline_parsing_and_pagination():
    p1_html = """
    <div class="card">
        <a href="/Detail.html?id=501"><img src="/GIF16.gif" data-original="//img.alicdn.com/501.jpg"></a>
        <div class="liLayer hide" id="go_501">
            蒙牛纯牛奶250ml*24盒<br>
            原价：69.9元<br>
            领券: <a href="https://uland.taobao.com/quan/detail?sellerId=1&activityId=2">领券</a><br>
            抢购: <a href="https://uland.taobao.com/item/edetail?id=abc12345">下单</a>
        </div>
    </div>
    <div class="card">
        <a href="/Detail.html?id=502"><img src="https://img.alicdn.com/502.jpg"></a>
        <div class="liLayer hide" id="go_502">已知历史商品</div>
    </div>
    """
    p2_html = """
    <div class="card">
        <a href="/Detail.html?id=503"><img data-original="https://img.alicdn.com/503.jpg"></a>
        <div class="liLayer hide" id="go_503">
            维达抽纸24包<br>拍下实付29.9元<br>
            下单：<a href="https://uland.taobao.com/item/edetail?id=xyz999">直达</a>
        </div>
    </div>
    """
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "pageid=2" in url:
            return _mk_resp(200, p2_html)
        if "pageid=3" in url:
            return _mk_resp(200, p2_html)  # 重复页自动止步
        return _mk_resp(200, p1_html)

    sess = MagicMock()
    sess.get.side_effect = fake_get

    ch = ChannelConfig(
        name="选单网_社群文案榜",
        site_key="xuandan",
        default_url="https://www.xuandan.com/allList.html?type=8",
        params={"type_id": 8},
        fetch_mode="full_sweep",
        max_pages=5,
        detail_delay_seconds=0.0,
    )
    res = XuandanSource().fetch(FetchContext(channel=ch, session=sess, known_ids={"502"})).to_dict()
    assert res["status"] == "ok"
    assert res["raw_list_count"] == 3
    assert res["skipped_known"] == 1
    assert set(res["touch_ids"]) == {"501", "502", "503"}
    assert len(res["records"]) == 2
    assert len(calls) == 3  # 第3页全重复即停止，且零详情请求
    rec501 = res["records"][0]
    assert rec501["platform"] == "taobao"
    assert rec501["image_url"] == "https://img.alicdn.com/501.jpg"
    assert "GIF16.gif" not in rec501["image_url"]
    assert any("uland.taobao.com/item/edetail" in lk["url"] for lk in rec501["extracted_links"])


def test_m_zzzdm_source_isolates_red_envelope_banner():
    rank_html = """
    <a href="/posts/view?sid=s101" class="rank-card">
        <img src="https://img.test/101.jpg" alt="京东 蓝月亮洗衣液 6斤">
        <span>🏬 京东</span>
        <span>￥29.9</span>
    </a>
    <a href="/posts/view?sid=s102" class="rank-card">
        <img src="https://img.test/102.jpg" alt="抖音电商 零食大礼包">
        <span>🏬 抖音</span>
    </a>
    """
    det_html = """
    <html><body>
        <!-- 顶部全站红包广告条（绝不可被当作该商品优惠券） -->
        <section>
            <h2>🎉 淘宝京东双11红包</h2>
            <a href="https://u.jd.com/FGpTvZF">领京东红包</a>
            <a href="https://m.tb.cn/h.jbIMh5M">领淘宝红包</a>
        </section>
        <!-- 优惠券 -->
        <section>
            <h3>🏷️ 优惠券</h3>
            <a href="https://u.jd.com/REALCOUPON1">领取满50减20券</a>
        </section>
        <!-- 优惠详情 -->
        <section>
            <h3>优惠详情</h3>
            <p>京东自营到手29.9元，拍1件立减。</p>
            <p><strong>双11惊喜大礼盒：</strong>每天可领三次</p>
        </section>
        <a href="https://u.jd.com/REALBUY1">🛒 立即购买</a>
    </body></html>
    """
    sess = MagicMock()
    sess.get.side_effect = lambda url, **kw: _mk_resp(200, det_html if "view?sid=s101" in url else rank_html)

    ch = ChannelConfig(
        name="值值得买_24小时热榜",
        site_key="zzzdm",
        source_key="m_zzzdm",
        default_url="https://m.zzzdm.com/posts/ranking.html",
        fetch_mode="full_sweep",
        max_details=10,
        detail_delay_seconds=0.0,
    )
    res = MZzzdmSource().fetch(FetchContext(channel=ch, session=sess)).to_dict()
    assert res["status"] == "ok"
    assert res["skipped_unsupported"] == 1
    assert len(res["records"]) == 1
    rec = res["records"][0]
    assert rec["source_site"] == "zzzdm"
    urls = [lk["url"] for lk in rec["extracted_links"]]
    assert "https://u.jd.com/REALBUY1" in urls
    assert "https://u.jd.com/REALCOUPON1" in urls
    assert "https://u.jd.com/FGpTvZF" not in urls
    assert "https://m.tb.cn/h.jbIMh5M" not in urls
    assert "双11惊喜大礼盒" not in rec["tutorial_text"]


def test_telegram_source_time_window_photos_and_filters():
    now_ts = 1_760_000_000.0
    t_recent = datetime.datetime.fromtimestamp(now_ts - 300, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    t_old = datetime.datetime.fromtimestamp(now_ts - 7200, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    tg_html = f"""
    <section class="tgme_channel_history">
      <div class="tgme_widget_message_wrap" data-post="zzzdm/100">
        <img class="tgme_widget_message_user_photo" src="https://cdn.t.me/avatar.jpg">
        <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.t.me/prod100.jpg')"></a>
        <div class="tgme_widget_message_text">【京东】维达抽纸24包\n价格：29.9元\n商家：[京东自营]\n<a href="https://u.jd.com/TG100">立即购买</a></div>
        <time datetime="{t_recent}"></time>
      </div>
      <div class="tgme_widget_message_wrap" data-post="zzzdm/101">
        <div class="tgme_widget_message_text">今日福利夸克网盘资源分享 <a href="https://pan.quark.cn/s/abc">下载</a></div>
        <time datetime="{t_recent}"></time>
      </div>
      <div class="tgme_widget_message_wrap" data-post="zzzdm/99">
        <div class="tgme_widget_message_text">超窗旧消息 <a href="https://u.jd.com/OLD99">购买</a></div>
        <time datetime="{t_old}"></time>
      </div>
    </section>
    """
    sess = MagicMock()
    sess.get.return_value = _mk_resp(200, tg_html)

    ch = ChannelConfig(
        name="TG_真正值得买",
        site_key="telegram",
        default_url="https://t.me/s/zzzdm",
        params={"tg_channel": "zzzdm"},
        fetch_mode="time_window",
        lookback_seconds=3600,
        max_pages=3,
        network_mode="proxy",
    )
    res = TelegramChannelSource().fetch(FetchContext(channel=ch, session=sess, now_ts=now_ts)).to_dict()
    assert res["status"] == "ok"
    assert res["raw_list_count"] == 3
    assert res["skipped_unsupported"] == 1  # pan.quark.cn
    assert res["skipped_out_of_window"] == 1  # t_old
    assert len(res["records"]) == 1
    rec = res["records"][0]
    assert rec["source_item_id"] == "zzzdm_100"
    assert rec["image_url"] == "https://cdn.t.me/prod100.jpg"
    assert "avatar.jpg" not in rec["tutorial_images"]


def test_ithome_lapin_ajax_pagination_and_app_link_exclusion():
    now_ts = 1_760_000_000.0
    cn_tz = datetime.timezone(datetime.timedelta(hours=8))
    ot1 = datetime.datetime.fromtimestamp(now_ts - 3600, tz=cn_tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    ot2 = datetime.datetime.fromtimestamp(now_ts - 7200, tz=cn_tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    page1_html = f"""
    <ul class="bl">
      <li data-ot="{ot1}">
        <a class="img" href="/0/800/111.htm"><img data-original="https://img.ithome.com/111.jpg"></a>
        <a class="title" href="/0/800/111.htm">京东精选好物 29.9元</a>
      </li>
    </ul>
    """
    more_html = f"""
    <li data-ot="{ot2}">
      <a class="img" href="/0/800/222.htm"><img src="https://img.ithome.com/222.jpg"></a>
      <a class="title" href="/0/800/222.htm">天猫精选好物 19.9元</a>
    </li>
    """
    det_html_111 = """
    <div id="paragraph">
      <p>满100减50元，领券下单实付29.9元：<a href="https://u.jd.com/IT111">点此购买</a></p>
      <p><a href="https://go.ruanmei.com/url.aspx?linkid=797">下载辣品App</a></p>
      <p class="ad-tips">广告声明：文内含对外跳转链接</p>
    </div>
    """
    det_html_222 = """
    <div id="paragraph">
      <p>立减10元实付19.9元包邮：<a href="https://uland.taobao.com/item/edetail?id=IT222">点此领券</a></p>
    </div>
    """

    sess = MagicMock()
    sess.get.side_effect = lambda url, **kw: _mk_resp(
        200,
        det_html_111 if "111.htm" in url else (det_html_222 if "222.htm" in url else page1_html),
    )
    sess.post.return_value = _mk_resp(200, json_data={"success": True, "content": {"count": 1, "html": more_html}})

    ch = ChannelConfig(
        name="IT之家辣品_好物",
        site_key="ithome_lapin",
        default_url="https://digi.ithome.com/lapin",
        fetch_mode="time_window",
        lookback_seconds=172800,
        max_pages=2,
        max_details=10,
        detail_delay_seconds=0.0,
    )
    res = IthomeLapinSource().fetch(FetchContext(channel=ch, session=sess, now_ts=now_ts)).to_dict()
    assert res["status"] == "ok"
    assert res["pages_crawled"] == 2
    assert len(res["records"]) == 2
    urls_111 = [lk["url"] for lk in res["records"][0]["extracted_links"]]
    assert "https://u.jd.com/IT111" in urls_111
    assert not any("go.ruanmei.com" in u for u in urls_111)
    assert "广告声明" not in res["records"][0]["tutorial_text"]


def test_kiees_and_niuza_empire_deal_sources():
    index_html = """
    <div id="home-left">
      <div class="postbox">
        <h2><a href="/2019/05/01/991.html">老爆款回炉牛奶 <span>29.9元</span></a></h2>
        <img src="https://img.test/991.jpg">
        <a class="imlink_gobuy" href="https://u.jd.com/KIEES991">直达</a>
      </div>
      <div class="postbox">
        <h2><a href="/2024/01/01/992.html">苏宁易购特价电视 <span>999元</span></a></h2>
        <a class="imlink_gobuy" href="https://product.suning.com/992.html">直达</a>
      </div>
    </div>
    <div id="sidebar">侧边栏</div>
    """
    det_html = """
    <html><body>
      <div id="sidebar"><a href="https://s.click.taobao.com/SIDEBAR_NOISE">广告</a></div>
      <div class="postsingle">
        <a id="shop_url" class="tracklink" href="https://u.jd.com/KIEES991">购买链接</a>
        <div class="entry-content">满59减30元，plus实付29.9元包邮</div>
      </div>
    </body></html>
    """
    hot_p1 = """
    <ul>
      <li class="list">
        <h2><a href="/2025/02/02/881.html">牛杂热榜商品 <span>19.9元</span></a></h2>
        <img src="https://img.test/881.jpg">
        <a class="imlink_gobuy" href="https://uland.taobao.com/item/edetail?id=NIUZA881">购买</a>
      </li>
    </ul>
    """
    sess = MagicMock()
    sess.get.side_effect = lambda url, **kw: _mk_resp(
        200,
        det_html if url.endswith(".html") and "/20" in url else (index_html if "kiees.com" in url else hot_p1),
    )

    ch_kiees = ChannelConfig(
        name="发现值得买_首页",
        site_key="kiees",
        default_url="http://www.kiees.com/index.html",
        params={"mode": "index"},
        fetch_mode="full_sweep",
        use_fetch_time_as_publish_time=True,
        max_pages=1,
        max_details=10,
        detail_delay_seconds=0.0,
        close_connection=True,
    )
    res_k = KieesSource().fetch(FetchContext(channel=ch_kiees, session=sess)).to_dict()
    assert res_k["status"] == "ok"
    assert res_k["skipped_unsupported"] == 1  # suning.com filtered
    assert len(res_k["records"]) == 1
    assert isinstance(res_k["records"][0]["publish_ts"], float)
    assert res_k["records"][0]["publish_ts"] > 1_700_000_000
    assert "publish_time" not in res_k["records"][0]
    assert not any("SIDEBAR_NOISE" in lk["url"] for lk in res_k["records"][0]["extracted_links"])

    ch_niuza = ChannelConfig(
        name="牛杂网_24H热榜",
        site_key="niuza",
        default_url="https://www.niuza.com/hot_1",
        params={"mode": "hot_1"},
        fetch_mode="full_sweep",
        use_fetch_time_as_publish_time=True,
        max_pages=1,
        max_details=10,
        detail_delay_seconds=0.0,
        close_connection=True,
    )
    res_n = NiuzaSource().fetch(FetchContext(channel=ch_niuza, session=sess)).to_dict()
    assert res_n["status"] == "ok"
    assert len(res_n["records"]) == 1
    assert res_n["records"][0]["source_site"] == "niuza"


def test_zdzdm_and_baicaio_sources_scope_and_302_unwrap():
    # 1. ZdzdmSource: 仅解析 <article> 并过滤非淘京拼来源 + 隔离右侧边栏广告
    zdz_list = """
    <article class="zd-deal"><a href="/p/71"><h2 class="zd-deal-title">舒肤佳香皂<span class="zd-content-price">12.9元</span></h2><img src="https://img.test/71.jpg"></a></article>
    <article class="zd-deal"><a href="/p/72"><h2 class="zd-deal-title">苏宁电器<span class="zd-content-price">299元</span></h2></a></article>
    """
    zdz_det_71 = """
    <main>
      <article>
        <span class="zd-article-origin">来源：京东自营</span>
        <time datetime="2026-11-20T10:00:00+08:00"></time>
        <div class="zd-article-actions"><a href="https://u.jd.com/ZDZ71">前往购买&gt;</a></div>
        <div class="zd-rich">满20减7元券后12.9元包邮</div>
      </article>
      <aside class="sidebar"><a href="https://s.click.taobao.com/SIDEBAR_AD">右侧推广广告</a></aside>
    </main>
    """
    zdz_det_72 = "<article><span class=\"zd-article-origin\">来源：苏宁易购</span><div class=\"zd-rich\">优惠描述</div></article>"

    sess_zdz = MagicMock()
    sess_zdz.get.side_effect = lambda url, **kw: _mk_resp(
        200, zdz_det_71 if "/p/71" in url else (zdz_det_72 if "/p/72" in url else zdz_list)
    )
    ch_zdz = ChannelConfig(
        name="真的值得买_国内优惠",
        site_key="zdzdm",
        default_url="https://www.zdzdm.com/youhui",
        max_details=10,
        detail_delay_seconds=0.0,
    )
    res_z = ZdzdmSource().fetch(FetchContext(channel=ch_zdz, session=sess_zdz)).to_dict()
    assert res_z["status"] == "ok"
    assert res_z["skipped_unsupported"] == 1
    assert len(res_z["records"]) == 1
    assert not any("SIDEBAR_AD" in lk["url"] for lk in res_z["records"][0]["extracted_links"])
    assert any("u.jd.com/ZDZ71" in lk["url"] for lk in res_z["records"][0]["extracted_links"])

    # 2. BaicaioSource: 忽略导航 orig-show-id-3，兼容省略 </li> 的多条 /nice/，提取 biaoqian 真实商城并解包 tgo 302 跳转
    bc_nice_list = """
    <ul class="J_item">
      <li class="item"><a href="https://www.baicaio.com/item/301.html"><img src="https://img.test/301.jpg"></a><div class="title"><a>清风卷纸</a></div><div class="user"><span>5分钟前</span></div>
      <li class="item"><a href="https://www.baicaio.com/item/302.html"></a><div class="title"><a>唯品会背包</a></div></li>
    </ul>
    """
    bc_hour_empty = """
    <ul class="list mt-20"><!--列表循环start--><!--列表循环end--></ul>
    <div class="mt-40 hour-page"><a href="https://www.baicaio.com/hour-index-hour-20261120220000">前一小时</a></div>
    """
    bc_hour_prev = """
    <ul class="list mt-20">
      <li class="clearfix">
        <a href="https://www.baicaio.com/item/301.html" title="小时榜清风卷纸"><img src="https://img.test/301.jpg"></a>
        <p class="fc-red">￥19.9</p>
      </li>
    </ul>
    """
    bc_det_301 = """
    <nav><a href="https://www.baicaio.com/orig-show-id-3">天猫商城（全站固定导航）</a></nav>
    <h1>清风卷纸 <span class="fc-red">19.9元</span></h1>
    <div class="mt-15 biaoqian"><a title="商城：京东商城">京东</a></div>
    <span>发表时间：2026-11-20 12:00</span>
    <a class="btn-3" href="/?m=item&a=tgo&to=abc">去购买</a>
    <!--正文start-->
    <p>领券满29减10元，实付19.9元：<a href="/?m=item&a=tgo&to=abc">购买</a></p>
    <!--正文end-->
    """
    bc_det_302 = """
    <h1>唯品会背包</h1>
    <div class="mt-15 biaoqian"><a title="商城：唯品会">唯品会</a></div>
    <!--正文start--><p>特卖</p><!--正文end-->
    """

    def fake_bc_get(url, **kw):
        if "a=tgo" in url:
            return _mk_resp(302, "", headers={"Location": "https://u.jd.com/BAICAIO_REAL"})
        if "301.html" in url:
            return _mk_resp(200, bc_det_301)
        if "302.html" in url:
            return _mk_resp(200, bc_det_302)
        if "hour-index-hour-" in url:
            return _mk_resp(200, bc_hour_prev)
        if url.endswith("/hour/"):
            return _mk_resp(200, bc_hour_empty)
        return _mk_resp(200, bc_nice_list)

    sess_bc = MagicMock()
    sess_bc.get.side_effect = fake_bc_get
    ch_bc = ChannelConfig(
        name="白菜哦_编辑推荐",
        site_key="baicaio",
        default_url="https://www.baicaio.com/nice/",
        params={"mode": "nice"},
        max_details=10,
        detail_delay_seconds=0.0,
    )
    res_bc = BaicaioSource().fetch(FetchContext(channel=ch_bc, session=sess_bc)).to_dict()
    assert res_bc["status"] == "ok"
    assert res_bc["raw_list_count"] == 2
    assert res_bc["skipped_unsupported"] == 1
    assert len(res_bc["records"]) == 1
    rec_bc = res_bc["records"][0]
    assert rec_bc["mall_name"] == "京东商城"
    assert any(lk["url"] == "https://u.jd.com/BAICAIO_REAL" for lk in rec_bc["extracted_links"])
    assert not any("a=tgo" in lk["url"] for lk in rec_bc["extracted_links"])

    # 验证 /hour/ 在当前小时无数据时自动回溯「前一小时」榜单
    ch_hour = ChannelConfig(
        name="白菜哦_小时榜",
        site_key="baicaio",
        default_url="https://www.baicaio.com/hour/",
        params={"mode": "hour"},
        fetch_mode="full_sweep",
        use_fetch_time_as_publish_time=True,
        max_details=10,
        detail_delay_seconds=0.0,
    )
    res_hour = BaicaioSource().fetch(FetchContext(channel=ch_hour, session=sess_bc)).to_dict()
    assert res_hour["status"] == "ok"
    assert res_hour["pages_crawled"] == 2
    assert len(res_hour["records"]) == 1
    assert isinstance(res_hour["records"][0]["publish_ts"], float)
    assert res_hour["records"][0]["publish_ts"] > 1_700_000_000
    assert "publish_time" not in res_hour["records"][0]

    # 验证合法空小时页面（无“前一小时”链接时）亦保持 status="ok" 不误报异常
    sess_empty_hour = MagicMock()
    sess_empty_hour.get.return_value = _mk_resp(200, '<ul class="list mt-20"></ul>')
    res_empty = BaicaioSource().fetch(FetchContext(channel=ch_hour, session=sess_empty_hour)).to_dict()
    assert res_empty["status"] == "ok"
    assert res_empty["records"] == []


# ------------------------------------------------------------------------------
# 4. 调度管道策略闭环与孤立会话验证
# ------------------------------------------------------------------------------
def _patch_pipeline_log_paths(monkeypatch, tmp_path) -> None:
    import sys

    for mod_name in ("mobile_web.services.deal_pipeline", "services.deal_pipeline"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        monkeypatch.setattr(mod.time, "sleep", lambda _: None, raising=False)
        monkeypatch.setattr(mod, "STATS_SNAPSHOT_PATH", str(tmp_path / "stats.json"), raising=False)
        monkeypatch.setattr(mod, "LATEST_OPS_REPORT_PATH", str(tmp_path / "ops.json"), raising=False)
        monkeypatch.setattr(mod, "OPS_REPORT_HISTORY_JSONL", str(tmp_path / "ops_hist.jsonl"), raising=False)
        monkeypatch.setattr(mod, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"), raising=False)
    for repo_mod_name in ("mobile_web.repositories.deal_raw_pool_repo", "repositories.deal_raw_pool_repo"):
        rmod = sys.modules.get(repo_mod_name)
        if rmod is not None:
            monkeypatch.setattr(rmod, "ERROR_LOG_JSONL_PATH", str(tmp_path / "errors.jsonl"), raising=False)


def test_pipeline_isolated_sessions_policy_flags_and_error_tracking(monkeypatch, tmp_path):
    _patch_pipeline_log_paths(monkeypatch, tmp_path)

    created_sessions = []

    def fake_factory(ch: ChannelConfig):
        s = MagicMock()
        s.network_mode = ch.network_mode
        s.close_connection = ch.close_connection
        created_sessions.append(s)
        return s

    class _SweepOkSource(BaseDealSource):
        site_key = "kiees"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                status="ok",
                http_status=200,
                request_url=ctx.channel.default_url,
                elapsed_ms=2.0,
                raw_list_count=2,
                sweep_completed=True,
                touch_ids=["k1"],
                records=[{
                    "source_site": "kiees",
                    "source_item_id": "k2",
                    "title": "好价测试品",
                    "publish_time": "2019-01-01 00:00:00",  # 应被策略时间覆盖
                    "tutorial_text": "领券拍下立减20元实付9.9元",
                    "extracted_links": [{"url": "https://u.jd.com/k2"}],
                    "has_full_detail": True,
                }],
            )

    class _FailNoRateLimitSource(BaseDealSource):
        site_key = "zdzdm"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                status="error",
                http_status=500,
                request_url=ctx.channel.default_url,
                elapsed_ms=1.0,
                sweep_completed=True,  # 即使返回 True，status="error" 也不得触发缺失淘汰
                rate_limited=False,
                detail_errors=[{"error": "HTTP 500", "http_status": 500, "error_type": "HTTP_500"}],
            )

    channels = [
        ChannelConfig(
            name="发现值得买_全量策略测试",
            site_key="kiees",
            default_url="http://www.kiees.com/index.html",
            fetch_mode="full_sweep",
            use_fetch_time_as_publish_time=True,
            expire_missing_on_sweep=True,
            max_age_days=5.0,
            close_connection=True,
            sleep_after=0.0,
        ),
        ChannelConfig(
            name="真的值得买_500失败测试",
            site_key="zdzdm",
            default_url="https://www.zdzdm.com/youhui",
            expire_missing_on_sweep=True,
            sleep_after=0.0,
        ),
    ]

    touch_calls = []
    save_calls = []
    expire_calls = []

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.generate_pool_summary.return_value = {"total_records": 1}
    repo.touch_seen.side_effect = lambda *a, **kw: touch_calls.append((a, kw)) or 1
    repo.save_batch.side_effect = lambda recs, **kw: (save_calls.append((recs, kw)) or {"inserted": len(recs), "updated": 0})
    repo.expire_missing_from_full_sweep.side_effect = lambda **kw: expire_calls.append(kw) or {"raw_expired": 0, "formal_expired": 0}

    out = dp_mod.run_fetch_stage(
        repo=repo,
        session=None,
        cycle_num=1,
        channels=channels,
        channel_last_run={},
        source_registry={
            "kiees": _SweepOkSource(),
            "zdzdm": _FailNoRateLimitSource(),
        },
        session_factory=fake_factory,
    )

    # 1. 验证每个频道创建独立 Session 并确定性关闭
    assert len(created_sessions) == 2
    assert all(s.close.call_count == 1 for s in created_sessions)

    # 2. 验证 use_fetch_time_as_publish_time 覆盖时间并向 touch_seen/save_batch 透传策略参数
    saved_recs, saved_kw = next((r, k) for r, k in save_calls if r)
    assert isinstance(saved_recs[0]["publish_ts"], float)
    assert saved_recs[0]["publish_ts"] > 1_700_000_000
    assert "publish_time" not in saved_recs[0]
    assert saved_kw["refresh_publish_time"] is True
    assert saved_kw["max_age_days"] == 5.0
    assert touch_calls[0][1]["refresh_publish_time"] is True
    assert touch_calls[0][1]["max_age_days"] == 5.0

    # 3. 只有全量成功频道触发 expire_missing_from_full_sweep；500 失败频道绝不误杀旧品
    assert len(expire_calls) == 1
    assert expire_calls[0]["channel"] == "发现值得买_全量策略测试"

    # 4. 非限流 status="error" 正确汇报为 error 并累计连续失败
    by_task = {t["task_name"]: t for t in out["tasks"]}
    assert by_task["真的值得买_500失败测试"]["status"] == "error"
    assert by_task["真的值得买_500失败测试"]["consecutive_failures"] == 1


# ------------------------------------------------------------------------------
# 5. 收口整合回归验证（平台门禁 / 特惠采采 / 逛丢 / 好单库 / 并发调度 / 重置清洗库）
# ------------------------------------------------------------------------------
def test_confirmed_unsupported_platform_filter_prevents_false_kills():
    assert is_confirmed_unsupported_deal(platform_code="vip") is True
    assert is_confirmed_unsupported_deal(mall_name="美团") is True
    assert is_confirmed_unsupported_deal(mall_name="唯品会") is True
    assert is_confirmed_unsupported_deal(mall_name="亚马逊中国") is True
    assert is_confirmed_unsupported_deal(
        mall_name="",
        links=[{"url": "https://iguangdiu.com/to.php?u=https%3A%2F%2Fclick.meituan.com%2Ft%3Ft%3D9"}],
    ) is True

    assert is_confirmed_unsupported_deal(platform_code="jd") is False
    assert is_confirmed_unsupported_deal(platform_code="pdd") is False
    assert is_confirmed_unsupported_deal(platform_code="tb") is False
    assert is_confirmed_unsupported_deal(mall_name="京东自营") is False
    assert is_confirmed_unsupported_deal(mall_name="天猫超市") is False
    assert is_confirmed_unsupported_deal(
        mall_name="肯德基官方旗舰店",
        links=[{"url": "https://s.click.taobao.com/t?e=1"}],
    ) is False

    # 未知商城或未知第三方短链绝不能误杀
    assert is_confirmed_unsupported_deal(
        mall_name="某品牌官方商城",
        links=[{"url": "https://llipin.cn/3IVaPCR"}],
    ) is False
    assert is_confirmed_unsupported_deal(mall_name="", links=[{"url": "https://u.jd.com/oONAYrQ"}]) is False


def test_tehuicaicai_skips_known_drops_shell_full_sweep_and_watermark():
    from services.deal_sources import TehuicaicaiSource

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

    def fake_get(url, headers=None, timeout=15.0):
        if "home-products" in url:
            return _mk_resp(200, json_data=list_payload)
        if "/products/1003/" in url:
            detail_called.append("1003")
            return _mk_resp(200, json_data={
                "code": "OK",
                "data": {"id": 1003, "title": "无详情空壳商品", "platform_code": "jd", "material_info": {}},
            })
        if "/products/1004/" in url:
            detail_called.append("1004")
            return _mk_resp(200, json_data={
                "code": "OK",
                "data": {
                    "id": 1004,
                    "title": "优质牛奶",
                    "platform_code": "jd",
                    "platform_name": "京东",
                    "material_info": {
                        "command_text": "领劵：https://u.jd.com///oaNLA4B\n【京东】优质牛奶\n下单：https://u.jd.com/oaNj0X8\n数量1 付29.9",
                        "purchase_url": "https://u.jd.com/oaNj0X8",
                        "coupon_urls": ["https://u.jd.com/oaNLA4B"],
                    },
                },
            })
        return _mk_resp(404)

    sess = MagicMock()
    sess.get.side_effect = fake_get
    ch = ChannelConfig(
        name="特惠采采_首页精选",
        site_key="tehuicaicai",
        default_url="https://xhhcds.cn/",
        params={"channel": "home", "page_size": 50, "auto_paginate": True},
        fetch_mode="full_sweep",
        max_pages=5,
        max_details=10,
        detail_delay_seconds=0.0,
    )
    res = TehuicaicaiSource().fetch(FetchContext(channel=ch, session=sess, known_ids={"1001"})).to_dict()
    assert res["status"] == "ok"
    assert detail_called == ["1003", "1004"]
    assert res["skipped_known"] == 1
    assert res["skipped_unsupported"] == 1
    assert res["skipped_no_detail"] == 1
    assert len(res["records"]) == 1
    urls = [lk["url"] for lk in res["records"][0]["extracted_links"]]
    assert "https://u.jd.com/oaNLA4B" in urls
    assert "https://u.jd.com/oaNj0X8" in urls

    # 全量巡视配额为 0 亦翻至末页回传 touch_ids；single_page 水位线全已知则第 1 页早停
    p1 = [{"id": 2001, "title": "已知商品A", "platform_code": "jd", "platform_name": "京东"}, {"id": 2002, "title": "新商品B", "platform_code": "jd", "platform_name": "京东"}]
    p2 = [{"id": 2003, "title": "新商品C", "platform_code": "jd", "platform_name": "京东"}]
    requested_pages = []

    def fake_page_get(url, headers=None, timeout=15.0):
        if "page=1" in url:
            requested_pages.append(1)
            return _mk_resp(200, json_data={"data": {"items": p1, "pagination": {"page": 1, "has_next": True}}})
        requested_pages.append(2)
        return _mk_resp(200, json_data={"data": {"items": p2, "pagination": {"page": 2, "has_next": False}}})

    sess2 = MagicMock()
    sess2.get.side_effect = fake_page_get
    res_full = TehuicaicaiSource().fetch(
        FetchContext(
            channel=ChannelConfig(
                name="特惠采采_全量", site_key="tehuicaicai", default_url="https://xhhcds.cn/",
                params={"channel": "home", "auto_paginate": True}, fetch_mode="full_sweep",
                max_pages=10, max_details=0, detail_delay_seconds=0.0,
            ),
            session=sess2,
            known_ids={"2001"},
        )
    ).to_dict()
    assert requested_pages == [1, 2]
    assert res_full["sweep_completed"] is True
    assert res_full["touch_ids"] == ["2001"]

    requested_pages.clear()
    res_early = TehuicaicaiSource().fetch(
        FetchContext(
            channel=ChannelConfig(
                name="特惠采采_早停", site_key="tehuicaicai", default_url="https://xhhcds.cn/",
                params={"channel": "home", "auto_paginate": True}, fetch_mode="single_page",
                max_pages=5, max_details=10, detail_delay_seconds=0.0,
            ),
            session=sess2,
            known_ids={"2001", "2002"},
        )
    ).to_dict()
    assert requested_pages == [1]
    assert res_early["records"] == []


def test_guangdiu_dabstract_filtering_and_relative_gophp_unpack():
    from services.deal_sources import GuangdiuSource

    list_html = """
    <div class="gooditem"><a class="goodname" href="detail.php?id=101">已知商品</a><a class="rightmallname">京东</a></div>
    <div class="gooditem"><a class="goodname" href="detail.php?id=102">海底捞</a><a class="rightmallname">美团</a></div>
    <div class="gooditem"><a class="goodname" href="detail.php?id=103">亚马逊</a><a class="innergototobuybtn" href="https://iguangdiu.com/to.php?u=https%3A%2F%2Fwww.amazon.cn%2Fdp%2FB001">直达</a></div>
    <div class="gooditem"><a class="goodname" href="detail.php?id=104">纯图空壳</a><a class="rightmallname">天猫</a></div>
    <div class="gooditem"><a class="goodname" href="detail.php?id=105">相对链商品</a><a class="rightmallname">京东</a><a class="innergototobuybtn" href="go.php?id=105">直达</a></div>
    """
    called_urls = []

    def fake_get(url, headers=None, timeout=15.0):
        called_urls.append(url)
        if "detail.php?id=104" in url:
            return _mk_resp(200, '<div class="dabstract" id="dabstract"><img src="https://img.test/c.jpg"/></div>', url=url)
        if "detail.php?id=105" in url:
            return _mk_resp(
                200,
                '<span class="dinfoweibo"><span class="latesttime">刚刚</span></span>'
                '<div class="dabstract" id="dabstract"><div class="steps"><div>1. 领取满100减40券</div>'
                '<div>2. 实付59元 <a href="https://iguangdiu.com/to.php?u=https%3A%2F%2Fitem.jd.com%2F105.html">加购</a></div></div></div>',
                url=url,
            )
        if "go.php?id=105" in url:
            return _mk_resp(200, "var target = 'https://union-click.jd.com/jdc?e=valid105';", url=url)
        return _mk_resp(200, list_html, url=url)

    sess = MagicMock()
    sess.get.side_effect = fake_get
    ch = ChannelConfig(
        name="逛丢_首页实时流", site_key="guangdiu", default_url="https://guangdiu.com/",
        params={"channel": "latest", "cookie_file": ""}, max_details=10, detail_delay_seconds=0.0,
    )
    res = GuangdiuSource().fetch(FetchContext(channel=ch, session=sess, known_ids={"101"})).to_dict()
    assert res["status"] == "ok"
    assert res["skipped_known"] == 1
    assert res["skipped_unsupported"] == 2
    assert res["skipped_no_detail"] == 1
    assert not any("id=101" in u or "id=102" in u or "id=103" in u for u in called_urls)
    assert not any("go.php?id=104" in u for u in called_urls)
    assert any(u.startswith("https://guangdiu.com/go.php?id=105") for u in called_urls)
    assert len(res["records"]) == 1
    urls = [lk["url"] for lk in res["records"][0]["extracted_links"]]
    assert "go.php?id=105" not in urls
    assert "https://union-click.jd.com/jdc?e=valid105" in urls
    assert "https://item.jd.com/105.html" in urls


def test_haodanku_source_parsing_fliggy_filter_and_touch_ids():
    mock_payload = {
        "code": 200,
        "msg": "SUCCESS",
        "min_id": 2,
        "data": [
            {
                "wire_id": "9001", "platform": "1", "starttime": "1630000000",
                "images": ["https://img.alicdn.com/img1.jpg,https://img.alicdn.com/img2.jpg"],
                "list": [{"content": "【34.9】高洁丝夜安裤20条"}, {"item_id": "bJZkm9ksOhoR707wwZIy7buQtR-ebK049oszM0NnNZQhq"}],
            },
            {
                "wire_id": "9002", "platform": "2", "starttime": "1630000000",
                "images": ["https://img14.360buyimg.com/jd1.jpg"],
                "list": [{"content": "良品铺子凤爪【29.9元】"}, {"item_id": "https://u.jd.com/o1H337p"}],
            },
            {
                "wire_id": "9003", "platform": "3", "starttime": "1630000000",
                "list": [{"content": "泰国普吉岛游"}, {"item_id": "fliggy_xxx"}],
            },
        ],
    }
    sess = MagicMock()
    sess.get.return_value = _mk_resp(200, json_data=mock_payload)
    ch = ChannelConfig(
        name="好单库_淘宝实时线报", site_key="haodanku", default_url="https://www.haodanku.com/activity/tip_off",
        params={"label_id": 10003}, fetch_mode="full_sweep", use_fetch_time_as_publish_time=True, max_pages=1,
    )
    res = HaodankuSource().fetch(FetchContext(channel=ch, session=sess, known_ids={"9002"})).to_dict()
    assert res["status"] == "ok"
    assert res["skipped_unsupported"] == 1
    assert res["skipped_known"] == 1
    assert "9002" in res["touch_ids"]
    assert len(res["records"]) == 1
    rec = res["records"][0]
    assert len(rec["tutorial_images"]) == 2
    assert isinstance(rec["publish_ts"], float)
    assert rec["publish_ts"] > 1_700_000_000
    assert "publish_time" not in rec
    assert any("uland.taobao.com" in lk["url"] for lk in rec["extracted_links"])


def test_haodanku_inline_positions_and_11char_token_and_parsing_truncated_url_and_guangdiu_time():
    from services.deal_sources.parsing import extract_links_images_and_tokens, html_to_tutorial_text
    from services.deal_sources.guangdiu import GuangdiuSource

    # 1. Haodanku: 保留多商品/凑单穿插位置（不污染标题/价格）+ 11位裸淘口令放入 tokens
    mock_payload = {
        "code": 200,
        "msg": "SUCCESS",
        "min_id": 2,
        "data": [
            {
                "wire_id": "9010",
                "platform": "1",
                "starttime": "1630000000",
                "images": ["https://img.alicdn.com/a.jpg"],
                "list": [
                    {"content": "东鹏特饮250ml×24瓶 到手35.9元"},
                    {"item_id": "bJZkm9ksOhoR707wwZIy7buQtR-ebK049oszM0NnNZQhq"},
                    {"content": "👉叠消费券，凑单商品1件"},
                    {"item_id": "JCnV5YILEw9"},
                ],
            }
        ],
    }
    sess = MagicMock()
    sess.get.return_value = _mk_resp(200, json_data=mock_payload)
    ch = ChannelConfig(
        name="好单库_淘宝实时线报", site_key="haodanku", default_url="https://www.haodanku.com/activity/tip_off",
        params={"label_id": 10003}, fetch_mode="full_sweep", max_pages=1,
    )
    res_hdk = HaodankuSource().fetch(FetchContext(channel=ch, session=sess)).to_dict()
    assert len(res_hdk["records"]) == 1
    r_hdk = res_hdk["records"][0]
    assert r_hdk["title"] == "东鹏特饮250ml×24瓶 到手35.9元"
    assert "￥JCnV5YILEw9￥" in r_hdk["extracted_tokens"]
    assert not any("JCnV5YILEw9" in lk.get("url", "") for lk in r_hdk["extracted_links"])
    tut_lines = r_hdk["tutorial_text"].splitlines()
    assert tut_lines.index("https://uland.taobao.com/item/edetail?id=bJZkm9ksOhoR707wwZIy7buQtR-ebK049oszM0NnNZQhq") < tut_lines.index("👉叠消费券，凑单商品1件")
    assert tut_lines.index("👉叠消费券，凑单商品1件") < tut_lines.index("￥JCnV5YILEw9￥")

    # 2. parsing.py: <a> 展示文本为截断前缀 URL 时仅输出/抽取完整 href，绝不重复产出残缺 raw_url
    full_cpn = "https://coupon.m.jd.com/coupons/show.action?linkKey=AAROH_xIpeffAs_-naABEFoeOj-9iMlEY&to=m.jd.com"
    trunc_cpn = "https://coupon.m.jd.com/coupons/show.action?linkKey=AAROH_xIpeffAs_-naABEF..."
    frag_html = f'<p>先领券：<a href="{full_cpn}">{trunc_cpn}</a> 再拍商品：<a href="https://u.jd.com/o6VybgQ">直达</a></p>'
    links, _imgs, _toks = extract_links_images_and_tokens(frag_html)
    assert [lk["url"] for lk in links] == [full_cpn, "https://u.jd.com/o6VybgQ"]
    txt = html_to_tutorial_text(frag_html)
    assert full_cpn in txt
    assert trunc_cpn not in txt
    assert f"({full_cpn})" not in txt

    # 3. guangdiu: 相对时间如“7分钟前”确定性规范化为 UTC ISO 字符串
    list_html = '<div class="gooditem"><a class="goodname" href="detail.php?id=201">测试好价牛奶</a><a class="rightmallname">京东</a><a class="innergototobuybtn" href="https://u.jd.com/ok201">直达</a></div>'
    det_html = '<span class="dinfoweibo"><span class="latesttime">7分钟前</span></span><div class="dabstract" id="dabstract">领券下单实付19.9元</div>'
    gd_sess = MagicMock()
    gd_sess.get.side_effect = lambda url, **_kw: _mk_resp(200, det_html if "detail.php" in url else list_html, url=url)
    gd_ch = ChannelConfig(name="逛丢_首页实时流", site_key="guangdiu", default_url="https://guangdiu.com/", params={"channel": "latest", "cookie_file": ""}, max_details=5, detail_delay_seconds=0.0)
    res_gd = GuangdiuSource().fetch(FetchContext(channel=gd_ch, session=gd_sess)).to_dict()
    assert len(res_gd["records"]) == 1
    assert isinstance(res_gd["records"][0]["publish_ts"], float)
    assert abs(time.time() - res_gd["records"][0]["publish_ts"] - 420.0) < 30.0
    assert "publish_time" not in res_gd["records"][0]


def test_run_fetch_stage_concurrency_cooldown_and_incremental_dedup(monkeypatch, tmp_path):
    import threading
    from services.deal_pipeline import run_fetch_stage
    from services.deal_sources import DEFAULT_CHANNEL_CONFIGS

    _patch_pipeline_log_paths(monkeypatch, tmp_path)

    # 1. 跨站点并发、同站点串行随机轮转
    barrier = threading.Barrier(2, timeout=2.0)
    seen_threads = {}

    class _BarrierSrc(BaseDealSource):
        def fetch(self, ctx: FetchContext) -> FetchResult:
            seen_threads[ctx.channel.site_key] = threading.get_ident()
            barrier.wait()
            return FetchResult(status="ok", http_status=200, request_url=ctx.channel.default_url, elapsed_ms=1.0)

    repo = MagicMock()
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 0, "updated": 0}
    repo.generate_pool_summary.return_value = {"total_records": 0}

    run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=1,
        channels=[
            ChannelConfig(name="A", site_key="site_a", default_url="https://a.test", sleep_after=0.0),
            ChannelConfig(name="B", site_key="site_b", default_url="https://b.test", sleep_after=0.0),
        ],
        channel_last_run={},
        source_registry={"site_a": _BarrierSrc(), "site_b": _BarrierSrc()},
    )
    assert seen_threads["site_a"] != seen_threads["site_b"]

    # 2. 逛丢 Captcha 按频道精准冷却（热门榜/小时榜触发验证码不连坐首页实时流），HTTP 429 仍触发站点级冷却且跨站零干扰
    class _GdChannelCaptchaSrc(BaseDealSource):
        site_key = "guangdiu"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            ch_kind = (ctx.channel.params or {}).get("channel")
            if ch_kind in ("hots", "rank"):
                return FetchResult(
                    status="error", http_status=200, request_url=ctx.channel.default_url, elapsed_ms=1.0,
                    raw_list_count=5, rate_limited=True,
                    detail_errors=[{"item_id": "1", "http_status": 200, "error_type": "CaptchaChallengeDetected", "error": "滑块验证"}],
                )
            return FetchResult(status="ok", http_status=200, request_url="https://guangdiu.com/", elapsed_ms=1.0, raw_list_count=10, skipped_known=10)

    class _ThOkSrc(BaseDealSource):
        site_key = "tehuicaicai"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(status="ok", http_status=200, request_url="https://xhhcds.cn/", elapsed_ms=1.0, raw_list_count=50, skipped_known=50, sweep_completed=True)

    cooldown_map = {}
    out_captcha = run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=1,
        channels=[c for c in DEFAULT_CHANNEL_CONFIGS if c.site_key in ("guangdiu", "tehuicaicai")],
        channel_last_run={},
        site_cooldown_until=cooldown_map,
        source_registry={"guangdiu": _GdChannelCaptchaSrc(), "tehuicaicai": _ThOkSrc()},
    )
    assert "guangdiu" not in cooldown_map
    assert cooldown_map.get("逛丢_热门榜", 0) > 0
    assert cooldown_map.get("逛丢_小时风云榜", 0) > 0
    assert next(t for t in out_captcha["tasks"] if t["task_name"] == "逛丢_首页实时流")["status"] == "success"
    assert next(t for t in out_captcha["tasks"] if t["site_key"] == "tehuicaicai")["status"] == "success"

    # 第二轮：热门榜/小时榜仍在频道冷却期内被跳过，首页实时流照常成功拉取
    out_captcha_r2 = run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=2,
        channels=[c for c in DEFAULT_CHANNEL_CONFIGS if c.site_key == "guangdiu"],
        channel_last_run={},
        site_cooldown_until=cooldown_map,
        source_registry={"guangdiu": _GdChannelCaptchaSrc()},
    )
    assert next(t for t in out_captcha_r2["tasks"] if t["task_name"] == "逛丢_首页实时流")["status"] == "success"
    assert sum(1 for t in out_captcha_r2["tasks"] if t["status"] == "cooldown_skipped") == 2

    # 3. 站点级 HTTP 429 限流：仍冷却整个站点，同站其余频道 cooldown_skipped 且跨站零干扰
    gd_429_calls = []

    class _Gd429Src(BaseDealSource):
        site_key = "guangdiu"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            gd_429_calls.append(ctx.channel.params.get("channel"))
            return FetchResult(
                status="error", http_status=429, request_url=ctx.channel.default_url, elapsed_ms=1.0,
                raw_list_count=0, rate_limited=True,
                detail_errors=[{"item_id": "1", "http_status": 429, "error_type": "HTTP_429", "error": "Too Many Requests"}],
            )

    site_cooldown_map = {}
    out_429 = run_fetch_stage(
        repo=repo,
        session=MagicMock(),
        cycle_num=3,
        channels=[c for c in DEFAULT_CHANNEL_CONFIGS if c.site_key in ("guangdiu", "tehuicaicai")],
        channel_last_run={},
        site_cooldown_until=site_cooldown_map,
        source_registry={"guangdiu": _Gd429Src(), "tehuicaicai": _ThOkSrc()},
    )
    assert len(gd_429_calls) == 1
    assert site_cooldown_map.get("guangdiu", 0) > 0
    assert next(t for t in out_429["tasks"] if t["site_key"] == "tehuicaicai")["status"] == "success"
    assert sum(1 for t in out_429["tasks"] if t["site_key"] == "guangdiu" and t["status"] == "cooldown_skipped") == 2


def test_repo_stops_perpetual_refetch_of_terminal_failures_and_allows_relisted_disappeared():
    from repositories.deal_raw_pool_repo import DealRawPoolRepository

    raw_col = MagicMock()
    formal_col = MagicMock()
    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda n: formal_col if "product" in n else raw_col))
    repo._indexes_ready = True

    # 1. get_existing_detailed_ids 屏蔽已定性失败与低分记录，仅放行 UPSTREAM_DISAPPEARED 重新上榜
    raw_col.find.return_value.sort.return_value.limit.return_value = [{"source_item_id": "perm_fail_1"}]
    ids = repo.get_existing_detailed_ids("haodanku", limit=100)
    assert ids == {"perm_fail_1"}
    find_query = raw_col.find.call_args[0][0]
    assert "$or" in find_query
    statuses = find_query["$or"][0]["clean_status"]["$in"]
    assert {"pending", "cleaned_success", "cleaned_low_score"}.issubset(set(statuses))
    assert find_query["$or"][1] == {"clean_status": "cleaned_failed", "clean_error_code": {"$ne": "UPSTREAM_DISAPPEARED"}}

    # 2. save_batch 仅重置因 UPSTREAM_DISAPPEARED 下架后重新上榜的记录，严禁把确定性失败洗回 pending
    raw_col.update_many.return_value = SimpleNamespace(modified_count=0)
    raw_col.bulk_write.return_value = SimpleNamespace(upserted_count=1, modified_count=0)
    repo.save_batch([{
        "source_site": "haodanku",
        "source_channel": "好单库_京东实时线报",
        "source_item_id": "hdk_123",
        "title": "某商品",
        "tutorial_text": "下单 https://u.jd.com/abc123",
    }])
    reset_filter, reset_update = raw_col.update_many.call_args[0]
    assert reset_filter["clean_status"] == "cleaned_failed"
    assert reset_filter["clean_error_code"] == "UPSTREAM_DISAPPEARED"
    assert "ai_clean_status" not in reset_update["$set"]

    # 3. touch_seen 仅刷新在架(pending/cleaned_success)记录，不动已失败归档时间戳
    raw_col.bulk_write.reset_mock()
    raw_col.bulk_write.return_value = SimpleNamespace(matched_count=1)
    repo.touch_seen("haodanku", ["hdk_123"], refresh_publish_time=False)
    touch_ops = raw_col.bulk_write.call_args[0][0]
    assert touch_ops[0]._filter["clean_status"] == {"$in": ["pending", "cleaned_success"]}


def test_repo_touch_seen_expire_sweep_and_reset_raw_collection():
    from models.deal_constants import QUALIFIED_DEAL_MIN_SCORE, SOURCE_REPUTATION_BONUS
    from models.product import ProductSummary, get_source_site_name
    from repositories.deal_raw_pool_repo import DealRawPoolRepository, evaluate_quality_signals
    from repositories.product_repo import ProductRepository
    from services.deal_sources import build_deal_record

    # 1. 信誉加分与信源中文名映射（仅活跃 12 站点）
    assert SOURCE_REPUTATION_BONUS.get("tehuicaicai") == 10
    assert QUALIFIED_DEAL_MIN_SCORE == 60
    assert get_source_site_name("tehuicaicai") == "特惠采采"
    assert get_source_site_name("zzzdm") == "值值得买"
    assert get_source_site_name("baicaio") == "白菜哦"
    rec = build_deal_record(
        source_site="tehuicaicai", source_channel="特惠采采_首页精选", source_item_id="5434",
        source_url="https://xhhcds.cn/#/product/5434", title="巧克力500g", tutorial_text="下单：https://u.jd.com/oOELq90",
    )
    assert evaluate_quality_signals(rec)["has_unique_product_anchor"] is True
    assert ProductSummary(platform="jd", goods_id="1", name="P", image_url="u", price=1, original_price=2, deal_source="tehuicaicai", deal_source_name="特惠采采").to_dict()["deal_source_name"] == "特惠采采"
    _doc = {
        "platform": "jd",
        "product_id": "1",
        "deal_source": "tehuicaicai",
        "deal_source_name": "特惠采采",
        "deal_info": {
            "is_deal_tip": True,
            "quality_score": 90,
            "publish_time": "2026-11-18 12:00",
            "deal_price": 19.9,
            "source_site": "tehuicaicai",
            "source_site_name": "特惠采采",
            "source_channel": "特惠采采_首页精选",
            "provider_nickname": "特惠采采-专员3",
            "tutorial_steps": ["领券下单"],
            "coupon_links": [{"label": "5元券", "url": "https://u.jd.com/test"}],
        },
    }
    tip = ProductRepository._extract_deal_tip(_doc)
    assert tip["deal_price"] == 19.9 and "tips_timeline" not in tip

    # 2. touch_seen, expire_missing_from_full_sweep & reset_raw_collection
    raw_col = MagicMock()
    formal_col = MagicMock()
    raw_col.delete_many.return_value = SimpleNamespace(deleted_count=12)
    formal_col.delete_many.return_value = SimpleNamespace(deleted_count=8)
    formal_col.update_many.return_value = SimpleNamespace(modified_count=3)
    raw_col.bulk_write.return_value = SimpleNamespace(matched_count=2)

    repo = DealRawPoolRepository(db_instance=SimpleNamespace(get_collection=lambda n: formal_col if "product" in n else raw_col))
    repo._indexes_ready = True

    fixed_now = datetime.datetime(2026, 11, 20, 7, 0, 0, tzinfo=datetime.timezone.utc)
    assert repo.touch_seen("tehuicaicai", ["5513", "5514"], channel="特惠采采_首页精选", refresh_publish_time=True, max_age_days=7.0, at=fixed_now) == 2
    assert formal_col.update_many.call_count == 2
    active_filter, active_upd = formal_col.update_many.call_args_list[0][0]
    revive_filter, revive_upd = formal_col.update_many.call_args_list[1][0]
    assert active_filter["deal_info.is_deal_tip"] is True
    active_set = active_upd["$set"]
    assert active_set["deal_info.publish_ts"] == fixed_now.timestamp()
    assert active_set["deal_info.last_seen_at"] == fixed_now.replace(tzinfo=None)
    assert "deal_info.updated_at" not in active_set
    assert "deal_info.publish_time" not in active_set
    assert revive_filter["deal_info.is_deal_tip"] == {"$ne": True}
    revive_set = revive_upd["$set"]
    assert revive_set["deal_info.is_deal_tip"] is True
    assert revive_set["deal_info.publish_ts"] == fixed_now.timestamp()
    assert revive_set["deal_info.last_seen_at"] == fixed_now.replace(tzinfo=None)
    assert revive_set["deal_info.updated_at"] == fixed_now.replace(tzinfo=None)

    formal_col.update_many.reset_mock()
    assert repo.touch_seen("guangdiu", ["9999"], channel="逛丢_首页实时流", refresh_publish_time=False, at=fixed_now) == 2
    assert formal_col.update_many.call_count == 0

    formal_col.update_many.reset_mock()
    raw_col.update_many.return_value = SimpleNamespace(modified_count=4)
    exp = repo.expire_missing_from_full_sweep(source_site="haodanku", channel="好单库_京东实时线报", sweep_start_time=datetime.datetime(2026, 11, 20, 8, 0, 0))
    # 全量巡视只收敛初步表待清洗任务，不在拉取阶段立即摘除正式表好价标（由维护线程统一按TTL超期失效）
    assert exp == {"raw_expired": 4, "formal_expired": 0}
    assert formal_col.update_many.call_count == 0

    formal_col.update_many.reset_mock()
    deleted_raw = repo.reset_raw_collection()
    assert deleted_raw == 12
    raw_col.delete_many.assert_called_once_with({})
    del_filter = formal_col.delete_many.call_args[0][0]
    assert {"source": "deal_pool"} in del_filter["$or"]
    upd_filter, upd_doc = formal_col.update_many.call_args[0]
    assert "deal_info" in upd_doc["$unset"] and "deal_source" in upd_doc["$unset"] and "last_clean_source" in upd_doc["$unset"]
    assert {"deal_info": {"$exists": True}} in upd_filter["$or"]


def test_ithome_lapin_strips_sitewide_redpacket_footer():
    from services.deal_sources.ithome_lapin import _clean_ithome_paragraph_html

    sample_html = (
        '<div id="paragraph">'
        '<p>京东【沃隆官方旗舰店】沃隆腰果 2 罐实付 29.9 元。</p>'
        '<p>先领 15 元券：<a href="https://coupon.m.jd.com/coupons/show.action?key=k1&amp;roleId=r1">点此领券</a></p>'
        '<p><a href="https://u.jd.com/5G43ieZ">直达链接</a></p>'
        '<p data-vmark="8b04"><span><strong>京东 11.11 无门槛红包</strong></span> 面额至高 26888 元：<a href="https://u.jd.com/FOYj5JY">点此抽红包</a></p>'
        '<p data-vmark="2828"><span><strong>淘宝 11.11 无门槛红包</strong></span> 面额至高 26888 元：<a href="https://s.click.taobao.com/7NGMyki">点此抽红包</a></p>'
        '<p>广告声明：文内含有的对外跳转链接...</p>'
        '</div>'
    )
    cleaned = _clean_ithome_paragraph_html(sample_html)
    assert "先领 15 元券" in cleaned
    assert "coupon.m.jd.com" in cleaned
    assert "u.jd.com/5G43ieZ" in cleaned
    assert "FOYj5JY" not in cleaned
    assert "7NGMyki" not in cleaned
    assert "无门槛红包" not in cleaned
    assert "广告声明：" not in cleaned


def test_filters_positive_link_overrides_mall_name_and_rejects_image_hash_only_shell():
    # 1. 虽商户标签属于品牌/餐饮名，只要携带淘宝/京东/拼多多正向购买锚点即绝不误杀
    assert is_confirmed_unsupported_deal(
        mall_name="肯德基",
        links=[{"url": "https://s.click.taobao.com/t?e=1"}],
    ) is False
    assert is_confirmed_unsupported_deal(
        mall_name="瑞幸",
        links=[{"url": "https://u.jd.com/abc"}],
    ) is False
    assert is_confirmed_unsupported_deal(mall_name="肯德基") is True

    # 2. 仅包含京东主图哈希 candidate_goods_id 但无任何外部电商链接/口令时，严格视为无效空壳
    hash_only_rec = {
        "has_full_detail": True,
        "candidate_goods_id": "jd_img_hash_0123456789abcdef",
        "tutorial_text": "实付19.9元京东自营包邮",
        "extracted_links": [],
        "extracted_tokens": [],
    }
    assert has_valid_detail_and_tutorial(hash_only_rec) is False


def test_token_ssot_and_publish_time_7digit_iso():
    from models.product import extract_all_tokens

    sample = (
        "苹果手机(iPhone16Pro)优惠：￥Ab12Cd34Ef5￥ "
        "！Ab12Cd34Ef56Gh！ "
        "长串：AbCdEfGhIjKlMnOpQrStUvWx-0123456789abcdef"
    )
    tokens = extract_all_tokens(sample)
    assert "￥Ab12Cd34Ef5￥" in tokens
    assert "！Ab12Cd34Ef56Gh！" in tokens
    assert "AbCdEfGhIjKlMnOpQrStUvWx-0123456789abcdef" in tokens
    assert not any("iPhone16Pro" in tk for tk in tokens)

    expected_ts = datetime.datetime(
        2026, 11, 20, 22, 53, 52, 450000,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
    ).timestamp()
    assert abs(parse_publish_time_epoch("2026-11-20T22:53:52.4500000+08:00") - expected_ts) < 1e-6


def test_guangdiu_network_error_not_rate_limited_and_cookie_guard(tmp_path):
    import os
    from services.deal_sources.guangdiu import (
        DEFAULT_GUANGDIU_COOKIE_FILE,
        GuangdiuSource,
        save_guangdiu_cookies,
    )

    sess_err = MagicMock()
    sess_err.get.side_effect = requests.exceptions.ConnectionError("dns failed")
    ch = ChannelConfig(
        name="逛丢_首页实时流",
        site_key="guangdiu",
        default_url="https://guangdiu.com/",
        params={"channel": "latest", "cookie_file": ""},
    )
    res = GuangdiuSource().fetch(FetchContext(channel=ch, session=sess_err)).to_dict()
    assert res["status"] == "error"
    assert res["rate_limited"] is False

    # WEB_LOG_DIR="-" 保护默认运行时 cookie 文件，同时允许显式写入 tmp_path
    s = requests.Session()
    s.cookies.set("gdhuman", "v1", domain="guangdiu.com", path="/")
    existed_before = os.path.exists(DEFAULT_GUANGDIU_COOKIE_FILE)
    save_guangdiu_cookies(s, DEFAULT_GUANGDIU_COOKIE_FILE)
    assert os.path.exists(DEFAULT_GUANGDIU_COOKIE_FILE) is existed_before

    tmp_cookie = tmp_path / "gd_cookies.json"
    save_guangdiu_cookies(s, str(tmp_cookie))
    assert tmp_cookie.exists()


def test_telegram_nested_div_in_message_text_preserved():
    now_ts = 1_760_000_000.0
    t_recent = datetime.datetime.fromtimestamp(now_ts - 120, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    tg_html = f"""
    <section class="tgme_channel_history">
      <div class="tgme_widget_message_wrap" data-post="zzzdm/202">
        <div class="tgme_widget_message_text">
          <div class="nested-quote">优惠说明：领券立减30元</div>
          实付19.9元 <a href="https://u.jd.com/NEST202">立即购买</a>
        </div>
        <time datetime="{t_recent}"></time>
      </div>
    </section>
    """
    sess = MagicMock()
    sess.get.return_value = _mk_resp(200, tg_html)
    ch = ChannelConfig(
        name="TG_NESTED",
        site_key="telegram",
        default_url="https://t.me/s/zzzdm",
        params={"tg_channel": "zzzdm"},
        fetch_mode="time_window",
        lookback_seconds=3600,
        max_pages=1,
        network_mode="proxy",
    )
    res = TelegramChannelSource().fetch(FetchContext(channel=ch, session=sess, now_ts=now_ts)).to_dict()
    assert res["status"] == "ok"
    assert len(res["records"]) == 1
    assert any(lk["url"] == "https://u.jd.com/NEST202" for lk in res["records"][0]["extracted_links"])


def test_haodanku_preserves_prefixed_coupon_urls_for_llm_and_links():
    """好单库 coupon_url 带中文/序号前缀时不得被 startswith('http') 丢弃，须原文进 tutorial_text 并抽入 extracted_links。"""
    u1 = "https://coupon.m.jd.com/coupons/show.action?key=k111&roleId=r111"
    u2 = "https://coupon.m.jd.com/coupons/show.action?key=k222&roleId=r222"
    payload = {
        "code": 200,
        "msg": "SUCCESS",
        "min_id": 2,
        "data": [{
            "wire_id": "1659858",
            "platform": "2",
            "starttime": "1760000000",
            "images": ["https://img14.360buyimg.com/a.jpg"],
            "list": [
                {"content": "①先领优惠券②拍下9.9元"},
                {"item_id": "https://u.jd.com/orX999", "coupon_url": f"领 {u1}"},
                {"content": "备用券", "coupon_url": f"②{u2}"},
            ],
        }],
    }
    sess = MagicMock()
    sess.get.return_value = _mk_resp(200, json_data=payload)
    ch = ChannelConfig(
        name="好单库_京东实时线报",
        site_key="haodanku",
        default_url="https://www.haodanku.com/activity/tip_off",
        params={"label_id": 10004},
        fetch_mode="full_sweep",
        max_pages=1,
    )
    res = HaodankuSource().fetch(FetchContext(channel=ch, session=sess)).to_dict()
    assert res["status"] == "ok"
    rec = res["records"][0]
    assert f"领 {u1}" in rec["tutorial_text"]
    assert f"②{u2}" in rec["tutorial_text"]
    ext_urls = [lk["url"] for lk in rec["extracted_links"]]
    assert u1 in ext_urls
    assert u2 in ext_urls


def test_deal_pipeline_respects_allow_empty_ok_from_baicaio_hour_list(monkeypatch, tmp_path):
    """验证 ListDetailDealSource.allow_empty_ok 透传至 deal_pipeline：正常空榜不得记为 empty_warning。"""
    from services.deal_pipeline import run_fetch_stage
    from services.deal_sources import BaicaioSource, ChannelConfig

    _patch_pipeline_log_paths(monkeypatch, tmp_path)

    empty_hour_html = '<html><body><ul class="list mt-20"></ul></body></html>'
    sess = MagicMock()
    sess.get.return_value = _mk_resp(200, empty_hour_html)
    sess.close = MagicMock()

    ch_empty_ok = ChannelConfig(
        name="白菜哦_小时榜",
        site_key="baicaio",
        default_url="https://www.baicaio.com/hour/",
        params={"mode": "hour"},
        fetch_mode="full_sweep",
        max_pages=1,
        sleep_after=0.0,
    )
    repo = MagicMock()
    repo.col = MagicMock()
    repo.col.count_documents.return_value = 0
    repo.get_existing_detailed_ids.return_value = set()
    repo.save_batch.return_value = {"inserted": 0, "updated": 0}
    repo.touch_seen.return_value = None
    repo.generate_pool_summary.return_value = {"total_records": 0}

    tracker = {}
    res_ok = run_fetch_stage(
        repo=repo,
        channels=[ch_empty_ok],
        channel_last_run={},
        source_registry={"baicaio": BaicaioSource()},
        session_factory=lambda _c: sess,
        channel_health_tracker=tracker,
    )
    rep_ok = res_ok["tasks"][0]
    assert rep_ok["status"] == "success"
    assert rep_ok["consecutive_failures"] == 0

    # 对照：当 allow_empty_ok=False 且未报错（如其它插件 status="ok" 但 0 条记录）仍必须报 empty_warning；
    # 当模板失效导致 BaicaioSource allow_empty_ok=False 时，ListDetailDealSource 判 status="error" 且 consecutive_failures == 1
    broken_html = "<html><body><div>changed template</div></body></html>"
    sess_broken = MagicMock()
    sess_broken.get.return_value = _mk_resp(200, broken_html)
    sess_broken.close = MagicMock()
    tracker2 = {}
    res_broken = run_fetch_stage(
        repo=repo,
        channels=[ch_empty_ok],
        channel_last_run={},
        source_registry={"baicaio": BaicaioSource()},
        session_factory=lambda _c: sess_broken,
        channel_health_tracker=tracker2,
    )
    rep_broken = res_broken["tasks"][0]
    assert rep_broken["status"] == "error"
    assert rep_broken["consecutive_failures"] == 1

    class _OkEmptySource(BaseDealSource):
        site_key = "baicaio"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                status="ok",
                http_status=200,
                request_url=ctx.channel.default_url,
                elapsed_ms=1.0,
                raw_list_count=0,
                allow_empty_ok=False,
            )

    tracker3 = {}
    res_warn = run_fetch_stage(
        repo=repo,
        channels=[ch_empty_ok],
        channel_last_run={},
        source_registry={"baicaio": _OkEmptySource()},
        session_factory=lambda _c: sess_broken,
        channel_health_tracker=tracker3,
    )
    rep_warn = res_warn["tasks"][0]
    assert rep_warn["status"] == "empty_warning"
    assert rep_warn["consecutive_failures"] == 1


def test_deal_pipeline_and_repo_persist_source_is_reliable(monkeypatch, tmp_path):
    """验证 ChannelConfig.source_is_reliable 随 Stage 1 拉取与 save_batch 准确落盘至 deal_raw_tips。"""
    from repositories.deal_raw_pool_repo import DealRawPoolRepository
    from services.deal_pipeline import run_fetch_stage
    from services.deal_sources import BaseDealSource, ChannelConfig, FetchContext, FetchResult

    _patch_pipeline_log_paths(monkeypatch, tmp_path)

    class _DummySource(BaseDealSource):
        site_key = "dummy_rel"

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                status="ok",
                http_status=200,
                request_url=ctx.channel.default_url,
                elapsed_ms=1.0,
                raw_list_count=1,
                records=[
                    {
                        "source_site": ctx.channel.site_key,
                        "source_item_id": f"{ctx.channel.name}_1",
                        "title": "维达抽纸 9.9元包邮",
                        "tutorial_text": "直接下单：https://u.jd.com/abc123",
                        "extracted_links": [{"url": "https://u.jd.com/abc123", "platform": "jd"}],
                        "has_full_detail": True,
                    }
                ],
            )

    raw_col = MagicMock()
    raw_col.bulk_write.return_value = SimpleNamespace(upserted_count=1, modified_count=0)
    raw_col.find.return_value.sort.return_value.limit.return_value = []
    formal_col = MagicMock()
    fake_db = SimpleNamespace(get_collection=lambda name: raw_col if name == "deal_raw_tips" else formal_col)
    repo = DealRawPoolRepository(db_instance=fake_db)
    repo._indexes_ready = True

    ch_reliable = ChannelConfig(
        name="高可靠频道",
        site_key="dummy_rel",
        default_url="https://rel.test",
        source_is_reliable=True,
        sleep_after=0.0,
    )
    ch_normal = ChannelConfig(
        name="普通频道",
        site_key="dummy_rel",
        default_url="https://norm.test",
        sleep_after=0.0,
    )

    sess = MagicMock()
    sess.close = MagicMock()
    run_fetch_stage(
        repo=repo,
        channels=[ch_reliable, ch_normal],
        channel_last_run={},
        source_registry={"dummy_rel": _DummySource()},
        session_factory=lambda _c: sess,
        channel_health_tracker={},
    )

    persisted_by_id = {}
    for call in raw_col.bulk_write.call_args_list:
        for op in call[0][0]:
            persisted_by_id[op._filter["source_item_id"]] = op._doc["$set"]["source_is_reliable"]

    assert persisted_by_id == {
        "高可靠频道_1": True,
        "普通频道_1": False,
    }


