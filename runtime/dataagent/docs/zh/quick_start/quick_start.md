---
hide:
  - navigation
---

## 快速开始

本页用于快速跑通一个 DataAgent Flex/ReAct Agent。

## 1. 准备环境

在仓库根目录执行：

```bash
uv sync
```

参考 `.env.example` 生成 `.env`，把模型 API Key、服务地址等环境差异项放到 `.env` 中：

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入实际的配置值。

## 2. 交互式快速启动

```bash
uv run -m dataagent quickstart
```

该命令用于快速验证安装、配置加载和基础 Agent 流程。按提示输入模型配置后即可开始与 Agent 对话！

## 3. 使用配置文件启动

创建 `config.yaml`：

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  version: "1.0"
  description: "数据分析 Agent"
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
      你是一个数据分析助手。根据用户问题直接回答；需要工具时，只使用已注册工具。

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

启动 Agent：

```bash
# 终端交互模式
uv run -m dataagent --config config.yaml
```

## 4. 配置检查

```bash
# 检查配置文件中的环境变量引用
uv run -m dataagent config check config.yaml
```

## 5. Python SDK 调用

```python
from dataagent import DataAgent

agent = DataAgent.from_config("config.yaml")

# 单轮对话
response = await agent.chat("分析最近一周的销售数据趋势")
print(response)

# 流式对话
async for chunk in agent.astream(input={"user_query": "生成用户报告"}):
    print(chunk, end="", flush=True)
```

## 6. A2A 1.0 服务模式

```bash
# 启动 A2A 服务器
uv run -m dataagent serve-a2a \
  --config config.yaml \
  --host 0.0.0.0 \
  --port 9999 \
  --auth-token your_token

# 服务地址
# ├── 🌟 AgentCard: http://localhost:9999/.well-known/agent.json
# ├── 📡 JSON-RPC:  http://localhost:9999/a2a/jsonrpc
# └── 🔌 REST:      http://localhost:9999/a2a/rest
```

## 7. 更多示例

可继续参考仓库内示例配置：

```
dataagent/core/flex/examples/
```

## 8. 可选：接入数据库语义服务 {#optional-semantic-service}

Semantic Service（Semantic Layer REST 服务）是 DataAgent 的**外部可选组件**，不是启动 Agent 的必选依赖。完成上文步骤后，你已经可以运行 Flex/ReAct Agent、调用 SDK 或启动 A2A 服务。

当你需要以下能力时，再部署 Semantic Service 并导入场景数据：

| 场景 | 是否需要 Semantic Service |
| --- | --- |
| 交互式对话、通用工具调用 | 否 |
| NL2SQL：自然语言查库、生成 SQL | 是 |
| 表/字段语义检索、JOIN 路径、SQL Few-shot | 是 |
| 向量语义搜索（表描述、列描述召回） | 是（需启用向量模型） |

### 8.1 推荐阅读顺序

按下列顺序完成语义层试用链路（约 30–60 分钟，含模型下载）：

| 步骤 | 文档 | 做什么 |
| --- | --- | --- |
| 1 | [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md) | 下载服务包、启动 PostgreSQL/pgvector、配置并启动 REST 服务 |
| 2 | [场景数据导入](../installation_doc/database_install/scenario-data-import.md) | 创建 demo 业务库、导入元数据、验证检索 API |
| 3 | [构建 NL2SQL 专用 Agent](../case/build-an-nl2sql-application.md) | 配置 `DATABASE` / `SEMANTIC_LAYER` 并运行 NL2SQL |
| 4 | [构建数据分析 Agent](../case/build-a-dataagent-from-scratch.md) | 主 Agent 按需调用 NL2SQL 子 Agent |

!!! note "关于 demo 业务库"
    场景教程中的 `demo_retail.sqlite` 是运行时创建的**示例业务数据库**，不是 Semantic Layer 服务包自带内容。Semantic Service 只保存该库的元数据（表、字段、关系等），真实数据仍由 Agent 的 `DATABASE.config.path` 指向 SQLite 文件。
