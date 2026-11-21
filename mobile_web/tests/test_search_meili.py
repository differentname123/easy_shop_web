# -*- coding: utf-8 -*-
"""
Meilisearch 全文搜索引擎客户端与仓储集成单元测试
涵盖主键清洗、文档转换、健康状态缓存、搜索过滤、排序降级与写穿透同步。
"""
import threading
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import datetime

import infrastructure.search_meili as search_meili
from infrastructure.search_meili import (
    MeiliSyncWorker,
    _extract_doc_sync_ts,
    make_meili_doc_id,
    format_meili_doc,
    is_meili_available,
    search_products,
    sync_all_products,
    sync_products_diff,
)
from repositories.product_repo import ProductRepository


class TestSearchMeili(unittest.TestCase):

    def test_make_meili_doc_id_sanitization(self):
        """测试 Meilisearch 主键清洗：仅保留 a-zA-Z0-9-_，非法字符安全转为下划线"""
        self.assertEqual(make_meili_doc_id("pdd", "123456"), "pdd_123456")
        self.assertEqual(make_meili_doc_id("jd", "100.200:300"), "jd_100_200_300")
        self.assertEqual(make_meili_doc_id("taobao", "a/b@c+d"), "taobao_a_b_c_d")
        self.assertEqual(make_meili_doc_id("pdd", "商品123"), "pdd___123")

    def test_format_meili_doc_valid(self):
        """测试正常商品文档转换为 Meilisearch 索引结构"""
        doc = {
            "platform": "pdd",
            "product_id": "P1001",
            "name": "可口可乐 1.25L 大瓶装",
            "brand": "可口可乐",
            "mall_name": "可口可乐自营旗舰店",
            "category": "饮料",
            "price": 45.0,
            "activity_price": 38.5,
            "saved_price": 6.5,
            "is_off_promotion": False,
            "channel_type": 1,
            "deal_info": {"is_deal_tip": True},
            "image_url": "https://example.com/coke.jpg",
            "updated_at": datetime.datetime(2026, 11, 18, 12, 0, 0),
        }
        res = format_meili_doc(doc)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "pdd_P1001")
        self.assertEqual(res["product_id"], "P1001")
        self.assertEqual(res["platform"], "pdd")
        self.assertEqual(res["name"], "可口可乐 1.25L 大瓶装")
        self.assertEqual(res["price"], 45.0)
        self.assertEqual(res["activity_price"], 38.5)
        self.assertEqual(res["saved_price"], 6.5)
        self.assertFalse(res["is_off_promotion"])
        self.assertTrue(res["is_deal_tip"])
        self.assertEqual(res["channel_type"], 1)

    def test_format_meili_doc_invalid(self):
        """测试不合法或缺失关键字段的文档安全返回 None"""
        self.assertIsNone(format_meili_doc(None))
        self.assertIsNone(format_meili_doc({}))
        self.assertIsNone(format_meili_doc({"platform": "pdd"}))  # 无 product_id 与 name
        self.assertIsNone(format_meili_doc({"product_id": "123", "name": ""}))  # 空标题

    @patch("infrastructure.search_meili.get_meili_client")
    def test_is_meili_available_caching(self, mock_get_client):
        """测试健康检测状态缓存，不会对不可用实例产生持续阻塞"""
        mock_client = MagicMock()
        mock_client.health.return_value = {"status": "available"}
        mock_get_client.return_value = mock_client

        # 首次强制检查返回 True
        self.assertTrue(is_meili_available(force_check=True))

        # 客户端异常时
        mock_client.health.side_effect = Exception("Connection refused")
        self.assertFalse(is_meili_available(force_check=True))

    def test_search_products_empty_keyword(self):
        """空关键词直接返回 None，不执行检索"""
        self.assertIsNone(search_products(""))
        self.assertIsNone(search_products("   "))

    @patch("infrastructure.search_meili.is_meili_available", return_value=False)
    def test_search_products_fallback_when_unavailable(self, mock_avail):
        """当 Meilisearch 处于不可用状态时，优雅返回 None 促使上游回退数据库"""
        self.assertIsNone(search_products("康师傅"))

    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_search_products_query_building(self, mock_get_client, mock_avail):
        """测试 search_products 组装 filter 与 sort 参数"""
        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_index.search.return_value = {"hits": [{"platform": "pdd", "product_id": "101"}]}
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        hits = search_products(
            keyword="可乐",
            platform="pdd",
            category="饮料",
            sort_by="price_asc",
            limit=10,
            offset=0,
        )
        self.assertEqual(len(hits), 1)
        mock_index.search.assert_called_once()
        args, kwargs = mock_index.search.call_args
        self.assertEqual(args[0], "可乐")
        params = args[1]
        self.assertIn('platform = "pdd"', params["filter"])
        self.assertIn('category = "饮料"', params["filter"])
        self.assertIn("NOT is_off_promotion = true", params["filter"])
        self.assertEqual(params["sort"], ["activity_price:asc"])
        self.assertEqual(params["limit"], 10)


class TestProductRepoMeiliIntegration(unittest.TestCase):

    def setUp(self):
        self.col = MagicMock()
        self.col.__class__.__name__ = "Collection"  # 模拟持久化集合类型
        self.db = SimpleNamespace(get_collection=lambda name: self.col)
        self.repo = ProductRepository(db_instance=self.db)
        self.repo._force_meili_enabled = True

    @patch("repositories.product_repo.search_products")
    def test_query_boom_products_uses_meili_hits(self, mock_search):
        """测试 query_boom_products 优先采用 Meilisearch 命中并保持相关性顺序"""
        mock_search.return_value = [
            {"platform": "pdd", "product_id": "P1"},
            {"platform": "pdd", "product_id": "P2"},
        ]
        self.col.find.return_value = [
            {"platform": "pdd", "product_id": "P2", "name": "可口可乐 P2", "price": 20.0},
            {"platform": "pdd", "product_id": "P1", "name": "可口可乐 P1", "price": 10.0},
        ]

        res = self.repo.query_boom_products(platform="pdd", keyword="可乐", sort_by="latest")
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["data"]), 2)
        # 结果必须按照 Meilisearch hits 的相关性顺序 [P1, P2] 排布，而非 MongoDB 游标乱序
        self.assertEqual([d["product_id"] for d in res["data"]], ["P1", "P2"])

    @patch("repositories.product_repo.search_products")
    def test_query_boom_products_falls_back_when_meili_returns_none(self, mock_search):
        """当 Meilisearch 返回 None (不可用) 时，优雅回退到 Mongo 游标查询"""
        mock_search.return_value = None
        # Mongo cursor mock
        cursor_mock = MagicMock()
        cursor_mock.sort.return_value = cursor_mock
        cursor_mock.skip.return_value = cursor_mock
        cursor_mock.limit.return_value = cursor_mock
        cursor_mock.max_time_ms.return_value = [
            {"platform": "pdd", "product_id": "FALLBACK_1", "name": "降级商品", "price": 15.0}
        ]
        self.col.find.return_value = cursor_mock

        res = self.repo.query_boom_products(platform="pdd", keyword="可乐", sort_by="latest")
        self.assertEqual(res["status"], "success")
        self.assertEqual([d["product_id"] for d in res["data"]], ["FALLBACK_1"])

    @patch("repositories.product_repo.search_products")
    def test_rank_boom_products_by_cost_performance_empty_hits(self, mock_search):
        """当 Meilisearch 返回 0 命中时，坚守防误杀准则，返回空列表，不模糊兜底错乱商品"""
        mock_search.return_value = []
        res = self.repo.rank_boom_products_by_cost_performance(keyword="完全不存在的商品999")
        self.assertEqual(res, [])

    def test_format_meili_doc_with_deal_info_and_entities(self):
        """测试 format_meili_doc 自动将原始爆料标题与 AI 提取实体打包入 search_keywords"""
        doc = {
            "platform": "pdd",
            "product_id": "P6635",
            "name": "可口可乐多口味装包邮",
            "price": 33.4,
            "deal_info": {
                "is_deal_tip": True,
                "raw_tip_title": "【多多】可口可乐500ml*18瓶混合装",
                "format_info": {
                    "core_entities": [{"name": "碳酸饮料", "score": 10}],
                    "decision_keywords": [{"attribute_name": "品类", "attribute_value": "可乐"}],
                },
            },
        }
        res = format_meili_doc(doc)
        self.assertIsNotNone(res)
        self.assertIn("search_keywords", res)
        # 验证 search_keywords 中包含原始爆料标题与实体词
        self.assertIn("【多多】可口可乐500ml*18瓶混合装", res["search_keywords"])
        self.assertIn("碳酸饮料", res["search_keywords"])
        self.assertIn("可乐", res["search_keywords"])

    @patch("repositories.product_repo.upsert_cleaned_products_to_formal_table")
    def test_repo_upsert_cleaned_deal_products_ssot(self, mock_upsert_deal):
        """测试 ProductRepository 作为单一真源入口统一调度好价商品入库"""
        mock_upsert_deal.return_value = {"upserted": 1, "modified": 0, "matched": 0}
        product_docs = [{"platform": "pdd", "product_id": "P999", "name": "测试商品"}]
        res = self.repo.upsert_cleaned_deal_products(product_docs)
        mock_upsert_deal.assert_called_once_with(self.col, product_docs)
        self.assertEqual(res["upserted"], 1)

    def test_format_meili_doc_sync_ts_deterministic_and_naive_utc(self):
        """sync_ts 确定性取更新/格式化相关 UTC 时间最大值，全缺失时固定为 0"""
        no_ts = format_meili_doc({"platform": "pdd", "product_id": "P0", "name": "无时间戳"})
        self.assertEqual(no_ts["sync_ts"], 0)
        self.assertEqual(no_ts["updated_at"], 0)

        t1 = datetime.datetime(2026, 11, 21, 10, 0, 0)  # naive UTC
        t2 = datetime.datetime(2026, 11, 21, 12, 30, 0, tzinfo=datetime.timezone.utc)
        doc = {
            "platform": "taobao",
            "product_id": "T1",
            "name": "格式更新商品",
            "updated_at": t1,
            "deal_info": {"is_deal_tip": True, "format_updated_at": t2},
        }
        expected_t1 = int(datetime.datetime(2026, 11, 21, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        expected_t2 = int(t2.timestamp())
        res = format_meili_doc(doc)
        self.assertEqual(res["updated_at"], expected_t1)
        self.assertEqual(res["sync_ts"], expected_t2)
        self.assertEqual(_extract_doc_sync_ts(doc), expected_t2)

    @patch("infrastructure.search_meili.ensure_index_configured", return_value=True)
    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_sync_products_diff_bidirectional_upsert_update_skip_and_delete(
        self, mock_get_client, _mock_avail, _mock_cfg
    ):
        """验证单一差量同步能同时完成：跳过一致、补齐缺失、刷新格式化变更、清理下架与幽灵"""
        t_keep = datetime.datetime(2026, 11, 21, 1, 0, 0, tzinfo=datetime.timezone.utc)
        t_new = datetime.datetime(2026, 11, 21, 2, 0, 0, tzinfo=datetime.timezone.utc)
        t_fmt = datetime.datetime(2026, 11, 21, 3, 0, 0, tzinfo=datetime.timezone.utc)
        t_legacy = datetime.datetime(2026, 11, 21, 4, 0, 0, tzinfo=datetime.timezone.utc)

        store = [
            {"platform": "pdd", "product_id": "KEEP_1", "name": "保持商品", "updated_at": t_keep},
            {"platform": "jd", "product_id": "MISSING_2", "name": "新增推荐流", "updated_at": t_new},
            {
                "platform": "taobao",
                "product_id": "UPDATED_3",
                "name": "已重新格式化",
                "updated_at": t_keep,
                "format_updated_at": t_fmt,
                "deal_info": {
                    "is_deal_tip": True,
                    "format_info": {"core_entities": [{"name": "新实体词"}]},
                },
            },
            {"platform": "pdd", "product_id": "LEGACY_4", "name": "旧索引缺sync_ts", "updated_at": t_legacy},
            {"platform": "jd", "product_id": "OFF_5", "name": "已下架商品", "is_off_promotion": True, "updated_at": t_new},
            {"platform": "jd", "product_id": "SUP_6", "name": "已被替代", "superseded_by": "NEW_6", "updated_at": t_new},
        ]

        def fake_find(query=None, projection=None):
            query = query or {}
            out = []
            for d in store:
                if query.get("is_off_promotion") == {"$ne": True} and d.get("is_off_promotion") is True:
                    continue
                if query.get("superseded_by") == {"$exists": False} and "superseded_by" in d:
                    continue
                if "platform" in query and d.get("platform") != query["platform"]:
                    continue
                if "product_id" in query and isinstance(query["product_id"], dict):
                    if d.get("product_id") not in query["product_id"].get("$in", []):
                        continue
                out.append(d)
            return out

        col = MagicMock()
        col.find.side_effect = fake_find
        mongo_db = SimpleNamespace(get_collection=lambda _name: col)

        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        mock_index.get_documents.side_effect = [
            SimpleNamespace(
                results=[
                    SimpleNamespace(id="pdd_KEEP_1", sync_ts=int(t_keep.timestamp())),
                    SimpleNamespace(id="taobao_UPDATED_3", sync_ts=int(t_keep.timestamp())),
                    SimpleNamespace(id="jd_OFF_5", sync_ts=int(t_new.timestamp())),
                ],
                total=5,
            ),
            {
                "results": [
                    {"id": "pdd_LEGACY_4"},  # 旧索引无 sync_ts
                    {"id": "taobao_GHOST"},
                ],
                "total": 5,
            },
        ]

        res = sync_products_diff(mongo_db, page_size=3, batch_size=2, delete_batch_size=1)
        self.assertEqual(res, {"upserted": 3, "deleted": 2, "skipped": 1})

        deleted_ids = {cid for call in mock_index.delete_documents.call_args_list for cid in call.args[0]}
        self.assertEqual(deleted_ids, {"jd_OFF_5", "taobao_GHOST"})

        pushed_docs = {doc["id"]: doc for call in mock_index.add_documents.call_args_list for doc in call.args[0]}
        self.assertEqual(set(pushed_docs.keys()), {"jd_MISSING_2", "taobao_UPDATED_3", "pdd_LEGACY_4"})
        self.assertNotIn("pdd_KEEP_1", pushed_docs)
        self.assertIn("新实体词", pushed_docs["taobao_UPDATED_3"]["search_keywords"])
        self.assertEqual(pushed_docs["taobao_UPDATED_3"]["sync_ts"], int(t_fmt.timestamp()))

    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_sync_products_diff_rebuilds_when_index_not_found(self, mock_get_client, _mock_avail):
        """索引不存在（Meili 重建/清库）时，自动强制建索引并全量补齐"""
        ts = datetime.datetime(2026, 11, 21, 5, 0, 0, tzinfo=datetime.timezone.utc)
        col = MagicMock()
        col.find.return_value = [{"platform": "pdd", "product_id": "NEW_1", "name": "自动建索引商品", "updated_at": ts}]
        mongo_db = SimpleNamespace(get_collection=lambda _name: col)

        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        mock_index.get_raw_info.side_effect = Exception("index_not_found")
        mock_client.create_index.return_value = {"taskUid": 7}
        mock_index.get_documents.side_effect = Exception("index_not_found")

        search_meili._INDEX_CONFIGURED = True
        res = sync_products_diff(mongo_db)
        self.assertEqual(res, {"upserted": 1, "deleted": 0, "skipped": 0})
        mock_client.create_index.assert_called_once()
        mock_client.wait_for_task.assert_called_once_with(7, timeout_in_ms=5000)
        mock_index.add_documents.assert_called_once()
        self.assertEqual(mock_index.add_documents.call_args.args[0][0]["id"], "pdd_NEW_1")

    @patch("infrastructure.search_meili.ensure_index_configured", return_value=True)
    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_sync_products_diff_fail_closed_on_mongo_error_and_incomplete_scan(
        self, mock_get_client, _mock_avail, _mock_cfg
    ):
        """Fail-Closed：Mongo 异常或 Meili 分页未扫描完整时，绝不删除任何文档"""
        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        col = MagicMock()
        col.find.side_effect = RuntimeError("mongo down")
        mongo_db = SimpleNamespace(get_collection=lambda _name: col)
        self.assertEqual(sync_products_diff(mongo_db), {"upserted": 0, "deleted": 0, "skipped": 0})
        mock_index.get_documents.assert_not_called()
        mock_index.delete_documents.assert_not_called()

        col.find.side_effect = None
        col.find.return_value = [{"platform": "pdd", "product_id": "KEEP", "name": "保留"}]
        with patch("infrastructure.search_meili._fetch_meili_sync_map", return_value=({"ghost": 1}, False)):
            res = sync_products_diff(mongo_db)
        self.assertEqual(res, {"upserted": 0, "deleted": 0, "skipped": 0})
        mock_index.delete_documents.assert_not_called()
        mock_index.add_documents.assert_not_called()

    @patch("infrastructure.search_meili.is_meili_available", return_value=False)
    def test_sync_products_diff_unavailable_and_mark_unavailable_resets_config(self, _mock_avail):
        search_meili._INDEX_CONFIGURED = True
        search_meili._mark_unavailable()
        self.assertFalse(search_meili._INDEX_CONFIGURED)

        mongo_db = SimpleNamespace(get_collection=lambda _name: MagicMock())
        self.assertEqual(sync_products_diff(mongo_db), {"upserted": 0, "deleted": 0, "skipped": 0})

    @patch("infrastructure.search_meili.ensure_index_configured", return_value=True)
    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_sync_all_products_pushes_active_and_cleans_stale(self, mock_get_client, _mock_avail, _mock_cfg):
        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        cursor_mock = MagicMock()
        cursor_mock.batch_size.return_value = [
            {"platform": "pdd", "product_id": "A1", "name": "商品A1"},
        ]
        col = MagicMock()
        col.count_documents.return_value = 1
        col.find.return_value = cursor_mock
        mongo_db = SimpleNamespace(get_collection=lambda _name: col)

        mock_index.get_documents.return_value = {
            "results": [{"id": "pdd_A1"}, {"id": "jd_GHOST"}],
            "total": 2,
        }
        synced = sync_all_products(mongo_db, batch_size=100)
        self.assertEqual(synced, 1)
        mock_index.delete_documents.assert_called_once_with(["jd_GHOST"])

    def test_meili_sync_worker_runs_periodically_and_stops_cleanly(self):
        calls = []
        done_two = threading.Event()

        def fake_sync(target, stop_event=None):
            calls.append((target, stop_event))
            if len(calls) >= 2:
                done_two.set()
            return {"upserted": 0, "deleted": 0, "skipped": 0}

        worker = MeiliSyncWorker(lambda: "repo-obj", interval_seconds=0.05, initial_delay_seconds=0.0)
        with patch("infrastructure.search_meili.sync_products_diff", side_effect=fake_sync):
            worker.start()
            self.assertTrue(done_two.wait(timeout=3.0), "MeiliSyncWorker 未能按周期触发同步")
            thread = worker._thread
            worker.stop()
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(t == "repo-obj" for t, _ in calls))


if __name__ == "__main__":
    unittest.main()
