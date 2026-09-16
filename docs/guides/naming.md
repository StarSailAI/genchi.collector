# 名称与专有词表

面向用户采用简体中文，优先翻译普通说明；作品、艺人、品牌和活动主题沿用固定通称或原名。一般贩售、事前贩售、先行、抽选、通贩、整理券、当日券等保留习惯用法。

## 唯一词表

维护文件：[`services/normalizer/src/genchi_normalizer/data/glossary.json`](../../services/normalizer/src/genchi_normalizer/data/glossary.json)。这是随 normalizer Python 包发布的配置，不依赖运行目录，也没有另一份复制到前端的词表。

- `version`：规则版本；修改词条时更新它，并重新部署 normalizer / product / notifier。
- `subjects`：作品 / 系列 slug 对应的首选展示名。
- `terms`：原名、简称、旧写法到固定展示名的映射。不同别名可以指向同一个名称。
- `protected`：保留原称的品牌、作品、艺人、票务产品或活动主题，不逐字翻译。
- `guidelines`：通用编辑规则。

例如：`学園アイドルマスター → 学园偶像大师`、`アニメイト → animate`、`アソビストア → ASOBI STORE`、`一般発売 → 一般贩售`。Love Live!、BanG Dream!、ねむらせ隊、-標- 等保留指定写法。

`genchi_normalizer.glossary.glossary_prompt()` 统一生成带版本号的提示内容。所有模型请求均包含整份通用词表：旧版信息抽取和票务相关性判断在 `_call_llm_json()` 注入，新版活动抽取在 `extract_text()` 注入，作为系统提示中的可信配置。网页正文仍是不可信数据。词表命中不代表活动相关性或事实已核验。

历史整条标题的已校对映射保存在 `services/product/src/genchi_product/data/reviewed-names.json`。这份文件用于确定性回填，不把数百条完整活动标题塞入每次提取提示；可复用的专名和别名应写入通用词表。

## 原文、展示与身份

- Activity：`title` 原始名，`title_zh` 展示名。
- Milestone：`title` 原始名，`title_zh` 展示名。
- Occurrence：`label` 原始名，`label_zh` 展示名。
- Subject：`name` 原始名，`name_zh` 展示名；历史写法保留在 `aliases`。

展示名可以包含专有日文或英文，不代表整串必须是汉字。网站、日程、邮件和 ICS 优先读展示字段；原文在详情中展开核对。原始采集资源、证据与旧表不会被翻译覆盖。

名称变化不改身份键、来源 ID、轮次键、时间精度、事实 revision、关注或参与状态，不生成活动变化邮件。不会用中文同名来自动合并活动或轮次。

模型使用 `title` 保存原名，`title_zh` 提交展示名候选。未审核模型译名不覆盖已发布名称。结构化来源在入库时应用确定性词表 / 已校对映射；未覆盖的日文名称进入独立名称审核，保留可以确认的部分和原称。

## 名称审核与清洗

SchemaContract 1.3 增量添加两个展示字段以及 `catalog_names`、`catalog_name_history`，保存原文、前后展示名、规则版本、方法和时间。

```bash
# 先备份数据库并执行增量迁移
# 默认只预览；报告包括每条名称的原名、现名和建议名
genchi-product normalize-names --output names-preview.json
# 确认规则后执行；重复运行不会重复产生名称修改历史
genchi-product normalize-names --apply --output names-applied.json
```

管理员工作台 `/zh-Hans/admin` 的“名称规范与审核”可搜索待核对 / 全部名称、分页查看并保存单条修订。API 为 `GET /admin/names` 与 `POST /admin/names`，仅管理员可用，禁用缓存。保存必须携带当前原始名称；原文过期返回 409。单条人工修订会在重复采集与词表重跑时保留；原文发生变化后重新评估。

更新通用词表后重新部署，再运行预览 / 回填。需要批量更正人工名称时，应逐项核对，不让自动规则悄悄覆盖它们。

## 边界

本轮清洗针对名称与命名结构。重复活动的事实归并、线上内容误分类、日期与地点的补全仍属于后续来源和事实治理；名称相同不是归并证据。

## 四语言展示投影

Schema 1.6 的 Product API 使用 `X-Genchi-Locale` 或 `locale` 查询参数产生展示投影。原始 title/name/label、规范 `_zh` 字段、原文证据和稳定 ID 保持不变；新增 `_localized` 字段供前端读取。繁体转换由后端 OpenCC s2twp 执行；日文与英文在没有已审核译名时展示官方原名，不自动生成英文专名或改写作品身份。中文说明的繁体投影使用 `_localized`，未翻译说明明确保留为来源内容。
