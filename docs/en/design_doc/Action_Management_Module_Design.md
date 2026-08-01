# Action Management Module Design

## 1. Overview

### 1.1 Functional Description

The Action Management module is the **action space gateway** of DataAgent, responsible for unified management of **local function tools** (Actions/Tools) callable by the Agent.

The module shields upstream components such as Planner and Executor from registration and invocation details, providing unified **register / call / query** interfaces. Upstream only needs to know "what the tool can do" without worrying about how tools are wrapped and validated.

---

## 2. Design Description

### 2.1 Design Principles

**Per-Agent Isolation**

Each agent instance has an independent tool management context. Tool collections and Schema caches between different agents are completely isolated and do not pollute each other.

**Unified Tool Abstraction**

Local functions are wrapped into a unified tool abstraction instance upon registration. This abstraction defines the call interface, Schema retrieval, parameter validation, and interoperability with the LangChain ecosystem.

**Schema and Metadata-Driven**

Each tool generates a structured Schema description upon registration, containing the tool name, parameter list (name, type, required, default), tool description, etc. Through Schema, the module can:

- Generate OpenAI-compatible tool definitions for LLM function calling;
- Perform parameter validation before invocation.

**Configuration-Driven and Decoupled**

Tool management module initialization is driven by the agent configuration file. After the config declares local function lists, the module parses the config and completes registration. Upstream only needs to maintain the tool configuration section in YAML; the Flex runtime automatically initializes the action space when building the agent environment.

---

### 2.2 Module Structure

#### 2.2.1 Core Components and Responsibilities

- **Tool Manager (Per-Agent Entry Point)**
  - Serves as the tool operation entry for each agent instance, responsible for:
    - Maintaining the current agent's tool instance cache and Schema cache;
    - Holding a reference to the local tool registry;
    - Executing configuration-driven initialization;
    - Exposing unified call interfaces (sync/async) and query interfaces (tool listing, Schema retrieval, LLM tool definitions, etc.).

- **Unified Tool Abstraction**
  - Defines the contract that all tool instances must fulfill: call execution, Schema exposure, LangChain-compatible conversion, parameter validation.

- **Local Tool Wrapper**
  - Wraps ordinary Python callables (functions) into the unified tool abstraction, automatically generating parameter Schema from function signatures and type annotations.

- **Local Tool Registry**
  - Manages tool-name-to-tool-instance mappings, supporting registration, deregistration, and category-based queries.

#### 2.2.2 Key Data Structures

- **Tool Instance Cache**: `tool_name → tool_instance` mapping. Local tools use keys like `"bash"` or `"read_file"`.
- **Tool Schema Cache**: `tool_name → Schema` mapping. All query operations are based on this cache, avoiding repeated Schema generation.
- **Tool Type Enum**: The mainline categories are `Local Function` and `Custom`.

---

### 2.3 Key Flows

#### 2.3.1 Local Tool Registration Flow

1. The agent config file's tool section declares local function tools, specifying module path, function name, etc.;
2. During initialization, the module dynamically imports callable objects from specified modules based on the config;
3. If the object is an ordinary callable, it is wrapped by the local tool wrapper into a unified tool abstraction; if it is already a subclass of tool abstraction, it is directly instantiated;
4. Wrapped tool instances are written to the local tool registry, and Schemas are generated and written to the cache.

#### 2.3.2 Configuration-Driven Initialization Flow

1. DataAgent reads and merges config at build time;
2. When the Flex runtime builds the agent environment, it creates an independent tool manager instance for each agent and passes in its config section for initialization:
   - Parse **local function list**: Import modules and functions one by one, register sequentially;
   - Parse **builtin tools** (if enabled): Register preset commonly-used local tools (file read/write, command execution, etc.).

#### 2.3.3 Tool Call and Query Flow

- **Call**:
  - Obtain the tool instance by tool name;
  - Call the tool instance's execute method, passing parameters;
  - If it is an async call and the tool supports async, take the async path; otherwise fall back to sync execution (running in a thread pool);
  - Return a unified result structure (including success flag, data, error info, error type, and retry policy).

- **Query**:
  - **List tools by criteria**: Support filtering by category and tool type;
  - **Get Schema**: Return the complete parameter Schema for a specified tool from cache;
  - **Get LLM tool definitions**: Batch-convert specified tool Schemas to OpenAI function calling format;
  - **Get tool details / summary**: Used for diagnostics and admin UI display.

#### 2.3.4 Error Handling and Retry

- Tool execution exceptions are uniformly classified (parameter validation errors, timeouts, internal errors, etc.) and associated with preset retry policies (whether retryable, max retries, backoff method);
- On call failure, a unified tool exception is raised. Upstream can decide whether to retry based on the error type and retry flag carried by the exception.

---

## 3. Specifications and Constraints

1. **Tool Naming Convention**
   - Local tool names must be unique within a single agent's tool manager instance.

2. **Thread Safety and Lifecycle**
   - Each agent's tool manager instance holds tool registries for the agent's full lifecycle;
   - Tool registration is typically completed during the agent startup phase, avoiding concurrent modification during runtime;
   - Resource cleanup (clearing caches) is triggered on agent destruction.

3. **Exception Handling**
   - Missing tool or Schema lookups uniformly raise a tool exception handled by upstream.

4. **Configuration Compatibility**
   - The tool manager supports multiple versions of configuration formats, maintaining backward compatibility with legacy config fields;
   - Tool declarations in config files are recommended to explicitly specify tool names to avoid coupling with function name changes.
