# Native NL2SQL Subagent Design

## Status

Accepted for implementation on 2026-09-03.

## Objective

Replace the remaining CBB and retired workflow-backend dependencies around NL2SQL with a native LangGraph graph,
then register that graph as a Deep Agents 0.7.5 `CompiledSubAgent` of the main DataAgent.

The implementation is final rather than transitional: it must execute SQL, persist SQL and CSV artifacts, and use
the same workspace backend as the main agent. It must not restore the retired FlexAgent, OpenJiuwen, asynchronous
job, resource, or `sub_agent_tool` systems.

## Public YAML contract

### Dedicated NL2SQL configuration

One default NL2SQL subagent may be configured inline in the main agent YAML:

```yaml
NL2SQL:
  MODEL:
    nl2sql_model:
      model_type: chat
      provider: deepseek
      params:
        model: deepseek-chat

  CORE:
    perceptor: {}
    generator: {}
    validator: {}
    reflector: {}
    executor: {}
    selector: {}

  DATABASE: {}
  SEMANTIC_LAYER: {}
```

`NL2SQL.AGENT_CONFIG` is optional. The compiler supplies these defaults:

```yaml
id: nl2sql
name: NL2SQL Agent
description: Generate, validate, and execute read-only SQL, then save the SQL and CSV result.
type: nl2sql
```

An explicitly supplied `AGENT_CONFIG` may override `id`, `name`, and `description`. An explicitly supplied `type`
must equal `nl2sql`, ignoring case.

The main agent's top-level `DATABASE` and `SEMANTIC_LAYER` sections override the corresponding inline values so the
dedicated subagent queries the main agent's runtime data source. An inline `MODEL` is used when present; otherwise
the subagent reuses the main agent's primary model.

### General subagents

General subagents use the canonical `SUBAGENTS` section:

```yaml
SUBAGENTS:
  - path: /absolute/path/to/agent.yaml
```

`SUBAGENT_CONFIGS` remains a loader-only compatibility alias. `ConfigManager` normalizes it to `SUBAGENTS` before
Suite merge and native compilation. Defining both keys in the same effective source is an error. The Deep Agents
compiler and runtime only know `SUBAGENTS`.

Suite `subagents/*.yaml` files are expanded by the Suite configuration resolver into ordinary `SUBAGENTS` entries.
The Deep Agents compiler remains unaware of Suite directory layout.

The dedicated `NL2SQL` section registers exactly one default NL2SQL subagent. Additional NL2SQL variants may be
registered through `SUBAGENTS`. All compiled subagent identifiers must be unique.

## Workspace and backend contract

`WORKSPACE.backend` supports case-insensitive `filesystem` and `state` values and defaults to `filesystem`.

For the default filesystem backend, the effective workspace is resolved in this order:

1. Explicit absolute `WORKSPACE.path`.
2. `${DATAAGENT_HOME:-~/.dataagent}/<user_id>/<session_id>`.

The SDK resolves `user_id` from invocation state, then top-level `USER_ID`, then `anonymous`. It resolves
`session_id` from the explicit argument, then invocation state, then a generated identifier. Both values are
validated before being used as path segments. LangGraph's internal thread identifier includes both values to avoid
cross-user checkpoint collisions.

Deep Agents 0.7.5 requires an initialized backend instance and no longer accepts backend factories. Therefore the
SDK caches a compiled graph per effective session workspace. The graph's `FilesystemBackend`, native file tools,
shell middleware, and NL2SQL subagent all use the same session directory. Externally supplied checkpointers and
stores may be shared across these compiled graphs.

Users may explicitly select state-backed files:

```yaml
WORKSPACE:
  backend: state
```

`WORKSPACE.backend: state` cannot be combined with `WORKSPACE.path`. It does not register `ShellToolMiddleware`,
and `SHELL_TOOL_WHITELIST` or explicitly configured `shell`/`bash` tools are rejected during configuration
compilation. `WORKSPACE.allow_path` remains available as read-only filesystem routes on a `CompositeBackend`.

## Native NL2SQL graph

`create_nl2sql_agent` returns a compiled LangGraph graph and is the canonical implementation for both standalone
NL2SQL execution and Deep Agents subagent execution.

The graph preserves the domain workflow:

```text
prepare -> perceptor -> generator -> validator -> reflector
                                      ^             |
                                      +-- retry ----+

reflector -> executor -> selector -> finalize
               ^           |
               +-- retry --+
```

The reflector and selector retry loops are NL2SQL domain behavior and remain configurable through `CORE`.

The graph uses three schemas:

- Input: `messages` and optional backend `files`.
- Internal: the complete NL2SQL state.
- Output: `messages`, `structured_response`, and optional backend `files`.

The `prepare` node derives `question` from the last human message and initializes internal state. The `finalize`
node produces an `NL2SQLResult`, appends an `AIMessage`, and persists artifacts through the injected native backend.
Internal fields such as schema data, SQL candidates, validation details, and full rows never merge into the parent
DataAgent state.

NL2SQL nodes no longer inherit `BaseNode`; the state no longer inherits CBB `BaseState`; routing no longer inherits
`BaseRouter`; and the agent no longer inherits `BaseAgent` or uses `workflow_backend_factory`. Node dependencies are
explicitly injected as a `BaseChatModel`, effective configuration, and backend. Model calls use LangChain message
objects and `BaseChatModel.ainvoke` directly rather than the global `llm_manager`.

## Artifact contract

On successful SQL execution, NL2SQL writes:

```text
/nl2sql/<invocation_id>/query.sql
/nl2sql/<invocation_id>/result.csv
```

These are backend-virtual paths. With the default filesystem backend they map to:

```text
${DATAAGENT_HOME:-~/.dataagent}/<user_id>/<session_id>/nl2sql/<invocation_id>/
```

The structured result contains:

```text
sql, sql_path, csv_path, columns, row_count, rows_preview, confidence, error
```

Full rows are written to CSV subject to `CORE.executor.limit` and are not placed in the parent model context. SQL
or query execution failure produces no success artifacts. Backend write failure fails the complete subagent call.
Generated invocation directories prevent overwriting existing output and avoid accepting model-controlled paths.

## Main-agent integration

The Deep Agent unified configuration gains a `subagents` field. A dedicated `SubagentConfigCompiler` compiles the
inline `NL2SQL` section followed by general `SUBAGENTS`, validates unique identifiers, and produces native
`CompiledSubAgent` values. Passing those values to `create_deep_agent` makes Deep Agents expose its native `task`
tool. No DataAgent-specific subagent tool or lifecycle protocol is added.

Standalone root YAML with `AGENT_CONFIG.type: nl2sql` is dispatched to the same `create_nl2sql_agent` factory so
the public `chat` and `astream` interfaces do not require a second NL2SQL implementation.

## Failure and security rules

- SQL read-only and SQLGlot validation behavior remains controlled by the existing NL2SQL validator configuration.
- User and session identifiers are validated as single path segments before workspace creation.
- Artifact paths are framework-generated and never taken from model input.
- Duplicate subagent identifiers, unsupported subagent types, invalid backend combinations, and missing required
  NL2SQL `CORE` nodes fail during graph compilation.
- Subagent exceptions flow through the main agent's native `ToolErrorMiddleware`.
- State backend mode deliberately has no shell because state-backed virtual files are not host filesystem files.

## Verification

Verification consists of focused configuration and graph compilation checks plus one real end-to-end run using the
configured DeepSeek endpoint and a temporary SQLite database:

1. Load a main YAML containing inline `NL2SQL` and legacy/general subagent configuration.
2. Confirm session workspace and backend selection.
3. Have the main Deep Agent invoke the native `task` tool for NL2SQL.
4. Confirm SQL generation, validation, execution, parent-visible structured output, and readable SQL/CSV artifacts.
5. Confirm NL2SQL internal state does not leak into the parent state.
