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
  2. 不做任何浏览器滑块或验证码自动绕过；若列表或详情页落入 `verify.php` / 滑块安全验证页，
     立即触发 Fail-Closed 熔断并返回 `CaptchaChallengeDetected`，等待冷却后重试，绝不入库空壳单品；
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests

from infrastructure.runtime_io import atomic_write_json, should_write_runtime_artifact
from models.product import detect_platform_signal
from repositories.deal_raw_pool_repo import parse_publish_time_epoch

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
_LEGACY_GUANGDIU_COOKIE_FILE: str = os.path.join(_SERVICES_DIR, "guangdiu_cookies.json")
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
    if (
        target == DEFAULT_GUANGDIU_COOKIE_FILE
        and not os.path.exists(target)
        and os.path.exists(_LEGACY_GUANGDIU_COOKIE_FILE)
    ):
        target = _LEGACY_GUANGDIU_COOKIE_FILE
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
    if not (
        should_write_runtime_artifact(target, _RUNTIME_DIR)
        and should_write_runtime_artifact(target, _SERVICES_DIR)
    ):
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
            captcha_err = FetchResult(
                status="error",
                http_status=resp.status_code,
                request_url=list_url,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                rate_limited=True,
                detail_errors=[{
                    "error_url": resp_url,
                    "http_status": resp.status_code,
                    "error_type": "CaptchaChallengeDetected",
                    "error": "逛丢列表页触发安全验证 (verify.php)，请更新 guangdiu_cookies.json 后重试",
                }],
            )
            return ListPhaseResult(request_url=list_url, http_status=resp.status_code, error_result=captcha_err)

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
            raise DetailCircuitBreak(
                error_entry={
                    "item_id": item_id,
                    "error_url": rd_url,
                    "http_status": 200,
                    "error_type": "CaptchaChallengeDetected",
                    "error": "逛丢详情页触发安全验证 (verify.php)，请更新 guangdiu_cookies.json 后重试",
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
        if pub_time and not re.match(r"^\d{4}-\d{2}-\d{2}", pub_time):
            pub_ts = parse_publish_time_epoch(pub_time)
            if isinstance(pub_ts, (int, float)):
                pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
