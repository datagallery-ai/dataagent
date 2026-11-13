# 快速开始

这篇文档面向第一次部署的用户。正式态有两条路径，启动后都是同一个控制面。网页和 TUI 是它的两种展示：

| 路径 | 适用环境 | 入口 |
| --- | --- | --- |
| **推荐：一键部署** | Ubuntu / Debian | `./deploy.sh`（配置、依赖、构建含 TUI、detached 后台启动 Web/API 与健康检查一次完成） |
| **手动 npm** | Windows、macOS、其他 Linux，或需要手改环境变量时 | `npm install` → 配置 `.env` → `npm run build` → `npm run start` |

部署完成后把 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY` 写入仓库根目录 `.env`（`npm run start` 会载入进程环境）或 Home `~/.dataagent/.env`。然后打开网页或 TUI 提问。本版本**不提供** Docker / Compose，控制面也没有注册登录。

## 环境要求

- **一键部署**：Ubuntu 或 Debian（x86_64 / aarch64）；Node.js 22（缺失时脚本可在确认后协助安装）和 uv
- **手动 npm**：Linux、macOS 或 Windows；Node.js >= 22、npm 和 uv

请在同一环境内安装和运行项目。Windows 用户不要在 Windows 和 WSL 之间共用 `node_modules`。

## 推荐：Ubuntu / Debian 一键部署

`./deploy.sh` **不支持**原生 Windows / macOS（请改用下文手动 npm）。

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

部署成功后 Web + API 在后台常驻（独立进程组）。关闭终端，或在 `./deploy.sh logs` 中按 `Ctrl+C`，**都不会**停止 DataGallery——停止请用 `./deploy.sh stop`。一键部署会一并构建 TUI，但 TUI **不会**随 stack 后台常驻；需要时另开终端按成功提示启动（见下文「启动 TUI」）。

打开 `http://127.0.0.1:3000/data-tasks`（若网页端口被改过，以脚本打印的地址为准，路径仍是 `/data-tasks`）。模型来自 `config.json` 和环境变量，不在网页里创建 Profile。

### 交互与配置规则

- 首次部署：脚本生成 `.env`，并确认端口 / 公开访问地址。Home 还没有 `config.json` 时，按默认模板写入完整配置，再把所选监听地址写入 `server.host` 和 `server.port`。已有配置只更新这两个字段；文件无法解析时停止写入并保留原文件。部署阶段不要求填写模型 Key。
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

部署时不要求填写 `LLM_*`。重复部署会进入维护窗口：先停止受管进程组，再执行 `npm ci` 与构建。

## Windows / macOS / 其他：手动 npm 部署

`./deploy.sh` **仅面向 Ubuntu / Debian**，不支持原生 Windows / macOS。在 Windows、macOS 或其他发行版上，请按下列步骤用 npm 安装、配置并启动。需要手改环境变量或拆分进程时，也可走这条路径。

正式态对照：

| 环境 | 用途 |
| --- | --- |
| **正式测试** | 本机或内网验收、联调 |
| **真实生产** | 对外服务，前面加反代 |

### 1. 安装依赖

在仓库根目录执行：

```bash
node -v
npm install
```

`node -v` 输出需要不低于 22。本机需要已安装 uv。安装结束会同步控制面的 Python 依赖，耗时取决于机器和网络。

### 2. 配置环境变量

```bash
cp .env.example .env
```

打开根目录 `.env`，填写模型。网页里不能改模型：

```bash
LLM_MODEL=qwen-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=你的_API_Key
```

DeepSeek 示例：

```bash
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的_API_Key
```

对外入口请用反代，样例见 [`deploy/nginx.datagallery.conf.example`](https://github.com/datagallery-lab/datafoundry/blob/main/deploy/nginx.datagallery.conf.example)：静态资源压缩，SSE 路径 `/dataagent/stream` 关闭 gzip 与 `proxy_buffering`。

### 3. 构建并启动（正式测试 / 真实生产相同）

`npm run build` 编译网页和 TUI。`npm run start` 在控制面没有时拉起它，网页开始提供页面，TUI 编译好后另开终端连接：

```bash
npm run build
npm run start
```

另开终端：

```bash
npm run start:tui
```

分开启动时，网页和 TUI 都会在控制面没起时拉起它。地址来自 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`：

```bash
npm run start:api
npm run start:web
```

检查：

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
```

打开 [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks)。控制面没有注册登录。

## 跑通第一个问题

打开 `/data-tasks` 后直接发送问题。模型、插件和工作区使用控制面启动时已经加载的配置。

推荐问题：

```text
用三句话说明你现在能做什么。
```

如果配置了只读工作区，可以改问那个目录里有哪些文件。看到流式回复后，说明网页到控制面的链路已经跑通。数据库类问题只有在相应插件启用之后才有意义。

## 启动 TUI

一键部署会在构建阶段准备好 TUI，但**不会**自动启动，也不会把它当作后台受管进程。直接启动，由 TUI 拉起后端：

```bash
npm run start:tui
```

地址来自 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。控制面已经在这个地址上时，同一条命令直接连上。`--runtime-url` 只连接、不拉起。

部署栈已经在跑时也可以 `./deploy.sh tui`。它连接这次启动记下的 API 地址。

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
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
```

如果健康检查失败：

```bash
./deploy.sh start
# 手动路径：npm run start
```

### 模型不可用

现象：Agent run 报 provider、401、rate limit 或 model not found。

处理：

- 检查 `.env` 中的 `LLM_API_KEY`。
- 检查 `LLM_BASE_URL` 是否以模型服务的兼容接口为准。
- 检查 `LLM_MODEL` 是否在你的账号下可用。
- 改完后重启控制面。网页里没有模型测试接口。

### 端口冲突

默认端口：

| 服务 | 端口 |
| --- | --- |
| Web | 3000 |
| API | 8790（`config.json` 的 `server.port`） |

如果端口被占用，先停止占用进程，或改 `config.json` 的 `server.port`。`npm run start:web` 会读这个端口。

### 模型读不到本机目录

- 控制面没有数据源注册。把目录写进 `dataagent.workspaces`，或启动时传 `--workspace`。
- 目录必须在启动前存在，并且进程可以读取和进入。
- 新会话会把当时的目录列表写入 `sessions.sqlite` 的 `session_bindings`。改列表后要开新会话。

## 下一步

- 使用 Web 界面：[Web 工作台指南](guides/web-workbench.md)
- 使用终端界面：[TUI 指南](guides/tui.md)
- 把本机目录交给模型：[数据从哪里来](guides/data-sources.md)
- 查看能力边界：[能力全览](capabilities.md)
