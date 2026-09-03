# Python SDK 与 YAML 配置参考

DataAgent 保留现有 YAML 与 `chat`/`astream` 入口，主 Agent 改为 Deep Agents 0.7.5 原生编译的 LangGraph。SDK 不会把 LangGraph 状态或流事件转换成私有协议。

## SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("config.yaml")
```

### `await agent.chat(...)`

```python
result = await agent.chat(
    "分析最新销售数据",
    session_id="session-001",
    initial_state={"user_id": "alice"},
    checkpoint_id="optional-checkpoint",
    config={"tags": ["production"], "recursion_limit": 100},
)
```

返回值是包含 `messages` 的 LangGraph 原生最终状态。`session_id` 会与 `user_id` 一起映射到 checkpointer 的 `thread_id`。未传 session ID 时，每次调用都会生成新会话；多轮对话应复用同一个 ID。

### `agent.astream(...)`

```python
async for event in agent.astream(
    {"messages": [("user", "生成报告")]},
    session_id="session-001",
    stream_mode="values",
    config={"tags": ["production"]},
):
    print(event)
```

`astream()` 返回异步迭代器，直接透传 LangGraph 原生 `stream_mode` 和事件载荷。为兼容旧调用，也接受 `initial_state={"user_query": "..."}`。调用方传入的 `RunnableConfig` 字段会被保留；DataAgent 负责 `configurable.thread_id`，显式 `checkpoint_id` 负责 `configurable.checkpoint_id`。

### 其他方法

- `await agent.build_agent_graph()` 返回 `CompiledStateGraph`。
- `await agent.select_engine(...)` 把有效配置编译成原生图。
- `agent.get_agent_info()` 返回配置中的名称、版本、描述和后端。
- `agent.update_config(mapping)` 清除按会话缓存的图。

## 最小 YAML

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
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
      api_key: "$env{DEEPSEEK_API_KEY}"
      base_url: "$env{DEEPSEEK_BASE_URL}"
```

`backend: langgraph` 和 `type: react` 继续作为兼容值。过去用于构造主循环的自定义节点拓扑字段已经退出，主循环现在由 Deep Agents 创建。

## 模型

`MODEL` 下每个非 embedding 槽位都会编译为 LangChain `BaseChatModel`，`provider` 忽略大小写。

```yaml
AGENT_CONFIG:
  primary_model: chat_model

MODEL:
  chat_model:
    provider: DeepSeek
    model_type: chat
    params:
      model: deepseek-chat
  backup_model:
    provider: openai
    model_type: chat
    params:
      model: gpt-4.1-mini
```

主模型选择顺序为 `AGENT_CONFIG.primary_model`、`chat_model` 槽位、第一个聊天模型槽位。未知供应商和明确声明的 OpenAI 兼容供应商使用 `ChatOpenAI`，LangChain 原生供应商使用 `init_chat_model`。`params` 未提供凭据时，compiler 会读取 `<PROVIDER>_API_KEY` 和 `<PROVIDER>_BASE_URL`。

其他聊天模型按配置顺序成为 fallback 模型。Embedding 槽位仍可供保留的 Semantic/NL2SQL 系统使用，但不会成为主 Agent 模型候选。

## 工作区

```yaml
USER_ID: alice
WORKSPACE:
  backend: filesystem
  path: /srv/dataagent/workspaces/alice
  allow_path:
    - /srv/reference-data
```

- 默认后端是 Deep Agents `FilesystemBackend`。
- 默认路径按工作区策略落在 `.dataagent/<user_id>/<session_id>`。
- `allow_path` 目录以只读方式挂入原生文件工具。
- `backend: state` 选择 `StateBackend`；此时不能配置 `path`，Shell 工具也会被禁用。

Deep Agents 提供原生文件工具。对于文件系统工作区，DataAgent 还会安装 LangChain Shell middleware，单条命令默认超时为 600 秒。

可选 Shell 白名单：

```yaml
SHELL_TOOL_WHITELIST:
  - ls
  - cat
  - python
```

管道或命令链中的每条命令都必须在白名单中，并拒绝命令替换与进程替换。

## 本地工具

现有 `TOOLS.local_functions` 会编译成 LangChain 原生工具：

```yaml
TOOLS:
  local_functions:
    - module: my_package.tools
      function: lookup_metric
      name: lookup_metric
      description: 查询受治理的业务指标。
      config:
        catalog: finance
      hooks:
        pre:
          - my_package.hooks.validate_metric_request
        post:
          - my_package.hooks.audit_metric_result
```

支持同步函数、异步函数和 `BaseTool` 对象。函数可声明 `_tool_context`，以获取兼容配置和原生 tool runtime；该参数不会暴露给模型。

Tool pre-hook 接收 `ToolCallRequest`，返回 `ToolCallRequest | None`。Post-hook 接收 `(ToolCallRequest, ToolMessage | Command)`，返回相同结果类型或 `None`。MCP hook 按 server、A2A hook 按远端 Agent 生效，配置形状同样是 `hooks.pre/post`。

## MCP

MCP 使用官方 `langchain-mcp-adapters` 客户端，在图编译阶段异步发现工具。

```yaml
TOOLS:
  mcp_servers:
    - server_id: local-files
      transport_type: stdio
      config:
        command: uvx
        args: [mcp-server-filesystem, /srv/data]
    - server_id: metrics
      transport_type: streamable_http
      config:
        url: https://metrics.example.com/mcp
        headers:
          Authorization: "Bearer $env{MCP_TOKEN}"
```

支持 `stdio`、`sse`、`streamable_http` 和 `websocket`。

## A2A 工具

系统会发现每个远端 AgentCard，并把其发布的每个 skill 转成异步 LangChain 工具：

```yaml
TOOLS:
  A2A:
    - agent_id: risk-agent
      base_url: https://risk.example.com
      auth_token: "$env{RISK_AGENT_TOKEN}"
      timeout: 30
```

旧的嵌套形式也兼容，例如 `- risk-agent: {base_url: ...}`。

## Skills

Skills 统一通过 Deep Agents 原生机制暴露：

```yaml
TOOLS:
  skills:
    builtin: [sql-analysis]
    custom_dirs:
      - /srv/dataagent-skills
    user: [my-personal-skill]
```

每个 Skill 是一个包含 `SKILL.md` 的目录。`builtin` 和 `user` 是按名称筛选的白名单，`custom_dirs` 暴露目录中的有效 Skill 子目录；Skill 源对 Agent 只读。

## Subagents 与 NL2SQL

通用子 Agent 指向完整 YAML：

```yaml
SUBAGENTS:
  - path: /srv/agents/researcher.yaml
```

路径必须是绝对路径或以 `~/` 开头。子 YAML 可以定义原生 Deep Agent，也可以声明 `AGENT_CONFIG.type: nl2sql`。已废弃的 `SUBAGENT_CONFIGS` 写法会归一化为 `SUBAGENTS`，新配置应只使用 `SUBAGENTS`。

重要的 NL2SQL 子 Agent 可以直接内联配置，不必重复元数据：

```yaml
DATABASE:
  type: sqlite
  config:
    path: /srv/data/sales.sqlite

NL2SQL:
  SEMANTIC_LAYER:
    enabled: true
```

只允许一个内联 `NL2SQL` Agent。默认 id、名称、描述和类型会自动补齐，也可以用内层 `AGENT_CONFIG` 覆盖兼容元数据。父配置的 `DATABASE` 和 `SEMANTIC_LAYER` 优先，以便统一管理连接策略。

## Prompt 与人工反馈

```yaml
AGENT_CONFIG:
  enable_human_feedback: true

SCENARIO:
  chat:
    instructions: 你是受治理的数据助手。
    prompt_appends:
      system:
        - 不得编造指标口径。
    human_feedback_conditions:
      - 一个指标存在多个受治理定义
```

System prompt 会组合 DataAgent 通用规划指引、工作区规则、prompt append、HITL 条件和场景指令。开启人工反馈后会注册 `request_human_feedback`，并在提示词中明确标注配置项是 HITL 条件。

## Agent 与模型 Hook

```yaml
HOOKS:
  agent:
    pre: [my_package.hooks.before_agent]
    post: [my_package.hooks.after_agent]
  model:
    pre:
      - name: my_package.hooks.before_model
        model: backup_model
        option: value
    post: [my_package.hooks.after_model]
```

Model hook 只包围主 Agent loop 发起的模型调用。工具内部自行调用模型属于另一个 runnable，不会触发这套 hook。`HOOKS.nodes.planner.pre/post` 继续作为 model hook 的兼容别名，但推荐使用 `HOOKS.model.pre/post`。Hook callable 接收 `state`、可选的 LangGraph 原生 `runtime`，以及可选的 keyword-only 配置参数。

## Context 与 Middleware

```yaml
CONTEXT:
  compress_token_limit: 64000
  compress_message_cnt: 100
```

这些既有字段用于配置 Deep Agents 原生 summarization。DataAgent 还会启用 summarization tool、Todo 跟踪、工具异常转换、模型重试、多模型 fallback，以及文件系统工作区中的 Shell middleware。`AGENT_CONFIG.max_iter` 会安装原生模型调用次数限制；设为 `null` 时使用原生运行行为和 LangGraph 安全步数限制。

## Suites

`SUITE.include` 仍由 `ConfigManager` 处理。Suite 中的模型、工具、hook、prompt、skills、governance 配置和 subagent YAML 会先展开成普通 YAML 层，再进入 Deep Agent compiler。运行时 resources/jobs 与保留的 `data_analysis` Suite 仍属于待迁移能力，不进入原生主循环。
