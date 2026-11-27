# Python API

Import `dataagent` after installation. Read `dataagent.__version__` for the package version. Model and graph modules load lazily. See the [SDK guide](../guides/integrate/sdk.md) for examples.

| Layer | Module | Audience and stability |
| --- | --- | --- |
| Public SDK | `dataagent` | Python callers and extension authors. Public contract |
| Host API | `dataagent.host` | Persistent services such as `dataagent serve`. Host-facing public contract |
| Low-level API | `dataagent.lowlevel` | Native LangGraph callers. May change with LangGraph |

Exports are defined by `__all__` in [dataagent](../../../dataagent/__init__.py), [host](../../../dataagent/host.py), and [lowlevel](../../../dataagent/lowlevel.py).

## Public SDK { #public-sdk }

| Name | Purpose |
| --- | --- |
| `LaunchOptions` | Frozen startup inputs |
| `prepare_runtime` | Validate settings and return `PreparedRuntime` without creating an agent |
| `PreparedRuntime` | Read-only settings, resolved paths, and extension locations |
| `create_agent` | Construct an `Agent` from a prepared runtime |
| `Agent` | Returned agent type, useful for annotations |
| `load_mcp_tools` | Load configured tools asynchronously. No configuration yields an empty list. See [MCP](../guides/integrate/extensions/mcp.md) |
| `AgentContext` | Tool context with models, semantic client, and plugin settings |
| `SemanticServiceClient` | Semantic HTTP client |
| `collect_model_stream(model, messages, **kwargs)` | Stream and collect a complete model message, retaining tool calls, reasoning, and usage |
| `SubagentServices` | Compiled factory services `get_model`, `semantic_client`, `outputs_dir`, and `run_hooks` |
| `ConfigurationLoadError` | Missing, malformed, or invalid YAML. A `ValueError` |
| `DataAgentError` | Diagnostic base with `source`, `component`, `fact`, and `trace_id`. Includes configuration, loop, A2A, and semantic client errors. Subclasses retain their built-in error bases |
| `LoopDetectedError` | Terminated turn with `kind` identifying repeated text or tool loops |
| `safe_error` | Redacted `{"code", "message"}` with terminal escapes removed and a 1,500-character message limit. See [events](events.md) |
| `read_yaml` | Read strict YAML from a path |
| `HITLHandler`, `HITLContext`, `HITLState`, `InteractionRequest`, `TriggerDecision`, `ResumeDecision` | [Human feedback extension types](../guides/integrate/extensions/hitl.md) |
| `MCPConfig`, `MCPInfrastructureError` | SQL-over-MCP connection type and infrastructure error. See [MCP SQL](../guides/integrate/extensions/mcp.md#nl2sql-mcp) |
| `build_sql_service`, `check_engine_driver`, `check_read_only_sql`, `check_sql` | Data gateway services and validation |
| `recall_database`, `RecallOptions`, `RecallError` | Async SQLite exploration and its options and errors |

### `LaunchOptions` { #launch-options }

| Field | Default | Meaning |
| --- | --- | --- |
| `config` | `None` | Product YAML, default Home `config.yaml` |
| `env_file` | `None` | Overrides Home `.env` |
| `user_id` | `"default"` | Local user for `runtime/users/<user>/` |
| `cwd` | `Path.cwd()` | Base for relative paths |
| `init_home` | `False` | True creates missing templates |
| `session_id` | `None` | Verify an existing session belongs to this user. Invocations still select their session through `configurable.thread_id` |

YAML `dataagent.input_workspaces` provides defaults. Hosts can override at creation. See [host API](#host-api) and [environment precedence](cli-and-environment.md#environment-variables).

### `create_agent` { #create-agent }

```text
create_agent(runtime, *, model=None, checkpointer=None, session=None,
             input_workspaces=None, mcp_tools=None, extension_revision=None,
             semantic_client=None)
```

| Argument | Meaning |
| --- | --- |
| `checkpointer` | LangGraph checkpointer. Supply a persistent implementation for cross-process history |
| `mcp_tools` | Required when configured, from `await load_mcp_tools(runtime)` |
| `semantic_client` | Optional host-owned client. If omitted and configured, the agent creates and owns its client |
| `model` | Default model override, mainly for tests |

Creation checks entry, plugin declarations, MCP, hooks, and semantic client. Preflight reads declarations and instruction files. Plugin Python loads at graph build. Standard SDK use builds on first session use. An explicit host `session` is opened and built during creation. Unknown keywords cause `TypeError`. See [host arguments](#host-api).

### `Agent` { #agent }

| Member | Behavior |
| --- | --- |
| `invoke`, `ainvoke`, `stream`, `astream`, `astream_events` | LangGraph arguments plus `output_dir`. Select by `config["configurable"]["thread_id"]`, default `sdk` or the explicit session. Create on first use |
| `close`, `aclose` | Close agent-owned semantic clients |
| Context managers | `with` closes synchronously; `async with` awaits close |

Different threads can run concurrently. Overlapping calls on one thread fail. `stream()` validates the path during preparation. Sync and async iteration hold the session until completion, failure, or close. See [close streams early](../guides/integrate/sdk.md#close-stream). The first call binds `output_dir`; subsequent calls reuse it. See [sessions and outputs](../guides/sessions-and-outputs.md).

Each session caches its own graph and context. Models are shared within the agent. The graph cache targets 32 entries and evicts the least recently used idle graphs. Active graphs can temporarily push the count above 32. The checkpointer owns history and each graph records turn traces.

### `AgentContext` { #agent-context }

Access through `runtime.context`.

| Member | Meaning |
| --- | --- |
| `get_model(name=None)` | Configured model, default `dataagent.model`; unknown aliases raise `ValueError` |
| `semantic_client` | Configured client or `None` |
| `session.outputs` | Current session output directory |
| `home` | Resolved Home for shared resources |
| `get_plugin_config(plugin_id)` | Copy of effective plugin config. Disabled plugins raise `ValueError` |
| `get_plugin_state(plugin_id)` | Private in-memory dictionary for an enabled plugin within the session graph. Not checkpointed; plugin owns concurrency |

## Host API { #host-api }

For a long-lived service exposing an agent:

| Name | Purpose |
| --- | --- |
| `refresh_extensions(runtime)` | Reread plugin, Hook, and MCP declarations into a new prepared runtime, keeping the model, paths, and startup policy. Swap after successful compilation. In-flight requests retain their graphs |
| `extension_revision(runtime)` | SHA-256 source fingerprint, excluding business inputs and session state |
| `server_listen(options=None)` | Read YAML `server`, expand variables using normal runtime precedence, and return `(host, port)`. Default `127.0.0.1:8790` without a selected file. Raise on selected file load, expansion, or validation errors. Usable even if preparation fails |
| `error_code(error)` | Map an exception to a fixed code |
| `configure_logging(runtime=None)` | Configure `backend.log`, including early errors |
| `register_log_secrets(values)` | Add redactions after extension or credential refresh and retain old ones |
| `diagnostic_traceback(error, secrets=())` | Full sanitized stack for logs |
| `log_context` | Bind correlation fields via ContextVar and reset on exit |
| `OPAQUE_ID_PATTERN` | `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` |
| `ERROR_CODES` | Valid [AG-UI error codes](events.md#errors) for safe errors and exception overrides |
| `ServerSettings` | Bind-address and port validation |
| `opaque_id(value, label)` | Validate and return an ID |
| `log_event(...)`, `safe_interrupt_fields(...)` | Structured logging and recursive feedback redaction |

Host use also includes:

- `create_agent(..., session=, input_workspaces=, extension_revision=)`. The default `session` must be its normal path under the current Home and user. With `input_workspaces=None`, new sessions use YAML defaults and existing ones retain bindings. An explicit tuple, including `()`, must match the existing path order. Names are ignored for that comparison. The revision updates the Skill cache.
- `runtime.paths.open_session(thread_id, *, create, output_dir=None, input_workspaces=None)` registers or resumes, returning `(SessionPaths, workspaces)`. A missing session with `create=False` yields `None`. See [directory rules](../guides/sessions-and-outputs.md).
- `agent.as_agui(name="dataagent", **kwargs)` creates an adapter whose `run(input)` selects by `input.thread_id`. Use a new adapter per request.
- `agent.graph_for(thread_id)` retrieves the native graph for an existing session, for example to call `aget_state(config)`. A missing session raises `ValueError`. `agent.graph` creates the default session on first access.

## Low-level API { #lowlevel-api }

`build_graph` opens one session and returns its native `CompiledStateGraph`. The caller passes context on invocation and owns the semantic client lifecycle. Use `create_agent` for thread routing and multiple cached graphs.

```python
from dataagent import AgentContext, load_mcp_tools
from dataagent.lowlevel import build_graph

context = AgentContext(runtime, semantic_client=client)
graph = build_graph(
    runtime, context=context, checkpointer=checkpointer,
    mcp_tools=await load_mcp_tools(runtime),
)
result = await graph.ainvoke(inputs, config, context=context)
```

```text
build_graph(runtime, *, checkpointer=None, model=None, session=None,
            input_workspaces=None, mcp_tools=None, extension_revision=None, context=None)
```

- Without a session or context, use session `sdk`.
- Configured MCP requires `mcp_tools`; configured semantics require the context's client.
- `dataagent.entry` selects only that compiled entry.
- Pass additional LangChain callbacks in `config["callbacks"]` or `graph.with_config(callbacks=[...])`.

## A2A { #a2a-api }

Client classes import from `dataagent.extensions.a2a`. Server factories import from `dataagent.server.a2a`. Importing the SDK or A2A client does not load the server.

| Name | Purpose |
| --- | --- |
| `A2ACaller(base_url, *, auth_token=None, timeout=180)` | Call an A2A 1.0 agent. `await send(text, *, thread_id=None)` returns `A2AResult` |
| `A2AResult` | `text`, `context_id`, `task_id`, `state`, plus optional `request_id` and `root_run_id` |
| `A2ACallError` | Remote failure, cancellation, or rejection. Local code `TOOL_ERROR`; optional remote and correlation fields `remote_code`, `request_id`, `task_id`, `context_id`, `root_run_id` |
| `create_a2a_server(runtime, *, host, port, auth_token=None, model=None, task_store=None, executor=None)` | Build a standalone Starlette app with replaceable model, TaskStore, or executor |
| `run_a2a_server(runtime, *, host, port, auth_token=None)` | Run it in uvicorn, as with `dataagent serve-a2a` |

Configured `a2a.remote_agents` become `a2a__<id>` tools. See the [outbound contract](a2a.md#outbound).
