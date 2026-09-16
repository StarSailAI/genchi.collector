# Agent 接入

Genchi 的目标仍是帮助用户发现和参加喜欢的现场。Agent API 是网站、邮件之外的个人接入方式，让用户自己的 Agent 查询结构化活动、读取变更、管理关注和整理日程。

公开的 [Genchi Agent Skill](https://genchi.news/SKILL.md) 说明安全连接、定时增量检查、游标保存、静默无更新和关注管理。用户可以先让 Agent 阅读该纯文字地址，再通过平台的 Secret Store 或环境变量提供独立 API Key。

## 创建密钥

用户登录 `https://genchi.news` 后，在个人中心的「连接你的 Agent」中创建 API Key。完整 Key 只显示一次，不通过邮件发送，也不能从 Dashboard 再次读取。遗失时应撤销并新建。

新 Key 默认只读，包含活动、更新、日程和关注读取权限。只有用户在创建时明确启用「允许这个 Agent 管理关注」，才增加 `subscriptions:write`。建议每个 Agent 单独创建并命名一枚 Key，不在聊天回复、URL 或日志中重复完整密钥。

请求使用标准 Bearer 认证：

```http
Authorization: Bearer gch_live_<key-id>.<secret>
```

生产入口：

- MCP：`https://genchi.news/mcp`
- REST：`https://genchi.news/agent/v1`
- OpenAPI：`https://genchi.news/agent/openapi.json`

## REST

| 方法 | 路径 | Scope | 作用 |
| --- | --- | --- | --- |
| GET | `/agent/v1/me` | 任一有效 Key | 检查 Key 名称、Scope 与有效期 |
| GET | `/agent/v1/activities` | `activities:read` | 搜索最新活动 |
| GET | `/agent/v1/activities/{id}` | `activities:read` | 读取完整时间线和证据 |
| GET | `/agent/v1/updates` | `updates:read` | 从签名游标继续读取变化 |
| GET | `/agent/v1/agenda` | `agenda:read` | 读取关注范围内的日程 |
| GET | `/agent/v1/subscriptions` | `subscriptions:read` | 查看关注 |
| POST | `/agent/v1/subscriptions` | `subscriptions:write` | 新建或更新关注 |
| DELETE | `/agent/v1/subscriptions/{id}` | `subscriptions:write` | 删除关注 |

持续同步必须保存 `/updates` 返回的 `next_cursor`。新监控首次使用 `cursor=now` 获取当前高水位，不返回历史变化；后续使用签名游标增量读取。没有匹配变化时游标仍推进到本次扫描高水位。`has_more=true` 时继续分页，完整处理成功后才保存最终游标。`mode=following` 只返回与当前关注匹配的活动，`mode=all` 返回全站发布活动的变化；两种模式分别保存游标。所有业务时间以 `Asia/Tokyo` 为准；`DATE` 和 `TBD` 精度不能被 Agent 补成具体时刻。

## MCP

支持远程 Streamable HTTP MCP。以 Codex 为例：

```toml
[mcp_servers.genchi]
url = "https://genchi.news/mcp"
bearer_token_env_var = "GENCHI_API_KEY"
```

MCP 暴露 `get_connection_info`、`search_activities`、`get_activity_timeline`、`get_latest_updates`、`list_subscriptions`、`create_subscription`、`delete_subscription` 和 `get_agenda`。`create_subscription` 支持提醒提前量、包含子系列、活动类型和城市过滤。读工具带只读标记，写工具保持可识别，便于客户端应用自己的审批策略。

## 定时检查契约

Agent 平台必须同时具备定时运行、持久状态与用户消息渠道，阅读 Skill 本身不会自动获得这些能力。建议新建任务时先以 `cursor=now` 初始化，然后每天读取关注更新；无变化时保持安静，网络或服务错误不能解释成“没有更新”，也不能推进游标。标准提示词和错误处理规则以公开 Skill 为准。

## 安全与运行

- 每个账号最多五枚有效 Key，默认同时执行每 Key 每分钟 60 次和每账号每分钟 300 次限制。
- Secret 使用带服务器密钥的 HMAC-SHA256 摘要保存；比较使用常量时间函数。
- 禁用账号、过期 Key 和撤销 Key 立即失效。
- 审计只记录 Key ID、账号 ID、HTTP 方法、路由、状态码、耗时和请求 ID。
- Nginx 与应用日志不得记录 Authorization；Agent 接口禁止浏览器跨源写入。
