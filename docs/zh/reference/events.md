# AG-UI 事件与运行契约

Web、TUI 和第三方客户端使用 `POST /api/v1/runs` 提交对话。这里定义请求负载和响应流。路由、HTTP 错误和会话读取见 [REST API](rest-api.md)，可运行示例见 [AG-UI 接入](agent-runtime.md)。

<a name="request"></a>

## 请求

请求体为 JSON。普通对话只提交本轮的新消息，服务端根据 `threadId` 加载历史。已保存的 Checkpoint 优先用于续接，缺少保存点时读取该线程的最新图状态。

| 字段 | 约束 |
| --- | --- |
| `threadId`、`runId` | 长度 1–128，首字符为 ASCII 字母或数字，其余字符还可使用 `_` 和 `-` |
| `messages` | 普通对话恰好一条消息，包含 `id`、`role: "user"` 和非空白文本 `content`；提交人工回答时发送 `[]` |
| `state` | 可省略，或发送 `null`、`{}` |
| `tools`、`context`、`forwardedProps` | 必填，分别发送空的 `[]`、`[]`、`{}` |
| `answers` | 提交[人工回答](#hitl)时发送非空的 `interruptId` 到文本回答的映射 |
| `resume`、`parentRunId` | 客户端省略；恢复会话沿用 `threadId`，恢复人工交互使用 `answers` |

服务端拒绝未知顶层字段、客户端注入的状态、工具和上下文，以及把子代理消息作为用户消息提交。模型、插件和工作区由服务配置决定。

每个会话同时只接受一轮。活跃的 `runId` 在同一服务进程内必须唯一，即便请求的线程不同。服务拒绝重用会话最后一次记录的 `runId`，也拒绝使用已加载历史里的消息 ID 进行编辑或重放。继续对话时为运行和消息生成新 ID。

<a name="sse-response"></a>

## SSE 响应

接受请求后返回 HTTP 200 和 `text/event-stream`，并设置 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`。客户端从 SSE 的 `data:` 帧读取 JSON。流建立后，执行失败通过 `RUN_ERROR` 表达。流建立前的错误返回 [REST 错误响应](rest-api.md#http-errors)。

<a name="completed"></a>

## 正常结束

正常完成时按以下顺序发送事件。

1. `RUN_STARTED`，带 `threadId` 和 `runId`
2. 适配器的中间事件，例如文本和工具调用
3. `CUSTOM`，`name="human_feedback"`，`value=[]`，清除已解决的交互
4. `MESSAGES_SNAPSHOT`，内容是图结束后的消息
5. `RUN_FINISHED`，`metadata.status="completed"`，`metadata.durationMs` 是服务端单调时钟从开始到结束的毫秒数

结束前会检查图没有未完成的 `next`。HITL 等待按下文结束本轮 HTTP 流。其他未完成节点以 `RUN_ERROR` 结束。

<a name="hitl"></a>

## 等待用户回答

HITL 暂停也用 `RUN_FINISHED` 结束本轮流，但 `metadata.status="waiting_for_input"`，
表示等待回答。最终消息快照前的 `CUSTOM name="human_feedback"` 携带当前全部待回答项：

```json
{"type":"CUSTOM","name":"human_feedback","value":[
  {"type":"human_feedback","interruptId":"<id>","message":"请选择身份字段","options":["user_id","device_id"]}
]}
```

`options` 供展示，用户也可以输入文本；回答无效时附带可选的 `validationError`。
客户端用数组替换当前的待回答项。通过同一个 `POST /api/v1/runs` 提交非空的 `answers`，沿用 `threadId`、使用新 `runId`，并发送空的 `messages`。

```json
{
  "threadId": "thread-001",
  "runId": "run-002",
  "messages": [],
  "answers": {"<id>": "user_id"},
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
```

将 `<id>` 换成事件里的 `interruptId`。多个并行交互可分批回答。等待回答时发送新消息，或提交未知、已解决的交互 ID，会收到 HTTP 409。空白回答会再次暂停，待回答项带有 `validationError`。

刷新客户端或重启服务后，可从 `GET /api/v1/sessions/{threadId}` 的 `pendingInteractions` 恢复待回答项。用户思考期间没有活动 HTTP 流，不计入单轮执行超时。通过 A2A 发起的人工交互须在 [A2A](a2a.md#a2a-hitl) 中恢复。

<a name="errors"></a>

## 失败

`RUN_ERROR.code` 取下表中的固定值。服务端脱敏 `message`，并限制其长度为 1500 个字符。

| `code` | 含义 |
| --- | --- |
| `TIMEOUT` | 超过 `dataagent.limits.timeout_seconds`，或模型请求超时 |
| `CANCELLED` | 客户端调用了 `POST /api/v1/runs/{runId}/cancel` |
| `LOOP_DETECTED` | 循环检测终止了本轮 |
| `LIMIT_EXCEEDED` | 超过 `model_calls_per_agent` 或 `tool_calls_per_agent` |
| `INCOMPLETE_STREAM` | 上游没有终止事件、重复终止，或终止后仍有事件 |
| `CONFIG_ERROR` | 配置无效 |
| `MODEL_ERROR` | 模型服务返回错误 |
| `TOOL_ERROR` | 工具或远端 Agent 调用失败 |
| `INTERNAL_ERROR` | 其他错误 |

客户端可以按 `code` 分支；新增取值会写进本表。`message` 是给人看的说明，不要按它分支。服务端日志另外记录异常类名（`error=`）。

上游若已经发出 `RUN_ERROR`，服务改写其 message 后再转发，并把会话标为 `error`。上游 code 不在上表时改为 `INTERNAL_ERROR`。

缺少终止事件、超时、扩展以外的执行异常，也编码成 `RUN_ERROR`。超时使用 `dataagent.limits.timeout_seconds`，包住模型重试、退避和工具时间。

<a name="terminal-events"></a>

## 客户端应如何处理

| 终止 | 会话状态 | 客户端 |
| --- | --- | --- |
| `RUN_FINISHED`，`metadata.status` 为 `completed` | `completed`，并记下新的 Checkpoint | 用最后的 `MESSAGES_SNAPSHOT` 作为该轮消息。可以显示 `durationMs` |
| `RUN_FINISHED`，`metadata.status` 为 `waiting_for_input` | `waiting_for_input`，保存暂停点 | 展示待回答项；保持未完成工具，提交回答后继续 |
| `RUN_ERROR` | `error`。图里已经有消息时，索引指向这一轮到达的 Checkpoint | 保留已显示的部分输出和错误码。不要用同一个 `runId` 重放。下一轮留在同一线程 |
| `RUN_ERROR`，`code` 为 `CANCELLED` | `interrupted` | 显式取消。按“已停止”显示，不按失败显示。有 Checkpoint 就从它继续，否则读该线程最新图状态。不新开线程 |
| 连接断开，客户端没收到终止事件 | 尚未完成的会话记为 `interrupted` | 重连后读取会话详情确认结果，再决定是否发送下一条消息 |

终态事件里的耗时和客户端显示的运行摘要不写回 Checkpoint。

<a name="stream-scope"></a>

## 流的范围

子代理运行包含在根运行中。服务保留适配器的原生进度事件，不转换成自定义事件：

| 事件 | 用途 |
| --- | --- |
| `SUBAGENT_STARTED` | 通过 `subagentRunId` 标识子代理，通过 `parentToolCallId` 关联外层 `task`；嵌套子代理另有 `parentSubagentRunId` |
| `STEP_STARTED` / `STEP_FINISHED` | 通过 `subagentRunId` 和 `stepName` 展示该子代理的图节点进度 |
| `SUBAGENT_FINISHED` / `SUBAGENT_ERROR` | 结束子代理的进度。它们不代表外层工具或根运行已经完成 |

根运行的其他事件按 AG-UI 原样转发：`TEXT_MESSAGE_*`、`TOOL_CALL_*`、`REASONING_*`、根图的 `STEP_STARTED` / `STEP_FINISHED`，以及适配器或工具发出的 `CUSTOM` 事件。服务端定义 `human_feedback` 交互事件；客户端应忽略不认识的 `CUSTOM`。服务端丢弃上游的 `RUN_STARTED`、`MESSAGES_SNAPSHOT` 和 `STATE_SNAPSHOT`，自己发出一次 `RUN_STARTED`，在完成或等待回答时发出最终的 `MESSAGES_SNAPSHOT`。

子代理文本、内部工具和状态事件不进入父对话流。最终消息快照仍取自根图。子代理错误消息与根错误一样经过脱敏。产物通过[会话产物接口](rest-api.md#outputs)读取。`after_model` 在每次模型调用返回后于服务内执行，不对应逐 token 的 AG-UI 事件。

子代理进度可能早于外层 `TOOL_CALL_START`。客户端按 `parentToolCallId` 关联，不依赖到达顺序。历史进度不由 Checkpoint 重放。客户端适配见[实现索引](../../development/reference/rest-api.md#client-progress)。
