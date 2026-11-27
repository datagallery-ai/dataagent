# Integrate with A2A

Use JSON-RPC to send a message to DataAgent, then poll or cancel the Task. To let DataAgent call another agent, configure an outbound connection below. See the [A2A reference](../../reference/a2a.md) for request fields, states, and errors.

<a name="connect"></a>

## Discover the agent

Start a local control plane using the [quick start](../../quick-start.md). This example uses `http://127.0.0.1:8790`. To run A2A by itself, use [`dataagent serve-a2a`](../../reference/cli-and-environment.md#dataagent-serve-a2a) and substitute its listen address in the commands.

```bash
curl -fsS http://127.0.0.1:8790/.well-known/agent-card.json
```

The Card is public and lists the JSON-RPC endpoint. If your deployment requires authentication, add `-H "Authorization: Bearer $DATAAGENT_A2A_AUTH_TOKEN"` to each JSON-RPC command below after setting the token in your client terminal. To enable the token on a server you operate, set `DATAAGENT_A2A_AUTH_TOKEN` in its startup environment and restart. See [authentication precedence and scope](../../reference/a2a.md#authentication).

<a name="send-message"></a>

## Send a message

Send a text message and ask the server to return a Task for polling:

```bash
curl --include -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{
    "jsonrpc": "2.0", "id": "send-1", "method": "SendMessage",
    "params": {
      "message": {
        "role": "ROLE_USER", "messageId": "message-1",
        "parts": [{"text": "Reply with a short greeting."}]
      },
      "configuration": {"returnImmediately": true}
    }
  }'
```

Read `result.task.id` for polling, `result.task.contextId` for follow-up questions, and `result.task.status.state` for the current result. On a follow-up, use the same context and a new `messageId`. Omit `returnImmediately` to wait for the turn to finish or pause and read the Task directly from the send response.

<a name="poll-and-cancel"></a>

## Poll or cancel

Replace `TASK_ID` with the returned Task ID. Poll while its state is `TASK_STATE_SUBMITTED` or `TASK_STATE_WORKING`:

```bash
curl --include -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"poll-1","method":"GetTask","params":{"id":"TASK_ID"}}'
```

Read the `dataagent_result` text artifact from `result.artifacts` when the Task completes. If the Task requires input, [resume that Task with a structured response](../../reference/a2a.md#a2a-hitl). To stop the Task, use the same request with `method` set to `CancelTask`. Closing the HTTP connection or stopping polling leaves the Task running.

For errors, keep the response's `X-Request-ID`, the Task ID, and the context ID. The [diagnostic fields](../../reference/a2a.md#request-diagnostics) identify whether the request was rejected or the Task failed during execution.

<a name="outbound"></a>

## Call another agent from DataAgent

Add the remote service root to the caller's `config.yaml`:

```yaml
a2a:
  remote_agents:
    - id: sales
      url: http://127.0.0.1:9999
      description: Retrieve sales metrics by time, region, and product.
```

Start the caller with this config and ask it to call `a2a__sales`. If the peer requires a token or a different timeout, use the [outbound configuration fields](../../reference/configuration.md#a2a). Each local thread keeps a separate remote context.

<a name="python-client"></a>

### Call directly from Python

Reuse the client for a follow-up in the same remote context. See the [Python API](../../reference/python-api.md#a2a-api) for available arguments and errors.

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
