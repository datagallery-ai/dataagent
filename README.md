<h1 align="center">🚀 DataAgent</h1>

<p align="center">
  <a href="README_zh.md">中文</a> · English
</p>

<!-- Badges -->
<p align="center">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/Python-3.11+-brightgreen" alt="Python">
  <img src="https://img.shields.io/badge/Version-0.1.0-orange" alt="Version">
  <img src="https://img.shields.io/badge/LangGraph-1.1.3-red" alt="LangGraph">
  <img src="https://img.shields.io/badge/openJiuwen-0.1.14-purple" alt="openJiuwen">
  <img src="https://img.shields.io/badge/GaussVector-supported-blue" alt="GaussVector">
</p>

---

<h2>Data + AI Agent: Enterprise Data Task Solution</h2>

> 🚀 **DataAgent** is a next-generation enterprise data intelligence platform for **Data + AI** scenarios, reimagining the entire data engineering pipeline through the Agent paradigm. Deeply integrating NL2SQL, unified semantic layers, and multi-agent collaboration, it delivers end-to-end data analysis and feature mining across **financial risk control, AI for Science**, and other core domains.

## 🌟 Why DataAgent

### 🏆 Scenario Advantages

| Scenario | Traditional Approach | The DataAgent Edge | Typical Applications |
|----------|---------------------|-------------------|-------------------|
| 📊 **Financial Q&A** | Business request → data team queue → manual SQL → manual verification; T+1 is the norm for a single metric query | NL2SQL four-stage pipeline (Perception→Generation→Validation→Reflection), natural language to instant answers. Semantic metric mapping, **74%+ execution accuracy on BIRD DEV benchmark, sub-second response** | ✅ Enterprise financial analytics assistant |
| 🔬 **AI for Science** | Multi-source scientific data scattered everywhere; cross-database correlation requires manual exports; literature and data cannot be jointly queried | Multi-source federated queries + structured/unstructured joint retrieval, **natural-language-driven scientific data exploration** | ✅ Scientific data exploration platform |

### ⚡ Core Capabilities

| Capability | Description |
|------------|-------------|
| 🧠 **NL2SQL Intelligent Engine** | Four-stage pipeline: Perceptor→Generator→Validator→Reflector; multi-strategy fusion: Prompt / ICL / Skeleton / DC; supports SQLite / MySQL / PostgreSQL / Hive; **74%+ execution accuracy on BIRD benchmark** |
| 🔬 **Automated Feature Engineering** | Agents autonomously explore relationships across hundreds of tables, auto-discover latent feature combinations with importance ranking and visualization — **10x+ efficiency boost** |
| 🏭 **Full-Pipeline Data Factory** | Data ingestion→Schema perception→Feature mining→Model training→Report generation — **one YAML config runs the complete data engineering pipeline** |
| 🧩 **Unified Semantic Layer** | Prioritizes GaussVector as an enhanced vector retrieval foundation in the semantic layer, turning tables, columns, metric definitions, and business descriptions into retrievable schema signals for NL2SQL and multi-source semantic alignment |
| 🔌 **Plugin Tool Ecosystem** | Local functions / MCP (stdio+sse) / A2A — three tool types with unified registration and invocation. Auto-discovery and on-demand loading. Built-in data analysis SKILLs |
| 📡 **Native Multi-Agent Collaboration** | Full A2A 1.0 protocol support: automatic agent discovery, capability mapping, standardized communication. Naturally supports distributed collaboration for complex business tasks |
| 🧩 **YAML as Agent** | Model, tools, memory, workflow, scenario prompts — all declaratively orchestrated. **From idea to running Agent in minutes** |
| 🛡️ **Enterprise Security Sandbox** | Workspace isolation + path whitelisting + full audit trail, meeting financial-grade compliance requirements |
| ⚡ **Out of the Box** | 20+ industry scenario example configs — **zero code to start, up and running in minutes** |

## 📋 Environment Requirements

| Dependency | Version |
|------------|---------|
| 🐍 **Python** | >= 3.11 |
| 📦 **Package Manager** | uv (recommended) or pip |

## 📚 Documentation

Full documentation lives under [`docs/`](docs/) ([中文](docs/zh/) · [English](docs/en/)). Build and preview locally:

```bash
uv sync --extra mkdoc
uv run mkdocs serve -f docs/mkdocs.yml
```

| Document | Description |
| --- | --- |
| 📖 [Installation](docs/en/installation/installation.md) | Install with `uv` / pip, environment variables, and verification |
| 📖 [Quick Start](docs/en/quick_start/quick_start.md) | Run an end-to-end DataAgent workflow in minutes |
| 🗄️ [Database Installation](docs/en/installation_doc/database_install/database_install.md) | Deploy Elasticsearch, PostgreSQL, MySQL; prioritize GaussVector integration, import scenario data, and connect Semantic Service |
| ⚙️ [Features](docs/en/function/function.md) | Core capabilities, modules, tools, and model support |
| 🧩 [Semantic Service](docs/en/semantic_service/semantic-service-user-guide.md) | Semantic Service enriched metadata for NL2SQL, prioritizing GaussVector-oriented semantic-layer indexing, candidate schema recall, and schema perception enhancement |
| 🔗 [openJiuwen](docs/en/openJiuwen/openJiuwen-user-guide.md) | openJiuwen integration and usage guide |
| 🏗️ [Architecture](docs/en/design_doc/design_doc.md) | System architecture; context, planning engine, and action modules |
| 📡 [API Design](docs/en/api_doc/api_doc.md) | A2A northbound interface and Python SDK |
| 📋 [Application Cases](docs/en/case/case.md) | Build a dedicated NL2SQL Agent; build a data analysis Agent |
| 📝 [Notes](docs/en/explain/explain.md) | Development, testing, and documentation maintenance |
| 🗓️ [Milestone](docs/en/milestone/milestone.md) | Release planning and roadmap |

## 🚴 Installation

### 1️⃣ Clone the project

```bash
git clone https://gitcode.com/datagallery/DataAgent.git
cd DataAgent
```

### 2️⃣ Install dependencies (uv recommended)

```bash
# Install dependencies
uv sync

# Activate virtual environment
source .venv/bin/activate  # Linux / macOS
.venv\Scripts\activate     # Windows
```

### 3️⃣ Or use pip

```bash
pip install -e .
```

### 4️⃣ Configure environment variables

```bash
# Copy environment template
cp .env.example .env

# Edit .env file with your actual configuration values
```

## ⚡ Quick Start

### 🎮 Interactive quick start

```bash
uv run -m dataagent quickstart
```

Follow the prompts to enter model configuration and start chatting with the Agent!

### 📁 Start with config file

```bash
# Terminal interactive mode
uv run -m dataagent --config dataagent/core/flex/examples/quickstart.yaml
```

### 🔍 Config check

```bash
# Check environment variable references in config
uv run -m dataagent config check dataagent/core/flex/examples/quickstart.yaml
```

## 📖 Usage

### 🐍 Python SDK

```python
from dataagent import DataAgent

agent = DataAgent.from_config("path/to/config.yaml")

# Single-turn conversation
response = await agent.chat("Analyze sales data trends for the past week")
print(response)

# Streaming conversation
async for chunk in agent.astream(input={"user_query": "Generate user report"}):
    print(chunk, end="", flush=True)
```

### 📝 YAML Config Example

```yaml
AGENT_CONFIG:
  name: "My Data Agent"
  version: "1.0"
  description: "Data Analysis Agent"
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

### 🌐 A2A 1.0 Server Mode

```bash
# Start A2A server
uv run -m dataagent serve-a2a \
  --config path/to/config.yaml \
  --host 0.0.0.0 \
  --port 9999 \
  --auth-token your_token

# Service endpoints
# ├── 🌟 AgentCard: http://localhost:9999/.well-known/agent.json
# ├── 📡 JSON-RPC:  http://localhost:9999/a2a/jsonrpc
# └── 🔌 REST:      http://localhost:9999/a2a/rest
```

## ⚙️ Configuration

### 🔐 Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | `sk-xxx` |
| `DEEPSEEK_BASE_URL` | DeepSeek API Base URL | `https://api.deepseek.com` |
| `BAILIAN_API_KEY` | Alibaba Cloud Bailian API Key | `sk-xxx` |
| `OPENAI_API_KEY` | OpenAI API Key | `sk-xxx` |

> 📌 For more configuration, refer to `.env.example`

## 📄 License

This project is licensed under the **Apache License 2.0** - see the [LICENSE](LICENSE) file for details.
