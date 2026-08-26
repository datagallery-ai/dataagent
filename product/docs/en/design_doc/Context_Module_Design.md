# Context Module Design

The Context module records a single agent run's problems, tool calls, intermediate states, and dependencies between data objects. It primarily serves internal node scheduling, trajectory replay and debugging, while also providing history restoration for multi-turn conversations.

## 1. Design Points

### 1.1 Instance Isolation, Not a Global Singleton

Context is not a global singleton but is managed per-instance using the tuple of "user + session + run round + sub-agent". Only one instance exists per tuple within a process, and different agents (main agent vs. sub-agents) hold independent trajectory records. Instance creation and retrieval uses the factory pattern with internal thread safety.

### 1.2 Trajectory Graph Model

Trajectories use a directed acyclic graph:

- **Nodes**: Divided into computation nodes and data nodes. Computation nodes include user questions, agent reasoning conclusions, and tool call actions; data nodes include knowledge snippets, tool definitions, data tables, data columns, files, scripts, skill packages, and other intermediate artifacts. Ten node types are supported in total.
- **Edges**: Represent relationships between nodes. Main types include trigger edges (question or conclusion triggers action), output edges (action produces conclusion or data), and continuation edges (cross-round). Edge types can be explicitly specified by the caller or automatically inferred by the system based on predecessor and current node types.
- **Active Branch Pointers**: Maintain the set of current trajectory endpoints for identifying focus areas in subsequent planning and debugging. When new nodes are added, pointers can be advanced (moved forward) or branches can simply be appended.

### 1.3 Cross-Round Trajectory Bridging

Multiple agent runs within the same session can be connected into a complete trajectory via bridging edges:

- Restore historical trajectory graphs from database or local files.
- Between adjacent rounds, connect leaf nodes (zero out-degree) from the previous round to the starting query node of the next round, with the edge type marked as "continuation".
- When the current round registers a new query, it is automatically bridged to the tail of the most recent historical round.
- The result is a unified DAG rooted at the session's first query, spanning all rounds.

### 1.4 Intermediate Representation and Auto-Portrait

Each trajectory node corresponds to a structured intermediate representation containing identity, description, attribution information, creation timestamp, and historical version records.

Data nodes have auto-portrait capability: the system asynchronously invokes large models to infer and generate natural language descriptions based on the node's predecessor actions and conclusion context. This process does not block the main flow; portrait tasks execute in the background and are uniformly awaited before streaming output ends.

Modifications to node attributes trigger version recording — before each modification, the current value is stored in a historical version dict, ensuring trajectory backtracking and auditing capability.

### 1.5 Workflow Queues

The module has three built-in queues for managing workflow nodes during agent execution:

- **Pre Queue**: Loaded from agent config, initialized at run startup, defines steps that must complete before entering the main loop.
- **Todo Queue**: Steps dynamically added and consumed during execution, the core driving force of the main loop.
- **Post Queue**: Loaded from agent config, defines cleanup steps to execute after the main loop ends.

Each queue has a capacity limit to prevent unbounded growth. Queues are consumed in FIFO order.

### 1.6 Message Storage and Context Formatting

- Supports persistence and restoration of standard dialogue message types (user messages, assistant messages, tool return messages, system messages) using a unified serialization format.
- Provides message cleaning: automatically filters system messages and removes orphaned tool call records (where the assistant declared a tool call but the corresponding tool return is missing), ensuring replay safety without polluting the context window.
- Provides context formatting: converts message lists into compact readable text blocks, skips initial user query during display, and simplifies tool call parameters (retaining only names and arguments, discarding internal identifiers), reducing downstream reasoning context length pressure.

## 2. Persistence Strategy

The module supports dual-channel persistence:

- **Local Files**: Each round generates two files — a complete trajectory graph (containing nodes and edges) and lightweight metadata (containing the current round's starting node identifier and active branch endpoint set), stored under the session root directory.
- **Relational Database**: All node types have corresponding database tables; edge relationships also have independent storage tables. Supports query and restore by user, session, round, and sub-agent dimensions. Database and tables are auto-created on first use. Suitable for scenarios requiring long-term trajectory preservation across sessions.

When writing, only the current round's own nodes and edges are persisted. Cross-round bridge edges and historical round nodes are outside the persistence scope — they do not belong to the current round's data and are reconstructed by the restore logic on the next load.

## 3. Runtime Relationship

The agent runtime carries context identification information in the state and obtains the corresponding Context instance through the factory interface. Ordinary users simply initiate conversations through the SDK entry point without manually managing context.

Typical execution chain:

1. User initiates a conversation through the SDK entry point.
2. The runtime prepares state and context identification for this round's request.
3. If this is a subsequent round in the same session, automatically restores historical trajectories from persistent storage and bridges to the current query.
4. Internal nodes such as Planner and Executor write messages, tool calls, and trajectory information during execution.
5. At the end of the run, the current round's trajectory and messages are persisted to the configured storage backend.
6. Final state is returned to the user, containing messages and statistics.

## 4. Usage Boundaries

- In normal usage scenarios, obtain results through the SDK-provided conversation interfaces without directly operating on Context.
- When developing internal nodes for extension, Context can be used to record replayable trajectories: register query entry points, register intermediate nodes (specifying predecessors and edge types), modify node attributes, advance active branch pointers.
- Trajectory graphs can be obtained for visualization export or pruning (keeping only active branch paths).
- External code is not recommended to directly modify trajectory graph structure unless the semantic constraints of node types and edge relationships are clearly understood.
- Async tasks inside Context are uniformly awaited at the end of streaming output; callers do not need to manage them manually.
