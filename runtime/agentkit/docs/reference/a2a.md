# A2A

`dataagent serve` 和 `dataagent serve-a2a` 把当前 Home 里的 Agent 暴露成 A2A 1.0 服务。其他 agent 通过 Agent Card 发现它，再用 JSON-RPC 发消息。实现在 `a2a_server/`，图仍由 `create_agent` 装配。

| 用途 | 路径 |
| --- | --- |
| Agent Card | `GET /.well-known/agent-card.json` |
| JSON-RPC | `POST /a2a/jsonrpc` |

Card 只声明 JSON-RPC，并且 `streaming` 为 false：执行器要等整张图结束才返回最终文本。接口 URL 按请求的 `Host` 填写。绑定 `0.0.0.0`、`::` 或 `::0` 时，未带 Host 的默认值是 `127.0.0.1`。其他 IPv6 地址写成带方括号的 URL。

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

`A2A-Version: 1.0` 必带。不带这个头时，协议把请求看成 0.3，`SendMessage` 会被拒绝。

`contextId` 必须是最长 128 的路径安全 ID（字母、数字、下划线和连字符）。它同时是 LangGraph 的 `thread_id` 和会话目录名。省略时由协议生成 UUID。下一条消息从会话索引里记下的检查点继续，和 `/api/v1/runs` 是同一条。报错或取消也会把图已经到达的检查点写进这个索引。同一 context 上的调用串行执行。

只读取文本 part。空文本或非法 `contextId` 使任务进入 `TASK_STATE_FAILED`，不会调用模型。成功时最终回答在 `result.task.artifacts`，状态是 `TASK_STATE_COMPLETED`。

这条会话记在进程用户（`default` 或 `--user`）的会话索引里。登记发生在读取 checkpoint 之前。`thread_id` 全局唯一：该 id 已经属于另一个 profile 时，任务失败，模型不会被调用。

默认匿名访问，所有访问者属于一个共享团队：能列举、读取、续接和取消同一实例内的任务。不同浏览器或客户端使用相同 `contextId` 就会进入同一段对话。日志里的 `user_id` 是进程 profile，不是访问者身份。

A2A、REST API、TUI 和 Web 共用 Home 下的会话、checkpoint、trace 与产物存储，各入口只适配协议和展示内容。SDK 的内存 TaskStore 用于 A2A 任务查询与取消，不另建一套持久业务记录；`taskId` 通过诊断 metadata 关联同一套内部轮次号。

令牌是可选的。设置环境变量 `DATAAGENT_A2A_AUTH_TOKEN`（进程环境或 `.env`）后，`dataagent serve` 和 `dataagent serve-a2a` 都要求 JSON-RPC 带 `Authorization: Bearer <token>`；Agent Card 仍可匿名读取。`serve-a2a --auth-token` 优先于环境变量。缺少或错误时返回 401，正文是错误信封，错误码 `UNAUTHORIZED`。共享令牌不区分调用方。

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

## 查询和取消任务

所有方法仍使用 `POST /a2a/jsonrpc`，携带相同版本头。`GetTask`、`CancelTask` 的任务号在 `params.id`，与外层 RPC `id` 不同：

```json
{"jsonrpc": "2.0", "id": "query-1", "method": "GetTask", "params": {"id": "<task-id>"}}
```

`GetTask` 和 `CancelTask` 直接在 `result` 返回 Task；`SendMessage` 在 `result.task` 返回。`ListTasks` 在 `result.tasks` 返回共享任务列表，可使用 `params.contextId` 过滤。

默认 `SendMessage` 等待任务结束。要提前取得任务号，在 `params.configuration` 中设置 `returnImmediately: true`，再使用 `GetTask` 查询或 `CancelTask` 取消；不要求流式能力。Task 默认存于内存，进程重启后旧任务号不可查询，但同一 Home 下的会话和 checkpoint 可继续。`serve-a2a` 不提供 `/healthz`、`/ready`、会话或产物 API。

本服务没有执行去重、自动重试或幂等承诺；超时后不要直接重发可能产生副作用的任务。

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
