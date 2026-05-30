---
hide:
  - navigation
---

<div style="text-align: center;" markdown>

# 🚀 DataAgent

<p style="text-align: center;">
  <a href="https://www.python.org/downloads/"><img alt="Python" src="https://img.shields.io/badge/Python-3.11+-blue.svg"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg"></a>
  <a href="https://github.com/langchain-ai/langgraph"><img alt="LangGraph" src="https://img.shields.io/badge/LangGraph-1.1.3-red.svg"></a>
  <a href="openJiuwen/openJiuwen-user-guide.md"><img alt="openJiuwen" src="https://img.shields.io/badge/openJiuwen-0.1.1-purple.svg"></a>
  <img alt="GaussVector" src="https://img.shields.io/badge/GaussVector-semantic%20layer-blue.svg">
</p>

</div>

---

<h2>Data + AI Agent 企业级数据任务解决方案</h2>

> 🚀 **DataAgent** 是面向 **Data + AI** 场景的新一代企业级智能数据平台，以 Agent 范式重构数据工程全链路。深度融合 NL2SQL、统一语义层与多智能体协同，在**金融问数、AI for Science**等核心场景实现端到端的数据分析与特征挖掘闭环。

## 🌟 为什么选择 DataAgent

### 🏆 场景化优势

| 场景 | 传统方案 | DataAgent 的降维打击 | 典型应用 |
|------|----------|---------------------|----------|
| 📊 **金融问数** | 业务人员提需求→数据团队排期→手写 SQL→人工核验，一个指标查询 T+1 是常态 | NL2SQL 四阶段流水线（感知→生成→校验→反思），自然语言即问即答。统一语义层驱动指标自动映射，**BIRD DEV 榜单 74%+ 执行准确率，秒级响应** | ✅ 企业金融分析助手 |
| 🔬 **AI for Science** | 多源科研数据散落各处，跨库关联分析靠手工导出拼接，文献与数据无法联合检索 | 多源联邦查询 + 结构化/非结构化联合检索，**自然语言驱动的科学数据探索** | ✅ 科研数据探索平台 |

### ⚡ 核心能力

| 能力 | 说明 |
|------|------|
| 🧠 **NL2SQL 智能引擎** | 感知器→生成器→校验器→反思器四阶段流水线；Prompt / ICL / Skeleton / DC 多策略融合；支持 SQLite / MySQL / PostgreSQL / Hive；BIRD 等 Benchmark **执行准确率 74%+** |
| 🔬 **自动特征工程** | Agent 自主探索数百张数据表关联关系，自动发现潜在特征组合，支持特征重要性排序与可视化，**特征工程效率提升 10 倍+** |
| 🏭 **全链路数据工厂** | 数据接入→Schema 感知→特征挖掘→模型训练→报告生成，**一套 YAML 配置跑通完整数据工程流水线** |
| 🧩 **统一语义层** | 优先支持 GaussVector 作为语义层增强向量检索底座，将表、字段、指标口径和业务描述沉淀为可检索的 schema 线索，支撑 NL2SQL 与多源查询的语义对齐 |
| 🔌 **插件化工具体系** | 本地函数 / MCP (stdio+sse) / A2A 三类工具统一注册与调用机制，工具自动发现、按需加载；内置数据分析等 SKILL |
| 📡 **多 Agent 协同原生** | 完整 A2A 1.0 协议支持，Agent 间自动能力发现、能力映射、标准化通信，天然支持复杂业务的分布式协作 |
| 🧩 **YAML 即 Agent** | 模型、工具、记忆、工作流、场景提示词全部声明式编排，**分钟级从想法到可运行 Agent** |
| 🛡️ **企业级安全沙箱** | Workspace 隔离 + 路径白名单 + 全链路操作审计，满足金融级安全合规要求 |
| ⚡ **开箱即用** | 20+ 行业场景示例配置，**零代码启动，分钟级上手** |

## 🚀 快速入口

- [安装部署](installation/installation.md)
- [快速开始](quick_start/quick_start.md)
- [功能特性](function/function.md)
- [应用案例](case/case.md)

## 📚 文档大纲

<div class="grid cards" markdown>

-   **安装部署**

    选择 `uv` / `pip` 安装方式，完成环境配置与模型接入；需要数据库时，可继续完成 Elasticsearch、PostgreSQL、MySQL 部署，优先支持 GaussVector 接入，并导入场景数据与 Semantic Service。

    [开始安装 →](installation/installation.md) · [数据库安装指导 →](installation_doc/database_install/database_install.md)

-   **快速开始**

    运行示例，快速跑通端到端链路。

    [一键启动 →](quick_start/quick_start.md)

-   **功能特性**

    了解核心能力、模块划分、工具与模型支持；含 Semantic Service、优先支持 GaussVector 的语义层向量检索增强、openJiuwen 等子模块。

    [查看功能特性 →](function/function.md) · [Semantic Service →](semantic_service/semantic-service-user-guide.md) · [openJiuwen →](openJiuwen/openJiuwen-user-guide.md)

-   **架构文档**

    了解整体架构、模块关系与关键流程设计。

    [查看架构文档 →](design_doc/design_doc.md)

-   **接口设计**

    了解关键接口与对接方式，便于二次开发与集成。

    [查看接口设计 →](api_doc/api_doc.md)

-   **应用案例**

    构建 NL2SQL 专用 Agent、构建数据分析 Agent 等教程与最佳实践。

    [查看应用案例 →](case/case.md)

-   **里程碑**

    了解版本规划与发布节奏。

    [查看里程碑 →](milestone/milestone.md)

-   **说明**

    查看常见说明、版本与贡献指南。

    [查看说明 →](explain/explain.md)

</div>
