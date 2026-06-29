# Application Cases

These guides show how to use DataAgent in different scenarios. **Not every case requires Semantic Service**—only database semantic / NL2SQL cases need deployment and scenario data import first.

## Prerequisites

| Case | Semantic Service required? | Read first |
| --- | --- | --- |
| [Build a dedicated NL2SQL Agent](build-an-nl2sql-application.md) | **Yes** | [Quick Start §8](../quick_start/quick_start.md#optional-semantic-service) → [Semantic Service deployment](../installation_doc/database_install/semantic-service-deployment.md) → [Scenario data import](../installation_doc/database_install/scenario-data-import.md) |
| [Build a data analysis Agent](build-a-dataagent-from-scratch.md) | **Yes** (when the main Agent calls the NL2SQL sub-Agent) | Same as above |

!!! note "Demo business database"
    `demo_retail.sqlite` in the scenario tutorial is a sample database created at runtime—it is **not** bundled with the Semantic Layer service package. Agent reads it via `DATABASE.config.path` (absolute path); Semantic Service stores metadata only.

## Case list

1. [Build a data analysis Agent](build-a-dataagent-from-scratch.md) — ReAct main Agent calls NL2SQL sub-Agent on demand
2. [Build a dedicated NL2SQL Agent](build-an-nl2sql-application.md) — Dedicated NL2SQL Agent for natural-language queries

## Sample questions after the demo

After [Scenario data import](../installation_doc/database_install/scenario-data-import.md) and Agent configuration, try:

- City-level GMV ranking (e.g. 各城市成交额排名)
- Monthly order count (e.g. 每月订单量是多少)
