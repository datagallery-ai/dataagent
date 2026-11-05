# Python API

导入 `dataagent` 时只加载轻量名称。图、模型和语义客户端在第一次使用时导入。

包版本是 `0.1.0`。请安装 `runtime/agentkit` 这一份，不要和旧的 `dataagent` 包混在同一环境。

## 导出

| 名称 | 作用 |
| --- | --- |
| `LaunchOptions` | 启动前调用方已知的输入。冻结数据类 |
| `prepare_runtime` | `LaunchOptions` 到 `Runtime` |
| `Runtime` | 不可变的设置、路径、扩展位置、报告和模型环境快照 |
| `refresh_extensions` | 重读插件、Hook 和 MCP。保留模型、路径和启动策略 |
| `extension_revision` | 扩展来源的 SHA-256 指纹。不含业务输入和会话状态 |
| `create_agent` | 创建 `Agent`。配置了语义服务时拥有客户端，除非调用方传入 |
| `Agent` | `graph` 加 `ModelContext`。提供 invoke/stream 和 `as_agui` |
| `build_agent` | 返回原生 `CompiledStateGraph`。`dataagent.entry` 有值时，`build_entry_graph` 只挂该工厂 Runnable。不启动服务 |
| `load_extensions` | 把已发现的声明编译成 Deep Agents 参数 |
| `load_mcp_tools` | 按 Runtime 里的 MCP 连接加载工具。异步 |
| `ModelContext` | `get_model` 和 `semantic_client`。不进入 Checkpoint |
| `SemanticServiceClient` | 语义服务 HTTP 客户端 |
| `DataAgentError` | 客户端错误类型 |
| `safe_error` | 返回 `{"code", "message"}`。`code` 是异常类名，消息截断到 1500，并替换已知密钥 |

拒绝一次 Hook 时，在函数中抛出异常。

## `LaunchOptions`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `cwd` | `Path.cwd()` | 相对路径基准 |
| `workspace` | `None` | 单个输入。不能和 `workspaces` 同时使用 |
| `workspaces` | `()` | 多个输入 |
| `user_id` | `"default"` | 本地 profile |
| `session_id` | `None` | 恢复会话 |
| `config` | `None` | 显式配置文件 |
| `env_file` | `None` | 显式环境文件。设置后用这一份代替 Home `.env` |
| `init_home` | `False` | `True` 时创建 Home 模板，不覆盖已有文件 |

## `Runtime.paths.open_session`

```python
session, workspaces = runtime.paths.open_session(
    "task-a", create=True, output_dir="~/reports/task-a",
)
```

返回 `(SessionPaths, tuple[WorkspaceInput, ...])`，可传给 `create_agent` 的 `session` 和 `input_workspaces`。`SessionPaths` 包含控制面目录 `root`、产物目录 `outputs` 和轨迹目录 `traces`；`artifacts` 为 `outputs / "artifacts"`，没有会话级 `logs` 字段。

- `create=True`：绑定不存在时登记；存在时恢复，不重新写库或扫描其他会话。`create=False`：只恢复，不存在时返回 `None`。
- `output_dir`：可选 `str | Path`，直接指定新会话的最终产物目录。只接受绝对路径或 `~/` 开头的路径；其他相对路径报错。已有文件但不属于任何会话的目录会被接管。
- `input_workspaces`：可选 `tuple[WorkspaceInput, ...]`。`None` 表示沿用已有绑定，新会话使用配置默认值；显式传入时，路径列表及顺序必须与已有绑定一致。`()` 是显式指定空列表。
- 未传 `output_dir` 时，新会话采用 `<output_root>/<user>/<thread>/`，`output_root` 默认是 `<home>/outputs`。派生目录里已有不明来源的文件时抛出 `ValueError`。
- 已有会话始终使用保存的目录；显式传入不同目录会抛出 `ValueError`。不会改变 Runtime 或其他会话的路径。
- 产物目录与同一 Home 的其他已登记会话目录重叠时抛出 `ValueError`，不登记绑定。规则见 [会话与产物](../guides/sessions-and-outputs.md#规则每个会话独占一个产物目录)。
- 产物目录不得与 Home 重叠，只有 `<home>/outputs` 区域例外；也不得与输入 workspace 重叠。

## `Agent`

| 方法 | 行为 |
| --- | --- |
| `invoke` / `ainvoke` / `stream` / `astream` / `astream_events` | 接受调用级 `output_dir`；按 `config.configurable.thread_id` 选择或创建会话图，注入本轮 context，沿用该图的 tracer |
| `graph_for(thread_id)` | 取得已有会话的原生图；会话不存在时抛出 `ValueError`，不创建会话 |
| `graph` | 取得默认会话的原生图；首次访问会创建默认会话并固定输出目录 |
| `as_agui(name="dataagent", **kwargs)` | 仅创建适配器，不打开默认会话；执行 `run(input)` 时按 `input.thread_id` 初始化原生 AG-UI。不要跨请求缓存适配器 |
| `close` / `aclose` | 关闭本句柄创建的语义客户端。传入的客户端保持打开 |
| 上下文管理器 | 同步和异步都支持，退出时关闭自有客户端 |

`create_agent(runtime, *, semantic_client=None, **kwargs)` 先检查配置，出错时立即抛出 `ValueError`，不创建会话目录。它把 `model`、`checkpointer`、`session`、`input_workspaces`、`mcp_tools`、`extension_revision` 传给 `build_agent`。

显式传入 `session` 时，root、traces 必须是当前 Home/user 下的标准会话地址，否则立即报错；outputs 规范化后保留。`input_workspaces=None` 对新会话使用配置默认值，对已有会话沿用保存值；显式元组必须与绑定的路径列表及顺序一致，`()` 也会参与检查。名称不参与比较。会话登记统一经过 `open_session`，`create_agent` 和 `build_agent` 不直接写绑定表。

不同 session 可并发。同一 Agent 的同一 session 拒绝重叠调用。同步 `stream()` 在调用时校验会话与路径，开始迭代后占用 session；流结束、异常或显式关闭时释放。提前停止必须使用 `closing` / `aclosing` 或显式关闭流，不能依赖 `break` 或 GC。会话首次保存输出路径后，不允许重新绑定其他目录。

`build_agent` 是低层接口，固定会话、backend 和默认 trace。SDK 沿用会话图绑定的 tracer，每轮按独立 run ID 导出并清理记录。`core` 的构图和 backend 机制保持不变，多任务应用用 `create_agent`。

`build_agent` 在配置了 MCP 且未传 `mcp_tools` 时失败。配置了语义服务且上下文没有客户端时失败。

## A2A 服务

`a2a_server` 不随 `import dataagent` 加载。

```python
from a2a_server import create_a2a_server, run_a2a_server
```

`dataagent serve` 在同一个进程上挂 Agent Card 和 `POST /a2a/jsonrpc`。`create_a2a_server(runtime, *, host, port, ...)` 返回只含这组路由的 Starlette 应用，`run_a2a_server` 用 uvicorn 跑起来。命令行入口是 `dataagent serve-a2a`。

`A2ACaller(base_url)` 调用另一个 A2A 1.0 agent。配置里的 `a2a.agents` 由 `load_extensions` 收成名为 `a2a__<id>` 的工具。约定见 [A2A](a2a.md)。

## `ModelContext`

```python
ModelContext(runtime, *, model=None, semantic_client=None)
```

- `get_model()` 使用 `dataagent.model.default`
- `get_model(alias)` 使用 `models.<alias>`
- 传入的 `model` 只覆盖默认别名，便于测试
- `semantic_client` 可能是 `None`
- `session.outputs` 是本轮产物路径；`agent.context` 保存默认会话，不表示最近一次调用
- 编译型插件工厂按会话创建，使用 `services.outputs_dir` 获取该会话的固定产物目录

未知别名抛出 `ValueError`。
