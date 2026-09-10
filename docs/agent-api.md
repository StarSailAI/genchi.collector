# Agent 接入

Genchi 的目标仍是帮助用户发现和参加喜欢的现场。Agent API 是网站、邮件之外的个人接入方式，让用户自己的 Agent 查询结构化活动、读取变更、管理关注和整理日程。

## 创建密钥

用户登录 `https://genchi.news` 后，在个人中心的「连接你的 Agent」中创建 API Key。完整 Key 只显示一次，不通过邮件发送，也不能从 Dashboard 再次读取。遗失时应撤销并新建。

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
| GET | `/agent/v1/activities` | `activities:read` | 搜索最新活动 |
| GET | `/agent/v1/activities/{id}` | `activities:read` | 读取完整时间线和证据 |
| GET | `/agent/v1/updates` | `updates:read` | 从签名游标继续读取变化 |
| GET | `/agent/v1/agenda` | `agenda:read` | 读取关注范围内的日程 |
| GET | `/agent/v1/subscriptions` | `subscriptions:read` | 查看关注 |
| POST | `/agent/v1/subscriptions` | `subscriptions:write` | 新建或更新关注 |
| DELETE | `/agent/v1/subscriptions/{id}` | `subscriptions:write` | 删除关注 |

持续同步必须保存 `/updates` 返回的 `next_cursor`。`mode=following` 只返回与当前关注匹配的活动，`mode=all` 返回全站发布活动的变化。所有业务时间以 `Asia/Tokyo` 为准；`DATE` 和 `TBD` 精度不能被 Agent 补成具体时刻。

## MCP

支持远程 Streamable HTTP MCP。以 Codex 为例：

```toml
[mcp_servers.genchi]
url = "https://genchi.news/mcp"
bearer_token_env_var = "GENCHI_API_KEY"
```

MCP 暴露 `search_activities`、`get_activity_timeline`、`get_latest_updates`、`list_subscriptions`、`create_subscription`、`delete_subscription` 和 `get_agenda`。读工具带只读标记，写工具保持可识别，便于客户端应用自己的审批策略。

## 安全与运行

- 每个账号最多五枚有效 Key，默认同时执行每 Key 每分钟 60 次和每账号每分钟 300 次限制。
- Secret 使用带服务器密钥的 HMAC-SHA256 摘要保存；比较使用常量时间函数。
- 禁用账号、过期 Key 和撤销 Key 立即失效。
- 审计只记录 Key ID、账号 ID、HTTP 方法、路由、状态码、耗时和请求 ID。
- Nginx 与应用日志不得记录 Authorization；Agent 接口禁止浏览器跨源写入。
