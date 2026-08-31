## ADDED Requirements

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
