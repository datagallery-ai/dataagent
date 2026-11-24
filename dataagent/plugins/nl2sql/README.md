# nl2sql builtin plugin

将自然语言数据问题转换为可信 SQL 并在目标数据库上执行的独立子代理。
由 dataagent 仓库的旧版 nl2sql 代理移植而来：运行时不依赖宿主任何模块——以
`kind="compiled"` SubAgent 形式接入，宿主编译插件时调用图工厂
`subagents/nl2sql.py:build_subagent`，把业务参数、宿主解析的模型与宿主
持有的语义层客户端（根配置 `semantic` 段，经 `services.semantic_client`
注入）注入 LangGraph 原生 `StateGraph` 工作流。配置、提示词全部
插件内自包含；模型与数据库连接来自 Agent 配置。

## 结构

```
nl2sql/                      插件根
├── .plugin.yaml             插件清单（id=nl2sql，声明子代理与主代理路由提示）
├── prompts/routing.md       注入主代理系统提示：只能经 nl2sql 查已配置的库，不搜文件、不用 shell 查库
├── subagents/
│   ├── nl2sql.yaml          SubAgentSpec（kind=compiled，指向图工厂与描述文件）
│   ├── nl2sql.description.md 子代理描述（主代理在 task 工具中看到的契约）
│   └── nl2sql.py            build_subagent 图工厂：参数/模型/语义客户端注入、
│                            任务协议适配（messages→question，JSON AIMessage 出参）、
│                            会话结果文件（已取行写成 CSV 快照 + meta）、
│                            向描述追加已绑定数据库摘要（db_id/dialect/engine，不含连接参数）
├── scenarios/                场景包挂载点：`scenario` 键按点分名路由
│                             （`udn.business_twin` → `scenarios/udn/business_twin/`）；
│                             udn/ 收编 business_twin、traffic_insight 两场景 + common/ 共用机制
├── docs/                     特性设计文档（udn-scenario-packs.md：钩子位机制与场景包规则）
└── nl2sql/                  Python 包
    ├── graph.py             StateGraph 组装（节点链实例化、路由规则 → 普通边/条件边）
    ├── state.py             NL2SQLState（langgraph MessagesState 派生）+ Result
    ├── config_manager.py    NL2SQLConfig（plugins.config.nl2sql 参数校验与点号路径读取）
    ├── hooks.py             场景钩子位契约（点位常量、SchemaContext/HookContext）与 load_scenario 加载器
    ├── prompt_manager.py    PromptTemplate（包内相对路径解析）
    ├── errors.py / constants.py
    ├── nodes/               BaseNL2SQLNode 基类、原六个节点及可选 bird-validator
    ├── security/            SQLGlot 安全规则与检查
    ├── utils/               解析、SQL 服务（受限取数）
    ├── examples/sql_rules/  场景示例 SQL 规则（bird、bio_lab，默认不启用）
    └── prompts/             节点提示词模板（.md）
```

## 启用与调用

nl2sql 随 DataAgent 安装在 `dataagent/plugins/nl2sql/`。在配置中启用插件。

1. Agent 配置 `plugins.enabled` 加入 `nl2sql`；
2. 业务参数写入同一配置的 `plugins.config.nl2sql`（见下）；
3. 宿主代理通过 `task` 工具把数据问题委派给子代理 `nl2sql`，子代理
   返回一条 JSON `AIMessage`（`sql`、`confidence`、`columns`、`rows_preview`、
   `row_count`、`total_known`、`truncated`、`issues`、`error`，
   预览不完整时另有 `rows_note`；写了结果文件时另有 `query_id`、`result_file`）。
   `rows_note` 只在预览不是完整结果时出现（取数被截断，或预览行数少于
   `row_count`），说明其余行在哪里，避免为看到更多行而重复派发。
4. 子代理描述末尾带有已绑定数据库摘要（`db_id`、`dialect`、`engine`，不含连接参数），
   主代理据此知道查的是哪个库；连接是否正常只由实际查询结果说明。

### 作为 Agent 入口

设置 `dataagent.entry` 为 `nl2sql` 时，不再经主代理的 `task` 工具委派。宿主仍只挂 `build_subagent` 返回的 Runnable，不调用 `build_nl2sql_graph`。用户消息直接进入这个 Runnable，回复仍是一条 JSON `AIMessage`。结果文件仍写到当前会话的 `outputs/nl2sql/`（宿主把 `outputs_dir` 传给工厂）。

```yaml
dataagent:
  entry: nl2sql
plugins:
  enabled:
  - nl2sql
  config:
    nl2sql:
      model: primary
      database:
        db_id: mydb
        dialect: sqlite
        engine: sqlite
        path: /data/app.db
```

可复制的产品配置是 [examples/nl2sql-entry.yaml](../../../examples/nl2sql-entry.yaml)。这种模式下没有主代理可读 `prompts/routing.md` 或子代理描述，这些内容被忽略。`dataagent.hooks`、调用上限和 `workspaces` 留在配置里，不装进这张图。请用 `ainvoke` / `astream`；`run_task` 是 async，同步 `invoke` 可能失败。模型的 token 和推理按模型本身的流式设置发出，最终回复也在消息快照里。不要把 Deep Agents 根上的线程恢复到这张图。改 `entry` 要重启。

| 旧版 | 现在 |
| --- | --- |
| `AGENT_CONFIG.type: "nl2sql"` | `dataagent.entry: "nl2sql"`，且 `plugins.enabled` 包含 `nl2sql` |
| 独立 YAML 中的模型、库和节点参数 | 根级 `models`、`semantic`，以及 `plugins.config.nl2sql` |
| 主代理把问题派给 nl2sql | 入口图没有 Deep Agents 根；用户消息就是问题 |

入口模式的其余限制见 [子代理](../../../docs/extensions/subagents.md#入口模式)。

### 主代理提示放在哪里

主代理从三处读到 nl2sql 的说明。每条规则只写在一处：

| 位置 | 何时可见 | 内容 |
| --- | --- | --- |
| `subagents/nl2sql.description.md` + 数据库摘要 | `task` 工具描述，常驻 | 能力边界（只取数）、一个 task 一条 SQL、task 怎么写、返回什么、`result_file` 的用途、已绑定哪个库 |
| 回复里的 `rows_note` | 只在预览不完整时 | 缺多少行、其余行在哪里 |
| `prompts/routing.md` | 根系统提示，常驻 | 约束主代理的其他工具：不搜文件、不用 shell 查已配置的库 |

`description` 写通用契约（能力、输入、输出），不写针对单次失败的禁令；
只在某个结果出现时才需要的说明放进回复。`routing.md` 只放这两处管不到的规则。
这三处都写英文，与主代理系统提示和节点提示词保持一致。

## 会话结果文件

查询成功后，子代理把选中候选的已取行写入当前会话输出目录。原链路受 `executor.limit` 约束，BIRD 链路取完整结果用于投票。

```
<session>/outputs/nl2sql/
├── <query_id>.csv         表头 + 全部已取行（UTF-8，NULL 写为空串）
└── <query_id>.meta.json   query_id、question、sql、db_id/dialect/engine、
                           executed_at、columns、row_count、total_known、truncated
```

- 回复里的 `result_file` 是 CSV 的绝对路径。主代理在后续轮次对“刚刚的数据”
  做计算时读取它，不抄对话里的行，也不重新查询；文件是执行当时的快照。
- 输出目录由宿主经 `services.outputs_dir` 传入；没有会话（`outputs_dir` 为空）
  或 `result_files: false` 时不写文件，回复不含这两个字段。
- 执行失败（无结果列）不写文件。写入失败不影响查询结果，回复带 `result_file_error`。
- 文件随会话目录保留和删除；meta 不含连接参数，但 CSV 含查询到的业务数据。
  可用 `result_files: false` 关闭文件输出。原链路还可用 `executor.limit` 限制取行数。

## 配置

插件参数写在宿主配置（如 schema_version 4 YAML）的 `plugins.config.nl2sql` 段。`model` 必须是同配置 `models` 里的别名。
语义层服务在根级 `semantic` 段配置（由宿主创建客户端并注入，生命周期
归宿主管理），不属于本插件参数：

```yaml
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
semantic:
  base_url: http://semantic-service
  username: …
  password: …
  timeout_seconds: 30.0
plugins:
  enabled:
  - nl2sql
  config:
    nl2sql:
      model: primary
      model_call_budget: 64
      database:
        db_id: mydb
        dialect: sqlite
        engine: sqlite
        path: /data/app.db
```

| 参数 | 说明 |
| --- | --- |
| `model` | 必填。`models` 别名，由宿主解析并注入所有节点 |
| `model_call_budget` | 单次任务内模型调用上限（默认 64），超出即报错返回 |
| `result_files` | 是否为每次成功查询写会话结果文件（默认 `true`，见上文） |
| `scenario` | 可选。场景包点名（点分名一段一级目录：`udn.business_twin` → `scenarios/udn/business_twin/`，目录须含 `scenario.py` 入口）；缺省走默认流程，配置后选表与生成后改写由场景包接管，见 `docs/udn-scenario-packs.md` |
| `database.db_id` | 语义服务中的数据库标识，需与实际查询库对应 |
| `database.dialect` | 必填。生成与 SQLGlot 校验使用的 SQL 方言，例如 `sqlite`、`mysql`、`postgres`；GaussVector 使用 `postgres` |
| `database.engine` | 执行引擎：`sqlite`、`mysql`、`gaussvector`、`hive`/`spark`；省略时取 dialect，二者不同时必须显式填写，例如 GaussVector |
| `database.*`（其余键） | 引擎连接参数，如 sqlite 的 `path`、mysql 的 `host/port/user/password/database` |
| `perceptor.schema_mode` | 可选 `full_schema`、`schema_linking` 或 `bird`。BIRD 按当前 `database.db_id` 从语义服务检索表 |
| `perceptor.user_schema` | schema .md 文件路径，注入 schema 绕过语义服务（测试后门） |
| `perceptor.user_sql_rules` | SQL 规则文件名；内置示例见 `nl2sql/examples/sql_rules/`（`sql_rules_bird` 等，默认不启用） |
| `generator.strategies` | `prompt`、`icl`、`skeleton`、`dc`；候选数 = num_workers × (num_samples×prompt + icl + skeleton + dc) |
| `emit_reasoning_tag` | 显式设置时统一控制 Generator 和 BIRD 修复 prompt 是否要求输出 `<reasoning>`。省略时 BIRD 根据模型的 thinking 配置选择 metavisor 模板 |
| `generator.parse_retries` | 每个生成任务遇到回复截断或 SQL 解析失败后的额外重试次数，默认 3；设为 0 时只请求一次 |
| `generator.num_workers` | 每策略并行度，**非常昂贵** |
| `validator.sql_security_enabled` | SQLGlot 确定性安全检查，默认关；开启需启用 reflector |
| `validator.db_explain` | 用 `EXPLAIN` 校验 SQL 模式与语法 |
| `validator.semantic_check` | 原 Validator 的 LLM 语义检查，默认 `true`。设为 `false` 时跳过此项，其他已启用的检查照常执行。BIRD 链使用独立的 `bird-validator.checkers` 配置 |
| `reflector.threshold` / `ref_retries` | 反思置信度阈值 / 生成-校验-反思重试次数 |
| `executor.limit` / `preview_limit` | 取行上限，也是结果文件的行数上限（-1 全量；引擎最多取 limit+1 行以判定截断）/ 预览行数（Selector 评分、日志与回复的 `rows_preview` 共用） |
| `selector.shortcut` / `threshold` / `sel_retries` | 多数票捷径（-1 关）/ 置信度阈值 / 重试次数 |

### 选择工作流

`plugins.config.nl2sql.nodes` 可显式选择以下链路。原六节点流程也支持截断为至少包含 Perceptor、Generator 的前缀。省略 `nodes` 时沿用原规则，根据已配置的节点段推断前缀；所有节点段均省略则运行完整六节点链。

```yaml
# 原流程，保留原有的校验、执行和重试路由
nodes: [perceptor, generator, validator, reflector, executor, selector]
```

```yaml
# BIRD 流程，以下字段均位于 plugins.config.nl2sql 下
nodes: [perceptor, generator, bird-validator, selector]
perceptor:
  schema_mode: bird
generator:
  strategies: [dc, skeleton, icl]
selector:
  shortcut: 1
```

完整宿主配置见 [BIRD 工作流配置示例](../../../examples/nl2sql-bird-workflow.yaml)。`schema_mode: bird` 使用根级 `semantic` 客户端。启用 `icl` 策略时，Perceptor 按题召回一次 few-shot 示例，写入 `state["few_shot_examples"]`，供 Generator 的所有 ICL worker 共用。两条流程均可使用这些 Perceptor 和 Generator 配置。

#### 选择 BIRD checker 及执行顺序

`nl2sql/nodes/checkers/` 下的 BIRD 校验实现使用 metavisor `BIRD_schema_link` 分支、提交 `6c0dcf4` 的八个 v2 checker。在 `plugins.config.nl2sql.bird-validator.checkers` 中按执行顺序列出要启用的 checker。省略此字段时，每条候选 SQL 默认只运行 `syntax → result`。可在列表中按需加入 `join`、`order_by_limit`、`time`、`select`、`max_min`、`order_by_null`。列表只接受这八个名称，每项最多出现一次；未知或重复名称会在加载配置时报错。

例如，下面的配置先修复语法，再让 `select` 先于 `join` 检查，最后检查执行结果。其余四项跳过。

```yaml
# 位于 plugins.config.nl2sql 下
bird-validator:
  checkers: [syntax, select, join, result]
```

设置 `checkers: []` 会跳过全部 checker。BIRD 节点仍执行最终 SQL，并把执行结果交给 Selector。每次执行前都会检查 SQL 是否为单条只读查询。

每个启用的 checker 检查上一步得到的 SQL。需要模型修复时，只把当前 checker 的建议或执行反馈随问题、schema 等上下文发给模型，按 metavisor 的 `<result>` XML 协议提取 SQL。两类提示都从 `database.dialect` 取目标方言，例如 GaussVector 配置 `postgres` 时要求返回 PostgreSQL SQL。`result` 遇到执行错误、超时、空结果或全 NULL 时请求三个候选，从成功且有非 NULL 值的结果中投票。

BIRD 节点把每条最终 SQL 的完整执行结果交给原 Selector，同一题内相同 SQL 复用执行结果。`selector.shortcut: 1` 指最高票组至少一票就直接选出结果；所有候选都执行失败时，沿用原 Selector 的模型评分。BIRD 流程在 Selector 结束，`selector.sel_retries` 使用默认值 0。

可选的 `bird-validator` 段支持 `temperature`（默认 0.3）、`repair_attempts`（默认 10，限制一次修复中整批 XML 都解析失败的次数）、`preview_limit`（限制展示行数）和 `sql_security_enabled`（启用插件的额外 SQL 安全策略）。生成和修复共享单题 `model_call_budget`。

### GaussVector 配置

在 `plugins.config.nl2sql.database` 中分别指定 SQL 方言和执行引擎：

```json
{
  "db_id": "your-semantic-db-id",
  "dialect": "postgres",
  "engine": "gaussvector",
  "host": "$env{GV_HOST}",
  "port": 5432,
  "user": "$env{GV_USER}",
  "password": "$env{GV_PASSWORD}",
  "database": "$env{GV_DATABASE}"
}
```

端口按实际服务填写，环境引用放在后端有效环境中。`db_id` 用于语义元数据查询，`database` 是数据库连接参数；模型配置仍需提供；使用语义服务获取元数据时，还需配置根级 `semantic`。

`dialect` 传给生成提示和 SQLGlot，`engine` 决定连接实现。不能把 dialect 写成 `gaussvector`，SQLGlot 不识别这个方言；也不能省略 engine 后让其变成不支持的 `postgres` 执行引擎。当前 GV 适配通过 `psycopg2` 执行 `EXPLAIN` 和查询，SQLite、MySQL、GV 所需驱动属于基础运行依赖，无需选择 extra；Spark/Hive 另需 `spark` extra 和 Java 环境。

这说明已有连接与执行适配，不代表特定 GV 版本、专有语法或真实业务库已经通过验收；这些仍需在目标环境验证。

## 与旧版的差异

- 接入方式：`nl2sql_run` 桥接工具 + 独立 YAML 配置，改为 compiled
  SubAgent——宿主编译时调用图工厂，参数来自 `plugins.config.nl2sql`，
  模型由宿主按别名解析注入（不再有插件内 `llm_manager`）。
- 语义层客户端由宿主统一创建与关闭（根配置 `semantic`，经
  `services.semantic_client` 注入）。
- 执行器受限取数：引擎最多取 `limit+1` 行，结果统一携带
  `row_count`（已取行数）、`total_known`（是否全量）、`truncated`（是否截断）。
- 观测（JSONL 轨迹记录、性能采集、上下文转储）与 cloud_core 引擎移除；
  场景 SQL 规则示例保留在 `nl2sql/examples/sql_rules/`。
- 场景变体以场景包方式回归：`business_twin`、`traffic_insight` 收编为
  `scenarios/udn/` 下的场景包，`scenario` 键一行挂载；基座只发布钩子位
  （`nl2sql/hooks.py`），不配置场景时行为不变。设计见 `docs/udn-scenario-packs.md`。
- checkpoint/resume 路径未移植（旧实现中为死代码：无中断点、未配置 checkpointer）。
