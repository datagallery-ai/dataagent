# 配置系统从 JSON 迁移到 YAML

> 历史设计与验证记录。当前版本直接使用 YAML，配置入口见[配置指南](../guides/configuration.md)。

状态：设计已确定，交由 Cursor / Grok 4.7 High 实现并验证。

用户已选择：提供一次性迁移命令，运行时只读取 YAML。此文档是实现范围和验收依据；以下新增命令在实现完成前不能当作已可用功能。

## 1. 目标与范围

把 DataAgent 自己维护的、供人编辑的配置和扩展声明统一改成 YAML。配置字段、分层、模型选择、环境变量解析、插件装配与业务行为保持现有契约。规范文件扩展名为 `.yaml`；显式文件路径也接受 `.yml`。

本次检查发现 21 份受版本控制的配置 JSON。另需处理用户 Home、外部插件、Python 启动、Node 启动、TUI 启动、部署向导和文档中的配置读取约定。至少 34 个后端测试文件直接引用旧配置文件名。

以下内容保留当前格式：

- npm 的 `package.json`、`package-lock.json`，TypeScript 的 `tsconfig.json`、`pyrightconfig.json` 等工具链文件。
- `.env` 和 `env.example`。密钥继续通过 `$env{NAME}` 引用，不复制进示例。
- HTTP / AG-UI / MCP / A2A JSON-RPC 请求、响应和事件，模型工具参数，子进程 ready 握手。
- 会话、日志、traces、localStorage、`storage/run/deployment.json` 和测试报告等运行数据。
- 国际化字典 `ui/web/src/i18n/messages/*.json`、业务 ID / 行政区划数据集、`metavisor_responses.json` 响应夹具。
- Markdown 提示词、SQL、Skill 的 `SKILL.md` 及其 YAML front matter。

当前文档和后端说明“控制面没有配置 REST”。因此不新增配置 CRUD 或 YAML 编辑器，不把前端现存 JSON DTO 和协议处理机械替换为 YAML。只修改真实的配置文件入口、帮助文案及相应测试。

## 2. 文件命名和目录方案

| 配置种类 | 当前文件 | 目标文件 |
| --- | --- | --- |
| Home 主配置 | `<home>/config.json` | `<home>/config.yaml` |
| 显式产品配置 | `--config team.json` | `--config team.yaml`，也支持显式 `.yml` |
| 内置初始化模板 | `dataagent/bootstrap/templates/config.json` | `config.yaml` |
| 产品完整示例 | `config.example.json` | `config.example.yaml` |
| 插件清单 | `.plugin.json` | `.plugin.yaml` |
| 插件默认配置 | 清单 `defaults_file` 指向的 JSON | 同目录 YAML，并更新清单引用 |
| 子 Agent 声明 | `subagents/*.json` | `subagents/*.yaml`，并更新清单引用 |
| Home / 插件 MCP 配置 | `.mcp.json` | `.mcp.yaml` |
| MCP 示例 | `examples/mcp/mcp.full.json` | `mcp.full.yaml` |
| NL2SQL 单独加载配置 | `NL2SQLConfig.load(path)` 读取 JSON | 只接受 `.yaml` / `.yml` |
| UDN 维度映射 | 两个场景的 `dimensions.json` | `dimensions.yaml`，更新场景读取路径 |

21 份文件覆盖：`dataagent/plugins/{data-qa,de-agent,nl2sql}` 中的清单、默认配置、子 Agent、两个维度映射；主模板和完整示例；`examples/{config,config-udn,de-agent,nl2sql-entry}.json`；`examples/mcp`；`examples/plugins/{common,llm-utils}`。

自动发现只使用规范名 `config.yaml`、`.plugin.yaml`、`.mcp.yaml`，不增加 YAML/YML/JSON 多格式优先级。任意显式路径（`--config`、`defaults_file`、`subagents[]`、独立 NL2SQL 参数）允许 `.yaml` / `.yml`。旧 `.json` 显式路径给出迁移提示；不能因为 JSON 也是 YAML 的子集而继续接受旧文件名。`.yaml` 中的合法 flow-style mapping 可正常解析。

## 3. YAML 解析契约

Python 增加统一的 `dataagent/config/strict_yaml.py`，所有配置读取入口调用同一函数。沿用已安装的 PyYAML 6，使用独立 SafeLoader 子类，不能修改全局 resolver；调整隐式类型到 YAML 1.2 Core 的语义。Node 增加 `yaml` 2.x 为直接依赖，以统一模块（建议 `ui/shared/config-yaml.mjs`）封装读取、校验和文档更新，TUI 复用该模块并补充需要的类型声明。

两端都必须满足：

1. UTF-8，单文档，根节点是 mapping。拒绝空文件、只有注释、根列表、根标量及多文档。
2. mapping 的键必须是字符串，任意层级重复键报错，不做“最后一个覆盖前一个”。不要先把非字符串键强转成字符串再校验。
3. 值域为字符串、布尔、有限数字、null、列表和字符串键 mapping。日期文本保持字符串；`yes/no/on/off` 保持字符串；`true/false` 为布尔；`012` 不按 YAML 1.1 解释为八进制。`0o` / `0x` 及指数写法如被接受，必须在 Python 与 Node 中一致。
4. 拒绝 `.nan`、`.inf`、数值溢出以及 JS 无法无损处理的整数；建议整数范围统一为 `±(2^53-1)`，超出时明确要求使用引号字符串。迁移不得悄悄改变超大整数。
5. 第一版拒绝锚点、别名、`<<` 合并和自定义标签，避免引入另一套配置继承机制。保留现有应用层合并规则。显式标准 JSON 值类型标签如支持，两端必须一致；不支持的标签明确拒绝。
6. 固定 YAML 1.2，拒绝将文档切换成 1.1 的指令。保留中文、引号、URL、正则、冒号、井号和多行字符串的值。
7. 报错提供文件路径、错误类别和可获得的行列号，不带原始行、原始键和值或解析器的源文本摘录。Node 应禁用 prettyErrors 的源摘录；不能直接插入原始 parser exception.message。
8. 对过深结构、循环/别名等给出有界失败，不允许递归栈错误泄漏配置内容。

PyYAML 原生支持安全加载和自定义 resolver；Node `yaml` 的 Document API 能保留注释并局部修改文档。依据：[PyYAML 文档](https://pyyaml.org/wiki/PyYAMLDocumentation)、[yaml 2 文档](https://eemeli.org/yaml/)。具体兼容行为必须由两端共享夹具验证，不能仅依赖库默认值。

`config/strict_json.py` 可保留给一次性迁移输入使用。运行时产品配置、清单、子 Agent、MCP 和 NL2SQL 配置不得再调用它。数据 JSON 的解析不受此限制。

## 4. Python 后端修改

| 文件 / 模块 | 修改内容 |
| --- | --- |
| `dataagent/bootstrap/config_files.py` | 改为严格 YAML；默认 `config.yaml`；显式后缀校验；检测旧 Home JSON 并给出迁移命令；保持 Layer 与 merge 行为 |
| `dataagent/bootstrap/paths.py` | 初始化复制 YAML 模板；仅存在旧 JSON 时不能创建默认 YAML 把旧配置遮盖掉；继续不覆盖已有文件，权限保持 0600 |
| `dataagent/bootstrap/startup.py` | 启动来源报告改成 YAML；降级启动监听读取也走严格 YAML；有效 YAML 内的 server 参数保持可用 |
| `dataagent/bootstrap/discovery.py` | 扩展 revision 指纹跟踪 `.mcp.yaml` 和 YAML 配置，新增/修改/删除能触发正确刷新 |
| `dataagent/config/plugins.py` | `.plugin.yaml` 发现与加载；仅有旧清单时给可操作错误；保留相对路径、目录越界和依赖环检查 |
| `dataagent/extensions/subagents.py` | 子 Agent YAML 加载与引用校验，仍然按同一个 SubAgentSpec 装配 |
| `dataagent/bootstrap/mcp.py` | Home/插件只读 `.mcp.yaml`；保留 `mcpServers`、传输类型、命名空间与相对命令路径 |
| `dataagent/config/settings.py`、`config/declarations.py` | 错误中的文件名和 JSON 文案改为 YAML / mapping；保持模型约束 |
| `dataagent/cli.py`、`dataagent/plugins/de-agent/scripts/history_sql.py` | 帮助文案、迁移子命令、入口分发；serve-a2a 使用同一配置入口 |
| NL2SQL `config_manager.py` | 独立文件加载改成严格 YAML |
| UDN `common/dimension_join.py` 和两个 `scenario.py` | 维度映射改为严格 YAML，并保持现有业务字段校验 |

主配置 `schema_version: 4`、插件清单 `3`、子 Agent `1` 保持不变；这次改变序列化方式，不引入字段重命名。MCP 继续不加 schema_version，保留 `mcpServers` 的兼容键名。DataAgent 的 `.mcp.yaml` 不宣称可直接被其他客户端作为 `.mcp.json` 使用。

配置合并顺序保持：程序默认值 < 已启用插件的 `plugins.config` 默认值 < 选中的产品文件。`--config` 替代 Home 主配置，不叠加两份。对象递归合并，Agent hooks 和 before_run/after_run 按层追加，其他列表和标量由高层替换。只在选中模型确定之后展开其环境变量。

旧格式或语法错误必须明确反馈，不能被吞掉并悄悄启动到默认端口。现有“模型未配置等原因导致降级服务”可继续保留，但其可用监听配置也必须来自合法 YAML。

## 5. Node / TUI / 部署修改

- `ui/shared/server-listen.mjs`：读取 YAML、识别旧文件、使用 YAML 模板；显式与自动配置的解析错误统一脱敏。不要吞掉损坏配置。
- `ui/tui/src/backend.ts`：移除独立 `JSON.parse` 监听配置路径，复用统一读取逻辑；保留子进程 JSON ready 协议。
- `ui/tui/src/index.tsx`、`scripts/start.mjs`、`scripts/deploy/args.mjs`：更新默认文件名和 `--config` 示例。
- `scripts/stack-runtime-config.mjs` 及部署调用链：确认路径传递不变，`.yaml` / `.yml` 显式路径生效。
- 部署向导写入 `server.host` / `server.port` 时用 Document.setIn 修改，保留无关字段、注释、键顺序和环境变量占位符；不将 YAML 解析为普通对象再重写整份文档。
- 写入使用同目录临时文件和原子替换，私密权限 0600。解析失败不得修改原文件；失败后清理自己的临时文件。
- 共享读取必须覆盖 server 中 `$env{...}` 的来源规则：进程非空值覆盖 Home `.env`，显式产品文件不改变环境文件位置。不能让 Node/TUI 连接到与 Python 监听不同的地址。只展开本次需要的 server 字段，不要求未使用模型的密钥存在。
- `package.json` / 锁文件只添加所需依赖和测试入口，仍为 JSON。除必要文案外不改变 Web 配置 DTO、REST content-type 或第三方凭据 JSON 编辑契约。

## 6. 一次性迁移命令

建议对外接口：

```sh
# 默认只预览 Home 中会转换的配置；尊重 DATAAGENT_HOME
uv run dataagent config migrate

# 指定 Home，预览通过后执行
uv run dataagent config migrate --home /path/to/home --apply

# 指定外部插件，允许重复 --plugin
uv run dataagent config migrate --plugin /path/to/plugin --apply

# 转换单独的产品 / NL2SQL / 声明配置文件，允许重复 --file
uv run dataagent config migrate --file /path/to/team.json --apply
```

实现要求：

1. 未给选择参数时只处理选中的 Home；显式 `--file` / `--plugin` 不隐式把真实 Home 也加入迁移。`--home`、`--plugin`、`--file` 可组合指定。
2. Home 范围包括 `config.json`、`.mcp.json`、Home/plugins 中的插件声明。解析产品配置的 `plugins.paths`，发现引用的外部插件；无法静态确定的环境路径应提示用 `--plugin`，不能猜测目录。
3. 插件迁移追踪 `.plugin.json`、`defaults_file`、`subagents[]`、`.mcp.json`，形成已声明文件闭包；保留插件根目录的路径边界，不递归转换任意 `.json` 数据文件。
4. 显式产品文件也追踪其中声明的插件；任意单独 NL2SQL/维度配置由 `--file` 转换。产品和插件字段中只有确认为配置引用的路径才改后缀；提示词、SQL、数据集路径和普通字符串不得做全局替换。
5. 严格读取 JSON，先解析和检查全部输入、输出位置、引用及冲突，生成转换计划。迁移不展开 `$env{...}`，不导入插件 Python，不连接模型/数据库。
6. 同目录生成 `.yaml`，保持数据类型与顺序，中文可读，多行字符串可读。对子 Agent/插件/defaults_file 的路径引用同步更新。生成后用严格 YAML 重新加载，比较转换前后数据（除明确的引用更新之外）。
7. 源 JSON 原样保留，作为回退副本；迁移期间不覆盖或删除源文件。仓库中随代码发版的 21 份模板/示例则正常重命名为 YAML，避免双份维护。
8. 已有目标 YAML 内容等价时标记 already_migrated，重复运行不修改文件。已有目标不同、不可读或为不允许的链接时停止并报告冲突，不提供默认强制覆盖。
9. 全部预检通过才写；目标采用独占创建/不覆盖策略，0600 权限。中途失败时撤回本次新建的目标，保留源和既有目标；不能声称多个文件整体原子事务。防止并发迁移覆盖他人的输出。
10. 输出只列路径、动作、数量和安全错误。无密钥、原文或展开后的环境值。迁移后提示重启应用。

正常运行时的旧配置处理：有合法规范 YAML 就读 YAML，遗留 JSON 不参与合并；只有旧 JSON 则明确要求迁移，Home 初始化和部署向导也必须执行同一检查。配置里还引用 `.json` 声明时明确报错，不静默忽略该插件或 MCP 服务。

本次实现和验收只使用仓库文件及临时 Home，不自动迁移用户真实 `~/.dataagent`、不重启正在运行的应用。

## 7. YAML 配置示例

```yaml
schema_version: 4
dataagent:
  model: primary
  limits:
    timeout_seconds: 180
    model_calls_per_agent: 128
    tool_calls_per_agent: 128
  output_root: null
models:
  primary:
    provider: openai
    name: "$env{LLM_MODEL}"
    base_url: "$env{LLM_BASE_URL}"
    api_key: "$env{LLM_API_KEY}"
    max_retries: 2
plugins:
  enabled: []
  paths: []
server:
  host: 127.0.0.1
  port: 8790
```

插件清单示例：

```yaml
schema_version: 3
id: common
defaults_file: config.yaml
subagents:
  - subagents/general-purpose.yaml
```

MCP 示例（凭据保留在环境中）：

```yaml
mcpServers:
  example:
    type: http
    url: http://127.0.0.1:8000/mcp
```

## 8. 测试与验收

先新增共享 YAML 契约夹具，再迁移读取入口、模板/引用和测试，最后更新文档。不能只将测试文件后缀换成 `.yaml` 而继续全部写 JSON 文本；关键路径要使用真正的块式 YAML、注释和多行字符串。

| 验收面 | 必须证明的行为 |
| --- | --- |
| 解析器 | 两端对布尔、日期、数值、字符串、UTF-8、重复键、空文件、多文档、非字符串键、别名、标签、深度与脱敏一致 |
| 主配置 | YAML 初次初始化、显式 .yml、分层、模型选择、env、相对路径、旧 JSON 检测；坏配置不会被默认值覆盖 |
| 插件和 MCP | 内置/示例插件清单、子 Agent、默认配置与 MCP 全链路 YAML；依赖、目录边界、刷新指纹不回归 |
| Node / TUI | 地址与 Python 一致，环境变量和端口正常；部署编辑后注释和未改字段保留；异常写入不损坏原文件 |
| 迁移 | dry-run 零写入；全闭包引用更新；不误改业务 JSON；原文件保留；冲突无覆盖；重跑幂等；失败回滚与权限 |
| NL2SQL | 独立参数加载与两个场景的维度映射使用 YAML，SQL 生成/维度校验契约不改变 |
| 发版 | wheel 中包含 YAML 模板与内置插件；示例引用全部存在，活跃文档可执行 |

建议运行并报告实际结果：

```sh
uv run pytest
node --test scripts/server-listen.test.mjs scripts/stack-runtime-config.test.mjs scripts/deploy/*.test.mjs scripts/password-only-cutover.test.mjs
npm run test:tui
npm run test:web
npm run smoke:docs
git diff --check
```

新增 Node 解析器测试也须进入测试命令或 npm 测试入口。对修改的 Python 文件执行 ruff；确认 TUI 构建通过。用 `uv build --project .` 或等效构建检查 wheel 资源，并在临时目录验证安装后的模板/插件加载。测试不得依赖真实模型、付费请求或用户真实 Home。已有失败需给出具体命令与证据，不能隐藏或当作通过。

文档覆盖根 README（中英）、`docs/{zh,en}`、Python 工程说明（`docs/development/python-project.md`）、guides/reference/extensions、examples、CLI 帮助、相关开发契约。历史 archive 可保留历史内容，但必须注明它描述旧 JSON 版本。用于接口和业务结果的 JSON 代码块继续保持 JSON。

## 9. 交付与协作

Codex 负责此设计、范围控制和收到结果后的审查。Cursor 使用 `grok-4.7-high` 负责实现、测试和修复，直接在当前已隔离的 Orca workspace 中工作。设计文件写完后 Codex 不并发编辑实现文件。

实现者不得使用其他模型代替指定模型，不自行再委派，不提交/推送代码，不迁移真实用户配置。交付需包括：修改列表、已执行测试及结果、残留问题、迁移命令示例、保留 JSON 文件的分类说明。非零验证命令不能记为完成。若发现设计与现有行为冲突，应通过 Orca 问答向协调者说明。
