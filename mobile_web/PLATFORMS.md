# 多平台接入说明

## 一套业务流程，多个平台实现

```text
API（参数、令牌）
  → Service（商品缓存 / 授权 / 推广文案 / 订单同步与归因）
    → Adapter（平台商品ID、字段单位、频道、订单状态、分页）
      → Client（签名、HTTP、供应商错误）
    → Repository（Mongo持久化、唯一约束、聚合）
```

- `ProductService`、`PromoteService`、`UserService`、`OrderService` 不复制淘宝专用版本。
- `tb_client.py` 访问淘宝官方TOP（自营推广、升级详情、渠道备案），`hdk_client.py` 只取第三方榜单ID，`ztk_client.py` 负责第三方订单协议以及推手渠道高佣转链。客户端和适配器不查用户数据库。
- `adapters/__init__.py` 是唯一平台装配表；`/api/home/platforms` 返回实际启用状态及 `capabilities` 字典：`recommend/search/detail/promotion/authorization/orders`。前端首页、账户和订单据此展示。
- 京东已实现多级高可用榜单、搜索、详情、数字推广转链和增量订单适配，默认关闭。由 `jd_client.py` 处理京东宙斯官方网关本地签名直连 (`https://api.jd.com/routerjson`)、折淘客官方协议通道 (`open_jing_union_open_goods_query.ashx` / `open_jd_union_open_goods_bigfield_query.ashx`) 及降级兜底；不复制京东专用 Service、页面或集合。
- `authorization_required` 由适配器声明生成；“需要备案”“授权接口已配置”“平台已启用”是不同事实。京东无需备案，不生成假授权状态。

## 京东接入与数字推广身份

以 [test_jd_api.py](scripts/test_jd_api.py) 的基准实现为唯一协议依据，正式模块不导入示例，不使用其中的默认凭据。私有配置字段：

| JSON字段 | 作用 |
| --- | --- |
| `jd_enabled` | 默认false。可同时关闭PDD/TB，仅启用JD。 |
| `zhetaoke_appkey` / `myself_jd_union_id` | 商品榜单、搜索、详情必需；不要求淘宝sid。 |
| `myself_jd_position_id` | 默认数字推广ID；缺少时仅推广能力关闭。 |
| `myself_jd_app_key` / `myself_jd_app_secret` | 京东开放平台应用凭据；用于榜单与订单行接口本地 MD5 签名直连 `https://api.jd.com/routerjson`（缺少时榜单走折淘客中继，订单能力关闭）。 |
| `jd_api_base` | 默认 `http://api.zhetaoke.com:20000/api`，集中配置供应商中继；不能拿淘宝10001端口替换。 |

**多级高可用与防限流机制：**
1. **实时榜单 (`get_rank_goods`)**：优先本地计算 MD5 签名直连 `https://api.jd.com/routerjson` (`jd.union.open.goods.rank.query`)，异常时自动回退至 `open_jd_union_open_goods_rank_query.ashx`。
2. **关键词搜索 (`search_goods`)**：优先走京东联盟官方协议 `open_jing_union_open_goods_query.ashx` (`jd.union.open.goods.query`)，覆盖全网京东 CPS 商品且自带多张高清轮播图 (`imageInfo.imageList`) 与落地页 `materialUrl`；仅在异常或空结果时回退至 `api_quanwang.ashx`。
3. **列表即详情快照 (`DetailSnapshot`) 与禁预取 (`can_prefetch_detail=False`)**：京东榜单 (`imgList`) 与官方搜索 (`imageInfo.imageList`) 已携带完整轮播图、价格、优惠券与佣金，列表拉取时通过 `detail_sink` 直接生成 `DetailSnapshot` 写入内存与数据库，用户点击详情实现 **0 外部详情请求**；同时禁用后台盲目单品详情轮询，彻底避免触发折淘客防采集封禁。
4. **冷启动单品详情 (`get_goods_detail`)**：优先调用 `open_jing_union_open_goods_query.ashx` (`keyword=goods_id, pageIndex=1, pageSize=1`)，轮播图不足时联动 `open_jd_union_open_goods_bigfield_query.ashx` 补充图片与 `mainSkuId`，最后才以 `api_detail.ashx` 兜底。
5. **增量订单 (`query_orders`)**：优先本地使用 `jd_app_key` 与 `jd_app_secret` 计算 MD5 签名直连 `https://api.jd.com/routerjson` (`jd.union.open.order.row.query`)，彻底避免将 `jd_app_secret` 通过 HTTP 明文发送给第三方中继；仅在直连失败时回退至 `open_jing_union_openz_order_row_query.ashx` 两跳签名 URL 模式（禁止重定向并严格校验 `https://api.jd.com/routerjson` 白名单）。

- 保留站内 `uid` / JWT。首次本人JD转链时，服务端以部分唯一索引、条件写入及有界碰撞重试分配持久数字字符串 `users.promoter_id`；不由前端选择。
- 生链只使用 `positionId=promoter_id`，订单按 `positionId → users.promoter_id → uid` 反查，不要求 `auth_status.jd`，不用 `subUnionId` 猜身份。
- 游客使用配置默认ID，登录推手使用自己的ID。站长同样是普通账户，只有数字ID不同；个人分配/转链失败不回退站长位。
- 默认ID保留不参与自动分配。游客链接直接使用配置默认ID；推手订单只按真实回传的数字ID匹配，不提供游客历史订单认领、默认位绑定或人工回填流程。
- 推广链接中的数字ID、商品ID均是字符串，不能转JavaScript Number；分享URL或请求扩展字段不能覆盖服务端归因。
- 上游未证明链接/口令有效期，因此JD返回 `cacheable:false`，不套三天长期缓存。真实京口令字段保留到 `promotion_token`。
- 搜索/榜单的可信物料URL可保存为摘要 `product_url`。只有上游明确数字SKU或已核验物料链接可构造商品URL；不透明物料ID没有可信URL时明确失败，不能标题搜索猜商品。

京东订单按更新时间 `type=3`、上海时区、至多一小时窗口查询；行 `id` 是 `order_sn`，父单 `orderId` 单独保存。首次23小时需要至少23个窗口，单次5页预算通常会返回partial，需要后续查询触发续传；不保证一次刷新同步全部历史。

`validCode=3/17` 不是已结算，18表示平台报告已结算，27售后中不等同退款成功，28为退款。分别保留 `estimateFee/actualFee` 和真实零值，不按费率或调试脚本的70%分成回算。JD `order_time_basis=created`、`order_amount_basis=commissionable`，不冒充付款时间/实付；平台结算不代表站内钱包到账。

## 两个榜单

| 公共channel_type | 拼多多 | 淘宝 | 淘宝内部好单库参数 | 京东 |
| --- | --- | --- | --- | --- |
| `5` | 实时销量榜 | 实时销量榜 | `sale_type=1` | 2小时实时榜，sortType=1 |
| `1` | 今日销量榜 | 今日销量榜 | `sale_type=2` | 24小时热销榜，sortType=3 |

PDD仍使用 `pdd.ddk.goods.recommend.get` 的 `channel_type`，不是搜索排序 `sort_type`。其余旧频道不再提供，历史商品不删除。

淘宝榜单流程：好单库取得来源ID及榜序 → 官方升级详情按最多20个ID批量查询 → 按已核准ID恢复原顺序。第三方标题、价格或佣金不补成“官方商品”；核准漏项会跳过。`next_offset/next_cursor/has_more` 表示**来源分页进度**，不按最后展示条数猜测。前端传回字典游标时需JSON序列化。

商品ID始终为字符串。淘宝官方不透明ID使用`goods_sign`，`input_item_iid`首先是请求回声，不把任意opaque echo当原ID；榜单仅在本次HDK原ID与官方echo精确对应时补可信`goods_id`。不要把两个客户端入参当可信别名，或将大订单号 / 渠道号转JavaScript Number。

开启后台详情准备后，淘宝用内部回调交出本批已有的公开完整详情及实际来源时间，先预热详情内存再异步入库，避免重复官方调用。PDD仅后台补有Sign的缺失 / 临期详情。队列、周期、时效及关闭说明见[README](README.md#后台准备热门详情)。

## 配置淘宝

只编辑你自己的 `config.local.json`，公共模板见 [config.example.json](config.example.json)。**示例脚本中的默认凭据不会自动被正式代码读取**，也不应复制进源码或日志。

| JSON字段 | 作用 |
| --- | --- |
| `pdd_enabled` | 默认true。只部署淘宝时可设false，此时不要求PDD凭据。 |
| `taobao_enabled` | 显式启用淘宝。旧配置未写时，官方凭据和推广位完整才自动启用；公开模板默认false。 |
| `myself_taobao_app_key` / `myself_taobao_app_secret` | 淘宝联盟TOP应用凭据。 |
| `myself_taobao_pid` | 原有 `mm_站长_媒体_推广位` 格式PID，完整格式可提取末段adzone_id。 |
| `taobao_adzone_id` | 可单独指定，优先于从PID提取。 |
| `taobao_session` | 联盟大号授权session，用于私域渠道查询；不是普通用户登录token。 |
| `taobao_inviter_code` | 已有官方渠道邀请码；也可通过具有权限的官方接口取得。 |
| `haodanku_api_key` | 仅用于淘宝榜单选品。缺少时淘宝官方搜索 / 详情仍可用，但榜单明确不可用。 |
| `zhetaoke_appkey` / `zhetaoke_sid` | 折淘客渠道订单查询及推手渠道高佣转链。缺少时订单与渠道推广能力明确不可用，不伪成功。 |

启用但缺少官方必需配置时校验失败，错误仅报字段名。`WEB_CONFIG_FILE` 或 `run.py --config` 只选择文件路径，其他业务字段不从环境变量覆盖。保留现有 `web_auth_secret`，不要为接平台重建用户令牌密钥。

本地配置校验不访问平台或Mongo：

```bash
python mobile_web/run.py --check
```

正式服务启动时不自动修改私有配置，也不导入示例脚本；示例中的可用凭据可在用户授权后一次性迁入私有JSON，不能复制进源码或日志。若凭据曾被公开或提交，应由账号持有人核实后轮换。

## 用户渠道授权与推广

1. 用户先通过站内登录取得令牌。
2. `/api/user/auth?platform=taobao` 用令牌UID作为官方 `external_id` 查询备案。
3. 未备案时生成带 `externalId=UID` 的专属邀请链接，用户在淘宝完成授权。
4. 官方复查取得独立 `relation_id` 后，服务保存到 `users.auth_status.taobao.identity`，建立唯一约束。
5. 生链时使用该用户已核实的relation_id；订单回传relation_id再反查站内UID。

`external_id` 是查询上下文，不假设第三方订单会原样返回它；`relation_id` 不等于站内UID。缺少 / 过期的联盟session是平台凭据错误，不会误判为“用户未授权”。同一渠道无法绑定多个站内用户，旧的“已授权但没有渠道ID”记录需要复核。

推广接口允许匿名。个人推广必须发送 `Authorization: Bearer <token>`，旧JSON `uid` 若提供必须与令牌本人相同；分享URL里的UID不能成为身份。前端无法指定任意relation_id。编排层只读本平台已保存且有效的授权；未授权或状态待确认时不带个人UID，优先复用站长公共缓存，再按需匿名生链，并在本次响应附 `need_auth` 提示，不静默使用公共测试渠道。adapter仍拒绝缺少可信身份的个人调用。

主动重新校验或授权返回复查使用 `/api/user/auth?platform=taobao&force=true&check_only=true`，绕过负状态缓存且不重新生成邀请链接。详情的去授权按钮直接进入我的页，同一提示同时支持继续复制已有通用口令；正向渠道身份长期有效；上游明确返回 `AUTH_REQUIRED` 时仅本次降级并提示复核，不物理撤销已保存授权，避免通信抖动误伤用户。

精准生链必须核对官方返回的商品身份，不能用关键词第一条冒充原商品。采用双轨制分流：站长自营/未授权通用推广使用淘宝官方TOP物料升级接口生成；已授权带渠道推手推广调用折淘客高佣转链接口（传入 relation_id 与渠道专属 PID），优先券二合一官方链接并生成真实淘口令。共享服务在每次请求中重建标题 / 价格文案并保留 `promotion_token`。个人渠道身份指纹进入缓存键；降级、错误及 `cacheable:false` 不缓存。来源未明确口令有效期时不沿用默认三天长效缓存。

## 订单同步与佣金口径

- 仍由查询账户订单触发后台同步，没有常驻定时任务。打开“我的”、切换日期或刷新都可能触发，不是天然只读操作。
- 初次从最近23小时开始。之后从持久成功水位继续，带5分钟重叠；`days=0` 只查询本地全部历史，不保证平台全部历史已补齐。
- 每平台单独进程锁、180秒尝试冷却、每次最多5页。预算耗尽返回 `partial`，`order_sync_state` 保存冻结窗口、页码和不透明游标，下次 / 重启继续。
- 订单完整写入后才保存续页检查点，完整窗口才推进成功水位；失败不会显示已成功，允许幂等重放。仍按单worker部署，不提供分布式互斥。
- 折淘客按更新时间 `query_type=4`、渠道订单 `order_scene=2`、20分钟安全窗口查询。第二页起传 `position_index`，不是仅增加page_no。
- 订单用 `(platform, order_sn)` 幂等；淘宝 `order_sn=trade_id` 是商品级子单，`trade_parent_id` 另存，不能以父单覆盖多个商品。
- 未匹配渠道的订单保存为 `unmatched`，不猜归当前访问者；成功绑定后补归因。已确认归属不能被后续缺字段清空，可靠来源时间防旧页回滚新状态。
- Mongo原子订单更新管线要求 **MongoDB 4.2+**。上线前核对版本、现有索引和渠道唯一性，不清库或自动合并历史别名文档。

| 淘宝字段 / 状态 | 含义 |
| --- | --- |
| `12` | 已付款，佣金是付款预估。 |
| `14` | 已确认收货，商家尚未完成付佣，不能算已结算。 |
| `3` | 商家付佣完成、联盟已结算；不代表站内钱包到账。 |
| `13` | 失效，可能关闭 / 取消等，不能一概算退款。 |
| `refund_tag` | 维权标志，不能独立证明退款成功。 |
| `pub_share_pre_fee` | 付款预估收入。 |
| `pub_share_fee` | 结算预估收入，也不等于最终扣费后的净到账。 |

统一数据保留 `is_invalid/is_refund/is_settled`、付款预估、结算预估和 `commission_basis`。失效排除有效收益但不虚增退款计数；缺少结算口径不拿付款预估冒充。统计覆盖整个筛选集，明细最多100条。

**没有新增70%固定分成、资金入余额、支付、提现或出款。**

## 历史订单与性能调整

- 新写入使用 `order_time/order_time_basis/order_amount_basis`，保留旧付款字段。未补齐的PDD/TB记录在查询和显示时兼容回退真实付款日期，不伪造JD付款时间。
- 历史字段在正常查询时兼容，不新增可选迁移脚本，也不要求手动预检或回填才可使用京东。
- 订单内部按UTC时间戳存储，面向用户的日期统一北京时间；来源修改时钟保留上游毫秒/小数精度，不能截成整秒后让较旧退款/结算页回滚状态。
- 商品库按实际全站/单平台价格及优惠排序补少量索引；保留分页前内容有效性校验与两秒查询时限，不以先截断数据换速度。任意子串搜索仍不能由普通B树索引解决。
- 数字推广ID增加部分唯一索引；订单增加中性时间查询索引。每个同步页重复身份只查一次，无长期身份缓存。
- 建索引不再持有跨集合全局锁，已初始化读取直接返回；失败仍可重试。关闭详情预取后的摘要写入也有并发上限，满载可观测降载，不影响订单必达/检查点规则。
- 继续进程内有界TTL、详情single-flight/CAS和单worker部署，不新增Redis、分布式锁、钱包账本或调度框架。未连接生产库或取得实际执行计划，不能声称生产性能提升了固定倍数。
- 独立部署风险：默认文件日志目录与systemd `ProtectSystem=strict` 不配可写路径会冲突；部署时在私有JSON用 `web_log_dir:"-"` 输出到journald，或显式配置允许写入的日志路径。本轮不修改实际部署或私有配置。

## 新增其他平台

1. 独立客户端负责HTTP和签名，不调用仓储。
2. 实现 `BasePlatformAdapter` 并声明频道、能力、可调用商品标识、必须的渠道身份字段、最大订单时间窗。
3. 输出现有商品 / 推广 / 订单契约；供应商状态和单位只在边界转换，分页提供 `has_more/next_cursor`。
4. 在显式注册表登记并添加JSON配置；服务和前端消费元信息，无需复制业务流程。
5. 用离线协议fixture及现有缓存、授权、订单、前端回归覆盖，再用受控真实请求验收。

## 验证和尚需真实联调的部分

离线回归始终阻断HTTP / Mongo出口，浏览器使用 `tests.preview_app` 的内存替身；不能用假数据证明真实订单已归因。

真实上线前需检查：官方应用权限、实际商品ID跨接口对应、好单库榜单枚举 / 翻页、折淘客返回结构和金额单位，以及一组“本人授权 → 精准生链 → 带对应relation_id的真实订单”证据。

折淘客新版文档对 `signurl` 前后说明矛盾：实现支持直接订单JSON或受信HTTPS官方网关的签名URL两段请求，并严格拒绝任意URL / 重定向，但实际账号模式仍需验证。没有猜测 `signurl=0` 就一定能直返JSON。

JD真实联调尚需验证非默认数字ID的“登录 → 生链 → 成交 → positionId回传”闭环、物料ID跨接口一致性与结算样本；离线替身只能证明程序契约，不证明真实佣金到账。

来源：
- [折京客新订单查询](https://j.zhetaoke.com/user/open/open_dingdanchaxun2.aspx)
- [折淘客新版订单查询](https://www.zhetaoke.com/user/open/open_dingdanchaxun2.aspx)
- [淘宝官方渠道备案查询（含external_id回传）](https://developer.alibaba.com/docs/api.htm?apiId=37989)
- [淘宝官方订单详情](https://developer.alibaba.com/docs/api.htm?apiId=43755)
- [淘宝官方升级商品详情](https://developer.alibaba.com/docs/api.htm?apiId=64763)
