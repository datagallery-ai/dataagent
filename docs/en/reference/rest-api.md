# REST API reference

Call the control plane started by `dataagent serve`. The default base URL is `http://127.0.0.1:8790`, configured by `server.host` and `server.port` in the selected YAML. The Web workbench proxies `/api/v1/*` to this service. REST has no built-in access-token check. See the [security notes](../concepts/security.md) before exposing the control plane beyond loopback.

## Envelope { #envelope }

JSON success responses and handled HTTP errors use these envelopes:

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

A successful `POST /api/v1/runs` returns an SSE stream with HTTP 200. A successful cancel request returns HTTP 204 with an empty body. `HEAD` on the health endpoints returns only the status and headers. The response examples below show the contents of `data`.

## Health and identity { #health-and-identity }

| Method | Path | Response |
| --- | --- | --- |
| GET, HEAD | `/healthz` | 200 with `status: "ok"` and `conversation`. |
| GET, HEAD | `/ready` | 200 with `status: "ready"` and the same `conversation` fields as `/healthz`. Returns 503 when the engine is unavailable. |
| GET | `/api/v1/me` | `user.id` and `user.displayName` are the server's local user (`--user`). Both default to `default` if no runtime is configured. `workspace` is always `{"id":"local","name":"Local"}`. |
| GET | `/api/v1/capabilities` | `interaction.resume`, `conversation.memory`, and `artifact.list` are true. `artifact.export` is false. |

When the engine is unavailable, the `conversation` object on `/healthz` contains only `{"ready": false}`. A ready control plane returns a shape like this:

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

Use `conversation.ready` for readiness. `instanceId` identifies the process, and `timeoutSeconds` reflects `dataagent.limits.timeout_seconds`. `entry` reports the configured value without checking whether it resolves to a compiled subagent. The response omits input directories, the state directory, and configuration contents.

## Sessions and runs { #sessions-and-runs }

### List sessions { #list-sessions }

`GET /api/v1/sessions?limit=50` returns sessions for the current server user in descending order by `updatedAt`, then `threadId`. `limit` is an integer from 1 through 100 and defaults to 50.

```json
{
  "sessions": [
    {
      "threadId": "thread-1",
      "title": "Summarize the latest report",
      "createdAt": "2026-10-15T00:00:00+00:00",
      "updatedAt": "2026-10-15T00:00:00+00:00",
      "status": "completed",
      "hasCheckpoint": true
    }
  ]
}
```

Statuses are `running`, `waiting_for_input`, `completed`, `error`, and `interrupted`. A session waiting for an A2A answer is shown as `interrupted` by REST. On startup, sessions still recorded as `running` become `interrupted`.

### Read a session { #session-detail }

`GET /api/v1/sessions/{threadId}` adds `messages`, `pendingInteractions`, and `notice` to the list item fields. The messages come from the saved checkpoint or, if absent, the latest graph state on that thread. The route remains available while a run is active. `pendingInteractions` contains prompts that can be answered over REST. See [human feedback](../reference/events.md#hitl) for their shape and how to answer. `notice` is text or `null`. On an error or interruption, it indicates whether a checkpoint was restored.

An unknown session or a session belonging to another user returns 404. If the current user's session binding cannot be opened, the route returns 409.

### Cancel a run { #cancel-run }

Send `POST /api/v1/runs/{runId}/cancel` with the active REST run's `runId`. No request body is needed. Success returns an empty 204. An unknown or finished run returns 404. Once cancellation takes effect, the connected stream ends with `RUN_ERROR` and `code: "CANCELLED"`. The session is marked `interrupted`. The client can continue on the same `threadId`. Use the [A2A API](../reference/a2a.md) to cancel an A2A task.

## Conversation { #conversation }

`POST /api/v1/runs` accepts one AG-UI request. See the [request format](../reference/events.md#request) and [integration example](agent-runtime.md). The successful response is `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

Before the stream starts, invalid bodies return 422. Conflicts, including an active run on the same session or an already-active `runId`, return 409. Once the server starts a 200 stream, run failures are reported as [terminal events](../reference/events.md#terminal-events). If the client disconnects, the server cancels the upstream stream and marks the unfinished session as interrupted.

## Outputs { #outputs }

### List files { #list-outputs }

`GET /api/v1/sessions/{threadId}/outputs` returns regular files sorted by descending `modifiedAt`, then descending `path`. Paths are relative to the session's output directory, and sizes are in bytes.

```json
{"files": [{"path": "notes.txt", "size": 12, "modifiedAt": "2026-10-15T00:00:00+00:00"}]}
```

The list excludes symbolic links and the internal paths `artifacts/conversation_history` and `artifacts/large_tool_results`. An existing session with no output directory returns an empty list. An output directory that is a symbolic link or a path that is not a directory returns 409. A session that does not exist or belongs to another user returns 404.

### Preview UTF-8 text { #preview-output }

`GET /api/v1/sessions/{threadId}/outputs/preview?path=notes.txt` takes a non-empty, URL-encoded relative path and returns:

```json
{"type": "file", "path": "notes.txt", "content": "Sample notes", "truncated": false}
```

The read limit is 64 KiB. A longer file has `truncated: true` and the returned content ends on a complete UTF-8 character. An absolute path, a `..` component, or a NUL character returns 400. Missing, linked, excluded, or non-regular files return 404. Binary or invalid UTF-8 content in the preview returns 415.

## HTTP errors { #http-errors }

| HTTP status | Code | Typical cause |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | Invalid session ID when accessing outputs, or an invalid preview path |
| 404 | `RESOURCE_NOT_FOUND` | Unknown route, session, file, or inactive run |
| 405 | `METHOD_NOT_ALLOWED` | Unsupported method for the route |
| 409 | `CONFLICT` | Active run, replay, unresolved feedback, invalid session binding, or invalid outputs directory |
| 415 | `UNSUPPORTED_FILE_TYPE` | Preview only supports UTF-8 text |
| 422 | `BAD_REQUEST` | Invalid request body or query parameter. `message` describes the first validation error. |
| 500 | `INTERNAL_ERROR` | Handled server error, such as failure to reload extensions or read outputs |
| 503 | `CONVERSATION_UNAVAILABLE` | Engine unavailable for `/ready`, session routes, or run routes |

## A2A { #a2a }

The same server exposes `GET /.well-known/agent-card.json` and `POST /a2a/jsonrpc` for A2A. See the [A2A reference](../reference/a2a.md) for discovery, JSON-RPC requests, authorization, and task handling.
