# Deep Agents Runtime 能力边界

这份文档说明当前 Python 控制面已经接通什么，以及第一阶段明确关闭什么。协议入口以 [Deep Agents Runtime](deep-agents-runtime.md) 为准。

## 当前分工

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| Python FastAPI (`apps/api`) | 认证、CSRF、最小启动 REST、官方 AG-UI endpoint、`create_deep_agent()`、SQLite checkpointer | 数据网关、知识库、MCP、Skill、文件、Artifact、会话事件回放 |
| 客户端 (Web / TUI) | 发送标准 `RunAgentInput`，渲染文本流和运行状态 | 不再发送 CopilotKit `method/params/body` envelope，也不直连旧 sidecar |

不再存在独立的 `RUNTIME_SERVICE_URL` / `:8790` sidecar。`npm run dev` 只启动 Python API 与 Web。

## 已接入

- 注册、登录、登出、邮箱校验（`AUTH_EMAIL_DELIVERY=test` 时返回 `verificationToken`）
- Cookie 名 `df_session` / `df_csrf`，不安全方法校验 `X-CSRF-Token`
- `GET /api/v1/capabilities` 合法最小响应，`activeLlmProfileId` 固定为 `server-default`
- 标准 `RunAgentInput` SSE：`RUN_STARTED`、`TEXT_MESSAGE_*`、`RUN_FINISHED`，错误为 `RUN_ERROR`
- 进程重启后，同一 `threadId` 的 LangGraph checkpoint 可恢复

## 已关闭

这些能力会在 `GET /api/v1/capabilities` 中返回 `false`，REST 也只给空列表或空配置：

- conversation memory / 会话标题
- data tools / 数据源 / SQL 审计
- knowledge / MCP / skills
- files / artifacts / 导出
- trace DAG / 自定义事件
- HITL resume / 分支恢复

Web 和 TUI 的现有面板可以继续显示，但不能把它们当成当前后端已实现。
