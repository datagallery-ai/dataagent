## ADDED Requirements

### Requirement: 四字段内部错误模型

系统 MUST 用唯一内部类型 `DataAgentError` 表达失败，公开字段固定为 `source`、`component`、`fact`、`trace_id`。`source` MUST 为 `config`、`llm`、`tool`、`internal`、`constraint` 之一。公开载荷 MUST NOT 包含 `locator`、`detail`、`retryable`、`http_status` 或 traceback。

#### Scenario: 构造四字段错误

- **WHEN** 调用方以 `source`、`component`、`fact`、`trace_id` 构造 `DataAgentError`
- **THEN** 对象持有这四个字段
- **AND** `str(error)` 等于 `fact`

#### Scenario: 非法 source 被拒绝

- **WHEN** 构造时传入不在允许集合内的 `source`
- **THEN** 系统拒绝该构造

### Requirement: 只接受 fact 主文案

`DataAgentError` 构造器 MUST 只接受 `fact=` 作为主文案入口，MUST NOT 提供 `message=` 别名。未传 `fact` 时 MUST 使用按 `source` 的短兜底文案。

#### Scenario: 未传 fact 使用兜底

- **WHEN** 以 `source=config` 构造且未传 `fact`
- **THEN** `fact` 为该 source 的短兜底（例如「配置无效」）

### Requirement: 只通过 to_dict 与 from_dict 序列化

系统 MUST 只提供 `to_dict` 与 `from_dict` 作为四字段序列化入口。`to_dict` MUST 只返回四字段。`from_dict` MUST 还原这四字段，并 MUST 忽略多余键。

#### Scenario: 往返保持四字段

- **WHEN** 将 `DataAgentError` 执行 `to_dict` 后再 `from_dict`
- **THEN** 还原后的 `source`、`component`、`fact`、`trace_id` 与原对象一致

#### Scenario: from_dict 忽略多余键

- **WHEN** 载荷在四字段之外还包含 `locator`、`detail`、`retryable` 或 `http_status`
- **THEN** `from_dict` 仍还原四字段
- **AND** 再次 `to_dict` 不含这些多余键

### Requirement: Actor 面脱敏

`fact`、`to_dict` 与 Actor 文案 MUST 脱敏 token、api_key 值及其它 secret 赋值，MUST NOT 把响应全文或凭据原样暴露给 Actor。

#### Scenario: fact 中的密钥被替换

- **WHEN** 构造 `fact` 含 `api_key=...` 或 Bearer token
- **THEN** Actor 可见文案与 `to_dict()["fact"]` 不含原始密钥

### Requirement: from_exception 最小归一

`DataAgentError.from_exception` MUST 按以下规则归一：已是 `DataAgentError` 则原样返回（若指定新 `trace_id` 则只替换 `trace_id`）；`TimeoutError` MUST 映射为 `source=constraint`；其它异常 MUST 映射为 `source=internal`，`fact` 为脱敏后的 `TypeName: 短消息`。

#### Scenario: 未知异常归一为 internal

- **WHEN** 将普通 `RuntimeError` 交给 `from_exception`
- **THEN** 得到 `source=internal` 的 `DataAgentError`
- **AND** `fact` 含异常类型名且已经脱敏

#### Scenario: TimeoutError 映射为 constraint

- **WHEN** 将 `TimeoutError` 交给 `from_exception`
- **THEN** 得到 `source=constraint` 的 `DataAgentError`

#### Scenario: 已是 DataAgentError 只换 trace_id

- **WHEN** 对已有 `DataAgentError` 调用 `from_exception` 并传入新 `trace_id`
- **THEN** `source`、`component`、`fact` 保持不变
- **AND** `trace_id` 为新值

### Requirement: 政策不挂在错误对象上

`DataAgentError` MUST NOT 把 `retryable` 或 `http_status` 作为一等字段。重试次数与 HTTP 状态 MUST 由边界政策决定，而不是从错误对象读取。

#### Scenario: 公开字典不含政策字段

- **WHEN** 序列化任意 `DataAgentError`
- **THEN** `to_dict()` 不含 `retryable` 与 `http_status`
