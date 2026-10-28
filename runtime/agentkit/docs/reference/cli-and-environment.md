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
| `--config` | 无 | Home 之上的额外配置层。文件必须存在 |
| `--user` | `default` | 本地 profile。决定 `runtime/users/<user>/` |
| `--workspace` | 无 | 可重复。已存在的只读输入。与配置列表互斥于更高优先级 |
| `--session` | 无 | 恢复属于该 profile 的会话，并沿用其输入绑定 |
| `--env-file` | 无 | 后端读取的环境文件。文件必须存在 |
| `--stdio-ready` | 关 | TUI 内部协议。帮助里隐藏 |

没有 `--host`。`npm run dev`、`./deploy.sh`、网页转发和 TUI 都读 `config.json` 的 `server.host` 与 `server.port`。默认 `127.0.0.1:8790`。`--stdio-ready` 仍给当前 TUI 的一键拉起使用。

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

TUI 参数由 `apps/tui` 解析，再转成后端启动参数或远程 URL。

| 参数 | 作用 |
| --- | --- |
| `--v2` | 使用 AgentKit 后端 |
| `--config` | 传给后端的额外配置 |
| `--env-file` | 传给后端。不配置 TUI 自身 |
| `--workspace` | 传给后端的只读输入 |
| `--resume [threadId]` | 启动时恢复。省略时走恢复流程 |
| `--runtime-url` | 连接已有流地址。不管理该进程的配置、环境或输入 |

`--runtime-url` 与 `--config`、`--env-file`、`--workspace` 不能组合。

## 环境变量

| 变量 | 谁读取 | 作用 |
| --- | --- | --- |
| `DATAAGENT_HOME` | 后端进程环境 | Home。默认 `~/.dataagent`。相对值相对启动 cwd。写在 env 文件里会被忽略并警告 |
| `DATAAGENT_WORKSPACE` | 后端有效环境 | 单个只读输入。低于 CLI `--workspace`，高于配置 `workspaces` |
| `DATAAGENT_V2_INSTANCE_ID` | 后端进程环境 | TUI 启动协议要求的实例 ID。env 文件不能设置 |
| `DATAAGENT_V2_TOKEN` | 进程环境 | 保留给进程。写在 env 文件里会被忽略。HTTP API 不读取访问令牌 |
| `DATAAGENT_V2_LOG_PATH` | TUI 进程 | 界面日志。后端 `--env-file` 不生效 |
| `INIT_CWD` | TUI 进程 | 路径基准相关。必须在 TUI 进程里 |
| `NO_COLOR` | TUI 进程 | `1` 时关闭颜色 |
| `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY` | 配置里的 `$env{...}` | 模板使用的三个名字。你可以改用别的名字，只要引用一致 |

`$env{NAME}` 的名字必须匹配 `[A-Za-z_][A-Za-z0-9_]*`。

有效环境优先级：进程非空值，然后 `--env-file`，然后显式配置旁边的 `.env`，然后 Home `.env`。更低层先载入，高层覆盖。空值不覆盖已有非空值。同一真实文件只保留优先级更高的那一次。

输入目录和当前工作目录里的 `.env` 不会自动加入，除非它正好是上述某一层。
