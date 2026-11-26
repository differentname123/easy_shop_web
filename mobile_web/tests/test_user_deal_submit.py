# -*- coding: utf-8 -*-
"""详情页锚定用户爆料核心流程、图片压缩、全图文对比与单阶段流水线集成测试。"""
from __future__ import annotations

import base64
import copy
import datetime
import io
import json
import os
import time
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
    SOURCE_REPUTATION_BONUS,
    USER_SUBMIT_SITE,
    is_reliable_source_site,
)
from models.product import compute_user_stats_score, get_source_site_name
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.product_repo import ProductRepository, upsert_cleaned_products_to_formal_table
from repositories.user_repo import UserRepository
from services.deal_pool_cleaner import DealPoolCleaner
from services.deal_submit_service import (
    USER_SUBMIT_MAX_IMAGE_DIM,
    USER_SUBMIT_MAX_PER_WINDOW,
    DealSubmitError,
    DealSubmitService,
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
    assert "不要求必须真实付款完成" in prompt_text
    assert "无论新价格更低，还是比旧价格更高" in prompt_text
    assert "同价防抄袭保护" in prompt_text
    assert "政治敏感" in prompt_text and "色情低俗" in prompt_text
    for banned_example in ("某品牌", "凡士林", "微信号", "QQ号", "0.01元"):
        assert banned_example not in prompt_text

    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"},
            {"uid": "u_bob", "phone": "13900139000", "nickname": "阿博"},
        ],
        image_dir=tmp_path / "imgs",
    )
    proof_img = _make_png_b64(320, 240)

    with pytest.raises(DealSubmitError) as e_auth:
        svc.submit_user_deal(uid="", platform="jd", product_id="1001001", deal_price=29.9, tutorial_text="先领券下单", images_base64=[proof_img])
    assert e_auth.value.status_code == 401

    with pytest.raises(DealSubmitError) as e_pid:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="", deal_price=29.9, tutorial_text="先领券下单", images_base64=[proof_img])
    assert e_pid.value.error_code == "missing_product_identity"

    with pytest.raises(DealSubmitError) as e_price:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="1001001", deal_price=0, tutorial_text="先领券下单", images_base64=[proof_img])
    assert e_price.value.error_code == "invalid_deal_price"

    with pytest.raises(DealSubmitError) as e_no_img:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="1001001", deal_price=29.9, tutorial_text="先领券下单")
    assert e_no_img.value.error_code == "missing_proof_image"
    assert e_no_img.value.status_code == 400

    # 未知平台统一由 ProductService / Adapter 校验拒绝
    ps.get_goods_detail.side_effect = PlatformUnavailableError("不支持的平台: unknown_shop")
    with pytest.raises(DealSubmitError) as e_plat:
        svc.submit_user_deal(uid="u_bob", platform="unknown_shop", product_id="1001001", deal_price=29.9, tutorial_text="下单", images_base64=[proof_img])
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
        images_base64=[proof_img],
    )
    assert res_ext["status"] == "success"
    assert res_ext["data"]["message"] == "提交成功，已进入人工审查"
    doc_ext = raw_repo.col.find_one({"source_item_id": res_ext["data"]["tip_id"]})
    assert doc_ext["platform"] == "vip"
    assert doc_ext["resolved_platform"] == "vip"
    assert doc_ext["resolved_product_id"] == "vip_9001"
    assert "ttl_at" not in doc_ext
    assert doc_ext["source_is_reliable"] is True

    # 验证非用户爆料渠道（如 guangdiu）写入 ttl_at（14天清理仅针对渠道数据）
    raw_repo.save_batch([{
        "source_site": "guangdiu",
        "source_item_id": "gd_ttl_check",
        "title": "测试渠道商品",
        "extracted_links": [{"url": "https://item.jd.com/1001.html"}],
        "publish_ts": datetime.datetime.now(datetime.timezone.utc).timestamp(),
    }], task_name="guangdiu")
    doc_gd = raw_repo.col.find_one({"source_item_id": "gd_ttl_check"})
    assert doc_gd.get("ttl_at") is not None
    assert doc_ext["clean_stage"] == CLEAN_STAGE_NEED_USER_AUDIT
    assert len(doc_ext["converted_links"]) == 1
    assert any(t.get("link_id") == "L2" for t in doc_ext["ai_stage1_result"]["extracted_targets"])
    pending_view = svc.get_my_submissions("u_alice")["data"]["items"][0]
    assert pending_view["state_label"] == "人工审查中"
    assert "人工审查" in pending_view["reason"]

    # 同一用户 10 分钟最多 USER_SUBMIT_MAX_PER_WINDOW 次（刚才已用 1 次，再补发满额度）
    for i in range(USER_SUBMIT_MAX_PER_WINDOW - 1):
        ps.get_goods_detail.return_value = _sample_detail_product(pid=f"p_{i}")
        res = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id=f"p_{i}",
            deal_price=29.9,
            tutorial_text=f"领详情页补贴下单实付29.9 {i}",
            images_base64=[proof_img],
        )
        assert res["status"] == "success"

    with pytest.raises(DealSubmitError) as e_lim:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="p_over",
            deal_price=29.9,
            tutorial_text="再多发一条超限",
            images_base64=[proof_img],
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
        images_base64=[_make_png_b64(400, 300)],
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
    # 驳回时不再立即删图（统一交由 30 天带引用保护定时清理，避免多人共享同图误删）
    assert svc.get_image_file_path(bad_img_id) is not None

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
    assert svc.get_image_file_path(rej_img_id) is not None
    assert rej_calls[0]["parent_dir"].exists() is False
    assert rej_calls[0]["file_names"] == ["official_1.jpg", "new_1.jpg", "existing_1.jpg", "existing_2.jpg"]
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == "old_tip_1"

    # 2. 现有同价暂不替换：验证展示「已有同价好价」、保留硬盘图片避免误删同图，且保留现有反馈票数
    res_keep = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=35.0,
        tutorial_text="详情页直接下单35元",
        images_base64=[_make_png_b64(400, 300)],
    )
    keep_img_id = raw_repo.col.find_one({"source_item_id": res_keep["data"]["tip_id"]})["tutorial_images"][0].rsplit("/", 1)[-1]
    assert keep_img_id == rej_img_id  # 同内容截图复用同一指纹文件名
    w_keep = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=False, decision_reason="现有在架价格与步骤均更优"),
        remote_image_fetcher=lambda _u: _make_png_bytes(160, 160),
    )
    assert w_keep.run_once()["skipped_existing"] == 1
    assert svc.get_image_file_path(keep_img_id) is not None
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["user_stats"] == {"buyable": 1, "unbuyable": 6}
    dup_item = svc.get_my_submissions("u_alice")["data"]["items"][0]
    assert dup_item["tip_id"] == res_keep["data"]["tip_id"]
    assert dup_item["state"] == "duplicate"
    assert dup_item["state_label"] == "已有同价好价"
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
    assert updated_prod["user_stats"] == {"buyable": 0, "unbuyable": 0}
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
    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"},
            {"uid": "u_bob", "phone": "13800138001", "nickname": "阿博"},
        ],
        image_dir=img_dir,
    )

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

    # 用户爆料截图遵循 30 天保留期：29 天留存，满 30 天在维护轮自动清理；非 dsi_*.jpg 文件不被误删
    now_ts = time.time()
    os.utime(saved_path, (now_ts - 29 * 86400, now_ts - 29 * 86400))

    expired_id = "dsi_abcdef0123456789abcd"
    expired_path = img_dir / f"{expired_id}.jpg"
    expired_path.write_bytes(_make_png_bytes(120, 120))
    os.utime(expired_path, (now_ts - 31 * 86400, now_ts - 31 * 86400))

    unrelated_path = img_dir / "keep_manual.jpg"
    unrelated_path.write_bytes(_make_png_bytes(100, 100))
    os.utime(unrelated_path, (now_ts - 90 * 86400, now_ts - 90 * 86400))

    # 同图内容去重：两次上传相同内容截图应产出同一指纹编号并刷新 mtime；且带活跃引用的图满 30 天仍受保护
    old_mtime = now_ts - 40 * 86400
    os.utime(saved_path, (old_mtime, old_mtime))
    svc.submit_user_deal(
        uid="u_bob",
        platform="jd",
        product_id="1001001",
        deal_price=28.9,
        tutorial_text="同图续期验证",
        images_base64=[big_png_b64, big_png_b64],
    )
    saved_id = img_url.rsplit("/", 1)[-1]
    assert saved_path.stat().st_mtime > now_ts - 60

    # 正式表或待审队列引用的超 30 天文件受白名单保护不被误删；无引用超 30 天孤儿文件被清理
    protected_formal_id = "dsi_11112222333344445555"
    protected_formal_path = img_dir / f"{protected_formal_id}.jpg"
    protected_formal_path.write_bytes(_make_png_bytes(120, 120))
    os.utime(protected_formal_path, (now_ts - 45 * 86400, now_ts - 45 * 86400))
    raw_repo.formal_col.docs.append({
        "platform": "jd",
        "product_id": "1001001",
        "deal_info": {"is_deal_tip": True, "user_share_images": [f"/api/deals/image/{protected_formal_id}"]},
    })

    worker_maint = DealSubmitWorker(svc, cleaner=DealPoolCleaner(ai_batch_size=2, convert_max_workers=1))
    worker_maint.run_maintenance_once()
    assert saved_path.exists()
    assert protected_formal_path.exists()
    assert not expired_path.exists()
    assert unrelated_path.exists()

    # DB 查询异常时 Fail-Closed：绝不盲删文件
    orphan2_path = img_dir / "dsi_99998888777766665555.jpg"
    orphan2_path.write_bytes(_make_png_bytes(120, 120))
    os.utime(orphan2_path, (now_ts - 45 * 86400, now_ts - 45 * 86400))
    orig_find = raw_repo.formal_col.find
    raw_repo.formal_col.find = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        assert svc.purge_expired_images() == 0
        assert orphan2_path.exists()
    finally:
        raw_repo.formal_col.find = orig_find


def test_audit_fail_closed_retry_source_isolation_and_lifespan(monkeypatch, tmp_path):
    from cache import BOOM_SORT_CACHE, cache_deal_tip, clear_cached_deal_tip, get_cached_deal_tip

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    old_utc = now - datetime.timedelta(hours=80)
    db, raw_repo, _, ps, svc = _make_harness(
        initial_products=[
            {
                "platform": "jd",
                "product_id": "crawler_prod_1",
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": "guangdiu",
                    "source_item_id": "gd_formal_1",
                    "updated_at": old_utc,
                },
            },
            {
                "platform": "jd",
                "product_id": "user_prod_1",
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": USER_SUBMIT_SITE,
                    "source_item_id": "us_formal_1",
                    "updated_at": old_utc,
                },
            },
            {
                "platform": "jd",
                "product_id": "user_prod_2",
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": USER_SUBMIT_SITE,
                    "source_item_id": "us_formal_2",
                    "updated_at": old_utc,
                },
            },
        ],
        image_dir=tmp_path / "imgs",
    )
    raw_repo.col.docs.extend([
        {
            "source_site": "guangdiu",
            "source_item_id": "gd_1",
            "clean_status": "pending",
            "clean_stage": CLEAN_STAGE_NEED_USER_AUDIT,
            "clean_retry_count": MAX_CLEAN_RETRIES,
        },
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_formal_1",
            "display_started_at": old_utc,
        },
        {
            "source_site": USER_SUBMIT_SITE,
            "source_item_id": "us_formal_2",
            "display_started_at": old_utc,
        },
    ])

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
    woke: List[int] = []
    w_err = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=lambda **kwargs: {"status": "✅ 成功", "content": "not-json"},
        remote_image_fetcher=lambda _u: None,
        on_formal_updated=lambda: woke.append(1),
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
            assert svc.get_image_file_path(img_id) is not None

    clear_cached_deal_tip()
    BOOM_SORT_CACHE.clear()
    try:
        cache_deal_tip("jd", "user_prod_1", "ok", {"is_deal_tip": True})
        BOOM_SORT_CACHE.set(("all", "desc"), [{"product_id": "user_prod_1"}])

        # 用户爆料工作线程维护只负责 user_submit（队列重试/租约/超期72h自动下线+缓存刷新+结束展期回写），不动普通渠道
        w_err.run_maintenance_once()
        assert raw_repo.col.find_one({"source_site": "guangdiu", "source_item_id": "gd_1"})["clean_status"] == "pending"
        assert raw_repo.formal_col.find_one({"product_id": "crawler_prod_1"})["deal_info"]["is_deal_tip"] is True
        assert raw_repo.formal_col.find_one({"product_id": "user_prod_1"})["deal_info"]["is_deal_tip"] is False
        assert raw_repo.formal_col.find_one({"product_id": "user_prod_2"})["deal_info"]["is_deal_tip"] is False
        assert raw_repo.col.find_one({"source_item_id": "us_formal_1"}).get("display_ended_at") is not None
        assert "replaced_by_item_id" not in raw_repo.col.find_one({"source_item_id": "us_formal_1"})
        assert get_cached_deal_tip("jd", "user_prod_1") is None
        assert BOOM_SORT_CACHE.get(("all", "desc")) is None
        assert len(woke) == 1

        # 恢复一条超过 72h 的 user_prod_2，验证全局 expire_stale_formal_deals() 对全平台含 user_submit 一视同仁 72h 自动过期
        raw_repo.formal_col.update_many(
            {"product_id": "user_prod_2"},
            {"$set": {"deal_info.is_deal_tip": True, "deal_info.updated_at": old_utc}},
        )
        assert raw_repo.expire_stale_formal_deals() == 2
        assert raw_repo.formal_col.find_one({"product_id": "crawler_prod_1"})["deal_info"]["is_deal_tip"] is False
        assert raw_repo.formal_col.find_one({"product_id": "user_prod_2"})["deal_info"]["is_deal_tip"] is False
    finally:
        clear_cached_deal_tip()
        BOOM_SORT_CACHE.clear()

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
        assert "max-age=86400" in img_resp.headers.get("cache-control", "")

        missing_img_resp = client.get("/api/deals/image/dsi_00000000000000000000")
        assert missing_img_resp.status_code == 404
        assert missing_img_resp.headers.get("cache-control") == "no-store"

        list_resp = client.get("/api/deals/my_submissions", headers={"Authorization": f"Bearer {token}"})
        assert list_resp.status_code == 200
        items = list_resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["state"] == "pending"
        assert items[0]["state_label"] == "人工审查中"
        assert items[0]["image_count"] == 1


def test_deal_display_lifecycle_and_product_history(tmp_path):
    """验证每条爆料开始展示时间、被替代时间及替代者标识、72h 过期结束时间与按商品历史反查。"""
    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"},
            {"uid": "u_bob", "phone": "13900139000", "nickname": "阿博"},
        ],
        image_dir=tmp_path / "imgs",
    )
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)

    # 1. 爆料 A 上线开始展示 -> 记录 display_started_at 与商品关联主键
    res_a = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="领券下单29.9元",
        images_base64=[_make_png_b64(360, 280)],
    )
    tip_a = res_a["data"]["tip_id"]
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达抽纸",
            "deal_price": 29.9,
            "price_logic": "券后",
            "card_highlight": "29.9元",
            "coupon_links": [],
            "tutorial_steps": ["1. 下单29.9元"],
            "quality_score": 82,
            "price_is_reliable": True,
            "price_reliability_reason": "有效",
        }
    }
    w_a = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=True),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    assert w_a.run_once()["published"] == 1
    doc_a = raw_repo.col.find_one({"source_item_id": tip_a})
    assert doc_a["resolved_platform"] == "jd"
    assert doc_a["resolved_product_id"] == "1001001"
    assert doc_a.get("display_started_at") is not None
    assert "display_ended_at" not in doc_a

    # 2. 爆料 B 替代 A -> A 记下结束时间与被谁替代，B 记下开始时间
    res_b = svc.submit_user_deal(
        uid="u_bob",
        platform="jd",
        product_id="1001001",
        deal_price=23.9,
        tutorial_text="叠加补贴后实付23.9元",
        images_base64=[_make_png_b64(360, 280)],
    )
    tip_b = res_b["data"]["tip_id"]
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达抽纸",
            "deal_price": 23.9,
            "price_logic": "叠加补贴",
            "card_highlight": "23.9元",
            "coupon_links": [],
            "tutorial_steps": ["1. 叠加补贴拍23.9元"],
            "quality_score": 88,
            "price_is_reliable": True,
            "price_reliability_reason": "更优",
        }
    }
    w_b = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=True, decision_reason="价格更优且步骤清晰"),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    assert w_b.run_once()["published"] == 1
    doc_a_after = raw_repo.col.find_one({"source_item_id": tip_a})
    doc_b = raw_repo.col.find_one({"source_item_id": tip_b})
    assert doc_a_after.get("display_ended_at") is not None
    assert "display_end_reason" not in doc_a_after
    assert doc_a_after["replaced_by_site"] == USER_SUBMIT_SITE
    assert doc_a_after["replaced_by_item_id"] == tip_b
    assert doc_b.get("display_started_at") is not None
    assert "display_ended_at" not in doc_b

    # 3. 爆料 C 未能替代 B（已有同价凭证）-> 可通过商品查到该次投稿，但绝不会记录 display_started_at
    res_c = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=23.9,
        tutorial_text="同价复投23.9元",
        images_base64=[_make_png_b64(360, 280)],
    )
    tip_c = res_c["data"]["tip_id"]
    w_c = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=False, decision_reason="现有在架已有同价截图凭证"),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    assert w_c.run_once()["skipped_existing"] == 1
    doc_c = raw_repo.col.find_one({"source_item_id": tip_c})
    assert "display_started_at" not in doc_c

    # 4. 所有好价（含用户爆料）超过 72h 统一自动下线，并自动闭合展示结束时间 `display_ended_at`，记录永久留存可反查
    old_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=80)
    raw_repo.formal_col.update_many(
        {"platform": "jd", "product_id": "1001001"},
        {"$set": {"deal_info.updated_at": old_utc, "deal_info.last_seen_at": old_utc}},
    )
    assert raw_repo.expire_stale_formal_deals() == 1
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["is_deal_tip"] is False
    doc_b_expired = raw_repo.col.find_one({"source_item_id": tip_b})
    assert doc_b_expired.get("display_ended_at") is not None
    assert "replaced_by_site" not in doc_b_expired
    assert "replaced_by_item_id" not in doc_b_expired

    # 5. 按商品反查全部历史投稿与实际展示时间线
    all_hist = raw_repo.list_product_deal_history("jd", "1001001")
    assert {item["source_item_id"] for item in all_hist} == {tip_a, tip_b, tip_c}
    displayed_hist = raw_repo.list_product_deal_history("jd", "1001001", only_displayed=True)
    assert [item["source_item_id"] for item in displayed_hist] == [tip_b, tip_a]


def test_repeat_submit_same_text_new_image_never_bypasses_audit(tmp_path):
    """同用户同文案价格换新图重提：必须生成全新 tip_id 并独立重走审核，严禁撞库吞单或覆写已审截图。"""
    db, raw_repo, _, _, svc = _make_harness(image_dir=tmp_path / "imgs")
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=1)

    res1 = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="领券拍下实付29.9",
        coupon_input="",
        images_base64=[_make_png_b64(320, 240, color=(200, 40, 40, 255))],
    )
    tid1 = res1["data"]["tip_id"]
    img1 = raw_repo.col.find_one({"source_item_id": tid1})["tutorial_images"][:]

    # 先让第一次提交审核通过并上架
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达抽纸",
            "deal_price": 29.9,
            "price_logic": "券后",
            "card_highlight": "29.9元",
            "coupon_links": [],
            "tutorial_steps": ["1. 下单29.9"],
            "quality_score": 82,
            "price_is_reliable": True,
            "price_reliability_reason": "有效",
        }
    }
    w1 = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=True),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    assert w1.run_once()["published"] == 1

    # 同用户以完全相同的文案价格 + 新图片再次提交
    res2 = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="领券拍下实付29.9",
        coupon_input="",
        images_base64=[_make_png_b64(320, 240, color=(40, 180, 40, 255))],
    )
    tid2 = res2["data"]["tip_id"]
    assert tid2 != tid1
    doc1_after = raw_repo.col.find_one({"source_item_id": tid1})
    doc2 = raw_repo.col.find_one({"source_item_id": tid2})
    assert doc1_after["clean_status"] == "cleaned_success"
    assert doc1_after["tutorial_images"] == img1
    assert doc2["clean_status"] == "pending"
    assert doc2["clean_stage"] == CLEAN_STAGE_NEED_USER_AUDIT

    # 第二次提交仍须正常进入审核，不得绕过
    audit_calls: List[Dict[str, Any]] = []
    w2 = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=False,
            reject_reason="新截图未体现价格凭证",
            capture_calls=audit_calls,
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    out2 = w2.run_once()
    assert len(audit_calls) == 1
    assert out2["rejected"] == 1
    assert raw_repo.col.find_one({"source_item_id": tid2})["clean_status"] == "cleaned_failed"


def test_user_submit_reputation_bonus_and_immediate_cache_invalidation(tmp_path):
    """验证通过审核的用户爆料获 +10 信誉加分（低分仍卡 >=60 门槛），且上架后立即失效相关缓存。"""
    from cache import BOOM_SORT_CACHE, cache_deal_tip, clear_cached_deal_tip, get_cached_deal_tip

    assert SOURCE_REPUTATION_BONUS.get(USER_SUBMIT_SITE) == 10
    db, raw_repo, _, _, svc = _make_harness(image_dir=tmp_path / "imgs")
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=1)

    clear_cached_deal_tip()
    BOOM_SORT_CACHE.clear()
    try:
        cache_deal_tip("jd", "1001001", "missing", None)
        cache_deal_tip("jd", "9999999", "ok", {"deal_price": 88.0})
        BOOM_SORT_CACHE.set(("all", "desc"), [{"product_id": "1001001"}])

        # 1. AI-2 基础分 72 >= 60：通过加分 +10 -> 正式表落盘 82 分，且缓存立即可见刷新
        res_pass = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=28.0,
            tutorial_text="领券拍下实付28元",
            images_base64=[_make_png_b64(360, 260)],
        )
        cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
            stage2_inputs[0]["main_tip_id"]: {
                "main_tip_id": stage2_inputs[0]["main_tip_id"],
                "is_valid_deal": True,
                "reject_reason": None,
                "main_link_id": "L1",
                "clean_title": "维达抽纸",
                "deal_price": 28.0,
                "price_logic": "券后",
                "card_highlight": "28元",
                "coupon_links": [],
                "tutorial_steps": ["1. 下单28元"],
                "quality_score": 72,
                "price_is_reliable": True,
                "price_reliability_reason": "凭证清晰",
            }
        }
        w_ok = DealSubmitWorker(
            svc,
            cleaner=cleaner,
            ai_generator=_make_audit_gen(is_valid=True, should_replace=True),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        )
        assert w_ok.run_once()["published"] == 1
        formal = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
        assert formal["deal_info"]["quality_score"] == 82
        assert get_cached_deal_tip("jd", "1001001") is None
        assert BOOM_SORT_CACHE.get(("all", "desc")) is None
        assert get_cached_deal_tip("jd", "9999999") == ("ok", {"deal_price": 88.0})

        # 2. AI-2 基础分 55 < 60：严选门槛不因来源加分放水，依然淘汰
        res_low = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=26.0,
            tutorial_text="低分测试",
            images_base64=[_make_png_b64(360, 260)],
        )
        cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
            stage2_inputs[0]["main_tip_id"]: {
                "main_tip_id": stage2_inputs[0]["main_tip_id"],
                "is_valid_deal": True,
                "reject_reason": None,
                "main_link_id": "L1",
                "clean_title": "维达抽纸",
                "deal_price": 26.0,
                "price_logic": "券后",
                "card_highlight": "26元",
                "coupon_links": [],
                "tutorial_steps": ["1. 26元"],
                "quality_score": 55,
                "price_is_reliable": True,
                "price_reliability_reason": "一般",
            }
        }
        out_low = w_ok.run_once()
        assert out_low["published"] == 0
        assert out_low["rejected"] == 1
        assert raw_repo.col.find_one({"source_item_id": res_low["data"]["tip_id"]})["clean_status"] == "cleaned_low_score"
    finally:
        clear_cached_deal_tip()
        BOOM_SORT_CACHE.clear()


def test_concurrent_same_product_batch_serialized_by_publish_ts(tmp_path):
    """同批次同商品多条提交时，按提交时间先后单线处理，后者释放租约顺延至下一轮对比前序在架结果。"""
    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"},
            {"uid": "u_bob", "phone": "13900139000", "nickname": "阿博"},
        ],
        image_dir=tmp_path / "imgs",
    )
    r_first = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="最早提交的29.9好价",
        images_base64=[_make_png_b64(320, 240)],
    )
    r_second = svc.submit_user_deal(
        uid="u_bob",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="稍晚提交的同价好价",
        images_base64=[_make_png_b64(320, 240)],
    )
    ps.get_goods_detail.return_value = _sample_detail_product(pid="1002002")
    r_other = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1002002",
        deal_price=19.9,
        tutorial_text="另一个商品19.9好价",
        images_base64=[_make_png_b64(320, 240)],
    )
    tid_first = r_first["data"]["tip_id"]
    tid_second = r_second["data"]["tip_id"]
    tid_other = r_other["data"]["tip_id"]

    raw_repo.col.update_many({"source_item_id": tid_first}, {"$set": {"publish_ts": 100.0}})
    raw_repo.col.update_many({"source_item_id": tid_other}, {"$set": {"publish_ts": 102.0}})
    raw_repo.col.update_many({"source_item_id": tid_second}, {"$set": {"publish_ts": 105.0}})

    price_by_tid = {tid_first: 29.9, tid_second: 29.9, tid_other: 19.9}
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=1)
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        inp["main_tip_id"]: {
            "main_tip_id": inp["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "商品标题",
            "deal_price": price_by_tid[inp["main_tip_id"]],
            "price_logic": "券后",
            "card_highlight": f"{price_by_tid[inp['main_tip_id']]}元",
            "coupon_links": [],
            "tutorial_steps": [f"1. 下单{price_by_tid[inp['main_tip_id']]}"],
            "quality_score": 82,
            "price_is_reliable": True,
            "price_reliability_reason": "凭证有效",
        }
        for inp in stage2_inputs
    }

    round1_audits: List[Dict[str, Any]] = []
    w1 = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, should_replace=True, capture_calls=round1_audits),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    out1 = w1.run_once()
    assert out1["claimed"] == 3
    assert out1["deferred"] == 1
    assert out1["published"] == 2
    assert len(round1_audits) == 2

    deferred_doc = raw_repo.col.find_one({"source_item_id": tid_second})
    assert deferred_doc["clean_status"] == "pending"
    assert deferred_doc["clean_retry_count"] == 0
    assert "stage_lease_owner" not in deferred_doc
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == tid_first

    # 第二轮拾起顺延的 tid_second，此时对比的是 tid_first 在架记录
    round2_audits: List[Dict[str, Any]] = []
    w2 = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            should_replace=False,
            decision_reason="已有同价在架凭证",
            capture_calls=round2_audits,
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    out2 = w2.run_once()
    assert out2["claimed"] == 1
    assert out2["deferred"] == 0
    assert out2["skipped_existing"] == 1
    assert len(round2_audits) == 1
    assert '"existing_deal": null' not in round2_audits[0]["prompt"]
    second_after = raw_repo.col.find_one({"source_item_id": tid_second})
    assert second_after["clean_error_code"] == OK_EXISTING_DEAL_BETTER
    assert "display_started_at" not in second_after


def test_feedback_vote_reset_and_user_stats_score_on_takeover():
    """仅在新好价真正接管商品时清零 buyable/unbuyable 并重算 user_stats_score，保留浏览/下单等计数。"""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    formal_col = _MemCollection([
        {
            "platform": "jd",
            "product_id": "p_with_views",
            "name": "商品A",
            "user_stats": {"buyable": 3, "unbuyable": 12, "view": 40, "valid_orders": 2},
            "user_stats_score": 99.0,
            "deal_info": {
                "is_deal_tip": True,
                "source_site": "guangdiu",
                "source_item_id": "old_1",
                "source_is_reliable": False,
                "quality_score": 70,
                "deal_price": 30.0,
                "updated_at": now,
            },
        },
        {
            "platform": "jd",
            "product_id": "p_votes_only",
            "name": "商品B",
            "user_stats": {"buyable": 1, "unbuyable": 4},
            "user_stats_score": 6.1111,
            "deal_info": {
                "is_deal_tip": True,
                "source_site": "guangdiu",
                "source_item_id": "old_2",
                "source_is_reliable": False,
                "quality_score": 70,
                "deal_price": 20.0,
                "updated_at": now,
            },
        },
        {
            "platform": "jd",
            "product_id": "p_same_deal",
            "name": "商品C",
            "user_stats": {"buyable": 5, "unbuyable": 2},
            "deal_info": {
                "is_deal_tip": True,
                "source_site": USER_SUBMIT_SITE,
                "source_item_id": "same_1",
                "source_is_reliable": True,
                "quality_score": 85,
                "deal_price": 25.0,
                "updated_at": now,
            },
        },
        {
            "platform": "jd",
            "product_id": "p_protected",
            "name": "商品D",
            "user_stats": {"buyable": 4, "unbuyable": 1},
            "deal_info": {
                "is_deal_tip": True,
                "source_site": USER_SUBMIT_SITE,
                "source_item_id": "prot_1",
                "source_is_reliable": True,
                "quality_score": 95,
                "deal_price": 15.0,
                "updated_at": now,
            },
        },
    ])

    upsert_cleaned_products_to_formal_table(
        formal_col,
        [
            {
                "platform": "jd",
                "product_id": "p_with_views",
                "goods_id": "p_with_views",
                "name": "商品A",
                "price": 39.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": USER_SUBMIT_SITE,
                    "source_item_id": "new_1",
                    "source_is_reliable": True,
                    "quality_score": 88,
                    "deal_price": 19.9,
                },
            },
            {
                "platform": "jd",
                "product_id": "p_votes_only",
                "goods_id": "p_votes_only",
                "name": "商品B",
                "price": 29.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": USER_SUBMIT_SITE,
                    "source_item_id": "new_2",
                    "source_is_reliable": True,
                    "quality_score": 86,
                    "deal_price": 12.9,
                },
            },
            {
                "platform": "jd",
                "product_id": "p_same_deal",
                "goods_id": "p_same_deal",
                "name": "商品C",
                "price": 29.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": USER_SUBMIT_SITE,
                    "source_item_id": "same_1",
                    "source_is_reliable": True,
                    "quality_score": 86,
                    "deal_price": 25.0,
                },
            },
            {
                "platform": "jd",
                "product_id": "p_protected",
                "goods_id": "p_protected",
                "name": "商品D",
                "price": 29.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": "guangdiu",
                    "source_item_id": "low_1",
                    "source_is_reliable": False,
                    "quality_score": 80,
                    "deal_price": 16.0,
                },
            },
            {
                "platform": "jd",
                "product_id": "p_brand_new",
                "goods_id": "p_brand_new",
                "name": "商品E",
                "price": 19.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": USER_SUBMIT_SITE,
                    "source_item_id": "new_5",
                    "source_is_reliable": True,
                    "quality_score": 84,
                    "deal_price": 9.9,
                },
            },
        ],
    )

    doc_a = formal_col.find_one({"product_id": "p_with_views"})
    assert doc_a["user_stats"] == {"buyable": 0, "unbuyable": 0, "view": 40, "valid_orders": 2}
    assert doc_a["user_stats_score"] == compute_user_stats_score(doc_a["user_stats"])

    doc_b = formal_col.find_one({"product_id": "p_votes_only"})
    assert doc_b["user_stats"] == {"buyable": 0, "unbuyable": 0}
    assert "user_stats_score" not in doc_b

    assert formal_col.find_one({"product_id": "p_same_deal"})["user_stats"] == {"buyable": 5, "unbuyable": 2}
    assert formal_col.find_one({"product_id": "p_protected"})["user_stats"] == {"buyable": 4, "unbuyable": 1}
    assert "user_stats" not in formal_col.find_one({"product_id": "p_brand_new"})
