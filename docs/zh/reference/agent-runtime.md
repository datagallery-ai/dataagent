# Agent Runtime

这篇给集成方构造一轮对话。它不写网页布局，也不写运行时内部的图。网页和 TUI 的一轮对话都发给同一个控制面。

```text
POST /api/v1/runs
```

默认地址是 `http://127.0.0.1:8790`。成功响应是 `text/event-stream`，带 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。

## 请求

请求体是一条 AG-UI 输入。多出来的字段会被拒绝。服务端保存历史，客户端每次只送这一轮的新问题。

```json
{
  "threadId": "thread-001",
  "runId": "run-001",
  "messages": [
    {"id": "msg-001", "role": "user", "content": "总结这个目录里的报告。"}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
```

| 字段 | 约束 |
| --- | --- |
| `threadId`、`runId` | 字母、数字、下划线和连字符，最长 128 |
| `messages` | 恰好一条，带 `id`，`role` 为 `user`，`content` 为非空字符串 |
| `state` | `null` 或 `{}` |
| `tools`、`context`、`forwardedProps` | 必须出现，且为空（`[]`、`[]`、`{}`） |
| `resume`、`parentRunId` | 不能使用 |

没有 `run_config`，也没有按请求选择数据源、知识库或模型 Profile。模型、插件和工作区来自控制面启动时的配置。网页和 TUI 都使用 `/api/v1/runs`。

## 取消和恢复

```text
POST /api/v1/runs/<runId>/cancel
```

成功是 204，事件流随后以 `RUN_ERROR`（`code` 为 `CANCELLED`）结束，客户端应显示为“已停止”。这一轮不在执行时是 404。

恢复会话使用同一个 `threadId`。行上有 Checkpoint 时下一轮从它继续，否则读该线程最新图状态。停止、连接断开和加载都留在这个线程。同一会话同时只接受一轮。重复的 `runId` 会被拒绝；正在运行的 `runId` 在任何会话上都不能再用。

## 事件

客户端按 AG-UI 事件渲染。连接已经返回 200 之后，运行失败写在流里的错误事件中，而不是改 HTTP 状态码。`RUN_ERROR.code` 取固定值：`TIMEOUT`、`CANCELLED`、`LOOP_DETECTED`、`LIMIT_EXCEEDED`、`INCOMPLETE_STREAM`、`CONFIG_ERROR`、`MODEL_ERROR`、`TOOL_ERROR`、`INTERNAL_ERROR`。客户端按 `code` 分支，不要按 `message` 分支。客户端断开时，服务取消上游流，并把未完成的会话标成中断。
