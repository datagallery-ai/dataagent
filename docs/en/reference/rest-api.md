# REST API

This page is for integrators calling the control plane. It does not describe internal modules. The control plane listens on `http://127.0.0.1:8790` by default, from `server.host` and `server.port` in Home `config.json`. There is no access token. The Web app proxies the same paths to that address.

## Envelope

`/healthz`, `/ready`, and `/api/v1/*` use an envelope.

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Route not found."}}
```

`/sessions` and the output routes return their own JSON, without that envelope. `POST /api/v1/runs/{runId}/cancel` returns an empty 204 on success.

Unknown paths return 404 with `RESOURCE_NOT_FOUND`. Conversation paths return 503 with `CONVERSATION_UNAVAILABLE` while the engine is not ready.

## Health and identity

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Always 200. When the engine is ready, `data.conversation` includes `ready`, `home`, `model`, `plugins`, `entry`, `user`, and `timeoutSeconds`. Otherwise `conversation` is only `ready: false`. |
| GET | `/ready` | 200 when the engine is ready. `data.status` and `data.control_plane` are both `ready`. Otherwise 503. |
| GET | `/api/v1/me` | The current `--user`. The id is `default` when the engine is not ready. There is no signed-in account. |

`/healthz` does not return input directories, the state directory, or the configuration body.

## Sessions and runs

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/sessions?limit=` | Sessions for the current user, inside the envelope. `limit` is 1 to 100, default 50. |
| GET | `/api/v1/sessions/{threadId}/conversation` | The same document as `GET /sessions/{threadId}` (AG-UI `messages`, `status`, `notice`) inside `{success, data}`. An unknown session is 404. A running turn can still be read. |
| GET | `/sessions?limit=` | The same index as `{"sessions":[...]}`. The TUI uses this route. |
| GET | `/sessions/{threadId}` | List fields plus `messages` and `notice`. Messages come from the saved checkpoint when the row has one, otherwise from the latest graph state on that thread. 409 when the binding cannot be opened. 404 for another user's session. A running turn can still be read. |
| GET | `/sessions/{threadId}/outputs` | Output files. Skips symlinks, `artifacts/conversation_history`, and `artifacts/large_tool_results`. |
| GET | `/sessions/{threadId}/outputs/preview?path=` | UTF-8 text, at most 64 KiB. Non-text is 415. |
| POST | `/api/v1/runs/{runId}/cancel` | Cancel the active turn. 404 when that run is not executing. |

These paths are not implemented: login, `/api/v1/capabilities`, datasources, knowledge bases, workspace configuration, session rename, delete, branches, or a trace DAG. They return the 404 envelope.

## Conversation

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/dataagent/stream` | One turn. The body is one AG-UI input; extra fields are rejected. Success is `text/event-stream`. |
| POST | `/api/copilotkit` | Another entry on the same process and the same graph. The Web path does not use it. |
| GET | `/api/copilotkit/info` | CopilotKit runtime info. |

Constraints for `POST /dataagent/stream`:

- `threadId` and `runId` are safe ids of at most 128 characters
- `messages` is exactly one item, `role` is `user`, and `content` is a nonempty string
- `state` is `null` or `{}`
- `tools`, `context`, and `forwardedProps` are empty
- `resume` and `parentRunId` are not accepted

One session runs one turn at a time. Validation failures are usually 422. Business rejections are usually 409 with `{"detail":"..."}`. After the stream has started with 200, a run failure is an event, not a changed HTTP status.

The same process also serves `GET /.well-known/agent-card.json` and `POST /a2a/jsonrpc`, without an extra Bearer token.
