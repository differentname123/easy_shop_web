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

    @staticmethod
    def _get_field(doc, key):
        cur = doc
        for part in key.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

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
            val = cls._get_field(doc, k) if "." in k else doc.get(k)
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

    @staticmethod
    def _project_doc(doc, projection):
        res = deepcopy(doc)
        if projection is None:
            res.pop("_id", None)
            return res
        if all(v == 0 for v in projection.values()):
            for k in projection:
                res.pop(k, None)
            return res
        out = {}
        if projection.get("_id", 1) != 0 and "_id" in res:
            out["_id"] = res["_id"]
        for k, flag in projection.items():
            if k != "_id" and flag and k in res:
                out[k] = res[k]
        return out

    def find_one(self, query, projection=None):
        for d in self.docs:
            if self._match(d, query):
                return self._project_doc(d, projection)
        return None

    def insert_one(self, doc):
        self.docs.append(deepcopy(doc))

    def find(self, query, projection=None):
        matched = [self._project_doc(d, projection) for d in self.docs if self._match(d, query)]
        return _MemoryCursor(matched)

    def count_documents(self, query):
        return sum(1 for d in self.docs if self._match(d, query))

    def find_one_and_update(self, query, update, projection=None, return_document=None):
        for d in self.docs:
            if self._match(d, query):
                self._apply_update(d, update)
                return self._project_doc(d, projection)
        return None

    def update_one(self, query, update):
        for d in self.docs:
            if self._match(d, query):
                self._apply_update(d, update)
                return SimpleNamespace(acknowledged=True, matched_count=1, modified_count=1)
        return SimpleNamespace(acknowledged=True, matched_count=0, modified_count=0)

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


def test_qr_decoder_detects_qr_in_any_position_and_rejects_blank():
    short_url = "https://file-link.pinduoduo.com/ntkk9Kybjt"
    for pos in ("bottom_right", "top_left", "center"):
        img_bytes = _make_poster_image_bytes(short_url, position=pos)
        assert decode_qr_from_image_bytes(img_bytes) == short_url
    assert decode_qr_from_image_bytes(_make_poster_image_bytes("")) == ""


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
        assert up_data["owner_token"]
        owner_token = up_data["owner_token"]
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

        # 7. 大模型解析完成后：列表可正常展示并支持关键词搜索（绝不对外泄露 owner_token）
        list_after = client.get("/api/group_help/list").json()["data"]
        assert list_after["pending_count"] == 0
        assert len(list_after["items"]) == 1
        item = list_after["items"][0]
        assert "owner_token" not in item
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
        # (a) 匿名用户反馈“我已参团”（当前还差 2 人，不触发下架） -> 免登录直接从 2 减为 1
        fb1 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "joined_one"},
        ).json()
        assert fb1["data"]["remain_people"] == 1
        assert fb1["data"]["group_status"] == GROUP_STATUS_ACTIVE

        # 此时在“仅差1人 (almost)”筛选下可被搜出
        almost_list = client.get("/api/group_help/list", params={"filter_mode": "almost"}).json()["data"]
        assert len(almost_list["items"]) == 1

        # (b) 此时仅差 1 人，若匿名路人再次点击“我已参团”（会导致该团直接下架） -> 必须返回 401 要求登录
        anon_takedown = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "joined_one"},
        )
        assert anon_takedown.status_code == 401
        assert anon_takedown.json()["error_code"] == "auth_required"

        # 团长本人（JSON body 携带本地 owner_token）免登录即可管理/下架/恢复自己的团
        fb2 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "joined_one", "owner_token": owner_token},
        ).json()
        assert fb2["data"]["remain_people"] == 0
        assert fb2["data"]["group_status"] == GROUP_STATUS_FULL

        active_after_full = client.get("/api/group_help/list", params={"filter_mode": "active"}).json()["data"]
        assert len(active_after_full["items"]) == 0

        # (c) 团长本人恢复上架：实测还差 3 人 -> 重新恢复为 active，且不会误罚团长自己
        fb3 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "set_remain", "remain_people": 3, "owner_token": owner_token},
        ).json()
        assert fb3["data"]["remain_people"] == 3
        assert fb3["data"]["group_status"] == GROUP_STATUS_ACTIVE
        assert fb3["penalized_saboteur"] is False

        # (d) 团长直接标记“已失效” -> 立即下线
        fb4 = client.post(
            "/api/group_help/feedback/3495229659187101111",
            json={"action": "mark_expired", "owner_token": owner_token},
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

    # 有效期内若误标已满或误点已失效，均可恢复上架但绝不延长原定 expires_at
    orig_expires = repo.get_by_group_order_id("3495229659187109999")["expires_at"]
    repo.apply_user_feedback("3495229659187109999", action="mark_full", now=base_ts + 3600)
    restored = repo.apply_user_feedback(
        "3495229659187109999", action="set_remain", remain_people=2, now=base_ts + 3700
    )
    assert restored["group_status"] == GROUP_STATUS_ACTIVE
    assert restored["expires_at"] == orig_expires

    repo.apply_user_feedback("3495229659187109999", action="mark_expired", now=base_ts + 3800)
    assert repo.get_by_group_order_id("3495229659187109999")["group_status"] == GROUP_STATUS_EXPIRED
    restored_from_expired = repo.apply_user_feedback(
        "3495229659187109999", action="set_remain", remain_people=1, now=base_ts + 3900
    )
    assert restored_from_expired["group_status"] == GROUP_STATUS_ACTIVE
    assert restored_from_expired["remain_people"] == 1
    assert restored_from_expired["expires_at"] == orig_expires

    # 24 小时 + 1 秒后自动过期下线
    expired_now = base_ts + 24 * 3600 + 1
    res_24h = repo.search_groups(now=expired_now)
    assert len(res_24h["items"]) == 0
    doc = repo.get_by_group_order_id("3495229659187109999")
    assert doc["group_status"] == GROUP_STATUS_EXPIRED

    # 超过 24 小时已失效的拼团，严禁再通过 set_remain 重新上架或执行 mark_full 下架
    with pytest.raises(ValueError, match="无法重新上架或修改状态"):
        repo.apply_user_feedback(
            "3495229659187109999",
            action="set_remain",
            remain_people=2,
            now=expired_now + 10,
        )
    with pytest.raises(ValueError, match="无法重新上架或修改状态"):
        repo.apply_user_feedback(
            "3495229659187109999",
            action="mark_full",
            now=expired_now + 10,
        )


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

    # 案例 C：夹带外部广告或违规敏感信息的图片（即便带有商品名称与价格，is_compliant=False 也必须一票否决；物理文件保留在服务器供复盘分析，但禁止对外访问）
    gid_risk = "3495190924788383333"
    risk_img_path = poster_dir / f"{gid_risk}.jpg"
    risk_img_path.write_bytes(_make_poster_image_bytes("https://file-link.pinduoduo.com/risk"))
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": gid_risk,
            "short_url": "https://file-link.pinduoduo.com/risk",
            "redirect_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid_risk}",
            "poster_path": str(risk_img_path),
            "created_at": base_ts,
        }
    )

    def fake_ai_non_compliant(prompt, file_paths=None, **kwargs):
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "is_compliant": False,
                    "reject_reason": "图片包含非官方涂改或夹带外部广告及敏感违规内容",
                    "goods_name": "某商品名称",
                    "keywords": ["商品"],
                    "group_price": 19.9,
                    "original_price": 39.9,
                    "total_people": 3,
                    "remain_people": 2,
                    "remaining_time": "20:00:00",
                    "remaining_seconds": 72000,
                }
            ),
        }

    svc_risk = GroupHelpService(repo=repo, poster_dir=poster_dir, ai_generator=fake_ai_non_compliant)
    # 审核前（pending 状态）也禁止外部直接访问海报图片
    assert svc_risk.get_poster_file_path(gid_risk) is None
    stats_risk = svc_risk.process_pending_batch(batch_size=2)
    assert stats_risk["parsed"] == 0
    assert stats_risk["failed"] == 1
    risk_doc = repo.get_by_group_order_id(gid_risk)
    assert risk_doc["parse_status"] == PARSE_STATUS_FAILED
    assert "外部广告" in risk_doc["parse_error"]
    # 物理图片保留在服务器本地供后续分析误判/漏判，但对外接口严格禁止访问
    assert risk_img_path.exists()
    assert svc_risk.get_poster_file_path(gid_risk) is None


def test_duplicate_active_group_matching_and_confirm_publish(tmp_path):
    """
    验证：
    1. 商品名称高度相似且拼团价完全一致的在拼商品智能匹配；
    2. 匹配命中时新上传的团处于 pending_confirm 状态（暂不上架大厅），详情接口返回 matched_group 供前端弹窗提示用户直接去拼或继续上传自己的团；
    3. 用户点击“继续上传我的团”(confirm_publish) 后立即转为 active 上架大厅。
    """
    import time as _time
    from models.group_help import (
        GROUP_STATUS_PENDING_CONFIRM,
        is_highly_similar_goods,
    )

    # 1. 标题相似度函数校验
    assert is_highly_similar_goods(
        "维达超韧抽纸3层130抽24包整箱装",
        "【百亿补贴】维达超韧抽纸3层130抽24包整箱装家用实惠装",
    )
    # 规格数字不同（24包 vs 18包）绝不误判为同款
    assert not is_highly_similar_goods(
        "维达超韧抽纸3层130抽24包整箱装",
        "维达超韧抽纸3层130抽18包整箱装",
    )
    # 同品牌不同款式（隔音耳塞 vs 睡眠耳塞）不误判为同款
    assert not is_highly_similar_goods("安耳悠隔音耳塞", "安耳悠睡眠耳塞")

    # 2. 端到端流程验证
    repo = GroupHelpRepository(db_instance=_MemoryDB())
    base_ts = _time.time()
    poster_dir = tmp_path / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)

    # 先已有一个在线可拼的团 A
    gid_a = "3495229659187108001"
    img_a = poster_dir / f"{gid_a}.jpg"
    img_a.write_bytes(_make_poster_image_bytes("https://file-link.pinduoduo.com/ga"))
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": gid_a,
            "short_url": "https://file-link.pinduoduo.com/ga",
            "redirect_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid_a}",
            "poster_path": str(img_a),
            "created_at": base_ts,
        }
    )
    repo.mark_parse_succeeded(
        gid_a,
        {
            "goods_name": "维达超韧抽纸3层130抽24包整箱装",
            "keywords": ["维达", "抽纸"],
            "group_price": 26.9,
            "original_price": 39.9,
            "total_people": 3,
            "remain_people": 1,
            "remaining_seconds": 36000,
        },
        now=base_ts + 2,
    )

    # 新用户上传了同款同价的团 B
    gid_b = "3495229659187108002"
    img_b = poster_dir / f"{gid_b}.jpg"
    img_b.write_bytes(_make_poster_image_bytes("https://file-link.pinduoduo.com/gb"))
    repo.create_or_refresh_initial_group(
        {
            "group_order_id": gid_b,
            "short_url": "https://file-link.pinduoduo.com/gb",
            "redirect_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid_b}",
            "poster_path": str(img_b),
            "created_at": base_ts + 10,
        }
    )

    def fake_ai_same_product(prompt, file_paths=None, **kwargs):
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "is_compliant": True,
                    "reject_reason": "",
                    "goods_name": "维达超韧抽纸3层130抽24包整箱装家用实惠装",
                    "keywords": ["维达", "抽纸", "24包"],
                    "group_price": 26.9,
                    "original_price": 39.9,
                    "total_people": 3,
                    "remain_people": 2,
                    "remaining_time": "18:00:00",
                    "remaining_seconds": 64800,
                }
            ),
        }

    svc = GroupHelpService(repo=repo, poster_dir=poster_dir, ai_generator=fake_ai_same_product)
    stats = svc.process_pending_batch(batch_size=2)
    assert stats["parsed"] == 1

    # 团 B 应处于 pending_confirm 状态，且绑定了团 A 的详情
    detail_b = svc.get_group_detail(gid_b)
    assert detail_b["parse_status"] == PARSE_STATUS_PARSED
    assert detail_b["group_status"] == GROUP_STATUS_PENDING_CONFIRM
    assert detail_b["matched_group_order_id"] == gid_a
    assert detail_b["matched_group"] is not None
    assert detail_b["matched_group"]["group_order_id"] == gid_a
    assert detail_b["matched_group"]["group_price"] == 26.9

    # 此时大厅列表中只有团 A，团 B 尚未上架
    hall_before = repo.search_groups(now=base_ts + 20)
    assert [it["group_order_id"] for it in hall_before["items"]] == [gid_a]

    # 用户选择“继续上传我的团” (confirm_publish)
    published_b = svc.submit_feedback(gid_b, action="confirm_publish")
    assert published_b["group_status"] == GROUP_STATUS_ACTIVE
    assert published_b["matched_group_order_id"] == ""
    assert published_b["matched_group"] is None

    # 确认发布后，团 A 和团 B 均在大厅展示
    hall_after = repo.search_groups(now=base_ts + 25)
    assert len(hall_after["items"]) == 2


def test_reupload_qr_only_for_existing_group_order_id_must_not_bypass_audit(tmp_path):
    """
    验证：当某个 group_order_id 已在库中处于 parsed 状态时，若用户截取该团的纯二维码局部图再次上传，
    系统绝不能直接复用旧的 parsed 状态放行，必须重置为 pending 重新送审并拦截为 failed。
    """
    from models.group_help import PARSE_STATUS_FAILED

    repo = GroupHelpRepository(db_instance=_MemoryDB())
    poster_dir = tmp_path / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)
    gid = "3495190924788381111"
    short_url = "https://file-link.pinduoduo.com/eYg3ajkbMq"

    def fake_http_get(url, headers=None, allow_redirects=False, timeout=8):
        return SimpleNamespace(
            status_code=307,
            headers={"Location": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid}"},
            text="",
        )

    # 第一次上传完整海报并审核通过
    svc_pass = GroupHelpService(
        repo=repo,
        poster_dir=poster_dir,
        http_get=fake_http_get,
        ai_generator=lambda prompt, file_paths=None, **kw: {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "is_compliant": True,
                    "reject_reason": None,
                    "goods_name": "【富之鲜】8斤新干蒜金乡紫白皮大蒜头",
                    "keywords": ["大蒜"],
                    "group_price": 24.99,
                    "original_price": 41.59,
                    "total_people": 3,
                    "remain_people": 2,
                    "remaining_time": "23:00:00",
                    "remaining_seconds": 82800,
                }
            ),
        },
    )
    up1 = svc_pass.submit_poster_image(_make_poster_image_bytes(short_url))
    assert up1["data"]["parse_status"] == PARSE_STATUS_PENDING
    svc_pass.process_pending_batch(batch_size=2)
    assert repo.get_by_group_order_id(gid)["parse_status"] == PARSE_STATUS_PARSED

    # 第二次：用户截取该同一个团单的纯二维码局部图上传
    svc_qr_only = GroupHelpService(
        repo=repo,
        poster_dir=poster_dir,
        http_get=fake_http_get,
        ai_generator=lambda prompt, file_paths=None, **kw: {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": False,
                    "is_compliant": True,
                    "reject_reason": "仅截取了局部二维码，缺失商品名称与价格",
                    "goods_name": "",
                    "keywords": [],
                    "group_price": None,
                    "original_price": None,
                    "total_people": 0,
                    "remain_people": 0,
                    "remaining_time": None,
                    "remaining_seconds": None,
                }
            ),
        },
    )
    up2 = svc_qr_only.submit_poster_image(_make_poster_image_bytes(short_url))
    # 上传接口必须返回 pending，严禁直接返回 parsed
    assert up2["data"]["parse_status"] == PARSE_STATUS_PENDING
    svc_qr_only.process_pending_batch(batch_size=2)
    # 审核后必须变为 failed 并从大厅下线
    doc_after = repo.get_by_group_order_id(gid)
    assert doc_after["parse_status"] == PARSE_STATUS_FAILED
    assert len(repo.search_groups()["items"]) == 0
    assert svc_qr_only.get_poster_file_path(gid) is None


def test_anti_sabotage_owner_relist_penalizes_saboteur_but_keeps_non_takedown_actions(tmp_path, monkeypatch):
    """验证防恶意下架全链路：路人重传不泄 owner_token、团长恢复惩罚破坏者(2次封禁下架)、非下架动作保留及10分钟3次防扫荡。"""
    from dataclasses import replace
    import config
    import main as main_module
    from api import user as user_api
    from services.user_service import UserService

    mem_db = _MemoryDB()
    repo = GroupHelpRepository(db_instance=mem_db)
    poster_dir = tmp_path / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)

    url_map = {}

    def fake_http_get(url, headers=None, allow_redirects=False, timeout=8):
        return SimpleNamespace(status_code=307, headers={"Location": url_map[url]}, text="")

    ai_remain_map = {}

    def fake_ai_gen(prompt, file_paths=None, **kw):
        fp = str((file_paths or [""])[0])
        matched_gid = "000"
        for k in ai_remain_map:
            if k in fp:
                matched_gid = k
                break
        rem = ai_remain_map.get(matched_gid, 3)
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_group_poster": True,
                    "is_compliant": True,
                    "reject_reason": "",
                    "goods_name": f"测试防破坏商品_{matched_gid[-4:]}",
                    "keywords": ["测试"],
                    "group_price": float(int(matched_gid[-2:]) + 10.9),
                    "original_price": 59.9,
                    "total_people": rem + 1,
                    "remain_people": rem,
                    "remaining_time": "22:00:00",
                    "remaining_seconds": 79200,
                }
            ),
        }

    service = GroupHelpService(
        repo=repo,
        poster_dir=poster_dir,
        http_get=fake_http_get,
        ai_generator=fake_ai_gen,
    )
    monkeypatch.setattr(group_help_api, "group_help_service", service)
    monkeypatch.setattr(user_api, "user_service", UserService(user_repo=service.user_repo))
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    def publish_group(client, gid: str, remain: int, share_id: str = "share-v1", headers=None):
        short_url = f"https://file-link.pinduoduo.com/s{gid[-6:]}"
        url_map[short_url] = (
            f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid}&refer_share_id={share_id}"
        )
        ai_remain_map[gid] = remain
        b64 = base64.b64encode(_make_poster_image_bytes(short_url)).decode("ascii")
        resp = client.post(
            "/api/group_help/upload",
            headers=headers or {},
            json={"image_base64": b64},
        )
        assert resp.status_code == 200
        service.process_pending_batch(batch_size=5)
        return resp.json()

    app = create_app()
    with TestClient(app) as client:
        reg_sab = client.post(
            "/api/user/register_or_login",
            json={"phone": "13800138001", "password": "password123", "mode": "register"},
        ).json()["user"]
        sab_headers = {"Authorization": f"Bearer {reg_sab['auth_token']}"}

        reg_owner = client.post(
            "/api/user/register_or_login",
            json={"phone": "13800138002", "password": "password123", "mode": "register"},
        ).json()["user"]
        owner_jwt_headers = {"Authorization": f"Bearer {reg_owner['auth_token']}"}

        # 1. 匿名团长 A 发布差 3 人的团 G1；路人重传绝不泄露 owner_token；匿名 directly takedown -> 401
        gid1 = "3495229659187200011"
        up1 = publish_group(client, gid1, remain=3)
        owner_tok_1 = up1["owner_token"]
        assert owner_tok_1
        assert publish_group(client, gid1, remain=3).get("owner_token", "") == ""
        for act in ("mark_expired", "mark_full"):
            r401 = client.post(f"/api/group_help/feedback/{gid1}", json={"action": act})
            assert r401.status_code == 401 and r401.json()["error_code"] == "auth_required"

        # 2. 登录态团长自己下架再恢复自己的团 G_SELF -> 绝不惩罚团长自己
        gid_self = "3495229659187200012"
        publish_group(client, gid_self, remain=2, headers=owner_jwt_headers)
        assert client.post(
            f"/api/group_help/feedback/{gid_self}",
            headers=owner_jwt_headers,
            json={"action": "mark_full"},
        ).status_code == 200
        r_self_up = client.post(
            f"/api/group_help/feedback/{gid_self}",
            headers=owner_jwt_headers,
            json={"action": "set_remain", "remain_people": 2},
        )
        assert r_self_up.status_code == 200 and r_self_up.json()["penalized_saboteur"] is False
        assert service.get_viewer_guard(reg_owner["uid"])["strikes"] == 0

        # 3. 破坏者 saboteur 第 1 次恶意标失效；非团长防栽赃 403；团长 A 恢复后记 1 次违规（仍可操作）且幂等
        assert client.post(
            f"/api/group_help/feedback/{gid1}",
            headers=sab_headers,
            json={"action": "mark_expired"},
        ).json()["data"]["group_status"] == GROUP_STATUS_EXPIRED
        frame_attempt = client.post(
            f"/api/group_help/feedback/{gid1}",
            headers=owner_jwt_headers,
            json={"action": "set_remain", "remain_people": 2},
        )
        assert frame_attempt.status_code == 403 and frame_attempt.json()["error_code"] == "owner_only"
        assert service.get_viewer_guard(reg_sab["uid"])["strikes"] == 0

        owner_relist_1 = client.post(
            f"/api/group_help/feedback/{gid1}",
            json={"action": "set_remain", "remain_people": 2, "owner_token": owner_tok_1},
        )
        assert owner_relist_1.status_code == 200 and owner_relist_1.json()["penalized_saboteur"] is True
        guard_after_1 = service.get_viewer_guard(reg_sab["uid"])
        assert guard_after_1["strikes"] == 1 and guard_after_1["can_takedown"] is True

        client.post(f"/api/group_help/feedback/{gid1}", json={"action": "mark_full", "owner_token": owner_tok_1})
        relist_again = client.post(
            f"/api/group_help/feedback/{gid1}",
            json={"action": "set_remain", "remain_people": 2, "owner_token": owner_tok_1},
        )
        assert relist_again.json()["penalized_saboteur"] is False
        assert service.get_viewer_guard(reg_sab["uid"])["strikes"] == 1

        # 4. 第 2 次恶意下架团 G2 -> 累计满 2 次永久禁用下架权限
        gid2 = "3495229659187200024"
        up2 = publish_group(client, gid2, remain=2)
        assert client.post(
            f"/api/group_help/feedback/{gid2}", headers=sab_headers, json={"action": "mark_expired"}
        ).status_code == 200
        owner_relist_2 = client.post(
            f"/api/group_help/feedback/{gid2}",
            json={"action": "set_remain", "remain_people": 2, "owner_token": up2["owner_token"]},
        )
        assert owner_relist_2.status_code == 200 and owner_relist_2.json()["penalized_saboteur"] is True
        guard_after_2 = service.get_viewer_guard(reg_sab["uid"])
        assert guard_after_2["strikes"] == 2 and guard_after_2["can_takedown"] is False

        # 5. 外科手术式权限：禁用后不可下架他人(403)，但差 >=2 人可参团(-1)且可管理自己团
        assert client.post(
            f"/api/group_help/feedback/{gid1}", headers=sab_headers, json={"action": "mark_full"}
        ).json()["error_code"] == "takedown_banned"
        allowed_join = client.post(
            f"/api/group_help/feedback/{gid1}", headers=sab_headers, json={"action": "joined_one"}
        )
        assert allowed_join.status_code == 200 and allowed_join.json()["data"]["remain_people"] == 1
        assert client.post(
            f"/api/group_help/feedback/{gid1}", headers=sab_headers, json={"action": "joined_one"}
        ).status_code == 403

        gid_sab_own = "3495229659187200013"
        up_sab_own = publish_group(client, gid_sab_own, remain=1, headers=sab_headers)
        assert client.post(
            f"/api/group_help/feedback/{gid_sab_own}",
            headers=sab_headers,
            json={"action": "mark_full", "owner_token": up_sab_own["owner_token"]},
        ).status_code == 200

        # 6. 短时防扫荡频控：10 分钟内第 4 次下架他人团返回 429
        reg_burst = client.post(
            "/api/user/register_or_login",
            json={"phone": "13800138009", "password": "password123", "mode": "register"},
        ).json()["user"]
        burst_headers = {"Authorization": f"Bearer {reg_burst['auth_token']}"}
        for idx in range(3):
            gid_k = f"349522965918720003{idx}"
            publish_group(client, gid_k, remain=1)
            assert client.post(
                f"/api/group_help/feedback/{gid_k}", headers=burst_headers, json={"action": "mark_full"}
            ).status_code == 200

        gid_4th = "3495229659187200039"
        publish_group(client, gid_4th, remain=1)
        rate_limited = client.post(
            f"/api/group_help/feedback/{gid_4th}", headers=burst_headers, json={"action": "mark_full"}
        )
        assert rate_limited.status_code == 429
        assert rate_limited.json()["error_code"] == "takedown_rate_limited"


def test_parse_remaining_seconds_compound_chinese_duration():
    from models.group_help import parse_remaining_seconds

    assert parse_remaining_seconds("5小时30分") == 5 * 3600 + 30 * 60
    assert parse_remaining_seconds("剩余2小时15分10秒") == 2 * 3600 + 15 * 60 + 10
    assert parse_remaining_seconds("1h30m") == 5400
    assert parse_remaining_seconds("45分钟") == 2700


def test_group_help_upload_rejects_oversized_content_length_with_413():
    import main

    resp = TestClient(main.app).post(
        "/api/group_help/upload",
        content=b"tiny",
        headers={"Content-Length": str(16 * 1024 * 1024)},
    )
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "payload_too_large"


def test_text_token_and_link_group_submission_and_validation(tmp_path, monkeypatch):
    """验证无图兼容全链路：⇥...⇤ 口令直发、官方短链解析直发、详情与海报404兜底、外站链接/违规引流拦截与10分钟限频。"""
    import config
    import main as main_module
    from dataclasses import replace

    repo = GroupHelpRepository(db_instance=_MemoryDB())
    short_link = "https://file-link.pinduoduo.com/fakeLink1"
    numeric_gid = "3501999000111222333"

    class _FakeResp:
        def __init__(self, status_code=307, location=""):
            self.status_code = status_code
            self.headers = {"Location": location} if location else {}

    def _fake_http_get(url, allow_redirects=False, timeout=10, headers=None):
        if url == short_link:
            return _FakeResp(
                307,
                f"https://mobile.yangkeduo.com/group7.html?group_order_id={numeric_gid}&refer_share_id=link-test-1",
            )
        return _FakeResp(404)

    service = GroupHelpService(repo=repo, poster_dir=tmp_path, http_get=_fake_http_get)
    monkeypatch.setattr(group_help_api, "group_help_service", service)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    app = create_app()
    with TestClient(app) as client:
        # 1. 提交无图拼团口令
        tok_resp = client.post(
            "/api/group_help/submit_text",
            json={
                "share_text": "快来帮我拼团呀 ⇥AbCdEf9876⇤",
                "goods_name": "维达抽纸24包整箱装",
                "group_price": 29.9,
                "original_price": 39.9,
                "total_people": 3,
                "remain_people": 1,
                "publisher_nick": "测试团长",
            },
        )
        assert tok_resp.status_code == 200, tok_resp.text
        tok_json = tok_resp.json()
        assert tok_json["status"] == "success"
        assert tok_json["owner_token"]
        tok_data = tok_json["data"]
        assert tok_data["group_order_id"] == "tok_AbCdEf9876"
        assert tok_data["entry_type"] == "token"
        assert tok_data["has_poster"] is False
        assert tok_data["poster_url"] == ""
        assert tok_data["h5_join_url"] == ""
        assert tok_data["app_wake_url"] == ""
        assert tok_data["share_code"] == "AbCdEf9876"

        # 2. 列表、详情、点击统计正常工作，请求无图记录的 /poster 返回 404
        list_resp = client.get("/api/group_help/list", params={"keyword": "维达抽纸"})
        assert list_resp.status_code == 200
        assert any(it["group_order_id"] == "tok_AbCdEf9876" for it in list_resp.json()["data"]["items"])

        detail_resp = client.get("/api/group_help/detail/tok_AbCdEf9876")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["data"]["share_text"] == "快来帮我拼团呀 ⇥AbCdEf9876⇤"

        click_resp = client.post("/api/group_help/click/tok_AbCdEf9876")
        assert click_resp.status_code == 200
        assert click_resp.json()["data"]["click_count"] == 1

        poster_resp = client.get("/api/group_help/poster/tok_AbCdEf9876")
        assert poster_resp.status_code == 404

        # 3. 拦截站外链接与违规引流广告，且禁止他人篡改已有口令
        evil_resp = client.post(
            "/api/group_help/submit_text",
            json={
                "share_text": "点击 https://evil.example.com/phish ⇥ZzYyXx1234⇤",
                "goods_name": "测试商品",
                "group_price": 19.9,
            },
        )
        assert evil_resp.status_code == 400

        spam_resp = client.post(
            "/api/group_help/submit_text",
            json={
                "share_text": "加微信abc12345领返现 ⇥ZzYyXx1234⇤",
                "goods_name": "测试商品",
                "group_price": 19.9,
            },
        )
        assert spam_resp.status_code == 400

        hijack_resp = client.post(
            "/api/group_help/submit_text",
            json={
                "share_text": "我想改掉别人团名 ⇥AbCdEf9876⇤",
                "goods_name": "恶意篡改标题",
                "group_price": 1.0,
            },
        )
        assert hijack_resp.status_code == 400

        # 4. 提交拼多多官方短链（无海报图但能解析数字团单）
        link_resp = client.post(
            "/api/group_help/submit_text",
            json={
                "share_text": short_link,
                "goods_name": "蓝月亮洗衣液3kg",
                "group_price": 32.5,
                "remain_people": 1,
            },
        )
        assert link_resp.status_code == 200, link_resp.text
        link_data = link_resp.json()["data"]
        assert link_data["group_order_id"] == numeric_gid
        assert link_data["entry_type"] == "link"
        assert link_data["has_poster"] is False
        assert link_data["poster_url"] == ""
        assert f"group_order_id={numeric_gid}" in link_data["h5_join_url"]

        # 5. 验证同 IP/用户 10 分钟最多无图发布 5 条，第 6 条触发 429 限频
        for idx in range(3):
            r = client.post(
                "/api/group_help/submit_text",
                json={
                    "share_text": f"正常拼团 ⇥RateTok00{idx}⇤",
                    "goods_name": f"常规日用品商品{idx}",
                    "group_price": 10.0 + idx,
                },
            )
            assert r.status_code == 200
        limited_resp = client.post(
            "/api/group_help/submit_text",
            json={
                "share_text": "超出限频 ⇥RateTok999⇤",
                "goods_name": "常规日用品商品超限",
                "group_price": 15.0,
            },
        )
        assert limited_resp.status_code == 429
        assert limited_resp.json()["error_code"] == "submit_rate_limited"


def _load_mobile_script(module_name: str):
    import importlib.util
    from pathlib import Path

    script_path = Path(__file__).resolve().parent.parent / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, str(script_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_group_help_importer_sanitizes_syncs_and_never_resurrects(tmp_path):
    """验证外部采集脚本：按唯一真源清洗外部数据、设置较短外部过期时间、同期同步上游满员且绝不复活本地已满团。"""
    mod = _load_mobile_script("group_help_importer")
    ACDOG_API_URL = mod.ACDOG_API_URL
    GroupHelpImporter = mod.GroupHelpImporter
    MIAOZHULI_API_URL = mod.MIAOZHULI_API_URL

    repo = GroupHelpRepository(db_instance=_MemoryDB())
    base_now = 1795000000.0
    miao_state = {"full": 0, "status": "active"}

    class _FakeJsonResp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    def _fake_get(url, params=None, headers=None, timeout=12):
        page = (params or {}).get("page", 1)
        if page != 1:
            return _FakeJsonResp({"items": []})
        if url == ACDOG_API_URL:
            return _FakeJsonResp(
                {
                    "items": [
                        {
                            "id": "1",
                            "platform": "pdd",
                            "rawText": "复制这段口令打开拼多多 ⇥AcdogGood1⇤",
                            "coreCode": "AcdogGood1",
                            "price": 18.8,
                            "titleDisplay": "清风抽纸原木纯品20包",
                            "groupSize": 3,
                            "spotsLeft": 1,
                            "status": "active",
                            "publisherDisplayName": "闪助拼友",
                            "createdAt": int((base_now - 3600) * 1000),
                            "expiresAt": int((base_now + 20 * 3600) * 1000),
                            "markedFullCount": 0,
                            "markedInvalidCount": 0,
                        },
                        {
                            "id": "2",
                            "platform": "pdd",
                            "rawText": "加微信wx888领红包 ⇥SpamCode99⇤",
                            "coreCode": "SpamCode99",
                            "price": 9.9,
                            "titleDisplay": "违规广告商品",
                            "groupSize": 2,
                            "spotsLeft": 1,
                            "status": "active",
                            "createdAt": int(base_now * 1000),
                            "expiresAt": int((base_now + 12 * 3600) * 1000),
                        },
                    ]
                }
            )
        if url == MIAOZHULI_API_URL:
            return _FakeJsonResp(
                {
                    "items": [
                        {
                            "id": 101,
                            "nick": "秒助拼友",
                            "title": "舒洁湿厕纸80抽3包装",
                            "url": "",
                            "share_text": "拼单立减 ⇥MiaoGood01⇤",
                            "price": 24.5,
                            "team_size": 2,
                            "status": miao_state["status"],
                            "marks": {"full": miao_state["full"], "expire": 0, "same": 0},
                            "created_at": "2026-11-18 12:00:00",
                            "time_left": 18000,
                        }
                    ]
                }
            )
        raise AssertionError(f"Unexpected url: {url}")

    importer = GroupHelpImporter(
        repo=repo,
        http_get=_fake_get,
        external_ttl_seconds=6 * 3600,
        pages=2,
    )
    res1 = importer.run_once(now=base_now)
    assert res1["sources"]["acdog"]["upserted"] == 1
    assert res1["sources"]["acdog"]["skipped_invalid"] == 1
    assert res1["sources"]["miaozhuli"]["upserted"] == 1

    ac_doc = repo.get_by_group_order_id("tok_AcdogGood1")
    assert ac_doc is not None
    assert ac_doc["source"] == "acdog"
    assert ac_doc["expires_at"] <= base_now + 6 * 3600 + 1
    assert repo.get_by_group_order_id("tok_SpamCode99") is None

    # 本地用户将 acdog 团标记满员后，即便上游仍报 active 也绝不能复活
    repo.apply_user_feedback(
        group_order_id="tok_AcdogGood1",
        action="mark_full",
        now=base_now + 30,
        operator_uid="u_verifier",
    )
    # 同时秒助力的团在上游被人标满
    miao_state["full"] = 1
    miao_state["status"] = "full"

    res2 = importer.run_once(now=base_now + 60)
    assert res2["sources"]["miaozhuli"]["synced_inactive"] == 1
    assert repo.get_by_group_order_id("tok_AcdogGood1")["group_status"] == GROUP_STATUS_FULL
    assert repo.get_by_group_order_id("tok_MiaoGood01")["group_status"] == GROUP_STATUS_FULL


def test_group_help_exporter_queues_completes_and_withdraws(tmp_path):
    """验证海报外发脚本：仅导出真实用户海报、生成队列与 task.json、done.flag 完工归档及站内满员自动撤回。"""
    from models.group_help import ENTRY_TYPE_POSTER, SOURCE_USER_UPLOAD
    exporter_mod = _load_mobile_script("group_help_exporter")
    GroupHelpExporter = exporter_mod.GroupHelpExporter

    repo = GroupHelpRepository(db_instance=_MemoryDB())
    poster_dir = tmp_path / "posters"
    bridge_dir = tmp_path / "bridge"
    poster_dir.mkdir(parents=True, exist_ok=True)
    now = 1795000000.0

    def _seed_poster_group(gid: str, title: str):
        p_path = poster_dir / f"{gid}.jpg"
        p_path.write_bytes(_make_poster_image_bytes(f"https://file-link.pinduoduo.com/{gid[-6:]}"))
        repo.upsert_parsed_group(
            {
                "group_order_id": gid,
                "entry_type": ENTRY_TYPE_POSTER,
                "source": SOURCE_USER_UPLOAD,
                "goods_name": title,
                "group_price": 19.9,
                "total_people": 3,
                "remain_people": 1,
                "poster_path": str(p_path),
                "h5_join_url": f"https://mobile.yangkeduo.com/group7.html?group_order_id={gid}",
                "created_at": now,
                "expires_at": now + 3600,
            },
            now=now,
            check_similar=False,
        )

    gid_1 = "3501888000111222331"
    gid_2 = "3501888000111222332"
    _seed_poster_group(gid_1, "用户真实上传海报商品A")

    # 外部采集无图口令绝不进入外发队列
    repo.upsert_parsed_group(
        {
            "group_order_id": "tok_ExtOnly001",
            "entry_type": "token",
            "source": "acdog",
            "share_text": "外部 ⇥ExtOnly001⇤",
            "share_code": "ExtOnly001",
            "goods_name": "外部采集商品B",
            "group_price": 9.9,
            "remain_people": 1,
            "created_at": now,
            "expires_at": now + 3600,
        },
        now=now,
        check_similar=False,
    )

    exporter = GroupHelpExporter(repo=repo, bridge_dir=bridge_dir, poster_dir=poster_dir)
    s1 = exporter.run_once(now=now + 5)
    assert s1["pending_total"] == 1
    assert s1["exported_new"] == 1
    assert (bridge_dir / "outbound_pending" / gid_1 / "poster.jpg").is_file()
    assert (bridge_dir / "outbound_pending" / gid_1 / "task.json").is_file()
    assert not (bridge_dir / "outbound_pending" / "tok_ExtOnly001").exists()

    # 外部脚本落盘 done.flag 后自动归档完成并清理临时目录，不再重复导出
    (bridge_dir / "outbound_pending" / gid_1 / "done.flag").write_text("ok", encoding="utf-8")
    _seed_poster_group(gid_2, "用户真实上传海报商品C")
    s2 = exporter.run_once(now=now + 10)
    assert s2["completed_via_flag"] == 1
    assert s2["exported_new"] == 1
    assert s2["pending_total"] == 1
    assert not (bridge_dir / "outbound_pending" / gid_1).exists()
    assert (bridge_dir / "outbound_pending" / gid_2 / "poster.jpg").is_file()

    # 站内该团被标记满员后，外发队列自动撤回并清理任务文件
    repo.apply_user_feedback(group_order_id=gid_2, action="mark_full", now=now + 15, operator_uid="u_owner")
    s3 = exporter.run_once(now=now + 20)
    assert s3["withdrawn"] == 1
    assert s3["pending_total"] == 0
    assert not (bridge_dir / "outbound_pending" / gid_2).exists()
    assert exporter.get_summary(now=now + 20)["counts"]["done"] == 1
    assert exporter.get_summary(now=now + 20)["counts"]["withdrawn"] == 1
