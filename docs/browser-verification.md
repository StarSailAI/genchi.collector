# 浏览器验证中断与恢复

Genchi 将 SkillHub 的 `browser-verification-interrupt`、`browser-slider-verification` 方法适配到私有 Camoufox 服务：代码正常采集，遇到可见验证时保留原页面，由外部 Agent 观察和操作，再由程序核验真实页面并继续。实现见 `services/browser/verification.py`，操作入口是 `deploy/browser-verification.py`。

## 已实现的范围

目前只登记 `https://natalie.mu/comic`、`https://natalie.mu/music` 及其子路径，验证操作限定在可见的 `#captcha-container` 内。不是通用远程浏览器，也不接受调用者传入脚本、选择器、Cookie 或任意导航目标。验证图片和字体正常加载；公网地址、重定向、TLS 和出口代理检查继续生效。X 的上下文独立。

普通 `/fetch` 遇到验证会返回 `409 VERIFICATION_REQUIRED` 和中断 ID。`BrowserClient` 在原调用中轮询该 ID 的结果，最多等待 600 秒；不会把挑战页交给正文提取，也不会因轮询网络超时重新提交导航。同 URL 的重复等待者共用原中断，不同 URL 在此期间收到明确的忙状态。

后续已加入独立的常驻 `verification-agent`，启用后自动接手登记的九宫格图片验证；手动桥接仍保留。配置、模型调用账本与采集接入说明见 [Natalie 常驻验证与日采集](natalie-collection.md)。下文的七次点击记录是早期外部 Agent 验收，不能与后续无人值守验收混淆。

## 会话与操作约束

- 一个 Natalie 上下文保留最长 30 分钟，动漫与音乐栏目可以复用；不导出或重放验证令牌。
- 同时只有一位操作者，领取后的控制权有效 120 秒，不续租；等待接手最长 600 秒。超时、取消或浏览器退出会结束等待并释放浏览器槽位。
- 先 `observe`，看实际截图，然后发送一次 `pointer`。坐标为 1280×900 视口的 CSS 像素；每次操作后必须重新观察。
- `observationId` 单次使用、最长有效 60 秒，绑定网址、可见操作范围和实际返回截图的 SHA-256。点击前再次截图比较；题目即使在原位置刷新，也拒绝旧图操作。动态动画可能导致保守拒绝，应重新观察，不能绕过检查。
- `requestId` 提供操作幂等：回复丢失时可用完全相同的命令恢复结果，不能重复点击。拒绝或驱动中断的有效操作请求也消耗预算。
- 同一上下文最多三个导航中断。图片点选最多 12 次点击；一旦使用拖动，总操作上限降至 6 次，拖动最多 3 次。预算跨栏目和后续中断累计，不通过新建 Run 续次数。
- SkillHub 原方法统一限制六次操作。本项目对已登记的 Natalie 图片点选作了明确适配：实测一道题有五个目标，加“开始”和“确认”需要七次点击，因此采用固定 12 次上限；滑块仍保留原来的六次操作、三次拖动上限。
- `resume` 检查验证消失、目标栏目路径有效、实际文档响应为 2xx、正文与新闻链接存在，才返回当前原页面 HTML。鼠标动作 `performed: true` 不代表通过验证。已恢复结果按中断 ID 留存 30 分钟，避免另一栏目导航覆盖仍在等待的调用者。

接口仅限内部网络，并需 `BROWSER_API_TOKEN`。领取后的观察、操作、取消和恢复还需短期 `X-Verification-Control`。没有添加公网端口或 nginx 路由。

## 在线操作

以下命令只在部署服务器执行。先把仓库中的脚本复制到运行中的 worker；用实际容器名替换占位符。脚本从容器环境读取已有内部认证，无需新增外部 API key。

```bash
sudo docker cp deploy/browser-verification.py <worker-container>:/tmp/browser-verification.py
sudo docker exec <worker-container> python /tmp/browser-verification.py list
sudo docker exec <worker-container> python /tmp/browser-verification.py claim --run=<id>
sudo docker exec <worker-container> python /tmp/browser-verification.py observe --run=<id>
```

`observe` 输出截图路径、文字、视口、操作范围和 `observationId`。先读取截图，再执行单次点击或拖动；每次都使用最新观察，不硬编码某一题的坐标。

```bash
sudo docker exec <worker-container> python /tmp/browser-verification.py pointer --run=<id> \
  --input='{"observationId":"<latest-id>","kind":"click","x":<x>,"y":<y>}'
sudo docker exec <worker-container> python /tmp/browser-verification.py observe --run=<id>
sudo docker exec <worker-container> python /tmp/browser-verification.py resume --run=<id>
```

拖动额外提供 `toX`、`toY` 和 100–3000 范围内的 `durationMs`。失败或不再操作时调用 `cancel --run=<id>`。无法获得控制权时不能继续使用旧凭证。ID 可能以连字符开头，命令使用 `--run=<id>` 的等号形式。

独立访问验收可用 `start --section comic` 或 `start --section music`；已有原任务等待时直接 `list` 接手，不重复发起查询。生产来源以后接入时，任务硬超时需要容纳页面加载和最多 600 秒的验证等待。

控制凭证、截图和恢复 HTML 保存于容器内 `/tmp/genchi-browser-verification/`，目录 0700、文件 0600；令牌不输出到终端。容器重建后这些临时文件消失。不要提交截图、凭证或完整页面到 Git。无需操作用户桌面鼠标。

## 2026-09-09 服务器实测

Camoufox `official/152.0.4-beta.30`，Python 包 `camoufox==0.5.6` / `playwright==1.62.0`。真实访问只在部署服务器运行，本机只运行模拟和隔离数据库测试。

最终一次流程：

1. 动漫栏目返回 `405 Human Verification`，出现九宫格图片点选，**不是滑块**。
2. 外部 Agent 按可见的“选择所有帽子”指示，逐次观察并点击：开始 1 次、图片 5 次、确认 1 次，共 7 次；只提交了一次答案。
3. 验证消失，程序 `resume` 返回 HTTP 200、448,223 字节的真实动漫栏目 HTML；原 `BrowserClient` 调用返回 430,669 个字符，未重放原查询。
4. 同上下文访问音乐栏目返回 HTTP 200、219,894 字节，不需要第二次验证。动漫和音乐列表分别解析到 28、20 个不同新闻详情链接。
5. 同上下文读取 [MAPPA EXPO 新闻](https://natalie.mu/comic/news/688780)（403,020 字节）和 [北村蕗演出相关新闻](https://natalie.mu/music/news/688688)（345,927 字节），均取得对应标题和真实详情页，没有验证面板。

这些结果证明此次验证及原请求恢复成功，也排除了“这台机器完全无法访问 Natalie”的说法；不能证明长期成功率、全站覆盖或日采集可靠性。此次没有运行项目的付费视觉模型调用、正文提取模型或写入生产原文/候选库；操作由当前外部 Agent 完成。

开发验收中曾因默认视口为空而没有操作能力；还遇到题目刷新和两分钟控制权到期。前四个开发会话均未提交答案，分别取消或超时，原等待者收到对应失败。已修复明确视口和同位置换题的旧截图检查。曾试验基于九个 `<img>` 的批量点选，但真实页面不满足该能力条件，最终删除该实验入口，保留逐步观察。没有把这些失败轮次计作网站拒绝答案。

测试覆盖独占控制、原请求保留、取消/超时释放、单次观察、截图变化、操作幂等、动作预算、同会话跨栏目、旧结果绑定及轮询丢包。服务器无网络的合成 Camoufox 页面也完成过点击和拖动验收；合成验收与上述真实官网成功分开记录。

发布验证：182 项测试通过，包含隔离 PostgreSQL 集成测试；ruff 与 20 个正式来源配置校验通过。Genchi 插件已重新构建 wheel 和 sdist，只更新 browser 和 worker 镜像，没有数据库迁移或产品服务变更。
