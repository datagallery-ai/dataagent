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
| **🔧 统一工具接入与知识检索** | 工具层以本地函数工具与 gym 环境工具为主，经 `ToolManager` 统一注册与调用；`Perceptor`（NL2SQL）负责把语义服务元数据与知识组织成可检索的感知层，支撑推理与执行阶段的上下文选择。 |
| **✅ 运行边界说明** | 通过场景提示词、工具描述和工作流节点描述 Agent 行为边界。当前主线没有面向用户的奖励引擎、约束推理引擎或独立 RewardManager 配置入口；评测能力已迁移到独立项目维护。 |
| **🧾 上下文与轨迹管理** | 框架将会话日志、业务元数据、知识与工具信息统一纳入同一套沉淀体系，按需对接 `ElasticSearch` / `GaussVector` / `PostgreSQL` 等外部存储，并支持向量检索、全文检索与图关系查询等多种检索形态；其中 `Context` 负责上下文与轨迹管理，完成 State 抽取与持久化，同时维护 DAG 与 IR，支撑复杂任务的可追溯与可复盘。 |

## 核心模块

| 模块 | 功能描述 |
|-|-|
| **NL2SQL** | 自然语言 → SQL 执行的专用能力。 |
| **Semantic Service** | 当前阶段提供面向 NL2SQL 的增强元数据 REST 能力，并优先围绕 GaussVector 做了语义层向量索引、召回排序和 schema 感知增强，支撑表、字段、指标口径和业务描述的候选 schema 召回；本体服务相关能力正在开发中。详见 [Semantic Service 使用指南](../semantic_service/semantic-service-user-guide.md)。 |
| **Perceptor** | 检索与感知能力。组织工具信息、元数据与知识。 |
| **Config Manager** | 配置管理。支持配置修改与加载。 |
| **CBB** | Core 基座抽象。定义 Agent、Node、Router、State 等基类。 |
| **Context** | 上下文与轨迹管理。State 抽取及持久化，同时维护 DAG 与 IR。 |
| **Framework Adapters** | 适配执行后端与存储。统一封装框架差异，也包含 checkpoint 机制。 |
| **Managers** | 统一管理 LLM、Prompt 与 Action；不包含面向用户的奖励引擎。 |
| **Interface** | 对外接口层。包含 SDK 与 REST 服务端入口。 |
| **Evolution** | 训练与演进相关代码。包含部分环境与训练脚本。 |
| **Tests** | 单元测试与端到端用例集合。覆盖工作流、工具与接口。 |

---

## 工具支持

| 工具支持主要特性 | 具体说明 |
| --- | --- |
| **统一管理入口** | DataAgent 将工具能力统一纳入每个 Agent 独立的 `ToolManager` 管理，支持注册、调用与结果封装。 |
| **工具类型** | 本地 Python 函数（YAML `TOOLS.local_functions` / 可选 builtin）与 gym 环境工具（如 `SQLiteEnv` 的 `@Env.tool`） |
| **统一形态** | 工具最终以统一实例进入工具管理器，提供统一的 schema 描述与调用入口。 |

### 工具加载和使用流程

| 阶段 | 说明 |
| --- | --- |
| Agent 初始化阶段 | Flex 运行时构建 `AgentEnv` 时会创建 `ToolManager(config_manager=agent.config)`，并调用 `init_from_config(config)` 注册 YAML 声明的本地工具与隐式 job/HITL 等工具（若启用）。 |
| 工具调用阶段 | 使用 `list_tools` / `get_schema` 获取元数据，再通过工具的 `call` 统一调用。 |
| 上层使用方式 | 上层只需声明工具名称与参数，路由由 `ToolManager` 处理。 |

---

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

说明：当前主线已拆除 MCP 客户端与 A2A 工具栈；请仅使用本地函数 / gym 环境工具。预制 builtin 目录默认为空，可按需通过 YAML 启用文件/bash 等工具。

---

### 配置项说明

| 类型 | 必填项 | 字段说明 |
| --- | --- | --- |
| 本地工具配置 | `module`、`function` | `module`：工具函数所在的 Python 模块路径。<br>`function`：函数名。<br>`category`：工具分类（用于分组与过滤）。<br>`description`：可选字段；当前仅 `sub_agent_tool` 会将该字段作为补充说明合并进工具描述，其他本地函数工具默认使用函数 docstring 作为工具说明。<br>`config`：扩展配置（如工具自定义参数）。 |

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
| 工作流 backend | `AGENT_CONFIG.backend`（当前仅 `langgraph`）决定工作流引擎，不影响 LLM SDK 选择。 |

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
 5. **backend 选择引擎** ：`provider` 不再用于选择 SDK；`AGENT_CONFIG.backend` 当前仅支持 LangGraph。
