## Context

阶段二已把四字段接到各边界。重试仍走 `classify_exception` / `ErrorType`，但实现扫 fact 中英文关键词，Executor 还用 `source=internal` 短路和 `__cause__` 当重试通道。本地 / MCP wrapper 用 `tool_failure(fact=str(e))` 丢掉类型后，中文超时 / 429 无法按类型重试，英文 fact 却能「碰巧」命中。

入口与锚点：

- 分类：`dataagent/core/managers/action_manager/base.py` `classify_exception`
- 重试：`dataagent/core/flex/nodes/executor.py` `_retry_policy_for` / `_retry_tool_execution`
- Wrapper：`dataagent/actions/tools/local.py`、`mcp.py`；manager `acall` raise `result.error`
- 展示模型：`dataagent/core/errors.py`（四字段、`from_exception`、`from_timeout_dict`）
- 语义客户端：`dataagent/actions/tools/semantic_tool/semantic_client.py`（当前把 httpx 包成 DAE 并挂 `__cause__`）
- 测试：`tests/ut/core/test_errors.py`、`tests/ut/test_error_classification.py`、`tests/ut/executor/`、`tests/ut/tools/`、`tests/ut/interface/test_rest_api_errors.py`、`tests/ut/test_performance_integration.py`

约束：REST / SSE / SDK chat 形状不变；不为重试补 cause；不扫 fact。

## Goals / Non-Goals

**Goals:**

- 重试只看原始异常类型 + HTTP 状态。先补类型表，否则中文回归落空。
- 展示只走四字段入口。REST 业务失败仍 HTTP 200。
- Wrapper / manager 传递原异常；本地 / MCP 不走 `from_exception`。
- `__cause__` 只用于栈 / 日志。评审「补 cause」在此模型无对象。
- 配置校验在调用点不进重试，不靠 `DAE.source` 短路。
- 删除仅服务重试的 `from_timeout_dict`；跨进程不合成 cause。
- Depends 取不到服务时 query / stream 用协议 503，与 `/health` 未就绪一致走协议层，不走 200 四字段。

**Non-Goals:**

- 改 REST / SSE / SDK chat 对外契约或帧名。
- 把重试政策写回 `DataAgentError`。
- 恢复 fact 关键词表或 ErrorCode。
- 为跨进程超时再发明第二条重试通道。
- query 前加 `is_ready()` guard、改 lazy-init、或改已初始化后的 200 四字段。
- 把 `error_type` / `retry_info` 加回 `NormalizedToolExecution`，或加回 ToolError / 补 `__cause__`。

## Decisions

### D1：重试与展示拆开

重试：`classify_exception(原始异常)`。展示：`DataAgentError` 四字段。Executor 先按捕获到的原异常分类，重试用尽后再 `_normalize_error` 做展示归一。

备选：继续扫 fact / 读 `__cause__`。否决原因：中文 fact 漏分、英文 fact 误分，且把展示字段当成重试协议。

### D2：类型表先于关键词

`classify_exception` MUST 用 `isinstance` / `status_code`，不得用类型名是否含 `network` 之类子串，也不得扫消息。

| 输入 | 分类 | 次数 |
|---|---|---|
| `TimeoutError`、`httpx.TimeoutException` | TIMEOUT | 1 |
| `ConnectionError`、`httpx.RequestError`（超时已先匹配） | NETWORK | 3 |
| HTTP 状态 401 | AUTHENTICATION | 0 |
| HTTP 状态 429 | RATE_LIMIT | 3 |
| `ParamsValueError` | VALIDATION | 0 |
| 其它（含 `DataAgentError`、`Exception("timeout 429")`） | UNKNOWN | 1 |

`httpx.TimeoutException` 是 `RequestError` 子类，必须先于网络类匹配。HTTP 状态只认异常自带的 `status_code`（如 `httpx.HTTPStatusError`），不从 fact 解析「HTTP 401」。

备选：保留英文关键词作兜底。否决原因：与「不扫 fact」冲突，且让无类型的碰巧重试继续存在。

### D3：Wrapper 保类型，manager raise 原异常

本地 / MCP / A2A 捕获非 `DataAgentError` 时：原样 raise，或 `ToolResult` 带原异常。禁止 `tool_failure(fact=str(e))`。`ToolManager.acall` raise **原异常**。本地 / MCP 不调用 `from_exception`。

语义客户端对 `httpx.TimeoutException` / `RequestError` / `HTTPStatusError` 同样原样 raise，否则类型表到不了 Executor，中文回归落空。JSON / 配置类失败仍可在调用点做成 `DataAgentError`（展示），因为它们本就不是重试类型。

备选：`ToolResult` 并行挂 DAE + 原异常。否决原因：多一条通道；manager raise 原异常已够。

### D4：不用 source 短路，不用 cause 重试

删除 Executor 的 `source=config` / `source=internal` 短路和 `prefer __cause__`。`__cause__` 只留给栈 / 日志。配置校验（`SemanticServiceClient.from_config`、Executor 入参 `ParamsValueError`、wrapper `validate_input`）在调用点失败并返回 / 抛出，不进入 `_retry_tool_execution`。

备选：给 DAE 加 `retryable`。否决原因：政策不挂错误对象（既有 error-model）。

### D5：删除 `from_timeout_dict`

它只为跨进程超时合成 `TimeoutError` cause 以便重试。按 D1/D4 无对象。IPC 还原只用 `from_dict`。父进程自己等到的 `TimeoutError` 仍按类型重试；只有四字段、没有类型的跨进程超时接受 UNKNOWN（1 次）。

### D6：对外形状不变

REST 业务失败 HTTP 200 + `result` 四字段；流式仍 `event: result`。SDK chat / SSE 帧形状不改。`from_exception` 仍只在系统边界做展示归一。

### D7：Depends 缺失是协议 503

`get_data_agent_service()` 在 `_data_agent_service is None` 时 MUST 抛 `HTTPException(503)`，`detail` 为短文案 `DataAgent service unavailable`。MUST NOT 再抛 `RuntimeError` 变成 500。MUST NOT 在 query 前加 `is_ready()` guard，MUST NOT 改 lazy-init。`/health` 未就绪仍 503。已初始化后的业务失败仍 200 + 四字段。

备选：query 里先 `is_ready()` 再 503。否决原因：未挂服务与未就绪是两件事；本条只修 Depends 取不到服务。

### D8：性能采集用同一次分类结果

`measure_tool` MUST 用 `classify_exception` 填事件的类型与 HTTP 状态（及对应 `ErrorPolicy`），与 Executor `_retry_policy_for` 同一张表。输入是执行对象上的 `error`（Exception）。MUST NOT 读已删除的 `error_type` / `retry_info`，MUST NOT 扫 fact，MUST NOT 走 `__cause__`，MUST NOT 把旧字段加回 `NormalizedToolExecution`。集成测试 MUST 用真实 `NormalizedToolExecution`，不得用带旧字段的 fake 掩盖。

备选：把 `error_type` 加回执行对象供采集读取。否决原因：重试模型已按原始异常分类，采集应跟分类函数，不应恢复旧字段。

## Risks / Trade-offs

- [只有英文 fact、没有类型的旧「碰巧 3 次」变成 1 次] → 接受；测试改为断言 UNKNOWN，不再扫 timeout / 429。
- [语义 / 子 Agent 在重试前包成 DAE，中文超时不再重试] → 这些路径必须 raise 原异常；类型表是回归前提。
- [配置错误若误进 Executor except] → 调用点不进重试；分类侧不读 `source`。
- [跨进程超时不再 TIMEOUT] → 不合成 cause；父进程仍持有 `TimeoutError` 的路径不受影响。

## Migration Plan

随本变更合入 `feat/retry-classify-raw-exception`。无对外契约迁移。回滚：整变更回退。

## Open Questions

无。
