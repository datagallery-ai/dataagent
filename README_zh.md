<h1 align="center">🚀 DataAgent</h1>

<p align="center">
  中文 · <a href="README.md">English</a>
</p>

<!-- Badges -->
<p align="center">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/Python-3.11+-brightgreen" alt="Python">
  <img src="https://img.shields.io/badge/Version-0.1.0-orange" alt="Version">
  <img src="https://img.shields.io/badge/LangGraph-1.1.3-red" alt="LangGraph">
  <img src="https://img.shields.io/badge/openJiuwen-0.1.1-purple" alt="openJiuwen">
  <img src="https://img.shields.io/badge/GaussVector-supported-blue" alt="GaussVector">
</p>

---

<h2>Data + AI Agent 企业级数据任务解决方案</h2>

> 🚀 **DataAgent** 是面向 **Data + AI** 场景的新一代企业级智能数据平台，以 Agent 范式重构数据工程全链路。深度融合 NL2SQL、统一语义层与多智能体协同，在**金融问数、AI for Science**等核心场景实现端到端的数据分析与特征挖掘闭环。

## 🌟 为什么选择 DataAgent

### 🏆 场景化优势

| 场景 | 传统方案 | DataAgent 的降维打击 | 典型应用 |
|------|----------|---------------------|----------|
| 📊 **金融问数** | 业务人员提需求→数据团队排期→手写 SQL→人工核验，一个指标查询 T+1 是常态 | NL2SQL 四阶段流水线（感知→生成→校验→反思），自然语言即问即答。统一语义层驱动指标自动映射，**BIRD DEV榜单 74%+ 执行准确率，秒级响应** | ✅ 企业金融分析助手 |
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

## 📋 环境要求

| 依赖 | 版本要求 |
|------|----------|
| 🐍 **Python** | >= 3.11 |
| 📦 **包管理** | uv (推荐) 或 pip |

## 📚 文档

完整文档位于 [`docs/`](docs/)（[中文](docs/zh/) · [English](docs/en/)）。本地构建与预览：

```bash
uv sync --extra mkdoc
uv run mkdocs serve -f docs/mkdocs.yml -a 0.0.0.0:8000
```

| 文档 | 说明 |
| --- | --- |
| 📖 [安装部署](docs/zh/installation/installation.md) | 使用 `uv` / pip 安装、环境变量配置与安装验证 |
| 📖 [快速开始](docs/zh/quick_start/quick_start.md) | 分钟级跑通 DataAgent 端到端流程 |
| 🗄️ [数据库安装指导](docs/zh/installation_doc/database_install/database_install.md) | 部署 Elasticsearch、PostgreSQL、MySQL；优先支持 GaussVector 接入，导入场景数据并接入 Semantic Service |
| ⚙️ [功能特性](docs/zh/function/function.md) | 核心能力、模块划分、工具与模型支持 |
| 🧩 [Semantic Service](docs/zh/semantic_service/semantic-service-user-guide.md) | 面向 NL2SQL 的 MetaVisor 增强元数据，优先围绕 GaussVector 提供语义层索引、候选表字段召回与 schema 感知增强 |
| 🔗 [openJiuwen](docs/zh/openJiuwen/openJiuwen-user-guide.md) | openJiuwen 集成与使用说明 |
| 🏗️ [架构文档](docs/zh/design_doc/design_doc.md) | 系统架构；context、规划引擎、action 等模块设计 |
| 📡 [接口设计](docs/zh/api_doc/api_doc.md) | A2A 北向服务接口与 Python SDK |
| 📋 [应用案例](docs/zh/case/case.md) | 构建 NL2SQL 专用 Agent、构建数据分析 Agent |
| 📝 [说明](docs/zh/explain/explain.md) | 开发、测试与文档维护说明 |
| 🗓️ [里程碑](docs/zh/milestone/milestone.md) | 版本规划与发布节奏 |

## 🚴 安装

### 1️⃣ 克隆项目

```bash
git clone https://gitcode.com/datagallery/DataAgent.git
cd DataAgent
```

### 2️⃣ 安装依赖 (推荐使用 uv)

```bash
# 安装依赖
uv sync

# 激活虚拟环境
source .venv/bin/activate  # Linux / macOS
.venv\Scripts\activate     # Windows
```

### 3️⃣ 或者使用 pip

```bash
pip install -e .
```

### 4️⃣ 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入实际的配置值
```

## ⚡ 快速开始

### 🎮 交互式快速启动

```bash
uv run -m dataagent quickstart
```

按提示输入模型配置后即可开始与 Agent 对话！

### 📁 使用配置文件启动

```bash
# 终端交互模式
uv run -m dataagent --config dataagent/core/flex/examples/quickstart.yaml
```

### 🔍 配置检查

```bash
# 检查配置文件中的环境变量引用
uv run -m dataagent config check dataagent/core/flex/examples/quickstart.yaml
```

## 📖 使用方法

### 🐍 Python SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("path/to/config.yaml")

# 单轮对话
response = await agent.chat("分析最近一周的销售数据趋势")
print(response)

# 流式对话
async for chunk in agent.astream(input={"user_query": "生成用户报告"}):
    print(chunk, end="", flush=True)
```

### 📝 YAML 配置文件示例

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  version: "1.0"
  description: "数据分析 Agent"
  backend: "langgraph"
  type: "react"

MODEL:
  chat_model:
    provider: "deepseek"
    model_type: "chat"
    params:
      model: "deepseek-chat"
      temperature: 0.7
      base_url: "$env{DEEPSEEK_BASE_URL}"
      api_key: "$env{DEEPSEEK_API_KEY}"

WORKSPACE:
  path: "/tmp/dataagent_workspace"
  allow_path:
    - "/tmp/dataagent_workspace"
```

### 🌐 A2A 1.0 服务模式

```bash
# 启动 A2A 服务器
uv run -m dataagent serve-a2a \
  --config path/to/config.yaml \
  --host 0.0.0.0 \
  --port 9999 \
  --auth-token your_token

# 服务地址
# ├── 🌟 AgentCard: http://localhost:9999/.well-known/agent.json
# ├── 📡 JSON-RPC:  http://localhost:9999/a2a/jsonrpc
# └── 🔌 REST:      http://localhost:9999/a2a/rest
```

## ⚙️ 配置说明

### 🔐 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | `sk-xxx` |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址 | `https://api.deepseek.com` |
| `BAILIAN_API_KEY` | 阿里云百炼 API 密钥 | `sk-xxx` |
| `OPENAI_API_KEY` | OpenAI API 密钥 | `sk-xxx` |

> 📌 更多配置请参考 `.env.example`

## 📄 许可证

本项目基于 **Apache License 2.0** 许可证开源 - 详见 [LICENSE](LICENSE) 文件。
