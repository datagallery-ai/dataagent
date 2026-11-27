# HITLHandler and HITLMiddleware

Use human feedback when a model needs a clarification or when a policy needs to pause execution at a specific phase. `HITLHandler` decides when to ask, what to ask, and how to use the answer. `HITLMiddleware` saves the pending interaction, interrupts the graph, and resumes it with an answer. Run the graph with async `ainvoke` or `astream`.

This guide covers the shared feedback mechanism. See [de-agent human feedback](de-agent-hitl.md) for the built-in SQL and IR policies.

<a name="enable-and-supply-instructions"></a>

## Enable and supply instructions

Declare `hitl` in the plugin's `.plugin.yaml` to enable feedback on the default root Agent.

```yaml
hitl:
  enabled: true
  instructions: prompts/hitl.md
  policies: []
```

The plugin compiler reads a nonempty Markdown file relative to the plugin root and adds its contents to the root system prompt. A blank, missing, or out-of-root file fails assembly. Policies can run without instructions. Disabling a plugin's HITL skips its instructions and handlers while leaving other enabled sources active. See [HITL configuration and defaults](../../../reference/extension-schema.md#hitl-configuration).

Enabled plugins share the `request_human_feedback` tool. The arguments below ask the user to choose a deduplication key.

```json
{
  "question": "Which identifier should be used to deduplicate active users?",
  "reason": "The request did not specify the identity field.",
  "options": ["user_id", "device_id"]
}
```

The tool waits for nonblank text and returns the raw answer as a successful `ToolMessage`. If the model includes other tool calls in that response, only the first feedback call runs. After the answer, the model replans. That response bypasses deterministic `after_model` policies, and the feedback tool bypasses `before_tool` and `after_tool` policies on its own call.

<a name="handler-methods"></a>

## Handler methods

| Method | Returns | Default |
| --- | --- | --- |
| `trigger_policy(context, *, params)` | `TriggerDecision` | Ask on every matching phase or tool call |
| `interaction_builder(context, *, params)` | `InteractionRequest` | Use `params.question` and `params.options`, with a generic question as fallback |
| `resume_handler(context, *, params)` | `ResumeDecision` | Replan using the answer as model feedback without updating business state |

Override the methods you need with `def` or `async def`. Save this example in the plugin's `hitl/clarification.py` to supply a fixed clarification question.

```python
from dataagent import HITLHandler, InteractionRequest


class ClarificationHandler(HITLHandler):
    def interaction_builder(self, context, *, params):
        return InteractionRequest(
            question="Which identifier should be used to deduplicate active users?",
            options=["user_id", "device_id"],
        )
```

| Phase | Timing |
| --- | --- |
| `before_agent` | Once per invocation before the first model call |
| `before_model` | Before each model call |
| `after_model` | After a model response, before planned tools |
| `before_tool` | Before an individual tool, including `task` |
| `after_tool` | After the tool returns, before the parent graph commits its result |
| `after_agent` | On the ending path. Replanning can enter the model loop and reach review again |

`HITLContext` provides the phase (`event`), full `policy_id`, detached `state`, and native `runtime`. Read host services from `runtime.context`. Tool phases include `tool_call`. At `after_tool`, `tool_result` holds the native result and the state preview includes the tool's business updates. The builder receives the trigger's `decision`. The resume handler also receives the `interaction` and `answer`. Treat handler instances as shared and return state updates explicitly.

<a name="manifest-and-explicit-assembly"></a>

## Manifest and explicit assembly

The plugin manifest registers the class and sets its execution phase.

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

The handler path is relative to the plugin root and refers to a parameterless `HITLHandler` subclass. Omit `handler` to use the base handler. To target a specific subagent, set `matcher: task` and check `subagent_type` in `trigger_policy`. The compiler uses the plugin ID to namespace policy IDs. Each full ID, such as `my-plugin/clarification`, must be unique.

DataAgent assembles enabled plugin policies into one middleware on the default root graph. With a custom Deep Agents graph, supply the model, tools, subagents, and checkpointer. The example uses an in-memory checkpointer.

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

For explicit assembly, pass your prompt to the graph yourself. The plugin compiler is responsible for reading the manifest's `instructions` file during standard assembly. Use a persistent checkpointer if the graph must resume after the process exits.

<a name="answer-and-resume"></a>

## Answer and resume

- **Web.** Enter text or choose a displayed option. Reload the session to recover a pending prompt.
- **TUI.** The next manually entered input answers the pending prompt while queued inputs wait. Use `/resume <threadId>` to reopen the session.
- **HTTP and A2A.** Submit answers to the original entry point with the pending interrupt IDs. Follow the [AG-UI feedback contract](../../../reference/events.md#hitl) or the [A2A feedback contract](../../../reference/a2a.md#a2a-hitl) for the request format and each host's restart behavior.
- **Embedded graph.** Read the interrupt and send a `Command(resume=...)` on the same thread.

```python
from langgraph.types import Command

config = {"configurable": {"thread_id": "clarification-example"}}
result = await graph.ainvoke(
    {"messages": [("user", "Delegate an active user summary to a subagent")]},
    config,
)
for pending in result.get("__interrupt__", ()):
    print(pending.value["message"])
# Submit this after receiving the answer. Use the matching ID if several prompts are pending.
if result.get("__interrupt__"):
    pending = result["__interrupt__"][0]
    result = await graph.ainvoke(Command(resume={pending.id: "user_id"}), config)
```

Send nonblank text. Blank or structured answers cause another pause with `validation_error`. Options are suggestions. Use `resume_handler` to validate domain-specific answers. Several parallel tools may pause independently, so match each answer to its interrupt ID. The middleware waits for caller input and has no automatic answer or interaction timeout.

<a name="resume-actions"></a>

### Resume actions and state updates

| Phase | `continue` | `replan` | `reject` |
| --- | --- | --- | --- |
| before_agent / before_model | Pass feedback into the model flow | Pass feedback forward without looping | Follow the ending path with the reason |
| after_model | Keep the current plan | Skip planned tools and return to the model | Skip planned tools and follow the ending path |
| before_tool | Execute once all matching policies continue | Skip the call and give feedback to the model | Decline the call and give the reason to the model |
| after_tool | Keep the actual result | Keep the actual result and give feedback to the model | Report rejection to the model after the call has run |
| after_agent | Finish normally | Return to the model loop with feedback | Finish with the rejection reason |

Tool rejection leaves the conversation usable. Put any continuing business restriction in the trigger or the execution check. State stages on the ending path still pass native `after_agent`. Skipped plans receive error `ToolMessage`s for their tool call IDs. Before-tool arguments are fixed. A `return_direct` tool returns directly on `continue` and goes back to the model on `replan` or `reject`. A policy's default trigger can ask again after replanning. Override it to implement a one-time question.

To commit a business state change, return it in `ResumeDecision`. This example stores the answer as `selected_key`.

```python
return ResumeDecision(
    action="continue",
    state_update={"selected_key": context.answer},
)
```

Declare the field in a state schema. The middleware combines compatible handler `state_schema` fields. Before-tool updates are visible in the tool's `ToolRuntime.state`. When the tool runs, changes commit in before-tool, tool, and after-tool order, using the field's reducer when present. An explicit update also commits when the policy returns `replan` or `reject`. Editing the detached state directly cannot commit a change.

<a name="recovery"></a>

### Checkpoint and recovery

Pending policy IDs, versions, phases, questions, and parameters are saved for recovery. Keep the same ID, phase, and version while an interaction is waiting. A completed tool result is reused when resuming after the tool. Handler code can replay, and an interrupted tool may need to run again, so make external effects idempotent. For the durable prepare and execute tasks, state reducers, and replay tests, see [HITL execution and recovery](../../../../development/architecture/execution.md#hitl-recovery).

<a id="ir-subagent-and-recovery" name="ir-subagent-and-recovery"></a>
<a id="completion-guards" name="completion-guards"></a>

The former IR and recovery section links to the [de-agent policies](de-agent-hitl.md#completion-guards) and the [shared recovery contract](#recovery).
