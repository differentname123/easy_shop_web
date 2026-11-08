# -*- coding: utf-8 -*-
r"""
=========================================================================================
[功能摘要] 闲鱼 (Goofish) 高性价比商品自动上架工具（基于独立浏览器目录持久化账号态）
[输入数据]
    - user_data_dir : 本地独立浏览器目录路径（默认 r"W:\temp\xianyu_myself"，账号信息全在目录内）
    - title         : 商品标题（自动截断至 30 字以内并清洗极限词）
    - desc          : 商品详情文案（自动剥离站外推广短链、淘口令与极限词）
    - images        : 商品图片列表（支持本地图片路径或 http/https 远程图片 URL，1~9 张）
    - price         : 闲鱼售卖价（元）
[数据流转/交互]
    1. 浏览器目录挂载：通过 Playwright `launch_persistent_context` 挂载独立浏览器目录 `W:\temp\xianyu_myself`。
    2. 登录态自愈：进入闲鱼发布页，若当前浏览器目录未登录，自动挂起等待人工扫码；扫码后账号信息永久保存在该浏览器目录中。
    3. 会话同步：直接从当前真实浏览器上下文提取内存 Cookie 与 User-Agent（不产生任何外部 cookies.json 文件）。
    4. 五步上架链路：
       - Step 0: 文案净水合规清洗 + 图片无损微扰防撞图
       - Step 1: 上传商品图至 `stream-upload.goofish.com`
       - Step 2: 调用官方 AI 知识图谱 `mtop.taobao.idle.kgraph.property.recommend` 预测四元组类目
       - Step 3: 调用 `mtop.taobao.idle.local.poi.get` 获取账号常驻发货地
       - Step 4: 调用 `mtop.idle.pc.idleitem.publish` 正式提交发布获取 `itemId`
       - Step 5: 调用 `mtop.taobao.idle.pc.detail` 回读核验真实在架状态
=========================================================================================
"""

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] %(message)s")
logger = logging.getLogger("publish_to_xianyu")

# ==============================================================================
# 全局默认配置
# ==============================================================================
DEFAULT_USER_DATA_DIR = r"W:\temp\xianyu_myself"
GOOFISH_HOME_URL = "https://www.goofish.com"
GOOFISH_PUBLISH_URL = "https://www.goofish.com/publish"
MTOP_HOST = "https://h5api.m.goofish.com"
UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api"
APP_KEY = "34839810"


# ==============================================================================
# 一、 独立浏览器目录管理（账号信息全量保存在 user_data_dir 中）
# ==============================================================================
def clean_browser_cache(user_data_dir: str) -> None:
    """
    清理浏览器冗余缓存，防止目录体积膨胀。
    严格保留 Cookie / LocalStorage / IndexedDB 等核心账号登录凭证。
    """
    if not os.path.exists(user_data_dir):
        return

    garbage_folders = (
        "Cache",
        "Code Cache",
        "GPUCache",
        "ShaderCache",
        "GrShaderCache",
        "Service Worker",
        "CacheStorage",
    )
    deleted_count = 0
    for base in (user_data_dir, os.path.join(user_data_dir, "Default")):
        for name in garbage_folders:
            path = os.path.join(base, name)
            if not os.path.exists(path):
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                deleted_count += 1
            except Exception:
                pass

    logger.info(f"[环境/瘦身] 冗余缓存清理完毕 | 目录: <{user_data_dir}> | 释放项数: 【{deleted_count}】")


def launch_persistent_context(
    p,
    user_data_dir: str,
    args: Optional[List[str]] = None,
    viewport: Optional[Dict[str, int]] = None,
    hide_automation: bool = True,
    headless: bool = False,
):
    """全局统一的持久化浏览器上下文启动器（直接挂载独立浏览器目录）。"""
    os.makedirs(user_data_dir, exist_ok=True)
    default_args = args or [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "--window-position=0,0",
    ]
    kwargs: Dict[str, Any] = {
        "channel": "chrome",
        "user_data_dir": user_data_dir,
        "headless": headless,
        "args": default_args,
    }
    if viewport:
        kwargs["viewport"] = viewport
    else:
        kwargs["no_viewport"] = True
    if hide_automation:
        kwargs["ignore_default_args"] = ["--enable-automation"]

    return p.chromium.launch_persistent_context(**kwargs)


def open_browser_for_manual_use(user_data_dir: str = DEFAULT_USER_DATA_DIR, home_url: str = GOOFISH_PUBLISH_URL) -> None:
    """
    启动独立浏览器目录并移交控制权给人工操作（如扫码登录、查看商品），关闭浏览器窗口后自动保存退出。
    """
    logger.info(f"\n{'=' * 60}\n[环境/接管] 启动闲鱼专属浏览器 | 目录: <{user_data_dir}>\n{'=' * 60}")
    clean_browser_cache(user_data_dir)
    with sync_playwright() as p:
        context = None
        try:
            context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.bring_to_front()
            page.goto(home_url)
            logger.info("[环境/接管] ✅ 浏览器已就绪 | 提示: [扫码登录或操作完成后，直接关闭浏览器窗口即可自动保存]")
            page.wait_for_event("close", timeout=0)
        except Exception as e:
            logger.warning(f"[环境/接管] 浏览器窗口已关闭: {e}")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            logger.info("[环境/接管] 👋 账号信息已保存在浏览器目录中，资源已释放\n")


def _extract_cookies_dict(context) -> Dict[str, str]:
    """从当前持久化浏览器上下文中提取最新 Cookie 字典。"""
    cookies = context.cookies(["https://www.goofish.com", "https://h5api.m.goofish.com", "https://www.taobao.com"])
    return {
        str(c.get("name")).strip(): str(c.get("value")).strip()
        for c in cookies
        if c.get("name") and c.get("value")
    }


def _build_session_from_context(context, page) -> Tuple[requests.Session, str, str]:
    """
    将浏览器目录当前的真实 Cookie 与 User-Agent 同步到内存 requests.Session。
    返回: (session, unb, user_agent)
    """
    flat = _extract_cookies_dict(context)
    unb = flat.get("unb", "")
    try:
        ua = page.evaluate("() => navigator.userAgent")
    except Exception:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    s = requests.Session()
    for k, v in flat.items():
        s.cookies.set(k, v)
    return s, unb, ua


def ensure_logged_in_context(context, page, wait_login_sec: int = 300) -> Tuple[requests.Session, str, str, Dict[str, Any]]:
    """
    在已打开的持久化浏览器中进入闲鱼并确保已登录：
    - 若浏览器目录内已有登录态，直接验证通过；
    - 若尚未登录，自动置顶浏览器窗口等待人工扫码登录，登录成功后自动继续。
    """
    logger.info(f"[登录/检查] 正在打开闲鱼页面检测账号登录态: {GOOFISH_PUBLISH_URL}")
    page.goto(GOOFISH_PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2.0)

    deadline = time.time() + wait_login_sec
    prompted = False

    while time.time() < deadline:
        flat = _extract_cookies_dict(context)
        if flat.get("unb") and flat.get("_m_h5_tk") and "_" in flat.get("_m_h5_tk", ""):
            session, unb, ua = _build_session_from_context(context, page)
            user_info = check_login_user(session, ua)
            if user_info.get("valid"):
                logger.info(
                    f"[登录/就绪] ✅ 账号登录态有效 | 昵称: 【{user_info.get('nick')}】 | UNB: 【{unb}】"
                )
                return session, unb, ua, user_info

        if not prompted:
            prompted = True
            try:
                page.bring_to_front()
            except Exception:
                pass
            logger.info(
                f"\n{'=' * 65}\n"
                f"👉 当前浏览器目录尚未登录闲鱼，请在弹出的 Chrome 窗口中用【手机闲鱼/淘宝 App】扫码登录！\n"
                f"⏳ 登录成功后脚本将自动检测并继续（最长等待 {wait_login_sec} 秒，账号将永久保存在该目录中）...\n"
                f"{'=' * 65}"
            )
        time.sleep(2.0)

    raise RuntimeError(f"等待扫码登录超时（{wait_login_sec} 秒），请确认已在弹出的 Chrome 窗口中完成登录。")


# ==============================================================================
# 二、 好价文案净水器与图片防撞处理（账号安全护栏）
# ==============================================================================
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_SHORTLINK_RE = re.compile(
    r"(?:u\.jd\.com|3\.cn|m\.tb\.cn|s\.click\.taobao\.com|yangkeduo\.com|pinduoduo\.com)/[A-Za-z0-9_/?=&.-]+",
    re.IGNORECASE,
)
_TAOKOULING_RE = re.compile(r"[￥¥$€£₴¢][0-9A-Za-z]{8,14}[￥¥$€£₴¢]|[\d]{1,2}\.[\d]+\s*[A-Za-z]{2,4}:/[A-Za-z0-9/]+")

_EXTREME_WORD_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    ("全网最低价", "高性价比"),
    ("全网最低", "诚意好价"),
    ("全网底价", "超值好价"),
    ("史上最低", "超值特惠"),
    ("历史最低", "超值好价"),
    ("国家级", "高品质"),
    ("第一品牌", "口碑品牌"),
    ("最便宜", "实惠"),
    ("顶级", "优质"),
    ("极品", "精品"),
    ("绝对正品", "正品保障"),
    ("100%正品", "正品保障"),
    ("假一赔十", "支持验货"),
    ("假一罚十", "支持验货"),
    ("专柜正品", "正品好物"),
    ("特效", "实用"),
    ("神效", "好用"),
)

_PROHIBITED_KEYWORDS: Tuple[str, ...] = (
    "加微信",
    "加v",
    "加V",
    "+v",
    "+V",
    "微信号",
    "wx号",
    "VX号",
    "绿泡泡",
    "私聊转账",
    "站外交易",
    "先款后货",
    "刷单",
    "套现",
    "高仿",
    "精仿",
    "A货",
)


def sanitize_title_and_desc(title: str, desc: str) -> Tuple[str, str, List[str]]:
    """
    清洗商品标题与详情文案：
    1. 自动剥离站外推广短链（u.jd.com / m.tb.cn 等）与淘口令；
    2. 自动替换极限广告词；
    3. 拦截诱导站外交易与违禁词；
    4. 标题严格控制在 30 字以内。
    """
    clean_title = _TAOKOULING_RE.sub("", _SHORTLINK_RE.sub("", _URL_RE.sub("", str(title or ""))))
    clean_desc = _TAOKOULING_RE.sub("", _SHORTLINK_RE.sub("", _URL_RE.sub("", str(desc or ""))))

    replaced: List[str] = []
    for bad, good in _EXTREME_WORD_REPLACEMENTS:
        if bad in clean_title or bad in clean_desc:
            clean_title = clean_title.replace(bad, good)
            clean_desc = clean_desc.replace(bad, good)
            replaced.append(f"{bad}->{good}")

    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in clean_desc.splitlines()]
    clean_desc = "\n".join(ln for ln in lines if ln).strip()

    if len(clean_title) > 30:
        clean_title = clean_title[:30].rstrip()

    combined = f"{clean_title}\n{clean_desc}"
    for kw in _PROHIBITED_KEYWORDS:
        if kw.lower() in combined.lower():
            raise ValueError(f"文案触发闲鱼违禁/外联拦截词: 【{kw}】，已阻止发布以保护账号安全")

    if not clean_title:
        raise ValueError("清洗后商品标题为空")
    if len(clean_desc) < 5:
        raise ValueError(f"商品描述过短: {clean_desc}")

    return clean_title, clean_desc, replaced


def _prepare_image_bytes(image_source: str, session: requests.Session, ua: str) -> Tuple[str, bytes, str]:
    """读取本地图片或下载远程图片，并对 JPEG 注入无损注释块微扰文件 Hash 防撞图。"""
    src = str(image_source or "").strip()
    if src.lower().startswith(("http://", "https://")):
        resp = session.get(src, headers={"User-Agent": ua}, timeout=25)
        resp.raise_for_status()
        raw_bytes = resp.content
        fallback_name = os.path.basename(urlsplit(src).path) or "item.jpg"
    else:
        path = Path(src).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"本地图片不存在: {src}")
        raw_bytes = path.read_bytes()
        fallback_name = path.name

    if raw_bytes.startswith(b"\xff\xd8\xff"):
        mime, ext = "image/jpeg", ".jpg"
    elif raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, ext = "image/png", ".png"
    elif len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        mime, ext = "image/webp", ".webp"
    else:
        guessed, _ = mimetypes.guess_type(fallback_name)
        mime = guessed if (guessed and guessed.startswith("image/")) else "image/jpeg"
        ext = mimetypes.guess_extension(mime) or ".jpg"

    # 对 JPEG 尾部注入无损 COM 段微扰 MD5，避免多商品撞图限流
    if mime == "image/jpeg" and len(raw_bytes) > 4 and raw_bytes.endswith(b"\xff\xd9"):
        seed = f"xy_{int(time.time() * 1000000)}_{random.randint(100, 999)}".encode("ascii")
        length = len(seed) + 2
        raw_bytes = raw_bytes[:-2] + b"\xff\xfe" + length.to_bytes(2, "big") + seed + b"\xff\xd9"

    filename = fallback_name if fallback_name.lower().endswith(ext) else f"goods_{int(time.time())}{ext}"
    return filename, raw_bytes, mime


# ==============================================================================
# 三、 闲鱼 H5 MTOP 原生协议核心方法
# ==============================================================================
def _stable_device_id(unb: str) -> str:
    """根据账号 unb 生成确定性的固定 device_id，保证同一账号设备指纹恒定。"""
    rng = random.Random(f"xianyu_device_{unb}")
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    out: List[str] = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            out.append("-")
        elif i == 14:
            out.append("4")
        else:
            val = int(rng.random() * 16)
            if i == 19:
                val = (val & 3) | 8
            out.append(chars[val])
    return f"{''.join(out)}-{unb}"


def _get_h5_token(session: requests.Session) -> str:
    for c in session.cookies:
        if c.name == "_m_h5_tk" and c.value and "_" in c.value:
            return c.value.split("_")[0].strip()
    return ""


def _mtop_headers(ua: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "origin": GOOFISH_HOME_URL,
        "referer": f"{GOOFISH_HOME_URL}/",
        "user-agent": ua,
    }


def mtop_request(
    session: requests.Session,
    ua: str,
    api: str,
    data: Dict[str, Any],
    *,
    version: str = "1.0",
    spm_cnt: str = "a21ybx.publish.0.0",
    _retry: bool = True,
) -> Dict[str, Any]:
    """执行闲鱼 H5 MTOP 签名请求，支持 `_m_h5_tk` 自动续期与风控熔断检测。"""
    token = _get_h5_token(session)
    t_ms = str(int(time.time() * 1000))
    data_json = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    sign = hashlib.md5(f"{token}&{t_ms}&{APP_KEY}&{data_json}".encode("utf-8")).hexdigest()

    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": t_ms,
        "sign": sign,
        "v": version,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": api,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": spm_cnt,
    }
    url = f"{MTOP_HOST}/h5/{api}/{version}/"
    resp = session.post(url, params=params, headers=_mtop_headers(ua), data={"data": data_json}, timeout=25)
    resp.raise_for_status()
    raw = resp.json()

    ret_list = raw.get("ret") or []
    ret_str = " | ".join(str(x) for x in ret_list) if isinstance(ret_list, list) else str(ret_list)
    if not ret_str or "SUCCESS" in ret_str:
        return raw

    if any(k in ret_str for k in ("RGV587_ERROR", "FAIL_SYS_USER_VALIDATE", "FAIL_SYS_ILLEGAL_ACCESS", "哎哟喂")):
        raise RuntimeError(f"[{api}] 触发闲鱼风控拦截 ({ret_str})，已立即停止请求以保护账号！")

    if _retry and any(k in ret_str for k in ("TOKEN_EXOIRED", "TOKEN_EXPIRED", "TOKEN_EMPTY")):
        new_token = _get_h5_token(session)
        if new_token and new_token != token:
            return mtop_request(session, ua, api, data, version=version, spm_cnt=spm_cnt, _retry=False)

    raise RuntimeError(f"[{api}] 接口返回错误: {ret_str}")


def check_login_user(session: requests.Session, ua: str) -> Dict[str, Any]:
    """验证当前闲鱼登录态并获取用户昵称。"""
    try:
        raw = mtop_request(
            session,
            ua,
            api="mtop.taobao.idlemessage.pc.loginuser.get",
            data={},
            version="1.0",
            spm_cnt="a21ybx.im.0.0",
        )
        user_data = (raw.get("data") or {}) if isinstance(raw, dict) else {}
        return {
            "valid": True,
            "nick": str(user_data.get("nick") or ""),
            "user_id": str(user_data.get("userId") or ""),
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def upload_image(session: requests.Session, ua: str, image_source: str) -> Dict[str, Any]:
    """Step 1: 上传商品图片至闲鱼 `stream-upload.goofish.com`。"""
    filename, img_bytes, mime = _prepare_image_bytes(image_source, session, ua)
    headers = {
        "accept": "*/*",
        "origin": GOOFISH_HOME_URL,
        "referer": f"{GOOFISH_HOME_URL}/",
        "user-agent": ua,
    }
    params = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}
    resp = session.post(
        UPLOAD_URL, headers=headers, params=params, files={"file": (filename, img_bytes, mime)}, timeout=45
    )
    resp.raise_for_status()
    raw = resp.json()
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise RuntimeError(f"图片上传失败: {raw}")

    obj = raw.get("object") or {}
    w_str, h_str = str(obj.get("pix") or "800x800").lower().split("x")
    return {
        "url": str(obj.get("url")),
        "width": int(w_str),
        "height": int(h_str),
        "source": image_source,
    }


def recommend_category(session: requests.Session, ua: str, title: str, uploaded_images: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Step 2: 调用闲鱼 AI 知识图谱预测商品类目四元组。"""
    unique_code = str(time.time_ns() // 1000)
    image_infos = [
        {
            "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
            "isQrCode": False,
            "url": img["url"],
            "heightSize": img["height"],
            "widthSize": img["width"],
            "major": True,
            "type": 0,
            "status": "done",
        }
        for img in uploaded_images[:9]
    ]
    data: Dict[str, Any] = {
        "itemType": "common",
        "simpleItem": "true",
        "title": title,
        "uniqueCode": unique_code,
    }
    if image_infos:
        data["imageInfos"] = json.dumps(image_infos, separators=(",", ":"), ensure_ascii=False)

    raw = mtop_request(session, ua, api="mtop.taobao.idle.kgraph.property.recommend", data=data, version="2.0")
    card_list = (raw.get("data") or {}).get("cardList") or []
    cat_do = (((card_list[0] if card_list else {}).get("cardData") or {}).get("categoryDO")) or {}
    if not cat_do.get("categoryId") or not cat_do.get("channelCatId"):
        raise RuntimeError(f"未能预测出有效闲鱼类目: {raw}")

    return {
        "cat_id": str(cat_do["categoryId"]),
        "cat_name": str(cat_do.get("categoryName") or ""),
        "channel_cat_id": str(cat_do["channelCatId"]),
        "tb_cat_id": str(cat_do.get("tbCategoryId") or ""),
        "pv_list": cat_do.get("pvList") or [],
    }


def get_default_location(session: requests.Session, ua: str) -> Dict[str, Any]:
    """Step 3: 获取账号常驻默认发货地址。"""
    raw = mtop_request(session, ua, api="mtop.taobao.idle.local.poi.get", data={}, version="1.0")
    d = (raw.get("data") or {}) if isinstance(raw, dict) else {}
    return {
        "prov": d.get("prov") or "浙江省",
        "city": d.get("city") or "杭州市",
        "area": d.get("area") or "余杭区",
        "division_id": int(d.get("divisionId") or 3172),
        "latitude": d.get("latitude") or 30.274085,
        "longitude": d.get("longitude") or 120.15507,
        "poi_id": d.get("poiId") or "",
        "poi_name": d.get("poiName") or "",
    }


def _yuan_to_cent_str(yuan: float) -> str:
    d = Decimal(str(yuan)).quantize(Decimal("0.01"))
    return str(int(d * 100))


def submit_publish_item(
    session: requests.Session,
    ua: str,
    unb: str,
    *,
    title: str,
    desc: str,
    price: float,
    original_price: Optional[float],
    uploaded_images: List[Dict[str, Any]],
    category: Dict[str, Any],
    location: Dict[str, Any],
    delivery: str = "包邮",
    post_price: float = 0.0,
) -> Dict[str, Any]:
    """Step 4: 提交 `mtop.idle.pc.idleitem.publish` 正式发布闲鱼商品。"""
    if delivery == "包邮":
        free_shipping, freight_cent, template_id = True, "0", "0"
    elif delivery == "一口价":
        free_shipping, freight_cent, template_id = False, _yuan_to_cent_str(post_price), "0"
    else:
        free_shipping, freight_cent, template_id = False, "0", "0"

    item_Label_ext_list: List[Dict[str, Any]] = []
    for pv in category.get("pv_list") or []:
        if not isinstance(pv, dict):
            continue
        p_id, p_name = pv.get("propertyId"), pv.get("propertyName")
        vals = pv.get("valueList") or []
        v0 = vals[0] if vals and isinstance(vals[0], dict) else {}
        v_id, v_name = v0.get("valueId"), v0.get("valueName")
        if p_id and v_id:
            item_Label_ext_list.append({
                "channelCateId": category["channel_cat_id"],
                "valueId": v_id,
                "channelPvId": f"{p_id}:{v_id}",
                "valueName": v_name,
                "tbCatId": category["tb_cat_id"],
                "subPropertyId": f"{category['channel_cat_id']}:{v_id}",
                "labelId": None,
                "propertyName": p_name,
                "isNew": True,
                "propertyId": p_id,
                "text": f"{p_name}:{v_name}",
                "properties": f"{p_id}:{v_id}##{p_name}:{v_name}",
            })

    price_cent = _yuan_to_cent_str(price)
    orig_cent = _yuan_to_cent_str(original_price) if (original_price and original_price > 0) else ""
    unique_code = str(time.time_ns() // 1000)

    payload = {
        "freeShipping": free_shipping,
        "itemCatDTO": {
            "catId": category["cat_id"],
            "catName": category["cat_name"],
            "channelCatId": category["channel_cat_id"],
            "tbCatId": category["tb_cat_id"],
        },
        "imageInfoDOList": [
            {
                "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
                "isQrCode": False,
                "url": img["url"],
                "heightSize": img["height"],
                "widthSize": img["width"],
                "major": idx == 0,
                "type": 0,
                "status": "done",
            }
            for idx, img in enumerate(uploaded_images[:9])
        ],
        "itemPriceDTO": {"origPriceInCent": orig_cent, "priceInCent": price_cent},
        "itemPostFeeDTO": {
            "canFreeShipping": free_shipping,
            "supportFreight": True,
            "OnlyTakeSelf": False,
            "freightInCent": freight_cent,
            "templateId": template_id,
        },
        "itemAddrDTO": {
            "prov": location["prov"],
            "city": location["city"],
            "area": location["area"],
            "divisionId": location["division_id"],
            "gps": f"{location['longitude']},{location['latitude']}",
            "poiId": location.get("poi_id") or "",
            "poiName": location.get("poi_name") or "",
        },
        "itemTextDTO": {
            "desc": desc,
            "title": title,
            "titleDescSeparate": False,
        },
        "itemLabelExtList": item_Label_ext_list,
        "itemStuffStatus": 100,
        "outerId": {"outId": "", "outerSys": "other"},
        "publishScene": "pcPublish",
        "bizcode": "pcMainPublish",
        "quantity": "1",
        "simpleItem": "true",
        "UniqueCode": unique_code,
        "sourceId": "pcMainPublish",
        "deviceId": _stable_device_id(unb),
    }

    raw = mtop_request(session, ua, api="mtop.idle.pc.idleitem.publish", data=payload, version="1.0")
    resp_data = (raw.get("data") or {}) if isinstance(raw, dict) else {}
    item_id = resp_data.get("itemId") or (resp_data.get("itemDO") or {}).get("itemId")
    if not item_id:
        raise RuntimeError(f"发布接口未返回 itemId: {raw}")
    return {"item_id": str(item_id), "item_url": f"{GOOFISH_HOME_URL}/item?id={item_id}"}


def get_item_detail(session: requests.Session, ua: str, item_id: str) -> Dict[str, Any]:
    """Step 5: 回读核验已发布商品的在架状态。"""
    raw = mtop_request(
        session,
        ua,
        api="mtop.taobao.idle.pc.detail",
        data={"itemId": str(item_id)},
        version="1.0",
        spm_cnt="a21ybx.item.0.0",
    )
    item_do = ((raw.get("data") or {}).get("itemDO")) or {}
    return {
        "item_id": str(item_do.get("itemId") or item_id),
        "title": str(item_do.get("title") or ""),
        "price": item_do.get("soldPrice"),
        "item_status": item_do.get("itemStatus"),
        "browse_count": item_do.get("browseCnt", 0),
    }


# ==============================================================================
# 四、 对外统一调用入口：验证账号 & 上架商品
# ==============================================================================
def verify_xianyu_account(user_data_dir: str = DEFAULT_USER_DATA_DIR, wait_login_sec: int = 300) -> Dict[str, Any]:
    """
    打开独立浏览器目录 `user_data_dir`，若未登录则等待人工扫码，
    随后实测调用闲鱼线上接口（用户信息 + AI 类目预测 + 默认发货地）。
    """
    clean_browser_cache(user_data_dir)
    with sync_playwright() as p:
        context = None
        try:
            context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            session, unb, ua, user_info = ensure_logged_in_context(context, page, wait_login_sec=wait_login_sec)

            # 实测线上类目预测与默认发货地接口
            cat = recommend_category(session, ua, "全新维达抽纸整箱24包家用实惠装", [])
            loc = get_default_location(session, ua)

            result = {
                "ok": True,
                "user_data_dir": user_data_dir,
                "nick": user_info.get("nick"),
                "unb": unb,
                "sample_predicted_category": f"{cat['cat_name']} ({cat['cat_id']})",
                "default_location": f"{loc['prov']}{loc['city']}{loc['area']}",
            }
            logger.info(f"[账号/实测通过] {json.dumps(result, ensure_ascii=False)}")
            return result
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass


def publish_goods_to_xianyu(
    title: str,
    desc: str,
    images: List[str],
    price: float,
    *,
    original_price: Optional[float] = None,
    delivery: str = "包邮",
    post_price: float = 0.0,
    user_data_dir: str = DEFAULT_USER_DATA_DIR,
    confirm: bool = False,
    wait_login_sec: int = 300,
) -> Dict[str, Any]:
    """
    将指定文案、图片和价格的商品发布到闲鱼：
    - `confirm=False`（默认）：只做文案净水与线上登录态、类目预测、发货地预检，不实际发帖；
    - `confirm=True`：完整执行图片上传 + 正式上架 + 详情回读核验。
    """
    if not images or not (1 <= len(images) <= 9):
        raise ValueError("闲鱼商品必须提供 1~9 张图片路径或 URL")
    if price <= 0:
        raise ValueError("商品售价必须大于 0 元")

    clean_title, clean_desc, replaced = sanitize_title_and_desc(title, desc)
    logger.info(f"[文案/净水] 标题({len(clean_title)}字): {clean_title} | 替换极限词: {replaced}")

    clean_browser_cache(user_data_dir)
    with sync_playwright() as p:
        context = None
        try:
            context = launch_persistent_context(p, user_data_dir=user_data_dir, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            session, unb, ua, user_info = ensure_logged_in_context(context, page, wait_login_sec=wait_login_sec)

            # Step 1: 若 confirm=True 则真实上传图片；预检模式仅验证前序接口
            uploaded_imgs: List[Dict[str, Any]] = []
            if confirm:
                for idx, img_src in enumerate(images, start=1):
                    logger.info(f"[上架/Step1] ({idx}/{len(images)}) 正在上传图片: {img_src}")
                    uploaded_imgs.append(upload_image(session, ua, img_src))
                    time.sleep(random.uniform(1.0, 2.0))

            # Step 2: 预测闲鱼四元组类目
            logger.info("[上架/Step2] 正在调用闲鱼 AI 预测商品类目...")
            category = recommend_category(session, ua, clean_title, uploaded_imgs)
            time.sleep(random.uniform(0.8, 1.5))

            # Step 3: 获取常驻发货地
            logger.info("[上架/Step3] 正在读取账号常驻发货地...")
            location = get_default_location(session, ua)

            if not confirm:
                preview = {
                    "ok": True,
                    "mode": "preview_only",
                    "tip": "当前为线上预检模式（已实测登录态、AI类目预测与常驻发货地），追加 --confirm 即可正式发布上架",
                    "user_data_dir": user_data_dir,
                    "nick": user_info.get("nick"),
                    "title": clean_title,
                    "desc": clean_desc,
                    "price": price,
                    "images": images,
                    "predicted_category": f"{category['cat_name']} ({category['cat_id']})",
                    "location": f"{location['prov']}{location['city']}{location['area']}",
                }
                logger.info(f"[上架/预检完成]\n{json.dumps(preview, ensure_ascii=False, indent=2)}")
                return preview

            # Step 4: 正式提交发布
            time.sleep(random.uniform(1.2, 2.2))
            logger.info("[上架/Step4] 正在提交闲鱼发布接口...")
            pub_res = submit_publish_item(
                session,
                ua,
                unb,
                title=clean_title,
                desc=clean_desc,
                price=price,
                original_price=original_price,
                uploaded_images=uploaded_imgs,
                category=category,
                location=location,
                delivery=delivery,
                post_price=post_price,
            )

            # Step 5: 回读核验详情
            time.sleep(random.uniform(1.0, 1.8))
            detail = get_item_detail(session, ua, pub_res["item_id"])

            final_res = {
                "ok": True,
                "mode": "published",
                "user_data_dir": user_data_dir,
                "nick": user_info.get("nick"),
                "item_id": pub_res["item_id"],
                "item_url": pub_res["item_url"],
                "title": clean_title,
                "price": price,
                "category": f"{category['cat_name']} ({category['cat_id']})",
                "location": f"{location['prov']}{location['city']}{location['area']}",
                "readback_detail": detail,
            }
            logger.info(f"[上架/成功] 🎉 商品已上架: {pub_res['item_url']}")
            return final_res
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass


# ==============================================================================
# 五、 命令行与直接运行入口
# ==============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="闲鱼高性价比商品上架工具（基于独立浏览器目录持久化账号）")
    parser.add_argument("--user-data-dir", type=str, default=DEFAULT_USER_DATA_DIR, help="独立浏览器目录路径")
    parser.add_argument("--manual", action="store_true", help="手动打开浏览器目录自由操作（关闭窗口即保存退出）")
    parser.add_argument("--title", type=str, default="", help="商品标题（<=30字）")
    parser.add_argument("--desc", type=str, default="", help="商品详情文案")
    parser.add_argument("--images", nargs="+", default=[], help="商品图片路径或 URL 列表（1~9张）")
    parser.add_argument("--price", type=float, default=0.0, help="出售价格（元）")
    parser.add_argument("--original-price", type=float, default=None, help="原价（元，可选）")
    parser.add_argument("--confirm", action="store_true", help="确认正式发布上架（不加则执行线上登录+类目+地址预检）")
    args = parser.parse_args()

    if args.manual:
        open_browser_for_manual_use(user_data_dir=args.user_data_dir)
    elif args.title and args.desc and args.images and args.price > 0:
        res = publish_goods_to_xianyu(
            title=args.title,
            desc=args.desc,
            images=args.images,
            price=args.price,
            original_price=args.original_price,
            user_data_dir=args.user_data_dir,
            confirm=args.confirm,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        # 默认直接打开独立浏览器目录 W:\temp\xianyu_myself 校验登录态并实测线上接口
        res = verify_xianyu_account(user_data_dir=args.user_data_dir)
        print(json.dumps(res, ensure_ascii=False, indent=2))
