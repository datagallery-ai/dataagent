# 能力全览

这篇文档面向评估 DataFoundry 能力范围的用户。读完后，你可以判断 Web、TUI 和 API 分别能做什么，以及哪些能力受后端 capability 或外部资源配置影响。

状态判断以当前代码为准：

| 状态 | 判断方式 |
| --- | --- |
| 可直接试用 | 本地启动后，配好模型 Key，用内置 DTC Growth Review 可以跑通。 |
| 需要配置 | 功能入口已接入，需要你提供模型 Key、数据库凭据、文件、MCP Server 或 Skill package。 |
| 受 capability 控制 | 读取 `GET /api/v1/capabilities`，按返回值启用或隐藏相关入口。 |
| 本地开发边界 | 本地默认身份和默认 workspace 可用；Web 可切换本地开发用户用于隔离验证。 |
| 密码认证边界 | 内置 password 模式覆盖账号注册、登录、重置、会话 Cookie 和 CSRF；生产部署仍需 Secret 管理、审计导出、访问控制策略和运维监控。 |

## 总览

| 能力 | Web 工作台 | TUI | 后端/API | 检查方式 |
| --- | --- | --- | --- | --- |
| 自然语言数据分析 | 可直接试用 | 可直接试用 | 可直接试用 | 配好 LLM Key，使用 `dtc-growth-demo` 提问。 |
| 内置 DTC 增长案例 | 可直接试用 | 可直接试用 | 可直接试用 | 数据源列表包含 `DTC Growth Review`；每个用户获得只读 workspace 副本。 |
| 数据源注册与测试 | 可直接试用 | 可选择已配置数据源 | 可直接试用 | `GET /api/v1/datasource-types`，`POST /api/v1/datasources/:id/test`。 |
| schema 抓取与表预览 | 可直接试用 | 通过 Agent 工具查看结果 | 可直接试用 | `POST /api/v1/datasources/:id/introspect`，`GET /schema`，`GET /tables/:table/preview`。 |
| 只读 SQL 分析 | 可直接试用 | 可直接试用 | 可直接试用 | Agent run 先检查 schema，再通过工具执行查询。 |
| 模型配置 | 需要配置 | 使用服务端模型配置 | 需要配置 | `.env` 或 `/api/v1/model-profiles`。 |
| 模型连接测试 | 可直接试用 | 使用服务端模型配置 | 可直接试用 | `POST /api/v1/model-profiles/:id/test`。 |
| 分析追溯 | 可直接试用 | 可直接试用 | 可直接试用 | 查看步骤、工具调用、run events 和 SQL audit。 |
| 语义 Trace DAG | 可直接试用 | 无图形视图 | 可直接试用 | 打开 Web trace graph，或 `GET /api/v1/sessions/:id/trace-dag`。 |
| Artifact 产出 | 可直接试用 | 可查看会话产出 | 受 capability 控制 | `artifact.list`、`artifact.export`、`artifact.promote`。 |
| 会话历史 | 可直接试用 | 可用 `/resume` 恢复 | 受 capability 控制 | `conversation.memory`、`conversation.title`。 |
| 并发会话与提问队列 | 可直接试用 | 单个活跃终端流 | 可直接试用 | 在不同会话启动 run；Web run 执行中再提交问题。 |
| Checkpoint 分支 | 可直接试用 | 无分支控件 | 可直接试用 | 从早期问题重问，或 `POST /api/v1/sessions/:id/branches`。 |
| 证据引用 | 可直接试用 | 无选区 UI | 可直接试用 | 引用完整产出或选区，查看 `evidenceRefs` 诊断。 |
| Data Link 图 | 需要配置 | 无图形视图 | 需要配置 | 配置兼容的 Data Link MCP Server，再打开「Data Link」。 |
| 用户身份 | 本地开发用户切换和 password auth 界面 | 使用后端身份 | 可直接试用 | `GET /api/v1/me`、`/api/v1/dev/*`、`/api/v1/auth/*`。 |
| 工作区文件 | 可上传、查看、下载、删除、复用 | 通过 run_config 使用已启用文件 | 受 capability 控制 | `files`、`GET/POST /api/v1/files`、`POST /api/v1/files/:id/promote`。 |
| 对话附件 | 可直接试用 | 不提供附件上传命令 | 受 capability 控制 | `chat.fileUpload`，`POST /api/v1/chat/uploads`。 |
| 图片输入 | 输入组件受开关控制 | 不提供图片输入命令 | 受 capability 控制 | `chat.imageInput`。 |
| 知识库 | 需要配置 | 可随启用资源进入 run_config | 受 capability 控制 | `knowledge`、`kb.chunking`、`kb.citationPolicy`。 |
| MCP 工具 | 需要配置 | 可随启用资源进入 run_config | 受 capability 控制 | `mcp`、`mcp.stdio`、`mcp.toolPolicy`。 |
| Skill | 需要配置 | 可用 `/skill` 选择 | 受 capability 控制 | `skills`、`skill.resourceBinding`。 |
| 取消运行 | 可直接试用 | 无 slash 命令 | 可直接试用 | `POST /api/v1/runs/:id/cancel`。 |

## 后端 capability keys

`GET /api/v1/capabilities` 返回以下 key。客户端按这些 key 控制 UI、运行配置和资源入口：

| Key | 控制内容 |
| --- | --- |
| `artifact.export` | 产物导出。 |
| `artifact.list` | 会话产物列表。 |
| `artifact.promote` | 文件型产物加入工作区。 |
| `chat.fileUpload` | 对话附件上传。 |
| `chat.imageInput` | 图片输入。 |
| `conversation.memory` | 服务端会话记忆。 |
| `conversation.title` | 会话标题保存。 |
| `interaction.resume` | 刷新或切换会话后的人工交互恢复。 |
| `datasource.fieldMasking` | 数据源字段脱敏配置。 |
| `datasource.extendedTypes` | 扩展数据源类型。 |
| `datasource.introspectionPolicy` | schema 抓取策略。 |
| `datasource.queryPolicy` | 查询行数、超时和写入限制策略。 |
| `datasource.samplePolicy` | 样本预览策略。 |
| `datasource.server` | 服务端数据库连接字段。 |
| `files` | 工作区文件资产。 |
| `kb.chunking` | 知识库分块配置。 |
| `kb.citationPolicy` | 知识库引用策略。 |
| `kb.scope` | 知识库作用域。 |
| `llm.advancedSampling` | 模型扩展采样参数。 |
| `llm.samplingParams` | 模型采样参数。 |
| `knowledge` | Knowledge 资源进入运行时。 |
| `mcp` | MCP 资源进入运行时。 |
| `mcp.stdio` | stdio MCP Server 配置。 |
| `mcp.toolPolicy` | MCP 工具策略。 |
| `skill.resourceBinding` | Skill 资源绑定。 |
| `skills` | Skill 资源进入运行时。 |

## Web 工作台

Web 工作台适合本地演示和日常分析：

- 左侧管理会话和工作区资源。
- 中间展示对话、步骤卡片和人工确认。
- 右侧展示概览、语义追溯、产出、可引用证据的步骤详情和工作区文件。
- 输入框支持模型选择、资源开关、`@` 提及、附件、证据 chip、提问队列和停止运行。
- 会话列表通过服务端 `/api/v1/sessions` 恢复历史；早期问题和 checkpoint 可创建持久化分支。
- 配置兼容的 MCP 服务后，Data Link 可打开工作区图。

详见 [Web 工作台指南](guides/web-workbench.md)。

## TUI

TUI 适合远程服务器和终端工作流：

- 支持以 Chat 为主界面的终端 UI，并提供独立 `/outputs` 页面。
- 支持 `/datasource` 选择数据源。
- 支持 `/skill` 选择 Skill。
- 支持 `/resume` 恢复服务端历史会话。
- 支持密码登录与本地 Session 缓存（需连接运行中的 API）。
- 支持 `Tab` 命令补全、输入历史和 Chat 视图滚动。

当前注册命令以 [TUI 指南](guides/tui.md) 为准。

## API 与集成

后端提供两类入口：

| 入口 | 用途 |
| --- | --- |
| `POST /api/copilotkit` | 启动 Agent run，返回 AG-UI 事件流。 |
| `/api/v1/*` | 管理资源、文件、会话、产出和配置。 |

集成方应通过配置 API 管理资源，通过 Agent Runtime 启动分析。数据源凭据只在资源创建或更新时提交。

## 安全边界

- 客户端不能把数据库密码、模型 API Key、MCP Token 放进 Agent run body。
- 读接口不返回明文凭据。
- SQL 执行经过只读限制、行数限制、超时和审计。
- 本地开发身份只用于试用和开发集成。
- password auth 负责用户会话；生产部署仍需 Secret 管理、审计导出、访问控制策略和运维监控。

继续阅读：[安全说明](security.md)。
