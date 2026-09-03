# DataFoundry API

Python FastAPI 控制面。同一进程内用官方 `ag-ui-langgraph` 挂载 `create_deep_agent()`。

## 启动

```bash
uv sync
uv run python -m datafoundry_api
```

仓库根目录执行 `npm run dev` 或 `npm run start:api` 也会启动本服务，默认监听 `127.0.0.1:8787`。

## 测试

```bash
uv run pytest
```
