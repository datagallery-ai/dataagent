# Web 工作台指南

这篇文档面向使用浏览器试用 DataFoundry 的用户。读完后，你可以创建数据任务、选择资源、停止运行、查看追溯和产出，并恢复服务端会话。

## 启动方式

默认按**正式态**启动（`password` + `build` / `start`）。正式测试与真实生产启动命令相同，差别在根目录 `.env` 的邮箱与公网地址，见 [快速开始](../quick-start.md)。

```bash
npm run build && npm run build:web
npm run start:api
npm run start:web
```

打开：

```text
http://127.0.0.1:3000/login
```

登录后进入 `/data-tasks`。

正式态前端配置（`apps/web/.env.local`，改 `NEXT_PUBLIC_*` 后需重新 `build:web`）：

```bash
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password
NEXT_PUBLIC_AGENT_RUNTIME_URL=
NEXT_PUBLIC_CONFIG_API_URL=
API_PROXY_TARGET=http://127.0.0.1:8787
```

浏览器走同源 Next BFF（`/api/v1/*`、`/api/copilotkit`）。BFF 上游用 `API_PROXY_TARGET`（默认 `http://127.0.0.1:8787`）。

就绪探针：

- 存活：`GET /healthz`
- 就绪：`GET /ready`（含 `startup_ms` / `phases`）

反代样例（真实生产）：[`deploy/nginx.datafoundry.conf.example`](https://github.com/datagallery-lab/datafoundry/blob/main/deploy/nginx.datafoundry.conf.example)。

贡献者本地热更新（非正式态，勿与 `start:*` 混开）见 [快速开始附录](../quick-start.md)。

## 身份和首次引导

正式态（`password`）展示登录、注册、密码重置和退出登录。正式测试下验证链接打在 API 控制台；真实生产走 SMTP。

Web 工作台会把同一组身份发送给配置 REST 请求和 CopilotKit agent run。用户变化时，工作台会重新挂载数据任务区域，让 session、已选资源、文件、产出、live run 状态和快速引导进度都留在当前用户作用域。

首次进入的用户会看到快速引导。可以点击用户区域附近的圆形 `?` 按钮重新打开。引导可以把示例 prompt 填入输入框，并会等你真正发送任务后再进入控制台步骤。

## 界面结构

Web 工作台分三栏：

| 区域 | 用途 |
| --- | --- |
| 左侧 | 管理会话和工作区资源。 |
| 中间 | 输入问题，查看 Agent 回复、步骤卡片和人工确认。 |
| 右侧 | 查看概览、追溯、产出、步骤详情和工作区文件。 |

窗口变窄时，右侧控制台会收起为抽屉。点击聊天区顶部的控制台入口可以重新打开。

## 跑第一个任务

1. 点击左侧「新建数据任务」。
2. 选择内置 **DTC Growth Review** 数据源。
3. 在输入框旁选择「服务端默认」或你配置的模型。
4. 输入问题并发送。

示例：

```text
帮我查看数据源里有哪些表，并说明每张表的主要字段。
```

发送后，中间区域会显示 Agent 回复和步骤卡片。右侧控制台同步显示运行状态、追溯和产出。

## 左侧：会话和资源

左侧管理两类内容：

| 内容 | 你可以做什么 |
| --- | --- |
| 数据任务 | 新建、切换、重命名、置顶、删除任务。 |
| 工作区资源 | 管理 Data Sources、Knowledge、Agent Tools 和 Assets。 |
| 用户菜单 | 查看当前用户、打开设置、退出登录或切换本地开发用户。 |

资源入口说明：

| 资源 | 用途 |
| --- | --- |
| Data Sources | 添加数据源、测试连接、抓取 schema、浏览表结构和表数据预览。 |
| Knowledge | 创建知识库、上传文档、导入文件资产、重建索引。 |
| Agent Tools | 管理 MCP Server 和 Skill。 |
| Assets | 查看、下载和删除可复用工作区文件。 |

模型选择不在左侧资源区。你可以在输入框旁的模型选择器中切换本次分析使用的模型。

## 中间：对话、步骤和人工确认

中间区域负责提问和展示 Agent 的分析过程。一次运行中，你会看到：

- Agent 流式回复。
- schema 检查、SQL 执行、文件读取等步骤卡片。
- 等待输入、运行中、已完成、失败或取消等状态。
- 人工确认类交互，例如后端要求你选择方案或补充信息。

点击步骤卡片后，右侧控制台会打开对应详情。你可以用这条链路核对 Agent 使用了哪些输入、调用了哪些工具、生成了哪些产出。

## 输入框能力

| 功能 | 用途 |
| --- | --- |
| 模型选择 | 为本次分析选择 LLM profile。 |
| 会话资源开关 | 控制本会话可用的数据源、知识库、MCP Server 和 Skill。 |
| `@` 提及 | 为单次提问指定数据源、文件或资源。 |
| 文件上传 | 上传本次分析需要的附件。 |
| 停止运行 | 取消正在执行的 run。 |

run 进行中再提交问题时，Web 工作台会把它放入提问队列，不会混入当前 run。你可以编辑或删除排队问题、立即发送某一条，或等当前 run 进入终态后自动发送下一条。队列只属于当前数据任务会话。

资源选择分三层：

1. 工作区默认资源：左侧资源面板里的长期配置。
2. 本会话资源：输入框底部资源开关，只影响当前任务。
3. 本次提问资源：用 `@` 指定单轮问题使用的资源或文件。

首次试用可以保留默认配置。

## 右侧：任务控制台

### 概览

概览展示当前问题、步骤数量、成功率、产出数量、Token 用量和动态步骤列表。你可以用它判断任务是否还在运行、是否失败、是否已经产出文件或表格。

### 追溯

追溯同时提供按时间排列的执行列表，以及基于持久化 checkpoint 的语义 DAG。图中连接 run、step、tool call、output 及它们的父子关系；列表更适合按时间快速扫描。常见条目包括：

1. 启动 run。
2. 检查数据源 schema。
3. 执行只读 SQL 或读取文件。
4. 创建表格、图表、SQL、报告或文件产出。

需要解释结果来源时，先看追溯，再进入单步详情。恢复历史会话时，该视图由服务端记录重建，不把浏览器 transcript 当作真实源。

### 产出

产出是 Agent 留下的可复用结果：

| 类型 | 能做什么 |
| --- | --- |
| 表格 | 预览、搜索、排序、下载或导出 CSV/XLSX。 |
| 图表 | 预览、下载或导出后端支持的文件格式。 |
| SQL | 查看 Agent 执行的查询。 |
| 报告 | 查看结构化结论和 Markdown 内容。 |
| 文件 | 下载，或加入工作区后在后续任务中引用。 |

文件型产出可以通过「加入工作区」变成跨会话可复用文件。表格、图表等产出下载和导出走 `/api/v1/artifacts/:id/download` 与 `/api/v1/artifacts/:id/export`。

打开产出后可将它引用到后续问题。可引用完整 artifact；对支持的表格和文本，也可只选中相关区域。选中的证据会显示在输入区旁，并在下一个 run 开始前由服务端解析。

### 详情

详情展示单个步骤的输入、输出、Token 用量、工具调用参数和工具结果。你可以从中间步骤卡片或追溯列表进入详情。

### 工作区文件

工作区文件面板支持：

- 将文件上传到当前会话，并将成功上传的文件提升到工作区。
- 列出当前可复用文件。
- 下载文件内容。
- 删除文件引用。

对话附件上传在输入框中完成，走 `/api/v1/chat/uploads`。文件型产出加入工作区后，会出现在工作区文件列表中。

## 数据源浏览

数据源面板支持两类只读查看：

| 功能 | API |
| --- | --- |
| schema 浏览 | `GET /api/v1/datasources/:id/schema` |
| 表数据预览 | `GET /api/v1/datasources/:id/tables/:table/preview` |

schema 浏览支持按表名或字段名搜索。表预览支持 `schema`、`limit`、`offset` 和 `orderBy` 查询参数。

## 停止运行

当 Agent 运行过久或问题问错了，可以点击输入框附近的停止按钮。前端会调用：

```text
POST /api/v1/runs/:id/cancel
```

后端会取消正在运行的 run，或把已持久化的运行状态标记为 canceled。已经产生的事件和产出仍可用于排查。

## 会话恢复

Web 工作台使用服务端会话接口恢复历史：

| API | 用途 |
| --- | --- |
| `GET /api/v1/sessions` | 读取左侧会话列表。 |
| `PATCH /api/v1/sessions/:id` | 更新会话标题。 |
| `DELETE /api/v1/sessions/:id` | 永久删除会话（含对话历史与子分支）；侧栏删除会调用此接口。 |
| `GET /api/v1/sessions/:id/conversation` | 恢复对话、工具调用、pending interaction 和 run events。 |
| `GET /api/v1/artifacts?sessionId=:id` | 恢复该会话的产出。 |

刷新页面或重新打开工作台后，你可以从左侧任务列表恢复之前的任务。恢复后，中间区域显示对话历史，右侧控制台可以回放 run 事件和产出，新问题会沿用该任务的会话上下文。

### 重问与分支

对已结束的历史轮次，编辑或重问早期问题会创建持久化子分支。也可从恢复对话中的 checkpoint 创建分支。DataFoundry 保留原历史，将工作台切换到子会话，并在分叉点提供分支导航，方便对比不同分析路径。

创建分支需要终态 run 或已持久化 checkpoint；运行中或 suspended 轮次不是稳定的分叉边界。

## Data Link

**Data Link** 打开表、字段、概念、实体及其关系的工作区图。当 workspace 中配置了包含预期工具的兼容 Data Link 或 DataGraph MCP Server 时，该入口可用。可在图中搜索入口节点、展开关联结构，并在让 Agent 分析前检查语义上下文。

DataFoundry 不内置语义图服务。如果没有配置兼容的 MCP Server，面板无法提供图数据；请先在 Agent Tools 中完成集成配置。

## 典型问题

| 场景 | 示例 |
| --- | --- |
| 探表 | `这个数据源里有哪些表？每张表主要字段是什么？` |
| 指标统计 | `按渠道统计订单量和 GMV。` |
| 趋势分析 | `分析最近 30 天 GMV 趋势，找出波动最大的日期。` |
| 多轮追问 | `再按品类拆分一下。` |
| 指定数据源 | `使用 sales-pg 数据源，统计最近 7 天订单情况。` |

## 能力边界

- 文件上传由 `chat.fileUpload` 和 `files` capability 控制。
- 图片输入由 `chat.imageInput` capability 控制。
- Knowledge、MCP 和 Skill 的运行效果取决于工作区资源配置和 `knowledge`、`mcp`、`skills` capability。
- 数据库查询走 Data Gateway，只暴露只读分析路径。
- 凭据只在资源创建或更新时提交，读接口不回传明文。

继续阅读：[数据源指南](data-sources.md)。
