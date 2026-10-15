# DataAgent V2：架构、模块职责与配置机制

本文描述当前代码的架构与配置机制。六类 Python Hook 与薄 Compiler 的函数契约、迁移边界和验收记录见 [实施文档](plugin-compiler-native-hooks.md)。用户、Session 与文件资源隔离的设计见 [用户与多 Session 资源隔离设计](user-session-isolation.md)。产品配置为版本 3，插件清单为版本 2；旧版资料仍可从 Git 历史恢复。

产品配置、插件清单和 SubAgent 声明统一使用标准 JSON；文件清单、字段说明和格式迁移注意事项见
[JSON 配置说明](../examples/README.md)。`.env` 和 Deep Agents 原生 `SKILL.md` frontmatter 不变。

阅读顺序：先看架构边界和模块地图，再看配置如何生成 `Runtime`，最后看 Compiler 如何把能力接入原生图。**如果只关心“Agent 到底依赖哪些外部文件”，直接阅读第 5 节。**

## 1. 核心设计选择

1. **Deep Agents 是 Agent Core，不在外面再写一个 Agent 循环。** 模型调用、工具执行、子 Agent 委派、文件工具、上下文压缩和检查点使用上游机制。当前锁定 Deep Agents `0.7.12`、LangChain `1.3.18`、LangGraph `1.2.11`。
2. **产品配置与能力声明分开。** 产品配置选择模型、限额和启用插件；插件清单声明 Tool、Skill、SubAgent、Prompt 和六事件 Python Hook。没有 middleware/callbacks 工厂配置入口；不是所有 `create_deep_agent` 参数都对 JSON 开放。
3. **启动、编译、装配各有归属，执行交给原生图。** `bootstrap` 找到并校验输入；`extensions` 将能力声明（含 Hooks）编译为原生对象；`agent.py` 补齐模型、backend、宿主策略与权限并建图，不再单设 governance 层。
4. **`Runtime` 是装配结果，不是 Agent 执行器。** 它保存配置、路径、来源摘要、能力资源索引。`build_agent` 消费它并直接返回原生 `CompiledStateGraph`，可继续使用原生调用、流式输出和状态读取接口。
5. **Home 是统一工作根，workspace 仅是可选只读输入。** 配置、扩展、运行状态和 session 输出归 Home；cwd 仅解析相对路径，不发现配置。
6. **REST 是宿主，TUI 是前端。** 核心可以通过 SDK 直接使用，无须 HTTP。TUI 复用仓库的 `apps/tui`，不在 runtime 复制 UI，也没有 Python CLI 转发启动 TUI 的一层。

## 2. 当前架构层级

### 2.1 五个责任边界

| 层级 | 当前实现 | 接收什么、产出什么 | 不负责什么 |
|---|---|---|---|
| 产品入口 | 仓库外层 `apps/tui`；其他软件的 Python SDK 调用方 | 用户输入、启动选项；展示事件或消费原生结果 | 不由 TUI 解析后端 JSON，不重写模型循环 |
| HTTP / 进程宿主 | `restapi` | 启动参数 → `LaunchOptions`；`Runtime` → HTTP 服务、SQLite 生命周期、AG-UI 流 | 不扫描 Home，不编译插件，不维护第二份消息历史 |
| 启动装配 | `dataagent.bootstrap` | `LaunchOptions` + 文件/环境 → `Runtime` | 不导入 LangChain，不执行扩展 Python，不启动 HTTP |
| 扩展编译与 Agent 装配 | `dataagent.agent`、`extensions` | `Runtime` → 插件及独立扩展参数 → 原生 kwargs → 原生图 | 不增加执行 DSL，不重新实现任务调度、压缩或检查点 |
| 原生运行与存储 | Deep Agents / LangChain / LangGraph、原生 backend / checkpointer | 执行模型、工具、子 Agent；保存状态与内部 artifacts | 不代替宿主决定 Home、用户会话索引和进程生命周期 |

### 2.2 三个阶段的先后关系

```text
启动：LaunchOptions → prepare_runtime() → Runtime
建图：Runtime → build_agent() → compile_extensions() → create_deep_agent(**kwargs)
执行：原生图 → 模型 / 工具 / 子 Agent → 结果或流式事件
```

`prepare_runtime()` 不调用 `build_agent()`；读取配置成功不代表模型连通或插件编译成功。
REST 在 lifespan 内加载 MCP 工具和存储，按 session 首次使用时建图并缓存；扩展变化后在下一轮重建。
TUI 只有通过身份一致的 `/healthz` 后才进入聊天。

### 2.3 Python 依赖方向

| 包 / 模块 | 允许依赖的核心代码 | 约束的目的 |
|---|---|---|
| `declarations`、`strict_json`、`diagnostics` | 无包内依赖 | 保持扩展声明、JSON 读取、错误脱敏为稳定叶子 |
| `settings` | `declarations` | 产品设置复用统一声明，不反向依赖编译或初始化 |
| `bootstrap` | 上述基础模块、`bootstrap` 内部 | 配置和信任确认之前不加载模型工具链 |
| `extensions` | 上述基础模块、扩展编译内部模块 | 不反向扫描或依赖 `bootstrap`、`agent.py` |
| `prompts` | `bootstrap.paths` 中的路径类型 | 读取随包模板，渲染宿主提示词；不加载配置、插件或模型 |
| `agent.py` | `bootstrap`、`extensions`、`prompts` 与基础模块 | 唯一同时装配启动结果、编译扩展与原生宿主策略的桥接点 |
| `restapi` | `dataagent` 包根公开 API | 不依赖核心内部文件布局、不读取 JSON/dotenv |

这些边界由 [test_layering.py](../tests/test_layering.py) 检查，包括相对导入、包成员导入和 `TYPE_CHECKING` 内导入。`import dataagent`、`import dataagent.bootstrap` 与 CLI `--help` 均保持轻量。

## 3. 代码目录与每个模块的职责

下列目录以 `runtime/agentkit` 为根；`apps/tui` 是仓库级目录，不在此树内。

```text
runtime/agentkit/
├── pyproject.toml
├── uv.lock
├── config.example.json
├── .env.example
├── README.md
├── docs/
│   ├── architecture-and-configuration.md
│   ├── plugin-compiler-native-hooks.md
│   └── runtime-boundaries.md
├── dataagent/
│   ├── __init__.py
│   ├── declarations.py
│   ├── settings.py
│   ├── strict_json.py
│   ├── diagnostics.py
│   ├── agent.py
│   ├── prompts/
│   │   ├── __init__.py            # 基座提示词渲染入口
│   │   ├── agent.md               # 主 Agent 身份、规划及扩展插槽
│   │   ├── subagent.md            # 公共路径规则及子 Agent 指令插槽
│   │   └── filesystem.md          # 文件与 Shell 的动态路径约定
│   ├── bootstrap/
│   │   ├── __init__.py
│   │   ├── options.py
│   │   ├── runtime.py
│   │   ├── paths.py
│   │   ├── environment.py
│   │   ├── config_files.py
│   │   ├── mcp.py                 # .mcp.json → 原生连接 dict
│   │   ├── discovery.py
│   │   ├── startup.py
│   │   └── templates/
│   │       ├── config.json
│   │       └── env.example
│   └── extensions/
│       ├── __init__.py
│       ├── loading.py
│       ├── skills.py
│       ├── tools.py
│       ├── mcp.py                 # 原生 MCP client → BaseTool
│       ├── subagents.py
│       ├── hooks.py
│       ├── tracing.py             # 原生回调 → LangSmith OTEL → 本地 OTLP JSON
│       ├── trace_exporter.py      # 按请求聚合 Span、脱敏和落盘
│       └── compiler.py
├── restapi/
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py
│   └── sessions.py
├── builtin-plugins/
│   └── attribution-analysis/README.md
├── examples/
│   └── plugins/common/
│       ├── .plugin.json
│       ├── .mcp.json
│       ├── README.md
│       ├── tools/statistics.py
│       ├── skills/number-summary/SKILL.md
│       ├── skills/tabular-inspection/SKILL.md
│       ├── subagents/general-purpose.json
│       ├── prompts/agent.md
│       ├── prompts/general-purpose.md
│       └── hooks/audit.py
└── tests/
```

### 3.1 包根与基础模块

| 模块 | 核心对象 / 函数 | 职责与边界 |
|---|---|---|
| [dataagent/__init__.py](../dataagent/__init__.py) | `__all__`、`__getattr__` | 稳定 SDK 入口；惰性导出 `build_agent`、`load_mcp_tools`，避免单纯导入包就加载 LangChain |
| [declarations.py](../dataagent/declarations.py) | `ToolSpec`、`HookSpec`、`SubAgentSpec`、`PluginSpec`、`MCPConfig`、`StrictModel`、ID pattern | 集中定义扩展声明及共享校验规则；纯 Pydantic，不加载扩展或模型工具链；`ToolSpec` 只声明 entrypoint，不重复定义原生工具参数 |
| [settings.py](../dataagent/settings.py) | `Settings`、`AgentSettings`、`ModelSettings`、`Limits`、`PluginSettings`、`ServerSettings` | 定义产品有效设置；复用 declarations 的 HookSpec 和校验基类；拒绝未知字段，不读取文件或环境 |
| [strict_json.py](../dataagent/strict_json.py) | `read_json` | 使用标准库读取 JSON object，拒绝嵌套重复键、非有限数值和非对象根；错误不回显可能含密钥的源内容 |
| [diagnostics.py](../dataagent/diagnostics.py) | `safe_error` | 生成 `code/message`，对已知密钥、Bearer、常见 key 格式脱敏，移除终端控制字符并限制消息长度 |
| [agent.py](../dataagent/agent.py) | `build_model`、`build_native_middleware`、`build_agent` | 创建模型，组合宿主 middleware，调用文件后端装配入口；补齐宿主 prompt，消费 Compiler 的原生 kwargs 并建图；不读配置文件、不管理服务 |
| [prompts/__init__.py](../dataagent/prompts/__init__.py) | `render_filesystem_prompt`、`render_agent_prompt`、`render_subagent_prompt` | 通过 `importlib.resources` 读取随包 Markdown 模板，填入真实路径和 Compiler 提供的指令；不改变插件所有权，不加载 Skill 正文 |
| [extensions/filesystem_backend.py](../dataagent/extensions/filesystem_backend.py) | `build_filesystem_backend` | 直接装配原生 LocalShellBackend，设置 session cwd、超时和 artifacts；文件与 Shell 均无自定义访问过滤；返回 backend，不创建 Agent 图 |

稳定包根导出为 `LaunchOptions`、`Runtime`、`prepare_runtime`、`build_agent`、`load_mcp_tools`、`safe_error`。项目模式和信任类型已移除。子包导出服务于内部装配，不等于承诺所有内部函数都是稳定 SDK API。

### 3.2 bootstrap：准备配置、路径与资源，返回 Runtime

| 模块 | 核心对象 / 函数 | 职责与边界 |
|---|---|---|
| [bootstrap/__init__.py](../dataagent/bootstrap/__init__.py) | 启动层导出 | 汇集 `prepare_runtime`、路径和输入输出类型；不引入模型工具链 |
| [options.py](../dataagent/bootstrap/options.py) | `LaunchOptions` | 仅定义调用者提供的启动参数，不依赖其他产品模块、不做 IO |
| [runtime.py](../dataagent/bootstrap/runtime.py) | `Runtime`、`StartupReport` | 定义准备结果及启动报告；分别保存 settings、paths、extensions、report；展示信息从 settings 派生，不存会话或 Agent 执行状态 |
| [paths.py](../dataagent/bootstrap/paths.py) | `RuntimePaths`、`home_path`、`initialize_home`、`builtin_plugins_path`、`absolute`、`directory` | 解析和校验运行路径；仅显式调用 `initialize_home` 时创建 Home 目录与模板，独占创建且不覆盖已有文件；不扫描项目扩展、不执行模型权限策略 |
| [mcp.py](../dataagent/bootstrap/mcp.py) | `load_mcp_config` | 读取 Home 和启用插件的 `.mcp.json`，展开已有环境引用并校验 schema，输出冻结的原生连接 dict；不导入 MCP SDK、不启动进程 |
| [environment.py](../dataagent/bootstrap/environment.py) | `Environment`、`load_env` | 解析 dotenv，按优先级合并有效环境并保留文件来源；不写回 `os.environ` |
| [config_files.py](../dataagent/bootstrap/config_files.py) | `ConfigSource`、`Layer`、`load_layers`、`merge`、`expand_env`、`validate_against` | 按 Home → CLI 读取并逐层校验，字典递归合并，独立 Hooks 按层累加，其他列表和标量由后层覆盖；最后展开选中模型并校验完整 Settings、补齐默认值 |
| [discovery.py](../dataagent/bootstrap/discovery.py) | `HookBinding`、`ExtensionLocations`、`locate_extensions` | 配置合并后整理内置/Home/显式插件目录、累加独立 Skill 源、关联每条 Hook 与各自配置目录；HookBinding 引用 Settings 中的声明，不复制；不读插件清单、不导入 Python |
| [startup.py](../dataagent/bootstrap/startup.py) | `prepare_runtime` | 固定顺序调用上述步骤，生成 `Runtime`；不接触终端、HTTP 或插件执行 |
| [templates/config.json](../dataagent/bootstrap/templates/config.json)、[templates/env.example](../dataagent/bootstrap/templates/env.example) | Home 初始化源文件 | 仅由显式初始化复制为 Home 的 `config.json` 和 `.env`，不是每次读取的隐含配置层 |

这几个文件按读者的使用顺序组织：

- `paths.py`：先看 `RuntimePaths` 的输出结构，再看 `home_path` 路径解析及紧随其后的 `initialize_home` 显式初始化，随后是内置插件/保护路径解析，最后是 `absolute`、`directory` 基础辅助函数。导入模块和解析路径均不创建文件；只有 `initialize_home` 创建 Home 目录与模板，且不覆盖已有文件。`absolute` 保留词法路径；`directory` 解析符号链接并检查目录类型，只有 `required=True` 时才要求存在。workspace 选择优先级及是否初始化 Home 仍由 `startup.py` 决定。
- `discovery.py`：结果类型在前，随后是 `locate_extensions` 和私有辅助函数。只发现内置/Home/显式插件及 Home Skills；独立 Hooks 按 Home → CLI 绑定各自配置目录，不自动执行目录中的 Python 文件。
- `options.py`：仅定义启动输入 `LaunchOptions`，不含项目模式或信任决定。
- `runtime.py`：先看核心结果 `Runtime` 及其派生属性，再看仅服务于诊断的 `StartupReport`。runtime 依赖发现结果和配置记录，discovery/config_files 不反向依赖 runtime 或 startup。
- `startup.py`：主入口在前、辅助函数在后；依次解析 Home、环境、配置、只读输入和扩展，最后装配四组结果。不再为寻找项目配置而预读取一遍 workspace 声明。

`Runtime` 中四组数据不能混为一谈：

| 字段 | 实际内容 | 主要消费者 |
|---|---|---|
| `settings` | 最终有效模型、限额、插件选择、扩展声明、server 设置 | `agent.py`；REST 读取服务地址与启用插件 |
| `paths` | Home、workspaces（约定只读）、user_id、state_dir、builtin_plugins | backend / artifacts / prompt；REST 的 SQLite、日志、锁 |
| `extensions` | 候选插件目录、累加的 Skill 源、每条独立 Hook 的 `HookBinding(base_dir, spec)`、冻结的原生 `mcp_servers` 连接参数 | `agent.py` → 插件选择及 Compiler 的 `skill_sources` / `hook_entries` 参数 |
| `report` | 配置文件及是否采用、已启用插件的候选来源类别 | 启动日志与来源诊断，不保存模型/插件配置副本 |
| 只读属性 | `model_name`、`redaction_secrets`、`timeout_seconds` | 从 settings 派生展示名与超时；脱敏值另包括 MCP 凭证，不缓存第二份配置 |

独立 Hook 声明保存在 `settings.dataagent.hooks`，按配置层累加。`extensions.hooks[i].spec` 引用同一个声明对象，新增信息只有该条声明所属配置目录；不能把多层 Hooks 简化为一个共享目录。保留明确的 HookBinding，不恢复泛型 ExtensionSource。`Runtime` 是启动准备结果，不保存会话或运行中的 Agent state。边界与字段迁移详见 [Runtime 职责边界优化](runtime-boundaries.md#8-启动类型按职责归位与-hook-包装简化)。

### 3.3 extensions：统一编译插件与独立扩展，不自建运行时

| 模块 | 核心对象 / 函数 | 职责与边界 |
|---|---|---|
| [extensions/__init__.py](../dataagent/extensions/__init__.py) | 编译层导出 | 汇集 Compiler 和加载函数；声明类型从 `dataagent.declarations` 导入 |
| [loading.py](../dataagent/extensions/loading.py) | `select_plugins`、`contained_path`、`PythonLoader` | 从 bootstrap 提供的位置选择插件、读取清单、校验资源路径和加载 Python 入口；单次编译按真实文件缓存模块供 Tools/Hooks/子 Agent 共享；不扫描 Home、不安装依赖、不提供沙箱 |
| [skills.py](../dataagent/extensions/skills.py) | `scan_skill_directory`、`compile_skill_sources`、`collect_loose_skills` | 复用锁定版本的原生 Skill metadata 解析器，校验名称、来源标签、重复真实文件；不执行 Skill |
| [mcp.py](../dataagent/extensions/mcp.py) | `load_mcp_tools` | 宿主异步装配：消费 Runtime 已解析连接，复用 `MultiServerMCPClient` 发现原生 BaseTool；调用时连接和清理由上游负责 |
| [tools.py](../dataagent/extensions/tools.py) | `collect_tools`、`resolve_tool_refs`、`NATIVE_TOOL_NAMES` | 将 Python 函数/BaseTool 变成 `plugin__local_id` 工具；解析子 Agent 的工具引用；不调度调用 |
| [subagents.py](../dataagent/extensions/subagents.py) | `collect_subagents`、`materialize_subagents` | 读取子 Agent JSON，装配原生声明与局部 Hooks；包括 common 中的 general-purpose，宿主治理由 agent.py 添加 |
| [hooks.py](../dataagent/extensions/hooks.py) | `compile_hooks`、`compile_hook`、`_ToolHookAdapter` | 集中完成普通 Python Hook 的加载、签名校验、稳定命名及原生适配；四类状态 Hook 使用原生装饰器，两类工具 Hook 使用 wrap_tool_call；原生参数和状态返回值不序列化，不自建调度器或加载工厂 |
| [tracing.py](../dataagent/extensions/tracing.py) | `LocalLangChainTracer`、`LocalFileExporter` | 复用 LangChainTracer 与 LangSmith SDK OTEL 转换，专用本地 Exporter 按请求写标准 OTLP JSON；根调用结束后 flush、脱敏和清理，由 build_agent 绑定，不参与插件编译或 HTTP 协议 |
| [compiler.py](../dataagent/extensions/compiler.py) | `compile_extensions`、`ROOT_AGENT_NAME` | 合并已选插件与显式传入的独立 Skill/Hook、已发现的 MCP BaseTool；保留统一重名检查、声明顺序和局部子 Agent 作用域；只返回原生 agent kwargs，不接收 Runtime，不配置自定义运行时 |

内部依赖有方向：`loading` 只使用声明、产品插件选择和 JSON 读取；`skills`、`tools`、`hooks` 使用加载器及必要声明；`subagents` 组合扩展；`compiler` 编排它们。具体模块不能反向依赖 compiler，也不经包级入口互相回引。静态测试维护允许的依赖表。

编译入口直接表达两类来源，不再经过 `HostBindings`：

```python
kwargs = compile_extensions(
    select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots),
    skill_sources=runtime.extensions.skill_sources,
    hook_entries=tuple(
        (binding.base_dir, binding.spec)
        for binding in runtime.extensions.hooks
    ),
)
```

`build_agent()` 在进入 Compiler 前验证绑定与有效声明的内容及顺序一致。手动构造 Runtime 或通过 SDK 替换 Hooks 时，应同时更新声明和绑定；不一致时明确报错，不以 cwd 猜测或静默使用旧声明。优先修改配置后重新 prepare_runtime。

插件 Hooks 按启用顺序先加入，随后是 Home → CLI 累加的独立 Hooks；每层内部保持声明顺序，重复注册不去重。Compiler 不再自行合并配置层。传空插件列表仍可编译独立扩展。没有虚拟插件、CompilerInput 或新的执行 IR。

Hook 适配属于能力编译，宿主默认策略属于 Agent 装配，因此不再保留独立的 `governance/` 包。
这里没有两套 Hook 实现：`compile_hooks` 加载声明后直接调用同文件的 `compile_hook`，适配器由原生图执行。

### 3.4 restapi：宿主而非另一个 Agent 层

| 模块 | 职责与边界 |
|---|---|
| [restapi/__init__.py](../restapi/__init__.py) | 标明本地 AG-UI HTTP 边界，无启动副作用 |
| [__main__.py](../restapi/__main__.py) | 唯一后端启动实现；解析 serve 参数、调用 prepare_runtime、端口检查、日志、uvicorn、就绪通知及前端断开后的退出 |
| [app.py](../restapi/app.py) | 认证、请求校验、AG-UI 转换、流终态/取消/超时；lifespan 持有 checkpointer、会话库与图；每个 thread 最多一轮活动执行 |
| [sessions.py](../restapi/sessions.py) | `Sessions` 保存会话索引和最后成功 checkpoint ID；`workspace_lock` 用 OS 文件锁保证同一状态目录只由一个后端占有；不复制消息存储 |

### 3.5 外部插件示例、安装资源与测试

common 是内部测试/外部插件示例，不再内置或默认启用。需将 `examples/plugins/common`
完整复制到 `<home>/plugins/common`，并在 Home 配置中启用 `common`；无需配置 `plugins.paths`。

| 文件 / 目录 | 职责 |
|---|---|
| [common/.plugin.json](../examples/plugins/common/.plugin.json) | 把 common 的提示词、统计工具、Skill、子 Agent 和六事件审计 Hooks 组合为可启用插件 |
| [common/tools/statistics.py](../examples/plugins/common/tools/statistics.py) | 无副作用数值统计；返回 count、sum、mean、min、max；空列表、非有限数值和溢出使用 ToolException |
| [common/skills/number-summary/SKILL.md](../examples/plugins/common/skills/number-summary/SKILL.md) | 指导模型提取数字、调用实际工具、依据结果回答；不是 Python 执行器 |
| [common/subagents/general-purpose.json](../examples/plugins/common/subagents/general-purpose.json) | 声明 `general-purpose` 的通用 prompt 及自身审计 Hooks，继承根工具与 Skill 来源 |
| [common/prompts/agent.md](../examples/plugins/common/prompts/agent.md)、[general-purpose.md](../examples/plugins/common/prompts/general-purpose.md) | 根 Agent 追加提示词与通用子 Agent 提示词，二者作用域不同 |
| [common/skills/tabular-inspection/SKILL.md](../examples/plugins/common/skills/tabular-inspection/SKILL.md) | 使用原生文件与 shell 工具检查 CSV/TSV 的结构和数据质量 |
| [common/.mcp.json](../examples/plugins/common/.mcp.json) | 插件 MCP 配置入口，默认空服务列表，不发起外部连接 |
| [common/hooks/audit.py](../examples/plugins/common/hooks/audit.py) | 六种普通 Python 函数，只记录事件/Agent 标签及工具名/call ID；不记录问题、参数或结果，不宣称完整调用树或终态审计 |
| [attribution-analysis/README.md](../builtin-plugins/attribution-analysis/README.md) | 占位说明，无可加载清单，不代表归因分析已实现 |
| [pyproject.toml](../pyproject.toml)、[uv.lock](../uv.lock) | Python 3.12 环境、依赖版本、唯一命令入口、wheel 打包；只有 builtin-plugins 随 wheel 分发；common 位于 examples，不打入 wheel |
| [config.example.json](../config.example.json)、[.env.example](../.env.example) | 供复制/显式指定的配置样例；它们不会仅因位于源码目录就自动生效 |
| [tests/](../tests/) | `test_launch/config/home_resources` 验证配置与资源；`test_declarations/compiler/contract/id_contract/layering` 验证声明、编译和分层；`test_agent/hooks/native_context` 验证原生执行；`test_api/launcher/home_startup/product_e2e/tui_cursor` 验证协议、进程、恢复和终端；`conftest.py`、`process_helpers.py` 提供隔离 fixture 和进程辅助 |

本项目中没有 `runtime/agentkit/tui`、`gui` 或 `dataagent/cli.py`。前端接入相关文件位于仓库的 `apps/tui/src/`：`index.tsx` 选择 V2 路径，`v2-backend.ts` 监督后端，`v2.ts` 完成 health/日志接入，`protocol/agui-client.ts` 与 `protocol/v2-session-client.ts` 分别处理事件流和会话恢复；已有 state 与 Ink UI 负责渲染。

## 4. 各包 __init__.py 的原始 docstring

以下逐字保留当前四个包入口的模块 docstring，不以中文概括替代。它们是源代码中对分层责任的直接声明。

### 4.1 dataagent/__init__.py

```python
"""DataAgent V2 core. Install in its own environment, not alongside the legacy package.

Layout, by phase:

    declarations.py extension schemas (pure Pydantic)
    settings.py     effective product configuration
    strict_json.py  duplicate-key-rejecting JSON reader
    diagnostics.py  credential-redacting, terminal-safe error rendering
    bootstrap/      settings, paths, extension locations and report -> Runtime; no LangChain
    extensions/     plugin and standalone declarations -> native Deep Agents kwargs
    prompts/        host templates + session paths + extension instructions -> prompt text
    agent.py        Runtime + compiled extensions + host policies -> Deep Agents graph

Hosts (REST, SDK) use `prepare_runtime` and `build_agent`; hooks receive native objects via middleware.
Nothing else is a stable entry point. Names that need LangChain are imported lazily so that
`import dataagent` stays light.
"""
```

### 4.2 dataagent/bootstrap/__init__.py

```python
"""LaunchOptions → prepare_runtime → Runtime: settings, paths, extension locations and startup report.

Importing this package must stay cheap: no LangChain, LangGraph or Deep Agents imports,
so `--help` and SDK configuration reads never load model tooling.
"""
```

### 4.3 dataagent/extensions/__init__.py

```python
"""Extension assembly: declarations and host filesystem capabilities → native Deep Agents inputs.

Consumes locations supplied by bootstrap; never discovers Home resources itself.
Importing this package loads model tooling; bootstrap uses the lightweight declarations module.
filesystem_backend assembles real-path file access, session guards and shell execution separately from the compiler.
"""
```

### 4.4 restapi/__init__.py

```python
"""Local AG-UI HTTP boundary."""
```

## 5. Agent 的配置思路与外部依赖

### 5.1 先分清四类输入

| 输入类别 | 入口 | 谁解析 | 最终影响 |
|---|---|---|---|
| 启动选择 | `LaunchOptions`；CLI 的 `--workspace/--config/--env-file/--user/--session` | `bootstrap.prepare_runtime` | Home、只读输入、user/session、哪些配置文件参与 |
| 产品设置 | Home / 显式 JSON + `$env{...}` | `config_files`、`settings` | 模型、限额、启用插件、Hooks、server |
| 扩展资源 | `.plugin.json`、子 Agent JSON、Prompt、`SKILL.md`、Python 入口 | 先 discovery 定位，再 extensions 编译 | 原生 Tool / Skill / SubAgent / Hook |
| 宿主注入 | `build_agent(runtime, checkpointer=..., model=...)` | `agent.py` | SDK 可提供模型实例、检查点；追踪通过返回图的原生 callbacks 配置接入，不经过 Compiler |

**Agent 执行中不重新装配扩展。** `prepare_runtime` 固化宿主启动输入，`build_agent` 创建原生图。
REST/TUI 在下一轮请求前检查扩展变化：未变化复用 session 图，变化则校验后重建。
模型、限额及宿主路径/服务配置仍需重启；没有 V2 `/model` 菜单。详见[扩展刷新](extension-refresh.md)。

模型参数不是 dotenv 中的变量名自动映射出来的：JSON 必须写 `"name": "$env{LLM_MODEL}"` 等引用。只设置 `LLM_MODEL` 但完全没有 `models` 配置，或 JSON 把 `name` 写成固定字符串，都不会由这个变量自动生成/覆盖模型字段。

### 5.2 Home、只读输入和 cwd

| 路径 | 选择规则 | 用途 |
|---|---|---|
| Home | 进程 `DATAAGENT_HOME`；未设置时 `~/.dataagent` | 唯一工作根：配置、凭证、Skills/Hooks/Plugins 与内部运行状态 |
| workspaces | CLI/SDK 显式输入 > 有效 `DATAAGENT_WORKSPACE` > JSON `dataagent.workspaces` | 额外只读业务目录；未指定则为空，不创建默认目录 |
| state_dir | `<home>/runtime` | checkpoint、会话索引、锁、日志、各 user/session 输出 |
| cwd | 启动目录，SDK 可显式传入 | 仅解析相对参数；不发现项目配置/扩展，不推断 Git 根 |
| builtin_plugins | 源码 `builtin-plugins` 或 wheel 内资源 | 安装包自带能力，不复制到 Home |

目录解析符号链接。TUI 以 health 返回的 `home/workspaces/stateDir` 展示，不以第一个只读输入冒充工作根。
改变 Home 会同时改变默认配置、扩展和状态位置；改变只读输入不会改变 Home。
恢复已有 session 使用其保存的输入列表，不自动改写文件或迁移目录。

### 5.3 关键文件清单：哪些自动读，哪些不会

| 文件 | 是否必需 / 何时读取 | 责任 |
|---|---|---|
| `<Home>/config.json` | 存在时作为 user 层自动读取 | 个人模型、默认限额、插件选择和扩展声明 |
| `<Home>/.mcp.json` / 启用插件根的 `.mcp.json` | 存在时在准备阶段解析；REST 就绪前连接发现工具 | 不参与产品配置层合并，服务名使用来源命名空间 |
| `<Home>/.env` | 存在时自动读取 | 模型凭证与 JSON 引用的环境值，可提供 `DATAAGENT_WORKSPACE` |
| `--config <文件>` | 显式指定则必须是文件，作为最高 cli 层读取 | 额外配置层，不替换整个 Home 加载流程 |
| `--config` 同目录的 `.env` | 指定 config 且该 `.env` 存在时自动读取 | 该启动方案的环境值；文件名固定为 `.env`，不是 JSON 同名 `.env` |
| `--env-file <文件>` | 显式指定必须存在，独立于配置文件层 | 最高优先级文件环境；不是一层 JSON，也不改变 Home |
| 启动目录的 `config.json` / `.env` | **不因 cwd 身份自动读取** | 只有同时符合 Home/显式配置等规则才读取 |
| `config.example.json` / `.env.example` | 不自动读取 | 样例，不是兜底配置 |
| `bootstrap/templates/config.json` / `env.example` | `initialize_home` 时作为复制源读取 | 生成初始 Home 文件，不覆盖已有配置 |
| `<插件根>/.plugin.json` | 插件被 enabled 选中时读取 | 能力声明，与产品 JSON schema 不同 |
| 插件引用的 SubAgent JSON / Prompt / Python / Skill 文件 | 编译所选插件时按声明处理；未被引用的 Python 入口不导入 | 具体能力的定义与实现 |
| Home 的 `skills/<name>/SKILL.md` | 独立发现、编译时校验，运行时原生渐进加载 | 不要求包装成完整插件 |

最低要求是：合并后必须存在合法 `models` 与选中的模型；若启用插件，声明及实际资源也必须存在。**Home 文件不是 SDK 硬性前提**：SDK 可以用显式配置提供全部必要字段，且默认不初始化 Home。但即使注入 `model=` 测试实例，当前 `prepare_runtime()` 仍先要求完整合法 Settings，不会跳过模型 schema 校验。

### 5.4 JSON 的合并顺序与边界

优先级从低到高：

```text
字段默认值 < Home/config.json (user)
           < --config 指定文件 (cli)
```

“字段默认值”来自 Pydantic，而不是额外隐藏 JSON。逐文件局部模型不填默认值，合并后才补齐默认值，所以未声明和显式空列表不一样。

代码中的优先级分两步实现，均在 `config_files.py` 内：

1. `_config_sources()` / `load_layers()` 按 `user → cli` 返回存在且允许加载的层。
2. `merge()` 不再按 scope 排序，直接按传入顺序处理：独立 Hooks 累加；其他标量与列表由最后声明它的层决定。

例如 Home 配置 `{"server": {"host": "127.0.0.1", "port": 8790}}`，CLI 配置
`{"server": {"port": 8801}}`：最终 host 保留为 `127.0.0.1`，port 为 `8801`。
CLI 未声明的字段不会被默认值覆盖。

- 标量由高层覆盖，mapping 按字段递归合并，`models` 按模型 ID 和字段合并。
- 普通列表整体替换，包括 `plugins.enabled`、`plugins.paths`。用户想在 common 之外启用新插件，需完整声明 `[common, my-plugin]`。
- `dataagent.hooks` 按 Home → CLI 累加，`"hooks": []` 不追加且不清空；同事件、同函数的多次注册保留，每条相对路径以各自配置目录为基准。最终产品 Hooks 排在启用插件 Hooks 之后。middleware/callbacks 配置字段已移除，包括空列表也会报错。
- 独立 Skills 从 Home 的 `skills/` 目录累加，再与启用插件 Skills 组合；没有 `dataagent.skills` 配置列表。同一真实目录去重，不同文件声明同名 Skill 报错，不静默覆盖。
- 不再维护 handler 命名空间或订阅表；每条 Hook 声明直接引用普通 Python 函数。
- `plugins.enabled: []` 只关闭完整插件，不关闭独立 Skill 或配置定义的 Hook。
- 每个文件先单独校验。未知字段、非法类型、重复插件、重复 JSON 键不能因为后续覆盖就被忽略。
- 显式 config 与 Home 配置是同一真实文件时，自动层跳过该文件，只以 cli 层应用一次；路径始终相对于声明文件目录。
- 非 models 字段逐层展开环境引用；models 字段只在合并后展开最终选中的模型。未选中模型仍必须符合结构 schema，但其中的 `$env{MISSING}` 不要求实际存在。

### 5.5 环境变量的合并顺序

优先级从低到高：

```text
Home/.env < --config 旁的 .env < --env-file < 进程环境
```

空字符串按未定义处理，不擦除低优先级非空值；同一真实环境文件只在最高位置读取一次。有效环境作为只读结果传递，不修改进程 `os.environ`。

JSON 的替换语法是 `$env{VARIABLE}`，支持字符串中的引用；变量缺失时启动失败并指出配置文件、字段和 Home `.env`。它不是 shell 表达式，也不会执行命令。

以下变量不允许从 dotenv 改写：`DATAAGENT_HOME`、`DATAAGENT_V2_TOKEN`、`DATAAGENT_V2_INSTANCE_ID`。它们只来自进程环境；写进文件会被忽略并提示。TUI 的 `DATAAGENT_V2_LOG_PATH`、`INIT_CWD` 等前端选项也由 Node 进程读取，不会因为传了后端 `--env-file` 就改变前端环境。

这不意味着插件 Python 的 `os.getenv()` 能看到有效环境：Compiler 未将 `.env` 合并结果注入进程。工具需要的额外凭证由扩展作者明确处理，例如从宿主进程环境读取，不能假设核心会把产品配置注入工具。插件清单本身不执行产品 JSON 的 `$env{...}` 展开；插件 `hooks[].params` 是原始声明字典，不能把其中的环境占位符误认为已经解析的密钥。

### 5.6 当前支持的产品配置字段

| 字段 | 默认 / 限制 | 实际消费者及作用 |
|---|---|---|
| `schema_version` | `3` | 产品配置版本；不等于插件 schema_version |
| `dataagent.model.default` | `primary` | 选择 `models` 中的一个 ID；必须存在 |
| `models.<id>.provider` | 仅 `openai` | 当前实现只构造 ChatOpenAI；不因上游支持更多 provider 就自动开放 |
| `models.<id>.name` | 必填非空 | 发给兼容接口的模型名 |
| `models.<id>.base_url` | 必填非空 | SDK 原样使用；代码不自动补 `/v1`，请填供应商实际 API 基址 |
| `models.<id>.api_key` | 必填非空，SecretStr | 模型认证、错误脱敏；推荐 `$env{LLM_API_KEY}` |
| `dataagent.limits.timeout_seconds` | `180`，正数 | 模型客户端 timeout；REST 另用它限制完整响应流，SDK 不自动有该整轮 deadline |
| `dataagent.limits.model_calls_per_agent` | `128`，正整数 | 每个 Agent 的原生 run_limit，超限报错；不是整棵委派树的共享预算 |
| `dataagent.limits.tool_calls_per_agent` | `128`，正整数 | 每个 Agent 的工具 run_limit，超限报错；不设置会话累计 thread_limit |
| `dataagent.hooks` | 无注册 | 六类事件分组；条目为 Python entrypoint + 可选 params、matcher（仅工具事件） |
| `plugins.enabled` | `[]` | 按顺序选择完整插件；同一列表不允许重复 |
| `plugins.paths` | `[]` | 额外的**插件目录本身**，不是“扫描其所有子目录”的容器 |
| `server.host` | 仅 `127.0.0.1` | REST 监听地址；核心只校验，不使用 |
| `server.port` | `8790`，1–65535 | REST 监听端口；被占用则失败，不复用未知服务 |

只读输入可通过 `dataagent.workspaces` 设置。`models.<id>.max_retries` 配置模型 SDK 原生请求重试，默认 2，0 禁用。
当前没有 JSON 字段用于设置任意 backend、temperature、Agent/工具重试、fallback、审批、memory、远端子 Agent 或任意 `create_deep_agent` 透传参数。未知字段启动即失败；固定策略不能伪装成已支持的配置。

#### 完整产品配置示例

以下可放在 Home `config.json`，也可作为显式 config；不会包含真实凭证。

```json
{
  "schema_version": 3,
  "dataagent": {
    "model": {
      "default": "primary"
    },
    "limits": {
      "timeout_seconds": 180,
      "model_calls_per_agent": 128,
      "tool_calls_per_agent": 128
    },
    "hooks": {
      "before_agent": [],
      "after_agent": [],
      "before_model": [],
      "after_model": [],
      "before_tool": [],
      "after_tool": []
    }
  },
  "models": {
    "primary": {
      "provider": "openai",
      "name": "$env{LLM_MODEL}",
      "base_url": "$env{LLM_BASE_URL}",
      "api_key": "$env{LLM_API_KEY}"
    }
  },
  "plugins": {
    "enabled": [
      "common"
    ],
    "paths": []
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8790
  }
}
```

对应 `.env` 示例，所有值需要自行替换：

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=replace-with-your-key
```

**模型 ID 与模型名不同**：`primary` 是产品配置的索引，`name` 才发往模型服务。可以在 `models` 中保存多个配置，再改 `dataagent.model.default` 并重启；不是运行中的动态切换。主 Agent 与当前声明式子 Agent默认共用建图时的模型，SubAgent JSON 不支持 `model` 字段。

### 5.7 路径相对谁解析

| 路径来源 | 基准与边界 |
|---|---|
| CLI / LaunchOptions 的 workspace、config、env_file | 启动 cwd；TUI 使用 `INIT_CWD ?? process.cwd()` 并传绝对路径 |
| `DATAAGENT_HOME`、`DATAAGENT_WORKSPACE` | 启动 cwd；支持开头的 `~`/`~/` 展开 |
| JSON `plugins.paths` | **声明该字段的配置文件目录**，支持 Home 展开，转成真实路径 |
| 配置 `hooks` 的 entrypoint | **声明该列表的配置文件目录**；必须为根内相对 Python 路径，不能借符号链接越界 |
| 插件中的 prompt、skills、subagents、tool/hook entrypoint | 插件根目录；SubAgent JSON 的内部引用也以插件根为基准，不以 subagents 子目录为基准 |
| 独立 Skills | Home 的 `skills`；允许符号链接，按真实文件去重 |
| 原生文件工具与 Shell | 共用真实路径；`virtual_mode=False`，当前 session outputs 为默认 cwd；输入只读和产出位置由 prompt 约定，不限制宿主路径访问 |

CLI 中的 `~` 通常由 shell 展开；SDK 推荐传 `Path.home()` 组成的路径或绝对路径。不要向 LaunchOptions 传包含字面量 `~` 的相对路径。

例如 `/opt/team/config.json` 声明 `plugins.paths: [./plugins/team]`，选中的插件目录是 `/opt/team/plugins/team`，与 workspace 在哪里无关。`--config` 只追加配置，不改变 cwd 或 workspace。

### 5.8 不再存在 project 层

不扫描 `cwd/.dataagent` 或只读 workspace 的配置和扩展，也不询问信任。
`--config` 仅显式读取该文件及其引用，不发现其相邻 Skills 或 Plugins。
插件与 Hook 仍是本地 Python 扩展；这次来源清理不引入代码沙箱。

MCP 仅从 `<home>/.mcp.json`、启用插件的 `<plugin_root>/.mcp.json` 读取。
格式、三类传输与原生 SDK 接入见 [MCP 配置说明](mcp.md)。REST 就绪前加载一次工具定义；SDK 显式 await `load_mcp_tools(runtime)` 并将结果传给 `build_agent`。

### 5.9 workspace 对 Agent 的具体影响

workspace 只表示可选的只读业务输入，不参与扩展发现，也不决定运行数据归属。
没有输入时列表为空；session 输出和 artifacts 仍写在 Home 下独立目录。
`logs/` 仅预留，当前后端日志统一在 `<home>/runtime/logs/backend.log`。
`build_agent()` 为原生图绑定 `extensions/tracing.py` 中的 `LocalLangChainTracer`。
接入链路为原生 LangChainTracer → `Client(tracing_mode="otel")` → 专用 TracerProvider →
SimpleSpanProcessor → `trace_exporter.py` 中的 LocalFileExporter。不需要 LangSmith 账号、Key、
Collector 或本地服务；专用 provider 只配置文件导出，远程 tracing 环境变量不改变目的地。
显式 `tracing_context(enabled=False)` 会关闭追踪。调用者自行附加的远程 tracer/client 不在此保证范围内。

SDK、TUI/REST 均在根调用成功、失败或正常取消后，等待 SDK 队列排空并 flush provider，
再保存到 `traces/<原生根 run_id>.json`。图可复用，并发请求独立聚合；REST 不管理追踪。
文件为标准 OTLP JSON object，使用 `resourceSpans → scopeSpans → spans`，ID 为十六进制，
时间为 Unix 纳秒，包含 Agent、子 Agent、LLM、工具及中间件调用耗时与父子关系。
Agent、LLM、工具的输入输出可能包含系统提示词、历史消息和完整工具结果；内部中间件只保留
调用信息，不序列化其运行时对象。不记录逐 token 事件，不改变 AG-UI 的展示策略。
这替代原来的自定义 `agents/messages` 格式，已有文件不会自动转换。

JSON 默认 0600，已知凭证脱敏；落盘失败只记录 `TRACE_SAVE_ERROR`，不改变 Agent 结果。
HTTP 的 AG-UI run ID 在 `langsmith.metadata.dataagent_root_run_id`，thread ID 在
`langsmith.metadata.thread_id`。原生 run/parent ID 保留在相应 dataagent_run_id/dataagent_parent_run_id
属性中；输出保留 SDK span ID，并按原生层级校正父子关联。`enabled="local"` 未导出的中间件节点
会被跳过，子节点连接最近的已导出祖先。适配代码针对锁定 SDK 0.12.5 修正最终错误状态与批处理缓存释放。
这不是实时日志：进程强杀时可能无文件；未收到结束回调的调用不伪造结束记录。SDK 未指定 session
时写入 sdk 会话目录。业务数据仍可能存在，且尚无自动清理策略。

默认磁盘布局：

```text
<Home>/                           # 默认 ~/.dataagent
├── config.json                   # 个人产品设置
├── .env                          # 个人凭证
├── skills/<name>/SKILL.md         # 跨项目独立 Skill
├── hooks/<name>.py               # 配置引用的普通 Python 函数，不自动执行
└── plugins/<id>/.plugin.json      # 需 enabled 选择

<Home>/runtime/
├── checkpoints.sqlite              # 原生图状态与消息，按 thread/checkpoint 区分
├── sessions.sqlite                 # 会话索引、状态、最后成功 checkpoint ID
├── backend.lock                    # OS 锁；文件存在本身不表示有残留进程
└── users/<user_id>/sessions/<thread_id>/
    ├── outputs/                    # Agent 写产出和大工具结果
    ├── logs/                       # 预留
    └── traces/<run_id>.json       # 每轮结束后保存的标准 OTLP JSON
```

workspace 输入目录不承载 session 状态；会话隔离由 `(user_id, thread_id)` 和 Home runtime 下的
session 目录共同完成。Home 初始化不创建会话数据库；`prepare_runtime()` 不创建运行时目录。
REST 在启动和 lifespan 中管理日志、锁与数据库；SDK 若需要同样持久化，调用方需明确管理这些资源。

CompositeBackend 使用 `routes={}` 和原生 LocalShellBackend，不再剥离虚拟路径前缀。
`artifacts_root` 为当前 session 的 `outputs/artifacts` 真实绝对路径。
主 Agent 和子 Agent 都会收到真实输入目录与输出 cwd 的提示；输入只读、生成文件写入
session outputs 是 prompt 约定，不是技术保证。

文件操作与 Shell 直接复用原生实现，不增加路径白名单、结果过滤或文件 chmod 包装。
Shell 继承后端进程环境；默认 cwd 为 session outputs，超时、输出截断、退出码由上游处理。
两类工具都可以按宿主权限读取配置文件、访问私有状态，或修改输入及其他 session。
因此也不再收集 `protected_paths`，避免保留无实际执行者的保护策略。
REST 文件下载路径校验、会话归属检查和 Home 私有目录权限不变。
当前模式只适用于可信本地使用，不适用于不可信或多用户服务。

### 5.10 配置最终如何落到原生建图参数

| 外部 / 宿主来源 | 装配过程 | 最终落点 |
|---|---|---|
| `models[default]` | `build_model` 构造 ChatOpenAI，`use_responses_api=False`、`max_retries` 来自模型配置（默认 2） | `model`；显式使用 Chat Completions，仅启用 SDK 原生请求重试 |
| Home session runtime | `build_filesystem_backend` 装配原生 LocalShellBackend 和 CompositeBackend | `backend` |
| workspace inputs + session outputs | 渲染输入只读、产出位置的行为约定，不增加工具权限策略 | `system_prompt` |
| 宿主提示词 + 启用插件 prompt | Compiler 只拼插件 prompt；agent.py 调用 prompts 渲染基座模板并填入插件指令 | `system_prompt` |
| 启用插件 tools | 加载函数/BaseTool，命名为 `plugin__local_id` | `tools` |
| 插件 Skill + 独立 Skill | 校验、去重、保留 `(path, label)` | `skills`，交给原生渐进加载 |
| 插件 SubAgent JSON | 编译工具/Skill/middleware，common 的 general-purpose 也走同一路径 | `subagents` |
| limits + Hook 声明 | Compiler 将 Hooks 编译成原生 middleware；agent.py 加独立宿主治理 | `middleware` |
| REST 的 AsyncSqliteSaver 或 SDK 显式注入 | 由宿主管理生命周期 | `checkpointer` |
| 固定根名称 | `ROOT_AGENT_NAME` | `name="dataagent-v2"` |

Compiler 只返回一个能力 kwargs 字典；model/backend/checkpointer/宿主治理由 agent.py 补齐。其他上游参数没有自动暴露。SDK 需要原生追踪时可以直接调用 `build_agent(runtime).with_config(callbacks=[handler])`，不经插件配置。

## 6. 插件、SubAgent、middleware 与 Hooks 如何协作

### 6.1 插件发现不等于加载，加载不等于执行

发现候选来源依次为内置插件、Home 插件；再由选择器加入 `plugins.paths` 的显式目录。只有 `plugins.enabled` 选中的 ID 才读取清单并编译。相同真实目录去重；同一启用 ID 对应两个不同目录时失败，不做隐式覆盖。

目录名必须等于清单 `id`；插件 ID 和 SubAgent 名允许小写字母开头及数字、下划线、连字符；Tool ID 不允许连字符。`user`、`cli` 继续保留为宿主来源标识，不能作为插件 ID。

产品 JSON `schema_version: 3` 与插件 `.plugin.json` 的 `schema_version: 2` 是两套声明版本。插件字段为 `id/system_prompt/tools/skills/subagents/hooks` 及 schema_version，不接受任意执行 DSL。旧 middleware/callbacks 字段必须显式删除，不能用提高版本号代替迁移。

例如外部示例 common 的关键引用为：

```json
{
  "schema_version": 2,
  "id": "common",
  "system_prompt": "prompts/agent.md",
  "tools": {
    "summarize_numbers": {
      "entrypoint": "tools/statistics.py:summarize_numbers"
    }
  },
  "skills": [
    "skills"
  ],
  "subagents": [
    "subagents/general-purpose.json"
  ]
}
```

这是能力引用示例，完整示例清单还包含六事件 audit Hooks。实际模型看到的工具名为 `common__summarize_numbers`。仅把 Python 文件放进 `tools/` 或 `hooks/` 不会自动启用。

### 6.2 Skill、Prompt 与子 Agent 的作用域

- 根 prompt 由 `dataagent/prompts/agent.md` 填入公共文件路径指令及各启用插件 prompt；SubAgent（含 common 的 general-purpose）由 `subagent.md` 填入相同路径指令及自己声明的 prompt，不继承根 Agent 的插件 prompt。插件的 `prompts/` 不会被全目录扫描。

基座模板集中放在 `dataagent/prompts/`，不与插件的业务提示词混放：

- `filesystem.md`：`{output_directory}` 为当前 session outputs（也是 shell cwd）；`{input_workspaces}` 为按配置顺序生成的只读目录行，允许为空。
- `agent.md`：`{filesystem_instructions}` 为上述渲染结果；`{extension_instructions}` 为 Compiler 按插件顺序拼接的根指令，置于“Enabled extension instructions”章节，在文件环境之后、统一验证与交付要求之前。标题和空行由模板管理，渲染函数只填入原文；无扩展时保留空章节。基座提示词使用英文，不指定回答语言。
- `subagent.md`：`{filesystem_instructions}` 为公共路径规则；`{subagent_instructions}` 为该子 Agent 声明的指令。
- 子 Agent 的可选指令插槽仍在非空时自带两个换行作为分隔，空时不添加分隔；主、子 Agent 的外部文本及尾部换行均原样保留。
- 使用 Python `str.format` 单次渲染每个基座模板，不对填入的路径、JSON 或外部指令再次解析。基座模板中的字面花括号需写成 `{{` / `}}`；外部内容不需要转义。缺失模板变量会明确报错。
- 主 Agent 模板描述当前数据任务定位、按需规划、输入检查、执行验证和交付要求。统计与表格 Skill、子 Agent 和 MCP 均以实际启用能力为准，不承诺旧运行时的数据连接、TaskGraph 或固定报告流程。
- 没有新增用户模板覆盖配置、模板引擎或 Skill 全文注入。模板在建图时读取，已缓存图不随模板文件修改自动刷新；修改基座模板后重启服务。
- Skill 不是另一套工具调度器。编译器验证 metadata 和来源，运行时由原生 SkillsMiddleware 按需读取。
- 子 Agent 不声明 `skills` 时继承根的全部已编译 Skill 来源；显式列表仅加载声明来源，`[]` 不加载。common 的 general-purpose 省略该字段，因此包含 Home Skills。
- SubAgent 的 `tools` 引用注册插件工具，支持本地 ID 或完整 `plugin__tool` 名。
- 每个专用子 Agent 独立实例化自己的限额与配置 Hooks。general-purpose 显式受治理，但不会自动继承根业务 Hooks。
- 当前仅支持声明式 SubAgent，不接受任意 CompiledSubAgent 或异步远端 Agent 配置。

### 6.3 编译与宿主装配顺序

1. 按 enabled 顺序收集插件 prompt、Skill、Tool、SubAgent 与 Hook 引用。
2. 加入独立 Skill 和最终产品 Hook 列表，加载 Python 函数；Hooks 此时只绑定、不调用。
3. 将子 Agent 工具引用解析为原生对象，创建各自独立命名的 Hook middleware。
4. 返回单个能力 kwargs 字典，不配置全局事件运行时。
5. agent.py 加入模型、backend、权限、checkpointer 和主子独立治理，直接返回原生图，不再硬编码 general-purpose。

启用 common 时，其 general-purpose 声明覆盖上游默认同名 Agent；禁用 common 时不再加载该声明。
Deep Agents 仍可能自动生成原生 general-purpose，它不携带 common 的 Hooks 或 DataAgent 给声明式子 Agent 添加的限额。
详见 [common 组件说明](../examples/plugins/common/README.md)。

不再收集全树工具名用于自定义 Hook match DSL；工具过滤由具体 Hook 实现。不同子 Agent 的同名工具不视为全局冲突。

### 6.4 当前 middleware 组合

产品侧统一称 Hooks：声明为 `HookSpec`，函数为 `handler`，编译结果为 `compiled_hooks`。仅在原生类型、API 和装配边界使用 middleware；它不是另一种可配置扩展。具体命名规则见 [Hooks 实施文档](plugin-compiler-native-hooks.md#命名边界)。

Deep Agents 根据原生参数装配 Skills、Filesystem、SubAgent、Summarization、PatchToolCalls 等核心能力；profile / prompt caching 由上游按模型适用性处理，不承诺兼容服务具备缓存支持。

宿主显式传入的顺序为：

```text
TodoListMiddleware（仅根）
→ ModelCallLimitMiddleware
→ ToolCallLimitMiddleware
→ 本 Agent 声明的 Hooks（每条编译成独立原生 middleware）
→ ToolErrorMiddleware
```

这只是宿主追加栈，不是整个原生图的最外层栈。上游将自定义 middleware 合入核心栈后；包装型前项位于后项外层，before 正序、after 逆序。工具限额在某些情况下可能于真正工具执行前拒绝，不应伪造已执行的 tool 事件。

模型/工具限额均 `exit_behavior="error"`，每次 Agent 调用独立计算，不是跨会话累计，也不覆盖所有摘要模型内部调用。ToolError 仅将明确的 `ToolException` 转为可纠正业务错误，未知异常继续抛出。

外部不再提供 middleware 工厂；Hook middleware 名称由编译器按 Agent、事件和序号生成，宿主治理不由插件配置替换。
模型 SDK 默认最多重试 2 次临时请求错误，不添加 ModelRetryMiddleware 或 ToolRetryMiddleware，不重放 Agent/工具或已开始的流式响应。
REST 整轮时限覆盖重试及退避；模型循环限额不等于 HTTP 尝试次数。当前没有启用 fallback、HITL、ShellToolMiddleware、Memory 或另一套上下文编辑机制。

### 6.5 Hook 声明、事件与错误策略

Home config 的个人 Hook 示例（需要自行创建对应 Python 文件）：

```json
{
  "dataagent": {
    "hooks": {
      "before_agent": [
        {
          "entrypoint": "hooks/check_input.py:handle",
          "params": {
            "max_chars": 4000
          }
        }
      ],
      "before_tool": [
        {
          "entrypoint": "hooks/check_tool.py:handle",
          "params": {
            "blocked_tools": []
          }
        }
      ]
    }
  }
}
```

仅支持 `before_agent/after_agent/before_model/after_model/before_tool/after_tool`。
按事件分组，组内不再写 event；旧扁平列表仍兼容。工具事件可配置 matcher，精确匹配
实际工具名（不支持 glob/正则），省略时匹配全部工具。空分组不追加也不清空继承项。
函数可以是 def 或 async def，params 必须显式声明为 keyword-only 参数：

- 四种 Agent/Model 事件：`handle(state, runtime, *, params)`，返回 None、原生状态更新 dict 或 Command。
- `before_tool`：`handle(request, *, params)`，仅返回 None。
- `after_tool`：`handle(request, result, *, params)`，仅返回 None，不替换工具结果。

参数保留原生对象，不缩减为 messages，不序列化为 JSON；params 每次深拷贝。
四类状态事件通过原生装饰器创建独立节点，更新由 LangGraph reducer 合并。
原生无独立工具前后方法，因此两类工具事件通过薄 wrap_tool_call/awrap_tool_call 包装器接入。
before 正序、after 逆序；根配置不自动覆盖子 Agent。

after_agent 是原生收尾节点，不保证异常/取消时执行，也不代表图最终持久化成功。
after_tool 在正常返回时执行，包括错误 ToolMessage；抛异常则不执行。
所有 Hook 异常向外传播，不吞掉、不重试。没有 command、错误事件、action 协议或单 Hook 超时。
REST 整轮时限保留；同步 Python 线程不能强杀。重复声明会重复执行。
同步 Hook 支持 invoke 和 ainvoke/astream；异步 Hook 使用 ainvoke/astream。
完整函数契约及可复制示例见 [实施文档](plugin-compiler-native-hooks.md) 和 [examples](../examples/config.json)。

## 7. TUI、REST 与 SDK 的启动和执行边界

### 7.1 产品启动

仓库根目录执行：

```bash
uv sync --project runtime/agentkit --locked
npm run start:tui -- --v2 --workspace /absolute/existing/project
```

前置条件是 macOS/Linux、Python 3.12、uv、Node 与已安装的仓库 npm 依赖。发行包、CLI 和 Python SDK 包名统一为 `dataagent`。
TUI 的 Node 进程已经是前端，它只拉起 `uv ... dataagent serve --stdio-ready` 后端，不再从 Python 反向启动另一份 TUI。内部 `dataagent-v2` 协议与图名称保持不变，与发行包名无关。

后端 CLI 使用 `init_home=True`；首次缺失时生成 Home 模板，不覆盖已有项。空模型配置会明确失败，当前没有首次启动模型配置向导。可先复制/填写模板再启动。

`--config`、`--env-file`、`--workspace` 原样作为启动选择透传。TUI 生成 instance ID，通过进程环境交给后端；就绪预算连续计时 30 秒；退出只清理它创建的进程，给予 5 秒退出时间。后端用 stdin 管道 EOF 检测前端消失，不依赖猜测残留 PID。

手动后端只有一个实现入口：

```bash
uv run --project runtime/agentkit dataagent serve \
  --workspace /absolute/existing/project
```

本地服务无密码、注册或 Bearer token 验证。`python -m restapi serve` 与 console command 都进入 `restapi.__main__.main`，不是两条独立启动架构。一次启动只允许一个后端持有同一 state_dir。

### 7.2 HTTP 与会话语义

| 接口 | 当前语义 |
|---|---|
| `GET /healthz` | 就绪、协议、实例 ID、模型名、启用插件、home、只读 workspaces、stateDir、timeoutSeconds；不返回 key 或 Home 配置内容 |
| `POST /dataagent/stream` | AG-UI RunAgentInput；每次恰好一条新 user 文本，服务端 checkpoint 是历史权威 |
| `GET /sessions?limit=50` | 会话索引，最多 100 条 |
| `GET /sessions/{threadId}` | 最后成功检查点消息、中断提示、是否需要新会话 |

服务仅监听 loopback，不验证 Bearer token。服务端拒绝客户端注入 state/tools/context/forwardedProps，也不支持任务 replay、消息编辑和审批 interrupt resume；同一 thread 并发返回 409。

使用官方 `LangGraphAgent` 与 `EventEncoder`；子 Agent 内部事件隐藏，根层 task 调用及结果保留。成功只发一个 `RUN_FINISHED`，失败只发一个 `RUN_ERROR`；保留已输出内容，无终态则报告 `INCOMPLETE_STREAM`，不重试整轮。断连取消执行，不继续向关闭的连接写事件。

REST 为整轮流设置 timeout，成功完成后才更新会话索引的 last_checkpoint_id。失败后恢复到最后成功状态，不自动重放中断任务；没有成功检查点时要求新会话。`/clear` 只是新建会话，不删除库。

### 7.3 SDK：复用核心，不隐式获得 HTTP 宿主能力

下面展示调用方主动管理持久化和整轮超时。运行前仍需准备第 5 节的合法配置，显式输入目录必须存在；无输入也可运行；使用 V2 独立 Python 环境，避免与旧版同名 `dataagent` 包混装。

```python
import asyncio
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from dataagent import LaunchOptions, build_agent, prepare_runtime


async def main():
    runtime = prepare_runtime(LaunchOptions(
        workspace=Path.cwd(),
    ))
    # SDK 自己选择和管理数据库；不要与正在运行的 REST 共享同一数据库。
    database = runtime.paths.state_dir / "sdk-checkpoints.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        await saver.setup()
        graph = build_agent(runtime, checkpointer=saver)
        async with asyncio.timeout(runtime.timeout_seconds):
            result = await graph.ainvoke(
                {"messages": [("user", "Summarize 1, 2, 3.")]},
                {"configurable": {"thread_id": "sdk-example"}},
            )
        print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
```

SDK 默认 `init_home=False`，不自动创建 Home；缺失 Home 等同无 user 层。它不会启动 HTTP、sessions.sqlite、Home runtime 单后端锁、产品日志或“最后成功会话恢复”协议；这些都属于宿主职责。示例使用已实现的异步 Hook / 图调用路径，未承诺自定义 Hook 在同步 invoke 下具有相同桥接行为。

## 8. 安全边界、验证与未实现能力

### 8.1 保护了什么，没有保护什么

- 文件工具与 Shell 按宿主权限执行，无自定义访问限制；输入只读、输出位置仅是 prompt 约定。
- 文件工具与 Shell 使用相同真实路径，以 session outputs 为默认 cwd；默认目录不是安全边界。
- REST 下载路径校验、会话归属检查和 Home 私有目录权限保留；它们不限制 Agent 工具或插件 Python 的宿主访问。
- 新建 Home 目录默认 0700、模板文件 0600，已有项不改权限/不覆盖。不要把日志、数据库和凭证提交到仓库；启动器不会替用户修改 `.gitignore`。
- 内置 audit 是生命周期元数据日志，不是完整推理/工具参数轨迹，也不是模型未公开思维链记录。

### 8.2 可执行验证入口

```bash
uv run --project runtime/agentkit --locked pytest runtime/agentkit/tests -q -m 'not live'
uv run --project runtime/agentkit --locked ruff check runtime/agentkit/dataagent
npm test --workspace apps/tui
```

测试使用临时 Home/workspace 与脚本化模型，覆盖原生工具/Skill/SubAgent、Hooks、限额、取消、持久化、重启、真实后端/TUI 进程等路径。确定性通过不等于对任意供应商或终端中文输入法做了验收；真实模型需另行配置并显式运行 live 测试，iTerm2 候选窗口仍需人工检查。

Hooks 收口的实际验证记录见实施文档；不把确定性测试结果当作真实模型连通证明。

### 8.3 当前明确不包含

GUI 实现、完整旧版能力迁移、NL2SQL / 文档召回 / 元数据召回迁移、归因分析插件实现、远程 MCP/A2A、Shell 执行、命令 Hooks、审批恢复、多用户服务、动态模型配置、跨会话 memory。目录或上游库具有相关能力，不代表当前产品已经接入。
