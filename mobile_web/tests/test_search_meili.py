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
    MEILI_SCHEMA_VERSION,
    MeiliSyncWorker,
    _extract_doc_sync_ts,
    ensure_index_configured,
    extract_core_entities,
    make_meili_doc_id,
    format_meili_doc,
    is_meili_available,
    partition_search_candidates,
    search_products,
    select_relevant_candidates,
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
        self.assertFalse(res["is_user_collected"])
        self.assertFalse(res["is_reliable"])
        self.assertEqual(res["channel_type"], 1)
        self.assertEqual(MEILI_SCHEMA_VERSION, 4)
        self.assertTrue(format_meili_doc({
            "platform": "pdd", "product_id": "P_UC", "name": "万能转链品", "is_user_collected": True,
        })["is_user_collected"])

        self.assertTrue(format_meili_doc({
            "platform": "pdd", "product_id": "P_NO_DEAL", "name": "官方商品", "channel_type": 5,
        })["is_reliable"])
        self.assertTrue(format_meili_doc({
            "platform": "jd", "product_id": "J_SRC_REL", "name": "高可靠来源线报",
            "deal_info": {"is_deal_tip": True, "reliability_score": 20, "price_is_reliable": False},
        })["is_reliable"])
        self.assertTrue(format_meili_doc({
            "platform": "jd", "product_id": "J_PRICE_REL", "name": "高可靠价格线报",
            "deal_info": {"is_deal_tip": True, "reliability_score": 10, "price_is_reliable": True},
        })["is_reliable"])

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
        """测试 search_products 组装 filter 与 sort 参数（严格 matchingStrategy=all 且无阈值兜底）"""
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
        args, _kwargs = mock_index.search.call_args
        self.assertEqual(args[0], "可乐")
        params = args[1]
        self.assertIn('platform = "pdd"', params["filter"])
        self.assertIn('category = "饮料"', params["filter"])
        self.assertIn("NOT is_off_promotion = true", params["filter"])
        self.assertEqual(params["sort"], ["is_reliable:desc", "activity_price:asc"])
        self.assertEqual(params["limit"], 10)
        self.assertEqual(params["matchingStrategy"], "all")
        self.assertNotIn("rankingScoreThreshold", params)

    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_search_products_empty_does_not_fallback_to_last_strategy(self, mock_get_client, mock_avail):
        """空命中时绝不放宽为 matchingStrategy=last（防止丢失中文核心中心词）"""
        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_index.search.return_value = {"hits": []}
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        hits = search_products(keyword="鲜牛肉", limit=100, offset=0)
        self.assertEqual(hits, [])
        mock_index.search.assert_called_once()

    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_ensure_index_configured_sets_cjk_no_typo_and_minimal_fields(self, mock_get_client, _mock_avail):
        """验证索引配置关闭中文容错、开启 cmn 分词并剔除 mall_name/category 脏检索字段"""
        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client

        self.assertTrue(ensure_index_configured(force=True))
        mock_index.update_settings.assert_called_once()
        cfg = mock_index.update_settings.call_args.args[0]
        self.assertEqual(cfg["searchableAttributes"], ["core_entity", "core_entity_alias", "brand", "name"])
        self.assertNotIn("mall_name", cfg["searchableAttributes"])
        self.assertNotIn("category", cfg["searchableAttributes"])
        self.assertIn("is_reliable", cfg["filterableAttributes"])
        self.assertIn("is_user_collected", cfg["filterableAttributes"])
        self.assertIn("is_reliable", cfg["sortableAttributes"])
        self.assertEqual(cfg["typoTolerance"], {"enabled": False})
        self.assertEqual(cfg["localizedAttributes"], [{"attributePatterns": ["*"], "locales": ["cmn"]}])
        self.assertIn("鲜牛肉", cfg["synonyms"])


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
        """测试 format_meili_doc 优先提取官方 format_info 本体并支持线报兜底与中文右中心语展开"""
        # 1. 官方顶层 SKU 清洗 + 线报清洗共存：官方为主本体，线报作为别名补充
        doc_official = {
            "platform": "jd",
            "product_id": "J100",
            "name": "大伊 原切牛腱子 4斤",
            "price": 169.9,
            "format_info": {
                "J100_0": {
                    "core_entities": [{"name": "牛腱子", "score": 10}],
                }
            },
            "deal_info": {
                "is_deal_tip": True,
                "raw_tip_title": "169元抢牛腱子",
                "format_info": {
                    "core_entities": [{"name": "牛腱子", "score": 10}, {"name": "生鲜牛肉", "score": 8}],
                },
            },
        }
        res1 = format_meili_doc(doc_official)
        self.assertIsNotNone(res1)
        self.assertEqual(res1["core_entities_raw"], ["牛腱子"])
        self.assertEqual(res1["core_entities_alias_raw"], ["生鲜牛肉"])
        self.assertIn("生鲜牛肉", res1["core_entity_alias"])
        self.assertIn("鲜牛肉", res1["core_entity_alias"])
        self.assertIn("牛肉", res1["core_entity_alias"])
        self.assertEqual(res1["schema_ver"], MEILI_SCHEMA_VERSION)
        self.assertNotIn("search_keywords", res1)

        # 2. 仅有线报清洗数据时安全兜底
        doc_deal_only = {
            "platform": "pdd",
            "product_id": "P6635",
            "name": "可口可乐多口味装包邮",
            "price": 33.4,
            "deal_info": {
                "is_deal_tip": True,
                "format_info": {
                    "core_entities": [
                        {"name": "可口可乐", "score": 10},
                        {"name": "碳酸饮料", "score": 7},
                    ],
                },
            },
        }
        res2 = format_meili_doc(doc_deal_only)
        self.assertEqual(res2["core_entities_raw"], ["可口可乐"])
        self.assertEqual(res2["core_entities_alias_raw"], ["碳酸饮料"])
        self.assertIn("可乐", res2["core_entity"])

        # 3. 无任何清洗字段时不报错且安全返回空本体
        doc_plain = {"platform": "pdd", "product_id": "P_RAW", "name": "未清洗新品牛肉", "format_info": "bad_type"}
        res3 = format_meili_doc(doc_plain)
        self.assertEqual(res3["core_entity"], "")
        self.assertEqual(res3["core_entities_raw"], [])
        self.assertEqual(extract_core_entities(None), ([], []))

    @patch("repositories.product_repo.UpdateOne")
    def test_repo_upsert_cleaned_deal_products_ssot(self, _mock_u1):
        """测试正式商品库好价线报统一入库中枢"""
        from repositories.product_repo import upsert_cleaned_products_to_formal_table
        with patch.object(self.col, "bulk_write", return_value=SimpleNamespace(upserted_count=1, modified_count=0, matched_count=0)):
            product_docs = [{"platform": "pdd", "product_id": "P999", "name": "测试商品", "deal_info": {"is_deal_tip": True, "source_site": "guangdiu", "source_item_id": "gd_1"}}]
            res = upsert_cleaned_products_to_formal_table(self.col, product_docs)
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
                    SimpleNamespace(
                        id="pdd_KEEP_1",
                        sync_ts=int(t_keep.timestamp()),
                        schema_ver=MEILI_SCHEMA_VERSION,
                    ),
                    SimpleNamespace(
                        id="taobao_UPDATED_3",
                        sync_ts=int(t_keep.timestamp()),
                        schema_ver=MEILI_SCHEMA_VERSION,
                    ),
                    SimpleNamespace(
                        id="jd_OFF_5",
                        sync_ts=int(t_new.timestamp()),
                        schema_ver=MEILI_SCHEMA_VERSION,
                    ),
                ],
                total=5,
            ),
            {
                "results": [
                    {"id": "pdd_LEGACY_4"},  # 旧索引无 sync_ts 与 schema_ver
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
        self.assertIn("新实体词", pushed_docs["taobao_UPDATED_3"]["core_entity"])
        self.assertEqual(pushed_docs["taobao_UPDATED_3"]["sync_ts"], int(t_fmt.timestamp()))
        self.assertEqual(pushed_docs["taobao_UPDATED_3"]["schema_ver"], MEILI_SCHEMA_VERSION)

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

    @patch("infrastructure.search_meili.ensure_index_configured", return_value=True)
    @patch("infrastructure.search_meili.is_meili_available", return_value=True)
    @patch("infrastructure.search_meili.get_meili_client")
    def test_sync_products_diff_upgrades_outdated_schema_ver(self, mock_get_client, _mock_avail, _mock_cfg):
        """即使 sync_ts 相同，旧文档缺少或低于当前 schema_ver 时也必须触发增量升级"""
        ts = datetime.datetime(2026, 11, 21, 6, 0, 0, tzinfo=datetime.timezone.utc)
        col = MagicMock()
        col.find.return_value = [{"platform": "pdd", "product_id": "OLD_SCHEMA", "name": "旧版本索引商品", "updated_at": ts}]
        mongo_db = SimpleNamespace(get_collection=lambda _name: col)

        mock_client = MagicMock()
        mock_index = MagicMock()
        mock_client.index.return_value = mock_index
        mock_get_client.return_value = mock_client
        mock_index.get_documents.return_value = {
            "results": [{"id": "pdd_OLD_SCHEMA", "sync_ts": int(ts.timestamp()), "schema_ver": 1}],
            "total": 1,
        }

        res = sync_products_diff(mongo_db)
        self.assertEqual(res, {"upserted": 1, "deleted": 0, "skipped": 0})
        pushed = mock_index.add_documents.call_args.args[0]
        self.assertEqual(pushed[0]["schema_ver"], MEILI_SCHEMA_VERSION)

    def test_partition_search_candidates_all_scenarios(self):
        """验证极简分桶函数在各类真实电商场景与无清洗数据降级下表现精确"""
        # 1. 鲜牛肉：生鲜牛肉进 L1/L2，狗粮与牛奶小芋头隔离进 L3
        beef_docs = [
            {"product_id": "B1", "name": "大伊 原切牛腱子 生鲜牛肉", "core_entities_raw": ["生鲜牛肉"], "core_entities_alias_raw": []},
            {"product_id": "B2", "name": "猫超 和牛牛腩块 生鲜牛肉", "core_entities_raw": ["牛腩块"], "core_entities_alias_raw": ["生鲜牛肉"]},
            {"product_id": "DOG", "name": "比乐原味鲜狗粮10kg牛肉味", "core_entities_raw": ["狗粮"], "core_entities_alias_raw": []},
            {"product_id": "TARO", "name": "京鲜生 云南牛奶小芋头", "core_entities_raw": ["小芋头"], "core_entities_alias_raw": []},
        ]
        l1, l2, l3 = partition_search_candidates("鲜牛肉", beef_docs)
        self.assertEqual([d["product_id"] for d in l1], ["B1"])
        self.assertEqual([d["product_id"] for d in l2], ["B2"])
        self.assertEqual([d["product_id"] for d in l3], ["DOG", "TARO"])

        # 2. 苹果手机：真 iPhone 进 L1，充电线/手机壳隔离进 L3；数量充足时屏蔽 L3
        phone_docs = [
            {"product_id": f"IP{i}", "name": f"Apple/苹果 iPhone 17 #{i}", "brand": "Apple", "core_entities_raw": ["手机"], "core_entities_alias_raw": []}
            for i in range(5)
        ] + [
            {"product_id": "CABLE", "name": "适用苹果手机数据线快充线", "core_entities_raw": ["数据线"], "core_entities_alias_raw": []},
            {"product_id": "CASE", "name": "苹果17手机壳防摔", "core_entities_raw": ["手机壳"], "core_entities_alias_raw": []},
        ]
        buckets = select_relevant_candidates("苹果手机", phone_docs, min_primary=5)
        kept_ids = [d["product_id"] for b in buckets for d in b]
        self.assertEqual(kept_ids, [f"IP{i}" for i in range(5)])

        # 3. 咖啡：真咖啡进 L1，咖啡味猫砂隔离进 L3
        coffee_docs = [
            {"product_id": "CF1", "name": "瑞幸浓缩黑咖啡", "core_entities_raw": ["黑咖啡"], "core_entities_alias_raw": []},
            {"product_id": "LITTER", "name": "肉垫速净咖啡纯木薯猫砂", "core_entities_raw": ["猫砂"], "core_entities_alias_raw": []},
        ]
        l1, l2, l3 = partition_search_candidates("咖啡", coffee_docs)
        self.assertEqual([d["product_id"] for d in l1], ["CF1"])
        self.assertEqual([d["product_id"] for d in l3], ["LITTER"])

        # 4. 牛肉：真酱牛肉进 L1，牛肉丸/牛肉辣酱进 L2，狗粮/方便面进 L3
        beef_broad = [
            {"product_id": "RAW", "name": "内蒙古酱牛肉500g", "core_entities_raw": ["酱牛肉"], "core_entities_alias_raw": []},
            {"product_id": "BALL", "name": "潮汕牛肉丸", "core_entities_raw": ["牛肉丸"], "core_entities_alias_raw": []},
            {"product_id": "NOODLE", "name": "康师傅红烧牛肉面", "core_entities_raw": ["方便面"], "core_entities_alias_raw": []},
        ]
        l1, l2, l3 = partition_search_candidates("牛肉", beef_broad)
        self.assertEqual([d["product_id"] for d in l1], ["RAW"])
        self.assertEqual([d["product_id"] for d in l2], ["BALL"])
        self.assertEqual([d["product_id"] for d in l3], ["NOODLE"])

        # 5. 无清洗数据新品（无 format_info / core_entities_raw）：绝不报错且正常进入 L1
        mixed_docs = [
            {"product_id": "FMT", "name": "伊利纯牛奶", "format_info": {"0": {"core_entities": [{"name": "纯牛奶", "score": 10}]}}},
            {"product_id": "NEW_RAW", "name": "光明鲜纯牛奶新品未清洗", "format_info": None},
        ]
        l1, l2, l3 = partition_search_candidates("纯牛奶", mixed_docs)
        self.assertEqual([d["product_id"] for d in l1], ["FMT", "NEW_RAW"])
        self.assertEqual(l3, [])

        # 6. 纯品牌词自适应提升：即使拼多多商品缺少 brand 且少数线报漏了品牌前缀，也不误杀无 brand 同品牌商品
        brand_docs = [
            {"product_id": "JD_B", "name": "维达抽纸100抽", "brand": "维达", "core_entities_raw": ["抽纸"], "core_entities_alias_raw": []},
            {"product_id": "PDD_NOBRAND", "name": "维达超韧卷纸", "brand": "", "core_entities_raw": ["卷纸"], "core_entities_alias_raw": ["维达卷纸"]},
        ]
        l1, l2, l3 = partition_search_candidates("维达", brand_docs)
        self.assertEqual([d["product_id"] for d in l1], ["JD_B", "PDD_NOBRAND"])
        self.assertEqual(l3, [])

        # 7. 冷门/型号词（L1+L2 < 5）：自动附带 L3 兜底，不返回空列表
        niche_docs = [
            {"product_id": "ACC1", "name": "适用iPhone 17磁吸手机壳", "core_entities_raw": ["手机壳"], "core_entities_alias_raw": []},
        ]
        buckets_niche = select_relevant_candidates("iPhone 17", niche_docs, min_primary=5)
        self.assertEqual([d["product_id"] for b in buckets_niche for d in b], ["ACC1"])


if __name__ == "__main__":
    unittest.main()
