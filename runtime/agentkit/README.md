# AgentKit

本地数据 Agent 运行时。它使用内置 Deep Agents 图、LangChain 和 AG-UI。前端是仓库里的 `apps/tui`。Python 包名、分发名和命令名都是 `dataagent`。

服务监听 `127.0.0.1`。文件工具和 shell 使用后端进程的权限，默认工作目录是当前会话的 `outputs/`。请在可信的本机环境中运行。

## 快速启动

需要 macOS 或 Linux、Python 3.12、uv，以及仓库的 Node.js 依赖。在仓库根目录：

```bash
uv sync --project runtime/agentkit --locked
npm run start:tui -- --v2
```

首次启动创建 `~/.dataagent/config.json`、`.env`、`skills/`、`hooks/` 和 `plugins/`，不覆盖已有文件。在 `~/.dataagent/.env` 填写 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`，再运行一次。

沿用仓库根目录的 `.env` 时：

```bash
npm run start:tui -- --v2 --env-file .env --workspace "$PWD"
```

手动启动后端：

```bash
uv run --project runtime/agentkit dataagent serve --workspace /existing/data
```

默认地址是 `http://127.0.0.1:8790`。端口被占用时启动失败，不会改动其他进程。

## 文档

文档在 [docs/README.md](docs/README.md)。

| 目的 | 入口 |
| --- | --- |
| 安装并完成第一次对话 | [快速启动](docs/guides/quickstart.md) |
| 修改配置 | [配置操作](docs/guides/configuration.md) |
| 增加插件、Skill、Hook 或 MCP | [扩展选择](docs/extensions/overview.md) |
| 查字段和接口 | [配置参考](docs/reference/configuration.md)、[HTTP 接口](docs/reference/rest-api.md) |
| 理解运行过程 | [架构概览](docs/architecture/overview.md) |
| 修改代码并验证 | [开发环境](docs/development/setup.md) |

可运行示例在 [examples/](examples/README.md)。
