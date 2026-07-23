# genchi.collector

Genchi 的独立采集与数据处理项目。它基于 AllFeeds 调度框架，定时采集动漫音乐企划的官网和 X 官方账号，把原始记录保存到 `allfeeds` schema，再归一化到供网站只读查询的 `genchi` schema。

```text
官网 ───────────────┐
                    ├─ Worker ─> allfeeds raw ─> Normalizer ─> genchi curated
X ─> CloakBrowser ─┘                                      └─> SearchDocument
                                                                    │
                                                     genchi_reader ─┴─ genchi.news
```

当前已启用 10 个数据源：pilot 企划的官网与 X、ASOBI TICKET，以及 e+ 的
「アニメ・ゲーム」公开票务分类。后续官网和 X 项目分波次记录在 `config/catalog.yaml`，
默认需要人工启用；MVP 不采 YouTube，也不回填 X 历史。

## 服务

- `control`：计划任务、重试、租约和 Worker 注册。
- `worker`：执行官网、X 以及 ASOBI TICKET 公共票务 API Fetcher。
- `cloakbrowser`：私网内的持久浏览器服务，只暴露受鉴权的受限抓取接口。
- `normalizer`：原始内容归一化、搜索索引、可选 LLM 抽取与人工审核。
- `dashboard`：只读运行面板。
- `postgres`：唯一写库；`genchi_reader` 只授予网站所需的查询权限。

## 本地启动

```bash
cp .env.example .env
# 本地开发请把 .env 中的 change-me 换成随机值
docker-compose up -d --build
docker-compose ps
```

默认端口：PostgreSQL `127.0.0.1:5433`、控制面 `127.0.0.1:8060`、Dashboard `127.0.0.1:8050`。CloakBrowser 不映射宿主机端口。

网站单独从相邻的 `genchi.news` 启动；两个 Compose 项目通过外部 Docker 网络 `genchi-data` 和只读数据库用户连接。

## 常用操作

```bash
make test
make lint
make validate

docker-compose exec normalizer genchi-normalizer review list
docker-compose exec normalizer genchi-normalizer review show <candidate-id>
docker-compose exec normalizer genchi-normalizer review approve <candidate-id>
docker-compose exec normalizer genchi-normalizer review reject <candidate-id> --reason "原因"
```

未配置 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL` 时，归一化器仍会生成可搜索的原文内容；配置兼容 OpenAI Chat Completions JSON Schema 的接口后才启用结构化事实抽取。低置信度或校验失败的候选项不会自动写入 Event、Ticket 或 Release。

在 `.env` 中填写以下三项并重启 normalizer：

```dotenv
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
LLM_RESPONSE_FORMAT=auto
```

`LLM_RESPONSE_FORMAT=auto` 会为 DeepSeek 自动使用 `json_object`，其他兼容接口默认使用
`json_schema`；也可以显式指定其中一种格式。

```bash
docker-compose up -d --no-deps --force-recreate normalizer
docker-compose logs -f normalizer
```

启动日志出现 `llm_enabled=True` 即表示配置已经生效。密钥只通过环境变量进入 normalizer，不会进入 Source 快照或采集内容。

`asobi-ticket-booths` 每 15 分钟读取公开的 booth、reception 和 act 数据。明确的公演与
受付时间直接经过确定性校验写入 Event/Ticket；booth 说明和无法关联 act 的当前受付才交给
LLM，失败候选留在审核队列，不进入正式日历。

`eplus-anime-tickets` 每 6 小时轮转读取 e+ 公开的「アニメ・ゲーム」地域分类页。它不会
调用 robots.txt 禁止的站内搜索接口，也不会执行登录、排队或购票。JSON-LD 中的场次和
页面中的受付区间经确定性校验写入 Event/TicketWindow，详情不明确时才使用
CloakBrowser 回退。完整的发现、限速和回填策略见
[`docs/eplus-ticket-source.md`](docs/eplus-ticket-source.md)。

已有内容不会因为配置模型而自动重复消耗 token。需要重新解读时按 Source 显式入队，例如：

```bash
docker-compose exec normalizer genchi-normalizer reprocess --source idolmaster-news
```

## 数据和升级边界

- 只有本项目拥有 Alembic 迁移和数据库写权限。
- `genchi.news` 只验证 `SchemaContract` 主版本并读取 `genchi` schema。
- 从旧版网页数据库导入时使用 [`docs/legacy-migration.md`](docs/legacy-migration.md) 的备份、试跑和导入流程。
- 生产环境必须替换 `.env.example` 中的所有令牌与密码，并限制 PostgreSQL 和控制面绑定地址。

底层框架来源及同步基线见 [`UPSTREAM.md`](UPSTREAM.md)。开发约束见 [`AGENTS.md`](AGENTS.md)。

[MIT License](LICENSE)
