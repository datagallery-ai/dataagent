# A2A

Both `dataagent serve` and `dataagent serve-a2a` expose A2A 1.0. Discover the Agent Card anonymously at `GET /.well-known/agent-card.json`. Send, poll, or cancel tasks with JSON-RPC 2.0 at `POST /a2a/jsonrpc` and set `A2A-Version: 1.0`.

The card lists only JSONRPC and sets `capabilities.streaming: false`. It constructs the endpoint using request `Host` (fallback `127.0.0.1`) and `X-Forwarded-Proto` or the request scheme. Both `a2a.card.name` and `description` default to `DataAgent`. The service provides skill `chat`. Restart after changing [card settings](configuration.md#a2a).

Regular `serve` also exposes [REST](rest-api.md). Card discovery works before the engine is ready. Standalone A2A exposes only A2A routes and can override `server.host` and `server.port`. Both commands acquire the same Home `backend.lock`. See [startup options](cli-and-environment.md#dataagent-serve-a2a).

<a name="send-message"></a>

## Send a message

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

<a name="request-parameters"></a>

### Request parameters

| Field or header | Required | Default and constraint |
| --- | --- | --- |
| `Content-Type: application/json` | Yes | JSON body |
| `A2A-Version: 1.0` | Yes | Missing or mismatched version causes a JSON-RPC error |
| `Authorization: Bearer <token>` | With server authentication | Applies to all JSON-RPC methods |
| `jsonrpc` | Yes | `"2.0"` |
| `id` | For request-response calls | String or number to correlate response. Separate from task ID or idempotency |
| `method` | Yes | Case-sensitive, e.g. `SendMessage` |
| `params.message` | For SendMessage | A2A Message |
| `params.message.messageId` | Yes | Nonempty. Use a fresh ID. Execution is not deduplicated |
| `params.message.role` | Yes | `"ROLE_USER"` for user input |
| `params.message.parts` | Yes | Nonblank text for normal turns, one structured data part for HITL resume |
| `params.message.contextId` | No | Generate a UUID when omitted; reuse the result for follow-ups. Pattern `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` |
| `params.message.taskId` | Only for resuming a pending task | Omit for a new task, including a new turn on a completed context |
| `params.configuration` | No | Defaults to waiting for the turn |
| `params.configuration.returnImmediately` | No | `false` waits. `true` returns a Task to poll |
| `params.configuration.historyLength` | No | Nonnegative integer limiting returned Task messages. Does not clear stored context |

The A2A SDK validates message shape. The service checks text and context. Requests do not override configured models, plugins, databases, or workspaces.

A normal message uses text parts. Blank text and invalid context produce `TASK_STATE_FAILED` with `BAD_REQUEST` before calling the model. Success returns `TASK_STATE_COMPLETED` with the text artifact named `dataagent_result` in `result.task.artifacts`.

`contextId` is the session's HTTP `threadId` and LangGraph `thread_id`. Requests on one context execute serially and resume from the shared index's checkpoint. Errors or cancellation save the checkpoint reached. The server startup user owns sessions, default `default` or `--user`. Another user's context yields a failed Task with `FORBIDDEN`.

<a name="authentication"></a>

## Authentication

Default access is anonymous. Both server commands use `DATAAGENT_A2A_AUTH_TOKEN` if set. Precedence is standalone `--auth-token`, nonempty process value, then the selected env file or Home `.env`. Regular `serve` reads the effective environment and has no auth flag. Restart after changing it. Use distinct credentials for model and outbound A2A calls.

With a token, every JSON-RPC method requires HTTP Bearer. The public Agent Card describes `securitySchemes` and `securityRequirements`. The token does not cover REST, AG-UI, health, or output routes and provides no caller identity or tenant isolation.

Set the token in the server terminal:

```bash
export DATAAGENT_A2A_AUTH_TOKEN='replace-with-a-long-random-token'
uv run dataagent serve
```

Set the same value separately in the client terminal. Loading server `.env` does not affect other terminals.

```bash
export DATAAGENT_A2A_AUTH_TOKEN='replace-with-a-long-random-token'
curl -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -H "Authorization: Bearer $DATAAGENT_A2A_AUTH_TOKEN" \
  -d '{"jsonrpc":"2.0","id":"list-1","method":"ListTasks","params":{}}'
```

Missing or invalid credentials return HTTP 401 with `{"success":false,"error":{"code":"UNAUTHORIZED","message":"Invalid or missing Bearer token."}}` before model execution. See [Bearer validation](../../../dataagent/server/a2a/server.py).

<a name="request-diagnostics"></a>

## Request IDs and diagnostics

Every JSON-RPC response includes server-generated `X-Request-ID`, including authentication, protocol, and readiness errors. Use it for diagnostics; the JSON-RPC `id` correlates the request and response.

| ID | Purpose |
| --- | --- |
| `message.messageId` | Fresh message ID, automatically generated by the SDK client |
| `message.contextId` | Session `thread_id`; reuse for follow-ups |
| `task.id` | Generated Task ID. Omit `message.taskId` when creating a task |
| `metadata.dataagent.requestId` | Matches the creation request's `X-Request-ID` |
| `metadata.dataagent.rootRunId` | Correlates index and graph trace. Absent if input validation fails |

Both successful and failed Tasks contain `metadata.dataagent` with `schemaVersion: 1`. Failure example:

```json
{
  "schemaVersion": 1,
  "requestId": "<server-request-id>",
  "messageId": "<message-id>",
  "rootRunId": "<host-run-id>",
  "error": {
    "code": "TIMEOUT",
    "stage": "agent_invoke",
    "message": "TimeoutError"
  }
}
```

Success omits `error`. Polling or canceling does not replace the creation correlation ID.

| Error layer | Where to read |
| --- | --- |
| HTTP rejection | HTTP envelope with 401 `UNAUTHORIZED` or, with regular `serve`, 503 `CONVERSATION_UNAVAILABLE` |
| RPC parsing, version, method, parameters, or Task access | Top-level `error`. Unknown method `-32601`, missing Task `-32001`, already terminal on cancellation `-32002` |
| Task execution | HTTP 200 with `TASK_STATE_FAILED`. Inspect `metadata.dataagent.error.code` |

| Code | Meaning |
| --- | --- |
| `BAD_REQUEST` | Empty text, invalid context, or other input validation |
| `CONFLICT` | REST holds the session or a plain A2A message encounters a pending interrupt |
| `FORBIDDEN` | Session belongs to a different server user |
| `TIMEOUT` | Invocation or recognized model timeout |
| `MODEL_ERROR`, `TOOL_ERROR` | Classified model or tool failure |
| `CONFIG_ERROR`, `LIMIT_EXCEEDED`, `LOOP_DETECTED` | Configuration, limits, or loop detection |
| `INTERNAL_ERROR` | Other failures, including checkpoint persistence |
| `CANCELLED` | Task canceled, state `TASK_STATE_CANCELED` |

Stages include `validation`, `queue`, `session_prepare`, `agent_invoke`, `checkpoint_settle`, and `response`. A checkpoint write failure can fail a successful model run. Inbound `dataagent.limits.timeout_seconds` bounds agent `ainvoke` only. Queueing, preparation, and settlement are outside the limit. After a client timeout, [poll](#tasks) if you have the ID.

<a name="tasks"></a>

## Poll and cancel tasks

All methods share the URL, version, and authentication. The following arguments go in outer `params`.

| Method | Arguments | Result |
| --- | --- | --- |
| `SendMessage` | `message`, optional `configuration` | `result.task` |
| `GetTask` | Task `id`, optional `historyLength` | Task in `result` |
| `CancelTask` | Task `id` | Task in `result`, state `TASK_STATE_CANCELED` on success |
| `ListTasks` | `{}`, or `contextId` to filter and `includeArtifacts: true` for artifacts | Instance's shared `result.tasks` |

Outer RPC `id` is separate from `params.id`. Stopping polling or disconnecting does not cancel. Call `CancelTask` to stop work. The default in-memory TaskStore loses IDs on restart. Ordinary sessions can resume on the same Home checkpoint. See [human feedback](#a2a-hitl) for waiting tasks.

<a name="call-modes"></a>

### Wait or return immediately

Both modes use nonstreaming `SendMessage`. Omit `returnImmediately` or set it false to wait for completion or input. Set true to receive the ID early, then poll with `GetTask` and cancel if needed. `SendStreamingMessage` and `SubscribeToTask` return `-32004`; push notifications return `-32003`.

```json
{
  "jsonrpc": "2.0", "id": "submit-1", "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER", "messageId": "async-message-1",
      "contextId": "async-thread-1", "parts": [{"text": "Analyze the configured data."}]
    },
    "configuration": {"returnImmediately": true}
  }
}
```

A short task may already complete in the first response. Example of an early response:

```json
{"jsonrpc":"2.0","id":"submit-1","result":{"task":{"id":"22222222-2222-4222-8222-222222222222","contextId":"async-thread-1","status":{"state":"TASK_STATE_SUBMITTED"}}}}
```

Poll with the same version and authentication headers:

```json
{"jsonrpc":"2.0","id":"poll-1","method":"GetTask","params":{"id":"22222222-2222-4222-8222-222222222222"}}
```

You can see `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`, or `TASK_STATE_INPUT_REQUIRED`. Terminal states are `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_CANCELED`, and `TASK_STATE_REJECTED`. Read artifacts on success and metadata on failure. See [server tests](../../../tests/test_a2a_server.py) and [diagnostic tests](../../../tests/test_a2a_observability.py). Execution does not guarantee retries, deduplication, or idempotency. Check task state before retrying work with side effects.

<a name="a2a-hitl"></a>

## Pause and resume human feedback

LangGraph `interrupt()` and `Command(resume=...)` work without streaming. A pause yields `TASK_STATE_INPUT_REQUIRED` in synchronous responses or from `GetTask`, with no final answer artifact. `status.message.parts` includes text and a structured part. `interrupts[].id/value` comes from the native interrupt. `pauseId` identifies this pause. Non-sensitive fields are kept. Approval requests include tool name, redacted arguments, and review rules. Execution uses checkpointed arguments or an approved edit.

```json
{
  "data": {
    "kind": "dataagent.hitl.request",
    "pauseId": "pause-1",
    "interrupts": [{
      "id": "interrupt-1",
      "value": {
        "kind": "human_feedback",
        "question": "Should the calculation include tax?",
        "tool_call_id": "call-1"
      }
    }]
  }
}
```

Resume with the same task and context, a fresh message ID, and exactly one data part:

```json
{
  "jsonrpc": "2.0", "id": "resume-1", "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER", "messageId": "human-response-1",
      "taskId": "task-1", "contextId": "thread-1",
      "parts": [{"data": {
        "kind": "dataagent.hitl.response",
        "pauseId": "pause-1",
        "responses": {"interrupt-1": "Include tax."}
      }}]
    }
  }
}
```

Use the actual IDs. Every pause gets a new `pauseId`, even if native interrupt IDs repeat. `responses` maps native IDs to native values. The `data-qa__request_human_feedback` tool expects text. Native approvals expect a decision object:

```json
{"interrupt-1":{"decisions":[{"type":"approve"}]}}
```

For ordinary interrupts with values of magnitude at least 2⁵³, `valueJson` replaces `value` with lossless JSON text. Use `responsesJson` instead of `responses` for corresponding large numbers, for example `"responsesJson":"{\"interrupt-1\":{\"id\":9223372036854775809}}"`. Parse these strings without losing integer precision. An out-of-range numeric value sent directly is rejected without resolving the interrupt.

If approval `action_requests[].args` contains such numbers, the entire argument object is serialized to JSON text. Display it as text or edit with a lossless parser. Return edited JSON text as `edited_action.args`, for example `"{\"id\":9223372036854775809}"`. Direct approval uses the checkpoint arguments. An edit containing `[REDACTED]` fails; supply actual values or approve the original. To reject, use a decision such as `{"decisions":[{"type":"reject","message":"Do not execute this request."}]}`. Use permitted decisions from `review_configs` and match `action_requests` order. Clarification and approval are separate.

- Resuming keeps the Task ID and can pause again or reach a terminal state. Creation correlation stays in metadata; each resume has its own audit and trace. Completion logs use `root_run_id` for the resumed run and `task_root_run_id` for creation.
- Bad or stale IDs, invalid decisions, missing original tasks, and plain text sent to a waiting Task are RPC parameter errors. If an answer goes stale after validation due to a concurrent retry, the service returns current Task state. Use its newest prompt if still waiting.
- If another run temporarily holds the session, the Task returns to `INPUT_REQUIRED` with the same prompt and pause IDs. Resubmit after the session is free.
- A pause saves the checkpoint and releases execution. REST shows `interrupted` with a pending-input notice. A new REST turn returns 409, and a new A2A task cannot bypass the pause. Submit to the original Task.
- Cancellation returns after session settlement. Canceling a waiting Task marks the session interrupted and keeps the checkpoint for inspection. A canceled or terminal Task cannot resume. Use a new context for fresh work.
- The default in-memory TaskStore loses waiting Tasks on restart even though checkpoints persist. Use a new context to run again. The default outbound client sends text only.

See [HITL tests](../../../tests/test_a2a_hitl.py) for both server modes, polling, repeat pauses, native tool approvals, and cancellation. This test needs no live model or semantic service:

```bash
uv run --no-sync pytest tests/test_a2a_hitl.py -q
```

For manual feedback testing, enable the `data-qa` plugin and ask the root to invoke `data-qa__request_human_feedback`. Reply using the actual interrupt. Native `interrupt_on` tool approval in tests is provided by the test assembly.

<a name="inbound-and-subagents"></a>

## Inbound calls and local subagents

The root receives inbound requests and can delegate to enabled local compiled subagents such as nl2sql through `task`. This requires neither another A2A server nor `a2a.remote_agents`. Leaving `dataagent.entry` unset exercises root delegation. Selecting nl2sql as the entry bypasses the root.

Task completion means agent execution finished. An ordinary nl2sql business failure can return a tool result with `error` and still be explained by a completed root Task. Check the delegation, actual result, and outputs. See [test coverage](../../development/testing.md#a2a-nl2sql).

<a name="outbound"></a>

## Call other agents

Configure remote agents in the caller's YAML. Each becomes `a2a__<id>`. The description can tell the caller when to use it and what to send.

```yaml
a2a:
  remote_agents:
    - id: sales
      url: http://127.0.0.1:9999
      description: >
        Call when the user needs sales metrics by time, region, or product.
        Specify the metric, time range, and filters in the message.
```

Supply the service root URL. See [configuration](configuration.md#a2a) for tokens and timeouts. Local threads keep separate remote context IDs.

`A2ACaller.send` awaits a turn result or a prompt and has no `returnImmediately` argument. It retains context from either a Message or Task response. A waiting Task yields text prefixed with `INPUT_REQUIRED`, and the next call on that local thread carries its task ID. The tool sends text, so use [direct JSON-RPC](#a2a-hitl) for structured resume. Failed, canceled, or rejected Tasks raise `A2ACallError`.

The error's local code is `TOOL_ERROR`, with optional `remote_code`, `request_id`, `task_id`, `context_id`, and `root_run_id`. With older servers, task and context remain available from protocol fields even when diagnostic metadata is absent. Successful `A2AResult` can also include request and run IDs. Calls without `thread_id` share the caller's own context.

```python
from dataagent.extensions.a2a import A2ACaller

async def ask():
    caller = A2ACaller("http://127.0.0.1:9999")
    first = await caller.send("hello")
    second = await caller.send("continue")
    return first.text, second.text
```
