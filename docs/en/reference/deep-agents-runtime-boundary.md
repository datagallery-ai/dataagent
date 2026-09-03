# Deep Agents Runtime capability boundary

This page states what the current Python control plane implements and what phase 1 explicitly closes. The protocol entry is [Deep Agents Runtime](deep-agents-runtime.md).

## Current split

| Role | Owns | Does not own |
| --- | --- | --- |
| Python FastAPI (`apps/api`) | Auth, CSRF, bootstrap REST, official AG-UI endpoint, `create_deep_agent()`, SQLite checkpointer | Data gateway, knowledge, MCP, skills, files, artifacts, session-event replay |
| Clients (Web / TUI) | Send a standard `RunAgentInput`; render text stream and run status | Must not send a CopilotKit `method/params/body` envelope or call the old sidecar |

There is no independent `RUNTIME_SERVICE_URL` / `:8790` sidecar. `npm run dev` starts only the Python API and Web.

## In scope

- Register, login, logout, and email verification (`verificationToken` when `AUTH_EMAIL_DELIVERY=test`)
- Cookies `df_session` / `df_csrf`; unsafe methods require `X-CSRF-Token`
- Legal minimal `GET /api/v1/capabilities`; `activeLlmProfileId` is `server-default`
- Standard `RunAgentInput` SSE: `RUN_STARTED`, `TEXT_MESSAGE_*`, `RUN_FINISHED`; errors as `RUN_ERROR`
- LangGraph checkpoint restore for the same `threadId` after process restart

## Closed

These capabilities return `false` from `GET /api/v1/capabilities`, and REST returns empty lists or empty config:

- conversation memory / session titles
- data tools / datasources / SQL audit
- knowledge / MCP / skills
- files / artifacts / export
- trace DAG / custom events
- HITL resume / branch restore

Existing Web and TUI panels may still render, but they are not implemented by the current backend.
