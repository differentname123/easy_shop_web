# -*- coding: utf-8 -*-
"""详情页锚定用户爆料核心流程、图片压缩、全图文对比与单阶段流水线集成测试。"""
from __future__ import annotations

import base64
import copy
import datetime
import io
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
import main as main_module
from adapters import PlatformUnavailableError
from auth import create_access_token
from main import create_app
from models.deal_constants import (
    CLEAN_STAGE_NEED_USER_AUDIT,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_USER_AUDIT_REJECTED,
    MAX_CLEAN_RETRIES,
    OK_EXISTING_DEAL_BETTER,
    PROMPT_USER_AUDIT_PATH,
    USER_SUBMIT_SITE,
    is_reliable_source_site,
)
from models.product import get_source_site_name
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.product_repo import ProductRepository, upsert_cleaned_products_to_formal_table
from repositories.user_repo import UserRepository
from services.deal_pool_cleaner import DealPoolCleaner
from services.deal_submit_service import (
    USER_SUBMIT_MAX_IMAGE_DIM,
    USER_SUBMIT_MAX_PER_WINDOW,
    DealSubmitError,
    DealSubmitService,
    cleanup_expired_deal_submit_images,
    normalize_and_compress_deal_image,
)
from services.deal_submit_worker import DealSubmitWorker


class _MemCursor:
    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = list(docs)
        self._limit: Optional[int] = None

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.docs.sort(
                key=lambda d, k=key: (0, 0) if d.get(k) is None else (1, d.get(k)),
                reverse=direction < 0,
            )
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    def __iter__(self):
        return iter(self.docs[: self._limit] if self._limit is not None else self.docs)


class _MemCollection:
    """独立内存 Mongo 集合桩。"""

    def __init__(self, initial_docs: Optional[List[Dict[str, Any]]] = None, database: Any = None):
        self.docs: List[Dict[str, Any]] = copy.deepcopy(initial_docs or [])
        self.database = database

    def create_index(self, *args, **kwargs):
        pass

    @staticmethod
    def _get_nested(doc: Dict[str, Any], path: str):
        cur: Any = doc
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False, None
            cur = cur[part]
        return True, cur

    @classmethod
    def _set_nested(cls, doc: Dict[str, Any], path: str, value: Any) -> None:
        parts = path.split(".")
        cur = doc
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = copy.deepcopy(value)

    @classmethod
    def _unset_nested(cls, doc: Dict[str, Any], path: str) -> None:
        parts = path.split(".")
        cur = doc
        for part in parts[:-1]:
            if not isinstance(cur, dict) or part not in cur:
                return
            cur = cur[part]
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)

    @classmethod
    def _matches(cls, doc: Dict[str, Any], flt: Optional[Dict[str, Any]]) -> bool:
        if not flt:
            return True
        for k, v in flt.items():
            if k == "$and":
                if not all(cls._matches(doc, cond) for cond in v):
                    return False
            elif k == "$or":
                if not any(cls._matches(doc, cond) for cond in v):
                    return False
            elif k == "$nor":
                if any(cls._matches(doc, cond) for cond in v):
                    return False
            else:
                exists, val = cls._get_nested(doc, k)
                if isinstance(v, dict):
                    for op, op_val in v.items():
                        if op == "$exists":
                            if (exists and val is not None) != bool(op_val):
                                return False
                        elif op == "$ne":
                            if val == op_val:
                                return False
                        elif op == "$in":
                            if val not in op_val:
                                return False
                        elif op == "$nin":
                            if val in op_val:
                                return False
                        elif op == "$lt":
                            if val is None or not (val < op_val):
                                return False
                        elif op == "$gt":
                            if val is None or not (val > op_val):
                                return False
                        elif op == "$gte":
                            if val is None or not (val >= op_val):
                                return False
                else:
                    if isinstance(val, list) and not isinstance(v, list):
                        if v not in val:
                            return False
                    elif val != v:
                        return False
        return True

    @staticmethod
    def _project(doc: Dict[str, Any], projection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        out = copy.deepcopy(doc)
        if not projection:
            return out
        if projection.get("_id") == 0:
            out.pop("_id", None)
        include_keys = [k for k, v in projection.items() if k != "_id" and v]
        if not include_keys:
            return out
        projected: Dict[str, Any] = {}
        for path in include_keys:
            ok, val = _MemCollection._get_nested(out, path)
            if ok:
                _MemCollection._set_nested(projected, path, val)
        if "_id" in out and projection.get("_id", 1) != 0:
            projected["_id"] = out["_id"]
        return projected

    def find_one(self, flt: Optional[Dict[str, Any]] = None, projection: Optional[Dict[str, Any]] = None):
        for d in self.docs:
            if self._matches(d, flt):
                return self._project(d, projection)
        return None

    def find(self, flt: Optional[Dict[str, Any]] = None, projection: Optional[Dict[str, Any]] = None):
        matched = [self._project(d, projection) for d in self.docs if self._matches(d, flt)]
        return _MemCursor(matched)

    def count_documents(self, flt: Optional[Dict[str, Any]] = None) -> int:
        return sum(1 for d in self.docs if self._matches(d, flt))

    def _apply_update(self, target: Dict[str, Any], update: Dict[str, Any], *, is_insert: bool) -> None:
        if "$set" in update:
            for k, v in update["$set"].items():
                self._set_nested(target, k, v)
        if is_insert and "$setOnInsert" in update:
            for k, v in update["$setOnInsert"].items():
                self._set_nested(target, k, v)
        if "$unset" in update:
            for k in update["$unset"]:
                self._unset_nested(target, k)
        if "$inc" in update:
            for k, inc_val in update["$inc"].items():
                _, prev = self._get_nested(target, k)
                self._set_nested(target, k, (prev or 0) + inc_val)
        if "$addToSet" in update:
            for k, spec in update["$addToSet"].items():
                _, prev = self._get_nested(target, k)
                arr = list(prev) if isinstance(prev, list) else []
                items = spec["$each"] if isinstance(spec, dict) and "$each" in spec else [spec]
                for item in items:
                    if item not in arr:
                        arr.append(item)
                self._set_nested(target, k, arr)

    def update_many(self, flt: Dict[str, Any], update: Dict[str, Any]):
        matched = 0
        modified = 0
        for d in self.docs:
            if self._matches(d, flt):
                matched += 1
                self._apply_update(d, update, is_insert=False)
                modified += 1
        return SimpleNamespace(matched_count=matched, modified_count=modified)

    def bulk_write(self, ops, ordered=False):
        upserted = 0
        modified = 0
        for op in ops:
            flt = op._filter
            upd = op._doc
            upsert = bool(getattr(op, "_upsert", False))
            found = None
            for d in self.docs:
                if self._matches(d, flt):
                    found = d
                    break
            if found is not None:
                self._apply_update(found, upd, is_insert=False)
                modified += 1
            elif upsert:
                new_doc = copy.deepcopy(flt)
                new_doc["_id"] = f"mem_{len(self.docs) + 1}"
                self._apply_update(new_doc, upd, is_insert=True)
                self.docs.append(new_doc)
                upserted += 1
        return SimpleNamespace(upserted_count=upserted, modified_count=modified, matched_count=modified)


class _MemDB:
    def __init__(self, initial_users: Optional[List[Dict[str, Any]]] = None, initial_products: Optional[List[Dict[str, Any]]] = None):
        self._cols = {
            "deal_raw_tips": _MemCollection(database=self),
            "products": _MemCollection(initial_docs=initial_products, database=self),
            "users": _MemCollection(initial_docs=initial_users, database=self),
        }

    def get_collection(self, name: str):
        if name not in self._cols:
            self._cols[name] = _MemCollection(database=self)
        return self._cols[name]


def _sample_detail_product(plat: str = "jd", pid: str = "1001001", price: float = 59.9) -> Dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "platform": plat,
            "platform_name": "京东" if plat == "jd" else ("淘宝" if plat == "taobao" else plat.upper()),
            "product_id": pid,
            "goods_id": pid,
            "goods_sign": f"sign_{pid}",
            "name": "维达抽纸超韧3层130抽24包家用整箱装",
            "price": price,
            "original_price": 79.9,
            "coupon_amount": 10.0,
            "mall_name": "维达官方旗舰店",
            "brand": "维达",
            "category": "家居日用",
            "image_url": "https://img.example.test/vinda_official.jpg",
            "carousel_images": ["https://img.example.test/vinda_official.jpg"],
            "sales_tip": "10万+",
            "product_url": f"https://item.example.test/{plat}/{pid}.html",
        },
    }


def _make_harness(initial_users=None, initial_products=None, detail_res=None, image_dir=None):
    users = initial_users or [{"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"}]
    db = _MemDB(initial_users=users, initial_products=initial_products)
    raw_repo = DealRawPoolRepository(db_instance=db)
    user_repo = UserRepository(db_instance=db)
    product_service = MagicMock()
    product_service.get_goods_detail.return_value = detail_res if detail_res is not None else _sample_detail_product()
    svc = DealSubmitService(product_service=product_service, raw_repo=raw_repo, user_repo=user_repo, image_dir=image_dir)
    return db, raw_repo, user_repo, product_service, svc


def _make_png_bytes(width: int = 320, height: int = 240, color=(220, 60, 40, 255)) -> bytes:
    img = Image.new("RGBA", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_png_b64(width: int = 2000, height: int = 1000, color=(220, 60, 40, 255)) -> str:
    return "data:image/png;base64," + base64.b64encode(_make_png_bytes(width=width, height=height, color=color)).decode("ascii")


def _make_audit_gen(
    *,
    is_valid: bool = True,
    reject_reason: str = "",
    should_replace: bool = True,
    decision_reason: str = "综合图文评审通过",
    capture_calls: Optional[List[Dict[str, Any]]] = None,
):
    def _gen(**kwargs):
        if capture_calls is not None:
            copied = dict(kwargs)
            if kwargs.get("file_paths"):
                copied["file_names"] = [Path(p).name for p in kwargs["file_paths"]]
                copied["parent_dir"] = Path(kwargs["file_paths"][0]).parent
            capture_calls.append(copied)
        return {
            "status": "✅ 成功",
            "content": json.dumps(
                {
                    "is_content_valid": is_valid,
                    "content_reject_reason": reject_reason,
                    "should_replace_existing": should_replace,
                    "decision_reason": decision_reason,
                },
                ensure_ascii=False,
            ),
        }

    return _gen


def test_core_input_validation_extensible_platform_and_rate_limit(tmp_path):
    assert get_source_site_name(USER_SUBMIT_SITE) == "用户爆料"
    assert is_reliable_source_site(USER_SUBMIT_SITE) is True
    assert is_reliable_source_site("tehuicaicai") is True
    assert is_reliable_source_site("guangdiu") is False

    prompt_text = Path(PROMPT_USER_AUDIT_PATH).read_text(encoding="utf-8")
    assert prompt_text.strip()
    for banned_example in ("某品牌", "凡士林", "微信号", "QQ号", "0.01元"):
        assert banned_example not in prompt_text

    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"},
            {"uid": "u_bob", "phone": "13900139000", "nickname": "阿博"},
        ],
        image_dir=tmp_path / "imgs",
    )

    with pytest.raises(DealSubmitError) as e_auth:
        svc.submit_user_deal(uid="", platform="jd", product_id="1001001", deal_price=29.9, tutorial_text="先领券下单")
    assert e_auth.value.status_code == 401

    with pytest.raises(DealSubmitError) as e_pid:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="", deal_price=29.9, tutorial_text="先领券下单")
    assert e_pid.value.error_code == "missing_product_identity"

    with pytest.raises(DealSubmitError) as e_price:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="1001001", deal_price=0, tutorial_text="先领券下单")
    assert e_price.value.error_code == "invalid_deal_price"

    # 未知平台统一由 ProductService / Adapter 校验拒绝
    ps.get_goods_detail.side_effect = PlatformUnavailableError("不支持的平台: unknown_shop")
    with pytest.raises(DealSubmitError) as e_plat:
        svc.submit_user_deal(uid="u_bob", platform="unknown_shop", product_id="1001001", deal_price=29.9, tutorial_text="下单")
    assert e_plat.value.error_code == "unsupported_platform"
    ps.get_goods_detail.side_effect = None

    # 无硬编码三平台限制：未来新增平台只要商品服务核实通过即可自然入池，且玩法含链接/口令不误杀（留给清洗剥离）
    ps.get_goods_detail.return_value = _sample_detail_product(plat="vip", pid="vip_9001")
    res_ext = svc.submit_user_deal(
        uid="u_alice",
        platform="vip",
        product_id="vip_9001",
        deal_price=29.9,
        tutorial_text="领页面补贴 https://promo.example.test/act/123 拍下实付29.9元",
        coupon_input="口令abcdefg",
    )
    assert res_ext["status"] == "success"
    doc_ext = raw_repo.col.find_one({"source_item_id": res_ext["data"]["tip_id"]})
    assert doc_ext["platform"] == "vip"
    assert doc_ext["source_is_reliable"] is True
    assert doc_ext["clean_stage"] == CLEAN_STAGE_NEED_USER_AUDIT
    assert len(doc_ext["converted_links"]) == 1
    assert any(t.get("link_id") == "L2" for t in doc_ext["ai_stage1_result"]["extracted_targets"])

    # 同一用户 10 分钟最多 USER_SUBMIT_MAX_PER_WINDOW 次（刚才已用 1 次，再补发满额度）
    for i in range(USER_SUBMIT_MAX_PER_WINDOW - 1):
        ps.get_goods_detail.return_value = _sample_detail_product(pid=f"p_{i}")
        res = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id=f"p_{i}",
            deal_price=29.9,
            tutorial_text=f"领详情页补贴下单实付29.9 {i}",
        )
        assert res["status"] == "success"

    with pytest.raises(DealSubmitError) as e_lim:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="p_over",
            deal_price=29.9,
            tutorial_text="再多发一条超限",
        )
    assert e_lim.value.status_code == 429
    assert e_lim.value.error_code == "submit_rate_limited"


def test_worker_reuses_convert_and_ai2_with_clean_copy(tmp_path):
    db, raw_repo, _, ps, svc = _make_harness(image_dir=tmp_path / "imgs")

    # 1. 提交含链接文案与官方优惠券，确认全程走低档模型组、convert_single_target 与 AI-2 清洗无链接残留
    res_cpn = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=24.9,
        tutorial_text="先点 https://u.jd.com/temp123 领50减25券再下单",
        coupon_input="https://coupon.m.jd.com/coupons/show.action?key=c1&roleId=999",
    )
    assert res_cpn["status"] == "success"
    assert svc.wake_event.is_set() is True

    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)
    audit_calls: List[Dict[str, Any]] = []
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达超韧抽纸",
            "deal_price": 24.9,
            "price_logic": "领券减25",
            "card_highlight": "24.9元",
            "coupon_links": [{"link_id": "L2", "label": "50减25优惠券"}],
            "tutorial_steps": ["1. 先在上方【领券】处领取50减25券，链接 https://u.jd.com/temp123 自动剥离", "2. 下单实付24.9元"],
            "polished_copywriting": "先点 https://u.jd.com/temp123 领50减25券再下单",
            "quality_score": 84,
            "price_is_reliable": True,
            "price_reliability_reason": "有效官方券",
        }
    }
    w_ok = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(capture_calls=audit_calls),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    out_ok = w_ok.run_once()
    assert out_ok["published"] == 1
    assert audit_calls[0]["preset_model_group"] == "low"

    formal_doc = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
    assert formal_doc["deal_info"]["source_is_reliable"] is True
    assert formal_doc["deal_info"]["coupon_links"][0]["url"] == "https://coupon.m.jd.com/coupons/show.action?key=c1&roleId=999"
    assert "https://" not in formal_doc["deal_info"]["tutorial_text"]
    assert "https://" not in formal_doc["deal_info"]["original_copywriting"]

    # 2. 附加优惠券经 convert_single_target 指向异品时，Step A 直接拦截、删图并展示友好原因
    ps.get_goods_detail.return_value = _sample_detail_product(pid="1001002")
    res_bad_cpn = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001002",
        deal_price=19.9,
        tutorial_text="领券下单",
        coupon_input="https://u.jd.com/other_prod",
        images_base64=[_make_png_b64(400, 300)],
    )
    bad_cpn_doc = raw_repo.col.find_one({"source_item_id": res_bad_cpn["data"]["tip_id"]})
    bad_img_id = bad_cpn_doc["tutorial_images"][0].rsplit("/", 1)[-1]
    assert svc.get_image_file_path(bad_img_id) is not None

    cleaner_bad = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)
    cleaner_bad.convert_single_target = lambda tgt, default_plat="unknown": {
        "link_id": tgt["link_id"],
        "clean_input": tgt["clean_input"],
        "text_role_hint": "coupon_candidate",
        "platform": "jd",
        "convert_status": "product",
        "goods_id": "9999999",
        "my_promo_link": "https://u.jd.com/other_prod",
        "official_product": {"title": "异品", "price": 9.9},
    }
    audit_never = []
    w_bad_cpn = DealSubmitWorker(
        svc,
        cleaner=cleaner_bad,
        ai_generator=_make_audit_gen(capture_calls=audit_never),
    )
    out_bad = w_bad_cpn.run_once()
    assert out_bad["coupon_failed"] == 1
    assert out_bad["rejected"] == 1
    assert audit_never == []
    assert svc.get_image_file_path(bad_img_id) is None

    my_items = svc.get_my_submissions("u_alice")["data"]["items"]
    assert my_items[0]["tip_id"] == res_bad_cpn["data"]["tip_id"]
    assert my_items[0]["state"] == "rejected"
    assert "指向其他商品" in my_items[0]["reason"]


def test_full_multimodal_media_comparison_and_resume_checkpoint(tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    img_dir = tmp_path / "deal_submit_images"

    # 先持久化一张现有在架好价的本地图片，再附带一张远程图，模拟在架好价共 2 张晒图
    img_dir.mkdir(parents=True, exist_ok=True)
    ex_local_id = "dsi_0123456789abcdef0123"
    (img_dir / f"{ex_local_id}.jpg").write_bytes(_make_png_bytes(200, 200))
    ex_local_url = f"/api/deals/image/{ex_local_id}"

    existing_prod = {
        "platform": "jd",
        "product_id": "1001001",
        "goods_id": "1001001",
        "name": "维达抽纸",
        "image_url": "https://img.example.test/vinda_official.jpg",
        "price": 59.9,
        "user_stats": {"buyable": 1, "unbuyable": 6},
        "deal_info": {
            "is_deal_tip": True,
            "source_site": "guangdiu",
            "source_item_id": "old_tip_1",
            "source_is_reliable": True,
            "quality_score": 92,
            "deal_price": 32.9,
            "price_logic": "旧券后价",
            "card_highlight": "32.9元",
            "tutorial_steps": ["1. 原旧攻略步骤"],
            "original_copywriting": "原在架详细文案说明",
            "user_share_images": [ex_local_url, "https://img.example.test/existing_remote.jpg"],
            "updated_at": now,
        },
    }
    db, raw_repo, _, ps, svc = _make_harness(initial_products=[existing_prod], image_dir=img_dir)

    # 1. 不相关图文驳回：验证删图、不动正式表、临时目录已释放
    res_rej = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=19.9,
        tutorial_text="乱填测试内容",
        images_base64=[_make_png_b64(400, 300)],
    )
    rej_img_id = raw_repo.col.find_one({"source_item_id": res_rej["data"]["tip_id"]})["tutorial_images"][0].rsplit("/", 1)[-1]
    rej_calls: List[Dict[str, Any]] = []
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)
    w_rej = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=False, reject_reason="图片与当前商品毫不相干", capture_calls=rej_calls),
        remote_image_fetcher=lambda _u: _make_png_bytes(160, 160),
    )
    assert w_rej.run_once()["rejected"] == 1
    assert svc.get_image_file_path(rej_img_id) is None
    assert rej_calls[0]["parent_dir"].exists() is False
    assert rej_calls[0]["file_names"] == ["official_1.jpg", "new_1.jpg", "existing_1.jpg", "existing_2.jpg"]
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == "old_tip_1"

    # 2. 现有更优暂不替换：验证展示「已有更优好价」与对比原因
    res_keep = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=35.0,
        tutorial_text="详情页直接下单35元",
    )
    w_keep = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=False, decision_reason="现有在架价格与步骤均更优"),
        remote_image_fetcher=lambda _u: _make_png_bytes(160, 160),
    )
    assert w_keep.run_once()["skipped_existing"] == 1
    dup_item = svc.get_my_submissions("u_alice")["data"]["items"][0]
    assert dup_item["tip_id"] == res_keep["data"]["tip_id"]
    assert dup_item["state"] == "duplicate"
    assert dup_item["state_label"] == "已有更优好价"
    assert "更优" in dup_item["reason"]

    # 3. 验证全图文载荷 + AI-2 暂态超时单阶段续跑（续跑绝不重审）+ 原子防并发替换
    res_win = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=24.9,
        tutorial_text="领新补贴券拍下实付24.9元",
        images_base64=[_make_png_b64(500, 400)],
    )
    win_tid = res_win["data"]["tip_id"]
    win_calls: List[Dict[str, Any]] = []

    # 第一轮：AI-2 返回空（触发 retry_pending），应保持在 need_user_audit 且写下通过检查点
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {}
    w_win = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            should_replace=True,
            decision_reason="价格更优且原有好价多人反馈买不到",
            capture_calls=win_calls,
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(160, 160),
    )
    out_r1 = w_win.run_once()
    assert out_r1["promoted"] == 1
    assert out_r1["published"] == 0
    assert len(win_calls) == 1
    assert win_calls[0]["preset_model_group"] == "low"
    assert win_calls[0]["file_names"] == ["official_1.jpg", "new_1.jpg", "existing_1.jpg", "existing_2.jpg"]
    assert win_calls[0]["parent_dir"].exists() is False

    prompt_sent = win_calls[0]["prompt"]
    payload_str = prompt_sent.split("待处理结构化输入数据", 1)[1].split("\n", 1)[1].lstrip("= \n")
    payload_obj = json.loads(payload_str)
    assert payload_obj["official_product"]["image_attachment_indices"] == [1]
    assert payload_obj["new_submission"]["image_attachment_indices"] == [2]
    assert payload_obj["existing_deal"]["image_attachment_indices"] == [3, 4]
    assert payload_obj["existing_deal"]["original_copywriting"] == "原在架详细文案说明"
    assert payload_obj["existing_deal"]["unbuyable_count"] == 6

    cur_doc = raw_repo.col.find_one({"source_item_id": win_tid})
    assert cur_doc["clean_status"] == "pending"
    assert cur_doc["clean_stage"] == CLEAN_STAGE_NEED_USER_AUDIT
    assert cur_doc["user_audit_result"]["passed"] is True
    raw_repo.col.update_many({"source_item_id": win_tid}, {"$set": {"next_retry_after_ts": 0}})

    # 第二轮：AI-2 成功，直接复用 user_audit_result 断点（audit 不重调）并替换 old_tip_1
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达抽纸",
            "deal_price": 24.9,
            "price_logic": "新补贴券",
            "card_highlight": "24.9元",
            "coupon_links": [],
            "tutorial_steps": ["1. 领新券拍下24.9元"],
            "quality_score": 85,
            "price_is_reliable": True,
            "price_reliability_reason": "补贴真实",
        }
    }
    out_r2 = w_win.run_once()
    assert out_r2["published"] == 1
    assert len(win_calls) == 1
    updated_prod = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
    assert updated_prod["deal_info"]["source_item_id"] == win_tid
    assert updated_prod["deal_info"]["deal_price"] == 24.9
    assert updated_prod["deal_info"]["source_is_reliable"] is True
    assert "_verified_replace_source_item_id" not in updated_prod

    # 4. 并发竞态保护：若正式表已被 other_tip_2(95分) 抢先更新，旧对比标记 old_tip_1 不得绕过保护
    raw_repo.formal_col.docs[0]["deal_info"]["source_item_id"] = "other_tip_2"
    raw_repo.formal_col.docs[0]["deal_info"]["quality_score"] = 95
    race_res = upsert_cleaned_products_to_formal_table(
        raw_repo.formal_col,
        [{
            "platform": "jd",
            "product_id": "1001001",
            "goods_id": "1001001",
            "name": "维达抽纸",
            "price": 59.9,
            "_verified_replace_source_item_id": "old_tip_1",
            "deal_info": {
                "is_deal_tip": True,
                "source_site": USER_SUBMIT_SITE,
                "source_item_id": "us_stale",
                "source_is_reliable": True,
                "deal_price": 24.9,
                "quality_score": 80,
            },
        }],
    )
    assert race_res["protected_high_score"] == 1
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == "other_tip_2"


def test_screenshot_compression_pipeline_and_cleanup(tmp_path):
    big_png_b64 = _make_png_b64(width=2000, height=1000)
    jpeg_bytes = normalize_and_compress_deal_image(big_png_b64)
    with Image.open(io.BytesIO(jpeg_bytes)) as parsed:
        assert parsed.format == "JPEG"
        assert max(parsed.size) == USER_SUBMIT_MAX_IMAGE_DIM
        assert parsed.size == (1280, 640)

    with pytest.raises(DealSubmitError) as e_bad:
        normalize_and_compress_deal_image("data:image/png;base64,bm90LWFuLWltYWdl")
    assert e_bad.value.error_code == "invalid_image"

    img_dir = tmp_path / "deal_submit_images"
    db, raw_repo, _, ps, svc = _make_harness(image_dir=img_dir)

    with pytest.raises(DealSubmitError) as e_cnt:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=29.9,
            tutorial_text="附多图测试",
            images_base64=[big_png_b64] * 4,
        )
    assert e_cnt.value.error_code == "too_many_images"
    assert not img_dir.exists()

    res_img = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="附实拍截图供参考",
        images_base64=[big_png_b64],
    )
    img_url = raw_repo.col.find_one({"source_item_id": res_img["data"]["tip_id"]})["tutorial_images"][0]
    saved_path = svc.get_image_file_path(img_url.rsplit("/", 1)[-1])
    assert saved_path is not None and saved_path.is_file()

    old_ts = 1000.0
    os.utime(saved_path, (old_ts, old_ts))
    assert cleanup_expired_deal_submit_images(image_dir=img_dir, now=old_ts + 8 * 86400) == 1
    assert not saved_path.exists()


def test_audit_fail_closed_retry_source_isolation_and_lifespan(monkeypatch, tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    old_utc = now - datetime.timedelta(hours=80)
    db, raw_repo, _, ps, svc = _make_harness(
        initial_products=[
            {
                "platform": "jd",
                "product_id": "crawler_prod_1",
                "deal_info": {"is_deal_tip": True, "source_site": "guangdiu", "updated_at": old_utc},
            },
            {
                "platform": "jd",
                "product_id": "user_prod_1",
                "deal_info": {"is_deal_tip": True, "source_site": USER_SUBMIT_SITE, "updated_at": old_utc},
            },
        ],
        image_dir=tmp_path / "imgs",
    )
    raw_repo.col.docs.append({
        "source_site": "guangdiu",
        "source_item_id": "gd_1",
        "clean_status": "pending",
        "clean_stage": CLEAN_STAGE_NEED_USER_AUDIT,
        "clean_retry_count": MAX_CLEAN_RETRIES,
    })

    res = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="详情页领券购买",
        images_base64=[_make_png_b64(300, 300)],
    )
    tip_id = res["data"]["tip_id"]
    img_id = raw_repo.col.find_one({"source_item_id": tip_id})["tutorial_images"][0].rsplit("/", 1)[-1]
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)
    w_err = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=lambda **kwargs: {"status": "✅ 成功", "content": "not-json"},
        remote_image_fetcher=lambda _u: None,
    )
    for idx in range(MAX_CLEAN_RETRIES):
        w_err.run_once()
        cur = raw_repo.col.find_one({"source_item_id": tip_id})
        if idx < MAX_CLEAN_RETRIES - 1:
            assert cur["clean_status"] == "pending"
            assert cur["clean_stage"] == CLEAN_STAGE_NEED_USER_AUDIT
            raw_repo.col.update_many({"source_item_id": tip_id}, {"$set": {"next_retry_after_ts": 0}})
        else:
            assert cur["clean_status"] == "cleaned_failed"
            assert cur["clean_error_code"] == ERR_AI_MAX_RETRIES_EXCEEDED
            assert svc.get_image_file_path(img_id) is None

    w_err.run_maintenance_once()
    assert raw_repo.col.find_one({"source_site": "guangdiu", "source_item_id": "gd_1"})["clean_status"] == "pending"
    assert raw_repo.formal_col.find_one({"product_id": "crawler_prod_1"})["deal_info"]["is_deal_tip"] is True
    assert raw_repo.formal_col.find_one({"product_id": "user_prod_1"})["deal_info"]["is_deal_tip"] is False

    import api.deal_submit as api_mod
    monkeypatch.setattr(api_mod, "deal_submit_service", svc)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False, deal_submit_worker_enabled=True),
    )
    app_on = create_app()
    with TestClient(app_on):
        assert app_on.state.deal_submit_worker is not None
        assert app_on.state.deal_submit_worker._thread is not None
        assert app_on.state.deal_submit_worker._thread.is_alive() is True
    assert app_on.state.deal_submit_worker is None


def test_http_routes_and_submit_page(monkeypatch, tmp_path):
    import api.deal_submit as api_mod

    db, raw_repo, _, ps, svc = _make_harness(image_dir=tmp_path / "imgs")
    monkeypatch.setattr(api_mod, "deal_submit_service", svc)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(config.settings, warmup_enabled=False, detail_prefetch_enabled=False),
    )

    app = create_app()
    with TestClient(app) as client:
        page_resp = client.get("/submit_deal")
        assert page_resp.status_code == 200
        assert "好价爆料中心" in page_resp.text

        assert client.post(
            "/api/deals/submit",
            json={"platform": "jd", "product_id": "1001001", "deal_price": 29.9, "tutorial_text": "先领券下单"},
        ).status_code == 401
        assert client.get("/api/deals/my_submissions").status_code == 401

        token = create_access_token("u_alice")
        ok_post = client.post(
            "/api/deals/submit",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform": "jd",
                "product_id": "1001001",
                "deal_price": 29.9,
                "tutorial_text": "领券直接购买29.9",
                "images_base64": [_make_png_b64(400, 300)],
            },
        )
        assert ok_post.status_code == 200
        tip_id = ok_post.json()["data"]["tip_id"]
        img_url = raw_repo.col.find_one({"source_item_id": tip_id})["tutorial_images"][0]
        img_resp = client.get(img_url)
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"].startswith("image/jpeg")

        list_resp = client.get("/api/deals/my_submissions", headers={"Authorization": f"Bearer {token}"})
        assert list_resp.status_code == 200
        items = list_resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["state"] == "pending"
        assert items[0]["image_count"] == 1
