# Deep Agents 扩展

这里放基于 Deep Agents 的二次开发（工具、middleware、子 agent、Skill、backend），**不是**独立 HTTP 服务。

`apps/api` 在 `create_runtime_agent()` 里调用 `deepagents_runtime.extra_*`，再传给官方 `create_deep_agent()`。Web / TUI 仍然只打 `POST /api/copilotkit`。

当前挂钩为空，只预留目录。往 `extensions.py` 里填即可，不要再加 FastAPI / SSE 映射。
