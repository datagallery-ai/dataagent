# Python SDK

Use DataAgent from a Python process. Configure a usable model with the [configuration quick reference](../configuration.md). The SDK reads `config.yaml` and `.env` from DataAgent Home by default. Use `LaunchOptions(config=..., env_file=...)` to choose them separately. See [environment precedence](../../reference/cli-and-environment.md#environment-variables).

<a name="without-file-logging"></a>

## Call an agent

Save this as a script. In a source checkout use `uv run python /path/to/example.py`. With DataAgent already installed, use `python /path/to/example.py`.

```python
import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from dataagent import LaunchOptions, create_agent, load_mcp_tools, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions())
    mcp_tools = await load_mcp_tools(runtime)
    async with create_agent(
        runtime, checkpointer=InMemorySaver(), mcp_tools=mcp_tools
    ) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", "Explain what you can do in three sentences.")]},
            config={"configurable": {"thread_id": "sdk-demo"}},
        )
        print(result["messages"][-1].content)


asyncio.run(main())
```

`prepare_runtime` loads configuration and prepares the runtime. To initialize missing Home templates, pass `LaunchOptions(init_home=True)`. The example loads MCP tools explicitly. Without MCP configuration the list is empty. See [MCP integration](extensions/mcp.md).

`async with` closes the semantic client if the agent created it. `InMemorySaver` keeps history in one process. Use the same thread and checkpointer to resume, and supply a persistent checkpointer for resuming across processes. See [Python API](../../reference/python-api.md#public-sdk) for resource ownership.

To see a root agent delegate to nl2sql, enable and configure that plugin, leave `dataagent.entry` unset, and ask a question about the configured database.

<a name="backend-log"></a>

## Write to backend.log

The SDK uses the host's Python logging configuration. To use DataAgent's JSON logging, import `configure_logging` from `dataagent.host` and call `configure_logging(runtime)` immediately after `prepare_runtime(...)`.

This replaces root handlers and sets the root level to INFO. The default is `<Home>/runtime/logs/backend.log`. The host controls initialization and may log an exception at the call boundary with `logger.exception(..., extra={"threadId": "sdk-demo"})` before re-raising. The caller handles configuration and execution failures. See [observability](../../../development/architecture/observability.md) and the [host API](../../reference/python-api.md#host-api) for trace and output locations.

`dataagent serve` and `serve-a2a` initialize logging at startup. SDK logging follows the selected Home. The launcher manages `DATAAGENT_LOG_CAPTURE`.

<a name="task-outputs"></a>

## Set an output directory per task

Reuse an agent with a distinct `thread_id` for each task. Use `output_dir` to specify the final directory.

```python
import asyncio

from dataagent import LaunchOptions, create_agent, load_mcp_tools, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions(user_id="alice"))
    mcp_tools = await load_mcp_tools(runtime)
    async with create_agent(runtime, mcp_tools=mcp_tools) as agent:
        for task_id in ("task-a", "task-b"):
            result = await agent.ainvoke(
                {"messages": [("user", "Create sales.csv with three sample sales rows.")]},
                config={"configurable": {"thread_id": task_id}},
                output_dir=f"~/reports/{task_id}",
            )
            print(result["messages"][-1].content)


asyncio.run(main())
```

If the agent writes the files, they appear in `~/reports/task-a/` and `~/reports/task-b/`. Each example runs one turn. Use a new thread to change the bound directory. Output directories within Home must be under its `outputs/` area. See [sessions and outputs](../sessions-and-outputs.md) for accepted paths, defaults, claiming existing directories, and restart behavior.

Tools obtain the directory through `runtime.context.session.outputs`. Outputs and checkpoints are stored separately.

<a name="close-stream"></a>

## Close a stream early

Use `closing` or `aclosing`. Break when you have what you need. Leaving the context closes the stream and releases the session.

```python
from contextlib import closing

with closing(agent.stream(inputs, config)) as stream:
    for chunk in stream:
        print(chunk)
        break
```

For async streams, use `async with aclosing(agent.astream(inputs, config))` and `async for`. See [Agent API](../../reference/python-api.md#agent) for concurrency rules.

<a name="tool-models"></a>

## Access models from tools

To select a model or call the semantic client inside a tool, see the [tool guide](extensions/tools.md#tool-runtime-clients).
