# 发布

发布前在仓库根目录执行：

```bash
npm run prepare:python
uv run --no-sync ruff check .
uv run --no-sync pytest -q
npm run test:tui
uv run --no-sync pytest -q -m 'tui and not live'
```

持有模型凭据时，可额外运行带 `-m live` 的测试，见 [测试](testing.md)。

## 生成安装包

构建机准备 Node.js 和 uv 后，在仓库根目录执行：

```bash
npm run package:agentkit
```

`make package` 调用同一入口，底层使用标准 `uv build` 和 `uv export`。出包不要求先安装前端依赖，也不编译前端；前端编译仍用 `npm run build` / `make build`。

发行包名沿用 `datagallery-dataagent`，导入名和 CLI 仍为 `dataagent`。版本来自 `pyproject.toml` 的 `project.version`，当前为 `0.1.0`。产物为 `dist/agentkit/datagallery_dataagent-0.1.0-py3-none-any.whl`、`datagallery_dataagent-0.1.0.tar.gz` 和 `constraints.txt`。构建默认先生成源码包，再从源码包构建 wheel。

源码包包含 `dataagent/`、构建配置、README 和许可证。wheel 只安装 `dataagent` 顶层包，包含 `dataagent.server`、Home 模板、提示词，以及 `dataagent/plugins` 下的插件。`constraints.txt` 从现有锁文件导出，包含基础运行依赖（含 MySQL、PostgreSQL 驱动）及 `spark`、`video` 全部 extras 的版本约束，排除开发测试依赖。约束不会主动安装可选包；使用者在安装 wheel 时选择所需 extra。安装工具不会自动使用源码仓库的 `uv.lock`。

安装、模型配置和两个前端的连接步骤见 [包部署](../zh/guides/agentkit-package.md)。这一步只生成文件；推送包仓库或发布版本另行执行。

## 验证安装包与前端

```bash
npm run build
npm run smoke:agentkit-package
```

验收脚本在临时目录构建 wheel/源码包，并在全新 Python 3.12 环境中安装。它验证导入来自已安装包、Home 模板和插件资源可用，以及 SDK、CLI、HTTP 和 A2A 调用；使用本地兼容模型服务检查 Web 代理和 TUI 客户端的流式回复、跨前端续聊与错误返回，不需要模型凭据。TUI 使用真实连接和协议客户端，终端绘制仍由 TUI 单元测试及手工交互覆盖。

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
