# Action Management Module Design

## 1. Overview

### 1.1 Functional Description

The Action Management module is the **action space gateway** of DataAgent, responsible for unified management of all tools (Actions/Tools) callable by the Agent, including:

- **Local Function Tools**: Tools that directly call Python functions
- **MCP Remote Tools**: Remote tools exposed via the Model Context Protocol
- **A2A Remote Tools**: Other agent tools exposed via the Agent-to-Agent protocol

The module shields upstream components such as Planner and Executor from the differences in underlying tool sources and protocols, providing unified **register / discover / call / query** interfaces. Upstream only needs to know "what the tool can do" without worrying about "where the tool is and how to communicate."

---

## 2. Design Description

### 2.1 Design Principles

**Per-Agent Isolation**

Each agent instance has an independent tool management context, internally aggregating multiple types of tool registries. Tool collections, discovery caches, and Schema caches between different agents are completely isolated and do not pollute each other.

**Unified Tool Abstraction**

All tools from any source — whether local functions, MCP services, or A2A agents — are wrapped into a unified tool abstraction instance upon registration or discovery. This abstraction defines the call interface, Schema retrieval, parameter validation, and interoperability with the LangChain ecosystem, allowing upstream to not differentiate tool sources.

**Schema and Metadata-Driven**

Each tool generates a structured Schema description upon registration or discovery, containing the tool name, parameter list (name, type, required, default), tool description, etc. Through Schema, the module can:
- Generate OpenAI-compatible tool definitions for LLM function calling;
- Provide structured tool information for frontend management interfaces;
- Perform parameter validation before invocation.

**Multi-Source Tool Discovery and Lazy Loading**

Remote tools (MCP/A2A) support two discovery timings:
- **Full discovery at startup**: During agent initialization, perform a batch tool discovery for all registered remote services;
- **On-demand lazy loading at runtime**: When a tool is first accessed via a prefixed name (e.g., `server_id.tool_name`), if the server's tools have not been cached locally, trigger an incremental discovery for that specific server.

Lazy loading is made idempotent by a "discovery attempted" cache — the same remote service will not repeatedly trigger discovery requests, nor will it retry on discovery failure.

**Configuration-Driven and Decoupled**

Tool management module initialization is driven by the agent configuration file. The config file declares local function lists, MCP server lists, A2A agent lists, etc. The module parses the config and automatically completes registration of the three tool types and discovery of remote tools. Upstream only needs to maintain the tool configuration section in YAML; the Flex runtime automatically initializes the action space when building the agent environment.

---

### 2.2 Module Structure

#### 2.2.1 Core Components and Responsibilities

- **Tool Manager (Per-Agent Entry Point)**
  - Serves as the tool operation entry for each agent instance, responsible for:
    - Maintaining the current agent's tool instance cache and Schema cache;
    - Holding references to the local tool registry, MCP registry, A2A registry;
    - Executing configuration-driven initialization, remote tool discovery, lazy loading dispatch;
    - Exposing unified call interfaces (sync/async) and query interfaces (tool listing, Schema retrieval, LLM tool definitions, categorized summaries, etc.).

- **Unified Tool Abstraction**
  - Defines the contract that all tool instances must fulfill: call execution, Schema exposure, LangChain-compatible conversion, parameter validation;
  - Source-specific tool subclasses implement the concrete adaptation logic (e.g., MCP tools convert JSON Schema to internal Schema, A2A tools handle HTTP calls and result parsing).

- **Local Tool Wrapper**
  - Wraps ordinary Python callables (functions) into the unified tool abstraction, automatically generating parameter Schema from function signatures and type annotations.

- **Registry Components**
  - Local Tool Registry: Manages tool-name-to-tool-instance mappings, supporting registration, deregistration, and category-based queries;
  - MCP Registry: Manages MCP server registration, connection, health checks, and tool list retrieval;
  - A2A Registry: Manages remote agent registration, connection, authentication, and tool list retrieval.

#### 2.2.2 Key Data Structures

- **Tool Instance Cache**: `tool_name → tool_instance` mapping. Key naming rules:
  - Local tools: `"tool_name"` (e.g., `"bash"`, `"read_file"`)
  - MCP tools: `"{server_id}.{tool_name}"`
  - A2A tools: `"{agent_id}.{tool_name}"`

- **Tool Schema Cache**: `tool_name → Schema` mapping. All query operations are based on this cache, avoiding repeated Schema generation.

- **Discovery Cache**: Records remote service identifiers (server/agent IDs) for which tool discovery has been attempted, ensuring lazy loading idempotency and preventing repeated requests to failed remote services.

- **Tool Type Enum**: Distinguishes four tool source categories: `Local Function`, `MCP Tool`, `A2A Tool`, and `Custom`.

---

### 2.3 Key Flows

#### 2.3.1 Local Tool Registration Flow

1. The agent config file's tool section declares local function tools, specifying module path, function name, etc.;
2. During initialization, the module dynamically imports callable objects from specified modules based on the config;
3. If the object is an ordinary callable, it is wrapped by the local tool wrapper into a unified tool abstraction; if it is already a subclass of tool abstraction, it is directly instantiated;
4. Wrapped tool instances are written to the local tool registry, and Schemas are generated and written to the cache.

#### 2.3.2 MCP Server Registration and Tool Discovery

1. **Registration Phase**: MCP servers are declared through config files (including service identifier, transport type, launch command / connection parameters), managed by the MCP registry for server connection configuration;
2. **Discovery Phase**: Triggered either via explicit invocation or on-demand in the lazy loading path. The MCP registry pulls the tool list from the target server, wraps remote tool definitions into unified tool abstractions, and writes them to the current agent's tool instance cache and Schema cache.

#### 2.3.3 A2A Agent Registration and Tool Discovery

1. **Registration Phase**: Remote A2A agents are declared through config files (including agent identifier, service address, auth token, timeout, etc.), managed by the A2A registry for connection info;
2. **Discovery Phase**: Similar to MCP, remote agent tool lists are pulled via explicit or lazy loading and wrapped into the local cache.

#### 2.3.4 Configuration-Driven Initialization Flow

1. DataAgent reads and merges config at build time;
2. When the Flex runtime builds the agent environment, it creates an independent tool manager instance for each agent and passes in its config section for initialization:
   - Parse **local function list**: Import modules and functions one by one, register sequentially;
   - Parse **MCP server config**: Register servers, optionally enable auto-discovery;
   - Parse **A2A agent config**: Register remote agents, optionally enable auto-discovery;
   - Parse **builtin tools**: Register preset commonly-used local tools (file read/write, command execution, etc.);
   - Parse **Skills**: Discover and load builtin and user-defined skill metadata.
3. If MCP or A2A services are registered, the initialization phase triggers full tool discovery, bringing remote tools into the current agent's tool cache.

#### 2.3.5 Tool Call and Query Flow

- **Call**:
  - Obtain the tool instance by tool name (this process may trigger lazy loading);
  - Call the tool instance's execute method, passing parameters;
  - If it is an async call and the tool supports async, take the async path; otherwise fall back to sync execution (running in a thread pool);
  - Return a unified result structure (including success flag, data, error info, error type, and retry policy).

- **Query**:
  - **List tools by criteria**: Support filtering by category and tool type;
  - **Get Schema**: Return the complete parameter Schema for a specified tool from cache;
  - **Get LLM tool definitions**: Batch-convert specified tool Schemas to OpenAI function calling format;
  - **Get tool details / summary / health status**: Used for diagnostics, monitoring, and admin UI display.

#### 2.3.6 Error Handling and Retry

- Tool execution exceptions are uniformly classified (parameter validation errors, network errors, timeouts, rate limiting, internal errors, etc.) and associated with preset retry policies (whether retryable, max retries, backoff method);
- On call failure, a unified tool exception is raised. Upstream can decide whether to retry based on the error type and retry flag carried by the exception.

---

## 3. Specifications and Constraints

1. **Tool Naming Convention**
   - Local tool names must be unique within a single agent's tool manager instance;
   - MCP/A2A tools must use the `"{service_id}.{tool_name}"` prefixed naming format to avoid conflicts with local tools.

2. **Lazy Loading and Cache Constraints**
   - Lazy loading is only triggered on first access to a remote service identifier; subsequent accesses are cache hits;
   - On discovery failure, the service identifier is marked as "attempted", preventing further retry attempts and avoiding frequent requests to unreachable remote services.

3. **Thread Safety and Lifecycle**
   - Each agent's tool manager instance holds tool registries for the agent's full lifecycle;
   - Tool registration and discovery are typically completed during the agent startup phase, avoiding concurrent modification during runtime;
   - Resource cleanup (closing remote connections, clearing caches) is triggered on agent destruction.

4. **Exception Handling**
   - Missing tool or Schema lookups uniformly raise a tool exception handled by upstream;
   - Remote service unreachability or discovery timeout exceptions are silently handled and marked as failed during lazy loading; callers perceive it through tool existence or call success.

5. **Configuration Compatibility**
   - The tool manager supports multiple versions of configuration formats, maintaining backward compatibility with legacy config fields;
   - Tool declarations in config files are recommended to explicitly specify tool names to avoid coupling with function name changes.
