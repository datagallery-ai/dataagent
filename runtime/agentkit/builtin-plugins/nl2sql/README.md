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
├── .plugin.json             V2 插件清单（id=nl2sql，声明子代理与主代理路由提示）
├── prompts/routing.md       注入主代理系统提示：只能经 nl2sql 查已配置的库，不搜文件、不用 shell 查库
├── subagents/
│   ├── nl2sql.json          SubAgentSpec（kind=compiled，指向图工厂与描述文件）
│   ├── nl2sql.description.md 子代理描述（主代理在 task 工具中看到的契约）
│   └── nl2sql.py            build_subagent 图工厂：参数/模型/语义客户端注入、
│                            任务协议适配（messages→question，JSON AIMessage 出参）、
│                            会话结果文件（已取行写成 CSV 快照 + meta）、
│                            向描述追加已绑定数据库摘要（db_id/dialect/engine，不含连接参数）
└── nl2sql/                  Python 包
    ├── graph.py             StateGraph 组装（节点链实例化、路由规则 → 普通边/条件边）
    ├── state.py             NL2SQLState（langgraph MessagesState 派生）+ Result
    ├── config_manager.py    NL2SQLConfig（plugins.config.nl2sql 参数校验与点号路径读取）
    ├── prompt_manager.py    PromptTemplate（包内相对路径解析）
    ├── errors.py / constants.py
    ├── nodes/               BaseNL2SQLNode 基类 + 6 个工作流节点（perceptor→…→selector）
    ├── security/            SQLGlot 安全规则与检查
    ├── utils/               解析、SQL 服务（受限取数）
    ├── examples/sql_rules/  场景示例 SQL 规则（bird、bio_lab，默认不启用）
    └── prompts/             节点提示词模板（.md）
```

## 启用与调用

插件随 agentkit 的 builtin-plugins 目录自动发现：

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

每次查询执行成功后，子代理把 executor 取到的全部行（受 `executor.limit`
约束，单元格保留原值）写入当前会话输出目录：

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
  数据敏感的部署可关闭 `result_files`，并应给 `executor.limit` 设正数上限。

## 配置

全部配置进入 Agent 配置文件的 `plugins.config.nl2sql` 段（JSON；不再
读取独立的 YAML 文件）。`model` 必须是同配置 `models` 里的别名。
语义层服务在根级 `semantic` 段配置（由宿主创建客户端并注入，生命周期
归宿主管理），不属于本插件参数：

```json
{
  "models": {
    "primary": {
      "provider": "openai",
      "name": "deepseek-chat",
      "base_url": "https://api.deepseek.com/v1",
      "api_key": { "$env": "DEEPSEEK_API_KEY" }
    }
  },
  "semantic": {
    "base_url": "http://semantic-service",
    "username": "…",
    "password": "…",
    "timeout": 30.0
  },
  "plugins": {
    "enabled": ["nl2sql"],
    "config": {
      "nl2sql": {
        "model": "primary",
        "model_call_budget": 64,
        "database": {
          "db_id": "mydb",
          "dialect": "sqlite",
          "engine": "sqlite",
          "path": "/data/app.db"
        }
      }
    }
  }
}
```

| 参数 | 说明 |
| --- | --- |
| `model` | 必填。`models` 别名，由宿主解析并注入所有节点 |
| `model_call_budget` | 单次任务内模型调用上限（默认 64），超出即报错返回 |
| `result_files` | 是否为每次成功查询写会话结果文件（默认 `true`，见上文） |
| `database.db_id` / `dialect` / `engine` | 语义层库名 / SQL 方言（必填）/ 执行引擎（默认同方言：`sqlite`、`mysql`、`gaussvector`、`hive`/`spark`；非 sqlite 引擎的驱动 `pymysql`/`psycopg2`/`pyspark` 为可选依赖，构建时缺失会明确报错） |
| `database.*`（其余键） | 引擎连接参数，如 sqlite 的 `path`、mysql 的 `host/port/user/password/database` |
| `perceptor.schema_mode` | `full_schema` 或 `schema_linking` |
| `perceptor.user_schema` | schema .md 文件路径，注入 schema 绕过语义服务（测试后门） |
| `perceptor.user_sql_rules` | SQL 规则文件名；内置示例见 `nl2sql/examples/sql_rules/`（`sql_rules_bird` 等，默认不启用） |
| `generator.strategies` | `prompt`、`icl`、`skeleton`、`dc`；候选数 = num_workers × (num_samples×prompt + icl + skeleton + dc) |
| `generator.num_workers` | 每策略并行度，**非常昂贵** |
| `validator.sql_security_enabled` | SQLGlot 确定性安全检查，默认关；开启需启用 reflector |
| `validator.db_explain` | 用 `EXPLAIN` 校验 SQL 模式与语法 |
| `reflector.threshold` / `ref_retries` | 反思置信度阈值 / 生成-校验-反思重试次数 |
| `executor.limit` / `preview_limit` | 取行上限，也是结果文件的行数上限（-1 全量；引擎最多取 limit+1 行以判定截断）/ 预览行数（Selector 评分、日志与回复的 `rows_preview` 共用） |
| `selector.shortcut` / `threshold` / `sel_retries` | 多数票捷径（-1 关）/ 置信度阈值 / 重试次数 |

节点段（`perceptor`…`selector`）全部可省略——省略时启用完整六节点链；
出现时必须构成 `perceptor→generator→validator→reflector→executor→selector`
前缀链（在链中第一个缺失的节点处截断），且 `generator` 及其之前的节点必填。

## 与旧版的差异

- 接入方式：`nl2sql_run` 桥接工具 + 独立 YAML 配置，改为 compiled
  SubAgent——宿主编译时调用图工厂，参数来自 `plugins.config.nl2sql`，
  模型由宿主按别名解析注入（不再有插件内 `llm_manager`）。
- 语义层客户端由宿主统一创建与关闭（根配置 `semantic`，经
  `services.semantic_client` 注入）；插件不再自带客户端实现，旧版
  `plugins.config.nl2sql.semantic` 段被忽略。
- 执行器受限取数：引擎最多取 `limit+1` 行，结果统一携带
  `row_count`（已取行数）、`total_known`（是否全量）、`truncated`（是否截断）。
- 观测（JSONL 轨迹记录、性能采集、上下文转储）与场景变体
  （`business_twin`、`traffic_insight`、cloud_core 引擎）移除；
  场景 SQL 规则示例保留在 `nl2sql/examples/sql_rules/`。
- checkpoint/resume 路径未移植（旧实现中为死代码：无中断点、未配置 checkpointer）。
