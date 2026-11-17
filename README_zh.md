# DataAgent

基于 Python AgentKit / Deep Agents 的本地数据 Agent，提供 Web、TUI、REST、A2A 和 Python SDK。主代理可以委派本地 nl2sql 等插件，按会话保存对话状态、结果文件和 Trace。

[English](README.md) · [中文文档](docs/zh/README.md) · [快速开始](docs/zh/quick-start.md)

## 当前架构与能力

```text
Web → Next 同源转发 ─┐
TUI → AG-UI / REST ─┼→ dataagent serve → 主代理 → 工具 / 本地子代理 / MCP
其他 Agent → A2A ───┘                        └→ nl2sql → 已配置数据库
Python SDK → 同一套配置准备与 Agent 装配（可跳过 HTTP）
```

- **同一后端，多种入口**：Web/TUI 连接同一个 Python 控制面；A2A 1.0 通过 JSON-RPC 调用同一运行时。也可独立启动只提供 A2A 的进程。
- **会话与产物**：服务端保存 Checkpoint、会话索引、Trace 和产物。A2A、Web、TUI 可续接同一会话；A2A 的内存 Task 在重启后不保留。
- **插件与子代理**：通过配置启用插件、Skill、Hook、MCP 和编译型子代理。数据库查询能力来自已启用且配置完整的插件，不是控制面自带的数据源注册服务。
- **配置驱动**：模型、服务地址、插件与输入目录来自 Home 配置和环境。Web 不提供模型 Profile 创建或测试。
- **诊断与关联**：日志收敛到 `backend.log`、`web.log`、`tui.log`；A2A 请求号、任务号、运行号可关联日志和本地 Trace。

当前没有注册登录、网页模型管理、数据源注册、知识库管理、会话分支或 trace DAG 接口。旧页面残留入口不代表后端已经提供对应能力。完整范围见 [能力全览](docs/zh/capabilities.md)。

## 🚀 正式态跑通

需要 Node.js ≥22 和 uv，后端使用 Python 3.12。安装与运行应处于同一环境；Windows 与 WSL 不要共用 `node_modules`。本版本不提供 Docker / Compose 部署。

### Ubuntu / Debian 一键部署

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

脚本准备依赖、配置与构建，并在后台启动 Web/API。部署阶段不要求填写模型密钥；对话前仍需在仓库根 `.env` 或 Home `.env` 填入可用模型，再重启服务。TUI 已构建但不会后台常驻，需另开终端运行 `./deploy.sh tui`。

```bash
./deploy.sh status
./deploy.sh doctor
./deploy.sh logs
./deploy.sh stop
./deploy.sh start
```

关闭终端或退出日志跟随不会停止受管服务，停止请用 `./deploy.sh stop`。重复部署会先停止受管服务再安装和构建；需要重新配置端口时使用 `./deploy.sh deploy --reconfigure`。

### 手动 npm

原生 Windows、macOS、其他 Linux，或需要手动配置时：

```bash
npm install
cp .env.example .env
```

编辑仓库根 `.env`：

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

```bash
npm run build
npm run start
# 另开终端，可选
npm run start:tui
```

直接打开 `http://127.0.0.1:3000/data-tasks`，无需注册或创建网页模型 Profile。先提问“用三句话说明你现在能做什么”；数据库问题要先启用并配置相应插件。

## 配置与独立启动

Home 默认 `~/.dataagent`，可通过进程环境 `DATAAGENT_HOME` 指定。CLI 首次启动会初始化缺少的配置模板。

| 文件/设置 | 作用 |
| --- | --- |
| `<Home>/config.json` | 产品配置，`schema_version: 4`，包含 `models`、`dataagent`、`plugins`、`server` 等 |
| `<Home>/.env` | 配置中 `$env{NAME}` 的值；非空进程环境覆盖文件值 |
| `--config` | 替代 Home 配置，不自动改读旁边的 `.env` |
| `--env-file` | 显式替代 Home `.env`；需要时用于直接 Python CLI |
| `server.host / port` | 后端默认 `127.0.0.1:8790`；Web 默认端口为 3000 |

```bash
# 只启动后端；也提供 A2A 路由
uv run --project runtime/agentkit dataagent serve

# 只启动 A2A。与 serve 二选一，同一 Home 不能并行启动两个后端
uv run --project runtime/agentkit dataagent serve-a2a
```

`serve` 提供 `/healthz`、`/ready`、`/api/v1/runs`、会话和产物接口。`serve-a2a` 不提供这些 REST/健康接口。SDK 可通过 `prepare_runtime`、`create_agent` 和 `ainvoke` 直接调用；其 checkpointer、异常处理和日志配置由调用方宿主管理。

## A2A 调用

服务提供 `GET /.well-known/agent-card.json` 与 `POST /a2a/jsonrpc`。请求必须带 `A2A-Version: 1.0`；支持默认等待结果和 `returnImmediately: true` 的异步任务查询，不支持 A2A 流式响应；[参数与调用方式](runtime/agentkit/docs/reference/a2a.md#等待结果与异步任务) 见详细示例。

```bash
curl -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"request-1","method":"SendMessage","params":{"message":{"role":"ROLE_USER","messageId":"message-1","contextId":"thread-1","parts":[{"text":"用三句话说明你现在能做什么。"}]}}}'
```

设置 `DATAAGENT_A2A_AUTH_TOKEN` 后，请求还需携带 `Authorization: Bearer <token>`，Agent Card 仍可匿名读取。执行失败可能仍返回 HTTP 200，须检查 Task 状态和 `metadata.dataagent.error`，不能只看 HTTP 状态。协议与查询/取消约定见 [REST 与 A2A](docs/zh/reference/rest-api.md)。

## 日志与问题定位

`<Home>/runtime/logs/` 下只使用三个主文件：

| 文件 | 内容 |
| --- | --- |
| `backend.log` | 后端、插件、模型、A2A、公共依赖准备及启停 |
| `web.log` | Web 构建、服务端、代理及浏览器上报错误 |
| `tui.log` | TUI 构建、连接、传输与交互 |

每个文件按 5,000,000 字节轮转、保留 3 个备份。按 `X-Request-ID`、Task ID 和运行号关联记录；相关进程未写入时文件可能尚未创建。浏览器错误上报依赖 Web 可达，不保证离线保存。

## 安全边界

- 默认只监听本机。REST 没有访问令牌；A2A 的可选共享令牌仅保护 JSON-RPC，不构成用户登录或租户隔离。
- 已知凭据在日志和 Trace 导出前脱敏，但对话、SQL、工具结果和 CSV 仍可能包含业务数据。
- 工作区的只读约定和插件 SQL 规则不是操作系统沙箱。文件工具与 shell 使用后端进程权限；nl2sql 的 SQL 安全策略需要按配置启用。

详情见 [安全说明](docs/zh/security.md) 和 [支持的输入](docs/zh/reference/supported-datasources.md)。

## 文档与开发

- [快速开始](docs/zh/quick-start.md)：部署、模型配置、启停与排查。
- [架构概览](docs/zh/architecture/overview.md)：控制面、Web/TUI 与 A2A 的关系。
- [配置](docs/zh/reference/configuration-api.md)、[API](docs/zh/reference/rest-api.md)：集成时的现行契约。
- [贡献指南](CONTRIBUTING.md)。运行时、插件与 SDK 的技术文档位于仓库 `runtime/agentkit/docs/`。
- [v0.2.0 历史发布说明](docs/zh/releases/v0.2.0.md)：记录当时的产品设计与能力，不作为当前重构后的验收清单。

```bash
npm run test:deploy
npm run test:web
npm run test:tui
npm run test:product
```

Apache-2.0，见 [LICENSE](LICENSE)。
