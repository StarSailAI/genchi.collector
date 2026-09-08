# 单机部署

目录结构为 `genchi/` 下并列放置 `genchi.collector`、`genchi.news`。主机安装 Docker Engine、Compose plugin、Nginx 与 Certbot；部署辅助命令支持 Ubuntu 22.04 自带的 Python 3.10。首次构建需要访问 Docker Hub、PyPI 和 npm registry。

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
| `RESEND_API_KEY` / `MAIL_FROM` | Resend API key 与已验证域名下的发件地址；登录邮件和提醒统一经 Resend 发送 |
| `ENABLE_X` / `X_COOKIES_FILE` | 可选官推采集；指定服务器上的 Playwright Cookie 数组 JSON |
| `CLOAKBROWSER_LICENSE_KEY` | 浏览器服务商要求许可证时填写 |

模型未配置时，normalizer 运行 `idle` 健康模式，不领取或终结处理任务；原文采集与已迁移活动读取继续工作。补齐模型配置后执行 `apply` 才切换到处理模式。未启用 X 时生成的来源配置仅关闭现有四个官推来源，不扩大其他来源范围。既有采集历史保留。

生产环境默认 `MAIL_PROVIDER=resend`。辅助命令把 API key 映射到 Resend 官方 SMTP 认证，固定使用 `smtp.resend.com:465`、用户名 `resend` 和校验证书的 TLS。API key 为空时 notifier 运行 `idle` 模式，保留队列而不投递到其他服务。配置 key 和合法的 `MAIL_FROM` 后执行 `apply` 启用。发送附带稳定的 `Resend-Idempotency-Key`；已有 SMTP 不确定状态仍需人工核对，不会自动重试。

Mailpit 仅保留为私网调试工具，不是生产投递的替代入口；既有本地环境仍支持 `MAIL_PROVIDER=smtp`。邮件域名的 SPF、DKIM、DMARC 按 Resend 指引配置。参见 [Resend SMTP 文档](https://resend.com/docs/send-with-smtp)。

`.deploy/effective.env` 与 `.deploy/sources.yaml` 是自动生成的运行文件，不要手工编辑。只修改上一级 `.env`。文件中的密码不要通过 shell `source` 加载；辅助命令按数据解析，保留单引号包裹值中的 `$` 和 `#`。

## 构建与启动

```bash
python3 genchi.collector/deploy/manage.py compose backend pull postgres cloakbrowser mailpit
python3 genchi.collector/deploy/manage.py compose backend build control worker normalizer
python3 genchi.collector/deploy/manage.py compose web build web
python3 genchi.collector/deploy/manage.py apply
python3 genchi.collector/deploy/manage.py status
```

首次在空闲机器上串行构建；后续尽量在 CI 构建 amd64 镜像再传输。`apply` 只启动已有镜像，不在运行采集时隐式构建。运行资源限制为 2 核 4GB 起步设计：浏览器并发 1、worker 并发 2、数据库连接池 8。建议主机配置 2GB swap 和容器日志轮转。

前端、产品 API、collector 共享上一级 env。网站由宿主机 Nginx 暴露 80 / 443；数据库、控制 API、Dashboard、Mailpit 和网站直连端口均绑定回环地址，浏览器端口不映射到宿主机。Dashboard 按需使用 `operations` profile。云安全组需允许 SSH 与 TCP 80 / 443。

域名 A 记录指向服务器后，用 `deploy/nginx.conf.example` 替换域名与网站回环端口占位符，安装到 `/etc/nginx/sites-available/genchi` 并链接到 `sites-enabled/`。使用 `certbot --nginx -d 你的域名 --redirect` 申请证书并启用 HTTPS 重定向；启用 `certbot.timer` 自动续期。改为 `PUBLIC_SITE_URL=https://你的域名` 后执行 `apply`，该命令会检查并重载已有 Nginx 配置。更换域名时同步调整 Nginx 与证书；前端来源校验和 ICS URL 无需重建镜像。

Nginx 示例日志省略查询串，避免记录登录链接令牌。证书文件保存在 `/etc/letsencrypt/`，不要复制到仓库或 Docker 镜像。

## Resend 收信转发

发信和收信共用 `.env` 中的 **`RESEND_API_KEY`**，必须有 **Full access** 权限。Resend 中启用域名的 Receiving，并按要求设置 MX。所有 `email.received` 都转发到 `ADMIN_EMAIL`，不按收件地址筛选；管理员邮箱应使用另一个邮箱服务，以便实际收取邮件。

Nginx 的精确路径 `/api/webhooks/resend` 代理至产品 API `/webhooks/resend`；安装模板时替换 `__PRODUCT_PORT__` 为产品回环端口。TCP 443 必须在云防火墙放行。先部署服务与 Nginx 路由，再执行：

```bash
python3 genchi.collector/deploy/inbound-setup.py
python3 genchi.collector/deploy/manage.py apply
python3 genchi.collector/deploy/manage.py compose backend exec -T notifier genchi-product inbound-backfill
python3 genchi.collector/deploy/manage.py compose backend exec -T notifier genchi-product inbound-status
```

setup 仅创建或复用本站 endpoint，保留其他 webhook；自动把签名密钥写入受保护的 `RESEND_WEBHOOK_SECRET`，不会输出密钥。不需要第二个 API key。重复执行 backfill 只补录未见过的收件，适合初次上线或 webhook 停用后补齐。

收件回调使用 Svix 验证原始请求和时间戳，确认入库后才返回成功。notifier 获取正文与所有附件分页，使用 `MAIL_FROM` 发送，原发件人与收件人显示在正文头部，Reply-To 保留原邮件回复地址，附件内容与内嵌 CID 一并转发。目标只取配置中的 `ADMIN_EMAIL`，不会抄送原收件人。

notifier 每分钟还会按持久游标检查 Resend 收件箱，补齐回调中断期间的邮件；只有完整扫描到上次游标后才推进进度，中途失败或重启不会跳过未处理收件。首次开启会补齐 Resend 保留的历史收件。公网回调不通时此补扫仍可转发邮件。

私有表 `genchi_private.inbound_mail` 保存去重记录；重试使用冻结正文与稳定的 Resend 幂等键，成功后清除本地正文和附件缓存。超时等临时错误自动重试，超过 23 小时的发送不确定状态转为 `UNCERTAIN`，避免超过 Resend 24 小时幂等窗口后重复发送。带有效本站转发签名的循环邮件标为 `SKIPPED`。`SENT` 表示 Resend 接受发送，不代表最终送达。

附件过大或 Resend 拒绝的文件类型不会静默丢弃附件后发送；任务保留为 `FAILED`，可通过 `inbound-status` 查看邮件 ID 与安全错误码，并在 Resend 收件箱中查看原件。核对 `UNCERTAIN` 的 Resend 发送记录后再人工处理，不能直接批量重试。参见 [Resend 收信转发](https://resend.com/docs/knowledge-base/forward-emails-with-resend-inbound) 和 [幂等窗口](https://resend.com/docs/dashboard/emails/idempotency-keys)。

查看 Mailpit 可用 SSH 隧道把服务器的 `127.0.0.1:18025` 转发到本机闲置端口；不要把测试收信箱暴露到公网。

## 数据迁移与备份

迁移前停止本机采集写入，使用 PostgreSQL 16 的 `pg_dump -Fc --no-owner` 导出一致性快照并记录各表计数与摘要。仅启动远程 postgres，在空数据库中执行 `pg_restore --exit-on-error --no-owner`，核对原始资源、目录、名称历史、关注与账户等数据后再启动应用。不要把 `pg_dumpall` 中的本地角色密码搬到服务器；远程内部密码由初始化生成，`genchi_reader` 由数据库初始化脚本创建。

```bash
python3 genchi.collector/deploy/manage.py backup
```

备份保存到上一级 `backups/`，权限为 0600；归档目录验证通过后才替换临时文件，默认保留 7 天。本机备份不能覆盖整台服务器丢失的情况，应另配置异地备份。迁移原始备份应单独保留，不放入自动过期的 `genchi-*.dump` 命名范围。

Gitee 无法直连时，可通过 SSH 传递 Git bundle 并核对提交哈希，或上传构建好的镜像。不要为拉代码关闭 TLS 校验，也不要把个人 Git 凭据写入镜像。
