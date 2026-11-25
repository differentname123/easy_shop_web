# -*- coding: utf-8 -*-
"""详情页锚定用户爆料核心流程、图片压缩与流水线集成测试。"""
from __future__ import annotations

import base64
import copy
import datetime
import io
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
import main as main_module
from auth import create_access_token
from main import create_app
from models.deal_constants import (
    CLEAN_STAGE_NEED_AI2,
    USER_SUBMIT_SITE,
)
from models.product import get_source_site_name
from repositories.deal_raw_pool_repo import DealRawPoolRepository
from repositories.product_repo import ProductRepository, upsert_cleaned_products_to_formal_table
from repositories.user_repo import UserRepository
from services.deal_pool_cleaner import DealPoolCleaner, _commit_batch_results_to_db
from services.deal_submit_service import (
    USER_SUBMIT_MAX_IMAGE_DIM,
    USER_SUBMIT_MAX_PER_WINDOW,
    DealSubmitError,
    DealSubmitService,
    cleanup_expired_deal_submit_images,
    normalize_and_compress_deal_image,
)


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
            "platform_name": "京东" if plat == "jd" else ("淘宝" if plat == "taobao" else "拼多多"),
            "product_id": pid,
            "goods_id": pid,
            "goods_sign": f"sign_{pid}",
            "name": "维达抽纸超韧3层130抽24包家用整箱装",
            "price": price,
            "original_price": 79.9,
            "coupon_amount": 10.0,
            "mall_name": "维达京东自营旗舰店",
            "brand": "维达",
            "category": "家居日用",
            "image_url": "https://img.example.test/vinda.jpg",
            "carousel_images": ["https://img.example.test/vinda.jpg"],
            "sales_tip": "10万+",
            "product_url": f"https://item.jd.com/{pid}.html" if plat == "jd" else f"https://item.taobao.com/item.htm?id={pid}",
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


def _make_png_b64(width: int = 2000, height: int = 1000, color=(220, 60, 40, 255)) -> str:
    img = Image.new("RGBA", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def test_core_input_validation_and_rate_limit():
    assert get_source_site_name("user_submit") == "用户爆料"
    db, raw_repo, _, ps, svc = _make_harness()

    with pytest.raises(DealSubmitError) as e_auth:
        svc.submit_user_deal(uid="", platform="jd", product_id="1001001", deal_price=29.9, tutorial_text="先领券下单")
    assert e_auth.value.status_code == 401

    with pytest.raises(DealSubmitError) as e_pid:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="", deal_price=29.9, tutorial_text="先领券下单")
    assert e_pid.value.error_code == "missing_product_identity"

    with pytest.raises(DealSubmitError) as e_price:
        svc.submit_user_deal(uid="u_alice", platform="jd", product_id="1001001", deal_price=0, tutorial_text="先领券下单")
    assert e_price.value.error_code == "invalid_deal_price"

    with pytest.raises(DealSubmitError) as e_ext:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=29.9,
            tutorial_text="点这个站外链接 https://evil-phishing.com/steal",
        )
    assert e_ext.value.error_code == "unsupported_external_url"

    with pytest.raises(DealSubmitError) as e_cpn_domain:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=29.9,
            tutorial_text="领券再买很划算",
            coupon_input="https://evil-phishing.com/coupon/1",
        )
    assert e_cpn_domain.value.error_code == "unsupported_coupon_domain"

    with pytest.raises(DealSubmitError) as e_cpn_plat:
        svc.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=29.9,
            tutorial_text="领券再买很划算",
            coupon_input="https://m.tb.cn/h.abc123",
        )
    assert e_cpn_plat.value.error_code == "coupon_platform_mismatch"

    # 每用户 10 分钟最多 USER_SUBMIT_MAX_PER_WINDOW 次
    for i in range(USER_SUBMIT_MAX_PER_WINDOW):
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


def test_pipeline_integration_with_existing_reliable_protection_and_coupon():
    db, raw_repo, _, ps, svc = _make_harness()

    # 1. 无额外券直入 CLEAN_STAGE_NEED_AI2 -> 按现有流水线进 formal table
    res_ok = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001001",
        deal_price=29.9,
        tutorial_text="详情页领满50减20券下单29.9元",
    )
    assert res_ok["status"] == "success"
    doc_ok = raw_repo.col.find_one({"source_item_id": res_ok["data"]["tip_id"]})
    assert doc_ok["clean_stage"] == CLEAN_STAGE_NEED_AI2
    assert doc_ok["source_is_reliable"] is False

    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达超韧抽纸",
            "deal_price": 29.9,
            "price_logic": "领50-20券",
            "card_highlight": "1.25元/包",
            "coupon_links": [],
            "tutorial_steps": ["1. 领券拍1件实付29.9元"],
            "polished_copywriting": "领券实付29.9元",
            "quality_score": 82,
            "score_breakdown": {"match_score": 26, "completeness_score": 22, "price_clarity_score": 18, "tutorial_and_image_score": 16},
            "price_is_reliable": True,
            "price_reliability_reason": "券面额明确",
        }
    }
    claimed = raw_repo.claim_stage_docs(CLEAN_STAGE_NEED_AI2, limit=10)
    outcomes, _ = cleaner.process_ai2_unit(claimed, raw_col=raw_repo.col, quiet_sub_logs=True)
    assert outcomes[0]["status"] == "success"
    _commit_batch_results_to_db(outcomes, raw_repo.col, raw_repo.formal_col, cycle_num=1)

    formal_prod = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
    assert formal_prod is not None
    assert formal_prod["deal_info"]["is_deal_tip"] is True
    assert formal_prod["deal_info"]["source_site"] == USER_SUBMIT_SITE
    assert formal_prod["deal_info"]["source_is_reliable"] is False
    assert svc.get_my_submissions("u_alice")["data"]["items"][0]["state"] == "published"

    # 2. 原先的高可靠爬虫好价保护机制保持正常生效，不会被 user_submit 覆写
    raw_repo.formal_col.docs[0]["deal_info"]["source_is_reliable"] = True
    raw_repo.formal_col.docs[0]["deal_info"]["deal_price"] = 35.0
    prot_res = upsert_cleaned_products_to_formal_table(
        raw_repo.formal_col,
        [{
            "platform": "jd",
            "product_id": "1001001",
            "goods_id": "1001001",
            "name": "维达",
            "price": 59.9,
            "deal_info": {"is_deal_tip": True, "source_site": USER_SUBMIT_SITE, "source_is_reliable": False, "deal_price": 19.0, "quality_score": 90},
        }],
    )
    assert prot_res["protected_high_score"] == 1
    assert raw_repo.formal_col.docs[0]["deal_info"]["deal_price"] == 35.0
    assert formal_prod.get("default_promotion") is None

    # 3. 统一走标准 CLEAN_STAGE_NEED_AI2：合法官方券直通无改动的 AI-2，误贴他品链接直接拦截
    ps.get_goods_detail.return_value = _sample_detail_product(pid="1001002")
    res_cpn = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="1001002",
        deal_price=24.9,
        tutorial_text="先领优惠券再购买",
        coupon_input="https://coupon.m.jd.com/coupons/show.action?key=c1&roleId=999",
    )
    doc_cpn = raw_repo.col.find_one({"source_item_id": res_cpn["data"]["tip_id"]})
    assert doc_cpn["clean_stage"] == CLEAN_STAGE_NEED_AI2
    l2 = [c for c in doc_cpn["converted_links"] if c["link_id"] == "L2"][0]
    assert l2["convert_status"] == "coupon_or_activity"
    assert l2["my_promo_link"] == "https://coupon.m.jd.com/coupons/show.action?key=c1&roleId=999"

    claimed_cpn = raw_repo.claim_stage_docs(CLEAN_STAGE_NEED_AI2, limit=10)
    cleaner.run_ai_stage2_batch = lambda stage2_inputs, quiet_sub_logs=False: {
        stage2_inputs[0]["main_tip_id"]: {
            "main_tip_id": stage2_inputs[0]["main_tip_id"],
            "is_valid_deal": True,
            "reject_reason": None,
            "main_link_id": "L1",
            "clean_title": "维达超韧抽纸",
            "deal_price": 24.9,
            "price_logic": "领券减后24.9",
            "card_highlight": "24.9元",
            "coupon_links": [{"link_id": "L2", "label": "商品优惠券"}],
            "tutorial_steps": ["1. 先领券", "2. 下单实付24.9元"],
            "quality_score": 84,
            "price_is_reliable": True,
            "price_reliability_reason": "有券",
        }
    }
    out_cpn, _ = cleaner.process_ai2_unit(claimed_cpn, raw_col=raw_repo.col, quiet_sub_logs=True)
    assert out_cpn[0]["status"] == "success"
    _commit_batch_results_to_db(out_cpn, raw_repo.col, raw_repo.formal_col, cycle_num=2)
    formal_cpn = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001002"})
    assert formal_cpn["deal_info"]["coupon_links"][0]["url"] == "https://coupon.m.jd.com/coupons/show.action?key=c1&roleId=999"

    # 无 canonical_url 的 jd_img_ 商品同样走标准信号入池（不依赖站点特判分支）
    ps.get_goods_detail.return_value = {
        "status": "success",
        "data": {"platform": "jd", "product_id": "jd_img_hash123", "goods_id": "jd_img_hash123", "goods_sign": "E_SIGN_1", "name": "测试纸巾", "price": 39.0},
    }
    res_img_id = svc.submit_user_deal(
        uid="u_alice",
        platform="jd",
        product_id="jd_img_hash123",
        goods_sign="E_SIGN_1",
        deal_price=19.9,
        tutorial_text="下单减20",
    )
    assert raw_repo.col.find_one({"source_item_id": res_img_id["data"]["tip_id"]})["candidate_goods_id"] == "jd_img_hash123"
    ps.get_goods_detail.return_value = _sample_detail_product()

    svc_bad_cpn = DealSubmitService(
        product_service=ps,
        raw_repo=raw_repo,
        user_repo=svc.user_repo,
        coupon_converter=lambda tgt, default_plat="unknown": {
            "link_id": tgt["link_id"],
            "clean_input": tgt["clean_input"],
            "text_role_hint": "coupon_candidate",
            "platform": "jd",
            "convert_status": "product",
            "goods_id": "9999999",
            "my_promo_link": "https://u.jd.com/other",
            "official_product": {"title": "异品", "price": 9.9},
        },
    )
    with pytest.raises(DealSubmitError) as e_cpn_prod:
        svc_bad_cpn.submit_user_deal(
            uid="u_alice",
            platform="jd",
            product_id="1001001",
            deal_price=24.9,
            tutorial_text="先领优惠券再购买",
            coupon_input="https://u.jd.com/other_prod",
        )
    assert e_cpn_prod.value.error_code == "coupon_product_mismatch"


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
    assert res_img["data"]["image_count"] == 1
    raw_doc = raw_repo.col.find_one({"source_item_id": res_img["data"]["tip_id"]})
    img_url = raw_doc["tutorial_images"][0]
    img_id = img_url.rsplit("/", 1)[-1]
    saved_path = svc.get_image_file_path(img_id)
    assert saved_path is not None and saved_path.is_file()

    cleaner = DealPoolCleaner(ai_batch_size=4, convert_max_workers=2)
    captured_cnt = []

    def _fake_ai2(stage2_inputs, quiet_sub_logs=False):
        captured_cnt.append(stage2_inputs[0]["share_image_count"])
        return {
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
                "tutorial_steps": ["1. 下单"],
                "quality_score": 85,
                "price_is_reliable": True,
                "price_reliability_reason": "有效",
            }
        }

    cleaner.run_ai_stage2_batch = _fake_ai2
    claimed = raw_repo.claim_stage_docs(CLEAN_STAGE_NEED_AI2, limit=10)
    outcomes, _ = cleaner.process_ai2_unit(claimed, raw_col=raw_repo.col, quiet_sub_logs=True)
    _commit_batch_results_to_db(outcomes, raw_repo.col, raw_repo.formal_col, cycle_num=1)

    assert captured_cnt == [1]
    formal_doc = raw_repo.formal_col.find_one({"platform": "jd", "product_id": "1001001"})
    assert formal_doc["deal_info"]["user_share_images"] == [img_url]
    assert ProductRepository._extract_deal_tip(formal_doc)["share_images"] == [img_url]
    assert svc.get_my_submissions("u_alice")["data"]["items"][0]["image_count"] == 1

    old_ts = 1000.0
    os.utime(saved_path, (old_ts, old_ts))
    assert cleanup_expired_deal_submit_images(image_dir=img_dir, now=old_ts + 8 * 86400) == 1
    assert not saved_path.exists()


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
