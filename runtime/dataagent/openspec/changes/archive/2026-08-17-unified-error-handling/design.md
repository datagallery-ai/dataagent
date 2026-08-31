## Context

阶段一（`unified-error-handling-core` / MR 316）已落地 `DataAgentError` 四字段。本阶段把该模型接到各边界，并落实「失败必须可见、成功必须是真成功」。

入口与锚点：

- 模型：`dataagent/core/errors.py`
- REST：`dataagent/interface/rest_api/app.py`、`service.py`、`middleware.py`
- A2A：`dataagent/a2a_server/agent_executor.py`
- SDK：`dataagent/interface/sdk/agent.py`
- NL2SQL 工具：`dataagent/actions/tools/local_tool/tools.py`（`nl2sql_sub_agent_tool`）
- 子 Agent：`dataagent/core/swarm/worker_result.py`、`sub_agent_entry.py`、`subagent_subprocess_runner.py`
- Job：`dataagent/actions/tools/local_tool/job_tools.py`、`dataagent/core/flex/nodes/executor.py`
- MCP：`dataagent/actions/tools/mcp.py`
- 重试：`dataagent/core/managers/action_manager/base.py` `classify_exception`
- 测试：`tests/ut/nl2sql/test_error_propagation.py`、`tests/ut/interface/test_rest_api_errors.py`、`tests/ut/tools/test_sub_agent_tool.py`、`tests/ut/core/test_errors.py`

约束：沿用现有模块边界；不恢复 ErrorCode；不为假想调用方做双轨兼容。

## Goals / Non-Goals

**Goals:**

- 子 Agent / Job 终态 / MCP / NL2SQL 工具失败对主 Agent 或调用方可见。
- 合法零行、`queued` / `running` 不得标成失败。
- REST 业务失败 HTTP 200 + `event: result`；协议 413 / 429 / 503 / 504。
- 直连 chat 不因 `state.error` 抛；REST NL2SQL 可 `success: true`；A2A 不扫 state。
- ToolMessage 无 `trace_id`；超时重试 1 次；401 = AUTHENTICATION、重试 0 次。

**Non-Goals:**

- 改 NL2SQL 选优、投票、反思算法。
- 改 REST SSE 帧名或 A2A 正常事件顺序。
- 把重试政策写回 `DataAgentError`。
- 兼容旧错误字典或旧子进程顶层 `error`。

## Decisions

### D1：边界各序列化一次，ToolMessage 只用 actor_text

REST / A2A / `worker_result.error` 传四字段。ToolMessage 只写 `[source/component] fact`，不带 `trace_id`。主 Agent 靠这行定位；对栈用日志里的 `trace_id`。

备选：ToolMessage 塞完整 JSON。否决原因：污染 Planner 上下文，且 `trace_id` 对 Actor 无行动价值。

### D2：REST 业务失败保持传输成功

业务失败：**HTTP 200** + `event: result` / `{"result": 四字段}`。协议层才用 413（体过大）、429（限流）、503（未就绪 / 排队满）、504（排队超时）。不要把 `http_status` 存进错误对象。

备选：按 source 回 400 / 422 / 500。否决原因：网关会重试/告警，客户端 `ok` 分支翻转，属于不必要的对外行为变化。

### D3：直连 chat 与工具路径分开

- `NL2SQLAgent.chat` / SDK `chat`：不因 `state.error` 抛；工作流正常返回 state。
- REST NL2SQL 成功格式化可带 `success: true`。
- `nl2sql_sub_agent_tool`：见 `state.error` 则 raise，不写空 CSV。需求 4 验收走工具路径。

备选：直连 chat 也因 `state.error` 抛。否决原因：用户明确取舍，避免把选中执行失败升级成工作流异常。

### D4：A2A 不扫 state

A2A 只在捕获异常时发 FAILED。`state.error` 不单独触发 FAILED。正常结束走 COMPLETED。

备选：扫描最终 state 的 error 字段。否决原因：会把「直连不抛」的 NL2SQL 执行失败误判成协议失败。

### D5：Job 生命周期与工具级 ERROR 分开

- `queued` / `running`：工具成功，快照必须交回 Planner。
- 终态 `failed` / `timed_out` / `cancelled` / `unknown` / `not_found`：工具失败 → error ToolMessage，不终止 ReAct。
- 协调器返回的工具级 `status=ERROR`：在 poll / cancel / submit / collect 边界 raise。

备选：把进行中状态也标失败。否决原因：submit / poll 无法工作，ReAct 断掉。

### D6：Executor 复用 main 分类表

重试不挂在错误对象上，不靠 fact 中文关键词。`classify_exception`：

| 分类 | 次数 |
|---|---|
| 校验 | 0 |
| AUTHENTICATION（含 401） | 0 |
| 超时 | 1 |
| 网络 / 429 | 3 |
| 普通 / UNKNOWN | 1 |

不要恢复 ErrorCode。

### D7：禁止假成功

stdout 缺 `worker_result`、旧顶层 `error`、synthetic success、MCP `isError` 当成功、Job 终态当成功——全部拒绝。需求 5：子 Agent 失败必须变成主 Agent 的 error ToolMessage。

## Risks / Trade-offs

- [REST 200 被当成「没失败」] → 文档写明业务失败看 body 四字段；协议失败才看 HTTP。
- [直连 chat 不抛被当成需求 4 不满足] → 需求 4 的验收点是 `nl2sql_sub_agent_tool`，不是 REST / 直连 chat。
- [Job 进行中被标失败] → Executor 只把终态集合当失败；`queued` / `running` 保持成功。
- [401 被当 UNKNOWN 重试] → 分类表显式匹配 `http 401` / unauthorized → AUTHENTICATION、0 次。

## Migration Plan

随 `feat/unified-error-handling` 整分支合入。旧子进程载荷直接失败，不做半兼容。回滚：整变更回退。

## Open Questions

无。拍板结论已全部落入本设计。
