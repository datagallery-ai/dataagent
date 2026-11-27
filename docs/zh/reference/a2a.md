# A2A 参考

发现、发送消息和查询任务的可运行示例见 [A2A 接入指南](../guides/integrate/a2a.md)。

`dataagent serve` 和 `dataagent serve-a2a` 都将 Agent 暴露为 A2A 1.0 服务。发现接口发布 Agent Card，调用统一使用 JSON-RPC 2.0。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /.well-known/agent-card.json` | 匿名发现 Agent Card |
| `POST /a2a/jsonrpc` | 提交消息、查询或取消 Task。请求带 `A2A-Version: 1.0` |

Agent Card 只发布 JSONRPC 绑定，声明 `capabilities.streaming: false`。发现响应的接口地址使用请求的 `Host`，缺省为 `127.0.0.1`。协议取 `X-Forwarded-Proto` 或请求 URL 的协议。可配置 `a2a.card.name` 和 `a2a.card.description`，两个字段分别默认 `DataAgent`。技能由服务提供，ID 为 `chat`。配置在重启后生效，字段见 [A2A 配置](configuration.md#a2a)。

`dataagent serve` 同时提供 A2A 和 [REST 接口](rest-api.md)。即使对话引擎未就绪，也能读取 Agent Card。独立的 `dataagent serve-a2a` 只提供上表的 A2A 路由，可用 `--host`、`--port` 覆盖 `server.host`、`server.port`。两个进程都占用同一 Home 的 `backend.lock`，不能对同一个 Home 同时启动。启动方式和参数见 [CLI 参考](cli-and-environment.md#dataagent-serve-a2a)。

<a name="send-message"></a>

## 发一条消息

使用 `SendMessage` 提交一轮对话。见[可运行的请求](../guides/integrate/a2a.md#send-message)。

<a name="request-parameters"></a>

### 请求参数

以下按本服务的请求响应调用说明；字段位于 JSON-RPC `params` 内时写出完整路径。

| 字段/请求头 | 必填性 | 默认与约束 |
| --- | --- | --- |
| `Content-Type: application/json` | 必填 | JSON 请求正文 |
| `A2A-Version: 1.0` | 必填 | 省略或不匹配会收到 JSON-RPC 版本错误 |
| `Authorization: Bearer <token>` | 条件必填 | 仅服务端启用令牌后要求，查询/取消也相同 |
| `jsonrpc` | 必填 | 固定 `"2.0"` |
| `id` | 请求响应调用必填 | 字符串或数值，用于配对；不是任务号或幂等键 |
| `method` | 必填 | 区分大小写，如 `SendMessage` |
| `params.message` | `SendMessage` 必填 | A2A Message 对象 |
| `params.message.messageId` | 必填 | 非空，新消息用新编号；不承诺执行去重 |
| `params.message.role` | 必填 | 用户输入使用 `"ROLE_USER"` |
| `params.message.parts` | 必填 | 普通消息需要非空 `text`；HITL 恢复使用下文约定的单个 `data` part |
| `params.message.contextId` | 可选 | 省略生成 UUID；普通续聊复用返回值，格式为 `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` |
| `params.message.taskId` | 可选 | 新任务省略；人工恢复引用待答复 Task。用已结束会话的 `contextId` 发新消息时省略 `taskId` |
| `params.configuration` | 可选 | 省略时同步等待本轮结果 |
| `params.configuration.returnImmediately` | 可选 | 默认 `false`：等结果；`true`：先返回任务再查询 |
| `params.configuration.historyLength` | 可选 | 非负整数，限制返回的任务消息历史；不清除服务端会话上下文 |

`messageId/role/parts` 的协议必填由已安装 A2A SDK 校验；文本非空和 context 安全性由本服务执行器校验。不要把主模型、插件、数据库或工作区当成请求参数临时覆盖。其他协议字段不等于宿主支持相应能力。

普通消息只读取文本 part。空文本或非法 `contextId` 会得到 `TASK_STATE_FAILED`，错误码为 `BAD_REQUEST`，模型不会运行。成功时结果为 `result.task.artifacts` 中名为 `dataagent_result` 的文本 artifact，任务状态为 `TASK_STATE_COMPLETED`。

`contextId` 是共享会话的 `threadId`，也映射为 LangGraph 的 `thread_id`。同一 context 的普通 A2A 调用串行执行，并从共享会话索引记录的 checkpoint 继续。报错或取消也会保存图已经到达的 checkpoint。会话归属是 Server 启动时选择的用户（默认 `default`，可用 `--user` 指定）。同一 ID 已归属其他用户时返回失败 Task，错误码为 `FORBIDDEN`。

<a name="authentication"></a>

## 鉴权

默认不启用认证。设置 `DATAAGENT_A2A_AUTH_TOKEN` 后，两个服务入口都校验 `Authorization: Bearer <token>`。令牌取值优先级：`serve-a2a --auth-token` → 非空进程变量 → 所选环境文件（`--env-file` 或 Home `.env`）。`serve` 没有 `--auth-token` 参数，使用有效环境中的令牌；改变后重启。不要把它与模型 `LLM_API_KEY`、出站 `a2a.remote_agents[].auth_token` 混用。

| 范围 | 行为 |
| --- | --- |
| `POST /a2a/jsonrpc` 全部方法 | 启用认证时都要 Bearer，包括查询、取消和任务列表 |
| Agent Card | 可匿名读取。启用令牌时，`securitySchemes` 和 `securityRequirements` 声明 JSON-RPC 需要 HTTP Bearer 认证，令牌值由部署方另行提供 |
| `serve` 的 REST/AG-UI/健康/产物接口 | 此令牌不保护这些接口 |
| 调用方身份 | 同一个共享令牌，不提供个人登录、任务所有权隔离或租户隔离 |

令牌的设置和发送方式见[接入指南](../guides/integrate/a2a.md#connect)。

缺少或错误令牌返回 HTTP 401，正文为 `{"success":false,"error":{"code":"UNAUTHORIZED","message":"Invalid or missing Bearer token."}}`，不进入模型执行。未配置令牌时可省略 Authorization。实现见 [Bearer 校验](../../../dataagent/server/a2a/server.py)。

<a name="request-diagnostics"></a>

## 请求号与诊断

每个 `/a2a/jsonrpc` 请求都有服务端生成的 `X-Request-ID` 响应头，包括鉴权失败、协议错误和 `serve` 的引擎未就绪响应。它只用于报障关联，不是用户身份或幂等键。调用方的 JSON-RPC `id` 仍用于请求响应配对。

| ID | 来源与作用 |
| --- | --- |
| `message.messageId` | A2A 消息号，新消息使用新编号；SDK 客户端自动生成 |
| `message.contextId` | A2A 会话号，当前直接映射内部 `thread_id`；首条可省略，续聊复用返回值 |
| `task.id` | SDK 为任务生成；新任务不传 `message.taskId`，不能自行创建任务号 |
| `metadata.dataagent.requestId` | 创建任务的 HTTP 请求号，与该次 `X-Request-ID` 一致 |
| `metadata.dataagent.rootRunId` | 宿主轮次号，关联会话索引与图 trace；输入校验拒绝时没有此字段 |

成功和失败 Task 都包含 `metadata.dataagent`，`schemaVersion` 为 `1`。失败示例：

```json
{
  "schemaVersion": 1,
  "requestId": "<server-request-id>",
  "messageId": "<message-id>",
  "rootRunId": "<host-run-id>",
  "error": {
    "code": "TIMEOUT",
    "stage": "agent_invoke",
    "message": "TimeoutError"
  }
}
```

成功时没有 `error`。查询或取消任务不会把创建请求号替换成查询、取消请求的 `X-Request-ID`。

| 错误层级 | 客户端读取位置 |
| --- | --- |
| HTTP 请求被宿主拒绝 | 鉴权失败返回 HTTP 401 和 `UNAUTHORIZED`。在 `serve` 中，对话引擎未就绪返回 HTTP 503 和 `CONVERSATION_UNAVAILABLE`。都使用 HTTP 错误信封 |
| JSON-RPC 解析、版本、方法、参数或任务查询错误 | 读取 JSON-RPC 顶层 `error`。未知方法为 `-32601`，任务不存在为 `-32001`，取消已完成任务为 `-32002` |
| 任务执行失败 | HTTP 200，Task 状态为 `TASK_STATE_FAILED`。按 `metadata.dataagent.error.code` 分支，说明文本用于诊断 |

| `error.code` | 含义 |
| --- | --- |
| `BAD_REQUEST` | 空文本或非法 context 等输入校验失败 |
| `CONFLICT` | 会话正被 REST 运行占用，或普通 A2A 消息试图进入已有待决中断的会话 |
| `FORBIDDEN` | 会话归属于另一 Server 启动用户 |
| `TIMEOUT` | 执行或已识别的模型超时 |
| `MODEL_ERROR` / `TOOL_ERROR` | 已分类的模型或工具错误 |
| `CONFIG_ERROR` / `LIMIT_EXCEEDED` / `LOOP_DETECTED` | 配置、调用限额或循环检测错误 |
| `INTERNAL_ERROR` | 其他未分类异常，包括 checkpoint 保存失败 |
| `CANCELLED` | 取消，Task 状态为 `TASK_STATE_CANCELED` |

`stage` 区分 `validation`、`queue`、`session_prepare`、`agent_invoke`、`checkpoint_settle` 和 `response`。模型成功但 checkpoint 保存失败时，任务仍失败，阶段为 `checkpoint_settle`。

`dataagent.limits.timeout_seconds` 在入站 A2A 中只限制 Agent 的 `ainvoke`；同 context 排队、会话准备和 Checkpoint 保存发生在这层时限之外。客户端不能用总耗时是否超过该值判断超时机制是否有效。客户端超时后，如有 Task ID，可[查询当前状态](#tasks)。

<a name="tasks"></a>

## 查询和取消任务

所有方法继续使用同一 JSON-RPC 地址、版本和鉴权。表中的参数位于外层 `params`。

| 方法 | 参数 | 返回值 |
| --- | --- | --- |
| `SendMessage` | `message`，可选 `configuration` | `result.task` |
| `GetTask` | `id` 为 Task ID，可选 `historyLength` | `result` 为 Task |
| `CancelTask` | `id` 为 Task ID | `result` 为 Task，成功后状态为 `TASK_STATE_CANCELED` |
| `ListTasks` | 可传 `{}`，用 `contextId` 筛选，用 `includeArtifacts: true` 包含产物 | `result.tasks` 为实例的共享任务列表 |

`params.id` 是服务端生成的任务号，外层 JSON-RPC `id` 用于配对请求和响应。停止轮询或关闭客户端不会发出取消请求。需要取消时显式调用 `CancelTask`。默认 TaskStore 在内存中，重启后旧任务号失效。普通会话可以继续读取同一 Home 中的 checkpoint。处于人工等待的会话有额外的[恢复限制](#a2a-hitl)。

<a name="call-modes"></a>

### 等待结果与异步任务

两种方式都通过非流式 `SendMessage`，由 `configuration.returnImmediately` 选择：

| 方式 | 配置 | 返回与后续操作 |
| --- | --- | --- |
| 同步等待结果 | 省略或 `false` | HTTP 请求等到本轮结束或需要人工输入，Task 在 `result.task`；检查其实际状态 |
| 异步任务 | `true` | 先取得 `result.task.id`，后台继续执行；用 `GetTask` 轮询到终态，必要时 `CancelTask` |

`SendStreamingMessage` 和 `SubscribeToTask` 返回 JSON-RPC 错误 `-32004`，Push 通知返回 `-32003`。

首次返回时，较短的任务可能已经完成。发送和查询示例见[接入指南](../guides/integrate/a2a.md#send-message)。

查询时可能读到 `TASK_STATE_SUBMITTED`、`TASK_STATE_WORKING` 或等待答复的 `TASK_STATE_INPUT_REQUIRED`。客户端按完整枚举识别终态 `TASK_STATE_COMPLETED`、`TASK_STATE_FAILED`、`TASK_STATE_CANCELED` 和 `TASK_STATE_REJECTED`。完成后读 artifacts，失败时读 metadata。

本服务没有执行去重、自动重试或幂等承诺；超时后不要直接重发可能产生副作用的任务。

<a name="a2a-hitl"></a>

## 原生 HITL 暂停与恢复

A2A 适配 LangGraph 原生 `interrupt()` / `Command(resume=...)`，不要求流式接口。业务工具或中间件触发中断后，Task 返回 `TASK_STATE_INPUT_REQUIRED`；此时未完成，不生成最终回答 artifact。同步调用在中断处返回，非阻塞调用可通过 `GetTask` 看到相同状态。

`status.message.parts` 同时提供文本和以下结构化 `data` part。`kind` 是本服务定义的数据约定；`interrupts[].id/value` 对应原生中断，具体值由触发它的工具或中间件定义。待办使用 `pauseId` 标识当前暂停轮次。对外载荷保留完整的非敏感内容，密码或令牌等字段会脱敏。审批待办提供工具名称、脱敏参数和审批规则。执行依据服务端 Checkpoint 保存的参数或经批准的编辑。

```json
{
  "data": {
    "kind": "dataagent.hitl.request",
    "pauseId": "pause-1",
    "interrupts": [{
      "id": "interrupt-1",
      "value": {
        "kind": "human_feedback",
        "question": "按含税口径统计吗？",
        "tool_call_id": "call-1"
      }
    }]
  }
}
```

收到人工答复后，继续调用同一个 `POST /a2a/jsonrpc`，携带版本头及按需启用的 Bearer 头。消息使用原 `taskId/contextId` 和新的 `messageId`，只包含一个 `data` part：

```json
{
  "jsonrpc": "2.0", "id": "resume-1", "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER", "messageId": "human-response-1",
      "taskId": "task-1", "contextId": "thread-1",
      "parts": [{"data": {
        "kind": "dataagent.hitl.response",
        "pauseId": "pause-1",
        "responses": {"interrupt-1": "按含税口径。"}
      }}]
    }
  }
}
```

示例 ID 必须替换成实际返回值。每次暂停返回新的 `pauseId`，答复须带上当前值，即使原生中断 ID 与上一次相同。`responses` 是“原生中断 ID → 原生恢复值”的映射。对于 `data-qa__request_human_feedback`，值是答复文本；对于原生工具审批中间件，值是它要求的决定对象，例如：

```json
{"interrupt-1":{"decisions":[{"type":"approve"}]}}
```

普通原生中断的 `value` 含有绝对值大于等于 `2^53` 的数值时，待办以 `valueJson` 字符串携带完整 JSON，替代 `value`。例如 `{"id":"interrupt-1","valueJson":"{\"records\":[9223372036854775809]}"}`。答复含这类数值时，用 `responsesJson` 替代 `responses`，例如 `"responsesJson":"{\"interrupt-1\":{\"id\":9223372036854775809}}"`。客户端读写这两种字符串时须保留数值精度。直接用普通数值提交超范围答复会收到参数错误，待办保留。

审批的 `action_requests[].args` 通常是对象。如果其中含有绝对值大于等于 `2^53` 的数值，服务端将整个参数对象编码为 JSON 文本，以保留整数 ID 的每一位。客户端可直接展示文本；编辑时应使用支持任意精度整数的解析器，或保留原始 JSON 文本。在 `edited_action.args` 中以字符串提交这份 JSON，例如 `"{\"id\":9223372036854775809}"`。直接提交含超范围数值的对象会收到参数错误，原待办可继续使用。直接批准时只提交 `approve`，工具使用 checkpoint 中的原参数。若待办含脱敏字段，编辑参数中仍出现 `[REDACTED]` 会被拒绝；要编辑请提供实际参数，要保留整份原参数请直接批准。

拒绝示例为 `{"decisions":[{"type":"reject","message":"本次不允许执行。"}]}`。可选决定由原生中断的 `review_configs` 给出；批量工具审批按 `action_requests` 顺序提供对应决定。服务端把值转换为原生恢复命令，批准才执行工具，拒绝由原生中间件返回拒绝结果。澄清答复不等于审批通过。

- 恢复后保持同一个 A2A Task，可再次进入 `INPUT_REQUIRED`，也可完成、失败或取消。Task metadata 保留创建请求的关联 ID，每次恢复另有自己的请求审计和 Trace。完成日志的 `root_run_id` 指向本轮执行，`task_root_run_id` 关联 Task 创建时的运行。
- 入口校验拒绝错误或过期中断号、审批决定格式不符、缺少原任务以及发给等待中任务的普通文本，并返回 JSON-RPC 参数错误。修正后可用原任务和有效中断号重试。
- 答复如果通过入口校验后才因并发重试而过期，服务返回 Task 的当前状态。若仍在等待人工输入，请按返回的新待办答复。
- 恢复时如果同一会话被其他运行临时占用，Task 回到 `INPUT_REQUIRED`，状态消息提示稍后重试，结构化待办保留原 `pauseId` 和中断号。会话释放后可重新提交这份答复。
- 等待期间会保存 checkpoint 并释放执行占用。REST 会话查询仍可读取这条会话，显示为 `interrupted` 并提示等待人工输入。REST 发起新一轮会收到 HTTP 409。同一 context 的新 A2A Task 也无法绕过待决中断，请向原 Task 提交结构化响应。
- `CancelTask` 在会话收尾后返回取消状态。取消待人工任务时，会话标为 `interrupted`，并保留检查点供查看。重新开始业务需使用新的 context。取消或终态 Task 不接受恢复。
- 使用默认进程内 TaskStore 时，重启会丢失原 Task。等待中的 checkpoint 仍保留。要再次执行业务，请使用新的 `contextId`。
- 入站恢复由此 JSON-RPC 原 Task 路径处理。默认客户端的[出站调用](#outbound)只发送文本。

两个服务入口、轮询、重复中断、审批和取消的验证方法见[开发者测试](../../development/testing.md#a2a-testing)。

<a name="inbound-and-subagents"></a>

## 入站调用与本地子代理

A2A 请求默认进入主代理。主代理可通过 `task` 委派已启用的本地编译型子代理，例如 nl2sql；这不要求启动第二个 A2A 服务，也不需要 `a2a.remote_agents`。验证这条链路时不要设置 `dataagent.entry: "nl2sql"`，否则用户消息会直接进入 nl2sql，绕过主代理。

`TASK_STATE_COMPLETED` 表示这一轮 Agent 执行完成，不证明业务结果一定成功或正确。nl2sql 的普通业务错误可能作为含 `error` 的工具结果返回，主代理解释该错误后，外层 Task 仍可完成。验收时还要检查实际委派、子代理结果和查询产物；协议测试与插件测试的关系见 [测试](../../development/testing.md#a2a-nl2sql)。

<a name="outbound"></a>

## 调用其他 Agent

每个已配置的远端都注册为 `a2a__<id>` 工具。工具发送文本，并为每条本地线程保留独立的远端会话。服务根地址、令牌和超时见[出站配置字段](configuration.md#a2a)与[配置示例](../guides/integrate/a2a.md#outbound)。

工具等待当轮结果或人工输入提示。等待输入时，工具返回以 `INPUT_REQUIRED` 开头的文本，并在同一线程的下一次调用中携带原任务 ID。结构化人工反馈需使用[直接 JSON-RPC](#a2a-hitl)。Python 调用、结果字段和异常见 [Python API](python-api.md#a2a-api)。
