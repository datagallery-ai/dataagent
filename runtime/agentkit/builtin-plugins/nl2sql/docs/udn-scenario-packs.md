# UDN 场景包（business_twin / traffic_insight）

| 项 | 内容 |
| --- | --- |
| 状态 | 已实现 |
| 范围 | `runtime/agentkit/builtin-plugins/nl2sql` |
| 关联 | 入口模式（compiled 子代理直连对外，独立特性，与本特性正交：`entry` 决定 nl2sql 是否直连对外，`scenario` 决定 nl2sql 内部运行哪个场景） |
| 日期 | 2026-11-11 |

## 1. 概述

UDN 场景是 nl2sql 插件内置的两个场景包：`business_twin`（业务孪生）与 `traffic_insight`（流量洞察）。两场景面向 UDN 数据库做**纯 SQL 生成**（不执行 SQL），按各自的表结构、维度体系与业务规则定制选表与生成行为。

场景包是 nl2sql 插件内部的挂载机制：

1. 基座（`nl2sql/`）零场景知识，只发布命名钩子位与按名路由的加载器；
2. 场景是 `scenarios/` 下的自包含目录，配置一行挂载、删除目录即移除；删掉整个 `scenarios/` 目录，基座仍以默认场景完整可用；
3. 两场景共用的知识与机制（维度 join、LLM JSON 解析、时间粒度与当前日期规则）在命名空间共用目录 `scenarios/udn/common/`；
4. 不配置 `scenario` 时走默认 `PerceptorNode`（full_schema / schema_linking），现有行为不变。

边界：UDN 场景不启用 executor / selector（纯生成不执行）；不包含 cloud_core 执行引擎与旧观测设施（观测由宿主 tracing 体系承接）。宿主侧唯一配套是 `dataagent/semantic_client.with_scenario()` 场景视图——共享宿主客户端的连接池与关闭语义，除 health 外的请求自动带 `X-Semantic-Scenario` 头；不配置场景时行为不变。

一轮问题的运行流：

```text
用户问题
  → perceptor   get("perceptor.schema_context") 命中 → 委托场景包选表器
  │              （bt 目录路由 / ti 服务端召回），SchemaContext 写入状态并追加场景规则
  → generator   生成候选 SQL；get("generator.post_sql") 命中 → 调 common/dimension_join 改写
  → validator   SQLGlot 语法校验 + 安全校验 + LLM 语义校验（review_history 跨轮反馈）
  → reflector   修复失败 SQL；review_history 账本随修复轮累积并回传 fix 提示词（防回退、上报 unresolved），
  │              连续一轮无变化且无 need_ref 时提前接受；get("reflector.post_sql") 改写，结果回 validator 复核
  → SQL 输出（不执行）
```

## 2. 场景包目录

```text
builtin-plugins/nl2sql/
├── nl2sql/                          # 基座：零场景知识，只含接缝
│   ├── hooks.py                     # 钩子位常量、SchemaContext / HookContext 契约、load_scenario 加载器
│   ├── config_manager.py            # 放行 scenario 键 + scenario 属性（仅形状校验）
│   ├── graph.py                     # build_nl2sql_graph 增 hooks 参数，逐节点 bind_hooks
│   ├── nodes/perceptor.py           # 查 perceptor.schema_context，命中即接管选表，extra_rules 注入规则
│   ├── nodes/generator.py、reflector.py  # 查各自 post_sql 改写 SQL，失败保留原结果继续
│   │                                     #（reflector 另带 review_history 账本与无变化提前接受）
│   └── ...                          # 其余节点 / security / utils / prompts
├── scenarios/
│   └── udn/                         # 命名空间目录（可选）：共用作用域
│       ├── common/                  # 命名空间共用资产（非场景：无 scenario.py 入口）
│       │   ├── dimension_join.py    # 维度 join 机制：SQLGlot 确定性改写 + 后置条件校验 + LLM 兜底
│       │   ├── llm.py               # execute_with_llm / execute_with_llm_json：LLM 输出解析（两场景共用）
│       │   ├── rules.py             # 当前日期 / 时间粒度（strict / soft 双态）规则 helper
│       │   └── prompts/
│       │       ├── dimension_join_system.md   # 维度 join 改写提示词（两场景共用）
│       │       └── dimension_join_user.md
│       ├── business_twin/           # scenario: "udn.business_twin"
│       │   ├── scenario.py          # 入口：build_scenario() 装配（纯接线）
│       │   ├── table_selector.py    # 选表流水线（装配期载资产，运行期 select(question) → SchemaContext）
│       │   ├── dimensions.yaml      # 15 维映射
│       │   ├── sql_rules.md         # 场景规则手册
│       │   ├── prompts/             # 感知阶段提示词（场景包 __file__ 自解析 + from_file 直读）
│       │   │   ├── filter_business_twin_business_id_{system,user}.md  # 列抽取：内嵌 156 字段规范目录
│       │   │   └── filter_business_twin_table_family_{system,user}.md # 表簇 + 粒度选择（含硬约束）
│       │   └── data/                # 场景数据与纯函数模块
│       │       ├── business_twin_business_id_catalog.py / .json    # 业务 ID 手册（21 schema + 6 条业务规则）
│       │       ├── business_twin_business_id_selector.py           # 确定性业务 ID 选择（打分 + 偏好路由）
│       │       ├── business_twin_dimension_metadata.py             # dimension_code 位 → 字段/名称/描述（26 项）
│       │       ├── administrative_divisions.py                     # 行政区划别名匹配
│       │       └── china_administrative_division_aliases.json      # 行政区划别名索引（约 6.2 万行）
│       └── traffic_insight/         # scenario: "udn.traffic_insight"
│           ├── scenario.py          # 入口：build_scenario() 装配（纯接线）
│           ├── table_selector.py    # 选表流水线（结构同 business_twin）
│           ├── dimensions.yaml      # 51 维映射
│           ├── sql_rules.md         # 场景规则手册（含静态派生公式）
│           ├── prompts/
│           │   ├── filter_traffic_insight_fields_{system,user}.md        # 字段抽取：88 项目录 + 抽取原则
│           │   └── filter_traffic_insight_table_family_{system,user}.md # 选簇 + 粒度（三类维度信号）
│           └── data/
│               ├── traffic_insight_field_catalog.py     # 88 项字段目录（与 fields 提示词同步维护）
│               ├── traffic_insight_field_normalizer.py  # 字段归一化（伪字段剔除、维度 / 指标分类）
│               └── traffic_insight_table_recall.py      # 召回 / 分簇 / 收敛 / 解析纯函数
└── subagents/nl2sql.py              # 工厂：读 scenario → load_scenario → hooks 注入组装
```

场景包与钩子位的单元测试在 `runtime/agentkit/tests/nl2sql_scenarios/`（包含：两场景选表流水线、召回 / 路由纯函数、加载器校验矩阵、三节点失败契约、维度 join 改写与后置条件、跨轮反馈、端到端冒烟）。

**场景与目录的判定规则**：目录是不是场景，看它有没有 `scenario.py` 入口，与命名无关——`common/` 没有该文件，永远不会被当作场景（误配 `scenario: "udn.common"` 走装配期"场景包缺入口"报错）。

**深度不限**：点分名的一段对应一级目录，一段即可成场景——单场景项目直接放 `scenarios/<场景>/scenario.py`（如 `scenario: "traffic"`），无需为凑层级造目录；多级嵌套（`scenario: "a.b.c"`）同理合法。中间目录无论几级都是命名空间，可放共用资产——约定名 `common/`，放在共用它的各场景的公共祖先目录；命名空间目录自身若含 `scenario.py` 也是场景，名字与目录一一映射，机制上无冲突。

**放置阶梯**：

| 层 | 放什么 |
| --- | --- |
| `nl2sql/` 基座 | 机制、无场景知识 |
| `scenarios/<命名空间>/common/`（可选、可嵌套） | 命名空间内共用的机制 |
| `scenarios/…/<场景>/` | 单场景资产 |

## 3. 钩子位机制（`nl2sql/hooks.py`）

基座对场景的全部定制面收敛为**命名钩子位**：节点在固定位置按名取用处理器，取不到就走默认流程；场景包对基座的全部产出是一张「点名 → 处理器」映射。

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

`SchemaContext`（`perceptor.schema_context` 的返回契约）：`schema`、`joins`、`schema_str`（处理器自制 DDL——business_twin 要把 derived_metrics 公式写进 DDL，不由基座派生）、`extra_rules`（场景规则唯一注入口：`sql_rules.md`、时间粒度、当前日期、行政区划均经此追加进 `sql_rules`）。

失败契约由**节点持有**，处理器可放心 raise：`generator.post_sql` 抛错 → 保留原候选、记 warning 继续；`reflector.post_sql` 抛错 → 保留修复后 SQL、记 warning，按无钩子流程继续（单候选改写失败仍由处理器内部置 `need_ref` 进下一轮反思，即维度 join 既有契约）；`perceptor.schema_context` 抛错 → 不兜底直接上抛（选表失败没有合理默认行为，fallback 层级属于场景选表逻辑自身）。**部分注册合法**：返回映射可只含点位子集，缺席点位各自走默认流程（"取不到走默认"逐点位生效）。

`HookContext`（装配期构造、frozen）是场景包可见的全部宿主能力，字段按**宿主能力**塑形而非场景需求：

| 字段 | 内容 |
| --- | --- |
| `scenario` | 场景点名（如 `udn.business_twin`） |
| `model` | 宿主解析的模型（`_BudgetedModel` 同一实例：场景 LLM 调用自动计入 `model_call_budget`） |
| `semantic_client` | 宿主语义客户端；根级 `semantic` 未配置为 `None`（选表器首次使用时自行报配置错） |
| `config` | `NL2SQLConfig`（dotted 访问 `database.db_id` / `database.dialect` 等） |

演进规则：场景特有数据 → 场景包自带（常量或构造器默认值，改一处场景包代码，基座零改动）；新宿主能力 → `HookContext` 加字段（基座两行加法）。处理器是结构类型、场景包与基座同仓同编译，加字段不破坏既有场景。

```python
# scenarios/udn/business_twin/scenario.py —— 纯接线：一个常量、两个构造、三处点位
SEMANTIC_SCENARIO = "business_twin"        # 模块常量：X-Semantic-Scenario 值

def build_scenario(ctx: HookContext) -> dict[str, Handler]:
    base = Path(__file__).resolve().parent
    rewrite = dimension_join_rewriter(     # 命名空间 common/ 装配
        mappings=load_dimensions(base / "dimensions.yaml", SEMANTIC_SCENARIO),
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

## 4. 功能现状

### 4.1 business_twin 选表

选表流水线（`table_selector.py`，重资产在装配期一次载入）：

- 表名点名直选（免 LLM）、LLM 列抽取（内嵌 156 字段规范目录）；
- 业务 ID 规则路由：21 个 business_schema 打分 + 6 条业务偏好路由，`business_twin_business_id_selector.py` 确定性实现（手册 `business_twin_business_id_catalog.json`）；
- bitmask 表簇解码、粒度推导、确定性 fallback、explicit-only 表剔除；
- 行政区划别名匹配（省/市/县 → 行政编码规则行，约 6.2 万行别名索引）；
- derived_metrics 公式写进 DDL；维度元数据 26 项（dimension_code 位 → 字段 / 名称 / 描述）。

### 4.2 traffic_insight 选表

- LLM 字段抽取（88 项目录 + 抽取原则）；
- 语义服务字段 EQ 召回（分页流式；构造器默认值 `column_eq_page_size=200`、`column_eq_max_offset=100000`、`hybrid_batch_size=50`、`candidate_table_families_limit=150`、`exclude_table_prefix=["dim"]`，测试可覆写）；
- 粒度后缀分簇（5min / 1h / 1d / 1w / 1m）、max need-hit / min-extra 收敛、选簇 + 粒度（无确定性 fallback）、hybrid 列补全；
- 字段归一化：时间伪字段剔除、目录外丢弃、维度 / 指标分类（`traffic_insight_field_normalizer.py`）。

### 4.3 维度 join 改写（`common/dimension_join.py`）

把映射到维表、且在最终 SELECT 投影（或仅 GROUP BY）中独立出现的维列改写为维表值查找（每映射一个 `INNER JOIN`），同步 ORDER BY / GROUP BY 等子句引用。两场景映射零交集：business_twin 15 维、traffic_insight 51 维。

改写是**确定性优先**：SQLGlot 重写器完成投影替换、子句同步与 JOIN 追加，随后一组**原子后置条件**验证结果（投影覆盖 GROUP BY、WHERE 不引维表值列、原投影保留等）；未通过或不安全的形状回落 LLM 改写，LLM 输出必须通过同一组后置条件才被接受。维表 DDL 与 join 规则注入运行状态，validator 复核改写后的 SQL。

失败契约：单候选改写失败置 `need_ref` 进下一轮反思；处理器整体抛错由节点持有（保留原结果继续，见 §3）。

### 4.4 规则注入（`common/rules.py` + 场景 `sql_rules.md`）

- `extra_rules` 是场景规则唯一注入口：`sql_rules.md`、当前日期（business_twin 附相对时间解析提示）、时间粒度契约均经此追加进 `sql_rules`；
- 时间粒度契约双态：business_twin **严格**（问题明确要求粒度时 `time` 必须出现在 SELECT 与 GROUP BY，统计 / 汇总措辞不压成单行，要求移除 `time` 的意见判无效）；traffic_insight **软性**（显式粒度主要用于选表，建议保留 `time` 分组）；
- 规则手册要点（两场景手册各自维护：business_twin 218 行 / traffic_insight 207 行）：半开时间窗、`EXTRACT(EPOCH FROM timestamptz)` 且以 `NOW()` 锚定（禁用 `CURRENT_DATE` 类日期锚点）、前 N 天（完整日历日）与最近 N 天（滚动窗）的区分、除法一律 `::numeric` 分子 + `NULLIF` 包裹分母、维度值映射、ASCII 别名强制；
- 提示词侧统一约定：场景规则以 *Additional Rules* 段进入 generator / reflector / validator 提示词，**问题显式指令优先级最高**（问题点名的写法压过规则与模型自行解读）；等价拼写（`CAST(x AS NUMERIC)` / `x::numeric`、`CURRENT_TIMESTAMP` / `NOW()`）不计为问题。

### 4.5 校验与跨轮反馈

- validator 三层：SQLGlot 语法校验；安全校验（`validator.sql_security_enabled` 开启时）；LLM 语义校验（逐候选评分 + issues）；
- 安全校验（`security/checker.py` + `security/rules.py`）：只读单语句、函数白名单、语法白名单（JOIN 类型 / 集合操作 / 谓词形状）、schema 名校验、资源限额（1 MiB 文本上限、布尔表达式深度等）；违规带 `SYNTAX-` / `FUNCTION-` / `SCHEMA-` / `SQL-` / `RESOURCE-` 编号报告，reflector 只修被点名的违规、与场景规则要求的时间形状冲突时上报 `unresolved`；校验通过时回写规范化 SQL；
- review_history 账本：reflector 逐轮累积修复与 unresolved 并回传 fix 提示词（防回退）；validator 侧以 *Prior Review Rounds* 感知历史，不重复上报已判 `unresolved` 的意见（除非本轮编辑可修）；
- 止损：评分低于 1 但无可修 issues 的候选直接放行（不空转反思）；连续一轮无变化且无 `need_ref` 时提前接受，不再空烧 `ref_retries`。

## 5. 配置

这份 YAML 不是独立文件：`plugins` 段属于宿主 **Agent 配置文件**——`$DATAAGENT_HOME/config.yaml`（默认 `~/.dataagent/config.yaml`；`init_home` 从模板生成，启动可显式指定替代文件）。完整可运行示例见 `examples/config-udn.yaml`（business_twin 场景，含模型别名、语义服务连接与入口声明）；`nl2sql` 及以下全部内容进入该文件的 `plugins.config.nl2sql` 段（`model` 别名来自同级 `models` 段，语义服务连接在根级 `semantic` 段、由宿主建客户端注入，均非插件参数）：

```yaml
plugins:
  enabled:
  - nl2sql
  config:
    nl2sql:
      model: primary
      scenario: udn.business_twin
      database:
        db_id: business_twin
        dialect: postgres
      perceptor: {}
      generator:
        strategies:
        - prompt
      validator:
        sql_security_enabled: true
      reflector:
        threshold: 0.9
```

| 规则 | 说明 |
| --- | --- |
| `scenario` 缺省 | 默认模式，走现有 `PerceptorNode`（full_schema / schema_linking），行为不变 |
| 取值 | 点分名，一段对应一级目录（`udn.business_twin` → `scenarios/udn/business_twin/`）；每段为标识符、一段及以上即合法，单段场景不设命名空间（`traffic` → `scenarios/traffic/`）；存在性 = 目录含 `scenario.py` 入口，装配时校验 |
| 场景参数 | 不经配置：场景自带数据（选表旋钮、模式）为场景包常量或构造器默认值，改场景包一处代码即调整 |
| 节点段 | 复用现有逐节点段机制（`perceptor`…`selector`，出现时须构成前缀链）；两场景均启用四节点（无 executor / selector，纯生成不执行）。`validator.sql_security_enabled` 须显式开启——基座默认关；`generator.strategies` / `reflector.threshold` 的默认值与场景需求一致，显式写出仅为自述 |

## 6. 环境配置与启动示例

### 6.1 前置条件

Linux 环境，需安装 uv 与 Node.js 22，Python 3.12 由 uv 在安装步骤中下载。

### 6.2 安装

在仓库根目录：

```bash
npm install
uv sync --project runtime/agentkit
```

### 6.3 环境变量

编辑 `$DATAAGENT_HOME/.env`（默认 `~/.dataagent/.env`，可用 `DATAAGENT_HOME` 改到其他目录）：

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
SEMANTIC_BASE_URL=http://your-semantic-service
```

### 6.4 启动

后端默认地址 `http://127.0.0.1:8790`（来自配置文件 `server.host` / `server.port`），Web 界面默认 `http://127.0.0.1:3000`（`--web-host` / `--web-port` 可覆盖）；`examples/config-udn.yaml` 已设 `dataagent.entry = "nl2sql"` 直连对外与 `scenario = "udn.business_twin"`。

#### 6.4.1 启动 GUI

```bash
npm run start -- --config runtime/agentkit/examples/config-udn.yaml
```

#### 6.4.2 启动 TUI

方式一：目前 TUI 不会自动加载根 `.env`，需要显式传给后端

```bash
npm run start:tui -- --config runtime/agentkit/examples/config-udn.yaml --env-file .env
```

方式二：如果已经启动了 GUI，TUI 探测到后端会直接附着，此时只需

```bash
npm run start:tui
```

### 6.5 切换到 traffic_insight

复制 `examples/config-udn.yaml` 后改两处：`plugins.config.nl2sql.scenario` → `"udn.traffic_insight"`，`database.db_id` → `"yunhe"`，再按 §6.4 启动。
