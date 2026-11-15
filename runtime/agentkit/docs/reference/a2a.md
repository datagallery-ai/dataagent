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

`contextId` 必须是最长 128 的路径安全 ID（字母、数字、下划线和连字符）。它同时是 LangGraph 的 `thread_id` 和会话目录名。省略时由协议生成 UUID。下一条消息从会话索引里记下的检查点继续，和 `/dataagent/stream` 是同一条。报错或取消也会把图已经到达的检查点写进这个索引。同一 context 上的调用串行执行。

只读取文本 part。空文本或非法 `contextId` 使任务进入 `TASK_STATE_FAILED`，不会调用模型。成功时最终回答在 `result.task.artifacts`，状态是 `TASK_STATE_COMPLETED`。

这条会话记在进程自己的 profile（`default` 或 `--user`）的会话索引里。登记发生在读取 checkpoint 之前。`thread_id` 全局唯一：该 id 已经属于另一个 profile 时，任务失败，模型不会被调用。

设置环境变量 `DATAAGENT_A2A_AUTH_TOKEN`（进程环境或 `.env`）后，`dataagent serve` 和 `dataagent serve-a2a` 都要求 JSON-RPC 带 `Authorization: Bearer <token>`；Agent Card 仍可匿名读取。`serve-a2a --auth-token` 优先于环境变量。缺少或错误时返回 401，正文是错误信封，错误码 `UNAUTHORIZED`。对外暴露 A2A 时一定要设置令牌。

## 调用别的 agent

在 `config.json` 里声明远端 agent。装配时它们成为工具，工具名是 `a2a__<id>`。

```json
{
  "a2a": {
    "agents": [
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

直接调用时，不传 `thread_id` 的 `A2ACaller.send` 共用这一条 caller 自己的 context。

```python
from a2a_server import A2ACaller

async def ask():
    caller = A2ACaller("http://127.0.0.1:9999")
    first = await caller.send("hello")
    second = await caller.send("continue")
    return first.text, second.text
```
