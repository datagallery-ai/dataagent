# Agent Runtime

Send a conversation turn to DataAgent and read its AG-UI event stream. Start the control plane using the [quick start](../quick-start.md) and check that `GET /ready` returns 200. The examples use the default address, `http://127.0.0.1:8790`.

## Request

Send one new user message to `POST /api/v1/runs`. The server loads conversation history for the given `threadId`.

```bash
curl --no-buffer --include \
  --header 'Content-Type: application/json' \
  --data-binary @- http://127.0.0.1:8790/api/v1/runs <<'JSON'
{
  "threadId": "thread-001",
  "runId": "run-001",
  "messages": [
    {"id": "msg-001", "role": "user", "content": "Reply with a short greeting."}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
JSON
```

Use fresh run and message IDs when repeating the example. See the [request contract](../reference/events.md#request) for field constraints.

## Cancel and resume

To stop the active run, send the following request from another terminal while the stream is open.

```bash
curl --include --request POST http://127.0.0.1:8790/api/v1/runs/run-001/cancel
```

For the next question, keep `threadId`, use fresh run and message IDs, and send the new message. To reload conversation history before continuing, use the [session details endpoint](rest-api.md#session-detail). When DataAgent pauses for human input, follow the [human feedback contract](../reference/events.md#hitl) to submit answers.

## Events

An accepted request returns HTTP 200 with `Content-Type: text/event-stream`. Read the JSON from each SSE `data:` frame. `RUN_STARTED` identifies the turn. Use text and tool events for live progress and apply the final `MESSAGES_SNAPSHOT` when the server sends one.

Check `RUN_FINISHED.metadata.status` for completion or a pause for human input. A `RUN_ERROR` ends the stream with an error code, including `CANCELLED` after an explicit stop. See the [terminal event reference](../reference/events.md#terminal-events) for how to handle each outcome. If the server rejects the request before SSE begins, read the [REST error response](rest-api.md#http-errors).
