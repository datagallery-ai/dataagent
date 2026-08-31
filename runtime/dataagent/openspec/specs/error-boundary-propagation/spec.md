# 错误边界传播规格

## Purpose

定义四字段错误接到 REST、A2A、SDK、MCP、Job、Flex Executor、NL2SQL 工具路径与子 Agent 后的可观察行为：业务失败与协议失败的区分、禁止假成功、以及重试分类。

## Requirements
### Requirement: REST 业务失败保持 HTTP 200

REST 查询业务失败 MUST 返回 HTTP 200，并把错误放在 `result` 中。所有 Agent 的流式业务失败连接 MUST 仍返回 HTTP 200，MUST 使用 `event: result`，MUST NOT 改用其它终端事件名。协议层失败 MUST 使用 413（请求体过大）、429（限流）、503（服务未就绪、排队满或排队等待超时）、504（请求处理超时）。协议层与业务失败响应 MUST NOT 写入旧 `code`、`nl2sql_compat` 或 `NL2SQL-*` / `WORKFLOW-AGENT-001` 一类旧业务码。

#### Scenario: 查询业务失败

- **WHEN** `/api/agent/query` 非流式路径抛出 `DataAgentError`
- **THEN** HTTP 状态为 200
- **AND** body 的 `result` 含 `source`、`component`、`fact`、`trace_id`

#### Scenario: 流式业务失败使用 result 事件

- **WHEN** REST 流式路径遇到 `DataAgentError`
- **THEN** HTTP 状态为 200
- **AND** 终端事件名为 `result`
- **AND** 事件数据携带四字段

#### Scenario: NL2SQL 业务失败仅四字段

- **WHEN** NL2SQL REST 非流式或流式查询抛出 `DataAgentError`
- **THEN** HTTP 状态为 200
- **AND** `result` 仅含 `source`、`component`、`fact`、`trace_id`
- **AND** `result` 不含旧 `code`、`success`、`message`、`http_status`、`nl2sql_compat`

#### Scenario: 协议层使用专用状态码

- **WHEN** 请求体过大
- **THEN** 返回 413
- **AND** 响应不含旧 `code` 或 `nl2sql_compat`

- **WHEN** 触发限流
- **THEN** 返回 429

- **WHEN** 服务未就绪、排队满或排队等待超时
- **THEN** 返回 503
- **AND** MUST NOT 把排队等待超时写成 504

- **WHEN** 已取得并发名额后请求处理超时
- **THEN** 返回 504

### Requirement: Depends 取不到服务时返回 503

当 REST Depends 取不到 DataAgent 服务（`_data_agent_service is None`）时，`/api/agent/query` 的非流式与流式路径 MUST 返回 HTTP 503，body MUST 为短 `detail`，MUST NOT 返回 500，MUST NOT 返回 HTTP 200 四字段。`/health` 未就绪 MUST 仍返回 503。服务已初始化后的业务失败 MUST 仍为 HTTP 200 + 四字段。

#### Scenario: Depends 缺失返回 503

- **WHEN** `_data_agent_service is None` 且客户端调用 `/api/agent/query`（非流式或流式）
- **THEN** HTTP 状态为 503
- **AND** body 含短 `detail`
- **AND** 不含 200 四字段，也不含旧业务码

#### Scenario: health 未就绪仍 503

- **WHEN** `/health` 判定服务未就绪
- **THEN** HTTP 状态为 503

#### Scenario: 已初始化后业务失败仍 200

- **WHEN** 服务已初始化且查询抛出 `DataAgentError`
- **THEN** HTTP 状态为 200
- **AND** body 的 `result` 含四字段

### Requirement: 性能采集按类型与 HTTP 状态填充分类

工具性能事件 MUST 用 `classify_exception` 的类型与 HTTP 状态（及对应重试政策）填写采集字段。MUST NOT 从执行对象读取已删除的 `error_type` / `retry_info`。MUST NOT 扫描 fact。MUST NOT 用 `__cause__`。MUST NOT 把这些旧字段加回 `NormalizedToolExecution`。

#### Scenario: 采集字段来自分类结果

- **WHEN** 工具执行失败且执行对象带 Exception 类型的 `error`
- **THEN** 性能事件的分类字段来自 `classify_exception`
- **AND** 不依赖执行对象上的 `error_type` / `retry_info`

#### Scenario: 采集不扫 fact

- **WHEN** 用于采集的异常是普通 `Exception`，消息为 `timeout 429`
- **THEN** 采集分类为 UNKNOWN

### Requirement: REST 非结构化 state.error 保留原文

REST 将字符串或非结构化 `state.error` 收成 `DataAgentError` 时，`fact` MUST 保留可提取的原文。MUST NOT 把有原文的错误改写成 `RuntimeError: Agent failed`。仅当无法提取任何文案时，才允许使用兜底 `Agent failed`。

#### Scenario: 字符串 error 保留原文

- **WHEN** REST 格式化结果时 `state.error` 为字符串（例如 `no such table: t`）
- **THEN** 抛出的 `DataAgentError.fact` 含该原文
- **AND** `fact` 不等于 `RuntimeError: Agent failed`

#### Scenario: 非结构化字典取原文

- **WHEN** `state.error` 为无 `source` 的字典，且含非空 `fact`、`message` 或 `error` 字符串
- **THEN** `fact` 取该字符串原文
- **AND** 不得改写成 `RuntimeError: Agent failed`

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

Flex Executor MUST 用 `classify_exception` / `ErrorType` 表决定重试。分类 MUST 只依据异常类型与 HTTP 状态。MUST NOT 从 `DataAgentError` 读取重试次数。MUST NOT 扫描 fact 或异常消息中的中英文关键词。MUST NOT 用 `source=internal` 短路。MUST NOT 用 `__cause__` 作为重试通道。超时 MUST 重试 1 次。当异常带 HTTP 状态 401 时 MUST 归为 AUTHENTICATION，重试 0 次。

#### Scenario: 超时重试一次

- **WHEN** 工具调用抛出 `TimeoutError`
- **THEN** 分类为 TIMEOUT
- **AND** Executor 最多再试 1 次

#### Scenario: 401 不重试

- **WHEN** 异常带 HTTP 状态 401
- **THEN** 分类为 AUTHENTICATION
- **AND** 重试次数为 0

### Requirement: 重试只认类型与 HTTP 状态

系统 MUST 先具备类型表，再谈中文回归：`TimeoutError`、`ConnectionError` / `httpx.RequestError`、以及带 HTTP 状态的异常（如 `httpx.HTTPStatusError.status_code`）。分类 MUST 使用异常类型与 HTTP 状态，MUST NOT 扫描 fact 或异常消息中的中英文关键词，MUST NOT 用类型名是否包含 `network` 一类子串代替 `isinstance`。`__cause__` MUST 只用于栈与日志，MUST NOT 作为重试通道。

#### Scenario: 网络异常按类型识别

- **WHEN** 工具调用抛出 `ConnectionError` 或 `httpx.RequestError`
- **THEN** 分类为 NETWORK
- **AND** 不依赖类型名或消息是否包含 `network`

#### Scenario: 未知异常不扫 timeout 或 429

- **WHEN** 抛出普通 `Exception`，消息为 `timeout 429`
- **THEN** 分类为 UNKNOWN
- **AND** 不因消息含 timeout 或 429 而改分类

### Requirement: Wrapper 保留原异常

本地与 MCP 工具路径 MUST 原样 raise 原异常，或让 `ToolResult` 携带原异常，由 manager raise **原异常**。这些路径 MUST NOT 调用 `from_exception`，MUST NOT 用 `tool_failure(fact=str(e))` 丢掉异常类型。四字段只作为对外展示入口。

#### Scenario: 本地工具超时保持 TimeoutError

- **WHEN** 本地工具函数抛出 `TimeoutError`
- **THEN** manager 抛出的仍是该 `TimeoutError`
- **AND** 分类为 TIMEOUT

### Requirement: 配置校验不进重试

`source=config` 的配置校验失败 MUST 在调用点直接失败并离开重试路径，MUST NOT 靠读取 `DataAgentError.source` 做短路。

#### Scenario: 语义层未配置不进重试

- **WHEN** `SEMANTIC_LAYER.base_url` 未配置并在调用点校验失败
- **THEN** 该失败不进入工具重试循环

### Requirement: 跨进程不合成重试 cause

系统 MUST NOT 为跨进程四字段载荷合成 `__cause__` 以便重试。IPC 还原 MUST 只走四字段入口。父进程自身捕获的 `TimeoutError` 仍按类型分类。

#### Scenario: 仅有四字段的超时载荷不伪装 TIMEOUT

- **WHEN** 从跨进程载荷按四字段还原错误，且当前异常不是 `TimeoutError`
- **THEN** 分类不得因为载荷曾表示超时而变成 TIMEOUT
- **AND** 不得为此合成 `TimeoutError` 作为 `__cause__`

### Requirement: ToolMessage 不含 trace_id

失败 ToolMessage 的 content MUST 为 `[source/component] fact`，MUST NOT 包含 `trace_id`。

#### Scenario: 工具失败写入 Actor

- **WHEN** Executor 将 `DataAgentError` 写成 ToolMessage
- **THEN** content 为 `actor_text()`
- **AND** content 不含 `trace_id`

### Requirement: 合法零行保持成功

候选 `error is None` 且行数为空时，系统 MUST 视为合法零行成功，MUST NOT 将其当作执行失败。

#### Scenario: 零行无 error

- **WHEN** SQL 执行返回 0 行且候选 `error` 为空
- **THEN** NL2SQL 将该候选视为成功
- **AND** 整体任务可以成功结束

### Requirement: 工具路径禁止静默空成功

`nl2sql_sub_agent_tool` 在子 Agent 返回的 `state.error` 有值时 MUST raise `DataAgentError`，MUST NOT 写空 CSV 冒充成功。需求 4 的验收点是该工具路径，不是直连 `chat` 或 REST `success: true`。

#### Scenario: 选中 SQL 执行失败

- **WHEN** 子 Agent state 含执行错误与 sql
- **THEN** `nl2sql_sub_agent_tool` 抛出 `source=tool`、`component=nl2sql` 的 `DataAgentError`
- **AND** `fact` 含库原文与 sql
- **AND** 不写空 CSV

### Requirement: 不修改选优算法

本变更 MUST NOT 改变 Selector 选优、投票或反思算法。Selector 接受候选时可以继续把该候选的 `error` 写入 `state["error"]`。

#### Scenario: Selector 仍按原规则接受

- **WHEN** 多个候选进入 Selector
- **THEN** 选优规则与本变更前一致
- **AND** 接受时可将选中候选的 `error` 写入 `state["error"]`

### Requirement: 生成全失败不得空成功

当全部 SQL 生成策略失败时，NL2SQL MUST 抛出 `DataAgentError`，MUST NOT 返回空的成功 SQL 结果。

#### Scenario: 所有生成策略失败

- **WHEN** 全部生成策略失败
- **THEN** NL2SQL 抛出 `DataAgentError`
- **AND** 不返回成功的空 SQL

### Requirement: 子进程只通过 worker_result.error 报错

失败的子 Agent 进程 MUST 只通过 `worker_result.error` 上报四字段错误。stdout 信封 MUST 包含 `worker_result`，MUST NOT 使用顶层 `error` 作为第二条失败通道。

#### Scenario: 子 Agent 结构化失败

- **WHEN** 子 Agent 执行失败并抛出 `DataAgentError`
- **THEN** stdout 含 `worker_result.status` 为失败态
- **AND** `worker_result.error` 含四字段
- **AND** 没有顶层 `error` 字段

### Requirement: 拒绝非法协议与假成功

父进程 MUST 只接受含合法 `worker_result` 的 JSON。非 JSON、缺少 `worker_result`、非法 status、旧 `original_msg` 格式以及为残缺载荷合成的成功 MUST 被拒绝，不得装成成功。

#### Scenario: 缺少 worker_result

- **WHEN** 子进程退出且 JSON 不含合法 `worker_result`
- **THEN** 父进程视为失败
- **AND** 不合成成功结果

#### Scenario: 旧顶层 error 被拒绝

- **WHEN** 子进程返回顶层 `error` 且没有合法 `worker_result`
- **THEN** 父进程拒绝该载荷

### Requirement: 主 Agent 必须看见子 Agent 失败

失败的子 Agent 结果 MUST 成为主 Agent 的 error ToolMessage。ToolMessage MUST 使用 `[source/component] fact`，MUST NOT 被标为成功工具结果。需求 5 的验收点是这条可见性，不是终止 ReAct。

#### Scenario: 子 Agent 失败传到主 Agent

- **WHEN** 子 Agent 以结构化 `DataAgentError` 失败
- **THEN** 主 Agent 收到 `status=error` 的 ToolMessage
- **AND** content 含 source、component 与 fact
- **AND** 该工具结果不被视为成功

### Requirement: 父子共享 trace_id 仅用于对日志

子 Agent 失败日志与 `worker_result.error` MUST 使用同一 `trace_id`。该字段 MUST 留在四字段 wire 与日志中，MUST NOT 写入 ToolMessage content。

#### Scenario: 用 trace_id 对子进程日志

- **WHEN** 父进程从 `worker_result.error` 还原 `DataAgentError`
- **THEN** `trace_id` 与子进程失败日志一致
- **AND** 发给主 Agent 的 ToolMessage content 不含 `trace_id`
