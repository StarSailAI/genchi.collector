# 文档目录

Genchi Collector 的使用、开发与维护入口。命令默认从仓库根目录执行；历史报告与当前操作指南分开存放。

[项目首页](../README.md) · [English overview](i18n/README.en.md) · [贡献指南](../.github/CONTRIBUTING.md) · [Agent 入口](../AGENTS.md)

## 先选一条阅读路线

- **第一次阅读**：[产品模型](architecture/product-v2.md) → [采集架构](architecture/architecture.md)。
- **修改代码**：[开发环境](development/getting-started.md) → [工程约束](development/engineering.md) → 对应功能文档。
- **部署实例**：[配置与凭据](operations/open-source.md) → [单机部署](operations/production.md) → [浏览器服务](operations/browser.md)。
- **新增来源**：[插件开发](development/plugin-development.md) → [来源配置](guides/configuration.md) → 数据源专题。

## 使用指南

- [邮箱账户与个人中心](guides/accounts.md)
- [Configuration](guides/configuration.md)
- [邮件模板](guides/email-templates.md)
- [首页问答与倒计时](guides/home-assistant.md)
- [名称与专有词表](guides/naming.md)

## 架构与决策

- [Architecture](architecture/architecture.md)
- [用户系统决策](architecture/foundation-decisions.md)
- [Genchi v2：活动与提醒](architecture/product-v2.md)

## 开发

- [Engineering invariants](development/engineering.md)
- [开发环境与验证](development/getting-started.md)
- [Plugin Development](development/plugin-development.md)

## 接口与安全

- [Agent 接入](reference/agent-api.md)
- [HTTP API](reference/api.md)
- [Security](reference/security.md)

## 部署与运维

- [浏览器验证中断与恢复](operations/browser-verification.md)
- [网站访问：F / Camoufox](operations/browser.md)
- [Deployment and Scaling](operations/deployment.md)
- [第一版服务器建议](operations/hosting-size.md)
- [旧 genchi.news 数据迁移](operations/legacy-migration.md)
- [开源配置与发布前检查](operations/open-source.md)
- [单机部署](operations/production.md)

## 数据源

- [聚合来源接入与筛选](sources/aggregator-sources.md)
- [Catalog data quality](sources/data-quality.md)
- [e+ 动漫票务来源](sources/eplus-ticket-source.md)
- [Natalie：常驻验证与采集接入](sources/natalie-collection.md)
- [综合票务平台采集](sources/ticket-platform-sources.md)

## 项目资料

- [Changelog](project/changelog.md)
- [Upstream](project/upstream.md)

## 历史归档

[验收、审计与统计记录](archive/README.md) 保留日期和当时结论，仅供追溯，不作为新部署的成功证明。

## 文档放置规则

根目录只保留 README 和 AGENTS 两个 Markdown 入口。社区文件在 `.github/`；包级 README 留在对应源码目录。新增主题放到以上分类并更新本目录；报告进入 `archive/`。维护规则见 [AGENTS.md](../AGENTS.md#documentation-ownership)。
