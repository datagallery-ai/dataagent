# Plugin Compiler 与 Python Hooks

## 1. 当前范围

Plugin Compiler 只将配置翻译成 Deep Agents 原生参数字典，
不定义第二套 Agent 执行模型。本版 Hooks 收口为普通 Python 函数、六个调用位置，
全部通过 `AgentMiddleware` 接入。配置仍写明 `event`，用户无需使用装饰器或继承类。

不提供跨语言命令、JSON 事件协议、错误事件、Hook 重试或单 Hook 超时。
不新增安全体系、扩展类型、远端调用、动态加载或其它尚未明确的能力。
不修改 TUI、REST 会话协议、模型配置、workspace 或检查点机制。
`hooks` 是唯一的生命周期配置入口。产品、插件及子 Agent 的 `middleware` / `callbacks`
工厂入口已移除，空列表也拒绝；内部宿主治理继续使用原生 middleware。

## 2. 配置与普通函数

产品配置保持 `"schema_version": 3`，插件保持 `"schema_version": 2`。
产品、插件和 SubAgent 配置统一采用 JSON；字段含义不因格式变化而改变。文件名与迁移范围见
[JSON 配置说明](../examples/README.md)。产品配置使用六个事件分组；每组条目包含必填
`entrypoint`、可选工具名 `matcher`（仅工具事件）和可选 `params`（默认 `{}`）。
编译器会将其规范化为内部 `HookSpec(event, entrypoint, matcher, params)`；入口为
`file.py:function`。

`matcher` 精确匹配实际工具名，不支持 glob/正则；省略时匹配所有工具。
分组内禁止重复写 event，未知事件或非列表分组报错；空分组不追加、不清空已有注册。
旧的 event + entrypoint 扁平列表仍兼容，插件和声明式 SubAgent 同样支持分组格式。

```json
{
  "schema_version": 3,
  "dataagent": {
    "hooks": {
      "before_model": [
        {
          "entrypoint": "hooks/check_input.py:handle",
          "params": {
            "max_chars": 4000
          }
        }
      ],
      "after_tool": [
        {
          "entrypoint": "hooks/observe_tool.py:handle",
          "params": {}
        }
      ]
    }
  }
}
```

上面是可覆盖默认配置的局部文件。模型、插件、服务地址齐全的文件见
[examples/config.json](../examples/config.json)。

`hooks/check_input.py`：

```python
def handle(state, runtime, *, params):
    # state 是原生 AgentState，messages 中是原生 Message 对象。
    latest = next((m for m in reversed(state["messages"]) if m.type == "human"), None)
    if latest and isinstance(latest.content, str):
        if len(latest.content) > params.get("max_chars", 4000):
            raise ValueError("Input is too long")
    # 可以读取 runtime.context 等原生接口；不需要导入 LangChain 才能使用它们。
```

`hooks/observe_tool.py`：

```python
import logging


async def handle(request, result, *, params):
    logging.getLogger(__name__).info("Tool returned: %s", request.tool_call["name"])
    # result 原样提供，可能是 ToolMessage，也可能是 Command。
```

函数可为 `def` 或 `async def`。`config` 必须显式声明为 keyword-only 参数，
即签名中的 `*, params`；其它位置参数按位置传递，无需强制变量名或类型注解。
不接受已装饰的 middleware 对象、类、任意 callable 或旧 `handle(event, config)`。
已有工厂需要自行提取可由六种事件表达的普通函数；不能原样放进 hooks。

### 六个事件的参数与返回值

| event | Python 签名 | 原生接入点 | 返回 |
| --- | --- | --- | --- |
| `before_agent` | `handle(state, runtime, *, params)` | `before_agent` 装饰器 | `None` / 状态更新 dict / `Command` |
| `after_agent` | 同上 | `after_agent` 装饰器 | 同上 |
| `before_model` | 同上 | `before_model` 装饰器 | 同上 |
| `after_model` | 同上 | `after_model` 装饰器 | 同上 |
| `before_tool` | `handle(request, *, params)` | `wrap_tool_call` / `awrap_tool_call` 调用工具前 | 仅 `None` |
| `after_tool` | `handle(request, result, *, params)` | 同一原生包装接口，正常返回后 | 仅 `None` |

LangChain 并没有独立的 `before_tool` / `after_tool` 方法；这两个配置名是薄包装器
提供的便利位置。包装器只检查或观察一次，不支持接管 handler、多次调用或结果替换。

`state/runtime/request/result` 保持原生对象，不裁剪成 `state["messages"]`，
也不转为 JSON。`params` 每次调用隔离复制，修改它不影响后续调用。
状态变化应以返回值提交，不应原地修改 state。Compiler 不合并状态，LangGraph
按原生 reducer 更新，每个 Hook 节点能看到前一个节点已提交的更新。

例如在模型前添加消息：

```python
from langchain_core.messages import HumanMessage


def handle(state, runtime, *, params):
    return {"messages": [HumanMessage(content=params["reminder"])]}
```

`Command` 直接交给原生图解释，只能使用图已支持的状态键和合法目标。
本版不增加自定义 state schema、context schema 或跳转目标配置。
不支持 `{"action": "continue"}` / `{"action": "deny"}`；拒绝执行直接抛异常。

## 3. 来源、顺序与作用域

1. 配置层优先级：默认值 → Home → 显式配置；不再发现项目配置层。
   `dataagent.hooks` 按 Home → 显式配置累加，`[]` 只表示这一层不追加，不清空低层；不对注册去重。模型字段和插件列表保持原覆盖规则。
2. 插件按 `plugins.enabled` 顺序编译，再追加有效产品配置的 Hooks。
   两条相同声明就是两个节点，按原生顺序执行两次。
3. 每条独立 Hook 通过 HookBinding 保留其声明配置文件目录；相同的相对 entrypoint 在不同目录可以指向不同函数。插件及其 SubAgent
   入口相对插件根目录，不相对 `subagents/` 目录。
4. 仅启用且被引用的函数会加载；放进 `hooks/` 目录本身不会执行。
5. 产品配置和插件根 Hooks 作用于主 Agent。子 Agent 只执行自身显式声明的 Hooks，
   不自动继承父 Agent；general-purpose 保留独立宿主治理，不隐式继承业务 Hooks。
6. Hook middleware 按声明顺序排列，位于宿主限额之后、ToolError 之前。
   原生 before 正序、after 逆序；tool wrapper 也是外层先进入、后退出。
   并行工具分别触发自己的 Hooks，不保证不同工具调用之间的全局顺序。

声明式 SubAgent 使用相同结构：

```json
{
  "name": "specialist",
  "description": "A focused assistant",
  "system_prompt": "prompts/specialist.md",
  "tools": [],
  "hooks": {
    "before_model": [
      {
        "entrypoint": "hooks/check_input.py:handle",
        "params": {
          "max_chars": 2000
        }
      }
    ]
  }
}
```

其中 `prompts/specialist.md` 是插件根目录下实际存在的提示词文件。

## 4. 执行与错误语义

- `before_agent` 每次 Agent 调用进入时执行；模型前后随原生主循环触发。
  摘要器内部的所有模型请求不保证经过主循环 Hook。
- `after_agent` 是原生收尾节点，不是图最终成功通知，也不是 `finally`。
  模型、工具、初始化或取消失败时，不保证它执行；后续检查点持久化仍可能失败。
- `after_tool` 在包装范围内调用正常返回时执行，包含状态为 error 的 ToolMessage
  或包含工具消息的 Command。若调用抛异常，则不运行该调用的后置 Hook。
- Hook 抛异常按原生路径传播，包括后置 Hook；不静默吞掉、不自动重试，
  HTTP 继续通过现有 `RUN_ERROR` 展示真实错误。
- 超限可能在进入工具 Hook 前被宿主拒绝，不伪造工具已执行事件。
- 同步 Hook 支持 `invoke` 和 `ainvoke/astream`；异步 Hook 使用 `ainvoke/astream`，
  同步入口遇到异步 Hook 明确失败。
- 无自建调度器、终态 Callback、调用树、去重缓存或子进程。REST 原有整轮时限保留；
  同步函数在异步执行时运行于线程，取消不承诺强制终止该线程。

## 5. 实现位置与装配

| 模块 | 唯一职责 |
| --- | --- |
| [declarations.py](../dataagent/declarations.py) | Tool、Hook、Plugin、SubAgent 统一声明；Hook 限定六事件与 `entrypoint/params`，拒绝旧字段 |
| [settings.py](../dataagent/settings.py) | 产品设置通过 `dataagent.hooks` 复用同一个 HookSpec |
| [extensions/hooks.py](../dataagent/extensions/hooks.py) | Hook 编译完整链路：按声明根加载函数、校验签名、生成稳定名称并适配；四类状态 Hook 使用原生装饰器，两类工具 Hook 使用薄包装器，返回 `compiled_hooks` |
| [extensions/compiler.py](../dataagent/extensions/compiler.py) | `compile_extensions` 将已选插件与独立 Skill/Hook 组合成单个原生 kwargs 字典 |
| [extensions/loading.py](../dataagent/extensions/loading.py) | 插件选择、路径校验及 Python 对象加载；共享一次编译内的模块缓存 |
| [extensions/subagents.py](../dataagent/extensions/subagents.py) | 为声明式子 Agent 独立编译局部扩展 |
| [agent.py](../dataagent/agent.py) | `build_native_middleware` 将原生宿主策略与 `compiled_hooks` 组合，不根据 Hook 类型做分支；从 `extensions/filesystem_backend.py` 获取 backend 和权限；补齐模型、checkpointer 等宿主参数并创建图 |

每条 Hook 都是一个独立、唯一命名的原生 middleware。没有 Hook 时不创建空适配节点。
命名为 `ConfiguredHook__<agent>__<event>__<index>`，不使用 LangGraph 保留分隔符。
名称由编译器按 Agent、事件、序号生成，用户不再通过工厂提供任意名称的 middleware。

宿主追加栈：

```text
TodoList（仅主 Agent）→ ModelCallLimit → ToolCallLimit
→ 配置编译的 Hooks → ToolError
```

这是 Deep Agents 核心 middleware 之外的追加列表，不是整个原生栈的最外层。
主/子 Agent 和 general-purpose 的独立限额不变。

```python
agent_kwargs = compile_extensions(...)
# agent.py 补充宿主参数及主子治理。
return create_deep_agent(**agent_kwargs)
```

`HostBindings` 已删除；`agent.py` 直接传入 `skill_sources` 和 `hook_entries` 两个关键字参数。
Compiler 不接收 `Runtime` 或 bootstrap 的类型；`ExtensionLocations` 保存插件/Skill 位置及 HookBinding，后者引用有效声明并补充各自目录，不复制配置或携带工厂引用。
`FactoryEntry`、`create_extension` 和 `plugin_middleware.py` 已删除；原生治理栈只接收编译后的 Hooks。
common audit 使用六种普通函数，根与 common 的 general-purpose 子 Agent 各自显式声明；不是根 Hooks 自动传播。
日志只记录事件、Agent 标签及工具名/call ID，不记录问题/参数/结果，也不再承诺完整调用树或错误回调。
HTTP 错误仍由 REST 的 `RUN_ERROR` 与错误日志处理，不能用 after_agent 替代终态判断。

需要 SDK 原生追踪时直接使用 `build_agent(runtime).with_config(callbacks=[handler])`，
或调用 `ainvoke/astream` 时传原生 config；不再提供 `build_agent(..., callbacks=...)` 参数。
Callbacks 不经过 Compiler，也不用于实现六种配置 Hooks。SDK/HTTP 使用同一个装配入口。

### 命名边界

产品语义统一称 Hooks，middleware 仅是 LangChain 的原生接入抽象，不是第二套扩展。

| 所处阶段 | 名称 | 含义 |
| --- | --- | --- |
| 配置声明 | `HookSpec`、`hooks` / `hook_entries` | 事件、入口、参数，以及加载来源 |
| Python 函数 | `handler` / `hook_handler` | 用户提供的普通函数 |
| 编译结果 | `compiled_hook` / `compiled_hooks` | 已适配为原生 `AgentMiddleware` 的钩子对象 |
| 原生装配 | `build_native_middleware(..., compiled_hooks=...)` | 组合内置 Todo、限额、ToolError 与编译后的 Hooks |

`extensions/hooks.py` 统一负责声明加载、函数校验和单函数适配，
工具前后适配类为 `_ToolHookAdapter`；`agent.py` 负责宿主默认策略与能力的最终装配。
上游 `AgentMiddleware`、`wrap_tool_call`、
`middleware=` 和 kwargs 的 `"middleware"` 键保持原名，不做别名包装：

```python
compiled_hooks = compile_hooks(hook_entries, loader, agent_name=agent_name)
agent_kwargs = {"middleware": compiled_hooks}  # 原生入参片段，不是另一个配置入口。
```

命名与目录归属调整不改变六事件、顺序、函数签名或生成名称
`ConfiguredHook__<agent>__<event>__<index>`；配置和检查点无须迁移。
原 `governance/hooks.py` 合入 `extensions/hooks.py`，原 `governance/middleware.py` 的装配辅助函数
移入 `agent.py`，删除整个 `governance/` 源码包，不保留兼容转发或新增封装层。

## 6. 迁移与验证

保持 JSON 版本不变，但有旧 Hooks 的文件必须显式迁移：

1. 删除 `command`、单 Hook `timeout_seconds`、旧 `hook_handlers/handler/events/match`。
   每条配置直接写 `event/entrypoint/params`；外部脚本需改为 Python 函数。
2. 删除 `agent_error/model_error/tool_error`；本版没有这类业务 Hook。
   原生 Callback 的观测能力只能由 SDK 调用方通过原生图运行配置提供。
3. 把 `(event, config)` 改为上表对应签名，保留 keyword-only `params`。
   改用原生对象属性；工具名为 `request.tool_call["name"]`。
4. 正常观察返回 None，拒绝抛异常；状态更新返回原生 dict/Command。
   不再返回 action 决定。后置失败不再只是记录日志，会终止当前执行路径。
5. 检查 `after_agent`：若依赖图最终成功语义，不能把它当作完成确认。
   根与子 Agent 的声明仍需分别配置。
6. 删除产品、插件及子 Agent 中的 `middleware`、`callbacks`，包括 `[]`。
   配置逐层验证：低层存在旧字段时，不能用高层覆盖掩盖错误。
   Home 不会自动改写；删除空工厂字段即可，非空工厂需要人工迁移。
7. Compiler 调用从旧 `kwargs, run_config = compile_plugins(...)` 改为
   `kwargs = compile_extensions(selected_plugins, skill_sources=..., hook_entries=...)`。
   内部导入路径改为 `dataagent.extensions`、`dataagent.bootstrap`；声明从 `dataagent.declarations` 导入。
   `PythonEntry` 改名 `ToolSpec`，JSON 的 `tools.<id>.entrypoint` 不变；包根 SDK 入口不变。

配置加载拒绝旧字段和未知事件，编译拒绝旧函数签名，执行拒绝旧 action 返回值。
不自动改写用户 Home、凭证或模型配置。

验证包括：六事件真实调用与顺序、同步/异步、原生参数 identity、runtime.context、
多 Hook reducer 更新、Command、工具错误返回/异常、取消、并发隔离、SDK/HTTP 一致、
子 Agent 作用域、配置迁移错误、宿主限额、会话持久化及现有 TUI 回归。

### 扩展编译与初始化命名收口验证

- `capabilities/` 改为 `extensions/`，`launch/` 改为 `bootstrap/`；根 SDK 的 `prepare_runtime`、`build_agent`、`LaunchOptions` 等入口保持不变。
- `compile_extensions` 直接接收已选插件、`skill_sources` 和 `hook_entries`；删除 HostBindings 及其模块，不新增输入中间类型或虚拟插件。
- 插件选择、资源路径校验及 Python 加载合入 `extensions/loading.py`；模块缓存仍由每次编译独立创建，并在该次编译的 Tools/Hooks/子 Agent 间共享。
- Tool/Hook/Plugin/SubAgent 声明集中到纯 Pydantic 的 `declarations.py`。与重构前逐项比较 Settings、HookSpec、ToolSpec、PluginSpec、SubAgentSpec 的 JSON Schema，除 PythonEntry → ToolSpec 的名称和说明外，结构完全一致。
- 全量后端回归：342 passed、1 deselected；24 条既有 AG-UI 弃用告警。专项 207 项通过；新增共享 Hook 声明、Tool entrypoint 格式、无插件独立扩展编译、跨来源 Skill 重名及输入不被修改的用例。
- Wheel 无旧 capabilities/launch/governance 模块，包含新 bootstrap 模板和内置插件资源。从解包后的安装内容验证轻量导入、Home 初始化、prepare_runtime、内置插件/Hooks、真实统计工具及最终回答；无网络请求。
- Ruff（仍排除已有 E701 的 test_tui_cursor.py）、四个包入口 docstring、文档本地链接及 diff 检查通过。未修改用户 Home 或凭证，未运行真实模型测试或独立 TUI 测试。

### Hooks 目录收口验证

- 两个 Hook 模块合为 `extensions/hooks.py`；原生策略与文件权限装配移入 `agent.py`；删除 `governance/` 源码包，没有兼容转发层。
- 全量后端回归：336 passed、1 deselected；24 条告警来自既有 AG-UI 弃用接口。未请求真实模型，本次未重复运行独立 TUI 测试。
- 专项测试 114 项通过；依赖检查禁止能力编译反向导入 `bootstrap` 或 `agent.py`，覆盖 Hook 模块的绝对、相对及包根导入。
- Wheel 构建与内容检查通过：无旧 governance 模块，保留内置插件清单和 Skills；从解包后的安装内容完成插件/Hooks 加载、真实统计工具调用及最终回答的无网络验证。
- Ruff（沿用已有 `test_tui_cursor.py` 排除）、四个包入口 docstring、文档本地链接及 `git diff --check` 通过。配置格式、事件行为、装配顺序、生成节点名称保持不变。

### Hooks 命名统一验证

- 本次只调整模块、函数和局部变量命名；配置格式、六事件行为、原生 `middleware` 入参和生成节点名称保持不变。
- 后端全量回归：333 passed、1 deselected；24 条告警来自既有 AG-UI 弃用接口。未请求真实模型，本次未重复运行独立 TUI 测试。
- 专项测试 111 项通过；新增主/子 Agent 装配顺序、Hook 对象引用及治理实例独立性验证，并断言工具适配器名称不变。
- Wheel 包含 `extensions/hooks.py`，不包含旧 `extensions.py` 和已删除的 `plugin_middleware.py`；内置插件清单和 Skills 资源完整。
- Ruff（沿用下述已有文件排除）、模块入口 docstring、文档本地链接及 `git diff --check` 通过。

### Hooks 单入口收口验证

- 后端全量确定性测试：331 passed、1 deselected；24 条告警来自既有 AG-UI 弃用接口。未请求真实模型。
- TUI：构建与 206 项测试通过，未修改 TUI 源码。
- 新增/调整覆盖：所有配置层及插件/子 Agent 拒绝旧工厂字段（包括空列表）；Hook 多层来源及对象引用（最新累加规则见第 3 节）；Compiler 单 dict 输出；直接返回原生图及原生 callbacks 观测；内置六事件审计不记录消息/参数/结果。
- 删除只服务于任意 middleware 工厂的类型、名称和私有工具登记逻辑；限额、Todo、ToolError 与原生文件权限行为由原回归用例验证。
- Wheel 构建成功，隐藏插件清单、普通 Python 审计函数及 Skills 均在包内，已删除的 `plugin_middleware.py` 不在包内。
- Ruff、包入口 docstring 一致性、文档本地链接及 `git diff --check` 通过；Ruff 继续排除已有 E701 的 `test_tui_cursor.py`。
- 只读检查当前 Home 配置：没有 middleware/callbacks 字段，无须此次迁移；未修改 Home 或凭证。

### 六事件初版验证记录（收口工厂入口前）

| 验证 | 本轮结果 |
| --- | --- |
| 全量后端 `pytest -q -m 'not live' --tb=short` | 291 passed，1 个真实模型用例未选中；24 条上游 AG-UI 弃用警告 |
| 现有 TUI `npm test --workspace apps/tui` | TypeScript 构建及 206 项测试通过；未改 TUI 源码 |
| 原生 Hook 专项 | 全量中的 52 项，覆盖对象 identity、sync/async、原生 reducer、Command、执行顺序、异常及取消 |
| 示例 | 全量中的 3 项，直接加载样例配置和 Python 函数，验证 SDK 成功、输入拒绝及 HTTP 单一 RUN_ERROR |
| 产品链路 | 全量测试包含真实 TUI/后端进程、Tool/Skill/SubAgent、检查点及重启恢复；模型响应为确定性替代 |
| Ruff | 生产代码、示例及测试通过；排除未修改的 `test_tui_cursor.py`，该文件存在原有 E701 |
| Wheel | 构建成功；包含新适配器、Compiler、隐藏插件清单及 Skills，不包含已移除的执行器 |
| 文档与 diff | 包入口 docstring 与源码一致、本地文件链接及 `git diff --check` 通过 |
| 当前 Home 配置 | 只读加载并完成原生图装配，无模型请求、无用户配置迁移 |

没有调用真实模型，也没有新增人工 iTerm2 中文输入法验收。
