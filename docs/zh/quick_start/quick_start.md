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
