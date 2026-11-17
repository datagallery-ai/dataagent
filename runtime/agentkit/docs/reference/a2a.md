# A2A

`dataagent serve` 和 `dataagent serve-a2a` 把当前 Home 里的 Agent 暴露成 A2A 1.0 服务。其他 agent 通过 Agent Card 发现它，再用 JSON-RPC 发消息。实现在 `a2a_server/`，图仍由 `create_agent` 装配。

| 用途 | 路径 |
| --- | --- |
| Agent Card | `GET /.well-known/agent-card.json` |
| JSON-RPC | `POST /a2a/jsonrpc` |

Card 只声明 JSON-RPC，并且 `streaming` 为 false：最终文本在图结束后提供；可等待最终结果，也可先返回 Task 再查询，见下文。接口 URL 按请求的 `Host` 填写。绑定 `0.0.0.0`、`::` 或 `::0` 时，未带 Host 的默认值是 `127.0.0.1`。其他 IPv6 地址写成带方括号的 URL。

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
| `params.message` | `SendMessage` 必填 | A2A Message 对象 |
| `params.message.messageId` | 必填 | 非空，新消息用新编号；不承诺执行去重 |
| `params.message.role` | 必填 | 用户输入使用 `"ROLE_USER"` |
| `params.message.parts` | 必填 | 非空数组，本服务需要至少一段非空 `text`，多段文本合并为输入 |
| `params.message.contextId` | 可选 | 省略生成 UUID；续聊复用返回值，路径安全 ID 最长 128 字符 |
| `params.message.taskId` | 可选 | 新任务省略；只允许引用已有任务，不能用它自建任务号，终态任务不能续接 |
| `params.configuration` | 可选 | 省略采用协议 SDK 默认值 |
| `params.configuration.returnImmediately` | 可选 | 默认 `false`：等结果；`true`：先返回任务再查询 |
| `params.configuration.historyLength` | 可选 | 非负整数，限制返回的任务消息历史；不清除服务端会话上下文 |

`messageId/role/parts` 的协议必填由已安装 A2A SDK 校验；文本非空和 context 安全性由本服务执行器校验。不要把主模型、插件、数据库或工作区当成请求参数临时覆盖。其他协议字段不等于宿主支持相应能力。

`contextId` 必须是最长 128 的路径安全 ID（字母、数字、下划线和连字符）。它同时是 LangGraph 的 `thread_id` 和会话目录名。省略时由协议生成 UUID。下一条消息从会话索引里记下的检查点继续，和 `/api/v1/runs` 是同一条。报错或取消也会把图已经到达的检查点写进这个索引。同一 context 上的调用串行执行。

只读取文本 part。空文本或非法 `contextId` 使任务进入 `TASK_STATE_FAILED`，不会调用模型。成功时最终回答在 `result.task.artifacts`，状态是 `TASK_STATE_COMPLETED`。

这条会话记在进程用户（`default` 或 `--user`）的会话索引里。登记发生在读取 checkpoint 之前。`thread_id` 全局唯一：该 id 已经属于另一个 profile 时，任务失败，模型不会被调用。

默认匿名访问，所有访问者属于一个共享团队：能列举、读取、续接和取消同一实例内的任务。不同浏览器或客户端使用相同 `contextId` 就会进入同一段对话。日志里的 `user_id` 是进程 profile，不是访问者身份。

A2A、REST API、TUI 和 Web 共用 Home 下的会话、checkpoint、trace 与产物存储，各入口只适配协议和展示内容。A2A 协议 SDK 的内存 TaskStore 用于任务查询与取消，不另建一套持久业务记录；`taskId` 通过诊断 metadata 关联同一套内部轮次号。

## 鉴权

默认不启用认证。设置 `DATAAGENT_A2A_AUTH_TOKEN` 后，两个服务入口都校验 `Authorization: Bearer <token>`。令牌取值优先级：`serve-a2a --auth-token` → 非空进程变量 → 所选环境文件（`--env-file` 或 Home `.env`）。`serve` 没有 `--auth-token` 参数，使用有效环境中的令牌；改变后重启。不要把它与模型 `LLM_API_KEY`、出站 `a2a.remote_agents[].auth_token` 混用。

| 范围 | 行为 |
| --- | --- |
| `POST /a2a/jsonrpc` 全部方法 | 启用认证时都要 Bearer，包括查询、取消和任务列表 |
| Agent Card | 仍可匿名读取；能读 Card 不代表能调用任务 |
| `serve` 的 REST/AG-UI/健康/产物接口 | 此令牌不保护这些接口 |
| 调用方身份 | 同一个共享令牌，不提供个人登录、任务所有权隔离或租户隔离 |

例如先在**服务端**环境设置令牌再启动服务：

```bash
export DATAAGENT_A2A_AUTH_TOKEN='replace-with-a-long-random-token'
uv run --project runtime/agentkit dataagent serve
```

调用终端单独设置相同值，再发送带认证头的请求：

```bash
export DATAAGENT_A2A_AUTH_TOKEN='replace-with-a-long-random-token'
curl -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -H "Authorization: Bearer $DATAAGENT_A2A_AUTH_TOKEN" \
  -d '{"jsonrpc":"2.0","id":"list-1","method":"ListTasks","params":{}}'
```

缺少或错误令牌返回 HTTP 401，正文为 `{"success":false,"error":{"code":"UNAUTHORIZED","message":"Invalid or missing Bearer token."}}`，不进入模型执行。未配置令牌时可省略 Authorization。后端读取 `.env` 不会替其他终端设置变量。实现见 [Bearer 校验](../../a2a_server/server.py)。

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

`dataagent.limits.timeout_seconds` 在入站 A2A 中只限制 Agent 的 `ainvoke`；同 context 排队、会话准备和 Checkpoint 保存发生在这层时限之外。客户端不能用总耗时是否超过该值判断超时机制是否有效。超时后先查询已经取得的 Task ID，不要把客户端断开或停止轮询当作 `CancelTask`。

## 查询和取消任务

所有方法仍使用 `POST /a2a/jsonrpc`，携带相同版本头。`GetTask`、`CancelTask` 的任务号在 `params.id`，与外层 RPC `id` 不同：

```json
{"jsonrpc": "2.0", "id": "query-1", "method": "GetTask", "params": {"id": "<task-id>"}}
```

`GetTask` 和 `CancelTask` 直接在 `result` 返回 Task；`SendMessage` 在 `result.task` 返回。`ListTasks` 在 `result.tasks` 返回共享任务列表，可使用 `params.contextId` 过滤。

### 等待结果与异步任务

两种方式都通过非流式 `SendMessage`，由 `configuration.returnImmediately` 选择：

| 方式 | 配置 | 返回与后续操作 |
| --- | --- | --- |
| 同步等待结果 | 省略或 `false` | HTTP 请求等待本轮结束，最终 Task 在 `result.task`；仍须检查失败/取消状态 |
| 异步任务 | `true` | 先取得 `result.task.id`，后台继续执行；用 `GetTask` 轮询到终态，必要时 `CancelTask` |
| 流式结果 | 不支持 | Card `streaming=false`，不提供 A2A token/event 流 |
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

Python `A2ACaller.send` 是可 `await` 的异步函数，但其当前请求等待最终结果，没有暴露 `returnImmediately` 参数；“Python 异步 I/O”与“先返回协议任务号”是两个维度。提前返回/轮询的实现依据见 [协议服务装配](../../a2a_server/server.py)，项目内请求参考 [A2A 任务测试](../../tests/test_a2a_server.py)。

停止轮询或关闭客户端不等于取消，需发送 `CancelTask`。Task 默认存于内存，进程重启后旧任务号不可查询，但同一 Home 下的会话和 checkpoint 可继续；这不是恢复旧 Task。`serve-a2a` 不提供 `/healthz`、`/ready`、会话或产物 API。

本服务没有执行去重、自动重试或幂等承诺；超时后不要直接重发可能产生副作用的任务。

## 入站调用与本地子代理

A2A 请求默认进入主代理。主代理可通过 `task` 委派已启用的本地编译型子代理，例如 nl2sql；这不要求启动第二个 A2A 服务，也不需要 `a2a.remote_agents`。验证这条链路时不要设置 `dataagent.entry: "nl2sql"`，否则用户消息会直接进入 nl2sql，绕过主代理。

`TASK_STATE_COMPLETED` 表示这一轮 Agent 执行完成，不证明业务结果一定成功或正确。nl2sql 的普通业务错误可能作为含 `error` 的工具结果返回，主代理解释该错误后，外层 Task 仍可完成。验收时还要检查实际委派、子代理结果和查询产物；协议测试与插件测试的关系见 [测试](../development/testing.md#a2a-与-nl2sql-的联合验收)。

## 调用别的 agent

在 `config.json` 里声明远端 agent。装配时它们成为工具，工具名是 `a2a__<id>`。

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
from a2a_server import A2ACaller

async def ask():
    caller = A2ACaller("http://127.0.0.1:9999")
    first = await caller.send("hello")
    second = await caller.send("continue")
    return first.text, second.text
```
