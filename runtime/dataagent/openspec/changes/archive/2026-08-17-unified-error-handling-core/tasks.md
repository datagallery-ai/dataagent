## 1. 核心错误模型

- [x] 1.1 新增 `dataagent/core/errors.py`：`DataAgentError` 四字段 `source` / `component` / `fact` / `trace_id`
- [x] 1.2 构造器只接受 `fact=`，非法 `source` 拒绝；未传 `fact` 使用按 source 兜底
- [x] 1.3 实现 `to_dict` / `from_dict`；`from_dict` 忽略多余键
- [x] 1.4 实现 `from_exception`：原样返回 / TimeoutError→constraint / 其它→internal
- [x] 1.5 实现 fact 与 Actor 文案脱敏（token、api_key、URL 查询参数）

## 2. 日志上下文

- [x] 2.1 日志模块绑定 session / workspace / trace_id 等上下文
- [x] 2.2 失败路径保留 `logger.exception`，栈不进错误对象

## 3. 测试与文档

- [x] 3.1 补充 `tests/ut/core/test_errors.py`：四字段往返、多余键、脱敏、`from_exception`
- [x] 3.2 补充 `tests/ut/test_error_logging_context.py`
- [x] 3.3 将架构背景写入 `docs/zh/design_doc/error-handling.md`，正式规格以本 change 为准
