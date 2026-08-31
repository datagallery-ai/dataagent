## Context

失败原先由多套模型并行表达：工具层 `ErrorType`、NL2SQL 独立错误类、子进程双轨 error、SDK 错误字典。阶段一只引入内部模型，不接线到 REST / A2A / 子 Agent。

入口与锚点：

- 模型：`dataagent/core/errors.py`
- 日志上下文：`dataagent/utils/log/dataagent_logger.py`
- 测试：`tests/ut/core/test_errors.py`、`tests/ut/test_error_logging_context.py`
- 架构背景：`docs/zh/design_doc/error-handling.md`（归档后改为指向 OpenSpec）

约束：最小实现；不引入错误框架、YAML 注册表或 Result Monad；不为假想调用方做兼容。

## Goals / Non-Goals

**Goals:**

- 内部统一 `DataAgentError(source, component, fact, trace_id)`。
- Actor / wire / REST JSON 使用同一份四字段；`fact=` 是唯一主文案入口。
- secret 不出 Actor；栈只进日志；`trace_id` 只对日志。
- 重试与 HTTP 状态不进错误对象。

**Non-Goals:**

- 接到子 Agent / Job / NL2SQL / MCP / REST / A2A / Executor（阶段二）。
- 恢复 ErrorCode、locator、detail、retryable、http_status。
- 修改 NL2SQL 选优、SSE 帧名、A2A 正常事件顺序。
- OpenTelemetry、告警平台、国际化。

## Decisions

### D1：四字段，不扩码

分类用 `source`（该去哪一类）和 `component`（哪个组件）；具体事实全部写进 `fact`。不靠扩错误码表定位。

备选：ErrorCode + 政策表。否决原因：码表无法承载键名、SQL、sub_id 等定位事实，且已被拍板删除。

### D2：只保留 `to_dict` / `from_dict`

wire / IPC / REST body 只传四字段。`from_dict` 忽略 `locator` / `detail` / `retryable` / `http_status` 等多余键。不要 `to_public_dict` / `to_wire_dict` / `to_actor_dict` 三套序列化。

备选：按出口各做一份 dict。否决原因：同构重复，且容易把内部字段漏出 Actor。

### D3：政策挂在边界，不挂在错误对象

`retryable` / `http_status` 不是 `DataAgentError` 字段。Flex 重试复用 `classify_exception` / `ErrorType` 表，阶段二接到 Executor。REST 业务失败的 HTTP 状态由阶段二边界决定。

备选：把重试次数写进错误对象。否决原因：同一错误在不同边界策略不同，对象会被政策污染。

### D4：Actor 面用 `fact`，secret 与栈不出 Actor

构造器只接受 `fact=`。token、api_key 值、响应全文只进日志。`trace_id` 不进 ToolMessage（阶段二写 `[source/component] fact`）。traceback 走 `logger.exception`。

备选：公开响应回传原始异常。否决原因：泄露凭据和内部路径。

### D5：`from_exception` 只做最小归一

- 已是 `DataAgentError`：原样返回；若指定新 `trace_id` 则只换 `trace_id`。
- `TimeoutError` → `source=constraint`，fact 带超时信息。
- 其它 → `source=internal`，fact 为 `TypeName: 短消息`（脱敏）。

不按中文关键词猜分类。

## Risks / Trade-offs

- [只返回四字段导致难对栈] → 强制 `trace_id` + 失败路径 `logger.exception`；验收看日志能否对上。
- [fact 写进敏感值] → 构造与 `to_dict` / `actor_text` 统一走脱敏。
- [阶段一尚未接到边界，旧出口仍在] → 明确本 change 只交付模型；接线归 `unified-error-handling`。

## Migration Plan

本阶段新增模块，不改公共 REST / A2A 出口。无单独回滚面；整提交回退即可。不为旧 ErrorCode 载荷提供双轨读取。

## Open Questions

无。阶段二边界语义（REST 200、`event: result`、401=AUTHENTICATION 0 次等）不在本 change 拍板范围，见 `unified-error-handling`。
