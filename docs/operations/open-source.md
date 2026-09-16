# 开源配置与发布前检查

本仓库采用 MIT 许可证，保留 [LICENSE](../../LICENSE) 和 [上游说明](../project/upstream.md) 中的原始版权及 AllFeeds 来源。公开官网、公开采集地址、Docker 服务名和测试用保留地址是代码的一部分；它们不是生产凭据。`config/catalog.yaml` 和 `config/sources.yaml` 是 Genchi 的公开领域配置，部署前应审阅其启用范围。

## 配置放在哪里

| 内容 | 位置 | 是否提交 |
| --- | --- | --- |
| 本地配置模板 | `.env.example` | 是，只含空密钥和本地示例 |
| 生产配置模板 | `deploy/production.env.example` | 是，只含空密钥和示例 |
| 本地真实配置 | 仓库根目录 `.env` | 否 |
| 生产真实配置 | 两仓库上一级 `.env` | 否，权限 0600 |
| 派生配置、迁移前 env、Git 镜像 | 部署根目录 `.deploy/` | 否 |
| X Cookie、第三方凭据 | 部署根目录 `secrets/` | 否 |
| 数据库备份、生产日志、用户数据、原始邮件 | `backups/`、`runtime/` 或仓库外私有目录 | 否 |
| 私有验收报告、生产截图 | 仓库外或被忽略的 `private/` | 否 |

Git 和 Docker 排除规则覆盖上述运行目录、常见私钥、Cookie、Worker 身份、数据库快照及 Git bundle。不要通过压缩整个工作目录的方式发布源码；忽略规则不能保护被手动打包、强制添加或已存在于历史中的文件。

本地初始化（只创建配置，不启动服务、采集或发送邮件）：

```bash
python3 scripts/init-local-env.py
```

八个内部密码／令牌使用独立随机值，文件权限为 0600；已有 `.env` 时拒绝覆盖。外部模型和邮件凭据保持为空。网站项目的 `PRODUCT_PROXY_SECRET` 必须与后端一致，并仅保存在服务端。基础 Compose 中的 `genchi-local-*` 回退值仅供历史本地开发兼容，不可用于生产；复制模板后应先初始化真实随机配置。

生产使用 [单机部署](production.md) 的 `deploy/manage.py init --site-url ...`，它负责生成共享配置，随后 `check` 校验。不要把生产 `.env` 覆盖成本地模板，也不要在命令行或提交信息中填写密钥。同步 Git bundle 时显式传入 `--branch`，不再绑定开发者分支。

## 发布前执行

```bash
python3 scripts/check-public-release.py --history
# 可选：与本地已知密钥比对，输出只有文件位置和规则名
python3 scripts/check-public-release.py --history --secrets-file .env
python3 scripts/check-doc-links.py
ruff check .
pytest -q
allfeeds-plugin validate --sources config/sources.yaml
allfeeds-control config-validate --sources config/sources.yaml
```

检查器扫描当前已跟踪和未忽略的新文件；`--history` 额外扫描本地全部引用可达的 Git 文件对象。CI 获取完整历史并执行检查。它会阻止常见私有文件名、部分常见服务令牌／私钥格式，以及显式传入的 env 密钥原值或 URL 编码值；不会打印命中的值。

这是有明确范围的防误提交检查，不能识别所有供应商密钥、任意硬编码密码、截图内容、不可达对象或远端未获取的引用。正式公开前还应对计划发布的全部分支／标签运行完整密钥扫描并人工审阅。历史中若发现真实密钥，先撤销／轮换，再清理历史或从审阅后的源码建立新仓库；仅删除当前文件或增加 `.gitignore` 不够。不要直接公开带私有历史的 Git bundle。

仓库现有 `docs/archive/` 中的报告与验收记录 是历史开发／采集验收资料，不代表新部署已通过验收。它们不能用于存放用户记录、真实邮件、生产截图或凭据。安全问题按 [安全策略](../../.github/SECURITY.md) 私下报告。

历史扫描与首次 GitHub 发布记录见 [2026-09-16 开源发布归档](../archive/2026-09-16-open-source-release.md)。
