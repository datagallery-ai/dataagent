# 事件

`POST /dataagent/stream` 使用 AG-UI SSE。编码来自 `ag_ui.encoder.EventEncoder`。适配器是 `ag_ui_langgraph`，名称为 `dataagent`，`emit_raw_events` 为 false，`subagent_visibility` 为 `attributed`。

服务自己发送权威的开始和结束事件。适配器发出的 `RUN_STARTED`、`MESSAGES_SNAPSHOT` 和 `STATE_SNAPSHOT` 会被丢掉，避免把恢复用的旧 Checkpoint 再快照一次。

## 正常结束

顺序：

1. `RUN_STARTED`，带 `threadId` 和 `runId`
2. 适配器的中间事件，例如文本和工具调用
3. `MESSAGES_SNAPSHOT`，内容是图结束后的消息
4. `RUN_FINISHED`，`metadata.durationMs` 是服务端单调时钟从开始到结束的毫秒数

结束前会检查图没有未完成的 `next`。若图停在未完成的节点，该轮以 `RUN_ERROR` 结束。

上游必须正好有一个终止事件，且后面不能再有事件。否则服务视为不完整流。

## 失败

`RUN_ERROR` 的 `code` 和 `message` 经过 `safe_error`。`code` 通常是异常类名。消息最长 1500，并去掉已知密钥。

上游若已经发出 `RUN_ERROR`，服务改写其 code 和 message 后再转发，并把会话标为 `error`。

缺少终止事件、超时、扩展以外的执行异常，也编码成 `RUN_ERROR`。超时使用 `dataagent.limits.timeout_seconds`，包住模型重试、退避和工具时间。

## 客户端应如何处理

| 终止 | 会话状态 | 客户端 |
| --- | --- | --- |
| `RUN_FINISHED` | `complete`，并记下新的 Checkpoint | 用最后的 `MESSAGES_SNAPSHOT` 作为该轮消息。可以显示 `durationMs` |
| `RUN_ERROR` | `error`。图里已经有消息时，索引指向这一轮到达的 Checkpoint | 保留已显示的部分输出和错误码。不要用同一个 `runId` 重放。下一轮留在同一线程 |
| 显式取消或连接断开，没有终止事件 | `interrupted` | 和停止同一条存储结果。有 Checkpoint 就从它继续，否则读该线程最新图状态。不新开线程 |

同一 `threadId` 在运行中再发请求会得到 HTTP 409。等这轮结束或确认中断后，用新的 `runId` 和新的用户消息继续。

消息 ID 若已在 Checkpoint 里，服务拒绝编辑和重放。

TUI 把这三种结果画成完成、失败或 `Interrupted — continue this conversation`。这些摘要不写回 Checkpoint。

## 流的范围

子代理运行包含在根运行中。服务保留适配器的原生进度事件，不转换成自定义事件：

| 事件 | 用途 |
| --- | --- |
| `SUBAGENT_STARTED` | 通过 `subagentRunId` 标识子代理，通过 `parentToolCallId` 关联外层 `task`；嵌套子代理另有 `parentSubagentRunId` |
| `STEP_STARTED` / `STEP_FINISHED` | 通过 `subagentRunId` 和 `stepName` 展示该子代理的图节点进度 |
| `SUBAGENT_FINISHED` / `SUBAGENT_ERROR` | 结束子代理的进度。它们不代表外层工具或根运行已经完成 |

子代理文本、内部工具和状态事件不进入父对话流。最终消息快照仍取自根图。子代理错误消息与根错误一样经过脱敏。产物通过会话的 `outputs/` 目录读取，见 [HTTP 接口](rest-api.md)。`after_model` 在每次模型调用返回后于服务内执行，不对应逐 token 的 AG-UI 事件。

当前 Python 依赖已提供上述子代理事件；`@ag-ui/core 0.0.57` 尚未声明生命周期类型。TUI 在解析边界补齐其原生格式。网页里的 CopilotKit 客户端会丢掉未知类型，并把步骤名当成全局唯一，因此浏览器在校验前把 `SUBAGENT_*` 收成名为 `subagent` 的 `CUSTOM` 事件，并把子步骤名改成 `子运行 ID/原步骤名`，展示时仍用原来的 `stepName`。步骤事件本身继续使用 SDK 的 schema。非流式模型下，子代理事件可能早于外层 `TOOL_CALL_START`，客户端按父工具 ID 合并，不依赖到达顺序猜测父任务。

TUI 在任务下显示一行当前阶段与耗时；`Ctrl+O` 可查看已收到的步骤和重复执行记录。耗时由客户端单调时钟计量。30 秒没有新进度时显示 `waiting for update`，这不代表服务仍存活，也不代表任务失败。步骤历史只保留于当前 TUI 会话，恢复 Checkpoint 不重放历史进度。
