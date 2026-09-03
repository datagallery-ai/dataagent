---
hide:
  - navigation
---

# 快速开始

DataAgent 主 Agent 现已基于 LangGraph 与 Deep Agents 运行。原有 YAML 的模型、工具、工作区、场景和上下文配置仍是配置边界，不再需要自定义工作流节点拓扑。

## 1. 安装并配置模型

在仓库根目录执行：

```bash
uv sync
cp .env.example .env
```

在 `.env` 中配置所选模型供应商的凭据。仓库自带 quickstart 使用 `DEEPSEEK_API_KEY`，并可选读取 `DEEPSEEK_BASE_URL`。

## 2. 运行内置 quickstart

```bash
uv run -m dataagent quickstart
```

也可以显式启动同一份 YAML：

```bash
uv run -m dataagent --config dataagent/examples/quickstart.yaml
```

## 3. 编写兼容 YAML

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  version: "1.0"
  description: "数据分析助手"
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
      你是数据分析助手。直接回答问题，需要时使用已注册工具。

TOOLS:
  local_functions:
    - module: "my_tools"
      function: "lookup_metric"
```

按约定，`MODEL.chat_model` 是主模型；配置多个聊天模型时，可用 `AGENT_CONFIG.primary_model` 指定模型槽位。`provider` 忽略大小写，OpenAI 兼容供应商统一使用 LangChain 的 `ChatOpenAI` 实现。

未配置 `WORKSPACE.path` 时，文件系统工作区默认位于 `.dataagent/<user_id>/<session_id>`。`allow_path` 中的目录以只读方式挂载。设置 `WORKSPACE.backend: state` 后，文件存入 Agent 状态；由于没有宿主机目录，Shell 工具会被禁用。

启动前可以先检查 YAML：

```bash
uv run -m dataagent config check config.yaml
uv run -m dataagent --config config.yaml
```

## 4. Python SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("config.yaml")

result = await agent.chat("分析销售趋势", session_id="demo-session")
print(result["messages"][-1].content)

async for event in agent.astream(
    {"messages": [("user", "生成一份简短报告")]},
    session_id="demo-session",
    stream_mode="values",
):
    print(event)
```

`chat()` 直接返回 LangGraph 原生状态，`astream()` 直接透传 LangGraph 原生流模式与事件。两者都可以通过 `config=` 传入 LangGraph `RunnableConfig`；DataAgent 会补充会话隔离所需的 `thread_id`，并保留其他运行参数。

## 5. 可选能力

- 通过 `TOOLS.local_functions` 注册同步或异步本地函数。
- 通过 `TOOLS.mcp_servers` 使用官方 MCP 适配器，通过 `TOOLS.A2A` 接入远程 Agent。
- 在 `TOOLS.skills` 下配置 Deep Agents 原生 Skills。
- 用 `SUBAGENTS` 注册通用子 Agent，或用一个内联 `NL2SQL` 段配置原生 NL2SQL 子 Agent。
- 用 `AGENT_CONFIG.enable_human_feedback` 开启用户澄清，并可配置 `SCENARIO.chat.human_feedback_conditions`。
- 继续使用旧 Suite 声明；ConfigManager 会先把 Suite 展开成普通 YAML，再交给 Deep Agent compiler。

完整配置面见 [Python SDK](../api_doc/PythonSDK.md)。

## 6. A2A 服务

```bash
uv run -m dataagent serve-a2a \
  --config config.yaml \
  --host 0.0.0.0 \
  --port 9999 \
  --auth-token your_token
```

## 7. 可选 Semantic Service

通用对话和工具调用不依赖 Semantic Service。NL2SQL 或表字段语义检索需要数据库元数据时，再按 [Semantic Service 部署指南](../installation_doc/database_install/semantic-service-deployment.md) 部署，然后参考 [构建 NL2SQL 专用 Agent](../case/build-an-nl2sql-application.md)。
