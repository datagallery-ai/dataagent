# 源码部署

本页介绍两种部署方式，源码从 [DataAgent 的 GitCode 仓库](https://gitcode.com/datagallery/dataagent)获取。Server wheel 和 TUI npm 包的构建与安装见[独立包部署](deploy-packages.md#get-artifacts)。

| 路径 | 适用环境 | 入口 |
| --- | --- | --- |
| **推荐：一键部署** | Linux（包括 openEuler） | `./deploy.sh`（配置、依赖、构建含 TUI、detached 后台启动 Web/API 与健康检查一次完成） |
| **手动 npm** | Windows、macOS，或需要手改环境变量时 | 准备工具和下载源 → `npm install` + `uv sync` → 模型配置 → `npm run build` → `npm run start` |

部署完成后把 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY` 写入仓库根目录 `.env`（`npm run start` 会载入进程环境）或 Home `~/.dataagent/.env`。然后打开网页或 TUI 提问。本版本**不提供** Docker / Compose，控制面也没有注册登录。

## 环境要求

- **一键部署**：Linux；Node.js ≥22.12.0、npm 和 uv。Node.js 缺失或版本过低时，脚本可在确认后通过 `apt-get`、`dnf` 或 `yum` 安装
- **手动 npm**：Linux、macOS 或 Windows；Node.js >= 22.12.0、npm 和 uv

后端支持 Python 3.11 和 3.12。受限网络环境先准备可用解释器和下载源，再执行安装命令；具体步骤见下文手动部署和 [自定义镜像部署](deploy-mirrors.md)。

最低版本由当前锁定的 TUI 与构建/测试依赖共同决定。

请在同一环境内安装和运行项目。Windows 用户不要在 Windows 和 WSL 之间共用 `node_modules`。

## 推荐：Linux 一键部署

`./deploy.sh` 不按发行版或 CPU 架构提前拒绝运行。需要安装 Node.js 时默认使用 NodeSource，也可用 `DATAAGENT_NODE_PACKAGE_URL` 指定自定义安装包：`apt-get` 走 DEB 安装，`dnf` / `yum` 走 RPM 安装；找不到可用包管理器时提示手动安装。依赖不兼容、权限不足或安装失败时直接报错。镜像和解释器配置见 [自定义镜像部署](deploy-mirrors.md)。Windows / macOS 推荐使用下文手动 npm。

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
./deploy.sh
```

部署成功后 Web + API 在后台常驻（独立进程组）。关闭终端，或在 `./deploy.sh logs` 中按 `Ctrl+C`，**都不会**停止 DataGallery——停止请用 `./deploy.sh stop`。一键部署会一并构建 TUI，但 TUI **不会**随 stack 后台常驻；需要时另开终端按成功提示启动（见下文「启动 TUI」）。

打开 `http://127.0.0.1:3000/data-tasks`（若网页端口被改过，以脚本打印的地址为准，路径仍是 `/data-tasks`）。模型来自 `config.yaml` 和环境变量，不在网页里创建 Profile。

### 交互与配置规则

- 首次部署：脚本生成 `.env`，并确认端口 / 客户端可达地址。Home 还没有 `config.yaml` 时，按默认模板写入完整配置，再把所选监听地址写入 `server.host` 和 `server.port`。已有配置只更新这两个字段；文件无法解析时停止写入并保留原文件。部署阶段不要求填写模型 Key。
- 再次执行交互式 `./deploy.sh` / `./deploy.sh deploy` 时，若已有受管服务运行或根 `.env` 非空，会跳过配置问答。
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
./deploy.sh restart   # 校验后沿用上次的启动配置重启
./deploy.sh logs      # 跟随运行日志；Ctrl+C 不停止服务
./deploy.sh doctor    # 只读检查依赖 / 配置 / 端口 / 磁盘 / 健康
./deploy.sh tui       # 可选：前台启动 TUI（需 API 已健康；不是受管后台服务）
./deploy.sh help
```

`deploy` 负责依赖准备和构建。`start` / `restart` 只使用已有的 Python 环境和构建产物，缺少依赖时提示重新执行 `deploy`；托管启动不会补装依赖或自动构建。手动 `npm start` 同样使用已有环境和 Web 构建产物。

`deploy`、`restart` 和 `doctor` 优先使用显式传入的 `--config`、`--web-host` 和 `--web-port`，其次沿用正在运行的受管服务的配置文件和 Web 地址，最后使用 Home 配置和默认 Web 地址。显式指定的 Web 端口会直接校验，占用时报告冲突。交互部署只询问未显式指定的端口。

原端口切换到重叠的监听地址时，Linux 通过 `ss` 确认占用来自当前受管服务。出现其他占用或无法确认归属时，当前服务继续运行，命令报告端口冲突。

`deploy` 自动检查依赖、部署目录权限、配置和最终监听端口。需要检查镜像连通性、磁盘或当前服务状态时，运行 `./deploy.sh doctor`。

`restart` 在停止旧服务前用 Server 的配置加载逻辑校验新配置。`doctor` 在已有 Python 环境时执行同样的校验，否则仅检查监听配置并提示跳过了 Server 配置校验。

部署时不要求填写 `LLM_*`。重复部署会进入维护窗口：先停止受管进程组，再执行 `npm install` 与构建。

### 日志位置

部署、单独启动 Web/API、启动 TUI 都使用同一个 Home 日志目录：`<Home>/runtime/logs/`。Home 由 `DATAAGENT_HOME` 指定，默认 `~/.dataagent`。

- `backend.log`：后端、插件、公共环境准备和整体启停。
- `web.log`：Web 构建、服务端及浏览器上报的错误。
- `tui.log`：TUI 构建、启动、连接和交互。

`./deploy.sh logs` 同时跟随三个文件。日志写入前脱敏，保留完整诊断；每个文件达到 5,000,000 字节后轮转，保留 3 个备份。浏览器错误上报依赖 Web 服务可达，不保证离线保存。

通过反向代理、域名或通配监听地址访问 Web 时，在服务端环境中设置 `DATAAGENT_WEB_ORIGIN` 为实际访问的 Origin（例如 `https://data.example.com`），用于校验浏览器错误上报来源。上报额度为每个 Web 进程每分钟 60 条、1 MiB 正文，所有来源共享。

## Windows / macOS / 其他：手动 npm 部署

Windows、macOS，或希望手动准备依赖的 Linux 环境，可按下列步骤用 npm 安装、配置并启动。需要手改环境变量或拆分进程时，也可走这条路径。

正式态对照：

| 环境 | 用途 |
| --- | --- |
| **正式测试** | 本机或部署环境验收、联调 |
| **生产运行** | 在部署环境中持续提供服务；可使用域名，并按环境需要配置反向代理 |

<a id="manual-prerequisites"></a>

### 1. 准备工具和下载源

先从可达的下载源获取匹配系统和架构的 Node.js ≥22.12.0 及 uv，安装后将可执行文件目录加入 `PATH`。已有工具时，在仓库根目录检查：

```bash
node --version
npm --version
uv --version
```

受限网络环境先预装 Python 3.11 或 3.12，指定解释器，并关闭自动下载以便缺失时直接报错。以下路径需替换为本机实际路径，命令在执行 `uv sync` 的同一终端运行：

```bash
export UV_PYTHON=/opt/python3.11/bin/python3.11
export UV_PYTHON_DOWNLOADS=never
uv python find "$UV_PYTHON" --no-python-downloads --show-version
```

需要使用镜像时，在安装前配置下列进程环境变量；示例地址需替换为实际下载源：

```bash
export npm_config_registry="https://mirror.example/npm/"
export UV_DEFAULT_INDEX="https://mirror.example/pypi/simple/"
npm config get registry
```

PowerShell 使用 `$env:NAME="value"` 设置对应变量，解释器路径例如 `C:\Python311\python.exe`；检查命令中的 `$UV_PYTHON` 对应 `$env:UV_PYTHON`。如果需要下载 Python，可按 [镜像指南](deploy-mirrors.md) 配置 `UV_PYTHON_INSTALL_MIRROR` 并允许下载。

`npm_config_registry` 控制前端依赖来源；`UV_DEFAULT_INDEX` 控制 Python 运行依赖和构建依赖的索引。设置这些进程变量即可，不要求修改全局源配置。手动安装时请通过进程环境或 npm、uv 的原生配置指定下载源。

Python 依赖通过原生 `uv sync` 安装，根据 `pyproject.toml` 的版本约束从配置的索引解析并安装依赖。仓库保留 `uv.lock` 作为解析快照，以减少首次解析可选依赖的下载成本；安装不使用 `--frozen` 或 `--locked`，配置或索引变化时由 uv 更新快照。无需执行 `sed` 或手动改写下载地址，不要求不同环境的全部传递依赖版本一致。

### 2. 安装依赖

```bash
npm install --include=dev
uv sync --no-dev --extra video
```

第一条命令安装 Web 和 TUI 依赖。第二条命令将运行依赖和视频依赖同步到 `.venv`。需要开发工具时执行 `uv sync`，其他可选能力按需选择。详见[分阶段安装](deploy-mirrors.md#staged-installation)。

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

根目录 `.env.example` 标明了 Server 和 TUI 的手动运行配置。默认模型模板使用上面三项，其余按需填写或从启动终端导出。安装镜像在安装前按[工具准备步骤](#manual-prerequisites)从终端设置。服务可以在未配置模型时启动并提供页面；对话前需补齐模型配置，修改后重启服务。

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

分开启动时，在两个终端先启动控制面，再启动网页。网页默认连接 `config.yaml` 中的地址（默认 `http://127.0.0.1:8790`），连接独立部署的后端时可先设置 `API_PROXY_TARGET`。

```bash
uv run --no-sync dataagent serve
npm run start:web
```

如果模型变量写在仓库根 `.env`，启动控制面时使用 `uv run --no-sync --env-file .env dataagent serve`。

<a id="manual-verification"></a>

### 5. 验证健康与模型调用

在另一个终端检查，端口按实际监听配置调整：

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz` 应返回 200，表示控制面存活。`/ready` 返回 200 表示会话引擎配置加载成功；未配置好模型时返回 503，应检查模型配置和后端日志。网页返回成功状态只表示页面可达，实际模型调用还需按[跑通第一个问题](../quick-start.md#first-question)发送问题并确认收到回复。

打开 [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks)。控制面没有注册登录。

<a id="connect-source-frontends"></a>

## 源码前端连接已有 Server

在前端源码目录准备 npm 依赖，并按需启动 Web 或 TUI。这里的 Server 可以来自源码部署，也可以是已经启动的 wheel 服务：

```bash
npm install --include=dev
```

启动 Web：

```bash
npm run build:web
API_PROXY_TARGET=http://127.0.0.1:8790 npm run start:web
```

只需要源码版 TUI 时，构建 TUI 并显式指定 Server 地址：

```bash
npm run build:tui
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

将地址换成从前端所在机器可访问的 Server 地址。

## 源码 Server 使用编译型插件入口

在 YAML 中启用插件并设置 `dataagent.entry`。内置 nl2sql 可用仓库的 `examples/nl2sql-entry.yaml`，先按示例准备模型、语义服务和 SQLite 文件，然后在仓库根目录运行：

```bash
uv run --no-sync dataagent serve --config examples/nl2sql-entry.yaml
```

另开终端，用上一节的显式地址连接。插件声明里的 `entrypoint` 写法和完整配置见 [使用编译型插件入口](deploy-packages.md#compiled-entry)。

## 启动 TUI

启动 TUI、连接 Server 和恢复会话的步骤见 [TUI 指南](tui.md)。

<a name="deployment-troubleshooting"></a>

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
| API | 8790（`config.yaml` 的 `server.port`） |

如果端口被占用，先停止占用进程，或改 `config.yaml` 的 `server.port`。`npm run start:web` 会读这个端口。

### 模型读不到本机目录

- 控制面没有数据源注册。把目录写进 `dataagent.input_workspaces`。
- 目录必须在启动前存在，并且进程可以读取和进入。
- 新会话会把当时的目录列表写入 `sessions.sqlite` 的 `session_bindings`。改列表后要开新会话。

## 下一步

- 使用 Web 界面：[Web 工作台指南](web.md)
- 使用终端界面：[TUI 指南](tui.md)
- 把本机目录交给模型：[数据从哪里来](inputs.md)
- 查看能力边界：[能力全览](../concepts/capabilities.md)
