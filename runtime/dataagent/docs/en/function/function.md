---
hide:
  - navigation
---

# Features

## Native Agent runtime

The main DataAgent is a Deep Agents 0.7.5 `CompiledStateGraph` running on LangGraph. Deep Agents owns the reasoning/tool loop, filesystem tools, state shape, checkpointer protocol, store protocol, subagent middleware, skills, and summarization behavior. DataAgent adds a compatibility compiler around the existing YAML boundary instead of maintaining a second Agent runtime.

The public SDK returns native LangGraph state and stream events. LangGraph runtime configuration, including tags, metadata, recursion limits, and checkpoint fields, can be passed through the SDK.

## Compatible YAML compiler

The compiler currently maps these configuration areas:

| Area | Native target |
| --- | --- |
| `MODEL` | LangChain `BaseChatModel` instances with primary-model selection and fallback |
| `TOOLS.local_functions` | Native LangChain structured tools |
| `TOOLS.mcp_servers` | Official `langchain-mcp-adapters` tools |
| `TOOLS.A2A` | Asynchronous tools discovered from remote AgentCards |
| `TOOLS.skills` | Deep Agents native skills and read-only skill mounts |
| `WORKSPACE` | Filesystem, composite, or state backend plus permissions |
| `SUBAGENTS` | Deep Agents compiled subagents loaded from YAML |
| `NL2SQL` | One dedicated inline native NL2SQL subgraph |
| `HOOKS` | LangChain agent/model/tool middleware hooks |
| `CONTEXT` | Deep Agents summarization thresholds |
| `SCENARIO.chat` | System-prompt instructions, appends, and HITL conditions |
| `SUITE` | ConfigManager expansion into ordinary YAML before compilation |

The previous configurable workflow-node topology is not part of the native runtime. Existing `backend: langgraph` and `type: react` values remain accepted so user YAML does not need a needless rename.

## Models and middleware

Provider names are case-insensitive. Native LangChain providers are created through `init_chat_model`; OpenAI-compatible providers use `ChatOpenAI`. The default primary model slot is `chat_model`, and `AGENT_CONFIG.primary_model` can select another slot.

The default middleware stack provides:

- tool exception conversion into model-readable tool errors;
- model retry using LangChain's native policy;
- model fallback when more than one chat model is configured;
- Shell execution for filesystem workspaces, with a 600-second command timeout;
- optional Shell command allowlisting;
- Todo tracking;
- automatic summarization and explicit summarization-tool support;
- optional maximum model-call count through `AGENT_CONFIG.max_iter`;
- configured agent, model, and tool hooks.

## Tools

All active tool sources become LangChain `BaseTool` objects before `create_deep_agent` is called. Local tools can be synchronous or asynchronous and may request a compatible `_tool_context`. MCP servers are discovered through the official adapter. A2A AgentCard skills become asynchronous tools that use the A2A client directly.

Deep Agents' native filesystem tools are used instead of duplicated DataAgent wrappers. Shell is available only when the backend has a real host directory. `WORKSPACE.backend: state` therefore rejects explicit Shell configuration.

## Workspace and skills

The default writable workspace is `.dataagent/<user_id>/<session_id>`. A configured absolute `WORKSPACE.path` overrides it. Additional `WORKSPACE.allow_path` directories are mounted read-only through a composite backend.

Built-in, custom-directory, and per-user skills are normalized to Deep Agents skill sources. The Agent can read skill files but cannot rewrite the mounted skill library.

## Subagents and NL2SQL

`SUBAGENTS` accepts complete child-Agent YAML files and compiles each to a runnable subgraph. Models may be inherited from the parent or declared by the child. Recursive YAML references and duplicate identifiers are rejected.

`NL2SQL` is a dedicated inline configuration for one default NL2SQL child. It compiles the existing NL2SQL nodes to a native LangGraph graph and gives Deep Agents the runnable directly. Parent database and semantic-layer connection settings remain centralized.

## Human feedback and hooks

When enabled, `request_human_feedback` interrupts the graph through LangGraph's native `interrupt()` mechanism. Configured `human_feedback_conditions` are appended to the system prompt as explicit HITL conditions.

`HOOKS.agent.pre/post` surrounds an Agent invocation. `HOOKS.model.pre/post` surrounds each model call in the main Agent loop. A model call made internally by a tool is outside that loop and does not trigger model hooks. Tool hooks wrap only the tagged local, MCP, or A2A tool source where they are configured.

## Retained migration-stage systems

Context, jobs, resource runtime/resources, governance, dataops, semantic recall, document recall, NL2SQL, and the `data_analysis` Suite remain in the repository. They are being retained deliberately while their integration boundary is migrated; they are not evidence that the removed main Agent runtime is still active.

For configuration examples and precise interfaces, see [Python SDK and YAML Reference](../api_doc/PythonSDK.md).
