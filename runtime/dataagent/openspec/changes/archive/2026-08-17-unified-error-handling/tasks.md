## 1. 子 Agent 与 Job

- [x] 1.1 `WorkerResult.error` 改为四字段；失败只走该字段
- [x] 1.2 runner 拒绝顶层 `error`、缺字段、旧 payload、synthetic success
- [x] 1.3 子 Agent 失败生成主 Agent error ToolMessage，content 为 `actor_text()`
- [x] 1.4 Job `queued` / `running` 保持工具成功；终态失败为工具失败
- [x] 1.5 Job 工具级 `status=ERROR` 在边界 raise

## 2. NL2SQL 与 MCP

- [x] 2.1 `nl2sql_sub_agent_tool` 见 `state.error` 则 raise，不写空 CSV
- [x] 2.2 合法零行保持成功；不改 Selector 选优
- [x] 2.3 直连 `chat` 不因 `state.error` 抛
- [x] 2.4 MCP `isError` / 传输失败 raise `DataAgentError`

## 3. REST / A2A / Executor

- [x] 3.1 REST 业务失败 HTTP 200 + `event: result` 四字段
- [x] 3.2 协议层 413 / 429 / 503 / 504
- [x] 3.3 REST NL2SQL 成功路径可 `success: true`
- [x] 3.4 A2A 只在异常时 FAILED，不扫 `state.error`
- [x] 3.5 Executor 复用 `classify_exception`：超时重试 1 次，401=AUTHENTICATION 0 次
- [x] 3.6 ToolMessage 不含 `trace_id`

## 4. 测试与文档

- [x] 4.1 补充 NL2SQL / REST / 子 Agent / Job / MCP / 分类表单测
- [x] 4.2 将 `docs/zh/design_doc/error-handling.md` 改为指向 OpenSpec 的架构指针
- [x] 4.3 归档前 `openspec validate --all --strict`
