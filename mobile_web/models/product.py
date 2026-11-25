# -*- coding: utf-8 -*-
"""商品领域数据契约及跨缓存、仓储使用的商品身份。"""
import math
import re
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Sequence
from urllib.parse import urlsplit
from pydantic import BaseModel, Field

_BARE_YUAN_TAG_RE = re.compile(r"^\d+(?:\.\d+)?元$")
USER_STATS_DIM_MAX_SCORE = 10.0
USER_STATS_DIM_HALF_SATURATION = 5.0
USER_STATS_DIM_DEFAULT_WEIGHT = 1.0
USER_STATS_DIM_WEIGHTS: Dict[str, float] = {
    "valid_orders": 10.0,
}
_STATS_INT_STR_RE = re.compile(r"^[+-]?\d+$")


def _coerce_stats_int(value: Any) -> Optional[int]:
    """将 user_stats 单字段安全转换为整数；排除布尔、日期及非整数字串。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    to_decimal = getattr(value, "to_decimal", None)
    if callable(to_decimal):
        try:
            dec_val = float(to_decimal())
            return int(dec_val) if math.isfinite(dec_val) else None
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if _STATS_INT_STR_RE.fullmatch(text):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def compute_user_stats_score(
    user_stats: Any,
    *,
    max_per_dim: float = USER_STATS_DIM_MAX_SCORE,
    half_saturation: float = USER_STATS_DIM_HALF_SATURATION,
    dim_weights: Optional[Dict[str, float]] = None,
) -> float:
    """
    汇总 user_stats 内全部可转整数的维度评分：
    默认各维度权重 1（单维上限 10 分），`valid_orders` 权重 10（上限 100 分）；
    每一正整数维度按 `weight * 10 * n / (n + 5)` 单调递减增益趋近上限，防单维刷量且兼容新增整数字段。
    """
    if not isinstance(user_stats, dict) or not user_stats:
        return 0.0
    cap = max(0.0, float(max_per_dim))
    k = max(1e-9, float(half_saturation))
    weights = USER_STATS_DIM_WEIGHTS if dim_weights is None else dim_weights
    total = 0.0
    for key, raw_val in user_stats.items():
        n = _coerce_stats_int(raw_val)
        if n is not None and n > 0:
            w = float(weights.get(str(key), USER_STATS_DIM_DEFAULT_WEIGHT))
            if w > 0:
                total += w * cap * (n / (n + k))
    return round(total, 4)


def normalize_platform(platform: Optional[str], default: str = "pdd") -> str:
    code = (platform or default).strip().lower()
    return "taobao" if code == "tb" else code


SOURCE_SITE_NAMES: Dict[str, str] = {
    "tehuicaicai": "特惠采采",
    "guangdiu": "逛丢",
    "haodanku": "好单库",
    "xuandan": "选单网",
    "0818tuan": "0818团",
    "zzzdm": "值值得买",
    "telegram": "Telegram线报",
    "ithome_lapin": "IT之家辣品",
    "kiees": "发现值得买",
    "niuza": "牛杂网",
    "zdzdm": "真的值得买",
    "baicaio": "白菜哦",
    "user_submit": "用户爆料",
}


def get_source_site_name(source_site: Optional[str]) -> str:
    """获取线报信息源的中文显示名称（单一真源）"""
    code = str(source_site or "").strip().lower()
    return SOURCE_SITE_NAMES.get(code, code)


def is_product_reliable(doc: Optional[Dict[str, Any]]) -> bool:
    """商品可靠性唯一真源：无活跃线报必可靠；有活跃线报则来源可靠或价格判定可靠任一满足即可靠。"""
    if not isinstance(doc, dict):
        return False
    deal_info = doc.get("deal_info")
    if not isinstance(deal_info, dict) or deal_info.get("is_deal_tip") is not True:
        return True
    return bool(
        deal_info.get("source_is_reliable") is True
        or deal_info.get("price_is_reliable") is True
    )


def reliable_product_mongo_filter() -> Dict[str, Any]:
    """与 `is_product_reliable(doc) is True` 严格等价的 MongoDB 查询过滤条件。"""
    return {
        "$or": [
            {"deal_info.is_deal_tip": {"$ne": True}},
            {"deal_info.source_is_reliable": True},
            {"deal_info.price_is_reliable": True},
        ]
    }


def unreliable_product_mongo_filter() -> Dict[str, Any]:
    """与 `is_product_reliable(doc) is False` 严格等价的 MongoDB 查询过滤条件。"""
    return {
        "deal_info.is_deal_tip": True,
        "deal_info.source_is_reliable": {"$ne": True},
        "deal_info.price_is_reliable": {"$ne": True},
    }


_TB_CURRENCY_OR_EMOJI_CHARS = r"￥¥$€₴£¢💲🗝🔑🔐"
_TB_WRAP_CHARS = _TB_CURRENCY_OR_EMOJI_CHARS + r"()（）/【】「」《》~|💰👉📲📦🎁"
_SHARE_CODE_PREFIXES = r"(?:CZ|CA|AC|ZH|MF|HU|TG|AA|SY|YD|TK|MU|CB)"
_SHARE_CODE_PAT = rf"{_SHARE_CODE_PREFIXES}\s*\d{{2,6}}"
_SHARE_CODE_HSPACE_PAT = rf"{_SHARE_CODE_PREFIXES}[ \t]*\d{{2,6}}"
_SHARE_CODE_RE = re.compile(rf"\b{_SHARE_CODE_PAT}\b", re.IGNORECASE)
_TB_UNIFIED_TOKEN_RE = re.compile(
    rf"([{_TB_WRAP_CHARS}])\s*(?:{_SHARE_CODE_PAT}\s+)?([A-Za-z0-9]{{8,15}})\s*(?:{_SHARE_CODE_PAT}\s*)?([{_TB_WRAP_CHARS}])",
    re.IGNORECASE,
)
_WEAK_TOKEN_LOWER_RUN_RE = re.compile(r"[a-z]{4,}")
_WEAK_TOKEN_TITLE_SEG_RE = re.compile(r"[A-Z][a-z]{2,}")
_JCMD_TOKEN_RE = re.compile(r"[！!][A-Za-z0-9]{10,20}[！!]")
_BARE_ENCRYPTED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")
_BARE_ENCRYPTED_TOKEN_RE = re.compile(
    r"(?:\b[A-Za-z0-9_-]{24,28}-[A-Za-z0-9_]{16,22}\b|\b[A-Za-z0-9]{25,35}_[A-Za-z0-9]{15,22}\b)"
)
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
PDD_GOODS_SIGN_RE = re.compile(r"^[EYc][A-Za-z0-9_-]{25,}$")
JD_JINGFEN_ID_RE = re.compile(r"jingfen\.jd\.com/detail/([A-Za-z0-9_]{20,})\.html", re.IGNORECASE)
_SHORTLINK_DOMAIN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:u\.jd\.com|3\.cn|m\.tb\.cn|e\.tb\.cn|s\.tb\.cn|tb\.cn|tb3\.cn|s\.click\.taobao\.com|p\.pinduoduo\.com|yangkeduo\.com)/[A-Za-z0-9_.\-?=&/%]+",
    re.IGNORECASE,
)
_HTTP_URL_STRIP_RE = re.compile(r"https?://[^\s\u4e00-\u9fa5\"'<>）)\]]+")
_MP_MINIAPP_RE = re.compile(r"(?:#小程序|mp)://\S+")
_TOKEN_LABEL_PAREN_RE = re.compile(r"[（(]\s*口令\s*[:：][^）)]+[）)]")
_DEAL_TOKEN_SHELL_RE = re.compile(
    rf"(?i)\d{{0,2}}[ \t]*([￥¥$€£₴¢!！（(《「/]{{1,2}})[ \t]*([A-Za-z0-9]{{8,16}})[ \t]*([￥¥$€£₴¢!！）)》」/]{{1,2}})"
    rf"([ \t]*:?/{{1,2}}(?:[ \t]*{_SHARE_CODE_HSPACE_PAT}[ \t]*/*)?|[ \t]+{_SHARE_CODE_HSPACE_PAT}[ \t]*/*)?"
)
_STANDALONE_SHARE_CODE_SHELL_RE = re.compile(
    rf"(?i)(?:(?<=\s)|^)\d{{0,2}}[ \t]*(?:[\(（\$￥¥!！/\-]{{1,3}}[ \t]*[\)）\$￥¥!！/\-]{{0,3}}[ \t]*)?(?::?/{{0,2}}[ \t]*){_SHARE_CODE_HSPACE_PAT}[ \t]*/*(?=\s|$)"
)
_BARE_TOKEN_WRAPPER_RE = re.compile(
    rf"\d{{0,2}}[ \t]*(?:[{_TB_WRAP_CHARS}]){{1,2}}[ \t]*(?:{_SHARE_CODE_PAT}[ \t]+)?([A-Za-z0-9]{{6,20}})[ \t]*(?:{_SHARE_CODE_PAT}[ \t]*)?(?:[{_TB_WRAP_CHARS}]){{1,2}}[ \t]*(?:(?:/|:/|：)\s*(?:{_SHARE_CODE_PAT})?)?",
    re.IGNORECASE,
)

JD_DOMAINS = (
    "u.jd.com", "jingfen.jd.com", "jingfen.com", "union-click.jd.com",
    "item.jd.com", "m.jd.com", "3.cn", "jingxi.com", "jd.com",
)
TAOBAO_DOMAINS = (
    "m.tb.cn", "e.tb.cn", "s.tb.cn", "tb.cn", "tb3.cn", "s.click.taobao.com",
    "uland.taobao.com", "item.taobao.com", "detail.tmall.com", "taobao.com", "tmall.com", "alimama.com",
)
PDD_DOMAINS = ("p.pinduoduo.com", "mobile.pinduoduo.com", "yangkeduo.com", "pinduoduo.com")
SUPPORTED_ECOMMERCE_DOMAINS = (
    "jd.com", "jingfen.com", "3.cn", "jingxi.com",
    "tb.cn", "tb3.cn", "taobao.com", "tmall.com", "alimama.com",
    "pinduoduo.com", "yangkeduo.com",
)


def url_matches_domains(url: Any, domains: Sequence[str]) -> bool:
    """严格按 URL 主机名后缀匹配官方域名白名单（SSOT，供三平台适配器共用）。"""
    try:
        parsed = urlsplit(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and any(
            host == root or host.endswith("." + root)
            for root in domains
            if root
        )
    except Exception:
        return False


def text_has_disallowed_url(text: Any, allowed_domains: Sequence[str]) -> bool:
    """文本中包含 HTTP(S) 链接且无一属于 allowed_domains 时返回 True（SSOT 跨平台守卫）。"""
    urls = [u.rstrip(".,!?)];'\"") for u in URL_RE.findall(str(text or ""))]
    return bool(urls) and not any(url_matches_domains(u, allowed_domains) for u in urls)


def _looks_like_random_11char_token(tok: str) -> bool:
    """结构化区分高熵 11 位淘口令与自然英文/拼音词（如 iPhone16Pro、MacBookPro1），绝不依赖型号黑名单。"""
    if _WEAK_TOKEN_LOWER_RUN_RE.search(tok):
        return False
    if len(_WEAK_TOKEN_TITLE_SEG_RE.findall(tok)) >= 2:
        return False
    return True


def _iter_taobao_token_matches(no_urls: str):
    """生成 (核心口令, 完整匹配文本)，统一实施强包裹符或 11 位高熵 + 斜杠边界校验。"""
    has_share_code = bool(_SHARE_CODE_RE.search(no_urls) or re.search(r"\b\d{1,2}:/", no_urls))
    for m in _TB_UNIFIED_TOKEN_RE.finditer(no_urls):
        left_ch, tok_core, right_ch = m.group(1), m.group(2), m.group(3)
        if not (any(c.isalpha() for c in tok_core) and any(c.isdigit() for c in tok_core)):
            continue
        is_strong_delim = (left_ch in _TB_CURRENCY_OR_EMOJI_CHARS) or (right_ch in _TB_CURRENCY_OR_EMOJI_CHARS)
        if not is_strong_delim:
            if not (len(tok_core) == 11 and (has_share_code or _looks_like_random_11char_token(tok_core))):
                continue
            if left_ch == "/" or right_ch == "/":
                before_ch = no_urls[m.start() - 1] if m.start() > 0 else ""
                after_ch = no_urls[m.end()] if m.end() < len(no_urls) else ""
                if (before_ch and re.match(r"[A-Za-z0-9:/._-]", before_ch)) or (
                    after_ch and re.match(r"[A-Za-z0-9_.-]", after_ch)
                ):
                    continue
        yield tok_core, m.group(0)


def extract_taobao_token_core(raw_text: Any) -> str:
    """
    淘口令唯一真源提取器：
    - 先剥离 http(s) URL 防止误匹配 URL 路径段；
    - 货币/钥匙符号包裹（如 `￥...￥`、`🗝...💲`）允许 8~15 位字母数字混合码；
    - 普通括号/斜杠/书名号包裹（如 `(...)`、`/.../`、`【...】`）严格要求标准 11 位字母数字混合码，
      且无分享前缀码时以结构化熵规则排除自然英文/拼音词，兼顾高召回与搜索框零误判。
    """
    text = str(raw_text or "").strip()
    if not text:
        return ""
    no_urls = URL_RE.sub(" ", text)
    for tok_core, _ in _iter_taobao_token_matches(no_urls):
        return tok_core
    return ""


def extract_all_tokens(raw_text: Any) -> List[str]:
    """跨模块口令提取唯一真源：提取正文中的淘口令、京口令（`！...！`）与裸加密商品串，自动去重。"""
    text = str(raw_text or "").strip()
    if not text:
        return []
    no_urls = URL_RE.sub(" ", text)
    found: List[str] = []
    seen = set()

    def _add(val: str) -> None:
        v = str(val or "").strip()
        if v and v not in seen:
            seen.add(v)
            found.append(v)

    for _, raw_tok in _iter_taobao_token_matches(no_urls):
        _add(raw_tok)

    for m in _JCMD_TOKEN_RE.finditer(no_urls):
        raw_jcmd = m.group(0).strip()
        if any(c.isalpha() for c in raw_jcmd) and any(c.isdigit() for c in raw_jcmd):
            _add(raw_jcmd)

    for m in _BARE_ENCRYPTED_TOKEN_RE.finditer(no_urls):
        val = m.group(0).strip()
        if any(c.isalpha() for c in val) and any(c.isdigit() for c in val):
            _add(val)

    return found


def is_standalone_ecommerce_token(raw_text: Any) -> bool:
    """判断整段文本是否为单一独立电商口令或加密商品串。"""
    s = str(raw_text or "").strip()
    return bool(s) and extract_all_tokens(s) == [s]


def strip_ecommerce_tokens_and_urls(raw_text: Any, *, preserve_newlines: bool = False) -> str:
    """统一剥离分享文本中的链接、内部占位符 `[#L1]`、小程序路径、淘/京口令壳及分享前缀码。"""
    text = str(raw_text or "")
    if not text:
        return ""
    rep = "" if preserve_newlines else " "

    def _replace_shell_if_token(m: re.Match[str]) -> str:
        left_d, core, right_d, tail = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        has_strong = any(ch in _TB_CURRENCY_OR_EMOJI_CHARS for ch in (left_d + right_d))
        if has_strong or tail.strip():
            return rep
        if len(core) == 11 and any(c.isalpha() for c in core) and any(c.isdigit() for c in core) and _looks_like_random_11char_token(core):
            return rep
        return m.group(0)

    def _replace_unified_if_token(m: re.Match[str]) -> str:
        left_ch, core, right_ch = m.group(1), m.group(2), m.group(3)
        if not (any(c.isalpha() for c in core) and any(c.isdigit() for c in core)):
            return m.group(0)
        if (left_ch in _TB_CURRENCY_OR_EMOJI_CHARS) or (right_ch in _TB_CURRENCY_OR_EMOJI_CHARS):
            return rep
        if len(core) == 11 and _looks_like_random_11char_token(core):
            return rep
        return m.group(0)

    cleaned = re.sub(r"\s*\[#L\d+\]" if preserve_newlines else r"\[#L\d+\]", rep, text)
    cleaned = _TOKEN_LABEL_PAREN_RE.sub(rep, cleaned)
    cleaned = _MP_MINIAPP_RE.sub(rep, cleaned)
    cleaned = _HTTP_URL_STRIP_RE.sub(rep, cleaned)
    if not preserve_newlines:
        cleaned = URL_RE.sub(rep, cleaned)
    cleaned = _SHORTLINK_DOMAIN_RE.sub(rep, cleaned)
    cleaned = _JCMD_TOKEN_RE.sub(rep, cleaned)
    cleaned = _DEAL_TOKEN_SHELL_RE.sub(_replace_shell_if_token, cleaned)
    cleaned = _TB_UNIFIED_TOKEN_RE.sub(_replace_unified_if_token, cleaned)
    if preserve_newlines:
        cleaned = _STANDALONE_SHARE_CODE_SHELL_RE.sub("", cleaned)
    else:
        cleaned = _BARE_TOKEN_WRAPPER_RE.sub(
            lambda m: rep if (any(c.isalpha() for c in m.group(1)) and any(c.isdigit() for c in m.group(1)) and _looks_like_random_11char_token(m.group(1))) else m.group(0),
            cleaned,
        )
        cleaned = _SHARE_CODE_RE.sub(rep, cleaned)
    return cleaned


def extract_search_keyword(raw_text: Any, max_length: int = 60) -> str:
    """
    跨平台统一分享文案清洗与搜索关键词提取器：
    1. 若包含「商品标题」书名号且内部非纯口令/链接，优先提取「...」内的商品标题；
    2. 自动剥离 HTTP(S) 链接、淘口令/京口令乱码、平台角标括号及促销引导语；
    3. 压缩空白并安全截断至 max_length，供三端搜索与万能转链标题反查共用。
    """
    text = str(raw_text or "").strip()
    if not text:
        return ""
    bracket_match = re.search(r"「([^」]{2,})」", text)
    if bracket_match:
        candidate = bracket_match.group(1).strip()
        if (
            candidate
            and not candidate.startswith(("http://", "https://"))
            and not re.fullmatch(rf"(?:{_SHARE_CODE_PAT}\s+)?[A-Za-z0-9]{{6,22}}", candidate, re.IGNORECASE)
        ):
            return candidate[:max_length].strip()

    cleaned = strip_ecommerce_tokens_and_urls(text, preserve_newlines=False)
    cleaned = re.sub(
        r"【(?:淘宝|天猫|京东|京东自营|拼多多|多多进宝|百亿补贴|好物推荐|好物分享|好物精选|手淘分享|券后特惠|特惠参考价|抢购口令|平台口令)[^】]*】",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?:^|\s)\d+\s*[\$￥¥!！:/]+\s*", " ", cleaned)
    cleaned = re.sub(
        r"(?:点击链接直接打开|手机淘宝|淘宝App|复制本段|打开手淘|长按复制[^\n]*|💰[^\n]*|👉[^\n]*)",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。！？、,!?:：-")
    if not cleaned:
        return ""
    return cleaned[:max_length].strip()


def detect_platform_signal(raw_text: Any) -> Optional[str]:
    """
    跨平台统一信号识别器：
    从链接域名、口令包裹符、平台标识词或裸加密商品 ID 中识别明确平台 ('jd' | 'taobao' | 'pdd')。
    若输入为不含任何平台特征的纯商品标题，返回 None（交由调用方决定默认行为或提示用户选择平台）。
    """
    text = str(raw_text or "").strip()
    if not text:
        return None
    lower = text.lower()

    # 1. 显式域名特征识别
    if any(d in lower for d in JD_DOMAINS):
        return "jd"
    if any(d in lower for d in TAOBAO_DOMAINS):
        return "taobao"
    if any(d in lower for d in PDD_DOMAINS) or "goods_sign=" in lower:
        return "pdd"

    # 2. 口令与文案特征识别（统一复用 extract_taobao_token_core 单一真源）
    if _JCMD_TOKEN_RE.search(text) or any(
        k in text for k in ("京东", "猄", "崠", "崬", "京口令", "Doōδng", "Doσσng", "Dσσδng")
    ):
        return "jd"
    if extract_taobao_token_core(text) or any(k in text for k in ("淘口令", "手淘", "淘宝", "天猫", "阿里妈妈")):
        return "taobao"
    if any(k in text for k in ("拼多多", "多多进宝", "百亿补贴")) or PDD_GOODS_SIGN_RE.match(text):
        return "pdd"

    # 3. 裸加密商品 ID 特征识别
    if len(text) >= 25 and "-" in text and re.match(r"^[A-Za-z0-9_-]{25,}$", text):
        return "taobao"
    if len(text) >= 25 and not text.isdigit() and re.match(r"^[A-Za-z0-9_]{25,}$", text):
        return "jd"

    return None


def classify_user_input(raw_text: Any) -> Dict[str, Any]:
    """
    跨入口（首页搜索 / 严选搜索 / 链接解析）统一输入分类器：
    从第一性原理区分「纯搜索词」、「电商分享链接/口令/加密ID」与「非支持的外站链接」，
    避免搜索框把纯链接当关键词盲搜，也避免转链框把纯商品名发往上游接口报错。
    """
    text = str(raw_text or "").strip()
    if not text:
        return {
            "kind": "empty",
            "platform": None,
            "keyword": "",
            "has_link_or_token": False,
        }

    lower = text.lower()
    has_http = bool(re.search(r"https?://", lower))
    has_supported_domain = any(d in lower for d in SUPPORTED_ECOMMERCE_DOMAINS)
    if has_http and not has_supported_domain:
        return {
            "kind": "unsupported_url",
            "platform": None,
            "keyword": extract_search_keyword(text, max_length=60),
            "has_link_or_token": True,
        }

    is_bare_encrypted_id = bool(
        len(text) >= 25 and not text.isdigit() and _BARE_ENCRYPTED_ID_RE.match(text)
    )
    has_token_signal = bool(
        has_supported_domain
        or "goods_sign=" in lower
        or extract_taobao_token_core(text)
        or _JCMD_TOKEN_RE.search(text)
        or is_bare_encrypted_id
    )
    platform = detect_platform_signal(text)
    if is_bare_encrypted_id:
        keyword = ""
    elif has_token_signal:
        keyword = extract_search_keyword(text, max_length=60)
    else:
        keyword = text[:60].strip()

    return {
        "kind": "link_or_token" if has_token_signal else "keyword",
        "platform": platform,
        "keyword": keyword,
        "has_link_or_token": has_token_signal,
    }



def clean_product_id(value: Any) -> str:
    """0 是旧清洗函数的缺省值，不可作为真实商品 ID。"""
    if isinstance(value, bool) or isinstance(value, (int, float)) and value == 0:
        return ""
    text = str(value).strip() if value is not None else ""
    return "" if text in ("", "0", "0.0", "None") else text


_JD_IMAGE_KEY_RE = re.compile(r"^[0-9a-fA-F]{14,22}_[0-9a-fA-F]{12,20}$")
_JD_ITEM_URL_RE = re.compile(r"^https?://item\.jd\.com/([^/?#.]+)\.html$", re.IGNORECASE)


def is_jd_image_key(value: Any) -> bool:
    """判断字符串是否为京东主图 CDN 提取的 <目录哈希>_<文件哈希> 静态唯一标识。"""
    text = clean_product_id(value)
    return bool(text and _JD_IMAGE_KEY_RE.match(text))


def normalize_jd_material_url(raw_url: Any, candidate_id: Any = "") -> str:
    """
    规范化京东商品物料链接：
    - 将用加密 itemId 误拼的 https://item.jd.com/{encrypted_id}.html 统一转换为 https://jingfen.jd.com/detail/{encrypted_id}.html；
    - 当 raw_url 为空且提供 candidate_id 时，数字 SKU 生成 item.jd.com 链接，加密 itemId 生成 jingfen.jd.com 链接。
    """
    url = str(raw_url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith(("http://", "https://")):
        url = ""
    cid = clean_product_id(candidate_id)

    if url:
        m_item = _JD_ITEM_URL_RE.match(url)
        if m_item:
            token = clean_product_id(m_item.group(1))
            if token and not token.isdigit():
                target = cid or token
                return f"https://jingfen.jd.com/detail/{target}.html" if target else ""
        return url

    if not cid:
        return ""
    if cid.isdigit():
        return f"https://item.jd.com/{cid}.html"
    return f"https://jingfen.jd.com/detail/{cid}.html"


def extract_jd_image_key(image_url: Any) -> str:
    """
    从京东官方 CDN 主图 URL (360buyimg.com / jfs) 中提取稳定的商品唯一标识：
    例如 https://img14.360buyimg.com/pop/jfs/t1/550472/38/9362/180494/6ae3f40fF84b3cfef/0083320320005d73.jpg
    提取末尾两级哈希路径 -> '6ae3f40fF84b3cfef_0083320320005d73'。
    无论商品来自实时榜单、全网搜索、单品详情还是万能转链，也无论底层使用哪个 ZTK_APPKEY / unionId，
    同一京东商品的主图 CDN 哈希标识始终恒定不变。
    """
    text = str(image_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit("https:" + text if text.startswith("//") else text)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if not (host == "360buyimg.com" or host.endswith(".360buyimg.com") or "/jfs/" in path):
            return ""
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            raw_last = parts[-1].split("!", 1)[0]
            stem = re.sub(r"(?:\.(?:jpg|jpeg|png|webp|gif|avif|dpg))+$", "", raw_last, flags=re.IGNORECASE)
            parent = parts[-2]
            if (
                len(parent) >= 8
                and len(stem) >= 8
                and all(c.isalnum() or c in "_-" for c in parent)
                and all(c.isalnum() or c in "_-" for c in stem)
            ):
                return f"{parent}_{stem}"
    except ValueError:
        return ""
    return ""


def extract_canonical_goods_id(
    platform: Optional[str],
    raw_id_or_sign: Any = "",
    *,
    image_url: Any = "",
) -> str:
    """
    第一性原理商品唯一主键 (goods_id / product_id) 规范化提取器：
    将“用于数据库去重与缓存锚定的静态唯一标识 (goods_id)”与“用于调用上游接口的动态混淆签名 (goods_sign)”解耦：
    1. 淘宝 (taobao)：
       TOP 27939 权限包与折淘客返回的加密 item_id 固定为 '<动态请求盐值前缀>-<稳定商品密文后缀>'
       （如 '0AqROdBH7t8b9oJzGJT5n6C2t6-0v7DKOgI5JqnZd4Nhxd'）。
    2. 京东 (jd)：
       优先从京东主图 URL (360buyimg.com/jfs/...) 提取 '<目录哈希>_<文件哈希>' 作为跨通道恒定唯一标识。
    3. 拼多多 (pdd)：
       保持原生数字 goods_id 不变。
    """
    plat = normalize_platform(platform)
    raw = clean_product_id(raw_id_or_sign)
    if plat == "taobao":
        if "-" in raw:
            prefix, suffix = raw.split("-", 1)
            if (
                len(prefix) >= 15
                and len(suffix) >= 12
                and all(c.isalnum() or c == "_" for c in suffix)
            ):
                return suffix
        return raw
    if plat == "jd":
        img_key = extract_jd_image_key(image_url)
        if img_key:
            return img_key
        return raw
    return raw


def resolve_item_identity(
    item: Dict[str, Any],
    default_platform: Optional[str] = "pdd",
) -> tuple[str, str, str]:
    """从商品字典中统一提取规范化三元组 (platform, goods_id, goods_sign)。"""
    platform = normalize_platform(item.get("platform") or default_platform)
    raw_gid = clean_product_id(item.get("goods_id"))
    raw_pid = clean_product_id(item.get("product_id"))
    raw_sign = str(item.get("goods_sign") or "").strip()

    if platform == "pdd":
        goods_id = raw_gid or (raw_pid if raw_pid != raw_sign else "")
        goods_sign = raw_sign or (raw_pid if not goods_id else "")
    else:
        val = raw_gid or raw_sign or raw_pid
        img_url = str(
            item.get("image_url")
            or item.get("goods_image_url")
            or item.get("goods_thumbnail_url")
            or ""
        ).strip()
        raw_id_candidate = raw_gid or raw_pid or val
        goods_id = (
            extract_canonical_goods_id(
                platform,
                raw_id_candidate,
                image_url=img_url,
            )
            or raw_id_candidate
        )
        goods_sign = raw_sign or val

    return platform, goods_id, goods_sign


@dataclass(frozen=True)
class ProductRef:
    """平台、真实 ID 与加密 Sign 独立保存，不将请求里的两者视为已验证别名。"""
    platform: str = "pdd"
    goods_id: str = ""
    goods_sign: str = ""

    def __post_init__(self):
        object.__setattr__(self, "platform", normalize_platform(self.platform))
        object.__setattr__(self, "goods_id", clean_product_id(self.goods_id))
        object.__setattr__(self, "goods_sign", str(self.goods_sign or "").strip())

    @property
    def key(self) -> str:
        return self.goods_sign or self.goods_id

    def cache_keys(self, uid: Optional[str] = None) -> list:
        scope = ("detail", self.platform, uid or "")
        return [scope + (kind, value) for kind, value in
                (("sign", self.goods_sign), ("id", self.goods_id)) if value]


def utc_millis(value: datetime) -> datetime:
    """来源时间统一为UTC及BSON毫秒精度，不在此生成新的来源时钟。"""
    if not isinstance(value, datetime):
        raise ValueError("详情来源时间必须为 datetime")
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


class ProductSummary(BaseModel):
    """商品卡片摘要。goods_id 可为空，不能用 Sign 冒充数字 ID。"""
    platform: str = "pdd"
    platform_name: str = "拼多多"
    goods_id: str = ""
    goods_sign: str = ""
    deal_source: str = ""
    deal_source_name: str = ""
    is_deal_tip: bool = False
    deal_highlight: str = ""
    deal_publish_time: str = ""
    name: str
    image_url: str
    product_url: str = ""
    price: float
    original_price: float
    coupon_amount: float = 0.0
    has_coupon: bool = False
    coupon_url: str = ""
    commission_rate: float = 0.0
    commission_amount: float = 0.0
    sales_tip: str = "0"
    mall_name: str = "品牌官方店"
    brand: str = "官方甄选"
    category: str = "精选好物"
    updated_at: Optional[str] = None

    def __init__(self, **data: Any):
        if "coupon_url" in data:
            raw_cu = data.get("coupon_url")
            data["coupon_url"] = raw_cu.strip() if isinstance(raw_cu, str) else ""
        if "coupon_amount" in data or "has_coupon" in data:
            try:
                c_val = round(max(0.0, float(data.get("coupon_amount") or 0.0)), 2)
            except (TypeError, ValueError):
                c_val = 0.0
            if "coupon_amount" in data:
                data["coupon_amount"] = c_val
            if "has_coupon" in data or c_val > 0:
                data["has_coupon"] = c_val > 0
        if "price" in data and "original_price" in data:
            try:
                p_val = float(data["price"])
                o_val = float(data["original_price"])
                c_val = round(max(0.0, float(data.get("coupon_amount") or 0.0)), 2)
                if math.isfinite(p_val) and math.isfinite(o_val):
                    if c_val > 0 and o_val <= p_val:
                        data["original_price"] = round(p_val + c_val, 2)
                    elif o_val < p_val:
                        data["original_price"] = round(p_val, 2)
            except (TypeError, ValueError):
                pass
        super().__init__(**data)

    def to_dict(self, **kwargs) -> Dict[str, Any]:
        return self.model_dump(**kwargs)


def normalize_detail_tags(raw_tags: Any) -> List[str]:
    """详情标签去重并补全裸金额优惠券文案（如 '5元' -> '5元优惠券'）。"""
    if not isinstance(raw_tags, list):
        return []
    normalized_tags: List[str] = []
    for tag in raw_tags:
        t = str(tag or "").strip()
        if not t:
            continue
        if _BARE_YUAN_TAG_RE.match(t):
            t = f"{t}优惠券"
        if t not in normalized_tags:
            normalized_tags.append(t)
    return normalized_tags


class ProductDetail(ProductSummary):
    """完整商品详情，商业承诺和券信息仅保存来源提供的真实值。"""
    desc: str = ""
    carousel_images: List[str] = Field(default_factory=list)
    detail_tags: List[str] = Field(default_factory=list)
    service_guarantees: List[str] = Field(default_factory=list)
    shop_ratings: Dict[str, str] = Field(default_factory=dict)
    specs: List[Dict[str, str]] = Field(default_factory=list)
    coupon_info: Dict[str, Any] = Field(default_factory=dict)
    deal_tip: Optional[Dict[str, Any]] = None

    def __init__(self, **data: Any):
        raw_tags = data.get("detail_tags")
        if isinstance(raw_tags, list):
            data["detail_tags"] = normalize_detail_tags(raw_tags)
        super().__init__(**data)


_DETAIL_FIELDS = frozenset(ProductDetail.model_fields.keys()) | {
    "brand_name", "category_name", "unified_tags", "search_id",
}


@dataclass(frozen=True)
class DetailSnapshot:
    """内部公共详情快照；与HTTP DTO分离，禁止夹带推广及用户上下文。"""
    ref: ProductRef
    data: Dict[str, Any]
    fetched_at: datetime

    def __post_init__(self):
        if not isinstance(self.ref, ProductRef) or not self.ref.key or not isinstance(self.data, dict):
            raise ValueError("详情快照缺少可信商品身份或内容")
        data = {key: deepcopy(value) for key, value in self.data.items() if key in _DETAIL_FIELDS}
        platform = normalize_platform(data.get("platform") or self.ref.platform)
        actual = ProductRef(platform, data.get("goods_id"), data.get("goods_sign"))
        if (platform != self.ref.platform or actual.goods_id != self.ref.goods_id
                or actual.goods_sign != self.ref.goods_sign):
            raise ValueError("详情快照与商品身份不一致")
        data["platform"] = platform
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "fetched_at", utc_millis(self.fetched_at))


@dataclass(frozen=True)
class DetailPrefetchResult:
    """预取内部结果不冒用HTTP成功；只有ready代表已确认持久化。"""
    status: str
    snapshot: Optional[DetailSnapshot] = None
    retryable: bool = False
    next_check_at: float = 0.0
    error_code: str = ""
    error_msg: str = ""
