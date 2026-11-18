# AgentKit

本地数据 Agent 运行时。它使用内置 Deep Agents 图、LangChain 和 AG-UI。宿主是仓库里的 `apps/web` 和 `apps/tui`，都连接这一个进程。Python 发行包沿用 `datagallery-dataagent`，导入名和命令名是 `dataagent`。产品部署在仓库 `docs/`；本目录的文档讲配置、扩展和运行时。

服务监听 `127.0.0.1`。文件工具和 shell 使用后端进程的权限，默认工作目录是当前会话的 `outputs/`。请在可信的本机环境中运行。

## 出包与独立安装

支持标准 wheel 和源码包。仅运行安装后的后端需要 Python 3.12；Web/TUI 通过 HTTP 连接，前端运行环境单独准备。构建、安装、前端连接及错误检查见 [发布](docs/development/release.md) 和 [包部署](../../docs/zh/guides/agentkit-package.md)。

## 快速启动

源码方式需要 uv 和 Node.js ≥22.12.0，后端使用 Python 3.12，由 uv 发现已有解释器或按配置下载。在仓库根目录：

```bash
npm install
npm run build
```

`npm install` 的安装钩子从锁文件导出带哈希的运行依赖，通过配置的 uv 索引安装，再安装可编辑源码。`npm run prepare:python` 默认包含开发组，与 `uv sync` 一致；仅运行依赖使用 `--no-dev`。基础依赖已包含 MySQL、PostgreSQL 驱动，Spark 和视频能力使用对应 extra 或 `--extra all`。同步会移除未选中的包。一键部署 `./deploy.sh deploy` 默认选择基础数据与视频运行依赖，不包含 Spark 和开发工具；Spark 按需选择。`npm run build` 编译网页和 TUI。

直接启动 TUI，由它拉起后端：

```bash
npm run start:tui
```

地址来自 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。控制面已经在这个地址上时，同一条命令直接连上。

在 `DATAAGENT_HOME`（默认 `~/.dataagent`）里填写 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。

手动启动后端：

```bash
uv run --no-sync --project runtime/agentkit dataagent serve --workspace /existing/data
```

默认地址是 `http://127.0.0.1:8790`，来自 `config.json` 的 `server.host` 和 `server.port`。端口被占用时启动失败，不会改动其他进程。

让其他 agent 通过 A2A 1.0 调用本机 Agent：

```bash
uv run --no-sync --project runtime/agentkit dataagent serve-a2a --workspace /existing/data
```

默认监听 `http://127.0.0.1:9999`。发现地址是 `/.well-known/agent-card.json`，JSON-RPC 在 `/a2a/jsonrpc`。`dataagent serve` 也会挂上这两条路由。这些进程共用同一份状态目录锁，不能同时开。

## 文档

文档在 [docs/README.md](docs/README.md)。

| 目的 | 入口 |
| --- | --- |
| 安装并完成第一次对话 | [快速启动](docs/guides/quickstart.md) |
| 修改配置 | [配置操作](docs/guides/configuration.md) |
| 增加插件、Skill、Hook 或 MCP | [扩展选择](docs/extensions/overview.md) |
| 用插件运行数据工程流程 | [de-agent 插件](builtin-plugins/de-agent/README.md)、[迁移与验证说明](docs/de-agent-migration.md) |
| 查字段和接口 | [配置参考](docs/reference/configuration.md)、[HTTP 接口](docs/reference/rest-api.md) |
| 理解运行过程 | [架构概览](docs/architecture/overview.md) |
| 修改代码并验证 | [开发环境](docs/development/setup.md) |

可运行示例在 [examples/](examples/README.md)。
