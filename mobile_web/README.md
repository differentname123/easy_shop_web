# mobile_web

移动端导购与推广佣金查询应用。当前实际接入 **拼多多**；淘宝、京东仅保留明确禁用的兼容占位，不包含真实授权、查询、转链或订单实现。

## 功能全景与业务边界

| 功能 | 已有入口 | 实际行为 |
| --- | --- | --- |
| 平台及频道 | `GET /api/home/platforms`、`GET /api/home/channels` | 从适配器装配表取得平台名称、可用性与频道；未接入平台不能伪装成功。 |
| 官方推荐和搜索 | `GET /api/home/recommend`、`GET /api/home/search` | 联盟在线推荐/搜索，按平台、UID、分页与查询条件缓存；推荐摘要后台更新本地商品库。 |
| 本地商品目录（爆单库） | `GET /api/goods/list`、`GET /api/goods/categories` | 查询本地 Mongo 商品，不等于联盟实时搜索；支持平台、类目、关键词、排序和分页。 |
| 商品详情 | `GET /api/home/detail`、`/detail` | 摘要先展示；按内存、有效数据库详情、官方刷新、故障降级取得内容。 |
| 推广转链和分享文案 | `POST /api/promote/convert` | 平台负责生链，服务负责归因作用域与缓存；复制/购买时才请求，文案按本次标题和金额生成。 |
| 账户和资料 | `POST /api/user/register_or_login`、`GET /api/user/info` | 手机号密码注册/登录合一、令牌认证、脱敏资料；邀请码只记录既有邀请关系，不代表已实现邀请奖励。 |
| 推广授权 | `GET /api/user/auth` | 查询本人平台备案状态，必要时生成授权链接；已确认授权有本地有效期。 |
| 订单和佣金 | `POST /api/order/sync_and_query` | 查询本人本地订单与佣金，同时有界触发后台增量同步；统计整个筛选集，明细最多100条。 |

页面包含首页、爆单推广、我的账户三个 Tab，以及独立商品详情页。已有列表搜索/滚动恢复、分页去重、失败重试、会话过期及迟到响应隔离。后端的类目筛选和价格降序接口不代表当前页面已经提供相应入口。

**当前没有站内下单、支付、资金账本、提现、打款或佣金入余额流程。** `users` 中 `balance / total_rebate / pending_rebate` 是保留的历史字段，不能代替订单统计，更不能据此承诺可提现金额。

## 用一套业务概念，而不是每个平台一套模型

| 概念 | 公共含义 | 保留的平台差异/兼容边界 |
| --- | --- | --- |
| 平台 | 统一平台代码、名称、启用状态 | 显式适配器装配；`tb` 是 `taobao` 的兼容别名。 |
| 商品身份 | 平台 + 有类型的标识，使用 `ProductRef` | `goods_id` 和不透明的 `goods_sign` 分开；只有适配器解释原始输入，不能因为字符串长得像数字就跨类型互认。 |
| 商品内容 | 摘要用于列表，完整详情有自己的来源时间 | 只有身份或推广缓存不等于已取得商品内容；不展示伪造的未命名零元商品。 |
| 优惠和价格 | `coupon_amount` 为已知券额，`price` 为商品券后/当前参考价，单位元 | 只使用来源提供的信息；付款以外部平台实际订单为准，佣金不抵扣付款。 |
| 商品预估佣金 | `commission_amount` 为商品推广佣金参考值，`commission_rate` 为百分数 | 原始金额/费率单位在适配器转换；不是已经获得的收益，也不是承诺给购买者的返利比例。 |
| 订单佣金 | 已归因订单的佣金，区分退款和平台结算状态 | 平台原始状态码由适配器转换；结算给联盟账户不等于本系统可提现余额。 |
| 平台授权 | 本人是否已完成该平台推广所需授权 | `bind / we_app_info` 等既有兼容字段保留；不把某个平台备案细节扩散成通用业务分支。 |
| 登录身份与推广归因 | 私有资料/订单以有效令牌 UID 为准；推广 UID 是另一作用域 | 当前页面登录后用本人 UID，游客用站长默认推广位。分享 URL 的 UID 不用于游客转链，也不能成为登录凭据。 |

保留 `goods_id / product_id`、`brand / brand_name` 等既有字段别名，是为了兼容接口和历史数据；不为命名整齐而全局改名、迁移集合或新增一套通用模型框架。后端推广接口仍兼容匿名和显式推广 UID，不改变已有外部调用方式。

## 模块边界

```text
run.py                     单目录启动、本地校验、显式 Mongo ping
main.py                    HTTP 装配、生命周期、静态页面、日志
infrastructure/            本地 Mongo、PDD SDK/传输、日志基础设施
scripts/init_config.py     一次性提取旧配置并初始化私有认证密钥
api/                       请求校验、认证依赖、调用应用服务
services/                  业务编排、缓存、授权有效期、后台同步节流
adapters/                  平台 SDK、商品标识、原始字段转换、转链降级
repositories/              Mongo 持久化、幂等更新、查询和聚合
models/                    商品身份与现有数据契约
cache.py / config.py       分域有界缓存及公共配置
static/js/
  api.js                   HTTP 传输，保留 HTTP 与业务状态
  session.js               已验证会话及过期响应隔离
  format.js                商品身份、列表去重、金额展示
  browser.js               剪贴板、提示、加载器、移动端跳转
  feeds.js                 首页/爆单列表、分页、搜索快照
  account.js               登录、授权、订单佣金
  app.js                   首页控制器组合与导航/手势
  detail.js                独立详情页及按需转链
```

依赖方向为 **路由 → 服务 → 适配器/仓储 → 本地 infrastructure**。仓储不解释平台 SDK 状态，通用服务不猜测拼多多原始字段。Mongo 管理器、PDD SDK/网络工具和日志实现均在本目录内，不再运行时导入父项目的 `common`、`app.pdd_utils`，也不通过修改仓库 `sys.path` 找模块。现有分层已经足够，不引入插件发现、微内核、通用仓储、任务队列、新缓存系统或前端构建工具。

`create_app()` 负责 HTTP 装配，仓储首次使用时才连接 Mongo、初始化索引；索引失败不会永久标记为成功。启动预热复用已装配的首页商品服务，不再私自创建另一套真实仓储。

## 以后如何接平台（本次不实现）

1. 在 `adapters/` 实现现有 `BasePlatformAdapter` 契约。
2. 在 `adapters/__init__.py` 的显式装配表登记适配器。
3. 由适配器提供平台名称、可用性、频道、标识解析、规范化商品/订单及平台自身限制。

通用商品、推广、授权和订单编排不需要另复制一套。API 平台参数验证代码格式，实际可用性由装配表判断。`GET /api/home/platforms` 的元信息已被前端平台筛选消费。

这**不意味着整套前端已完全动态多平台化**：目前首页、授权与订单页面仍针对实际接入的拼多多，真正接入新平台时应按其实际能力补充页面。不得将历史本地库中的其他平台商品或占位类误当成已完成平台业务。

## 商品身份、内容及缓存

### 身份不等于内容

- `ProductRef` 独立保存平台、真实 ID、Sign。缺失 ID、旧清洗器的 `0` 不是真实 ID。
- Sign-only 商品内部使用 `product_id = sign:<goods_sign>`，兼容已有 `(platform, product_id)` 唯一索引；对外真实 ID 仍为空。
- 同平台后来获得真实 ID 时可更新原 Sign-only 文档；不带真实 ID 的摘要不会抹掉已有 ID。
- 同时提交 ID 与 Sign 不代表二者已验证为同一商品。详情按主标识查找，缓存别名只来自可信响应；推广的辅助 ID 只来自同平台身份投影查询。
- 推广持久缓存允许创建仅有身份的文档，但列表、类目和详情共用内容准入：实际非空名称及已知有效价格。价格字段按 `activity_price` 非空优先、否则 `price` 回退；真实0、数值字符串和 BSON 数值保持兼容。
- 内容条件在 Mongo 排序/分页之前执行，不能取一页后再过滤。裸文档不能在上游失败时作为零元详情降级；`get_product_identity` 不要求完整内容，仍可提供推广身份。

### 详情有效期只有一个来源时钟

- 摘要和详情共用字段映射；摘要只更新实际提供的值，不覆盖完整详情、品牌或类目。
- 详情使用独立 UTC `detail_updated_at`，来源寿命由 `PRODUCT_DETAIL_TTL` 管理。推荐摘要的 `updated_at` 不会延长它。
- 数据库详情回填内存后仍沿用 `detail_updated_at + TTL`；官方新详情以响应取得时刻计时，仓储保存同一抓取时间，不因写库延迟重新续期。
- 内存容器继续有界 LRU-TTL，同时检查私有来源截止时间。缓存键包含平台、UID、标识类型；不使用裸 ID 键。内部源时间和缓存信封不进入详情 HTTP 内容。
- 上游故障可返回有效陈旧内容并标记 `source=database_fallback`，但不把降级内容写入正常 TTL 缓存，以便恢复后立即重试。
- 真实零佣金、零券额保持零；未知保障、评分、库存保持空，不虚构补贴、最低价或服务承诺。

### 推广只缓存生链结果，文案按当前请求生成

- 推广内存键包含平台、主标识类型/值、URL、归因 UID；**先查缓存，miss 后才补全可信身份**，Sign 热命中不再查询 Mongo。
- 缓存有效链接、唤起协议等兼容字段，不缓存本次标题/价格生成的 `command_text`。每次返回都由适配器按当前请求重建文案，历史缓存中的旧文案也不直接复用。
- 无有效链接的伪成功及 `is_fallback` 降级链接不进入正常缓存；返回值防御性复制，不能跨 UID 串链。
- 仅匿名、身份已知的正常推广结果持久化到 `products.default_promotion`；URL-only 只使用内存 URL 作用域缓存，不捏造商品身份。
- 新 `default_promotion_expires_at` 明确按 UTC 保存三天来源截止时间。旧文档无可信截止时间、时间非法/过期时按 miss 按需重建；不猜历史本地时区，不迁移或删除商品。
- 数据库推广结果回填内存也保留原截止时间，不重新续期。现有内存容器最多保留30分钟；截止时间不出现在 HTTP 响应。
- 详情页不长期保存成功转链结果，只合并同一完整 payload/会话的进行中请求。后续购买/复制再次请求后端，避免页面永久保留旧文案；卸载或会话变更后旧操作不能复制、导航或恢复账户。

## 查询与索引：已改什么、没有证明什么

| 查询 | 当前处理 |
| --- | --- |
| 商品唯一身份 | 保留唯一 `(platform, product_id)` 和原 `goods_sign` 索引；按同平台、有类型身份查询。 |
| 全平台默认最新列表 | 新增非唯一 `updated_at DESC` 索引声明。 |
| 指定平台默认最新列表 | 新增非唯一 `platform ASC + updated_at DESC` 索引声明，与共享管理器已有同规格索引保持一致。 |
| 列表读取 | 只投影摘要所需字段，不读取大段图文和推广载荷；推广身份及持久缓存也使用窄投影。 |
| 子串关键词搜索 | 保留正则转义、查询时限和分页上限；不锚定、忽略大小写的包含搜索不能靠普通 B 树索引直接解决。 |
| 订单汇总 | 保留 `(platform, promoter_uid, order_pay_time)` 查询索引及全量 `$facet` 汇总，明细单独限制100条；不另造统计缓存。 |

不为价格、优惠、类目所有组合铺索引，不给 `products` 加会删除整条商品的缓存 TTL 索引，也没有因为未来平台接入而增加 Redis 或搜索服务。

**本轮没有连接真实数据库，不能断言生产缺索引或给出性能倍数。** 已验证的是代码查询形状、字段投影、索引声明，以及推广热命中不调用仓储。上线前应核对现有索引，评估首次索引创建负载，再在受控环境使用实际请求分布及 `explain(executionStats)` 检查默认排序、价格排序、类目和关键词查询；索引初始化仍沿用既有首次访问机制。

本地爆单库 `total_count` 保留探测下界语义，翻页使用 `has_more`；订单 `total_count` 是完整筛选集计数。保留 `products/users/orders` 集合和主要字段，不删除历史数据或索引。

## 账户、授权与订单的正确性边界

- 手机号登录、令牌格式、旧密码哈希升级、脱敏输出及原认证头兼容保留。本人资料、授权、订单始终以令牌 UID 为权威。
- 页面会话必须由 token 验证；URL UID、缓存手机号不能当作登录身份。退出、换号、筛选后迟到的响应不能恢复旧账户或覆盖新结果。
- 当前页面推广归因保持：已验证本人 UID 优先；游客使用站长默认推广位，分享 URL 的 UID 只保留原商品查询行为。
- 已确认授权按 `AUTH_STATUS_TTL` 缓存，过期后复核；平台故障不等于未授权，不覆盖已有状态。
- 订单统计整个筛选集，不只统计返回的100条明细；退款/结算布尔字段优先，旧文档缺失时才用对应适配器的状态兼容规则。数据库错误不伪装零收益。
- `days=0` 查询本地全部历史，不代表同步了全部历史。`sync_status/sync_error/synced_count/last_sync_attempt_time` 区分尝试与成功，`last_sync_time` 只在完整窗口落库成功后推进；手动刷新不绕过冷却或锁。

### 明确保留、需要独立处理的限制

1. **订单同步仍为 best-effort**：最近23小时、默认至多5页、180秒冷却、进程内互斥。达到预算会报 `partial`，但没有持久化游标、长期离线补单或多 worker 分布式互斥；已有库统计正确不等于所有平台历史订单已入库。
2. **缓存是进程内缓存**：多 worker 不共享命中或失效；本轮不引入分布式缓存。推荐写入/预热仍使用既有 daemon 线程，不宣称具备完整任务回收体系。
3. **共享数据库的采集工作流**：在线写入仍保留 `format_retry_count=999` 等隔绝离线清洗的既有标记；父项目采集器使用的匿名 `promotion_url` 与本应用推广缓存不是可直接混用的个人归因链接。数据库数据契约仍需兼容，但部署和运行不依赖父项目采集器文件；本轮不更改其工作流。
4. **PDD 网络兼容策略**：SDK/传输实现已迁入本地 `infrastructure/`；`PDD_DNS_MODE=auto` 保留 Windows 网络优化，Linux 默认走原生 DNS；代理环境的默认信任策略也区分 Windows/Linux。不是新增平台能力，也不代表已经验证所有网络环境。
5. **没有资金钱包**：预估商品佣金、订单佣金、平台已结算状态均不能证明本系统有可提现资产；不承诺固定到账或提现日期。

## 单目录运行：依赖与配置

**复制整个 `mobile_web` 目录即可独立运行，不需要复制父项目或安装父级 requirements。** 参考环境为 Python **3.11+**：Debian 12 默认 Python 3.11，Debian 13 默认 Python 3.13；先确认实际系统版本，不假定所有 Debian 都使用同一 Python。生产直接依赖仅为 `fastapi / uvicorn / pydantic / pymongo / requests / cachetools`，直接依赖版本见 `requirements.txt`，完整传递依赖通过 `constraints.txt` 固定（安装时自动使用）；不需要 GUI、AI SDK、Node/npm、gunicorn 或单独的 `bson` 包（BSON 由 PyMongo 提供）。`requirements-dev.txt` 额外安装 pytest/httpx，仅供测试。

Mongo 已在 Debian 云服务器部署，沿用 Windows 当前的 Mongo 主机、端口、账号、密码、数据库和认证库配置。**本方案不部署、搬迁或初始化另一套 Mongo，也不迁移集合、改历史时间或清理数据；没有 compose。** 拼多多仍需可用的联盟凭据和外网连接。

### 私有配置只初始化一次

- 跟踪的 `config.example.json` 是结构模板，不包含真实凭据，也不能直接作为可用生产配置。
- 默认私有配置是应用目录内的 `config.local.json`。`WEB_CONFIG_FILE` 可以指定其他文件；相对路径始终锚定 `mobile_web` 目录，不随启动命令的当前目录改变。
- 优先级为 **环境变量 > JSON > 默认值**。JSON 字段名称参照模板；迁入工具会转换支持的旧字段，不要直接复制整个父项目配置。
- 生产不会向上搜索父级配置。首次从旧环境迁入时，显式运行 `scripts/init_config.py --source <旧JSON绝对路径>`；它只提取 Mongo、PDD 和已有的安全认证密钥，**不覆盖已存在的目标文件**。
- 安全的已有 `WEB_AUTH_SECRET`/JSON 认证密钥应继续沿用。缺少安全密钥时，迁入工具只在首次初始化生成并持久保存一个新密钥；原公开默认值不再接受，旧 token 需重新登录。不要在每次启动或部署时生成新密钥，也不要任意轮换有效密钥。
- 全新配置可按模板创建私有 JSON，填写实际凭据与至少32字符的随机认证密钥。不要把密钥、私有 JSON、`.env`、日志或证书提交到 Git 或打进镜像。

| 环境变量 | 含义 / 默认值 |
| --- | --- |
| `PDD_CLIENT_ID`、`PDD_CLIENT_SECRET`、`PDD_PID` | 当前拼多多联盟凭据和推广位；沿用原值。 |
| `MONGO_HOST`、`MONGO_PORT`、`MONGO_USER`、`MONGO_PASSWORD` | 当前 Mongo 连接值；常规端口默认 `27017`。 |
| `MONGO_DB_NAME`、`MONGO_AUTH_SOURCE` | 业务数据库及认证库；必须与现有 Mongo 用户实际配置一致。 |
| `WEB_AUTH_SECRET` | 私有认证密钥，至少32字符，拒绝历史公开默认值；无安全配置时不能用旧默认密钥启动。 |
| `WEB_HOST`、`WEB_PORT` | 默认 `127.0.0.1:8000`；未设置 `WEB_PORT` 时兼容 `PORT`。命令行 `--host/--port` 可显式覆盖监听值。 |
| `WEB_WARMUP_ENABLED` | 默认 `1`；设为 `0` 或使用 `--no-warmup` 禁止启动预热。 |
| `WEB_LOG_DIR` | 默认 `runtime/logs`，相对路径锚定应用目录；空字符串或 `-` 禁用文件日志，控制台仍有日志。 |
| `WEB_LOG_LEVEL` | 日志级别，例如 `INFO`。 |
| `PDD_DNS_MODE` | `auto / on / off`，默认 `auto`：Windows 保留优化、Linux 使用原生 DNS；特殊网络问题才显式调整。 |
| `PDD_TRUST_ENV` | 是否信任 requests 代理等环境设置；默认 Windows `0`、Linux `1`。 |

`run.py` 默认单进程、单 worker、不开热重载。`--reload` 仅供开发；多 worker 不能共享当前进程内缓存或订单同步锁，不作为本方案生产默认值。默认入口为 **`python /绝对路径/mobile_web/run.py`**，无需仓库包名或 `--app-dir ../..`。

### Windows：使用专用虚拟环境

以下以现有 `W:/project/python_project/easy_shop/app/mobile_web` 和本次实际验证的 Windows Python 3.12 为例，在 PowerShell 或 Git Bash 执行；若使用其他支持版本，按已安装解释器调整 `-3.12`。路径有空格时，PowerShell 需用 `& "完整python.exe路径"` 调用。全程无需激活虚拟环境；本次已在本目录创建 `.venv`，当前机器可直接使用，无需重复创建。

```bash
py -3.12 -m venv W:/project/python_project/easy_shop/app/mobile_web/.venv
```

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe -m pip install -r W:/project/python_project/easy_shop/app/mobile_web/requirements.txt
```

首次迁入时，把下条命令中的旧配置路径替换为实际绝对路径；若 `config.local.json` 已存在，跳过此步，不删除重建它。

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe W:/project/python_project/easy_shop/app/mobile_web/scripts/init_config.py --source "W:/旧配置目录/config.json"
```

纯本地校验不会访问 Mongo 或 PDD，也不会启动服务器。

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe W:/project/python_project/easy_shop/app/mobile_web/run.py --check
```

需要主动核对现有 Mongo 连接时，再单独执行只读 ping；不会创建索引或写入业务数据。

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe W:/project/python_project/easy_shop/app/mobile_web/run.py --check-mongo
```

正常启动默认会预热真实平台连接和推荐商品；浏览器打开 `http://127.0.0.1:8000`。

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe W:/project/python_project/easy_shop/app/mobile_web/run.py
```

只检查真实入口的页面、静态资源和存活接口时，可改用下条命令（与上条二选一）。这**不是离线模式**：访问真实业务 API 仍可能连接 Mongo/PDD。

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe W:/project/python_project/easy_shop/app/mobile_web/run.py --no-warmup
```

## Debian：venv + systemd（推荐）

### 1. 上传与安装

通过 SFTP/部署工具将本目录代码上传到登录用户的 `~/mobile_web-upload/`。不要上传 Windows `.venv/venv`、`__pycache__`、缓存、旧日志、`runtime/`、`.git/`、`.claude/`、`.env`、私钥或证书。首次所需的 **同一份私有配置**单独安全上传为 `~/mobile_web.config.local.json`，不要与公共代码包混在一起；传输过程应使用 SSH/SFTP，源文件和暂存文件只允许本人读取。

下列 `/opt/mobile_web` 与用户 `mobileweb` 是统一示例，实际路径或用户不同时，同时替换命令和 `deploy/mobile_web.service` 的 `User / Group / WorkingDirectory / ExecStart / WEB_CONFIG_FILE`。这些命令仅用于应用，不安装或重启 Mongo。

```bash
sudo apt-get update
```

```bash
sudo apt-get install -y python3 python3-venv ca-certificates
```

```bash
python3 --version
```

确认 Python 至少3.11后，首次建立专用非登录用户和目录（已存在则不要重复创建）。

```bash
sudo useradd --system --home-dir /opt/mobile_web --no-create-home --shell /usr/sbin/nologin mobileweb
```

```bash
sudo install -d -o mobileweb -g mobileweb -m 0750 /opt/mobile_web
```

以下整体复制**仅用于首次安装**。后续同步务必排除服务器的 `config.local.json`、`.env`、`.venv/` 和 `runtime/`，不要用删除式同步覆盖私有配置或运行数据。

```bash
sudo cp -a "$HOME/mobile_web-upload/." /opt/mobile_web/
```

```bash
sudo chown -R mobileweb:mobileweb /opt/mobile_web
```

仅首次安装私有配置；后续发布跳过此步，沿用服务器已有配置和安全认证密钥。

```bash
sudo install -o mobileweb -g mobileweb -m 0600 "$HOME/mobile_web.config.local.json" /opt/mobile_web/config.local.json
```

确认安全落位后，移除登录用户的临时私有配置副本。

```bash
rm "$HOME/mobile_web.config.local.json"
```

Windows 的虚拟环境不能复用到 Linux，必须在 Debian 本机新建。

```bash
sudo -u mobileweb python3 -m venv /opt/mobile_web/.venv
```

```bash
sudo -u mobileweb /opt/mobile_web/.venv/bin/python -m pip install -r /opt/mobile_web/requirements.txt
```

```bash
sudo -u mobileweb /opt/mobile_web/.venv/bin/python /opt/mobile_web/run.py --check
```

如需显式验证已部署的 Mongo，可单独执行；本地校验成功不等于数据库连接成功。

```bash
sudo -u mobileweb /opt/mobile_web/.venv/bin/python /opt/mobile_web/run.py --check-mongo
```

### 2. 交给 systemd

先检查 `deploy/mobile_web.service` 内的路径和账号。该模板直接使用虚拟环境 Python，不依赖 shell 激活；监听 `127.0.0.1:8000`，单 worker、无 reload，文件日志关闭后进入 journal。可选 `/etc/mobile_web.env` 用于环境覆盖，若创建，应由 root 持有且权限 `0600`，不要在 unit 文件中填写凭据。

```bash
sudo install -m 0644 /opt/mobile_web/deploy/mobile_web.service /etc/systemd/system/mobile_web.service
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now mobile_web
```

```bash
sudo systemctl status mobile_web --no-pager
```

```bash
sudo journalctl -u mobile_web -n 100 --no-pager
```

服务模板启用了只读系统保护。若改为文件日志，需要先创建服务用户可写的 `runtime/logs`，修改 `WEB_LOG_DIR` 并在 unit 中仅为该目录增加 `ReadWritePaths`；不要把整个应用目录改为运行时可写。

后续发布只更新代码和必要的依赖，不覆盖配置、不重建密钥；确认 `--check` 成功后重启。

```bash
sudo systemctl restart mobile_web
```

### 3. 可选 nginx 整站反向代理

直接对公网部署时必须使用 **有效 HTTPS**，不要通过明文 HTTP 传输登录密码或 token。`deploy/mobile_web.nginx.conf` 是需替换域名的 **HTTP/本地联调模板**，没有伪造证书，也没有声称它本身完成生产 TLS；上线前应配置真实证书、443站点和80到HTTPS跳转。Uvicorn 保持只监听回环地址，防火墙不放行8000。未配置 HTTPS 前不要进行真实登录。

```bash
sudo apt-get install -y nginx
```

先在上传的模板中把 `shop.example.com` 替换为实际域名，再安装；完整站点（`/`、`/detail`、`/static`、`/api`、`/health`）都反代到应用，不只代理 API。

```bash
sudo install -m 0644 /opt/mobile_web/deploy/mobile_web.nginx.conf /etc/nginx/sites-available/mobile_web
```

```bash
sudo ln -s /etc/nginx/sites-available/mobile_web /etc/nginx/sites-enabled/mobile_web
```

```bash
sudo nginx -t
```

```bash
sudo systemctl reload nginx
```

模板假设 nginx 是公网边界：用 `$remote_addr` **覆盖** `X-Forwarded-For`，而不是无条件追加客户端自报值，同时覆盖相关协议/主机头并清除标准 `Forwarded` 头。若前面另有 CDN/负载均衡，必须先按实际可信代理网段配置 real-IP 信任，不能直接信任任意来源头。

## Docker：可选，不包含 Mongo

构建上下文必须是 **本目录**。Dockerfile 只复制本目录生产文件，非 root UID/GID `10001:10001` 运行；最终 `runtime` 镜像不包含测试或开发依赖。`.dockerignore` 排除私有 JSON、环境文件、虚拟环境、日志和密钥，但保留 `tests/` 供独立 `test` target 使用。真实私有 JSON **只能在运行时只读挂载**，不要用 `COPY`、build arg 或镜像层写入密钥；JSON 的实际挂载文件名不受 `.dockerignore` 限制。

在 `mobile_web` 目录执行以下构建命令，默认基础镜像为 `python:3.11-slim-bookworm`。

```bash
docker build --target runtime -t mobile-web:local .
```

如需验证 Debian 13 / Python 3.13 组合，可显式更换基础镜像；应重新跑测试，不能只凭构建成功宣称业务兼容。

```bash
docker build --build-arg PYTHON_IMAGE=python:3.13-slim-trixie --target test -t mobile-web:test-py313 .
```

```bash
docker run --rm --network none mobile-web:test-py313
```

默认 Python 3.11 的后端离线测试也可单独构建/执行；test 镜像不包含 Node，前端回归在宿主开发环境运行。

```bash
docker build --target test -t mobile-web:test .
```

```bash
docker run --rm --network none mobile-web:test
```

### Debian Docker 网络与私有配置

**容器中的 `localhost` 不是宿主机 Mongo。** 若现有 Mongo 配置指向 Debian 宿主机回环地址，推荐 Linux Docker 的 host 网络，沿用同一连接值，不改 Mongo、不另启数据库。下面用 `--host 127.0.0.1` 显式覆盖镜像的 `WEB_HOST=0.0.0.0`，保证 host 网络下仍只暴露给本机 nginx；host 网络不用 `-p`。不要同时运行占用同一8000端口的 systemd 应用与容器。

宿主服务用户的 `0600` 配置不一定能被容器 UID10001读取。以下命令为 Docker 准备独立的受限挂载副本；仅在首次切换 Docker 或有意更新配置时执行，不修改原配置、不生成新认证密钥。启用 userns-remap/rootless Docker 时，应按实际 UID 映射调整权限，而不是放宽为全员可读。

```bash
sudo install -d -o root -g root -m 0700 /etc/mobile_web
```

```bash
sudo install -o 10001 -g 10001 -m 0400 /opt/mobile_web/config.local.json /etc/mobile_web/config.local.json
```

```bash
sudo docker run -d --name mobile-web --restart unless-stopped --network host --mount type=bind,src=/etc/mobile_web/config.local.json,dst=/opt/mobile_web/config.local.json,readonly mobile-web:local python /opt/mobile_web/run.py --host 127.0.0.1
```

若原值本来指向远程 Mongo，可以用 bridge 网络并将8000只映射至宿主 `127.0.0.1`；如改连宿主地址，应通过明确的 `MONGO_HOST` 环境覆盖，确认 Mongo 已有绑定、防火墙和权限后再用，不能假定容器能访问宿主回环 Mongo。

Windows Docker Desktop **不能照搬 Linux host 网络步骤**。访问 Windows 宿主服务通常使用 `host.docker.internal`，但 Mongo 必须已经允许来自该网络的连接；仅替换主机名不能解决只绑定回环地址或权限不通的问题。本项目 Windows 首选前述 venv 部署；Docker Desktop 网络按实际环境另行验证，不会自动变更 Mongo 配置。

镜像默认日志到 stdout/stderr（`WEB_LOG_DIR=-`）；容器 `HEALTHCHECK` 仅请求本进程 `/health`，是 **liveness，不是 Mongo/PDD readiness**。默认端口为8000，若有意变更容器监听端口，须同步调整健康检查。`--no-warmup` 仅禁止预热，不会把真实应用变成离线替身。

## 测试与验收边界

以下开发/回归命令在 `mobile_web` 目录、其专用虚拟环境的 Python 下执行。生产不必安装开发依赖；不对整个父项目环境运行 `pip freeze`。

```bash
python -m pip install -r requirements-dev.txt
```

后端回归：

```bash
python -B -m pytest tests -q
```

`pytest.ini` 仅设置本目录导入路径并关闭 pytest cache；`tests/conftest.py` **在收集/导入应用前**安装测试隔离：假配置、无文件日志、阻断真实 MongoClient 与联盟 requests HTTP。每个测试和整个 session 都检查访问尝试，业务捕获异常也不能掩盖误连；注册表和缓存按测试隔离。

前端无依赖回归仅需开发机 Node.js 的内置 `node:test`，不安装 npm 包，生产不需要 Node。

```bash
node --test tests/frontend_regression.cjs
```

浏览器离线回归入口仅用于测试（先停止其他占用8000的服务，或选择其他端口）：

```bash
python -B -m uvicorn tests.preview_app:app --host 127.0.0.1 --port 8000
```

离线入口在应用导入前安装隔离，再替换仓储/适配器，启动预热也使用替身。`GET /test/offline-status` 仅存在于测试应用，可检查被阻断的资源访问尝试是否为空。商品、账户、订单和本机购买链接均是假数据，**不可作为生产入口部署**。生产入口不导入该测试隔离。

`.claude/launch.json` 中两个启动配置也都只供开发验证：`mobile-web-offline-smoke` 使用上述假数据应用；`mobile-web-production-smoke` 使用真实 `run.py`/`main:app`，但由测试启动器注入假配置、关闭预热并阻断真实服务。后者只验证入口/静态内容，首页请求真实联盟会被故意阻断，不能用它部署业务。正常本地/云端运行请使用 `run.py`，不经过这些测试启动器。

回归重点覆盖：源 TTL 临界、可信身份与缓存别名、推广热命中零仓储访问、当前价格文案、纯缓存记录不上榜/不作详情降级、真实零值、UTC 推广截止时间、完整启动生命周期、会话/卸载后的迟到响应。Node VM 回归不能替代真实 Vue 模板及窄屏浏览器验证；内存 Mongo 替身不能替代真库执行计划检查。

验收时请分开记录：① `--check` 纯本地配置/结构校验；②显式 `--check-mongo` 只读连接验证；③真实入口 `--no-warmup` 的 `/health`、页面与静态资源；④测试替身下的离线业务回归；⑤经批准的真实平台联调。前四项不能证明真实联盟授权权限、商品时效、订单归因或最终佣金结算正确。默认预热和真实业务访问仍可能触发既有 Mongo 索引/写入及 PDD 请求，应在受控环境单独验证。

### 真实入口的独立性烟测

下列脚本只依赖生产 Python 包与标准库，不需要 pytest/httpx。它将白名单生产文件复制到仓库外的空格/中文路径，使用新进程、真实配置解析器、真实 ASGI 应用和日志代码进行验证；不使用 conftest 或离线预览应用，并阻断外部 socket。不会复制私有配置，也不会连接真实 Mongo/PDD。

```bash
python -B tests/standalone_smoke.py
```

### 2026-11-03 跨平台验收记录

Linux 项为本机 Docker 的 Debian amd64 容器，不是云服务器实机。另用嵌套目录中的公开假配置、假 `.env` 和假密钥文件实测 Docker 排除规则，确认它们均未进入 runtime 镜像。

| 环境 / 检查 | 结果 |
| --- | --- |
| Windows，独立新建 Python 3.12 venv，安装本目录固定依赖 | `pip check` 通过；后端 **306 passed**。 |
| Debian 12 / Python 3.11.18，非 root、运行期 `--network none` | 后端 **306 passed**。 |
| Debian 13 / Python 3.13.18，非 root、运行期 `--network none` | 后端 **306 passed**。 |
| 仅生产依赖的 Debian 12 runtime 镜像，不含私有配置和测试依赖 | 独立真实入口烟测 **4/4 passed**。 |
| Node.js 前端回归 | **51 passed**。 |
| 浏览器离线预览 | 首页到详情跳转、静态资源与健康接口可用；外部资源访问记录为空。 |
| 真实生产入口的隔离 HTTP 检查 | `/health`、详情与本地 Vue 为200；测试专用端点为404。 |

三个后端环境各有一条 Starlette 对 `httpx` TestClient 的弃用提示，不影响当前通过结果；这是开发测试兼容提示，生产不安装 httpx/pytest。Linux 构建只使用本目录作为 context；Docker Hub 的认证连接在本次网络环境中曾被重置，改用 Google 的 Docker Hub 公共缓存取得相同发行版 Python 基础镜像后完成验证，不是应用启动错误。网络受限时可显式选择该缓存，例如：

```bash
docker build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim-bookworm --target test -t mobile-web:test .
```

**本次没有登录用户云服务器，没有执行真实 Mongo ping、创建索引、写业务库或调用真实拼多多接口。** 以上证明源码/依赖和启动路径的独立、跨平台可用性，不替代云端实际网络、凭据权限、域名与 HTTPS 验证。
