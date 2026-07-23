# 综合票务平台采集

Genchi 将 e+、チケットぴあ和ローチケ作为日本综合票务的第一层数据源。采集器只读取无需登录即可查看的公开页面，不提交订单、不绕过验证码，也不访问会员或结算页面。

## 数据流

| Source | Fetcher | 发现入口 | 详情策略 |
| --- | --- | --- | --- |
| `eplus-anime-tickets` | `genchi.eplus_ticket` | 动漫地区页、作品关键词页 | HTML/JSON-LD，必要时使用 CloakBrowser |
| `pia-anime-tickets` | `genchi.pia_ticket` | 动漫首页、动漫/声优/游戏标签、轮换关键词 | 活动页发现销售页，销售页提取精确场次和受付时间 |
| `lawson-anime-tickets` | `genchi.lawson_ticket` | 动漫、声优、游戏和重点作品轮换关键词 | CloakBrowser 渲染搜索结果卡 |

三个 Fetcher 都输出平台无关的 `ticket_page`：

- `events` 保存公演、开场、开演和会场；
- `ticketWindows` 保存先行、抽选、一般发售、结果发表和状态；
- `project:*` 标签由确定性作品关键词匹配得出，未命中时使用 `anime-general`；
- 原始网页仍进入 AllFeeds 的 Resource/Version 历史，结构化投影保持幂等。

归一化时，同平台使用平台活动 ID 保持稳定。同日期、同会场、标题高度一致且只有一个候选活动时，其他平台的 TicketWindow 会复用已有 Event；候选不唯一时保留独立 Event，避免把同日昼夜场错误合并。

## 本地运行

```bash
docker-compose up -d --build control worker normalizer
docker-compose exec control allfeeds-control task-submit --source eplus-anime-tickets
docker-compose exec control allfeeds-control task-submit --source pia-anime-tickets
docker-compose exec control allfeeds-control task-submit --source lawson-anime-tickets
```

三个 Source 默认每六小时运行。Pia 对普通 HTTP 失败使用 CloakBrowser 回退；Lawson 始终使用 CloakBrowser，并通过 `browser_api` 和域名资源锁限制并发。
