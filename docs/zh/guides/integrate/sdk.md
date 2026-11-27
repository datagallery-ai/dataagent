# Python SDK

通过 SDK 在 Python 进程里调用 DataAgent。先按[配置速查](../../reference/configuration-api.md)准备可用模型。默认读取 DataAgent Home 的 `config.yaml` 和 `.env`，也可用 `LaunchOptions(config=..., env_file=...)` 分别选择文件。环境变量取值顺序见[环境变量参考](../../reference/cli-and-environment.md#environment-variables)。

## 调用 Agent { #without-file-logging }

把示例保存为脚本。在源码环境中用 `uv run python /path/to/example.py` 运行，或在已安装 DataAgent 的环境里用 `python /path/to/example.py` 运行。

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
            {"messages": [("user", "用三句话说明你现在能做什么。")]},
            config={"configurable": {"thread_id": "sdk-demo"}},
        )
        print(result["messages"][-1].content)


asyncio.run(main())
```

`prepare_runtime` 读取配置并准备运行环境。需要在 Home 初始化模板时，传入 `LaunchOptions(init_home=True)`。脚本显式加载 MCP 工具，未配置 MCP 时得到空列表。连接步骤见 [MCP 接入指南](extensions/mcp.md)。

`async with` 会关闭 Agent 自己创建的语义客户端。示例的 `InMemorySaver` 仅在进程内保留对话历史。要续接同一会话，继续使用同一个 `thread_id` 和 checkpointer。跨进程续接时，用调用方管理的持久化 checkpointer。接口和资源归属见 [Python API](../../reference/python-api.md#public-sdk)。

要查看主代理委派 nl2sql 的效果，在所选配置中启用并配置 nl2sql，保持 `dataagent.entry` 未设置，然后用实际数据库问题替换示例里的消息。

## 写入统一 backend.log { #backend-log }

SDK 使用宿主已有的 Python logging 配置。想沿用 DataAgent 服务的 JSON 日志，可在上面脚本的导入区加入 `from dataagent.host import configure_logging`，并在 `main()` 里紧接 `prepare_runtime(...)` 调用 `configure_logging(runtime)`。

这会把根日志级别设为 INFO 并替换现有 handlers，默认日志文件为 `<Home>/runtime/logs/backend.log`。由宿主统一安排初始化，在调用边界按需用 `logger.exception(..., extra={"threadId": "sdk-demo"})` 记录堆栈并重新抛出。准备配置和运行的异常都由调用方处理。Trace 与产物使用各自的目录，详见[可观测性](../../../development/architecture/observability.md)和[宿主 API](../../reference/python-api.md#host-api)。

`dataagent serve` 和 `serve-a2a` 在启动时初始化日志。SDK 的日志跟随准备出的 Home，启动器内部的 `DATAAGENT_LOG_CAPTURE` 由启动器管理。

## 每个任务指定产物目录 { #task-outputs }

可以重用一个 Agent 运行多个任务。给任务分配独立的 `thread_id`，用 `output_dir` 指定最终目录。

```python
import asyncio

from dataagent import LaunchOptions, create_agent, load_mcp_tools, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions(user_id="alice"))
    mcp_tools = await load_mcp_tools(runtime)
    async with create_agent(runtime, mcp_tools=mcp_tools) as agent:
        for task_id in ("task-a", "task-b"):
            result = await agent.ainvoke(
                {"messages": [("user", "生成一份包含三行示例销售数据的 sales.csv。")]},
                config={"configurable": {"thread_id": task_id}},
                output_dir=f"~/reports/{task_id}",
            )
            print(result["messages"][-1].content)


asyncio.run(main())
```

如果 Agent 生成文件，两份 `sales.csv` 分别写在 `~/reports/task-a/` 与 `~/reports/task-b/`。示例只运行单轮任务，每个任务都可以从首次绑定的目录查看产物。要换产物目录，使用新的 `thread_id`。产物目录位于 Home 内时，必须在 Home 的 `outputs/` 下。地址类型、默认路径、接管已有目录和重启后的绑定规则见[会话与产物](../sessions-and-outputs.md)。

工具通过 `runtime.context.session.outputs` 取得当前会话的目录。这个目录里的文件和对话 Checkpoint 各自独立保存。

## 提前结束事件流 { #close-stream }

用 `closing` 或 `aclosing` 管理流。收到所需事件就退出循环，退出上下文时关闭流并释放会话。

```python
from contextlib import closing

with closing(agent.stream(inputs, config)) as stream:
    for chunk in stream:
        print(chunk)
        break
```

异步调用用 `async with aclosing(agent.astream(inputs, config))` 和 `async for`。并发与会话占用规则见 [Agent API](../../reference/python-api.md#agent)。

## 在工具中访问模型 { #tool-models }

工具可通过 `ToolRuntime[AgentContext]` 调用 `runtime.context.get_model()`，或传已配置的别名。写法见[工具扩展指南](extensions/tools.md)。调用时传 `config=runtime.config`，让模型运行能关联到当前 Trace。工具内直接调用的模型走独立路径，Agent 循环的模型调用限额和中间件只作用于循环中的调用。
