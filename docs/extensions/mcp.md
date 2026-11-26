# MCP

用 `.mcp.yaml` 接入外部 MCP 服务。字段表在 [MCP 配置](../reference/mcp-configuration.md)。本地可运行示例在 [examples/mcp](../../examples/mcp/README.md)。

## 放在哪里

只读两处：

- `<home>/.mcp.yaml`，作用域名 `user`
- 已启用插件根目录的 `.mcp.yaml`，作用域名是插件目录名

产品 `config.yaml` 和 `.plugin.yaml` 里没有 MCP 段。未启用插件的 `.mcp.yaml` 不会加载。

## 最小 stdio 服务

`<home>/.mcp.yaml`：

```yaml
mcpServers:
  math:
    command: python
    args:
    - math_server.py
```

省略 `type` 时按 `stdio`。`command` 含 `/` 且不是绝对路径时，相对该 `.mcp.yaml` 所在目录解析。工作目录也是这个目录。

环境引用使用 `$env{NAME}`，优先级与产品配置相同。示例用 `$env{MCP_PYTHON}` 指向解释器。

支持的传输：

| `type` | 连接 |
| --- | --- |
| `stdio` | `command`、`args`、`env` |
| `http` | `url`、`headers`。传输为 Streamable HTTP |
| `sse` | `url`、`headers` |

服务名匹配 `[a-z][a-z0-9_-]{0,63}`。连接后的服务 ID 是 `mcp__<scope>__<name>`。同名冲突会使准备失败。工具名是 `mcp__<scope>__<name>__<tool>`。

## 何时生效

REST 启动时，在对外就绪前加载 MCP 工具。配置或发现失败会阻止启动。

下一轮查询前，若 `.mcp.yaml` 变化，会重新连接并编译。进行中的运行保持旧工具集。无效更新只拒绝新查询。

SDK：

```python
from dataagent import create_agent, load_mcp_tools

agent = create_agent(runtime, mcp_tools=await load_mcp_tools(runtime))
```

没有 MCP 配置时不必调用。配置了却没把工具传给 `create_agent` 时，`create_agent` 立即报错。

每次工具调用都会打开并关闭一次 MCP 会话。stdio 服务因此在每次调用时启动。需要跨调用保留的数据，请放在文件或外部服务中。

## NL2SQL 插件的 MCP SQL 连接

NL2SQL 的 `engine: mcp` 是插件私有的 SQL transport。插件通过
`plugins.config.nl2sql.database.path` 配置的 endpoint 直接调用读取工具执行查询和
`EXPLAIN`，模型无需选择工具。配置示例见 [NL2SQL 插件](../../dataagent/plugins/nl2sql/README.md)。

`.mcp.json` 的连接配置、认证 headers、工具发现及工具级 Hook 和计数仅适用于宿主
MCP 工具路径。NL2SQL 插件按自己的数据库配置建立连接并发现读取工具。
