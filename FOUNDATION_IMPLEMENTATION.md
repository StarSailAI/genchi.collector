# 实现范围

Product API 负责认证挑战、共享限流、Session、用户资料与关注匹配。私有表通过有序迁移升级。Next.js 同源代理限制路由、Cookie 和来源信息，浏览器仅接收安全 Cookie。

前端提供邮箱发送验证码、六位验证码确认、重发冷却、错误反馈、个人资料和关注 Dashboard。旧日程与活动参与进度继续使用现有模型。

实现与验收结果完成后记录在 FOUNDATION_ACCEPTANCE.md。

## Agent 接入

Schema 1.8 在 `genchi_private` 增加 API Key 和脱敏调用审计表。Product API 暴露由浏览器 Session 保护的 Key 管理接口，以及使用 Bearer Key 的 `/agent/v1` REST、`/agent/openapi.json` 和 sessionless Streamable HTTP `/mcp`。REST 与 MCP 复用活动目录、`catalog_changes`、关注匹配和日程投影；不允许 Agent Key 进入账号、Session 或后台管理接口。

FoundationServices 的 API Key 模块没有可采用 Recipe，因此 `foundation.project.yaml` 不将其声明为已启用 Foundation 模块。项目自建实现及偏离原因记录在 `FOUNDATION_DECISIONS.md`，接口和运维边界记录在 `docs/agent-api.md`。
