# Deep Agents Runtime 接入契约

这篇文档是 DataFoundry 控制面与独立 Agent Runtime 服务之间的唯一界面。前端、TUI 和 REST 不感知 runtime 实现；runtime 不感知 DataFoundry 内部服务。

第一版只打通对话、流式、取消、HITL 与历史持久化。数据工具、SQL 审计、artifact 产出、语义治理与 Skill 均不接入。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | Runtime 存活与能力声明 |
| POST | `/runs/stream` | 启动或恢复一次 run，返回 AG-UI SSE |
| POST | `/runs/:runId/cancel` | 取消正在执行的 run |

默认本地服务：`http://127.0.0.1:8790`，由 `services/deepagents-runtime` 用 Deep Agents SDK（`create_deep_agent`）实现。`npm run dev` 会拉起该进程并写入 `RUNTIME_SERVICE_URL`。未配置 URL 时，API 回退到进程内 TypeScript 桩。独立启动：`npm run runtime:deepagents`。旧桩仍可通过 `npm run runtime:stub` 使用。

内部调用可带 `Authorization: Bearer <RUNTIME_SERVICE_TOKEN>`。不要把数据库凭据或模型密钥放进 run 请求。

## `GET /health`

```json
{
  "status": "ok",
  "provider": "deepagents",
  "version": "v1",
  "capabilities": {
    "streaming": true,
    "tools": true,
    "interrupt": true,
    "cancel": true
  }
}
```

`status` 为 `ok` 或 `degraded`。控制面 `/ready` 会把该结果放进 `runtime` 字段；runtime 不可用时 REST 与历史回放仍可服务。

## `POST /runs/stream`

请求体为 `RuntimeRunRequest`：

```json
{
  "threadId": "session-001",
  "runId": "run-001",
  "messages": [
    { "id": "m1", "role": "user", "content": "你好" }
  ],
  "systemPrompt": "You are DataFoundry's assistant. Data tools are not connected in this version.",
  "model": {
    "profileId": "server-default",
    "name": "qwen-plus",
    "provider": "openai-compatible"
  },
  "limits": {
    "maxSteps": 80
  },
  "resume": {
    "interrupt": {
      "type": "agent_interrupt",
      "toolCallId": "call_ask_1",
      "toolName": "ask_user",
      "runId": "run-001"
    },
    "response": { "answer": "继续" }
  },
  "checkpointRef": "opaque-runtime-checkpoint",
  "trace": {
    "userId": "user-1",
    "workspaceId": "ws-1"
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `threadId` | 是 | 会话 ID，对应控制面 session |
| `runId` | 是 | 单次 run ID；resume 必须复用挂起时的 runId |
| `messages` | 是 | 服务端整理后的对话历史与本轮输入 |
| `systemPrompt` | 是 | 由控制面构建，runtime 不得自行拼接业务指令 |
| `model` | 否 | 模型选择元数据，不含 API Key |
| `limits` | 否 | 步数等软限制 |
| `resume` | 否 | HITL 恢复；`response === false` 表示取消该中断 |
| `checkpointRef` | 否 | runtime 私有 checkpoint 的不透明引用 |
| `trace` | 否 | 仅用于日志，不含凭据 |

响应为 `text/event-stream`，每条 `data:` 行是一个 AG-UI `BaseEvent` JSON。

## 事件

Runtime 必须发出标准生命周期、文本、reasoning 与 tool call 事件：

- `RUN_STARTED` / `RUN_FINISHED` / `RUN_ERROR`
- `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`
- `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` / `TOOL_CALL_RESULT`
- 可选 reasoning / activity 事件

控制面是唯一事件序列器：它会落库、投影并转发给前端。Runtime 只发自己产生的事件。

建议额外发一条 CUSTOM 事件，便于控制面识别来源：

```json
{
  "type": "CUSTOM",
  "name": "runtime.bound",
  "value": {
    "provider": "deepagents",
    "version": "v1",
    "checkpointRef": "opaque-runtime-checkpoint"
  }
}
```

第一版不要求 `artifact`、`sql_audit`、`workspace.metadata`、`sandbox.output`、`skill.selection`、`goal.updated`、`context.compiled`。前端会把这些面板显示为「本版本未启用」。

## HITL

中断时发 CUSTOM `on_interrupt`，value 使用中性结构：

```json
{
  "type": "agent_interrupt",
  "toolCallId": "call_ask_1",
  "toolName": "ask_user",
  "runId": "run-001",
  "args": { "question": "需要我继续吗？", "options": ["继续", "停止"] },
  "suspendPayload": { "question": "需要我继续吗？", "options": ["继续", "停止"] },
  "resumeSchema": { "type": "object" }
}
```

`toolName` 目前支持 `ask_user` 与 `submit_plan`。控制面会补齐 `interaction.requested`、必要的 `TOOL_CALL_START/END`，并向客户端发一条仅用于传输的 `RUN_FINISHED`。

恢复时控制面再次调用 `/runs/stream`，带上原 `runId` 与 `resume`。旧 Mastra 会话的 `mastra_suspend` 仅用于只读回放，不能在新 runtime 上续跑。

## 取消

`POST /runs/:runId/cancel` 请求体：

```json
{ "reason": "RUN_CANCELLED" }
```

Runtime 应尽快停止执行。控制面随后把 run 标为 canceled。

## 第一版验证

Python runtime 直接调用 `create_deep_agent`。未配置 `LLM_API_KEY` 时默认用脚本化模型走通同一条 LangGraph 路径（对话、`write_todos`、`ask_user` HITL）。配置了 Key 后走 `LLM_BASE_URL` / `LLM_MODEL` 的 OpenAI 兼容接口。验证命令：

```bash
cd services/deepagents-runtime && uv sync
npm run smoke:deepagents-sdk
```

## 桩服务场景

独立桩 `npm run runtime:stub` 根据用户文本或 `forwardedProps.runtimeStubScenario` 选择：

| 场景 | 触发 | 行为 |
| --- | --- | --- |
| `text` | 默认 | 流式文本回复后结束 |
| `tool` | 文本含 `tool` / `plan` | 发出一次 `write_todos` 工具调用 |
| `interrupt` | 文本含 `interrupt` / `ask` | 发出 `ask_user` 中断 |

恢复中断后，桩会发出 `TOOL_CALL_RESULT` 与收尾文本。

## 安全

- 不传数据库密码、模型 API Key、MCP Token。
- Runtime 实验期只用 demo 或本地数据，不连生产库。
- 数据工具接入留到下一期再议。

## 延伸阅读

- [v1 能力边界](deep-agents-runtime-boundary.md) — 已接入、遗留项、明确不做，以及 runtime 确认清单
- [Agent Runtime 与 AG-UI 参考](agent-runtime.md) — 客户端如何调用控制面
