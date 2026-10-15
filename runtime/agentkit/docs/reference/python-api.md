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
| `build_agent` | 返回原生 `CompiledStateGraph`。不启动服务 |
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
| `env_file` | `None` | 显式环境文件 |
| `init_home` | `False` | `True` 时创建 Home 模板，不覆盖已有文件 |

## `Agent`

| 方法 | 行为 |
| --- | --- |
| `invoke` / `ainvoke` / `stream` / `astream` / `astream_events` | 调用 `graph`，并传入本句柄的 `context` |
| `as_agui(name="dataagent", **kwargs)` | 每次新建适配器。不要跨请求缓存适配器 |
| `close` / `aclose` | 关闭本句柄创建的语义客户端。传入的客户端保持打开 |
| 上下文管理器 | 同步和异步都支持，退出时关闭自有客户端 |

`create_agent(runtime, *, semantic_client=None, **kwargs)` 把 `model`、`checkpointer`、`session`、`input_workspaces`、`mcp_tools`、`extension_revision` 传给 `build_agent`。

`build_agent` 在配置了 MCP 且未传 `mcp_tools` 时失败。配置了语义服务且上下文没有客户端时失败。

## `ModelContext`

```python
ModelContext(runtime, *, model=None, semantic_client=None)
```

- `get_model()` 使用 `dataagent.model.default`
- `get_model(alias)` 使用 `models.<alias>`
- 传入的 `model` 只覆盖默认别名，便于测试
- `semantic_client` 可能是 `None`

未知别名抛出 `ValueError`。
