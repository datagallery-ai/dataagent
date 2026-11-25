# 发布

发布前在仓库根目录执行：

```bash
uv sync
uv run --no-sync ruff check .
uv run --no-sync pytest -q
npm run test:tui
uv run --no-sync pytest -q -m 'tui and not live'
```

持有模型凭据时，可额外运行带 `-m live` 的测试，见 [测试](testing.md)。

## 构建流水线产物

在仓库根目录执行 `make prepare` 和 `make build PYTHON_VERSION=3.12`，归档 `dist/server/` 下的 Python wheel 和 `dist/tui/` 下的 TUI npm 包。Web 构建文件留在 `ui/web/.next/`。

源码部署继续使用 `npm run build`。独立交付后的安装与连接见[包部署](../zh/guides/agentkit-package.md)。

## 单独生成 Python 安装包

```bash
uv build --wheel --clear --out-dir dist/server
```

发行包名为 `datagallery-dataagent`，导入名和 CLI 为 `dataagent`。版本来自 `pyproject.toml`。包包含 Server、Home 模板、提示词和内置插件。安装时通过包元数据解析运行依赖，可选择 `spark`、`video` 或 `all`。

## 验证安装包与前端

```bash
make build
npm run smoke:agentkit-package
```

验收脚本在仓库外安装本次构建的 wheel 和 TUI npm 包。它验证 CLI、插件资源、SDK、HTTP 和 A2A，并通过本地测试模型验证 Web 代理、TUI 安装后的启动入口、流式对话及错误处理。

缺少模型时，应为 `/healthz=200`、`/ready=503`；模型请求失败应产生 `RUN_ERROR`，不报告成功；后端停服时 Web 应返回 502，TUI 应明确报连接错误，不另起源码后端。外部模型与数据库服务仍需单独配置和验证。

## 配置版本

| 对象 | 版本 | 读取规则 |
| --- | --- | --- |
| 产品 `config.yaml` | `schema_version` 4 | 其他版本会导致启动失败，程序不改写该文件 |
| 插件 `.plugin.yaml` | `schema_version` 3 | 其他版本会导致加载失败 |
| 子代理 YAML | `schema_version` 1（可省略） | 其他版本会导致加载失败 |
| Hook | 六个事件和 Python 入口 | 声明字段是 `event`、`entrypoint`、`params` |
| MCP | `.mcp.yaml` 的 `mcpServers` | 连接写在该文件中，不写在产品配置里 |

启动不改写已有配置文件。环境引用的写法是 `$env{NAME}`。

## 状态兼容

- Checkpoint 保存在 `<home>/runtime/checkpoints.sqlite`，使用 LangGraph 的 Sqlite saver。修改 `DeepAgentState` 或私有状态键之后，确认已有会话仍能 `aget_state`。
- `session_bindings` 是会话固定输入和输出绑定的唯一来源；首版不支持旧文件格式的兼容读取或迁移。
- 进程启动时，索引里仍为 `running` 的会话改为 `interrupted`。这些轮次不会自动重放。
- Trace 使用 OTLP JSON。更早的自定义轨迹文件保持原样。
- 长工具结果的路径写在消息里。修改 `artifacts/large_tool_results` 前缀后，旧消息中的路径会失效。
