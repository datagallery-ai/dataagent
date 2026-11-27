# REST API 参考

通过 `dataagent serve` 启动控制面。默认地址为 `http://127.0.0.1:8790`，由所选 YAML 的 `server.host` 和 `server.port` 配置。Web 工作台将 `/api/v1/*` 转发到这个服务。REST 没有内置的访问令牌校验。需要对回环地址之外提供服务时，请先阅读[安全说明](../concepts/security.md)。

<a name="envelope"></a>

## 信封

JSON 成功响应和已处理的 HTTP 错误采用以下信封：

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

`POST /api/v1/runs` 成功时返回 HTTP 200 和 SSE 事件流。取消成功时返回空的 HTTP 204。健康接口的 `HEAD` 只返回状态和响应头。下文的示例展示的是 `data` 内容。

<a name="health-and-identity"></a>

## 健康与身份

| 方法 | 路径 | 响应 |
| --- | --- | --- |
| GET, HEAD | `/healthz` | 200，包含 `status: "ok"` 和 `conversation`。 |
| GET, HEAD | `/ready` | 200，包含 `status: "ready"` 和与 `/healthz` 相同的 `conversation` 字段。引擎不可用时返回 503。 |
| GET | `/api/v1/me` | `user.id` 和 `user.displayName` 是服务端本地用户（`--user`）。没有运行时配置时两者为 `default`。`workspace` 固定为 `{"id":"local","name":"Local"}`。 |
| GET | `/api/v1/capabilities` | `interaction.resume`、`conversation.memory` 和 `artifact.list` 为 true。`artifact.export` 为 false。 |

引擎不可用时，`/healthz` 的 `conversation` 只有 `{"ready": false}`。就绪时返回的数据形如：

```json
{
  "status": "ok",
  "conversation": {
    "ready": true,
    "protocol": "dataagent",
    "instanceId": "server-1",
    "model": "example-model",
    "plugins": ["common"],
    "home": "/absolute/path/to/.dataagent",
    "entry": null,
    "user": "default",
    "timeoutSeconds": 180
  }
}
```

用 `conversation.ready` 判断就绪状态。`instanceId` 标识进程，`timeoutSeconds` 来自 `dataagent.limits.timeout_seconds`。`entry` 返回配置值，接口不会判断该值是否能解析为编译型子代理。响应不包含输入目录、状态目录或配置正文。

<a name="sessions-and-runs"></a>

## 会话与运行

<a name="list-sessions"></a>

### 列出会话

`GET /api/v1/sessions?limit=50` 返回当前服务端用户的会话，按 `updatedAt`、`threadId` 降序排列。`limit` 为 1 至 100 的整数，默认 50。

```json
{
  "sessions": [
    {
      "threadId": "thread-1",
      "title": "总结最新报告",
      "createdAt": "2026-10-15T00:00:00+00:00",
      "updatedAt": "2026-10-15T00:00:00+00:00",
      "status": "completed",
      "hasCheckpoint": true
    }
  ]
}
```

会话状态为 `running`、`waiting_for_input`、`completed`、`error` 或 `interrupted`。等待通过 A2A 回答的会话在 REST 中显示为 `interrupted`。启动时仍记录为 `running` 的会话会转为 `interrupted`。

<a name="session-detail"></a>

### 读取会话

`GET /api/v1/sessions/{threadId}` 在列表字段上增加 `messages`、`pendingInteractions` 和 `notice`。消息取已保存的检查点，没有检查点时读取该线程的最新图状态。会话运行中也可读取。`pendingInteractions` 只包含能通过 REST 回答的交互，具体结构和答复方式见[人工反馈](events.md#hitl)。`notice` 是提示文本或 `null`。会话失败或中断时，提示会说明是否恢复了检查点。

会话不存在或属于其他用户时返回 404。当前用户的会话绑定无法打开时返回 409。

<a name="cancel-run"></a>

### 取消运行

用活跃 REST 运行的 `runId` 发送 `POST /api/v1/runs/{runId}/cancel`。请求无需正文。成功时返回空的 204。运行不存在或已结束时返回 404。取消生效后，如果事件流仍连接，流以 `RUN_ERROR` 结束，`code` 为 `CANCELLED`。会话记为 `interrupted`，客户端可以用同一个 `threadId` 继续。A2A 任务通过 [A2A API](a2a.md) 取消。

<a name="conversation"></a>

## 对话

`POST /api/v1/runs` 接收一条 AG-UI 请求，详见[请求格式](events.md#request)和[集成示例](agent-runtime.md)。成功时返回 `text/event-stream`，并带上 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。

事件流开始前，请求体校验失败返回 422。同一会话已有运行或 `runId` 正在使用等冲突返回 409。HTTP 200 的流开始后，运行失败通过[终止事件](events.md#terminal-events)报告。客户端断开时，服务取消上游流，并将未完成的会话标记为中断。

<a name="outputs"></a>

## 产物

<a name="list-outputs"></a>

### 列出文件

`GET /api/v1/sessions/{threadId}/outputs` 返回普通文件，按 `modifiedAt` 降序排列，时间相同再按 `path` 降序排列。路径相对会话产物目录，`size` 以字节计。

```json
{"files": [{"path": "notes.txt", "size": 12, "modifiedAt": "2026-10-15T00:00:00+00:00"}]}
```

列表跳过符号链接和内部路径 `artifacts/conversation_history`、`artifacts/large_tool_results`。会话存在但产物目录尚不存在时返回空列表。产物路径是符号链接或不是目录时返回 409。会话不存在或属于其他用户时返回 404。

<a name="preview-output"></a>

### 预览 UTF-8 文本

`GET /api/v1/sessions/{threadId}/outputs/preview?path=notes.txt` 接收经过 URL 编码的非空相对路径，返回：

```json
{"type": "file", "path": "notes.txt", "content": "Sample notes", "truncated": false}
```

读取上限为 64 KiB。更长的文件返回 `truncated: true`，内容以完整的 UTF-8 字符结尾。绝对路径、`..` 路径分量或 NUL 字符返回 400。文件不存在、经过符号链接、被排除或不是普通文件时返回 404。预览范围内有二进制数据或无效 UTF-8 时返回 415。

<a name="http-errors"></a>

## HTTP 错误

| HTTP 状态码 | 错误码 | 常见原因 |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | 访问产物时会话 ID 无效，或预览路径无效 |
| 404 | `RESOURCE_NOT_FOUND` | 未知路由、会话、文件或非活跃运行 |
| 405 | `METHOD_NOT_ALLOWED` | 路由不支持请求方法 |
| 409 | `CONFLICT` | 活跃运行、重放、待处理的人工反馈、会话绑定无效或产物目录无效 |
| 415 | `UNSUPPORTED_FILE_TYPE` | 预览只支持 UTF-8 文本 |
| 422 | `BAD_REQUEST` | 请求体或查询参数无效，`message` 提供第一条校验错误 |
| 500 | `INTERNAL_ERROR` | 已处理的服务失败，例如扩展重载失败或读取产物失败 |
| 503 | `CONVERSATION_UNAVAILABLE` | 引擎不可用，影响 `/ready`、会话和运行路由 |

<a name="a2a"></a>

## A2A

同一个服务也提供 `GET /.well-known/agent-card.json` 和 `POST /a2a/jsonrpc`。发现、JSON-RPC 请求、鉴权和任务处理见 [A2A 参考](a2a.md)。
