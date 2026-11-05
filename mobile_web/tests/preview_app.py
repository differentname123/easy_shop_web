# -*- coding: utf-8 -*-
"""Browser-only assembly: real routes/services/Vue, in-memory platform/user/order boundaries.

Never deploy this entry point. No alliance credentials, Mongo, or external HTTP are used.
"""
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse, Response
from tests.offline_support import BLOCKED_CALLS, install_offline_environment

install_offline_environment()

from adapters import register_adapter
from adapters.base import BasePlatformAdapter
from adapters.pdd_adapter import PddAdapter
from adapters.tb_adapter import TaobaoAdapter

PRODUCTS = []
for platform, adapter_type in (("pdd", PddAdapter), ("taobao", TaobaoAdapter)):
    for index in range(1, 10):
        PRODUCTS.append({
            "platform": platform, "platform_name": adapter_type.platform_name,
            "goods_id": str(10000 + index) if platform == "pdd" or index != 1 else "",
            "product_id": str(10000 + index) if platform == "pdd" or index != 1 else "",
            "goods_sign": f"{platform.upper()}_SMOKE_SIGN_{index}",
            "name": f"{adapter_type.platform_name}回归测试商品 {index} · 家庭日用精选",
            "desc": "离线回归数据，不是真实商品、真实淘口令或返利承诺。",
            "price": 25.0 + index, "original_price": 30.0 + index, "coupon_amount": 5.0,
            "saved_price": 5.0, "has_coupon": True, "commission_rate": 10.0,
            "commission_amount": round(2.6 + index / 10, 2), "image_url": "/test-product.svg",
            "mall_name": "离线测试店铺", "sales_tip": "100+", "category_name": "日用",
            "carousel_images": ["/test-product.svg", "/test-product.svg?second=1"],
            "specs": [{"label": "数据来源", "value": "离线内存替身"}],
            "service_guarantees": [], "detail_tags": [],
            "product_url": "/detail?" + urlencode({"platform": platform, "goods_sign": f"{platform.upper()}_SMOKE_SIGN_{index}"}),
        })


def catalog_result(items, page=1, page_size=20, offset=None):
    start = (page - 1) * page_size if offset is None else offset
    end = min(start + page_size, len(items))
    return {"status": "success", "data": deepcopy(items[start:end]), "total_count": len(items),
            "has_more": end < len(items), "page": page, "page_size": page_size, "next_offset": end}


class MemoryProducts:
    def __init__(self):
        self.promotion_links = {}
        self.cache_calls = {"reads": 0, "hits": 0, "writes": 0}

    def get_product_detail(self, key, platform="pdd", key_type=None):
        return next((deepcopy(item) for item in PRODUCTS if item["platform"] == platform and
                     key and key in (item["goods_id"], item["goods_sign"])), None)

    def get_product_identity(self, goods_sign, platform="pdd"):
        item = self.get_product_detail(goods_sign, platform)
        return {"platform": platform, "product_id": item["goods_id"], "goods_sign": goods_sign} if item else None

    def upsert_home_products(self, items, **kwargs): return len(items)
    def upsert_product_detail(self, item): return True
    def query_boom_products(self, platform="all", keyword=None, page=1, page_size=20, **kwargs):
        items = [item for item in PRODUCTS if (platform == "all" or item["platform"] == platform)
                 and (not keyword or keyword in item["name"])]
        return catalog_result(items, page, page_size)

    def get_categories(self, **kwargs): return ["日用"]

    def _promotion_key(self, platform, goods_id=None, goods_sign=None, goods_key=None):
        key = goods_sign or goods_id or goods_key
        item = self.get_product_detail(key, platform)
        return platform, item["goods_sign"] if item else key

    def get_promotion_link_cache(self, platform, goods_id=None, goods_sign=None, goods_key=None):
        self.cache_calls["reads"] += 1
        key = self._promotion_key(platform, goods_id, goods_sign, goods_key)
        saved = self.promotion_links.get(key)
        if not saved or saved["_expires_at"] <= time.time():
            self.promotion_links.pop(key, None)
            return None
        self.cache_calls["hits"] += 1
        return {**deepcopy(saved), "from_cache": True}

    def save_promotion_link_cache(self, platform, data, goods_id=None, goods_sign=None, goods_key=None, expires_at=None):
        if not expires_at or expires_at <= time.time():
            return False
        self.cache_calls["writes"] += 1
        key = self._promotion_key(platform, goods_id, goods_sign, goods_key)
        self.promotion_links[key] = {**deepcopy(data), "_expires_at": expires_at}
        return True


class MemoryUsers:
    def __init__(self):
        self.profiles = {}
        self.authorized = set()

    def register_or_login(self, phone, password, nickname=None, invite_code=None):
        uid = "u_12345678" if phone.endswith("0000") else "u_87654321"
        self.profiles.setdefault(uid, {
            "uid": uid, "phone": phone, "nickname": nickname or "离线测试用户",
            "avatar": "", "invite_code": "SMOKE01", "balance": 0, "total_rebate": 0, "pending_rebate": 0,
            "auth_status": {"pdd": {"is_authorized": False, "bind": 0}, "taobao": {"is_authorized": False, "bind": 0}},
        })
        return deepcopy(self.profiles[uid])

    def get_user_by_uid(self, uid): return deepcopy(self.profiles.get(uid))

    def update_auth_status(self, uid, platform, is_authorized, bind, extra=None):
        profile = self.profiles.get(uid)
        if not profile: return False
        profile["auth_status"][platform] = {
            "is_authorized": is_authorized, "bind": bind, "updated_at": datetime.now(timezone.utc), **(extra or {}),
        }
        return True

    def get_uid_by_platform_identity(self, platform, identity):
        for uid, profile in self.profiles.items():
            saved = profile["auth_status"].get(platform, {})
            if saved.get("is_authorized") and identity and all(saved.get("identity", {}).get(key) == value for key, value in identity.items()):
                return uid
        return None


class MemoryOrders:
    def __init__(self):
        self.orders = []
        for platform in ("pdd", "taobao"):
            for uid in ("u_12345678", "u_87654321"):
                for index, state in enumerate(("paid", "settled", "invalid", "refund"), 1):
                    item = next(item for item in PRODUCTS if item["platform"] == platform)
                    self.orders.append({
                        "platform": platform, "promoter_uid": uid, "order_sn": f"{uid}_SMOKE_ORDER_{index}",
                        "goods_id": item["goods_id"], "goods_name": item["name"], "goods_thumbnail_url": "/test-product.svg",
                        "goods_quantity": 1, "order_amount_yuan": 26.0, "promotion_amount_yuan": 2.7,
                        "settled_promotion_amount_yuan": 2.5 if state == "settled" else None,
                        "order_status_desc": {"paid": "已支付（预估）", "settled": "联盟结算预估" if platform == "taobao" else "平台已结算",
                                              "invalid": "订单失效（非退款）", "refund": "已退款"}[state],
                        "order_pay_time": int(time.time()) - index * 3600, "order_pay_time_str": "离线测试时间",
                        "is_refund": state == "refund", "is_invalid": state in ("invalid", "refund"), "is_settled": state == "settled",
                    })

    def upsert_orders(self, items, **kwargs):
        for item in items:
            self.orders = [old for old in self.orders if (old["platform"], old["order_sn"]) != (item["platform"], item["order_sn"])]
            self.orders.append(deepcopy(item))
        return len(items)

    def assign_unmatched_orders(self, platform, identity, uid): return 0

    def query_user_orders(self, platform="pdd", promoter_uid="", start_timestamp=None, **kwargs):
        items = [item for item in self.orders if item["platform"] == platform and item["promoter_uid"] == promoter_uid
                 and (not start_timestamp or item["order_pay_time"] >= start_timestamp)]
        valid = [item for item in items if not item["is_invalid"] and not item["is_refund"]]
        refunds = [item for item in items if item["is_refund"]]
        return {"total_count": len(items), "total_commission": round(sum(item["promotion_amount_yuan"] for item in valid), 2),
                "settled_commission": round(sum(item["settled_promotion_amount_yuan"] or 0 for item in valid if item["is_settled"]), 2),
                "refund_commission": round(sum(item["promotion_amount_yuan"] for item in refunds), 2), "refund_count": len(refunds),
                "order_list": deepcopy(items)}


class MemorySync:
    def __init__(self): self.states = {}
    def get_state(self, platform): return deepcopy(self.states.get(platform))
    def save_state(self, platform, state): self.states[platform] = deepcopy(state); return True


products, users, orders, sync = MemoryProducts(), MemoryUsers(), MemoryOrders(), MemorySync()


class PreviewAdapter(BasePlatformAdapter):
    active = True
    capabilities = {name: True for name in ("recommend", "search", "detail", "promotion", "authorization", "orders")}

    def __init__(self, platform, adapter_type):
        self.platform_code, self.platform_name = platform, adapter_type.platform_name
        self.channels = deepcopy(adapter_type.channels)  # Exactly the production adapter's two authoritative ranks.
        self.required_identity_fields = ("relation_id",) if platform == "taobao" else ()
        self.requires_authorized_promotion = True
        self.calls = {"anonymous_converts": 0, "personal_converts": 0, "authority_checks": 0, "authority_urls": 0}

    def get_recommend_goods(self, offset=0, limit=20, cursor=None, **kwargs):
        if isinstance(cursor, str) and cursor: offset = int(json.loads(cursor).get("offset", offset))
        elif isinstance(cursor, dict): offset = int(cursor.get("offset", offset))
        return catalog_result([item for item in PRODUCTS if item["platform"] == self.platform_code], offset=offset, page_size=limit)

    def search_goods(self, keyword, page=1, page_size=20, **kwargs):
        return catalog_result([item for item in PRODUCTS if item["platform"] == self.platform_code and keyword in item["name"]], page, page_size)

    def get_goods_detail(self, goods_id_or_sign="", goods_id=None, goods_sign=None, **kwargs):
        return {"status": "success", "data": products.get_product_detail(goods_sign or goods_id or goods_id_or_sign, self.platform_code)}

    def convert_promotion_url(self, goods_id_or_sign="", uid=None, identity=None, **kwargs):
        self.calls["personal_converts" if uid else "anonymous_converts"] += 1
        if uid and any(not (identity or {}).get(field) for field in self.required_identity_fields):
            return {"status": "error", "error_code": "AUTH_REQUIRED", "need_auth": True, "auth_platform": self.platform_code,
                    "auth_msg": "离线平台渠道身份尚未确认，请完成模拟授权后重新生成链接。"}
        item = products.get_product_detail(kwargs.get("goods_sign") or kwargs.get("goods_id") or goods_id_or_sign, self.platform_code)
        if not item:
            return {"status": "error", "error_msg": "离线测试商品不存在"}
        # Personal calls rely on the service's stored authorization decision, never a second remote check here.
        link = item["product_url"] + "&" + urlencode({"promotion": "personal" if uid else "general"})
        return {"status": "success", "final_link": link, "schema_url": "", "is_fallback": False,
                "cacheable": True, "expires_at": time.time() + 3600,
                "promotion_token": "￥OFFLINE_TEST_ONLY￥" if self.platform_code == "taobao" else ""}

    def check_authority(self, uid):
        self.calls["authority_checks"] += 1
        authorized = (uid, self.platform_code) in users.authorized
        identity = {"relation_id": f"OFFLINE_RELATION_{uid}", "external_id": uid} if authorized and self.platform_code == "taobao" else {}
        return {"status": "success", "is_authorized": authorized, "bind": 1 if authorized else 0, "identity": identity}

    def generate_authority_url(self, uid, **kwargs):
        self.calls["authority_urls"] += 1
        url = "/test/authorize?" + urlencode({"platform": self.platform_code, "uid": uid})
        return {"status": "success", "auth_url": url, "short_url": url}

    def fetch_increment_orders(self, **kwargs):
        return {"status": "success", "order_list": [], "total_count": 0, "has_more": False}


preview_adapters = {platform: PreviewAdapter(platform, adapter_type)
                    for platform, adapter_type in (("pdd", PddAdapter), ("taobao", TaobaoAdapter))}
for platform, adapter in preview_adapters.items():
    register_adapter(platform, adapter)

from main import create_app
from api import home, goods, promote, user, order

for service in (home.product_service, goods.product_service, promote.promote_service): service.repo = products
user.user_service.repo = users
user.user_service.order_repo = orders
promote.promote_service.user_service = user.user_service
order.order_service.repo = orders
order.order_service.sync_repo = sync
order.order_service.user_repo = users
app = create_app()


@app.get("/test/offline-status")
def offline_status():
    return {"status": "error" if BLOCKED_CALLS else "ok", "blocked_external_calls": list(BLOCKED_CALLS),
            "platform_calls": {platform: dict(adapter.calls) for platform, adapter in preview_adapters.items()},
            "public_cache_calls": dict(products.cache_calls)}


@app.get("/test/authorize")
def authorize(platform: str, uid: str):
    if platform not in ("pdd", "taobao") or uid not in users.profiles:
        return HTMLResponse("离线授权参数无效", status_code=400)
    users.authorized.add((uid, platform))
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>离线模拟授权</title>'
                        '<body><h2>离线模拟授权已完成</h2><p>请返回原页面重新校验授权；不会连接真实平台。</p>'
                        '<a href="/?tab=user&platform=' + platform + '">返回账户</a></body></html>')


@app.get("/test-product.svg")
def product_image():
    return Response('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480">'
                    '<rect width="480" height="480" fill="#fff1e8"/>'
                    '<rect x="110" y="80" width="260" height="280" rx="24" fill="#f5ad80"/>'
                    '<text x="240" y="420" text-anchor="middle" font-size="24" fill="#743f21">OFFLINE TEST</text>'
                    '</svg>', media_type="image/svg+xml")
