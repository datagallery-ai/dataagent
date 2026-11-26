# Python API

包版本是 `0.2.0`。请安装 `.` 这一份，不要和旧的 `dataagent` 包混在同一环境。导入 `dataagent` 时只加载轻量名称；图、模型和语义客户端在第一次使用时导入。

接口分三层。先用公开 SDK；只有在写宿主服务或需要原生 LangGraph 图时，才用后两层。

| 层 | 模块 | 给谁 | 稳定性 |
| --- | --- | --- | --- |
| 公开 SDK | `dataagent` | 在 Python 里调用 Agent 的人、写工具的人 | 对外契约 |
| 宿主接口 | `dataagent.host` | 把 Agent 提供给其他客户端的服务，例如 `dataagent serve` | 对外契约，面向宿主 |
| 低层接口 | `dataagent.lowlevel` | 需要自己管理 LangGraph 图和上下文的人 | 跟随 LangGraph 变化，可能调整 |

不在这三个模块 `__all__` 里的名称都是内部实现，可能随时改变。

## 公开 SDK（`dataagent`）

| 名称 | 作用 |
| --- | --- |
| `LaunchOptions` | 启动输入：配置文件、环境文件、用户、输入目录。冻结数据类 |
| `prepare_runtime` | `LaunchOptions` → `PreparedRuntime`。读取配置并校验，不创建 Agent |
| `PreparedRuntime` | `prepare_runtime` 的结果：设置、路径和扩展位置。只读，不要自己构造 |
| `create_agent` | 由 `PreparedRuntime` 创建 `Agent`。唯一推荐的创建方式 |
| `Agent` | `create_agent` 的返回类型。用于类型标注，不要自己构造 |
| `load_mcp_tools` | 配置了 MCP 时，先 `await` 它，再把结果传给 `create_agent` |
| `AgentContext` | 工具里 `ToolRuntime[AgentContext]` 的上下文类型：配置的模型、语义客户端、插件配置 |
| `SemanticServiceClient` | 语义服务 HTTP 客户端 |
| `collect_model_stream(model, messages, **kwargs)` | 以流式调用模型并合成一条完整消息，保留工具调用、推理和用量。插件里调用 `AgentContext.get_model()` 返回的模型时用它 |
| `SubagentServices` | 编译子代理工厂 `factory(params, services)` 的第二个参数：`get_model`、`semantic_client`、`outputs_dir`、`run_hooks` |
| `ConfigurationLoadError` | 配置文件不存在、不是合法 YAML 或校验失败。`ValueError` 的子类 |
| `DataAgentError` | 所有 DataAgent 异常的基类，字段为 `source`、`component`、`fact`、`trace_id`。`ConfigurationLoadError`、`LoopDetectedError`、`A2ACallError` 都继承它，并保留各自的内置基类（`ValueError`、`RuntimeError`）。语义服务客户端也抛出它 |
| `LoopDetectedError` | 循环检测终止本轮。`kind` 说明是文本复读还是工具循环 |
| `safe_error` | 把异常渲染成 `{"code", "message"}`：去掉已知密钥和终端控制符，消息截断到 1500。`code` 的取值见 [事件](events.md) |

### 典型用法

```python
from dataagent import LaunchOptions, create_agent, prepare_runtime

runtime = prepare_runtime(LaunchOptions(workspaces=("/existing/data",)))
async with create_agent(runtime, checkpointer=checkpointer) as agent:
    result = await agent.ainvoke(
        {"messages": [("user", "Summarize 1, 2, 3")]},
        config={"configurable": {"thread_id": "task-a"}},
    )
```

步骤和注意事项见 [SDK](../guides/sdk.md)。

### `LaunchOptions`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `config` | `None` | 配置文件。省略时读 Home `config.yaml` |
| `env_file` | `None` | 环境文件。设置后用这一份代替 Home `.env` |
| `user_id` | `"default"` | 本地用户 ID，决定 `runtime/users/<user>/` |
| `workspaces` | `()` | 只读输入目录 |
| `cwd` | `Path.cwd()` | 相对路径的基准 |
| `init_home` | `False` | `True` 时创建 Home 模板，不覆盖已有文件 |
| `session_id` | `None` | 宿主用：沿用该会话创建时记录的输入目录。不选择对话；对话由调用时的 `thread_id` 决定 |

### `create_agent`

```python
create_agent(runtime, *, model=None, checkpointer=None, mcp_tools=None, semantic_client=None)
```

| 参数 | 说明 |
| --- | --- |
| `checkpointer` | LangGraph checkpointer。要跨进程保留对话历史时传持久化实现 |
| `mcp_tools` | 配置了 MCP 时必传，值来自 `await load_mcp_tools(runtime)`。没有 `.mcp.yaml` 时省略 |
| `semantic_client` | 宿主已有语义客户端时传入，Agent 不关闭它。省略时，配置了 `semantic` 就由 Agent 创建并拥有 |
| `model` | 覆盖默认模型对象，主要用于测试 |

`create_agent` 立即检查配置（入口、插件声明、MCP、Hook、语义客户端）。出错时抛出 `ValueError`，不创建会话目录。参数名写错时抛出 `TypeError`。另外三个参数（`session`、`input_workspaces`、`extension_revision`）给宿主用，见 [宿主接口](#宿主接口dataagenthost)。

### `Agent`

| 成员 | 行为 |
| --- | --- |
| `invoke` / `ainvoke` / `stream` / `astream` / `astream_events` | 参数同 LangGraph，另加调用级 `output_dir`。按 `config["configurable"]["thread_id"]` 选择会话，首次使用时创建 |
| `close` / `aclose` | 关闭本 Agent 创建的语义客户端。传入的客户端保持打开 |
| 上下文管理器 | `with` 和 `async with` 都支持，退出时调用 `close` |

- 不同 `thread_id` 可以并发。同一 `thread_id` 上的重叠调用会报错。
- 流式调用开始迭代后占用会话，流结束、出错或关闭时释放。提前停止时用 `contextlib.closing` / `aclosing` 关闭流，不能只靠 `break`。
- `output_dir` 只在会话首次调用时生效，之后不能改。规则见 [会话与产物](../guides/sessions-and-outputs.md)。

### `AgentContext`

工具通过 `runtime.context` 拿到它，SDK 用户不需要自己构造。

| 成员 | 说明 |
| --- | --- |
| `get_model(alias=None)` | 返回配置的模型。省略别名时用 `dataagent.model`。未知别名抛出 `ValueError` |
| `semantic_client` | 语义客户端，没有配置 `semantic` 时为 `None` |
| `session.outputs` | 本轮产物目录 |
| `home` | 已解析的 Home 路径，用于跨会话共享的资源 |
| `get_plugin_config(plugin_id)` | `plugins.config.<plugin_id>` 的副本。插件未启用时抛出 `ValueError` |
| `get_plugin_state(plugin_id)` | 该插件在本 Agent 内的私有内存字典。不进入 Checkpoint，进程重启后清空。插件自己负责并发同步 |

## 宿主接口（`dataagent.host`）

宿主是把 Agent 提供给其他客户端的长驻服务，例如 `dataagent serve`。只调用 Agent 的程序不需要这一节。

| 名称 | 作用 |
| --- | --- |
| `refresh_extensions(runtime)` | 重读插件、Hook 和 MCP 声明，返回新的 `PreparedRuntime`。保留模型、路径和启动策略。先编译成功再替换正在使用的 PreparedRuntime；进行中的调用继续用旧图 |
| `extension_revision(runtime)` | 扩展来源的 SHA-256 指纹，用来判断是否需要刷新。不含业务输入和会话状态 |
| `server_listen(options=None)` | 读所选配置的 `server` 段，按 `prepare_runtime` 的环境规则展开 `$env{NAME}`（含 `options.env_file`），返回 `(host, port)`。读不到、缺少变量或校验失败时返回 `127.0.0.1:8790`。`prepare_runtime` 失败时也能用 |
| `error_code(error)` | 把异常映射为固定错误码 |
| `configure_logging(runtime=None)` | 宿主启动时配置统一的 `backend.log`。无 runtime 时也能记录早期启动错误 |
| `register_log_secrets(values)` | 扩展或凭据刷新后追加脱敏值，保留旧运行凭据的脱敏能力 |
| `diagnostic_traceback(error, secrets=())` | 返回完整的脱敏堆栈，用于诊断日志，不作为公开错误响应 |
| `log_context` | 宿主用 ContextVar 绑定 `requestId`、`threadId`、`runId` 等关联字段，结束后重置 token |
| `OPAQUE_ID_PATTERN` | 客户端给的 ID（`threadId`、`runId`、用户）的正则：`[A-Za-z0-9][A-Za-z0-9_-]{0,127}`。宿主校验请求时用它 |
| `ERROR_CODES` | 全部错误码。取值和含义见 [事件](events.md)。异常类可用类属性 `error_code` 声明自己的码 |

宿主另外使用这些成员：

- `create_agent(..., session=, input_workspaces=, extension_revision=)`：
  - `session` 指定默认会话，必须是当前 Home 和用户下的标准会话路径，否则立即报错。
  - `input_workspaces=None` 时，新会话用配置默认值，已有会话沿用保存值；显式元组（包括 `()`）必须和已有绑定的路径及顺序一致。名称不参与比较。
  - `extension_revision` 是上面 `extension_revision(runtime)` 的值，用来让 Skill 缓存跟随扩展刷新。
- `runtime.paths.open_session(thread_id, *, create, output_dir=None, input_workspaces=None)`：登记或恢复会话，返回 `(SessionPaths, workspaces)`，可传给上面两个参数。`create=False` 时会话不存在返回 `None`。目录规则见 [会话与产物](../guides/sessions-and-outputs.md)。
- `agent.as_agui(name="dataagent", **kwargs)`：创建 AG-UI 适配器，执行 `run(input)` 时按 `input.thread_id` 选择会话。每个请求用新的适配器。
- `agent.graph_for(thread_id)`：取得已有会话的原生图，例如读状态 `graph_for(t).aget_state(config)`。会话不存在时抛出 `ValueError`。`agent.graph` 是默认会话的图，首次访问会创建默认会话。

## 低层接口（`dataagent.lowlevel`）

`build_graph` 返回一张原生 LangGraph `CompiledStateGraph`，绑定一个会话。它不拥有语义客户端，不按 `thread_id` 缓存图，也不管理产物目录。调用方每次调用都要传上下文。多任务应用请用 `create_agent`。

```python
from dataagent import AgentContext, load_mcp_tools
from dataagent.lowlevel import build_graph

context = AgentContext(runtime, semantic_client=client)
graph = build_graph(
    runtime, context=context, checkpointer=checkpointer,
    mcp_tools=await load_mcp_tools(runtime),
)
result = await graph.ainvoke(inputs, config, context=context)
```

```python
build_graph(runtime, *, checkpointer=None, model=None, session=None,
            input_workspaces=None, mcp_tools=None, extension_revision=None, context=None)
```

- 省略 `session` 和 `context` 时用名为 `sdk` 的会话。
- 配置了 MCP 却没传 `mcp_tools` 时失败。配置了语义服务、而 `context` 没有语义客户端时失败。
- `dataagent.entry` 有值时只挂该编译型入口。
- 额外的 LangChain callback 放在调用的 `config["callbacks"]`，或用 `graph.with_config(callbacks=[...])`。

## A2A

客户端接口 `A2ACaller`、`A2AResult` 和 `A2ACallError` 从 `dataagent.extensions.a2a` 导入。服务端工厂从 `dataagent.server.a2a` 导入。导入 SDK 或 A2A 客户端不会加载服务端。

| 名称 | 给谁 | 作用 |
| --- | --- | --- |
| `A2ACaller(base_url, *, auth_token=None, timeout=180)` | 调用方 | 调用另一个 A2A 1.0 Agent。`await send(text, *, thread_id=None)` 返回 `A2AResult` |
| `A2AResult` | 调用方 | 结果：`text`、`context_id`、`task_id`、`state`，以及可选的 `request_id`、`root_run_id` |
| `A2ACallError` | 调用方 | 远端任务失败、取消或拒绝；本地 `error_code` 为 `TOOL_ERROR`，可选的 `remote_code`、`request_id`、`task_id`、`context_id`、`root_run_id` 用于远端诊断 |
| `create_a2a_server(runtime, *, host, port, auth_token=None)` | 宿主 | 返回只含 A2A 路由的 Starlette 应用 |
| `run_a2a_server(runtime, *, host, port, auth_token=None)` | 宿主 | 用 uvicorn 运行上面的应用。命令行入口是 `dataagent serve-a2a` |

配置里的 `a2a.remote_agents` 会自动变成名为 `a2a__<id>` 的工具，不需要调用 Python 接口。约定见 [A2A](a2a.md)。
