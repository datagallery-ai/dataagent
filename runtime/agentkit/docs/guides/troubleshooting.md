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
| `Port 127.0.0.1:8790 is occupied` | 换 `server.port`，或停掉占用进程。启动不会接管已有服务 |
| `Another DataAgent V2 backend owns this workspace` | 同一个 `stateDir` 已有后端持有 `backend.lock`。停掉它，或换 `DATAAGENT_HOME` |
| `Expected a directory` | Home 下的 `skills`、`hooks`、`plugins` 或 `runtime` 被文件占用 |

`DATAAGENT_HOME`、`DATAAGENT_V2_INSTANCE_ID` 和 `DATAAGENT_V2_TOKEN` 只认进程环境。出现 “Ignoring process-only variable” 时，从 `.env` 里删掉它们。

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
| TUI 连不上 `--runtime-url` | URL 使用 `http://127.0.0.1:8790/dataagent/stream`。先确认 `/healthz` 返回 `ready` |
| 外部模式仍试图读本地配置 | `--runtime-url` 不能和 `--config`、`--env-file`、`--workspace` 一起用 |
| MCP 导致后端起不来 | 修正 `<home>/.mcp.json` 或插件 `.mcp.json`。发现失败会阻止启动 |
| 语义服务 TLS 失败 | `verify_server` 为 false 时不校验服务端证书。要校验时同时提供可达的 `ca_file`。客户端证书和私钥必须成对出现 |

## 恢复

| 现象 | 处理 |
| --- | --- |
| `Session ... is not in profile` | `threadId` 属于另一个 `--user`。换对应用户，或新开对话 |
| `bound to a different workspace list` | 恢复时不要换输入目录。新输入请新开会话 |
| `No successful checkpoint exists` | 上次没有成功结束。新开对话 |
| `Session is currently running` 或 HTTP 409 | 等当前轮结束。同一会话不能并行两轮 |
| `Run replay is not supported` | 使用新的 `runId`，并发送一条新的用户消息 |
| `Extension reload failed` | 这一轮拒绝新查询。正在跑的轮次仍用旧图。修好插件或 Hook 后再发 |

## 产物是空的

`/outputs` 只列出当前会话 `outputs/` 下的普通文件。它排除 `artifacts/conversation_history` 和 `artifacts/large_tool_results`，也不跟随符号链接。文件若写到了其他路径，列表里不会出现。列出权限错误时会报错，而不是显示空列表。
