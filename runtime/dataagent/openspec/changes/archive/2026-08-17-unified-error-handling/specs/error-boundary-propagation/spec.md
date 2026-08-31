## ADDED Requirements

### Requirement: REST 业务失败保持 HTTP 200

REST 查询与流式的业务失败 MUST 返回 HTTP 200，并把四字段放在 `result` 中。流式业务失败 MUST 使用 `event: result`，MUST NOT 改用其它终端事件名。协议层失败 MUST 使用 413（请求体过大）、429（限流）、503（服务未就绪或排队满）、504（排队超时）。

#### Scenario: 查询业务失败

- **WHEN** `/api/agent/query` 非流式路径抛出 `DataAgentError`
- **THEN** HTTP 状态为 200
- **AND** body 为 `{"result": {source, component, fact, trace_id}}`

#### Scenario: 流式业务失败使用 result 事件

- **WHEN** REST 流式路径遇到 `DataAgentError`
- **THEN** 终端事件名为 `result`
- **AND** 事件数据携带四字段

#### Scenario: 协议层使用专用状态码

- **WHEN** 请求体过大、触发限流、服务未就绪或排队超时
- **THEN** 分别返回 413、429、503 或 504

### Requirement: REST NL2SQL 允许 success true

当 NL2SQL 直连路径正常返回且未被工具边界 raise 时，REST MUST 允许把结果格式化为带 `success: true` 的结构化载荷。该行为 MUST NOT 被解释为「静默空成功」。

#### Scenario: NL2SQL 成功格式化

- **WHEN** REST 收到 NL2SQL 最终 state 且不因 `state.error` 走工具失败路径
- **THEN** 响应可以包含 `success: true` 与 candidates / sql 等字段

### Requirement: 直连 chat 不因 state.error 抛出

`NL2SQLAgent.chat` 与 SDK `chat` MUST 在工作流正常结束时返回 state，MUST NOT 仅仅因为 `state.error` 有值而抛出 `DataAgentError`。

#### Scenario: 选中候选带执行错误

- **WHEN** Selector 把候选 `error` 写入 `state["error"]` 且调用方走直连 `chat`
- **THEN** `chat` 返回该 state
- **AND** 不因该字段抛异常

### Requirement: A2A 不扫描 state.error

A2A 执行器 MUST 只在捕获异常时发送 FAILED。最终 state 中的 `error` 字段 MUST NOT 单独触发 FAILED。

#### Scenario: 工作流返回带 error 的 state

- **WHEN** A2A 流式或非流式调用正常返回最终 state，且 state 含 `error`
- **THEN** 执行器不因此发送 FAILED
- **AND** 按正常完成路径发送 COMPLETED

#### Scenario: 调用抛出 DataAgentError

- **WHEN** A2A 调用路径抛出 `DataAgentError`
- **THEN** 执行器发送 FAILED
- **AND** 文本含 `[source/component] fact` 与 `trace_id`

### Requirement: Job 进行中为工具成功

当 Job 快照状态为 `queued` 或 `running` 时，Executor MUST 将其视为工具成功，MUST 把该快照交回 Planner。终态 `failed`、`timed_out`、`cancelled`、`unknown`、`not_found` MUST 视为工具失败并生成 error ToolMessage，MUST NOT 终止 ReAct 循环。工具级 `status=ERROR` MUST 在 Job 工具边界 raise。

#### Scenario: poll 返回 running

- **WHEN** Job 工具返回带 `job_id` 且 `status=running` 的快照
- **THEN** Executor 生成成功 ToolMessage
- **AND** Planner 能继续看到该快照

#### Scenario: collect 返回 failed

- **WHEN** Job 快照 `status=failed`
- **THEN** Executor 生成 `status=error` 的 ToolMessage
- **AND** ReAct 循环继续

### Requirement: MCP 失败必须可见

MCP 工具在 `CallToolResult.isError` 或传输失败时 MUST raise `DataAgentError`，MUST NOT 把失败内容当作成功 data 返回。

#### Scenario: MCP isError

- **WHEN** MCP 调用返回 `isError=true`
- **THEN** 工具路径抛出 `DataAgentError`
- **AND** 主 Agent 收到 error ToolMessage

### Requirement: Executor 重试按分类表

Flex Executor MUST 用 `classify_exception` / `ErrorType` 表决定重试，MUST NOT 从 `DataAgentError` 读取重试次数，MUST NOT 用 fact 中文关键词分类。超时 MUST 重试 1 次。401 / unauthorized MUST 归为 AUTHENTICATION，重试 0 次。

#### Scenario: 超时重试一次

- **WHEN** 工具调用抛出 TimeoutError 或分类为 TIMEOUT
- **THEN** Executor 最多再试 1 次

#### Scenario: 401 不重试

- **WHEN** 异常信息匹配 HTTP 401 或 unauthorized
- **THEN** 分类为 AUTHENTICATION
- **AND** 重试次数为 0

### Requirement: ToolMessage 不含 trace_id

失败 ToolMessage 的 content MUST 为 `[source/component] fact`，MUST NOT 包含 `trace_id`。

#### Scenario: 工具失败写入 Actor

- **WHEN** Executor 将 `DataAgentError` 写成 ToolMessage
- **THEN** content 为 `actor_text()`
- **AND** content 不含 `trace_id`
