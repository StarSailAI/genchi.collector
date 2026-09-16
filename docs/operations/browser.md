# 网站访问：F / Camoufox

Genchi 使用 SkillHub「网站访问 Pro」的 F 路线：Camoufox / Firefox。固定版本为 Python `camoufox==0.5.6`、`playwright==1.62.0`、浏览器 `official/152.0.4-beta.30`。浏览器在构建镜像时下载，运行时不自动升级。首次构建还需要访问 GitHub Releases。

SkillHub 的其他路线是 S（SeleniumBase / Chrome）和 C（CloakBrowser / Chromium）。本项目只部署 F，不在遇到挑战页时自动轮换路线。普通来源继续优先使用 HTTP；需要页面渲染的票务网站和官推才调用浏览器。

## 服务边界

- `browser` 是非 root 容器，默认后台运行、并发 1。生产内存上限 1400MB，共享内存 512MB。
- 提供受令牌保护的 `/fetch`、`/extract/x-profile` 和限定 Natalie 的 `/verification`，不开放任意脚本、CDP 或宿主机端口。`/health` 可检查实际引擎、版本和连接状态。
- 普通公共网页抓取创建并关闭独立 context；Natalie 保留最长 30 分钟的独立验证上下文。X 默认使用匿名 context，同一轮的列表与详情复用该匿名会话。仅显式选择 cookies 模式时使用独立账号 context，公共网页不会复用官推登录状态。
- 正文采集默认不下载图片、视频和字体文件，Natalie 验证上下文允许加载图片与字体。页面中的媒体链接仍可提取。Firefox 子进程断开后，下一个请求会在互斥锁下重新启动 F 引擎，避免接口存活但浏览器永久失效。
- 页面请求、跳转和子资源都检查目标地址。容器内的回环 HTTP/CONNECT 代理解析公网 IPv4 后直接连接该 IP，避免检查后由浏览器再次解析 DNS。
- 只允许 HTTP(S) 与 80/443 端口；拒绝内网、回环、链路本地和保留地址。仅遇到本地 VPN 的 `198.18.0.0/15` 假 DNS 时，通过校验证书的 Cloudflare DoH 获取真实 A 记录并再次验证。
- 保留 HTTPS 证书验证，关闭下载、WebSocket、Service Worker、WebRTC 和 HTTP/3；限制请求、连接、页面时间及响应体大小。

X 的登录提示、挑战页、空页面或结构变化会返回明确错误，不把失败当作零结果。`ENABLE_X=true` 启用既有四个官推；`X_AUTH_MODE=anonymous` 为默认值，不要求 Cookie。只有显式选择 `X_AUTH_MODE=cookies` 才要求 `X_COOKIES_FILE`。服务 API 的 Bearer 认证始终保留，与 X 账号登录是两回事。

## X 公开页面采集

适配器发现匿名页面中的普通 `article` 和真实 `/账号/status/数字ID` 链接，不依赖生成的 CSS 类名或旧 `data-testid`。读取有界列表后，逐条打开详情页；核对 canonical URL 与原推 ID，从正文取得完整文本，从页面元数据或 `time[datetime]` 取得绝对发布时间。相对的“2h”不会换算成伪精确时间。仍折叠的正文、时间缺失或详情错配均报错。

引用和回复的正文不会拼接到原推证据里。媒体、外部链接与引用推文链接单独保存；图片中没有转录的文字不被当作已提取事实。点赞、转发数量和置顶位置不进入内容版本哈希，避免每天重复触发模型。上游 `x:<post_id>` 身份保持不变。

每轮最多读取 50 个可见推文、滚动 8 次（接口最多允许 12 次），没有历史回溯。置顶的已知推文不会提前中断扫描。报告记录发现数、完成数、滚动次数、停止原因、与上轮非置顶推文的重合数；匿名列表只是公开窗口，不保证账号全量历史。与上轮完全没有重合时标记 `gapPossible` 和 partial，提示可能有未覆盖更新；达到条数或滚动上限时也标记 partial。详情失败时已完整取得的原文仍保存，但不推进检查点。导航遇网络中断、超时或 502/503/504 时最多尝试两次；登录拒绝和解析错误不会反复导航。报告另记录 `pageRetries`。

模型使用既有词表与证据校验流程，输出进入人工审核，不能直接发布活动。匿名访问将来出现登录限制时会显示明确错误，不静默切换账号或其他浏览器。

## 从旧浏览器迁移

在两个仓库的上一级执行：

```bash
python3 genchi.collector/deploy/manage.py migrate-browser-env
python3 genchi.collector/deploy/manage.py compose backend build browser worker control
python3 genchi.collector/deploy/manage.py compose backend up -d --no-build --wait browser
```

验证新浏览器能访问实际来源后，等待旧 worker 的在途任务完成，停止 worker，再执行 `manage.py apply`。确认新 worker 正常后，停止并删除旧 `genchi-collector-cloakbrowser-1` 容器。保留原浏览器卷作为备份；Chromium 的 profile 不能直接用于 Firefox。

迁移命令将 `CLOAKBROWSER_*` 改为 `BROWSER_*`，保留内部令牌和无关配置，移除旧镜像、许可证及 profile 参数；已有新配置优先。原 env 备份到 `.deploy/env-before-camoufox`，权限 0600。首次迁移前检查没有旧名称的自定义活跃任务快照；不要修改历史任务来更换浏览器。

`secrets/` 目录权限 0700，自动生成的 `x-cookies.json` 为 0644，允许非 root 容器读取单文件挂载；其他宿主机用户不能穿过目录读取它。整个 secrets 目录不会挂载到浏览器。共享 `.env` 和 `.deploy/effective.env` 始终为 0600。

## 验证

`tests/test_browser_service.py` 覆盖地址验证、代理连接、接口认证、会话隔离和页面清理。模拟测试不代表网站兼容性：部署还需检查 `/health`，并对已启用的官方新闻页和票务来源执行真实渲染与解析。挑战页或选择器失效不能作为成功采集验收。

今后的真实浏览器探测和采集验收只在部署服务器执行，本地使用模拟测试。浏览器客户端对临时页面失败最多尝试 3 次。上游短期限流先按 Retry-After 等待，至少 60 秒，再重试当前页面；长时间或持续限流交回调度器退避，认证错误不会反复重试。来源可显式配置已核对的页面删除提示，但普通 403 拒绝不会被当成删除成功跳过。

2026-09-09 的迁移验收在本机 Linux ARM64 容器和服务器 Linux x86_64 容器通过：Love Live! 与偶像大师新闻列表均可读取；Lawson「ラブライブ」搜索均解析出 2 个结果页、10 个场次、15 个票务窗口。全量测试 102 项通过。当时 X 尚未验收；后续匿名页面适配和四源验收另见 [X 公开采集验收](../archive/x-public-acceptance-2026-09-09.md)。

Natalie 可见验证码的会话保留、私有 `/verification` 接口及真实通过记录，见 [验证中断与恢复](browser-verification.md)。启用常驻 `verification-agent` 后可自动处理已登记的九宫格验证，配置与调用上限见 [Natalie 采集](../sources/natalie-collection.md)。

参考：[Camoufox 使用说明](https://camoufox.com/python/usage/)、[安装说明](https://camoufox.com/python/installation/)。
