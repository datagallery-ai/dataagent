# HITLHandler and HITLMiddleware

`dataagent/extensions/hitl.py` supplies the handler, middleware, and interaction types. The handler defines decisions and domain behavior. Middleware prepares an interaction, checkpoints it, interrupts, collects answers, and resumes. Use async `ainvoke` or `astream`.

| Phase | Timing |
| --- | --- |
| `before_agent` | Once per invocation before the first model call |
| `before_model` | Before each model call |
| `after_model` | After each model response, before planned tools |
| `before_tool` | Before an individual tool, including `task` |
| `after_tool` | After tool return, before committing to the parent graph |
| `after_agent` | On the normal return path |

Replanning from `after_agent` can reenter the model loop and reach review again. The plugin's `.plugin.yaml` installs its `hitl` on the parent automatically. Explicit Python assembly is also available. Guard arguments come from manifest policy params. Automatic answering and interaction timeouts are not implemented.

Web displays the question, choices, and text input. TUI treats the next manual input as the answer while queued requests wait. Reload Web or use `/resume <threadId>` in TUI to recover a prompt. Checkpoints hold policy internals. See the [AG-UI feedback contract](../../../reference/events.md#hitl).

## Enable and supply instructions

```yaml
hitl:
  enabled: true
  instructions: prompts/hitl.md
  policies: []
```

See [configuration and defaults](../../../reference/extension-schema.md#hitl-configuration). Each enabled source may have a nonempty plugin-relative Markdown `instructions` file, injected into the parent's system prompt. Missing or escaping paths fail. Disabling a source skips its handler and instructions. Other enabled sources still work. Policies also work without instructions. All sources share the proactive feedback tool:

```python
request_human_feedback(
    question="Which identifier should be used to deduplicate active users?",
    reason="The request did not specify the identity field.",
    options=["user_id", "device_id"],
)
```

The tool pauses for nonblank text and returns the answer as its success ToolMessage. It does not confirm or modify IR. If other tool calls are in that reply, only the first feedback call is kept. After feedback the model replans without triggering the same turn's deterministic `after_model` guard or nested tool-stage HITL. See [de-agent instructions](../../../../../dataagent/plugins/de-agent/prompts/hitl.md) for question, reason, and choice guidance.

## Handler methods

| Method | Returns | Default |
| --- | --- | --- |
| `trigger_policy(context, *, params)` | `TriggerDecision` | Trigger for every matching call |
| `interaction_builder(context, *, params)` | `InteractionRequest` | Use configured question/options or generate a general question |
| `resume_handler(context, *, params)` | `ResumeDecision` | Send the answer back to the model without changing business state |

Use `def` or `async def`:

```python
from dataagent import HITLHandler, InteractionRequest

class ClarificationHandler(HITLHandler):
    def interaction_builder(self, context, *, params):
        return InteractionRequest(
            question="Which identifier should be used to deduplicate active users?",
            options=["user_id", "device_id"],
        )
```

`context` exposes the phase `event`, tool call, full policy ID, detached `state`, native runtime, trigger decision, interaction, and answer. The last two are available only at their relevant stages. Agent and model phases have no tool call. `after_tool` adds native `tool_result`, and its state preview includes the result and business updates. Read services from native `runtime.context` and use detached `context.state` for business state. Return updates explicitly. Handlers can run concurrently, so do not store interaction-specific mutable data on the instance.

## Manifest and explicit assembly

```yaml
hitl:
  policies:
  - id: clarification
    version: '1'
    at: before_tool
    matcher: task
    handler: hitl/clarification.py:ClarificationHandler
    params: {}
```

`create_agent` compiles enabled plugins automatically. For a custom Deep Agents graph, read the same manifest and supply middleware explicitly. The caller provides model, tools, and subagents.

```python
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from dataagent import read_yaml
from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import HITLMiddleware

path = Path("/path/to/plugin/.plugin.yaml")
section = read_yaml(path)["hitl"]
middleware = HITLMiddleware.from_config(section, root=path.parent, namespace="main")
graph = create_deep_agent(
    model=model,
    tools=tools,
    subagents=subagents,
    middleware=[middleware],
    checkpointer=InMemorySaver(),
)
```

A handler is a root-relative `file.py:ClassName` with a parameterless `HITLHandler` subclass. Omit it for the base handler. You can set a fixed question and options in `params`. To target a specific subagent, match `task` and check `subagent_type` in `trigger_policy`. See [matcher rules](../../../reference/extension-schema.md#hitl-configuration).

Full IDs such as `main/clarification` combine namespace and policy ID and must be unique in one middleware. Plugin compilation uses plugin IDs as namespaces.

## Answer and resume

```python
from langgraph.types import Command

config = {"configurable": {"thread_id": "ir-example"}}
result = await graph.ainvoke({"messages": [("user", "Generate SQL for active user statistics")]}, config)
pending = result["__interrupt__"][0]
print(pending.value["message"])
result = await graph.ainvoke(Command(resume={pending.id: "user_id"}), config)
```

The middleware accepts nonblank text. Empty or structured answers pause again with `validation_error`. Options are display hints; validate domain choices in `resume_handler` and use `replan` if needed.

| Phase | `continue` | `replan` | `reject` |
| --- | --- | --- | --- |
| before_agent / before_model | Pass feedback into the model flow | Pass feedback forward without looping | Follow the ending path with the reason |
| after_model | Keep the current plan | Skip planned tools and return to the model | Skip planned tools and follow the ending path |
| before_tool | Execute once all policies continue | Skip and give feedback to the model | Decline the call and give the reason to the model |
| after_tool | Keep the actual result | Keep the actual result and give feedback to the model | Reject the result without undoing the call |
| after_agent | Finish normally | Return to the model loop with feedback | Finish with the rejection reason |

Tool rejection does not end the session. Enforce lasting business restrictions in the trigger or during execution. State stages on the ending path still pass native `after_agent`. Skipped plans get error ToolMessages so tool calls are not orphaned. Before-tool arguments stay fixed. `return_direct` tools return directly on continue and go back to the model on replan or reject. The default trigger may ask again after replanning. Override it for a one-time question or when known IR values should permit execution.

To commit a business update, return it explicitly:

```python
return ResumeDecision(
    action="continue",
    state_update={"ir": confirmed_ir},
)
```

Declare fields in the state schema. Before-tool updates are visible in `ToolRuntime.state` and are committed after the tool and its after-tool interaction. Explicit updates also commit on replan or reject. A raised handler exception does not commit detached mutations. Merge order is before-tool, tool, then after-tool. Ordinary fields use the last update, and reducer fields receive deltas in order. Previews follow the same rules. For multiple deltas to a reducer field, native Commands preserve update order and exactly one tool result.

## IR subagent and recovery

### Completion guards

de-agent's manifest registers two `after_model` handlers in `hitl/completion_guards.py`, sharing private checkpoint state. `TextQuestionGuard` detects a question in the final text's last line and uses up to 2,000 characters of the full text, at most twice per user turn. `HumanFeedbackGuard` asks for final confirmation once per turn and takes optional conditions as a string or list. Remove the policy to disable that guard or set `hitl.enabled: false` to disable all plugin HITL. The declarations alone enable these guards.

```yaml
hitl:
  enabled: true
  instructions: prompts/hitl.md
  policies:
  - id: text_question_guard
    at: after_model
    handler: hitl/completion_guards.py:TextQuestionGuard
    params:
      text_question_max_fires: 2
```

The caller consumes interrupts and answers. Completion guards skip messages with valid or invalid tool calls, then replan after the answer. Their counts update on resume without duplicate replay. Middleware feedback is not a new user turn. The IR collector deduplicates raw answers by stable IDs.

See [IRQuestionGuard](../../../../../dataagent/plugins/de-agent/hitl/ir_question_guard.py) and [manifest policies](../../../../../dataagent/plugins/de-agent/.plugin.yaml). The policy runs when declared and IR is enabled through `data_task_ir.enabled`. Policy parameters belong in the manifest. IR business settings such as `data_task_ir.enabled` and model settings stay in `config.yaml`.

| Policy | Parameter | Default |
| --- | --- | --- |
| `text_question_guard` | `text_question_max_fires` | 2 |
| `ir_question_guard` | `budget` | 8 |
| `ir_question_guard` | `hitl_question_guard_max_fires` | 2 |
| `ir_question_guard` | `hitl_max_same_fingerprint_asks` | 2 |
| `ir_question_guard` | `hitl_max_same_field_asks` | 2 |
| `ir_question_guard` | `hitl_blocking_fields` | identity, sources, time_context, output_features |
| `ir_question_guard` | `consumerTools` | `de-agent__check_and_repair_sql`, plus recognition of nl2sql delegation |
| `human_feedback_guard` | `conditions` | `[]` |

The compiler merges plugin policies into one parent middleware with separate namespaces and merges handler `state_schema` fields, including `IRQuestionState`. The default after-model policy checks unresolved critical fields and questions before consumer calls or completion. It pauses the whole plan to avoid duplicate parallel prompts. Hosts with serial consumers can use before-tool. Answers are stored per task as `ir_clarification` and deduplicated by the IR collector. Unresolved fields after the answer proceed confirmed-only with documented gaps. The budget resets per task and applies to IR rather than all policies.

- At each phase, matching policies freeze questions, params, and versions in declaration order. All must continue. Later handlers see earlier state updates.
- A durable prepare task checkpoints prompts. SQLite resume does not rerun preparation. Removing a policy, moving its phase, or changing its incompatible version rejects resume. Handlers may replay, so keep them side-effect-free or idempotent.
- Only the targeted tool pauses at tool stages. Other parallel tools can run. Respond to multiple interrupts by ID.
- Private `hitl_history` merges by interaction ID and is available through `graph.aget_state(config)`. External answers may come from a client rather than a person.
- A durable execution task caches finished results. After-tool resume does not repeat a completed call. An in-flight crash can retry external effects, so ensure business idempotency and define concurrency reducers.
- Supported results include ordinary tool results and `Command(update=dict)` for the current graph. Parent graph commands, command lists as tool outputs, and automatic argument rewriting are unsupported.

```bash
uv run pytest tests/test_hitl.py tests/test_hitl_stages.py -q
```
