# e+ 动漫票务与 J-pop 试点

`genchi.eplus_ticket` 只读取 e+ 公开展示的票务信息，目标是建立事实性索引，不参与登录、
抽选、排队或购票。

## 发现与筛选

Fetcher 不调用 `/sf/search`。入口是 e+ 的七个公开「アニメ・ゲーム」地域分类页：

- 每天一轮，检查全部七个地域；
- 每个地域固定检查第一页，用于及时发现新条目；
- 同时沿分页游标向后检查一页，用于逐步完成当前库存回填；
- 从列表里的带销售条件链接统一还原成 `/sf/detail/<10 位 ID>`，避免把同一活动的
  场次或席种重复下载；
- 已确认的详情最多跟踪 2,000 个，每轮轮转复查其中 20 个，以更新受付状态。

每轮最多处理 80 个详情，先保留已跟踪页面的复查名额，其余发现结果按持久游标轮转，避免列表尾部长期没有机会被读取。该上限意味着单轮不是全库存快照。

少量人工核验过的公开作品词条可以通过 `seed_urls` 优先发现详情。默认先加入
`THE IDOLM@STER` 和 `アイドルマスター シャイニーカラーズ` 的 e+ 词条；这些是
`/sf/word/<数字 ID>` 公开页，不是被 robots.txt 禁止的搜索接口。种子用于提高重点企划的
时效性，不能替代地域分类的广泛发现。

默认请求间隔为 2.5 秒，并使用域名级单并发资源锁。限流交给调度器退避重试，失败任务不提交新游标。拥堵页或临时 HTTP 错误经有限重试后仍失败时，报告为 `partial` 并列出失败页面；失败的列表分页不推进游标，不能把部分成功当作完整验收。
已过期详情返回 404/410 时记录到任务报告并退出跟踪，保留已落库历史，继续处理其他详情；403 等访问错误仍会明确失败。分页失效则重置对应地域的分页游标。

分类页本身就是第一层高召回筛选。详情页再根据标题映射已知企划，例如偶像大师、
Love Live、BanG Dream、世界计划等；无法可靠映射的条目归入 `anime-general`，不会因为
关键词表不完整而丢失。关键词只负责归类，不负责决定是否下载分类页已经确认的动漫条目。

## 详情解析

普通 HTTP 是首选；仅当服务端 HTML 缺少活动结构时才通过私网 Camoufox 重新渲染。
保存的数据限定为：

- JSON-LD `Event` 中的活动名、开演/结束时间和场馆；
- 公演区块中的开场、开演时间；只公布日期时保留 DATE 精度，不补成 00:00；
- 受付名称、开始/截止时间、阶段和当前状态；
- e+ 原始详情链接和 Open Graph 图片链接；
- 为排查和搜索生成的精简事实文本。

不保存整页 HTML，也不复制介绍正文或图片文件。每个 e+ performance URL 形成一个稳定
Event；每个受付按“详情页、受付内容、performance”形成稳定 TicketWindow。重复执行只会
更新已有记录。

## 线上运行

所有真实抓取、浏览器探测和上游接口验收均在服务器执行。本机仅编辑代码和运行模拟、隔离数据库测试，保持 worker、normalizer、notifier 和 browser 停止。

配置位于 `config/sources.yaml`。在服务器的两仓库上一级执行：

```bash
python3 genchi.collector/deploy/manage.py compose backend build worker normalizer
python3 genchi.collector/deploy/manage.py apply
python3 genchi.collector/deploy/manage.py compose backend exec -T control allfeeds-control task-submit --source eplus-anime-tickets
python3 genchi.collector/deploy/manage.py compose backend logs --tail 100 worker normalizer
```

查看落库结果：

```bash
python3 genchi.collector/deploy/manage.py compose backend exec -T postgres psql -U genchi -d genchi -c \
  "SELECT final_status,finished_at,result FROM allfeeds.task_runs WHERE source_id='eplus-anime-tickets' ORDER BY finished_at DESC LIMIT 1;"
python3 genchi.collector/deploy/manage.py compose backend exec -T postgres psql -U genchi -d genchi -c \
  "SELECT j.status,count(*) FROM genchi.catalog_jobs j JOIN allfeeds.resources r ON r.id=j.resource_id WHERE r.source_id='eplus-anime-tickets' GROUP BY j.status;"
```

需要降低压力时，优先减少 `roots_per_run`、`pages_per_root` 和
`refresh_details_per_run`，不要降低 `rate_limit_seconds`。

## J-pop 独立试点

`eplus-jpop-tickets` 使用公开的 `/sf/live/j-pop` 分类页，独立于七个动漫地域入口。
它每日最多读取两页列表和六个详情，已知详情轮转复查两个；仍可能包含纯配信或混合
活动，所以分类页只用于发现，不被当作已经核实的线下演出证据。Streaming+ 纯配信
场次在产品层按场馆链接排除；同一详情页中仍保留实体场次。直接 HTTP 可能返回
拥堵页，Fetcher 使用同一受控 Camoufox 回退。

每条资源保留 `discoveryScope=jpop` 和原生详情、场次及受付信息。旧动漫相关性模型
不会把 J-pop 误判成“二次元无关”后丢弃；音乐资源进入产品批量 AI 二审候选，
确认实体演出、艺人／活动身份、场次与每轮售票的适用范围后自动显示。证据不完整的
候选继续待核；即便标题与现有主体别名匹配，也不能绕过二审。
首轮服务器手动任务成功提取 6 个详情、9 个场次和 49 个售票窗口，其中 4 个场次
是 Streaming+ 纯配信，已据此补充过滤规则。日调度以每轮最多 6 个详情开启，
继续保持全部音乐候选先审核后显示。扩大到其他音乐分类或平台需分别验收。
