# -*- coding: utf-8 -*-
"""
一次性安全清理本地与云端 MongoDB `products` 集合中的历史导入 `sku_info` 商品数据。

安全保障：
1. 默认 `--dry-run` 只读预览，必须显式传入 `--execute` 才会执行写操作；
2. 严格区分：
   - 纯历史非严选商品（带 `sku_info` 且既无整数 `channel_type` 也无 `deal_info.is_deal_tip == True`）-> 物理删除；
   - 已被现役推荐流/线报池复用的重叠商品（带 `sku_info` 且属于严选可见商品）-> 绝不删除商品，仅 `$unset` 剥离历史 `sku_info` 并触发本地重洗；
3. 严格按「先本地、后云端」顺序执行，防止运行中的云同步任务将本地未删数据重新推回云端。
"""
import argparse
import os
import sys
import urllib.parse
from pathlib import Path

import pymongo

CURRENT_DIR = Path(__file__).resolve().parent
MOBILE_WEB_DIR = CURRENT_DIR.parent
for _p in (str(MOBILE_WEB_DIR), str(CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from infrastructure.settings import get_settings
from repositories.product_repo import ProductRepository
from run_product_cloud_sync_scheduler import resolve_cloud_mongo_target


def build_queries():
    boom_filter = ProductRepository._boom_content_filter()
    pure_historical_sku_query = {
        "sku_info": {"$exists": True},
        "$nor": [
            {"channel_type": {"$type": "int"}},
            {"deal_info.is_deal_tip": True},
        ],
    }
    overlapped_boom_sku_query = {
        "sku_info": {"$exists": True},
        "$or": [
            {"channel_type": {"$type": "int"}},
            {"deal_info.is_deal_tip": True},
        ],
    }
    return boom_filter, pure_historical_sku_query, overlapped_boom_sku_query


def connect_collections(explicit_cloud_uri: str = ""):
    s = get_settings()
    if s.mongo_user and s.mongo_password:
        u = urllib.parse.quote_plus(s.mongo_user)
        p = urllib.parse.quote_plus(s.mongo_password)
        local_uri = f"mongodb://{u}:{p}@{s.mongo_host}:{s.mongo_port}/{s.mongo_auth_source}"
    else:
        local_uri = f"mongodb://{s.mongo_host}:{s.mongo_port}/"

    cloud_uri, cloud_db_name, _, _ = resolve_cloud_mongo_target(explicit_uri=explicit_cloud_uri or None)
    local_client = pymongo.MongoClient(
        local_uri,
        tz_aware=True,
        serverSelectionTimeoutMS=8000,
        socketTimeoutMS=120000,
    )
    cloud_client = pymongo.MongoClient(
        cloud_uri,
        tz_aware=True,
        serverSelectionTimeoutMS=10000,
        socketTimeoutMS=120000,
        compressors="zlib",
    )
    return (
        local_client,
        cloud_client,
        local_client[s.mongo_db_name]["products"],
        cloud_client[cloud_db_name]["products"],
    )


def inspect_collection(label: str, col) -> dict:
    boom_filter, pure_q, overlap_q = build_queries()
    total = col.count_documents({})
    boom_cnt = col.count_documents(boom_filter)
    has_sku = col.count_documents({"sku_info": {"$exists": True}})
    pure_cnt = col.count_documents(pure_q)
    overlap_cnt = col.count_documents(overlap_q)
    boom_in_pure = col.count_documents({"$and": [pure_q, boom_filter]})
    info = {
        "total": total,
        "boom_cnt": boom_cnt,
        "has_sku": has_sku,
        "pure_delete_cnt": pure_cnt,
        "overlap_unset_cnt": overlap_cnt,
        "boom_in_pure_safety_check": boom_in_pure,
    }
    print(
        f"[{label}] total={total:,} | boom_visible={boom_cnt:,} | has_sku={has_sku:,} "
        f"| to_delete_pure_historical={pure_cnt:,} | to_unset_overlapped_boom={overlap_cnt:,} "
        f"| boom_collateral_risk={boom_in_pure}"
    )
    if boom_in_pure != 0:
        raise RuntimeError(f"[{label}] 安全校验未通过：待删除集合中包含 {boom_in_pure} 条严选可见商品！")
    return info


def main():
    from datetime import datetime, timezone

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="清理本地与云端历史导入的 sku_info 商品数据")
    parser.add_argument("--cloud-uri", default=os.environ.get("CLOUD_MONGO_URI") or "")
    parser.add_argument("--execute", action="store_true", help="真正执行删除与字段剥离（默认仅只读预览）")
    args = parser.parse_args()

    local_client, cloud_client, local_col, cloud_col = connect_collections(args.cloud_uri)
    try:
        print("=== 清理前库表核验 ===")
        local_before = inspect_collection("LOCAL_BEFORE", local_col)
        cloud_before = inspect_collection("CLOUD_BEFORE", cloud_col)

        if not args.execute:
            print("\n[DRY-RUN] 当前为只读预览模式，未修改任何数据。传入 --execute 执行清理。")
            return

        _, pure_q, overlap_q = build_queries()
        now = datetime.now(timezone.utc)

        print("\n=== Step 1/2: 正在清理本地库 (LOCAL FIRST) ===")
        l_del = local_col.delete_many(pure_q)
        l_upd = local_col.update_many(
            overlap_q,
            {
                "$unset": {
                    "sku_info": "",
                    "format_sku_prices": "",
                    "format_activity_price": "",
                },
                "$set": {
                    "need_reformat": True,
                    "format_status": "pending",
                    "format_retry_count": 0,
                    "updated_at": now,
                },
            },
        )
        print(f"  本地物理删除历史商品={l_del.deleted_count:,} 条 | 剥离现役重叠商品 sku_info={l_upd.modified_count:,} 条")

        print("\n=== Step 2/2: 正在清理云端库 (CLOUD SECOND) ===")
        c_del = cloud_col.delete_many(pure_q)
        c_upd = cloud_col.update_many(
            overlap_q,
            {"$unset": {"sku_info": "", "format_sku_prices": ""}},
        )
        print(f"  云端物理删除历史商品={c_del.deleted_count:,} 条 | 剥离现役重叠商品 sku_info={c_upd.modified_count:,} 条")

        print("\n=== 清理后库表复检 ===")
        local_after = inspect_collection("LOCAL_AFTER", local_col)
        cloud_after = inspect_collection("CLOUD_AFTER", cloud_col)

        assert local_after["has_sku"] == 0, f"本地仍残留 sku_info: {local_after['has_sku']}"
        assert cloud_after["has_sku"] == 0, f"云端仍残留 sku_info: {cloud_after['has_sku']}"
        print(
            f"\n✅ 清理完成并校验通过！本地严选商品数: {local_before['boom_cnt']:,} -> {local_after['boom_cnt']:,}, "
            f"云端严选商品数: {cloud_before['boom_cnt']:,} -> {cloud_after['boom_cnt']:,}"
        )
    finally:
        local_client.close()
        cloud_client.close()


if __name__ == "__main__":
    main()
