# 构建数据分析 Agent

本文说明如何构建一个 ReAct 主 Agent，并在需要查数据库时调用 NL2SQL 子 Agent。这个案例适合“用户任务不只是查数”，但其中某一步需要自然语言转 SQL 的场景。

和 [构建 NL2SQL 专用 Agent](build-an-nl2sql-application.md) 不同，本案例中的主 Agent 负责理解任务、组织步骤和汇总回答；NL2SQL 只作为一个工具能力被按需调用。

## 1. 适用场景

| 适用 | 不适用 |
| --- | --- |
| 用户任务需要先理解目标，再决定是否查询数据库。 | 用户问题本身就是单次查库。 |
| 主 Agent 还需要组织最终回答、保存 SQL/CSV 文件或和其他工具协作。 | 只想验证某个数据库的 NL2SQL 效果。 |
| 希望把 NL2SQL 封装成一个可复用工具，由主 Agent 控制调用时机。 | 不需要 ReAct 编排，只需要 SQL 生成和执行。 |

典型问题示例：

```text
统计高价值客户最近一个季度的购买金额变化，并把 SQL 和结果文件保存下来。
```

主 Agent 需要明确任务目标、调用 `nl2sql_sub_agent_tool`、拿到 SQL/CSV 文件路径，然后组织最终回答。

## 2. 整体架构

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

这个模式的关键点是：`DATABASE` 和 `METAVISOR` 配在主 Agent YAML 中，工具运行时会把它们覆盖到临时 NL2SQL 子 Agent 配置里。因此，同一个 NL2SQL 子 Agent 能被不同业务主 Agent 复用。

## 3. 准备工作

开始前确认以下内容：

1. 已完成项目安装，并能在仓库根目录执行 `uv run ...`。
2. 已配置模型环境变量，例如 `BAILIAN_BASE_URL` 和 `BAILIAN_API_KEY`。
3. 已准备业务数据库。SQLite 场景建议使用绝对路径。
4. 已完成 MetaVisor/Semantic Service 部署和元数据导入。
5. 已准备可写的 workspace，用于保存子 Agent 生成的 SQL 和 CSV 文件。

MetaVisor/Semantic Service 的部署和数据导入请参考：

- [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md)
- [数据库服务部署](../installation_doc/database_install/service-deployment.md)
- [场景数据导入](../installation_doc/database_install/scenario-data-import.md)
- [Semantic Service 使用指南](../semantic_service/semantic-service-user-guide.md)

## 4. 编写主 Agent 配置

仓库内置示例位于：

```text
dataagent/core/flex/examples/nl2sql_flex_e2e_subagent.yaml
```

一个主 Agent 调 NL2SQL 子 Agent 的配置主要包含四部分。

| 配置块 | 作用 |
| --- | --- |
| `AGENT_CONFIG` | 使用 `type: "react"`，让主 Agent 走 Flex/ReAct 编排。 |
| `MODEL` | 至少配置主 Agent 使用的模型，以及 NL2SQL 子 Agent 绑定的模型槽位。 |
| `SCENARIO` | 告诉主 Agent 何时调用 NL2SQL，以及调用时要传哪些参数。 |
| `TOOLS.local_functions` | 注册 `nl2sql_sub_agent_tool`。 |
| `DATABASE` / `METAVISOR` | 主 Agent 持有运行时数据库和 Semantic Service 配置，并覆盖给子 Agent。 |

示例配置：

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

注意：

- `TOOLS.local_functions[].function` 必须是 `nl2sql_sub_agent_tool`。
- `config.llm_model` 指向主 Agent `MODEL` 中的一个 section，例如 `qwen3_coder`。
- `DATABASE` 和 `METAVISOR` 写在主 Agent YAML 中，不需要手工改临时子 Agent 配置。
- `SCENARIO.chat.instructions` 要明确要求传入 `workspace`、`sql_filename` 和 `csv_filename`，否则工具无法保存结果文件。

## 5. 子 Agent 配置覆盖逻辑

`nl2sql_sub_agent_tool` 内部会做以下事情：

1. 读取内置 NL2SQL 配置：`dataagent/agents/nl2sql/nl2sql_agent.yaml`。
2. 从主 Agent 当前配置中读取 `DATABASE` 和 `METAVISOR`。
3. 用主 Agent 的配置覆盖临时 NL2SQL 子 Agent YAML。
4. 如果工具配置了 `config.llm_model`，从主 Agent 的 `MODEL.<llm_model>` 读取模型配置，并写入子 Agent。
5. 调用 `sub_agent_tool` 拉起 NL2SQL 子 Agent。
6. 将子 Agent 返回的 SQL 和查询结果保存到 `workspace` 下。

工具参数如下：

| 参数 | 说明 |
| --- | --- |
| `query` | 交给 NL2SQL 子 Agent 的自然语言查询。应尽量明确业务目标、指标、过滤条件、分组和输出字段。 |
| `workspace` | SQL 和 CSV 文件保存目录，必须是可写路径。 |
| `sql_filename` | 生成 SQL 的文件名，例如 `monthly_orders.sql`。 |
| `csv_filename` | 查询结果 CSV 的文件名，例如 `monthly_orders.csv`。 |

## 6. 运行主 Agent

可以直接运行仓库中的端到端示例：

```bash
uv run tests/e2e/test_nl2sql_flex_subagent.py
```

也可以用 SDK 加载你的主 Agent 配置：

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

如果你的运行环境启用了沙箱或 workspace 限制，确保 `workspace` 可写，并确保 SQLite 文件路径在允许访问范围内。

## 7. 查看结果

正常执行后，最终回答中应包含：

- SQL 文件路径。
- CSV 结果文件路径。
- 生成的 SQL 或查询结果摘要。

同时可以检查 workspace：

```bash
ls -lh /tmp/dataagent-nl2sql-demo
```

如果工具执行失败，优先查看消息中是否出现：

- `nl2sql_sub_agent_tool 工具执行失败`
- `子 Agent 执行失败`
- `Config YAML not found`
- 数据库连接或 SQLite 文件路径错误

## 8. 和专用 NL2SQL Agent 的区别

| 对比项 | 专用 NL2SQL Agent | 主 Agent 调 NL2SQL 子 Agent |
| --- | --- | --- |
| `AGENT_CONFIG.type` | `nl2sql` | `react` |
| 主要职责 | 直接完成自然语言转 SQL。 | 主 Agent 规划任务，必要时调用 NL2SQL。 |
| 配置位置 | `DATABASE` / `METAVISOR` 写在 NL2SQL Agent 配置中。 | `DATABASE` / `METAVISOR` 写在主 Agent 配置中，再覆盖给子 Agent。 |
| 输出形态 | 返回 NL2SQL 状态和查询结果。 | 主 Agent 汇总回答，并可保存 SQL/CSV 文件。 |
| 适合场景 | 单一查库问题。 | 多步骤任务中的数据库查询子任务。 |

## 9. 常见问题

### 9.1 主 Agent 没有调用 NL2SQL 工具

检查 `SCENARIO.chat.instructions` 是否明确写了“需要数据库查询时调用 `nl2sql_sub_agent_tool`”，并要求传入四个工具参数。

### 9.2 工具提示缺少参数

`nl2sql_sub_agent_tool` 需要 `query`、`workspace`、`sql_filename` 和 `csv_filename`。如果模型没有传全，补强 `SCENARIO`，让它在调用工具前明确文件名和保存目录。

### 9.3 子 Agent 数据库配置不对

检查主 Agent YAML 中的 `DATABASE` 和 `METAVISOR`。工具会用主 Agent 的这两段配置覆盖子 Agent，因此问题通常不在临时 YAML，而在主 Agent 的运行时配置。

### 9.4 MetaVisor 连接失败

先确认 `METAVISOR.metavisor_url` 和 `METAVISOR.valuematch_url` 可访问，再确认 `DATABASE.db_id` 已完成元数据导入。完整流程请参考 [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md)。

## 10. 小结

主 Agent 调 NL2SQL 子 Agent 的关键是：

1. 主 Agent 使用 `AGENT_CONFIG.type: "react"`。
2. 注册 `nl2sql_sub_agent_tool`。
3. 在主 Agent YAML 中配置 `DATABASE` 和 `METAVISOR`。
4. 在 `SCENARIO` 中明确调用边界和工具参数。
5. 使用 `workspace` 保存 SQL 和 CSV 结果，方便后续回答、审计和复用。
