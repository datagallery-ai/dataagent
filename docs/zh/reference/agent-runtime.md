# Agent Runtime 与 AG-UI 参考

这篇文档面向 Web、TUI 和其他客户端开发者。读完后，你可以构造 Agent run 请求，理解 `run_config`，消费 AG-UI 事件，并处理取消、错误和恢复。

## 运行入口

```text
POST /api/copilotkit
```

这个接口启动一次 Agent run，并返回 AG-UI 事件流。资源管理、文件上传、artifact 下载等动作走 `/api/v1/*` REST API。

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
| 用户取消 | 调用 `POST /api/v1/runs/:runId/cancel`，停止按钮进入取消中状态。 |
| run 失败 | 展示后端错误消息，保留已收到的事件和产出。 |
| 网络中断 | 用 `threadId` 读取会话历史，再恢复 UI 状态。 |
| 刷新页面 | 调用会话和 artifact 接口重建对话、追溯和产出。 |

后端会把 run 事件持久化，客户端不需要把完整历史塞回下一次请求。

## 安全边界

- 不把数据库密码、模型 API Key、MCP Token 放进 `messages`、`context` 或 `forwardedProps`。
- 数据源访问经过 Data Gateway。
- 文件、知识库、Skill 和 MCP 工具由后端策略筛选。
- 事件流可用于展示和回放，不携带敏感明文。

## 延伸阅读

- 配置资源：[配置 API 参考](configuration-api.md)
- HTTP 端点：[REST API 参考](rest-api.md)
- 系统结构：[架构概览](../architecture/overview.md)
