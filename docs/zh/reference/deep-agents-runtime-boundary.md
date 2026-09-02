# Deep Agents Runtime v1 能力边界

这份文档给 **runtime 实现方** 确认边界：什么必须具备、现在已经具备、哪些是遗留、哪些明确不做。协议字段与事件形状以 [接入契约](deep-agents-runtime.md) 为准。本文是 2026-09-01 的能力快照，不替代契约。

读者只需要回答三件事：

1. 边界内的「必须具备」是否认账。
2. 「遗留项」里哪些由 runtime 收口，哪些由控制面收口。
3. 「明确不做」是否同意，避免下一期能力被提前塞进 v1。

## 分工

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| 控制面 (`apps/api`) | 会话、鉴权、整理 `messages` / `systemPrompt`、事件落库与投影、HITL 传输、取消编排、前端 / TUI | 不执行 LangGraph，不持有 runtime checkpoint 内部结构 |
| Runtime (`services/deepagents-runtime`) | 跑 Deep Agents / LangGraph、发 AG-UI SSE、工具执行、中断与恢复、取消、不透明 `checkpointRef` | 不感知 DataFoundry 元数据、数据网关、SQL 审计、artifact、知识库、Skill |
| 客户端 (Web / TUI) | 渲染 AG-UI 事件与会话回放 | 不直连 runtime HTTP |

未配置 `RUNTIME_SERVICE_URL` 时，控制面回退到进程内 TypeScript 桩。`npm run dev` 会拉起 Python runtime（默认 `:8790`）并注入该 URL。

## v1 必须具备

这些是 runtime 对控制面的承诺。缺一项，前端或控制面就不能按当前协议工作。

### 传输

| 能力 | 约定 |
| --- | --- |
| 存活探测 | `GET /health`，声明 `provider`、`version`、`capabilities` |
| 流式 run | `POST /runs/stream`，SSE，每条 `data:` 是一个 AG-UI `BaseEvent` |
| 取消 | `POST /runs/:runId/cancel`，尽快停图；随后发带 `status: "cancelled"` 的 `RUN_FINISHED` |
| 鉴权 | 可校验 `Authorization: Bearer <RUNTIME_SERVICE_TOKEN>` |
| 密钥隔离 | 请求里没有模型 Key、数据库密码、MCP Token |

### 事件与身份

AG-UI `toolCallId` **等于模型 `tool_calls[].id`**（例如 `call_xxx`），不等于 LangGraph / LangChain 执行层 `run_id`（UUID）。

| 规则 | 原因 |
| --- | --- |
| `on_chat_model_stream` / `on_chat_model_end` 用模型 id 发 `TOOL_CALL_START` / `ARGS` | 前端与控制面只认这一套 id |
| `on_tool_start` 只能绑定已有模型 id，不得再 `START` | 否则同一次调用会出现两条「运行中」，`RUN_FINISHED` 会被 AG-UI 拒掉 |
| 同名并行按 FIFO 对齐 | 两个 `write_todos` 必须分别对应 `call_a`、`call_b` |
| 仅当没有未绑定的模型 id 时，才允许用执行层 id 开新调用 | 兼容「模型没带 id」的退化路径 |
| `on_tool_start` 不得重放模型已经发过的 ARGS | 重复 delta 会拼坏 JSON，CopilotKit 关不掉工具卡 |
| `TOOL_CALL_RESULT` 必须带 `messageId` 和 `role: "tool"` | CopilotKit 靠这条生成 tool 消息；缺了前端会一直「运行中」 |
| 图跑完仍有未结束的 `toolCallId` 时发 `RUN_ERROR`（`UNFINISHED_TOOL_CALLS:…`），禁止补 `TOOL_CALL_END` | 补 END 会掩盖真实中断或丢结果 |
| 工具前后的文本用独立 `messageId`，不要对已 `TEXT_MESSAGE_END` 的 id 再 `START` | 避免校验失败、回复被丢掉 |

### 对话与工具

| 能力 | v1 范围 |
| --- | --- |
| 流式文本 | `TEXT_MESSAGE_START` / `CONTENT` / `END` |
| 同 `threadId` 多轮 | 控制面带上历史 `messages`；runtime 用自己的 checkpointer |
| `write_todos` | Deep Agents Todo 中间件，已作为第一支工具验收 |
| `ask_user` | 唯一正式接入的 HITL 工具，`interrupt_on.ask_user` |
| SDK 自带 filesystem（如 `glob`） | **不是** DataFoundry 数据工具。真实模型会调用。控制面不治理路径与权限，只当普通 AG-UI 工具展示 |

### HITL

中断时发 CUSTOM `on_interrupt`，`value.type = "agent_interrupt"`。`toolName` 当前实现只保证 `ask_user`。

恢复时控制面再次 `POST /runs/stream`，**复用原 `runId`**，带 `resume.interrupt` 与 `resume.response`。`response === false` 表示用户取消该中断。

旧 `mastra_suspend` 只用于历史回放，runtime 必须拒绝续跑。

### 持久化与回放

- Runtime 只维护自己的 checkpoint，经 `runtime.bound` 回传不透明 `checkpointRef`。
- 对话历史、工具结果、checkpoint 状态由**控制面**落库。
- 刷新页面后的回放走控制面会话 API，不要求 runtime 重放 SSE。

## 已接入并验收

2026-09-01 用真实模型 + Web（`http://127.0.0.1:3000`）和控制面（`:8787`）跑过。Runtime 单测：`services/deepagents-runtime` 下 25 passed。

| 场景 | 结果 |
| --- | --- |
| 纯对话 | 流式文本，`RUN_FINISHED` |
| 单次 `write_todos` | 一条 `call_*`，RESULT 到达前端，工具卡结束 |
| 同名三次 `glob` | 三个不同模型 id，全部 `completed`，成功率 100% |
| `ask_user` 中断后点「继续」 | 弹出协作卡，恢复后有收尾文本，checkpoint `completed` |
| 同会话追问 | HITL 之后纯对话不再挂起 |
| 流式中点停止 | `POST .../cancel` 200；控制面 checkpoint `canceled` |
| 刷新回放已完成的工具会话 | 一条 `write_todos` + 原文回复，不再「运行中」 |
| Schema / SQL 类提示 | 不崩。没有 `inspect_schema` / 真实查库；模型可能改调 filesystem |

假模型路径（`DEEPAGENTS_RUNTIME_MODEL=fake`）覆盖对话、`write_todos`、`ask_user` 中断与恢复，见 `npm run smoke:deepagents-sdk`。

## 遗留项

边界内已承诺、但尚未收口。请 runtime 标出责任方。

| 编号 | 现象 | 建议责任 | 说明 |
| --- | --- | --- | --- |
| L1 | HITL 恢复后，会话 DTO 里 `pendingInteractions` 与 `ask_user` 仍可能是 `pending` | 控制面为主 | 页面已走完且 checkpoint 为 `completed`。恢复路径要能把工具标成完成；runtime 恢复后应再发该 `toolCallId` 的 `TOOL_CALL_RESULT` |
| L2 | 契约写了 `submit_plan`，runtime 未把它放进 `interrupt_on` | Runtime | `write_todos` 中断映射到 `submit_plan` 只存在于事件翻译，真实图不会因 todo 挂起 |
| L3 | HITL 点「停止」/ `response === false` 未做前端验收 | 双方 | 代码路径在，缺真实点击证据 |
| L4 | 用户取消后，控制面 `terminalEvent` 可能记成 `RUN_ERROR`，status 为 `canceled` | 控制面记录，runtime 对齐 | Runtime 应发 `RUN_FINISHED` + `status: "cancelled"`，不要只断流 |
| L5 | 工具前若已结束一段文本，再用同一个 `msg_{runId}` 开下一段 | Runtime | 当前验收的工具回合没有这段前导文本，风险仍在 |
| L6 | `./deploy.sh` 尚未自动拉起 Python runtime | 控制面 / 部署 | 本地 `npm run dev` 已接入；部署形态未对齐 |
| L7 | SDK filesystem 工具对控制面不可治理 | 需 runtime 确认 | 真实模型会 `glob`。v1 允许当普通工具展示，还是应在 runtime 关掉，需要书面确认 |

不要用这些方式「修」L 系列：按工具名去重、在 `RUN_FINISHED` 前补 `END`、关掉 AG-UI 校验、前端合并同名 running 卡片。那些会掩盖身份错误。

## 明确不做（不是缺陷）

v1 **不**把下列能力算进 runtime 边界。前端相应面板显示「本版本未启用」或「后端暂不支持」。

- DataFoundry 数据工具：查 Schema、只读 SQL、数据源探测
- SQL 审计、artifact、workspace / sandbox 元数据
- 知识库检索、Skill、MCP、目标 / 记忆 / protocol 门控
- 控制面不得把数据源凭据或业务策略下沉到 runtime
- 旧 Mastra 中断不能在新 runtime 上 resume

用户问「展示数据源中的表」时，runtime 可以拒绝、用 filesystem 空转，或用文本说明未接入。控制面不期望出现 `inspect_schema` / `run_sql` 的真实结果。

## Runtime 应具备的能力（确认清单）

请按「必须 / 应当 / 禁止」签字或回注。

### 必须

- [ ] 实现契约三个 HTTP 端点，SSE 为 AG-UI `BaseEvent`
- [ ] `toolCallId` 使用模型 `tool_calls[].id`；执行层 id 只做绑定
- [ ] `TOOL_CALL_RESULT` 含 `toolCallId`、`toolCallName`、`content`、`messageId`、`role: "tool"`
- [ ] 支持流式文本、同线程多轮
- [ ] 支持 `ask_user` 中断与按原 `runId` 恢复
- [ ] 支持 `POST /runs/:runId/cancel`，并以 `RUN_FINISHED`（`cancelled`）收尾
- [ ] `runtime.bound` 带回不透明 `checkpointRef`
- [ ] 拒绝 `mastra_suspend` 续跑
- [ ] 不接收、不索要数据面凭据

### 应当

- [ ] 同名并行 FIFO
- [ ] 未结束工具在无 interrupt 时 `RUN_ERROR`，不补 `END`
- [ ] 无 Key 时可用假模型走同一条 LangGraph 路径
- [ ] 遵守控制面下发的 `systemPrompt` 与 `limits.maxSteps`
- [ ] 书面确认是否保留 SDK filesystem 工具（见 L7）

### 禁止

- [ ] 用 LangGraph `run_id` 当作新的 AG-UI `toolCallId`
- [ ] 为通过校验而补发 `TOOL_CALL_END`
- [ ] 覆盖或改写控制面 `systemPrompt` 里的业务策略
- [ ] 把 `artifact` / `sql_audit` / `skill.selection` 当成 v1 必发事件
- [ ] 在 runtime 内直连生产库或 DataFoundry 元数据库

## 下一期（不在本次边界）

下列能力要单独立项，不要 quietly 扩进 v1 契约：

1. 经控制面工具网关接入数据工具（权限、审计、`ToolObservation`）
2. SQL 审计、artifact、workspace 信号
3. 正式 `submit_plan` HITL
4. 部署脚本拉起 Python runtime
5. 清理 HITL 恢复后的 pending 投影（L1）

## 相关文档

- [Deep Agents Runtime 接入契约](deep-agents-runtime.md) — 字段与事件协议
- [Agent Runtime 与 AG-UI 参考](agent-runtime.md) — 客户端如何调控制面
- 实现：`services/deepagents-runtime/`
- 控制面客户端：`apps/api/src/runtime/`
