---
hide:
  - navigation
---

## Quick Start

This page helps you get a DataAgent Flex/ReAct Agent running in 5 minutes.

## 1. Prepare Environment

Run in the project root directory:

```bash
uv sync
```

Copy `.env.example` to create `.env` and configure your model API Key:

```bash
cp .env.example .env
```

Edit the `.env` file with your actual configuration values.

## 2. Interactive Quick Start

```bash
uv run -m dataagent quickstart
```

This command verifies installation, configuration loading and basic Agent flow. Follow the prompts to enter model configuration and start chatting with the Agent!

## 3. Start with Config File

Create `config.yaml`:

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  version: "1.0"
  description: "Data Analysis Agent"
  backend: "langgraph"
  type: "react"

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
    - "/tmp/dataagent_workspace"

SCENARIO:
  chat:
    instructions: |
      You are a data analysis assistant. Answer questions directly; use registered tools when needed.

PRE_WORKFLOW: []

ACTOR_LOOP:
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "chat_model"
  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"

POST_WORKFLOW: []

TOOLS:
  local_functions:
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_sql"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_plot"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "report_generator"
```

Start the Agent:

```bash
# Terminal interactive mode
uv run -m dataagent --config config.yaml
```

## 4. Config Check

```bash
# Check environment variable references in config
uv run -m dataagent config check config.yaml
```

## 5. Python SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("config.yaml")

# Single-turn conversation
response = await agent.chat("Analyze sales data trends for the past week")
print(response)

# Streaming conversation
async for chunk in agent.astream(input={"user_query": "Generate user report"}):
    print(chunk, end="", flush=True)
```

## 6. A2A 1.0 Server Mode

```bash
# Start A2A server
uv run -m dataagent serve-a2a \
  --config config.yaml \
  --host 0.0.0.0 \
  --port 9999 \
  --auth-token your_token

# Service endpoints
# ├── 🌟 AgentCard: http://localhost:9999/.well-known/agent.json
# ├── 📡 JSON-RPC:  http://localhost:9999/a2a/jsonrpc
# └── 🔌 REST:      http://localhost:9999/a2a/rest
```

## 7. More Examples

Reference the example configs in the repository:

```
dataagent/core/flex/examples/
```

## 8. Optional: Connect Database Semantic Service {#optional-semantic-service}

Semantic Service (Semantic Layer REST service) is an **optional external component** of DataAgent—not required to start an Agent. After the steps above, you can already run a Flex/ReAct Agent, use the SDK, or start an A2A server.

Deploy Semantic Service and import scenario data when you need:

| Scenario | Semantic Service required? |
| --- | --- |
| Interactive chat, general tool use | No |
| NL2SQL: natural language to SQL | Yes |
| Table/column semantic search, JOIN paths, SQL Few-shot | Yes |
| Vector semantic search (table/column description recall) | Yes (vector model required) |

### 8.1 Recommended reading order

Follow this order for the semantic-layer trial path (about 30–60 minutes including model download):

| Step | Document | What to do |
| --- | --- | --- |
| 1 | [Semantic Service Deployment Guide](../installation_doc/database_install/semantic-service-deployment.md) | Download the service package, start PostgreSQL/pgvector, configure and start the REST service |
| 2 | [Scenario Data Import](../installation_doc/database_install/scenario-data-import.md) | Create demo business DB, import metadata, verify search APIs |
| 3 | [Build a Dedicated NL2SQL Agent](../case/build-an-nl2sql-application.md) | Configure `DATABASE` / `METAVISOR` and run NL2SQL |
| 4 | [Build a Data Analysis Agent](../case/build-a-dataagent-from-scratch.md) | Main Agent calls NL2SQL sub-Agent on demand |

!!! note "About the demo business database"
    `demo_retail.sqlite` in the scenario tutorial is a **sample business database created at runtime**—it is not bundled with the Semantic Layer service package. Semantic Service stores only metadata (tables, columns, relationships); real data is read via Agent `DATABASE.config.path` pointing at the SQLite file.
