# Build a Dedicated NL2SQL Agent

This guide walks through a natural-language database query scenario and shows how to build a dedicated Agent that only handles NL2SQL. It fits when the user's question is itself a lookup, aggregation, grouping, sort, or filter—and the main Agent does not need complex task planning.

The focus of a dedicated NL2SQL Agent is not tool orchestration, but turning natural language into SQL and completing validation, execution, and result return.

## 1. When to Use This Pattern

| Suitable | Not suitable |
| --- | --- |
| The user's question maps directly to a database query. | The task must be broken into multiple steps before deciding whether to query the database. |
| A single NL2SQL pipeline is enough for schema awareness, SQL generation, execution, and results. | The task needs several tools—for example files, reports, or external APIs before querying the database. |
| You want a fixed configuration to validate NL2SQL on a business database. | You want NL2SQL as an on-demand capability of a main Agent. |

If your goal is "the main Agent understands the task and only calls NL2SQL when a database query is needed," see [Build a Data Analysis Agent](build-a-dataagent-from-scratch.md).

## 2. Overall Architecture

The dedicated NL2SQL Agent pipeline:

```text
用户自然语言问题
      │
      ▼
NL2SQLAgent（AGENT_CONFIG.type = nl2sql）
      │
      ├─ Perceptor：读取数据库 schema、字段语义、join 信息
      ├─ Generator：生成候选 SQL
      ├─ Validator：做 SQL explain、关键词或值匹配校验
      ├─ Executor：执行 SQL，返回结果
      ├─ Reflector：必要时反思修正
      └─ Selector：选择最终 SQL 与结果
```

MetaVisor supplies enriched metadata. Deployment and import (**required for NL2SQL cases**):

- [Quick Start §8: optional semantic service](../quick_start/quick_start.md#optional-semantic-service)
- [Semantic Service Deployment Guide](../installation_doc/database_install/semantic-service-deployment.md)
- [Scenario Data Import](../installation_doc/database_install/scenario-data-import.md)
- [Semantic Service User Guide](../semantic_service/semantic-service-user-guide.md)

After deployment, you need three key values:

| Item | Purpose |
| --- | --- |
| `DATABASE.db_id` | Database identifier registered in MetaVisor. |
| `METAVISOR.metavisor_url` | MetaVisor metadata service URL. |
| `METAVISOR.valuematch_url` | ValueMatch service URL for literal value matching. |

## 3. Prerequisites

Before you start, confirm:

1. Project installation is complete and you can run `uv run ...` from the repository root.
2. Model environment variables are configured, e.g. `BAILIAN_BASE_URL` and `BAILIAN_API_KEY`.
3. **(Required)** Semantic Service deployment and scenario data import (NL2SQL depends on the external semantic service):
   - [Semantic Service Deployment Guide](../installation_doc/database_install/semantic-service-deployment.md)
   - [Scenario Data Import](../installation_doc/database_install/scenario-data-import.md)
4. Demo SQLite business database ready with an **absolute path** in Agent config (logical `demo_db`; file created by the tutorial, not bundled with the service package).
5. `METAVISOR.metavisor_url` is reachable and `DATABASE.db_id` matches metadata `databaseName`.

If Semantic Service is not deployed yet, start from [Quick Start §8](../quick_start/quick_start.md#optional-semantic-service).

Example SQLite path and Semantic Service connection (same fields as the built-in YAML; values adapted for the demo scenario):

```yaml
DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/data/demo_retail.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  valuematch_url: "http://localhost:8000"
```

Sample verification questions:

- City-level GMV ranking (各城市成交额排名)
- Monthly order count (每月订单量是多少)

See [Semantic Service User Guide](../semantic_service/semantic-service-user-guide.md) for MetaVisor capabilities.

## 4. Author the NL2SQL Agent Configuration

The built-in configuration is at:

```text
dataagent/agents/nl2sql/nl2sql_agent.yaml
```

You can edit that file or copy it as your own business config. Core configuration has five parts.

| Block | Role |
| --- | --- |
| `AGENT_CONFIG` | Agent type. Dedicated NL2SQL must use `type: "nl2sql"`. |
| `MODEL` | Chat model for SQL generation and revision. |
| `CORE` | NL2SQL internal nodes and thresholds. |
| `DATABASE` | Database id, engine, and connection parameters. |
| `METAVISOR` | Enriched metadata and value-matching service URLs. |

Example configuration (same structure as repository `dataagent/agents/nl2sql/nl2sql_agent.yaml`; replace `demo_db` and paths for your scenario):

```yaml
AGENT_CONFIG:
  name: "NL2SQL Agent"
  backend: "langgraph"
  type: "nl2sql"

MODEL:
  deepseek:
    model_type: "chat"
    provider: "bailian"
    params:
      model: "deepseek-v4-flash"
      temperature: 0.0

CORE:
  coordinator: {}
  perceptor:
    user_schema: null
    user_evidence: null
    user_sql_rules: "sql_rules_bird"
    user_few_shot_examples: null
  generator:
    strategies: ["prompt"]
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

DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/data/demo_retail.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  valuematch_url: "http://localhost:8000"
```

When configuring, verify:

- `DATABASE.db_id` matches the database id imported into MetaVisor.
- `DATABASE.engine` matches the real database, for example `sqlite`, `mysql`, or `postgres`.
- For SQLite, prefer an absolute path in `DATABASE.config.path` so the file is found regardless of the working directory.
- Do not put `api_key` in YAML; use `.env` instead.
- Point `METAVISOR.metavisor_url` and `METAVISOR.valuematch_url` at your deployed Semantic Service; set `username` / `password` for your deployment.

## 5. Run the Dedicated Agent

Load the configuration and invoke via the SDK:

```python
import asyncio
from pathlib import Path

from dataagent.interface.sdk.agent import DataAgent


async def main():
    project_dir = Path(__file__).resolve().parents[2]
    config_path = project_dir / "dataagent" / "agents" / "nl2sql" / "nl2sql_agent.yaml"
    agent = DataAgent.from_config(config_path)

    result = await agent.chat("每月订单量是多少")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

The repository also provides an end-to-end script:

```bash
uv run tests/e2e/test_nl2sql.py
```

If you use your own YAML, point `config_path` in the script to your config file.

## 6. Inspect Results

`agent.chat()` returns the final state. When debugging NL2SQL, check these fields first:

| Field | Description |
| --- | --- |
| `messages` | Message stream and final answer. |
| `sql` | Selected final SQL. |
| `columns` / `rows` / `rows_preview` | Result columns, full rows, and preview rows. |
| `generation_results` | Candidate SQL generation output. |
| `validation_results` | SQL validation output. |
| `execution_results` | SQL execution output. |
| `confidence` | Confidence score from the Selector. |

If results are empty, determine whether SQL was not generated, execution failed, or the database has no matching rows.

## 7. Common Issues

### 7.1 Model API key not loaded

Check that `.env` exists in the runtime directory and variable names match `provider`. For example `provider: "bailian"` reads `BAILIAN_BASE_URL` and `BAILIAN_API_KEY`.

### 7.2 SQLite file not found

Use an absolute path in `DATABASE.config.path`. Relative paths follow the current working directory when you run the command.

### 7.3 MetaVisor connection failure

Use `curl` to verify `METAVISOR.metavisor_url`, then confirm `DATABASE.db_id` metadata is imported into MetaVisor. See [Semantic Service Deployment Guide](../installation_doc/database_install/semantic-service-deployment.md) for deployment, initialization, and import.

### 7.4 Generated SQL does not match business definitions

Make the user question more explicit: entity definitions, metric formulas, filters, grain, and sort order. The NL2SQL Agent handles clear query intent; it cannot infer missing business rules for the user.

## 8. Summary

Essentials for a dedicated NL2SQL Agent:

1. Set `AGENT_CONFIG.type` to `nl2sql`.
2. Point `DATABASE` at the real business database.
3. Point `METAVISOR` at Semantic Service with metadata imported.
4. Phrase user questions with clear business objects, metric definitions, and query conditions.

When a main Agent must plan tasks, organize reports, and query the database on demand, do not fold that logic into a dedicated NL2SQL Agent—use the main Agent with an NL2SQL sub-Agent instead.
