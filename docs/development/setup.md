# 开发环境

Python 要求 `>=3.11,<3.13`。仓库根目录用 hatchling 打包唯一顶层包 `dataagent`，服务端位于 `dataagent.server`。控制台命令是 `dataagent`。网页和 TUI 需要 Node.js ≥22.12.0；源码模式的 Python 准备使用 uv，包部署的前端只连接已启动的后端。

运行依赖位于 `project.dependencies`；pytest 及 ruff 位于 `dependency-groups.dev`，`uv sync` 默认安装开发组，部署时用 `--no-dev` 只准备运行依赖。MySQL、PostgreSQL 驱动属于基础运行依赖；Spark/Hive 和视频功能分别通过 `spark`、`video` extras 按需安装，`all` 组合这些扩展依赖，命令及环境条件见 [包部署](../zh/guides/dataagent-package.md#可选能力依赖)。

在仓库根目录：

```bash
npm install --include=dev
uv sync
npm run build
```

`npm install --include=dev` 准备前端依赖，`uv sync` 准备 Python 环境。`npm run build` 编译网页和 TUI。只编其中一个时用 `npm run build:web` 或 `npm run build:tui`。名字是 `npm run start`，或 `npm run start:<部分>`。

| 命令 | 作用 |
| --- | --- |
| `npm run start` | 启动已有的 Web 构建，控制面未运行时使用已有 Python 环境启动它 |
| `uv run --no-sync dataagent serve` | 使用已有 Python 环境，只启动控制面 |
| `npm run start:web` | 仅启动网页，连接现有控制面 |
| `npm run start:tui` | 启动已构建的 TUI，按配置连接或拉起控制面 |

`npm run start` 和 `npm run start:web` 的网页默认是 `127.0.0.1:3000`。`./deploy.sh deploy`、`start`、`restart` 和 `doctor` 接受同样的地址参数。重启运行中的部署时，未指定的参数沿用上次启动设置。要改地址时：

```bash
npm run start -- --web-host 127.0.0.1 --web-port 3000
npm run start:web -- --web-host 127.0.0.1 --web-port 3000
```

网页和 TUI 都读 `server.host` 与 `server.port`，默认 `http://127.0.0.1:8790`。启动使用已有的环境和构建产物。只连接、不拉起后端时：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。

`--config`、`--env-file`、`--user`、`--session` 会传给这次拉起的后端，不能和 `--runtime-url` 一起用。

## 调试后端

单独起服务，日志在 `$DATAAGENT_HOME/runtime/logs/backend.log`：

```bash
uv run --no-sync dataagent serve \
  --env-file .env \
  --config /path/to/config.yaml
```

然后看 `http://127.0.0.1:8790/healthz`。端口来自 `config.yaml` 的 `server.port`。

SDK 路径不初始化 Home。调试配置层时显式传入 `LaunchOptions(config=..., env_file=..., init_home=True)`。

断点可以放在：

- `dataagent.bootstrap.startup.prepare_runtime`：配置和路径
- `dataagent.extensions.compiler.compile_extensions`：插件编译
- `dataagent.server.app.create_app` 里的 `stream`：一轮 HTTP
- `dataagent.assembly.graph.build_graph`：图装配

`import dataagent` 不会加载 LangChain。要在导入后的属性上断点，先访问 `build_graph` 或 `create_agent`。

## 检查

```bash
uv sync
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

Ruff 选择 `E4`、`E7`、`E9`、`F`、`I`，行宽 100，目标 `py311`。

改 TUI 时再跑 `npm run test:tui`。输入法候选窗仍要手工看。
