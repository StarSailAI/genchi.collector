# Genchi v2：活动与提醒

## 边界与模型

网站的核心是长期稳定的 Activity，下面有 Occurrence（真实场次）和 Milestone（公告、受付、结果、入金、物贩、开演等节点）。节点通过 scopes 关联一个或多个场次。同一节点更新时增加 revision，不生成新的活动 URL。

- `allfeeds` 保存原始资源、不可变资源版本和采集任务。
- `genchi.catalog_*` 保存活动、场次、节点、主体、证据、变化记录和审核候选。
- `genchi_private` 保存邮箱账户、令牌哈希、关注、用户参与进度和邮件队列；不授予 `genchi_reader` 访问权限。
- `services/product` 是唯一产品写入服务。Next.js 用同源代理访问它，不持有写库密码或管理员令牌。
- 旧 Event、TicketWindow、ContentItem、SearchDocument 保留。旧活动链接通过映射跳转到新活动。

主体可以有父子关系，例如偶像大师 → 学园偶像大师；关注父系列可选择包含子系列。活动类型支持演出、音乐节、快闪、咖啡、展览、见面会、物贩和其他，实体模型不绑定二次元，之后可接入 J-pop 艺人。

## 采集与审核

资源新增或 content_hash 变化后，由 PostgreSQL 触发器写入 `catalog_jobs`。Worker 使用带令牌的租约领取任务，在完成写入前再次检查版本和租约。

1. 先独立更新原文搜索索引；模型失败也不影响原文检索。
2. ASOBI / e+ / ぴあ / Lawson 的既有结构化适配器产物，经过时间、类型与主体校验后进入活动目录。无法确认主体的内容进入审核。
3. 其他官网与官推正文经过大模型抽取；每项活动及节点必须携带正文中的精确证据片段。模型输出只作为待审核候选，不直接修改已发布活动。
4. 管理员在 `/zh-Hans/admin` 查看证据，可修订候选 JSON 后发布或拒绝。原文已更新的旧候选不能发布。
5. 不同来源对节点发生冲突时进入审核；已核验的同一上游 ID 更新保留节点 ID 并增加 revision。

合并使用稳定来源 ID、精确标题的年度归属和明确的官方共同活动标识。ASOBI 的 booth 标识能把旧的多个 act 合并为一场活动，保留场次、关注和旧链接。不会因为日期连续就把离散演出猜成一段活动期间。跨来源名称差异仍需要整理和审核。

时间必须显式区分 TIME、DATE、TBD。TIME 必须有时区；DATE 不附加虚构的午夜。旧数据中的 JST 午夜保守转换成 DATE。历史导入证据标为未核验，不触发精确时间提醒。

## 邮件语义

邮箱登录链接 20 分钟内有效且只能用一次；会话有效期 30 天。登录邮件与业务邮件共用 outbox。未验证账户不会收到业务提醒。

- 新活动按账户时区每日 09:00 汇总。
- 已核验的开放受付、截止、开演分别产生通知；同一活动 5 分钟内的多项信息变化汇总成一封更新邮件，保留各项变化。已结束时间节点的补录不发更新邮件。
- 多个关注命中同一节点时，只生成一份提醒；单个活动的设置优先于系列设置。
- 提前提醒可选关闭、2、24 或 48 小时。
- 结果通知要求用户记录申请了对应轮次，不宣称用户已中选。
- 入金通知要求用户明确记录对应轮次已中选。
- 取消、延期、退订、取消关注、节点修订及偏好修改都在发送前重新检查。
- 数据库的 SENT 表示 SMTP 接受了邮件，不表示用户实际收到了。SMTP 超时或进程中断后标为 UNCERTAIN，不自动重发，以免重复投递。

日历导出支持活动日历和行动日历；行动日历分别导出受付开始与截止。日期未知的节点不伪造日历日期；日期已知、时间未知的节点为全天事件。

## 开发与线上运行

在 `.env` 中设置 `PRODUCT_SECRET`、`PRODUCT_ADMIN_TOKEN`（独立随机值），`PUBLIC_SITE_URL` 与网站 `NEXT_PUBLIC_SITE_URL` 必须一致。默认都为 `http://localhost:13000`。管理员邮箱由 `ADMIN_EMAIL` 指定，本地默认 `admin@genchi.local`。

```bash
# 生成两个不同的随机值，分别填入 .env；不要提交 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# 本机只启动数据读取所需服务，不启动任何采集或通知进程
docker-compose up -d --build --no-deps postgres control product

cd ../genchi.news
docker-compose up -d --build web
```

- 网站：`http://localhost:13000/zh-Hans`
- 本地收信箱：`http://localhost:18025`。任意邮箱发起登录后，都在这里查看邮件；默认不向真实外部邮箱投递。
- Product API：`http://127.0.0.1:18080`（只映射回环地址）。
- 原有控制台和采集端口由 `.env` 保持配置。

真实采集、浏览器探测、模型抽取和通知只在线上运行，部署步骤见 [单机部署](production.md)。本机保持 worker、browser、normalizer 和 notifier 停止。需要历史桥接时，先在线上备份，再有针对性地运行 `genchi-product import-legacy`、`index-raw` 或 `refresh-structured`；日常修改不重复执行全量回放。

日常修改后可单独构建所需服务。Next.js Docker 构建限制一个构建 CPU、512 MB Node 堆。

上线前需要配置真实 SMTP（远程连接必须 TLS）、发信域名、公共 HTTPS 域名，并整理试点系列的来源与审核积压。默认 Mailpit 仅用于本地验收。

## 操作与恢复

`/zh-Hans/admin` 展示审核队列、各来源最近观测时间、抽取任务和邮件队列计数。采集失败详情仍在原来的只读 Dashboard。来源最近观测时间不等同于每一次抓取成功。

模型暂时不可用时，原文继续可检索，失败任务会重试；终止失败进入审核。`refresh-structured` 只回放有原生结构的票务页面，不扩展来源范围。审核工作台可修改失败记录为完整 ActivityInput 后发布；需要认真核对证据和轮次。

备份数据库后再升级。迁移是增量添加，旧应用可继续读取旧表。不要通过删除 v2 表回滚，否则会损失关注和邮件状态。若邮件为 UNCERTAIN，先核对 SMTP 日志和 Message-ID 再做人工处置；当前版本不提供自动重试按钮。

## 验证

```bash
ruff check .
pytest -q
# 集成测试使用随机隔离 schema，结束后仅删除这些测试 schema
ALLFEEDS_TEST_DATABASE_URL=postgresql://... pytest -q
allfeeds-plugin validate --sources config/sources.yaml
allfeeds-control config-validate --sources config/sources.yaml
```

事务测试覆盖稳定 ID、时区等价、修订、跨活动票务隔离、多场次、官方分组归并、候选隔离、原文搜索、关注合并、一次性登录、越权、退订、取消、入金条件、提醒调整和 SMTP 不确定状态。

## 活动与日程读取契约

- `/activities` 新增 `from_date` / `to_date`（含首尾日）：按 JST 的真实场次日期与城市共同匹配，同活动只计一次；`sort=event` 用于出行查找。只公布日期的节点同样参与下一节点排序。
- `/me/activities` 返回 `{items,total,page,limit}`，按最近更新分页，系列关注包含配置的子系列、类型与城市；取消邮件订阅不隐藏个人日程。
- `/me/agenda` 接受 `from_date` / `to_date`（结束日不含，最多 31 天），返回按日期与活动分组的真实边界。另附跨越整个区间的持续安排、待定数量与期间变更。`total` 是日期 × 活动的组数，`action_count` 是日期事项数。
- 参与状态未记录时，结果 / 付款事项带条件提示；记录申请、中选或购票后，已完成的相应事项不再占据个人日程。邮件发送前执行同样的轮次判断；修改另一轮不会抑制本轮提醒。DELETE `/me/participation/{id}?round_key=...` 可恢复为未记录。
- `/calendar` 保留 ICS 使用的节点读取接口，新增 `total/page/limit`。网站循环取完分页，不静默截取前 2,000 项。

上述读取 API 与参与状态撤销沿用原有表结构；当前完整版本包含名称规范与收信转发迁移，SchemaContract 为 1.4，兼容网站的 1.3 读取契约。

## 名称规范（SchemaContract 1.3）

原始名称与展示名称分开保存，网站 / 日程 / 邮件 / ICS 共用展示字段。所有模型提取请求注入同一个版本化专有名词表；确定性规则无法处理的名称进入名称审核。名称修订不改变事实 revision，也不触发活动变更提醒。详见 [命名规范与词表维护](naming.md) 与 [第一版服务器建议](hosting-size.md)。

## 收信转发（SchemaContract 1.4）

Resend 收件进入独立私有队列 `genchi_private.inbound_mail`，由 notifier 转发到 `ADMIN_EMAIL`，与账户登录、关注及退订状态无关。邮件不进入活动目录或模型提取。回调签名、附件、幂等重试与运维命令见 [单机部署](production.md#resend-收信转发)。
