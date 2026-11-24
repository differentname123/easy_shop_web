# -*- coding: utf-8 -*-
"""线报文本脱敏、无链文案重建与阶段输入组装（复用单一口令/链接剥离真源）。"""
from __future__ import annotations

import html as html_lib
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

from models.deal_constants import format_timestamp_cn
from models.product import strip_ecommerce_tokens_and_urls

_MATH_ALPHANUM_RE = re.compile(r"[\U0001D400-\U0001D7FF]+")
_COMPETITOR_ZZZDM_JIFEN_RE = re.compile(
    r"(?:小编补充\s*[:：]\s*)?返\s*\d+(?:\.\d+)?\s*值得买积分"
    r"(?:[，,\s]*(?:补贴后)?(?:低至|折合?|折)\s*\d+(?:\.\d+)?\s*元(?:/[^\s，,。\n|]+)?)*[。！!~～]?"
)
_TG_ZZZDM_BOT_MARKER_RE = re.compile(
    r"(?m)^[ \t]*(?:值率\s*[:：]\s*值\s*[:：]\s*\d+|商家\s*[:：]\s*\[[^\]]+\])"
)
_CARD_HIGHLIGHT_FLUFF = frozenset({
    "好价推荐",
    "超值优惠",
    "限时特惠",
    "精选好物",
    "点击查看",
    "无",
    "null",
    "none",
})


def normalize_inline_spaces(text: str) -> str:
    """统一特殊空格、剥离零宽字符，并将倾斜/花体数学字母数字还原为标准 ASCII。"""
    if not text:
        return ""
    s = str(text).replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
    if _MATH_ALPHANUM_RE.search(s):
        s = _MATH_ALPHANUM_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), s)
    return s


def strip_markdown_link_wrapper(raw_val: str) -> str:
    """若字符串被包裹在 `[...](https://...)` Markdown 语法中，提取圆括号内的真实 URL。"""
    s = html_lib.unescape(str(raw_val or "")).strip()
    if s.startswith("[") and "](" in s and s.endswith(")"):
        inner = s.split("](", 1)[-1][:-1].strip()
        if inner:
            return inner
    return s


def clean_coupon_label(label: str) -> str:
    """清理优惠券按钮文案中的冗余前缀，避免与前端 [领券] 角标重复。"""
    s = normalize_inline_spaces(str(label or "")).strip()
    s = re.sub(r"^(?:先领|点击领取|领取|领券|优惠券)\s*[:：]\s*", "", s).strip()
    s = re.sub(r"^(?:先领|点击领取)(?=.{2,})", "", s).strip()
    if not s or s in ("领券", "领", "券"):
        return "优惠券"
    return s


def _strip_collector_prefixes(text: str) -> str:
    """剥离采集源站伪标签头（如 折淘客 / 来源采集群 / 拔草哦合集头 / 逛丢榜标签）。"""
    out = re.sub(r"【来源采集群:\s*(?:【[^】]*】)?[^】]*】\s*", "", text)
    out = re.sub(r"【折淘客[^】]*】", "", out)
    out = re.sub(r"【[^】]*拔草哦[^】]*】\s*", "", out)
    out = re.sub(r"【逛丢(?:热门|风云榜|好价)】\s*", "", out)
    return out


def _strip_competitor_platform_noise(text: str, *, strip_polluted_bot_price: bool = False) -> str:
    """精确剥离竞品导购站（值得买积分、TG 机器人值率/空领券/分割线等）非电商平台噪音，严防误删正规优惠。"""
    if not text:
        return ""
    out = str(text)
    had_zzzdm_jifen = bool(_COMPETITOR_ZZZDM_JIFEN_RE.search(out))
    is_tg_zzzdm_bot = bool(_TG_ZZZDM_BOT_MARKER_RE.search(out))

    out = _COMPETITOR_ZZZDM_JIFEN_RE.sub("", out)
    # 仅清理剥离积分后残余的空壳 `小编补充：` 行；含实质购买提示的内容完整保留
    out = re.sub(r"(?m)^[ \t]*小编补充\s*[:：][ \t]*$", "", out)
    out = re.sub(r"(?m)^[ \t]*值率\s*[:：]\s*值\s*[:：]\s*\d+\s*不值\s*[:：]\s*\d+.*$", "", out)
    out = re.sub(r"(?m)^[ \t]*领券\s*[:：]\s*\[无需领券\][ \t]*$", "", out)
    out = re.sub(r"(?m)^[ \t]*-{10,}[ \t]*$", "", out)
    if strip_polluted_bot_price and had_zzzdm_jifen and is_tg_zzzdm_bot:
        out = re.sub(r"(?m)^[ \t]*价格\s*[:：][^\n]*$", "", out)

    kept_lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    return "\n".join(kept_lines).strip()


def sanitize_clean_tutorial_step(text: str) -> str:
    """确保购买教程步骤为 100% 纯文字规范指引：剥离占位符、裸网址与口令壳。"""
    out = normalize_inline_spaces(str(text or ""))
    out = strip_ecommerce_tokens_and_urls(out, preserve_newlines=True)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def sanitize_card_highlight(raw_val: Any) -> str:
    """提纯缩略图超短亮点（复用无链清理，剔除空洞套话并截断至 16 字）。"""
    if not isinstance(raw_val, str):
        return ""
    text = raw_val.replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""
    text = sanitize_clean_tutorial_step(text)
    text = text.strip(" \t\r\n-·•:：|｜,，。!！;；\"'“”‘’[]【】()")
    if not text or text.lower() in _CARD_HIGHLIGHT_FLUFF:
        return ""
    return text[:16].rstrip(" \t-·•:：|｜,，;；")


def sanitize_polished_copywriting(text: str) -> str:
    """对微整形后的文案执行确定性无链校验与竞品噪声清理，杜绝残留占位符、链接与竞品积分水印。"""
    out = normalize_inline_spaces(str(text or "")).strip()
    if not out:
        return ""
    out = _strip_collector_prefixes(out)
    out = _strip_competitor_platform_noise(out, strip_polluted_bot_price=False)
    out = strip_ecommerce_tokens_and_urls(out, preserve_newlines=True)
    kept_lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    return "\n".join(kept_lines).strip()


def build_clean_original_copywriting(
    merged_tip_ids: List[str],
    tip_map: Dict[str, Dict[str, Any]],
    ai1_targets: List[Dict[str, Any]],
    doc: Optional[Dict[str, Any]] = None,
    strip_spans: Optional[List[str]] = None,
) -> str:
    """保留原博主真实行文并确定性剥离他人推广链接、口令壳与采集伪标签。"""
    ordered_docs: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for tid in merged_tip_ids or []:
        tid_s = str(tid or "").strip()
        if tid_s and tid_s not in seen_ids and tid_s in (tip_map or {}):
            seen_ids.add(tid_s)
            ordered_docs.append(tip_map[tid_s])
    if not ordered_docs and doc:
        ordered_docs.append(doc)
    if not ordered_docs:
        return ""

    raw_blocks: List[str] = []
    doc_foreign_spans: List[str] = []
    for nd in ordered_docs:
        tut = normalize_inline_spaces(str(nd.get("tutorial_text") or "")).strip()
        ttl = normalize_inline_spaces(str(nd.get("title") or "")).strip()
        ttl_cmp = re.sub(r"\s+", "", strip_ecommerce_tokens_and_urls(ttl)).rstrip("….")
        tut_cmp = re.sub(r"\s+", "", strip_ecommerce_tokens_and_urls(tut))
        if tut and ttl and (not ttl_cmp or ttl_cmp not in tut_cmp):
            block = f"{ttl}\n{tut}"
        else:
            block = tut if tut else ttl
        if block:
            raw_blocks.append(block)
        for lk in nd.get("extracted_links") or []:
            if isinstance(lk, dict):
                u = html_lib.unescape(str(lk.get("url") or lk.get("item_id") or "")).strip()
                if u:
                    doc_foreign_spans.append(u)
            elif lk:
                doc_foreign_spans.append(str(lk).strip())
        for tk in nd.get("extracted_tokens") or []:
            tk_s = html_lib.unescape(str(tk or "")).strip()
            if tk_s:
                doc_foreign_spans.append(tk_s)

    out = normalize_inline_spaces("\n".join(raw_blocks)).strip()
    if not out:
        return ""

    # 1. 剥离爬虫/聚合源站内部拼接伪标签与竞品平台积分/水印噪音
    out = _strip_collector_prefixes(out)
    out = _strip_competitor_platform_noise(out, strip_polluted_bot_price=True)
    out = re.sub(r"\[[^\]]*(?:图片|附图|晒单图|外链)\s*[:：]\s*https?://[^\]]*\]", "", out)
    out = re.sub(r"\[(?:群聊晒单/)?教程图片(?:\s*[:：][^\]]*)?\]", "", out)
    out = re.sub(r"\[口令\s*[:：][^\]]*\]", "", out)
    out = re.sub(r"(?m)^[ \t]*群发凑单文案\s*[:：]\s*", "", out)
    out = re.sub(r"[ \t]*\|[ \t]*(?:淘宝|京东|拼多多)?商品ID\s*[:：]\s*[A-Za-z0-9_\-]+", "", out)
    out = re.sub(
        r"(?m)^[ \t]*(?:->\s*(?:领券/活动链接|链接|淘宝加密商品ID)|商品加密ID|加密ID|二合一链接|二合一落地页|落地页|直达链接|附带链接|附带口令/加密ID)\s*[:：].*$",
        "",
        out,
    )
    out = re.sub(r"(?m)^[ \t]*-->[ \t]*$", "", out)

    # 2. 剥离 Markdown 链接与括号 URL，保留文字标签
    def _unwrap_md_link(m: re.Match[str]) -> str:
        lbl = m.group(1).strip()
        if not lbl or lbl.lower().startswith(("http://", "https://", "www.")):
            return ""
        return lbl

    out = re.sub(r"\[([^\]]*)\]\(\s*https?://[^)\s]+\s*\)", _unwrap_md_link, out)
    out = re.sub(r"[ \t]*\(\s*https?://[^)\s]+\s*\)", "", out)

    # 3. 复用统一真源剥离链接、口令壳与小程序路径
    out = strip_ecommerce_tokens_and_urls(out, preserve_newlines=True)

    # 4. 精确剔除 AI-1 识别出的 raw_span / strip_spans 与预提取锚点
    def _can_safely_remove_ai_span(s: str) -> bool:
        if not s or len(s) < 5:
            return False
        zh_count = len(re.findall(r"[\u4e00-\u9fff]", s))
        if zh_count == 0:
            return True
        return len(s) <= 60 and zh_count <= 8 and bool(re.search(r"[A-Za-z0-9]{4,}", s))

    spans_to_remove: Set[str] = set()
    for t in ai1_targets or []:
        if not isinstance(t, dict):
            continue
        raw_sp = normalize_inline_spaces(str(t.get("raw_span") or "")).strip()
        if _can_safely_remove_ai_span(raw_sp):
            spans_to_remove.add(raw_sp)
        for field_name in ("raw_span", "clean_input"):
            val = normalize_inline_spaces(strip_markdown_link_wrapper(str(t.get(field_name) or "").strip()))
            if _can_safely_remove_ai_span(val):
                spans_to_remove.add(val)
    for sp in strip_spans or []:
        val = normalize_inline_spaces(strip_markdown_link_wrapper(str(sp or "").strip()))
        if _can_safely_remove_ai_span(val):
            spans_to_remove.add(val)
    for sp in doc_foreign_spans:
        val = normalize_inline_spaces(strip_markdown_link_wrapper(sp))
        if val and len(val) >= 6 and not re.search(r"[\u4e00-\u9fff]", val):
            spans_to_remove.add(val)

    for sp in sorted(spans_to_remove, key=len, reverse=True):
        if sp in out:
            out = out.replace(sp, "")

    out = re.sub(r"(?:(?<=\s)|^)(?:/:/|/:|:/)(?=\s|$)", "", out)
    kept_lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    return "\n".join(kept_lines).strip()


def extract_doc_images(msg_doc: Dict[str, Any]) -> List[str]:
    """确定性提取单条原始文档中的去重图片列表。"""
    imgs: List[str] = []
    primary_img = str(msg_doc.get("image_url") or "").strip()
    if primary_img:
        imgs.append(primary_img)
    for im in msg_doc.get("tutorial_images") or []:
        im_s = str(im or "").strip()
        if im_s and im_s not in imgs:
            imgs.append(im_s)
    return imgs


def format_raw_message_for_ai1(msg_doc: Dict[str, Any], is_target: bool = False) -> Dict[str, Any]:
    """将单条 `deal_raw_tips` 文档格式化为 Stage 1 AI 标准输入的 `raw_messages` 元素。"""
    title = normalize_inline_spaces(str(msg_doc.get("title") or "")).strip()
    tut = normalize_inline_spaces(str(msg_doc.get("tutorial_text") or "")).strip()
    content = tut if tut else title
    if title and title not in content:
        content = f"【标题】{title}\n{content}".strip()

    extra_anchor_lines: List[str] = []
    for lk in msg_doc.get("extracted_links") or []:
        if not isinstance(lk, dict):
            continue
        u = normalize_inline_spaces(html_lib.unescape(str(lk.get("url") or lk.get("item_id") or ""))).strip()
        lbl = str(lk.get("label") or "附带链接").strip()
        if u and u not in content:
            extra_anchor_lines.append(f"{lbl}: {u}")
    for tk in msg_doc.get("extracted_tokens") or []:
        tk_s = normalize_inline_spaces(html_lib.unescape(str(tk or ""))).strip()
        if tk_s and tk_s not in content and not any(tk_s in ln for ln in extra_anchor_lines):
            extra_anchor_lines.append(f"附带口令/加密ID: {tk_s}")

    if extra_anchor_lines:
        content = f"{content}\n" + "\n".join(extra_anchor_lines)

    imgs = extract_doc_images(msg_doc)
    return {
        "tip_id": str(msg_doc.get("source_item_id") or ""),
        "is_target": bool(is_target),
        "publish_time": format_timestamp_cn(
            msg_doc.get("publish_ts") or msg_doc.get("publish_time") or msg_doc.get("fetched_at")
        ),
        "content": content.strip(),
        "image_count": len(imgs),
    }


def assemble_combined_text_and_images(
    merged_tip_ids: List[str],
    norm_targets: List[Dict[str, Any]],
    tip_map: Dict[str, Dict[str, Any]],
    orig_doc: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """按 `merged_tip_ids` 顺序确定性拼接文本与图片，并把锚点替换为 `[#L1]` 占位符。"""
    ordered_docs: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for tid in merged_tip_ids or []:
        tid_s = str(tid or "").strip()
        if tid_s and tid_s not in seen_ids and tid_s in tip_map:
            seen_ids.add(tid_s)
            ordered_docs.append(tip_map[tid_s])
    if not ordered_docs and orig_doc:
        ordered_docs.append(orig_doc)

    text_blocks: List[str] = []
    extracted_images: List[str] = []
    for nd in ordered_docs:
        msg_fmt = format_raw_message_for_ai1(nd, is_target=False)
        c_txt = str(msg_fmt.get("content") or "").strip()
        if c_txt:
            text_blocks.append(c_txt)
        for im_u in extract_doc_images(nd):
            if im_u not in extracted_images:
                extracted_images.append(im_u)

    combined = "\n".join(text_blocks).strip()
    combined = _strip_collector_prefixes(combined)
    combined = _strip_competitor_platform_noise(combined, strip_polluted_bot_price=True)

    replace_jobs: List[Tuple[str, str, str]] = []
    for tgt in norm_targets or []:
        lid = str(tgt.get("link_id") or "").strip()
        if not lid:
            continue
        r_span = str(tgt.get("raw_span") or "").strip()
        c_inp = str(tgt.get("clean_input") or "").strip()
        cand = ""
        if r_span and len(r_span) >= 4 and r_span in combined:
            cand = r_span
        elif c_inp and len(c_inp) >= 4 and c_inp in combined:
            cand = c_inp
        replace_jobs.append((lid, cand, c_inp))

    unmatched_lids: List[str] = []
    for lid, cand, _c_inp in sorted(replace_jobs, key=lambda x: len(x[1]), reverse=True):
        tmp_tok = f"\x00_PLACEHOLDER_{lid}_\x00"
        if cand and cand in combined:
            combined = combined.replace(cand, f" {tmp_tok} ", 1)
        else:
            unmatched_lids.append(lid)

    for lid, _cand, _c_inp in replace_jobs:
        combined = combined.replace(f"\x00_PLACEHOLDER_{lid}_\x00", f"[#{lid}]")

    if unmatched_lids:
        combined = (combined + "\n" + " ".join(f"[#{lid}]" for lid in unmatched_lids)).strip()

    return combined, extracted_images
