# REST API

This page is for integrators calling the control plane. It does not describe internal modules. The control plane listens on `http://127.0.0.1:8790` by default, from `server.host` and `server.port` in Home `config.yaml`. REST has no access token; A2A JSON-RPC has an optional shared token, described below. The Web app proxies `/api/v1/*` to that address.

## Envelope

REST JSON responses use the envelope for success and error alike. `POST /api/v1/runs` returns an SSE event stream on success, and REST cancellation returns an empty 204. A2A uses its own Agent Card and JSON-RPC formats, described below, rather than the REST success envelope.

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

Every route is under `/api/v1` except `/healthz`, `/ready`, and A2A (`/.well-known/agent-card.json`, `/a2a/jsonrpc`, whose paths the protocol fixes).

Unknown paths return 404 with `RESOURCE_NOT_FOUND`; a wrong method returns 405 with `METHOD_NOT_ALLOWED`. Conversation paths, including cancel, return 503 with `CONVERSATION_UNAVAILABLE` while the engine is not ready; the Agent Card is not affected.

## Health and identity

| Method | Path | Purpose |
| --- | --- | --- |
| GET, HEAD | `/healthz` | Always 200. When the engine is ready, `data.conversation` includes `ready`, `protocol`, `instanceId`, `model`, `plugins`, `home`, `entry`, `user`, and `timeoutSeconds`; `ready` is the only readiness flag. Otherwise `conversation` is only `ready: false`. |
| GET, HEAD | `/ready` | 200 when the engine is ready, with `data.status` set to `ready`. Otherwise 503. |
| GET | `/api/v1/me` | The current `--user`. The id is `default` when the engine is not ready. There is no signed-in account. |
| GET | `/api/v1/capabilities` | `interaction.resume`, `conversation.memory`, and `artifact.list` are true; `artifact.export` is false. |

`/healthz` does not return input directories, the state directory, or the configuration body.

## Sessions and runs

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/sessions?limit=` | Sessions for the current user. Web and TUI both use this route. `limit` is 1 to 100, default 50. |
| GET | `/api/v1/sessions/{threadId}` | List fields plus `messages`, `notice`, and `pendingInteractions`. Messages come from the saved checkpoint when the row has one, otherwise from the latest graph state on that thread. 409 when the binding cannot be opened. 404 for another user's session. A running turn can still be read. |
| GET | `/api/v1/sessions/{threadId}/outputs` | Output files. Skips symlinks, `artifacts/conversation_history`, and `artifacts/large_tool_results`. 404 when the session does not exist. |
| GET | `/api/v1/sessions/{threadId}/outputs/preview?path=` | UTF-8 text, at most 64 KiB; `truncated` is `true` when cut. Non-text is 415. |
| POST | `/api/v1/runs/{runId}/cancel` | Cancel the active turn. `runId` comes from the `POST /api/v1/runs` request body. Returns an empty 204 on success, 404 when that run does not exist or has finished. After a cancel the event stream ends with `RUN_ERROR` whose `code` is `CANCELLED`; the session is marked `interrupted` and the next turn continues from the saved checkpoint. |

These paths are not implemented: login, `/api/copilotkit`, `/api/copilotkit/info`, datasources, knowledge bases, workspace configuration, session rename, delete, branches, or a trace DAG. They return the 404 envelope.

## Conversation

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/runs` | One turn. The body is one AG-UI input; extra fields are rejected. Success is `text/event-stream`. |

Constraints for `POST /api/v1/runs`:

- `threadId` and `runId` are safe ids of at most 128 characters
- For a new message, `messages` is exactly one item with an `id`, `role` is `user`, and `content` is a nonempty string
- `state` is `null` or `{}`
- `tools`, `context`, and `forwardedProps` are required and empty (`[]`, `[]`, `{}`)
- `resume` and `parentRunId` are not accepted

For HITL, keep the same `threadId`, generate a new `runId`, and send `messages: []`
with a nonempty `answers` map instead of a new message:

```json
{
  "threadId": "thread-1", "runId": "reply-1",
  "messages": [], "answers": {"<interruptId>": "user_id"},
  "state": {}, "tools": [], "context": [], "forwardedProps": {}
}
```

A paused turn ends its SSE stream with `CUSTOM name="human_feedback"`, whose value
is the current pending interaction array, followed by `MESSAGES_SNAPSHOT` and
`RUN_FINISHED` with `metadata.status="waiting_for_input"`. Each interaction contains
`type: "human_feedback"`, `interruptId`, `message`, and optional `options` and
`validationError`. Session detail exposes the same array as `pendingInteractions`,
including after a server restart. Parallel questions can be answered in batches.
Successful completion sends an empty interaction array and `metadata.status="completed"`.
Session status can be `running`, `waiting_for_input`, `completed`, `error`, or `interrupted`.

New messages while waiting, unknown or resolved interrupt IDs, and reuse of a
submitted `runId` in the same session return 409. Empty text answers ask again with
`validationError`. Waiting for user input does not consume the execution timeout.

One session runs one turn at a time. Validation failures are 422 with `BAD_REQUEST`. Business rejections are usually 409 with `CONFLICT`. Both use the error envelope. Other codes: 400 `BAD_REQUEST`, 404 `RESOURCE_NOT_FOUND`, 415 `UNSUPPORTED_FILE_TYPE`, 5xx `INTERNAL_ERROR`. After the stream has started with 200, a run failure is an event, not a changed HTTP status.

## A2A

The same process also serves two A2A 1.0 routes for other agents.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/.well-known/agent-card.json` | Agent Card. The built-in executor advertises JSON-RPC streaming via `SendStreamingMessage`. |
| POST | `/a2a/jsonrpc` | JSON-RPC entry, for example `SendMessage`. Requests must send the `A2A-Version: 1.0` header. |

- `contextId` is the session `threadId`. It shares sessions and checkpoints with `/api/v1/runs`. It must be a safe ID of at most 128 characters; the protocol generates one when it is omitted.
- Only text parts are read. On success the final answer is in `result.task.artifacts` with state `TASK_STATE_COMPLETED`.
- Access is anonymous by default, with one shared team: callers can read, continue, and cancel tasks in the same instance. The optional `DATAAGENT_A2A_AUTH_TOKEN` requires `Authorization: Bearer <token>` on `/a2a/jsonrpc`, otherwise returning 401 with `UNAUTHORIZED`. The Agent Card stays anonymous.
- Each RPC response carries a server-generated `X-Request-ID`. Task `metadata.dataagent` contains `schemaVersion: 1`, the creating request's `requestId`, the input `messageId`, and `rootRunId` when a host run exists. Queries and cancellation preserve the creating request's correlation fields.
- Execution failures remain HTTP 200 with a failed Task. `metadata.dataagent.error` supplies a stable `code`, `stage`, and redacted `message`. Codes include `BAD_REQUEST`, `CONFLICT`, `FORBIDDEN`, `TIMEOUT`, `MODEL_ERROR`, `TOOL_ERROR`, `CONFIG_ERROR`, `LIMIT_EXCEEDED`, `LOOP_DETECTED`, `INTERNAL_ERROR`, and `CANCELLED`. A session occupied by Web/TUI returns a failed Task with `CONFLICT`, not HTTP 409.
- `GetTask` and `CancelTask` accept the task ID in `params.id` and return the Task directly in `result`; `ListTasks` returns the shared list in `result.tasks`. Set `params.configuration.returnImmediately: true` on `SendMessage` to obtain a task ID before completion, then query or cancel it. Tasks are in memory and cannot be queried after restart; persisted conversations and checkpoints can continue. Execution deduplication and automatic retries are not provided.
- JSON-RPC errors follow the JSON-RPC spec, not the error envelope above.
