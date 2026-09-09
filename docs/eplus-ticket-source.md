# e+ 动漫票务来源

`genchi.eplus_ticket` 只读取 e+ 公开展示的票务信息，目标是建立事实性索引，不参与登录、
抽选、排队或购票。

## 发现与筛选

Fetcher 不调用 `/sf/search`。入口是 e+ 的七个公开「アニメ・ゲーム」地域分类页：

- 每轮选择两个地域；
- 每个地域固定检查第一页，用于及时发现新条目；
- 同时沿分页游标向后检查一页，用于逐步完成当前库存回填；
- 从列表里的带销售条件链接统一还原成 `/sf/detail/<10 位 ID>`，避免把同一活动的
  场次或席种重复下载；
- 已确认的详情最多跟踪 2,000 个，每轮轮转复查其中 20 个，以更新受付状态。

少量人工核验过的公开作品词条可以通过 `seed_urls` 优先发现详情。默认先加入
`THE IDOLM@STER` 和 `アイドルマスター シャイニーカラーズ` 的 e+ 词条；这些是
`/sf/word/<数字 ID>` 公开页，不是被 robots.txt 禁止的搜索接口。种子用于提高重点企划的
时效性，不能替代地域分类的广泛发现。

默认请求间隔为 2.5 秒，并使用域名级单并发资源锁。遇到 e+ 的
「混雑のお知らせ」、HTTP 5xx 或限流时，本轮保留游标并交给调度器退避重试。

分类页本身就是第一层高召回筛选。详情页再根据标题映射已知企划，例如偶像大师、
Love Live、BanG Dream、世界计划等；无法可靠映射的条目归入 `anime-general`，不会因为
关键词表不完整而丢失。关键词只负责归类，不负责决定是否下载分类页已经确认的动漫条目。

## 详情解析

普通 HTTP 是首选；仅当服务端 HTML 缺少活动结构时才通过私网 Camoufox 重新渲染。
保存的数据限定为：

- JSON-LD `Event` 中的活动名、开演/结束时间和场馆；
- 公演区块中的开场时间；
- 受付名称、开始/截止时间、阶段和当前状态；
- e+ 原始详情链接和 Open Graph 图片链接；
- 为排查和搜索生成的精简事实文本。

不保存整页 HTML，也不复制介绍正文或图片文件。每个 e+ performance URL 形成一个稳定
Event；每个受付按“详情页、受付内容、performance”形成稳定 TicketWindow。重复执行只会
更新已有记录。

## 本地运行

配置位于 `config/sources.yaml`。修改插件或配置后重建相关服务：

```bash
docker-compose up -d --build control worker normalizer
docker-compose exec control allfeeds-control task-submit --source eplus-anime-tickets
docker-compose logs -f worker normalizer
```

查看落库结果：

```bash
docker-compose exec postgres psql -U genchi -d genchi -c \
  "SELECT count(*) FROM genchi.\"Event\" WHERE \"sourceKey\" LIKE 'eplus:event:%';"
docker-compose exec postgres psql -U genchi -d genchi -c \
  "SELECT count(*) FROM genchi.\"TicketWindow\" WHERE \"platform\"='eplus';"
```

需要降低压力时，优先减少 `roots_per_run`、`pages_per_root` 和
`refresh_details_per_run`，不要降低 `rate_limit_seconds`。
