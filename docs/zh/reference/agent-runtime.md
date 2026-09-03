# Agent Runtime 与 AG-UI 参考

这篇文档面向 Web、TUI 和其他客户端开发者。读完后，你可以构造 Agent run 请求，理解 `run_config`，消费 AG-UI 事件，并处理取消、错误和恢复。

## 运行入口

```text
POST /api/copilotkit
```

这个接口启动一次 Agent run，并返回 AG-UI 事件流。请求体必须是标准 AG-UI `RunAgentInput`，不要再包一层 CopilotKit `method/params/body` envelope。

当前控制面和 Deep Agents Runtime 运行在同一个 Python FastAPI 进程中，由官方 `ag-ui-langgraph` 直接挂载 `create_deep_agent()` 图。第一阶段只保证认证、启动配置、SQLite checkpoint 和文本流。

`GET /api/v1/capabilities` 会关闭 conversation memory、data tools、knowledge、MCP、skills、files、artifacts、trace 和 HITL resume。客户端仍可以渲染这些面板，但不能把它们当成已实现能力。

资源管理、文件上传、artifact 下载等动作走 `/api/v1/*` REST API。其中未实现的能力会通过 `GET /api/v1/capabilities` 明确关闭。

## 请求上下文

常用字段：

| 字段 | 说明 |
| --- | --- |
| `threadId` | 会话 ID。后端用它关联历史、恢复会话和归档产出。 |
| `runId` | 单次运行 ID。客户端用它取消、追踪和回放。 |
| `messages` | 本轮用户输入。不要放凭据。 |
| `forwardedProps.run_config` | 本次运行资源选择，优先级高于 state。 |
| `state.run_config` | 客户端状态中的运行配置。 |

示例：

```json
{
  "threadId": "session-001",
  "runId": "run-001",
  "messages": [
    {
      "role": "user",
      "content": "统计 orders 表各渠道 GMV。"
    }
  ],
  "forwardedProps": {
    "run_config": {
      "activeDatasourceId": "dtc-growth-demo",
      "enabledDatasourceIds": ["dtc-growth-demo"],
      "activeLlmProfileId": "server-default"
    }
  }
}
```

## `run_config` 字段

| 字段 | 用途 |
| --- | --- |
| `enabledDatasourceIds` | 本次 run 可用的数据源集合。 |
| `activeDatasourceId` | 默认使用的数据源。 |
| `enabledKnowledgeIds` | 本次 run 可用的知识库集合。 |
| `enabledMcpServerIds` | 本次 run 可用的 MCP Server 集合。 |
| `enabledSkillIds` | 本次 run 可用的 Skill 集合。 |
| `activeSkillId` | 用户明确指定的 Skill。 |
| `activeLlmProfileId` | 本次 run 使用的模型配置。 |
| `skill_mode` | Skill 选择模式，例如 `auto`。 |
| `fileIds` | 工作区文件 ID。 |
| `pinnedPaths` | 本会话内文件或产出路径。 |
| `mentioned` | 用户通过 `@` 提及的资源。 |

客户端只传资源 ID、选择和引用。后端负责校验权限、状态和能力开关。

## 配置合并

```text
workspace defaults
  + per-run overrides
  + server policy
  = effective run config
```

- `workspace defaults` 来自工作区配置。
- `per-run overrides` 来自输入框选择、会话资源开关和 `@` 提及。
- `server policy` 由后端执行，客户端不能绕过。

## 事件消费

客户端按 AG-UI 事件语义渲染，不需要自定义另一套 SSE/chat 协议。

| 类别 | 用途 |
| --- | --- |
| run 状态 | 表示运行开始、完成、取消或失败。 |
| 文本消息 | 展示 Agent 回复。 |
| reasoning / thought | 展示可公开的推理摘要或步骤说明。 |
| tool call | 展示 schema 检查、SQL 查询、文件读取等工具调用。 |
| custom event | 承载 artifact、SQL audit、token usage、workspace metadata 等结构化信息。 |

客户端应保存 `runId`、`threadId`、tool call id 和 artifact id，用于详情展示、取消和恢复。

## 取消、错误和恢复

| 场景 | 客户端动作 |
| --- | --- |
| 用户取消 | 第一阶段没有独立 cancel REST。Web / TUI 停止按钮应中断当前 SSE。 |
| run 失败 | 展示标准 `RUN_ERROR` 或 HTTP 错误，保留已收到的文本。 |
| 网络中断 | 可以继续用同一个 `threadId` 再发一条消息；不要依赖会话 REST 回放。 |
| 刷新页面 | 第一阶段没有会话事件持久化或 artifact 重建。 |

后端用 SQLite checkpointer 按 `threadId` 保存 LangGraph 状态。这不是完整的会话事件回放。

## 安全边界

- 不把数据库密码、模型 API Key、MCP Token 放进 `messages`、`context` 或 `forwardedProps`。
- 数据源访问经过 Data Gateway。
- 文件、知识库、Skill 和 MCP 工具由后端策略筛选。
- 事件流可用于展示和回放，不携带敏感明文。

客户端仍走 `POST /api/copilotkit`，请求体必须是标准 `RunAgentInput`。当前实现说明见 [Deep Agents Runtime](deep-agents-runtime.md) 与 [能力边界](deep-agents-runtime-boundary.md)。

## 延伸阅读

- 配置资源：[配置 API 参考](configuration-api.md)
- HTTP 端点：[REST API 参考](rest-api.md)
- 系统结构：[架构概览](../architecture/overview.md)
