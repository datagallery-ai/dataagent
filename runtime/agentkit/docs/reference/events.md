# 事件

`POST /dataagent/stream` 使用 AG-UI SSE。编码来自 `ag_ui.encoder.EventEncoder`。适配器是 `ag_ui_langgraph`，名称为 `dataagent-v2`，`emit_raw_events` 为 false，`subagent_visibility` 为 `hidden`。

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
| `RUN_ERROR` | `error`。成功 Checkpoint 仍是上一轮的 | 保留已显示的部分输出和错误码。不要用同一个 `runId` 重放 |
| 连接断开，没有终止事件 | `interrupted` | 完成情况未知。恢复时服务回到最后成功 Checkpoint，不重放 |

同一 `threadId` 在运行中再发请求会得到 HTTP 409。等这轮结束或确认中断后，用新的 `runId` 和新的用户消息继续。

消息 ID 若已在 Checkpoint 里，服务拒绝编辑和重放。

TUI 把这三种结果画成完成、失败或 `Interrupted — completion unknown`。这些摘要不写回 Checkpoint。

## 流的范围

子代理运行包含在根运行中，适配器的 `subagent_visibility` 为 `hidden`。产物通过会话的 `outputs/` 目录读取，见 [HTTP 接口](rest-api.md)。`after_model` 在每次模型调用返回后于服务内执行，不对应逐 token 的 AG-UI 事件。
