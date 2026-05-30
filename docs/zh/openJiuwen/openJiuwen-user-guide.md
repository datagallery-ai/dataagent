# openJiuwen 使用指南

本文说明 DataAgent 当前如何接入 openJiuwen。项目里的 openJiuwen 接入主要指 **openJiuwen 运行时后端**，负责执行 ReAct 工作流；模型调用与 LangGraph 后端相同，经 `runtime.llm` 走 litellm。

当前项目使用的 openJiuwen 版本为：

```text
openjiuwen==0.1.1
```


## 1. 使用场景

DataAgent 支持两类工作流后端：

| 后端 | 配置值 | 说明 |
| --- | --- | --- |
| LangGraph | `AGENT_CONFIG.backend: "langgraph"` | 默认后端，使用 LangGraph 执行工作流。 |
| openJiuwen | `AGENT_CONFIG.backend: "openjiuwen"` | 使用 openJiuwen 工作流引擎执行 ReAct 工作流。 |

推荐在这些场景使用 openJiuwen：

- 需要在 openJiuwen 运行时中执行 DataAgent 的 ReAct 流程。
- 需要复用 openJiuwen 的中断、恢复和流式执行机制。

不建议在这些场景使用 openJiuwen：

- NL2SQL 主流程尚未明确要求 openJiuwen 后端时，优先使用现有 `langgraph` 示例。
- 通过 `BaseDataAgent.set_base_config()` 手工组装底层节点时，当前该路径只允许 `backend="langgraph"`。

## 2. 使用的 openJiuwen 特性

DataAgent 在工作流执行、流式事件和中断恢复上接入了 openJiuwen 的能力。

| openJiuwen 特性 | DataAgent 中的使用方式 | 对用户的价值 |
| --- | --- | --- |
| openJiuwen Workflow | `backend: "openjiuwen"` 时，通过 `OpenJiuWenWorkflow` 构建并执行 ReAct 工作流。 | 同一套 DataAgent YAML 可以切换到 openJiuwen 工作流引擎运行。 |
| `WorkflowRuntime` | 每次 `ainvoke` / `astream` 时创建或复用 openJiuwen runtime，并把 DataAgent runtime 注入节点执行过程。 | 节点仍按 DataAgent 的 Planner、Executor、工具机制开发，不需要直接关心 openJiuwen runtime 差异。 |
| `Workflow.compile(runtime)` | openJiuwen workflow 在运行时绑定 runtime 后 compile。当前实现避免跨轮复用已绑定旧 runtime 的 compiled graph。 | 多轮对话中 `run_id`、`session_id`、workspace 等状态不会串到上一轮。 |
| `global_state` / `update_global_state` | DataAgent 用 openJiuwen 的 global state 承载 FlexState，并在节点执行后把 delta 合并回 global state。 | 保持 Planner、Executor、多轮消息和工具结果在 openJiuwen 后端下可持续推进。 |
| Reducer 语义适配 | 对 `messages` 等字段按追加语义合并，对带 reducer 的字段按 reducer 聚合，其他字段默认覆盖。 | 对齐 LangGraph 下的 state 更新行为，避免消息流被覆盖。 |
| `write_stream` | 在 `astream` 中接入 openJiuwen runtime 的 `write_stream`，并用旁路队列把节点事件实时 yield 给上层。 | 服务端可以持续返回模型、工具、中断等运行事件。 |
| `GraphInterrupt` | Human feedback 等节点在 openJiuwen 后端下通过 openJiuwen `GraphInterrupt` 中断工作流。 | 支持“执行到一半等待用户反馈，再继续执行”的交互流程。 |
| Checkpoint + resume | 中断时保存 `start_at`、interrupt message 和 state；恢复时注入 `__human_feedback_resume__` 并从中断节点继续。 | 用户反馈不会开启全新任务，而是在原工作流状态上继续。 |

这些特性主要分布在两层：

1. **工作流层**：`OpenJiuWenWorkflow` 负责把 DataAgent 节点包装成 openJiuwen component，并接入路由、state 合并和运行时。
2. **服务层**：服务端根据 `AGENT_CONFIG.backend` 选择 LangGraph 或 openJiuwen 分支，openJiuwen 分支负责 checkpoint 恢复和流式事件转发。

## 3. 安装依赖

openJiuwen 是可选依赖，使用 openJiuwen 后端前需要安装 `jiuwen` extra：

```bash
uv sync --extra jiuwen
```

或在临时命令中带上 extra：

```bash
uv run --extra jiuwen python your_script.py
```

`pyproject.toml` 中当前固定依赖为 `openjiuwen==0.1.1`：

```toml
[project.optional-dependencies]
jiuwen = [
    "openjiuwen==0.1.1"
]
```

## 4. YAML 配置

### 4.1 最小配置

openJiuwen 后端通过 `AGENT_CONFIG.backend` 选择：

```yaml
AGENT_CONFIG:
  name: "jiuwen demo agent"
  type: "react"
  backend: "openjiuwen"
  max_iter: 10

MODEL:
  deepseek:
    model_type: "chat"
    provider: "deepseek"
    params:
      base_url: "https://api.deepseek.com"
      model: "deepseek-chat"
      temperature: 0.1

ACTOR_LOOP:
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "deepseek"
  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"

TOOLS:
  local_functions: []
```

同一份 ReAct YAML 通常只需要把：

```yaml
AGENT_CONFIG:
  backend: "langgraph"
```

改为：

```yaml
AGENT_CONFIG:
  backend: "openjiuwen"
```

即可切换工作流后端。Planner、Executor、工具注册、Scenario、Memory 等配置仍沿用 DataAgent 的 ReAct 配置结构。

### 4.2 模型参数

`MODEL` 中的 `provider` 不是 SDK 选择器，而是平台标识，用来读取环境变量：

| 配置 | 说明 |
| --- | --- |
| `AGENT_CONFIG.backend` | 决定使用 `langgraph` 还是 `openjiuwen` 后端。 |
| `MODEL.<name>.provider` | 决定读取 `{PROVIDER}_BASE_URL`、`{PROVIDER}_API_KEY`。 |
| `MODEL.<name>.params.base_url` | 模型服务地址；优先级高于环境变量。 |
| `MODEL.<name>.params.model` | 模型名称，必填。 |
| `MODEL.<name>.params.model_provider` | litellm 透传，默认 `openai`。 |

推荐显式写 `base_url` 和 `model`，把 `api_key` 放到环境变量中：

```bash
export DEEPSEEK_API_KEY="sk-..."
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

也可以在 YAML 中直接配置：

```yaml
MODEL:
  deepseek:
    model_type: "chat"
    provider: "deepseek"
    params:
      base_url: "https://api.deepseek.com"
      api_key: "sk-..."
      model: "deepseek-chat"
      model_provider: "openai"
      temperature: 0.1
      timeout: 60
```

## 5. 运行方式

通过 `DataAgent.from_config()` 加载 openJiuwen YAML：

```python
import asyncio

from dataagent.interface.sdk.agent import DataAgent


async def main():
    agent = DataAgent.from_config("path/to/jiuwen_agent.yaml")
    result = await agent.chat(
        "帮我完成一次简单计算：先算 3 乘 2，再加 5。",
        initial_state={
            "run_id": 0,
            "sub_id": 0,
            "workspace": "/tmp/dataagent-jiuwen-demo",
        },
    )
    print(result.get("messages", [])[-1])


asyncio.run(main())
```

openJiuwen 后端执行 ReAct 时仍依赖 DataAgent 的运行时状态。调用 `chat()` 时建议提供 `initial_state.workspace`、`run_id` 和 `sub_id`，方便工具产物、子 Agent 和日志使用稳定路径。

## 6. 流式输出与服务端使用

服务端会根据 Agent 配置中的 backend 分支执行：

- `backend == "langgraph"`：使用 LangGraph checkpointer 和 `thread_id` 恢复。
- `backend == "openjiuwen"`：使用 openJiuwen workflow 的 checkpoint 机制恢复。

openJiuwen 分支中，服务端会默认设置：

```python
os.environ.setdefault("LLM_SSL_VERIFY", "false")
```

这是为了适配内网或自签证书环境。底层 litellm 客户端也会在未提供 `LLM_SSL_CERT` 时自动关闭 SSL 校验。若生产环境需要严格校验，请配置：

```bash
export LLM_SSL_CERT="/path/to/cert.pem"
export LLM_SSL_VERIFY="true"
```

openJiuwen 流式输出使用的是 openJiuwen runtime 的 `write_stream` 能力。DataAgent 在 `OpenJiuWenWorkflow.astream()` 中把 `write_stream(data)` 写入旁路队列，再由 `astream()` 持续消费并返回给上层。这样 Planner、Executor、工具执行和 human feedback 中断都可以作为流式事件输出。

## 7. 中断与恢复

openJiuwen 后端支持 human feedback 中断恢复。核心流程是：

1. 工作流节点触发 openJiuwen `GraphInterrupt`。
2. DataAgent 将其包装为 `OpenJiuWenInterrupt`。
3. 当前 state、恢复起点和中断消息写入 checkpoint store。
4. 用户提交反馈后，恢复逻辑加载 checkpoint，把反馈写入 `__human_feedback_resume__`。
5. 工作流从中断节点继续执行。

Checkpoint 目前通过 `DATABASE_URL` 选择存储：

```bash
# PostgreSQL
export DATABASE_URL="postgresql://user:password@host:5432/dbname"

# SQLite，本地轻量部署可用
export DATABASE_URL="sqlite:///./dataagent_checkpoints.db"
```

支持的后端：

| 数据库 | 说明 |
| --- | --- |
| PostgreSQL | 推荐服务端部署使用。 |
| SQLite | 适合本地、测试或轻量部署。 |

默认 checkpoint 表名是 `dataagent_checkpoints`。如需自定义表名，可配置：

```yaml
AGENT_CONFIG:
  checkpoint_postgres_table: "dataagent_checkpoints"
```

## 8. 与 LangGraph 后端的差异

| 维度 | LangGraph | openJiuwen |
| --- | --- | --- |
| 工作流构建 | 使用 LangGraph `StateGraph` 和 compiled graph。 | 使用 openJiuwen `Workflow`、component 和 runtime。 |
| 状态承载 | 由 LangGraph state schema 管理。 | 由 openJiuwen runtime `global_state` 承载，DataAgent 做 delta merge。 |
| 流式事件 | 走 LangGraph `astream`。 | 走 openJiuwen `write_stream` + DataAgent 旁路队列。 |
| 中断恢复 | 依赖 LangGraph checkpointer、thread_id 和 `Command(resume=...)`。 | 依赖 DataAgent checkpoint store，保存 `start_at` 和 global state 后从指定节点继续。 |

用户侧最重要的差异是：切到 `openjiuwen` 后，工作流状态由 openJiuwen runtime 管理；如果启用 human feedback 恢复，必须配置 `DATABASE_URL`，否则中断点无法落库。

## 9. 常见问题

### 9.1 找不到 openJiuwen

安装 `jiuwen` extra 后仍报 `ImportError: openjiuwen` 时：

```bash
uv sync --extra jiuwen
```

### 9.2 缺少 base_url 或 model

ReAct 路径要求 `env.llm_configs` 中能解析出 `api_base` 与 `model`。如果报：

```text
missing required params base_url/model for openjiuwen client
```

请检查：

- `MODEL.<name>.params.base_url` 是否配置。
- 或 `{PROVIDER}_BASE_URL` 环境变量是否存在。
- `MODEL.<name>.params.model` 是否配置。

### 9.3 缺少 API Key

API Key 优先级：

1. `MODEL.<name>.params.api_key`
2. `{PROVIDER}_API_KEY`
3. `OPENAI_API_KEY`

例如 `provider: "deepseek"` 时，会读取 `DEEPSEEK_API_KEY`。

### 9.4 SSL 证书问题

如果 litellm 因证书失败，开发和内网环境可设置：

```bash
export LLM_SSL_VERIFY="false"
```

如果需要严格校验，配置 `LLM_SSL_CERT`，不要依赖默认关闭校验。

### 9.5 BaseDataAgent 不支持 openJiuwen

`dataagent/interface/sdk/base_data_agent.py` 当前仍限制 `BaseDataAgent.set_base_config(..., backend=...)` 只能使用 `langgraph`。需要使用 openJiuwen 时，推荐通过 YAML + `DataAgent.from_config()` 加载 ReAct Agent。

## 10. 相关代码

- 后端工厂：`dataagent/core/framework_adapters/runtime/workflow_backend_factory.py`
- openJiuwen Workflow 适配：`dataagent/core/framework_adapters/runtime/workflow_openjiuwen.py`
- 统一 WorkflowBackend 接口：`dataagent/core/framework_adapters/runtime/workflow_backend.py`
- openJiuwen runtime context：`dataagent/core/framework_adapters/runtime/context.py`
- PostgreSQL checkpoint：`dataagent/core/framework_adapters/checkpoints/postgres_store.py`
- SQLite checkpoint：`dataagent/core/framework_adapters/checkpoints/sqlite_store.py`
