# 上下文

对话状态保存在 LangGraph 的消息列表里。运行时按下面的规则控制上下文长度。

## 摘要

core 总会加上摘要中间件。阈值由 `compute_summarization_defaults` 决定：

- 模型 profile 含整数 `max_input_tokens` 时，占用比例到 0.85 触发，保留约 0.10
- 否则在约 170000 token 触发，保留 6 条消息；工具参数截断另有 20 条消息的阈值

摘要使用图上的模型。`model_calls_per_agent` 统计 Agent 的模型循环，一次模型请求里的 HTTP 重试另计在 `max_retries`。

## 工具结果和长消息

文件系统中间件默认在工具结果约 20000 token 时把正文写到产物目录，消息里留下头尾预览和 `read_file` 提示。用户消息约 50000 token 时同样卸载。

卸载文件位于会话的 `outputs/artifacts/`。`/outputs` 跳过 `artifacts/large_tool_results` 和 `artifacts/conversation_history`。

预览按行截断，单行大约 1000 字符，长文本保留头 5 行和尾 5 行。非文本块留在消息里。

## Skill 与提示

Skill 正文不进 Checkpoint。中间件把名称和描述写进系统提示。扩展修订变化时，`VersionedSkillsMiddleware` 丢掉旧的 `skills_metadata` 再扫描。

根提示由文件系统说明和插件 `system_prompt` 拼成。文件系统说明列出 `outputs/` 和输入目录，并要求模型把输入当作只读、把新文件写到输出目录。文件权限见 [安全](security-boundaries.md)。

子代理有自己的提示文件，再附上同一份文件系统说明。

## 调用参数

`ModelContext`、语义客户端和 API 密钥通过调用参数传递，并放在 Pydantic 私有属性中。它们不进入消息或 Checkpoint。远端 MCP 服务使用自己的模型配置。
