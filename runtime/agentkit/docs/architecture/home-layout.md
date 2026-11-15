# Home、工作区与目录

运行时只有一个 Home，默认是 `~/.dataagent`。进程环境变量 `DATAAGENT_HOME` 可以改成别的目录；相对路径相对启动时的 `cwd`。写在 `.env` 文件里的 `DATAAGENT_HOME` 会被忽略。

当前工作目录下的 `.dataagent/` 不会被读取。放在那里的 Skill、插件和配置不生效。

## Home

`init_home`（CLI 默认会做，SDK 默认不做）创建下面这些目录和模板。已有文件不覆盖。新建目录模式 `0700`，新建的 `config.json` 和 `.env` 模式 `0600`。

```text
<home>/
  config.json
  .env
  .mcp.json                 # 可选，初始化不会创建
  skills/<name>/SKILL.md
  hooks/                    # 只是目录；不会自动导入
  plugins/<id>/
  outputs/<user>/<thread>/  # 默认产物目录，打开会话时才创建
    artifacts/
  runtime/
    backend.lock
    checkpoints.sqlite
    sessions.sqlite         # session_bindings 与 REST 会话索引
    logs/backend.log
    users/<user>/sessions/<thread>/
      traces/<run_id>.json
```

| 路径 | 作用 |
| --- | --- |
| `config.json` | 个人配置。字段见 [配置参考](../reference/configuration.md) |
| `.env` | `$env{...}` 用到的模型名、地址和密钥 |
| `.mcp.json` | Home 级 MCP 连接。已启用插件根目录里也可以各有一份 |
| `skills/` | 用户 Skill。来源标签是 `User` |
| `hooks/` | 初始化时创建。Hook 只来自配置里的 `entrypoint`，不会扫描这个目录 |
| `plugins/<id>/` | 用户插件。目录名必须等于 `.plugin.json` 的 `id`，并且出现在 `plugins.enabled` |
| `runtime/` | 这个 Home 的全部进程状态。同一时刻只能有一个后端持有 `backend.lock` |

包内 `builtin-plugins/` 不在 Home 里。它和 `plugins.paths` 里的目录一起作为插件候选。

## 会话目录

`--user` 选择 `runtime/users/<user>/`，默认 `default`。这是本地目录名，不是登录账号。`user` 和 `threadId` 只能包含字母、数字、下划线和连字符，最长 128 个字符。

| 文件 | 谁写入 |
| --- | --- |
| `checkpoints.sqlite` | 图状态和消息 |
| `sessions.sqlite` | `session_bindings` 保存输入和产物目录；REST 的 `sessions` 表保存会话索引。两张表互不读写 |
| `outputs/<user>/<thread>/` | 默认产物目录。`dataagent.output_root` 或 SDK `output_dir` 可以改位置 |
| `outputs/.../artifacts/` | 长工具结果和对话卸载。列表接口跳过 `large_tool_results` 和 `conversation_history` |
| `traces/<run_id>.json` | 根运行结束时的本地导出。控制面会话目录里只有这一处 |

会话目录不含 `logs/` 或 `outputs/`。服务日志只在 `runtime/logs/backend.log`。SDK 未指定会话时，thread id 用 `sdk`。

绑定规则和产物目录优先级见 [会话与产物](../guides/sessions-and-outputs.md)。

## 工作区

工作区是进程启动时绑定的只读输入目录，不是 Home 里的子目录，也没有单独的工作区配置服务。

配置写在 `dataagent.workspaces`：

```json
{
  "dataagent": {
    "workspaces": [
      {"name": "input", "path": "/data/reports", "read_only": true}
    ]
  }
}
```

| 字段 | 约束 |
| --- | --- |
| `name` | 逻辑名。`[A-Za-z0-9][A-Za-z0-9_-]{0,127}`，列表内唯一。不会变成路径的一段 |
| `path` | 已存在且进程可读、可进入的目录。解析后唯一 |
| `read_only` | 只能是 `true` |

来源由高到低：重复的 `--workspace`、环境变量 `DATAAGENT_WORKSPACE`、配置里的 `dataagent.workspaces`。都没有时列表为空。命令行输入的名称是 `workspace-0`、`workspace-1`。

新会话把当时的输入列表和产物目录写入 `sessions.sqlite` 的 `session_bindings`。恢复时继续用这份绑定。启动参数给出不同路径列表时，准备失败，需要另开新会话。

系统提示要求模型把这些目录当只读，并把新文件写到该会话的 `outputs/`。提示不改变操作系统权限。文件工具和 shell 使用进程自己的权限。

## 一个服务，两种展示

产品怎么启动写在仓库 `docs/zh/quick-start.md`。这里只记录进程关系。

`dataagent serve` 是唯一的控制面，默认听 `config.json` 的 `server.host` 和 `server.port`（`127.0.0.1:8790`）。

| 展示 | 怎么接上 |
| --- | --- |
| Web | `npm run start:web` 或 `npm run start`。浏览器打开 `http://127.0.0.1:3000/data-tasks`。Next 把 `/dataagent/stream` 和 `/api/v1/*` 转到同一个控制面 |
| TUI | `npm run start:tui`。没有控制面就拉起一个，已经在听就连上。对话走 `POST /dataagent/stream` |

两边共用会话索引、Checkpoint 和 `outputs/`。Python SDK 可以不经 HTTP，直接 `prepare_runtime`。改模型、监听地址、Home、工作区或 `dataagent.entry` 后要重启这个进程。
