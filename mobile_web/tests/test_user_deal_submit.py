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
    DEFAULT_NORMAL_RELIABILITY_SCORE,
    DEFAULT_RELIABLE_SCORE,
    ERR_AI2_COUPON_FAILED_OR_DROPPED,
    ERR_AI_MAX_RETRIES_EXCEEDED,
    ERR_USER_AUDIT_REJECTED,
    MAX_CLEAN_RETRIES,
    OK_EXISTING_DEAL_BETTER,
    PROMPT_USER_AUDIT_PATH,
    USER_SUBMIT_SITE,
)
from models.product import compute_user_stats_score, get_source_site_name
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.product_repo import ProductRepository, upsert_cleaned_products_to_formal_table
from repositories.user_repo import UserRepository
from services.deal_pool_cleaner import DealPoolCleaner
from services.deal_submit_service import (
    USER_SUBMIT_DAILY_LIMIT_AUTHORIZED,
    USER_SUBMIT_DAILY_LIMIT_UNAUTHORIZED,
    USER_SUBMIT_MAX_IMAGE_DIM,
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

    def insert_one(self, doc: Dict[str, Any]):
        stored = copy.deepcopy(doc)
        stored.setdefault("_id", f"mem_{len(self.docs) + 1}")
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

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
    evidence_supports_claims: Optional[bool] = None,
    reject_reason: str = "",
    price_comparison: str = "cheaper",
    comparison_reason: str = "综合图文评审通过",
    observed_image_evidence: str = "图片清晰展示商品及对应价格证据",
    clean_title: str = "维达超韧抽纸",
    price_logic: str = "券后实付",
    card_highlight: str = "到手24.9元",
    coupon_label: str = "50减25优惠券",
    tutorial_steps: Optional[List[str]] = None,
    polished_copywriting: str = "真实实拍好价分享",
    capture_calls: Optional[List[Dict[str, Any]]] = None,
):
    eff_evidence = is_valid if evidence_supports_claims is None else evidence_supports_claims
    eff_steps = tutorial_steps if tutorial_steps is not None else ["1. 下单实付好价"]

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
                    "observed_image_evidence": observed_image_evidence,
                    "evidence_supports_claims": eff_evidence,
                    "is_content_valid": is_valid,
                    "content_reject_reason": reject_reason,
                    "price_comparison": price_comparison,
                    "comparison_reason": comparison_reason,
                    "clean_title": clean_title,
                    "price_logic": price_logic,
                    "card_highlight": card_highlight,
                    "coupon_label": coupon_label,
                    "tutorial_steps": eff_steps,
                    "polished_copywriting": polished_copywriting,
                },
                ensure_ascii=False,
            ),
        }

    return _gen


def test_core_input_validation_extensible_platform_and_daily_quota(tmp_path):
    assert get_source_site_name(USER_SUBMIT_SITE) == "用户爆料"
    assert DEFAULT_NORMAL_RELIABILITY_SCORE == 10
    assert DEFAULT_RELIABLE_SCORE == 20

    prompt_text = Path(PROMPT_USER_AUDIT_PATH).read_text(encoding="utf-8")
    assert prompt_text.strip()
    for required_kw in (
        "observed_image_evidence",
        "evidence_supports_claims",
        "不要求必须真实付款完成",
        "price_comparison",
        "cheaper",
        "same",
        "higher",
        "card_highlight",
        "tutorial_steps",
        "polished_copywriting",
        "政治敏感",
        "色情低俗",
    ):
        assert required_kw in prompt_text
    for banned_example in (
        "某品牌",
        "凡士林",
        "微信号",
        "QQ号",
        "0.01元",
        "可乐",
        "10.9",
        "8.9",
        "2元",
        "红包",
        "拍两件",
        "15元",
        "百亿补贴",
        "淘金币",
    ):
        assert banned_example not in prompt_text

    db, raw_repo, user_repo, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽"},
            {
                "uid": "u_bob",
                "phone": "13900139000",
                "nickname": "阿博",
                "auth_status": {"taobao": {"is_authorized": True}, "pdd": {"is_authorized": False}},
                "reliability_score": 25,
            },
        ],
        image_dir=tmp_path / "imgs",
    )
    # 新注册用户默认写入 reliability_score=20
    reg_user = user_repo.register_or_login("13700137000", "secret123", nickname="新人")
    assert reg_user["reliability_score"] == 20

    proof_img = _make_png_b64(320, 240)
    proof_img_alt = _make_png_b64(320, 240, color=(40, 120, 200, 255))

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

    # 无硬编码三平台限制：未来新增平台只要商品服务核实通过即可自然入池，且默认绑定用户可靠分=20
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
    assert doc_ext["reliability_score"] == 20
    assert "source_is_reliable" not in doc_ext
    assert doc_ext["clean_stage"] == CLEAN_STAGE_NEED_USER_AUDIT
    assert len(doc_ext["converted_links"]) == 1
    assert any(t.get("link_id") == "L2" for t in doc_ext["ai_stage1_result"]["extracted_targets"])
    pending_view = svc.get_my_submissions("u_alice")["data"]["items"][0]
    assert pending_view["state_label"] == "人工审查中"
    assert "人工审查" in pending_view["reason"]

    # 同一用户同一商品存在 pending 爆料时禁止并发重复提交（409 duplicate_pending_submission）
    with pytest.raises(DealSubmitError) as e_dup_pend:
        svc.submit_user_deal(
            uid="u_alice",
            platform="vip",
            product_id="vip_9001",
            deal_price=28.9,
            tutorial_text="重复提交同一待审商品",
            images_base64=[proof_img_alt],
        )
    assert e_dup_pend.value.status_code == 409
    assert e_dup_pend.value.error_code == "duplicate_pending_submission"

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

    # 原图复制校验：若新图与当前正式表/详情页在架或最近失效好价图完全一致，入口直接拒绝（不误杀用户自己曾被驳回的稿件图）
    ps.get_goods_detail.return_value = {
        **_sample_detail_product(plat="jd", pid="jd_copy_guard"),
        "data": {
            **_sample_detail_product(plat="jd", pid="jd_copy_guard")["data"],
            "deal_tip": {"is_deal_tip": False, "share_images": list(doc_ext["tutorial_images"])},
        },
    }
    with pytest.raises(DealSubmitError) as e_dup_img:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="jd_copy_guard",
            deal_price=25.9,
            tutorial_text="复制已有截图测试",
            images_base64=[proof_img],
        )
    assert e_dup_img.value.status_code == 400
    assert e_dup_img.value.error_code == "duplicate_deal_image"

    # 用户自身被驳回的旧稿件截图不在正式表/deal_tip 中，可自由改图或原图重提
    raw_repo.col.insert_one({
        "source_site": USER_SUBMIT_SITE,
        "source_item_id": "us_rejected_old",
        "submitted_by_uid": "u_alice",
        "resolved_platform": "jd",
        "resolved_product_id": "jd_rej_resubmit",
        "tutorial_images": list(doc_ext["tutorial_images"]),
        "clean_status": "cleaned_failed",
        "first_seen_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        "publish_ts": datetime.datetime.now(datetime.timezone.utc).timestamp(),
    })
    ps.get_goods_detail.return_value = _sample_detail_product(plat="jd", pid="jd_rej_resubmit")
    # 此时 u_alice 今日已累计：vip_9001(1) + us_rejected_old(2 -> 已达未授权上限 2 条)，先验证被拒后的第 3 条直接触发未授权每日 2 条配额上限
    with pytest.raises(DealSubmitError) as e_unauth_lim:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="jd_rej_resubmit",
            deal_price=25.9,
            tutorial_text="重提被拒稿件",
            images_base64=[proof_img],
        )
    assert e_unauth_lim.value.status_code == 429
    assert e_unauth_lim.value.error_code == "submit_daily_limit_unauth"

    # 若将 us_rejected_old 改为昨日（跨天），则今日仅 1 条，重提被拒旧图可以顺利通过且配额恰好达 2/天
    raw_repo.col.update_many(
        {"source_item_id": "us_rejected_old"},
        {"$set": {"first_seen_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=2)}},
    )
    res_rej_retry = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="jd_rej_resubmit",
        deal_price=25.9,
        tutorial_text="修正后重提原图",
        images_base64=[proof_img],
    )
    assert res_rej_retry["status"] == "success"
    assert USER_SUBMIT_DAILY_LIMIT_UNAUTHORIZED == 2

    # 已授权用户 u_bob（淘宝已授权）每日可提交 20 条，且绑定其用户表自定义 reliability_score=25
    for i in range(USER_SUBMIT_DAILY_LIMIT_AUTHORIZED):
        ps.get_goods_detail.return_value = _sample_detail_product(pid=f"p_bob_{i}")
        res_bob = svc.submit_user_deal(
            uid="u_bob",
            platform="jd",
            product_id=f"p_bob_{i}",
            deal_price=29.9,
            tutorial_text=f"授权用户下单实付29.9 {i}",
            images_base64=[proof_img_alt],
        )
        assert res_bob["status"] == "success"
        if i == 0:
            bob_doc = raw_repo.col.find_one({"source_item_id": res_bob["data"]["tip_id"]})
            assert bob_doc["reliability_score"] == 25

    ps.get_goods_detail.return_value = _sample_detail_product(pid="p_bob_over")
    with pytest.raises(DealSubmitError) as e_bob_lim:
        svc.submit_user_deal(
            uid="u_bob",
            platform="jd",
            product_id="p_bob_over",
            deal_price=29.9,
            tutorial_text="超过20条上限",
            images_base64=[proof_img_alt],
        )
    assert e_bob_lim.value.status_code == 429
    assert e_bob_lim.value.error_code == "submit_daily_limit_reached"


def test_worker_single_stage_skips_ai2_with_clean_copy(tmp_path):
    db, raw_repo, _, ps, svc = _make_harness(image_dir=tmp_path / "imgs")

    # 1. 提交含链接文案与官方优惠券：全程单阶段低档多模态审核与展示规整，彻底跳过冗余 AI-2 调用
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
    ai2_Spy = MagicMock(side_effect=AssertionError("user_submit must skip process_ai2_unit!"))
    cleaner.process_ai2_unit = ai2_Spy
    audit_calls: List[Dict[str, Any]] = []
    w_ok = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            price_comparison="cheaper",
            clean_title="维达超韧抽纸",
            price_logic="领券减25",
            card_highlight="到手24.9元",
            coupon_label="50减25优惠券",
            tutorial_steps=["1. 先在上方【领券】处领取50减25券，链接 https://u.jd.com/temp123 自动剥离", "2. 下单实付24.9元"],
            polished_copywriting="先点 https://u.jd.com/temp123 领50减25券再下单",
            capture_calls=audit_calls,
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    out_ok = w_ok.run_once()
    assert out_ok["published"] == 1
    assert ai2_Spy.call_count == 0
    assert audit_calls[0]["preset_model_group"] == "low"

    formal_doc = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
    assert formal_doc["deal_info"]["reliability_score"] == 20
    assert "source_is_reliable" not in formal_doc["deal_info"]
    assert "quality_score" not in formal_doc["deal_info"]
    assert formal_doc["deal_info"]["price_is_reliable"] is True
    assert formal_doc["deal_info"]["coupon_links"][0]["url"] == "https://coupon.m.jd.com/coupons/show.action?key=c1&roleId=999"
    assert formal_doc["deal_info"]["coupon_links"][0]["label"] == "50减25优惠券"
    assert "https://" not in formal_doc["deal_info"]["tutorial_text"]
    assert "https://" not in formal_doc["deal_info"]["original_copywriting"]

    # 2. 附加优惠券经 convert_single_target 指向异品时，Step A 直接拦截并展示友好原因
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
    audit_never: List[Dict[str, Any]] = []
    w_bad_cpn = DealSubmitWorker(
        svc,
        cleaner=cleaner_bad,
        ai_generator=_make_audit_gen(capture_calls=audit_never),
    )
    out_bad = w_bad_cpn.run_once()
    assert out_bad["coupon_failed"] == 1
    assert out_bad["rejected"] == 1
    assert audit_never == []
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
            "reliability_score": 20,
            "deal_price": 32.9,
            "price_logic": "旧券后价",
            "card_highlight": "32.9元",
            "tutorial_steps": ["1. 原旧攻略步骤"],
            "original_copywriting": "原在架详细文案说明",
            "user_share_images": [ex_local_url, "https://img.example.test/existing_remote.jpg"],
            "updated_at": now,
        },
    }
    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽", "auth_status": {"taobao": {"is_authorized": True}}},
            {"uid": "u_bob", "phone": "13900139000", "nickname": "阿博", "auth_status": {"pdd": {"is_authorized": True}}},
        ],
        initial_products=[existing_prod],
        image_dir=img_dir,
    )

    # 1. 不相关图文驳回：验证不动正式表、临时目录已释放
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
        deal_price=32.9,
        tutorial_text="详情页直接下单32.9元",
        images_base64=[_make_png_b64(400, 300)],
    )
    keep_img_id = raw_repo.col.find_one({"source_item_id": res_keep["data"]["tip_id"]})["tutorial_images"][0].rsplit("/", 1)[-1]
    assert keep_img_id == rej_img_id  # 同内容截图复用同一指纹文件名
    w_keep = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, price_comparison="same", comparison_reason="现有在架价格与步骤均更优"),
        remote_image_fetcher=lambda _u: _make_png_bytes(160, 160),
    )
    assert w_keep.run_once()["skipped_existing"] == 1
    assert svc.get_image_file_path(keep_img_id) is not None
    assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["user_stats"] == {"buyable": 1, "unbuyable": 6}
    dup_item = svc.get_my_submissions("u_alice")["data"]["items"][0]
    assert dup_item["tip_id"] == res_keep["data"]["tip_id"]
    assert dup_item["state"] == "duplicate"
    assert dup_item["state_label"] == "已有同价好价"

    # 3. 验证全图文载荷 + 单阶段断点续跑（user_audit_result.passed=True 续跑绝不重调模型）+ 原子防并发替换
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

    w_win = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            price_comparison="cheaper",
            comparison_reason="新到手价实质更低",
            clean_title="维达抽纸",
            price_logic="新补贴券",
            card_highlight="24.9元",
            tutorial_steps=["1. 领新券拍下24.9元"],
            capture_calls=win_calls,
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(160, 160),
    )
    # 模拟审核刚写下 passed=True 后中断，第二轮从断点续跑直接发布且不再调用 AI
    win_doc = raw_repo.col.find_one({"source_item_id": win_tid})
    assert w_win._audit_single_doc(raw_repo, w_win._get_audit_ai_client(), win_doc) == "ready"
    assert len(win_calls) == 1
    assert win_calls[0]["preset_model_group"] == "low"
    assert win_calls[0]["file_names"] == ["official_1.jpg", "new_1.jpg", "existing_1.jpg", "existing_2.jpg"]
    assert win_calls[0]["parent_dir"].exists() is False

    prompt_sent = win_calls[0]["prompt"]
    payload_str = prompt_sent.split("待处理结构化输入数据", 1)[1].split("\n", 1)[1].lstrip("= \n")
    payload_obj = json.loads(payload_str)
    assert payload_obj["need_price_comparison"] is True
    assert payload_obj["official_product"]["image_attachment_indices"] == [1]
    assert payload_obj["new_submission"]["image_attachment_indices"] == [2]
    assert payload_obj["existing_deal"]["image_attachment_indices"] == [3, 4]
    assert payload_obj["existing_deal"]["original_copywriting"] == "原在架详细文案说明"
    assert payload_obj["existing_deal"]["reliability_score"] == 20

    out_r2 = w_win.run_once()
    assert out_r2["published"] == 1
    assert len(win_calls) == 1
    updated_prod = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
    assert updated_prod["deal_info"]["source_item_id"] == win_tid
    assert updated_prod["deal_info"]["deal_price"] == 24.9
    assert updated_prod["deal_info"]["reliability_score"] == 20
    assert updated_prod["user_stats"] == {"buyable": 0, "unbuyable": 0}
    assert "_verified_replace_source_item_id" not in updated_prod

    # 4. 并发竞态保护：若正式表已被更高可信度 other_tip_2(reliability_score=30) 抢先更新，旧对比标记 old_tip_1 不得绕过保护
    raw_repo.formal_col.docs[0]["deal_info"]["source_item_id"] = "other_tip_2"
    raw_repo.formal_col.docs[0]["deal_info"]["reliability_score"] = 30
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
                "reliability_score": 20,
                "deal_price": 19.9,
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
        images_base64=[_make_png_b64(360, 280, color=(200, 40, 40, 255))],
    )
    tip_a = res_a["data"]["tip_id"]
    w_a = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            price_comparison="cheaper",
            clean_title="维达抽纸",
            price_logic="券后",
            card_highlight="到手29.9元",
            tutorial_steps=["1. 下单29.9元"],
        ),
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
        images_base64=[_make_png_b64(360, 280, color=(40, 120, 200, 255))],
    )
    tip_b = res_b["data"]["tip_id"]
    w_b = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            price_comparison="cheaper",
            comparison_reason="价格更优且步骤清晰",
            clean_title="维达抽纸",
            price_logic="叠加补贴",
            card_highlight="到手23.9元",
            tutorial_steps=["1. 叠加补贴拍23.9元"],
        ),
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
        images_base64=[_make_png_b64(360, 280, color=(40, 180, 90, 255))],
    )
    tip_c = res_c["data"]["tip_id"]
    w_c = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, price_comparison="same", comparison_reason="现有在架已有同价截图凭证"),
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
    w1 = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, price_comparison="cheaper"),
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


def test_unified_reliability_tiers_6h_protection_and_cache_invalidation(tmp_path):
    """验证跨级覆盖(差>=10)、低可靠零成本前置跳过、同级6小时涨价保护与满6小时纠偏放行、以及即时缓存失效。"""
    from cache import BOOM_SORT_CACHE, cache_deal_tip, clear_cached_deal_tip, get_cached_deal_tip

    db, raw_repo, _, _, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽", "auth_status": {"taobao": {"is_authorized": True}}},
            {"uid": "u_low", "phone": "13800138001", "nickname": "低分号", "reliability_score": 10, "auth_status": {"taobao": {"is_authorized": True}}},
            {"uid": "u_trusted", "phone": "13800138002", "nickname": "核心号", "reliability_score": 30, "auth_status": {"taobao": {"is_authorized": True}}},
        ],
        initial_products=[
            {
                "platform": "jd",
                "product_id": "1001001",
                "goods_id": "1001001",
                "name": "维达抽纸",
                "price": 59.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": "guangdiu",
                    "source_item_id": "gd_10",
                    "reliability_score": 10,
                    "deal_price": 19.9,
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=1),
                },
            }
        ],
        image_dir=tmp_path / "imgs",
    )

    clear_cached_deal_tip()
    BOOM_SORT_CACHE.clear()
    try:
        cache_deal_tip("jd", "1001001", "missing", None)
        cache_deal_tip("jd", "9999999", "ok", {"deal_price": 88.0})
        BOOM_SORT_CACHE.set(("all", "desc"), [{"product_id": "1001001"}])

        # 1) 跨级覆盖 (20 vs 10)：即使价格高于普通源，仍立即无条件替换并失效缓存
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=28.0,
            tutorial_text="领券拍下实付28元",
            images_base64=[_make_png_b64(360, 260, color=(10, 10, 200, 255))],
        )
        cross_calls: List[Dict[str, Any]] = []
        w_cross = DealSubmitWorker(
            svc,
            ai_generator=_make_audit_gen(price_comparison="none", capture_calls=cross_calls),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        )
        assert w_cross.run_once()["published"] == 1
        assert len(cross_calls) == 1
        formal = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
        assert formal["deal_info"]["reliability_score"] == 20
        assert formal["deal_info"]["deal_price"] == 28.0
        assert get_cached_deal_tip("jd", "1001001") is None
        assert BOOM_SORT_CACHE.get(("all", "desc")) is None
        assert get_cached_deal_tip("jd", "9999999") == ("ok", {"deal_price": 88.0})

        # 2) 低可靠投稿 (10 vs 20)：零成本前置拦截，连多模态 AI 都不调用
        res_low = svc.submit_user_deal(
            uid="u_low",
            platform="jd",
            product_id="1001001",
            deal_price=15.0,
            tutorial_text="低可靠投稿尝试覆盖",
            images_base64=[_make_png_b64(360, 260, color=(20, 200, 10, 255))],
        )
        low_calls: List[Dict[str, Any]] = []
        w_low = DealSubmitWorker(
            svc,
            ai_generator=_make_audit_gen(capture_calls=low_calls),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        )
        assert w_low.run_once()["skipped_existing"] == 1
        assert low_calls == []
        assert raw_repo.col.find_one({"source_item_id": res_low["data"]["tip_id"]})["clean_error_code"] == OK_EXISTING_DEAL_BETTER

        # 3) 同级 (20 vs 20) 在 6 小时保护期内涨价 (age=2h)：拦截并友好提示保护期
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        raw_repo.formal_col.update_many(
            {"platform": "jd", "product_id": "1001001"},
            {"$set": {"deal_info.updated_at": now_utc - datetime.timedelta(hours=2)}},
        )
        res_prot = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=35.0,
            tutorial_text="券失效现价35",
            images_base64=[_make_png_b64(360, 260, color=(200, 20, 20, 255))],
        )
        w_prot = DealSubmitWorker(
            svc,
            ai_generator=_make_audit_gen(price_comparison="higher", comparison_reason="新截图实付35高于原28"),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        )
        assert w_prot.run_once()["skipped_existing"] == 1
        prot_view = svc.get_my_submissions("u_alice")["data"]["items"][0]
        assert prot_view["tip_id"] == res_prot["data"]["tip_id"]
        assert prot_view["state_label"] == "未替换在架好价"
        assert "6小时" in prot_view["reason"]

        # 4) 同级 (20 vs 20) 满 6 小时保护期 (age=7h) 纠偏涨价：放行替换并获奖励资格
        raw_repo.formal_col.update_many(
            {"platform": "jd", "product_id": "1001001"},
            {"$set": {"deal_info.updated_at": now_utc - datetime.timedelta(hours=7)}},
        )
        res_corr = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=35.0,
            tutorial_text="原券已无，最新真实价35元",
            images_base64=[_make_png_b64(360, 260, color=(150, 80, 20, 255))],
        )
        assert DealSubmitWorker(
            svc,
            ai_generator=_make_audit_gen(price_comparison="higher", comparison_reason="原券失效最新实证35"),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        ).run_once()["published"] == 1
        assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == res_corr["data"]["tip_id"]

        # 5) 同级 (20 vs 20) 在 6 小时保护期内明显更低价：立即替换
        raw_repo.formal_col.update_many(
            {"platform": "jd", "product_id": "1001001"},
            {"$set": {"deal_info.updated_at": now_utc - datetime.timedelta(hours=1)}},
        )
        res_cheap = svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=21.0,
            tutorial_text="新神券实付21元",
            images_base64=[_make_png_b64(360, 260, color=(80, 150, 20, 255))],
        )
        assert DealSubmitWorker(
            svc,
            ai_generator=_make_audit_gen(price_comparison="cheaper", comparison_reason="实付21明显更低"),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        ).run_once()["published"] == 1
        assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == res_cheap["data"]["tip_id"]

        # 6) 跨级 (30 vs 20) 核心号即使不低价也立即压制替换
        res_admin = svc.submit_user_deal(
            uid="u_trusted",
            platform="jd",
            product_id="1001001",
            deal_price=22.0,
            tutorial_text="核心号实拍更新",
            images_base64=[_make_png_b64(360, 260, color=(80, 20, 150, 255))],
        )
        assert DealSubmitWorker(
            svc,
            ai_generator=_make_audit_gen(price_comparison="none"),
            remote_image_fetcher=lambda _u: _make_png_bytes(),
        ).run_once()["published"] == 1
        assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["source_item_id"] == res_admin["data"]["tip_id"]
        assert raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})["deal_info"]["reliability_score"] == 30
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

    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=1)
    round1_audits: List[Dict[str, Any]] = []
    w1 = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(is_valid=True, price_comparison="cheaper", capture_calls=round1_audits),
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
            price_comparison="same",
            comparison_reason="已有同价在架凭证",
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
                "reliability_score": 10,
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
                "reliability_score": 10,
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
                "reliability_score": 20,
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
                "reliability_score": 20,
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
                    "reliability_score": 20,
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
                    "reliability_score": 20,
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
                    "reliability_score": 20,
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
                    "reliability_score": 10,
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
                    "reliability_score": 20,
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


def test_evidence_fail_closed_and_incidental_replacement_trace(tmp_path):
    """验证 Fail-Closed 证据硬门禁，以及无需新表即可顺手留存替代方向与对比快照。"""
    db, raw_repo, _, _, svc = _make_harness(
        initial_users=[{"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽", "auth_status": {"taobao": {"is_authorized": True}}}],
        image_dir=tmp_path / "imgs",
        initial_products=[
            {
                "platform": "jd",
                "product_id": "1001001",
                "goods_id": "1001001",
                "name": "维达抽纸",
                "price": 59.9,
                "deal_info": {
                    "is_deal_tip": True,
                    "source_site": "guangdiu",
                    "source_item_id": "old_gd_1",
                    "reliability_score": 10,
                    "deal_price": 19.9,
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=1),
                },
            }
        ],
    )
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=1)

    # 先放一条在架旧爆料 A（在 raw 中存在，与 formal 中的在架 deal 对应）
    raw_repo.col.docs.append(
        {
            "source_site": "guangdiu",
            "source_item_id": "old_gd_1",
            "resolved_platform": "jd",
            "resolved_product_id": "1001001",
            "clean_status": "cleaned_success",
            "clean_stage": "done",
            "first_seen_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=3),
            "display_started_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=3),
        }
    )

    # 1) 模型缺 evidence_supports_claims 布尔字段 -> Fail-Closed 返回 ok=False（走重试）
    from services.deal_submit_worker import _evaluate_audit_payload
    from services.deal_cleaning.ai_client import DealAIClient

    bad_ai = DealAIClient(
        ai_caller=lambda **_kw: {
            "status": "✅ 成功",
            "content": json.dumps(
                {"is_content_valid": True, "price_comparison": "cheaper", "comparison_reason": "缺证据字段"},
                ensure_ascii=False,
            ),
        }
    )
    res_missing = _evaluate_audit_payload(bad_ai, {"new_submission": {}}, existing_deal=None, image_paths=[])
    assert res_missing["ok"] is False

    # 2) 模型口头返回 is_content_valid=True 但 evidence_supports_claims=False -> 代码门禁强制拒绝并记录对比信息
    res_bad = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=15.9,
        tutorial_text="实付15.9元",
        images_base64=[_make_png_b64(320, 240, color=(20, 120, 200, 255))],
    )
    tid_bad = res_bad["data"]["tip_id"]
    worker_bad = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            evidence_supports_claims=False,
            reject_reason="",
            price_comparison="none",
            observed_image_evidence="截图仅展示19.9元，未见15.9元抵扣凭证",
            comparison_reason="图片仅展示19.9元且缺少额外优惠凭证",
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    assert worker_bad.run_once()["rejected"] == 1
    doc_bad = raw_repo.col.find_one({"source_item_id": tid_bad})
    assert doc_bad["clean_status"] == "cleaned_failed"
    assert doc_bad["user_audit_result"]["passed"] is False
    assert doc_bad["user_audit_result"]["evidence_supports_claims"] is False
    assert doc_bad["user_audit_result"]["compared_existing_site"] == "guangdiu"
    assert doc_bad["user_audit_result"]["compared_existing_item_id"] == "old_gd_1"
    assert doc_bad["user_audit_result"]["compared_existing_price"] == 19.9
    assert "19.9元" in doc_bad["user_audit_result"]["observed_image_evidence"]

    # 3) 证据成立并替换旧爆料 old_gd_1 -> 双向顺手记录 replaced_by_* 与 replaces_* + replaces_price
    res_good = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=15.9,
        tutorial_text="截图见15.9元结算页",
        images_base64=[_make_png_b64(320, 240, color=(40, 180, 90, 255))],
    )
    tid_good = res_good["data"]["tip_id"]
    worker_good = DealSubmitWorker(
        svc,
        cleaner=cleaner,
        ai_generator=_make_audit_gen(
            is_valid=True,
            evidence_supports_claims=True,
            price_comparison="none",
            observed_image_evidence="结算页实付15.9元",
            comparison_reason="证据充分且跨级替换在架旧爆料",
        ),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    )
    assert worker_good.run_once()["published"] == 1

    old_after = raw_repo.col.find_one({"source_site": "guangdiu", "source_item_id": "old_gd_1"})
    new_after = raw_repo.col.find_one({"source_item_id": tid_good})
    assert old_after["replaced_by_site"] == USER_SUBMIT_SITE
    assert old_after["replaced_by_item_id"] == tid_good
    assert new_after["replaces_site"] == "guangdiu"
    assert new_after["replaces_item_id"] == "old_gd_1"
    assert new_after["replaces_price"] == 19.9
    assert "display_end_reason" not in old_after

    hist = raw_repo.list_product_deal_history("jd", "1001001")
    hist_by_id = {h["source_item_id"]: h for h in hist}
    assert hist_by_id[tid_good]["replaces_item_id"] == "old_gd_1"
    assert hist_by_id[tid_good]["replaces_price"] == 19.9
    assert hist_by_id[tid_good]["user_audit_result"]["observed_image_evidence"] == "结算页实付15.9元"
    assert hist_by_id[tid_bad]["user_audit_result"]["compared_existing_item_id"] == "old_gd_1"


def test_official_baseline_virtual_gate_prompt_injection_guard_and_recompressed_image_dedup(tmp_path):
    """验证空位虚拟官方基线拦截原价占位、防注入完整度 Fail-Closed 门禁及 JPEG 二次重编码防盗图不误杀。"""
    from PIL import ImageDraw
    from services.deal_submit_worker import _evaluate_audit_payload
    from services.deal_cleaning.ai_client import DealAIClient

    prompt_text = Path(PROMPT_USER_AUDIT_PATH).read_text(encoding="utf-8")
    assert "不可信" in prompt_text
    assert "official_baseline" in prompt_text

    # 1) 空位虚拟官方基线：30分高信誉账号与20分账号面对无好价商品，同价/更高价均被拦截为「未达优惠门槛」，实质更低价才可发布且不写虚假替换史
    db, raw_repo, _, ps, svc = _make_harness(
        initial_users=[
            {"uid": "u_alice", "phone": "13800138000", "nickname": "阿丽", "auth_status": {"taobao": {"is_authorized": True}}, "reliability_score": 20},
            {"uid": "u_vip", "phone": "13900139000", "nickname": "达人", "auth_status": {"taobao": {"is_authorized": True}}, "reliability_score": 30},
        ],
        initial_products=[
            {"platform": "jd", "product_id": "1001001", "goods_id": "1001001", "name": "维达抽纸", "price": 59.9},
        ],
        image_dir=tmp_path / "imgs",
    )
    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=1)

    res_same = svc.submit_user_deal(
        uid="u_alice", platform="jd", product_id="1001001", deal_price=59.9,
        tutorial_text="原价59.9直接买", images_base64=[_make_png_b64(320, 240, color=(100, 100, 100, 255))],
    )
    same_calls: List[Dict[str, Any]] = []
    out_same = DealSubmitWorker(
        svc, cleaner=cleaner,
        ai_generator=_make_audit_gen(price_comparison="same", comparison_reason="与官方在售价相同", capture_calls=same_calls),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    ).run_once()
    assert out_same["skipped_existing"] == 1
    assert len(same_calls) == 1
    assert '"source_site": "official_baseline"' in same_calls[0]["prompt"]
    assert raw_repo.formal_col.find_one({"product_id": "1001001"}).get("deal_info") is None
    view_same = svc.get_my_submissions("u_alice")["data"]["items"][0]
    assert view_same["tip_id"] == res_same["data"]["tip_id"]
    assert view_same["state"] == "duplicate"
    assert view_same["state_label"] == "未达优惠门槛"
    assert "官方在售价" in view_same["reason"]

    # 30分达人在空位报高于官方价也不能绕过
    svc.submit_user_deal(
        uid="u_vip", platform="jd", product_id="1001001", deal_price=65.0,
        tutorial_text="65元高于官价", images_base64=[_make_png_b64(320, 240, color=(120, 90, 90, 255))],
    )
    out_vip_high = DealSubmitWorker(
        svc, cleaner=cleaner,
        ai_generator=_make_audit_gen(price_comparison="higher", comparison_reason="高于官方在售价"),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    ).run_once()
    assert out_vip_high["skipped_existing"] == 1
    assert raw_repo.formal_col.find_one({"product_id": "1001001"}).get("deal_info") is None

    # 降价超过5%且>=1元则替换虚拟基线成功上架，且不写出虚假 replaces_* 历史
    res_lower = svc.submit_user_deal(
        uid="u_alice", platform="jd", product_id="1001001", deal_price=39.9,
        tutorial_text="领20券后实付39.9元", images_base64=[_make_png_b64(320, 240, color=(60, 160, 90, 255))],
    )
    tid_lower = res_lower["data"]["tip_id"]
    assert DealSubmitWorker(
        svc, cleaner=cleaner,
        ai_generator=_make_audit_gen(price_comparison="cheaper", comparison_reason="低于官方价33%"),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    ).run_once()["published"] == 1
    doc_lower_raw = raw_repo.col.find_one({"source_item_id": tid_lower})
    assert "replaces_item_id" not in doc_lower_raw
    assert raw_repo.formal_col.find_one({"product_id": "1001001"})["deal_info"]["source_item_id"] == tid_lower

    # 2) JPEG 二次有损重编码及转存 PNG 再上传均命中近原图去重；同模板改写价格数字的诚实更新不被误杀
    # 先构造一张带纹理和文字的真实截图上架正式表
    base_im = Image.new("RGB", (480, 640), (245, 246, 248))
    d1 = ImageDraw.Draw(base_im)
    d1.rectangle([20, 40, 460, 200], fill=(255, 255, 255), outline=(210, 210, 215))
    d1.text((40, 80), "Product Order Preview", fill=(20, 20, 20))
    d1.text((40, 140), "TOTAL: 44.00", fill=(220, 30, 30))
    buf_orig = io.BytesIO()
    base_im.save(buf_orig, format="JPEG", quality=85)
    orig_b64 = "data:image/jpeg;base64," + base64.b64encode(buf_orig.getvalue()).decode("ascii")

    ps.get_goods_detail.return_value = _sample_detail_product(plat="jd", pid="1009999", price=59.9)
    sub_orig = svc.submit_user_deal(
        uid="u_alice", platform="jd", product_id="1009999", deal_price=44.0,
        tutorial_text="原实拍44元", images_base64=[orig_b64],
    )
    assert DealSubmitWorker(
        svc, cleaner=cleaner,
        ai_generator=_make_audit_gen(price_comparison="cheaper"),
        remote_image_fetcher=lambda _u: _make_png_bytes(),
    ).run_once()["published"] == 1

    # 把硬盘里存好的 .jpg 读出来，模拟用户右键下载后经浏览器 JPEG 二次重编码 / 转存 PNG 再重传 -> Hash 不同但仍被拦截
    formal_img_url = raw_repo.formal_col.find_one({"product_id": "1009999"})["deal_info"]["user_share_images"][0]
    stored_jpg_path = svc.get_image_file_path(formal_img_url.rsplit("/", 1)[-1])
    stored_bytes = stored_jpg_path.read_bytes()
    recompressed_b64 = "data:image/jpeg;base64," + base64.b64encode(
        normalize_and_compress_deal_image(base64.b64encode(stored_bytes).decode("ascii"))
    ).decode("ascii")
    png_reexport_buf = io.BytesIO()
    Image.open(stored_jpg_path).save(png_reexport_buf, format="PNG")
    png_reexport_b64 = "data:image/png;base64," + base64.b64encode(png_reexport_buf.getvalue()).decode("ascii")

    for dup_payload in (recompressed_b64, png_reexport_b64):
        with pytest.raises(DealSubmitError) as e_recomp_dup:
            svc.submit_user_deal(
                uid="u_vip", platform="jd", product_id="1009999", deal_price=43.0,
                tutorial_text="二次压缩复用旧图", images_base64=[dup_payload],
            )
        assert e_recomp_dup.value.error_code == "duplicate_deal_image"

    # 同版式界面仅真实改动价格区域（如 44.00 -> 39.00，足够文字差分像素）绝不误杀
    changed_im = base_im.copy()
    d2 = ImageDraw.Draw(changed_im)
    d2.rectangle([38, 136, 220, 170], fill=(255, 255, 255))
    d2.text((40, 140), "NEW TOTAL: 39.00", fill=(220, 30, 30))
    buf_changed = io.BytesIO()
    changed_im.save(buf_changed, format="JPEG", quality=85)
    changed_b64 = "data:image/jpeg;base64," + base64.b64encode(buf_changed.getvalue()).decode("ascii")
    res_honest_diff = svc.submit_user_deal(
        uid="u_vip", platform="jd", product_id="1009999", deal_price=39.0,
        tutorial_text="新低价39元更新", images_base64=[changed_b64],
    )
    assert res_honest_diff["status"] == "success"

    # 3) 防提示词注入 Fail-Closed：模型被文本注入诱导返回 passed + 短证据 + 空步骤和空正文 -> 代码强制拒绝该结果
    hijacked_ai = DealAIClient(
        ai_caller=lambda **_kw: {
            "status": "✅ 成功",
            "content": json.dumps({
                "observed_image_evidence": "实付1元",
                "evidence_supports_claims": True,
                "is_content_valid": True,
                "content_reject_reason": "",
                "price_comparison": "cheaper",
                "tutorial_steps": [],
                "polished_copywriting": "",
            }, ensure_ascii=False),
        }
    )
    hijack_res = _evaluate_audit_payload(
        hijacked_ai, {"new_submission": {}}, existing_deal=None, image_paths=[],
    )
    assert hijack_res["ok"] is False
    assert "缺少完整的视觉证据与规范文案" in hijack_res["error"]
