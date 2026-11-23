# HTTP 接口

这篇对照 `dataagent/server/` 的实现，给改控制面的人。对外产品站的 `docs/*/reference/rest-api.md` 给集成方看同一份路径和请求约束。改路径、状态码或请求体时两处一起改。

除 `/healthz`、`/ready` 和 A2A 外，所有路由都在 `/api/v1` 下。所有 JSON 响应（成功和失败）都用信封 `{"success": true, "data": ...}` / `{"success": false, "error": {...}}`；`POST /api/v1/runs` 成功时是 SSE，取消成功时是空的 204。下文会话和产物接口的成功示例只写 `data` 的内容。

默认监听 `127.0.0.1:8790`，来自 `config.yaml` 的 `server.host` 和 `server.port`。`/healthz` 返回实际使用的 DataAgent Home，供 TUI 底栏显示；不返回输入目录或 `stateDir`。

## `GET /healthz`

始终返回 200 信封。

```json
{
  "success": true,
  "data": {
    "status": "ok",
    "conversation": {
      "ready": true,
      "protocol": "dataagent",
      "instanceId": "<uuid-or-launch-id>",
      "model": "<provider model name>",
      "plugins": ["common"],
      "home": "/absolute/path/to/.dataagent",
      "entry": null,
      "user": "default",
      "timeoutSeconds": 180
    }
  }
}
```

`conversation.ready` 是唯一的就绪标志。引擎未就绪时 `conversation` 只有 `ready: false`。`instanceId` 是本进程的启动标识，TUI 用它确认连上的是自己拉起的后端。`timeoutSeconds` 是 `dataagent.limits.timeout_seconds`。`model` 是解析后的服务商模型名。`plugins` 是启用 ID。`entry` 是 `dataagent.entry`，未设置时为 `null`；这里不检查这个名字是否对应一个编译型子代理。

## `GET /ready`

引擎就绪时返回 200，`data.status` 是 `ready`，`data.conversation` 同 `/healthz`。未就绪时返回 503，错误码 `CONVERSATION_UNAVAILABLE`。

## `/api/v1`

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/v1/me` | 当前用户。引擎未就绪时 id 为 `default` |

未知路径返回 404 信封，错误码 `RESOURCE_NOT_FOUND`。路径存在但方法不对时返回 405，错误码 `METHOD_NOT_ALLOWED`。`/healthz` 和 `/ready` 也接受 `HEAD`，其状态码与 `GET` 一致、响应体为空；OpenAPI 只列出 `GET` 操作。对话路径（`/api/v1/runs`、`/sessions*`、`/runs/*`、`/a2a/jsonrpc`）在引擎未就绪时返回 503；Agent Card 不受影响。

## `POST /api/v1/runs`

请求体是一条 AG-UI `RunAgentInput`，且 `extra` 字段被拒绝。

约束：

- `threadId`、`runId` 是最长 128 的安全 ID
- `messages` 恰好一条，带 `id`，`role` 为 `user`，`content` 为非空字符串
- 不接受子代理消息
- `state` 只能是 `null` 或 `{}`
- `tools`、`context`、`forwardedProps` 必须出现，且为空（`[]`、`[]`、`{}`）
- `resume` 和 `parentRunId` 不能使用

成功响应是 `text/event-stream`，带 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。事件见 [事件](events.md)。

客户端断开时，服务取消上游流，并在后台把未完成的会话标成中断。

## `POST /api/v1/runs/{runId}/cancel`

停止正在执行的一轮。`runId` 是这一轮 `POST /api/v1/runs` 请求体里的 `runId`。

- 成功返回空的 204。
- 这一轮不存在或已经结束时，返回 404 信封，错误码 `RESOURCE_NOT_FOUND`。
- 引擎未就绪时返回 503 信封，错误码 `CONVERSATION_UNAVAILABLE`。
- 取消后，对应的事件流以 `RUN_ERROR` 结束，`code` 为 `CANCELLED`。
- `runId` 只在运行期间登记。正在运行的 `runId` 再次用于 `POST /api/v1/runs`（即使换了 `threadId`）返回 409。
- A2A 的运行使用内部 ID，不能通过这条路由取消。
- 会话状态记为 `interrupted`，并写入图已经到达的 Checkpoint。下一轮在同一 `threadId` 上从这个 Checkpoint 继续。见 [事件](events.md)。

## A2A 路由

`dataagent serve` 在同一个进程上挂 A2A 1.0 的两条路由。完整约定见 [A2A](a2a.md)。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/.well-known/agent-card.json` | Agent Card。只声明 JSON-RPC，`streaming` 为 false |
| `POST` | `/a2a/jsonrpc` | JSON-RPC 入口，例如 `SendMessage`。请求必须带 `A2A-Version: 1.0` 头 |

- `contextId` 就是会话的 `threadId`，和 `/api/v1/runs` 共用会话索引和 Checkpoint。
- 设置环境变量 `DATAAGENT_A2A_AUTH_TOKEN` 后，`/a2a/jsonrpc` 需要 `Authorization: Bearer <token>`，否则返回 401 信封，错误码 `UNAUTHORIZED`。Agent Card 和其他路由不受影响。未设置时不校验。
- 引擎未就绪时，`/a2a/jsonrpc` 返回 503 信封，错误码 `CONVERSATION_UNAVAILABLE`。Agent Card 是静态信息，照常返回，包括缺少模型环境变量、尚未创建 runtime 的降级启动。Card 中的 JSON-RPC 地址使用请求主机和协议。
- JSON-RPC 的错误按 JSON-RPC 规范返回，不用本文的错误信封。

## `GET /api/v1/sessions`

查询参数 `limit` 范围 1 到 100，默认 50。只返回当前 `--user` 的会话，按 `updatedAt`、`threadId` 降序。

```json
{
  "sessions": [
    {
      "threadId": "thread-1",
      "title": "first 100 chars of the user text",
      "createdAt": "2026-10-15T00:00:00+00:00",
      "updatedAt": "2026-10-15T00:00:00+00:00",
      "status": "completed",
      "hasCheckpoint": true
    }
  ]
}
```

`status` 为 `running`、`completed`、`error` 或 `interrupted`。进程启动时，索引里仍为 `running` 的行会改成 `interrupted`，旧版本写入的 `complete` 会改成 `completed`。

## `GET /api/v1/sessions/{threadId}`

返回列表接口中的字段，再加上：

| 字段 | 含义 |
| --- | --- |
| `messages` | 下一轮 `POST /api/v1/runs` 接着用的消息。行上有 `last_checkpoint_id` 时读该 Checkpoint，否则读这个线程的最新图状态 |
| `notice` | 中断或失败且有 Checkpoint 时说明已恢复到保存点；中断或失败但没有 Checkpoint 时说明继续当前对话；完成或仍在运行时为 null |

运行中也可以读。绑定缺失或打不开时返回 409。当前用户没有该会话时返回 404。网页和 TUI 都读这份文档。

## `GET /api/v1/sessions/{threadId}/outputs`

```json
{"files": [{"path": "notes.txt", "size": 12, "modifiedAt": "2026-10-15T00:00:00+00:00"}]}
```

按修改时间新到旧，路径作为次序。排除 `artifacts/conversation_history` 和 `artifacts/large_tool_results`。不跟随符号链接。目录本身是符号链接时返回 409。

会话不存在（包括还没跑过一轮的新对话）或属于其他用户时返回 404；客户端把 404 当作“暂无产物”。

## `GET /api/v1/sessions/{threadId}/outputs/preview`

查询参数 `path` 是相对产物目录的路径。

成功：

```json
{"type": "file", "path": "notes.txt", "content": "...", "truncated": false}
```

`path` 与列表接口相同，相对产物目录。只预览 UTF-8 文本，最多 64 KiB；超出时 `truncated` 为 `true`，`content` 只含前 64 KiB。二进制或非 UTF-8 返回 415。路径含 `..`、绝对路径或空字节返回 400。

## 错误

所有 HTTP 错误都用同一个信封。`dataagent/server/envelopes.py` 的 `register_error_handlers` 把 `HTTPException` 和请求校验错误转成这个格式：

```json
{"success": false, "error": {"code": "CONFLICT", "message": "A run is already active for this session"}}
```

校验失败返回 422，`message` 是第一条校验错误，格式为 `<字段位置>: <原因>`。

| HTTP | `code` | 典型原因 |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | 非法会话 ID 或产物路径 |
| 404 | `RESOURCE_NOT_FOUND` | 路由、会话或文件不存在，或会话属于其他用户 |
| 405 | `METHOD_NOT_ALLOWED` | 路径存在，方法不对 |
| 409 | `CONFLICT` | 同一会话已有一轮在跑、用户不匹配、绑定打不开、重复 run、输出目录不是普通目录 |
| 415 | `UNSUPPORTED_FILE_TYPE` | 预览不是 UTF-8 文本 |
| 422 | `BAD_REQUEST` | 请求体或查询参数校验失败 |
| 500 | `INTERNAL_ERROR` | 扩展重载失败；列目录或读文件时的操作系统错误 |
| 503 | `CONVERSATION_UNAVAILABLE` | 对话引擎未就绪 |

A2A 的 `/a2a/jsonrpc` 不用这个信封，它按 JSON-RPC 规范返回错误。

流内的运行失败不是这些 HTTP 码。连接已经是 200 的事件流，终止事件见 [事件](events.md)。
