# -*- coding: utf-8 -*-
"""
存量京东榜单/有券商品一次性低耗补券回填脚本：
- 默认优先扫描存量从未拉取过详情的京东实时榜单商品 (channel_type in [1, 5], detail_updated_at is None)，
  通过 JdAdapter.enrich_detail_resilient 单次补齐官方隐藏优惠券链接 (coupon_url) 与券后价；
- 遇并发线报调度器折淘客 60s 峰值窗口限流时，自动退避重试，遇明确下架 (PRODUCT_OFF_PROMOTION) 自动清理；
- 已查过的商品写入 detail_updated_at，幂等可中断续跑，不会重复消耗配额。

用法示例：
  python scripts/backfill_jd_coupons.py --only-recommend --limit 50
  python scripts/backfill_jd_coupons.py --only-recommend
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(SCRIPT_DIR)
REPO_ROOT = os.path.dirname(APP_ROOT)
for _p in (REPO_ROOT, APP_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from adapters import get_adapter  # noqa: E402
from adapters.jd_adapter import _is_encrypted_jd_item_id  # noqa: E402
from models.product import utc_millis  # noqa: E402
from repositories.product_repo import ProductRepository  # noqa: E402


BACKOFF_SCHEDULE = (0.0, 4.0, 8.0, 15.0, 20.0)


def build_query(*, only_recommend: bool, include_missing_coupon_url: bool) -> dict:
    base = {
        "platform": "jd",
        "$or": [
            {"coupon_url": {"$exists": False}},
            {"coupon_url": ""},
            {"coupon_url": None},
        ],
    }
    if only_recommend and not include_missing_coupon_url:
        return {
            "$and": [
                base,
                {"channel_type": {"$in": [1, 5]}},
                {"$or": [{"detail_updated_at": {"$exists": False}}, {"detail_updated_at": None}]},
            ]
        }
    if only_recommend:
        return {
            "$and": [
                base,
                {"channel_type": {"$in": [1, 5]}},
                {
                    "$or": [
                        {"detail_updated_at": {"$exists": False}},
                        {"detail_updated_at": None},
                        {"coupon_amount": {"$gt": 0}},
                    ]
                },
            ]
        }
    return {
        "$and": [
            base,
            {
                "$or": [
                    {"detail_updated_at": {"$exists": False}},
                    {"detail_updated_at": None},
                    {"coupon_amount": {"$gt": 0}},
                ]
            },
        ]
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="京东存量商品官方优惠券链接低耗回填脚本")
    parser.add_argument("--only-recommend", action="store_true", help="仅处理京东榜单商品 (channel_type in [1, 5])")
    parser.add_argument("--include-missing-coupon-url", action="store_true", help="同时补齐已有 coupon_amount>0 但缺 coupon_url 的历史记录")
    parser.add_argument("--limit", type=int, default=0, help="最多处理的商品条数 (0 表示不限)")
    parser.add_argument("--pace", type=float, default=0.35, help="单条请求间隔秒数 (默认 0.35s)")
    parser.add_argument("--dry-run", action="store_true", help="仅统计待处理数量，不发起上游请求或写库")
    args = parser.parse_args(argv)

    repo = ProductRepository()
    adapter = get_adapter("jd")
    query = build_query(
        only_recommend=args.only_recommend,
        include_missing_coupon_url=args.include_missing_coupon_url,
    )

    projection = {
        "_id": 0,
        "product_id": 1,
        "goods_id": 1,
        "goods_sign": 1,
        "name": 1,
        "coupon_amount": 1,
        "detail_updated_at": 1,
    }
    cursor = repo.col.find(query, projection).sort([("updated_at", -1)])
    if args.limit and args.limit > 0:
        cursor = cursor.limit(args.limit)
    candidates = [
        doc for doc in cursor
        if isinstance(doc, dict) and _is_encrypted_jd_item_id(doc.get("goods_sign"))
    ]

    print(f"[backfill_jd_coupons] 匹配待补券加密ID商品: {len(candidates)} 条 (dry_run={args.dry_run})")
    if args.dry_run or not candidates:
        return 0

    stats = {
        "checked": 0,
        "found_coupon": 0,
        "zero_coupon": 0,
        "off_promotion_deleted": 0,
        "rate_limited": 0,
        "other_error": 0,
    }
    started = time.perf_counter()

    for idx, doc in enumerate(candidates, start=1):
        sign = str(doc.get("goods_sign") or "").strip()
        gid = str(doc.get("product_id") or doc.get("goods_id") or "").strip()
        res = None
        for delay in BACKOFF_SCHEDULE:
            if delay > 0:
                time.sleep(delay)
            res = adapter.enrich_detail_resilient(sign, goods_id=gid or None, goods_sign=sign)
            if res.get("error_code") != "RATE_LIMIT":
                break

        stats["checked"] += 1
        if isinstance(res, dict) and res.get("status") == "success" and isinstance(res.get("data"), dict):
            data = dict(res["data"])
            now = utc_millis(datetime.fromtimestamp(time.time(), timezone.utc))
            repo.upsert_product_detail(dict(data, detail_updated_at=now))
            if data.get("coupon_url") or float(data.get("coupon_amount") or 0.0) > 0:
                stats["found_coupon"] += 1
            else:
                stats["zero_coupon"] += 1
        elif isinstance(res, dict) and res.get("error_code") == "PRODUCT_OFF_PROMOTION":
            repo.delete_product("jd", goods_id=gid or None, goods_sign=sign)
            stats["off_promotion_deleted"] += 1
        elif isinstance(res, dict) and res.get("error_code") == "RATE_LIMIT":
            stats["rate_limited"] += 1
        else:
            stats["other_error"] += 1

        if idx % 25 == 0 or idx == len(candidates):
            elapsed = time.perf_counter() - started
            print(
                f"  [{idx}/{len(candidates)}] 耗时 {elapsed:.1f}s | "
                f"发现优惠券:{stats['found_coupon']} 无券:{stats['zero_coupon']} "
                f"下架清理:{stats['off_promotion_deleted']} 限流未决:{stats['rate_limited']} 其他失败:{stats['other_error']}"
            )
        if args.pace > 0 and idx < len(candidates):
            time.sleep(args.pace)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
