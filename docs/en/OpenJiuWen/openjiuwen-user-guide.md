# openJiuwen User Guide

This guide explains how DataAgent integrates with openJiuwen. In this project, openJiuwen integration mainly refers to the **openJiuwen runtime backend**, which runs ReAct workflows. Model calls are the same as on the LangGraph backend: they go through `runtime.llm` and litellm.

The openJiuwen version used in this project is:

```text
openjiuwen==0.1.1
```

## 1. When to Use

DataAgent supports two workflow backends:

| Backend | Config value | Description |
| --- | --- | --- |
| LangGraph | `AGENT_CONFIG.backend: "langgraph"` | Default backend; runs workflows with LangGraph. |
| openJiuwen | `AGENT_CONFIG.backend: "openjiuwen"` | Runs ReAct workflows with the openJiuwen workflow engine. |

**Recommended** when:

- You need to run DataAgent ReAct flows on the openJiuwen runtime.
- You want openJiuwen interrupt, resume, and streaming execution.

**Not recommended** when:

- The NL2SQL main path does not require the openJiuwen backend yet—prefer existing `langgraph` examples.
- You assemble low-level nodes manually via `BaseDataAgent.set_base_config()`—that path currently only allows `backend="langgraph"`.

## 2. openJiuwen Features Used

DataAgent integrates openJiuwen for workflow execution, streaming events, and interrupt/resume.

| openJiuwen feature | How DataAgent uses it | User benefit |
| --- | --- | --- |
| openJiuwen Workflow | With `backend: "openjiuwen"`, `OpenJiuWenWorkflow` builds and runs ReAct workflows. | The same DataAgent YAML can switch to the openJiuwen engine. |
| `WorkflowRuntime` | Each `ainvoke` / `astream` creates or reuses an openJiuwen runtime and injects the DataAgent runtime into node execution. | Nodes still use DataAgent Planner, Executor, and tools without caring about openJiuwen runtime details. |
| `Workflow.compile(runtime)` | openJiuwen workflow binds runtime at run time, then compiles. The implementation avoids reusing a compiled graph tied to an old runtime across turns. | `run_id`, `session_id`, workspace, etc. do not leak from the previous turn. |
| `global_state` / `update_global_state` | DataAgent carries `FlexState` in openJiuwen global state and merges deltas after node execution. | Planner, Executor, multi-turn messages, and tool results keep progressing on the openJiuwen backend. |
| Reducer semantics | `messages` append; fields with reducers aggregate; other fields overwrite by default. | Aligns with LangGraph state updates and avoids overwriting message streams. |
| `write_stream` | `astream` hooks openJiuwen `write_stream` and yields node events to the caller via a side queue. | The server can stream model, tool, and interrupt events continuously. |
| `GraphInterrupt` | Human feedback nodes interrupt the workflow via openJiuwen `GraphInterrupt`. | Supports “pause mid-run for user input, then continue” flows. |
| Checkpoint + resume | On interrupt, saves `start_at`, interrupt message, and state; on resume, injects `__human_feedback_resume__` and continues from the interrupt node. | User feedback resumes the same workflow instead of starting a new task. |

These capabilities sit in two layers:

1. **Workflow layer**: `OpenJiuWenWorkflow` wraps DataAgent nodes as openJiuwen components and handles routing, state merge, and runtime.
2. **Service layer**: The server picks LangGraph or openJiuwen from `AGENT_CONFIG.backend`; the openJiuwen branch handles checkpoint restore and streaming.

## 3. Install Dependencies

openJiuwen is optional. Install the `jiuwen` extra before using the openJiuwen backend:

```bash
uv sync --extra jiuwen
```

Or for one-off commands:

```bash
uv run --extra jiuwen python your_script.py
```

`pyproject.toml` pins:

```toml
[project.optional-dependencies]
jiuwen = [
    "openjiuwen==0.1.1"
]
```

## 4. YAML Configuration

### 4.1 Minimal config

Select the openJiuwen backend with `AGENT_CONFIG.backend`:

```yaml
AGENT_CONFIG:
  name: "jiuwen demo agent"
  type: "react"
  backend: "openjiuwen"
  max_iter: 10

MODEL:
  deepseek:
    model_type: "chat"
    provider: "deepseek"
    params:
      base_url: "https://api.deepseek.com"
      model: "deepseek-chat"
      temperature: 0.1

ACTOR_LOOP:
  - node: "planner"
    module: "dataagent.core.flex.nodes.planner.Planner"
    chat_model:
      name: "deepseek"
  - node: "executor"
    module: "dataagent.core.flex.nodes.executor.Executor"

TOOLS:
  local_functions: []
```

To switch backends, change:

```yaml
AGENT_CONFIG:
  backend: "langgraph"
```

to:

```yaml
AGENT_CONFIG:
  backend: "openjiuwen"
```

Planner, Executor, tools, Scenario, Memory, and other settings still use the same ReAct YAML structure.

### 4.2 Model parameters

`provider` under `MODEL` is a **platform id** for environment variables, not an SDK selector:

| Config | Description |
| --- | --- |
| `AGENT_CONFIG.backend` | `langgraph` or `openjiuwen`. |
| `MODEL.<name>.provider` | Reads `{PROVIDER}_BASE_URL`, `{PROVIDER}_API_KEY`. |
| `MODEL.<name>.params.base_url` | Model endpoint; overrides env when set. |
| `MODEL.<name>.params.model` | Model name (required). |
| `MODEL.<name>.params.model_provider` | Passed through to litellm; default `openai`. |

Prefer explicit `base_url` and `model`, with `api_key` in the environment:

```bash
export DEEPSEEK_API_KEY="sk-..."
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

Or in YAML:

```yaml
MODEL:
  deepseek:
    model_type: "chat"
    provider: "deepseek"
    params:
      base_url: "https://api.deepseek.com"
      api_key: "sk-..."
      model: "deepseek-chat"
      model_provider: "openai"
      temperature: 0.1
      timeout: 60
```

## 5. Running

Load openJiuwen YAML via `DataAgent.from_config()`:

```python
import asyncio

from dataagent.interface.sdk.agent import DataAgent


async def main():
    agent = DataAgent.from_config("path/to/jiuwen_agent.yaml")
    result = await agent.chat(
        "Do a simple calculation: multiply 3 by 2, then add 5.",
        initial_state={
            "run_id": 0,
            "sub_id": 0,
            "workspace": "/tmp/dataagent-jiuwen-demo",
        },
    )
    print(result.get("messages", [])[-1])


asyncio.run(main())
```

ReAct on the openJiuwen backend still relies on DataAgent runtime state. Pass `initial_state.workspace`, `run_id`, and `sub_id` in `chat()` for stable tool artifacts, sub-agents, and logs.

## 6. Streaming and Server Usage

The server branches on `backend`:

- `backend == "langgraph"`: LangGraph checkpointer and `thread_id` resume.
- `backend == "openjiuwen"`: openJiuwen workflow checkpoint resume.

On the openJiuwen branch, the server defaults to:

```python
os.environ.setdefault("LLM_SSL_VERIFY", "false")
```

This suits intranet or self-signed certificates. The litellm client also disables SSL verification when `LLM_SSL_CERT` is unset. For strict verification in production:

```bash
export LLM_SSL_CERT="/path/to/cert.pem"
export LLM_SSL_VERIFY="true"
```

openJiuwen streaming uses runtime `write_stream`. `OpenJiuWenWorkflow.astream()` pushes `write_stream(data)` into a side queue; `astream()` consumes it and returns events upstream so Planner, Executor, tools, and human-feedback interrupts can stream.

## 7. Interrupt and Resume

openJiuwen supports human-feedback interrupt/resume:

1. A workflow node raises openJiuwen `GraphInterrupt`.
2. DataAgent wraps it as `OpenJiuWenInterrupt`.
3. Current state, resume point, and interrupt message go to the checkpoint store.
4. After user feedback, resume loads the checkpoint and writes `__human_feedback_resume__`.
5. The workflow continues from the interrupt node.

Checkpoints use `DATABASE_URL`:

```bash
# PostgreSQL
export DATABASE_URL="postgresql://user:password@host:5432/dbname"

# SQLite for local/light deployments
export DATABASE_URL="sqlite:///./dataagent_checkpoints.db"
```

| Database | Notes |
| --- | --- |
| PostgreSQL | Recommended for server deployments. |
| SQLite | Local, test, or lightweight setups. |

Default table name: `dataagent_checkpoints`. Override:

```yaml
AGENT_CONFIG:
  checkpoint_postgres_table: "dataagent_checkpoints"
```

## 8. Differences vs LangGraph

| Dimension | LangGraph | openJiuwen |
| --- | --- | --- |
| Workflow build | LangGraph `StateGraph` and compiled graph. | openJiuwen `Workflow`, components, and runtime. |
| State | LangGraph state schema. | openJiuwen runtime `global_state` with DataAgent delta merge. |
| Streaming | LangGraph `astream`. | openJiuwen `write_stream` + DataAgent side queue. |
| Interrupt/resume | LangGraph checkpointer, `thread_id`, `Command(resume=...)`. | DataAgent checkpoint store; resume from `start_at` and global state. |

After switching to `openjiuwen`, workflow state is owned by the openJiuwen runtime. If you enable human-feedback resume, you must set `DATABASE_URL` or checkpoints cannot be persisted.

## 9. FAQ

### 9.1 Cannot import openJiuwen

If `ImportError: openjiuwen` persists after installing the extra:

```bash
uv sync --extra jiuwen
```

### 9.2 Missing base_url or model

The ReAct path requires `api_base` and `model` in `env.llm_configs`. If you see:

```text
missing required params base_url/model for openjiuwen client
```

Check:

- `MODEL.<name>.params.base_url` is set, or
- `{PROVIDER}_BASE_URL` exists, and
- `MODEL.<name>.params.model` is set.

### 9.3 Missing API key

Priority:

1. `MODEL.<name>.params.api_key`
2. `{PROVIDER}_API_KEY`
3. `OPENAI_API_KEY`

For `provider: "deepseek"`, use `DEEPSEEK_API_KEY`.

### 9.4 SSL certificate errors

For dev/intranet with litellm certificate failures:

```bash
export LLM_SSL_VERIFY="false"
```

For strict verification, set `LLM_SSL_CERT` instead of relying on defaults.

### 9.5 BaseDataAgent does not support openJiuwen

`dataagent/interface/sdk/base_data_agent.py` still restricts `BaseDataAgent.set_base_config(..., backend=...)` to `langgraph`. For openJiuwen, use YAML + `DataAgent.from_config()` to load a ReAct agent.

## 10. Related Code

- Backend factory: `dataagent/core/framework_adapters/runtime/workflow_backend_factory.py`
- openJiuwen workflow adapter: `dataagent/core/framework_adapters/runtime/workflow_openjiuwen.py`
- WorkflowBackend interface: `dataagent/core/framework_adapters/runtime/workflow_backend.py`
- openJiuwen runtime context: `dataagent/core/framework_adapters/runtime/context.py`
- PostgreSQL checkpoint: `dataagent/core/framework_adapters/checkpoints/postgres_store.py`
- SQLite checkpoint: `dataagent/core/framework_adapters/checkpoints/sqlite_store.py`
