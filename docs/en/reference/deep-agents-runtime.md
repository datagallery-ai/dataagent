# Deep Agents Runtime

The control plane and Deep Agents run in one Python FastAPI process. Clients talk only to `POST /api/copilotkit`. Deep Agents extensions live in `runtime/deepagents` and are merged by `create_runtime_agent()`; that package is not a separate service.

## Current entry points

| Method | Path | Description |
| --- | --- | --- |
| GET | `/healthz` | Process liveness |
| GET | `/ready` | Control plane and in-process Deep Agents ready |
| POST | `/api/copilotkit` | Standard AG-UI `RunAgentInput`; returns SSE |

The body accepts only standard fields: `threadId`, `runId`, `messages`, `state`, `tools`, `context`, `forwardedProps`, and `resume`. A CopilotKit envelope with `method: "agent/run"` returns `400 BAD_REQUEST`.

When `LLM_API_KEY` is empty, or `DEEPAGENTS_RUNTIME_MODEL=fake`, the process uses a scripted model on the real `create_deep_agent()` / LangGraph path. Production uses a persistent SQLite checkpointer, not `MemorySaver`.

## Phase 1 capabilities

In scope:

- Cookie sessions and CSRF
- Bootstrap REST: `/api/v1/auth/*`, `/me`, `/capabilities`, `/workspace-config`, `/run-defaults`
- Official `ag-ui-langgraph` text streaming: at least `RUN_STARTED`, text message events, and `RUN_FINISHED`
- LangGraph checkpoint restore by `threadId`

Out of scope: datasources, Knowledge, MCP, Skills, files, artifacts, trace, session-event persistence, branch restore, custom events, and formal HITL.

See the [capability boundary](deep-agents-runtime-boundary.md) and [Agent Runtime and AG-UI](agent-runtime.md).
