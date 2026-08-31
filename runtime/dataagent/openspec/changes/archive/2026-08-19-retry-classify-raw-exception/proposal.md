## Why

重试和对外展示是两件事，但当前 `classify_exception` 扫 fact 中英文关键词，又用 `source=internal` 短路和一层 `__cause__` 当重试通道。Wrapper 用 `tool_failure(fact=str(e))` 丢掉异常类型后，中文 fact 无法重试，英文 fact 却「碰巧」对上关键词。现在把重试改回原始异常类型 + HTTP 状态，四字段只负责展示入口。

## What Changes

- 重试只看异常类型与 HTTP 状态：`TimeoutError`、`ConnectionError` / `httpx.RequestError`、`httpx.HTTPStatusError.status_code`。MUST NOT 扫 fact（中英都不扫）。
- 不用 `source=internal` 短路，不用 `__cause__` 当重试通道。`__cause__` 只留给栈 / 日志。
- Wrapper 不得 `tool_failure(fact=str(e))` 丢掉类型；原样 raise 或 `ToolResult` 带原异常，manager raise **原异常**。本地 / MCP 不走 `from_exception`。
- `source=config` 校验失败：调用点直接不进重试，不靠 `DataAgentError.source` 短路。
- `from_timeout_dict` 若只为重试存在则删除；跨进程不靠合成 cause。
- 展示仍是四字段 `source/component/fact/trace_id`；REST 业务失败 HTTP 200。REST / SSE / SDK chat 形状不变。
- Depends 取不到服务（`_data_agent_service is None`）时 REST query / stream 返回 HTTP 503、短 `detail`，不是 500，也不是 200 四字段。`/health` 未就绪 503 保持。
- `measure_tool` 采集字段改从 `classify_exception`（类型 + HTTP 状态 / Executor 政策）填写，不再读执行对象上已删除的 `error_type` / `retry_info`。不把旧字段加回 `NormalizedToolExecution`。
- 接受代价：只有英文 fact、没有类型的「碰巧 3 次」变为 1 次。

## Capabilities

### New Capabilities

- （无。不新增能力域。）

### Modified Capabilities

- `error-boundary-propagation`: Executor 重试 MUST 按类型 + HTTP 状态分类；MUST NOT 扫 fact 中英文关键词；401 改为「异常带 HTTP 状态」而不是「信息匹配」。四字段只给出展示入口。Depends 取不到服务时 REST MUST 返回 503。性能采集 MUST 用同一次分类结果填字段。

## Impact

- 代码：`classify_exception`、Flex Executor 重试、本地 / MCP wrapper、`DataAgentError.from_timeout_dict` 调用点、语义客户端对 httpx 的包装、`measure_tool`。
- API：REST / SSE / SDK chat 形状不变；业务失败仍 HTTP 200 + 四字段。Depends 缺失改为协议 503。
- 行为：跨进程超时不再靠合成 `__cause__` 获得 TIMEOUT 重试；无类型、仅英文 fact 的旧「碰巧重试」变为 UNKNOWN（1 次）。
- 测试：删 / 改 fact 关键词用例；未知 `Exception` 按类型 UNKNOWN，不扫 timeout / 429。
