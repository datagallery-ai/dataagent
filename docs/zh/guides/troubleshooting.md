# 故障排查

先按现象确定要检查的组件，再保存失败请求的关联 ID。分开部署时，Server、Web 和 TUI 可能运行在不同机器上。到对应进程的 DataAgent Home 查找 `runtime/logs/`。[查看 Home 目录](../../development/architecture/home-layout.md)

| 现象 | 先检查 | 日志与关联信息 |
| --- | --- | --- |
| 受管部署失败，或工作台打不开 | 在仓库根目录运行 `./deploy.sh status` 和 `./deploy.sh doctor`。独立进程先看启动终端。 | 环境准备、整体启停看 `backend.log`，Web 构建、页面和代理看 `web.log`。记录失败时间和部署阶段。 |
| 页面能打开，但发起对话报错 | 检查 Server 是否存活和就绪，再看响应或 AG-UI 的 `RUN_ERROR`。 | 查 `backend.log`。保存错误码、可取得的 `X-Request-ID`、`threadId` 和 `runId`。 |
| TUI 连不上，或终端界面异常 | 检查 TUI 实际连接的 Server 地址及其 `/ready` 响应。 | 查 TUI 进程 Home 的 `tui.log`。Server 返回错误时，用时间和轮次号查 Server 的 `backend.log`。 |
| A2A 请求或任务失败 | 依次检查 HTTP 状态、JSON-RPC 响应和 Task 状态。 | 查 `backend.log`。保存 `X-Request-ID`、任务号、错误码和阶段。 |
| 一轮结束后找不到 Trace | 确认 Server 或 SDK 进程使用的 Home，并在宿主日志中搜索 `TRACE_SAVE_ERROR`。 | 按 `threadId` 找会话 Trace 目录，再关联原生运行号。见[执行 Trace](../../development/architecture/execution-traces.md)。 |

Linux 一键部署可用 `./deploy.sh logs` 同时跟随三个文件。独立启动的进程如果没有写出日志文件，先看启动终端。后端写文件失败时可以退回到 stderr。故障早于当前日志时，继续检查轮转文件。分享日志片段前，去除凭据、提示词和业务数据。[日志格式与保留策略](../../development/architecture/observability.md)

把下面的 Home 路径和请求号或轮次号换成实际值，再搜索当前与轮转后的后端日志：

```bash
home="/path/to/dataagent-home"
id="your-request-or-run-id"
grep -F -- "$id" "$home"/runtime/logs/backend.log*
```

REST 诊断使用 `requestId`、`threadId`、`runId`。A2A 审计日志使用 `request_id`、`task_id`。客户端拿不到 ID 时，记录失败时间。

直接检查 `dataagent serve` 时，把下面的地址换成 Server 实际监听地址：

```bash
curl -i http://127.0.0.1:8790/healthz
curl -i http://127.0.0.1:8790/ready
```

`/healthz` 返回 200 表示进程正在响应。如果 `/ready` 返回 503，检查启动或扩展发现日志。引擎已就绪时，单轮仍可能遇到模型或工具错误。独立启动的 `serve-a2a` 使用[发现和 JSON-RPC 入口](../reference/a2a.md)。

<a id="a2a"></a><a id="a2a-报障"></a>

<a name="a2a-failures"></a>

## A2A 报障

先保存响应头 `X-Request-ID` 和任务号。任务失败时读取 `metadata.dataagent.error.code`、`stage`、`message`；HTTP 200 也可能是失败 Task。任务查询和取消不会改变创建请求的 `metadata.dataagent.requestId`。

在 `backend.log` 及其轮转文件中按 `request_id` 或 `task_id` 查找。`a2a.assembly.failed` 给出失败阶段、错误分类和栈帧位置；`a2a.trace.saved` 给出对应原生运行号与 trace 文件。输入校验失败不会创建图 trace，只能查入口或执行拒绝日志。

`checkpoint_settle` 表示图执行后的 checkpoint 保存阶段；先修复存储问题，不要只凭成功的图 trace 判断整个任务成功。`a2a.checkpoint.failed`、`a2a.cleanup.failed` 是附加诊断，不会用清理错误替换原始模型或工具错误。

默认匿名、同团队共享访问，无法从日志确定真实访问者。内存 Task 在服务重启后不可查询，但会话和 checkpoint 仍可续接；旧调用关联在日志保留期内可检索。本服务没有自动重试或执行去重，错误码不能单独作为安全重试依据。

<a id="_2"></a><a id="启动"></a>

<a name="startup"></a>

## 启动

查后端启动错误时，按 `component=dataagent.startup` 和 `stage=prepare` 或 `stage=start` 筛选 JSON 日志。启动失败可能早于 Agent 运行和 Trace 的创建。

| 现象 | 处理 |
| --- | --- |
| `Cannot determine user home; set DATAAGENT_HOME` | 在进程环境设置 `DATAAGENT_HOME` 为可写目录 |
| `Configuration file does not exist`、`Expected a configuration file`、`invalid YAML syntax` | 进程以退出码 1 结束，不监听端口。检查 `--config` 指向的文件。相对路径相对启动命令的工作目录 |
| `unknown field` 或重复键 | 删掉未知字段。YAML 不允许重复键，包括嵌套对象。`schema_version` 只能是产品配置的 `4`、插件清单的 `3` 或子代理 YAML 的 `1`；版本不对时报错会指出支持的版本 |
| `missing environment variable LLM_...` | 在这次选中的环境文件或进程环境中提供该变量。传了 `--env-file` 用那份文件，否则用 Home `.env` |
| `Port 127.0.0.1:8790 is occupied` | 改 `config.yaml` 的 `server.port`，或停掉占用进程。启动不会接管已有服务 |
| 引擎未就绪 | 配置文件已经读入，对话引擎没有起来。`serve` 进程仍在，`/healthz` 为 200，`data.conversation.ready` 为 false，`/ready` 和对话返回 503。查看 `backend.log` 中的启动错误，常见原因是模型环境变量还没填。独立 `serve-a2a` 没有这两个健康接口 |
| `Another DataAgent backend owns this workspace` | 同一个 `stateDir` 已有后端持有 `backend.lock`。停掉它，或换 `DATAAGENT_HOME` |
| `Expected a directory` | Home 下的 `skills`、`hooks`、`plugins` 或 `runtime` 被文件占用 |

`DATAAGENT_HOME` 和 `DATAAGENT_INSTANCE_ID` 只认进程环境。出现 “Ignoring process-only variable” 时，从 `.env` 里删掉它们。

<a id="_3"></a><a id="模型"></a>

<a name="models"></a>

## 模型

| 现象 | 处理 |
| --- | --- |
| `dataagent.model does not name a configured model` | `dataagent.model` 必须是 `models` 里的 ID，不是服务商模型名 |
| `Unknown model alias` | 工具调用了未配置的别名。补上 `models.<id>`，或改调用 |
| 连接超时或 401 | 核对 `base_url` 和 `api_key`。地址写 API 根，不要写到 `/chat/completions` |
| 重试很久后仍失败 | `max_retries` 默认 2，即最多 3 次 HTTP。设为 `0` 可关闭。它不重跑已经开始的 Agent 循环 |

默认模型的环境引用在启动时展开。其他模型第一次被用到才展开。改 `.env` 后要重启，刷新扩展不会重读模型字段。

<a id="_4"></a><a id="连接"></a>

<a name="connections"></a>

## 连接

| 现象 | 处理 |
| --- | --- |
| TUI 连不上 | 无参数时连 `config.yaml` 的 `server.host` 和 `server.port`，默认 `http://127.0.0.1:8790`。先确认 `/healthz` 里 `conversation.ready` 为 true。只连接、不拉起时用 `--runtime-url`。主机可以是 Home 的 `server.host`、回环地址或其他 IP。`0.0.0.0` 和 `::` 会改走回环地址。部署栈用 `./deploy.sh tui` |
| 带 `--config` 等参数时端口已被占用 | 这些参数用于启动后端。要连指定的已有 Server，用 `--runtime-url` 明确给出地址。受管部署也可用 `./deploy.sh tui` |
| MCP 发现失败 | 先查 `backend.log` 确定失败的 MCP 服务。首次发现失败时，`serve` 可保持健康接口可用，对话路径返回 503。修正 Home 或插件的 `.mcp.yaml` 后重启 |
| 语义服务 TLS 失败 | `verify_server` 为 false 时不校验服务端证书。要校验时同时提供可达的 `ca_file`。客户端证书和私钥必须成对出现 |

<a id="_5"></a><a id="恢复"></a>

<a name="resume"></a>

## 恢复

| 现象 | 处理 |
| --- | --- |
| `Session ... does not belong to user` | `threadId` 属于另一个 `--user`。换对应用户，或新开对话 |
| `bound to a different workspace list` | 恢复时不要换输入目录。新输入请新开会话 |
| 首轮报错后同一会话发不出去 | 用新的 `runId` 再发一条新消息。没有检查点时 `/api/v1/runs` 从最新图状态继续。取消会把会话指到图里当时的检查点，界面和下一轮读同一段历史 |
| HTTP 409，`A run is already active for this session` | 等当前轮结束，或先取消这一轮。同一会话不能并行两轮。读取会话在运行中也返回 200 |
| `Run replay is not supported` | 使用新的 `runId`，并发送一条新的用户消息 |
| `Extension reload failed` | 按 `stage=reload` 查 `backend.log`，修正插件、Hook 或 MCP 连接后再发起请求。刷新失败的请求返回 HTTP 500，旧运行时保留 |
| `Output directory is used by session ...` | 产物目录与同一 Home 内已登记的会话目录相同或嵌套。为新会话选择互不重叠的目录，见[会话与产物](sessions-and-outputs.md#common-errors) |
| `Output directory ... already contains files that no session owns` | `output_root` 派生的目录里已有不明文件。清空它，或用 SDK `output_dir` 显式接管 |

<a id="_6"></a><a id="产物是空的"></a>

<a name="empty-outputs"></a>

## 产物是空的

`/outputs` 只列出当前会话产物目录下的普通文件。产物目录可能不在 Home 内，实际位置见 Home 下 `sessions.sqlite` 的 `session_bindings` 表。列表排除 `artifacts/conversation_history` 和 `artifacts/large_tool_results`，也不跟随符号链接。文件若写到了其他路径，列表里不会出现。列出权限错误时会报错，而不是显示空列表。

本地文件已生成但收集端看不到 Trace 时，见 [OTLP/HTTP 导出](trace-export.md)。
