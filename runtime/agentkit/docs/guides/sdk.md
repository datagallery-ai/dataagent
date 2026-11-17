# Python SDK

SDK 与 REST 使用同一套准备和装配。只有一条路径：`prepare_runtime` 得到 PreparedRuntime，`create_agent` 创建 Agent，再调用 Agent。默认不创建 Home 目录。

公开名称见 [Python API](../reference/python-api.md)。`dataagent.lowlevel.build_graph` 等低层接口只在需要自己管理 LangGraph 图时使用，本文不涉及。

## 准备和调用

### 不配置文件日志

先在 Home `config.json` / `.env` 配好可用模型，再将以下示例保存为脚本，从仓库根目录用 `uv run --project runtime/agentkit python /path/to/example.py` 运行。示例使用内存 Checkpoint，进程退出后不保留对话历史。

```python
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from dataagent import LaunchOptions, create_agent, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions())
    async with create_agent(runtime, checkpointer=InMemorySaver()) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", "用三句话说明你现在能做什么。")]},
            config={"configurable": {"thread_id": "sdk-demo"}},
        )
        print(result["messages"][-1].content)


asyncio.run(main())
```

SDK 不自动安装日志处理器，也不自动创建 `backend.log`。现有宿主若已配置 Python logging，SDK/插件日志仍会交给该宿主；“不配置文件日志”也不关闭 Trace 或产物。调用异常向上抛出。

### 写入统一 backend.log

自建宿主需要文件日志时使用已有的 `configure_logging(runtime)`，仍写同一个 Home 日志目录。此函数设置根 logger 为 INFO，并替换已有 handlers（`force=True`），接入已有应用时应由宿主统一选择初始化位置。下面用 `logger.exception` 记录创建/调用异常及堆栈，然后继续抛出；日志配置本身不会自动捕获所有 SDK 异常。

```python
import asyncio
import logging

from langgraph.checkpoint.memory import InMemorySaver
from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.host import configure_logging


async def main():
    runtime = prepare_runtime(LaunchOptions())
    configure_logging(runtime)
    logger = logging.getLogger("dataagent.sdk.example")
    thread_id = "sdk-logged-demo"
    try:
        async with create_agent(runtime, checkpointer=InMemorySaver()) as agent:
            logger.info("SDK 调用开始", extra={"threadId": thread_id})
            result = await agent.ainvoke(
                {"messages": [("user", "用三句话说明你现在能做什么。")]},
                config={"configurable": {"thread_id": thread_id}},
            )
            logger.info("SDK 调用完成", extra={"threadId": thread_id})
            print(result["messages"][-1].content)
    except Exception:
        logger.exception("SDK 创建或调用失败", extra={"threadId": thread_id})
        raise


asyncio.run(main())
```

配置准备在日志初始化前执行；若 `prepare_runtime` 就失败，异常仍交给宿主处理。日志位于 `<Home>/runtime/logs/backend.log`，JSON 格式、脱敏与轮转沿用后端实现，不另建 SDK 日志文件。非空进程 `DATAAGENT_HOME` 决定 Home；内部 `DATAAGENT_LOG_CAPTURE` 留给启动器管理，不手动设置。实现见 [日志配置](../../dataagent/logging.py)，准备和调用见 [SDK 句柄](../../dataagent/client.py)。

上述两个示例使用默认主代理。验证“主代理委派 nl2sql”时，在所选配置中启用并配置 nl2sql，省略 `dataagent.entry`，将问题改成实际数据库问题。配置了 MCP 时还需按下文加载工具。

`prepare_runtime` 返回不可变的 PreparedRuntime：设置、路径、扩展位置和启动报告。默认 `init_home=False`。Home 不存在时，只是没有用户配置层。需要模板目录时传入 `init_home=True`。

调用方拥有 checkpointer 和这次运行的 `config`。`thread_id` 放在 `config["configurable"]`，它决定使用哪个会话。

`create_agent` 返回 `Agent`。它会立即检查配置（入口、插件声明、MCP、Hook、语义客户端），出错时直接报错，不创建会话目录。预检只读取声明和说明文件，不导入插件 Python；SDK 会话构图阶段才在构建锁内执行插件加载。同一用户、配置和插件版本下，一个 SDK 实例服务多个会话。每个会话首次使用时创建原生图并缓存，backend 和插件输出路径固定。缓存最多保留 32 个空闲会话图，超出时淘汰最久未用的；对话状态在 checkpointer 中，不受淘汰影响。模型客户端共享；每个会话使用独立的 `AgentContext`；会话图上的 tracer 按 run ID 分别导出轨迹，结束后清理调用记录。

配置了 MCP 时，先加载工具再创建 Agent：

```python
from dataagent import create_agent, load_mcp_tools

agent = create_agent(runtime, mcp_tools=await load_mcp_tools(runtime))
```

若配置了 `semantic`，`create_agent` 会创建并拥有语义客户端。用 `with` 或 `async with`，或在结束时调用 `close()` / `aclose()`。宿主已经有客户端时，把它传给 `semantic_client=`，该客户端不会被 Agent 关闭。

## 每个任务指定产物目录

同一个 Agent 实例可以执行多个会话。每次调用直接指定最终产物目录，SDK 内部按会话缓存图：

```python
import asyncio
from pathlib import Path

from dataagent import LaunchOptions, create_agent, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions(
        config=Path.home() / ".dataagent/config.json",
        user_id="alice",
    ))
    async with create_agent(runtime) as agent:
        for task_id in ("task-a", "task-b"):
            result = await agent.ainvoke(
                {"messages": [("user", "生成一份包含三行示例销售数据的 sales.csv。")]},
                config={"configurable": {"thread_id": task_id}},
                output_dir=f"~/reports/{task_id}",
            )
            print(result["messages"][-1].content)


asyncio.run(main())
```

两个任务分别写入 `~/reports/task-a/` 和 `~/reports/task-b/`。

- **`output_dir` 是最终目录。** 不会追加用户或会话 ID。这一点和配置项 `dataagent.output_root` 不同，后者是根目录，会追加 `<user>/<thread>/`。
- **只接受绝对路径或 `~/` 开头的路径**（`str` 或 `Path`）。`reports`、`./reports`、`../reports` 均报错。`~/` 指操作系统用户的主目录。
- **同一 Home 中每个会话独占一个目录。** 两个 `thread_id` 传同一个目录时，第二个会报错。需要放在一起时，使用同一父目录下的不同子目录。
- **可以接管已有目录。** 传入一个已有文件、但不属于任何会话的目录时，DataAgent 保留这些文件，并在数据库登记本会话的归属。
- **只在首次调用时生效。** 目录保存到 `sessions.sqlite` 的 `session_bindings` 表。之后的调用可以省略 `output_dir`；传入不同的目录会报错，已有文件不会搬迁。

新会话的路径优先级：SDK 的 `output_dir` → `<output_root>/<user>/<thread>/`。`output_root` 省略或为 `null` 时，使用 `<home>/outputs`。Trace 仍留在 Home 的 `runtime/` 内。
`artifacts/` 跟随产物目录；日志和 trace 留在 Home，checkpointer 由调用方管理。此绑定只保存产物位置，对话历史持久化仍需传入持久化 checkpointer。

完整规则（会话身份、数据库绑定、常见报错、不推荐的用法）见 [会话与产物](sessions-and-outputs.md)。

不同 session 可并发。同一 Agent 的同一 session 若已有一轮执行，重叠调用会报错。需要改址时使用新的 `thread_id`。
同步 `stream()` 在调用时准备会话并检查路径参数，开始迭代后才持续占用 session。未开始迭代的流不占用 session。`stream` / `astream` / `astream_events` 开始迭代后，在流结束、出错或关闭时释放 session。

提前停止时必须关闭流。单独 `break` 不会通知 Python 生成器结束，不能依赖 GC。同步使用 `contextlib.closing`，异步使用 `aclosing`：

```python
from contextlib import closing

with closing(agent.stream(inputs, config)) as stream:
    for chunk in stream:
        print(chunk)
        # 此处即使 break 或抛异常，退出 with 时也会关闭流并释放 session。
```

工具通过 `runtime.context.session.outputs` 读取本轮产物目录。

写服务、需要读原生图状态或接 AG-UI 时，见 [Python API 的宿主接口](../reference/python-api.md#宿主接口dataagenthost)。

## 模型上下文

Python 工具通过原生 `ToolRuntime` 使用已配置的模型，不必自己读密钥：

```python
from langchain.tools import ToolRuntime
from dataagent import AgentContext

async def summarize(text: str, runtime: ToolRuntime[AgentContext]) -> str:
    """Summarize text using the configured fast model."""
    model = runtime.context.get_model("fast")
    return (await model.ainvoke(text, config=runtime.config)).text
```

`get_model()` 使用默认别名。未知别名直接失败，没有回退。客户端在这个 Agent 句柄内懒创建并复用。密钥不进入消息或 Checkpoint。

内部模型调用沿用 `runtime.config` 的 Trace 父级。它不经过 Agent 的模型中间件，也不计入 Agent 的模型循环限额。

## 和环境的关系

SDK 准备过程不修改 `os.environ`。`LaunchOptions.env_file` 和 Home `.env` 二选一，与 CLI 相同。`LaunchOptions.config` 不改变环境文件。见 [命令与环境变量](../reference/cli-and-environment.md)。

服务入口 `dataagent serve`、`serve-a2a` 自动配置 `backend.log`；SDK 的可选接入见上文 [写入统一 backend.log](#写入统一-backendlog)，接口定义见 [宿主接口](../reference/python-api.md#宿主接口dataagenthost)。

请把 AgentKit 装在独立环境。旧运行时使用同一个导入名 `dataagent`。
