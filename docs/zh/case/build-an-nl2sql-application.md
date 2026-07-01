# 构建 NL2SQL 专用 Agent

本文以一个“自然语言查数据库”的场景为例，说明如何构建一个只负责 NL2SQL 的专用 Agent。这个案例适合用户问题本身就是查数、统计、分组、排序或筛选，不需要主 Agent 再做复杂任务规划的场景。

NL2SQL 专用 Agent 的重点不是工具编排，而是把自然语言问题转成 SQL，并完成校验、执行和结果返回。

## 1. 适用场景

| 适用 | 不适用 |
| --- | --- |
| 用户问题可以直接落到数据库查询。 | 任务需要先拆解多个步骤，再决定是否查数据库。 |
| 只需要一个 NL2SQL 链路完成 schema 感知、SQL 生成、执行与结果返回。 | 任务需要调用多个工具，例如查文件、生成报告、调用外部 API 后再查数据库。 |
| 希望用固定配置验证某个业务库的 NL2SQL 效果。 | 希望把 NL2SQL 作为主 Agent 的一个能力按需调用。 |

如果你的目标是“主 Agent 先理解任务，只有遇到数据库查询时才调用 NL2SQL”，请看另一个案例：[构建数据分析 Agent](build-a-dataagent-from-scratch.md)。

## 2. 整体架构

专用 NL2SQL Agent 的运行链路如下：

```text
用户自然语言问题
      │
      ▼
NL2SQLAgent（AGENT_CONFIG.type = nl2sql）
      │
      ├─ Perceptor：读取数据库 schema、字段语义、join 信息
      ├─ Generator：生成候选 SQL
      ├─ Validator：做 SQL explain、关键词或元数据校验
      ├─ Executor：执行 SQL，返回结果
      ├─ Reflector：必要时反思修正
      └─ Selector：选择最终 SQL 与结果
```

其中 Semantic Service 提供表、字段、字段描述、join 关系和语义检索等增强元数据。部署和数据导入请参考（**NL2SQL 案例必需**）：

- [快速开始 §8：可选接入数据库语义服务](../quick_start/quick_start.md#optional-semantic-service)
- [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md)
- [场景数据导入](../installation_doc/database_install/scenario-data-import.md)
- [Semantic Service 使用指南](../semantic_service/semantic-service-user-guide.md)

完成部署后，你需要拿到两个关键信息：

| 信息 | 用途 |
| --- | --- |
| `DATABASE.db_id` | Semantic Service 中导入的数据库标识。 |
| `SEMANTIC_LAYER.base_url` | Semantic Service REST 服务地址。 |

## 3. 准备工作

开始前确认以下内容：

1. 已完成项目安装，并能在仓库根目录执行 `uv run ...`。
2. 已配置模型环境变量，例如 `BAILIAN_BASE_URL` 和 `BAILIAN_API_KEY`。
3. **（必需）** 已完成 Semantic Service 部署与场景数据导入（NL2SQL 依赖外部语义服务）：
   - [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md)
   - [场景数据导入](../installation_doc/database_install/scenario-data-import.md)
4. 已准备 demo SQLite 业务库，并在 Agent 配置中使用**绝对路径**（示例逻辑库 `demo_db`，文件由教程创建，非服务包自带）。
5. 确认 `SEMANTIC_LAYER.base_url` 可访问，且 `DATABASE.db_id` 与元数据 `databaseName` 一致。

若你尚未部署 Semantic Service，请从 [快速开始 §8](../quick_start/quick_start.md#optional-semantic-service) 的可选路径开始。

示例配置中的 SQLite 路径与 Semantic Service 连接（字段与仓库内置 YAML 一致，值按 demo 场景替换）：

```yaml
DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/data/demo_retail.sqlite"

SEMANTIC_LAYER:
  base_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  timeout: 30
  verify_ssl: false
```

跑通后可尝试的验证问题：

- 「各城市成交额排名」
- 「每月订单量是多少」

Semantic Service 能力说明见 [Semantic Service 使用指南](../semantic_service/semantic-service-user-guide.md)。

## 4. 编写 NL2SQL Agent 配置

仓库内置配置位于：

```text
dataagent/agents/nl2sql/nl2sql_agent.yaml
```

你可以直接修改该文件，也可以复制一份作为自己的业务配置。核心配置包括五部分。

| 配置块 | 作用 |
| --- | --- |
| `AGENT_CONFIG` | 指定 Agent 类型。专用 NL2SQL 必须使用 `type: "nl2sql"`。 |
| `MODEL` | 指定用于 SQL 生成和修正的 chat 模型。 |
| `CORE` | 配置 NL2SQL 内部节点和阈值。 |
| `DATABASE` | 指定数据库标识、数据库类型和连接参数。 |
| `SEMANTIC_LAYER` | 指定 Semantic Service REST 服务地址、认证和超时配置。 |

示例配置（结构与仓库 `dataagent/agents/nl2sql/nl2sql_agent.yaml` 一致；`demo_db` 与路径按场景教程替换）：

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

SEMANTIC_LAYER:
  base_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  timeout: 30
  verify_ssl: false
```

配置时重点检查：

- `DATABASE.db_id` 必须和 Semantic Service 中导入的数据库标识一致。
- `DATABASE.engine` 要和实际数据库一致，例如 `sqlite`、`mysql`、`postgres`。
- SQLite 的 `DATABASE.config.path` 建议使用绝对路径，避免从不同目录启动时找不到文件。
- 模型的 `api_key` 不建议写入 YAML，优先放到 `.env` 中。
- `SEMANTIC_LAYER.base_url` 指向已部署的 Semantic Service；`username` / `password` 按实际部署填写，无认证环境可不配置。

## 5. 运行专用 Agent

可以通过 SDK 直接加载配置并调用：

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

仓库中也提供了端到端示例脚本：

```bash
uv run tests/e2e/test_nl2sql.py
```

如果你复制了自己的 YAML，可以把脚本中的 `config_path` 改成你的配置文件路径。

## 6. 查看结果

`agent.chat()` 返回最终状态。排查 NL2SQL 效果时，优先查看这些字段：

| 字段 | 说明 |
| --- | --- |
| `messages` | 消息流和最终回答。 |
| `sql` | 最终选择的 SQL。 |
| `columns` / `rows` / `rows_preview` | 查询结果字段、完整结果和预览结果。 |
| `generation_results` | 候选 SQL 生成结果。 |
| `validation_results` | SQL 校验结果。 |
| `execution_results` | SQL 执行结果。 |
| `confidence` | Selector 给出的结果置信度。 |

如果返回结果为空，先判断是 SQL 没生成、SQL 执行失败，还是数据库本身没有匹配数据。

## 7. 常见问题

### 7.1 模型 Key 没读到

检查运行目录下是否存在 `.env`，并确认变量名和 `provider` 对应。例如 `provider: "bailian"` 会读取 `BAILIAN_BASE_URL` 和 `BAILIAN_API_KEY`。

### 7.2 SQLite 文件找不到

优先把 `DATABASE.config.path` 写成绝对路径。相对路径会跟随当前执行命令的工作目录变化。

### 7.3 Semantic Service 连接失败

先用 `curl` 验证 `SEMANTIC_LAYER.base_url` 是否可访问，再确认 `DATABASE.db_id` 是否已经导入 Semantic Service。完整部署、初始化和导入流程请参考 [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md)。

### 7.4 生成 SQL 和业务口径不一致

把用户问题写得更明确，补充实体定义、指标公式、过滤条件、统计粒度和排序要求。NL2SQL Agent 适合处理明确的查询问题，不适合替用户补全缺失的业务规则。

## 8. 小结

专用 NL2SQL Agent 的关键是：

1. `AGENT_CONFIG.type` 使用 `nl2sql`。
2. `DATABASE` 指向真实业务库。
3. `SEMANTIC_LAYER` 指向已完成元数据导入的 Semantic Service。
4. 用户问题尽量明确业务对象、指标口径和查询条件。

当你需要让一个主 Agent 同时处理任务规划、报告组织和按需查库时，不要把这些逻辑塞进专用 NL2SQL Agent，而应使用主 Agent 调用 NL2SQL 子 Agent 的模式。
