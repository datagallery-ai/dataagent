# A2A

`dataagent serve` 和 `dataagent serve-a2a` 把当前 Home 里的 Agent 暴露成 A2A 1.0 服务。其他 agent 通过 Agent Card 发现它，再用 JSON-RPC 发消息。实现在 `dataagent/server/a2a/`，图仍由 `create_agent` 装配。

| 用途 | 路径 |
| --- | --- |
| Agent Card | `GET /.well-known/agent-card.json` |
| JSON-RPC | `POST /a2a/jsonrpc` |

Card 声明 JSON-RPC，内置执行器通过 Agent Card 的 `capabilities.streaming: true` 声明支持流式。调用方每次请求通过 JSON-RPC 的 `method` 选择响应方式。接口 URL 按请求的 `Host` 填写。绑定 `0.0.0.0`、`::` 或 `::0` 时，未带 Host 的默认值是 `127.0.0.1`。其他 IPv6 地址写成带方括号的 URL。

`dataagent serve` 在已有会话和检查点的进程上挂这组路由，默认 `127.0.0.1:8790`。`dataagent serve-a2a` 是只跑这组路由的独立进程，默认也读 `server.host` 和 `server.port`，可用 `--host`、`--port` 覆盖。这些进程都占用 `backend.lock`，不要对同一个 Home 同时启动。

## 发一条消息

```bash
curl -X POST http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "role": "ROLE_USER",
        "messageId": "message-1",
        "contextId": "thread-1",
        "parts": [{"text": "hello"}]
      }
    }
  }'
```

### 请求参数：必填与可选

以下按本服务的请求响应调用说明；字段位于 JSON-RPC `params` 内时写出完整路径。

| 字段/请求头 | 必填性 | 默认与约束 |
| --- | --- | --- |
| `Content-Type: application/json` | 必填 | JSON 请求正文 |
| `A2A-Version: 1.0` | 必填 | 缺省按旧版协议处理，不可省略 |
| `Authorization: Bearer <token>` | 条件必填 | 仅服务端启用令牌后要求，查询/取消也相同 |
| `jsonrpc` | 必填 | 固定 `"2.0"` |
| `id` | 请求响应调用必填 | 字符串或数值，用于配对；不是任务号或幂等键 |
| `method` | 必填 | 区分大小写，如 `SendMessage` |
| `params.message` | `SendMessage` 和 `SendStreamingMessage` 必填 | A2A Message 对象 |
| `params.message.messageId` | 必填 | 非空，新消息用新编号；不承诺执行去重 |
| `params.message.role` | 必填 | 用户输入使用 `"ROLE_USER"` |
| `params.message.parts` | 必填 | 普通消息需要非空 `text`；HITL 恢复使用下文约定的单个 `data` part |
| `params.message.contextId` | 可选 | 省略生成 UUID；续聊复用返回值，路径安全 ID 最长 128 字符 |
| `params.message.taskId` | 可选 | 新任务省略；只允许引用已有任务，不能用它自建任务号，终态任务不能续接 |
| `params.configuration` | 可选 | 省略采用协议 SDK 默认值 |
| `params.configuration.returnImmediately` | 可选 | 默认 `false`：等结果；`true`：先返回任务再查询 |
| `params.configuration.historyLength` | 可选 | 非负整数，限制返回的任务消息历史；不清除服务端会话上下文 |

`messageId/role/parts` 的协议必填由已安装 A2A SDK 校验；文本非空和 context 安全性由本服务执行器校验。不要把主模型、插件、数据库或工作区当成请求参数临时覆盖。其他协议字段不等于宿主支持相应能力。

`contextId` 必须是最长 128 的路径安全 ID（字母、数字、下划线和连字符）。它同时是 LangGraph 的 `thread_id` 和会话目录名。省略时由协议生成 UUID。下一条消息从会话索引里记下的检查点继续，和 `/api/v1/runs` 是同一条。报错或取消也会把图已经到达的检查点写进这个索引。同一 context 上的调用串行执行。

普通消息只读取文本 part。空文本或非法 `contextId` 使任务进入 `TASK_STATE_FAILED`，不会调用模型。HITL 响应单独解析，不作为新的用户文本交给模型。成功时最终回答在 `result.task.artifacts`，状态是 `TASK_STATE_COMPLETED`。

这条会话记在进程用户（`default` 或 `--user`）的会话索引里。先校验归属及待决中断，再登记本次运行。`thread_id` 全局唯一：该 id 已经属于另一个 profile 时，任务失败，模型不会被调用。

默认匿名访问，所有访问者属于一个共享团队：能列举、读取、续接和取消同一实例内的任务。不同浏览器或客户端使用相同 `contextId` 就会进入同一段对话。日志里的 `user_id` 是进程 profile，不是访问者身份。

A2A、REST API、TUI 和 Web 共用 Home 下的会话、checkpoint、trace 与产物存储，各入口只适配协议和展示内容。A2A 协议 SDK 的内存 TaskStore 用于任务查询与取消，不另建一套持久业务记录；`taskId` 通过诊断 metadata 关联同一套内部轮次号。

## 鉴权

默认不启用认证。设置 `DATAAGENT_A2A_AUTH_TOKEN` 后，两个服务入口都校验 `Authorization: Bearer <token>`。令牌取值优先级：`serve-a2a --auth-token` → 非空进程变量 → 所选环境文件（`--env-file` 或 Home `.env`）。`serve` 没有 `--auth-token` 参数，使用有效环境中的令牌；改变后重启。不要把它与模型 `LLM_API_KEY`、出站 `a2a.remote_agents[].auth_token` 混用。

| 范围 | 行为 |
| --- | --- |
| `POST /a2a/jsonrpc` 全部方法 | 启用认证时都要 Bearer，包括查询、取消和任务列表 |
| Agent Card | 可匿名读取。启用令牌时，`securitySchemes` 和 `securityRequirements` 声明 JSON-RPC 需要 HTTP Bearer 认证，令牌值由部署方另行提供 |
| `serve` 的 REST/AG-UI/健康/产物接口 | 此令牌不保护这些接口 |
| 调用方身份 | 同一个共享令牌，不提供个人登录、任务所有权隔离或租户隔离 |

例如先在**服务端**环境设置令牌再启动服务：

```bash
export DATAAGENT_A2A_AUTH_TOKEN='replace-with-a-long-random-token'
uv run dataagent serve
```

调用终端单独设置相同值，再发送带认证头的请求：

```bash
export DATAAGENT_A2A_AUTH_TOKEN='replace-with-a-long-random-token'
curl -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -H "Authorization: Bearer $DATAAGENT_A2A_AUTH_TOKEN" \
  -d '{"jsonrpc":"2.0","id":"list-1","method":"ListTasks","params":{}}'
```

缺少或错误令牌返回 HTTP 401，正文为 `{"success":false,"error":{"code":"UNAUTHORIZED","message":"Invalid or missing Bearer token."}}`，不进入模型执行。未配置令牌时可省略 Authorization。后端读取 `.env` 不会替其他终端设置变量。实现见 [Bearer 校验](../../dataagent/server/a2a/server.py)。

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

错误分三层：HTTP 鉴权、就绪失败沿用错误信封；JSON-RPC 解析、版本、方法和任务不存在等错误由 SDK 返回标准 JSON-RPC 错误；执行失败仍是 HTTP 200 加失败 Task，通过 `metadata.dataagent.error` 判断原因，不要解析说明文本。

| `error.code` | 含义 |
| --- | --- |
| `BAD_REQUEST` | 空文本或非法 context 等输入校验失败 |
| `CONFLICT` | 会话被 Web/TUI 运行占用 |
| `FORBIDDEN` | 现有本地 profile 归属校验拒绝 |
| `TIMEOUT` | 执行或已识别的模型超时 |
| `MODEL_ERROR` / `TOOL_ERROR` | 已分类的模型或工具错误 |
| `CONFIG_ERROR` / `LIMIT_EXCEEDED` / `LOOP_DETECTED` | 配置、调用限额或循环检测错误 |
| `INTERNAL_ERROR` | 其他未分类异常，包括 checkpoint 保存失败 |
| `CANCELLED` | 取消，Task 状态为 `TASK_STATE_CANCELED` |

`stage` 区分 `validation`、`queue`、`session_prepare`、`agent_invoke`、`checkpoint_settle` 和 `response`。模型成功但 checkpoint 保存失败时，任务仍失败，阶段为 `checkpoint_settle`。

`dataagent.limits.timeout_seconds` 在入站 A2A 中限制一次 Agent 执行（流式和非流式）；同 context 排队、会话准备和 Checkpoint 保存发生在这层时限之外。客户端不能用总耗时是否超过该值判断超时机制是否有效。超时后先查询已经取得的 Task ID，不要把客户端断开或停止轮询当作 `CancelTask`。

## 查询和取消任务

所有方法仍使用 `POST /a2a/jsonrpc`，携带相同版本头。`GetTask`、`CancelTask` 的任务号在 `params.id`，与外层 RPC `id` 不同：

```json
{"jsonrpc": "2.0", "id": "query-1", "method": "GetTask", "params": {"id": "<task-id>"}}
```

`GetTask` 和 `CancelTask` 直接在 `result` 返回 Task；`SendMessage` 在 `result.task` 返回。`ListTasks` 在 `result.tasks` 返回共享任务列表，可使用 `params.contextId` 过滤。

### 流式调用

在 `POST /a2a/jsonrpc` 使用 `SendStreamingMessage`。请求头与 `SendMessage` 相同，`params.message` 沿用相同结构。

```json
{
  "jsonrpc": "2.0",
  "id": "stream-1",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "messageId": "stream-message-1",
      "parts": [{"text": "统计本月销售额"}]
    }
  }
}
```

响应为 `text/event-stream`。每个 SSE `data` 帧包含一个 JSON-RPC 响应。首个 `result.task` 提供 Task ID 和 Context ID。后续 `result.statusUpdate` 报告任务状态。模型提供文本时，`result.artifactUpdate` 发布执行中的预览。首个文本分片立即发送，后续分片按约 100 毫秒的窗口合并，暂停或完成前会送出待发送的文本。客户端按 `artifactId` 更新展示，`append: true` 时追加，否则替换。成功后，同一产物的最后一帧通过 `lastChunk: true` 和完整答案替换预览，随后是 `TASK_STATE_COMPLETED`。

使用编译型插件作为 `dataagent.entry` 时，在生成公开答案的模型调用上设置 LangChain 标签，例如 `model.ainvoke(messages, config={"tags": ["dataagent:a2a-public-text"]})`。只在这次模型调用上标记，图或节点上的标签会传给其子调用。未标记的插件仍在本轮完成时返回最终答案。

从旧版迁移的客户端，应从 `artifactUpdate` 读取答案文本。旧版在 `WORKING` 状态消息中返回累计文本。

触发人工确认时，本轮以 `TASK_STATE_INPUT_REQUIRED` 状态更新结束。调用方从该事件取待办，按 [HITL 恢复格式](#a2a-hitl) 将 `dataagent.hitl.response` 提交给同一 Task。用 `SendStreamingMessage` 提交可接收恢复后的 SSE，用 `SendMessage` 提交可接收单次 JSON 响应。

### 等待结果与异步任务

两种方式都通过非流式 `SendMessage`，由 `configuration.returnImmediately` 选择：

| 方式 | 配置 | 返回与后续操作 |
| --- | --- | --- |
| 同步等待结果 | 省略或 `false` | HTTP 请求等到本轮结束或需要人工输入，Task 在 `result.task`；检查其实际状态 |
| 异步任务 | `true` | 先取得 `result.task.id`，后台继续执行；用 `GetTask` 轮询到终态，必要时 `CancelTask` |
| 完成回调 | 未配置 | 当前未接入 Push 通知发送链路，使用任务查询 |

异步提交的完整请求示例：

```json
{
  "jsonrpc": "2.0", "id": "submit-1", "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER", "messageId": "async-message-1",
      "contextId": "async-thread-1", "parts": [{"text": "请分析已配置的数据。"}]
    },
    "configuration": {"returnImmediately": true}
  }
}
```

早期返回示例只展示定位字段；短任务也可能返回时已完成，不能要求第一次必定为 WORKING：

```json
{"jsonrpc":"2.0","id":"submit-1","result":{"task":{"id":"22222222-2222-4222-8222-222222222222","contextId":"async-thread-1","status":{"state":"TASK_STATE_SUBMITTED"}}}}
```

查询该任务，仍使用版本头及按需认证头：

```json
{"jsonrpc":"2.0","id":"poll-1","method":"GetTask","params":{"id":"22222222-2222-4222-8222-222222222222"}}
```

`GetTask` 返回 Task 直接位于 `result`，状态可能为 SUBMITTED/WORKING；终态包括 `TASK_STATE_COMPLETED/FAILED/CANCELED/REJECTED`。完成后从 artifacts 读取结果，失败时检查 metadata；状态枚举使用完整 `TASK_STATE_*` 名称。

| 方法参数 | 必填性 | 含义 |
| --- | --- | --- |
| `GetTask.params.id`、`CancelTask.params.id` | 必填 | 服务器返回的 Task ID；不是外层 RPC `id` |
| `GetTask.params.historyLength` | 可选 | 非负整数，限制返回历史 |
| `ListTasks.params` | 可传 `{}` | `contextId` 可选过滤会话；默认不包含 artifacts，按需设 `includeArtifacts: true` |

Python `A2ACaller.send` 是可 `await` 的异步函数，但其当前请求等待本轮结果或外部输入，没有暴露 `returnImmediately` 参数；“Python 异步 I/O”与“先返回协议任务号”是两个维度。提前返回/轮询的实现依据见 [协议服务装配](../../dataagent/server/a2a/server.py)，项目内请求参考 [A2A 任务测试](../../tests/test_a2a_server.py)。

停止轮询或关闭客户端不等于取消，需发送 `CancelTask`。Task 默认存于内存，进程重启后旧任务号不可查询。普通会话可从同一 Home 下的 checkpoint 继续；等待人工的会话还需要原任务关联，本阶段的限制见下文。这不是恢复旧 Task。`serve-a2a` 不提供 `/healthz`、`/ready`、会话或产物 API。

本服务没有执行去重、自动重试或幂等承诺；超时后不要直接重发可能产生副作用的任务。

## 原生 HITL 暂停与恢复 { #a2a-hitl }

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
- 等待期间会保存 Checkpoint 并释放执行占用。同一 context 的新任务不能绕过待决中断；需对原 Task 提交结构化响应。
- `CancelTask` 在会话收尾后返回取消状态。取消待人工任务时，会话标为 `interrupted`，并保留检查点供查看。重新开始业务需使用新的 context。取消或终态 Task 不接受恢复。
- 本阶段沿用进程内 TaskStore，不保证等待期间重启后的 A2A 恢复；旧 Task 丢失后应使用新 context 重新发起。原生 Checkpoint 本身仍保留。
- 这是入站 A2A 的最小适配，不改变 REST/Web/TUI 的恢复接口，不增加审批策略配置。`A2ACaller.send` / 远端工具当前仍主要提供文本调用，结构化 HITL 响应按本节直接使用 JSON-RPC。未接入个人审批身份或多级审批。

可控模型下的完整协议验证见 [test_a2a_hitl.py](../../tests/test_a2a_hitl.py)：覆盖两个服务入口、同步/非阻塞、中断查询、人工答复、多次中断、原生工具批准/拒绝及取消。

在仓库根目录运行最小闭环，无需真实模型或语义服务：

```bash
uv run --locked pytest tests/test_a2a_hitl.py -q
```

手动联调可先在现有模型配置中启用 `data-qa` 插件，请主代理调用 `data-qa__request_human_feedback`；收到实际中断后按上述格式回复。工具审批用例由测试装配启用原生 `interrupt_on`，本次没有新增产品审批规则配置。

## 入站调用与本地子代理

A2A 请求默认进入主代理。主代理可通过 `task` 委派已启用的本地编译型子代理，例如 nl2sql；这不要求启动第二个 A2A 服务，也不需要 `a2a.remote_agents`。验证这条链路时不要设置 `dataagent.entry: "nl2sql"`，否则用户消息会直接进入 nl2sql，绕过主代理。

`TASK_STATE_COMPLETED` 表示这一轮 Agent 执行完成，不证明业务结果一定成功或正确。nl2sql 的普通业务错误可能作为含 `error` 的工具结果返回，主代理解释该错误后，外层 Task 仍可完成。验收时还要检查实际委派、子代理结果和查询产物；协议测试与插件测试的关系见 [测试](../development/testing.md#a2a-与-nl2sql-的联合验收)。

## 调用别的 agent

在 `config.yaml` 里声明远端 agent。装配时它们成为工具，工具名是 `a2a__<id>`。

```json
{
  "a2a": {
    "remote_agents": [
      {
        "id": "researcher",
        "url": "http://127.0.0.1:9999",
        "description": "Ask the research agent."
      }
    ]
  }
}
```

`url` 是服务根地址，不是 `/a2a/jsonrpc`。省略 `timeout_seconds` 时用 `dataagent.limits.timeout_seconds`（默认 180）。`auth_token` 放在声明里，并进入错误信息的脱敏列表。

同一个工具可以被多个本地线程共用。远端 `contextId` 按本地 `thread_id` 分开保存，所以两条本地会话不会写进同一段远端对话。

对方返回 Task 或直接返回 Message 都可以。Message 里的 `contextId` 同样会被记住。任务停在 `TASK_STATE_INPUT_REQUIRED` 时，工具正文以 `INPUT_REQUIRED` 开头，下一次同一本地线程的调用会带上这个 `taskId`。失败、取消或拒绝抛出 `A2ACallError`。

`A2ACallError` 提供可选的 `remote_code`、`request_id`、`task_id`、`context_id` 和 `root_run_id`。本地 `error_code` 仍是 `TOOL_ERROR`。旧服务没有诊断 metadata 时仍可调用，远端错误码、请求号和宿主轮次号为空，任务号与会话号仍从协议字段读取；成功的 `A2AResult` 也提供可选的请求号和宿主轮次号。

直接调用时，不传 `thread_id` 的 `A2ACaller.send` 共用这一条 caller 自己的 context。

```python
from dataagent.extensions.a2a import A2ACaller

async def ask():
    caller = A2ACaller("http://127.0.0.1:9999")
    first = await caller.send("hello")
    second = await caller.send("continue")
    return first.text, second.text
```
