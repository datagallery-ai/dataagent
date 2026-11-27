# MCP 接入

通过 `.mcp.yaml` 将外部 MCP 服务的工具提供给 DataAgent。在同一个 DataAgent Home 中共用的连接放在 Home，随某个插件启停的连接放在该插件根目录。配置位置、传输类型和完整字段见 [MCP 配置参考](../../../reference/mcp-configuration.md)。

<a name="local-stdio-example"></a>

## 运行本地 stdio 示例

在仓库根目录准备 Python 环境，并用一个独立的 Home 试用：

```bash
uv sync --locked
export MCP_PYTHON="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
export DATAAGENT_HOME="$(mktemp -d)"
cp examples/mcp/.mcp.yaml examples/mcp/math_server.py "$DATAAGENT_HOME/"
```

[示例配置](../../../../../examples/mcp/.mcp.yaml)使用 `MCP_PYTHON` 启动同目录的 [math_server.py](../../../../../examples/mcp/math_server.py)。在已有 Home 中使用时，只把 `math` 条目合并进现有 `mcpServers`，并把脚本放在该配置文件旁边。启用同名服务或复制脚本前，先检查是否已有相应配置或文件。

要在 TUI 中试用，先按[配置速查](../../../reference/configuration-api.md)为本次 Home 提供模型环境变量。在同一终端使用[已构建的源码 TUI](../../tui.md)启动或连接使用该 Home 的 Server，然后输入 `Use mcp__user__math__add to add 12 and 30.`。工具应返回 42。默认端口已有其他 Server 时，按 [CLI 参考](../../../reference/cli-and-environment.md#dataagent-serve)调整本次 Home 的监听配置，再连接对应实例。

只想验证示例的工具发现和调用时，可以从仓库根运行以下测试。测试使用临时 Home 和本地 stdio 服务，无需配置真实模型：

```bash
uv run --no-sync pytest -q tests/test_mcp.py::test_documented_local_example_is_runnable
```

<a name="remote-mcp"></a>

## 连接远程 MCP 服务

将 [完整配置模板](../../../../../examples/mcp/mcp.full.yaml)中所需的 `mcpServers` 条目合并到目标 `.mcp.yaml`，把端点替换为服务提供的完整 HTTP(S) MCP URL。根据服务要求选择 Streamable HTTP（`type: http`）或 SSE（`type: sse`），在 `headers` 中声明所需的认证头。凭据通过 `$env{NAME}` 引用，变量来源见 [MCP 配置参考](../../../reference/mcp-configuration.md#environment-and-paths)。

<a name="discovery-and-refresh"></a>

## 发现、更新与调用

Server 在会话引擎就绪前发现工具。如果连接失败，`dataagent serve` 保留健康接口，`/healthz` 返回 200 且 `data.conversation.ready` 为 `false`，`/ready` 和对话接口返回 503。请在 `backend.log` 中查看 `MCP discovery failed` 或 `MCP discovery timed out`，修复配置或连接后重启服务。

已就绪的 Server 在新一轮执行前检查本地扩展变化。只有生效的 MCP 连接映射变化时才重新发现工具。刷新失败会拒绝触发该刷新的一轮，继续运行的轮次保留原工具集。远端工具清单变化且本地连接配置保持原样时，可重启 Server 重新发现。更多细节见 [扩展生命周期](../../../../development/architecture/extension-lifecycle.md)。

工具调用各自建立并关闭 MCP 会话，stdio 服务随每次调用启动。需要跨次保留的数据可存到文件或外部服务。

<a name="python-sdk"></a>

## 在 Python SDK 中使用

配置了 MCP 的 SDK 调用方先异步发现工具，再把工具列表传给 `create_agent`。下面的 `runtime` 由 [SDK 指南](../sdk.md)中的 `prepare_runtime` 创建：

```python
from dataagent import create_agent, load_mcp_tools

agent = create_agent(runtime, mcp_tools=await load_mcp_tools(runtime))
```

SDK 由调用方显式管理工具加载与更新。配置已启用但未传 `mcp_tools` 时，`create_agent` 会在创建时报告该缺失。

<a name="nl2sql-mcp"></a>

## NL2SQL 通过 MCP 执行 SQL

NL2SQL 单独使用 `plugins.config.nl2sql.database.engine: mcp` 及 `database.path` 连接 Streamable HTTP 端点。插件直接调用其配置的读取工具执行查询和 EXPLAIN。端点、读取工具和返回结构见 [NL2SQL 插件说明](../../../../../dataagent/plugins/nl2sql/README.md)。

宿主 `.mcp.yaml` 中的认证头、工具发现及宿主工具的 Hook 和调用计数作用于宿主 MCP 工具。NL2SQL 的数据库连接和 SQL 执行由插件自己的配置管理。
