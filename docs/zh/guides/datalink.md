# DataLink 语义服务

DataLink 是 DataFoundry 的可选语义图服务，用于将 Schema 与数据画像连接到业务概念、实体、可 JOIN 路径和带置信度的关系。实现位于 `services/datalink`，但作为独立 Python 服务运行。

## 运行拓扑

| 进程 | 默认地址 | 用途 |
| --- | --- | --- |
| DataLink MCP | `http://127.0.0.1:8080/mcp` | 提供 `datalink_explore`，为 Agent 提供语义上下文 |
| DataLink REST | `http://127.0.0.1:8081` | 图谱管理与可视化 API |

DataFoundry 的 Web 与 API 进程不受影响。`deploy.sh` 和 `npm run start` 不会启动 DataLink；需要语义增强时请单独启动。

## 安装与启动

进入服务目录，准备 Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)：

```bash
cd services/datalink
uv pip install -e .
cp datalink_config.example.json datalink_config.json
```

在 `datalink_config.json` 中配置 LLM 与 embedding 提供商，然后在两个终端分别启动：

```bash
uv run datalink serve --port 8080 --transport streamable-http
uv run datalink api --port 8081
```

提供商示例、建图和探索命令见服务目录下的 README。

## 在 DataFoundry 中连接

在 Web 工作台打开 MCP 设置，添加外部服务：

| 字段 | 示例 |
| --- | --- |
| `serverUrl` | `http://127.0.0.1:8080/mcp` |
| `apiUrl` | `http://127.0.0.1:8081` |
| `transport` | `streamable-http` |
| `toolManifest` | `[{ "name": "datalink_explore" }]` |

名称或 id 中包含 `datalink`（或 `datagraph`）时，DataLink 面板会识别该服务。

## 验证

```bash
curl http://127.0.0.1:8081/healthz
```

如果面板显示服务不可用，请检查两个进程、`8080`/`8081` 端口、图数据库路径和 MCP transport。API Key 应放在环境变量或 Secret 管理系统中，不要提交到 `datalink_config.json`。
