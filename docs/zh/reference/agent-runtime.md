<a id="agent-runtime"></a>

# AG-UI 接入

向 DataAgent 发送一轮对话并读取 AG-UI 事件流。先按[快速开始](../quick-start.md)启动控制面，确认 `GET /ready` 返回 200。以下示例使用默认地址 `http://127.0.0.1:8790`。

## 请求

向 `POST /api/v1/runs` 提交一条新消息。服务端按 `threadId` 加载对话历史。

```bash
curl --no-buffer --include \
  --header 'Content-Type: application/json' \
  --data-binary @- http://127.0.0.1:8790/api/v1/runs <<'JSON'
{
  "threadId": "thread-001",
  "runId": "run-001",
  "messages": [
    {"id": "msg-001", "role": "user", "content": "请用一句话打招呼。"}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
JSON
```

再次运行示例时，换用新的运行 ID 和消息 ID。完整字段约束见[请求契约](events.md#request)。

## 取消和恢复

要停止这一轮，在流仍打开时，从另一个终端发送以下请求。

```bash
curl --include --request POST http://127.0.0.1:8790/api/v1/runs/run-001/cancel
```

发送下一题时沿用 `threadId`，使用新的运行 ID、消息 ID 和消息内容。继续前如需加载历史，调用[会话详情接口](rest-api.md#session-detail)。如果 DataAgent 暂停并等待用户输入，按[人工交互契约](events.md#hitl)提交回答。

## 事件

请求被接受后返回 HTTP 200 和 `Content-Type: text/event-stream`。逐条读取 SSE 的 `data:` 帧并解析其中的 JSON。`RUN_STARTED` 标识本轮运行。用文本和工具事件呈现实时进度，收到最终的 `MESSAGES_SNAPSHOT` 时更新消息。

查看 `RUN_FINISHED.metadata.status` 判断本轮已完成还是在等待用户输入。`RUN_ERROR` 携带结束错误码，显式停止对应 `CANCELLED`。各类结束事件的处理方式见[终止事件参考](events.md#terminal-events)。流建立前的拒绝响应见 [REST 错误响应](rest-api.md#http-errors)。
