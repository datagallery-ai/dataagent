# Python SDK and YAML Reference

DataAgent preserves the YAML and `chat`/`astream` entrypoints while running the main Agent as a native Deep Agents 0.7.5 compiled LangGraph. The SDK does not translate LangGraph state or stream events into a private protocol.

## SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("config.yaml")
```

### `await agent.chat(...)`

```python
result = await agent.chat(
    "Analyze the latest sales data",
    session_id="session-001",
    initial_state={"user_id": "alice"},
    checkpoint_id="optional-checkpoint",
    config={"tags": ["production"], "recursion_limit": 100},
)
```

The result is the final native LangGraph state, including `messages`. `session_id` is mapped to LangGraph's checkpointer `thread_id` together with `user_id`. If no session ID is supplied, each call receives a new generated session. Pass the same ID for multi-turn continuity.

### `agent.astream(...)`

```python
async for event in agent.astream(
    {"messages": [("user", "Create a report")]},
    session_id="session-001",
    stream_mode="values",
    config={"tags": ["production"]},
):
    print(event)
```

`astream()` returns an async iterator and forwards native LangGraph `stream_mode` and event payloads. For compatibility, `initial_state={"user_query": "..."}` is also accepted. Caller-supplied `RunnableConfig` fields are preserved; DataAgent owns `configurable.thread_id`, and an explicit `checkpoint_id` owns `configurable.checkpoint_id`.

### Other methods

- `await agent.build_agent_graph()` returns the `CompiledStateGraph`.
- `await agent.select_engine(...)` compiles an effective config into a native graph.
- `agent.get_agent_info()` returns configured name, version, description, and backend.
- `agent.update_config(mapping)` invalidates cached per-session graphs.

## Minimal YAML

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  description: "Data analysis assistant"
  backend: "langgraph"
  type: "react"
  max_iter: 30

MODEL:
  chat_model:
    provider: "deepseek"
    model_type: "chat"
    params:
      model: "deepseek-chat"
      api_key: "$env{DEEPSEEK_API_KEY}"
      base_url: "$env{DEEPSEEK_BASE_URL}"
```

`backend: langgraph` and `type: react` remain accepted compatibility values. The custom node topology fields previously used to construct the main loop are retired; the loop is now created by Deep Agents.

## Models

Every non-embedding entry under `MODEL` is compiled to a LangChain `BaseChatModel`. Provider names are case-insensitive.

```yaml
AGENT_CONFIG:
  primary_model: chat_model

MODEL:
  chat_model:
    provider: DeepSeek
    model_type: chat
    params:
      model: deepseek-chat
  backup_model:
    provider: openai
    model_type: chat
    params:
      model: gpt-4.1-mini
```

Primary selection order is `AGENT_CONFIG.primary_model`, then the `chat_model` slot, then the first chat slot. Unknown and explicitly OpenAI-compatible providers use `ChatOpenAI`; native LangChain providers use `init_chat_model`. The compiler reads `<PROVIDER>_API_KEY` and `<PROVIDER>_BASE_URL` when those values are absent from `params`.

Additional chat models become ordered fallback models. Embedding slots stay available to retained semantic/NL2SQL systems but are not candidates for the main Agent model.

## Workspace

```yaml
USER_ID: alice
WORKSPACE:
  backend: filesystem
  path: /srv/dataagent/workspaces/alice
  allow_path:
    - /srv/reference-data
```

- Default backend: Deep Agents `FilesystemBackend`.
- Default path: `.dataagent/<user_id>/<session_id>` according to the configured workspace policy.
- `allow_path` entries are mounted into native filesystem tools as read-only directories.
- `backend: state` selects `StateBackend`; `path` is then invalid and the Shell tool is disabled.

Deep Agents supplies its native filesystem tools. DataAgent also installs the LangChain Shell middleware for filesystem workspaces, with a 600-second command timeout.

Optional shell allowlist:

```yaml
SHELL_TOOL_WHITELIST:
  - ls
  - cat
  - python
```

Each command in a pipeline or chain must be allowed. Command substitution and process substitution are rejected.

## Local tools

Existing `TOOLS.local_functions` declarations are compiled to native LangChain tools:

```yaml
TOOLS:
  local_functions:
    - module: my_package.tools
      function: lookup_metric
      name: lookup_metric
      description: Look up a governed business metric.
      config:
        catalog: finance
      hooks:
        pre:
          - my_package.hooks.validate_metric_request
        post:
          - my_package.hooks.audit_metric_result
```

Synchronous functions, asynchronous functions, and `BaseTool` objects are supported. A function may declare `_tool_context` to receive compatible config plus the native tool runtime without exposing that parameter to the model.

Tool pre-hooks receive `ToolCallRequest` and return `ToolCallRequest | None`. Post-hooks receive `(ToolCallRequest, ToolMessage | Command)` and return the same result type or `None`. MCP hooks apply per server and A2A hooks per remote Agent using the same `hooks.pre/post` shape.

## MCP

MCP uses the official `langchain-mcp-adapters` client and is discovered asynchronously during graph compilation.

```yaml
TOOLS:
  mcp_servers:
    - server_id: local-files
      transport_type: stdio
      config:
        command: uvx
        args: [mcp-server-filesystem, /srv/data]
    - server_id: metrics
      transport_type: streamable_http
      config:
        url: https://metrics.example.com/mcp
        headers:
          Authorization: "Bearer $env{MCP_TOKEN}"
```

Supported transports are `stdio`, `sse`, `streamable_http`, and `websocket`.

## A2A tools

Each configured remote AgentCard is discovered, and each published skill becomes an asynchronous LangChain tool:

```yaml
TOOLS:
  A2A:
    - agent_id: risk-agent
      base_url: https://risk.example.com
      auth_token: "$env{RISK_AGENT_TOKEN}"
      timeout: 30
```

The legacy nested form, such as `- risk-agent: {base_url: ...}`, is also accepted.

## Skills

Skills are exposed through Deep Agents' native skill mechanism:

```yaml
TOOLS:
  skills:
    builtin: [sql-analysis]
    custom_dirs:
      - /srv/dataagent-skills
    user: [my-personal-skill]
```

Each skill is a directory containing `SKILL.md`. Built-in and user lists are name allowlists. Custom directories expose their valid skill subdirectories. Skill sources are read-only to the Agent.

## Subagents and NL2SQL

General child Agents point to complete YAML files:

```yaml
SUBAGENTS:
  - path: /srv/agents/researcher.yaml
```

Paths must be absolute or begin with `~/`. A child YAML may define a native Deep Agent or `AGENT_CONFIG.type: nl2sql`. The deprecated `SUBAGENT_CONFIGS` spelling is normalized to `SUBAGENTS`, but new configuration should use `SUBAGENTS`.

One important NL2SQL child can be configured inline without repeating metadata:

```yaml
DATABASE:
  type: sqlite
  config:
    path: /srv/data/sales.sqlite

NL2SQL:
  SEMANTIC_LAYER:
    enabled: true
```

Only one inline `NL2SQL` Agent is allowed. Its default identifier, name, description, and type are supplied automatically; an optional nested `AGENT_CONFIG` may override compatible metadata. Parent `DATABASE` and `SEMANTIC_LAYER` sections take precedence so connection policy remains centralized.

## Prompts and human feedback

```yaml
AGENT_CONFIG:
  enable_human_feedback: true

SCENARIO:
  chat:
    instructions: You are a governed data assistant.
    prompt_appends:
      system:
        - Never invent metric definitions.
    human_feedback_conditions:
      - A requested metric has more than one governed definition
```

The system prompt combines DataAgent's generic planner guidance, workspace rules, prompt appends, HITL conditions, and scenario instructions. When feedback is enabled, `request_human_feedback` is registered and its configured conditions are explicitly marked as HITL conditions in the prompt.

## Agent and model hooks

```yaml
HOOKS:
  agent:
    pre: [my_package.hooks.before_agent]
    post: [my_package.hooks.after_agent]
  model:
    pre:
      - name: my_package.hooks.before_model
        model: backup_model
        option: value
    post: [my_package.hooks.after_model]
```

Model hooks surround only calls made by the main Agent loop. A model call performed inside a tool is a separate runnable and does not trigger these hooks. `HOOKS.nodes.planner.pre/post` remains a compatibility alias for model hooks; `HOOKS.model.pre/post` is recommended. Hook callables receive `state`, optionally native LangGraph `runtime`, and optional keyword-only configured parameters.

## Context and middleware

```yaml
CONTEXT:
  compress_token_limit: 64000
  compress_message_cnt: 100
```

These existing fields configure native Deep Agents summarization. DataAgent also enables summarization-tool support, Todo tracking, tool-error conversion, model retry, model fallback when multiple models exist, and Shell middleware for filesystem workspaces. `AGENT_CONFIG.max_iter` installs a native model-call limit; `null` uses the native runtime's normal behavior and LangGraph safety limit.

## Suites

`SUITE.include` is still handled by `ConfigManager`. Suite models, tools, hooks, prompts, skills, governance config, and subagent YAML files are expanded into ordinary YAML layers before the Deep Agent compiler sees the result. Runtime resources/jobs and the retained `data_analysis` Suite remain migration-stage features rather than part of the native main loop.
