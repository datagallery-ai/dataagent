# Planning Engine Module Design

Current general-purpose data tasks are primarily handled by the Flex/ReAct engine. Users configure models, scenario descriptions, workflow nodes, and tools via YAML. At runtime, the Planner and Executor alternate in a "plan-execute-observe-continue planning" loop.

## 1. Configuration Entry

```yaml
AGENT_CONFIG:
  name: "data agent"
  backend: "langgraph"
  type: "react"

PRE_WORKFLOW: []

ACTOR_LOOP:
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "chat_model"
  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"

POST_WORKFLOW: []
```

`type: "react"` selects the current Flex/ReAct implementation. `Planner` in `ACTOR_LOOP` generates the next action or final answer, while `Executor` executes tool calls and writes results back to state.

## 1. Overall Architecture

The planning engine is built on a directed graph workflow consisting of three sequential phases:

| Phase | Description |
|-------|-------------|
| Pre Workflow | A set of fixed nodes executed before entering the main loop, typically for environment preparation or context initialization |
| Main Loop | The core ReAct loop, where planning and execution nodes alternate |
| Post Workflow | Cleanup nodes executed after the main loop ends, for result finalization or resource release |

In most scenarios, pre and post workflows remain empty; only the standard main loop nodes need configuration.

## 2. Configuration-Driven

Users declare all agent behavior through YAML declarative configuration. The entry configuration includes the following key sections:

- **Agent Base Config**: Specifies backend engine type (e.g., LangGraph), run mode, debug toggle, etc.
- **Scenario Description**: Defines the agent's role identity, task boundary, behavioral constraints, and output specification. This is the primary entry point for tuning agent behavior.
- **Model Configuration**: Specifies the dialogue models used by each node.
- **Tool and Environment Config**: Declares the available tool set and execution environment.

Main loop nodes are declared in the configuration as a list, with each node specifying its module path and required model binding. The framework constructs routing relationships between nodes in list order.

## 3. Workflow Routing

The router controls transition logic between nodes within the main loop:

- **Loop Routing**: Forms a closed loop between planning and execution nodes. Tool calls produced by the planning node are routed to the execution node; after execution completes, results are returned to the planning node for further reasoning.
- **Termination Judgment**: The loop ends automatically when the model gives a final answer (no more tool calls), the maximum loop rounds are reached, the context length limit is triggered, or an unrecoverable error occurs.
- **Human-in-the-Loop**: Supports inserting human confirmation nodes into the main loop, allowing external feedback to intervene in the agent's decision flow.

The router's core parameters include max iterations and token limit, both of which can be overridden in configuration.

## 4. Planning Node

The planning node is the core of reasoning, responsible for:

- **Prompt Organization**: Assembles scenario instructions, tool catalogs, message history, and runtime state into a structured prompt for the dialogue model.
- **Reasoning and Decision-Making**: The model decides the next action based on current context — calling a tool, or directly providing the final answer.
- **Multi-Format Compatibility**: Automatically handles conversion between different tool call formats, ensuring decoupling from the underlying model.

The planning node's prompt template uses a layered design: the framework's built-in base prompt provides general capability descriptions, and user-added instructions through scenario configuration are injected into reserved slots in the template — an "append only, no replace" extension approach.

## 5. Execution Node

The execution node is responsible for turning tool calls produced by the planning node into actual execution:

- **Unified Tool Interface**: All tools — whether from local functions, MCP services, or A2A agents — are executed through a unified call protocol. The execution node does not care about specific tool origins.
- **Concurrency Control**: Supports concurrent execution of multiple tool calls, with concurrency adjustable via configuration.
- **Parameter Validation**: Validates tool call parameters against schemas to ensure passed values match the expected format defined by the tool.
- **Result Truncation**: Limits the length of overly long tool return content to prevent context bloat.
- **Error Classification**: Categorizes execution exceptions into retryable errors, recoverable errors, and unrecoverable errors, assisting upstream in deciding subsequent strategies.

## 6. Tool System

Tools are registered and discovered by a unified manager. Currently supported sources include:

- **Local Functions**: Directly registered Python functions, wrapped by the framework as callable tools.
- **MCP Services**: External tool services connected via the MCP protocol, supporting both stdio and HTTP communication modes.
- **A2A Agents**: Other agents called as tools by the current agent, enabling inter-agent collaboration.
- **Framework Builtin Tools**: General-purpose tools pre-installed by the system.

All tools provide structured name, description, and parameter definitions, used by the planning node for reference when reasoning about tool selection.

## 7. Runtime State

The engine maintains a unified state object throughout execution, spanning all nodes:

- **Message History**: Complete conversation records, including user input, model responses, and tool call results.
- **Execution Statistics**: Accumulated conversation rounds, effective tool call count, invalid call count, etc.
- **Session Identification**: User, session, run instance, and other associated information.
- **Termination Flag**: Whether the current task has completed.
- **Cross-Session Memory**: Summary information retrieved from historical sessions for context continuity in long-duration tasks.

## 8. Debugging Suggestions

When agent behavior does not meet expectations, prioritize investigating the following:

1. **Scenario Instructions**: Whether the agent's role, boundaries, and termination conditions are clearly constrained. Overly vague instructions cause the model to call tools aimlessly; overly strict instructions may cause premature termination.
2. **Tool Descriptions**: Whether tool names and descriptions accurately reflect their functionality. The model relies on tool descriptions for selection decisions; inaccurate descriptions trigger incorrect calls.
3. **Loop Limits**: Whether max iterations and token limits are set too low, causing task truncation; or too high, causing resource waste.
4. **Tool Returns**: Whether tool return content is concise yet sufficiently informative for the model to accurately judge the next action.
