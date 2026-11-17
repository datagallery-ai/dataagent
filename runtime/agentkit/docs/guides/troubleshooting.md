# 故障排查

先看进程打出的那一行 `DataAgent: ...`。密钥会被替换后再显示。后端文件日志在 `<home>/runtime/logs/backend.log`，单文件约 5 MB，保留 3 个轮转文件。

## A2A 报障

先保存响应头 `X-Request-ID` 和任务号。任务失败时读取 `metadata.dataagent.error.code`、`stage`、`message`；HTTP 200 也可能是失败 Task。任务查询和取消不会改变创建请求的 `metadata.dataagent.requestId`。

在 `<home>/runtime/logs/backend.log` 及其轮转文件中搜索这个请求号或任务号。`a2a.execution.failed` 给出失败阶段、错误分类和栈帧位置；`a2a.trace.saved` 给出对应原生运行号与 trace 文件。输入校验失败不会创建图 trace，只能查入口或执行拒绝日志。

`checkpoint_settle` 表示图执行后的 checkpoint 保存阶段；先修复存储问题，不要只凭成功的图 trace 判断整个任务成功。`a2a.checkpoint.failed`、`a2a.cleanup.failed` 是附加诊断，不会用清理错误替换原始模型或工具错误。

默认匿名、同团队共享访问，无法从日志确定真实访问者。内存 Task 在服务重启后不可查询，但会话和 checkpoint 仍可续接；旧调用关联在日志保留期内可检索。本服务没有自动重试或执行去重，错误码不能单独作为安全重试依据。

## 启动

| 现象 | 处理 |
| --- | --- |
| `Cannot determine user home; set DATAAGENT_HOME` | 在进程环境设置 `DATAAGENT_HOME` 为可写目录 |
| `Configuration file does not exist`、`Expected a configuration file`、`Invalid JSON` | 进程以退出码 1 结束，不监听端口。检查 `--config` 指向的文件。相对路径相对启动命令的工作目录 |
| `unknown field` 或重复键 | 删掉未知字段。JSON 不允许重复键，包括嵌套对象。`schema_version` 只能是产品配置的 `4`、插件清单的 `3` 或子代理 JSON 的 `1`；版本不对时报错会说明怎样升级 |
| `missing environment variable LLM_...` | 在这次选中的环境文件或进程环境中提供该变量。传了 `--env-file` 用那份文件，否则用 Home `.env` |
| `Port 127.0.0.1:8790 is occupied` | 改 `config.json` 的 `server.port`，或停掉占用进程。启动不会接管已有服务 |
| 引擎未就绪 | 配置文件已经读入，对话引擎没有起来。进程仍在。`/healthz` 为 200，`data.conversation.ready` 为 false。`/ready` 和对话返回 503。标准错误里有 `DataAgent:`，常见原因是模型环境变量还没填 |
| `Another DataAgent backend owns this workspace` | 同一个 `stateDir` 已有后端持有 `backend.lock`。停掉它，或换 `DATAAGENT_HOME` |
| `Expected a directory` | Home 下的 `skills`、`hooks`、`plugins` 或 `runtime` 被文件占用 |

`DATAAGENT_HOME` 和 `DATAAGENT_INSTANCE_ID` 只认进程环境。出现 “Ignoring process-only variable” 时，从 `.env` 里删掉它们。

## 模型

| 现象 | 处理 |
| --- | --- |
| `dataagent.model does not name a configured model` | `dataagent.model` 必须是 `models` 里的 ID，不是服务商模型名 |
| `Unknown model alias` | 工具调用了未配置的别名。补上 `models.<id>`，或改调用 |
| 连接超时或 401 | 核对 `base_url` 和 `api_key`。地址写 API 根，不要写到 `/chat/completions` |
| 重试很久后仍失败 | `max_retries` 默认 2，即最多 3 次 HTTP。设为 `0` 可关闭。它不重跑已经开始的 Agent 循环 |

默认模型的环境引用在启动时展开。其他模型第一次被用到才展开。改 `.env` 后要重启，刷新扩展不会重读模型字段。

## 连接

| 现象 | 处理 |
| --- | --- |
| TUI 连不上 | 无参数时连 `config.json` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。先确认 `/healthz` 里 `conversation.ready` 为 true。只连接、不拉起时用 `--runtime-url`。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。部署栈用 `./deploy.sh tui` |
| 带 `--workspace` 等参数时端口已被占用 | 这些参数会再拉起一个后端。要连已有控制面就去掉它们，只运行 `npm run start:tui` |
| MCP 发现失败 | 修正 `<home>/.mcp.json` 或插件 `.mcp.json` 后重启。发现失败时进程仍在，对话路径返回 503 |
| 语义服务 TLS 失败 | `verify_server` 为 false 时不校验服务端证书。要校验时同时提供可达的 `ca_file`。客户端证书和私钥必须成对出现 |

## 恢复

| 现象 | 处理 |
| --- | --- |
| `Session ... does not belong to user` | `threadId` 属于另一个 `--user`。换对应用户，或新开对话 |
| `bound to a different workspace list` | 恢复时不要换输入目录。新输入请新开会话 |
| 首轮报错后同一会话发不出去 | 用新的 `runId` 再发一条新消息。没有检查点时 `/api/v1/runs` 从最新图状态继续。取消会把会话指到图里当时的检查点，界面和下一轮读同一段历史 |
| HTTP 409，`A run is already active for this session` | 等当前轮结束，或先取消这一轮。同一会话不能并行两轮。读取会话在运行中也返回 200 |
| `Run replay is not supported` | 使用新的 `runId`，并发送一条新的用户消息 |
| `Extension reload failed` | 这一轮拒绝新查询。正在跑的轮次仍用旧图。修好插件或 Hook 后再发 |
| `Output directory ... is used by session ...` | 产物目录已属于另一个会话（可能在另一个 Home）。换目录；确认对方不再使用时，删除报错中的标记文件。见 [会话与产物](sessions-and-outputs.md#常见报错) |
| `Output directory ... already contains files that no session owns` | `output_root` 派生的目录里已有不明文件。清空它，或用 SDK `output_dir` 显式接管 |

## 产物是空的

`/outputs` 只列出当前会话产物目录下的普通文件。产物目录可能不在 Home 内，实际位置见 Home 下 `sessions.sqlite` 的 `session_bindings` 表。列表排除 `artifacts/conversation_history` 和 `artifacts/large_tool_results`，也不跟随符号链接。文件若写到了其他路径，列表里不会出现。列出权限错误时会报错，而不是显示空列表。
