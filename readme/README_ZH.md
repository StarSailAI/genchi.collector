> 本仓库已加入 Genchi 活动目录与邮件提醒产品。参见 [Genchi v2 运行说明](../docs/product-v2.md)。下文保留继承的 AllFeeds 框架参考。

Genchi 现支持根据活动资料进行 AI 问答，并每日精选演出、票务截止等倒计时。免费问答为全站所有用户共用每小时一次，详见 [功能说明](../docs/home-assistant.md)。

<p align="center">
  <img src="../assets/allfeeds-logo.png" alt="AllFeeds" width="380">
</p>

# AllFeeds

> 一个面向各种数据来源的分布式爬取工具。

[English](README_EN.md) | 简体中文 | [Gitee](https://gitee.com/StarSailAI/allfeeds)

## 一套爬虫，接入各种来源，随时扩展规模

抓取一个网页并不难。真正困难的是让几百个数据源持续更新、让失败任务自动恢复、让历史数据可以重新回刷，并且在任务变多时随时增加机器。

AllFeeds 是一套面向网站、RSS、JSON API、Sitemap 和自定义数据源的分布式采集系统。每个任务都会进入可观察的任务池，再由具备对应能力的 Worker 领取，最终被整理成结构统一的 Resource。

你可以先在一台机器上同时运行 Controller 和 Worker。任务积压时，再启动一台 Worker，它会立即加入集群并帮助处理同一个任务池。

![AllFeeds 运维总览](../assets/screenshots/dashboard-overview.png)

## AllFeeds 可以用来做什么？

- 建立持续更新的新闻、研究资料或市场数据管线。
- 建立同时保存最新内容和历史版本的网页资料库。
- 执行可以暂停、恢复和横向扩容的大规模历史回刷。
- 让不同 Worker 专门处理不同类型的数据来源。
- 把新的取数能力做成插件，而不是不断修改调度器核心。

AllFeeds 已经内置 RSS、单网页、列表详情、JSON API 和 Sitemap Fetcher。Fetcher、Sink 和 Asset Store 都可以继续通过插件扩展，让同一套框架适应不同领域的数据采集需求。

## 我们为什么这样设计？

### 任务注册和任务执行应该独立演进

计划负责回答“什么任务应该在什么时候运行”，Worker 负责回答“任务在哪里、以什么方式执行”。两者拆开之后，修改计划不需要关心机器，增加机器也不需要修改计划。

### 机器应该只是计算能力，而不是系统配置

Worker 会主动上报自己支持的队列、插件和可用槽位，Controller 只分配它能够处理的任务。日常可以用一台小机器常驻；需要批量回刷时，可以临时加入一台 24 核机器，无需搬迁数据，也无需重新配置任务。

### 增加数据源不应该让框架越来越复杂

Fetcher 是一个可以独立安装的 Python 插件，拥有明确的配置和统一的输出契约。调度器不需要理解每个网站的特殊逻辑，因此即使接入的数据源越来越多，核心框架仍然保持清晰。

### 失败必须可以看见，也必须能够恢复

每个任务都有优先级、尝试次数、超时、租约和执行历史。临时故障可以延迟后重新进入任务池，永久失败会被保留下来等待人工处理。PostgreSQL 是统一的事实来源，所有队列状态都可以查询和审计。

### 分布式采集必须允许安全重试

分布式任务天然是至少执行一次。AllFeeds 不回避这个事实：租约令牌会拒绝过期 Worker 的迟到结果，PostgreSQL Sink 使用稳定的数据源 ID 实现幂等写入，并在内容变化时保存版本历史。

## 这样的结构能带来什么？

| 使用场景 | 你需要做什么 | AllFeeds 提供什么 |
| --- | --- | --- |
| 日常持续采集 | 保持一台常驻 Worker 在线 | 稳定的资源占用和持续调度 |
| 突然出现任务积压 | 再启动一台 Worker | 新机器立即加入现有任务池 |
| 大规模历史回刷 | 创建 Backfill 批次 | 按窗口拆分、并行执行、暂停和恢复 |
| 上游服务不稳定 | 配置重试和并发上限 | 限制压力、延迟重试和死信记录 |
| 接入一种新数据源 | 安装一个 Fetcher 插件 | 无需修改 Controller，自动按能力路由 |
| 日常运维检查 | 打开 Dashboard | Worker、队列、错误、历史和数据新鲜度 |

## 这是一套可以真正运维的爬虫系统

只有能够判断“它是否正常工作”的爬虫，才是可用的系统。只读 Dashboard 把正在运行的任务、等待队列、错误、执行摘要、计划和 Worker 容量集中展示在一个页面中。

![AllFeeds 执行历史](../assets/screenshots/dashboard-history.png)

## 整套系统只有一个简单的心智模型

~~~text
数据源和 Backfill
        |
        v
Controller -> PostgreSQL 任务池 -> Workers
                                      |
                                      v
                           Fetcher -> Resource -> Sink
~~~

Controller 负责协调，Worker 负责执行，插件负责采集，PostgreSQL 保存事实。

## 从一台机器开始，需要时再扩展

~~~bash
cp .env.example .env
docker compose up -d --build postgres control dashboard
~~~

添加第一个 Source、注册第一台 Worker，AllFeeds 就可以开始采集。

- [部署说明](../docs/deployment.md)
- [插件开发](../docs/plugin-development.md)
- [架构设计](../docs/architecture.md)
- [Agent 二次开发指南](../AGENTS.md)

## 使用你的 Agent 二次开发 AllFeeds

AllFeeds 已经为编程 Agent 准备了完整的开发指南，涵盖架构、数据契约、插件接口和验证流程。在仓库根目录启动你常用的 Agent，然后直接告诉它：

> 请先完整阅读 `AGENTS.md`，理解 AllFeeds 的架构、任务生命周期、插件接口和开发约束，然后帮我实现：**在这里描述你的需求**。

对于大多数用户，这是扩展 AllFeeds 最直接的方式：你描述想要的结果，Agent 按照项目规范完成实现和验证。

[MIT License](../LICENSE)
