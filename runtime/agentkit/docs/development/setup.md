# 开发环境

Python 要求 `>=3.12,<3.13`。项目用 hatchling 打包 `dataagent`、`serve` 和 `a2a_server`。控制台命令是 `dataagent`。网页和 TUI 需要 Node.js 22 和 uv。

在仓库根目录：

```bash
npm install
npm run build
```

`npm install` 结束后执行 `uv sync --project runtime/agentkit --frozen --no-dev --python 3.12`。`npm run build` 编译网页和 TUI。只编其中一个时用 `npm run build:web` 或 `npm run build:tui`。名字是 `npm run start`，或 `npm run start:<部分>`。

| 命令 | 作用 |
| --- | --- |
| `npm run start` | 一键。没有 Python 环境就先同步，控制面没有就拉起，网页打开，TUI 编译好可连 |
| `npm run start:api` | 只起控制面 |
| `npm run start:web` | 网页。控制面没有就拉起，已经在跑就只起网页 |

`npm run start` 和 `npm run start:web` 的网页默认是 `127.0.0.1:3000`。不写配置文件。`./deploy.sh deploy`、`start`、`restart` 使用同样的参数，不传就用默认值。要改地址时：

```bash
npm run start -- --web-host 127.0.0.1 --web-port 3000
npm run start:web -- --web-host 127.0.0.1 --web-port 3000
```
| `npm run start:tui` | TUI。控制面没有就拉起，已经在跑就连上 |

网页和 TUI 都读 `server.host` 与 `server.port`，默认 `http://127.0.0.1:8790`。`npm run start` 在 `runtime/agentkit/.venv` 不存在时先同步 Python 依赖，网页或 TUI 还没有构建结果时再补构建。只连接、不拉起后端时：

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

主机必须是 `server.host` 或 `127.0.0.1`。

`--config`、`--env-file`、`--workspace`、`--user`、`--session` 会传给这次拉起的后端，不能和 `--runtime-url` 一起用。

## 调试后端

单独起服务，日志在 `$DATAAGENT_HOME/runtime/logs/backend.log`：

```bash
uv run --project runtime/agentkit dataagent serve \
  --env-file .env \
  --workspace "$PWD"
```

然后看 `http://127.0.0.1:8790/healthz`。端口来自 `config.json` 的 `server.port`。

SDK 路径不初始化 Home。调试配置层时显式传入 `LaunchOptions(config=..., env_file=..., init_home=True)`。

断点可以放在：

- `dataagent.bootstrap.startup.prepare_runtime`：配置和路径
- `dataagent.extensions.compiler.compile_extensions`：插件编译
- `serve.app.create_app` 里的 `stream`：一轮 HTTP
- `dataagent.agent.build_agent`：图装配

`import dataagent` 不会加载 LangChain。要在导入后的属性上断点，先访问 `build_agent` 或 `create_agent`。

## 检查

```bash
uv run --project runtime/agentkit ruff check runtime/agentkit
uv run --project runtime/agentkit pytest runtime/agentkit/tests -q -m 'not live'
```

Ruff 选择 `E4`、`E7`、`E9`、`F`、`I`，行宽 100，目标 `py312`。

改 TUI 时再跑 `npm run test:tui`。输入法候选窗仍要手工看。
