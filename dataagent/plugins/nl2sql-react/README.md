# nl2sql-react · ver6.4

本插件移植自本地 `personal/nl2sql_ver6.4` 工作区（`4de4052` 加未提交修改），
适配本仓库 `15f172a1` 的根目录 Python 包与 YAML 配置。

## 启用

插件随代码提供，默认不启用。项目现有默认配置、SDK 客户端、模型构建器与 Agent 图均保持原样。
在启动使用的 `config.yaml` 中，保留现有模型等参数，加入以下配置即可启用：

```yaml
plugins:
  enabled: [nl2sql-react]
  config:
    nl2sql-react:
      database:
        engine: sqlite
        dialect: sqlite
        db_id: california_schools
        path: /absolute/path/california_schools.sqlite
        # descriptions_dir: /absolute/path/database_description
```

`path`、可选 `descriptions_dir` 接受绝对路径或 `~/...`；相对路径会被拒绝。
当前工具和探索入口仅支持 SQLite。若启用其他插件，请保留其 enabled 项及配置。
完整示例见 [config.example.yaml](../../../examples/nl2sql-react/config.example.yaml)。

从 `nl2sql-local` 升级时，在 `plugins.enabled` 和 `plugins.config` 中同时更换插件名。
直接委派的调用方改用 `subagent_type="nl2sql-react"`，工具名前缀改为 `nl2sql-react__`。
已有结果字段 `nl2sql_local_results`、`nl2sql_local_selected_query_id` 和 `outputs/nl2sql-local/` 路径继续沿用。

## ver6.4 的运行行为

1. 根 Agent 按路由提示词把数据库问题委派给 Local，完整传递题目和 Evidence。
   首次委派是提示词要求；代码不会自动替根模型构造 task 调用。
2. Local 通过真实 schema 预取、定向探查和可选 `db_explore` 准备上下文，锁定指标、
   粒度、分母、过滤和输出列。缺业务选择与缺数据库事实分别处理。
3. 首次锁定前代码调用独立 reviewer。审查、lint、输出列检查、有证据的有限修复及
   结果落盘沿用 ver6.4；lint 告警不会自动改 SQL。
4. Local 完成后，根 Agent 保留普通模型循环，可解释结果并完成用户额外要求。
   每个用户回合最多实际启动三次 Local 子任务，含失败；子任务串行，相同描述复用结果。
   每个子任务内部仍有独立的探查和答案执行预算。
5. 有且只有一个 `status: success` 的真实执行结果时自动选择其 query_id。多个成功结果
   需要根 Agent 调用 `nl2sql-react__select_result(query_id=...)`；该工具不生成或执行 SQL。
   失败重试不会抹去先前成功结果。`partial` 保留为部分结果，但不纳入上述成功候选集合。
6. 根级 `after_agent` 在最终消息的 `additional_kwargs` 中归档本回合的成功结果、
   所选 `query_id` 或 Local 的失败原因。最终消息正文由根 Agent 提供。

`direct_result_delivery` 仅兼容旧配置，默认 false；ver6.4 根级 Hook 不读取它，
设为 true 也不会恢复旧版的直接结束流程。子代理 `finish_query` 与受限自动交付能力保留。

## 配置与接口边界

- `.plugin.yaml` 注册六个子代理工具与根级 `select_result`，加载路由与生命周期 Hook。
- 内部 Python 模块统一放在 `nl2sql_react` 包下，避免与同进程的其他插件共享 `hooks`、`tools` 等导入名。
- `nl2sql_react/tools/database.py` 校验插件内 database 参数，通过已有 `AgentContext` 读取插件配置；
  不需要宿主顶层 `datasources` 或新增上下文方法。连接参数不暴露为模型工具参数。
- `dataagent/data_gateway/` 提供公共 schema、SQL 静态检查和数据库执行能力。插件通过 SDK 的
  `build_sql_service` 访问网关维护的 SQLite 驱动、元数据读取、只读限制、超时和取消。
- `dataagent/db_explore/` 保留可复用的探索 Agent；使用原生 LangChain create_agent，
  调用方显式传入模型、数据库和预算。无需扩展宿主 core/graph。
- Local、SQL reviewer 和数据库探索复用 `dataagent.model` 指定的宿主模型。
  供应商的请求参数配置在 `models.<别名>.extra_body`。
- 数据库路径的错误脱敏在插件中处理。宿主按 `subagents/react.yaml` 装配标准 Local
  子代理的工具、文件系统和 Hook。根 Agent 通过 `task(subagent_type="nl2sql-react", ...)`
  委派。Local 的模型 Hook 显式声明子图内的提前收尾和工具参数重试跳转。

## 可选功能

`plugins.config.nl2sql-react` 支持 `review_sql`、`review_mode`（critique/independent）、
`sql_lint`、`projection_check`、`schema_prefetch`、`schema_prefetch_strategy`、
`schema_sample_rows`、`schema_value_hints`、经哈希校验的 `examples` 等开关。
设置 `user_sql_rules: sql_rules_bird` 可加载 [BIRD 补充规则](prompts/sql_rules_bird.md)。
控制预算见 `nl2sql_react/query_control/state.py`，完整参数及默认值见 `nl2sql_react/tools/database.py`。
数据库探索默认预取不超过 8 张表的完整元数据，随后允许一轮工具调用。
目录超过完整预取上限时，默认先提供表名，并为描述表和取值探查各留一轮。
显式设置 `dbexplore.max_tool_rounds` 时，以该值为上限。

测试使用真实 SQLite、脚本化模型和模拟 HTTP，验证控制与兼容行为，不能代替 BIRD 模型精度实测。
