# 单机部署

目录结构为 `genchi/` 下并列放置 `genchi.collector`、`genchi.news`。Docker Engine 和 Compose plugin 是主机唯一的应用运行依赖；部署辅助命令支持 Ubuntu 22.04 自带的 Python 3.10。首次构建需要访问 Docker Hub、PyPI 和 npm registry。

## 共用配置

在两仓库的上一级执行：

```bash
python3 genchi.collector/deploy/manage.py init --site-url http://YOUR_SERVER_IP
nano .env
python3 genchi.collector/deploy/manage.py check
```

`init` 生成独立数据库密码、只读密码、内部服务令牌和产品密钥，文件权限为 0600；重复运行拒绝覆盖已有配置。需要填写的外部参数在文件上半部分：

| 配置 | 用途 |
| --- | --- |
| `PUBLIC_SITE_URL` | 初次用服务器 HTTP IP；正式域名解析到服务器后填写 HTTPS 根地址 |
| `ADMIN_EMAIL` | 用于登录审核工作台的邮箱 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | Chat Completions 兼容接口；三项一起填写 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_SECURITY` / `SMTP_USER` / `SMTP_PASSWORD` / `MAIL_FROM` | 正式登录邮件与提醒；需要服务商授权的发信域名和发件地址 |
| `ENABLE_X` / `X_COOKIES_FILE` | 可选官推采集；指定服务器上的 Playwright Cookie 数组 JSON |
| `CLOAKBROWSER_LICENSE_KEY` | 浏览器服务商要求许可证时填写 |

模型未配置时，normalizer 运行 `idle` 健康模式，不领取或终结处理任务；原文采集与已迁移活动读取继续工作。补齐模型配置后执行 `apply` 才切换到处理模式。未启用 X 时生成的来源配置仅关闭现有四个官推来源，不扩大其他来源范围。既有采集历史保留。

SMTP 初始为私网 Mailpit，邮件不投递到真实邮箱。启用正式 SMTP 时需同时修改主机、端口和 TLS 模式，并配置合法的 `MAIL_FROM`。通常为 587 / starttls 或 465 / ssl；邮件域名的 SPF、DKIM、DMARC 按服务商要求配置。

`.deploy/effective.env` 与 `.deploy/sources.yaml` 是自动生成的运行文件，不要手工编辑。只修改上一级 `.env`。文件中的密码不要通过 shell `source` 加载；辅助命令按数据解析，保留单引号包裹值中的 `$` 和 `#`。

## 构建与启动

```bash
python3 genchi.collector/deploy/manage.py compose backend pull postgres cloakbrowser mailpit gateway
python3 genchi.collector/deploy/manage.py compose backend build control worker normalizer
python3 genchi.collector/deploy/manage.py compose web build web
python3 genchi.collector/deploy/manage.py apply
python3 genchi.collector/deploy/manage.py status
```

首次在空闲机器上串行构建；后续尽量在 CI 构建 amd64 镜像再传输。`apply` 只启动已有镜像，不在运行采集时隐式构建。运行资源限制为 2 核 4GB 起步设计：浏览器并发 1、worker 并发 2、数据库连接池 8。建议主机配置 2GB swap 和容器日志轮转。

前端、产品 API、collector 共享上一级 env。网站由 Caddy 暴露 80 / 443；数据库、控制 API、Dashboard、Mailpit 和网站直连端口均绑定回环地址，浏览器端口不映射到宿主机。Dashboard 按需使用 `operations` profile。云安全组需允许 SSH 与 TCP 80 / 443。

域名 A 记录指向服务器后，将 `PUBLIC_SITE_URL` 改为 `https://你的域名`，再执行 `apply`。Caddy 自动申请证书；前端请求来源校验与 ICS URL 使用运行时配置，无需仅为更换域名重建镜像。

查看 Mailpit 可用 SSH 隧道把服务器的 `127.0.0.1:18025` 转发到本机闲置端口；不要把测试收信箱暴露到公网。

## 数据迁移与备份

迁移前停止本机采集写入，使用 PostgreSQL 16 的 `pg_dump -Fc --no-owner` 导出一致性快照并记录各表计数与摘要。仅启动远程 postgres，在空数据库中执行 `pg_restore --exit-on-error --no-owner`，核对原始资源、目录、名称历史、关注与账户等数据后再启动应用。不要把 `pg_dumpall` 中的本地角色密码搬到服务器；远程内部密码由初始化生成，`genchi_reader` 由数据库初始化脚本创建。

```bash
python3 genchi.collector/deploy/manage.py backup
```

备份保存到上一级 `backups/`，权限为 0600；归档目录验证通过后才替换临时文件，默认保留 7 天。本机备份不能覆盖整台服务器丢失的情况，应另配置异地备份。迁移原始备份应单独保留，不放入自动过期的 `genchi-*.dump` 命名范围。

Gitee 无法直连时，可通过 SSH 传递 Git bundle 并核对提交哈希，或上传构建好的镜像。不要为拉代码关闭 TLS 校验，也不要把个人 Git 凭据写入镜像。
