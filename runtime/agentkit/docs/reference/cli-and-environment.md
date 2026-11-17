# 命令与环境变量

## `dataagent serve`

入口是 `serve.__main__:main`。`python -m serve` 调用同一实现。这是 Web 和当前 TUI 共用的后端。

```bash
uv run --project runtime/agentkit dataagent serve \
  --config path/to/config.json \
  --user default \
  --workspace /existing/data \
  --workspace /other/data \
  --session <threadId> \
  --env-file /path/.env
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--config` | 无 | 用这份文件代替 Home `config.json`。文件必须存在 |
| `--user` | `default` | 本地用户 ID。决定 `runtime/users/<user>/` |
| `--workspace` | 无 | 可重复。已存在的只读输入。传了就不用 `DATAAGENT_WORKSPACE` 和配置里的 `workspaces` |
| `--session` | 无 | 采用该会话创建时记录的输入绑定，作为进程默认值。它不恢复对话；客户端用同一 `threadId` 续接对话 |
| `--env-file` | 无 | 只用这份环境文件，代替 Home `.env`。文件必须存在 |

没有 `--host`。`npm run start`、网页转发和无参数 TUI 在启动时读所选配置的 `server.host` 与 `server.port`：传了 `--config` 用那份文件，否则用 Home `config.json`。默认 `127.0.0.1:8790`。TUI 无参数时，这个地址上没有控制面就拉起，已经在跑就连上。`npm run start -- --config <path>` 和 `./deploy.sh start --config <path>` 把同一路径交给 `dataagent serve`。`npm run start` 只接受 `--config`、`--web-host`、`--web-port`、`--api`、`--web`，其他参数（例如 `--env-file`、`--user`）报错退出。需要这些选项时直接运行 `uv run dataagent serve`。`./deploy.sh` 把当时解析出的 API 主机名和端口写入部署状态。之后的 `tui`、`status`、`doctor` 使用这份地址。

选中的配置文件不存在、不是普通文件，或内容不是合法 JSON 时，`dataagent serve` 与 `dataagent serve-a2a` 以退出码 1 结束，进程不监听端口。错误写入 `backend.log` 并输出到标准错误，启动记录使用 JSON，包含 `component=dataagent.startup`、`stage` 和诊断堆栈。配置文件已读入但启动准备失败时，`serve` 可以继续以未就绪状态监听：`/healthz` 为 200，`data.conversation.ready` 为 false，`/ready` 为 503。独立 `serve-a2a` 不提供这两个健康接口。缺少环境变量时，先补上当前需要的变量再重启；非默认模型的环境引用按首次使用时解析。

开发和内部参数不属于对外接口，可能改变：

| 参数 | 作用 |
| --- | --- |
| `--reload` | 开发用。源码变化时重启 `serve`、`dataagent`、`a2a_server`。`npm run start` 和 `./deploy.sh` 不传 |
| `--stdio-ready` | TUI 拉起后端时的内部握手，帮助里不显示。和环境变量 `DATAAGENT_INSTANCE_ID` 一起使用；env 文件不能设置这个变量 |

## `dataagent serve-a2a`

把同一个运行时暴露成 A2A 1.0 服务，供其他 agent 调用。准备配置、用户和输入目录的参数与 `serve` 相同。不接受 `--session`：每个 A2A `contextId` 自己对应一条会话。

```bash
uv run --project runtime/agentkit dataagent serve-a2a \
  --config path/to/config.json \
  --user default \
  --workspace /existing/data \
  --env-file /path/.env \
  --host 127.0.0.1 \
  --port 8791
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--host` | 配置的 `server.host` | 绑定地址。需要被其他机器访问时再改为 `0.0.0.0`，并设置令牌 |
| `--port` | 配置的 `server.port` | 绑定端口。被占用则失败 |
| `--auth-token` | `DATAAGENT_A2A_AUTH_TOKEN` | 设置后，JSON-RPC 要带 `Authorization: Bearer <token>`。写在命令行会出现在进程列表里，优先用环境变量 |

JSON-RPC 固定在 `/a2a/jsonrpc`，请求头必须是 `A2A-Version: 1.0`。`--config`、`--user`、`--workspace`、`--env-file` 的含义与 `serve` 相同。这条命令占用 `<home>/runtime/backend.lock`，不能和 `dataagent serve` 同时使用同一 Home。`dataagent serve` 自己也会挂上同一组 A2A 路由。

调用约定见 [A2A](a2a.md)。

SDK 的 `LaunchOptions` 另有 `cwd` 和 `init_home`。可重复的 `--workspace` 对应 `LaunchOptions.workspaces`。CLI 启动使用 `init_home=True`，SDK 默认 `init_home=False`。

## `dataagent history-sql`

从语义服务下载历史 SQL，生成 de-agent `history_sql` 使用的语料快照。需要配置 `semantic`。

```bash
uv run --project runtime/agentkit dataagent history-sql --config path/to/config.json
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--config` | 无 | 同 `serve` |
| `--env-file` | 无 | 同 `serve` |
| `--output-dir` | 见下文 | 语料目录，必须是绝对路径 |
| `--compare-dir` | 无 | 和这个目录里的已有语料比较，结果写进输出的 `comparison` |
| `--page-size` | `200` | 每页条数 |
| `--if-enabled` | 关 | `plugins.config.de-agent.history_sql.enabled` 为 false 时直接退出 0 |
| `--no-lock` | 关 | 不把写好的快照改为只读 |

语料目录按这个顺序取第一个有值的：`--output-dir`、环境变量 `DATAAGENT_HISTORY_SQL_CORPUS`、`history_sql.corpus_path`、`<home>/history_sql`。顺序符合 R7（CLI > 环境变量 > 配置 > 默认）。

成功时在标准输出打印一份 JSON 报告，退出码 0。配置错误退出码 2，下载失败退出码 1。

## TUI

没有参数时拉起本机后端并连接 `/api/v1/runs`。

| 参数 | 作用 |
| --- | --- |
| （无参数） | 读 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。该地址上已有控制面就连上，否则拉起后端 |
| `--config`、`--env-file`、`--workspace`、`--user`、`--session` | 传给拉起的后端。即使该地址上已有控制面，也会再启动一个 |
| `--resume [threadId]` | 启动时恢复会话 |
| `--runtime-url` | 连接这个地址，不启动后端。不能和上一行的启动参数一起用 |

## 环境变量

| 变量 | 谁读取 | 作用 |
| --- | --- | --- |
| `DATAAGENT_HOME` | 后端进程环境 | Home。未设置或为空（含只有空白）时用 `~/.dataagent`；`~` 会展开；相对值相对启动 cwd。后端、Web 启动脚本、部署脚本和 TUI 用同一规则。写在后端所选 Home `.env` / `--env-file` 里会被忽略并警告 |
| `DATAAGENT_WORKSPACE` | 后端有效环境 | 单个只读输入。低于 CLI `--workspace`，高于配置 `workspaces` |
| `NO_COLOR` | TUI 进程 | `1` 时关闭颜色 |
| `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY` | 配置里的 `$env{...}` | 模板和示例使用的三个名字。配置里写成别的 `$env{...}` 时，以那个引用为准 |
| `DATAAGENT_OTLP_TRACES_ENDPOINT` | 后端进程环境 | 实时 OTLP/HTTP traces 的完整 URL。未设置则只写本地 trace 文件 |
| `DATAAGENT_OTLP_HEADERS` | 后端进程环境 | 实时导出的请求头，逗号分隔的 `key=value` |
| `DATAAGENT_A2A_AUTH_TOKEN` | 后端有效环境 | A2A JSON-RPC 的 Bearer 令牌。`serve` 和 `serve-a2a` 都读。进入日志脱敏 |
| `DATAAGENT_WEB_ORIGIN` | Web 进程环境 | 浏览器错误上报允许的 Origin，例如 `https://data.example.com`。代理、公网域名或通配地址部署时指定；不用于配置 Next 到后端的转发 |
| `DATAAGENT_HISTORY_SQL_CORPUS` | 后端进程环境 | 历史 SQL 语料目录，绝对路径。优先于 `history_sql.corpus_path` |

`$env{NAME}` 的名字必须匹配 `[A-Za-z_][A-Za-z0-9_]*`。

“后端进程环境”只读启动进程的环境变量，不读 Home `.env` 或 `--env-file`。插件自己读取的环境变量（例如 de-agent 的 `DATAOPS_*`）也是这样。要让这些变量生效，在启动命令的环境里设置，或写进仓库根 `.env` 由 `npm run start` 载入。

环境文件只读一份。传了 `--env-file` 就用它。没传时用 Home `.env`，`--config` 不改变这个选择。Home `.env` 可以不存在；`--env-file` 必须存在。进程里的非空值覆盖文件里的值。空值不写入有效环境。

输入目录、当前工作目录，以及 `--config` 旁边的 `.env` 不会自动加入。

仓库根 `.env` 由 npm/部署启动器预先载入进程，因此其中的值进入的是进程环境阶段，与后端选择 Home `.env` / `--env-file` 不同。`DATAAGENT_HOME` 建议启动前显式 export。后端加载环境文件不会修改 `os.environ`，也不会为另一个终端中的 curl 设置认证变量。

日志目录随 `DATAAGENT_HOME` 固定为 `runtime/logs/`，不另设日志目录变量。后端默认级别为 INFO，当前没有统一的 `LOG_LEVEL` 或 `DATAAGENT_LOG_LEVEL` 配置。`DATAAGENT_LOG_CAPTURE` 是启动器管理的内部输出收集标记，使用者不要手动设置；它用于避免子进程与启动器重复落盘。详见 [可观测性](../architecture/observability.md)。
