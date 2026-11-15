# REST API 参考

这篇给要调用控制面的集成方，不写服务内部模块。控制面默认地址是 `http://127.0.0.1:8790`，来自 Home `config.json` 的 `server.host` 和 `server.port`。没有访问令牌。网页通过 Next 把相同路径转到这个地址。

## 信封

`/healthz`、`/ready` 和 `/api/v1/*` 的成功和失败都用信封。其他 HTTP 路由失败时也用同一个错误信封；`/sessions` 和产物接口成功时直接返回文档本身。

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

`/sessions`、产物接口和 `POST /runs/{runId}/cancel` 不在 `/api/v1` 下。取消成功时是空的 204。

未知路径返回 404，错误码 `RESOURCE_NOT_FOUND`。对话路径在引擎未就绪时返回 503，错误码 `CONVERSATION_UNAVAILABLE`。

## 健康与身份

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/healthz` | 始终 200。引擎就绪时 `data.conversation` 含 `ready`、`protocol`、`instanceId`、`model`、`plugins`、`home`、`entry`、`user`、`timeoutSeconds`；`ready` 是唯一的就绪标志。未就绪时 `conversation` 只有 `ready: false`。 |
| GET | `/ready` | 引擎就绪时 200，`data.status` 是 `ready`。否则 503。 |
| GET | `/api/v1/me` | 当前 `--user`。引擎未就绪时 id 是 `default`。没有登录用户。 |

`/healthz` 不返回输入目录、状态目录或配置正文。

## 会话与运行

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/sessions?limit=` | 当前 user 的会话。网页和 TUI 都用这条。`limit` 为 1 到 100，默认 50。 |
| GET | `/sessions/{threadId}` | 列表字段加上 `messages`、`notice`。有 Checkpoint 时读它，否则读该线程最新图状态。绑定打不开时 409。其他 user 的会话是 404。运行中也可以读。 |
| GET | `/sessions/{threadId}/outputs` | 产物文件。跳过符号链接，以及 `artifacts/conversation_history` 和 `artifacts/large_tool_results`。 |
| GET | `/sessions/{threadId}/outputs/preview?path=` | UTF-8 文本，最多 64 KiB。非文本是 415。 |
| POST | `/runs/{runId}/cancel` | 取消正在执行的一轮，`runId` 来自 `POST /dataagent/stream` 的请求体。成功是空的 204。这一轮不存在或已结束时 404。取消后事件流以 `RUN_ERROR`（`code` 为 `CANCELLED`）结束；会话记为 `interrupted`，下一轮从已保存的 Checkpoint 继续。 |

没有这些路径：登录、`/api/v1/capabilities`、`/api/copilotkit`、`/api/copilotkit/info`、数据源、知识库、工作区配置、会话改名、删除、分支或 trace DAG。调用它们会得到 404 信封。

## 对话

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/dataagent/stream` | 一轮对话。请求体是一条 AG-UI 输入，多出来的字段会被拒绝。成功响应是 `text/event-stream`。 |

`POST /dataagent/stream` 的约束：

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

- `contextId` 就是会话的 `threadId`，和 `/dataagent/stream` 共用会话与 Checkpoint。它必须是最长 128 的安全 ID，省略时自动生成。
- 只读取文本 part。成功时最终回答在 `result.task.artifacts`，状态是 `TASK_STATE_COMPLETED`。
- 设置环境变量 `DATAAGENT_A2A_AUTH_TOKEN` 后，`/a2a/jsonrpc` 需要 `Authorization: Bearer <token>`，否则返回 401，错误码 `UNAUTHORIZED`。Agent Card 仍可匿名读取。对外暴露 A2A 时一定要设置。
- JSON-RPC 的错误按 JSON-RPC 规范返回，不用上面的错误信封。
