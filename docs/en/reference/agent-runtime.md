# Agent Runtime

This page shows an integrator how to send one turn. It does not describe the Web layout or the runtime graph. Web and TUI send each turn to the same control plane.

```text
POST /dataagent/stream
```

The default address is `http://127.0.0.1:8790`. Success is `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

## Request

The body is one AG-UI input. Extra fields are rejected. The server keeps history. The client sends only the new question for this turn.

```json
{
  "threadId": "thread-001",
  "runId": "run-001",
  "messages": [
    {"role": "user", "content": "Summarize the reports in this directory."}
  ]
}
```

| Field | Constraint |
| --- | --- |
| `threadId`, `runId` | Letters, digits, underscore, and hyphen, at most 128 characters |
| `messages` | Exactly one item, `role` is `user`, `content` is a nonempty string |
| `state` | `null` or `{}` |
| `tools`, `context`, `forwardedProps` | Empty |
| `resume`, `parentRunId` | Not accepted |

There is no `run_config`, and no per-request choice of datasource, knowledge base, or model profile. The model, plugins, and workspaces come from the configuration loaded when the control plane started. Web and TUI both use `/dataagent/stream`.

## Cancel and resume

```text
POST /runs/<runId>/cancel
```

Success is 204. 404 when that run is not executing.

Resume by sending the same `threadId`. The next turn continues from the saved checkpoint when the row has one, and otherwise from the latest graph state on that thread. Stop, a dropped connection, and load all stay on this thread. One session accepts one turn at a time. A repeated `runId` is rejected.

## Events

Clients render AG-UI events. After the response has started with HTTP 200, a run failure is an error event on the stream, not a changed status code. When the client disconnects, the server cancels the upstream stream and marks the unfinished session interrupted.
