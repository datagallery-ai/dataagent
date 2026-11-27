# Subagents

The manifest's `subagents` list references YAML files. The model delegates to them using the built-in `task` tool. See [general-purpose.yaml](../../../../../examples/plugins/common/subagents/general-purpose.yaml).

## Minimal declaration

```yaml
schema_version: 1
name: general-purpose
description: Handle focused research and file inspection.
system_prompt_file: prompts/general-purpose.md
```

Supply the description inline or in a file and use a plugin-relative `system_prompt_file`. See [subagent declarations](../../../reference/extension-schema.md#subagent-declaration).

## Inheritance

| Field | Omitted | Explicit |
| --- | --- | --- |
| `tools` | Inherit parent tools | Use the specified tools, including `scope: subagents` tools. Short names get the plugin prefix. Names containing `__` resolve as given. Unknown or duplicate names fail compilation |
| `skills` | Inherit compiled root sources | Compile the listed paths. `[]` loads no Skills |
| `hooks` | No hooks | Apply these hooks only to this subagent |

`tools: []` attaches no declared tools. Standard subagents inherit the parent model. Set `model` to a configured alias and `temperature` to override sampling for that subagent. They have their own system prompt with filesystem guidance.

## Compiled subagents

`kind: compiled` points to a Python graph factory, such as [nl2sql](../../../../../dataagent/plugins/nl2sql/subagents/nl2sql.yaml). The compiled graph owns its prompt, model, and tools. See the [schema](../../../reference/extension-schema.md#subagent-declaration).

The signature is `factory(params, services)`. `params` contains `plugins.config.<plugin-id>` without `run_hooks`. `dataagent.SubagentServices` supplies:

- `get_model` resolves a model alias.
- `semantic_client` is optional.
- `outputs_dir` is the session output directory, or absent outside a session. The factory can write files the parent can read.
- `run_hooks` supplies the plugin's [workflow hooks](../../../reference/extension-schema.md#compiled-workflow-hook).

Return a LangChain Runnable or `{"runnable": Runnable, "description": "..."}`. The optional description appends runtime details for the parent, such as a bound data source. Omit credentials, hostnames, and connection paths. Extra keys or other return types fail compilation.

## Execution limits

`dataagent.limits` applies independently to each invocation of the root and each standard subagent. Compiled subagents manage their own budgets. `model_calls_per_agent` and `tool_calls_per_agent` default to 128. Model calls count loop iterations rather than HTTP retries or an entire subtree. The next turn resets the counters. Todo middleware belongs to the root.

If no plugin declares `general-purpose`, the built-in subagent uses parent tools and Skills and receives no other subagent's hooks. Declare that name explicitly to configure its hooks. Standard limits still apply.

AG-UI uses `subagent_visibility="attributed"` so subagent steps appear under the root run rather than becoming user turns. A2A returns a Task and supports waiting or polling for results.

<a name="entry-mode"></a>

## Entry mode

Set `dataagent.entry` to a compiled subagent's name to wrap its Runnable as the entry graph. The Runnable reads `messages` from state and returns messages for the checkpoint. It manages its internal execution. See [graph execution](../../../../development/architecture/execution.md).

```yaml
dataagent:
  entry: nl2sql
```

The name matches `[a-z][a-z0-9_-]{0,63}` and must name an enabled compiled subagent. An unknown name lists available entries.

- Entry mode still compiles or reads declarations, but does not assemble root hooks, model/tool call limits, input workspaces, Skills, tools including MCP, plugin prompts, other subagents, or root middleware into the entry wrapper. `limits.timeout_seconds` still bounds host execution. See [run limits](../../../reference/configuration.md#dataagent).
- Use separate `thread_id` values for the normal root and the compiled entry because checkpoint shapes differ.
- Use `ainvoke` or `astream`. Async-only Runnables such as nl2sql may fail with synchronous calls.
- Entry names are checked before factories. MCP discovery and plugin factories still run for valid entries and can fail assembly. REST discovers MCP before marking the conversation engine ready.
- A bad entry name does not fail `/healthz`; `entry` shows the configured value, or `null`. Initial graph assembly returns HTTP 409 for a bad entry.
- Restart after changing the entry. Extension refresh rereads plugin and hook declarations while retaining the selected entry.
