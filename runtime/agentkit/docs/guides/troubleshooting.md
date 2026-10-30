# 故障排查

先看进程打出的那一行 `DataAgent: ...`。密钥会被替换后再显示。后端文件日志在 `<home>/runtime/logs/backend.log`，单文件约 5 MB，保留 3 个轮转文件。

## 启动

| 现象 | 处理 |
| --- | --- |
| `Cannot determine user home; set DATAAGENT_HOME` | 在进程环境设置 `DATAAGENT_HOME` 为可写目录 |
| `Configuration file does not exist` | 检查 `--config` 路径。相对路径相对启动命令的工作目录 |
| `schema_version must be 3` | 把产品配置改为 JSON，并把 Hook 改成事件加 Python 入口。只改后缀不够 |
| `unknown field` 或重复键 | 删掉未知字段。JSON 不允许重复键，包括嵌套对象 |
| `missing environment variable LLM_...` | 在 Home `.env`、`--env-file` 或进程环境中提供该变量 |
| `Port 127.0.0.1:8790 is occupied` | 改 `config.json` 的 `server.port`，或停掉占用进程。启动不会接管已有服务 |
| 引擎未就绪 | 进程仍在。`/healthz` 为 200，`data.conversation.ready` 为 false。`/ready` 和对话返回 503。看标准错误里的 `DataAgent:` |
| `Another DataAgent V2 backend owns this workspace` | 同一个 `stateDir` 已有后端持有 `backend.lock`。停掉它，或换 `DATAAGENT_HOME` |
| `Expected a directory` | Home 下的 `skills`、`hooks`、`plugins` 或 `runtime` 被文件占用 |

`DATAAGENT_HOME` 和 `DATAAGENT_V2_INSTANCE_ID` 只认进程环境。出现 “Ignoring process-only variable” 时，从 `.env` 里删掉它们。

## 模型

| 现象 | 处理 |
| --- | --- |
| `dataagent.model.default does not name a configured model` | `default` 必须是 `models` 里的 ID，不是服务商模型名 |
| `Unknown model alias` | 工具调用了未配置的别名。补上 `models.<id>`，或改调用 |
| 连接超时或 401 | 核对 `base_url` 和 `api_key`。地址写 API 根，不要写到 `/chat/completions` |
| 重试很久后仍失败 | `max_retries` 默认 2，即最多 3 次 HTTP。设为 `0` 可关闭。它不重跑已经开始的 Agent 循环 |

默认模型的环境引用在启动时展开。其他模型第一次被用到才展开。改 `.env` 后要重启，刷新扩展不会重读模型字段。

## 连接

| 现象 | 处理 |
| --- | --- |
| TUI 连不上 | 无参数时连 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。先确认 `/healthz` 里 `conversation.ready` 为 true。只连接、不拉起时用 `--runtime-url`，主机必须是 `server.host` 或 `127.0.0.1` |
| 带 `--workspace` 等参数时端口已被占用 | 这些参数会再拉起一个后端。要连已有控制面就去掉它们，只运行 `npm run start:tui` |
| MCP 发现失败 | 修正 `<home>/.mcp.json` 或插件 `.mcp.json` 后重启。发现失败时进程仍在，对话路径返回 503 |
| 语义服务 TLS 失败 | `verify_server` 为 false 时不校验服务端证书。要校验时同时提供可达的 `ca_file`。客户端证书和私钥必须成对出现 |

## 恢复

| 现象 | 处理 |
| --- | --- |
| `Session ... is not in profile` | `threadId` 属于另一个 `--user`。换对应用户，或新开对话 |
| `bound to a different workspace list` | 恢复时不要换输入目录。新输入请新开会话 |
| 首轮报错后同一会话发不出去 | 用新的 `runId` 再发一条新消息。没有检查点时 `/dataagent/stream` 从最新图状态继续。取消会把会话指到图里当时的检查点，界面和下一轮读同一段历史 |
| `Session is currently running` 或 HTTP 409 | 等当前轮结束。同一会话不能并行两轮 |
| `Run replay is not supported` | 使用新的 `runId`，并发送一条新的用户消息 |
| `Extension reload failed` | 这一轮拒绝新查询。正在跑的轮次仍用旧图。修好插件或 Hook 后再发 |
| `Output directory ... is used by session ...` | 产物目录已属于另一个会话（可能在另一个 Home）。换目录；确认对方不再使用时，删除报错中的标记文件。见 [会话与产物](sessions-and-outputs.md#常见报错) |
| `Output directory ... already contains files that no session owns` | `output_root` 派生的目录里已有不明文件。清空它，或用 SDK `output_dir` 显式接管 |

## 产物是空的

`/outputs` 只列出当前会话产物目录下的普通文件。产物目录可能不在 Home 内，实际位置见 Home 下 `sessions.sqlite` 的 `session_bindings` 表。列表排除 `artifacts/conversation_history` 和 `artifacts/large_tool_results`，也不跟随符号链接。文件若写到了其他路径，列表里不会出现。列出权限错误时会报错，而不是显示空列表。
