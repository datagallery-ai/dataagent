# JSON 配置说明

[config.json](config.json) 是覆盖当前全部产品配置字段的可运行示例，并启用两个随附的 Python Hook。
只需要默认配置时，使用 [config.example.json](../config.example.json)。JSON 不允许注释；字段说明集中在本文，不引入 JSONC 或 `_comment` 字段。

## 文件清单与迁移范围

| 用途 | JSON 文件 | 加载位置 |
| --- | --- | --- |
| 个人产品配置 | `<home>/config.json` | bootstrap 自动读取 |
| 显式产品配置 | `--config <path>/config.json` | 作为最高优先级配置层 |
| Home 初始化模板 | `dataagent/bootstrap/templates/config.json` | 首次初始化时复制，不覆盖已有目标文件 |
| 最小配置示例 | `config.example.json` | 手动复制或显式指定 |
| 完整配置示例 | `examples/config.json` | 显式指定，随附 `hooks/` |
| 插件清单 | `<plugin>/.plugin.json` | Compiler 读取已启用插件 |
| 子 Agent 声明 | 如 `subagents/statistics.json` | 由插件 `subagents` 引用 |
| MCP 服务配置 | `<home>/.mcp.json`、启用插件的 `<plugin_root>/.mcp.json` | 只加载这两处；使用 `mcpServers`，不参与 config.json 合并 |

这是格式替换，不增加第二套配置体系：字段、schema 版本、Hooks 函数签名和合并规则不变。
产品配置版本仍为 `3`，插件清单版本仍为 `2`。只接受标准 JSON；不再解析 YAML 配置。

已有 Home、显式配置和外部插件需要把配置**内容**转为 JSON，并改名为上述文件名；仅改后缀不够。
插件清单中的 `subagents` 路径也要同步改为 `.json`。原文件可留作备份，但不会被自动加载；不要把真实凭证提交到仓库。
此代码迁移不自动改写本机 Home 或仓库外的插件。

不转换 `.env`、Prompt Markdown、Python Hook/Tool 文件、`pyproject.toml`，也不改变
Deep Agents 原生 `SKILL.md` 的 YAML frontmatter。`session.json` 已是 JSON，无需迁移。

## 启动和配置优先级

从仓库根目录运行：

```bash
npm run start:tui -- --v2 --config runtime/agentkit/examples/config.json
```

在 `~/.dataagent/.env` 设置 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`，或用 `--env-file` 指定文件。
复制完整示例到其他目录时，需同时复制 `hooks/`，或修改/移除两个示例 Hook。

配置由低到高：字段默认值 → Home `config.json` → `--config`。
启动目录的 `config.json` 不会自动读取。相同真实文件被显式选择时，只在最高层应用一次。

- 字典按字段合并；标量和普通列表由后层覆盖。省略字段表示继承；普通列表 `[]` 可以清空。
- 例外：`dataagent.hooks` 按层追加。`"hooks": []` 或空事件分组只表示本层不追加，不清除低层注册。
- 完整示例列出了默认值，作为最高层使用会覆盖低层相应字段。
- 环境变量优先级：进程 > `--env-file` > 显式配置旁的 `.env` > Home `.env`。空值不覆盖低层非空值。
- 引用写为 JSON 字符串 `"$env{LLM_MODEL}"`，不是 `${LLM_MODEL}`。仅展开最终选中模型的环境引用。
- 扩展相关配置在 REST/TUI 的下一轮请求前刷新；模型、宿主路径和服务配置仍需重启。
  具体范围见[扩展刷新说明](../docs/extension-refresh.md)。未知字段、重复键（含嵌套对象）、非法 JSON 和非有限数值都会报错。

## 产品字段

| 字段 | 默认值 / 含义 |
| --- | --- |
| `schema_version` | `3` |
| `dataagent.model.default` | `"primary"`；引用 `models` 的配置 ID，不是服务端模型名 |
| `dataagent.limits.timeout_seconds` | `180`；正数，REST 整轮流式时限，同时作为模型请求超时；SDK 调用方自行控制整轮时限 |
| `dataagent.limits.model_calls_per_agent` | `128`；每个 Agent 每次调用的模型循环上限，非整棵子 Agent 树的预算；不覆盖摘要器所有内部调用 |
| `dataagent.limits.tool_calls_per_agent` | `128`；每个 Agent 每次调用的工具次数上限；主/子 Agent 独立计数，新一轮重置；超限抛错 |
| `dataagent.workspaces` | `[]`；可选只读输入列表，在 Home 或显式配置声明；空列表不创建默认输入目录 |
| `dataagent.hooks` | 默认空；六事件分组，见下一节 |
| `models.<id>.provider` | `"openai"`；当前唯一支持值，OpenAI-compatible 服务也使用它 |
| `models.<id>.name` | 必填非空；服务商实际模型名 |
| `models.<id>.base_url` | 必填非空；API 根地址，是否带 `/v1` 按服务商要求，不填到 `/chat/completions` |
| `models.<id>.api_key` | 必填非空；建议引用 `"$env{LLM_API_KEY}"` |
| `models.<id>.max_retries` | `2`；非负整数，模型 SDK 单次请求的最大重试次数（首次请求不计）；`0` 禁用 |
| `plugins.enabled` | 默认 `[]`；本示例为 `["common"]`，需先把 `examples/plugins/common` 完整复制到 `<home>/plugins/common`；按顺序装配，不允许重复 ID；`[]` 不禁用独立 Skills/Hooks 和宿主工具 |
| `plugins.paths` | `[]`；填写每个插件自身目录，不是插件集合父目录；添加路径不等于启用 |
| `server.host` | `"127.0.0.1"`；当前仅本地服务，不支持远程多用户鉴权 |
| `server.port` | `8790`；1–65535，被占用则报错，不复用未知服务 |

多个模型可放在 `models` 中，修改 `dataagent.model.default` 后重启切换；没有自动 fallback 或动态切换接口。
只展开最终选中模型的环境引用，未选择模型的环境变量可以暂不定义。

## 六类 Hooks

每个事件对应数组；条目包含 `entrypoint`、可选 `params`（默认 `{}`），工具事件还可包含 `matcher`。
组内不再填写 `event`；现有扁平列表也可使用。产品、插件和 SubAgent 共用同一声明格式。

```json
{
  "dataagent": {
    "hooks": {
      "before_model": [
        {"entrypoint": "hooks/check_input.py:handle", "params": {"max_chars": 4000}}
      ],
      "before_tool": [
        {"entrypoint": "hooks/check_tool.py:handle", "matcher": "common__summarize_numbers", "params": {"blocked_tools": []}}
      ]
    }
  }
}
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
两种 tool Hook 只返回 `None`。拒绝调用用异常，不返回 `action: deny`；异步 Hook 需用 `ainvoke` / `astream`。

插件根 Hooks 先装配，独立 Hooks 按 Home → 显式配置追加；before 正序、after 逆序，重复声明不去重。
根 Hooks 不自动继承到子 Agent；子 Agent 在自己的 JSON 中注册。

## 插件、路径和内部状态

插件目录名、`.plugin.json` 的 `id`、`plugins.enabled` 中的 ID 必须一致。
候选目录来自内置插件、Home `plugins/<id>` 和显式 `plugins.paths`。
同一真实目录只计一次，同一启用 ID 对应多个不同目录时报错，不做隐式覆盖。

配置中的 `plugins.paths` / workspace `path` 以该配置文件目录为基准，支持绝对路径和 `~/`。
workspace 条目为 `{"name": "inputs", "path": "./data", "read_only": true}`：`name` 为展示/标识名称，必须唯一；`path` 必须存在且可读；当前只允许 `read_only: true`。
CLI 相对路径仍以启动 cwd 为基准。

workspace 优先级：CLI `--workspace`（可重复）> 有效 `DATAAGENT_WORKSPACE` > Home/显式配置的 `dataagent.workspaces`；未声明则为空列表，不创建默认输入目录。
默认 user 是 `default`。显式 workspace 不追加用户/session 子目录；恢复 session 使用已保存的输入绑定。
Home 由进程 `DATAAGENT_HOME` 指定，默认 `~/.dataagent`；它同时决定配置、扩展和运行状态根。cwd 只解析相对参数，cwd 和输入目录都不提供自动配置层。

模型直接使用 workspace 的真实绝对路径读取输入，产出写入 `<home>/runtime/users/<user>/sessions/<session>/outputs/`。
文件工具和 shell 使用相同的真实路径；shell 的 cwd 是当前 session 的 outputs，无虚拟路径转换。
检查点、会话索引和后端日志留在 Home runtime；session 的 `logs/`、`traces/` 目前仅预留目录。
`--user` 仅选择本地资源 profile，不是登录；没有 Bearer token 验证。

独立 Skills 仅自动发现于 Home；任意 `--config` 旁的 `skills/` 不会自动发现。
Tools、SubAgents 和 Prompts 在插件 `.plugin.json` 中声明，不是顶层产品字段。
模型固定使用 Chat Completions，`max_retries` 默认 `2`。仅使用 SDK 原生连接/超时和可重试 HTTP 错误的退避，
不重跑 Agent、Hook 或工具；已建立流式响应后的读取失败不自动重放。主/子 Agent 共用模型配置。
REST 整轮 `timeout_seconds` 包括请求、重试等待及工具耗时；SDK 直接调用由调用方控制整轮时限。
模型调用限额统计 Agent 循环次数，不统计同一次模型请求内部的 HTTP 重试。重试可能增加等待和服务端费用。
没有 `temperature`、`max_tokens`、`response_format`、`memory`、任意 backend 或 sandbox 配置入口。
Hooks 不支持 command、handler/events/match、单 Hook timeout、middleware/callbacks 工厂或 error 事件。

## MCP

格式与语义见 [MCP 配置说明](../docs/mcp.md)。可运行本地示例见
[mcp/.mcp.json](mcp/.mcp.json) 与 [math_server.py](mcp/math_server.py)。
MCP 服务声明不写入产品 `config.json` 或插件 `.plugin.json`。
覆盖 stdio、HTTP、SSE 全部支持字段的模板见 [mcp/mcp.full.json](mcp/mcp.full.json)，
字段、路径及凭证配置见 [MCP 示例说明](mcp/README.md)。完整模板包含占位远程地址，使用前需替换或删除。
