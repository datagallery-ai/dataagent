## Why

DataAgent 失败原先用异常、错误字典、状态字段和字符串混杂表达，公开面缺少稳定、可定位、可脱敏的结构。阶段一先落地唯一内部错误模型，让后续边界传播有同一套四字段可序列化、可还原。

## What Changes

- 引入唯一内部错误类型 `DataAgentError`，公开契约固定为四字段：`source`、`component`、`fact`、`trace_id`。
- 构造器只接受 `fact=`，不提供 `message=` 别名；未传 `fact` 时才用按 `source` 的短兜底。
- 序列化只保留 `to_dict` / `from_dict`；`from_dict` 只读四字段，忽略多余键。
- `fact` 与 Actor 文案脱敏：token、api_key 值、响应全文不出 Actor 面。
- `source` 仅允许 `config` / `llm` / `tool` / `internal` / `constraint`。
- `from_exception`：已是 `DataAgentError` 则原样返回（可只换 `trace_id`）；`TimeoutError` → `source=constraint`；其它 → `source=internal`。
- 重试与 HTTP 状态不挂在错误对象上；分类政策留给边界（阶段二接到 Executor）。
- 系统入口绑定日志上下文，`trace_id` 只用来对日志，不进 ToolMessage。
- **BREAKING**：删除以 ErrorCode / locator / detail / retryable / http_status 为一等公民的旧错误模型；不提供向后兼容。

## Capabilities

### New Capabilities

- `error-model`: DataAgent 统一错误模型、四字段契约、脱敏与序列化还原。

### Modified Capabilities

- （无。现有 `agent-config-bootstrap` / `flex-react-runtime` / `tool-action-space` 的需求不在本阶段改写。）

## Impact

- 代码：`dataagent/core/errors.py`（新增）、日志上下文（`dataagent/utils/log/`）。
- 测试：`tests/ut/core/test_errors.py`、`tests/ut/test_error_logging_context.py`。
- 文档：架构背景仍放在 `docs/zh/design_doc/`，正式变更与验收规格以 OpenSpec 为准。
- 不改：NL2SQL 选优、REST / A2A / 子 Agent / Job / MCP / Executor 接线（阶段二）。
- 兼容：本阶段只引入模型，不改公共 REST / A2A 出口形状；旧 ErrorCode 载荷不作为本阶段兼容目标。
