# 电商底层最小基础能力模块规范 (`mobile_web/adapters/README.md`)

> ⚠️ **终态架构保护提示（见仓库根目录 `AGENTS.md` 第七章）**：本文档声明的 8 大底层基础能力及 `adapters/`、`infrastructure/{jd,tb,pdd,hdk}_client.py`、`infrastructure/api_governance.py` 均属**已完成生产实证的最终架构形态（Frozen Baseline）**。任何改动动刀前**必须**向用户做 `⚠️【触及已定型商品底层 API 终态架构警示】` 专项提醒并获得明确确认，改完必须实测上游样本并同步回写三大基准脚本与本文件。

本文件是仓库内 **全平台电商底层基础能力（8 个最小模块）** 的唯一架构声明与调用契约（SSOT）。
所有上层业务服务、调度器、批处理脚本（如 `services/product_service.py`、`services/promote_service.py`、`services/order_service.py`、`services/detail_prefetch.py`、`services/xianyu_discovery.py`、`services/deal_cleaning/`、`scripts/backfill_jd_coupons.py`）**必须直接调用这些标准模块**，严禁绕过适配器自行拼接外部电商 HTTP/SDK 调用或复制降级状态机。

---

## 一、 8 大最小基础能力模块声明与契约

所有平台适配器均继承 `adapters/base.py::BasePlatformAdapter`，统一对外输出标准化字典结果；任何异常情况返回 `{"status": "error", "error_code": str, "error_msg": str, ...}`。

### ① `get_recommend_goods(channel_type=5, offset=0, limit=20, uid=None, detail_sink=None, **kwargs)` — 推荐/热销榜单获取
- **输入**：频道代码（`5` 实时热销榜 / `1` 今日爆款榜）、分页偏移 `offset`、页大小 `limit`、可选 `detail_sink(DetailSnapshot)`。
- **输出**：`{"status": "success", "data": List[ProductSummary], "has_more": bool, "next_offset": int | None, "next_cursor": Any}`。
- **平台执行链（稳态优先，逐级无损兜底）**：
  - **JD**：ZTK `rank_query` -> HDK `jd_hot_rank` -> **JD 官方直连 `jd.union.open.goods.rank.query`（已解锁，不依附 ZTK 选品池）**。
  - **TB**：HDK `sales_list` + TOP `tbk.item.info.upgrade.get` 批量补全（含 `detail_sink` 快照零额外配额复用） -> TOP 官方物料精选。
  - **PDD**：PDD 官方 `pdd.ddk.goods.recommend.get`（分页上限 400）。
- **硬不变量**：`goods_id` 存恒定指纹（JD 34 位主图哈希 / TB 19 位密文后缀 / PDD 数字 ID），`goods_sign` 存最新上游动态加密签名。

### ② `search_goods(keyword, page=1, page_size=20, uid=None, **kwargs)` — 全网关键词搜索
- **输入**：非空搜索关键词 `keyword`（支持短词，上层不再硬拦截链接/口令输入），页码/页大小。
- **输出**：`{"status": "success", "data": List[ProductSummary], "has_more": bool, "total_count": int}`。
- **平台执行链与字符边界**：
  - **JD**：ZTK `goods_query`（支持 1 字，识别 `1000`/`1001` 频控） -> `[len >= 2]` ZTK `api_quanwang` -> HDK `v3/jd_goods_search`（支持 1 字，`back` 自动向上对齐离散档位 `(1, 2, 5, 10, 20, 30, 50)` 后截断至 `page_size`） -> `[len == 1]` ZTK `api_quanwang`（最终兜底）。*已永久剔除日配额为 0 的个人 `jd.union.open.goods.query` (`listId=533`)。*
  - **TB**：TOP `material.optional.upgrade` -> ZTK `api_quanwang` -> HDK `search_tb_goods`（三家均支持 1 字关键词）。
  - **PDD**：官方 `pdd.ddk.goods.search`（**前置直接传 `gateway_size = max(10, size)` 切片返回**，不再先试小分页等报 `3000000` 再重发；遇 `60001` 自动脱去 `custom_parameters` 重试） -> HDK `search_pdd_goods`。

### ③ `get_goods_detail(goods_id_or_sign="", uid=None, goods_id=None, goods_sign=None, **kwargs)` — 单品官方详情与停推确权
- **输入**：`ProductRef` 或 `(goods_id_or_sign, goods_id, goods_sign)`。JD 必须使用 50 位加密 `goods_sign` 或数字 SKU（**严禁传入 34 位主图指纹**）；TB 必须使用完整 46 位 `<prefix>-<suffix>` 加密 `goods_sign`；PDD 首选 `goods_sign`。
- **输出**：`{"status": "success", "data": ProductDetail}`；确证下架/停推返回 `error_code="PRODUCT_OFF_PROMOTION"`。
- **平台执行链**：
  - **JD**：**ZTK `api_detail`（独立通道 + 0.35s/Key 微节流 + 遇 `301: 对应宝贝已下架` 第 1 把 Key 立即早停，绝不与 `goods_query` 选品池 `1000`/`1001` 限流连坐；可信重定向覆盖 `jd.com`/`jd.hk`/`yiyaojd.com`/`3.cn`）** -> ZTK `goods_query(itemIds=...)` -> HDK `v3/jd_goods_search`（`HdkClient.get_goods_detail` 默认 `allow_keyword_search=False` 严禁非 URL 标识落入 `keyword=` 模糊搜索，且对好单库返回强制核验主图指纹一致性）。当 `api_detail` 报 `301` 且 `goods_query` 为空且 HDK 未触发限流与 `PRODUCT_IDENTITY_MISMATCH` 时，交叉确证 `PRODUCT_OFF_PROMOTION`；*已永久删除拿 `byunionid_get` 反推 `api_detail` 限流的伪探测。*
  - **TB**：TOP `tbk.item.info.upgrade.get` -> ZTK `api_detail`（强制校验返回 `tao_id` 非空并匹配目标身份） -> HDK `v2/item_detail`。
  - **PDD**：官方 `pdd.ddk.goods.detail` -> 限流时自动回退 `pdd.ddk.goods.search(goods_sign_list=[sign])`（标记 `cacheable=False`，不写入详情缓存与 `detail_updated_at`）。

### ④ `batch_get_goods_details(refs: Sequence[ProductRef], **kwargs)` — 20 批量官方查卡（精准对齐、坏项零连坐）
- **输入**：`ProductRef` 序列，内部分片上限 **20 个/批**。
- **输出**：`Dict[str, Dict[str, Any]]`，以入参去重后的 `ref.key` 为键，逐条映射各自 `get_goods_detail` 同构结果字典。
- **平台对齐与防连坐不变量**：
  - **JD**：ZTK `goods_query(itemIds<=20)`，严格按官方回传 **`oriItemId`** 对齐（无视返回 `itemId` 重加密与乱序）；对未在 `goods_query` 命中的子集（如混入坏 ID/下架 ID），仅对缺席项逐条通过独立 `api_detail` + HDK 单查确证 `PRODUCT_OFF_PROMOTION` 或报错，**绝不连坐同批正常商品**；主图哈希 `is_jd_image_key` 原地拦截拒传。
  - **TB**：TOP `tbk.item.info.upgrade.get(<=20)`，严格按 **`input_item_iid`** 对齐；批次内未命中或 TOP 整批异常的条目逐条回退单品 `get_goods_detail` 确证。
  - **PDD**：官方 `pdd.ddk.goods.search(goods_sign_list<=20)`，严格按 `goods_sign` / `goods_id` 对齐；缺失或无 `goods_sign` 条目逐条回退 `get_goods_detail` 经 `40001` 确证下架或报错。注意 `goods.search` 不返回 `goods_gallery_urls`（`batch_detail_includes_gallery = False`），仅用于 20 批量实时价格/优惠/下架核验，完整详情页预取由 `ProductService` 回退逐条 `get_goods_detail`。

### ⑤ `resolve_product_from_link(raw_text, ...)` — 链接/口令/短链身份识别与商品卡反查
- **输入**：原始链接、短链或混排分享文案。
- **输出**：结构化解析结果（含 `is_venue`、`product` 卡、目标 `target_id` / `goods_sign` / `goods_id`、`material_url` 等）。
- **核心策略（API 优先、零无意义重定向）**：
  - **JD**：明确会场域名（`pro.m.jd.com` / `prodev.m.jd.com` / `shop.m.jd.com` / `mall.jd.com` / `coupon.m.jd.com`，即使携带纯数字 `?sku=` 如 `o19hsre` 亦属会场）直接正向实证 `is_venue=True`（0 重定向）；`u.jd.com` / `union-click` / 京口令（`！...！`）**统一走 `api_detail` -> `goods_query` -> HDK `v3/jd_goods_search`（仅干净 `u.jd.com` 短链显式 `allow_keyword_search=True`，绝不直喂 `union-click` 长链以免切出假 ID） -> 0 配额 `resolve_jd_short_url` 解包**（非裸标题纯文案不触发查询，且 `api_detail` 返回 `301` 不直接短路停推）。
  - **TB**：作为纯口令/短链解 ID 辅助工具保留；日常万能转链已停用救活率 `0/58` 的 `open_shangpin_id`。
  - **PDD**：严格校验官方域名，`goods.search(page_size>=10)` 命中 `total_count == 1` 后调用 `goods.detail` 补全详情；当 `total_count == 1` 但 `goods_name` 为空时，保留唯一 `raw_goods_sign` 交 `goods.detail` 终局裁决 `PRODUCT_OFF_PROMOTION` (`40001`)，**严禁跳过直接送 `zs.unit.url.gen` 洗链**。

### ⑥ `convert_promotion_url(goods_id_or_sign, uid=None, **kwargs)` — 已知身份高佣推广转链
- **输入**：已确认的 `goods_sign` / `goods_id`、推手 `uid`、归因参数（JD `promoter_id` / TB `relation_id` / PDD `custom_parameters`）、可选 `trusted_product_url` 与 `trusted_coupon_url`。
- **输出**：含 `final_link`、`short_url`、`promotion_token`、`attribution_status`、`need_auth`、`promoter_id`/`relation_id`。
- **关键边界**：
  - **JD**：`byunionid_get` -> 公共（非个人推手）兜底 HDK `get_jditems_link`；个人推手绝不走不传 `positionId` 的 HDK。
  - **TB**：优先使用 4 段式 `channel_pid` + 折淘客通道透传 `relation_id`；非个人/站长级支持官方 `uland` 二合一升级兜底与 HDK。
  - **PDD**：三层直连 (`rp.prom.url.generate[23]` -> `goods.promotion.url.generate` -> `zs.unit.url.gen`)。

### ⑦ `resolve_and_convert_universal(raw_text, uid=None, **kwargs)` — 万能解析转链（组合 ⑤ + ③ + ⑥）
- **核心原则（Fail-Closed & 宁缺毋滥）**：
  - 严禁在失败时截取标题调用关键词搜索“指鹿为马”；
  - 四态确定性标记 `resolution_kind`：`product`（完整实证单品卡）、`coupon_or_venue`（明确会场/官方券页，仅正向实证允许）、`degraded_product`（转链成功但暂未拿到商品卡，禁止判作纯券或写入永久误分类缓存）、`rejected`（跨平台/数字直链/空文本/停推等）。
  - **JD**：`resolve_product_from_link` 拿卡/会场实证（含 `hour.jd.com` / `mcard.jd.com` / `plus.m.jd.com` / `lives.jd.com` 官方活动域正向会场判定） -> `byunionid_get` 唯一转链主出口（`99.98%` 独立稳定，不受选品池冷却阻塞；公共回退 HDK `get_jditems_link`；若旧 `u.jd.com` 短链因内嵌过期券报 `2001917` 等可恢复业务错且已查出不同纯净 `jingfen` 商品链时，自动回退纯净商品链重转一次），对裸京口令或新产出 `u.jd.com` 补 1 次 `api_detail` 轻查卡；非 `u.jd.com` 入口（如 `union-click` 长链）在洗出干净 `u.jd.com` 后若仍缺卡再按短链交由 HDK 拿官方正卡并补绑券；**已永久移除 `tkl_piliang` (`0.06%`)、`api_quanwang` (`0.00%`) 与选品冷却门卫**。
  - **TB**：主通道 `open_gaoyongzhuanlian_tkl`（遇混排长文案自动升级 `tkl_piliang`），`ZtkClient.convert_channel_url` **严禁伪造 `"渠道推广商品"`/`0.00` 占位卡片**；`TaobaoAdapter` 强制校验完整卡字段（`tao_id + 非空原标题 + price > 0 + 主图/轮播图`），缺卡自动回查 `get_goods_detail(tao_id)` 补全或明确 `PRODUCT_OFF_PROMOTION` / Fail-Closed `degraded_product`。`pages-fast.m.taobao.com/wow/`、`mo.m.taobao.com`、`uland.taobao.com/quan` 等在 ZTK 成功转出推广链且无 `tao_id` 时正向确认为 `coupon_or_venue`（**仍拒绝未转换成功的原始 `/wow/z/lianmeng/` 与 `wh_alimama=` 页面**）。已移除 `open_shangpin_id` (`0/58`) 兜底。
  - **PDD**：复用 `resolve_product_from_link`；仅对合法但搜索未收录的非 `40009`/非下架/非多结果分享短链走 `zs.unit.url.gen` 洗链并标为 `degraded_product`。
  - **线报清理统一前置分流 (`deal_cleaning/url_venue.py::resolve_non_cps_http_url`)**：所有非官方 CPS HTTP 入口（`3.cn`、`llipin.cn`、`kurl07.cn`、`t.cn` 等）统一前置免费 SSRF 安全解包分流为 `venue` / `cps_redirect` / `numeric_item` / `dead(0重试)` / `transient(重试)`，且正式入库与云同步仅靠结构化字段更新 `deal_info.updated_at`（在榜心跳只续命 `last_seen_at` + `publish_ts`）。

### ⑧ 推手授权与增量订单（`check_authority` / `generate_authority_url` & `fetch_increment_orders`）
- **推手授权**：
  - **JD**：不强制通道授权（返回 `not_required`）。
  - **TB**：未配置长期 `taobao_refresh_token` 时，**首选折淘客 90 天长效 `publisher_sid_list`（`open_sc_publisher_get` / `open_sc_invitecode_get`）** 规避 24h `taobao_session` `27: invalid-sessionkey`；仅当显式配置 `taobao_refresh_token` 时优先官方 TOP 并附带 300s 会话失效冷却。
  - **PDD**：官方 `member.authority.query` & `rp.prom.url.generate[10]`。
- **增量订单**：
  - **JD**：**JD 官方直连 `jd.union.open.order.row.query` 优先** -> ZTK 订单网关兜底（窗宽 `<= 3600s`）。
  - **TB**：ZTK `open_dingdanchaxun2.ashx`（二跳官方签名 HTTPS URL，窗宽 `<= 1200s`）。
  - **PDD**：官方 `pdd.ddk.order.list.increment.get`（窗宽 `<= 86400s`，金额分转元）。

---

## 二、 折淘客 3-Key 双 SID 池隔离运维规范 (`config.local.json`)

淘宝渠道接口与高频交易/转链接口在折淘客授权后端存在两套独立 SID，必须按下标 1 对 1 对齐配置：

| Key 下标 | 用途 | 配置字段名 | 说明 |
| --- | --- | --- | --- |
| `0..2` | 3 把 AppKey 轮询池 | `zhetaoke_appkey_list` | JD 全接口免 SID 通用；TB 强校验 `(appkey[i], sid[i])` 一一对应 |
| `0..2` | 高频转链/查券/口令/订单 SID 池 | `zhetaoke_sid_list` | 仅供 `tkl` / `tkl_piliang` / `gaoyong` / `quanwang` / `dingdanchaxun2` 使用 |
| `0..2` | 渠道备案与邀请码 SID 池 | `zhetaoke_publisher_sid_list` | **仅由 `check_publisher_relation` 与 `generate_authority_url` 消费** |

> 缺省时 `ztk_publisher_sid_list` 自动回退 `zhetaoke_sid_list`。严禁打乱三个列表的下标对应关系。

---

## 三、 11 项系统业务功能 × 8 个最小基础模块复用矩阵

| 系统业务功能 | 主调用入口 | 复用的底层最小基础模块 |
| --- | --- | --- |
| 1. 首页爆品榜单展示 | `ProductService.get_home_recommendations` | ① `get_recommend_goods` |
| 2. 首页爆款后台刷新预热 | `DetailPrefetcher._seed_inner` / `worker` | ① `get_recommend_goods` + ④ `batch_get_goods_details`（单平台 <=20 合并） |
| 3. 后台详情批量补全与下架清理 | `ProductService.prefetch_product_details_batch` | ④ `batch_get_goods_details`（<=20，下架自动物理驱逐） |
| 4. 全网商品关键词搜索 | `ProductService.search_home_goods` | ② `search_goods`（直透短词与常规搜索词） |
| 5. 商品详情页实时查价与发券页渲染 | `ProductService.get_goods_detail` | ③ `get_goods_detail` |
| 6. 点击推广高佣转链（公开 / 个人归因） | `PromoteService.convert_link` | ⑥ `convert_promotion_url`（缺优惠券或需要时辅以 ③ `get_goods_detail`） |
| 7. 万能转链框 / 淘京拼短链与口令转换 | `PromoteService.convert_universal` | ⑦ `resolve_and_convert_universal`（内部组合 ⑤+③+⑥） |
| 8. 社群线报清洗与自动转化入库 | `deal_cleaning/stages.py` | ⑦ `resolve_and_convert_universal` + ⑤ `resolve_product_from_link` |
| 9. 闲鱼选品比价前置实时官方验价 | `xianyu_discovery.select_round_candidates_for_source` | ④ `batch_get_goods_details`（领先品类首候选批量 <=20 预验，未命中回退 ③） |
| 10. 存量京东商品优惠券回填脚本 | `scripts/backfill_jd_coupons.py` | ④ `batch_get_goods_details`（每批 20 条回填 `coupon_url` 与清理下架） |
| 11. 推手渠道授权引导 & 订单定时同步归因 | `UserService` / `OrderService` | ⑧ `check_authority` / `generate_authority_url` & `fetch_increment_orders` |
