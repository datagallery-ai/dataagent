---
hide:
  - navigation
---

<div style="text-align: center;" markdown>

# 🚀 DataAgent
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
| 🔌 **Plugin Tool Ecosystem** | Local Python tools and gym env tools (e.g. SQLiteEnv) register through `ToolManager`; YAML `TOOLS.local_functions` / optional builtin overrides |
| 📡 **Agent Composition** | Flex + NL2SQL with optional sub-agent / job tools for multi-step workflows; REST and Python SDK as northbound entrypoints |
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

    Choose `uv` / `pip` installation, environment setup and model configuration; for NL2SQL prefer Semantic Service / GaussVector; MySQL / PostgreSQL / Elasticsearch are optional for extended scenarios only.

    [Start Installation →](installation/installation.md) · [Database Installation →](installation_doc/database_install/database_install.md)

-   **Quick Start**

    Run examples and quickly get the end-to-end pipeline working.

    [Quick Start →](quick_start/quick_start.md)

-   **Features**

    Learn about core capabilities, module structure, tools and model support; includes Semantic Service and semantic-layer vector retrieval with prioritized GaussVector support.

    [View Features →](function/function.md) · [Semantic Service →](semantic_service/semantic-service-user-guide.md)

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
