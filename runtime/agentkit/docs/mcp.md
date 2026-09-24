# MCP 配置与接入

## 固定格式

文件名是 **`.mcp.json`**（带前导点），内容为标准 JSON，不接受注释、重复键或未知字段：

```json
{
  "mcpServers": {
    "local": {
      "type": "stdio",
      "command": "python3",
      "args": ["servers/math_server.py"],
      "env": {"SERVICE_KEY": "$env{SERVICE_KEY}"}
    },
    "remote": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer $env{SERVICE_TOKEN}"}
    }
  }
}
```

- `mcpServers` 必填，空对象表示没有 MCP；键为小写字母开头的字母、数字、`_`、`-`。
- `stdio`：`command` 必填；`args` 默认 `[]`；`env` 默认 `{}`。省略 `type` 仅表示 `stdio`。
- `http`：Streamable HTTP；`sse`：旧版 SSE。两者均要求绝对 HTTP(S) `url`，`headers` 默认 `{}`。
- 不支持 `cwd`、`disabled`、OAuth、WebSocket、动态 headers 或自定义超时字段。
  删除服务条目即停用；插件 MCP 随插件启停。请求超时沿用产品 `timeout_seconds`。
- 环境引用统一沿用本产品的 `$env{NAME}`，**不是 Claude 的 `${NAME}`**；缺失即启动报错。
  使用现有环境优先级：进程 > `--env-file` > 显式 config 同目录 `.env` > Home `.env`。
  不自动读取插件旁 `.env`；不把整个宿主环境复制进 JSON 或日志。
- stdio 的 cwd 固定为该 `.mcp.json` 所在目录；相对可执行文件/脚本参数以此为基准。
  `command` 是单个可执行程序，`args` 是参数列表，不经过 shell。依赖由用户预先安装。

## 两处来源与名称

只读取 `<home>/.mcp.json` 和 **启用插件**的 `<plugin_root>/.mcp.json`。
不读取 cwd、只读 workspace、session 或 `--config` 旁的 MCP 文件；不增加新路径选项。
文件不存在即无配置，不创建空模板。未启用插件的文件不解析、不连接。

合并采用命名空间，不做覆盖：Home 服务 `calc` → `mcp__user__calc`；插件 `analytics`
中的服务 `calc` → `mcp__analytics__calc`。原生适配器添加服务名前缀后，工具 `add` 分别为
`mcp__user__calc_add`、`mcp__analytics__calc_add`。最终工具名必须唯一，且符合
`[A-Za-z0-9_-]{1,64}`；冲突或非法名称明确报错，不静默改名。

根 Agent 与 general-purpose 可用全部 MCP 工具。声明式 SubAgent 不声明 `tools` 时沿用
原生继承；声明列表时只获得列出的工具，可直接填写上述完整 MCP 工具名，`tools: []` 不继承。
Hooks matcher 同样使用实际完整工具名。

## 实施边界

1. `declarations.py` 定义配置 schema；`bootstrap/mcp.py` 校验并翻译为原生连接 dict。
   bootstrap 只读取配置，不启动 MCP 进程、不导入模型工具链。结果放在 extensions 中，隐藏凭证 repr。
2. `extensions/mcp.py` 使用官方 `MultiServerMCPClient(tool_name_prefix=True)` 获取原生 BaseTool。
   不自行实现 JSON-RPC、HTTP/SSE、工具消息或调用循环。保留原生 `isError` → 错误 ToolMessage 行为。
3. Compiler 将 MCP 工具与 Python 工具放进同一 registry 后处理 SubAgent 引用；
   Agent 装配继续调用 `create_deep_agent`，沿用现有限额、Hooks、文件系统和 AG-UI。
4. REST 在 lifespan 就绪前发现工具，整个发现过程最多 20 秒（产品总时限更短时从短）；
   任何启用服务失败则启动失败，不带缺失工具继续运行。每轮复用工具定义；运行期间本地 MCP
   连接配置变化时，在下一轮重新发现，失败则拒绝该轮请求，保留已有会话。
5. 原生客户端采用无状态模式：发现后关闭连接；每次工具调用新建并关闭 MCP session，
   stdio 因此会重新启动子进程。不支持跨调用的服务器内存状态。正常退出、异常、取消交由原生客户端清理。
   MCP server 自身权限不等于 DataAgent 文件 backend 的权限；这不是第三方进程沙箱。
6. SDK 显式 `tools = await load_mcp_tools(runtime)`，再 `build_agent(runtime, mcp_tools=tools)`。
   保留同步建图入口；配置了 MCP 却未传入加载结果时明确提示，不静默忽略。

首版只接入 Tools，不接入 MCP Resources、Prompts、交互授权、连接池或重试。
REST/TUI 支持[轮次边界的配置刷新](extension-refresh.md)，不轮询服务端工具列表变化。
服务端输出走现有工具结果/AG-UI 链路，不添加 TUI 私有事件或字符串错误识别。

## 本地使用示例

安装更新依赖：`uv sync --project runtime/agentkit --locked`。
将 [math_server.py](../examples/mcp/math_server.py) 放到 Home 根，参考
[本地 .mcp.json 示例](../examples/mcp/.mcp.json) 把 `math` 服务加入 Home 的 `.mcp.json`。
已有其他服务时合并 `mcpServers`，不要覆盖原文件。示例脚本不访问外部网络。

三种连接类型的完整模板见 [mcp.full.json](../examples/mcp/mcp.full.json)，
逐字段解释见 [MCP 示例说明](../examples/mcp/README.md)。完整模板的远程地址需要替换，不能直接启动。

从仓库根启动（自定义 Home 时沿用 `DATAAGENT_HOME`）：

```bash
export MCP_PYTHON="$PWD/runtime/agentkit/.venv/bin/python"
npm run start:tui -- --v2
```

然后输入：`Use mcp__user__math_add to add 12 and 30.`
若放在启用插件根目录，脚本也放在该目录，工具名改为 `mcp__<plugin_id>__math_add`。
不需要修改插件 `.plugin.json` 的 schema，也不需要把 MCP 配置放入 `config.json`。

SDK 使用同一份配置和原生工具：

```python
from dataagent import build_agent, load_mcp_tools, prepare_runtime

async def query():
    runtime = prepare_runtime()
    tools = await load_mcp_tools(runtime)
    graph = build_agent(runtime, mcp_tools=tools)
    return await graph.ainvoke({"messages": [("user", "Use the MCP math tool to add 12 and 30.")]})
```

凭证不要写成明文提交；原生文件工具和 Shell 均可能读取 `.mcp.json`，不提供凭证文件访问隔离。宿主诊断沿用脱敏，
但第三方服务器返回内容、自己打印的 stderr 和外部副作用由该服务负责，不能当作沙箱保证。

## 验收

使用本地脚本化 MCP server 验证 stdio/HTTP/SSE、真实 tools/list 与 tools/call、环境与路径解析；
验证 Home/启用插件/禁用插件、同名隔离、工具错误/连接失败/超时取消、SubAgent 与 Hooks、
SDK/REST 流式输出与恢复，并回归已有非 live 测试。不连接用户真实 MCP 服务、不调用付费模型。

实施结果：

- 后端全量非 live 测试：414 passed、1 deselected，其中 MCP 专项 32 项。
- 验证了真实 CLI 的 MCP 发现先于 ready，父进程关闭后后端退出；stdio 发现、调用、取消及超时后无遗留服务进程。
- 本文随附的 `.mcp.json` + 数学服务实际调用得到 42；SDK 与 REST 均消费原生工具/消息。
- TUI 构建和 194 项测试通过；Ruff 与 `git diff --check` 通过（沿用排除既有格式问题的 `test_tui_cursor.py`）。
- 未修改本机 Home/MCP 配置，未连接真实外部服务；未提交或推送。

参考：[Claude 的 mcpServers 外形](https://code.claude.com/docs/en/mcp)、
[LangChain MCP 客户端](https://reference.langchain.com/python/langchain-mcp-adapters/client/)。
当前 LangChain 固定为 1.3.18，使用 `langchain-mcp-adapters==0.3.2`，不升级到新版 beta MCP API。
