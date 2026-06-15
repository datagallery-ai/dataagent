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
  <a href="openJiuwen/openJiuwen-user-guide.md"><img alt="openJiuwen" src="https://img.shields.io/badge/openJiuwen-0.1.14-purple.svg"></a>
  <img alt="GaussVector" src="https://img.shields.io/badge/GaussVector-semantic%20layer-blue.svg">
</p>

</div>

---

<h2>Data + AI Agent: Enterprise Data Task Solution</h2>

> 🚀 **DataAgent** is a next-generation enterprise data intelligence platform for **Data + AI** scenarios, reimagining the entire data engineering pipeline through the Agent paradigm. Deeply integrating NL2SQL, unified semantic layers, and multi-agent collaboration, it delivers end-to-end data analysis and feature mining across **financial Q&A, AI for Science**, and other core domains.

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

## 🚀 Quick Links

- [Installation](installation/installation.md)
- [Quick Start](quick_start/quick_start.md)
- [Features](function/function.md)
- [Use Cases](case/case.md)

## 📚 Documentation

<div class="grid cards" markdown>

-   **Installation**

    Choose `uv` / `pip` installation, environment setup and model configuration; when databases are needed, continue with Elasticsearch, PostgreSQL, MySQL deployment, prioritized GaussVector integration, scenario data import, and Semantic Service setup.

    [Start Installation →](installation/installation.md) · [Database Installation →](installation_doc/database_install/database_install.md)

-   **Quick Start**

    Run examples and quickly get the end-to-end pipeline working.

    [Quick Start →](quick_start/quick_start.md)

-   **Features**

    Learn about core capabilities, module structure, tools and model support; includes Semantic Service, semantic-layer vector retrieval with prioritized GaussVector support, and openJiuwen.

    [View Features →](function/function.md) · [Semantic Service →](semantic_service/semantic-service-user-guide.md) · [openJiuwen →](openJiuwen/openJiuwen-user-guide.md)

-   **Architecture**

    Learn about overall architecture, module relationships and key process design.

    [View Architecture →](design_doc/design_doc.md)

-   **API Design**

    Learn about key interfaces and integration methods for secondary development.

    [View API Design →](api_doc/api_doc.md)

-   **Use Cases**

    Build a dedicated NL2SQL Agent, build a data analysis Agent, and related tutorials and best practices.

    [View Use Cases →](case/case.md)

-   **Milestone**

    View release planning and roadmap.

    [View Milestone →](milestone/milestone.md)

-   **Reference**

    View common references, versions and contribution guidelines.

    [View Reference →](explain/explain.md)

</div>
