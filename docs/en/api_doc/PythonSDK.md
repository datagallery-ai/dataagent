# Python SDK Interface

DataAgent is the core Python SDK entry point exposed by the DataAgent framework. Instantiate an Agent by loading YAML configuration via `from_config`, then interact through `chat` or `astream`.

---

## DataAgent.from_config

**Interface Definition**

```python
class DataAgent:
    @classmethod
    def from_config(cls, config: str | Path) -> "DataAgent":
        ...
```

Creates an Agent instance from a YAML configuration file. The config path can be absolute or relative.

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `config` | `str \| Path` | Path to YAML config file (required) |

**Returns**

A `DataAgent` instance.

**Example**

```python
from dataagent.interface.sdk.agent import DataAgent

agent = DataAgent.from_config("path/to/ecommerce_agent.yaml")
```

---

## DataAgent.chat

**Interface Definition**

```python
async def chat(
    self,
    user_query: str,
    session_id: str | None = None,
    workspace: Path | str | None = None,
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ...
```

Triggers a single-turn agent conversation. When `debug=True` (default), conversation logs are streamed to the terminal via Rich renderer with intermediate results.

**Parameters**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_query` | `str` | required | User query text |
| `session_id` | `str \| None` | `None` | Session ID. When omitted, first tries `initial_state.session_id`, then reuses `self.session_id`, finally auto-generates |
| `workspace` | `Path \| str \| None` | `None` | Workspace override. Overrides the workspace setting in the config file |
| `initial_state` | `dict \| None` | `None` | Initial state dict, can carry `user_id`, `session_id`, `messages`, etc. |

**Returns**

`dict[str, Any]` — final state dict. Key fields:

| Field | Type | Description |
|-------|------|-------------|
| `messages` | `list` | Complete message history for this turn |
| `final_answer` | `str` | Present only on error, contains error description |
| `complete` | `bool` | Whether the conversation ended normally |
| `user_query` | `str` | The original user query |
| `error` | `str` | Present only on exception, exception info string |

**Example**

```python
response = await agent.chat("What was the top-selling product last month?")
# Extract final answer from messages on success
if "messages" in response:
    last_msg = response["messages"][-1]
    print(last_msg.content)
```

---

## DataAgent.astream

**Interface Definition**

```python
def astream(self, *args, **kwargs):
    ...
```

Triggers a streaming agent conversation, yielding events one by one via async generator. Suitable for web frontend-backend interaction scenarios.

**Parameters**

Supports two calling conventions:

1. **LangGraph native**: `astream(input={...}, config={...}, stream_mode=...)`
2. **openJiuwen**: `astream(initial_state={...}, start_at=..., checkpoint_id=...)`

Both support passing state fields such as `session_id` and `workspace` via `initial_state`.

**Returns**

`AsyncGenerator` — async generator yielding `(stream_mode, event_data)` tuples:
- `stream_mode="values"`: `event_data` is the current complete state
- `stream_mode="updates"`: `event_data` is incremental updates
- `stream_mode="custom"`: `event_data` is custom events (e.g., Rich render events)

**Example**

```python
async for mode, data in agent.astream(input={"messages": [("human", "Analyze customer data")]}):
    if mode == "values":
        print(data)
```

---

# YAML Configuration Reference

The following sections show the complete YAML configuration structure by module. All fields reflect actual code behavior. Fields not marked "optional" are required.

## AGENT_CONFIG — Agent Base Configuration

```yaml
AGENT_CONFIG:
  name: "Ecommerce Analysis Agent"       # Agent name
  type: "react"                          # Agent engine type: react (FlexAgent) | nl2sql (NL2SQLAgent)
  backend: "langgraph"                   # Backend engine, default "langgraph"
  max_iter: 50                           # Max iterations, unlimited if unset
  token_limit: 100000                    # Token limit, unlimited if unset
  enable_human_feedback: false           # Enable HITL human-in-the-loop, default false
  enable_portrait: false                 # Enable user portrait memory, default false
```

**Code Behavior**:
- `type` determines engine selection in `select_engine()`: `react` → `dataagent.core.flex.agent.FlexAgent`, `nl2sql` → `dataagent.agents.nl2sql.agent.NL2SQLAgent`
- `max_iter` when set is written to `FlexRouter`; exceeding it raises `LimitReachedError`, returning current state with a termination message appended
- `enable_human_feedback=true` creates `HumanFeedbackNode` and registers the `request_human_feedback` tool
- `enable_portrait=true` writes user characteristics to Memory via portraiter hook

---

## MODEL — Model Configuration

```yaml
MODEL:
  deepseek:                              # Model slot name (referenced by chat_model.name in Planner)
    name: "DEEPSEEK_CHAT"                # Model identifier
    model_type: "chat"                   # chat | embedding
    provider: "deepseek"                 # Platform identifier, used to read DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY env vars
    tool_call_mode: "native"             # Tool call mode, default "native"
    params:
      model: "deepseek-chat"             # Actual model name (passed to litellm)
      temperature: 0.7
      max_tokens: 8192
      timeout: 90
      max_retries: 3

  qwen3:                                 # Auxiliary model slot (for hooks or standalone nodes)
    name: "QWEN3_CHAT"
    model_type: "chat"
    provider: "openai"                   # OpenAI-protocol-compatible service
    params:
      model: "qwen3-235b"
      temperature: 0.3
```

**Code Behavior**:
- Each model slot is a dict; the key (e.g., `deepseek`) is the slot name
- `provider` is uppercased to construct env var names: `{PROVIDER}_BASE_URL` and `{PROVIDER}_API_KEY`. Actual API keys and base URLs are injected via `.env`
- `params.model` is required; other parameters (temperature, max_tokens, etc.) are optional and passed directly to litellm
- Nodes reference model slots via `chat_model.name`, merged into `AgentEnv.llm_configs`
- Model slots not referenced by child nodes are still included in `llm_configs` for hook use via `runtime.llm("<slot_name>")`

---

## SCENARIO — Scenario Description

```yaml
SCENARIO:
  chat:                                  # Scenario mode key, corresponds to mode="chat"
    instructions: |
      You are a professional data analysis assistant.
      Prioritize using available tools to obtain real data; note missing information when uncertain.
      Answers must be based on actual query results; do not fabricate data.
```

**Code Behavior**:
- `instructions` is written to `AgentEnv.instructions` for use by the Planner node's prompt template

---

## ACTOR_LOOP — Workflow Nodes

```yaml
ACTOR_LOOP:                              # Main loop workflow (required, at least one node)
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "deepseek"                   # References MODEL.deepseek
    prompt_template:                     # Optional, append prompt
      system:                            # Only system / user supported
        content: "Extra text injected into system prompt (Jinja2 template)"

  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"
    max_tool_result_length: 8192         # Max tool result length (truncation)
    max_concurrency: 5                   # Max concurrent tool calls
```

**Code Behavior**:
- `FlexAgent._create_nodes_from_config` dynamically `import`s each node's `module`, using `node` as the node name
- Reserved keys (`node`, `module`, `chat_model`, `prompt_template`) are not passed to the constructor; all other key-value pairs are passed as `**kwargs`
- `chat_model` can be a string (shorthand for name) or a dict (with `name` key)
- `prompt_template` supports only `system` / `user` message types, each with `content` (inline) or `path` (absolute path), mutually exclusive
- FlexRouter loops through ACTOR_LOOP nodes until `state.complete` is True or `max_iter` is reached

---

## TOOLS — Tool Configuration

```yaml
TOOLS:
  local_functions:                       # Custom local Python function tools
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_sql"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_plot"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "report_generator"

  mcp_servers:                           # MCP server tools
    - name: "my_mcp_server"
      url: "http://localhost:8000/mcp"

  A2A:                                   # Agent-to-Agent protocol tools
    - name: "other_agent"
      url: "http://localhost:9000/a2a"

  builtin:                               # Builtin tool override (6 tools registered by default below)
    - module: "dataagent.actions.tools.local_tool.bash_tool"
      function: "bash"
    - module: "dataagent.actions.tools.local_tool.file_tools"
      function: "edit_file"
    - module: "dataagent.actions.tools.local_tool.file_tools"
      function: "read_file"
    - module: "dataagent.actions.tools.local_tool.file_tools"
      function: "write_file"
    - module: "dataagent.actions.tools.local_tool.search_tools"
      function: "grep"
    - module: "dataagent.actions.tools.local_tool.search_tools"
      function: "glob"
```

**Code Behavior**:
- 6 builtin tools registered by default: `bash`, `edit_file`, `read_file`, `write_file`, `grep`, `glob`
- Setting `TOOLS.builtin` overrides the default list
- Each `local_functions` entry is dynamically imported via `module` + `function` and registered
- `mcp_servers` starts MCP client connections and auto-discovers tools
- `A2A` registers remote Agent tools
- Builtin skill `data_analysis_report` is active by default (`dataagent/actions/skills/data_analysis_report/`)
- All tools are registered with `ToolManager`; executor calls them via `runtime.tool_manager`

---

## CONTEXT — Context Management

```yaml
CONTEXT:
  compress_token_limit: 32768            # Trigger LLM compression when message tokens exceed this value ×1.2
  compress_message_cnt: 200              # Trigger compression when message count exceeds this value
  file_node_threshold: 500               # Min chars for long text to be persisted as FileNode during IR conversion
```

**Code Behavior**:
- All three are optional; no limit if unset
- `compress_token_limit` actual trigger threshold is `compress_token_limit * 1.2`

---

## WORKSPACE — Working Directory

```yaml
WORKSPACE:
  path: "/data/agent_workspace"          # Agent workspace root directory (use absolute path)
  allow_path:                            # Allowed directories (Bash tool can only access these)
    - "/data/shared"
    - "/home/user/datasets"
```

**Code Behavior**:
- Paths in `path` and `allow_path` must be absolute (supports `~/`)
- `ConfigManager._validate_workspace_yaml_config` validates during config loading
- `allow_path` must be a list, not a single string

---

## BASH_TOOL_WHITELIST — Bash Command Whitelist

```yaml
BASH_TOOL_WHITELIST:
  - ls
  - cat
  - head
  - python
  - pip
```

**Code Behavior**:
- When configured, only commands in the list are allowed in the Bash tool
- Unlimited if unset or null

---

# Complete Example

A ready-to-use complete YAML configuration:

```yaml
AGENT_CONFIG:
  name: "Ecommerce Data Analysis Agent"
  type: "react"
  backend: "langgraph"
  max_iter: 50

MODEL:
  deepseek:
    name: "DEEPSEEK_CHAT"
    model_type: "chat"
    provider: "deepseek"
    params:
      model: "deepseek-chat"
      temperature: 0.7
      max_tokens: 8192
      timeout: 90
      max_retries: 3

  jina_v3:
    name: "jina_v3"
    model_type: "embedding"
    provider: "embedding"
    params:
      model: "jina-embeddings-v3"

SCENARIO:
  chat:
    instructions: |
      You are an ecommerce data analysis assistant. Prioritize using tools for real data; note missing information when uncertain.

ACTOR_LOOP:
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "deepseek"

  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"
    max_tool_result_length: 8192

TOOLS:
  local_functions:
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_sql"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "report_generator"

WORKSPACE:
  path: "/data/agent_workspace"
```

**Usage Example**:

```python
from dataagent.interface.sdk.agent import DataAgent

# Create Agent from config
agent = DataAgent.from_config("ecommerce_agent.yaml")

# Single-turn conversation
response = await agent.chat("What was the top-selling product last month?")
if "messages" in response:
    last_msg = response["messages"][-1]
    print(last_msg.content)

# Streaming conversation
async for mode, data in agent.astream(
    input={"messages": [("human", "Analyze customer retention trends")]},
    stream_mode="values"
):
    if mode == "values":
        print(data.get("messages", [])[-1] if data.get("messages") else "")
```
