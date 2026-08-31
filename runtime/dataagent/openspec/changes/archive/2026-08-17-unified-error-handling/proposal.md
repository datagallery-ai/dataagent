## Why

阶段一已给出四字段 `DataAgentError`，但失败仍可能在子 Agent、Job、NL2SQL、MCP、REST / A2A 和 Executor 被吞成空成功或假成功。阶段二把同一模型接到各边界，并禁止假成功。

## What Changes

- 内部失败继续只抛 `DataAgentError`；中间层传播，系统边界各序列化一次。
- 子 Agent 失败经 `worker_result.error` 四字段回到主 Agent，ToolMessage 为 `status=error`，文案为 `[source/component] fact`，不含 `trace_id`。
- Job：`queued` / `running` 视为工具成功；终态 `failed` / `timed_out` / `cancelled` / `unknown` / `not_found` 视为工具失败；工具级 `status=ERROR` 在边界 raise。
- NL2SQL：`nl2sql_sub_agent_tool` 见 `state.error` 则 raise，不写空 CSV；合法零行仍成功；不改选优。直连 `chat` 不因 `state.error` 抛。
- MCP 调用失败 raise `DataAgentError`，不得当成功 data 返回。
- REST：业务失败 HTTP 200 + `event: result` / `{"result": ...}` 四字段；协议层 413 / 429 / 503 / 504。REST NL2SQL 成功路径可返回 `success: true`。
- A2A 只在捕获异常时发 FAILED，不扫描 `state.error`。
- Executor 复用 `classify_exception` / `ErrorType`：超时重试 1 次；401 = AUTHENTICATION，重试 0 次。
- **BREAKING**：删除旧错误字典出口、synthetic success、A2A / REST 启发式扫 state；不提供向后兼容。

## Capabilities

### New Capabilities

- `error-boundary-propagation`: REST / A2A / SDK / MCP / Job / Executor 的失败可见性、HTTP 与重试边界。
- `nl2sql-error-propagation`: NL2SQL 工具路径上报失败，禁止静默空成功。
- `subagent-error-propagation`: 子 Agent 四字段 wire 与主 Agent ToolMessage。

### Modified Capabilities

- （无。不改写 `error-model` 的四字段契约；不改 `flex-react-runtime` / `tool-action-space` 既有需求条文。）

## Impact

- 代码：REST / A2A / SDK、NL2SQL 工具与 Agent、子进程 runner / WorkerResult、Job 工具、MCP、Flex Executor、`classify_exception`。
- API：**BREAKING** — 子 Agent / Job 失败不再装成功；旧顶层 `error` / synthetic success 直接拒绝。
- 调用方：主 Agent 会看到子 Agent / Job 终态失败的 error ToolMessage；REST 客户端继续用 HTTP 200 解析业务失败四字段。
- 不改：NL2SQL 选优算法、REST 流式帧名保持 `event: result`、直连 chat 不因 `state.error` 抛、A2A 不扫 state。
