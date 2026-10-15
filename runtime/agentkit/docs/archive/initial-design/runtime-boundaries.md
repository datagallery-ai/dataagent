# Runtime 职责边界优化

> 本文保留此前重构的设计与验收记录，不是当前路径配置规范。project 发现、信任类型及报告字段已由 [Home 统一工作根设计](home-root-cleanup.md) 移除；当前实现以 [架构与配置说明](architecture-and-configuration.md) 为准。旧的 Hooks 覆盖/清空及共享 hooks_base_dir 方案也已废弃。

> 当前文件 backend 已改为原生 LocalShellBackend，无自定义路径限制；下文历史设计中的 `protected_paths` 和受保护路径收集逻辑已删除，输入只读是 prompt 约定。

## 1. 目标与复审结论

本次只整理 `prepare_runtime()` 输出的数据职责，不改变配置格式、配置优先级、项目加载决策、插件选择或 Agent 执行方式。

后续 Hooks 收口已同步到本文的当前字段说明：只保留六事件 Python Hooks，移除 middleware/callbacks 工厂引用。配置迁移与验证见 [Hooks 实施文档](plugin-compiler-native-hooks.md)。第 6 节记录的是此前 Runtime 重构阶段的验证结果。

原 `Runtime(settings, paths, sources, resources)` 不是循环引用，但存在三类可读性问题：

- `sources.model/plugins` 是配置快照，`plugin_origins` 又同时保存在 `sources` 和 `resources`。
- `ResourceIndex.protected_paths` 是文件访问策略的输入，不是可加载能力。
- `project_dir` 实际指项目的 `.dataagent` 目录；扩展使用 `(Path, spec)` 元组，字段意义不直观。

保留四个职责，不合并成一个大配置对象，不引入新的运行时或 Compiler IR。

复审后对原建议作两点限定：

1. 模型名称改为 `Runtime.model_name` 派生属性。宿主无需复制默认模型选择逻辑，也不会产生第二份模型配置。
2. 受保护文件依赖已加载的环境文件及显式配置路径，不能只靠 Home/workspace 提前决定。`prepare_runtime()` 确认这些输入后补齐 `RuntimePaths`，路径模块只接收路径，不依赖 `Environment` 或配置解析模块。

## 2. 四个成员的唯一职责

| 成员 | 保存什么 | 不保存什么 | 使用方 |
| --- | --- | --- | --- |
| `settings: Settings` | 最终有效配置：模型、限制、扩展声明、启用插件、显式插件路径、服务地址 | 自动发现结果、运行中的消息状态 | Agent 装配、REST |
| `paths: RuntimePaths` | Home、workspace、项目配置目录、状态目录、内置插件目录、受保护文件路径 | 配置值、能力对象、权限执行逻辑 | 启动准备、Agent 文件后端、REST 持久化与日志 |
| `resources: ResourceIndex` | 插件候选根目录、独立 Skill 来源、绑定声明目录的扩展引用 | 模型/插件配置副本、来源摘要、受保护文件 | Agent 装配，再交 Compiler |
| `provenance: RuntimeProvenance` | 配置文件路径及是否采用、项目加载决策、插件候选来源分类 | 模型名称、插件启用列表、密钥或扩展 config | 日志、诊断、SDK 查看 |

`Runtime` 是启动准备结果，不是 Agent state、执行会话或动态配置服务。四个成员不互相持有整个对象。

### 2.1 settings 是配置的权威来源

删除 `sources.model`、`sources.plugins`。健康接口保持原字段不变：

- `model` 读取 `runtime.model_name`，属性每次从 `settings.models[settings.dataagent.model.default].name` 计算。
- `plugins` 读取 `runtime.settings.plugins.enabled`。

保留现有 `timeout_seconds`、`redaction_secrets` 派生属性，不新增模型信息缓存。

### 2.2 paths 只负责地址，不执行策略

`project_dir` 改为 `project_config_dir`，默认关系保持不变：

```text
home                 用户级 .dataagent
workspace            用户指定或从启动环境确定的工作目录
project_config_dir   <workspace>/.dataagent
state_dir            <project_config_dir>/state
builtin_plugins      安装包或源码中的内置插件目录
protected_paths      配置文件、环境文件的访问保护路径
```

保留符号链接解析规则。`protected_paths` 同时保留词法路径与真实路径，并去重；跳过项目配置加载不代表放弃对该配置文件的保护。

路径计算放入现有 `bootstrap/paths.py`，只输入 `RuntimePaths`、环境文件路径、显式配置路径和启动 cwd。`prepare_runtime()` 在合并配置后用 `dataclasses.replace` 补齐不可变路径对象，再返回最终 Runtime。基础路径对象只在准备阶段局部使用，不提前暴露一个尚未完成的 Runtime。

`paths.py` 仅负责位置解析和检查，不创建目录、不发现扩展。项目入口探测 `discover_project_sources()` 位于 `bootstrap/discovery.py`，与配置合并后的资源索引分为两个入口；探测结果 `ProjectSource` 位于 `bootstrap/options.py`，供信任请求复用。

`extensions.filesystem_backend.build_filesystem_backend()` 统一装配真实路径文件工具和 Shell 能力，
直接使用原生 LocalShellBackend，设置 session cwd 和 artifacts；`agent.py` 只消费装配结果。

### 2.3 resources 保存绑定，不创建第二套声明

新增局部数据类型 `ExtensionSource[T]`，放在现有 `bootstrap/options.py`：

```python
@dataclass(frozen=True)
class ExtensionSource[T]:
    base_dir: Path
    spec: T = field(repr=False)
```

当前只有 `hooks` 保存这些绑定的 tuple；middleware/callbacks 工厂入口已移除。

例如 `~/.dataagent/config.json` 声明 `hooks/check.py:handle`：

- `settings.dataagent.hooks[0]` 保存 HookSpec。
- `resources.hooks[0].spec` 引用同一个 HookSpec，不复制声明。
- `resources.hooks[0].base_dir` 是该声明所属配置层的目录。

声明和目录的关联不能删除：Hooks 使用最后声明该列表的配置层目录，空列表清空下层配置。目录定位保持现有语义，包括显式配置符号链接所在目录的锚定规则。

`agent.py` 在装配边界把具名绑定转换成 Compiler 的 `hook_entries` 参数所需的 `(base_dir, spec)`，直接传入 `skill_sources`，不把 bootstrap 类型引入 extensions。原 HostBindings 已删除，Compiler 直接返回 Deep Agents kwargs。

### 2.4 provenance 只解释来源

`RuntimeSources` 改名 `RuntimeProvenance`，`Runtime.sources` 改成 `Runtime.provenance`。

`plugin_origins` 只在 provenance 中保存。资源发现函数同一次扫描返回资源索引和来源分类两个结果，`prepare_runtime()` 分别归位，不再重复扫描或复制到资源索引。

来源分类继续表示启用插件 ID 在 builtin/user/project/explicit 中的候选来源，不宣称它是新的完整编译追踪或最终加载路径记录。插件冲突检查仍由原选择逻辑执行。

## 3. 生产与消费流程

```text
LaunchOptions + 环境
  → 基础路径与项目加载决策
  → 配置层读取、校验、合并 → settings
  → 完成保护路径 → paths
  → 能力发现及声明目录绑定 → resources + 插件来源分类
  → 汇总配置采用情况与来源分类 → provenance
  → Runtime
      → build_agent：settings + paths + resources
      → REST：配置派生值 + 存储路径
      → 启动日志：provenance
```

`prepare_runtime()` 仍不加载 Python 扩展、不导入模型工具链、不写 workspace；可选 Home 初始化行为不变。

## 4. 实施步骤与影响模块

1. `bootstrap/options.py`：收窄 provenance、具名扩展绑定、派生模型属性；更新公开导出。
2. `bootstrap/paths.py`：重命名项目配置目录，接收保护路径字段及计算函数；不引入环境/配置模块依赖。
3. `bootstrap/discovery.py`：只发现能力与分类来源，不再接收环境对象、cwd、CLI 配置或计算保护路径。
4. `bootstrap/startup.py`：按既有顺序装配四个结果；`config_files.py` 只同步路径字段名。
5. `agent.py`：在现有装配边界转换具名引用，读取 `paths.protected_paths`。
6. `restapi`：日志读取 provenance，健康接口读取配置派生值；HTTP JSON 与 TUI 启动握手保持不变。
7. 更新 SDK/测试引用及现有架构文档，补充边界和行为回归测试。

不添加旧字段别名，否则会继续保留两套叫法。直接访问旧 Runtime 字段的 Python 调用方需要按以下关系迁移；普通 `prepare_runtime()` → `build_agent()` 调用不变：

| 旧访问 | 新访问 |
| --- | --- |
| `runtime.sources` | `runtime.provenance`（仅来源字段） |
| `runtime.sources.model` | `runtime.model_name` |
| `runtime.sources.plugins` | `runtime.settings.plugins.enabled` |
| `runtime.resources.plugin_origins` | `runtime.provenance.plugin_origins` |
| `runtime.paths.project_dir` | `runtime.paths.project_config_dir` |
| `runtime.resources.protected_paths` | `runtime.paths.protected_paths` |
| `runtime.resources.hooks[i][0/1]` | `runtime.resources.hooks[i].base_dir/.spec` |

middleware/callbacks 已在后续 Hooks 收口中移除，不再迁移为具名绑定。JSON schema version、磁盘目录、CLI 参数与 TUI 代码不变；旧工厂字段需要显式删除。

## 5. 验收

- 无重复模型/插件快照；修改 Runtime 的有效 settings 后，派生模型名称和健康接口读取新的配置，不依赖来源摘要。
- provenance 仅含来源信息；ResourceIndex 不再包含来源分类或保护路径。
- Hooks 绑定正确的胜出层目录，spec 与最终配置是同一对象；继承、替换、空列表清空保持不变。
- Home、项目、显式环境文件和显式配置的词法/真实路径继续受保护，包括符号链接和跳过项目场景。
- 插件来源分类保持原顺序与去重；插件选择行为不变。
- SDK、HTTP、重启恢复、插件调用等现有确定性测试通过；HTTP/启动协议字段不变；无额外模型请求。
- 层级检查仍通过：bootstrap 不依赖 LangChain，extensions 不依赖 bootstrap，REST 通过 dataagent 包入口消费 Runtime。

## 6. 实施结果

上述改动已完成，未新增生产代码文件。现有架构说明和内部 Python 调用方已同步迁移，TUI 源码与配置文件不需要修改。

- 后端确定性回归：`pytest -q -m 'not live' --tb=short`，320 passed、1 deselected。未调用真实模型；24 条告警来自现有 AG-UI 对上游废弃方法的调用。
- 现有 TUI：`npm test --workspace apps/tui`，构建成功、206 项测试通过。
- Ruff 检查通过，沿用排除已有 E701 问题的 `tests/test_tui_cursor.py`；`git diff --check` 通过。
- 新增测试验证职责边界、配置派生健康信息、插件候选来源顺序与去重、符号链接保护；原有三层扩展测试补充声明对象引用身份断言。

## 7. 路径与资源发现职责收口

- `discover_project_sources` 从 paths 移至 discovery；`ProjectSource` 从 paths 移至 options，与信任请求类型相邻。`dataagent.bootstrap.ProjectSource` 的公开导出保持可用。
- `paths.py` 按 RuntimePaths、运行位置解析、基础路径辅助函数排列；`discovery.py` 按信任前探测、配置合并后索引、按调用关系分组的私有辅助函数排列。`startup.py` 原有主入口在前的结构保留。
- 补充函数说明：路径检查不创建目录、词法路径与符号链接解析分开、保护地址不等于权限执行、目录中的 Hook 不会自动启用。
- 对比调整前后的 26 个函数和类定义，除 docstring 和定义位置外，函数体及签名不变。未新增生产模块或配置字段。
- 专项测试 138 项通过；全量后端 345 passed、1 deselected，24 条既有 AG-UI 弃用告警。新增浅层探测不读文件、不深入资源内容、路径辅助函数不创建目录、公开类型身份及单向模块依赖的回归断言。
- Wheel 构建及解包后的初始化检查通过：轻量导入、Home 模板、Runtime 路径、资源发现与公开类型均正常；Ruff、文档链接、包入口 docstring 和 diff 检查通过。
- 未修改用户配置或凭证，未运行真实模型测试或独立 TUI 测试。

## 8. 启动类型按职责归位与 Hook 包装简化

### 8.1 目标与模块归属

`options.py` 原来混放启动输入、准备结果、发现结果、诊断记录和异常，多个名称都包含 Source/Runtime，阅读时需要反复区分。此次按职责归位，并将独立 Hooks 改为跨配置层累加；不改变其他列表的合并规则，不扩展执行能力。

| 模块 | 类型 | 职责 |
| --- | --- | --- |
| `bootstrap/options.py` | `LaunchOptions`，以及 `ProjectMode` 类型别名 | 调用者在启动前提供的参数；不依赖其他产品模块 |
| `bootstrap/runtime.py` | `Runtime`、`StartupReport` | 准备结果与配置参与报告；不保存正在运行的 Agent 状态 |
| `bootstrap/discovery.py` | `ProjectEntry`、`ProjectTrustRequired`、`HookBinding`、`ExtensionLocations` | 项目浅层探测、信任请求、扩展位置及每条 Hook 的来源关联 |
| `bootstrap/config_files.py` | `ConfigSource`、`Layer` | 配置文件参与记录与合并时使用的配置内容 |
| `bootstrap/paths.py` | `RuntimePaths` | 路径解析、校验与显式 Home 初始化；仅 `initialize_home` 写入目录和模板，路径解析仍无写入副作用 |

只新增 `runtime.py` 一个生产模块，不为辅助类逐个建立文件。`Runtime` 和公开 SDK 调用方式保持不变：`prepare_runtime(LaunchOptions(...))` → `build_agent(runtime)`。

Runtime 的四组信息统一为：

- `settings`：使用什么配置，包含唯一一份独立 Hook 声明。
- `paths`：工作区、Home、状态和保护文件在哪里。
- `extensions`：插件候选目录、Skill 来源、每条独立 Hook 与其配置目录的绑定。
- `report`：哪些配置参与了加载、项目加载决定、插件候选来源。

依赖保持单向：runtime 可以使用 discovery/config_files/paths 的数据类型；这些模块不依赖 runtime 或 startup。options 不导入它们。各模块先定义主要数据结构或入口，再放辅助定义；整个 bootstrap 仍不导入模型工具链。

### 8.2 独立 Hooks 累加与逐条来源绑定

独立 Hooks 必须按 Home → 已加载项目 → CLI 累加。此前根据覆盖逻辑将所有 Hooks 归到一个 `hooks_base_dir` 的简化不成立，已撤回。保留具体的 `HookBinding(base_dir, spec)`，不恢复泛型 ExtensionSource，也不单独新增 bindings 文件。

```python
runtime.settings.dataagent.hooks       # 各配置层按顺序累加的有效声明
runtime.extensions.hooks[i].base_dir   # 第 i 条声明所属配置的目录
runtime.extensions.hooks[i].spec       # 引用 settings 中同一个 HookSpec，不复制
```

`merge()` 只对精确字段 `dataagent.hooks` 执行追加；`plugins.enabled`、`plugins.paths` 等其他列表仍替换。Hook 的 params 内部即使存在同名字段，也不触发这个合并特例。

配置文件使用事件分组，在读取层时先规范化为扁平 HookSpec 声明，再执行上述追加与目录绑定。
旧扁平列表仍兼容。空分组或 `hooks: {}` 与 `hooks: []` 一样不追加、不清空。

`locate_extensions()` 按相同层顺序为每条有效声明绑定目录，不再次解析 HookSpec；数量必须严格一致。配置文件本身是符号链接时，仍以声明位置的父目录为基准，不改为目标文件目录。相同相对 entrypoint 在不同配置目录可以加载不同的文件。

`build_agent()` 在装配边界传入 `(binding.base_dir, binding.spec)`。Compiler 接口不变、无需认识 Runtime、HookBinding 或 ExtensionLocations；插件自己的 Hooks 仍以插件根目录为准。

行为约定：

1. 上层不声明 hooks 或声明 `hooks: []`：不追加，不清空之前的注册。
2. 上层声明非空 hooks：追加整层声明，保留层内顺序和各自目录。
3. 同事件、同函数的多次显式注册均保留，不隐式去重。相同真实配置文件被 CLI 再次选中时仍只加载一次，避免自动层重复注册。
4. 插件 Hooks 先按插件启用顺序加入，再追加独立 Hooks；原生 before 正序、after 逆序，工具 wrapper 同样先入后出。
5. SDK 若直接替换、删减或重排 Hooks，必须同时更新 settings 声明及 extensions 绑定；build 检查内容与顺序，不一致时明确报错，不静默执行旧声明。常规使用应修改配置后重新 prepare_runtime。
6. 没有新增清空继承 Hooks 的 JSON 语法；移除注册需修改其来源配置。跳过项目只影响自动项目层，显式配置的既有语义不变。

独立 Skills 已按 Home 与已加载项目目录累加，不走配置列表覆盖逻辑。此次保持实现，补测同一轮实际读取两侧 Skills、关闭插件仍保留独立 Skills、跳过项目不加载项目 Skills。不同文件声明同名 Skill 继续报错，不引入隐式覆盖。

### 8.3 名称与字段迁移

| 原名称或访问 | 当前名称或访问 |
| --- | --- |
| `RuntimeProvenance` | `StartupReport`，位于 `bootstrap/runtime.py` |
| `ResourceIndex` | `ExtensionLocations`，位于 `bootstrap/discovery.py` |
| `ProjectSource` | `ProjectEntry`，位于 `bootstrap/discovery.py` |
| `ExtensionSource[T]` | 具体的 `HookBinding`，位于 discovery；不再使用泛型能力包装 |
| `runtime.provenance` | `runtime.report` |
| `runtime.resources.plugin_roots/skill_sources` | `runtime.extensions.plugin_roots/skill_sources` |
| `runtime.resources.hooks[i].spec` | `runtime.settings.dataagent.hooks[i]` |
| `runtime.resources.hooks[i].base_dir` | `runtime.extensions.hooks[i].base_dir` |
| 临时单目录方案 `runtime.extensions.hooks_base_dir` | 删除，改为逐条 HookBinding |
| `discover_project_sources()` | `discover_project_entries()` |
| `index_resources()` | `locate_extensions()` |

内部旧名称不保留别名。包根的 `LaunchOptions`、`Runtime`、`ProjectTrustRequired`、`prepare_runtime`、`build_agent`、`safe_error` 保持可用。信任异常的 `workspace`/`sources` 字段、HTTP JSON 和 TUI 握手不变；JSON 字段与 schema 版本不变，但 Hooks 的语义明确改为累加。磁盘目录、权限规则、项目确认和其他配置优先级不变。

### 8.4 验证范围

- 结构断言：options 只有 LaunchOptions；其余类型各归所属模块；不存在反向依赖。
- 三层 Hooks 累加、空列表不清除、重复注册均执行、逐条来源目录及符号链接锚定正确。
- SDK 同时修改声明与绑定能影响原生图；绑定缺失或过期时明确失败。
- SDK 与 HTTP 下六类 Hooks 的 before 正序、after 逆序均通过实际事件序列验证。
- 累加更多 Hook 节点暴露了 AG-UI 运行配置的步数上限问题：HTTP 显式传递 graph.config 中原有的 recursion_limit，防止适配器默认的 25 覆盖 Deep Agents 默认值。只传递这项必要默认值，callbacks 仍由原生图继承，避免重复注册。模型/工具调用限额和整轮超时不变，不硬编码新的步数上限。
- 现有协议、会话恢复、插件装配和轻量导入测试继续覆盖调用方。

### 8.5 实施验证

- 累加实现完整回归：357 passed、1 deselected；未调用真实模型。25 条告警均来自现有 AG-UI 对上游废弃方法的调用（新增 HTTP 用例增加一次告警）。
- API 与扩展专项：47 passed；包含 SDK/HTTP 六事件顺序、callbacks 不重复、空列表仍执行用户 Hook、Skills 多来源共存。
- 模块归位阶段曾通过 349 项测试，但其中 Hooks 覆盖用例不符合累加要求；本轮已改为累加断言，并增加实际调用顺序与多来源 Skill 用例，不能以旧测试结果替代新语义验证。
- Ruff 检查通过，沿用既有 `tests/test_tui_cursor.py` 排除项；`git diff --check` 通过。
- SDK 包根导出身份、Runtime 类型注解和不提前导入模型工具链的行为验证通过。
- 本轮未修改 shell、文件权限、状态目录、配置凭证或 TUI 源码，未提交或推送。

### 8.6 Home 初始化并入路径模块

删除仅承载初始化函数的 `bootstrap/home.py`，将 `initialize_home` 和模板映射常量移入 `bootstrap/paths.py`，函数紧随 `home_path`，便于连续阅读 Home 的解析与初始化流程。`bootstrap.initialize_home` 对外入口不变。

模块合并不合并行为：`home_path`、`RuntimePaths.resolve` 及基础路径辅助函数仍不创建文件；`prepare_runtime` 仅在 `options.init_home` 为真时显式调用 `initialize_home`。初始化仍使用目录 `0700`、新文件 `0600`，独占创建模板且不覆盖已有项；模板继续通过包资源读取。导入模块不写入 Home，也不加载模型工具链。

### 8.7 启动准备入口命名

`bootstrap/prepare.py` 改名为 `bootstrap/startup.py`，`prepare()` 改名为 `prepare_runtime()`。模块表示启动准备流程，函数明确返回 Runtime；与 `runtime.py` 中的结果类型保持分离，不改变配置加载、路径解析、信任判断、可选 Home 初始化或扩展定位的顺序及行为。

包根和 bootstrap 统一导出 `prepare_runtime`，不保留旧别名。外部 SDK 调用方需要同步修改导入和调用；当前用法是 `from dataagent import LaunchOptions, prepare_runtime, build_agent`，随后 `runtime = prepare_runtime(options)`、`agent = build_agent(runtime)`。REST 入口、测试和可执行文档示例同步使用新名称，TUI 启动命令和 HTTP 协议不变。
