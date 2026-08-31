## ADDED Requirements

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
