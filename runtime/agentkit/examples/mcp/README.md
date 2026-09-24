# MCP 配置示例

- [.mcp.json](.mcp.json)：只连接随附的 [math_server.py](math_server.py)，用于本地快速验证。
- [mcp.full.json](mcp.full.json)：覆盖当前支持的全部字段和三种连接类型，作为配置参考。

**完整模板中的远程地址是占位地址，不能直接运行。** 使用前删除不需要的服务，替换其余服务的地址和凭证引用。
`mcp.full.json` 不会被自动加载；将需要的 `mcpServers` 条目合并到 `<home>/.mcp.json`
或启用插件的 `<plugin_root>/.mcp.json`。已有配置不要直接覆盖。MCP 不写入产品 `config.json`。

## 字段说明

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `mcpServers` | 是 | 服务集合；`{}` 表示没有服务 |
| `mcpServers.<name>` | — | 自定义服务名，小写字母开头，仅字母、数字、`_`、`-`；例如 `math` |
| `type` | HTTP/SSE 必填 | `stdio`、`http`（Streamable HTTP）或 `sse`；省略时为 `stdio` |
| `command` | stdio 必填 | 一个可执行程序的路径或名称，不是整条 shell 命令 |
| `args` | 否 | stdio 的参数数组，默认 `[]`；每个参数单独一项 |
| `env` | 否 | 传给 stdio 子进程的环境变量，值必须是字符串，默认 `{}` |
| `url` | HTTP/SSE 必填 | 服务提供的完整 HTTP(S) MCP 端点，不是 LLM API 地址 |
| `headers` | 否 | HTTP/SSE 请求头，值必须是字符串，默认 `{}`；不需要认证时可省略 |

`command/args/env` 仅用于 stdio，`url/headers` 仅用于 HTTP/SSE，不能混用。
请求头的名称和认证方式由目标 MCP 服务定义；示例展示 Bearer token 和 API key 两种写法，不代表每个服务都要求认证。

JSON 不支持注释，不添加 `_comment`。当前不支持 `cwd`、`disabled`、`timeout`、OAuth 等其他字段。
停用服务直接删除对应条目，或禁用携带该配置的插件。

## 路径和环境变量

stdio 的工作目录固定为 `.mcp.json` 所在目录。示例 `math_server.py` 必须与它放在一起。
`command` 中带 `/` 的相对路径也以该目录解析；程序名称则由系统查找。
例如 Node 服务可以使用 `"command": "node", "args": ["servers/index.js"]`，对应脚本和依赖需提前准备。
参数不经过 shell，不在 `command` 中填写 `cd ... && ...`。

本地数学示例使用 DataAgent 自己的 Python 环境。从仓库根执行：

```bash
uv sync --project runtime/agentkit --locked
export MCP_PYTHON="$PWD/runtime/agentkit/.venv/bin/python"
```

完整模板还引用 `ANALYTICS_MCP_TOKEN`、`LEGACY_MCP_API_KEY`。如果保留相应服务，将真实值放到
Home 的 `.env`（不要提交凭证）：

```dotenv
ANALYTICS_MCP_TOKEN=replace-with-your-service-token
LEGACY_MCP_API_KEY=replace-with-your-service-api-key
```

环境引用格式是 `$env{NAME}`，不是 `${NAME}`；被保留条目引用的变量缺失会启动失败。
环境来源优先级：进程环境 > `--env-file` > 显式产品配置旁 `.env` > Home `.env`。
插件旁 `.env` 不自动加载；HTTP/SSE 的 `headers` 不会自动从进程环境生成，需明确声明引用。

## 启动后的行为

1. 读取 Home 和启用插件的 `.mcp.json`，校验并展开环境变量。
2. 通过官方适配器连接每个服务、发现工具；任意保留的服务不可用都会导致启动失败。
3. 将发现的原生工具装配到 Agent。Home 中 `math` 服务的 `add` 工具名为 `mcp__user__math_add`；
   插件 `analytics` 中同一服务的工具名为 `mcp__analytics__math_add`。
4. REST/TUI 在下一轮请求前检测本地配置变化并刷新；当前按工具调用建立连接，不保留跨调用的服务内存状态。
   仅服务端工具列表变化而本地配置不变时，仍需重启；SDK 调用方显式重新加载工具并建图。

本地示例配置好后，从仓库根运行 `npm run start:tui -- --v2`，输入：
`Use mcp__user__math_add to add 12 and 30.`

完整配置语义及 SDK 用法见 [MCP 配置与接入](../../docs/mcp.md)。
