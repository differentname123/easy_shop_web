# mobile_web

移动端导购与推广佣金查询应用。支持 **拼多多、淘宝联盟、京东**，京东默认关闭，可仅启用京东。各平台复用实时和今日两个榜单、搜索、详情、转链、账户与订单页面；淘宝榜单由好单库提供ID并经官方核准，京东使用折京客协议及京东官方订单第二跳。启用方式、HTTP凭据传输限制及真实联调边界见 [多平台接入说明](PLATFORMS.md)。

## 功能全景与业务边界

| 功能 | 已有入口 | 实际行为 |
| --- | --- | --- |
| 平台及频道 | `GET /api/home/platforms`、`GET /api/home/channels` | 从适配器装配表取得平台名称、可用性与频道；未接入平台不能伪装成功。 |
| 官方推荐和搜索 | `GET /api/home/recommend`、`GET /api/home/search` | 联盟在线推荐/搜索，按平台、UID、分页与查询条件缓存；推荐摘要后台更新本地商品库。 |
| 本地商品目录（爆单库） | `GET /api/goods/list`、`GET /api/goods/categories` | 查询本地 Mongo 商品，不等于联盟实时搜索；支持平台、类目、关键词、排序和分页。 |
| 商品详情 | `GET /api/home/detail`、`/detail` | 摘要先展示；按内存、有效数据库详情、官方刷新、故障降级取得内容。 |
| 推广转链和分享文案 | `POST /api/promote/convert` | 平台负责生链，服务负责归因作用域与缓存；复制/购买时才请求，文案按本次标题和金额生成。 |
| 账户和资料 | `POST /api/user/register_or_login`、`GET /api/user/info` | 手机号密码注册/登录合一、令牌认证、脱敏资料；邀请码只记录既有邀请关系，不代表已实现邀请奖励。 |
| 推广授权 | `GET /api/user/auth` | PDD/TB查询本人备案状态，必要时生成授权链接；京东明确无需授权，不生成虚假已备案状态。 |
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
| 登录身份与推广归因 | 私有资料/订单以有效令牌 UID 为准；平台归因是另一作用域 | 保留原UID/JWT。京东个人推广用服务端持久数字 `promoter_id`，游客用配置的默认位；无需伪造授权。分享 URL 的 UID 不用于游客转链，也不能成为登录凭据。 |
| 订单日期和金额口径 | `order_time/order_time_basis/order_amount_basis` 明确来源 | PDD/TB按原付款时间/实付；JD按已知下单时间/计佣金额。不能将JD计佣金额或下单时间伪称实付/付款。 |

保留 `goods_id / product_id`、`brand / brand_name` 等既有字段别名，是为了兼容接口和历史数据；不为命名整齐而全局改名或迁移业务集合。推广接口允许匿名；个人归因以有效令牌为准，旧 `uid` 参数只能与令牌本人一致，不能再单凭请求UID生成个人渠道链接。

## 模块边界

```text
run.py                     单目录启动、本地校验、显式 Mongo ping
main.py                    HTTP 装配、生命周期、静态页面、日志
infrastructure/            Mongo、PDD/TOP网关、好单库/折淘客HTTP、配置与日志
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

## 以后如何接平台

1. 在 `adapters/` 实现现有 `BasePlatformAdapter` 契约。
2. 在 `adapters/__init__.py` 的显式装配表登记适配器。
3. 由适配器提供平台名称、可用性、频道、标识解析、规范化商品/订单及平台自身限制。

通用商品、推广、授权和订单编排不需要另复制一套。API 平台参数验证代码格式，实际可用性由装配表判断。`GET /api/home/platforms` 的元信息已被前端平台筛选消费。

首页、授权和订单页面消费同一平台元信息及能力字典，`authorization_required` 直接来自适配器的 `requires_authorized_promotion`，不等同于授权接口是否可用。已启用京东显示“无需授权”且没有授权按钮，不写 `auth_status.jd.is_authorized=True`。平台和会话隔离推荐 / 搜索缓存与迟到响应；商品、推广、订单凭据分别控制对应能力。历史本地库中的商品不代表对应平台在线能力已经启用。

## 商品身份、内容及缓存

### 身份不等于内容

- Sign-only 商品内部直接使用 `product_id = <goods_sign>`（无 `sign:` 前缀），满足 `(platform, product_id)` 唯一索引。除拼多多保留数字 `goods_id` 与加密 `goods_sign` 区分外，其他平台（淘宝、京东等）统一遵循 `goods_id == goods_sign == product_id`，下游消费与仓储持久化无需区分数字与非数字。
- 同平台后来获得真实 ID 时可更新原 Sign-only 文档；不带真实 ID 的摘要不会抹掉已有 ID。
- 同时提交 ID 与 Sign 不代表二者已验证为同一商品。详情按主标识查找，缓存别名只来自可信响应；推广的辅助 ID 只来自同平台身份投影查询。
- 摘要可保存上游可信 `product_url`，但不刷新详情来源时间。京东按ID转链只取同平台同商品的服务端物料链接，缺失时查询详情；仅明确数字SKU可构造规范商品地址。不透明itemId不能拼接 `item.jd.com`，客户端同时提供任意URL也不能覆盖可信映射，缺链接明确失败。
- 公开身份到物料URL另用有界短TTL缓存（与详情同为600秒、最多2048条），列表/搜索命中也会发布映射；因此关闭详情预取或后台摘要写入满载，不会立即丢掉刚取得的转链必要信息。它不是完整详情或个人推广链接；进程重启/过期后仍需数据库或上游证据。
- 推广持久缓存允许创建仅有身份的文档，但列表、类目和详情共用内容准入：实际非空名称及已知有效价格。价格字段按 `activity_price` 非空优先、否则 `price` 回退；真实0、数值字符串和 BSON 数值保持兼容。
- 内容条件在 Mongo 排序/分页之前执行，不能取一页后再过滤。裸文档不能在上游失败时作为零元详情降级；`get_product_identity` 不要求完整内容，仍可提供推广身份。

### 详情有效期只有一个来源时钟

- 摘要和详情共用字段映射；摘要只更新实际提供的值，不覆盖完整详情、品牌或类目。
- 详情使用独立 UTC `detail_updated_at`，来源寿命由 `PRODUCT_DETAIL_TTL` 管理。推荐摘要的 `updated_at` 不会延长它。
- 数据库详情回填内存后仍沿用 `detail_updated_at + TTL`；官方新详情以响应取得时刻计时，仓储保存同一抓取时间，不因写库延迟重新续期。
- 内存容器继续有界 LRU-TTL，同时检查私有来源截止时间。缓存键包含平台、UID、标识类型；不使用裸 ID 键。内部源时间和缓存信封不进入详情 HTTP 内容。
- 上游故障可返回有效陈旧内容并标记 `source=database_fallback`，但不把降级内容写入正常 TTL 缓存，以便恢复后立即重试。
- 真实零佣金、零券额保持零；未知保障、评分、库存保持空，不虚构补贴、最低价或服务承诺。

### 后台准备热门详情

- 后台复用同一套商品服务，不新增前端预取请求。启用后在启动及每5分钟准备各已启用平台前两个榜单首屏，各最多20件；不翻页、不扫描全库。
- 淘宝榜单已取得官方完整联盟详情，通过内部`detail_sink`传递公开快照，立即预热匿名详情内存，再异步保存Mongo；不会为了预取重复查询一遍详情。列表HTTP及推荐缓存仍只有摘要，不夹带内部快照、原始响应或推广数据。
- 拼多多仅对有Sign且缺失、过期或临近到期的商品补详情；后台不执行ID-only洗链。推荐、搜索和用户实际看到的本地列表也可提交前20件候选，冷门商品仍保留原有按需读取。
- 单个生命周期托管worker、最多128项任务（含在途）、原子去重、队满非阻塞丢弃、最多两次尝试。新推荐的全部摘要仍按小批量保存，也由该worker管理，不按商品创建线程。
- 临期阈值为周期加约30秒，后台提前刷新；前台继续使用尚有效快照。前后台同平台、同UID作用域、同类型主标识的在途请求单飞；匿名预取不混入个人推广上下文。
- Mongo详情使用来源时间CAS，内存比较/发布在同一锁内完成，旧预取不能回滚较新完整详情。写库失败只重试已有快照，内存可用及旧库降级不等于“详情已入库”。
- 关机拒收新任务、丢弃未执行项并等待在途调用结束；停止及退避可唤醒，但不能强杀同步HTTP，也不承诺固定秒数退出。import和服务构造不会自行启动worker。
- 冷启动首个用户、队列满和上游故障仍可能走按需查询，不能把该功能描述成所有历史商品永久秒开。多ASGI进程也不会共享预取去重。

| JSON字段 | 默认 / 行为 |
| --- | --- |
| `web_detail_prefetch_enabled` | 缺省跟随`web_warmup_enabled`，显式false关闭。 |
| `web_detail_prefetch_interval_seconds` | 默认300，允许60～300秒；详情来源有效期仍为600秒，不因刷新周期改写。 |

两开关均为true时执行主动首屏周期；显式prefetch=true但warmup=false时只处理用户列表触发，不主动拉榜。旧warmup=false配置缺省不新增后台请求。配置及后端代码修改后需重启/热重载对应进程；不会自动重启用户已运行的服务。离线测试配置显式关闭自动预取，专门测试注入完整替身后才开启。

### 推广只缓存生链结果，文案按当前请求生成

- PDD/TB转链只读本平台已保存的有效授权，不在热路径远程核验；游客、未授权及状态待确认的登录用户复用站长作用域并收到授权提示。京东不走授权分流：登录用户取服务端数字推广ID，游客取配置默认位；个人ID分配/转链失败明确报错，不能悄悄切换站长ID。缓存隔离平台、商品、UID、可信归因及默认联盟/推广位配置指纹；请求中的归因字段不能覆盖服务端身份。
- 缓存有效链接、唤起协议等兼容字段，不缓存本次标题/价格生成的 `command_text`。每次返回都由适配器按当前请求重建文案，历史缓存中的旧文案也不直接复用。
- 无有效链接、`is_fallback`及`cacheable:false`均不进正常缓存；平台口令凭证参与本次文案重建，返回值防御性复制，不能跨UID串链。
- 通用路径先查内存、再查数据库，均miss才匿名生链；有效且身份已知的匿名原始结果持久化到 `products.default_promotion`。未授权用户的 `need_auth` 等提示仅附加到本次响应，不污染共享缓存；个人结果不写公共库。URL-only 只使用内存 URL 作用域缓存，不捏造商品身份。
- `default_promotion_expires_at`按UTC保存，取公共上限三天与平台来源有效期中的较早值；淘宝口令来源未给有效期时不长效缓存。旧文档无可信截止时间或已过期则miss重建，不迁移或删除商品。
- 数据库推广结果回填内存也保留原截止时间，不重新续期。现有内存容器最多保留30分钟；截止时间不出现在 HTTP 响应。
- 详情页不长期保存成功转链结果，只合并同一完整 payload/会话的进行中请求。后续购买/复制再次请求后端，避免页面永久保留旧文案；卸载或会话变更后旧操作不能复制、导航或恢复账户。

## 查询与索引：已改什么、没有证明什么

| 查询 | 当前处理 |
| --- | --- |
| 商品唯一身份 | 保留唯一 `(platform, product_id)` 和原 `goods_sign` 索引；按同平台、有类型身份查询。 |
| 全平台/指定平台最新列表 | 保留 `updated_at DESC`、`platform ASC + updated_at DESC` 索引。 |
| 全平台/指定平台价格、优惠排序 | 补 `activity_price ASC`（兼容反序）和 `saved_price DESC + updated_at DESC`，各有带/不带平台前缀两种索引；不为全部类目组合铺索引。 |
| 数字推广身份 | `users.promoter_id` 部分唯一索引；建立失败时拒绝分配ID，不无约束降级写入。 |
| 列表读取 | 只投影摘要所需字段，不读取大段图文和推广载荷；推广身份及持久缓存也使用窄投影。 |
| 子串关键词搜索 | 保留正则转义、查询时限和分页上限；不锚定、忽略大小写的包含搜索不能靠普通 B 树索引直接解决。 |
| 订单汇总 | 保留旧付款日期索引，新增 `(platform, promoter_uid, order_time DESC, order_sn DESC)`；全量 `$facet` 汇总不受最多100条明细限制。混合旧数据在查询时兼容，无需维护脚本；不承诺新索引消除全部排序。 |

只补实际查询使用的少量排序索引，不铺类目等全部组合，不给 `products` 加会删除整条商品的缓存 TTL 索引，不增加 Redis 或搜索服务。索引初始化的数据库DDL按数据库/集合独立加锁，已初始化读取走快速路径，不再被其他集合建索引全局阻塞；失败可重试。关闭详情预取时，摘要后台写入最多同时两批、无排队，满载可观测跳过缓存性写入，关机仍等待在途操作。

**本轮没有连接真实数据库，不能断言生产缺索引或给出性能倍数。** 已验证的是代码查询形状、字段投影、索引声明，以及推广热命中不调用仓储。上线前应核对现有索引，评估首次索引创建负载，再在受控环境使用实际请求分布及 `explain(executionStats)` 检查默认排序、价格排序、类目和关键词查询；索引初始化仍沿用既有首次访问机制。

本地爆单库 `total_count` 保留探测下界语义，翻页使用 `has_more`；订单 `total_count` 是完整筛选集计数。保留 `products/users/orders` 集合和主要字段，仅新增轻量 `order_sync_state` 检查点，不删除历史数据或索引。

## 账户、授权与订单的正确性边界

- 手机号登录、令牌格式、旧密码哈希升级、脱敏输出及原认证头兼容保留。本人资料、授权、订单始终以令牌 UID 为权威。
- 页面会话必须由 token 验证；URL UID、缓存手机号不能当作登录身份。退出、换号、筛选后迟到的响应不能恢复旧账户或覆盖新结果。
- 登录证明站内身份；PDD/TB个人推广还须本平台已保存的有效授权，未授权时按既有规则使用通用链接并提示不计本人佣金。JD个人推广只用服务端数字ID，无需备案，不做个人失败转站长兜底；分享 URL 的 UID 不能成为个人推广身份。
- 正向授权（`is_authorized: True`）长期有效，不设人工24小时定时过期，转链直接复用本地备案渠道身份；上游返回 `AUTH_REQUIRED` 时仅本次降级并提示复核，不物理注销已有授权。明确未授权按 `AUTH_NEGATIVE_STATUS_TTL`（300秒）短缓存防刷。`/api/user/auth?platform=...&force=true&check_only=true` 用于主动/授权返回复查，绕过负缓存但不再生成邀请链接；平台故障不等于未授权，不覆盖已有状态。
- 淘宝与拼多多共用详情授权提示，可继续复制已有通用结果或直接进入 `/?tab=user&platform=...`。外部授权统一由我的页承接；完成授权后重新生链，不自动下单。拼多多每个有效UID只走一套智能生链（红包→标准）及必要URL洗链，不重复标准请求。
- 订单统计整个筛选集，不只统计返回的100条明细；退款/结算布尔字段优先，旧文档缺失时才用对应适配器的状态兼容规则。数据库错误不伪装零收益。
- `days=0` 查询本地全部历史，不代表同步了全部历史。`sync_status/sync_error/synced_count/last_sync_attempt_time` 区分尝试与成功，`last_sync_time` 只在完整窗口落库成功后推进；手动刷新不绕过冷却或锁。

### 明确保留、需要独立处理的限制

1. **订单同步是查询触发的有界增量同步**：首次最近23小时，每次至多5页、180秒冷却、按平台进程内锁。`order_sync_state` 持久化冻结窗口与页游标，预算用完报 `partial`，后续继续；完整窗口入库后才推进水位，后续重叠5分钟。没有常驻调度、全历史回补或多worker分布式互斥；无人访问账户不会自动同步。订单原子更新管线要求 MongoDB 4.2+。
2. **缓存与预取去重是进程内状态**：多ASGI worker不共享命中、失效或队列。详情预取和推荐批量写已由lifespan托管，关闭时等待在途工作退出；不引入分布式缓存或可靠任务投递系统。
3. **共享数据库的采集工作流**：在线写入仍保留 `format_retry_count=999` 等隔绝离线清洗的既有标记；父项目采集器使用的匿名 `promotion_url` 与本应用推广缓存不是可直接混用的个人归因链接。数据库数据契约仍需兼容，但部署和运行不依赖父项目采集器文件；本轮不更改其工作流。
4. **PDD 网络兼容策略**：SDK/传输实现已迁入本地 `infrastructure/`；JSON `pdd_dns_mode=auto` 保留Windows网络优化，Linux 默认走原生 DNS；代理环境的默认信任策略也区分 Windows/Linux。不是新增平台能力，也不代表已经验证所有网络环境。
5. **没有资金钱包**：预估商品佣金、订单佣金、平台已结算状态均不能证明本系统有可提现资产；不承诺固定到账或提现日期。

## 单目录运行：依赖与配置

**复制整个 `mobile_web` 目录即可独立运行，不需要复制父项目或安装父级 requirements。** 参考环境为 Python **3.11+**：Debian 12 默认 Python 3.11，Debian 13 默认 Python 3.13；先确认实际系统版本，不假定所有 Debian 都使用同一 Python。生产直接依赖仅为 `fastapi / uvicorn / pydantic / pymongo / requests / cachetools`，直接依赖版本见 `requirements.txt`，完整传递依赖通过 `constraints.txt` 固定（安装时自动使用）；不需要 GUI、AI SDK、Node/npm、gunicorn 或单独的 `bson` 包（BSON 由 PyMongo 提供）。`requirements-dev.txt` 额外安装 pytest/httpx，仅供测试。

Mongo 已在 Debian 云服务器部署，沿用 Windows 当前的 Mongo 主机、端口、账号、密码、数据库和认证库配置。**本方案不部署、搬迁或初始化另一套 Mongo，也不迁移集合、改历史时间或清理数据；没有 compose。** 拼多多仍需可用的联盟凭据和外网连接。

### 私有配置只初始化一次

- 跟踪的 `config.example.json` 是结构模板，不包含真实凭据，也不能直接作为可用生产配置。
- 默认私有配置是应用目录内的 `config.local.json`。`WEB_CONFIG_FILE` 可以指定其他文件；相对路径始终锚定 `mobile_web` 目录，不随启动命令的当前目录改变。
- **业务配置只读取 JSON；不使用同名环境变量覆盖。** `WEB_CONFIG_FILE` 或 `run.py --config` 仅选择JSON文件，监听 / 预热 / 日志也在JSON中配置。字段名称参照模板，不要直接复制整个父项目配置。
- 生产不会向上搜索父级配置。首次从旧环境迁入时，显式运行 `scripts/init_config.py --source <旧JSON绝对路径>`；它只提取Mongo、联盟及第三方凭据白名单和已有的安全认证密钥，**不覆盖已存在的目标文件**。
- 安全的已有 JSON `web_auth_secret` 认证密钥应继续沿用。缺少安全密钥时，迁入工具只在首次初始化生成并持久保存一个新密钥；原公开默认值不再接受，旧 token 需重新登录。不要在每次启动或部署时生成新密钥，也不要任意轮换有效密钥。
- 全新配置可按模板创建私有 JSON，填写实际凭据与至少32字符的随机认证密钥。不要把密钥、私有 JSON、`.env`、日志或证书提交到 Git 或打进镜像。

| JSON字段 | 含义 / 默认值 |
| --- | --- |
| `myself_pdd_client_id`、`myself_pdd_client_secret`、`myself_pdd_pid` | 拼多多联盟凭据和推广位；沿用原值。 |
| `pdd_enabled`、`taobao_enabled` | 启用平台；PDD默认启用。旧JSON未写淘宝开关时，仅在官方凭据 / 推广位完整时自动启用。 |
| 淘宝 / 好单库 / 折淘客字段 | 见 [PLATFORMS.md](PLATFORMS.md#配置淘宝)，密钥只放私有JSON。 |
| `local_mongo_host`、`local_mongo_port`、`local_mongo_user`、`local_mongo_password` | 现有Mongo连接，端口默认27017，MongoDB要求4.2+。 |
| `local_mongo_db_name`、`mongo_auth_source` | 业务数据库及认证库，与Mongo已有用户一致。 |
| `web_auth_secret` | 至少32字符的私有认证密钥，拒绝历史公开默认值。 |
| `web_host`、`web_port` | 模板设127.0.0.1:8000；解析器未配置host时默认0.0.0.0，公网部署须显式限制。 |
| `web_warmup_enabled` | 默认true；设false禁止主动启动/周期热榜，但真实业务API仍可能读写Mongo / 请求平台。 |
| `web_detail_prefetch_enabled`、`web_detail_prefetch_interval_seconds` | 后台公开详情准备开关及周期，详见上文；不生成个人推广或订单任务。 |
| `web_log_dir`、`web_log_level` | 日志目录默认runtime/logs；空字符串或`-`只写控制台；默认级别INFO。 |
| `pdd_dns_mode`、`pdd_trust_env` | DNS模式auto/on/off；是否信任代理环境默认Windows false、Linux true。 |

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

只检查真实入口的页面、静态资源和存活接口时，先在所选JSON设 `web_warmup_enabled:false`，再运行下条命令（与上条二选一）。这**不是离线模式**：访问真实业务API仍可能请求平台或写Mongo。

```bash
W:/project/python_project/easy_shop/app/mobile_web/.venv/Scripts/python.exe W:/project/python_project/easy_shop/app/mobile_web/run.py
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

服务模板启用了只读系统保护。若改为文件日志，需要先创建服务用户可写的 `runtime/logs`，修改JSON `web_log_dir` 并在unit中仅为该目录增加 `ReadWritePaths`；不要把整个应用目录改为运行时可写。

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

**容器中的 `localhost` 不是宿主机Mongo。** 若配置指向Debian宿主机回环地址，可用Linux Docker host网络沿用连接值。挂载的私有JSON必须设 `web_host:"127.0.0.1"`、`web_log_dir:"-"`，只供本机nginx访问；host网络不用`-p`。镜像中的旧WEB_*环境值不覆盖JSON。不要同时运行占同一端口的systemd应用和容器。

宿主服务用户的 `0600` 配置不一定能被容器 UID10001读取。以下命令为 Docker 准备独立的受限挂载副本；仅在首次切换 Docker 或有意更新配置时执行，不修改原配置、不生成新认证密钥。启用 userns-remap/rootless Docker 时，应按实际 UID 映射调整权限，而不是放宽为全员可读。

```bash
sudo install -d -o root -g root -m 0700 /etc/mobile_web
```

```bash
sudo install -o 10001 -g 10001 -m 0400 /opt/mobile_web/config.local.json /etc/mobile_web/config.local.json
```

```bash
sudo docker run -d --name mobile-web --restart unless-stopped --network host --mount type=bind,src=/etc/mobile_web/config.local.json,dst=/opt/mobile_web/config.local.json,readonly mobile-web:local python /opt/mobile_web/run.py
```

若原值本来指向远程 Mongo，可以用 bridge 网络并将8000只映射至宿主 `127.0.0.1`；如改连宿主地址，应在私有JSON明确配置 `local_mongo_host`，确认 Mongo 已有绑定、防火墙和权限后再用，不能假定容器能访问宿主回环 Mongo。

Windows Docker Desktop **不能照搬 Linux host 网络步骤**。访问 Windows 宿主服务通常使用 `host.docker.internal`，但 Mongo 必须已经允许来自该网络的连接；仅替换主机名不能解决只绑定回环地址或权限不通的问题。本项目 Windows 首选前述 venv 部署；Docker Desktop 网络按实际环境另行验证，不会自动变更 Mongo 配置。

在JSON设 `web_log_dir:"-"` 才会只写stdout/stderr；容器HEALTHCHECK仅查`/health`，是liveness，不是Mongo或联盟readiness。变更JSON监听端口须同步健康检查。`web_warmup_enabled:false` 仅禁止预热，不会把真实应用变成离线替身。

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

验收时请分开记录：① `--check` 纯本地配置/结构校验；②显式 `--check-mongo` 只读连接验证；③JSON禁用预热的真实入口 `/health`、页面与静态资源；④测试替身下的离线业务回归；⑤经批准的真实平台联调。前四项不能证明真实联盟授权权限、商品时效、订单归因或最终佣金结算正确。默认预热和真实业务访问仍可能触发既有 Mongo 索引/写入及 PDD 请求，应在受控环境单独验证。

### 真实入口的独立性烟测

下列脚本只依赖生产 Python 包与标准库，不需要 pytest/httpx。它将白名单生产文件复制到仓库外的空格/中文路径，使用新进程、真实配置解析器、真实 ASGI 应用和日志代码进行验证；不使用 conftest 或离线预览应用，并阻断外部 socket。不会复制私有配置，也不会连接真实 Mongo/PDD。

```bash
python -B tests/standalone_smoke.py
```

### 2026-11-06 京东接入验收记录

- 本次环境为现有 Windows Python 3.11.7（非项目固定依赖虚拟环境）；按要求移除可选维护脚本及其专用测试后，后端离线回归 **886 passed**，Node前端回归 **129 passed**。保留数字归因、JD协议、历史字段兼容和并发背压覆盖。
- 浏览器使用根目录 `mobile-web-offline` 配置、8076端口，检查移动/桌面展示、京东无需授权、下单时间/计佣标签、不透明物料转链、缺链接拒绝及账号切换。A/B两用户同商品分别使用不同数字ID，授权调用为0，外部资源拦截记录为空；所有商品和口令都是离线fixture。
- 复核后增加了摘要写入满载时物料映射仍可用、同秒来源修改精度防回退、北京时间展示的回归；不再提供游客历史订单认领、默认位绑定或可选维护脚本。
- 未连接生产Mongo、未执行真实迁移/默认位绑定、未使用实际密钥调用联盟，也未重做下方历史Docker/多Python版本验收。真实成交归因、结算到账和索引吞吐不在本次已验证范围。

### 2026-11-03 跨平台验收记录（历史，不代表本轮重测）

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
