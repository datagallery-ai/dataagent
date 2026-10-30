# Python SDK

SDK 与 REST 使用同一套准备和装配。调用 `prepare_runtime` 得到 Runtime，再创建 Agent。默认不创建 Home 目录。

公开名称见 [Python API](../reference/python-api.md)。

## 准备和调用

```python
from dataagent import LaunchOptions, create_agent, prepare_runtime

runtime = prepare_runtime(LaunchOptions(workspace="/existing/data"))
agent = create_agent(runtime, checkpointer=checkpointer)
result = await agent.ainvoke(
    {"messages": [("user", "Summarize 1, 2, 3")]},
    config,
)
agent.close()
```

`prepare_runtime` 返回不可变的 Runtime：设置、路径、扩展位置和启动报告。默认 `init_home=False`。Home 不存在时，只是没有用户配置层。需要模板目录时传入 `init_home=True`。

调用方拥有 checkpointer 和这次运行的 `config`。`thread_id` 放在 `config["configurable"]`。未传会话时，Trace 写到 `sdk` 这个会话目录。

`create_agent` 返回 `Agent`。它会立即检查配置（入口、插件声明、MCP、Hook、语义客户端），出错时直接报错，不创建会话目录。预检只读取声明和说明文件，不导入插件 Python；SDK 会话构图阶段才在构建锁内执行插件加载。同一用户、配置和插件版本下，一个 SDK 实例服务多个会话。每个会话首次使用时创建原生图并缓存，backend 和插件输出路径固定。缓存最多保留 32 个空闲会话图，超出时淘汰最久未用的；对话状态在 checkpointer 中，不受淘汰影响。模型客户端共享；每个会话使用独立的 `ModelContext`；会话图上的 tracer 按 run ID 分别导出轨迹，结束后清理调用记录。

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

工具通过 `runtime.context.session.outputs` 读取本轮产物目录。编译型插件工厂按会话调用，可以使用 `SubagentServices.outputs_dir`。

读取某个会话的原生图状态，用 `agent.graph_for(thread_id).aget_state(config)`。`graph_for` 只读取已有会话，会话不存在时抛出 `ValueError`，不会创建会话或固定路径。`agent.graph` 表示默认会话的图，首次访问会创建默认会话。

`agent.as_agui()` 只创建适配器，不构图、不打开默认 `sdk` 会话。执行 `adapter.run(input)` 时才按 `input.thread_id` 选择图和 context，并初始化原生 `LangGraphAgent`；每个请求使用新适配器。显式调用 `get_schema_keys(config)` 则按 `config.configurable.thread_id` 准备要检查的会话。

`create_agent` 和 `build_agent` 的 `input_workspaces` 参数含义一致：`None` 表示新会话使用配置默认值，已有会话沿用保存值；显式元组（包括 `()`）必须与已有绑定的路径列表一致，否则报错。比较解析后的路径及顺序，名称不参与比较，也不会覆盖保存的名称。

## 模型上下文

Python 工具通过原生 `ToolRuntime` 使用已配置的模型，不必自己读密钥：

```python
from langchain.tools import ToolRuntime
from dataagent import ModelContext

async def summarize(text: str, runtime: ToolRuntime[ModelContext]) -> str:
    """Summarize text using the configured fast model."""
    model = runtime.context.get_model("fast")
    return (await model.ainvoke(text, config=runtime.config)).text
```

`get_model()` 使用默认别名。未知别名直接失败，没有回退。客户端在这个 Agent 句柄内懒创建并复用。密钥不进入消息或 Checkpoint。

内部模型调用沿用 `runtime.config` 的 Trace 父级。它不经过 Agent 的模型中间件，也不计入 Agent 的模型循环限额。

## 低层图

需要自己传上下文时：

```python
from dataagent import ModelContext, build_agent

context = ModelContext(runtime)
graph = build_agent(runtime, context=context, checkpointer=checkpointer)
result = await graph.ainvoke(inputs, config, context=context)
```

配置了语义服务却没有客户端时，`build_agent` 会失败。请改用 `create_agent`，或传入带 `semantic_client` 的 `ModelContext`。

配置了 MCP 时，先加载工具再装配：

```python
from dataagent import build_agent, load_mcp_tools

tools = await load_mcp_tools(runtime)
graph = build_agent(runtime, mcp_tools=tools, context=context)
```

没有 `.mcp.json` 时，省略 `mcp_tools` 即可。

额外的 LangChain callback 放在调用 `config["callbacks"]`，或：

```python
graph = build_agent(runtime).with_config(callbacks=[your_native_callback])
```

`build_agent` 不再接受 `callbacks=` 参数。

## 刷新扩展

REST 在下一轮请求前自动刷新扩展。SDK 要自己调用：

```python
from dataagent import extension_revision, refresh_extensions

updated = refresh_extensions(runtime)
```

它重读插件、Hook 和 MCP 声明，保留原来的模型、路径和启动策略。调用方先编译成功，再替换正在使用的 Runtime。进行中的调用继续用旧图。模型、端口和路径变更仍要重新 `prepare_runtime`。

## 和环境的关系

SDK 准备过程不修改 `os.environ`。`LaunchOptions.env_file` 和 Home `.env` 的优先级与 CLI 相同，见 [命令与环境变量](../reference/cli-and-environment.md)。

请把 AgentKit 装在独立环境。旧运行时使用同一个导入名 `dataagent`。
