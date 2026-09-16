# 邮件模板

采用 FoundationServices Email 0.2.0 的模板规范：纯文本必备、HTML 动态值转义、表格和内联样式、链接限定公开 Origin、认证投递与测试环境隔离。参考模块为 spec-only，本项目自行实现和验收。

`services/product/src/genchi_product/emails.py` 是不访问网络或数据库的渲染层，返回 `subject`、`text` 和 `html`。HTML 源文件位于 `templates/login_code.html` 和 `templates/notification.html`，通过 Python package data 随安装包分发。登录流程传入实际生成的六位验证码、实际有效分钟数和 `PUBLIC_SITE_URL`；模板不保存验证码，不在主题、日志或对象 repr 中暴露验证码。

邮件沿用网站的 Georgia 衬线品牌名、明朝体「現地情報」、珊瑚红与浅色卡片，页脚为「下一次心动，现场见。」。验证码保持连续六位文本，便于复制；没有远程图片、字体、脚本、追踪像素或登录令牌链接。模板语言通过 `locale` 显式传入，支持简体中文、繁体中文、英文、日文；未知语言回退到简体中文，不根据邮箱域名猜测。链接只使用部署中确认的公开 Origin。

SMTP 以 `multipart/alternative` 投递，先放 UTF-8 纯文本，再放 HTML。不支持 HTML 的客户端可直接阅读纯文本。认证邮件不添加营销退订头；活动汇总、信息更新、开售、截止、结果和临近活动提醒均使用统一的品牌模板，并保留管理关注和退订入口。

同一活动在五分钟内产生的更新仍合并为一封邮件。渲染时再按用户可读的变更名称聚合重复记录，并以「变更内容 / 记录数」两列表格展示，例如 14 条「更新：活动开始」只占一行，记录数为 14。正文最多展示八类变更，其余内容引导用户前往活动详情查看。批量变更不会展示仅属于最后一条底层记录的开始或截止时间，避免将单条值误解为整批更新；只有单条变更才展示对应的新时间。

离线预览（示例验证码不关联任何登录挑战）：

```bash
python - <<'PY'
from pathlib import Path
from genchi_product.emails import render_login_email
message = render_login_email('004281', expires_minutes=15, site_url='https://example.com')
Path('.runtime').mkdir(exist_ok=True)
Path('.runtime/login-email-preview.html').write_text(message.html, encoding='utf-8')
Path('.runtime/login-email-preview.txt').write_text(message.text, encoding='utf-8')
PY
pytest -q tests/test_email_templates.py
```

2026-09-09 验收：模板测试 21 项、后端完整测试 189 项通过；HTML 已确认包含在 wheel 内。Chromium 预览检查 760 / 390 / 320 px，无横向溢出；Mailpit 实际捕获的登录邮件同时包含 HTML 和文本，验证码可完成登录。未自动向真实邮箱发送模板测试邮件，也未宣称所有邮箱客户端均已实测。

同日多语言验收：新增简体、繁体、英文、日文模板及账户语言偏好，后端完整测试增至 201 项并通过。四种语言均通过本地 Mailpit 的真实发送、HTML / 文本内容检查和浏览器验证码登录；生产镜像通过断网渲染检查并已部署。测试账户与邮件已清理，本轮未向外部邮箱发送测试邮件。

2026-09-11 活动通知验收：包含 28 条底层记录的更新邮件被聚合成两行表格，HTML 与纯文本均明确显示变更内容和记录数，且不再附带误导性的单条更新时间。Chromium 预览检查 760 / 390 / 320 px，无横向溢出。本轮仅使用测试地址和本地 Mailpit 验证，未向外部邮箱发送测试邮件。
