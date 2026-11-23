# YAML 配置说明

[config.yaml](config.yaml) 是覆盖当前全部产品配置字段的可运行示例，并启用两个随附的 Python Hook。
只需要默认配置时，使用 [config.example.yaml](../config.example.yaml)。YAML 支持注释；字段说明集中在本文。

## 配置文件清单

| 用途 | YAML 文件 | 加载位置 |
| --- | --- | --- |
| 个人产品配置 | `<home>/config.yaml` | bootstrap 自动读取 |
| 显式产品配置 | `--config <path>/config.yaml` | 替代 Home config.yaml，不与它叠加 |
| Home 初始化模板 | `dataagent/bootstrap/templates/config.yaml` | 首次初始化时复制，不覆盖已有目标文件 |
| 最小配置示例 | `config.example.yaml` | 手动复制或显式指定 |
| 完整配置示例 | `examples/config.yaml` | 显式指定，随附 `hooks/` |
| nl2sql 入口 | `examples/nl2sql-entry.yaml` | 显式指定；`dataagent.entry` 为 `nl2sql` |
| 插件清单 | `<plugin>/.plugin.yaml` | Compiler 读取已启用插件 |
| 子 Agent 声明 | 如 `subagents/statistics.yaml` | 由插件 `subagents` 引用 |
| MCP 服务配置 | `<home>/.mcp.yaml`、启用插件的 `<plugin_root>/.mcp.yaml` | 只加载这两处；使用 `mcpServers`，不参与 config.yaml 合并 |

产品配置版本为 `4`，插件清单版本为 `3`，子 Agent 版本为 `1`。
自动发现只读取规范名 `.yaml`；显式文件路径也支持 `.yml`。

## 启动和配置优先级

从仓库根目录运行：

`npm run start:tui` 连接 `/api/v1/runs`（默认 `http://127.0.0.1:8790`，地址来自 `config.yaml` 的 `server.host` 和 `server.port`）。示例配置用 `--config` 传给它拉起的后端。

在控制面的 `DATAAGENT_HOME/.env` 设置 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`。
复制完整示例到其他目录时，需同时复制 `hooks/`，或修改/移除两个示例 Hook。

nl2sql 入口示例不带 Hook：

```bash
npm run start:tui -- --config examples/nl2sql-entry.yaml
```

除模型的 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY` 外，再设置 `SEMANTIC_BASE_URL` 和 `NL2SQL_DATABASE_PATH`。
`NL2SQL_DATABASE_PATH` 是 sqlite 文件路径。这一层会把 `plugins.enabled` 换成 `["nl2sql"]`。
它只写入口会用到的限额：`limits.timeout_seconds` 是宿主执行阶段时限，`plugins.config.nl2sql.model_call_budget` 是单次任务的模型调用上限。
这份文件省略的 `hooks`、`model_calls_per_agent`、`tool_calls_per_agent` 和 `workspaces` 来自程序默认值，留在配置里，不装进这张图。

配置文件选择、插件依赖顺序、`defaults_file` 与 `plugins.config` 的合并/覆盖规则，以及环境文件优先级统一见 [配置操作](../docs/guides/configuration.md#优先级)。Home 和显式配置二选一，当前内置 nl2sql 没有声明默认配置文件。显式写出的值会覆盖插件默认值，省略项才继承；Hook 数组按相应契约追加。

扩展相关配置可在下一轮请求前刷新；模型、宿主路径、`dataagent.entry` 和服务配置仍需重启，见 [扩展生命周期](../docs/architecture/extension-lifecycle.md)。字段定义见 [配置参考](../docs/reference/configuration.md)。

SDK 的完整调用示例见 [不配置文件日志](../docs/guides/sdk.md#不配置文件日志) 与 [写入统一 backend.log](../docs/guides/sdk.md#写入统一-backendlog)。A2A 请求参数、等待/异步任务及认证见 [A2A](../docs/reference/a2a.md)。

## 产品字段

| 字段 | 默认值 / 含义 |
| --- | --- |
| `schema_version` | `4` |
| `dataagent.model` | `"primary"`；引用 `models` 的配置 ID，不是服务端模型名 |
| `dataagent.limits.timeout_seconds` | `180`；正数，用于宿主执行阶段及模型请求等超时；A2A 排队与 Checkpoint 保存不计入调用时限，SDK 自行控制整轮时限 |
| `dataagent.limits.model_calls_per_agent` | `128`；每个 Agent 每次调用的模型循环上限，非整棵子 Agent 树的预算；不覆盖摘要器所有内部调用 |
| `dataagent.limits.tool_calls_per_agent` | `128`；每个 Agent 每次调用的工具次数上限；主/子 Agent 独立计数，新一轮重置；超限抛错 |
| `dataagent.workspaces` | `[]`；可选只读输入列表，在 Home 或显式配置声明；空列表不创建默认输入目录 |
| `dataagent.entry` | `null`；编译型子代理名。设置后图只有该子代理。本示例保持 `null`，仍装配 Deep Agents 根。入口示例见 [nl2sql-entry.yaml](nl2sql-entry.yaml) |
| `dataagent.hooks` | 默认空；六事件分组，见下一节 |
| `models.<id>.provider` | `"openai"`；当前唯一支持值，OpenAI-compatible 服务也使用它 |
| `models.<id>.name` | 必填非空；服务商实际模型名 |
| `models.<id>.base_url` | 必填非空；API 根地址，是否带 `/v1` 按服务商要求，不填到 `/chat/completions` |
| `models.<id>.api_key` | 必填非空；建议引用 `"$env{LLM_API_KEY}"` |
| `models.<id>.max_retries` | `2`；非负整数，模型 SDK 单次请求的最大重试次数（首次请求不计）；`0` 禁用 |
| `plugins.enabled` | 默认 `[]`；本示例为 `["common"]`，需先把 `examples/plugins/common` 完整复制到 `<home>/plugins/common`；按顺序装配，不允许重复 ID；`[]` 不禁用独立 Skills/Hooks 和宿主工具 |
| `plugins.paths` | `[]`；填写每个插件自身目录，不是插件集合父目录；添加路径不等于启用 |
| `server.host` | `"127.0.0.1"`；默认本地监听，无个人登录或租户隔离 |
| `server.port` | `8790`；1–65535，被占用则报错，不复用未知服务 |

多个模型可放在 `models` 中，修改 `dataagent.model` 后重启切换；没有自动 fallback 或动态切换接口。
启动时只展开默认模型的环境引用；工具通过 `runtime.context.get_model("模型ID")` 首次获取其他模型时，
再使用启动时捕获的环境值解析该模型。未使用模型的变量可以暂不定义，实际使用时缺失则报错。
工具的完整示例见 [llm-utils](plugins/llm-utils/README.md)；该机制不增加 `/model` 命令。

## 六类 Hooks

每个事件对应数组；条目包含 `entrypoint`、可选 `params`（默认 `{}`），工具事件还可包含 `matcher`。
组内不写 `event`。也可以写成带 `event` 的扁平列表。产品、插件和 SubAgent 共用同一声明格式。

```yaml
dataagent:
  hooks:
    before_model:
    - entrypoint: hooks/check_input.py:handle
      params:
        max_chars: 4000
    before_tool:
    - entrypoint: hooks/check_tool.py:handle
      matcher: common__summarize_numbers
      params:
        blocked_tools: []
```

| 事件 | 调用点与 Python 签名 |
| --- | --- |
| `before_agent` | Agent 调用开始前；`handle(state, runtime, *, params)` |
| `after_agent` | Agent 正常执行至末尾；同上，不是异常/取消回调 |
| `before_model` | 每次模型调用前；同上，可访问完整原生 state/runtime |
| `after_model` | 每次模型调用返回后；同上，不是逐 token 回调 |
| `before_tool` | 工具调用前；`handle(request, *, params)` |
| `after_tool` | 工具正常返回结果后；`handle(request, result, *, params)`；异常直接抛出时不调用 |

`entrypoint` 为 `相对路径.py:函数名`，以声明所在配置目录为基准；插件及其子 Agent 的引用均以插件根目录为基准。
不接受绝对路径或越出该根目录的路径。只把文件放入 `hooks/` 不会自动注册。

`matcher` 精确匹配实际工具名，不支持 glob/正则；省略即匹配所有工具。
`params` 是自定义 JSON 参数，不是宿主配置。示例的 `max_chars` / `blocked_tools` 由相应 Python 函数定义。

使用普通 `def` / `async def`，不传装饰后的 middleware 对象。四种 state Hook 可返回 `None`、原生 state-update dict 或 `Command`；
工具 Hook 还可返回原生请求/结果替换对象；拒绝单个工具可返回错误 ToolMessage，抛异常则结束整轮。完整返回契约见 [Hook](../docs/extensions/hooks.md#函数签名)，不使用 `action: deny`；异步 Hook 需用 `ainvoke` / `astream`。

插件清单里的 Hook 先装配，随后是选中产品文件里的 `dataagent.hooks`。before 正序、after 逆序，重复声明不去重。
根 Hooks 不自动继承到子 Agent；子 Agent 在自己的 YAML 中注册。

## 插件、路径和内部状态

插件目录名、`.plugin.yaml` 的 `id`、`plugins.enabled` 中的 ID 必须一致。
候选目录来自内置插件、Home `plugins/<id>` 和显式 `plugins.paths`。
同一真实目录只计一次，同一启用 ID 对应多个不同目录时报错，不做隐式覆盖。

配置中的 `plugins.paths` / workspace `path` 以该配置文件目录为基准，支持绝对路径和 `~/`。
workspace 条目为 `{"name": "inputs", "path": "./data", "read_only": true}`：`name` 为展示/标识名称，必须唯一；`path` 必须存在且可读；当前只允许 `read_only: true`。
CLI 相对路径仍以启动 cwd 为基准。

workspace 优先级：CLI `--workspace`（可重复）> 有效 `DATAAGENT_WORKSPACE` > Home/显式配置的 `dataagent.workspaces`；未声明则为空列表，不创建默认输入目录。
默认 user 是 `default`。显式 workspace 不追加用户/session 子目录；恢复 session 使用已保存的输入绑定。
Home 由进程 `DATAAGENT_HOME` 指定，默认 `~/.dataagent`；它同时决定配置、扩展和运行状态根。cwd 只解析相对参数，cwd 和输入目录都不提供自动配置层。

模型直接使用 workspace 的真实绝对路径读取输入，产出写入 `<home>/outputs/<user>/<session>/`。
文件工具和 shell 使用相同的真实路径；shell 的 cwd 是当前 session 的 outputs，无虚拟路径转换。
检查点、会话索引和后端日志留在 Home runtime；控制面会话目录只保留 `traces/`，用于保存运行轨迹。
`--user` 仅选择本地资源 profile，不是登录。A2A JSON-RPC 可用共享 Bearer 令牌，REST 不受该令牌保护。

独立 Skills 仅自动发现于 Home；任意 `--config` 旁的 `skills/` 不会自动发现。
Tools、SubAgents 和 Prompts 在插件 `.plugin.yaml` 中声明，不是顶层产品字段。
模型固定使用 Chat Completions，`max_retries` 默认 `2`。仅使用 SDK 原生连接/超时和可重试 HTTP 错误的退避，
不重跑 Agent、Hook 或工具；已建立流式响应后的读取失败不自动重放。主/子 Agent 共用模型配置。
REST 的 Agent 事件迭代时限包含其中的模型请求、重试等待和工具耗时；并非 HTTP 从接收到结束的总耗时。SDK 直接调用由调用方控制整轮时限。
模型调用限额统计 Agent 循环次数，不统计同一次模型请求内部的 HTTP 重试。重试可能增加等待和服务端费用。
模型支持 `temperature`、`max_tokens`、`extra_body`，省略时保留提供者默认行为；没有独立的 `response_format`、`memory`、任意 backend 或 sandbox 配置入口。

## MCP

格式与语义见 [MCP 配置](../docs/reference/mcp-configuration.md)。可运行本地示例见
[mcp/.mcp.yaml](mcp/.mcp.yaml) 与 [math_server.py](mcp/math_server.py)。
MCP 服务声明不写入产品 `config.yaml` 或插件 `.plugin.yaml`。
覆盖 stdio、HTTP、SSE 全部支持字段的模板见 [mcp/mcp.full.yaml](mcp/mcp.full.yaml)，
字段、路径及凭证配置见 [MCP 示例说明](mcp/README.md)。完整模板包含占位远程地址，使用前需替换或删除。
