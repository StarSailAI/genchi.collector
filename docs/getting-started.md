# 配置与运行

本仓库只运行采集核心。请勿把生产 Genchi 的数据库、密钥或 Source 清单复制进来。

## 本地运行

在仓库根目录执行：

```bash
python3 scripts/init-local-env.py
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8060/health
```

初始化脚本只创建权限为 `0600` 的 `.env`，不会启动服务。三个随机值分别用于 PostgreSQL、Controller 管理 API 和 Worker 首次注册。Docker Compose 将数据库映射到本机 `127.0.0.1:5433`，Controller 映射到 `127.0.0.1:8060`。不要直接暴露这两个端口到公网。停止服务用 `docker compose down`；这会保留数据库和 Worker 身份卷。

`config/sources.yaml` 里的示例默认关闭。编辑来源时，设置稳定的 `id`、`fetcher`、`schedule`、`config`、`routing` 和 `sink: postgres`。先确认目标站点的使用条款、访问频率和抓取范围，再启用。Controller 启动时读取来源配置；修改后运行：

```bash
docker compose exec -T control allfeeds-control config-validate --sources /app/config/sources.yaml
docker compose restart control
```

查看进度使用 `docker compose logs -f control worker`。采集的当前记录在 PostgreSQL 的 `allfeeds.resources`，版本在 `allfeeds.resource_versions`；这些是原始证据，不是经过人工审核的活动目录。只做本地测试时，保持示例 Source 关闭。

## 离线开发与验证

Python 3.11+ 环境中安装本地包：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e packages/contracts -e packages/sdk -e services/controller \
  -e services/worker -e plugins/builtin -e examples/custom-fetcher -e '.[dev]'
ruff check .
pytest -q
allfeeds-plugin validate --sources config/sources.yaml
allfeeds-control config-validate --sources config/sources.yaml
python3 scripts/check-public-release.py --history
```

PostgreSQL 集成测试需要单独设置 `ALLFEEDS_TEST_DATABASE_URL`，并且只在测试数据库运行。未设置时这些测试跳过。不要使用线上数据库进行测试。新增数据库结构应添加迁移，并在隔离测试 schema 中验证。
