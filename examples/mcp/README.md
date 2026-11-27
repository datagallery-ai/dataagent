# MCP 配置示例

| 文件 | 用途 |
| --- | --- |
| [`.mcp.yaml`](.mcp.yaml) 和 [`math_server.py`](math_server.py) | 可直接验证的本地 stdio 服务。按[本地 stdio 示例](../../docs/zh/guides/integrate/extensions/mcp.md#local-stdio-example)准备两个文件和 Python 解释器。 |
| [`mcp.full.yaml`](mcp.full.yaml) | stdio、Streamable HTTP 和 SSE 模板。远程地址是占位值。选择需要的条目并替换地址和环境变量后，合并进 Home 或已启用插件的 `.mcp.yaml`。 |

字段、路径和环境变量规则见 [MCP 配置参考](../../docs/zh/reference/mcp-configuration.md)。运行时验证和 SDK 装配步骤见 [MCP 接入指南](../../docs/zh/guides/integrate/extensions/mcp.md)。
