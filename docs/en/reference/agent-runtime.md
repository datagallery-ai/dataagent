# Agent Runtime

This page shows an integrator how to send one turn. It does not describe the Web layout or the runtime graph. Web and TUI send each turn to the same control plane.

```text
POST /api/v1/runs
```

The default address is `http://127.0.0.1:8790`. Success is `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

## Request

The body is one AG-UI input. Extra fields are rejected. The server keeps history. The client sends only the new question for this turn.

```json
{
  "threadId": "thread-001",
  "runId": "run-001",
  "messages": [
    {"id": "msg-001", "role": "user", "content": "Summarize the reports in this directory."}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
```

| Field | Constraint |
| --- | --- |
| `threadId`, `runId` | Letters, digits, underscore, and hyphen, at most 128 characters |
| `messages` | Exactly one item with an `id`, `role` is `user`, `content` is a nonempty string |
| `state` | `null` or `{}` |
| `tools`, `context`, `forwardedProps` | Required and empty (`[]`, `[]`, `{}`) |
| `resume`, `parentRunId` | Not accepted |

There is no `run_config`, and no per-request choice of datasource, knowledge base, or model profile. The model, plugins, and workspaces come from the configuration loaded when the control plane started. Web and TUI both use `/api/v1/runs`.

## Cancel and resume

```text
POST /api/v1/runs/<runId>/cancel
```

Success is 204; the event stream then ends with `RUN_ERROR` whose `code` is `CANCELLED`, which clients should show as stopped. 404 when that run is not executing.

Resume by sending the same `threadId`. The next turn continues from the saved checkpoint when the row has one, and otherwise from the latest graph state on that thread. Stop, a dropped connection, and load all stay on this thread. One session accepts one turn at a time. A repeated `runId` is rejected; a `runId` that is still running cannot be reused on any session.

## Events

Clients render AG-UI events. After the response has started with HTTP 200, a run failure is an error event on the stream, not a changed status code. `RUN_ERROR.code` is one of: `TIMEOUT`, `CANCELLED`, `LOOP_DETECTED`, `LIMIT_EXCEEDED`, `INCOMPLETE_STREAM`, `CONFIG_ERROR`, `MODEL_ERROR`, `TOOL_ERROR`, `INTERNAL_ERROR`. Branch on `code`, not on `message`. When the client disconnects, the server cancels the upstream stream and marks the unfinished session interrupted.
