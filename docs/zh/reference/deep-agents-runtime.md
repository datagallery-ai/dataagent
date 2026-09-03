# Deep Agents Runtime

控制面和 Deep Agents 运行在同一个 Python FastAPI 进程里。客户端只对接 `POST /api/copilotkit`。基于 Deep Agents 的二次开发放在 `runtime/deepagents`，由 `create_runtime_agent()` 合并进官方 `create_deep_agent()`，不是独立服务。

## 当前入口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 进程存活 |
| GET | `/ready` | 控制面与进程内 Deep Agents 就绪 |
| POST | `/api/copilotkit` | 标准 AG-UI `RunAgentInput`，返回 SSE |

请求体只接受标准字段：`threadId`、`runId`、`messages`、`state`、`tools`、`context`、`forwardedProps`、`resume`。带 `method: "agent/run"` 的 CopilotKit envelope 会返回 `400 BAD_REQUEST`。

未配置 `LLM_API_KEY`，或设置 `DEEPAGENTS_RUNTIME_MODEL=fake` 时，使用脚本化模型，仍走真实 `create_deep_agent()` 与 LangGraph。生产使用持久化 SQLite checkpointer，不使用 `MemorySaver`。

## 第一阶段能力

已接入：

- Cookie 会话与 CSRF
- 最小启动 REST：`/api/v1/auth/*`、`/me`、`/capabilities`、`/workspace-config`、`/run-defaults`
- 官方 `ag-ui-langgraph` 文本流：至少包含 `RUN_STARTED`、文本消息事件、`RUN_FINISHED`
- 按 `threadId` 恢复 LangGraph checkpoint

未接入：数据源、Knowledge、MCP、Skill、文件、Artifact、Trace、会话事件持久化、分支恢复、自定义事件和正式 HITL。

详见 [能力边界](deep-agents-runtime-boundary.md) 与 [Agent Runtime 与 AG-UI](agent-runtime.md)。
