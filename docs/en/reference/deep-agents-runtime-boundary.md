# Deep Agents Runtime v1 capability boundary

This document is for **runtime implementers**. It states what v1 must provide, what is already wired, what is leftover, and what is explicitly out of scope. Wire format stays in the [contract](deep-agents-runtime.md). Chinese source of truth: [v1 能力边界](../../zh/reference/deep-agents-runtime-boundary.md). Snapshot date: 2026-09-01.

Confirm three things:

1. The must-have list is accepted.
2. Each leftover is owned by runtime or the control plane.
3. Out-of-scope items stay out of v1.

## Split of responsibility

| Side | Owns | Does not own |
| --- | --- | --- |
| Control plane (`apps/api`) | Sessions, auth, assembled `messages` / `systemPrompt`, event persist and projection, HITL transport, cancel orchestration, Web / TUI | Does not run LangGraph or interpret runtime checkpoint internals |
| Runtime (`services/deepagents-runtime`) | Deep Agents / LangGraph, AG-UI SSE, tools, interrupt / resume, cancel, opaque `checkpointRef` | Does not know DataFoundry metadata, data gateway, SQL audit, artifacts, knowledge, or skills |
| Clients | Render AG-UI events and restored history | Do not call runtime HTTP directly |

Without `RUNTIME_SERVICE_URL`, the API uses an in-process TypeScript stub. `npm run dev` starts the Python runtime on `:8790` and injects the URL.

## v1 must-haves

### Transport

`GET /health`, `POST /runs/stream` (AG-UI SSE), `POST /runs/:runId/cancel`. Optional bearer token. No model keys or database secrets in the run request.

Cancel should stop the graph and finish with `RUN_FINISHED` + `status: "cancelled"`.

### Event identity

AG-UI `toolCallId` **is** the model `tool_calls[].id` (for example `call_xxx`). It is **not** the LangGraph / LangChain execution `run_id`.

- `on_tool_start` binds an existing model id. It must not emit another `TOOL_CALL_START`.
- Same-name parallel calls use FIFO.
- Do not replay `TOOL_CALL_ARGS` after the model already streamed them.
- `TOOL_CALL_RESULT` must include `messageId` and `role: "tool"` (CopilotKit will otherwise leave the card running).
- If the graph ends with open tool ids and there is no interrupt, emit `RUN_ERROR` (`UNFINISHED_TOOL_CALLS:…`). Do not synthesize `TOOL_CALL_END`.
- Do not reopen a text `messageId` that already received `TEXT_MESSAGE_END`.

### Conversation, tools, HITL

Streaming text and multi-turn on the same `threadId` are in scope.

In-scope tools: `write_todos`, `ask_user` (the only HITL tool on `interrupt_on`), and Deep Agents built-in filesystem tools such as `glob` if the SDK exposes them. Those filesystem tools are **not** DataFoundry data tools. The control plane does not govern their paths.

`on_interrupt` uses `type: "agent_interrupt"`. Resume reuses the original `runId`. `response === false` cancels the interrupt. `mastra_suspend` is replay-only; refuse to continue it.

History replay after refresh is a control-plane concern. Runtime only returns an opaque `checkpointRef` via `runtime.bound`.

## Verified on 2026-09-01

Live model + Web + API. Runtime unit tests: 25 passed.

Plain chat; one `write_todos`; three same-name `glob` calls with distinct ids; `ask_user` then resume with 「继续」; follow-up text in the same thread; mid-stream cancel (`canceled`); refresh restore of a completed tool run; Schema/SQL-style prompts do not crash and do not run DataFoundry data tools.

Fake-model coverage: `npm run smoke:deepagents-sdk`.

## Leftovers

| ID | Symptom | Owner | Notes |
| --- | --- | --- | --- |
| L1 | After HITL resume, conversation DTO may still list `pendingInteractions` / `ask_user` as pending | Control plane first | UI and checkpoint already completed. Runtime should still emit `TOOL_CALL_RESULT` for that id on resume |
| L2 | Contract mentions `submit_plan`; runtime does not `interrupt_on` it | Runtime | Mapping `write_todos` → `submit_plan` is translation only |
| L3 | HITL reject / 「停止」 not verified in the browser | Both | Code path exists |
| L4 | Control plane may persist cancel as `terminalEvent: RUN_ERROR` with status `canceled` | Align both | Runtime should send `RUN_FINISHED` + `cancelled` |
| L5 | Reusing `msg_{runId}` after a closed text segment | Runtime | Not hit in the verified tool turns |
| L6 | `./deploy.sh` does not start the Python runtime | Deploy / control plane | `npm run dev` already does |
| L7 | SDK filesystem tools are ungoverned | Runtime to confirm | Live model called `glob`. Keep as generic tools, or disable in v1 |

Do not “fix” leftovers by deduping on tool name, synthesizing `END` before `RUN_FINISHED`, disabling AG-UI verify, or merging same-name running cards in the UI.

## Explicitly out of scope

Not bugs: DataFoundry schema/SQL tools, SQL audit, artifacts, workspace/sandbox metadata, knowledge, skills, MCP, goals/memory. No datasource credentials to the runtime. Old Mastra interrupts cannot resume here.

A user asking to “list tables” may get a refusal, a filesystem no-op, or a text explanation. The control plane does not expect real `inspect_schema` / `run_sql` results.

## Confirmation checklist

**Must:** the three HTTP endpoints; model-id `toolCallId`; RESULT with `messageId` + `role: "tool"`; streaming + multi-turn; `ask_user` interrupt/resume; cancel with `RUN_FINISHED` cancelled; opaque `checkpointRef`; reject `mastra_suspend`; no data-plane secrets.

**Should:** FIFO for same-name tools; `RUN_ERROR` on leftover tool ids; fake-model path; honor control-plane `systemPrompt` and `limits.maxSteps`; written decision on L7.

**Must not:** treat LangGraph `run_id` as a new AG-UI id; synthesize `TOOL_CALL_END` to pass verify; rewrite business `systemPrompt`; require `artifact` / `sql_audit` / `skill.selection` in v1; connect production databases from the runtime.

## Next period (not this boundary)

Control-plane tool gateway for data tools; SQL audit and artifacts; real `submit_plan`; deploy-script runtime; close L1 pending projection.

## See also

- [Contract](deep-agents-runtime.md)
- Chinese detail: [v1 能力边界](../../zh/reference/deep-agents-runtime-boundary.md)
- Implementation: `services/deepagents-runtime/`
