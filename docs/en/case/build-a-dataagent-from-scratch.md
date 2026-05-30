# Build a Data Analysis Agent

This guide explains how to build a ReAct main Agent that invokes an NL2SQL sub-Agent when a database query is needed. It fits scenarios where the user's task is more than a single lookup, but one step requires natural language to SQL.

Unlike [Build a Dedicated NL2SQL Agent](build-an-nl2sql-application.md), the main Agent here understands the task, organizes steps, and summarizes the answer; NL2SQL is only invoked on demand as a tool capability.

## 1. When to Use This Pattern

| Suitable | Not suitable |
| --- | --- |
| The user's task requires understanding the goal first, then deciding whether to query the database. | The user's question is itself a one-off database lookup. |
| The main Agent also organizes the final answer, saves SQL/CSV files, or coordinates with other tools. | You only want to validate NL2SQL on a specific database. |
| You want NL2SQL wrapped as a reusable tool, with the main Agent controlling when it is called. | You do not need ReAct orchestration—only SQL generation and execution. |

Example question:

```text
统计高价值客户最近一个季度的购买金额变化，并把 SQL 和结果文件保存下来。
```

The main Agent must clarify the task goal, call `nl2sql_sub_agent_tool`, obtain SQL/CSV file paths, and then compose the final answer.

## 2. Overall Architecture

```text
用户问题
  │
  ▼
FlexAgent（AGENT_CONFIG.type = react）
  │
  ├─ Planner：判断是否需要查数据库
  ├─ Executor：调用工具
  │       │
  │       ▼
  │   nl2sql_sub_agent_tool
  │       │
  │       ├─ 读取内置 NL2SQL 配置
  │       ├─ 用主 Agent 的 DATABASE / METAVISOR 覆盖子 Agent 配置
  │       ├─ 拉起 NL2SQLAgent 执行查询
  │       └─ 保存 SQL 文件和 CSV 结果文件
  │
  └─ 汇总最终回答
```

The key point of this pattern: `DATABASE` and `METAVISOR` live in the main Agent YAML; at runtime the tool overlays them onto the temporary NL2SQL sub-Agent config. The same NL2SQL sub-Agent can therefore be reused by different business main Agents.

## 3. Prerequisites

Before you start, confirm the following:

1. Project installation is complete and you can run `uv run ...` from the repository root.
2. Model environment variables are configured, for example `BAILIAN_BASE_URL` and `BAILIAN_API_KEY`.
3. A business database is ready. For SQLite, use an absolute file path.
4. MetaVisor/Semantic Service deployment and metadata import are complete.
5. A writable workspace is available for SQL and CSV files produced by the sub-Agent.

For MetaVisor/Semantic Service deployment and data import, see:

- [Semantic Service Deployment Guide](../installation_doc/database_install/semantic-service-deployment.md)
- [Database Service Deployment](../installation_doc/database_install/service-deployment.md)
- [Scenario Data Import](../installation_doc/database_install/scenario-data-import.md)
- [Semantic Service User Guide](../semantic_service/semantic-service-user-guide.md)

## 4. Author the Main Agent Configuration

The built-in example lives at:

```text
dataagent/core/flex/examples/nl2sql_flex_e2e_subagent.yaml
```

A main Agent that calls an NL2SQL sub-Agent has four main configuration blocks.

| Block | Role |
| --- | --- |
| `AGENT_CONFIG` | Use `type: "react"` so the main Agent follows Flex/ReAct orchestration. |
| `MODEL` | Configure at least the main Agent model and the model slot bound to the NL2SQL sub-Agent. |
| `SCENARIO` | Tell the main Agent when to call NL2SQL and which parameters to pass. |
| `TOOLS.local_functions` | Register `nl2sql_sub_agent_tool`. |
| `DATABASE` / `METAVISOR` | The main Agent holds runtime database and Semantic Service settings and overlays them onto the sub-Agent. |

Example configuration:

```yaml
AGENT_CONFIG:
  name: "NL2SQL Flex Subagent Launcher"
  type: "react"
  backend: "langgraph"
  debug: true

MODEL:
  chat_model:
    model_type: "chat"
    provider: "bailian"
    params:
      model: "Qwen3.6-Plus"
      temperature: 0.0
  qwen3_coder:
    model_type: "chat"
    provider: "bailian"
    params:
      model: "Qwen3.6-Plus"
      temperature: 0.0

SCENARIO:
  chat:
    input: "data analysis question"
    task: "complete data analysis and call NL2SQL sub-agent when SQL is required"
    instructions: |
      完成用户的数据分析任务。
      如果需要执行数据库查询，调用 nl2sql_sub_agent_tool。
      调用工具时必须明确传入 query、workspace、sql_filename 和 csv_filename。
      query 应写清楚业务目标、指标口径、过滤条件、分组粒度和输出字段。
      回答时说明生成的 SQL 文件路径和 CSV 结果文件路径。
    output_format: "回答用户问题，并说明 SQL/CSV 结果文件路径"

TOOLS:
  local_functions:
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "nl2sql_sub_agent_tool"
      description: "将自然语言数据查询交给 NL2SQL 子 Agent，并保存 SQL 与 CSV 结果。"
      config:
        llm_model: qwen3_coder

DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/demo_retail.sqlite"

METAVISOR:
  metavisor_url: "http://host:32000"
  valuematch_url: "http://host:8000"
```

Notes:

- `TOOLS.local_functions[].function` must be `nl2sql_sub_agent_tool`.
- `config.llm_model` must point to a section under main Agent `MODEL`, for example `qwen3_coder`.
- Put `DATABASE` and `METAVISOR` in the main Agent YAML; you do not need to edit the temporary sub-Agent config by hand.
- `SCENARIO.chat.instructions` must require `workspace`, `sql_filename`, and `csv_filename`; otherwise the tool cannot save result files.

## 5. Sub-Agent Configuration Overlay Logic

Inside `nl2sql_sub_agent_tool`, the following happens:

1. Load the built-in NL2SQL config: `dataagent/agents/nl2sql/nl2sql_agent.yaml`.
2. Read `DATABASE` and `METAVISOR` from the main Agent's current configuration.
3. Overlay the main Agent settings onto the temporary NL2SQL sub-Agent YAML.
4. If the tool defines `config.llm_model`, read `MODEL.<llm_model>` from the main Agent and write it into the sub-Agent.
5. Invoke `sub_agent_tool` to launch the NL2SQL sub-Agent.
6. Save SQL and query results returned by the sub-Agent under `workspace`.

Tool parameters:

| Parameter | Description |
| --- | --- |
| `query` | Natural language query passed to the NL2SQL sub-Agent. State the business goal, metrics, filters, grouping, and output columns as clearly as possible. |
| `workspace` | Directory for SQL and CSV files; must be writable. |
| `sql_filename` | Filename for generated SQL, for example `monthly_orders.sql`. |
| `csv_filename` | Filename for the result CSV, for example `monthly_orders.csv`. |

## 6. Run the Main Agent

You can run the end-to-end example in the repository:

```bash
uv run tests/e2e/test_nl2sql_flex_subagent.py
```

Or load your main Agent configuration with the SDK:

```python
import asyncio
from pathlib import Path

from dataagent.interface.sdk.agent import DataAgent


async def main():
    project_dir = Path(__file__).resolve().parents[2]
    config_path = project_dir / "dataagent" / "core" / "flex" / "examples" / "nl2sql_flex_e2e_subagent.yaml"
    agent = DataAgent.from_config(config_path)

    result = await agent.chat(
        "每月订单量是多少？请保存 SQL 和 CSV 结果。",
        initial_state={
            "run_id": 0,
            "sub_id": 0,
            "workspace": "/tmp/dataagent-nl2sql-demo",
        },
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

If your runtime uses a sandbox or workspace restrictions, ensure `workspace` is writable and the SQLite file path is within allowed access.

## 7. Inspect Results

After a successful run, the final answer should include:

- SQL file path.
- CSV result file path.
- A summary of generated SQL or query results.

You can also inspect the workspace:

```bash
ls -lh /tmp/dataagent-nl2sql-demo
```

If the tool fails, check the message for:

- `nl2sql_sub_agent_tool 工具执行失败`
- `子 Agent 执行失败`
- `Config YAML not found`
- Database connection or SQLite path errors

## 8. Comparison with a Dedicated NL2SQL Agent

| Aspect | Dedicated NL2SQL Agent | Main Agent calling NL2SQL sub-Agent |
| --- | --- | --- |
| `AGENT_CONFIG.type` | `nl2sql` | `react` |
| Primary role | Convert natural language to SQL directly. | Main Agent plans the task and calls NL2SQL when needed. |
| Config location | `DATABASE` / `METAVISOR` in the NL2SQL Agent config. | `DATABASE` / `METAVISOR` in the main Agent config, overlaid onto the sub-Agent. |
| Output shape | Returns NL2SQL state and query results. | Main Agent summarizes the answer and can save SQL/CSV files. |
| Best fit | Single database lookup questions. | Database query subtasks within multi-step workflows. |

## 9. Common Issues

### 9.1 Main Agent does not call the NL2SQL tool

Check that `SCENARIO.chat.instructions` explicitly says to call `nl2sql_sub_agent_tool` when a database query is needed, and that all four tool parameters must be passed.

### 9.2 Tool reports missing parameters

`nl2sql_sub_agent_tool` requires `query`, `workspace`, `sql_filename`, and `csv_filename`. If the model omits any of them, strengthen `SCENARIO` so filenames and the save directory are specified before the tool call.

### 9.3 Sub-Agent database configuration is wrong

Check `DATABASE` and `METAVISOR` in the main Agent YAML. The tool overlays these onto the sub-Agent, so issues usually come from the main Agent runtime config, not the temporary YAML.

### 9.4 MetaVisor connection failure

Confirm `METAVISOR.metavisor_url` and `METAVISOR.valuematch_url` are reachable, and that metadata for `DATABASE.db_id` has been imported. See [Semantic Service Deployment Guide](../installation_doc/database_install/semantic-service-deployment.md) for the full flow.

## 10. Summary

The essentials for a main Agent calling an NL2SQL sub-Agent:

1. Main Agent uses `AGENT_CONFIG.type: "react"`.
2. Register `nl2sql_sub_agent_tool`.
3. Configure `DATABASE` and `METAVISOR` in the main Agent YAML.
4. Define call boundaries and tool parameters in `SCENARIO`.
5. Use `workspace` to save SQL and CSV results for answers, auditing, and reuse.
