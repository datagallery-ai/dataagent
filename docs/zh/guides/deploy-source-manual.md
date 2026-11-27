# 手动源码部署

适用于 Windows、macOS，或需要分别管理依赖和进程的 Linux 部署。以下命令从仓库根目录运行。需要托管启停时，见 [Linux 一键部署](deploy-source-linux.md)。

<a name="manual-prerequisites"></a>

## 1. 准备工具和下载源

安装 Git、Node.js 22.12.0 或更高版本、npm 和 uv，后端需要 Python 3.11 或 3.12。选择与系统和架构匹配的制品，并将可执行文件加入 `PATH`。

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
node --version
npm --version
uv --version
```

安装与运行使用同一环境。Windows 和 WSL 各自安装依赖。若需预装 Python 或使用自定义 npm / Python 索引，先按[镜像配置](deploy-mirrors.md)设置进程变量。在 PowerShell 中用 `$env:NAME = "value"` 设置变量，解释器路径可写成 `C:\Python311\python.exe`。

在 WSL 2 中安装前，查看[WSL 2 资源建议](deploy-source.md#wsl2-sizing)。

<a name="manual-dependencies"></a>

## 2. 安装依赖

```bash
npm install --include=dev
uv sync --no-dev
```

npm 安装 Web 与 TUI 的构建依赖，uv 准备 Python 运行环境。仅运行 Server 时可跳过 npm 安装和前端构建。需要开发工具时执行 `uv sync`。用到视频或其他可选能力时，在同步命令加上对应的 `--extra <name>`。现有环境、切换镜像与可选依赖见[分阶段安装](deploy-mirrors.md#staged-installation)。

<a name="manual-model"></a>

## 3. 配置模型服务

将 `.env.example` 复制为根目录的 `.env`，已有文件时直接编辑。默认模型读取以下三项，替换为 Server 可访问的实际服务信息。

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

根目录的 `.env` 由 npm 启动器读入进程环境。可以改用 Home 的 `.env` 或启动终端环境。修改模型后重启 Server。文件选择与覆盖规则见[配置速查](configuration.md#files-to-edit)。

<a name="manual-start"></a>

## 4. 构建并启动

```bash
npm run build
npm run start
```

`npm run build` 构建 Web 和 TUI。`npm run start` 启动 Web，在选定的地址没有运行 Server 时也会拉起 Server。使用终端时在另一个终端执行 `npm run start:tui`。

只运行 Server 时，用同一个 Python 环境启动。下面的 uv 参数将根目录 `.env` 读入进程环境，改用 Home `.env` 或已导出的变量时可省略 `--env-file .env`。

```bash
uv run --no-sync --env-file .env dataagent serve
```

分开运行时，见 [Web 连接已有 Server](web.md#connect-server)和 [TUI 连接已有 Server](tui.md#connect-server)。使用标准启动命令可以验收生产构建，也可持续运行服务。需要浏览器经域名访问时，另行配置反向代理并查看 [Web Origin](web.md#web-origin)。

<a name="manual-verification"></a>

## 5. 验证健康与模型调用

在另一个终端检查，将地址改为实际监听配置。只运行 Server 时，仅检查前两项。如果 PowerShell 将 `curl` 解析为别名，改用 `curl.exe`。

```bash
curl -i http://127.0.0.1:8790/healthz
curl -i http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz` 为 200 表示 Server 进程能响应。`/ready` 为 200 表示对话引擎就绪，返回 503 时查看[启动诊断](troubleshooting.md#startup)。页面可达后按[第一次提问](../quick-start.md#first-question)验证实际模型回复。

如需让已启用的插件接管整张图，见[编译型子代理的入口模式](integrate/extensions/subagents.md#entry-mode)。出现其他部署问题时，见[故障排查](troubleshooting.md)。
