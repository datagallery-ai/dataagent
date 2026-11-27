# 开发环境

Python 支持 3.11 和 3.12，使用 uv 管理依赖。构建或调试 Web 和 TUI 还需要 Node.js ≥22.12.0 和 npm。环境版本与依赖组见 [pyproject.toml](../../pyproject.toml) 和 [package.json](../../package.json)。

<a name="prepare-source"></a>

## 准备源码环境

在仓库根目录运行：

```bash
uv sync --python 3.12
npm install --include=dev
```

`uv sync` 默认安装 pytest 和 Ruff 等开发依赖。只修改 Python 时可先跳过 npm 步骤。运行依赖与可选 extra 见[独立包部署](../zh/guides/deploy-packages.md#optional-dependencies)。调用模型前按[配置速查](../zh/reference/configuration-api.md)准备配置。

| 要调试的入口 | 从源码运行 |
| --- | --- |
| 仅后端 | `uv run --no-sync dataagent serve` |
| Web 与本机后端 | `npm run build:web`，再用 `npm run start` |
| Web 连接已有后端 | `npm run build:web`，再用 `npm run start:web`，连接参数见[源码部署](../zh/guides/deploy-source.md#connect-source-frontends) |
| 终端客户端 | `npm run build:tui`，再按 [TUI 指南](../zh/guides/tui.md)启动或连接 |

完整发行制品由[发布流程](release.md)构建。

<a name="debug-backend"></a>

## 调试后端

用独立 Home 调试配置、会话和日志。启动前可在进程环境指定 `DATAAGENT_HOME`，也可显式选择 YAML 和环境文件：

```bash
uv run --no-sync dataagent serve --config /path/to/config.yaml --env-file /path/to/env-file
```

先检查 [`/healthz` 和 `/ready`](../zh/reference/rest-api.md#health-and-identity)，再用请求和运行 ID 查看[日志与 Trace](architecture/observability.md)。SDK 调试配置层时可用 `LaunchOptions(config=..., env_file=..., init_home=True)` 初始化缺少的模板。

| 断点 | 用途 |
| --- | --- |
| `dataagent.bootstrap.startup.prepare_runtime` | 配置和路径准备 |
| `dataagent.extensions.compiler.compile_extensions` | 扩展声明编译 |
| `dataagent.server.app.create_app` 中的 `stream` | HTTP 对话入口 |
| `dataagent.assembly.graph.build_graph` | 原生图装配 |

改动后按[测试指南](testing.md)选择验证。

<a name="preview-docs"></a>

## 本地预览文档

在仓库根目录用独立 Python 环境准备 MkDocs：

```bash
python -m venv .venv-docs
source .venv-docs/bin/activate
python -m pip install -r requirements-docs.txt
npm run docs:dev
```

打开 `http://127.0.0.1:8000` 预览，交付前按[测试指南](testing.md)执行文档构建和链接检查。
