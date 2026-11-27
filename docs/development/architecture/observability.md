# 可观测性

这里说明运行日志的结构、记录边界和协议 ID 如何关联。遇到故障时，从[故障排查](../../zh/guides/troubleshooting.md)选择日志和检查步骤。会话持久数据见[状态与存储](state-and-storage.md)。

## 日志

所有正式入口统一使用 `<home>/runtime/logs/`，Home 来自 `DATAAGENT_HOME`，默认 `~/.dataagent`。

| 文件 | 内容 |
| --- | --- |
| `backend.log` | 后端、模型、工具、插件，以及公共依赖准备、环境检查、整体启停 |
| `web.log` | Web 构建、Next.js 服务端、代理错误、浏览器错误上报 |
| `tui.log` | TUI 构建、启动、连接、传输和交互错误 |

不再生成独立的部署日志和进程输出汇总文件。`./deploy.sh logs` 同时跟随这三个文件。历史日志保留在原位置，不自动删除。

日志每行是一个 JSON 对象，包含 `timestamp`、`level`、`component`、`message`。有上下文时附带 `stage`、`requestId`、`threadId`、`runId`、`code`。异常堆栈保存为 `stack`（Node 异常在 `error.stack` 中）。文件日志保留换行和完整堆栈；JSON 编码让每个事件仍只占一行。用户可见的 `safe_error` 仍输出不超过 1,500 字符的短摘要。

三类日志均在写入前脱敏，处理已知密钥、Bearer/Basic、URL 中的凭据和签名参数。插件统一使用 `logging.getLogger(__name__)`，不自行添加文件 handler。扩展刷新会登记新凭据，并保留旧凭据的脱敏能力，以覆盖仍在执行的旧运行。

已知凭据来自模型密钥、语义密码、认证 token，以及 MCP 中明确的凭据环境变量、请求头和参数（如 `API_KEY`、`Authorization`、`--token`、`--password=<value>`）。用户名、服务 URL 和普通命令参数保留用于诊断。短于 8 个字符的凭据只按完整值匹配，避免替换普通单词或路径中的同名片段；凭据字段和 URL 内认证信息仍会脱敏。

轮转大小统一为 5_000_000 字节，保留 3 个备份；日志文件模式为 `0600`。后端日志文件无法初始化时，降级为同样脱敏、格式化的标准错误输出。写入失败会提示一次，避免日志错误掩盖原始异常。

单独启动 `dataagent serve`、Web 的 `npm start` 或 TUI 时，各服务直接写对应文件。由启动器拉起时，内部启动标记 `DATAAGENT_LOG_CAPTURE` 让子进程只输出日志，启动器通过同一日志写入器落盘，避免重复记录。该标记由启动器管理，不应手动设置。TUI 对后端 stderr 的短摘要只用于错误展示，完整内容仍写入 `backend.log`。

浏览器通过同源 `/api/client-errors` 上报页面异常、未处理 Promise 和客户端通信错误，最终进入 `web.log`。客户端对重复报告去重，服务端限制上报频率与正文大小。网络不可用时，上报可能失败，界面继续运行。runtime 失败的完整诊断以 `backend.log` 为准，客户端展示安全摘要。

浏览器上报每个 Web 进程每分钟最多写入 60 条记录、接受 1 MiB 的有效上报正文，单次正文最大 32 KiB；所有来源共享额度，客户端 `x-forwarded-for` 不参与配额分组。允许的 Origin 依次取 `DATAAGENT_WEB_ORIGIN`、Next.js 内部监听 Origin 或请求 URL 的 Origin。反向代理或通配地址部署时，将 `DATAAGENT_WEB_ORIGIN` 设为浏览器实际使用的 Origin。校验不直接读取客户端提交的 Host 或转发头。Origin 和 Fetch Metadata 用于浏览器来源校验，不作为客户端身份认证；浏览器自报的运行 ID 仍是不可信诊断数据。

### 日志级别与入口边界

| 入口 | 默认行为 |
| --- | --- |
| `dataagent serve`、`serve-a2a` | Python 根日志 INFO；DEBUG 不落盘，WARNING/ERROR 和异常诊断保留 |
| TUI | 默认 INFO；代码里的 `setDebugMode` / `setLogLevel` 可调整界面日志，当前没有对应的公开 CLI 或环境变量 |
| Web 与启动器 | 共享写入器记录调用方指定的级别；Web console 的 log/info、warn、error 分别映射 INFO、WARN、ERROR |
| SDK 直接调用 | 不自动配置文件日志或捕获异常；自建宿主可显式使用 `configure_logging` |

当前没有统一的 `LOG_LEVEL` 配置项。需要临时查看 Python DEBUG 时，由开发在自己的宿主完成日志初始化后调整 logger 级别，重启官方 CLI 会恢复 INFO；不要把它当作已支持的部署参数。WARNING 是 Python 的名称，WARN 是 Node 的名称，检索时同时考虑。

<a name="a2a-correlation"></a>

## A2A 调用关联

`/a2a/jsonrpc` 的接收、完成、取消和失败事件通过同一后端写入器记录为每行一个 JSON 对象。关联字段包括 `request_id`、`rpc_id`、`message_id`、`context_id`、`task_id`、`root_run_id`、`instance_id` 和进程 `user_id`，均保留在 JSON 顶层。匿名访问不识别实际访问者，`user_id` 不能作为调用方身份。

主要事件是 `a2a.request.received/finished/aborted/rejected/failed`、`a2a.assembly.queued/started/completed/failed/cancelled`、`a2a.task.cancel_requested`、`a2a.checkpoint.saved/unchanged/failed`、`a2a.cleanup.failed`、`a2a.trace.saved/failed`。HTTP 完成记录区分 `http_status`、`rpc_error_code` 和 `task_state`，HTTP 200 不等于任务成功。

失败记录带 `error_code`、`stage`、脱敏安全摘要和 `frames`（最多 12 个异常栈帧位置），不记录局部变量、完整请求体、模型提示或工具结果。按字段脱敏和限长，长错误不会截掉整条 JSON 或后面的关联号。普通异常日志的 `stack` 和 Trace 可补充诊断，不能假设每条 A2A 审计都有完整堆栈。

先按响应头 `X-Request-ID` 或 Task ID 搜索日志，再用 `root_run_id` 找到 `a2a.trace.saved` 中的 `native_run_id` 和 `trace_file`。会话索引只保留最后的轮次和 checkpoint，历史调用关系依靠保留期内的日志与 trace。

<a name="trace"></a>

## Trace

启用追踪时，Agent 根执行结束后尝试写入本地 OTLP JSON。文件路径、字段、终止时的数据完整性和请求 ID 的对应关系见[执行 Trace](execution-traces.md)。运行结果和业务数据分开核对。

要把 Trace 发送给 Collector 或评测平台，见[实时导出](../../zh/guides/trace-export.md)和[用 Trace 做评测](../../zh/guides/trace-evaluation.md)。

## 流上的标识

SSE 的 `RUN_FINISHED.metadata.durationMs` 只表示服务端这一轮的耗时。它不写入 Checkpoint。TUI 的完成行同样只存在于界面。
