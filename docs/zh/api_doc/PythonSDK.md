# Python SDK 接口

DataAgent 是 DataAgent 框架向用户暴露的核心 Python SDK 入口，通过 `from_config` 加载 YAML 配置实例化 Agent，再通过 `chat` 或 `astream` 进行对话。

---

## DataAgent.from_config

**接口定义**

```python
class DataAgent:
    @classmethod
    def from_config(cls, config: str | Path) -> "DataAgent":
        ...
```

从 YAML 配置文件创建 Agent 实例。配置文件路径可以是绝对路径或相对路径。

**参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `config` | `str \| Path` | YAML 配置文件路径（必填） |

**返回值**

`DataAgent` 实例。

**示例**

```python
from dataagent.interface.sdk.agent import DataAgent

agent = DataAgent.from_config("path/to/ecommerce_agent.yaml")
```

---

## DataAgent.chat

**接口定义**

```python
async def chat(
    self,
    user_query: str,
    session_id: str | None = None,
    workspace: Path | str | None = None,
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ...
```

触发 Agent 单轮对话。`debug=True`（默认）时，对话日志通过 Rich 渲染器在终端输出流式中间结果。

**参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `user_query` | `str` | 必填 | 用户输入的查询文本 |
| `session_id` | `str \| None` | `None` | 会话 ID。不传时优先取 `initial_state` 中的 `session_id`；若仍未提供，则为本次调用自动生成新的 ID |
| `workspace` | `Path \| str \| None` | `None` | 工作目录覆盖。传入后覆盖配置文件中的工作目录设置 |
| `initial_state` | `dict \| None` | `None` | 初始状态字典，可携带 `user_id`、`session_id`、`messages` 等字段 |

**返回值**

`dict[str, Any]` — 最终 state 字典。核心字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages` | `list` | 本轮对话的完整消息历史 |
| `final_answer` | `str` | 仅错误时存在，包含错误描述 |
| `complete` | `bool` | 对话是否正常结束 |
| `user_query` | `str` | 用户原始查询 |
| `error` | `str` | 仅异常时存在，异常信息字符串 |

**示例**

```python
response = await agent.chat("上个月销售额最高的产品是什么？")
# 正常时从 messages 中提取最终回答
if "messages" in response:
    last_msg = response["messages"][-1]
    print(last_msg.content)
```

---

## DataAgent.astream

**接口定义**

```python
def astream(self, *args, **kwargs):
    ...
```

触发 Agent 流式对话，通过异步生成器逐条产出事件，适用于 Web 前后端交互场景。

**参数**

支持两种调用方式：

1. **LangGraph 原生调用**：`astream(input={...}, config={...}, stream_mode=...)`
2. **openJiuwen 调用**：`astream(initial_state={...}, start_at=..., checkpoint_id=...)`

两种方式均支持通过 `initial_state` 传入 `session_id` 和 `workspace` 等状态字段。

**返回值**

`AsyncGenerator` — 异步生成器，逐条产出 `(stream_mode, event_data)` 元组：
- `stream_mode="values"` 时，`event_data` 为当前完整 state
- `stream_mode="updates"` 时，`event_data` 为增量更新
- `stream_mode="custom"` 时，`event_data` 为自定义事件（如 Rich 渲染事件）

**示例**

```python
async for mode, data in agent.astream(input={"messages": [("human", "分析客户数据")]}):
    if mode == "values":
        print(data)
```

---

# YAML 配置详解

以下按模块展示完整的 YAML 配置结构。所有字段均基于代码实际行为，未标注"可选"的字段为必填。

## AGENT_CONFIG — Agent 基础配置

```yaml
AGENT_CONFIG:
  name: "电商分析Agent"                 # Agent 名称
  type: "react"                        # Agent 引擎类型：react (FlexAgent) | nl2sql (NL2SQLAgent)
  backend: "langgraph"                 # 后端引擎，默认 "langgraph"
  max_iter: 50                         # 最大迭代次数，不设则不限制
  token_limit: 100000                  # token 上限，不设则不限制
  enable_human_feedback: false         # 是否启用 HITL 人机协同，默认 false
  enable_portrait: false               # 是否启用用户画像记忆，默认 false
```

**代码行为**：
- `type` 决定 `select_engine()` 的引擎选择，可选 `react`（`dataagent.core.flex.agent.FlexAgent`）或 `nl2sql`（`dataagent.agents.nl2sql.agent.NL2SQLAgent`）
- `max_iter` 非空时写入 `FlexRouter`，超出限制时抛出 `LimitReachedError`，返回当前 state 并追加终止消息
- `enable_human_feedback=true` 会创建 `HumanFeedbackNode` 并注册 `request_human_feedback` 工具
- `enable_portrait=true` 会通过 portraiter hook 将用户特征写入 Memory

---

## MODEL — 模型配置

```yaml
MODEL:
  deepseek:                            # 模型槽名（Planner 通过 chat_model.name 引用）
    name: "DEEPSEEK_CHAT"              # 模型标识名
    model_type: "chat"                 # chat | embedding
    provider: "deepseek"               # 平台标识，用于读取 DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY 环境变量
    tool_call_mode: "native"           # 工具调用模式，默认 "native"
    params:
      model: "deepseek-chat"           # 实际模型名（传给 litellm）
      temperature: 0.7
      max_tokens: 8192
      timeout: 90
      max_retries: 3

  qwen3:                               # 辅助模型槽（给 hook 或独立节点使用）
    name: "QWEN3_CHAT"
    model_type: "chat"
    provider: "openai"                 # 兼容 OpenAI 协议的服务
    params:
      model: "qwen3-235b"
      temperature: 0.3
```

**代码行为**：
- 每个模型槽为一个 dict，key 如 `deepseek` 即为槽名
- `provider` 大写后用于拼接环境变量：`{PROVIDER}_BASE_URL` 和 `{PROVIDER}_API_KEY`。模型的实际 `API_KEY` 和 `BASE_URL` 通过.env文件注入
- `params.model` 为必填，其余参数（temperature, max_tokens 等）可选，直传 litellm
- 节点通过 `chat_model.name` 引用模型槽名，合并后写入 `AgentEnv.llm_configs`
- 未被子节点引用的模型槽也会纳入 `llm_configs`，供 hook 通过 `runtime.llm("<槽名>")` 使用

---

## SCENARIO — 场景描述

```yaml
SCENARIO:
  chat:                                # 场景模式 key，对应 mode="chat"
    instructions: |
      你是一个专业的数据分析助手。
      优先使用可用工具获取真实数据；无法确定时说明缺失信息。
      回答需基于实际查询结果，不得编造数据。
```

**代码行为**：
- `instructions` 写入 `AgentEnv.instructions`，供 Planner 节点的 prompt template 使用

---

## ACTOR_LOOP — 工作流节点

```yaml
=
ACTOR_LOOP:                            # 主循环工作流（必填，至少一个节点）
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "deepseek"                 # 引用 MODEL.deepseek
    prompt_template:                   # 可选，追加 prompt
      system:                          # 仅支持 system / user
        content: "额外注入到 system prompt 的文本（Jinja2 模板）"

  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"
    max_tool_result_length: 8192       # 工具结果最大长度（截断）
    max_concurrency: 5                 # 工具调用最大并发数

```

**代码行为**：
- `FlexAgent._create_nodes_from_config` 动态 `import` 每个节点的 `module`，用 `node` 作为节点名
- 保留键（`node`、`module`、`chat_model`、`prompt_template`）不传入构造函数；其余键值一概作为 `**kwargs` 传入
- `chat_model` 可为字符串（简写为 name）或 dict（含 `name` 键）
- `prompt_template` 仅支持 `system` / `user` 两个 message_type，每个含 `content`（内联）或 `path`（绝对路径）二选一
- FlexRouter 在 ACTOR_LOOP 节点间循环，直到 state.complete 为 True 或达到 max_iter

---

## TOOLS — 工具配置

```yaml
TOOLS:
  local_functions:                     # 自定义本地 Python 函数工具
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_sql"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_plot"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "report_generator"

  mcp_servers:                         # MCP 服务端工具
    - name: "my_mcp_server"
      url: "http://localhost:8000/mcp"

  A2A:                                 # Agent-to-Agent 协议工具
    - name: "other_agent"
      url: "http://localhost:9000/a2a"

  builtin:                             # 不需配置，内置工具覆盖（默认注册下方 6 个）
    - module: "dataagent.actions.tools.local_tool.bash_tool"
      function: "bash"
    - module: "dataagent.actions.tools.local_tool.file_tools"
      function: "edit_file"
    - module: "dataagent.actions.tools.local_tool.file_tools"
      function: "read_file"
    - module: "dataagent.actions.tools.local_tool.file_tools"
      function: "write_file"
    - module: "dataagent.actions.tools.local_tool.search_tools"
      function: "grep"
    - module: "dataagent.actions.tools.local_tool.search_tools"
      function: "glob"
```

**代码行为**：
- 默认注册 6 个内置工具：`bash`、`edit_file`、`read_file`、`write_file`、`grep`、`glob`
- 配了 `TOOLS.builtin` 会覆盖默认列表
- `local_functions` 中每个条目通过 `module` + `function` 动态 import 并注册
- `mcp_servers` 会启动 MCP 客户端连接并自动发现工具
- `A2A` 注册远端 Agent 的工具
- 内置 Skill `data_analysis_report` 默认激活（`dataagent/actions/skills/data_analysis_report/`）
- 所有工具注册到 `ToolManager`，executor 通过 `runtime.tool_manager` 调用

---

## CONTEXT — 上下文管理

```yaml
CONTEXT:
  compress_token_limit: 32768          # 消息 token 超过此值 ×1.2 时触发 LLM 折叠压缩
  compress_message_cnt: 200            # 消息数量超过此值时触发压缩
  file_node_threshold: 500             # IR 转换中长文本落盘为 FileNode 的最小字符阈值
```

**代码行为**：
- 三项均为可选，不配则不限制
- `compress_token_limit` 的实际触发阈值为 `compress_token_limit * 1.2`

---

## WORKSPACE — 工作目录

```yaml
WORKSPACE:
  path: "/data/agent_workspace"        # Agent 工作根目录（必填时写绝对路径）
  allow_path:                          # 白名单目录（Bash 工具只能访问这些路径）
    - "/data/shared"
    - "/home/user/datasets"
```

**代码行为**：
- `path` 和 `allow_path` 中的路径必须为绝对路径（支持 `~/`）
- `ConfigManager._validate_workspace_yaml_config` 在配置加载时校验
- `allow_path` 须为列表，不能是单个字符串

---

## BASH_TOOL_WHITELIST — Bash 命令白名单

```yaml
BASH_TOOL_WHITELIST:
  - ls
  - cat
  - head
  - python
  - pip
```

**代码行为**：
- 配置后仅允许列表中的命令在 Bash 工具中执行
- 未配或为 null 时不限制

---

# 完整示例

以下是一个可直接使用的完整 YAML 配置：

```yaml
AGENT_CONFIG:
  name: "电商数据分析Agent"
  type: "react"
  backend: "langgraph"
  max_iter: 50

MODEL:
  deepseek:
    name: "DEEPSEEK_CHAT"
    model_type: "chat"
    provider: "deepseek"
    params:
      model: "deepseek-chat"
      temperature: 0.7
      max_tokens: 8192
      timeout: 90
      max_retries: 3

  jina_v3:
    name: "jina_v3"
    model_type: "embedding"
    provider: "embedding"
    params:
      model: "jina-embeddings-v3"

SCENARIO:
  chat:
    instructions: |
      你是电商数据分析助手。优先使用工具获取真实数据；无法确定时说明缺失信息。

ACTOR_LOOP:
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "deepseek"

  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"
    max_tool_result_length: 8192

TOOLS:
  local_functions:
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "natural_language_to_sql"
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "report_generator"

WORKSPACE:
  path: "/data/agent_workspace"
```

**使用示例**：

```python
from dataagent.interface.sdk.agent import DataAgent

# 从配置创建 Agent
agent = DataAgent.from_config("ecommerce_agent.yaml")

# 单轮对话
response = await agent.chat("上个月销售额最高的产品是什么？")
if "messages" in response:
    last_msg = response["messages"][-1]
    print(last_msg.content)

# 流式对话
async for mode, data in agent.astream(
    input={"messages": [("human", "分析客户留存率趋势")]},
    stream_mode="values"
):
    if mode == "values":
        print(data.get("messages", [])[-1] if data.get("messages") else "")
```
