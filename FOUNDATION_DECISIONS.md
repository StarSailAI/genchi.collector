# 用户系统决策

采用 auth / session / core-web 0.1.0、email 0.2.0 的规范；这些参考模块目前为 review / spec-only，本项目自行实现和验证，不宣称使用了现成生产认证服务。

用户已明确要求邮箱数字验证码注册登录、无密码与密码重置，以及个人关注 Dashboard。沿用本项目身份数据库、SMTP 配置和同源浏览器 Cookie，不接入中心化身份提供方。

- 用户与会话继续保存在私有 schema。保留已有 ID、关注和参与进度；邮箱验证成功才新建账号。邮箱 trim、NFKC 与小写归一化，不移除加号别名或邮箱中的点。
- 验证码六位、15 分钟、最多五次错误；用途与挑战 ID 绑定的 HMAC 摘要，原始验证码只存在于请求内存与目标邮件中。新挑战、成功验证、耗尽次数、超时、邮件失败均不能再次使用。
- 发送按邮箱每分钟一次、每小时六次，来源每分钟五次、每小时二十次，另有全局每小时一百次、每天五百次上限。验证按邮箱、来源和邮箱来源组合限流，限流与错误次数使用数据库事务保留；并发、多实例共用状态。
- 后端不信任任意转发 IP；网站只转发服务器签名的来源信息。启用可信边缘头前必须保证入口代理覆盖该头且网站源端口不直接暴露。默认来源合并限流，避免伪造头绕过保护。
- 认证邮件同步发送；SMTP 未确认接受时返回服务不可用并作废挑战，不自动重发。普通提醒保留 outbox。
- Session 保持 30 天、随机 Token、数据库仅存摘要、HttpOnly / SameSite=Lax / Path=/ / host-only；HTTPS 使用 Secure。重新登录轮换当前 Session，支持退出当前与全部设备，禁用账号立即拒绝现有会话。
- Dashboard 管理单个活动、系列、关键词及活动类型标签。关键词按标题、规范显示名、简介与系列名进行字面匹配；标签使用现有八种活动分类。个人活动、日程和提醒共用关注匹配规则。
- 账户资料只开放昵称、时区及提醒开关，邮箱与权限字段不可由用户请求修改。

## 与参考规范的差异

数据库沿用 psycopg 和 Alembic，而非引入 SQLAlchemy ORM。注册流程沿用现有信息用途说明；本次不创建未经确认的法律文本或伪造条款同意记录。生产发布前需单独核对已有隐私与服务条款。没有新增密码、密码重置、收费或第三方登录。

安全设计参考 [OWASP Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) 与 [Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。这些规范不等同于本项目已经通过安全审核。

## Agent API 与 API Key（2026-09-10）

Agent 是用户读取活动、管理关注与安排日程的一种能力，网站目标和主 slogan 仍为「下一次心动，现场见。」。首版提供同一套服务端权限模型下的 REST API 和远程 Streamable HTTP MCP，不按 Agent 品牌复制业务接口。

FoundationServices 的 API Key 模块当前为 `planned / draft / spec-only`，没有支持栈或可采用 Recipe。因此本项目只采用其安全和验收基线，自行实现并标记为 `project-owned`：完整 Secret 只显示一次；数据库保存带服务端密钥的摘要和非敏感前缀；Key ID 与 Secret 分离；Scope、账号状态、Key 状态均由服务端检查；账号级和 Key 级同时限流；撤销立即生效；调用记录不保存 Authorization、请求正文或查询内容。

API Key 只能由现有浏览器 Session 创建和撤销，不能访问账号邮箱、Session、后台或创建其他 Key。Agent 写入仅限用户自己的关注；活动和来源内容视为不可信数据，以结构化字段和官方证据返回，不作为工具指令。增量更新使用签名游标读取 `catalog_changes`，不依赖时间戳推断是否遗漏。

## 验证码邮件模板（2026-09-09）

沿用 Email 0.2.0 的 HTML / 纯文本、上下文转义、公开 Origin 和测试隔离要求。该模块仍为 spec-only，本项目实现独立的纯渲染函数、可随 Python 包分发的 HTML 模板，以及 SMTP multipart/alternative 支持。模板使用衬线品牌名、珊瑚红和网站主 slogan，不依赖图片、脚本或外部字体。语言固定为当前登录流程的简体中文，不按收件人域名推断语言。只调整验证码邮件展示，保留现有验证码、会话、限流、SMTP 和业务通知行为。真实收信链路已由用户确认可用；新模板先通过 Mailpit 验收，不额外向真实邮箱发送测试邮件。
