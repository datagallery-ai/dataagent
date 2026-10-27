# 特性设计：UDN 场景收编（business_twin / traffic_insight）

| 项 | 内容 |
| --- | --- |
| 状态 | 已实现（随本特性合入） |
| 范围 | `runtime/agentkit/builtin-plugins/nl2sql` |
| 来源 | `br_release_930` 分支 `dataagent/agents/nl2sql/` |
| 关联 | 入口模式（compiled 子代理直连对外，独立特性，与本特性正交：`entry` 决定 nl2sql 是否直连对外，`scenario` 决定 nl2sql 内部运行哪个场景） |
| 日期 | 2026-10-21（2026-10-24 修订） |

## 1. 背景

### 1.1 旧架构的场景变体

br_release_930 的 nl2sql Agent 通过 `DATABASE.perceptor_type` 字段支持场景变体，分叉共三处：

1. `agent.py:112-119` `_perceptor_class`：按字段选择感知节点类（`business_twin` / `traffic_insight` / 默认 `PerceptorNode`）；
2. `nodes/generator.py:117`、`nodes/reflector.py:87`：按同一字段在生成与修复后做维度 join 改写；
3. 一份场景 YAML（数据库指向、规则文件、语义层参数）。

其余节点（validator）与链路裁剪（两场景均不启用 executor/selector，纯 SQL 生成不执行）由配置表达。

### 1.2 新架构现状

nl2sql 以 compiled 子代理接入（`.plugin.json` → `subagents/nl2sql.json`），无任何场景机制：

- 参数校验 `_KNOWN_KEYS` 拒绝未知顶层键（`nl2sql/config_manager.py:36,57-59`）；
- 节点注册表 `_NODE_CLASSES` 硬编码六个基类节点（`nl2sql/graph.py:51`）；
- README"与旧版的差异"在特性合入前明确：场景变体（business_twin、traffic_insight、cloud_core 引擎）已移除。

宿主层扩展机制无法承载本需求：

- compiled 子代理禁止声明 system_prompt/tools/skills/hooks（`dataagent/declarations.py:190-194`），装配时跳过宿主全部包装（`dataagent/agent.py:118-121`）；
- middleware 是宿主内部机制，插件清单没有该槽位（`dataagent/extensions/compiler.py:76`）；
- 插件之间是代码孤岛，跨插件 import 不可行（`dataagent/extensions/loading.py:55-136`），独立场景插件无法复用 nl2sql 基座。

结论：**宿主给 nl2sql 的定制通道只有 `plugins.config.nl2sql`（params）与 `SubagentServices`（模型别名解析、语义客户端）两个口，"可插拔"必须设计在 nl2sql 插件内部。**

### 1.3 收编要求

1. 场景定制代码可插拔：挂载/移除一个场景不修改基座代码；
2. 场景逻辑与基座分离：基座零场景知识，只发布钩子位；
3. 语义服务就绪后，选表实现可直接切换为语义服务调用，业务代码与既有场景零改动；
4. 不配置场景时，现有行为完全不变。

## 2. 目标与非目标

### 目标

1. 在 nl2sql 插件内定义钩子位机制，场景以"场景包"目录形式挂载；
2. 收编 business_twin 与 traffic_insight 全部定制能力（选表、规则注入、维度 join 映射、行政区划、公式装配、字段召回与归一化）；
3. 两场景共用的知识与机制（维度 join、时间粒度规则）下沉在命名空间共用目录 `scenarios/udn/common/`，消除旧代码中的重复实现；
4. 复用现有能力：插件参数、模型别名、宿主语义客户端、节点前缀链、扩展热刷新。

### 非目标

- 不收编 cloud_core 执行引擎与旧观测设施（Context 轨迹记录、性能 JSONL、上下文转储）：两场景不执行 SQL；观测由宿主 tracing 体系（`extensions/tracing.py`、`trace_exporter.py`）承接。
- 不修改宿主核心（`dataagent/` 其余部分与 `client.py`）。唯一例外：
  `dataagent/semantic_client` 新增 `with_scenario()` 场景视图——共享宿主客户端
  的连接池与关闭语义，除 health 外的请求自动带 `X-Semantic-Scenario` 头；
  不配置场景时行为不变。这是场景头（旧 `SEMANTIC_LAYER.scenario`）的下发通道，见 §6。
- 不改默认场景行为（不场景包化，保持 `PerceptorNode` 现状）。

## 3. br_release_930 定制化代码梳理

梳理范围：`dataagent/agents/nl2sql/` 流程代码。方法：场景关键词全量检索 + 共享文件内分叉行定位。

### 3.1 business_twin 专属

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `nodes/business_twin_perceptor.py` | 474 | 选表流水线：表名点名直选（免 LLM）、LLM 列抽取、业务 ID 规则路由、bitmask 表簇解码、粒度推导、确定性 fallback、explicit-only 表剔除、行政区划规则注入、derived_metrics 公式进 DDL |
| `utils/business_twin_business_id_selector.py` | 233 | 确定性业务 ID 选择（21 schema 打分 + 业务偏好路由） |
| `utils/business_twin_business_id_catalog.py` + `.json` | 54 + 859 | 业务 ID 手册（21 个 schema、路由偏好表、忽略字段表），打包资源 |
| `utils/administrative_divisions.py` + `prompts/perceptor/china_administrative_division_aliases.json` | 157 + 61778 | 行政区划别名匹配（省/市/县 → 行政编码规则行） |
| `prompts/perceptor/filter_business_twin_business_id_{system,user}.md` | 212 + 5 | 列抽取提示词（内嵌 156 字段规范目录） |
| `prompts/perceptor/filter_business_twin_table_family_{system,user}.md` | 123 + 15 | 表簇 + 粒度选择提示词 |
| `prompts/user/sql_rules_business_twin.md` | 213 | 场景 SQL 规则手册 |
| `prompts/generator/business_twin_dimensions.json` | 80 | 15 个维度 → 维表映射 |

### 3.2 traffic_insight 专属

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `nodes/traffic_insight_perceptor.py` | 403 | 选表流水线：LLM 字段抽取、语义服务字段 EQ 召回（分页流式）、粒度后缀分簇、max need-hit / min-extra 收敛、选簇 + 粒度（无确定性 fallback）、hybrid 列补全 |
| `utils/traffic_insight_table_recall.py` | 688 | 召回 / 分簇 / 过滤 / 解析 / prompt 渲染全部纯函数 |
| `utils/traffic_insight_field_catalog.py` | 134 | 88 项字段目录（与 fields 提示词双份维护，测试锁定一致性） |
| `utils/traffic_insight_field_normalizer.py` | 86 | 字段归一化（时间伪字段剔除、目录外丢弃、维度/指标分类） |
| `prompts/perceptor/filter_traffic_insight_fields_{system,user}.md` | 158 + 5 | 字段抽取提示词（88 项目录 + 抽取原则） |
| `prompts/perceptor/filter_traffic_insight_table_family_{system,user}.md` | 105 + 15 | 选簇 + 粒度提示词 |
| `prompts/user/sql_rules_traffic_insight.md` | 207 | 场景 SQL 规则手册（含静态派生公式） |
| `prompts/generator/traffic_insight_dimensions.json` | 260 | 51 个维度 → 维表映射（与 business_twin 零交集） |

### 3.3 两场景共用

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 维度 join 机制函数群 | `utils/nl2sql_utils.py:35-38, 268, 287, 345, 395`（`_DIMENSION_CONFIG_FILES`、`load_dimension_mappings`、`selected_dimensions`、`_add_dimension_context`、`process_dimension_joins`） | 机制完全共用，仅映射文件按场景加载；generator 与 reflector 两个调用点共用 |
| 维度 join 提示词 | `prompts/generator/dimension_join_{system,user}.md`（12 + 10 行） | 两场景共用 |
| 时间粒度强制规则 | 两个场景 perceptor 内**逐字相同**的 `## 时间粒度` 注入逻辑 | 各自重复实现，收编时合并为 common 一份 |
| 当前日期注入 | 两个场景 perceptor 均追加 | 同上 |
| 六节点工作流与安全校验 | `nodes/` 其余节点、`security/` | 与默认场景共用，无场景分支 |

### 3.4 共享文件内的场景分叉段（收编时的改造点）

| 位置 | 内容 | 收编去向 |
| --- | --- | --- |
| `agent.py:112-119` | `_perceptor_class` 场景 → 节点类映射（分叉总开关） | 替换为场景包发现 / 加载机制 |
| `nodes/__init__.py:26,32` | 场景节点 import 导出 | 场景包自动发现替代 |
| `nodes/generator.py:117` | 按 `DATABASE.perceptor_type` 调维度 join | 钩子函数 |
| `nodes/reflector.py:87` | 同上 | 钩子函数 |
| `utils/nl2sql_utils.py:35-38` | 场景 → 映射文件注册表 | 场景包自带映射，注册表移除 |

### 3.5 场景 YAML

| 配置项 | business_twin.yaml | traffic_insight.yaml |
| --- | --- | --- |
| CORE 节点链 | perceptor / generator / validator / reflector（executor、selector 未启用） | 相同 |
| `CORE.perceptor.user_sql_rules` | `sql_rules_business_twin` | `sql_rules_traffic_insight` |
| `DATABASE.db_id` | `business_twin` | `yunhe` |
| `DATABASE.dialect` / `engine` | postgres / cloud_core | 相同 |
| `DATABASE.perceptor_type` | `business_twin` | `traffic_insight` |
| `SEMANTIC_LAYER.scenario` | `business_twin`（HTTP 头） | `traffic_insight`（HTTP 头） |
| `SEMANTIC_LAYER` 场景子段 | `business_twin.table_selection.mode: business_family` | `traffic_insight.recall: {column_eq_page_size: 200, column_eq_max_offset: 100000, hybrid_batch_size: 50, candidate_table_families_limit: 150, exclude_table_prefix: ["dim"]}` |
| MODEL | bailian qwen3-30b-a3b, temperature 0 | 相同 |

### 3.6 体量小结

场景专属流程代码约 2,229 行（business_twin 918 + traffic_insight 1,311），提示词 / 规则 / 映射约 1,600 行，行政区划数据 61,778 行，共享文件内分叉段约 100 行。场景定制集中在专属文件中，位于共享文件内的分叉仅约百行，收编主体是搬运与适配，不是重写。

## 4. 方案概述

```text
装配期（进程启动时执行一次）
  工厂 build_subagent 读配置 scenario: "udn.business_twin"
    ├─ load_scenario 按名路由到 scenarios/udn/business_twin/scenario.py，
    │   build_scenario(ctx) 返回 {钩子位名: 处理器}，装配期对照 HOOK_POINTS 校验
    └─ graph.py 按前缀链组装节点，hooks 注入各节点（UDN 启用前四节点）

运行期（每轮问题依次流过四个节点；未配置场景时各节点全走默认流程）
  用户问题
    → perceptor   get("perceptor.schema_context") 命中 → 委托场景包选表器
    │              （bt 目录路由 / ti 服务端召回），SchemaContext 写入状态并追加场景规则
    → generator   生成候选 SQL；get("generator.post_sql") 命中 → 调 common/dimension_join 改写
    → validator   SQLGlot 安全校验
    → reflector   修复失败 SQL；review_history 账本随修复轮累积并回传 fix 提示词（防回退、上报 unresolved），
    │              连续一轮无变化且无 need_ref 时提前接受，不再空烧 ref_retries；
    │              get("reflector.post_sql") 改写，结果回 validator 复核
    → SQL 输出（UDN 不执行 SQL，executor / selector 未启用）
```

场景包架构：nl2sql 基座一次性改造出**知识无关的接缝**（命名钩子位与守卫调用、按名路由的加载器、处理器工具箱）；场景名是 `scenarios/` 下的点分相对路径，一段一目录、深度不限——中间目录是命名空间，其 `common/` 沉淀跨场景共享的知识与机制，单个场景是自包含目录。配置一行挂载、删除目录即移除；删掉整个 `scenarios/` 目录，基座仍以默认场景完整可用。

## 5. 详细设计

### 5.1 场景包目录结构与放置规则

```text
builtin-plugins/nl2sql/
├── nl2sql/                          # 基座：零场景知识，只含接缝
│   ├── hooks.py                     # 新增：钩子位常量、SchemaContext / HookContext 契约、load_scenario 加载器
│   ├── config_manager.py            # 修改：放行 scenario 键 + scenario 属性（仅形状校验）
│   ├── graph.py                     # 修改：build_nl2sql_graph 增 hooks 参数，逐节点 bind_hooks
│   ├── nodes/perceptor.py           # 修改：查 perceptor.schema_context，命中即接管选表，extra_rules 注入规则
│   ├── nodes/generator.py、reflector.py  # 修改：查各自 post_sql 改写 SQL，失败保留原结果继续
│   │                                     #（reflector 另带 review_history 账本与无变化提前接受）
│   └── ...                          # 其余节点 / security / utils / prompts 与现状一致
├── scenarios/
│   └── udn/                         # 命名空间目录（可选）：共用作用域
│       ├── common/                  # 命名空间共用资产（非场景：无 scenario.py 入口）
│       │   ├── dimension_join.py    # 维度 join 机制函数群（自 nl2sql_utils.py:268-395 移植，签名改造）
│       │   ├── llm.py               # execute_with_llm_json：LLM JSON 输出解析（两场景共用）
│       │   ├── rules.py             # 时间粒度 / 当前日期 / 行政区划规则 helper 与文本
│       │   └── prompts/
│       │       ├── dimension_join_system.md   # 维度 join 改写提示词（两场景共用）
│       │       └── dimension_join_user.md
│       ├── business_twin/           # scenario: "udn.business_twin"
│       │   ├── scenario.py          # 入口：build_scenario() 装配（纯接线）
│       │   ├── table_selector.py     # 选表流水线（自 business_twin_perceptor.py 移植；装配期载资产，运行期 select(question) → SchemaContext）
│       │   ├── dimensions.json      # 15 维映射
│       │   ├── sql_rules.md         # 场景规则手册
│       │   ├── prompts/             # 感知阶段提示词（action 命名，场景包 __file__ 自解析 + from_file 直读）
│       │   │   ├── filter_business_twin_business_id_system.md  # 列抽取：内嵌 156 字段规范目录
│       │   │   ├── filter_business_twin_business_id_user.md    # 列抽取用户提示词
│       │   │   ├── filter_business_twin_table_family_system.md # 表簇 + 粒度选择（含硬约束）
│       │   │   └── filter_business_twin_table_family_user.md   # 表簇 + 粒度选择用户提示词
│       │   └── data/                # 场景数据与纯函数模块
│       │       ├── business_twin_business_id_catalog.py        # 业务 ID 手册加载校验
│       │       ├── business_twin_business_id_catalog.json      # 21 个业务 ID 手册（859 行）
│       │       ├── business_twin_business_id_selector.py       # 确定性业务 ID 选择（打分 + 偏好路由）
│       │       ├── business_twin_dimension_metadata.py         # dimension_code 位 → 字段/名称/描述（26 项）
│       │       ├── administrative_divisions.py                 # 行政区划别名匹配
│       │       └── china_administrative_division_aliases.json  # 行政区划别名索引（约 6.2 万行）
│       └── traffic_insight/         # scenario: "udn.traffic_insight"
│           ├── scenario.py          # 入口：build_scenario() 装配（纯接线）
│           ├── table_selector.py     # 选表流水线（自 traffic_insight_perceptor.py 移植，结构同 business_twin）
│           ├── dimensions.json      # 51 维映射
│           ├── sql_rules.md         # 场景规则手册（含静态派生公式）
│           ├── prompts/             # 感知阶段提示词（action 命名，场景包 __file__ 自解析 + from_file 直读）
│           │   ├── filter_traffic_insight_fields_system.md       # 字段抽取：88 项目录 + 抽取原则
│           │   ├── filter_traffic_insight_fields_user.md         # 字段抽取用户提示词
│           │   ├── filter_traffic_insight_table_family_system.md # 选簇 + 粒度（三类维度信号）
│           │   └── filter_traffic_insight_table_family_user.md   # 选簇 + 粒度用户提示词
│           └── data/                # 场景数据与纯函数模块
│               ├── traffic_insight_field_catalog.py     # 88 项字段目录（与 fields 提示词同步维护）
│               ├── traffic_insight_field_normalizer.py  # 字段归一化（伪字段剔除、维度 / 指标分类）
│               └── traffic_insight_table_recall.py      # 召回 / 分簇 / 收敛 / 解析纯函数（688 行）
└── subagents/nl2sql.py              # 工厂：读 scenario → load_scenario → hooks 注入组装
```

场景包放插件根 `scenarios/`（与 `nl2sql/` 包平级）。场景包与钩子位的单元测试在
`runtime/agentkit/tests/nl2sql_scenarios/`（183 项：两场景选表流水线、召回 / 路由
纯函数、加载器校验矩阵、三节点失败契约、端到端冒烟）。

**场景与目录的判定规则**：目录是不是场景，看它有没有 `scenario.py` 入口，与命名无关——`common/` 没有该文件，永远不会被当作场景（误配 `scenario: "udn.common"` 走装配期"场景包缺入口"报错）

**深度不限**：点分名的一段对应一级目录，一段即可成场景——单场景项目直接放 `scenarios/<场景>/scenario.py`（如 `scenario: "traffic"`），无需为凑层级造目录；多级嵌套（`scenario: "a.b.c"`）同理合法。中间目录无论几级都是命名空间，可放共用资产——约定名 `common/`，放在共用它的各场景的公共祖先目录；命名空间目录自身若含 `scenario.py` 也是场景，名字与目录一一映射，机制上无冲突。

**放置阶梯**：

| 层 | 放什么 |
| --- | --- |
| `nl2sql/` 基座 | 机制、无场景知识 |
| `scenarios/<命名空间>/common/`（可选、可嵌套） | 命名空间内共用的机制 |
| `scenarios/…/<场景>/` | 单场景资产 |

### 5.2 钩子位机制（`nl2sql/hooks.py`）

基座对场景的全部定制面收敛为**命名钩子位**：节点在固定位置按名取用处理器，取不到就走默认流程；场景包对基座的全部产出是一张「点名 → 处理器」映射。两个场景的感知输出本就同构，收敛为返回契约 `SchemaContext`（定义见下）

```python
# nl2sql/hooks.py —— 契约层：基座发布的全部钩子位
PERCEPTOR_SCHEMA_CONTEXT = "perceptor.schema_context"
GENERATOR_POST_SQL = "generator.post_sql"
REFLECTOR_POST_SQL = "reflector.post_sql"

HOOK_POINTS = frozenset({PERCEPTOR_SCHEMA_CONTEXT, GENERATOR_POST_SQL, REFLECTOR_POST_SQL})
# 扩位 = 基座变更；场景包注册未知位在装配期报错并列出合法位。
```

三个位置的签名（结构类型——处理器无需 import 基座即可满足）为**闭包式**：装配期 `build_scenario(ctx)` 已捕获全部上下文，运行期不再重复接收 `ctx`：

| 钩子位 | 签名 | 语义 |
| --- | --- | --- |
| `perceptor.schema_context` | `async (question) -> SchemaContext` | **替换**默认选表（full_schema / schema_linking）；用户显式提供的 schema 仍优先 |
| `generator.post_sql` | `async (results, state) -> list[Result]` | 生成后、首个候选被选中前**改写**候选 SQL（question / schema 从 `state` 取） |
| `reflector.post_sql` | `async (results, state) -> list[Result]` | 修复后、回 validator 复核前改写；`state` 为可变输入（维度 join 注入维表 DDL 供 validator 复核） |

`SchemaContext`（`perceptor.schema_context` 的返回契约，定义在 hooks.py）：`schema`、`joins`、`schema_str`（处理器自制 DDL——business_twin 要把 derived_metrics 公式写进 DDL，不由基座派生）、`extra_rules`（场景规则唯一注入口：`sql_rules.md`、时间粒度、当前日期、行政区划均经此追加进 `sql_rules`）。

失败契约由**节点持有**，处理器可放心 raise：`generator.post_sql` 抛错 → 保留原候选、记 warning 继续；`reflector.post_sql` 抛错 → 保留修复后 SQL、记 warning，按无钩子流程继续（单候选改写失败仍由处理器内部置 `need_ref` 进下一轮反思，即维度 join 既有契约）；`perceptor.schema_context` 抛错 → 不兜底直接上抛（选表失败没有合理默认行为，fallback 层级属于场景选表逻辑自身）。**部分注册合法**：返回映射可只含点位子集，缺席点位各自走默认流程（"取不到走默认"逐点位生效）。

场景包侧的全部约定。`HookContext`（装配期构造、frozen）是场景包可见的全部宿主能力，字段按**宿主能力**塑形而非场景需求：

| 字段 | 内容 |
| --- | --- |
| `scenario` | 场景点名（如 `udn.business_twin`） |
| `model` | 宿主解析的模型（`_BudgetedModel` 同一实例：场景 LLM 调用自动计入 `model_call_budget`） |
| `semantic_client` | 宿主语义客户端；根级 `semantic` 未配置为 `None`（选表器首次使用时自行报配置错） |
| `config` | `NL2SQLConfig`（dotted 访问 `database.db_id` / `database.dialect` 等） |

演进规则：场景特有数据 → 场景包自带（常量或构造器默认值，改一处场景包代码，基座零改动）；新宿主能力 → 加字段（基座两行加法）。处理器是结构类型、场景包与基座同仓同编译，加字段不破坏既有场景。

```python
# scenarios/udn/business_twin/scenario.py —— 纯接线：一个常量、两个构造、三处点位
SEMANTIC_SCENARIO = "business_twin"        # 模块常量：X-Semantic-Scenario 值

def build_scenario(ctx: HookContext) -> dict[str, Handler]:
    base = Path(__file__).resolve().parent
    rewrite = dimension_join_rewriter(     # 命名空间 common/ 装配
        mappings=load_dimensions(base / "dimensions.json", SEMANTIC_SCENARIO),
        model=ctx.model, dialect=ctx.config.get("database.dialect", "sqlite"))
    semantic = ctx.semantic_client.with_scenario(SEMANTIC_SCENARIO) if ctx.semantic_client else None
    selector = BusinessTwinTableSelector(  # 重资产装配期加载（手册 / 行政区划 / 提示词）
        ctx, base=base, semantic_client=semantic)  # 场景视图：选表查询自动带场景头
    return {
        PERCEPTOR_SCHEMA_CONTEXT: selector.select,  # (question) -> SchemaContext
        GENERATOR_POST_SQL: rewrite,                # (results, state) -> list[Result]
        REFLECTOR_POST_SQL: rewrite,
    }
```

### 5.3 配置

这份 JSON 不是独立文件：`plugins` 段属于宿主 **Agent 配置文件**——`$DATAAGENT_HOME/config.json`（默认 `~/.dataagent/config.json`；`init_home` 从模板生成，启动可显式指定替代文件），仓库示例见 `examples/config.json`
（默认模式）与 `examples/config-udn.json`（本特性：UDN business_twin 场景的完整
Agent 配置）。`nl2sql` 及以下全部内容进入该文件的 `plugins.config.nl2sql` 段（`model` 别名来自同级 `models` 段，语义服务连接在根级 `semantic` 段、由宿主建客户端注入，均非插件参数）：

```json
{
  "plugins": {
    "enabled": ["nl2sql"],
    "config": {
      "nl2sql": {
        "model": "primary",
        "scenario": "udn.business_twin",
        "database": { "db_id": "business_twin", "dialect": "postgres" },
        "perceptor": {},
        "generator": { "strategies": ["prompt"] },
        "validator": { "sql_security_enabled": true },
        "reflector": { "threshold": 0.9 }
      }
    }
  }
}
```

| 规则 | 说明 |
| --- | --- |
| `scenario` 缺省 | 默认模式，走现有 `PerceptorNode`（full_schema / schema_linking），行为不变 |
| 取值 | 点分名，一段对应一级目录（`udn.business_twin` → `scenarios/udn/business_twin/`）；每段为标识符、一段及以上即合法，单段场景不设命名空间（`traffic` → `scenarios/traffic/`）；存在性 = 目录含 `scenario.py` 入口，装配时校验 |
| 场景参数 | 不经配置：场景自带数据（选表旋钮、模式）为场景包常量或构造器默认值，改场景包一处代码即调整 |
| 节点段 | 复用现有逐节点段机制（`perceptor`…`selector`，出现时须构成前缀链）；两场景均启用四节点（无 executor / selector，纯生成不执行）。`validator.sql_security_enabled` 须显式开启——新基座默认关、旧版默认开；`generator.strategies` / `reflector.threshold` 的默认值已与旧 YAML 一致，显式写出仅为自述 |

### 5.4 扩展规则

新场景 = `scenarios/` 下新增目录 + 配置一段参数。钩子位的本质是"位置"而非"功能"，只有当现有钩子位够不着新切点时才动基座。

## 6. 旧配置迁移对照

| 旧（场景 YAML） | 新（`plugins.config.nl2sql`） |
| --- | --- |
| `DATABASE.perceptor_type` | `scenario`（点分名，如 `udn.business_twin`） |
| `CORE.perceptor.user_sql_rules` | 场景包自带 `sql_rules.md`（无需配置） |
| `CORE` 节点键序列 | 逐节点段前缀链（现有能力） |
| `DATABASE.{db_id, dialect, engine}` | `database.{db_id, dialect, engine}` |
| `DATABASE.config.*` | `database.*` 平铺（现有能力） |
| `SEMANTIC_LAYER.scenario` | 场景包模块常量 `SEMANTIC_SCENARIO` 声明；`build_scenario` 经宿主 `with_scenario()` 场景视图下发请求头（health 豁免） |
| `SEMANTIC_LAYER.business_twin.table_selection` | 场景包自带（唯一合法值即默认值，无需配置） |
| `SEMANTIC_LAYER.traffic_insight.recall` | 场景包构造器默认值（`table_selector.py`，测试可覆写） |

## 7. 语义服务替换路径

收编要求 3 的兑现方式：选表是钩子位背后的**策略位**，替换 = 新增实现，不是修改。

### 7.1 介入位置

语义服务接管 `perceptor.schema_context` 处理器的内部实现：本地"LLM 抽取 + 本地算法 + 多次语义查询"的编排，替换为一次选表接口调用，返回内容与 `SchemaContext` 字段一一对应：

```text
现在：处理器(question) → 2 次 LLM 调用（场景包 prompts/）
                       + 本地算法（场景包 data/）
                       + 语义查询（表目录 / 列信息）→ SchemaContext

将来：处理器(question) → POST /api/semantic/v1/table-selection
                       {question, 场景头} → {schema, schema_str, joins, extra_rules} → SchemaContext
```

### 7.2 接管边界

| 语义服务接管（下沉） | 保留在 agent 侧 |
| --- | --- |
| business_twin 业务 ID 路由手册知识（859 行 JSON） | 维度 join 改写（命名空间共用机制 `scenarios/udn/common/` + 场景包 `dimensions.json`，发生在 SQL 生成之后） |
| bitmask 表簇结构与解码 | `sql_rules` 场景规则手册（面向生成器的提示词工程） |
| traffic_insight 字段召回 / 分簇 / 收敛算法 | 时间粒度强规则（命名空间共用 rules helper，经 `SchemaContext.extra_rules` 注入） |
| 选表与粒度判断、schema 组装 | 错误处理、模型预算、输出协议（基座职责） |

### 7.3 目录演进

语义选表器不含业务知识（知识已在服务端），是通用能力，放**基座**；场景身份（维度映射、规则、场景头）留在**场景包**。两个场景的维度映射与规则不同，不合并为独立的"semantic 场景包"。

过渡期（本地与服务端实现共存，场景包常量切换、可回退）：

```text
nl2sql/                              # 基座
├── hooks.py                         # 不变（钩子位即为替换边界）
└── table_selector.py                # 新增：通用语义服务选表器（一次调用 → SchemaContext）

scenarios/udn/
├── common/                          # 跨切换存活：语义服务不接管维度 join 与规则注入
│   ├── dimension_join.py            # 不变
│   └── rules.py                     # 不变
├── business_twin/
│   ├── scenario.py                  # 修改：按模块常量 TABLE_SELECTOR 选 local / semantic
│   │                                #   （默认 local，即场景包 table_selector.py；semantic 即基座同名模块，经 common 包装器注入 extra_rules）；
│   │                                #   post_sql 处理器仍由 common/dimension_join 装配
│   ├── table_selector.py            # 保留：本地流水线，服务端故障时回退
│   ├── dimensions.json / sql_rules.md   # 保留：维度映射与规则仍按场景
│   ├── prompts/                     # 保留（本地实现使用）
│   └── data/                        # 保留（本地实现使用；知识已镜像到服务端）
└── traffic_insight/                 # 同构
```

切换即场景包改一个常量：

```python
# scenarios/udn/business_twin/scenario.py
TABLE_SELECTOR = "semantic"   # "local"：场景包本地流水线 | "semantic"：基座语义选表器
```

最终态（服务端稳定后本地实现退役）：场景包瘦身为三个文件——`scenario.py`（声明基座语义选表器、经 common 包装器注入规则、装配 `post_sql` 处理器与场景头）、`dimensions.json`、`sql_rules.md`；`table_selector.py`、`prompts/`、`data/` 退役，其承载的知识与算法已在语义服务端。`common/` 作为命名空间基础设施跨切换存活。
