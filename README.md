<p align="center">
  <a href="https://genchi.news">
    <img src="assets/genchi-mascot.webp" width="192" height="192" alt="GENCHI 看板娘" />
  </a>
</p>

<h1 align="center">Genchi Collector</h1>

**把日本二次元活动的公告、票务和官推，整理成可追溯的活动时间线。**

简体中文 · [English](docs/i18n/README.en.md) · [文档目录](docs/README.md) · [贡献指南](.github/CONTRIBUTING.md)

Genchi Collector 是 [genchi.news](https://genchi.news) 的采集、活动目录与提醒后端，基于 AllFeeds 分布式采集框架开发。它保存原始证据，将官网、官方 X、票务平台和经过审核的聚合来源统一为活动、场次与时间节点，供网站和 Agent 使用。

```text
官网 / 官推 / 票务 / 聚合来源
             ↓
     调度 → 采集 → 原始版本
             ↓
  结构校验 / 模型候选 / 人工审核
             ↓
   活动 → 场次 → 受付、结果、开演等节点
             ↓
    产品 API / Agent API / 邮件提醒
```

## 能做什么

- **保留来源与版本**：公告更新可追溯，重复采集不重复写入。
- **整理活动与票务**：区分活动、真实场次、申请轮次和时间精度；不确定内容进入审核。
- **分布式采集**：Controller 调度，Worker 执行，插件扩展来源；支持重试、租约和历史回填。
- **提供产品能力**：邮箱验证码登录、关注、日程、提醒，以及基于目录证据的问答。
- **开放接口**：网站通过 Product API 访问，Agent 可使用 REST 和 MCP。

本仓库提供后端、采集插件和只读运维 Dashboard。面向用户的 Next.js 网站属于独立的 `genchi.news` 项目。新作品和艺人由 [目录配置](config/catalog.yaml) 手工启用，采集范围与频率见 [来源配置](config/sources.yaml)。

## 从这里开始

| 目标 | 入口 |
| --- | --- |
| 阅读代码、离线开发 | [开发环境与验证](docs/development/getting-started.md) |
| 部署自己的实例 | [单机部署](docs/operations/production.md) |
| 配置密钥与准备发布 | [开源配置](docs/operations/open-source.md) |
| 理解数据与服务边界 | [产品模型](docs/architecture/product-v2.md) · [采集架构](docs/architecture/architecture.md) |
| 扩展采集来源 | [插件开发](docs/development/plugin-development.md) · [数据源文档](docs/README.md#数据源) |
| 使用接口 | [Controller API](docs/reference/api.md) · [Agent API](docs/reference/agent-api.md) |

本地配置可以用 `python3 scripts/init-local-env.py` 生成：命令只创建 `.env`，不会启动服务。生产部署使用独立的共享配置初始化流程，见部署文档。

## 仓库结构

```text
packages/       共享协议与插件 SDK
services/       调度、执行、浏览器、归一化与产品服务
plugins/        通用 Fetcher 与 Genchi 数据源
config/         目录与来源配置
dashboard/     只读运维界面
deploy/        部署与维护工具
scripts/       本地初始化与发布检查
tests/         单元与 PostgreSQL 集成测试
docs/          按主题组织的文档与历史归档
```

## 参与项目

请先阅读 [贡献指南](.github/CONTRIBUTING.md)；代码 Agent 从 [AGENTS.md](AGENTS.md) 开始。安全问题请按 [安全策略](.github/SECURITY.md) 私下报告。

[MIT 许可证](LICENSE) · [AllFeeds 来源与版权](docs/project/upstream.md) · [变更记录](docs/project/changelog.md) · [行为准则](.github/CODE_OF_CONDUCT.md)
