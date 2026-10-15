# 可观测性

运行记录保存在本机，分为后端日志、AG-UI 事件流和会话 Trace。

## 日志

`dataagent serve` 把根日志设为 INFO，只写入 `<home>/runtime/logs/backend.log`。轮转大小 5_000_000 字节，保留 3 个备份。格式化时用 `safe_error` 去掉已知密钥。

启动行记录已启用插件和来源。流失败记录：

```text
RUN_ERROR run=<run_id> thread=<thread_id> stage=agent_stream code=<code> message=<message>
```

`code` 是异常类名。空消息仍保留类型。不写原始 traceback，也不把请求体打进这条日志。

TUI 自己的日志路径由 `DATAAGENT_V2_LOG_PATH` 决定，与后端文件分开。

## Trace

`build_agent` 绑定 `LocalLangChainTracer`。链路是 LangChain tracer、LangSmith SDK 的 OTEL 模式、只含本地文件导出器的 `TracerProvider`。不需要 LangSmith 账号。远程 Trace 环境变量不改变这个导出器的目的地。

根运行完成、失败或被正常取消时，写入：

```text
<home>/runtime/users/<user>/sessions/<thread>/traces/<run_id>.json
```

文件是标准 OTLP JSON：`resourceSpans`、`scopeSpans`、`spans`，十六进制 ID，纳秒时间戳。模式 `0600`。文件名和 trace id 使用根运行 UUID（OTLP 去掉连字符）。

Span 覆盖 Agent、子代理、模型、工具和中间件耗时。Agent、模型和工具的输入输出属性可能含系统提示、历史和完整工具结果。中间件里的活动对象不会原样导出。token 事件不保留。

关联字段：

| 属性 | 含义 |
| --- | --- |
| `langsmith.metadata.dataagent_run_id` | 原生运行 ID |
| `langsmith.metadata.dataagent_parent_run_id` | 父运行 |
| `langsmith.metadata.dataagent_root_run_id` | HTTP 请求的 `runId` |
| `langsmith.metadata.thread_id` | 会话 |

导出发生在根运行结束回调：排空队列、刷 provider、只提交这一条 trace。并发调用共用导出器，文件分开。进程被强杀时可能没有文件。保存失败记 `TRACE_SAVE_ERROR`，不改变 Agent 结果。

`tracing_context(enabled=False)` 可以关掉这次追踪。调用方另附的远程 tracer 由调用方负责。

已知密钥和常见 API key、Bearer 模式会脱敏。业务数据仍在文件里。没有自动清理策略。

## 流上的标识

SSE 的 `RUN_FINISHED.metadata.durationMs` 只表示服务端这一轮的耗时。它不写入 Checkpoint。TUI 的完成行同样只存在于界面。
