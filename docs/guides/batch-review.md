# 批量 AI 审核目录候选

`genchi-product review-batch` 在采集与抽取后运行。它把多个当前版本的候选交给 DeepSeek **再次**核对，和第一次提取是两次独立调用。每次最多 30 项；按输入长度自动拆小批次，模型必须逐 ID 返回判定。一次运行默认最多处理 500 项；可重复运行，已判定的同版本候选不会重复消耗模型额度。查询只加载候选结构，需核验发布的正文来源按批读取原文，避免同一长页面随每个场次重复占用容器内存。运行时使用 normalizer 容器现有的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。

线上 `reviewer` 独立进程在模型配置齐全时自动运行，每 5 分钟最多处理 32 项，单次模型调用最多 16 项；空队列不调用模型。它不占用抽取进程，失败后保留已落库的批次，下轮继续。`BATCH_REVIEW_ENABLED=false` 可暂时关闭自动二审并保留队列；修改共用 `.env` 后执行部署 `apply` 生效。大量历史积压可用下面的命令分批加速；避免同时启动多个手动审核进程，以免重复调用模型。

判定分四类：

| 判定 | 落库行为 |
| --- | --- |
| `APPROVE` | 全部来源先进入二审。直属官网需有完整当前原文证据；票务平台需与当前原生场次及售票窗口逐字段一致；编辑来源需有原文中的外部活动／票务详情链接，社区来源还需唯一的已有活动匹配。线下会场、活动日期及其他事实明确、无多重精确匹配时自动发布并记录 `llm:review` 证据。其他情况保留待核。 |
| `OUT_OF_SCOPE` | 仅当模型判断与全部收录系列无关，且候选标题与所有证据均未命中已知系列别名时，自动拒绝；原文及候选仍保留。 |
| `MANUAL` | 留在待审队列，保存 AI 理由与硬校验结果。 |
| `REJECT` | 模型发现明确矛盾，但不会单凭模型判断删除／拒绝候选；留待人工确认。 |

原文已更新的旧候选不会发送给模型；`--apply` 将其标记为 `system:stale-source` 拒绝并保留审计记录。模型响应缺项、重复 ID、格式错误或截断时自动拆小批次重试；单项仍失败则留给人工。HTTP 错误中断本轮，先前成功批次保留，重跑从未完成候选继续。每项记录的 `payload.ai_review` 保存版本、模型、判定、理由及校验结果。审批前再次检查候选和原文版本；这一步不能由一个模型输出绕过。

J-pop 来源的艺人可能不在既有系列目录中；不能仅凭未命中系列判为范围外。若票务平台当前原生详情、艺人／活动名称、实体会场、开演日期与每轮售票的场次关系都清楚，DeepSeek 二审通过后可自动显示，无需等人工逐条放行；不清楚的候选继续待核，不显示。已知系列的结构化票务也先进入二审，不再绕过它直接发布。

在生产目录下执行：

```bash
# 只看积压规模，不调用模型、不写库
python3 genchi.collector/deploy/manage.py compose backend exec -T normalizer \
  genchi-product review-batch --limit 500

# 抽样调用模型，查看汇总但不写库
python3 genchi.collector/deploy/manage.py compose backend exec -T normalizer \
  genchi-product review-batch --source-type official_site --limit 20 --batch-size 10 --dry-run

# 保存审核结果；每次最多 500 项，重复运行直至 selected=0
python3 genchi.collector/deploy/manage.py compose backend exec -T normalizer \
  genchi-product review-batch --limit 500 --batch-size 16 --apply
```

`--dry-run` 和 `--apply` 都会列出最多 20 个判定示例，便于核对标题、理由及硬校验结果。先抽样检查自动发布和范围外拒绝的结果，再扩大运行批次。该命令可能触发目录变更和后续提醒；在生产部署后应检查审核计数、已发布活动及通知计划。只读计划显示 `stale` 和 `selected`，不计入已经保存过同版本 AI 判定的候选。
可用 `--source-type official_site`、`--source-type pia_ticket` 等按来源分阶段运行；不传时处理全部来源。过期候选清理始终覆盖全队列，不受来源筛选影响。
