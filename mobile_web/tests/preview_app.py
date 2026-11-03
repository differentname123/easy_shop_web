# -*- coding: utf-8 -*-
"""仅供浏览器回归的离线装配：运行真实路由/服务/页面，不连接联盟和 Mongo。

生产入口仍是 main:app，不能将此测试入口用于部署。
"""
import os
from copy import deepcopy
from datetime import datetime, timezone
from fastapi.responses import Response

from tests.offline_support import BLOCKED_CALLS, install_offline_environment

install_offline_environment()

from main import create_app
from adapters import get_adapter
from api import home, goods, promote, user, order

adapter = get_adapter("pdd")
PRODUCTS = []
for index in range(1, 10):
    raw = {
        "goods_id": str(10000 + index), "goods_sign": f"SMOKE_SIGN_{index}",
        "goods_name": f"回归测试商品 {index} · 家庭日用精选", "goods_desc": "离线回归数据，不是真实商品或返利承诺。",
        "min_group_price_yuan": 30.0 + index, "coupon_discount_yuan": 5.0,
        "has_coupon": True, "promotion_rate": 100, "estimated_commission_yuan": round(2.6 + index / 10, 2),
        "goods_image_url": "/test-product.svg", "mall_name": "离线测试店铺", "sales_tip": "100+",
        "brand_name": "回归测试", "category_name": "日用",
        "_raw_data": {"goods_gallery_urls": ["/test-product.svg", "/test-product.svg?second=1"],
                      "coupon_remain_quantity": 7, "coupon_total_quantity": 10},
    }
    PRODUCTS.append(adapter._normalize_goods(raw))


def catalog_result(items, page=1, page_size=20, offset=None):
    start = (page - 1) * page_size if offset is None else offset
    return {"status": "success", "data": deepcopy(items[start:start + page_size]), "total_count": len(items),
            "has_more": start + page_size < len(items), "page": page, "page_size": page_size}


class MemoryProducts:
    def get_product_detail(self, key, platform="pdd", key_type=None):
        return next((deepcopy(item) for item in PRODUCTS if item["platform"] == platform and
                     key in (item["goods_id"], item["goods_sign"])), None)
    def get_product_identity(self, goods_sign, platform="pdd"):
        item = next((item for item in PRODUCTS if item["platform"] == platform
                     and item["goods_sign"] == goods_sign), None)
        return {"platform": platform, "product_id": item["goods_id"], "goods_sign": goods_sign} if item else None
    def upsert_home_products(self, items, **kwargs): return len(items)
    def upsert_product_detail(self, item): return True
    def query_boom_products(self, platform="all", keyword=None, page=1, page_size=20, **kwargs):
        items = [item for item in PRODUCTS if (platform == "all" or item["platform"] == platform)
                 and (not keyword or keyword in item["name"])]
        return catalog_result(items, page, page_size)
    def get_categories(self, **kwargs): return ["日用"]
    def get_promotion_link_cache(self, **kwargs): return None
    def save_promotion_link_cache(self, **kwargs): return True


class MemoryUsers:
    def __init__(self):
        self.profile = None
    def register_or_login(self, phone, password, nickname=None, invite_code=None):
        self.profile = {"uid": "u_12345678", "phone": phone, "nickname": nickname or "离线测试用户",
                        "avatar": "", "invite_code": "SMOKE01", "balance": 0, "total_rebate": 0, "pending_rebate": 0,
                        "auth_status": {"pdd": {"is_authorized": False, "bind": 0}}}
        return deepcopy(self.profile)
    def get_user_by_uid(self, uid):
        return deepcopy(self.profile) if self.profile and uid == self.profile["uid"] else None
    def update_auth_status(self, uid, platform, is_authorized, bind):
        self.profile["auth_status"][platform] = {"is_authorized": is_authorized, "bind": bind,
                                                 "updated_at": datetime.now(timezone.utc)}
        return True


class MemoryOrders:
    def upsert_orders(self, items, **kwargs): return len(items)
    def query_user_orders(self, **kwargs):
        return {"total_count": 1, "total_commission": 2.7, "settled_commission": 0.0,
                "refund_commission": 0.0, "refund_count": 0, "order_list": [{
                    "order_sn": "SMOKE_ORDER_1", "goods_id": "10001", "goods_name": PRODUCTS[0]["name"],
                    "goods_thumbnail_url": "/test-product.svg", "goods_quantity": 1, "order_amount_yuan": 26.0,
                    "promotion_amount_yuan": 2.7, "order_status_desc": "已支付（离线测试）",
                    "order_pay_time_str": "2026-11-02 12:00", "is_refund": False, "is_settled": False,
                }]}


products = MemoryProducts()
for service in (home.product_service, goods.product_service, promote.promote_service):
    service.repo = products
user.user_service.repo = MemoryUsers()
order.order_service.repo = MemoryOrders()
adapter.warmup = lambda: None
adapter.get_recommend_goods = lambda offset=0, limit=20, **kwargs: catalog_result(PRODUCTS, offset=offset, page_size=limit)
adapter.search_goods = lambda keyword, page=1, page_size=20, **kwargs: catalog_result(
    [item for item in PRODUCTS if keyword in item["name"]], page, page_size)
adapter.get_goods_detail = lambda goods_id_or_sign="", goods_id=None, goods_sign=None, **kwargs: {
    "status": "success", "data": products.get_product_detail(goods_sign or goods_id or goods_id_or_sign)}
adapter.convert_promotion_url = lambda goods_id_or_sign="", **kwargs: adapter._promotion_result(
    f"http://localhost:{os.environ.get('PORT', '8000')}/detail?goods_id=10001", {}, kwargs.get("title", "回归商品"),
    kwargs.get("price"), kwargs.get("coupon_amount"), "离线测试链接")
adapter.check_authority = lambda uid: {"status": "success", "is_authorized": False, "bind": 0}
adapter.generate_authority_url = lambda uid, **kwargs: {"status": "success", "auth_url": "/", "short_url": "/"}
adapter.fetch_increment_orders = lambda **kwargs: {"status": "success", "order_list": [], "total_count": 0, "has_more": False}

app = create_app()


@app.get("/test/offline-status")
def offline_status():
    return {"status": "error" if BLOCKED_CALLS else "ok", "blocked_external_calls": list(BLOCKED_CALLS)}


@app.get("/test-product.svg")
def product_image():
    return Response('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480">'
                    '<rect width="480" height="480" fill="#fff1e8"/>'
                    '<rect x="110" y="80" width="260" height="280" rx="24" fill="#f5ad80"/>'
                    '<text x="240" y="420" text-anchor="middle" font-size="24" fill="#743f21">OFFLINE TEST</text>'
                    '</svg>', media_type="image/svg+xml")
