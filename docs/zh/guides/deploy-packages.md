# 独立包部署

把 Server wheel 放到运行服务的机器，把 TUI 包放到使用终端的机器。两者可以在同一台机器，也可以分开部署。

<a name="get-artifacts"></a>

## 获取制品与确认版本

当前 [GitCode 公开发布](https://gitcode.com/datagallery/dataagent/releases)提供 `v0.1.0` 的源码归档。本页对应 `0.2.0`，可按下文从 [GitCode 上的 DataAgent `main`](https://gitcode.com/datagallery/dataagent) 构建 Server wheel 和 TUI 包。[PyPI 上的 `0.1.0`](https://pypi.org/project/datagallery-dataagent/0.1.0/)使用旧版运行时和配置。使用该版本时，请阅读 [`v0.1.0` 源码中的文档](https://gitcode.com/datagallery/dataagent/tree/v0.1.0)。切换到现行运行时时，可参照[旧版迁移对照](../../development/archive/main-legacy-migration.md)。

首次安装时可从源码构建制品。在装有 Git、Node.js ≥22.12.0、npm、uv 和 Make 的 Linux 环境运行：

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
git rev-parse HEAD
make prepare PYTHON_VERSION=3.12
make build PYTHON_VERSION=3.12
sha256sum dist/server/*.whl dist/tui/*.tgz
```

需要复现某次构建时，先检出该制品记录的源码提交，再执行构建命令。构建后把 Git 提交号、制品文件名和 SHA-256 一同记录。收到预构建制品时，安装前核对提供方记录的构建提交，并用提供方的 SHA-256 校验文件。完整步骤见[构建与制品验收](../../development/release.md#verify-artifacts)。

| 交付文件 | 用途 | 环境 |
| --- | --- | --- |
| `datagallery_dataagent-0.2.0-py3-none-any.whl` | 安装 Python Server、CLI、SDK 和内置插件 | Python 3.11 或 3.12，uv 或 pip |
| `datagallery-tui-0.2.0.tgz` | 安装终端客户端，连接已经运行的 Server | Node.js ≥22.12.0 和 npm |

用实际交付的文件名替换下方示例。Web 使用源码构建的前端。安装会解析包里声明的依赖，需要可用索引或预先准备的离线依赖。需要构建产物时看[发布说明](../../development/release.md)。需要 Web 时，按[源码前端连接已有 Server](deploy-source.md#connect-source-frontends)准备。

## 1. 安装并启动 Server

在 wheel 所在目录创建虚拟环境并安装：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python ./datagallery_dataagent-0.2.0-py3-none-any.whl
```

用 pip 时，先执行 `python3.11 -m venv .venv`，再执行 `.venv/bin/python -m pip install ./datagallery_dataagent-0.2.0-py3-none-any.whl`。

安装后的 Server 命令入口是 `dataagent serve`。使用默认模板时，在启动终端设置这三项，或填写 Home 的 `.env`：

```bash
export LLM_MODEL=your-model-name
export LLM_BASE_URL=https://your-openai-compatible-host/v1
export LLM_API_KEY=replace-with-your-key
.venv/bin/dataagent serve
```

Windows PowerShell 用 `$env:NAME="value"` 设置环境变量。安装和启动时分别使用 `.\.venv\Scripts\python.exe` 与 `.\.venv\Scripts\dataagent.exe`。

Server 前台运行，按 Ctrl+C 停止。首次启动在 `~/.dataagent` 创建缺少的配置模板，可在启动前通过 `DATAAGENT_HOME` 指定其他 Home。默认读取 Home 的 `config.yaml` 和 `.env`，也可用 `--config` 选择另一份完整 YAML，用 `--env-file` 选择另一份环境文件。显式选定的 YAML 和环境文件分别替代对应的 Home 文件。包部署目录下的普通 `.env` 需要通过 `--env-file` 显式指定。

默认监听 `127.0.0.1:8790`，修改 YAML 中的 `server.host`、`server.port` 后重启。跨机器连接时，把 `server.host` 设为 Server 上可达的监听地址，客户端使用 Server 的可达地址。对外监听前请核对[网络与权限边界](../concepts/security.md)。输入目录和启动参数见[配置速查](../reference/configuration-api.md)和 [CLI 参考](../reference/cli-and-environment.md)。

另开终端检查：

```bash
curl -fsS http://127.0.0.1:8790/healthz
curl -fsS http://127.0.0.1:8790/ready
```

`/healthz` 返回 200 表示进程存活。`/ready` 返回 200 表示会话引擎已加载配置。模型未配置时，`/ready` 返回 503。随后从 TUI 提问，确认模型能返回流式回复。Server 日志位于其 Home 的 `runtime/logs/backend.log`。

<a id="connect-tui"></a>

## 2. 安装并启动 TUI

在 TUI 包所在目录安装，然后使用 npm 提供的命令入口：

```bash
npm install -g ./datagallery-tui-0.2.0.tgz
dataagent-cli --runtime-url http://127.0.0.1:8790
```

`da` 是同一入口的简写。安装后两个命令都从终端运行。若客户端和 Server 分开部署，将示例中的 `127.0.0.1:8790` 换成客户端能访问的 Server 地址。启动参数 `--resume` 或 `--resume <threadId>` 可恢复已有会话。退出 TUI 后 Server 继续运行，连接诊断记录在客户端 Home 的 `runtime/logs/tui.log`。

<a id="compiled-entry"></a>

## 3. 使用编译型插件入口（可选）

常规启动使用默认 Agent。要让某个编译型子代理接管会话，在 Server YAML 中设置 `dataagent.entry`，填入已启用插件的子代理 `name`。例如，内置 nl2sql 的子代理名是 `nl2sql`，它自己的声明将 `entrypoint` 指向包内的 `subagents/nl2sql.py:build_subagent`。部署者选择 `entry: nl2sql` 即可。

以下是 SQLite 场景的完整 `nl2sql.yaml` 示例。默认模型环境变量沿用上文，另需设置 `NL2SQL_DATABASE_PATH` 为已有 SQLite 文件的绝对路径，设置 `SEMANTIC_BASE_URL` 为可达的语义服务地址。

```yaml
schema_version: 4
dataagent:
  model: primary
  entry: nl2sql
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
plugins:
  enabled: [nl2sql]
  config:
    nl2sql:
      model: primary
      database:
        db_id: mydb
        dialect: sqlite
        engine: sqlite
        path: $env{NL2SQL_DATABASE_PATH}
semantic:
  base_url: $env{SEMANTIC_BASE_URL}
```

```bash
.venv/bin/dataagent serve --config ./nl2sql.yaml
```

使用相同的 TUI 命令连接。修改 `dataagent.entry` 后重启 Server，并开启新会话，避免沿用不同入口图的会话状态。健康检查通过后，仍需实际发起一次该入口处理的请求。

适配自有插件时，将插件放到 Server Home 的 `plugins/` 下。在插件的子代理声明里用 `kind: compiled`，并将 `entrypoint` 写成相对插件目录的 `file.py:symbol`。该符号应实现 `factory(params, services)` 并返回 Runnable。把插件 ID 加到 `plugins.enabled`，再把子代理的 `name` 写到 `dataagent.entry`。[编译型子代理和入口模式](integrate/extensions/subagents.md#entry-mode)

<a name="optional-dependencies"></a>

## 可选依赖

SQLite、MySQL 和 PostgreSQL 驱动随 wheel 安装。按需在 wheel 文件名后追加 `[video]`、`[spark]` 或 `[all]`，例如：

```bash
uv pip install --python .venv/bin/python "./datagallery_dataagent-0.2.0-py3-none-any.whl[video]"
```

`video` 安装视频处理依赖，`spark` 安装 PySpark。Spark/Hive 还需 Java 17 或更高版本以及对应服务配置。`all` 安装这两个 extra。插件和数据库连接仍需在 Server YAML 中配置。
