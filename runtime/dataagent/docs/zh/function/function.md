---
hide:
  - navigation
---

# 功能特性

## 原生 Agent 运行时

DataAgent 主 Agent 是运行在 LangGraph 上的 Deep Agents 0.7.5 `CompiledStateGraph`。推理/工具循环、文件工具、状态结构、checkpointer 协议、store 协议、subagent middleware、skills 和 summarization 均由 Deep Agents 管理。DataAgent 只在现有 YAML 边界外增加兼容 compiler，不再维护第二套 Agent 运行时。

公共 SDK 直接返回 LangGraph 原生状态和流事件。Tags、metadata、recursion limit、checkpoint 等 LangGraph runtime 配置可以经 SDK 透传。

## YAML 兼容 compiler

当前映射范围如下：

| 配置区域 | 原生目标 |
| --- | --- |
| `MODEL` | LangChain `BaseChatModel`，包含主模型选择与 fallback |
| `TOOLS.local_functions` | LangChain 原生 structured tools |
| `TOOLS.mcp_servers` | 官方 `langchain-mcp-adapters` 工具 |
| `TOOLS.A2A` | 从远端 AgentCard 异步发现的工具 |
| `TOOLS.skills` | Deep Agents 原生 Skills 与只读 Skill 挂载 |
| `WORKSPACE` | 文件系统、组合或 state backend 以及权限 |
| `SUBAGENTS` | 从 YAML 加载的 Deep Agents compiled subagents |
| `NL2SQL` | 一个专用内联原生 NL2SQL 子图 |
| `HOOKS` | LangChain agent/model/tool middleware hooks |
| `CONTEXT` | Deep Agents summarization 阈值 |
| `SCENARIO.chat` | System prompt 指令、追加内容和 HITL 条件 |
| `SUITE` | ConfigManager 在编译前展开成普通 YAML |

旧的自定义工作流节点拓扑不再进入原生运行时。`backend: langgraph` 和 `type: react` 继续作为兼容值，用户 YAML 无需进行没有意义的改名。

## 模型与 Middleware

`provider` 忽略大小写。LangChain 原生供应商通过 `init_chat_model` 创建，OpenAI 兼容供应商使用 `ChatOpenAI`。默认主模型槽位是 `chat_model`，也可通过 `AGENT_CONFIG.primary_model` 指定其他槽位。

默认 middleware 栈包括：

- 将工具异常转换成模型可读的 tool error；
- 使用 LangChain 原生策略重试模型调用；
- 配置多个聊天模型时启用 model fallback；
- 文件系统工作区中的 Shell，单条命令超时 600 秒；
- 可选 Shell 命令白名单；
- Todo 跟踪；
- 自动 summarization 与显式 summarization tool；
- 通过 `AGENT_CONFIG.max_iter` 可选限制模型调用次数；
- 配置的 agent、model 与 tool hooks。

## 工具

所有活跃工具源会在调用 `create_deep_agent` 前转成 LangChain `BaseTool`。本地工具支持同步与异步函数，也可以声明兼容 `_tool_context`。MCP 使用官方 adapter 发现工具；A2A AgentCard skill 会转成直接使用 A2A client 的异步工具。

文件能力直接使用 Deep Agents 原生工具，不再重复包装。Shell 只在 backend 拥有真实宿主机目录时可用，因此 `WORKSPACE.backend: state` 会拒绝显式 Shell 配置。

## 工作区与 Skills

默认可写工作区位于 `.dataagent/<user_id>/<session_id>`。显式绝对路径 `WORKSPACE.path` 可以覆盖默认位置；额外的 `WORKSPACE.allow_path` 目录通过 composite backend 只读挂载。

内置、自定义目录和用户级 Skills 都会归一化为 Deep Agents Skill source。Agent 可读取 Skill 文件，但不能改写挂载的 Skill 库。

## Subagents 与 NL2SQL

`SUBAGENTS` 接收完整的子 Agent YAML，并把每个配置编译成 runnable 子图。模型可继承父 Agent，也可由子 Agent 自行声明。递归 YAML 引用和重复 identifier 会被拒绝。

`NL2SQL` 是一个专用内联配置，只允许一个默认 NL2SQL 子 Agent。现有 NL2SQL nodes 会被编译为原生 LangGraph，然后以 runnable 形式直接交给 Deep Agents；数据库和语义层连接设置继续由父配置集中管理。

## 人工反馈与 Hook

开启后，`request_human_feedback` 通过 LangGraph 原生 `interrupt()` 中断图。配置的 `human_feedback_conditions` 会作为明确的 HITL 条件追加进 system prompt。

`HOOKS.agent.pre/post` 包围一次 Agent 调用；`HOOKS.model.pre/post` 包围主 Agent loop 中的每次模型调用。工具内部自行调用模型不在该 loop 内，因此不会触发 model hook。Tool hook 只包围它所配置的 local、MCP 或 A2A 工具源。

## 有意保留的待迁移系统

Context、jobs、resource runtime/resources、governance、dataops、semantic recall、document recall、NL2SQL 和 `data_analysis` Suite 按决策继续保留。它们的接入边界仍在迁移中，并不代表已删除的主 Agent 运行时仍然生效。

配置示例和精确接口见 [Python SDK 与 YAML 配置参考](../api_doc/PythonSDK.md)。
