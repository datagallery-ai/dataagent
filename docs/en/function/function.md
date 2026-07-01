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
| **Unified Tool Access and Knowledge Retrieval** | The tool layer supports local function tools, `MCP` / `A2A` external tools, and external Agents through a shared registration and invocation mechanism. It supports automatic discovery and on-demand loading. The `Perceptor` module organizes tool information, metadata, and knowledge into a retrievable perception layer, helping the Agent select tools more accurately during reasoning and execution, and persisting tool metadata into memory for later reuse. |
| **Runtime Boundary Notes** | Agent behavior boundaries are described through scenario prompts, tool descriptions, and workflow node descriptions. The current mainline does not expose a user-facing reward engine, constraint reasoning engine, or standalone RewardManager configuration entry. Evaluation capabilities have been migrated to a separate project. |
| **Context and Trace Management** | The framework consolidates session logs, business metadata, knowledge, and tool information into a shared persistence system. It can connect to external storage such as `ElasticSearch` / `GaussVector` / `PostgreSQL`, and supports vector search, full-text search, and graph relationship queries. `Context` handles context and trace management, state extraction and persistence, and maintains DAG and IR structures for traceability and replay. |

## Core Modules

| Module | Description |
|-|-|
| **NL2SQL** | Dedicated capability for natural language to SQL execution. |
| **Semantic Service** | NL2SQL-oriented enriched metadata REST capabilities at the current stage, prioritizing GaussVector-oriented semantic-layer enhancements for vector indexing, recall ranking, and schema perception across tables, columns, metric definitions, and business descriptions; Ontology service capabilities are under development. See [Semantic Service User Guide](../semantic_service/semantic-service-user-guide.md). |
| **openJiuwen** | openJiuwen integration and usage. See [openJiuwen User Guide](../openJiuwen/openJiuwen-user-guide.md). |
| **Perceptor** | Retrieval and perception capabilities for organizing tool information, metadata, and knowledge. |
| **Config Manager** | Configuration management, including configuration modification and loading. |
| **CBB** | Core foundation abstraction defining base classes for Agent, Node, Router, State, and related concepts. |
| **Context** | Context and trace management, including state extraction and persistence, plus DAG and IR maintenance. |
| **Framework Adapters** | Adapters for execution backends and storage, including checkpoint mechanisms. |
| **Managers** | Unified management for LLM, Prompt, and Action; does not include a user-facing reward engine. |
| **Interface** | External interface layer, including CLI, SDK, and service entry points. |
| **Evolution** | Training and evolution-related code, including some environments and training scripts. |
| **Tests** | Unit tests and end-to-end test cases covering workflows, tools, and interfaces. |

---

## Tool Support

| Tool Support Feature | Description |
| --- | --- |
| **Unified Management Entry** | DataAgent manages tool capabilities through an independent `ToolManager` for each Agent, supporting registration, discovery, invocation, and result wrapping. |
| **Tool Types** | Local Python functions, including built-in tools and user-defined functions<br>A2A external Agents<br>MCP external service calls |
| **Unified Form** | Regardless of tool type, tools are eventually registered as unified tool instances in the tool manager, with a shared schema description and invocation entry. |

### Tool Loading and Usage Flow

| Stage | Description |
| --- | --- |
| Agent Initialization | When the Flex runtime builds `AgentEnv`, it creates `ToolManager(config_manager=agent.config)` and calls `init_from_config(config)` to register built-in tools, local tools declared in YAML, A2A tools, and MCP tools. This process normalizes all tool sources into structured tool representations: tool name, tool description, and tool parameters. |
| Tool Invocation | When a tool needs to be called, the tool manager's `list_tools` and `get_schema` interfaces can be used to obtain tool metadata, then the tool's `call` member function is invoked through the unified entry. |
| Upper-Layer Usage | At the DataAgent layer, callers only need to declare the tool name and parameters. The concrete routing is handled automatically by the system. |

---

### Tool Type Comparison: Local / A2A / MCP

The following table compares the three tool types side by side for easier lookup.

| Dimension | Local Python Function | A2A External Agent | MCP External Service |
| --- | --- | --- | --- |
| Overview | Local tools run in the current process, with low latency and convenient debugging. They are suitable for wrapping business logic, data processing, or existing Python capabilities. This is the default and lightest tool form. | A2A connects to external Agents through the protocol and automatically discovers exposed capabilities such as skills or tools. These capabilities are mapped into callable tools, making A2A suitable for cross-system and cross-team capability reuse. | MCP connects to external tool services and supports both stdio and sse transports. It is suitable for standalone services, cross-language tools, or capabilities that require runtime isolation. |
| Configuration Entry | Loaded from the `TOOLS.local_functions` list. Each item declares a module and function name. Tests or scripts can also call `ToolManager.register_local_tool` directly. | `TOOLS.A2A`, where each agent uses `agent_id` as the key. | Recommended: `TOOLS.mcp_servers`, specifying `server_id`, `transport_type`, and `config`. |
| Required Fields | `module`, `function` | Agent key, `base_url` | `server_id`, `config` |
| Notes | - | 1. Availability depends on whether the remote Agent is online and whether its AgentCard is complete.<br>2. A2A calls are forwarded through natural language, so parameters should be clear and semantically explicit.<br>3. Remote tool names may conflict with local tool names, so naming conventions are recommended. | 1. Transport choice: stdio is suitable for local subprocess services; sse is suitable for remote HTTP services.<br>2. Connections and resources: stdio requires attention to subprocess lifecycle and resource cleanup.<br>3. Result content type: MCP tools may return text, images, or other content, and upper layers should decide how to display them.<br>4. Service stability: configure timeouts and retries to avoid unstable remote services affecting the workflow. |

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

Note: required fields under `config` depend on `transport_type`: `stdio` must include `command`; for `sse`, the current runtime connects through `url`, so configuring `url` directly is recommended.

---

### Configuration Fields

The following table summarizes common tool-related configuration concepts. Exact fields should follow the project configuration.

| Type | Required Fields | Field Description |
| --- | --- | --- |
| Local Tool Configuration | `module`, `function` | `module`: Python module path containing the tool function.<br>`function`: function name.<br>`category`: tool category, used for grouping and filtering.<br>`description`: optional field. Currently, only `sub_agent_tool` merges this field into the tool description as supplemental guidance. Other local function tools use the function docstring as the tool description by default.<br>`config`: extended configuration, such as tool-specific parameters. |
| A2A Agent Configuration | Agent key, `base_url` | Agent key: unique identifier of the remote agent, for example `TOOLS.A2A: - web_searcher: ...`.<br>`base_url`: access URL.<br>`auth_token`: optional authentication token.<br>`timeout`: timeout in seconds. |
| MCP Service Configuration | `server_id`, `config` | `server_id`: MCP service identifier.<br>`transport_type`: `stdio` or `sse`, defaulting to `stdio`.<br>`config`: common stdio fields include `command`, `args`, `env`, and `cwd`; for sse, configuring `url` is currently recommended.<br>`category / description`: used for categorization and display. |

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
| Backend Selection | Controlled by `AGENT_CONFIG.backend`: `langgraph` uses the OpenAI-compatible / LiteLLM call path; `openjiuwen` uses the OpenJiuWen Provider, mainly through an OpenAI-compatible interface. |
| Provider Semantics | `provider` is a platform identifier used to read `{PROVIDER}_BASE_URL` and `{PROVIDER}_API_KEY`, such as `deepseek`, `bailian`, `openai`, or `embedding`. When `backend=langgraph`, the OpenAI-compatible client path is used. When `backend=openjiuwen`, the OpenJiuWen Provider is used. |

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
5. **Backend SDK selection**: `provider` no longer selects the SDK. `AGENT_CONFIG.backend` decides whether LangGraph/OpenAI-compatible or OpenJiuWen is used.
