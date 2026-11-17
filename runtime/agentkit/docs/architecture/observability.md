# 可观测性

运行记录保存在本机，分为后端日志、AG-UI 事件流和会话 Trace。

A2A、REST API、TUI 和 Web 共用这些记录及会话 checkpoint、产物目录。协议任务号与内部轮次号保留关联，各入口不建立独立的持久业务存储。

## 日志

`dataagent serve` 和 `dataagent serve-a2a` 把根日志设为 INFO，写入 `<home>/runtime/logs/backend.log` 和标准错误。轮转大小 5_000_000 字节，保留 3 个备份。已知密钥会脱敏；时间格式和时区沿用现有行为。

启动行记录已启用插件和来源。流失败记录：

```text
RUN_ERROR run=<run_id> thread=<thread_id> stage=agent_stream code=<code> message=<message>
```

`code` 是 `TIMEOUT`、`MODEL_ERROR`、`TOOL_ERROR` 等公开错误分类，异常类名另记在 `error`。空消息仍保留类型。不写原始 traceback，也不把请求体打进这条日志。

TUI 日志固定写入 `<home>/runtime/logs/tui.log`，与 `backend.log` 同目录。Home 来自进程环境 `DATAAGENT_HOME`，未设置时是 `~/.dataagent`。

## A2A 调用关联

`/a2a/jsonrpc` 的接收、完成、取消和失败事件在同一个文件中记录，正文为 JSON。关联字段包括 `request_id`、`rpc_id`、`message_id`、`context_id`、`task_id`、`root_run_id`、`instance_id` 和进程 `user_id`。匿名访问不识别实际访问者，`user_id` 不能作为调用方身份。

主要事件是 `a2a.request.received/finished/aborted/rejected/failed`、`a2a.execution.queued/started/completed/failed/cancelled`、`a2a.task.cancel_requested`、`a2a.checkpoint.saved/unchanged/failed`、`a2a.cleanup.failed`、`a2a.trace.saved/failed`。HTTP 完成记录区分 `http_status`、`rpc_error_code` 和 `task_state`，HTTP 200 不等于任务成功。

失败记录带 `error_code`、`stage`、脱敏说明及最多 12 个异常栈帧的位置，不记录局部变量、完整请求体、模型提示或工具结果。按字段脱敏和限长，长错误不会截掉整条 JSON 或后面的关联号。

先按响应头 `X-Request-ID` 或 Task ID 搜索日志，再用 `root_run_id` 找到 `a2a.trace.saved` 中的 `native_run_id` 和 `trace_file`。会话索引只保留最后的轮次和 checkpoint，历史调用关系依靠保留期内的日志与 trace。

## Trace

`build_graph` 绑定 `LocalLangChainTracer`。链路是 LangChain tracer、LangSmith SDK 的 OTEL 模式、只含本地文件导出器的 `TracerProvider`。不需要 LangSmith 账号。本地文件的目的地固定。实时推送只认构造参数，或进程环境里的 `DATAAGENT_OTLP_TRACES_ENDPOINT`（完整 traces URL）和 `DATAAGENT_OTLP_HEADERS`。`OTEL_EXPORTER_OTLP_*` 与 `LANGSMITH_TRACING` 不改变这两处。

根运行完成、失败或被正常取消时，写入：

```text
<home>/runtime/users/<user>/sessions/<thread>/traces/<run_id>.json
```

文件是标准 OTLP JSON：`resourceSpans`、`scopeSpans`、`spans`，十六进制 ID，纳秒时间戳。模式 `0600`。文件名和 trace id 使用根运行 UUID（OTLP 去掉连字符）。

Span 覆盖 Agent、子代理、模型、工具和中间件耗时。Agent、模型和工具的输入输出属性可能含系统提示、历史和完整工具结果。中间件里的活动对象不会原样导出。token 事件不保留。

取消或超时可能使原生模型结束回调缺失。本地 tracer 在异常根执行收尾时，补齐本条 trace 中未结束且允许导出的模型记录，沿用原生 ID、输入、模型信息和父子关系，并标明 `interrupted locally`。结束时间采用根执行结束时间，表示本地等待截止点；未返回的模型输出和 Token 用量保持未知，不代表上游计算已经停止。已结束记录不覆盖，普通工具内部模型仍不导出。补收尾失败记录脱敏后的 `TRACE_SAVE_ERROR`，继续保存根 trace。

关联字段：

| 属性 | 含义 |
| --- | --- |
| `langsmith.metadata.dataagent_run_id` | 原生运行 ID |
| `langsmith.metadata.dataagent_parent_run_id` | 父运行 |
| `langsmith.metadata.dataagent_root_run_id` | 宿主轮次号；AG-UI 使用输入 `runId`，A2A 由服务端生成 |
| `langsmith.metadata.dataagent_request_id` | A2A 创建请求号，与该次 `X-Request-ID` 一致 |
| `langsmith.metadata.dataagent_a2a_task_id` | A2A Task ID |
| `langsmith.metadata.dataagent_a2a_message_id` | A2A 输入 Message ID |
| `langsmith.metadata.thread_id` | 会话 |

导出发生在根运行结束回调：排空队列、刷 provider、只提交这一条 trace。并发调用共用导出器，文件分开。进程被强杀时可能没有文件。保存失败记 `TRACE_SAVE_ERROR`，不改变 Agent 结果。

Trace 文件名使用原生根运行号，不是宿主 `root_run_id`；二者通过 metadata 和 A2A trace 保存日志关联。图已成功而 checkpoint 保存失败时，图 trace 可以成功，但 A2A Task 会失败，诊断阶段为 `checkpoint_settle`。

`tracing_context(enabled=False)` 可以关掉这次追踪。调用方另附的远程 tracer 由调用方负责。

已知密钥和常见 API key、Bearer 模式会脱敏。业务数据仍在文件里。没有自动清理策略。

## 流上的标识

SSE 的 `RUN_FINISHED.metadata.durationMs` 只表示服务端这一轮的耗时。它不写入 Checkpoint。TUI 的完成行同样只存在于界面。
