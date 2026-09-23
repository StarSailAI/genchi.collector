# 架构与边界

```text
Source YAML → Controller（注册、调度、租约、重试）
                      ↓
               Worker（插件执行）
                      ↓
       PostgreSQL（原始记录、版本、任务历史）
```

Controller 不执行 Fetcher；Worker 不创建日程。PostgreSQL 是任务与租约的唯一权威。任务采用至少一次投递，Sink 必须幂等。Source 在注册为任务时成为不可变快照。Worker 使用子进程执行插件并施加超时；管理员 API 和 Worker 注册使用不同的凭据。

内置 HTTP 客户端限制协议、重定向、响应大小和访问私有网络；抓取到的内容始终是不可信输入。扩展插件时保留稳定 `external_id`、限速和明确的错误语义。凭据只通过环境变量名引用，不写入 Source 快照或日志。

本仓库有意只保留采集层。网页提取、活动/场次/票务轮次归一化、模型审核、账号、邮件和网站 API 属于其他系统。不能把一条原始 Resource 直接当作准确的活动发布。
