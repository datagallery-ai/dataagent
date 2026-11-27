# AG-UI events and run contract

Web, TUI, and integrations submit turns via `POST /api/v1/runs`. See [REST API](rest-api.md) for routes and HTTP errors and [AG-UI integration](agent-runtime.md) for a runnable example.

<a name="request"></a>

## Request

Send JSON. A normal turn contains one new message. The server loads history for `threadId` from its recorded checkpoint or, if absent, the latest graph state on that thread.

| Field | Constraint |
| --- | --- |
| `threadId`, `runId` | 1–128 characters, starting with an ASCII letter or digit. Later characters may include `_` and `-` |
| `messages` | Exactly one message with `id`, `role: "user"`, and nonblank text `content` for a normal turn. Use `[]` when answering a human feedback prompt |
| `state` | Omit or send `null` or `{}` |
| `tools`, `context`, `forwardedProps` | Required. Send `[]`, `[]`, and `{}`, respectively |
| `answers` | A nonempty map from `interruptId` to answer text when [resuming human feedback](#hitl) |
| `resume`, `parentRunId` | Omit. Use the same thread to continue or `answers` to resume an interrupt |

Unknown top-level fields and client-supplied state, tools, or context are rejected. Subagent messages cannot be submitted as user messages. The Server selects the model, plugins, and workspaces.

Only one turn may be active per session. Active run IDs are process-wide, including across threads. The last run ID on a session and message IDs from loaded history cannot be reused to edit or replay. Use new run and message IDs.

<a name="sse-response"></a>

## SSE response

An accepted run returns HTTP 200 and `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`. Read JSON from SSE `data:` frames. Failures after opening the stream use `RUN_ERROR`. Earlier failures return the [REST error response](rest-api.md#http-errors).

<a name="completed"></a>

## Successful completion

1. `RUN_STARTED` with `threadId` and `runId`.
2. Intermediate adapter events, such as text and tool calls.
3. `CUSTOM` with `name="human_feedback"` and `value=[]`, clearing resolved interactions.
4. `MESSAGES_SNAPSHOT` with the final graph messages.
5. `RUN_FINISHED` with `metadata.status="completed"` and server-monotonic elapsed time in `metadata.durationMs`.

The server checks for pending graph `next` steps. Human feedback pauses end the HTTP stream as below. Other incomplete steps end with `RUN_ERROR`.

<a name="hitl"></a>

## Wait for human feedback

A pause ends the stream with `RUN_FINISHED` and `metadata.status="waiting_for_input"`. A `CUSTOM name="human_feedback"` event before the final snapshot carries the complete list of pending interactions:

```json
{"type":"CUSTOM","name":"human_feedback","value":[
  {"type":"human_feedback","interruptId":"<id>","message":"Choose an identity field.","options":["user_id","device_id"]}
]}
```

`options` are suggestions. Users can also type an answer. Invalid answers may carry `validationError`. Replace the client list with each event's array. Submit a nonempty `answers` map to the same endpoint with the same thread, a fresh run ID, and empty `messages`.

```json
{
  "threadId": "thread-001",
  "runId": "run-002",
  "messages": [],
  "answers": {"<id>": "user_id"},
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
```

Replace `<id>` with the received `interruptId`. Parallel interactions can be answered in batches. Sending a new user message while waiting or using an unknown or resolved ID returns 409. Blank answers pause again with `validationError`.

Reload pending items from `GET /api/v1/sessions/{threadId}` and its `pendingInteractions` after refresh or restart. No HTTP stream runs while the user decides, so this wait does not count against turn timeout. Interactions started by [A2A](a2a.md#a2a-hitl) must resume via A2A.

<a name="errors"></a>

## Errors

`RUN_ERROR.code` uses these values. The human-readable `message` is redacted and limited to 1,500 characters. Integrations should branch on the code. Server logs also include the exception class as `error=`.

| Code | Meaning |
| --- | --- |
| `TIMEOUT` | Turn limit `dataagent.limits.timeout_seconds` or model request timeout |
| `CANCELLED` | Explicit `POST /api/v1/runs/{runId}/cancel` |
| `LOOP_DETECTED` | Loop detection stopped the turn |
| `LIMIT_EXCEEDED` | `model_calls_per_agent` or `tool_calls_per_agent` exceeded |
| `INCOMPLETE_STREAM` | Missing or duplicate terminal event, or output after termination |
| `CONFIG_ERROR` | Invalid configuration |
| `MODEL_ERROR` | Error from the model service |
| `TOOL_ERROR` | Tool or remote agent failed |
| `INTERNAL_ERROR` | Other failure |

The server sanitizes upstream `RUN_ERROR` messages, marks the session `error`, and maps unknown codes to `INTERNAL_ERROR`. Missing terminal events, timeouts, and other execution errors also become `RUN_ERROR`. The turn limit includes retries, backoff, and tools.

<a name="terminal-events"></a>

## Handle termination

| Terminal result | Session status | Client handling |
| --- | --- | --- |
| `RUN_FINISHED` with `completed` | `completed` with a new checkpoint | Use the final `MESSAGES_SNAPSHOT`. You can show `durationMs` |
| `RUN_FINISHED` with `waiting_for_input` | Paused at the saved checkpoint | Display pending prompts and resume with answers |
| `RUN_ERROR` | `error`. If messages exist, the index points to the checkpoint reached | Keep visible partial output and the error code. Use a new run ID on the same thread |
| `RUN_ERROR` with `CANCELLED` | `interrupted` | Show as stopped. Continue from the saved checkpoint or latest thread state |
| Connection closes without a terminal event | Unfinished session becomes `interrupted` | Reconnect and read session details before submitting another message |

TUI summaries such as `Interrupted — continue this conversation` are not written into checkpoints.

<a name="stream-scope"></a>

## Stream scope

Subagent events are attributed to the root run.

| Event | Use |
| --- | --- |
| `SUBAGENT_STARTED` | `subagentRunId` identifies the subagent and `parentToolCallId` its outer `task`. Nested runs add `parentSubagentRunId` |
| `STEP_STARTED` and `STEP_FINISHED` | Show graph-node progress through `subagentRunId` and `stepName` |
| `SUBAGENT_FINISHED` and `SUBAGENT_ERROR` | End subagent progress. The root and outer tool can still be running |

Root text, tool, reasoning, and step events and adapter/tool `CUSTOM` events pass through. Ignore unknown custom events. The server drops upstream `RUN_STARTED`, `MESSAGES_SNAPSHOT`, and `STATE_SNAPSHOT`, emits a single start, and supplies the final message snapshot on completion or pause.

Subagent text, internal tools, and state do not enter the parent conversation stream. Errors are redacted. Fetch files through the [output endpoints](rest-api.md#outputs). `after_model` runs after model calls rather than per token.

Python supplies the subagent events above. `@ag-ui/core 0.0.57` lacks its lifecycle types, so TUI handles them at the parser boundary. Before CopilotKit validates browser events, Web wraps `SUBAGENT_*` into a `CUSTOM` event named `subagent` and qualifies the step name by run ID while displaying the original name. Step events keep the SDK schema. When subagent events arrive before `TOOL_CALL_START`, clients correlate by parent tool ID.

TUI shows the active subagent, stage, and elapsed time. `Ctrl+O` opens received steps, reruns, and error reasons. Web shows delegation and wait time on the tool chip, progress and redacted errors on the progress line, and a waiting indicator after 30 seconds without progress. The task console lists step timings. Client-monotonic elapsed time and waiting indicators do not establish server health or failure. Progress history is in-memory for the current session and is not replayed from checkpoints. The parent tool result remains available after refresh.
