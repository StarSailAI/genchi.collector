# 综合票务平台采集

Genchi 将 e+、チケットぴあ和ローチケ作为日本综合票务的第一层数据源。采集器只读取无需登录即可查看的公开页面，不提交订单、不绕过验证码，也不访问会员或结算页面。

## 数据流

| Source | Fetcher | 发现入口 | 详情策略 |
| --- | --- | --- | --- |
| `eplus-anime-tickets` | `genchi.eplus_ticket` | 动漫地区页、作品关键词页 | HTML/JSON-LD，必要时使用 Camoufox |
| `pia-anime-tickets` | `genchi.pia_ticket` | 动漫首页、动漫/声优/游戏标签、轮换关键词 | 活动页发现销售页，销售页提取精确场次和受付时间 |
| `lawson-anime-tickets` | `genchi.lawson_ticket` | 动漫、声优、游戏和重点作品轮换关键词 | Camoufox 渲染搜索结果卡 |

三个 Fetcher 都输出平台无关的 `ticket_page`：

- `events` 保存公演、开场、开演和会场；
- `ticketWindows` 保存先行、抽选、一般发售、结果发表和状态；
- `nativeCategories` 和 `discovery` 原样保存平台分类、发现入口及检索词；
- `project:*` 标签由确定性作品关键词匹配得出，未命中时使用 `unknown`；
- 原始网页仍进入 AllFeeds 的 Resource/Version 历史，结构化投影保持幂等。

票务结果落入 Event 前会执行三级相关性判断：

1. e+ 动画分类、Pia 动画专区以及明确的动画、游戏、声优、2.5 次元原生分类直接通过；
2. 明确无关且没有正向信号的分类直接拒绝；
3. 罗森等平台通过宽泛检索发现的舞台、展览、演唱会等灰区结果交给 LLM 判断。

检索词只用于发现候选，不作为相关性证据。判断结果保存在
`ContentItem.metadata.ticketRelevance`，状态为 `accepted`、`review` 或 `rejected`。
未配置 LLM 或置信度不足的灰区结果保留原始内容和搜索能力，但不会写入 Event 和
TicketWindow。

归一化时，同平台使用平台活动 ID 保持稳定。同日期、同会场、标题高度一致且只有一个候选活动时，其他平台的 TicketWindow 会复用已有 Event；候选不唯一时保留独立 Event，避免把同日昼夜场错误合并。

## 本地运行

```bash
docker-compose up -d --build control worker normalizer
docker-compose exec control allfeeds-control task-submit --source eplus-anime-tickets
docker-compose exec control allfeeds-control task-submit --source pia-anime-tickets
docker-compose exec control allfeeds-control task-submit --source lawson-anime-tickets
```

三个 Source 默认每六小时运行。Pia 对普通 HTTP 失败使用 Camoufox 回退；Lawson 始终使用 Camoufox，并通过 `browser_api` 和域名资源锁限制并发。
