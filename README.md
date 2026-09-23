# Genchi Collector · 开源采集核心

这是从 Genchi Collector 中单独整理出来的**采集层**。它负责定时抓取公开来源、保留原始记录和版本，并通过插件接入 RSS、网页、列表页、JSON API 与 Sitemap。Controller 负责调度，Worker 负责执行，PostgreSQL 保存任务和采集结果。

这个仓库是可独立运行的开源版，不包含 [genchi.news](https://genchi.news) 的活动归一化、账号、订阅、通知、Agent API、私有浏览器会话或生产部署配置。采集到的网页**不会自动变成已经核验的活动**；如果要构建面向用户的活动目录，需要另行实现提取、去重和审核。

## 快速开始

需要 Docker Compose、Python 3.11+；默认只绑定本机回环地址。示例来源均为 `enabled: false`，启动服务不会自行抓取网站。

```bash
python3 scripts/init-local-env.py
docker compose config --quiet
docker compose up -d --build
curl http://127.0.0.1:8060/health
```

编辑 [config/sources.yaml](config/sources.yaml)，把示例 URL、选择器和调度改成你有权采集的来源，再将该 Source 设为 `enabled: true`。先验证配置，再重启 Controller 让它读取更新：

```bash
docker compose exec -T control allfeeds-control config-validate --sources /app/config/sources.yaml
docker compose restart control
docker compose logs -f control worker
```

管理接口要求 `X-API-Key: CONTROL_API_TOKEN`。令牌只保存在本地 `.env`，不要发到聊天、Issue 或日志；Controller 默认不对公网开放。详细使用方法见 [配置与运行](docs/getting-started.md)。

## 包含内容

| 目录 | 用途 |
| --- | --- |
| `packages/contracts`、`packages/sdk` | Source、Resource 协议与插件接口 |
| `services/controller` | 调度、租约、重试、Controller API |
| `services/worker` | 隔离执行、幂等 PostgreSQL Sink |
| `plugins/builtin` | RSS、网页、列表页、JSON API、Sitemap Fetcher |
| `examples/custom-fetcher` | 自定义 Fetcher 模板 |
| `config/sources.yaml` | 默认关闭的来源示例 |

插件开发见 [插件指南](docs/plugin-development.md)；架构与数据边界见 [架构说明](docs/architecture.md)。本项目源自 [AllFeeds](https://github.com/StarSailAI/AllFeeds)，[来源与许可说明](docs/upstream.md)记录了上游基点。[MIT 许可](LICENSE)。欢迎按[贡献指南](.github/CONTRIBUTING.md)参与；安全问题请按 [安全政策](.github/SECURITY.md) 私下报告。
