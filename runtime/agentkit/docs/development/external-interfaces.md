# 对外接口总览与优化清单

这篇给改对外接口的人。它列出 DataAgent 的全部对外接口，给出统一规则，并记录当前问题和处理顺序。

目标：对外接口简洁、直观、有逻辑，接口之间不冲突，用户理解成本低。

每个问题都带代码位置，路径相对仓库根目录。行号以 2026-11-13 的 `deepagents_refactor_new` 为准。标“待确认”的条目需要再查一次。

## 1. 接口分类

| 类别 | 调用方 | 内容 | 主要代码 | 参考文档 |
| --- | --- | --- | --- | --- |
| HTTP 服务 | Web、TUI、第三方前端 | AG-UI 对话流、会话、产物、取消、健康检查、`/api/v1/me` | `serve/` | [HTTP 接口](../reference/rest-api.md)、[事件](../reference/events.md) |
| A2A 服务 | 其他 Agent | Agent Card、JSON-RPC；出站调用远端 Agent | `a2a_server/` | [A2A](../reference/a2a.md) |
| Python SDK | 嵌入式集成方 | `prepare_runtime`、`create_agent`、`Agent`、`ModelContext`、错误类型 | `dataagent/__init__.py` | [SDK](../guides/sdk.md)、[Python API](../reference/python-api.md) |
| 扩展声明 | 插件开发者 | `.plugin.json`、工具、Hook、Skill、子代理、`.mcp.json` | `dataagent/declarations.py` | [扩展声明](../reference/extension-schema.md)、[MCP 配置](../reference/mcp-configuration.md) |
| 配置、CLI 和环境变量 | 运维、部署、终端用户 | `config.json`、`.env`、`dataagent serve`、`serve-a2a`、`history_sql` | `dataagent/settings.py`、`serve/__main__.py` | [配置字段](../reference/configuration.md)、[命令与环境变量](../reference/cli-and-environment.md) |

本文中的代码路径默认在 `runtime/agentkit/` 下，其他路径写全。

`builtin-plugins/` 下的 de-agent、nl2sql 是外部插件的示例，不属于 DataAgent 的对外契约。插件自己的配置键、默认值和环境变量由插件作者和用户决定。本文只在插件暴露出核心接口的问题时引用它们。

## 2. 统一规则

后续新增或修改对外接口时按这些规则执行。第 4 节的问题大多是违反了其中一条。

| 编号 | 规则 |
| --- | --- |
| R1 | 一个概念只用一个名字。别名只在协议强制时保留，并在第 3 节的术语表中登记。 |
| R2 | JSON 字段命名：HTTP 和事件用 camelCase；`config.json` 和扩展声明用 snake_case。`.mcp.json` 的 `mcpServers` 是兼容 Claude 格式的唯一例外。 |
| R3 | 时长字段统一以 `_seconds` 结尾，值为秒。 |
| R4 | 字段值是文件路径时，字段名以 `_file` 或 `_path` 结尾。没有后缀的字段放正文。 |
| R5 | 所有 ID 用同一个正则，并带 `^$` 锚点和长度上限 64。 |
| R6 | 错误只有一种格式：HTTP 用 `{"success": false, "error": {"code", "message"}}`；事件流 `RUN_ERROR.code` 用同一套大写枚举；Python 异常都继承 `DataAgentError`。 |
| R7 | 同一设置的来源优先级统一为：CLI 参数 > 进程环境变量 > `.env` > `config.json` > 默认值。 |
| R8 | 每个对外入口只有一条推荐路径。底层入口放到单独模块，不进 `__all__`。 |
| R9 | 每种声明文件都有 `schema_version`。版本不符时，报错要说明怎样升级。 |
| R10 | 能力没有实现时，客户端不显示入口。服务端对未实现的路由返回 404，客户端不把 404 当成功。 |

## 3. 术语表

| 概念 | 统一叫法 | 现有别名 | 处理 |
| --- | --- | --- | --- |
| 本机用户 | user（`--user`、`user_id`） | profile（`serve --user` 的 help、报错文案） | 删除 profile 叫法 |
| 会话 | 对外 `threadId`，Python 内部 `thread_id` | SDK `session_id`、CLI `--session`、Web `sessionId`/`id`、A2A `contextId` | A2A 按协议保留 `contextId`，文档写明它等于 `threadId`；其他统一 |
| 一轮运行 | `runId` | A2A `taskId` | A2A 按协议保留，文档写对照 |
| 产品名 | DataAgent | FastAPI 标题 "DataGallery API"（`serve/app.py:620`）、"AgentKit data agent"、artifact 名 `agentkit_result` | 对外统一为 DataAgent |
| 启动快照 | 待定，建议 `PreparedRuntime` | `dataagent.Runtime`，与 LangGraph `Runtime`、`ToolRuntime` 同名 | 改名 |
| 工具和 Hook 的上下文 | 待定，建议 `AgentContext` | `ModelContext`（实际承载模型、语义客户端、插件配置、Home、会话） | 改名 |
| 完成状态 | `completed` | 会话状态 `complete` | 统一 |
| A2A 出站 | `remote_agents` | `config.json` 的 `a2a.agents` | 与入站 `serve-a2a` 区分 |

## 4. 问题清单

优先级：

- **P0**：转测前处理。修正错误行为或误导性文档，不改变契约。
- **P1**：本版本契约冻结前处理。会改变契约，但现在改成本最低。
- **P2**：下一版本处理。改名或重组。

### 4.1 已完成（datagallery/dataagent!651）

- 所有 HTTP 错误统一为错误信封，错误码沿用 Web 的 `ApiErrorCode`。
- 对外错误文案统一为英文：HTTP 接口、`DataAgentError` 默认文案、`history_sql` 报错。
- `rest-api.md` 补全错误码、`POST /runs/{runId}/cancel` 和 A2A 路由说明。

### 4.1.1 P0 处理结果（2026-11-13）

| 编号 | 结果 |
| --- | --- |
| H1 | 已修。产品文档示例补全 `id`、`tools`、`context`、`forwardedProps`，并用 `QueryInput` 校验通过；三份 `rest-api.md` 的约束改为“必须出现且为空” |
| H2 | 复核后改为待决策，见 H2 行 |
| H3 | 已修。删除 `deploy/nginx.datagallery.conf.example` 及其引用：没有部署脚本、测试或 CI 使用它，内容多次与代码不一致。SSE 响应自带 `X-Accel-Buffering: no`，Web 转发时另加 `Cache-Control: no-transform`；nginx 按这个头自动关闭 SSE 缓冲 |
| S1 | 已修。MCP 报错按调用入口写 `create_agent` 或 `build_graph` |
| S2 | 已修。`create_agent` 改为显式关键字参数，拼错立即 `TypeError`；`python-api.md` 同步 |
| E1 | 待决策：`validate_deliverables` 工具是否恢复。见 E1 行 |
| E4 | 锚点已加。workspace 名称正则加 `^$`。四套正则的统一仍是 P1 |
| C1 | 端口已修。新增 `dataagent.server_listen`，降级启动时也读所选配置的 `server` 段。“缺少环境变量时 models 段降级、其他段退出”的差异已写进 `cli-and-environment.md`，行为未改，转为 P1 |
| C2 | 已修。`.env.example`、`README.md`、`README_zh.md` 改为“网页上不能改模型”，并说明根 `.env` 覆盖 Home `.env`。根 `.env` 仍是文档推荐的模型配置位置，所以保留模型变量 |
| C3 | 不处理。插件是示例，模型别名由用户在插件配置中指定 |
| C4 | 已修。`configuration.md` 列全 `timeout_seconds` 的用途、补 `a2a` 行、写明 `output_root` 例外；`cli-and-environment.md` 增加 `history_sql` 命令、`DATAAGENT_HISTORY_SQL_CORPUS`，说明“进程环境”变量不读 `.env`，改正 `--user`、`--workspace`、`--session` 的说明 |
| 4.6 | 已修，见 4.6 节。`load_a2a_tools` 是内部函数，不导出 |

### 4.1.2 P1 处理结果（2026-11-14）

本轮只处理服务侧（HTTP、事件、A2A、CLI）。SDK 和扩展声明的改名（S3、S4、E2、E4 统一、E5）会影响插件作者，需要兼容别名，推迟到下一版本。

| 编号 | 结果 |
| --- | --- |
| H4 | 已修。取消后流以 `RUN_ERROR{code:"CANCELLED"}` 结束，会话仍为 `interrupted`。Web 把它当作 canceled；TUI 显示 “Stopped”，摘要为 interrupted。Web `cancelRun` 收到 404 时视为已停止 |
| H5 | 已修。新增 `dataagent.error_code` / `ERROR_CODES`，`RUN_ERROR.code` 只取 `TIMEOUT`、`CANCELLED`、`LOOP_DETECTED`、`LIMIT_EXCEEDED`、`INCOMPLETE_STREAM`、`CONFIG_ERROR`、`MODEL_ERROR`、`TOOL_ERROR`、`INTERNAL_ERROR`。服务端日志另记异常类名 |
| H6 | 已修。正在运行的 `runId` 再次用于 `POST /dataagent/stream` 返回 409，不论 `threadId`。cancel 的 404 含义写明为“不在运行”。A2A 运行不能经 cancel 停止，已写进文档 |
| H7 | 已修。删除 `conversation.status` 和 `/ready` 的 `control_plane`；`conversation.ready` 是唯一就绪标志。TUI 改为校验 `ready`。文档列全字段 |
| H8 | 已修。`DATAAGENT_A2A_AUTH_TOKEN`（进程环境或 `.env`）设置后，`serve` 和 `serve-a2a` 都对 `/a2a/jsonrpc` 校验 Bearer，用常量时间比较；401 改为错误信封 `UNAUTHORIZED`。令牌进入日志脱敏。未设置时行为不变；绑定非回环地址时不强制要求令牌 |
| C5 | 已修。`serve-a2a` 的 `--host`、`--port` 默认读 `server.*` |
| C6 | 已修。`--auth-token` 默认取 `DATAAGENT_A2A_AUTH_TOKEN` |
| E3 | 已修（文档）。`hooks.md` 写明两种拒绝方式和工具 Hook 取上下文的方法 |
| S5 | 已修，见 4.1.3：`LaunchOptions` 输入目录只留 `workspaces` |
| C8 | 关闭。名称保留，P0 已改正文档 |
| C7 | 关闭。核心的 `history_sql` 已是环境变量优先于配置，符合 R7 |
| C1 剩余 | 转 P2：缺少环境变量时 models 段降级、其他段退出的差异 |
| S3、S4、E2、E4、E5 | 推迟到下一版本（S3 已在 4.1.3 完成） |

### 4.1.3 Python SDK 分层（2026-11-14）

原则：对外只暴露一条高层路径，底层和宿主专用的接口放到单独模块。

| 层 | 模块 | 名称 |
| --- | --- | --- |
| 公开 SDK | `dataagent` | `LaunchOptions`、`prepare_runtime`、`Runtime`、`create_agent`、`Agent`、`ModelContext`、`load_mcp_tools`、`SemanticServiceClient`、`ConfigurationLoadError`、`DataAgentError`、`LoopDetectedError`、`safe_error` |
| 宿主接口 | `dataagent.host` | `refresh_extensions`、`extension_revision`、`server_listen`、`error_code`、`ERROR_CODES` |
| 低层接口 | `dataagent.lowlevel` | `build_graph` |
| 内部 | `dataagent.agent` | `load_extensions`（不再导出） |
| A2A 公开 | `a2a_server` | `A2ACaller`、`A2AResult`、`A2ACallError`；宿主用 `create_a2a_server`、`run_a2a_server` |

- 低层建图函数命名为 `build_graph`，名字说明它返回 LangGraph 图。
- `LaunchOptions` 只保留 `config`、`env_file`、`user_id`、`workspaces`、`cwd`、`init_home`、`session_id`；输入目录只有 `workspaces` 一种写法。
- 版本未正式发布，不保留兼容别名：包根不再提供上表以外的名称，`a2a_server` 只导出上表的 5 个名称。
- `serve/` 只从 `dataagent` 和 `dataagent.host` 导入，由 `test_layering.py` 检查；`test_public_sdk_exports_only_the_high_level_path` 固定三层的导出列表。
- 文档：`python-api.md` 按三层重写，`create_agent` 只列 SDK 参数，宿主参数（`session`、`input_workspaces`、`extension_revision`）和 `Agent.graph`/`graph_for`/`as_agui` 归入宿主接口；`sdk.md` 删除低层图和扩展刷新两节；扩展与参考文档不再要求用户调用 `build_graph`、`load_extensions`、`refresh_extensions`。
- CLI：`--reload`、`--stdio-ready`、`DATAAGENT_INSTANCE_ID` 从用户参数表移到“开发和内部参数”。
- 顺带修正：`plugins.md` 写“配置里的 `server` 不决定监听地址”，与实现相反，已改正。

### 4.2 HTTP 与 A2A 服务

| 编号 | 优先级 | 问题 | 证据 | 建议 |
| --- | --- | --- | --- | --- |
| H1 | P0 | 产品文档的 `POST /dataagent/stream` 请求示例会返回 422 | `docs/zh/reference/agent-runtime.md:14-22`、`docs/en/reference/agent-runtime.md:15-23` 缺 `messages[0].id`、`tools`、`context`、`forwardedProps`；`QueryInput` 要求这些字段（`serve/app.py:73-98`） | 补全示例。或给这些字段默认值 |
| H2 | 待决策 | Web 调用约 70 个后端不存在的 `/api/v1/*` 路由。复核结果：会话改名、删除和分支在界面上没有入口（`SessionListItem` 不使用 `onRename`/`onDelete`；分支依赖 `/api/v1/sessions/{id}/conversation`，它返回 404），所以“删除后又出现”不会发生；`/login` 只能直接输入地址打开。界面上能触发的有：Trace 图（显示加载失败），以及 `workspaceResourceNavEnabled = true` 打开的左侧资源栏：数据源、知识库、Skill、DataLink、查询历史、任务 | `apps/web/src/lib/config-api/client.ts`；`apps/web/src/app/data-tasks/data-tasks-app.tsx:352,4707`；`apps/web/src/app/data-tasks/components/task-console/TraceOverlay.tsx:185`；后端只有 `/api/v1/me`（`serve/product.py:49`） | 先确定本版本 Web 发布哪些面板，再隐藏其余入口、删除不可达的 client 方法。这是发布范围决策，不是单点修复 |
| H3 | 已修 | 部署示例的后端端口是 8787，代码默认 8790；nginx 把 A2A 路由转给 Web，Web 没有对应代理，A2A 返回 404 | 原 `deploy/nginx.datagallery.conf.example:9,18`；`dataagent/settings.py:138` | 删除该示例 |
| H4 | 已修 | 取消后流直接结束，不发终止事件。客户端无法区分取消和断网 | `serve/app.py:557`；Web 遇到无终止事件时报 `INCOMPLETE_STREAM`（`apps/web/src/lib/stream-http-agent.ts:89-91`） | 取消时发 `RUN_ERROR{code: "CANCELLED"}` |
| H5 | 已修 | `RUN_ERROR.code` 不是稳定枚举：取值是 Python 类名或字面量 `RUN_ERROR`；`INCOMPLETE_STREAM` 放在 message 里；`SUBAGENT_ERROR` 没有 code | `serve/app.py:276-284,530`；`dataagent/diagnostics.py:34` | 定义 `TIMEOUT`、`CANCELLED`、`INCOMPLETE_STREAM`、`MODEL_ERROR`、`TOOL_ERROR`、`INTERNAL_ERROR`，写进 events.md |
| H6 | 已修 | `runId` 按全局登记且直接覆盖，两个会话用同一个 `runId` 时先跑的无法取消；404 同时表示路由不存在、run 未知、run 已结束；A2A 的运行无法通过 cancel 停止 | `serve/app.py:207-209,455`；`serve/product.py:62`；`a2a_server/server.py:96` | 已在运行的重复 `runId` 返回 409；已结束返回幂等 204；A2A 运行也登记 |
| H7 | 已修 | `/healthz` 和 `/ready` 字段风格混用且冗余：`control_plane` 是 snake_case，值恒为 `ready`；状态在三处重复表达；文档漏了 `status`、`protocol`、`instanceId`，但 TUI 把它们当必填 | `serve/product.py:32,40,47`；`docs/en/reference/rest-api.md:25`；`apps/tui/src/connect.ts:108` | 删除 `control_plane` 和重复状态，补全文档 |
| H8 | 已修 | A2A 鉴权与启动方式有关：`serve` 上全部路由无鉴权，`serve-a2a` 支持 `--auth-token`；`server.host` 允许 `0.0.0.0`；`/healthz` 返回 Home 绝对路径 | `serve/__main__.py:123`；`dataagent/settings.py:145`；`a2a_server/server.py:114-131`（401 响应体还是 `{"detail"}`） | `serve` 也支持同一个 token；绑定非回环地址且无 token 时拒绝启动；401 改用错误信封 |
| H9 | P2 | 路由前缀不统一：发起在 `/dataagent/stream`，取消在 `/runs`，读取在 `/sessions`，版本前缀下只有 `/api/v1/me` | `serve/app.py:382-430`；`serve/product.py:49,57` | 统一为 `/api/v1/sessions`、`/api/v1/sessions/{threadId}/runs`（POST 返回 SSE）、`.../runs/{runId}/cancel`；旧路径保留一个版本 |
| H10 | P2 | 成功响应格式混用：`/healthz`、`/ready`、`/api/v1/me` 用信封，`/sessions*` 和产物接口返回裸 JSON；客户端要写两套解析 | `serve/product.py:40-55`；`serve/app.py:386,408,427`；`apps/web/src/lib/config-api/client.ts:104,166` | 随 H9 一起统一；之后删除客户端对 `detail` 的兼容 |
| H11 | P2 | 会话存在性判断不一致：不存在的会话在详情和预览返回 404，在产物列表返回 200 空列表；扩展重载失败（服务端故障）返回 409 | `serve/app.py:341,397-399` | 不存在 → 404；非法 → 400；服务端故障 → 5xx |
| H12 | P2 | 产物列表返回相对路径 `notes.txt`，预览返回 `/output/notes.txt`；截断只体现在正文末尾 | `serve/outputs.py:59,80-81` | 都返回相对路径；增加 `truncated` 字段 |
| H13 | P2 | catch-all 把错误方法伪装成 404；405 映射为 `BAD_REQUEST`；探活不支持 HEAD；OpenAPI 中出现 `/{path}`，缺少 stream、sessions 和 A2A | `serve/product.py:69`；`serve/envelopes.py` | catch-all 设 `include_in_schema=False`；405 映射 `METHOD_NOT_ALLOWED`；健康检查支持 HEAD |
| H14 | P2 | 引擎未就绪时，cancel 返回 404，其他对话路由返回 503；Agent Card 也返回 503 | `serve/product.py:12-21` | cancel 纳入就绪检查；Agent Card 改为静态 |

### 4.3 Python SDK

| 编号 | 优先级 | 问题 | 证据 | 建议 |
| --- | --- | --- | --- | --- |
| S1 | P0 | 配置了 MCP 时，`create_agent` 不会自动加载；报错让用户“传给”低层建图函数，而用户调用的是 `create_agent` | `dataagent/agent.py:65-68`；`dataagent/client.py:223-225` | 报错按入口给文案；`sdk.md` 主示例写出 MCP 用法 |
| S2 | P0 | `create_agent(**kwargs)` 不校验参数名，拼错要到首次访问 `agent.graph` 才报错；文档说它会转发 `session`，代码把它过滤了 | `dataagent/client.py:243-245`；`docs/reference/python-api.md:70` | 改为显式关键字参数 |
| S3 | 已修 | 入口太多，没有唯一主路径：`create_agent`、`Agent`、低层建图函数、`load_extensions`、`load_mcp_tools` 同时从包根导出；建图函数返回图，`create_agent` 返回句柄，名字却相近 | `dataagent/__init__.py:34-51` | 主路径固定为 `prepare_runtime → create_agent → Agent`；建图函数命名为 `build_graph` 并放在 `dataagent.lowlevel`；`load_extensions` 移出 `__all__` |
| S4 | P2 | 错误类型没有统一基类：`DataAgentError(Exception)`、`LoopDetectedError(RuntimeError)`、`ConfigurationLoadError(ValueError)`、`A2ACallError(RuntimeError)`，配置错误多数直接抛 `ValueError`；nl2sql 复制了一个同名 `DataAgentError` | `dataagent/errors.py:164`；`dataagent/extensions/loop_detection.py:55`；`dataagent/bootstrap/config_files.py:33`；`builtin-plugins/nl2sql/nl2sql/errors.py:177` | 全部继承 `DataAgentError`；nl2sql 复用核心类 |
| S5 | 已修 | `LaunchOptions.session_id` 只恢复 workspace 绑定，不是默认会话；默认会话固定为 `"sdk"`；workspace 有多种写法 | `dataagent/bootstrap/startup.py:84-90`；`dataagent/models.py:104` | `session_id` 改为默认会话，或从 SDK 移除；workspace 只留一种写法 |
| S6 | P2 | `Runtime` 和 `ModelContext` 命名误导，见第 3 节 | `dataagent/bootstrap/runtime.py:18`；`dataagent/models.py:76-161` | 按术语表改名 |
| S7 | P2 | 内置插件依赖非公开模块 `dataagent.model_stream` 和 `dataagent.history_sql.config`，说明公开 API 不够用 | `builtin-plugins/de-agent/tools/deliverables.py:28`；`builtin-plugins/de-agent/hooks/history_sql.py:8` | 导出确实需要的能力，或改用公开 API |
| S8 | P2 | `SemanticServiceClient` 方法签名风格不统一：参数顺序、`limit` 和 `top_k` 混用；构造函数暴露 `_shared`；`verify` 默认 False | `dataagent/semantic_client/client.py:40,251,276` | 统一为 `(database, ..., *, limit)`；补全文档 |

### 4.4 扩展声明

| 编号 | 优先级 | 问题 | 证据 | 建议 |
| --- | --- | --- | --- | --- |
| E1 | 待决策 | de-agent 的一条 Hook 永远不会触发：matcher 写的是 `validate_deliverables`，这个函数还在 `tools/deliverables.py:32`，但 commit 4f717b50 从清单 `tools` 中删除了它的注册，之后 ac4b5b08、cb69853d、1528d1e3 仍在修改这个函数，删除注册可能是误操作；即使存在，注册名也是 `de-agent__validate_deliverables`；多个 Skill 仍让模型调用它 | `builtin-plugins/de-agent/.plugin.json:111`；`dataagent/extensions/hooks.py:119`；`dataagent/extensions/tools.py:22` | 由插件负责人确定：恢复 `validate_deliverables`（matcher 改为 `de-agent__validate_deliverables`），或删除这条 Hook 并把两个 dataops Skill 改指 `check_and_repair_sql`。编译时校验 matcher 放到 P1：matcher 也可能指向 MCP 和原生工具，需要先定规则 |
| E2 | P2 | 两套 Hook 共用 `hooks` 键，但事件名、声明位置和签名都不同：Agent Hook 有 6 个事件，写在 `.plugin.json` 或 `dataagent.hooks`；Run Hook 只有 `before_run`/`after_run`，写在 `plugins.config.<id>.hooks`，只在含 compiled 子代理的插件中生效，其他插件写了被静默忽略 | `dataagent/declarations.py:92-99,226-230`；`dataagent/bootstrap/discovery.py:209-227`；`dataagent/extensions/subagents.py:93-104` | Run Hook 移到 `.plugin.json` 或子代理声明，键名改为 `run_hooks`；没有消费者时告警 |
| E3 | 已修 | Hook 契约不完整：工具 Hook 签名不带 runtime，内置插件靠 `request.runtime.context` 取上下文，文档未写；文档对“怎样拒绝执行”前后矛盾 | `builtin-plugins/de-agent/hooks/history_sql.py:20-24,43`；`docs/extensions/hooks.md:3,41-51,72` | 写明两种拒绝方式：返回错误 `ToolMessage`（模型可以纠正）；抛异常（终止本轮） |
| E4 | P2 | ID 正则有四套，workspace 的没有锚点（`'../../evil name!'` 能通过校验） | `dataagent/settings.py:41,108`；`dataagent/declarations.py` 的 `RESOURCE_ID_PATTERN`；插件工具 ID 不允许 `-` | 按 R5 统一；workspace 正则先加锚点（P0） |
| E5 | P2 | 工具命名空间格式不一致：插件工具 `<plugin>__<tool>`，A2A `a2a__<id>`，MCP `mcp__<scope>__<server>_<tool>`（最后一段是单下划线）；完整规则只在 `examples/mcp/README.md` 中出现 | `dataagent/extensions/tools.py:22`；`dataagent/extensions/mcp.py:31` | MCP 改为双下划线；在 extension-schema 中列出命名规则表 |
| E6 | P2 | 路径字段命名不对称：`description` 是正文、`description_file` 是路径，`system_prompt` 却是路径；清单的 `config` 是配置片段路径，和 `config.json` 同名 | `dataagent/declarations.py:161-164,239-240` | 按 R4 改为 `system_prompt_file`、`defaults_file` |
| E7 | P2 | 版本管理不统一：`.plugin.json` 是 2，`config.json` 是 3，子代理 JSON 和 `.mcp.json` 没有版本；全部 `extra=forbid`，旧运行时会拒绝新字段，报错只有 "Input should be 2" | `dataagent/declarations.py:234`；`dataagent/settings.py:188` | 按 R9 处理；文档写明省略时的默认值 |
| E8 | P2 | 编译型子代理工厂的 `services` 与 `services.context` 内容重复，类型未导出，文档漏了 `context` 和 `run_hooks` | `dataagent/extensions/subagents.py:56-72,117` | 签名改为 `factory(params, context)` 并导出类型 |

### 4.5 配置、CLI 和环境变量

| 编号 | 优先级 | 问题 | 证据 | 建议 |
| --- | --- | --- | --- | --- |
| C1 | P0 | 降级启动时忽略 `server.port`，固定监听 8790；Web 和 TUI 仍按配置端口连接。缺少环境变量时，models 段降级启动，其他段直接退出，两种处理不一致 | `serve/__main__.py:140-152`；`dataagent/bootstrap/config_files.py:103-107,253`；`scripts/server-listen.mjs:68-74` | 配置能解析时用解析出的地址；统一缺少环境变量时的处理 |
| C2 | P0 | 仓库根 `.env.example` 写“可以在网页上配置模型”，产品文档写“网页上改不了模型”；它还预填 `LLM_MODEL=qwen-plus`，经 `npm run start` 载入后会覆盖 Home `.env` | `.env.example:1-3`；`docs/zh/reference/configuration-api.md:5`；`scripts/stack-runner.mjs:39,113-115`；`dataagent/bootstrap/environment.py:67` | 改正注释，删除预填值；根 `.env` 只放启动脚本变量 |
| C3 | 不处理 | 示例插件把模型别名写为 `primary`，和 `dataagent.model.default` 无关联 | `builtin-plugins/de-agent/config.json:6,14,18,30,57,72` | 插件配置由用户指定，不属于核心契约。用户改了模型 ID 时，需要同时改插件配置 |
| C4 | P0 | 文档遗漏：`limits.timeout_seconds` 实际还控制 shell、MCP 和 A2A 的超时；`python -m dataagent.history_sql` 没有文档；插件环境变量只读进程环境，不读 Home `.env` | `dataagent/extensions/filesystem_backend.py:16`；`dataagent/extensions/mcp.py:26-36`；`a2a_server/client.py:170-172`；`dataagent/history_sql/__main__.py` | 补进 `configuration.md` 和 `cli-and-environment.md` |
| C5 | 已修 | 监听地址三处配置方式不同：`serve` 只认 `server.*`；`serve-a2a` 只认 `--host`/`--port`，默认 9999；Web 只认 `--web-host`/`--web-port`。文档称 `server` 是唯一监听地址 | `serve/__main__.py:121-122,149-153`；`scripts/stack-runtime-config.mjs:41-50`；`docs/reference/configuration.md:17` | `serve-a2a` 默认读 `server.*`，CLI 参数覆盖；或并入 `serve` |
| C6 | 已修 | `--auth-token` 只能明文写在命令行，会出现在 `ps` 和 shell 历史中 | `serve/__main__.py:123` | 增加 `DATAAGENT_A2A_AUTH_TOKEN` 或 `--auth-token-file` |
| C7 | 关闭 | 来源优先级各处不同：`history_sql` 环境变量优先，`dataops` 配置优先；（dataops 属于示例插件，只作对照） | `dataagent/history_sql/config.py:9`；`builtin-plugins/de-agent/utils/dataops_mcp.py:131-134`；`builtin-plugins/de-agent/config.json:33` | 核心的 `history_sql` 按 R7 统一 |
| C8 | 关闭 | `serve --session` 只采用该会话的 workspace 绑定，文档写“恢复会话” | `serve/__main__.py:100`；`docs/reference/cli-and-environment.md:22` | 改名为 `--bind-session` 或删除；改正文档 |
| C9 | P2 | 超时键名和单位不统一：`timeout_seconds`、`semantic.timeout`、`dataops.timeout_s`、`poll_interval_ms`、`DATAOPS_MCP_TIMEOUT`、`DATAOPS_REQUEST_TIMEOUT_SEC` | `dataagent/settings.py:28,112,174`；`builtin-plugins/de-agent/config.json:31-32` | 按 R3 统一，旧名告警兼容 |
| C10 | 不处理 | 插件配置命名混乱：模型别名有 `model` 和 `llm_model` 两种写法；开关有四种写法；`thresholds.null_rate_threshold` 后缀重复；代码会读的 `quality_gate`、dataops 的 `url` 等键默认文件里没有；nl2sql 接受 `semantic` 键但没有代码读取 | `builtin-plugins/de-agent/config.json:6,14,44-48`；`builtin-plugins/nl2sql/nl2sql/config_manager.py:50,62` | 示例插件的配置命名不属于核心契约，留给插件作者 |
| C11 | P2 | 结构小问题：`dataagent.model` 是只有 `default` 一个键的组；顶层键 `dataagent` 与产品名重复；`a2a.agents` 与入站 A2A 同名 | `dataagent/settings.py:23-24,121-124` | `dataagent.model` 改为字符串；`a2a.agents` 改为 `remote_agents` |
| C12 | P2 | `history_sql` 不是 `dataagent` 子命令，只接受 `--config`；语料目录有三处设置 | `dataagent/history_sql/__main__.py:22-31`；`pyproject.toml:40` | 改为 `dataagent history-sql`，复用通用参数 |
| C13 | 待确认 | `DATAAGENT_HOME=""` 在 Node 和 Python 两端处理不同；`npm run start` 只转发 `--config`，其他参数被静默忽略；`builtin-plugins/attribution-analysis` 没有 `.plugin.json` 却被当作插件候选 | `scripts/server-listen.mjs:20-21`；`dataagent/bootstrap/paths.py:149-152`；`scripts/stack-runtime-config.mjs:53-61` | 确认后再定 |

### 4.6 文档缺口

这些只改文档，都是 P0：

- `events.md` 只写了 `SUBAGENT_*` 和子代理 `STEP_*`。实际还透传根节点 `STEP_*`、`REASONING_*` 和任意 `CUSTOM`（`serve/app.py:518`）。
- `python-api.md` 的导出表漏了 `ConfigurationLoadError` 和 `LoopDetectedError`，还引用了未导出的 `build_entry_graph`；`ModelContext` 签名漏了 `session=`；`get_plugin_state` 没有文档。
- `extension-schema.md` 的子代理表漏了 `kind`、`entrypoint`、`description_file`；`subagents.md` 漏了 compiled 子代理不能写 `filesystem_tools`。
- `semantic-service.md` 漏了 `search_entities`、`list_retrieval_tables`、`hybrid_table_columns`、`with_scenario`。
- `configuration.md` 顶层字段表缺 `a2a`；“相对路径以配置文件目录为基准”与 `output_root` 拒绝相对路径矛盾（`dataagent/bootstrap/paths.py:426-427`）。

## 5. 处理顺序

1. **转测前（P0）**：已处理，结果见 4.1.1。H2 和 E1 待决策。
2. **契约冻结前（P1）**：服务侧已处理，结果见 4.1.2。S3、S4、E2、E4、E5 改为 P2。
3. **下一版本（P2）**：按第 3 节术语表和 R1–R10 改名、重组。
