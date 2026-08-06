---
hide:
  - navigation
---

## Core Features

| Feature | Description |
|-|-|
| **Configurable Agent Framework** | Built on the `CBB` foundation, DataAgent provides common Agent / Node / Router / State capabilities and supports one-step startup from YAML configuration. Configuration loading and overrides follow a layered strategy across default configuration, user configuration, and `.env` values, managed through `Config Manager` for stable reuse across environments and scenarios. |
| **Composable ReAct Framework** | For exploratory reasoning and multi-step tool use, `Flex` uses a ReAct-style architecture and supports configurable `Pre / Actor Loop / Post` workflows. |
| **Scenario Coverage and Custom Extension** | DataAgent covers scenarios such as NL2SQL data queries and main Agents calling NL2SQL sub-agents. It includes NL2SQL capabilities for natural language understanding, SQL generation, validation, execution, and explanation/output. Custom Agents can also be extended through configuration or `AgentBuilder`. |
| **Unified Tool Access and Knowledge Retrieval** | The tool layer focuses on local function tools and gym env tools, registered and invoked through `ToolManager`. The NL2SQL `Perceptor` organizes semantic-service metadata and knowledge into a retrievable perception layer for reasoning and execution. |
| **Runtime Boundary Notes** | Agent behavior boundaries are described through scenario prompts, tool descriptions, and workflow node descriptions. The current mainline does not expose a user-facing reward engine, constraint reasoning engine, or standalone RewardManager configuration entry. Evaluation capabilities have been migrated to a separate project. |
| **Context and Trace Management** | The framework consolidates session logs, business metadata, knowledge, and tool information into a shared persistence system. It can connect to external storage such as `ElasticSearch` / `GaussVector` / `PostgreSQL`, and supports vector search, full-text search, and graph relationship queries. `Context` handles context and trace management, state extraction and persistence, and maintains DAG and IR structures for traceability and replay. |

## Core Modules

| Module | Description |
|-|-|
| **NL2SQL** | Dedicated capability for natural language to SQL execution. |
| **Semantic Service** | NL2SQL-oriented enriched metadata REST capabilities at the current stage, prioritizing GaussVector-oriented semantic-layer enhancements for vector indexing, recall ranking, and schema perception across tables, columns, metric definitions, and business descriptions; Ontology service capabilities are under development. See [Semantic Service User Guide](../semantic_service/semantic-service-user-guide.md). |
| **Perceptor** | Retrieval and perception capabilities for organizing tool information, metadata, and knowledge. |
| **Config Manager** | Configuration management, including configuration modification and loading. |
| **CBB** | Core foundation abstraction defining base classes for Agent, Node, Router, State, and related concepts. |
| **Context** | Context and trace management, including state extraction and persistence, plus DAG and IR maintenance. |
| **Framework Adapters** | Adapters for execution backends and storage, including checkpoint mechanisms. |
| **Managers** | Unified management for LLM, Prompt, and Action; does not include a user-facing reward engine. |
| **Interface** | External interface layer, including SDK and REST service entry points. |
| **Evolution** | Training and evolution-related code, including some environments and training scripts. |
| **Tests** | Unit tests and end-to-end test cases covering workflows, tools, and interfaces. |

---

## Tool Support

| Tool Support Feature | Description |
| --- | --- |
| **Unified Management Entry** | DataAgent manages tools through a per-Agent `ToolManager`, supporting registration, invocation, and result wrapping. |
| **Tool Types** | Local Python functions (`TOOLS.local_functions` / optional builtin) and gym env tools (e.g. `SQLiteEnv` `@Env.tool`) |
| **Unified Form** | Tools are registered as unified instances with a shared schema and call entry. |

### Tool Loading and Usage Flow

| Stage | Description |
| --- | --- |
| Agent Initialization | When Flex builds `AgentEnv`, it creates `ToolManager(config_manager=agent.config)` and calls `init_from_config(config)` to register YAML local tools and implicit job/HITL tools when enabled. |
| Tool Invocation | Use `list_tools` / `get_schema` for metadata, then invoke via the tool's `call` method. |
| Upper-Layer Usage | Callers declare tool name and parameters; `ToolManager` handles routing. |

---

### Example Configuration

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

Note: the mainline no longer ships an MCP client or A2A tool stack. Use local functions / gym env tools only. The prefab builtin catalog is empty by default; enable file/bash tools via YAML when needed.

---

### Configuration Fields

| Type | Required Fields | Field Description |
| --- | --- | --- |
| Local Tool Configuration | `module`, `function` | `module`: Python module path containing the tool function.<br>`function`: function name.<br>`category`: tool category, used for grouping and filtering.<br>`description`: optional field. Currently, only `sub_agent_tool` merges this field into the tool description as supplemental guidance. Other local function tools use the function docstring as the tool description by default.<br>`config`: extended configuration, such as tool-specific parameters. |

---

### Naming Recommendations

| Recommendation | Description |
| --- | --- |
| Clear Semantics | Names should express "action + object" and avoid being too short or too generic. |
| Avoid Duplicates | Tool names across different sources should avoid duplication to prevent routing ambiguity. |
| Keep Stable | Once a tool is externally used, keep its name stable whenever possible to avoid affecting callers. |

---

## Model Support

| Module | Description |
| --- | --- |
| Unified Management Entry | DataAgent uses `LLMManager` to manage model instance creation and caching. Model configuration comes from the YAML `MODEL` section. |
| Initialization Flow | During initialization, the system iterates through each section under `MODEL` in the YAML file and creates the corresponding model instance for Agents and workflows. |
| Backend Selection | Controlled by `AGENT_CONFIG.backend` (currently `langgraph` only). LLM calls use the OpenAI-compatible / LiteLLM path. |
| Provider Semantics | `provider` is a platform identifier used to read `{PROVIDER}_BASE_URL` and `{PROVIDER}_API_KEY`, such as `deepseek`, `bailian`, `openai`, or `embedding`. |

### Usage: YAML Configuration

Models are configured under `MODEL`. Each section represents one model instance configuration block.

### YAML Structure

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

### MODEL Configuration

| Item | Description |
| --- | --- |
| MODEL Field Meaning | `name`: model instance name.<br>`provider`: platform identifier used to look up environment variables.<br>`model_type`: model type, `chat` or `embedding`.<br>`params`: parameters passed to the underlying SDK, such as `model`, `base_url`, `api_key`, `temperature`, and `max_tokens`. |
| Required Fields | `name` (globally unique model instance name), `provider` (provider identifier), `model_type` (`chat` or `embedding`), and `params` (model initialization parameters, which must include `model`). |
| General `params` Requirements | Must include at least `model`; `api_key` must be provided either in YAML or through environment variables; compatible interfaces need `base_url`. |

### Notes

1. **At least one chat model**: the system prefers a model with `model_type=chat` as the default model.
2. **Name uniqueness**: duplicate `name` values overwrite existing instances. Avoid duplicates. The current code has a compatibility fallback for configurations without `name`: it uses the section name under `MODEL` as the model instance name. Explicitly setting `name` is recommended.
3. **API key lookup**: `MODEL.<section>.params.api_key` is used first. If it is not configured, the system looks up `{PROVIDER}_API_KEY` by `provider`.
4. **Base URL lookup**: `MODEL.<section>.params.base_url` is used first. If it is not configured, the system looks up `{PROVIDER}_BASE_URL` by `provider`.
5. **Backend engine selection**: `provider` no longer selects the SDK. `AGENT_CONFIG.backend` currently supports LangGraph only.
