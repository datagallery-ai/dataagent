# HTTP 接口

服务只监听 `127.0.0.1`。没有登录和 Bearer 校验。`/healthz` 会返回 Home、输入目录和 `stateDir` 的路径，不返回配置正文或密钥。

默认基址：`http://127.0.0.1:8790`。

## `GET /healthz`

```json
{
  "status": "ready",
  "protocol": "dataagent-v2",
  "instanceId": "<uuid-or-launch-id>",
  "model": "<provider model name>",
  "plugins": ["common"],
  "user": "default",
  "home": "/Users/me/.dataagent",
  "workspaces": [{"name": "workspace-0", "path": "/data"}],
  "stateDir": "/Users/me/.dataagent/runtime",
  "timeoutSeconds": 180
}
```

`model` 是解析后的服务商模型名。`plugins` 是启用 ID。

## `POST /dataagent/stream`

请求体是一条 AG-UI `RunAgentInput`，且 `extra` 字段被拒绝。

约束：

- `threadId`、`runId` 是最长 128 的安全 ID
- `messages` 恰好一条，`role` 为 `user`，`content` 为非空字符串
- 不接受子代理消息
- `state` 只能是 `null` 或 `{}`
- `tools`、`context`、`forwardedProps` 必须为空
- `resume` 和 `parentRunId` 不能使用

成功响应是 `text/event-stream`，带 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。事件见 [事件](events.md)。

客户端断开时，服务取消上游流，并在后台把未完成的会话标成中断。

## `GET /sessions`

查询参数 `limit` 范围 1 到 100，默认 50。只返回当前 `--user` 的会话，按 `updatedAt`、`threadId` 降序。

```json
{
  "sessions": [
    {
      "threadId": "thread-1",
      "title": "first 100 chars of the user text",
      "createdAt": "2026-10-15T00:00:00+00:00",
      "updatedAt": "2026-10-15T00:00:00+00:00",
      "status": "complete",
      "hasCheckpoint": true
    }
  ]
}
```

`status` 为 `running`、`complete`、`error` 或 `interrupted`。进程启动时，索引里仍为 `running` 的行会改成 `interrupted`。

## `GET /sessions/{threadId}`

返回列表接口中的字段，再加上：

| 字段 | 含义 |
| --- | --- |
| `messages` | 最后成功 Checkpoint 里的消息，转成 AG-UI 消息 |
| `newThreadRequired` | 没有成功 Checkpoint 时为 true |
| `notice` | 未完成且有消息时，说明已恢复到最后成功点；未完成且无消息时，要求新开对话；完成时为 null |

会话正在运行时返回 409。绑定缺失或打不开时返回 409。当前用户没有该会话时返回 404。

## `GET /sessions/{threadId}/outputs`

```json
{"files": [{"path": "notes.txt", "size": 12, "modifiedAt": "2026-10-15T00:00:00+00:00"}]}
```

按修改时间新到旧，路径作为次序。排除 `artifacts/conversation_history` 和 `artifacts/large_tool_results`。不跟随符号链接。目录本身是符号链接时返回 409。

尚无持久会话的新对话返回空列表。会话属于其他用户时返回 404。

## `GET /sessions/{threadId}/outputs/preview`

查询参数 `path` 是相对产物目录的路径。

成功：

```json
{"type": "file", "path": "/output/notes.txt", "content": "..."}
```

只预览 UTF-8 文本，最多 64 KiB。超出时在正文末尾附加截断说明。二进制或非 UTF-8 返回 415。路径含 `..`、绝对路径或空字节返回 400。

## 错误

校验失败由 FastAPI 返回 422。业务拒绝多用 409，正文是 `{"detail": "<message>"}`。

| HTTP | 典型原因 |
| --- | --- |
| 400 | 非法会话 ID 或产物路径 |
| 404 | 会话或文件不存在，或会话属于其他 profile |
| 409 | 会话正在运行、profile 不匹配、没有 Checkpoint、重复 run、扩展重载失败、输出目录不是普通目录 |
| 415 | 预览不是 UTF-8 文本 |
| 500 | 列目录或读文件时的操作系统错误 |

流内的运行失败不是这些 HTTP 码。连接已经是 200 的事件流，终止事件见 [事件](events.md)。
