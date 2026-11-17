# REST API 参考

这篇给要调用控制面的集成方，不写服务内部模块。控制面默认地址是 `http://127.0.0.1:8790`，来自 Home `config.json` 的 `server.host` 和 `server.port`。REST 没有访问令牌；A2A JSON-RPC 有可选共享令牌，见下文。网页通过 Next 把 `/api/v1/*` 转到这个地址。

## 信封

REST 的 JSON 响应都用信封，成功和失败都一样。`POST /api/v1/runs` 成功时返回 SSE 事件流，REST 取消成功时返回空的 204。A2A 使用自己的 Agent Card 和 JSON-RPC 格式，见下文，不套 REST 成功信封。

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

除 `/healthz`、`/ready` 和 A2A（`/.well-known/agent-card.json`、`/a2a/jsonrpc`，路径由协议固定）外，所有路由都在 `/api/v1` 下。

未知路径返回 404，错误码 `RESOURCE_NOT_FOUND`；方法不对返回 405，错误码 `METHOD_NOT_ALLOWED`。对话路径（含取消）在引擎未就绪时返回 503，错误码 `CONVERSATION_UNAVAILABLE`；Agent Card 不受影响。

## 健康与身份

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET, HEAD | `/healthz` | 始终 200。引擎就绪时 `data.conversation` 含 `ready`、`protocol`、`instanceId`、`model`、`plugins`、`home`、`entry`、`user`、`timeoutSeconds`；`ready` 是唯一的就绪标志。未就绪时 `conversation` 只有 `ready: false`。 |
| GET, HEAD | `/ready` | 引擎就绪时 200，`data.status` 是 `ready`。否则 503。 |
| GET | `/api/v1/me` | 当前 `--user`。引擎未就绪时 id 是 `default`。没有登录用户。 |

`/healthz` 不返回输入目录、状态目录或配置正文。

## 会话与运行

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/v1/sessions?limit=` | 当前 user 的会话。网页和 TUI 都用这条。`limit` 为 1 到 100，默认 50。 |
| GET | `/api/v1/sessions/{threadId}` | 列表字段加上 `messages`、`notice`。有 Checkpoint 时读它，否则读该线程最新图状态。绑定打不开时 409。其他 user 的会话是 404。运行中也可以读。 |
| GET | `/api/v1/sessions/{threadId}/outputs` | 产物文件。跳过符号链接，以及 `artifacts/conversation_history` 和 `artifacts/large_tool_results`。会话不存在时 404。 |
| GET | `/api/v1/sessions/{threadId}/outputs/preview?path=` | UTF-8 文本，最多 64 KiB，超出时 `truncated` 为 `true`。非文本是 415。 |
| POST | `/api/v1/runs/{runId}/cancel` | 取消正在执行的一轮，`runId` 来自 `POST /api/v1/runs` 的请求体。成功是空的 204。这一轮不存在或已结束时 404。取消后事件流以 `RUN_ERROR`（`code` 为 `CANCELLED`）结束；会话记为 `interrupted`，下一轮从已保存的 Checkpoint 继续。 |

没有这些路径：登录、`/api/v1/capabilities`、`/api/copilotkit`、`/api/copilotkit/info`、数据源、知识库、工作区配置、会话改名、删除、分支或 trace DAG。调用它们会得到 404 信封。

## 对话

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/v1/runs` | 一轮对话。请求体是一条 AG-UI 输入，多出来的字段会被拒绝。成功响应是 `text/event-stream`。 |

`POST /api/v1/runs` 的约束：

- `threadId` 和 `runId` 是最长 128 的安全 ID
- `messages` 恰好一条，带 `id`，`role` 为 `user`，`content` 为非空字符串
- `state` 只能是 `null` 或 `{}`
- `tools`、`context`、`forwardedProps` 必须出现，且为空（`[]`、`[]`、`{}`）
- 不接受 `resume` 和 `parentRunId`

同一会话同时只跑一轮。校验失败是 422，错误码 `BAD_REQUEST`。业务拒绝多用 409，错误码 `CONFLICT`。两者都用错误信封。其他错误码：400 `BAD_REQUEST`，404 `RESOURCE_NOT_FOUND`，415 `UNSUPPORTED_FILE_TYPE`，5xx `INTERNAL_ERROR`。流已经以 200 开始之后，运行失败写在事件里，而不是改成 HTTP 错误码。

## A2A

同一个进程还按 A2A 1.0 协议提供两条路由，供其他 Agent 调用。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/.well-known/agent-card.json` | Agent Card。只声明 JSON-RPC，不支持流式。 |
| POST | `/a2a/jsonrpc` | JSON-RPC 入口，例如 `SendMessage`。请求必须带 `A2A-Version: 1.0` 头。 |

- `contextId` 就是会话的 `threadId`，和 `/api/v1/runs` 共用会话与 Checkpoint。它必须是最长 128 的安全 ID，省略时自动生成。
- 只读取文本 part。成功时最终回答在 `result.task.artifacts`，状态是 `TASK_STATE_COMPLETED`。
- 默认匿名、单团队共享访问，可查看、续接和取消同实例的任务。设置可选的 `DATAAGENT_A2A_AUTH_TOKEN` 后，`/a2a/jsonrpc` 需要 `Authorization: Bearer <token>`，否则返回 401，错误码 `UNAUTHORIZED`。Agent Card 仍可匿名读取。
- 每次 RPC 响应带服务端生成的 `X-Request-ID`。Task 的 `metadata.dataagent` 包含 `schemaVersion: 1`、创建请求的 `requestId`、输入 `messageId` 和已创建时的 `rootRunId`。查询、取消不覆盖创建请求的关联信息。
- 执行失败仍是 HTTP 200 加失败 Task，`metadata.dataagent.error` 包含稳定的 `code`、`stage` 和脱敏 `message`。错误码包括 `BAD_REQUEST`、`CONFLICT`、`FORBIDDEN`、`TIMEOUT`、`MODEL_ERROR`、`TOOL_ERROR`、`CONFIG_ERROR`、`LIMIT_EXCEEDED`、`LOOP_DETECTED`、`INTERNAL_ERROR` 和 `CANCELLED`。同一会话被 Web/TUI 占用时，A2A 返回失败 Task 加 `CONFLICT`，不是 HTTP 409。
- `GetTask`、`CancelTask` 在 `params.id` 传任务号，直接在 `result` 返回 Task；`ListTasks` 在 `result.tasks` 返回共享列表。`SendMessage` 设置 `params.configuration.returnImmediately: true` 可提前取得任务号，再查询或取消。任务默认保存在内存中，重启后旧 task 不可查询，会话和 checkpoint 可续接；没有执行去重或自动重试承诺。
- JSON-RPC 的错误按 JSON-RPC 规范返回，不用上面的错误信封。
