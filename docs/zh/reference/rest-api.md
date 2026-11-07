# REST API 参考

这篇给要调用控制面的集成方，不写服务内部模块。控制面默认地址是 `http://127.0.0.1:8790`，来自 Home `config.json` 的 `server.host` 和 `server.port`。没有访问令牌。网页通过 Next 把相同路径转到这个地址。

## 信封

`/healthz`、`/ready` 和 `/api/v1/*` 使用信封。

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

`/sessions` 和产物接口返回自己的 JSON，不用这层信封。`POST /api/v1/runs/{runId}/cancel` 成功时是空的 204。

未知路径返回 404，错误码 `RESOURCE_NOT_FOUND`。对话路径在引擎未就绪时返回 503，错误码 `CONVERSATION_UNAVAILABLE`。

## 健康与身份

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/healthz` | 始终 200。引擎就绪时 `data.conversation` 含 `ready`、`home`、`model`、`plugins`、`entry`、`user`、`timeoutSeconds`。未就绪时 `conversation` 只有 `ready: false`。 |
| GET | `/ready` | 引擎就绪时 200，`data.status` 和 `data.control_plane` 都是 `ready`。否则 503。 |
| GET | `/api/v1/me` | 当前 `--user`。引擎未就绪时 id 是 `default`。没有登录用户。 |

`/healthz` 不返回输入目录、状态目录或配置正文。

## 会话与运行

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/v1/sessions?limit=` | 当前 user 的会话，包在信封里。`limit` 为 1 到 100，默认 50。 |
| GET | `/api/v1/sessions/{threadId}/conversation` | 与 `GET /sessions/{threadId}` 同一份文档（AG-UI `messages`、`status`、`notice`），包在 `{success, data}` 里。未知会话 404。运行中也可以读。 |
| GET | `/sessions?limit=` | 同一份索引，JSON 形如 `{"sessions":[...]}`。TUI 用这条。 |
| GET | `/sessions/{threadId}` | 列表字段加上 `messages`、`notice`。有 Checkpoint 时读它，否则读该线程最新图状态。绑定打不开时 409。其他 user 的会话是 404。运行中也可以读。 |
| GET | `/sessions/{threadId}/outputs` | 产物文件。跳过符号链接，以及 `artifacts/conversation_history` 和 `artifacts/large_tool_results`。 |
| GET | `/sessions/{threadId}/outputs/preview?path=` | UTF-8 文本，最多 64 KiB。非文本是 415。 |
| POST | `/api/v1/runs/{runId}/cancel` | 取消正在执行的一轮。没有这一轮时 404。 |

没有这些路径：登录、`/api/v1/capabilities`、`/api/copilotkit`、`/api/copilotkit/info`、数据源、知识库、工作区配置、会话改名、删除、分支或 trace DAG。调用它们会得到 404 信封。

## 对话

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/dataagent/stream` | 一轮对话。请求体是一条 AG-UI 输入，多出来的字段会被拒绝。成功响应是 `text/event-stream`。 |

`POST /dataagent/stream` 的约束：

- `threadId` 和 `runId` 是最长 128 的安全 ID
- `messages` 恰好一条，`role` 为 `user`，`content` 为非空字符串
- `state` 只能是 `null` 或 `{}`
- `tools`、`context`、`forwardedProps` 必须为空
- 不接受 `resume` 和 `parentRunId`

同一会话同时只跑一轮。校验失败多为 422。业务拒绝多用 409，正文是 `{"detail":"..."}`。流已经以 200 开始之后，运行失败写在事件里，而不是改成 HTTP 错误码。

同一个进程还提供 `GET /.well-known/agent-card.json` 和 `POST /a2a/jsonrpc`，不另加 Bearer。
