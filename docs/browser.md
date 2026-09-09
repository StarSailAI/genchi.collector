# 网站访问：F / Camoufox

Genchi 使用 SkillHub「网站访问 Pro」的 F 路线：Camoufox / Firefox。固定版本为 Python `camoufox==0.5.6`、`playwright==1.62.0`、浏览器 `official/152.0.4-beta.30`。浏览器在构建镜像时下载，运行时不自动升级。首次构建还需要访问 GitHub Releases。

SkillHub 的其他路线是 S（SeleniumBase / Chrome）和 C（CloakBrowser / Chromium）。本项目只部署 F，不在遇到挑战页时自动轮换路线。普通来源继续优先使用 HTTP；需要页面渲染的票务网站和官推才调用浏览器。

## 服务边界

- `browser` 是非 root 容器，默认后台运行、并发 1。生产内存上限 1400MB，共享内存 512MB。
- 只提供受令牌保护的 `/fetch` 和 `/extract/x-profile`，不开放任意脚本、CDP 或宿主机端口。`/health` 可检查实际引擎、版本和连接状态。
- 每次公共网页抓取创建并关闭独立 context；X 使用单独的 Cookie context，公共网页不会复用官推登录状态。
- 正文采集默认不下载图片、视频和字体文件，页面中的媒体链接仍可提取。Firefox 子进程断开后，下一个请求会在互斥锁下重新启动 F 引擎，避免接口存活但浏览器永久失效。
- 页面请求、跳转和子资源都检查目标地址。容器内的回环 HTTP/CONNECT 代理解析公网 IPv4 后直接连接该 IP，避免检查后由浏览器再次解析 DNS。
- 只允许 HTTP(S) 与 80/443 端口；拒绝内网、回环、链路本地和保留地址。仅遇到本地 VPN 的 `198.18.0.0/15` 假 DNS 时，通过校验证书的 Cloudflare DoH 获取真实 A 记录并再次验证。
- 保留 HTTPS 证书验证，关闭下载、WebSocket、Service Worker、WebRTC 和 HTTP/3；限制请求、连接、页面时间及响应体大小。

X 的登录失效、挑战页、空页面或结构变化会返回明确错误。本次迁移没有接入 SkillHub 的验证码桥或远程人工接管；`ENABLE_X` 和 Cookie 配置继续由运维明确开启。

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

2026-09-09 的迁移验收在本机 Linux ARM64 容器和服务器 Linux x86_64 容器通过：Love Live! 与偶像大师新闻列表均可读取；Lawson「ラブライブ」搜索均解析出 2 个结果页、10 个场次、15 个票务窗口。全量测试 102 项通过。X 未配置登录 Cookie，尚未进行官推登录采集验收。

参考：[Camoufox 使用说明](https://camoufox.com/python/usage/)、[安装说明](https://camoufox.com/python/installation/)。
