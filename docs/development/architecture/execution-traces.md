# 执行 Trace

一条 Trace 记录一次 Agent 根执行。它按父子关系组织 Agent、子代理、模型、工具和可导出的中间件执行情况。查找恢复对话所需的数据时，请看[状态与存储](state-and-storage.md)。

<a name="local-files"></a>

## 本地文件

已运行的图在根执行结束时写入一个 OTLP JSON 请求体：

```text
<home>/runtime/users/<user>/sessions/<thread>/traces/<native-root-run-id>.json
```

成功、失败和正常取消均可触发导出。请求在进入图之前被拒绝、主动关闭追踪或进程被强制终止时，可能没有文件。导出器收齐当前根执行的已结束 span，在同目录写临时文件，再用硬链接发布完整 JSON。目标文件已存在时保留原文件。文件模式为 `0600`，保存失败记录 `TRACE_SAVE_ERROR`，Agent 结果保持原有状态。并发轮次分别导出。当前没有自动清理策略。

默认图由 `build_graph` 绑定 `LocalLangChainTracer`。LangChain 回调经 LangSmith SDK 的 OTEL 模式写入独立的 `TracerProvider`。进程也可同时启用[实时 OTLP 导出](../../zh/guides/trace-export.md)，根执行结束仍会写本地文件。SDK 调用方可用 `tracing_context(enabled=False)` 关闭单次追踪。

<a name="trace-shape"></a>

## JSON 结构与字段

文件顶层为 `resourceSpans`。每个资源包含 `scopeSpans`，每个 scope 包含 `spans`。资源属性 `service.name` 为 `dataagent`。

| 字段 | 用途 |
| --- | --- |
| `traceId`、`spanId`、`parentSpanId` | 十六进制 ID。用前两项定位 span，用父 ID 还原调用关系。根 span 没有父 ID |
| `startTimeUnixNano`、`endTimeUnixNano` | 十进制字符串表示的 Unix 纳秒时间。先转成整数再计算耗时 |
| `name`、`status` | 节点名称与 OTLP 状态。图 span 的状态与最终 HTTP、A2A Task 结果可能不同 |
| `attributes[]` | 每项含 `key` 和带类型的 `value`，如 `stringValue` 或 `intValue` |
| `langsmith.span.kind` | 如 `llm` 或 `tool`，可用于按调用类型筛选 |
| `gen_ai.prompt`、`gen_ai.completion` | 可导出模型记录的输入和输出。通常以 JSON 字符串形式出现在属性中，依实际字段读取 |

Trace 文件名使用原生根运行 UUID。文件中的 `traceId` 使用同一根 UUID 的 32 位十六进制表示。REST 客户端传入的 `runId` 和 A2A Task ID 通过属性关联。

<a name="trace-correlation"></a>

## 如何关联到一次请求

| 属性 | 对应标识 |
| --- | --- |
| `langsmith.metadata.dataagent_run_id` | 当前节点的原生运行 ID |
| `langsmith.metadata.dataagent_parent_run_id` | 当前节点的原生父运行 ID |
| `langsmith.metadata.dataagent_root_run_id` | 宿主轮次号。AG-UI 使用请求的 `runId`，A2A 由服务端生成 |
| `langsmith.metadata.thread_id` | 会话 ID |
| `langsmith.metadata.dataagent_request_id` | A2A 创建请求号，对应这次请求的 `X-Request-ID` |
| `langsmith.metadata.dataagent_a2a_task_id` | A2A Task ID |
| `langsmith.metadata.dataagent_a2a_message_id` | A2A Message ID |

AG-UI 调用可在相应会话的文件中按宿主轮次号查根 span。A2A 可从 [调用关联日志](observability.md#a2a-correlation)的 `a2a.trace.saved` 查 `native_run_id` 和 `trace_file`。图已成功而 Checkpoint 保存失败时，图 Trace 可以显示成功，Task 仍以阶段 `checkpoint_settle` 报告失败。核对结果时要同时检查协议响应或日志。

<a name="trace-data-boundaries"></a>

## 数据范围与留存

- Trace 可能含系统提示、历史消息和完整工具结果。已知密钥和常见认证格式在本地与实时导出前脱敏。交给评测工具前仍需检查和按需移除业务数据。
- 普通工具内部直接调用的模型不单独导出。工具 span、Agent 和子代理自身的模型调用仍可见。文件不保留逐 token 事件。
- 超时或取消可能中断模型等待。根执行收尾时，tracer 尝试补齐已启动且允许导出的模型 span，标为 `interrupted locally`。结束时间是本地停止等待的时间，缺失的输出或用量保持未知。

DataAgent CLI 当前提供服务、A2A 和历史 SQL 命令。要将文件用于离线评测，可用[评测接入指南](../../zh/guides/trace-evaluation.md)中的方法读取、筛选并映射评测所需的字段。需要由接收端实时采集时，按 [Trace 导出指南](../../zh/guides/trace-export.md)配置 OTLP/HTTP。

实现和验证见[原生 tracer](../../../dataagent/extensions/tracing.py)、[本地导出器](../../../dataagent/extensions/trace_exporter.py)和 [Trace 测试](../../../tests/test_traces.py)。
