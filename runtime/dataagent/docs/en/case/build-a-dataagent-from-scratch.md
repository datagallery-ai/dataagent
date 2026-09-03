# Build a Data Analysis Agent with NL2SQL

This pattern uses a native Deep Agent as the main planner and gives it one dedicated native NL2SQL subagent. The main Agent can inspect files, use business tools, and produce reports; database questions are delegated to the NL2SQL runnable only when needed.

## Architecture

```text
User request
    │
    ▼
Deep Agents main Agent
    ├── native filesystem / Shell / local / MCP / A2A tools
    ├── native skills and middleware
    └── native subagent delegation
             │
             ▼
       NL2SQL LangGraph
       perception → generation → validation → execution → selection
```

There is no separate NL2SQL wrapper tool to register. The `NL2SQL` YAML block is compiled to a runnable and passed directly to Deep Agents as a subagent.

## Configuration

```yaml
AGENT_CONFIG:
  name: "Sales Data Agent"
  description: "Analyze sales data and produce grounded reports."
  backend: langgraph
  type: react
  primary_model: chat_model
  max_iter: 40

MODEL:
  chat_model:
    provider: deepseek
    model_type: chat
    params:
      model: deepseek-chat
      temperature: 0.1

WORKSPACE:
  path: /absolute/path/to/workspace

SCENARIO:
  chat:
    instructions: |
      Delegate database questions to the nl2sql subagent.
      Preserve its SQL and result artifacts, then explain the result in business language.

DATABASE:
  db_id: sales
  dialect: sqlite
  config:
    path: /absolute/path/to/sales.sqlite

SEMANTIC_LAYER:
  base_url: http://localhost:32000
  username: example
  password: "123456"
  timeout: 30

NL2SQL:
  CORE:
    coordinator: {}
    perceptor:
      user_schema: null
      user_evidence: null
      user_sql_rules: sql_rules_bird
      user_few_shot_examples: null
    generator:
      strategies: [prompt]
      num_workers: 1
      num_samples: 3
    validator:
      db_explain: true
      keyword_match: false
      metadata_match: false
    reflector:
      threshold: 0.9
    executor:
      limit: -1
      preview_limit: 5
    selector:
      threshold: 0.9
```

The inline block supplies default metadata:

- identifier: `nl2sql`;
- name: `NL2SQL Agent`;
- type: `nl2sql`;
- a description instructing the main Agent when to delegate.

You may add `NL2SQL.AGENT_CONFIG` to override compatible metadata or choose another configured model with `primary_model`. Exactly one inline `NL2SQL` child is supported. Top-level `DATABASE` and `SEMANTIC_LAYER` settings override copies inside the inline block so connection policy stays centralized.

## Run

```python
import asyncio

from dataagent import DataAgent


async def main() -> None:
    agent = DataAgent.from_config("config.yaml")
    result = await agent.chat("Which five cities had the highest revenue last month?", session_id="demo")
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
```

The returned object is native LangGraph state. SQL, CSV, and supporting files produced through the shared backend are visible to the main Agent, while message handoff and execution remain inside Deep Agents' subagent middleware.

## General subagents

Use `SUBAGENTS` when a child needs its own complete YAML rather than the dedicated inline NL2SQL form:

```yaml
SUBAGENTS:
  - path: /absolute/path/to/researcher.yaml
  - path: /absolute/path/to/another_nl2sql.yaml
```

The child may inherit the parent's models and backend. A child that declares its own `MODEL` section uses those models instead. Recursive paths and duplicate identifiers fail during compilation.

## Operational checks

1. Verify the model provider's API key and optional base URL in `.env`.
2. Use an absolute database path.
3. Confirm `DATABASE.db_id` matches metadata loaded into Semantic Service.
4. Verify `SEMANTIC_LAYER.base_url` before testing SQL generation.
5. Reuse `session_id` when the main Agent must continue the same LangGraph thread.

For a standalone Agent that always runs the NL2SQL graph directly, see [Build a Dedicated NL2SQL Agent](build-an-nl2sql-application.md).
