# 开发环境

在仓库根目录安装 AgentKit 的锁文件依赖：

```bash
uv sync --project runtime/agentkit --locked
```

Python 要求 `>=3.12,<3.13`。项目用 hatchling 打包 `dataagent` 和 `restapi`。控制台命令是 `dataagent`。

Node 侧只在你要跑 TUI 时需要：

```bash
npm install
npm run start:tui -- --v2 --env-file .env
```

## 调试后端

单独起服务，日志在 `$DATAAGENT_HOME/runtime/logs/backend.log`：

```bash
uv run --project runtime/agentkit dataagent serve \
  --env-file .env \
  --workspace "$PWD"
```

然后看 `http://127.0.0.1:8790/healthz`。

SDK 路径不初始化 Home。调试配置层时显式传入 `LaunchOptions(config=..., env_file=..., init_home=True)`。

断点可以放在：

- `dataagent.bootstrap.startup.prepare_runtime`：配置和路径
- `dataagent.extensions.compiler.compile_extensions`：插件编译
- `restapi.app.create_app` 里的 `stream`：一轮 HTTP
- `dataagent.agent.build_agent`：图装配

`import dataagent` 不会加载 LangChain。要在导入后的属性上断点，先访问 `build_agent` 或 `create_agent`。

## 检查

```bash
uv run --project runtime/agentkit ruff check runtime/agentkit
uv run --project runtime/agentkit pytest runtime/agentkit/tests -q -m 'not live'
```

Ruff 选择 `E4`、`E7`、`E9`、`F`、`I`，行宽 100，目标 `py312`。

改 TUI 时再跑 `npm test --workspace @datafoundry/tui`。输入法候选窗仍要手工看。
