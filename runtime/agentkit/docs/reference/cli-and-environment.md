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
| `--config` | 无 | 使用这份文件，不再读 Home `config.json`。文件必须存在 |
| `--user` | `default` | 本地 profile。决定 `runtime/users/<user>/` |
| `--workspace` | 无 | 可重复。已存在的只读输入。与配置列表互斥于更高优先级 |
| `--session` | 无 | 恢复属于该 profile 的会话，并沿用其输入绑定 |
| `--env-file` | 无 | 只用这份环境文件，代替 Home `.env`。文件必须存在 |
| `--reload` | 关 | 源码变化时重启 `serve`、`dataagent`、`a2a_server`。`npm run start` 和 `./deploy.sh` 不传 |
| `--stdio-ready` | 关 | TUI 拉起后端时使用的隐藏握手。帮助里不显示 |

没有 `--host`。`npm run start`、网页转发和无参数 TUI 在启动时读所选配置的 `server.host` 与 `server.port`：传了 `--config` 用那份文件，否则用 Home `config.json`。默认 `127.0.0.1:8790`。TUI 无参数时，这个地址上没有控制面就拉起，已经在跑就连上。`npm run start -- --config <path>` 和 `./deploy.sh start --config <path>` 把同一路径交给 `dataagent serve`。`./deploy.sh` 把当时解析出的 API 主机名和端口写入部署状态。之后的 `tui`、`status`、`doctor` 使用这份地址。

选中的配置文件不存在、不是普通文件，或内容不是合法 JSON 时，`dataagent serve` 与 `dataagent serve-a2a` 以退出码 1 结束。标准错误有一行 `DataAgent:`。进程不监听端口。配置文件已经读入、模型环境变量尚未提供时，进程仍会监听：`/healthz` 为 200，`conversation.ready` 为 false，`/ready` 为 503。

## `dataagent serve-a2a`

把同一个运行时暴露成 A2A 1.0 服务，供其他 agent 调用。准备配置、用户和输入目录的参数与 `serve` 相同。不接受 `--session`：每个 A2A `contextId` 自己对应一条会话。

```bash
uv run --project runtime/agentkit dataagent serve-a2a \
  --config path/to/config.json \
  --user default \
  --workspace /existing/data \
  --env-file /path/.env \
  --host 127.0.0.1 \
  --port 9999
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 绑定地址。需要被其他机器访问时再改为 `0.0.0.0` |
| `--port` | `9999` | 绑定端口。被占用则失败 |
| `--auth-token` | 无 | 设置后，除 Agent Card 外都要带 `Authorization: Bearer <token>` |

JSON-RPC 固定在 `/a2a/jsonrpc`，请求头必须是 `A2A-Version: 1.0`。`--config`、`--user`、`--workspace`、`--env-file` 的含义与 `serve` 相同。这条命令占用 `<home>/runtime/backend.lock`，不能和 `dataagent serve` 同时使用同一 Home。`dataagent serve` 自己也会挂上同一组 A2A 路由。

调用约定见 [A2A](a2a.md)。

SDK 的 `LaunchOptions` 还有 `cwd`、`init_home` 和单个 `workspace`。`workspace` 与 `workspaces` 不能同时传。CLI 只暴露可重复的 `--workspace`。CLI 启动使用 `init_home=True`。SDK 默认 `init_home=False`。

## TUI

没有参数时拉起本机后端并连接 `/dataagent/stream`。

| 参数 | 作用 |
| --- | --- |
| （无参数） | 读 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。该地址上已有控制面就连上，否则拉起后端 |
| `--config`、`--env-file`、`--workspace`、`--user`、`--session` | 传给拉起的后端。即使该地址上已有控制面，也会再启动一个 |
| `--resume [threadId]` | 启动时恢复会话 |
| `--runtime-url` | 连接这个地址，不再启动后端。不能和上一行的启动参数一起用 |

## 环境变量

| 变量 | 谁读取 | 作用 |
| --- | --- | --- |
| `DATAAGENT_HOME` | 后端进程环境 | Home。默认 `~/.dataagent`。相对值相对启动 cwd。写在 env 文件里会被忽略并警告 |
| `DATAAGENT_WORKSPACE` | 后端有效环境 | 单个只读输入。低于 CLI `--workspace`，高于配置 `workspaces` |
| `DATAAGENT_INSTANCE_ID` | 后端进程环境 | TUI 拉起后端时写入的实例 ID。env 文件不能设置 |
| `NO_COLOR` | TUI 进程 | `1` 时关闭颜色 |
| `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY` | 配置里的 `$env{...}` | 模板和示例使用的三个名字。配置里写成别的 `$env{...}` 时，以那个引用为准 |
| `DATAAGENT_OTLP_TRACES_ENDPOINT` | 后端进程环境 | 实时 OTLP/HTTP traces 的完整 URL。未设置则只写本地 trace 文件 |
| `DATAAGENT_OTLP_HEADERS` | 后端进程环境 | 实时导出的请求头，逗号分隔的 `key=value` |

`$env{NAME}` 的名字必须匹配 `[A-Za-z_][A-Za-z0-9_]*`。

环境文件只读一份。传了 `--env-file` 就用它。没传时用 Home `.env`，`--config` 不改变这个选择。Home `.env` 可以不存在；`--env-file` 必须存在。进程里的非空值覆盖文件里的值。空值不写入有效环境。

输入目录、当前工作目录，以及 `--config` 旁边的 `.env` 不会自动加入。
