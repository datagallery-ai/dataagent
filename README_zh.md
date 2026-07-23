<h1 align="center">DataFoundry</h1>

<p align="center">
  企业级 Data Agent 工作台 —— 用统一语义读懂业务口径，在只读安全边界内执行多表、多步的复杂分析，<br />
  每一步可审计、可回放，把一句提问变成一次可信的数据分析。
</p>

<p align="center">
  <strong>28 类数据源开箱接入 · 企业语义与上下文组织 · 私有化部署 · 多模型适配 · 全程审计可追溯</strong>
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<p align="center">
  <a href="#-正式态跑通"><strong>快速开始</strong></a>
  ·
  <a href="https://datagallery-lab.github.io/datafoundry/"><strong>在线文档</strong></a>
  ·
  <a href="docs/zh/reference/supported-datasources.md"><strong>支持的数据源</strong></a>
  ·
  <a href="#️-路线图"><strong>路线图</strong></a>
  ·
  <a href="#-参与贡献"><strong>参与贡献</strong></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0" />
  <img src="https://img.shields.io/badge/TypeScript-5.x-3178c6?logo=typescript&logoColor=white" alt="TypeScript" />
  <img src="https://img.shields.io/badge/self--hostable-local%20first-2ea44f" alt="Self-hostable" />
  <img src="https://img.shields.io/badge/PRs-welcome-ff69b4" alt="PRs welcome" />
  <img src="https://img.shields.io/badge/status-early%20but%20usable-orange" alt="Status" />
  <br />
  <a href="https://github.com/mastra-ai/mastra"><img src="https://img.shields.io/badge/Mastra-agent%20runtime-111827" alt="Mastra agent runtime" /></a>
  <a href="https://github.com/ag-ui-protocol/ag-ui"><img src="https://img.shields.io/badge/AG--UI-event%20stream-6f42c1" alt="AG-UI event stream" /></a>
  <a href="https://github.com/vadimdemedes/ink"><img src="https://img.shields.io/badge/Ink-terminal%20UI-0f766e" alt="Ink terminal UI" /></a>
</p>

<p align="center">
  <img src="docs/assets/readme/gui-demo.gif" alt="DataFoundry Web 工作台演示" width="100%">
</p>

---

## 🤔 DataFoundry 是什么

让 AI 直接查企业数据库，团队最担心的从来不是「模型会不会写 SQL」，而是：**它懂不懂业务口径？会不会误改生产数据？凭据会不会漏进上下文？结论出了问题能不能复盘？**

大多数工具把问题简化成 `prompt → SQL → answer`，demo 惊艳，进企业就卡死。DataFoundry 走另一条路：**把 Agent 放进一个有语义、有权限、有证据链的数据任务系统里**，让「自然语言问数」升级成可控、可信、可验证的数据工作。

## ✨ 核心能力

- 🗄️ **28 类数据源，开箱接入** — 从 PostgreSQL、MySQL 到 Snowflake、BigQuery、ClickHouse，再到 MongoDB、Redis、Elasticsearch，快速打通企业现有数据栈，大幅降低数据接入和适配成本，让 Agent 更快进入真实业务分析。
- 🧠 **企业语义与上下文组织** — 统一管理 schema、指标口径和字段关系，让「GMV」「复购率」落到企业认可的表、字段和口径上，减少字段猜测、错误关联和口径偏差，从根上提升分析准确性。
- 🏠 **私有化部署与多模型适配** — 支持本地自托管，数据不出企业边界；模型侧兼容任意 OpenAI-compatible 服务（Qwen、DeepSeek、GPT……），按场景平衡安全、成本、延迟和效果。
- 🔒 **安全可控，审计可追溯** — 默认只读查询、凭据隔离、字段脱敏、行数限制和超时控制；SQL、工具调用和事件流全程留痕、可回放，让每个结论都有据可查。
- 🧩 **复杂数据任务深度优化** — 面向多表、多字段、长程分析和多步骤推理，把复杂问题逐步拆解、验证并收敛到可信结论，最终沉淀为表格、图表和报告等团队资产。

## 🆕 v0.2.0 新能力

DataFoundry 0.2 在首个可用版本上，进一步补齐了有状态、可追溯的 Data Agent 工作流：

- **可分支的并发分析** — 多个会话可同时运行，运行中可排队后续问题；恢复历史后，可从早期问题或 checkpoint 创建新分支，不覆盖原分析路径。
- **证据驱动的追问** — 可将完整产出，或选中的表格区域、文本片段引用到下一个问题；证据解析结果和诊断信息会进入受控 run context。
- **语义 Trace 与自研 Data Link** — 通过基于 checkpoint 的语义 Trace DAG 复盘执行结构；结合我们新近开源的 [Data Link](https://github.com/datagallery-lab/datalink)，把表和字段连接到业务概念、实体、可 JOIN 路径和带置信度的关系，为 Agent 提供更强语义支撑。
- **可复用的产出与工作区资产** — 统一预览和导出表格、图表、报告、SQL 和文件；文件可先上传到当前会话，再提升为跨会话复用的工作区资产。
- **面向正式部署的 Web 基础** — 内置密码认证、同源 API 代理、中英双语界面、模型连接测试、首次引导，并自动准备 DTC 增长分析案例。

完整能力与文档盘点见 [v0.2.0 发布说明](docs/zh/releases/v0.2.0.md)。

## 🚀 正式态跑通

正式态有两条路径（都不要跑 `npm run dev`）。本版本不提供 Docker / Compose。

### 推荐：Ubuntu / Debian 一键部署

`./deploy.sh` 自动生成配置、安装依赖、构建（含 Web、API 与 TUI），并以 detached 后台进程启动 Web + API——关闭终端一般不会停止服务。TUI 会在部署时构建就绪，但它是前台交互客户端：请另开终端执行 `./deploy.sh tui`（或 `npm run start:tui`），**不会**随 stack 后台常驻。语义 Data Link 为**外置**组件（deploy 不会安装或启动）——如需使用，稍后在 Web 的 MCP 配置中连接外部服务即可。部署时不要求填写模型 Key——登录后在 Web 中创建并启用模型即可。**不支持**原生 Windows / macOS。

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

若已有完整 `.env`，交互部署会跳过配置问答。要改端口或公开访问地址时（会保留密钥并先备份 `.env`）：

```bash
./deploy.sh deploy --reconfigure
```

常用管理命令：

```bash
./deploy.sh status
./deploy.sh start
./deploy.sh stop
./deploy.sh logs
./deploy.sh doctor
./deploy.sh tui      # 可选：前台启动 TUI（需 API 已在运行）
```

打开 `http://127.0.0.1:3000/login` 注册登录，在模型配置中创建 OpenAI-compatible Profile，然后进入 `/data-tasks`。远程部署请设置 `AUTH_PUBLIC_BASE_URL`；重复执行 `./deploy.sh deploy` 会进入维护窗口（先停止旧进程再安装/构建）。

### Windows / macOS / 其他：手动 npm

原生 Windows、macOS 或其他非 Ubuntu/Debian 环境请用手动 npm。请在同一环境内安装和运行；Windows 用户不要在 Windows 和 WSL 之间共用 `node_modules`。

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
npm install
cp .env.example .env
cp apps/web/.env.example apps/web/.env.local
```

在根目录 `.env` 配置模型，并按正式测试或真实生产填好认证（样例默认偏正式测试）：

```bash
LLM_PROVIDER=openai-compatible
LLM_MODEL=qwen-plus                # 或 deepseek-chat、gpt-4o……
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=replace-with-your-key

DATAFOUNDRY_AUTH_MODE=password
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000   # 真实生产改为 https://你的域名
AUTH_EMAIL_DELIVERY=test                     # 真实生产改为 smtp，并填 AUTH_SMTP_*
```

`apps/web/.env.local`：

```bash
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password
NEXT_PUBLIC_AGENT_RUNTIME_URL=
NEXT_PUBLIC_CONFIG_API_URL=
API_PROXY_TARGET=http://127.0.0.1:8787
```

构建并启动：

```bash
npm run build
npm run build:web
npm run start        # Web :3000 + API :8787
```

打开 `http://127.0.0.1:3000/login` 注册登录后进入 `/data-tasks`，提问：

```text
帮我查看数据源里有哪些表，并说明每张表的主要字段。
```

你会看到完整的一条链路：schema 检查 → 只读 SQL → SQL 审计 → 表格产出 → 可回放的 run history。

真实生产请再配 SMTP 与反代：[`deploy/nginx.datafoundry.conf.example`](deploy/nginx.datafoundry.conf.example)（静态 gzip/brotli；`/api/copilotkit` 关压缩与缓冲以保护 SSE）。

> 完整步骤与两种环境对照见 [快速开始](docs/zh/quick-start.md)；贡献者热更新（`npm run dev`）仅见该文档附录。接入自己的 PostgreSQL / MySQL / CSV 等见 [数据源指南](docs/zh/guides/data-sources.md)。

## 🆚 和 Coding Agent、SQL Chatbot 有什么不同

Coding agent 改代码，SQL chatbot 回答问题，DataFoundry 跑数据任务——三者的工作对象、风险边界和产出完全不同：

| | 工作对象 | 核心风险 | 产出 |
| --- | --- | --- | --- |
| Coding agent | 代码仓、测试、PR | 改错代码 | patch、commit、PR |
| SQL chatbot | prompt、SQL、回答 | 猜错表、越权、凭据泄漏、不可复盘 | 一段 SQL 或一段回答 |
| **DataFoundry** | 数据源、文件、知识库、工具、任务状态 | 生产数据边界、业务口径、审计证据 | **可回放的数据任务** + SQL 审计 + 表格 / 图表 / 报告 |

具体到数据任务上，DataFoundry 相比通用 coding agent 的核心优势是：

- **精度优势，来自数据约束** — coding agent 直接面对数据库时容易猜表、猜字段、猜口径；DataFoundry 强制 schema-first，并通过 Data Gateway 约束查询路径，减少字段猜测和错误关联。
- **安全优势，来自受控执行** — coding agent 能执行命令、读写文件，能力强但对企业数据风险更高；DataFoundry 默认只读 SQL、凭据隔离、字段脱敏、行数限制、超时和审计，更适合真实数据环境。
- **性能优势，来自任务链路收敛** — 不靠模型推理天然更快，而是通过数据源选择、schema 缓存、上下文预算、工具策略和 artifact 流程减少无效尝试，让分析更快收敛到结果。
- **复杂任务优势，来自数据工作流设计** — coding agent 擅长代码工程；DataFoundry 面向多表、多字段、多指标、知识库、文件和报告产出的分析任务，把「查数、验证、解释、沉淀产出」串成完整流程。
- **落地优势，来自企业运行时** — 这不是一个 demo，而是 Web 工作台、TUI、REST API、CopilotKit / AG-UI、Data Gateway、Skill、MCP、Files、Artifacts、Metadata 组合成的数据 Agent 操作底座。

## ⚙️ 一条数据任务如何跑完

```text
提出问题 → 对齐语义 → 受控执行 → 沉淀产出 → 回放复盘
```

1. **定义任务** — 选择数据源、文件、知识库和工具，用自然语言描述业务问题。
2. **对齐语义与结构** — Agent 先检查 schema 和可用上下文，把「GMV」「复购率」这类业务词落到真实表和字段上。
3. **受控执行** — Data Gateway 在只读边界内执行查询，统一处理 SQL guard、行数限制、超时和脱敏，每条 SQL 留审计日志。
4. **沉淀产出** — 结果进入表格、图表、报告或文件，成为团队可引用的资产。
5. **回放复盘** — Web、TUI、API 共用同一条 run history，随时回看每一步的依据。

<p align="center">
  <img src="docs/assets/readme/runtime-flow.png" alt="DataFoundry 运行流程" width="100%">
</p>

## 🖥️ 不止一个聊天框

**Web 工作台**适合日常分析和演示，**TUI** 适合终端和远程服务器，**API / CopilotKit / AG-UI** 让你把同一套可信运行时嵌进自己的产品。

<p align="center">
  <a href="docs/assets/readme/tui-demo.mp4">
    <img src="docs/assets/readme/tui-demo.gif" alt="DataFoundry TUI 演示" width="100%">
  </a>
</p>

## 🗄️ 接入现有数据栈，不重构

通过 Data Gateway 适配器接入：内置 DTC 增长经营复盘案例开箱即用；DuckDB、SQLite、CSV、Excel、PostgreSQL、MySQL 适合本地试用；云数仓、搜索引擎和 NoSQL 按需配置服务与凭据。

<p align="center">
  <img src="docs/assets/readme/database-wall.png" alt="DataFoundry 支持的数据源" width="100%">
</p>

完整列表见 [支持的数据源](docs/zh/reference/supported-datasources.md)。

## 🛡️ 安全边界

- 模型只接收受控上下文；数据库凭据、模型 API Key 和 MCP Token 不进入 `messages`、`context` 或 `forwardedProps`。
- 所有数据源访问经由 Data Gateway，默认只读，附带 SQL guard、行数限制、超时和字段脱敏。
- SQL 审计日志、工具调用记录和事件流全程留痕，支持事后复核。
- 生产级多租户鉴权、集中式 Secret 管理、监控和部署运维需按你的部署方案单独设计，详见 [安全说明](docs/zh/security.md)。

## 🗺️ 路线图

- [x] **受控数据任务工作台** — Web 与 TUI 共用 TypeScript Agent Runtime、CopilotKit / AG-UI 事件流、可回放运行历史、SQL 审计和统一资产层。
- [x] **安全数据访问底座** — 数据源注册、连接测试、schema 抓取、表预览、只读 SQL、脱敏、知识库导入、MCP 资源、Skill 包和模型配置。
- [ ] **统一语义层** — 沉淀指标、实体、关联、血缘和策略，让 Agent 从「猜字段」走向「理解业务口径」，从一次性 SQL 走向可治理的数据操作层。
- [ ] **自主分析循环** — Agent 规划分析、执行受控实验、审视结论，收敛到有证据支撑的结果。
- [ ] **评测与可靠性实验室** — NL2SQL、检索、工具调用和端到端任务基准，支撑回归门禁和失败分析。
- [ ] **企业控制平面** — 身份、RBAC、审批、审计导出、策略即代码、成本限制。

欢迎在 issue 和 discussion 里参与路线图讨论。

## 📚 文档

| 你要做什么 | 阅读 |
| --- | --- |
| 本地跑通 demo | [快速开始](docs/zh/quick-start.md) |
| 了解产品定位与能力边界 | [产品概览](docs/zh/overview.md) · [能力全览](docs/zh/capabilities.md) |
| 使用 Web / TUI | [Web 工作台指南](docs/zh/guides/web-workbench.md) · [TUI 指南](docs/zh/guides/tui.md) |
| 接入数据源 | [数据源指南](docs/zh/guides/data-sources.md) · [支持的数据源](docs/zh/reference/supported-datasources.md) |
| 对接 API 与运行时 | [REST API](docs/zh/reference/rest-api.md) · [Agent Runtime 与 AG-UI](docs/zh/reference/agent-runtime.md) |
| 理解架构与安全 | [架构概览](docs/zh/architecture/overview.md) · [安全说明](docs/zh/security.md) |

## 🤝 参与贡献

DataFoundry 迭代很快，小而聚焦的 PR 最容易被合入：

1. 行为、协议、数据源适配器和 Agent 策略变更，请先开 issue 或 discussion。
2. 每个 PR 聚焦一个运行时边界或功能区。
3. 提交前运行 `npm run build` 和改动区域对应的 smoke 检查（如 `npm run smoke:data-gateway`）。
4. 改动 setup、API、数据源配置或用户可见输出时，同步更新文档。
5. 不提交凭据、本地数据库、生成的 storage 或私有 benchmark 数据。

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 💬 社区与交流

加入 DataFoundry 社区，和我们讨论产品、路线图和落地实践；也欢迎通过 issue 和 discussion 反馈问题、提出想法。

<table align="center">
  <tr>
    <td align="center"><strong>QQ 交流群</strong></td>
    <td align="center"><strong>Slack Community</strong></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/assets/readme/community-qq-qrcode.png" alt="DataFoundry QQ 交流群二维码" width="220"></td>
    <td align="center"><img src="docs/assets/readme/community-slack-qrcode.png" alt="DataFoundry Slack 社区二维码" width="300"></td>
  </tr>
  <tr>
    <td align="center"><strong>交流群号</strong><br><code>1048076064</code></td>
    <td align="center"><a href="https://join.slack.com/t/datafoundry-7bb8405/shared_invite/zt-42qikc65e-DwA~8ltIri_WYWWpRMjCFQ"><strong>加入 Slack</strong></a></td>
  </tr>
</table>

## 🙏 致谢

DataFoundry 受益于这些优秀的开源项目与社区：

- 感谢 [LINUX DO](https://linux.do/) 社区的支持与讨论。
- [Mastra](https://github.com/mastra-ai/mastra)：Agent 运行时模式。
- [AG-UI](https://github.com/ag-ui-protocol/ag-ui)：事件流协议设计。
- [CopilotKit](https://github.com/CopilotKit/CopilotKit)：Agent 原生交互体验。
- [Ink](https://github.com/vadimdemedes/ink)：终端 UI 基础能力。
- [MCP](https://modelcontextprotocol.io)：工具生态与集成模型。

## 📄 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。

> DataFoundry 仍在快速开发中，以当前代码、公开文档和 smoke 检查结果为准。
