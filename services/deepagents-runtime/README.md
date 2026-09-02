# Deep Agents Runtime

DataFoundry 控制面的独立 Agent Runtime。对外只暴露已冻结的 HTTP / SSE 契约，对内调用 Deep Agents SDK 的 `create_deep_agent`。

## 端点

- `GET /health`
- `POST /runs/stream`（AG-UI SSE）
- `POST /runs/:runId/cancel`

## 本地启动

```bash
uv sync
uv run deepagents-runtime
```

或在仓库根目录执行 `npm run runtime:deepagents`。`npm run dev` 会默认拉起本服务并设置 `RUNTIME_SERVICE_URL`。

未配置 `LLM_API_KEY` 时使用脚本化模型，仍走真实 SDK / LangGraph。要强制走线上模型：

```bash
export DEEPAGENTS_RUNTIME_MODEL=live
export LLM_API_KEY=...
export LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export LLM_MODEL=qwen-plus
```

## 测试

```bash
uv run pytest
```
