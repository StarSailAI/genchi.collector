# 开发环境与验证

以下命令均从仓库根目录运行。需要 Python 3.11 或更新版本；CI 使用 Python 3.12 和 PostgreSQL 16。阅读代码、单元测试和配置校验不需要真实模型、X 登录或邮件凭据。

## 安装与本地配置

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e packages/contracts -e packages/sdk \
  -e services/controller -e services/worker -e plugins/builtin \
  -e plugins/genchi -e services/normalizer -e services/product -e dashboard \
  -e examples/custom-fetcher -e '.[dev]'
python3 scripts/init-local-env.py
```

初始化生成八个独立随机凭据，权限为 0600；已有 `.env` 时拒绝覆盖。它不会启动服务。外部集成留空，配置规则见 [开源配置](../operations/open-source.md)。不要运行 `source .env`，配置文件应按数据解析。

本地保持采集 Worker、normalizer、notifier 和采集浏览器停止。真实采集及上游验收在部署服务器执行，避免重复采集和通知。完整实例按 [单机部署](../operations/production.md) 安装；前端不包含在本仓库中。

## 检查

```bash
ruff check .
pytest -q
allfeeds-plugin validate --sources config/sources.yaml
allfeeds-control config-validate --sources config/sources.yaml
python3 scripts/check-doc-links.py
python3 scripts/check-public-release.py --history
```

未设置 `ALLFEEDS_TEST_DATABASE_URL` 时，数据库集成测试会跳过。需要完整验证时，为测试提供单独 PostgreSQL 数据库，并通过环境设置连接串；测试创建唯一临时 schema，清理时仅删除这些 schema。不要把生产数据库用作测试环境。

CI 执行同样的检查，并提供临时 PostgreSQL 服务。文档检查覆盖仓库内 Markdown 链接、图片路径和标题锚点，不请求外部网站。

## 构建分发包

```bash
python -m build packages/contracts
python -m build packages/sdk
python -m build services/controller
python -m build services/worker
python -m build plugins/builtin
python -m build plugins/genchi
python -m build services/normalizer
python -m build services/product
python -m build dashboard
python -m build examples/custom-fetcher
```

发布前阅读 [配置与历史扫描](../operations/open-source.md)。SDK 或协议修改说明兼容性，数据库修改新增迁移。详细约束见 [工程规则](engineering.md)，新 Fetcher 的接口和验收见 [插件开发](plugin-development.md)。
