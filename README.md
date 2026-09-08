# genchi.collector

Genchi 的采集、活动目录与提醒后端。持续收集日本二次元现地活动的官网、官方 X 和票务信息，把公告、抽选、售票、结果与开演整理成可追溯的时间线。

```text
官网 / 官推 / 票务 → AllFeeds 原始版本 → 结构校验 / 模型候选 / 人工审核
                                           ↓
                        活动 → 场次 → 时间节点 → 关注与邮件提醒
                                           ↓
                                      genchi.news
```

保留现有 12 个来源；新作品和艺人的采集范围仍由 `config/catalog.yaml` 手工启用。旧数据保留，活动更新使用稳定链接。

- `control` / `worker` / `cloakbrowser`：调度与私网采集。
- `normalizer`：数据库迁移、原文索引、活动处理 Worker。
- `product`：活动查询、邮箱登录、关注与审核 API。
- `notifier` / `mailpit`：提醒规划、可靠投递与本地测试收信。
- `dashboard` / `postgres`：运行状态与存储。

本机网站为 `http://localhost:13000/zh-Hans`，测试收信箱为 `http://localhost:18025`。

远程机器使用共用 `.env`、生产 Compose 覆盖文件和 Caddy 入口，参见 [单机部署与配置](docs/production.md)。

[启动、架构与验证](docs/product-v2.md) · [命名与词表维护](docs/naming.md) · [服务器配置建议](docs/hosting-size.md) · [数据源说明](docs/ticket-platform-sources.md) · [Agent 开发约束](AGENTS.md)
