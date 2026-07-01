---
hide:
  - navigation
---

## 核心功能

| 功能 | 功能描述 |
|-|-|
| **🧩 配置化的 Agent 框架** | 抽象 `CBB` 基座，综合 Agent / Node / Router / State 等通用能力，在此之上支持用 YAML 配置一键启动；配置加载与覆盖遵循默认配置、用户配置与 `.env` 覆盖的分层策略，并由 `Config Manager` 统一管理与加载，从而在不同环境/场景中稳定复用同一套编排与依赖。 |
| **♻️ 可编排的 ReAct 框架** | 面向探索式推理与多步工具调用，`Flex` 以 ReAct 风格为核心，支持按配置组合 `Pre / Actor Loop / Post` 流程。 |
| **🧭 场景覆盖和自定义扩展** | 覆盖 NL2SQL 数据查询、主 Agent 调用 NL2SQL 子 Agent 等场景。内置 NL2SQL 能力，覆盖自然语言理解、SQL 生成、校验、执行与解释/输出。此外，还支持通过配置或 `AgentBuilder` 扩展自定义 Agent。 |
| **🔧 统一工具接入与知识检索** | 工具层既支持本地函数工具，也支持 `MCP` / `A2A` 外部工具或外部 Agent，并复用同一套注册与调用机制，支持自动发现与按需加载；`Perceptor` 模块负责把工具信息、元数据与知识组织成可检索的感知层，使 Agent 在推理与执行阶段能够基于上下文更准确地选择工具，并将工具元数据沉淀到记忆体系中以便后续复用。 |
| **✅ 运行边界说明** | 通过场景提示词、工具描述和工作流节点描述 Agent 行为边界。当前主线没有面向用户的奖励引擎、约束推理引擎或独立 RewardManager 配置入口；评测能力已迁移到独立项目维护。 |
| **🧾 上下文与轨迹管理** | 框架将会话日志、业务元数据、知识与工具信息统一纳入同一套沉淀体系，按需对接 `ElasticSearch` / `GaussVector` / `PostgreSQL` 等外部存储，并支持向量检索、全文检索与图关系查询等多种检索形态；其中 `Context` 负责上下文与轨迹管理，完成 State 抽取与持久化，同时维护 DAG 与 IR，支撑复杂任务的可追溯与可复盘。 |

## 核心模块

| 模块 | 功能描述 |
|-|-|
| **NL2SQL** | 自然语言 → SQL 执行的专用能力。 |
| **Semantic Service** | 当前阶段提供面向 NL2SQL 的增强元数据 REST 能力，并优先围绕 GaussVector 做了语义层向量索引、召回排序和 schema 感知增强，支撑表、字段、指标口径和业务描述的候选 schema 召回；本体服务相关能力正在开发中。详见 [Semantic Service 使用指南](../semantic_service/semantic-service-user-guide.md)。 |
| **openJiuwen** | openJiuwen 集成与使用。详见 [openJiuwen 使用指南](../openJiuwen/openJiuwen-user-guide.md)。 |
| **Perceptor** | 检索与感知能力。组织工具信息、元数据与知识。 |
| **Config Manager** | 配置管理。支持配置修改与加载。 |
| **CBB** | Core 基座抽象。定义 Agent、Node、Router、State 等基类。 |
| **Context** | 上下文与轨迹管理。State 抽取及持久化，同时维护 DAG 与 IR。 |
| **Framework Adapters** | 适配执行后端与存储。统一封装框架差异，也包含 checkpoint 机制。 |
| **Managers** | 统一管理 LLM、Prompt 与 Action；不包含面向用户的奖励引擎。 |
| **Interface** | 对外接口层。包含 CLI、SDK 与服务端入口。 |
| **Evolution** | 训练与演进相关代码。包含部分环境与训练脚本。 |
| **Tests** | 单元测试与端到端用例集合。覆盖工作流、工具与接口。 |

---

## 工具支持

| 工具支持主要特性 | 具体说明 |
| --- | --- |
| **统一管理入口** | DataAgent 将工具能力统一纳入每个 Agent 独立的 `ToolManager` 管理，支持注册、发现、调用与结果封装。 |
| **工具类型** | 本地 Python 函数（包含内置工具函数与用户自定义函数）<br>A2A 外部 Agent<br>MCP 外部服务调用 |
| **统一形态** | 无论工具类型如何，最终都会以统一的工具实例形式进入工具管理器，并提供统一的 schema 描述与调用入口。 |

### 工具加载和使用流程

| 阶段 | 说明 |
| --- | --- |
| Agent 初始化阶段 | Flex 运行时构建 `AgentEnv` 时会创建 `ToolManager(config_manager=agent.config)`，并调用 `init_from_config(config)` 注册内置工具、YAML 中声明的本地工具、A2A 工具和 MCP 工具。这个过程会将所有来源的工具统一成结构化的工具表示形式：工具名、工具描述、工具参数。 |
| 工具调用阶段 | 在工具需要被调用时，使用工具管理器的 `list_tools` 接口和 `get_schema` 接口即可获得工具的相关元数据信息，然后统一唤起工具的 `call` 成员函数直接调用。 |
| 上层使用方式 | DataAgent 上层在实际调用时只需声明“工具名称与参数”，具体的调用路由由系统自动处理。 |

---

### 工具类型对比（本地 / A2A / MCP）

以下将三类工具类型并列到一张表中，方便横向对比与检索。

| 维度 | 本地 Python 函数 | A2A 外部 Agent | MCP 外部服务 |
| --- | --- | --- | --- |
| 概要 | 本地工具在当前进程内执行，延迟低、调试方便，适合封装业务逻辑、数据处理或已有 Python 能力。它是默认且最轻量的工具形态。 | A2A 支持通过协议接入外部 Agent，并自动发现对方暴露的能力（skills/tools）。系统将这些能力映射为可调用工具，适合跨系统、跨团队复用能力。 | MCP 支持连接外部工具服务，兼容 stdio 与 sse 两种传输方式。适合对接独立服务、跨语言工具或需要运行隔离的能力。 |
| 配置入口 | `TOOLS.local_functions` 列表配置加载，每一项声明模块与函数名；测试或脚本中也可直接调用 `ToolManager.register_local_tool`。 | `TOOLS.A2A` 配置，每个 agent 以 `agent_id` 作为键。 | 推荐 `TOOLS.mcp_servers`，指定 `server_id`、`transport_type` 与 `config`。 |
| 必填项 | `module`、`function` | agent 键名、`base_url` | `server_id`、`config` |
| 注意事项 | — | 1. 可用性依赖：工具可用性取决于远端 Agent 是否在线，以及其 AgentCard 描述是否完整。<br>2. 参数表达：A2A 调用通过自然语言转发，建议参数结构清晰、字段语义明确。<br>3. 工具命名冲突：远端工具名可能与本地工具重名，建议通过命名规范区分。 | 1. Transport 选择：stdio 适合本地子进程型服务；sse 适合远端 HTTP 服务。<br>2. 连接与资源：stdio 需要关注子进程生命周期与资源清理。<br>3. 结果内容类型：MCP 工具可能返回文本或图像等内容，需要上层决定展示策略。<br>4. 服务稳定性：建议配置超时与重试策略，避免远端不稳定影响流程。|

### 示例配置

**```local tools```**
<pre><code>TOOLS:
  local_functions:
    - module: "your_project.tools.text_tools"
      function: "clean_text"
      category: "text"
    - module: "your_project.tools.sql_tools"
      function: "sql_executor"
      category: "data"
      config:
        timeout: 30</code></pre>

**```A2A tools```**
<pre><code>TOOLS:
  A2A:
    - my_agent:
        base_url: "https://a2a.example.com"
        auth_token: "YOUR_TOKEN"
        timeout: 30</code></pre>

**```mcp tools```**
<pre><code>TOOLS:
  mcp_servers:
    - server_id: "analytics_mcp"
      transport_type: "stdio"
      config:
        command: "python"
        args: ["-m", "your_mcp_server"]
        env:
          API_KEY: "YOUR_KEY"
        cwd: "/path/to/working/dir"</code></pre>

注意：`config` 的必填项取决于 `transport_type`：`stdio` 必须包含 `command`；`sse` 当前运行时按 `url` 建立连接，建议直接配置 `url`。

---

### 配置项说明

以下为工具相关的通用配置概念说明（具体字段以项目配置为准），按类型集中到一张表便于横向对比：

| 类型 | 必填项 | 字段说明 |
| --- | --- | --- |
| 本地工具配置 | `module`、`function` | `module`：工具函数所在的 Python 模块路径。<br>`function`：函数名。<br>`category`：工具分类（用于分组与过滤）。<br>`description`：可选字段；当前仅 `sub_agent_tool` 会将该字段作为补充说明合并进工具描述，其他本地函数工具默认使用函数 docstring 作为工具说明。<br>`config`：扩展配置（如工具自定义参数）。 |
| A2A Agent 配置 | agent 键名、`base_url` | agent 键名：远端 agent 的唯一标识，例如 `TOOLS.A2A: - web_searcher: ...`。<br>`base_url`：访问地址。<br>`auth_token`：鉴权令牌（可选）。<br>`timeout`：超时时间。 |
| MCP 服务配置 | `server_id`、`config` | `server_id`：MCP 服务标识。<br>`transport_type`：`stdio` 或 `sse`（默认 `stdio`）。<br>`config`：stdio 常用项 `command`、`args`、`env`、`cwd`；sse 当前建议配置 `url`。<br>`category / description`：用于分类与展示说明。 |

---

### 命名规范建议

| 命名规范 | 具体内容 |
| --- | --- |
| 语义清晰 | 名称体现“动作 + 对象”，避免过短或过泛。 |
| 避免重名 | 跨来源工具命名应避免同名，避免工具路由歧义。 |
| 保持稳定 | 一旦对外使用，尽量保持名称稳定以免影响调用方。 |

---

## 模型支持

| 模块 | 说明 |
| --- | --- |
| 统一管理入口 | DataAgent 使用 `LLMManager` 统一管理模型实例的创建与缓存，模型配置来自 YAML 的 `MODEL` 段。 |
| 初始化流程 | 在初始化阶段，系统会遍历yaml配置文件的 `MODEL` 下的每个 section 并创建对应模型实例，供 Agent 与工作流调用。 |
| LLM 底层 | 统一经 `LLMClient`（litellm，OpenAI 兼容协议）；`MODEL.provider` 用于拼接环境变量 `{PROVIDER}_BASE_URL` / `{PROVIDER}_API_KEY`。 |
| Embedding | `model_type=embedding` 的 section 仅注册配置（`get_llm_config`）；向量推理由知识库/工具侧通过 OpenAI 兼容 `embeddings` API 直接调用，不创建 `LLMClient` 实例。 |
| 工作流 backend | `AGENT_CONFIG.backend`（`langgraph` / `openjiuwen`）仅决定工作流引擎，不影响 LLM SDK 选择。 |

### 使用方法（YAML 配置）

模型统一配置在 `MODEL` 下，每个 section 表示一个模型实例的配置块。

### YAML 结构

```yaml
MODEL:
  chat_model:
    name: "DEEPSEEK_CHAT"
    provider: "deepseek"
    model_type: "chat"
    params:
      base_url: "https://api.deepseek.com"
      model: "deepseek-chat"
      api_key: "YOUR_KEY"
  embedding_model:
    name: "EMB_MODEL"
    provider: "embedding"
    model_type: "embedding"
    params:
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
      model: "text-embedding-v4"
      api_key: "YOUR_KEY"
```

### MODEL 配置

| 项目 | 说明 |
| --- | --- |
| MODEL 字段含义 | `name`：模型实例名称；<br>`provider`：平台标识，用于查找环境变量；<br>`model_type`：模型类型（`chat` 或 `embedding`）；<br>`params`：传给底层 SDK 的参数集合（如 `model`、`base_url`、`api_key`、`temperature`、`max_tokens` 等）。 |
| 必填字段 | `name`（模型实例名称，全局唯一）、`provider`（提供商标识）、`model_type`（`chat` 或 `embedding`）、`params`（模型初始化参数集合，其中必须包含 `model`）。 |
| params 通用要求 | 至少包含 `model`；需提供 `api_key`（可在 YAML 中配置，也可通过环境变量注入）；兼容接口时需提供 `base_url`。 |

### 注意事项
 1. **至少配置一个 chat 模型** ：系统默认优先使用 `model_type=chat` 的模型作为默认模型。
 2. **name 唯一性** ：相同 `name` 会覆盖已有实例，请避免重名。当前代码对未填写 `name` 的配置有兼容兜底，会使用 `MODEL` 下的 section 名作为模型实例名称；建议显式填写 `name`。
 3. **API Key 读取逻辑** ：优先读取 `MODEL.<section>.params.api_key`；未配置时按 `provider` 查找 `{PROVIDER}_API_KEY`。
 4. **base_url 读取逻辑** ：优先读取 `MODEL.<section>.params.base_url`；未配置时按 `provider` 查找 `{PROVIDER}_BASE_URL`。
 5. **backend 选择 SDK** ：`provider` 不再用于选择 SDK；`AGENT_CONFIG.backend` 决定使用 LangGraph 还是 openJiuWen。
