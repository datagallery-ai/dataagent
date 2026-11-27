# 接入 A2A

通过 JSON-RPC 向 DataAgent 发送消息，再查询或取消任务。也可以配置 DataAgent 调用其他 Agent。字段、任务状态和错误见 [A2A 参考](../../reference/a2a.md)。

<a name="connect"></a>

## 发现 Agent

按[快速开始](../../quick-start.md)启动本地控制面。示例使用 `http://127.0.0.1:8790`。如果只需启动 A2A 服务，使用 [`dataagent serve-a2a`](../../reference/cli-and-environment.md#dataagent-serve-a2a)，并将示例替换为实际监听地址。

```bash
curl -fsS http://127.0.0.1:8790/.well-known/agent-card.json
```

Agent Card 可匿名读取，其中列有 JSON-RPC 地址。如果服务要求令牌，先在调用终端设置 `DATAAGENT_A2A_AUTH_TOKEN`，再向下文每条 JSON-RPC 命令添加 `-H "Authorization: Bearer $DATAAGENT_A2A_AUTH_TOKEN"`。部署方需要启用令牌时，在服务启动环境中设置该变量并重启。见[令牌生效范围及优先级](../../reference/a2a.md#authentication)。

<a name="send-message"></a>

## 发送消息

发送一条文本消息，先拿到任务再查询：

```bash
curl --include -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{
    "jsonrpc": "2.0", "id": "send-1", "method": "SendMessage",
    "params": {
      "message": {
        "role": "ROLE_USER", "messageId": "message-1",
        "parts": [{"text": "请用一句话打招呼。"}]
      },
      "configuration": {"returnImmediately": true}
    }
  }'
```

从响应读取 `result.task.id` 用于查询，读取 `result.task.contextId` 用于续聊，并查看 `result.task.status.state`。下一条消息沿用会话 ID，并使用新的 `messageId`。省略 `returnImmediately` 时，发送请求会等待本轮结束或暂停，随后在发送响应中返回任务。

<a name="poll-and-cancel"></a>

## 查询或取消任务

把 `TASK_ID` 替换为刚收到的任务 ID。状态为 `TASK_STATE_SUBMITTED` 或 `TASK_STATE_WORKING` 时继续查询：

```bash
curl --include -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"poll-1","method":"GetTask","params":{"id":"TASK_ID"}}'
```

任务完成后，从 `result.artifacts` 读取 `dataagent_result` 文本产物。若任务等待人工输入，[向原任务提交结构化答复](../../reference/a2a.md#a2a-hitl)。需要停止任务时，将同一请求的 `method` 改为 `CancelTask`。断开 HTTP 连接或停止轮询后，任务会继续运行。

报错时保留响应头 `X-Request-ID`、任务 ID 和会话 ID。[诊断字段](../../reference/a2a.md#request-diagnostics)可以区分请求被拒绝和任务执行失败。

<a name="outbound"></a>

## 让 DataAgent 调用其他 Agent

在调用方的 `config.yaml` 中填写远端服务的根地址：

```yaml
a2a:
  remote_agents:
    - id: sales
      url: http://127.0.0.1:9999
      description: 按时间、区域和产品查询销售指标。
```

用这份配置启动调用方，并请它调用 `a2a__sales`。若远端需要令牌或不同超时，见[出站配置字段](../../reference/configuration.md#a2a)。每条本地线程保留独立的远端会话。

<a name="python-client"></a>

### 从 Python 直接调用

复用同一个客户端，可以在原远端会话中继续提问。参数与异常见 [Python API](../../reference/python-api.md#a2a-api)。

```python
import asyncio

from dataagent.extensions.a2a import A2ACaller


async def ask():
    caller = A2ACaller("http://127.0.0.1:9999")
    first = await caller.send("hello")
    second = await caller.send("continue")
    print(first.text, second.text)


asyncio.run(ask())
```
