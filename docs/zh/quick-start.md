# 快速开始

这篇文档面向第一次部署 DataFoundry 的用户。正式态有两条路径，**启动后都是** `password` 认证的 Web 工作台（不要跑 `npm run dev`）：

| 路径 | 适用环境 | 入口 |
| --- | --- | --- |
| **推荐：一键部署** | Ubuntu / Debian | `./deploy.sh`（配置、依赖、构建含 TUI、detached 后台启动 Web/API 与健康检查一次完成） |
| **手动 npm** | Windows、macOS、其他 Linux，或需要手改环境变量时 | `npm install` → 配置 `.env` → `npm run build` / `build:web` → `npm run start` |

部署完成后在 Web 中配置模型，再用内置 DTC Growth Review 数据源跑通一次分析。本版本**不提供** Docker / Compose。

## 环境要求

- **一键部署**：Ubuntu 或 Debian（x86_64 / aarch64）；Node.js 22（缺失时脚本可在确认后协助安装）
- **手动 npm**：Linux、macOS 或 Windows；Node.js >= 22 与 npm
- 可选外置 [Data Link](https://github.com/datagallery-lab/datalink)：需要语义图谱能力时单独运行（一键部署不依赖）

请在同一环境内安装和运行项目。Windows 用户不要在 Windows 和 WSL 之间共用 `node_modules`。

## 推荐：Ubuntu / Debian 一键部署

`./deploy.sh` **不支持**原生 Windows / macOS（请改用下文手动 npm）。

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

部署成功后 Web + API 在后台常驻（独立进程组）。关闭终端，或在 `./deploy.sh logs` 中按 `Ctrl+C`，**都不会**停止 DataFoundry——停止请用 `./deploy.sh stop`。一键部署会一并构建 TUI，但 TUI **不会**随 stack 后台常驻；需要时另开终端按成功提示启动（见下文「启动 TUI」）。

打开 `http://127.0.0.1:3000/login`（若端口被改过，以脚本打印的 Web 地址为准），注册并登录，在模型配置中创建、测试并启用 OpenAI-compatible Profile，然后进入 `/data-tasks`。

### 交互与配置规则

- 首次部署：脚本生成 `.env` 与 `apps/web/.env.local`，并确认端口 / 公开访问地址。部署阶段不要求填写模型 Key。
- 之后再执行交互式 `./deploy.sh` / `./deploy.sh deploy`：若已有完整 `.env`，会跳过配置问答。
- 需要重新配置端口或公开访问地址时（保留现有密钥，并先备份 `.env`）：

```bash
./deploy.sh deploy --reconfigure
```

- 无人值守 / CI 默认（不提问；端口冲突或需要 sudo 密码的安装会立即失败）：

```bash
./deploy.sh deploy --non-interactive
```

`--reconfigure` 与 `--non-interactive` 互斥，且仅对 `deploy` 有效。

### 生命周期命令

```bash
./deploy.sh status    # 进程与 API / Web 健康状态
./deploy.sh start     # 用已有构建启动（不安装、不构建）
./deploy.sh stop      # 只停止受管进程组
./deploy.sh restart   # 停止后启动（不安装、不构建）
./deploy.sh logs      # 跟随运行日志；Ctrl+C 不停止服务
./deploy.sh doctor    # 只读检查依赖 / 配置 / 端口 / 磁盘 / 健康
./deploy.sh tui       # 可选：前台启动 TUI（需 API 已健康；不是受管后台服务）
./deploy.sh help
```

部署时不要求填写 `LLM_*`。远程主机请设置 `AUTH_PUBLIC_BASE_URL`。重复部署会进入维护窗口：先停止受管进程组，再执行 `npm ci` 与构建。

### 外置 Data Link（可选）

一键部署**只启动** Web + API，**不会**安装、启动或健康检查 Data Link。

若需要语义能力，请单独运行 [Data Link](https://github.com/datagallery-lab/datalink)（常见为 MCP `:8080` + REST `:8081`），再在 Web 工作台的 MCP 设置中添加外部服务，例如：

| 字段 | 示例 |
| --- | --- |
| `serverUrl` | `http://127.0.0.1:8080/mcp` |
| `apiUrl` | `http://127.0.0.1:8081` |
| `transport` | `streamable-http` |
| `toolManifest` | `[{ "name": "datalink_explore" }]` |

名称/id 包含 `datalink`（或 `datagraph`）即可被 Data Link 面板识别。

## Windows / macOS / 其他：手动 npm 部署

`./deploy.sh` **仅面向 Ubuntu / Debian**，不支持原生 Windows / macOS。在 Windows、macOS 或其他发行版上，请按下列步骤用 npm 安装、配置并启动。需要手改环境变量或拆分进程时，也可走这条路径。两种正式态都**不要跑** `npm run dev`。贡献者热更新见文末附录。

正式态对照：

| 环境 | 用途 | `AUTH_EMAIL_DELIVERY` | `AUTH_PUBLIC_BASE_URL` |
| --- | --- | --- | --- |
| **正式测试** | 本机或内网验收、联调 | `test`（验证/重置链接打到控制台） | 如 `http://127.0.0.1:3000` |
| **真实生产** | 对外服务 | `smtp`（真实发信） | 公网 HTTPS 域名 |

两种正式态都**不要跑** `npm run dev` / `dev:api` / `dev:web`。贡献者本地热更新见文末附录。

### 1. 安装依赖

在仓库根目录执行：

```bash
node -v
npm install
```

`node -v` 输出需要不低于 22。首次安装会生成本地 DTC Growth Review SQLite fixture，并编译工作区依赖；耗时取决于机器和网络。

### 2. 配置环境变量

```bash
cp .env.example .env
cp apps/web/.env.example apps/web/.env.local
```

#### 2.1 模型（可选 server-default）

打开根目录 `.env`，可填写可选的服务端默认模型（也可仅在 Web 中配置）：

```bash
LLM_PROVIDER=openai-compatible
LLM_MODEL=qwen-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=你的_API_Key
```

DeepSeek 示例：

```bash
LLM_PROVIDER=openai-compatible
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的_API_Key
```

### 2.2 正式测试（推荐首次验收）

根目录 `.env`：

```bash
DATAFOUNDRY_AUTH_MODE=password
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000
AUTH_EMAIL_DELIVERY=test
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
# smtp 相关可先留空
```

`apps/web/.env.local`（会在 `next build` 时打进前端）：

```bash
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password
# 正式态留空，走同源 BFF（Cookie + CSRF）
NEXT_PUBLIC_AGENT_RUNTIME_URL=
NEXT_PUBLIC_CONFIG_API_URL=
API_PROXY_TARGET=http://127.0.0.1:8787
```

注册/重置密码时，验证链接会打印在 **API 进程控制台**，复制到浏览器即可。

### 2.3 真实生产

在正式测试配置基础上改为：

```bash
DATAFOUNDRY_AUTH_MODE=password
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=https://datafoundry.example.com
AUTH_EMAIL_DELIVERY=smtp
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
AUTH_SMTP_HOST=smtp.example.com
AUTH_SMTP_PORT=587
AUTH_SMTP_SECURE=false
AUTH_SMTP_USER=
AUTH_SMTP_PASSWORD=
```

前端同样保持 `password` + 空公开 API URL + `API_PROXY_TARGET`。对外入口请用反代，样例见 [`deploy/nginx.datafoundry.conf.example`](https://github.com/datagallery-lab/datafoundry/blob/main/deploy/nginx.datafoundry.conf.example)：静态资源压缩，SSE 路径 `/api/copilotkit` 关闭 gzip 与 `proxy_buffering`。

### 3. 构建并启动（正式测试 / 真实生产相同）

```bash
npm run build
npm run build:web
npm run start:api    # :8787
npm run start:web    # :3000
```

检查：

```bash
curl http://127.0.0.1:8787/healthz   # 进程存活
curl http://127.0.0.1:8787/ready     # Mastra / builtin 就绪（含 startup_ms）
```

打开 [http://127.0.0.1:3000/login](http://127.0.0.1:3000/login)（真实生产则打开你的公网域名）注册或登录后进入 `/data-tasks`。

改过 `apps/web/.env.local` 中的 `NEXT_PUBLIC_*` 后，需要重新执行 `npm run build:web`。

## 跑通第一个问题

打开 `/data-tasks` 后：

1. 点击「新建数据任务」。
2. 选择内置 **DTC Growth Review** 数据源。
3. 在输入框旁选择「服务端默认」或你配置的模型。
4. 发送第一个问题。

推荐问题：

```text
帮我查看数据源里有哪些表，并说明每张表的主要字段。
```

统计问题：

```text
对比各渠道的 GMV、毛利、投放和退款，并说明下一轮预算应优先增加到哪个渠道。
```

你看到 schema 检查、SQL 执行和结果产出后，说明链路已经跑通。

## 启动 TUI

一键部署会在构建阶段准备好 TUI，但**不会**自动启动，也不会把它当作后台受管进程。后端（API）运行后，另开终端启动前台客户端：

```bash
./deploy.sh tui
# 或：npm run start:tui
```

指定当前部署的 API 地址（可选；默认使用 `.env` 中的 `API_PORT`）：

```bash
./deploy.sh tui --runtime-url http://127.0.0.1:8787/api/copilotkit
```

登录需要可用的 API 与密码账户（离线演示模式已移除）：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
```

恢复最近的服务端会话：

```bash
npm run start:tui -- --resume
```

更多命令见 [TUI 指南](guides/tui.md)。

## 排查

一键部署路径请先看：

```bash
./deploy.sh status
./deploy.sh doctor
./deploy.sh logs
```

手动 npm 路径则确认 `npm run start` 仍在运行，并检查对应终端输出。

### Node 版本不对

现象：`npm install` 或构建阶段报 Node 版本错误。

处理：

```bash
node -v
# 一键部署：
./deploy.sh doctor
```

升级到 Node.js 22 或更高版本后重试。一键部署也可让 `./deploy.sh` 在确认后协助安装；手动路径则重新执行 `npm install`。

### 页面打不开

现象：浏览器打不开工作台地址。

处理：

- 先执行 `./deploy.sh status`（手动路径则确认 `npm run start` 仍在运行）。正式态不要开 `dev`。
- 检查 3000 端口是否被占用；若部署时改过端口，以脚本打印的 Web 地址为准。
- 若进程已停止：`./deploy.sh start`。

### 后端未启动

现象：页面能打开，但发送问题没有响应，或资源面板加载失败。

处理：

```bash
./deploy.sh status
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/ready
```

如果健康检查失败：

```bash
./deploy.sh start
# 手动路径：npm run start
```

### 注册收不到邮件

- **正式测试**（`AUTH_EMAIL_DELIVERY=test`）：到运行 `start:api` 的终端里找验证链接。
- **真实生产**（`smtp`）：检查 `AUTH_SMTP_*` 与发信账号；确认 `AUTH_PUBLIC_BASE_URL` 与对外域名一致。

### 模型不可用

现象：Agent run 报 provider、401、rate limit 或 model not found。

处理：

- 检查 `.env` 中的 `LLM_API_KEY`。
- 检查 `LLM_BASE_URL` 是否以模型服务的兼容接口为准。
- 检查 `LLM_MODEL` 是否在你的账号下可用。
- 在 Web 工作台的模型配置里执行测试动作。

### 端口冲突

默认端口：

| 服务 | 端口 |
| --- | --- |
| Web | 3000 |
| API | 8787 |

如果端口被占用，先停止占用进程，或按终端输出访问新的端口。改后端端口后，同步更新 `API_PROXY_TARGET`。

### 数据库连接失败

- PostgreSQL / MySQL 等服务端数据库需要网络可达。
- SQLite、CSV、Excel、DuckDB 文件需要使用后端进程能访问的路径。
- 首次接入建议使用只读账号或测试库。
- 凭据只在创建或更新资源时提交，读接口不会回传明文。

## 附录：贡献者本地热更新（非正式态）

仅用于改代码时的热更新，**不是**正式测试或真实生产路径。与正式态二选一，不要混开。

```bash
# 根目录 .env
DATAFOUNDRY_AUTH_MODE=dev

# apps/web/.env.local
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=dev
NEXT_PUBLIC_AGENT_RUNTIME_URL=http://127.0.0.1:8787/api/copilotkit

npm run dev
# 或：npm run dev:api && npm run dev:web
```

## 下一步

- 使用 Web 界面：[Web 工作台指南](guides/web-workbench.md)
- 使用终端界面：[TUI 指南](guides/tui.md)
- 连接自己的数据：[数据源指南](guides/data-sources.md)
- 查看能力边界：[能力全览](capabilities.md)
