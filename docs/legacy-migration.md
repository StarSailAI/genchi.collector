# 旧 genchi.news 数据迁移

旧版 `genchi.news` 曾拥有自己的 `public` schema 和写入脚本。拆分后，数据库迁移归 `genchi.collector` 所有，网站只读。导入器会按 slug 合并四个核心 IP，并幂等复制 Franchise、Group、Artist、Venue、Event、Ticket、Release 和 News 关系。

## 1. 备份旧库

先对旧数据库创建可恢复的 custom-format 备份：

```bash
pg_dump "$LEGACY_DATABASE_URL" --format=custom --file=genchi-news-before-split.dump
```

## 2. 试跑

先构建并启动 collector。若旧 PostgreSQL 也是容器，临时把它接入 collector 私网是最稳妥的方式：

```bash
docker network connect genchi-collector_collector <legacy-postgres-container>
docker-compose exec normalizer genchi-normalizer import-legacy \
  --source-url 'postgresql://genchi:password@<legacy-postgres-container>:5432/genchi' \
  --source-schema public \
  --dry-run
```

试跑在一个事务中执行并回滚。也可以使用 normalizer 可访问的普通 PostgreSQL URL；仅绑定宿主机 `127.0.0.1` 的端口通常不能通过 `host.docker.internal` 访问。

## 3. 正式导入与验证

移除 `--dry-run` 后执行一次；命令可安全重跑，冲突行会复用目标记录。

```bash
docker-compose exec normalizer genchi-normalizer import-legacy \
  --source-url 'postgresql://genchi:password@<legacy-postgres-container>:5432/genchi' \
  --source-schema public

docker network disconnect genchi-collector_collector <legacy-postgres-container>

docker-compose exec postgres psql -U genchi -d genchi \
  -c 'SELECT count(*) FROM genchi."Event"' \
  -c 'SELECT count(*) FROM genchi."TicketWindow"'
```

确认网站、日历和事件详情正常后再决定旧容器的下线时间；导入器不会删除或修改旧库。
