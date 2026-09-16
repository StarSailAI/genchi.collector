# 邮箱账户与个人中心

当前契约最低为 Schema 1.6。沿用 FoundationServices 的账户、验证码和 Cookie Session 分层，具体选择见[项目配置](../../foundation.project.yaml) 与 [架构决策](../architecture/foundation-decisions.md)。

## 使用流程

`/zh-Hans/login` 输入邮箱，获取六位数字验证码；验证成功后进入 `/zh-Hans/dashboard`。未注册邮箱在验证成功时创建账户，老用户沿用原账户和关注记录。不保存密码，也没有重置密码页面。

个人中心管理活动、系列、关键词、活动类型标签四类关注。关键词经 NFKC、去首尾空格及小写规范化，按字面子串匹配活动原文 / 中文标题、简介和系列名称；`%`、`_` 不作为通配符。标签使用现有八种活动类型，不引入另一套标签库。关注结果同时用于个人活动列表、日程和提醒；同一活动多个关注命中时不会重复发信，偏好优先级为活动、系列、关键词、标签。

个人资料允许修改昵称、时区和是否接收活动提醒。邮箱与账户权限不能通过资料接口修改。退订后新增 / 修改关注不会自动恢复邮件，需在个人中心明确开启。活动时间展示继续使用 JST；账户时区用于每日汇总时间。

## 接口与存储

- `POST /auth/login`：`email`、可选 `timezone`；SMTP 接受后返回 `expires_in=900`、`retry_after=60`。发送失败返回 503，挑战失效。SMTP 超时可能已投递的旧验证码也不能登录，需重新获取。
- `/health` 与验证码发送成功响应必须带有 `auth_method=email_code`。网站在转发登录 / 验证前检查此能力；旧版 `{sent:true}` 不能触发验证码输入步骤。部署入口会在启动容器前检查镜像的真实验证接口，阻止数据库版本正确、应用仍使用登录链接的情况。
- `POST /auth/verify`：`email`、`code`；成功返回 `ok`、`is_new_user` 并写入 Cookie。
- `POST /auth/logout`、`POST /auth/logout-all`：撤销当前 / 所有会话。
- `GET /me`、`PUT /me`：资料读写；写入字段严格限定为 `display_name`、`timezone`、`unsubscribed`。
- `GET /tags`：当前可关注的类型标签。
- `PUT /me/follows`、`DELETE /me/follows/{id}`：管理四种关注；同一账户同类型同目标唯一，最多 500 项。所有操作从会话取得账户 ID，忽略客户端自称身份。

账户、挑战、Session、限流计数位于 `genchi_private`，不授予网站只读数据库角色访问权。验证码使用随机六位数字，数据库保存带服务端密钥的 HMAC 摘要；Session 使用 40 字节随机 token，仅存 SHA-256 摘要。Cookie 为 host-only、HttpOnly、SameSite=Lax、30 天，在 HTTPS 环境启用 Secure。认证及私人响应禁止缓存，写请求检查完整 Origin。

挑战行锁确保并发只能消费一次。新验证码覆盖旧验证码；五次错误会消费挑战。错误计数和限流独立提交，不会因 HTTP 错误回滚。过期会话在账户查询 / 发码时清理；旧挑战和限流在发码时清理。

## 默认限流

| 范围 | 发送上限 | 验证上限 |
| --- | --- | --- |
| 每邮箱 | 1 次 / 分钟，6 次 / 小时 | 15 次 / 15 分钟 |
| 每来源（IPv6 按 /64） | 5 次 / 分钟，20 次 / 小时 | 50 次 / 15 分钟 |
| 邮箱与来源组合 | — | 10 次 / 15 分钟 |
| 全站 | 100 次 / 小时，500 次 / 日 | — |

计数存于 PostgreSQL，重启 / 多进程不能绕过。受限响应返回 429 和 Retry-After。全站限额限制邮件成本；恶意消耗限额也可能暂时阻止正常发码，后续按实际流量调整或接入挑战验证，不把当前限额描述为绝对防刷保证。

## 配置与本地验收

两个项目的服务器环境设置相同、至少 32 字符的随机 `PRODUCT_PROXY_SECRET`，与 `PRODUCT_SECRET` 独立。不得放入 `NEXT_PUBLIC_*`。网页 BFF 使用 HMAC 签名来源信息；Product API 不接受普通 X-Forwarded-For 的身份声明，Uvicorn 不启用隐式代理头信任。

默认 `AUTH_TRUST_PROXY=false`，所有经过网页入口的请求共享来源配额，适合本地直接访问。线上只有在网站端口被限制、只接受可信 Nginx 入口，并由 Nginx 覆盖 X-Real-IP 为真实连接地址后，才设置网站的 `AUTH_TRUST_PROXY=true`。参考 `deploy/nginx.conf.example`；多层代理必须先配置可信代理链，不能直接相信浏览器提供的头。Product API 仍只绑定本机 / 私有网络。

`AUTH_REGISTRATION_OPEN=true` 默认开放注册；false 时已验证账户仍可登录。停用账户始终拒绝登录和会话使用。Product API 需要与 notifier 相同的 SMTP 配置（远程 SMTP 使用 TLS），验证码发送无需启动 notifier。

本地只使用 Mailpit（收件箱 `http://localhost:18025`）；测试命令使用内存 SMTP stub，数据库测试创建并删除唯一临时 schema。浏览器验收使用无头 Chromium。不要把本地 Mailpit 收信误认为真实邮箱投递成功。

验证码邮件使用品牌 HTML 模板，并提供 UTF-8 纯文本回退。模板结构、离线预览及验收见 [邮件模板](email-templates.md)。

升级前备份账户与活动 schema，运行 normalizer 的 Alembic 至 `0008_email_code_auth`，再更新 Product API 和网站。迁移保留账户、会话、关注、参与记录；旧登录链接与旧 LOGIN 邮件内容失效。迁移不可通过删除用户数据回滚；回退也应使用支持数字验证码的版本。

## 语言偏好（Schema 1.6）

四语言代码为 `zh-Hans`、`zh-Hant`、`en`、`ja`。登录和验证请求新增 `locale`（默认简体），验证成功后保存到账户；`PUT /me` 可单独设置活动邮件语言，省略时保持原值。界面切换不更改退订、关注或账户时区。

`0009_account_locale` 为账户与验证码挑战增加受约束的语言列，旧记录默认为简体。先备份再运行 `genchi-normalizer migrate`，发布时同步更新 Product 与 notifier。`tests/test_localization.py` 覆盖四语言邮件、注册与资料保存、并发请求隔离、官方名称保护和提醒链接。
