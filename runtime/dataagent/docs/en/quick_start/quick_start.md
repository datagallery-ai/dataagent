---
hide:
  - navigation
---

# Quick Start

DataAgent now runs its main Agent on LangGraph and Deep Agents. Existing YAML model, tool, workspace, scenario, and context sections remain the configuration boundary; custom workflow-node topology is no longer required.

## 1. Install and configure the model

Run from the project root:

```bash
uv sync
cp .env.example .env
```

Set the credentials used by your selected provider. The checked-in quickstart uses `DEEPSEEK_API_KEY` and optionally `DEEPSEEK_BASE_URL`.

## 2. Run the built-in quickstart

```bash
uv run -m dataagent quickstart
```

Or start the same YAML explicitly:

```bash
uv run -m dataagent --config dataagent/examples/quickstart.yaml
```

## 3. Create a compatible YAML

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  version: "1.0"
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
      temperature: 0.7
      base_url: "$env{DEEPSEEK_BASE_URL}"
      api_key: "$env{DEEPSEEK_API_KEY}"

WORKSPACE:
  path: "/tmp/dataagent_workspace"
  allow_path:
    - "/tmp/reference_data"

SCENARIO:
  chat:
    instructions: |
      You are a data analysis assistant. Answer directly and use registered tools when needed.

TOOLS:
  local_functions:
    - module: "my_tools"
      function: "lookup_metric"
```

`MODEL.chat_model` is the primary model by convention. With several chat models, set `AGENT_CONFIG.primary_model` to a model slot name. Provider names are case-insensitive, and OpenAI-compatible providers use LangChain's `ChatOpenAI` implementation.

If `WORKSPACE.path` is omitted, the filesystem workspace defaults to `.dataagent/<user_id>/<session_id>`. Paths listed in `allow_path` are exposed read-only. Set `WORKSPACE.backend: state` for state-backed files; the shell tool is then disabled because no host directory exists.

Validate the YAML before starting it:

```bash
uv run -m dataagent config check config.yaml
uv run -m dataagent --config config.yaml
```

## 4. Python SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("config.yaml")

result = await agent.chat("Analyze sales trends", session_id="demo-session")
print(result["messages"][-1].content)

async for event in agent.astream(
    {"messages": [("user", "Generate a short report")]},
    session_id="demo-session",
    stream_mode="values",
):
    print(event)
```

`chat()` returns the native LangGraph state. `astream()` forwards native LangGraph stream modes and events. Both accept a LangGraph `RunnableConfig` through `config=`; DataAgent adds the session-scoped `thread_id` while preserving other run options.

## 5. Optional capabilities

- Register local functions with `TOOLS.local_functions`; synchronous and asynchronous functions are supported.
- Configure official MCP adapters with `TOOLS.mcp_servers` and remote Agents with `TOOLS.A2A`.
- Configure native Deep Agents skills under `TOOLS.skills`.
- Add general child Agents with `SUBAGENTS`, or one inline native NL2SQL child with `NL2SQL`.
- Enable user clarification with `AGENT_CONFIG.enable_human_feedback` and optional `SCENARIO.chat.human_feedback_conditions`.
- Reuse legacy Suite declarations: ConfigManager expands them into ordinary YAML before the Deep Agent compiler runs.

See [Python SDK](../api_doc/PythonSDK.md) for the complete supported configuration surface.

## 6. A2A server

```bash
uv run -m dataagent serve-a2a \
  --config config.yaml \
  --host 0.0.0.0 \
  --port 9999 \
  --auth-token your_token
```

## 7. Optional Semantic Service

Semantic Service is not required for general chat or tool use. Deploy it when NL2SQL or semantic table/column retrieval needs database metadata. Follow [Semantic Service Deployment](../installation_doc/database_install/semantic-service-deployment.md), then [Build a Dedicated NL2SQL Agent](../case/build-an-nl2sql-application.md).
