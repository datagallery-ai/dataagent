# 快速开始

这篇文档面向第一次部署的用户。正式态有两条路径，启动后都是同一个控制面。网页和 TUI 是它的两种展示：

| 路径 | 适用环境 | 入口 |
| --- | --- | --- |
| **推荐：一键部署** | Linux（包括 openEuler） | `./deploy.sh`（配置、依赖、构建含 TUI、detached 后台启动 Web/API 与健康检查一次完成） |
| **手动 npm** | Windows、macOS，或需要手改环境变量时 | 准备工具和下载源 → `npm install` → 模型配置 → `npm run build` → `npm run start` |

部署完成后把 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY` 写入仓库根目录 `.env`（`npm run start` 会载入进程环境）或 Home `~/.dataagent/.env`。然后打开网页或 TUI 提问。本版本**不提供** Docker / Compose，控制面也没有注册登录。

## 环境要求

- **一键部署**：Linux；Node.js ≥22.12.0、npm 和 uv。Node.js 缺失或版本过低时，脚本可在确认后通过 `apt-get`、`dnf` 或 `yum` 安装
- **手动 npm**：Linux、macOS 或 Windows；Node.js >= 22.12.0、npm 和 uv

后端使用 Python 3.12。受限网络环境先准备可用解释器和下载源，再执行安装命令；具体步骤见下文手动部署和 [自定义镜像部署](guides/mirror-deployment.md)。

最低版本由当前锁定的 TUI 与构建/测试依赖共同决定。

请在同一环境内安装和运行项目。Windows 用户不要在 Windows 和 WSL 之间共用 `node_modules`。

## 推荐：Linux 一键部署

`./deploy.sh` 不按发行版或 CPU 架构提前拒绝运行。需要安装 Node.js 时默认使用 NodeSource，也可用 `DATAAGENT_NODE_PACKAGE_URL` 指定自定义安装包：`apt-get` 走 DEB 安装，`dnf` / `yum` 走 RPM 安装；找不到可用包管理器时提示手动安装。依赖不兼容、权限不足或安装失败时直接报错。镜像和解释器配置见 [自定义镜像部署](guides/mirror-deployment.md)。Windows / macOS 推荐使用下文手动 npm。

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

部署成功后 Web + API 在后台常驻（独立进程组）。关闭终端，或在 `./deploy.sh logs` 中按 `Ctrl+C`，**都不会**停止 DataGallery——停止请用 `./deploy.sh stop`。一键部署会一并构建 TUI，但 TUI **不会**随 stack 后台常驻；需要时另开终端按成功提示启动（见下文「启动 TUI」）。

打开 `http://127.0.0.1:3000/data-tasks`（若网页端口被改过，以脚本打印的地址为准，路径仍是 `/data-tasks`）。模型来自 `config.json` 和环境变量，不在网页里创建 Profile。

### 交互与配置规则

- 首次部署：脚本生成 `.env`，并确认端口 / 客户端可达地址。Home 还没有 `config.json` 时，按默认模板写入完整配置，再把所选监听地址写入 `server.host` 和 `server.port`。已有配置只更新这两个字段；文件无法解析时停止写入并保留原文件。部署阶段不要求填写模型 Key。
- 之后再执行交互式 `./deploy.sh` / `./deploy.sh deploy`：若已有完整 `.env`，会跳过配置问答。
- 需要重新配置端口或客户端可达地址时（保留现有密钥，并先备份 `.env`）：

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

`deploy` 负责依赖准备和构建。`start` / `restart` 只使用已有的 Python 环境和构建产物，缺少依赖时提示重新执行 `deploy`；托管启动不会补装依赖或自动构建。手动 `npm start` 仍保留按需准备环境和构建产物的便利行为。

部署时不要求填写 `LLM_*`。重复部署会进入维护窗口：先停止受管进程组，再执行 `npm ci` 与构建。

### 日志位置

部署、单独启动 Web/API、启动 TUI 都使用同一个 Home 日志目录：`<Home>/runtime/logs/`。Home 由 `DATAAGENT_HOME` 指定，默认 `~/.dataagent`。

- `backend.log`：后端、插件、公共环境准备和整体启停。
- `web.log`：Web 构建、服务端及浏览器上报的错误。
- `tui.log`：TUI 构建、启动、连接和交互。

`./deploy.sh logs` 同时跟随三个文件。日志写入前脱敏，保留完整诊断；每个文件达到 5,000,000 字节后轮转，保留 3 个备份。浏览器错误上报依赖 Web 服务可达，不保证离线保存。

通过反向代理、内网域名或通配监听地址访问 Web 时，在服务端环境中设置 `DATAAGENT_WEB_ORIGIN` 为实际访问的 Origin（例如 `https://data.example.com`），用于校验浏览器错误上报来源。上报额度为每个 Web 进程每分钟 60 条、1 MiB 正文，所有来源共享。

## Windows / macOS / 其他：手动 npm 部署

Windows、macOS，或希望手动准备依赖的 Linux 环境，可按下列步骤用 npm 安装、配置并启动。需要手改环境变量或拆分进程时，也可走这条路径。

正式态对照：

| 环境 | 用途 |
| --- | --- |
| **正式测试** | 本机或内网环境验收、联调 |
| **生产运行** | 在部署环境中持续提供服务；可使用内网域名，并按环境需要配置反向代理 |

<a id="manual-prerequisites"></a>

### 1. 准备工具和下载源

先从可达的下载源获取匹配系统和架构的 Node.js ≥22.12.0 及 uv，安装后将可执行文件目录加入 `PATH`。已有工具时，在仓库根目录检查：

```bash
node --version
npm --version
uv --version
```

受限网络环境先预装 Python 3.12，指定解释器，并关闭自动下载以便缺失时直接报错。以下路径需替换为本机实际路径，命令在执行 `npm install` 的同一终端运行：

```bash
export UV_PYTHON=/opt/python3.12/bin/python3.12
export UV_PYTHON_DOWNLOADS=never
uv python find "$UV_PYTHON" --no-python-downloads --show-version
```

需要使用镜像时，在安装前配置下列进程环境变量；示例地址需替换为实际下载源：

```bash
export npm_config_registry="https://mirror.example/npm/"
export UV_DEFAULT_INDEX="https://mirror.example/pypi/simple/"
npm config get registry
```

PowerShell 使用 `$env:NAME="value"` 设置对应变量，解释器路径例如 `C:\Python312\python.exe`；检查命令中的 `$UV_PYTHON` 对应 `$env:UV_PYTHON`。如果需要下载 Python，可按 [镜像指南](guides/mirror-deployment.md) 配置 `UV_PYTHON_INSTALL_MIRROR` 并允许下载。

`npm_config_registry` 控制前端依赖来源；`UV_DEFAULT_INDEX` 控制 Python 运行依赖和构建依赖的索引。设置这些进程变量即可，不要求修改全局源配置。npm 下载源需要进程变量或 npm 原生配置；Python 准备也会读取根 `.env` 的默认值，同名进程变量优先。

安装钩子直接调用原生 `uv sync`，根据 `pyproject.toml` 的版本约束从配置的索引解析并安装依赖。仓库保留 `uv.lock` 作为解析快照，以减少首次解析可选依赖的下载成本；安装不使用 `--frozen` 或 `--locked`，配置或索引变化时由 uv 更新快照。无需执行 `sed` 或手动改写下载地址，不要求不同环境的全部传递依赖版本一致。

### 2. 安装依赖

```bash
npm install
```

安装尾部的 `postinstall` 会自动同步控制面的 Python 依赖到 `runtime/agentkit/.venv`。需要拆分前端安装、Python 准备和构建时，见 [分阶段安装与原生命令](guides/mirror-deployment.md#staged-installation)。确认整个命令成功退出、Python 同步完成后再继续；耗时取决于机器和下载源。

### 3. 配置模型服务

```bash
cp .env.example .env
```

打开根目录 `.env`，填写部署机器可达的兼容模型服务。以下为配置格式示例，地址、模型名和密钥都需按实际服务替换；网页里不能改模型：

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

模型服务地址属于运行配置，与 npm / Python 下载源分开设置。服务可以在未配置模型时启动并提供页面；对话前需补齐模型配置，修改后重启服务。

### 4. 构建并启动（正式测试 / 真实生产相同）

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

<a id="manual-verification"></a>

### 5. 验证健康与模型调用

在另一个终端检查，端口按实际监听配置调整：

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz` 应返回 200，表示控制面存活。`/ready` 返回 200 表示会话引擎配置加载成功；未配置好模型时返回 503，应检查模型配置和后端日志。网页返回成功状态只表示页面可达，实际模型调用还需按下一节发送问题并确认收到回复。

打开 [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks)。控制面没有注册登录。

## 跑通第一个问题

打开 `/data-tasks` 后直接发送问题。模型、插件和工作区使用控制面启动时已经加载的配置。

推荐问题：

```text
用三句话说明你现在能做什么。
```

如果配置了只读工作区，可以改问那个目录里有哪些文件。看到流式回复后，说明网页到控制面的链路已经跑通。数据库类问题只有在相应插件启用之后才有意义。

## 启动 TUI

TUI 自行启动后端时，会与 `npm start` 一样加载仓库根 `.env`。终端已有的同名变量优先；根 `.env` 中载入的非空模型变量，优先于后端 Home `.env` 中的同名值。因此两处配置不同的 `LLM_MODEL`、`LLM_BASE_URL` 时，实际模型可能变化。使用 `--runtime-url` 连接已有后端不会重新加载其配置。

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

升级到 Node.js 22.12.0 或更高版本后重试。一键部署也可让 `./deploy.sh` 在确认后协助安装；手动路径则重新执行 `npm install`。

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
