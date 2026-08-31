## ADDED Requirements

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

## MODIFIED Requirements

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
