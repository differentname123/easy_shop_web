# -*- coding: utf-8 -*-
"""
Meilisearch 全量数据同步工具
用于初始化构建索引、全量导入 MongoDB products 商品数据以及重建索引。

使用示例：
  python mobile_web/scripts/sync_meilisearch.py
  python mobile_web/scripts/sync_meilisearch.py --reset
  python mobile_web/scripts/sync_meilisearch.py --batch-size 1000
"""
import sys
import os
import argparse
from pathlib import Path

# 确保脚本能正确定位并导入 mobile_web 模块
SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from infrastructure.logging_utils import configure_console_encoding
from infrastructure.mongo import gen_db_object
from infrastructure.search_meili import (
    get_meili_client,
    is_meili_available,
    ensure_index_configured,
    sync_all_products,
    INDEX_NAME,
)


def main():
    configure_console_encoding()
    parser = argparse.ArgumentParser(description="Meilisearch 全量商品索引同步脚本")
    parser.add_argument("--batch-size", type=int, default=2000, help="每批次推送文档数量 (默认 2000)")
    parser.add_argument("--reset", action="store_true", help="清空并重新创建索引")
    args = parser.parse_args()

    print("==================================================")
    print("🚀 启动 Meilisearch 商品索引同步程序")
    print("==================================================")

    if not is_meili_available(force_check=True):
        print("❌ 错误：无法连接到 Meilisearch 实例！")
        print("请确认 Meilisearch 是否已启动（例如: docker ps 查看 meilisearch 容器状态）")
        print("以及 config.local.json 中的 meilisearch_url 和 meilisearch_key 是否正确。")
        sys.exit(1)

    client = get_meili_client()
    if args.reset:
        print(f"⚠️ 正在清空旧索引 [{INDEX_NAME}]...")
        try:
            client.delete_index(INDEX_NAME)
            print(f"✅ 旧索引 [{INDEX_NAME}] 已删除")
        except Exception as e:
            print(f"ℹ️ 索引未存在或删除略过: {e}")

    print("🔧 正在初始化与校验索引配置（搜索、过滤与排序属性）...")
    if not ensure_index_configured():
        print("❌ 配置索引失败，请检查 Meilisearch 权限与日志。")
        sys.exit(1)

    print("🔌 正在连接本地 MongoDB 数据库...")
    mongo = gen_db_object()
    db = mongo.db

    def on_progress(synced, total):
        pct = (synced / total * 100) if total > 0 else 100
        print(f"进度: {synced}/{total} ({pct:.1f}%)")

    print(f"📦 开始同步商品数据（批次大小: {args.batch_size}）...")
    try:
        synced_count = sync_all_products(db, batch_size=args.batch_size, progress_cb=on_progress)
        print("==================================================")
        print(f"🎉 同步完成！共成功同步 {synced_count} 条商品到 Meilisearch。")
        print("==================================================")
    except Exception as e:
        print(f"❌ 同步过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if callable(getattr(mongo, "close", None)):
            mongo.close()


if __name__ == "__main__":
    main()
