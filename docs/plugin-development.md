# Fetcher 插件

从 [examples/custom-fetcher](../examples/custom-fetcher) 复制一个 Python 包，并通过 `allfeeds.fetchers` entry point 注册。Fetcher 应声明稳定的 `FetcherManifest.name`、严格的 Pydantic 配置模型和 `fetch()`；支持历史补采时再实现 `backfill()`。

通过 `FetchContext.emit()` 输出 `ResourceRecord`，使用来自上游的稳定 `external_id`。相同 Source 与 `external_id` 会幂等更新当前记录；内容改变时保留版本。`checkpoint()` 只在成功写入后推进。认证、限流、临时网络故障和永久错误应使用 SDK 的类型化异常，便于 Controller 正确重试。

对于 HTTP 来源，复用 `plugins/builtin/src/allfeeds_builtin/http.py` 的安全客户端。不要绕过私网地址检查、重定向复核、响应上限或站点访问限制。新增来源先写离线解析测试，再小范围启用。

```bash
allfeeds-plugin list
allfeeds-plugin validate --sources config/sources.yaml
```
