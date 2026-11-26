# -*- coding: utf-8 -*-
"""
数据源 2：逛丢 (`https://guangdiu.com/`)
================================================================================
- 支持频道（单页抓取）：
  - `"latest"` -> `https://guangdiu.com/`
  - `"hots"`   -> `https://guangdiu.com/hots.php`
  - `"rank"`   -> `https://guangdiu.com/rank.php`
- 工程准则（简洁、零破解、宁缺毋滥）：
  1. 支持加载并持久化用户提供的 `guangdiu_cookies.json`（默认存放于 `runtime/guangdiu_cookies.json`）；
  2. 落入 `verify.php` 滑块安全验证页时，用 Playwright 自动把滑块拖到最右端，以服务端是否下发新
     `gdhuman` Cookie 判定通过，通过后把 Cookie 同步回 `requests.Session` 并重拉当前页面；连续失败
     才触发 Fail-Closed 熔断并返回 `CaptchaChallengeDetected`，绝不入库空壳单品；
  3. 列表页先行执行 100% 确定性平台过滤与已知 ID 去重，不发多余详情/跳转请求；
  4. 详情 `#dabstract` 校验通过后再解包 `go.php` 直链，相对路径 `go.php?id=...` 不得误当作直达链。
================================================================================
"""
from __future__ import annotations

import html as html_lib
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlsplit

import requests

from infrastructure.runtime_io import atomic_write_json, should_write_runtime_artifact
from models.product import detect_platform_signal

from .base import FetchContext, FetchResult
from .filters import (
    SOURCE_SELF_HOSTS,
    is_confirmed_unsupported_deal,
    is_host_match,
    unwrap_known_redirect_url,
)
from .http_client import DEFAULT_HEADERS, is_rate_limit_status, request_with_retry
from .list_detail_base import (
    DetailCircuitBreak,
    ListDetailDealSource,
    ListPhaseResult,
    SkipItem,
    fetch_page_html,
)
from .parsing import (
    build_deal_record,
    extract_div_by_id,
    extract_links_images_and_tokens,
    html_to_tutorial_text,
    strip_html_tags,
)

_GUANGDIU_CAPTCHA_MARKERS = (
    "安全验证",
    "verify-wrap",
    "slidetrack",
    "请按住滑块",
    "拖动到最右边",
    "逛丢安全验证",
)

_SERVICES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOBILE_WEB_ROOT = os.path.dirname(_SERVICES_DIR)
_RUNTIME_DIR = os.path.join(_MOBILE_WEB_ROOT, "runtime")
DEFAULT_GUANGDIU_COOKIE_FILE: str = os.path.join(_RUNTIME_DIR, "guangdiu_cookies.json")


def is_guangdiu_captcha_page(url: str, html_text: str) -> bool:
    if "verify.php" in str(url or "").lower():
        return True
    body = str(html_text or "")
    return any(marker in body for marker in _GUANGDIU_CAPTCHA_MARKERS)


def load_guangdiu_cookies(
    session: requests.Session,
    cookie_file: Optional[str] = None,
) -> bool:
    target = DEFAULT_GUANGDIU_COOKIE_FILE if cookie_file is None else str(cookie_file).strip()
    if not target or not hasattr(session, "cookies"):
        return False
    if not os.path.exists(target):
        return False
    try:
        with open(target, "r", encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return False
        now_ts = time.time()
        loaded = 0
        for c in items:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            value = str(c.get("value") or "")
            if not name:
                continue
            expires = c.get("expires")
            if isinstance(expires, (int, float)) and expires > 0 and expires < now_ts:
                continue
            domain = str(c.get("domain") or "guangdiu.com")
            path = str(c.get("path") or "/")
            session.cookies.set(name, value, domain=domain, path=path)
            loaded += 1
        return loaded > 0
    except Exception:
        return False


def save_guangdiu_cookies(
    session: requests.Session,
    cookie_file: Optional[str] = None,
) -> None:
    target = DEFAULT_GUANGDIU_COOKIE_FILE if cookie_file is None else str(cookie_file).strip()
    if not target or not hasattr(session, "cookies"):
        return
    if not should_write_runtime_artifact(target, _RUNTIME_DIR):
        return
    try:
        from requests.cookies import RequestsCookieJar

        if not isinstance(session.cookies, (RequestsCookieJar, list, tuple)):
            return
        out: List[Dict[str, Any]] = []
        for c in session.cookies:
            c_name = getattr(c, "name", None)
            if not c_name or not isinstance(c_name, str):
                continue
            dom = str(getattr(c, "domain", "") or "")
            if dom and "guangdiu.com" not in dom.lower():
                continue
            out.append({
                "name": c_name,
                "value": getattr(c, "value", ""),
                "domain": dom or "guangdiu.com",
                "path": getattr(c, "path", "/") or "/",
                "expires": getattr(c, "expires", None),
            })
        if out:
            atomic_write_json(target, out)
    except Exception:
        pass


_GUANGDIU_SLIDE_MAX_ATTEMPTS = 3
_GUANGDIU_DEFAULT_MAX_CAPTCHA_SOLVES = 2


def _launch_guangdiu_chromium(
    p: Any,
    *,
    headless: bool = True,
    proxy: Optional[str] = None,
    browser_channel: Optional[str] = "chrome",
    bypass_system_proxy: bool = True,
) -> Any:
    """启动浏览器用于滑块验证：优先系统 Chrome，其次 Edge，最后 Playwright 内置 Chromium。"""
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        # 逛丢首包在丢 UDP 的网络下会因 QUIC/HTTP3 握手卡住，强制走 TCP
        "--disable-quic",
    ]
    if not proxy and bypass_system_proxy:
        launch_args.append("--no-proxy-server")

    base_kwargs: Dict[str, Any] = {
        "headless": headless,
        "args": launch_args,
        "ignore_default_args": [
            "--enable-automation",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-component-extensions-with-background-pages",
        ],
    }
    if proxy:
        base_kwargs["proxy"] = {"server": proxy}

    candidates: List[Optional[str]] = []
    if browser_channel and str(browser_channel).strip().lower() not in ("", "chromium", "none"):
        candidates.append(str(browser_channel).strip())
    if "chrome" not in candidates:
        candidates.append("chrome")
    if os.name == "nt" and "msedge" not in candidates:
        candidates.append("msedge")
    candidates.append(None)

    last_err: Optional[Exception] = None
    for ch in candidates:
        kwargs = dict(base_kwargs)
        if ch:
            kwargs["channel"] = ch
        try:
            return p.chromium.launch(**kwargs)
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err is not None else RuntimeError("无法启动任何可用 Chromium/Chrome 浏览器")


def _guangdiu_slide_geometry(page: Any) -> Optional[Dict[str, float]]:
    """读取滑块轨道与把手的真实几何尺寸（终点必须按真实尺寸算）。"""
    try:
        geo = page.evaluate(
            """() => {
                const track = document.getElementById('slidetrack');
                const handle = document.getElementById('slidehandle');
                if (!track || !handle) return null;
                const t = track.getBoundingClientRect();
                const h = handle.getBoundingClientRect();
                if (!t.width || !h.width) return null;
                return {
                    trackLeft: t.left, trackTop: t.top, trackWidth: t.width, trackHeight: t.height,
                    handleLeft: h.left, handleTop: h.top, handleWidth: h.width, handleHeight: h.height
                };
            }"""
        )
    except Exception:
        return None
    return geo if isinstance(geo, dict) else None


def _guangdiu_drag_slider(page: Any, geo: Dict[str, float], *, rng: random.Random) -> None:
    """真人式一次拖动：对准把手 → 按住 → 变速多帧移动 → 到最右端 → 松手（必须先到终点再松手）。"""
    max_x = float(geo["trackWidth"]) - float(geo["handleWidth"])
    if max_x <= 4:
        raise RuntimeError(
            f"滑块轨道尺寸异常: track_width={geo['trackWidth']:.1f}, handle_width={geo['handleWidth']:.1f}"
        )
    grip_x = float(geo["handleLeft"]) + float(geo["handleWidth"]) / 2.0
    grip_y = float(geo["handleTop"]) + float(geo["handleHeight"]) / 2.0
    end_x = float(geo["trackLeft"]) + max_x + 3.0

    page.mouse.move(grip_x, grip_y, steps=5)
    page.wait_for_timeout(rng.randint(120, 260))
    page.mouse.down()
    page.wait_for_timeout(rng.randint(70, 140))

    duration_s = rng.uniform(0.95, 1.35)
    frames = max(28, int(duration_s / 0.018))
    prev_x = 0.0
    for i in range(1, frames + 1):
        progress = i / float(frames)
        eased = 1.0 - (1.0 - progress) ** 3
        x = eased * max_x + rng.uniform(-0.8, 0.8)
        x = max(prev_x, min(x, max_x))
        prev_x = x
        page.mouse.move(grip_x + x, grip_y + rng.uniform(-1.5, 1.5))
        page.wait_for_timeout(rng.randint(14, 26))

    page.mouse.move(end_x, grip_y, steps=2)
    page.wait_for_timeout(rng.randint(90, 160))
    page.mouse.up()


def _open_guangdiu_page(page: Any, url: str, *, timeout_ms: int, attempts: int = 3, settle_ms: int = 600) -> None:
    """打开页面并等滑块控件就绪：用 wait_until="commit"，再等 #slidetrack 挂载。"""
    last_err: Optional[Exception] = None
    for i in range(max(1, int(attempts))):
        try:
            page.goto(url, wait_until="commit", timeout=timeout_ms)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if i + 1 < attempts:
                page.wait_for_timeout(1200)
    if last_err is not None:
        raise last_err

    if "verify.php" in (page.url or "").lower():
        try:
            page.wait_for_selector(
                "#slidetrack",
                state="attached",
                timeout=min(15000, max(3000, int(timeout_ms // 3))),
            )
        except Exception:
            pass
    page.wait_for_timeout(max(200, int(settle_ms)))


def _guangdiu_human_cookie(context: Any) -> str:
    """读取服务端放行 Cookie `gdhuman` 的当前值（尚未下发返回空串）。"""
    for c in context.cookies():
        if str(c.get("name") or "") == "gdhuman":
            return str(c.get("value") or "")
    return ""


def _sync_guangdiu_cookies(session: requests.Session, context: Any, site_host: str) -> List[str]:
    """把浏览器里本站域名的 Cookie 写回 `requests.Session` 并持久化，返回 Cookie 名列表。"""
    names: List[str] = []
    for c in context.cookies():
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if dom and not (dom == site_host or dom.endswith("." + site_host)):
            continue
        names.append(str(c["name"]))
        session.cookies.set(
            str(c["name"]),
            str(c["value"]),
            domain=dom or site_host,
            path=str(c.get("path") or "/"),
        )
    save_guangdiu_cookies(session)
    return names


def solve_guangdiu_slider(
    session: requests.Session,
    target_url: str,
    *,
    verify_url: str = "",
    user_agent: Optional[str] = None,
    timeout: float = 20.0,
    headless: bool = True,
    proxy: Optional[str] = None,
    max_drag_retries: int = _GUANGDIU_SLIDE_MAX_ATTEMPTS,
    browser_channel: Optional[str] = "chrome",
    bypass_system_proxy: bool = True,
) -> Tuple[bool, str]:
    """自动通过逛丢 `verify.php` 滑块验证，并把放行 Cookie 同步回 `requests.Session`。

    只认「服务端是否放行」：拖动后 `gdhuman` 变成新值即通过。不看前端 ✓/类名（跳转后元素消失易误判），
    不读 verifyconfirm.php 的响应体（慢网下会永久挂起）。没拿到新 Cookie 就重开验证页换 token 重拖，
    宁可多滑几次，也不加更多取证分支。返回 `(solved, error_msg)`，正文由调用方用同步后的 Cookie 重拉。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "未安装 playwright，请先执行: pip install playwright"

    open_url = verify_url or target_url
    goto_timeout_ms = int(max(35.0, float(timeout) * 1.6) * 1000)
    rng = random.Random()
    site_host = (urlsplit(target_url).hostname or "").lower() or "guangdiu.com"
    attempts = max(1, int(max_drag_retries))
    ua = user_agent or DEFAULT_HEADERS["User-Agent"]

    try:
        with sync_playwright() as p:
            browser = _launch_guangdiu_chromium(
                p,
                headless=headless,
                proxy=proxy,
                browser_channel=browser_channel,
                bypass_system_proxy=bypass_system_proxy,
            )
            try:
                context = browser.new_context(
                    user_agent=ua,
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 800},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                # 带着会话里已有的本站 Cookie 起跑：若它仍有效，站点会直接放行、根本不出验证页
                pw_cookies = [
                    {
                        "name": c.name,
                        "value": c.value,
                        "domain": str(c.domain or site_host).lstrip(".") or site_host,
                        "path": c.path or "/",
                    }
                    for c in session.cookies
                    if not c.domain
                    or str(c.domain).lstrip(".").lower() == site_host
                    or str(c.domain).lstrip(".").lower().endswith("." + site_host)
                ]
                if pw_cookies:
                    try:
                        context.add_cookies(pw_cookies)
                    except Exception:
                        pass

                page = context.new_page()
                page.set_default_timeout(goto_timeout_ms)
                _open_guangdiu_page(
                    page, open_url, timeout_ms=goto_timeout_ms, settle_ms=rng.randint(500, 900)
                )

                last_err = ""
                for attempt in range(1, attempts + 1):
                    geo = _guangdiu_slide_geometry(page)
                    if not geo:
                        last_err = "验证页未渲染出 #slidetrack / #slidehandle 滑块控件，无法自动拖动"
                        break

                    before = _guangdiu_human_cookie(context)
                    _guangdiu_drag_slider(page, geo, rng=rng)

                    # 松手后服务端回执 + 下发 Cookie 要几百毫秒，最多等 5 秒
                    deadline = time.time() + 5.0
                    after = before
                    while True:
                        after = _guangdiu_human_cookie(context)
                        if after and after != before:
                            break
                        if time.time() >= deadline:
                            break
                        page.wait_for_timeout(200)

                    cookie_names = _sync_guangdiu_cookies(session, context, site_host)
                    if after and after != before:
                        return True, ""

                    last_err = (
                        f"第 {attempt}/{attempts} 次拖动未获服务端放行"
                        f"（gdhuman={after[:16] or '未下发'}，Cookie={'、'.join(cookie_names) or '无'}）"
                    )
                    if attempt < attempts:
                        # 上报失败后 token 即作废，必须重开验证页拿新 token 再拖
                        try:
                            _open_guangdiu_page(
                                page,
                                open_url,
                                timeout_ms=goto_timeout_ms,
                                settle_ms=rng.randint(700, 1200),
                            )
                        except Exception as e_reload:
                            last_err = f"重新打开验证页失败: {type(e_reload).__name__}: {str(e_reload)[:120]}"
                            break

                return False, last_err or "滑块自动拖动未通过"
            finally:
                browser.close()
    except Exception as e:
        return False, f"Playwright 滑块验证异常: {type(e).__name__}: {str(e)[:160]}"


def unwrap_guangdiu_to_php(html_fragment: str) -> str:
    def _repl(m: re.Match) -> str:
        return html_lib.unescape(unquote(m.group(1)))

    return re.sub(
        r"(?:https?://(?:[a-z0-9-]+\.)?i?guangdiu\.com/)?to\.php\?[^\s\"'<>]*\bu=(https?%3A%2F%2F[^&\"'\s<>]+)",
        _repl,
        html_fragment,
        flags=re.I,
    )


def has_substantive_guangdiu_tutorial(dabs_text: str, title: str) -> bool:
    if not dabs_text:
        return False
    no_imgs = re.sub(r"\[(?:群聊晒单/教程图片|教程图片):[^\]]*\]", " ", dabs_text)
    no_urls = re.sub(r"https?://\S+", " ", no_imgs)
    clean_body = re.sub(r"\s+", "", no_urls)
    if len(clean_body) < 6:
        return False
    clean_title = re.sub(r"\s+", "", str(title or ""))
    if clean_title and clean_body == clean_title:
        return False
    return True


def _parse_guangdiu_list_cards(list_html: str, base_url: str, channel_kind: str) -> List[Dict[str, Any]]:
    is_rank = channel_kind == "rank"
    blocks: List[str] = []
    if is_rank:
        blocks = re.findall(
            r'(<div\s+class=["\'][^"\']*\bhourrankitem\b[^"\']*["\'][^>]*>.*?)(?=<div\s+class=["\'][^"\']*\bhourrankitem\b|<div\s+id=["\'][^"\']*(?:footer|sidebar)|<div\s+class=["\']headerholder|$)',
            list_html,
            re.S | re.I,
        )
    if not blocks:
        blocks = re.findall(
            r'(<div\s+class=["\'][^"\']*\bgooditem\b[^"\']*["\'][^>]*>.*?)(?=<div\s+class=["\'][^"\']*\bgooditem\b|<div\s+class=["\'][^"\']*\b(?:page|centerfix)\b|$)',
            list_html,
            re.S | re.I,
        )

    cards: List[Dict[str, Any]] = []
    for blk in blocks:
        list_pub_time = ""
        list_buy_url = ""
        if is_rank or "hourrankitem" in blk:
            m_id = re.search(r"detail\.php\?id=(\d+)", blk)
            if not m_id:
                continue
            item_id = m_id.group(1).strip()

            m_title_a = re.search(r'<a[^>]+class=["\'][^"\']*hourranktitle[^"\']*["\'][^>]*>(.*?)</a>', blk, re.S | re.I)
            raw_t = m_title_a.group(1) if m_title_a else ""
            m_emph = re.search(r'<span\s+class=["\']emphricepart["\'][^>]*>(.*?)</span>', raw_t, re.S | re.I)
            price_span = strip_html_tags(m_emph.group(1)) if m_emph else ""
            t_only = re.sub(r'<span\s+class=["\']emphricepart["\'][^>]*>.*?</span>', "", raw_t, flags=re.S | re.I)
            title = strip_html_tags(t_only)

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blk, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

            m_site = re.search(r'<div\s+class=["\'][^"\']*hourrankfromsite[^"\']*["\'][^>]*>(.*?)</div>', blk, re.S | re.I)
            site_raw = html_lib.unescape(m_site.group(1)).strip() if m_site else ""
            site_parts = [p.strip() for p in re.split(r"[\s\xa0·•-]+", site_raw) if p.strip()]
            mall_name = site_parts[0] if site_parts else ""
        else:
            m_a = re.search(
                r'<a[^>]+class=["\'][^"\']*goodname[^"\']*["\'][^>]*href=["\'](?:https?://guangdiu\.com/)?detail\.php\?id=(\d+)["\'][^>]*>(.*?)</a>',
                blk,
                re.S | re.I,
            )
            if not m_a:
                m_a = re.search(
                    r'<a[^>]+href=["\'](?:https?://guangdiu\.com/)?detail\.php\?id=(\d+)["\'][^>]*>(.*?)</a>',
                    blk,
                    re.S | re.I,
                )
            if not m_a:
                continue
            item_id = m_a.group(1).strip()
            a_inner = m_a.group(2)
            m_emph = re.search(r'<span\s+class=["\']emphricepart["\'][^>]*>(.*?)</span>', a_inner, re.S | re.I)
            price_span = strip_html_tags(m_emph.group(1)) if m_emph else ""
            title = strip_html_tags(a_inner)
            if not title:
                m_alt = re.search(r'<img[^>]+alt=["\']([^"\']+)["\']', blk, re.I)
                if m_alt:
                    title = html_lib.unescape(m_alt.group(1)).strip()

            m_img = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blk, re.I)
            img_url = html_lib.unescape(m_img.group(1)).strip() if m_img else ""

            m_mall = re.search(r'class=["\'][^"\']*rightmallname[^"\']*["\'][^>]*>(.*?)</a>', blk, re.S | re.I)
            mall_name = strip_html_tags(m_mall.group(1)) if m_mall else ""

            m_time = re.search(r'<span[^>]*class=["\'][^"\']*latesttime[^"\']*["\'][^>]*>(.*?)</span>', blk, re.S | re.I)
            if m_time:
                list_pub_time = strip_html_tags(m_time.group(1))

            m_buy_tag = re.search(r'<a\b[^>]*class=["\'][^"\']*innergototobuybtn[^"\']*["\'][^>]*>', blk, re.S | re.I)
            if m_buy_tag:
                m_href = re.search(r'href=["\']([^"\']+)["\']', m_buy_tag.group(0), re.I)
                if m_href:
                    cand = unwrap_known_redirect_url(m_href.group(1))
                    host = (urlsplit(cand).hostname or "").lower()
                    if cand.startswith(("http://", "https://")) and not any(
                        is_host_match(host, self_h) for self_h in SOURCE_SELF_HOSTS
                    ):
                        list_buy_url = cand

        cards.append({
            "item_id": item_id,
            "detail_url": f"{base_url}/detail.php?id={item_id}",
            "title": title,
            "price_text": price_span,
            "image_url": img_url,
            "mall_name": mall_name,
            "publish_time": list_pub_time,
            "list_buy_url": list_buy_url,
        })
    return cards


class GuangdiuSource(ListDetailDealSource):
    site_key = "guangdiu"
    source_key = "guangdiu"
    troubleshooting = {
        "endpoint": "https://guangdiu.com/ | https://guangdiu.com/hots.php | https://guangdiu.com/rank.php",
        "http_403_429": "逛丢触发限流或安全验证：已触发主动熔断冷却，如需恢复请在浏览器完成验证并刷新 runtime/guangdiu_cookies.json。",
        "empty_or_parse": "逛丢列表解析为 0：请核对页面 DOM 是否变更。",
        "timeout_or_conn": "逛丢请求超时：单次超时不影响其余频道，下轮自动补抓。",
    }

    BASE_URL = "https://guangdiu.com"

    def _cookie_file(self, ctx: FetchContext) -> Optional[str]:
        return (ctx.channel.params or {}).get("cookie_file", DEFAULT_GUANGDIU_COOKIE_FILE)

    def before_fetch(self, ctx: FetchContext) -> None:
        cookie_file = self._cookie_file(ctx)
        if cookie_file:
            load_guangdiu_cookies(ctx.session, cookie_file)

    def after_fetch(self, ctx: FetchContext, result: FetchResult) -> None:
        cookie_file = self._cookie_file(ctx)
        if cookie_file and result.pages_crawled > 0:
            save_guangdiu_cookies(ctx.session, cookie_file)

    def get_detail_timeout(self, ctx: FetchContext) -> float:
        return max(15.0, float(ctx.timeout))

    def sleep_before_detail(self, ctx: FetchContext) -> None:
        eff_delay = max(0.0, float(ctx.channel.detail_delay_seconds))
        if eff_delay > 0:
            time.sleep(eff_delay * random.uniform(0.85, 1.35))

    def _solver_params(self, ctx: FetchContext) -> Dict[str, Any]:
        params = ctx.channel.params or {}
        return {
            "enabled": bool(params.get("enable_slider_solver", True)),
            "max_solves": max(0, int(params.get("max_captcha_solves", _GUANGDIU_DEFAULT_MAX_CAPTCHA_SOLVES))),
            "headless": bool(params.get("solver_headless", True)),
            "proxy": params.get("proxy") or None,
            "browser_channel": params.get("browser_channel", "chrome"),
        }

    def _solve_captcha(
        self,
        ctx: FetchContext,
        target_url: str,
        verify_url: str,
    ) -> Tuple[bool, str]:
        """本轮内限额地自动过一次滑块；成功即把新 Cookie 写回会话（含 gdhuman）。"""
        opts = self._solver_params(ctx)
        if not opts["enabled"]:
            return False, "滑块自动验证已关闭 (enable_slider_solver=False)"
        used = int(getattr(ctx.session, "_guangdiu_captcha_solves", 0) or 0)
        if used >= opts["max_solves"]:
            return False, f"本轮滑块自动验证已达上限 ({opts['max_solves']} 次)"
        setattr(ctx.session, "_guangdiu_captcha_solves", used + 1)
        ok, err = solve_guangdiu_slider(
            ctx.session,
            target_url,
            verify_url=verify_url,
            timeout=max(20.0, float(ctx.timeout)),
            headless=opts["headless"],
            proxy=opts["proxy"],
            browser_channel=opts["browser_channel"],
        )
        return ok, err

    def _captcha_error_result(
        self,
        list_url: str,
        resp: Any,
        error_url: str,
        message: str,
        t0: float,
    ) -> ListPhaseResult:
        captcha_err = FetchResult(
            status="error",
            http_status=getattr(resp, "status_code", None),
            request_url=list_url,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            rate_limited=True,
            detail_errors=[{
                "error_url": error_url,
                "http_status": getattr(resp, "status_code", None),
                "error_type": "CaptchaChallengeDetected",
                "error": message,
            }],
        )
        return ListPhaseResult(
            request_url=list_url,
            http_status=getattr(resp, "status_code", None),
            error_result=captcha_err,
        )

    def fetch_list_cards(self, ctx: FetchContext, t0: float) -> ListPhaseResult:
        params = ctx.channel.params or {}
        channel_kind = str(params.get("channel") or "latest").strip()
        if channel_kind == "rank":
            list_url = f"{self.BASE_URL}/rank.php"
        elif channel_kind == "hots":
            list_url = f"{self.BASE_URL}/hots.php"
        else:
            list_url = f"{self.BASE_URL}/"

        eff_timeout = self.get_detail_timeout(ctx)
        resp, list_html, err = fetch_page_html(
            ctx,
            list_url,
            t0=t0,
            timeout=eff_timeout,
            error_prefix="逛丢列表页网络异常",
        )
        if err is not None:
            return ListPhaseResult(request_url=list_url, http_status=err.http_status, error_result=err)

        resp_url = getattr(resp, "url", list_url) or list_url
        if is_guangdiu_captcha_page(resp_url, list_html):
            solved, solve_err = self._solve_captcha(ctx, list_url, resp_url)
            if not solved:
                return self._captcha_error_result(
                    list_url,
                    resp,
                    resp_url,
                    f"逛丢列表页触发安全验证 (verify.php)，自动通过失败（{solve_err}）",
                    t0,
                )
            # 滑块通过后立刻用同一会话重拉列表页（不依赖浏览器代取正文）
            resp, list_html, err = fetch_page_html(
                ctx,
                list_url,
                t0=t0,
                timeout=eff_timeout,
                error_prefix="逛丢列表页滑块通过后重拉异常",
            )
            if err is not None:
                return ListPhaseResult(request_url=list_url, http_status=err.http_status, error_result=err)
            resp_url = getattr(resp, "url", list_url) or list_url
            if is_guangdiu_captcha_page(resp_url, list_html):
                return self._captcha_error_result(
                    list_url,
                    resp,
                    resp_url,
                    "逛丢列表页滑块已通过但重拉仍被拦截，触发主动熔断保护",
                    t0,
                )

        cards = _parse_guangdiu_list_cards(list_html, self.BASE_URL, channel_kind)
        return ListPhaseResult(
            cards=cards,
            request_url=list_url,
            http_status=resp.status_code,
            pages_crawled=1,
            reached_end=False,
        )

    def _fetch_go_target(
        self,
        go_url: str,
        item_id: str,
        list_res: ListPhaseResult,
        ctx: FetchContext,
    ) -> str:
        eff_timeout = self.get_detail_timeout(ctx)
        extra_errors = list_res.extra_state.setdefault("extra_errors", [])
        try:
            r_go = request_with_retry(
                ctx.session,
                "get",
                go_url,
                headers={**DEFAULT_HEADERS, "Referer": list_res.request_url},
                timeout=eff_timeout,
            )
            r_go.encoding = "utf-8"
            go_html = r_go.text
            r_go_url = getattr(r_go, "url", go_url) or go_url
            if r_go.status_code == 200 and is_guangdiu_captcha_page(r_go_url, go_html):
                list_res.extra_state["circuit_broken"] = True
                list_res.extra_state["rate_limited"] = True
                extra_errors.append({
                    "item_id": item_id,
                    "error_url": r_go_url,
                    "http_status": r_go.status_code,
                    "error_type": "CaptchaChallengeDetected",
                    "error": "逛丢 go.php 触发安全验证，触发主动熔断保护",
                })
                return ""

            if r_go.status_code == 200:
                m_var = re.search(
                    r"var\s+[A-Za-z0-9_]+\s*=\s*['\"](https?://[^'\"]+|/[^'\"]+)['\"]",
                    go_html,
                    re.I,
                )
                if m_var:
                    go_target = html_lib.unescape(m_var.group(1)).strip()
                    m_inner_u = re.search(r"[?&]u=(https?%3A%2F%2F[^&\"'\s]+)", go_target, re.I)
                    if m_inner_u:
                        return unquote(m_inner_u.group(1))
                    if go_target.startswith("/"):
                        return urljoin(self.BASE_URL, go_target)
                    return go_target
            else:
                if is_rate_limit_status(r_go.status_code):
                    list_res.extra_state["circuit_broken"] = True
                    list_res.extra_state["rate_limited"] = True
                extra_errors.append({
                    "item_id": item_id,
                    "error_url": go_url,
                    "http_status": r_go.status_code,
                    "error_type": f"GoRedirectHTTP_{r_go.status_code}",
                    "error": f"go.php 返回状态码: {r_go.status_code}",
                })
        except Exception as exc_go:
            extra_errors.append({
                "item_id": item_id,
                "error_url": go_url,
                "http_status": None,
                "error_type": type(exc_go).__name__,
                "error": str(exc_go)[:160],
            })
        return ""

    def parse_detail_record(
        self,
        card: Dict[str, Any],
        det_resp: Any,
        det_html: str,
        list_res: ListPhaseResult,
        ctx: FetchContext,
    ) -> Optional[Dict[str, Any]]:
        ch = ctx.channel
        channel_kind = str((ch.params or {}).get("channel") or "latest").strip()
        is_rank = channel_kind == "rank"

        item_id = card["item_id"]
        detail_url = card["detail_url"]
        raw_u = getattr(det_resp, "url", None)
        rd_url = str(raw_u).strip() if isinstance(raw_u, str) and str(raw_u).strip() else detail_url
        if is_guangdiu_captcha_page(rd_url, det_html):
            solved, solve_err = self._solve_captcha(ctx, detail_url, rd_url)
            if not solved:
                raise DetailCircuitBreak(
                    error_entry={
                        "item_id": item_id,
                        "error_url": rd_url,
                        "http_status": 200,
                        "error_type": "CaptchaChallengeDetected",
                        "error": f"逛丢详情页触发安全验证 (verify.php)，自动通过失败（{solve_err}）",
                    },
                    rate_limited=True,
                    stop_loop=True,
                )
            # 滑块通过后带上新 Cookie 重拉本详情页，再用新正文继续解析
            try:
                det_resp = request_with_retry(
                    ctx.session,
                    "get",
                    detail_url,
                    headers={**DEFAULT_HEADERS, "Referer": list_res.request_url},
                    timeout=self.get_detail_timeout(ctx),
                )
                det_resp.encoding = "utf-8"
                det_html = det_resp.text
                raw_u2 = getattr(det_resp, "url", None)
                rd_url = str(raw_u2).strip() if isinstance(raw_u2, str) and str(raw_u2).strip() else detail_url
            except Exception as exc_refetch:
                raise DetailCircuitBreak(
                    error_entry={
                        "item_id": item_id,
                        "error_url": rd_url,
                        "http_status": None,
                        "error_type": type(exc_refetch).__name__,
                        "error": f"滑块通过后重拉详情页异常: {str(exc_refetch)[:140]}",
                    },
                    stop_loop=True,
                )
            if det_resp.status_code != 200:
                raise DetailCircuitBreak(
                    error_entry={
                        "item_id": item_id,
                        "error_url": rd_url,
                        "http_status": det_resp.status_code,
                        "error_type": "HTTPError",
                        "error": f"滑块通过后重拉详情页返回状态码: {det_resp.status_code}",
                    },
                    stop_loop=True,
                )
            if is_guangdiu_captcha_page(rd_url, det_html):
                raise DetailCircuitBreak(
                    error_entry={
                        "item_id": item_id,
                        "error_url": rd_url,
                        "http_status": det_resp.status_code,
                        "error_type": "CaptchaChallengeDetected",
                        "error": "逛丢详情页滑块已通过但重拉仍被拦截，触发主动熔断保护",
                    },
                    rate_limited=True,
                    stop_loop=True,
                )

        dabs_html = extract_div_by_id(det_html, "dabstract")
        if not dabs_html:
            return None

        title = card["title"]
        img_url = card["image_url"]
        mall_name = card["mall_name"]
        pub_time = card["publish_time"]

        unwrapped_dabs = unwrap_guangdiu_to_php(dabs_html)
        d_links, step_imgs, extracted_tokens = extract_links_images_and_tokens(
            unwrapped_dabs, base_url=detail_url, exclude_hosts=SOURCE_SELF_HOSTS
        )
        extracted_links: List[Dict[str, Any]] = []
        for lk in d_links:
            if not any(x.get("url") == lk.get("url") for x in extracted_links):
                extracted_links.append(lk)

        tutorial_images: List[str] = [img_url] if img_url else []
        for im in step_imgs:
            if im not in tutorial_images:
                tutorial_images.append(im)

        dabs_text = html_to_tutorial_text(unwrapped_dabs, base_url=detail_url).strip()
        if not has_substantive_guangdiu_tutorial(dabs_text, title):
            return None

        m_det_time = re.search(
            r'class=["\'][^"\']*dinfoweibo[^"\']*["\'][^>]*>.*?<span[^>]*class=["\'][^"\']*latesttime[^"\']*["\'][^>]*>(.*?)</span>',
            det_html,
            re.S | re.I,
        )
        if not m_det_time:
            m_det_time = re.search(r'<span[^>]*class=["\'][^"\']*latesttime[^"\']*["\'][^>]*>(.*?)</span>', det_html, re.S | re.I)
        if m_det_time:
            parsed_t = strip_html_tags(m_det_time.group(1))
            if parsed_t:
                pub_time = parsed_t

        go_target = card.get("list_buy_url") or self._fetch_go_target(
            f"{self.BASE_URL}/go.php?id={item_id}", item_id, list_res, ctx
        )
        if go_target and not any(lk.get("url") == go_target for lk in extracted_links):
            extracted_links.insert(0, {
                "label": "逛丢直达购买链",
                "url": go_target,
                "type": "guangdiu_go_target",
                "platform": detect_platform_signal(f"{go_target} {mall_name}") or "unknown",
            })

        if is_confirmed_unsupported_deal(mall_name=mall_name, links=extracted_links, tokens=extracted_tokens):
            raise SkipItem("unsupported")

        channel_label = "逛丢风云榜" if is_rank else ("逛丢热门" if channel_kind == "hots" else (mall_name or "逛丢好价"))
        go_urls_str = "\n".join(
            f"直达链接: {lk['url']}" for lk in extracted_links if lk.get("type") == "guangdiu_go_target"
        )
        tutorial_text = f"【{channel_label}】{title}\n{dabs_text}".strip()
        if go_urls_str:
            tutorial_text = f"{tutorial_text}\n{go_urls_str}".strip()

        return build_deal_record(
            source_site="guangdiu",
            source_channel=ch.name,
            source_item_id=item_id,
            source_url=detail_url,
            source_list_page_url=list_res.request_url,
            source_detail_url=detail_url,
            title=title,
            price_text=card["price_text"],
            image_url=img_url,
            mall_name=mall_name,
            publish_time=pub_time,
            hot_metrics={"sub_channel": channel_kind},
            tutorial_text=tutorial_text,
            tutorial_images=tutorial_images,
            extracted_links=extracted_links,
            extracted_tokens=extracted_tokens,
            raw_payload=unwrapped_dabs,
            has_full_detail=True,
        )
