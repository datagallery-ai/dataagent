# Hooks

A hook is a Python function attached to one of six lifecycle points. Runnable examples are [check_input.py](../../../../../examples/hooks/check_input.py) and [check_tool.py](../../../../../examples/hooks/check_tool.py).

## Register in configuration

Use the selected product file. Without `--config` this is Home's `config.yaml`.

```yaml
schema_version: 4
dataagent:
  hooks:
    before_model:
    - entrypoint: hooks/check_input.py:handle
      params:
        max_chars: 4000
    before_tool:
    - entrypoint: hooks/check_tool.py:handle
      matcher: common__summarize_numbers
```

Placing a file in `hooks/` does not register it. See [hook declarations](../../../reference/extension-schema.md#agent-hooks) for grouped and flat forms. `params` is copied for every invocation.

## Signatures { #hook-signatures }

| Event | Signature | Return value |
| --- | --- | --- |
| `before_agent`, `after_agent`, `before_model`, `after_model` | `handle(state, runtime, *, params)` | `None`, a state update dict, or LangGraph `Command` |
| `before_tool` | `handle(request, *, params)` | `None`, replacement `ToolCallRequest`, or direct `ToolMessage` or `Command` |
| `after_tool` | `handle(request, result, *, params)` | `None`, native `ToolMessage`, or `Command` |

Declare keyword-only `params`. Plain `def` and `async def` are supported. Use functions rather than generators or decorated middleware objects.

State hooks read `state["messages"]` and `runtime.context`. Return updates for the native reducer. Tool hooks access models, the semantic client, and plugin configuration through `request.runtime.context`. `None` keeps the original object. A replacement request preserves its tool call ID. A direct before-tool result skips tool execution. A replacement result must contain a ToolMessage with the corresponding ID. When replacing a Command, preserve other needed updates, for example with `dataclasses.replace(result, update=...)`.

A standard subagent's state hook can declare targets with `@hook_config(can_jump_to=[...])` and return `jump_to`. Use `"end"` to finish or `"model"` to go back to that subagent's model. The `task` tool hands the result to the parent. Root configuration hooks keep their default routing.

Use `ainvoke` or `astream` for async hooks. A synchronous function running in a thread cannot be forcibly stopped by async cancellation.

## Order and errors

Plugin manifest hooks compile before product `dataagent.hooks`, in declaration order and without deduplication. `before_*` runs forward and `after_*` in reverse.

| Event | Timing |
| --- | --- |
| `before_agent` | At the start of the invocation |
| `after_agent` | On normal completion, excluding failures and cancellation |
| `before_model` | Before each model call |
| `after_model` | After the model returns |
| `before_tool` | Before each tool invocation |
| `after_tool` | After a normal return, including error ToolMessages, but not a raised exception |

Exceptions propagate without retries or per-hook timeouts. REST's whole-turn `timeout_seconds` covers the stream.

## Reject execution

Return `ToolMessage(content=..., tool_call_id=request.tool_call["id"], status="error")` from `before_tool` to skip that tool and let the model correct the input. Raise from a hook to end the turn. HTTP clients receive `RUN_ERROR`, usually with `INTERNAL_ERROR`. Hook errors are outside tool error handling.

Root hooks apply to the root. Subagents declare their own `hooks`, and model/tool limits apply independently to each declared agent.

## Compiled workflow hooks

Compiled subagents can use `services.run_hooks` in their factory. These are separate from agent lifecycle hooks. For example, provide the following defaults for the built-in NL2SQL workflow:

```yaml
plugins:
  config:
    nl2sql:
      run_hooks:
        before_run:
        - entrypoint: hooks/nl2sql.py:prepare_input
        after_run:
        - entrypoint: hooks/nl2sql.py:finish_output
```

Handlers access host services and session paths through `runtime.context`. See [workflow hook signatures and replacement rules](../../../reference/extension-schema.md#compiled-workflow-hook) and the [de-agent example](../../../../../dataagent/plugins/de-agent/README.md).
