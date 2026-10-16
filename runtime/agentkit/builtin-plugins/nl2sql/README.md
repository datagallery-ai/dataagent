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
├── .plugin.json             V2 插件清单（id=nl2sql，仅声明子代理）
├── subagents/
│   ├── nl2sql.json          SubAgentSpec（kind=compiled，指向图工厂）
│   └── nl2sql.py            build_subagent 图工厂：参数/模型/语义客户端注入、
│                            任务协议适配（messages→question，JSON AIMessage 出参）
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
   返回一条 JSON `AIMessage`（`sql`、`confidence`、`columns`、
   `rows_preview`、`row_count`、`total_known`、`truncated`、`issues`、`error`）。

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
| `executor.limit` / `preview_limit` | 取行上限（-1 全量；引擎最多取 limit+1 行以判定截断）/ 预览行数 |
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
