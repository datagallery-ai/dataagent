## 1. 分类表与展示入口

- [x] 1.1 重写 `classify_exception`：按 `TimeoutError` / `httpx.TimeoutException`、`ConnectionError` / `httpx.RequestError`、`HTTPStatusError.status_code`（401 / 429）分类；不扫 fact；不用类型名含 `network`
- [x] 1.2 删除 `from_timeout_dict`；IPC / Job 还原改 `from_dict`；不合成 cause
- [x] 1.3 更新 `openspec/specs/error-boundary-propagation/spec.md` 主规格：按类型 + HTTP 状态分类；禁止扫 fact；401 改为异常带 HTTP 状态

## 2. 传递原异常

- [x] 2.1 本地 / MCP / A2A wrapper：禁止 `tool_failure(fact=str(e))`；原样 raise 或 ToolResult 带原异常
- [x] 2.2 `ToolManager.acall` raise 原异常；本地 / MCP 不走 `from_exception`
- [x] 2.3 语义客户端对 httpx 超时 / 网络 / HTTP 状态原样 raise；`from_config` 校验失败不进重试
- [x] 2.4 Executor：按捕获的原异常分类；去掉 `source` 短路与 `__cause__` 重试通道；配置 / 入参校验调用点不进重试

## 3. 测试与验证

- [x] 3.1 改 `tests/ut/core/test_errors.py`：删 / 改 fact 关键词用例；补类型表与 HTTP 状态；跨进程不合成 cause
- [x] 3.2 `test_unknown_exception_does_not_use_message_classification` 改为 Exception 按类型 UNKNOWN，不扫 timeout / 429
- [x] 3.3 更新 executor / local tools / semantic / rest errors 相关用例
- [x] 3.4 `uv run ruff format` / `ruff check` 与相关 pytest 全绿

## 4. Depends 缺失 → 503

- [x] 4.1 `get_data_agent_service()` 在 service 为 None 时抛 `HTTPException(503)`，短 `detail`；不加 query 前 `is_ready()` guard，不改 lazy-init
- [x] 4.2 更新 `openspec/specs/error-boundary-propagation/spec.md`：Depends 缺失 → 503；`/health` 未就绪 503 保持；已初始化业务失败仍 200
- [x] 4.3 `tests/ut/interface/test_rest_api_errors.py`：non-stream / stream missing-service → 503
- [x] 4.4 ruff + `test_rest_api_errors.py` 与 errors / executor 回归全绿

## 5. 性能采集跟分类表

- [x] 5.1 `measure_tool` 用 `classify_exception` 填类型 + HTTP 状态 / policy；不读、不加回 `error_type` / `retry_info`
- [x] 5.2 `tests/ut/test_performance_integration.py` 改用真实 `NormalizedToolExecution`，覆盖分类结果与不扫 fact
- [x] 5.3 ruff + performance / errors / executor 相关 pytest 全绿
