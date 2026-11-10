# -*- coding: utf-8 -*-
"""拼多多多人团互助独立模块单元测试与端到端闭环验证（全内存离线测试，无外部依赖）。"""
import base64
import io
import json
import re
from copy import deepcopy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import group_help as group_help_api
from main import create_app
from models.group_help import (
    GROUP_STATUS_ACTIVE,
    GROUP_STATUS_EXPIRED,
    GROUP_STATUS_FULL,
    PARSE_STATUS_PARSED,
    PARSE_STATUS_PENDING,
)
from repositories.group_help_repo import GroupHelpRepository
from services.group_help_service import (
    GroupHelpService,
    decode_qr_from_image_bytes,
)


class _MemoryCursor:
    def __init__(self, docs):
        self.docs = list(docs)
        self.offset = 0
        self.limit_n = None

    def sort(self, key_or_list, direction=None):
        if isinstance(key_or_list, str):
            specs = [(key_or_list, direction or 1)]
        else:
            specs = list(key_or_list)
        for key, dir_val in reversed(specs):
            self.docs.sort(key=lambda d: (d.get(key) is not None, d.get(key)), reverse=(dir_val < 0))
        return self

    def skip(self, n):
        self.offset = int(n)
        return self

    def limit(self, n):
        self.limit_n = int(n)
        return self

    def __iter__(self):
        end = None if self.limit_n is None else (self.offset + self.limit_n)
        return iter(deepcopy(self.docs[self.offset : end]))


class _MemoryCollection:
    def __init__(self):
        self.docs = []
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    @classmethod
    def _match(cls, doc, query):
        for k, v in query.items():
            if k == "$and":
                if not all(cls._match(doc, sub) for sub in v):
                    return False
                continue
            if k == "$or":
                if not any(cls._match(doc, sub) for sub in v):
                    return False
                continue
            val = doc.get(k)
            if isinstance(v, dict):
                if "$lt" in v and not (val is not None and val < v["$lt"]):
                    return False
                if "$lte" in v and not (val is not None and val <= v["$lte"]):
                    return False
                if "$gt" in v and not (val is not None and val > v["$gt"]):
                    return False
                if "$gte" in v and not (val is not None and val >= v["$gte"]):
                    return False
                if "$regex" in v:
                    flags = re.IGNORECASE if "i" in v.get("$options", "") else 0
                    pat = re.compile(v["$regex"], flags)
                    if isinstance(val, list):
                        if not any(isinstance(item, str) and pat.search(item) for item in val):
                            return False
                    elif not (isinstance(val, str) and pat.search(val)):
                        return False
            else:
                if val != v:
                    return False
        return True

    @staticmethod
    def _apply_update(doc, update):
        if "$set" in update:
            for k, v in update["$set"].items():
                doc[k] = deepcopy(v)
        if "$inc" in update:
            for k, v in update["$inc"].items():
                doc[k] = (doc.get(k) or 0) + v

    def find_one(self, query, projection=None):
        for d in self.docs:
            if self._match(d, query):
                res = deepcopy(d)
                res.pop("_id", None)
                return res
        return None

    def insert_one(self, doc):
        self.docs.append(deepcopy(doc))

    def find(self, query, projection=None):
        matched = [deepcopy(d) for d in self.docs if self._match(d, query)]
        for d in matched:
            d.pop("_id", None)
        return _MemoryCursor(matched)

    def count_documents(self, query):
        return sum(1 for d in self.docs if self._match(d, query))

    def find_one_and_update(self, query, update, projection=None, return_document=None):
        for d in self.docs:
            if self._match(d, query):
                self._apply_update(d, update)
                res = deepcopy(d)
                res.pop("_id", None)
                return res
        return None

    def update_many(self, query, update):
        count = 0
        for d in self.docs:
            if self._match(d, query):
                self._apply_update(d, update)
                count += 1
        return SimpleNamespace(modified_count=count)


class _MemoryDB:
    def __init__(self):
        self.collections = {}

    def get_collection(self, name):
        if name not in self.collections:
            self.collections[name] = _MemoryCollection()
        return self.collections[name]


def _make_poster_image_bytes(qr_text: str = "", poster_size=(750, 1200), position: str = "bottom_right") -> bytes:
    """生成模拟拼多多多人团分享海报图（支持二维码位于左上角、正中间或右下角等任意位置）。"""
    w, h = poster_size
    canvas = np.full((h, w, 3), 245, dtype=np.uint8)
    canvas[40:640, 40 : w - 40] = (220, 230, 240)

    if qr_text:
        encoder = cv2.QRCodeEncoder_create()
        qr_mat = encoder.encode(qr_text)
        qr_h, qr_w = qr_mat.shape[:2]
        scale = 6
        qr_resized = cv2.resize(qr_mat, (qr_w * scale, qr_h * scale), interpolation=cv2.INTER_NEAREST)
        qr_rgb = cv2.cvtColor(qr_resized, cv2.COLOR_GRAY2RGB)
        rh, rw = qr_rgb.shape[:2]
        if position == "top_left":
            y0, x0 = 60, 60
        elif position == "center":
            y0, x0 = (h - rh) // 2, (w - rw) // 2
        else:
            y0, x0 = h - rh - 80, w - rw - 60
        canvas[y0 : y0 + rh, x0 : x0 + rw] = qr_rgb

    pil_img = Image.fromarray(canvas)
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_qr_decoder_detects_qr_in_any_position():
    short_url = "https://file-link.pinduoduo.com/ntkk9Kybjt"
    for pos in ("bottom_right", "top_left", "center"):
        img_bytes = _make_poster_image_bytes(short_url, position=pos)
        decoded = decode_qr_from_image_bytes(img_bytes)
        assert decoded == short_url


def test_qr_decoder_returns_empty_for_image_without_qr():
    img_bytes = _make_poster_image_bytes("")
    assert decode_qr_from_image_bytes(img_bytes) == ""


def test_group_help_full_closed_loop(tmp_path, monkeypatch):
    """验证：二维码上传 -> 307免登录核验入库(pending) -> 定时任务调用大模型解析 -> 搜索与详情 -> 用户共创维护状态 -> 24h过期下线。"""
    repo = GroupHelpRepository(db_instance=_MemoryDB())

    redirect_loc = (
        "https://mobile.yangkeduo.com/group7.html"
        "?group_order_id=3495229659187101111"
        "&refer_share_channel=group_short_link"
        "&refer_share_uin=X4SHUDVGMG7HGQBVER6XRAMGHI_GEXDA"
        "&refer_share_id=83399013-a694-4d61-b7f4-dee941136d45"
    )

    def fake_http_get(url, headers=None, allow_redirects=False, timeout=8):
        assert url == "https://file-link.pinduoduo.com/ntkk9Kybjt"
        assert allow_redirects is False
        return SimpleNamespace(
            status_code=307,
            headers={"Location": redirect_loc},
            text="",
        )

    ai_calls = []

    def fake_ai_generator(prompt, file_paths=None, **kwargs):
        ai_calls.append({"prompt": prompt, "file_paths": file_paths})
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "goods_name": "维达超韧抽纸3层130抽24包整箱装家用实惠装",
                    "keywords": ["维达", "抽纸", "纸巾", "24包"],
                    "group_price": 26.9,
                    "original_price": 39.9,
                    "total_people": 3,
                    "remain_people": 2,
                    "remaining_seconds": 72000,
                },
                ensure_ascii=False,
            ),
        }

    service = GroupHelpService(
        repo=repo,
        poster_dir=tmp_path / "posters",
        http_get=fake_http_get,
        ai_generator=fake_ai_generator,
    )
    monkeypatch.setattr(group_help_api, "group_help_service", service)
    import config
    import main as main_module
    from dataclasses import replace
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    app = create_app()
    with TestClient(app) as client:
        # 1. 访问独立前端页面
        page_resp = client.get("/group_help")
        assert page_resp.status_code == 200
        assert "多人团互助" in page_resp.text

        # 2. 上传无二维码图片 -> 报错拦截
        no_qr_b64 = base64.b64encode(_make_poster_image_bytes("")).decode("ascii")
        bad_resp = client.post("/api/group_help/upload", json={"image_base64": no_qr_b64})
        assert bad_resp.status_code == 400
        assert "未在图片中识别到二维码" in bad_resp.json()["error_msg"]

        # 3. 上传非拼多多二维码图片 -> 报错拦截
        other_qr_b64 = base64.b64encode(_make_poster_image_bytes("https://example.com/test")).decode("ascii")
        other_resp = client.post("/api/group_help/upload", json={"image_base64": other_qr_b64})
        assert other_resp.status_code == 400
        assert "拼多多" in other_resp.json()["error_msg"]

        # 4. 上传真实的拼多多拼团二维码海报 -> 立即同步识别二维码并通过 307 提取 group_order_id 入库
        valid_b64 = base64.b64encode(
            _make_poster_image_bytes("https://file-link.pinduoduo.com/ntkk9Kybjt")
        ).decode("ascii")
        up_resp = client.post("/api/group_help/upload", json={"image_base64": f"data:image/jpeg;base64,{valid_b64}"})
        assert up_resp.status_code == 200
        up_data = up_resp.json()
        assert up_data["status"] == "success"
        assert up_data["is_new"] is True
        assert up_data["data"]["group_order_id"] == "3495229659187101111"
        assert up_data["data"]["parse_status"] == PARSE_STATUS_PENDING
        assert "pinduoduo://com.xunmeng.pinduoduo/group7.html?group_order_id=3495229659187101111" in up_data["data"]["app_wake_url"]

        # 5. 大模型尚未执行前：前台列表不展示半成品，但返回 pending_count = 1
        list_before = client.get("/api/group_help/list").json()["data"]
        assert list_before["items"] == []
        assert list_before["pending_count"] == 1

        # 6. 触发后台定时任务：调用大模型识别图片并更新数据表为 parsed
        batch_stats = service.process_pending_batch(batch_size=2)
        assert batch_stats["parsed"] == 1
        assert len(ai_calls) == 1

        # 7. 大模型解析完成后：列表可正常展示并支持关键词搜索
        list_after = client.get("/api/group_help/list").json()["data"]
        assert list_after["pending_count"] == 0
        assert len(list_after["items"]) == 1
        item = list_after["items"][0]
        assert item["parse_status"] == PARSE_STATUS_PARSED
        assert item["group_status"] == GROUP_STATUS_ACTIVE
        assert item["goods_name"] == "维达超韧抽纸3层130抽24包整箱装家用实惠装"
        assert item["group_price"] == 26.9
        assert item["total_people"] == 3
        assert item["remain_people"] == 2

        # 搜索命中测试
        search_hit = client.get("/api/group_help/list", params={"keyword": "维达 抽纸"}).json()["data"]
        assert len(search_hit["items"]) == 1
        search_miss = client.get("/api/group_help/list", params={"keyword": "苹果手机"}).json()["data"]
        assert len(search_miss["items"]) == 0

        # 海报图片读取测试
        poster_resp = client.get("/api/group_help/poster/3495229659187101111")
        assert poster_resp.status_code == 200
        assert poster_resp.headers["content-type"] == "image/jpeg"

        # 8. 记录跳转点击
        click_resp = client.post("/api/group_help/click/3495229659187101111").json()
        assert click_resp["data"]["click_count"] == 1

        # 9. 用户共创状态维护测试
        # (a) 反馈“我已参团” -> 还差人数从 2 减为 1
        fb1 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "joined_one"},
        ).json()
        assert fb1["data"]["remain_people"] == 1
        assert fb1["data"]["group_status"] == GROUP_STATUS_ACTIVE

        # 此时在“仅差1人 (almost)”筛选下可被搜出
        almost_list = client.get("/api/group_help/list", params={"filter_mode": "almost"}).json()["data"]
        assert len(almost_list["items"]) == 1

        # (b) 再次反馈“我已参团” -> 还差人数减为 0，自动变为 full (人已满) 并从进行中列表下线
        fb2 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "joined_one"},
        ).json()
        assert fb2["data"]["remain_people"] == 0
        assert fb2["data"]["group_status"] == GROUP_STATUS_FULL

        active_after_full = client.get("/api/group_help/list", params={"filter_mode": "active"}).json()["data"]
        assert len(active_after_full["items"]) == 0

        # (c) 用户共创直接校准人数：实测还差 3 人 -> 重新恢复为 active
        fb3 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "set_remain", "remain_people": 3},
        ).json()
        assert fb3["data"]["remain_people"] == 3
        assert fb3["data"]["group_status"] == GROUP_STATUS_ACTIVE

        # (d) 用户共创直接标记“已失效” -> 立即下线
        fb4 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "mark_expired"},
        ).json()
        assert fb4["data"]["group_status"] == GROUP_STATUS_EXPIRED


def test_24h_auto_expiration():
    """验证超过 24 小时后自动下线机制。"""
    repo = GroupHelpRepository(db_instance=_MemoryDB())
    base_ts = 1_700_000_000.0

    repo.create_or_refresh_initial_group(
        {
            "group_order_id": "3495229659187109999",
            "short_url": "https://file-link.pinduoduo.com/abc",
            "redirect_url": "https://mobile.yangkeduo.com/group7.html?group_order_id=3495229659187109999",
            "created_at": base_ts,
        }
    )
    repo.mark_parse_succeeded(
        "3495229659187109999",
        {"goods_name": "农夫山泉饮用天然水550ml*24瓶", "group_price": 19.9, "total_people": 3, "remain_people": 2},
        now=base_ts + 5,
    )

    # 23 小时后仍在线
    res_23h = repo.search_groups(now=base_ts + 23 * 3600)
    assert len(res_23h["items"]) == 1

    # 24 小时 + 1 秒后自动过期下线
    expired_now = base_ts + 24 * 3600 + 1
    res_24h = repo.search_groups(now=expired_now)
    assert len(res_24h["items"]) == 0
    doc = repo.get_by_group_order_id("3495229659187109999")
    assert doc["group_status"] == GROUP_STATUS_EXPIRED

    # 过期后通过 set_remain 重新上架，应自动恢复为 active 并顺延有效期
    relisted = repo.apply_user_feedback(
        "3495229659187109999",
        action="set_remain",
        remain_people=2,
        now=expired_now + 10,
    )
    assert relisted["group_status"] == GROUP_STATUS_ACTIVE
    assert relisted["remain_people"] == 2
    assert relisted["expires_at"] >= expired_now + 3600
    res_after_relist = repo.search_groups(now=expired_now + 20)
    assert len(res_after_relist["items"]) == 1


def test_search_sorts_by_click_count_ascending_first():
    """验证搜索与列表排序：前往次数少(click_count升序)的团排在前面，同名次下差人数少的优先。"""
    repo = GroupHelpRepository(db_instance=_MemoryDB())
    base_ts = 1_700_000_000.0

    # 团 A：较早发布，但 0 人前往 (click_count = 0)
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": "3495229659187100001",
            "short_url": "https://file-link.pinduoduo.com/a",
            "redirect_url": "https://mobile.yangkeduo.com/group7.html?group_order_id=3495229659187100001",
            "created_at": base_ts + 10,
        }
    )
    repo.mark_parse_succeeded(
        "3495229659187100001",
        {"goods_name": "安耳悠隔音耳塞", "group_price": 23.2, "total_people": 3, "remain_people": 2},
        now=base_ts + 15,
    )

    # 团 B：最新入库，但已有 3 人点击前往 (click_count = 3)
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": "3495229659187100002",
            "short_url": "https://file-link.pinduoduo.com/b",
            "redirect_url": "https://mobile.yangkeduo.com/group7.html?group_order_id=3495229659187100002",
            "created_at": base_ts + 100,
        }
    )
    repo.mark_parse_succeeded(
        "3495229659187100002",
        {"goods_name": "安耳悠睡眠耳塞", "group_price": 23.2, "total_people": 3, "remain_people": 1},
        now=base_ts + 105,
    )
    for _ in range(3):
        repo.increment_click("3495229659187100002", now=base_ts + 110)

    # 团 C：已有 1 人点击前往 (click_count = 1)
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": "3495229659187100003",
            "short_url": "https://file-link.pinduoduo.com/c",
            "redirect_url": "https://mobile.yangkeduo.com/group7.html?group_order_id=3495229659187100003",
            "created_at": base_ts + 50,
        }
    )
    repo.mark_parse_succeeded(
        "3495229659187100003",
        {"goods_name": "安耳悠降噪耳塞", "group_price": 23.2, "total_people": 3, "remain_people": 1},
        now=base_ts + 55,
    )
    repo.increment_click("3495229659187100003", now=base_ts + 60)

    res = repo.search_groups(keyword="耳塞", now=base_ts + 200)
    ordered_ids = [item["group_order_id"] for item in res["items"]]
    assert ordered_ids == [
        "3495229659187100001",  # 0 人前往
        "3495229659187100003",  # 1 人前往
        "3495229659187100002",  # 3 人前往
    ]


def test_incomplete_poster_rejected_and_remaining_time_formats(tmp_path):
    """
    验证：
    1. 用户仅截取局部二维码上传（无商品名称或无价格）时，即使二维码有效且 is_group_poster=True，也必须拦截为 failed，绝不上线大厅；
    2. 大模型返回不同格式的剩余时间（如 "05:30:00"、"剩余 12小时"、"18000"）均能被准确解析并作为存活倒计时，而非默认 24 小时。
    """
    from models.group_help import (
        PARSE_STATUS_FAILED,
        parse_remaining_seconds,
        validate_and_normalize_parsed_poster,
    )

    # 1. 多格式剩余时间解析验证
    assert parse_remaining_seconds(raw_time="23:00:00") == 23 * 3600
    assert parse_remaining_seconds(raw_time="05:30:00") == 5 * 3600 + 30 * 60
    assert parse_remaining_seconds(raw_time="剩余 12小时") == 12 * 3600
    assert parse_remaining_seconds(raw_time="45分钟") == 45 * 60
    assert parse_remaining_seconds(raw_time=None, raw_seconds="18000") == 18000
    assert parse_remaining_seconds(raw_time="00:00:00") == 0

    # 2. 缺关键字段校验拦截验证
    with pytest.raises(ValueError, match="商品名称"):
        validate_and_normalize_parsed_poster(
            {
                "is_group_poster": True,
                "goods_name": "",
                "group_price": None,
                "total_people": 0,
                "remain_people": 1,
            }
        )
    with pytest.raises(ValueError, match="拼团价格"):
        validate_and_normalize_parsed_poster(
            {
                "is_group_poster": True,
                "goods_name": "百事可乐500ml*20瓶",
                "group_price": None,
                "total_people": 3,
                "remain_people": 2,
            }
        )

    # 3. 服务层端到端拦截局部二维码图片 & 准确应用海报剩余时间
    import time as _time
    repo = GroupHelpRepository(db_instance=_MemoryDB())
    base_ts = _time.time()
    poster_dir = tmp_path / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)

    # 案例 A：局部二维码图（无商品名、无价格）
    gid_bad = "3495190924788381111"
    bad_img_path = poster_dir / f"{gid_bad}.jpg"
    bad_img_path.write_bytes(_make_poster_image_bytes("https://file-link.pinduoduo.com/bad"))
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": gid_bad,
            "short_url": "https://file-link.pinduoduo.com/bad",
            "redirect_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid_bad}",
            "poster_path": str(bad_img_path),
            "created_at": base_ts,
        }
    )

    def fake_ai_partial_qr(prompt, file_paths=None, **kwargs):
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "goods_name": "",
                    "keywords": [],
                    "group_price": None,
                    "original_price": None,
                    "total_people": 0,
                    "remain_people": 1,
                    "remaining_time": None,
                    "remaining_seconds": None,
                }
            ),
        }

    svc_bad = GroupHelpService(repo=repo, poster_dir=poster_dir, ai_generator=fake_ai_partial_qr)
    stats_bad = svc_bad.process_pending_batch(batch_size=2)
    assert stats_bad["parsed"] == 0
    assert stats_bad["failed"] == 1
    bad_doc = repo.get_by_group_order_id(gid_bad)
    assert bad_doc["parse_status"] == PARSE_STATUS_FAILED
    assert "商品名称" in bad_doc["parse_error"]
    assert len(repo.search_groups(now=base_ts + 10)["items"]) == 0

    # 案例 B：真实海报识别出剩余 5小时30分 ("05:30:00")，到期时间应为上传时间 + 19800s，而非 24h
    gid_good = "3495190924788382222"
    good_img_path = poster_dir / f"{gid_good}.jpg"
    good_img_path.write_bytes(_make_poster_image_bytes("https://file-link.pinduoduo.com/good"))
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": gid_good,
            "short_url": "https://file-link.pinduoduo.com/good",
            "redirect_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid_good}",
            "poster_path": str(good_img_path),
            "created_at": base_ts,
        }
    )

    def fake_ai_5h30m(prompt, file_paths=None, **kwargs):
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "goods_name": "百事可乐碳酸汽水500ml*20瓶整箱装",
                    "keywords": ["百事可乐", "汽水"],
                    "group_price": 32.9,
                    "original_price": 45.0,
                    "total_people": 3,
                    "remain_people": 2,
                    "remaining_time": "05:30:00",
                    "remaining_seconds": None,
                }
            ),
        }

    svc_good = GroupHelpService(repo=repo, poster_dir=poster_dir, ai_generator=fake_ai_5h30m)
    stats_good = svc_good.process_pending_batch(batch_size=2)
    assert stats_good["parsed"] == 1
    good_doc = repo.get_by_group_order_id(gid_good)
    assert good_doc["parse_status"] == PARSE_STATUS_PARSED
    assert good_doc["expires_at"] == base_ts + (5 * 3600 + 30 * 60)
    # 5小时后仍在架，6小时后自动下线（而非等到24小时）
    assert len(repo.search_groups(now=base_ts + 5 * 3600)["items"]) == 1
    assert len(repo.search_groups(now=base_ts + 6 * 3600)["items"]) == 0


