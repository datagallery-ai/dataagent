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

`create_agent` 返回 `Agent`。它持有原生图和一个 `ModelContext`。`invoke`、`ainvoke`、`stream`、`astream` 会自动带上这个上下文。

若配置了 `semantic`，`create_agent` 会创建并拥有语义客户端。用 `with` 或 `async with`，或在结束时调用 `close()` / `aclose()`。宿主已经有客户端时，把它传给 `semantic_client=`，该客户端不会被 Agent 关闭。

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
